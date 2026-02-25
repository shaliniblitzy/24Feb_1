"""
config/development.py - Development Environment Configuration

Development-specific configuration class that inherits from DefaultConfig.
Replaces the H2 2.3.232 standalone database configuration from the Java source
system with SQLite for zero-config local development.

Characteristics:
    - DEBUG = True  — enables Flask debug mode with auto-reloader
    - SQLite database for zero-config standalone operation (replaces H2)
    - SQLALCHEMY_ECHO = True — logs all SQL queries for debugging
    - Human-readable log format for console output
    - Extended JWT token expiry for developer convenience
    - Permissive CORS for frontend development

Architecture mapping:
    Java H2 standalone config  →  SQLite via SQLAlchemy
    MyBatis SQL logging        →  SQLAlchemy echo mode
    Logback console appender   →  Python standard log formatter
"""

import os

from config.default import DefaultConfig

# Base directory — absolute path of the directory containing this file.
# Used to construct default file-system paths relative to the project root.
_basedir: str = os.path.abspath(os.path.dirname(__file__))


class DevelopmentConfig(DefaultConfig):
    """Development configuration with SQLite, debug mode, and verbose logging.

    Inherits all defaults from :class:`DefaultConfig` and overrides
    settings that benefit local development workflows.  SQLite is the
    default database (zero-config, no external services required), and
    SQL query echo is enabled so developers can inspect every query
    that SQLAlchemy generates.
    """

    # ==================================================================
    # 1. Flask Core — Debug & Environment
    # ==================================================================

    # Enable Flask debug mode — activates the interactive debugger and
    # automatic code reloader so that code changes take effect without
    # manually restarting the development server.
    DEBUG: bool = True

    # Not in testing mode.
    TESTING: bool = False

    # Logical environment name used in diagnostics and log context.
    ENV: str = "development"

    # ==================================================================
    # 2. Database — SQLite  (replaces H2 2.3.232)
    # ==================================================================

    # Default to a file-based SQLite database in the project root for
    # zero-config standalone operation.  Developers can override with a
    # PostgreSQL DATABASE_URL env var to test against a production-like
    # backend without changing code.
    SQLALCHEMY_DATABASE_URI: str = os.getenv(
        "DATABASE_URL",
        "sqlite:///" + os.path.join(_basedir, "..", "nexus_dev.db"),
    )

    # Echo all SQL statements to the console — invaluable for debugging
    # ORM behavior, verifying query patterns, and catching N+1 problems.
    # Replaces MyBatis SQL logging from the Java source.
    SQLALCHEMY_ECHO: bool = True

    # Enable modification tracking in development so that developers
    # can observe SQLAlchemy event notifications during debugging.
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = True

    # ==================================================================
    # 3. Logging — Verbose, Human-Readable
    # ==================================================================

    # Maximum verbosity for local development.
    LOG_LEVEL: str = "DEBUG"

    # Human-readable format for console output — much easier to scan
    # during development than the JSON format used in production.
    LOG_FORMAT: str = "standard"

    # ==================================================================
    # 4. Security — Relaxed for Developer Convenience
    # ==================================================================

    # Extended access token lifetime (24 hours) so that developers do
    # not need to re-authenticate frequently during long dev sessions.
    JWT_ACCESS_TOKEN_EXPIRES: int = int(
        os.getenv("JWT_ACCESS_TOKEN_EXPIRES", "86400")
    )

    # ==================================================================
    # 5. Storage — Local Filesystem BlobStore
    # ==================================================================

    # Local development BlobStore directory relative to the project root.
    BLOBSTORE_PATH: str = os.getenv(
        "BLOBSTORE_PATH",
        os.path.join(_basedir, "..", "data", "blobs"),
    )

    # ==================================================================
    # 6. Elasticsearch — Optional in Development
    # ==================================================================

    # Elasticsearch is optional during development; the application
    # should degrade gracefully when ES is unavailable.
    ELASTICSEARCH_URL: str = os.getenv(
        "ELASTICSEARCH_URL", "http://localhost:9200"
    )

    # ==================================================================
    # 7. CORS — Permissive for Frontend Development
    # ==================================================================

    # Allow all origins so that a locally running frontend (e.g. on a
    # different port) can communicate with the API without restrictions.
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "*")

    # ==================================================================
    # 8. API Documentation — Enabled
    # ==================================================================

    # Include environment label in the Swagger UI title for clarity.
    API_TITLE: str = "Nexus Repository API (Development)"

    # Swagger/OpenAPI UI available at this URL prefix.
    OPENAPI_URL_PREFIX: str = "/api/docs"
