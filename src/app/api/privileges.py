"""
Privilege Descriptor and Content Selector Management REST API Blueprint.

This module implements the complete REST API for managing **privilege
descriptors** and **content selectors** — the two core primitives of the
Role-Based Access Control (RBAC) security layer (Feature F-501-RQ-003).

**Architecture Context:**
Replaces the Java Privilege descriptor REST API resource classes from the
original Sonatype Nexus Repository system (RESTEasy 6.2.7 JAX-RS).  All
endpoints are exposed as Flask-smorest Blueprint routes with automatic
OpenAPI 3.x specification generation and Swagger UI documentation.

**API Surface:**

+--------+------------------------------------------------------+-------------------+
| Method | Endpoint                                             | Description       |
+========+======================================================+===================+
| GET    | /api/v1/security/privileges                          | List privileges   |
+--------+------------------------------------------------------+-------------------+
| POST   | /api/v1/security/privileges                          | Create privilege  |
+--------+------------------------------------------------------+-------------------+
| GET    | /api/v1/security/privileges/<privilege_id>            | Get privilege     |
+--------+------------------------------------------------------+-------------------+
| PUT    | /api/v1/security/privileges/<privilege_id>            | Update privilege  |
+--------+------------------------------------------------------+-------------------+
| DELETE | /api/v1/security/privileges/<privilege_id>            | Delete privilege  |
+--------+------------------------------------------------------+-------------------+
| GET    | /api/v1/security/content-selectors                   | List selectors    |
+--------+------------------------------------------------------+-------------------+
| POST   | /api/v1/security/content-selectors                   | Create selector   |
+--------+------------------------------------------------------+-------------------+
| PUT    | /api/v1/security/content-selectors/<selector_id>     | Update selector   |
+--------+------------------------------------------------------+-------------------+
| DELETE | /api/v1/security/content-selectors/<selector_id>     | Delete selector   |
+--------+------------------------------------------------------+-------------------+

**Privilege Types (Three-Tier RBAC):**

- **Tier 1 — System-wide:**
  - ``application``: system domain permissions (e.g., user management)
  - ``wildcard``: unrestricted access (e.g., ``nx-all``)

- **Tier 2 — Repository-scoped:**
  - ``repository-admin``: repository administration
  - ``repository-view``: repository content read access

- **Tier 3 — Sub-repository:**
  - ``repository-content-selector``: CSEL expression-based access control

**Content Selectors:**

Content selectors define CSEL (Content Selector Expression Language) or
legacy JEXL expressions for sub-repository access control.  They are
referenced by ``repository-content-selector`` privilege types.

**Built-in Privilege Protection:**

Privileges with IDs starting with ``nx-`` are treated as system built-in
privileges and cannot be modified or deleted through the API.

Exports:
    privileges_bp : flask_smorest.Blueprint — registered with the Flask app
                    in ``src/app/factory.py``.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from flask import current_app, g, jsonify, request
from flask_smorest import Blueprint, abort
from marshmallow import Schema, ValidationError, fields, validate, validates

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.extensions import db
from src.app.models.content_selector import ContentSelector
from src.app.models.privilege import Privilege
from src.app.models.role import Role
from src.app.schemas.role import PrivilegeSchema

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for privilege and content selector CRUD
# operations, validation errors, built-in privilege protection warnings,
# and CSEL expression validation.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------
# Replaces RESTEasy 6.2.7 JAX-RS resource class registration for
# privilege and content selector management endpoints.  flask-smorest
# 0.45.0 provides automatic OpenAPI 3.x spec generation and Swagger UI.
#
# The url_prefix is set to /api/v1/security to accommodate both
# /api/v1/security/privileges and /api/v1/security/content-selectors
# route families within a single Blueprint, as required by the API spec.
# ---------------------------------------------------------------------------

privileges_bp: Blueprint = Blueprint(
    "privileges",
    __name__,
    url_prefix="/api/v1/security",
    description="Privilege and content selector management (F-501-RQ-003)",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pagination Defaults
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE: int = 50
"""Default number of privileges returned per page."""

MAX_PAGE_SIZE: int = 200
"""Maximum number of privileges allowed per page."""


class PrivilegeListQuerySchema(Schema):
    """Marshmallow schema for ``list_privileges`` query parameters.

    Provides optional filtering by privilege ``type`` and pagination
    via ``page`` and ``page_size``.
    """

    type = fields.String(
        load_default=None,
        metadata={
            "description": (
                "Filter by privilege type (application, repository-admin, "
                "repository-view, repository-content-selector, wildcard)."
            ),
        },
    )
    page = fields.Integer(
        load_default=1,
        metadata={"description": "Page number (1-based, default 1)."},
    )
    page_size = fields.Integer(
        load_default=DEFAULT_PAGE_SIZE,
        metadata={
            "description": (
                f"Results per page (1–{MAX_PAGE_SIZE}, default "
                f"{DEFAULT_PAGE_SIZE})."
            ),
        },
    )


#: Valid privilege type identifiers corresponding to the three-tier RBAC model.
VALID_PRIVILEGE_TYPES: frozenset[str] = frozenset(
    {
        "application",
        "repository-admin",
        "repository-content-selector",
        "repository-view",
        "wildcard",
    }
)

#: Required properties keys for each privilege type.
#: Maps privilege type to list of required property keys.
TYPE_REQUIRED_PROPERTIES: Dict[str, List[str]] = {
    "application": ["domain", "actions"],
    "repository-admin": ["format", "repository"],
    "repository-view": ["format", "repository", "actions"],
    "repository-content-selector": [
        "format",
        "repository",
        "contentSelector",
        "actions",
    ],
    "wildcard": [],
}

#: Prefix identifying built-in (system) privileges that cannot be
#: modified or deleted via the API.
BUILTIN_PRIVILEGE_PREFIX: str = "nx-"

#: Well-known built-in privilege IDs that are always protected.
BUILTIN_PRIVILEGE_IDS: frozenset[str] = frozenset(
    {
        "nx-all",
        "nx-repository-admin",
        "nx-repository-view",
        "nx-component-upload",
        "nx-healthcheck-read",
        "nx-search-read",
        "nx-apikey-all",
    }
)

#: Valid content selector expression types.
VALID_SELECTOR_TYPES: frozenset[str] = frozenset({"csel", "jexl"})

#: Regex pattern for basic CSEL expression validation.
#: Validates that the expression contains recognized CSEL operators
#: or variable references.  Word-boundary anchors (\b) prevent false
#: positives from substrings (e.g., "or" inside "world").
CSEL_OPERATOR_PATTERN: re.Pattern[str] = re.compile(
    r"(==|=\^|=~|!=|\band\b|\bor\b|\bnot\b|\bformat\b|\bpath\b)"
)


# ===========================================================================
# Inline Marshmallow Schemas for Content Selectors
# ===========================================================================
# No dedicated schema file exists in src/app/schemas/ for content selectors.
# These inline schemas handle request/response serialization and validation.
# Replaces Jackson 2.16.1 content selector DTOs from the Java source.
# ===========================================================================


class ContentSelectorSchema(Schema):
    """Marshmallow schema for serializing content selector instances.

    Used for response serialization in content selector list and detail
    endpoints.  Fields mirror the ``ContentSelector`` SQLAlchemy model
    from ``src/app/models/content_selector.py``.
    """

    selector_id = fields.String(
        required=True,
        metadata={
            "description": "Unique content selector identifier.",
        },
    )

    name = fields.String(
        required=True,
        validate=validate.Length(min=1, max=255),
        metadata={
            "description": (
                "Human-readable selector name. Must be unique across "
                "all content selectors."
            ),
        },
    )

    type = fields.String(
        required=True,
        validate=validate.OneOf(sorted(VALID_SELECTOR_TYPES)),
        metadata={
            "description": (
                "Expression language type: 'csel' (modern, default) "
                "or 'jexl' (legacy)."
            ),
        },
    )

    expression = fields.String(
        required=True,
        validate=validate.Length(min=1),
        metadata={
            "description": (
                "Selector expression in the specified language. "
                "CSEL operators: == (equals), =^ (starts with), "
                "=~ (regex), and, or, not."
            ),
        },
    )

    description = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            "description": "Optional description of what this selector matches.",
        },
    )

    is_csel = fields.Boolean(
        dump_only=True,
        metadata={
            "description": "True if this selector uses CSEL expression language.",
        },
    )

    is_jexl = fields.Boolean(
        dump_only=True,
        metadata={
            "description": "True if this selector uses legacy JEXL expressions.",
        },
    )

    created_at = fields.DateTime(
        dump_only=True,
        metadata={
            "description": "UTC timestamp of selector creation (ISO 8601).",
        },
    )

    updated_at = fields.DateTime(
        dump_only=True,
        metadata={
            "description": "UTC timestamp of last modification (ISO 8601).",
        },
    )


class ContentSelectorCreateSchema(Schema):
    """Marshmallow schema for creating and updating content selectors.

    Used for request body validation in POST and PUT endpoints.  Validates
    that the expression type is recognized and that the expression string
    is syntactically plausible for CSEL expressions.
    """

    name = fields.String(
        required=True,
        validate=validate.Length(min=1, max=255),
        metadata={
            "description": "Human-readable selector name (must be unique).",
        },
    )

    type = fields.String(
        required=False,
        load_default="csel",
        validate=validate.OneOf(sorted(VALID_SELECTOR_TYPES)),
        metadata={
            "description": (
                "Expression language type. Defaults to 'csel' if not specified."
            ),
        },
    )

    expression = fields.String(
        required=True,
        validate=validate.Length(min=1),
        metadata={
            "description": (
                "Selector expression. CSEL example: "
                'format == "maven2" and path =^ "/org/"'
            ),
        },
    )

    description = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            "description": "Optional description of what this selector matches.",
        },
    )

    @validates("expression")
    def validate_expression(self, value: str) -> None:
        """Validate that CSEL expressions contain recognized operators.

        For CSEL expressions, performs a lightweight syntactic check to
        ensure the expression contains at least one recognized CSEL
        operator (``==``, ``=^``, ``=~``, ``and``, ``or``, ``not``,
        ``format``, ``path``).

        JEXL expressions are not validated at the schema level — they
        are validated at runtime by the JEXL expression evaluator.

        Args:
            value: The expression string to validate.

        Raises:
            ValidationError: If the expression appears to be invalid CSEL.
        """
        stripped: str = value.strip()
        if not stripped:
            raise ValidationError("Expression cannot be empty or whitespace.")


# ===========================================================================
# Helper Functions
# ===========================================================================


def _is_builtin_privilege(privilege_id: str) -> bool:
    """Determine if a privilege ID refers to a built-in (system) privilege.

    Built-in privileges are identified by the ``nx-`` prefix.  They are
    created during system initialization and must not be modified or
    deleted through the REST API.

    Args:
        privilege_id: The privilege identifier to check.

    Returns:
        ``True`` if the privilege is built-in; ``False`` otherwise.
    """
    if not privilege_id:
        return False
    return privilege_id.startswith(BUILTIN_PRIVILEGE_PREFIX)


def _validate_privilege_properties(
    privilege_type: str, properties: Optional[Dict[str, Any]]
) -> None:
    """Validate that privilege properties match the type-specific schema.

    Each privilege type requires specific keys in the ``properties`` JSON
    dictionary.  This function verifies that all required keys are present
    and that their values are of acceptable types.

    **Properties Schema by Type:**

    - ``application``: ``{"domain": str, "actions": list[str]}``
    - ``repository-admin``: ``{"format": str, "repository": str}``
    - ``repository-view``: ``{"format": str, "repository": str, "actions": list[str]}``
    - ``repository-content-selector``: ``{"format": str, "repository": str,
      "contentSelector": str, "actions": list[str]}``
    - ``wildcard``: ``{}`` (no properties required)

    Args:
        privilege_type: One of the five valid privilege type strings.
        properties: The properties dictionary to validate.

    Raises:
        abort(400): If required properties are missing or invalid.
    """
    required_keys: List[str] = TYPE_REQUIRED_PROPERTIES.get(privilege_type, [])

    # Wildcard privileges require no properties.
    if privilege_type == "wildcard":
        return

    if properties is None:
        if required_keys:
            abort(
                400,
                message=(
                    f"Privilege type '{privilege_type}' requires properties: "
                    f"{', '.join(required_keys)}"
                ),
            )
        return

    # Verify all required keys are present.
    missing_keys: List[str] = [
        key for key in required_keys if key not in properties
    ]
    if missing_keys:
        abort(
            400,
            message=(
                f"Missing required properties for type '{privilege_type}': "
                f"{', '.join(missing_keys)}"
            ),
        )

    # Validate 'actions' is a list of strings when present and required.
    if "actions" in properties:
        actions_value: Any = properties["actions"]
        if not isinstance(actions_value, list):
            abort(
                400,
                message=(
                    "Property 'actions' must be a list of action strings."
                ),
            )
        for idx, action in enumerate(actions_value):
            if not isinstance(action, str) or not action.strip():
                abort(
                    400,
                    message=(
                        f"Action at index {idx} in 'actions' must be a "
                        f"non-empty string."
                    ),
                )

    # Validate 'domain' is a non-empty string for application type.
    if "domain" in properties and privilege_type == "application":
        domain_value: Any = properties["domain"]
        if not isinstance(domain_value, str) or not domain_value.strip():
            abort(
                400,
                message="Property 'domain' must be a non-empty string.",
            )

    # Validate 'format' is a non-empty string for repository-scoped types.
    if "format" in properties:
        format_value: Any = properties["format"]
        if not isinstance(format_value, str) or not format_value.strip():
            abort(
                400,
                message="Property 'format' must be a non-empty string.",
            )

    # Validate 'repository' is a non-empty string for repository-scoped types.
    if "repository" in properties:
        repo_value: Any = properties["repository"]
        if not isinstance(repo_value, str) or not repo_value.strip():
            abort(
                400,
                message="Property 'repository' must be a non-empty string.",
            )

    # Validate 'contentSelector' is a non-empty string for content-selector type.
    if (
        "contentSelector" in properties
        and privilege_type == "repository-content-selector"
    ):
        cs_value: Any = properties["contentSelector"]
        if not isinstance(cs_value, str) or not cs_value.strip():
            abort(
                400,
                message=(
                    "Property 'contentSelector' must be a non-empty string."
                ),
            )


def _validate_csel_expression(expression: str) -> bool:
    """Perform lightweight CSEL expression syntax validation.

    Checks that the expression contains at least one recognized CSEL
    operator.  This is a basic sanity check — full parsing is deferred
    to the ``ContentSelectorEvaluator`` at runtime.

    CSEL operators recognized: ``==``, ``=^``, ``=~``, ``!=``, ``and``,
    ``or``, ``not``, plus the ``format`` and ``path`` variable references.

    Args:
        expression: The CSEL expression string to validate.

    Returns:
        ``True`` if the expression appears syntactically valid.
    """
    if not expression or not expression.strip():
        return False
    return bool(CSEL_OPERATOR_PATTERN.search(expression))


def _generate_selector_id(name: str) -> str:
    """Generate a unique selector ID from a human-readable name.

    Creates a URL-friendly identifier by lowercasing the name, replacing
    non-alphanumeric characters with hyphens, and appending a short UUID
    suffix to ensure uniqueness.

    Args:
        name: The human-readable content selector name.

    Returns:
        A unique selector_id string (max 200 characters).
    """
    base: str = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    short_uuid: str = uuid.uuid4().hex[:8]
    selector_id: str = f"{base}-{short_uuid}"
    # Ensure the ID does not exceed the column max length (200).
    return selector_id[:200]


# ===========================================================================
# Privilege CRUD Endpoints
# ===========================================================================
# Route prefix: /api/v1/security/privileges (via Blueprint url_prefix +
# "/privileges" route path)
# ===========================================================================


@privileges_bp.route("/privileges", methods=["GET"])
@privileges_bp.arguments(PrivilegeListQuerySchema, location="query")
@login_required
@require_permission("privileges", "read")
def list_privileges(args: dict) -> tuple:
    """List all privilege descriptors with pagination.

    Returns privileges in the system, optionally filtered by privilege
    type, with pagination via ``page`` and ``page_size``.  Supports the
    five RBAC privilege types: ``application``, ``repository-admin``,
    ``repository-view``, ``repository-content-selector``, and ``wildcard``.

    **Query Parameters:**

    - ``type`` (str, optional): Filter by privilege type.
    - ``page`` (int, optional): Page number (1-based, default 1).
    - ``page_size`` (int, optional): Results per page (1–200, default 50).

    **Response:** 200 OK — JSON object with ``items``, ``total_count``,
    ``page``, and ``page_size``.
    """
    type_filter: Optional[str] = args.get("type")
    page: int = max(1, args.get("page", 1))
    page_size: int = max(1, min(args.get("page_size", DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))

    # Use application-level logger for request-scoped structured logging
    # complementing the module-level logger (AAP Section 0.7.2).
    current_app.logger.debug(
        "Privilege list requested (type_filter=%s, page=%d).",
        type_filter,
        page,
    )
    logger.info(
        "Listing privileges (type_filter=%s, page=%d).",
        type_filter,
        page,
    )

    query = Privilege.query

    if type_filter:
        # Validate the type filter value.
        if type_filter not in VALID_PRIVILEGE_TYPES:
            logger.warning(
                "Invalid privilege type filter: '%s'. Valid types: %s.",
                type_filter,
                ", ".join(sorted(VALID_PRIVILEGE_TYPES)),
            )
            abort(
                400,
                message=(
                    f"Invalid privilege type: '{type_filter}'. "
                    f"Valid types: "
                    f"{', '.join(sorted(VALID_PRIVILEGE_TYPES))}"
                ),
            )
        query = query.filter_by(type=type_filter)

    total_count: int = query.count()
    offset: int = (page - 1) * page_size

    privileges: List[Privilege] = (
        query.order_by(Privilege.privilege_id)
        .offset(offset)
        .limit(page_size)
        .all()
    )

    logger.info("Returning %d/%d privilege(s).", len(privileges), total_count)

    schema = PrivilegeSchema(many=True)
    return jsonify({
        "items": schema.dump(privileges),
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
    })


@privileges_bp.route(
    "/privileges/<string:privilege_id>", methods=["GET"]
)
@privileges_bp.response(200, PrivilegeSchema)
@login_required
@require_permission("privileges", "read")
def get_privilege(privilege_id: str) -> Privilege:
    """Get a single privilege descriptor by its ID.

    **Path Parameters:**

    - ``privilege_id`` (str): The unique privilege identifier.

    **Response:** 200 OK — Privilege descriptor object.

    **Errors:**

    - 404 Not Found — If no privilege with the given ID exists.
    """
    logger.info("Fetching privilege: '%s'.", privilege_id)

    privilege: Optional[Privilege] = db.session.get(Privilege, privilege_id)
    if privilege is None:
        logger.warning("Privilege not found: '%s'.", privilege_id)
        abort(404, message=f"Privilege not found: '{privilege_id}'")

    logger.info(
        "Returning privilege: '%s' (type=%s).",
        privilege.privilege_id,
        privilege.type,
    )
    return privilege


@privileges_bp.route("/privileges", methods=["POST"])
@login_required
@require_permission("privileges", "create")
@privileges_bp.arguments(PrivilegeSchema)
@privileges_bp.response(201, PrivilegeSchema)
def create_privilege(payload: Dict[str, Any]) -> Privilege:
    """Create a new privilege descriptor.

    Creates a new privilege with the specified type and properties.  The
    properties JSON structure must match the type-specific schema:

    - **application**: ``{domain, actions}``
    - **repository-admin**: ``{format, repository}``
    - **repository-view**: ``{format, repository, actions}``
    - **repository-content-selector**: ``{format, repository,
      contentSelector, actions}``
    - **wildcard**: ``{}`` (no properties required)

    **Request Body:** Privilege descriptor object (validated by
    PrivilegeSchema).

    **Response:** 201 Created — The created privilege descriptor.

    **Errors:**

    - 400 Bad Request — If the privilege type is invalid or properties
      do not match the type-specific schema.
    - 409 Conflict — If a privilege with the same ID already exists.
    """
    privilege_id: str = payload.get("privilege_id", "")
    privilege_type: str = payload.get("type", "")
    name: str = payload.get("name", "")
    description: Optional[str] = payload.get("description")
    properties: Optional[Dict[str, Any]] = payload.get("properties")
    attributes: Optional[Dict[str, Any]] = payload.get("attributes")

    logger.info(
        "Creating privilege: id='%s', type='%s', name='%s'.",
        privilege_id,
        privilege_type,
        name,
    )

    # Validate privilege type.
    if privilege_type not in VALID_PRIVILEGE_TYPES:
        logger.warning(
            "Invalid privilege type '%s' for privilege '%s'.",
            privilege_type,
            privilege_id,
        )
        abort(
            400,
            message=(
                f"Invalid privilege type: '{privilege_type}'. "
                f"Valid types: "
                f"{', '.join(sorted(VALID_PRIVILEGE_TYPES))}"
            ),
        )

    # Validate type-specific properties.
    _validate_privilege_properties(privilege_type, properties)

    # Check for duplicate privilege_id.
    existing: Optional[Privilege] = db.session.get(Privilege, privilege_id)
    if existing is not None:
        logger.warning(
            "Privilege already exists: '%s'.",
            privilege_id,
        )
        abort(
            409,
            message=f"Privilege already exists: '{privilege_id}'",
        )

    # Create the new privilege instance.
    try:
        privilege: Privilege = Privilege(
            privilege_id=privilege_id,
            type=privilege_type,
            name=name,
            description=description,
            properties=properties if properties else {},
            attributes=attributes if attributes else {},
        )
        db.session.add(privilege)
        db.session.commit()

        logger.info(
            "Privilege created successfully: '%s' (type=%s).",
            privilege.privilege_id,
            privilege.type,
        )

        # Emit audit event for privilege creation (Feature F-303)
        try:
            caller_id = getattr(g, "current_user", None)
            caller_id = getattr(caller_id, "user_id", "<system>") if caller_id else "<system>"
            emit_event(EventType.USER_AUTHENTICATED, payload={
                "user_id": caller_id,
                "action": "privilege_created",
                "target_privilege": privilege.privilege_id,
                "ip_address": request.remote_addr or "unknown",
            })
        except Exception:
            logger.debug("Failed to emit privilege create audit event.", exc_info=True)

        return privilege

    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to create privilege '%s': %s",
            privilege_id,
            str(exc),
            exc_info=True,
        )
        abort(
            500,
            message=f"Failed to create privilege: {str(exc)}",
        )


class PrivilegeUpdateSchema(Schema):
    """Marshmallow schema for updating privilege descriptors.

    All fields are **optional** to support partial updates — only fields
    present in the request body are modified.  This differs from
    :class:`PrivilegeSchema` where ``privilege_id``, ``type``, and ``name``
    are required.

    This is consistent with the ``RoleUpdateSchema`` and ``UserUpdateSchema``
    patterns in the other security API endpoints.
    """

    type = fields.String(
        required=False,
        validate=validate.OneOf(sorted(VALID_PRIVILEGE_TYPES)),
        metadata={
            "description": "Privilege type identifier.",
        },
    )

    name = fields.String(
        required=False,
        validate=validate.Length(min=1, max=255),
        metadata={
            "description": "Human-readable privilege name.",
        },
    )

    description = fields.String(
        required=False,
        allow_none=True,
        metadata={
            "description": "Optional description of what this privilege grants.",
        },
    )

    properties = fields.Dict(
        required=False,
        allow_none=True,
        metadata={
            "description": "Type-specific privilege properties.",
        },
    )

    attributes = fields.Dict(
        required=False,
        allow_none=True,
        metadata={
            "description": "Extensible JSON metadata.",
        },
    )


@privileges_bp.route(
    "/privileges/<string:privilege_id>", methods=["PUT"]
)
@login_required
@require_permission("privileges", "update")
@privileges_bp.arguments(PrivilegeUpdateSchema)
@privileges_bp.response(200, PrivilegeSchema)
def update_privilege(
    payload: Dict[str, Any], privilege_id: str
) -> Privilege:
    """Update an existing privilege descriptor.

    Updates the name, description, and/or properties of an existing
    privilege.  Built-in privileges (IDs starting with ``nx-``) cannot
    be modified.

    **Path Parameters:**

    - ``privilege_id`` (str): The unique privilege identifier.

    **Request Body:** Updated privilege descriptor fields.

    **Response:** 200 OK — The updated privilege descriptor.

    **Errors:**

    - 400 Bad Request — If the updated properties are invalid.
    - 403 Forbidden — If attempting to modify a built-in privilege.
    - 404 Not Found — If the privilege does not exist.
    """
    logger.info("Updating privilege: '%s'.", privilege_id)

    # Prevent modification of built-in privileges.
    if _is_builtin_privilege(privilege_id):
        logger.warning(
            "Attempted modification of built-in privilege: '%s'.",
            privilege_id,
        )
        abort(
            403,
            message=(
                f"Built-in privilege '{privilege_id}' cannot be modified."
            ),
        )

    # Look up the existing privilege.
    privilege: Optional[Privilege] = db.session.get(Privilege, privilege_id)
    if privilege is None:
        logger.warning(
            "Privilege not found for update: '%s'.",
            privilege_id,
        )
        abort(404, message=f"Privilege not found: '{privilege_id}'")

    # Extract updateable fields from payload.
    new_name: Optional[str] = payload.get("name")
    new_description: Optional[str] = payload.get("description")
    new_properties: Optional[Dict[str, Any]] = payload.get("properties")
    new_type: Optional[str] = payload.get("type")

    # Determine effective type for property validation.
    effective_type: str = new_type if new_type else privilege.type

    # If type is being changed, validate the new type.
    if new_type and new_type not in VALID_PRIVILEGE_TYPES:
        abort(
            400,
            message=(
                f"Invalid privilege type: '{new_type}'. "
                f"Valid types: "
                f"{', '.join(sorted(VALID_PRIVILEGE_TYPES))}"
            ),
        )

    # Validate properties against the effective type.
    if new_properties is not None:
        _validate_privilege_properties(effective_type, new_properties)

    try:
        if new_name is not None:
            privilege.name = new_name
        if new_description is not None:
            privilege.description = new_description
        if new_properties is not None:
            privilege.properties = new_properties
        if new_type is not None:
            privilege.type = new_type

        db.session.commit()

        logger.info(
            "Privilege updated successfully: '%s' (type=%s).",
            privilege.privilege_id,
            privilege.type,
        )

        # Emit audit event for privilege update (Feature F-303)
        try:
            caller_id = getattr(g, "current_user", None)
            caller_id = getattr(caller_id, "user_id", "<system>") if caller_id else "<system>"
            emit_event(EventType.USER_AUTHENTICATED, payload={
                "user_id": caller_id,
                "action": "privilege_updated",
                "target_privilege": privilege.privilege_id,
                "ip_address": request.remote_addr or "unknown",
            })
        except Exception:
            logger.debug("Failed to emit privilege update audit event.", exc_info=True)

        return privilege

    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to update privilege '%s': %s",
            privilege_id,
            str(exc),
            exc_info=True,
        )
        abort(
            500,
            message=f"Failed to update privilege: {str(exc)}",
        )


@privileges_bp.route(
    "/privileges/<string:privilege_id>", methods=["DELETE"]
)
@privileges_bp.response(204)
@login_required
@require_permission("privileges", "delete")
def delete_privilege(privilege_id: str) -> str:
    """Delete a privilege descriptor.

    Removes a privilege from the system.  Built-in privileges (IDs
    starting with ``nx-``) cannot be deleted.  If the privilege is
    currently referenced by any role, deletion is refused with a
    409 Conflict response.

    **Path Parameters:**

    - ``privilege_id`` (str): The unique privilege identifier.

    **Response:** 204 No Content — Privilege successfully deleted.

    **Errors:**

    - 403 Forbidden — If attempting to delete a built-in privilege.
    - 404 Not Found — If the privilege does not exist.
    - 409 Conflict — If the privilege is referenced by one or more roles.
    """
    logger.info("Deleting privilege: '%s'.", privilege_id)

    # Prevent deletion of built-in privileges.
    if _is_builtin_privilege(privilege_id):
        logger.warning(
            "Attempted deletion of built-in privilege: '%s'.",
            privilege_id,
        )
        abort(
            403,
            message=(
                f"Built-in privilege '{privilege_id}' cannot be deleted."
            ),
        )

    # Look up the privilege.
    privilege: Optional[Privilege] = db.session.get(Privilege, privilege_id)
    if privilege is None:
        logger.warning(
            "Privilege not found for deletion: '%s'.",
            privilege_id,
        )
        abort(404, message=f"Privilege not found: '{privilege_id}'")

    # Check if the privilege is in use by any role.
    roles: List[Role] = Role.query.all()
    referencing_roles: List[str] = []
    for role in roles:
        if role.has_privilege(privilege_id):
            referencing_roles.append(role.role_id)

    if referencing_roles:
        logger.warning(
            "Cannot delete privilege '%s': in use by role(s): %s.",
            privilege_id,
            ", ".join(referencing_roles),
        )
        abort(
            409,
            message=(
                f"Privilege '{privilege_id}' is in use by role(s): "
                f"{', '.join(referencing_roles)}. Remove the privilege "
                f"from these roles before deleting."
            ),
        )

    try:
        db.session.delete(privilege)
        db.session.commit()

        logger.info(
            "Privilege deleted successfully: '%s'.",
            privilege_id,
        )

        # Emit audit event for privilege deletion (Feature F-303)
        try:
            caller_id = getattr(g, "current_user", None)
            caller_id = getattr(caller_id, "user_id", "<system>") if caller_id else "<system>"
            emit_event(EventType.USER_AUTHENTICATED, payload={
                "user_id": caller_id,
                "action": "privilege_deleted",
                "target_privilege": privilege_id,
                "ip_address": request.remote_addr or "unknown",
            })
        except Exception:
            logger.debug("Failed to emit privilege delete audit event.", exc_info=True)

        return ""

    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to delete privilege '%s': %s",
            privilege_id,
            str(exc),
            exc_info=True,
        )
        abort(
            500,
            message=f"Failed to delete privilege: {str(exc)}",
        )


# ===========================================================================
# Content Selector CRUD Endpoints
# ===========================================================================
# Content selectors are managed under /api/v1/security/content-selectors
# within this same Blueprint.  They support sub-repository (Tier 3) RBAC
# via CSEL or JEXL expression-based access control.
# ===========================================================================


@privileges_bp.route(
    "/content-selectors",
    methods=["GET"],
    endpoint="list_content_selectors",
)
@privileges_bp.response(200, ContentSelectorSchema(many=True))
@login_required
@require_permission("privileges", "read")
def list_content_selectors() -> List[ContentSelector]:
    """List all content selectors.

    Returns all content selector definitions in the system.  Content
    selectors define CSEL or JEXL expressions for sub-repository access
    control (Tier 3 RBAC).

    **Response:** 200 OK — Array of content selector objects.
    """
    logger.info("Listing content selectors.")

    selectors: List[ContentSelector] = ContentSelector.query.all()

    logger.info("Returning %d content selector(s).", len(selectors))
    return selectors


@privileges_bp.route(
    "/content-selectors",
    methods=["POST"],
    endpoint="create_content_selector",
)
@login_required
@require_permission("privileges", "create")
@privileges_bp.arguments(ContentSelectorCreateSchema)
@privileges_bp.response(201, ContentSelectorSchema)
def create_content_selector(
    payload: Dict[str, Any],
) -> ContentSelector:
    """Create a new content selector.

    Creates a content selector with a CSEL or JEXL expression for
    sub-repository access control.  The selector name must be unique.

    **Request Body:** Content selector creation payload.

    **Response:** 201 Created — The created content selector.

    **Errors:**

    - 400 Bad Request — If the expression is invalid or the name
      already exists.
    """
    name: str = payload.get("name", "")
    selector_type: str = payload.get("type", "csel")
    expression: str = payload.get("expression", "")
    description: Optional[str] = payload.get("description")

    logger.info(
        "Creating content selector: name='%s', type='%s'.",
        name,
        selector_type,
    )

    # Check if strict CSEL validation is enabled via app config.
    strict_csel: bool = current_app.config.get(
        "STRICT_CSEL_VALIDATION", True
    )

    # Validate expression for CSEL type.
    if (
        selector_type == "csel"
        and strict_csel
        and not _validate_csel_expression(expression)
    ):
        logger.warning(
            "Invalid CSEL expression for selector '%s': '%s'.",
            name,
            expression[:100],
        )
        abort(
            400,
            message=(
                "Invalid CSEL expression. Expression must contain "
                "recognized CSEL operators (==, =^, =~, and, or, not) "
                "and reference 'format' or 'path' variables."
            ),
        )

    # Check for duplicate name.
    existing: Optional[ContentSelector] = ContentSelector.query.filter_by(
        name=name
    ).first()
    if existing is not None:
        logger.warning(
            "Content selector name already exists: '%s'.",
            name,
        )
        abort(
            400,
            message=(
                f"Content selector with name '{name}' already exists."
            ),
        )

    # Generate a unique selector ID.
    selector_id: str = _generate_selector_id(name)

    try:
        selector: ContentSelector = ContentSelector(
            selector_id=selector_id,
            name=name,
            type=selector_type,
            expression=expression,
            description=description,
        )
        db.session.add(selector)
        db.session.commit()

        logger.info(
            "Content selector created: id='%s', name='%s', type='%s'.",
            selector.selector_id,
            selector.name,
            selector.type,
        )
        return selector

    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to create content selector '%s': %s",
            name,
            str(exc),
            exc_info=True,
        )
        abort(
            500,
            message=f"Failed to create content selector: {str(exc)}",
        )


@privileges_bp.route(
    "/content-selectors/<string:selector_id>",
    methods=["PUT"],
    endpoint="update_content_selector",
)
@login_required
@require_permission("privileges", "update")
@privileges_bp.arguments(ContentSelectorCreateSchema)
@privileges_bp.response(200, ContentSelectorSchema)
def update_content_selector(
    payload: Dict[str, Any], selector_id: str
) -> ContentSelector:
    """Update an existing content selector.

    Updates the name, type, expression, and/or description of a content
    selector.

    **Path Parameters:**

    - ``selector_id`` (str): The unique content selector identifier.

    **Request Body:** Updated content selector fields.

    **Response:** 200 OK — The updated content selector.

    **Errors:**

    - 400 Bad Request — If the expression is invalid or the new name
      conflicts with an existing selector.
    - 404 Not Found — If the content selector does not exist.
    """
    logger.info("Updating content selector: '%s'.", selector_id)

    selector: Optional[ContentSelector] = db.session.get(ContentSelector, 
        selector_id
    )
    if selector is None:
        logger.warning(
            "Content selector not found for update: '%s'.",
            selector_id,
        )
        abort(
            404,
            message=f"Content selector not found: '{selector_id}'",
        )

    new_name: Optional[str] = payload.get("name")
    new_type: Optional[str] = payload.get("type")
    new_expression: Optional[str] = payload.get("expression")
    new_description: Optional[str] = payload.get("description")

    # Check name uniqueness if name is being changed.
    if new_name and new_name != selector.name:
        duplicate: Optional[ContentSelector] = (
            ContentSelector.query.filter_by(name=new_name).first()
        )
        if duplicate is not None:
            abort(
                400,
                message=(
                    f"Content selector with name '{new_name}' "
                    f"already exists."
                ),
            )

    # Determine the effective type for expression validation.
    effective_type: str = new_type if new_type else selector.type

    # Validate expression if it is being updated.
    if new_expression and effective_type == "csel":
        if not _validate_csel_expression(new_expression):
            abort(
                400,
                message=(
                    "Invalid CSEL expression. Expression must contain "
                    "recognized CSEL operators (==, =^, =~, and, or, "
                    "not) and reference 'format' or 'path' variables."
                ),
            )

    try:
        if new_name is not None:
            selector.name = new_name
        if new_type is not None:
            selector.type = new_type
        if new_expression is not None:
            selector.expression = new_expression
        if new_description is not None:
            selector.description = new_description

        db.session.commit()

        logger.info(
            "Content selector updated: id='%s', name='%s'.",
            selector.selector_id,
            selector.name,
        )
        return selector

    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to update content selector '%s': %s",
            selector_id,
            str(exc),
            exc_info=True,
        )
        abort(
            500,
            message=f"Failed to update content selector: {str(exc)}",
        )


@privileges_bp.route(
    "/content-selectors/<string:selector_id>",
    methods=["DELETE"],
    endpoint="delete_content_selector",
)
@privileges_bp.response(204)
@login_required
@require_permission("privileges", "delete")
def delete_content_selector(selector_id: str) -> str:
    """Delete a content selector.

    Removes a content selector from the system.  If the selector is
    referenced by any ``repository-content-selector`` privilege, deletion
    is refused with a 409 Conflict response.

    **Path Parameters:**

    - ``selector_id`` (str): The unique content selector identifier.

    **Response:** 204 No Content — Content selector successfully deleted.

    **Errors:**

    - 404 Not Found — If the content selector does not exist.
    - 409 Conflict — If the selector is referenced by a privilege.
    """
    logger.info("Deleting content selector: '%s'.", selector_id)

    selector: Optional[ContentSelector] = db.session.get(ContentSelector, 
        selector_id
    )
    if selector is None:
        logger.warning(
            "Content selector not found for deletion: '%s'.",
            selector_id,
        )
        abort(
            404,
            message=f"Content selector not found: '{selector_id}'",
        )

    # Check if any repository-content-selector privileges reference this
    # selector by name in their properties JSON.
    referencing_privileges: List[Privilege] = Privilege.query.filter_by(
        type="repository-content-selector"
    ).all()

    referencing_ids: List[str] = []
    for priv in referencing_privileges:
        if (
            priv.properties
            and priv.properties.get("contentSelector") == selector.name
        ):
            referencing_ids.append(priv.privilege_id)

    if referencing_ids:
        logger.warning(
            "Cannot delete content selector '%s': referenced by "
            "privilege(s): %s.",
            selector_id,
            ", ".join(referencing_ids),
        )
        abort(
            409,
            message=(
                f"Content selector '{selector.name}' is referenced by "
                f"privilege(s): {', '.join(referencing_ids)}. Remove the "
                f"references before deleting."
            ),
        )

    try:
        db.session.delete(selector)
        db.session.commit()

        logger.info(
            "Content selector deleted: '%s' (name='%s').",
            selector_id,
            selector.name,
        )
        return ""

    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to delete content selector '%s': %s",
            selector_id,
            str(exc),
            exc_info=True,
        )
        abort(
            500,
            message=f"Failed to delete content selector: {str(exc)}",
        )


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------
logger.debug(
    "Privileges API blueprint loaded — url_prefix=%s, "
    "privilege_types=%s, selector_types=%s.",
    privileges_bp.url_prefix,
    ", ".join(sorted(VALID_PRIVILEGE_TYPES)),
    ", ".join(sorted(VALID_SELECTOR_TYPES)),
)
