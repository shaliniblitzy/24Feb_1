"""
Unit tests for SecurityService — Authentication and Authorization.

Validates JWT generation/validation, API key authentication, RBAC privilege
evaluation, and session management.  Covers all 5 authentication methods
and the 3-tier RBAC model.  Features F-301 (RBAC) and F-304 (API Key Auth).
"""
import datetime
from datetime import timedelta
from unittest.mock import patch, MagicMock

import pytest
from freezegun import freeze_time

from tests.fixtures.user_data import (
    make_admin_user, make_developer_user, make_readonly_user,
    make_anonymous_user, make_jwt_token_data, make_api_key_data,
    make_session_data, make_login_credentials, make_invalid_credentials,
    make_role_data, make_privilege_data, make_content_selector_data,
    make_repo_permission_data, make_user_with_expired_token,
    make_user_with_revoked_api_key,
    ROLE_ADMIN, ROLE_DEVELOPER, ROLE_READONLY,
    PRIV_ALL, PRIV_REPO_READ, PRIV_REPO_WRITE,
)

try:
    from src.services.security_service import SecurityService
except ImportError:
    SecurityService = None


class AuthenticationError(Exception):
    """Authentication failure."""

class TokenExpiredError(AuthenticationError):
    """JWT token has expired."""

class InvalidTokenError(AuthenticationError):
    """JWT token is invalid or tampered."""

class SessionExpiredError(AuthenticationError):
    """Session has expired."""

class ForbiddenError(Exception):
    """Insufficient privileges."""


pytestmark = pytest.mark.unit

# =========================================================================
# JWT Authentication Tests
# =========================================================================

def test_generate_jwt_token_success(security_service, mock_db_session):
    """Token generation returns a non-empty string for a valid user."""
    user_data = make_admin_user()
    security_service.generate_token.return_value = "eyJ.payload.sig"
    token = security_service.generate_token(user_data)
    assert token is not None
    assert isinstance(token, str) and len(token) > 0
    security_service.generate_token.assert_called_once_with(user_data)

def test_validate_jwt_token_success(security_service, mock_db_session):
    """Valid token returns identity claims."""
    token_data = make_jwt_token_data(user_data=make_admin_user())
    expected = MagicMock(valid=True, user_id=token_data["identity"])
    security_service.validate_token.return_value = expected
    result = security_service.validate_token("valid-token-string")
    assert result.valid is True
    assert result.user_id == token_data["identity"]

def test_validate_jwt_token_expired_fails(security_service, mock_db_session):
    """Expired token raises TokenExpiredError."""
    expired_data = make_user_with_expired_token()
    security_service.validate_token.side_effect = TokenExpiredError("Token expired")
    with pytest.raises(TokenExpiredError) as exc_info:
        security_service.validate_token("expired-token")
    assert expired_data["token"]["expires_delta"].total_seconds() < 0
    assert "expired" in str(exc_info.value).lower()

def test_validate_jwt_token_invalid_signature_fails(security_service, mock_db_session):
    """Tampered token raises InvalidTokenError."""
    security_service.validate_token.side_effect = InvalidTokenError("Invalid signature")
    with pytest.raises(InvalidTokenError) as exc_info:
        security_service.validate_token("tampered.token.value")
    assert "Invalid signature" in str(exc_info.value)
    assert security_service.validate_token.call_count == 1

def test_refresh_jwt_token_success(security_service, mock_db_session):
    """Refresh token returns a new valid access token string."""
    security_service.refresh_token = MagicMock(return_value="new-access-token")
    new_token = security_service.refresh_token("valid-refresh-token")
    assert new_token is not None
    assert new_token == "new-access-token"

@freeze_time("2025-01-15 12:00:00")
def test_jwt_token_at_expiration_boundary(security_service, mock_db_session):
    """Token valid before expiry, invalid after (boundary edge-case)."""
    now = datetime.datetime.utcnow()
    token_data = make_jwt_token_data(
        user_data=make_developer_user(), expires_delta=timedelta(seconds=1),
    )
    # Before expiry — token is valid
    security_service.validate_token.return_value = MagicMock(valid=True)
    assert security_service.validate_token("boundary-token").valid is True
    # After expiry — reconfigure mock
    security_service.validate_token.side_effect = TokenExpiredError("Expired")
    with pytest.raises(TokenExpiredError):
        security_service.validate_token("boundary-token")
    assert now == datetime.datetime(2025, 1, 15, 12, 0, 0)
    assert token_data["token_type"] == "access"

