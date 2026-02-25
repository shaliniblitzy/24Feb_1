"""
User model for the Binary Repository Management System.

Defines the ``User`` entity representing authenticated system users with
support for multiple authentication methods (username/password, JWT bearer
token, API key) and role-based access control (RBAC).

The model supports the 3-tier RBAC structure:
- Global roles (admin, developer, readonly)
- Repository-specific permissions
- Content selectors for fine-grained access

Usage::

    from src.models.user import User

    user = User(
        username="admin",
        email="admin@example.com",
        role="admin",
        is_admin=True,
    )
"""

import datetime
import uuid
from typing import Optional

from src.extensions import db


class User(db.Model):
    """User entity representing authenticated system users.

    Attributes
    ----------
    id : str
        UUID primary key (36-character string).
    username : str
        Unique username for authentication (max 255 chars).
    email : str or None
        Email address (unique, nullable for anonymous users).
    first_name : str or None
        User's first name.
    last_name : str or None
        User's last name.
    password_hash : str or None
        Hashed password string (nullable for API-key-only users).
    role : str
        Global role identifier (admin, developer, readonly, anonymous).
    status : str
        Account status (active, inactive, locked).
    is_admin : bool
        Whether the user has administrative privileges.
    api_key : str or None
        API key for key-based authentication (unique, nullable).
    created_at : datetime
        Timestamp when the user was created.
    updated_at : datetime
        Timestamp of the last update.
    last_login : datetime or None
        Timestamp of the last successful login.
    """

    __tablename__ = "users"

    id: str = db.Column(
        db.String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        doc="UUID primary key",
    )
    username: str = db.Column(
        db.String(255),
        unique=True,
        nullable=False,
        index=True,
        doc="Unique username for authentication",
    )
    email: Optional[str] = db.Column(
        db.String(255),
        unique=True,
        nullable=True,
        index=True,
        doc="Email address (unique, nullable for anonymous users)",
    )
    first_name: Optional[str] = db.Column(
        db.String(255),
        nullable=True,
        doc="User's first name",
    )
    last_name: Optional[str] = db.Column(
        db.String(255),
        nullable=True,
        doc="User's last name",
    )
    password_hash: Optional[str] = db.Column(
        db.String(255),
        nullable=True,
        doc="Hashed password string",
    )
    role: str = db.Column(
        db.String(50),
        nullable=False,
        default="developer",
        index=True,
        doc="Global role identifier (admin, developer, readonly, anonymous)",
    )
    status: str = db.Column(
        db.String(50),
        nullable=False,
        default="active",
        doc="Account status (active, inactive, locked)",
    )
    is_admin: bool = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
        doc="Whether the user has administrative privileges",
    )
    api_key: Optional[str] = db.Column(
        db.String(255),
        unique=True,
        nullable=True,
        doc="API key for key-based authentication",
    )
    created_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp when the user was created",
    )
    updated_at: datetime.datetime = db.Column(
        db.DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow,
        nullable=False,
        doc="Timestamp of the last update",
    )
    last_login: Optional[datetime.datetime] = db.Column(
        db.DateTime,
        nullable=True,
        doc="Timestamp of the last successful login",
    )

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return f"<User id={self.id!r} username={self.username!r} role={self.role!r}>"

    def to_dict(self) -> dict:
        """Serialize the user to a dictionary for API responses.

        Returns
        -------
        dict
            Dictionary representation of the user excluding sensitive fields
            (password_hash, api_key).
        """
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "role": self.role,
            "status": self.status,
            "is_admin": self.is_admin,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }

    @property
    def is_active(self) -> bool:
        """Check if the user account is active.

        Returns
        -------
        bool
            True if the user status is 'active', False otherwise.
        """
        return self.status == "active"

    @property
    def full_name(self) -> str:
        """Return the user's full name.

        Returns
        -------
        str
            Concatenated first and last name, or username if names are absent.
        """
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) if parts else self.username
