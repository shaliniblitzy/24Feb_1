"""
Cleanup Policy CRUD and Preview REST API Blueprint — Feature F-204.

This module implements the cleanup policy management REST API endpoints
for the Sonatype Nexus Repository Flask application, replacing the Java
cleanup policy management resource class from the original RESTEasy 6.2.7
JAX-RS source system.

Endpoints:
    GET    /api/v1/cleanup-policies               — List all cleanup policies
    GET    /api/v1/cleanup-policies/<policy_id>    — Get a specific policy
    POST   /api/v1/cleanup-policies                — Create a new policy
    PUT    /api/v1/cleanup-policies/<policy_id>    — Update an existing policy
    DELETE /api/v1/cleanup-policies/<policy_id>    — Delete a policy
    POST   /api/v1/cleanup-policies/<policy_id>/preview — Dry-run preview
    POST   /api/v1/cleanup-policies/run            — Trigger cleanup execution

Architecture Context:
    - Replaces Java cleanup policy management REST API (RESTEasy 6.2.7)
    - Uses flask-smorest 0.45.0 Blueprint for OpenAPI 3.x documentation
    - Marshmallow 3.23.2 schemas for request/response serialization
    - CleanupService delegates to APScheduler for async execution (F-402)
    - Audit events emitted via Blinker signals (F-303)

Security:
    - All endpoints require authentication via ``login_required`` decorator
    - RBAC permissions enforced via ``require_permission('cleanup', action)``
    - Permission actions: read, create, update, delete, run

Criteria Structure:
    The ``criteria`` JSON dictionary supports the following keys (combined
    with AND logic when multiple are present):

    +---------------------------+-------+-----------------------------------------------+
    | Key                       | Type  | Description                                   |
    +===========================+=======+===============================================+
    | ``lastDownloadedBefore``  | int   | Days since last download threshold            |
    +---------------------------+-------+-----------------------------------------------+
    | ``lastBlobUpdatedBefore`` | int   | Days since blob was last updated threshold    |
    +---------------------------+-------+-----------------------------------------------+
    | ``regexPattern``          | str   | Regex matching component version / asset path |
    +---------------------------+-------+-----------------------------------------------+
    | ``isPrerelease``          | bool  | Match pre-release versions only               |
    +---------------------------+-------+-----------------------------------------------+

Exports:
    cleanup_bp : flask_smorest.Blueprint
        The cleanup API blueprint, registered at ``/api/v1/cleanup-policies``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import current_app, jsonify, request
from flask_smorest import Blueprint, abort
import marshmallow
from marshmallow import Schema, fields, validate

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.extensions import db
from src.app.models.cleanup_policy import CleanupPolicy
from src.app.models.repository import Repository
from src.app.services.cleanup_service import CleanupService

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for all cleanup API operations per AAP
# Section 0.7.2 (structured JSON logging for SIEM integration).
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS: List[str] = [
    "maven2", "npm", "docker", "nuget", "pypi", "apt", "raw",
]
"""Supported repository format identifiers for cleanup policy targeting.

Matches the seven repository formats defined in AAP Section 0.2.4
(Features F-101-RQ-001 through F-101-RQ-007).
"""

VALID_CRITERIA_KEYS: frozenset[str] = frozenset({
    "lastDownloadedBefore",
    "lastBlobUpdatedBefore",
    "regexPattern",
    "isPrerelease",
})
"""Valid keys for the cleanup policy criteria JSON dictionary.

Aligned with ``CleanupService._validate_criteria()`` for consistent
validation between the API layer and service layer.
"""

# ===========================================================================
# Blueprint Definition
# ===========================================================================

cleanup_bp: Blueprint = Blueprint(
    "cleanup",
    __name__,
    url_prefix="/api/v1/cleanup-policies",
    description="Cleanup policy management and execution (F-204)",
)
"""Flask-smorest Blueprint for the cleanup policy API endpoint group.

Registered at ``/api/v1/cleanup-policies`` by
``src.app.api.register_api_blueprints()`` in the application factory.

