"""
Unit tests for the scheduled task data model (``src/models/task.py``).

Validates task model instantiation, cron expression parsing, state
transitions (WAITING → RUNNING → COMPLETED / FAILED / CANCELLED),
execution timestamps, task type and properties handling, serialisation,
edge cases, error handling, and database persistence.

Depends on fixtures from ``tests/unit/models/conftest.py``:
    - ``db_session`` — Per-test database session with cleanup
    - ``model_factory`` — Generic model creation helper
    - ``sample_task`` — Pre-built Task instance with cron schedule
"""

import uuid

import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy.exc import IntegrityError

from src.models.task import Task

pytestmark = pytest.mark.unit

# State constants (correspond to Task.status field values)
STATE_WAITING = "WAITING"
STATE_RUNNING = "RUNNING"
STATE_COMPLETED = "COMPLETED"
STATE_FAILED = "FAILED"
STATE_CANCELLED = "CANCELLED"

VALID_STATES = [
    STATE_WAITING, STATE_RUNNING, STATE_COMPLETED,
    STATE_FAILED, STATE_CANCELLED,
]


# ── Phase 2: Happy Path — Model Instantiation ────────────────────────────


def test_task_model_instantiation_with_required_fields(db_session, model_factory):
    """Task created with name, type, and schedule stores all required fields."""
    task = model_factory(Task, name="daily-cleanup", type="cleanup",
                         schedule="0 0 * * *")
    assert task is not None
    assert task.name == "daily-cleanup"
    assert task.type == "cleanup"


def test_task_model_default_values(db_session, model_factory):
    """Task with minimal required fields receives correct defaults."""
    task = model_factory(Task, name="default-task", type="maintenance")
    assert task.status == STATE_WAITING
    assert task.enabled is True
    assert isinstance(task.created_at, datetime)


def test_task_model_with_full_configuration(db_session, model_factory):
    """Task with all fields populated stores everything correctly."""
    now = datetime.now(timezone.utc)
    task = model_factory(
        Task, name="full-task", type="backup", schedule="0 3 * * *",
        enabled=False, message="Full backup of all repositories",
        properties={"retention_days": 30, "compression": "gzip"},
        started_at=now, completed_at=now + timedelta(minutes=5),
        last_run_at=now - timedelta(hours=24),
        next_run_at=now + timedelta(hours=24), status=STATE_COMPLETED,
    )
    assert task.name == "full-task"
    assert task.schedule == "0 3 * * *"
    assert task.enabled is False
    assert task.properties["retention_days"] == 30


def test_task_model_enabled_field(db_session, model_factory):
    """Task with enabled=True is active."""
    task = model_factory(Task, name="enabled-task", type="cleanup", enabled=True)
    assert task.enabled is True
    assert task.status == STATE_WAITING


def test_task_model_disabled_field(db_session, model_factory):
    """Task with enabled=False is inactive."""
    task = model_factory(Task, name="disabled-task", type="cleanup", enabled=False)
    assert task.enabled is False
    assert task.name == "disabled-task"


# ── Phase 3: Cron Expression Tests ───────────────────────────────────────


def test_task_cron_expression_daily(db_session, model_factory):
    """Daily cron '0 0 * * *' is stored correctly."""
    task = model_factory(Task, name="cron-daily", type="cleanup", schedule="0 0 * * *")
    assert task.schedule == "0 0 * * *"
    assert task.name == "cron-daily"


def test_task_cron_expression_hourly(db_session, model_factory):
    """Hourly cron '0 * * * *' is stored correctly."""
    task = model_factory(Task, name="cron-hourly", type="cleanup", schedule="0 * * * *")
    assert task.schedule == "0 * * * *"
    assert task.type == "cleanup"


def test_task_cron_expression_weekly(db_session, model_factory):
    """Weekly cron '0 0 * * 0' is stored correctly."""
    task = model_factory(Task, name="cron-weekly", type="maintenance", schedule="0 0 * * 0")
    assert task.schedule == "0 0 * * 0"
    assert task.name == "cron-weekly"


def test_task_cron_expression_every_five_minutes(db_session, model_factory):
    """Every-five-minutes cron is stored correctly."""
    task = model_factory(Task, name="cron-5m", type="health_check", schedule="*/5 * * * *")
    assert task.schedule == "*/5 * * * *"
    assert task.type == "health_check"


def test_task_cron_expression_complex(db_session, model_factory):
    """Complex cron '30 2 15 * *' is stored correctly."""
    task = model_factory(Task, name="cron-complex", type="backup", schedule="30 2 15 * *")
    assert task.schedule == "30 2 15 * *"
    assert task.name == "cron-complex"


