"""
Role, RoleAssignment, and Privilege Marshmallow Schemas.

This module defines Marshmallow 3.x serialization schemas for the RBAC
(Role-Based Access Control) security layer of the Nexus Repository
application.  These schemas replace the Jackson 2.16.1 security management
DTOs from the original Java source system (Feature F-501-RQ-003).

**Schema Inventory:**

+----------------------------+----------------------------------------------+
| Schema                     | Purpose                                      |
+============================+==============================================+
| ``RoleSchema``             | Full role serialization / deserialization     |
+----------------------------+----------------------------------------------+
| ``RoleCreateSchema``       | Role creation request validation             |
+----------------------------+----------------------------------------------+
| ``RoleAssignmentSchema``   | Single user↔role assignment mapping          |
+----------------------------+----------------------------------------------+
| ``RoleBulkAssignmentSchema``| Bulk user↔role(s) assignment mapping        |
+----------------------------+----------------------------------------------+
| ``PrivilegeSchema``        | Privilege descriptor serialization           |
+----------------------------+----------------------------------------------+

**Design Decisions:**

- Schemas are **standalone** Marshmallow ``Schema`` subclasses — they do NOT
  import SQLAlchemy models at runtime.  Field definitions are derived from the
  model column definitions in ``src/app/models/role.py`` and
  ``src/app/models/privilege.py`` as *design-time references*.

- The ``privileges`` field on ``RoleSchema`` is a JSON array of privilege ID
  strings (**not** nested ``PrivilegeSchema`` objects).  This matches the
  source DataStore schema where roles store privilege IDs as a flat JSON array
  for flexibility (wildcard patterns) and performance (no join required).

- The ``source`` field distinguishes locally-created roles (``'internal'``)
  from externally-mapped roles (``'external'`` — synced from LDAP/AD or
  SAML/OIDC identity providers).

- ``PrivilegeSchema`` supports the five privilege types implementing the
  three-tier RBAC authorization model:

  - **Tier 1 (System-wide):** ``'application'`` and ``'wildcard'``
  - **Tier 2 (Repository-scoped):** ``'repository-admin'`` and
    ``'repository-view'``
  - **Tier 3 (Sub-repository):** ``'repository-content-selector'``

Exports:
    RoleSchema              : Full role serialization with validation.
    RoleCreateSchema        : Role creation request schema.
    RoleAssignmentSchema    : Single user-role assignment schema.
    RoleBulkAssignmentSchema: Bulk user-role assignment schema.
    PrivilegeSchema         : Privilege descriptor serialization schema.
"""

from __future__ import annotations

from marshmallow import (
    Schema,
    ValidationError,
    fields,
    post_dump,
    pre_load,
    validate,
    validates,
)
from marshmallow.validate import Length, OneOf

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "RoleSchema",
    "RoleCreateSchema",
    "RoleUpdateSchema",
    "RoleAssignmentSchema",
    "RoleBulkAssignmentSchema",
    "PrivilegeSchema",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Valid values for the Role.source column.
VALID_ROLE_SOURCES: list[str] = ["internal", "external"]

#: Valid privilege type identifiers corresponding to the three-tier RBAC model.
VALID_PRIVILEGE_TYPES: list[str] = [
    "application",
    "repository-admin",
    "repository-content-selector",
    "repository-view",
    "wildcard",
]

#: Maximum length for a single privilege_id string (mirrors Privilege model PK).
MAX_PRIVILEGE_ID_LENGTH: int = 300


# ===========================================================================
# RoleSchema
# ===========================================================================


