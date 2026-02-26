"""
Task Scheduling and Management REST API Blueprint.

Implements Feature F-501-RQ-004 (System Management — Tasks) and Feature
F-402 (Scheduled Tasks).  Provides full CRUD for task definitions, manual
task execution triggers, execution history retrieval, and running-task
cancellation.

**Architecture Context:**

This module replaces the Quartz 2.3.2 task management REST API from the
original Java source system (Sonatype Nexus Repository).  In the Java
architecture, task management was exposed through RESTEasy 6.2.7 JAX-RS
resource classes backed by the Quartz scheduler.  Here, equivalent
functionality is provided through a flask-smorest 0.45.0 Blueprint with
OpenAPI 3.x auto-documentation.

**Endpoint Inventory:**

+--------+-----------------------------------+-------+---------------------------+
| Method | Path                              | Code  | Description               |
+========+===================================+=======+===========================+
| GET    | /api/v1/tasks                     | 200   | List task definitions     |
+--------+-----------------------------------+-------+---------------------------+
| GET    | /api/v1/tasks/{task_id}           | 200   | Get task definition       |
+--------+-----------------------------------+-------+---------------------------+
| POST   | /api/v1/tasks                     | 201   | Create task definition    |
+--------+-----------------------------------+-------+---------------------------+
| PUT    | /api/v1/tasks/{task_id}           | 200   | Update task definition    |
+--------+-----------------------------------+-------+---------------------------+
| DELETE | /api/v1/tasks/{task_id}           | 204   | Delete task + executions  |
+--------+-----------------------------------+-------+---------------------------+
| POST   | /api/v1/tasks/{task_id}/run       | 202   | Trigger manual execution  |
+--------+-----------------------------------+-------+---------------------------+
| POST   | /api/v1/tasks/{task_id}/stop      | 200   | Stop running execution    |
+--------+-----------------------------------+-------+---------------------------+
| GET    | /api/v1/tasks/{task_id}/executions| 200   | Execution history         |
+--------+-----------------------------------+-------+---------------------------+

**Authentication / Authorisation:**

All endpoints require authentication via the multi-realm chain
(``login_required``) and system-wide (Tier 1) RBAC permission checks
(``require_permission``).  Permission domain is ``'tasks'`` with actions:
``read``, ``create``, ``update``, ``delete``, ``run``.

**Event Emission:**

Task lifecycle events are dispatched via Blinker signals:
- ``EventType.TASK_STARTED`` — emitted when a task is manually triggered.
- ``EventType.TASK_COMPLETED`` — emitted when a running task is stopped.

Exports:
    tasks_bp : flask-smorest Blueprint registered at ``/api/v1/tasks``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import current_app, g, jsonify, request
from flask_smorest import Blueprint, abort

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.extensions import db
from src.app.models.task import TaskDefinition, TaskExecution
from src.app.schemas.task import (
    TaskCreateSchema,
    TaskDefinitionSchema,
    TaskExecutionSchema,
    TaskRunRequestSchema,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for task management operations, manual task
# runs, execution events, and error conditions.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known Task Types
# ---------------------------------------------------------------------------
# Standard Nexus Repository task type identifiers.  These are used for
# informational logging and match the VALID_TASK_TYPES list defined in the
# TaskCreateSchema.  Actual validation is handled by Marshmallow.
# ---------------------------------------------------------------------------

KNOWN_TASK_TYPES: frozenset[str] = frozenset({
    "repository.cleanup",
    "blobstore.compact",
    "blobstore.cleanup-temp",
    "blobstore.rebuild-index",
    "repository.rebuild-index",
    "repository.repair-rebuild",
    "db.backup",
    "db.vacuum",
    "security.purge-api-keys",
    "system.log-rotate",
})

# ---------------------------------------------------------------------------
# Default Execution History Limits
# ---------------------------------------------------------------------------

_DEFAULT_EXECUTION_LIMIT: int = 50
"""Default number of execution history records to return per request."""

_MAX_EXECUTION_LIMIT: int = 1000
"""Maximum number of execution history records to return per request."""

# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------
# Replaces the JAX-RS resource class registration from RESTEasy 6.2.7.
# The Blueprint is registered in src/app/factory.py via
# api.register_blueprint(tasks_bp).
# ---------------------------------------------------------------------------

tasks_bp: Blueprint = Blueprint(
    "tasks",
    __name__,
    url_prefix="/api/v1/tasks",
    description="Scheduled task management and execution (F-402, F-501-RQ-004)",
)


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------


def _get_task_or_404(task_id: str) -> TaskDefinition:
    """Retrieve a ``TaskDefinition`` by primary key or abort with 404.

    Uses ``db.session.query()`` to look up the task definition by its
    ``task_id`` primary key.  Aborts with a structured 404 JSON response
    if the task does not exist.

    Args:
        task_id: The unique task definition identifier.

    Returns:
        The matching ``TaskDefinition`` instance.

    Raises:
        HTTPException: 404 if no task with the given ID exists.
    """
    task: Optional[TaskDefinition] = (
        db.session.query(TaskDefinition)
        .filter(TaskDefinition.task_id == task_id)
        .first()
    )
    if task is None:
        logger.warning(
            "Task definition not found: task_id='%s'",
            task_id,
        )
        abort(404, message=f"Task definition not found: {task_id}")
    return task


def _get_current_user_id() -> Optional[str]:
    """Extract the authenticated user's ID from the Flask ``g`` context.

    Returns:
        The ``user_id`` string if a user is authenticated, or ``None``
        if no authenticated user is present in the request context.
    """
    user = getattr(g, "current_user", None)
    if user is not None:
        return getattr(user, "user_id", None)
    return None


# ===========================================================================
# GET / — List Task Definitions
# ===========================================================================


@tasks_bp.route("/")
@tasks_bp.response(200, TaskDefinitionSchema(many=True))
@login_required
@require_permission("tasks", "read")
def list_tasks() -> List[TaskDefinition]:
    """List all scheduled task definitions.

    Returns all configured task definitions with their enabled/disabled
    status, cron expressions, and cached last-run information.  Supports
    optional filtering by task type via the ``type`` query parameter.

    **Query Parameters:**

    - ``type`` (str, optional): Filter results to only task definitions
      matching the specified task type identifier (e.g.,
      ``repository.cleanup``, ``blobstore.compact``).

    **Responses:**

    - 200: List of task definitions (may be empty).
    - 401: Authentication required.
    - 403: Insufficient privileges (``tasks:read``).
    """
    task_type_filter: Optional[str] = request.args.get("type")

    query = db.session.query(TaskDefinition)

    if task_type_filter:
        query = query.filter(TaskDefinition.type == task_type_filter)
        logger.debug(
            "Filtering task definitions by type='%s'.",
            task_type_filter,
        )

    tasks: List[TaskDefinition] = query.order_by(TaskDefinition.name).all()

    current_app.logger.info(
        "Listed %d task definition(s)%s",
        len(tasks),
        f" (type={task_type_filter})" if task_type_filter else "",
    )

    return tasks


# ===========================================================================
# GET /<task_id> — Get Task Definition
# ===========================================================================


@tasks_bp.route("/<string:task_id>")
@tasks_bp.response(200, TaskDefinitionSchema)
@login_required
@require_permission("tasks", "read")
def get_task(task_id: str) -> TaskDefinition:
    """Get a specific task definition by ID.

    Returns the full task definition including configuration, schedule,
    cached last-run status, and next scheduled run time.

    **Path Parameters:**

    - ``task_id`` (str): Unique task definition identifier.

    **Responses:**

    - 200: Task definition details.
    - 401: Authentication required.
    - 403: Insufficient privileges (``tasks:read``).
    - 404: Task definition not found.
    """
    task: TaskDefinition = _get_task_or_404(task_id)

    logger.info(
        "Retrieved task definition: task_id='%s', type='%s', "
        "enabled=%s, is_scheduled=%s",
        task.task_id,
        task.type,
        task.enabled,
        task.is_scheduled,
    )

    return task


# ===========================================================================
# POST / — Create Task Definition
# ===========================================================================


@tasks_bp.route("/", methods=["POST"])
@login_required
@require_permission("tasks", "create")
@tasks_bp.arguments(TaskCreateSchema)
@tasks_bp.response(201, TaskDefinitionSchema)
def create_task(task_data: Dict[str, Any]) -> TaskDefinition:
    """Create a new scheduled task definition.

    Validates the incoming payload against ``TaskCreateSchema``, which
    enforces valid task type (from the standard Nexus Repository task type
    set) and optional cron expression format validation.

    If ``task_id`` is not provided in the request body, a UUID is
    auto-generated.  The task is created with ``enabled=True`` by default.

    **Request Body:** ``TaskCreateSchema``

    **Responses:**

    - 201: Task definition created successfully.
    - 401: Authentication required.
    - 403: Insufficient privileges (``tasks:create``).
    - 409: Task definition with the same ``task_id`` already exists.
    - 422: Validation error (invalid type, malformed cron expression).
    """
    # Resolve task_id — auto-generate UUID if not provided by the client.
    task_id: str = task_data.get("task_id") or str(uuid.uuid4())

    # Check for duplicate task_id — primary key collision.
    existing: Optional[TaskDefinition] = (
        db.session.query(TaskDefinition)
        .filter(TaskDefinition.task_id == task_id)
        .first()
    )
    if existing is not None:
        logger.warning(
            "Duplicate task_id on create: task_id='%s'",
            task_id,
        )
        abort(409, message=f"Task definition already exists: {task_id}")

    # Construct the new TaskDefinition model instance.
    task: TaskDefinition = TaskDefinition(
        task_id=task_id,
        type=task_data["type"],
        name=task_data["name"],
        cron_expression=task_data.get("cron_expression"),
        enabled=task_data.get("enabled", True),
        configuration=task_data.get("configuration"),
    )

    db.session.add(task)
    db.session.commit()

    current_app.logger.info(
        "Created task definition: task_id='%s', type='%s', name='%s', "
        "cron='%s', enabled=%s",
        task.task_id,
        task.type,
        task.name,
        task.cron_expression,
        task.enabled,
    )

    return task


# ===========================================================================
# PUT /<task_id> — Update Task Definition
# ===========================================================================


@tasks_bp.route("/<string:task_id>", methods=["PUT"])
@login_required
@require_permission("tasks", "update")
@tasks_bp.arguments(TaskCreateSchema)
@tasks_bp.response(200, TaskDefinitionSchema)
def update_task(task_data: Dict[str, Any], task_id: str) -> TaskDefinition:
    """Update an existing task definition.

    Replaces the task definition fields with the values from the request
    body.  Supports updating ``name``, ``type``, ``cron_expression``,
    ``enabled``, and ``configuration``.

    **Path Parameters:**

    - ``task_id`` (str): Unique task definition identifier.

    **Request Body:** ``TaskCreateSchema``

    **Responses:**

    - 200: Task definition updated successfully.
    - 401: Authentication required.
    - 403: Insufficient privileges (``tasks:update``).
    - 404: Task definition not found.
    - 422: Validation error (invalid type, malformed cron expression).
    """
    task: TaskDefinition = _get_task_or_404(task_id)

    # Apply updates from the validated request data.
    # All fields from TaskCreateSchema are applied (PUT = full replacement).
    if "name" in task_data and task_data["name"] is not None:
        task.name = task_data["name"]

    if "type" in task_data and task_data["type"] is not None:
        task.type = task_data["type"]

    if "cron_expression" in task_data:
        task.cron_expression = task_data.get("cron_expression")

    if "enabled" in task_data:
        task.enabled = task_data["enabled"]

    if "configuration" in task_data:
        task.configuration = task_data.get("configuration")

    db.session.commit()

    current_app.logger.info(
        "Updated task definition: task_id='%s', type='%s', name='%s', "
        "cron='%s', enabled=%s",
        task.task_id,
        task.type,
        task.name,
        task.cron_expression,
        task.enabled,
    )

    return task


# ===========================================================================
# DELETE /<task_id> — Delete Task Definition
# ===========================================================================


@tasks_bp.route("/<string:task_id>", methods=["DELETE"])
@tasks_bp.response(204)
@login_required
@require_permission("tasks", "delete")
def delete_task(task_id: str) -> None:
    """Delete a task definition and all associated execution records.

    Removes the task definition and cascade-deletes all linked
    ``TaskExecution`` records (via the ``all, delete-orphan`` cascade on
    the ``TaskDefinition.executions`` relationship).

    **Path Parameters:**

    - ``task_id`` (str): Unique task definition identifier.

    **Responses:**

    - 204: Task definition deleted successfully (no content).
    - 401: Authentication required.
    - 403: Insufficient privileges (``tasks:delete``).
    - 404: Task definition not found.
    """
    task: TaskDefinition = _get_task_or_404(task_id)

    task_name: str = task.name
    task_type: str = task.type

    db.session.delete(task)
    db.session.commit()

    current_app.logger.info(
        "Deleted task definition: task_id='%s', type='%s', name='%s'",
        task_id,
        task_type,
        task_name,
    )


# ===========================================================================
# POST /<task_id>/run — Trigger Immediate Execution
# ===========================================================================


@tasks_bp.route("/<string:task_id>/run", methods=["POST"])
@tasks_bp.response(202, TaskExecutionSchema)
@login_required
@require_permission("tasks", "run")
def run_task(task_id: str) -> TaskExecution:
    """Trigger immediate execution of a scheduled task.

    Creates a new ``TaskExecution`` record with ``status='running'`` and
    emits a ``TASK_STARTED`` event via the Blinker event bus.  The
    execution runs asynchronously; this endpoint returns 202 Accepted
    immediately.

    The task is executed regardless of its ``enabled`` status or cron
    schedule — manual runs always proceed.

    **Path Parameters:**

    - ``task_id`` (str): Unique task definition identifier.

    **Responses:**

    - 202: Execution started (returns the ``TaskExecution`` record).
    - 401: Authentication required.
    - 403: Insufficient privileges (``tasks:run``).
    - 404: Task definition not found.
    """
    task: TaskDefinition = _get_task_or_404(task_id)

    # Create a new execution record for this manual run.
    now: datetime = datetime.now(timezone.utc)

    execution: TaskExecution = TaskExecution(
        task_id=task.task_id,
        start_time=now,
        status="running",
    )

    db.session.add(execution)

    # Update the cached last_run_status on the parent task definition.
    task.last_run_status = "running"

    db.session.commit()

    # Emit TASK_STARTED event via Blinker for audit logging (F-303) and
    # metrics monitoring (F-401).
    user_id: Optional[str] = _get_current_user_id()

    emit_event(
        EventType.TASK_STARTED,
        payload={
            "task_id": task.task_id,
            "task_type": task.type,
            "task_name": task.name,
            "execution_id": execution.execution_id,
            "user_id": user_id,
        },
    )

    current_app.logger.info(
        "Task run triggered: task_id='%s', execution_id=%s, "
        "triggered_by='%s'",
        task.task_id,
        execution.execution_id,
        user_id or "unknown",
    )

    return execution


# ===========================================================================
# POST /<task_id>/stop — Stop Running Task Execution
# ===========================================================================


@tasks_bp.route("/<string:task_id>/stop", methods=["POST"])
@tasks_bp.response(200, TaskExecutionSchema)
@login_required
@require_permission("tasks", "run")
def stop_task(task_id: str) -> TaskExecution:
    """Stop a currently running task execution.

    Finds the most recent execution with ``status='running'`` for the
    specified task and marks it as ``'canceled'`` via the
    ``TaskExecution.complete()`` method.  Emits a ``TASK_COMPLETED``
    event with ``status='canceled'``.

    **Path Parameters:**

    - ``task_id`` (str): Unique task definition identifier.

    **Responses:**

    - 200: Execution canceled successfully (returns the updated record).
    - 401: Authentication required.
    - 403: Insufficient privileges (``tasks:run``).
    - 404: Task definition not found OR no running execution found.
    """
    task: TaskDefinition = _get_task_or_404(task_id)

    # Find the most recent running execution for this task.
    running_execution: Optional[TaskExecution] = (
        TaskExecution.query
        .filter_by(task_id=task.task_id, status="running")
        .order_by(TaskExecution.start_time.desc())
        .first()
    )

    if running_execution is None:
        logger.warning(
            "No running execution found for task: task_id='%s'",
            task_id,
        )
        abort(
            404,
            message=f"No running execution found for task: {task_id}",
        )

    # Verify execution is truly running before canceling.
    if not running_execution.is_running:
        logger.warning(
            "Execution %s for task '%s' is not in running state: "
            "status='%s'",
            running_execution.execution_id,
            task_id,
            running_execution.status,
        )
        abort(
            409,
            message=(
                f"Execution {running_execution.execution_id} is not "
                f"running (status: {running_execution.status})"
            ),
        )

    # Mark the execution as canceled.  The complete() method sets
    # end_time, computes duration_ms, and commits to the database.
    running_execution.complete(status="canceled")

    # Update the cached last_run_status on the parent task definition.
    task.last_run_status = "canceled"
    db.session.commit()

    # Emit TASK_COMPLETED event for audit logging and monitoring.
    user_id: Optional[str] = _get_current_user_id()

    emit_event(
        EventType.TASK_COMPLETED,
        payload={
            "task_id": task.task_id,
            "task_type": task.type,
            "task_name": task.name,
            "execution_id": running_execution.execution_id,
            "status": "canceled",
            "duration_ms": running_execution.duration_ms,
            "user_id": user_id,
        },
    )

    current_app.logger.info(
        "Task stopped: task_id='%s', execution_id=%s, "
        "duration_ms=%s, canceled_by='%s'",
        task.task_id,
        running_execution.execution_id,
        running_execution.duration_ms,
        user_id or "unknown",
    )

    return running_execution


# ===========================================================================
# GET /<task_id>/executions — Task Execution History
# ===========================================================================


@tasks_bp.route("/<string:task_id>/executions")
@tasks_bp.response(200, TaskExecutionSchema(many=True))
@login_required
@require_permission("tasks", "read")
def list_executions(task_id: str) -> List[TaskExecution]:
    """Get execution history for a specific task.

    Returns a paginated list of execution records for the specified task,
    ordered by ``start_time`` descending (most recent first).  Each record
    includes status, duration, progress, and error details.

    **Path Parameters:**

    - ``task_id`` (str): Unique task definition identifier.

    **Query Parameters:**

    - ``limit`` (int, optional): Maximum number of records to return.
      Defaults to 50, capped at 1000.

    **Responses:**

    - 200: List of execution records (may be empty).
    - 401: Authentication required.
    - 403: Insufficient privileges (``tasks:read``).
    - 404: Task definition not found.
    """
    task: TaskDefinition = _get_task_or_404(task_id)

    # Parse the optional limit parameter with sane defaults and ceiling.
    limit: int = request.args.get("limit", _DEFAULT_EXECUTION_LIMIT, type=int)
    limit = max(1, min(limit, _MAX_EXECUTION_LIMIT))

    # Query execution history ordered by most recent first.
    executions: List[TaskExecution] = (
        task.executions
        .order_by(TaskExecution.start_time.desc())
        .limit(limit)
        .all()
    )

    logger.info(
        "Listed %d execution(s) for task: task_id='%s' (limit=%d)",
        len(executions),
        task_id,
        limit,
    )

    return executions


# ---------------------------------------------------------------------------
# Error Handler Registration
# ---------------------------------------------------------------------------
# Register blueprint-scoped error handlers to provide consistent JSON
# error responses for common HTTP error codes.  These augment the
# application-wide error handlers defined in factory.py.
# ---------------------------------------------------------------------------


@tasks_bp.app_context_processor
def inject_task_types() -> Dict[str, Any]:
    """Inject known task types into template context (if ever needed).

    Returns:
        Dictionary with ``known_task_types`` available in templates.
    """
    return {"known_task_types": sorted(KNOWN_TASK_TYPES)}


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------

logger.debug(
    "Tasks API blueprint module loaded — url_prefix='/api/v1/tasks', "
    "endpoints: list_tasks, get_task, create_task, update_task, "
    "delete_task, run_task, stop_task, list_executions",
)
