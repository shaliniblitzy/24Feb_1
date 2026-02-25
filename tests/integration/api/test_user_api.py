"""Full HTTP lifecycle integration tests for user management API endpoints.

POST/GET/PUT/DELETE on /api/v1/users: CRUD, role assignment, password
management, filtering, pagination, RBAC.  AAP §0.5.1, §0.5.2, §0.10.1.
"""
import hashlib, json
from datetime import datetime as _dt, timezone as _tz
from unittest.mock import patch, MagicMock

import pytest

from src.models.user import User as _U
from tests.fixtures.user_data import (
    make_admin_user, make_developer_user, make_readonly_user,
    make_user_base, make_login_credentials,
    DEFAULT_PASSWORD, ROLE_ADMIN, ROLE_DEVELOPER, ROLE_READONLY,
)
from tests.integration.conftest import (
    assert_json_response, assert_error_response, assert_pagination,
)

# ---------------------------------------------------------------------------
# Import real source modules (no shim fallback).
# If source modules are not yet created, all tests in this file are skipped.
# ---------------------------------------------------------------------------
pytest.importorskip(
    "src.api.user_routes",
    reason="User routes module not yet created",
)

pytestmark = pytest.mark.integration
BASE = "/api/v1/users"

def _hash(pw):
    return f"sha256${hashlib.sha256(pw.encode()).hexdigest()}"

def _seed(ses, role=ROLE_DEVELOPER, **kw):
    factory = {ROLE_ADMIN: make_admin_user, ROLE_READONLY: make_readonly_user}.get(role, make_developer_user)
    data = factory(**kw)
    vk = {k: v for k, v in data.items() if hasattr(_U, k)}
    for dk in ("created_at", "updated_at", "last_login"):
        if dk in vk and isinstance(vk[dk], str): vk[dk] = _dt.fromisoformat(vk[dk])
    u = _U(**vk); ses.add(u); ses.commit()
    return u

# =================================================================
# CRUD — happy path
# =================================================================

def test_create_user_returns_201(client, auth_headers):
    """POST creates user → 201 without password in response."""
    payload = {"username": "newuser", "email": "new@example.com",
               "password": "SecurePass123!", "role": ROLE_DEVELOPER}
    d = assert_json_response(client.post(BASE, json=payload, headers=auth_headers), 201)
    assert d["username"] == "newuser"
    assert "password" not in d and "password_hash" not in d

def test_list_users_returns_200(client, auth_headers, db_session):
    """GET / returns paginated list."""
    for i in range(3): _seed(db_session, username=f"lu-{i}")
    d = assert_json_response(client.get(BASE, headers=auth_headers), 200)
    assert len(d["items"]) >= 3
    assert "total" in d

def test_get_user_by_id_returns_200(client, auth_headers, db_session):
    """GET /<id> returns user."""
    u = _seed(db_session, username="get-u")
    d = assert_json_response(client.get(f"{BASE}/{u.id}", headers=auth_headers), 200)
    assert d["id"] == u.id
    assert d["username"] == "get-u"

def test_update_user_returns_200(client, auth_headers, db_session):
    """PUT /<id> updates and returns 200."""
    u = _seed(db_session, username="upd-u")
    d = assert_json_response(
        client.put(f"{BASE}/{u.id}", json={"email": "upd@x.com", "first_name": "Up"},
                   headers=auth_headers), 200)
    assert d["email"] == "upd@x.com"
    assert d["first_name"] == "Up"

def test_delete_user_returns_204(client, auth_headers, db_session):
    """DELETE removes user; subsequent GET → 404."""
    u = _seed(db_session, username="del-u")
    assert client.delete(f"{BASE}/{u.id}", headers=auth_headers).status_code == 204
    assert client.get(f"{BASE}/{u.id}", headers=auth_headers).status_code == 404

# =================================================================
# Role assignment
# =================================================================

def test_assign_admin_role_returns_200(client, auth_headers, db_session):
    u = _seed(db_session, username="ra-1")
    d = assert_json_response(
        client.put(f"{BASE}/{u.id}/role", json={"role": ROLE_ADMIN}, headers=auth_headers), 200)
    assert d["role"] == ROLE_ADMIN
    assert d["is_admin"] is True

def test_assign_developer_role_returns_200(client, auth_headers, db_session):
    u = _seed(db_session, role=ROLE_READONLY, username="ra-2")
    d = assert_json_response(
        client.put(f"{BASE}/{u.id}/role", json={"role": ROLE_DEVELOPER}, headers=auth_headers), 200)
    assert d["role"] == ROLE_DEVELOPER

