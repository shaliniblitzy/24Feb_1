"""
Flask Application Factory Module — ``src/app/factory.py``

This module is the central hub of the Nexus Repository Flask application,
implementing the **Application Factory** pattern (AAP Section 0.4.3).  It
replaces the following Java source system components:

+---------------------------------------+------------------------------------+
| Java Source Component                 | Replacement in this module          |
+=======================================+====================================+
| ``Launcher.java``                     | :func:`create_app` entry point     |
+---------------------------------------+------------------------------------+
| ``NodeAccessBooter.java``             | Extension and service initialization|
+---------------------------------------+------------------------------------+
| ``CapabilityRegistryBooter.java``     | Plugin/capability activation       |
+---------------------------------------+------------------------------------+
| Guice 7.0.0 DI module wiring         | ``_init_extensions()`` calls       |
+---------------------------------------+------------------------------------+
| RESTEasy 6.2.7 JAX-RS router         | ``_register_api_blueprints()``     |
+---------------------------------------+------------------------------------+
| OSGi/Karaf 4.4.4 bundle activation   | ``_register_format_blueprints()``  |
+---------------------------------------+------------------------------------+
| Guava EventBus subscriber setup       | ``_init_event_system()``           |
+---------------------------------------+------------------------------------+
| Quartz 2.3.2 scheduler bootstrap     | ``_init_scheduler()``              |
+---------------------------------------+------------------------------------+
| ``BypassHttpErrorException.java``     | ``_register_error_handlers()``     |
+---------------------------------------+------------------------------------+
| ``NexusAuthenticationFilter``         | ``_register_request_hooks()``      |
+---------------------------------------+------------------------------------+

The :func:`create_app` function is the **single entry point** for creating
configured Flask application instances, consumed by:

- ``wsgi.py`` — Production entry point for Gunicorn 25.1.0
- ``run.py`` — Development server entry point
- ``tests/conftest.py`` — Test fixtures

Design Principles:

- **Twelve-factor methodology** — Configuration driven entirely by
  environment variables with sensible defaults.
- **Blueprint modularization** — Each functional area is a Flask Blueprint.
- **Graceful degradation** — Optional subsystems (scheduler, Elasticsearch,
  Prometheus) fail gracefully without blocking application startup.
- **Python 3.12+** — Uses modern type hints and language features.
- **Zero Java dependencies** — Pure Python/Flask stack.

Exports:
    create_app : Flask application factory function.
"""

from __future__ import annotations

import atexit
import logging
import logging.config
import os
from typing import Any

from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Initialized early; reconfigured by ``_configure_logging()`` once the Flask
# application configuration is loaded.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-Level Shutdown Registry
# ---------------------------------------------------------------------------
# Stores callable cleanup hooks registered during application creation.
# ``atexit`` is used to invoke them on interpreter shutdown (Gunicorn worker
# exit, SIGTERM, etc.).  This replaces the OSGi bundle deactivation
# lifecycle from the Java source.
# ---------------------------------------------------------------------------

_shutdown_hooks: list[Any] = []


def _run_shutdown_hooks() -> None:
    """Execute all registered shutdown hooks in LIFO order.

    Called automatically via :func:`atexit.register` when the Python
    interpreter exits.  Each hook is invoked in reverse registration
    order (last registered = first executed) for correct dependency
    teardown ordering.
    """
    for hook in reversed(_shutdown_hooks):
        try:
            hook()
        except Exception as exc:
            # Shutdown hooks must never propagate exceptions
            logging.getLogger(__name__).warning(
                "Shutdown hook %s failed: %s", hook, exc
            )


atexit.register(_run_shutdown_hooks)


# ============================================================================
# Logging Configuration
# ============================================================================


