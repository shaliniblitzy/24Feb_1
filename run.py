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
- **Debug mode** enabled (auto-reload on code changes, interactive debugger)
- **Host** ``0.0.0.0`` (accessible from other machines on the network)
- **Port** ``5000`` (configurable via ``FLASK_RUN_PORT`` env var)

.. warning::
    Do NOT use ``python run.py`` for production deployments.  Use
    ``gunicorn -c gunicorn.conf.py wsgi:app`` instead.

See Also:
    - ``wsgi.py`` — Production entry point for Gunicorn 25.1.0
    - ``src/app/factory.py`` — Application factory (``create_app``)
    - ``gunicorn.conf.py`` — Production Gunicorn configuration
"""

from __future__ import annotations

import os

from src.app.factory import create_app

# Resolve configuration from environment (defaults to 'development')
config_name: str = os.getenv("FLASK_CONFIG", "development")

# Create the Flask application using the application factory
app = create_app(config_name)

if __name__ == "__main__":
    # Read host and port from environment with sensible defaults
    host: str = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port: int = int(os.getenv("FLASK_RUN_PORT", "5000"))
    debug: bool = config_name in ("development", "testing")

    app.run(
        host=host,
        port=port,
        debug=debug,
        use_reloader=debug,
    )
