"""
Privilege SQLAlchemy Model.

This module defines the ``Privilege`` model that represents the PRIVILEGE entity
from the DataStore schema (Section 6.2.1.2 of the Technical Specification).
Privileges are the **atomic permissions** in the Role-Based Access Control
(RBAC) system — they define what actions are permitted on which resources.

**RBAC Hierarchy:**

    Users ─┬─ are assigned to ─► Roles ─┬─ aggregate ─► Privileges
           │                             │
           └─ inherit effective ─────────┘    (atomic permissions)

**Three-Tier Authorization Model (Feature F-301):**

    ┌────────────────────────────────────────────────────────────┐
    │ Tier 1 — System-wide:                                     │
    │   • 'application' type  (e.g., user management, settings) │
    │   • 'wildcard' type     (e.g., nx-all — super-admin)      │
    ├────────────────────────────────────────────────────────────┤
    │ Tier 2 — Repository-scoped:                               │
    │   • 'repository-admin' type  (repo admin operations)      │
    │   • 'repository-view' type   (read access to content)     │
    ├────────────────────────────────────────────────────────────┤
    │ Tier 3 — Sub-repository:                                  │
    │   • 'repository-content-selector' type (CSEL expressions) │
    └────────────────────────────────────────────────────────────┘

**Architecture Context:**
Replaces the PRIVILEGE entity from the Java DataStore with MyBatis 3.5.15
mappers.  The ``properties`` JSON column stores privilege-specific parameters
that vary by type, providing the same flexibility as the Java source while
leveraging SQLAlchemy's portable JSON type for both SQLite and PostgreSQL.

**Cross-Module Usage:**
- The ``Role`` model's ``privileges`` JSON array references privilege IDs.
- ``src.app.auth.authorization.py`` calls ``matches()`` during RBAC evaluation.
- ``src.app.api.privileges.py`` exposes CRUD endpoints for privilege management.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any, List, Optional

from sqlalchemy import Column, JSON, String, Text

from src.app.extensions import db
from src.app.models.base import BaseModel, JSONAttributesMixin, TimestampMixin

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

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

#: Privilege types that are scoped to specific repositories and formats.
REPOSITORY_SCOPED_TYPES: frozenset[str] = frozenset(
    {
        "repository-admin",
        "repository-content-selector",
        "repository-view",
    }
)


# ===========================================================================
# Privilege Model
# ===========================================================================


class Privilege(BaseModel, TimestampMixin, JSONAttributesMixin):
    """SQLAlchemy model representing an RBAC privilege descriptor.

    A privilege is the finest-grained permission unit in the security system.
    Privileges are aggregated into roles, which are then assigned to users.
    The ``properties`` JSON column stores type-specific parameters that define
    the exact scope of each privilege.

    **Privilege ID Format:**
        ``nx-<scope>-<action>-<format>-<repository>`` (varies by type)

    **Properties JSON Structures (by type):**

    - **application**::

          {"domain": "users", "actions": ["create", "read", "update", "delete"]}

    - **repository-admin**::

          {"format": "docker", "repository": "docker-hosted"}

    - **repository-view**::

          {"format": "maven2", "repository": "*", "actions": ["read", "browse"]}

    - **repository-content-selector**::

          {"format": "npm", "repository": "*",
           "contentSelector": "my-selector", "actions": ["read"]}

    - **wildcard**::

          {}  (no properties — grants all access)

    Attributes:
        privilege_id: Unique string identifier for this privilege.
        type: One of the five privilege type descriptors.
        name: Human-readable privilege name.
        description: Optional detailed description.
        properties: JSON dict of type-specific parameters.
        attributes: Extensible JSON metadata column (from mixin).
        created_at: UTC timestamp of record creation (from mixin).
        updated_at: UTC timestamp of last modification (from mixin).
    """

    __tablename__: str = "privileges"

    # -- Table-Level Indexes -------------------------------------------------
    # Indexes on ``type`` and ``name`` accelerate the frequent privilege lookup
    # queries executed during RBAC authorization evaluation (Feature F-301).
    __table_args__ = (
        db.Index("ix_privileges_type", "type"),
        db.Index("ix_privileges_name", "name"),
    )

    # -- Columns -------------------------------------------------------------

    privilege_id: str = Column(
        String(300),
        primary_key=True,
        doc=(
            "Unique privilege identifier.  Follows the pattern "
            "'nx-<scope>-<action>-<format>-<repository>' or a simpler form "
            "for wildcard / application privileges (e.g. 'nx-all')."
        ),
    )

    type: str = Column(
        String(100),
        nullable=False,
        doc=(
            "Privilege type descriptor.  Determines which RBAC tier this "
            "privilege belongs to and how the ``properties`` JSON is "
            "interpreted.  Valid values: "
            "'application', 'repository-admin', 'repository-content-selector', "
            "'repository-view', 'wildcard'."
        ),
    )

    name: str = Column(
        String(255),
        nullable=False,
        doc=(
            "Human-readable privilege name displayed in the administration UI.  "
            "Example: 'nx-repository-admin - (maven2 - *)'."
        ),
    )

    description: Optional[str] = Column(
        Text,
        nullable=True,
        doc="Optional detailed description of what this privilege grants.",
    )

    properties: Optional[dict] = Column(
        JSON,
        nullable=True,
        doc=(
            "Type-specific parameters that define the exact scope of this "
            "privilege.  Structure varies by ``type`` — see class docstring."
        ),
    )

    # ``attributes``, ``created_at``, and ``updated_at`` are provided by the
    # JSONAttributesMixin and TimestampMixin respectively.

    # -- Computed Properties --------------------------------------------------

    @property
    def actions(self) -> List[str]:
        """Extract the ``actions`` list from the ``properties`` JSON.

        Actions represent the specific operations this privilege grants.
        Common action values: ``'read'``, ``'browse'``, ``'edit'``,
        ``'delete'``, ``'create'``, ``'update'``.

        Returns:
            A list of action strings.  Returns an empty list if
            ``properties`` is ``None`` or does not contain an ``'actions'``
            key.
        """
        if self.properties is None:
            return []
        actions_value = self.properties.get("actions")
        if isinstance(actions_value, list):
            return list(actions_value)
        return []

    @property
    def target_format(self) -> Optional[str]:
        """Extract the ``format`` field from the ``properties`` JSON.

        The format specifies which repository format this privilege applies
        to (e.g., ``'maven2'``, ``'docker'``, ``'npm'``).  A value of
        ``'*'`` denotes all formats.

        Returns:
            The format string, or ``None`` if not present in ``properties``.
        """
        if self.properties is None:
            return None
        return self.properties.get("format")

    @property
    def target_repository(self) -> Optional[str]:
        """Extract the ``repository`` field from the ``properties`` JSON.

        The repository specifies which repository this privilege applies to.
        A value of ``'*'`` denotes all repositories of the matching format.

        Returns:
            The repository name, or ``None`` if not present in ``properties``.
        """
        if self.properties is None:
            return None
        return self.properties.get("repository")

    @property
    def is_wildcard(self) -> bool:
        """Check if this is a wildcard privilege that grants all access.

        Wildcard privileges (type ``'wildcard'``, e.g., ``nx-all``) belong
        to RBAC Tier 1 and bypass all repository-level authorization checks.

        Returns:
            ``True`` if this privilege has type ``'wildcard'``.
        """
        return self.type == "wildcard"

    # -- Instance Methods ----------------------------------------------------

    def matches(
        self,
        format_name: str,
        repository_name: str,
        action: str,
    ) -> bool:
        """Determine if this privilege grants the specified action.

        Implements the privilege matching logic used during RBAC authorization
        evaluation in ``src.app.auth.authorization``.  Supports wildcard
        ``'*'`` patterns in the ``format`` and ``repository`` fields of the
        ``properties`` JSON, as well as ``fnmatch``-style glob patterns.

        **Matching Rules by Privilege Type:**

        - **wildcard**: Always matches — grants all access.
        - **application**: Never matches repository-level checks (system-wide
          scope only; use separate application-level authorization).
        - **repository-admin**: Matches on format and repository.  Admin
          privileges have no action restrictions — if format and repository
          match, all actions are implicitly granted.
        - **repository-view**: Matches on format, repository, *and* action.
          The action must be present in the ``actions`` list.
        - **repository-content-selector**: Same matching as repository-view,
          with additional CSEL expression evaluation handled upstream by
          ``src.app.auth.content_selector``.

        Args:
            format_name: Repository format to check (e.g., ``'maven2'``,
                ``'docker'``, ``'npm'``).
            repository_name: Repository name to check.
            action: Action to check (e.g., ``'read'``, ``'browse'``,
                ``'edit'``, ``'delete'``, ``'create'``).

        Returns:
            ``True`` if this privilege grants the specified action on the
            given repository/format combination; ``False`` otherwise.
        """
        # Tier 1 — Wildcard: matches everything unconditionally
        if self.is_wildcard:
            return True

        # Tier 1 — Application: system-wide scope; does not match
        # repository-level authorization checks
        if self.type == "application":
            return False

        # Tier 2 & 3 — Repository-scoped privileges
        # Check format match (supports '*' wildcard and glob patterns)
        priv_format: Optional[str] = self.target_format
        if priv_format is not None and priv_format != "*":
            if not fnmatch.fnmatch(format_name, priv_format):
                return False

        # Check repository match (supports '*' wildcard and glob patterns)
        priv_repo: Optional[str] = self.target_repository
        if priv_repo is not None and priv_repo != "*":
            if not fnmatch.fnmatch(repository_name, priv_repo):
                return False

        # Check action match — only enforce if privilege specifies actions.
        # Repository-admin privileges typically omit actions (granting all).
        priv_actions: List[str] = self.actions
        if priv_actions and action not in priv_actions:
            return False

        return True

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this privilege to a dictionary representation.

        Extends ``BaseModel.to_dict()`` by including computed properties
        (``actions``, ``target_format``, ``target_repository``,
        ``is_wildcard``) in addition to all mapped columns.

        Returns:
            A dictionary suitable for JSON serialization.
        """
        result: dict[str, Any] = super().to_dict()
        result["actions"] = self.actions
        result["target_format"] = self.target_format
        result["target_repository"] = self.target_repository
        result["is_wildcard"] = self.is_wildcard
        return result

    # -- Representation ------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Format: ``<Privilege nx-all (wildcard)>``
        """
        return f"<Privilege {self.privilege_id} ({self.type})>"


logger.debug(
    "Privilege model loaded — tablename=%s, valid_types=%s",
    Privilege.__tablename__,
    ", ".join(sorted(VALID_PRIVILEGE_TYPES)),
)
