"""
Flask Extension Initialization — Central Extension Registry.

This module instantiates all Flask extensions WITHOUT binding them to a
Flask application instance.  Each extension's ``init_app(app)`` method is
called later inside the ``create_app()`` application factory defined in
``src/app.py``.  This pattern enables:

* Multiple Flask app instances for testing with different configurations.
* Clean dependency injection without circular imports (this module must
  NEVER import any other ``src`` package modules).
* A single source of truth for every shared extension object.

**Java equivalency mapping:**

+-------------------------------+-------------------------------------------+
| Original Java Component       | Python / Flask Replacement                |
+===============================+===========================================+
| MyBatis 3.5.15 + HikariCP    | Flask-SQLAlchemy (``db``)                 |
+-------------------------------+-------------------------------------------+
| Flyway 8.5.13                 | Flask-Migrate / Alembic (``migrate``)     |
+-------------------------------+-------------------------------------------+
| Apache Shiro 2.0.0 sessions  | Flask-Login (``login_manager``)           |
+-------------------------------+-------------------------------------------+
| Jetty 12.0.5 CORS filter     | Flask-CORS (``cors``)                     |
+-------------------------------+-------------------------------------------+
| Quartz 2.3.2 scheduler       | APScheduler (``scheduler``)               |
+-------------------------------+-------------------------------------------+
| Guice 7.0.0 DI bindings      | This module + Flask app context / ``g``   |
+-------------------------------+-------------------------------------------+

Usage from other modules::

    from src.extensions import db, migrate, login_manager, cors, scheduler
    from src.extensions import init_scheduler
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# ---------------------------------------------------------------------------
# External extension imports (third-party packages only — no src.* imports)
# ---------------------------------------------------------------------------

# Database ORM — replaces MyBatis 3.5.15 + HikariCP 4.0.3 connection pooling
from flask_sqlalchemy import SQLAlchemy

# Database schema migration management — replaces Flyway 8.5.13
from flask_migrate import Migrate

# User session management and authentication state — replaces Apache Shiro 2.0.0 session handling
from flask_login import LoginManager

# Cross-Origin Resource Sharing — replaces Jetty 12.0.5 CORS filter
from flask_cors import CORS

# Background task scheduling — replaces Quartz 2.3.2 cluster-aware scheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

if TYPE_CHECKING:  # pragma: no cover
    from flask import Flask

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ===========================================================================
# Extension Instances
# ===========================================================================
# Each extension is instantiated here WITHOUT an app reference.  The app is
# bound later via ``init_app(app)`` inside ``create_app()`` in src/app.py.
# ===========================================================================

# Database ORM — replaces MyBatis 3.5.15 + HikariCP 4.0.3
# Exposes: init_app(), session, Model, engine, create_all(), drop_all(),
#          Column, Integer, String, Text, DateTime, Boolean, ForeignKey,
#          JSON, relationship, backref
db: SQLAlchemy = SQLAlchemy()

# Database migration management — replaces Flyway 8.5.13
# Exposes: init_app()
migrate: Migrate = Migrate()

# Authentication session management — replaces Apache Shiro 2.0.0
# Exposes: init_app(), login_view, login_message, session_protection,
#          user_loader(), request_loader()
login_manager: LoginManager = LoginManager()

# Cross-Origin Resource Sharing — replaces Jetty CORS filter
# Exposes: init_app()
cors: CORS = CORS()

# ---------------------------------------------------------------------------
# Login Manager — API-first default configuration
# ---------------------------------------------------------------------------
# login_view = None  → No redirect on 401; callers receive a JSON 401 response.
# login_message = None → No flash message (irrelevant for API-first design).
# session_protection = 'strong' → Regenerate session on IP/UA change to mitigate
#   session fixation attacks.
# NOTE: @login_manager.user_loader and @login_manager.request_loader callbacks
# are registered in src/app.py to avoid circular imports with the User model.
login_manager.login_view = None
login_manager.login_message = None
login_manager.session_protection = "strong"

# ---------------------------------------------------------------------------
# Task Scheduler — replaces Quartz 2.3.2
# ---------------------------------------------------------------------------
# The scheduler is initialised lazily via ``init_scheduler(app)`` during the
# SERVICES phase of the six-phase startup sequence (see src/startup.py).
# Until ``init_scheduler`` is called, this variable is ``None``.
# After initialisation it becomes a fully configured ``BackgroundScheduler``
# instance exposing: add_job(), remove_job(), get_jobs(), start(), shutdown(),
# get_job().
# ---------------------------------------------------------------------------
scheduler: BackgroundScheduler | None = None


# ===========================================================================
# Scheduler Initialisation Helper
# ===========================================================================

def init_scheduler(app: "Flask") -> BackgroundScheduler:
    """Create and configure the APScheduler :class:`BackgroundScheduler`.

    This function:

    1. Reads scheduler-related settings from the Flask ``app.config``.
    2. Creates a :class:`SQLAlchemyJobStore` using the application's
       ``SQLALCHEMY_DATABASE_URI`` so that scheduled jobs survive restarts.
    3. Configures a thread-pool executor whose size is controlled by
       ``SCHEDULER_MAX_WORKERS`` (default ``10``).
    4. Sets a default misfire grace time via ``SCHEDULER_MISFIRE_GRACE_TIME``
       (default ``60`` seconds).
    5. Stores the new scheduler instance in the module-level ``scheduler``
       variable so that other modules can import it directly.

    .. important::

       This function does **not** start the scheduler.  Starting is handled
       by ``src/startup.py`` during the SERVICES startup phase so that the
       database, BlobStores, and security subsystems are fully ready first.

    Parameters
    ----------
    app:
        The Flask application instance whose ``config`` supplies the
        database URI and scheduler tuning knobs.

    Returns
    -------
    BackgroundScheduler
        The fully-configured (but **not** started) scheduler.

    Raises
    ------
    RuntimeError
        If ``SQLALCHEMY_DATABASE_URI`` is not set in ``app.config``.
    """
    global scheduler  # noqa: PLW0603 — intentional module-level mutation

    database_uri: str | None = app.config.get("SQLALCHEMY_DATABASE_URI")
    if not database_uri:
        raise RuntimeError(
            "Cannot initialise scheduler: SQLALCHEMY_DATABASE_URI is not "
            "configured.  Ensure the Flask app config includes a valid "
            "database URI before calling init_scheduler()."
        )

    # ---- Job stores ---------------------------------------------------------
    # Persistent job store backed by the same database as the application.
    # This ensures scheduled jobs are preserved across application restarts,
    # replicating Quartz 2.3.2's JDBC job store behaviour.
    jobstores = {
        "default": SQLAlchemyJobStore(url=database_uri),
    }

    # ---- Executors ----------------------------------------------------------
    # Thread-pool executor size is tunable via app config.  The default of 10
    # matches Quartz's default thread count.
    max_workers: int = int(app.config.get("SCHEDULER_MAX_WORKERS", 10))
    executors = {
        "default": {
            "type": "threadpool",
            "max_workers": max_workers,
        },
    }

    # ---- Job defaults -------------------------------------------------------
    # misfire_grace_time: Number of seconds after the designated run time that
    # the job is still allowed to fire.  Prevents immediate-fire storms after
    # a brief application pause or restart.
    misfire_grace_time: int = int(
        app.config.get("SCHEDULER_MISFIRE_GRACE_TIME", 60)
    )
    coalesce: bool = bool(app.config.get("SCHEDULER_COALESCE", True))
    max_instances: int = int(app.config.get("SCHEDULER_MAX_INSTANCES", 3))

    job_defaults = {
        "coalesce": coalesce,
        "max_instances": max_instances,
        "misfire_grace_time": misfire_grace_time,
    }

    # ---- Build the scheduler ------------------------------------------------
    scheduler = BackgroundScheduler(
        jobstores=jobstores,
        executors=executors,
        job_defaults=job_defaults,
    )

    logger.info(
        "Scheduler initialised: job_store=SQLAlchemy, workers=%d, "
        "misfire_grace=%ds, coalesce=%s, max_instances=%d",
        max_workers,
        misfire_grace_time,
        coalesce,
        max_instances,
    )

    return scheduler
