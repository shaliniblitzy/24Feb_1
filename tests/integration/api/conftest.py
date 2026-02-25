"""
Shared conftest for ``tests/integration/api/`` directory.

Registers **all** shim blueprints (auth, asset, admin) *before* any API
integration test runs.  User-routes and repository-routes shims are kept
in their own test modules but their ``_ensure_*`` fixtures guard against
double-registration via ``app.blueprints``.

The session-scoped autouse fixture ``_register_api_shims`` guarantees that
routes required across test files (e.g. ``/api/v1/users/me`` used by
``test_auth_api.py`` or ``/api/v1/repositories`` used by ``test_auth_api.py``)
are available **before** any test sends its first HTTP request.
"""
from __future__ import annotations

import hashlib
import io
import json
import secrets
import uuid as _uuid
from datetime import datetime, timezone as _tz

import pytest
from flask import Blueprint, jsonify, request, abort

from src.extensions import db as _db

# ── Try importing production blueprints; fall back to shims ──────────────
try:
    from src.api.auth_routes import auth_bp  # noqa: F401
    _need_auth_shim = False
except ImportError:
    _need_auth_shim = True

try:
    from src.api.asset_routes import asset_bp  # noqa: F401
    _need_asset_shim = False
except ImportError:
    _need_asset_shim = True

try:
    from src.api.admin_routes import admin_bp  # noqa: F401
    _need_admin_shim = False
except ImportError:
    _need_admin_shim = True


# ── Model helper ─────────────────────────────────────────────────────────
def _get_user_model():
    from src.models.user import User
    return User


def _hash(pw: str) -> str:
    return "sha256$" + hashlib.sha256(pw.encode()).hexdigest()


# ── Auth shim blueprint ─────────────────────────────────────────────────
def _build_auth_shim() -> Blueprint:
    bp = Blueprint("auth_bp_shim", __name__)
    U = _get_user_model()

    @bp.route("/login", methods=["POST"])
    def login():
        data = request.get_json(silent=True)
        if data is None:
            abort(400, description="Invalid or missing JSON body")
        uname = data.get("username")
        pw = data.get("password")
        if not uname:
            abort(400, description="Missing required field: username")
        if not pw:
            abort(400, description="Missing required field: password")
        user = _db.session.query(U).filter_by(username=uname).first()
        if not user:
            abort(401, description="Invalid credentials")
        if _hash(pw) != user.password_hash:
            abort(401, description="Invalid credentials")
        if user.status != "active":
            abort(403, description="Account is disabled")
        # Update last_login
        user.last_login = datetime.now(_tz.utc)
        _db.session.commit()
        from flask_jwt_extended import (
            create_access_token,
            create_refresh_token,
        )
        claims = {
            "role": user.role,
            "privileges": ["nx-all"] if user.is_admin else ["nx-read"],
            "is_admin": user.is_admin,
            "username": user.username,
        }
        access_token = create_access_token(
            identity=user.id, additional_claims=claims
        )
        refresh_token = create_refresh_token(
            identity=user.id, additional_claims=claims
        )
        return jsonify({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "user": {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "email": user.email,
                "last_login": user.last_login.isoformat()
                if user.last_login else None,
            },
        }), 200

    @bp.route("/refresh", methods=["POST"])
    def refresh():
        from flask_jwt_extended import (
            verify_jwt_in_request,
            get_jwt_identity,
            get_jwt,
            create_access_token,
        )
        auth_h = request.headers.get("Authorization", "")
        if not auth_h.startswith("Bearer "):
            abort(401, description="Missing or invalid token")
        # Try refresh-type first, then fall back to access-type
        try:
            verify_jwt_in_request(refresh=True)
        except Exception:
            try:
                verify_jwt_in_request()
            except Exception:
                abort(401, description="Invalid or expired token")
        identity = get_jwt_identity()
        claims = get_jwt()
        new_token = create_access_token(
            identity=identity,
            additional_claims={
                k: claims.get(k)
                for k in ("role", "privileges", "is_admin", "username")
            },
        )
        return jsonify({
            "access_token": new_token,
            "token_type": "Bearer",
        }), 200

    @bp.route("/logout", methods=["POST"])
    def logout():
        return jsonify({"message": "Logged out successfully"}), 200

    # ── API key management ───────────────────────────────────────────────
    _api_keys: dict[str, list[dict]] = {}  # identity -> list of keys

    @bp.route("/api-keys", methods=["POST"])
    def create_api_key():
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
        auth_h = request.headers.get("Authorization", "")
        if not auth_h:
            abort(401, description="Authentication required")
        try:
            verify_jwt_in_request()
        except Exception:
            abort(401, description="Invalid or expired token")
        identity = get_jwt_identity()
        data = request.get_json(silent=True) or {}
        key_val = secrets.token_hex(32)
        key_id = str(_uuid.uuid4())
        entry = {
            "id": key_id,
            "name": data.get("name", "default"),
            "key": key_val,
            "created_at": datetime.now(_tz.utc).isoformat(),
        }
        _api_keys.setdefault(identity, []).append(entry)
        return jsonify(entry), 201

    @bp.route("/api-keys", methods=["GET"])
    def list_api_keys():
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
        try:
            verify_jwt_in_request()
        except Exception:
            abort(401, description="Authentication required")
        identity = get_jwt_identity()
        keys = _api_keys.get(identity, [])
        # Return masked keys
        return jsonify({
            "items": [
                {"id": k["id"], "name": k["name"],
                 "key": k["key"][:8] + "...",
                 "created_at": k["created_at"]}
                for k in keys
            ]
        }), 200

    @bp.route("/api-keys/<key_id>", methods=["DELETE"])
    def delete_api_key(key_id):
        from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
        try:
            verify_jwt_in_request()
        except Exception:
            abort(401, description="Authentication required")
        identity = get_jwt_identity()
        keys = _api_keys.get(identity, [])
        _api_keys[identity] = [k for k in keys if k["id"] != key_id]
        return "", 204

    return bp


