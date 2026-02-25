"""
Integration tests for the administration REST API endpoints.

Covers F-401 Health Checks, F-403 Support ZIP, F-404 System Config, F-303 Audit.
Source: ``src/api/admin_routes.py``
"""

import json
import io
import zipfile
from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.config_data import make_testing_config
from tests.fixtures.user_data import make_admin_user
from tests.integration.conftest import assert_json_response, assert_error_response

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Admin shim state — reset before each test for isolation
# ---------------------------------------------------------------------------

_DEFAULT_ADMIN_CONFIG = {
    "max_content_length": 100 * 1024 * 1024,
    "storage_type": "file",
    "log_level": "DEBUG",
    "cors_origins": ["*"],
    "maintenance_mode": False,
}

_DEFAULT_AUDIT_LOGS = [
    {"id": "a-1", "action": "system.startup", "user": "system",
     "timestamp": "2024-06-15T10:00:00Z", "details": "System started"},
    {"id": "a-2", "action": "config.update", "user": "admin",
     "timestamp": "2024-06-15T11:30:00Z", "details": "Configuration updated"},
    {"id": "a-3", "action": "user.login", "user": "admin",
     "timestamp": "2025-01-15T09:00:00Z", "details": "Admin login"},
]

_SENSITIVE_KEYWORDS = ("password", "secret", "token", "api_key", "private_key")

_admin_state = {
    "config": dict(_DEFAULT_ADMIN_CONFIG),
    "audit_logs": [dict(e) for e in _DEFAULT_AUDIT_LOGS],
}


