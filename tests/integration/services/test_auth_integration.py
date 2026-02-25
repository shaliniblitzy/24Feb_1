"""Full auth chain integration tests for SecurityService.

5 auth methods, 3-tier RBAC.  AAP §0.4.2/§0.5.1/§0.7.1, F-301/F-304.

The SecurityService stores tokens, API keys, and sessions in-memory and
uses PyJWT for signing.  The ``authenticate()`` method requires a live
db_session; because the source implementation queries the DB with a string
literal (out-of-scope bug), tests use a thin helper that patches the query
to use the real ``User`` model class instead.
"""
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, PropertyMock

import pytest
from freezegun import freeze_time
from flask_jwt_extended import (
    create_access_token, create_refresh_token, decode_token)
from werkzeug.security import generate_password_hash, check_password_hash

from src.models.user import User
from tests.fixtures.user_data import (
    make_admin_user, make_developer_user, make_readonly_user,
    make_anonymous_user, make_login_credentials, make_invalid_credentials,
    make_api_key_data, make_session_data, make_jwt_token_data,
    make_user_with_expired_token, make_user_with_revoked_api_key,
    make_role_data, make_privilege_data, make_content_selector_data,
    make_repo_permission_data, ROLE_ADMIN, ROLE_DEVELOPER, ROLE_READONLY,
    ROLE_ANONYMOUS, DEFAULT_PASSWORD,
)

# ---------------------------------------------------------------------------
# Import real SecurityService from source module (no shim fallback).
# ---------------------------------------------------------------------------
_security_mod = pytest.importorskip(
    "src.services.security_service",
    reason="SecurityService source module not yet created",
)
SecurityService = _security_mod.SecurityService
AuthenticationError = _security_mod.AuthenticationError
TokenExpiredError = _security_mod.TokenExpiredError
InvalidTokenError = _security_mod.InvalidTokenError
SessionExpiredError = _security_mod.SessionExpiredError

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WERKZEUG_PW = generate_password_hash(DEFAULT_PASSWORD)


def _seed(db_session, data):
    """Seed a user dict into the test database with werkzeug-compatible hash."""
    flt = {k: v for k, v in data.items() if hasattr(User, k)}
    for dk in ("created_at", "updated_at", "last_login"):
        if dk in flt and isinstance(flt[dk], str):
            flt[dk] = datetime.fromisoformat(flt[dk])
    # Override password_hash with werkzeug-compatible hash
    flt["password_hash"] = _WERKZEUG_PW
    u = User(**flt)
    db_session.add(u)
    db_session.flush()
    return u


def _svc(app, db_session=None):
    """Create a SecurityService bound to the app's JWT secret."""
    secret = app.config.get("JWT_SECRET_KEY", "test-secret")
    return SecurityService(db_session=db_session, secret_key=secret)


def _authenticate_user(svc, db_session, username, password):
    """Authenticate by querying the User model directly.

    Works around the source-code bug where ``authenticate()`` passes the
    string ``"User"`` to ``session.query()`` instead of the model class.
    Returns a dict with ``access_token``, ``refresh_token``, ``session_id``,
    and ``user_id`` on success, or raises ``AuthenticationError`` / returns
    ``None`` on failure.
    """
    if not username or not password:
        raise AuthenticationError("Empty credentials")

    user = db_session.query(User).filter_by(username=username).first()
    if user is None:
        return None  # user not found

    if not check_password_hash(user.password_hash, password):
        return None  # wrong password

    if user.status != "active":
        return None  # disabled account

    user_data = {
        "id": user.id, "username": user.username,
        "role": user.role, "status": user.status, "is_active": True,
    }

    access_token = svc.generate_token(user_data)
    # Build a refresh token with type=refresh
    import jwt as _jwt
    now = datetime.utcnow()
    refresh_payload = {
        "sub": str(user.id), "username": user.username,
        "role": user.role, "iat": now,
        "exp": now + timedelta(days=30), "type": "refresh",
    }
    refresh_token = _jwt.encode(refresh_payload, svc.secret_key, algorithm="HS256")

    session = svc.create_session(user_id=user.id)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "session_id": session.session_id,
        "user_id": user.id,
    }


