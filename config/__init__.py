"""
Configuration package for the Nexus Repository Flask application.

Provides environment-specific configuration classes:
- ``default.py``:     Base configuration with all defaults
- ``development.py``: Development settings (SQLite, debug mode)
- ``production.py``:  Production settings (PostgreSQL, Gunicorn)
- ``testing.py``:     Test settings (in-memory SQLite)

Replaces the System Configuration (F-404) component from the Java source.
"""
