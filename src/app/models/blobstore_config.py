"""
BlobStoreConfig SQLAlchemy Model.

Defines the ``BlobStoreConfig`` model that replaces the ``BLOBSTORE_CONFIG``
entity from the Java DataStore schema (Section 6.2.1.2).  This model stores
the configuration for pluggable BlobStore backends — supporting both local
filesystem storage (Feature F-201) and Amazon S3 storage (Feature F-202).

**Architecture Context:**
In the original Java system, BlobStore configurations were persisted via
MyBatis 3.5.15 mappers against the ``BLOBSTORE_CONFIG`` table.  Here, the
same schema is represented as a SQLAlchemy Declarative model inheriting
from ``BaseModel``, ``TimestampMixin``, and ``JSONAttributesMixin``.

**Supported Backend Types:**

+----------+-----------------------------------------------------------+
| Type     | Description                                               |
+==========+===========================================================+
| ``file`` | Local filesystem storage (F-201) — atomic writes with     |
|          | temp staging + rename-on-commit, soft-delete support.     |
+----------+-----------------------------------------------------------+
| ``s3``   | Amazon S3 storage (F-202) — SSE encryption, multipart     |
|          | uploads, configurable region and endpoint.                |
+----------+-----------------------------------------------------------+

**Security:**
Both ``to_dict()`` and ``to_dict_secure()`` automatically mask sensitive S3
credentials (``secretAccessKey``, ``accessKeyId``, encryption keys) so that
raw secrets are **never** leaked through API responses or logs.

**Compatibility:**
Designed to work with both SQLite (standalone deployments) and PostgreSQL
(clustered / enterprise deployments) via portable SQLAlchemy types.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, JSON, String

from src.app.extensions import db
from src.app.models.base import BaseModel, JSONAttributesMixin, TimestampMixin

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Valid BlobStore backend type identifiers.
BLOBSTORE_TYPE_FILE: str = "file"
BLOBSTORE_TYPE_S3: str = "s3"

#: Sentinel mask value used when redacting sensitive credential fields.
_CREDENTIAL_MASK: str = "********"

#: Set of top-level configuration keys considered sensitive for basic masking
#: (used by ``to_dict``).  These are replaced with ``_CREDENTIAL_MASK``.
_BASIC_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "secretAccessKey",
})

#: Set of top-level configuration keys considered sensitive for secure masking
#: (used by ``to_dict_secure``).  A superset of ``_BASIC_SENSITIVE_KEYS``.
_SECURE_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "secretAccessKey",
    "accessKeyId",
})

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = ["BlobStoreConfig"]


# ===========================================================================
# BlobStoreConfig Model
# ===========================================================================


class BlobStoreConfig(BaseModel, TimestampMixin, JSONAttributesMixin):
    """SQLAlchemy model representing a BlobStore backend configuration.

    Each row defines a named BlobStore instance with a specific backend type
    (``file`` or ``s3``) and its type-dependent configuration payload.

    The ``blob_store_name`` serves as the natural primary key and is
    referenced by ``Repository.blob_store_name`` to associate repositories
    with their storage backend.

    Attributes:
        blob_store_name: Unique identifier for this BlobStore (e.g.
            ``'default'``, ``'s3-production'``, ``'local-archive'``).
        type: Backend type — ``'file'`` for local filesystem (F-201) or
            ``'s3'`` for Amazon S3 (F-202).
        configuration: JSON payload with backend-specific settings.
        total_size: Total storage consumed in bytes (petabyte-safe).
        blob_count: Total number of blobs stored.
        available_space: Available space in bytes (filesystem free space for
            ``file`` type; typically ``None`` for ``s3``).
        attributes: Extensible JSON metadata column (from
            ``JSONAttributesMixin``).
        created_at: UTC timestamp of record creation (from
            ``TimestampMixin``).
        updated_at: UTC timestamp of last modification (from
            ``TimestampMixin``).

    Configuration JSON Structures:

        **File BlobStore (type='file'):**

        .. code-block:: json

            {
                "path": "/nexus-data/blobs/default",
                "maxSize": null
            }

        **S3 BlobStore (type='s3'):**

        .. code-block:: json

            {
                "bucket": "nexus-blobs-production",
                "region": "us-east-1",
                "prefix": "nexus/",
                "accessKeyId": "AKIA...",
                "secretAccessKey": "...",
                "endpoint": null,
                "encryption": {
                    "type": "s3ManagedEncryption",
                    "key": null
                },
                "forcePathStyle": false
            }
    """

    __tablename__: str = "blobstore_configs"

    # -- Table arguments (indexes) -------------------------------------------

    __table_args__ = (
        db.Index("ix_blobstore_configs_type", "type"),
    )

    # -- Columns -------------------------------------------------------------

    blob_store_name: str = Column(
        String(200),
        primary_key=True,
        doc="Unique BlobStore identifier (natural key).",
    )

    type: str = Column(
        String(20),
        nullable=False,
        doc=(
            "Backend type: 'file' for local filesystem (F-201), "
            "'s3' for Amazon S3 (F-202)."
        ),
    )

    configuration: dict = Column(
        JSON,
        nullable=False,
        doc=(
            "Backend-specific configuration payload (JSON). "
            "Structure depends on `type`."
        ),
    )

    total_size: int = Column(
        BigInteger,
        nullable=True,
        default=0,
        server_default="0",
        doc="Total storage consumed in bytes. BigInteger for petabyte-scale.",
    )

    blob_count: int = Column(
        BigInteger,
        nullable=True,
        default=0,
        server_default="0",
        doc="Total number of blobs stored in this BlobStore.",
    )

    available_space: Optional[int] = Column(
        BigInteger,
        nullable=True,
        doc=(
            "Available space in bytes. For 'file' type: filesystem free "
            "space. For 's3' type: typically None (unlimited)."
        ),
    )

    # -- Representation ------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Example::

            <BlobStoreConfig default (file)>
        """
        return f"<BlobStoreConfig {self.blob_store_name} ({self.type})>"

    # -- Type-check properties -----------------------------------------------

    @property
    def is_file(self) -> bool:
        """Return ``True`` if this BlobStore uses the local filesystem backend.

        Corresponds to Feature F-201 (File BlobStore).
        """
        return self.type == BLOBSTORE_TYPE_FILE

    @property
    def is_s3(self) -> bool:
        """Return ``True`` if this BlobStore uses the Amazon S3 backend.

        Corresponds to Feature F-202 (S3 BlobStore).
        """
        return self.type == BLOBSTORE_TYPE_S3

    # -- Configuration accessor properties -----------------------------------

    @property
    def storage_path(self) -> Optional[str]:
        """Extract the filesystem path from a ``file``-type configuration.

        Returns:
            The ``path`` value from the configuration JSON for ``file`` type
            BlobStores, or ``None`` if the type is not ``file`` or the key
            is absent.
        """
        if not self.is_file:
            return None
        if self.configuration is None:
            return None
        return self.configuration.get("path")

    @property
    def s3_bucket(self) -> Optional[str]:
        """Extract the S3 bucket name from an ``s3``-type configuration.

        Returns:
            The ``bucket`` value from the configuration JSON for ``s3`` type
            BlobStores, or ``None`` if the type is not ``s3`` or the key
            is absent.
        """
        if not self.is_s3:
            return None
        if self.configuration is None:
            return None
        return self.configuration.get("bucket")

    @property
    def s3_region(self) -> Optional[str]:
        """Extract the AWS region from an ``s3``-type configuration.

        Returns:
            The ``region`` value from the configuration JSON for ``s3`` type
            BlobStores, or ``None`` if the type is not ``s3`` or the key
            is absent.
        """
        if not self.is_s3:
            return None
        if self.configuration is None:
            return None
        return self.configuration.get("region")

    # -- Serialization methods -----------------------------------------------

    @staticmethod
    def _mask_config(
        config: Optional[dict],
        sensitive_keys: frozenset[str],
    ) -> Optional[dict]:
        """Create a deep copy of *config* with sensitive keys masked.

        This helper is shared by both ``to_dict()`` and ``to_dict_secure()``
        to ensure that credential values are **never** leaked through API
        responses or logging output.

        Args:
            config: The raw configuration dictionary (may be ``None``).
            sensitive_keys: Set of top-level key names whose values should
                be replaced with ``_CREDENTIAL_MASK``.

        Returns:
            A deep-copied dictionary with sensitive values replaced, or
            ``None`` if *config* was ``None``.
        """
        if config is None:
            return None

        masked: dict = copy.deepcopy(config)

        # Mask top-level sensitive keys
        for key in sensitive_keys:
            if key in masked:
                masked[key] = _CREDENTIAL_MASK

        # Mask nested encryption key if present (S3 SSE configuration)
        encryption: Any = masked.get("encryption")
        if isinstance(encryption, dict) and encryption.get("key") is not None:
            encryption["key"] = _CREDENTIAL_MASK

        return masked

    def to_dict(self) -> dict[str, Any]:
        """Serialize this model instance to a dictionary with basic credential masking.

        Overrides :meth:`BaseModel.to_dict` to ensure that S3
        ``secretAccessKey`` values are **never** exposed in the output.
        For ``file``-type BlobStores, the configuration is returned as-is
        (no sensitive fields to mask).

        The ``configuration`` value in the returned dict is a deep copy — the
        original model attribute is never mutated.

        Returns:
            A dictionary suitable for JSON serialization, with
            ``secretAccessKey`` masked for S3 configurations.
        """
        result: dict[str, Any] = super().to_dict()

        # Replace the raw configuration with a masked copy
        if self.is_s3 and result.get("configuration"):
            result["configuration"] = self._mask_config(
                result["configuration"],
                _BASIC_SENSITIVE_KEYS,
            )

        return result

    def to_dict_secure(self) -> dict[str, Any]:
        """Serialize this model instance with aggressive credential masking.

        Similar to :meth:`to_dict`, but masks a broader set of sensitive
        fields including ``accessKeyId``, ``secretAccessKey``, and any
        nested encryption keys.  This variant is intended for audit logs,
        support bundles, and any context where maximal credential redaction
        is required.

        Returns:
            A dictionary suitable for JSON serialization, with all
            credential-related fields masked for S3 configurations.
        """
        result: dict[str, Any] = super().to_dict()

        # Apply comprehensive masking for S3 configurations
        if self.is_s3 and result.get("configuration"):
            result["configuration"] = self._mask_config(
                result["configuration"],
                _SECURE_SENSITIVE_KEYS,
            )

        return result

    # -- Statistics update ---------------------------------------------------

    def update_stats(
        self,
        total_size: int,
        blob_count: int,
        available_space: Optional[int] = None,
    ) -> None:
        """Update the storage statistics for this BlobStore.

        Persists the new values to the database immediately.  This method
        is typically called by the BlobStore maintenance tasks (Feature
        F-203) after a compaction, integrity check, or periodic statistics
        refresh.

        Args:
            total_size: New total storage consumed in bytes.
            blob_count: New total number of blobs stored.
            available_space: New available space in bytes.  Pass ``None``
                to leave the current value unchanged (common for S3
                BlobStores where available space is effectively unlimited).
        """
        self.total_size = total_size
        self.blob_count = blob_count
        if available_space is not None:
            self.available_space = available_space

        db.session.add(self)
        db.session.commit()

        logger.info(
            "Updated stats for BlobStore '%s': total_size=%d, "
            "blob_count=%d, available_space=%s",
            self.blob_store_name,
            total_size,
            blob_count,
            available_space if available_space is not None else "unchanged",
        )


logger.debug("BlobStoreConfig model loaded (table=%s).", BlobStoreConfig.__tablename__)
