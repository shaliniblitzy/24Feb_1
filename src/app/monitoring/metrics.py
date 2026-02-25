"""
Application Metrics Collection Module.

Defines all application-level Prometheus metrics as module-level singletons
using ``prometheus-client 0.21.1``.  These metrics are incremented / observed
throughout the application code and exposed via the ``/metrics`` endpoint in
``prometheus_exporter.py``.

Replaces **Dropwizard Metrics 4.2.25** + **Prometheus Client 0.16.0** from
the Java source system (Sonatype Nexus Repository).  Implements **Feature
F-401** (Health Checks and Monitoring) — the metrics collection component.

**Design Decisions:**

- Every metric is instantiated at module load time as a **singleton**.  The
  ``prometheus-client`` library enforces single-registration semantics so
  duplicate metric names raise ``ValueError`` — module-level definition
  prevents accidental re-registration.
- All metrics share the ``nexus_`` prefix for consistent Prometheus namespace
  isolation.
- Histogram buckets are carefully tuned based on the performance targets
  documented in AAP Section 0.7.3 (REST API < 500 ms average, cached artifact
  resolution < 200 ms).
- Event-based metric updates are wired through the Blinker event bus
  (``src.app.events``) via **lazy imports** inside
  :func:`register_event_metrics_handlers` to avoid hard coupling.

**Usage Examples:**

.. code-block:: python

    from src.app.monitoring.metrics import (
        REQUEST_COUNT, REQUEST_LATENCY, track_request_metrics,
        track_operation_duration, TASK_EXECUTION_DURATION_SECONDS,
    )

    # Direct counter increment
    REQUEST_COUNT.labels(method='GET', endpoint='api.repos', status='200').inc()

    # Convenience helper
    track_request_metrics('GET', 'api.repos', 200, 0.042)

    # Context manager for duration tracking
    with track_operation_duration(TASK_EXECUTION_DURATION_SECONDS, task_type='cleanup'):
        run_cleanup()
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Generator, Optional

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
    Summary,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 structured logging from the Java source.
# Provides INFO-level event registration messages and WARNING-level fallback
# handling when event module imports fail in register_event_metrics_handlers().
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric Namespace Prefix
# ---------------------------------------------------------------------------
# All metrics are namespaced under ``nexus_`` to avoid collisions with
# Prometheus client defaults and other application metrics.
# ---------------------------------------------------------------------------

METRIC_PREFIX: str = "nexus_"

# =========================================================================
# HTTP Request Metrics
# =========================================================================
# Tracks inbound HTTP request volume, latency distribution, and concurrency.
# Incremented by prometheus_exporter's manual instrumentation or by Flask
# ``after_request`` hooks.
# =========================================================================

REQUEST_COUNT: Counter = Counter(
    f"{METRIC_PREFIX}http_requests_total",
    "Total number of HTTP requests received",
    ["method", "endpoint", "status"],
)
"""Counter for total HTTP requests.

Labels:
    method: HTTP method (GET, POST, PUT, DELETE, PATCH, etc.)
    endpoint: Flask endpoint name (e.g. ``'api.repos'``)
    status: HTTP response status code as string (e.g. ``'200'``, ``'404'``)
"""

REQUEST_LATENCY: Histogram = Histogram(
    f"{METRIC_PREFIX}http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=(
        0.005, 0.01, 0.025, 0.05, 0.075,
        0.1, 0.25, 0.5, 0.75, 1.0,
        2.5, 5.0, 10.0,
    ),
)
"""Histogram for HTTP request latency.

Custom buckets are tuned for the REST API performance target of
**< 500 ms average** (AAP Section 0.7.3), with fine granularity below
100 ms and extended buckets up to 10 s for long-running operations.

Labels:
    method: HTTP method
    endpoint: Flask endpoint name
"""

ACTIVE_CONNECTIONS: Gauge = Gauge(
    f"{METRIC_PREFIX}active_connections",
    "Number of currently active HTTP connections",
)
"""Gauge tracking concurrent HTTP connections.

