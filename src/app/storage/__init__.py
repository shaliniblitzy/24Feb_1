"""
Pluggable BlobStore abstraction layer for the Nexus Repository application.

This package implements the Strategy pattern for interchangeable storage backends,
supporting both local filesystem (File BlobStore, Feature F-201) and Amazon S3
(S3 BlobStore, Feature F-202) storage.

The BlobStore abstraction replaces the Java BlobStore API from the source system,
providing a unified interface for binary artifact storage operations:

- **create**: Store a new blob with checksums and metadata
- **get**: Retrieve blob metadata
- **get_stream**: Stream blob content
- **delete**: Soft-delete or hard-delete a blob
- **exists**: Check blob existence
- **undelete**: Recover a soft-deleted blob
- **compact**: Remove permanently soft-deleted blobs beyond the retention period
- **get_metrics**: Collect storage usage statistics
- **list_blobs**: Enumerate blobs in the store
- **compute_checksums**: Compute SHA-1, SHA-256, and MD5 checksums for data

Architecture:
    The package exposes two concrete backends that implement the abstract
    :class:`BlobStore` interface:

    * :class:`FileBlobStore` — Local filesystem with atomic writes, soft-delete,
      content-addressable directory nesting, and metadata sidecar files.
    * :class:`S3BlobStore` — Amazon S3 (or S3-compatible) with SSE encryption,
      multipart uploads, object tagging for soft-delete, and batch operations.

    The :func:`create_blobstore` factory function selects the correct backend
    based on a ``type`` key in the configuration dictionary.

    The :class:`BlobStoreMaintenanceService` orchestrates scheduled maintenance
    across all registered BlobStores (compaction, temp cleanup, integrity
    verification, and statistics collection).

Usage::

    from src.app.storage import create_blobstore, BlobStore, BlobId

    # Create a file-based BlobStore
    store = create_blobstore('default', {'type': 'file', 'path': '/data/blobs'})

    # Create an S3 BlobStore
    store = create_blobstore('s3-prod', {
        'type': 's3',
        'bucket': 'my-bucket',
        'region': 'us-east-1',
    })

    # Store a blob
    blob = store.create(None, data_bytes, content_type='application/jar')

    # Retrieve a blob
    blob = store.get(blob_id)
    stream = store.get_stream(blob_id)

    # Create from a BlobStoreConfig database model
    from src.app.storage import create_blobstore_from_model
    store = create_blobstore_from_model(blobstore_config_instance)

Replaces:
    Java BlobStore API interface and Guice-wired factory from the OSGi/Karaf
    container in the original Sonatype Nexus Repository system.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-exports from blobstore.py — Abstract base class and data classes
# ---------------------------------------------------------------------------
# These form the public API of the storage package.  Consumers can import
# directly from ``src.app.storage`` instead of reaching into submodules.
from src.app.storage.blobstore import (
    BlobStore,
    BlobId,
    Blob,
    BlobAttributes,
    BlobStoreMetrics,
)

# ---------------------------------------------------------------------------
# Re-exports from concrete implementations
# ---------------------------------------------------------------------------
# File BlobStore — Feature F-201 (local filesystem with atomic writes)
from src.app.storage.file_blobstore import FileBlobStore, create_file_blobstore

# S3 BlobStore — Feature F-202 (Amazon S3 with SSE and multipart uploads)
from src.app.storage.s3_blobstore import S3BlobStore, create_s3_blobstore

# ---------------------------------------------------------------------------
# Re-export from maintenance module — Feature F-203
# ---------------------------------------------------------------------------
from src.app.storage.maintenance import BlobStoreMaintenanceService

# ---------------------------------------------------------------------------
# TYPE_CHECKING-only import to avoid circular dependency with models
# ---------------------------------------------------------------------------
if TYPE_CHECKING:
    from src.app.models.blobstore_config import BlobStoreConfig

# ---------------------------------------------------------------------------
# Supported BlobStore type identifiers
# ---------------------------------------------------------------------------
_SUPPORTED_TYPES: frozenset[str] = frozenset({'file', 's3'})


# ===========================================================================
# Factory Functions
# ===========================================================================


def create_blobstore(name: str, config: dict) -> BlobStore:
    """Create a BlobStore instance based on configuration type.

    This is the **primary entry point** (Strategy pattern factory) for
    creating BlobStore instances.  It inspects the ``type`` key in the
    *config* dictionary and delegates to the appropriate backend-specific
    factory function:

    * ``'file'`` → :func:`create_file_blobstore`
    * ``'s3'``   → :func:`create_s3_blobstore`

    If ``type`` is omitted, defaults to ``'file'`` (the standard standalone
    deployment backend).

    Args:
        name: Unique BlobStore name (e.g., ``'default'``, ``'s3-production'``).
            Used for logging, metrics collection, and registry lookup.
        config: Configuration dictionary with a ``type`` key and
            backend-specific settings.

            **File BlobStore** (``type='file'``)::

                {
                    'type': 'file',
                    'path': '/nexus-data/blobs/default',
                }

            **S3 BlobStore** (``type='s3'``)::

                {
                    'type': 's3',
                    'bucket': 'my-nexus-bucket',
                    'region': 'us-east-1',
                    'prefix': 'nexus/',
                    'encryption': {'type': 's3ManagedEncryption'},
                }

    Returns:
        A configured :class:`BlobStore` instance (either
        :class:`FileBlobStore` or :class:`S3BlobStore`).  The returned
        store is **not** started — callers must invoke ``start()`` before
        performing CRUD operations.

    Raises:
        ValueError: If the ``type`` value is not one of the supported
            backends (``'file'`` or ``'s3'``).

    Examples:
        >>> store = create_blobstore('default', {'type': 'file', 'path': '/tmp/blobs'})
        >>> isinstance(store, FileBlobStore)
        True

        >>> store = create_blobstore('s3-prod', {'type': 's3', 'bucket': 'b', 'region': 'us-east-1'})
        >>> isinstance(store, S3BlobStore)
        True
    """
    store_type: str = config.get('type', 'file')

    if store_type == 'file':
        logger.debug(
            "Creating FileBlobStore '%s' with config keys: %s",
            name,
            list(config.keys()),
        )
        return create_file_blobstore(name, config)

    if store_type == 's3':
        logger.debug(
            "Creating S3BlobStore '%s' with config keys: %s",
            name,
            list(config.keys()),
        )
        return create_s3_blobstore(name, config)

    # Unsupported type — provide a clear, actionable error message
    raise ValueError(
        f"Unsupported BlobStore type: '{store_type}'. "
        f"Supported types are: {', '.join(sorted(_SUPPORTED_TYPES))}"
    )


def create_blobstore_from_model(blobstore_config: BlobStoreConfig) -> BlobStore:
    """Create a BlobStore from a :class:`BlobStoreConfig` database model.

    This is a higher-level factory intended for use by the
    :class:`~src.app.services.blobstore_service.BlobStoreService` when
    initialising BlobStore instances from database records.  It extracts
    the ``blob_store_name``, ``type``, and ``configuration`` fields from
    the model and delegates to :func:`create_blobstore`.

    Args:
        blobstore_config: A :class:`~src.app.models.blobstore_config.BlobStoreConfig`
            SQLAlchemy model instance.  Must have non-``None`` ``blob_store_name``
            and ``type`` attributes.

    Returns:
        A configured :class:`BlobStore` instance matching the model's
        ``type`` field.

    Raises:
        ValueError: If ``blobstore_config.type`` is not a supported backend
            type, or if required model attributes are missing.
        AttributeError: If *blobstore_config* does not have the expected
            ``blob_store_name``, ``type``, or ``configuration`` attributes.

    Examples:
        >>> from src.app.models.blobstore_config import BlobStoreConfig
        >>> model = BlobStoreConfig(
        ...     blob_store_name='default',
        ...     type='file',
        ...     configuration={'path': '/data/blobs'},
        ... )
        >>> store = create_blobstore_from_model(model)
        >>> store.name
        'default'
    """
    # Extract the configuration payload; default to empty dict if None/NULL
    config: dict = dict(blobstore_config.configuration or {})

    # Inject the backend type into the config dict for create_blobstore()
    config['type'] = blobstore_config.type

    store_name: str = blobstore_config.blob_store_name

    logger.info(
        "Creating BlobStore '%s' (type=%s) from database model.",
        store_name,
        blobstore_config.type,
    )

    return create_blobstore(store_name, config)


# ===========================================================================
# Public API — __all__ defines the package's public surface
# ===========================================================================

__all__: list[str] = [
    # Abstract base class and core data classes (from blobstore.py)
    'BlobStore',
    'BlobId',
    'Blob',
    'BlobAttributes',
    'BlobStoreMetrics',
    # Concrete implementations
    'FileBlobStore',
    'S3BlobStore',
    # Maintenance service (Feature F-203)
    'BlobStoreMaintenanceService',
    # Factory functions
    'create_blobstore',
    'create_file_blobstore',
    'create_s3_blobstore',
    'create_blobstore_from_model',
]
