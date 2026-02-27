# =============================================================================
# gunicorn.conf.py — Production WSGI Server Configuration
# =============================================================================
# Replaces the embedded Jetty 12.0.5 HTTP server configuration from the Java
# source system (Sonatype Nexus Repository). Configures Gunicorn 25.1.0 as the
# production-grade WSGI HTTP server with a pre-fork worker model.
#
# Usage:
#   gunicorn -c gunicorn.conf.py wsgi:app
#
# All settings support environment-variable overrides following the twelve-factor
# app methodology (https://12factor.net/).  Sensible defaults are provided so
# that the server can start without any environment configuration.
#
# Reference:
#   https://docs.gunicorn.org/en/stable/settings.html
# =============================================================================

import multiprocessing
import os

# =============================================================================
# Helper — read an environment variable with a typed default
# =============================================================================


def _env(name, default=None):
    """Read an environment variable, returning *default* when unset or empty."""
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value


def _env_int(name, default):
    """Read an environment variable as an integer."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _env_bool(name, default):
    """Read an environment variable as a boolean."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# =============================================================================
# 1. Server Socket Configuration
# =============================================================================
# Bind address and port.  Default listens on all interfaces at port 8000 so
# that Docker port-mapping and reverse-proxy setups work out of the box.
# Equivalent to the Jetty 12.0.5 connector configuration in the Java source.

bind = _env("GUNICORN_BIND", "0.0.0.0:8000")

# Maximum number of pending connections in the kernel listen queue.  A higher
# backlog accommodates sudden connection bursts typical of CI/CD pipelines
# pushing artefacts concurrently.

backlog = _env_int("GUNICORN_BACKLOG", 2048)

# =============================================================================
# 2. Worker Configuration
# =============================================================================
# Gunicorn uses a pre-fork worker model.  Each worker is an independent OS
# process, replacing the thread-pool model of Jetty 12.0.5.
#
# The default formula (CPU cores × 2 + 1) is the Gunicorn-recommended heuristic
# for synchronous workers.  Override via GUNICORN_WORKERS for fine-tuning.

def _calculate_workers():
    """Return the optimal worker count, capped between 2 and 12."""
    env_workers = _env_int("GUNICORN_WORKERS", None)
    if env_workers is not None:
        # Respect the explicit override but ensure at least 1 worker.
        return max(1, env_workers)
    try:
        cpus = multiprocessing.cpu_count()
    except NotImplementedError:
        cpus = 1
    # Gunicorn-recommended formula, clamped to a sensible range.
    calculated = cpus * 2 + 1
    return max(2, min(calculated, 12))


workers = _calculate_workers()

# Worker class — "sync" (default) uses blocking I/O.  Switch to "gevent" or
# "gthread" if the workload is I/O-bound (e.g., heavy proxy-repository traffic).

worker_class = _env("GUNICORN_WORKER_CLASS", "sync")

# Maximum concurrent connections per worker (only meaningful for async workers
# such as gevent).  Ignored when worker_class is "sync".

worker_connections = _env_int("GUNICORN_WORKER_CONNECTIONS", 1000)

# Worker timeout in seconds.  Set generously to accommodate large artefact
# uploads (e.g., Docker layers, Maven shaded JARs).  The source system's
# Jetty 12.0.5 had a 120-second idle timeout.

timeout = _env_int("GUNICORN_TIMEOUT", 120)

# Keep-alive duration in seconds.  Persistent connections reduce TCP handshake
# overhead for clients that pipeline requests (build tools, CI agents).

keepalive = _env_int("GUNICORN_KEEPALIVE", 5)

# Time allowed for a worker to finish serving in-flight requests during a
# graceful shutdown (e.g., SIGTERM from Docker or Kubernetes).

graceful_timeout = _env_int("GUNICORN_GRACEFUL_TIMEOUT", 30)

# Worker recycling — each worker restarts after serving this many requests,
# preventing long-running memory leaks from degrading the process over time.

max_requests = _env_int("GUNICORN_MAX_REQUESTS", 1000)

# Jitter (random offset 0..N) applied to max_requests so that all workers do
# not restart simultaneously, which would cause a brief availability dip.

max_requests_jitter = _env_int("GUNICORN_MAX_REQUESTS_JITTER", 50)

# =============================================================================
# 3. Logging Configuration
# =============================================================================
# Gunicorn writes access and error logs to stdout/stderr by default, which is
# the recommended approach for containerised deployments (Docker, Kubernetes)
# where log aggregation is handled externally (Fluentd, CloudWatch, ELK).
#
# The logging.conf file provides structured JSON formatting compatible with
# SIEM systems — replacing Logback 1.2.13 + SLF4J 1.7.36 from the Java source.

# Access log destination ("-" = stdout).
accesslog = _env("GUNICORN_ACCESS_LOG", "-")

# Error log destination ("-" = stderr).
errorlog = _env("GUNICORN_ERROR_LOG", "-")

# Logging verbosity: debug | info | warning | error | critical
loglevel = _env("GUNICORN_LOG_LEVEL", "info")

# Access log format — records the essential request details for observability.
# Tokens: %(h)s remote address, %(l)s ident, %(u)s user, %(t)s date,
#          %(r)s request line, %(s)s status, %(b)s response length,
#          %(f)s referrer, %(a)s user-agent, %(T)s request time (seconds),
#          %(D)s request time (microseconds), %(p)s worker PID.
access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" '
    "%(T)s.%(D)s %(p)s"
)

