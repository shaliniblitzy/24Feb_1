"""
Abstract BlobStore base class defining the pluggable storage backend interface.

This module implements the Strategy pattern for interchangeable binary artifact storage.
The BlobStore abstraction supports two concrete backends:
- FileBlobStore (Feature F-201): Local filesystem with atomic writes and soft-delete
- S3BlobStore (Feature F-202): Amazon S3 with SSE encryption and multipart uploads

Replaces the Java BlobStore API interface from Sonatype Nexus Repository.
"""
from __future__ import annotations

import hashlib
import uuid
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from io import BytesIO
from typing import BinaryIO, Optional, Dict, Any, Iterator

# Module-level logger for storage operations
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-Level Constants
# ---------------------------------------------------------------------------

# Default soft-delete retention period (days) before permanent removal by compact()
DEFAULT_SOFT_DELETE_RETENTION_DAYS: int = 30

# Checksum algorithms used for blob integrity verification
CHECKSUM_ALGORITHMS: tuple = ('sha1', 'sha256', 'md5')

# Disk space warning thresholds (percentage) — per AAP Section 0.7.1
SPACE_WARNING_THRESHOLD: float = 90.0
SPACE_CRITICAL_THRESHOLD: float = 95.0

# ---------------------------------------------------------------------------
# Custom Exception Classes
# ---------------------------------------------------------------------------


class BlobStoreError(Exception):
    """Base exception for all BlobStore-related errors.

    All storage backend errors inherit from this class, allowing consumers
    to catch any BlobStore error with a single except clause.
    """
    pass


class BlobNotFoundError(BlobStoreError):
    """Raised when a requested blob does not exist in the BlobStore.

    Attributes:
        blob_id: The BlobId that was not found.
    """

    def __init__(self, blob_id: BlobId) -> None:
        self.blob_id = blob_id
        super().__init__(f"Blob not found: {blob_id}")


class BlobAlreadyExistsError(BlobStoreError):
    """Raised when attempting to create a blob with an ID that already exists.

    Attributes:
        blob_id: The BlobId that already exists.
    """

    def __init__(self, blob_id: BlobId) -> None:
        self.blob_id = blob_id
        super().__init__(f"Blob already exists: {blob_id}")


class BlobStoreNotStartedError(BlobStoreError):
    """Raised when operations are attempted on a BlobStore that has not been started.

    Consumers must call ``BlobStore.start()`` before performing any CRUD operations.
    """
    pass


class BlobStoreFullError(BlobStoreError):
    """Raised when the BlobStore has no available space to accept new blobs.

    This may be triggered when filesystem usage exceeds the critical threshold
    or when an S3 bucket quota is reached.
    """
    pass


# ---------------------------------------------------------------------------
# BlobStoreType Enum
# ---------------------------------------------------------------------------


class BlobStoreType(str, Enum):
    """Supported BlobStore backend types.

    Uses ``str`` mixin so that the enum serialises directly to its string
    value when converted to JSON (e.g., ``json.dumps(BlobStoreType.FILE)``
    produces ``"file"``).

    Members:
        FILE: Local filesystem storage backend (Feature F-201).
        S3:   Amazon S3 (or S3-compatible) storage backend (Feature F-202).
    """

    FILE = 'file'
    S3 = 's3'


# ---------------------------------------------------------------------------
# BlobId Data Class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlobId:
    """Unique, immutable identifier for a blob stored in a BlobStore.

    ``BlobId`` instances are frozen dataclasses — they are hashable and can
    be used as dictionary keys or set members.  The ``value`` field is
    typically a UUID-4 string, while ``store_name`` identifies which
    BlobStore instance owns the blob.

    Attributes:
        value:      UUID string identifying the blob.
        store_name: Name of the BlobStore that owns this blob.
    """

    value: str
    store_name: str

    # -- Factory methods ----------------------------------------------------

    @staticmethod
    def generate(store_name: str) -> BlobId:
        """Generate a new unique ``BlobId`` for the given store.

        Args:
            store_name: Name of the owning BlobStore.

        Returns:
            A freshly generated ``BlobId`` with a UUID-4 value.
        """
        return BlobId(value=str(uuid.uuid4()), store_name=store_name)

    @staticmethod
    def from_string(blob_id_str: str) -> BlobId:
        """Parse a ``"store_name:value"`` string back into a ``BlobId``.

        The expected format is ``<store_name>:<uuid_value>``.  The first
        colon is used as the separator so that store names themselves may
        not contain colons.

        Args:
            blob_id_str: String representation in ``"store_name:value"``
                format.

        Returns:
            The reconstructed ``BlobId``.

        Raises:
            ValueError: If *blob_id_str* does not contain exactly one colon
                separator or either component is empty.
        """
        if ':' not in blob_id_str:
            raise ValueError(
                f"Invalid BlobId string format: '{blob_id_str}'. "
                f"Expected 'store_name:value'."
            )
        store_name, _, value = blob_id_str.partition(':')
        if not store_name or not value:
            raise ValueError(
                f"Invalid BlobId string format: '{blob_id_str}'. "
                f"Both store_name and value must be non-empty."
            )
        return BlobId(value=value, store_name=store_name)

    # -- String representation ----------------------------------------------

    def __str__(self) -> str:
        """Return the canonical ``store_name:value`` string representation."""
        return f"{self.store_name}:{self.value}"


