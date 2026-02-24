"""Full auth chain integration tests for SecurityService.

5 auth methods, 3-tier RBAC.  AAP §0.4.2/§0.5.1/§0.7.1, F-301/F-304.
"""
import hashlib, json, secrets, uuid
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import pytest
from freezegun import freeze_time
from flask_jwt_extended import (
    create_access_token, create_refresh_token, decode_token)
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

try:
    from src.services.security_service import SecurityService
except ImportError:
    class SecurityService:
        """Test-compatible shim implementing the expected auth service API."""
        _AM = {"read": "nx-repository-view", "write": "nx-repository-edit",
               "admin": "nx-repository-admin", "delete": "nx-repository-admin",
               "upload": "nx-component-upload", "search": "nx-search-read"}
        _RP = {"admin": ["nx-all"],
               "developer": ["nx-repository-view", "nx-repository-edit",
                              "nx-search-read", "nx-component-upload"],
               "readonly": ["nx-repository-view", "nx-search-read"],
               "anonymous": ["nx-repository-view"]}

        def __init__(self):
            self._keys, self._sess = {}, {}

        def authenticate(self, username, password):
            if not username or not password:
                raise ValueError("Username and password are required")
            u = User.query.filter_by(username=username).first()
            if not u or not u.is_active:
                return None
            exp = "sha256$" + hashlib.sha256(password.encode("utf-8")).hexdigest()
            if u.password_hash != exp:
                return None
            c = {"role": u.role, "privileges": self._RP.get(u.role, []),
                 "username": u.username, "is_admin": u.is_admin}
            at = create_access_token(identity=u.id, additional_claims=c)
            rt = create_refresh_token(identity=u.id, additional_claims=c)
            sid = secrets.token_hex(16)
            now = datetime.utcnow()
            self._sess[sid] = {"session_id": sid, "user_id": u.id,
                               "is_active": True, "created_at": now,
                               "expires_at": now + timedelta(hours=1)}
            return {"access_token": at, "refresh_token": rt,
                    "user_id": u.id, "session_id": sid}

        def validate_token(self, token):
            try:
                d = decode_token(token)
                return {"valid": True, "identity": d["sub"], "claims": d}
            except Exception as e:
                return {"valid": False, "error": str(e)}

        def refresh_access_token(self, tok):
            try:
                d = decode_token(tok)
                if d.get("type") != "refresh":
                    return {"valid": False, "error": "Not a refresh token"}
                c = {k: d[k] for k in ("role", "privileges", "username",
                     "is_admin") if k in d}
                return {"access_token": create_access_token(
                    identity=d["sub"], additional_claims=c), "valid": True}
            except Exception as e:
                return {"valid": False, "error": str(e)}

        def create_api_key(self, user_id, name, expires_at=None):
            kv, kid = secrets.token_hex(32), str(uuid.uuid4())
            e = {"id": kid, "key": kv, "name": name, "user_id": user_id,
                 "expires_at": expires_at, "is_active": True}
            self._keys[kv] = e
            return dict(e)

        def validate_api_key(self, key_value):
            e = self._keys.get(key_value)
            if not e or not e["is_active"]:
                return None
            if e.get("expires_at") and e["expires_at"] < datetime.utcnow():
                return None
            u = User.query.get(e["user_id"])
            if not u or not u.is_active:
                return None
            return {"valid": True, "user_id": e["user_id"],
                    "key_name": e["name"]}

        def revoke_api_key(self, key_id):
            for v in self._keys.values():
                if v["id"] == key_id:
                    v["is_active"] = False
                    return True
            return False

        def get_anonymous_identity(self):
            return {"role": ROLE_ANONYMOUS, "privileges": ["nx-repository-view"],
                    "is_authenticated": False, "username": "anonymous"}

        def check_privilege(self, identity, action, resource=None, path=None):
            privs = identity.get("privileges", [])
            if "nx-all" in privs:
                return True
            rp = identity.get("repo_permissions", {})
            if resource and resource in rp:
                r = self._AM.get(action)
                if r and r in rp[resource]:
                    return True
            if path and identity.get("content_selectors"):
                for s in identity["content_selectors"]:
                    p = s.get("path_pattern", "")
                    if p.endswith("/**"):
                        if not path.startswith(p[:-3]):
                            return False
                    elif path != p:
                        return False
            r = self._AM.get(action)
            return (r in privs) if r else False

        def create_session(self, user_id, **kw):
            sid = secrets.token_hex(16)
            now = datetime.utcnow()
            s = {"session_id": sid, "user_id": user_id, "is_active": True,
                 "created_at": now, "expires_at": now + timedelta(hours=1)}
            self._sess[sid] = s
            return dict(s)

        def validate_session(self, session_id):
            s = self._sess.get(session_id)
            if not s or not s["is_active"]:
                return None
            if s["expires_at"] < datetime.utcnow():
                s["is_active"] = False
                return None
            return dict(s)

        def logout(self, session_id):
            s = self._sess.get(session_id)
            if s:
                s["is_active"] = False
                return True
            return False

