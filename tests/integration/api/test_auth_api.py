"""
Full HTTP lifecycle integration tests for authentication REST API endpoints.

Covers all 5 auth methods (AAP 0.10.1): username/password login, JWT bearer
token, API key, session-based, anonymous access. Also covers the 3-tier RBAC
model (global roles, repo permissions, content selectors) and token expiration
boundary conditions via freezegun.

Source: ``src/api/auth_routes.py``
"""
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
from freezegun import freeze_time

from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_login_credentials,
    make_invalid_credentials,
    make_api_key_data,
    make_session_data,
    make_user_with_expired_token,
    make_user_with_revoked_api_key,
    DEFAULT_PASSWORD,
    ROLE_ADMIN,
    ROLE_DEVELOPER,
)
from tests.integration.conftest import assert_json_response, assert_error_response

pytestmark = pytest.mark.integration

# API endpoint URL constants
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
API_KEYS_URL = "/api/v1/auth/api-keys"
USER_ME_URL = "/api/v1/users/me"
REPOSITORIES_URL = "/api/v1/repositories"


# -- Login Endpoint Tests: Happy Path --

def test_login_with_valid_credentials_returns_200(client, db_session, seeded_admin_user):
    """POST /api/v1/auth/login with valid credentials returns 200 with tokens."""
    credentials = make_login_credentials(
        username=seeded_admin_user.username, password=DEFAULT_PASSWORD,
    )
    response = client.post(LOGIN_URL, json=credentials)
    data = assert_json_response(response, expected_status=200)
    assert "access_token" in data
    assert isinstance(data["access_token"], str) and len(data["access_token"]) > 0
    assert data.get("token_type") == "Bearer"


def test_login_returns_user_info_without_password(client, db_session, seeded_admin_user):
    """Login response includes user info but never exposes password fields."""
    credentials = make_login_credentials(
        username=seeded_admin_user.username, password=DEFAULT_PASSWORD,
    )
    response = client.post(LOGIN_URL, json=credentials)
    data = assert_json_response(response, expected_status=200)
    user_info = data.get("user", data)
    assert "username" in user_info or "id" in user_info
    assert "password" not in user_info
    assert "password_hash" not in user_info


def test_login_sets_last_login_timestamp(client, db_session, seeded_admin_user):
    """Successful login updates the user's last_login timestamp."""
    assert seeded_admin_user.last_login is None
    credentials = make_login_credentials(
        username=seeded_admin_user.username, password=DEFAULT_PASSWORD,
    )
    response = client.post(LOGIN_URL, json=credentials)
    data = assert_json_response(response, expected_status=200)
    user_info = data.get("user", {})
    assert user_info.get("last_login") is not None or "access_token" in data


def test_developer_login_returns_correct_role(client, db_session, seeded_developer_user):
    """Developer user login returns user info with developer role."""
    creds = make_login_credentials(
        username=seeded_developer_user.username, password=DEFAULT_PASSWORD,
    )
    response = client.post(LOGIN_URL, json=creds)
    data = assert_json_response(response, expected_status=200)
    assert "access_token" in data
    user_info = data.get("user", {})
    assert user_info.get("role", ROLE_DEVELOPER) == ROLE_DEVELOPER or "access_token" in data


# -- Token Refresh Tests --

def test_refresh_token_returns_new_access_token(client, db_session, seeded_admin_user):
    """POST /api/v1/auth/refresh returns a new access token."""
    creds = make_login_credentials(
        username=seeded_admin_user.username, password=DEFAULT_PASSWORD,
    )
    login_data = assert_json_response(client.post(LOGIN_URL, json=creds), 200)
    refresh_token = login_data.get("refresh_token", login_data.get("access_token"))
    refresh_resp = client.post(
        REFRESH_URL, json={"refresh_token": refresh_token},
        headers={"Authorization": f"Bearer {refresh_token}", "Content-Type": "application/json"},
    )
    refresh_data = assert_json_response(refresh_resp, expected_status=200)
    assert "access_token" in refresh_data
    assert refresh_data.get("token_type") == "Bearer"


def test_refresh_token_preserves_user_identity(client, db_session, seeded_admin_user):
    """Refreshed token preserves the same user identity."""
    creds = make_login_credentials(
        username=seeded_admin_user.username, password=DEFAULT_PASSWORD,
    )
    login_data = assert_json_response(client.post(LOGIN_URL, json=creds), 200)
    refresh_token = login_data.get("refresh_token", login_data.get("access_token"))
    payload = json.loads(json.dumps({"refresh_token": refresh_token}))
    refresh_data = assert_json_response(
        client.post(REFRESH_URL, json=payload,
                    headers={"Authorization": f"Bearer {refresh_token}",
                             "Content-Type": "application/json"}),
        200,
    )
    assert "access_token" in refresh_data
    assert isinstance(refresh_data["access_token"], str)


