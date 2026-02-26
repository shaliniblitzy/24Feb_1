"""
Health Status and Metrics REST API Blueprint — Feature F-401.

This module implements the health check and monitoring REST API endpoints
for the Sonatype Nexus Repository Flask application, replacing the Java
Dropwizard Metrics 4.2.25 health check endpoint from the original source
system.

Endpoints:
    GET  /api/v1/health           — Full composite health check (unauthenticated)
    GET  /api/v1/health/live      — Kubernetes liveness probe (unauthenticated)
    GET  /api/v1/health/healthz   — Alias for /live (common convention)
    GET  /api/v1/health/ready     — Kubernetes readiness probe (unauthenticated)
    GET  /api/v1/health/system    — System information (authenticated)
    GET  /api/v1/health/<name>    — Individual health check (unauthenticated)

Health Status Levels:
    - **HEALTHY**  (HTTP 200) — all checks pass
    - **DEGRADED** (HTTP 200) — non-critical checks fail (e.g. Elasticsearch)
    - **UNHEALTHY** (HTTP 503) — critical checks fail (e.g. database)

Architecture Notes:
    - Health, liveness, and readiness endpoints are **unauthenticated** so that
      monitoring infrastructure (Kubernetes, Prometheus, load balancers) can poll
      them without credentials.
    - The ``/system`` endpoint **requires authentication** because it exposes
      potentially sensitive system details (Python version, OS, database type).
    - The :class:`HealthCheckRegistry` from
      ``src.app.monitoring.health_checks`` provides the check execution engine.
      If a persistent registry is stored in ``current_app.extensions``, it is
      reused; otherwise a fresh default registry is created per request.
    - Uptime is tracked via a module-level monotonic clock snapshot captured at
      import time, preventing wall-clock drift issues.

Performance Targets (AAP Section 0.7.3):
    - ``/live`` response time < 200 ms
    - Full ``/health`` response time < 5 s

Exports:
    health_bp : flask_smorest.Blueprint
        The health API blueprint, registered with url_prefix ``/api/v1/health``.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import platform
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import flask
from flask import Response, current_app, jsonify
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields as ma_fields

from src.app import __app_name__, __version__
from src.app.auth.authentication import login_required
from src.app.monitoring.health_checks import (
    CompositeHealthResult,
    HealthCheckRegistry,
    HealthCheckResult,
    HealthStatus,
    create_default_registry,
    run_all_health_checks,
)

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source system.  Uses Python's built-in logging module per AAP Section 0.7.2.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level uptime tracker (monotonic clock)
# ---------------------------------------------------------------------------
# Captures the time at which this module was first imported.  The ``/system``
# endpoint calculates uptime as ``time.monotonic() - _start_time``.  Using
# the monotonic clock prevents wall-clock drift or NTP jump issues.
# ---------------------------------------------------------------------------

_start_time: float = time.monotonic()

# ============================================================================
# Blueprint Definition
# ============================================================================

health_bp: Blueprint = Blueprint(
    "health",
    __name__,
    url_prefix="/api/v1/health",
    description="Health status, readiness probes, and monitoring (F-401)",
)
"""Flask-smorest Blueprint for the health API endpoint group.

