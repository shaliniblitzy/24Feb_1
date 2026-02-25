"""
config/production.py - Production Environment Configuration

Production-specific configuration class that inherits from DefaultConfig.
Replaces the PostgreSQL clustered deployment configuration from the Java
source system.  Configures the Flask application for production use with
PostgreSQL, optimized connection pooling, strict security, structured JSON
logging, and Docker-oriented paths.

Characteristics:
    - DEBUG = False  — NEVER enable debug in production
    - PostgreSQL as the enterprise database (replaces PostgreSQL JDBC 42.7.2)
    - Optimized connection pool (replaces HikariCP 4.0.3)
    - SECRET_KEY MUST be set via environment variable (no fallback)
    - Secure cookie settings (HTTPS-only, HttpOnly)
    - JSON structured logging for SIEM integration
    - Restrictive CORS policy

Architecture mapping:
    PostgreSQL JDBC 42.7.2    →  psycopg2-binary via SQLAlchemy
    HikariCP 4.0.3 pool       →  SQLAlchemy QueuePool engine options
    Logback JSON encoder       →  Python JSON log formatter
    Jetty 12.0.5 HTTPS        →  Gunicorn behind Nginx / TLS termination

Performance targets (AAP Section 0.7.3):
    REST API Response Time         < 500ms average
    Cached Artifact Resolution     < 200ms
    Full-Text Search (100K comps)  < 2 seconds
    System Uptime                  >= 99.9%
    Storage Efficiency (dedup)     > 2:1 ratio
"""

import os

from config.default import DefaultConfig


