"""
Artifact Upload Handling Service.

This module implements the ``UploadManager`` — the primary entry point for all
artifact uploads to hosted repositories.  It replaces ``UploadManagerImpl.java``
from the Sonatype Nexus Repository Java source system.

**Feature Coverage:**

- **F-101** (Multi-Format Repository Support): Handles uploads for all seven
  supported repository formats (Maven, npm, Docker, NuGet, PyPI, APT, Raw)
  through a format-agnostic interface.
- **F-102** (Repository Types): Enforces that only ``hosted`` repositories
  accept uploads — proxy and group repositories reject upload requests.
- **F-201 / F-202** (BlobStore): Stores binary artifact data through the
  abstract BlobStore interface (File or S3 backends).

**Architecture Context:**

Replaces the following Java components:
    - ``UploadManagerImpl.java`` — Artifact upload orchestration
    - Hosted repository facet upload logic — write policy enforcement
    - Component/Asset record creation within MyBatis transaction scope

**Key Responsibilities:**

1. Repository validation (exists, is hosted, is online)
2. Write policy enforcement (ALLOW, ALLOW_ONCE, DENY)
3. Checksum computation and verification (SHA-1, SHA-256, MD5)
4. Content type detection via filename extension and magic bytes
5. Binary blob storage through BlobStore abstraction
6. Component and Asset record creation/update in a single DB transaction
7. Event emission for audit logging, search indexing, and webhook dispatch
8. Component and Asset deletion with BlobStore cleanup

**Transaction Safety:**

All database operations within a single upload are wrapped in a transaction.
On failure, the transaction is rolled back and any stored blobs are cleaned up
to prevent orphaned data.

**Write Policies:**

- ``ALLOW`` (default): Overwrites are permitted; existing assets are updated.
- ``ALLOW_ONCE``: First write succeeds; subsequent writes to the same path
  are rejected (immutable artifacts, e.g., Maven releases).
- ``DENY``: Repository is read-only; all uploads are rejected.

**Concurrency:**

Race conditions during concurrent uploads of the same component coordinates
are handled via ``db.session.merge()`` and unique constraint catch-and-retry
logic, matching the Java source system's conflict resolution strategy.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, BinaryIO

from flask import current_app

from src.app.extensions import db
from src.app.models.repository import Repository
from src.app.models.component import Component
from src.app.models.asset import Asset
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.events.event_bus import emit_event
from src.app.events.event_types import (
    EventType,
    ComponentEventPayload,
    AssetEventPayload,
)
from src.app.utils.helpers import compute_checksums, detect_mime_type, generate_uuid
from src.app.utils.validators import validate_path
from src.app.storage import create_blobstore_from_model

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for upload operations, write policy enforcement,
# checksum verification, storage errors, and deletion events.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-Level Constants
# ---------------------------------------------------------------------------

# Maximum upload size in bytes (default 1 GB, configurable via app config)
DEFAULT_MAX_UPLOAD_SIZE: int = 1024 * 1024 * 1024  # 1 GB

# Supported write policies for hosted repositories
WRITE_POLICY_ALLOW: str = "ALLOW"
WRITE_POLICY_ALLOW_ONCE: str = "ALLOW_ONCE"
WRITE_POLICY_DENY: str = "DENY"

# Set of all valid write policies for validation
_VALID_WRITE_POLICIES: frozenset[str] = frozenset({
    WRITE_POLICY_ALLOW,
    WRITE_POLICY_ALLOW_ONCE,
    WRITE_POLICY_DENY,
})

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "UploadManager",
    "UploadError",
    "DEFAULT_MAX_UPLOAD_SIZE",
    "WRITE_POLICY_ALLOW",
    "WRITE_POLICY_ALLOW_ONCE",
    "WRITE_POLICY_DENY",
]


# ===========================================================================
# Custom Exception
# ===========================================================================


class UploadError(Exception):
    """Custom exception for upload operation failures.

    Raised when an upload cannot proceed due to:
    - Repository validation failures (not found, not hosted, offline)
    - Write policy violations (DENY, ALLOW_ONCE duplicate)
    - Checksum verification mismatches
    - BlobStore storage errors
    - Maximum upload size exceeded
    - Invalid component coordinates or asset paths

    Attributes:
        message: Human-readable error description.
        status_code: Suggested HTTP status code for API responses.
        details: Optional dictionary with additional error context.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialise an UploadError.

        Args:
            message: Human-readable error description.
            status_code: Suggested HTTP status code (default 400).
            details: Optional dictionary with additional error context.
        """
        super().__init__(message)
        self.message: str = message
        self.status_code: int = status_code
        self.details: dict[str, Any] = details or {}


# ===========================================================================
# UploadManager Service Class
# ===========================================================================


class UploadManager:
    """Service for managing artifact uploads and deletions in hosted repositories.

    This class is the **primary entry point** for all artifact upload and
    deletion operations.  It replaces ``UploadManagerImpl.java`` from the
    Java source system, providing:

    - Component-level uploads (``upload_component``) with multi-asset support
    - Single-asset uploads (``upload_asset``) for metadata and raw files
    - Component deletion (``delete_component``) with cascading asset cleanup
    - Single-asset deletion (``delete_asset``) with orphan component cleanup

    **Usage Example:**

    .. code-block:: python

        manager = UploadManager()
        component = manager.upload_component(
            repository_name="maven-hosted",
            namespace="com.example",
            name="my-lib",
            version="1.0.0",
            assets=[{
                "filename": "my-lib-1.0.0.jar",
                "data": jar_file_stream,
            }],
        )

    **Thread Safety:**

    The ``UploadManager`` is stateless and safe to use from multiple threads.
    Database session isolation is provided by Flask-SQLAlchemy's scoped session.
    """

    def __init__(self) -> None:
        """Initialise the UploadManager.

        Creates a module-level logger for structured logging of upload
        operations.  No other state is initialised — the manager is designed
        to be lightweight and stateless.
        """
        self.logger: logging.Logger = logging.getLogger(__name__)

    # -----------------------------------------------------------------------
    # Public API — Component Upload
    # -----------------------------------------------------------------------

    def upload_component(
        self,
        repository_name: str,
        namespace: str | None,
        name: str,
        version: str | None,
        assets: list[dict[str, Any]],
    ) -> Component:
        """Upload a component with one or more assets to a hosted repository.

        This is the **main upload entry point** — it orchestrates the entire
        upload lifecycle: repository validation, write policy enforcement,
        component record creation, per-asset checksum computation, content
        type detection, BlobStore persistence, and event emission.

        All database operations are wrapped in a single transaction.  On
        failure, the transaction is rolled back and any already-stored blobs
        are cleaned up to prevent orphaned storage data.

        Args:
            repository_name: Name of the target hosted repository.
            namespace: Component namespace (e.g., Maven groupId, npm scope).
                ``None`` for formats that don't use namespaces.
            name: Component name (e.g., Maven artifactId, npm package name).
            version: Component version string. ``None`` for formats that
                don't use explicit versions (e.g., some raw format uploads).
            assets: List of asset dictionaries, each containing:
                - ``filename`` (str): Original filename of the uploaded file.
                - ``data`` (BinaryIO | bytes): File content as a stream or
                  raw bytes.
                - ``content_type`` (str, optional): Explicit MIME type. If
                  omitted, auto-detected from filename and data.
                - ``path`` (str, optional): Explicit storage path. If omitted,
                  generated from component coordinates and filename.
                - ``checksums`` (dict[str, str], optional): Client-provided
                  checksums for verification (keys: ``sha1``, ``sha256``,
                  ``md5``).

        Returns:
            The created or updated :class:`Component` instance with all
            associated :class:`Asset` records.

        Raises:
            UploadError: If the upload fails for any reason (repository
                validation, write policy, checksum mismatch, storage error).
        """
        self.logger.info(
            "Starting component upload: repo=%s, namespace=%s, name=%s, "
            "version=%s, asset_count=%d",
            repository_name,
            namespace,
            name,
            version,
            len(assets),
        )

        # -- Step 1: Validate repository ------------------------------------
        repository = self._validate_repository(repository_name)

        # -- Step 2: Validate component coordinates -------------------------
        if not name or not name.strip():
            raise UploadError(
                "Component name must not be empty",
                status_code=400,
                details={"field": "name"},
            )

        # -- Step 3: Check maximum upload size ------------------------------
        max_upload_size = current_app.config.get(
            "MAX_UPLOAD_SIZE", DEFAULT_MAX_UPLOAD_SIZE
        )

        # -- Step 4: Find or create the Component record --------------------
        stored_blob_refs: list[str] = []
        try:
            component = self._find_or_create_component(
                repository_name=repository_name,
                namespace=namespace,
                name=name,
                version=version,
            )
            db.session.flush()

            # -- Step 5: Process each asset ---------------------------------
            created_assets: list[Asset] = []
            for asset_data in assets:
                filename = asset_data.get("filename", "")
                data = asset_data.get("data")
                explicit_content_type = asset_data.get("content_type")
                explicit_path = asset_data.get("path")
                expected_checksums = asset_data.get("checksums")

                if data is None:
                    raise UploadError(
                        f"Asset '{filename}' has no data",
                        status_code=400,
                        details={"filename": filename},
                    )

                # Read data into bytes for processing
                raw_data = self._read_data(data)

                # Enforce maximum upload size
                if len(raw_data) > max_upload_size:
                    raise UploadError(
                        f"Asset '{filename}' exceeds maximum upload size "
                        f"of {max_upload_size} bytes (actual: {len(raw_data)})",
                        status_code=413,
                        details={
                            "filename": filename,
                            "size": len(raw_data),
                            "max_size": max_upload_size,
                        },
                    )

                # Compute and verify checksums
                checksums = self._verify_checksums(raw_data, expected_checksums)

                # Detect content type
                content_type = explicit_content_type or detect_mime_type(
                    filename, raw_data
                )

                # Validate strict content type if configured
                self._check_strict_content_type(
                    repository, content_type, filename
                )

                # Determine asset path
                asset_path = explicit_path or self._generate_asset_path(
                    repository.format, namespace, name, version, filename
                )
                validate_path(asset_path)

                # Enforce write policy for this asset path
                self._check_write_policy(repository, asset_path)

                # Store blob in BlobStore
                blob_ref = self._store_blob(
                    repository, raw_data, asset_path, content_type
                )
                stored_blob_refs.append(blob_ref)

                # Create asset record
                asset = self._create_asset(
                    component=component,
                    repository_name=repository_name,
                    path=asset_path,
                    blob_ref=blob_ref,
                    checksums=checksums,
                    size=len(raw_data),
                    content_type=content_type,
                )
                created_assets.append(asset)

            # -- Step 6: Commit the transaction -----------------------------
            db.session.commit()

            self.logger.info(
                "Component upload successful: repo=%s, component=%s/%s/%s, "
                "assets_created=%d",
                repository_name,
                namespace,
                name,
                version,
                len(created_assets),
            )

            # -- Step 7: Emit event -----------------------------------------
            try:
                emit_event(
                    EventType.COMPONENT_UPLOADED,
                    payload={
                        "repository_name": repository_name,
                        "component_name": name,
                        "component_version": version,
                        "namespace": namespace,
                        "format": repository.format,
                        "component_id": component.id,
                        "asset_count": len(created_assets),
                    },
                )
            except Exception as event_err:
                # Event emission failures must never break the upload
                self.logger.warning(
                    "Failed to emit COMPONENT_UPLOADED event: %s",
                    str(event_err),
                )

            return component

        except UploadError:
            # Roll back DB and clean up blobs on known upload errors
            db.session.rollback()
            self._cleanup_blobs(repository_name, stored_blob_refs)
            raise
        except Exception as exc:
            # Roll back DB and clean up blobs on unexpected errors
            db.session.rollback()
            self._cleanup_blobs(repository_name, stored_blob_refs)
            self.logger.error(
                "Unexpected error during component upload: %s",
                str(exc),
                exc_info=True,
            )
            raise UploadError(
                f"Upload failed due to an internal error: {str(exc)}",
                status_code=500,
            ) from exc

    # -----------------------------------------------------------------------
    # Public API — Single Asset Upload
    # -----------------------------------------------------------------------

    def upload_asset(
        self,
        repository_name: str,
        path: str,
        data: BinaryIO,
        content_type: str | None = None,
        component_id: int | None = None,
    ) -> Asset:
        """Upload a single asset to a hosted repository.

        A simplified upload method for metadata files, raw format uploads,
        and other assets that don't require full component coordinate
        resolution.  This is used for:

        - Repository-level metadata files (``maven-metadata.xml``)
        - Package index files (``Packages.gz`` for APT)
        - Raw format uploads with no component structure
        - Standalone files attached to an existing component

        Args:
            repository_name: Name of the target hosted repository.
            path: Storage path for the asset within the repository.
            data: Binary content as a readable stream.
            content_type: Explicit MIME type. If ``None``, auto-detected.
            component_id: Optional parent component ID to associate with.

        Returns:
            The created :class:`Asset` instance.

        Raises:
            UploadError: If the upload fails for any reason.
        """
        self.logger.info(
            "Starting single asset upload: repo=%s, path=%s, component_id=%s",
            repository_name,
            path,
            component_id,
        )

        # Validate repository
        repository = self._validate_repository(repository_name)

        # Validate path
        validate_path(path)

        stored_blob_ref: str | None = None
        try:
            # Read data into bytes
            raw_data = self._read_data(data)

            # Enforce maximum upload size
            max_upload_size = current_app.config.get(
                "MAX_UPLOAD_SIZE", DEFAULT_MAX_UPLOAD_SIZE
            )
            if len(raw_data) > max_upload_size:
                raise UploadError(
                    f"Asset exceeds maximum upload size of {max_upload_size} "
                    f"bytes (actual: {len(raw_data)})",
                    status_code=413,
                )

            # Compute checksums
            checksums = self._verify_checksums(raw_data, None)

            # Detect content type
            resolved_content_type = content_type or detect_mime_type(
                path, raw_data
            )

            # Enforce write policy
            self._check_write_policy(repository, path)

            # Store blob
            blob_ref = self._store_blob(
                repository, raw_data, path, resolved_content_type
            )
            stored_blob_ref = blob_ref

            # Resolve parent component if specified
            component: Component | None = None
            if component_id is not None:
                component = Component.query.filter_by(
                    id=component_id,
                    repository_name=repository_name,
                ).first()
                if component is None:
                    self.logger.warning(
                        "Component ID %d not found in repository '%s'; "
                        "creating orphan asset",
                        component_id,
                        repository_name,
                    )

            # Create asset record
            asset = self._create_asset(
                component=component,
                repository_name=repository_name,
                path=path,
                blob_ref=blob_ref,
                checksums=checksums,
                size=len(raw_data),
                content_type=resolved_content_type,
            )

            db.session.commit()

            self.logger.info(
                "Single asset upload successful: repo=%s, path=%s, "
                "size=%d, blob_ref=%s",
                repository_name,
                path,
                len(raw_data),
                blob_ref,
            )

            # Emit event
            try:
                emit_event(
                    EventType.ASSET_DOWNLOADED,
                    payload={
                        "repository_name": repository_name,
                        "asset_path": path,
                        "content_type": resolved_content_type,
                        "size": len(raw_data),
                        "upload": True,
                    },
                )
            except Exception as event_err:
                self.logger.warning(
                    "Failed to emit asset upload event: %s",
                    str(event_err),
                )

            return asset

        except UploadError:
            db.session.rollback()
            if stored_blob_ref:
                self._cleanup_blobs(repository_name, [stored_blob_ref])
            raise
        except Exception as exc:
            db.session.rollback()
            if stored_blob_ref:
                self._cleanup_blobs(repository_name, [stored_blob_ref])
            self.logger.error(
                "Unexpected error during asset upload: %s",
                str(exc),
                exc_info=True,
            )
            raise UploadError(
                f"Asset upload failed due to an internal error: {str(exc)}",
                status_code=500,
            ) from exc

    # -----------------------------------------------------------------------
    # Public API — Delete Component
    # -----------------------------------------------------------------------

    def delete_component(
        self,
        repository_name: str,
        component_id: int,
    ) -> bool:
        """Delete a component and all its associated assets from a repository.

        Performs cascading deletion:
        1. Retrieves all assets belonging to the component.
        2. Deletes each asset's blob from the BlobStore.
        3. Deletes all Asset records from the database.
        4. Deletes the Component record from the database.
        5. Emits a ``COMPONENT_DELETED`` event.

        Args:
            repository_name: Name of the repository containing the component.
            component_id: ID of the component to delete.

        Returns:
            ``True`` if the component was found and deleted successfully.
            ``False`` if the component was not found.

        Raises:
            UploadError: If a database or storage error occurs during deletion.
        """
        self.logger.info(
            "Starting component deletion: repo=%s, component_id=%d",
            repository_name,
            component_id,
        )

        try:
            # Find the component
            component = Component.query.filter_by(
                id=component_id,
                repository_name=repository_name,
            ).first()

            if component is None:
                self.logger.warning(
                    "Component not found for deletion: repo=%s, id=%d",
                    repository_name,
                    component_id,
                )
                return False

            # Capture component info before deletion for event emission
            component_name = component.name
            component_version = component.version
            component_namespace = component.namespace

            # Get repository for BlobStore access
            repository = Repository.query.filter_by(
                name=repository_name
            ).first()

            # Delete blobs from BlobStore for each asset
            assets_query = Asset.query.filter_by(
                component_id=component_id,
                repository_name=repository_name,
            )
            assets_list = assets_query.all()

            for asset in assets_list:
                if asset.blob_ref and repository:
                    self._delete_blob(repository, asset.blob_ref)
                # Delete asset record
                db.session.delete(asset)

            # Delete the component record
            db.session.delete(component)
            db.session.commit()

            self.logger.info(
                "Component deleted: repo=%s, component=%s/%s/%s, "
                "assets_deleted=%d",
                repository_name,
                component_namespace,
                component_name,
                component_version,
                len(assets_list),
            )

            # Emit event
            try:
                repo_format = repository.format if repository else ""
                emit_event(
                    EventType.COMPONENT_DELETED,
                    payload={
                        "repository_name": repository_name,
                        "component_name": component_name,
                        "component_version": component_version,
                        "namespace": component_namespace,
                        "format": repo_format,
                        "component_id": component_id,
                        "assets_deleted": len(assets_list),
                    },
                )
            except Exception as event_err:
                self.logger.warning(
                    "Failed to emit COMPONENT_DELETED event: %s",
                    str(event_err),
                )

            return True

        except Exception as exc:
            db.session.rollback()
            self.logger.error(
                "Error deleting component: repo=%s, id=%d, error=%s",
                repository_name,
                component_id,
                str(exc),
                exc_info=True,
            )
            raise UploadError(
                f"Failed to delete component: {str(exc)}",
                status_code=500,
            ) from exc

    # -----------------------------------------------------------------------
    # Public API — Delete Asset
    # -----------------------------------------------------------------------

    def delete_asset(
        self,
        repository_name: str,
        asset_id: int,
    ) -> bool:
        """Delete a single asset from a repository.

        After deleting the asset, checks whether the parent component has
        any remaining assets.  If the component becomes orphaned (zero
        assets), it is also deleted to prevent stale component records.

        Args:
            repository_name: Name of the repository containing the asset.
            asset_id: ID of the asset to delete.

        Returns:
            ``True`` if the asset was found and deleted successfully.
            ``False`` if the asset was not found.

        Raises:
            UploadError: If a database or storage error occurs during deletion.
        """
        self.logger.info(
            "Starting asset deletion: repo=%s, asset_id=%d",
            repository_name,
            asset_id,
        )

        try:
            # Find the asset
            asset = Asset.query.filter_by(
                id=asset_id,
                repository_name=repository_name,
            ).first()

            if asset is None:
                self.logger.warning(
                    "Asset not found for deletion: repo=%s, id=%d",
                    repository_name,
                    asset_id,
                )
                return False

            # Capture asset info for logging and event
            asset_path = asset.path
            asset_blob_ref = asset.blob_ref
            parent_component_id = asset.component_id

            # Get repository for BlobStore access
            repository = Repository.query.filter_by(
                name=repository_name
            ).first()

            # Delete blob from BlobStore
            if asset_blob_ref and repository:
                self._delete_blob(repository, asset_blob_ref)

            # Delete the asset record
            db.session.delete(asset)
            db.session.flush()

            # Check if the parent component has become orphaned
            orphaned_component_deleted = False
            if parent_component_id is not None:
                remaining_assets_count = Asset.query.filter_by(
                    component_id=parent_component_id,
                    repository_name=repository_name,
                ).count()

                if remaining_assets_count == 0:
                    orphan_component = Component.query.filter_by(
                        id=parent_component_id,
                        repository_name=repository_name,
                    ).first()
                    if orphan_component is not None:
                        self.logger.info(
                            "Deleting orphaned component: repo=%s, "
                            "component_id=%d, name=%s",
                            repository_name,
                            parent_component_id,
                            orphan_component.name,
                        )
                        db.session.delete(orphan_component)
                        orphaned_component_deleted = True

            db.session.commit()

            self.logger.info(
                "Asset deleted: repo=%s, path=%s, "
                "orphaned_component_deleted=%s",
                repository_name,
                asset_path,
                orphaned_component_deleted,
            )

            return True

        except Exception as exc:
            db.session.rollback()
            self.logger.error(
                "Error deleting asset: repo=%s, id=%d, error=%s",
                repository_name,
                asset_id,
                str(exc),
                exc_info=True,
            )
            raise UploadError(
                f"Failed to delete asset: {str(exc)}",
                status_code=500,
            ) from exc

    # ===================================================================
    # Private Methods — Repository Validation
    # ===================================================================

    def _validate_repository(self, repository_name: str) -> Repository:
        """Validate that the target repository exists, is hosted, and is online.

        Args:
            repository_name: Name of the repository to validate.

        Returns:
            The validated :class:`Repository` instance.

        Raises:
            UploadError: If the repository is not found, not hosted, or offline.
        """
        repository = Repository.query.filter_by(name=repository_name).first()

        if repository is None:
            self.logger.warning(
                "Upload rejected: repository '%s' not found",
                repository_name,
            )
            raise UploadError(
                f"Repository '{repository_name}' not found",
                status_code=404,
                details={"repository_name": repository_name},
            )

        # Only hosted repositories accept uploads
        if not repository.is_hosted:
            self.logger.warning(
                "Upload rejected: repository '%s' is type '%s' "
                "(only 'hosted' accepts uploads)",
                repository_name,
                repository.type,
            )
            raise UploadError(
                f"Repository '{repository_name}' is of type "
                f"'{repository.type}' — only hosted repositories "
                f"accept uploads",
                status_code=400,
                details={
                    "repository_name": repository_name,
                    "type": repository.type,
                },
            )

        # Repository must be online
        if not repository.online:
            self.logger.warning(
                "Upload rejected: repository '%s' is offline",
                repository_name,
            )
            raise UploadError(
                f"Repository '{repository_name}' is offline",
                status_code=503,
                details={"repository_name": repository_name},
            )

        return repository

    # ===================================================================
    # Private Methods — Write Policy Enforcement
    # ===================================================================

    def _check_write_policy(
        self,
        repository: Repository,
        asset_path: str,
    ) -> None:
        """Enforce the repository's write policy for the given asset path.

        The write policy is stored in the repository's attributes at:
        ``attributes["storage"]["writePolicy"]``

        Policies:
        - ``ALLOW`` (default): No restriction — overwrites permitted.
        - ``ALLOW_ONCE``: First write succeeds; subsequent writes to the
          same path are rejected.
        - ``DENY``: All writes are rejected.

        Args:
            repository: The target repository instance.
            asset_path: The path being written to.

        Raises:
            UploadError: If the write policy rejects the operation.
        """
        # Extract write policy from repository attributes
        attributes = repository.attributes or {}
        storage_config = attributes.get("storage", {})
        write_policy = storage_config.get("writePolicy", WRITE_POLICY_ALLOW)

        self.logger.debug(
            "Checking write policy: repo=%s, policy=%s, path=%s",
            repository.name,
            write_policy,
            asset_path,
        )

        if write_policy == WRITE_POLICY_DENY:
            self.logger.warning(
                "Write policy DENY: repo=%s, path=%s",
                repository.name,
                asset_path,
            )
            raise UploadError(
                f"Repository '{repository.name}' does not allow writes "
                f"(write policy: DENY)",
                status_code=403,
                details={
                    "repository_name": repository.name,
                    "write_policy": WRITE_POLICY_DENY,
                    "path": asset_path,
                },
            )

        if write_policy == WRITE_POLICY_ALLOW_ONCE:
            # Check if an asset already exists at this path
            existing_asset = Asset.query.filter_by(
                repository_name=repository.name,
                path=asset_path,
            ).first()

            if existing_asset is not None:
                self.logger.warning(
                    "Write policy ALLOW_ONCE: artifact already exists at "
                    "repo=%s, path=%s",
                    repository.name,
                    asset_path,
                )
                raise UploadError(
                    f"Artifact already exists at path '{asset_path}' and "
                    f"repository '{repository.name}' has ALLOW_ONCE policy",
                    status_code=409,
                    details={
                        "repository_name": repository.name,
                        "write_policy": WRITE_POLICY_ALLOW_ONCE,
                        "path": asset_path,
                        "existing_asset_id": existing_asset.id,
                    },
                )

        # ALLOW policy — no restriction
        self.logger.debug(
            "Write policy check passed: repo=%s, policy=%s, path=%s",
            repository.name,
            write_policy,
            asset_path,
        )

    # ===================================================================
    # Private Methods — Checksum Verification
    # ===================================================================

    def _verify_checksums(
        self,
        data: bytes | BinaryIO,
        expected_checksums: dict[str, str] | None,
    ) -> dict[str, str]:
        """Compute and optionally verify checksums for uploaded data.

        Always computes SHA-1, SHA-256, and MD5 checksums.  If
        ``expected_checksums`` are provided (from client-supplied headers),
        each provided checksum is compared against the computed value.

        Args:
            data: The artifact binary data (bytes or file-like object).
            expected_checksums: Optional dictionary of expected checksums
                with keys ``sha1``, ``sha256``, ``md5``.  Only provided
                keys are verified.

        Returns:
            Dictionary with computed checksums:
            ``{'sha1': '…', 'sha256': '…', 'md5': '…'}``

        Raises:
            UploadError: If any provided expected checksum does not match.
        """
        # Compute all three checksums using the utility function
        computed = compute_checksums(data)

        self.logger.debug(
            "Checksums computed: sha1=%s, sha256=%s, md5=%s",
            computed.get("sha1", "")[:12] + "...",
            computed.get("sha256", "")[:12] + "...",
            computed.get("md5", "")[:12] + "...",
        )

        # Verify against expected checksums if provided
        if expected_checksums:
            for algo, expected_value in expected_checksums.items():
                algo_lower = algo.lower()
                if algo_lower not in computed:
                    continue

                computed_value = computed[algo_lower]
                if computed_value.lower() != expected_value.lower():
                    self.logger.error(
                        "Checksum verification failed: algorithm=%s, "
                        "expected=%s, computed=%s",
                        algo_lower,
                        expected_value,
                        computed_value,
                    )
                    raise UploadError(
                        f"Checksum verification failed for {algo_lower}: "
                        f"expected '{expected_value}', "
                        f"computed '{computed_value}'",
                        status_code=400,
                        details={
                            "algorithm": algo_lower,
                            "expected": expected_value,
                            "computed": computed_value,
                        },
                    )

            self.logger.debug("All provided checksums verified successfully")

        return computed

    # ===================================================================
    # Private Methods — BlobStore Integration
    # ===================================================================

    def _store_blob(
        self,
        repository: Repository,
        data: bytes | BinaryIO,
        path: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Store binary data in the repository's configured BlobStore.

        Retrieves the BlobStore configuration for the repository, instantiates
        the appropriate backend (File or S3), and stores the blob data.

        Args:
            repository: The target repository instance.
            data: Binary data to store.
            path: Asset path (used for logging context).
            content_type: MIME type of the content.

        Returns:
            The blob reference string (``store_name:blob_uuid``) for use
            as the ``blob_ref`` field on the Asset record.

        Raises:
            UploadError: If the BlobStore configuration is not found or
                storage fails.
        """
        self.logger.debug(
            "Storing blob: repo=%s, blobstore=%s, path=%s",
            repository.name,
            repository.blob_store_name,
            path,
        )

        # Get BlobStore configuration from database
        config = BlobStoreConfig.query.get(repository.blob_store_name)
        if config is None:
            self.logger.error(
                "BlobStore configuration not found: '%s' for repository '%s'",
                repository.blob_store_name,
                repository.name,
            )
            raise UploadError(
                f"BlobStore '{repository.blob_store_name}' not found — "
                f"cannot store artifact for repository '{repository.name}'",
                status_code=500,
                details={
                    "blob_store_name": repository.blob_store_name,
                    "repository_name": repository.name,
                },
            )

        try:
            # Instantiate the appropriate BlobStore backend
            blobstore = create_blobstore_from_model(config)

            # Ensure BlobStore is started
            if not blobstore.is_started:
                blobstore.start()

            # Store the blob (auto-generates BlobId)
            blob = blobstore.create(
                blob_id=None,
                data=data,
                content_type=content_type,
                headers={"path": path},
            )

            blob_ref = str(blob.blob_id)

            self.logger.debug(
                "Blob stored successfully: blob_ref=%s, size=%d",
                blob_ref,
                blob.size,
            )

            return blob_ref

        except UploadError:
            raise
        except Exception as exc:
            self.logger.error(
                "BlobStore storage error: repo=%s, blobstore=%s, path=%s, "
                "error=%s",
                repository.name,
                repository.blob_store_name,
                path,
                str(exc),
                exc_info=True,
            )
            raise UploadError(
                f"Failed to store blob in BlobStore "
                f"'{repository.blob_store_name}': {str(exc)}",
                status_code=500,
            ) from exc

    # ===================================================================
    # Private Methods — Component Record Management
    # ===================================================================

    def _find_or_create_component(
        self,
        repository_name: str,
        namespace: str | None,
        name: str,
        version: str | None,
    ) -> Component:
        """Find an existing component by coordinates or create a new one.

        Searches for a component matching the coordinate tuple
        ``(repository_name, namespace, name, version)``.  If found, returns
        it.  If not found, creates a new Component record.

        Handles race conditions during concurrent uploads by catching
        unique constraint violations and falling back to ``db.session.merge()``.

        Args:
            repository_name: Name of the owning repository.
            namespace: Component namespace (may be ``None``).
            name: Component name.
            version: Component version (may be ``None``).

        Returns:
            The found or newly created :class:`Component` instance.
        """
        # Query for existing component with matching coordinates
        existing = Component.query.filter_by(
            repository_name=repository_name,
            namespace=namespace,
            name=name,
            version=version,
        ).first()

        if existing is not None:
            self.logger.debug(
                "Found existing component: repo=%s, namespace=%s, "
                "name=%s, version=%s, id=%d",
                repository_name,
                namespace,
                name,
                version,
                existing.id,
            )
            return existing

        # Create new component
        self.logger.info(
            "Creating new component: repo=%s, namespace=%s, name=%s, "
            "version=%s",
            repository_name,
            namespace,
            name,
            version,
        )

        component = Component(
            repository_name=repository_name,
            namespace=namespace,
            name=name,
            version=version,
        )

        try:
            db.session.add(component)
            db.session.flush()
        except Exception:
            # Handle race condition: another thread may have created the
            # same component between our SELECT and INSERT.  Fall back
            # to merge semantics.
            db.session.rollback()
            self.logger.debug(
                "Race condition detected during component creation; "
                "falling back to merge: repo=%s, name=%s, version=%s",
                repository_name,
                name,
                version,
            )
            existing = Component.query.filter_by(
                repository_name=repository_name,
                namespace=namespace,
                name=name,
                version=version,
            ).first()
            if existing is not None:
                return existing

            # If still not found, create again via merge
            component = Component(
                repository_name=repository_name,
                namespace=namespace,
                name=name,
                version=version,
            )
            component = db.session.merge(component)
            db.session.flush()

        return component

    # ===================================================================
    # Private Methods — Asset Record Management
    # ===================================================================

    def _create_asset(
        self,
        component: Component | None,
        repository_name: str,
        path: str,
        blob_ref: str,
        checksums: dict[str, str],
        size: int,
        content_type: str | None,
    ) -> Asset:
        """Create a new Asset record or update an existing one.

        If an asset already exists at the given path within the repository
        (and the write policy is ``ALLOW``), the existing record is updated
        with the new blob reference, checksums, size, and content type.
        Otherwise, a new Asset record is created.

        Args:
            component: Parent component (``None`` for standalone assets).
            repository_name: Name of the owning repository.
            path: Storage path within the repository.
            blob_ref: Reference to the stored blob in BlobStore.
            checksums: Dictionary with ``sha1``, ``sha256``, ``md5`` values.
            size: Size of the asset content in bytes.
            content_type: MIME type of the content.

        Returns:
            The created or updated :class:`Asset` instance.
        """
        # Check for existing asset at this path (for ALLOW policy updates)
        existing_asset = Asset.query.filter_by(
            repository_name=repository_name,
            path=path,
        ).first()

        if existing_asset is not None:
            # Update the existing asset record (ALLOW policy)
            self.logger.info(
                "Updating existing asset: repo=%s, path=%s, asset_id=%d",
                repository_name,
                path,
                existing_asset.id,
            )
            existing_asset.blob_ref = blob_ref
            existing_asset.checksum_sha1 = checksums.get("sha1")
            existing_asset.checksum_sha256 = checksums.get("sha256")
            existing_asset.checksum_md5 = checksums.get("md5")
            existing_asset.size = size
            existing_asset.content_type = content_type
            if component is not None:
                existing_asset.component_id = component.id
            db.session.add(existing_asset)
            db.session.flush()
            return existing_asset

        # Create a new asset record
        self.logger.debug(
            "Creating new asset: repo=%s, path=%s, size=%d, "
            "content_type=%s",
            repository_name,
            path,
            size,
            content_type,
        )

        asset = Asset(
            repository_name=repository_name,
            path=path,
            blob_ref=blob_ref,
            checksum_sha1=checksums.get("sha1"),
            checksum_sha256=checksums.get("sha256"),
            checksum_md5=checksums.get("md5"),
            size=size,
            content_type=content_type,
            component_id=component.id if component is not None else None,
        )

        db.session.add(asset)
        db.session.flush()

        return asset

    # ===================================================================
    # Private Helper Methods
    # ===================================================================

    @staticmethod
    def _read_data(data: bytes | BinaryIO) -> bytes:
        """Read data from a file-like object or return bytes directly.

        Ensures that the data is always available as a ``bytes`` object for
        checksum computation, size checking, and BlobStore storage.

        Args:
            data: Binary data as bytes or a readable stream.

        Returns:
            The data as a ``bytes`` object.
        """
        if isinstance(data, bytes):
            return data
        if hasattr(data, "read"):
            content = data.read()
            # Reset stream position for potential re-reads
            if hasattr(data, "seek"):
                data.seek(0)
            if isinstance(content, str):
                return content.encode("utf-8")
            return content
        return bytes(data)

    def _generate_asset_path(
        self,
        repo_format: str,
        namespace: str | None,
        name: str,
        version: str | None,
        filename: str,
    ) -> str:
        """Generate a storage path for an asset based on format conventions.

        Path conventions vary by repository format:
        - Maven: ``/{groupId}/{artifactId}/{version}/{filename}``
        - npm: ``/{scope}/{name}/-/{filename}``
        - Docker: ``/v2/{name}/...``
        - PyPI: ``/packages/{name}/{version}/{filename}``
        - NuGet: ``/{name}/{version}/{filename}``
        - APT: ``/pool/{section}/{name}/{filename}``
        - Raw: ``/{filename}``

        Args:
            repo_format: Repository format string.
            namespace: Component namespace.
            name: Component name.
            version: Component version.
            filename: Original filename.

        Returns:
            The generated asset path string.
        """
        parts: list[str] = []

        if repo_format == "maven2":
            # Maven convention: group/artifact/version/filename
            if namespace:
                parts.extend(namespace.replace(".", "/").split("/"))
            parts.append(name)
            if version:
                parts.append(version)
            parts.append(filename)

        elif repo_format == "npm":
            # npm convention: scope/name/-/filename
            if namespace:
                parts.append(namespace)
            parts.append(name)
            parts.append("-")
            parts.append(filename)

        elif repo_format == "docker":
            # Docker: v2/name/...
            parts.append("v2")
            if namespace:
                parts.append(namespace)
            parts.append(name)
            parts.append(filename)

        elif repo_format == "pypi":
            # PyPI: packages/name/version/filename
            parts.append("packages")
            parts.append(name)
            if version:
                parts.append(version)
            parts.append(filename)

        elif repo_format == "nuget":
            # NuGet: name/version/filename
            parts.append(name)
            if version:
                parts.append(version)
            parts.append(filename)

        elif repo_format == "apt":
            # APT: pool/section/name/filename
            parts.append("pool")
            if namespace:
                parts.append(namespace)
            parts.append(name)
            parts.append(filename)

        else:
            # Raw and other formats: simple path
            if namespace:
                parts.append(namespace)
            if name:
                parts.append(name)
            if version:
                parts.append(version)
            parts.append(filename)

        return "/".join(p for p in parts if p)

    def _check_strict_content_type(
        self,
        repository: Repository,
        content_type: str,
        filename: str,
    ) -> None:
        """Enforce strict content type validation if configured.

        When the repository has ``strictContentTypeValidation`` enabled in
        its storage attributes, the detected content type must not be the
        generic ``application/octet-stream`` — a specific MIME type is
        required.

        Args:
            repository: The target repository.
            content_type: The detected MIME type.
            filename: The original filename (for error context).

        Raises:
            UploadError: If strict validation is enabled and the content
                type is generic/unknown.
        """
        attributes = repository.attributes or {}
        storage_config = attributes.get("storage", {})
        strict_validation = storage_config.get(
            "strictContentTypeValidation", False
        )

        if strict_validation and content_type == "application/octet-stream":
            self.logger.warning(
                "Strict content type validation failed: repo=%s, "
                "filename=%s, content_type=%s",
                repository.name,
                filename,
                content_type,
            )
            raise UploadError(
                f"Content type could not be determined for '{filename}' "
                f"and repository '{repository.name}' requires strict "
                f"content type validation",
                status_code=400,
                details={
                    "filename": filename,
                    "content_type": content_type,
                    "repository_name": repository.name,
                },
            )

    def _delete_blob(
        self,
        repository: Repository,
        blob_ref: str,
    ) -> None:
        """Delete a blob from the repository's BlobStore.

        Errors during blob deletion are logged but do not propagate — the
        database record deletion takes priority.  Orphaned blobs will be
        cleaned up during the next BlobStore maintenance run.

        Args:
            repository: The repository whose BlobStore to use.
            blob_ref: The blob reference string to delete.
        """
        try:
            config = BlobStoreConfig.query.get(repository.blob_store_name)
            if config is None:
                self.logger.warning(
                    "BlobStore config not found for blob deletion: '%s'",
                    repository.blob_store_name,
                )
                return

            blobstore = create_blobstore_from_model(config)
            if not blobstore.is_started:
                blobstore.start()

            from src.app.storage.blobstore import BlobId
            blob_id = BlobId.from_string(blob_ref)
            blobstore.delete(blob_id, soft=True)

            self.logger.debug("Blob deleted (soft): %s", blob_ref)

        except Exception as exc:
            # Blob deletion failures are non-fatal — orphaned blobs will
            # be cleaned up during maintenance compaction
            self.logger.warning(
                "Failed to delete blob '%s': %s",
                blob_ref,
                str(exc),
            )

    def _cleanup_blobs(
        self,
        repository_name: str,
        blob_refs: list[str],
    ) -> None:
        """Clean up already-stored blobs after a failed upload transaction.

        This method is called during error recovery to remove blobs that
        were stored before the transaction was rolled back.  Errors during
        cleanup are logged but never propagated.

        Args:
            repository_name: Name of the repository.
            blob_refs: List of blob reference strings to clean up.
        """
        if not blob_refs:
            return

        self.logger.info(
            "Cleaning up %d blob(s) after failed upload: repo=%s",
            len(blob_refs),
            repository_name,
        )

        try:
            repository = Repository.query.filter_by(
                name=repository_name
            ).first()
            if repository is None:
                self.logger.warning(
                    "Repository '%s' not found during blob cleanup",
                    repository_name,
                )
                return

            for blob_ref in blob_refs:
                self._delete_blob(repository, blob_ref)

        except Exception as exc:
            self.logger.warning(
                "Error during blob cleanup: repo=%s, error=%s",
                repository_name,
                str(exc),
            )


# ---------------------------------------------------------------------------
# Module-Level Logging
# ---------------------------------------------------------------------------
logger.debug(
    "Upload manager module loaded — exports: %s",
    ", ".join(__all__),
)
