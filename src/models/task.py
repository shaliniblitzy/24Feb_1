"""
Scheduled task model for the Binary Repository Management System.

Defines the ``Task`` entity representing scheduled and ad-hoc background
tasks such as repository cleanup, maintenance, backup, health checks, and
content indexing.

The model tracks the full task lifecycle through status transitions:

    WAITING → RUNNING → COMPLETED
                      → FAILED
    WAITING → CANCELLED

Usage::

    from src.models.task import Task

    task = Task(
        name="cleanup-snapshots",
        type="repository.cleanup",
        schedule="0 2 * * *",
        enabled=True,
    )
"""

import datetime
import json
import uuid
from typing import Any, Dict, Optional

from sqlalchemy.orm import validates

from src.extensions import db


# ---------------------------------------------------------------------------
# Valid status values for the task lifecycle
# ---------------------------------------------------------------------------
VALID_STATUSES = frozenset({"WAITING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"})
"""Set of accepted Task.status values enforced by model-level validation."""


class Task(db.Model):
    """Scheduled task entity representing a background job definition.

    Attributes
    ----------
    id : str
        UUID primary key (36-character string).
    name : str
        Unique task name for identification (max 255 chars).
    type : str
        Task type classifier (e.g. ``cleanup``, ``maintenance``, ``backup``).
    schedule : str or None
        Cron expression for recurring execution; ``None`` for one-time tasks.
    enabled : bool
        Whether the task is enabled for scheduling (default ``True``).
    status : str
        Current lifecycle status (default ``WAITING``).
        Validated against :data:`VALID_STATUSES`.
    message : str or None
        Optional human-readable description or progress message.
    error_message : str or None
        Error details captured when status transitions to ``FAILED``.
    properties : dict or None
        Arbitrary JSON configuration parameters for the task.
    started_at : datetime or None
        Timestamp when the task began executing.
    completed_at : datetime or None
        Timestamp when the task finished (success or failure).
    last_run_at : datetime or None
        Timestamp of the most recent previous execution.
    next_run_at : datetime or None
        Computed timestamp for the next scheduled execution.
    created_at : datetime
        Timestamp when the task record was created.
    updated_at : datetime
        Timestamp of the last record modification.
    """

    __tablename__ = "tasks"

    id: str = db.Column(
        db.String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        doc="UUID primary key",
    )
    name: str = db.Column(
        db.String(255),
        unique=True,
        nullable=False,
        index=True,
        doc="Unique task name for identification",
    )
    type: str = db.Column(
        db.String(100),
        nullable=False,
        doc="Task type classifier",
    )
    schedule: Optional[str] = db.Column(
        db.String(255),
        nullable=True,
        doc="Cron expression for recurring execution",
    )
    enabled: bool = db.Column(
        db.Boolean,
        default=True,
        nullable=False,
        doc="Whether the task is enabled for scheduling",
    )
    status: str = db.Column(
        db.String(50),
        nullable=False,
        default="WAITING",
        index=True,
        doc="Current lifecycle status",
    )
    message: Optional[str] = db.Column(
        db.Text,
        nullable=True,
        doc="Human-readable description or progress message",
    )
    error_message: Optional[str] = db.Column(
        db.Text,
        nullable=True,
        doc="Error details when status is FAILED",
    )
    properties: Optional[dict] = db.Column(
        db.JSON,
        nullable=True,
        doc="Arbitrary JSON configuration parameters",
    )
    started_at: Optional[datetime.datetime] = db.Column(
        db.DateTime,
        nullable=True,
        doc="Timestamp when the task began executing",
    )
    completed_at: Optional[datetime.datetime] = db.Column(
        db.DateTime,
        nullable=True,
        doc="Timestamp when the task finished",
    )
    last_run_at: Optional[datetime.datetime] = db.Column(
        db.DateTime,
        nullable=True,
        doc="Timestamp of the most recent previous execution",
    )
    next_run_at: Optional[datetime.datetime] = db.Column(
        db.DateTime,
        nullable=True,
        doc="Computed timestamp for next scheduled execution",
    )
    created_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp when the task record was created",
    )
    updated_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp of the last record modification",
    )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @validates("status")
    def validate_status(self, key: str, value: str) -> str:
        """Validate that status is one of the accepted lifecycle values.

        Parameters
        ----------
        key : str
            The attribute name (``'status'``).
        value : str
            The proposed status value.

        Returns
        -------
        str
            The validated status value.

        Raises
        ------
        ValueError
            If *value* is not in :data:`VALID_STATUSES`.
        """
        if value not in VALID_STATUSES:
            raise ValueError(
                f"Invalid task status: {value!r}. "
                f"Must be one of {sorted(VALID_STATUSES)}."
            )
        return value

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the task to a dictionary for API responses.

        Returns
        -------
        dict
            Dictionary representation of the task suitable for JSON
            serialisation. Datetime values are converted to ISO-8601
            strings.
        """
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "status": self.status,
            "message": self.message,
            "error_message": self.error_message,
            "properties": self.properties,
            "started_at": (
                self.started_at.isoformat() if self.started_at else None
            ),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "last_run_at": (
                self.last_run_at.isoformat() if self.last_run_at else None
            ),
            "next_run_at": (
                self.next_run_at.isoformat() if self.next_run_at else None
            ),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return (
            f"<Task id={self.id!r} name={self.name!r} status={self.status!r}>"
        )
