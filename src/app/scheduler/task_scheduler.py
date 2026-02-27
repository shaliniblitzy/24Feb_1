"""
Core APScheduler Integration — Task Scheduler Module.

Replaces **Quartz 2.3.2** from the Java source system (Sonatype Nexus
Repository) with **APScheduler 3.10.4**.  This is the primary integration
point between the APScheduler background scheduler and the Flask application,
implementing **Feature F-402 (Scheduled Tasks)**.

**Responsibilities:**

- Job management: add, remove, pause, resume scheduled tasks.
- Cron expression parsing via ``CronTrigger.from_crontab()`` (standard
  5-field format).
- Cluster-aware execution for multi-node deployments via
  ``SQLAlchemyJobStore`` (replaces Quartz's clustered JDBC job store).
- Synchronisation between APScheduler jobs and the database-backed
  ``TaskDefinition`` / ``TaskExecution`` models.
- Event emission (``TASK_STARTED`` / ``TASK_COMPLETED``) for decoupled
  audit logging (F-303), webhook dispatch (F-503), and metrics tracking.

**Architecture Context:**

+-------------------------------+----------------------------------------------+
| Java Source                   | Python Target                                |
+===============================+==============================================+
| Quartz ``Scheduler``          | ``TaskScheduler`` (this class)               |
+-------------------------------+----------------------------------------------+
| Quartz ``JobDetail``          | ``TaskDefinition`` SQLAlchemy model           |
+-------------------------------+----------------------------------------------+
| Quartz ``Trigger``            | APScheduler triggers (Cron/Date/Interval)     |
+-------------------------------+----------------------------------------------+
| Quartz ``JobStore`` (JDBC)    | ``SQLAlchemyJobStore`` (cluster mode)         |
+-------------------------------+----------------------------------------------+
| Quartz ``JobListener``        | APScheduler event listeners                   |
+-------------------------------+----------------------------------------------+

**Design Patterns:**

- **Application Factory** — ``init_app(app)`` for Flask integration.
- **Observer** — Blinker events for decoupled audit/webhook dispatch.
- **Strategy** — Pluggable triggers (Cron, Date, Interval).
- **Facade** — Unified interface wrapping APScheduler internals.

**Critical Constraints:**

- All task execution runs within ``with self._app.app_context()`` because
  APScheduler uses background threads without Flask context.
- System uptime ≥ 99.9% — scheduler must be resilient and must not crash
  the application on individual task failures.
- Python 3.12+ with type hints.
- No Java dependencies.

Exports:
    TaskScheduler               — Core scheduler class.
    DEFAULT_MAX_WORKERS         — Default thread pool size (10).
    DEFAULT_MISFIRE_GRACE_TIME  — Default misfire grace period (60 s).
    DEFAULT_COALESCE            — Default coalesce setting (True).
    STATUS_WAITING              — Task status: waiting.
    STATUS_RUNNING              — Task status: running.
    STATUS_OK                   — Task status: ok.
    STATUS_FAILED               — Task status: failed.
    STATUS_CANCELED             — Task status: canceled.
"""

from __future__ import annotations

import logging
import functools
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from flask import current_app

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.events import (
    EVENT_JOB_EXECUTED,
    EVENT_JOB_ERROR,
    EVENT_JOB_MISSED,
    EVENT_JOB_ADDED,
    EVENT_JOB_REMOVED,
    JobExecutionEvent,
)
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor, ProcessPoolExecutor

from src.app.extensions import db
from src.app.models.task import TaskDefinition, TaskExecution
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.scheduler.task_registry import TaskRegistry
from src.app.scheduler.task_logger import TaskLogger

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for scheduler lifecycle events, job execution,
# error reporting, and missed-job warnings.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default Scheduler Configuration Constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_WORKERS: int = 10
"""Default maximum number of concurrent worker threads for task execution.

Controls the ``ThreadPoolExecutor`` ``max_workers`` parameter.  APScheduler
uses this pool to run jobs concurrently.  Adjust via the Flask config key
``SCHEDULER_MAX_WORKERS``.
"""

DEFAULT_MISFIRE_GRACE_TIME: int = 60
"""Default misfire grace time in seconds.

If a job's scheduled fire time is missed by more than this many seconds
(e.g., due to high system load or scheduler downtime), the job is considered
misfired.  Misfired jobs are either coalesced or skipped depending on the
``coalesce`` setting.  Adjust via ``SCHEDULER_MISFIRE_GRACE_TIME``.
"""

DEFAULT_COALESCE: bool = True
"""Default coalesce setting for missed job runs.

When ``True``, multiple misfired runs that accumulated during downtime are
collapsed into a single execution.  When ``False``, each missed run fires
individually (may cause a burst of executions after downtime).  Adjust via
``SCHEDULER_COALESCE``.
"""

