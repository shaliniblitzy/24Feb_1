"""
Full HTTP lifecycle integration tests for scheduled task management endpoints.

Covers CRUD operations, execution triggers, and execution history at
``/api/v1/tasks``.  Uses Flask's test_client() with in-memory SQLite.

Source file under test: ``src/api/task_routes.py``
"""

from __future__ import annotations

import json
import datetime
from unittest.mock import patch, MagicMock

import pytest

# Module-level marker — all tests in this file are integration tests.
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task_payload(
    name: str = "nightly-cleanup",
    task_type: str = "cleanup",
    cron_expression: str = "0 0 * * *",
    enabled: bool = True,
    **overrides,
) -> dict:
    """Build a task creation/update payload."""
    payload: dict = {
        "name": name,
        "type": task_type,
        "cron_expression": cron_expression,
        "enabled": enabled,
    }
    payload.update(overrides)
    return payload


def _create_task_via_api(client, auth_headers, **kwargs) -> dict:
    """POST a task through the API; return parsed JSON. Fails if not 2xx."""
    payload = _make_task_payload(**kwargs)
    resp = client.post("/api/v1/tasks", json=payload, headers=auth_headers)
    assert resp.status_code in (200, 201), (
        f"Task creation failed ({resp.status_code}): "
        f"{resp.get_data(as_text=True)[:300]}"
    )
    return resp.get_json()


# =========================================================================
# CRUD — Happy Path
# =========================================================================


class TestTaskCRUDHappyPath:
    """Happy-path CRUD for scheduled tasks."""

    def test_create_scheduled_task_returns_201(self, client, auth_headers):
        """POST /api/v1/tasks → 201 with created task data."""
        payload = _make_task_payload(
            name="daily-index-rebuild",
            task_type="maintenance",
            cron_expression="0 2 * * *",
        )

        response = client.post("/api/v1/tasks", json=payload, headers=auth_headers)

        assert response.status_code == 201
        data = response.get_json()
        assert data is not None
        assert data["name"] == "daily-index-rebuild"
        assert data["type"] == "maintenance"
        assert "id" in data

    def test_list_scheduled_tasks_returns_200(self, client, auth_headers, db_session):
        """GET /api/v1/tasks → 200 with a list containing seeded tasks."""
        for i in range(3):
            _create_task_via_api(client, auth_headers, name=f"list-task-{i}")

        response = client.get("/api/v1/tasks", headers=auth_headers)

        assert response.status_code == 200
        data = response.get_json()
        assert data is not None
        items = data if isinstance(data, list) else data.get("items", data.get("data", []))
        assert len(items) >= 3

    def test_get_scheduled_task_by_id_returns_200(self, client, auth_headers, db_session):
        """GET /api/v1/tasks/{id} → 200 with matching task."""
        created = _create_task_via_api(client, auth_headers, name="fetch-test")
        task_id = created["id"]

        response = client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)

        assert response.status_code == 200
        data = response.get_json()
        assert data["id"] == task_id
        assert data["name"] == "fetch-test"

    def test_update_scheduled_task_returns_200(self, client, auth_headers, db_session):
        """PUT /api/v1/tasks/{id} → 200 with updated fields."""
        created = _create_task_via_api(client, auth_headers, name="update-target", enabled=True)
        task_id = created["id"]

        response = client.put(
            f"/api/v1/tasks/{task_id}",
            json={"name": "updated-name", "enabled": False},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["name"] == "updated-name"
        assert data["enabled"] is False

    def test_delete_scheduled_task_returns_204(self, client, auth_headers, db_session):
        """DELETE /api/v1/tasks/{id} → 204; subsequent GET → 404."""
        created = _create_task_via_api(client, auth_headers, name="delete-target")
        task_id = created["id"]

        response = client.delete(f"/api/v1/tasks/{task_id}", headers=auth_headers)

        assert response.status_code == 204
        verify = client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)
        assert verify.status_code == 404


# =========================================================================
# Execution Triggers
# =========================================================================


