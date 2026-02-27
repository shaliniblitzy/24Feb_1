"""
Health Check Registry and Implementations for Feature F-401.

This module provides a health check framework with a registry of checks and
concrete implementations for:
  - Database connectivity (SQLAlchemy / Flask-SQLAlchemy)
  - Elasticsearch cluster health
  - BlobStore availability (File and S3 backends)
  - Disk space utilisation
  - System memory utilisation

The composite health result reports one of three statuses:
  - **HEALTHY** — all checks pass
  - **DEGRADED** — non-critical checks fail (e.g. Elasticsearch unavailable)
  - **UNHEALTHY** — critical checks fail (e.g. database unreachable)

Replaces the Java Dropwizard Metrics 4.2.25 health check registry and the
Prometheus Client 0.16.0 health indicators from the Sonatype Nexus Repository
source system.

Architecture Notes:
    - Each health check extends the abstract ``HealthCheck`` base class
    - The ``execute()`` wrapper in the base class provides timing and fault
      tolerance — individual ``check()`` implementations should never raise
    - The ``HealthCheckRegistry`` follows the Service Locator pattern,
      collecting checks and producing a ``CompositeHealthResult``
    - Critical checks (database, blobstore, disk) mark the system UNHEALTHY
      on failure; non-critical checks (elasticsearch, memory) mark DEGRADED

Usage::

    from src.app.monitoring.health_checks import (
        HealthCheckRegistry,
        DatabaseHealthCheck,
        create_default_registry,
        run_all_health_checks,
    )

    # Option 1 — persistent registry
    registry = create_default_registry()
    result = registry.run_all()
    print(result.status)  # HealthStatus.HEALTHY

    # Option 2 — one-shot convenience
    result = run_all_health_checks()
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from flask import current_app

from src.app.extensions import db

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source.  Uses Python's built-in logging module per AAP Section 0.7.2.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)


# ============================================================================
# Health Status Enum
# ============================================================================


class HealthStatus(str, Enum):
    """Composite health status levels for the system.

    Three-level health model (not just pass/fail) that allows the system to
    report partial degradation when non-critical subsystems are unavailable.

    Extends ``str`` so that enum values serialise directly to JSON strings
    without requiring custom encoders.
    """

    HEALTHY = "healthy"
    """All registered checks pass — system is fully operational."""

    DEGRADED = "degraded"
    """One or more non-critical checks fail (e.g. Elasticsearch down)."""

    UNHEALTHY = "unhealthy"
    """One or more critical checks fail (e.g. database unreachable)."""


# ============================================================================
# Health Check Result Dataclasses
# ============================================================================


@dataclass
class HealthCheckResult:
    """Result of a single health check execution.

    Captures the outcome of one health check including timing information,
    human-readable messages, and implementation-specific detail dicts.

    Attributes:
        name:        Check name (e.g. ``'database'``, ``'elasticsearch'``).
        status:      Resulting health status.
        message:     Human-readable status message.
        details:     Additional check-specific key-value pairs.
        duration_ms: Wall-clock time taken for the check in milliseconds.
        timestamp:   ISO 8601 UTC timestamp of check execution.
        critical:    ``True`` if failure makes the system UNHEALTHY.
    """

    name: str
    status: HealthStatus
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    timestamp: str = ""
    critical: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the result to a JSON-compatible dictionary.

        Returns:
            A plain ``dict`` suitable for ``json.dumps`` or Flask's
            ``jsonify``.
        """
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp,
            "critical": self.critical,
        }