# ---------------------------------------------------------------------------
# BlobAttributes Data Class
# ---------------------------------------------------------------------------


@dataclass
class BlobAttributes:
    """Metadata attributes associated with a stored blob.

    These correspond to the ``.properties`` sidecar metadata files in the
    File BlobStore and to S3 object metadata in the S3 BlobStore.

    Attributes:
        content_type:  MIME type of the blob content.
        sha1:          SHA-1 checksum hex digest.
        sha256:        SHA-256 checksum hex digest.
        md5:           MD5 checksum hex digest.
        size:          Content size in bytes.
        creation_time: UTC timestamp when the blob was first stored.
        headers:       Additional custom headers / metadata key-value pairs.
    """

    content_type: str = 'application/octet-stream'
    sha1: str = ''
    sha256: str = ''
    md5: str = ''
    size: int = 0
    creation_time: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    headers: Dict[str, str] = field(default_factory=dict)

    # -- Serialisation helpers ----------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize all fields to a flat dictionary suitable for persistence.

        The ``creation_time`` field is formatted as an ISO-8601 string.

        Returns:
            Dictionary with all attribute fields.
        """
        return {
            'content_type': self.content_type,
            'sha1': self.sha1,
            'sha256': self.sha256,
            'md5': self.md5,
            'size': self.size,
            'creation_time': self.creation_time.isoformat(),
            'headers': dict(self.headers),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BlobAttributes:
        """Deserialize a ``BlobAttributes`` instance from a dictionary.

        Handles both ISO-8601 string and ``datetime`` objects for the
        ``creation_time`` field.

        Args:
            data: Dictionary previously produced by :meth:`to_dict` or an
                equivalent mapping.

        Returns:
            Reconstructed ``BlobAttributes`` instance.
        """
        creation_time_raw = data.get('creation_time')
        if isinstance(creation_time_raw, str):
            creation_time = datetime.fromisoformat(creation_time_raw)
        elif isinstance(creation_time_raw, datetime):
            creation_time = creation_time_raw
        else:
            creation_time = datetime.now(timezone.utc)

        return cls(
            content_type=data.get('content_type', 'application/octet-stream'),
            sha1=data.get('sha1', ''),
            sha256=data.get('sha256', ''),
            md5=data.get('md5', ''),
            size=int(data.get('size', 0)),
            creation_time=creation_time,
            headers=dict(data.get('headers', {})),
        )


# ---------------------------------------------------------------------------
# Blob Data Class
# ---------------------------------------------------------------------------


@dataclass
class Blob:
    """Represents a stored binary artifact (blob) with its associated metadata.

    A ``Blob`` encapsulates the blob identifier, metadata attributes, and
    the logical reference to the stored content.  Use
    ``BlobStore.get_stream()`` to retrieve the actual binary content.

    Attributes:
        blob_id:      Unique identifier for this blob.
        attributes:   Metadata attributes (checksums, size, content type …).
        soft_deleted: ``True`` if the blob has been soft-deleted.
        deleted_at:   UTC timestamp of soft-deletion, or ``None``.
    """

    blob_id: BlobId
    attributes: BlobAttributes
    soft_deleted: bool = False
    deleted_at: Optional[datetime] = None

    # -- Convenience properties ---------------------------------------------

    @property
    def path(self) -> str:
        """Return the blob_id value for path-resolution convenience."""
        return self.blob_id.value

    @property
    def is_deleted(self) -> bool:
        """Return ``True`` if the blob has been soft-deleted."""
        return self.soft_deleted

    @property
    def size(self) -> int:
        """Return the content size in bytes."""
        return self.attributes.size

    @property
    def content_type(self) -> str:
        """Return the MIME content type."""
        return self.attributes.content_type

    @property
    def sha1(self) -> str:
        """Return the SHA-1 checksum hex digest."""
        return self.attributes.sha1


# ---------------------------------------------------------------------------
# BlobStoreMetrics Data Class
# ---------------------------------------------------------------------------


@dataclass
class BlobStoreMetrics:
    """Storage usage statistics for a BlobStore instance.

    Collected by ``get_metrics()`` and used for health monitoring (F-401),
    disk space warnings, and the administration dashboard.

    Attributes:
        blob_store_name:         Name of the BlobStore.
        blob_store_type:         Backend type (``'file'`` or ``'s3'``).
        total_size_bytes:        Total size of all stored blobs.
        blob_count:              Number of active (non-deleted) blobs.
        soft_deleted_count:      Number of soft-deleted blobs.
        soft_deleted_size_bytes: Total size of soft-deleted blobs.
        available_space_bytes:   Available space (``-1`` if unknown, e.g. S3).
        total_space_bytes:       Total space (``-1`` if unknown).
        usage_percentage:        Percentage of used space (0–100).
    """

    blob_store_name: str
    blob_store_type: str
    total_size_bytes: int = 0
    blob_count: int = 0
    soft_deleted_count: int = 0
    soft_deleted_size_bytes: int = 0
    available_space_bytes: int = -1
    total_space_bytes: int = -1
    usage_percentage: float = 0.0

    # -- Threshold properties -----------------------------------------------

    @property
    def is_space_critical(self) -> bool:
        """Return ``True`` if usage exceeds the critical threshold (95 %)."""
        return self.usage_percentage > SPACE_CRITICAL_THRESHOLD

    @property
    def is_space_warning(self) -> bool:
        """Return ``True`` if usage exceeds the warning threshold (90 %)."""
        return self.usage_percentage > SPACE_WARNING_THRESHOLD

    # -- Serialisation -------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize metrics to a dictionary for API responses and logging.

        Returns:
            Dictionary containing all metrics fields plus the computed
            ``is_space_critical`` and ``is_space_warning`` flags.
        """
        return {
            'blob_store_name': self.blob_store_name,
            'blob_store_type': self.blob_store_type,
            'total_size_bytes': self.total_size_bytes,
            'blob_count': self.blob_count,
            'soft_deleted_count': self.soft_deleted_count,
            'soft_deleted_size_bytes': self.soft_deleted_size_bytes,
            'available_space_bytes': self.available_space_bytes,
            'total_space_bytes': self.total_space_bytes,
            'usage_percentage': self.usage_percentage,
            'is_space_critical': self.is_space_critical,
            'is_space_warning': self.is_space_warning,
        }