def _configure_logging(config_name: str) -> None:
    """Configure the Python logging subsystem.

    Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
    Configuration sources are tried in priority order:

    1. ``logging.conf`` file (if it exists in the project root)
    2. Structured JSON-capable dict config (default)
    3. Basic config fallback

    Noisy third-party loggers (SQLAlchemy engine, Elasticsearch, boto3,
    botocore, urllib3) are suppressed to WARNING level to keep application
    logs readable.

    Args:
        config_name: The environment name (``'development'``, ``'production'``,
            ``'testing'``) used to select the appropriate log level.
    """
    # Determine the log level from environment or config name
    log_level_str: str = os.getenv("LOG_LEVEL", "").upper()
    if not log_level_str:
        match config_name:
            case "production":
                log_level_str = "INFO"
            case "testing":
                log_level_str = "WARNING"
            case _:
                log_level_str = "DEBUG"

    log_level: int = getattr(logging, log_level_str, logging.INFO)

    # Attempt to load logging.conf file (highest priority)
    logging_conf_path: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "logging.conf",
    )
    if os.path.exists(logging_conf_path):
        try:
            logging.config.fileConfig(
                logging_conf_path,
                disable_existing_loggers=False,
            )
            logger.info(
                "Logging configured from file: %s", logging_conf_path
            )
            return
        except Exception as exc:
            # Fall through to dict config
            logging.warning(
                "Failed to load logging.conf (%s), using dict config", exc
            )

    # Structured dict config with JSON-compatible formatting
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": (
                        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
                    ),
                    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
                },
                "json": {
                    "format": (
                        '{"timestamp":"%(asctime)s","level":"%(levelname)s",'
                        '"logger":"%(name)s","message":"%(message)s"}'
                    ),
                    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": (
                        "json" if config_name == "production" else "standard"
                    ),
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {
                "level": log_level_str,
                "handlers": ["console"],
            },
            "loggers": {
                "src.app": {
                    "level": log_level_str,
                    "handlers": ["console"],
                    "propagate": False,
                },
            },
        }
    )

    # Suppress noisy third-party loggers to WARNING
    for noisy_logger_name in (
        "sqlalchemy.engine",
        "elasticsearch",
        "boto3",
        "botocore",
        "urllib3",
        "werkzeug",
        "apscheduler",
        "alembic",
    ):
        logging.getLogger(noisy_logger_name).setLevel(logging.WARNING)

    logger.debug(
        "Logging configured via dictConfig: level=%s, config=%s",
        log_level_str,
        config_name,
    )


# ============================================================================
# Configuration Loading
# ============================================================================


def _load_configuration(app: Flask, config_name: str) -> None:
    """Load the environment-specific configuration into the Flask app.

    Uses :func:`config.get_config` to resolve the configuration class,
    then applies it via :meth:`Flask.config.from_object`.  Additional
    overrides are loaded from ``FLASK_``-prefixed environment variables
    via :meth:`Flask.config.from_prefixed_env`.

    Args:
        app: The Flask application instance.
        config_name: The configuration environment name.

    Raises:
        ValueError: If *config_name* does not match any known configuration.
    """
    from config import get_config

    config_class = get_config(config_name)
    app.config.from_object(config_class)

    # Load additional overrides from FLASK_-prefixed env vars
    # (twelve-factor app methodology)
    app.config.from_prefixed_env()

    logger.info(
        "Configuration loaded: %s (class=%s)",
        config_name,
        config_class.__name__,
    )


# ============================================================================
# Extension Initialization
# ============================================================================


