"""
Marshmallow serialization schemas for task scheduling and execution.

This module defines four Marshmallow 3.x schema classes that handle
serialization, deserialization, and validation for the task scheduling
subsystem (Feature F-402 — Scheduled Tasks).  These schemas mirror the
``TaskDefinition`` and ``TaskExecution`` SQLAlchemy models defined in
``src.app.models.task`` and replace the Quartz 2.3.2 task-related Jackson
DTOs from the original Java source system.

**Schema Inventory:**

+-------------------------+----------------------------------------------+
| Schema                  | Purpose                                      |
+=========================+==============================================+
| ``TaskCreateSchema``    | Validates task creation requests.  Includes  |
|                         | cron expression format validation.           |
+-------------------------+----------------------------------------------+
| ``TaskDefinitionSchema``| Full read/write serialization of a task      |
|                         | definition record (dump includes computed    |
|                         | fields like ``last_run_status``).             |
+-------------------------+----------------------------------------------+
| ``TaskExecutionSchema`` | Serializes individual task execution records |
|                         | (primarily dump-only for history reporting). |
+-------------------------+----------------------------------------------+
| ``TaskRunRequestSchema``| Validates manual task run trigger requests.  |
+-------------------------+----------------------------------------------+

**Cron Expression Format:**

The ``cron_expression`` field accepts standard 5-field Unix cron syntax
compatible with APScheduler 3.10.4 ``CronTrigger``:

    ``minute  hour  day_of_month  month  day_of_week``

Each field supports: numbers, wildcards (``*``), ranges (``1-5``),
steps (``*/5``), and comma-separated lists (``1,3,5``).

**Execution Status Values:**

Both ``TaskDefinitionSchema.last_run_status`` and
``TaskExecutionSchema.status`` use the same well-defined lifecycle values:

- ``waiting``  — Execution is queued but has not started.
- ``running``  — Execution is currently in progress.
- ``ok``       — Execution completed successfully.
- ``failed``   — Execution terminated with an error.
- ``canceled`` — Execution was manually or programmatically canceled.

**DateTime Serialization:**

All ``DateTime`` fields are serialized as ISO 8601 formatted strings via
a ``@post_dump`` hook to ensure consistent JSON output across all API
endpoints.

Exports:
    TaskCreateSchema       — Task creation request validation schema.
    TaskDefinitionSchema   — Task definition read/write schema.
    TaskExecutionSchema    — Task execution record serialization schema.
    TaskRunRequestSchema   — Manual task run trigger request schema.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from marshmallow import (
    Schema,
    ValidationError,
    fields,
    post_dump,
    pre_load,
    validate,
    validates,
    validates_schema,
)
from marshmallow.validate import Length, OneOf, Range

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "TaskCreateSchema",
    "TaskDefinitionSchema",
    "TaskExecutionSchema",
    "TaskRunRequestSchema",
]

# ---------------------------------------------------------------------------
# Shared Constants
# ---------------------------------------------------------------------------

# Standard Nexus Repository task types that map to specific execution handlers.
# These mirror the task type identifiers used in the Java Quartz 2.3.2
# scheduler configuration.
VALID_TASK_TYPES: list[str] = [
    "repository.cleanup",
    "blobstore.compact",
    "blobstore.rebuild-index",
    "db.backup",
    "db.vacuum",
    "repository.rebuild-index",
    "repository.repair-rebuild",
    "security.purge-api-keys",
    "system.log-rotate",
]
"""Complete list of standard Nexus Repository task types."""

# Valid execution status values matching the TaskExecution model constraints.
VALID_EXECUTION_STATUSES: list[str] = [
    "waiting",
    "running",
    "ok",
    "failed",
    "canceled",
]
"""Valid execution lifecycle status values."""

# ---------------------------------------------------------------------------
# Cron Expression Validation
# ---------------------------------------------------------------------------

# Compiled regex pattern for validating individual cron fields.
# Each field may be:
#   - A single number (e.g. ``5``)
#   - A wildcard (``*``)
#   - A range (e.g. ``1-5``)
#   - A step expression (e.g. ``*/5`` or ``1-30/2``)
#   - A comma-separated list of any of the above (e.g. ``1,3,5``)
_CRON_FIELD_PATTERN: str = (
    r"(\*|[0-9]+(-[0-9]+)?)(\/[0-9]+)?"
)

# A single cron field can also be a comma-separated list of field elements.
_CRON_FIELD_ELEMENT: str = (
    r"(\*|[0-9]+(-[0-9]+)?)(\/[0-9]+)?"
)

# Full pattern for a single cron field including comma-separated lists.
_CRON_SINGLE_FIELD: str = (
    rf"{_CRON_FIELD_ELEMENT}(,{_CRON_FIELD_ELEMENT})*"
)

# Full 5-field cron expression pattern.
# Fields: minute hour day_of_month month day_of_week
_CRON_EXPRESSION_REGEX: re.Pattern[str] = re.compile(
    rf"^{_CRON_SINGLE_FIELD}"
    rf"\s+{_CRON_SINGLE_FIELD}"
    rf"\s+{_CRON_SINGLE_FIELD}"
    rf"\s+{_CRON_SINGLE_FIELD}"
    rf"\s+{_CRON_SINGLE_FIELD}$"
)

# Range constraints for each cron field position.
_CRON_FIELD_RANGES: list[tuple[str, int, int]] = [
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 7),  # 0 and 7 both represent Sunday
]


def _validate_cron_field_ranges(expression: str) -> list[str]:
    """Validate that numeric values in each cron field are within range.

    Parses the 5-field cron expression, extracts numeric values from each
    field, and verifies they fall within the valid range for that field
    position.

    Args:
        expression: A 5-field cron expression string that has already
            passed the regex structural validation.

    Returns:
        A list of error message strings.  Empty if all values are valid.
    """
    errors: list[str] = []
    parts = expression.strip().split()
    if len(parts) != 5:
        return ["Cron expression must have exactly 5 fields"]

    for idx, (field_name, min_val, max_val) in enumerate(_CRON_FIELD_RANGES):
        field_value = parts[idx]

        # Extract all numeric values from the field (skip wildcards).
        for element in field_value.split(","):
            # Remove step suffix (e.g., "*/5" -> "*", "1-30/2" -> "1-30").
            base = element.split("/")[0]

            if base == "*":
                continue

            # Handle ranges (e.g., "1-5").
            if "-" in base:
                range_parts = base.split("-")
                for part in range_parts:
                    try:
                        val = int(part)
                        if val < min_val or val > max_val:
                            errors.append(
                                f"Value {val} in {field_name} field is out of "
                                f"range ({min_val}-{max_val})"
                            )
                    except ValueError:
                        errors.append(
                            f"Invalid numeric value '{part}' in "
                            f"{field_name} field"
                        )
            else:
                # Single numeric value.
                try:
                    val = int(base)
                    if val < min_val or val > max_val:
                        errors.append(
                            f"Value {val} in {field_name} field is out of "
                            f"range ({min_val}-{max_val})"
                        )
                except ValueError:
                    errors.append(
                        f"Invalid numeric value '{base}' in "
                        f"{field_name} field"
                    )

            # Validate step value if present.
            if "/" in element:
                step_str = element.split("/")[1]
                try:
                    step_val = int(step_str)
                    if step_val < 1:
                        errors.append(
                            f"Step value in {field_name} field must be >= 1"
                        )
                except ValueError:
                    errors.append(
                        f"Invalid step value '{step_str}' in "
                        f"{field_name} field"
                    )

    return errors


# ---------------------------------------------------------------------------
# Helper: ISO 8601 DateTime Formatting
# ---------------------------------------------------------------------------


def _format_datetime_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Convert any ``datetime`` values in *data* to ISO 8601 strings.

    Marshmallow's ``DateTime`` field serializes to ISO 8601 by default,
    but this helper ensures consistent formatting for edge cases where
    the value may already be a datetime object (e.g., from SQLAlchemy
    lazy attribute access).

    Args:
        data: A dictionary produced by ``Schema.dump()``.

    Returns:
        The same dictionary with datetime values converted to strings.
    """
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