Incremented on request start and decremented on response completion.
"""

# =========================================================================
# Repository Operation Metrics
# =========================================================================
# Tracks repository lifecycle operations (create, update, delete, start,
# stop) broken down by format and repository type.
# =========================================================================

REPOSITORY_OPERATIONS_TOTAL: Counter = Counter(
    f"{METRIC_PREFIX}repository_operations_total",
    "Total repository operations (create, update, delete)",
    ["operation", "format", "type"],
)
"""Counter for repository lifecycle operations.

Labels:
    operation: One of ``'create'``, ``'update'``, ``'delete'``, ``'start'``,
        ``'stop'``
    format: Repository format — ``'maven2'``, ``'npm'``, ``'docker'``,
        ``'nuget'``, ``'pypi'``, ``'apt'``, ``'raw'``
    type: Repository type — ``'hosted'``, ``'proxy'``, ``'group'``
"""

# =========================================================================
# Artifact Upload / Download Metrics
# =========================================================================

ARTIFACT_UPLOADS_TOTAL: Counter = Counter(
    f"{METRIC_PREFIX}artifact_uploads_total",
    "Total number of artifact uploads",
    ["repository", "format"],
)
"""Counter for artifact uploads to hosted repositories.

Labels:
    repository: Repository name
    format: Artifact format
"""

ARTIFACT_DOWNLOADS_TOTAL: Counter = Counter(
    f"{METRIC_PREFIX}artifact_downloads_total",
    "Total number of artifact downloads",
    ["repository", "format"],
)
"""Counter for artifact downloads (hosted, proxy, or group).

Labels:
    repository: Repository name
    format: Artifact format
"""

ARTIFACT_UPLOAD_SIZE_BYTES: Histogram = Histogram(
    f"{METRIC_PREFIX}artifact_upload_size_bytes",
    "Size of uploaded artifacts in bytes",
    ["format"],
    buckets=(
        1024,          # 1 KB
        10240,         # 10 KB
        102400,        # 100 KB
        1048576,       # 1 MB
        10485760,      # 10 MB
        104857600,     # 100 MB
        1073741824,    # 1 GB
    ),
)
"""Histogram for artifact upload sizes.

Buckets span from 1 KB to 1 GB to cover the full range of typical
binary artifacts (POM files to Docker layers).

Labels:
    format: Artifact format
"""

# =========================================================================
# Cache Metrics
# =========================================================================
# Tracks proxy repository caching effectiveness and other cached resource
# access patterns.
# =========================================================================

CACHE_HITS_TOTAL: Counter = Counter(
    f"{METRIC_PREFIX}cache_hits_total",
    "Total number of cache hits",
    ["cache_name"],
)
"""Counter for cache hits.

Labels:
    cache_name: Logical cache name (e.g. ``'proxy_artifact'``,
        ``'metadata'``, ``'config'``)
"""

CACHE_MISSES_TOTAL: Counter = Counter(
    f"{METRIC_PREFIX}cache_misses_total",
    "Total number of cache misses",
    ["cache_name"],
)
"""Counter for cache misses.

Labels:
    cache_name: Logical cache name
"""

# =========================================================================
# BlobStore Metrics
# =========================================================================
# Tracks BlobStore storage utilisation, blob counts, and CRUD operations.
# Updated periodically by maintenance tasks.
# =========================================================================

BLOBSTORE_USAGE_BYTES: Gauge = Gauge(
    f"{METRIC_PREFIX}blobstore_usage_bytes",
    "Current BlobStore storage usage in bytes",
    ["blob_store_name", "blob_store_type"],
)
"""Gauge for current BlobStore storage utilisation.

Labels:
    blob_store_name: BlobStore name (e.g. ``'default'``)
    blob_store_type: ``'file'`` or ``'s3'``
"""

BLOBSTORE_BLOB_COUNT: Gauge = Gauge(
    f"{METRIC_PREFIX}blobstore_blob_count",
    "Current number of blobs in BlobStore",
    ["blob_store_name", "blob_store_type"],
)
"""Gauge for current blob count per BlobStore.

Labels:
    blob_store_name: BlobStore name
    blob_store_type: ``'file'`` or ``'s3'``
