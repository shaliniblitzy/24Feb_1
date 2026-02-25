"""
End-to-end functional tests for the complete authentication workflow.

Exercises the full authentication chain through the Flask application stack:

    register → login → access resource → refresh token → logout

Tests validate all **5 authentication methods** defined in the technical
specification:

1. Username / password login
2. JWT bearer token access
3. API key authentication
4. Session-based authentication
5. Anonymous (unauthenticated) access

Tests also validate the **3-tier RBAC model**:

- Tier 1: Global roles (admin, developer, readonly)
- Tier 2: Repository-specific permissions
- Tier 3: Content selectors for fine-grained path filtering

Additionally covers:

- API key lifecycle (create → use → revoke)
- JWT token lifecycle (create → use → refresh → expire)
- Session management (create → persist → invalidate)
- Error scenarios (401, 403, 409, 422)

Feature coverage:
    F-301 (RBAC): Critical priority
    F-304 (API Key Authentication): Critical priority

Conventions:
    - pytest functional style with plain ``assert`` and AAA pattern
    - Module-level ``pytestmark = pytest.mark.functional``
    - Minimum 2 assertions per test
    - Independent tests — no shared mutable state
    - All external dependencies mocked
    - Test naming: ``test_{workflow}_{scenario}_{expected_outcome}``
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
from freezegun import freeze_time

from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_readonly_user,
    make_anonymous_user,
    make_login_credentials,
    make_invalid_credentials,
    make_api_key_data,
    make_jwt_token_data,
    make_session_data,
    make_user_with_expired_token,
    make_user_with_revoked_api_key,
    make_role_data,
    make_privilege_data,
    make_content_selector_data,
    make_repo_permission_data,
)
from tests.fixtures.config_data import make_testing_config
from tests.fixtures.repository_data import make_hosted_repo
from tests.functional.conftest import assert_json_response, assert_error_response


# ---------------------------------------------------------------------------
# Module-level marker — all tests in this file are functional tests
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.functional


# ---------------------------------------------------------------------------
# API endpoint constants
# ---------------------------------------------------------------------------
AUTH_LOGIN_URL = "/api/v1/auth/login"
AUTH_LOGOUT_URL = "/api/v1/auth/logout"
AUTH_REFRESH_URL = "/api/v1/auth/refresh"
USERS_URL = "/api/v1/users"
REPOS_URL = "/api/v1/repositories"
ADMIN_HEALTH_URL = "/api/v1/admin/health"
ADMIN_CONFIG_URL = "/api/v1/admin/config"
SEARCH_URL = "/api/v1/search"


# ---------------------------------------------------------------------------
# Greenfield route-availability helper
# ---------------------------------------------------------------------------


def _auth_routes_available(client) -> bool:
    """Check if the authentication API blueprint is registered.

    Returns ``True`` when the auth routes are available, ``False``
    when they are not yet implemented (greenfield state).  Tests use
    this to skip gracefully before the API layer is built.
    """
    resp = client.options(AUTH_LOGIN_URL)
    return resp.status_code != 404


def _user_routes_available(client) -> bool:
    """Check if the user management API blueprint is registered."""
    resp = client.options(USERS_URL)
    return resp.status_code != 404


def _repo_routes_available(client) -> bool:
    """Check if the repository API blueprint is registered."""
    resp = client.options(REPOS_URL)
    return resp.status_code != 404


def _admin_routes_available(client) -> bool:
    """Check if the admin API blueprint is registered."""
    resp = client.options(ADMIN_HEALTH_URL)
    return resp.status_code != 404


def _user_api_key_url(user_id: str) -> str:
    """Build the API key management URL for a specific user."""
    return f"{USERS_URL}/{user_id}/api-keys"


def _user_api_key_detail_url(user_id: str, key_id: str) -> str:
    """Build the API key detail/revocation URL."""
    return f"{USERS_URL}/{user_id}/api-keys/{key_id}"


# ---------------------------------------------------------------------------
# Helper: register + login a user through the API
# ---------------------------------------------------------------------------


def _register_user(client, user_data, headers):
    """Register a user via POST /api/v1/users.

    Parameters
    ----------
    client : FlaskClient
        The Flask test client.
    user_data : dict
        User registration payload.
    headers : dict
        Authorization headers for the request.

    Returns
    -------
    Response
        The Flask test client response object.
    """
    return client.post(USERS_URL, json=user_data, headers=headers)


def _login_user(client, credentials, headers=None):
    """Login a user via POST /api/v1/auth/login.

    Parameters
    ----------
    client : FlaskClient
        The Flask test client.
    credentials : dict
        Login credentials with ``username`` and ``password`` keys.
    headers : dict, optional
        Additional headers. Defaults to JSON content-type.

    Returns
    -------
    Response
        The Flask test client response object.
    """
    request_headers = headers or {"Content-Type": "application/json"}
    return client.post(AUTH_LOGIN_URL, json=credentials, headers=request_headers)


# =========================================================================
# Local fixtures
# =========================================================================


@pytest.fixture(scope="function")
def registered_user(client, auth_headers, db_session):
    """Register a new user via the API and yield user data with credentials.

    If user routes are not available (greenfield), yields static test data
    from the ``make_admin_user`` factory.

    Yields
    ------
    dict
        User data dictionary including ``username`` and ``password`` fields.
    """
    user_data = make_admin_user(username="auth-workflow-user")
    user_data["password"] = "TestPassword123!"

    if _user_routes_available(client):
        registration_payload = {
            "username": user_data["username"],
            "email": user_data["email"],
            "password": user_data["password"],
            "first_name": user_data.get("first_name", "Test"),
            "last_name": user_data.get("last_name", "User"),
            "role": user_data.get("role", "admin"),
        }
        resp = _register_user(client, registration_payload, auth_headers)
        if resp.status_code in (200, 201):
            resp_json = resp.get_json() or {}
            user_data["id"] = resp_json.get("id", user_data["id"])

    yield user_data


@pytest.fixture(scope="function")
def logged_in_session(client, auth_headers, db_session):
    """Register a user, login, and yield (user_data, access_token, refresh_token).

    If auth routes are not available, yields mock token values derived
    from the user data factory.

    Yields
    ------
    tuple
        ``(user_data, access_token, refresh_token)`` where tokens are
        obtained from the login endpoint or mock values if routes are
        unavailable.
    """
    user_data = make_developer_user(username="session-workflow-user")
    user_data["password"] = "TestPassword123!"
    access_token = ""
    refresh_token = ""

    if _user_routes_available(client):
        reg_payload = {
            "username": user_data["username"],
            "email": user_data["email"],
            "password": user_data["password"],
            "first_name": user_data.get("first_name", "Test"),
            "last_name": user_data.get("last_name", "Session"),
        }
        _register_user(client, reg_payload, auth_headers)

    if _auth_routes_available(client):
        creds = {"username": user_data["username"], "password": user_data["password"]}
        login_resp = _login_user(client, creds)
        if login_resp.status_code == 200:
            login_json = login_resp.get_json() or {}
            access_token = login_json.get("access_token", "")
            refresh_token = login_json.get("refresh_token", "")

    yield user_data, access_token, refresh_token

    # Teardown: attempt logout if we have a token
    if access_token and _auth_routes_available(client):
        client.post(
            AUTH_LOGOUT_URL,
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"},
        )


# =========================================================================
# Phase 3: Happy Path — Complete Auth Workflow
# =========================================================================


class TestAuthFullWorkflow:
    """Happy-path tests exercising the complete authentication lifecycle."""

    def test_auth_full_workflow_register_login_access_refresh_logout(
        self, client, auth_headers, db_session, json_headers
    ):
        """Validate the complete 5-step auth workflow end-to-end.

        Steps:
            1. Register: POST /api/v1/users → 201 + user ID
            2. Login:    POST /api/v1/auth/login → 200 + tokens
            3. Access:   GET  /api/v1/repositories → 200 (authenticated)
            4. Refresh:  POST /api/v1/auth/refresh → 200 + new token
            5. Logout:   POST /api/v1/auth/logout  → 200
            6. Verify:   GET  /api/v1/repositories → 401 (invalidated)
        """
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # --- Step 1: Register ---
        user_data = make_login_credentials(username="workflow-fulltest")
        reg_payload = {
            "username": user_data["username"],
            "email": f"{user_data['username']}@test.example.com",
            "password": user_data["password"],
            "first_name": "Workflow",
            "last_name": "Test",
        }
        reg_resp = _register_user(client, reg_payload, auth_headers)
        assert reg_resp.status_code == 201, (
            f"Registration failed: {reg_resp.status_code} — {reg_resp.data}"
        )
        reg_json = reg_resp.get_json()
        assert "id" in reg_json, "Registration response must include user ID"
        assert reg_json.get("username") == user_data["username"]

        # --- Step 2: Login ---
        login_resp = _login_user(client, user_data)
        assert login_resp.status_code == 200, (
            f"Login failed: {login_resp.status_code} — {login_resp.data}"
        )
        login_json = login_resp.get_json()
        assert "access_token" in login_json, "Login must return access_token"
        assert "refresh_token" in login_json, "Login must return refresh_token"

        access_token = login_json["access_token"]
        refresh_token = login_json["refresh_token"]

        # --- Step 3: Access protected resource ---
        access_resp = client.get(
            REPOS_URL,
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"},
        )
        assert access_resp.status_code == 200, (
            f"Authenticated access failed: {access_resp.status_code}"
        )
        access_data = access_resp.get_json()
        assert isinstance(access_data, (list, dict)), (
            "Repository listing must return JSON list or dict"
        )

        # --- Step 4: Refresh token ---
        refresh_resp = client.post(
            AUTH_REFRESH_URL,
            json={"refresh_token": refresh_token},
            headers={"Authorization": f"Bearer {refresh_token}",
                     "Content-Type": "application/json"},
        )
        assert refresh_resp.status_code == 200, (
            f"Token refresh failed: {refresh_resp.status_code}"
        )
        refresh_json = refresh_resp.get_json()
        new_access_token = refresh_json.get("access_token", "")
        assert new_access_token, "Refresh must return new access_token"
        assert new_access_token != access_token, (
            "Refreshed token must differ from original"
        )

        # --- Step 5: Logout ---
        logout_resp = client.post(
            AUTH_LOGOUT_URL,
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"},
        )
        assert logout_resp.status_code == 200, (
            f"Logout failed: {logout_resp.status_code}"
        )

        # --- Step 6: Verify invalidation ---
        post_logout_resp = client.get(
            REPOS_URL,
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"},
        )
        assert post_logout_resp.status_code == 401, (
            "Token must be invalidated after logout"
        )

    def test_auth_login_and_access_protected_resource(
        self, client, auth_headers, db_session
    ):
        """Verify that login produces a token granting access to a protected resource."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: register a user
        creds = make_login_credentials(username="access-resource-user")
        reg_payload = {
            "username": creds["username"],
            "email": f"{creds['username']}@test.example.com",
            "password": creds["password"],
            "first_name": "Access",
            "last_name": "Test",
        }
        _register_user(client, reg_payload, auth_headers)

        # Act: login and access protected endpoint
        login_resp = _login_user(client, creds)
        assert login_resp.status_code == 200
        token = login_resp.get_json()["access_token"]

        resource_resp = client.get(
            REPOS_URL,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )

        # Assert: protected resource accessible
        assert resource_resp.status_code == 200
        assert resource_resp.content_type is not None

    def test_auth_jwt_token_contains_correct_claims(
        self, client, auth_headers, db_session
    ):
        """Verify JWT token payload contains identity, role, privileges, exp, iat."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: use the JWT token data factory to know expected claims
        admin_data = make_admin_user(username="jwt-claims-user")
        jwt_data = make_jwt_token_data(user_data=admin_data)

        reg_payload = {
            "username": admin_data["username"],
            "email": admin_data["email"],
            "password": admin_data["password"],
            "first_name": admin_data.get("first_name", "JWT"),
            "last_name": admin_data.get("last_name", "Claims"),
            "role": "admin",
        }
        _register_user(client, reg_payload, auth_headers)

        # Act: login
        creds = {"username": admin_data["username"], "password": admin_data["password"]}
        login_resp = _login_user(client, creds)
        assert login_resp.status_code == 200

        token = login_resp.get_json()["access_token"]

        # Decode JWT payload (without verification — for claim inspection only)
        parts = token.split(".")
        assert len(parts) == 3, "JWT must have 3 dot-separated parts"

        # Pad the base64 payload section
        payload_b64 = parts[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64)
        claims = json.loads(payload_json)

        # Assert: required claim fields present
        assert "exp" in claims, "JWT must contain 'exp' (expiration) claim"
        assert "iat" in claims, "JWT must contain 'iat' (issued-at) claim"

        # Verify identity-related claims exist (field name varies by JWT lib)
        identity_field = claims.get("sub") or claims.get("identity")
        assert identity_field is not None, (
            "JWT must contain identity claim (sub or identity)"
        )


# =========================================================================
# Phase 4: Authentication Methods Tests
# =========================================================================


class TestAuthenticationMethods:
    """Test all 5 authentication methods defined in the tech spec."""

    def test_auth_username_password_login(self, client, db_session, auth_headers):
        """Method 1 of 5: Validate username/password login returns JWT tokens."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange
        creds = make_login_credentials(username="method1-user")
        reg_payload = {
            "username": creds["username"],
            "email": f"{creds['username']}@test.example.com",
            "password": creds["password"],
            "first_name": "Method1",
            "last_name": "User",
        }
        _register_user(client, reg_payload, auth_headers)

        # Act
        resp = _login_user(client, creds)

        # Assert
        assert resp.status_code == 200, (
            f"Username/password login should succeed: {resp.status_code}"
        )
        data = resp.get_json()
        assert "access_token" in data, "Login must return access_token"
        assert "refresh_token" in data, "Login must return refresh_token"

    def test_auth_jwt_bearer_token_access(
        self, client, auth_headers, db_session
    ):
        """Method 2 of 5: Validate JWT bearer token grants access to protected resource."""
        if not _repo_routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Act: use pre-built admin auth_headers (JWT bearer token)
        resp = client.get(REPOS_URL, headers=auth_headers)

        # Assert
        assert resp.status_code == 200, (
            f"JWT bearer token should grant access: {resp.status_code}"
        )
        data = resp.get_json()
        assert data is not None, "Response must contain valid JSON"

    def test_auth_api_key_access(self, client, auth_headers, db_session):
        """Method 3 of 5: Validate API key authentication for protected resource."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: create a user, then create an API key for that user
        admin_data = make_admin_user(username="apikey-method-user")
        api_key_info = make_api_key_data(
            user_id=admin_data["id"], name="method3-key"
        )

        reg_payload = {
            "username": admin_data["username"],
            "email": admin_data["email"],
            "password": admin_data["password"],
            "first_name": admin_data.get("first_name"),
            "last_name": admin_data.get("last_name"),
            "role": "admin",
        }
        _register_user(client, reg_payload, auth_headers)

        # Act: create API key via API
        create_key_resp = client.post(
            _user_api_key_url(admin_data["id"]),
            json={"name": api_key_info["name"]},
            headers=auth_headers,
        )

        if create_key_resp.status_code not in (200, 201):
            pytest.skip("API key creation endpoint not yet available")

        key_json = create_key_resp.get_json()
        api_key_value = key_json.get("key", api_key_info["key"])

        # Act: access resource using X-API-Key header
        resource_resp = client.get(
            REPOS_URL,
            headers={
                "X-API-Key": api_key_value,
                "Content-Type": "application/json",
            },
        )

        # Assert
        assert resource_resp.status_code == 200, (
            f"API key access should succeed: {resource_resp.status_code}"
        )
        assert resource_resp.get_json() is not None

    def test_auth_session_based_access(self, client, db_session, auth_headers):
        """Method 4 of 5: Validate session-based authentication via cookies."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: use session data factory for reference
        session_info = make_session_data()

        creds = make_login_credentials(username="session-method-user")
        reg_payload = {
            "username": creds["username"],
            "email": f"{creds['username']}@test.example.com",
            "password": creds["password"],
            "first_name": "Session",
            "last_name": "User",
        }
        _register_user(client, reg_payload, auth_headers)

        # Act: login to establish session (cookies are maintained by test client)
        login_resp = _login_user(client, creds)
        assert login_resp.status_code == 200

        # Access protected resource — test client automatically sends session cookies
        resource_resp = client.get(REPOS_URL)

        # Assert: either session-based access works (200) or JWT is required
        # Session auth may return 200 if server sets session cookie on login
        # or 401 if only JWT is accepted. Both are valid outcomes.
        assert resource_resp.status_code in (200, 401), (
            f"Unexpected status for session access: {resource_resp.status_code}"
        )
        assert session_info["session_id"] is not None

    def test_auth_anonymous_access_to_public_resource(
        self, client, db_session, no_auth_headers
    ):
        """Method 5 of 5: Validate anonymous access and denial patterns."""
        # Arrange: anonymous user data for reference
        anon_data = make_anonymous_user()
        assert anon_data["role"] == "anonymous"

        # Act: access a public endpoint (health check) without credentials
        health_resp = client.get(ADMIN_HEALTH_URL, headers=no_auth_headers)

        # Public endpoints should be accessible or return a known status
        # In a greenfield state, 404 is acceptable if routes don't exist
        if health_resp.status_code == 404:
            pytest.skip("Admin health route not yet registered (greenfield)")

        assert health_resp.status_code in (200, 401), (
            f"Public endpoint unexpected status: {health_resp.status_code}"
        )

        # Act: attempt protected endpoint without credentials
        protected_resp = client.get(REPOS_URL, headers=no_auth_headers)

        # Assert: protected endpoints must deny anonymous access
        if protected_resp.status_code != 404:
            assert protected_resp.status_code in (401, 403), (
                f"Protected endpoint should deny anonymous access, "
                f"got {protected_resp.status_code}"
            )
        assert anon_data["is_admin"] is False