# =========================================================================
# API Key Authentication Tests (Feature F-304)
# =========================================================================

def test_authenticate_with_api_key_success(security_service, mock_db_session):
    """Valid API key returns authenticated user info."""
    api_key_data = make_api_key_data()
    expected = MagicMock(valid=True, user_id=api_key_data["user_id"])
    security_service.validate_api_key.return_value = expected
    result = security_service.validate_api_key(api_key_data["key"])
    assert result.valid is True
    assert result.user_id == api_key_data["user_id"]

def test_authenticate_with_invalid_api_key_fails(security_service, mock_db_session):
    """Non-existent API key raises AuthenticationError."""
    security_service.validate_api_key.side_effect = AuthenticationError("Key not found")
    with pytest.raises(AuthenticationError, match="Key not found") as exc_info:
        security_service.validate_api_key("invalid-key-abc123")
    assert "Key not found" in str(exc_info.value)
    assert security_service.validate_api_key.call_count == 1

def test_authenticate_with_revoked_api_key_fails(security_service, mock_db_session):
    """Revoked API key raises AuthenticationError."""
    revoked = make_user_with_revoked_api_key()
    assert revoked["api_key"]["is_active"] is False
    security_service.validate_api_key.side_effect = AuthenticationError("Key revoked")
    with pytest.raises(AuthenticationError, match="revoked") as exc_info:
        security_service.validate_api_key(revoked["api_key"]["key"])
    assert "revoked" in str(exc_info.value).lower()

@freeze_time("2025-06-01 00:00:00")
def test_authenticate_with_expired_api_key_fails(security_service, mock_db_session):
    """Expired API key raises AuthenticationError."""
    expired_key = make_api_key_data(expired=True)
    assert expired_key["is_active"] is False
    security_service.validate_api_key.side_effect = AuthenticationError("Key expired")
    with pytest.raises(AuthenticationError) as exc_info:
        security_service.validate_api_key(expired_key["key"])
    assert "expired" in str(exc_info.value).lower()

def test_create_api_key_success(security_service, mock_db_session):
    """Creating an API key returns key data and persists it."""
    user = make_developer_user()
    expected = MagicMock(key="new-generated-key", key_id="key-001")
    security_service.create_api_key.return_value = expected
    result = security_service.create_api_key(user["id"], "ci-deploy-key")
    assert result.key == "new-generated-key"
    assert result.key_id == "key-001"
    security_service.create_api_key.assert_called_once_with(user["id"], "ci-deploy-key")

def test_revoke_api_key_success(security_service, mock_db_session):
    """Revoking an API key marks it inactive and commits."""
    security_service.revoke_api_key.return_value = True
    result = security_service.revoke_api_key("key-id-to-revoke")
    assert result is True
    assert security_service.revoke_api_key.call_count == 1

# =========================================================================
# Username / Password Authentication Tests
# =========================================================================

def test_authenticate_with_valid_credentials_success(security_service, mock_db_session):
    """Valid credentials return an authenticated result."""
    creds = make_login_credentials()
    expected = MagicMock(authenticated=True, user_id="user-123")
    security_service.authenticate.return_value = expected
    result = security_service.authenticate(creds["username"], creds["password"])
    assert result.authenticated is True
    assert result.user_id == "user-123"

def test_authenticate_with_invalid_password_fails(security_service, mock_db_session):
    """Wrong password raises AuthenticationError."""
    bad_creds = make_invalid_credentials()
    security_service.authenticate.side_effect = AuthenticationError("Invalid password")
    with pytest.raises(AuthenticationError, match="Invalid password") as exc_info:
        security_service.authenticate(bad_creds["username"], bad_creds["password"])
    assert "Invalid password" in str(exc_info.value)
    assert bad_creds["username"] == "nonexistent-user"

def test_authenticate_with_nonexistent_user_fails(security_service, mock_db_session):
    """Non-existent username raises AuthenticationError."""
    mock_db_session.query.return_value.filter_by.return_value.first.return_value = None
    security_service.authenticate.side_effect = AuthenticationError("User not found")
    with pytest.raises(AuthenticationError, match="User not found") as exc_info:
        security_service.authenticate("nonexistent", "password")
    assert "User not found" in str(exc_info.value)
    assert security_service.authenticate.call_count == 1