Registered at ``/api/v1/health`` by ``src.app.api.register_api_blueprints()``.
"""


# ============================================================================
# Inline Marshmallow Schemas for Query Parameters
# ============================================================================


class HealthQuerySchema(Schema):
    """Query parameter schema for the full health check endpoint.

    Allows callers to optionally request verbose output including full
    system information in the composite health response, or to filter
    which checks are included.

    Attributes:
        verbose: If ``true``, include extended system information in the
                 response alongside check results.
    """

    verbose = ma_fields.Boolean(
        load_default=False,
        metadata={
            "description": (
                "Include extended system information in the response"
            ),
        },
    )


# ============================================================================
# Internal Helper Functions
# ============================================================================


def _get_health_registry() -> HealthCheckRegistry:
    """Retrieve the application's health check registry.

    Looks for a persistent :class:`HealthCheckRegistry` stored in
    ``current_app.extensions['health_check_registry']``.  If none is found
    (e.g. during early startup or in minimal test configurations), a
    default registry is created on-the-fly via
    :func:`create_default_registry`.

    Returns:
        The active :class:`HealthCheckRegistry` instance.
    """
    try:
        registry: Optional[HealthCheckRegistry] = current_app.extensions.get(
            "health_check_registry"
        )
        if registry is not None:
            return registry
    except RuntimeError:
        # Outside of application context — fall back to default.
        pass

    logger.debug(
        "No persistent health registry found in app extensions; "
        "creating default registry."
    )
    return create_default_registry()


def _status_to_http_code(status: HealthStatus) -> int:
    """Map a :class:`HealthStatus` to the appropriate HTTP status code.

    - HEALTHY  → 200
    - DEGRADED → 200  (service is partially available)
    - UNHEALTHY → 503 (service unavailable)

    Args:
        status: The composite health status.

    Returns:
        An HTTP status code integer.
    """
    if status == HealthStatus.UNHEALTHY:
        return 503
    return 200


def _make_health_response(
    data: Dict[str, Any], status: HealthStatus
) -> Response:
    """Build a Flask :class:`Response` with the correct HTTP status code.

    The response body is JSON-serialised from *data* and the status code
    is derived from *status* using :func:`_status_to_http_code`.

    Args:
        data:   Dictionary payload to serialise as JSON.
        status: The health status determining the HTTP code.

    Returns:
        A fully-formed :class:`Response` object.
    """
    http_code: int = _status_to_http_code(status)
    body: str = json.dumps(data, default=str)
    return Response(
        response=body,
        status=http_code,
        mimetype="application/json",
    )


def _get_flask_version() -> str:
    """Return the installed Flask version string.

    Uses ``importlib.metadata`` (preferred) with a fallback to
    ``flask.__version__`` for environments where package metadata is
    unavailable.

    Returns:
        The Flask version string, e.g. ``'3.1.3'``.
    """
    try:
        return importlib.metadata.version("flask")
    except importlib.metadata.PackageNotFoundError:
        # Fallback for edge cases where metadata is unavailable.
        return getattr(flask, "__version__", "unknown")


def _utc_iso_now() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Example:
        ``'2026-01-15T12:34:56.789012+00:00'``

    Returns:
        An ISO 8601 formatted UTC timestamp string.
    """
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# Endpoint 1 — Full Health Check (GET /api/v1/health)
# ============================================================================


@health_bp.route("", methods=["GET"])
@health_bp.route("/", methods=["GET"])
@health_bp.arguments(HealthQuerySchema, location="query", as_kwargs=True)
def full_health_check(*, verbose: bool = False) -> Response:
    """Execute all registered health checks and return composite status.

    **No authentication required** — monitoring infrastructure (Prometheus,
    Kubernetes, load-balancers) must be able to poll this endpoint without
    credentials.

    The response includes:

    - ``status`` — overall system status (``healthy``, ``degraded``, or
      ``unhealthy``)
    - ``checks`` — array of individual check results with name, status,
      message, duration_ms, and critical flag
    - ``total_duration_ms`` — total time for all checks in milliseconds
    - ``timestamp`` — ISO 8601 UTC timestamp of the check run
    - ``node_id`` — hostname for clustered deployment identification
    - (if ``verbose=true``) ``system_info`` — extended system details

    Query Parameters:
        verbose: If ``true``, include extended system information.

    HTTP Status Codes:
        200: System is HEALTHY or DEGRADED.
        503: System is UNHEALTHY (one or more critical checks failed).

    Returns:
        JSON response with composite health check results.
    """
    logger.debug("Full health check requested (verbose=%s)", verbose)

    try:
        registry: HealthCheckRegistry = _get_health_registry()
        result: CompositeHealthResult = registry.run_all()

        logger.info(
            "Health check completed: status=%s, checks=%d, duration=%.1fms",
            result.status.value,
            len(result.checks),
            result.total_duration_ms,
        )

        response_data: Dict[str, Any] = result.to_dict()

        # If verbose is not requested, strip system_info to keep the
        # response lightweight for monitoring agents.
        if not verbose and "system_info" in response_data:
            del response_data["system_info"]

        return _make_health_response(response_data, result.status)

    except Exception as exc:
        logger.error("Health check execution failed: %s", str(exc))
        error_payload: Dict[str, Any] = {
            "status": HealthStatus.UNHEALTHY.value,
            "checks": [],
            "total_duration_ms": 0.0,
            "timestamp": _utc_iso_now(),
            "node_id": platform.node(),
            "error": f"Health check execution failed: {exc}",
        }
        return _make_health_response(error_payload, HealthStatus.UNHEALTHY)