def _init_extensions(app: Flask) -> None:
    """Initialize all Flask extensions with the application.

    Replaces Guice 7.0.0 dependency injection module wiring from the Java
    source system.  Each extension is initialized within a try/except block
    for graceful degradation — a failure in a non-critical extension must
    not prevent the application from starting.

    Extensions initialized:

    - **db** (Flask-SQLAlchemy) — Replaces MyBatis 3.5.15 + HikariCP 4.0.3
    - **migrate** (Flask-Migrate / Alembic) — Replaces Flyway 8.5.13
    - **api** (flask-smorest) — Replaces RESTEasy 6.2.7 + Swagger/OpenAPI
    - **cors** (Flask-CORS) — Cross-origin resource sharing

    Args:
        app: The Flask application instance to bind extensions to.
    """
    from src.app.extensions import api, cors, db, migrate

    # 1. SQLAlchemy ORM (replaces MyBatis 3.5.15 + HikariCP 4.0.3)
    try:
        db.init_app(app)
        logger.info("SQLAlchemy extension initialized")
    except Exception:
        logger.exception("CRITICAL: Failed to initialize SQLAlchemy")
        raise

    # 2. Flask-Migrate / Alembic (replaces Flyway 8.5.13)
    try:
        migrate.init_app(app, db)
        logger.info("Flask-Migrate extension initialized")
    except Exception:
        logger.exception("Failed to initialize Flask-Migrate")
        # Non-fatal: app can run without migrations CLI

    # 3. flask-smorest API (replaces RESTEasy 6.2.7 + Swagger/OpenAPI 2.2.20)
    #    Requires API_TITLE, API_VERSION, OPENAPI_VERSION in app.config
    try:
        # Ensure minimum required config keys for flask-smorest
        app.config.setdefault("API_TITLE", "Nexus Repository Manager API")
        app.config.setdefault("API_VERSION", "v1")
        app.config.setdefault("OPENAPI_VERSION", "3.0.3")
        app.config.setdefault("OPENAPI_URL_PREFIX", "/api/docs")
        app.config.setdefault("OPENAPI_SWAGGER_UI_PATH", "/swagger-ui")
        app.config.setdefault(
            "OPENAPI_SWAGGER_UI_URL",
            "https://cdn.jsdelivr.net/npm/swagger-ui-dist/",
        )
        api.init_app(app)
        logger.info("flask-smorest API extension initialized (OpenAPI at /api/docs)")
    except Exception:
        logger.exception("Failed to initialize flask-smorest API")
        raise

    # 4. Flask-CORS (Cross-Origin Resource Sharing)
    try:
        cors.init_app(app)
        logger.info("Flask-CORS extension initialized")
    except Exception:
        logger.exception("Failed to initialize Flask-CORS")
        # Non-fatal: app can run without CORS (same-origin only)


# ============================================================================
# Blueprint Registration
# ============================================================================


def _register_api_blueprints(app: Flask) -> None:
    """Register all REST API blueprints with flask-smorest.

    Replaces RESTEasy 6.2.7 JAX-RS resource class registration from the
    Java source.  Delegates to :func:`src.app.api.register_api_blueprints`
    which iterates the 15 API blueprints and calls
    ``api.register_blueprint()`` for each.

    Args:
        app: The Flask application instance (used for logging context).
    """
    from src.app.api import register_api_blueprints
    from src.app.extensions import api

    try:
        register_api_blueprints(api)
        logger.info("All REST API blueprints registered successfully")
    except Exception:
        logger.exception("Failed to register API blueprints")
        raise


def _register_format_blueprints(app: Flask) -> None:
    """Register all format-specific handler blueprints.

    Replaces OSGi format plugin bundle activation from the Karaf 4.4.4
    container.  Registers Flask Blueprints for Maven, npm, Docker, NuGet,
    PyPI, APT, and Raw format protocol handlers.

    Args:
        app: The Flask application instance.
    """
    from src.app.formats import register_format_blueprints

    try:
        register_format_blueprints(app)
        logger.info("All format handler blueprints registered")
    except Exception:
        logger.exception("Failed to register format handler blueprints")
        # Non-fatal: app can start without format handlers (reduced functionality)


# ============================================================================
# Error Handler Registration
# ============================================================================