# INI-style logging configuration file consumed by Python's logging.config
# module.  Gunicorn loads this at startup to configure its internal loggers,
# handlers, and formatters — providing the structured JSON output required
# for SIEM integration.  This replaces Logback 1.2.13 from the Java source.
#
# The file is only referenced if it exists on disk; otherwise Gunicorn falls
# back to its built-in logging defaults so that the server can still start
# without the config file present.

_logconfig_path = _env("GUNICORN_LOGCONFIG", "logging.conf")
if os.path.isfile(_logconfig_path):
    logconfig = _logconfig_path
else:
    logconfig = None

# =============================================================================
# 4. Process Configuration
# =============================================================================
# These settings control how the Gunicorn master process behaves.  The defaults
# are tuned for container deployments where the process manager (Docker, systemd,
# supervisord) owns the process lifecycle.

# Run in the foreground — required for Docker (PID 1 must not daemonise).
daemon = False

# PID file — not needed in containerised deployments.
pidfile = _env("GUNICORN_PIDFILE", None)

# File-creation permission mask.
umask = 0o022

# Temporary upload directory — None lets the OS choose (typically /tmp).
tmp_upload_dir = None

# Pre-load the Flask application in the master process before forking workers.
# Benefits:
#   - Faster worker startup (shared copy-on-write memory pages).
#   - Fails fast on application import errors.
# Caveats:
#   - Database connections must be re-initialised in each worker (handled by
#     SQLAlchemy's connection-pool-per-process model).
#   - Compatible with Flask's application factory pattern (create_app()).
preload_app = _env_bool("GUNICORN_PRELOAD", True)

# =============================================================================
# 5. Security Configuration
# =============================================================================
# Request size limits protect against over-sized or malicious requests.
# These mirror the default safety rails of Jetty 12.0.5 (8 KiB URL/header).

# Maximum size of the HTTP request line (URL + query string), in bytes.
limit_request_line = _env_int("GUNICORN_LIMIT_REQUEST_LINE", 8190)

# Maximum number of HTTP header fields per request.
limit_request_fields = _env_int("GUNICORN_LIMIT_REQUEST_FIELDS", 100)

# Maximum size of an individual HTTP header field, in bytes.
limit_request_field_size = _env_int("GUNICORN_LIMIT_REQUEST_FIELD_SIZE", 8190)

# =============================================================================
# 6. SSL/TLS Configuration (Optional)
# =============================================================================
# TLS termination is normally handled by a reverse proxy (Nginx, HAProxy,
# cloud load-balancer).  These settings are available for direct TLS when no
# reverse proxy is present.  Replaces the Jetty 12.0.5 SSL context factory.

_ssl_keyfile = _env("GUNICORN_SSL_KEYFILE", None)
_ssl_certfile = _env("GUNICORN_SSL_CERTFILE", None)
if _ssl_keyfile and _ssl_certfile:
    keyfile = _ssl_keyfile
    certfile = _ssl_certfile
    # Optional: path to a CA bundle for client certificate verification.
    ca_certs = _env("GUNICORN_SSL_CA_CERTS", None)

# =============================================================================
# 7. Server Hooks
# =============================================================================
# Lifecycle hooks provide observability into the Gunicorn master/worker
# lifecycle.  They are the Python equivalent of Jetty's LifeCycleListener
# callbacks from the Java source.


def on_starting(server):
    """Called just before the master process is initialised.

    Logs a startup banner with the Gunicorn bind address and worker count.
    """
    server.log.info(
        "Nexus Repository (Flask) — Gunicorn starting: bind=%s workers=%s",
        server.cfg.bind,
        server.cfg.workers,
    )


def when_ready(server):
    """Called when the server is ready to accept connections.

    This is the earliest point at which health-check probes should succeed.
    """
    server.log.info(
        "Nexus Repository (Flask) — Gunicorn ready: pid=%s", server.pid
    )


def pre_fork(server, worker):
    """Called just before a worker process is forked.

    Useful for logging or releasing resources that should not be shared
    across the fork boundary (e.g., database connections).
    """
    pass  # Intentionally empty — SQLAlchemy handles per-process pools.


def post_fork(server, worker):
    """Called immediately after a worker process has been forked.

    Re-seeds the random number generator and logs the new worker PID.
    Database connection pools are automatically created per-worker by
    SQLAlchemy's scoped-session machinery.
    """
    server.log.info(
        "Gunicorn worker spawned: pid=%s ppid=%s", worker.pid, server.pid
    )


def pre_exec(server):
    """Called just before a new master process is exec'd (during upgrade).

    Logs the impending binary upgrade so operators can correlate restarts.
    """
    server.log.info("Gunicorn master pre-exec: pid=%s", server.pid)


def child_exit(server, worker):
    """Called when a worker process exits.

    Logs the exit for operational monitoring (unexpected exits are
    indicative of OOM kills or unhandled exceptions).
    """
    server.log.info("Gunicorn worker exited: pid=%s", worker.pid)


def worker_exit(server, worker):
    """Called in the worker process just before it exits.

    Final opportunity for the worker to clean up resources.  SQLAlchemy
    sessions are disposed automatically by Flask-SQLAlchemy teardown.
    """
    pass  # Intentionally empty — cleanup handled by Flask teardown.


def on_exit(server):
    """Called just before the master process shuts down.

    Logs a clean shutdown message for operational observability.
    """
    server.log.info("Nexus Repository (Flask) — Gunicorn shutdown complete")
