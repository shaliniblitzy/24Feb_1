"""
config/default.py - Base Configuration Class

Foundational configuration module for the Nexus Repository Flask application.
All environment-specific configuration classes (DevelopmentConfig, ProductionConfig,
TestingConfig) inherit from DefaultConfig defined here.

This module replaces the Java System Configuration management layer (Feature F-404)
and implements the twelve-factor app methodology: every setting is driven by
environment variables with sensible development-safe defaults.

Architecture mapping:
    - Java System Properties / JNDI → os.getenv() with defaults
    - HikariCP 4.0.3 connection pool → SQLAlchemy engine options
    - Apache Shiro 2.0.0 config → JWT / LDAP / SSO settings
    - Jetty 12.0.5 server config → Gunicorn settings
    - Logback 1.2.13 config → Python logging settings
    - Quartz 2.3.2 config → APScheduler settings
    - Dropwizard Metrics 4.2.25 → Prometheus metrics settings
"""

import os

# ---------------------------------------------------------------------------
# Base directory — the absolute path of the directory containing this file.
# Used to construct default file-system paths for SQLite databases, BlobStore
# storage, and plugin directories relative to the project root.
# ---------------------------------------------------------------------------
basedir: str = os.path.abspath(os.path.dirname(__file__))


class DefaultConfig:
    """Base configuration with all default settings.

    All environment-specific configs inherit from this class.  Every attribute
    that might differ across environments is read from an environment variable
    via ``os.getenv()`` so that the twelve-factor app methodology is respected.

    Configuration surface area covers all 20 features across 5 functional
    categories:
        - Repository Management  (F-101 – F-104)
        - Storage Management     (F-201 – F-204)
        - Security               (F-301 – F-304)
        - Administration         (F-401 – F-404)
        - Integration            (F-501 – F-504)
    """

    # ======================================================================
    # 1. Flask Core Settings
    # ======================================================================

    # Secret key for session signing, CSRF tokens, and cookie encryption.
    # MUST be overridden in production via the SECRET_KEY environment variable.
    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")

    # Debug mode — disabled by default; DevelopmentConfig overrides to True.
    DEBUG: bool = False

    # Testing mode — disabled by default; TestingConfig overrides to True.
    TESTING: bool = False

    # Logical environment name used for diagnostics and logging context.
    ENV: str = "default"

    # ======================================================================
    # 2. Database Configuration  (replaces MyBatis 3.5.15 + HikariCP 4.0.3)
    # ======================================================================

    # SQLAlchemy database connection URI.
    # Default: file-based SQLite for zero-config standalone deployments
    # (replaces H2 2.3.232 embedded database from the Java stack).
    # Override with DATABASE_URL env var for PostgreSQL in clustered mode
    # (e.g. "postgresql://user:pass@host:5432/nexus").
    SQLALCHEMY_DATABASE_URI: str = os.getenv(
        "DATABASE_URL",
        "sqlite:///" + os.path.join(basedir, "..", "nexus.db"),
    )

    # Disable the Flask-SQLAlchemy event notification system to reduce
    # memory overhead — not needed when using Blinker for domain events.
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False

    # SQL echo — disable by default; DevelopmentConfig enables for debugging.
    SQLALCHEMY_ECHO: bool = False

    # Connection pool options replacing HikariCP 4.0.3 configuration.
    # These apply to QueuePool used with PostgreSQL and file-based SQLite.
    # In-memory SQLite (test config) overrides to use StaticPool instead.
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "pool_size": int(os.getenv("DATABASE_POOL_SIZE", "10")),
        "max_overflow": int(os.getenv("DATABASE_MAX_OVERFLOW", "20")),
        "pool_timeout": int(os.getenv("DATABASE_POOL_TIMEOUT", "30")),
        "pool_recycle": int(os.getenv("DATABASE_POOL_RECYCLE", "3600")),
        "pool_pre_ping": True,
    }

    # ======================================================================
    # 3. JWT & Authentication  (replaces Apache Shiro 2.0.0 + Java-JWT 4.4.0)
    # ======================================================================

    # JWT signing key — defaults to the Flask SECRET_KEY when not set
    # independently.  Production should set a dedicated JWT_SECRET_KEY.
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", SECRET_KEY)

    # Access token lifetime in seconds (default: 1 hour).
    JWT_ACCESS_TOKEN_EXPIRES: int = int(
        os.getenv("JWT_ACCESS_TOKEN_EXPIRES", "3600")
    )

    # Refresh token lifetime in seconds (default: 7 days).
    JWT_REFRESH_TOKEN_EXPIRES: int = int(
        os.getenv("JWT_REFRESH_TOKEN_EXPIRES", "604800")
    )

    # HMAC-SHA256 algorithm for JWT signing — matches Java-JWT 4.4.0 default.
    JWT_ALGORITHM: str = "HS256"

    # bcrypt cost factor for password hashing (replaces BouncyCastle 1.78.1).
    # 12 rounds provides a good security / performance balance for production.
    PASSWORD_HASH_ROUNDS: int = int(os.getenv("PASSWORD_HASH_ROUNDS", "12"))

    # ======================================================================
    # 4. LDAP Configuration  (replaces Shiro LDAP realm)
    # ======================================================================

    # Master toggle for LDAP/AD directory authentication backend.
    LDAP_ENABLED: bool = os.getenv("LDAP_ENABLED", "False").lower() == "true"

    # LDAP server connection URL (ldap:// or ldaps://).
    LDAP_SERVER_URL: str = os.getenv("LDAP_SERVER_URL", "ldap://localhost:389")

    # Bind DN for LDAP searches — service account used for user lookups.
    LDAP_BIND_DN: str = os.getenv("LDAP_BIND_DN", "")

    # Bind password for the LDAP service account.
    LDAP_BIND_PASSWORD: str = os.getenv("LDAP_BIND_PASSWORD", "")

    # Base DN for user searches (e.g. "ou=users,dc=example,dc=com").
    LDAP_USER_BASE_DN: str = os.getenv("LDAP_USER_BASE_DN", "")

    # Base DN for group/role searches (e.g. "ou=groups,dc=example,dc=com").
    LDAP_GROUP_BASE_DN: str = os.getenv("LDAP_GROUP_BASE_DN", "")

    # ======================================================================
    # 5. SSO Configuration  (replaces SAML/OIDC security plugin)
    # ======================================================================

    # Master toggle for SSO federation (SAML 2.0 or OpenID Connect).
    SSO_ENABLED: bool = os.getenv("SSO_ENABLED", "False").lower() == "true"

    # SSO identity provider type (e.g. "saml", "oidc").
    SSO_PROVIDER: str = os.getenv("SSO_PROVIDER", "")

    # OAuth2 / OIDC client identifier registered with the identity provider.
    SSO_CLIENT_ID: str = os.getenv("SSO_CLIENT_ID", "")

    # OAuth2 / OIDC client secret.
    SSO_CLIENT_SECRET: str = os.getenv("SSO_CLIENT_SECRET", "")

    # OIDC issuer URL for auto-discovery of provider endpoints.
    SSO_ISSUER_URL: str = os.getenv("SSO_ISSUER_URL", "")

    # ======================================================================
    # 6. Elasticsearch Configuration  (replaces Java ES client 2.4.3, F-103)
    # ======================================================================

    # Elasticsearch cluster URL for full-text component search.
    ELASTICSEARCH_URL: str = os.getenv(
        "ELASTICSEARCH_URL", "http://localhost:9200"
    )

    # Index name prefix to namespace Nexus indices within a shared cluster.
    ELASTICSEARCH_INDEX_PREFIX: str = os.getenv(
        "ELASTICSEARCH_INDEX_PREFIX", "nexus_"
    )

    # HTTP request timeout in seconds for Elasticsearch operations.
    ELASTICSEARCH_TIMEOUT: int = int(
        os.getenv("ELASTICSEARCH_TIMEOUT", "30")
    )

    # ======================================================================
    # 7. Storage / BlobStore Configuration  (F-201 File, F-202 S3)
    # ======================================================================

    # BlobStore backend type: "file" for local filesystem (F-201) or
    # "s3" for Amazon S3 (F-202).
    BLOBSTORE_TYPE: str = os.getenv("BLOBSTORE_TYPE", "file")

    # Default local filesystem path for the File BlobStore (F-201).
    # Artifacts are stored here with atomic writes (temp staging + rename)
    # and soft-delete support.
    BLOBSTORE_PATH: str = os.getenv(
        "BLOBSTORE_PATH",
        os.path.join(basedir, "..", "data", "blobs"),
    )

    # --- S3 BlobStore settings (F-202, replaces S3BlobStore.java) ---

    # S3 bucket name for artifact storage.
    S3_BUCKET: str = os.getenv("S3_BUCKET", "")

    # AWS region for the S3 bucket.
    S3_REGION: str = os.getenv("S3_REGION", "us-east-1")

    # AWS access key ID (use IAM roles in production instead of static keys).
    S3_ACCESS_KEY_ID: str = os.getenv("S3_ACCESS_KEY_ID", "")

    # AWS secret access key.
    S3_SECRET_ACCESS_KEY: str = os.getenv("S3_SECRET_ACCESS_KEY", "")

    # Custom S3-compatible endpoint URL (e.g. MinIO, LocalStack).
    # Leave empty to use the default AWS endpoint.
    S3_ENDPOINT_URL: str = os.getenv("S3_ENDPOINT_URL", "")

    # Server-side encryption algorithm for S3 objects.
    # Supports "AES256" (SSE-S3) or "aws:kms" (SSE-KMS).
    S3_ENCRYPTION: str = os.getenv("S3_ENCRYPTION", "AES256")

    # ======================================================================
    # 8. Scheduler Configuration  (replaces Quartz 2.3.2, F-402)
    # ======================================================================

    # Master toggle for the APScheduler background task scheduler.
    SCHEDULER_ENABLED: bool = (
        os.getenv("SCHEDULER_ENABLED", "True").lower() == "true"
    )

    # Whether to expose APScheduler's built-in REST API.
    # Disabled by default — the Nexus task API provides equivalent control.
    SCHEDULER_API_ENABLED: bool = False

    # Timezone for cron-based task scheduling expressions.
    SCHEDULER_TIMEZONE: str = os.getenv("SCHEDULER_TIMEZONE", "UTC")

    # ======================================================================
    # 9. Monitoring & Metrics  (replaces Dropwizard 4.2.25 + Prometheus, F-401)
    # ======================================================================

    # Master toggle for Prometheus metrics collection and export.
    METRICS_ENABLED: bool = (
        os.getenv("METRICS_ENABLED", "True").lower() == "true"
    )

    # URL path where Prometheus metrics are exposed for scraping.
    METRICS_PATH: str = os.getenv("METRICS_PATH", "/metrics")

    # URL path for the application health-check endpoint.
    HEALTH_CHECK_PATH: str = os.getenv("HEALTH_CHECK_PATH", "/api/v1/health")

    # Health check endpoint availability toggle.
    HEALTH_CHECK_ENABLED: bool = True

    # ======================================================================
    # 10. Logging Configuration  (replaces SLF4J 1.7.36 + Logback 1.2.13)
    # ======================================================================

    # Root log level — standard Python levels: DEBUG, INFO, WARNING, ERROR.
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Log output format: "json" for structured SIEM-compatible output
    # (per AAP Section 0.7.2) or "standard" for human-readable console logs.
    LOG_FORMAT: str = os.getenv("LOG_FORMAT", "json")

    # File path for persistent log output.
    LOG_FILE: str = os.getenv("LOG_FILE", "logs/nexus.log")

    # ======================================================================
    # 11. Webhook Configuration  (F-503)
    # ======================================================================

    # Master toggle for outbound webhook HTTP POST dispatch.
    WEBHOOK_ENABLED: bool = (
        os.getenv("WEBHOOK_ENABLED", "True").lower() == "true"
    )

    # HTTP request timeout in seconds for webhook delivery attempts.
    WEBHOOK_TIMEOUT: int = int(os.getenv("WEBHOOK_TIMEOUT", "10"))

    # Maximum retry count for failed webhook delivery (exponential backoff).
    WEBHOOK_MAX_RETRIES: int = int(os.getenv("WEBHOOK_MAX_RETRIES", "3"))

    # ======================================================================
    # 12. Proxy Repository Configuration
    #     (replaces HttpClientFacetImpl.java + Apache HttpClient 4.5.14)
    # ======================================================================

    # Forward HTTP proxy host for outbound requests to upstream registries.
    HTTP_PROXY_HOST: str = os.getenv("HTTP_PROXY_HOST", "")

    # Forward HTTP proxy port.
    HTTP_PROXY_PORT: str = os.getenv("HTTP_PROXY_PORT", "")

    # Forward HTTPS proxy host.
    HTTPS_PROXY_HOST: str = os.getenv("HTTPS_PROXY_HOST", "")

    # Forward HTTPS proxy port.
    HTTPS_PROXY_PORT: str = os.getenv("HTTPS_PROXY_PORT", "")

    # Comma-separated list of hosts to bypass the forward proxy.
    NO_PROXY: str = os.getenv("NO_PROXY", "localhost,127.0.0.1")

    # Connection timeout in seconds for proxy repository remote fetches.
    PROXY_TIMEOUT: int = int(os.getenv("PROXY_TIMEOUT", "60"))

    # Maximum retry attempts for failed upstream fetches.
    PROXY_MAX_RETRIES: int = int(os.getenv("PROXY_MAX_RETRIES", "3"))

    # ======================================================================
    # 13. CORS Configuration
    # ======================================================================

    # Allowed origins for Cross-Origin Resource Sharing.
    # Use "*" for permissive development, or a comma-separated list of
    # specific origins for production.
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "*")

    # ======================================================================
    # 14. SSL / TLS Configuration  (F-302)
    # ======================================================================

    # Enable HTTPS termination at the application level.
    # In production, TLS is typically terminated by a reverse proxy (Nginx).
    SSL_ENABLED: bool = os.getenv("SSL_ENABLED", "False").lower() == "true"

    # File-system path to the PEM-encoded SSL certificate.
    SSL_CERT_PATH: str = os.getenv("SSL_CERT_PATH", "")

    # File-system path to the PEM-encoded SSL private key.
    SSL_KEY_PATH: str = os.getenv("SSL_KEY_PATH", "")

    # ======================================================================
    # 15. API Documentation  (flask-smorest / OpenAPI, F-501)
    # ======================================================================

    # OpenAPI document title displayed in the Swagger UI header.
    API_TITLE: str = "Nexus Repository API"

    # Logical API version string.
    API_VERSION: str = "v1"

    # OpenAPI specification version for the generated document.
    OPENAPI_VERSION: str = "3.0.3"

    # URL prefix under which the OpenAPI JSON spec is served.
    OPENAPI_URL_PREFIX: str = "/api/docs"

    # Sub-path for the Swagger UI web interface.
    OPENAPI_SWAGGER_UI_PATH: str = "/swagger-ui"

    # CDN URL for Swagger UI static assets.
    OPENAPI_SWAGGER_UI_URL: str = (
        "https://cdn.jsdelivr.net/npm/swagger-ui-dist/"
    )

    # ======================================================================
    # 16. Gunicorn Server Configuration  (replaces Jetty 12.0.5)
    # ======================================================================

    # Number of Gunicorn worker processes (typically 2×CPU + 1).
    GUNICORN_WORKERS: int = int(os.getenv("GUNICORN_WORKERS", "4"))

    # Socket bind address and port for Gunicorn.
    GUNICORN_BIND: str = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")

    # Worker request timeout in seconds — long enough for large artifact
    # uploads but bounded to prevent runaway requests.
    GUNICORN_TIMEOUT: int = int(os.getenv("GUNICORN_TIMEOUT", "120"))

    # ======================================================================
    # 17. Scripting Configuration  (F-502)
    # ======================================================================

    # Master toggle for the server-side script execution engine.
    SCRIPTING_ENABLED: bool = (
        os.getenv("SCRIPTING_ENABLED", "True").lower() == "true"
    )

    # Maximum wall-clock execution time in seconds for a single script run.
    # Prevents runaway scripts from consuming resources indefinitely.
    SCRIPT_MAX_EXECUTION_TIME: int = int(
        os.getenv("SCRIPT_MAX_EXECUTION_TIME", "300")
    )

    # ======================================================================
    # 18. Plugin Configuration  (F-504)
    # ======================================================================

    # Master toggle for the plugin extension point framework.
    PLUGINS_ENABLED: bool = (
        os.getenv("PLUGINS_ENABLED", "True").lower() == "true"
    )

    # Directory path where plugin packages are discovered and loaded from.
    PLUGINS_DIRECTORY: str = os.getenv(
        "PLUGINS_DIRECTORY",
        os.path.join(basedir, "..", "plugins"),
    )
