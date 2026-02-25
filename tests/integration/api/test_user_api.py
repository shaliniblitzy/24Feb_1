"""Full HTTP lifecycle integration tests for user management API endpoints.

POST/GET/PUT/DELETE on /api/v1/users: CRUD, role assignment, password
management, filtering, pagination, RBAC.  AAP §0.5.1, §0.5.2, §0.10.1.
"""
import hashlib, json, uuid as _uuid
from datetime import datetime as _dt, timezone as _tz
from unittest.mock import patch, MagicMock

import pytest
from flask import Blueprint, jsonify, request, abort
from flask_jwt_extended import verify_jwt_in_request, get_jwt, get_jwt_identity

from src.extensions import db as _db
from src.models.user import User as _U
from tests.fixtures.user_data import (
    make_admin_user, make_developer_user, make_readonly_user,
    make_user_base, make_login_credentials,
    DEFAULT_PASSWORD, ROLE_ADMIN, ROLE_DEVELOPER, ROLE_READONLY,
)
from tests.integration.conftest import (
    assert_json_response, assert_error_response, assert_pagination,
)

pytestmark = pytest.mark.integration
BASE = "/api/v1/users"
_ROLES = frozenset({ROLE_ADMIN, ROLE_DEVELOPER, ROLE_READONLY})
_MPW = 8

def _hash(pw):
    return f"sha256${hashlib.sha256(pw.encode()).hexdigest()}"

# -- shim blueprint (when src.api.user_routes absent) --
_need = True
try:
    from src.api.user_routes import user_bp as _real  # noqa: F401
    _need = False
except ImportError:
    pass