class TestTaskExecutionTrigger:
    """Task execution trigger and history endpoints."""

    def test_trigger_task_execution_returns_accepted(self, client, auth_headers, db_session):
        """POST /api/v1/tasks/{id}/run on enabled task → 200 or 202."""
        created = _create_task_via_api(client, auth_headers, name="trigger-test", enabled=True)
        task_id = created["id"]

        response = client.post(f"/api/v1/tasks/{task_id}/run", headers=auth_headers)

        assert response.status_code in (200, 202)
        data = response.get_json()
        assert data is not None

    def test_trigger_disabled_task_returns_400(self, client, auth_headers, db_session):
        """POST /api/v1/tasks/{id}/run on disabled task → 400."""
        created = _create_task_via_api(client, auth_headers, name="disabled-trigger", enabled=False)
        task_id = created["id"]

        response = client.post(f"/api/v1/tasks/{task_id}/run", headers=auth_headers)

        assert response.status_code == 400
        data = response.get_json()
        assert data is not None
        msg = data.get("error", data.get("message", "")).lower()
        assert "disabled" in msg or "enabled" in msg

    def test_get_task_execution_history_returns_200(self, client, auth_headers, db_session):
        """GET /api/v1/tasks/{id}/history → 200 with execution records."""
        created = _create_task_via_api(client, auth_headers, name="history-test", enabled=True)
        task_id = created["id"]
        # Generate at least one execution record
        client.post(f"/api/v1/tasks/{task_id}/run", headers=auth_headers)

        response = client.get(f"/api/v1/tasks/{task_id}/history", headers=auth_headers)

        assert response.status_code == 200
        data = response.get_json()
        assert data is not None
        records = data if isinstance(data, list) else data.get("items", data.get("data", []))
        assert isinstance(records, list)


# =========================================================================
# Task Types and Configuration
# =========================================================================