"""

BLOBSTORE_OPERATIONS_TOTAL: Counter = Counter(
    f"{METRIC_PREFIX}blobstore_operations_total",
    "Total BlobStore operations",
    ["blob_store_name", "operation"],
)
"""Counter for BlobStore CRUD operations.

Labels:
    blob_store_name: BlobStore name
    operation: One of ``'create'``, ``'get'``, ``'delete'``, ``'compact'``
"""

# =========================================================================
# Authentication and Security Metrics
# =========================================================================

AUTH_ATTEMPTS_TOTAL: Counter = Counter(
    f"{METRIC_PREFIX}auth_attempts_total",
    "Total authentication attempts",
    ["realm", "result"],
)
"""Counter for authentication attempts across all realms.

Labels:
    realm: Authentication realm — ``'local'``, ``'bearer_token'``, ``'jwt'``,
        ``'ldap'``, ``'sso'``
    result: ``'success'`` or ``'failure'``
"""

# =========================================================================
# Task Scheduler Metrics
# =========================================================================

TASK_EXECUTIONS_TOTAL: Counter = Counter(
    f"{METRIC_PREFIX}task_executions_total",
    "Total scheduled task executions",
    ["task_type", "status"],
)
"""Counter for scheduled task executions.

Labels:
    task_type: Task type key (e.g. ``'repository.cleanup'``,
        ``'blobstore.compact'``)
    status: ``'ok'``, ``'failed'``, or ``'canceled'``
"""

TASK_EXECUTION_DURATION_SECONDS: Histogram = Histogram(
    f"{METRIC_PREFIX}task_execution_duration_seconds",
    "Duration of scheduled task executions",
    ["task_type"],
    buckets=(
        1, 5, 10, 30,        # seconds
        60, 120, 300,         # 1m, 2m, 5m
        600, 1800, 3600,      # 10m, 30m, 1h
    ),
)
"""Histogram for task execution durations.

Buckets range from 1 s to 1 h to accommodate both quick maintenance
tasks and long-running cleanup operations.

Labels:
    task_type: Task type key
"""

# =========================================================================
# Application Info Metric
# =========================================================================

APP_INFO: Info = Info(
    f"{METRIC_PREFIX}app",
    "Nexus Repository application information",
)
"""Info metric exposing static application metadata.

Set once during application startup via :func:`set_app_info`.
"""


# =========================================================================
# Helper Functions
# =========================================================================

def set_app_info(
    version: str,
    python_version: str,
    flask_version: str,
) -> None:
    """Set application metadata in the Prometheus info metric.

    Called **once** during application startup from
    ``src/app/factory.py:create_app()``.

    Args:
        version: Application version string (e.g. ``'1.0.0'``).
        python_version: Python runtime version (e.g. ``'3.12.3'``).
        flask_version: Flask framework version (e.g. ``'3.1.3'``).
    """
    APP_INFO.info({
        "version": version,
        "python_version": python_version,
        "flask_version": flask_version,
        "app_name": "nexus-repository",
    })


def track_request_metrics(
    method: str,
    endpoint: str,
    status: int,
    duration: float,
) -> None:
    """Record HTTP request metrics in a single call.

    Increments the :data:`REQUEST_COUNT` counter and observes the request
    duration in the :data:`REQUEST_LATENCY` histogram.

    Args:
        method: HTTP method (``'GET'``, ``'POST'``, etc.).
        endpoint: Flask endpoint name.
        status: HTTP response status code (integer).
        duration: Request duration in **seconds**.
    """
    REQUEST_COUNT.labels(
        method=method,
        endpoint=endpoint,
        status=str(status),
    ).inc()
    REQUEST_LATENCY.labels(
        method=method,
        endpoint=endpoint,
    ).observe(duration)


@contextmanager
def track_operation_duration(
    histogram: Histogram,
    **labels: Any,
) -> Generator[None, None, None]:
    """Context manager to track operation duration in a Histogram.

    Uses :func:`time.monotonic` for drift-free duration measurement.

    Args:
        histogram: The Prometheus :class:`~prometheus_client.Histogram`
            to observe the duration in.
        **labels: Label key-value pairs to apply to the histogram
            observation.

    Yields:
        ``None`` — the caller executes its code block inside the ``with``
        statement.

    Example::

        with track_operation_duration(
            TASK_EXECUTION_DURATION_SECONDS,
            task_type='cleanup',
        ):
            perform_cleanup()
    """
    start: float = time.monotonic()
    try:
        yield
    finally:
        duration: float = time.monotonic() - start
        histogram.labels(**labels).observe(duration)


def timed_metric(
    histogram: Histogram,
    **labels: Any,
) -> Callable:
    """Decorator to track function execution duration in a Histogram.

    The decorated function's ``__name__``, ``__doc__``, and other metadata
    are preserved via :func:`functools.wraps`.

    Args:
        histogram: The Prometheus :class:`~prometheus_client.Histogram`
            to observe the duration in.
        **labels: Label key-value pairs to apply to the histogram
            observation.

    Returns:
        A decorator that wraps the target function.

    Example::

        @timed_metric(TASK_EXECUTION_DURATION_SECONDS, task_type='cleanup')
        def run_cleanup():
            ...
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start: float = time.monotonic()
            try:
                return func(*args, **kwargs)
            finally:
                duration: float = time.monotonic() - start
                histogram.labels(**labels).observe(duration)

        return wrapper

    return decorator


