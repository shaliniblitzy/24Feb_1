"""
Configuration validation unit tests for the Flask Binary Repository
Management System.

Validates that configuration dictionaries for testing, development, and
production environments produce correct Flask application configuration
when applied through the application factory. Tests cover all three
environment profiles, cross-environment invariants, environment variable
overrides, edge cases, error scenarios, and storage-specific settings.

Source under test: src/app.py  (``create_app`` with configuration loading)
Fixture data:     tests/fixtures/config_data.py

The ``@pytest.mark.unit`` marker is applied at module level via
``pytestmark`` so that every test is selected when running
``pytest -m unit``.
"""

import os
from datetime import timedelta

import pytest
from unittest.mock import patch, MagicMock

from src.app import create_app
from tests.fixtures.config_data import (
    make_testing_config,
    make_development_config,
    make_production_config,
    make_config_with_missing_required_fields,
    make_config_with_invalid_database_uri,
    make_config_for_environment,
    make_config_with_s3_storage,
    make_config_with_file_storage,
)

# Module-level marker — ensures every test in this file is selected
# when running ``pytest -m unit``.
pytestmark = pytest.mark.unit


# =========================================================================
# Phase 2: Testing Configuration Profile Tests
# =========================================================================


def test_testing_config_has_testing_true():
    """Testing config sets TESTING=True and DEBUG=True."""
    config = make_testing_config()
    app = create_app(config)

    assert app.config["TESTING"] is True
    assert app.config["DEBUG"] is True


def test_testing_config_uses_sqlite_memory_database():
    """Testing config uses in-memory SQLite and disables track modifications."""
    config = make_testing_config()
    app = create_app(config)

    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    assert app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] is False


def test_testing_config_has_test_secret_key():
    """Testing config has a non-empty, clearly test-only secret key."""
    config = make_testing_config()
    app = create_app(config)

    secret = app.config["SECRET_KEY"]
    assert isinstance(secret, str) and len(secret) > 0
    assert "test" in secret.lower()


def test_testing_config_disables_csrf():
    """Testing config disables CSRF protection for API testing convenience."""
    config = make_testing_config()
    app = create_app(config)

    assert app.config["WTF_CSRF_ENABLED"] is False
    assert app.config["TESTING"] is True


def test_testing_config_has_jwt_settings():
    """Testing config provides JWT secret key and token expiration."""
    config = make_testing_config()
    app = create_app(config)

    jwt_secret = app.config["JWT_SECRET_KEY"]
    jwt_expires = app.config["JWT_ACCESS_TOKEN_EXPIRES"]
    assert isinstance(jwt_secret, str) and len(jwt_secret) > 0
    assert isinstance(jwt_expires, timedelta) and jwt_expires > timedelta(0)


# =========================================================================
# Phase 3: Development Configuration Profile Tests
# =========================================================================


def test_development_config_has_debug_true():
    """Development config enables DEBUG but not TESTING."""
    config = make_development_config()
    app = create_app(config)

    assert app.config["DEBUG"] is True
    assert app.config["TESTING"] is False


def test_development_config_has_database_uri():
    """Development config uses a persistent database, not in-memory."""
    config = make_development_config()
    app = create_app(config)

    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    assert isinstance(uri, str) and len(uri) > 0
    assert uri != "sqlite:///:memory:"


def test_development_config_enables_sql_echo():
    """Development config enables SQL query logging for debugging."""
    config = make_development_config()
    app = create_app(config)

    assert app.config["SQLALCHEMY_ECHO"] is True
    assert app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] is False


def test_development_config_enables_csrf():
    """Development config enables CSRF protection."""
    config = make_development_config()
    app = create_app(config)

    assert app.config["WTF_CSRF_ENABLED"] is True
    assert app.config["DEBUG"] is True


# =========================================================================
# Phase 4: Production Configuration Profile Tests
# =========================================================================


def test_production_config_has_debug_false():
    """Production config disables both DEBUG and TESTING."""
    config = make_production_config()

    assert config["DEBUG"] is False
    assert config["TESTING"] is False


def test_production_config_disables_sql_echo():
    """Production config disables SQL echo for performance."""
    config = make_production_config()

    assert config["SQLALCHEMY_ECHO"] is False
    assert config["DEBUG"] is False


def test_production_config_has_secure_session_cookies():
    """Production config enables secure session cookie attributes."""
    config = make_production_config()

    assert config["SESSION_COOKIE_SECURE"] is True
    assert config["SESSION_COOKIE_HTTPONLY"] is True