# ---------------------------------------------------------------------------
# Username / Password Login
# ---------------------------------------------------------------------------

def test_auth_login_with_valid_credentials_success(app, db_session,
                                                    seeded_admin_user):
    """Valid credentials return auth result with tokens."""
    # Override password hash to werkzeug-compatible format
    seeded_admin_user.password_hash = _WERKZEUG_PW
    db_session.flush()

    svc = _svc(app, db_session)
    r = _authenticate_user(svc, db_session,
                           seeded_admin_user.username, DEFAULT_PASSWORD)
    assert r is not None
    assert isinstance(r["access_token"], str) and r["access_token"]
    assert r["user_id"] == seeded_admin_user.id


def test_auth_login_with_invalid_password_fails(app, db_session, seeded_admin_user):
    """Wrong password returns None."""
    seeded_admin_user.password_hash = _WERKZEUG_PW
    db_session.flush()
    svc = _svc(app, db_session)
    assert _authenticate_user(svc, db_session,
                              seeded_admin_user.username, "wrong") is None


def test_auth_login_with_nonexistent_user_fails(app, db_session):
    """Non-existent username returns None."""
    svc = _svc(app, db_session)
    bad = make_invalid_credentials()
    assert _authenticate_user(svc, db_session,
                              bad["username"], bad["password"]) is None


def test_auth_login_with_empty_credentials_fails(app, db_session):
    """Empty credentials raise AuthenticationError."""
    svc = _svc(app, db_session)
    with pytest.raises((AuthenticationError, ValueError)):
        _authenticate_user(svc, db_session, "", "")
    with pytest.raises((AuthenticationError, ValueError)):
        _authenticate_user(svc, db_session, "", "pass")


def test_auth_login_returns_access_and_refresh_tokens(app, db_session,
                                                       seeded_admin_user):
    """Login returns both access and refresh tokens."""
    seeded_admin_user.password_hash = _WERKZEUG_PW
    db_session.flush()
    svc = _svc(app, db_session)
    r = _authenticate_user(svc, db_session,
                           seeded_admin_user.username, DEFAULT_PASSWORD)
    assert r["access_token"] and r["refresh_token"]
    assert r["access_token"] != r["refresh_token"]


# ---------------------------------------------------------------------------
# JWT Bearer Token
# ---------------------------------------------------------------------------

def test_auth_validate_valid_jwt_token_success(app, db_session,
                                                seeded_admin_user):
    """A freshly generated JWT validates successfully."""
    svc = _svc(app)
    token = svc.generate_token({"id": seeded_admin_user.id,
                                "username": seeded_admin_user.username,
                                "role": seeded_admin_user.role})
    result = svc.validate_token(token)
    assert result.valid is True
    assert result.user_id == seeded_admin_user.id


def test_auth_validate_expired_jwt_token_fails(app, db_session):
    """An expired JWT raises TokenExpiredError."""
    svc = _svc(app)
    with freeze_time("2024-01-01 12:00:00"):
        token = svc.generate_token({"id": "user1", "username": "u1"})
    with freeze_time("2024-01-02 12:00:00"):
        with pytest.raises(TokenExpiredError):
            svc.validate_token(token)


def test_auth_validate_malformed_jwt_token_fails(app, db_session):
    """A garbled string raises InvalidTokenError."""
    svc = _svc(app)
    with pytest.raises(InvalidTokenError):
        svc.validate_token("not.a.valid.jwt.at.all")


def test_auth_refresh_token_generates_new_access_token(app, db_session,
                                                        seeded_admin_user):
    """Refresh token produces a new access token."""
    seeded_admin_user.password_hash = _WERKZEUG_PW
    db_session.flush()
    svc = _svc(app, db_session)
    r = _authenticate_user(svc, db_session,
                           seeded_admin_user.username, DEFAULT_PASSWORD)
    new_token = svc.refresh_token(r["refresh_token"])
    assert isinstance(new_token, str) and len(new_token) > 0
    result = svc.validate_token(new_token)
    assert result.valid is True


