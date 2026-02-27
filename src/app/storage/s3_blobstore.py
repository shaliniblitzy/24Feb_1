"""
AWS S3 BlobStore implementation (Feature F-202).

This module provides the Amazon S3 storage backend for binary artifacts,
implementing the Strategy pattern as a concrete subclass of the abstract
:class:`~src.app.storage.blobstore.BlobStore` base class.

Replaces ``S3BlobStore.java`` from the Java source system, which used the
AWS SDK for Java.  This implementation uses **boto3 1.36.7** (AWS SDK for
Python) for all S3 operations.

Key capabilities:

* **Server-side encryption (SSE)** — Supports SSE-S3 (AES-256),
  SSE-KMS (AWS Key Management Service), and SSE-C (customer-provided keys).
* **Multipart uploads** — Automatically switches to multipart upload for
  blobs larger than 5 MB, with configurable chunk size (default 8 MB).
* **Soft-delete via S3 object tagging** — Marks objects with
  ``nexus-deleted=true`` and ``nexus-deleted-at=<ISO timestamp>`` tags
  instead of immediate deletion, enabling recovery and deferred permanent
  removal during compaction.
* **S3-compatible storage** — Supports custom endpoint URLs for providers
  such as MinIO, Ceph, and DigitalOcean Spaces, with optional path-style
  bucket addressing.
* **Batch operations** — Uses ``delete_objects()`` for efficient bulk
  deletion during compaction (up to 1000 objects per request).

Usage::

    from src.app.storage.s3_blobstore import S3BlobStore, create_s3_blobstore

    store = create_s3_blobstore('s3-prod', {
        'bucket': 'my-nexus-bucket',
        'region': 'us-east-1',
        'encryption': {'type': 's3ManagedEncryption'},
    })
    store.start()
    blob = store.create(None, data_bytes, content_type='application/jar')
"""

import io
import json
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timezone, timedelta
from typing import BinaryIO, Optional, Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, BotoCoreError

from src.app.storage.blobstore import (
    BlobStore,
    BlobId,
    Blob,
    BlobAttributes,
    BlobStoreMetrics,
    BlobStoreConfiguration,
    BlobStoreType,
    BlobStoreError,
)
from src.app.utils.helpers import generate_uuid, compute_checksums, format_file_size

# ---------------------------------------------------------------------------
# Module-level logger (replaces SLF4J 1.7.36)
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Multipart upload settings
# ---------------------------------------------------------------------------
MULTIPART_THRESHOLD: int = 5 * 1024 * 1024     # 5 MB — switch to multipart above this
MULTIPART_CHUNK_SIZE: int = 8 * 1024 * 1024    # 8 MB per part
MAX_MULTIPART_PARTS: int = 10000                # S3 hard limit

# ---------------------------------------------------------------------------
# Encryption type identifiers (matches Nexus Java config keys)
# ---------------------------------------------------------------------------
ENCRYPTION_NONE: str = 'none'
ENCRYPTION_S3_MANAGED: str = 's3ManagedEncryption'     # SSE-S3 (AES-256)
ENCRYPTION_KMS: str = 'kmsManagedEncryption'           # SSE-KMS
ENCRYPTION_CUSTOMER: str = 'customerManagedEncryption'  # SSE-C

# ---------------------------------------------------------------------------
# S3 object metadata keys (stored in S3 Metadata dict)
# ---------------------------------------------------------------------------
META_SHA1: str = 'sha1'
META_SHA256: str = 'sha256'
META_MD5: str = 'md5'
META_CONTENT_TYPE: str = 'content-type'
META_CREATED: str = 'created'
META_BLOB_NAME: str = 'blob-name'
META_SIZE: str = 'size'

# ---------------------------------------------------------------------------
# Soft-delete S3 object tagging keys
# ---------------------------------------------------------------------------
DELETED_TAG_KEY: str = 'nexus-deleted'
DELETED_TAG_VALUE: str = 'true'
DELETED_AT_TAG_KEY: str = 'nexus-deleted-at'

# ---------------------------------------------------------------------------
# S3 key prefixes — content and metadata stored separately
# ---------------------------------------------------------------------------
CONTENT_PREFIX: str = 'content/'
METADATA_PREFIX: str = 'metadata/'

# ---------------------------------------------------------------------------
# Connection / client settings
# ---------------------------------------------------------------------------
DEFAULT_MAX_CONNECTIONS: int = 50
DEFAULT_CONNECT_TIMEOUT: int = 10   # seconds
DEFAULT_READ_TIMEOUT: int = 30      # seconds

# Batch delete limit per S3 API call
BATCH_DELETE_LIMIT: int = 1000


# ---------------------------------------------------------------------------
# S3 StreamingBody adapter for BinaryIO compatibility
# ---------------------------------------------------------------------------


class _S3StreamAdapter:
    """Thin wrapper that adapts a boto3 ``StreamingBody`` to a file-like
    interface suitable for consumers expecting a :class:`~typing.BinaryIO`.

    Only the ``read()`` and ``close()`` operations are supported because S3
    ``StreamingBody`` objects are **not** seekable.
    """

    def __init__(self, streaming_body: Any, content_length: int = 0) -> None:
        self._body = streaming_body
        self._content_length = content_length
        self._position = 0
        self._closed = False

    # -- BinaryIO-compatible interface --------------------------------------

    def read(self, size: int = -1) -> bytes:
        """Read up to *size* bytes from the S3 stream."""
        if self._closed:
            raise ValueError("I/O operation on closed stream")
        if size == -1 or size is None:
            data = self._body.read()
        else:
            data = self._body.read(size)
        self._position += len(data)
        return data

    def readable(self) -> bool:
        """Return ``True`` — this stream is always readable."""
        return True

    def writable(self) -> bool:
        """Return ``False`` — S3 streams are read-only."""
        return False

    def seekable(self) -> bool:
        """Return ``False`` — S3 streams are not seekable."""
        return False

    def tell(self) -> int:
        """Return the current byte offset from the start of the stream."""
        return self._position

    def close(self) -> None:
        """Close the underlying ``StreamingBody``."""
        if not self._closed:
            self._body.close()
            self._closed = True

    @property
    def closed(self) -> bool:
        """Return ``True`` if the stream has been closed."""
        return self._closed

    def __enter__(self) -> '_S3StreamAdapter':
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __iter__(self) -> '_S3StreamAdapter':
        return self

    def __next__(self) -> bytes:
        chunk = self.read(8192)
        if not chunk:
            raise StopIteration
        return chunk


