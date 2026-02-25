"""
Prometheus Metrics Endpoint and Request Instrumentation.

Exposes a ``/metrics`` HTTP endpoint for Prometheus scraping and configures
automatic Flask request instrumentation. This module is the bridge between
the application's internal metric counters (defined in ``metrics.py``) and
the Prometheus monitoring infrastructure.

**Architecture Context:**
Replaces the **Java Prometheus Client 0.16.0** metrics servlet from the
Sonatype Nexus Repository source system.  Implements **Feature F-401**
(Health Checks and Monitoring) — the Prometheus-compatible metrics export
component.

**Key Responsibilities:**

1. **``/metrics`` Blueprint** — A Flask Blueprint that serves all collected
   Prometheus metrics in the standard text exposition format
   (``text/plain; version=0.0.4; charset=utf-8``).  Supports both
   single-process mode (Flask dev server) and multi-process mode
   (Gunicorn pre-fork workers).

2. **Automatic Request Instrumentation** — Integrates
   ``prometheus-flask-instrumentator`` to transparently record HTTP request
   duration, count, and size per endpoint.  Falls back to a manual
   ``before_request`` / ``after_request`` hook implementation when the
   instrumentator is unavailable.

3. **Gunicorn Multiprocess Support** — Configures the
   ``prometheus_multiproc_dir`` environment variable and provides
   ``cleanup_multiprocess_dir()`` for the ``gunicorn.conf.py``
   ``child_exit`` hook so that stale worker metrics are correctly
   aggregated and cleaned up.

**Usage:**

.. code-block:: python

    # In src/app/factory.py
    from src.app.monitoring.prometheus_exporter import init_prometheus_exporter

    def create_app(config_name: str = 'default') -> Flask:
        app = Flask(__name__)
        # ... configure app ...
        init_prometheus_exporter(app)
        return app

.. code-block:: python

    # In gunicorn.conf.py
    from src.app.monitoring.prometheus_exporter import cleanup_multiprocess_dir

    def child_exit(server, worker):
        cleanup_multiprocess_dir()
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from flask import Blueprint, Flask, Response, request
from prometheus_client import (
    CollectorRegistry,
    CONTENT_TYPE_LATEST,
    generate_latest,
    multiprocess,
    REGISTRY,
)

from src.app.extensions import metrics_registry

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 structured logging from the Java source.
# Provides structured diagnostic output for Prometheus initialisation,
# fallback warnings, and process lifecycle events.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration Constants
# ---------------------------------------------------------------------------
# These constants provide sensible defaults and are referenced by
# init_prometheus_exporter() and external configuration (gunicorn.conf.py).
# ---------------------------------------------------------------------------

DEFAULT_METRICS_PATH: str = "/metrics"
"""Default endpoint path for Prometheus scraping."""

EXCLUDED_ENDPOINTS: list[str] = ["/metrics", "/health", "/healthz", "/ready"]
"""Endpoints excluded from automatic request instrumentation.

Excluding these prevents noisy self-referential metrics (the ``/metrics``
endpoint being counted each time Prometheus scrapes it) and avoids
inflating request statistics with liveness/readiness probes.
"""

DEFAULT_MULTIPROC_DIR: str = "/tmp/prometheus_multiproc"
"""Default directory for Prometheus multiprocess metric aggregation.