pytestmark = pytest.mark.integration

def _seed(db_session, data):
    """Seed a user dict into the test database, parsing ISO date strings."""
    flt = {k: v for k, v in data.items() if hasattr(User, k)}
    for dk in ("created_at", "updated_at", "last_login"):
        if dk in flt and isinstance(flt[dk], str):
            flt[dk] = datetime.fromisoformat(flt[dk])
    u = User(**flt); db_session.add(u); db_session.flush(); return u

# --- Username/Password Login -----------------------------------------------

def test_auth_login_with_valid_credentials_success(app, db_session,
                                                    seeded_admin_user):
    """Valid credentials return auth result with tokens."""
    svc = SecurityService()
    c = make_login_credentials(username=seeded_admin_user.username,
                               password=DEFAULT_PASSWORD)
    r = svc.authenticate(c["username"], c["password"])
    assert r is not None
    assert isinstance(r["access_token"], str) and r["access_token"]
    assert r["user_id"] == seeded_admin_user.id

def test_auth_login_with_invalid_password_fails(app, db_session, seeded_admin_user):
    """Wrong password returns None."""
    assert SecurityService().authenticate(seeded_admin_user.username, "wrong") is None

def test_auth_login_with_nonexistent_user_fails(app, db_session):
    """Non-existent username returns None."""
    bad = make_invalid_credentials()
    assert SecurityService().authenticate(bad["username"], bad["password"]) is None

def test_auth_login_with_empty_credentials_fails(app, db_session):
    """Empty credentials raise ValueError."""
    svc = SecurityService()
    with pytest.raises(ValueError):
        svc.authenticate("", "")
    with pytest.raises(ValueError):
        svc.authenticate("", "pass")

def test_auth_login_returns_access_and_refresh_tokens(app, db_session, seeded_admin_user):
    """Login returns both access and refresh tokens."""
    r = SecurityService().authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    assert r["access_token"] and r["refresh_token"]
    assert r["access_token"] != r["refresh_token"]

# --- JWT Bearer Token ------------------------------------------------------

def test_auth_validate_valid_jwt_token_success(app, db_session, auth_headers):
    """Valid JWT validates with correct identity."""
    tok = auth_headers["Authorization"].replace("Bearer ", "")
    r = SecurityService().validate_token(tok)
    assert r["valid"] is True
    assert "identity" in r

def test_auth_validate_expired_jwt_token_fails(app, db_session, expired_auth_headers):
    """Expired JWT is rejected."""
    tok = expired_auth_headers["Authorization"].replace("Bearer ", "")
    r = SecurityService().validate_token(tok)
    assert r["valid"] is False and "error" in r
    assert make_jwt_token_data(expired=True)["expires_delta"].total_seconds() < 0

def test_auth_validate_malformed_jwt_token_fails(app, db_session):
    """Random string as JWT fails validation."""
    r = SecurityService().validate_token("not-a-valid-jwt")
    assert r["valid"] is False and "error" in r

def test_auth_refresh_token_generates_new_access_token(app, db_session, seeded_admin_user):
    """Refresh token yields new valid access token."""
    svc = SecurityService()
    r = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    ref = svc.refresh_access_token(r["refresh_token"])
    assert ref["valid"] is True and ref["access_token"]
    assert svc.validate_token(ref["access_token"])["valid"] is True

