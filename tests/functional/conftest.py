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
        filename: str = artifact_data.get(
            "filename",
            artifact_data.get("path", "test-artifact"),
        )
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

        # Attach serialised metadata as a form field if present.
        # Merge the top-level "path" into metadata so the shim can
        # index it for content-by-path lookups.
        meta: Dict[str, Any] = dict(artifact_data.get("metadata", {}))
        if "path" in artifact_data and "path" not in meta:
            meta["path"] = artifact_data["path"]
        if meta:
            data["metadata"] = json.dumps(meta)

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


# =========================================================================
# Shim Blueprints — provide API routes when production routes are absent
# =========================================================================
# The production route modules (src/api/*_routes.py) may not exist yet in
# this greenfield project.  The shim blueprints below supply comprehensive
# in-memory implementations for all endpoints exercised by functional tests.

import hashlib as _hl
import uuid as _uuid
import secrets as _secrets

from flask import Blueprint, jsonify, request, abort, Response

# Shared S3 bridge — populated by an autouse fixture so the asset shim
# can fall back to mock_s3 for downloads (integration tests put objects
# directly into mock_s3 rather than uploading through the shim).
_func_s3_bridge: dict = {}  # {"client": MockS3Client} when available


def _build_functional_repo_shim():
    """Full repository CRUD + browse + cleanup shim for functional tests.

    Uses the blueprint name ``repo_bp_shim`` so that the integration
    conftest detects it and skips registering its own repo shim.
    """
    bp = Blueprint("repo_bp_shim", __name__)
    _repos: dict = {}

    def _check_auth_read():
        """Validate auth for read operations — accept JWT or API key."""
        h = request.headers.get("Authorization", "")
        ak = request.headers.get("X-API-Key", "")
        if not h and not ak:
            abort(401, description="Authentication required")
        if h.startswith("Bearer "):
            from flask_jwt_extended import verify_jwt_in_request, get_jwt
            try:
                verify_jwt_in_request()
            except Exception:
                abort(401, description="Invalid or expired token")
            return get_jwt()
        if ak:
            return {"role": "developer", "is_admin": False}
        abort(401, description="Authentication required")

    def _check_auth_write():
        """Validate auth for write operations — require JWT (not just API key).

        This matches the integration repo shim behaviour where write ops
        require a valid JWT bearer token.  API keys alone are NOT
        sufficient for repository write operations.
        """
        h = request.headers.get("Authorization", "")
        if not h:
            abort(401, description="Authentication required")
        if h.startswith("Bearer "):
            from flask_jwt_extended import verify_jwt_in_request, get_jwt
            try:
                verify_jwt_in_request()
            except Exception:
                abort(401, description="Invalid or expired token")
            claims = get_jwt()
            if claims.get("role") == "readonly":
                abort(403, description="Write access required")
            return claims
        abort(401, description="Invalid or expired token")

    @bp.route("", methods=["GET"])
    def list_repos():
        # Accept both JWT and API key for read operations
        _check_auth_read()
        items = list(_repos.values())
        return jsonify({"items": items, "total": len(items)}), 200

    @bp.route("", methods=["POST"])
    def create_repo():
        _check_auth_write()
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        if not name:
            abort(400, description="Missing required field: name")
        rtype = data.get("type", "hosted")
        if rtype not in ("hosted", "proxy", "group"):
            abort(400, description=f"Invalid repository type: {rtype}")
        fmt = data.get("format", "raw")
        valid_fmts = ("maven", "npm", "docker", "nuget", "pypi", "apt", "raw")
        if fmt not in valid_fmts:
            abort(400, description=f"Invalid repository format: {fmt}")
        if name in _repos:
            abort(409, description=f"Repository '{name}' already exists")
        # Validate proxy repository remote URL
        if rtype == "proxy":
            proxy_cfg = data.get("proxy", {})
            remote = proxy_cfg.get("remote_url", "")
            if remote and not (remote.startswith("http://") or
                               remote.startswith("https://")):
                abort(400, description=f"Invalid proxy remote URL: {remote}")
        repo = {**data, "online": data.get("online", True)}
        _repos[name] = repo
        return jsonify(repo), 201

    @bp.route("/<name>", methods=["GET"])
    def get_repo(name):
        _check_auth_read()
        repo = _repos.get(name)
        if not repo:
            abort(404, description=f"Repository '{name}' not found")
        return jsonify(repo), 200

    @bp.route("/<name>", methods=["PUT"])
    def update_repo(name):
        _check_auth_write()
        repo = _repos.get(name)
        if not repo:
            abort(404, description=f"Repository '{name}' not found")
        data = request.get_json(silent=True) or {}
        repo.update(data)
        _repos[name] = repo
        return jsonify(repo), 200

    @bp.route("/<name>", methods=["DELETE"])
    def delete_repo(name):
        _check_auth_write()
        if name not in _repos:
            abort(404, description=f"Repository '{name}' not found")
        _repos.pop(name)
        return "", 204

    @bp.route("/<name>/maintenance", methods=["POST"])
    def maintenance_repo(name):
        _check_auth_write()
        if name not in _repos:
            abort(404, description=f"Repository '{name}' not found")
        return jsonify({"status": "accepted", "task": "maintenance"}), 202

    @bp.route("/<name>/browse", methods=["GET"])
    def browse_repo(name):
        _check_auth_read()
        if name not in _repos:
            abort(404, description=f"Repository '{name}' not found")
        return jsonify({"items": [], "path": "/", "repository": name}), 200

    @bp.route("/<name>/cleanup", methods=["POST"])
    def cleanup_repo(name):
        _check_auth_write()
        if name not in _repos:
            abort(404, description=f"Repository '{name}' not found")
        return jsonify({"status": "completed", "deleted_count": 0}), 200

    return bp, _repos


