"""
WSGI Entry Point for Production Deployment — ``wsgi.py``

This module provides the WSGI-compatible application object consumed by
Gunicorn 25.1.0 (or any WSGI server) for production deployments.  It
replaces the Java system's embedded Jetty 12.0.5 server bootstrap.

Usage::

    # Production deployment via Gunicorn
    gunicorn -c gunicorn.conf.py wsgi:app

    # Or with explicit options
    gunicorn --bind 0.0.0.0:8081 --workers 4 wsgi:app

The ``app`` module-level variable is the Flask application instance that
Gunicorn imports and serves.  Gunicorn forks multiple worker processes,
each importing this module and receiving its own copy of ``app``.

Configuration is controlled by the ``FLASK_CONFIG`` environment variable:
- ``'production'`` (recommended) — PostgreSQL, structured JSON logging,
  optimized settings.
- ``'development'`` (default fallback) — SQLite, debug logging.

See Also:
    - ``run.py`` — Development server entry point
    - ``src/app/factory.py`` — Application factory (``create_app``)
    - ``gunicorn.conf.py`` — Gunicorn production server configuration
    - ``Dockerfile`` — Container image using ``gunicorn wsgi:app``
"""

from __future__ import annotations

import os

from src.app.factory import create_app

# Resolve configuration from environment (defaults to 'production' for WSGI)
config_name: str = os.getenv("FLASK_CONFIG", "production")

# Create the Flask application using the application factory.
# This is the WSGI-compatible callable that Gunicorn imports as ``wsgi:app``.
app = create_app(config_name)