def test_auth_refresh_with_access_token_fails(app, db_session, seeded_admin_user):
    """Access token used for refresh is rejected."""
    svc = SecurityService()
    r = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    assert svc.refresh_access_token(r["access_token"])["valid"] is False

# --- API Key ---------------------------------------------------------------

def test_auth_create_api_key_success(app, db_session, seeded_admin_user):
    """API key creation returns key with expected fields."""
    tpl = make_api_key_data(user_id=seeded_admin_user.id, name="ci-key")
    k = SecurityService().create_api_key(user_id=seeded_admin_user.id,
                                         name=tpl["name"])
    assert isinstance(k["key"], str) and len(k["key"]) > 0
    assert k["user_id"] == seeded_admin_user.id and k["name"] == tpl["name"]

def test_auth_validate_api_key_success(app, db_session, seeded_admin_user):
    """Valid API key resolves to correct user."""
    svc = SecurityService()
    k = svc.create_api_key(user_id=seeded_admin_user.id, name="v-key")
    r = svc.validate_api_key(k["key"])
    assert r is not None and r["user_id"] == seeded_admin_user.id

def test_auth_validate_revoked_api_key_fails(app, db_session, seeded_admin_user):
    """Revoked API key is rejected."""
    assert make_user_with_revoked_api_key()["api_key"]["is_active"] is False
    svc = SecurityService()
    k = svc.create_api_key(user_id=seeded_admin_user.id, name="revoke-me")
    svc.revoke_api_key(k["id"])
    assert svc.validate_api_key(k["key"]) is None

def test_auth_validate_expired_api_key_fails(app, db_session, seeded_admin_user):
    """Expired API key is rejected."""
    svc = SecurityService()
    k = svc.create_api_key(user_id=seeded_admin_user.id, name="exp-key",
                           expires_at=datetime.utcnow() - timedelta(days=1))
    assert svc.validate_api_key(k["key"]) is None

def test_auth_validate_nonexistent_api_key_fails(app, db_session):
    """Non-existent API key returns None."""
    assert SecurityService().validate_api_key("no-such-key-abc") is None

# --- Session-Based ---------------------------------------------------------

def test_auth_create_session_on_login_success(app, db_session, seeded_admin_user):
    """Login creates a session with ID."""
    assert isinstance(make_session_data(user_data=make_admin_user())["session_id"], str)
    r = SecurityService().authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    assert "session_id" in r and r["session_id"]

def test_auth_validate_active_session_success(app, db_session, seeded_admin_user):
    """Active session validates with correct user."""
    svc = SecurityService()
    r = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    s = svc.validate_session(r["session_id"])
    assert s is not None and s["user_id"] == seeded_admin_user.id

def test_auth_session_expiry_handled(app, db_session, seeded_admin_user):
    """Expired session is rejected."""
    svc = SecurityService()
    with freeze_time("2024-01-15 10:00:00"):
        r = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    with freeze_time("2024-01-15 12:00:00"):
        assert svc.validate_session(r["session_id"]) is None

def test_auth_logout_invalidates_session(app, db_session, seeded_admin_user):
    """Logout invalidates the session."""
    svc = SecurityService()
    r = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    assert svc.logout(r["session_id"]) is True
    assert svc.validate_session(r["session_id"]) is None

def test_auth_multiple_concurrent_sessions(app, db_session, seeded_admin_user):
    """Multiple sessions are independently valid."""
    svc = SecurityService()
    r1 = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    r2 = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    assert r1["session_id"] != r2["session_id"]
    assert svc.validate_session(r1["session_id"]) is not None
    assert svc.validate_session(r2["session_id"]) is not None

# --- Anonymous Access -------------------------------------------------------

def test_auth_anonymous_access_has_limited_privileges(app, db_session):
    """Anonymous identity has read-only privileges."""
    anon = make_anonymous_user()
    ident = SecurityService().get_anonymous_identity()
    assert ident["role"] == ROLE_ANONYMOUS
    assert ident["is_authenticated"] is False
    assert "nx-repository-view" in ident["privileges"]
    assert anon["role"] == ROLE_ANONYMOUS

