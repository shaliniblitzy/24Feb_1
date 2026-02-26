"""
Script CRUD and Execution REST API Blueprint — Feature F-502 (Scripting Support).

This module implements the scripting management REST API endpoints for the
Sonatype Nexus Repository Flask application.  It provides a complete CRUD
interface for named Python scripts and a sandboxed execution endpoint,
replacing the Java Groovy scripting plugin REST API from the original
source system.

Endpoints:
    GET    /api/v1/scripts                   — List all stored scripts
    GET    /api/v1/scripts/<script_name>      — Get a single script by name
    POST   /api/v1/scripts                   — Create a new script
    PUT    /api/v1/scripts/<script_name>      — Update an existing script
    DELETE /api/v1/scripts/<script_name>      — Delete a script
    POST   /api/v1/scripts/<script_name>/run  — Execute a script

Architecture Context:
    In the Java source system, Nexus exposed a Groovy scripting API via
    JAX-RS resource classes (RESTEasy 6.2.7) with Jackson 2.16.1 JSON
    serialization and Apache Shiro 2.0.0 security.  This Python
    reimplementation replaces all of those technologies:

    +-------------------------------+--------------------------------------+
    | Java Component                | Python Replacement                   |
    +===============================+======================================+
    | RESTEasy 6.2.7 JAX-RS        | flask-smorest 0.45.0 Blueprint       |
    +-------------------------------+--------------------------------------+
    | Jackson 2.16.1               | Marshmallow 3.23.2 inline schemas    |
    +-------------------------------+--------------------------------------+
    | Swagger/OpenAPI 2.2.20       | flask-smorest auto-gen OpenAPI 3.x   |
    +-------------------------------+--------------------------------------+
    | Apache Shiro 2.0.0           | login_required + require_permission  |
    +-------------------------------+--------------------------------------+
    | Groovy scripting engine      | Python-native sandboxed execution    |
    +-------------------------------+--------------------------------------+

Security Model:
    - All endpoints require authentication (``@login_required``).
    - CRUD operations require ``scripts:read``, ``scripts:create``, or
      ``scripts:delete`` system-wide (Tier 1) privileges enforced via
      ``@require_permission``.
    - Script execution requires ``scripts:run`` privilege.
    - Execution is sandboxed with restricted builtins, AST validation,
      and configurable timeout enforcement.
    - All operations are logged for audit trail compliance (Feature F-303).

Exports:
    scripts_bp : flask_smorest.Blueprint
        The scripts API blueprint, registered at ``/api/v1/scripts``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from flask import current_app, jsonify
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields, validate
from werkzeug.exceptions import HTTPException

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.services.script_service import (
    ScriptError,
    ScriptExecutionError,
    ScriptService,
    ScriptTimeoutError,
    ScriptValidationError,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source system.  Provides diagnostic output for script CRUD operations,
# execution requests, security validations, and error conditions per AAP
# Section 0.7.2 (structured JSON logging compatible with SIEM integration).
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ===========================================================================
# Blueprint Definition
# ===========================================================================

scripts_bp: Blueprint = Blueprint(
    "scripts",
    __name__,
    url_prefix="/api/v1/scripts",
    description="Script storage and execution (F-502)",
)
"""Flask-smorest Blueprint for the scripts API endpoint group.

Registered at ``/api/v1/scripts`` by the application factory in
``src.app.factory.create_app()``.  Provides OpenAPI 3.x auto-generated
documentation for all scripting endpoints.
"""

# ===========================================================================
# Supported Script Types
# ===========================================================================
# The original Java system supported 'groovy' as the scripting language.
# In this Python reimplementation, 'python' is the primary (and default)
# scripting type.  Additional type labels are permitted for metadata
# categorisation but all scripts are executed as Python.
# ===========================================================================

SUPPORTED_SCRIPT_TYPES: List[str] = ["python"]
"""List of supported script type identifiers.