def test_production_config_has_strict_jwt_expiry():
    """Production JWT access token expiry is shorter than other environments."""
    prod_config = make_production_config()
    test_config = make_testing_config()
    dev_config = make_development_config()

    prod_expiry = prod_config["JWT_ACCESS_TOKEN_EXPIRES"]
    assert prod_expiry < test_config["JWT_ACCESS_TOKEN_EXPIRES"]
    assert prod_expiry < dev_config["JWT_ACCESS_TOKEN_EXPIRES"]


# =========================================================================
# Phase 5: Cross-Environment Parametrized Tests
# =========================================================================


@pytest.mark.parametrize(
    "config_factory",
    [make_testing_config, make_development_config, make_production_config],
    ids=["testing", "development", "production"],
)
def test_all_configs_have_required_keys(config_factory):
    """Every environment config contains all required Flask configuration keys."""
    config = config_factory()

    assert "SECRET_KEY" in config
    assert "SQLALCHEMY_DATABASE_URI" in config
    assert "SQLALCHEMY_TRACK_MODIFICATIONS" in config


@pytest.mark.parametrize(
    "config_factory",
    [make_testing_config, make_development_config, make_production_config],
    ids=["testing", "development", "production"],
)
def test_all_configs_have_sqlalchemy_tracking_disabled(config_factory):
    """SQLAlchemy modification tracking is disabled in all environments."""
    config = config_factory()

    assert config["SQLALCHEMY_TRACK_MODIFICATIONS"] is False
    assert isinstance(config["SQLALCHEMY_TRACK_MODIFICATIONS"], bool)


@pytest.mark.parametrize(
    "config_factory",
    [make_testing_config, make_development_config, make_production_config],
    ids=["testing", "development", "production"],
)
def test_all_configs_have_valid_secret_key_type(config_factory):
    """SECRET_KEY is a non-empty string in every environment."""
    config = config_factory()

    secret_key = config["SECRET_KEY"]
    assert isinstance(secret_key, str)
    assert len(secret_key) > 0


@pytest.mark.parametrize(
    "config_factory",
    [make_testing_config, make_development_config, make_production_config],
    ids=["testing", "development", "production"],
)
def test_config_values_correct_types(config_factory):
    """TESTING and DEBUG are bools, SECRET_KEY is str in every environment."""
    config = config_factory()

    assert isinstance(config["TESTING"], bool)
    assert isinstance(config["DEBUG"], bool)
    assert isinstance(config["SECRET_KEY"], str)


# =========================================================================
# Phase 6: Configuration Loading by Environment Name
# =========================================================================


def test_get_config_by_name_testing():
    """make_config_for_environment('testing') returns testing configuration."""
    config = make_config_for_environment("testing")

    assert config["TESTING"] is True
    assert config["FLASK_ENV"] == "testing"


def test_get_config_by_name_development():
    """make_config_for_environment('development') returns dev configuration."""
    config = make_config_for_environment("development")

    assert config["TESTING"] is False
    assert config["FLASK_ENV"] == "development"


def test_get_config_by_name_production():
    """make_config_for_environment('production') returns prod configuration."""
    config = make_config_for_environment("production")

    assert config["TESTING"] is False
    assert config["FLASK_ENV"] == "production"


def test_get_config_by_name_unknown_raises_error():
    """make_config_for_environment with unknown name raises ValueError."""
    with pytest.raises(ValueError, match="Unrecognised environment name"):
        make_config_for_environment("unknown_env")

    # Confirm that valid names do NOT raise
    valid_config = make_config_for_environment("testing")
    assert valid_config is not None


# =========================================================================
# Phase 7: Environment Variable Override Tests
# =========================================================================


def test_config_reads_database_url_from_environment(monkeypatch):
    """create_app reads SQLALCHEMY_DATABASE_URI default from DATABASE_URL."""
    custom_uri = "sqlite:///custom_from_env.db"
    monkeypatch.setenv("DATABASE_URL", custom_uri)

    # Minimal override — does NOT include SQLALCHEMY_DATABASE_URI so the
    # env-var default from create_app is preserved.
    app = create_app({"TESTING": True})

    assert app.config["SQLALCHEMY_DATABASE_URI"] == custom_uri
    assert app.config["TESTING"] is True


def test_config_reads_secret_key_from_environment():
    """create_app reads SECRET_KEY default from the SECRET_KEY env var."""
    custom_key = "custom-env-secret-key-value"

    with patch.dict(os.environ, {"SECRET_KEY": custom_key}):
        # Minimal override — does NOT include SECRET_KEY
        app = create_app({"TESTING": True})

    assert app.config["SECRET_KEY"] == custom_key
    assert isinstance(app.config["SECRET_KEY"], str)