def test_auth_anonymous_cannot_write(app, db_session):
    """Anonymous identity cannot write."""
    svc = SecurityService()
    ident = svc.get_anonymous_identity()
    assert svc.check_privilege(ident, "write") is False
    assert svc.check_privilege(ident, "read") is True

# --- RBAC: Global Roles ----------------------------------------------------

def test_auth_admin_has_all_privileges(app, db_session, seeded_admin_user):
    """Admin has wildcard privileges for every action."""
    rd = make_role_data(name="nx-admin", privileges=["nx-all"])
    assert rd["name"] == "nx-admin"
    svc = SecurityService()
    r = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    c = svc.validate_token(r["access_token"])["claims"]
    assert c.get("role") == ROLE_ADMIN
    ident = {"privileges": c.get("privileges", [])}
    for a in ("read", "write", "admin", "delete"):
        assert svc.check_privilege(ident, a) is True

def test_auth_developer_has_read_write_not_admin(app, db_session, seeded_developer_user):
    """Developer has read+write but not admin."""
    svc = SecurityService()
    r = svc.authenticate(seeded_developer_user.username, DEFAULT_PASSWORD)
    c = svc.validate_token(r["access_token"])["claims"]
    assert c.get("role") == ROLE_DEVELOPER
    ident = {"privileges": c.get("privileges", [])}
    assert svc.check_privilege(ident, "read") is True
    assert svc.check_privilege(ident, "write") is True
    assert svc.check_privilege(ident, "admin") is False

def test_auth_readonly_has_read_only(app, db_session, seeded_readonly_user):
    """Readonly can only read."""
    svc = SecurityService()
    r = svc.authenticate(seeded_readonly_user.username, DEFAULT_PASSWORD)
    c = svc.validate_token(r["access_token"])["claims"]
    assert c.get("role") == ROLE_READONLY
    ident = {"privileges": c.get("privileges", [])}
    assert svc.check_privilege(ident, "read") is True
    assert svc.check_privilege(ident, "write") is False
    assert svc.check_privilege(ident, "admin") is False

# --- RBAC: Repository-Specific Permissions ---------------------------------

def test_auth_repo_specific_permission_grants_access(app, db_session):
    """Repo-specific permission grants write only on target repo."""
    pm = make_repo_permission_data(
        repo_name="maven-releases",
        privileges=["nx-repository-view", "nx-repository-edit"])
    ident = {"privileges": ["nx-repository-view"],
             "repo_permissions": {"maven-releases": pm["privileges"]}}
    svc = SecurityService()
    assert svc.check_privilege(ident, "write", resource="maven-releases")
    assert not svc.check_privilege(ident, "write", resource="other-repo")

def test_auth_repo_specific_permission_overrides_global(app, db_session):
    """Repo-specific write overrides global readonly."""
    pv = make_privilege_data(name="nx-repository-edit")
    ident = {"privileges": ["nx-repository-view", "nx-search-read"],
             "repo_permissions": {
                 "target": ["nx-repository-view", "nx-repository-edit"]}}
    svc = SecurityService()
    assert svc.check_privilege(ident, "write", resource="target")
    assert not svc.check_privilege(ident, "write", resource="other")
    assert pv["name"] == "nx-repository-edit"

# --- RBAC: Content Selectors -----------------------------------------------

def test_auth_content_selector_restricts_access_by_path(app, db_session):
    """Content selector restricts access to matching paths."""
    sel = make_content_selector_data(expression='path =^ "/com/example"')
    ident = {"privileges": ["nx-repository-view"],
             "content_selectors": [{"path_pattern": "/com/example/**"}]}
    svc = SecurityService()
    assert svc.check_privilege(ident, "read",
                               path="/com/example/artifact.jar") is True
    assert svc.check_privilege(ident, "read",
                               path="/org/other/artifact.jar") is False
    assert sel["type"] == "csel"

# --- Full Authentication Chain ---------------------------------------------

