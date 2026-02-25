"""
Integration test scoped fixtures for the Flask Binary Repository Management System.

This conftest.py provides fixtures specific to the ``tests/integration/`` directory,
extending the root ``tests/conftest.py`` fixtures (auto-discovered by pytest) with
integration-specific setup:

- A real test database with per-test transaction rollback via ``db_session``
- Pre-seeded user and repository data fixtures
- Controlled external service mocks (S3, search, proxy, email)
- HTTP response mocking via the ``responses`` library
- JWT-based authentication header generators for admin, developer, readonly, and
  expired token scenarios
- Module-level ``pytestmark`` applying ``@pytest.mark.integration`` to all tests

The root ``tests/conftest.py`` provides the session-scoped ``app`` and ``db_instance``
fixtures that this module depends on through pytest's automatic fixture discovery.

Fixture summary:

- ``integration_app`` — session-scoped Flask app alias (delegates to root ``app``)
- ``init_db`` — session-scoped database initialiser
- ``db_session`` — function-scoped session with per-test cleanup
- ``client`` — function-scoped Flask test client
- ``auth_headers`` — admin JWT bearer headers
- ``developer_auth_headers`` — developer JWT bearer headers
- ``readonly_auth_headers`` — readonly JWT bearer headers
- ``expired_auth_headers`` — expired JWT bearer headers
- ``api_key_headers`` — API key authentication headers
- ``seeded_admin_user`` — pre-seeded admin user in test DB
- ``seeded_developer_user`` — pre-seeded developer user in test DB
- ``seeded_readonly_user`` — pre-seeded readonly user in test DB
- ``seeded_repository`` — pre-seeded hosted Maven repository
- ``seeded_repositories`` — pre-seeded collection of different repository types
- ``mock_s3`` — MockS3Client instance for BlobStore testing
- ``mock_search`` — MockSearchEngine instance for search testing
- ``mock_proxy`` — MockProxyClient instance for upstream registry testing
- ``mock_email`` — MockEmailService instance for notification testing
- ``mocked_responses`` — ``responses.RequestsMock`` context for HTTP call interception
- ``json_headers`` — standard JSON content-type headers
- ``clean_db`` — fixture ensuring all tables are empty before a test
- ``tmp_storage`` — temporary directory for File BlobStore integration testing

Assertion helpers (module-level functions):

- ``assert_json_response`` — validate HTTP status and JSON content type
- ``assert_error_response`` — validate error responses with optional message check
- ``assert_pagination`` — validate paginated response structure

Usage::

    def test_create_repository(client, auth_headers, db_session):
        response = client.post(
            "/api/v1/repositories",
            json={"name": "test-repo", "format": "maven", "type": "hosted"},
            headers=auth_headers,
        )
        assert response.status_code == 201

    def test_search_artifacts(client, auth_headers, mock_search):
        mock_search.index("components", {"name": "flask"}, doc_id="1")
        response = client.get("/api/v1/search?q=flask", headers=auth_headers)
        data = assert_json_response(response, expected_status=200)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, Generator
from unittest.mock import patch, MagicMock

import pytest
import responses

from flask_jwt_extended import create_access_token, create_refresh_token

from src.app import create_app
from src.extensions import db
from tests.fixtures.config_data import make_testing_config
from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_readonly_user,
)
from tests.fixtures.repository_data import (
    make_hosted_repo,
    make_proxy_repo,
    make_group_repo,
)
from tests.mocks.mock_s3_client import MockS3Client, create_mock_s3_client
from tests.mocks.mock_search_engine import MockSearchEngine, create_mock_search_engine
from tests.mocks.mock_proxy_client import MockProxyClient, create_mock_proxy_client
from tests.mocks.mock_email_service import MockEmailService, create_mock_email_service


# ---------------------------------------------------------------------------
# Module-level marker — applies @pytest.mark.integration to every test
# collected in this directory and its subdirectories.
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration


# =========================================================================
# Application factory fixture (integration-specific alias)
# =========================================================================


@pytest.fixture(scope="session")
def integration_app(app):
    """Provide the Flask application instance configured for integration tests.

    Delegates to the root ``tests/conftest.py`` session-scoped ``app`` fixture
    which creates a Flask application via ``create_app()`` with testing
    configuration (``TESTING=True``, ``sqlite:///:memory:``, test secret keys).

    This fixture exists as a named alias so integration tests can explicitly
    request ``integration_app`` when they need to distinguish from the root
    ``app`` fixture, though both refer to the same application instance.

    The root ``app`` fixture already applies:

    - ``TESTING = True``
    - ``SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"``
    - ``SECRET_KEY = "test-secret-key-not-for-production"``
    - ``JWT_SECRET_KEY = "test-jwt-secret-key-not-for-production"``
    - ``WTF_CSRF_ENABLED = False``
    - ``SERVER_NAME = "localhost"``

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application from root conftest.

    Yields
    ------
    Flask
        The configured Flask application instance.
    """
    yield app


# =========================================================================
# Database fixtures for integration tests
# =========================================================================


@pytest.fixture(scope="session")
def init_db(app):
    """Initialise the test database schema once per session.

    Creates all SQLAlchemy ORM-mapped tables at the start of the integration
    test session and drops them after all integration tests complete.  This
    fixture depends on the root ``app`` fixture which has already pushed an
    application context.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture (from root conftest).

    Yields
    ------
    SQLAlchemy
        The initialised SQLAlchemy database extension instance.
    """
    with app.app_context():
        db.create_all()
        yield db
        db.drop_all()


@pytest.fixture(scope="function")
def db_session(app, init_db):
    """Provide a database session with per-test isolation via cleanup.

    Each integration test receives a clean database session.  After the test
    completes (success or failure):

    1. Any uncommitted changes are rolled back.
    2. All committed data is deleted from every table in reverse dependency
       order to respect foreign key constraints.
    3. The session is committed to finalise the cleanup.

    This strategy ensures complete isolation between tests and supports
    parallel execution with ``pytest-xdist`` where each worker has its own
    in-memory SQLite database.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.
    init_db : SQLAlchemy
        The session-scoped database instance with tables created.

    Yields
    ------
    scoped_session
        The Flask-SQLAlchemy scoped session ready for database operations.
    """
    with app.app_context():
        yield db.session

        # Phase 1: Roll back any uncommitted changes from the test
        db.session.rollback()

        # Phase 2: Delete all committed data from every table in reverse
        # dependency order to respect foreign key constraints
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())
        db.session.commit()


# =========================================================================
# Test client fixture
# =========================================================================


@pytest.fixture(scope="function")
def client(app):
    """Provide a Flask test client for integration HTTP request testing.

    Function-scoped to ensure each test receives a fresh client instance,
    preventing cookie, session, or other state leakage between tests.

    The test client provides HTTP method wrappers: ``get()``, ``post()``,
    ``put()``, ``delete()``, ``patch()``, ``head()``, ``options()``.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture (from root conftest).

    Returns
    -------
    FlaskClient
        A Flask test client bound to the test application.
    """
    return app.test_client()


# =========================================================================
# Authentication header fixtures
# =========================================================================


@pytest.fixture(scope="function")
def auth_headers(app):
    """Generate Authorization headers with a valid admin JWT bearer token.

    Creates a JWT access token for a test admin identity using
    Flask-JWT-Extended's ``create_access_token``.  The token carries admin
    claims (``role='admin'``, ``privileges=['nx-all']``) and is signed with
    the application's ``JWT_SECRET_KEY``.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    dict
        Dictionary with ``Authorization`` (Bearer token) and
        ``Content-Type`` (application/json) headers.
    """
    with app.app_context():
        token = create_access_token(
            identity="test-admin-user",
            additional_claims={
                "role": "admin",
                "privileges": ["nx-all"],
                "username": "admin",
                "is_admin": True,
            },
        )
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }


@pytest.fixture(scope="function")
def developer_auth_headers(app):
    """Generate Authorization headers with a developer JWT bearer token.

    Creates a JWT access token for a test developer identity with standard
    developer privileges: repository view/edit, search read, and component
    upload — but no administrative access.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    dict
        Dictionary with ``Authorization`` (Bearer token) and
        ``Content-Type`` (application/json) headers.
    """
    with app.app_context():
        token = create_access_token(
            identity="test-developer-user",
            additional_claims={
                "role": "developer",
                "privileges": [
                    "nx-repository-view",
                    "nx-repository-edit",
                    "nx-search-read",
                    "nx-component-upload",
                ],
                "username": "developer",
                "is_admin": False,
            },
        )
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }


@pytest.fixture(scope="function")
def readonly_auth_headers(app):
    """Generate Authorization headers with a read-only JWT bearer token.

    Creates a JWT access token for a test read-only identity with minimal
    privileges: repository view and search read only.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    dict
        Dictionary with ``Authorization`` (Bearer token) and
        ``Content-Type`` (application/json) headers.
    """
    with app.app_context():
        token = create_access_token(
            identity="test-readonly-user",
            additional_claims={
                "role": "readonly",
                "privileges": [
                    "nx-repository-view",
                    "nx-search-read",
                ],
                "username": "readonly",
                "is_admin": False,
            },
        )
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }


@pytest.fixture(scope="function")
def expired_auth_headers(app):
    """Generate Authorization headers with an already-expired JWT token.

    Creates a JWT access token that has already expired by setting
    ``expires_delta=timedelta(seconds=-1)``.  Used for testing that the
    application correctly rejects expired bearer tokens with an appropriate
    HTTP 401 response.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    dict
        Dictionary with ``Authorization`` (expired Bearer token) and
        ``Content-Type`` (application/json) headers.
    """
    with app.app_context():
        token = create_access_token(
            identity="test-expired-user",
            additional_claims={
                "role": "admin",
                "privileges": ["nx-all"],
                "username": "expired-admin",
                "is_admin": True,
            },
            expires_delta=timedelta(seconds=-1),
        )
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }


@pytest.fixture(scope="function")
def api_key_headers():
    """Provide HTTP headers for API key-based authentication testing.

    Returns a dictionary with the ``X-API-Key`` header set to a
    test-only API key value.  This fixture provides the correct header
    format for testing API key extraction and validation logic in
    integration tests.

    Returns
    -------
    dict
        Dictionary containing ``X-API-Key`` and ``Content-Type`` headers.
    """
    return {
        "X-API-Key": "test-api-key-value-for-integration",
        "Content-Type": "application/json",
    }


# =========================================================================
# Pre-seeded data fixtures
# =========================================================================


@pytest.fixture(scope="function")
def seeded_admin_user(db_session, app):
    """Pre-seed an admin user into the test database.

    Creates a ``User`` entity with full admin privileges using the
    ``make_admin_user`` factory from ``tests/fixtures/user_data``.
    The user is flushed (not committed) so the calling test can decide
    when to commit.  The ``db_session`` fixture handles cleanup via
    rollback + delete after the test.

    Parameters
    ----------
    db_session : scoped_session
        The function-scoped database session with per-test cleanup.
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    User
        The persisted admin user ORM instance.
    """
    from src.models.user import User

    user_data = make_admin_user(username="integration-admin")
    # Filter out keys that are not actual User model columns to
    # prevent unexpected keyword argument errors
    valid_keys = {
        k: v for k, v in user_data.items() if hasattr(User, k)
    }
    # Parse ISO datetime strings to Python datetime objects for SQLAlchemy
    for _dk in ("created_at", "updated_at", "last_login"):
        if _dk in valid_keys and isinstance(valid_keys[_dk], str):
            valid_keys[_dk] = datetime.fromisoformat(valid_keys[_dk])
    user = User(**valid_keys)
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture(scope="function")
def seeded_developer_user(db_session, app):
    """Pre-seed a developer user into the test database.

    Creates a ``User`` entity with standard developer privileges (repository
    view/edit, search, upload) using the ``make_developer_user`` factory.

    Parameters
    ----------
    db_session : scoped_session
        The function-scoped database session with per-test cleanup.
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    User
        The persisted developer user ORM instance.
    """
    from src.models.user import User

    user_data = make_developer_user(username="integration-developer")
    valid_keys = {
        k: v for k, v in user_data.items() if hasattr(User, k)
    }
    # Parse ISO datetime strings to Python datetime objects for SQLAlchemy
    for _dk in ("created_at", "updated_at", "last_login"):
        if _dk in valid_keys and isinstance(valid_keys[_dk], str):
            valid_keys[_dk] = datetime.fromisoformat(valid_keys[_dk])
    user = User(**valid_keys)
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture(scope="function")
def seeded_readonly_user(db_session, app):
    """Pre-seed a read-only user into the test database.

    Creates a ``User`` entity with minimal read-only privileges (repository
    view and search only) using the ``make_readonly_user`` factory.

    Parameters
    ----------
    db_session : scoped_session
        The function-scoped database session with per-test cleanup.
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    User
        The persisted read-only user ORM instance.
    """
    from src.models.user import User

    user_data = make_readonly_user(username="integration-readonly")
    valid_keys = {
        k: v for k, v in user_data.items() if hasattr(User, k)
    }
    # Parse ISO datetime strings to Python datetime objects for SQLAlchemy
    for _dk in ("created_at", "updated_at", "last_login"):
        if _dk in valid_keys and isinstance(valid_keys[_dk], str):
            valid_keys[_dk] = datetime.fromisoformat(valid_keys[_dk])
    user = User(**valid_keys)
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture(scope="function")
def seeded_repository(db_session, app):
    """Pre-seed a hosted Maven repository into the test database.

    Creates a repository entity using the ``make_hosted_repo`` factory.
    If the ``Repository`` model is not yet available (greenfield project),
    stores the data in the session as a dictionary record for tests that
    work with raw data.

    Parameters
    ----------
    db_session : scoped_session
        The function-scoped database session with per-test cleanup.
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    Repository or dict
        The persisted repository ORM instance, or the raw data dictionary
        if the Repository model is not yet available.
    """
    repo_data = make_hosted_repo(
        name="integration-test-repo",
        format_type="maven",
    )
    try:
        from src.models.repository import Repository

        valid_keys = {
            k: v for k, v in repo_data.items() if hasattr(Repository, k)
        }
        # Parse ISO datetime strings to Python datetime objects for SQLAlchemy
        for _dk in ("created_at", "updated_at"):
            if _dk in valid_keys and isinstance(valid_keys[_dk], str):
                valid_keys[_dk] = datetime.fromisoformat(valid_keys[_dk])
        repo = Repository(**valid_keys)
        db_session.add(repo)
        db_session.flush()
        return repo
    except ImportError:
        # Repository model not yet implemented — return raw dict
        return repo_data


@pytest.fixture(scope="function")
def seeded_repositories(db_session, app):
    """Pre-seed multiple repositories of different types into the test database.

    Creates one hosted, one proxy, and one group repository using the
    respective factory functions from ``tests/fixtures/repository_data``.
    Returns a dictionary keyed by repository type for easy lookup in tests.

    Parameters
    ----------
    db_session : scoped_session
        The function-scoped database session with per-test cleanup.
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    dict
        Dictionary with keys ``'hosted'``, ``'proxy'``, ``'group'`` mapping
        to their respective repository instances (ORM or dict).
    """
    repos = {}
    factories = {
        "hosted": lambda: make_hosted_repo(
            name="integration-hosted-repo", format_type="maven"
        ),
        "proxy": lambda: make_proxy_repo(
            name="integration-proxy-repo", format_type="npm"
        ),
        "group": lambda: make_group_repo(
            name="integration-group-repo", format_type="maven"
        ),
    }

    try:
        from src.models.repository import Repository

        for repo_type, factory_fn in factories.items():
            repo_data = factory_fn()
            valid_keys = {
                k: v
                for k, v in repo_data.items()
                if hasattr(Repository, k)
            }
            # Parse ISO datetime strings for SQLAlchemy
            for _dk in ("created_at", "updated_at"):
                if _dk in valid_keys and isinstance(valid_keys[_dk], str):
                    valid_keys[_dk] = datetime.fromisoformat(valid_keys[_dk])
            repo = Repository(**valid_keys)
            db_session.add(repo)
            repos[repo_type] = repo

        db_session.flush()
    except ImportError:
        # Repository model not yet implemented — return raw dicts
        for repo_type, factory_fn in factories.items():
            repos[repo_type] = factory_fn()

    return repos


# =========================================================================
# Mock service fixtures
# =========================================================================


@pytest.fixture(scope="function")
def mock_s3():
    """Provide a MockS3Client for S3 BlobStore operation testing.

    Creates a fresh ``MockS3Client`` via the factory function.  The mock
    stores objects in memory and supports all standard S3 operations
    (``put_object``, ``get_object``, ``delete_object``, ``list_objects_v2``).
    The mock is reset after each test to prevent state leakage.

    Yields
    ------
    MockS3Client
        A fresh mock S3 client instance.
    """
    s3_client = create_mock_s3_client()
    yield s3_client
    s3_client.reset()


@pytest.fixture(scope="function")
def mock_search():
    """Provide a MockSearchEngine for search operation testing.

    Creates a fresh ``MockSearchEngine`` via the factory function.  The mock
    stores documents in memory and supports ``index``, ``search``, and
    ``delete_index`` operations.  The mock is reset after each test.

    Yields
    ------
    MockSearchEngine
        A fresh mock search engine instance.
    """
    engine = create_mock_search_engine()
    yield engine
    engine.reset()


@pytest.fixture(scope="function")
def mock_proxy():
    """Provide a MockProxyClient for upstream registry testing.

    Creates a fresh ``MockProxyClient`` via the factory function.  The mock
    simulates HTTP responses from upstream proxy registries (Maven Central,
    npm, Docker Hub, PyPI, NuGet, APT) with configurable status codes and
    payloads.  The mock is reset after each test.

    Yields
    ------
    MockProxyClient
        A fresh mock proxy client instance.
    """
    proxy_client = create_mock_proxy_client()
    yield proxy_client
    proxy_client.reset()


@pytest.fixture(scope="function")
def mock_email():
    """Provide a MockEmailService for notification testing.

    Creates a fresh ``MockEmailService`` via the factory function.  The mock
    captures sent notifications (email, webhook, alert) in memory for
    assertion without performing actual dispatch.  The mock is reset after
    each test.

    Yields
    ------
    MockEmailService
        A fresh mock email service instance.
    """
    service = create_mock_email_service()
    yield service
    service.reset()


# =========================================================================
# HTTP response mocking fixture
# =========================================================================


@pytest.fixture(scope="function")
def mocked_responses():
    """Activate the ``responses`` library for mocking external HTTP calls.

    Intercepts all outgoing HTTP requests made via the ``requests`` library
    during the test.  Any HTTP call that has not been explicitly registered
    will raise a ``ConnectionError``, ensuring no real network traffic
    occurs during integration tests.

    Tests register mock responses via the yielded ``RequestsMock`` object::

        def test_proxy_fetch(mocked_responses, client, auth_headers):
            mocked_responses.add(
                responses.GET,
                "https://registry.npmjs.org/lodash",
                json={"name": "lodash"},
                status=200,
            )
            response = client.get("/api/v1/proxy/npm/lodash", headers=auth_headers)
            assert response.status_code == 200

    Yields
    ------
    responses.RequestsMock
        The active request mock context manager.
    """
    with responses.RequestsMock() as rsps:
        yield rsps


# =========================================================================
# Utility fixtures
# =========================================================================


@pytest.fixture(scope="function")
def json_headers():
    """Provide standard JSON content-type headers for API requests.

    Returns a dictionary with ``Content-Type`` and ``Accept`` headers set to
    ``application/json``.  Use this fixture when making unauthenticated API
    requests that only need content negotiation headers.

    Returns
    -------
    dict
        JSON content-type and accept headers.
    """
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


@pytest.fixture(scope="function")
def clean_db(db_session, app):
    """Ensure all database tables are empty before the test runs.

    Iterates over all defined SQLAlchemy tables and deletes all rows
    in reverse dependency order (respecting foreign key constraints),
    then flushes the session.  Useful for tests requiring a guaranteed
    empty database at the START of the test.

    Parameters
    ----------
    db_session : scoped_session
        The function-scoped database session.
    app : Flask
        The session-scoped Flask application fixture.

    Yields
    ------
    scoped_session
        The cleaned database session with all tables empty.
    """
    with app.app_context():
        for table in reversed(db.metadata.sorted_tables):
            db_session.execute(table.delete())
        db_session.flush()
    yield db_session


@pytest.fixture(scope="function")
def tmp_storage(tmp_path):
    """Provide a temporary directory for File BlobStore integration testing.

    Creates a ``blobstore`` subdirectory inside pytest's ``tmp_path``
    fixture.  Each test gets a unique temporary directory that is
    automatically cleaned up by pytest after the test session.

    Parameters
    ----------
    tmp_path : pathlib.Path
        Pytest's built-in temporary directory fixture (unique per test).

    Returns
    -------
    pathlib.Path
        Path to the ``blobstore`` subdirectory inside ``tmp_path``.
    """
    storage_path = tmp_path / "blobstore"
    storage_path.mkdir(parents=True, exist_ok=True)
    return storage_path


# =========================================================================
# Assertion helpers (module-level functions)
# =========================================================================


def assert_json_response(response, expected_status=200):
    """Assert that a Flask test client response has the expected status and JSON content.

    Validates:
    1. The HTTP status code matches ``expected_status``
    2. The response content type is ``application/json``
    3. The response body is parseable as JSON

    Parameters
    ----------
    response : Response
        The Flask test client response object.
    expected_status : int, optional
        The expected HTTP status code (default: 200).

    Returns
    -------
    dict or list
        The parsed JSON response body.

    Raises
    ------
    AssertionError
        If the status code or content type does not match expectations.
    """
    assert response.status_code == expected_status, (
        f"Expected status {expected_status}, got {response.status_code}. "
        f"Response body: {response.get_data(as_text=True)[:500]}"
    )
    assert "application/json" in response.content_type, (
        f"Expected JSON content type, got {response.content_type}"
    )
    return response.get_json()


def assert_error_response(response, expected_status, expected_message=None):
    """Assert that a Flask test client response is an error with expected details.

    Validates:
    1. The HTTP status code matches ``expected_status``
    2. The JSON body contains an ``error`` or ``message`` key
    3. If ``expected_message`` is provided, the error message contains it

    Parameters
    ----------
    response : Response
        The Flask test client response object.
    expected_status : int
        The expected HTTP error status code (e.g. 400, 401, 403, 404, 500).
    expected_message : str, optional
        A substring expected to appear in the error message.

    Returns
    -------
    dict
        The parsed JSON error response body.

    Raises
    ------
    AssertionError
        If the response does not match error expectations.
    """
    assert response.status_code == expected_status, (
        f"Expected error status {expected_status}, got {response.status_code}. "
        f"Response body: {response.get_data(as_text=True)[:500]}"
    )
    data = response.get_json()
    assert data is not None, "Expected JSON body in error response"
    assert "error" in data or "message" in data, (
        f"Expected 'error' or 'message' key in response: {data}"
    )
    if expected_message is not None:
        error_msg = data.get("error", data.get("message", ""))
        assert expected_message in error_msg, (
            f"Expected '{expected_message}' in error message, got: '{error_msg}'"
        )
    return data


def assert_pagination(response_json, expected_total=None):
    """Assert that a response JSON body contains proper pagination metadata.

    Validates:
    1. The response contains an ``items`` or ``data`` key (the collection)
    2. If ``expected_total`` is provided, the ``total`` or ``totalCount``
       field matches

    Parameters
    ----------
    response_json : dict
        The parsed JSON response body.
    expected_total : int, optional
        The expected total count of items in the paginated collection.

    Returns
    -------
    dict
        The same ``response_json`` for further assertions.

    Raises
    ------
    AssertionError
        If the response does not contain pagination structure.
    """
    assert "items" in response_json or "data" in response_json, (
        f"Expected 'items' or 'data' key in paginated response: "
        f"{list(response_json.keys())}"
    )
    if expected_total is not None:
        total = response_json.get("total", response_json.get("totalCount", 0))
        assert total == expected_total, (
            f"Expected total={expected_total}, got total={total}"
        )
    return response_json