class TestTaskTypesAndConfig:
    """Cleanup, maintenance task types and configuration updates."""

    def test_create_cleanup_task_returns_201(self, client, auth_headers):
        """POST /api/v1/tasks with type=cleanup → 201."""
        payload = _make_task_payload(
            name="repo-cleanup-daily",
            task_type="cleanup",
            cron_expression="0 3 * * *",
            config={"repository": "maven-releases", "policy": "remove-old-snapshots"},
        )

        response = client.post("/api/v1/tasks", json=payload, headers=auth_headers)

        assert response.status_code == 201
        data = response.get_json()
        assert data["type"] == "cleanup"
        assert data["name"] == "repo-cleanup-daily"

    def test_create_maintenance_task_returns_201(self, client, auth_headers):
        """POST /api/v1/tasks with type=maintenance → 201."""
        payload = _make_task_payload(
            name="compact-blobstore",
            task_type="maintenance",
            cron_expression="0 4 * * 0",
            config={"action": "compact-blobstore", "target": "default"},
        )

        response = client.post("/api/v1/tasks", json=payload, headers=auth_headers)

        assert response.status_code == 201
        data = response.get_json()
        assert data["type"] == "maintenance"
        assert "id" in data

    def test_update_task_cron_expression_returns_200(self, client, auth_headers, db_session):
        """PUT /api/v1/tasks/{id} updates cron_expression."""
        created = _create_task_via_api(
            client, auth_headers, name="cron-update", cron_expression="0 0 * * *",
        )
        task_id = created["id"]

        response = client.put(
            f"/api/v1/tasks/{task_id}",
            json={"cron_expression": "0 */6 * * *"},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["cron_expression"] == "0 */6 * * *"

    def test_enable_disabled_task_returns_200(self, client, auth_headers, db_session):
        """PUT /api/v1/tasks/{id} re-enables a disabled task."""
        created = _create_task_via_api(
            client, auth_headers, name="enable-test", enabled=False,
        )
        task_id = created["id"]

        response = client.put(
            f"/api/v1/tasks/{task_id}",
            json={"enabled": True},
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["enabled"] is True


# =========================================================================
# Edge Cases
# =========================================================================


class TestTaskEdgeCases:
    """Boundary conditions, invalid inputs, and empty-state scenarios."""

    def test_create_task_with_invalid_cron_returns_400(self, client, auth_headers):
        """POST /api/v1/tasks with malformed cron → 400."""
        payload = _make_task_payload(name="bad-cron", cron_expression="invalid-cron")

        response = client.post("/api/v1/tasks", json=payload, headers=auth_headers)

        assert response.status_code == 400
        data = response.get_json()
        assert data is not None
        msg = data.get("error", data.get("message", "")).lower()
        assert "cron" in msg or "invalid" in msg

    def test_create_task_with_duplicate_name_returns_409(self, client, auth_headers, db_session):
        """POST /api/v1/tasks with existing name → 409 or 400."""
        _create_task_via_api(client, auth_headers, name="daily-cleanup")

        payload = _make_task_payload(name="daily-cleanup")
        response = client.post("/api/v1/tasks", json=payload, headers=auth_headers)

        assert response.status_code in (400, 409)
        assert response.get_json() is not None

    def test_get_nonexistent_task_returns_404(self, client, auth_headers):
        """GET /api/v1/tasks/{bad_id} → 404."""
        response = client.get("/api/v1/tasks/nonexistent-999", headers=auth_headers)

        assert response.status_code == 404
        assert response.get_json() is not None

    def test_delete_nonexistent_task_returns_404(self, client, auth_headers):
        """DELETE /api/v1/tasks/{bad_id} → 404."""
        response = client.delete("/api/v1/tasks/nonexistent-999", headers=auth_headers)

        assert response.status_code == 404
        assert response.get_json() is not None

    def test_list_tasks_empty_returns_200(self, client, auth_headers, clean_db):
        """GET /api/v1/tasks when DB is empty → 200 with empty list."""
        response = client.get("/api/v1/tasks", headers=auth_headers)

        assert response.status_code == 200
        data = response.get_json()
        assert data is not None
        items = data if isinstance(data, list) else data.get("items", data.get("data", []))
        assert len(items) == 0


# =========================================================================
# Security and Authorization
# =========================================================================


class TestTaskSecurity:
    """Authentication and authorization enforcement."""

    def test_create_task_without_auth_returns_401(self, client):
        """POST /api/v1/tasks without Authorization → 401."""
        payload = _make_task_payload(name="no-auth-task")

        response = client.post(
            "/api/v1/tasks",
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 401
        assert response.get_json() is not None

    def test_create_task_with_readonly_user_returns_403(self, client, readonly_auth_headers):
        """POST /api/v1/tasks with readonly JWT → 403."""
        payload = _make_task_payload(name="readonly-attempt")

        response = client.post("/api/v1/tasks", json=payload, headers=readonly_auth_headers)

        assert response.status_code == 403
        assert response.get_json() is not None

    def test_trigger_task_requires_admin(self, client, developer_auth_headers, auth_headers, db_session):
        """POST /api/v1/tasks/{id}/run with developer JWT → 403."""
        created = _create_task_via_api(client, auth_headers, name="admin-trigger", enabled=True)
        task_id = created["id"]

        response = client.post(f"/api/v1/tasks/{task_id}/run", headers=developer_auth_headers)

        assert response.status_code == 403
        assert response.get_json() is not None

    def test_delete_task_with_expired_token_returns_401(
        self, client, expired_auth_headers, auth_headers, db_session
    ):
        """DELETE /api/v1/tasks/{id} with expired JWT → 401."""
        created = _create_task_via_api(client, auth_headers, name="expired-del")
        task_id = created["id"]

        response = client.delete(f"/api/v1/tasks/{task_id}", headers=expired_auth_headers)

        assert response.status_code == 401
        assert response.get_json() is not None


# =========================================================================
# Error Handling
# =========================================================================


class TestTaskErrorHandling:
    """Missing fields, malformed payloads, readonly fields."""

    def test_create_task_with_missing_required_fields_returns_400(self, client, auth_headers):
        """POST /api/v1/tasks without name or type → 400."""
        payload = {"cron_expression": "0 0 * * *", "enabled": True}

        response = client.post("/api/v1/tasks", json=payload, headers=auth_headers)

        assert response.status_code == 400
        data = response.get_json()
        assert data is not None
        assert len(data.get("error", data.get("message", ""))) > 0

    def test_create_task_with_invalid_json_returns_400(self, client, auth_headers):
        """POST /api/v1/tasks with unparseable body → 400."""
        response = client.post(
            "/api/v1/tasks",
            data="this-is-not-valid-json",
            content_type="application/json",
            headers=auth_headers,
        )

        assert response.status_code == 400
        assert response.get_json() is not None

    def test_update_task_with_readonly_fields_ignored(self, client, auth_headers, db_session):
        """PUT /api/v1/tasks/{id} ignores readonly fields (id, created_at)."""
        created = _create_task_via_api(client, auth_headers, name="ro-fields-test")
        task_id = created["id"]
        original_created_at = created.get("created_at")

        response = client.put(
            f"/api/v1/tasks/{task_id}",
            json={
                "id": "overwritten-id",
                "created_at": "1999-01-01T00:00:00Z",
                "name": "still-updatable",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data["id"] == task_id
        assert data["name"] == "still-updatable"
        if original_created_at is not None:
            assert data.get("created_at") == original_created_at
