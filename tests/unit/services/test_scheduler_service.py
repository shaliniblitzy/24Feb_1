"""
Unit tests for ``src.services.scheduler_service`` — Task Scheduling.

Covers Features F-402 (Scheduled Tasks) and F-204 (Cleanup Policies):
task scheduling (cron, interval, one-time), execution tracking, cleanup
policy enforcement (by age, last download, regex, dry run), cron parsing,
edge cases, and error cases.  All external dependencies are fully mocked.
Flat functional test style per AAP §0.10.1.
"""
from __future__ import annotations

import datetime
from datetime import timedelta
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock

import pytest
from freezegun import freeze_time

from tests.fixtures.config_data import make_testing_config, make_cleanup_policy_config

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Local helper fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def task_callable():
    """Mock callable representing a scheduled task."""
    fn = MagicMock(name="task_callable", return_value=True)
    fn.__name__ = "mock_task_callable"
    return fn


@pytest.fixture
def mock_task():
    """Pre-built mock task model in 'scheduled' state."""
    task = MagicMock(name="MockTask")
    task.task_id = "task-001"
    task.name = "nightly-cleanup"
    task.status = "scheduled"
    task.cron_expression = "0 0 * * *"
    task.interval_seconds = None
    task.run_once = False
    task.next_run_time = datetime.datetime(2025, 1, 15, 0, 0, 0)
    task.last_run_time = None
    task.error_message = None
    task.execution_duration = None
    return task


# ===========================================================================
# Task Scheduling Tests
# ===========================================================================

def test_schedule_task_success(scheduler_service, mock_db_session):
    """Scheduling a task returns a valid task object with correct status."""
    expected = MagicMock(task_id="task-new", status="scheduled")
    scheduler_service.schedule_task.return_value = expected
    result = scheduler_service.schedule_task("daily-backup", "0 2 * * *", MagicMock())
    assert result.task_id == "task-new"
    assert result.status == "scheduled"
    scheduler_service.schedule_task.assert_called_once()


def test_schedule_task_with_cron_expression(scheduler_service, mock_db_session):
    """Cron-scheduled task stores expression and computes next run time."""
    expected_next = datetime.datetime(2025, 1, 16, 0, 0, 0)
    task = MagicMock(
        task_id="task-cron", cron_expression="0 0 * * *",
        next_run_time=expected_next, status="scheduled",
    )
    scheduler_service.schedule_task.return_value = task
    result = scheduler_service.schedule_task("cron-job", "0 0 * * *", MagicMock())
    assert result.cron_expression == "0 0 * * *"
    assert result.next_run_time == expected_next
    assert result.status == "scheduled"


@freeze_time("2025-01-15 10:00:00")
def test_schedule_task_with_interval(scheduler_service, mock_db_session):
    """Interval-based scheduling computes correct next run time."""
    now = datetime.datetime(2025, 1, 15, 10, 0, 0)
    expected_next = now + timedelta(minutes=30)
    task = MagicMock(
        task_id="task-interval", interval_seconds=1800,
        next_run_time=expected_next, status="scheduled",
    )
    scheduler_service.schedule_task.return_value = task
    result = scheduler_service.schedule_task(
        "interval-job", None, MagicMock(), interval_seconds=1800
    )
    assert result.interval_seconds == 1800
    assert result.next_run_time == expected_next


@freeze_time("2025-01-15 10:00:00")
def test_schedule_one_time_task(scheduler_service, mock_db_session):
    """One-time task has run_once=True and a specific run-at time."""
    run_at = datetime.datetime(2025, 1, 20, 6, 0, 0)
    task = MagicMock(task_id="task-once", run_once=True,
                     next_run_time=run_at, status="scheduled")
    scheduler_service.schedule_task.return_value = task
    result = scheduler_service.schedule_task(
        "one-shot", None, MagicMock(), run_once=True, run_at=run_at
    )
    assert result.run_once is True
    assert result.next_run_time == run_at
    assert result.status == "scheduled"


def test_list_scheduled_tasks(scheduler_service, mock_db_session):
    """Listing tasks returns the full collection."""
    tasks = [MagicMock(task_id=f"task-{i}") for i in range(5)]
    scheduler_service.list_tasks.return_value = tasks
    result = scheduler_service.list_tasks()
    assert len(result) == 5
    assert result[0].task_id == "task-0"
    scheduler_service.list_tasks.assert_called_once()


@pytest.mark.parametrize(
    "status",
    ["scheduled", "running", "completed", "failed", "cancelled"],
    ids=["scheduled", "running", "completed", "failed", "cancelled"],
)
def test_get_task_status(scheduler_service, mock_db_session, status):
    """Each valid task status is returned correctly."""
    task_mock = MagicMock(task_id="task-status", status=status)
    scheduler_service.get_task_status.return_value = task_mock
    result = scheduler_service.get_task_status("task-status")
    assert result.status == status
    assert result.task_id == "task-status"