# ---------------------------------------------------------------------------
# BlobStoreConfiguration Data Class
# ---------------------------------------------------------------------------


@dataclass
class BlobStoreConfiguration:
    """Configuration for initialising a BlobStore backend.

    Wraps the raw configuration dictionary from the ``BlobStoreConfig``
    database model with type-safe accessors for common settings shared
    across all backend types.

    Attributes:
        name:       Unique BlobStore name.
        store_type: Backend type (``BlobStoreType.FILE`` or ``BlobStoreType.S3``).
        config:     Raw configuration dictionary with backend-specific keys.
    """

    name: str
    store_type: BlobStoreType
    config: Dict[str, Any] = field(default_factory=dict)

    # -- Helper properties for common config keys ---------------------------

    @property
    def path(self) -> str:
        """Return the filesystem path (for File BlobStore)."""
        return str(self.config.get('path', ''))

    @property
    def bucket(self) -> str:
        """Return the S3 bucket name (for S3 BlobStore)."""
        return str(self.config.get('bucket', ''))

    @property
    def region(self) -> str:
        """Return the AWS region (for S3 BlobStore), defaulting to us-east-1."""
        return str(self.config.get('region', 'us-east-1'))

    @property
    def encryption_type(self) -> str:
        """Return the encryption type (``'none'``, ``'s3ManagedEncryption'``, etc.)."""
        return str(self.config.get('encryption_type', 'none'))


# ---------------------------------------------------------------------------
# Module-Level Utility Function
# ---------------------------------------------------------------------------


