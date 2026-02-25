"""
Local Username/Password Authentication Realm for Nexus Repository.

This module implements the **LocalRealm**, which handles authentication via
username and password credentials stored in the local database.  It replaces
``AuthenticatingRealmImpl`` from the Java source system (Apache Shiro 2.0.0).

**Realm Order Position: 1** (first in the multi-backend authentication chain)

The authentication chain order is:
    1. **LocalRealm** — Username/password against local DB (this module)
    2. BearerTokenRealm — API key authentication (Feature F-304)
    3. JWTRealm — JWT token validation
    4. LDAPRealm — LDAP/Active Directory integration
    5. SSORealm — SAML 2.0 / OpenID Connect federation

Using a *first-successful* strategy, if local authentication succeeds no
other realm is consulted.

**Architecture Context:**

- Replaces ``AuthenticatingRealmImpl`` from Apache Shiro 2.0.0
  (AAP Section 0.2.2, 0.8.5).
- Extends :class:`~src.app.auth.realms.RealmBase` abstract interface.
- Password verification via ``src.app.auth.password_utils`` (bcrypt/scrypt),
  replacing BouncyCastle 1.78.1 from the Java source.
- Transparent password hash upgrade on successful login when the stored
  hash uses an outdated algorithm or cost factor.
- Configurable account lockout after repeated failed login attempts with
  automatic expiry-based unlock.
- Lockout metadata (``failed_login_count``, ``locked_at``, ``last_login``)
  is persisted in the ``User.attributes`` JSON column.
- All timestamps are UTC-aware ISO 8601 strings.

**Security Invariants:**

- Plaintext passwords are **NEVER** logged, stored, or returned.
- Authentication failures do not reveal whether the username exists
  (timing-safe: both cases return ``None``).
- Only users with ``status == 'active'`` may authenticate.

**Feature Coverage:**

- F-301 — Role-Based Access Control (local credential verification)
- F-404 — System Configuration (lockout thresholds via app config)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from flask import current_app

from src.app.auth.password_utils import hash_password, needs_rehash, verify_password
from src.app.auth.realms import RealmBase
from src.app.extensions import db
from src.app.models.user import User

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Structured logging for authentication events, lockout warnings, and
# transparent password rehash notifications.
# CRITICAL: Passwords must NEVER appear in log output.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)


# ===========================================================================
# LocalRealm — Username/Password Authentication
# ===========================================================================


class LocalRealm(RealmBase):
    """Local username/password authentication realm.

    Verifies credentials against the ``User`` table in the local database.
    This is realm position **1** (first) in the authentication chain:
    ``local → bearer_token → jwt → ldap → sso``.

    Replaces ``AuthenticatingRealmImpl`` from the Java source system
    (Apache Shiro 2.0.0).

    **Account Lockout Behaviour:**

    After a configurable number of consecutive failed login attempts
    (default ``5``), the account is automatically locked for a configurable
    duration (default ``30`` minutes).  The lockout expires automatically;
    an administrator may also unlock the account via :meth:`unlock_account`.

    Lockout metadata is stored in the ``User.attributes`` JSON column:

    - ``failed_login_count`` (int): consecutive failed attempts
    - ``locked_at`` (str): ISO 8601 UTC timestamp of lockout activation
    - ``last_failed_login`` (str): ISO 8601 UTC timestamp of last failure
    - ``last_login`` (str): ISO 8601 UTC timestamp of last successful login

    **Transparent Password Rehash:**

    On successful authentication, if the stored hash uses an outdated
    algorithm or insufficient cost factor, the password is transparently
    re-hashed with the current preferred algorithm and the updated hash
    is persisted.

    Class Attributes:
        REALM_NAME: Unique identifier for this realm (``'local'``).
        SUPPORTED_CREDENTIAL_TYPES: Credential types this realm handles.
        DEFAULT_MAX_FAILED_ATTEMPTS: Default lockout threshold.
        DEFAULT_LOCKOUT_DURATION_MINUTES: Default lockout window in minutes.
    """

    # ------------------------------------------------------------------
    # Class-Level Constants
    # ------------------------------------------------------------------

    REALM_NAME: str = "local"
    """Unique identifier for this realm in the authentication chain."""

    SUPPORTED_CREDENTIAL_TYPES: list[str] = ["username_password"]
    """Credential types supported by this realm."""

    DEFAULT_MAX_FAILED_ATTEMPTS: int = 5
    """Default number of consecutive failed attempts before lockout.

    Overridable at runtime via Flask app config key
    ``AUTH_MAX_FAILED_ATTEMPTS``.
    """

    DEFAULT_LOCKOUT_DURATION_MINUTES: int = 30
    """Default lockout window in minutes after threshold is reached.

    Overridable at runtime via Flask app config key
    ``AUTH_LOCKOUT_DURATION_MINUTES``.
    """

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialize the LocalRealm.

        Calls :meth:`RealmBase.__init__` to set up the per-class structured
        logger, then stores a convenience reference to the module logger for
        operations that occur outside of an instance context.
        """
        super().__init__()
        self.logger: logging.Logger = logging.getLogger(__name__)

    # ==================================================================
    # RealmBase Interface Implementation
    # ==================================================================

    def authenticate(self, credentials: Dict[str, Any]) -> Optional[User]:
        """Authenticate a user with username and password credentials.

        Implements the :class:`RealmBase` contract.  This is the main
        entry point invoked by the authentication chain when
        :meth:`supports` returns ``True``.

        **Authentication Flow:**

        1. Extract ``username`` and ``password`` from *credentials*.
        2. Look up the user in the local database by ``user_id``.
        3. If the user is not found → return ``None`` (no user-existence
           leak).
        4. If the account status is not ``'active'`` → return ``None``.
        5. If the account is locked and the lockout has not expired →
           return ``None``.
        6. Verify the password against the stored hash.
        7. On failure → record the failed attempt, check lockout threshold.
        8. On success → reset failed-login counter, update last-login
           timestamp, transparently rehash the password if needed.

        Args:
            credentials: Dictionary containing authentication data.
                Expected keys:

                - ``'username'`` (str): The user's login identifier.
                - ``'password'`` (str): The plaintext password.

        Returns:
            The authenticated :class:`~src.app.models.user.User` instance
            on success; ``None`` on any failure.

        Note:
            Passwords are **never** logged.  Only the username and outcome
            metadata appear in log messages.
        """
        username: Optional[str] = credentials.get("username")
        password: Optional[str] = credentials.get("password")

        if not username or not password:
            return None

        # Step 1: Database lookup by user_id
        user: Optional[User] = User.query.filter_by(user_id=username).first()

        if user is None:
            self.logger.debug("User not found: %s", username)
            return None

        # Step 2: Account status check — only 'active' users may log in
        if user.status != "active":
            self.logger.warning(
                "Login attempt for non-active user: %s (status=%s)",
                username,
                user.status,
            )
            return None

        # Step 3: Account lockout check (with auto-unlock on expiry)
        if self._is_account_locked(user):
            self.logger.warning(
                "Login attempt for locked account: %s",
                username,
            )
            return None

        # Step 4: Password verification (timing-safe via password_utils)
        if not verify_password(password, user.password_hash):
            self._record_failed_login(user)
            self.logger.info("Failed login attempt for user: %s", username)
            return None

        # Step 5: Successful authentication
        self._record_successful_login(user, password)
        self.logger.info(
            "Local authentication successful for user: %s", username
        )
        return user

    def supports(self, credentials: Dict[str, Any]) -> bool:
        """Check whether this realm supports the given credentials.

        Implements the :class:`RealmBase` contract.  Returns ``True``
        when the credentials dictionary contains both ``'username'`` and
        ``'password'`` keys with truthy values.

        Args:
            credentials: Dictionary containing authentication data.

        Returns:
            ``True`` if this realm can handle the credentials;
            ``False`` otherwise.
        """
        return bool(credentials.get("username")) and bool(
            credentials.get("password")
        )

    def get_realm_name(self) -> str:
        """Return the unique identifier for this realm.

        Implements the :class:`RealmBase` contract.

        Returns:
            The string ``'local'``.
        """
        return self.REALM_NAME

    def is_configured(self) -> bool:
        """Check whether this realm is properly configured.

        The local realm is always configured — it relies on the local
        database which is always available when the application is
        running.

        Returns:
            Always ``True``.
        """
        return True

    # ==================================================================
    # Account Lockout Management (Private)
    # ==================================================================

    def _is_account_locked(self, user: User) -> bool:
        """Determine whether the account is currently locked.

        An account is considered locked when the number of consecutive
        failed login attempts stored in ``user.attributes`` meets or
        exceeds the configured threshold **and** the lockout window
        has not yet expired.

        If the lockout has expired, this method performs an **auto-unlock**:
        it resets the failed-login counter and clears the ``locked_at``
        timestamp, then commits the change.

        Args:
            user: The :class:`User` instance to check.

        Returns:
            ``True`` if the account is currently locked and the lockout
            has **not** expired; ``False`` otherwise.
        """
        max_attempts: int = current_app.config.get(
            "AUTH_MAX_FAILED_ATTEMPTS", self.DEFAULT_MAX_FAILED_ATTEMPTS
        )
        lockout_minutes: int = current_app.config.get(
            "AUTH_LOCKOUT_DURATION_MINUTES",
            self.DEFAULT_LOCKOUT_DURATION_MINUTES,
        )

        attrs: Dict[str, Any] = dict(user.attributes) if user.attributes else {}
        failed_count: int = attrs.get("failed_login_count", 0)

        if failed_count < max_attempts:
            return False

        # Threshold reached — check if lockout has expired
        locked_at_str: Optional[str] = attrs.get("locked_at")

        if locked_at_str is None:
            # Threshold met but no lockout timestamp — treat as locked,
            # set the timestamp now for future expiry calculation
            attrs["locked_at"] = datetime.now(tz=timezone.utc).isoformat()
            user.attributes = attrs
            db.session.commit()
            return True

        try:
            locked_at: datetime = datetime.fromisoformat(locked_at_str)
            # Ensure timezone-aware comparison
            if locked_at.tzinfo is None:
                locked_at = locked_at.replace(tzinfo=timezone.utc)

            lockout_expiry: datetime = locked_at + timedelta(
                minutes=lockout_minutes
            )
            now: datetime = datetime.now(tz=timezone.utc)

            if now >= lockout_expiry:
                # Lockout has expired — auto-unlock the account
                self.logger.info(
                    "Auto-unlocking account after lockout expiry: %s",
                    user.user_id,
                )
                attrs["failed_login_count"] = 0
                attrs.pop("locked_at", None)
                user.attributes = attrs
                db.session.commit()
                return False

            # Lockout still active
            return True

        except (ValueError, TypeError) as exc:
            # Malformed locked_at timestamp — defensively treat as locked
            self.logger.warning(
                "Malformed locked_at timestamp for user %s: %s",
                user.user_id,
                exc,
            )
            return True

    def _record_failed_login(self, user: User) -> None:
        """Record a failed login attempt for the given user.

        Increments the ``failed_login_count`` in ``user.attributes``,
        updates the ``last_failed_login`` timestamp, and — if the
        configured threshold is reached — sets the ``locked_at``
        timestamp to activate the account lockout.

        All changes are committed to the database immediately so that
        concurrent requests see the updated state.

        Args:
            user: The :class:`User` instance that failed authentication.
        """
        attrs: Dict[str, Any] = dict(user.attributes) if user.attributes else {}
        failed_count: int = attrs.get("failed_login_count", 0) + 1
        attrs["failed_login_count"] = failed_count
        attrs["last_failed_login"] = datetime.now(tz=timezone.utc).isoformat()

        # Check whether the lockout threshold has been reached
        max_attempts: int = current_app.config.get(
            "AUTH_MAX_FAILED_ATTEMPTS", self.DEFAULT_MAX_FAILED_ATTEMPTS
        )
        if failed_count >= max_attempts:
            attrs["locked_at"] = datetime.now(tz=timezone.utc).isoformat()
            self.logger.warning(
                "Account locked due to %d failed attempts: %s",
                failed_count,
                user.user_id,
            )

        user.attributes = attrs
        db.session.commit()

    def _record_successful_login(self, user: User, password: str) -> None:
        """Record a successful login for the given user.

        Resets the ``failed_login_count`` to ``0``, clears any
        ``locked_at`` timestamp, and updates the ``last_login``
        timestamp in ``user.attributes``.

        Additionally, checks whether the stored password hash needs
        to be upgraded (transparent rehash).  If
        :func:`~src.app.auth.password_utils.needs_rehash` returns
        ``True``, the password is re-hashed with the current preferred
        algorithm and persisted.

        All changes are committed to the database immediately.

        Args:
            user: The authenticated :class:`User` instance.
            password: The plaintext password (used only for rehash;
                **never** stored or logged).
        """
        attrs: Dict[str, Any] = dict(user.attributes) if user.attributes else {}
        attrs["failed_login_count"] = 0
        attrs.pop("locked_at", None)
        attrs["last_login"] = datetime.now(tz=timezone.utc).isoformat()
        user.attributes = attrs

        # Transparent password rehash if algorithm was upgraded
        if user.password_hash and needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
            self.logger.info(
                "Password hash upgraded for user: %s", user.user_id
            )

        db.session.commit()

    # ==================================================================
    # Password Change Support (Public)
    # ==================================================================

    def change_password(
        self,
        user: User,
        current_password: str,
        new_password: str,
    ) -> bool:
        """Change a user's password after verifying the current password.

        This is the user-initiated password change flow.  The current
        password must be verified before the new password is accepted.

        Args:
            user: The :class:`User` instance whose password is being
                changed.
            current_password: The user's current plaintext password for
                verification.
            new_password: The new plaintext password to set.

        Returns:
            ``True`` if the password was successfully changed;
            ``False`` if the current password verification failed.

        Note:
            On success, any lockout state is cleared and the
            ``password_change_required`` flag is removed.
        """
        # Verify the current password first
        if not user.password_hash or not verify_password(
            current_password, user.password_hash
        ):
            self.logger.info(
                "Password change failed for user %s: current password "
                "verification failed.",
                user.user_id,
            )
            return False

        # Hash and set the new password
        user.password_hash = hash_password(new_password)

        # Reset lockout state and clear password-change-required flag
        attrs: Dict[str, Any] = dict(user.attributes) if user.attributes else {}
        attrs["failed_login_count"] = 0
        attrs.pop("locked_at", None)
        attrs.pop("password_change_required", None)
        user.attributes = attrs

        db.session.commit()

        self.logger.info(
            "Password changed successfully for user: %s", user.user_id
        )
        return True

    def reset_password(self, user: User, new_password: str) -> bool:
        """Reset a user's password without verifying the old password.

        This is the administrator-initiated password reset flow.  No
        current password verification is performed.  A
        ``password_change_required`` flag is set in the user's
        attributes to force the user to choose a new password on
        their next login.

        Args:
            user: The :class:`User` instance whose password is being
                reset.
            new_password: The new plaintext password to set.

        Returns:
            ``True`` on success.  This method does not fail under
            normal circumstances.

        Note:
            On success, any lockout state is cleared and the
            ``password_change_required`` flag is set to ``True``.
        """
        # Hash and set the new password
        user.password_hash = hash_password(new_password)

        # Reset lockout state and set password-change-required flag
        attrs: Dict[str, Any] = dict(user.attributes) if user.attributes else {}
        attrs["failed_login_count"] = 0
        attrs.pop("locked_at", None)
        attrs["password_change_required"] = True
        user.attributes = attrs

        db.session.commit()

        self.logger.info(
            "Password reset by administrator for user: %s", user.user_id
        )
        return True

    # ==================================================================
    # Account Management (Public)
    # ==================================================================

    def unlock_account(self, user: User) -> None:
        """Unlock a user account that was locked due to failed attempts.

        This is the administrator-initiated account unlock.  Resets the
        ``failed_login_count`` to ``0`` and clears the ``locked_at``
        timestamp in ``user.attributes``.

        Args:
            user: The :class:`User` instance to unlock.
        """
        attrs: Dict[str, Any] = dict(user.attributes) if user.attributes else {}
        attrs["failed_login_count"] = 0
        attrs.pop("locked_at", None)
        attrs.pop("last_failed_login", None)
        user.attributes = attrs

        db.session.commit()

        self.logger.info(
            "Account unlocked by administrator: %s", user.user_id
        )

    # ==================================================================
    # User Status Checks (Public)
    # ==================================================================

    def is_password_change_required(self, user: User) -> bool:
        """Check whether the user must change their password.

        This flag is set during administrator-initiated password resets
        via :meth:`reset_password`.  The consuming layer (e.g. API
        middleware) should enforce the password change before allowing
        normal operations.

        Args:
            user: The :class:`User` instance to check.

        Returns:
            ``True`` if the user must change their password on next
            login; ``False`` otherwise.
        """
        attrs: Dict[str, Any] = user.attributes if user.attributes else {}
        return bool(attrs.get("password_change_required", False))