class RoleSchema(Schema):
    """Marshmallow schema for serializing and deserializing role definitions.

    Mirrors the ``Role`` SQLAlchemy model from ``src/app/models/role.py``.
    Used for both request validation (load) and response serialization (dump)
    by the ``src/app/api/roles.py`` blueprint.

    **Field Reference (Role model column → schema field):**

    +-------------------+----------------------------+----------------------+
    | Model Column      | Schema Field               | Notes                |
    +===================+============================+======================+
    | role_id (PK)      | role_id (String, required) | Length 1–200         |
    +-------------------+----------------------------+----------------------+
    | name (unique)     | name (String, required)    | Length 1–255         |
    +-------------------+----------------------------+----------------------+
    | description       | description (String)       | Nullable             |
    +-------------------+----------------------------+----------------------+
    | privileges (JSON) | privileges (List[String])  | Privilege ID array   |
    +-------------------+----------------------------+----------------------+
    | source            | source (String)            | 'internal'/'external'|
    +-------------------+----------------------------+----------------------+
    | attributes (JSON) | attributes (Dict)          | Extensible metadata  |
    +-------------------+----------------------------+----------------------+
    | created_at        | created_at (DateTime)      | dump_only, ISO 8601  |
    +-------------------+----------------------------+----------------------+
    | updated_at        | updated_at (DateTime)      | dump_only, ISO 8601  |
    +-------------------+----------------------------+----------------------+
    """

    # -- Primary Key ---------------------------------------------------------

    role_id = fields.String(
        required=True,
        validate=Length(min=1, max=200),
        metadata={
            "description": (
                "Unique role identifier.  Examples: 'nx-admin', "
                "'nx-anonymous', 'custom-deployer'."
            ),
        },
    )

    # -- Descriptive Fields --------------------------------------------------

    name = fields.String(
        required=True,
        validate=Length(min=1, max=255),
        metadata={"description": "Human-readable role name."},
    )

    description = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={"description": "Optional detailed description of the role."},
    )

    # -- Privilege Aggregation -----------------------------------------------

    privileges = fields.List(
        fields.String(),
        required=False,
        load_default=[],
        metadata={
            "description": (
                "JSON array of privilege ID strings associated with this "
                "role.  Each entry references a Privilege.privilege_id.  "
                "Examples: ['nx-all'], "
                "['nx-repository-view-maven2-*-read', "
                "'nx-repository-view-npm-*-browse']."
            ),
        },
    )

    # -- Source Classification -----------------------------------------------

    source = fields.String(
        required=False,
        load_default="internal",
        validate=OneOf(VALID_ROLE_SOURCES),
        metadata={
            "description": (
                "Role source classification: 'internal' for locally-created "
                "roles, 'external' for roles mapped from LDAP/AD or SSO "
                "providers (SAML/OIDC)."
            ),
        },
    )

    # -- Extensible Metadata -------------------------------------------------

    attributes = fields.Dict(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            "description": "Extensible JSON metadata for the role.",
        },
    )

    # -- Timestamps (Read-Only) ----------------------------------------------

    created_at = fields.DateTime(
        dump_only=True,
        metadata={"description": "UTC timestamp of role creation (ISO 8601)."},
    )

    updated_at = fields.DateTime(
        dump_only=True,
        metadata={
            "description": (
                "UTC timestamp of last role modification (ISO 8601)."
            ),
        },
    )

    # -- Custom Validators ---------------------------------------------------

    @validates("privileges")
    def validate_privileges(self, value: list) -> None:
        """Validate that each privilege ID in the list is well-formed.

        Enforces the following constraints on each entry:

        1. Must be a non-empty string.
        2. Must not exceed :data:`MAX_PRIVILEGE_ID_LENGTH` (300) characters,
           matching the ``Privilege.privilege_id`` column width.

        Args:
            value: The deserialized ``privileges`` list.

        Raises:
            ValidationError: If any entry violates the constraints above.
        """
        if not value:
            return

        for index, privilege_id in enumerate(value):
            if not isinstance(privilege_id, str) or not privilege_id.strip():
                raise ValidationError(
                    f"Privilege at index {index} must be a non-empty string.",
                    field_name="privileges",
                )
            if len(privilege_id) > MAX_PRIVILEGE_ID_LENGTH:
                raise ValidationError(
                    f"Privilege ID at index {index} exceeds maximum length "
                    f"of {MAX_PRIVILEGE_ID_LENGTH} characters: "
                    f"'{privilege_id[:50]}...'",
                    field_name="privileges",
                )

    # -- Pre/Post Processing -------------------------------------------------

    @pre_load
    def normalize_input(self, data: dict, **kwargs) -> dict:
        """Pre-process incoming data before deserialization.

        - Strips leading/trailing whitespace from ``role_id`` and ``name``.
        - Normalises ``source`` to lowercase.

        Args:
            data: Raw input data dictionary.

        Returns:
            The cleaned data dictionary.
        """
        if "role_id" in data and isinstance(data["role_id"], str):
            data["role_id"] = data["role_id"].strip()
        if "name" in data and isinstance(data["name"], str):
            data["name"] = data["name"].strip()
        if "source" in data and isinstance(data["source"], str):
            data["source"] = data["source"].strip().lower()
        return data

    @post_dump
    def clean_output(self, data: dict, **kwargs) -> dict:
        """Post-process serialized data after dumping.

        Ensures ``privileges`` defaults to an empty list rather than
        ``None`` in the serialized output for consistent API responses.

        Args:
            data: Serialized data dictionary.

        Returns:
            The cleaned output dictionary.
        """
        if data.get("privileges") is None:
            data["privileges"] = []
        return data


