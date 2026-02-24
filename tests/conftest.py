"""
Root test configuration with shared fixtures for the Flask Binary Repository
Management System test suite.

This is the ROOT-LEVEL conftest.py that provides fixtures automatically
available to ALL test files across all subdirectories (unit/, integration/,
functional/). It implements Flask's application factory pattern with per-test
database isolation via transaction cleanup, ensuring safe parallel execution
with pytest-xdist.

Shared fixtures defined here:

- ``app`` — Session-scoped Flask application instance with test configuration
- ``client`` — Function-scoped Flask test client for HTTP request testing
- ``db_instance`` — Session-scoped SQLAlchemy database with schema created
- ``db_session`` — Function-scoped database session with per-test cleanup
- ``auth_headers`` — Function-scoped JWT bearer token Authorization headers
- ``admin_user`` — Function-scoped pre-seeded admin user entity
- ``developer_user`` — Function-scoped pre-seeded developer user entity
- ``readonly_user`` — Function-scoped pre-seeded read-only user entity
- ``runner`` — Function-scoped Flask CLI test runner
- ``api_key_headers`` — Function-scoped API key Authorization headers
- ``clean_db`` — Function-scoped fixture that empties all database tables

Custom pytest markers registered:

- ``unit`` — Isolated component tests with mocked dependencies
- ``integration`` — Component interaction tests with test database
- ``functional`` — End-to-end workflow tests
- ``slow`` — Tests exceeding 5 seconds execution time

Usage in test files::

    def test_example(client, auth_headers):
        response = client.get("/api/v1/repositories", headers=auth_headers)
        assert response.status_code == 200

    def test_with_db(db_session, admin_user):
        assert admin_user.is_admin is True
        assert admin_user.role == "admin"
"""

from __future__ import annotations

import datetime
import os
from typing import Generator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from flask_jwt_extended import create_access_token

from src.app import create_app
from src.extensions import db
from src.models.user import User


# ---------------------------------------------------------------------------
# Environment setup — ensure test-specific environment variables are present
# before any application is created. These serve as fallback defaults; the
# test configuration dictionary in the ``app`` fixture takes precedence.
# ---------------------------------------------------------------------------
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")


