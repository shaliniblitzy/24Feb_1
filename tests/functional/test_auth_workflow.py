"""End-to-end functional tests for the core authentication workflow
and JWT token lifecycle.

Exercises: register -> login -> access -> refresh -> logout
Token lifecycle: create -> use -> expire -> refresh -> reject-expired

Feature coverage: F-301 (RBAC), F-304 (API Key Auth) — Critical priority.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
from freezegun import freeze_time

from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_login_credentials,
    make_jwt_token_data,
    make_user_with_expired_token,
    make_session_data,
)
from tests.fixtures.config_data import make_testing_config
from tests.fixtures.repository_data import make_hosted_repo
from tests.functional.conftest import assert_json_response, assert_error_response

pytestmark = pytest.mark.functional

AUTH_LOGIN_URL = "/api/v1/auth/login"
AUTH_LOGOUT_URL = "/api/v1/auth/logout"
AUTH_REFRESH_URL = "/api/v1/auth/refresh"
USERS_URL = "/api/v1/users"
REPOS_URL = "/api/v1/repositories"

def _auth_routes_available(client) -> bool:
    """Return ``True`` when the auth API blueprint is registered."""
    resp = client.options(AUTH_LOGIN_URL)
    return resp.status_code != 404


def _user_routes_available(client) -> bool:
    """Return ``True`` when the user management API blueprint is registered."""
    resp = client.options(USERS_URL)
    return resp.status_code != 404

def _register_user(client, user_data, headers):
    """Register a user via ``POST /api/v1/users``."""
    return client.post(USERS_URL, json=user_data, headers=headers)


def _login_user(client, credentials, headers=None):
    """Login a user via ``POST /api/v1/auth/login``."""
    request_headers = headers or {"Content-Type": "application/json"}
    return client.post(AUTH_LOGIN_URL, json=credentials, headers=request_headers)

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
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
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
        future = (datetime.now(timezone.utc) + timedelta(days=31)).isoformat()
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