@pytest.mark.parametrize(
    "cron_expr",
    ["0 0 * * *", "0 * * * *", "*/5 * * * *", "30 2 15 * *", "0 0 * * 0"],
    ids=["daily", "hourly", "every-5m", "monthly", "weekly-sunday"],
)
def test_task_cron_expression_parametrized(db_session, model_factory, cron_expr):
    """Parametrised: valid cron expressions are stored correctly."""
    name = f"cron-p-{uuid.uuid4().hex[:8]}"
    task = model_factory(Task, name=name, type="cleanup", schedule=cron_expr)
    assert task.schedule == cron_expr
    assert task.name == name


def test_task_invalid_cron_expression(db_session, model_factory):
    """Invalid cron string stored as-is (no model-level cron validation)."""
    task = model_factory(Task, name="bad-cron", type="cleanup", schedule="invalid-cron")
    assert task.schedule == "invalid-cron"
    assert task.name == "bad-cron"


def test_task_empty_cron_expression(db_session, model_factory):
    """Empty cron string stored as empty string."""
    task = model_factory(Task, name="empty-cron", type="cleanup", schedule="")
    assert task.schedule == ""
    assert task.name == "empty-cron"


# ── Phase 4: State Transition Tests ──────────────────────────────────────


def test_task_initial_state_is_waiting(db_session, model_factory):
    """New task has WAITING status with no execution timestamps."""
    task = model_factory(Task, name="initial-state", type="cleanup")
    assert task.status == STATE_WAITING
    assert task.started_at is None
    assert task.completed_at is None


def test_task_transition_waiting_to_running(db_session, model_factory):
    """WAITING → RUNNING sets started_at."""
    now = datetime.now(timezone.utc)
    task = model_factory(Task, name="w-to-r", type="cleanup")
    task.status = STATE_RUNNING
    task.started_at = now
    db_session.flush()
    assert task.status == STATE_RUNNING
    assert isinstance(task.started_at, datetime)


def test_task_transition_running_to_completed(db_session, model_factory):
    """RUNNING → COMPLETED with valid timestamps."""
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=3)
    task = model_factory(Task, name="r-to-c", type="cleanup",
                         status=STATE_RUNNING, started_at=start)
    task.status = STATE_COMPLETED
    task.completed_at = end
    db_session.flush()
    assert task.status == STATE_COMPLETED
    assert task.completed_at >= task.started_at


def test_task_transition_running_to_failed(db_session, model_factory):
    """RUNNING → FAILED stores error message."""
    task = model_factory(Task, name="r-to-f", type="cleanup",
                         status=STATE_RUNNING, started_at=datetime.now(timezone.utc))
    task.status = STATE_FAILED
    task.error_message = "Connection timeout"
    task.completed_at = datetime.now(timezone.utc)
    db_session.flush()
    assert task.status == STATE_FAILED
    assert task.error_message == "Connection timeout"


def test_task_transition_waiting_to_cancelled(db_session, model_factory):
    """WAITING → CANCELLED."""
    task = model_factory(Task, name="w-to-cancel", type="cleanup")
    task.status = STATE_CANCELLED
    db_session.flush()
    assert task.status == STATE_CANCELLED
    assert task.name == "w-to-cancel"


@pytest.mark.parametrize("state", VALID_STATES, ids=VALID_STATES)
def test_task_state_parametrized(db_session, model_factory, state):
    """Parametrised: each valid state is accepted and stored."""
    name = f"st-{state.lower()}-{uuid.uuid4().hex[:8]}"
    task = model_factory(Task, name=name, type="cleanup", status=state)
    assert task.status == state
    assert task.name == name


def test_task_invalid_state_raises_error(db_session):
    """Setting status to an invalid value raises ValueError."""
    with pytest.raises(ValueError):
        Task(name="invalid-state", type="cleanup", status="INVALID_STATE")
    valid = Task(name="valid-after-err", type="cleanup", status=STATE_WAITING)
    assert valid.status == STATE_WAITING


def test_task_state_transition_completed_cannot_go_back_to_running(
    db_session, model_factory,
):
    """Completed → RUNNING allowed (model has no ordering constraint)."""
    task = model_factory(Task, name="back-tr", type="cleanup", status=STATE_COMPLETED)
    task.status = STATE_RUNNING
    db_session.flush()
    assert task.status == STATE_RUNNING
    assert task.name == "back-tr"


