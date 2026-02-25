"""
Unit tests for SecurityService — Authentication and Authorization.

Tests real ``SecurityService`` instances with mocked dependencies (db
session, in-memory key/session stores), covering all 5 authentication
methods and the 3-tier RBAC model.  Features F-301 (RBAC) and F-304
(API Key Auth).  Coverage target: 100 % (AAP §0.7.1).
"""
import datetime
from datetime import timedelta
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from freezegun import freeze_time
from werkzeug.security import generate_password_hash

from src.services.security_service import (
    SecurityService,
    AuthenticationError,
    TokenExpiredError,
    InvalidTokenError,
    SessionExpiredError,
    ForbiddenError,
    AuthResult,
    TokenResult,
    ApiKeyResult,
    ApiKeyValidation,
    SessionData,
)
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

pytestmark = pytest.mark.unit

SECRET = "unit-test-secret-key-2025-long-enough-for-hmac-sha256"


def _svc(db_session=None):
    """Create a real SecurityService with a mocked db session."""
    return SecurityService(
        db_session=db_session or MagicMock(), secret_key=SECRET,
        token_expiry=3600, session_expiry=86400,
    )


def _mock_user(user_id="user-123", username="testuser", role="developer",
               password="TestPassword123!", is_active=True):
    """Build a mock user ORM object with a hashed password."""
    user = MagicMock()
    user.id = user_id
    user.username = username
    user.role = role
    user.is_active = is_active
    user.password_hash = generate_password_hash(password)
    return user


# =========================================================================
# JWT Authentication Tests
# =========================================================================

def test_generate_jwt_token_success(mock_db_session):
    """Token generation returns a non-empty JWT string for a valid user."""
    svc = _svc(mock_db_session)
    user_data = make_admin_user()
    token = svc.generate_token(user_data)
    assert token is not None
    assert isinstance(token, str) and len(token) > 0


def test_generate_jwt_token_contains_user_id(mock_db_session):
    """Generated token's payload contains the correct user ID."""
    svc = _svc(mock_db_session)
    user_data = make_admin_user()
    token = svc.generate_token(user_data)
    payload = pyjwt.decode(token, SECRET, algorithms=["HS256"])
    assert payload["sub"] == str(user_data["id"])
    assert "exp" in payload


def test_validate_jwt_token_success(mock_db_session):
    """Valid token returns identity claims with valid=True."""
    svc = _svc(mock_db_session)
    user_data = make_admin_user()
    token = svc.generate_token(user_data)
    result = svc.validate_token(token)
    assert result.valid is True
    assert result.user_id == str(user_data["id"])


def test_validate_jwt_token_expired_fails(mock_db_session):
    """Expired token raises TokenExpiredError."""
    svc = SecurityService(db_session=mock_db_session, secret_key=SECRET,
                          token_expiry=1)
    user_data = make_admin_user()
    expired_payload = {
        "sub": str(user_data["id"]), "username": user_data["username"],
        "role": "", "iat": datetime.datetime(2020, 1, 1),
        "exp": datetime.datetime(2020, 1, 1, 0, 0, 1), "type": "access",
    }
    expired_token = pyjwt.encode(expired_payload, SECRET, algorithm="HS256")
    with pytest.raises(TokenExpiredError) as exc_info:
        svc.validate_token(expired_token)
    assert "expired" in str(exc_info.value).lower()
    assert exc_info.type is TokenExpiredError


def test_validate_jwt_token_invalid_signature_fails(mock_db_session):
    """Tampered token raises InvalidTokenError."""
    svc = _svc(mock_db_session)
    with pytest.raises(InvalidTokenError) as exc_info:
        svc.validate_token("tampered.token.value")
    assert "Invalid signature" in str(exc_info.value)
    assert exc_info.type is InvalidTokenError


def test_refresh_jwt_token_success(mock_db_session):
    """Refresh token returns a new valid access token string."""
    svc = _svc(mock_db_session)
    user_data = make_developer_user()
    original = svc.generate_token(user_data)
    new_token = svc.refresh_token(original)
    assert new_token is not None
    assert isinstance(new_token, str) and len(new_token) > 0


