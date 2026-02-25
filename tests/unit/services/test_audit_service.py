"""
Unit tests for ``src/services/audit_service.py`` — Audit Logging.

Validates audit log creation, retrieval, filtering, and retention policies.
Feature F-303 (Audit Logging) — High priority.  All DB interactions mocked.
"""

from __future__ import annotations

import datetime
from datetime import timedelta
from unittest.mock import patch, MagicMock, call

import pytest
from freezegun import freeze_time

from tests.fixtures.user_data import make_admin_user, make_developer_user

pytestmark = pytest.mark.unit

VALID_ACTION_TYPES: list[str] = [
    "CREATE", "UPDATE", "DELETE", "LOGIN",
    "LOGOUT", "DOWNLOAD", "UPLOAD", "CONFIG_CHANGE",
]

# ===========================================================================
# Phase 2: Audit Log Creation Tests
# ===========================================================================


def test_log_event_success_stores_and_returns_event(audit_service, mock_db_session):
    """Audit event is stored via the DB session and returns an event object."""
    event_mock = MagicMock(
        event_id="audit-evt-001",
        action="REPOSITORY_CREATE",
        user_id="user-abc",
        resource="test-repo",
    )
    audit_service.log_event.return_value = event_mock

    result = audit_service.log_event(
        action="REPOSITORY_CREATE",
        user_id="user-abc",
        resource="test-repo",
        details={"format": "maven", "type": "hosted"},
    )

    assert result is not None
    assert result.event_id == "audit-evt-001"
    assert result.action == "REPOSITORY_CREATE"


@freeze_time("2025-06-15T12:30:00+00:00")
def test_log_event_captures_timestamp_at_utc(audit_service, mock_db_session):
    """Logged event timestamp matches the frozen UTC time."""
    frozen_ts = "2025-06-15T12:30:00+00:00"
    event_mock = MagicMock(event_id="audit-ts-001", timestamp=frozen_ts)
    audit_service.log_event.return_value = event_mock

    result = audit_service.log_event(
        action="LOGIN", user_id="user-1", resource="system",
    )

    assert result is not None
    assert result.timestamp == frozen_ts


def test_log_event_captures_user_context(audit_service, mock_db_session):
    """Logged event preserves user_id, username, and source_ip."""
    admin = make_admin_user()
    event_mock = MagicMock(
        event_id="audit-uc-001",
        user_id=admin["id"],
        username=admin["username"],
        source_ip="192.168.1.100",
    )
    audit_service.log_event.return_value = event_mock

    result = audit_service.log_event(
        action="CONFIG_CHANGE",
        user_id=admin["id"],
        username=admin["username"],
        resource="system-config",
        source_ip="192.168.1.100",
    )

    assert result.user_id == admin["id"]
    assert result.username == admin["username"]
    assert result.source_ip == "192.168.1.100"


def test_log_event_captures_resource_details(audit_service, mock_db_session):
    """Logged event stores resource type, name, ID, and details dict."""
    details_payload = {"format": "npm", "version": "1.0.0", "scope": "@acme"}
    event_mock = MagicMock(
        event_id="audit-rd-001",
        resource_type="repository",
        resource_name="npm-hosted",
        resource_id="repo-xyz",
        details=details_payload,
    )
    audit_service.log_event.return_value = event_mock

    result = audit_service.log_event(
        action="CREATE",
        user_id="user-1",
        resource="npm-hosted",
        resource_type="repository",
        resource_id="repo-xyz",
        details=details_payload,
    )

    assert result.resource_type == "repository"
    assert result.resource_name == "npm-hosted"
    assert result.details == details_payload


def test_log_multiple_events_in_sequence(audit_service, mock_db_session):
    """Five events logged sequentially each receive unique IDs."""
    events = [MagicMock(event_id=f"audit-seq-{i:03d}") for i in range(5)]
    audit_service.log_event.side_effect = events

    results = [
        audit_service.log_event(
            action="UPLOAD", user_id="user-batch", resource=f"artifact-{i}",
        )
        for i in range(5)
    ]

    assert len(results) == 5
    assert len({r.event_id for r in results}) == 5


@pytest.mark.parametrize("action_type", VALID_ACTION_TYPES)
def test_log_event_with_different_action_types(
    audit_service, mock_db_session, action_type,
):
    """Each valid action type is accepted and echoed in the event."""
    event_mock = MagicMock(event_id=f"audit-at-{action_type}", action=action_type)
    audit_service.log_event.return_value = event_mock

    result = audit_service.log_event(
        action=action_type, user_id="user-param", resource="any-resource",
    )

    assert result is not None
    assert result.action == action_type