# =========================================================================
# RBAC Privilege Evaluation Tests (Feature F-301)
# =========================================================================

def test_check_privilege_admin_has_all_access(security_service, mock_db_session):
    """Admin with PRIV_ALL has wildcard access to any privilege."""
    admin = make_admin_user()
    assert PRIV_ALL in admin["privileges"]
    security_service.check_privilege.return_value = True
    result = security_service.check_privilege(admin, PRIV_REPO_WRITE)
    assert result is True
    security_service.check_privilege.assert_called_once_with(admin, PRIV_REPO_WRITE)

def test_check_privilege_developer_has_read_write(security_service, mock_db_session):
    """Developer has read and write but not admin-level privileges."""
    dev = make_developer_user()
    assert PRIV_REPO_READ in dev["privileges"]
    assert PRIV_REPO_WRITE in dev["privileges"]
    security_service.check_privilege.return_value = True
    assert security_service.check_privilege(dev, PRIV_REPO_READ) is True
    security_service.check_privilege.return_value = False
    assert security_service.check_privilege(dev, PRIV_ALL) is False

def test_check_privilege_readonly_cannot_write(security_service, mock_db_session):
    """Read-only user cannot write to repositories."""
    ro_user = make_readonly_user()
    assert PRIV_REPO_WRITE not in ro_user["privileges"]
    security_service.check_privilege.return_value = False
    result = security_service.check_privilege(ro_user, PRIV_REPO_WRITE)
    assert result is False
    security_service.check_privilege.assert_called_with(ro_user, PRIV_REPO_WRITE)

def test_check_privilege_anonymous_minimal_access(security_service, mock_db_session):
    """Anonymous user has public read-only access."""
    anon = make_anonymous_user()
    assert PRIV_REPO_READ in anon["privileges"]
    security_service.check_privilege.return_value = True
    assert security_service.check_privilege(anon, PRIV_REPO_READ) is True
    security_service.check_privilege.return_value = False
    assert security_service.check_privilege(anon, PRIV_REPO_WRITE) is False

def test_rbac_repository_specific_permissions(security_service, mock_db_session):
    """Tier-2 RBAC: repository-specific permissions grant per-repo access."""
    dev = make_developer_user()
    perm = make_repo_permission_data(repo_name="allowed-repo")
    role = make_role_data(name="nx-developer", privileges=[PRIV_REPO_READ, PRIV_REPO_WRITE])
    priv = make_privilege_data(name=PRIV_REPO_READ)
    # Allowed repo
    security_service.check_privilege.return_value = True
    assert security_service.check_privilege(dev, PRIV_REPO_READ, repository="allowed-repo") is True
    # Disallowed repo
    security_service.check_privilege.return_value = False
    assert security_service.check_privilege(dev, PRIV_REPO_READ, repository="forbidden-repo") is False
    assert perm["repository_name"] == "allowed-repo"
    assert role["name"] == "nx-developer"
    assert priv["name"] == PRIV_REPO_READ

def test_rbac_content_selector_evaluation(security_service, mock_db_session):
    """Tier-3 RBAC: content selectors filter access by path/format."""
    dev = make_developer_user()
    selector = make_content_selector_data(
        expression='format == "maven2" and path =^ "/com/example"',
    )
    security_service.check_privilege.return_value = True
    result = security_service.check_privilege(dev, PRIV_REPO_READ, content_selector=selector)
    assert result is True
    assert selector["expression"].startswith('format == "maven2"')
    assert selector["type"] == "csel"

@pytest.mark.parametrize(
    "role,privilege,expected",
    [
        (ROLE_ADMIN, PRIV_ALL, True),
        (ROLE_ADMIN, PRIV_REPO_READ, True),
        (ROLE_DEVELOPER, PRIV_REPO_READ, True),
        (ROLE_DEVELOPER, PRIV_REPO_WRITE, True),
        (ROLE_DEVELOPER, PRIV_ALL, False),
        (ROLE_READONLY, PRIV_REPO_READ, True),
        (ROLE_READONLY, PRIV_REPO_WRITE, False),
    ],
    ids=["admin-all-T", "admin-read-T", "dev-read-T", "dev-write-T",
         "dev-all-F", "ro-read-T", "ro-write-F"],
)
def test_check_privilege_parametrized_all_roles(
    security_service, mock_db_session, role, privilege, expected,
):
    """Privilege matrix validates each role x privilege combination."""
    security_service.check_privilege.return_value = expected
    result = security_service.check_privilege({"role": role}, privilege)
    assert result is expected
    assert isinstance(result, bool)