# ===========================================================================
# RoleCreateSchema
# ===========================================================================


class RoleCreateSchema(Schema):
    """Marshmallow schema for creating new role definitions.

    A focused subset of :class:`RoleSchema` tailored for ``POST`` requests
    to the role creation endpoint.  Excludes read-only timestamp fields
    that are set automatically by the database layer.

    Usage::

        schema = RoleCreateSchema()
        result = schema.load({
            "role_id": "custom-deployer",
            "name": "Custom Deployer",
            "description": "Can deploy to all Maven repositories",
            "privileges": ["nx-repository-view-maven2-*-read",
                           "nx-component-upload"],
            "source": "internal",
        })
    """

    role_id = fields.String(
        required=True,
        validate=Length(min=1, max=200),
        metadata={
            "description": (
                "Unique role identifier for the new role.  Must be "
                "between 1 and 200 characters."
            ),
        },
    )

    name = fields.String(
        required=True,
        validate=Length(min=1, max=255),
        metadata={"description": "Human-readable role name."},
    )

    description = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={"description": "Optional role description."},
    )

    privileges = fields.List(
        fields.String(),
        required=False,
        load_default=[],
        metadata={
            "description": (
                "List of privilege IDs to associate with this role."
            ),
        },
    )

    source = fields.String(
        required=False,
        load_default="internal",
        validate=OneOf(VALID_ROLE_SOURCES),
        metadata={
            "description": (
                "Role source: 'internal' (default) or 'external' "
                "(LDAP/SSO mapped)."
            ),
        },
    )

    @pre_load
    def normalize_input(self, data: dict, **kwargs) -> dict:
        """Strip whitespace from string fields before validation.

        Args:
            data: Raw input data dictionary.

        Returns:
            The cleaned data dictionary.
        """
        if "role_id" in data and isinstance(data["role_id"], str):
            data["role_id"] = data["role_id"].strip()
        if "name" in data and isinstance(data["name"], str):
            data["name"] = data["name"].strip()
        if "source" in data and isinstance(data["source"], str):
            data["source"] = data["source"].strip().lower()
        return data


# ===========================================================================
# RoleUpdateSchema
# ===========================================================================


class RoleUpdateSchema(Schema):
    """Marshmallow schema for updating existing role definitions.

    All fields are **optional** to support partial updates — only the
    fields included in the request body are modified.  This differs from
    :class:`RoleCreateSchema` where ``role_id`` and ``name`` are required.

    This schema replaces the use of ``RoleCreateSchema`` on the PUT endpoint
    and is consistent with the ``UserUpdateSchema`` pattern in the users API,
    which also supports partial updates.

    Usage::

        schema = RoleUpdateSchema()
        result = schema.load({"description": "Updated description"})
        # Only 'description' is present in result
    """

    name = fields.String(
        required=False,
        validate=Length(min=1, max=255),
        metadata={"description": "Human-readable role name."},
    )

    description = fields.String(
        required=False,
        allow_none=True,
        metadata={"description": "Optional role description."},
    )

    privileges = fields.List(
        fields.String(),
        required=False,
        metadata={
            "description": (
                "List of privilege IDs to associate with this role."
            ),
        },
    )

    source = fields.String(
        required=False,
        validate=OneOf(VALID_ROLE_SOURCES),
        metadata={
            "description": (
                "Role source: 'internal' (default) or 'external' "
                "(LDAP/SSO mapped)."
            ),
        },
    )

    @pre_load
    def normalize_input(self, data: dict, **kwargs) -> dict:
        """Strip whitespace from string fields before validation.

        Args:
            data: Raw input data dictionary.

        Returns:
            The cleaned data dictionary.
        """
        if "name" in data and isinstance(data["name"], str):
            data["name"] = data["name"].strip()
        if "source" in data and isinstance(data["source"], str):
            data["source"] = data["source"].strip().lower()
        return data