Used when ``PROMETHEUS_MULTIPROC_DIR`` is not set in the Flask
application configuration.  Each Gunicorn worker writes its own
metric files here; the ``/metrics`` endpoint aggregates them on
each scrape request.
"""

# ---------------------------------------------------------------------------
# Flask Blueprint — /metrics Endpoint
# ---------------------------------------------------------------------------
# Provides a dedicated route for Prometheus scraping, separate from the
# main API Blueprints.  Registered by init_prometheus_exporter().
# ---------------------------------------------------------------------------

metrics_bp: Blueprint = Blueprint("metrics", __name__)


@metrics_bp.route("/metrics")
def prometheus_metrics() -> Response:
    """Expose Prometheus metrics for scraping.

    Returns all collected metrics in the Prometheus text exposition format.
    Supports both single-process and multiprocess (Gunicorn) modes.

    In **multiprocess mode** (when the ``prometheus_multiproc_dir`` environment
    variable is set), a fresh :class:`CollectorRegistry` is created on each
    request and populated by :class:`MultiProcessCollector` which reads metric
    files written by all Gunicorn workers.

    In **single-process mode** (Flask development server), the default global
    :data:`REGISTRY` is used directly.

    Returns:
        A :class:`flask.Response` with:
        - Body: Prometheus text exposition format
        - Content-Type: ``text/plain; version=0.0.4; charset=utf-8``
        - Status: ``200 OK``
    """
    try:
        if "prometheus_multiproc_dir" in os.environ:
            # Multiprocess mode (Gunicorn) — aggregate metrics from all workers
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)
            metrics_output = generate_latest(registry)
        else:
            # Single-process mode — use the default global registry
            metrics_output = generate_latest(REGISTRY)

        return Response(
            metrics_output,
            mimetype=CONTENT_TYPE_LATEST,
            status=200,
        )
    except Exception as exc:
        logger.error("Failed to generate Prometheus metrics: %s", exc)
        return Response(
            f"# Error generating metrics: {exc}\n",
            mimetype="text/plain; charset=utf-8",
            status=500,
        )


# ---------------------------------------------------------------------------
# Initialization — Public API
# ---------------------------------------------------------------------------


def init_prometheus_exporter(app: Flask) -> None:
    """Initialise Prometheus metrics instrumentation for the Flask application.

    This function performs three setup steps in order:

    1. **Multiprocess directory** — If ``PROMETHEUS_MULTIPROC_DIR`` is set in
       the Flask configuration, propagate it to the ``prometheus_multiproc_dir``
       environment variable and ensure the directory exists.
    2. **Automatic request instrumentation** — Attempt to use
       ``prometheus-flask-instrumentator`` for transparent per-endpoint
       latency / count / size tracking.  If the package is unavailable or
       fails to initialise, fall back to manual ``before_request`` /
       ``after_request`` hooks that use :data:`REQUEST_COUNT` and
       :data:`REQUEST_LATENCY` from :mod:`src.app.monitoring.metrics`.
    3. **``/metrics`` Blueprint** — Register :data:`metrics_bp` on the
       application so that the ``/metrics`` endpoint is available for
       Prometheus scraping.

    Args:
        app: The Flask application instance to instrument.

    Note:
        Called from :func:`src.app.factory.create_app` during application
        bootstrap.

    Configuration Keys:
        ``METRICS_ENABLED``
            Set to ``False`` to disable all Prometheus instrumentation.
            Defaults to ``True``.
        ``PROMETHEUS_MULTIPROC_DIR``
            Filesystem path for Gunicorn multiprocess metric aggregation.
            When set, each worker writes its metrics to this directory.
    """
    # ------------------------------------------------------------------ #
    # Guard: metrics disabled by configuration
    # ------------------------------------------------------------------ #
    if not app.config.get("METRICS_ENABLED", True):
        logger.info("Prometheus metrics disabled via METRICS_ENABLED=False")
        return

    # ------------------------------------------------------------------ #
    # Step 1: Configure multiprocess directory for Gunicorn
    # ------------------------------------------------------------------ #
    multiproc_dir: Optional[str] = app.config.get("PROMETHEUS_MULTIPROC_DIR")
    if multiproc_dir:
        os.environ["prometheus_multiproc_dir"] = multiproc_dir
        os.makedirs(multiproc_dir, exist_ok=True)
        logger.info("Prometheus multiprocess mode enabled: %s", multiproc_dir)

    # ------------------------------------------------------------------ #
    # Step 2: Automatic request instrumentation
    # ------------------------------------------------------------------ #
    try:
        from prometheus_flask_instrumentator import Instrumentator

        instrumentator = Instrumentator(
            should_group_status_codes=True,
            should_ignore_untemplated=True,
            excluded_handlers=["/metrics", "/health"],
            round_latency_decimals=4,
        )
        instrumentator.instrument(app)
        logger.info("Prometheus Flask instrumentator initialised successfully")
    except ImportError:
        logger.warning(
            "prometheus-flask-instrumentator package not available; "
            "falling back to manual instrumentation"
        )
        _setup_manual_instrumentation(app)
    except Exception as exc:
        logger.warning(
            "Failed to initialise prometheus-flask-instrumentator, "
            "using manual instrumentation: %s",
            exc,
        )
        _setup_manual_instrumentation(app)

    # ------------------------------------------------------------------ #
    # Step 3: Register the /metrics Blueprint
    # ------------------------------------------------------------------ #
    app.register_blueprint(metrics_bp)
    logger.info("Prometheus /metrics endpoint registered at %s", DEFAULT_METRICS_PATH)


# ---------------------------------------------------------------------------
# Manual Instrumentation Fallback
# ---------------------------------------------------------------------------


def _setup_manual_instrumentation(app: Flask) -> None:
    """Set up manual request instrumentation using prometheus-client directly.

    This is the fallback path when ``prometheus-flask-instrumentator`` is
    unavailable or fails to initialise.  It attaches ``before_request`` and
    ``after_request`` hooks to the Flask application that measure per-request
    latency and increment the :data:`REQUEST_COUNT` counter and observe into
    the :data:`REQUEST_LATENCY` histogram defined in
    :mod:`src.app.monitoring.metrics`.

    The instrumentation automatically excludes endpoints listed in
    :data:`EXCLUDED_ENDPOINTS` to prevent noisy self-referential metrics.

    Args:
        app: The Flask application instance to instrument.
    """
    import time

    from src.app.monitoring.metrics import REQUEST_COUNT, REQUEST_LATENCY

    @app.before_request
    def _start_timer() -> None:
        """Record the request start time on the request context."""
        request._prometheus_start_time = time.time()  # type: ignore[attr-defined]

    @app.after_request
    def _record_metrics(response: Response) -> Response:
        """Compute latency and record request metrics after each response.

        Skips instrumentation for endpoints listed in
        :data:`EXCLUDED_ENDPOINTS`.
        """
        if hasattr(request, "_prometheus_start_time"):
            # Skip excluded endpoints to avoid self-scraping noise
            endpoint: str = request.endpoint or "unknown"
            if request.path in EXCLUDED_ENDPOINTS:
                return response

            latency: float = time.time() - request._prometheus_start_time  # type: ignore[attr-defined]
            method: str = request.method
            status: str = str(response.status_code)

            REQUEST_COUNT.labels(
                method=method,
                endpoint=endpoint,
                status=status,
            ).inc()

            REQUEST_LATENCY.labels(
                method=method,
                endpoint=endpoint,
            ).observe(latency)

        return response

    logger.info("Manual Prometheus instrumentation initialised")


# ---------------------------------------------------------------------------
# Gunicorn Multiprocess Cleanup
# ---------------------------------------------------------------------------


def cleanup_multiprocess_dir() -> None:
    """Clean up Prometheus multiprocess directory for the current worker.

    Marks the current process as dead in the multiprocess metrics directory
    so that its stale metric files are no longer included in aggregated
    scrape responses.

    **Must** be called during Gunicorn worker restart or shutdown to prevent
    stale metrics from accumulating.  Typically referenced by
    ``gunicorn.conf.py`` in the ``child_exit`` hook:

    .. code-block:: python

        # gunicorn.conf.py
        from src.app.monitoring.prometheus_exporter import cleanup_multiprocess_dir

        def child_exit(server, worker):
            cleanup_multiprocess_dir()
    """
    multiproc_dir: Optional[str] = os.environ.get("prometheus_multiproc_dir")
    if multiproc_dir and os.path.exists(multiproc_dir):
        try:
            multiprocess.mark_process_dead(os.getpid())
            logger.debug(
                "Marked process %d as dead in multiproc dir %s",
                os.getpid(),
                multiproc_dir,
            )
        except Exception as exc:
            logger.warning("Failed to mark process %d as dead: %s", os.getpid(), exc)
    else:
        logger.debug(
            "cleanup_multiprocess_dir called but no multiproc dir configured "
            "or directory does not exist"
        )


# ---------------------------------------------------------------------------
# Module-Level Export List
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "init_prometheus_exporter",
    "metrics_bp",
    "cleanup_multiprocess_dir",
    "DEFAULT_METRICS_PATH",
    "EXCLUDED_ENDPOINTS",
]