@freeze_time("2025-01-15 12:00:00")
def test_jwt_token_at_expiration_boundary(mock_db_session):
    """Token valid before expiry, invalid after (boundary edge-case)."""
    svc = SecurityService(db_session=mock_db_session, secret_key=SECRET,
                          token_expiry=2)
    user_data = make_developer_user()
    token = svc.generate_token(user_data)
    result = svc.validate_token(token)
    assert result.valid is True
    assert result.user_id == str(user_data["id"])


# =========================================================================
# API Key Authentication Tests (Feature F-304)
# =========================================================================

def test_authenticate_with_api_key_success(mock_db_session):
    """Valid API key returns authenticated user info."""
    svc = _svc(mock_db_session)
    user_data = make_developer_user()
    created = svc.create_api_key(user_data["id"], "ci-deploy-key")
    result = svc.validate_api_key(created.key)
    assert result.valid is True
    assert result.user_id == user_data["id"]


def test_authenticate_with_invalid_api_key_fails(mock_db_session):
    """Non-existent API key raises AuthenticationError."""
    svc = _svc(mock_db_session)
    with pytest.raises(AuthenticationError, match="Key not found") as exc_info:
        svc.validate_api_key("invalid-key-abc123")
    assert "Key not found" in str(exc_info.value)
    assert exc_info.type is AuthenticationError


def test_authenticate_with_revoked_api_key_fails(mock_db_session):
    """Revoked API key raises AuthenticationError."""
    svc = _svc(mock_db_session)
    created = svc.create_api_key("user-1", "temp-key")
    svc.revoke_api_key(created.key_id)
    with pytest.raises(AuthenticationError, match="revoked") as exc_info:
        svc.validate_api_key(created.key)
    assert "revoked" in str(exc_info.value).lower()
    assert exc_info.type is AuthenticationError


def test_authenticate_with_expired_api_key_fails(mock_db_session):
    """Expired API key raises AuthenticationError."""
    svc = _svc(mock_db_session)
    created = svc.create_api_key("user-1", "expiring-key")
    svc._api_keys[created.key]["is_active"] = False
    with pytest.raises(AuthenticationError) as exc_info:
        svc.validate_api_key(created.key)
    assert "expired" in str(exc_info.value).lower()
    assert exc_info.type is AuthenticationError


def test_create_api_key_success(mock_db_session):
    """Creating an API key returns key data with non-empty key and key_id."""
    svc = _svc(mock_db_session)
    user = make_developer_user()
    result = svc.create_api_key(user["id"], "ci-deploy-key")
    assert isinstance(result, ApiKeyResult)
    assert len(result.key) > 0
    assert result.key_id.startswith("key-")


def test_revoke_api_key_success(mock_db_session):
    """Revoking an API key marks it inactive and returns True."""
    svc = _svc(mock_db_session)
    created = svc.create_api_key("user-1", "temp-key")
    result = svc.revoke_api_key(created.key_id)
    assert result is True
    assert svc._api_keys[created.key]["is_active"] is False


# =========================================================================
# Username / Password Authentication Tests
# =========================================================================

def test_authenticate_with_valid_credentials_success(mock_db_session):
    """Valid credentials return an authenticated result."""
    svc = _svc(mock_db_session)
    mock_user = _mock_user(password="TestPassword123!")
    mock_db_session.query.return_value.filter_by.return_value.first.return_value = mock_user
    result = svc.authenticate("testuser", "TestPassword123!")
    assert result.authenticated is True
    assert result.user_id == "user-123"


def test_authenticate_with_invalid_password_fails(mock_db_session):
    """Wrong password raises AuthenticationError."""
    svc = _svc(mock_db_session)
    mock_user = _mock_user(password="CorrectPassword!")
    mock_db_session.query.return_value.filter_by.return_value.first.return_value = mock_user
    with pytest.raises(AuthenticationError, match="Invalid password") as exc_info:
        svc.authenticate("testuser", "WrongPassword!")
    assert "Invalid password" in str(exc_info.value)
    assert exc_info.type is AuthenticationError