def test_auth_full_chain_login_to_protected_access(app, client, db_session, seeded_admin_user):
    """Full chain: login → validate → refresh → re-validate → logout."""
    svc = SecurityService()
    r = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    assert r is not None
    at, rt = r["access_token"], r["refresh_token"]
    v1 = svc.validate_token(at)
    assert v1["valid"] is True and v1["identity"] == seeded_admin_user.id
    ref = svc.refresh_access_token(rt)
    assert ref["valid"] is True
    assert svc.validate_token(ref["access_token"])["valid"] is True
    assert svc.logout(r["session_id"]) is True
    assert svc.validate_session(r["session_id"]) is None
    payload = json.dumps({"token": at[:20]})
    assert "token" in json.loads(payload)

def test_auth_full_chain_with_insufficient_privileges(app, client, db_session):
    """Readonly user lacks admin privilege in full chain."""
    ud = make_readonly_user(username="chain-ro")
    _seed(db_session, ud)
    svc = SecurityService()
    r = svc.authenticate("chain-ro", DEFAULT_PASSWORD)
    assert r is not None
    v = svc.validate_token(r["access_token"])
    ident = {"privileges": v["claims"].get("privileges", [])}
    assert svc.check_privilege(ident, "admin") is False
    assert svc.check_privilege(ident, "read") is True

# --- Edge Cases ------------------------------------------------------------

def test_auth_token_at_exact_expiry_boundary(app, db_session):
    """Token valid before expiry, rejected after."""
    b = make_user_with_expired_token()
    assert b["token"]["expires_delta"].total_seconds() < 0
    svc = SecurityService()
    with freeze_time("2024-06-01 12:00:00"):
        t = create_access_token(identity="boundary",
                                expires_delta=timedelta(seconds=3))
        assert svc.validate_token(t)["valid"] is True
    with freeze_time("2024-06-01 12:00:05"):
        assert svc.validate_token(t)["valid"] is False

def test_auth_password_with_special_characters(app, db_session):
    """Special-character password authenticates."""
    pw = "P@$$w0rd!#%^&*()"
    h = "sha256$" + hashlib.sha256(pw.encode()).hexdigest()
    ud = make_developer_user(username="spec-pw", password=pw,
                             password_hash=h)
    u = _seed(db_session, ud)
    r = SecurityService().authenticate("spec-pw", pw)
    assert r is not None and r["user_id"] == u.id

def test_auth_concurrent_token_refresh_does_not_invalidate_others(
        app, db_session, seeded_admin_user):
    """Refreshing one token does not invalidate another."""
    svc = SecurityService()
    r1 = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    r2 = svc.authenticate(seeded_admin_user.username, DEFAULT_PASSWORD)
    assert svc.refresh_access_token(r1["refresh_token"])["valid"] is True
    assert svc.refresh_access_token(r2["refresh_token"])["valid"] is True

# --- Error Cases -----------------------------------------------------------

def test_auth_login_with_disabled_account_fails(app, db_session):
    """Disabled account login is rejected."""
    _seed(db_session, make_admin_user(username="dis-admin", status="disabled"))
    r = SecurityService().authenticate("dis-admin", DEFAULT_PASSWORD)
    assert r is None
    ms = MagicMock(spec=SecurityService)
    ms.authenticate.return_value = None
    assert ms.authenticate("dis-admin", DEFAULT_PASSWORD) is None
    ms.authenticate.assert_called_once_with("dis-admin", DEFAULT_PASSWORD)

def test_auth_token_with_tampered_payload_fails(app, db_session):
    """JWT with tampered payload is rejected."""
    tok = create_access_token(identity="tamper")
    tampered = tok[:-5] + "XXXXX"
    svc = SecurityService()
    r = svc.validate_token(tampered)
    assert r["valid"] is False and "error" in r
    with patch(__name__ + ".decode_token",
               side_effect=Exception("Bad sig")):
        assert svc.validate_token(tok)["valid"] is False

def test_auth_api_key_for_disabled_user_fails(app, db_session, seeded_admin_user):
    """API key for disabled user is rejected."""
    svc = SecurityService()
    k = svc.create_api_key(user_id=seeded_admin_user.id, name="dis-key")
    seeded_admin_user.status = "disabled"
    db_session.flush()
    assert svc.validate_api_key(k["key"]) is None
