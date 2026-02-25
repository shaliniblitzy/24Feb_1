"""
Hosted Repository Type Implementation (Feature F-102).

This module implements the **Hosted** repository type — one of the three
repository types in the Nexus Repository system (alongside Proxy and Group).
Hosted repositories serve artifacts exclusively from the local BlobStore and
handle direct artifact uploads with configurable write-policy enforcement.

**Architecture Context:**

Replaces the Java Hosted repository facets from Section 5.2.4 of the Technical
Specification.  In the Java source system, hosted repository behaviour was
implemented as an OSGi-managed facet bean using Guice dependency injection; in
this Python/Flask implementation, it is a plain Python class composed with
:class:`StorageFacet` (for BlobStore delegation) and
:class:`RepositoryLifecycle` (for state machine enforcement).

**Feature Coverage:**

+---------+-------------------------------------------+----------------------------+
| Feature | Name                                      | Role of HostedRepository   |
+=========+===========================================+============================+
| F-102   | Repository Types — Hosted                 | Core implementation        |
+---------+-------------------------------------------+----------------------------+
| F-104   | Browse Tree Navigation                    | Virtual directory tree     |
+---------+-------------------------------------------+----------------------------+
| F-201   | File BlobStore                            | Content persistence        |
+---------+-------------------------------------------+----------------------------+
| F-202   | S3 BlobStore                              | Content persistence (alt)  |
+---------+-------------------------------------------+----------------------------+
| F-303   | Audit Logging                             | Event emission             |
+---------+-------------------------------------------+----------------------------+
| F-503   | Webhook Integration                       | Event emission             |
+---------+-------------------------------------------+----------------------------+

**Write Policies (AAP Section 0.7.1):**

- ``ALLOW``       — Allow all writes (default)
- ``ALLOW_ONCE``  — Allow only the first write per path (immutable artifacts)
- ``DENY``        — Read-only repository; no uploads accepted

**Performance Targets (AAP Section 0.7.3):**

- Cached artifact resolution: < 200 ms

**Exports:**

- :class:`HostedRepository`           — Main handler class
- :class:`HostedRepositoryError`      — Base exception
- :class:`WritePolicyViolationError`  — Write-policy violation
- :class:`ContentTypeValidationError` — Content-type mismatch
- :data:`WRITE_POLICY_ALLOW`          — ``'ALLOW'`` constant
- :data:`WRITE_POLICY_ALLOW_ONCE`     — ``'ALLOW_ONCE'`` constant
- :data:`WRITE_POLICY_DENY`           — ``'DENY'`` constant
- :data:`VALID_WRITE_POLICIES`        — Set of all valid policies
- :data:`DEFAULT_HOSTED_ATTRIBUTES`   — Default hosted config dict
"""

from __future__ import annotations

import copy
import hashlib
import io
import logging
import mimetypes
from datetime import datetime, timezone
from typing import Any, BinaryIO

from flask import current_app

from src.app.extensions import db
from src.app.models.repository import Repository
from src.app.models.component import Component
from src.app.models.asset import Asset
from src.app.repositories.facets import RepositoryFacet, StorageFacet
from src.app.repositories.lifecycle import RepositoryLifecycle, RepositoryState
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for hosted repository operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — Write Policies
# ---------------------------------------------------------------------------
# Per AAP Section 0.7.1, hosted repositories support three write policies
# that govern whether artifact uploads are accepted.
# ---------------------------------------------------------------------------

WRITE_POLICY_ALLOW: str = "ALLOW"
"""Allow all writes — any upload replaces existing content.  Default policy."""

WRITE_POLICY_ALLOW_ONCE: str = "ALLOW_ONCE"
"""Allow only the first write per path — artifacts become immutable after
initial upload.  Re-uploading to an existing path is rejected."""

WRITE_POLICY_DENY: str = "DENY"
"""Deny all writes — the repository is read-only.  No uploads are accepted."""

VALID_WRITE_POLICIES: set[str] = {
    WRITE_POLICY_ALLOW,
    WRITE_POLICY_ALLOW_ONCE,
    WRITE_POLICY_DENY,
}
"""Complete set of recognised write-policy values for validation."""

# ---------------------------------------------------------------------------
# Constants — Default Hosted Repository Attributes
# ---------------------------------------------------------------------------