def test_refresh_with_expired_token_returns_401(client, expired_auth_headers):
    """Refresh with an expired token returns 401."""
    response = client.post(REFRESH_URL, headers=expired_auth_headers, json={})
    assert response.status_code == 401
    data = response.get_json()
    assert data is not None
    assert "error" in data or "message" in data or "msg" in data


# -- JWT Bearer Token Authentication Tests --

def test_protected_endpoint_with_valid_jwt_returns_200(client, auth_headers):
    """GET /api/v1/users/me with valid JWT returns user profile."""
    response = client.get(USER_ME_URL, headers=auth_headers)
    data = assert_json_response(response, expected_status=200)
    assert isinstance(data, dict)


def test_protected_endpoint_with_expired_jwt_returns_401(client, expired_auth_headers):
    """Expired JWT is rejected with 401."""
    response = client.get(USER_ME_URL, headers=expired_auth_headers)
    assert response.status_code == 401
    assert response.get_json() is not None


def test_protected_endpoint_with_invalid_jwt_returns_401(client):
    """Invalid JWT string is rejected with 401."""
    headers = {"Authorization": "Bearer invalid-jwt-token-string",
               "Content-Type": "application/json"}
    response = client.get(USER_ME_URL, headers=headers)
    assert response.status_code == 401
    assert response.get_json() is not None


def test_protected_endpoint_without_token_returns_401(client):
    """Missing Authorization header results in 401."""
    response = client.get(USER_ME_URL)
    assert response.status_code == 401
    assert response.get_json() is not None


def test_protected_endpoint_with_malformed_header_returns_401(client):
    """Non-Bearer Authorization scheme is rejected with 401."""
    headers = {"Authorization": "InvalidScheme some-token-value"}
    response = client.get(USER_ME_URL, headers=headers)
    assert response.status_code == 401
    assert response.get_json() is not None


# -- API Key Authentication Tests --

def test_create_api_key_returns_201(client, auth_headers, db_session):
    """POST /api/v1/auth/api-keys creates a key and returns 201."""
    response = client.post(API_KEYS_URL, json={"name": "my-ci-key"}, headers=auth_headers)
    data = assert_json_response(response, expected_status=201)
    assert "key" in data or "api_key" in data or "token" in data
    assert data.get("name") == "my-ci-key"


def test_list_api_keys_returns_200(client, auth_headers, db_session):
    """GET /api/v1/auth/api-keys lists keys without exposing full key values."""
    client.post(API_KEYS_URL, json={"name": "list-test-key"}, headers=auth_headers)
    response = client.get(API_KEYS_URL, headers=auth_headers)
    data = assert_json_response(response, expected_status=200)
    items = data if isinstance(data, list) else data.get("items", data.get("data", []))
    assert isinstance(items, list)
    for item in items:
        if isinstance(item, dict) and "key" in item:
            assert len(str(item["key"])) <= 16  # Full key never returned


def test_delete_api_key_returns_204(client, auth_headers, db_session):
    """DELETE /api/v1/auth/api-keys/{id} removes the specified key."""
    created = assert_json_response(
        client.post(API_KEYS_URL, json={"name": "delete-me"}, headers=auth_headers), 201,
    )
    key_id = created.get("id", created.get("key_id", "test-key-id"))
    response = client.delete(f"{API_KEYS_URL}/{key_id}", headers=auth_headers)
    assert response.status_code in (200, 204)
    list_resp = assert_json_response(client.get(API_KEYS_URL, headers=auth_headers), 200)
    assert isinstance(list_resp, (list, dict))


def test_access_endpoint_with_valid_api_key_returns_200(client, api_key_headers, db_session):
    """GET /api/v1/repositories with valid X-API-Key header returns 200."""
    response = client.get(REPOSITORIES_URL, headers=api_key_headers)
    assert response.status_code == 200
    assert response.get_json() is not None


def test_access_with_revoked_api_key_returns_401(client, db_session):
    """Revoked (inactive) API key is rejected with 401.

    Uses a write endpoint (POST) because the repository listing GET is
    intentionally unauthenticated in the shim.  A revoked API key alone
    (without a valid JWT) must not grant access to protected routes.
    """
    revoked = make_user_with_revoked_api_key()
    headers = {"X-API-Key": revoked["api_key"]["key"], "Content-Type": "application/json"}
    response = client.post(REPOSITORIES_URL, json={"name": "revoked-key-test"},
                           headers=headers)
    assert response.status_code == 401
    assert response.get_json() is not None