**CRITICAL**: Export name MUST be ``cleanup_bp`` as specified in the
file schema exports.
"""


# ===========================================================================
# Inline Marshmallow Schemas
# ===========================================================================
# Schemas are defined inline in this module because there is no dedicated
# cleanup schema file in ``src/app/schemas/``.  This follows the pattern
# used by other API blueprints in this application.
# Replaces Jackson 2.16.1 JSON serialization DTOs from the Java source.
# ===========================================================================


class CriteriaSchema(Schema):
    """Marshmallow schema for cleanup policy criteria JSON structure.

    All fields are optional; multiple fields are combined with AND logic
    during cleanup evaluation.  At least one criterion should be specified
    for the policy to have any effect.

    Attributes:
        lastDownloadedBefore: Days since last download threshold.
        lastBlobUpdatedBefore: Days since blob was last updated threshold.
        regexPattern: Regex pattern matching component version / asset path.
        isPrerelease: If True, match pre-release versions only.
    """

    lastDownloadedBefore = fields.Integer(
        load_default=None,
        metadata={
            "description": (
                "Delete assets not downloaded within this many days. "
                "Must be a positive integer."
            ),
        },
    )
    lastBlobUpdatedBefore = fields.Integer(
        load_default=None,
        metadata={
            "description": (
                "Delete assets whose blob was not updated within this many days. "
                "Must be a positive integer."
            ),
        },
    )
    regexPattern = fields.String(
        load_default=None,
        metadata={
            "description": (
                "Delete assets whose path matches this regex pattern."
            ),
        },
    )
    isPrerelease = fields.Boolean(
        load_default=None,
        metadata={
            "description": (
                "If true, delete pre-release versions "
                "(SNAPSHOT, alpha, beta, rc)."
            ),
        },
    )


class CleanupPolicyCreateSchema(Schema):
    """Request schema for creating a new cleanup policy.

    Validates the incoming JSON body for POST /api/v1/cleanup-policies.

    Attributes:
        name: Unique human-readable policy name (required).
        format: Target repository format, or null for all formats.
        criteria: Cleanup criteria dictionary (required).
        description: Optional human-readable description.
    """

    name = fields.String(
        required=True,
        validate=validate.Length(min=1, max=255),
        metadata={
            "description": "Unique human-readable policy name.",
            "example": "cleanup-old-snapshots",
        },
    )
    format = fields.String(
        load_default=None,
        allow_none=True,
        validate=validate.OneOf(
            SUPPORTED_FORMATS,
            error="Invalid format. Must be one of: {choices}",
        ),
        metadata={
            "description": (
                "Target repository format, or null to apply to all formats. "
                "Valid values: maven2, npm, docker, nuget, pypi, apt, raw."
            ),
        },
    )
    criteria = fields.Dict(
        keys=fields.String(),
        required=True,
        metadata={
            "description": (
                "Cleanup criteria dictionary. Supported keys: "
                "lastDownloadedBefore (int days), lastBlobUpdatedBefore "
                "(int days), regexPattern (string), isPrerelease (bool). "
                "All specified criteria are combined with AND logic."
            ),
        },
    )
    description = fields.String(
        load_default=None,
        allow_none=True,
        validate=validate.Length(max=2000),
        metadata={
            "description": "Optional human-readable description of the policy.",
        },
    )


class CleanupPolicyUpdateSchema(Schema):
    """Request schema for updating an existing cleanup policy.

    Validates the incoming JSON body for PUT /api/v1/cleanup-policies/<id>.
    All fields are optional — only supplied fields are updated.  Fields
    that are **not present** in the request body are excluded from the
    deserialized output (via ``EXCLUDE`` unknown handling and no
    ``load_default``), so the handler can distinguish "not provided" from
    "explicitly set to null".

    Attributes:
        name: New policy name (optional).
        format: New target repository format (optional).
        criteria: New cleanup criteria dictionary (optional).
        description: New description (optional).
    """

    name = fields.String(
        validate=validate.Length(min=1, max=255),
        metadata={
            "description": "Updated human-readable policy name.",
        },
    )
    format = fields.String(
        allow_none=True,
        validate=validate.OneOf(
            SUPPORTED_FORMATS,
            error="Invalid format. Must be one of: {choices}",
        ),
        metadata={
            "description": (
                "Updated target repository format, or null for all formats."
            ),
        },
    )
    criteria = fields.Dict(
        keys=fields.String(),
        metadata={
            "description": (
                "Updated cleanup criteria dictionary. "
                "Replaces the entire criteria object."
            ),
        },
    )
    description = fields.String(
        allow_none=True,
        validate=validate.Length(max=2000),
        metadata={
            "description": "Updated description.",
        },
    )

    class Meta:
        """Only include fields actually present in the request body."""
        unknown = marshmallow.EXCLUDE


class CleanupPolicyResponseSchema(Schema):
    """Response schema for a single cleanup policy.

    Serializes a CleanupPolicy model instance for API responses.

    Attributes:
        policy_id: Unique policy identifier (primary key).
        name: Human-readable policy name.
        format: Target repository format (null for all formats).
        criteria: Cleanup criteria dictionary.
        description: Optional description.
        has_download_criteria: Whether lastDownloadedBefore is set.
        has_age_criteria: Whether lastBlobUpdatedBefore is set.
        has_regex_criteria: Whether regexPattern is set.
        has_prerelease_criteria: Whether isPrerelease is True.
        is_format_specific: Whether the policy targets a specific format.
        created_at: UTC timestamp of policy creation.
        updated_at: UTC timestamp of last update.
    """

    policy_id = fields.String(
        metadata={"description": "Unique policy identifier."},
    )
    name = fields.String(
        metadata={"description": "Human-readable policy name."},
    )
    format = fields.String(
        allow_none=True,
        metadata={
            "description": "Target repository format, or null for all.",
        },
    )
    criteria = fields.Dict(
        metadata={"description": "Cleanup criteria dictionary."},
    )
    description = fields.String(
        allow_none=True,
        metadata={"description": "Optional description."},
    )
    has_download_criteria = fields.Boolean(
        metadata={
            "description": "Whether lastDownloadedBefore criterion is set.",
        },
    )
    has_age_criteria = fields.Boolean(
        metadata={
            "description": "Whether lastBlobUpdatedBefore criterion is set.",
        },
    )
    has_regex_criteria = fields.Boolean(
        metadata={"description": "Whether regexPattern criterion is set."},
    )
    has_prerelease_criteria = fields.Boolean(
        metadata={
            "description": "Whether isPrerelease criterion is True.",
        },
    )
    is_format_specific = fields.Boolean(
        metadata={
            "description": "Whether the policy targets a specific format.",
        },
    )
    created_at = fields.DateTime(
        metadata={"description": "UTC timestamp of policy creation."},
    )
    updated_at = fields.DateTime(
        metadata={"description": "UTC timestamp of last update."},
    )


class CleanupPreviewResponseSchema(Schema):
    """Response schema for cleanup preview (dry-run) results.

    Returned by POST /api/v1/cleanup-policies/<policy_id>/preview.

    Attributes:
        policy_name: Name of the evaluated cleanup policy.
        repository: Target repository name.
        matched_assets: Number of assets that would be deleted.
        matched_components: Number of components affected.
        total_size: Estimated bytes of storage that would be reclaimed.
        sample_assets: Up to 20 sample matched assets with path and size.
        preview_timestamp: UTC timestamp when the preview was generated.
    """

    policy_name = fields.String(
        metadata={"description": "Name of the evaluated policy."},
    )
    repository = fields.String(
        metadata={"description": "Target repository name."},
    )
    matched_assets = fields.Integer(
        metadata={"description": "Number of assets matched for cleanup."},
    )
    matched_components = fields.Integer(
        metadata={"description": "Number of components affected."},
    )
    total_size = fields.Integer(
        metadata={"description": "Estimated bytes to be reclaimed."},
    )
    sample_assets = fields.List(
        fields.Dict(),
        metadata={
            "description": "Up to 20 sample assets with path and size.",
        },
    )
    preview_timestamp = fields.DateTime(
        metadata={
            "description": "UTC timestamp when the preview was generated.",
        },
    )


class CleanupRunResponseSchema(Schema):
    """Response schema for cleanup run trigger.

    Returned by POST /api/v1/cleanup-policies/run with HTTP 202 Accepted.

    Attributes:
        message: Confirmation message.
        repository: Target repository name, or 'all' for system-wide.
        accepted_at: UTC timestamp when the request was accepted.
        status: Current status of the cleanup operation.
    """

    message = fields.String(
        metadata={"description": "Confirmation message."},
    )
    repository = fields.String(
        metadata={
            "description": "Target repository, or 'all' for system-wide.",
        },
    )
    accepted_at = fields.String(
        metadata={
            "description": "ISO 8601 UTC timestamp of acceptance.",
        },
    )
    status = fields.String(
        metadata={"description": "Current operation status."},
    )


class PreviewRequestSchema(Schema):
    """Request schema for cleanup preview query parameters.

    Attributes:
        repository: Repository name to preview cleanup against (required).
    """

    repository = fields.String(
        required=True,
        validate=validate.Length(min=1),
        metadata={
            "description": "Repository name to preview cleanup against.",
        },
    )


# ===========================================================================
# Internal Helpers
# ===========================================================================


def _get_cleanup_service() -> CleanupService:
    """Obtain a CleanupService instance.

    Checks ``current_app.extensions`` for a shared CleanupService instance;
    if not found, creates a new one.  This allows the service to be injected
    during testing or shared across requests in production.

    Returns:
        A :class:`CleanupService` instance ready for use.
    """
    try:
        service: Optional[CleanupService] = current_app.extensions.get(
            "cleanup_service"
        )
        if service is not None:
            return service
    except RuntimeError:
        pass

    return CleanupService()


def _validate_criteria_keys(criteria: Dict[str, Any]) -> None:
    """Validate that criteria dictionary contains only recognised keys.

    Performs API-level validation before delegating to the service layer.
    The service layer performs deeper validation (type checks, regex
    compilation) via ``CleanupService._validate_criteria()``.

    Args:
        criteria: The criteria dictionary from the request body.

    Raises:
        Calls ``abort(400)`` if unknown criteria keys are found or if
        the criteria dictionary is empty.
    """
    if not criteria:
        abort(
            400,
            message="Criteria must be a non-empty dictionary with at least "
            "one criterion specified.",
        )

    if not isinstance(criteria, dict):
        abort(400, message="Criteria must be a JSON object (dictionary).")

    unknown_keys: set = set(criteria.keys()) - VALID_CRITERIA_KEYS
    if unknown_keys:
        abort(
            400,
            message=(
                f"Unknown criteria keys: {', '.join(sorted(unknown_keys))}. "
                f"Valid keys: {', '.join(sorted(VALID_CRITERIA_KEYS))}."
            ),
        )

    # Validate individual criteria value types at the API level
    if "lastDownloadedBefore" in criteria:
        val = criteria["lastDownloadedBefore"]
        if not isinstance(val, int) or val <= 0:
            abort(
                400,
                message=(
                    "Criteria 'lastDownloadedBefore' must be a positive "
                    f"integer (days). Got: {val!r}"
                ),
            )

    if "lastBlobUpdatedBefore" in criteria:
        val = criteria["lastBlobUpdatedBefore"]
        if not isinstance(val, int) or val <= 0:
            abort(
                400,
                message=(
                    "Criteria 'lastBlobUpdatedBefore' must be a positive "
                    f"integer (days). Got: {val!r}"
                ),
            )

    if "isPrerelease" in criteria:
        val = criteria["isPrerelease"]
        if not isinstance(val, bool):
            abort(
                400,
                message=(
                    f"Criteria 'isPrerelease' must be a boolean. Got: {val!r}"
                ),
            )

    if "regexPattern" in criteria:
        val = criteria["regexPattern"]
        if not isinstance(val, str) or not val.strip():
            abort(
                400,
                message="Criteria 'regexPattern' must be a non-empty string.",
            )
        import re
        try:
            re.compile(val)
        except re.error as exc:
            abort(
                400,
                message=(
                    f"Criteria 'regexPattern' is not a valid regex: {exc}"
                ),
            )


def _serialize_policy(policy: CleanupPolicy) -> Dict[str, Any]:
    """Serialize a CleanupPolicy model instance to a response dictionary.

    Delegates to ``CleanupPolicy.to_dict()`` which includes all column
    values plus computed property values (has_download_criteria,
    has_age_criteria, etc.).

    Handles ``created_at`` and ``updated_at`` serialization to ISO 8601
    format for JSON compatibility.

    Args:
        policy: The CleanupPolicy instance to serialize.

    Returns:
        A dictionary suitable for JSON response.
    """
    data: Dict[str, Any] = policy.to_dict()

    # Ensure datetime fields are serialized as ISO 8601 strings
    for dt_field in ("created_at", "updated_at"):
        if dt_field in data and data[dt_field] is not None:
            if isinstance(data[dt_field], datetime):
                data[dt_field] = data[dt_field].isoformat()

    return data


def _find_referencing_repositories(policy_name: str) -> List[Repository]:
    """Find repositories that reference a cleanup policy by name.

    Scans all repositories and checks their ``cleanup_policies`` JSON
    array for the given policy name.  Used by the DELETE endpoint to
    detect policies that are still in use.

    Args:
        policy_name: The policy name to search for in repository assignments.

    Returns:
        A list of :class:`Repository` instances that reference the policy.
    """
    referencing: List[Repository] = []
    try:
        repositories = Repository.query.all()
        for repo in repositories:
            repo_policies = repo.cleanup_policies
            if repo_policies and isinstance(repo_policies, list):
                if policy_name in repo_policies:
                    referencing.append(repo)
    except Exception as exc:
        logger.warning(
            "Error checking repository references for policy '%s': %s",
            policy_name,
            str(exc),
        )
    return referencing


# ===========================================================================
# GET / — List All Cleanup Policies
# ===========================================================================


@cleanup_bp.route("/")
@cleanup_bp.response(200, CleanupPolicyResponseSchema(many=True))
@login_required
@require_permission("cleanup", "read")
def list_cleanup_policies() -> Any:
    """List all cleanup policies.

    Returns all configured cleanup policies with their criteria, format
    targeting, and computed property indicators.  Supports optional
    filtering by repository format via the ``format`` query parameter.

    **Query Parameters:**

    - ``format`` (str, optional): Filter results to only policies that
      apply to the specified format (including format-agnostic policies).

    **Responses:**

    - 200: List of cleanup policies (may be empty).
    - 401: Authentication required.
    - 403: Insufficient privileges (``cleanup:read``).
    """
    format_filter: Optional[str] = request.args.get("format")

    logger.debug(
        "Listing cleanup policies (format_filter=%s).",
        format_filter,
    )

    try:
        service: CleanupService = _get_cleanup_service()
        policies: List[CleanupPolicy] = service.list_policies(
            format_type=format_filter
        )
    except Exception as exc:
        logger.error(
            "Error listing cleanup policies: %s",
            str(exc),
            exc_info=True,
        )
        abort(500, message="Failed to list cleanup policies.")

    result: List[Dict[str, Any]] = [
        _serialize_policy(policy) for policy in policies
    ]

    logger.info(
        "Listed %d cleanup policy(ies)%s.",
        len(result),
        f" (format={format_filter})" if format_filter else "",
    )

    return jsonify(result)


# ===========================================================================
# GET /<policy_id> — Get a Specific Cleanup Policy
# ===========================================================================


@cleanup_bp.route("/<string:policy_id>")
@cleanup_bp.response(200, CleanupPolicyResponseSchema)
@login_required
@require_permission("cleanup", "read")
def get_cleanup_policy(policy_id: str) -> Any:
    """Get a specific cleanup policy by ID.

    Returns the full cleanup policy definition including criteria, format
    targeting, description, timestamps, and computed property indicators.

    **Path Parameters:**

    - ``policy_id`` (str): Unique cleanup policy identifier.

    **Responses:**

    - 200: Cleanup policy details.
    - 401: Authentication required.
    - 403: Insufficient privileges (``cleanup:read``).
    - 404: Cleanup policy not found.
    """
    logger.debug("Retrieving cleanup policy '%s'.", policy_id)

    try:
        service: CleanupService = _get_cleanup_service()
        policy: Optional[CleanupPolicy] = service.get_policy(policy_id)
    except Exception as exc:
        logger.error(
            "Error retrieving cleanup policy '%s': %s",
            policy_id,
            str(exc),
            exc_info=True,
        )
        abort(500, message="Failed to retrieve cleanup policy.")

    if policy is None:
        logger.info("Cleanup policy '%s' not found.", policy_id)
        abort(404, message=f"Cleanup policy '{policy_id}' not found.")

    logger.info("Retrieved cleanup policy '%s'.", policy_id)
    return jsonify(_serialize_policy(policy))


# ===========================================================================
# POST / — Create a New Cleanup Policy
# ===========================================================================


@cleanup_bp.route("/", methods=["POST"])
@cleanup_bp.arguments(CleanupPolicyCreateSchema, location="json")
@cleanup_bp.response(201, CleanupPolicyResponseSchema)
@login_required
@require_permission("cleanup", "create")
def create_cleanup_policy(payload: Dict[str, Any]) -> Any:
    """Create a new cleanup policy.

    Creates a cleanup policy with the specified name, optional format
    targeting, and criteria rules.  The policy ``policy_id`` is
    automatically derived from the policy name.

    **Request Body (JSON):**

    - ``name`` (str, required): Unique human-readable policy name.
    - ``format`` (str, optional): Target repository format (null = all).
    - ``criteria`` (dict, required): Cleanup criteria rules.
    - ``description`` (str, optional): Human-readable description.

    **Criteria Keys:**

    - ``lastDownloadedBefore`` (int): Days since last download.
    - ``lastBlobUpdatedBefore`` (int): Days since blob update.
    - ``regexPattern`` (str): Asset path regex pattern.
    - ``isPrerelease`` (bool): Match pre-release versions.

    **Responses:**

    - 201: Cleanup policy created successfully.
    - 400: Invalid request body or criteria structure.
    - 401: Authentication required.
    - 403: Insufficient privileges (``cleanup:create``).
    - 409: Policy with the same name already exists.
    """
    name: str = payload.get("name", "").strip()
    format_type: Optional[str] = payload.get("format")
    criteria: Dict[str, Any] = payload.get("criteria", {})
    description: Optional[str] = payload.get("description")

    if not name:
        abort(400, message="Policy name is required and must not be empty.")

    # Validate criteria at API level
    _validate_criteria_keys(criteria)

    # Convert None format to wildcard for CleanupService
    service_format: str = format_type if format_type is not None else "*"

    logger.debug(
        "Creating cleanup policy: name='%s', format='%s', criteria_keys=%s.",
        name,
        service_format,
        list(criteria.keys()),
    )

    try:
        service: CleanupService = _get_cleanup_service()
        policy: CleanupPolicy = service.create_policy(
            name=name,
            format_type=service_format,
            criteria=criteria,
            description=description,
        )
    except ValueError as exc:
        error_msg: str = str(exc)
        if "already exists" in error_msg.lower():
            logger.warning(
                "Conflict creating cleanup policy '%s': %s", name, error_msg
            )
            abort(409, message=error_msg)
        else:
            logger.warning(
                "Validation error creating cleanup policy '%s': %s",
                name,
                error_msg,
            )
            abort(400, message=error_msg)
    except Exception as exc:
        logger.error(
            "Unexpected error creating cleanup policy '%s': %s",
            name,
            str(exc),
            exc_info=True,
        )
        abort(500, message="Failed to create cleanup policy.")

    logger.info(
        "Created cleanup policy '%s' (policy_id='%s', format='%s').",
        name,
        policy.policy_id,
        format_type,
    )

    response = jsonify(_serialize_policy(policy))
    response.status_code = 201
    return response


# ===========================================================================
# PUT /<policy_id> — Update an Existing Cleanup Policy
# ===========================================================================


@cleanup_bp.route("/<string:policy_id>", methods=["PUT"])
@cleanup_bp.arguments(CleanupPolicyUpdateSchema, location="json")
@cleanup_bp.response(200, CleanupPolicyResponseSchema)
@login_required
@require_permission("cleanup", "update")
def update_cleanup_policy(payload: Dict[str, Any], policy_id: str) -> Any:
    """Update an existing cleanup policy.

    Updates the specified fields of a cleanup policy.  Only fields
    present in the request body are updated; omitted fields retain
    their current values.

    **Path Parameters:**

    - ``policy_id`` (str): Unique cleanup policy identifier.

    **Request Body (JSON, all optional):**

    - ``name`` (str): Updated policy name.
    - ``format`` (str | null): Updated target format.
    - ``criteria`` (dict): Updated cleanup criteria (replaces entirely).
    - ``description`` (str | null): Updated description.

    **Responses:**

    - 200: Cleanup policy updated successfully.
    - 400: Invalid request body or criteria structure.
    - 401: Authentication required.
    - 403: Insufficient privileges (``cleanup:update``).
    - 404: Cleanup policy not found.
    - 409: Updated name conflicts with an existing policy.
    """
    logger.debug(
        "Updating cleanup policy '%s' with fields: %s.",
        policy_id,
        list(payload.keys()),
    )

    # Validate criteria if provided
    criteria: Optional[Dict[str, Any]] = payload.get("criteria")
    if criteria is not None:
        _validate_criteria_keys(criteria)

    # Build kwargs for service.update_policy()
    update_kwargs: Dict[str, Any] = {}

    if payload.get("name") is not None:
        update_kwargs["name"] = payload["name"].strip()

    if "format" in payload:
        fmt_val = payload["format"]
        if fmt_val is not None:
            update_kwargs["format_type"] = fmt_val
        else:
            update_kwargs["format_type"] = "*"

    if criteria is not None:
        update_kwargs["criteria"] = criteria

    if "description" in payload:
        update_kwargs["description"] = payload["description"]

    if not update_kwargs:
        abort(400, message="No update fields provided.")

    try:
        service: CleanupService = _get_cleanup_service()
        policy: CleanupPolicy = service.update_policy(
            policy_id, **update_kwargs
        )
    except ValueError as exc:
        error_msg: str = str(exc)
        if "not found" in error_msg.lower():
            logger.info(
                "Cleanup policy '%s' not found for update.", policy_id
            )
            abort(404, message=error_msg)
        elif "already exists" in error_msg.lower():
            logger.warning(
                "Name conflict updating cleanup policy '%s': %s",
                policy_id,
                error_msg,
            )
            abort(409, message=error_msg)
        else:
            logger.warning(
                "Validation error updating cleanup policy '%s': %s",
                policy_id,
                error_msg,
            )
            abort(400, message=error_msg)
    except Exception as exc:
        logger.error(
            "Unexpected error updating cleanup policy '%s': %s",
            policy_id,
            str(exc),
            exc_info=True,
        )
        abort(500, message="Failed to update cleanup policy.")

    logger.info("Updated cleanup policy '%s'.", policy_id)
    return jsonify(_serialize_policy(policy))


# ===========================================================================
# DELETE /<policy_id> — Delete a Cleanup Policy
# ===========================================================================


@cleanup_bp.route("/<string:policy_id>", methods=["DELETE"])
@login_required
@require_permission("cleanup", "delete")
def delete_cleanup_policy(policy_id: str) -> Any:
    """Delete a cleanup policy.

    Removes a cleanup policy from the system.  Before deletion, checks
    whether any repositories reference this policy in their
    ``cleanup_policies`` JSON array.  If references exist, returns
    HTTP 409 Conflict to prevent orphaned references.

    **Path Parameters:**

    - ``policy_id`` (str): Unique cleanup policy identifier.

    **Responses:**

    - 204: Cleanup policy deleted successfully (no response body).
    - 401: Authentication required.
    - 403: Insufficient privileges (``cleanup:delete``).
    - 404: Cleanup policy not found.
    - 409: Policy is still assigned to one or more repositories.
    """
    logger.debug("Deleting cleanup policy '%s'.", policy_id)

    # First, verify the policy exists
    service: CleanupService = _get_cleanup_service()
    policy: Optional[CleanupPolicy] = service.get_policy(policy_id)

    if policy is None:
        logger.info("Cleanup policy '%s' not found for deletion.", policy_id)
        abort(404, message=f"Cleanup policy '{policy_id}' not found.")

    # Check for repository references before deletion
    referencing_repos: List[Repository] = _find_referencing_repositories(
        policy.name
    )
    if referencing_repos:
        repo_names: List[str] = [r.name for r in referencing_repos]
        logger.warning(
            "Cannot delete cleanup policy '%s': referenced by %d "
            "repository(ies): %s.",
            policy_id,
            len(repo_names),
            repo_names,
        )
        abort(
            409,
            message=(
                f"Cleanup policy '{policy_id}' is still assigned to "
                f"{len(repo_names)} repository(ies): "
                f"{', '.join(repo_names)}. Remove the policy from all "
                f"repositories before deleting."
            ),
        )

    # Perform deletion
    try:
        deleted: bool = service.delete_policy(policy_id)
        if not deleted:
            abort(
                404,
                message=f"Cleanup policy '{policy_id}' not found.",
            )
    except Exception as exc:
        logger.error(
            "Unexpected error deleting cleanup policy '%s': %s",
            policy_id,
            str(exc),
            exc_info=True,
        )
        abort(500, message="Failed to delete cleanup policy.")

    logger.info("Deleted cleanup policy '%s'.", policy_id)
    return "", 204


# ===========================================================================
# POST /<policy_id>/preview — Dry-Run Cleanup Preview
# ===========================================================================


@cleanup_bp.route("/<string:policy_id>/preview", methods=["POST"])
@cleanup_bp.response(200, CleanupPreviewResponseSchema)
@login_required
@require_permission("cleanup", "read")
def preview_cleanup(policy_id: str) -> Any:
    """Preview cleanup impact without performing deletions (dry-run).

    Evaluates the specified cleanup policy against the given repository
    and returns a summary of assets and components that **would** be
    deleted, along with the estimated storage space to be reclaimed.
    No actual deletions are performed.

    Useful for administrators to verify a cleanup policy's impact before
    enabling it or running a manual cleanup.

    **Path Parameters:**

    - ``policy_id`` (str): Unique cleanup policy identifier.

    **Request Body or Query Parameters:**

    - ``repository`` (str, required): Name of the repository to preview
      cleanup against.

    **Responses:**

    - 200: Preview results with matched assets, components, and size.
    - 400: Missing or invalid repository parameter.
    - 401: Authentication required.
    - 403: Insufficient privileges (``cleanup:read``).
    - 404: Cleanup policy or repository not found.
    """
    # Accept repository from either JSON body or query parameter
    repository_name: Optional[str] = None

    body: Optional[Dict[str, Any]] = request.get_json(silent=True)
    if body and "repository" in body:
        repository_name = body["repository"]

    if not repository_name:
        repository_name = request.args.get("repository")

    if not repository_name:
        abort(
            400,
            message=(
                "The 'repository' parameter is required for cleanup preview. "
                "Provide it as a JSON body field or query parameter."
            ),
        )

    logger.debug(
        "Previewing cleanup for policy '%s' on repository '%s'.",
        policy_id,
        repository_name,
    )

    try:
        service: CleanupService = _get_cleanup_service()
        preview_result: Dict[str, Any] = service.preview_cleanup(
            policy_id=policy_id,
            repository_name=repository_name,
        )
    except ValueError as exc:
        error_msg: str = str(exc)
        if "not found" in error_msg.lower():
            logger.info(
                "Preview failed — resource not found: %s", error_msg
            )
            abort(404, message=error_msg)
        else:
            logger.warning(
                "Preview validation error for policy '%s': %s",
                policy_id,
                error_msg,
            )
            abort(400, message=error_msg)
    except Exception as exc:
        logger.error(
            "Unexpected error previewing cleanup for policy '%s' on "
            "repository '%s': %s",
            policy_id,
            repository_name,
            str(exc),
            exc_info=True,
        )
        abort(500, message="Failed to preview cleanup.")

    # Add timestamp to preview result
    preview_result["preview_timestamp"] = (
        datetime.now(timezone.utc).isoformat()
    )

    logger.info(
        "Preview cleanup for policy '%s' on repo '%s': "
        "%d assets, %d components, %d bytes.",
        policy_id,
        repository_name,
        preview_result.get("matched_assets", 0),
        preview_result.get("matched_components", 0),
        preview_result.get("total_size", 0),
    )

    return jsonify(preview_result)


# ===========================================================================
# POST /run — Trigger Cleanup Execution
# ===========================================================================


@cleanup_bp.route("/run", methods=["POST"])
@cleanup_bp.response(202, CleanupRunResponseSchema)
@login_required
@require_permission("cleanup", "run")
def run_cleanup() -> Any:
    """Trigger cleanup evaluation and execution.

    Initiates cleanup evaluation for all active policies.  When a
    ``repository`` query parameter is provided, cleanup is limited to
    that specific repository; otherwise, cleanup runs across all
    repositories with assigned policies.

    Cleanup execution is asynchronous — this endpoint returns HTTP 202
    Accepted immediately and delegates the actual cleanup work to the
    APScheduler background task (Feature F-402).

    **Query Parameters:**

    - ``repository`` (str, optional): Limit cleanup to a specific
      repository name.

    **Request Body (JSON, optional):**

    - ``repository`` (str, optional): Same as the query parameter
      (body takes precedence over query param).

    **Responses:**

    - 202: Cleanup run accepted for asynchronous execution.
    - 401: Authentication required.
    - 403: Insufficient privileges (``cleanup:run``).
    - 404: Specified repository not found.
    """
    # Accept repository from either JSON body or query parameter
    repository_name: Optional[str] = None

    body: Optional[Dict[str, Any]] = request.get_json(silent=True)
    if body and "repository" in body:
        repository_name = body["repository"]

    if not repository_name:
        repository_name = request.args.get("repository")

    accepted_at: str = datetime.now(timezone.utc).isoformat()

    logger.debug(
        "Cleanup run requested (repository=%s).",
        repository_name or "all",
    )

    service: CleanupService = _get_cleanup_service()

    if repository_name:
        # Validate repository exists
        repo: Optional[Repository] = Repository.query.filter_by(
            name=repository_name
        ).first()
        if repo is None:
            logger.info(
                "Cleanup run failed: repository '%s' not found.",
                repository_name,
            )
            abort(
                404,
                message=f"Repository '{repository_name}' not found.",
            )

        # Schedule repository-specific cleanup via the scheduler
        try:
            from src.app.extensions import scheduler

            scheduler.add_job(
                func=service.execute_cleanup,
                args=[repository_name],
                id=f"cleanup_run_{repository_name}_{accepted_at}",
                name=f"Manual cleanup: {repository_name}",
                trigger="date",
                replace_existing=False,
                misfire_grace_time=120,
            )
            logger.info(
                "Scheduled cleanup for repository '%s'.",
                repository_name,
            )
        except Exception as exc:
            # If scheduling fails, attempt synchronous execution as fallback
            logger.warning(
                "Failed to schedule cleanup for repo '%s' via APScheduler "
                "(%s). Attempting synchronous execution.",
                repository_name,
                str(exc),
            )
            try:
                service.execute_cleanup(repository_name)
            except Exception as exec_exc:
                logger.error(
                    "Failed to execute cleanup for repo '%s': %s",
                    repository_name,
                    str(exec_exc),
                    exc_info=True,
                )
                abort(500, message="Failed to trigger cleanup execution.")
    else:
        # Schedule system-wide cleanup across all repositories
        try:
            from src.app.extensions import scheduler

            scheduler.add_job(
                func=service.run_all_repository_cleanups,
                id=f"cleanup_run_all_{accepted_at}",
                name="Manual cleanup: all repositories",
                trigger="date",
                replace_existing=False,
                misfire_grace_time=120,
            )
            logger.info("Scheduled system-wide cleanup run.")
        except Exception as exc:
            logger.warning(
                "Failed to schedule system-wide cleanup via APScheduler "
                "(%s). Attempting synchronous execution.",
                str(exc),
            )
            try:
                service.run_all_repository_cleanups()
            except Exception as exec_exc:
                logger.error(
                    "Failed to execute system-wide cleanup: %s",
                    str(exec_exc),
                    exc_info=True,
                )
                abort(500, message="Failed to trigger cleanup execution.")

    response_data: Dict[str, Any] = {
        "message": "Cleanup run accepted for asynchronous execution.",
        "repository": repository_name or "all",
        "accepted_at": accepted_at,
        "status": "accepted",
    }

    logger.info(
        "Cleanup run accepted: repository=%s, accepted_at=%s.",
        repository_name or "all",
        accepted_at,
    )

    response = jsonify(response_data)
    response.status_code = 202
    return response


# ===========================================================================
# Error Handlers for the Blueprint
# ===========================================================================


def _extract_blueprint_error_message(error: Any, default: str) -> str:
    """Extract a human-readable error message from an HTTPException.

    flask-smorest's ``abort()`` stores extra data in ``error.data`` (a dict
    with a ``'message'`` key).  Standard Flask ``abort()`` stores the message
    in ``error.description``.  This helper handles both cases so that custom
    messages are always surfaced in JSON error responses.

    Args:
        error: The caught exception (typically a Werkzeug HTTPException).
        default: Fallback message if extraction fails.

    Returns:
        The extracted error message string.
    """
    data = getattr(error, "data", None)
    if isinstance(data, dict) and "message" in data:
        return str(data["message"])
    desc = getattr(error, "description", None)
    if desc:
        return str(desc)
    return default


@cleanup_bp.errorhandler(404)
def handle_not_found(error: Any) -> Any:
    """Handle 404 Not Found errors within the cleanup blueprint.

    Provides a consistent JSON error response format for missing resources.

    Args:
        error: The error object from Flask.

    Returns:
        JSON error response with HTTP 404 status.
    """
    message: str = _extract_blueprint_error_message(error, "Resource not found.")
    return jsonify({"error": {"code": 404, "message": message}}), 404


@cleanup_bp.errorhandler(400)
def handle_bad_request(error: Any) -> Any:
    """Handle 400 Bad Request errors within the cleanup blueprint.

    Provides a consistent JSON error response format for validation errors.

    Args:
        error: The error object from Flask.

    Returns:
        JSON error response with HTTP 400 status.
    """
    message: str = _extract_blueprint_error_message(error, "Bad request.")
    return jsonify({"error": {"code": 400, "message": message}}), 400


@cleanup_bp.errorhandler(409)
def handle_conflict(error: Any) -> Any:
    """Handle 409 Conflict errors within the cleanup blueprint.

    Provides a consistent JSON error response format for resource conflicts.

    Args:
        error: The error object from Flask.

    Returns:
        JSON error response with HTTP 409 status.
    """
    message: str = _extract_blueprint_error_message(error, "Resource conflict.")
    return jsonify({"error": {"code": 409, "message": message}}), 409


# ===========================================================================
# Module Load Logging
# ===========================================================================

logger.debug(
    "Cleanup API blueprint module loaded — url_prefix=%s",
    cleanup_bp.url_prefix,
)