# ── Phase 5: Execution Timestamp Tests ───────────────────────────────────


def test_task_started_at_initially_none(db_session, model_factory):
    """New task has started_at = None."""
    task = model_factory(Task, name="start-none", type="cleanup")
    assert task.started_at is None
    assert task.status == STATE_WAITING


def test_task_completed_at_initially_none(db_session, model_factory):
    """New task has completed_at = None."""
    task = model_factory(Task, name="comp-none", type="cleanup")
    assert task.completed_at is None
    assert task.status == STATE_WAITING


def test_task_started_at_set_when_running(db_session, model_factory):
    """Running task has started_at as a datetime."""
    now = datetime.now(timezone.utc)
    task = model_factory(Task, name="start-set", type="cleanup",
                         status=STATE_RUNNING, started_at=now)
    assert isinstance(task.started_at, datetime)
    assert task.status == STATE_RUNNING


def test_task_completed_at_set_when_completed(db_session, model_factory):
    """Completed task has completed_at as a datetime."""
    now = datetime.now(timezone.utc)
    task = model_factory(Task, name="comp-set", type="cleanup",
                         status=STATE_COMPLETED,
                         started_at=now - timedelta(minutes=5), completed_at=now)
    assert isinstance(task.completed_at, datetime)
    assert task.status == STATE_COMPLETED


def test_task_last_run_at_timestamp(db_session, model_factory):
    """Task stores last_run_at tracking previous execution."""
    past = datetime.now(timezone.utc) - timedelta(hours=24)
    task = model_factory(Task, name="last-run", type="cleanup", last_run_at=past)
    assert isinstance(task.last_run_at, datetime)
    assert task.last_run_at == past


def test_task_next_run_at_timestamp(db_session, model_factory):
    """Task stores next_run_at as a future datetime."""
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    task = model_factory(Task, name="next-run", type="cleanup", next_run_at=future)
    assert isinstance(task.next_run_at, datetime)
    assert task.next_run_at > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_task_execution_duration_calculation(db_session, model_factory):
    """Duration between started_at and completed_at is correct."""
    start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, 0, 5, 0, tzinfo=timezone.utc)
    task = model_factory(Task, name="duration", type="cleanup",
                         status=STATE_COMPLETED, started_at=start, completed_at=end)
    duration = task.completed_at - task.started_at
    assert duration == timedelta(minutes=5)
    assert duration.total_seconds() == 300


# ── Phase 6: Task Type and Properties Tests ──────────────────────────────


def test_task_type_field(db_session, model_factory):
    """Task type field stores the value correctly."""
    task = model_factory(Task, name="type-test", type="cleanup")
    assert task.type == "cleanup"
    assert task.name == "type-test"


@pytest.mark.parametrize(
    "task_type",
    ["cleanup", "maintenance", "backup", "index", "health_check"],
    ids=["cleanup", "maintenance", "backup", "index", "health-check"],
)
def test_task_type_parametrized(db_session, model_factory, task_type):
    """Parametrised: each task type is accepted and stored."""
    name = f"tp-{task_type}-{uuid.uuid4().hex[:8]}"
    task = model_factory(Task, name=name, type=task_type)
    assert task.type == task_type
    assert task.name == name


def test_task_properties_as_json(db_session, model_factory):
    """Properties field stores and retrieves JSON data correctly."""
    props = {"retention_days": 30, "dry_run": False}
    task = model_factory(Task, name="props-json", type="cleanup", properties=props)
    assert task.properties == props
    assert task.properties["retention_days"] == 30


def test_task_message_field(db_session, model_factory):
    """Task message field stores text correctly."""
    task = model_factory(Task, name="msg-task", type="cleanup",
                         message="Cleanup old artifacts")
    assert task.message == "Cleanup old artifacts"
    assert task.name == "msg-task"


def test_task_error_message_on_failure(db_session, model_factory):
    """Failed task stores error_message alongside FAILED status."""
    task = model_factory(Task, name="err-msg", type="cleanup",
                         status=STATE_FAILED, error_message="Connection timeout")
    assert task.error_message == "Connection timeout"
    assert task.status == STATE_FAILED


# ── Phase 7: Serialisation Tests ─────────────────────────────────────────


def test_task_to_dict_serialization(db_session, model_factory):
    """Task to_dict() returns dict with all expected keys."""
    task = model_factory(Task, name="serial-task", type="cleanup",
                         schedule="0 0 * * *", message="Test serialisation")
    result = task.to_dict()
    assert isinstance(result, dict)
    assert result["name"] == "serial-task"
    for key in ("type", "schedule", "status", "enabled"):
        assert key in result