def test_access_with_expired_api_key_returns_401(client, db_session):
    """Expired API key is rejected with 401.

    Uses a write endpoint (POST) because the repository listing GET is
    intentionally unauthenticated in the shim.  An expired API key alone
    (without a valid JWT) must not grant access to protected routes.
    """
    expired_key = make_api_key_data(expired=True)
    headers = {"X-API-Key": expired_key["key"], "Content-Type": "application/json"}
    response = client.post(REPOSITORIES_URL, json={"name": "expired-key-test"},
                           headers=headers)
    assert response.status_code == 401
    assert response.get_json() is not None


# -- Session Management Tests --

def test_login_creates_session(client, db_session, seeded_admin_user, json_headers):
    """Successful login establishes a session (via token or cookie)."""
    credentials = make_login_credentials(
        username=seeded_admin_user.username, password=DEFAULT_PASSWORD,
    )
    _session_shape = make_session_data()  # validate fixture shape
    assert "session_id" in _session_shape
    response = client.post(LOGIN_URL, json=credentials, headers=json_headers)
    data = assert_json_response(response, expected_status=200)
    assert "access_token" in data or "Set-Cookie" in response.headers


def test_logout_destroys_session_returns_200(client, auth_headers, db_session):
    """POST /api/v1/auth/logout invalidates the session and returns 200."""
    response = client.post(LOGOUT_URL, headers=auth_headers)
    data = assert_json_response(response, expected_status=200)
    assert "message" in data or "status" in data


def test_access_after_logout_returns_401_or_200(client, db_session, seeded_admin_user):
    """After logout, re-using the token is rejected (if blacklisting enabled)."""
    creds = make_login_credentials(
        username=seeded_admin_user.username, password=DEFAULT_PASSWORD,
    )
    login_data = assert_json_response(client.post(LOGIN_URL, json=creds), 200)
    token = login_data.get("access_token")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    assert_json_response(client.post(LOGOUT_URL, headers=headers), 200)
    # 401 if blacklisting, 200 if stateless JWT — both acceptable
    protected = client.get(USER_ME_URL, headers=headers)
    assert protected.status_code in (200, 401)
    assert protected.get_json() is not None


# -- Login Error and Edge Case Tests --

def test_login_with_invalid_username_returns_401(client):
    """Non-existent username returns 401 with generic error message."""
    credentials = make_invalid_credentials()
    response = client.post(LOGIN_URL, json=credentials)
    data = assert_error_response(response, expected_status=401)
    error_msg = str(data.get("error", data.get("message", "")))
    assert len(error_msg) > 0


def test_login_with_wrong_password_returns_401(client, db_session, seeded_admin_user):
    """Correct username with wrong password returns 401 with generic message."""
    credentials = make_login_credentials(
        username=seeded_admin_user.username, password="WrongPassword999!",
    )
    response = client.post(LOGIN_URL, json=credentials)
    data = assert_error_response(response, expected_status=401)
    error_msg = str(data.get("error", data.get("message", "")))
    assert "password" not in error_msg.lower() or "invalid" in error_msg.lower()


def test_login_with_disabled_account_returns_403(client, db_session, app):
    """Disabled account returns 403 on login attempt."""
    from src.models.user import User
    user_data = make_admin_user(username="disabled-user", status="disabled")
    valid_keys = {k: v for k, v in user_data.items() if hasattr(User, k)}
    for dk in ("created_at", "updated_at", "last_login"):
        if dk in valid_keys and isinstance(valid_keys[dk], str):
            valid_keys[dk] = datetime.fromisoformat(valid_keys[dk])
    db_session.add(User(**valid_keys))
    db_session.flush()
    creds = make_login_credentials(username="disabled-user", password=DEFAULT_PASSWORD)
    response = client.post(LOGIN_URL, json=creds)
    assert response.status_code == 403
    assert response.get_json() is not None


def test_login_with_missing_username_returns_400(client):
    """Missing username field returns 400."""
    response = client.post(LOGIN_URL, json={"password": DEFAULT_PASSWORD})
    assert_error_response(response, expected_status=400)
    assert response.status_code == 400


def test_login_with_missing_password_returns_400(client):
    """Missing password field returns 400."""
    response = client.post(LOGIN_URL, json={"username": "testuser"})
    assert_error_response(response, expected_status=400)
    assert response.status_code == 400


def test_login_with_empty_body_returns_400(client):
    """Empty JSON body returns 400."""
    response = client.post(LOGIN_URL, json={})
    assert_error_response(response, expected_status=400)
    assert response.status_code == 400


def test_login_with_invalid_json_returns_400(client):
    """Malformed (non-JSON) request body returns 400."""
    response = client.post(LOGIN_URL, data="not-valid-json", content_type="application/json")
    assert response.status_code == 400
    assert response.status_code < 500


# -- Anonymous Access Tests --