# ===========================================================================
# RoleAssignmentSchema
# ===========================================================================


class RoleAssignmentSchema(Schema):
    """Marshmallow schema for user-role assignment operations.

    Mirrors the ``RoleAssignment`` junction table from
    ``src/app/models/role.py``, which implements the many-to-many
    relationship between ``User`` and ``Role`` entities.

    This schema is used for **both** assigning (``POST``) and un-assigning
    (``DELETE``) roles to/from users.

    Usage::

        schema = RoleAssignmentSchema()
        result = schema.load({"user_id": "admin", "role_id": "nx-admin"})
    """

    user_id = fields.String(
        required=True,
        validate=Length(min=1, max=200),
        metadata={
            "description": (
                "The user to assign or unassign the role to/from."
            ),
        },
    )

    role_id = fields.String(
        required=True,
        validate=Length(min=1, max=200),
        metadata={
            "description": "The role to assign or unassign.",
        },
    )

    @pre_load
    def normalize_input(self, data: dict, **kwargs) -> dict:
        """Strip whitespace from identifier fields.

        Args:
            data: Raw input data dictionary.

        Returns:
            The cleaned data dictionary.
        """
        if "user_id" in data and isinstance(data["user_id"], str):
            data["user_id"] = data["user_id"].strip()
        if "role_id" in data and isinstance(data["role_id"], str):
            data["role_id"] = data["role_id"].strip()
        return data


# ===========================================================================
# RoleBulkAssignmentSchema
# ===========================================================================


class RoleBulkAssignmentSchema(Schema):
    """Marshmallow schema for bulk role assignment operations.

    Allows assigning **multiple** roles to a single user in one request.
    The ``role_ids`` list must contain at least one role identifier.

    Usage::

        schema = RoleBulkAssignmentSchema()
        result = schema.load({
            "user_id": "new-developer",
            "role_ids": ["nx-anonymous", "custom-deployer"],
        })
    """

    user_id = fields.String(
        required=True,
        validate=Length(min=1, max=200),
        metadata={
            "description": "The target user for the bulk role assignment.",
        },
    )

    role_ids = fields.List(
        fields.String(validate=Length(min=1, max=200)),
        required=True,
        validate=Length(min=1),
        metadata={
            "description": (
                "List of role_id strings to assign to the user.  "
                "Must contain at least one role."
            ),
        },
    )

    @pre_load
    def normalize_input(self, data: dict, **kwargs) -> dict:
        """Strip whitespace from the user_id field.

        Args:
            data: Raw input data dictionary.

        Returns:
            The cleaned data dictionary.
        """
        if "user_id" in data and isinstance(data["user_id"], str):
            data["user_id"] = data["user_id"].strip()
        return data


# ===========================================================================
# PrivilegeSchema
# ===========================================================================


