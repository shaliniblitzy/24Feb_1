"""
Component SQLAlchemy Model.

This module defines the ``Component`` SQLAlchemy model — representing logical
packages or artifacts within a repository.  In a binary repository manager,
a **component** is the conceptual grouping of one or more **assets** (files)
that together form a publishable unit.  For example:

- **Maven**: A component is a GAV coordinate (groupId:artifactId:version);
  its assets include the JAR, POM, sources JAR, and checksum files.
- **npm**: A component is a package name + version; its assets include the
  tarball and the package metadata JSON.
- **Docker**: A component is an image name + tag/digest; its assets include
  manifests and layer blobs.
- **NuGet**: A component is a package ID + version; its asset is the ``.nupkg``.
- **PyPI**: A component is a project name + version; its assets include
  sdists and wheels.
- **APT**: A component is a package name + version + architecture; its asset
  is the ``.deb`` file.
- **Raw**: A component is the filename; its asset is the binary file.

This model replaces the ``COMPONENT`` entity from the DataStore schema
(Section 6.2.1.2) and the MyBatis 3.5.15 mapper for the ``COMPONENT`` table
in the original Java source system (Sonatype Nexus Repository).

**Feature Support:**

- **F-101** (Multi-Format Repository Support): Components store format-specific
  metadata in the extensible ``attributes`` JSON column (via
  ``JSONAttributesMixin``).  The ``namespace`` column captures format-specific
  grouping (Maven groupId, npm scope, Docker registry namespace).
- **F-103** (Content Indexing and Search): Components are indexed by
  ``repository_name``, ``namespace``, ``name``, and ``version`` for efficient
  lookup and full-text search integration.

**Architecture Context:**

Replaces the following Java components:
    - MyBatis 3.5.15 mappers for the ``COMPONENT`` table
    - DataStore API ``COMPONENT`` entity (Section 6.2.1.2)
    - Component coordinate resolution logic per format

**Compatibility:**

Designed to support **both** SQLite (standalone / zero-config deployments) and
PostgreSQL (clustered / enterprise deployments) via portable SQLAlchemy types.
No database-specific types (e.g., ``JSONB``, ``ARRAY``) are used.

**Coordinate Uniqueness:**

The tuple ``(repository_name, namespace, name, version)`` forms the unique
"coordinates" of a component within a repository.  A composite unique
constraint enforces this invariant at the database level.

**Relationships:**

- **Many-to-One** to ``Repository``: Every component belongs to exactly one
  repository, referenced via ``repository_name`` foreign key.
- **One-to-Many** to ``Asset``: A component owns zero or more assets with
  cascade delete-orphan semantics.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import Column, ForeignKey, Integer, String
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
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source.  Provides diagnostic output for model operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "Component",
]


# ===========================================================================
# Component Model
# ===========================================================================


class Component(BaseModel, TimestampMixin, SoftDeleteMixin, JSONAttributesMixin):
    """SQLAlchemy model representing a component (logical package/artifact).

    A component is the fundamental content unit in the repository system.  It
    groups one or more :class:`Asset` records (physical files) under a single
    logical identity described by **coordinates**: a tuple of
    ``(repository_name, namespace, name, version)``.

    Inherits from:
        - :class:`BaseModel`: ``to_dict()``, ``save()``, ``delete()``,
          ``update()``, ``__repr__()``
        - :class:`TimestampMixin`: ``created_at``, ``updated_at``
        - :class:`SoftDeleteMixin`: ``is_deleted``, ``deleted_at``,
          ``soft_delete()``, ``restore()``, ``is_active``, ``query_active()``
        - :class:`JSONAttributesMixin`: ``attributes``, ``get_attribute()``,
          ``set_attribute()``, ``remove_attribute()``, ``merge_attributes()``

    Example::

        component = Component(
            repository_name="maven-central",
            namespace="org.apache.commons",
            name="commons-lang3",
            version="3.14.0",
        )
        component.set_attribute("format", "maven2")
        component.set_attribute("packaging", "jar")
        component.save()
    """

    __tablename__: str = "components"

    # -- Columns (per AAP Section 0.2.3 COMPONENT entity) --------------------

    id: int = Column(
        Integer,
        primary_key=True,
        autoincrement=True,
        doc=(
            "Auto-incrementing surrogate primary key.  While the logical "
            "identity of a component is its coordinate tuple "
            "(repository_name, namespace, name, version), an integer PK "
            "is used for efficient joins and foreign-key references from "
            "the assets table."
        ),
    )

    repository_name: str = Column(
        String(200),
        ForeignKey("repositories.name"),
        nullable=False,
        index=True,
        doc=(
            "Foreign key reference to the owning repository.  Every "
            "component belongs to exactly one repository."
        ),
    )

    namespace: Optional[str] = Column(
        String(512),
        nullable=True,
        doc=(
            "Format-specific namespace for grouping components.  Examples:\n"
            "  - Maven: groupId (e.g., 'org.apache.commons')\n"
            "  - npm: scope (e.g., '@angular')\n"
            "  - Docker: registry namespace (e.g., 'library')\n"
            "  - NuGet: typically null\n"
            "  - PyPI: typically null\n"
            "  - APT: section (e.g., 'main')\n"
            "  - Raw: directory path prefix\n"
            "Nullable because not all formats use namespaces."
        ),
    )

    name: str = Column(
        String(512),
        nullable=False,
        doc=(
            "Component name — the core identifier within its namespace.  "
            "Examples:\n"
            "  - Maven: artifactId (e.g., 'commons-lang3')\n"
            "  - npm: package name (e.g., 'lodash')\n"
            "  - Docker: image name (e.g., 'nginx')\n"
            "  - NuGet: package ID (e.g., 'Newtonsoft.Json')\n"
            "  - PyPI: project name (e.g., 'flask')\n"
            "  - APT: package name (e.g., 'libssl-dev')\n"
            "  - Raw: filename (e.g., 'config.yaml')\n"
            "NOT nullable — every component has a name."
        ),
    )

    version: Optional[str] = Column(
        String(256),
        nullable=True,
        doc=(
            "Version string for this component.  Examples:\n"
            "  - Maven: '3.14.0', '1.0.0-SNAPSHOT'\n"
            "  - npm: '18.2.0'\n"
            "  - Docker: tag ('latest', 'v1.25.4') or digest\n"
            "  - NuGet: '13.0.3'\n"
            "  - PyPI: '3.1.3'\n"
            "  - APT: '3.0.13-1ubuntu1'\n"
            "Nullable because some component types may not have explicit "
            "versions (e.g., Docker images with digest-only references, "
            "or raw format files)."
        ),
    )

    # ``attributes`` JSON column is inherited from JSONAttributesMixin.
    # Stores format-specific metadata that varies by repository format:
    #   - Maven: {"groupId": "...", "artifactId": "...", "packaging": "jar",
    #             "classifier": "sources"}
    #   - npm: {"scope": "@angular", "dist-tags": {"latest": "18.2.0"}}
    #   - Docker: {"mediaType": "...", "digest": "sha256:..."}
    #   - NuGet: {"packageType": "Dependency", "authors": "..."}
    #   - PyPI: {"requires_python": ">=3.8", "summary": "..."}

    # -- Relationships -------------------------------------------------------

    repository = db.relationship(
        "Repository",
        back_populates="components",
        doc=(
            "Many-to-one relationship to the owning Repository.  "
            "Every component belongs to exactly one repository."
        ),
    )

    assets = db.relationship(
        "Asset",
        back_populates="component",
        cascade="all, delete-orphan",
        lazy="dynamic",
        doc=(
            "One-to-many relationship to Asset records (physical files).  "
            "cascade='all, delete-orphan' ensures assets are automatically "
            "deleted when the owning component is deleted.  lazy='dynamic' "
            "returns a query object for efficient filtering and counting."
        ),
    )

    # -- Indexes and Constraints ---------------------------------------------
    # The coordinate uniqueness constraint ensures that no two components in
    # the same repository can have identical (namespace, name, version) tuples.
    # Additional indexes optimize common query patterns:
    #   - ix_components_repo_name_version: lookup by repo + name + version
    #   - ix_components_namespace: filter by namespace (Maven groupId search)

    __table_args__ = (
        db.UniqueConstraint(
            "repository_name",
            "namespace",
            "name",
            "version",
            name="uq_component_coordinates",
        ),
        db.Index(
            "ix_components_repo_name_version",
            "repository_name",
            "name",
            "version",
        ),
        db.Index(
            "ix_components_namespace",
            "namespace",
        ),
    )

    # -- String Representation -----------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Format: ``<Component namespace:name:version in repository_name>``

        Parts that are ``None`` are rendered as empty strings so the output
        degrades gracefully for formats that don't use namespace or version.
        For example:
            - Maven: ``<Component org.apache.commons:commons-lang3:3.14.0 in maven-central>``
            - npm:   ``<Component @angular:core:18.2.0 in npm-hosted>``
            - Raw:   ``<Component None:config.yaml:None in raw-hosted>``
        """
        return (
            f"<Component {self.namespace}:{self.name}:{self.version} "
            f"in {self.repository_name}>"
        )

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this Component instance to a plain dictionary.

        Extends the base ``BaseModel.to_dict()`` method with computed property
        values (``coordinates``, ``asset_count``, ``is_active``) for complete
        serialization suitable for REST API responses.

        Returns:
            A dictionary containing all column values and computed properties.
        """
        result: dict[str, Any] = super().to_dict()

        # Append computed properties for API consumers
        result["coordinates"] = self.coordinates
        result["asset_count"] = self.asset_count
        result["is_active"] = self.is_active

        return result

    # -- Computed Properties -------------------------------------------------

    @property
    def coordinates(self) -> str:
        """Return the GAV-like coordinate string for this component.

        Coordinates are the canonical identifier for a component within a
        repository.  The format is ``namespace:name:version`` where ``None``
        parts are omitted (replaced with empty strings) for clean display.

        Examples:
            - Maven:  ``org.apache.commons:commons-lang3:3.14.0``
            - npm:    ``@angular:core:18.2.0``
            - Docker: ``library:nginx:1.25.4``
            - PyPI:   ``:flask:3.1.3`` (no namespace)
            - Raw:    ``:config.yaml:`` (no namespace or version)

        Returns:
            A colon-separated coordinate string.
        """
        ns: str = self.namespace if self.namespace is not None else ""
        ver: str = self.version if self.version is not None else ""
        return f"{ns}:{self.name}:{ver}"

    @property
    def asset_count(self) -> int:
        """Return the count of assets associated with this component.

        Uses the dynamic relationship's ``.count()`` method for an efficient
        SQL ``COUNT(*)`` query rather than loading all asset objects into
        memory.

        Returns:
            Integer count of assets belonging to this component.
        """
        try:
            return self.assets.count()
        except Exception:
            # Gracefully handle cases where the relationship is not yet
            # bound to a session (e.g., detached or transient instances).
            logger.debug(
                "Could not count assets for component id=%s (%s); returning 0.",
                self.id,
                self.name,
            )
            return 0


# ---------------------------------------------------------------------------
# Module Load Diagnostic
# ---------------------------------------------------------------------------
logger.debug(
    "Component model module loaded — exports: %s",
    ", ".join(__all__),
)