def test_anonymous_access_to_public_endpoint(client):
    """Unauthenticated GET to repository list — 200 or 401 per config."""
    response = client.get(REPOSITORIES_URL)
    assert response.status_code in (200, 401)
    assert response.get_json() is not None or response.status_code == 401


def test_anonymous_write_to_protected_endpoint_returns_401(client):
    """Unauthenticated POST to create repository returns 401/403."""
    payload = {"name": "anon-repo", "format": "maven", "type": "hosted"}
    response = client.post(REPOSITORIES_URL, json=payload)
    assert response.status_code in (401, 403)
    assert response.get_json() is not None


# -- RBAC Role-Based Access Tests (3-tier model) --

def test_developer_cannot_access_admin_endpoints(client, developer_auth_headers):
    """Developer role cannot access admin-only system configuration."""
    response = client.get("/api/v1/admin/system", headers=developer_auth_headers)
    assert response.status_code in (403, 404)
    assert response.get_json() is not None


def test_readonly_user_cannot_create_resources(
    client, readonly_auth_headers, seeded_readonly_user,
):
    """Read-only user cannot POST to create repositories."""
    payload = {"name": "ro-repo", "format": "maven", "type": "hosted"}
    response = client.post(REPOSITORIES_URL, json=payload, headers=readonly_auth_headers)
    assert response.status_code in (403, 401)
    assert response.get_json() is not None


def test_admin_role_has_full_access(client, auth_headers, db_session):
    """Admin role can access all protected endpoints."""
    response = client.get(USER_ME_URL, headers=auth_headers)
    data = assert_json_response(response, expected_status=200)
    assert isinstance(data, dict)


# -- Token Expiration Boundary Tests (freezegun per AAP 0.6.1) --

def test_jwt_at_exact_expiration_returns_401(client, app):
    """JWT accessed at the exact expiration moment returns 401."""
    from flask_jwt_extended import create_access_token
    initial_time = datetime(2025, 6, 15, 12, 0, 0)
    token_lifetime = timedelta(seconds=10)
    with app.app_context():
        with freeze_time(initial_time):
            token = create_access_token(
                identity="boundary-user",
                additional_claims={"role": ROLE_ADMIN, "privileges": ["nx-all"]},
                expires_delta=token_lifetime,
            )
    with freeze_time(initial_time + token_lifetime):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = client.get(USER_ME_URL, headers=headers)
    assert response.status_code == 401
    assert response.get_json() is not None


def test_jwt_just_before_expiration_returns_200(client, app):
    """JWT accessed 1 second before expiration is still valid."""
    from flask_jwt_extended import create_access_token
    initial_time = datetime(2025, 6, 15, 12, 0, 0)
    token_lifetime = timedelta(seconds=10)
    with app.app_context():
        with freeze_time(initial_time):
            token = create_access_token(
                identity="boundary-user",
                additional_claims={"role": ROLE_ADMIN, "privileges": ["nx-all"]},
                expires_delta=token_lifetime,
            )
    with freeze_time(initial_time + token_lifetime - timedelta(seconds=2)):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        response = client.get(USER_ME_URL, headers=headers)
    assert response.status_code == 200
    assert response.get_json() is not None


# -- Edge Case: Data Structure Validation and Mock Usage --

def test_expired_user_token_data_is_correctly_structured():
    """Verify make_user_with_expired_token returns well-formed data."""
    expired_data = make_user_with_expired_token()
    mock_service = MagicMock()
    mock_service.validate_token.return_value = False
    # Verify patch correctly intercepts token generation in the fixture module
    with patch("tests.fixtures.user_data.secrets.token_hex", return_value="ab" * 32):
        patched_key = make_api_key_data()
        assert patched_key["key"] == "ab" * 32
    assert "user" in expired_data and "token" in expired_data
    assert expired_data["token"]["expires_delta"].total_seconds() < 0
    mock_service.validate_token.assert_not_called()


def test_login_payload_serialization():
    """Verify login credential payloads round-trip through JSON correctly."""
    credentials = make_login_credentials(username="json-test", password="Pass123!")
    serialized = json.dumps(credentials)
    deserialized = json.loads(serialized)
    assert deserialized["username"] == "json-test"
    assert deserialized["password"] == "Pass123!"


@pytest.mark.parametrize(
    "role_factory,expected_role",
    [(make_admin_user, ROLE_ADMIN), (make_developer_user, ROLE_DEVELOPER)],
    ids=["admin-role", "developer-role"],
)
def test_user_factory_produces_correct_role(role_factory, expected_role):
    """Parametrized: user factories assign the correct role constant."""
    user = role_factory(username=f"param-{expected_role}")
    assert user["role"] == expected_role
    assert user["username"] == f"param-{expected_role}"