# ============================================================================
# Endpoint 2 — Liveness Probe (GET /api/v1/health/live)
# ============================================================================


@health_bp.route("/live", methods=["GET"])
@health_bp.route("/healthz", methods=["GET"])
@health_bp.response(200, description="Application is alive")
def liveness_probe() -> Tuple[Dict[str, Any], int]:
    """Kubernetes liveness probe.

    **Lightweight check** that simply confirms the Flask process is running
    and can serve HTTP requests.  Does **not** check databases,
    Elasticsearch, BlobStores, or any other external dependency.

    This endpoint should always return HTTP 200 as long as the application
    is alive.  A non-200 response would cause Kubernetes to restart the
    pod.

    Also registered at ``/healthz`` for common convention compatibility.

    **No authentication required.**

    HTTP Status Codes:
        200: Application process is alive.

    Returns:
        A JSON dict with ``status`` and ``timestamp`` fields.
    """
    logger.debug("Liveness probe requested")
    return {
        "status": "alive",
        "timestamp": _utc_iso_now(),
    }, 200


# ============================================================================
# Endpoint 3 — Readiness Probe (GET /api/v1/health/ready)
# ============================================================================


@health_bp.route("/ready", methods=["GET"])
def readiness_probe() -> Response:
    """Kubernetes readiness probe.

    Checks whether the application is ready to serve traffic by running
    **only critical health checks** (database, blobstore).  Non-critical
    checks (Elasticsearch, system memory) are skipped to keep the probe
    lightweight and fast.

    A pod that is alive but not ready is temporarily removed from the
    Kubernetes service load balancer until readiness is restored.

    **No authentication required.**

    HTTP Status Codes:
        200: Application is ready (critical subsystems are available).
        503: Application is NOT ready (critical checks failing).

    Returns:
        JSON response indicating readiness status.
    """
    logger.debug("Readiness probe requested")

    try:
        registry: HealthCheckRegistry = _get_health_registry()
        critical_check_names: list[str] = [
            name
            for name in registry.check_names
            if registry.get_check(name) is not None
            and registry.get_check(name).critical  # type: ignore[union-attr]
        ]

        # Run only critical checks for readiness evaluation.
        critical_results: list[HealthCheckResult] = []
        total_start: float = time.monotonic()

        for name in critical_check_names:
            check_result: Optional[HealthCheckResult] = registry.run_single(
                name
            )
            if check_result is not None:
                critical_results.append(check_result)

        total_duration_ms: float = round(
            (time.monotonic() - total_start) * 1000, 2
        )

        # Determine readiness status — any critical failure means not ready.
        has_critical_failure: bool = any(
            r.status == HealthStatus.UNHEALTHY for r in critical_results
        )

        if has_critical_failure:
            overall_status = HealthStatus.UNHEALTHY
            ready: bool = False
            message: str = "Application is NOT ready — critical checks failed"
        else:
            overall_status = HealthStatus.HEALTHY
            ready = True
            message = "Application is ready to serve traffic"

        logger.info(
            "Readiness probe: ready=%s, critical_checks=%d, duration=%.1fms",
            ready,
            len(critical_results),
            total_duration_ms,
        )

        payload: Dict[str, Any] = {
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "message": message,
            "checks": [r.to_dict() for r in critical_results],
            "total_duration_ms": total_duration_ms,
            "timestamp": _utc_iso_now(),
            "node_id": platform.node(),
        }

        return _make_health_response(payload, overall_status)

    except Exception as exc:
        logger.error("Readiness probe failed: %s", str(exc))
        error_payload: Dict[str, Any] = {
            "status": "not_ready",
            "ready": False,
            "message": f"Readiness check failed: {exc}",
            "checks": [],
            "total_duration_ms": 0.0,
            "timestamp": _utc_iso_now(),
            "node_id": platform.node(),
        }
        return _make_health_response(error_payload, HealthStatus.UNHEALTHY)


# ============================================================================
# Endpoint 4 — System Information (GET /api/v1/health/system)
# ============================================================================


