"""
Test configuration dictionaries for all environment profiles.

Provides deterministic Flask configuration objects for app factory testing
and environment-specific settings validation across testing, development,
and production profiles. All factory functions follow the ``make_*`` naming
convention and accept ``**overrides`` for flexible customization.

Key design decisions:
- Configurations are static dictionaries with no random values to ensure
  deterministic test outcomes.
- Secret key values use clearly test-only strings that must never appear
  in production deployments (per AAP section 0.9.1).
- The ``make_testing_config()`` output is the PRIMARY configuration used
  by the ``tests/conftest.py`` ``app`` fixture for Flask test client setup.
- Specialized variants (SSL, S3, File storage, error cases) enable targeted
  testing of specific subsystems without modifying the base configurations.
"""

from datetime import timedelta
from typing import Dict, Any, Optional, List

# ---------------------------------------------------------------------------
# Base constants — clearly test-only values that must NEVER be used in
# production. Values are aligned with AAP section 0.9.1 requirements.
# ---------------------------------------------------------------------------

TEST_SECRET_KEY: str = "test-secret-key-not-for-production"
"""Flask SECRET_KEY for the testing environment."""

TEST_JWT_SECRET_KEY: str = "test-jwt-secret-key-not-for-production"
"""JWT signing key for the testing environment."""

TEST_DATABASE_URI: str = "sqlite:///:memory:"
"""In-memory SQLite URI for fast, isolated test runs."""

DEV_DATABASE_URI: str = "sqlite:///dev.db"
"""Local file-based SQLite URI for the development profile."""

PROD_DATABASE_URI: str = "postgresql://user:pass@localhost/prod_db"
"""PostgreSQL connection URI placeholder for the production profile."""


# ---------------------------------------------------------------------------
# Environment-specific configuration factories
# ---------------------------------------------------------------------------


def make_testing_config(**overrides: Any) -> Dict[str, Any]:
    """Return a Flask configuration dictionary for the **testing** environment.

    This is the primary configuration consumed by ``tests/conftest.py`` to
    create the Flask application under test.  It enables ``TESTING`` mode,
    uses an in-memory SQLite database, disables CSRF protection, and
    configures mocked S3 / search engine endpoints.

    Parameters
    ----------
    **overrides:
        Arbitrary key-value pairs merged into the base configuration,
        allowing individual tests to tweak settings without rebuilding
        the entire dictionary.

    Returns
    -------
    Dict[str, Any]
        A complete Flask configuration dictionary ready for
        ``app.config.from_mapping()``.
    """
    config: Dict[str, Any] = {
        # Core Flask settings
        "TESTING": True,
        "DEBUG": True,
        "SECRET_KEY": TEST_SECRET_KEY,
        "SERVER_NAME": "localhost",
        "PREFERRED_URL_SCHEME": "http",
        "FLASK_ENV": "testing",
        # SQLAlchemy / database settings
        "SQLALCHEMY_DATABASE_URI": TEST_DATABASE_URI,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "SQLALCHEMY_ECHO": False,
        # JWT / authentication settings
        "JWT_SECRET_KEY": TEST_JWT_SECRET_KEY,
        "JWT_ACCESS_TOKEN_EXPIRES": timedelta(hours=1),
        "JWT_REFRESH_TOKEN_EXPIRES": timedelta(days=30),
        # CSRF (disabled in tests for convenience)
        "WTF_CSRF_ENABLED": False,
        # Storage — default to local file-based BlobStore for tests
        "STORAGE_TYPE": "file",
        "STORAGE_PATH": "/tmp/test-blobstore",
        # S3 BlobStore (mocked endpoints)
        "S3_BUCKET_NAME": "test-bucket",
        "S3_ENDPOINT_URL": "http://localhost:5000",
        "S3_ACCESS_KEY": "test-access-key",
        "S3_SECRET_KEY": "test-secret-key",
        "S3_REGION": "us-east-1",
        # Search engine (mocked)
        "SEARCH_ENGINE_URL": "http://localhost:9200",
        # Logging
        "LOG_LEVEL": "DEBUG",
        # Request limits — 100 MB for tests
        "MAX_CONTENT_LENGTH": 100 * 1024 * 1024,
        # CORS — permissive during tests
        "CORS_ORIGINS": ["*"],
    }
    config.update(overrides)
    return config