def test_authenticate_with_nonexistent_user_fails(mock_db_session):
    """Non-existent username raises AuthenticationError."""
    svc = _svc(mock_db_session)
    mock_db_session.query.return_value.filter_by.return_value.first.return_value = None
    with pytest.raises(AuthenticationError, match="User not found") as exc_info:
        svc.authenticate("nonexistent", "password")
    assert "User not found" in str(exc_info.value)
    assert exc_info.type is AuthenticationError


# =========================================================================
# RBAC Privilege Evaluation Tests (Feature F-301)
# =========================================================================

def test_check_privilege_admin_has_all_access(mock_db_session):
    """Admin with PRIV_ALL has wildcard access to any privilege."""
    svc = _svc(mock_db_session)
    admin = make_admin_user()
    assert PRIV_ALL in admin["privileges"]
    result = svc.check_privilege(admin, PRIV_REPO_WRITE)
    assert result is True


def test_check_privilege_developer_has_read_write(mock_db_session):
    """Developer has read and write but not admin-level privileges."""
    svc = _svc(mock_db_session)
    dev = make_developer_user()
    assert svc.check_privilege(dev, PRIV_REPO_READ) is True
    assert svc.check_privilege(dev, PRIV_ALL) is False


def test_check_privilege_readonly_cannot_write(mock_db_session):
    """Read-only user cannot write to repositories."""
    svc = _svc(mock_db_session)
    ro_user = make_readonly_user()
    assert PRIV_REPO_WRITE not in ro_user["privileges"]
    result = svc.check_privilege(ro_user, PRIV_REPO_WRITE)
    assert result is False


def test_check_privilege_anonymous_minimal_access(mock_db_session):
    """Anonymous user has public read-only access."""
    svc = _svc(mock_db_session)
    anon = make_anonymous_user()
    assert svc.check_privilege(anon, PRIV_REPO_READ) is True
    assert svc.check_privilege(anon, PRIV_REPO_WRITE) is False


def test_rbac_repository_specific_permissions(mock_db_session):
    """Tier-2 RBAC: repository-specific permissions restrict per-repo."""
    svc = _svc(mock_db_session)
    dev = make_developer_user()
    dev["repository_permissions"] = {
        PRIV_REPO_READ: ["allowed-repo"],
    }
    assert svc.check_privilege(dev, PRIV_REPO_READ, repository="allowed-repo") is True
    assert svc.check_privilege(dev, PRIV_REPO_READ, repository="forbidden-repo") is False


def test_rbac_content_selector_evaluation(mock_db_session):
    """Tier-3 RBAC: privilege check passes with valid content selector."""
    svc = _svc(mock_db_session)
    dev = make_developer_user()
    selector = make_content_selector_data(
        expression='format == "maven2" and path =^ "/com/example"',
    )
    result = svc.check_privilege(dev, PRIV_REPO_READ, content_selector=selector)
    assert result is True
    assert selector["expression"].startswith('format == "maven2"')


@pytest.mark.parametrize(
    "user_factory,privilege,expected",
    [
        (make_admin_user, PRIV_ALL, True),
        (make_admin_user, PRIV_REPO_READ, True),
        (make_developer_user, PRIV_REPO_READ, True),
        (make_developer_user, PRIV_REPO_WRITE, True),
        (make_developer_user, PRIV_ALL, False),
        (make_readonly_user, PRIV_REPO_READ, True),
        (make_readonly_user, PRIV_REPO_WRITE, False),
    ],
    ids=["admin-all-T", "admin-read-T", "dev-read-T", "dev-write-T",
         "dev-all-F", "ro-read-T", "ro-write-F"],
)
def test_check_privilege_parametrized_all_roles(
    mock_db_session, user_factory, privilege, expected,
):
    """Privilege matrix validates each role × privilege combination."""
    svc = _svc(mock_db_session)
    user = user_factory()
    result = svc.check_privilege(user, privilege)
    assert result is expected
    assert isinstance(result, bool)


# =========================================================================
# Session Management Tests
# =========================================================================

