"""
WSGI Entry Point for Production Deployment — ``wsgi.py``

This module provides the WSGI-compatible application object consumed by
Gunicorn 25.1.0 (or any WSGI server) for production deployments.  It
replaces the Java system's embedded Jetty 12.0.5 server bootstrap
(``Launcher.java``) from the original Sonatype Nexus Repository codebase.

Responsibilities:

1. **Environment Loading** — Calls :func:`dotenv.load_dotenv` to read
   ``.env`` file entries into :data:`os.environ` *before* the Flask
   application is created.  This guarantees that ``DATABASE_URL``,
   ``SECRET_KEY``, ``JWT_SECRET_KEY``, ``ELASTICSEARCH_URL``, S3
   credentials, and all other configuration values are available when
   :func:`~src.app.factory.create_app` reads them.

2. **Application Creation** — Invokes the Flask application factory
   (:func:`~src.app.factory.create_app`) with the configuration profile
   resolved from the ``FLASK_CONFIG`` environment variable (defaulting to
   ``'production'`` in a WSGI context).

3. **Module-Level Export** — Exposes the :data:`app` variable at module
   scope so that Gunicorn (and other WSGI servers) can import it as
   ``wsgi:app``.

4. **Direct Execution** — When run directly (``python wsgi.py``), starts
   the Flask development server via :meth:`Flask.run` for quick local
   testing outside of Gunicorn.

Usage::

    # Production deployment via Gunicorn (recommended)
    gunicorn -c gunicorn.conf.py wsgi:app

    # With explicit options
    gunicorn --bind 0.0.0.0:8081 --workers 4 wsgi:app

    # Direct execution for quick local testing
    python wsgi.py

Configuration is controlled by the ``FLASK_CONFIG`` environment variable:

- ``'production'`` (default for WSGI) — PostgreSQL, structured JSON
  logging, optimized settings.
- ``'development'`` — SQLite, debug logging, auto-reload.
- ``'testing'`` — In-memory SQLite, minimal logging.

See Also:
    - ``run.py`` — Development server entry point (uses 'development' config)
    - ``src/app/factory.py`` — Application factory (:func:`create_app`)
    - ``gunicorn.conf.py`` — Gunicorn production server configuration
    - ``Dockerfile`` — Container image CMD uses ``gunicorn wsgi:app``
    - ``.flaskenv`` — Flask CLI environment (``FLASK_APP=wsgi.py``)
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Step 1: Load environment variables from .env file
# ---------------------------------------------------------------------------
# Must happen BEFORE create_app() is called so that all configuration
# values (DATABASE_URL, SECRET_KEY, JWT_SECRET_KEY, ELASTICSEARCH_URL,
# S3 credentials, LOG_LEVEL, etc.) are present in os.environ when the
# Flask application factory reads them.
#
# load_dotenv() is a no-op when the .env file does not exist, making
# this safe for container deployments where env vars are injected
# directly by the orchestrator (Docker, Kubernetes, etc.).
# ---------------------------------------------------------------------------
load_dotenv()

from src.app.factory import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# Step 2: Resolve configuration profile from environment
# ---------------------------------------------------------------------------
# In a Gunicorn/WSGI context the default is 'production'.  Override via
# the FLASK_CONFIG environment variable for development or testing.
# ---------------------------------------------------------------------------
config_name: str = os.getenv("FLASK_CONFIG", "production")

# ---------------------------------------------------------------------------
# Step 3: Create the Flask application instance
# ---------------------------------------------------------------------------
# This is the WSGI-compatible callable that Gunicorn imports and serves.
# Gunicorn forks multiple worker processes; each imports this module and
# receives its own copy of the ``app`` object.
#
# The create_app() factory handles all initialization:
#   - Flask extension registration (SQLAlchemy, Migrate, JWT, CORS, etc.)
#   - Blueprint registration (15 REST API groups + 7 format handlers)
#   - Logging configuration (structured JSON in production)
#   - Error handler registration (JSON error responses)
#   - Authentication chain setup (local, API key, JWT, LDAP, SSO)
#   - Event system initialization (Blinker signals)
#   - Database table creation and cache warming
#   - Background task scheduler (APScheduler)
#   - Health checks and Prometheus metrics
# ---------------------------------------------------------------------------
app = create_app(config_name)


# ---------------------------------------------------------------------------
# Step 4: Direct execution guard
# ---------------------------------------------------------------------------
# When this file is executed directly (python wsgi.py) rather than
# imported by Gunicorn, start the Flask development server.  This is
# a convenience for quick local testing; for proper development use
# ``run.py`` or ``flask run`` instead.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run()
