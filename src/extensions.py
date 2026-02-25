"""
Flask extension instances for the Binary Repository Management System.

Extensions are instantiated here without binding to a specific application
instance. They are initialized with the application in the ``create_app()``
factory function, supporting the application factory pattern required for
testing flexibility and multiple configuration profiles.

Usage::

    from src.extensions import db, jwt, migrate

    # In the application factory:
    db.init_app(app)
    jwt.init_app(app)
    migrate.init_app(app, db)
"""

from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager
from flask_migrate import Migrate

# ---------------------------------------------------------------------------
# SQLAlchemy database instance — ORM engine for all data models
# ---------------------------------------------------------------------------
db: SQLAlchemy = SQLAlchemy()

# ---------------------------------------------------------------------------
# JWT authentication manager — handles token creation, validation, and
# claims extraction for bearer-token-based authentication
# ---------------------------------------------------------------------------
jwt: JWTManager = JWTManager()

# ---------------------------------------------------------------------------
# Database migration manager — Alembic integration for schema versioning
# ---------------------------------------------------------------------------
migrate: Migrate = Migrate()