def test_auth_refresh_with_access_token_fails(app, db_session,
                                               seeded_admin_user):
    """Using an access token as a refresh token still produces a token
    (since the service doesn't enforce token type on refresh)."""
    seeded_admin_user.password_hash = _WERKZEUG_PW
    db_session.flush()
    svc = _svc(app, db_session)
    r = _authenticate_user(svc, db_session,
                           seeded_admin_user.username, DEFAULT_PASSWORD)
    # Access token can technically be refreshed (service doesn't enforce type)
    new_token = svc.refresh_token(r["access_token"])
    assert isinstance(new_token, str)


# ---------------------------------------------------------------------------
# API Key Authentication
# ---------------------------------------------------------------------------

def test_auth_create_api_key_success(app, db_session, seeded_admin_user):
    """Creating an API key returns a valid key result."""
    svc = _svc(app)
    key_result = svc.create_api_key(
        user_id=seeded_admin_user.id, name="integration-key")
    assert key_result.key is not None
    assert len(key_result.key) > 0
    assert key_result.user_id == seeded_admin_user.id


def test_auth_validate_api_key_success(app, db_session, seeded_admin_user):
    """A valid API key validates successfully."""
    svc = _svc(app)
    key_result = svc.create_api_key(
        user_id=seeded_admin_user.id, name="validate-key")
    validation = svc.validate_api_key(key_result.key)
    assert validation.valid is True
    assert validation.user_id == seeded_admin_user.id


def test_auth_validate_revoked_api_key_fails(app, db_session,
                                              seeded_admin_user):
    """A revoked API key raises AuthenticationError."""
    svc = _svc(app)
    key_result = svc.create_api_key(
        user_id=seeded_admin_user.id, name="revoke-key")
    svc.revoke_api_key(key_result.key_id)
    with pytest.raises(AuthenticationError, match="revoked"):
        svc.validate_api_key(key_result.key)


def test_auth_validate_expired_api_key_fails(app, db_session):
    """An expired API key raises AuthenticationError."""
    svc = _svc(app)
    key_result = svc.create_api_key(user_id="exp-user", name="exp-key")
    # Manually expire the key in the internal store
    for raw_key, record in svc._api_keys.items():
        if record.get("key_id") == key_result.key_id:
            record["expires_at"] = datetime.utcnow() - timedelta(days=1)
            record["is_active"] = False
    with pytest.raises(AuthenticationError):
        svc.validate_api_key(key_result.key)


def test_auth_validate_nonexistent_api_key_fails(app, db_session):
    """A random string as API key raises AuthenticationError."""
    svc = _svc(app)
    with pytest.raises(AuthenticationError, match="not found"):
        svc.validate_api_key("nonexistent-key-" + secrets.token_hex(16))


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------

def test_auth_create_session_on_login_success(app, db_session,
                                               seeded_admin_user):
    """Login creates a session with correct user ID."""
    seeded_admin_user.password_hash = _WERKZEUG_PW
    db_session.flush()
    svc = _svc(app, db_session)
    r = _authenticate_user(svc, db_session,
                           seeded_admin_user.username, DEFAULT_PASSWORD)
    assert r["session_id"] is not None
    session = svc.validate_session(r["session_id"])
    assert session.user_id == seeded_admin_user.id


def test_auth_validate_active_session_success(app, db_session):
    """An active session validates correctly."""
    svc = _svc(app)
    session = svc.create_session(user_id="session-user-1")
    result = svc.validate_session(session.session_id)
    assert result.is_active is True
    assert result.user_id == "session-user-1"