# =========================================================================
# Pytest configuration hook
# =========================================================================


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers for test categorization.

    Markers are also declared in ``pyproject.toml`` under
    ``[tool.pytest.ini_options].markers``, but registering them
    programmatically here ensures they are always available even when
    running pytest from a subdirectory or with a non-default config path.

    Parameters
    ----------
    config : pytest.Config
        The pytest configuration object provided by the framework.
    """
    config.addinivalue_line(
        "markers",
        "unit: Unit tests - isolated component tests with mocked dependencies",
    )
    config.addinivalue_line(
        "markers",
        "integration: Integration tests - component interaction tests with test database",
    )
    config.addinivalue_line(
        "markers",
        "functional: Functional tests - end-to-end workflow tests",
    )
    config.addinivalue_line(
        "markers",
        "slow: Slow tests - tests exceeding 5 seconds execution time",
    )


# =========================================================================
# Application factory fixture
# =========================================================================


@pytest.fixture(scope="session")
def app() -> Generator[Flask, None, None]:
    """Create a Flask application instance configured for testing.

    Uses **session** scope for efficiency — the application is created once
    and shared across all tests in the session.  Database state isolation
    is handled per-test by the ``db_session`` fixture's cleanup mechanism.

    Configuration applied:

    - ``TESTING = True`` — enables Flask test-mode behaviour
    - ``SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"`` — fast, isolated DB
    - ``SECRET_KEY`` / ``JWT_SECRET_KEY`` — test-only keys (never production)
    - ``WTF_CSRF_ENABLED = False`` — disables CSRF for API testing convenience
    - ``SERVER_NAME = "localhost"`` — required for URL generation in tests

    Yields
    ------
    Flask
        The configured Flask application instance with an active application
        context pushed for the duration of the test session.
    """
    test_config: dict = {
        # Core Flask settings
        "TESTING": True,
        "DEBUG": True,
        "SECRET_KEY": "test-secret-key-not-for-production",
        "SERVER_NAME": "localhost",
        "PREFERRED_URL_SCHEME": "http",
        # SQLAlchemy / database settings
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "SQLALCHEMY_ECHO": False,
        # JWT / authentication settings
        "JWT_SECRET_KEY": "test-jwt-secret-key-not-for-production",
        # CSRF disabled for test convenience
        "WTF_CSRF_ENABLED": False,
        # Storage defaults (mocked in individual tests)
        "STORAGE_TYPE": "file",
        "STORAGE_PATH": "/tmp/test-blobstore",
        # S3 settings (mocked via moto in storage tests)
        "S3_BUCKET_NAME": "test-bucket",
        "S3_ENDPOINT_URL": "http://localhost:5000",
        "S3_ACCESS_KEY": "test-access-key",
        "S3_SECRET_KEY": "test-secret-key",
        "S3_REGION": "us-east-1",
        # Search engine (mocked in search tests)
        "SEARCH_ENGINE_URL": "http://localhost:9200",
        # Logging — verbose for debugging test failures
        "LOG_LEVEL": "DEBUG",
        # Request limits — 100 MB for tests
        "MAX_CONTENT_LENGTH": 100 * 1024 * 1024,
        # CORS — fully permissive during tests
        "CORS_ORIGINS": ["*"],
    }

    flask_app: Flask = create_app(test_config)

    # Push an application context for the entire test session so that
    # current_app, g, db, and other context-dependent objects are available.
    ctx = flask_app.app_context()
    ctx.push()

    yield flask_app

    # Teardown: pop the session-level application context
    ctx.pop()


# =========================================================================
# Test client fixture
# =========================================================================


@pytest.fixture(scope="function")
def client(app: Flask) -> FlaskClient:
    """Provide a Flask test client for HTTP request testing.

    Function-scoped to ensure each test receives a fresh client instance,
    preventing cookie, session, or other state leakage between tests.

    The test client provides HTTP method wrappers: ``get()``, ``post()``,
    ``put()``, ``delete()``, ``patch()``, ``head()``, ``options()``.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    FlaskClient
        A Flask test client bound to the test application.
    """
    return app.test_client()


# =========================================================================
# Database fixtures
# =========================================================================


@pytest.fixture(scope="session")
def db_instance(app: Flask) -> Generator:
    """Provide the SQLAlchemy database instance with all tables created.

    Session-scoped — tables are created once at the start of the test
    session and dropped at the end.  Per-test data isolation is handled
    by the ``db_session`` fixture which cleans up table contents after
    every test.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Yields
    ------
    SQLAlchemy
        The initialized SQLAlchemy database extension instance with all
        ORM-mapped tables created in the in-memory SQLite database.
    """
    # Create all tables defined by ORM models
    db.create_all()

    yield db

    # Teardown: drop all tables at the end of the test session
    db.session.remove()
    db.drop_all()


@pytest.fixture(scope="function")
def db_session(db_instance, app: Flask) -> Generator:
    """Provide a database session with per-test isolation via cleanup.

    Each test function receives the SQLAlchemy session in a clean state.
    After the test completes (whether it passes or fails):

    1. Any uncommitted changes are rolled back.
    2. All committed data is deleted from every table.
    3. The session is committed and cleaned up.

    This ensures complete isolation between tests and supports parallel
    execution with ``pytest-xdist`` where each worker has its own
    in-memory database.

    Parameters
    ----------
    db_instance : SQLAlchemy
        The session-scoped database instance with tables created.
    app : Flask
        The session-scoped Flask application fixture.

    Yields
    ------
    scoped_session
        The Flask-SQLAlchemy scoped session ready for database operations.
    """
    yield db_instance.session

    # Phase 1: Roll back any uncommitted changes from the test
    db_instance.session.rollback()

    # Phase 2: Delete all committed data from every table in reverse
    # dependency order to respect foreign key constraints
    for table in reversed(db_instance.metadata.sorted_tables):
        db_instance.session.execute(table.delete())
    db_instance.session.commit()


# =========================================================================
# Authentication helper fixtures
# =========================================================================


@pytest.fixture(scope="function")
def auth_headers(app: Flask) -> dict:
    """Provide HTTP Authorization headers with a valid JWT bearer token.

    Generates a JWT access token for a test admin user identity using
    Flask-JWT-Extended's ``create_access_token``.  The token is signed
    with the app's ``JWT_SECRET_KEY`` and valid for the configured
    ``JWT_ACCESS_TOKEN_EXPIRES`` duration.

    The default identity is ``"test-admin-user-id"`` with admin claims,
    suitable for testing endpoints that require admin-level authorization.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    dict
        Dictionary containing ``Authorization`` and ``Content-Type`` headers.

    Example
    -------
    ::

        def test_protected_endpoint(client, auth_headers):
            response = client.get("/api/v1/admin/health", headers=auth_headers)
            assert response.status_code == 200
    """
    with app.app_context():
        token: str = create_access_token(
            identity="test-admin-user-id",
            additional_claims={
                "role": "admin",
                "username": "test-admin",
                "privileges": ["nx-all"],
                "is_admin": True,
            },
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="function")
def admin_user(db_session, app: Flask) -> Generator[User, None, None]:
    """Pre-seed and provide an admin user with full system privileges.

    Creates a ``User`` entity in the test database with the ``admin`` role,
    ``is_admin=True``, and all privileges.  The user is automatically
    removed when the ``db_session`` fixture performs its post-test cleanup.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session fixture.
    app : Flask
        The session-scoped Flask application fixture.

    Yields
    ------
    User
        The persisted admin user ORM instance.
    """
    now: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    user: User = User(
        id="admin-test-user-id",
        username="test-admin",
        email="admin@test.example.com",
        first_name="Test",
        last_name="Admin",
        role="admin",
        status="active",
        is_admin=True,
        password_hash="sha256$test-hashed-admin-password",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    db_session.commit()
    yield user


@pytest.fixture(scope="function")
def developer_user(db_session, app: Flask) -> Generator[User, None, None]:
    """Pre-seed and provide a developer user with read/write privileges.

    Creates a ``User`` entity with the ``developer`` role, granting
    repository browse, upload, and search capabilities but no admin
    access.  Automatically cleaned up by ``db_session``.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session fixture.
    app : Flask
        The session-scoped Flask application fixture.

    Yields
    ------
    User
        The persisted developer user ORM instance.
    """
    now: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    user: User = User(
        id="developer-test-user-id",
        username="test-developer",
        email="developer@test.example.com",
        first_name="Test",
        last_name="Developer",
        role="developer",
        status="active",
        is_admin=False,
        password_hash="sha256$test-hashed-developer-password",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    db_session.commit()
    yield user


@pytest.fixture(scope="function")
def readonly_user(db_session, app: Flask) -> Generator[User, None, None]:
    """Pre-seed and provide a read-only user with minimal privileges.

    Creates a ``User`` entity with the ``readonly`` role, granting only
    repository browse and search access.  Automatically cleaned up by
    ``db_session``.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session fixture.
    app : Flask
        The session-scoped Flask application fixture.

    Yields
    ------
    User
        The persisted read-only user ORM instance.
    """
    now: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)
    user: User = User(
        id="readonly-test-user-id",
        username="test-readonly",
        email="readonly@test.example.com",
        first_name="Test",
        last_name="ReadOnly",
        role="readonly",
        status="active",
        is_admin=False,
        password_hash="sha256$test-hashed-readonly-password",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    db_session.commit()
    yield user


# =========================================================================
# Utility fixtures
# =========================================================================


@pytest.fixture(scope="function")
def runner(app: Flask):
    """Provide a Flask CLI test runner for testing CLI commands.

    Useful for validating Flask CLI commands registered via Click
    (e.g., database migration commands, seed commands, admin utilities).

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    FlaskCliRunner
        Flask's built-in CLI test runner bound to the test application.
    """
    return app.test_cli_runner()


@pytest.fixture(scope="function")
def api_key_headers() -> dict:
    """Provide HTTP headers for API key-based authentication testing.

    Returns a dictionary with the ``X-API-Key`` header set to a
    test-only API key value.  This fixture provides the header format
    for testing API key extraction and validation logic.

    Returns
    -------
    dict
        Dictionary containing ``X-API-Key`` and ``Content-Type`` headers.

    Example
    -------
    ::

        def test_api_key_auth(client, api_key_headers):
            response = client.get("/api/v1/repositories", headers=api_key_headers)
            assert response.status_code in (200, 401)
    """
    return {
        "X-API-Key": "test-api-key-value-0123456789abcdef",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="function")
def clean_db(db_instance, app: Flask) -> Generator:
    """Ensure all database tables are empty before a test runs.

    Iterates over all defined SQLAlchemy tables and deletes all rows
    in reverse dependency order (respecting foreign key constraints).
    This fixture is useful for tests that require a guaranteed empty
    database state beyond what the normal ``db_session`` cleanup provides.

    Use this fixture explicitly when you need to guarantee an empty
    database at the START of a test (``db_session`` cleans up AFTER).

    Parameters
    ----------
    db_instance : SQLAlchemy
        The session-scoped database instance.
    app : Flask
        The session-scoped Flask application fixture.

    Yields
    ------
    SQLAlchemy
        The cleaned database instance with all tables empty.

    Example
    -------
    ::

        def test_empty_repository_list(client, clean_db, auth_headers):
            response = client.get("/api/v1/repositories", headers=auth_headers)
            assert response.status_code == 200
            assert response.json == []
    """
    # Delete all rows from all tables in reverse dependency order
    for table in reversed(db_instance.metadata.sorted_tables):
        db_instance.session.execute(table.delete())
    db_instance.session.commit()

    yield db_instance

    # Post-test cleanup (mirrors db_session pattern)
    db_instance.session.rollback()
    for table in reversed(db_instance.metadata.sorted_tables):
        db_instance.session.execute(table.delete())
    db_instance.session.commit()
