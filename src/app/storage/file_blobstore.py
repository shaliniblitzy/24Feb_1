"""
Local filesystem BlobStore implementation (Feature F-201).

This module provides the default storage backend for standalone Nexus Repository
deployments.  It implements the abstract ``BlobStore`` interface using local
filesystem operations with the following key features:

- **Content-addressable storage**: Blob files are stored using ID-based
  directory nesting (e.g., ``content/55/0e/84/550e8400-...bytes``) for even
  distribution across directories and efficient lookup.

- **Atomic writes**: All blob content is first written to a temporary staging
  file, then atomically moved to its final location using ``os.replace()``.
  This guarantees that partial writes never corrupt the BlobStore.

- **Soft-delete**: The default ``delete()`` behaviour moves blobs to a
  ``.deleted/`` directory instead of permanently removing them.  This enables
  recovery (``undelete()``) and deferred permanent deletion (``compact()``).

- **Metadata sidecar files**: Every blob has a companion ``.properties`` file
  containing checksums (SHA-1, SHA-256, MD5), size, content type, and
  timestamps in JSON format.

- **Disk space monitoring**: The ``get_metrics()`` and ``_check_disk_space()``
  methods monitor available disk space and log warnings at 90% and errors at
  95% utilisation.

Replaces the Java File BlobStore implementation from Sonatype Nexus Repository.

Strategy Pattern: This is a concrete implementation of the abstract ``BlobStore``
base class defined in ``src/app/storage/blobstore.py`` (AAP Section 0.4.3).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Generator, Optional

from src.app.storage.blobstore import (
    BlobAlreadyExistsError,
    BlobAttributes,
    Blob,
    BlobId,
    BlobStore,
    BlobStoreConfiguration,
    BlobStoreError,
    BlobStoreFullError,
    BlobStoreMetrics,
    BlobStoreType,
    DEFAULT_SOFT_DELETE_RETENTION_DAYS,
)
from src.app.utils.file_utils import (
    atomic_write,
    ensure_directory,
    file_exists,
    get_directory_size,
    list_files,
    remove_directory,
    safe_join,
    sanitize_path,
)
from src.app.utils.helpers import (
    compute_checksums,
    format_file_size,
    generate_uuid,
)

# ---------------------------------------------------------------------------
# Module-level logger (replaces SLF4J 1.7.36 from the Java source)
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Subdirectory for active blob content files.
CONTENT_DIR: str = "content"
#: Subdirectory for soft-deleted blobs (content + metadata together).
DELETED_DIR: str = ".deleted"
#: Subdirectory for temporary files during atomic writes.
STAGING_DIR: str = ".staging"
#: Subdirectory for metadata sidecar files.
METADATA_DIR: str = "metadata"

#: Metadata sidecar file extension.
PROPERTIES_EXTENSION: str = ".properties"
#: Blob content file extension.
BLOB_EXTENSION: str = ".bytes"

#: Number of directory nesting levels derived from the blob-ID prefix.
PATH_DEPTH: int = 3
#: Characters per directory segment (e.g. ``"a1"`` for segment length 2).
PATH_SEGMENT_LEN: int = 2

#: Chunk size (8 KB) for streaming read/write operations.
CHUNK_SIZE: int = 8192

#: Disk-usage ratio at which a WARNING is logged.
DISK_SPACE_WARNING_THRESHOLD: float = 0.90
#: Disk-usage ratio at which an ERROR is logged and writes are rejected.
DISK_SPACE_CRITICAL_THRESHOLD: float = 0.95


# =========================================================================
# FileBlobStore Class
# =========================================================================


class FileBlobStore(BlobStore):
    """Local filesystem BlobStore implementation (Feature F-201).

    Stores binary artifacts on the local filesystem using content-addressable
    storage with ID-based directory nesting, atomic writes via temp staging +
    ``os.replace()`` rename-on-commit, soft-delete via move to ``.deleted/``
    directory, and JSON metadata sidecar ``.properties`` files.

    This is the **default** storage backend for standalone deployments,
    replacing the Java File BlobStore from the source Nexus system.

    Directory structure::

        <root>/
        ├── content/         # Active blob content files (.bytes)
        │   └── a1/b2/c3/   # ID-prefix-based nesting
        ├── metadata/        # Metadata sidecar files (.properties)
        │   └── a1/b2/c3/
        ├── .deleted/        # Soft-deleted blobs (content + metadata)
        │   └── a1/b2/c3/
        └── .staging/        # Temporary files during atomic writes

    Args:
        name:      Unique name identifying this BlobStore instance.
        root_path: Base directory path for this BlobStore.
        config:    Optional configuration dictionary with backend-specific
                   settings.
    """

    def __init__(
        self,
        name: str,
        root_path: str | Path,
        config: dict[str, Any] | None = None,
    ) -> None:
        raw_config: dict[str, Any] = config or {}
        blob_config = BlobStoreConfiguration(
            name=name,
            store_type=BlobStoreType.FILE,
            config={**raw_config, "path": str(root_path)},
        )
        super().__init__(name=name, config=blob_config)

        self._root: Path = Path(root_path)
        self._content_dir: Path = self._root / CONTENT_DIR
        self._deleted_dir: Path = self._root / DELETED_DIR
        self._staging_dir: Path = self._root / STAGING_DIR
        self._metadata_dir: Path = self._root / METADATA_DIR

        # Create the directory structure on initialisation
        self._initialize_directories()
        logger.info("FileBlobStore initialized: %s at %s", name, root_path)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def store_type(self) -> str:  # type: ignore[override]
        """Return ``'file'`` as the backend type identifier."""
        return "file"

    @property
    def root_path(self) -> Path:
        """Return the root directory path for this BlobStore."""
        return self._root

    @property
    def is_available(self) -> bool:
        """Return ``True`` if the root directory exists and is writable."""
        try:
            return self._root.is_dir() and os.access(str(self._root), os.W_OK)
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Lifecycle Methods
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Initialise the filesystem BlobStore.

        Creates the directory structure, validates disk access, and marks
        the store as started.

        Raises:
            BlobStoreError: If the root directory cannot be created or
                accessed.
        """
        try:
            self._initialize_directories()
            self._check_disk_space()
            self._started = True
            logger.info(
                "FileBlobStore started: %s at %s", self._name, self._root
            )
        except BlobStoreFullError:
            # Disk-space critical is not a start failure — store can still
            # be used for reads; log the warning but mark as started.
            self._started = True
            logger.warning(
                "FileBlobStore started with critical disk space: %s",
                self._name,
            )
        except OSError as exc:
            raise BlobStoreError(
                f"Failed to start FileBlobStore '{self._name}': {exc}"
            ) from exc

    def stop(self) -> None:
        """Gracefully shut down the filesystem BlobStore.

        Sets the store to the stopped state.  No pending operations are
        interrupted — callers should ensure all CRUD operations have
        completed before calling ``stop()``.
        """
        self._started = False
        logger.info("FileBlobStore stopped: %s", self._name)

    # ------------------------------------------------------------------
    # Core CRUD Operations
    # ------------------------------------------------------------------

    def create(
        self,
        blob_id: BlobId | None,
        data: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> Blob:
        """Store binary data as a new blob with atomic-write semantics.

        Algorithm:
        1. Generate a ``BlobId`` if not provided.
        2. Write content to a staging file in ``.staging/``.
        3. Compute SHA-1, SHA-256, and MD5 checksums in a single pass.
        4. Atomically move the staging file to the content path via
           ``os.replace()``.
        5. Write a metadata sidecar ``.properties`` file.
        6. Return the fully populated ``Blob`` object.

        If **any** step fails, the staging file is cleaned up and the
        exception is re-raised.

        Args:
            blob_id:      Optional pre-determined blob identifier.
            data:         Binary payload as ``bytes`` or a readable stream.
            content_type: MIME type for the stored content.
            headers:      Additional metadata key-value pairs.

        Returns:
            The fully populated :class:`Blob` object.

        Raises:
            BlobStoreError:         On storage failure.
            BlobStoreFullError:     If disk space exceeds the critical threshold.
            BlobAlreadyExistsError: If *blob_id* already exists.
        """
        self._ensure_started()
        self._check_disk_space()

        # Step 1 — generate id
        if blob_id is None:
            blob_id = BlobId.generate(self._name)

        # Step 2 — resolve target path and guard against duplicates
        content_path = self._blob_path(blob_id)
        if content_path.exists():
            raise BlobAlreadyExistsError(blob_id)

        ensure_directory(content_path.parent)

        # Staging file lives on the same filesystem for atomic rename
        staging_path = self._staging_dir / f"{blob_id.value}.staging"
        ensure_directory(staging_path.parent)

        try:
            # Step 3 — stream data to staging with checksum computation
            sha1_h = hashlib.sha1()
            sha256_h = hashlib.sha256()
            md5_h = hashlib.md5()
            total_size: int = 0

            with open(str(staging_path), "wb") as staging_file:
                if isinstance(data, bytes):
                    staging_file.write(data)
                    sha1_h.update(data)
                    sha256_h.update(data)
                    md5_h.update(data)
                    total_size = len(data)
                else:
                    while True:
                        chunk = data.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        if isinstance(chunk, str):
                            chunk = chunk.encode("utf-8")
                        staging_file.write(chunk)
                        sha1_h.update(chunk)
                        sha256_h.update(chunk)
                        md5_h.update(chunk)
                        total_size += len(chunk)

                # Flush to disk for durability
                staging_file.flush()
                os.fsync(staging_file.fileno())

            # Step 4 — atomic commit (POSIX-atomic on the same filesystem)
            os.replace(str(staging_path), str(content_path))

            # Step 5 — build and persist metadata
            now = datetime.now(timezone.utc)
            attributes = BlobAttributes(
                content_type=content_type,
                sha1=sha1_h.hexdigest(),
                sha256=sha256_h.hexdigest(),
                md5=md5_h.hexdigest(),
                size=total_size,
                creation_time=now,
                headers=dict(headers) if headers else {},
            )
            self._write_properties(blob_id, attributes)

            # Step 6 — return Blob
            blob = Blob(blob_id=blob_id, attributes=attributes)
            logger.debug(
                "Blob created: %s (%s)",
                blob_id,
                format_file_size(total_size),
            )
            return blob

        except Exception:
            # Guarantee staging clean-up on any failure
            if staging_path.exists():
                try:
                    os.unlink(str(staging_path))
                except OSError:
                    pass
            raise

    def get(self, blob_id: BlobId) -> Blob | None:
        """Retrieve blob metadata by identifier.

        Does **not** read the blob content into memory — returns metadata
        only.  Use :meth:`get_stream` to read the actual content.

        Args:
            blob_id: Identifier of the blob to retrieve.

        Returns:
            :class:`Blob` with populated metadata, or ``None`` if not
            found.
        """
        self._ensure_started()

        content_path = self._blob_path(blob_id)
        if not content_path.is_file():
            return None

        # Read metadata from the sidecar file
        attributes = self._read_properties(blob_id)
        if attributes is None:
            # Fallback: reconstruct minimal attributes from file stats
            try:
                stat_info = content_path.stat()
                attributes = BlobAttributes(
                    size=stat_info.st_size,
                    creation_time=datetime.fromtimestamp(
                        stat_info.st_ctime, tz=timezone.utc
                    ),
                )
            except OSError as exc:
                logger.warning(
                    "Failed to stat blob file %s: %s", content_path, exc
                )
                return None

        return Blob(blob_id=blob_id, attributes=attributes)

    def get_stream(self, blob_id: BlobId) -> BinaryIO | None:
        """Return a file handle for streaming blob content.

        The caller is responsible for closing the returned file handle.
        Returns ``None`` if the blob does not exist.

        Args:
            blob_id: Identifier of the blob whose content to stream.

        Returns:
            A readable binary stream, or ``None``.
        """
        self._ensure_started()

        content_path = self._blob_path(blob_id)
        if not content_path.is_file():
            return None

        try:
            return open(str(content_path), "rb")
        except OSError as exc:
            logger.error(
                "Failed to open blob stream for %s: %s", blob_id, exc
            )
            return None

    def delete(self, blob_id: BlobId, soft: bool = True) -> bool:
        """Delete a blob from the store.

        * **Soft-delete** (``soft=True``, default): Moves the blob content
          and metadata to the ``.deleted/`` directory.  The blob can be
          recovered with :meth:`undelete` or permanently removed during
          :meth:`compact`.
        * **Hard-delete** (``soft=False``): Permanently removes the blob
          content and metadata files immediately.

        Args:
            blob_id: Identifier of the blob to delete.
            soft:    If ``True`` (default) perform a soft-delete.

        Returns:
            ``True`` if the blob was found and deleted, ``False`` if not
            found.
        """
        self._ensure_started()

        content_path = self._blob_path(blob_id)
        metadata_path = self._metadata_path(blob_id)

        if not content_path.is_file():
            return False

        try:
            if soft:
                self._soft_delete(blob_id, content_path, metadata_path)
                logger.debug("Blob soft-deleted: %s", blob_id)
            else:
                self._hard_delete(content_path, metadata_path)
                logger.debug("Blob hard-deleted: %s", blob_id)

            return True

        except OSError as exc:
            logger.error(
                "Failed to delete blob %s (soft=%s): %s",
                blob_id,
                soft,
                exc,
            )
            raise BlobStoreError(
                f"Failed to delete blob {blob_id}: {exc}"
            ) from exc

    def exists(self, blob_id: BlobId) -> bool:
        """Check whether a non-deleted blob exists.

        Args:
            blob_id: Identifier to check.

        Returns:
            ``True`` if the blob exists and is **not** soft-deleted.
        """
        content_path = self._blob_path(blob_id)
        return content_path.is_file()

    def undelete(self, blob_id: BlobId) -> bool:
        """Restore a soft-deleted blob.

        Moves the blob content and metadata back from ``.deleted/`` to
        their original locations and removes the ``deleted_at`` marker
        from the properties file.

        Args:
            blob_id: Identifier of the soft-deleted blob.

        Returns:
            ``True`` if the blob was found and restored, ``False``
            otherwise.
        """
        self._ensure_started()

        deleted_content = self._deleted_blob_path(blob_id)
        if not deleted_content.is_file():
            return False

        content_path = self._blob_path(blob_id)
        metadata_path = self._metadata_path(blob_id)
        deleted_metadata = self._deleted_metadata_path(blob_id)

        try:
            # Move content back to active directory
            ensure_directory(content_path.parent)
            shutil.move(str(deleted_content), str(content_path))

            # Move metadata back
            if deleted_metadata.is_file():
                ensure_directory(metadata_path.parent)
                shutil.move(str(deleted_metadata), str(metadata_path))
                # Remove the deleted_at marker
                self._remove_property_key(metadata_path, "deleted_at")

            # Tidy empty directories in .deleted/
            self._cleanup_empty_parents(
                deleted_content.parent, self._deleted_dir
            )

            logger.debug("Blob undeleted: %s", blob_id)
            return True

        except OSError as exc:
            logger.error("Failed to undelete blob %s: %s", blob_id, exc)
            return False

    # ------------------------------------------------------------------
    # Compaction and Cleanup
    # ------------------------------------------------------------------

    def compact(self) -> int:
        """Permanently remove soft-deleted blobs past the retention period.

        Scans the ``.deleted/`` directory and permanently removes any blobs
        whose ``deleted_at`` timestamp is older than the default retention
        period (:data:`DEFAULT_SOFT_DELETE_RETENTION_DAYS` — 30 days).

        Returns:
            The number of blobs permanently removed.
        """
        start_time = time.monotonic()
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=DEFAULT_SOFT_DELETE_RETENTION_DAYS
        )

        blobs_removed: int = 0
        space_reclaimed: int = 0

        deleted_blobs = list_files(
            str(self._deleted_dir), f"*{BLOB_EXTENSION}", recursive=True
        )

        for blob_path in deleted_blobs:
            props_path = blob_path.with_suffix(PROPERTIES_EXTENSION)
            deleted_at = self._get_deleted_at(blob_path, props_path)

            if deleted_at is not None and deleted_at < cutoff:
                try:
                    size = blob_path.stat().st_size
                    os.unlink(str(blob_path))
                    if props_path.is_file():
                        os.unlink(str(props_path))
                    blobs_removed += 1
                    space_reclaimed += size
                except OSError as exc:
                    logger.warning(
                        "Failed to compact blob %s: %s", blob_path, exc
                    )

        self._cleanup_empty_dirs(self._deleted_dir)

        elapsed = time.monotonic() - start_time
        logger.info(
            "Compact completed for '%s': removed=%d, reclaimed=%s, "
            "elapsed=%.2fs",
            self._name,
            blobs_removed,
            format_file_size(space_reclaimed),
            elapsed,
        )
        return blobs_removed

    def cleanup_temp(self, max_age_hours: int = 24) -> dict[str, int]:
        """Remove orphaned temporary files in the staging directory.

        Scans ``.staging/`` for files older than *max_age_hours* and
        permanently deletes them.

        Args:
            max_age_hours: Maximum age in hours before a staging file is
                considered orphaned (default 24).

        Returns:
            Dictionary with ``files_removed`` and
            ``space_reclaimed_bytes`` keys.
        """
        start_time = time.monotonic()
        cutoff_ts = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).timestamp()

        files_removed: int = 0
        space_reclaimed: int = 0

        staging_files = list_files(
            str(self._staging_dir), "*", recursive=True
        )

        for file_path in staging_files:
            try:
                stat_info = file_path.stat()
                if stat_info.st_mtime < cutoff_ts:
                    space_reclaimed += stat_info.st_size
                    os.unlink(str(file_path))
                    files_removed += 1
            except OSError as exc:
                logger.warning(
                    "Failed to clean up staging file %s: %s",
                    file_path,
                    exc,
                )

        elapsed = time.monotonic() - start_time
        logger.info(
            "Temp cleanup for '%s': removed=%d, reclaimed=%s, "
            "elapsed=%.2fs",
            self._name,
            files_removed,
            format_file_size(space_reclaimed),
            elapsed,
        )
        return {
            "files_removed": files_removed,
            "space_reclaimed_bytes": space_reclaimed,
        }

    # ------------------------------------------------------------------
    # Metrics and Statistics
    # ------------------------------------------------------------------

    def get_metrics(self) -> BlobStoreMetrics:
        """Collect current storage-usage statistics.

        Returns:
            :class:`BlobStoreMetrics` populated with current size, count,
            and disk-space information.
        """
        start_time = time.monotonic()

        # Content metrics
        total_size = get_directory_size(str(self._content_dir))
        blob_count = len(
            list_files(
                str(self._content_dir),
                f"*{BLOB_EXTENSION}",
                recursive=True,
            )
        )

        # Soft-deleted metrics
        deleted_blobs = list_files(
            str(self._deleted_dir), f"*{BLOB_EXTENSION}", recursive=True
        )
        soft_deleted_count = len(deleted_blobs)
        soft_deleted_size = get_directory_size(str(self._deleted_dir))

        # Disk-space metrics
        try:
            usage = shutil.disk_usage(str(self._root))
            available_space = usage.free
            total_space = usage.total
            usage_pct = (
                ((total_space - available_space) / total_space * 100.0)
                if total_space > 0
                else 0.0
            )
        except OSError:
            available_space = -1
            total_space = -1
            usage_pct = 0.0

        metrics = BlobStoreMetrics(
            blob_store_name=self._name,
            blob_store_type="file",
            total_size_bytes=total_size,
            blob_count=blob_count,
            soft_deleted_count=soft_deleted_count,
            soft_deleted_size_bytes=soft_deleted_size,
            available_space_bytes=available_space,
            total_space_bytes=total_space,
            usage_percentage=round(usage_pct, 2),
        )

        # Disk-space warnings
        if metrics.is_space_critical:
            logger.error(
                "CRITICAL: Disk space for BlobStore '%s' at %.1f%% usage "
                "(available: %s)",
                self._name,
                usage_pct,
                format_file_size(available_space),
            )
        elif metrics.is_space_warning:
            logger.warning(
                "Disk space for BlobStore '%s' at %.1f%% usage "
                "(available: %s)",
                self._name,
                usage_pct,
                format_file_size(available_space),
            )

        elapsed = time.monotonic() - start_time
        logger.debug(
            "Metrics collected for '%s' in %.2fs: blobs=%d, size=%s",
            self._name,
            elapsed,
            blob_count,
            format_file_size(total_size),
        )
        return metrics

    # ------------------------------------------------------------------
    # Blob Iteration
    # ------------------------------------------------------------------

    def list_blobs(self, include_deleted: bool = False) -> Iterator[Blob]:
        """Iterate over all blobs in the store.

        Args:
            include_deleted: If ``True`` soft-deleted blobs are included
                in the iteration.

        Yields:
            :class:`Blob` instances for each stored blob.
        """
        # --- Active blobs ---
        active_blobs = list_files(
            str(self._content_dir), f"*{BLOB_EXTENSION}", recursive=True
        )
        for blob_path in active_blobs:
            blob_id = self._blob_id_from_path(blob_path)
            if blob_id is not None:
                blob = self.get(blob_id)
                if blob is not None:
                    yield blob

        # --- Soft-deleted blobs (optional) ---
        if include_deleted:
            yield from self._iter_deleted_blobs()

    # ------------------------------------------------------------------
    # Private: Directory Initialisation
    # ------------------------------------------------------------------

    def _initialize_directories(self) -> None:
        """Create all required BlobStore sub-directories."""
        for directory in (
            self._content_dir,
            self._deleted_dir,
            self._staging_dir,
            self._metadata_dir,
        ):
            ensure_directory(directory)
            logger.debug("BlobStore directory ensured: %s", directory)

    # ------------------------------------------------------------------
    # Private: Content-Addressable Path Resolution
    # ------------------------------------------------------------------

    def _blob_path(self, blob_id: BlobId) -> Path:
        """Compute the content-file path for a blob.

        Uses ID-prefix-based directory nesting:
        ``content/<s1>/<s2>/<s3>/<full_id>.bytes``

        Example::

            BlobId("550e8400-e29b-41d4-a716-446655440000")
            → content/55/0e/84/550e8400-e29b-41d4-a716-446655440000.bytes
        """
        segments = self._path_segments(blob_id.value)
        filename = f"{blob_id.value}{BLOB_EXTENSION}"
        return safe_join(str(self._content_dir), *segments, filename)

    def _metadata_path(self, blob_id: BlobId) -> Path:
        """Compute the metadata-sidecar file path for a blob."""
        segments = self._path_segments(blob_id.value)
        filename = f"{blob_id.value}{PROPERTIES_EXTENSION}"
        return safe_join(str(self._metadata_dir), *segments, filename)

    def _deleted_blob_path(self, blob_id: BlobId) -> Path:
        """Compute the path for a soft-deleted blob's content."""
        segments = self._path_segments(blob_id.value)
        filename = f"{blob_id.value}{BLOB_EXTENSION}"
        return safe_join(str(self._deleted_dir), *segments, filename)

    def _deleted_metadata_path(self, blob_id: BlobId) -> Path:
        """Compute the path for a soft-deleted blob's metadata."""
        segments = self._path_segments(blob_id.value)
        filename = f"{blob_id.value}{PROPERTIES_EXTENSION}"
        return safe_join(str(self._deleted_dir), *segments, filename)

    @staticmethod
    def _path_segments(blob_value: str) -> list[str]:
        """Derive directory-nesting segments from a blob-ID value.

        Strips hyphens from the value and extracts the first
        ``PATH_DEPTH * PATH_SEGMENT_LEN`` characters, split into
        ``PATH_DEPTH`` segments of ``PATH_SEGMENT_LEN`` each.

        Example: ``"550e8400-..."`` → ``["55", "0e", "84"]``
        """
        hex_id = blob_value.replace("-", "")
        segments: list[str] = []
        for i in range(PATH_DEPTH):
            start = i * PATH_SEGMENT_LEN
            end = start + PATH_SEGMENT_LEN
            if end <= len(hex_id):
                segments.append(hex_id[start:end])
            else:
                segments.append(hex_id[start:].ljust(PATH_SEGMENT_LEN, "0"))
        return segments

    def _blob_id_from_path(self, blob_path: Path) -> BlobId | None:
        """Extract a ``BlobId`` from a blob file path."""
        try:
            stem = blob_path.stem  # filename without extension
            return BlobId(value=stem, store_name=self._name)
        except (ValueError, AttributeError):
            return None

    # ------------------------------------------------------------------
    # Private: Metadata (.properties) File Operations
    # ------------------------------------------------------------------

    def _write_properties(
        self, blob_id: BlobId, attributes: BlobAttributes
    ) -> None:
        """Write metadata to a ``.properties`` sidecar file (JSON).

        Uses :func:`atomic_write` for data-integrity guarantees.
        """
        metadata_path = self._metadata_path(blob_id)
        props_data: dict[str, Any] = {
            "@BlobStore.created-by": "nexus",
            "blob-name": blob_id.value,
            **attributes.to_dict(),
        }
        content = json.dumps(props_data, indent=2, default=str)
        atomic_write(str(metadata_path), content.encode("utf-8"))
        logger.debug("Properties written for blob: %s", blob_id)

    def _read_properties(self, blob_id: BlobId) -> BlobAttributes | None:
        """Read metadata from the ``.properties`` sidecar file."""
        metadata_path = self._metadata_path(blob_id)
        props_data = self._read_properties_raw(metadata_path)
        if props_data is None:
            return None
        return BlobAttributes.from_dict(props_data)

    def _read_properties_raw(self, props_path: Path) -> dict[str, Any] | None:
        """Read the raw JSON dictionary from a properties file."""
        if not props_path.is_file():
            return None
        try:
            with open(str(props_path), "r", encoding="utf-8") as fh:
                return json.loads(fh.read())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read properties from %s: %s", props_path, exc
            )
            return None

    def _write_properties_file(
        self, path: Path, data: dict[str, Any]
    ) -> None:
        """Write a properties file at the given *path*."""
        ensure_directory(path.parent)
        content = json.dumps(data, indent=2, default=str)
        atomic_write(str(path), content.encode("utf-8"))

    def _update_properties_file(
        self, path: Path, updates: dict[str, Any]
    ) -> None:
        """Merge *updates* into an existing properties file and rewrite it."""
        existing = self._read_properties_raw(path) or {}
        existing.update(updates)
        content = json.dumps(existing, indent=2, default=str)
        atomic_write(str(path), content.encode("utf-8"))

    def _remove_property_key(self, path: Path, key: str) -> None:
        """Remove a single *key* from a properties file."""
        data = self._read_properties_raw(path)
        if data and key in data:
            del data[key]
            content = json.dumps(data, indent=2, default=str)
            atomic_write(str(path), content.encode("utf-8"))

    # ------------------------------------------------------------------
    # Private: Delete Helpers
    # ------------------------------------------------------------------

    def _soft_delete(
        self,
        blob_id: BlobId,
        content_path: Path,
        metadata_path: Path,
    ) -> None:
        """Move a blob and its metadata to ``.deleted/``."""
        deleted_content = self._deleted_blob_path(blob_id)
        deleted_metadata = self._deleted_metadata_path(blob_id)

        ensure_directory(deleted_content.parent)
        shutil.move(str(content_path), str(deleted_content))

        if metadata_path.is_file():
            ensure_directory(deleted_metadata.parent)
            shutil.move(str(metadata_path), str(deleted_metadata))
            self._update_properties_file(
                deleted_metadata,
                {"deleted_at": datetime.now(timezone.utc).isoformat()},
            )
        else:
            # Create a minimal properties file with the deleted_at marker
            self._write_properties_file(
                deleted_metadata,
                {"deleted_at": datetime.now(timezone.utc).isoformat()},
            )

        # Tidy empty parent directories
        self._cleanup_empty_parents(content_path.parent, self._content_dir)
        self._cleanup_empty_parents(metadata_path.parent, self._metadata_dir)

    def _hard_delete(self, content_path: Path, metadata_path: Path) -> None:
        """Permanently remove a blob's content and metadata files."""
        os.unlink(str(content_path))
        if metadata_path.is_file():
            os.unlink(str(metadata_path))

        self._cleanup_empty_parents(content_path.parent, self._content_dir)
        self._cleanup_empty_parents(metadata_path.parent, self._metadata_dir)

    # ------------------------------------------------------------------
    # Private: Disk-Space Monitoring
    # ------------------------------------------------------------------

    def _check_disk_space(self) -> None:
        """Check available disk space and log warnings or raise on critical."""
        try:
            usage = shutil.disk_usage(str(self._root))
            usage_ratio = (
                (usage.total - usage.free) / usage.total
                if usage.total > 0
                else 0.0
            )

            if usage_ratio >= DISK_SPACE_CRITICAL_THRESHOLD:
                logger.error(
                    "CRITICAL: Disk space for BlobStore '%s' at %.1f%% "
                    "(available: %s)",
                    self._name,
                    usage_ratio * 100,
                    format_file_size(usage.free),
                )
                raise BlobStoreFullError(
                    f"BlobStore '{self._name}' has insufficient disk space: "
                    f"{usage_ratio * 100:.1f}% used"
                )
            elif usage_ratio >= DISK_SPACE_WARNING_THRESHOLD:
                logger.warning(
                    "Disk space for BlobStore '%s' at %.1f%% "
                    "(available: %s)",
                    self._name,
                    usage_ratio * 100,
                    format_file_size(usage.free),
                )
        except BlobStoreFullError:
            raise
        except OSError as exc:
            logger.warning(
                "Unable to check disk space for BlobStore '%s': %s",
                self._name,
                exc,
            )

    # ------------------------------------------------------------------
    # Private: Directory Cleanup Helpers
    # ------------------------------------------------------------------

    def _cleanup_empty_parents(self, directory: Path, stop_at: Path) -> None:
        """Remove empty parent directories up to (but not including)
        *stop_at*."""
        current = directory
        stop_resolved = stop_at.resolve()

        while True:
            try:
                if current.resolve() == stop_resolved:
                    break
                if current.is_dir() and not any(current.iterdir()):
                    os.rmdir(str(current))
                    current = current.parent
                else:
                    break
            except OSError:
                break

    def _cleanup_empty_dirs(self, root_dir: Path) -> None:
        """Remove all empty directories under *root_dir* (bottom-up)."""
        if not root_dir.is_dir():
            return

        for dirpath, _dirnames, _filenames in os.walk(
            str(root_dir), topdown=False
        ):
            dir_path = Path(dirpath)
            if dir_path == root_dir:
                continue
            try:
                if not any(dir_path.iterdir()):
                    os.rmdir(str(dir_path))
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Private: Compact Helper
    # ------------------------------------------------------------------

    def _get_deleted_at(
        self, blob_path: Path, props_path: Path
    ) -> datetime | None:
        """Extract the ``deleted_at`` timestamp for a soft-deleted blob."""
        if props_path.is_file():
            props_data = self._read_properties_raw(props_path)
            if props_data:
                deleted_at_str = props_data.get("deleted_at")
                if deleted_at_str:
                    try:
                        return datetime.fromisoformat(str(deleted_at_str))
                    except ValueError:
                        pass

        # Fallback: use the file-modification time
        try:
            stat_info = blob_path.stat()
            return datetime.fromtimestamp(stat_info.st_mtime, tz=timezone.utc)
        except OSError:
            return None

    # ------------------------------------------------------------------
    # Private: Deleted-Blob Iteration (for list_blobs)
    # ------------------------------------------------------------------

    def _iter_deleted_blobs(self) -> Iterator[Blob]:
        """Yield :class:`Blob` objects for every soft-deleted blob."""
        deleted_blobs = list_files(
            str(self._deleted_dir), f"*{BLOB_EXTENSION}", recursive=True
        )
        for blob_path in deleted_blobs:
            blob_id = self._blob_id_from_path(blob_path)
            if blob_id is None:
                continue

            props_path = blob_path.with_suffix(PROPERTIES_EXTENSION)
            props_data = self._read_properties_raw(props_path)

            deleted_at: datetime | None = None
            if props_data:
                attributes = BlobAttributes.from_dict(props_data)
                deleted_at_str = props_data.get("deleted_at")
                if deleted_at_str:
                    try:
                        deleted_at = datetime.fromisoformat(
                            str(deleted_at_str)
                        )
                    except ValueError:
                        pass
            else:
                # Minimal fallback attributes
                try:
                    stat_info = blob_path.stat()
                    attributes = BlobAttributes(
                        size=stat_info.st_size,
                        creation_time=datetime.fromtimestamp(
                            stat_info.st_ctime, tz=timezone.utc
                        ),
                    )
                except OSError:
                    attributes = BlobAttributes()

            yield Blob(
                blob_id=blob_id,
                attributes=attributes,
                soft_deleted=True,
                deleted_at=deleted_at,
            )


# =========================================================================
# Factory Function
# =========================================================================


def create_file_blobstore(
    name: str, config: dict[str, Any]
) -> FileBlobStore:
    """Create a :class:`FileBlobStore` from a configuration dictionary.

    Extracts the ``path`` key from *config* to determine the root
    directory.  If no path is specified, defaults to
    ``/nexus-data/blobs/<name>``.

    Args:
        name:   Unique name for the BlobStore.
        config: Configuration dictionary.  Expected keys:

                * ``path`` — Root directory path for the BlobStore.

    Returns:
        A configured :class:`FileBlobStore` instance.
    """
    default_path = f"/nexus-data/blobs/{name}"
    root_path = config.get("path", default_path)
    return FileBlobStore(name=name, root_path=root_path, config=config)