# =========================================================================
# Event-Based Metric Updates
# =========================================================================

def register_event_metrics_handlers() -> None:
    """Register event bus subscribers that automatically update Prometheus
    metrics in response to application events.

    This function subscribes lightweight handler callbacks to the Blinker
    event bus (``src.app.events.event_bus``) for eight event types.  Each
    handler increments the appropriate Prometheus counter when the
    corresponding event fires.

    The imports are performed **lazily** (inside this function, wrapped in
    a ``try … except ImportError``) to avoid hard coupling between the
    monitoring and events packages.  If the events module is not available
    (e.g. during isolated unit testing of metrics), a warning is logged and
    the function returns gracefully.

    Called once during application startup from ``src/app/factory.py``.

    Event → Metric Mapping:

    - ``REPOSITORY_CREATED`` / ``REPOSITORY_UPDATED`` / ``REPOSITORY_DELETED``
      → :data:`REPOSITORY_OPERATIONS_TOTAL`
    - ``COMPONENT_UPLOADED`` → :data:`ARTIFACT_UPLOADS_TOTAL`
    - ``ASSET_DOWNLOADED`` → :data:`ARTIFACT_DOWNLOADS_TOTAL`
    - ``USER_AUTHENTICATED`` / ``USER_AUTHORIZATION_FAILED``
      → :data:`AUTH_ATTEMPTS_TOTAL`
    - ``TASK_COMPLETED`` → :data:`TASK_EXECUTIONS_TOTAL` +
      :data:`TASK_EXECUTION_DURATION_SECONDS`
    """
    try:
        from src.app.events.event_bus import subscribe  # noqa: WPS433
        from src.app.events.event_types import EventType  # noqa: WPS433
    except ImportError as exc:
        logger.warning("Could not register event metrics handlers: %s", exc)
        return

    # -- Repository lifecycle events ----------------------------------------

    def _on_repository_event(sender: Any, **kwargs: Any) -> None:
        """Handle repository lifecycle events and update the repository
        operations counter."""
        payload: dict = kwargs.get("payload", {})
        event_type = kwargs.get("event_type", "")
        # Extract the operation verb from the dotted event type value
        # e.g. "repository.created" → "created"
        event_type_str: str = (
            event_type.value
            if hasattr(event_type, "value")
            else str(event_type)
        )
        operation: str = (
            event_type_str.split(".")[-1]
            if "." in event_type_str
            else event_type_str
        )
        fmt: str = payload.get("format", "unknown")
        repo_type: str = payload.get("type", "unknown")
        REPOSITORY_OPERATIONS_TOTAL.labels(
            operation=operation,
            format=fmt,
            type=repo_type,
        ).inc()

    # -- Artifact upload event ----------------------------------------------

    def _on_component_uploaded(sender: Any, **kwargs: Any) -> None:
        """Handle component upload events and update the artifact uploads
        counter."""
        payload: dict = kwargs.get("payload", {})
        repo: str = payload.get("repository_name", "unknown")
        fmt: str = payload.get("format", "unknown")
        ARTIFACT_UPLOADS_TOTAL.labels(
            repository=repo,
            format=fmt,
        ).inc()

    # -- Artifact download event --------------------------------------------

    def _on_asset_downloaded(sender: Any, **kwargs: Any) -> None:
        """Handle asset download events and update the artifact downloads
        counter."""
        payload: dict = kwargs.get("payload", {})
        repo: str = payload.get("repository_name", "unknown")
        fmt: str = payload.get("format", "unknown")
        ARTIFACT_DOWNLOADS_TOTAL.labels(
            repository=repo,
            format=fmt,
        ).inc()

    # -- Authentication / authorisation events ------------------------------

    def _on_auth_event(sender: Any, **kwargs: Any) -> None:
        """Handle authentication and authorisation failure events and update
        the auth attempts counter."""
        payload: dict = kwargs.get("payload", {})
        event_type = kwargs.get("event_type", "")
        realm: str = payload.get("realm", "unknown")
        event_type_str: str = (
            event_type.value
            if hasattr(event_type, "value")
            else str(event_type)
        )
        result: str = (
            "success" if "authenticated" in event_type_str else "failure"
        )
        AUTH_ATTEMPTS_TOTAL.labels(realm=realm, result=result).inc()

    # -- Task completion event ----------------------------------------------

    def _on_task_completed(sender: Any, **kwargs: Any) -> None:
        """Handle scheduled task completion events and update the task
        executions counter and duration histogram."""
        payload: dict = kwargs.get("payload", {})
        task_type: str = payload.get("task_type", "unknown")
        status: str = payload.get("status", "unknown")
        duration_ms: int = payload.get("duration_ms", 0)
        TASK_EXECUTIONS_TOTAL.labels(
            task_type=task_type,
            status=status,
        ).inc()
        if duration_ms > 0:
            TASK_EXECUTION_DURATION_SECONDS.labels(
                task_type=task_type,
            ).observe(duration_ms / 1000.0)

    # -- Subscribe to events ------------------------------------------------

    subscribe(EventType.REPOSITORY_CREATED, _on_repository_event)
    subscribe(EventType.REPOSITORY_UPDATED, _on_repository_event)
    subscribe(EventType.REPOSITORY_DELETED, _on_repository_event)
    subscribe(EventType.COMPONENT_UPLOADED, _on_component_uploaded)
    subscribe(EventType.ASSET_DOWNLOADED, _on_asset_downloaded)
    subscribe(EventType.USER_AUTHENTICATED, _on_auth_event)
    subscribe(EventType.USER_AUTHORIZATION_FAILED, _on_auth_event)
    subscribe(EventType.TASK_COMPLETED, _on_task_completed)

    logger.info(
        "Event-based metrics handlers registered for %d event types",
        8,
    )