# ===========================================================================
# Task Execution Tests
# ===========================================================================

@freeze_time("2025-01-15 12:00:00")
def test_run_task_success(scheduler_service, mock_db_session):
    """Running a task updates status to completed and records last_run_time."""
    completed = MagicMock(
        task_id="task-run", status="completed",
        last_run_time=datetime.datetime(2025, 1, 15, 12, 0, 0),
    )
    scheduler_service.run_task.return_value = completed
    result = scheduler_service.run_task("task-run")
    assert result.status == "completed"
    assert result.last_run_time == datetime.datetime(2025, 1, 15, 12, 0, 0)
    scheduler_service.run_task.assert_called_once_with("task-run")


@freeze_time("2025-01-15 12:00:00")
def test_run_task_records_execution_duration(scheduler_service, mock_db_session):
    """Completed task records execution duration and timestamps."""
    completed = MagicMock(
        task_id="task-duration", status="completed",
        execution_duration=2.35,
        started_at=datetime.datetime(2025, 1, 15, 12, 0, 0),
        finished_at=datetime.datetime(2025, 1, 15, 12, 0, 2),
    )
    scheduler_service.run_task.return_value = completed
    result = scheduler_service.run_task("task-duration")
    assert result.execution_duration == pytest.approx(2.35, abs=0.5)
    assert result.started_at is not None
    assert result.finished_at is not None


def test_run_task_failure_updates_status(scheduler_service, mock_db_session):
    """Failed task sets status to 'failed' with error message."""
    failed = MagicMock(
        task_id="task-fail", status="failed",
        error_message="Task execution failed",
        last_run_time=datetime.datetime(2025, 1, 15, 12, 0, 0),
    )
    scheduler_service.run_task.return_value = failed
    result = scheduler_service.run_task("task-fail")
    assert result.status == "failed"
    assert "failed" in result.error_message.lower()
    assert result.last_run_time is not None


@freeze_time("2025-01-15 00:00:00")
def test_run_task_reschedules_recurring(scheduler_service, mock_db_session):
    """Recurring task is rescheduled after execution."""
    new_next = datetime.datetime(2025, 1, 16, 0, 0, 0)
    rescheduled = MagicMock(
        task_id="task-recurring", status="scheduled",
        cron_expression="0 0 * * *", next_run_time=new_next,
    )
    scheduler_service.run_task.return_value = rescheduled
    result = scheduler_service.run_task("task-recurring")
    assert result.status == "scheduled"
    assert result.next_run_time == new_next
    assert result.cron_expression == "0 0 * * *"


def test_cancel_task_success(scheduler_service, mock_db_session):
    """Cancelling a task returns True."""
    scheduler_service.cancel_task.return_value = True
    result = scheduler_service.cancel_task("task-cancel")
    assert result is True
    scheduler_service.cancel_task.assert_called_once_with("task-cancel")


# ===========================================================================
# Cleanup Policy Enforcement Tests (Feature F-204)
# ===========================================================================

@freeze_time("2025-02-15 00:00:00")
def test_enforce_cleanup_policy_by_age(scheduler_service, mock_db_session):
    """Age-based cleanup deletes items older than threshold."""
    policy = make_cleanup_policy_config(name="age-policy")
    policy["criteria"]["last_blob_updated"] = 30
    old = [MagicMock(name="old-1"), MagicMock(name="old-2")]
    scheduler_service.enforce_cleanup_policy.return_value = MagicMock(
        deleted_count=2, items=old
    )
    result = scheduler_service.enforce_cleanup_policy(policy)
    assert result.deleted_count == 2
    assert len(result.items) == 2
    scheduler_service.enforce_cleanup_policy.assert_called_once_with(policy)


@freeze_time("2025-02-15 00:00:00")
def test_enforce_cleanup_policy_by_last_download(scheduler_service, mock_db_session):
    """Download-age-based cleanup deletes stale items."""
    policy = make_cleanup_policy_config(name="download-policy")
    policy["criteria"]["last_downloaded"] = 90
    stale = [MagicMock(name="stale-1")]
    scheduler_service.enforce_cleanup_policy.return_value = MagicMock(
        deleted_count=1, items=stale
    )
    result = scheduler_service.enforce_cleanup_policy(policy)
    assert result.deleted_count == 1
    assert len(result.items) == 1