class ProductionConfig(DefaultConfig):
    """Production configuration with PostgreSQL, strict security, and optimized performance.

    Inherits all defaults from :class:`DefaultConfig` and overrides
    settings that must differ in a production deployment.  The
    :attr:`SECRET_KEY` is intentionally read via ``os.environ[]``
    (dictionary-style access) so that a missing ``SECRET_KEY``
    environment variable raises a :class:`KeyError` immediately at
    import time rather than silently falling back to an insecure
    default.  This is a deliberate fail-fast design choice: production
    deployments **must** have a properly generated secret key.
    """

    # ==================================================================
    # 1. Flask Core — Production Mode
    # ==================================================================

    # NEVER enable debug in production — exposes the interactive
    # debugger and disables optimized error handling.
    DEBUG: bool = False

    # Not in testing mode.
    TESTING: bool = False

    # Logical environment name.
    ENV: str = "production"

    # ==================================================================
    # 2. Secret Key — MANDATORY in Production
    # ==================================================================

    # The SECRET_KEY MUST be explicitly set in the production environment.
    # Using os.environ['SECRET_KEY'] (dictionary-style access) intentionally
    # raises a KeyError if the variable is absent.  This fail-fast behaviour
    # ensures that a missing key is caught at startup rather than silently
    # falling back to an insecure default.
    SECRET_KEY: str = os.environ['SECRET_KEY']

    # JWT signing key — defaults to the class-level SECRET_KEY when not
    # independently set via its own environment variable.
    JWT_SECRET_KEY: str = os.environ.get('JWT_SECRET_KEY', SECRET_KEY)

    # ==================================================================
    # 3. Database — PostgreSQL  (replaces PostgreSQL JDBC 42.7.2)
    # ==================================================================

    # PostgreSQL is the enterprise/clustered database per AAP Section 0.7.1.
    # The connection string defaults to the standard Docker Compose service
    # name so that containerized deployments work out of the box.
    SQLALCHEMY_DATABASE_URI: str = os.environ.get(
        "DATABASE_URL",
        "postgresql://nexus:nexus@localhost:5432/nexus",
    )

    # Optimized connection pool settings replacing HikariCP 4.0.3.
    # Production workloads require a larger pool and aggressive recycling
    # to maintain connection health under sustained load.
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "pool_size": int(os.getenv("DATABASE_POOL_SIZE", "20")),
        "max_overflow": int(os.getenv("DATABASE_MAX_OVERFLOW", "40")),
        "pool_timeout": int(os.getenv("DATABASE_POOL_TIMEOUT", "30")),
        "pool_recycle": int(os.getenv("DATABASE_POOL_RECYCLE", "1800")),
        "pool_pre_ping": True,
    }

    # No SQL echo in production — eliminates unnecessary I/O and
    # prevents sensitive data from appearing in logs.
    SQLALCHEMY_ECHO: bool = False

    # Disable modification tracking for performance — not needed when
    # domain events are dispatched via Blinker signals.
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False

    # ==================================================================
    # 4. Security — Strict Production Settings
    # ==================================================================

    # Access token lifetime — strict 1-hour default for production.
    JWT_ACCESS_TOKEN_EXPIRES: int = int(
        os.getenv("JWT_ACCESS_TOKEN_EXPIRES", "3600")
    )

    # Refresh token lifetime — 7 days.
    JWT_REFRESH_TOKEN_EXPIRES: int = int(
        os.getenv("JWT_REFRESH_TOKEN_EXPIRES", "604800")
    )

    # Secure cookie flags — cookies are only sent over HTTPS and are
    # inaccessible to client-side JavaScript (mitigates XSS attacks).
    SESSION_COOKIE_SECURE: bool = True
    SESSION_COOKIE_HTTPONLY: bool = True
    REMEMBER_COOKIE_SECURE: bool = True
    REMEMBER_COOKIE_HTTPONLY: bool = True

    # ==================================================================
    # 5. Logging — Structured JSON for SIEM Integration
    # ==================================================================

    # Production log level — INFO by default; can be overridden via
    # environment variable for temporary debugging.
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # JSON structured logging for SIEM integration per AAP Section 0.7.2.
    # Machine-parsable output enables log aggregation, alerting, and
    # compliance auditing pipelines.
    LOG_FORMAT: str = "json"

    # Persistent log file path — defaults to the container log directory.
    LOG_FILE: str = os.getenv("LOG_FILE", "logs/nexus.log")

    # ==================================================================
    # 6. Storage — Container-Oriented Paths
    # ==================================================================

    # BlobStore backend type driven by environment variable.
    BLOBSTORE_TYPE: str = os.getenv("BLOBSTORE_TYPE", "file")

    # Default BlobStore path inside the Docker container.
    BLOBSTORE_PATH: str = os.getenv("BLOBSTORE_PATH", "/app/data/blobs")

    # ==================================================================
    # 7. Elasticsearch — Docker Service Name
    # ==================================================================

    # Uses the Docker Compose service name for container networking.
    ELASTICSEARCH_URL: str = os.getenv(
        "ELASTICSEARCH_URL", "http://elasticsearch:9200"
    )

    # ==================================================================
    # 8. CORS — Restrictive for Production
    # ==================================================================

    # CORS origins MUST be explicitly configured for production.
    # An empty default forces operators to set CORS_ORIGINS to the
    # specific frontend domain(s) that should be allowed.
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "")

    # ==================================================================
    # 9. HTTPS Enforcement
    # ==================================================================

    # Enforce HTTPS scheme for URL generation and redirects.
    PREFERRED_URL_SCHEME: str = "https"

    # ==================================================================
    # 10. Proxy Repository Configuration
    # ==================================================================

    # Connection timeout for upstream registry fetches.
    PROXY_TIMEOUT: int = int(os.getenv("PROXY_TIMEOUT", "60"))

    # Maximum retry attempts for failed upstream fetches.
    PROXY_MAX_RETRIES: int = int(os.getenv("PROXY_MAX_RETRIES", "3"))

    # ==================================================================
    # 11. Monitoring — Always Enabled in Production
    # ==================================================================

    # Prometheus metrics must be active for production observability.
    METRICS_ENABLED: bool = True

    # Health check endpoint is always available.
    HEALTH_CHECK_ENABLED: bool = True

    # ==================================================================
    # 12. API Documentation
    # ==================================================================

    # Production API title without environment label.
    API_TITLE: str = "Nexus Repository API"

    # Swagger/OpenAPI UI available at this URL prefix.
    OPENAPI_URL_PREFIX: str = "/api/docs"
