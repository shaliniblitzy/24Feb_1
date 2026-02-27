"""
Health checks and Prometheus metrics monitoring for the Nexus Repository application.

This package replaces Dropwizard Metrics 4.2.25 + Prometheus Client 0.16.0
from the Java source system, implementing Feature F-401 (Health Checks and Monitoring).

Components:
- health_checks: Health check registry with database, Elasticsearch, BlobStore,
  disk space, and system memory checks. Returns composite health status
  (healthy/degraded/unhealthy).
- metrics: Application metrics collection using prometheus-client 0.21.1.
  Tracks request count/latency, repository operations, artifact uploads/downloads,
  cache hit/miss ratios, BlobStore usage gauges, and active connections.
- prometheus_exporter: Prometheus /metrics endpoint using
  prometheus-flask-instrumentator with multiprocess support for Gunicorn.

Usage::

    from src.app.monitoring import HealthCheckRegistry, run_all_health_checks
    from src.app.monitoring import (
        REQUEST_LATENCY, REQUEST_COUNT,
        ARTIFACT_UPLOADS_TOTAL, ARTIFACT_DOWNLOADS_TOTAL
    )
    from src.app.monitoring import init_prometheus_exporter
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Re-exports from health_checks submodule
# ---------------------------------------------------------------------------
# Provides the complete health check public API for Feature F-401.
# Includes the HealthCheckRegistry (class for managing and running health
# checks), HealthStatus (enum: HEALTHY/DEGRADED/UNHEALTHY), data classes
# for check results, convenience functions, and 5 concrete health check
# implementations (Database, Elasticsearch, BlobStore, DiskSpace,
# SystemMemory).
# ---------------------------------------------------------------------------

from src.app.monitoring.health_checks import (
    BlobStoreHealthCheck,
    CompositeHealthResult,
    DatabaseHealthCheck,
    DiskSpaceHealthCheck,
    ElasticsearchHealthCheck,
    HealthCheckRegistry,
    HealthCheckResult,
    HealthStatus,
    SystemMemoryHealthCheck,
    run_all_health_checks,
)

# ---------------------------------------------------------------------------
# Re-exports from metrics submodule
# ---------------------------------------------------------------------------
# Provides core Prometheus metric singletons and the track_request_metrics
# helper.  Includes HTTP request metrics (REQUEST_COUNT Counter,
# REQUEST_LATENCY Histogram, ACTIVE_CONNECTIONS Gauge), repository
# operation metrics, artifact upload/download counters, cache hit/miss
# counters, BlobStore usage gauges, and the track_request_metrics
# convenience function.
# ---------------------------------------------------------------------------

from src.app.monitoring.metrics import (
    ACTIVE_CONNECTIONS,
    ARTIFACT_DOWNLOADS_TOTAL,
    ARTIFACT_UPLOADS_TOTAL,
    BLOBSTORE_BLOB_COUNT,
    BLOBSTORE_USAGE_BYTES,
    CACHE_HITS_TOTAL,
    CACHE_MISSES_TOTAL,
    REPOSITORY_OPERATIONS_TOTAL,
    REQUEST_COUNT,
    REQUEST_LATENCY,
    track_request_metrics,
)

# ---------------------------------------------------------------------------
# Re-exports from prometheus_exporter submodule
# ---------------------------------------------------------------------------
# Provides the Prometheus exporter initialisation function and Flask
# Blueprint.  init_prometheus_exporter(app) sets up automatic request
# instrumentation and registers the /metrics endpoint; called from
# src/app/factory.py during create_app().  metrics_bp is the Flask
# Blueprint providing the /metrics Prometheus scraping endpoint with
# multiprocess support for Gunicorn.
# ---------------------------------------------------------------------------

from src.app.monitoring.prometheus_exporter import (
    init_prometheus_exporter,
    metrics_bp,
)

# ---------------------------------------------------------------------------
# Public API — __all__
# ---------------------------------------------------------------------------
# Explicit list of all names exported by this package when a consumer uses
# ``from src.app.monitoring import *``.  This list must match exactly the
# set of re-exported symbols above.
# ---------------------------------------------------------------------------

__all__: list[str] = [
    # Health checks
    "HealthCheckRegistry",
    "HealthStatus",
    "HealthCheckResult",
    "CompositeHealthResult",
    "run_all_health_checks",
    "DatabaseHealthCheck",
    "ElasticsearchHealthCheck",
    "BlobStoreHealthCheck",
    "DiskSpaceHealthCheck",
    "SystemMemoryHealthCheck",
    # Metrics
    "REQUEST_COUNT",
    "REQUEST_LATENCY",
    "ACTIVE_CONNECTIONS",
    "REPOSITORY_OPERATIONS_TOTAL",
    "ARTIFACT_UPLOADS_TOTAL",
    "ARTIFACT_DOWNLOADS_TOTAL",
    "CACHE_HITS_TOTAL",
    "CACHE_MISSES_TOTAL",
    "BLOBSTORE_USAGE_BYTES",
    "BLOBSTORE_BLOB_COUNT",
    "track_request_metrics",
    # Prometheus exporter
    "init_prometheus_exporter",
    "metrics_bp",
]