@dataclass
class CompositeHealthResult:
    """Aggregate health status produced by running all registered checks.

    Collects individual :class:`HealthCheckResult` instances and computes an
    overall system status based on criticality rules:

    * ANY critical failure → ``UNHEALTHY``
    * ANY non-critical failure (all critical pass) → ``DEGRADED``
    * All pass → ``HEALTHY``

    Attributes:
        status:           Overall system health status.
        checks:           Individual check results.
        total_duration_ms: Total time for all checks in milliseconds.
        timestamp:        ISO 8601 UTC timestamp.
        node_id:          Node identifier for clustered deployments.
    """

    status: HealthStatus
    checks: List[HealthCheckResult] = field(default_factory=list)
    total_duration_ms: float = 0.0
    timestamp: str = ""
    node_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the composite result including system information.

        The returned dictionary includes Python version, OS platform, and
        hostname to aid diagnostics in clustered deployments.

        Returns:
            A plain ``dict`` suitable for JSON serialisation.
        """
        return {
            "status": self.status.value,
            "checks": [check.to_dict() for check in self.checks],
            "total_duration_ms": self.total_duration_ms,
            "timestamp": self.timestamp,
            "node_id": self.node_id,
            "system_info": {
                "python_version": platform.python_version(),
                "os": platform.system(),
                "hostname": platform.node(),
            },
        }

    @property
    def is_healthy(self) -> bool:
        """Return ``True`` only when the overall status is HEALTHY."""
        return self.status == HealthStatus.HEALTHY

    @property
    def failed_checks(self) -> List[HealthCheckResult]:
        """Return the subset of checks that are not HEALTHY."""
        return [c for c in self.checks if c.status != HealthStatus.HEALTHY]


# ============================================================================
# Abstract Health Check Base Class
# ============================================================================


class HealthCheck(ABC):
    """Abstract base class for all health check implementations.

    Each concrete subclass verifies the availability and correctness of a
    specific subsystem (database, Elasticsearch, BlobStore, disk, memory).

    The public entry-point is :meth:`execute` which wraps :meth:`check` with
    monotonic timing and top-level exception handling so that a single
    misbehaving check can never crash the entire health-check run.

    Args:
        name:     Human-readable identifier for this check.
        critical: If ``True``, failure marks the composite status UNHEALTHY.
                  If ``False``, failure marks the composite status DEGRADED.
        timeout:  Maximum seconds to wait before marking the check as failed.
    """

    def __init__(
        self,
        name: str,
        critical: bool = True,
        timeout: float = 5.0,
    ) -> None:
        self.name: str = name
        self.critical: bool = critical
        self.timeout: float = timeout
        self._logger: logging.Logger = logging.getLogger(
            f"{__name__}.{self.__class__.__name__}"
        )

    @abstractmethod
    def check(self) -> HealthCheckResult:
        """Execute the health check and return the result.

        Implementations must:
          1. Test connectivity / availability of the subsystem.
          2. Return a :class:`HealthCheckResult` with the appropriate status.
          3. Handle all exceptions internally (never propagate).
          4. Respect ``self.timeout``.
        """

    def execute(self) -> HealthCheckResult:
        """Execute :meth:`check` with timing and top-level error handling.

        Wraps the concrete ``check()`` implementation:
          - Measures elapsed wall-clock time via ``time.monotonic()``.
          - Catches any unhandled exception and converts it to an UNHEALTHY
            result so that the caller never sees a raw exception.
          - Stamps each result with an ISO 8601 UTC timestamp.

        Returns:
            A fully-populated :class:`HealthCheckResult`.
        """
        start: float = time.monotonic()
        try:
            result: HealthCheckResult = self.check()
        except Exception as exc:
            self._logger.error(
                "Health check %s failed with exception: %s",
                self.name,
                str(exc),
            )
            result = HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=f"Check failed: {exc}",
                critical=self.critical,
            )

        duration_ms: float = (time.monotonic() - start) * 1000
        result.duration_ms = round(duration_ms, 2)
        result.timestamp = datetime.now(timezone.utc).isoformat()
        return result


# ============================================================================
# Concrete Health Check Implementations
# ============================================================================


class DatabaseHealthCheck(HealthCheck):
    """Verify database connectivity using SQLAlchemy.

    Executes a lightweight ``SELECT 1`` query to confirm the database is
    reachable.  On success, gathers metadata about the underlying engine
    (dialect, driver, connection-pool stats) while ensuring that **no
    credentials are leaked** in the details dict.

    This check is **CRITICAL** — database failure makes the system UNHEALTHY.

    Replaces the Java HikariCP 4.0.3 health indicator.
    """

    def __init__(self, timeout: float = 5.0) -> None:
        super().__init__(name="database", critical=True, timeout=timeout)

    def check(self) -> HealthCheckResult:
        """Test database connectivity with ``SELECT 1``."""
        try:
            from sqlalchemy import text  # lazy import per schema spec

            result = db.session.execute(text("SELECT 1"))
            result.close()

            # Gather safe database metadata
            engine = db.engine
            raw_url = str(engine.url)
            safe_url = (
                raw_url.split("@")[-1] if "@" in raw_url else raw_url
            )

            details: Dict[str, Any] = {
                "dialect": engine.dialect.name,
                "driver": engine.dialect.driver,
                "url": safe_url,
                "pool_size": (
                    engine.pool.size()
                    if hasattr(engine.pool, "size")
                    else "N/A"
                ),
                "pool_checkedout": (
                    engine.pool.checkedout()
                    if hasattr(engine.pool, "checkedout")
                    else "N/A"
                ),
            }

            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.HEALTHY,
                message="Database connection is healthy",
                details=details,
                critical=self.critical,
            )
        except Exception as exc:
            self._logger.error("Database health check failed: %s", str(exc))
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=f"Database unreachable: {exc}",
                details={"error": str(exc)},
                critical=self.critical,
            )


class ElasticsearchHealthCheck(HealthCheck):
    """Verify Elasticsearch connectivity and cluster health.

    Calls the Elasticsearch cluster health API and maps the cluster colour
    to our three-level :class:`HealthStatus`:

    * ``green``  → HEALTHY
    * ``yellow`` → DEGRADED
    * ``red``    → UNHEALTHY

    This check is **NON-CRITICAL** — Elasticsearch being down degrades search
    functionality (Feature F-103) but does not make the overall system
    unhealthy.

    The ``elasticsearch`` package is lazily imported with a ``try/except
    ImportError`` fallback so the application still starts even when the
    package is not installed.

    Replaces the Java Elasticsearch 2.4.3 health indicator.
    """

    def __init__(self, timeout: float = 5.0) -> None:
        super().__init__(name="elasticsearch", critical=False, timeout=timeout)

    def check(self) -> HealthCheckResult:
        """Test Elasticsearch connectivity via the cluster health API."""
        try:
            from elasticsearch import Elasticsearch  # lazy + optional

            es_url: str = current_app.config.get(
                "ELASTICSEARCH_URL", "http://localhost:9200"
            )
            es = Elasticsearch([es_url], request_timeout=self.timeout)

            health: Dict[str, Any] = es.cluster.health()
            cluster_status: str = health.get("status", "unknown")

            # Map ES status → HealthStatus
            if cluster_status == "green":
                status = HealthStatus.HEALTHY
                message = "Elasticsearch cluster is green"
            elif cluster_status == "yellow":
                status = HealthStatus.DEGRADED
                message = (
                    "Elasticsearch cluster is yellow "
                    "(some replicas unavailable)"
                )
            else:
                status = HealthStatus.UNHEALTHY
                message = f"Elasticsearch cluster status: {cluster_status}"

            details: Dict[str, Any] = {
                "cluster_name": health.get("cluster_name", "unknown"),
                "cluster_status": cluster_status,
                "number_of_nodes": health.get("number_of_nodes", 0),
                "active_shards": health.get("active_shards", 0),
                "url": es_url,
            }

            return HealthCheckResult(
                name=self.name,
                status=status,
                message=message,
                details=details,
                critical=self.critical,
            )

        except ImportError:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message="Elasticsearch client not installed",
                details={"error": "elasticsearch package not installed"},
                critical=self.critical,
            )

        except Exception as exc:
            self._logger.warning(
                "Elasticsearch health check failed: %s", str(exc)
            )
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.DEGRADED,
                message=f"Elasticsearch unreachable: {exc}",
                details={"error": str(exc)},
                critical=self.critical,
            )


class BlobStoreHealthCheck(HealthCheck):
    """Verify BlobStore availability and storage health.

    Iterates through every BlobStore in the provided registry and checks:
      - ``is_available`` attribute / property
      - ``get_metrics()`` for size, blob-count, and usage percentage

    For File BlobStores this effectively validates disk access; for S3
    BlobStores it validates bucket reachability.

    This check is **CRITICAL** — BlobStore failure means artefacts cannot be
    stored or retrieved.

    The registry dictionary can be injected at construction time or set later
    via :meth:`set_registry` once BlobStores have been initialised.
    """

    def __init__(
        self,
        blobstore_registry: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> None:
        super().__init__(name="blobstore", critical=True, timeout=timeout)
        self._registry: Dict[str, Any] = blobstore_registry or {}

    def set_registry(self, registry: Dict[str, Any]) -> None:
        """Set or replace the BlobStore registry for health checking.

        Called after BlobStores have been initialised during application
        startup or when BlobStore configuration changes.

        Args:
            registry: Mapping of store name → store instance.
        """
        self._registry = registry

    def check(self) -> HealthCheckResult:
        """Check availability and storage health of all registered BlobStores."""
        try:
            if not self._registry:
                return HealthCheckResult(
                    name=self.name,
                    status=HealthStatus.DEGRADED,
                    message="No BlobStores configured",
                    details={"stores_count": 0},
                    critical=self.critical,
                )

            stores_status: Dict[str, Dict[str, Any]] = {}
            all_healthy: bool = True

            for store_name, store in self._registry.items():
                try:
                    is_available: bool = (
                        store.is_available
                        if hasattr(store, "is_available")
                        else True
                    )
                    metrics = (
                        store.get_metrics()
                        if hasattr(store, "get_metrics")
                        else None
                    )

                    store_info: Dict[str, Any] = {
                        "type": getattr(store, "store_type", "unknown"),
                        "available": is_available,
                    }

                    if metrics is not None:
                        store_info["total_size_bytes"] = getattr(
                            metrics, "total_size_bytes", 0
                        )
                        store_info["blob_count"] = getattr(
                            metrics, "blob_count", 0
                        )
                        store_info["available_space_bytes"] = getattr(
                            metrics, "available_space_bytes", 0
                        )
                        store_info["usage_percentage"] = getattr(
                            metrics, "usage_percentage", 0.0
                        )

                    if not is_available:
                        all_healthy = False

                    stores_status[store_name] = store_info

                except Exception as inner_exc:
                    stores_status[store_name] = {
                        "available": False,
                        "error": str(inner_exc),
                    }
                    all_healthy = False

            if all_healthy:
                status = HealthStatus.HEALTHY
                message = (
                    f"All {len(self._registry)} BlobStore(s) are healthy"
                )
            else:
                status = HealthStatus.UNHEALTHY
                message = "One or more BlobStores are unhealthy"

            return HealthCheckResult(
                name=self.name,
                status=status,
                message=message,
                details={"stores": stores_status},
                critical=self.critical,
            )

        except Exception as exc:
            self._logger.error(
                "BlobStore health check failed: %s", str(exc)
            )
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=f"BlobStore check error: {exc}",
                details={"error": str(exc)},
                critical=self.critical,
            )


class DiskSpaceHealthCheck(HealthCheck):
    """Check available disk space on the data directory.

    Monitors disk utilisation via :func:`shutil.disk_usage` and reports:

    * **HEALTHY**   — usage < 85 %
    * **DEGRADED**  — usage ≥ 85 % and < 95 %
    * **UNHEALTHY** — usage ≥ 95 %

    This is a **CRITICAL** check — running out of disk space will prevent
    artefact storage and database writes.

    The data directory defaults to the ``DATA_DIR`` Flask config key, falling
    back to ``/`` if unset.
    """

    WARNING_THRESHOLD: float = 85.0
    """Percentage of disk usage above which the check returns DEGRADED."""

    CRITICAL_THRESHOLD: float = 95.0
    """Percentage of disk usage above which the check returns UNHEALTHY."""

    def __init__(
        self,
        data_dir: Optional[str] = None,
        timeout: float = 2.0,
    ) -> None:
        super().__init__(name="disk_space", critical=True, timeout=timeout)
        self._data_dir: Optional[str] = data_dir

    def check(self) -> HealthCheckResult:
        """Check disk space availability on the data directory."""
        try:
            data_dir: str = self._data_dir or current_app.config.get(
                "DATA_DIR", "/"
            )
            usage = shutil.disk_usage(data_dir)

            total_gb: float = usage.total / (1024**3)
            used_gb: float = usage.used / (1024**3)
            free_gb: float = usage.free / (1024**3)
            usage_percent: float = (
                (usage.used / usage.total) * 100 if usage.total > 0 else 0.0
            )

            if usage_percent >= self.CRITICAL_THRESHOLD:
                status = HealthStatus.UNHEALTHY
                message = (
                    f"Disk space CRITICAL: {usage_percent:.1f}% used "
                    f"({free_gb:.1f} GB free)"
                )
            elif usage_percent >= self.WARNING_THRESHOLD:
                status = HealthStatus.DEGRADED
                message = (
                    f"Disk space WARNING: {usage_percent:.1f}% used "
                    f"({free_gb:.1f} GB free)"
                )
            else:
                status = HealthStatus.HEALTHY
                message = (
                    f"Disk space OK: {usage_percent:.1f}% used "
                    f"({free_gb:.1f} GB free)"
                )

            details: Dict[str, Any] = {
                "path": data_dir,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "total_gb": round(total_gb, 2),
                "used_gb": round(used_gb, 2),
                "free_gb": round(free_gb, 2),
                "usage_percent": round(usage_percent, 2),
            }

            return HealthCheckResult(
                name=self.name,
                status=status,
                message=message,
                details=details,
                critical=self.critical,
            )

        except Exception as exc:
            self._logger.error("Disk space check failed: %s", str(exc))
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                message=f"Disk space check error: {exc}",
                details={"error": str(exc)},
                critical=self.critical,
            )


class SystemMemoryHealthCheck(HealthCheck):
    """Check system memory utilisation.

    Monitors system RAM usage and reports:

    * **HEALTHY**   — usage < 85 %
    * **DEGRADED**  — usage >= 85 % and < 95 %
    * **UNHEALTHY** — usage >= 95 %

    This check is **NON-CRITICAL** — high memory usage may degrade
    performance but does not necessarily break functionality.

    Memory information is read from ``/proc/meminfo`` on Linux and falls
    back to ``os.sysconf`` POSIX calls on other Unix-like systems.  No
    external dependencies (e.g. ``psutil``) are required.
    """

    WARNING_THRESHOLD: float = 85.0
    """Percentage of memory usage above which the check returns DEGRADED."""

    CRITICAL_THRESHOLD: float = 95.0
    """Percentage of memory usage above which the check returns UNHEALTHY."""

    def __init__(self, timeout: float = 2.0) -> None:
        super().__init__(
            name="system_memory", critical=False, timeout=timeout
        )

    def check(self) -> HealthCheckResult:
        """Check system memory utilisation."""
        try:
            mem_info: Optional[Dict[str, Any]] = self._get_memory_info()

            if mem_info is None:
                return HealthCheckResult(
                    name=self.name,
                    status=HealthStatus.HEALTHY,
                    message="Memory info not available on this platform",
                    details={"platform": platform.system()},
                    critical=self.critical,
                )

            usage_percent: float = mem_info.get("usage_percent", 0.0)

            if usage_percent >= self.CRITICAL_THRESHOLD:
                status = HealthStatus.UNHEALTHY
                message = f"Memory CRITICAL: {usage_percent:.1f}% used"
            elif usage_percent >= self.WARNING_THRESHOLD:
                status = HealthStatus.DEGRADED
                message = f"Memory WARNING: {usage_percent:.1f}% used"
            else:
                status = HealthStatus.HEALTHY
                message = f"Memory OK: {usage_percent:.1f}% used"

            return HealthCheckResult(
                name=self.name,
                status=status,
                message=message,
                details=mem_info,
                critical=self.critical,
            )

        except Exception as exc:
            self._logger.warning("Memory check failed: %s", str(exc))
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.HEALTHY,
                message=f"Memory check unavailable: {exc}",
                details={"error": str(exc)},
                critical=self.critical,
            )

    def _get_memory_info(self) -> Optional[Dict[str, Any]]:
        """Read system memory information from the operating system.

        Attempts two strategies in order:

        1. Parse ``/proc/meminfo`` (Linux) for ``MemTotal`` and
           ``MemAvailable`` fields.
        2. Fall back to ``os.sysconf`` POSIX system calls
           (``SC_PAGE_SIZE``, ``SC_PHYS_PAGES``, ``SC_AVPHYS_PAGES``).

        Returns:
            A dictionary with ``total_bytes``, ``available_bytes``,
            ``used_bytes``, ``usage_percent``, and their GB equivalents;
            or ``None`` if memory information is not available on the
            current platform.
        """
        # Strategy 1: /proc/meminfo (Linux)
        try:
            if os.path.exists("/proc/meminfo"):
                with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                    lines = fh.readlines()

                mem_data: Dict[str, int] = {}
                for line in lines:
                    parts = line.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip()
                        # Value is in kB — convert to bytes
                        value_str = parts[1].strip().split()[0]
                        mem_data[key] = int(value_str) * 1024

                total: int = mem_data.get("MemTotal", 0)
                available: int = mem_data.get("MemAvailable", 0)
                used: int = total - available
                usage_pct: float = (
                    (used / total) * 100 if total > 0 else 0.0
                )

                return {
                    "total_bytes": total,
                    "available_bytes": available,
                    "used_bytes": used,
                    "total_gb": round(total / (1024**3), 2),
                    "available_gb": round(available / (1024**3), 2),
                    "used_gb": round(used / (1024**3), 2),
                    "usage_percent": round(usage_pct, 2),
                }
        except Exception:
            pass  # Fall through to POSIX strategy

        # Strategy 2: os.sysconf (POSIX)
        try:
            page_size: int = os.sysconf("SC_PAGE_SIZE")
            total_pages: int = os.sysconf("SC_PHYS_PAGES")
            avail_pages: int = os.sysconf("SC_AVPHYS_PAGES")

            total = page_size * total_pages
            available = page_size * avail_pages
            used = total - available
            usage_pct = (used / total) * 100 if total > 0 else 0.0

            return {
                "total_bytes": total,
                "available_bytes": available,
                "used_bytes": used,
                "total_gb": round(total / (1024**3), 2),
                "available_gb": round(available / (1024**3), 2),
                "used_gb": round(used / (1024**3), 2),
                "usage_percent": round(usage_pct, 2),
            }
        except (ValueError, OSError, AttributeError):
            return None


# ============================================================================
# Health Check Registry
# ============================================================================


class HealthCheckRegistry:
    """Registry of health checks for the Nexus Repository application.

    Manages registration and execution of all health checks, producing a
    :class:`CompositeHealthResult` with the overall system status.

    **Status determination logic:**
        * If ANY critical check returns UNHEALTHY -> composite is UNHEALTHY
        * If ANY non-critical check is not HEALTHY (but all critical pass) ->
          composite is DEGRADED
        * If ALL checks are HEALTHY -> composite is HEALTHY

    Replaces the Java Dropwizard Metrics health check registry.

    Example::

        registry = HealthCheckRegistry()
        registry.register(DatabaseHealthCheck())
        registry.register(ElasticsearchHealthCheck())

        result = registry.run_all()
        print(result.status)  # HealthStatus.HEALTHY
    """

    def __init__(self) -> None:
        self._checks: Dict[str, HealthCheck] = {}
        self._logger: logging.Logger = logging.getLogger(
            f"{__name__}.HealthCheckRegistry"
        )

    def register(self, check: HealthCheck) -> None:
        """Register a health check by its name.

        If a check with the same name already exists it is silently
        replaced.

        Args:
            check: The health check instance to register.
        """
        self._checks[check.name] = check
        self._logger.debug(
            "Health check registered: %s (critical=%s)",
            check.name,
            check.critical,
        )

    def unregister(self, name: str) -> None:
        """Remove a health check by name.

        Does nothing if the name is not registered.

        Args:
            name: The name of the check to remove.
        """
        removed = self._checks.pop(name, None)
        if removed is not None:
            self._logger.debug("Health check unregistered: %s", name)

    def get_check(self, name: str) -> Optional[HealthCheck]:
        """Get a registered check by name.

        Args:
            name: The name of the check to retrieve.

        Returns:
            The :class:`HealthCheck` instance, or ``None`` if not found.
        """
        return self._checks.get(name)

    @property
    def check_names(self) -> List[str]:
        """Return a list of all registered check names."""
        return list(self._checks.keys())

    def run_all(self) -> CompositeHealthResult:
        """Execute all registered health checks and return a composite result.

        Each check is executed through its :meth:`~HealthCheck.execute`
        wrapper (which provides timing and fault tolerance).  After all
        checks complete, the composite status is computed:

        * ANY critical failure -> UNHEALTHY
        * ANY non-critical failure -> DEGRADED
        * ALL pass -> HEALTHY

        Returns:
            A :class:`CompositeHealthResult` with individual results and
            aggregate status.
        """
        start: float = time.monotonic()
        results: List[HealthCheckResult] = []

        for _name, check in self._checks.items():
            result: HealthCheckResult = check.execute()
            results.append(result)

        # ---- Composite status determination ---------------------------------
        has_critical_failure: bool = any(
            r.status == HealthStatus.UNHEALTHY and r.critical
            for r in results
        )
        has_any_failure: bool = any(
            r.status != HealthStatus.HEALTHY for r in results
        )

        if has_critical_failure:
            overall_status = HealthStatus.UNHEALTHY
        elif has_any_failure:
            overall_status = HealthStatus.DEGRADED
        else:
            overall_status = HealthStatus.HEALTHY

        total_duration: float = (time.monotonic() - start) * 1000

        composite = CompositeHealthResult(
            status=overall_status,
            checks=results,
            total_duration_ms=round(total_duration, 2),
            timestamp=datetime.now(timezone.utc).isoformat(),
            node_id=platform.node(),
        )

        self._logger.info(
            "Health check completed: status=%s, checks=%d, duration=%.1fms",
            overall_status.value,
            len(results),
            total_duration,
        )

        return composite

    def run_single(self, name: str) -> Optional[HealthCheckResult]:
        """Execute a single health check by name.

        Args:
            name: The name of the registered check to run.

        Returns:
            The :class:`HealthCheckResult`, or ``None`` if the check name
            is not registered.
        """
        check: Optional[HealthCheck] = self._checks.get(name)
        if check is not None:
            return check.execute()
        return None


# ============================================================================
# Module-Level Convenience Functions
# ============================================================================


def create_default_registry() -> HealthCheckRegistry:
    """Create a :class:`HealthCheckRegistry` with all default checks.

    Registers:
      1. :class:`DatabaseHealthCheck` (critical)
      2. :class:`ElasticsearchHealthCheck` (non-critical)
      3. :class:`BlobStoreHealthCheck` (critical)
      4. :class:`DiskSpaceHealthCheck` (critical)
      5. :class:`SystemMemoryHealthCheck` (non-critical)

    Called from ``src/app/factory.py`` during application initialisation.

    Returns:
        A fully-populated :class:`HealthCheckRegistry`.
    """
    registry = HealthCheckRegistry()
    registry.register(DatabaseHealthCheck())
    registry.register(ElasticsearchHealthCheck())
    registry.register(BlobStoreHealthCheck())
    registry.register(DiskSpaceHealthCheck())
    registry.register(SystemMemoryHealthCheck())
    logger.info(
        "Default health check registry created with %d checks",
        len(registry.check_names),
    )
    return registry


def run_all_health_checks() -> CompositeHealthResult:
    """Convenience function to create a default registry and run all checks.

    For use when a persistent registry is not needed (e.g. one-shot health
    probes).  For repeated checks, prefer creating a registry via
    :func:`create_default_registry` and calling its :meth:`run_all` method.

    Returns:
        A :class:`CompositeHealthResult` with all default checks executed.
    """
    registry: HealthCheckRegistry = create_default_registry()
    return registry.run_all()


# ============================================================================
# Module Public API
# ============================================================================

__all__: List[str] = [
    "HealthStatus",
    "HealthCheckResult",
    "CompositeHealthResult",
    "HealthCheck",
    "HealthCheckRegistry",
    "DatabaseHealthCheck",
    "ElasticsearchHealthCheck",
    "BlobStoreHealthCheck",
    "DiskSpaceHealthCheck",
    "SystemMemoryHealthCheck",
    "create_default_registry",
    "run_all_health_checks",
]
