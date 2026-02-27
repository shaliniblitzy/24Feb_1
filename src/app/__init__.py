"""
Sonatype Nexus Repository - Flask Application Package.

This package contains the complete Flask-based reimplementation of the
Sonatype Nexus Repository Manager backend, originally implemented in
Java 21 with OSGi/Karaf, RESTEasy, Apache Shiro, and MyBatis.

The application is organized as a layered architecture:
- api/ — REST API route handlers (Flask Blueprints)
- models/ — SQLAlchemy data models
- schemas/ — Marshmallow serialization schemas
- services/ — Business logic service layer
- auth/ — Authentication and authorization
- formats/ — Format-specific protocol handlers
- storage/ — BlobStore implementations
- repositories/ — Repository type logic
- scheduler/ — Task scheduling
- events/ — Event system
- monitoring/ — Health checks and metrics
- search/ — Elasticsearch integration
- security/ — SSL/TLS management
- webhooks/ — Webhook dispatch
- plugins/ — Plugin architecture
- utils/ — Shared utilities

Architecture Notes:
    This package serves as the root namespace for the entire Flask application.
    The application factory pattern is used (see factory.py) to allow flexible
    configuration and testability. Extension singletons are defined in
    extensions.py and initialized with the app context in factory.py.

    This __init__.py is intentionally kept minimal to prevent circular import
    issues. The heavy lifting is delegated to:
        - factory.py: Application factory (create_app)
        - extensions.py: Flask extension singleton instances

Usage:
    # Access version information
    from src.app import __version__, __app_name__

    # Create the application (import from factory, not here)
    from src.app.factory import create_app
    app = create_app('development')
"""

# ---------------------------------------------------------------------------
# Package metadata
# ---------------------------------------------------------------------------

__version__: str = "1.0.0"
"""Semantic version string for the Nexus Repository Flask application."""

__app_name__: str = "nexus-repository"
"""Canonical application identifier used in logging, metrics, and API responses."""