# ===========================================================================
# TaskCreateSchema
# ===========================================================================


class TaskCreateSchema(Schema):
    """Marshmallow schema for validating task creation requests.

    Used by ``POST /api/v1/tasks`` to validate incoming JSON payloads
    when creating a new scheduled task definition.  Validates task type
    against the standard set of Nexus Repository task types and optionally
    validates the cron expression format for scheduled tasks.

    Fields:
        task_id: Optional unique identifier (auto-generated if omitted).
        type: Required task type identifier from ``VALID_TASK_TYPES``.
        name: Required human-readable task name.
        cron_expression: Optional cron schedule (``None`` for manual tasks).
        enabled: Whether the task schedule is active (default ``True``).
        configuration: Optional task-specific configuration parameters.

    Example payload::

        {
            "type": "repository.cleanup",
            "name": "Daily Snapshot Cleanup",
            "cron_expression": "0 0 * * *",
            "enabled": true,
            "configuration": {
                "repositoryName": "*",
                "policyNames": ["cleanup-snapshots"]
            }
        }
    """

    class Meta:
        """Schema configuration."""

        strict = True

    # -- Fields --------------------------------------------------------------

    task_id = fields.String(
        required=False,
        load_default=None,
        allow_none=True,
        validate=Length(max=200),
        metadata={
            "description": (
                "Unique task identifier.  Auto-generated as a UUID if not "
                "provided.  Must not exceed 200 characters."
            ),
            "example": "cleanup-snapshots-daily",
        },
    )

    type = fields.String(
        required=True,
        validate=OneOf(
            VALID_TASK_TYPES,
            error="Invalid task type.  Must be one of: {choices}.",
        ),
        metadata={
            "description": (
                "Task type identifier used to look up the execution handler.  "
                "Must be one of the standard Nexus Repository task types."
            ),
            "example": "repository.cleanup",
        },
    )

    name = fields.String(
        required=True,
        validate=Length(min=1, max=255),
        metadata={
            "description": "Human-readable task name for display purposes.",
            "example": "Daily Snapshot Cleanup",
        },
    )

    cron_expression = fields.String(
        required=False,
        load_default=None,
        allow_none=True,
        metadata={
            "description": (
                "Standard 5-field Unix cron expression compatible with "
                "APScheduler CronTrigger.  Set to null for one-time or "
                "manual-only tasks.  "
                "Format: 'minute hour day_of_month month day_of_week'."
            ),
            "example": "0 0 * * *",
        },
    )

    enabled = fields.Boolean(
        required=False,
        load_default=True,
        metadata={
            "description": (
                "Whether the task schedule is active.  Defaults to true."
            ),
            "example": True,
        },
    )

    configuration = fields.Dict(
        required=False,
        load_default=None,
        allow_none=True,
        metadata={
            "description": (
                "Task-specific configuration parameters as JSON.  "
                "Structure varies by task type.  Outer structure is "
                "validated; type-specific validation is performed by the "
                "service layer."
            ),
            "example": {
                "repositoryName": "*",
                "policyNames": ["cleanup-snapshots"],
            },
        },
    )

    # -- Custom Validators ---------------------------------------------------

    @validates("cron_expression")
    def validate_cron_expression(self, value: str | None) -> None:
        """Validate that the cron expression has correct 5-field format.

        Accepts standard Unix cron syntax with wildcards, ranges, steps,
        and comma-separated lists.  ``None`` is allowed to represent
        manual-only or one-time tasks.

        Args:
            value: The cron expression string to validate, or ``None``.

        Raises:
            ValidationError: If the expression is malformed or contains
                out-of-range values.
        """
        # None is acceptable — represents a manual-only task.
        if value is None:
            return

        # Empty strings are not valid cron expressions.
        stripped = value.strip()
        if not stripped:
            raise ValidationError(
                "Cron expression must not be empty.  "
                "Use null for manual-only tasks."
            )

        # Structural validation via regex.
        if not _CRON_EXPRESSION_REGEX.match(stripped):
            raise ValidationError(
                "Invalid cron expression format.  Expected 5 space-separated "
                "fields: 'minute hour day_of_month month day_of_week'.  "
                "Each field accepts: numbers, wildcards (*), ranges (1-5), "
                "steps (*/5), and comma-separated lists (1,3,5)."
            )

        # Range validation for each field.
        range_errors = _validate_cron_field_ranges(stripped)
        if range_errors:
            raise ValidationError(
                "Invalid cron expression values: " + "; ".join(range_errors)
            )

    @pre_load
    def strip_whitespace(
        self, data: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        """Strip leading/trailing whitespace from string fields.

        Args:
            data: The incoming data dictionary.

        Returns:
            The data dictionary with whitespace-trimmed string values.
        """
        if isinstance(data.get("task_id"), str):
            data["task_id"] = data["task_id"].strip()
        if isinstance(data.get("name"), str):
            data["name"] = data["name"].strip()
        if isinstance(data.get("cron_expression"), str):
            data["cron_expression"] = data["cron_expression"].strip()
        return data


# ===========================================================================
# TaskDefinitionSchema
# ===========================================================================


class TaskDefinitionSchema(Schema):
    """Marshmallow schema for serializing and deserializing task definitions.

    This schema mirrors the ``TaskDefinition`` SQLAlchemy model and is used
    for both API response serialization (``dump``) and request deserialization
    (``load``).  Computed fields like ``last_run_status`` and ``next_run_time``
    are dump-only — they are populated by the scheduler service and should
    never be set directly by API consumers.

    Fields:
        task_id: Unique task identifier (primary key).
        type: Task type identifier.
        name: Human-readable task name.
        cron_expression: Cron schedule expression (nullable).
        enabled: Whether the task schedule is active.
        configuration: Task-specific configuration parameters (JSON).
        attributes: Extensible metadata from JSONAttributesMixin (JSON).
        last_run_status: Cached status of the most recent execution (dump-only).
        next_run_time: Computed next scheduled execution time (dump-only).
        created_at: Record creation timestamp (dump-only).
        updated_at: Last modification timestamp (dump-only).
    """

    class Meta:
        """Schema configuration."""

        strict = True

    # -- Fields --------------------------------------------------------------

    task_id = fields.String(
        required=True,
        validate=Length(min=1, max=200),
        metadata={
            "description": "Unique task identifier (primary key).",
            "example": "cleanup-snapshots-daily",
        },
    )

    type = fields.String(
        required=True,
        validate=OneOf(
            VALID_TASK_TYPES,
            error="Invalid task type.  Must be one of: {choices}.",
        ),
        metadata={
            "description": "Task type identifier for execution handler lookup.",
            "example": "repository.cleanup",
        },
    )

    name = fields.String(
        required=True,
        validate=Length(min=1, max=255),
        metadata={
            "description": "Human-readable task display name.",
            "example": "Daily Snapshot Cleanup",
        },
    )

    cron_expression = fields.String(
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Cron schedule expression.  Null for manual-only tasks."
            ),
            "example": "0 0 * * *",
        },
    )

    enabled = fields.Boolean(
        load_default=True,
        metadata={
            "description": "Whether the task schedule is active.",
            "example": True,
        },
    )

    configuration = fields.Dict(
        allow_none=True,
        load_default=None,
        metadata={
            "description": "Task-specific configuration parameters (JSON).",
            "example": {
                "repositoryName": "*",
                "policyNames": ["cleanup-snapshots"],
            },
        },
    )

    attributes = fields.Dict(
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Extensible metadata dictionary from JSONAttributesMixin."
            ),
        },
    )

    last_run_status = fields.String(
        dump_only=True,
        allow_none=True,
        load_default=None,
        validate=OneOf(
            VALID_EXECUTION_STATUSES,
            error="Invalid status.  Must be one of: {choices}.",
        ),
        metadata={
            "description": (
                "Cached status of the most recent execution.  "
                "Possible values: waiting, running, ok, failed, canceled."
            ),
            "example": "ok",
        },
    )

    next_run_time = fields.DateTime(
        dump_only=True,
        allow_none=True,
        load_default=None,
        format="iso",
        metadata={
            "description": (
                "Computed next scheduled execution time (ISO 8601).  "
                "Null for disabled or one-time tasks."
            ),
            "example": "2026-03-01T00:00:00",
        },
    )

    created_at = fields.DateTime(
        dump_only=True,
        format="iso",
        metadata={
            "description": "Record creation timestamp (ISO 8601).",
            "example": "2026-02-25T10:00:00",
        },
    )

    updated_at = fields.DateTime(
        dump_only=True,
        format="iso",
        metadata={
            "description": "Last modification timestamp (ISO 8601).",
            "example": "2026-02-25T12:30:00",
        },
    )

    # -- Post-Dump Hook ------------------------------------------------------

    @post_dump
    def format_datetimes(
        self, data: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        """Ensure all datetime values are serialized as ISO 8601 strings.

        Marshmallow's ``DateTime`` field with ``format='iso'`` handles most
        cases, but this hook catches edge cases where the value may still
        be a ``datetime`` object.

        Args:
            data: The dumped data dictionary.

        Returns:
            The data dictionary with datetime values as ISO 8601 strings.
        """
        return _format_datetime_fields(data)


# ===========================================================================
# TaskExecutionSchema
# ===========================================================================


class TaskExecutionSchema(Schema):
    """Marshmallow schema for serializing task execution records.

    This schema mirrors the ``TaskExecution`` SQLAlchemy model and is
    primarily used for **dump** (serialization) of execution history
    records.  It is returned by task execution list and detail API
    endpoints.

    Fields:
        execution_id: Auto-incremented execution record identifier (dump-only).
        task_id: Foreign key referencing the parent TaskDefinition.
        start_time: UTC timestamp when the execution began.
        end_time: UTC timestamp when the execution completed (nullable).
        status: Current execution lifecycle status.
        duration_ms: Execution duration in milliseconds (nullable).
        progress: Human-readable progress message (nullable).
        error_message: Error details when status is 'failed' (nullable).
        created_at: Record creation timestamp (dump-only).

    **Note:** ``end_time``, ``duration_ms``, ``progress``, and
    ``error_message`` are nullable because they are populated during
    or after task execution — not at creation time.
    """

    class Meta:
        """Schema configuration."""

        strict = True

    # -- Fields --------------------------------------------------------------

    execution_id = fields.Integer(
        dump_only=True,
        metadata={
            "description": "Auto-incremented execution record identifier.",
            "example": 42,
        },
    )

    task_id = fields.String(
        required=True,
        validate=Length(min=1, max=200),
        metadata={
            "description": (
                "Foreign key referencing the parent TaskDefinition."
            ),
            "example": "cleanup-snapshots-daily",
        },
    )

    start_time = fields.DateTime(
        required=True,
        format="iso",
        metadata={
            "description": "UTC timestamp when the execution began (ISO 8601).",
            "example": "2026-02-25T00:00:00",
        },
    )

    end_time = fields.DateTime(
        allow_none=True,
        load_default=None,
        format="iso",
        metadata={
            "description": (
                "UTC timestamp when the execution completed (ISO 8601).  "
                "Null while the task is still running."
            ),
            "example": "2026-02-25T00:05:30",
        },
    )

    status = fields.String(
        required=True,
        validate=OneOf(
            VALID_EXECUTION_STATUSES,
            error="Invalid status.  Must be one of: {choices}.",
        ),
        metadata={
            "description": (
                "Current execution lifecycle status.  "
                "Valid values: waiting, running, ok, failed, canceled."
            ),
            "example": "ok",
        },
    )

    duration_ms = fields.Integer(
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Execution duration in milliseconds.  Computed from "
                "start_time and end_time when the execution completes."
            ),
            "example": 330000,
        },
    )

    progress = fields.String(
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Human-readable progress message updated during execution."
            ),
            "example": "50/100 components processed",
        },
    )

    error_message = fields.String(
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Detailed error information populated when status is "
                "'failed'.  May contain stack traces or diagnostic messages."
            ),
        },
    )

    created_at = fields.DateTime(
        dump_only=True,
        format="iso",
        metadata={
            "description": "Record creation timestamp (ISO 8601).",
            "example": "2026-02-25T00:00:00",
        },
    )

    # -- Post-Dump Hook ------------------------------------------------------

    @post_dump
    def format_datetimes(
        self, data: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        """Ensure all datetime values are serialized as ISO 8601 strings.

        Args:
            data: The dumped data dictionary.

        Returns:
            The data dictionary with datetime values as ISO 8601 strings.
        """
        return _format_datetime_fields(data)


# ===========================================================================
# TaskRunRequestSchema
# ===========================================================================


class TaskRunRequestSchema(Schema):
    """Marshmallow schema for manual task run trigger requests.

    Used by ``POST /api/v1/tasks/{task_id}/run`` to validate the request
    payload when triggering an ad-hoc execution of an existing task
    definition.

    Fields:
        task_id: The identifier of the task definition to execute.

    Example payload::

        {
            "task_id": "cleanup-snapshots-daily"
        }
    """

    class Meta:
        """Schema configuration."""

        strict = True

    # -- Fields --------------------------------------------------------------

    task_id = fields.String(
        required=True,
        validate=Length(min=1, max=200),
        metadata={
            "description": (
                "The task definition identifier to trigger for execution."
            ),
            "example": "cleanup-snapshots-daily",
        },
    )