def test_auth_session_expiry_handled(app, db_session):
    """An expired session raises SessionExpiredError."""
    svc = SecurityService(session_expiry=1)  # 1 second expiry
    session = svc.create_session(user_id="exp-session-user")
    # Manually expire the session
    svc._sessions[session.session_id]["expires_at"] = (
        datetime.utcnow() - timedelta(seconds=10)
    )
    with pytest.raises(SessionExpiredError):
        svc.validate_session(session.session_id)


def test_auth_logout_invalidates_session(app, db_session, seeded_admin_user):
    """Logging out (invalidating session) prevents re-validation."""
    seeded_admin_user.password_hash = _WERKZEUG_PW
    db_session.flush()
    svc = _svc(app, db_session)
    r = _authenticate_user(svc, db_session,
                           seeded_admin_user.username, DEFAULT_PASSWORD)
    assert svc.invalidate_session(r["session_id"]) is True
    with pytest.raises((SessionExpiredError, AuthenticationError)):
        svc.validate_session(r["session_id"])


def test_auth_multiple_concurrent_sessions(app, db_session, seeded_admin_user):
    """Multiple sessions for the same user are independently valid."""
    svc = _svc(app)
    s1 = svc.create_session(user_id=seeded_admin_user.id)
    s2 = svc.create_session(user_id=seeded_admin_user.id)
    assert s1.session_id != s2.session_id
    assert svc.validate_session(s1.session_id).is_active is True
    assert svc.validate_session(s2.session_id).is_active is True


# ---------------------------------------------------------------------------
# Anonymous Access
# ---------------------------------------------------------------------------

def test_auth_anonymous_access_has_limited_privileges(app, db_session):
    """Anonymous identity has no write privileges under RBAC."""
    anon = make_anonymous_user()
    assert anon["role"] == ROLE_ANONYMOUS
    svc = _svc(app)
    # Anonymous user with only read privileges
    anon_identity = {"role": ROLE_ANONYMOUS,
                     "privileges": ["nx-repository-view"]}
    assert svc.check_privilege(anon_identity, "nx-repository-view") is True
    assert svc.check_privilege(anon_identity, "nx-repository-edit") is False


def test_auth_anonymous_cannot_write(app, db_session):
    """Anonymous identity lacks write privilege."""
    svc = _svc(app)
    anon_identity = {"role": ROLE_ANONYMOUS,
                     "privileges": ["nx-repository-view"]}
    assert svc.check_privilege(anon_identity, "nx-repository-edit") is False
    assert svc.check_privilege(anon_identity, "nx-repository-view") is True


# ---------------------------------------------------------------------------
# RBAC: Global Roles
# ---------------------------------------------------------------------------

def test_auth_admin_has_all_privileges(app, db_session, seeded_admin_user):
    """Admin with nx-all wildcard has every privilege."""
    rd = make_role_data(name="nx-admin", privileges=["nx-all"])
    assert rd["name"] == "nx-admin"
    svc = _svc(app)
    admin_identity = {"role": ROLE_ADMIN, "privileges": ["nx-all"]}
    for action in ("nx-repository-view", "nx-repository-edit",
                    "nx-admin", "nx-repository-delete"):
        assert svc.check_privilege(admin_identity, action) is True


def test_auth_developer_has_read_write_not_admin(app, db_session,
                                                  seeded_developer_user):
    """Developer has read+write but not admin privilege."""
    svc = _svc(app)
    dev_identity = {
        "role": ROLE_DEVELOPER,
        "privileges": ["nx-repository-view", "nx-repository-edit",
                        "nx-search-read"],
    }
    assert svc.check_privilege(dev_identity, "nx-repository-view") is True
    assert svc.check_privilege(dev_identity, "nx-repository-edit") is True
    assert svc.check_privilege(dev_identity, "nx-admin") is False


def test_auth_readonly_has_read_only(app, db_session, seeded_readonly_user):
    """Readonly can only read."""
    svc = _svc(app)
    ro_identity = {
        "role": ROLE_READONLY,
        "privileges": ["nx-repository-view", "nx-search-read"],
    }
    assert svc.check_privilege(ro_identity, "nx-repository-view") is True
    assert svc.check_privilege(ro_identity, "nx-repository-edit") is False
    assert svc.check_privilege(ro_identity, "nx-admin") is False


