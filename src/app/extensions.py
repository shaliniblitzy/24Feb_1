"""
Flask Extension Initialization Module.

This module instantiates all Flask extensions as module-level singletons that
are later bound to the Flask application in ``factory.py`` via ``init_app()``.

**Architecture Context:**
This module replaces ``Guice 7.0.0`` DI module wiring from the original Java
source system (Sonatype Nexus Repository). In the Java architecture, Guice
modules declared bindings between interfaces and implementations; here,
Python's module-level singleton pattern provides equivalent functionality.

**Design Pattern:**
The standard Flask "extension factory" pattern is used:
    1. Extension instances are created here at module level *without* an app.
    2. ``factory.py`` calls ``ext.init_app(app)`` during application creation.
    3. Any module in the codebase can safely import extensions from here
       without triggering circular dependencies.

**Extension Mapping (Java → Python):**

+-------------------------------+--------------------------------------+
| Java Component                | Python Extension                     |
+===============================+======================================+
| MyBatis 3.5.15 + HikariCP    | Flask-SQLAlchemy (``db``)            |
+-------------------------------+--------------------------------------+
| Flyway 8.5.13                 | Flask-Migrate (``migrate``)          |
+-------------------------------+--------------------------------------+
| RESTEasy 6.2.7 + Swagger     | flask-smorest (``api``)              |
+-------------------------------+--------------------------------------+
| Apache Shiro 2.0.0 AuthN     | Flask-HTTPAuth (``basic_auth``,      |
|                               | ``token_auth``, ``multi_auth``)      |
+-------------------------------+--------------------------------------+
| (CORS config)                 | Flask-CORS (``cors``)                |
+-------------------------------+--------------------------------------+
| Quartz 2.3.2                  | APScheduler (``scheduler``)          |
+-------------------------------+--------------------------------------+
| Guava EventBus                | Blinker (``event_signals``)          |
+-------------------------------+--------------------------------------+
| Dropwizard Metrics 4.2.25    | prometheus-client                    |
| + Prometheus Client 0.16.0   | (``metrics_registry``)               |
+-------------------------------+--------------------------------------+

Exported Instances:
    db             : SQLAlchemy ORM — database access and model base class
    migrate        : Alembic migration wrapper — schema versioning
    api            : REST API framework — Blueprint registration and OpenAPI docs
    basic_auth     : HTTP Basic authentication handler
    token_auth     : HTTP Bearer token authentication handler
    multi_auth     : Combined auth with first-successful strategy
    cors           : Cross-Origin Resource Sharing support
    scheduler      : Background task scheduler
    event_signals  : Signal namespace for decoupled event dispatch
    metrics_registry : Prometheus metrics collector registry
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from blinker import Namespace
from flask_cors import CORS
from flask_httpauth import HTTPBasicAuth, HTTPTokenAuth, MultiAuth
from flask_migrate import Migrate
from flask_smorest import Api
from flask_sqlalchemy import SQLAlchemy
from prometheus_client import CollectorRegistry

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for extension initialization diagnostics.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Database Extensions
# ---------------------------------------------------------------------------
# Replaces MyBatis 3.5.15 (ORM) and HikariCP 4.0.3 (connection pool) from the
# Java source.  Flask-SQLAlchemy wraps SQLAlchemy 2.0.36 with Flask app-context
# awareness.  Built-in connection pooling supports both SQLite (standalone /
# zero-config deployments) and PostgreSQL (clustered / enterprise deployments)
# selected via the ``DATABASE_URL`` / ``SQLALCHEMY_DATABASE_URI`` config key.
#
# Key members used by downstream modules:
#   db.init_app(app)   — bind to a Flask application
#   db.Model           — declarative base class for all SQLAlchemy models
#   db.session          — scoped session proxy
#   db.create_all()    — create all tables (dev/testing convenience)
#   db.drop_all()      — drop all tables (testing teardown)
#   db.engine           — underlying SQLAlchemy Engine
#   db.Column           — column type shorthand
#   db.relationship()  — ORM relationship helper
# ---------------------------------------------------------------------------

db: SQLAlchemy = SQLAlchemy()

logger.debug("SQLAlchemy extension instantiated (pending init_app).")

# ---------------------------------------------------------------------------
# 2. Database Migration Extension
# ---------------------------------------------------------------------------
# Replaces Flyway 8.5.13 schema migration tool from the Java source.
# Flask-Migrate 4.0.7 wraps Alembic 1.14.1, providing CLI commands
# (``flask db init``, ``flask db migrate``, ``flask db upgrade``) for
# versioned schema management across SQLite and PostgreSQL deployments.
#
# Key members used by downstream modules:
#   migrate.init_app(app, db)  — bind to a Flask application and db instance
# ---------------------------------------------------------------------------

migrate: Migrate = Migrate()

logger.debug("Flask-Migrate extension instantiated (pending init_app).")

# ---------------------------------------------------------------------------
# 3. REST API / OpenAPI Extension
# ---------------------------------------------------------------------------
# Replaces RESTEasy 6.2.7 (JAX-RS) and Swagger/OpenAPI 2.2.20 from the Java
# source.  flask-smorest 0.45.0 provides:
#   • Blueprint-based API endpoint registration
#   • Automatic OpenAPI 3.x specification generation
#   • Marshmallow 3.23.2 schema integration for request/response serialization
#   • Interactive Swagger UI at ``/api/docs`` (configurable)
#
# Key members used by downstream modules:
#   api.init_app(app)            — bind to a Flask application
#   api.register_blueprint(bp)   — register an API Blueprint with docs
# ---------------------------------------------------------------------------

api: Api = Api()

logger.debug("flask-smorest Api extension instantiated (pending init_app).")

# ---------------------------------------------------------------------------
# 4. Authentication Extensions
# ---------------------------------------------------------------------------
# Replaces Apache Shiro 2.0.0 authentication framework from the Java source.
#
# basic_auth  — Handles HTTP Basic Authentication (username / password).
#               Equivalent to AuthenticatingRealmImpl (local credential realm).
#
# token_auth  — Handles HTTP Bearer Token Authentication.
#               Equivalent to BearerTokenRealm (API key authentication) and
#               JwtSecurityFilter (JWT token validation).
#               The ``scheme='Bearer'`` parameter instructs Flask-HTTPAuth to
#               look for ``Authorization: Bearer <token>`` headers.
#
# multi_auth  — Combines ``basic_auth`` and ``token_auth`` with a
#               first-successful strategy.  Equivalent to
#               FirstSuccessfulModularRealmAuthenticator from Shiro 2.0.0:
#               authentication succeeds as soon as *any* chained realm
#               successfully validates the supplied credentials.
#
# Key members used by downstream modules:
#   basic_auth.login_required   — decorator to protect routes with Basic auth
#   basic_auth.verify_password  — decorator to register password verifier
#   basic_auth.error_handler    — decorator to register custom error response
#   token_auth.login_required   — decorator to protect routes with Bearer auth
#   token_auth.verify_token     — decorator to register token verifier
#   token_auth.error_handler    — decorator to register custom error response
#   multi_auth.login_required   — decorator combining both auth schemes
# ---------------------------------------------------------------------------

basic_auth: HTTPBasicAuth = HTTPBasicAuth()
token_auth: HTTPTokenAuth = HTTPTokenAuth(scheme="Bearer")
multi_auth: MultiAuth = MultiAuth(basic_auth, token_auth)

logger.debug(
    "Flask-HTTPAuth extensions instantiated: "
    "basic_auth (Basic), token_auth (Bearer), multi_auth (combined)."
)

# ---------------------------------------------------------------------------
# 5. CORS Extension
# ---------------------------------------------------------------------------
# Provides configurable Cross-Origin Resource Sharing (CORS) headers so that
# frontend applications served from different origins can interact with the
# REST API.  Configuration is driven by Flask app config keys such as
# ``CORS_ORIGINS``, ``CORS_METHODS``, ``CORS_HEADERS``, etc.
#
# Key members used by downstream modules:
#   cors.init_app(app)  — bind to a Flask application with CORS settings
# ---------------------------------------------------------------------------

cors: CORS = CORS()

logger.debug("Flask-CORS extension instantiated (pending init_app).")

# ---------------------------------------------------------------------------
# 6. Scheduling Extension
# ---------------------------------------------------------------------------
# Replaces Quartz 2.3.2 from the Java source.
# APScheduler 3.10.4 ``BackgroundScheduler`` runs in a daemon thread alongside
# the WSGI process.  It supports:
#   • One-time jobs (``date`` trigger)
#   • Recurring jobs (``interval`` trigger)
#   • Cron-based jobs (``cron`` trigger)
#   • Cluster-aware execution via ``SQLAlchemyJobStore`` for multi-node
#     deployments (configured at ``init_app`` time).
#
# The scheduler is created in an *idle* (not-running) state.  ``start()`` must
# be called explicitly after the Flask app is fully initialised — typically in
# ``factory.py`` or a ``before_first_request``-equivalent hook.
#
# Key members used by downstream modules:
#   scheduler.add_job()    — register a new job
#   scheduler.remove_job() — cancel a job by ID
#   scheduler.start()      — begin executing scheduled jobs
#   scheduler.shutdown()   — gracefully stop the scheduler
#   scheduler.get_jobs()   — list all registered jobs
# ---------------------------------------------------------------------------

scheduler: BackgroundScheduler = BackgroundScheduler(
    daemon=True,
    job_defaults={
        "coalesce": True,         # Collapse missed runs into a single run
        "max_instances": 1,       # Only one concurrent instance per job
        "misfire_grace_time": 60, # 60-second grace period for misfired jobs
    },
)

logger.debug("APScheduler BackgroundScheduler instantiated (idle, pending start).")

# ---------------------------------------------------------------------------
# 7. Event System (Signal Namespace)
# ---------------------------------------------------------------------------
# Replaces Guava EventBus from the Java source.
# Blinker 1.9.0 ``Namespace`` provides a scoped signal registry for decoupled
# event dispatch.  Named signals are created on demand via
# ``event_signals.signal('name')``.
#
# Used for:
#   • Audit logging (Feature F-303)
#   • Webhook dispatch (Feature F-503)
#   • Configuration propagation (Feature F-404)
#   • Cleanup triggers (Feature F-204)
#
# Key members used by downstream modules:
#   event_signals.signal(name)  — obtain or create a named signal
# ---------------------------------------------------------------------------

event_signals: Namespace = Namespace()

logger.debug("Blinker event signal Namespace instantiated.")

# ---------------------------------------------------------------------------
# 8. Metrics Extension
# ---------------------------------------------------------------------------
# Replaces Dropwizard Metrics 4.2.25 + Prometheus Client 0.16.0 from the Java
# source.  A dedicated ``CollectorRegistry`` is used (instead of the default
# global registry) so that:
#   • Application metrics are isolated from Python process metrics.
#   • Multiprocess mode (Gunicorn pre-fork workers) can be supported by
#     configuring ``prometheus_multiproc_dir`` at runtime.
#   • The ``/metrics`` endpoint (Feature F-401) exposes only application-
#     relevant metrics to Prometheus scrapers.
#
# Key members used by downstream modules:
#   metrics_registry.register(collector)     — register a metric collector
#   metrics_registry.unregister(collector)   — remove a metric collector
# ---------------------------------------------------------------------------

metrics_registry: CollectorRegistry = CollectorRegistry(auto_describe=True)

logger.debug("Prometheus CollectorRegistry instantiated.")

# ---------------------------------------------------------------------------
# Module-Level Export List
# ---------------------------------------------------------------------------
# Explicitly declares the public API of this module.  All listed names are
# intended for import by ``factory.py``, API blueprints, service classes,
# authentication modules, and any other component that needs access to the
# shared extension singletons.
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "db",
    "migrate",
    "api",
    "basic_auth",
    "token_auth",
    "multi_auth",
    "cors",
    "scheduler",
    "event_signals",
    "metrics_registry",
]

logger.debug(
    "All %d Flask extensions initialised at module level: %s",
    len(__all__),
    ", ".join(__all__),
)