def _register_error_handlers(app: Flask) -> None:
    """Register global JSON error handlers for all common HTTP status codes.

    Replaces ``BypassHttpErrorException.java`` and
    ``InvalidStateException.java`` custom error handling from the Java
    source.  All error responses use a consistent JSON envelope:

    .. code-block:: json

        {
            "error": {
                "code": 404,
                "message": "The requested resource was not found"
            }
        }

    Args:
        app: The Flask application instance.
    """
    error_messages: dict[int, str] = {
        400: "Bad Request",
        401: "Unauthorized — valid credentials are required",
        403: "Forbidden — insufficient privileges",
        404: "The requested resource was not found",
        405: "Method Not Allowed",
        409: "Conflict — the request conflicts with current state",
        422: "Unprocessable Entity — validation failed",
        429: "Too Many Requests — rate limit exceeded",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }

    def _make_error_handler(status_code: int, default_message: str):
        """Create an error handler closure for the given status code."""

        def handler(error: Any):
            # Extract message from the exception if available
            message: str = default_message
            if hasattr(error, "description") and error.description:
                message = str(error.description)

            # Log 5xx errors at ERROR level; 4xx at DEBUG
            if status_code >= 500:
                logger.error(
                    "HTTP %d error: %s (path=%s)",
                    status_code,
                    message,
                    _safe_request_path(),
                )
            else:
                logger.debug(
                    "HTTP %d: %s (path=%s)",
                    status_code,
                    message,
                    _safe_request_path(),
                )

            response = jsonify(
                {"error": {"code": status_code, "message": message}}
            )
            response.status_code = status_code
            return response

        handler.__name__ = f"handle_{status_code}"
        return handler

    for code, msg in error_messages.items():
        app.errorhandler(code)(_make_error_handler(code, msg))

    # Catch-all for unhandled exceptions
    @app.errorhandler(Exception)
    def handle_unhandled_exception(error: Exception):
        """Handle any uncaught exception with a 500 JSON response."""
        logger.exception(
            "Unhandled exception on %s: %s",
            _safe_request_path(),
            str(error),
        )
        response = jsonify(
            {
                "error": {
                    "code": 500,
                    "message": "Internal Server Error",
                }
            }
        )
        response.status_code = 500
        return response

    logger.debug("Global JSON error handlers registered")


def _safe_request_path() -> str:
    """Safely retrieve the current request path for logging.

    Returns ``'<no request>'`` when called outside a request context
    (e.g., during startup error handling).
    """
    try:
        from flask import request

        return request.path
    except RuntimeError:
        return "<no request>"


# ============================================================================
# Request Hooks
# ============================================================================


def _register_request_hooks(app: Flask) -> None:
    """Register Flask before_request, after_request, and teardown hooks.

    **before_request** — Authenticates each incoming request using the
    multi-realm authentication chain (replaces ``NexusAuthenticationFilter``
    from Apache Shiro 2.0.0).

    **after_request** — Adds standard security headers to all responses.

    **teardown_appcontext** — Cleans up the SQLAlchemy session at the end
    of each request to prevent connection leaks.

    Args:
        app: The Flask application instance.
    """
    from src.app.extensions import db

    @app.before_request
    def before_request_auth() -> None:
        """Authenticate the incoming request via the multi-realm chain.

        Skips authentication for:
        - Health check endpoints (``/api/v1/health``, ``/health``)
        - Metrics endpoint (``/metrics``)
        - OpenAPI documentation (``/api/docs``)
        - Static files

        This replaces ``NexusAuthenticationFilter`` from Apache Shiro 2.0.0.
        """
        from flask import request

        # Skip authentication for public endpoints
        skip_prefixes = (
            "/api/docs",
            "/swagger-ui",
            "/metrics",
            "/health",
            "/healthz",
            "/ready",
            "/static",
        )
        if any(request.path.startswith(prefix) for prefix in skip_prefixes):
            return None

        # Delegate to the multi-realm authentication chain
        try:
            from src.app.auth.authentication import authenticate_request

            authenticate_request()
        except Exception:
            logger.debug(
                "Authentication skipped or failed for %s %s",
                request.method,
                request.path,
            )
            # Authentication failures are handled by the auth module
            # via Flask abort() — no need to re-raise here
            return None

        return None

    @app.after_request
    def after_request_headers(response):
        """Add standard security and cache-control headers.

        These headers provide defense-in-depth security for the REST API
        and ensure correct caching behavior for artifact responses.
        """
        # Security headers
        response.headers.setdefault(
            "X-Content-Type-Options", "nosniff"
        )
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "X-XSS-Protection", "1; mode=block"
        )
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
        response.headers.setdefault(
            "Cache-Control", "no-store, no-cache, must-revalidate"
        )
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'self'"
        )

        # Server identification
        response.headers["Server"] = "Nexus-Repository/1.0.0"

        return response

    @app.teardown_appcontext
    def teardown_db_session(exception=None) -> None:
        """Clean up the SQLAlchemy session after each request.

        Ensures database connections are returned to the connection pool
        and not leaked across requests.  Replaces HikariCP 4.0.3 connection
        management from the Java source.
        """
        try:
            db.session.remove()
        except Exception:
            # Session removal should never fail, but guard defensively
            pass

    logger.debug("Request hooks registered (auth, headers, teardown)")