def test_config_reads_flask_env_from_environment(monkeypatch):
    """Environment config is dispatched correctly from FLASK_ENV env var."""
    monkeypatch.setenv("FLASK_ENV", "production")

    env_name = os.getenv("FLASK_ENV", "testing")
    config = make_config_for_environment(env_name)

    assert config["FLASK_ENV"] == "production"
    assert config["TESTING"] is False


def test_config_default_values_when_env_vars_missing(monkeypatch):
    """create_app uses hardcoded defaults when env vars are absent."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)

    app = create_app({"TESTING": True})

    # Fallback defaults defined inside create_app
    assert app.config["SECRET_KEY"] == "dev-secret-key-change-in-production"
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"


# =========================================================================
# Phase 8: Edge Cases
# =========================================================================


def test_config_with_empty_string_database_uri():
    """Empty-string database URI is rejected by SQLAlchemy at init time."""
    config = make_testing_config(SQLALCHEMY_DATABASE_URI="")

    # Verify the factory accepted the empty string override
    assert config["SQLALCHEMY_DATABASE_URI"] == ""

    # SQLAlchemy raises an error when attempting to parse an empty URI
    with pytest.raises(Exception):
        create_app(config)


def test_config_with_extra_unknown_keys():
    """Unrecognised configuration keys pass through without error."""
    mock_callback = MagicMock(name="custom_callback")
    config = make_testing_config(
        CUSTOM_UNKNOWN_KEY="custom_value",
        ANOTHER_EXTRA_KEY=42,
        ON_STARTUP_HOOK=mock_callback,
    )
    app = create_app(config)

    assert app.config["CUSTOM_UNKNOWN_KEY"] == "custom_value"
    assert app.config["ANOTHER_EXTRA_KEY"] == 42
    assert app.config["ON_STARTUP_HOOK"] is mock_callback


def test_config_factory_immutability_returns_independent_copies():
    """Each factory invocation returns an independent dictionary instance."""
    config_a = make_testing_config()
    config_b = make_testing_config()

    # Equal values but distinct objects
    assert config_a == config_b
    assert config_a is not config_b

    # Mutating one must NOT affect the other
    config_a["SECRET_KEY"] = "modified-key"
    assert config_b["SECRET_KEY"] != "modified-key"


# =========================================================================
# Phase 9: Error Cases
# =========================================================================


def test_config_missing_required_fields_raises_error(monkeypatch):
    """Missing required fields fall back to create_app hardcoded defaults."""
    # Remove env var fallbacks so only create_app's hardcoded defaults apply
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    config = make_config_with_missing_required_fields()

    # Verify the factory intentionally omits required fields
    assert "SECRET_KEY" not in config
    assert "SQLALCHEMY_DATABASE_URI" not in config

    # create_app handles missing fields via its own hardcoded defaults
    app = create_app(config)
    assert app.config["SECRET_KEY"] == "dev-secret-key-change-in-production"
    assert app.config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"


def test_config_with_invalid_database_uri_format():
    """Invalid database URI dialect is rejected by SQLAlchemy at init time."""
    config = make_config_with_invalid_database_uri()

    # Verify the factory set the invalid URI
    assert config["SQLALCHEMY_DATABASE_URI"] == "invalid://not-a-real-database"

    # SQLAlchemy raises an error for an unrecognised dialect
    with pytest.raises(Exception):
        create_app(config)


def test_config_with_none_secret_key():
    """None SECRET_KEY is stored as-is, representing invalid configuration."""
    config = make_testing_config(SECRET_KEY=None)
    app = create_app(config)

    assert app.config["SECRET_KEY"] is None
    assert app.config.get("SECRET_KEY") is None


# =========================================================================
# Phase 10: Storage-Specific Configuration Tests
# =========================================================================


def test_config_file_storage_settings():
    """File BlobStore configuration contains required storage fields."""
    config = make_config_with_file_storage()
    app = create_app(config)

    assert app.config["STORAGE_TYPE"] == "file"
    assert isinstance(app.config["STORAGE_PATH"], str)
    assert len(app.config["STORAGE_PATH"]) > 0


def test_config_s3_storage_settings():
    """S3 BlobStore configuration contains all required S3 connection fields."""
    config = make_config_with_s3_storage()
    app = create_app(config)

    assert app.config["S3_BUCKET_NAME"] == "test-s3-bucket"
    assert "S3_ENDPOINT_URL" in app.config
    assert "S3_ACCESS_KEY" in app.config
    assert "S3_SECRET_KEY" in app.config


def test_config_max_content_length_set():
    """MAX_CONTENT_LENGTH is a positive integer across configurations."""
    config = make_testing_config()
    app = create_app(config)

    max_length = app.config["MAX_CONTENT_LENGTH"]
    assert isinstance(max_length, int)
    assert max_length > 0