def make_development_config(**overrides: Any) -> Dict[str, Any]:
    """Return a Flask configuration dictionary for the **development** environment.

    Mirrors a local developer workstation setup with SQL echo enabled for
    query debugging, a file-based SQLite database, and relaxed request-size
    limits.

    Parameters
    ----------
    **overrides:
        Arbitrary key-value pairs merged into the base configuration.

    Returns
    -------
    Dict[str, Any]
        A development-profile Flask configuration dictionary.
    """
    config: Dict[str, Any] = {
        # Core Flask settings
        "TESTING": False,
        "DEBUG": True,
        "SECRET_KEY": "dev-secret-key-change-in-production",
        "PREFERRED_URL_SCHEME": "http",
        "FLASK_ENV": "development",
        # SQLAlchemy — echo SQL statements for debugging
        "SQLALCHEMY_DATABASE_URI": DEV_DATABASE_URI,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "SQLALCHEMY_ECHO": True,
        # JWT
        "JWT_SECRET_KEY": "dev-jwt-secret-key",
        "JWT_ACCESS_TOKEN_EXPIRES": timedelta(hours=4),
        "JWT_REFRESH_TOKEN_EXPIRES": timedelta(days=30),
        # CSRF enabled in development
        "WTF_CSRF_ENABLED": True,
        # Storage
        "STORAGE_TYPE": "file",
        "STORAGE_PATH": "./data/blobstore",
        # Logging
        "LOG_LEVEL": "DEBUG",
        # Request limits — 500 MB
        "MAX_CONTENT_LENGTH": 500 * 1024 * 1024,
        # CORS — permissive in development
        "CORS_ORIGINS": ["http://localhost:3000", "http://localhost:8080"],
    }
    config.update(overrides)
    return config


def make_production_config(**overrides: Any) -> Dict[str, Any]:
    """Return a Flask configuration dictionary for the **production** environment.

    Applies hardened settings: ``DEBUG=False``, short-lived JWT access
    tokens, S3-backed storage, secure session cookies, and restrictive CORS.

    Parameters
    ----------
    **overrides:
        Arbitrary key-value pairs merged into the base configuration.

    Returns
    -------
    Dict[str, Any]
        A production-profile Flask configuration dictionary.
    """
    config: Dict[str, Any] = {
        # Core Flask settings
        "TESTING": False,
        "DEBUG": False,
        "SECRET_KEY": "prod-secret-key-must-be-overridden",
        "PREFERRED_URL_SCHEME": "https",
        "FLASK_ENV": "production",
        # SQLAlchemy — no echo in production
        "SQLALCHEMY_DATABASE_URI": PROD_DATABASE_URI,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "SQLALCHEMY_ECHO": False,
        # JWT — shorter expiry for security
        "JWT_SECRET_KEY": "prod-jwt-secret-key-must-be-overridden",
        "JWT_ACCESS_TOKEN_EXPIRES": timedelta(minutes=15),
        "JWT_REFRESH_TOKEN_EXPIRES": timedelta(days=7),
        # CSRF enabled
        "WTF_CSRF_ENABLED": True,
        # Storage — S3 by default in production
        "STORAGE_TYPE": "s3",
        # Logging — only warnings and above
        "LOG_LEVEL": "WARNING",
        # Request limits — 1 GB
        "MAX_CONTENT_LENGTH": 1024 * 1024 * 1024,
        # Secure session cookies
        "SESSION_COOKIE_SECURE": True,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        # CORS — no external origins by default
        "CORS_ORIGINS": [],
    }
    config.update(overrides)
    return config


# ---------------------------------------------------------------------------
# Specialized configuration variants for edge-case and error-path testing
# ---------------------------------------------------------------------------


def make_config_with_missing_required_fields() -> Dict[str, Any]:
    """Return a configuration with required fields intentionally omitted.

    The returned dictionary is missing ``SECRET_KEY`` and
    ``SQLALCHEMY_DATABASE_URI``, which are mandatory for a functioning
    Flask application.  Use this fixture to verify that the application
    factory raises an appropriate error when required settings are absent.

    Returns
    -------
    Dict[str, Any]
        An intentionally incomplete configuration dictionary.
    """
    return {
        "TESTING": True,
        "DEBUG": True,
        "FLASK_ENV": "testing",
        # SECRET_KEY intentionally omitted
        # SQLALCHEMY_DATABASE_URI intentionally omitted
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "JWT_ACCESS_TOKEN_EXPIRES": timedelta(hours=1),
        "LOG_LEVEL": "DEBUG",
        "STORAGE_TYPE": "file",
        "STORAGE_PATH": "/tmp/test-blobstore",
        "MAX_CONTENT_LENGTH": 100 * 1024 * 1024,
    }


