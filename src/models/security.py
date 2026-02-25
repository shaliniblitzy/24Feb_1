"""
Security data models for the Binary Repository Management System.

Implements the 3-tier RBAC (Role-Based Access Control) system:

- **Tier 1 — Global Roles**: System-wide role definitions (admin, developer,
  readonly) that assign broad privilege sets to users.
- **Tier 2 — Privileges**: Granular permission definitions that specify
  allowed actions (READ, BROWSE, EDIT, DELETE) within specific domains
  (repository, component, system).
- **Tier 3 — Content Selectors**: CSEL-expression-based rules that filter
  which content within a repository a privilege applies to, enabling
  fine-grained path-level or format-level access control.

Models defined:

- :class:`Role` — Named role with an associated set of privileges
- :class:`Privilege` — Named permission with actions and domain scope
- :class:`ContentSelector` — CSEL expression for content-level filtering

Usage::

    from src.models.security import Role, Privilege, ContentSelector

    admin_role = Role(name="nx-admin", description="Full system access")
    read_priv = Privilege(name="nx-repository-view", type="repository",
                          actions=["READ", "BROWSE"])
    maven_selector = ContentSelector(
        name="maven-only",
        expression='format == "maven2" and path =^ "/com/example"',
    )
"""

import datetime
import json
import uuid
from typing import Any, Dict, List, Optional

from src.extensions import db

# ---------------------------------------------------------------------------
# Association table: many-to-many relationship between Role and Privilege
# ---------------------------------------------------------------------------
role_privileges = db.Table(
    "role_privileges",
    db.Column(
        "role_id",
        db.String(36),
        db.ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "privilege_id",
        db.String(36),
        db.ForeignKey("privileges.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Role(db.Model):
    """Global role definition in the RBAC system.

    Roles represent named collections of privileges that can be assigned
    to users either globally or scoped to specific repositories.

    Attributes
    ----------
    id : str
        UUID primary key (36-character string).
    name : str
        Unique role name (e.g. ``nx-admin``, ``nx-developer``).
    description : str or None
        Human-readable description of the role's purpose.
    source : str
        Origin of the role definition (``default`` or ``user-defined``).
    created_at : datetime.datetime
        Timestamp when the role was created.
    updated_at : datetime.datetime
        Timestamp of the last modification.
    privileges : list[Privilege]
        Ordered collection of privileges assigned to this role.
    """

    __tablename__ = "roles"

    id: str = db.Column(
        db.String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        doc="UUID primary key",
    )
    name: str = db.Column(
        db.String(255),
        unique=True,
        nullable=False,
        index=True,
        doc="Unique role name",
    )
    description: Optional[str] = db.Column(
        db.String(500),
        nullable=True,
        doc="Human-readable description of the role",
    )
    source: str = db.Column(
        db.String(50),
        nullable=False,
        default="default",
        doc="Origin of the role definition",
    )
    created_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp when the role was created",
    )
    updated_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp of the last modification",
    )

    # Many-to-many relationship with Privilege
    privileges = db.relationship(
        "Privilege",
        secondary=role_privileges,
        backref=db.backref("roles", lazy="dynamic"),
        lazy="select",
    )

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return f"<Role id={self.id!r} name={self.name!r}>"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the role to a dictionary for API responses.

        Returns
        -------
        dict
            Dictionary representation including privilege names.
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "privileges": [p.name for p in self.privileges],
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }


class Privilege(db.Model):
    """Granular permission definition in the RBAC system.

    Privileges define specific actions that can be performed within a
    particular domain.  They are assigned to roles and, through roles,
    to users.

    Attributes
    ----------
    id : str
        UUID primary key (36-character string).
    name : str
        Privilege identifier (e.g. ``nx-repository-view``).
    description : str or None
        Human-readable description.
    type : str or None
        Domain scope (``repository``, ``component``, ``system``).
    actions : list or None
        JSON-encoded list of permitted action strings.
    created_at : datetime.datetime
        Timestamp when the privilege was created.
    updated_at : datetime.datetime
        Timestamp of the last modification.
    """

    __tablename__ = "privileges"

    id: str = db.Column(
        db.String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        doc="UUID primary key",
    )
    name: str = db.Column(
        db.String(255),
        nullable=False,
        index=True,
        doc="Privilege identifier",
    )
    description: Optional[str] = db.Column(
        db.String(500),
        nullable=True,
        doc="Human-readable description",
    )
    type: Optional[str] = db.Column(
        db.String(50),
        nullable=True,
        doc="Domain scope (repository, component, system)",
    )
    _actions: Optional[str] = db.Column(
        "actions",
        db.Text,
        nullable=True,
        doc="JSON-encoded list of permitted actions",
    )
    created_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp when the privilege was created",
    )
    updated_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp of the last modification",
    )

    @property
    def actions(self) -> List[str]:
        """Return the list of permitted action strings.

        Deserializes the JSON-encoded ``_actions`` column.  Returns an
        empty list when the column is ``NULL`` or empty.
        """
        if self._actions:
            try:
                return json.loads(self._actions)
            except (json.JSONDecodeError, TypeError):
                return []
        return []

    @actions.setter
    def actions(self, value: Optional[List[str]]) -> None:
        """Set the list of permitted action strings.

        Serializes the provided list to JSON for storage.
        """
        if value is None:
            self._actions = None
        else:
            self._actions = json.dumps(value)

    def __init__(self, **kwargs: Any) -> None:
        """Initialize a Privilege, handling the actions list."""
        actions_value = kwargs.pop("actions", None)
        super().__init__(**kwargs)
        if actions_value is not None:
            self.actions = actions_value

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return f"<Privilege id={self.id!r} name={self.name!r}>"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the privilege to a dictionary for API responses.

        Returns
        -------
        dict
            Dictionary representation of the privilege.
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "actions": self.actions,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }


class ContentSelector(db.Model):
    """CSEL-expression-based content filter for fine-grained RBAC.

    Content selectors allow administrators to define rules that restrict
    which repository content a privilege applies to, based on artifact
    format, path prefixes, or other metadata attributes.

    Attributes
    ----------
    id : str
        UUID primary key (36-character string).
    name : str
        Unique selector name.
    description : str or None
        Human-readable description.
    type : str
        Selector type (default ``csel``).
    expression : str
        CSEL filter expression (e.g. ``format == "maven2"``).
    created_at : datetime.datetime
        Timestamp when the selector was created.
    updated_at : datetime.datetime
        Timestamp of the last modification.
    """

    __tablename__ = "content_selectors"

    id: str = db.Column(
        db.String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        doc="UUID primary key",
    )
    name: str = db.Column(
        db.String(255),
        nullable=False,
        index=True,
        doc="Unique selector name",
    )
    description: Optional[str] = db.Column(
        db.String(500),
        nullable=True,
        doc="Human-readable description",
    )
    type: str = db.Column(
        db.String(50),
        nullable=False,
        default="csel",
        doc="Selector type",
    )
    expression: str = db.Column(
        db.Text,
        nullable=False,
        doc="CSEL filter expression",
    )
    created_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp when the selector was created",
    )
    updated_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp of the last modification",
    )

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return f"<ContentSelector id={self.id!r} name={self.name!r}>"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the content selector to a dictionary for API responses.

        Returns
        -------
        dict
            Dictionary representation of the content selector.
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "expression": self.expression,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }
