"""
Asset SQLAlchemy Model.

This module defines the ``Asset`` model representing individual files stored
within a repository.  Each asset is linked to an optional ``Component`` (parent
artifact) and a mandatory ``Repository`` (the owning repository).

**Architecture Context:**
Replaces the ``ASSET`` entity from the DataStore schema (Section 6.2.1.2) in
the original Sonatype Nexus Repository Java source system, where assets were
mapped via MyBatis 3.5.15 XML mappers.  In this Python/Flask implementation,
SQLAlchemy's Declarative ORM provides the same data access capabilities with
significantly less boilerplate.

**Feature Coverage:**

+-------+-------------------------------------+------------------------------+
| ID    | Feature Name                        | Asset Role                   |
+=======+=====================================+==============================+
| F-101 | Multi-Format Repository Support     | Stores format-specific files |
+-------+-------------------------------------+------------------------------+
| F-103 | Content Indexing and Search          | Searchable path / checksums  |
+-------+-------------------------------------+------------------------------+
| F-204 | Cleanup Policies                    | ``last_downloaded`` drives   |
|       |                                     | retention decisions          |
+-------+-------------------------------------+------------------------------+
| F-201 | File BlobStore                      | ``blob_ref`` links to local  |
|       |                                     | filesystem storage           |
+-------+-------------------------------------+------------------------------+
| F-202 | S3 BlobStore                        | ``blob_ref`` links to S3 key |
+-------+-------------------------------------+------------------------------+

**Data Model Notes:**

- ``path`` uses ``String(2048)`` to accommodate deeply nested Maven coordinate
  paths (e.g., ``/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar``).
- ``size`` uses ``BigInteger`` to handle very large artifacts such as multi-GB
  Docker image layers.
- Checksum columns (``checksum_sha1``, ``checksum_sha256``, ``checksum_md5``)
  use fixed-length ``String`` columns matching the hex digest lengths of each
  hash algorithm (40, 64, and 32 characters respectively).
- ``blob_ref`` stores a reference to the binary blob in the configured BlobStore
  backend (local filesystem path or S3 object key).
- ``last_downloaded`` is the primary field used by cleanup policy (F-204)
  criteria such as ``last_downloaded_before`` to identify stale artifacts.
- ``attributes`` (from ``JSONAttributesMixin``) stores format-specific metadata
  that varies by repository format (Maven GAV, npm scope, Docker manifest
  digest, NuGet package metadata, etc.).

**Dual-Database Compatibility:**
All column types are portable across SQLite (standalone) and PostgreSQL
(clustered/enterprise), per AAP Section 0.7.1.  No database-specific types
(``JSONB``, ``ARRAY``, etc.) are used.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String
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
# Replaces SLF4J 1.7.36 + Logback 1.2.13 per-entity logging from the Java
# source.  Provides structured logging for asset lifecycle operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = ["Asset"]


# ===========================================================================
# Asset Model
# ===========================================================================


class Asset(BaseModel, TimestampMixin, SoftDeleteMixin, JSONAttributesMixin):
    """Represents a stored file or artifact within a repository.

    Each asset corresponds to a single file stored in the repository's
    associated BlobStore.  Assets may optionally be grouped under a
    ``Component`` (e.g., a Maven artifact with its POM, JAR, and sources)
    or exist independently (e.g., repository-level metadata files).

    **Relationships:**

    - ``component`` (many-to-one, nullable): The parent component that this
      asset belongs to.  ``None`` for standalone metadata files.
    - ``repository`` (many-to-one, required): The repository that owns this
      asset.  Every asset must belong to exactly one repository.

    **Inherited Capabilities:**

    - ``BaseModel``: ``to_dict()``, ``save()``, ``delete()``, ``update()``
    - ``TimestampMixin``: ``created_at``, ``updated_at``
    - ``SoftDeleteMixin``: ``is_deleted``, ``deleted_at``, ``soft_delete()``,
      ``restore()``, ``is_active``, ``query_active()``
    - ``JSONAttributesMixin``: ``attributes``, ``get_attribute()``,
      ``set_attribute()``, ``remove_attribute()``, ``merge_attributes()``

    **Table Indexes:**

    - Composite unique index on ``(repository_name, path)`` — each path must
      be unique within a repository.
    - Index on ``last_downloaded`` for efficient cleanup policy evaluation.
    - Index on ``content_type`` for format-specific queries.
    - Index on ``component_id`` for component → assets lookups.
    - Index on ``repository_name`` for repository → assets lookups.

    Example usage::

        asset = Asset(
            repository_name="maven-central",
            path="/org/example/lib/1.0/lib-1.0.jar",
            content_type="application/java-archive",
            size=1048576,
            blob_ref="default@maven-central/org/example/lib/1.0/lib-1.0.jar",
        )
        asset.update_checksum(
            sha1="a94a8fe5ccb19ba61c4c0873d391e987982fbbd3",
            sha256="9f86d081884c7d659a2feaa0c55ad015...",
            md5="098f6bcd4621d373cade4e832627b4f6",
        )
        asset.set_attribute("maven.groupId", "org.example")
        asset.save()
    """

    __tablename__: str = "assets"

    # -- Table Arguments (Indexes and Constraints) ---------------------------
    # Defined before columns as per SQLAlchemy convention for __table_args__.
    # Uses db.Index() as specified by the internal imports contract.
    __table_args__ = (
        db.Index(
            "ix_assets_repo_path",
            "repository_name",
            "path",
            unique=True,
        ),
        db.Index("ix_assets_last_downloaded", "last_downloaded"),
        db.Index("ix_assets_content_type", "content_type"),
        db.Index("ix_assets_component_id", "component_id"),
    )

    # -- Primary Key ---------------------------------------------------------

    id: int = Column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        doc=(
            "Unique auto-incrementing identifier for this asset.  Uses "
            "BigInteger to support large repositories with millions of assets "
            "without overflow (Integer max ~2.1B is insufficient)."
        ),
    )

    # -- Foreign Keys --------------------------------------------------------

    component_id: Optional[int] = Column(
        Integer,
        ForeignKey("components.id", ondelete="SET NULL"),
        nullable=True,
        doc=(
            "Foreign key to the parent Component.  Nullable because standalone "
            "metadata files (e.g., maven-metadata.xml, Packages.gz) have no "
            "parent component.  Indexed via ix_assets_component_id in "
            "__table_args__."
        ),
    )

    repository_name: str = Column(
        String(200),
        ForeignKey("repositories.name", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc=(
            "Foreign key to the owning Repository.  Every asset must belong "
            "to exactly one repository.  Cascades on delete so that removing "
            "a repository also removes all its assets."
        ),
    )

    # -- Content Fields ------------------------------------------------------

    path: str = Column(
        String(2048),
        nullable=False,
        doc=(
            "Storage path within the repository.  Uses String(2048) to "
            "accommodate deeply nested Maven coordinate paths.  Example: "
            "'/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar'"
        ),
    )

    content_type: Optional[str] = Column(
        String(255),
        nullable=True,
        doc=(
            "MIME type of the stored file.  Examples: "
            "'application/java-archive', 'application/json', "
            "'application/octet-stream'.  Nullable for assets whose type "
            "has not yet been determined."
        ),
    )

    # -- Checksum Fields -----------------------------------------------------
    # Fixed-length hex digest strings for each supported hash algorithm.
    # All nullable because checksums may be computed asynchronously after
    # initial upload.

    checksum_sha1: Optional[str] = Column(
        String(40),
        nullable=True,
        doc="SHA-1 hash of the asset content as a 40-character hex string.",
    )

    checksum_sha256: Optional[str] = Column(
        String(64),
        nullable=True,
        doc="SHA-256 hash of the asset content as a 64-character hex string.",
    )

    checksum_md5: Optional[str] = Column(
        String(32),
        nullable=True,
        doc="MD5 hash of the asset content as a 32-character hex string.",
    )

    # -- Storage Fields ------------------------------------------------------

    size: Optional[int] = Column(
        BigInteger,
        nullable=True,
        doc=(
            "File size in bytes.  Uses BigInteger to support very large "
            "artifacts such as multi-GB Docker image layers.  Nullable for "
            "assets whose size has not yet been computed."
        ),
    )

    last_downloaded: Optional[datetime] = Column(
        DateTime,
        nullable=True,
        index=False,  # Covered by ix_assets_last_downloaded in __table_args__
        doc=(
            "Timestamp of the most recent download of this asset (UTC).  "
            "Critical for cleanup policy (F-204) evaluation criteria such as "
            "'last_downloaded_before'.  Null if the asset has never been "
            "downloaded since upload."
        ),
    )

    blob_ref: Optional[str] = Column(
        String(500),
        nullable=True,
        doc=(
            "Reference to the binary blob in the configured BlobStore.  "
            "For File BlobStore (F-201): a relative filesystem path.  "
            "For S3 BlobStore (F-202): an S3 object key.  "
            "Nullable for assets that have been created but not yet stored."
        ),
    )

    # -- Relationships -------------------------------------------------------
    # Bidirectional many-to-one relationships using SQLAlchemy relationship().
    # back_populates ensures consistency between parent and child sides.

    component = relationship(
        "Component",
        back_populates="assets",
        doc=(
            "Many-to-one relationship to the parent Component.  "
            "An asset with component_id=None is an orphaned/standalone asset."
        ),
    )

    repository = relationship(
        "Repository",
        back_populates="assets",
        doc=(
            "Many-to-one relationship to the owning Repository.  "
            "Every asset belongs to exactly one repository."
        ),
    )

    # -----------------------------------------------------------------------
    # Properties
    # -----------------------------------------------------------------------

    @property
    def is_orphan(self) -> bool:
        """Check if this asset has no associated component.

        Orphaned assets are files that exist in a repository without being
        grouped under a component.  Common examples include:

        - Repository-level metadata files (``maven-metadata.xml``)
        - Package index files (``Packages.gz`` for APT)
        - Docker manifest lists
        - Raw format files with no component grouping

        Returns:
            ``True`` if ``component_id`` is ``None``, ``False`` otherwise.
        """
        return self.component_id is None

    # -----------------------------------------------------------------------
    # Instance Methods
    # -----------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this asset to a dictionary for JSON serialization.

        Extends the ``BaseModel.to_dict()`` method by adding the computed
        ``is_orphan`` property, which is not a database column but is useful
        for API consumers.

        Returns:
            A dictionary containing all column values (with datetimes
            serialized to ISO-8601 strings) plus the ``is_orphan`` flag.
        """
        result: dict[str, Any] = super().to_dict()
        result["is_orphan"] = self.is_orphan
        return result

    def update_checksum(
        self,
        sha1: Optional[str] = None,
        sha256: Optional[str] = None,
        md5: Optional[str] = None,
    ) -> None:
        """Update one or more checksum fields on this asset.

        Only the provided (non-``None``) checksum values are updated; fields
        not specified are left unchanged.  The session is committed after the
        update.

        This method is typically called after an asynchronous hash computation
        completes, or during an integrity verification pass.

        Args:
            sha1: SHA-1 hash as a 40-character hex string, or ``None`` to
                leave unchanged.
            sha256: SHA-256 hash as a 64-character hex string, or ``None``
                to leave unchanged.
            md5: MD5 hash as a 32-character hex string, or ``None`` to
                leave unchanged.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On any database-level error
                during commit.

        Example::

            asset.update_checksum(
                sha1="da39a3ee5e6b4b0d3255bfef95601890afd80709",
                sha256="e3b0c44298fc1c149afbf4c8996fb924"
                       "27ae41e4649b934ca495991b7852b855",
                md5="d41d8cd98f00b204e9800998ecf8427e",
            )
        """
        if sha1 is not None:
            self.checksum_sha1 = sha1
        if sha256 is not None:
            self.checksum_sha256 = sha256
        if md5 is not None:
            self.checksum_md5 = md5
        db.session.add(self)
        db.session.commit()
        logger.debug(
            "Updated checksums for asset %s (sha1=%s, sha256=%s, md5=%s).",
            self.path,
            "set" if sha1 is not None else "unchanged",
            "set" if sha256 is not None else "unchanged",
            "set" if md5 is not None else "unchanged",
        )

    def record_download(self) -> None:
        """Record a download event by updating the ``last_downloaded`` timestamp.

        Sets ``last_downloaded`` to the current UTC time and commits the
        session.  This timestamp is critical for cleanup policy (F-204)
        evaluation — assets with a recent ``last_downloaded`` are retained
        while stale assets may be removed.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On any database-level error
                during commit.

        Example::

            asset = Asset.query.get(42)
            asset.record_download()
            # asset.last_downloaded is now set to current UTC time
        """
        self.last_downloaded = datetime.now(timezone.utc)
        db.session.add(self)
        db.session.commit()
        logger.debug(
            "Recorded download for asset %s at %s.",
            self.path,
            self.last_downloaded.isoformat() if self.last_downloaded else "N/A",
        )

    # -----------------------------------------------------------------------
    # String Representation
    # -----------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Format: ``<Asset repository_name:path>``

        Examples::

            >>> asset = Asset(repository_name="maven-central", path="/com/example/1.0/example-1.0.jar")
            >>> repr(asset)
            '<Asset maven-central:/com/example/1.0/example-1.0.jar>'
        """
        return f"<Asset {self.repository_name}:{self.path}>"


logger.debug("Asset model module loaded — exports: %s", ", ".join(__all__))
