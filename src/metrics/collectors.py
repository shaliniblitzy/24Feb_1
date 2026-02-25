"""
Prometheus Metric Collectors

Defines all application-level Prometheus metrics for the Nexus Repository
Manager. Metric objects are module-level singletons registered with the
default prometheus_client registry.

All metric types (Counter, Histogram, Gauge, Info) from prometheus_client
automatically register themselves with the global default CollectorRegistry
upon instantiation. The ``/service/metrics`` endpoint (implemented in
``src/metrics/health.py``) calls ``generate_latest()`` to serialize every
registered collector into the Prometheus text exposition format.

Metrics defined:
    - http_requests_total: Counter for total HTTP requests
      (method, endpoint, status)
    - http_request_duration_seconds: Histogram for request latency
      (method, endpoint)
    - blob_operations_total: Counter for BlobStore operations
      (operation, store_type, result)
    - auth_attempts_total: Counter for authentication attempts
      (result, realm)
    - active_repositories_count: Gauge for active repository count
      (format, type)
    - scheduler_executions_total: Counter for scheduled task runs
      (task_type, result)
    - app_info: Info metric with static application metadata

Usage::

    from src.metrics.collectors import http_requests_total
    http_requests_total.labels(
        method='GET', endpoint='repository_list', status='200'
    ).inc()

    from src.metrics.collectors import http_request_duration_seconds
    http_request_duration_seconds.labels(
        method='GET', endpoint='repository_list'
    ).observe(0.042)

    from src.metrics.collectors import active_repositories_count
    active_repositories_count.labels(format='maven', type='hosted').inc()
    active_repositories_count.labels(format='maven', type='hosted').dec()
    active_repositories_count.labels(format='maven', type='hosted').set(5)

Thread Safety:
    All prometheus_client metric types are inherently thread-safe.
    Concurrent calls to ``labels()``, ``inc()``, ``observe()``,
    ``set()``, and ``dec()`` are safe without external locking.

Replaces: Dropwizard Metrics 4.2.25 and Prometheus Java client 0.16.0
from the original Java stack.
"""

import platform
import sys

from prometheus_client import Counter, Histogram, Gauge, Info

# ---------------------------------------------------------------------------
# HTTP Request Metrics
# ---------------------------------------------------------------------------
# Incremented by: src/metrics/middleware.py after_request hook

http_requests_total: Counter = Counter(
    "http_requests_total",
    "Total number of HTTP requests received by the Nexus Repository Manager.",
    labelnames=["method", "endpoint", "status"],
)
"""Monotonically increasing counter of HTTP requests.

Labels:
    method  – HTTP method (GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS).
    endpoint – Flask endpoint name (e.g. ``repository_list``).  Using the
               endpoint name rather than the raw path avoids high-cardinality
               label explosion from dynamic URL segments like
               ``/repository/{name}/...``.
    status  – HTTP response status code as a string (``'200'``, ``'404'``, …).
"""

http_request_duration_seconds: Histogram = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=["method", "endpoint"],
    buckets=(
        0.005,   # 5 ms   – fast cache hits
        0.01,    # 10 ms  – simple metadata reads
        0.025,   # 25 ms  – typical database queries
        0.05,    # 50 ms  – small artifact downloads
        0.1,     # 100 ms – average API call
        0.25,    # 250 ms – complex queries / medium artifacts
        0.5,     # 500 ms – large metadata operations
        1.0,     # 1 s    – large artifact transfers
        2.5,     # 2.5 s  – very large operations
        5.0,     # 5 s    – timeouts approaching
        10.0,    # 10 s   – at the edge of acceptable latency
    ),
)
"""Histogram tracking the distribution of HTTP request durations.

Labels:
    method   – HTTP method.
    endpoint – Flask endpoint name.

Buckets are tuned for typical repository-manager workloads ranging from
sub-millisecond cache hits to multi-second artifact transfers.

.. note::
    The ``status`` label is intentionally *not* included here to reduce
    cardinality.  Per-status breakdowns can be obtained by joining with
    ``http_requests_total``.
"""

# ---------------------------------------------------------------------------
# BlobStore Operation Metrics
# ---------------------------------------------------------------------------
# Incremented by: src/storage/blobstore/file_blobstore.py and
#                 src/storage/blobstore/s3_blobstore.py after each operation.

blob_operations_total: Counter = Counter(
    "blob_operations_total",
    "Total number of BlobStore operations.",
    labelnames=["operation", "store_type", "result"],
)
"""Counter for binary artifact storage operations.

Labels:
    operation  – Operation type (``'store'``, ``'get'``, ``'delete'``,
                 ``'exists'``, ``'compact'``).
    store_type – BlobStore backend (``'file'``, ``'s3'``).
    result     – Outcome (``'success'``, ``'error'``, ``'not_found'``).
"""

# ---------------------------------------------------------------------------
# Authentication Metrics
# ---------------------------------------------------------------------------
# Incremented by: src/security/auth/chain.py after each authentication
# attempt.  Per AAP Section 0.7.2 all auth attempts (success *and* failure)
# must be tracked.

auth_attempts_total: Counter = Counter(
    "auth_attempts_total",
    "Total number of authentication attempts.",
    labelnames=["result", "realm"],
)
"""Counter for authentication events across all configured realms.

Labels:
    result – Outcome (``'success'``, ``'failure'``).
    realm  – Realm that handled the attempt (``'local'``, ``'bearer'``,
             ``'jwt'``, ``'ldap'``, ``'saml'``).
"""

# ---------------------------------------------------------------------------
# Repository Metrics
# ---------------------------------------------------------------------------
# Updated by: src/repositories/services.py during repository lifecycle
# state transitions (NEW → STARTED → STOPPED → DELETED).

active_repositories_count: Gauge = Gauge(
    "active_repositories_count",
    "Number of currently active (STARTED) repositories.",
    labelnames=["format", "type"],
)
"""Gauge tracking repositories in the STARTED state.

This is a *Gauge* (not a Counter) because the value can both increase
and decrease as repositories are started, stopped, or deleted.

Labels:
    format – Repository format (``'maven'``, ``'npm'``, ``'docker'``,
             ``'nuget'``, ``'pypi'``, ``'apt'``, ``'raw'``).
    type   – Repository type (``'hosted'``, ``'proxy'``, ``'group'``).
"""

# ---------------------------------------------------------------------------
# Scheduler Metrics
# ---------------------------------------------------------------------------
# Incremented by: src/admin/scheduler.py after each task execution completes.

scheduler_executions_total: Counter = Counter(
    "scheduler_executions_total",
    "Total number of scheduled task executions.",
    labelnames=["task_type", "result"],
)
"""Counter for APScheduler task execution events.

Labels:
    task_type – Task category (``'cleanup'``, ``'compaction'``,
                ``'integrity_check'``, ``'health_check'``,
                ``'proxy_cache_invalidation'``, ``'audit_rotation'``).
    result    – Outcome (``'success'``, ``'failure'``, ``'timeout'``).
"""

# ---------------------------------------------------------------------------
# Application Info Metric
# ---------------------------------------------------------------------------
# Static metadata set once at module load time.  Useful for PromQL queries
# that correlate operational metrics with specific application versions.

app_info: Info = Info(
    "nexus_repository",
    "Nexus Repository Manager application information.",
)

# Populate static application metadata.  This information is emitted as
# a Prometheus Info metric (gauge with ``{…}_info`` suffix and a constant
# value of 1) so that it appears alongside other metrics during scraping.
app_info.info(
    {
        "version": "1.0.0",
        "framework": "flask",
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": sys.platform,
    }
)