# =========================================================================
# Public API
# =========================================================================

__all__ = [
    # Prefix constant
    "METRIC_PREFIX",
    # HTTP metrics
    "REQUEST_COUNT",
    "REQUEST_LATENCY",
    "ACTIVE_CONNECTIONS",
    # Repository metrics
    "REPOSITORY_OPERATIONS_TOTAL",
    # Artifact metrics
    "ARTIFACT_UPLOADS_TOTAL",
    "ARTIFACT_DOWNLOADS_TOTAL",
    "ARTIFACT_UPLOAD_SIZE_BYTES",
    # Cache metrics
    "CACHE_HITS_TOTAL",
    "CACHE_MISSES_TOTAL",
    # BlobStore metrics
    "BLOBSTORE_USAGE_BYTES",
    "BLOBSTORE_BLOB_COUNT",
    "BLOBSTORE_OPERATIONS_TOTAL",
    # Auth metrics
    "AUTH_ATTEMPTS_TOTAL",
    # Task metrics
    "TASK_EXECUTIONS_TOTAL",
    "TASK_EXECUTION_DURATION_SECONDS",
    # App info
    "APP_INFO",
    "set_app_info",
    # Helper functions
    "track_request_metrics",
    "track_operation_duration",
    "timed_metric",
    # Event integration
    "register_event_metrics_handlers",
]