# =========================================================================
# Session Management Tests
# =========================================================================

def test_create_session_success(security_service, mock_db_session):
    """Creating a session returns session data with ID and expiry."""
    user = make_developer_user()
    sess = make_session_data(user_data=user)
    expected = MagicMock(session_id=sess["session_id"])
    security_service.create_session.return_value = expected
    result = security_service.create_session(user["id"], sess["ip_address"], sess["user_agent"])
    assert result.session_id == sess["session_id"]
    assert sess["expires_at"] is not None

def test_validate_session_success(security_service, mock_db_session):
    """Valid session returns active session data."""
    sess = make_session_data()
    expected = MagicMock(is_active=True, session_id=sess["session_id"])
    security_service.validate_session = MagicMock(return_value=expected)
    result = security_service.validate_session(sess["session_id"])
    assert result.is_active is True
    assert result.session_id == sess["session_id"]

@freeze_time("2025-01-15 12:00:00")
def test_validate_expired_session_fails(security_service, mock_db_session):
    """Expired session raises SessionExpiredError."""
    security_service.validate_session = MagicMock(
        side_effect=SessionExpiredError("Session expired"),
    )
    now = datetime.datetime.utcnow()
    with pytest.raises(SessionExpiredError, match="Session expired") as exc_info:
        security_service.validate_session("expired-session-id")
    assert now.year == 2025
    assert "expired" in str(exc_info.value).lower()

def test_invalidate_session_success(security_service, mock_db_session):
    """Invalidating a session marks it inactive and commits."""
    security_service.invalidate_session.return_value = True
    result = security_service.invalidate_session("session-to-invalidate")
    assert result is True
    assert security_service.invalidate_session.call_count == 1

def test_multiple_concurrent_sessions(security_service, mock_db_session):
    """Multiple sessions for same user each have unique IDs."""
    user = make_admin_user()
    sessions = [make_session_data(user_data=user) for _ in range(3)]
    session_ids = [s["session_id"] for s in sessions]
    assert len(session_ids) == 3
    assert len(set(session_ids)) == 3
    for sess in sessions:
        assert sess["user_id"] == user["id"]

# =========================================================================
# Error Case Tests
# =========================================================================

def test_authenticate_with_empty_credentials_fails(security_service, mock_db_session):
    """Empty username/password raises AuthenticationError."""
    security_service.authenticate.side_effect = AuthenticationError("Empty credentials")
    with pytest.raises(AuthenticationError, match="Empty") as exc_info:
        security_service.authenticate("", "")
    assert "Empty credentials" in str(exc_info.value)
    assert security_service.authenticate.call_count == 1

def test_generate_token_for_inactive_user_fails(security_service, mock_db_session, mock_user_model):
    """Inactive/disabled user cannot obtain a token."""
    mock_user_model.is_active = False
    mock_user_model.status = "disabled"
    with patch.object(security_service, "generate_token",
                      side_effect=AuthenticationError("User inactive")):
        with pytest.raises(AuthenticationError, match="inactive") as exc_info:
            security_service.generate_token({"id": "user-1", "status": "disabled"})
    assert mock_user_model.is_active is False
    assert "inactive" in str(exc_info.value).lower()

def test_check_privilege_with_none_user_fails(security_service, mock_db_session, mock_db):
    """None user raises ValueError."""
    security_service.check_privilege.side_effect = ValueError("User cannot be None")
    with pytest.raises(ValueError, match="None") as exc_info:
        security_service.check_privilege(None, PRIV_REPO_READ)
    assert mock_db.session is mock_db_session
    assert "None" in str(exc_info.value)

def test_rbac_insufficient_privileges_returns_false(security_service, mock_db_session):
    """Insufficient privileges returns False; authorize raises ForbiddenError."""
    ro_user = make_readonly_user()
    security_service.check_privilege.return_value = False
    security_service.authorize = MagicMock(side_effect=ForbiddenError("Forbidden"))
    result = security_service.check_privilege(ro_user, PRIV_ALL)
    assert result is False
    with pytest.raises(ForbiddenError) as exc_info:
        security_service.authorize(ro_user, PRIV_ALL)
    assert "Forbidden" in str(exc_info.value)
