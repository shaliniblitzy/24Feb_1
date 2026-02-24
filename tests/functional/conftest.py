"""
Functional test-specific conftest providing fixtures scoped to
``tests/functional/``.

This module extends the root ``tests/conftest.py`` fixtures (auto-discovered
by pytest via its parent-directory fixture inheritance mechanism) with
specialised fixtures for **multi-step, end-to-end workflow testing**.

Functional tests exercise complete user workflows through multiple
application layers (route → service → data access → response) and may
perform real database operations against the in-memory SQLite test
database.

**Key design principles:**

- **Inherits root fixtures** — ``app``, ``client``, ``db_instance``,
  ``db_session``, and ``auth_headers`` from ``tests/conftest.py`` are
  automatically available; only *additional* or *overriding* fixtures
  are defined here.
- **Parallel-safe** — All fixtures produce isolated state suitable for
  concurrent execution with ``pytest-xdist``.
- **No external calls** — Every external dependency (S3, search engine,
  upstream registries, email) is replaced by an in-memory mock.
- **Factory fixture pattern** — Workflow helpers (``create_test_repository``,
  ``upload_test_artifact``) return *callables* rather than static values,
  enabling tests to control creation parameters.

Exported fixtures and helpers
-----------------------------

Fixtures (available via pytest injection):
    ``functional_db``         — Session-scoped database with full schema
    ``db_session``            — Function-scoped session with rollback cleanup
    ``auth_headers``          — Admin JWT Authorization + Content-Type headers
    ``developer_auth_headers``— Developer JWT Authorization headers
    ``readonly_auth_headers`` — Read-only JWT Authorization headers
    ``no_auth_headers``       — Unauthenticated Content-Type-only headers
    ``mock_storage``          — In-memory MockS3Client for BlobStore ops
    ``mock_search``           — In-memory MockSearchEngine for search ops
    ``mock_upstream``         — In-memory MockProxyClient for proxy ops
    ``mock_notifications``    — In-memory MockEmailService for alerts
    ``create_test_repository``— Callable factory for repository creation
    ``upload_test_artifact``  — Callable factory for artifact uploads
    ``mocked_responses``      — ``responses.RequestsMock`` context for HTTP mocking
    ``json_headers``          — Standard JSON Content-Type + Accept headers
    ``tmp_storage``           — Temporary directory for File BlobStore testing

Module-level helpers (importable):
    ``assert_json_response``       — Validate JSON response with status
    ``assert_error_response``      — Validate error response with message
    ``assert_created_response``    — Validate 201 Created JSON response
    ``assert_no_content_response`` — Validate 204 No Content response
"""

from __future__ import annotations

import io
import json
import os
import datetime
from typing import Any, Callable, Dict, Generator, List

import pytest
import responses
from unittest.mock import patch, MagicMock

from flask_jwt_extended import create_access_token, create_refresh_token

from src.extensions import db

from tests.fixtures.config_data import make_testing_config
from tests.fixtures.repository_data import (
    make_hosted_repo,
    make_proxy_repo,
    make_group_repo,
)
from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_readonly_user,
)
from tests.fixtures.artifact_data import make_maven_artifact, make_binary_artifact
from tests.mocks.mock_s3_client import MockS3Client, create_mock_s3_client
from tests.mocks.mock_search_engine import MockSearchEngine, create_mock_search_engine
from tests.mocks.mock_proxy_client import MockProxyClient, create_mock_proxy_client
from tests.mocks.mock_email_service import MockEmailService, create_mock_email_service


# ---------------------------------------------------------------------------
# Module-level pytest marker — automatically applied to every test collected
# under tests/functional/ so that ``pytest -m functional`` selects them.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.functional


# =========================================================================
# Database fixtures
# =========================================================================