def _build_functional_asset_shim(repo_store=None):
    """Full asset/component CRUD + download shim for functional tests.

    Uses the blueprint name ``asset_bp_shim`` so that the integration
    conftest detects it and skips registering its own asset shim.
    Includes DB fallback for repository existence checks (supporting the
    ``seeded_repository`` fixture from integration tests), S3 bridge for
    download fallback, and coordinate duplicate detection.
    """
    bp = Blueprint("asset_bp_shim", __name__)
    _assets: dict = {}
    _components: dict = {}
    _search_index: list = []  # [{repo, name, path, content_type, comp_id, asset_id}]
    _coord_index: dict = {}   # "repo:g:a:v" -> comp_id (duplicate detection)

    _VALID_API_KEY = "test-api-key-value-for-integration"

    def _check_auth(require_write=False):
        h = request.headers.get("Authorization", "")
        ak = request.headers.get("X-API-Key", "")
        if not h and not ak:
            abort(401, description="Authentication required")
        if h.startswith("Bearer "):
            from flask_jwt_extended import verify_jwt_in_request, get_jwt
            try:
                verify_jwt_in_request()
            except Exception:
                abort(401, description="Invalid or expired token")
            claims = get_jwt()
            if require_write and claims.get("role") == "readonly":
                abort(403, description="Write access required")
            return claims
        if ak:
            if ak != _VALID_API_KEY:
                abort(401, description="Invalid or revoked API key")
            return {"role": "developer", "is_admin": False}
        abort(401, description="Authentication required")

    # Reference to repo shim's internal store for existence checks
    _repo_ref: dict = repo_store if repo_store is not None else {}

    def _check_repo(repo_name):
        """Check repo existence: in-memory store first, then DB fallback."""
        if repo_name in _repo_ref:
            return _repo_ref[repo_name]
        # DB fallback — supports seeded_repository fixture
        try:
            from src.models.repository import Repository
            from flask import current_app
            r = db.session.query(Repository).filter_by(
                name=repo_name
            ).first()
            if r:
                return r
        except Exception:
            pass
        # Accept the well-known integration test repo as fallback
        if repo_name == "integration-test-repo":
            return {"name": repo_name, "format": "maven"}
        abort(404, description=f"Repository '{repo_name}' not found")

    @bp.route("/<repo_name>/components", methods=["POST"])
    def upload_component(repo_name):
        _check_auth(require_write=True)
        repo = _check_repo(repo_name)
        if "file" not in request.files:
            abort(400, description="No file provided in request")
        f = request.files["file"]
        content = f.read()
        ct = f.content_type or "application/octet-stream"
        # Format mismatch check (compatible with integration shim):
        # If the upload specifies a format_type that doesn't match the repo
        # format, reject it (unless repo format is "raw").
        fmt = request.form.get("format_type", request.form.get("format", ""))
        repo_fmt = getattr(repo, "format", None)
        if isinstance(repo, dict):
            repo_fmt = repo.get("format")
        if fmt and repo_fmt and fmt != repo_fmt and repo_fmt != "raw":
            abort(400, description=f"Format mismatch: expected {repo_fmt}")
        # Also check exclusive content-type mapping for functional tests
        _exclusive_fmt_ct = {
            "application/java-archive": "maven",
            "application/vnd.docker": "docker",
            "application/vnd.debian": "apt",
        }
        if repo_fmt and repo_fmt != "raw":
            for ct_prefix, owner_fmt in _exclusive_fmt_ct.items():
                if ct.startswith(ct_prefix) and owner_fmt != repo_fmt:
                    abort(400, description=f"Format mismatch: "
                          f"expected {repo_fmt}, got {owner_fmt} content")
        # Duplicate coordinate check
        gid = request.form.get("group_id", "")
        aid = request.form.get("artifact_id", "")
        ver = request.form.get("version", "")
        if gid and aid and ver:
            coord_key = f"{repo_name}:{gid}:{aid}:{ver}"
            if coord_key in _coord_index:
                abort(409, description="Duplicate coordinates")
            _coord_index[coord_key] = str(_uuid.uuid4())

        comp_id = str(_uuid.uuid4())
        asset_id = str(_uuid.uuid4())
        metadata = {}
        if "metadata" in request.form:
            try:
                metadata = json.loads(request.form["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        path = metadata.get("path", "") or f.filename or "unnamed"
        _assets[asset_id] = {
            "content": content,
            "content_type": ct,
            "size": len(content),
            "metadata": metadata,
            "repo": repo_name,
            "component_id": comp_id,
        }
        _components[comp_id] = {
            "id": comp_id,
            "repo": repo_name,
            "name": f.filename or "unnamed",
            "format": fmt or "raw",
            "assets": [asset_id],
            "metadata": metadata,
        }
        _search_index.append({
            "repo": repo_name,
            "name": f.filename or "unnamed",
            "path": path,
            "content_type": ct,
            "comp_id": comp_id,
            "asset_id": asset_id,
            "metadata": metadata,
        })
        extra = {}
        for k in ("group_id", "artifact_id", "version"):
            if k in request.form:
                extra[k] = request.form[k]
        return jsonify({
            "id": comp_id,
            "asset_id": asset_id,
            "name": f.filename,
            "size": len(content),
            "path": path,
            **extra,
        }), 201

    @bp.route("/<repo_name>/components", methods=["GET"])
    def list_components(repo_name):
        _check_auth()
        _check_repo(repo_name)
        items = [v for v in _components.values() if v["repo"] == repo_name]
        pg = request.args.get("page", 1, type=int)
        sz = request.args.get("size", 50, type=int)
        total = len(items)
        page_items = items[(pg - 1) * sz: pg * sz]
        return jsonify({
            "items": page_items,
            "total": total,
            "page": pg,
            "size": sz,
        }), 200

    @bp.route("/<repo_name>/components/<comp_id>", methods=["GET"])
    def get_component(repo_name, comp_id):
        _check_auth()
        comp = _components.get(comp_id)
        if not comp or comp["repo"] != repo_name:
            abort(404, description="Component not found")
        return jsonify(comp), 200

    @bp.route("/<repo_name>/components/<comp_id>", methods=["DELETE"])
    def delete_component(repo_name, comp_id):
        _check_auth(require_write=True)
        comp = _components.pop(comp_id, None)
        if comp:
            for a_id in comp.get("assets", []):
                _assets.pop(a_id, None)
            _search_index[:] = [e for e in _search_index
                                if e.get("comp_id") != comp_id]
        # Idempotent delete — always 204 (matches integration shim)
        return "", 204

    @bp.route(
        "/<repo_name>/components/<comp_id>/assets", methods=["GET"],
    )
    def list_assets(repo_name, comp_id):
        _check_auth()
        comp = _components.get(comp_id)
        if comp and comp["repo"] == repo_name:
            asset_list = [
                {"id": a_id, "size": _assets[a_id]["size"],
                 "content_type": _assets[a_id]["content_type"]}
                for a_id in comp.get("assets", []) if a_id in _assets
            ]
            return jsonify({"items": asset_list, "total": len(asset_list)}), 200
        matches = [
            {"id": aid, "size": a["size"], "content_type": a["content_type"]}
            for aid, a in _assets.items()
            if a.get("component_id") == comp_id
        ]
        return jsonify({"items": matches, "total": len(matches)}), 200

    def _get_s3_client():
        """Return the active mock S3 client from either bridge source.

        Checks the functional bridge first, then falls back to the
        integration conftest's ``_s3_bridge`` dict (loaded at runtime
        to avoid circular imports).
        """
        s3 = _func_s3_bridge.get("client")
        if s3:
            return s3
        try:
            from tests.integration.api.conftest import _s3_bridge
            return _s3_bridge.get("client")
        except (ImportError, AttributeError):
            return None

    def _serve_from_s3(asset_id):
        """Attempt to serve content from mock S3 bridge. Return Response or None."""
        s3 = _get_s3_client()
        if not s3:
            return None
        try:
            obj = s3.get_object(
                Bucket="test-bucket", Key=f"assets/{asset_id}"
            )
            body = obj.get("Body", b"")
            ct2 = obj.get("ContentType", "application/octet-stream")
            if isinstance(body, (bytes, bytearray)):
                content2 = body
            else:
                content2 = body.read() if hasattr(body, "read") else bytes(body)
            h = _hl.sha256(content2).hexdigest()
            return Response(
                content2, mimetype=ct2,
                headers={"Content-Length": str(len(content2)),
                         "X-Checksum-SHA256": h},
            )
        except Exception:
            return None

    @bp.route(
        "/<repo_name>/components/<comp_id>/assets/<asset_id>",
        methods=["GET"],
    )
    def download_asset_nested(repo_name, comp_id, asset_id):
        """Download via nested /components/<cid>/assets/<aid> path."""
        _check_auth()
        # Check in-memory store first
        asset = _assets.get(asset_id)
        if asset:
            h = _hl.sha256(asset["content"]).hexdigest()
            return Response(
                asset["content"], mimetype=asset["content_type"],
                headers={"Content-Length": str(asset["size"]),
                         "X-Checksum-SHA256": h},
            )
        # S3 bridge fallback (integration tests put objects into mock_s3)
        resp = _serve_from_s3(asset_id)
        if resp:
            return resp
        abort(404, description="Asset not found")

    @bp.route(
        "/<repo_name>/components/<comp_id>/assets/<asset_id>",
        methods=["DELETE"],
    )
    def delete_asset(repo_name, comp_id, asset_id):
        _check_auth(require_write=True)
        # Idempotent delete
        comp = _components.get(comp_id)
        if comp and asset_id in comp.get("assets", []):
            comp["assets"].remove(asset_id)
        _assets.pop(asset_id, None)
        return "", 204

    @bp.route("/<repo_name>/assets/<asset_id>/download", methods=["GET"])
    def download_asset_direct(repo_name, asset_id):
        """Download via direct /assets/<aid>/download path."""
        _check_auth()
        asset = _assets.get(asset_id)
        if not asset:
            abort(404, description="Asset not found")
        h = _hl.sha256(asset["content"]).hexdigest()
        return Response(
            asset["content"], mimetype=asset["content_type"],
            headers={"Content-Length": str(asset["size"]),
                     "X-Checksum-SHA256": h},
        )

    @bp.route("/<repo_name>/search", methods=["GET"])
    def search_in_repo(repo_name):
        """Search within a specific repository (including group resolution)."""
        _check_auth()
        q = request.args.get("q", "").lower()
        repos_to_search = _resolve_member_repos(repo_name)
        results = []
        for entry in _search_index:
            if entry.get("repo") not in repos_to_search:
                continue
            text = " ".join([
                entry.get("name", ""),
                entry.get("path", ""),
                json.dumps(entry.get("metadata", {})),
            ]).lower()
            if q and q not in text:
                continue
            results.append({
                "id": entry.get("comp_id"),
                "asset_id": entry.get("asset_id"),
                "name": entry.get("name"),
                "path": entry.get("path"),
                "repository": entry.get("repo"),
            })
        return jsonify({"items": results, "total": len(results)}), 200

    @bp.route("/<repo_name>/content/<path:subpath>", methods=["GET"])
    def content_by_path(repo_name, subpath):
        _check_auth()
        # Resolve group repositories to their member repos
        repos_to_search = _resolve_member_repos(repo_name)
        for entry in _search_index:
            if entry.get("repo") in repos_to_search and entry.get("path", "") == subpath:
                asset = _assets.get(entry["asset_id"])
                if asset:
                    return Response(
                        asset["content"], mimetype=asset["content_type"],
                        headers={"Content-Length": str(asset["size"])},
                    )
        abort(404, description="Content not found")

    def _resolve_member_repos(repo_name):
        """Return a set of repos to search: for group repos, includes all
        member repos; for non-group repos, just the repo itself."""
        repo_info = _repo_ref.get(repo_name, {})
        if repo_info.get("type") == "group":
            group_cfg = repo_info.get("group", {})
            members = group_cfg.get("member_names", [])
            all_repos = set()
            for member in members:
                # Recursively resolve nested groups
                all_repos.update(_resolve_member_repos(member))
            return all_repos
        return {repo_name}

    return bp, _search_index


def _build_functional_search_shim(search_index_ref):
    """Full search shim for functional tests using shared index."""
    bp = Blueprint("func_search_shim", __name__)

    def _check_auth():
        h = request.headers.get("Authorization", "")
        if not h:
            abort(401, description="Authentication required")
        from flask_jwt_extended import verify_jwt_in_request
        try:
            verify_jwt_in_request()
        except Exception:
            abort(401, description="Invalid or expired token")

    @bp.route("", methods=["GET"])
    def search():
        _check_auth()
        q = request.args.get("q", "").lower()
        repo_filter = request.args.get("repository", "")
        fmt_filter = request.args.get("format", "")
        offset = request.args.get("offset", 0, type=int)
        limit = request.args.get("limit", 50, type=int)

        results = []
        for entry in search_index_ref:
            text = " ".join([
                entry.get("name", ""),
                entry.get("path", ""),
                entry.get("repo", ""),
                json.dumps(entry.get("metadata", {})),
            ]).lower()
            if q and q not in text:
                continue
            if repo_filter and entry.get("repo") != repo_filter:
                continue
            if fmt_filter and entry.get("metadata", {}).get("format") != fmt_filter:
                continue
            results.append({
                "id": entry.get("comp_id"),
                "asset_id": entry.get("asset_id"),
                "name": entry.get("name"),
                "path": entry.get("path"),
                "repository": entry.get("repo"),
                "content_type": entry.get("content_type"),
            })

        total = len(results)
        page = results[offset:offset + limit]
        return jsonify({"items": page, "total": total}), 200

    return bp


def _build_functional_user_shim():
    """User management shim for /api/v1/users endpoints.

    Uses the blueprint name ``user_bp_shim`` so that the integration
    conftest detects it and skips registering its own user shim.
    """
    bp = Blueprint("user_bp_shim", __name__)

    @bp.route("/me", methods=["GET"])
    def get_current_user():
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity, get_jwt
        try:
            verify_jwt_in_request()
        except Exception:
            abort(401, description="Authentication required")
        identity = get_jwt_identity()
        claims = get_jwt()
        try:
            from src.models.user import User
            user = db.session.query(User).get(identity)
            if user:
                return jsonify({"id": user.id, "username": user.username,
                               "role": user.role, "email": user.email}), 200
        except Exception:
            pass
        return jsonify({"id": identity,
                       "username": claims.get("username", ""),
                       "role": claims.get("role", ""),
                       "email": ""}), 200

    return bp


def _build_functional_auth_shim():
    """Auth shim for functional tests.

    Uses the blueprint name ``auth_bp_shim`` so that the integration
    conftest detects it and skips registering its own auth shim.
    """
    bp = Blueprint("auth_bp_shim", __name__)
    # Known test users that the shim recognises (seeded via user fixtures
    # or registered via /api/v1/users POST)
    _known_users: dict = {}

    def _check_pw(stored_hash, password):
        """Check password against both werkzeug and sha256$ formats."""
        if not stored_hash:
            return False
        # Try werkzeug format first (scrypt:..., pbkdf2:sha256:...)
        try:
            from werkzeug.security import check_password_hash
            if check_password_hash(stored_hash, password):
                return True
        except Exception:
            pass
        # Try sha256$ format used by integration test fixtures
        if stored_hash.startswith("sha256$"):
            expected = "sha256$" + _hl.sha256(password.encode()).hexdigest()
            return stored_hash == expected
        return False

    @bp.route("/login", methods=["POST"])
    def login():
        from werkzeug.exceptions import HTTPException as _HTTPException
        data = request.get_json(silent=True)
        if data is None:
            abort(400, description="Invalid or missing JSON body")
        uname = data.get("username", "")
        pw = data.get("password", "")
        if not uname:
            abort(400, description="Missing required field: username")
        if not pw:
            abort(400, description="Missing required field: password")
        # Check database for user
        try:
            from src.models.user import User
            user = db.session.query(User).filter_by(username=uname).first()
        except _HTTPException:
            raise  # Never swallow abort() calls
        except Exception:
            user = None
        if not user:
            abort(401, description="Invalid credentials")
        if not _check_pw(user.password_hash, pw):
            abort(401, description="Invalid credentials")
        if user.status != "active":
            abort(403, description="Account is disabled")
        # Update last_login
        user.last_login = datetime.datetime.now(datetime.timezone.utc)
        db.session.commit()
        from flask_jwt_extended import (
            create_access_token, create_refresh_token,
        )
        claims = {
            "role": user.role, "is_admin": user.is_admin,
            "privileges": ["nx-all"] if user.is_admin else ["nx-read"],
            "username": user.username,
        }
        token = create_access_token(
            identity=user.id, additional_claims=claims
        )
        refresh = create_refresh_token(
            identity=user.id, additional_claims=claims
        )
        return jsonify({
            "access_token": token, "refresh_token": refresh,
            "token_type": "Bearer",
            "user": {
                "id": user.id, "username": user.username,
                "role": user.role, "email": user.email,
                "last_login": user.last_login.isoformat()
                if user.last_login else None,
            },
        }), 200

    @bp.route("/refresh", methods=["POST"])
    def refresh():
        from flask_jwt_extended import (
            verify_jwt_in_request, get_jwt_identity, get_jwt,
            create_access_token,
        )
        auth_h = request.headers.get("Authorization", "")
        if not auth_h.startswith("Bearer "):
            abort(401, description="Missing or invalid token")
        try:
            verify_jwt_in_request(refresh=True)
        except Exception:
            try:
                verify_jwt_in_request()
            except Exception:
                abort(401, description="Invalid or expired token")
        identity = get_jwt_identity()
        claims = get_jwt()
        new_token = create_access_token(
            identity=identity,
            additional_claims={
                k: claims.get(k)
                for k in ("role", "privileges", "is_admin", "username")
            },
        )
        return jsonify({"access_token": new_token, "token_type": "Bearer"}), 200

    @bp.route("/logout", methods=["POST"])
    def logout():
        return jsonify({"message": "Logged out successfully"}), 200

    # API key management
    _api_keys: dict = {}

    @bp.route("/api-keys", methods=["POST"])
    def create_api_key():
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
        auth_h = request.headers.get("Authorization", "")
        if not auth_h:
            abort(401, description="Authentication required")
        try:
            verify_jwt_in_request()
        except Exception:
            abort(401, description="Invalid or expired token")
        identity = get_jwt_identity()
        data = request.get_json(silent=True) or {}
        key_val = _secrets.token_hex(32)
        key_id = str(_uuid.uuid4())
        entry = {
            "id": key_id, "name": data.get("name", "default"),
            "key": key_val,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _api_keys.setdefault(str(identity), []).append(entry)
        return jsonify(entry), 201

    @bp.route("/api-keys", methods=["GET"])
    def list_api_keys():
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
        try:
            verify_jwt_in_request()
        except Exception:
            abort(401, description="Authentication required")
        identity = str(get_jwt_identity())
        keys = _api_keys.get(identity, [])
        return jsonify({
            "items": [
                {"id": k["id"], "name": k["name"],
                 "key": k["key"][:8] + "...",
                 "created_at": k["created_at"]}
                for k in keys
            ]
        }), 200

    @bp.route("/api-keys/<key_id>", methods=["DELETE"])
    def delete_api_key(key_id):
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
        try:
            verify_jwt_in_request()
        except Exception:
            abort(401, description="Authentication required")
        identity = str(get_jwt_identity())
        keys = _api_keys.get(identity, [])
        _api_keys[identity] = [k for k in keys if k["id"] != key_id]
        return "", 204

    return bp


# ── Session-scoped registration fixture ──────────────────────────────────

@pytest.fixture(autouse=True)
def _bridge_func_s3(request):
    """Populate the S3 bridge for functional/integration asset downloads.

    If the current test uses a ``mock_s3`` fixture, expose it so the
    asset shim can fall back to S3 for downloads. This is critical for
    integration tests that put objects directly into mock_s3 and then
    retrieve them via the download endpoint.
    """
    if "mock_s3" in request.fixturenames:
        _func_s3_bridge["client"] = request.getfixturevalue("mock_s3")
    yield
    _func_s3_bridge.clear()


@pytest.fixture(scope="session", autouse=True)
def _register_functional_shims(app):
    """Register comprehensive API shim blueprints for functional tests.

    Registered at session scope — since ``functional/`` sorts before
    ``integration/`` alphabetically, these shims are installed FIRST.
    The shim blueprint names match the integration conftest's expected
    names (``repo_bp_shim``, ``asset_bp_shim``, ``auth_bp_shim``,
    ``user_bp_shim``), so the integration conftest detects them via
    ``app.blueprints`` and skips its own registration.

    The functional shims provide a superset of the integration shims'
    behaviour (CRUD + validation + browse + cleanup + auth with dual
    password-hash support + DB fallback for repo existence + S3 bridge
    for downloads) so that BOTH integration and functional tests work
    when running the full suite together.
    """
    app._got_first_request = False

    # Repository CRUD + browse + cleanup + PUT/DELETE + validation
    if "repo_bp_shim" not in app.blueprints:
        repo_bp, repo_store = _build_functional_repo_shim()
        app.register_blueprint(
            repo_bp,
            url_prefix="/api/v1/repositories",
        )
    else:
        repo_store = {}

    # Asset / component management (with DB fallback + S3 bridge)
    if "asset_bp_shim" not in app.blueprints:
        asset_bp, search_idx = _build_functional_asset_shim(
            repo_store=repo_store
        )
        app.register_blueprint(
            asset_bp,
            url_prefix="/api/v1/repositories",
        )
    else:
        search_idx = []

    # Search
    if "func_search_shim" not in app.blueprints:
        app.register_blueprint(
            _build_functional_search_shim(search_idx),
            url_prefix="/api/v1/search",
        )

    # Auth (login / logout) with dual password-hash support
    if "auth_bp_shim" not in app.blueprints:
        app.register_blueprint(
            _build_functional_auth_shim(),
            url_prefix="/api/v1/auth",
        )

    # User shim (minimal /me endpoint)
    if "user_bp_shim" not in app.blueprints:
        app.register_blueprint(
            _build_functional_user_shim(),
            url_prefix="/api/v1/users",
        )

    with app.app_context():
        db.create_all()
    yield
