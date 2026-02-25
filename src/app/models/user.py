"""
User SQLAlchemy Model for Sonatype Nexus Repository (Python/Flask).

This module defines the ``User`` model that represents system users in the
Nexus Repository Manager.  It replaces the ``USER`` entity from the Java
DataStore schema (Section 6.2.1.2) with a SQLAlchemy ORM model.

**Authentication Backends Supported:**

+----------------------------+-----------------------------------------------+
| Backend                    | How User Model Is Used                        |
+============================+===============================================+
| Local credentials          | ``password_hash`` verified via                |
|                            | ``check_password()``                          |
+----------------------------+-----------------------------------------------+
| API key (F-304)            | ``api_key`` matched by BearerTokenRealm       |
+----------------------------+-----------------------------------------------+
| JWT tokens                 | User loaded after JWT validation              |
+----------------------------+-----------------------------------------------+
| LDAP/AD                    | ``external_id`` stores Distinguished Name     |
+----------------------------+-----------------------------------------------+
| SAML/OIDC SSO              | ``external_id`` stores NameID / subject       |
+----------------------------+-----------------------------------------------+

**Feature Dependencies:**

- **F-301** — Role-Based Access Control (many-to-many with ``Role`` via
  ``role_assignments`` junction table)
- **F-303** — Audit Logging (``SoftDeleteMixin`` preserves user records for
  audit trail integrity)
- **F-304** — API Key Authentication (``api_key`` unique column)

**Architecture Context:**

- Replaces ``MyBatis 3.5.15`` USER entity mapper from the Java source.
- Password hashing uses ``src.app.auth.password_utils`` (bcrypt/scrypt),
  replacing BouncyCastle 1.78.1 from the Java source.
- Supports both SQLite (standalone) and PostgreSQL (clustered) deployments
  via portable SQLAlchemy column types only.
- Integrates with the multi-backend authentication chain described in AAP
  Section 0.7.1: local → API key → JWT → LDAP → SSO.

**Security Invariants:**

- ``password_hash`` and ``api_key`` are **NEVER** included in ``to_dict()``
  output or any serialised API response.
- Failed-login tracking with automatic account locking after a configurable
  threshold protects against brute-force attacks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.orm import relationship

from src.app.extensions import db
from src.app.models.base import (
    BaseModel,
    JSONAttributesMixin,
    SoftDeleteMixin,
    TimestampMixin,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES: tuple[str, ...] = ("active", "disabled", "locked")
"""Valid values for the ``status`` column.  Any other value is a data error."""

DEFAULT_STATUS: str = "active"
"""Default status assigned to newly created users."""

MAX_FAILED_LOGIN_ATTEMPTS: int = 5
"""Number of consecutive failed login attempts before an active account is
automatically locked.  This threshold can be overridden via system
configuration at runtime (F-404), but the model default is enforced here."""

SENSITIVE_FIELDS: frozenset[str] = frozenset({"password_hash", "api_key"})
"""Column names that must **never** appear in serialised (to_dict) output."""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = ["User"]


# ===========================================================================
# User Model
# ===========================================================================


class User(BaseModel, TimestampMixin, SoftDeleteMixin, JSONAttributesMixin):
    """SQLAlchemy model representing a system user.

    Each user has a unique ``user_id`` (the login name), optional local
    credentials (``password_hash``), optional programmatic access credentials
    (``api_key``), and optional external identity provider bindings
    (``external_id``).

    The many-to-many relationship with :class:`Role` (via the
    ``role_assignments`` junction table) implements Feature F-301
    (Role-Based Access Control).

    Inherits from:
        - :class:`BaseModel` — ``to_dict()``, ``save()``, ``delete()``,
          ``update()`` convenience methods.
        - :class:`TimestampMixin` — ``created_at``, ``updated_at`` columns.
        - :class:`SoftDeleteMixin` — ``is_deleted``, ``deleted_at`` columns,
          ``soft_delete()`` and ``restore()`` methods.
        - :class:`JSONAttributesMixin` — ``attributes`` JSON column,
          ``get_attribute()``, ``set_attribute()`` helpers.
    """

    __tablename__: str = "users"

    # ------------------------------------------------------------------
    # Primary Key
    # ------------------------------------------------------------------

    user_id: str = Column(
        String(200),
        primary_key=True,
        doc=(
            "Unique username / login identifier.  Examples: 'admin', "
            "'deploy-user', 'john.doe'."
        ),
    )

    # ------------------------------------------------------------------
    # Authentication Columns
    # ------------------------------------------------------------------

    password_hash: Optional[str] = Column(
        String(512),
        nullable=True,
        doc=(
            "Hashed password for local authentication.  Null for users that "
            "authenticate exclusively via external providers (LDAP/SSO).  "
            "Hashing performed by Werkzeug (replacing BouncyCastle 1.78.1)."
        ),
    )

    api_key: Optional[str] = Column(
        String(255),
        nullable=True,
        unique=True,
        doc=(
            "API key for programmatic access (Feature F-304).  Generated on "
            "demand and used by BearerTokenRealm for authentication."
        ),
    )

    external_id: Optional[str] = Column(
        String(500),
        nullable=True,
        doc=(
            "External identity provider identifier.  For LDAP users this is "
            "the Distinguished Name (DN); for SSO users this is the SAML "
            "NameID or OIDC subject claim."
        ),
    )

    # ------------------------------------------------------------------
    # Account Status
    # ------------------------------------------------------------------

    status: str = Column(
        String(20),
        nullable=False,
        default=DEFAULT_STATUS,
        server_default="active",
        doc=(
            "Account status.  Valid values: 'active', 'disabled', 'locked'.  "
            "'locked' results from exceeding the failed-login threshold."
        ),
    )

    # ------------------------------------------------------------------
    # Profile Columns
    # ------------------------------------------------------------------

    email: Optional[str] = Column(
        String(255),
        nullable=True,
        doc="User email address.",
    )

    first_name: Optional[str] = Column(
        String(100),
        nullable=True,
        doc="User first name.",
    )

    last_name: Optional[str] = Column(
        String(100),
        nullable=True,
        doc="User last name.",
    )

    # ------------------------------------------------------------------
    # Login Tracking
    # ------------------------------------------------------------------

    last_login: Optional[datetime] = Column(
        DateTime,
        nullable=True,
        doc="UTC timestamp of the most recent successful login.",
    )

    failed_login_count: int = Column(
        Integer,
        nullable=True,
        default=0,
        server_default="0",
        doc=(
            "Counter of consecutive failed login attempts.  Reset to 0 on "
            "successful login.  When this reaches MAX_FAILED_LOGIN_ATTEMPTS "
            "the account is automatically locked."
        ),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    roles = db.relationship(
        "Role",
        secondary="role_assignments",
        back_populates="users",
        lazy="dynamic",
    )
    """Many-to-many relationship to :class:`Role` via the
    ``role_assignments`` junction table (Feature F-301 RBAC)."""

    # ------------------------------------------------------------------
    # Table-Level Indexes
    # ------------------------------------------------------------------

    __table_args__ = (
        db.Index("ix_users_email", "email"),
        db.Index("ix_users_status", "status"),
        db.Index("ix_users_external_id", "external_id"),
        db.Index("ix_users_api_key", "api_key", unique=True),
    )

    # ==================================================================
    # Representation
    # ==================================================================

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return f"<User {self.user_id} ({self.status})>"

    # ==================================================================
    # Properties
    # ==================================================================

    @property
    def is_active(self) -> bool:
        """Return ``True`` if the account status is ``'active'``.

        .. note::
            This intentionally **overrides** the ``is_active`` property from
            :class:`SoftDeleteMixin` (which checks ``not self.is_deleted``).
            For the User model, ``is_active`` reflects the *account status*
            rather than the soft-delete flag.
        """
        return self.status == "active"

    @property
    def is_locked(self) -> bool:
        """Return ``True`` if the account has been locked.

        Accounts are locked either manually by an administrator or
        automatically after ``MAX_FAILED_LOGIN_ATTEMPTS`` consecutive
        failed login attempts.
        """
        return self.status == "locked"

    @property
    def is_external(self) -> bool:
        """Return ``True`` if this user is bound to an external identity
        provider (LDAP/AD or SAML/OIDC SSO).
        """
        return self.external_id is not None

    @property
    def full_name(self) -> str:
        """Return the user's full name by joining first and last names.

        Returns an empty string if neither name is set.
        """
        parts: list[str] = []
        if self.first_name:
            parts.append(self.first_name)
        if self.last_name:
            parts.append(self.last_name)
        return " ".join(parts)

    @property
    def role_names(self) -> List[str]:
        """Return the list of ``role_id`` values from the assigned roles.

        Evaluates the dynamic ``roles`` relationship lazily.
        """
        try:
            return [role.role_id for role in self.roles]
        except Exception:
            # Gracefully handle detached session or unloaded relationship
            logger.debug(
                "Could not load role_names for user '%s'; returning empty list.",
                self.user_id,
            )
            return []

    # ==================================================================
    # Serialisation Methods
    # ==================================================================

    def to_dict(self) -> dict[str, Any]:
        """Convert the user to a dictionary, **excluding sensitive fields**.

        Overrides :meth:`BaseModel.to_dict` to strip ``password_hash`` and
        ``api_key`` from the output.  This ensures that secrets are never
        accidentally leaked through API responses or log messages.

        Returns:
            A dictionary of column values (datetime values as ISO-8601
            strings).  Sensitive columns are omitted.
        """
        result: dict[str, Any] = super().to_dict()
        for field in SENSITIVE_FIELDS:
            result.pop(field, None)
        return result

    def to_dict_secure(self) -> dict[str, Any]:
        """Return a minimal, safe representation suitable for API responses.

        Includes only non-sensitive scalar fields plus computed properties.
        Never includes ``password_hash``, ``api_key``, or ``external_id``.

        Returns:
            A dictionary with safe user fields and computed properties.
        """
        return {
            "user_id": self.user_id,
            "status": self.status,
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": self.full_name,
            "is_active": self.is_active,
            "is_locked": self.is_locked,
            "is_external": self.is_external,
            "role_names": self.role_names,
            "last_login": (
                self.last_login.isoformat() if self.last_login else None
            ),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }

    # ==================================================================
    # Password Management
    # ==================================================================

    def set_password(self, password: str) -> None:
        """Hash *password* and store in ``password_hash``.

        Uses ``src.app.auth.password_utils.hash_password()`` which provides
        bcrypt (primary) or scrypt (fallback) hashing, replacing BouncyCastle
        1.78.1 from the Java source.  Password strength is validated before
        hashing to prevent weak passwords from being set programmatically.

        Args:
            password: The plaintext password to hash.

        Raises:
            ValueError: If *password* is empty, ``None``, or fails strength
                validation.
        """
        if not password:
            raise ValueError("Password cannot be empty or None.")

        # Import here to avoid circular imports at module load time.
        from src.app.auth.password_utils import (
            hash_password,
            validate_password_strength,
        )

        # Validate password strength before hashing (defence-in-depth).
        # validate_password_strength returns (valid: bool, errors: list[str]).
        # Raises ValueError with descriptive messages if the password is
        # too weak, allowing callers to surface the error to users.
        is_valid, errors = validate_password_strength(password)
        if not is_valid:
            raise ValueError(
                "Password does not meet strength requirements: "
                + "; ".join(errors)
            )

        self.password_hash = hash_password(password)
        logger.info("Password updated for user '%s'.", self.user_id)

    def check_password(self, password: str) -> bool:
        """Verify *password* against the stored ``password_hash``.

        Uses ``src.app.auth.password_utils.verify_password()`` which
        auto-detects the hashing algorithm (bcrypt or scrypt) from the
        stored hash format and uses constant-time comparison.

        Returns ``False`` (rather than raising) when:
        - The user has no local password set (external-only user).
        - The supplied password does not match the hash.

        Args:
            password: The plaintext password to verify.

        Returns:
            ``True`` if the password matches; ``False`` otherwise.
        """
        if self.password_hash is None:
            logger.warning(
                "Password check failed for user '%s': no local password set "
                "(external user).",
                self.user_id,
            )
            return False
        if not password:
            return False

        # Import here to avoid circular imports at module load time.
        from src.app.auth.password_utils import verify_password

        return verify_password(password, self.password_hash)

    # ==================================================================
    # Login Tracking
    # ==================================================================

    def record_login(self) -> None:
        """Record a successful authentication event.

        Updates ``last_login`` to the current UTC time and resets
        ``failed_login_count`` to zero.  Commits the session.
        """
        self.last_login = datetime.now(timezone.utc)
        self.failed_login_count = 0
        db.session.add(self)
        db.session.commit()
        logger.info(
            "Successful login recorded for user '%s' at %s.",
            self.user_id,
            self.last_login.isoformat(),
        )

    def record_failed_login(self) -> None:
        """Record a failed authentication attempt.

        Increments ``failed_login_count`` by one.  If the counter reaches
        ``MAX_FAILED_LOGIN_ATTEMPTS`` **and** the account is currently
        ``'active'``, the status is automatically set to ``'locked'``.

        Commits the session after recording.
        """
        self.failed_login_count = (self.failed_login_count or 0) + 1

        if (
            self.failed_login_count >= MAX_FAILED_LOGIN_ATTEMPTS
            and self.status == "active"
        ):
            self.status = "locked"
            logger.warning(
                "User '%s' locked after %d consecutive failed login attempts.",
                self.user_id,
                self.failed_login_count,
            )

        db.session.add(self)
        db.session.commit()
        logger.debug(
            "Failed login recorded for user '%s' (count: %d).",
            self.user_id,
            self.failed_login_count,
        )


# ---------------------------------------------------------------------------
# Module Load Logging
# ---------------------------------------------------------------------------

logger.debug(
    "User model module loaded — exports: %s",
    ", ".join(__all__),
)
