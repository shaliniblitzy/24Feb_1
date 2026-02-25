"""
Task scheduling SQLAlchemy models: TaskDefinition and TaskExecution.

This module defines two related SQLAlchemy models that together replace the
``TASK_DEFINITION`` and ``TASK_EXECUTION`` entities from the DataStore schema
(Section 6.2.1.2 of the Technical Specification).  They support **Feature
F-402 (Scheduled Tasks)**, replacing the Quartz 2.3.2 task scheduling data
model from the original Java source system.

**Architecture Context:**
In the Java Nexus Repository, Quartz 2.3.2 managed a job store backed by JDBC
tables (``QRTZ_JOB_DETAILS``, ``QRTZ_TRIGGERS``, ``QRTZ_FIRED_TRIGGERS``, …).
Here, these are replaced by two clean SQLAlchemy models integrated with
APScheduler 3.10.4:

+----------------------------+------------------------------------------+
| Java / Quartz Component    | Python / Flask Equivalent                |
+============================+==========================================+
| ``QRTZ_JOB_DETAILS``       | ``TaskDefinition`` (this file)           |
+----------------------------+------------------------------------------+
| ``QRTZ_TRIGGERS``          | ``TaskDefinition.cron_expression`` etc.  |
+----------------------------+------------------------------------------+
| ``QRTZ_FIRED_TRIGGERS``    | ``TaskExecution`` (this file)            |
+----------------------------+------------------------------------------+

**Relationship:**
``TaskDefinition`` has a one-to-many relationship to ``TaskExecution``.  Each
definition can have many execution records, tracking the history of every run.
Cascade ``all, delete-orphan`` ensures executions are removed when a task
definition is deleted.

**Dual-Database Support:**
Both models are designed to work on **SQLite** (standalone deployments) and
**PostgreSQL** (clustered / enterprise deployments) via portable SQLAlchemy
column types.  No database-specific types are used.

**Execution Status Values:**
The ``status`` column on ``TaskExecution`` uses the following well-defined
values, matching the original Quartz execution lifecycle:

- ``waiting``   — Execution is queued but has not started.
- ``running``   — Execution is currently in progress.
- ``ok``        — Execution completed successfully.
- ``failed``    — Execution terminated with an error.
- ``canceled``  — Execution was manually or programmatically canceled.

Exports:
    TaskDefinition  — Scheduled task configuration model.
    TaskExecution   — Individual task run record model.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from src.app.extensions import db
from src.app.models.base import BaseModel, JSONAttributesMixin, TimestampMixin

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "TaskDefinition",
    "TaskExecution",
]

# ---------------------------------------------------------------------------
# Execution Status Constants
# ---------------------------------------------------------------------------
# These constants define the valid execution lifecycle states for task runs.
# They mirror the Quartz 2.3.2 trigger states from the Java source and are
# used throughout the scheduler, API, and monitoring subsystems.
# ---------------------------------------------------------------------------

EXECUTION_STATUS_WAITING: str = "waiting"
"""Execution is queued but has not started yet."""

EXECUTION_STATUS_RUNNING: str = "running"
"""Execution is currently in progress."""

EXECUTION_STATUS_OK: str = "ok"
"""Execution completed successfully."""

EXECUTION_STATUS_FAILED: str = "failed"
"""Execution terminated with an error."""

EXECUTION_STATUS_CANCELED: str = "canceled"
"""Execution was manually or programmatically canceled."""

VALID_EXECUTION_STATUSES: frozenset[str] = frozenset({
    EXECUTION_STATUS_WAITING,
    EXECUTION_STATUS_RUNNING,
    EXECUTION_STATUS_OK,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_CANCELED,
})
"""Complete set of valid execution status values for validation."""

COMPLETED_STATUSES: frozenset[str] = frozenset({
    EXECUTION_STATUS_OK,
    EXECUTION_STATUS_FAILED,
    EXECUTION_STATUS_CANCELED,
})
"""Subset of statuses that represent a terminal (completed) execution."""


# ===========================================================================
# TaskDefinition Model
# ===========================================================================


class TaskDefinition(BaseModel, TimestampMixin, JSONAttributesMixin):
    """Scheduled task configuration model.

    Stores the definition and schedule of background tasks that the system
    executes automatically (e.g., repository cleanup, blob store compaction,
    database backup, index rebuilding).

    Replaces ``TASK_DEFINITION`` entity from the DataStore schema (Section
    6.2.1.2).  In the Java source, this was backed by Quartz 2.3.2 job
    detail and trigger tables.

    **Inheritance Chain:**

    - ``BaseModel`` — Provides ``to_dict()``, ``save()``, ``delete()``,
      ``update()`` convenience methods via the abstract base class.
    - ``TimestampMixin`` — Provides automatic ``created_at`` and
      ``updated_at`` UTC timestamp columns.
    - ``JSONAttributesMixin`` — Provides the extensible ``attributes`` JSON
      column with ``get_attribute()``, ``set_attribute()``,
      ``remove_attribute()``, and ``merge_attributes()`` helpers.

    **Table:**  ``task_definitions``

    **Indexes:**
    - ``ix_task_definitions_type`` — Accelerates queries filtering by task
      type (e.g., listing all cleanup tasks).
    - ``ix_task_definitions_enabled`` — Accelerates queries for active tasks.

    **Example Usage::

        task = TaskDefinition(
            task_id='cleanup-snapshots-daily',
            type='repository.cleanup',
            name='Daily Snapshot Cleanup',
            cron_expression='0 0 * * *',
            enabled=True,
            configuration={
                'repositoryName': '*',
                'policyNames': ['cleanup-snapshots'],
            },
        )
        task.save()

    Attributes:
        task_id: Unique task identifier string (primary key).
        type: Task type identifier (e.g., 'repository.cleanup').
        name: Human-readable display name.
        cron_expression: Cron schedule expression, or ``None`` for one-time.
        enabled: Whether the task schedule is active.
        configuration: JSON dict of task-specific parameters.
        attributes: Extensible JSON metadata (from ``JSONAttributesMixin``).
        last_run_status: Cached status string of the most recent execution.
        next_run_time: Computed datetime of the next scheduled execution.
        executions: Dynamic relationship to ``TaskExecution`` records.
        created_at: UTC timestamp of record creation (from ``TimestampMixin``).
        updated_at: UTC timestamp of last modification (from ``TimestampMixin``).
    """

    __tablename__: str = "task_definitions"

    # -- Primary Key ---------------------------------------------------------

    task_id: str = Column(
        String(200),
        primary_key=True,
        doc="Unique task identifier (e.g., 'cleanup-snapshots-daily').",
    )

    # -- Task Configuration Columns ------------------------------------------

    type: str = Column(
        String(100),
        nullable=False,
        doc=(
            "Task type identifier used to look up the execution handler. "
            "Examples: 'repository.cleanup', 'blobstore.compact', "
            "'db.backup', 'repository.rebuild-index'."
        ),
    )

    name: str = Column(
        String(255),
        nullable=False,
        doc="Human-readable task name displayed in the administration UI.",
    )

    cron_expression: Optional[str] = Column(
        String(100),
        nullable=True,
        doc=(
            "Cron schedule expression compatible with APScheduler 3.10.4. "
            "Set to None for one-time tasks. "
            "Examples: '0 0 * * *' (daily at midnight), "
            "'0 */6 * * *' (every 6 hours)."
        ),
    )

    enabled: bool = Column(
        Boolean,
        nullable=False,
        default=True,
        doc="Whether this task schedule is active and should be executed.",
    )

    configuration: Optional[dict] = Column(
        JSON,
        nullable=True,
        doc=(
            "Task-specific configuration parameters stored as JSON. "
            "Structure varies by task type. "
            "Example for cleanup: "
            "{'repositoryName': '*', 'policyNames': ['cleanup-snapshots']}. "
            "Example for compact: {'blobStoreName': 'default'}."
        ),
    )

    # -- Cached Status Columns -----------------------------------------------
    # These columns provide fast access to scheduling metadata without
    # requiring a join to the task_executions table.

    last_run_status: Optional[str] = Column(
        String(50),
        nullable=True,
        doc=(
            "Cached status of the most recent execution. Updated after each "
            "execution completes. Valid values: "
            "'waiting', 'running', 'ok', 'failed', 'canceled'."
        ),
    )

    next_run_time: Optional[datetime] = Column(
        DateTime,
        nullable=True,
        doc=(
            "Computed next execution time for cron-scheduled tasks. "
            "Null for one-time tasks or disabled tasks."
        ),
    )

    # -- Relationships -------------------------------------------------------

    executions = db.relationship(
        "TaskExecution",
        back_populates="task_definition",
        cascade="all, delete-orphan",
        lazy="dynamic",
        doc=(
            "Dynamic collection of all execution records for this task. "
            "Supports further filtering and ordering as a query object."
        ),
    )

    # -- Table-Level Configuration -------------------------------------------

    __table_args__ = (
        db.Index("ix_task_definitions_type", "type"),
        db.Index("ix_task_definitions_enabled", "enabled"),
    )

    # -- Properties ----------------------------------------------------------

    @property
    def is_scheduled(self) -> bool:
        """Return ``True`` if this task has a cron schedule.

        One-time tasks have ``cron_expression`` set to ``None`` and are
        triggered manually or programmatically rather than on a recurring
        schedule.

        Returns:
            ``True`` if a cron expression is defined, ``False`` otherwise.
        """
        return self.cron_expression is not None

    @property
    def last_execution(self) -> Optional[TaskExecution]:
        """Return the most recent ``TaskExecution`` for this task.

        Queries the ``executions`` dynamic relationship ordered by
        ``start_time`` descending and returns the first result.

        Returns:
            The most recent ``TaskExecution`` instance, or ``None`` if this
            task has never been executed.
        """
        return (
            self.executions
            .order_by(TaskExecution.start_time.desc())
            .first()
        )

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this task definition to a plain dictionary.

        Extends ``BaseModel.to_dict()`` with computed properties:

        - ``is_scheduled``: Whether the task is cron-scheduled.

        The ``executions`` relationship is **not** included to avoid
        potentially expensive lazy-loading of large execution histories.
        Use the ``/api/tasks/{task_id}/executions`` endpoint to retrieve
        execution records.

        Returns:
            Dictionary representation suitable for JSON serialization.
        """
        result: dict[str, Any] = super().to_dict()
        result["is_scheduled"] = self.is_scheduled
        return result

    # -- Representation ------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Format: ``<TaskDefinition {task_id}: {name}>``
        """
        return f"<TaskDefinition {self.task_id}: {self.name}>"


# ===========================================================================
# TaskExecution Model
# ===========================================================================


class TaskExecution(BaseModel, TimestampMixin):
    """Individual task execution record model.

    Tracks a single run of a ``TaskDefinition``, recording start time,
    end time, status, duration, progress messages, and any error details.

    Replaces ``TASK_EXECUTION`` entity from the DataStore schema (Section
    6.2.1.2).  In the Java source, this was equivalent to Quartz 2.3.2
    ``QRTZ_FIRED_TRIGGERS`` combined with custom execution history tables.

    **Note:** This model inherits from ``BaseModel`` and ``TimestampMixin``
    only — it does **not** include ``JSONAttributesMixin`` because execution
    records are immutable audit trails that do not need extensible attributes.

    **Inheritance Chain:**

    - ``BaseModel`` — Provides ``to_dict()``, ``save()``, ``delete()``,
      ``update()`` convenience methods.
    - ``TimestampMixin`` — Provides ``created_at`` and ``updated_at``.

    **Table:**  ``task_executions``

    **Indexes:**
    - ``ix_task_executions_task_id_start`` — Composite index accelerating
      queries for execution history of a specific task ordered by time.
    - ``ix_task_executions_status`` — Accelerates queries filtering by
      execution status (e.g., finding all currently running tasks).

    **Example Usage::

        execution = TaskExecution(
            task_id='cleanup-snapshots-daily',
            start_time=datetime.now(timezone.utc),
            status='running',
        )
        execution.save()

        # ... task runs ...

        execution.complete(status='ok')

    Attributes:
        execution_id: Auto-incrementing integer primary key.
        task_id: Foreign key referencing the parent ``TaskDefinition``.
        start_time: UTC timestamp when execution began.
        end_time: UTC timestamp when execution completed (null while running).
        status: Current execution status string.
        duration_ms: Execution duration in milliseconds (computed on complete).
        progress: Human-readable progress message (updated during execution).
        error_message: Detailed error information if status is ``'failed'``.
        task_definition: Many-to-one relationship back to ``TaskDefinition``.
        created_at: UTC timestamp of record creation (from ``TimestampMixin``).
        updated_at: UTC timestamp of last modification (from ``TimestampMixin``).
    """

    __tablename__: str = "task_executions"

    # -- Primary Key ---------------------------------------------------------

    execution_id: int = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        doc="Auto-incrementing execution record identifier.",
    )

    # -- Foreign Key ---------------------------------------------------------

    task_id: str = Column(
        String(200),
        ForeignKey("task_definitions.task_id"),
        nullable=False,
        index=True,
        doc="Foreign key reference to the parent TaskDefinition.",
    )

    # -- Execution Timing Columns --------------------------------------------

    start_time: datetime = Column(
        DateTime,
        nullable=False,
        doc="UTC timestamp when the task execution began.",
    )

    end_time: Optional[datetime] = Column(
        DateTime,
        nullable=True,
        doc=(
            "UTC timestamp when the task execution completed. "
            "Null while the task is still running."
        ),
    )

    # -- Status and Progress Columns -----------------------------------------

    status: str = Column(
        String(50),
        nullable=False,
        default=EXECUTION_STATUS_WAITING,
        doc=(
            "Current execution lifecycle status. Valid values: "
            "'waiting', 'running', 'ok', 'failed', 'canceled'."
        ),
    )

    duration_ms: Optional[int] = Column(
        BigInteger,
        nullable=True,
        doc=(
            "Execution duration in milliseconds, computed from start_time "
            "and end_time when the execution completes. Uses BigInteger to "
            "accommodate very long-running tasks (e.g., large repository "
            "reindexing operations)."
        ),
    )

    progress: Optional[str] = Column(
        String(255),
        nullable=True,
        doc=(
            "Human-readable progress message updated during execution. "
            "Examples: '50/100 components processed', 'Phase 2 of 3'."
        ),
    )

    error_message: Optional[str] = Column(
        Text,
        nullable=True,
        doc=(
            "Detailed error information populated when status is 'failed'. "
            "May contain stack traces or diagnostic messages."
        ),
    )

    # -- Relationships -------------------------------------------------------

    task_definition = db.relationship(
        "TaskDefinition",
        back_populates="executions",
        doc="Many-to-one back-reference to the parent TaskDefinition.",
    )

    # -- Table-Level Configuration -------------------------------------------

    __table_args__ = (
        db.Index(
            "ix_task_executions_task_id_start",
            "task_id",
            "start_time",
        ),
        db.Index("ix_task_executions_status", "status"),
    )

    # -- Properties ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Return ``True`` if this execution is currently in progress.

        Returns:
            ``True`` if status is ``'running'``, ``False`` otherwise.
        """
        return self.status == EXECUTION_STATUS_RUNNING

    @property
    def is_complete(self) -> bool:
        """Return ``True`` if this execution has reached a terminal state.

        Terminal states are ``'ok'``, ``'failed'``, and ``'canceled'``.  A
        complete execution has a populated ``end_time`` and ``duration_ms``.

        Returns:
            ``True`` if status is in the completed statuses set.
        """
        return self.status in COMPLETED_STATUSES

    # -- Instance Methods ----------------------------------------------------

    def complete(
        self,
        status: str,
        error_message: Optional[str] = None,
    ) -> None:
        """Mark this execution as complete with the given terminal status.

        Sets ``end_time`` to the current UTC time, updates ``status``,
        computes ``duration_ms`` from the delta between ``start_time`` and
        ``end_time``, and optionally records an error message.

        This method commits the changes to the database immediately via
        ``db.session``.

        Args:
            status: The terminal status to set.  Must be one of ``'ok'``,
                ``'failed'``, or ``'canceled'``.
            error_message: Optional error detail string.  Typically provided
                when ``status`` is ``'failed'``.

        Raises:
            ValueError: If ``status`` is not a valid completion status.

        Example::

            execution.complete(status='ok')
            execution.complete(status='failed', error_message='Disk full')
        """
        if status not in COMPLETED_STATUSES:
            raise ValueError(
                f"Invalid completion status '{status}'. "
                f"Must be one of: {sorted(COMPLETED_STATUSES)}."
            )

        self.end_time = datetime.now(timezone.utc)
        self.status = status

        # Compute duration in milliseconds from start/end timestamps.
        # Handle timezone-naive vs timezone-aware mismatch that can occur
        # when SQLite strips timezone info on round-trip.  We normalize both
        # timestamps to naive UTC before computing the delta.
        if self.start_time is not None and self.end_time is not None:
            end = self.end_time
            start = self.start_time
            # Strip tzinfo for safe subtraction — both are assumed UTC.
            if end.tzinfo is not None:
                end = end.replace(tzinfo=None)
            if start.tzinfo is not None:
                start = start.replace(tzinfo=None)
            delta = end - start
            self.duration_ms = int(delta.total_seconds() * 1000)

        if error_message is not None:
            self.error_message = error_message

        # Persist immediately — the execution record is an audit trail and
        # should be durably recorded as soon as the task completes.
        db.session.add(self)
        db.session.commit()

        logger.info(
            "Task execution %s completed: status=%s, duration_ms=%s",
            self.execution_id,
            self.status,
            self.duration_ms,
        )

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this execution record to a plain dictionary.

        Extends ``BaseModel.to_dict()`` with computed properties:

        - ``is_running``: Whether the execution is currently in progress.
        - ``is_complete``: Whether the execution has reached a terminal state.

        Returns:
            Dictionary representation suitable for JSON serialization.
        """
        result: dict[str, Any] = super().to_dict()
        result["is_running"] = self.is_running
        result["is_complete"] = self.is_complete
        return result

    # -- Representation ------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Format: ``<TaskExecution {execution_id}: {status}>``
        """
        return f"<TaskExecution {self.execution_id}: {self.status}>"


# ---------------------------------------------------------------------------
# Module Initialization Log
# ---------------------------------------------------------------------------
logger.debug(
    "Task models module loaded — exports: %s",
    ", ".join(__all__),
)