def make_config_with_invalid_database_uri() -> Dict[str, Any]:
    """Return a configuration with an invalid database connection URI.

    The ``SQLALCHEMY_DATABASE_URI`` is set to a deliberately malformed
    scheme (``invalid://not-a-real-database``) that cannot be resolved by
    SQLAlchemy.  Use this fixture to verify that the application factory
    or database initialization code handles bogus URIs gracefully.

    Returns
    -------
    Dict[str, Any]
        A configuration dictionary with an invalid database URI.
    """
    config = make_testing_config()
    config["SQLALCHEMY_DATABASE_URI"] = "invalid://not-a-real-database"
    return config


def make_config_with_conflicting_settings() -> Dict[str, Any]:
    """Return a configuration with intentionally conflicting settings.

    Combines ``TESTING=True`` with production-like hardening options
    (``DEBUG=False``, secure cookies, S3 storage) that would not normally
    coexist in a coherent deployment.  Use this fixture to validate that
    the application factory detects or tolerates contradictory flags.

    Returns
    -------
    Dict[str, Any]
        A configuration dictionary with conflicting settings.
    """
    return {
        "TESTING": True,
        "DEBUG": False,
        "SECRET_KEY": TEST_SECRET_KEY,
        "FLASK_ENV": "production",
        "SQLALCHEMY_DATABASE_URI": TEST_DATABASE_URI,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "SQLALCHEMY_ECHO": True,
        "JWT_SECRET_KEY": TEST_JWT_SECRET_KEY,
        "JWT_ACCESS_TOKEN_EXPIRES": timedelta(minutes=15),
        "JWT_REFRESH_TOKEN_EXPIRES": timedelta(days=7),
        "WTF_CSRF_ENABLED": True,
        "STORAGE_TYPE": "s3",
        "LOG_LEVEL": "WARNING",
        "MAX_CONTENT_LENGTH": 1024 * 1024 * 1024,
        "SESSION_COOKIE_SECURE": True,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "CORS_ORIGINS": [],
    }


def make_config_with_ssl_enabled(**overrides: Any) -> Dict[str, Any]:
    """Return a configuration with SSL/TLS management enabled.

    Adds certificate and key file paths along with ``https`` as the
    preferred URL scheme.  Supports AAP Feature F-302 (SSL/TLS Management).

    Parameters
    ----------
    **overrides:
        Arbitrary key-value pairs merged into the base configuration.

    Returns
    -------
    Dict[str, Any]
        A testing configuration augmented with SSL settings.
    """
    config = make_testing_config()
    config.update(
        {
            "SSL_CERT_PATH": "/path/to/cert.pem",
            "SSL_KEY_PATH": "/path/to/key.pem",
            "SSL_CA_BUNDLE_PATH": "/path/to/ca-bundle.crt",
            "PREFERRED_URL_SCHEME": "https",
            "SESSION_COOKIE_SECURE": True,
            "SSL_REDIRECT": True,
        }
    )
    config.update(overrides)
    return config


def make_config_with_s3_storage(**overrides: Any) -> Dict[str, Any]:
    """Return a configuration specifically targeting S3 BlobStore.

    Sets ``STORAGE_TYPE`` to ``s3`` and populates all required S3
    connection parameters.  Supports AAP Feature F-202 (S3 BlobStore).

    Parameters
    ----------
    **overrides:
        Arbitrary key-value pairs merged into the base configuration.

    Returns
    -------
    Dict[str, Any]
        A testing configuration with S3 storage settings.
    """
    config = make_testing_config()
    config.update(
        {
            "STORAGE_TYPE": "s3",
            "S3_BUCKET_NAME": "test-s3-bucket",
            "S3_ENDPOINT_URL": "http://localhost:4566",
            "S3_ACCESS_KEY": "test-s3-access-key",
            "S3_SECRET_KEY": "test-s3-secret-key",
            "S3_REGION": "us-east-1",
            "S3_PREFIX": "artifacts/",
            "S3_FORCE_PATH_STYLE": True,
        }
    )
    config.update(overrides)
    return config


def make_config_with_file_storage(**overrides: Any) -> Dict[str, Any]:
    """Return a configuration specifically targeting File BlobStore.

    Sets ``STORAGE_TYPE`` to ``file`` and configures a temporary storage
    path.  Supports AAP Feature F-201 (File BlobStore).

    Parameters
    ----------
    **overrides:
        Arbitrary key-value pairs merged into the base configuration.

    Returns
    -------
    Dict[str, Any]
        A testing configuration with file-based storage settings.
    """
    config = make_testing_config()
    config.update(
        {
            "STORAGE_TYPE": "file",
            "STORAGE_PATH": "/tmp/test-file-blobstore",
            "STORAGE_STRICT_CONTENT_VALIDATION": True,
            "STORAGE_MAX_FILE_SIZE": 500 * 1024 * 1024,
        }
    )
    config.update(overrides)
    return config