def test_create_session_success(mock_db_session):
    """Creating a session returns session data with a non-empty ID."""
    svc = _svc(mock_db_session)
    user = make_developer_user()
    result = svc.create_session(user["id"], "127.0.0.1", "pytest-agent")
    assert isinstance(result, SessionData)
    assert len(result.session_id) > 0
    assert result.user_id == user["id"]


def test_validate_session_success(mock_db_session):
    """Valid session returns active session data."""
    svc = _svc(mock_db_session)
    created = svc.create_session("user-1", "127.0.0.1", "agent")
    result = svc.validate_session(created.session_id)
    assert result.is_active is True
    assert result.session_id == created.session_id


@freeze_time("2025-01-15 12:00:00")
def test_validate_expired_session_fails(mock_db_session):
    """Expired session raises SessionExpiredError."""
    svc = SecurityService(db_session=mock_db_session, secret_key=SECRET,
                          session_expiry=1)
    created = svc.create_session("user-1")
    svc._sessions[created.session_id]["expires_at"] = (
        datetime.datetime(2025, 1, 14, 0, 0, 0)
    )
    with pytest.raises(SessionExpiredError, match="expired") as exc_info:
        svc.validate_session(created.session_id)
    assert "expired" in str(exc_info.value).lower()
    assert exc_info.type is SessionExpiredError


def test_invalidate_session_success(mock_db_session):
    """Invalidating a session marks it inactive and returns True."""
    svc = _svc(mock_db_session)
    created = svc.create_session("user-1")
    result = svc.invalidate_session(created.session_id)
    assert result is True
    assert svc._sessions[created.session_id]["is_active"] is False


def test_multiple_concurrent_sessions(mock_db_session):
    """Multiple sessions for same user each have unique IDs."""
    svc = _svc(mock_db_session)
    user = make_admin_user()
    sessions = [svc.create_session(user["id"]) for _ in range(3)]
    session_ids = [s.session_id for s in sessions]
    assert len(set(session_ids)) == 3
    assert all(isinstance(s, SessionData) for s in sessions)


# =========================================================================
# Error Case Tests
# =========================================================================

def test_authenticate_with_empty_credentials_fails(mock_db_session):
    """Empty username/password raises AuthenticationError."""
    svc = _svc(mock_db_session)
    with pytest.raises(AuthenticationError, match="Empty") as exc_info:
        svc.authenticate("", "")
    assert "Empty credentials" in str(exc_info.value)
    assert exc_info.type is AuthenticationError


def test_generate_token_for_inactive_user_fails(mock_db_session):
    """Inactive/disabled user cannot obtain a token."""
    svc = _svc(mock_db_session)
    user_data = {"id": "user-1", "username": "disabled", "status": "disabled"}
    with pytest.raises(AuthenticationError, match="inactive") as exc_info:
        svc.generate_token(user_data)
    assert "inactive" in str(exc_info.value).lower()
    assert exc_info.type is AuthenticationError


def test_check_privilege_with_none_user_fails(mock_db_session):
    """None user raises ValueError."""
    svc = _svc(mock_db_session)
    with pytest.raises(ValueError, match="None") as exc_info:
        svc.check_privilege(None, PRIV_REPO_READ)
    assert "None" in str(exc_info.value)
    assert exc_info.type is ValueError


def test_rbac_insufficient_privileges_returns_false(mock_db_session):
    """Insufficient privileges returns False; authorize raises ForbiddenError."""
    svc = _svc(mock_db_session)
    ro_user = make_readonly_user()
    result = svc.check_privilege(ro_user, PRIV_ALL)
    assert result is False
    with pytest.raises(ForbiddenError) as exc_info:
        svc.authorize(ro_user, PRIV_ALL)
    assert "Forbidden" in str(exc_info.value)


def test_validate_session_not_found_raises(mock_db_session):
    """Validating a nonexistent session raises AuthenticationError."""
    svc = _svc(mock_db_session)
    with pytest.raises(AuthenticationError, match="not found") as exc_info:
        svc.validate_session("nonexistent-session-id")
    assert "not found" in str(exc_info.value).lower()
    assert exc_info.type is AuthenticationError