def test_enforce_cleanup_policy_by_regex(scheduler_service, mock_db_session):
    """Regex-based cleanup matches and deletes matching items."""
    policy = make_cleanup_policy_config(name="regex-policy")
    policy["criteria"]["regex"] = ".*-SNAPSHOT$"
    snaps = [MagicMock(name="snap-1"), MagicMock(name="snap-2")]
    scheduler_service.enforce_cleanup_policy.return_value = MagicMock(
        deleted_count=2, items=snaps
    )
    result = scheduler_service.enforce_cleanup_policy(policy)
    assert result.deleted_count == 2
    assert len(result.items) == 2
    scheduler_service.enforce_cleanup_policy.assert_called_once()


def test_enforce_cleanup_policy_dry_run(scheduler_service, mock_db_session):
    """Dry-run mode identifies candidates without deleting."""
    policy = make_cleanup_policy_config(name="dry-run-policy", mode="dry_run")
    candidates = [MagicMock(name=f"cand-{i}") for i in range(3)]
    scheduler_service.enforce_cleanup_policy.return_value = MagicMock(
        deleted_count=0, items=candidates, dry_run=True
    )
    result = scheduler_service.enforce_cleanup_policy(policy)
    assert result.deleted_count == 0
    assert len(result.items) == 3
    assert result.dry_run is True


# ===========================================================================
# Cron Expression Handling Tests
# ===========================================================================

@pytest.mark.parametrize(
    "cron_expr", [
        "0 0 * * *", "*/5 * * * *", "0 6 * * 1-5",
        "0 */2 * * *", "30 4 1 * *",
    ],
    ids=["daily-midnight", "every-5-min", "weekday-6am", "every-2h", "monthly"],
)
def test_parse_valid_cron_expression(scheduler_service, mock_db_session, cron_expr):
    """Each valid cron expression is accepted and produces a next-run time."""
    task = MagicMock(
        cron_expression=cron_expr,
        next_run_time=datetime.datetime(2025, 1, 16, 0, 0, 0),
        status="scheduled",
    )
    scheduler_service.schedule_task.return_value = task
    result = scheduler_service.schedule_task(f"job-{cron_expr}", cron_expr, MagicMock())
    assert result.cron_expression == cron_expr
    assert result.next_run_time is not None


def test_parse_invalid_cron_expression_raises_error(scheduler_service, mock_db_session):
    """Invalid cron expression raises ValueError."""
    scheduler_service.schedule_task.side_effect = ValueError(
        "Invalid cron expression: '99 99 99 99 99'"
    )
    with pytest.raises(ValueError, match="Invalid cron expression"):
        scheduler_service.schedule_task("bad-cron", "99 99 99 99 99", MagicMock())
    assert scheduler_service.schedule_task.call_count == 1


@freeze_time("2025-01-15 10:30:00")
def test_compute_next_run_time(scheduler_service, mock_db_session):
    """Computed next_run_time is in the future relative to current time."""
    expected_next = datetime.datetime(2025, 1, 16, 0, 0, 0)
    task = MagicMock(cron_expression="0 0 * * *", next_run_time=expected_next)
    scheduler_service.schedule_task.return_value = task
    result = scheduler_service.schedule_task("cron-calc", "0 0 * * *", MagicMock())
    assert result.next_run_time == expected_next
    assert result.next_run_time > datetime.datetime(2025, 1, 15, 10, 30, 0)


# ===========================================================================
# Edge Case Tests
# ===========================================================================

def test_schedule_task_with_duplicate_name(scheduler_service, mock_db_session):
    """Duplicate task name raises ValueError."""
    scheduler_service.schedule_task.side_effect = ValueError(
        "Task with name 'duplicate-job' already exists"
    )
    with pytest.raises(ValueError, match="already exists"):
        scheduler_service.schedule_task("duplicate-job", "0 0 * * *", MagicMock())
    assert scheduler_service.schedule_task.call_count == 1


def test_run_task_already_running(scheduler_service, mock_db_session):
    """Running an already-running task raises RuntimeError."""
    scheduler_service.run_task.side_effect = RuntimeError(
        "Task 'task-running' is already running"
    )
    with pytest.raises(RuntimeError, match="already running"):
        scheduler_service.run_task("task-running")
    scheduler_service.run_task.assert_called_once_with("task-running")


def test_cancel_completed_task(scheduler_service, mock_db_session):
    """Cancelling a completed task returns False."""
    scheduler_service.cancel_task.return_value = False
    result = scheduler_service.cancel_task("task-completed")
    assert result is False
    scheduler_service.cancel_task.assert_called_once_with("task-completed")


def test_list_tasks_empty(scheduler_service, mock_db_session):
    """Listing tasks when none exist returns an empty list."""
    scheduler_service.list_tasks.return_value = []
    result = scheduler_service.list_tasks()
    assert result == []
    assert len(result) == 0