# ============================================================================
# Event System Initialization
# ============================================================================


def _init_event_system(app: Flask) -> None:
    """Initialize the Blinker signal-based event system.

    Replaces Guava EventBus subscriber registration from the Java source.
    Performs three setup steps:

    1. Register all built-in event subscribers (audit, metrics, cache, cleanup)
    2. Create and configure the :class:`WebhookDispatcher` for Feature F-503
    3. Emit an ``app_started`` signal for lifecycle-aware subscribers

    Args:
        app: The Flask application instance.
    """
    from src.app.events import register_all_subscribers
    from src.app.extensions import event_signals
    from src.app.webhooks.dispatcher import WebhookDispatcher

    # Step 1: Register all built-in event subscribers
    try:
        register_all_subscribers()
        logger.info(
            "Built-in event subscribers registered "
            "(audit, metrics, cache, cleanup)"
        )
    except Exception:
        logger.exception("Failed to register event subscribers")
        # Non-fatal: app can run without event subscribers

    # Step 2: Initialize the webhook dispatcher (Feature F-503)
    try:
        dispatcher = WebhookDispatcher()

        # Subscribe to all event types for webhook delivery
        dispatcher.register_event_listeners()

        # Load webhook configurations from app config or database
        with app.app_context():
            dispatcher.load_webhooks_from_config(app.config)

        # Store dispatcher in app extensions for access by API routes
        app.extensions["webhook_dispatcher"] = dispatcher

        # Register shutdown hook for graceful executor termination
        _shutdown_hooks.append(lambda: dispatcher.shutdown(wait=True))

        logger.info("WebhookDispatcher initialized and event listeners registered")
    except Exception:
        logger.exception("Failed to initialize WebhookDispatcher")
        # Non-fatal: app runs without webhook delivery

    # Step 3: Create an application lifecycle signal
    try:
        app_started_signal = event_signals.signal("app-started")
        app_started_signal.send(
            app,
            event_type="app.started",
            payload={"config": app.config.get("ENV", "development")},
        )
        logger.debug("app-started signal emitted")
    except Exception:
        logger.debug("Failed to emit app-started signal")


# ============================================================================
# Database Initialization
# ============================================================================


def _init_database(app: Flask) -> None:
    """Initialize the database: create tables and seed default data.

    In **development** and **testing** environments, calls
    ``db.create_all()`` to auto-create all SQLAlchemy model tables.  In
    **production**, Alembic migrations are the authoritative schema source.

    After table creation, the :class:`ConfigService` seeds default system
    configuration values and warms the in-memory configuration cache.

    Args:
        app: The Flask application instance.
    """
    from src.app.extensions import db

    # Import models package to register all 14 SQLAlchemy models with
    # the metadata.  This is required for db.create_all() and Alembic
    # migration detection.
    import src.app.models  # noqa: F401

    with app.app_context():
        # Create tables in development/testing (production uses Alembic)
        env_name: str = app.config.get("ENV", "development")
        auto_create: bool = app.config.get(
            "AUTO_CREATE_TABLES",
            env_name in ("development", "testing"),
        )

        if auto_create:
            try:
                db.create_all()
                logger.info(
                    "Database tables created via db.create_all() "
                    "(env=%s)",
                    env_name,
                )
            except Exception:
                logger.exception("Failed to create database tables")
                raise

        # Initialize system configuration defaults (Feature F-404)
        try:
            from src.app.services.config_service import ConfigService

            config_svc = ConfigService()
            config_svc.initialize_defaults()
            config_svc.warm_cache()

            # Store in app extensions for access by API routes and services
            app.extensions["config_service"] = config_svc

            logger.info(
                "ConfigService initialized: defaults seeded, cache warmed"
            )
        except Exception:
            logger.exception(
                "Failed to initialize ConfigService — "
                "system defaults may be missing"
            )
            # Non-fatal: app can start with degraded config management