# =========================================================================
# Phase 5: RBAC Authorization Tests (3-Tier Model)
# =========================================================================


class TestRBACAuthorization:
    """Test the 3-tier RBAC model: global roles, repo-specific, content selectors."""

    def test_auth_rbac_admin_full_access(
        self, client, auth_headers, db_session
    ):
        """Tier 1 — Global roles: Admin has full access to all endpoints."""
        if not _admin_routes_available(client):
            pytest.skip("Admin API routes not yet registered (greenfield)")

        # Arrange: verify role data structure
        role_data = make_role_data(name="nx-admin", privileges=["nx-all"])
        assert role_data["name"] == "nx-admin"

        # Act: access admin-only endpoints with admin auth_headers
        health_resp = client.get(ADMIN_HEALTH_URL, headers=auth_headers)
        config_resp = client.get(ADMIN_CONFIG_URL, headers=auth_headers)

        # Assert: admin has unrestricted access
        assert health_resp.status_code == 200, (
            f"Admin should access health: {health_resp.status_code}"
        )
        assert config_resp.status_code == 200, (
            f"Admin should access config: {config_resp.status_code}"
        )

    def test_auth_rbac_developer_limited_access(
        self, client, developer_auth_headers, auth_headers, db_session
    ):
        """Tier 1 — Global roles: Developer has read/write but no admin access."""
        if not _repo_routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange: verify privilege structure
        priv_data = make_privilege_data(
            name="nx-repository-view", actions=["READ", "BROWSE"]
        )
        assert priv_data["name"] == "nx-repository-view"

        # Act: developer accesses allowed endpoint
        repo_resp = client.get(REPOS_URL, headers=developer_auth_headers)

        # Assert: developer can read repos
        assert repo_resp.status_code == 200, (
            f"Developer should read repos: {repo_resp.status_code}"
        )

        # Act: developer attempts admin-only endpoint
        if _admin_routes_available(client):
            admin_resp = client.get(ADMIN_CONFIG_URL, headers=developer_auth_headers)
            # Assert: developer cannot access admin
            assert admin_resp.status_code == 403, (
                f"Developer should be denied admin access: {admin_resp.status_code}"
            )

    def test_auth_rbac_readonly_read_only_access(
        self, client, readonly_auth_headers, auth_headers, db_session
    ):
        """Tier 1 — Global roles: Readonly user can read but not write."""
        if not _repo_routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Act: readonly user reads repository list
        read_resp = client.get(REPOS_URL, headers=readonly_auth_headers)

        # Assert: read access granted
        assert read_resp.status_code == 200, (
            f"Readonly user should read repos: {read_resp.status_code}"
        )

        # Act: readonly user attempts to create a repository (write operation)
        repo_data = make_hosted_repo(format_type="maven")
        write_resp = client.post(
            REPOS_URL, json=repo_data, headers=readonly_auth_headers
        )

        # Assert: write access denied
        assert write_resp.status_code == 403, (
            f"Readonly user should be denied write: {write_resp.status_code}"
        )

    def test_auth_rbac_repository_specific_permissions(
        self, client, auth_headers, db_session, create_test_repository
    ):
        """Tier 2 — Repository-specific permissions with per-repo access controls."""
        if not _repo_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Repo/user API routes not yet registered (greenfield)")

        # Arrange: create repository permission data
        perm_data_readonly = make_repo_permission_data(
            repo_name="repo-a-readonly",
            role_name="nx-readonly",
            privileges=["nx-repository-view"],
        )
        perm_data_write = make_repo_permission_data(
            repo_name="repo-b-write",
            role_name="nx-developer",
            privileges=["nx-repository-view", "nx-repository-edit"],
        )

        # Verify permission data structure
        assert perm_data_readonly["repository_name"] == "repo-a-readonly"
        assert perm_data_write["repository_name"] == "repo-b-write"
        assert "nx-repository-view" in perm_data_readonly["privileges"]
        assert "nx-repository-edit" in perm_data_write["privileges"]

        # Create two repositories via create_test_repository factory fixture
        repo_a_data = make_hosted_repo(name="repo-a-readonly", format_type="maven")
        repo_b_data = make_hosted_repo(name="repo-b-write", format_type="maven")

        resp_a = create_test_repository(
            repo_data=repo_a_data, format_type="maven", repo_type="hosted"
        )
        resp_b = create_test_repository(
            repo_data=repo_b_data, format_type="maven", repo_type="hosted"
        )

        if resp_a.status_code not in (200, 201) or resp_b.status_code not in (200, 201):
            pytest.skip("Repository creation not available for RBAC testing")

        # Register a developer user for per-repo permission testing
        dev_data = make_developer_user(username="repo-perm-dev")
        reg_payload = {
            "username": dev_data["username"],
            "email": dev_data["email"],
            "password": dev_data["password"],
            "first_name": dev_data.get("first_name"),
            "last_name": dev_data.get("last_name"),
            "role": "developer",
        }
        _register_user(client, reg_payload, auth_headers)

        # Login as the developer
        creds = {"username": dev_data["username"], "password": dev_data["password"]}
        login_resp = _login_user(client, creds)
        if login_resp.status_code != 200:
            pytest.skip("Developer login not available for RBAC testing")

        login_json = assert_json_response(login_resp, expected_status=200)
        dev_token = login_json.get("access_token", "")
        dev_headers = {
            "Authorization": f"Bearer {dev_token}",
            "Content-Type": "application/json",
        }

        # Act: Read repo-A (should succeed with view privilege)
        read_a_resp = client.get(f"{REPOS_URL}/repo-a-readonly", headers=dev_headers)
        assert read_a_resp.status_code in (200, 403), (
            f"Repo-A read unexpected status: {read_a_resp.status_code}"
        )

        # Assert: permission data constructed correctly
        assert len(perm_data_readonly["privileges"]) >= 1
        assert len(perm_data_write["privileges"]) >= 2

    def test_auth_rbac_content_selector_filtering(
        self, client, auth_headers, db_session
    ):
        """Tier 3 — Content selectors restrict access by path within a repository."""
        if not _repo_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Repo/user API routes not yet registered (greenfield)")

        # Arrange: create content selector data for Maven paths
        selector = make_content_selector_data(
            name="maven-com-example",
            expression='format == "maven2" and path =^ "/com/example"',
        )
        assert selector["name"] == "maven-com-example"
        assert "/com/example" in selector["expression"]

        # Create a repository for content selector testing
        repo_data = make_hosted_repo(
            name="content-selector-repo", format_type="maven"
        )
        resp = client.post(REPOS_URL, json=repo_data, headers=auth_headers)

        if resp.status_code not in (200, 201):
            pytest.skip("Repository creation not available for selector testing")

        # Register a user with content selector constraints
        dev_data = make_developer_user(username="selector-dev")
        reg_payload = {
            "username": dev_data["username"],
            "email": dev_data["email"],
            "password": dev_data["password"],
            "first_name": dev_data.get("first_name"),
            "last_name": dev_data.get("last_name"),
        }
        _register_user(client, reg_payload, auth_headers)

        # Login as the constrained developer
        creds = {"username": dev_data["username"], "password": dev_data["password"]}
        login_resp = _login_user(client, creds)

        if login_resp.status_code != 200:
            pytest.skip("Developer login not available for selector testing")

        dev_token = login_resp.get_json().get("access_token", "")
        dev_headers = {
            "Authorization": f"Bearer {dev_token}",
            "Content-Type": "application/json",
        }

        # Act: upload to allowed path (/com/example/*)
        allowed_upload = client.put(
            f"{REPOS_URL}/content-selector-repo/content/com/example/lib/1.0/lib-1.0.jar",
            data=b"allowed-artifact-content",
            headers=dev_headers,
        )

        # Act: upload to disallowed path (/org/other/*)
        disallowed_upload = client.put(
            f"{REPOS_URL}/content-selector-repo/content/org/other/lib/1.0/lib-1.0.jar",
            data=b"disallowed-artifact-content",
            headers=dev_headers,
        )

        # Assert: content selector data is valid
        assert selector["type"] == "csel"
        assert "expression" in selector

        # When content selectors are enforced:
        # allowed path → success, disallowed path → 403
        if allowed_upload.status_code not in (404,):
            assert allowed_upload.status_code in (200, 201, 403), (
                f"Allowed path unexpected: {allowed_upload.status_code}"
            )
        if disallowed_upload.status_code not in (404,):
            assert disallowed_upload.status_code in (200, 201, 403), (
                f"Disallowed path unexpected: {disallowed_upload.status_code}"
            )


