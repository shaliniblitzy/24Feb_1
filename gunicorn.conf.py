"""
Gunicorn configuration file for Nexus Repository Manager.

Replaces Jetty 12.0.5 as the production HTTP server (per AAP Section 0.1.3).
Gunicorn 23.0.0 serves as the WSGI server for the Flask application, providing
multi-worker process management, graceful shutdown, and production-grade HTTP
request handling.

All settings can be overridden via environment variables to support container-native
deployments per AAP Section 0.7.5 (Environment Variable Configuration rule).

Environment Variable Reference:
    GUNICORN_BIND              - Bind address (default: 0.0.0.0:8081)
    GUNICORN_BACKLOG           - Maximum pending connections (default: 2048)
    GUNICORN_WORKERS           - Number of worker processes (default: min(4, cpu*2+1))
    GUNICORN_WORKER_CLASS      - Worker type: sync, gevent, eventlet (default: sync)
    GUNICORN_WORKER_CONNECTIONS- Max simultaneous clients per worker (default: 1000)
    GUNICORN_THREADS           - Threads per worker for gthread class (default: 1)
    GUNICORN_TIMEOUT           - Worker silent timeout in seconds (default: 120)
    GUNICORN_GRACEFUL_TIMEOUT  - Graceful worker shutdown timeout (default: 30)
    GUNICORN_KEEPALIVE         - Keep-alive seconds for connections (default: 5)
    GUNICORN_ACCESS_LOG        - Access log file path or "-" for stdout (default: "-")
    GUNICORN_ERROR_LOG         - Error log file path or "-" for stderr (default: "-")
    GUNICORN_LOG_LEVEL         - Log level: debug, info, warning, error, critical (default: info)
    GUNICORN_LIMIT_REQUEST_LINE       - Max HTTP request line size (default: 8190)
    GUNICORN_LIMIT_REQUEST_FIELDS     - Max HTTP header fields (default: 100)
    GUNICORN_LIMIT_REQUEST_FIELD_SIZE - Max HTTP header field size (default: 8190)
    SSL_KEY_PATH               - Path to SSL private key file (default: None)
    SSL_CERT_PATH              - Path to SSL certificate file (default: None)

Usage:
    gunicorn -c gunicorn.conf.py "src.app:create_app()"
"""

import multiprocessing
import os

# =============================================================================
# Server Socket Configuration
# =============================================================================
# Bind address and port. Default port 8081 matches the typical Nexus Repository
# configuration. Use 0.0.0.0 to listen on all network interfaces, which is
# required for container deployments where the host IP is dynamic.
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8081")

# Maximum number of pending connections in the socket backlog queue. A higher
# value allows more connections to queue during traffic spikes before being
# rejected with a connection refused error. 2048 is appropriate for production
# repository managers that serve many concurrent CI/CD build agents.
backlog = int(os.environ.get("GUNICORN_BACKLOG", "2048"))

# =============================================================================
# Worker Process Configuration
# =============================================================================
# Number of worker processes for handling requests. The default formula
# min(4, cpu_count * 2 + 1) follows the Gunicorn recommendation while capping
# at 4 workers to comply with the C-001 resource constraint (4 CPU / 4 GB RAM
# minimum resource allocation). Each worker is an independent OS process with
# its own memory space, providing process-level isolation for request handling.
#
# For container deployments, always set GUNICORN_WORKERS explicitly to match
# the container's CPU allocation (e.g., GUNICORN_WORKERS=2 for a 1-CPU container).
_default_workers = min(4, multiprocessing.cpu_count() * 2 + 1)
workers = int(os.environ.get("GUNICORN_WORKERS", str(_default_workers)))

# Worker class determines the concurrency model:
#   - "sync"    : Synchronous workers (default, simplest, one request per worker)
#   - "gthread" : Threaded workers (use with GUNICORN_THREADS > 1)
#   - "gevent"  : Greenlet-based async workers (requires gevent package)
#   - "eventlet": Eventlet-based async workers (requires eventlet package)
# The sync worker class is the safest default for a repository manager that
# performs heavy I/O (artifact uploads/downloads, database queries, S3 calls).
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "sync")

# Maximum number of simultaneous clients per worker. Only affects async worker
# classes (gevent, eventlet). For the default sync class, each worker handles
# exactly one request at a time.
worker_connections = int(os.environ.get("GUNICORN_WORKER_CONNECTIONS", "1000"))

# Number of threads per worker process. Only meaningful with the "gthread"
# worker class. With sync workers, this setting is ignored.
threads = int(os.environ.get("GUNICORN_THREADS", "1"))