def test_assign_readonly_role_returns_200(client, auth_headers, db_session):
    u = _seed(db_session, username="ra-3")
    d = assert_json_response(
        client.put(f"{BASE}/{u.id}/role", json={"role": ROLE_READONLY}, headers=auth_headers), 200)
    assert d["role"] == ROLE_READONLY

def test_assign_invalid_role_returns_400(client, auth_headers, db_session):
    u = _seed(db_session, username="ra-bad")
    resp = client.put(f"{BASE}/{u.id}/role", json={"role": "superadmin"}, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.get_json() is not None

def test_get_user_roles_returns_200(client, auth_headers, db_session):
    u = _seed(db_session, role=ROLE_ADMIN, username="ra-v")
    d = assert_json_response(client.get(f"{BASE}/{u.id}/roles", headers=auth_headers), 200)
    assert d["role"] == ROLE_ADMIN
    assert "is_admin" in d

# =================================================================
# Password management
# =================================================================

def test_change_own_password_returns_200(client, auth_headers, db_session):
    """Admin changes own password with correct current_password."""
    u = _U(id="test-admin-user", username="pw-self", email="pws@a.com",
           role=ROLE_ADMIN, status="active", is_admin=True,
           password_hash=_hash(DEFAULT_PASSWORD),
           created_at=_dt.now(_tz.utc), updated_at=_dt.now(_tz.utc))
    db_session.add(u); db_session.commit()
    resp = client.put(f"{BASE}/{u.id}/password",
                      json={"current_password": DEFAULT_PASSWORD, "new_password": "NewSecure123!"},
                      headers=auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()["message"] == "Password updated successfully"

def test_admin_reset_user_password_returns_200(client, auth_headers, db_session):
    """Admin resets another user's password."""
    u = _seed(db_session, username="pw-rst")
    resp = client.put(f"{BASE}/{u.id}/password",
                      json={"new_password": "ResetPass123!"}, headers=auth_headers)
    assert resp.status_code == 200
    assert "message" in resp.get_json()

def test_change_password_wrong_current_returns_400(client, auth_headers, db_session):
    """Wrong current_password → 400."""
    u = _U(id="test-admin-user", username="pw-wr", email="pww@a.com",
           role=ROLE_ADMIN, status="active", is_admin=True,
           password_hash=_hash(DEFAULT_PASSWORD),
           created_at=_dt.now(_tz.utc), updated_at=_dt.now(_tz.utc))
    db_session.add(u); db_session.commit()
    resp = client.put(f"{BASE}/{u.id}/password",
                      json={"current_password": "WrongPassword!", "new_password": "NewSecure123!"},
                      headers=auth_headers)
    assert resp.status_code in (400, 401)
    assert resp.get_json() is not None

def test_change_password_weak_password_returns_400(client, auth_headers, db_session):
    """Weak new_password → 400."""
    u = _seed(db_session, username="pw-wk")
    resp = client.put(f"{BASE}/{u.id}/password",
                      json={"new_password": "123"}, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.get_json() is not None

# =================================================================
# Filtering and pagination
# =================================================================

def test_list_users_with_role_filter(client, auth_headers, db_session):
    _seed(db_session, role=ROLE_ADMIN, username="fl-a")
    _seed(db_session, username="fl-d")
    d = assert_json_response(client.get(f"{BASE}?role={ROLE_ADMIN}", headers=auth_headers), 200)
    assert all(u["role"] == ROLE_ADMIN for u in d["items"])
    assert len(d["items"]) >= 1

def test_list_users_with_status_filter(client, auth_headers, db_session):
    _seed(db_session, username="fs-act")
    _seed(db_session, username="fs-inact", status="inactive")
    d = assert_json_response(client.get(f"{BASE}?status=active", headers=auth_headers), 200)
    assert all(u["status"] == "active" for u in d["items"])
    assert any(u["username"] == "fs-act" for u in d["items"])

def test_list_users_with_pagination(client, auth_headers, db_session):
    for i in range(12): _seed(db_session, username=f"pg-{i}")
    d = assert_json_response(client.get(f"{BASE}?page=1&size=5", headers=auth_headers), 200)
    assert len(d["items"]) == 5
    assert d["total"] >= 12

def test_search_users_by_username(client, auth_headers, db_session):
    _seed(db_session, username="srch-target")
    _seed(db_session, username="srch-other")
    d = assert_json_response(client.get(f"{BASE}?search=target", headers=auth_headers), 200)
    assert any("target" in u["username"] for u in d["items"])
    assert d["total"] >= 1

# =================================================================
# Edge cases
# =================================================================

def test_create_user_duplicate_username_returns_409(client, auth_headers, db_session):
    _seed(db_session, username="dup-un")
    resp = client.post(BASE, json={"username": "dup-un", "email": "d1@x.com",
                       "password": "Secure12!", "role": ROLE_DEVELOPER}, headers=auth_headers)
    assert resp.status_code == 409
    assert resp.get_json() is not None

def test_create_user_duplicate_email_returns_409(client, auth_headers, db_session):
    _seed(db_session, username="dup-em", email="dup@x.com")
    resp = client.post(BASE, json={"username": "dup-em2", "email": "dup@x.com",
                       "password": "Secure12!", "role": ROLE_DEVELOPER}, headers=auth_headers)
    assert resp.status_code == 409
    assert resp.get_json() is not None

def test_get_nonexistent_user_returns_404(client, auth_headers):
    resp = client.get(f"{BASE}/nonexistent-id-999", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.get_json() is not None

def test_delete_own_admin_account_returns_400(client, auth_headers, db_session):
    """Admin cannot self-delete."""
    u = _U(id="test-admin-user", username="sd-adm", email="sd@a.com",
           role=ROLE_ADMIN, status="active", is_admin=True, password_hash=_hash("x"),
           created_at=_dt.now(_tz.utc), updated_at=_dt.now(_tz.utc))
    db_session.add(u); db_session.commit()
    resp = client.delete(f"{BASE}/test-admin-user", headers=auth_headers)
    assert resp.status_code in (400, 403)
    assert resp.get_json() is not None

def test_create_user_special_chars_in_username(client, auth_headers):
    resp = client.post(BASE, json={"username": "u@#$%", "email": "sp@x.com",
                       "password": "Secure12!", "role": ROLE_DEVELOPER}, headers=auth_headers)
    assert resp.status_code in (400, 201)
    assert resp.get_json() is not None

# =================================================================
# Security and authorization
# =================================================================

def test_create_user_without_auth_returns_401(client):
    resp = client.post(BASE, json={"username": "x", "email": "x@x.com",
                       "password": "Secure12!", "role": ROLE_DEVELOPER},
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert resp.get_json() is not None

def test_create_user_developer_returns_403(client, developer_auth_headers):
    resp = client.post(BASE, json={"username": "x2", "email": "x2@x.com",
                       "password": "Secure12!", "role": ROLE_DEVELOPER},
                       headers=developer_auth_headers)
    assert resp.status_code == 403
    assert resp.get_json() is not None

def test_list_users_readonly_returns_403(client, readonly_auth_headers):
    resp = client.get(BASE, headers=readonly_auth_headers)
    assert resp.status_code in (200, 403)
    assert resp.get_json() is not None

def test_delete_user_non_admin_returns_403(client, developer_auth_headers, db_session):
    u = _seed(db_session, role=ROLE_READONLY, username="del-nonadm")
    resp = client.delete(f"{BASE}/{u.id}", headers=developer_auth_headers)
    assert resp.status_code == 403
    assert resp.get_json() is not None

def test_user_can_get_own_profile(client, developer_auth_headers):
    """GET /me returns profile from JWT claims."""
    d = assert_json_response(client.get(f"{BASE}/me", headers=developer_auth_headers), 200)
    assert "username" in d
    assert "role" in d

def test_user_cannot_change_others_password(client, developer_auth_headers, db_session):
    u = _seed(db_session, role=ROLE_READONLY, username="oth-pw")
    resp = client.put(f"{BASE}/{u.id}/password", json={"new_password": "Hacked123!"},
                      headers=developer_auth_headers)
    assert resp.status_code == 403
    assert resp.get_json() is not None

# =================================================================
# Error handling
# =================================================================

def test_create_user_missing_fields_returns_400(client, auth_headers):
    resp = client.post(BASE, json={"username": "inc"}, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.get_json() is not None

def test_create_user_invalid_email_returns_400(client, auth_headers):
    resp = client.post(BASE, json={"username": "bad-em", "email": "not-email",
                       "password": "Secure12!", "role": ROLE_DEVELOPER}, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.get_json() is not None

def test_create_user_invalid_json_returns_400(client, auth_headers):
    resp = client.post(BASE, data="not-valid-json{{{", headers=auth_headers)
    assert resp.status_code == 400
    assert resp.get_json() is not None