# ---------------------------------------------------------------------------
# Task Execution Status Constants
# ---------------------------------------------------------------------------
# These must match the ``TaskExecution.status`` column values defined in
# ``src.app.models.task``.  They mirror Quartz 2.3.2 execution lifecycle
# states from the Java source system.
# ---------------------------------------------------------------------------

STATUS_WAITING: str = "waiting"
"""Task execution is queued but has not started yet."""

STATUS_RUNNING: str = "running"
"""Task execution is currently in progress."""

STATUS_OK: str = "ok"
"""Task execution completed successfully."""

STATUS_FAILED: str = "failed"
"""Task execution terminated with an error."""

STATUS_CANCELED: str = "canceled"
"""Task execution was manually or programmatically canceled."""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "TaskScheduler",
    "DEFAULT_MAX_WORKERS",
    "DEFAULT_MISFIRE_GRACE_TIME",
    "DEFAULT_COALESCE",
    "STATUS_WAITING",
    "STATUS_RUNNING",
    "STATUS_OK",
    "STATUS_FAILED",
    "STATUS_CANCELED",
]


# ===========================================================================
# TaskScheduler Class
# ===========================================================================


class TaskScheduler:
    """Core task scheduler integrating APScheduler 3.10.4 with Flask.

    Manages the complete lifecycle of background scheduled tasks:

    - **Initialisation** — ``init_app()`` configures APScheduler with Flask
      app settings, creates the ``TaskRegistry`` and ``TaskLogger``, and
      registers event listeners.
    - **Lifecycle** — ``start()`` / ``shutdown()`` control the background
      scheduler thread.
    - **Scheduling** — ``schedule_task()``, ``schedule_one_time_task()``,
      ``schedule_interval_task()``, ``schedule_cron_task()`` create and
      register jobs.
    - **Management** — ``pause_task()``, ``resume_task()``, ``remove_task()``,
      ``run_task_now()`` control individual task states.
    - **Querying** — ``get_task_status()``, ``list_tasks()``,
      ``get_scheduled_jobs()``, ``get_execution_history()`` provide status
      and history information.
    - **Cluster** — ``configure_cluster_mode()`` enables multi-node-safe
      scheduling via ``SQLAlchemyJobStore``.

    **Thread Safety:**
    APScheduler's ``BackgroundScheduler`` runs in a daemon thread.  All task
    execution wrappers inject the Flask application context via
    ``with self._app.app_context()`` to ensure database access and other
    Flask-dependent operations work correctly.

    **Usage Example::

        scheduler = TaskScheduler()
        scheduler.init_app(app)
        scheduler.start()

        # Schedule a cron task
        scheduler.schedule_cron_task(
            task_type='repository.cleanup',
            name='Nightly Cleanup',
            cron_expression='0 0 * * *',
            configuration={'repositoryName': '*'},
        )

        # Trigger immediate execution
        scheduler.run_task_now('cleanup-snapshots-daily')

        # Graceful shutdown
        scheduler.shutdown()
    """

    def __init__(self, app: Any | None = None) -> None:
        """Initialise the TaskScheduler.

        Optionally accepts a Flask app for immediate configuration.  If no
        app is provided, ``init_app()`` must be called later.

        Args:
            app: Optional Flask application instance.
        """
        self.logger: logging.Logger = logging.getLogger(__name__)
        self._scheduler: BackgroundScheduler | None = None
        self._registry: TaskRegistry | None = None
        self._task_logger: TaskLogger | None = None
        self._app: Any | None = None

        if app is not None:
            self.init_app(app)

    # ------------------------------------------------------------------
    # Flask Application Factory Integration
    # ------------------------------------------------------------------

    def init_app(self, app: Any) -> None:
        """Configure the task scheduler with a Flask application.

        This method implements the Flask Application Factory pattern:

        1. Stores the Flask app reference for context injection.
        2. Creates ``TaskRegistry`` and ``TaskLogger`` instances.
        3. Configures APScheduler ``BackgroundScheduler`` with settings
           from Flask config.
        4. Registers APScheduler event listeners for job lifecycle events.
        5. Registers built-in task types via the ``TaskRegistry``.

        Flask Config Keys:
            SCHEDULER_CLUSTER_ENABLED (bool): Enable SQLAlchemy-backed job
                store for cluster-aware scheduling.  Default ``False``.
            SQLALCHEMY_DATABASE_URI (str): Database URL for the job store
                (used only when cluster mode is enabled).
            SCHEDULER_MAX_WORKERS (int): Thread pool size.
                Default ``DEFAULT_MAX_WORKERS``.
            SCHEDULER_COALESCE (bool): Coalesce missed runs.
                Default ``DEFAULT_COALESCE``.
            SCHEDULER_MAX_INSTANCES (int): Max concurrent instances per job.
                Default ``1``.
            SCHEDULER_MISFIRE_GRACE_TIME (int): Misfire grace period in
                seconds.  Default ``DEFAULT_MISFIRE_GRACE_TIME``.

        Args:
            app: Flask application instance.
        """
        self._app = app

        # Initialise task registry and logger
        self._registry = TaskRegistry()
        self._task_logger = TaskLogger()

        # ----------------------------------------------------------
        # Configure APScheduler job stores
        # ----------------------------------------------------------
        jobstores: dict[str, Any] = {}
        if app.config.get("SCHEDULER_CLUSTER_ENABLED", False):
            db_url = app.config.get("SQLALCHEMY_DATABASE_URI")
            if db_url:
                jobstores["default"] = SQLAlchemyJobStore(url=db_url)
                self.logger.info(
                    "Cluster-aware scheduling enabled via SQLAlchemyJobStore"
                )

        # ----------------------------------------------------------
        # Configure executors
        # ----------------------------------------------------------
        executors: dict[str, Any] = {
            "default": ThreadPoolExecutor(
                max_workers=app.config.get(
                    "SCHEDULER_MAX_WORKERS", DEFAULT_MAX_WORKERS
                )
            ),
        }

        # ----------------------------------------------------------
        # Configure job defaults
        # ----------------------------------------------------------
        job_defaults: dict[str, Any] = {
            "coalesce": app.config.get("SCHEDULER_COALESCE", DEFAULT_COALESCE),
            "max_instances": app.config.get("SCHEDULER_MAX_INSTANCES", 1),
            "misfire_grace_time": app.config.get(
                "SCHEDULER_MISFIRE_GRACE_TIME", DEFAULT_MISFIRE_GRACE_TIME
            ),
        }

        # ----------------------------------------------------------
        # Create the BackgroundScheduler
        # ----------------------------------------------------------
        self._scheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
            timezone="UTC",
        )

        # ----------------------------------------------------------
        # Register APScheduler event listeners
        # ----------------------------------------------------------
        self._scheduler.add_listener(
            self._on_job_executed, EVENT_JOB_EXECUTED
        )
        self._scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        self._scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)

        # ----------------------------------------------------------
        # Register built-in task types
        # ----------------------------------------------------------
        self._registry.register_builtin_tasks()

        self.logger.info(
            "TaskScheduler initialised: max_workers=%d, coalesce=%s, "
            "misfire_grace=%ds, cluster=%s",
            app.config.get("SCHEDULER_MAX_WORKERS", DEFAULT_MAX_WORKERS),
            app.config.get("SCHEDULER_COALESCE", DEFAULT_COALESCE),
            app.config.get(
                "SCHEDULER_MISFIRE_GRACE_TIME", DEFAULT_MISFIRE_GRACE_TIME
            ),
            app.config.get("SCHEDULER_CLUSTER_ENABLED", False),
        )

    # ------------------------------------------------------------------
    # Scheduler Lifecycle Management
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background scheduler and load tasks from the database.

        Starts the APScheduler ``BackgroundScheduler`` daemon thread and
        then loads all enabled ``TaskDefinition`` records from the database,
        scheduling each one with APScheduler.

        This method is idempotent — calling it when the scheduler is already
        running has no effect.

        Called from ``factory.py`` during app initialisation when
        ``SCHEDULER_ENABLED=True``.
        """
        if self._scheduler and not self._scheduler.running:
            self._scheduler.start()
            self.logger.info("Task scheduler started")
            self._load_tasks_from_db()

    def shutdown(self, wait: bool = True) -> None:
        """Gracefully shut down the background scheduler.

        Args:
            wait: If ``True`` (default), wait for currently running jobs to
                complete before returning.  If ``False``, interrupt running
                jobs immediately.

        Called during application shutdown / teardown.
        """
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            self.logger.info("Task scheduler shut down (wait=%s)", wait)

    @property
    def is_running(self) -> bool:
        """Return whether the scheduler is currently active.

        Returns:
            ``True`` if the ``BackgroundScheduler`` is started and running,
            ``False`` otherwise.
        """
        return self._scheduler is not None and self._scheduler.running

    def _load_tasks_from_db(self) -> None:
        """Load all enabled task definitions from the database and schedule.

        Iterates over all ``TaskDefinition`` records with ``enabled=True``
        and registers each one with APScheduler via ``schedule_task()``.
        Errors on individual tasks are logged and skipped — a single bad
        task definition must never prevent other tasks from loading.

        Must run within the Flask application context.
        """
        if not self._app:
            self.logger.warning(
                "Cannot load tasks: no Flask app configured"
            )
            return

        loaded_count = 0
        error_count = 0

        with self._app.app_context():
            try:
                tasks = TaskDefinition.query.filter_by(enabled=True).all()
            except Exception as exc:
                self.logger.error(
                    "Failed to query enabled tasks from database: %s",
                    exc,
                    exc_info=True,
                )
                return

            for task_def in tasks:
                try:
                    self.schedule_task(task_def)
                    loaded_count += 1
                except Exception as exc:
                    error_count += 1
                    self.logger.error(
                        "Failed to load task %s (%s): %s",
                        task_def.task_id,
                        task_def.name,
                        exc,
                        exc_info=True,
                    )

        self.logger.info(
            "Task loading complete: %d loaded, %d errors, %d total enabled",
            loaded_count,
            error_count,
            loaded_count + error_count,
        )

    # ------------------------------------------------------------------
    # Job Scheduling Operations
    # ------------------------------------------------------------------

    def schedule_task(self, task_def: TaskDefinition) -> str:
        """Schedule a task based on its ``TaskDefinition`` model.

        Resolves the task callable from the ``TaskRegistry``, builds the
        appropriate APScheduler trigger, wraps the callable with execution
        tracking, and adds the job to APScheduler.

        Args:
            task_def: The ``TaskDefinition`` instance to schedule.

        Returns:
            The ``task_id`` of the scheduled task.

        Raises:
            ValueError: If the task type is not registered in the
                ``TaskRegistry`` or the schedule type cannot be determined.
            RuntimeError: If the scheduler is not initialised.
        """
        if not self._scheduler:
            raise RuntimeError(
                "TaskScheduler not initialised — call init_app() first"
            )
        if not self._registry:
            raise RuntimeError("TaskRegistry not initialised")

        # Resolve the task callable from the registry
        task_func = self._registry.get_task_callable(task_def.type)
        if task_func is None:
            raise ValueError(
                f"Unknown task type '{task_def.type}' — "
                f"not registered in TaskRegistry"
            )

        # Build the appropriate trigger
        trigger = self._build_trigger(task_def)

        # Wrap the callable with execution tracking, app context, events
        wrapped_func = self._wrap_task_execution(task_def, task_func)

        # Add the job to APScheduler (replace if already exists)
        job = self._scheduler.add_job(
            func=wrapped_func,
            trigger=trigger,
            id=task_def.task_id,
            name=task_def.name,
            replace_existing=True,
        )

        # Update the task definition with the computed next run time
        if job.next_run_time is not None:
            task_def.next_run_time = job.next_run_time
        try:
            db.session.commit()
        except Exception as exc:
            self.logger.warning(
                "Failed to persist next_run_time for task %s: %s",
                task_def.task_id,
                exc,
            )

        self.logger.debug(
            "Task scheduled: %s (%s), next_run=%s",
            task_def.task_id,
            task_def.name,
            job.next_run_time,
        )

        return task_def.task_id

    def schedule_one_time_task(
        self,
        task_type: str,
        name: str,
        run_at: datetime,
        configuration: dict[str, Any] | None = None,
    ) -> str:
        """Create and schedule a one-time task.

        Creates a new ``TaskDefinition`` record for a single execution at
        the specified ``run_at`` datetime, then schedules it with APScheduler
        using a ``DateTrigger``.

        Args:
            task_type: Task type identifier (e.g., ``'repository.cleanup'``).
            name: Human-readable task name.
            run_at: UTC datetime for the single execution.
            configuration: Optional task-specific configuration dict.

        Returns:
            The generated ``task_id``.
        """
        task_id = f"{task_type}_{uuid.uuid4().hex[:12]}"
        config = configuration or {}
        config["run_at"] = run_at.isoformat()

        task_def = TaskDefinition(
            task_id=task_id,
            type=task_type,
            name=name,
            cron_expression=None,
            enabled=True,
            configuration=config,
        )

        with self._app.app_context():
            db.session.add(task_def)
            db.session.commit()
            return self.schedule_task(task_def)

    def schedule_interval_task(
        self,
        task_type: str,
        name: str,
        interval_seconds: int,
        configuration: dict[str, Any] | None = None,
    ) -> str:
        """Create and schedule a recurring interval-based task.

        Creates a new ``TaskDefinition`` record for repeated execution at
        the specified interval, then schedules it with APScheduler using
        an ``IntervalTrigger``.

        Args:
            task_type: Task type identifier.
            name: Human-readable task name.
            interval_seconds: Number of seconds between executions.
            configuration: Optional task-specific configuration dict.

        Returns:
            The generated ``task_id``.
        """
        task_id = f"{task_type}_{uuid.uuid4().hex[:12]}"
        config = configuration or {}
        config["interval_seconds"] = interval_seconds

        task_def = TaskDefinition(
            task_id=task_id,
            type=task_type,
            name=name,
            cron_expression=None,
            enabled=True,
            configuration=config,
        )

        with self._app.app_context():
            db.session.add(task_def)
            db.session.commit()
            return self.schedule_task(task_def)

    def schedule_cron_task(
        self,
        task_type: str,
        name: str,
        cron_expression: str,
        configuration: dict[str, Any] | None = None,
    ) -> str:
        """Create and schedule a cron-based task.

        Creates a new ``TaskDefinition`` record with the given cron
        expression, then schedules it with APScheduler using a
        ``CronTrigger``.

        Args:
            task_type: Task type identifier.
            name: Human-readable task name.
            cron_expression: Standard 5-field cron expression
                (minute hour day_of_month month day_of_week).
            configuration: Optional task-specific configuration dict.

        Returns:
            The generated ``task_id``.
        """
        task_id = f"{task_type}_{uuid.uuid4().hex[:12]}"
        config = configuration or {}

        task_def = TaskDefinition(
            task_id=task_id,
            type=task_type,
            name=name,
            cron_expression=cron_expression,
            enabled=True,
            configuration=config,
        )

        with self._app.app_context():
            db.session.add(task_def)
            db.session.commit()
            return self.schedule_task(task_def)

    # ------------------------------------------------------------------
    # Trigger Building
    # ------------------------------------------------------------------

    def _build_trigger(self, task_def: TaskDefinition) -> Any:
        """Build the appropriate APScheduler trigger from a TaskDefinition.

        Resolution order:

        1. If ``cron_expression`` is set → ``CronTrigger.from_crontab()``.
        2. If ``configuration['interval_seconds']`` is set →
           ``IntervalTrigger``.
        3. If ``configuration['run_at']`` is set → ``DateTrigger``.
        4. Otherwise → ``ValueError``.

        **Note:** APScheduler's ``CronTrigger.from_crontab()`` accepts
        standard 5-field cron expressions:
        ``minute hour day_of_month month day_of_week``.

        Args:
            task_def: The ``TaskDefinition`` to build a trigger for.

        Returns:
            An APScheduler trigger instance.

        Raises:
            ValueError: If no valid schedule type can be determined.
        """
        # 1. Cron expression takes precedence
        if task_def.cron_expression:
            return CronTrigger.from_crontab(task_def.cron_expression)

        # 2. Check configuration for interval or one-time schedule
        config = task_def.configuration or {}

        if "interval_seconds" in config:
            interval = int(config["interval_seconds"])
            if interval <= 0:
                raise ValueError(
                    f"interval_seconds must be positive, got {interval}"
                )
            return IntervalTrigger(seconds=interval)

        if "run_at" in config:
            run_at = config["run_at"]
            if isinstance(run_at, str):
                run_at = datetime.fromisoformat(run_at)
            return DateTrigger(run_date=run_at)

        raise ValueError(
            f"Cannot determine schedule type for task '{task_def.task_id}': "
            f"no cron_expression, interval_seconds, or run_at configured"
        )

    # ------------------------------------------------------------------
    # Job Management Operations
    # ------------------------------------------------------------------

    def pause_task(self, task_id: str) -> bool:
        """Pause a scheduled task.

        Pauses the APScheduler job and sets ``enabled=False`` on the
        corresponding ``TaskDefinition`` record.

        Args:
            task_id: Unique task identifier.

        Returns:
            ``True`` if the task was found and paused, ``False`` otherwise.
        """
        if not self._scheduler:
            return False

        try:
            self._scheduler.pause_job(task_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to pause APScheduler job %s: %s", task_id, exc
            )

        with self._app.app_context():
            task_def = db.session.get(TaskDefinition, task_id)
            if task_def:
                task_def.enabled = False
                db.session.commit()
                self.logger.info("Task paused: %s", task_id)
                return True

        self.logger.warning("Task not found for pause: %s", task_id)
        return False

    def resume_task(self, task_id: str) -> bool:
        """Resume a paused task.

        Resumes the APScheduler job and sets ``enabled=True`` on the
        corresponding ``TaskDefinition`` record.

        Args:
            task_id: Unique task identifier.

        Returns:
            ``True`` if the task was found and resumed, ``False`` otherwise.
        """
        if not self._scheduler:
            return False

        try:
            self._scheduler.resume_job(task_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to resume APScheduler job %s: %s", task_id, exc
            )

        with self._app.app_context():
            task_def = db.session.get(TaskDefinition, task_id)
            if task_def:
                task_def.enabled = True
                db.session.commit()
                self.logger.info("Task resumed: %s", task_id)
                return True

        self.logger.warning("Task not found for resume: %s", task_id)
        return False

    def remove_task(self, task_id: str) -> bool:
        """Remove a task from the scheduler and delete from the database.

        Removes the APScheduler job (silently ignoring if not found) and
        then deletes the ``TaskDefinition`` record from the database.
        Cascade ``delete-orphan`` on the relationship ensures associated
        ``TaskExecution`` records are also removed.

        Args:
            task_id: Unique task identifier.

        Returns:
            ``True`` if the task was found and removed, ``False`` otherwise.
        """
        # Remove from APScheduler (may not exist if scheduler is stopped)
        if self._scheduler:
            try:
                self._scheduler.remove_job(task_id)
            except Exception:
                pass  # Job may not exist in scheduler

        with self._app.app_context():
            task_def = db.session.get(TaskDefinition, task_id)
            if task_def:
                db.session.delete(task_def)
                db.session.commit()
                self.logger.info("Task removed: %s", task_id)
                return True

        self.logger.warning("Task not found for removal: %s", task_id)
        return False

    def run_task_now(self, task_id: str) -> str:
        """Trigger immediate ad-hoc execution of a scheduled task.

        Creates a one-shot job with a ``DateTrigger`` set to the current
        UTC time.  The ad-hoc job gets a unique ID (``<task_id>_adhoc_<ts>``)
        so it doesn't interfere with the task's regular schedule.

        Args:
            task_id: Unique task identifier.

        Returns:
            The ad-hoc job ID string.

        Raises:
            ValueError: If the task is not found or its type is not
                registered.
            RuntimeError: If the scheduler is not initialised.
        """
        if not self._scheduler:
            raise RuntimeError(
                "TaskScheduler not initialised — call init_app() first"
            )
        if not self._registry:
            raise RuntimeError("TaskRegistry not initialised")

        with self._app.app_context():
            task_def = db.session.get(TaskDefinition, task_id)
            if not task_def:
                raise ValueError(f"Task '{task_id}' not found")

            task_func = self._registry.get_task_callable(task_def.type)
            if task_func is None:
                raise ValueError(
                    f"Unknown task type '{task_def.type}' for task "
                    f"'{task_id}'"
                )

            wrapped = self._wrap_task_execution(task_def, task_func)
            now = datetime.now(timezone.utc)
            adhoc_id = (
                f"{task_id}_adhoc_{now.timestamp():.0f}"
            )

            self._scheduler.add_job(
                func=wrapped,
                trigger=DateTrigger(run_date=now),
                id=adhoc_id,
                name=f"{task_def.name} (ad-hoc)",
            )

            self.logger.info(
                "Ad-hoc execution triggered: %s (job_id=%s)",
                task_def.name,
                adhoc_id,
            )

            return adhoc_id

    def get_task_status(self, task_id: str) -> dict[str, Any] | None:
        """Return the current status of a scheduled task.

        Combines information from the ``TaskDefinition`` database record
        and the APScheduler job (if present) into a unified status dict.

        Args:
            task_id: Unique task identifier.

        Returns:
            A dictionary with task status information, or ``None`` if the
            task is not found.
        """
        with self._app.app_context():
            task_def = db.session.get(TaskDefinition, task_id)
            if not task_def:
                return None

            # Get next_run_time from APScheduler if available
            next_run: str | None = None
            if self._scheduler:
                try:
                    job = self._scheduler.get_job(task_id)
                    if job and job.next_run_time:
                        next_run = job.next_run_time.isoformat()
                except Exception:
                    pass

            return {
                "task_id": task_def.task_id,
                "name": task_def.name,
                "type": task_def.type,
                "enabled": task_def.enabled,
                "cron_expression": task_def.cron_expression,
                "last_run_status": task_def.last_run_status,
                "next_run_time": next_run,
                "configuration": task_def.configuration,
            }

    def list_tasks(self) -> list[dict[str, Any]]:
        """List all scheduled tasks with their current status.

        Queries all ``TaskDefinition`` records from the database and
        enriches each with APScheduler job information (next run time).

        Returns:
            A list of task status dictionaries.
        """
        result: list[dict[str, Any]] = []

        with self._app.app_context():
            try:
                tasks = TaskDefinition.query.all()
            except Exception as exc:
                self.logger.error(
                    "Failed to query tasks: %s", exc, exc_info=True
                )
                return result

            for task_def in tasks:
                # Get next_run_time from APScheduler if available
                next_run: str | None = None
                if self._scheduler:
                    try:
                        job = self._scheduler.get_job(task_def.task_id)
                        if job and job.next_run_time:
                            next_run = job.next_run_time.isoformat()
                    except Exception:
                        pass

                result.append({
                    "task_id": task_def.task_id,
                    "name": task_def.name,
                    "type": task_def.type,
                    "enabled": task_def.enabled,
                    "cron_expression": task_def.cron_expression,
                    "last_run_status": task_def.last_run_status,
                    "next_run_time": next_run,
                    "configuration": task_def.configuration,
                })

        return result

    # ------------------------------------------------------------------
    # Task Execution Wrapping
    # ------------------------------------------------------------------

    def _wrap_task_execution(
        self, task_def: TaskDefinition, task_func: Callable
    ) -> Callable:
        """Create an execution-tracking wrapper around a task callable.

        The wrapper:

        1. Injects the Flask application context (required for database
           access in background threads).
        2. Creates a ``TaskExecution`` record via ``TaskLogger``.
        3. Emits ``EventType.TASK_STARTED`` before execution.
        4. Calls the actual task callable.
        5. Records success or failure via ``TaskLogger``.
        6. Emits ``EventType.TASK_COMPLETED`` with result status.
        7. Updates ``TaskDefinition.last_run_status``.

        **CRITICAL:** The wrapper always runs within
        ``with self._app.app_context()`` because APScheduler executes jobs
        in background threads that lack Flask context.

        Args:
            task_def: The ``TaskDefinition`` being executed.
            task_func: The actual task callable to invoke.

        Returns:
            A wrapped callable suitable for APScheduler ``add_job()``.
        """
        # Capture references for closure
        app = self._app
        task_logger_ref = self._task_logger
        scheduler_logger = self.logger

        @functools.wraps(task_func)
        def wrapper(**kwargs: Any) -> Any:
            with app.app_context():
                # Refresh the task_def within this session context
                current_task_def = db.session.get(
                    TaskDefinition, task_def.task_id
                )
                if current_task_def is None:
                    scheduler_logger.warning(
                        "Task definition %s no longer exists — skipping",
                        task_def.task_id,
                    )
                    return None

                execution = task_logger_ref.start_execution(current_task_def)

                try:
                    # Emit TASK_STARTED event for audit logging and webhooks
                    emit_event(EventType.TASK_STARTED, {
                        "task_id": current_task_def.task_id,
                        "task_type": current_task_def.type,
                        "execution_id": execution.execution_id,
                    })

                    # Execute the actual task callable
                    result = task_func(
                        task_def=current_task_def,
                        configuration=current_task_def.configuration or {},
                    )

                    # Record success via TaskLogger
                    task_logger_ref.complete_execution(
                        execution, STATUS_OK, result=result
                    )

                    # Update cached last_run_status on TaskDefinition
                    current_task_def.last_run_status = STATUS_OK
                    db.session.commit()

                    # Emit TASK_COMPLETED event (success)
                    emit_event(EventType.TASK_COMPLETED, {
                        "task_id": current_task_def.task_id,
                        "task_type": current_task_def.type,
                        "execution_id": execution.execution_id,
                        "status": STATUS_OK,
                        "duration_ms": execution.duration_ms,
                    })

                    return result

                except Exception as exc:
                    # Record failure via TaskLogger
                    task_logger_ref.fail_execution(execution, str(exc))

                    # Update cached last_run_status on TaskDefinition
                    try:
                        current_task_def.last_run_status = STATUS_FAILED
                        db.session.commit()
                    except Exception as commit_exc:
                        scheduler_logger.error(
                            "Failed to update last_run_status for task %s: %s",
                            current_task_def.task_id,
                            commit_exc,
                        )

                    # Emit TASK_COMPLETED event (failure)
                    emit_event(EventType.TASK_COMPLETED, {
                        "task_id": current_task_def.task_id,
                        "task_type": current_task_def.type,
                        "execution_id": execution.execution_id,
                        "status": STATUS_FAILED,
                        "error": str(exc),
                    })

                    scheduler_logger.error(
                        "Task %s (%s) failed: %s",
                        current_task_def.name,
                        current_task_def.task_id,
                        exc,
                        exc_info=True,
                    )

                    raise

        return wrapper

    # ------------------------------------------------------------------
    # APScheduler Event Listeners
    # ------------------------------------------------------------------

    def _on_job_executed(self, event: JobExecutionEvent) -> None:
        """Handle successful job execution events from APScheduler.

        Updates ``TaskDefinition.next_run_time`` from the APScheduler job
        to keep the database in sync with the scheduler's computed next
        run time.

        Args:
            event: The APScheduler ``JobExecutionEvent``.
        """
        if not self._app:
            return

        try:
            with self._app.app_context():
                job = self._scheduler.get_job(event.job_id)
                if job and job.next_run_time:
                    task_def = db.session.get(TaskDefinition, event.job_id)
                    if task_def:
                        task_def.next_run_time = job.next_run_time
                        db.session.commit()
                        self.logger.debug(
                            "Updated next_run_time for task %s: %s",
                            event.job_id,
                            job.next_run_time,
                        )
        except Exception as exc:
            self.logger.warning(
                "Failed to update next_run_time for task %s after "
                "execution: %s",
                event.job_id,
                exc,
            )

    def _on_job_error(self, event: JobExecutionEvent) -> None:
        """Handle job error events from APScheduler.

        Logs the error at ERROR level with the exception details.  The
        actual failure recording (TaskExecution status update, event
        emission) is handled by the ``_wrap_task_execution`` wrapper.

        Args:
            event: The APScheduler ``JobExecutionEvent``.
        """
        self.logger.error(
            "Task %s raised an exception: %s",
            event.job_id,
            event.exception,
            exc_info=True,
        )

    def _on_job_missed(self, event: Any) -> None:
        """Handle missed job events from APScheduler.

        A job is "missed" when its scheduled fire time passes beyond the
        ``misfire_grace_time`` window (default 60 seconds).  This typically
        occurs during system overload or scheduler downtime.

        Args:
            event: The APScheduler missed-job event.
        """
        self.logger.warning(
            "Task %s missed its scheduled run time", event.job_id
        )

    # ------------------------------------------------------------------
    # Cluster-Aware Execution (Multi-Node)
    # ------------------------------------------------------------------

    def configure_cluster_mode(self, database_url: str) -> None:
        """Enable cluster-aware execution using SQLAlchemy job store.

        When multiple Flask application instances share the same database,
        APScheduler's ``SQLAlchemyJobStore`` uses database row-level locking
        to ensure each job runs on only one node.  This replaces Quartz's
        clustered mode with JDBC job store.

        Args:
            database_url: SQLAlchemy database URL for the shared job store.

        Raises:
            RuntimeError: If the scheduler is not initialised.
        """
        if not self._scheduler:
            raise RuntimeError(
                "TaskScheduler not initialised — call init_app() first"
            )

        self._scheduler.add_jobstore(
            SQLAlchemyJobStore(url=database_url),
            alias="cluster",
        )
        self.logger.info(
            "Cluster-aware scheduling enabled with job store alias 'cluster'"
        )

    # ------------------------------------------------------------------
    # Properties — External Access to Sub-Components
    # ------------------------------------------------------------------

    @property
    def registry(self) -> TaskRegistry | None:
        """Return the ``TaskRegistry`` instance for external access.

        Allows other modules (e.g., API layer, plugin manager) to register
        custom task types or query available task types.
        """
        return self._registry

    @property
    def task_logger(self) -> TaskLogger | None:
        """Return the ``TaskLogger`` instance for external access.

        Allows other modules to query execution history or access per-task
        loggers directly.
        """
        return self._task_logger

    # ------------------------------------------------------------------
    # Query / Utility Methods
    # ------------------------------------------------------------------

    def get_scheduled_jobs(self) -> list[dict[str, Any]]:
        """Return information about all jobs in the APScheduler store.

        Provides a snapshot of all currently registered APScheduler jobs,
        including their next run time and trigger configuration.

        Returns:
            A list of dictionaries with job metadata.
        """
        if not self._scheduler:
            return []

        jobs = self._scheduler.get_jobs()
        return [
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": (
                    job.next_run_time.isoformat()
                    if job.next_run_time
                    else None
                ),
                "trigger": str(job.trigger),
                "pending": job.pending,
            }
            for job in jobs
        ]

    def get_execution_history(
        self,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query ``TaskExecution`` records from the database.

        Returns a list of execution history dictionaries, ordered by
        ``start_time`` descending (most recent first).

        Args:
            task_id: Optional task identifier to filter by.  If ``None``,
                returns execution history for all tasks.
            limit: Maximum number of records to return.  Default ``50``.

        Returns:
            A list of execution history dictionaries.
        """
        result: list[dict[str, Any]] = []

        with self._app.app_context():
            try:
                query = TaskExecution.query

                if task_id is not None:
                    query = query.filter_by(task_id=task_id)

                executions = (
                    query
                    .order_by(TaskExecution.start_time.desc())
                    .limit(limit)
                    .all()
                )

                for execution in executions:
                    result.append({
                        "execution_id": execution.execution_id,
                        "task_id": execution.task_id,
                        "start_time": (
                            execution.start_time.isoformat()
                            if execution.start_time
                            else None
                        ),
                        "end_time": (
                            execution.end_time.isoformat()
                            if execution.end_time
                            else None
                        ),
                        "status": execution.status,
                        "duration_ms": execution.duration_ms,
                        "progress": execution.progress,
                        "error_message": execution.error_message,
                    })

            except Exception as exc:
                self.logger.error(
                    "Failed to query execution history: %s",
                    exc,
                    exc_info=True,
                )

        return result


# ---------------------------------------------------------------------------
# Module Initialization Log
# ---------------------------------------------------------------------------
logger.debug(
    "Task scheduler module loaded — exports: %s",
    ", ".join(__all__),
)