# ============================================================================
# Scheduler Initialization
# ============================================================================


def _init_scheduler(app: Flask) -> None:
    """Initialize the task scheduling subsystem.

    Performs two initialization steps:

    1. **Raw APScheduler** (from ``extensions.py``) — Started for lightweight
       internal background tasks (cache refresh, health polling).
    2. **TaskScheduler** (Feature F-402) — Full task management lifecycle with
       database-backed task definitions, execution tracking, and cluster-aware
       scheduling via ``SQLAlchemyJobStore``.

    Both schedulers are started only when ``SCHEDULER_ENABLED`` is ``True``
    in the Flask configuration.

    Args:
        app: The Flask application instance.
    """
    from src.app.extensions import scheduler
    from src.app.scheduler.task_scheduler import TaskScheduler

    scheduler_enabled: bool = app.config.get("SCHEDULER_ENABLED", False)

    if not scheduler_enabled:
        logger.info("Task scheduler disabled via SCHEDULER_ENABLED=False")
        return

    # Step 1: Start the extensions-level BackgroundScheduler for simple tasks
    try:
        if not scheduler.running:
            scheduler.start()
            logger.info("Extensions BackgroundScheduler started")

            # Register shutdown hook
            _shutdown_hooks.append(
                lambda: scheduler.shutdown(wait=False)
            )
    except Exception:
        logger.exception("Failed to start extensions BackgroundScheduler")

    # Step 2: Initialize the full TaskScheduler (Feature F-402)
    try:
        task_scheduler = TaskScheduler()

        with app.app_context():
            task_scheduler.init_app(app)
            task_scheduler.start()

        # Store in app extensions for API route access
        app.extensions["task_scheduler"] = task_scheduler

        # Register shutdown hook
        _shutdown_hooks.append(
            lambda: task_scheduler.shutdown(wait=True)
        )

        logger.info("TaskScheduler initialized and started")
    except Exception:
        logger.exception(
            "Failed to initialize TaskScheduler — "
            "scheduled tasks will not run"
        )
        # Non-fatal: app can run without task scheduling


# ============================================================================
# Monitoring Initialization
# ============================================================================


def _init_monitoring(app: Flask) -> None:
    """Initialize health checks and Prometheus metrics instrumentation.

    Replaces Dropwizard Metrics 4.2.25 + Prometheus Client 0.16.0 from
    the Java source system.

    1. Creates the default :class:`HealthCheckRegistry` with 5 checks
       (database, Elasticsearch, BlobStore, disk space, memory).
    2. Initializes Prometheus metrics instrumentation and the ``/metrics``
       endpoint for scraping.

    Args:
        app: The Flask application instance.
    """
    from src.app.monitoring.health_checks import create_default_registry
    from src.app.monitoring.prometheus_exporter import init_prometheus_exporter

    # Step 1: Health check registry (Feature F-401)
    try:
        health_registry = create_default_registry()
        app.extensions["health_registry"] = health_registry
        logger.info(
            "Health check registry initialized with default checks"
        )
    except Exception:
        logger.exception("Failed to initialize health check registry")
        # Non-fatal: health endpoints will report unknown status

    # Step 2: Prometheus metrics (Feature F-401)
    try:
        init_prometheus_exporter(app)
        logger.info("Prometheus metrics exporter initialized")
    except Exception:
        logger.exception("Failed to initialize Prometheus metrics exporter")
        # Non-fatal: metrics endpoint will be unavailable


# ============================================================================
# Application Factory — Public API
# ============================================================================