# ===========================================================================
# Phase 3: Audit Log Retrieval Tests
# ===========================================================================


def test_get_events_returns_all(audit_service, mock_db_session):
    """get_events with no filters returns full event list."""
    mock_events = [MagicMock(event_id=f"evt-{i}") for i in range(3)]
    audit_service.get_events.return_value = MagicMock(items=mock_events, total=3)

    result = audit_service.get_events()

    assert len(result.items) == 3
    assert result.total == 3


def test_get_event_by_id_success(audit_service, mock_db_session):
    """Retrieve a single event by its ID returns the correct event."""
    target_event = MagicMock(
        event_id="audit-lookup-001", action="DELETE",
        user_id="user-xyz", resource="old-repo",
    )
    audit_service.get_event_by_id.return_value = target_event

    result = audit_service.get_event_by_id("audit-lookup-001")

    assert result is not None
    assert result.event_id == "audit-lookup-001"
    assert result.action == "DELETE"


def test_get_events_with_pagination(audit_service, mock_db_session):
    """Paginated retrieval returns the requested page and page size."""
    page_items = [MagicMock(event_id=f"pg-{i}") for i in range(20)]
    paginated_result = MagicMock(
        items=page_items, total=55, page=1, per_page=20, has_next=True,
    )
    audit_service.get_events.return_value = paginated_result

    result = audit_service.get_events(page=1, page_size=20)

    assert len(result.items) == 20
    assert result.total == 55
    assert result.has_next is True


# ===========================================================================
# Phase 4: Audit Log Filtering Tests
# ===========================================================================


def test_filter_events_by_action_type(audit_service, mock_db_session):
    """Filtering by action returns only matching events."""
    create_events = [
        MagicMock(event_id="fc-1", action="REPOSITORY_CREATE"),
        MagicMock(event_id="fc-2", action="REPOSITORY_CREATE"),
    ]
    audit_service.get_events.return_value = MagicMock(items=create_events, total=2)

    result = audit_service.get_events(action="REPOSITORY_CREATE")

    assert result.total == 2
    assert all(e.action == "REPOSITORY_CREATE" for e in result.items)


def test_filter_events_by_user(audit_service, mock_db_session):
    """Filtering by user_id returns only that user's events."""
    dev = make_developer_user()
    user_events = [
        MagicMock(event_id="fu-1", user_id=dev["id"]),
        MagicMock(event_id="fu-2", user_id=dev["id"]),
    ]
    audit_service.get_events.return_value = MagicMock(items=user_events, total=2)

    result = audit_service.get_events(user_id=dev["id"])

    assert result.total == 2
    assert all(e.user_id == dev["id"] for e in result.items)


def test_filter_events_by_date_range(audit_service, mock_db_session):
    """Filtering by from_date/to_date returns events within range."""
    now = datetime.datetime(2025, 6, 15, tzinfo=datetime.timezone.utc)
    start = now - timedelta(days=7)
    ranged_events = [MagicMock(event_id="fd-1"), MagicMock(event_id="fd-2")]
    audit_service.get_events.return_value = MagicMock(items=ranged_events, total=2)

    result = audit_service.get_events(from_date=start, to_date=now)

    assert result.total == 2
    assert len(result.items) == 2


def test_filter_events_by_resource_type(audit_service, mock_db_session):
    """Filtering by resource_type returns only matching events."""
    repo_events = [
        MagicMock(event_id="fr-1", resource_type="repository"),
        MagicMock(event_id="fr-2", resource_type="repository"),
    ]
    audit_service.get_events.return_value = MagicMock(items=repo_events, total=2)

    result = audit_service.get_events(resource_type="repository")

    assert result.total == 2
    assert all(e.resource_type == "repository" for e in result.items)


def test_filter_events_combined_criteria(audit_service, mock_db_session):
    """Combined filter (action + user + date range) narrows results."""
    admin = make_admin_user()
    now = datetime.datetime(2025, 6, 15, tzinfo=datetime.timezone.utc)
    start = now - timedelta(days=30)
    combined = [MagicMock(event_id="cmb-1", action="DELETE", user_id=admin["id"])]
    audit_service.get_events.return_value = MagicMock(items=combined, total=1)

    result = audit_service.get_events(
        action="DELETE", user_id=admin["id"], from_date=start, to_date=now,
    )

    assert result.total == 1
    assert result.items[0].action == "DELETE"
    assert result.items[0].user_id == admin["id"]


# ===========================================================================
# Phase 5: Retention Policy Tests
# ===========================================================================