# =============================================================================
# Timeout Configuration
# =============================================================================
# Worker timeout in seconds. Workers that are silent (not sending heartbeats)
# for longer than this value are killed and restarted. Set to 120 seconds to
# accommodate large artifact transfers (Docker images, Maven assemblies) that
# may take significant time to upload or download over slow connections.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))

# Timeout for graceful worker shutdown. After receiving a restart or stop
# signal, workers have this many seconds to finish serving their current
# request before being forcefully killed. 30 seconds allows in-flight artifact
# transfers to complete during rolling deployments.
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))

# Duration in seconds to keep idle connections open. A keep-alive value of 5
# seconds reduces TCP connection overhead for clients that send multiple
# sequential requests (e.g., Maven resolving multiple transitive dependencies).
# Should be set higher when behind a load balancer with keep-alive enabled.
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

# =============================================================================
# Process Naming
# =============================================================================
# Name for the Gunicorn master and worker processes as displayed in system
# process listings (ps, top, htop). Helps operators identify the application
# in multi-service container hosts and process monitoring tools.
proc_name = "nexus-repository"

# =============================================================================
# Logging Configuration
# =============================================================================
# Access log destination. "-" means stdout, which is the correct choice for
# container deployments where logs are collected by the container runtime
# (Docker, Kubernetes). Set to a file path for traditional deployments.
accesslog = os.environ.get("GUNICORN_ACCESS_LOG", "-")

# Error log destination. "-" means stderr. Like access logs, stderr output
# is captured by container runtimes for centralized log aggregation.
errorlog = os.environ.get("GUNICORN_ERROR_LOG", "-")

# Logging level for Gunicorn's internal logger. Controls verbosity of server
# operational messages (worker lifecycle, signal handling, binding events).
# Valid values: debug, info, warning, error, critical
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Access log format. Includes all standard fields plus response time in
# microseconds (%(D)s) for observability and latency monitoring. This format
# is compatible with common log analysis tools and can be parsed by the
# application's Prometheus metrics middleware for request duration histograms.
#
# Fields:
#   %(h)s  - Remote address
#   %(l)s  - '-' (ident, always dash)
#   %(u)s  - User name from HTTP Basic auth (or '-')
#   %(t)s  - Date/time of the request
#   %(r)s  - Status line (e.g., "GET /v2/library/alpine/manifests/latest HTTP/1.1")
#   %(s)s  - HTTP status code
#   %(b)s  - Response length in bytes
#   %(f)s  - Referer header
#   %(a)s  - User-Agent header
#   %(D)s  - Response time in microseconds
access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'
)

# =============================================================================
# Request Security Limits
# =============================================================================
# Maximum size of the HTTP request line (URL + protocol) in bytes. 8190 bytes
# is the Gunicorn default and accommodates long repository paths and query
# parameters used by format-native protocol endpoints (e.g., Maven dependency
# resolution URLs, Docker manifest digests, NuGet service index queries).
limit_request_line = int(
    os.environ.get("GUNICORN_LIMIT_REQUEST_LINE", "8190")
)

# Maximum number of HTTP header fields per request. 100 is the Gunicorn
# default and is sufficient for all supported format protocols. Docker
# Registry API V2 and NuGet V3 may include additional custom headers.
limit_request_fields = int(
    os.environ.get("GUNICORN_LIMIT_REQUEST_FIELDS", "100")
)

# Maximum size of an individual HTTP header field in bytes. 8190 bytes
# accommodates large Authorization headers (Bearer tokens, base64-encoded
# credentials) and custom headers used by CI/CD integrations.
limit_request_field_size = int(
    os.environ.get("GUNICORN_LIMIT_REQUEST_FIELD_SIZE", "8190")
)

# =============================================================================
# Server Mechanics
# =============================================================================
# Preload the Flask application before forking worker processes. This provides
# two key benefits:
#   1. Memory efficiency: Shared code pages across workers via copy-on-write
#   2. Startup validation: Application initialization errors are caught in the
#      master process before any workers are spawned, preventing a fleet of
#      workers all failing with the same configuration or database error.
# The six-phase startup sequence (KERNEL → SCHEMAS → STORAGE → SECURITY →
# CAPABILITIES → SERVICES) runs once in the master, and workers inherit the
# initialized application state.
preload_app = True

# Do not daemonize the Gunicorn process. In container deployments, the
# application must run in the foreground so the container runtime can manage
# the process lifecycle (health checks, restart policies, signal forwarding).
daemon = False

