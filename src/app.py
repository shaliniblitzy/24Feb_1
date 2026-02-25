"""
Flask Application Factory for the Binary Repository Management System.

Implements the application factory pattern recommended by Flask documentation,
enabling multiple application instances with different configurations for
testing, development, and production environments.

The factory function ``create_app()`` is the single entry point for creating
Flask application instances, supporting:

- Dictionary-based configuration overrides (primary for testing)
- Object-based configuration loading (for environment-specific config classes)
- Environment variable fallbacks for production deployments
- Automatic extension initialization (SQLAlchemy, JWT, Migrate)
- Blueprint registration for modular route handling
- Application-wide error handler registration

Usage::

    from src.app import create_app

    # With dictionary config (testing)
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})

    # With defaults (development)
    app = create_app()
"""

import os
import logging
from typing import Any, Dict, Optional, Union

from flask import Flask, jsonify

from src.extensions import db, jwt, migrate


def create_app(
    config_override: Optional[Union[Dict[str, Any], object]] = None,
) -> Flask:
    """Create and configure the Flask application instance.

    This factory function creates a new Flask application, applies
    configuration (defaults + overrides), initializes extensions,
    registers blueprints, and sets up error handlers.

    Parameters
    ----------
    config_override : dict or object, optional
        Configuration overrides applied after defaults.
        - If a ``dict``, applied via ``app.config.from_mapping()``.
        - If an object, applied via ``app.config.from_object()``.
        - If ``None``, only default configuration is used.

    Returns
    -------
    Flask
        A fully configured Flask application instance ready to serve
        requests or be used with a test client.
    """
    app = Flask(__name__)

    # -----------------------------------------------------------------------
    # Phase 1: Apply default configuration from environment variables
    # -----------------------------------------------------------------------
    app.config.from_mapping(
        # Core Flask settings
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production"),
        DEBUG=os.environ.get("FLASK_DEBUG", "false").lower() in ("true", "1", "yes"),
        # SQLAlchemy / database settings
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", "sqlite:///:memory:"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ECHO=False,
        # JWT authentication settings
        JWT_SECRET_KEY=os.environ.get(
            "JWT_SECRET_KEY", "dev-jwt-secret-key-change-in-production"
        ),
        # CSRF protection (enabled by default, disabled in test config)
        WTF_CSRF_ENABLED=True,
        # Storage defaults
        STORAGE_TYPE=os.environ.get("STORAGE_TYPE", "file"),
        STORAGE_PATH=os.environ.get("STORAGE_PATH", "./data/blobstore"),
        # Request limits — 500 MB default
        MAX_CONTENT_LENGTH=500 * 1024 * 1024,
        # Logging
        LOG_LEVEL=os.environ.get("LOG_LEVEL", "INFO"),
    )

    # -----------------------------------------------------------------------
    # Phase 2: Apply configuration overrides
    # -----------------------------------------------------------------------
    if config_override is not None:
        if isinstance(config_override, dict):
            app.config.from_mapping(config_override)
        else:
            app.config.from_object(config_override)

    # -----------------------------------------------------------------------
    # Phase 3: Configure logging
    # -----------------------------------------------------------------------
    log_level = getattr(logging, app.config.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=log_level)
    app.logger.setLevel(log_level)

    # -----------------------------------------------------------------------
    # Phase 4: Initialize Flask extensions
    # -----------------------------------------------------------------------
    db.init_app(app)
    jwt.init_app(app)
    migrate.init_app(app, db)

    # -----------------------------------------------------------------------
    # Phase 5: Register error handlers
    # -----------------------------------------------------------------------
    _register_error_handlers(app)

    # -----------------------------------------------------------------------
    # Phase 6: Register blueprints (when available)
    # -----------------------------------------------------------------------
    _register_blueprints(app)

    return app


def _register_error_handlers(app: Flask) -> None:
    """Register application-wide HTTP error handlers.

    Each handler returns a JSON response with a consistent error format:
    ``{"error": "<Error Name>", "message": "<Description>"}``.

    Parameters
    ----------
    app : Flask
        The Flask application instance to register handlers on.
    """

    @app.errorhandler(400)
    def bad_request(error):
        return jsonify(error="Bad Request", message=str(error.description)), 400

    @app.errorhandler(401)
    def unauthorized(error):
        return jsonify(error="Unauthorized", message=str(error.description)), 401

    @app.errorhandler(403)
    def forbidden(error):
        return jsonify(error="Forbidden", message=str(error.description)), 403

    @app.errorhandler(404)
    def not_found(error):
        return jsonify(error="Not Found", message=str(error.description)), 404

    @app.errorhandler(405)
    def method_not_allowed(error):
        return jsonify(error="Method Not Allowed", message=str(error.description)), 405

    @app.errorhandler(409)
    def conflict(error):
        return jsonify(error="Conflict", message=str(error.description)), 409

    @app.errorhandler(422)
    def unprocessable_entity(error):
        return jsonify(error="Unprocessable Entity", message=str(error.description)), 422

    @app.errorhandler(500)
    def internal_server_error(error):
        return (
            jsonify(error="Internal Server Error", message="An unexpected error occurred"),
            500,
        )


def _register_blueprints(app: Flask) -> None:
    """Register route blueprints with the Flask application.

    Blueprints are imported and registered here. If a blueprint module
    is not yet available (during incremental development), the import
    error is caught and logged without crashing the application.

    Parameters
    ----------
    app : Flask
        The Flask application instance to register blueprints on.
    """
    # Blueprint registration is done dynamically to support incremental
    # development. As API route modules are created, they are imported
    # and registered here.
    blueprint_configs = [
        ("src.api.repository_routes", "repository_bp", "/api/v1/repositories"),
        ("src.api.asset_routes", "asset_bp", "/api/v1/assets"),
        ("src.api.auth_routes", "auth_bp", "/api/v1/auth"),
        ("src.api.user_routes", "user_bp", "/api/v1/users"),
        ("src.api.search_routes", "search_bp", "/api/v1/search"),
        ("src.api.admin_routes", "admin_bp", "/api/v1/admin"),
        ("src.api.task_routes", "task_bp", "/api/v1/tasks"),
        ("src.api.blobstore_routes", "blobstore_bp", "/api/v1/blobstores"),
    ]

    for module_path, bp_name, url_prefix in blueprint_configs:
        try:
            module = __import__(module_path, fromlist=[bp_name])
            blueprint = getattr(module, bp_name)
            app.register_blueprint(blueprint, url_prefix=url_prefix)
        except (ImportError, AttributeError):
            # Blueprint module not yet created — skip gracefully
            app.logger.debug(
                "Blueprint '%s' from '%s' not available — skipping registration",
                bp_name,
                module_path,
            )
