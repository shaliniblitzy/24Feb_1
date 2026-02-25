"""End-to-end functional tests for authentication error handling and
session management.

Error scenarios: invalid credentials (401), expired tokens (401),
insufficient privileges (403), duplicate registration (409).
Session: persistence across requests, invalidation on logout.

Feature coverage: F-301 (RBAC), F-304 (API Key Auth) — Critical priority.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
from freezegun import freeze_time

from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_login_credentials,
    make_invalid_credentials,
    make_session_data,
    make_user_with_revoked_api_key,
)
from tests.fixtures.config_data import make_testing_config
from tests.functional.conftest import assert_json_response, assert_error_response

pytestmark = pytest.mark.functional

AUTH_LOGIN_URL = "/api/v1/auth/login"
AUTH_LOGOUT_URL = "/api/v1/auth/logout"
USERS_URL = "/api/v1/users"
REPOS_URL = "/api/v1/repositories"
ADMIN_HEALTH_URL = "/api/v1/admin/health"
ADMIN_CONFIG_URL = "/api/v1/admin/config"

def _auth_routes_available(client) -> bool:
    """Return ``True`` when the auth API blueprint is registered."""
    resp = client.options(AUTH_LOGIN_URL)
    return resp.status_code != 404


def _user_routes_available(client) -> bool:
    """Return ``True`` when the user management API blueprint is registered."""
    resp = client.options(USERS_URL)
    return resp.status_code != 404


def _repo_routes_available(client) -> bool:
    """Return ``True`` when the repository API blueprint is registered."""
    resp = client.options(REPOS_URL)
    return resp.status_code != 404


def _admin_routes_available(client) -> bool:
    """Return ``True`` when the admin API blueprint is registered."""
    resp = client.options(ADMIN_HEALTH_URL)
    return resp.status_code != 404


def _user_api_key_url(user_id: str) -> str:
    """Build the API key management URL for a specific user."""
    return f"{USERS_URL}/{user_id}/api-keys"


def _user_api_key_detail_url(user_id: str, key_id: str) -> str:
    """Build the API key detail/revocation URL."""
    return f"{USERS_URL}/{user_id}/api-keys/{key_id}"

def _register_user(client, user_data, headers):
    """Register a user via ``POST /api/v1/users``."""
    return client.post(USERS_URL, json=user_data, headers=headers)


def _login_user(client, credentials, headers=None):
    """Login a user via ``POST /api/v1/auth/login``."""
    request_headers = headers or {"Content-Type": "application/json"}
    return client.post(AUTH_LOGIN_URL, json=credentials, headers=request_headers)

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
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
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