# Temporary directory for buffering request bodies during upload. None means
# use the system default (/tmp). For large artifact uploads, the system temp
# directory is typically adequate. Override if /tmp is a tmpfs with limited
# space in container environments.
tmp_upload_dir = None

# =============================================================================
# SSL/TLS Configuration (Optional)
# =============================================================================
# SSL is typically terminated at a reverse proxy (Nginx, HAProxy, AWS ALB)
# in production deployments. However, direct SSL termination at Gunicorn is
# supported for standalone deployments or development environments.
#
# Set SSL_KEY_PATH and SSL_CERT_PATH environment variables to enable HTTPS.
# Certificate management is handled by src/security/ssl_manager.py (F-302).
_ssl_keyfile = os.environ.get("SSL_KEY_PATH")
keyfile = _ssl_keyfile if _ssl_keyfile else None

_ssl_certfile = os.environ.get("SSL_CERT_PATH")
certfile = _ssl_certfile if _ssl_certfile else None

# =============================================================================
# Server Lifecycle Hooks
# =============================================================================
# Gunicorn lifecycle hooks provide integration points for logging, monitoring,
# and application-specific initialization/cleanup. These hooks replace the
# Jetty LifeCycle.Listener callbacks from the original Java implementation.


def on_starting(server):
    """Called just before the master process is initialized.

    This hook fires at the very beginning of the Gunicorn startup sequence,
    before the application is loaded (if preload_app is True) and before
    any workers are forked. Use this hook for early initialization tasks
    such as verifying system prerequisites or logging server configuration.

    Args:
        server: The Gunicorn arbiter instance managing the master process.
    """
    server.log.info(
        "Nexus Repository Manager starting — "
        "bind=%s workers=%d timeout=%d preload=%s",
        bind,
        workers,
        timeout,
        preload_app,
    )


def post_fork(server, worker):
    """Called just after a worker process has been forked from the master.

    Each worker is an independent OS process with its own memory space.
    This hook is useful for per-worker initialization such as setting up
    database connections (since file descriptors cannot be shared across
    fork boundaries) or configuring worker-specific logging.

    In the Nexus Repository context, SQLAlchemy's connection pool is
    automatically recreated after fork when using preload_app=True,
    thanks to SQLAlchemy's pool disposal on fork detection.

    Args:
        server: The Gunicorn arbiter instance managing the master process.
        worker: The newly forked worker instance with its own PID.
    """
    server.log.info("Worker spawned (pid: %s)", worker.pid)


def pre_exec(server):
    """Called just before a new master process is forked (during reload).

    This hook fires when Gunicorn is executing a graceful binary upgrade
    (USR2 signal), just before the new master process is exec'd. Useful
    for logging the transition during zero-downtime deployments.

    Args:
        server: The Gunicorn arbiter instance that is about to be replaced.
    """
    server.log.info("Forked child, re-executing.")


def when_ready(server):
    """Called just after the server is started and ready to accept requests.

    All workers have been spawned and are listening on the bound socket.
    This hook signals that the application has completed its startup
    sequence (including the six-phase initialization if preload_app is True)
    and is ready to serve traffic.

    In container orchestration environments (Kubernetes, Docker Swarm),
    this is the point at which the application would pass a readiness probe.

    Args:
        server: The Gunicorn arbiter instance that is now fully operational.
    """
    server.log.info("Server is ready. Spawning workers")


def worker_int(worker):
    """Called when a worker receives the INT or QUIT signal.

    This hook fires during graceful shutdown when SIGINT or SIGQUIT is
    sent to a worker process. The worker will finish its current request
    (up to graceful_timeout seconds) before exiting.

    Per AAP Section 0.7.5 operational rules, the application handles
    SIGTERM by completing in-flight requests, flushing pending audit
    events, and cleanly stopping the scheduler before process exit.

    Args:
        worker: The worker instance that received the interrupt signal.
    """
    worker.log.info("Worker received INT or QUIT signal (pid: %s)", worker.pid)


def worker_abort(worker):
    """Called when a worker receives the ABRT signal.

    This hook fires when a worker is forcefully terminated, typically
    because it exceeded the timeout or graceful_timeout values. This
    indicates the worker was stuck processing a request and could not
    shut down gracefully — which may happen during very large artifact
    transfers that exceed the configured timeout.

    Args:
        worker: The worker instance that received the abort signal.
    """
    worker.log.info("Worker received ABORT signal (pid: %s)", worker.pid)
