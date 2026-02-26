"""
Role and Role Assignment REST API Blueprint (Feature F-501-RQ-003).

This module implements the security role management REST API endpoints that
replace the Java Security management REST API for roles from the original
Sonatype Nexus Repository system.

**Endpoint Inventory:**

+--------+--------------------------------------------+---------------------+
| Method | Path                                       | Operation           |
+========+============================================+=====================+
| GET    | /api/v1/security/roles                     | List all roles      |
+--------+--------------------------------------------+---------------------+
| GET    | /api/v1/security/roles/<role_id>           | Get role detail     |
+--------+--------------------------------------------+---------------------+
| POST   | /api/v1/security/roles                     | Create role         |
+--------+--------------------------------------------+---------------------+
| PUT    | /api/v1/security/roles/<role_id>           | Update role         |
+--------+--------------------------------------------+---------------------+
| DELETE | /api/v1/security/roles/<role_id>           | Delete role         |
+--------+--------------------------------------------+---------------------+
| GET    | /api/v1/security/roles/<role_id>/users     | List role users     |
+--------+--------------------------------------------+---------------------+
| PUT    | /api/v1/security/roles/<role_id>/users     | Bulk assign users   |
+--------+--------------------------------------------+---------------------+
| POST   | /api/v1/security/roles/<id>/users/<uid>    | Assign single user  |
+--------+--------------------------------------------+---------------------+
| DELETE | /api/v1/security/roles/<id>/users/<uid>    | Remove assignment   |
+--------+--------------------------------------------+---------------------+

**Architecture Context:**

- Replaces RESTEasy 6.2.7 JAX-RS resource classes and Swagger 2.2.20
  documentation from the Java source.
- Uses flask-smorest 0.45.0 for OpenAPI 3.x auto-documentation.
- Marshmallow 3.23.2 schemas handle request/response serialization
  (replacing Jackson 2.16.1 DTOs).
- Authentication via ``login_required`` (replacing NexusAuthenticationFilter
  from Apache Shiro 2.0.0).
- Authorization via ``require_permission`` (replacing SecurityComponent
  privilege evaluation).

**Built-in Role Protection:**

The ``nx-admin`` and ``nx-anonymous`` roles are system-defined and cannot
be modified or deleted through the API.  Any attempt to update or delete
these roles results in an HTTP 400 error.

**RBAC Permissions Required:**

- ``roles:read``   — List and view roles, list role assignments.
- ``roles:create`` — Create new roles.
- ``roles:update`` — Update existing roles, manage role assignments.
- ``roles:delete`` — Delete roles, remove role assignments.

Exports:
    roles_bp : Flask-smorest Blueprint registered with the API.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from flask import request
from flask_smorest import Blueprint, abort

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.extensions import db
from src.app.models.privilege import Privilege
from src.app.models.role import Role, RoleAssignment
from src.app.models.user import User
from src.app.schemas.role import (
    RoleAssignmentSchema,
    RoleBulkAssignmentSchema,
    RoleCreateSchema,
    RoleSchema,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for role CRUD operations, built-in role
# protection warnings, role assignment changes, and error conditions.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUILTIN_ROLE_IDS: frozenset[str] = frozenset({"nx-admin", "nx-anonymous"})
"""Role IDs that are system-defined and protected from modification/deletion.

These roles are created during initial system setup:
- ``nx-admin``    — Full system administration (all privileges).
- ``nx-anonymous`` — Unauthenticated / anonymous access.
"""

SOURCE_ALIAS_MAP: dict[str, str] = {
    "default": "internal",
    "internal": "internal",
    "external": "external",
}
"""Maps query parameter source aliases to the actual database values.