class PrivilegeSchema(Schema):
    """Marshmallow schema for serializing privilege descriptors.

    Mirrors the ``Privilege`` SQLAlchemy model from
    ``src/app/models/privilege.py``.  Privileges are the atomic permission
    units in the RBAC system — they define what actions are permitted on
    which resources.

    **Privilege Types and Properties JSON Structures:**

    - **application** (Tier 1 — System-wide)::

          {"domain": "users", "actions": ["create", "read", "update", "delete"]}

    - **wildcard** (Tier 1 — System-wide)::

          {}  (no properties — grants all access)

    - **repository-admin** (Tier 2 — Repository-scoped)::

          {"format": "docker", "repository": "docker-hosted"}

    - **repository-view** (Tier 2 — Repository-scoped)::

          {"format": "maven2", "repository": "*",
           "actions": ["read", "browse"]}

    - **repository-content-selector** (Tier 3 — Sub-repository)::

          {"format": "npm", "repository": "*",
           "contentSelector": "my-selector", "actions": ["read"]}

    .. note::
        Privilege data is typically **read-only** from the API perspective
        — privileges are system-defined.  The ``properties`` JSON structure
        varies by privilege type; type-specific validation is handled at the
        service layer.
    """

    # -- Primary Key ---------------------------------------------------------

    privilege_id = fields.String(
        required=True,
        validate=Length(max=MAX_PRIVILEGE_ID_LENGTH),
        metadata={
            "description": (
                "Unique privilege identifier.  Follows the pattern "
                "'nx-<scope>-<action>-<format>-<repository>' or a simpler "
                "form for wildcard / application privileges (e.g. 'nx-all')."
            ),
        },
    )

    # -- Type Classification -------------------------------------------------

    type = fields.String(
        required=True,
        validate=OneOf(VALID_PRIVILEGE_TYPES),
        metadata={
            "description": (
                "Privilege type descriptor.  Determines which RBAC tier "
                "this privilege belongs to and how the 'properties' JSON "
                "is interpreted.  Valid values: 'application', "
                "'repository-admin', 'repository-content-selector', "
                "'repository-view', 'wildcard'."
            ),
        },
    )

    # -- Descriptive Fields --------------------------------------------------

    name = fields.String(
        required=True,
        validate=Length(min=1, max=255),
        metadata={
            "description": (
                "Human-readable privilege name displayed in the "
                "administration UI.  Example: "
                "'nx-repository-admin - (maven2 - *)'."
            ),
        },
    )

    description = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Optional detailed description of what this privilege grants."
            ),
        },
    )

    # -- Type-Specific Properties --------------------------------------------

    properties = fields.Dict(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Type-specific parameters that define the exact scope of "
                "this privilege.  Structure varies by 'type' — see class "
                "docstring for examples per type."
            ),
        },
    )

    # -- Extensible Metadata -------------------------------------------------

    attributes = fields.Dict(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            "description": "Extensible JSON metadata for the privilege.",
        },
    )

    # -- Timestamps (Read-Only) ----------------------------------------------

    created_at = fields.DateTime(
        dump_only=True,
        metadata={
            "description": (
                "UTC timestamp of privilege creation (ISO 8601)."
            ),
        },
    )

    updated_at = fields.DateTime(
        dump_only=True,
        metadata={
            "description": (
                "UTC timestamp of last privilege modification (ISO 8601)."
            ),
        },
    )

    # -- Pre/Post Processing -------------------------------------------------

    @pre_load
    def normalize_input(self, data: dict, **kwargs) -> dict:
        """Pre-process incoming data before deserialization.

        Strips leading/trailing whitespace from ``privilege_id``, ``name``,
        and ``type`` string fields.

        Args:
            data: Raw input data dictionary.

        Returns:
            The cleaned data dictionary.
        """
        if "privilege_id" in data and isinstance(data["privilege_id"], str):
            data["privilege_id"] = data["privilege_id"].strip()
        if "name" in data and isinstance(data["name"], str):
            data["name"] = data["name"].strip()
        if "type" in data and isinstance(data["type"], str):
            data["type"] = data["type"].strip().lower()
        return data

    @post_dump
    def clean_output(self, data: dict, **kwargs) -> dict:
        """Post-process serialized data after dumping.

        Ensures ``properties`` defaults to an empty dict rather than
        ``None`` for wildcard privileges, providing consistent API responses.

        Args:
            data: Serialized data dictionary.

        Returns:
            The cleaned output dictionary.
        """
        if data.get("properties") is None:
            data["properties"] = {}
        return data
