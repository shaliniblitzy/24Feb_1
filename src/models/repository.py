"""
Repository model for the Binary Repository Management System.

Defines the ``Repository`` entity representing a package repository that
stores binary artifacts. Supports three repository types:

- **hosted** — Stores locally-published artifacts
- **proxy** — Caches artifacts fetched from upstream registries
- **group** — Aggregates multiple repositories into a single logical endpoint

And seven repository formats:

- Maven, npm, Docker, NuGet, PyPI, APT, Raw

Features covered:
- F-101 Multi-Format Support
- F-102 Repository Types (Hosted, Proxy, Group)

Usage::

    from src.models.repository import Repository

    repo = Repository(
        name="maven-releases",
        format="maven",
        type="hosted",
        online=True,
    )
"""

import datetime
import uuid
from typing import Any, Dict, List, Optional

from src.extensions import db

# ---------------------------------------------------------------------------
# Valid repository types and formats — used for model-level validation
# ---------------------------------------------------------------------------
VALID_REPOSITORY_TYPES = {"hosted", "proxy", "group"}
"""Set of accepted repository type identifiers."""

VALID_REPOSITORY_FORMATS = {"maven", "npm", "docker", "nuget", "pypi", "apt", "raw"}
"""Set of accepted repository format identifiers."""


class Repository(db.Model):
    """Repository entity representing a package storage endpoint.

    Attributes
    ----------
    id : str
        UUID primary key (36-character string).
    name : str
        Unique human-readable repository name (max 255 chars).
    format : str
        Repository format (maven, npm, docker, nuget, pypi, apt, raw).
    type : str
        Repository type (hosted, proxy, group).
    online : bool
        Whether the repository is accepting requests (default True).
    description : str or None
        Human-readable description of the repository.
    url : str or None
        Base URL for proxy repositories' upstream registry.
    created_at : datetime.datetime
        Timestamp when the repository was created.
    updated_at : datetime.datetime
        Timestamp of the last configuration update.
    """

    __tablename__ = "repositories"

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
        doc="Unique human-readable repository name",
    )
    format: str = db.Column(
        db.String(50),
        nullable=False,
        doc="Repository format (maven, npm, docker, nuget, pypi, apt, raw)",
    )
    type: str = db.Column(
        db.String(50),
        nullable=False,
        doc="Repository type (hosted, proxy, group)",
    )
    online: bool = db.Column(
        db.Boolean,
        default=True,
        nullable=False,
        doc="Whether the repository is accepting requests",
    )
    description: Optional[str] = db.Column(
        db.Text,
        nullable=True,
        doc="Human-readable description of the repository",
    )
    url: Optional[str] = db.Column(
        db.String(2048),
        nullable=True,
        doc="Upstream registry URL for proxy repositories",
    )

    # -- Timestamps -----------------------------------------------------------
    created_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp when the repository was created",
    )
    updated_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp of the last configuration update",
    )

    # -- Relationships --------------------------------------------------------
    assets = db.relationship(
        "Asset",
        back_populates="repository",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    # -- Serialisation --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the repository to a dictionary for API responses.

        Returns
        -------
        dict
            Dictionary representation of the repository.
        """
        return {
            "id": self.id,
            "name": self.name,
            "format": self.format,
            "type": self.type,
            "online": self.online,
            "description": self.description,
            "url": self.url,
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
            f"<Repository id={self.id!r} name={self.name!r} "
            f"format={self.format!r} type={self.type!r}>"
        )