def create_app(config_name: str | None = None) -> Flask:
    """Create and configure a Flask application instance.

    This is the **Flask Application Factory** — the single entry point for
    creating fully configured application instances.  It orchestrates all
    subsystem initialization in the correct dependency order:

    1. **Logging** — Configured first so all subsequent steps can log.
    2. **Configuration** — Loaded from environment-specific config class.
    3. **Extensions** — Flask-SQLAlchemy, Flask-Migrate, flask-smorest, CORS.
    4. **API Blueprints** — All 15 REST API endpoint groups.
    5. **Format Blueprints** — 7 repository format protocol handlers.
    6. **Error Handlers** — Global JSON error responses.
    7. **Request Hooks** — Authentication, security headers, session cleanup.
    8. **Event System** — Blinker signals, subscribers, webhook dispatcher.
    9. **Database** — Table creation (dev/test), config defaults, cache warm.
    10. **Scheduler** — APScheduler background task scheduling.
    11. **Monitoring** — Health checks, Prometheus metrics.

    Args:
        config_name: The configuration environment name.  One of
            ``'development'``, ``'production'``, ``'testing'``, or
            ``'default'``.  When ``None``, the value is read from the
            ``FLASK_CONFIG`` environment variable (defaulting to
            ``'development'`` when unset).

    Returns:
        A fully configured :class:`~flask.Flask` application instance,
        ready for deployment via Gunicorn or the Flask development server.

    Raises:
        ValueError: If *config_name* does not match any known configuration.

    Example::

        # Production (Gunicorn — wsgi.py)
        app = create_app('production')

        # Development (run.py)
        app = create_app('development')

        # Testing (conftest.py)
        app = create_app('testing')

        # Auto-detect from FLASK_CONFIG env var
        app = create_app()
    """
    # ------------------------------------------------------------------ #
    # Step 0: Resolve configuration name
    # ------------------------------------------------------------------ #
    if config_name is None:
        config_name = os.getenv("FLASK_CONFIG", "development")

    # ------------------------------------------------------------------ #
    # Step 1: Configure logging (must be first for diagnostic output)
    # ------------------------------------------------------------------ #
    _configure_logging(config_name)

    logger.info(
        "=== Nexus Repository Flask Application Starting ===  "
        "(config=%s, pid=%d)",
        config_name,
        os.getpid(),
    )

    # ------------------------------------------------------------------ #
    # Step 2: Create the Flask application instance
    # ------------------------------------------------------------------ #
    app: Flask = Flask(__name__)

    # ------------------------------------------------------------------ #
    # Step 3: Load configuration
    # ------------------------------------------------------------------ #
    _load_configuration(app, config_name)

    # ------------------------------------------------------------------ #
    # Step 4: Initialize Flask extensions (replaces Guice DI)
    # ------------------------------------------------------------------ #
    _init_extensions(app)

    # ------------------------------------------------------------------ #
    # Step 5: Register REST API blueprints (replaces RESTEasy router)
    # ------------------------------------------------------------------ #
    _register_api_blueprints(app)

    # ------------------------------------------------------------------ #
    # Step 6: Register format handler blueprints (replaces OSGi bundles)
    # ------------------------------------------------------------------ #
    _register_format_blueprints(app)

    # ------------------------------------------------------------------ #
    # Step 7: Register global JSON error handlers
    # ------------------------------------------------------------------ #
    _register_error_handlers(app)

    # ------------------------------------------------------------------ #
    # Step 8: Register request lifecycle hooks (auth, headers, teardown)
    # ------------------------------------------------------------------ #
    _register_request_hooks(app)

    # ------------------------------------------------------------------ #
    # Step 9: Initialize event system (replaces Guava EventBus)
    # ------------------------------------------------------------------ #
    _init_event_system(app)

    # ------------------------------------------------------------------ #
    # Step 10: Initialize database and seed defaults
    # ------------------------------------------------------------------ #
    _init_database(app)

    # ------------------------------------------------------------------ #
    # Step 11: Initialize task scheduler (replaces Quartz 2.3.2)
    # ------------------------------------------------------------------ #
    _init_scheduler(app)

    # ------------------------------------------------------------------ #
    # Step 12: Initialize monitoring (health checks, Prometheus)
    # ------------------------------------------------------------------ #
    _init_monitoring(app)

    # ------------------------------------------------------------------ #
    # Startup Complete
    # ------------------------------------------------------------------ #
    logger.info(
        "=== Nexus Repository Flask Application Ready ===  "
        "(config=%s, routes=%d)",
        config_name,
        len(app.url_map._rules),
    )

    return app


# ---------------------------------------------------------------------------
# Module Public API
# ---------------------------------------------------------------------------

__all__: list[str] = ["create_app"]
