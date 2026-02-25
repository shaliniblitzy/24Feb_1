"""
Asset model for the Binary Repository Management System.

Defines the ``Asset`` entity (also referred to as Component) representing a
binary artifact stored in the repository management system. Assets are the
fundamental storage units — each represents a single file (JAR, tarball,
Docker layer, NuGet package, wheel, etc.) with associated metadata, integrity
checksums, and a reference to the BlobStore backend where the physical bytes
reside.

The model tracks:

- **Identity**: name, path within the repository tree, content type, format
- **Integrity**: SHA-1, SHA-256, and MD5 checksums for verification
- **Storage**: size in bytes and a BlobStore reference for physical location
- **Relationships**: belongs to a Repository via ``repository_id`` foreign key

Usage::

    from src.models.asset import Asset

    asset = Asset(
        name="my-library-1.0.0.jar",
        path="/com/example/my-library/1.0.0/my-library-1.0.0.jar",
        format="maven",
        content_type="application/java-archive",
        size=102400,
        sha1="da39a3ee5e6b4b0d3255bfef95601890afd80709",
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        md5="d41d8cd98f00b204e9800998ecf8427e",
        blob_ref="default",
    )
"""

import datetime
import uuid
from typing import Any, Dict, Optional

from sqlalchemy.orm import validates

from src.extensions import db


class Asset(db.Model):
    """Asset (Component) entity representing a stored binary artifact.

    Attributes
    ----------
    id : str
        UUID primary key (36-character string).
    name : str
        Filename of the artifact (max 255 chars, NOT NULL).
    path : str
        Full path within the repository tree (max 2048 chars, NOT NULL).
    format : str or None
        Repository format identifier (maven, npm, docker, etc.).
    content_type : str or None
        MIME content type of the artifact.
    size : int
        Size of the artifact in bytes (default 0, NOT NULL).
    sha1 : str or None
        SHA-1 hex digest (40 characters).
    sha256 : str or None
        SHA-256 hex digest (64 characters).
    md5 : str or None
        MD5 hex digest (32 characters).
    blob_ref : str or None
        Reference name of the BlobStore backend storing the physical bytes.
    repository_id : str or None
        Foreign key referencing the owning repository.
    created_at : datetime.datetime
        Timestamp when the asset was created.
    updated_at : datetime.datetime
        Timestamp of the last modification.
    """

    __tablename__ = "assets"

    id: str = db.Column(
        db.String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        doc="UUID primary key",
    )
    name: str = db.Column(
        db.String(255),
        nullable=False,
        index=True,
        doc="Filename of the artifact",
    )
    path: str = db.Column(
        db.String(2048),
        nullable=False,
        doc="Full path within the repository tree",
    )
    format: Optional[str] = db.Column(
        db.String(50),
        nullable=True,
        doc="Repository format identifier (maven, npm, docker, etc.)",
    )
    content_type: Optional[str] = db.Column(
        db.String(255),
        nullable=True,
        doc="MIME content type of the artifact",
    )
    size: int = db.Column(
        db.BigInteger,
        default=0,
        nullable=False,
        doc="Size of the artifact in bytes",
    )

    # -- Integrity checksums --------------------------------------------------
    sha1: Optional[str] = db.Column(
        db.String(40),
        nullable=True,
        doc="SHA-1 hex digest (40 characters)",
    )
    sha256: Optional[str] = db.Column(
        db.String(64),
        nullable=True,
        doc="SHA-256 hex digest (64 characters)",
    )
    md5: Optional[str] = db.Column(
        db.String(32),
        nullable=True,
        doc="MD5 hex digest (32 characters)",
    )

    # -- BlobStore reference --------------------------------------------------
    blob_ref: Optional[str] = db.Column(
        db.String(255),
        nullable=True,
        doc="Reference name of the BlobStore backend",
    )

    # -- Repository relationship ----------------------------------------------
    repository_id: Optional[str] = db.Column(
        db.String(36),
        db.ForeignKey("repositories.id"),
        nullable=True,
        index=True,
        doc="Foreign key referencing the owning repository",
    )
    repository = db.relationship(
        "Repository",
        back_populates="assets",
    )

    # -- Timestamps -----------------------------------------------------------
    created_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp when the asset was created",
    )
    updated_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp of the last modification",
    )

    # -- Validation -----------------------------------------------------------

    @validates("size")
    def validate_size(self, _key: str, value: Any) -> int:
        """Ensure ``size`` is a non-negative integer.

        Parameters
        ----------
        _key : str
            Column name (always ``'size'``).
        value : Any
            The proposed size value.

        Returns
        -------
        int
            The validated size value.

        Raises
        ------
        ValueError
            If *value* is negative.
        """
        if value is not None and value < 0:
            raise ValueError(
                f"Asset size cannot be negative: {value}"
            )
        return value

    # -- Serialisation --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the asset to a dictionary for API responses.

        Returns
        -------
        dict
            Dictionary representation of the asset including all metadata
            and integrity checksums.
        """
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "format": self.format,
            "content_type": self.content_type,
            "size": self.size,
            "sha1": self.sha1,
            "sha256": self.sha256,
            "md5": self.md5,
            "blob_ref": self.blob_ref,
            "repository_id": self.repository_id,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

    # -- Representation -------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return (
            f"<Asset id={self.id!r} name={self.name!r} "
            f"path={self.path!r} format={self.format!r}>"
        )