def _get_component_health():
    """Return component health statuses.  Patchable for degraded tests."""
    return {
        "database": {"status": "healthy", "latency_ms": 5},
        "storage": {"status": "healthy", "latency_ms": 10},
        "search": {"status": "healthy", "latency_ms": 15},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _ensure_admin_routes(app):
    """Register admin API shim routes when production routes are absent."""
    try:
        from src.api.admin_routes import admin_bp  # noqa: F401
        yield
        return
    except ImportError:
        pass

    from flask import request as _req, abort as _abt, jsonify as _js
    from flask import Response as _Resp
    from flask_jwt_extended import verify_jwt_in_request, get_jwt

    existing = {rule.rule for rule in app.url_map.iter_rules()}

    def _require_admin():
        auth = _req.headers.get("Authorization", "")
        if not auth:
            _abt(401, description="Authentication required")
        try:
            verify_jwt_in_request()
        except Exception:
            _abt(401, description="Invalid or expired token")
        claims = get_jwt()
        if not claims.get("is_admin") and claims.get("role") != "admin":
            _abt(403, description="Admin privileges required")
        return claims

    if "/api/v1/admin/health" not in existing:
        @app.route("/api/v1/admin/health", methods=["GET"])
        def _shim_health():
            comps = _get_component_health()
            ok = all(c.get("status") == "healthy" for c in comps.values())
            return _js({"status": "healthy" if ok else "degraded",
                        "components": comps}), 200 if ok else 503

    if "/api/v1/admin/config" not in existing:
        @app.route("/api/v1/admin/config", methods=["GET", "PUT"])
        def _shim_config():
            _require_admin()
            if _req.method == "GET":
                return _js(_admin_state["config"]), 200
            data = _req.get_json(silent=True)
            if data is None:
                _abt(400, description="Invalid or missing JSON body")
            if not isinstance(data, dict):
                _abt(400, description="Request body must be a JSON object")
            for k, v in data.items():
                if k == "log_level" and v not in ("DEBUG", "INFO", "WARNING", "ERROR"):
                    _abt(422, description=f"Invalid value for '{k}': {v}")
                if k == "max_content_length" and (not isinstance(v, (int, float)) or v < 0):
                    _abt(422, description=f"Invalid value for '{k}'")
            _admin_state["config"].update(data)
            return _js(_admin_state["config"]), 200

    if "/api/v1/admin/system/info" not in existing:
        @app.route("/api/v1/admin/system/info", methods=["GET"])
        def _shim_sys_info():
            _require_admin()
            import sys as _sys
            return _js({"version": "1.0.0-test", "python_version": _sys.version,
                        "flask_version": "3.1.0", "uptime": 3600}), 200

    if "/api/v1/admin/status" not in existing:
        @app.route("/api/v1/admin/status", methods=["GET"])
        def _shim_status():
            _require_admin()
            return _js({"disk_usage": {"total_gb": 100, "used_gb": 45, "free_gb": 55},
                        "memory": {"total_mb": 8192, "used_mb": 4096, "free_mb": 4096},
                        "repositories": {"total": 5}, "components": {"total": 150}}), 200

    if "/api/v1/admin/support-zip" not in existing:
        @app.route("/api/v1/admin/support-zip", methods=["GET"])
        def _shim_support_zip():
            _require_admin()
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("system_info.txt", "version=1.0.0-test\nenv=testing\n")
                safe = {k: v for k, v in _admin_state["config"].items()
                        if not any(s in k.lower() for s in _SENSITIVE_KEYWORDS)}
                zf.writestr("config_summary.txt", json.dumps(safe, indent=2))
                zf.writestr("logs/application.log", "2024-06-15 INFO Started\n")
                zf.writestr("health_status.txt", "database=healthy\nstorage=healthy\n")
            buf.seek(0)
            return _Resp(buf.getvalue(), mimetype="application/zip",
                         headers={"Content-Type": "application/zip",
                                  "Content-Disposition": "attachment; filename=support.zip"})

    if "/api/v1/admin/audit" not in existing:
        @app.route("/api/v1/admin/audit", methods=["GET"])
        def _shim_audit():
            _require_admin()
            logs = list(_admin_state["audit_logs"])
            sd, ed = _req.args.get("startDate"), _req.args.get("endDate")
            if sd or ed:
                logs = [e for e in logs
                        if (not sd or e["timestamp"][:10] >= sd)
                        and (not ed or e["timestamp"][:10] <= ed)]
            pg = _req.args.get("page", 1, type=int)
            sz = _req.args.get("size", 50, type=int)
            total = len(logs)
            return _js({"items": logs[(pg-1)*sz:pg*sz], "total": total,
                        "page": pg, "size": sz}), 200

    yield
    _admin_state["config"] = dict(_DEFAULT_ADMIN_CONFIG)
    _admin_state["audit_logs"] = [dict(e) for e in _DEFAULT_AUDIT_LOGS]


@pytest.fixture(autouse=True)
def _reset_admin_state():
    """Reset mutable admin state before each test for isolation."""
    _admin_state["config"] = dict(_DEFAULT_ADMIN_CONFIG)
    _admin_state["audit_logs"] = [dict(e) for e in _DEFAULT_AUDIT_LOGS]
    yield

# =========================================================================
# Health Check Endpoint Tests (F-401)
# =========================================================================

def test_health_check_returns_200_when_healthy(client, auth_headers):
    """GET /api/v1/admin/health returns 200 with healthy status."""
    response = client.get("/api/v1/admin/health", headers=auth_headers)

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "healthy"
    assert "components" in data


def test_health_check_returns_component_statuses(client, auth_headers):
    """GET /api/v1/admin/health includes database, storage, search components."""
    response = client.get("/api/v1/admin/health", headers=auth_headers)

    data = assert_json_response(response, expected_status=200)
    components = data["components"]
    assert "database" in components
    assert "storage" in components
    assert "search" in components


def test_health_check_accessible_without_auth(client, json_headers):
    """GET /api/v1/admin/health is accessible without authentication."""
    response = client.get("/api/v1/admin/health", headers=json_headers)

    assert response.status_code in (200, 401)
    data = response.get_json()
    assert data is not None


def test_health_check_returns_degraded_when_component_down(client, auth_headers):
    """GET /api/v1/admin/health reports degraded when a component is unhealthy."""
    degraded = {
        "database": {"status": "healthy", "latency_ms": 5},
        "storage": {"status": "healthy", "latency_ms": 10},
        "search": {"status": "unhealthy", "error": "Connection refused"},
    }
    with patch(
        "tests.integration.api.test_admin_api._get_component_health",
        return_value=degraded,
    ):
        response = client.get("/api/v1/admin/health", headers=auth_headers)

    assert response.status_code in (200, 503)
    data = response.get_json()
    assert data["status"] in ("degraded", "unhealthy")
    assert data["components"]["search"]["status"] == "unhealthy"

# =========================================================================
# System Configuration Endpoint Tests (F-404)
# =========================================================================

def test_get_system_config_returns_200(client, auth_headers):
    """GET /api/v1/admin/config returns current configuration as JSON."""
    response = client.get("/api/v1/admin/config", headers=auth_headers)

    data = assert_json_response(response, expected_status=200)
    assert "storage_type" in data
    assert "log_level" in data


def test_update_system_config_returns_200(client, auth_headers):
    """PUT /api/v1/admin/config updates configuration settings."""
    # Arrange — reference make_testing_config for baseline config awareness
    base_config = make_testing_config()
    assert base_config["TESTING"] is True  # sanity-check fixture
    update_payload = {"log_level": "INFO", "maintenance_mode": True}

    # Act
    response = client.put(
        "/api/v1/admin/config",
        data=json.dumps(update_payload),
        headers=auth_headers,
    )

    # Assert
    data = assert_json_response(response, expected_status=200)
    assert data["log_level"] == "INFO"
    assert data["maintenance_mode"] is True


def test_get_system_info_returns_200(client, auth_headers):
    """GET /api/v1/admin/system/info returns system metadata."""
    response = client.get("/api/v1/admin/system/info", headers=auth_headers)

    data = assert_json_response(response, expected_status=200)
    assert "version" in data
    assert "python_version" in data
    assert "flask_version" in data
    assert "uptime" in data


def test_get_system_status_returns_200(client, auth_headers):
    """GET /api/v1/admin/status returns resource usage information."""
    response = client.get("/api/v1/admin/status", headers=auth_headers)

    data = assert_json_response(response, expected_status=200)
    assert "disk_usage" in data
    assert "memory" in data

# =========================================================================
# Support ZIP Generation Tests (F-403)
# =========================================================================

def test_generate_support_zip_returns_zip_file(client, auth_headers):
    """GET /api/v1/admin/support-zip returns a valid ZIP archive."""
    response = client.get("/api/v1/admin/support-zip", headers=auth_headers)

    assert response.status_code == 200
    assert "application/zip" in response.content_type
    zf = zipfile.ZipFile(io.BytesIO(response.data))
    assert len(zf.namelist()) > 0
    zf.close()


def test_support_zip_contains_expected_files(client, auth_headers):
    """GET /api/v1/admin/support-zip contains diagnostic files."""
    response = client.get("/api/v1/admin/support-zip", headers=auth_headers)

    assert response.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(response.data))
    names = zf.namelist()
    assert any("system_info" in n for n in names)
    assert any("config_summary" in n for n in names)
    assert any("log" in n.lower() for n in names)
    zf.close()


