"""
Repository SQLAlchemy Model.

This module defines the ``Repository`` SQLAlchemy model — the **central entity**
of the Nexus Repository system.  Every component, asset, and BlobStore reference
traces back to a Repository instance.  The model replaces the ``REPOSITORY``
entity from the DataStore schema (Section 6.2.1.2) and the
``RepositoryImpl.java`` lifecycle management from the original Java source.

**Feature Support:**

- **F-101** (Multi-Format Repository Support): Seven repository formats are
  supported — ``maven2``, ``npm``, ``docker``, ``nuget``, ``pypi``, ``apt``,
  ``raw``.  The ``format`` column determines which format handler blueprint
  (in ``src.app.formats``) processes requests for this repository.
- **F-102** (Repository Types): Three repository types — ``hosted`` (serves
  from local BlobStore only), ``proxy`` (caches from remote sources with
  negative cache), ``group`` (aggregates ordered member repositories).
- **F-404** (System Configuration Management): Repository-level configuration
  is stored in the extensible ``attributes`` JSON column (inherited from
  ``JSONAttributesMixin``).

**Architecture Context:**

Replaces the following Java components:
    - ``RepositoryImpl.java`` — lifecycle state machine (NEW → STARTED → STOPPED)
    - MyBatis 3.5.15 mappers for the ``REPOSITORY`` table
    - Repository recipe / factory patterns for format-specific creation

**Compatibility:**

Designed to support **both** SQLite (standalone / zero-config deployments) and
PostgreSQL (clustered / enterprise deployments) via portable SQLAlchemy types.

**Primary Key:**

``name`` (String) — *not* an auto-incrementing integer — matching the source
DataStore schema where repositories are uniquely identified by their name
(e.g., ``'maven-central'``, ``'npm-hosted'``, ``'docker-hub-proxy'``).
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from sqlalchemy import Boolean, Column, JSON, String, Text
from sqlalchemy.orm import relationship

from src.app.extensions import db
from src.app.models.base import (
    BaseModel,
    JSONAttributesMixin,
    SoftDeleteMixin,
    TimestampMixin,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — Valid Domain Values
# ---------------------------------------------------------------------------
# These constants define the valid values for the ``format`` and ``type``
# columns.  They are used for documentation, validation, and as a single
# source of truth across the application.
# ---------------------------------------------------------------------------

VALID_FORMATS: tuple[str, ...] = (
    "maven2",
    "npm",
    "docker",
    "nuget",
    "pypi",
    "apt",
    "raw",
)
"""Supported repository formats (F-101).

Each format maps to a corresponding handler blueprint in ``src.app.formats/``.
"""

VALID_TYPES: tuple[str, ...] = (
    "hosted",
    "proxy",
    "group",
)
"""Supported repository types (F-102).

Resolution strategies:
    - ``hosted``: Serves artifacts from the local BlobStore only.
    - ``proxy``: Caches artifacts from a remote source (with negative cache).
    - ``group``: Aggregates ordered member repositories for unified access.
