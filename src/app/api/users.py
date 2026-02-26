"""
User CRUD REST API Blueprint for Sonatype Nexus Repository (Python/Flask).

Implements Feature **F-501-RQ-003** (Security Management — Users).  Provides
full user lifecycle management endpoints: creation, listing, retrieval,
update, deletion, password changes, and API key management.

**Replaces:** Java Security management REST API resource classes from the
original ``RESTEasy 6.2.7`` JAX-RS implementation.

**Endpoint Overview:**

+--------+-------------------------------------------+---------------------+
| Method | Path                                      | Description         |
+========+===========================================+=====================+
| GET    | /api/v1/security/users                    | List all users      |
+--------+-------------------------------------------+---------------------+
| POST   | /api/v1/security/users                    | Create a new user   |
+--------+-------------------------------------------+---------------------+
| GET    | /api/v1/security/users/<user_id>          | Get user details    |
+--------+-------------------------------------------+---------------------+
| PUT    | /api/v1/security/users/<user_id>          | Update user         |
+--------+-------------------------------------------+---------------------+
| DELETE | /api/v1/security/users/<user_id>          | Delete user         |
+--------+-------------------------------------------+---------------------+
| PUT    | /api/v1/security/users/<id>/change-password | Change password   |
+--------+-------------------------------------------+---------------------+
| POST   | /api/v1/security/users/<id>/api-key       | Generate API key    |
+--------+-------------------------------------------+---------------------+
| DELETE | /api/v1/security/users/<id>/api-key       | Revoke API key      |
+--------+-------------------------------------------+---------------------+

**Security Invariants:**

-  ``password_hash``, ``api_key``, and ``failed_login_count`` are **NEVER**
   exposed through any API response.  All serialization flows through
   :class:`~src.app.schemas.user.UserResponseSchema` which excludes these
   sensitive columns.
-  Password hashing uses ``hash_password()`` from
   ``src.app.auth.password_utils`` (replacing BouncyCastle 1.78.1).
-  API key generation uses ``generate_api_key()`` which produces 40-char
   hex tokens via the OS CSPRNG (Feature F-304).
-  RBAC enforcement via ``require_permission`` decorator matches the
   three-tier authorization model from Apache Shiro 2.0.0.

**Feature Dependencies:**

- **F-301** — Role-Based Access Control (role assignment management)
- **F-303** — Audit Logging (request/response logging hooks)
- **F-304** — API Key Authentication (key generation and revocation)
- **F-501-RQ-003** — Security management REST API specification

Exports:
    users_bp : The Flask-smorest Blueprint instance for user management.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import current_app, g, request
from flask_smorest import Blueprint, abort

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.auth.password_utils import (
    generate_api_key,
    hash_password,
    validate_password_strength,
    verify_password,
)
from src.app.extensions import db
from src.app.models.role import Role, RoleAssignment
from src.app.models.user import User
from src.app.schemas.user import (
    UserApiKeySchema,
    UserChangePasswordSchema,
    UserCreateSchema,
    UserResponseSchema,
    UserUpdateSchema,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for all user CRUD operations, authentication
# and authorization decisions, password change events, and API key
# generation/revocation for audit trail support (Feature F-303).
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------
# Replaces the JAX-RS Security Management resource classes from the Java
# source.  The url_prefix maps to the same REST API path prefix as the
# original implementation to preserve client compatibility.
# ---------------------------------------------------------------------------

users_bp: Blueprint = Blueprint(
    "users",
    __name__,
    url_prefix="/api/v1/security/users",
    description="User account management (F-501-RQ-003)",
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = ["users_bp"]


# ===========================================================================
# Helper Functions
# ===========================================================================


def _user_to_response(user: User) -> Dict[str, Any]:
    """Convert a :class:`User` model instance to a dict suitable for
    serialization by :class:`UserResponseSchema`.

    This helper resolves the dynamic ``roles`` relationship into a flat
    list of ``role_id`` strings, and maps all safe scalar fields.

    **SECURITY CRITICAL**: This function NEVER includes ``password_hash``,
    ``api_key``, or ``failed_login_count``.

    Args:
        user: The User model instance to serialize.

    Returns:
        A dictionary matching the :class:`UserResponseSchema` field set.
    """
    return {
        "user_id": user.user_id,
        "status": user.status,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "roles": user.role_names,
        "external_id": getattr(user, "external_id", None),
        "last_login": user.last_login,
        "created_at": getattr(user, "created_at", None),
        "updated_at": getattr(user, "updated_at", None),
    }


def _check_self_or_admin(user_id: str, action: str = "read") -> None:
    """Verify the current request user is either the target user or holds
    the required system-wide admin privilege.

    Self-access (``g.current_user.user_id == user_id``) is always allowed.
    Non-self access requires the ``users:<action>`` system privilege
    evaluated via :class:`AuthorizationEngine`.

    Args:
        user_id: The target user identifier.
        action:  The authorization action to check (default ``'read'``).

    Raises:
        HTTPException: 401 if unauthenticated, 403 if unauthorized.
    """
    current_user: Optional[User] = getattr(g, "current_user", None)
    if current_user is None:
        abort(401, message="Authentication required")

    # Self-access is always permitted
    if current_user.user_id == user_id:
        return

    # Non-self access: check system-wide admin privilege
    # Lazy import to avoid circular dependency at module load time
    from src.app.auth.authorization import AuthorizationEngine

    engine: AuthorizationEngine = AuthorizationEngine()
    if not engine.check_system_privilege(current_user, "users", action):
        logger.warning(
            "User '%s' denied access to user '%s' (required: users:%s).",
            current_user.user_id,
            user_id,
            action,
        )
        abort(
            403,
            message=f"Insufficient privileges: users:{action}",
        )


def _validate_role_ids(role_ids: List[str]) -> List[Role]:
    """Validate that all provided role IDs exist in the database.

    Uses :meth:`Role.query.filter` to batch-load roles and compares
    against the requested set.

    Args:
        role_ids: A list of role_id strings to validate.

    Returns:
        A list of valid :class:`Role` instances.

    Raises:
        HTTPException: 400 if any role IDs are invalid.
    """
    if not role_ids:
        return []

    valid_roles: List[Role] = Role.query.filter(
        Role.role_id.in_(role_ids)
    ).all()
    valid_ids: set[str] = {r.role_id for r in valid_roles}
    invalid_ids: set[str] = set(role_ids) - valid_ids

    if invalid_ids:
        abort(
            400,
            message=(
                f"Invalid role ID(s): {', '.join(sorted(invalid_ids))}"
            ),
        )

    return valid_roles


def _assign_roles(user: User, role_ids: List[str]) -> None:
    """Replace all role assignments for a user with the given role IDs.

    Deletes all existing :class:`RoleAssignment` rows for the user and
    creates new ones for each valid role ID.  Role validation is
    performed by :func:`_validate_role_ids` prior to calling this
    function.

    Args:
        user:     The :class:`User` whose roles are being updated.
        role_ids: List of role_id strings to assign.
    """
    # Remove existing role assignments
    RoleAssignment.query.filter_by(user_id=user.user_id).delete()

    # Create new assignments
    for role_id in role_ids:
        role: Optional[Role] = db.session.get(Role, role_id)
        if role is None:
            logger.warning(
                "Skipping unknown role '%s' during assignment for user '%s'.",
                role_id,
                user.user_id,
            )
            continue
        assignment = RoleAssignment(
            user_id=user.user_id,
            role_id=role.role_id,
        )
        db.session.add(assignment)

    logger.info(
        "Assigned %d role(s) to user '%s': [%s].",
        len(role_ids),
        user.user_id,
        ", ".join(role_ids),
    )


def _is_last_admin_user(user: User) -> bool:
    """Determine whether this user is the sole holder of the ``nx-admin``
    role.

    Prevents accidental lockout by refusing to delete or deactivate the
    last administrator.

    Args:
        user: The :class:`User` to check.

    Returns:
        ``True`` if the user holds ``nx-admin`` and no other user does.
    """
    try:
        admin_assignment_count: int = (
            RoleAssignment.query.filter_by(role_id="nx-admin").count()
        )
        user_is_admin: bool = (
            RoleAssignment.query.filter_by(
                user_id=user.user_id,
                role_id="nx-admin",
            ).first()
            is not None
        )
        return user_is_admin and admin_assignment_count <= 1
    except Exception:
        logger.exception(
            "Error checking admin user count for '%s'; "
            "erring on the side of caution.",
            user.user_id,
        )
        # Err on the side of safety — treat as last admin
        return True


# ===========================================================================
# Blueprint Lifecycle Hooks
# ===========================================================================


@users_bp.before_request
def before_request() -> None:
    """Log incoming requests to the users API for audit purposes.

    Captures method, path, and client IP address for the audit trail
    (Feature F-303).  This hook runs before authentication and
    authorization decorators.
    """
    logger.debug(
        "Users API request: method=%s path=%s remote_addr=%s user_agent=%s",
        request.method,
        request.path,
        request.remote_addr,
        request.headers.get("User-Agent", "<unknown>"),
    )


@users_bp.after_request
def after_request(response: Any) -> Any:
    """Add security headers to all user API responses.

    Sets ``X-Content-Type-Options``, ``Cache-Control``, and
    ``Pragma`` headers to prevent caching of sensitive user data
    by intermediaries.

    Args:
        response: The Flask response object.

    Returns:
        The modified response with security headers applied.
    """
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


# ===========================================================================
# Blueprint Error Handlers
# ===========================================================================


@users_bp.errorhandler(404)
def handle_not_found(error: Any) -> tuple:
    """Handle 404 Not Found errors within the users blueprint.

    Produces a JSON response consistent with flask-smorest's error
    format.
    """
    message: str = getattr(error, "description", str(error))
    return {"code": 404, "status": "Not Found", "message": message}, 404


@users_bp.errorhandler(409)
def handle_conflict(error: Any) -> tuple:
    """Handle 409 Conflict errors (duplicate user, last admin deletion)."""
    message = getattr(error, "description", str(error))
    return {"code": 409, "status": "Conflict", "message": message}, 409


@users_bp.errorhandler(400)
def handle_bad_request(error: Any) -> tuple:
    """Handle 400 Bad Request errors (validation failures, bad passwords)."""
    message = getattr(error, "description", str(error))
    return {"code": 400, "status": "Bad Request", "message": message}, 400


# ===========================================================================
# GET /api/v1/security/users — List All Users
# ===========================================================================


@users_bp.route("/", methods=["GET"])
@users_bp.response(200, UserResponseSchema(many=True))
@login_required
@require_permission("users", "read")
def list_users() -> List[Dict[str, Any]]:
    """List all user accounts.

    Supports optional ``?status=`` query parameter for filtering by
    account status (``active``, ``disabled``, ``locked``).

    **Authorization:** Requires ``users:read`` system-wide privilege.

    Returns:
        A list of user representations.  Sensitive fields
        (``password_hash``, ``api_key``, ``failed_login_count``) are
        NEVER included.
    """
    status_filter: Optional[str] = request.args.get("status")
    source_filter: Optional[str] = request.args.get("source")

    query = User.query

    if status_filter:
        query = query.filter_by(status=status_filter)
    if source_filter:
        query = query.filter(
            User.user_id.isnot(None)  # Baseline filter
        )

    users: List[User] = query.all()

    caller = getattr(g, "current_user", None)
    caller_id: str = getattr(caller, "user_id", "<unknown>") if caller else "<unknown>"
    current_app.logger.info(
        "User list requested by '%s': %d user(s) returned "
        "(status_filter=%s).",
        caller_id,
        len(users),
        status_filter,
    )
    return [_user_to_response(u) for u in users]


# ===========================================================================
# GET /api/v1/security/users/<user_id> — Get User Details
# ===========================================================================


@users_bp.route("/<string:user_id>", methods=["GET"])
@users_bp.response(200, UserResponseSchema)
@login_required
def get_user(user_id: str) -> Dict[str, Any]:
    """Retrieve a single user by their ``user_id``.

    **Authorization:**

    - Users can always view their **own** profile.
    - Viewing another user's profile requires ``users:read`` privilege.

    Args:
        user_id: The unique user identifier from the URL path.

    Returns:
        The user representation.  Sensitive fields are excluded.

    Raises:
        404: If no user with the given ``user_id`` exists.
        403: If the caller lacks permission to view the target user.
    """
    _check_self_or_admin(user_id, "read")

    user: Optional[User] = db.session.get(User, user_id)
    if user is None:
        abort(404, message=f"User '{user_id}' not found")

    logger.info(
        "User '%s' retrieved by '%s'.",
        user_id,
        getattr(g.current_user, "user_id", "<unknown>"),
    )
    return _user_to_response(user)


# ===========================================================================
# POST /api/v1/security/users — Create a New User
# ===========================================================================


@users_bp.route("/", methods=["POST"])
@users_bp.arguments(UserCreateSchema)
@users_bp.response(201, UserResponseSchema)
@login_required
@require_permission("users", "create")
def create_user(args: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new user account.

    **Authorization:** Requires ``users:create`` system-wide privilege.

    The request body is validated by :class:`UserCreateSchema` which
    enforces field constraints and password complexity rules.

    Workflow:
        1. Check for duplicate ``user_id`` (abort 409 if exists).
        2. Validate password strength via ``validate_password_strength()``.
        3. Hash the password via ``hash_password()``.
        4. Create the :class:`User` record.
        5. Assign initial roles (if specified).
        6. Commit the transaction.
        7. Return the created user (201).

    Args:
        args: Deserialized request body from :class:`UserCreateSchema`.

    Returns:
        The newly created user representation.

    Raises:
        409: If ``user_id`` already exists.
        400: If the password fails strength validation or role IDs are
             invalid.
    """
    user_id: str = args["user_id"]
    password: str = args["password"]

    # -- Duplicate check ------------------------------------------------
    existing: Optional[User] = User.query.filter_by(user_id=user_id).first()
    if existing is not None:
        abort(409, message=f"User '{user_id}' already exists")

    # -- Password strength validation -----------------------------------
    is_valid, violations = validate_password_strength(password)
    if not is_valid:
        abort(
            400,
            message=(
                "Password does not meet strength requirements: "
                + "; ".join(violations)
            ),
        )

    # -- Validate roles if specified ------------------------------------
    role_ids: List[str] = args.get("roles", [])
    if role_ids:
        _validate_role_ids(role_ids)

    # -- Hash password --------------------------------------------------
    password_hashed: str = hash_password(password)

    # -- Create user entity ---------------------------------------------
    user = User(
        user_id=user_id,
        password_hash=password_hashed,
        status=args.get("status", "active"),
        email=args.get("email"),
        first_name=args.get("first_name"),
        last_name=args.get("last_name"),
    )

    try:
        db.session.add(user)
        # Flush to ensure user_id constraint is checked before roles
        db.session.flush()

        # Assign initial roles
        if role_ids:
            _assign_roles(user, role_ids)

        db.session.commit()

        current_app.logger.info(
            "User '%s' created with status='%s' by '%s'.",
            user.user_id,
            user.status,
            getattr(g.current_user, "user_id", "<system>"),
        )
        return _user_to_response(user)

    except Exception as exc:
        db.session.rollback()
        logger.exception("Failed to create user '%s': %s", user_id, exc)
        abort(500, message="Failed to create user due to an internal error")