@health_bp.route("/system", methods=["GET"])
@health_bp.response(200, description="System information returned successfully")
@login_required
def system_information() -> Tuple[Dict[str, Any], int]:
    """Return detailed system information.

    **Requires authentication** — this endpoint exposes potentially sensitive
    details about the running system (Python version, Flask version, OS info,
    database type, node hostname).

    The response includes:

    - Application metadata (name, version)
    - Python runtime details (version, full version string)
    - Flask framework version
    - Operating system information (system, release, machine)
    - Application uptime in seconds
    - Database engine dialect
    - Node identifier for clustered deployments

    Auth:
        ``@login_required`` — aborts with HTTP 401 if not authenticated.

    HTTP Status Codes:
        200: System information returned successfully.
        401: Authentication required.

    Returns:
        JSON dict with comprehensive system diagnostics.
    """
    logger.debug("System information requested (authenticated)")

    uptime_seconds: float = round(time.monotonic() - _start_time, 2)

    # Attempt to determine database type from SQLAlchemy engine.
    db_type: str = "unknown"
    db_driver: str = "unknown"
    try:
        sqlalchemy_ext = current_app.extensions.get("sqlalchemy")
        if sqlalchemy_ext is not None:
            engine = sqlalchemy_ext.engine
            db_type = engine.dialect.name
            db_driver = engine.dialect.driver
    except Exception as db_exc:
        logger.warning(
            "Could not determine database type: %s", str(db_exc)
        )

    # Retrieve environment configuration name from app config.
    env_name: str = current_app.config.get("ENV_NAME", "production")

    system_info: Dict[str, Any] = {
        "application_name": __app_name__,
        "application_version": __version__,
        "environment": env_name,
        "python_version": platform.python_version(),
        "python_version_full": sys.version,
        "flask_version": _get_flask_version(),
        "os_system": platform.system(),
        "os_release": platform.release(),
        "os_machine": platform.machine(),
        "node_id": platform.node(),
        "uptime_seconds": uptime_seconds,
        "database_type": db_type,
        "database_driver": db_driver,
        "timestamp": _utc_iso_now(),
    }

    logger.info(
        "System information returned: app=%s version=%s uptime=%.0fs",
        __app_name__,
        __version__,
        uptime_seconds,
    )

    return system_info, 200


# ============================================================================
# Endpoint 5 — Individual Health Check (GET /api/v1/health/<check_name>)
# ============================================================================


@health_bp.route("/<string:check_name>", methods=["GET"])
@health_bp.response(200, description="Individual check result (HEALTHY or DEGRADED)")
def individual_health_check(check_name: str) -> Response:
    """Execute a single health check by name.

    Runs the check identified by *check_name* (e.g. ``'database'``,
    ``'elasticsearch'``, ``'blobstore'``, ``'disk_space'``,
    ``'system_memory'``) and returns its individual result.

    **No authentication required.**

    Args:
        check_name: The registered name of the health check to execute.

    HTTP Status Codes:
        200: Check executed and returned HEALTHY or DEGRADED.
        404: No check registered with the given name.
        503: Check returned UNHEALTHY.

    Returns:
        JSON response with the individual health check result.
    """
    logger.debug("Individual health check requested: %s", check_name)

    registry: HealthCheckRegistry = _get_health_registry()

    # Verify the check exists before attempting execution.
    if registry.get_check(check_name) is None:
        logger.warning(
            "Health check not found: '%s'. Available checks: %s",
            check_name,
            ", ".join(registry.check_names),
        )
        abort(
            404,
            message=(
                f"Health check '{check_name}' not found. "
                f"Available checks: {', '.join(registry.check_names)}"
            ),
        )

    result: Optional[HealthCheckResult] = registry.run_single(check_name)

    if result is None:
        # Should not happen after the get_check guard, but handle gracefully.
        abort(
            404,
            message=f"Health check '{check_name}' could not be executed.",
        )

    logger.info(
        "Individual check '%s': status=%s, duration=%.1fms",
        check_name,
        result.status.value,
        result.duration_ms,
    )

    return _make_health_response(result.to_dict(), result.status)


# ============================================================================
# Module Public API
# ============================================================================

__all__: list[str] = [
    "health_bp",
]