# ---------------------------------------------------------------------------
# S3BlobStore — Concrete BlobStore implementation for Amazon S3
# ---------------------------------------------------------------------------


class S3BlobStore(BlobStore):
    """Amazon S3 storage backend for binary artifacts (Feature F-202).

    Implements the :class:`~src.app.storage.blobstore.BlobStore` Strategy
    interface using **boto3** for all S3 operations.  Supports:

    * SSE-S3, SSE-KMS, and SSE-C server-side encryption
    * Automatic multipart uploads for blobs > 5 MB
    * Soft-delete via S3 object tagging (``nexus-deleted`` / ``nexus-deleted-at``)
    * Custom S3-compatible endpoints (MinIO, Ceph, DigitalOcean Spaces)
    * Path-style bucket addressing for non-AWS providers
    * Batch delete during compaction (up to 1000 objects per request)

    Configuration dict keys::

        {
            "bucket":           "my-nexus-bucket",     # REQUIRED
            "region":           "us-east-1",           # default: us-east-1
            "prefix":           "nexus/",              # key prefix in bucket
            "endpoint":         "https://minio:9000",  # custom S3-compat endpoint
            "accessKeyId":      "AKIA...",             # explicit credentials
            "secretAccessKey":  "secret...",           # explicit credentials
            "forcePathStyle":   true,                  # path-style addressing
            "encryption": {
                "type": "s3ManagedEncryption",         # SSE-S3 | SSE-KMS | SSE-C
                "key":  "arn:aws:kms:...:key/...",     # KMS key ARN (for SSE-KMS)
            }
        }
    """

    def __init__(self, name: str, config: dict) -> None:
        """Initialise the S3BlobStore.

        Args:
            name:   Unique name identifying this BlobStore instance.
            config: Configuration dictionary with S3-specific settings.
                    The ``bucket`` key is **required**; all other keys are
                    optional and fall back to sensible defaults.

        Raises:
            KeyError: If the mandatory ``bucket`` key is missing from *config*.
            BlobStoreError: If the boto3 S3 client cannot be created.
        """
        # Build the BlobStoreConfiguration required by the base class
        blob_store_config = BlobStoreConfiguration(
            name=name,
            store_type=BlobStoreType.S3,
            config=config,
        )
        super().__init__(name=name, config=blob_store_config)

        # Store raw config dict for direct S3-specific access
        self._raw_config: dict = config

        # Extract S3-specific configuration
        self._bucket: str = config['bucket']
        self._region: str = config.get('region', 'us-east-1')
        self._prefix: str = config.get('prefix', '')
        self._endpoint: Optional[str] = config.get('endpoint')
        self._access_key: Optional[str] = config.get('accessKeyId')
        self._secret_key: Optional[str] = config.get('secretAccessKey')
        self._force_path_style: bool = config.get('forcePathStyle', False)
        self._encryption: dict = config.get('encryption', {})

        # S3 client — initialised immediately
        self._client: Any = None
        self._resource: Any = None
        self._create_client()

        # NOTE: credentials are never included in log output (security)
        logger.info(
            "S3BlobStore initialised: %s -> s3://%s/%s (region=%s, endpoint=%s)",
            name,
            self._bucket,
            self._prefix,
            self._region,
            self._endpoint or 'default',
        )

    # -- Client initialisation ----------------------------------------------

    def _create_client(self) -> None:
        """Create the boto3 S3 client and optional resource.

        Uses credentials from the config dict when provided; otherwise
        falls back to the default boto3 credential chain (IAM roles,
        environment variables, ``~/.aws/credentials``, etc.).
        """
        try:
            boto_config = BotoConfig(
                max_pool_connections=DEFAULT_MAX_CONNECTIONS,
                connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                read_timeout=DEFAULT_READ_TIMEOUT,
                s3={
                    'addressing_style': 'path' if self._force_path_style else 'auto',
                },
            )

            client_kwargs: dict[str, Any] = {
                'service_name': 's3',
                'config': boto_config,
                'region_name': self._region,
            }
            if self._endpoint:
                client_kwargs['endpoint_url'] = self._endpoint
            if self._access_key and self._secret_key:
                client_kwargs['aws_access_key_id'] = self._access_key
                client_kwargs['aws_secret_access_key'] = self._secret_key

            self._client = boto3.client(**client_kwargs)

            # Resource for higher-level operations (optional)
            resource_kwargs: dict[str, Any] = {
                'service_name': 's3',
                'config': boto_config,
                'region_name': self._region,
            }
            if self._endpoint:
                resource_kwargs['endpoint_url'] = self._endpoint
            if self._access_key and self._secret_key:
                resource_kwargs['aws_access_key_id'] = self._access_key
                resource_kwargs['aws_secret_access_key'] = self._secret_key

            self._resource = boto3.resource(**resource_kwargs)

        except (BotoCoreError, Exception) as exc:
            logger.error(
                "Failed to create S3 client for BlobStore '%s': %s",
                self._name,
                exc,
            )
            raise BlobStoreError(
                f"Failed to create S3 client for BlobStore '{self._name}': {exc}"
            ) from exc

    # -- Properties ---------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the unique name of this BlobStore instance."""
        return self._name

    @property
    def store_type(self) -> str:
        """Return the backend type identifier (``'s3'``)."""
        return 's3'

    @property
    def bucket(self) -> str:
        """Return the S3 bucket name."""
        return self._bucket

    @property
    def region(self) -> str:
        """Return the configured AWS region."""
        return self._region

    @property
    def is_available(self) -> bool:
        """Check whether the configured S3 bucket is reachable.

        Performs a ``HeadBucket`` API call and returns ``True`` if the
        bucket responds successfully.  Returns ``False`` on any error
        (including 403 Forbidden or network failures).
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return True
        except (ClientError, BotoCoreError) as exc:
            logger.warning(
                "S3 bucket '%s' is not available for BlobStore '%s': %s",
                self._bucket,
                self._name,
                exc,
            )
            return False

    # -- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Initialise the S3 storage backend.

        Verifies that the S3 bucket exists and is accessible, then marks
        the store as started.

        Raises:
            BlobStoreError: If the bucket is not accessible.
        """
        if self._started:
            logger.debug("S3BlobStore '%s' is already started.", self._name)
            return

        logger.info("Starting S3BlobStore '%s' (bucket=%s) ...", self._name, self._bucket)

        # Verify bucket accessibility
        if not self.is_available:
            raise BlobStoreError(
                f"S3 bucket '{self._bucket}' is not accessible for "
                f"BlobStore '{self._name}'. Check credentials and permissions."
            )

        self._started = True
        logger.info("S3BlobStore '%s' started successfully.", self._name)

    def stop(self) -> None:
        """Gracefully shut down the S3 storage backend.

        Closes the boto3 client connection pools and marks the store as
        stopped.
        """
        if not self._started:
            logger.debug("S3BlobStore '%s' is already stopped.", self._name)
            return

        logger.info("Stopping S3BlobStore '%s' ...", self._name)
        self._started = False
        logger.info("S3BlobStore '%s' stopped.", self._name)

    # -- Core CRUD: create --------------------------------------------------

    def create(
        self,
        blob_id: BlobId | None,
        data: bytes | BinaryIO,
        content_type: str = 'application/octet-stream',
        headers: dict[str, str] | None = None,
    ) -> Blob:
        """Store binary data as a new blob in S3.

        If *blob_id* is ``None`` a fresh identifier is generated via
        :meth:`BlobId.generate`.  For blobs larger than
        :data:`MULTIPART_THRESHOLD` (5 MB) a multipart upload is used
        automatically.

        Args:
            blob_id:      Optional pre-determined blob identifier.
            data:         Binary payload as ``bytes`` or a readable stream.
            content_type: MIME type for the stored content.
            headers:      Additional metadata key-value pairs.

        Returns:
            The fully populated :class:`Blob` with all metadata.

        Raises:
            BlobStoreError: On any S3 upload failure.
        """
        self._ensure_started()
        start_time = time.time()

        # 1. Generate a blob_id if not provided
        if blob_id is None:
            blob_id = BlobId.generate(self._name)

        key = self._blob_key(blob_id)
        metadata_key = self._metadata_key(blob_id)

        # 2. Normalise data to bytes so we can compute checksums and size
        if isinstance(data, (bytes, bytearray)):
            raw_bytes: bytes = bytes(data)
        else:
            raw_bytes = data.read()

        size = len(raw_bytes)

        # 3. Compute checksums (SHA-1, SHA-256, MD5) using helpers
        checksums = compute_checksums(raw_bytes)

        # 4. Build BlobAttributes
        now = datetime.now(timezone.utc)
        attrs = BlobAttributes(
            content_type=content_type,
            sha1=checksums['sha1'],
            sha256=checksums['sha256'],
            md5=checksums['md5'],
            size=size,
            creation_time=now,
            headers=dict(headers or {}),
        )

        # 5. Build S3 object metadata dict
        s3_metadata: dict[str, str] = {
            META_SHA1: attrs.sha1,
            META_SHA256: attrs.sha256,
            META_MD5: attrs.md5,
            META_CONTENT_TYPE: content_type,
            META_CREATED: now.isoformat(),
            META_SIZE: str(size),
        }

        # 6. Get encryption kwargs
        encryption_kwargs = self._get_encryption_kwargs()

        # 7. Upload to S3
        try:
            if size < MULTIPART_THRESHOLD:
                # Simple single-request upload
                put_kwargs: dict[str, Any] = {
                    'Bucket': self._bucket,
                    'Key': key,
                    'Body': raw_bytes,
                    'ContentType': content_type,
                    'Metadata': s3_metadata,
                }
                put_kwargs.update(encryption_kwargs)
                self._client.put_object(**put_kwargs)
            else:
                # Multipart upload for large blobs
                stream = io.BytesIO(raw_bytes)
                self._multipart_upload(
                    key=key,
                    stream=stream,
                    content_type=content_type,
                    encryption_kwargs=encryption_kwargs,
                    s3_metadata=s3_metadata,
                )

            # 8. Write metadata sidecar to a separate S3 key
            self._write_metadata_sidecar(blob_id, attrs)

        except (ClientError, BotoCoreError) as exc:
            logger.error(
                "Failed to upload blob %s to S3 (bucket=%s, key=%s): %s",
                blob_id, self._bucket, key, exc,
            )
            raise BlobStoreError(
                f"S3 upload failed for blob {blob_id}: {exc}"
            ) from exc

        elapsed_ms = (time.time() - start_time) * 1000
        logger.debug(
            "S3 blob created: %s (%s) in s3://%s/%s [%.1f ms]",
            blob_id, format_file_size(size), self._bucket, key, elapsed_ms,
        )

        return Blob(
            blob_id=blob_id,
            attributes=attrs,
            soft_deleted=False,
            deleted_at=None,
        )

    # -- Core CRUD: get -----------------------------------------------------

    def get(self, blob_id: BlobId) -> Blob | None:
        """Retrieve blob metadata from S3 without downloading content.

        Uses ``HeadObject`` to fetch metadata and checks for soft-delete
        tags.  Returns ``None`` if the blob does not exist or is
        soft-deleted.

        Args:
            blob_id: Identifier of the blob to retrieve.

        Returns:
            :class:`Blob` with populated metadata, or ``None``.
        """
        self._ensure_started()
        key = self._blob_key(blob_id)

        try:
            # Attempt to read from sidecar first for richer metadata
            attrs = self._read_metadata_sidecar(blob_id)
            if attrs is None:
                # Fall back to S3 object metadata
                head_kwargs: dict[str, Any] = {
                    'Bucket': self._bucket,
                    'Key': key,
                }
                head_kwargs.update(self._get_sse_c_get_kwargs())
                response = self._client.head_object(**head_kwargs)

                s3_meta = response.get('Metadata', {})
                attrs = BlobAttributes(
                    content_type=s3_meta.get(META_CONTENT_TYPE, response.get('ContentType', 'application/octet-stream')),
                    sha1=s3_meta.get(META_SHA1, ''),
                    sha256=s3_meta.get(META_SHA256, ''),
                    md5=s3_meta.get(META_MD5, ''),
                    size=response.get('ContentLength', 0),
                    creation_time=datetime.fromisoformat(s3_meta[META_CREATED]) if META_CREATED in s3_meta else datetime.now(timezone.utc),
                    headers={k: v for k, v in s3_meta.items() if k not in {META_SHA1, META_SHA256, META_MD5, META_CONTENT_TYPE, META_CREATED, META_SIZE}},
                )

            # Check soft-delete tags
            is_deleted, deleted_at = self._is_soft_deleted(blob_id)
            if is_deleted:
                return None

            return Blob(
                blob_id=blob_id,
                attributes=attrs,
                soft_deleted=False,
                deleted_at=None,
            )

        except ClientError as exc:
            error_code = exc.response.get('Error', {}).get('Code', '')
            if error_code in ('404', 'NoSuchKey'):
                return None
            logger.error("Failed to get blob %s from S3: %s", blob_id, exc)
            raise BlobStoreError(f"S3 get failed for blob {blob_id}: {exc}") from exc
        except BotoCoreError as exc:
            logger.error("Failed to get blob %s from S3: %s", blob_id, exc)
            raise BlobStoreError(f"S3 get failed for blob {blob_id}: {exc}") from exc

    # -- Core CRUD: get_stream ----------------------------------------------

    def get_stream(self, blob_id: BlobId) -> BinaryIO | None:
        """Retrieve blob content as a readable binary stream.

        Returns an :class:`_S3StreamAdapter` wrapping the S3
        ``StreamingBody``.  Returns ``None`` if the blob does not exist
        or is soft-deleted.

        Args:
            blob_id: Identifier of the blob whose content to stream.

        Returns:
            A readable stream wrapping the S3 response body, or ``None``.
        """
        self._ensure_started()
        start_time = time.time()
        key = self._blob_key(blob_id)

        try:
            # Check soft-delete status first
            is_deleted, _ = self._is_soft_deleted(blob_id)
            if is_deleted:
                return None

            get_kwargs: dict[str, Any] = {
                'Bucket': self._bucket,
                'Key': key,
            }
            get_kwargs.update(self._get_sse_c_get_kwargs())

            response = self._client.get_object(**get_kwargs)
            content_length = response.get('ContentLength', 0)

            elapsed_ms = (time.time() - start_time) * 1000
            logger.debug(
                "S3 stream opened: %s (%s) [%.1f ms]",
                blob_id, format_file_size(content_length), elapsed_ms,
            )

            return _S3StreamAdapter(
                streaming_body=response['Body'],
                content_length=content_length,
            )

        except ClientError as exc:
            error_code = exc.response.get('Error', {}).get('Code', '')
            if error_code in ('404', 'NoSuchKey'):
                return None
            logger.error("Failed to stream blob %s from S3: %s", blob_id, exc)
            raise BlobStoreError(f"S3 stream failed for blob {blob_id}: {exc}") from exc
        except BotoCoreError as exc:
            logger.error("Failed to stream blob %s from S3: %s", blob_id, exc)
            raise BlobStoreError(f"S3 stream failed for blob {blob_id}: {exc}") from exc

    # -- Core CRUD: delete --------------------------------------------------

    def delete(self, blob_id: BlobId, soft: bool = True) -> bool:
        """Delete a blob from S3.

        * **Soft-delete** (``soft=True``, default): Applies S3 object tags
          ``nexus-deleted=true`` and ``nexus-deleted-at=<ISO timestamp>``
          to the content object.  This preserves the data for later
          recovery (:meth:`undelete`) or deferred permanent removal
          (:meth:`compact`).
        * **Hard-delete** (``soft=False``): Permanently removes both the
          content object and the metadata sidecar from S3.

        Args:
            blob_id: Identifier of the blob to delete.
            soft:    If ``True`` (default) perform a soft-delete.

        Returns:
            ``True`` if the blob was found and deleted, ``False`` if not
            found.
        """
        self._ensure_started()
        key = self._blob_key(blob_id)
        metadata_key = self._metadata_key(blob_id)

        try:
            # Verify the object exists
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code', '') in ('404', 'NoSuchKey'):
                    logger.debug("Blob %s not found for deletion.", blob_id)
                    return False
                raise

            if soft:
                # Tag the object as soft-deleted
                now_iso = datetime.now(timezone.utc).isoformat()
                self._client.put_object_tagging(
                    Bucket=self._bucket,
                    Key=key,
                    Tagging={
                        'TagSet': [
                            {'Key': DELETED_TAG_KEY, 'Value': DELETED_TAG_VALUE},
                            {'Key': DELETED_AT_TAG_KEY, 'Value': now_iso},
                        ],
                    },
                )
                logger.debug("S3 blob soft-deleted: %s (tagged nexus-deleted)", blob_id)
            else:
                # Permanently remove both content and metadata objects
                self._client.delete_object(Bucket=self._bucket, Key=key)
                try:
                    self._client.delete_object(Bucket=self._bucket, Key=metadata_key)
                except ClientError:
                    pass  # Metadata sidecar may not exist; ignore
                logger.debug("S3 blob hard-deleted: %s", blob_id)

            return True

        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to delete blob %s from S3: %s", blob_id, exc)
            raise BlobStoreError(f"S3 delete failed for blob {blob_id}: {exc}") from exc

    # -- Core CRUD: exists --------------------------------------------------

    def exists(self, blob_id: BlobId) -> bool:
        """Check whether a non-deleted blob exists in S3.

        Performs a ``HeadObject`` API call and verifies that the object
        is **not** tagged as soft-deleted.

        Args:
            blob_id: Identifier to check.

        Returns:
            ``True`` if the blob exists and is not soft-deleted.
        """
        self._ensure_started()
        key = self._blob_key(blob_id)

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code', '') in ('404', 'NoSuchKey'):
                return False
            logger.error("Error checking existence of blob %s: %s", blob_id, exc)
            return False
        except BotoCoreError as exc:
            logger.error("Error checking existence of blob %s: %s", blob_id, exc)
            return False

        # Check soft-delete tags
        is_deleted, _ = self._is_soft_deleted(blob_id)
        return not is_deleted

    # -- Core CRUD: undelete ------------------------------------------------

    def undelete(self, blob_id: BlobId) -> bool:
        """Restore a soft-deleted blob by removing its delete tags.

        Args:
            blob_id: Identifier of the soft-deleted blob.

        Returns:
            ``True`` if the blob was found and successfully restored,
            ``False`` if it was not found or was not soft-deleted.
        """
        self._ensure_started()
        key = self._blob_key(blob_id)

        try:
            # Verify the object exists
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code', '') in ('404', 'NoSuchKey'):
                    return False
                raise

            # Check if it is actually soft-deleted
            is_deleted, _ = self._is_soft_deleted(blob_id)
            if not is_deleted:
                logger.debug("Blob %s is not soft-deleted; nothing to undelete.", blob_id)
                return False

            # Remove all tags (including soft-delete markers) from the object
            self._client.delete_object_tagging(
                Bucket=self._bucket,
                Key=key,
            )
            logger.info("S3 blob undeleted: %s", blob_id)
            return True

        except (ClientError, BotoCoreError) as exc:
            logger.error("Failed to undelete blob %s: %s", blob_id, exc)
            raise BlobStoreError(f"S3 undelete failed for blob {blob_id}: {exc}") from exc

    # -- S3 key resolution --------------------------------------------------

    def _blob_key(self, blob_id: BlobId) -> str:
        """Compute the full S3 object key for a blob's content.

        The key uses a hash-sharded directory structure for even
        distribution across S3 partitions:

            ``{prefix}content/{id[0:2]}/{id[2:4]}/{id[4:6]}/{id}.bytes``

        Example::

            nexus/content/a1/b2/c3/a1b2c3d4-e5f6-7890-abcd-ef1234567890.bytes
        """
        bid = blob_id.value.replace('-', '')
        return (
            f"{self._prefix}{CONTENT_PREFIX}"
            f"{bid[:2]}/{bid[2:4]}/{bid[4:6]}/{blob_id.value}.bytes"
        )

    def _metadata_key(self, blob_id: BlobId) -> str:
        """Compute the S3 key for the metadata sidecar object.

        Mirrors the content key structure under the metadata prefix:

            ``{prefix}metadata/{id[0:2]}/{id[2:4]}/{id[4:6]}/{id}.properties``
        """
        bid = blob_id.value.replace('-', '')
        return (
            f"{self._prefix}{METADATA_PREFIX}"
            f"{bid[:2]}/{bid[2:4]}/{bid[4:6]}/{blob_id.value}.properties"
        )

    # -- Metadata sidecar helpers -------------------------------------------

    def _write_metadata_sidecar(self, blob_id: BlobId, attrs: BlobAttributes) -> None:
        """Write blob attributes as a JSON sidecar object in S3.

        The sidecar is stored under :data:`METADATA_PREFIX` and contains
        all metadata from the :class:`BlobAttributes` instance.
        """
        key = self._metadata_key(blob_id)
        payload = json.dumps(attrs.to_dict(), default=str).encode('utf-8')

        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=payload,
                ContentType='application/json',
            )
        except (ClientError, BotoCoreError) as exc:
            # Metadata write failure is logged but does not fail the create
            logger.warning(
                "Failed to write metadata sidecar for blob %s: %s",
                blob_id, exc,
            )

    def _read_metadata_sidecar(self, blob_id: BlobId) -> Optional[BlobAttributes]:
        """Read blob attributes from the JSON sidecar in S3.

        Returns ``None`` if the sidecar does not exist.
        """
        key = self._metadata_key(blob_id)

        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=key,
            )
            body = response['Body'].read()
            data = json.loads(body.decode('utf-8'))
            return BlobAttributes.from_dict(data)
        except ClientError as exc:
            error_code = exc.response.get('Error', {}).get('Code', '')
            if error_code in ('404', 'NoSuchKey'):
                return None
            logger.warning("Failed to read metadata sidecar for blob %s: %s", blob_id, exc)
            return None
        except (BotoCoreError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to parse metadata sidecar for blob %s: %s", blob_id, exc)
            return None

    # -- Soft-delete tag helpers --------------------------------------------

    def _is_soft_deleted(self, blob_id: BlobId) -> tuple[bool, Optional[datetime]]:
        """Check whether a blob is tagged as soft-deleted.

        Returns:
            A tuple ``(is_deleted, deleted_at)`` where *deleted_at* is a
            :class:`datetime` parsed from the ``nexus-deleted-at`` tag or
            ``None`` if the tag is missing.
        """
        tags = self._get_object_tags(blob_id)
        if not tags:
            return False, None

        tag_map = {t['Key']: t['Value'] for t in tags}
        is_deleted = tag_map.get(DELETED_TAG_KEY, '').lower() == DELETED_TAG_VALUE

        deleted_at: Optional[datetime] = None
        if is_deleted and DELETED_AT_TAG_KEY in tag_map:
            try:
                deleted_at = datetime.fromisoformat(tag_map[DELETED_AT_TAG_KEY])
            except (ValueError, TypeError):
                deleted_at = None

        return is_deleted, deleted_at

    def _get_object_tags(self, blob_id: BlobId) -> list[dict[str, str]]:
        """Retrieve S3 object tags for a blob's content key.

        Returns an empty list if the object has no tags or does not exist.
        """
        key = self._blob_key(blob_id)
        try:
            response = self._client.get_object_tagging(
                Bucket=self._bucket,
                Key=key,
            )
            return response.get('TagSet', [])
        except (ClientError, BotoCoreError):
            return []

    # -- Multipart upload ---------------------------------------------------

    def _multipart_upload(
        self,
        key: str,
        stream: BinaryIO,
        content_type: Optional[str],
        encryption_kwargs: dict,
        s3_metadata: Optional[dict[str, str]] = None,
    ) -> dict:
        """Perform a multipart upload for large blobs.

        Reads the stream in chunks of :data:`MULTIPART_CHUNK_SIZE` and
        uploads each chunk as a separate part.  If any part upload fails
        the entire multipart upload is aborted.

        Args:
            key:               S3 object key.
            stream:            Binary stream to upload.
            content_type:      MIME content type.
            encryption_kwargs: Server-side encryption parameters.
            s3_metadata:       Optional S3 object metadata dict.

        Returns:
            A dictionary with the ``ETag`` and other upload metadata.

        Raises:
            BlobStoreError: If the multipart upload fails.
        """
        create_kwargs: dict[str, Any] = {
            'Bucket': self._bucket,
            'Key': key,
        }
        if content_type:
            create_kwargs['ContentType'] = content_type
        if s3_metadata:
            create_kwargs['Metadata'] = s3_metadata
        create_kwargs.update(encryption_kwargs)

        upload_id: Optional[str] = None
        parts: list[dict[str, Any]] = []
        total_uploaded = 0

        try:
            # Initiate multipart upload
            mpu_response = self._client.create_multipart_upload(**create_kwargs)
            upload_id = mpu_response['UploadId']
            logger.debug(
                "Multipart upload started: key=%s, upload_id=%s",
                key, upload_id,
            )

            part_number = 1
            while True:
                chunk = stream.read(MULTIPART_CHUNK_SIZE)
                if not chunk:
                    break

                if part_number > MAX_MULTIPART_PARTS:
                    raise BlobStoreError(
                        f"Multipart upload exceeds maximum parts ({MAX_MULTIPART_PARTS}) "
                        f"for key={key}"
                    )

                upload_part_kwargs: dict[str, Any] = {
                    'Bucket': self._bucket,
                    'Key': key,
                    'UploadId': upload_id,
                    'PartNumber': part_number,
                    'Body': chunk,
                }
                # For SSE-C, each part upload also needs the customer key
                enc_type = self._encryption.get('type', ENCRYPTION_NONE)
                if enc_type == ENCRYPTION_CUSTOMER:
                    upload_part_kwargs.update(self._get_sse_c_put_kwargs())

                part_response = self._client.upload_part(**upload_part_kwargs)
                parts.append({
                    'ETag': part_response['ETag'],
                    'PartNumber': part_number,
                })

                total_uploaded += len(chunk)
                if part_number % 10 == 0:
                    logger.debug(
                        "Multipart upload progress: key=%s, parts=%d, uploaded=%s",
                        key, part_number, format_file_size(total_uploaded),
                    )
                part_number += 1

            if not parts:
                # No data to upload — abort
                self._abort_multipart_if_needed(self._bucket, key, upload_id)
                raise BlobStoreError(f"Multipart upload received empty stream for key={key}")

            # Complete the multipart upload
            complete_response = self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={'Parts': parts},
            )

            logger.debug(
                "Multipart upload completed: key=%s, parts=%d, total=%s",
                key, len(parts), format_file_size(total_uploaded),
            )

            return {
                'ETag': complete_response.get('ETag', ''),
                'Location': complete_response.get('Location', ''),
                'parts': len(parts),
                'total_bytes': total_uploaded,
            }

        except (ClientError, BotoCoreError) as exc:
            if upload_id:
                self._abort_multipart_if_needed(self._bucket, key, upload_id)
            logger.error(
                "Multipart upload failed for key=%s: %s", key, exc,
            )
            raise BlobStoreError(
                f"Multipart upload failed for key={key}: {exc}"
            ) from exc

    def _abort_multipart_if_needed(
        self, bucket: str, key: str, upload_id: str
    ) -> None:
        """Abort an in-progress multipart upload, logging but not raising
        on failure.
        """
        try:
            self._client.abort_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
            )
            logger.info(
                "Aborted multipart upload: key=%s, upload_id=%s",
                key, upload_id,
            )
        except (ClientError, BotoCoreError) as abort_exc:
            logger.warning(
                "Failed to abort multipart upload (key=%s, upload_id=%s): %s",
                key, upload_id, abort_exc,
            )

    # -- Server-side encryption helpers -------------------------------------

    def _get_encryption_kwargs(self) -> dict[str, Any]:
        """Build S3 encryption keyword arguments for PUT operations.

        Returns a dictionary that can be unpacked into ``put_object()``
        or ``create_multipart_upload()`` calls.
        """
        enc_type = self._encryption.get('type', ENCRYPTION_NONE)

        if enc_type == ENCRYPTION_S3_MANAGED:
            return {'ServerSideEncryption': 'AES256'}

        if enc_type == ENCRYPTION_KMS:
            kwargs: dict[str, Any] = {'ServerSideEncryption': 'aws:kms'}
            kms_key = self._encryption.get('key', '')
            if kms_key:
                kwargs['SSEKMSKeyId'] = kms_key
            return kwargs

        if enc_type == ENCRYPTION_CUSTOMER:
            return self._get_sse_c_put_kwargs()

        # ENCRYPTION_NONE or unrecognised → no encryption headers
        return {}

    def _get_sse_c_put_kwargs(self) -> dict[str, str]:
        """Build SSE-C keyword arguments for PUT / upload operations.

        The customer key and its MD5 digest are extracted from the
        encryption config.
        """
        import hashlib
        import base64

        customer_key = self._encryption.get('key', '')
        if not customer_key:
            return {}

        # The key must be raw bytes (32 bytes for AES-256)
        if isinstance(customer_key, str):
            key_bytes = customer_key.encode('utf-8')[:32].ljust(32, b'\0')
        else:
            key_bytes = customer_key

        key_b64 = base64.b64encode(key_bytes).decode('ascii')
        key_md5 = base64.b64encode(hashlib.md5(key_bytes).hexdigest().encode('ascii')).decode('ascii')

        return {
            'SSECustomerAlgorithm': 'AES256',
            'SSECustomerKey': key_b64,
            'SSECustomerKeyMD5': key_md5,
        }

    def _get_sse_c_get_kwargs(self) -> dict[str, str]:
        """Build SSE-C keyword arguments for GET / HEAD operations.

        SSE-C requires the same customer key to be provided on read.
        """
        enc_type = self._encryption.get('type', ENCRYPTION_NONE)
        if enc_type == ENCRYPTION_CUSTOMER:
            return self._get_sse_c_put_kwargs()
        return {}

    # -- Compaction and cleanup ---------------------------------------------

    def compact(self, cutoff: Optional[datetime] = None) -> int:
        """Permanently remove soft-deleted S3 objects past the retention period.

        Iterates over all content objects in the bucket, checks their
        soft-delete tags, and permanently deletes those whose
        ``nexus-deleted-at`` timestamp is older than *cutoff*.  Uses
        batch delete (``delete_objects()``) for efficiency.

        Args:
            cutoff: Remove blobs deleted before this time.  Defaults to
                    30 days ago.

        Returns:
            The number of blobs permanently removed.
        """
        self._ensure_started()
        start_time = time.time()

        if cutoff is None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)

        logger.info(
            "Starting S3 compaction for BlobStore '%s' (cutoff=%s) ...",
            self._name, cutoff.isoformat(),
        )

        blobs_removed = 0
        space_reclaimed = 0
        keys_to_delete: list[dict[str, str]] = []

        content_prefix = f"{self._prefix}{CONTENT_PREFIX}"
        paginator = self._client.get_paginator('list_objects_v2')

        try:
            for page in paginator.paginate(Bucket=self._bucket, Prefix=content_prefix):
                for obj in page.get('Contents', []):
                    obj_key = obj['Key']
                    obj_size = obj.get('Size', 0)

                    # Check tags on each object
                    try:
                        tag_response = self._client.get_object_tagging(
                            Bucket=self._bucket, Key=obj_key,
                        )
                        tag_map = {
                            t['Key']: t['Value']
                            for t in tag_response.get('TagSet', [])
                        }
                    except (ClientError, BotoCoreError):
                        continue

                    if tag_map.get(DELETED_TAG_KEY, '').lower() != DELETED_TAG_VALUE:
                        continue

                    # Parse deletion timestamp
                    deleted_at_str = tag_map.get(DELETED_AT_TAG_KEY, '')
                    if not deleted_at_str:
                        continue

                    try:
                        deleted_at = datetime.fromisoformat(deleted_at_str)
                    except (ValueError, TypeError):
                        continue

                    if deleted_at >= cutoff:
                        continue  # Not yet past the retention period

                    # Mark for deletion
                    keys_to_delete.append({'Key': obj_key})

                    # Also queue the metadata sidecar for deletion
                    meta_key = obj_key.replace(CONTENT_PREFIX, METADATA_PREFIX, 1)
                    meta_key = meta_key.replace('.bytes', '.properties')
                    keys_to_delete.append({'Key': meta_key})

                    space_reclaimed += obj_size
                    blobs_removed += 1

                    # Flush batch when we hit the limit
                    if len(keys_to_delete) >= BATCH_DELETE_LIMIT:
                        self._batch_delete(keys_to_delete)
                        keys_to_delete = []

            # Delete any remaining keys
            if keys_to_delete:
                self._batch_delete(keys_to_delete)

        except (ClientError, BotoCoreError) as exc:
            logger.error("Error during S3 compaction: %s", exc)

        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(
            "S3 compaction completed for '%s': removed=%d, reclaimed=%s [%.1f ms]",
            self._name, blobs_removed, format_file_size(space_reclaimed), elapsed_ms,
        )

        return blobs_removed

    def cleanup_temp(self, cutoff: Optional[datetime] = None) -> dict:
        """Abort incomplete multipart uploads older than *cutoff*.

        S3 multipart uploads that were started but never completed (e.g.
        due to client crashes) consume storage.  This method lists all
        in-progress uploads and aborts those initiated before *cutoff*.

        Args:
            cutoff: Abort uploads initiated before this time.  Defaults
                    to 24 hours ago.

        Returns:
            A dictionary with cleanup statistics.
        """
        self._ensure_started()
        start_time = time.time()

        if cutoff is None:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        uploads_aborted = 0
        errors = 0

        try:
            # List all in-progress multipart uploads
            list_kwargs: dict[str, Any] = {
                'Bucket': self._bucket,
            }
            if self._prefix:
                list_kwargs['Prefix'] = self._prefix

            response = self._client.list_multipart_uploads(**list_kwargs)

            for upload in response.get('Uploads', []):
                initiated = upload.get('Initiated')
                if initiated is None:
                    continue

                # initiated is a datetime from boto3 (tz-aware)
                if hasattr(initiated, 'tzinfo') and initiated.tzinfo is None:
                    initiated = initiated.replace(tzinfo=timezone.utc)

                if initiated < cutoff:
                    try:
                        self._client.abort_multipart_upload(
                            Bucket=self._bucket,
                            Key=upload['Key'],
                            UploadId=upload['UploadId'],
                        )
                        uploads_aborted += 1
                        logger.debug(
                            "Aborted stale multipart upload: key=%s, upload_id=%s, initiated=%s",
                            upload['Key'], upload['UploadId'], initiated.isoformat(),
                        )
                    except (ClientError, BotoCoreError) as exc:
                        errors += 1
                        logger.warning(
                            "Failed to abort multipart upload %s: %s",
                            upload['UploadId'], exc,
                        )

        except (ClientError, BotoCoreError) as exc:
            logger.error("Error listing multipart uploads for cleanup: %s", exc)

        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(
            "S3 temp cleanup for '%s': aborted=%d, errors=%d [%.1f ms]",
            self._name, uploads_aborted, errors, elapsed_ms,
        )

        return {
            'blob_store_name': self._name,
            'uploads_aborted': uploads_aborted,
            'errors': errors,
            'duration_ms': round(elapsed_ms, 1),
        }

    def _batch_delete(self, keys: list[dict[str, str]]) -> None:
        """Batch-delete a list of S3 objects using ``delete_objects()``.

        Processes up to :data:`BATCH_DELETE_LIMIT` keys per API call.
        """
        if not keys:
            return

        # Process in batches of BATCH_DELETE_LIMIT
        for i in range(0, len(keys), BATCH_DELETE_LIMIT):
            batch = keys[i:i + BATCH_DELETE_LIMIT]
            try:
                self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={'Objects': batch, 'Quiet': True},
                )
            except (ClientError, BotoCoreError) as exc:
                logger.warning(
                    "Batch delete failed for %d objects: %s",
                    len(batch), exc,
                )

    # -- Metrics and statistics ---------------------------------------------

    def get_metrics(self) -> BlobStoreMetrics:
        """Collect S3 storage usage statistics.

        Iterates over all content objects in the bucket to count blobs
        and sum their sizes.  **Note:** This can be expensive for very
        large buckets; consider caching the results.

        S3 storage is effectively unlimited, so ``available_space_bytes``
        is reported as ``-1``.

        Returns:
            :class:`BlobStoreMetrics` populated with current statistics.
        """
        self._ensure_started()
        start_time = time.time()

        total_size = 0
        blob_count = 0
        soft_deleted_count = 0
        soft_deleted_size = 0

        content_prefix = f"{self._prefix}{CONTENT_PREFIX}"
        paginator = self._client.get_paginator('list_objects_v2')

        try:
            for page in paginator.paginate(Bucket=self._bucket, Prefix=content_prefix):
                for obj in page.get('Contents', []):
                    obj_size = obj.get('Size', 0)

                    # Check for soft-delete tags
                    try:
                        tag_response = self._client.get_object_tagging(
                            Bucket=self._bucket,
                            Key=obj['Key'],
                        )
                        tag_map = {
                            t['Key']: t['Value']
                            for t in tag_response.get('TagSet', [])
                        }
                        is_deleted = tag_map.get(DELETED_TAG_KEY, '').lower() == DELETED_TAG_VALUE
                    except (ClientError, BotoCoreError):
                        is_deleted = False

                    if is_deleted:
                        soft_deleted_count += 1
                        soft_deleted_size += obj_size
                    else:
                        blob_count += 1
                        total_size += obj_size

        except (ClientError, BotoCoreError) as exc:
            logger.error("Error collecting S3 metrics for '%s': %s", self._name, exc)

        elapsed_ms = (time.time() - start_time) * 1000
        logger.debug(
            "S3 metrics for '%s': blobs=%d, size=%s, deleted=%d [%.1f ms]",
            self._name, blob_count, format_file_size(total_size), soft_deleted_count, elapsed_ms,
        )

        return BlobStoreMetrics(
            blob_store_name=self._name,
            blob_store_type='s3',
            total_size_bytes=total_size,
            blob_count=blob_count,
            soft_deleted_count=soft_deleted_count,
            soft_deleted_size_bytes=soft_deleted_size,
            available_space_bytes=-1,   # S3 is effectively unlimited
            total_space_bytes=-1,       # S3 is effectively unlimited
            usage_percentage=0.0,       # Not applicable for S3
        )

    # -- Blob listing -------------------------------------------------------

    def list_blobs(self, include_deleted: bool = False) -> Iterator[Blob]:
        """Iterate over all blobs in the S3 BlobStore.

        Lists all content objects under the configured prefix and yields
        :class:`Blob` instances with metadata.

        Args:
            include_deleted: If ``True``, soft-deleted blobs are included.

        Yields:
            :class:`Blob` instances for each stored blob.
        """
        self._ensure_started()

        content_prefix = f"{self._prefix}{CONTENT_PREFIX}"
        paginator = self._client.get_paginator('list_objects_v2')

        try:
            for page in paginator.paginate(Bucket=self._bucket, Prefix=content_prefix):
                for obj in page.get('Contents', []):
                    obj_key = obj['Key']

                    # Extract blob_id value from the key
                    # Key format: {prefix}content/{xx}/{xx}/{xx}/{uuid}.bytes
                    blob_value = obj_key.rsplit('/', 1)[-1]
                    if blob_value.endswith('.bytes'):
                        blob_value = blob_value[:-6]  # strip .bytes
                    else:
                        continue  # Not a blob content file

                    blob_id = BlobId(value=blob_value, store_name=self._name)

                    # Check soft-delete status
                    is_deleted, deleted_at = self._is_soft_deleted(blob_id)
                    if is_deleted and not include_deleted:
                        continue

                    # Load attributes from sidecar or head_object
                    attrs = self._read_metadata_sidecar(blob_id)
                    if attrs is None:
                        attrs = BlobAttributes(
                            content_type='application/octet-stream',
                            size=obj.get('Size', 0),
                            creation_time=obj.get('LastModified', datetime.now(timezone.utc)),
                        )

                    yield Blob(
                        blob_id=blob_id,
                        attributes=attrs,
                        soft_deleted=is_deleted,
                        deleted_at=deleted_at,
                    )

        except (ClientError, BotoCoreError) as exc:
            logger.error("Error listing blobs in S3 BlobStore '%s': %s", self._name, exc)

    # -- String representation ----------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return (
            f"<S3BlobStore name='{self._name}' bucket='{self._bucket}' "
            f"region='{self._region}' started={self._started}>"
        )


# ---------------------------------------------------------------------------
# Module-level factory function
# ---------------------------------------------------------------------------


def create_s3_blobstore(name: str, config: dict) -> S3BlobStore:
    """Create an :class:`S3BlobStore` instance from a configuration dict.

    This is the factory function used by the BlobStore registry in
    :mod:`src.app.storage.__init__` to instantiate S3 backends.

    Args:
        name:   Unique BlobStore name.
        config: Configuration dictionary.  The ``bucket`` key is
                **required**; see :class:`S3BlobStore` for all supported
                keys.

    Returns:
        A fully initialised (but **not** started) :class:`S3BlobStore`.

    Raises:
        ValueError: If the ``bucket`` key is missing from *config*.
    """
    if 'bucket' not in config:
        raise ValueError(
            f"S3BlobStore configuration for '{name}' must include a 'bucket' key."
        )

    return S3BlobStore(name=name, config=config)