"""


# ===========================================================================
# Repository Model
# ===========================================================================


class Repository(BaseModel, TimestampMixin, SoftDeleteMixin, JSONAttributesMixin):
    """SQLAlchemy model representing a repository in the Nexus Repository system.

    A repository is the fundamental organisational unit.  It:
        - Belongs to exactly one **format** (e.g., Maven, npm, Docker).
        - Has exactly one **type** that determines resolution behaviour.
        - Stores content in a named **BlobStore**.
        - Contains zero or more **Components** (logical packages / artifacts).
        - Contains zero or more **Assets** (physical files).
        - Carries format-specific and type-specific configuration in its
          ``attributes`` JSON column.

    Inherits from:
        - ``BaseModel``: ``to_dict()``, ``save()``, ``delete()``, ``update()``
        - ``TimestampMixin``: ``created_at``, ``updated_at``
        - ``SoftDeleteMixin``: ``is_deleted``, ``deleted_at``, ``soft_delete()``,
          ``restore()``, ``is_active``
        - ``JSONAttributesMixin``: ``attributes``, ``get_attribute()``,
          ``set_attribute()``, ``remove_attribute()``, ``merge_attributes()``

    Example::

        repo = Repository(
            name="maven-central",
            format="maven2",
            type="proxy",
            blob_store_name="default",
            online=True,
        )
        repo.set_attribute("proxy", {
            "remoteUrl": "https://repo1.maven.org/maven2/",
            "contentMaxAge": 1440,
            "metadataMaxAge": 1440,
        })
        repo.save()
    """

    __tablename__: str = "repositories"

    # -- Columns (per AAP Section 0.2.3 REPOSITORY entity) -------------------

    name: str = Column(
        String(200),
        primary_key=True,
        doc=(
            "Unique repository name (e.g., 'maven-central', 'npm-hosted'). "
            "Primary key — NOT an auto-incrementing integer."
        ),
    )

    format: str = Column(
        String(50),
        nullable=False,
        doc=(
            "Repository format type. Valid values: "
            + ", ".join(f"'{f}'" for f in VALID_FORMATS)
            + ". Maps to format handler blueprints in src/app/formats/."
        ),
    )

    type: str = Column(
        String(20),
        nullable=False,
        doc=(
            "Repository type determining resolution strategy. Valid values: "
            + ", ".join(f"'{t}'" for t in VALID_TYPES)
            + "."
        ),
    )

    blob_store_name: str = Column(
        String(200),
        nullable=False,
        doc=(
            "Logical FK reference to the BlobStore name "
            "(blobstore_configs.blob_store_name). Not enforced as a DB-level FK "
            "for operational flexibility."
        ),
    )

    online: bool = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        doc="Whether the repository is accepting requests.",
    )

    # ``attributes`` JSON column is inherited from JSONAttributesMixin.
    # It stores format-specific and type-specific configuration:
    #   - Proxy: {"proxy": {"remoteUrl": ..., "contentMaxAge": 1440, ...},
    #             "negativeCache": {"enabled": true, "timeToLive": 1440}}
    #   - Group: {"group": {"memberNames": ["maven-releases", "maven-central"]}}
    #   - Hosted: {"storage": {"writePolicy": "ALLOW",
    #              "strictContentTypeValidation": true}}

    routing_rule: Optional[str] = Column(
        String(200),
        nullable=True,
        doc="Optional routing rule name for request routing.",
    )

    cleanup_policies = Column(
        JSON,
        nullable=True,
        doc=(
            "JSON array of cleanup policy names applied to this repository. "
            'Example: ["cleanup-snapshots", "cleanup-stale"].'
        ),
    )

    # -- Relationships -------------------------------------------------------
    # One-to-many relationship to Components.
    # cascade='all, delete-orphan' ensures components are cleaned up when
    # the repository is deleted (hard delete).  lazy='dynamic' returns a
    # query object for efficient filtering and counting.

    components = db.relationship(
        "Component",
        back_populates="repository",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    # One-to-many relationship to Assets.

    assets = db.relationship(
        "Asset",
        back_populates="repository",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    # -- Indexes and Constraints ---------------------------------------------
    # Performance indexes for common query patterns:
    #   - ix_repositories_format: filter by format (e.g., list all Maven repos)
    #   - ix_repositories_type: filter by type (e.g., list all proxy repos)
    #   - ix_repositories_format_type: composite filter (e.g., Maven + proxy)
    #   - ix_repositories_online: filter by online/offline status

    __table_args__ = (
        db.Index("ix_repositories_format", "format"),
        db.Index("ix_repositories_type", "type"),
        db.Index("ix_repositories_format_type", "format", "type"),
        db.Index("ix_repositories_online", "online"),
    )

    # -- String Representation -----------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Format: ``<Repository maven-central (maven2/proxy)>``
        """
        return f"<Repository {self.name} ({self.format}/{self.type})>"

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this Repository instance to a plain dictionary.

        Extends the base ``BaseModel.to_dict()`` method with computed property
        values (type flags, remote URL, group members, content counts) for
        complete serialization.

        Returns:
            A dictionary containing all column values and computed properties.
        """
        result: dict[str, Any] = super().to_dict()

        # Append computed properties for API consumers
        result["is_hosted"] = self.is_hosted
        result["is_proxy"] = self.is_proxy
        result["is_group"] = self.is_group
        result["remote_url"] = self.remote_url
        result["group_members"] = self.group_members
        result["component_count"] = self.component_count
        result["asset_count"] = self.asset_count
        result["is_active"] = self.is_active

        return result

    # -- Type Properties -----------------------------------------------------

    @property
    def is_hosted(self) -> bool:
        """Return ``True`` if this is a **hosted** repository.

        Hosted repositories serve artifacts exclusively from the local
        BlobStore.  Content is published (uploaded) directly by clients.
        """
        return self.type == "hosted"

    @property
    def is_proxy(self) -> bool:
        """Return ``True`` if this is a **proxy** repository.

        Proxy repositories cache artifacts fetched from a remote source URL.
        They maintain a negative cache for missing artifacts and honour
        content / metadata max-age settings.
        """
        return self.type == "proxy"

    @property
    def is_group(self) -> bool:
        """Return ``True`` if this is a **group** repository.

        Group repositories aggregate content from an ordered list of member
        repositories.  Resolution walks the member list in order, returning
        the first match.
        """
        return self.type == "group"

    # -- Attribute-Derived Properties ----------------------------------------

    @property
    def remote_url(self) -> Optional[str]:
        """Extract the remote URL from attributes for **proxy** repositories.

        The remote URL is stored at ``attributes["proxy"]["remoteUrl"]``.

        Returns:
            The remote URL string for proxy repos, or ``None`` for hosted /
            group repositories.
        """
        if not self.is_proxy:
            return None
        proxy_attrs: Optional[dict] = self.get_attribute("proxy")
        if proxy_attrs is None or not isinstance(proxy_attrs, dict):
            return None
        return proxy_attrs.get("remoteUrl")

    @property
    def group_members(self) -> List[str]:
        """Extract member repository names from attributes for **group** repos.

        The member list is stored at ``attributes["group"]["memberNames"]``.

        Returns:
            A list of member repository name strings for group repos, or an
            empty list for hosted / proxy repositories.
        """
        if not self.is_group:
            return []
        group_attrs: Optional[dict] = self.get_attribute("group")
        if group_attrs is None or not isinstance(group_attrs, dict):
            return []
        member_names = group_attrs.get("memberNames")
        if member_names is None or not isinstance(member_names, list):
            return []
        return list(member_names)

    # -- Content Count Properties --------------------------------------------

    @property
    def component_count(self) -> int:
        """Return the count of components associated with this repository.

        Uses the dynamic relationship's ``.count()`` method for an efficient
        SQL ``COUNT(*)`` query rather than loading all objects into memory.

        Returns:
            Integer count of components.
        """
        try:
            return self.components.count()
        except Exception:
            # Gracefully handle cases where the relationship is not yet
            # bound to a session (e.g., detached or transient instances).
            logger.debug(
                "Could not count components for repository '%s'; returning 0.",
                self.name,
            )
            return 0

    @property
    def asset_count(self) -> int:
        """Return the count of assets associated with this repository.

        Uses the dynamic relationship's ``.count()`` method for an efficient
        SQL ``COUNT(*)`` query rather than loading all objects into memory.

        Returns:
            Integer count of assets.
        """
        try:
            return self.assets.count()
        except Exception:
            # Gracefully handle cases where the relationship is not yet
            # bound to a session (e.g., detached or transient instances).
            logger.debug(
                "Could not count assets for repository '%s'; returning 0.",
                self.name,
            )
            return 0


# ---------------------------------------------------------------------------
# Module-Level Export List
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "Repository",
    "VALID_FORMATS",
    "VALID_TYPES",
]

logger.debug(
    "Repository model module loaded — exports: %s",
    ", ".join(__all__),
)