@pytest.fixture(scope="session")
def functional_db(app):
    """Initialise the database with the full ORM schema for functional tests.

    Creates all SQLAlchemy-mapped tables once per test session and tears
    them down at the end.  Individual test isolation is handled by the
    function-scoped ``db_session`` fixture below.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture (inherited from root
        ``tests/conftest.py``).

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
def db_session(app, functional_db):
    """Provide a database session with per-test isolation via cleanup.

    Overrides the root ``db_session`` fixture to depend on
    ``functional_db`` (session-scoped), ensuring tables exist before any
    test runs.  After each test, uncommitted changes are rolled back and
    all committed data is deleted from every table in reverse dependency
    order.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.
    functional_db : SQLAlchemy
        The session-scoped database instance with tables already created.

    Yields
    ------
    scoped_session
        A clean SQLAlchemy scoped session ready for database operations.
    """
    with app.app_context():
        yield functional_db.session

        # Roll back any uncommitted work from the test
        functional_db.session.rollback()

        # Delete all committed data in reverse dependency order to
        # respect foreign-key constraints
        for table in reversed(functional_db.metadata.sorted_tables):
            functional_db.session.execute(table.delete())
        functional_db.session.commit()


# =========================================================================
# Authentication fixtures
# =========================================================================


@pytest.fixture(scope="function")
def auth_headers(app):
    """Generate admin-level JWT Authorization headers for functional tests.

    Creates a short-lived access token with ``admin`` role and the
    ``nx-all`` wildcard privilege, suitable for testing endpoints that
    require full administrative access.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    dict
        A dictionary with ``Authorization`` (Bearer token) and
        ``Content-Type`` (application/json) headers.
    """
    with app.app_context():
        admin_data = make_admin_user(username="functional-admin")
        token: str = create_access_token(
            identity="functional-admin",
            additional_claims={
                "role": "admin",
                "privileges": admin_data.get("privileges", ["nx-all"]),
                "username": "functional-admin",
                "is_admin": True,
            },
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="function")
def developer_auth_headers(app):
    """Generate developer-level JWT Authorization headers.

    The token carries ``developer`` role with read, write, search, and
    upload privileges — but **no** admin access.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    dict
        Headers with a developer-level Bearer token.
    """
    with app.app_context():
        dev_data = make_developer_user(username="functional-developer")
        token: str = create_access_token(
            identity="functional-developer",
            additional_claims={
                "role": "developer",
                "privileges": dev_data.get(
                    "privileges",
                    [
                        "nx-repository-view",
                        "nx-repository-edit",
                        "nx-search-read",
                        "nx-component-upload",
                    ],
                ),
                "username": "functional-developer",
                "is_admin": False,
            },
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="function")
def readonly_auth_headers(app):
    """Generate read-only JWT Authorization headers.

    The token carries ``readonly`` role with only browse and search
    privileges — no write or admin access.

    Parameters
    ----------
    app : Flask
        The session-scoped Flask application fixture.

    Returns
    -------
    dict
        Headers with a read-only Bearer token.
    """
    with app.app_context():
        ro_data = make_readonly_user(username="functional-readonly")
        token: str = create_access_token(
            identity="functional-readonly",
            additional_claims={
                "role": "readonly",
                "privileges": ro_data.get(
                    "privileges",
                    ["nx-repository-view", "nx-search-read"],
                ),
                "username": "functional-readonly",
                "is_admin": False,
            },
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="function")
def no_auth_headers():
    """Provide headers with **no** authentication credentials.

    Useful for testing anonymous / unauthenticated access scenarios
    where the server should either permit public access or return a 401.

    Returns
    -------
    dict
        A dictionary containing only ``Content-Type`` set to JSON.
    """
    return {"Content-Type": "application/json"}


# =========================================================================
# Mock service fixtures
# =========================================================================


@pytest.fixture(scope="function")
def mock_storage():
    """Provide a ``MockS3Client`` for functional BlobStore testing.

    The client is created fresh for each test and automatically reset
    after the test completes, ensuring no storage state leaks between
    tests.

    Yields
    ------
    MockS3Client
        An in-memory S3 client with a pre-created default bucket.
    """
    client = create_mock_s3_client()
    yield client
    client.reset()


@pytest.fixture(scope="function")
def mock_search():
    """Provide a ``MockSearchEngine`` for functional search testing.

    Yields
    ------
    MockSearchEngine
        An in-memory search backend ready for indexing and queries.
    """
    engine = create_mock_search_engine()
    yield engine
    engine.reset()


@pytest.fixture(scope="function")
def mock_upstream():
    """Provide a ``MockProxyClient`` for upstream registry testing.

    Yields
    ------
    MockProxyClient
        A mock HTTP client with configurable status codes and payloads
        simulating upstream proxy registries for all 7 formats.
    """
    client = create_mock_proxy_client()
    yield client
    client.reset()


@pytest.fixture(scope="function")
def mock_notifications():
    """Provide a ``MockEmailService`` for notification / webhook testing.

    Yields
    ------
    MockEmailService
        A mock notification service that captures all dispatched messages
        in memory for later assertion.
    """
    service = create_mock_email_service()
    yield service
    service.reset()


# =========================================================================
# Workflow helper fixtures
# =========================================================================


@pytest.fixture(scope="function")
def create_test_repository(client, auth_headers, db_session):
    """Factory fixture for creating test repositories during functional tests.

    Returns a callable that:

    1. Accepts optional ``repo_data``, ``format_type``, and ``repo_type``
       parameters.
    2. Posts the repository configuration to ``/api/v1/repositories``.
    3. Tracks successfully-created repository names.
    4. Cleans up all created repositories after the test completes.

    Parameters
    ----------
    client : FlaskClient
        Function-scoped Flask test client.
    auth_headers : dict
        Admin-level JWT Authorization headers.
    db_session : scoped_session
        Function-scoped database session (ensures tables exist).

    Yields
    ------
    Callable
        A function ``_create_repo(repo_data=None, format_type='maven',
        repo_type='hosted')`` that returns the Flask test client response.
    """
    created_repos: List[str] = []

    def _create_repo(
        repo_data: Dict[str, Any] | None = None,
        format_type: str = "maven",
        repo_type: str = "hosted",
    ):
        """Create a repository via the REST API.

        Parameters
        ----------
        repo_data : dict, optional
            Full repository configuration dict.  When *None* a default
            configuration is generated via the ``make_hosted_repo``,
            ``make_proxy_repo``, or ``make_group_repo`` factory matching
            *repo_type*.
        format_type : str
            Repository format (e.g. ``'maven'``, ``'npm'``).
        repo_type : str
            Repository type — ``'hosted'``, ``'proxy'``, or ``'group'``.

        Returns
        -------
        Response
            The Flask test client response object.
        """
        if repo_data is None:
            factory_map = {
                "hosted": make_hosted_repo,
                "proxy": make_proxy_repo,
                "group": make_group_repo,
            }
            factory_fn = factory_map.get(repo_type, make_hosted_repo)
            repo_data = factory_fn(format_type=format_type)

        response = client.post(
            "/api/v1/repositories",
            json=repo_data,
            headers=auth_headers,
        )

        # Track successfully created repositories for teardown cleanup
        if response.status_code == 201:
            response_json = response.get_json()
            if response_json and "name" in response_json:
                created_repos.append(response_json["name"])
            elif repo_data and "name" in repo_data:
                created_repos.append(repo_data["name"])

        return response

    yield _create_repo

    # Teardown: attempt to delete every repository created during the test
    for repo_name in created_repos:
        try:
            client.delete(
                f"/api/v1/repositories/{repo_name}",
                headers=auth_headers,
            )
        except Exception:
            # Swallow cleanup errors — the test itself has already completed
            pass


@pytest.fixture(scope="function")
def upload_test_artifact(client, auth_headers):
    """Factory fixture for uploading test artifacts during functional tests.

    Returns a callable that:

    1. Accepts a ``repo_name`` and optional ``artifact_data`` / ``format_type``.
    2. Generates a Maven artifact by default if no ``artifact_data`` is supplied.
    3. Posts the artifact as a multipart form upload to the repository's
       component endpoint.

    Parameters
    ----------
    client : FlaskClient
        Function-scoped Flask test client.
    auth_headers : dict
        Admin-level JWT Authorization headers.

    Returns
    -------
    Callable
        A function ``_upload(repo_name, artifact_data=None,
        format_type='maven')`` returning the Flask test client response.
    """

    def _upload(
        repo_name: str,
        artifact_data: Dict[str, Any] | None = None,
        format_type: str = "maven",
    ):
        """Upload an artifact to a repository via the REST API.

        Parameters
        ----------
        repo_name : str
            Target repository name.
        artifact_data : dict, optional
            Artifact dict containing at minimum ``content`` (bytes).
            Auto-generated via ``make_maven_artifact`` when *None*.
        format_type : str
            Repository format hint (used only when generating defaults).

        Returns
        -------
        Response
            The Flask test client response object.
        """
        if artifact_data is None:
            artifact_data = make_maven_artifact()

        content_bytes: bytes = artifact_data.get("content", b"")
        filename: str = artifact_data.get("filename", "test-artifact")
        content_type: str = artifact_data.get(
            "content_type", "application/octet-stream"
        )

        data: Dict[str, Any] = {
            "file": (
                io.BytesIO(content_bytes),
                filename,
                content_type,
            ),
        }

        # Attach serialised metadata as a form field if present
        if "metadata" in artifact_data:
            data["metadata"] = json.dumps(artifact_data["metadata"])

        # Build upload headers — strip Content-Type so Flask's test client
        # can set the correct multipart boundary automatically.
        upload_headers: Dict[str, str] = {
            k: v for k, v in auth_headers.items() if k != "Content-Type"
        }

        response = client.post(
            f"/api/v1/repositories/{repo_name}/components",
            data=data,
            headers=upload_headers,
            content_type="multipart/form-data",
        )
        return response

    return _upload


# =========================================================================
# HTTP response mocking fixture
# =========================================================================


@pytest.fixture(scope="function")
def mocked_responses():
    """Activate HTTP mocking for outbound requests during functional tests.

    Uses the ``responses`` library to intercept all outbound HTTP calls
    made via the ``requests`` library (used by the proxy fetch service,
    webhook dispatcher, etc.).  Any request not explicitly registered
    will raise ``ConnectionError`` — ensuring no real network traffic
    occurs.

    Yields
    ------
    responses.RequestsMock
        The active mock registry.  Tests can add routes via
        ``rsps.add(method, url, ...)`` or ``rsps.get(url, ...)``.
    """
    with responses.RequestsMock() as rsps:
        yield rsps


# =========================================================================
# Assertion helper functions
# =========================================================================


def assert_json_response(response, expected_status: int = 200) -> dict:
    """Assert the response has the expected status and JSON content type.

    Parameters
    ----------
    response : Response
        A Flask test client response object.
    expected_status : int
        The expected HTTP status code (default ``200``).

    Returns
    -------
    dict
        The parsed JSON body of the response.

    Raises
    ------
    AssertionError
        If the status code or content type does not match.
    """
    assert response.status_code == expected_status, (
        f"Expected status {expected_status}, got {response.status_code}: "
        f"{response.data.decode('utf-8', errors='replace')}"
    )
    assert "application/json" in response.content_type, (
        f"Expected JSON content type, got {response.content_type}"
    )
    return response.get_json()


def assert_error_response(
    response,
    expected_status: int,
    expected_message: str | None = None,
) -> dict:
    """Assert the response is an error with the expected status and message.

    Parameters
    ----------
    response : Response
        A Flask test client response object.
    expected_status : int
        The expected HTTP error status code (e.g. 400, 401, 404, 409).
    expected_message : str, optional
        A substring expected within the ``error`` or ``message`` field
        of the JSON body (case-insensitive comparison).

    Returns
    -------
    dict
        The parsed JSON body of the error response.

    Raises
    ------
    AssertionError
        If the status code does not match, or the expected message
        substring is not found in the error body.
    """
    assert response.status_code == expected_status, (
        f"Expected error status {expected_status}, got {response.status_code}: "
        f"{response.data.decode('utf-8', errors='replace')}"
    )
    data: dict = response.get_json() or {}
    if expected_message is not None:
        error_text: str = str(
            data.get("error", data.get("message", data.get("detail", "")))
        )
        assert expected_message.lower() in error_text.lower(), (
            f"Expected error message containing '{expected_message}', "
            f"got '{error_text}'"
        )
    return data


def assert_created_response(response) -> dict:
    """Assert the response is ``201 Created`` with a JSON body.

    Parameters
    ----------
    response : Response
        A Flask test client response object.

    Returns
    -------
    dict
        The parsed JSON body of the 201 response.
    """
    return assert_json_response(response, expected_status=201)


def assert_no_content_response(response) -> None:
    """Assert the response is ``204 No Content``.

    Parameters
    ----------
    response : Response
        A Flask test client response object.

    Raises
    ------
    AssertionError
        If the status code is not 204.
    """
    assert response.status_code == 204, (
        f"Expected 204 No Content, got {response.status_code}: "
        f"{response.data.decode('utf-8', errors='replace')}"
    )


# =========================================================================
# Utility fixtures
# =========================================================================


@pytest.fixture(scope="function")
def json_headers():
    """Provide standard JSON Content-Type and Accept headers.

    Returns
    -------
    dict
        ``{'Content-Type': 'application/json', 'Accept': 'application/json'}``
    """
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


@pytest.fixture(scope="function")
def tmp_storage(tmp_path):
    """Provide a temporary directory for File BlobStore functional testing.

    Creates a fresh, isolated directory under pytest's per-test ``tmp_path``
    fixture.  Automatically cleaned up by pytest after the test session.

    Parameters
    ----------
    tmp_path : pathlib.Path
        pytest built-in fixture providing a unique temporary directory.

    Returns
    -------
    pathlib.Path
        A ``pathlib.Path`` pointing to the created storage directory.
    """
    storage_dir = tmp_path / "functional-blobstore"
    storage_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir
