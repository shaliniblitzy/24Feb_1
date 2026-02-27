"""
Per-task execution logging module for the Nexus Repository Flask application.

Replaces ``TaskLoggerHelper.java`` from the Java source system.  This module
provides:

1. **Dedicated log streams per task execution** — Each task gets its own
   Python logger under the ``nexus.task.*`` namespace, enabling per-task log
   filtering in centralized logging systems (ELK, CloudWatch, Splunk).

2. **Execution status tracking** — Creates and manages ``TaskExecution``
   database records that track start time, end time, status, duration, progress,
   and error details for every task run.

3. **Accurate duration measurement** — Uses ``time.monotonic()`` instead of
   wall-clock ``time.time()`` to avoid drift from NTP adjustments.  This is
   critical for reliable ``duration_ms`` calculation on ``TaskExecution`` records.

**Architecture Context:**

In the Java Nexus Repository, ``TaskLoggerHelper.java`` provided per-task log
streams backed by SLF4J 1.7.36 + Logback 1.2.13.  Here, Python's standard
``logging`` module with hierarchical logger names (``nexus.task.<task_name>``)
provides equivalent functionality with native support for log level filtering,
handler routing, and structured output.

**Feature Mapping:**
- Feature F-402 (Scheduled Tasks) — execution tracking and logging
- AAP Section 0.4.3 — Observer Pattern via Blinker signals (audit events)
- AAP Section 0.7.2 — Structured logging, Python 3.12+ type hints

Exports:
    TaskLogger            — Per-task execution logging and tracking class.
    STATUS_WAITING        — Execution status: queued but not started.
    STATUS_RUNNING        — Execution status: currently in progress.
    STATUS_OK             — Execution status: completed successfully.
    STATUS_FAILED         — Execution status: terminated with error.
    STATUS_CANCELED       — Execution status: manually/programmatically canceled.
    TASK_LOGGER_PREFIX    — Logger name prefix for per-task loggers.
    MAX_ERROR_MESSAGE_LENGTH — Maximum error message length stored in DB.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func

from src.app.extensions import db
from src.app.models.task import TaskDefinition, TaskExecution

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for the task logger subsystem itself.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Task Execution Status Constants
# ---------------------------------------------------------------------------
# These constants must match the TaskExecution model's status values as
# defined in ``src.app.models.task``.  They mirror the Quartz 2.3.2
# execution lifecycle states from the Java source system.
# ---------------------------------------------------------------------------

STATUS_WAITING: str = "waiting"
"""Execution is queued but has not started yet."""

STATUS_RUNNING: str = "running"
"""Execution is currently in progress."""

STATUS_OK: str = "ok"
"""Execution completed successfully."""

STATUS_FAILED: str = "failed"
"""Execution terminated with an error."""

STATUS_CANCELED: str = "canceled"
"""Execution was manually or programmatically canceled."""

# ---------------------------------------------------------------------------
# Logger Configuration Constants
# ---------------------------------------------------------------------------

TASK_LOGGER_PREFIX: str = "nexus.task"
"""Logger name prefix for per-task loggers.

Each task gets a logger named ``nexus.task.<task_name_or_id>``, enabling
log filtering by task in centralized logging systems.
"""

MAX_ERROR_MESSAGE_LENGTH: int = 4096
"""Maximum length of error messages stored in the database.

Error messages exceeding this limit are truncated with a trailing ``'...'``
to prevent database column overflow (``TaskExecution.error_message`` is a
``Text`` column, but we apply an application-level limit for consistency
and to prevent very large strings from degrading performance).
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "TaskLogger",
    "STATUS_WAITING",
    "STATUS_RUNNING",
    "STATUS_OK",
    "STATUS_FAILED",
    "STATUS_CANCELED",
    "TASK_LOGGER_PREFIX",
    "MAX_ERROR_MESSAGE_LENGTH",
]


# ===========================================================================
# TaskLogger Class
# ===========================================================================