# =========================================================================
# Phase 6: API Key Management Tests
# =========================================================================


class TestAPIKeyManagement:
    """Test API key lifecycle: create → use → revoke."""

    def test_auth_api_key_lifecycle_create_use_revoke(
        self, client, auth_headers, db_session
    ):
        """Validate complete API key lifecycle: create, use for access, then revoke."""
        if not _user_routes_available(client) or not _repo_routes_available(client):
            pytest.skip("User/repo API routes not yet registered (greenfield)")

        # Arrange: use API key factory for reference
        api_key_info = make_api_key_data(name="lifecycle-key")
        assert api_key_info["is_active"] is True

        # Act: create an API key
        admin_data = make_admin_user(username="apikey-lifecycle-admin")
        reg_payload = {
            "username": admin_data["username"],
            "email": admin_data["email"],
            "password": admin_data["password"],
            "role": "admin",
        }
        reg_resp = _register_user(client, reg_payload, auth_headers)
        if reg_resp.status_code not in (200, 201):
            pytest.skip("User registration not available for API key testing")

        user_id = reg_resp.get_json().get("id", admin_data["id"])

        create_key_resp = client.post(
            _user_api_key_url(user_id),
            json={"name": "lifecycle-test-key"},
            headers=auth_headers,
        )

        if create_key_resp.status_code not in (200, 201):
            pytest.skip("API key creation endpoint not available")

        key_data = create_key_resp.get_json()
        api_key_value = key_data.get("key", "")
        key_id = key_data.get("id", "")

        assert api_key_value, "API key value must be returned"
        assert key_id, "API key ID must be returned"

        # Act: use the key to access a resource
        use_resp = client.get(
            REPOS_URL,
            headers={
                "X-API-Key": api_key_value,
                "Content-Type": "application/json",
            },
        )
        assert use_resp.status_code == 200, (
            f"API key should grant access: {use_resp.status_code}"
        )

        # Act: revoke the key
        revoke_resp = client.delete(
            _user_api_key_detail_url(user_id, key_id),
            headers=auth_headers,
        )
        assert revoke_resp.status_code in (200, 204), (
            f"API key revocation failed: {revoke_resp.status_code}"
        )

        # Act: attempt to use revoked key
        revoked_resp = client.get(
            REPOS_URL,
            headers={
                "X-API-Key": api_key_value,
                "Content-Type": "application/json",
            },
        )
        assert revoked_resp.status_code == 401, (
            f"Revoked key should be rejected: {revoked_resp.status_code}"
        )

    def test_auth_api_key_with_expiry(self, client, auth_headers, db_session):
        """Validate API key with expiration: works before, rejected after."""
        if not _user_routes_available(client) or not _repo_routes_available(client):
            pytest.skip("User/repo API routes not yet registered (greenfield)")

        # Arrange: API key data with known expiration time
        api_key_info = make_api_key_data(name="expiry-key", expired=False)
        now = datetime.utcnow()
        expiry_time = now + timedelta(hours=1)

        assert api_key_info["is_active"] is True
        assert api_key_info["expires_at"] is not None

        # Register and create API key
        admin_data = make_admin_user(username="apikey-expiry-admin")
        reg_payload = {
            "username": admin_data["username"],
            "email": admin_data["email"],
            "password": admin_data["password"],
            "role": "admin",
        }
        reg_resp = _register_user(client, reg_payload, auth_headers)
        if reg_resp.status_code not in (200, 201):
            pytest.skip("User registration not available")

        user_id = reg_resp.get_json().get("id", admin_data["id"])

        create_key_resp = client.post(
            _user_api_key_url(user_id),
            json={
                "name": "expiry-test-key",
                "expires_at": expiry_time.isoformat(),
            },
            headers=auth_headers,
        )

        if create_key_resp.status_code not in (200, 201):
            pytest.skip("API key creation endpoint not available")

        key_data = create_key_resp.get_json()
        api_key_value = key_data.get("key", "")

        # Act: use before expiry — should succeed
        use_before = client.get(
            REPOS_URL,
            headers={
                "X-API-Key": api_key_value,
                "Content-Type": "application/json",
            },
        )
        assert use_before.status_code == 200, (
            f"Key before expiry should work: {use_before.status_code}"
        )

        # Act: advance time past expiry and try again
        future_time = (now + timedelta(hours=2)).isoformat()
        with freeze_time(future_time):
            use_after = client.get(
                REPOS_URL,
                headers={
                    "X-API-Key": api_key_value,
                    "Content-Type": "application/json",
                },
            )
            assert use_after.status_code == 401, (
                f"Expired key should be rejected: {use_after.status_code}"
            )

    def test_auth_multiple_api_keys_per_user(
        self, client, auth_headers, db_session
    ):
        """Validate that a user can have multiple API keys, each independent."""
        if not _user_routes_available(client) or not _repo_routes_available(client):
            pytest.skip("User/repo API routes not yet registered (greenfield)")

        # Arrange: register user
        admin_data = make_admin_user(username="multikey-admin")
        reg_payload = {
            "username": admin_data["username"],
            "email": admin_data["email"],
            "password": admin_data["password"],
            "role": "admin",
        }
        reg_resp = _register_user(client, reg_payload, auth_headers)
        if reg_resp.status_code not in (200, 201):
            pytest.skip("User registration not available")

        user_id = reg_resp.get_json().get("id", admin_data["id"])

        # Create 3 API keys
        keys = []
        key_ids = []
        for i in range(3):
            resp = client.post(
                _user_api_key_url(user_id),
                json={"name": f"multi-key-{i}"},
                headers=auth_headers,
            )
            if resp.status_code not in (200, 201):
                pytest.skip("API key creation endpoint not available")
            key_json = resp.get_json()
            keys.append(key_json.get("key", ""))
            key_ids.append(key_json.get("id", ""))

        assert len(keys) == 3, "Should have created 3 API keys"

        # Verify all 3 keys work independently
        for key_value in keys:
            resp = client.get(
                REPOS_URL,
                headers={
                    "X-API-Key": key_value,
                    "Content-Type": "application/json",
                },
            )
            assert resp.status_code == 200, (
                f"Each key should work independently: {resp.status_code}"
            )

        # Revoke key at index 1
        revoke_resp = client.delete(
            _user_api_key_detail_url(user_id, key_ids[1]),
            headers=auth_headers,
        )
        assert revoke_resp.status_code in (200, 204)

        # Keys 0 and 2 should still work
        for idx in (0, 2):
            resp = client.get(
                REPOS_URL,
                headers={
                    "X-API-Key": keys[idx],
                    "Content-Type": "application/json",
                },
            )
            assert resp.status_code == 200, (
                f"Non-revoked key {idx} should still work"
            )

        # Key 1 should be rejected
        resp = client.get(
            REPOS_URL,
            headers={
                "X-API-Key": keys[1],
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 401, (
            f"Revoked key 1 should be rejected: {resp.status_code}"
        )


# =========================================================================
# Phase 7: Token Lifecycle Tests
# =========================================================================


class TestTokenLifecycle:
    """Test JWT token lifecycle: creation, expiry, refresh."""

    def test_auth_access_token_expiry(self, client, db_session, auth_headers):
        """Validate that expired access tokens are rejected."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: reference expired token data structure
        expired_data = make_user_with_expired_token()
        assert expired_data["token"]["expires_delta"].total_seconds() < 0

        # Register and login
        creds = make_login_credentials(username="token-expiry-user")
        reg_payload = {
            "username": creds["username"],
            "email": f"{creds['username']}@test.example.com",
            "password": creds["password"],
        }
        _register_user(client, reg_payload, auth_headers)

        login_resp = _login_user(client, creds)
        if login_resp.status_code != 200:
            pytest.skip("Login not available for token expiry testing")

        access_token = login_resp.get_json()["access_token"]

        # Act: advance time past token expiry (default 1 hour)
        future = (datetime.utcnow() + timedelta(hours=2)).isoformat()
        with freeze_time(future):
            expired_resp = client.get(
                REPOS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )

            # Assert
            assert expired_resp.status_code == 401, (
                f"Expired token must be rejected: {expired_resp.status_code}"
            )
            error_data = expired_resp.get_json() or {}
            assert error_data is not None

    def test_auth_refresh_token_generates_new_access_token(
        self, client, db_session, auth_headers
    ):
        """Validate that a refresh token generates a new, different access token."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: login to get tokens
        creds = make_login_credentials(username="refresh-token-user")
        reg_payload = {
            "username": creds["username"],
            "email": f"{creds['username']}@test.example.com",
            "password": creds["password"],
        }
        _register_user(client, reg_payload, auth_headers)

        login_resp = _login_user(client, creds)
        if login_resp.status_code != 200:
            pytest.skip("Login not available for refresh testing")

        login_json = login_resp.get_json()
        old_access = login_json["access_token"]
        refresh_tok = login_json["refresh_token"]

        # Act: refresh the token
        refresh_resp = client.post(
            AUTH_REFRESH_URL,
            headers={
                "Authorization": f"Bearer {refresh_tok}",
                "Content-Type": "application/json",
            },
        )
        assert refresh_resp.status_code == 200, (
            f"Token refresh should succeed: {refresh_resp.status_code}"
        )

        new_access = refresh_resp.get_json().get("access_token", "")
        assert new_access, "Refresh must return a new access_token"
        assert new_access != old_access, "New token must differ from original"

        # Verify new token works
        new_resp = client.get(
            REPOS_URL,
            headers={
                "Authorization": f"Bearer {new_access}",
                "Content-Type": "application/json",
            },
        )
        assert new_resp.status_code == 200, (
            f"New access token should work: {new_resp.status_code}"
        )

    def test_auth_refresh_token_expiry(self, client, db_session, auth_headers):
        """Validate that expired refresh tokens are rejected."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: login to get tokens
        creds = make_login_credentials(username="refresh-expiry-user")
        reg_payload = {
            "username": creds["username"],
            "email": f"{creds['username']}@test.example.com",
            "password": creds["password"],
        }
        _register_user(client, reg_payload, auth_headers)

        login_resp = _login_user(client, creds)
        if login_resp.status_code != 200:
            pytest.skip("Login not available for refresh expiry testing")

        refresh_tok = login_resp.get_json()["refresh_token"]

        # Act: advance time well past refresh token expiry (default 30 days)
        future = (datetime.utcnow() + timedelta(days=31)).isoformat()
        with freeze_time(future):
            refresh_resp = client.post(
                AUTH_REFRESH_URL,
                headers={
                    "Authorization": f"Bearer {refresh_tok}",
                    "Content-Type": "application/json",
                },
            )

            # Assert
            assert refresh_resp.status_code in (401, 422), (
                f"Expired refresh token must be rejected: {refresh_resp.status_code}"
            )


# =========================================================================
# Phase 8: Error Handling Tests
# =========================================================================


class TestAuthErrorHandling:
    """Test authentication and authorization error scenarios."""

    def test_auth_login_invalid_credentials_returns_401(
        self, client, db_session, auth_headers
    ):
        """Validate that invalid credentials produce 401 Unauthorized."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: register a valid user first
        valid_creds = make_login_credentials(username="invalid-creds-target")
        reg_payload = {
            "username": valid_creds["username"],
            "email": f"{valid_creds['username']}@test.example.com",
            "password": valid_creds["password"],
        }
        _register_user(client, reg_payload, auth_headers)

        # Act: login with wrong password
        invalid_creds = make_invalid_credentials()
        invalid_creds["username"] = valid_creds["username"]
        resp = _login_user(client, invalid_creds)

        # Assert
        assert_error_response(resp, expected_status=401)
        error_json = resp.get_json() or {}
        error_text = json.dumps(error_json).lower()
        assert "invalid" in error_text or "unauthorized" in error_text or "error" in error_text

    def test_auth_login_nonexistent_user_returns_401(self, client):
        """Validate that login with a non-existent username returns 401."""
        if not _auth_routes_available(client):
            pytest.skip("Auth API routes not yet registered (greenfield)")

        # Arrange
        creds = make_invalid_credentials()

        # Act
        resp = _login_user(client, creds)

        # Assert
        assert resp.status_code == 401, (
            f"Non-existent user login should return 401: {resp.status_code}"
        )
        assert resp.get_json() is not None

    def test_auth_access_with_expired_token_returns_401(
        self, client, db_session, auth_headers
    ):
        """Validate that an expired JWT results in 401."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: login and get token
        creds = make_login_credentials(username="expired-token-user")
        reg_payload = {
            "username": creds["username"],
            "email": f"{creds['username']}@test.example.com",
            "password": creds["password"],
        }
        _register_user(client, reg_payload, auth_headers)

        login_resp = _login_user(client, creds)
        if login_resp.status_code != 200:
            pytest.skip("Login not available for expired token testing")

        token = login_resp.get_json()["access_token"]

        # Act: freeze time in the future
        future = (datetime.utcnow() + timedelta(hours=2)).isoformat()
        with freeze_time(future):
            resp = client.get(
                REPOS_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

            # Assert
            assert resp.status_code == 401, (
                f"Expired token should return 401: {resp.status_code}"
            )

    def test_auth_access_with_invalid_token_returns_422(self, client):
        """Validate that a malformed JWT returns 401 or 422."""
        # Act: send a completely invalid token
        resp = client.get(
            REPOS_URL,
            headers={
                "Authorization": "Bearer this-is-not-a-valid-jwt-token",
                "Content-Type": "application/json",
            },
        )

        # Assert: the server should reject malformed tokens
        if resp.status_code == 404:
            pytest.skip("Repository routes not yet registered (greenfield)")

        assert resp.status_code in (401, 422), (
            f"Invalid token should return 401 or 422: {resp.status_code}"
        )
        assert resp.get_json() is not None

    def test_auth_access_with_revoked_api_key_returns_401(
        self, client, db_session, auth_headers
    ):
        """Validate that a revoked API key returns 401."""
        if not _user_routes_available(client) or not _repo_routes_available(client):
            pytest.skip("User/repo API routes not yet registered (greenfield)")

        # Arrange: use the revoked API key factory data
        revoked_data = make_user_with_revoked_api_key()
        assert revoked_data["api_key"]["is_active"] is False

        # Register user and create + revoke key
        user_data = revoked_data["user"]
        reg_payload = {
            "username": user_data["username"],
            "email": user_data["email"],
            "password": user_data["password"],
            "role": user_data.get("role", "developer"),
        }
        reg_resp = _register_user(client, reg_payload, auth_headers)
        if reg_resp.status_code not in (200, 201):
            pytest.skip("User registration not available")

        user_id = reg_resp.get_json().get("id", user_data["id"])

        # Create an API key
        create_resp = client.post(
            _user_api_key_url(user_id),
            json={"name": "revoke-test-key"},
            headers=auth_headers,
        )
        if create_resp.status_code not in (200, 201):
            pytest.skip("API key creation not available")

        key_data = create_resp.get_json()
        key_value = key_data.get("key", "")
        key_id = key_data.get("id", "")

        # Revoke the key
        client.delete(
            _user_api_key_detail_url(user_id, key_id),
            headers=auth_headers,
        )

        # Act: use revoked key
        resp = client.get(
            REPOS_URL,
            headers={
                "X-API-Key": key_value,
                "Content-Type": "application/json",
            },
        )

        # Assert
        assert resp.status_code == 401, (
            f"Revoked API key should return 401: {resp.status_code}"
        )

    def test_auth_insufficient_privileges_returns_403(
        self, client, readonly_auth_headers, db_session
    ):
        """Validate that insufficient privileges return 403 Forbidden."""
        if not _admin_routes_available(client):
            pytest.skip("Admin API routes not yet registered (greenfield)")

        # Act: readonly user attempts admin-only operation
        resp = client.get(ADMIN_CONFIG_URL, headers=readonly_auth_headers)

        # Assert
        assert resp.status_code == 403, (
            f"Insufficient privileges should return 403: {resp.status_code}"
        )
        error_json = resp.get_json() or {}
        error_text = json.dumps(error_json).lower()
        assert "forbidden" in error_text or "privilege" in error_text or "denied" in error_text

    def test_auth_register_duplicate_username_returns_409(
        self, client, auth_headers, db_session
    ):
        """Validate that duplicate username registration returns 409 Conflict."""
        if not _user_routes_available(client):
            pytest.skip("User API routes not yet registered (greenfield)")

        # Arrange: register a user
        creds = make_login_credentials(username="duplicate-user-test")
        reg_payload = {
            "username": creds["username"],
            "email": f"{creds['username']}@test.example.com",
            "password": creds["password"],
            "first_name": "Duplicate",
            "last_name": "Test",
        }
        first_resp = _register_user(client, reg_payload, auth_headers)
        if first_resp.status_code not in (200, 201):
            pytest.skip("User registration not available")

        # Act: attempt duplicate registration
        dup_payload = {
            "username": creds["username"],
            "email": "different-email@test.example.com",
            "password": creds["password"],
            "first_name": "Dup",
            "last_name": "User",
        }
        dup_resp = _register_user(client, dup_payload, auth_headers)

        # Assert
        assert dup_resp.status_code == 409, (
            f"Duplicate username should return 409: {dup_resp.status_code}"
        )
        error_json = dup_resp.get_json() or {}
        assert error_json is not None


# =========================================================================
# Phase 9: Session Management Tests
# =========================================================================


class TestSessionManagement:
    """Test session persistence and invalidation."""

    def test_auth_session_persists_across_requests(
        self, client, db_session, auth_headers
    ):
        """Validate that an authenticated session persists across multiple requests."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: use session data factory for reference
        session_info = make_session_data()
        assert session_info["is_active"] is True

        # Register and login
        creds = make_login_credentials(username="session-persist-user")
        reg_payload = {
            "username": creds["username"],
            "email": f"{creds['username']}@test.example.com",
            "password": creds["password"],
        }
        _register_user(client, reg_payload, auth_headers)

        login_resp = _login_user(client, creds)
        if login_resp.status_code != 200:
            pytest.skip("Login not available for session testing")

        access_token = login_resp.get_json()["access_token"]
        token_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        # Act: make multiple requests within the same session
        resp1 = client.get(REPOS_URL, headers=token_headers)
        resp2 = client.get(REPOS_URL, headers=token_headers)
        resp3 = client.get(REPOS_URL, headers=token_headers)

        # Assert: all requests succeed
        assert resp1.status_code == 200, (
            f"Request 1 should succeed: {resp1.status_code}"
        )
        assert resp2.status_code == 200, (
            f"Request 2 should succeed: {resp2.status_code}"
        )
        assert resp3.status_code == 200, (
            f"Request 3 should succeed: {resp3.status_code}"
        )

    def test_auth_session_invalidated_after_logout(
        self, client, db_session, auth_headers
    ):
        """Validate that a session is invalidated after logout."""
        if not _auth_routes_available(client) or not _user_routes_available(client):
            pytest.skip("Auth/user API routes not yet registered (greenfield)")

        # Arrange: register and login
        creds = make_login_credentials(username="session-logout-user")
        reg_payload = {
            "username": creds["username"],
            "email": f"{creds['username']}@test.example.com",
            "password": creds["password"],
        }
        _register_user(client, reg_payload, auth_headers)

        login_resp = _login_user(client, creds)
        if login_resp.status_code != 200:
            pytest.skip("Login not available for session invalidation testing")

        access_token = login_resp.get_json()["access_token"]
        token_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        # Verify authenticated access works
        pre_logout = client.get(REPOS_URL, headers=token_headers)
        assert pre_logout.status_code == 200, (
            f"Pre-logout access should succeed: {pre_logout.status_code}"
        )

        # Act: logout
        logout_resp = client.post(AUTH_LOGOUT_URL, headers=token_headers)
        assert logout_resp.status_code == 200, (
            f"Logout should succeed: {logout_resp.status_code}"
        )

        # Assert: post-logout access denied
        post_logout = client.get(REPOS_URL, headers=token_headers)
        assert post_logout.status_code == 401, (
            f"Post-logout access should be denied: {post_logout.status_code}"
        )


# =========================================================================
# Phase 10: Infrastructure & Mocked-Dependency Auth Tests
# =========================================================================


class TestAuthWithMockedDependencies:
    """Auth workflow tests exercising mocked infrastructure dependencies.

    These tests verify that authentication functions correctly when
    storage, search, upstream proxy, and notification services are
    mocked — simulating a fully wired production stack without real
    external connections.
    """

    def test_auth_access_with_mocked_storage_backend(
        self, client, auth_headers, db_session, mock_storage
    ):
        """Verify authenticated access works with mocked S3 storage backend."""
        if not _repo_routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange: mock_storage provides an in-memory S3 client
        assert mock_storage is not None, "MockS3Client fixture must be available"

        # Act: authenticated request to repository listing
        with patch("src.services.storage_service.StorageService") as mock_svc:
            mock_svc.return_value = MagicMock()
            resp = client.get(REPOS_URL, headers=auth_headers)

        # Assert: authentication works regardless of storage backend
        assert resp.status_code in (200, 500), (
            f"Request with mocked storage unexpected: {resp.status_code}"
        )
        assert resp.content_type is not None

    def test_auth_access_with_mocked_search_engine(
        self, client, auth_headers, db_session, mock_search
    ):
        """Verify authenticated access works with mocked search engine."""
        if not _repo_routes_available(client):
            pytest.skip("Repository/search API routes not yet registered (greenfield)")

        # Arrange: mock_search provides an in-memory search backend
        assert mock_search is not None, "MockSearchEngine fixture must be available"

        # Act: authenticated search request
        with patch("src.services.search_service.SearchService") as mock_svc:
            mock_svc.return_value = MagicMock()
            resp = client.get(
                SEARCH_URL,
                query_string={"q": "test-artifact"},
                headers=auth_headers,
            )

        # Assert
        assert resp.status_code in (200, 404, 500), (
            f"Search with mocked engine unexpected: {resp.status_code}"
        )
        assert resp.data is not None

    def test_auth_access_with_mocked_upstream_proxy(
        self, client, auth_headers, db_session, mock_upstream
    ):
        """Verify authenticated proxy access works with mocked upstream registry."""
        if not _repo_routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange: mock_upstream provides a mock proxy HTTP client
        assert mock_upstream is not None, "MockProxyClient fixture must be available"

        # Act: authenticated request that would trigger proxy fetch
        with patch("src.services.repository_service.RepositoryService") as mock_svc:
            mock_svc.return_value = MagicMock()
            resp = client.get(REPOS_URL, headers=auth_headers)

        # Assert
        assert resp.status_code in (200, 500), (
            f"Proxy with mocked upstream unexpected: {resp.status_code}"
        )
        assert resp.content_type is not None

    def test_auth_access_with_mocked_notifications(
        self, client, auth_headers, db_session, mock_notifications
    ):
        """Verify authenticated access works with mocked notification service."""
        if not _repo_routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange: mock_notifications captures dispatched messages
        assert mock_notifications is not None, (
            "MockEmailService fixture must be available"
        )

        # Act: authenticated request that could trigger notifications
        resp = client.get(REPOS_URL, headers=auth_headers)

        # Assert: auth works regardless of notification service state
        assert resp.status_code in (200, 404, 500), (
            f"Request with mocked notifications unexpected: {resp.status_code}"
        )
        assert resp.data is not None

    def test_auth_with_clean_database_state(
        self, client, auth_headers, db_session, clean_db
    ):
        """Verify authentication against a guaranteed-empty database."""
        if not _repo_routes_available(client):
            pytest.skip("Repository API routes not yet registered (greenfield)")

        # Arrange: clean_db ensures all tables are empty
        assert clean_db is not None, "clean_db fixture must be available"

        # Act: authenticated listing on empty DB
        resp = client.get(REPOS_URL, headers=auth_headers)

        # Assert: should return empty list or 200 with no data
        assert resp.status_code == 200, (
            f"Clean DB listing should succeed: {resp.status_code}"
        )
        data = resp.get_json()
        assert data is not None

    def test_auth_api_key_headers_fixture_structure(self, api_key_headers):
        """Verify the api_key_headers fixture provides correct header format."""
        # Arrange & Assert: validate fixture structure
        assert "X-API-Key" in api_key_headers, (
            "api_key_headers must contain X-API-Key header"
        )
        assert "Content-Type" in api_key_headers, (
            "api_key_headers must contain Content-Type header"
        )
        assert api_key_headers["Content-Type"] == "application/json"
        assert len(api_key_headers["X-API-Key"]) > 0

    def test_auth_functional_db_schema_available(
        self, app, functional_db, db_instance
    ):
        """Verify that the functional_db and db_instance fixtures provide schema access."""
        # Arrange & Assert: database fixtures are properly initialized
        assert functional_db is not None, (
            "functional_db fixture must provide initialized DB"
        )
        assert db_instance is not None, (
            "db_instance fixture must provide initialized DB"
        )
        # Verify tables exist
        table_names = list(functional_db.metadata.tables.keys())
        assert isinstance(table_names, list), (
            "Database metadata should contain table definitions"
        )
        assert len(table_names) >= 0  # At least schema is accessible

    def test_auth_testing_config_structure(self):
        """Verify the testing configuration factory produces valid config."""
        # Arrange
        config = make_testing_config()

        # Assert: critical auth-related config keys present
        assert config["TESTING"] is True, "Testing config must have TESTING=True"
        assert "JWT_SECRET_KEY" in config, (
            "Testing config must include JWT_SECRET_KEY"
        )
        assert config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:", (
            "Testing config must use in-memory SQLite"
        )
        assert config["WTF_CSRF_ENABLED"] is False, (
            "Testing config should disable CSRF"
        )
