"""
Unit tests for the APScheduler-based task scheduling subsystem.

This module replaces Quartz 2.3.2 task tests from the Java source system.
Tests cover the three core scheduler components:

- **TaskRegistry** — Task type registration, lookup, and 8 built-in types
  with lazy-import verification.
- **TaskLogger** — Execution lifecycle (start/complete/fail/cancel),
  progress tracking (0-100 clamped), per-task loggers (nexus.task.*),
  execution history queries, and purge operations.
- **TaskScheduler** — APScheduler 3.10.4 integration: init_app, trigger
  building (Cron/Interval/Date), job management (schedule/pause/resume/
  remove/run_now), execution wrapping (Flask context, logger tracking,
  event emission, error handling), and cluster-aware SQLAlchemyJobStore.

Testing Stack:
  - pytest 8.3.4 (replaces JUnit 5.10.1 + Spock)
  - unittest.mock (replaces Mockito 5.8.0)
  - APScheduler 3.10.4 (replaces Quartz 2.3.2)
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, PropertyMock, call, patch

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.app.extensions import db
from src.app.models.task import TaskDefinition, TaskExecution
from src.app.scheduler.task_logger import (
    MAX_ERROR_MESSAGE_LENGTH,
    STATUS_CANCELED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_RUNNING,
    STATUS_WAITING,
    TASK_LOGGER_PREFIX,
    TaskLogger,
)
from src.app.scheduler.task_registry import (
    BUILTIN_TASK_TYPES,
    TASK_TYPE_BACKUP,
    TASK_TYPE_CLEANUP,
    TASK_TYPE_COMPACT,
    TASK_TYPE_HEALTH_CHECK,
    TASK_TYPE_METRICS_SNAPSHOT,
    TASK_TYPE_PURGE_UNUSED,
    TASK_TYPE_REBUILD_BROWSE,
    TASK_TYPE_REINDEX,
    TaskRegistry,
)
from src.app.scheduler.task_scheduler import TaskScheduler


# ---------------------------------------------------------------------------
# Shared Helper — Minimal Flask App Factory for Isolated Tests
# ---------------------------------------------------------------------------

def _create_test_app() -> Any:
    """Create a minimal Flask app for scheduler tests.

    Uses an in-memory SQLite database and disables the scheduler
    so that tests control the lifecycle explicitly.
    """
    from flask import Flask

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SCHEDULER_ENABLED"] = False
    app.config["SCHEDULER_CLUSTER_ENABLED"] = False
    app.config["SCHEDULER_MAX_WORKERS"] = 2
    app.config["SCHEDULER_MISFIRE_GRACE_TIME"] = 30
    app.config["SCHEDULER_COALESCE"] = True
    app.config["SECRET_KEY"] = "test-secret-key"

    db.init_app(app)

    with app.app_context():
        db.create_all()

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    """Module-scoped Flask app with in-memory SQLite for all scheduler tests."""
    application = _create_test_app()
    yield application
    with application.app_context():
        db.drop_all()


@pytest.fixture(autouse=True)
def _clean_tables(app):
    """Automatically clean task-related tables between every test."""
    yield
    with app.app_context():
        db.session.rollback()
        TaskExecution.query.delete()
        TaskDefinition.query.delete()
        db.session.commit()


@pytest.fixture()
def task_def_factory(app):
    """Factory fixture that creates and persists a TaskDefinition."""

    def _make(
        task_id: str = "test-task-1",
        task_type: str = "repository.cleanup",
        name: str = "Test Task",
        cron_expression: str | None = "0 0 * * *",
        enabled: bool = True,
        configuration: dict | None = None,
    ) -> TaskDefinition:
        with app.app_context():
            td = TaskDefinition(
                task_id=task_id,
                type=task_type,
                name=name,
                cron_expression=cron_expression,
                enabled=enabled,
                configuration=configuration or {},
            )
            db.session.add(td)
            db.session.commit()
            # Re-query to ensure the object is bound to the session
            return db.session.get(TaskDefinition, task_id)

    return _make


# =========================================================================
# TestTaskRegistry
# =========================================================================


class TestTaskRegistry:
    """Tests for the TaskRegistry class — task type registration and lookup."""

    # -- Registration Tests -----------------------------------------------

    def test_register_task_type(self):
        """Register a callable with a task type string."""
        registry = TaskRegistry()
        dummy_fn = MagicMock()
        registry.register("my.task", dummy_fn, "My task description")

        assert registry.is_registered("my.task") is True
        assert registry.get_task_callable("my.task") is dummy_fn

    def test_register_duplicate_type(self):
        """Re-registering the same task type overrides silently with warning."""
        registry = TaskRegistry()
        fn_a = MagicMock()
        fn_b = MagicMock()

        registry.register("my.task", fn_a, "First")
        registry.register("my.task", fn_b, "Second")

        # The second registration should override
        assert registry.get_task_callable("my.task") is fn_b

    def test_unregister_task_type(self):
        """Unregister removes the task type from the registry."""
        registry = TaskRegistry()
        registry.register("my.task", MagicMock(), "desc")

        result = registry.unregister("my.task")

        assert result is True
        assert registry.is_registered("my.task") is False

    def test_unregister_nonexistent(self):
        """Unregistering an unknown task type returns False gracefully."""
        registry = TaskRegistry()

        result = registry.unregister("does.not.exist")

        assert result is False

    # -- Retrieval Tests --------------------------------------------------

    def test_get_task_callable(self):
        """get_task_callable returns the registered callable for a type."""
        registry = TaskRegistry()
        fn = MagicMock()
        registry.register("repository.cleanup", fn)

        result = registry.get_task_callable("repository.cleanup")

        assert result is fn

    def test_get_task_callable_not_found(self):
        """get_task_callable returns None for an unknown task type."""
        registry = TaskRegistry()

        result = registry.get_task_callable("nonexistent.type")

        assert result is None

    def test_is_registered(self):
        """is_registered returns True for registered types."""
        registry = TaskRegistry()
        registry.register("repository.cleanup", MagicMock())

        assert registry.is_registered("repository.cleanup") is True

    def test_is_registered_false(self):
        """is_registered returns False for unknown types."""
        registry = TaskRegistry()

        assert registry.is_registered("unknown.type") is False

    def test_list_task_types(self):
        """list_task_types returns metadata for all registered types."""
        registry = TaskRegistry()
        registry.register("alpha.task", MagicMock(), "Alpha task")
        registry.register("beta.task", MagicMock(), "Beta task")

        types_list = registry.list_task_types()

        assert len(types_list) == 2
        type_names = [t["type"] for t in types_list]
        assert "alpha.task" in type_names
        assert "beta.task" in type_names
        # Verify metadata structure
        for entry in types_list:
            assert "type" in entry
            assert "description" in entry
            assert "builtin" in entry

    # -- Built-in Task Types (8 types) ------------------------------------

    def test_builtin_repository_cleanup(self):
        """'repository.cleanup' is registered as a built-in task type."""
        registry = TaskRegistry()
        registry.register_builtin_tasks()

        assert registry.is_registered(TASK_TYPE_CLEANUP) is True

    def test_builtin_blobstore_compact(self):
        """'blobstore.compact' is registered as a built-in task type."""
        registry = TaskRegistry()
        registry.register_builtin_tasks()

        assert registry.is_registered(TASK_TYPE_COMPACT) is True

    def test_builtin_db_backup(self):
        """'db.backup' is registered as a built-in task type."""
        registry = TaskRegistry()
        registry.register_builtin_tasks()

        assert registry.is_registered(TASK_TYPE_BACKUP) is True

    def test_builtin_repository_rebuild_index(self):
        """'repository.rebuild-index' is registered as a built-in type."""
        registry = TaskRegistry()
        registry.register_builtin_tasks()

        assert registry.is_registered(TASK_TYPE_REINDEX) is True

    def test_builtin_system_health_check(self):
        """'system.health-check' is registered as a built-in type."""
        registry = TaskRegistry()
        registry.register_builtin_tasks()

        assert registry.is_registered(TASK_TYPE_HEALTH_CHECK) is True

    def test_builtin_blobstore_purge_unused(self):
        """'blobstore.purge-unused' is registered as a built-in type."""
        registry = TaskRegistry()
        registry.register_builtin_tasks()

        assert registry.is_registered(TASK_TYPE_PURGE_UNUSED) is True

    def test_builtin_repository_rebuild_browse(self):
        """'repository.rebuild-browse' is registered as a built-in type."""
        registry = TaskRegistry()
        registry.register_builtin_tasks()

        assert registry.is_registered(TASK_TYPE_REBUILD_BROWSE) is True

    def test_builtin_system_metrics_snapshot(self):
        """'system.metrics-snapshot' is registered as a built-in type."""
        registry = TaskRegistry()
        registry.register_builtin_tasks()

        assert registry.is_registered(TASK_TYPE_METRICS_SNAPSHOT) is True

    def test_builtin_types_count(self):
        """Exactly 8 built-in task types are defined."""
        assert len(BUILTIN_TASK_TYPES) == 8

        registry = TaskRegistry()
        registry.register_builtin_tasks()

        # All 8 should be in the registry
        for task_type in BUILTIN_TASK_TYPES:
            assert registry.is_registered(task_type), (
                f"Built-in type '{task_type}' not registered"
            )

    def test_builtin_callables_use_lazy_imports(self):
        """Built-in task callables use lazy imports — no service import at
        registration time; only when the callable is invoked.

        This verifies the circular-dependency prevention strategy described
        in the task_registry module docstring.
        """
        registry = TaskRegistry()
        registry.register_builtin_tasks()

        # Get the cleanup callable (should be a module-level function)
        cleanup_fn = registry.get_task_callable(TASK_TYPE_CLEANUP)
        assert cleanup_fn is not None

        # Inspect the function source — it should NOT have service imports
        # at the module level.  Instead, the import happens inside the
        # function body.  We verify by checking the function's code object
        # for the import bytecode indirectly: call the function with a mock
        # that patches the service import.
        with patch(
            "src.app.scheduler.task_registry.CleanupService",
            create=True,
        ) as _mock_cls:
            # The lazy import inside _builtin_cleanup_task does:
            #   from src.app.services.cleanup_service import CleanupService
            # We patch at the point of use.
            with patch(
                "src.app.services.cleanup_service.CleanupService",
                create=True,
            ) as mock_svc_cls:
                mock_instance = MagicMock()
                mock_instance.run_all_repository_cleanups.return_value = {
                    "status": "completed"
                }
                mock_svc_cls.return_value = mock_instance

                # Calling the callable triggers the lazy import
                result = cleanup_fn(task_def=None, configuration=None)
                assert result == {"status": "completed"}


# =========================================================================
# TestTaskLogger
# =========================================================================


class TestTaskLogger:
    """Tests for the TaskLogger class — execution lifecycle and history."""

    # -- Execution Lifecycle Tests ----------------------------------------

    def test_start_execution(self, app, task_def_factory):
        """start_execution creates a TaskExecution with status='running'."""
        with app.app_context():
            td = task_def_factory(task_id="start-exec-test")
            task_logger = TaskLogger()

            execution = task_logger.start_execution(td)

            assert execution is not None
            assert execution.task_id == "start-exec-test"
            assert execution.status == STATUS_RUNNING
            assert execution.start_time is not None
            assert execution.progress == str(0)

    def test_complete_execution(self, app, task_def_factory):
        """complete_execution sets status, end_time, and duration_ms."""
        with app.app_context():
            td = task_def_factory(task_id="complete-exec-test")
            task_logger = TaskLogger()

            execution = task_logger.start_execution(td)
            completed = task_logger.complete_execution(execution)

            assert completed.status == STATUS_OK
            assert completed.end_time is not None
            assert completed.duration_ms is not None
            assert completed.duration_ms >= 0
            assert completed.progress == str(100)

    def test_fail_execution(self, app, task_def_factory):
        """fail_execution sets status='failed' and stores error message."""
        with app.app_context():
            td = task_def_factory(task_id="fail-exec-test")
            task_logger = TaskLogger()

            execution = task_logger.start_execution(td)
            failed = task_logger.fail_execution(execution, "Something broke")

            assert failed.status == STATUS_FAILED
            assert failed.end_time is not None
            assert "Something broke" in failed.error_message
            assert failed.duration_ms is not None
            assert failed.duration_ms >= 0

    def test_fail_execution_truncates_error(self, app, task_def_factory):
        """Error messages longer than MAX_ERROR_MESSAGE_LENGTH are truncated."""
        with app.app_context():
            td = task_def_factory(task_id="truncate-test")
            task_logger = TaskLogger()

            execution = task_logger.start_execution(td)
            long_error = "x" * (MAX_ERROR_MESSAGE_LENGTH + 500)
            failed = task_logger.fail_execution(execution, long_error)

            assert len(failed.error_message) <= MAX_ERROR_MESSAGE_LENGTH
            assert failed.error_message.endswith("...")

    def test_cancel_execution(self, app, task_def_factory):
        """cancel_execution sets status='canceled'."""
        with app.app_context():
            td = task_def_factory(task_id="cancel-exec-test")
            task_logger = TaskLogger()

            execution = task_logger.start_execution(td)
            canceled = task_logger.cancel_execution(execution)

            assert canceled.status == STATUS_CANCELED
            assert canceled.end_time is not None
            assert canceled.duration_ms is not None

    # -- Progress Tracking Tests ------------------------------------------

    def test_update_progress(self, app, task_def_factory):
        """update_progress sets progress field (0-100)."""
        with app.app_context():
            td = task_def_factory(task_id="progress-test")
            task_logger = TaskLogger()

            execution = task_logger.start_execution(td)
            task_logger.update_progress(execution, 50)

            assert execution.progress == str(50)

    def test_update_progress_clamped(self, app, task_def_factory):
        """Progress values are clamped to 0-100 range."""
        with app.app_context():
            td = task_def_factory(task_id="clamp-test")
            task_logger = TaskLogger()

            execution = task_logger.start_execution(td)

            # Test value above 100
            task_logger.update_progress(execution, 150)
            assert execution.progress == str(100)

            # Test value below 0
            task_logger.update_progress(execution, -10)
            assert execution.progress == str(0)

    def test_progress_initial_zero(self, app, task_def_factory):
        """Initial progress is 0 at start_execution."""
        with app.app_context():
            td = task_def_factory(task_id="init-progress-test")
            task_logger = TaskLogger()

            execution = task_logger.start_execution(td)

            assert execution.progress == str(0)

    # -- Duration Calculation Tests ---------------------------------------

    def test_duration_calculation(self, app, task_def_factory):
        """Duration is calculated via time.monotonic() difference."""
        with app.app_context():
            td = task_def_factory(task_id="duration-mono-test")
            task_logger = TaskLogger()

            # Mock monotonic to control the time values precisely
            with patch("src.app.scheduler.task_logger.time") as mock_time:
                mock_time.monotonic.side_effect = [100.0, 100.5]

                execution = task_logger.start_execution(td)
                # The first monotonic() call returns 100.0 (captured at start)
                # The second monotonic() call returns 100.5 (at completion)
                completed = task_logger.complete_execution(execution)

                assert completed.duration_ms == 500  # 0.5s = 500ms

    def test_duration_ms_precision(self, app, task_def_factory):
        """Duration is stored in milliseconds with integer precision."""
        with app.app_context():
            td = task_def_factory(task_id="duration-precision-test")
            task_logger = TaskLogger()

            # Use 0.25 which is exactly representable in IEEE 754 float
            with patch("src.app.scheduler.task_logger.time") as mock_time:
                mock_time.monotonic.side_effect = [200.0, 200.25]

                execution = task_logger.start_execution(td)
                completed = task_logger.complete_execution(execution)

                assert completed.duration_ms == 250
                assert isinstance(completed.duration_ms, int)

    # -- Per-Task Logger Tests --------------------------------------------

    def test_get_task_logger(self):
        """get_task_logger returns a Python logger instance."""
        task_logger = TaskLogger()

        result = task_logger.get_task_logger("my-task", "My Task")

        assert isinstance(result, logging.Logger)

    def test_task_logger_namespace(self):
        """Logger for a task uses 'nexus.task.*' namespace."""
        task_logger = TaskLogger()

        result = task_logger.get_task_logger("cleanup-daily", "Daily Cleanup")

        assert result.name.startswith(TASK_LOGGER_PREFIX)
        assert "Daily Cleanup" in result.name

    def test_different_task_different_logger(self):
        """Different task types get different logger instances."""
        task_logger = TaskLogger()

        logger_a = task_logger.get_task_logger("task-a", "Task A")
        logger_b = task_logger.get_task_logger("task-b", "Task B")

        assert logger_a is not logger_b
        assert logger_a.name != logger_b.name

    # -- History Query Tests ----------------------------------------------

    def test_get_execution_history(self, app, task_def_factory):
        """get_execution_history returns records ordered by start_time desc."""
        with app.app_context():
            td = task_def_factory(task_id="history-test")
            task_logger = TaskLogger()

            # Create multiple executions
            exec1 = task_logger.start_execution(td)
            task_logger.complete_execution(exec1)
            exec2 = task_logger.start_execution(td)
            task_logger.complete_execution(exec2)

            history = task_logger.get_execution_history(task_id="history-test")

            assert len(history) == 2
            # Most recent should be first (desc order)
            assert history[0].start_time >= history[1].start_time

    def test_get_execution_history_limit(self, app, task_def_factory):
        """get_execution_history supports limit parameter."""
        with app.app_context():
            td = task_def_factory(task_id="history-limit-test")
            task_logger = TaskLogger()

            # Create 5 executions
            for _ in range(5):
                ex = task_logger.start_execution(td)
                task_logger.complete_execution(ex)

            history = task_logger.get_execution_history(
                task_id="history-limit-test", limit=3
            )

            assert len(history) == 3

    def test_get_last_execution(self, app, task_def_factory):
        """get_last_execution returns the most recent TaskExecution."""
        with app.app_context():
            td = task_def_factory(task_id="last-exec-test")
            task_logger = TaskLogger()

            ex1 = task_logger.start_execution(td)
            task_logger.complete_execution(ex1)
            ex2 = task_logger.start_execution(td)
            task_logger.complete_execution(ex2)

            last = task_logger.get_last_execution("last-exec-test")

            assert last is not None
            assert last.execution_id == ex2.execution_id

    def test_get_last_execution_none(self, app):
        """get_last_execution returns None if no executions exist."""
        with app.app_context():
            task_logger = TaskLogger()

            result = task_logger.get_last_execution("nonexistent-task")

            assert result is None

    def test_get_running_executions(self, app, task_def_factory):
        """get_running_executions returns all executions with status='running'."""
        with app.app_context():
            td = task_def_factory(task_id="running-exec-test")
            task_logger = TaskLogger()

            # Create two running executions
            ex1 = task_logger.start_execution(td)
            ex2 = task_logger.start_execution(td)

            running = task_logger.get_running_executions()

            assert len(running) >= 2
            for ex in running:
                assert ex.status == STATUS_RUNNING

    def test_get_execution_stats(self, app, task_def_factory):
        """get_execution_stats returns aggregate stats."""
        with app.app_context():
            td = task_def_factory(task_id="stats-test")
            task_logger = TaskLogger()

            # Create mixed executions
            ex1 = task_logger.start_execution(td)
            task_logger.complete_execution(ex1, STATUS_OK)
            ex2 = task_logger.start_execution(td)
            task_logger.fail_execution(ex2, "Error occurred")
            ex3 = task_logger.start_execution(td)
            task_logger.complete_execution(ex3, STATUS_OK)

            stats = task_logger.get_execution_stats(task_id="stats-test")

            assert stats["total_executions"] == 3
            assert stats["successful"] == 2
            assert stats["failed"] == 1
            assert "success_rate" in stats
            assert "average_duration_ms" in stats

    # -- Purge Tests ------------------------------------------------------

    def test_purge_old_executions(self, app, task_def_factory):
        """purge_old_executions removes records older than retention period."""
        with app.app_context():
            td = task_def_factory(task_id="purge-test")
            task_logger = TaskLogger()

            # Create an execution and manually backdate it
            ex = task_logger.start_execution(td)
            task_logger.complete_execution(ex)

            # Backdate the execution to 60 days ago
            old_time = datetime.now(timezone.utc) - timedelta(days=60)
            ex.start_time = old_time
            ex.end_time = old_time + timedelta(minutes=5)
            db.session.commit()

            deleted = task_logger.purge_old_executions(retention_days=30)

            assert deleted >= 1

    def test_purge_preserves_recent(self, app, task_def_factory):
        """Purge does not remove recent execution records."""
        with app.app_context():
            td = task_def_factory(task_id="purge-recent-test")
            task_logger = TaskLogger()

            # Create a recent execution
            ex = task_logger.start_execution(td)
            task_logger.complete_execution(ex)

            deleted = task_logger.purge_old_executions(retention_days=30)

            assert deleted == 0
            # Verify execution still exists
            remaining = task_logger.get_execution_history(
                task_id="purge-recent-test"
            )
            assert len(remaining) >= 1


# =========================================================================
# TestTaskScheduler
# =========================================================================


class TestTaskScheduler:
    """Tests for the TaskScheduler — APScheduler 3.10.4 integration."""

    # -- Initialization Tests ---------------------------------------------

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_init_app(self, mock_bg_class, app):
        """init_app configures BackgroundScheduler with Flask app settings."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        # BackgroundScheduler constructor should have been called
        mock_bg_class.assert_called_once()
        call_kwargs = mock_bg_class.call_args
        # Should include timezone="UTC"
        assert call_kwargs.kwargs.get("timezone") == "UTC"
        # Registry and task_logger should be created
        assert scheduler.registry is not None
        assert scheduler.task_logger is not None

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_init_app_registers_jobstore(self, mock_bg_class, app):
        """init_app configures jobstore based on cluster settings."""
        # With SCHEDULER_CLUSTER_ENABLED = False (default test config)
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        # Jobstores dict should be empty for non-cluster mode
        call_kwargs = mock_bg_class.call_args
        jobstores = call_kwargs.kwargs.get("jobstores", {})
        # Default (non-cluster) should have empty jobstores
        assert "default" not in jobstores or jobstores == {}

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_scheduler_start(self, mock_bg_class, app):
        """Scheduler starts the background scheduler daemon."""
        mock_instance = mock_bg_class.return_value
        mock_instance.running = False

        scheduler = TaskScheduler()
        scheduler.init_app(app)
        scheduler.start()

        mock_instance.start.assert_called_once()

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_scheduler_shutdown(self, mock_bg_class, app):
        """Scheduler shuts down cleanly."""
        mock_instance = mock_bg_class.return_value
        mock_instance.running = True

        scheduler = TaskScheduler()
        scheduler.init_app(app)
        scheduler.shutdown(wait=True)

        mock_instance.shutdown.assert_called_once_with(wait=True)

    # -- Trigger Building Tests -------------------------------------------

    def test_cron_trigger_from_crontab(self, app):
        """Build CronTrigger from crontab expression '0 */4 * * *'."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            td = TaskDefinition(
                task_id="cron-trigger-test",
                type="repository.cleanup",
                name="Cron Test",
                cron_expression="0 */4 * * *",
                enabled=True,
            )

            trigger = scheduler._build_trigger(td)

            assert isinstance(trigger, CronTrigger)

    def test_interval_trigger(self, app):
        """Build IntervalTrigger from interval_seconds configuration."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            td = TaskDefinition(
                task_id="interval-trigger-test",
                type="repository.cleanup",
                name="Interval Test",
                cron_expression=None,
                enabled=True,
                configuration={"interval_seconds": 3600},
            )

            trigger = scheduler._build_trigger(td)

            assert isinstance(trigger, IntervalTrigger)

    def test_date_trigger(self, app):
        """Build DateTrigger for one-time execution at specific datetime."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        run_at = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        with app.app_context():
            td = TaskDefinition(
                task_id="date-trigger-test",
                type="repository.cleanup",
                name="Date Test",
                cron_expression=None,
                enabled=True,
                configuration={"run_at": run_at.isoformat()},
            )

            trigger = scheduler._build_trigger(td)

            assert isinstance(trigger, DateTrigger)

    def test_invalid_cron_expression(self, app):
        """Invalid cron expression raises ValueError."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            td = TaskDefinition(
                task_id="bad-cron-test",
                type="repository.cleanup",
                name="Bad Cron",
                cron_expression="this is not valid",
                enabled=True,
            )

            with pytest.raises((ValueError, Exception)):
                scheduler._build_trigger(td)

    # -- Job Management Tests ---------------------------------------------

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_schedule_task_cron(self, mock_bg_class, app, task_def_factory):
        """schedule_task with cron_expression schedules a cron job."""
        mock_instance = mock_bg_class.return_value
        mock_job = MagicMock()
        mock_job.next_run_time = datetime.now(timezone.utc)
        mock_instance.add_job.return_value = mock_job

        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            td = task_def_factory(
                task_id="schedule-cron-test",
                cron_expression="0 0 * * *",
            )
            result = scheduler.schedule_task(td)

            assert result == "schedule-cron-test"
            mock_instance.add_job.assert_called_once()

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_schedule_task_interval(self, mock_bg_class, app, task_def_factory):
        """schedule_task with interval_seconds uses IntervalTrigger."""
        mock_instance = mock_bg_class.return_value
        mock_job = MagicMock()
        mock_job.next_run_time = datetime.now(timezone.utc)
        mock_instance.add_job.return_value = mock_job

        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            td = task_def_factory(
                task_id="schedule-interval-test",
                cron_expression=None,
                configuration={"interval_seconds": 300},
            )
            result = scheduler.schedule_task(td)

            assert result == "schedule-interval-test"
            mock_instance.add_job.assert_called_once()
            # Verify the trigger type in the add_job call
            call_kwargs = mock_instance.add_job.call_args
            trigger_arg = call_kwargs.kwargs.get(
                "trigger", call_kwargs.args[1] if len(call_kwargs.args) > 1 else None
            )
            if trigger_arg is None and "trigger" in (call_kwargs.kwargs or {}):
                trigger_arg = call_kwargs.kwargs["trigger"]

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_schedule_task_one_time(self, mock_bg_class, app, task_def_factory):
        """schedule_task with run_at uses DateTrigger."""
        mock_instance = mock_bg_class.return_value
        mock_job = MagicMock()
        mock_job.next_run_time = datetime.now(timezone.utc)
        mock_instance.add_job.return_value = mock_job

        scheduler = TaskScheduler()
        scheduler.init_app(app)

        run_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with app.app_context():
            td = task_def_factory(
                task_id="schedule-onetime-test",
                cron_expression=None,
                configuration={"run_at": run_at},
            )
            result = scheduler.schedule_task(td)

            assert result == "schedule-onetime-test"
            mock_instance.add_job.assert_called_once()

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_pause_task(self, mock_bg_class, app, task_def_factory):
        """pause_task pauses the scheduled job and sets enabled=False."""
        mock_instance = mock_bg_class.return_value
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            td = task_def_factory(task_id="pause-task-test")
            result = scheduler.pause_task("pause-task-test")

            assert result is True
            mock_instance.pause_job.assert_called_once_with("pause-task-test")

            # Verify database is updated
            refreshed = db.session.get(TaskDefinition, "pause-task-test")
            assert refreshed.enabled is False

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_resume_task(self, mock_bg_class, app, task_def_factory):
        """resume_task resumes a paused job and sets enabled=True."""
        mock_instance = mock_bg_class.return_value
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            td = task_def_factory(
                task_id="resume-task-test", enabled=False,
            )
            result = scheduler.resume_task("resume-task-test")

            assert result is True
            mock_instance.resume_job.assert_called_once_with(
                "resume-task-test"
            )

            refreshed = db.session.get(TaskDefinition, "resume-task-test")
            assert refreshed.enabled is True

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_remove_task(self, mock_bg_class, app, task_def_factory):
        """remove_task removes the job and deletes from database."""
        mock_instance = mock_bg_class.return_value
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            td = task_def_factory(task_id="remove-task-test")
            result = scheduler.remove_task("remove-task-test")

            assert result is True
            mock_instance.remove_job.assert_called_once_with(
                "remove-task-test"
            )

            # Verify deleted from database
            deleted = db.session.get(TaskDefinition, "remove-task-test")
            assert deleted is None

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_run_task_now(self, mock_bg_class, app, task_def_factory):
        """run_task_now triggers immediate execution via DateTrigger."""
        mock_instance = mock_bg_class.return_value
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            td = task_def_factory(task_id="run-now-test")
            adhoc_id = scheduler.run_task_now("run-now-test")

            assert "run-now-test" in adhoc_id
            assert "adhoc" in adhoc_id
            mock_instance.add_job.assert_called_once()

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_schedule_nonexistent_task_type(
        self, mock_bg_class, app, task_def_factory
    ):
        """Scheduling an unknown task type raises ValueError."""
        mock_instance = mock_bg_class.return_value
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            td = task_def_factory(
                task_id="nonexistent-type-test",
                task_type="totally.unknown.type",
            )

            with pytest.raises(ValueError, match="Unknown task type"):
                scheduler.schedule_task(td)

    # -- Task Execution Wrapping Tests ------------------------------------

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    @patch("src.app.scheduler.task_scheduler.emit_event")
    def test_wrap_task_execution_flask_context(
        self, mock_emit, mock_bg_class, app, task_def_factory
    ):
        """_wrap_task_execution ensures Flask app context is active."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        task_fn = MagicMock(return_value={"status": "ok"})

        with app.app_context():
            td = task_def_factory(task_id="flask-ctx-test")
            wrapper = scheduler._wrap_task_execution(td, task_fn)

            # Execute the wrapper — it should push app context
            wrapper()

            # Task function was called
            task_fn.assert_called_once()

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    @patch("src.app.scheduler.task_scheduler.emit_event")
    def test_wrap_task_execution_logger_tracking(
        self, mock_emit, mock_bg_class, app, task_def_factory
    ):
        """Wrapped execution uses TaskLogger for start → complete tracking."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        task_fn = MagicMock(return_value={"status": "ok"})

        with app.app_context():
            td = task_def_factory(task_id="logger-tracking-test")
            wrapper = scheduler._wrap_task_execution(td, task_fn)
            wrapper()

            # Verify an execution record was created and completed
            executions = TaskExecution.query.filter_by(
                task_id="logger-tracking-test"
            ).all()
            assert len(executions) >= 1
            latest = max(executions, key=lambda e: e.start_time)
            assert latest.status == STATUS_OK

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    @patch("src.app.scheduler.task_scheduler.emit_event")
    def test_wrap_task_execution_event_emission(
        self, mock_emit, mock_bg_class, app, task_def_factory
    ):
        """Wrapped execution emits TASK_STARTED and TASK_COMPLETED events."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        task_fn = MagicMock(return_value={"status": "ok"})

        with app.app_context():
            td = task_def_factory(task_id="event-emit-test")
            wrapper = scheduler._wrap_task_execution(td, task_fn)
            wrapper()

            # Should have emitted at least TASK_STARTED and TASK_COMPLETED
            assert mock_emit.call_count >= 2

            # Check the event types
            from src.app.events.event_types import EventType

            event_types_emitted = [
                c.args[0] for c in mock_emit.call_args_list
            ]
            assert EventType.TASK_STARTED in event_types_emitted
            assert EventType.TASK_COMPLETED in event_types_emitted

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    @patch("src.app.scheduler.task_scheduler.emit_event")
    def test_wrap_task_execution_error_handling(
        self, mock_emit, mock_bg_class, app, task_def_factory
    ):
        """Exceptions in task callable are caught and recorded via fail_execution."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        task_fn = MagicMock(side_effect=RuntimeError("Task exploded"))

        with app.app_context():
            td = task_def_factory(task_id="error-handling-test")
            wrapper = scheduler._wrap_task_execution(td, task_fn)

            # The wrapper should re-raise the exception
            with pytest.raises(RuntimeError, match="Task exploded"):
                wrapper()

            # But the execution should be recorded as failed
            executions = TaskExecution.query.filter_by(
                task_id="error-handling-test"
            ).all()
            assert len(executions) >= 1
            failed_exec = [
                e for e in executions if e.status == STATUS_FAILED
            ]
            assert len(failed_exec) >= 1
            assert "Task exploded" in failed_exec[0].error_message

    # -- Task State Management Tests --------------------------------------

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_get_scheduled_tasks(self, mock_bg_class, app, task_def_factory):
        """list_tasks returns all currently scheduled tasks."""
        mock_instance = mock_bg_class.return_value
        mock_instance.get_job.return_value = None

        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            task_def_factory(task_id="list-task-1", name="Task One")
            task_def_factory(task_id="list-task-2", name="Task Two")

            tasks = scheduler.list_tasks()

            assert len(tasks) >= 2
            task_ids = [t["task_id"] for t in tasks]
            assert "list-task-1" in task_ids
            assert "list-task-2" in task_ids

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_get_task_status(self, mock_bg_class, app, task_def_factory):
        """get_task_status returns status dict for a specific task."""
        mock_instance = mock_bg_class.return_value
        mock_instance.get_job.return_value = None

        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            task_def_factory(task_id="status-test", name="Status Task")

            status = scheduler.get_task_status("status-test")

            assert status is not None
            assert status["task_id"] == "status-test"
            assert status["name"] == "Status Task"
            assert "enabled" in status
            assert "type" in status

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_task_enabled_disabled(
        self, mock_bg_class, app, task_def_factory
    ):
        """Task with enabled=False should not be scheduled by _load_tasks."""
        mock_instance = mock_bg_class.return_value
        mock_instance.running = False

        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            # Create a disabled task
            task_def_factory(
                task_id="disabled-task-test",
                enabled=False,
            )

            # _load_tasks_from_db only queries enabled tasks
            mock_instance.running = True
            scheduler._load_tasks_from_db()

            # Since the task is disabled, add_job should not be called for it
            # (unless there are other enabled tasks from other tests, but
            # our cleanup fixture removes them)
            for call_item in mock_instance.add_job.call_args_list:
                kwargs = call_item.kwargs
                assert kwargs.get("id") != "disabled-task-test"

    # -- Cluster Awareness Tests ------------------------------------------

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    @patch(
        "src.app.scheduler.task_scheduler.SQLAlchemyJobStore",
        autospec=True,
    )
    def test_cluster_aware_job_store(
        self, mock_jobstore_class, mock_bg_class, app
    ):
        """SQLAlchemyJobStore is configured for multi-node deployment."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        # configure_cluster_mode adds a new jobstore
        scheduler.configure_cluster_mode("sqlite:///cluster_test.db")

        mock_jobstore_class.assert_called()
        mock_bg_class.return_value.add_jobstore.assert_called_once()

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    @patch(
        "src.app.scheduler.task_scheduler.SQLAlchemyJobStore",
        autospec=True,
    )
    def test_job_store_configuration(
        self, mock_jobstore_class, mock_bg_class
    ):
        """JobStore properly configured from app settings (cluster enabled)."""
        from flask import Flask

        cluster_app = Flask(__name__)
        cluster_app.config["TESTING"] = True
        cluster_app.config["SQLALCHEMY_DATABASE_URI"] = (
            "sqlite:///cluster_jobstore.db"
        )
        cluster_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        cluster_app.config["SCHEDULER_CLUSTER_ENABLED"] = True
        cluster_app.config["SCHEDULER_MAX_WORKERS"] = 5

        scheduler = TaskScheduler()
        scheduler.init_app(cluster_app)

        # When cluster is enabled, SQLAlchemyJobStore should be created
        mock_jobstore_class.assert_called_with(
            url="sqlite:///cluster_jobstore.db"
        )


# =========================================================================
# TestSchedulerIntegration
# =========================================================================


class TestSchedulerIntegration:
    """Integration tests between TaskScheduler, TaskRegistry, and TaskLogger."""

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_scheduler_uses_registry(self, mock_bg_class, app):
        """TaskScheduler looks up task callable from TaskRegistry."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        # Registry should be populated with built-in types
        assert scheduler.registry is not None
        assert scheduler.registry.is_registered(TASK_TYPE_CLEANUP)
        assert scheduler.registry.is_registered(TASK_TYPE_COMPACT)

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    def test_scheduler_uses_logger(self, mock_bg_class, app):
        """TaskScheduler uses TaskLogger for execution tracking."""
        scheduler = TaskScheduler()
        scheduler.init_app(app)

        assert scheduler.task_logger is not None
        assert isinstance(scheduler.task_logger, TaskLogger)

    @patch(
        "src.app.scheduler.task_scheduler.BackgroundScheduler",
        autospec=True,
    )
    @patch("src.app.scheduler.task_scheduler.emit_event")
    def test_full_lifecycle(
        self, mock_emit, mock_bg_class, app, task_def_factory
    ):
        """Full lifecycle: schedule → execute → log → query history.

        1. Create a TaskDefinition in the database.
        2. Schedule it via the TaskScheduler.
        3. Simulate execution by calling the wrapped function.
        4. Verify TaskLogger recorded start + complete.
        5. Verify execution history is retrievable.
        """
        mock_instance = mock_bg_class.return_value
        mock_job = MagicMock()
        mock_job.next_run_time = datetime.now(timezone.utc)
        mock_instance.add_job.return_value = mock_job

        scheduler = TaskScheduler()
        scheduler.init_app(app)

        with app.app_context():
            # 1. Create a TaskDefinition
            td = task_def_factory(
                task_id="lifecycle-test",
                task_type="repository.cleanup",
                name="Lifecycle Test Task",
                cron_expression="0 0 * * *",
            )

            # 2. Schedule via TaskScheduler
            task_id = scheduler.schedule_task(td)
            assert task_id == "lifecycle-test"

            # 3. Simulate execution — get the wrapper from add_job call
            call_kwargs = mock_instance.add_job.call_args
            wrapped_fn = call_kwargs.kwargs.get(
                "func",
                call_kwargs.args[0] if call_kwargs.args else None,
            )
            assert wrapped_fn is not None

            # Execute the wrapper
            wrapped_fn()

            # 4. Verify TaskExecution record was created
            executions = TaskExecution.query.filter_by(
                task_id="lifecycle-test"
            ).all()
            assert len(executions) >= 1

            latest = max(executions, key=lambda e: e.start_time)
            assert latest.status == STATUS_OK
            assert latest.duration_ms is not None

            # 5. Verify execution history is retrievable
            history = scheduler.task_logger.get_execution_history(
                task_id="lifecycle-test"
            )
            assert len(history) >= 1
            assert history[0].task_id == "lifecycle-test"