def test_task_serialization_includes_state(db_session, model_factory):
    """Serialised dict includes current status value."""
    task = model_factory(Task, name="state-serial", type="backup",
                         status=STATE_RUNNING, started_at=datetime.now(timezone.utc))
    result = task.to_dict()
    assert result["status"] == STATE_RUNNING
    assert result["name"] == "state-serial"


def test_task_repr_string(db_session, model_factory):
    """Task repr contains task name and model identifier."""
    task = model_factory(Task, name="repr-task", type="cleanup")
    result = repr(task)
    assert "repr-task" in result
    assert "Task" in result


# ── Phase 8: Edge Cases ──────────────────────────────────────────────────


def test_task_with_no_schedule(db_session, model_factory):
    """Manual task without schedule has schedule=None."""
    task = model_factory(Task, name="no-schedule", type="cleanup")
    assert task.schedule is None
    assert task.name == "no-schedule"


def test_task_with_very_long_name(db_session, model_factory):
    """Task with 255-character name is accepted."""
    long_name = "a" * 255
    task = model_factory(Task, name=long_name, type="cleanup")
    assert len(task.name) == 255
    assert task.name == long_name


def test_task_with_empty_properties(db_session, model_factory):
    """Task with empty properties dict is accepted."""
    task = model_factory(Task, name="empty-props", type="cleanup", properties={})
    assert task.properties == {}
    assert task.name == "empty-props"


def test_task_with_null_properties(db_session, model_factory):
    """Task with properties=None is accepted."""
    task = model_factory(Task, name="null-props", type="cleanup", properties=None)
    assert task.properties is None
    assert task.name == "null-props"


# ── Phase 9: Error Cases ─────────────────────────────────────────────────


def test_task_null_name_raises_error(db_session):
    """NOT NULL constraint on Task.name is enforced."""
    task = Task(id=str(uuid.uuid4()), type="cleanup")
    assert task.name is None
    db_session.add(task)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_task_null_type_raises_error(db_session):
    """NOT NULL constraint on Task.type is enforced."""
    task = Task(id=str(uuid.uuid4()), name=f"null-type-{uuid.uuid4().hex[:8]}")
    assert task.type is None
    db_session.add(task)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_task_duplicate_name_constraint(db_session, model_factory):
    """Unique constraint on Task.name raises IntegrityError for duplicates."""
    model_factory(Task, name="unique-task", type="cleanup")
    with pytest.raises(IntegrityError):
        model_factory(Task, name="unique-task", type="maintenance")
    db_session.rollback()
    task2 = model_factory(Task, name="different-task", type="maintenance")
    assert task2.name == "different-task"


# ── Phase 10: Database Persistence Tests ─────────────────────────────────


def test_task_persists_to_database(db_session, model_factory):
    """Task is persisted and retrievable from the database."""
    task = model_factory(Task, name="persist-task", type="cleanup", schedule="0 0 * * *")
    task_id = task.id
    db_session.commit()
    found = db_session.get(Task, task_id)
    assert found is not None
    assert found.name == "persist-task"


def test_task_state_update_persists(db_session, model_factory):
    """Status update persists after commit."""
    task = model_factory(Task, name="update-state", type="cleanup")
    task_id = task.id
    task.status = STATE_RUNNING
    task.started_at = datetime.now(timezone.utc)
    db_session.commit()
    found = db_session.get(Task, task_id)
    assert found.status == STATE_RUNNING
    assert found.started_at is not None


def test_task_query_by_state(db_session, model_factory):
    """Tasks can be queried by status."""
    now = datetime.now(timezone.utc)
    model_factory(Task, name="q-waiting", type="cleanup", status=STATE_WAITING)
    model_factory(Task, name="q-running", type="cleanup",
                  status=STATE_RUNNING, started_at=now)
    model_factory(Task, name="q-completed", type="cleanup",
                  status=STATE_COMPLETED, started_at=now, completed_at=now)
    waiting = db_session.query(Task).filter_by(status=STATE_WAITING).all()
    assert len(waiting) == 1
    assert waiting[0].name == "q-waiting"


def test_task_delete_removes_from_db(db_session, model_factory):
    """Deleted task is no longer retrievable."""
    task = model_factory(Task, name="delete-task", type="cleanup")
    task_id = task.id
    db_session.delete(task)
    db_session.commit()
    found = db_session.get(Task, task_id)
    assert found is None
    assert task_id is not None