# ===========================================================================
# PUT /api/v1/security/users/<user_id> — Update an Existing User
# ===========================================================================


@users_bp.route("/<string:user_id>", methods=["PUT"])
@users_bp.arguments(UserUpdateSchema)
@users_bp.response(200, UserResponseSchema)
@login_required
@require_permission("users", "update")
def update_user(args: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """Update an existing user account.

    **Authorization:** Requires ``users:update`` system-wide privilege.

    Updatable fields: ``email``, ``first_name``, ``last_name``,
    ``status``, ``roles``.

    **CRITICAL:** Password changes are NOT allowed via this endpoint.
    Use ``PUT /api/v1/security/users/<user_id>/change-password`` instead.

    Args:
        args:    Deserialized request body from :class:`UserUpdateSchema`.
        user_id: The unique user identifier from the URL path.

    Returns:
        The updated user representation.

    Raises:
        404: If no user with the given ``user_id`` exists.
        400: If role IDs are invalid.
    """
    user: Optional[User] = db.session.get(User, user_id)
    if user is None:
        abort(404, message=f"User '{user_id}' not found")

    try:
        # -- Update profile fields --------------------------------------
        if "email" in args:
            user.email = args["email"]
        if "first_name" in args:
            user.first_name = args["first_name"]
        if "last_name" in args:
            user.last_name = args["last_name"]
        if "status" in args:
            new_status: str = args["status"]
            # Prevent deactivating the last admin
            if new_status != "active" and user.is_active:
                if _is_last_admin_user(user):
                    abort(
                        409,
                        message="Cannot deactivate the last admin user",
                    )
            user.status = new_status

        # -- Update roles if specified ----------------------------------
        if "roles" in args:
            role_ids: List[str] = args["roles"]
            _validate_role_ids(role_ids)
            _assign_roles(user, role_ids)

        # NOTE: Password changes via this endpoint are INTENTIONALLY
        # excluded.  Use the dedicated change-password endpoint.

        db.session.commit()

        logger.info(
            "User '%s' updated by '%s'.",
            user.user_id,
            getattr(g.current_user, "user_id", "<system>"),
        )
        return _user_to_response(user)

    except Exception as exc:
        db.session.rollback()
        # Re-raise HTTP exceptions (from abort()) without wrapping
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            raise
        logger.exception("Failed to update user '%s': %s", user_id, exc)
        abort(500, message="Failed to update user due to an internal error")


# ===========================================================================
# DELETE /api/v1/security/users/<user_id> — Delete a User
# ===========================================================================


@users_bp.route("/<string:user_id>", methods=["DELETE"])
@users_bp.response(204)
@login_required
@require_permission("users", "delete")
def delete_user(user_id: str) -> None:
    """Delete a user account.

    **Authorization:** Requires ``users:delete`` system-wide privilege.

    **Safety checks:**

    - Cannot delete the last user holding the ``nx-admin`` role (prevents
      admin lockout).
    - Cannot delete your own account (self-deletion is prohibited).

    Args:
        user_id: The unique user identifier from the URL path.

    Raises:
        404: If no user with the given ``user_id`` exists.
        409: If the user is the last admin or self-deletion is attempted.
    """
    user: Optional[User] = db.session.get(User, user_id)
    if user is None:
        abort(404, message=f"User '{user_id}' not found")

    # -- Safety: prevent last-admin deletion ----------------------------
    if _is_last_admin_user(user):
        abort(
            409,
            message=(
                "Cannot delete user '%s': this is the last user with "
                "the 'nx-admin' role" % user_id
            ),
        )

    # -- Safety: prevent self-deletion ----------------------------------
    current_user: Optional[User] = getattr(g, "current_user", None)
    if current_user is not None and current_user.user_id == user_id:
        abort(409, message="Cannot delete your own user account")

    try:
        # Remove role assignments first to satisfy FK constraints
        RoleAssignment.query.filter_by(user_id=user.user_id).delete()
        db.session.delete(user)
        db.session.commit()

        current_app.logger.info(
            "User '%s' deleted by '%s'.",
            user_id,
            getattr(g.current_user, "user_id", "<system>"),
        )

    except Exception as exc:
        db.session.rollback()
        logger.exception("Failed to delete user '%s': %s", user_id, exc)
        abort(500, message="Failed to delete user due to an internal error")


# ===========================================================================
# PUT /api/v1/security/users/<user_id>/change-password
# ===========================================================================


@users_bp.route("/<string:user_id>/change-password", methods=["PUT"])
@users_bp.arguments(UserChangePasswordSchema)
@users_bp.response(200)
@login_required
def change_password(args: Dict[str, Any], user_id: str) -> Dict[str, str]:
    """Change a user's password.

    **Authorization:**

    - Users can change their **own** password (must supply
      ``current_password`` for verification).
    - Administrators with ``users:update`` privilege can change any
      user's password **without** providing the current password.

    Workflow:
        1. Verify the caller has permission (self or admin).
        2. If self-service: verify ``current_password`` against stored
           hash via ``verify_password()``.
        3. Validate new password strength via
           ``validate_password_strength()``.
        4. Hash the new password via ``hash_password()``.
        5. Reset ``failed_login_count`` to 0.
        6. Commit the transaction.

    Args:
        args:    Deserialized body from :class:`UserChangePasswordSchema`.
        user_id: The unique user identifier from the URL path.

    Returns:
        A JSON confirmation message.

    Raises:
        404: If no user with the given ``user_id`` exists.
        400: If the current password is incorrect or the new password
             fails strength validation.
        403: If the caller lacks permission.
    """
    _check_self_or_admin(user_id, "update")

    user: Optional[User] = db.session.get(User, user_id)
    if user is None:
        abort(404, message=f"User '{user_id}' not found")

    current_password: str = args.get("current_password", "")
    new_password: str = args["new_password"]

    # -- Determine if this is self-service or admin-initiated -----------
    caller: Optional[User] = getattr(g, "current_user", None)
    is_self_service: bool = caller is not None and caller.user_id == user_id

    if is_self_service:
        # Self-service: verify current password
        if not current_password:
            abort(
                400,
                message="Current password is required for self-service "
                "password changes",
            )
        if not verify_password(current_password, user.password_hash or ""):
            abort(400, message="Current password is incorrect")

    # -- Validate new password strength ---------------------------------
    min_pw_length: int = current_app.config.get("PASSWORD_MIN_LENGTH", 8)
    is_valid, violations = validate_password_strength(
        new_password, min_length=min_pw_length
    )
    if not is_valid:
        abort(
            400,
            message=(
                "New password does not meet strength requirements: "
                + "; ".join(violations)
            ),
        )

    try:
        # -- Hash and store new password --------------------------------
        user.password_hash = hash_password(new_password)

        # -- Reset failed login counter (security best practice) --------
        user.failed_login_count = 0

        # If the user model supports recording login events, do so
        if hasattr(user, "record_login") and callable(user.record_login):
            user.record_login()

        db.session.commit()

        logger.info(
            "Password changed for user '%s' by '%s' (self_service=%s).",
            user_id,
            getattr(caller, "user_id", "<system>"),
            is_self_service,
        )
        return {"message": "Password changed successfully"}

    except Exception as exc:
        db.session.rollback()
        logger.exception(
            "Failed to change password for user '%s': %s", user_id, exc
        )
        abort(
            500,
            message="Failed to change password due to an internal error",
        )


# ===========================================================================
# POST /api/v1/security/users/<user_id>/api-key — Generate API Key
# ===========================================================================


@users_bp.route("/<string:user_id>/api-key", methods=["POST"])
@users_bp.response(201, UserApiKeySchema)
@login_required
def generate_user_api_key(user_id: str) -> Dict[str, Any]:
    """Generate a new API key for a user (Feature F-304).

    **Authorization:**

    - Users can generate their **own** API key.
    - Administrators with ``users:update`` privilege can generate a key
      for any user.

    The generated API key is returned **only once** in the response body.
    It cannot be retrieved later — the user must generate a new key if
    the current one is lost.

    If the user already has an API key, it is replaced (revoked and
    regenerated).

    Args:
        user_id: The unique user identifier from the URL path.

    Returns:
        A dict containing the ``api_key`` and ``created_at`` timestamp.

    Raises:
        404: If no user with the given ``user_id`` exists.
    """
    _check_self_or_admin(user_id, "update")

    user: Optional[User] = db.session.get(User, user_id)
    if user is None:
        abort(404, message=f"User '{user_id}' not found")

    # Check that the user account is active
    if not user.is_active:
        abort(
            400,
            message=(
                f"Cannot generate API key for user '{user_id}': "
                f"account status is '{user.status}'"
            ),
        )

    try:
        api_key: str = generate_api_key()
        user.api_key = api_key
        now: datetime = datetime.now(timezone.utc)

        db.session.commit()

        logger.info(
            "API key generated for user '%s' by '%s'.",
            user_id,
            getattr(g.current_user, "user_id", "<system>"),
        )
        return {"api_key": api_key, "created_at": now}

    except Exception as exc:
        db.session.rollback()
        logger.exception(
            "Failed to generate API key for user '%s': %s", user_id, exc
        )
        abort(
            500,
            message="Failed to generate API key due to an internal error",
        )


# ===========================================================================
# DELETE /api/v1/security/users/<user_id>/api-key — Revoke API Key
# ===========================================================================


@users_bp.route("/<string:user_id>/api-key", methods=["DELETE"])
@users_bp.response(204)
@login_required
def revoke_user_api_key(user_id: str) -> None:
    """Revoke (delete) a user's API key (Feature F-304).

    **Authorization:**

    - Users can revoke their **own** API key.
    - Administrators with ``users:update`` privilege can revoke any
      user's API key.

    After revocation, any clients using the old API key will receive
    ``401 Unauthorized`` responses.

    Args:
        user_id: The unique user identifier from the URL path.

    Raises:
        404: If no user with the given ``user_id`` exists.
    """
    _check_self_or_admin(user_id, "update")

    user: Optional[User] = db.session.get(User, user_id)
    if user is None:
        abort(404, message=f"User '{user_id}' not found")

    if user.api_key is None:
        logger.debug(
            "Revoke API key requested for user '%s' but no key exists.",
            user_id,
        )
        # Idempotent — return 204 even if no key existed
        return

    try:
        user.api_key = None
        db.session.commit()

        logger.info(
            "API key revoked for user '%s' by '%s'.",
            user_id,
            getattr(g.current_user, "user_id", "<system>"),
        )

    except Exception as exc:
        db.session.rollback()
        logger.exception(
            "Failed to revoke API key for user '%s': %s", user_id, exc
        )
        abort(
            500,
            message="Failed to revoke API key due to an internal error",
        )


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------
logger.debug(
    "Users API blueprint loaded — url_prefix='%s', exports: %s",
    users_bp.url_prefix,
    ", ".join(__all__),
)
