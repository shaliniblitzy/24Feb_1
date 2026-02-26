"""
User Marshmallow Schemas for Sonatype Nexus Repository (Python/Flask).

This module defines Marshmallow 3.x serialization schemas for user management
API operations, replacing the security management Jackson 2.16.1 DTOs from the
Java source system (F-501-RQ-003).

**Schema Inventory:**

+---------------------------+------------------------------------------------+
| Schema                    | Purpose                                        |
+===========================+================================================+
| ``UserCreateSchema``      | Validate new user creation requests            |
+---------------------------+------------------------------------------------+
| ``UserUpdateSchema``      | Validate partial user update requests          |
+---------------------------+------------------------------------------------+
| ``UserResponseSchema``    | Serialize user data for API responses          |
+---------------------------+------------------------------------------------+
| ``UserChangePasswordSchema`` | Validate self-service password change       |
+---------------------------+------------------------------------------------+
| ``UserApiKeySchema``      | Serialize API key generation responses (F-304) |
+---------------------------+------------------------------------------------+

**Feature Dependencies:**

- **F-301** — Role-Based Access Control (``roles`` field maps to ``role_id``
  strings from the ``Role`` model via the many-to-many junction table).
- **F-304** — API Key Authentication (``UserApiKeySchema`` for API key
  generation responses).
- **F-501-RQ-003** — Security management API endpoint schemas.

**Security Invariants:**

- ``password_hash`` is **NEVER** a schema field — it is internal-only.
- ``password`` fields are **ALWAYS** ``load_only=True`` — they are accepted
  in creation/update requests but never serialized in responses.
- ``api_key`` only appears in ``UserApiKeySchema`` — it is shown once at
  generation time and never included in ``UserResponseSchema``.
- ``UserResponseSchema`` **NEVER** includes ``password_hash``, ``api_key``,
  or ``failed_login_count``.

**Design References (not runtime imports):**

- ``src.app.models.user.User`` — field definitions, lengths, constraints.
- ``src.app.models.role.Role`` — ``role_id`` defines valid role ID strings.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

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
from marshmallow.validate import Email, Length, OneOf, Regexp

# ---------------------------------------------------------------------------
# Module-Level Constants
# ---------------------------------------------------------------------------

VALID_STATUSES: tuple[str, ...] = ("active", "disabled", "locked")
"""Valid user account status values (must match ``User.status`` column)."""

USER_ID_PATTERN: str = r"^[a-zA-Z0-9._-]+$"
"""Regex pattern for valid ``user_id`` values — alphanumeric characters,
dots, hyphens, and underscores only.  No spaces or special characters."""

PASSWORD_MIN_LENGTH: int = 8
"""Minimum password length enforced by the password policy."""

PASSWORD_MAX_LENGTH: int = 256
"""Maximum password length to prevent excessively long inputs."""

USER_ID_MAX_LENGTH: int = 200
"""Maximum length for the ``user_id`` field (matches ``User.user_id``
column ``String(200)``)."""

EMAIL_MAX_LENGTH: int = 255
"""Maximum length for the ``email`` field (matches ``User.email``
column ``String(255)``)."""

NAME_MAX_LENGTH: int = 100
"""Maximum length for ``first_name`` and ``last_name`` fields (matches
``User.first_name`` / ``User.last_name`` column ``String(100)``)."""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "UserCreateSchema",
    "UserUpdateSchema",
    "UserResponseSchema",
    "UserChangePasswordSchema",
    "UserApiKeySchema",
]


# ---------------------------------------------------------------------------
# Helper: Password Policy Validation
# ---------------------------------------------------------------------------


def _validate_password_policy(value: str) -> None:
    """Enforce the password complexity policy defined for Feature F-301.

    The policy requires:

    1. At least one uppercase letter (``[A-Z]``).
    2. At least one lowercase letter (``[a-z]``).
    3. At least one digit (``[0-9]``).

    The minimum/maximum length constraints are enforced separately by the
    ``Length`` validator on the field definition.

    Args:
        value: The plaintext password to validate.

    Raises:
        ValidationError: If the password does not meet the complexity policy.
    """
    errors: list[str] = []

    if not re.search(r"[A-Z]", value):
        errors.append("Password must contain at least one uppercase letter.")

    if not re.search(r"[a-z]", value):
        errors.append("Password must contain at least one lowercase letter.")

    if not re.search(r"[0-9]", value):
        errors.append("Password must contain at least one digit.")

    if errors:
        raise ValidationError(errors)


# ===========================================================================
# UserCreateSchema
# ===========================================================================


class UserCreateSchema(Schema):
    """Marshmallow schema for validating new user creation requests.

    Accepts a JSON body with the following structure::

        {
            "user_id": "john.doe",
            "password": "SecureP@ss1",
            "status": "active",
            "email": "john.doe@example.com",
            "first_name": "John",
            "last_name": "Doe",
            "roles": ["nx-admin", "deployer"]
        }

    **Required fields:** ``user_id``, ``password``.
    **Optional fields:** ``status`` (default ``'active'``), ``email``,
    ``first_name``, ``last_name``, ``roles`` (default ``[]``).

    The ``password`` field is ``load_only=True`` — it is accepted during
    deserialization but **never** included in serialized output.
    """

    class Meta:
        """Schema-level configuration."""

        strict = True

    # -- Required Fields -----------------------------------------------------

    user_id = fields.String(
        required=True,
        validate=[
            Length(min=1, max=USER_ID_MAX_LENGTH),
            Regexp(
                USER_ID_PATTERN,
                error=(
                    "user_id must contain only alphanumeric characters, "
                    "dots, hyphens, and underscores."
                ),
            ),
        ],
        metadata={
            "description": (
                "Unique username / login identifier. "
                "Alphanumeric characters, dots, hyphens, and underscores only."
            ),
            "example": "john.doe",
        },
    )

    password = fields.String(
        required=True,
        load_only=True,
        validate=Length(min=PASSWORD_MIN_LENGTH, max=PASSWORD_MAX_LENGTH),
        metadata={
            "description": (
                "Plaintext password (will be hashed by the service layer). "
                "Must satisfy the password complexity policy."
            ),
        },
    )

    # -- Optional Fields -----------------------------------------------------

    status = fields.String(
        required=False,
        load_default="active",
        validate=OneOf(list(VALID_STATUSES)),
        metadata={
            "description": "Account status. Valid values: active, disabled, locked.",
            "example": "active",
        },
    )

    email = fields.String(
        required=False,
        allow_none=True,
        validate=[Email(), Length(max=EMAIL_MAX_LENGTH)],
        metadata={
            "description": "User email address.",
            "example": "john.doe@example.com",
        },
    )

    first_name = fields.String(
        required=False,
        allow_none=True,
        validate=Length(max=NAME_MAX_LENGTH),
        metadata={
            "description": "User first name.",
            "example": "John",
        },
    )

    last_name = fields.String(
        required=False,
        allow_none=True,
        validate=Length(max=NAME_MAX_LENGTH),
        metadata={
            "description": "User last name.",
            "example": "Doe",
        },
    )

    roles = fields.List(
        fields.String(validate=Length(min=1, max=200)),
        required=False,
        load_default=[],
        metadata={
            "description": (
                "List of role_id strings to assign to the new user. "
                "Each entry references a Role.role_id."
            ),
            "example": ["nx-admin"],
        },
    )

    # -- Custom Validators ---------------------------------------------------

    @validates("password")
    def validate_password(self, value: str) -> None:
        """Enforce the password complexity policy.

        Checks for at least one uppercase letter, one lowercase letter,
        and one digit.  Length constraints are enforced by the ``Length``
        validator on the field definition.

        Args:
            value: The plaintext password submitted in the request.

        Raises:
            ValidationError: If the password does not meet the policy.
        """
        _validate_password_policy(value)

    @pre_load
    def strip_whitespace(self, data: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        """Strip leading/trailing whitespace from string fields.

        Prevents accidental spaces in ``user_id`` and ``email`` fields that
        could cause subtle authentication or uniqueness issues.
        """
        if isinstance(data.get("user_id"), str):
            data["user_id"] = data["user_id"].strip()
        if isinstance(data.get("email"), str):
            data["email"] = data["email"].strip()
        if isinstance(data.get("first_name"), str):
            data["first_name"] = data["first_name"].strip()
        if isinstance(data.get("last_name"), str):
            data["last_name"] = data["last_name"].strip()
        return data


# ===========================================================================
# UserUpdateSchema
# ===========================================================================


class UserUpdateSchema(Schema):
    """Marshmallow schema for validating partial user update requests.

    All fields are optional since this schema supports partial updates.
    Only the fields included in the request body will be updated.

    ``user_id`` is **NOT** included — the username is immutable after
    account creation.

    Example request body::

        {
            "email": "new.email@example.com",
            "status": "disabled",
            "roles": ["deployer"]
        }
    """

    class Meta:
        """Schema-level configuration."""

        strict = True

    # -- Optional Fields (all fields are optional for partial update) ---------

    password = fields.String(
        required=False,
        load_only=True,
        validate=Length(min=PASSWORD_MIN_LENGTH, max=PASSWORD_MAX_LENGTH),
        metadata={
            "description": (
                "New password (if changing). Will be hashed by the service. "
                "Must satisfy the password complexity policy."
            ),
        },
    )

    status = fields.String(
        required=False,
        validate=OneOf(list(VALID_STATUSES)),
        metadata={
            "description": "Updated account status. Valid values: active, disabled, locked.",
        },
    )

    email = fields.String(
        required=False,
        allow_none=True,
        validate=[Email(), Length(max=EMAIL_MAX_LENGTH)],
        metadata={
            "description": "Updated email address.",
        },
    )

    first_name = fields.String(
        required=False,
        allow_none=True,
        validate=Length(max=NAME_MAX_LENGTH),
        metadata={
            "description": "Updated first name.",
        },
    )

    last_name = fields.String(
        required=False,
        allow_none=True,
        validate=Length(max=NAME_MAX_LENGTH),
        metadata={
            "description": "Updated last name.",
        },
    )

    roles = fields.List(
        fields.String(validate=Length(min=1, max=200)),
        required=False,
        metadata={
            "description": (
                "Updated list of role_id strings. Replaces the current "
                "role assignments entirely."
            ),
        },
    )

    # -- Custom Validators ---------------------------------------------------

    @validates("password")
    def validate_password(self, value: str) -> None:
        """Enforce the password complexity policy on the new password.

        Same policy as ``UserCreateSchema``: at least one uppercase letter,
        one lowercase letter, and one digit.

        Args:
            value: The new plaintext password submitted in the request.

        Raises:
            ValidationError: If the password does not meet the policy.
        """
        _validate_password_policy(value)

    @pre_load
    def strip_whitespace(self, data: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        """Strip leading/trailing whitespace from string fields."""
        if isinstance(data.get("email"), str):
            data["email"] = data["email"].strip()
        if isinstance(data.get("first_name"), str):
            data["first_name"] = data["first_name"].strip()
        if isinstance(data.get("last_name"), str):
            data["last_name"] = data["last_name"].strip()
        return data


# ===========================================================================
# UserResponseSchema
# ===========================================================================


class UserResponseSchema(Schema):
    """Marshmallow schema for serializing user data in API responses.

    All fields are ``dump_only=True`` — this schema is used exclusively for
    outbound serialization and does not accept input data.

    **SECURITY CRITICAL — This schema intentionally EXCLUDES:**

    - ``password_hash`` — hashed password (internal-only).
    - ``api_key`` — API access key (only exposed via ``UserApiKeySchema``).
    - ``failed_login_count`` — security-sensitive counter.

    Example serialized output::

        {
            "user_id": "john.doe",
            "status": "active",
            "email": "john.doe@example.com",
            "first_name": "John",
            "last_name": "Doe",
            "roles": ["nx-admin", "deployer"],
            "external_id": null,
            "last_login": "2025-01-15T10:30:00+00:00",
            "created_at": "2024-06-01T08:00:00+00:00",
            "updated_at": "2025-01-15T10:30:00+00:00"
        }
    """

    class Meta:
        """Schema-level configuration."""

        strict = True

    # -- Identification Fields -----------------------------------------------

    user_id = fields.String(
        dump_only=True,
        metadata={
            "description": "Unique username / login identifier.",
            "example": "john.doe",
        },
    )

    # -- Account Status ------------------------------------------------------

    status = fields.String(
        dump_only=True,
        metadata={
            "description": "Account status: active, disabled, or locked.",
            "example": "active",
        },
    )

    # -- Profile Fields ------------------------------------------------------

    email = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={
            "description": "User email address.",
            "example": "john.doe@example.com",
        },
    )

    first_name = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={
            "description": "User first name.",
            "example": "John",
        },
    )

    last_name = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={
            "description": "User last name.",
            "example": "Doe",
        },
    )

    # -- RBAC Fields ---------------------------------------------------------

    roles = fields.List(
        fields.String(),
        dump_only=True,
        metadata={
            "description": (
                "List of role_id strings assigned to this user via the "
                "many-to-many RoleAssignment junction table (F-301)."
            ),
            "example": ["nx-admin", "deployer"],
        },
    )

    # -- External Identity ---------------------------------------------------

    external_id = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={
            "description": (
                "External identity provider identifier. LDAP Distinguished "
                "Name or SAML/OIDC subject claim. Null for local users."
            ),
        },
    )

    # -- Timestamps ----------------------------------------------------------

    last_login = fields.DateTime(
        dump_only=True,
        allow_none=True,
        format="iso",
        metadata={
            "description": "UTC timestamp of the most recent successful login.",
        },
    )

    created_at = fields.DateTime(
        dump_only=True,
        format="iso",
        metadata={
            "description": "UTC timestamp of when the user account was created.",
        },
    )

    updated_at = fields.DateTime(
        dump_only=True,
        format="iso",
        metadata={
            "description": "UTC timestamp of the most recent account update.",
        },
    )

    # -- Post-Dump Processing ------------------------------------------------

    @post_dump
    def strip_none_values(self, data: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        """Remove keys with ``None`` values for cleaner JSON responses.

        This produces more compact API responses by omitting unset optional
        fields entirely rather than including them as ``null``.
        """
        return {key: value for key, value in data.items() if value is not None}


# ===========================================================================
# UserChangePasswordSchema
# ===========================================================================


class UserChangePasswordSchema(Schema):
    """Marshmallow schema for self-service password change requests.

    Used by the ``PUT /api/v1/users/{user_id}/change-password`` endpoint.

    Both fields are ``load_only=True`` — passwords are never included in
    serialized responses.

    Example request body::

        {
            "current_password": "OldP@ssw0rd",
            "new_password": "NewSecure1"
        }
    """

    class Meta:
        """Schema-level configuration."""

        strict = True

    current_password = fields.String(
        required=True,
        load_only=True,
        metadata={
            "description": "The user's current password for verification.",
        },
    )

    new_password = fields.String(
        required=True,
        load_only=True,
        validate=Length(min=PASSWORD_MIN_LENGTH, max=PASSWORD_MAX_LENGTH),
        metadata={
            "description": (
                "The desired new password. Must satisfy the password "
                "complexity policy."
            ),
        },
    )

    # -- Custom Validators ---------------------------------------------------

    @validates("new_password")
    def validate_new_password(self, value: str) -> None:
        """Enforce the password complexity policy on the new password.

        Same policy as ``UserCreateSchema``: at least one uppercase letter,
        one lowercase letter, and one digit.

        Args:
            value: The new plaintext password submitted in the request.

        Raises:
            ValidationError: If the new password does not meet the policy.
        """
        _validate_password_policy(value)

    @validates_schema
    def validate_passwords_differ(
        self, data: Dict[str, Any], **kwargs: Any
    ) -> None:
        """Ensure the new password is different from the current password.

        Prevents users from "changing" their password to the same value,
        which would be a no-op but might give a false sense of security
        rotation.

        Args:
            data: The deserialized request data containing both passwords.

        Raises:
            ValidationError: If the new password matches the current one.
        """
        current: Optional[str] = data.get("current_password")
        new: Optional[str] = data.get("new_password")
        if current and new and current == new:
            raise ValidationError(
                "New password must be different from the current password.",
                field_name="new_password",
            )


# ===========================================================================
# UserApiKeySchema
# ===========================================================================


class UserApiKeySchema(Schema):
    """Marshmallow schema for API key generation responses (Feature F-304).

    Used by:

    - ``POST /api/v1/users/{user_id}/api-key`` — generate a new API key.
    - ``DELETE /api/v1/users/{user_id}/api-key`` — revoke the API key.

    The ``api_key`` value is shown **only once** at generation time.  It is
    never stored in cleartext after the initial response and cannot be
    retrieved again — the user must generate a new key if lost.

    Example serialized output::

        {
            "api_key": "NXapikey-abc123def456ghi789...",
            "created_at": "2025-02-01T12:00:00+00:00"
        }
    """

    class Meta:
        """Schema-level configuration."""

        strict = True

    api_key = fields.String(
        dump_only=True,
        metadata={
            "description": (
                "The generated API key. Shown only once at generation time. "
                "Used by BearerTokenRealm for programmatic authentication."
            ),
        },
    )

    created_at = fields.DateTime(
        dump_only=True,
        format="iso",
        metadata={
            "description": "UTC timestamp of when the API key was generated.",
        },
    )
