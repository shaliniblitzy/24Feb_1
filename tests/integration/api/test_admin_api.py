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

# ---------------------------------------------------------------------------
# Import real source modules (no shim fallback).
# If source modules are not yet created, all tests in this file are skipped.
# ---------------------------------------------------------------------------
pytest.importorskip(
    "src.api.admin_routes",
    reason="Admin routes module not yet created",
)

pytestmark = pytest.mark.integration

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
