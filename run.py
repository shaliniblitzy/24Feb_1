"""
Development Server Entry Point — ``run.py``

This module provides the entry point for running the Nexus Repository Flask
application in **development mode** using the built-in Werkzeug development
server.  It replaces the Java system's ``Launcher.java`` bootstrap sequence
for local development workflows.

Usage::

    # Activate the virtual environment and start the dev server
    source venv/bin/activate
    python run.py

    # Or use Flask CLI (reads .flaskenv for FLASK_APP and FLASK_ENV)
    flask run

The development server runs with:

- **Debug mode** enabled by default (auto-reload on code changes, interactive
  debugger).  Override via ``FLASK_DEBUG=False``.
- **Host** ``0.0.0.0`` (accessible from other machines on the network for
  Docker development).  Override via ``FLASK_HOST``.
- **Port** ``5000`` (configurable via ``FLASK_PORT``).

Environment Variables:

- ``FLASK_CONFIG`` — Configuration profile name.  Defaults to
  ``'development'`` which selects SQLite and debug mode.  Valid values:
  ``'development'``, ``'testing'``, ``'production'``.
- ``FLASK_HOST`` — Bind address for the development server.  Defaults to
  ``'0.0.0.0'``.
- ``FLASK_PORT`` — Listen port for the development server.  Defaults to
  ``5000``.
- ``FLASK_DEBUG`` — Enable or disable debug mode.  Defaults to ``'True'``
  in development.

Key Differences from ``wsgi.py``:

- This file defaults to ``'development'`` config (SQLite, debug mode).
- ``wsgi.py`` defaults to ``'production'`` config (PostgreSQL, no debug).
- Both use the same :func:`~src.app.factory.create_app` factory function.
- ``run.py`` is invoked via ``python run.py`` for local development.
- ``wsgi.py`` is invoked via ``gunicorn -c gunicorn.conf.py wsgi:app`` for
  production deployment.

.. warning::
    Do NOT use ``python run.py`` for production deployments.  Use
    ``gunicorn -c gunicorn.conf.py wsgi:app`` instead.

See Also:
    - ``wsgi.py`` — Production entry point for Gunicorn 25.1.0
    - ``src/app/factory.py`` — Application factory (:func:`create_app`)
    - ``gunicorn.conf.py`` — Production Gunicorn configuration
    - ``config/default.py`` — Base configuration defaults
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load environment variables from .env file BEFORE application creation.
#
# python-dotenv reads the .env file in the project root and injects its
# key-value pairs into ``os.environ``.  This ensures that configuration
# values such as DATABASE_URL, SECRET_KEY, ELASTICSEARCH_URL, and S3
# credentials are available when the Flask application factory reads them
# during initialization.
#
# The call is idempotent — if the .env file does not exist, this is a no-op.
# Existing environment variables are NOT overridden (``override=False`` is
# the default behaviour of load_dotenv).
# ---------------------------------------------------------------------------
load_dotenv()

from src.app.factory import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# Application Instance Creation
# ---------------------------------------------------------------------------
# Resolve the configuration profile from the FLASK_CONFIG environment
# variable.  Defaults to 'development' which selects:
#   - SQLite database (zero-config, replaces H2 from Java source)
#   - Debug mode enabled
#   - Verbose logging at DEBUG level
#
# The ``app`` object is exported at module level so that it can be imported
# by test fixtures, Flask CLI commands, and ``flask run`` (which looks for
# an ``app`` object in the module specified by FLASK_APP).
# ---------------------------------------------------------------------------

config_name: str = os.getenv("FLASK_CONFIG", "development")

app = create_app(config_name)


if __name__ == "__main__":
    # ------------------------------------------------------------------
    # Main Entry Point — Development Server
    # ------------------------------------------------------------------
    # This block executes only when run directly via ``python run.py``.
    # It reads additional environment variables to configure the Werkzeug
    # development server and starts it.
    #
    # In development mode the server features:
    #   - Auto-reloader: restarts on source file changes
    #   - Interactive debugger: Werkzeug debugger on unhandled exceptions
    #   - Threaded mode: handles concurrent requests for testing
    # ------------------------------------------------------------------

    # Bind address — default 0.0.0.0 allows external connections which
    # is essential for Docker-based development workflows where the
    # container needs to be accessible from the host machine.
    host: str = os.getenv("FLASK_HOST", "0.0.0.0")

    # Listen port — default 5000 is the Flask convention.  Ensure this
    # does not conflict with other common development services.
    try:
        port: int = int(os.getenv("FLASK_PORT", "5000"))
    except (ValueError, TypeError):
        print(
            "ERROR: FLASK_PORT must be a valid integer. "
            f"Got: {os.getenv('FLASK_PORT')!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate the port is within the valid range (1–65535)
    if not (1 <= port <= 65535):
        print(
            f"ERROR: FLASK_PORT must be between 1 and 65535. Got: {port}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Debug flag — defaults to True for development.  When debug is
    # enabled, the Werkzeug auto-reloader and interactive debugger are
    # activated.  In production, debug MUST be False.
    debug: bool = os.getenv("FLASK_DEBUG", "True").lower() == "true"

    # Log startup information for developer convenience
    print(
        f" * Nexus Repository Manager (Development Server)\n"
        f" * Configuration: {config_name}\n"
        f" * Debug mode: {'on' if debug else 'off'}\n"
        f" * Running on http://{host}:{port}/ (Press CTRL+C to quit)"
    )

    app.run(
        host=host,
        port=port,
        debug=debug,
        use_reloader=debug,
    )