def compute_checksums(data: bytes | BinaryIO) -> Dict[str, str]:
    """Compute SHA-1, SHA-256, and MD5 checksums for the given data.

    This is a **module-level** convenience function that can be called
    without a ``BlobStore`` instance.

    Args:
        data: Binary data as ``bytes`` / ``bytearray`` or a readable binary
            stream (``BinaryIO`` / ``BytesIO``).  If a stream is provided
            it will be read in full and then rewound to its original
            position.

    Returns:
        Dictionary with keys ``'sha1'``, ``'sha256'``, ``'md5'`` mapped to
        their respective hex-digest strings.
    """
    if isinstance(data, (bytes, bytearray)):
        raw = data
    else:
        raw = data.read()
        data.seek(0)  # Reset stream position for subsequent use

    return {
        'sha1': hashlib.sha1(raw).hexdigest(),
        'sha256': hashlib.sha256(raw).hexdigest(),
        'md5': hashlib.md5(raw).hexdigest(),
    }


# ---------------------------------------------------------------------------
# Abstract BlobStore Base Class
# ---------------------------------------------------------------------------


class BlobStore(ABC):
    """Abstract base class for all BlobStore implementations.

    Implements the **Strategy pattern** (AAP Section 0.4.3) for pluggable
    storage backends.  Concrete implementations (``FileBlobStore``,
    ``S3BlobStore``) must override every ``@abstractmethod``.

    Functional equivalence with the source Java BlobStore interface:

    ============== =====================================================
    Python method  Java equivalent
    ============== =====================================================
    ``create()``   ``BlobStore.create(InputStream, Map headers, BlobId)``
    ``get()``      ``BlobStore.get(BlobId)``
    ``get_stream`` ``BlobStore.get(BlobId)`` + ``Blob.getInputStream()``
    ``delete()``   ``BlobStore.delete(BlobId)`` / ``deleteHard(BlobId)``
    ``exists()``   ``BlobStore.exists(BlobId)``
    ``compact()``  ``BlobStore.compact()``
    ``get_metrics`` ``BlobStore.getMetrics()``
    ============== =====================================================
    """

    def __init__(self, name: str, config: BlobStoreConfiguration) -> None:
        """Initialise the BlobStore.

        Args:
            name:   Unique name identifying this BlobStore instance.
            config: Backend-specific configuration object.
        """
        self._name: str = name
        self._config: BlobStoreConfiguration = config
        self._started: bool = False
        self._logger = logging.getLogger(
            f'{__name__}.{self.__class__.__name__}'
        )

    # -- Read-Only Properties -----------------------------------------------

    @property
    def name(self) -> str:
        """Return the unique name of this BlobStore instance."""
        return self._name

    @property
    def store_type(self) -> BlobStoreType:
        """Return the backend type (``FILE`` or ``S3``)."""
        return self._config.store_type

    @property
    def is_started(self) -> bool:
        """Return ``True`` if the store has been started and is operational."""
        return self._started

    @property
    def config(self) -> BlobStoreConfiguration:
        """Return the configuration object for this BlobStore."""
        return self._config

    # -- Abstract Methods (Core Interface Contract) -------------------------

    @abstractmethod
    def start(self) -> None:
        """Initialise the storage backend.

        Concrete implementations must create directories, validate S3
        buckets, establish connections, etc.  After successful
        initialisation the implementation **must** set
        ``self._started = True``.

        Called during application startup.

        Raises:
            BlobStoreError: If initialisation fails.
        """
        ...

    @abstractmethod
    def stop(self) -> None:
        """Gracefully shut down the storage backend.

        Flush caches, close connections, and release resources.  After
        shutdown the implementation **must** set
        ``self._started = False``.

        Called during application shutdown.
        """
        ...

    @abstractmethod
    def create(
        self,
        blob_id: Optional[BlobId],
        data: bytes | BinaryIO,
        content_type: str = 'application/octet-stream',
        headers: Optional[Dict[str, str]] = None,
    ) -> Blob:
        """Store binary data as a new blob.

        If *blob_id* is ``None`` a new identifier is auto-generated via
        ``BlobId.generate(self._name)``.

        Implementations **must**:
        * compute SHA-1, SHA-256 and MD5 checksums during write,
        * write atomically (temp staging + rename-on-commit),
        * create metadata / attributes for the blob.

        Args:
            blob_id:      Optional pre-determined blob identifier.
            data:         Binary payload as ``bytes`` or a readable stream.
            content_type: MIME type for the stored content.
            headers:      Additional metadata key-value pairs.

        Returns:
            The fully populated :class:`Blob` object.

        Raises:
            BlobStoreError:     On storage failure.
            BlobStoreFullError: If the store has insufficient space.
            BlobAlreadyExistsError: If *blob_id* already exists.
        """
        ...

    @abstractmethod
    def get(self, blob_id: BlobId) -> Optional[Blob]:
        """Retrieve blob metadata by identifier.

        Returns ``None`` if the blob does not exist.  Soft-deleted blobs
        are **not** returned by default.

        Args:
            blob_id: Identifier of the blob to retrieve.

        Returns:
            :class:`Blob` with populated metadata, or ``None``.
        """
        ...

    @abstractmethod
    def get_stream(self, blob_id: BlobId) -> Optional[BinaryIO]:
        """Retrieve blob content as a binary stream.

        Returns ``None`` if the blob does not exist.  **The caller is
        responsible for closing the returned stream.**

        Args:
            blob_id: Identifier of the blob whose content to stream.

        Returns:
            A readable binary stream, or ``None``.
        """
        ...

    @abstractmethod
    def delete(self, blob_id: BlobId, soft: bool = True) -> bool:
        """Delete a blob from the store.

        * **Soft-delete** (``soft=True``, default): marks the blob as
          deleted without removing its data.  Soft-deleted blobs can be
          restored with :meth:`undelete` and are permanently removed
          during :meth:`compact`.
        * **Hard-delete** (``soft=False``): permanently removes the blob
          data and metadata immediately.

        Args:
            blob_id: Identifier of the blob to delete.
            soft:    If ``True`` (default) perform a soft-delete.

        Returns:
            ``True`` if the blob was found and deleted, ``False`` if the
            blob was not found.
        """
        ...

    @abstractmethod
    def exists(self, blob_id: BlobId) -> bool:
        """Check whether a non-deleted blob exists.

        Args:
            blob_id: Identifier to check.

        Returns:
            ``True`` if the blob exists and is **not** soft-deleted.
        """
        ...

    @abstractmethod
    def undelete(self, blob_id: BlobId) -> bool:
        """Restore a soft-deleted blob.

        Args:
            blob_id: Identifier of the soft-deleted blob.

        Returns:
            ``True`` if the blob was found and successfully restored,
            ``False`` if it was not found or was not soft-deleted.
        """
        ...

    @abstractmethod
    def compact(self) -> int:
        """Permanently remove soft-deleted blobs past the retention period.

        Blobs that have been soft-deleted for longer than
        :data:`DEFAULT_SOFT_DELETE_RETENTION_DAYS` (30 days) are
        permanently removed.  This is typically called by a scheduled
        maintenance task (``maintenance.py``).

        Returns:
            The number of blobs permanently removed.
        """
        ...

    @abstractmethod
    def get_metrics(self) -> BlobStoreMetrics:
        """Collect and return storage usage statistics.

        Used for health monitoring (Feature F-401) and the
        administration dashboard.

        Returns:
            :class:`BlobStoreMetrics` populated with current statistics.
        """
        ...

    @abstractmethod
    def list_blobs(self, include_deleted: bool = False) -> Iterator[Blob]:
        """Iterate over all blobs in the store.

        Used by maintenance tasks for integrity verification and
        reporting.

        Args:
            include_deleted: If ``True`` soft-deleted blobs are included
                in the iteration.

        Yields:
            :class:`Blob` instances for each stored blob.
        """
        ...

    # -- Concrete Utility Methods -------------------------------------------

    def compute_checksums(self, data: bytes | BinaryIO) -> Dict[str, str]:
        """Compute SHA-1, SHA-256, and MD5 checksums for the given data.

        Delegates to the module-level :func:`compute_checksums` function.

        Args:
            data: Binary data as ``bytes`` or a readable stream.  If a
                stream is provided it will be read and its position reset.

        Returns:
            Dictionary with keys ``'sha1'``, ``'sha256'``, ``'md5'`` and
            hex-digest values.
        """
        # Delegate to the module-level utility so that both call sites
        # share identical logic.
        return compute_checksums(data)

    def _ensure_started(self) -> None:
        """Raise :exc:`BlobStoreNotStartedError` if the store is not running.

        Concrete implementations should call this guard at the top of
        every public CRUD method.

        Raises:
            BlobStoreNotStartedError: If :attr:`is_started` is ``False``.
        """
        if not self._started:
            raise BlobStoreNotStartedError(
                f"BlobStore '{self._name}' has not been started. "
                f"Call start() first."
            )

    # -- Dunder methods -----------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return (
            f"<{self.__class__.__name__} name='{self._name}' "
            f"type='{self._config.store_type.value}' started={self._started}>"
        )