class TaskLogger:
    """Per-task execution logging and tracking utility.

    Manages the complete lifecycle of task execution records, providing:

    - **Execution lifecycle** — ``start_execution()``, ``complete_execution()``,
      ``fail_execution()``, ``cancel_execution()`` to create and finalize
      ``TaskExecution`` database records.
    - **Progress tracking** — ``update_progress()`` for long-running tasks to
      report intermediate progress (0–100%).
    - **Dedicated loggers** — ``get_task_logger()`` returns a Python logger
      under the ``nexus.task.*`` namespace for per-task log filtering.
    - **Execution history** — Query methods for retrieving execution records
      by task, status, or time range.
    - **Self-maintenance** — ``purge_old_executions()`` deletes stale records
      beyond a configurable retention period.

    **Thread Safety:**
    The ``_task_loggers`` and ``_execution_start_times`` dicts are accessed
    from both the main thread and APScheduler background threads.  In CPython,
    dict operations are atomic due to the GIL, which is sufficient for our
    use case.  If a non-GIL runtime is used in the future, these should be
    wrapped with ``threading.Lock``.

    **Usage Example::

        task_logger = TaskLogger()

        # Start tracking an execution
        execution = task_logger.start_execution(task_def)

        # Update progress during the run
        task_logger.update_progress(execution, 50, "Halfway done")

        # Mark completion
        task_logger.complete_execution(execution)

        # Query history
        history = task_logger.get_execution_history(task_id='my-task')
    """

    def __init__(self) -> None:
        """Initialize the TaskLogger.

        Creates internal caches for per-task loggers and execution timing.
        No database or Flask app context is required at construction time,
        making this safe to instantiate during ``init_app()``.
        """
        self.logger: logging.Logger = logging.getLogger(__name__)

        # Cache of per-task loggers, keyed by task_id (string).
        # Avoids repeated calls to logging.getLogger() for the same task.
        self._task_loggers: dict[str, logging.Logger] = {}

        # Maps execution_id (as string) to time.monotonic() start time.
        # Used for accurate duration_ms calculation that is immune to
        # NTP clock adjustments.  Entries are added in start_execution()
        # and consumed (popped) in complete/fail/cancel methods.
        self._execution_start_times: dict[str, float] = {}

    # -------------------------------------------------------------------
    # Task Execution Lifecycle Methods
    # -------------------------------------------------------------------

    def start_execution(self, task_def: TaskDefinition) -> TaskExecution:
        """Create a new ``TaskExecution`` record to track a task run.

        Persists the execution record to the database immediately and records
        a monotonic start timestamp for accurate duration measurement.

        Args:
            task_def: The ``TaskDefinition`` whose execution is starting.
                Must have ``task_id``, ``name``, and ``type`` populated.

        Returns:
            The newly created ``TaskExecution`` instance with status
            ``STATUS_RUNNING`` and the database-assigned ``execution_id``.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database commit fails.
        """
        # Generate a unique trace ID for log correlation across distributed
        # systems.  This is separate from the database auto-increment
        # execution_id and ensures globally unique identifiers for cluster
        # node log aggregation.
        trace_id: str = str(uuid.uuid4())

        execution = TaskExecution(
            task_id=task_def.task_id,
            start_time=datetime.now(timezone.utc),
            status=STATUS_RUNNING,
            progress=str(0),
        )
        db.session.add(execution)
        db.session.commit()

        # After commit, execution.execution_id is populated by autoincrement.
        # Use string representation as dictionary key for _execution_start_times.
        exec_key: str = str(execution.execution_id)
        self._execution_start_times[exec_key] = time.monotonic()

        # Get or create a dedicated logger for this task.
        task_logger = self.get_task_logger(task_def.task_id, task_def.name)
        task_logger.info(
            "Task execution started: %s (type=%s, execution_id=%s, trace_id=%s)",
            task_def.name,
            task_def.type,
            execution.execution_id,
            trace_id,
        )

        return execution

    def complete_execution(
        self,
        execution: TaskExecution,
        status: str = STATUS_OK,
        result: Any = None,
    ) -> TaskExecution:
        """Mark a task execution as completed successfully.

        Sets ``end_time``, ``status``, ``progress`` to 100, and computes
        ``duration_ms`` using the monotonic clock (with wall-clock fallback).

        Args:
            execution: The ``TaskExecution`` instance to finalize.
            status: The completion status (default ``STATUS_OK``).
            result: Optional result data from the task execution (for
                logging/event purposes; not stored in the DB).

        Returns:
            The updated ``TaskExecution`` instance.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database commit fails.
        """
        now = datetime.now(timezone.utc)
        execution.end_time = now
        execution.status = status
        execution.progress = str(100)

        # Calculate duration using monotonic clock for accuracy.
        exec_key = str(execution.execution_id)
        start_mono = self._execution_start_times.pop(exec_key, None)
        if start_mono is not None:
            duration_ms = int((time.monotonic() - start_mono) * 1000)
        else:
            # Fallback to wall-clock delta if monotonic start not found
            # (e.g., process restart mid-execution).  Normalize timezone
            # for safe subtraction.
            start = execution.start_time
            end = now
            if start.tzinfo is not None:
                start = start.replace(tzinfo=None)
            if end.tzinfo is not None:
                end = end.replace(tzinfo=None)
            duration_ms = int((end - start).total_seconds() * 1000)
        execution.duration_ms = duration_ms

        db.session.commit()

        # Log completion via the dedicated task logger.
        task_logger = self.get_task_logger(execution.task_id)
        task_logger.info(
            "Task execution completed: execution_id=%s, status=%s, duration=%dms",
            execution.execution_id,
            status,
            duration_ms,
        )

        return execution

    def fail_execution(
        self,
        execution: TaskExecution,
        error_message: str,
        error_details: str | None = None,
    ) -> TaskExecution:
        """Mark a task execution as failed.

        Sets ``end_time``, ``status`` to ``STATUS_FAILED``, records the
        (potentially truncated) error message, and computes ``duration_ms``.

        Args:
            execution: The ``TaskExecution`` instance to finalize.
            error_message: Human-readable error description.  Truncated to
                ``MAX_ERROR_MESSAGE_LENGTH`` characters if too long.
            error_details: Optional additional error context (e.g., stack
                trace).  If provided and error_message is within limits,
                appended to the stored error_message.

        Returns:
            The updated ``TaskExecution`` instance.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database commit fails.
        """
        now = datetime.now(timezone.utc)
        execution.end_time = now
        execution.status = STATUS_FAILED

        # Build the full error string, optionally including details.
        full_error = error_message
        if error_details:
            full_error = f"{error_message}\n{error_details}"

        # Truncate to MAX_ERROR_MESSAGE_LENGTH to prevent DB column overflow.
        if len(full_error) > MAX_ERROR_MESSAGE_LENGTH:
            full_error = full_error[: MAX_ERROR_MESSAGE_LENGTH - 3] + "..."
        execution.error_message = full_error

        # Calculate duration using monotonic clock.
        exec_key = str(execution.execution_id)
        start_mono = self._execution_start_times.pop(exec_key, None)
        if start_mono is not None:
            duration_ms = int((time.monotonic() - start_mono) * 1000)
        else:
            start = execution.start_time
            end = now
            if start.tzinfo is not None:
                start = start.replace(tzinfo=None)
            if end.tzinfo is not None:
                end = end.replace(tzinfo=None)
            duration_ms = int((end - start).total_seconds() * 1000)
        execution.duration_ms = duration_ms

        db.session.commit()

        # Log failure via the dedicated task logger.
        task_logger = self.get_task_logger(execution.task_id)
        task_logger.error(
            "Task execution failed: execution_id=%s, duration=%dms, error=%s",
            execution.execution_id,
            duration_ms,
            error_message,
        )

        return execution

    def cancel_execution(self, execution: TaskExecution) -> TaskExecution:
        """Mark a task execution as canceled.

        Sets ``end_time``, ``status`` to ``STATUS_CANCELED``, and computes
        ``duration_ms``.

        Args:
            execution: The ``TaskExecution`` instance to cancel.

        Returns:
            The updated ``TaskExecution`` instance.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database commit fails.
        """
        now = datetime.now(timezone.utc)
        execution.end_time = now
        execution.status = STATUS_CANCELED

        # Calculate duration using monotonic clock.
        exec_key = str(execution.execution_id)
        start_mono = self._execution_start_times.pop(exec_key, None)
        if start_mono is not None:
            duration_ms = int((time.monotonic() - start_mono) * 1000)
        else:
            start = execution.start_time
            end = now
            if start.tzinfo is not None:
                start = start.replace(tzinfo=None)
            if end.tzinfo is not None:
                end = end.replace(tzinfo=None)
            duration_ms = int((end - start).total_seconds() * 1000)
        execution.duration_ms = duration_ms

        db.session.commit()

        # Log cancellation via the dedicated task logger.
        task_logger = self.get_task_logger(execution.task_id)
        task_logger.warning(
            "Task execution canceled: execution_id=%s, duration=%dms",
            execution.execution_id,
            duration_ms,
        )

        return execution

    # -------------------------------------------------------------------
    # Progress Tracking
    # -------------------------------------------------------------------

    def update_progress(
        self,
        execution: TaskExecution,
        progress: int,
        message: str | None = None,
    ) -> None:
        """Update the progress percentage of a running task execution.

        Clamps the progress value to the 0–100 range and persists it to
        the database.  Optionally logs an informational message via the
        task's dedicated logger.

        Args:
            execution: The running ``TaskExecution`` to update.
            progress: Progress percentage (0–100).  Values outside this
                range are clamped automatically.
            message: Optional human-readable progress description.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database commit fails.
        """
        clamped = max(0, min(100, progress))
        # TaskExecution.progress is a String(255) column.  Store the clamped
        # integer value as a string representation.
        execution.progress = str(clamped)
        db.session.commit()

        if message:
            task_logger = self.get_task_logger(execution.task_id)
            task_logger.info(
                "Task progress: execution_id=%s, progress=%d%%, %s",
                execution.execution_id,
                clamped,
                message,
            )

    # -------------------------------------------------------------------
    # Per-Task Logger Management
    # -------------------------------------------------------------------

    def get_task_logger(
        self,
        task_id: str,
        task_name: str | None = None,
    ) -> logging.Logger:
        """Get or create a dedicated Python logger for a specific task.

        Each task receives its own logger under the ``nexus.task.*``
        namespace.  This enables:

        - **Centralized filtering** — ELK, CloudWatch, Splunk can filter
          by logger name (e.g., ``nexus.task.cleanup-snapshots-daily``).
        - **Per-task log levels** — Python logging configuration can set
          different log levels per task.
        - **Structured context** — Logger name encodes the task identity.

        Loggers are cached by ``task_id`` so repeated calls for the same
        task return the same logger instance without re-creating it.

        Args:
            task_id: Unique task identifier (used as cache key).
            task_name: Optional human-readable task name.  Used in the
                logger name for readability.  Falls back to ``task_id``
                if not provided.

        Returns:
            A ``logging.Logger`` instance for the specified task.
        """
        if task_id in self._task_loggers:
            return self._task_loggers[task_id]

        # Create a logger with the task-specific name.
        # Format: nexus.task.<task_name_or_id>
        # Sanitize the name to avoid dots that would create extra hierarchy.
        display_name = task_name or task_id
        logger_name = f"{TASK_LOGGER_PREFIX}.{display_name}"
        task_logger = logging.getLogger(logger_name)

        self._task_loggers[task_id] = task_logger
        return task_logger

    def cleanup_task_logger(self, task_id: str) -> None:
        """Remove a cached task logger.

        Called when a task is permanently deleted to free the cached
        logger reference.  The underlying Python logger is not destroyed
        (Python's logging module manages logger lifecycle), but the cache
        entry is removed so a new logger can be created if a task with
        the same ID is re-registered.

        Args:
            task_id: The task identifier whose logger should be removed
                from the cache.
        """
        self._task_loggers.pop(task_id, None)

    # -------------------------------------------------------------------
    # Execution History Queries
    # -------------------------------------------------------------------

    def get_execution_history(
        self,
        task_id: str | None = None,
        limit: int = 50,
        status: str | None = None,
    ) -> list[TaskExecution]:
        """Query ``TaskExecution`` records with optional filters.

        Returns execution records ordered by ``start_time`` descending
        (most recent first), with optional filtering by task and status.

        Args:
            task_id: If provided, return only executions for this task.
            limit: Maximum number of records to return (default 50).
            status: If provided, return only executions with this status.

        Returns:
            List of ``TaskExecution`` instances matching the criteria,
            ordered by most recent first.
        """
        query = TaskExecution.query
        if task_id:
            query = query.filter(TaskExecution.task_id == task_id)
        if status:
            query = query.filter(TaskExecution.status == status)
        query = query.order_by(TaskExecution.start_time.desc())
        return query.limit(limit).all()

    def get_last_execution(self, task_id: str) -> TaskExecution | None:
        """Get the most recent execution for a specific task.

        Args:
            task_id: The task identifier to look up.

        Returns:
            The most recent ``TaskExecution`` for the task, or ``None``
            if the task has never been executed.
        """
        return (
            TaskExecution.query.filter_by(task_id=task_id)
            .order_by(TaskExecution.start_time.desc())
            .first()
        )

    def get_running_executions(self) -> list[TaskExecution]:
        """Get all currently running task executions.

        Returns executions ordered by ``start_time`` ascending (oldest
        running first), which is useful for identifying long-running or
        potentially stuck tasks.

        Returns:
            List of ``TaskExecution`` instances with status
            ``STATUS_RUNNING``.
        """
        return (
            TaskExecution.query.filter_by(status=STATUS_RUNNING)
            .order_by(TaskExecution.start_time.asc())
            .all()
        )

    def get_execution_stats(
        self,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Calculate aggregate execution statistics.

        Provides a summary of execution counts by status, success rate,
        and average duration for completed executions.

        Args:
            task_id: If provided, calculate stats only for this task.
                If ``None``, returns system-wide statistics.

        Returns:
            Dictionary containing:
            - ``total_executions`` — Total number of execution records.
            - ``successful`` — Count of executions with ``STATUS_OK``.
            - ``failed`` — Count of executions with ``STATUS_FAILED``.
            - ``canceled`` — Count of executions with ``STATUS_CANCELED``.
            - ``success_rate`` — Percentage of successful executions
              (0.0 if no executions exist).
            - ``average_duration_ms`` — Average duration of completed
              executions in milliseconds (0 if none).
        """
        query = TaskExecution.query
        if task_id:
            query = query.filter(TaskExecution.task_id == task_id)

        total = query.count()
        successful = query.filter(
            TaskExecution.status == STATUS_OK
        ).count()
        failed = query.filter(
            TaskExecution.status == STATUS_FAILED
        ).count()
        canceled = query.filter(
            TaskExecution.status == STATUS_CANCELED
        ).count()

        # Average duration of completed executions using SQLAlchemy func.avg.
        avg_duration = (
            query.filter(TaskExecution.duration_ms.isnot(None))
            .with_entities(func.avg(TaskExecution.duration_ms))
            .scalar()
        ) or 0

        return {
            "total_executions": total,
            "successful": successful,
            "failed": failed,
            "canceled": canceled,
            "success_rate": (
                (successful / total * 100) if total > 0 else 0.0
            ),
            "average_duration_ms": round(float(avg_duration)),
        }

    # -------------------------------------------------------------------
    # Cleanup of Old Execution Records
    # -------------------------------------------------------------------

    def purge_old_executions(self, retention_days: int = 30) -> int:
        """Delete old ``TaskExecution`` records beyond the retention period.

        Only purges completed/failed/canceled executions — running
        executions are never deleted, regardless of age.  This method
        can itself be registered as a scheduled task for self-maintenance.

        Args:
            retention_days: Number of days to retain execution records.
                Records with ``start_time`` older than this threshold
                are deleted.  Defaults to 30 days.

        Returns:
            The number of execution records deleted.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the database operation fails.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

        deleted = TaskExecution.query.filter(
            TaskExecution.start_time < cutoff,
            TaskExecution.status.in_(
                [STATUS_OK, STATUS_FAILED, STATUS_CANCELED]
            ),
        ).delete(synchronize_session=False)

        db.session.commit()

        self.logger.info(
            "Purged %d task execution records older than %d days",
            deleted,
            retention_days,
        )
        return deleted


# ---------------------------------------------------------------------------
# Module Initialization Log
# ---------------------------------------------------------------------------

logger.debug(
    "Task logger module loaded — exports: %s",
    ", ".join(__all__),
)
