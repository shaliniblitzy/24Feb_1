"""
Alembic Migration Environment Configuration
=============================================

This module replaces Flyway 8.5.13 from the original Java/Jetty source system.
It connects Alembic 1.14.1 (wrapped by Flask-Migrate 4.0.7) to the Flask/SQLAlchemy
application, enabling automatic migration generation by comparing SQLAlchemy model
definitions to the current database state.

Dual-database architecture support:
  - Standalone/Development: SQLite (zero-config, replaces H2 2.3.232)
  - Production/Clustered: PostgreSQL (replaces JDBC 42.7.2 + HikariCP 4.0.3)

Flask-Migrate CLI integration:
  flask db upgrade    — Apply pending migrations
  flask db migrate    — Auto-generate a migration script
  flask db downgrade  — Revert the last migration
  flask db current    — Show current migration revision
  flask db history    — Show migration history

All 14 DataStore entity tables (Repository, Component, Asset, User, Role, Privilege,
ContentSelector, TaskDefinition, TaskExecution, AuditEvent, BlobStoreConfig,
CleanupPolicy, SystemConfig, plus RoleAssignment junction table) are discovered
automatically via the SQLAlchemy metadata registered in src/app/models/.
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from flask import current_app
from sqlalchemy import engine_from_config, pool  # noqa: F401 — standard Alembic template imports

# ---------------------------------------------------------------------------
# Alembic Config Object
# ---------------------------------------------------------------------------
# This is the Alembic Config object, which provides access to the values
# within the alembic.ini file in use. It is used by env.py to read
# configuration values (e.g., sqlalchemy.url, script_location).
config = context.config

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
# Interpret the config file for Python logging. This line sets up loggers
# defined in alembic.ini (root, sqlalchemy, alembic, flask_migrate).
# Replaces SLF4J 1.7.36 + Logback 1.2.13 migration logging from Java source.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Module-level logger for migration progress messages and empty migration
# suppression notifications during autogenerate operations.
logger = logging.getLogger("alembic.env")

# ---------------------------------------------------------------------------
# Target Metadata
# ---------------------------------------------------------------------------
# CRITICAL: This is the connection between Alembic and the Flask/SQLAlchemy
# application. The metadata object contains all table definitions from the
# models package (all 14 entity models). When Flask-Migrate's init_app(app, db)
# is called in factory.py, db.metadata is populated with all registered
# SQLAlchemy model definitions.
#
# This replaces the Flyway schema scanning that discovered Java entity
# definitions via MyBatis 3.5.15 mappers in the original source system.
target_metadata = current_app.extensions["migrate"].db.metadata


def get_engine():
    """Get the SQLAlchemy engine from the Flask application context.

    Retrieves the database engine configured via Flask-SQLAlchemy, supporting
    both the modern ``db.get_engine()`` API (Flask-SQLAlchemy >= 3.0) and
    the legacy ``db.engine`` property for backward compatibility.

    The engine abstracts the underlying database dialect, supporting both:
      - SQLite for standalone/development deployments
      - PostgreSQL for production/clustered deployments

    Returns:
        sqlalchemy.engine.Engine: The configured SQLAlchemy engine instance
            bound to either SQLite or PostgreSQL depending on the active
            Flask application configuration (SQLALCHEMY_DATABASE_URI).

    Raises:
        RuntimeError: If called outside of a Flask application context
            (no active app pushed onto the context stack).
    """
    try:
        # Flask-SQLAlchemy >= 3.0 API
        return current_app.extensions["migrate"].db.get_engine()
    except (TypeError, AttributeError):
        # Fallback for older Flask-SQLAlchemy versions or edge cases
        # where get_engine() is not available or requires different args
        return current_app.extensions["migrate"].db.engine


def get_engine_url() -> str:
    """Get the database URL as a string suitable for Alembic configuration.

    Retrieves the database URL from the SQLAlchemy engine and formats it
    for use in Alembic's configuration system. The ``%`` character is escaped
    to ``%%`` because Alembic's underlying ConfigParser uses ``%`` for
    variable interpolation (Python's ``configparser`` module).

    This function supports both SQLite and PostgreSQL URL formats:
      - SQLite:      ``sqlite:///nexus.db``
      - PostgreSQL:  ``postgresql://user:pass@host:5432/nexus``

    Returns:
        str: The database URL string with percent signs properly escaped
            for Alembic's ConfigParser interpolation.

    Raises:
        RuntimeError: If called outside of a Flask application context.
    """
    try:
        # Modern SQLAlchemy URL rendering with explicit password display
        # hide_password=False is needed so Alembic can use the full URL
        return get_engine().url.render_as_string(hide_password=False).replace(
            "%", "%%"
        )
    except AttributeError:
        # Fallback: convert URL object to string directly
        return str(get_engine().url).replace("%", "%%")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well. By skipping the Engine
    creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    Offline mode is useful for:
      - Generating SQL migration scripts for review before execution
      - Creating migration scripts for environments where direct DB
        access is not available (e.g., DBA-managed production databases)
      - Producing audit-ready SQL change logs

    The ``literal_binds=True`` option renders bound parameters inline
    in the generated SQL output, making the scripts self-contained
    and directly executable.

    The ``dialect_opts`` with ``paramstyle='named'`` ensures consistent
    parameter formatting across SQLite and PostgreSQL dialects.
    """
    url = get_engine_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a
    connection with the context. This is the standard execution mode
    used by ``flask db upgrade`` and ``flask db downgrade``.

    Key features of online mode:

    1. **Empty migration prevention**: The ``process_revision_directives``
       callback inspects autogenerated migration scripts and suppresses
       empty migrations (no schema changes detected). This prevents
       cluttering the versions directory with no-op migration files.

    2. **SQLite batch mode** (``render_as_batch=True``): CRITICAL for the
       dual-database architecture. SQLite does not support most ALTER TABLE
       operations (column rename, type change, constraint modification).
       Batch mode works around this limitation by:
         - Creating a new table with the desired schema
         - Copying data from the old table
         - Dropping the old table
         - Renaming the new table
       PostgreSQL ignores batch mode and uses native ALTER TABLE.

    3. **Flask-Migrate configure_args**: Additional configuration options
       passed through from Flask-Migrate's ``init_app()`` call, allowing
       application-level customization of the migration context.
    """

    def process_revision_directives(context, revision, directives):
        """Prevent generation of empty migration scripts.

        When running ``flask db migrate`` (autogenerate mode), this callback
        inspects the generated upgrade operations. If no schema changes are
        detected (the upgrade_ops tree is empty), the migration script
        directives are cleared, preventing an empty file from being written
        to the versions directory.

        Args:
            context: The Alembic MigrationContext.
            revision: Tuple of revision identifiers.
            directives: List of MigrationScript directives to be processed.
        """
        if getattr(config.cmd_opts, "autogenerate", False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info("No changes in schema detected.")

    # Get the database engine from Flask app context
    connectable = get_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            process_revision_directives=process_revision_directives,
            # CRITICAL: render_as_batch=True enables SQLite ALTER TABLE support
            # via Alembic's batch migration operations. This is MANDATORY for
            # the dual-database architecture (SQLite standalone + PostgreSQL
            # clustered). PostgreSQL ignores batch mode and uses native DDL.
            render_as_batch=True,
            # Pass through any additional configuration from Flask-Migrate
            # This allows application-level customization via init_app()
            **current_app.extensions["migrate"].configure_args,
        )

        with context.begin_transaction():
            context.run_migrations()


# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------
# Determine the execution mode and run migrations accordingly.
# Alembic sets offline mode when invoked with the --sql flag, which
# generates SQL scripts instead of executing against a live database.
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