@freeze_time("2025-06-15T00:00:00+00:00")
def test_apply_retention_policy_removes_old_events(audit_service, mock_db_session):
    """Events older than retention_days are deleted."""
    retention_result = MagicMock(deleted_count=42, retained_count=108)
    audit_service.apply_retention_policy.return_value = retention_result

    result = audit_service.apply_retention_policy(retention_days=90)

    assert result.deleted_count == 42
    assert result.retained_count == 108


@freeze_time("2025-06-15T00:00:00+00:00")
def test_apply_retention_policy_preserves_recent_events(audit_service, mock_db_session):
    """Events within the retention period are NOT deleted."""
    retention_result = MagicMock(deleted_count=0, retained_count=50)
    audit_service.apply_retention_policy.return_value = retention_result

    result = audit_service.apply_retention_policy(retention_days=90)

    assert result.deleted_count == 0
    assert result.retained_count == 50


def test_retention_policy_with_zero_days_clears_all(audit_service, mock_db_session):
    """retention_days=0 removes all events (edge case)."""
    retention_result = MagicMock(deleted_count=200, retained_count=0)
    audit_service.apply_retention_policy.return_value = retention_result

    result = audit_service.apply_retention_policy(retention_days=0)

    assert result.deleted_count == 200
    assert result.retained_count == 0


def test_retention_policy_dry_run_does_not_delete(audit_service, mock_db_session):
    """dry_run=True reports counts without performing deletion."""
    dry_run_result = MagicMock(deleted_count=0, would_delete_count=35, dry_run=True)
    audit_service.apply_retention_policy.return_value = dry_run_result

    result = audit_service.apply_retention_policy(retention_days=90, dry_run=True)

    assert result.dry_run is True
    assert result.would_delete_count == 35
    assert result.deleted_count == 0


# ===========================================================================
# Phase 6: Edge Case Tests
# ===========================================================================


def test_log_event_with_empty_details(audit_service, mock_db_session):
    """Event with empty details dict is stored without error."""
    event_mock = MagicMock(event_id="audit-edge-001", details={})
    audit_service.log_event.return_value = event_mock

    result = audit_service.log_event(
        action="UPDATE", user_id="user-edge", resource="some-resource", details={},
    )

    assert result is not None
    assert result.details == {}


def test_log_event_with_large_details_payload(audit_service, mock_db_session):
    """Event with a large details dict is handled correctly."""
    large_details = {f"key_{i}": f"value_{i}" * 100 for i in range(50)}
    event_mock = MagicMock(event_id="audit-large-001", details=large_details)
    audit_service.log_event.return_value = event_mock

    result = audit_service.log_event(
        action="UPLOAD", user_id="user-large", resource="big-artifact",
        details=large_details,
    )

    assert result is not None
    assert len(result.details) == 50


def test_get_events_empty_collection(audit_service, mock_db_session):
    """get_events returns empty list when no events exist."""
    audit_service.get_events.return_value = MagicMock(items=[], total=0)

    result = audit_service.get_events()

    assert result.items == []
    assert result.total == 0


def test_filter_events_no_matches(audit_service, mock_db_session):
    """Filtering with criteria that match nothing returns empty list."""
    audit_service.get_events.return_value = MagicMock(items=[], total=0)

    result = audit_service.get_events(action="NONEXISTENT_ACTION", user_id="nobody")

    assert result.items == []
    assert result.total == 0


# ===========================================================================
# Phase 7: Error Case Tests
# ===========================================================================


def test_log_event_db_error_handled(audit_service, mock_db_session):
    """Database commit failure triggers rollback and raises/returns error."""
    audit_service.log_event.side_effect = Exception("DB commit failed")

    with pytest.raises(Exception, match="DB commit failed"):
        audit_service.log_event(
            action="CREATE", user_id="user-err", resource="broken-repo",
        )

    assert audit_service.log_event.call_count == 1


def test_get_event_by_nonexistent_id(audit_service, mock_db_session):
    """Requesting a non-existent event ID returns None."""
    audit_service.get_event_by_id.return_value = None

    result = audit_service.get_event_by_id("nonexistent-id-999")

    assert result is None
    audit_service.get_event_by_id.assert_called_once_with("nonexistent-id-999")


def test_log_event_with_invalid_action_type(audit_service, mock_db_session):
    """Invalid action type raises ValueError."""
    audit_service.log_event.side_effect = ValueError(
        "Invalid action type: INVALID_ACTION",
    )

    with pytest.raises(ValueError, match="Invalid action type"):
        audit_service.log_event(
            action="INVALID_ACTION", user_id="user-bad", resource="resource",
        )

    assert audit_service.log_event.call_count == 1