The REST API accepts ``'default'`` as an alias for ``'internal'`` to match
the Nexus API convention where 'default' refers to locally-created roles.
"""

# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------

roles_bp: Blueprint = Blueprint(
    "roles",
    __name__,
    url_prefix="/api/v1/security/roles",
    description="Role and role assignment management (F-501-RQ-003)",
)

# ---------------------------------------------------------------------------
# Reusable Schema Instances
# ---------------------------------------------------------------------------
# Pre-instantiated for consistent serialization across endpoints.
# ---------------------------------------------------------------------------

_role_schema: RoleSchema = RoleSchema()
_role_list_schema: RoleSchema = RoleSchema(many=True)
_role_create_schema: RoleCreateSchema = RoleCreateSchema()
_role_assignment_schema: RoleAssignmentSchema = RoleAssignmentSchema()
_role_assignment_list_schema: RoleAssignmentSchema = RoleAssignmentSchema(many=True)
_bulk_assignment_schema: RoleBulkAssignmentSchema = RoleBulkAssignmentSchema()


# ===========================================================================
# Helper Functions
# ===========================================================================


def _get_role_or_404(role_id: str) -> Role:
    """Retrieve a :class:`Role` by its primary key or abort with HTTP 404.

    Args:
        role_id: The unique role identifier to look up.

    Returns:
        The matching :class:`Role` instance.

    Raises:
        HTTPException: 404 Not Found if no role with the given ID exists.
    """
    role: Optional[Role] = Role.query.get(role_id)
    if role is None:
        logger.warning("Role not found: role_id='%s'.", role_id)
        abort(404, message=f"Role '{role_id}' not found.")
    return role


def _is_builtin_role(role_id: str) -> bool:
    """Check whether a role ID corresponds to a built-in system role.

    Built-in roles (``nx-admin``, ``nx-anonymous``) are protected from
    modification and deletion to preserve system integrity.

    Args:
        role_id: The role identifier to check.

    Returns:
        ``True`` if the role is a built-in system role.
    """
    return role_id in BUILTIN_ROLE_IDS


def _validate_privilege_ids(privilege_ids: List[str]) -> List[str]:
    """Validate that all privilege IDs in a list reference existing records.

    Queries the ``Privilege`` table for each ID.  Returns a list of IDs
    that do **not** exist in the database, enabling the caller to generate
    a precise error message.

    Args:
        privilege_ids: A list of privilege ID strings to validate.

    Returns:
        A list of privilege IDs that were **not** found in the database.
        An empty list indicates all IDs are valid.
    """
    if not privilege_ids:
        return []

    invalid_ids: List[str] = []
    for privilege_id in privilege_ids:
        existing: Optional[Privilege] = Privilege.query.get(privilege_id)
        if existing is None:
            invalid_ids.append(privilege_id)
        else:
            # Confirm the primary key matches (defensive check against
            # case-sensitivity issues with Privilege.privilege_id).
            if existing.privilege_id != privilege_id:
                invalid_ids.append(privilege_id)
    return invalid_ids


def _validate_user_ids(user_ids: List[str]) -> List[str]:
    """Validate that all user IDs in a list reference existing records.

    Queries the ``User`` table for each ID.  Returns a list of IDs that
    do **not** exist in the database.

    Args:
        user_ids: A list of user ID strings to validate.

    Returns:
        A list of user IDs that were **not** found in the database.
        An empty list indicates all IDs are valid.
    """
    if not user_ids:
        return []

    invalid_ids: List[str] = []
    for user_id in user_ids:
        existing: Optional[User] = User.query.get(user_id)
        if existing is None:
            invalid_ids.append(user_id)
    return invalid_ids


# ===========================================================================
# Role CRUD Endpoints
# ===========================================================================


@roles_bp.route("/", methods=["GET"])
@roles_bp.response(200, RoleSchema(many=True))
@login_required
@require_permission("roles", "read")
def list_roles() -> List[Role]:
    """List all security roles.

    Retrieves all roles from the database with optional filtering by the
    ``source`` query parameter.  Supports the following source values:

    - ``'default'`` or ``'internal'`` — locally-created roles.
    - ``'external'`` — roles mapped from LDAP/AD or SSO providers.

    If no ``source`` parameter is provided, all roles are returned.

    Query Parameters:
        source (str, optional): Filter roles by source classification.

    Returns:
        A JSON array of serialized role objects.
    """
    source_filter: Optional[str] = request.args.get("source")

    query = Role.query

    if source_filter is not None:
        # Map alias 'default' to the actual DB value 'internal'
        resolved_source: Optional[str] = SOURCE_ALIAS_MAP.get(
            source_filter.lower()
        )
        if resolved_source is None:
            logger.info(
                "Unknown source filter value '%s'; returning empty list.",
                source_filter,
            )
            return []
        query = query.filter(Role.source == resolved_source)

    roles: List[Role] = query.order_by(Role.role_id).all()
    logger.debug(
        "Listed %d role(s) (source_filter='%s').",
        len(roles),
        source_filter,
    )
    return roles


@roles_bp.route("/<string:role_id>", methods=["GET"])
@roles_bp.response(200, RoleSchema)
@login_required
@require_permission("roles", "read")
def get_role(role_id: str) -> Role:
    """Get a specific security role by its unique identifier.

    Path Parameters:
        role_id (str): The unique role identifier.

    Returns:
        A JSON object representing the role with its full privilege list.

    Raises:
        404: If no role with the specified ID exists.
    """
    role: Role = _get_role_or_404(role_id)
    logger.debug("Retrieved role: role_id='%s'.", role.role_id)
    return role


@roles_bp.route("/", methods=["POST"])
@roles_bp.arguments(RoleCreateSchema)
@roles_bp.response(201, RoleSchema)
@login_required
@require_permission("roles", "create")
def create_role(role_data: dict) -> Role:
    """Create a new security role.

    Accepts a JSON request body conforming to :class:`RoleCreateSchema`.
    Validates that the ``role_id`` is unique and that all referenced
    privilege IDs exist in the database.

    Request Body:
        role_id (str): Unique identifier for the new role (1–200 chars).
        name (str): Human-readable role name (1–255 chars, unique).
        description (str, optional): Detailed role description.
        privileges (list[str], optional): Array of privilege IDs.
        source (str, optional): ``'internal'`` (default) or ``'external'``.

    Returns:
        The newly created role object serialized via :class:`RoleSchema`.

    Raises:
        400: If privilege IDs are invalid.
        409: If a role with the specified ``role_id`` already exists.
    """
    new_role_id: str = role_data["role_id"]
    new_name: str = role_data["name"]

    # Check for duplicate role_id
    existing_by_id: Optional[Role] = Role.query.get(new_role_id)
    if existing_by_id is not None:
        logger.warning(
            "Attempted to create duplicate role: role_id='%s'.",
            new_role_id,
        )
        abort(409, message=f"Role '{new_role_id}' already exists.")

    # Check for duplicate name
    existing_by_name: Optional[Role] = Role.query.filter(
        Role.name == new_name
    ).first()
    if existing_by_name is not None:
        logger.warning(
            "Attempted to create role with duplicate name: name='%s'.",
            new_name,
        )
        abort(
            409,
            message=f"A role with name '{new_name}' already exists.",
        )

    # Validate privilege IDs
    privileges: List[str] = role_data.get("privileges", [])
    if privileges:
        invalid_ids: List[str] = _validate_privilege_ids(privileges)
        if invalid_ids:
            logger.warning(
                "Invalid privilege IDs in role creation: %s.",
                invalid_ids,
            )
            abort(
                400,
                message=(
                    f"Invalid privilege IDs: {', '.join(invalid_ids)}. "
                    f"Each privilege ID must reference an existing privilege."
                ),
            )

    # Create the new role
    role: Role = Role(
        role_id=new_role_id,
        name=new_name,
        description=role_data.get("description"),
        privileges=privileges,
        source=role_data.get("source", "internal"),
    )

    try:
        # Use BaseModel.save() which performs db.session.add + commit
        role.save()
    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to create role '%s': %s",
            new_role_id,
            str(exc),
        )
        abort(
            400,
            message=f"Failed to create role: {str(exc)}",
        )

    logger.info(
        "Created role: role_id='%s', name='%s', is_internal=%s, "
        "privileges=%d, data=%s.",
        role.role_id,
        role.name,
        role.is_internal,
        len(privileges),
        role.to_dict(),
    )
    return role


@roles_bp.route("/<string:role_id>", methods=["PUT"])
@roles_bp.arguments(RoleCreateSchema)
@roles_bp.response(200, RoleSchema)
@login_required
@require_permission("roles", "update")
def update_role(role_data: dict, role_id: str) -> Role:
    """Update an existing security role.

    Modifies the name, description, privileges, and/or source of an
    existing role.  **Built-in roles** (``nx-admin``, ``nx-anonymous``)
    are protected from modification.

    Path Parameters:
        role_id (str): The unique role identifier to update.

    Request Body:
        Same as the create endpoint (:class:`RoleCreateSchema`).

    Returns:
        The updated role object serialized via :class:`RoleSchema`.

    Raises:
        400: If attempting to modify a built-in role or privilege IDs
             are invalid.
        404: If no role with the specified ID exists.
    """
    role: Role = _get_role_or_404(role_id)

    # Protect built-in roles from modification
    if _is_builtin_role(role_id):
        logger.warning(
            "Attempted to modify built-in role: role_id='%s'.",
            role_id,
        )
        abort(
            400,
            message=(
                f"Built-in role '{role_id}' cannot be modified. "
                f"System roles are managed automatically."
            ),
        )

    # Validate privilege IDs
    privileges: List[str] = role_data.get("privileges", [])
    if privileges:
        invalid_ids: List[str] = _validate_privilege_ids(privileges)
        if invalid_ids:
            logger.warning(
                "Invalid privilege IDs in role update for '%s': %s.",
                role_id,
                invalid_ids,
            )
            abort(
                400,
                message=(
                    f"Invalid privilege IDs: {', '.join(invalid_ids)}. "
                    f"Each privilege ID must reference an existing privilege."
                ),
            )

    # Check for name uniqueness if name is being changed
    new_name: str = role_data.get("name", role.name)
    if new_name != role.name:
        existing_by_name: Optional[Role] = Role.query.filter(
            Role.name == new_name
        ).first()
        if existing_by_name is not None and existing_by_name.role_id != role_id:
            logger.warning(
                "Attempted to rename role '%s' to existing name '%s'.",
                role_id,
                new_name,
            )
            abort(
                409,
                message=f"A role with name '{new_name}' already exists.",
            )

    # Apply updates via the BaseModel.update() method
    try:
        role.update(
            name=new_name,
            description=role_data.get("description", role.description),
            privileges=privileges,
            source=role_data.get("source", role.source),
        )
    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to update role '%s': %s",
            role_id,
            str(exc),
        )
        abort(
            400,
            message=f"Failed to update role: {str(exc)}",
        )

    logger.info(
        "Updated role: role_id='%s', name='%s', privileges=%d.",
        role.role_id,
        role.name,
        len(privileges),
    )
    return role


@roles_bp.route("/<string:role_id>", methods=["DELETE"])
@roles_bp.response(204)
@login_required
@require_permission("roles", "delete")
def delete_role(role_id: str) -> None:
    """Delete a security role and all its user assignments.

    Removes the specified role from the database.  Before deletion, all
    ``RoleAssignment`` records referencing this role are removed to
    maintain referential integrity.

    **Built-in roles** (``nx-admin``, ``nx-anonymous``) are protected
    from deletion to preserve system integrity.

    Path Parameters:
        role_id (str): The unique role identifier to delete.

    Returns:
        HTTP 204 No Content on successful deletion.

    Raises:
        400: If attempting to delete a built-in role.
        404: If no role with the specified ID exists.
    """
    role: Role = _get_role_or_404(role_id)

    # Protect built-in roles from deletion
    if _is_builtin_role(role_id):
        logger.warning(
            "Attempted to delete built-in role: role_id='%s'.",
            role_id,
        )
        abort(
            400,
            message=(
                f"Built-in role '{role_id}' cannot be deleted. "
                f"System roles are managed automatically."
            ),
        )

    try:
        # Remove all role assignments first (cascade cleanup)
        assignment_count: int = RoleAssignment.query.filter_by(
            role_id=role_id
        ).delete()
        logger.debug(
            "Removed %d role assignment(s) for role '%s'.",
            assignment_count,
            role_id,
        )

        # Delete the role itself
        role.delete()
    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to delete role '%s': %s",
            role_id,
            str(exc),
        )
        abort(
            400,
            message=f"Failed to delete role: {str(exc)}",
        )

    logger.info("Deleted role: role_id='%s'.", role_id)
    return None


# ===========================================================================
# Role Assignment Endpoints
# ===========================================================================


@roles_bp.route("/<string:role_id>/users", methods=["GET"])
@roles_bp.response(200, RoleAssignmentSchema(many=True))
@login_required
@require_permission("roles", "read")
def list_role_users(role_id: str) -> List[RoleAssignment]:
    """List all users assigned to a specific role.

    Returns a list of :class:`RoleAssignment` records for the given
    ``role_id``, each containing the ``user_id`` and ``role_id`` pair.

    Path Parameters:
        role_id (str): The unique role identifier.

    Returns:
        A JSON array of user-role assignment objects.

    Raises:
        404: If no role with the specified ID exists.
    """
    _get_role_or_404(role_id)

    assignments: List[RoleAssignment] = (
        RoleAssignment.query.filter_by(role_id=role_id).all()
    )
    logger.debug(
        "Listed %d user assignment(s) for role '%s'.",
        len(assignments),
        role_id,
    )
    return assignments


@roles_bp.route("/<string:role_id>/users", methods=["PUT"])
@roles_bp.response(200, RoleAssignmentSchema(many=True))
@login_required
@require_permission("roles", "update")
def bulk_assign_role_users(role_id: str) -> List[RoleAssignment]:
    """Bulk assign users to a role (replace all assignments).

    Replaces the current set of user assignments for the specified role
    with the provided list of ``user_ids``.  Existing assignments not in
    the new list are removed.

    The request body must be a JSON object with a ``user_ids`` key
    containing a list of user ID strings.  The
    :class:`RoleBulkAssignmentSchema` provides the validation contract
    for bulk assignment operations.

    Path Parameters:
        role_id (str): The unique role identifier.

    Request Body:
        user_ids (list[str]): Array of user IDs to assign to the role.

    Returns:
        A JSON array of the new assignment records.

    Raises:
        400: If the request body is invalid or user IDs do not exist.
        404: If no role with the specified ID exists.
    """
    role: Role = _get_role_or_404(role_id)

    # Parse and validate the request body
    body: Optional[dict] = request.get_json(silent=True)
    if body is None:
        abort(400, message="Request body must be valid JSON.")

    # Use RoleBulkAssignmentSchema for schema-level reference validation
    # The schema expects user_id and role_ids; for this endpoint we accept
    # a user_ids list with the role_id provided in the URL path.
    user_ids: Optional[List[str]] = body.get("user_ids")
    if user_ids is None or not isinstance(user_ids, list):
        abort(
            400,
            message=(
                "Request body must contain a 'user_ids' key with a "
                "list of user ID strings."
            ),
        )

    # Remove empty strings and duplicates while preserving order
    seen: set = set()
    cleaned_user_ids: List[str] = []
    for uid in user_ids:
        if not isinstance(uid, str) or not uid.strip():
            abort(
                400,
                message="Each user_id must be a non-empty string.",
            )
        uid_stripped: str = uid.strip()
        if uid_stripped not in seen:
            seen.add(uid_stripped)
            cleaned_user_ids.append(uid_stripped)

    # Validate that all user IDs exist
    if cleaned_user_ids:
        invalid_user_ids: List[str] = _validate_user_ids(cleaned_user_ids)
        if invalid_user_ids:
            logger.warning(
                "Invalid user IDs in bulk assignment for role '%s': %s.",
                role_id,
                invalid_user_ids,
            )
            abort(
                400,
                message=(
                    f"Invalid user IDs: {', '.join(invalid_user_ids)}. "
                    f"Each user ID must reference an existing user."
                ),
            )

    try:
        # Remove all existing assignments for this role
        removed_count: int = RoleAssignment.query.filter_by(
            role_id=role_id
        ).delete()
        logger.debug(
            "Removed %d existing assignment(s) for role '%s'.",
            removed_count,
            role_id,
        )

        # Create new assignments
        new_assignments: List[RoleAssignment] = []
        for uid in cleaned_user_ids:
            assignment: RoleAssignment = RoleAssignment(
                user_id=uid,
                role_id=role_id,
            )
            db.session.add(assignment)
            new_assignments.append(assignment)

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to bulk assign users to role '%s': %s",
            role_id,
            str(exc),
        )
        abort(
            400,
            message=f"Failed to assign users to role: {str(exc)}",
        )

    logger.info(
        "Bulk assigned %d user(s) to role '%s'.",
        len(new_assignments),
        role_id,
    )
    return new_assignments


@roles_bp.route(
    "/<string:role_id>/users/<string:user_id>", methods=["POST"]
)
@roles_bp.response(201, RoleAssignmentSchema)
@login_required
@require_permission("roles", "update")
def assign_role_to_user(role_id: str, user_id: str) -> RoleAssignment:
    """Assign a role to a specific user.

    Creates a new :class:`RoleAssignment` linking the specified user to
    the specified role.  If the assignment already exists, returns
    HTTP 409 Conflict.

    Path Parameters:
        role_id (str): The unique role identifier.
        user_id (str): The unique user identifier.

    Returns:
        The created assignment object.

    Raises:
        404: If the role or user does not exist.
        409: If the user is already assigned to the role.
    """
    _get_role_or_404(role_id)

    # Validate user existence
    user: Optional[User] = User.query.get(user_id)
    if user is None:
        logger.warning(
            "User not found for role assignment: user_id='%s'.",
            user_id,
        )
        abort(404, message=f"User '{user_id}' not found.")

    # Check for existing assignment
    existing: Optional[RoleAssignment] = RoleAssignment.query.filter_by(
        user_id=user_id,
        role_id=role_id,
    ).first()
    if existing is not None:
        logger.info(
            "Duplicate role assignment: user_id='%s', role_id='%s'.",
            user_id,
            role_id,
        )
        abort(
            409,
            message=(
                f"User '{user_id}' is already assigned to role '{role_id}'."
            ),
        )

    # Create the assignment
    assignment: RoleAssignment = RoleAssignment(
        user_id=user_id,
        role_id=role_id,
    )

    try:
        db.session.add(assignment)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to assign user '%s' to role '%s': %s",
            user_id,
            role_id,
            str(exc),
        )
        abort(
            400,
            message=f"Failed to create role assignment: {str(exc)}",
        )

    logger.info(
        "Assigned user '%s' to role '%s'.",
        user_id,
        role_id,
    )
    return assignment


@roles_bp.route(
    "/<string:role_id>/users/<string:user_id>", methods=["DELETE"]
)
@roles_bp.response(204)
@login_required
@require_permission("roles", "delete")
def remove_role_from_user(role_id: str, user_id: str) -> None:
    """Remove a role assignment from a user.

    Deletes the :class:`RoleAssignment` record linking the specified
    user to the specified role.

    Path Parameters:
        role_id (str): The unique role identifier.
        user_id (str): The unique user identifier.

    Returns:
        HTTP 204 No Content on successful removal.

    Raises:
        404: If the role does not exist or the assignment is not found.
    """
    _get_role_or_404(role_id)

    assignment: Optional[RoleAssignment] = RoleAssignment.query.filter_by(
        user_id=user_id,
        role_id=role_id,
    ).first()
    if assignment is None:
        logger.warning(
            "Role assignment not found: user_id='%s', role_id='%s'.",
            user_id,
            role_id,
        )
        abort(
            404,
            message=(
                f"Assignment not found for user '{user_id}' "
                f"and role '{role_id}'."
            ),
        )

    try:
        db.session.delete(assignment)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to remove assignment user='%s' role='%s': %s",
            user_id,
            role_id,
            str(exc),
        )
        abort(
            400,
            message=f"Failed to remove role assignment: {str(exc)}",
        )

    logger.info(
        "Removed user '%s' from role '%s'.",
        user_id,
        role_id,
    )
    return None


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------

logger.debug(
    "Roles API blueprint module loaded — url_prefix='%s'.",
    roles_bp.url_prefix,
)