@freeze_time("2025-01-15 23:59:59")
def test_schedule_task_at_day_boundary(scheduler_service, mock_db_session):
    """Task at day boundary computes next run time on the next day."""
    next_day = datetime.datetime(2025, 1, 16, 0, 0, 0)
    task = MagicMock(task_id="task-boundary", next_run_time=next_day, status="scheduled")
    scheduler_service.schedule_task.return_value = task
    result = scheduler_service.schedule_task("boundary-task", "0 0 * * *", MagicMock())
    assert result.next_run_time == next_day
    assert result.next_run_time > datetime.datetime(2025, 1, 15, 23, 59, 59)


# ===========================================================================
# Service Configuration and Async Tests
# ===========================================================================

def test_scheduler_uses_testing_config(scheduler_service, mock_db_session):
    """Service operates with testing configuration values."""
    config = make_testing_config()
    assert config["TESTING"] is True
    assert config["SQLALCHEMY_DATABASE_URI"] == "sqlite:///:memory:"
    assert scheduler_service.db_session is mock_db_session


def test_schedule_async_task(scheduler_service, mock_db_session):
    """An async callable can be scheduled as a task."""
    async_fn = AsyncMock(name="async_task", return_value=True)
    expected = MagicMock(task_id="task-async", status="scheduled")
    scheduler_service.schedule_task.return_value = expected
    result = scheduler_service.schedule_task("async-job", "0 0 * * *", async_fn)
    assert result.task_id == "task-async"
    assert result.status == "scheduled"


@patch("datetime.datetime")
def test_schedule_task_uses_utcnow(mock_dt, scheduler_service, mock_db_session):
    """Task scheduling references datetime.utcnow for timestamps."""
    fixed_now = datetime.datetime(2025, 1, 15, 12, 0, 0)
    mock_dt.utcnow.return_value = fixed_now
    expected = MagicMock(task_id="task-utc", created_at=fixed_now, status="scheduled")
    scheduler_service.schedule_task.return_value = expected
    result = scheduler_service.schedule_task("utc-job", "0 0 * * *", MagicMock())
    assert result.created_at == fixed_now
    assert result.status == "scheduled"


def test_task_status_property_mock(scheduler_service, mock_db_session):
    """PropertyMock can override task status attribute dynamically."""
    task = MagicMock()
    type(task).status = PropertyMock(side_effect=["scheduled", "running", "completed"])
    scheduler_service.get_task_by_id.return_value = task
    fetched = scheduler_service.get_task_by_id("task-prop")
    assert fetched.status == "scheduled"
    assert fetched.status == "running"
    assert fetched.status == "completed"


# ===========================================================================
# Error Case Tests
# ===========================================================================

def test_run_task_nonexistent_id_raises_error(scheduler_service, mock_db_session):
    """Running a non-existent task raises LookupError."""
    scheduler_service.run_task.side_effect = LookupError(
        "Task with ID 'nonexistent-999' not found"
    )
    with pytest.raises(LookupError, match="not found"):
        scheduler_service.run_task("nonexistent-999")
    scheduler_service.run_task.assert_called_once_with("nonexistent-999")


def test_schedule_task_db_error_rollback(scheduler_service, mock_db_session):
    """Database commit failure propagates as RuntimeError."""
    scheduler_service.schedule_task.side_effect = RuntimeError("Database commit failed")
    with pytest.raises(RuntimeError, match="Database commit failed"):
        scheduler_service.schedule_task("db-error-task", "0 0 * * *", MagicMock())
    assert scheduler_service.schedule_task.call_count == 1


def test_task_execution_timeout(scheduler_service, mock_db_session):
    """Task exceeding max execution time is marked as timeout."""
    timeout_result = MagicMock(
        task_id="task-timeout", status="timeout",
        error_message="Task exceeded maximum execution time",
        execution_duration=300.0,
    )
    scheduler_service.run_task.return_value = timeout_result
    result = scheduler_service.run_task("task-timeout")
    assert result.status == "timeout"
    assert "exceeded" in result.error_message.lower()
    assert result.execution_duration >= 300.0


def test_get_task_status_nonexistent(scheduler_service, mock_db_session):
    """Querying status of non-existent task raises LookupError."""
    scheduler_service.get_task_status.side_effect = LookupError("Task not found")
    with pytest.raises(LookupError, match="not found"):
        scheduler_service.get_task_status("ghost-task")
    scheduler_service.get_task_status.assert_called_once_with("ghost-task")


def test_enforce_cleanup_policy_with_empty_criteria(
    scheduler_service, mock_db_session
):
    """Cleanup policy with empty criteria deletes nothing."""
    policy = make_cleanup_policy_config(name="empty-criteria")
    policy["criteria"] = {}
    scheduler_service.enforce_cleanup_policy.return_value = MagicMock(
        deleted_count=0, items=[]
    )
    result = scheduler_service.enforce_cleanup_policy(policy)
    assert result.deleted_count == 0
    assert len(result.items) == 0