DEFAULT_HOSTED_ATTRIBUTES: dict[str, Any] = {
    "storage": {
        "writePolicy": WRITE_POLICY_ALLOW,
        "strictContentTypeValidation": True,
    },
}
"""Default configuration attributes applied to new hosted repositories.

Structure mirrors the ``attributes`` JSON column layout defined in
:class:`Repository` (from ``JSONAttributesMixin``).
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "HostedRepository",
    "HostedRepositoryError",
    "WritePolicyViolationError",
    "ContentTypeValidationError",
    "WRITE_POLICY_ALLOW",
    "WRITE_POLICY_ALLOW_ONCE",
    "WRITE_POLICY_DENY",
    "VALID_WRITE_POLICIES",
    "DEFAULT_HOSTED_ATTRIBUTES",
]


# ═══════════════════════════════════════════════════════════════════════════
# Custom Exception Hierarchy
# ═══════════════════════════════════════════════════════════════════════════


class HostedRepositoryError(Exception):
    """Base exception for all hosted repository operations.

    Attributes:
        message: Human-readable description of the error.
        repository_name: Name of the repository that triggered the error,
            or ``None`` if unknown.
    """

    def __init__(
        self,
        message: str = "Hosted repository error",
        repository_name: str | None = None,
    ) -> None:
        self.message: str = message
        self.repository_name: str | None = repository_name
        super().__init__(self.message)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"repository_name={self.repository_name!r})"
        )


class WritePolicyViolationError(HostedRepositoryError):
    """Raised when a write operation violates the repository's write policy.

    Examples of violations:
        - Uploading to a repository with ``DENY`` policy
        - Re-uploading to an existing path under ``ALLOW_ONCE`` policy
    """

    def __init__(
        self,
        message: str = "Write policy violation",
        repository_name: str | None = None,
    ) -> None:
        super().__init__(message=message, repository_name=repository_name)


class ContentTypeValidationError(HostedRepositoryError):
    """Raised when strict content-type validation rejects an upload.

    Occurs when the declared content type does not match the expected MIME
    type inferred from the file extension, and the repository has
    ``strictContentTypeValidation`` enabled.
    """

    def __init__(
        self,
        message: str = "Content type validation failed",
        repository_name: str | None = None,
    ) -> None:
        super().__init__(message=message, repository_name=repository_name)


# ═══════════════════════════════════════════════════════════════════════════
# HostedRepository — Core Handler Class
# ═══════════════════════════════════════════════════════════════════════════


class HostedRepository:
    """Hosted repository type handler.

    Serves artifacts from the local BlobStore only.  Supports direct uploads
    with write-policy enforcement (ALLOW, ALLOW_ONCE, DENY) and optional
    strict content-type validation.

    This class composes two cross-cutting facets:

    - :class:`StorageFacet` for BlobStore content operations
    - :class:`RepositoryLifecycle` for state machine enforcement

    **Usage:**

    .. code-block:: python

        repo = Repository.query.get("maven-hosted")
        hosted = HostedRepository(repo)
        asset = hosted.store_artifact(
            path="/org/example/lib/1.0/lib-1.0.jar",
            content=jar_bytes,
            content_type="application/java-archive",
            component_coordinates={
                "namespace": "org.example",
                "name": "lib",
                "version": "1.0",
            },
        )
    """

    # -----------------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------------

    def __init__(self, repository: Repository) -> None:
        """Initialise a hosted repository handler bound to *repository*.

        Args:
            repository: The :class:`Repository` model instance.  Must have
                ``type == 'hosted'``.

        Raises:
            TypeError: If *repository* is not a hosted-type repository.
        """
        if not repository.is_hosted:
            raise TypeError(
                f"HostedRepository requires a repository with type='hosted', "
                f"but got type='{repository.type}' for '{repository.name}'"
            )

        self.repository: Repository = repository
        self.name: str = repository.name
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.{repository.name}"
        )

        # Compose cross-cutting facets
        self._lifecycle: RepositoryLifecycle = RepositoryLifecycle(repository)
        self._storage_facet: StorageFacet = StorageFacet(repository)

        # Attach storage facet so it can service requests
        self._storage_facet.attach()

        self.logger.debug(
            "HostedRepository initialised for '%s' (format=%s, write_policy=%s)",
            self.name,
            repository.format,
            self._get_write_policy(),
        )

    # -----------------------------------------------------------------------
    # Content Retrieval Operations
    # -----------------------------------------------------------------------

    def get_artifact(
        self, path: str
    ) -> tuple[BinaryIO | None, str | None, int | None]:
        """Retrieve an artifact by path from this hosted repository.

        Checks that the repository is online and in the ``STARTED`` state,
        then queries the Asset table for a matching record.  If found, the
        binary content is retrieved from the BlobStore via the storage facet
        and ``last_downloaded`` is updated.

        Performance target: < 200 ms for cached artifact resolution
        (AAP Section 0.7.3).

        Args:
            path: The logical artifact path within the repository
                (e.g. ``'/org/example/lib/1.0/lib-1.0.jar'``).

        Returns:
            A 3-tuple of ``(content_stream, content_type, size)`` on success,
            or ``(None, None, None)`` if the artifact is not found.

        Raises:
            InvalidStateError: If the repository is not in the ``STARTED``
                state (propagated from :meth:`RepositoryLifecycle.ensure_started`).
            HostedRepositoryError: If the repository is offline.
        """
        self._ensure_online_and_started()

        # Normalise path
        normalised_path = self._normalise_path(path)

        # Query asset by repository and path
        asset: Asset | None = Asset.query.filter_by(
            repository_name=self.name,
            path=normalised_path,
        ).first()

        if asset is None:
            self.logger.debug(
                "Artifact not found at path '%s' in repository '%s'",
                normalised_path,
                self.name,
            )
            return None, None, None

        # Retrieve content from BlobStore
        if not asset.blob_ref:
            self.logger.warning(
                "Asset '%s' in repository '%s' has no blob_ref — "
                "content unavailable",
                normalised_path,
                self.name,
            )
            return None, None, None

        content_stream, _metadata = self._storage_facet.get_content(
            asset.blob_ref
        )
        if content_stream is None:
            self.logger.warning(
                "Blob '%s' not found in BlobStore for asset '%s'",
                asset.blob_ref,
                normalised_path,
            )
            return None, None, None

        # Record the download (updates last_downloaded timestamp)
        try:
            asset.record_download()
        except Exception as exc:
            # Non-fatal — content is still served even if download
            # recording fails
            self.logger.warning(
                "Failed to record download for asset '%s': %s",
                normalised_path,
                exc,
            )

        # Emit ASSET_DOWNLOADED event for audit logging (F-303) and
        # webhook dispatch (F-503)
        emit_event(
            EventType.ASSET_DOWNLOADED,
            {
                "repository_name": self.name,
                "asset_path": normalised_path,
                "content_type": asset.content_type,
                "size": asset.size,
            },
        )

        self.logger.debug(
            "Artifact served: '%s' (%s, %d bytes) from repository '%s'",
            normalised_path,
            asset.content_type or "unknown",
            asset.size or 0,
            self.name,
        )

        return content_stream, asset.content_type, asset.size

    def get_component(
        self,
        namespace: str | None,
        name: str,
        version: str | None,
    ) -> Component | None:
        """Retrieve a component by coordinates from this hosted repository.

        Args:
            namespace: Component namespace (e.g. Maven groupId, npm scope).
                ``None`` for formats that do not use namespaces.
            name: Component name (e.g. artifactId, package name).
            version: Version string, or ``None`` for unversioned components.

        Returns:
            The :class:`Component` instance if found, otherwise ``None``.
        """
        self._ensure_online_and_started()

        query = Component.query.filter_by(
            repository_name=self.name,
            name=name,
        )
        if namespace is not None:
            query = query.filter_by(namespace=namespace)
        else:
            query = query.filter(Component.namespace.is_(None))

        if version is not None:
            query = query.filter_by(version=version)
        else:
            query = query.filter(Component.version.is_(None))

        component: Component | None = query.first()
        return component

    def list_components(
        self,
        namespace: str | None = None,
        name: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """List components in this hosted repository with optional filters.

        Args:
            namespace: Optional filter by namespace.
            name: Optional filter by component name.
            page: Page number (1-based).
            page_size: Number of items per page.

        Returns:
            A paginated result dictionary::

                {
                    "items": [<Component.to_dict()>, ...],
                    "total_count": 42,
                    "page": 1,
                    "page_size": 50,
                }
        """
        self._ensure_online_and_started()

        query = Component.query.filter_by(repository_name=self.name)

        if namespace is not None:
            query = query.filter_by(namespace=namespace)
        if name is not None:
            query = query.filter_by(name=name)

        # Order by name for deterministic pagination
        query = query.order_by(Component.name, Component.version)

        total_count: int = query.count()
        offset = (max(page, 1) - 1) * page_size
        items = query.offset(offset).limit(page_size).all()

        return {
            "items": [self._component_to_dict(c) for c in items],
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
        }

    def list_assets(
        self,
        component_id: int | None = None,
        path_prefix: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """List assets in this hosted repository with optional filters.

        Args:
            component_id: Optional filter to list assets belonging to a
                specific component.
            path_prefix: Optional filter by path prefix (e.g. ``'/org/apache'``).
            page: Page number (1-based).
            page_size: Number of items per page.

        Returns:
            A paginated result dictionary::

                {
                    "items": [<Asset.to_dict()>, ...],
                    "total_count": 100,
                    "page": 1,
                    "page_size": 50,
                }
        """
        self._ensure_online_and_started()

        query = Asset.query.filter_by(repository_name=self.name)

        if component_id is not None:
            query = query.filter_by(component_id=component_id)
        if path_prefix is not None:
            normalised_prefix = path_prefix if path_prefix.startswith("/") else f"/{path_prefix}"
            query = query.filter(Asset.path.startswith(normalised_prefix))

        query = query.order_by(Asset.path)

        total_count: int = query.count()
        offset = (max(page, 1) - 1) * page_size
        items = query.offset(offset).limit(page_size).all()

        return {
            "items": [a.to_dict() for a in items],
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
        }

    # -----------------------------------------------------------------------
    # Write Operations — Upload Handling
    # -----------------------------------------------------------------------

    def store_artifact(
        self,
        path: str,
        content: bytes | BinaryIO,
        content_type: str | None = None,
        component_coordinates: dict[str, Any] | None = None,
    ) -> Asset:
        """Store (upload) an artifact to this hosted repository.

        This is the **main upload entry point** for hosted repositories.
        The method enforces the repository's write policy, optionally
        validates the content type, computes checksums, persists the blob
        to the BlobStore, creates or updates the Asset and Component
        database records, and emits lifecycle events.

        Args:
            path: Logical path for the artifact (e.g.
                ``'/org/example/lib/1.0/lib-1.0.jar'``).
            content: The binary content to store — either raw ``bytes`` or
                a readable ``BinaryIO`` stream.
            content_type: Optional MIME type (e.g. ``'application/java-archive'``).
                If ``None``, it will be inferred from the file extension.
            component_coordinates: Optional dict with keys ``'namespace'``,
                ``'name'``, ``'version'`` to associate the asset with a
                component.

        Returns:
            The created or updated :class:`Asset` instance.

        Raises:
            WritePolicyViolationError: If the upload violates the write policy.
            ContentTypeValidationError: If strict content-type validation
                rejects the upload.
            HostedRepositoryError: For general errors during upload.
            InvalidStateError: If the repository is not in STARTED state.
        """
        self._ensure_online_and_started()

        normalised_path = self._normalise_path(path)

        # Step 1 — Enforce write policy
        self._check_write_policy(normalised_path)

        # Step 2 — Materialise content to bytes if BinaryIO
        if isinstance(content, (bytes, bytearray)):
            content_bytes: bytes = bytes(content)
        else:
            content_bytes = content.read()

        # Step 3 — Infer content type if not provided
        if content_type is None:
            content_type = self._detect_content_type(normalised_path)

        # Step 4 — Validate content type (if strict validation enabled)
        self._validate_content_type(normalised_path, content_type, content_bytes)

        # Step 5 — Compute checksums (SHA-1, SHA-256, MD5)
        checksums = self._compute_checksums(content_bytes)

        # Step 6 — Store blob in BlobStore via storage facet
        content_stream = io.BytesIO(content_bytes)
        content_size = len(content_bytes)

        try:
            blob_ref = self._storage_facet.store_content(
                path=normalised_path,
                content=content_stream,
                content_type=content_type,
                size=content_size,
                checksums=checksums,
            )
        except Exception as exc:
            self.logger.error(
                "Failed to store blob for '%s' in repository '%s': %s",
                normalised_path,
                self.name,
                exc,
            )
            raise HostedRepositoryError(
                message=f"Failed to store artifact at '{normalised_path}': {exc}",
                repository_name=self.name,
            ) from exc

        # Step 7 — Create or update Asset record
        try:
            asset: Asset | None = Asset.query.filter_by(
                repository_name=self.name,
                path=normalised_path,
            ).first()

            component_created = False
            component: Component | None = None

            if asset is not None:
                # Update existing asset
                asset.blob_ref = blob_ref
                asset.content_type = content_type
                asset.size = content_size
                asset.checksum_sha1 = checksums.get("sha1")
                asset.checksum_sha256 = checksums.get("sha256")
                asset.checksum_md5 = checksums.get("md5")
                asset.last_downloaded = datetime.now(timezone.utc)
                db.session.add(asset)
            else:
                # Create new asset
                asset = Asset(
                    repository_name=self.name,
                    path=normalised_path,
                    content_type=content_type,
                    size=content_size,
                    blob_ref=blob_ref,
                    checksum_sha1=checksums.get("sha1"),
                    checksum_sha256=checksums.get("sha256"),
                    checksum_md5=checksums.get("md5"),
                    last_downloaded=datetime.now(timezone.utc),
                )
                db.session.add(asset)

            # Step 8 — Link to component if coordinates provided
            if component_coordinates is not None:
                component, component_created = self._find_or_create_component(
                    component_coordinates
                )
                asset.component_id = component.id

            # Step 9 — Commit transaction
            db.session.commit()

            self.logger.info(
                "Artifact stored: '%s' (%s, %d bytes, sha1=%s) "
                "in repository '%s'",
                normalised_path,
                content_type or "unknown",
                content_size,
                checksums.get("sha1", "N/A")[:12],
                self.name,
            )

            # Step 10 — Emit COMPONENT_UPLOADED event if applicable
            if component is not None and component_created:
                emit_event(
                    EventType.COMPONENT_UPLOADED,
                    {
                        "repository_name": self.name,
                        "component_name": component.name,
                        "component_version": component.version,
                        "namespace": component.namespace,
                        "format": self.repository.format,
                    },
                )
            elif component is not None:
                # Component already existed — still emit event for the upload
                emit_event(
                    EventType.COMPONENT_UPLOADED,
                    {
                        "repository_name": self.name,
                        "component_name": component.name,
                        "component_version": component.version,
                        "namespace": component.namespace,
                        "format": self.repository.format,
                    },
                )

            return asset

        except (WritePolicyViolationError, ContentTypeValidationError):
            raise
        except Exception as exc:
            db.session.rollback()
            self.logger.error(
                "Failed to persist asset record for '%s' in repository '%s': %s",
                normalised_path,
                self.name,
                exc,
            )
            raise HostedRepositoryError(
                message=f"Failed to store artifact at '{normalised_path}': {exc}",
                repository_name=self.name,
            ) from exc

    # -----------------------------------------------------------------------
    # Delete Operations
    # -----------------------------------------------------------------------

    def delete_artifact(self, path: str) -> bool:
        """Delete an asset by path from this hosted repository.

        Removes the blob from the BlobStore (soft-delete) and deletes the
        Asset record from the database.  If the parent Component has no
        remaining assets after deletion, the orphaned Component is also
        cleaned up.

        Args:
            path: The artifact path to delete.

        Returns:
            ``True`` if the asset was found and deleted, ``False`` if the
            path does not exist in this repository.
        """
        self._ensure_online_and_started()

        normalised_path = self._normalise_path(path)

        asset: Asset | None = Asset.query.filter_by(
            repository_name=self.name,
            path=normalised_path,
        ).first()

        if asset is None:
            self.logger.debug(
                "Delete requested for non-existent path '%s' in repository '%s'",
                normalised_path,
                self.name,
            )
            return False

        # Remove blob from BlobStore (soft-delete)
        if asset.blob_ref:
            self._storage_facet.delete_content(asset.blob_ref)

        # Track parent component for orphan cleanup
        parent_component_id: int | None = asset.component_id

        # Delete asset record
        db.session.delete(asset)
        db.session.commit()

        self.logger.info(
            "Asset deleted: '%s' from repository '%s'",
            normalised_path,
            self.name,
        )

        # Clean up orphaned Component if no assets remain
        if parent_component_id is not None:
            remaining_count = Asset.query.filter_by(
                component_id=parent_component_id,
            ).count()
            if remaining_count == 0:
                orphan_component = db.session.get(Component, parent_component_id)
                if orphan_component is not None:
                    component_name = orphan_component.name
                    component_version = orphan_component.version
                    component_namespace = orphan_component.namespace
                    db.session.delete(orphan_component)
                    db.session.commit()
                    self.logger.info(
                        "Orphaned component cleaned up: '%s:%s' "
                        "from repository '%s'",
                        component_namespace or "",
                        component_name,
                        self.name,
                    )
                    emit_event(
                        EventType.COMPONENT_DELETED,
                        {
                            "repository_name": self.name,
                            "component_name": component_name,
                            "component_version": component_version,
                            "namespace": component_namespace,
                            "format": self.repository.format,
                        },
                    )

        return True

    def delete_component(self, component_id: int) -> bool:
        """Delete a component and all its assets from this hosted repository.

        All Asset records and their corresponding BlobStore blobs are deleted
        before the Component record is removed.  A ``COMPONENT_DELETED``
        event is emitted on success.

        Args:
            component_id: The integer ID of the component to delete.

        Returns:
            ``True`` if the component was found and deleted, ``False`` if
            the component does not exist in this repository.
        """
        self._ensure_online_and_started()

        component: Component | None = Component.query.filter_by(
            id=component_id,
            repository_name=self.name,
        ).first()

        if component is None:
            self.logger.debug(
                "Delete requested for non-existent component %d "
                "in repository '%s'",
                component_id,
                self.name,
            )
            return False

        # Capture component info before deletion for event emission
        component_name: str = component.name
        component_version: str | None = component.version
        component_namespace: str | None = component.namespace

        # Delete all child assets and their blobs
        assets = Asset.query.filter_by(
            component_id=component.id,
            repository_name=self.name,
        ).all()

        for asset in assets:
            if asset.blob_ref:
                self._storage_facet.delete_content(asset.blob_ref)
            db.session.delete(asset)

        # Delete the component record
        db.session.delete(component)
        db.session.commit()

        self.logger.info(
            "Component deleted: '%s:%s:%s' (%d assets) from repository '%s'",
            component_namespace or "",
            component_name,
            component_version or "",
            len(assets),
            self.name,
        )

        # Emit COMPONENT_DELETED event
        emit_event(
            EventType.COMPONENT_DELETED,
            {
                "repository_name": self.name,
                "component_name": component_name,
                "component_version": component_version,
                "namespace": component_namespace,
                "format": self.repository.format,
            },
        )

        return True

    # -----------------------------------------------------------------------
    # Browse Operations (Feature F-104)
    # -----------------------------------------------------------------------

    def browse(self, path: str = "/") -> dict[str, Any]:
        """Build a virtual directory tree at the requested path level.

        Parses asset paths in this repository to construct a folder/file
        hierarchy, analogous to browsing a filesystem.  Implements Feature
        F-104 (Browse Tree Navigation).

        Args:
            path: The directory path to browse (default ``'/'`` for root).

        Returns:
            A dictionary describing the children at this path level::

                {
                    "path": "/org",
                    "children": [
                        {"name": "apache", "type": "folder",
                         "path": "/org/apache"},
                        {"name": "example", "type": "folder",
                         "path": "/org/example"},
                    ],
                }
        """
        self._ensure_online_and_started()

        # Normalise browse path
        normalised_path = path.rstrip("/") if path != "/" else ""

        # Query all asset paths in this repository
        assets = (
            Asset.query
            .filter_by(repository_name=self.name)
            .with_entities(Asset.path, Asset.size)
            .all()
        )

        # Build children set at the requested path level
        children_map: dict[str, dict[str, Any]] = {}

        for asset_path, asset_size in assets:
            if not asset_path:
                continue

            # Ensure the asset path starts with /
            if not asset_path.startswith("/"):
                asset_path = f"/{asset_path}"

            # Check if the asset is under the browse path
            if normalised_path and not asset_path.startswith(f"{normalised_path}/"):
                continue
            if not normalised_path and not asset_path.startswith("/"):
                continue

            # Extract the relative path below the browse path
            if normalised_path:
                relative = asset_path[len(normalised_path) + 1:]
            else:
                relative = asset_path.lstrip("/")

            if not relative:
                continue

            # Split into segments
            segments = relative.split("/")
            child_name = segments[0]

            if not child_name:
                continue

            if len(segments) == 1:
                # This is a file at the current level
                child_path = f"{normalised_path}/{child_name}" if normalised_path else f"/{child_name}"
                children_map[child_name] = {
                    "name": child_name,
                    "type": "file",
                    "path": child_path,
                    "size": asset_size or 0,
                }
            else:
                # This is a folder at the current level (only add once)
                if child_name not in children_map:
                    child_path = f"{normalised_path}/{child_name}" if normalised_path else f"/{child_name}"
                    children_map[child_name] = {
                        "name": child_name,
                        "type": "folder",
                        "path": child_path,
                    }

        # Sort children: folders first, then files, alphabetically within
        children = sorted(
            children_map.values(),
            key=lambda c: (0 if c["type"] == "folder" else 1, c["name"]),
        )

        display_path = normalised_path if normalised_path else "/"

        return {
            "path": display_path,
            "children": children,
        }

    # -----------------------------------------------------------------------
    # Repository Status and Configuration
    # -----------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        """Return a comprehensive status snapshot for this hosted repository.

        Returns:
            A dictionary with repository status information including
            state, write policy, and content counts.
        """
        return {
            "name": self.name,
            "type": "hosted",
            "format": self.repository.format,
            "online": self.repository.online,
            "state": self._lifecycle.current_state.value,
            "write_policy": self._get_write_policy(),
            "component_count": Component.query.filter_by(
                repository_name=self.name
            ).count(),
            "asset_count": Asset.query.filter_by(
                repository_name=self.name
            ).count(),
        }

    def update_configuration(self, attributes: dict[str, Any]) -> None:
        """Update hosted-specific configuration attributes.

        Validates the ``writePolicy`` value if provided, then performs a
        deep merge with existing attributes.  The change is committed to
        the database and a ``REPOSITORY_UPDATED`` event is emitted.

        Args:
            attributes: A dictionary of configuration values to merge.
                Only recognised keys under ``'storage'`` are applied.

        Raises:
            HostedRepositoryError: If the provided ``writePolicy`` is invalid.
        """
        # Validate write policy if provided
        storage_attrs = attributes.get("storage", {})
        if "writePolicy" in storage_attrs:
            new_policy = storage_attrs["writePolicy"]
            if new_policy not in VALID_WRITE_POLICIES:
                raise HostedRepositoryError(
                    message=(
                        f"Invalid write policy '{new_policy}'. "
                        f"Valid values: {', '.join(sorted(VALID_WRITE_POLICIES))}"
                    ),
                    repository_name=self.name,
                )

        # Deep merge with existing attributes — use deepcopy to ensure
        # SQLAlchemy detects the change when we reassign the JSON column.
        current_attrs: dict[str, Any] = copy.deepcopy(
            self.repository.attributes or {}
        )
        self._deep_merge(current_attrs, attributes)

        # SQLAlchemy JSON mutation detection requires top-level reassignment
        self.repository.attributes = current_attrs
        db.session.add(self.repository)
        db.session.commit()

        self.logger.info(
            "Configuration updated for hosted repository '%s': %s",
            self.name,
            attributes,
        )

        # Emit configuration change event
        emit_event(
            EventType.REPOSITORY_UPDATED,
            {
                "repository_name": self.name,
                "event_subtype": "configuration_updated",
                "attributes": attributes,
            },
        )

    # -----------------------------------------------------------------------
    # Private Helpers — Write Policy Enforcement
    # -----------------------------------------------------------------------

    def _check_write_policy(self, path: str) -> None:
        """Enforce the repository's write policy for the given path.

        Args:
            path: The target artifact path.

        Raises:
            WritePolicyViolationError: If the write is not permitted.
        """
        policy = self._get_write_policy()

        if policy == WRITE_POLICY_DENY:
            raise WritePolicyViolationError(
                message=(
                    f"Repository '{self.name}' does not accept writes "
                    f"(write policy: DENY)"
                ),
                repository_name=self.name,
            )

        if policy == WRITE_POLICY_ALLOW_ONCE:
            existing = Asset.query.filter_by(
                repository_name=self.name,
                path=path,
            ).first()
            if existing is not None:
                raise WritePolicyViolationError(
                    message=(
                        f"Artifact already exists at '{path}' in repository "
                        f"'{self.name}' (write policy: ALLOW_ONCE)"
                    ),
                    repository_name=self.name,
                )

        # WRITE_POLICY_ALLOW — no restriction

    def _get_write_policy(self) -> str:
        """Extract the write policy from repository attributes.

        Returns:
            The write-policy string.  Defaults to ``WRITE_POLICY_ALLOW``
            if not explicitly set.
        """
        attrs: dict[str, Any] = self.repository.attributes or {}
        storage_config: dict[str, Any] = attrs.get("storage", {})
        return storage_config.get("writePolicy", WRITE_POLICY_ALLOW)

    # -----------------------------------------------------------------------
    # Private Helpers — Content Type Validation
    # -----------------------------------------------------------------------

    def _validate_content_type(
        self,
        path: str,
        content_type: str | None,
        content: bytes,
    ) -> None:
        """Validate the content type if strict validation is enabled.

        Args:
            path: The artifact path (used for MIME type inference).
            content_type: The declared content type.
            content: The binary content (unused currently; reserved for
                magic-byte detection in the future).

        Raises:
            ContentTypeValidationError: If the declared content type does
                not match the expected MIME type.
        """
        attrs: dict[str, Any] = self.repository.attributes or {}
        storage_config: dict[str, Any] = attrs.get("storage", {})
        strict_validation: bool = storage_config.get(
            "strictContentTypeValidation", True
        )

        if not strict_validation:
            return

        if content_type is None:
            # No content type declared — skip validation (will be inferred)
            return

        expected_type = self._detect_content_type(path)
        if expected_type is None:
            # Cannot determine expected type from extension — skip
            return

        # Compare only the primary type (ignore parameters like charset)
        declared_primary = content_type.split(";")[0].strip().lower()
        expected_primary = expected_type.split(";")[0].strip().lower()

        # application/octet-stream is a wildcard — always accepted
        if declared_primary == "application/octet-stream":
            return
        if expected_primary == "application/octet-stream":
            return

        if declared_primary != expected_primary:
            raise ContentTypeValidationError(
                message=(
                    f"Content type mismatch for '{path}': "
                    f"declared '{declared_primary}', "
                    f"expected '{expected_primary}'"
                ),
                repository_name=self.name,
            )

    # -----------------------------------------------------------------------
    # Private Helpers — Checksum Computation
    # -----------------------------------------------------------------------

    @staticmethod
    def _compute_checksums(content: bytes) -> dict[str, str]:
        """Compute SHA-1, SHA-256, and MD5 checksums for binary content.

        Args:
            content: The binary content to hash.

        Returns:
            A dict with keys ``'sha1'``, ``'sha256'``, ``'md5'`` mapping
            to hex digest strings.
        """
        return {
            "sha1": hashlib.sha1(content).hexdigest(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "md5": hashlib.md5(content).hexdigest(),
        }

    # -----------------------------------------------------------------------
    # Private Helpers — Component Management
    # -----------------------------------------------------------------------

    def _find_or_create_component(
        self,
        coordinates: dict[str, Any],
    ) -> tuple[Component, bool]:
        """Find an existing component or create a new one.

        Args:
            coordinates: A dict with keys ``'namespace'``, ``'name'``,
                and ``'version'``.

        Returns:
            A 2-tuple of ``(component, was_created)`` where *was_created*
            is ``True`` if a new Component was inserted.
        """
        comp_namespace: str | None = coordinates.get("namespace")
        comp_name: str = coordinates.get("name", "unknown")
        comp_version: str | None = coordinates.get("version")

        # Attempt to find existing component
        query = Component.query.filter_by(
            repository_name=self.name,
            name=comp_name,
        )
        if comp_namespace is not None:
            query = query.filter_by(namespace=comp_namespace)
        else:
            query = query.filter(Component.namespace.is_(None))

        if comp_version is not None:
            query = query.filter_by(version=comp_version)
        else:
            query = query.filter(Component.version.is_(None))

        existing: Component | None = query.first()
        if existing is not None:
            return existing, False

        # Create new component
        new_component = Component(
            repository_name=self.name,
            namespace=comp_namespace,
            name=comp_name,
            version=comp_version,
        )
        db.session.add(new_component)
        db.session.flush()  # Assign PK so we can reference component.id

        self.logger.info(
            "Component created: '%s:%s:%s' in repository '%s'",
            comp_namespace or "",
            comp_name,
            comp_version or "",
            self.name,
        )

        return new_component, True

    # -----------------------------------------------------------------------
    # Private Helpers — General Utilities
    # -----------------------------------------------------------------------

    def _ensure_online_and_started(self) -> None:
        """Guard method — raise if the repository is offline or not STARTED.

        Raises:
            HostedRepositoryError: If the repository is offline.
            InvalidStateError: If the repository is not in STARTED state.
        """
        if not self.repository.online:
            raise HostedRepositoryError(
                message=f"Repository '{self.name}' is offline",
                repository_name=self.name,
            )
        self._lifecycle.ensure_started()

    @staticmethod
    def _normalise_path(path: str) -> str:
        """Normalise an artifact path to ensure it starts with ``/``.

        Args:
            path: The input path.

        Returns:
            The path with a leading ``/`` and no trailing ``/``.
        """
        normalised = path.strip()
        if not normalised.startswith("/"):
            normalised = f"/{normalised}"
        normalised = normalised.rstrip("/")
        return normalised if normalised else "/"

    @staticmethod
    def _detect_content_type(path: str) -> str | None:
        """Detect MIME type from file extension.

        Uses Python's built-in ``mimetypes`` module for extension-based
        type guessing.

        Args:
            path: The file path to inspect.

        Returns:
            The guessed MIME type, or ``None`` if it cannot be determined.
        """
        guessed_type, _ = mimetypes.guess_type(path)
        return guessed_type

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """Recursively merge *override* into *base* (in-place).

        For nested dicts, the merge recurses.  For all other types, the
        *override* value replaces the *base* value.

        Args:
            base: The base dictionary to merge into.
            override: The dictionary whose values take precedence.

        Returns:
            The *base* dict (mutated in place) for convenience.
        """
        for key, value in override.items():
            if (
                key in base
                and isinstance(base[key], dict)
                and isinstance(value, dict)
            ):
                HostedRepository._deep_merge(base[key], value)
            else:
                base[key] = value
        return base

    @staticmethod
    def _component_to_dict(component: Component) -> dict[str, Any]:
        """Serialise a Component to a dictionary.

        Uses the component's ``to_dict()`` if available; otherwise builds
        a minimal representation from known fields.

        Args:
            component: The component to serialise.

        Returns:
            A serialisable dictionary.
        """
        try:
            return component.to_dict()
        except Exception:
            return {
                "id": component.id,
                "repository_name": component.repository_name,
                "namespace": component.namespace,
                "name": component.name,
                "version": component.version,
            }

    # -----------------------------------------------------------------------
    # String Representation
    # -----------------------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return (
            f"<HostedRepository {self.name} "
            f"(format={self.repository.format}, "
            f"policy={self._get_write_policy()})>"
        )


# ---------------------------------------------------------------------------
# Module Initialisation Logging
# ---------------------------------------------------------------------------

logger.debug(
    "Hosted repository module loaded — exports: %s",
    ", ".join(__all__),
)
