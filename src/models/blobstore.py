"""
BlobStore configuration model for the Binary Repository Management System.

Defines the ``BlobStore`` entity representing a storage backend configuration
for binary artifacts. Supports two storage types:

- **file** — Local file-system-based blob storage with a configurable path
- **s3** — S3-compatible object storage with bucket, prefix, region, and
  credentials

The model stores configuration metadata only; actual storage I/O is handled
by the storage service layer (``src/services/storage_service.py``).

Features covered:
- F-201 File BlobStore
- F-202 S3 BlobStore

Usage::

    from src.models.blobstore import BlobStore

    file_store = BlobStore(name="default", type="file", path="/data/blobs")
    s3_store = BlobStore(
        name="cloud", type="s3",
        bucket_name="my-bucket", prefix="artifacts/", region="us-east-1",
    )
"""

import datetime
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import event
from sqlalchemy.orm import validates

from src.extensions import db

# ---------------------------------------------------------------------------
# Valid BlobStore types — used for model-level validation
# ---------------------------------------------------------------------------
VALID_BLOBSTORE_TYPES = {"file", "s3"}
"""Set of accepted BlobStore type identifiers."""


class BlobStore(db.Model):
    """BlobStore configuration entity.

    Represents the configuration for a binary storage backend. Each instance
    describes *where* and *how* artifacts are physically stored, without
    containing the artifacts themselves.

    Attributes
    ----------
    id : str
        UUID primary key (36-character string).
    name : str
        Unique human-readable name for this BlobStore (max 255 chars).
    type : str
        Storage backend type — ``'file'`` or ``'s3'``.
    path : str or None
        File-system path for ``file`` type stores.
    bucket_name : str or None
        S3 bucket name for ``s3`` type stores.
    prefix : str or None
        Object key prefix within the S3 bucket.
    region : str or None
        AWS region identifier for ``s3`` type stores.
    endpoint_url : str or None
        Custom S3 endpoint URL (for S3-compatible services).
    access_key_id : str or None
        AWS access key ID for ``s3`` authentication.
    secret_access_key : str or None
        AWS secret access key for ``s3`` authentication (excluded from
        serialisation for security).
    soft_quota : int or None
        Optional soft storage quota in bytes.
    created_at : datetime.datetime
        Timestamp when the BlobStore was created.
    updated_at : datetime.datetime
        Timestamp of the last configuration update.
    """

    __tablename__ = "blobstores"

    id: str = db.Column(
        db.String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        doc="UUID primary key",
    )
    name: str = db.Column(
        db.String(255),
        unique=True,
        nullable=False,
        index=True,
        doc="Unique human-readable BlobStore name",
    )
    type: str = db.Column(
        db.String(50),
        nullable=False,
        doc="Storage backend type (file or s3)",
    )

    # -- File BlobStore fields -----------------------------------------------
    path: Optional[str] = db.Column(
        db.String(1024),
        nullable=True,
        doc="File-system path for file-type stores",
    )

    # -- S3 BlobStore fields -------------------------------------------------
    bucket_name: Optional[str] = db.Column(
        db.String(255),
        nullable=True,
        doc="S3 bucket name",
    )
    prefix: Optional[str] = db.Column(
        db.String(1024),
        nullable=True,
        doc="S3 object key prefix",
    )
    region: Optional[str] = db.Column(
        db.String(100),
        nullable=True,
        doc="AWS region identifier",
    )
    endpoint_url: Optional[str] = db.Column(
        db.String(1024),
        nullable=True,
        doc="Custom S3-compatible endpoint URL",
    )
    access_key_id: Optional[str] = db.Column(
        db.String(255),
        nullable=True,
        doc="AWS access key ID",
    )
    secret_access_key: Optional[str] = db.Column(
        db.String(255),
        nullable=True,
        doc="AWS secret access key (never serialised)",
    )

    # -- Common optional fields -----------------------------------------------
    soft_quota: Optional[int] = db.Column(
        db.BigInteger,
        nullable=True,
        doc="Soft storage quota in bytes",
    )

    # -- Timestamps -----------------------------------------------------------
    created_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp when the BlobStore was created",
    )
    updated_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp of the last configuration update",
    )

    # -- Validation -----------------------------------------------------------

    @validates("type")
    def validate_type(self, _key: str, value: str) -> str:
        """Ensure ``type`` is one of the accepted storage backend types.

        Parameters
        ----------
        _key : str
            Column name (always ``'type'``).
        value : str
            The proposed type value.

        Returns
        -------
        str
            The validated type value.

        Raises
        ------
        ValueError
            If *value* is not in :data:`VALID_BLOBSTORE_TYPES`.
        """
        if value not in VALID_BLOBSTORE_TYPES:
            raise ValueError(
                f"Invalid BlobStore type: {value!r}. "
                f"Must be one of {sorted(VALID_BLOBSTORE_TYPES)}."
            )
        return value

    # -- Serialisation --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the BlobStore to a dictionary for API responses.

        Sensitive fields (``secret_access_key``) are **excluded** to
        prevent credential leakage in API responses and logs.

        Returns
        -------
        dict
            Dictionary representation of the BlobStore configuration.
        """
        result: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "soft_quota": self.soft_quota,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

        if self.type == "file":
            result["path"] = self.path
        elif self.type == "s3":
            result["bucket_name"] = self.bucket_name
            result["prefix"] = self.prefix
            result["region"] = self.region
            result["endpoint_url"] = self.endpoint_url
            result["access_key_id"] = self.access_key_id
            # secret_access_key deliberately excluded for security

        return result

    # -- Representation -------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return (
            f"<BlobStore id={self.id!r} name={self.name!r} type={self.type!r}>"
        )