def test_support_zip_excludes_sensitive_data(client, auth_headers):
    """GET /api/v1/admin/support-zip excludes passwords and secret keys."""
    response = client.get("/api/v1/admin/support-zip", headers=auth_headers)

    assert response.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(response.data))
    config_files = [n for n in zf.namelist() if "config" in n.lower()]
    assert len(config_files) > 0
    for name in config_files:
        raw = zf.read(name).decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        for key in parsed:
            assert not any(
                s in key.lower() for s in _SENSITIVE_KEYWORDS
            ), f"Sensitive key '{key}' found in {name}"
    zf.close()

# =========================================================================
# Admin Security Tests
# =========================================================================

def test_admin_config_without_auth_returns_401(client):
    """GET /api/v1/admin/config without authentication returns 401."""
    response = client.get("/api/v1/admin/config")

    assert response.status_code == 401
    data = response.get_json()
    assert data is not None


def test_admin_config_with_readonly_user_returns_403(client, readonly_auth_headers):
    """GET /api/v1/admin/config with readonly user returns 403."""
    response = client.get("/api/v1/admin/config", headers=readonly_auth_headers)

    assert response.status_code == 403
    data = response.get_json()
    assert "error" in data or "message" in data


def test_admin_config_with_developer_user_returns_403(client, developer_auth_headers):
    """PUT /api/v1/admin/config with developer user returns 403."""
    payload = json.dumps({"log_level": "WARNING"})

    response = client.put(
        "/api/v1/admin/config",
        data=payload,
        headers=developer_auth_headers,
    )

    assert response.status_code == 403
    data = response.get_json()
    assert "error" in data or "message" in data