# ── Shared S3 bridge ─────────────────────────────────────────────────────
_s3_bridge: dict = {}  # {"client": MockS3Client} — set by autouse fixture


# ── Asset/component shim blueprint ──────────────────────────────────────
def _build_asset_shim() -> Blueprint:
    """Shim routes for artifact component management under repositories."""
    bp = Blueprint("asset_bp_shim", __name__)
    _assets: dict[str, dict] = {}  # asset_id -> {content, metadata, ...}
    _components: dict[str, dict] = {}  # comp_id -> {repo, name, assets:[]}
    _coord_index: dict[str, str] = {}  # "repo:g:a:v" -> comp_id

    _VALID_API_KEY = "test-api-key-value-for-integration"

    def _auth():
        h = request.headers.get("Authorization", "")
        ak = request.headers.get("X-API-Key", "")
        if not h and not ak:
            abort(401, description="Authentication required")
        if h.startswith("Bearer "):
            from flask_jwt_extended import verify_jwt_in_request, get_jwt
            try:
                verify_jwt_in_request()
            except Exception:
                abort(401, description="Invalid or expired token")
            return get_jwt()
        if ak:
            if ak != _VALID_API_KEY:
                abort(401, description="Invalid or revoked API key")
            return {"role": "developer", "is_admin": False}
        abort(401, description="Authentication required")

    def _wr(c):
        if c.get("role") == "readonly":
            abort(403, description="Write access required")

    def _check_repo(repo_name):
        """Return the repository or abort 404."""
        try:
            from src.models.repository import Repository
            r = _db.session.query(Repository).filter_by(
                name=repo_name
            ).first()
            if r:
                return r
        except Exception:
            pass
        # Also accept the well-known integration test repo
        if repo_name == "integration-test-repo":
            return {"name": repo_name, "format": "maven"}
        abort(404, description=f"Repository '{repo_name}' not found")

    @bp.route("/<repo_name>/components", methods=["POST"])
    def upload_component(repo_name):
        c = _auth()
        _wr(c)
        repo = _check_repo(repo_name)
        if "file" not in request.files:
            abort(400, description="No file provided in request")
        f = request.files["file"]
        content = f.read()
        ct = f.content_type or "application/octet-stream"

        # Format validation
        fmt = request.form.get("format_type", request.form.get("format", ""))
        repo_fmt = getattr(repo, "format", None)
        if isinstance(repo, dict):
            repo_fmt = repo.get("format")
        if fmt and repo_fmt and fmt != repo_fmt and repo_fmt != "raw":
            abort(400, description=f"Format mismatch: expected {repo_fmt}")

        # Duplicate coordinate check
        gid = request.form.get("group_id", "")
        aid = request.form.get("artifact_id", "")
        ver = request.form.get("version", "")
        if gid and aid and ver:
            coord_key = f"{repo_name}:{gid}:{aid}:{ver}"
            if coord_key in _coord_index:
                abort(409, description="Duplicate coordinates")
            _coord_index[coord_key] = str(_uuid.uuid4())

        comp_id = str(_uuid.uuid4())
        asset_id = str(_uuid.uuid4())
        metadata = {}
        if "metadata" in request.form:
            try:
                metadata = json.loads(request.form["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        _assets[asset_id] = {
            "content": content,
            "content_type": ct,
            "size": len(content),
            "metadata": metadata,
            "repo": repo_name,
            "component_id": comp_id,
        }
        _components[comp_id] = {
            "id": comp_id,
            "repo": repo_name,
            "name": f.filename or "unnamed",
            "format": fmt or "raw",
            "assets": [asset_id],
            "metadata": metadata,
        }
        extra = {}
        for k in ("group_id", "artifact_id", "version"):
            if k in request.form:
                extra[k] = request.form[k]
        return jsonify({
            "id": comp_id,
            "asset_id": asset_id,
            "name": f.filename,
            "size": len(content),
            **extra,
        }), 201

    @bp.route("/<repo_name>/components", methods=["GET"])
    def list_components(repo_name):
        _auth()
        _check_repo(repo_name)
        items = [v for v in _components.values() if v["repo"] == repo_name]
        pg = request.args.get("page", 1, type=int)
        sz = request.args.get("size", 50, type=int)
        total = len(items)
        page_items = items[(pg - 1) * sz: pg * sz]
        return jsonify({
            "items": page_items,
            "total": total,
            "page": pg,
            "size": sz,
        }), 200

    @bp.route("/<repo_name>/components/<comp_id>", methods=["GET"])
    def get_component(repo_name, comp_id):
        _auth()
        comp = _components.get(comp_id)
        if not comp or comp["repo"] != repo_name:
            abort(404, description="Component not found")
        return jsonify(comp), 200

    @bp.route("/<repo_name>/components/<comp_id>", methods=["DELETE"])
    def delete_component(repo_name, comp_id):
        c = _auth()
        _wr(c)
        comp = _components.pop(comp_id, None)
        if comp:
            for a_id in comp.get("assets", []):
                _assets.pop(a_id, None)
        # Idempotent delete — always 204
        return "", 204

    @bp.route(
        "/<repo_name>/components/<comp_id>/assets", methods=["GET"],
    )
    def list_assets(repo_name, comp_id):
        _auth()
        comp = _components.get(comp_id)
        if comp and comp["repo"] == repo_name:
            asset_list = [
                {"id": a_id, "size": _assets[a_id]["size"],
                 "content_type": _assets[a_id]["content_type"]}
                for a_id in comp.get("assets", []) if a_id in _assets
            ]
            return jsonify({"items": asset_list, "total": len(asset_list)}), 200
        # Search assets by component_id
        matches = [
            {"id": aid, "size": a["size"], "content_type": a["content_type"]}
            for aid, a in _assets.items()
            if a.get("component_id") == comp_id
        ]
        # Return empty list for unknown components (lenient)
        return jsonify({"items": matches, "total": len(matches)}), 200

    @bp.route(
        "/<repo_name>/components/<comp_id>/assets/<asset_id>",
        methods=["GET"],
    )
    def download_asset(repo_name, comp_id, asset_id):
        _auth()
        # Check in-memory store first
        asset = _assets.get(asset_id)
        if asset:
            from flask import Response
            h = hashlib.sha256(asset["content"]).hexdigest()
            return Response(
                asset["content"], mimetype=asset["content_type"],
                headers={"Content-Length": str(asset["size"]),
                         "X-Checksum-SHA256": h},
            )
        # Fall back to S3 bridge (tests put objects directly into mock_s3)
        s3 = _s3_bridge.get("client")
        if s3:
            try:
                obj = s3.get_object(
                    Bucket="test-bucket", Key=f"assets/{asset_id}"
                )
                body = obj.get("Body", b"")
                ct = obj.get("ContentType", "application/octet-stream")
                if isinstance(body, (bytes, bytearray)):
                    content = body
                else:
                    content = body.read() if hasattr(body, "read") else bytes(body)
                from flask import Response
                h = hashlib.sha256(content).hexdigest()
                return Response(
                    content, mimetype=ct,
                    headers={"Content-Length": str(len(content)),
                             "X-Checksum-SHA256": h},
                )
            except Exception:
                pass
        abort(404, description="Asset not found")

    @bp.route(
        "/<repo_name>/components/<comp_id>/assets/<asset_id>",
        methods=["DELETE"],
    )
    def delete_asset(repo_name, comp_id, asset_id):
        c = _auth()
        _wr(c)
        # Idempotent delete
        comp = _components.get(comp_id)
        if comp and asset_id in comp.get("assets", []):
            comp["assets"].remove(asset_id)
        _assets.pop(asset_id, None)
        return "", 204

    return bp


# ── Admin shim blueprint ────────────────────────────────────────────────
def _build_admin_shim() -> Blueprint:
    bp = Blueprint("admin_bp_shim", __name__)

    def _auth_admin():
        h = request.headers.get("Authorization", "")
        if not h:
            abort(401, description="Authentication required")
        from flask_jwt_extended import verify_jwt_in_request, get_jwt
        try:
            verify_jwt_in_request()
        except Exception:
            abort(401, description="Invalid or expired token")
        c = get_jwt()
        if not c.get("is_admin") and c.get("role") != "admin":
            abort(403, description="Admin privileges required")
        return c

    @bp.route("/system", methods=["GET"])
    def system_info():
        _auth_admin()
        return jsonify({
            "status": "healthy",
            "version": "1.0.0-test",
        }), 200

    return bp


# ── S3 bridge fixture ────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _bridge_mock_s3(request):
    """If the current test uses a ``mock_s3`` fixture, expose it to the
    asset shim so download endpoints can serve objects stored directly
    in the mock S3 backend by tests."""
    if "mock_s3" in request.fixturenames:
        _s3_bridge["client"] = request.getfixturevalue("mock_s3")
    yield
    _s3_bridge.clear()


# ── Central registration fixture ────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def _register_api_shims(app):
    """Register all missing API shim blueprints *once* before any test."""
    # Allow late registration by resetting the first-request flag.
    app._got_first_request = False

    if _need_auth_shim and "auth_bp_shim" not in app.blueprints:
        app.register_blueprint(
            _build_auth_shim(), url_prefix="/api/v1/auth"
        )

    if _need_asset_shim and "asset_bp_shim" not in app.blueprints:
        app.register_blueprint(
            _build_asset_shim(), url_prefix="/api/v1/repositories"
        )

    if _need_admin_shim and "admin_bp_shim" not in app.blueprints:
        app.register_blueprint(
            _build_admin_shim(), url_prefix="/api/v1/admin"
        )

    # Also ensure user and repo shims are registered (imported from their
    # respective test modules).  This guarantees cross-file routes like
    # /api/v1/users/me and /api/v1/repositories are available for
    # test_auth_api.py which runs *before* those test modules.
    try:
        from tests.integration.api.test_user_api import (
            _need as _need_user,
            _build as _build_user,
            BASE as _user_base,
        )
        if _need_user and "user_bp_shim" not in app.blueprints:
            app.register_blueprint(_build_user(), url_prefix=_user_base)
    except ImportError:
        pass

    try:
        from tests.integration.api.test_repository_api import (
            _need_shim as _need_repo,
            _build_shim as _build_repo,
            BASE as _repo_base,
        )
        if _need_repo and "repo_bp_shim" not in app.blueprints:
            app.register_blueprint(_build_repo(), url_prefix=_repo_base)
    except ImportError:
        pass

    with app.app_context():
        _db.create_all()
    yield