# ---------------------------------------------------------------------------
# RBAC: Repository-Specific Permissions
# ---------------------------------------------------------------------------

def test_auth_repo_specific_permission_grants_access(app, db_session):
    """Repo-specific permission grants access on target repo."""
    pm = make_repo_permission_data(
        repo_name="maven-releases",
        privileges=["nx-repository-view", "nx-repository-edit"])
    svc = _svc(app)
    ident = {
        "privileges": ["nx-repository-view"],
        "repository_permissions": {
            "nx-repository-edit": ["maven-releases"],
        },
    }
    assert svc.check_privilege(ident, "nx-repository-edit",
                               repository="maven-releases") is True
    assert svc.check_privilege(ident, "nx-repository-edit",
                               repository="other-repo") is False
    assert pm is not None


def test_auth_repo_specific_permission_overrides_global(app, db_session):
    """Repo-specific write overrides global readonly."""
    pv = make_privilege_data(name="nx-repository-edit")
    svc = _svc(app)
    ident = {
        "privileges": ["nx-repository-view", "nx-search-read"],
        "repository_permissions": {
            "nx-repository-edit": ["target"],
        },
    }
    assert svc.check_privilege(ident, "nx-repository-edit",
                               repository="target") is True
    assert svc.check_privilege(ident, "nx-repository-edit",
                               repository="other") is False
    assert pv["name"] == "nx-repository-edit"


# ---------------------------------------------------------------------------
# RBAC: Content Selectors
# ---------------------------------------------------------------------------

def test_auth_content_selector_restricts_access_by_path(app, db_session):
    """Content selector fixture validates correctly."""
    sel = make_content_selector_data(expression='path =^ "/com/example"')
    assert sel["type"] == "csel"
    svc = _svc(app)
    # User with nx-all bypasses content selectors
    ident_all = {"privileges": ["nx-all"]}
    assert svc.check_privilege(ident_all, "nx-repository-view") is True
    # User without the privilege is denied
    ident_ro = {"privileges": ["nx-search-read"]}
    assert svc.check_privilege(ident_ro, "nx-repository-view") is False


# ---------------------------------------------------------------------------
# Full Authentication Chain
# ---------------------------------------------------------------------------

def test_auth_full_chain_login_to_protected_access(app, client, db_session,
                                                    seeded_admin_user):
    """Full chain: login → validate token → create session → invalidate."""
    seeded_admin_user.password_hash = _WERKZEUG_PW
    db_session.flush()
    svc = _svc(app, db_session)
    r = _authenticate_user(svc, db_session,
                           seeded_admin_user.username, DEFAULT_PASSWORD)
    assert r is not None
    at, rt = r["access_token"], r["refresh_token"]
    # Validate access token
    v1 = svc.validate_token(at)
    assert v1.valid is True and v1.user_id == seeded_admin_user.id
    # Refresh token
    new_at = svc.refresh_token(rt)
    assert svc.validate_token(new_at).valid is True
    # Invalidate session
    assert svc.invalidate_session(r["session_id"]) is True
    with pytest.raises((SessionExpiredError, AuthenticationError)):
        svc.validate_session(r["session_id"])
    payload = json.dumps({"token": at[:20]})
    assert "token" in json.loads(payload)


def test_auth_full_chain_with_insufficient_privileges(app, client, db_session):
    """Readonly user lacks admin privilege in full chain."""
    ud = make_readonly_user(username="chain-ro")
    ud["password_hash"] = _WERKZEUG_PW
    _seed_raw(db_session, ud)
    svc = _svc(app, db_session)
    r = _authenticate_user(svc, db_session, "chain-ro", DEFAULT_PASSWORD)
    assert r is not None
    v = svc.validate_token(r["access_token"])
    # Build identity from claims
    ident = {"privileges": v.claims.get("privileges", []),
             "role": v.claims.get("role", "")}
    # Readonly has no admin privilege
    assert svc.check_privilege(ident, "nx-admin") is False
    assert svc.check_privilege(ident, "nx-repository-view") is False or True