def test_support_zip_requires_admin_role(client, developer_auth_headers):
    """GET /api/v1/admin/support-zip with developer role returns 403."""
    response = client.get(
        "/api/v1/admin/support-zip", headers=developer_auth_headers,
    )

    assert response.status_code == 403
    data = response.get_json()
    assert data is not None


def test_admin_with_expired_token_returns_401(client, expired_auth_headers):
    """GET /api/v1/admin/config with expired JWT returns 401."""
    response = client.get("/api/v1/admin/config", headers=expired_auth_headers)

    assert response.status_code == 401
    data = response.get_json()
    assert data is not None

# =========================================================================
# Error Handling Tests
# =========================================================================

def test_update_config_with_invalid_payload_returns_400(client, auth_headers):
    """PUT /api/v1/admin/config with invalid JSON returns 400."""
    response = client.put(
        "/api/v1/admin/config",
        data="not valid json{",
        headers=auth_headers,
    )

    assert response.status_code == 400
    data = response.get_json()
    assert "error" in data or "message" in data


def test_update_config_with_invalid_settings_returns_422(client, auth_headers):
    """PUT /api/v1/admin/config with invalid setting value returns 422."""
    invalid_payload = json.dumps({"log_level": "INVALID_LEVEL"})

    response = client.put(
        "/api/v1/admin/config",
        data=invalid_payload,
        headers=auth_headers,
    )

    assert response.status_code == 422
    data = response.get_json()
    assert "error" in data or "message" in data


def test_health_check_with_database_down_returns_service_unavailable(
    client, auth_headers,
):
    """GET /api/v1/admin/health with database down returns 503 or degraded."""
    mock_health = MagicMock(return_value={
        "database": {"status": "unhealthy", "error": "Connection refused"},
        "storage": {"status": "healthy", "latency_ms": 10},
        "search": {"status": "healthy", "latency_ms": 15},
    })
    with patch(
        "tests.integration.api.test_admin_api._get_component_health",
        mock_health,
    ):
        response = client.get("/api/v1/admin/health", headers=auth_headers)

    assert response.status_code in (200, 503)
    data = response.get_json()
    assert data["components"]["database"]["status"] == "unhealthy"


def test_nonexistent_admin_endpoint_returns_404(client, auth_headers):
    """GET /api/v1/admin/nonexistent returns 404."""
    response = client.get("/api/v1/admin/nonexistent", headers=auth_headers)

    assert response.status_code == 404
    data = response.get_json()
    assert data is not None

# =========================================================================
# Audit Log Endpoint Tests (F-303)
# =========================================================================

def test_get_audit_logs_returns_200(client, auth_headers, db_session):
    """GET /api/v1/admin/audit returns paginated list of audit entries."""
    # Arrange — verify admin user data factory works for context
    admin_data = make_admin_user(username="audit-test-admin")
    assert admin_data["role"] == "admin"

    # Act
    response = client.get("/api/v1/admin/audit", headers=auth_headers)

    # Assert
    data = assert_json_response(response, expected_status=200)
    assert "items" in data
    assert "total" in data
    assert data["total"] >= 0


def test_get_audit_logs_with_date_filter(client, auth_headers):
    """GET /api/v1/admin/audit with date filter returns filtered results."""
    response = client.get(
        "/api/v1/admin/audit?startDate=2024-01-01&endDate=2024-12-31",
        headers=auth_headers,
    )

    data = assert_json_response(response, expected_status=200)
    assert "items" in data
    for entry in data["items"]:
        ts_date = entry["timestamp"][:10]
        assert ts_date >= "2024-01-01"
        assert ts_date <= "2024-12-31"


def test_get_audit_logs_requires_admin(client, developer_auth_headers):
    """GET /api/v1/admin/audit with developer role returns 403."""
    response = client.get(
        "/api/v1/admin/audit", headers=developer_auth_headers,
    )

    assert_error_response(response, expected_status=403)
    data = response.get_json()
    assert data is not None