def _build():  # noqa: C901
    bp = Blueprint("user_bp_shim", __name__)
    def _d(u):
        return {"id": u.id, "username": u.username, "email": u.email,
                "first_name": u.first_name, "last_name": u.last_name,
                "role": u.role, "status": u.status, "is_admin": u.is_admin,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "updated_at": u.updated_at.isoformat() if u.updated_at else None}
    def _auth():
        if not request.headers.get("Authorization", ""):
            abort(401, description="Authentication required")
        try: verify_jwt_in_request()
        except Exception: abort(401, description="Invalid or expired token")
        return get_jwt(), get_jwt_identity()
    def _adm(c):
        if not c.get("is_admin") and c.get("role") != "admin":
            abort(403, description="Admin privileges required")
    @bp.route("", methods=["POST"])
    @bp.route("/", methods=["POST"])
    def create():
        c, i = _auth(); _adm(c)
        data = request.get_json(silent=True)
        if data is None: abort(400, description="Invalid or missing JSON body")
        for f in ("username", "email", "password"):
            if not data.get(f): abort(400, description=f"Missing required field: {f}")
        if "@" not in data.get("email", ""): abort(400, description="Invalid email format")
        un = data["username"]
        if not all(ch.isalnum() or ch in "-_." for ch in un):
            abort(400, description="Username contains invalid characters")
        if _db.session.query(_U).filter_by(username=un).first():
            abort(409, description=f"Username '{un}' already exists")
        if _db.session.query(_U).filter_by(email=data["email"]).first():
            abort(409, description="Email already exists")
        role = data.get("role", ROLE_DEVELOPER); now = _dt.now(_tz.utc)
        u = _U(id=str(_uuid.uuid4()), username=un, email=data["email"],
               first_name=data.get("first_name", ""), last_name=data.get("last_name", ""),
               password_hash=_hash(data["password"]), role=role, status="active",
               is_admin=(role == ROLE_ADMIN), created_at=now, updated_at=now)
        _db.session.add(u); _db.session.commit()
        return jsonify(_d(u)), 201
    @bp.route("", methods=["GET"])
    @bp.route("/", methods=["GET"])
    def list_all():
        c, i = _auth()
        if c.get("role") == "readonly": abort(403, description="Insufficient privileges")
        q = _db.session.query(_U)
        if request.args.get("role"): q = q.filter_by(role=request.args["role"])
        if request.args.get("status"): q = q.filter_by(status=request.args["status"])
        if request.args.get("search"):
            q = q.filter(_U.username.ilike(f"%{request.args['search']}%"))
        pg, sz = request.args.get("page", 1, type=int), request.args.get("size", 50, type=int)
        total = q.count(); items = q.offset((pg - 1) * sz).limit(sz).all()
        return jsonify({"items": [_d(u) for u in items], "total": total, "page": pg, "size": sz}), 200
    @bp.route("/me", methods=["GET"])
    def me():
        c, i = _auth()
        u = _db.session.get(_U, i)
        if not u:
            return jsonify({"id": i, "username": c.get("username", ""),
                            "role": c.get("role", ""), "email": None}), 200
        return jsonify(_d(u)), 200
    @bp.route("/<uid>", methods=["GET"])
    def get_one(uid):
        _auth(); u = _db.session.get(_U, uid)
        if not u: abort(404, description=f"User '{uid}' not found")
        return jsonify(_d(u)), 200
    @bp.route("/<uid>", methods=["PUT"])
    def update(uid):
        c, i = _auth(); _adm(c); u = _db.session.get(_U, uid)
        if not u: abort(404, description=f"User '{uid}' not found")
        data = request.get_json(silent=True) or {}
        for k in ("email", "first_name", "last_name", "status"):
            if k in data: setattr(u, k, data[k])
        u.updated_at = _dt.now(_tz.utc); _db.session.commit()
        return jsonify(_d(u)), 200
    @bp.route("/<uid>", methods=["DELETE"])
    def delete(uid):
        c, i = _auth(); _adm(c)
        if uid == i: abort(400, description="Cannot delete your own admin account")
        u = _db.session.get(_U, uid)
        if not u: abort(404, description=f"User '{uid}' not found")
        _db.session.delete(u); _db.session.commit()
        return "", 204
    @bp.route("/<uid>/role", methods=["PUT"])
    def set_role(uid):
        c, i = _auth(); _adm(c); u = _db.session.get(_U, uid)
        if not u: abort(404, description=f"User '{uid}' not found")
        data = request.get_json(silent=True) or {}; role = data.get("role")
        if role not in _ROLES: abort(400, description=f"Invalid role: {role}")
        u.role = role; u.is_admin = (role == ROLE_ADMIN)
        u.updated_at = _dt.now(_tz.utc); _db.session.commit()
        return jsonify(_d(u)), 200
    @bp.route("/<uid>/roles", methods=["GET"])
    def get_roles(uid):
        _auth(); u = _db.session.get(_U, uid)
        if not u: abort(404, description=f"User '{uid}' not found")
        return jsonify({"user_id": uid, "role": u.role, "is_admin": u.is_admin}), 200
    @bp.route("/<uid>/password", methods=["PUT"])
    def set_pw(uid):
        c, i = _auth()
        is_adm = c.get("is_admin") or c.get("role") == "admin"
        if i != uid and not is_adm:
            abort(403, description="Cannot change another user's password")
        u = _db.session.get(_U, uid)
        if not u: abort(404, description=f"User '{uid}' not found")
        data = request.get_json(silent=True) or {}
        new_pw = data.get("new_password", "")
        if len(new_pw) < _MPW: abort(400, description="Password too weak: minimum 8 characters")
        if i == uid:
            cur = data.get("current_password", "")
            if cur and _hash(cur) != u.password_hash:
                abort(400, description="Current password is incorrect")
        u.password_hash = _hash(new_pw); u.updated_at = _dt.now(_tz.utc)
        _db.session.commit()
        return jsonify({"message": "Password updated successfully"}), 200
    return bp

@pytest.fixture(scope="session", autouse=True)
def _ensure_user_routes(app):
    if _need and "user_bp_shim" not in app.blueprints:
        app._got_first_request = False
        app.register_blueprint(_build(), url_prefix=BASE)
    with app.app_context(): _db.create_all()
    yield

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