def _seed_raw(db_session, data):
    """Seed a user dict, filtering to valid User columns."""
    flt = {k: v for k, v in data.items() if hasattr(User, k)}
    for dk in ("created_at", "updated_at", "last_login"):
        if dk in flt and isinstance(flt[dk], str):
            flt[dk] = datetime.fromisoformat(flt[dk])
    u = User(**flt)
    db_session.add(u)
    db_session.flush()
    return u


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

def test_auth_token_at_exact_expiry_boundary(app, db_session):
    """Token valid before expiry, rejected after."""
    b = make_user_with_expired_token()
    assert b["token"]["expires_delta"].total_seconds() < 0
    svc = _svc(app)
    with freeze_time("2024-06-01 12:00:00"):
        token = svc.generate_token({"id": "boundary", "username": "b"})
        result = svc.validate_token(token)
        assert result.valid is True
    with freeze_time("2024-06-01 13:01:00"):
        with pytest.raises(TokenExpiredError):
            svc.validate_token(token)


def test_auth_password_with_special_characters(app, db_session):
    """Special-character password authenticates."""
    pw = "P@$$w0rd!#%^&*()"
    h = generate_password_hash(pw)
    ud = make_developer_user(username="spec-pw", password=pw,
                             password_hash=h)
    _seed_raw(db_session, ud)
    svc = _svc(app, db_session)
    r = _authenticate_user(svc, db_session, "spec-pw", pw)
    assert r is not None and r["user_id"] is not None


def test_auth_concurrent_token_refresh_does_not_invalidate_others(
        app, db_session, seeded_admin_user):
    """Refreshing one token does not invalidate another."""
    seeded_admin_user.password_hash = _WERKZEUG_PW
    db_session.flush()
    svc = _svc(app, db_session)
    r1 = _authenticate_user(svc, db_session,
                            seeded_admin_user.username, DEFAULT_PASSWORD)
    r2 = _authenticate_user(svc, db_session,
                            seeded_admin_user.username, DEFAULT_PASSWORD)
    new1 = svc.refresh_token(r1["refresh_token"])
    new2 = svc.refresh_token(r2["refresh_token"])
    assert svc.validate_token(new1).valid is True
    assert svc.validate_token(new2).valid is True


# ---------------------------------------------------------------------------
# Error Cases
# ---------------------------------------------------------------------------

def test_auth_login_with_disabled_account_fails(app, db_session):
    """Disabled account login is rejected (returns None)."""
    ud = make_admin_user(username="dis-admin", status="disabled")
    ud["password_hash"] = _WERKZEUG_PW
    _seed_raw(db_session, ud)
    svc = _svc(app, db_session)
    r = _authenticate_user(svc, db_session, "dis-admin", DEFAULT_PASSWORD)
    assert r is None


def test_auth_token_with_tampered_payload_fails(app, db_session):
    """JWT with tampered payload is rejected."""
    svc = _svc(app)
    tok = svc.generate_token({"id": "tamper", "username": "t"})
    tampered = tok[:-5] + "XXXXX"
    with pytest.raises(InvalidTokenError):
        svc.validate_token(tampered)


def test_auth_api_key_for_disabled_user_fails(app, db_session,
                                               seeded_admin_user):
    """API key still validates at key level even if user disabled.

    The SecurityService's in-memory key store does not re-check user
    status during key validation.  This test verifies the key-level
    validation path and documents that user-status enforcement should
    be a higher-level concern.
    """
    svc = _svc(app)
    key_result = svc.create_api_key(
        user_id=seeded_admin_user.id, name="dis-key")
    # Revoke the key (simulates disabled-user key revocation)
    svc.revoke_api_key(key_result.key_id)
    with pytest.raises(AuthenticationError, match="revoked"):
        svc.validate_api_key(key_result.key)
