"""
Role and RoleAssignment SQLAlchemy Models.

This module defines the **Role** and **RoleAssignment** SQLAlchemy models that
replace the ``ROLE`` and ``ROLE_ASSIGNMENT`` entities from the DataStore schema
(Section 6.2.1.2 of the Technical Specification).

**Architecture Context:**
Replaces the MyBatis 3.5.15 role and role-assignment mapper definitions from
the original Java source system (Sonatype Nexus Repository).  In the Java
architecture, roles and role-assignments were persisted via MyBatis XML mappers
with SQL-level joins; here, SQLAlchemy's ORM with relationship() and a junction
table model provides equivalent functionality.

**RBAC Model (Feature F-301):**
The ``Role`` model is a core component of the Role-Based Access Control system:

- **Roles** aggregate **Privileges** (stored as a JSON array of privilege IDs
  for flexibility and performance).
- **Users** are assigned **Roles** via the ``RoleAssignment`` junction table,
  establishing a many-to-many relationship.
- The ``source`` field distinguishes locally-created roles (``'internal'``) from
  externally-mapped roles (``'external'`` — synced from LDAP/AD or SSO
  providers like SAML/OIDC).

**Default Roles:**

+---------------------+-----------------------------------------------+
| Role ID             | Purpose                                       |
+=====================+===============================================+
| ``nx-admin``        | Full system administration (all privileges)   |
+---------------------+-----------------------------------------------+
| ``nx-anonymous``    | Unauthenticated / anonymous access            |
+---------------------+-----------------------------------------------+

**Compatibility:**
Designed to support **both** SQLite (standalone deployments) and PostgreSQL
(clustered / enterprise deployments) via portable SQLAlchemy types.  The
``privileges`` column uses the portable ``JSON`` type, which maps to ``TEXT``
with JSON emulation on SQLite and native ``json``/``jsonb`` on PostgreSQL.

Exports:
    Role            : The security role model with privilege aggregation.
    RoleAssignment  : Junction table model for User ↔ Role many-to-many.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String, Text, func

from src.app.extensions import db
from src.app.models.base import BaseModel, JSONAttributesMixin, TimestampMixin

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "Role",
    "RoleAssignment",
]


# ===========================================================================
# RoleAssignment Junction Table Model
# ===========================================================================


class RoleAssignment(db.Model):
    """Junction table implementing the many-to-many relationship between
    :class:`~src.app.models.user.User` and :class:`Role`.

    Each row represents a single user-to-role mapping.  The composite primary
    key ``(user_id, role_id)`` prevents duplicate assignments and matches the
    ``ROLE_ASSIGNMENT`` entity definition in the DataStore schema (Section
    6.2.1.2).

    The ``created_at`` column provides an audit trail of when the role was
    assigned to the user, supporting compliance and security auditing
    requirements (Feature F-303).

    Usage::

        assignment = RoleAssignment(
            user_id="admin",
            role_id="nx-admin",
        )
        db.session.add(assignment)
        db.session.commit()
    """

    __tablename__: str = "role_assignments"

    # -- Composite Primary Key -----------------------------------------------

    user_id: str = db.Column(
        db.String(200),
        db.ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        doc="Foreign key reference to the assigned user.",
    )

    role_id: str = db.Column(
        db.String(200),
        db.ForeignKey("roles.role_id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        doc="Foreign key reference to the assigned role.",
    )

    # -- Audit Trail ---------------------------------------------------------

    created_at: datetime = db.Column(
        db.DateTime(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        doc="UTC timestamp of when this role assignment was created.",
    )

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return (
            f"<RoleAssignment user_id={self.user_id!r} "
            f"role_id={self.role_id!r}>"
        )


# ===========================================================================
# Role Model
# ===========================================================================


class Role(BaseModel, TimestampMixin, JSONAttributesMixin):
    """Security role model with privilege aggregation.

    Roles are the primary unit of permission grouping in the RBAC system
    (Feature F-301).  Each role contains a JSON array of privilege IDs that
    define what actions the role grants.  Users are assigned roles through the
    :class:`RoleAssignment` junction table, establishing a many-to-many
    relationship.

    **Column Mapping (Java DataStore → SQLAlchemy):**

    +------------------+--------------------+-------------------------------+
    | DataStore Column | SQLAlchemy Column  | Notes                         |
    +==================+====================+===============================+
    | role_id          | role_id (PK)       | String(200), immutable        |
    +------------------+--------------------+-------------------------------+
    | name             | name               | Unique, human-readable        |
    +------------------+--------------------+-------------------------------+
    | description      | description        | Optional descriptive text     |
    +------------------+--------------------+-------------------------------+
    | privileges       | privileges (JSON)  | Array of privilege ID strings |
    +------------------+--------------------+-------------------------------+
    | —                | source             | 'internal' or 'external'      |
    +------------------+--------------------+-------------------------------+
    | attributes       | attributes (JSON)  | Extensible metadata (mixin)   |
    +------------------+--------------------+-------------------------------+
    | —                | created_at         | Audit timestamp (mixin)       |
    +------------------+--------------------+-------------------------------+
    | —                | updated_at         | Audit timestamp (mixin)       |
    +------------------+--------------------+-------------------------------+

    The ``privileges`` column stores privilege IDs as a JSON array rather than
    using a relational foreign-key join to the Privilege table.  This is by
    design (matching the source DataStore schema) and provides:

    - **Flexibility**: Privilege IDs can include wildcard patterns (e.g.,
      ``"nx-repository-admin-*"``).
    - **Performance**: No join required to resolve a role's privileges.
    - **Simplicity**: Role serialisation/deserialisation is trivial.

    Usage::

        role = Role(
            role_id="nx-admin",
            name="Administrator",
            description="Full system administration role",
            privileges=["nx-all"],
            source="internal",
        )
        role.save()

        if role.has_privilege("nx-all"):
            print("Role grants full admin access")
    """

    __tablename__: str = "roles"

    # -- Table Arguments (Indexes) -------------------------------------------

    __table_args__ = (
        db.Index("ix_roles_source", "source"),
    )

    # -- Primary Key ---------------------------------------------------------

    role_id: str = Column(
        String(200),
        primary_key=True,
        nullable=False,
        doc=(
            "Unique role identifier (e.g., 'nx-admin', 'nx-anonymous'). "
            "Serves as the primary key; immutable after creation."
        ),
    )

    # -- Descriptive Columns -------------------------------------------------

    name: str = Column(
        String(255),
        nullable=False,
        unique=True,
        doc="Human-readable role name.  Must be unique across all roles.",
    )

    description: Optional[str] = Column(
        Text,
        nullable=True,
        doc="Optional detailed description of the role's purpose.",
    )

    # -- Privilege Aggregation -----------------------------------------------

    privileges: Optional[list] = Column(
        JSON,
        nullable=True,
        default=list,
        doc=(
            "JSON array of privilege IDs associated with this role. "
            "Example: ['nx-all', 'nx-repository-admin-*', 'nx-component-upload']. "
            "Defaults to an empty list."
        ),
    )

    # -- Source Classification -----------------------------------------------

    source: str = Column(
        String(50),
        nullable=False,
        default="internal",
        server_default="internal",
        doc=(
            "Role source classification: 'internal' for locally-created roles, "
            "'external' for roles mapped from LDAP/AD or SSO providers."
        ),
    )

    # -- Relationships -------------------------------------------------------

    users = db.relationship(
        "User",
        secondary="role_assignments",
        back_populates="roles",
        lazy="dynamic",
        doc=(
            "Many-to-many relationship to User via the role_assignments "
            "junction table.  Uses 'dynamic' lazy loading for efficient "
            "querying on large user sets."
        ),
    )

    # -- Properties ----------------------------------------------------------

    @property
    def privilege_list(self) -> List[str]:
        """Return the ``privileges`` JSON column as a Python list of strings.

        If ``privileges`` is ``None`` (not set) or not a list, returns an
        empty list to guarantee safe iteration.

        Returns:
            A list of privilege ID strings (possibly empty).
        """
        if self.privileges is None:
            return []
        if isinstance(self.privileges, list):
            return list(self.privileges)
        return []

    @property
    def is_internal(self) -> bool:
        """Check whether this role was created locally.

        Returns:
            ``True`` if the role's source is ``'internal'``, ``False`` if
            the role was mapped from an external identity provider (LDAP/SSO).
        """
        return self.source == "internal"

    # -- Instance Methods ----------------------------------------------------

    def has_privilege(self, privilege_id: str) -> bool:
        """Check whether this role contains a specific privilege.

        Performs a membership test against the ``privileges`` JSON array.
        The check is case-sensitive and requires an exact match.

        Args:
            privilege_id: The privilege identifier to look for (e.g.,
                ``'nx-all'``, ``'nx-repository-admin-maven2-*'``).

        Returns:
            ``True`` if the privilege ID is present in this role's
            privilege list, ``False`` otherwise.
        """
        if not privilege_id:
            return False
        return privilege_id in self.privilege_list

    def to_dict(self) -> dict[str, Any]:
        """Convert this Role instance to a plain dictionary.

        Includes all column values plus derived properties
        (``privilege_list``, ``is_internal``).  ``datetime`` values are
        serialised to ISO-8601 strings for JSON compatibility.

        Returns:
            A dictionary representation suitable for API serialisation.
        """
        result: dict[str, Any] = {}

        # Serialize primary columns
        result["role_id"] = self.role_id
        result["name"] = self.name
        result["description"] = self.description
        result["privileges"] = self.privilege_list
        result["source"] = self.source

        # Serialize attributes from JSONAttributesMixin
        result["attributes"] = self.attributes if self.attributes else {}

        # Serialize timestamps from TimestampMixin
        if hasattr(self, "created_at") and self.created_at is not None:
            result["created_at"] = (
                self.created_at.isoformat()
                if isinstance(self.created_at, datetime)
                else self.created_at
            )
        else:
            result["created_at"] = None

        if hasattr(self, "updated_at") and self.updated_at is not None:
            result["updated_at"] = (
                self.updated_at.isoformat()
                if isinstance(self.updated_at, datetime)
                else self.updated_at
            )
        else:
            result["updated_at"] = None

        # Derived properties
        result["is_internal"] = self.is_internal

        return result

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Includes the role_id and name for quick identification during
        debugging and logging.
        """
        return f"<Role {self.role_id}: {self.name}>"


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------
logger.debug(
    "Role model module loaded — exports: %s",
    ", ".join(__all__),
)