# ---------------------------------------------------------------------------
# Configuration collection generators — useful with @pytest.mark.parametrize
# ---------------------------------------------------------------------------


def make_all_environment_configs() -> Dict[str, Dict[str, Any]]:
    """Return a mapping of environment name → configuration dictionary.

    Produces configs for all three supported environments.  Convenient
    for parametrized tests that validate configuration loading behaviour
    across every profile.

    Returns
    -------
    Dict[str, Dict[str, Any]]
        Keys are ``"testing"``, ``"development"``, and ``"production"``.
    """
    return {
        "testing": make_testing_config(),
        "development": make_development_config(),
        "production": make_production_config(),
    }


def get_environment_ids() -> List[str]:
    """Return the canonical list of environment names.

    Intended for use as ``ids`` in ``@pytest.mark.parametrize`` decorators
    so that test output clearly labels which environment profile is under
    evaluation.

    Returns
    -------
    List[str]
        ``["testing", "development", "production"]``
    """
    return ["testing", "development", "production"]


def make_config_for_environment(
    env_name: str, **overrides: Any
) -> Dict[str, Any]:
    """Dispatch to the correct environment-specific configuration factory.

    Parameters
    ----------
    env_name:
        One of ``"testing"``, ``"development"``, or ``"production"``.
    **overrides:
        Forwarded to the underlying ``make_*_config`` factory.

    Returns
    -------
    Dict[str, Any]
        The configuration dictionary for the requested environment.

    Raises
    ------
    ValueError
        If *env_name* is not a recognised environment identifier.
    """
    factories: Dict[str, Any] = {
        "testing": make_testing_config,
        "development": make_development_config,
        "production": make_production_config,
    }
    factory_fn = factories.get(env_name)
    if factory_fn is None:
        raise ValueError(
            f"Unrecognised environment name: {env_name!r}. "
            f"Expected one of {list(factories.keys())}."
        )
    return factory_fn(**overrides)


# ---------------------------------------------------------------------------
# BlobStore and cleanup policy configuration data
# ---------------------------------------------------------------------------


def make_blobstore_config(
    store_type: str = "file",
    name: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate a BlobStore configuration dictionary for testing.

    Produces the settings structure expected by BlobStore management
    endpoints and models, covering both file-system and S3-backed stores.

    Parameters
    ----------
    store_type:
        Either ``"file"`` or ``"s3"``.
    name:
        Human-readable BlobStore name.  Defaults to a descriptive
        placeholder based on *store_type*.
    **overrides:
        Arbitrary key-value pairs merged into the base configuration.

    Returns
    -------
    Dict[str, Any]
        A BlobStore configuration dictionary.

    Raises
    ------
    ValueError
        If *store_type* is not ``"file"`` or ``"s3"``.
    """
    if store_type == "file":
        config: Dict[str, Any] = {
            "name": name or "default-file-blobstore",
            "type": "file",
            "path": "/tmp/test-blobstore-data",
            "soft_quota": None,
        }
    elif store_type == "s3":
        config = {
            "name": name or "default-s3-blobstore",
            "type": "s3",
            "bucket": "test-blobstore-bucket",
            "prefix": "blobs/",
            "region": "us-east-1",
            "endpoint_url": "http://localhost:4566",
            "access_key_id": "test-access-key",
            "secret_access_key": "test-secret-key",
            "force_path_style": True,
            "soft_quota": None,
        }
    else:
        raise ValueError(
            f"Unsupported BlobStore type: {store_type!r}. "
            f"Expected 'file' or 's3'."
        )
    config.update(overrides)
    return config


def make_cleanup_policy_config(
    name: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """Generate a cleanup policy configuration for maintenance task testing.

    Supports AAP Feature F-204 (Cleanup Policies) by providing criteria
    dictionaries used to evaluate which components should be cleaned up.

    Parameters
    ----------
    name:
        Human-readable policy name.  Defaults to
        ``"default-cleanup-policy"``.
    **overrides:
        Arbitrary key-value pairs merged into the base configuration.

    Returns
    -------
    Dict[str, Any]
        A cleanup policy configuration dictionary.
    """
    config: Dict[str, Any] = {
        "name": name or "default-cleanup-policy",
        "format": "maven",
        "notes": "Automatically generated test cleanup policy.",
        "criteria": {
            "last_blob_updated": 30,
            "last_downloaded": 60,
            "is_prerelease": False,
            "regex": ".*-SNAPSHOT$",
        },
        "mode": "delete",
        "retain_sort_by": "version",
        "retain_count": 5,
    }
    config.update(overrides)
    return config