Only ``'python'`` is supported in the Python reimplementation (replacing
``'groovy'`` from the Java source system).  The type field serves as a
metadata label — all scripts are validated and executed as Python code
regardless of the type value.
"""


# ===========================================================================
# Inline Marshmallow Schemas
# ===========================================================================
# No dedicated script schema file exists in the schemas/ package, so
# request/response schemas are defined inline in this blueprint module.
# This mirrors the approach used in the health.py blueprint.
# ===========================================================================


class ScriptCreateSchema(Schema):
    """Marshmallow schema for script creation requests (POST /api/v1/scripts).

    Validates the incoming JSON body for creating a new script.  All three
    fields are required.

    Replaces Jackson 2.16.1 JSON deserialization from the Java source system.

    Attributes:
        name: Unique script identifier (alphanumeric + underscores).
        type: Script type label.  Must be one of ``SUPPORTED_SCRIPT_TYPES``.
        content: Python source code of the script (1–65536 chars).
    """

    name = fields.String(
        required=True,
        metadata={
            "description": "Unique script identifier (alphanumeric and underscores)",
            "example": "cleanup_temp",
        },
        validate=validate.Length(min=1, max=256),
    )
    type = fields.String(
        required=True,
        metadata={
            "description": "Script type label (e.g. 'python')",
            "example": "python",
        },
        validate=validate.OneOf(SUPPORTED_SCRIPT_TYPES),
    )
    content = fields.String(
        required=True,
        metadata={
            "description": "Python source code of the script",
            "example": "result = 'Hello, World!'",
        },
        validate=validate.Length(min=1, max=65536),
    )


class ScriptUpdateSchema(Schema):
    """Marshmallow schema for script update requests (PUT /api/v1/scripts/<name>).

    Only the ``content`` field is required for updates.  The ``type``
    field is optional and can be used to reclassify the script.

    Attributes:
        content: Updated Python source code.
        type: Optional new script type label.
    """

    content = fields.String(
        required=True,
        metadata={
            "description": "Updated Python source code of the script",
            "example": "result = args.get('x', 0) * 2",
        },
        validate=validate.Length(min=1, max=65536),
    )
    type = fields.String(
        required=False,
        load_default=None,
        metadata={
            "description": "Optional new script type label",
        },
        validate=validate.OneOf(SUPPORTED_SCRIPT_TYPES),
    )


class ScriptExecuteSchema(Schema):
    """Marshmallow schema for script execution requests (POST /<name>/run).

    The request body is optional.  If provided, the ``args`` dictionary
    is passed into the script execution namespace as the ``args`` variable.

    Attributes:
        args: Optional key-value arguments passed to the script.
    """

    args = fields.Dict(
        keys=fields.String(),
        values=fields.Raw(),
        required=False,
        load_default=None,
        metadata={
            "description": "Optional arguments passed to the script as 'args' dict",
            "example": {"x": 10, "name": "test"},
        },
    )


class ScriptResponseSchema(Schema):
    """Marshmallow schema for script metadata responses.

    Used for serializing script metadata returned by list and get endpoints.

    Attributes:
        name: Script identifier.
        type: Script type label.
        content: Script source code (included in get, excluded from list).
        content_length: Length of the script content in characters.
        created: ISO 8601 creation timestamp.
        updated: ISO 8601 last-modification timestamp.
    """

    name = fields.String(
        metadata={"description": "Script identifier"},
    )
    type = fields.String(
        metadata={"description": "Script type label"},
    )
    content = fields.String(
        load_default=None,
        metadata={"description": "Script source code"},
    )
    content_length = fields.Integer(
        load_default=None,
        metadata={"description": "Length of script content in characters"},
    )
    created = fields.String(
        load_default=None,
        metadata={"description": "ISO 8601 creation timestamp"},
    )
    updated = fields.String(
        load_default=None,
        metadata={"description": "ISO 8601 last-modification timestamp"},
    )


class ScriptExecutionResultSchema(Schema):
    """Marshmallow schema for script execution result responses.

    Serializes the outcome of a script execution including the result
    value, captured stdout output, and execution metrics.

    Attributes:
        name: Script identifier that was executed.
        result: The value of the ``result`` variable after execution.
        output: Captured stdout output from ``print()`` calls.
        duration_ms: Execution duration in milliseconds.
        status: Execution status (``'success'``).
        execution_id: Unique identifier for this execution run.
    """

    name = fields.String(
        metadata={"description": "Script identifier"},
    )
    result = fields.Raw(
        allow_none=True,
        metadata={"description": "Script execution result value"},
    )
    output = fields.String(
        load_default="",
        metadata={"description": "Captured stdout output"},
    )
    duration_ms = fields.Float(
        metadata={"description": "Execution duration in milliseconds"},
    )
    status = fields.String(
        metadata={"description": "Execution status"},
    )
    execution_id = fields.String(
        metadata={"description": "Unique execution run identifier"},
    )


# ===========================================================================
# Internal Helpers
# ===========================================================================


def _get_script_service() -> ScriptService:
    """Obtain a :class:`ScriptService` instance for the current request.

    Attempts to retrieve a persistent service instance stored in
    ``current_app.extensions['script_service']``.  If none is found
    (e.g., during early startup or minimal test configurations), a fresh
    instance is created on the fly.

    Returns:
        A configured :class:`ScriptService` instance.
    """
    try:
        svc: Optional[ScriptService] = current_app.extensions.get(
            "script_service"
        )
        if svc is not None:
            return svc
    except RuntimeError:
        # Outside application context — fall through to fresh instance.
        pass

    logger.debug(
        "No persistent ScriptService found in app extensions; "
        "creating a new instance."
    )
    return ScriptService()


def _check_scripting_enabled() -> None:
    """Verify that scripting is enabled in system configuration.

    Aborts with HTTP 403 Forbidden if scripting is disabled.  This check
    is performed before all script execution requests and optionally
    before CRUD operations to provide early feedback.

    Raises:
        werkzeug.exceptions.Forbidden: If scripting is disabled.
    """
    svc: ScriptService = _get_script_service()
    if not svc.is_scripting_enabled():
        logger.warning(
            "Scripting is disabled. Denying script operation request."
        )
        abort(
            403,
            message="Scripting is disabled. Enable via system configuration "
                    "'system.scripting.enabled' or the SCRIPTING_ENABLED "
                    "environment variable.",
        )


# ===========================================================================
# Endpoint 1 — List Scripts (GET /api/v1/scripts)
# ===========================================================================


@scripts_bp.route("", methods=["GET"])
@scripts_bp.route("/", methods=["GET"])
@scripts_bp.response(200, description="List of all stored scripts")
@login_required
@require_permission("scripts", "read")
def list_scripts() -> Any:
    """Retrieve metadata for all stored scripts.

    Returns an array of script metadata objects.  Script content is
    **not** included in the listing response to keep the payload
    lightweight — use the individual ``GET /<script_name>`` endpoint
    to retrieve full content.

    **Authentication:** Required (``@login_required``).
    **Authorization:** ``scripts:read`` system-wide privilege.

    HTTP Status Codes:
        200: Script listing returned successfully.
        401: Authentication required.
        403: Insufficient privileges.
        500: Internal server error.

    Returns:
        JSON array of script metadata objects.
    """
    logger.debug("Listing all stored scripts.")

    try:
        svc: ScriptService = _get_script_service()
        scripts: List[Dict[str, Any]] = svc.list_scripts()

        logger.info("Listed %d script(s).", len(scripts))
        return jsonify(scripts), 200

    except HTTPException:
        raise
    except ScriptError as exc:
        logger.error("Failed to list scripts: %s", exc.message)
        abort(500, message=f"Failed to list scripts: {exc.message}")
    except Exception as exc:
        logger.exception("Unexpected error listing scripts: %s", str(exc))
        abort(500, message="An unexpected error occurred while listing scripts.")


# ===========================================================================
# Endpoint 2 — Get Script (GET /api/v1/scripts/<script_name>)
# ===========================================================================


@scripts_bp.route("/<string:script_name>", methods=["GET"])
@scripts_bp.response(200, description="Script details with full content")
@login_required
@require_permission("scripts", "read")
def get_script(script_name: str) -> Any:
    """Retrieve a single script by name, including full source content.

    **Authentication:** Required (``@login_required``).
    **Authorization:** ``scripts:read`` system-wide privilege.

    Args:
        script_name: The unique script identifier (URL path parameter).

    HTTP Status Codes:
        200: Script found and returned.
        401: Authentication required.
        403: Insufficient privileges.
        404: Script not found.
        500: Internal server error.

    Returns:
        JSON object with full script details including content.
    """
    logger.debug("Retrieving script '%s'.", script_name)

    try:
        svc: ScriptService = _get_script_service()
        script_data: Optional[Dict[str, Any]] = svc.get_script(script_name)

        if script_data is None:
            logger.info("Script '%s' not found.", script_name)
            abort(404, message=f"Script '{script_name}' not found.")

        logger.info("Retrieved script '%s'.", script_name)
        return jsonify(script_data), 200

    except HTTPException:
        # Re-raise HTTP exceptions (404, 403, etc.) so Flask handles them
        # correctly.  Without this, abort(404) would be caught by the
        # generic ``except Exception`` below and masked as a 500.
        raise
    except ScriptError as exc:
        logger.error(
            "Failed to retrieve script '%s': %s",
            script_name,
            exc.message,
        )
        abort(500, message=f"Failed to retrieve script: {exc.message}")
    except Exception as exc:
        logger.exception(
            "Unexpected error retrieving script '%s': %s",
            script_name,
            str(exc),
        )
        abort(500, message="An unexpected error occurred while retrieving the script.")


# ===========================================================================
# Endpoint 3 — Create Script (POST /api/v1/scripts)
# ===========================================================================


@scripts_bp.route("", methods=["POST"])
@scripts_bp.route("/", methods=["POST"])
@login_required
@require_permission("scripts", "create")
@scripts_bp.arguments(ScriptCreateSchema, location="json")
@scripts_bp.response(201, description="Script created successfully")
def create_script(json_data: Dict[str, Any]) -> Any:
    """Create a new named script.

    The script content is validated for syntax correctness and safety
    (AST analysis for forbidden constructs) before being persisted.

    **Authentication:** Required (``@login_required``).
    **Authorization:** ``scripts:create`` system-wide privilege.

    Request Body (JSON):
        name (str): Unique script identifier.
        type (str): Script type label (must be ``'python'``).
        content (str): Python source code.

    HTTP Status Codes:
        201: Script created successfully (returns created script object).
        400: Invalid request body (missing fields or validation failure).
        401: Authentication required.
        403: Insufficient privileges.
        409: Script with the same name already exists.
        500: Internal server error.

    Returns:
        JSON object with the created script details and 201 status.
    """
    script_name: str = json_data.get("name", "")
    script_type: str = json_data.get("type", "python")
    script_content: str = json_data.get("content", "")

    logger.info(
        "Creating script '%s' (type=%s, size=%d bytes).",
        script_name,
        script_type,
        len(script_content),
    )

    try:
        svc: ScriptService = _get_script_service()
        svc.create_script(
            name=script_name,
            script_type=script_type,
            content=script_content,
        )

        # Retrieve the created script to return it in the response body.
        # Clients need confirmation of the persisted state without making
        # a separate GET request (AAP F-502 compliance).
        created_script: Optional[Dict[str, Any]] = svc.get_script(script_name)
        if created_script is None:
            # Fallback: construct response from input data if retrieval fails
            created_script = {
                "name": script_name,
                "type": script_type,
                "content": script_content,
            }

        logger.info("Script '%s' created successfully.", script_name)
        return jsonify(created_script), 201

    except HTTPException:
        raise
    except ScriptValidationError as exc:
        logger.warning(
            "Script '%s' creation failed validation: %s",
            script_name,
            exc.message,
        )
        abort(400, message=f"Script validation failed: {exc.message}")
    except ScriptError as exc:
        # ScriptError with "already exists" indicates a 409 Conflict.
        if "already exists" in exc.message.lower():
            logger.info(
                "Script '%s' already exists (conflict).",
                script_name,
            )
            abort(409, message=exc.message)
        logger.error(
            "Script '%s' creation failed: %s",
            script_name,
            exc.message,
        )
        abort(400, message=f"Script creation failed: {exc.message}")
    except Exception as exc:
        logger.exception(
            "Unexpected error creating script '%s': %s",
            script_name,
            str(exc),
        )
        abort(500, message="An unexpected error occurred while creating the script.")


# ===========================================================================
# Endpoint 4 — Update Script (PUT /api/v1/scripts/<script_name>)
# ===========================================================================


@scripts_bp.route("/<string:script_name>", methods=["PUT"])
@login_required
@require_permission("scripts", "create")
@scripts_bp.arguments(ScriptUpdateSchema, location="json")
@scripts_bp.response(200, description="Script updated successfully")
def update_script(json_data: Dict[str, Any], script_name: str) -> Any:
    """Update the content of an existing script.

    Re-validates the new content for syntax correctness and safety
    before persisting the update.

    **Authentication:** Required (``@login_required``).
    **Authorization:** ``scripts:create`` system-wide privilege.

    Args:
        script_name: The script identifier (URL path parameter).

    Request Body (JSON):
        content (str): Updated Python source code.
        type (str, optional): New script type label.

    HTTP Status Codes:
        200: Script updated successfully (returns updated script object).
        400: Invalid content or validation failure.
        401: Authentication required.
        403: Insufficient privileges.
        404: Script not found.
        500: Internal server error.

    Returns:
        JSON object with the updated script details and 200 status.
    """
    new_content: str = json_data.get("content", "")

    logger.info(
        "Updating script '%s' (new size=%d bytes).",
        script_name,
        len(new_content),
    )

    try:
        svc: ScriptService = _get_script_service()
        svc.update_script(name=script_name, content=new_content)

        # Retrieve the updated script to return it in the response body.
        # Clients need confirmation of the persisted state without making
        # a separate GET request (AAP F-502 compliance).
        updated_script: Optional[Dict[str, Any]] = svc.get_script(script_name)
        if updated_script is None:
            # Fallback: construct response from known data if retrieval fails
            updated_script = {
                "name": script_name,
                "content": new_content,
            }

        logger.info("Script '%s' updated successfully.", script_name)
        return jsonify(updated_script), 200

    except HTTPException:
        raise
    except ScriptValidationError as exc:
        logger.warning(
            "Script '%s' update failed validation: %s",
            script_name,
            exc.message,
        )
        abort(400, message=f"Script validation failed: {exc.message}")
    except ScriptError as exc:
        if "not found" in exc.message.lower():
            logger.info("Script '%s' not found for update.", script_name)
            abort(404, message=f"Script '{script_name}' not found.")
        logger.error(
            "Script '%s' update failed: %s",
            script_name,
            exc.message,
        )
        abort(400, message=f"Script update failed: {exc.message}")
    except Exception as exc:
        logger.exception(
            "Unexpected error updating script '%s': %s",
            script_name,
            str(exc),
        )
        abort(500, message="An unexpected error occurred while updating the script.")


# ===========================================================================
# Endpoint 5 — Delete Script (DELETE /api/v1/scripts/<script_name>)
# ===========================================================================


@scripts_bp.route("/<string:script_name>", methods=["DELETE"])
@scripts_bp.response(204, description="Script deleted successfully")
@login_required
@require_permission("scripts", "delete")
def delete_script(script_name: str) -> Any:
    """Delete a script by name.

    **Authentication:** Required (``@login_required``).
    **Authorization:** ``scripts:delete`` system-wide privilege.

    Args:
        script_name: The script identifier (URL path parameter).

    HTTP Status Codes:
        204: Script deleted successfully.
        401: Authentication required.
        403: Insufficient privileges.
        404: Script not found.
        500: Internal server error.

    Returns:
        Empty response with 204 status on success.
    """
    logger.info("Deleting script '%s'.", script_name)

    try:
        svc: ScriptService = _get_script_service()
        deleted: bool = svc.delete_script(script_name)

        if not deleted:
            logger.info("Script '%s' not found for deletion.", script_name)
            abort(404, message=f"Script '{script_name}' not found.")

        logger.info("Script '%s' deleted successfully.", script_name)
        return "", 204

    except HTTPException:
        raise
    except ScriptError as exc:
        logger.error(
            "Failed to delete script '%s': %s",
            script_name,
            exc.message,
        )
        abort(500, message=f"Failed to delete script: {exc.message}")
    except Exception as exc:
        logger.exception(
            "Unexpected error deleting script '%s': %s",
            script_name,
            str(exc),
        )
        abort(500, message="An unexpected error occurred while deleting the script.")


# ===========================================================================
# Endpoint 6 — Execute Script (POST /api/v1/scripts/<script_name>/run)
# ===========================================================================


@scripts_bp.route("/<string:script_name>/run", methods=["POST"])
@login_required
@require_permission("scripts", "run")
@scripts_bp.arguments(ScriptExecuteSchema, location="json", required=False)
@scripts_bp.response(200, description="Script execution result")
def execute_script(json_data: Optional[Dict[str, Any]], script_name: str) -> Any:
    """Execute a stored script within a sandboxed environment.

    The script is retrieved from the database, re-validated for safety
    (defence-in-depth), and executed with restricted builtins and a
    configurable timeout.  The execution result is returned as JSON.

    Scripts can communicate results by assigning to the ``result``
    variable in their execution namespace::

        # Example script content:
        result = args.get('x', 0) * 2

    Standard output from ``print()`` calls is captured and returned in
    the ``output`` field.

    **Authentication:** Required (``@login_required``).
    **Authorization:** ``scripts:run`` system-wide privilege.

    **Security Controls:**
        - AST-validated before every execution (defence-in-depth).
        - Restricted builtins (no ``open``, ``exec``, ``eval``,
          ``__import__``).
        - Forbidden import statements (``import``, ``from … import``).
        - Timeout enforcement (default 30 seconds).
        - Sandboxed namespace without access to application internals.

    Args:
        script_name: The script identifier (URL path parameter).

    Request Body (JSON, optional):
        args (dict): Key-value arguments passed as ``args`` in the
            script namespace.

    HTTP Status Codes:
        200: Script executed successfully; result returned.
        400: Invalid execution arguments or script validation failure.
        403: Scripting is disabled or insufficient privileges.
        404: Script not found.
        408: Script execution timed out.
        500: Script execution error or internal server error.

    Returns:
        JSON object with execution result, captured output, duration,
        and execution metadata.
    """
    # Extract execution arguments from the request body.
    execution_args: Optional[Dict[str, Any]] = None
    if json_data is not None:
        execution_args = json_data.get("args")

    logger.info(
        "Executing script '%s' (args_provided=%s).",
        script_name,
        execution_args is not None,
    )

    # Check scripting enablement before attempting execution.
    _check_scripting_enabled()

    try:
        svc: ScriptService = _get_script_service()
        result: Dict[str, Any] = svc.execute_script(
            name=script_name,
            args=execution_args,
        )

        logger.info(
            "Script '%s' executed successfully (duration=%.1fms, "
            "execution_id=%s).",
            script_name,
            result.get("duration_ms", 0),
            result.get("execution_id", "unknown"),
        )

        return jsonify(result), 200

    except ScriptTimeoutError as exc:
        logger.warning(
            "Script '%s' execution timed out: %s",
            script_name,
            exc.message,
        )
        abort(
            408,
            message=f"Script execution timed out: {exc.message}",
        )
    except ScriptValidationError as exc:
        logger.warning(
            "Script '%s' failed re-validation before execution: %s",
            script_name,
            exc.message,
        )
        abort(
            400,
            message=f"Script validation failed: {exc.message}",
        )
    except ScriptExecutionError as exc:
        logger.error(
            "Script '%s' execution failed: %s",
            script_name,
            exc.message,
        )
        abort(
            500,
            message=f"Script execution failed: {exc.message}",
        )
    except ScriptError as exc:
        # ScriptError "not found" or "scripting disabled"
        if "not found" in exc.message.lower():
            logger.info(
                "Script '%s' not found for execution.",
                script_name,
            )
            abort(404, message=f"Script '{script_name}' not found.")
        if "disabled" in exc.message.lower():
            logger.warning(
                "Scripting disabled — cannot execute '%s'.",
                script_name,
            )
            abort(
                403,
                message=exc.message,
            )
        logger.error(
            "Script '%s' execution error: %s",
            script_name,
            exc.message,
        )
        abort(500, message=f"Script error: {exc.message}")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Unexpected error executing script '%s': %s",
            script_name,
            str(exc),
        )
        abort(
            500,
            message="An unexpected error occurred during script execution.",
        )


# ===========================================================================
# Blueprint Error Handlers
# ===========================================================================
# Register blueprint-level error handlers for common script-related
# exceptions.  These handlers convert service-layer exceptions into
# structured JSON error responses, providing consistent error formatting
# across all script endpoints.
# ===========================================================================


@scripts_bp.errorhandler(ScriptValidationError)
def handle_script_validation_error(error: ScriptValidationError) -> Any:
    """Handle :class:`ScriptValidationError` at the blueprint level.

    Returns a 400 Bad Request response with a structured error payload
    containing the validation message and any associated error details.

    Args:
        error: The caught validation error.

    Returns:
        Tuple of (JSON response, HTTP status code).
    """
    logger.warning("Script validation error: %s", error.message)
    return jsonify({
        "error": {
            "code": 400,
            "message": error.message,
            "details": error.details,
        }
    }), 400


@scripts_bp.errorhandler(ScriptExecutionError)
def handle_script_execution_error(error: ScriptExecutionError) -> Any:
    """Handle :class:`ScriptExecutionError` at the blueprint level.

    Returns a 500 Internal Server Error response with a structured error
    payload containing the execution failure message and diagnostic details.

    Args:
        error: The caught execution error.

    Returns:
        Tuple of (JSON response, HTTP status code).
    """
    logger.error("Script execution error: %s", error.message)
    return jsonify({
        "error": {
            "code": 500,
            "message": error.message,
            "details": error.details,
        }
    }), 500


@scripts_bp.errorhandler(ScriptTimeoutError)
def handle_script_timeout_error(error: ScriptTimeoutError) -> Any:
    """Handle :class:`ScriptTimeoutError` at the blueprint level.

    Returns a 408 Request Timeout response indicating the script exceeded
    its maximum allowed execution duration.

    Args:
        error: The caught timeout error.

    Returns:
        Tuple of (JSON response, HTTP status code).
    """
    logger.warning("Script timeout error: %s", error.message)
    return jsonify({
        "error": {
            "code": 408,
            "message": error.message,
            "details": error.details,
        }
    }), 408


# ===========================================================================
# Module Load Logging
# ===========================================================================

logger.debug(
    "Scripts API blueprint loaded — url_prefix='/api/v1/scripts', "
    "endpoints: list, get, create, update, delete, execute."
)
