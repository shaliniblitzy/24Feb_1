"""
JWT Token Validation Realm for Sonatype Nexus Repository (Python/Flask).

This module implements JWT (JSON Web Token) authentication as realm position 3
in the multi-backend authentication chain::

    local → bearer_token → **jwt** → ldap → sso

It replaces ``JwtSecurityFilter`` from the original Java source system
(Apache Shiro 2.0.0), using **PyJWT 2.10.1** (replacing Java-JWT 4.4.0) for
all token operations.

Features:
    - HS256 (symmetric) and RS256 (asymmetric) algorithm support
    - Configurable issuer, audience, and custom claim validation
    - Token generation with user claims (sub, email, roles, jti)
    - Token revocation via JTI blacklist mechanism
    - Token refresh with configurable grace period
    - Structured logging — no token content is ever logged

Configuration Keys (via ``flask.current_app.config``):

+------------------------------+-----------------------------------------------+
| Key                          | Purpose                                       |
+==============================+===============================================+
| ``JWT_SECRET_KEY``           | Symmetric signing key for HS256 (falls back   |
|                              | to ``SECRET_KEY`` when absent)                |
+------------------------------+-----------------------------------------------+
| ``JWT_ALGORITHM``            | ``'HS256'`` (default) or ``'RS256'``          |
+------------------------------+-----------------------------------------------+
| ``JWT_ISSUER``               | Expected token issuer (optional)              |
+------------------------------+-----------------------------------------------+
| ``JWT_AUDIENCE``             | Expected token audience (optional)            |
+------------------------------+-----------------------------------------------+
| ``JWT_PUBLIC_KEY``           | RSA public key PEM for RS256 verification     |
+------------------------------+-----------------------------------------------+
| ``JWT_PRIVATE_KEY``          | RSA private key PEM for RS256 signing (inline)|
+------------------------------+-----------------------------------------------+
| ``JWT_PRIVATE_KEY_PATH``     | File path to RSA private key PEM              |
+------------------------------+-----------------------------------------------+
| ``JWT_REFRESH_GRACE_SECONDS``| Max seconds after expiry for refresh          |
|                              | (default: 604 800 = 7 days)                  |
+------------------------------+-----------------------------------------------+
| ``JWT_TOKEN_EXPIRY_SECONDS`` | Default token lifetime in seconds             |
|                              | (default: 3 600 = 1 hour)                    |
+------------------------------+-----------------------------------------------+
| ``JWT_REQUIRED_REALM``       | Optional custom ``nexus_realm`` claim value    |
+------------------------------+-----------------------------------------------+

Architecture Context:
    - Extends :class:`RealmBase` from ``src.app.auth.realms`` (Chain of
      Responsibility pattern — AAP Section 0.4.3).
    - Uses :class:`User` model for database lookups during authentication.
    - Integrates with Flask application context for configuration access.
    - Follows twelve-factor app methodology (AAP Section 0.7.2).

Security Invariants:
    - Token contents (payload, signature) are **NEVER** logged.
    - Only metadata (event type, username, issuer) appear in log messages.
    - Blacklisted JTIs are checked before user lookup to fail-fast.
    - Expired tokens are rejected by PyJWT before any custom validation.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import jwt
from jwt.exceptions import (
    DecodeError,
    ExpiredSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidTokenError,
)
from flask import current_app

from src.app.auth.realms import RealmBase
from src.app.extensions import db
from src.app.models.user import User

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for JWT authentication diagnostics.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public Exports
# ---------------------------------------------------------------------------

__all__: list[str] = ["JWTRealm"]


# ===========================================================================
# JWTRealm — JWT Token Authentication Realm
# ===========================================================================


class JWTRealm(RealmBase):
    """JWT token authentication realm.

    Validates JWT tokens passed in ``Authorization: Bearer <token>`` headers,
    checking signatures, expiration, issuer, audience, and custom claims.

    This is **realm position 3** in the authentication chain.  The
    ``BearerTokenRealm`` (position 2) attempts API key lookup first; tokens
    that are not recognised API keys fall through to this realm for JWT
    validation.

    Replaces ``JwtSecurityFilter`` from the Java source system
    (Apache Shiro 2.0.0).  Uses **PyJWT 2.10.1** (replacing Java-JWT 4.4.0)
    for all token operations.

    Attributes:
        REALM_NAME: Unique identifier for this realm in the authentication
            chain.
        SUPPORTED_CREDENTIAL_TYPES: List of credential type strings that
            this realm handles.
    """

    # ------------------------------------------------------------------
    # Class-Level Attributes
    # ------------------------------------------------------------------

    REALM_NAME: str = "jwt"
    """Unique realm identifier used for logging, config look-ups, and
    ordering in the authentication chain."""

    SUPPORTED_CREDENTIAL_TYPES: List[str] = ["bearer_token"]
    """Credential types this realm processes.  Shared with
    ``BearerTokenRealm``; the chain tries API key lookup first, then JWT."""

    # ------------------------------------------------------------------
    # Internal Defaults
    # ------------------------------------------------------------------

    _DEFAULT_TOKEN_EXPIRY_SECONDS: int = 3600
    """Default token lifetime: 1 hour (3 600 seconds)."""

    _DEFAULT_REFRESH_GRACE_SECONDS: int = 604800
    """Default refresh grace period: 7 days (604 800 seconds)."""

    _DEFAULT_ISSUER: str = "nexus-repository"
    """Default ``iss`` claim when ``JWT_ISSUER`` is not configured."""

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the JWT realm.

        Sets up:
            - Instance logger (via :meth:`RealmBase.__init__`)
            - In-memory token blacklist (``_blacklisted_jti``) for JTI-based
              revocation.  Maps ``jti → expiry_timestamp`` so that
              :meth:`cleanup_expired_blacklist` can prune naturally expired
              entries.

        .. note::
            In production deployments the blacklist should be backed by Redis
            or a database table for durability across application restarts.
            The in-memory ``dict`` is suitable for single-process development
            and testing.
        """
        super().__init__()
        self.logger: logging.Logger = logging.getLogger(__name__)

        # Blacklist: JTI string → expiry epoch float.
        # Using a dict (rather than a bare set) so cleanup can prune entries
        # whose token expiry has passed, preventing unbounded growth.
        self._blacklisted_jti: Dict[str, float] = {}

    # ==================================================================
    # RealmBase Abstract Interface Implementation
    # ==================================================================

    def authenticate(self, credentials: Dict[str, Any]) -> Optional[User]:
        """Authenticate a request using a JWT bearer token.

        Implements the :class:`RealmBase` contract.  Called by the
        authentication chain after :meth:`supports` returns ``True``.

        Authentication flow:
            1. Extract token string from *credentials* dict.
            2. Decode and verify the JWT (signature, expiration, required
               claims) via :meth:`_decode_token`.
            3. Perform additional custom claim validation via
               :meth:`_validate_claims` (``sub`` non-empty, ``iat`` not
               future, ``jti`` not revoked).
            4. Look up the user by the ``sub`` (subject) claim.
            5. Verify the user exists and has ``status == 'active'``.
            6. Return the :class:`User` on success, or ``None`` on any
               failure.

        Args:
            credentials: Dictionary containing authentication credentials.
                Expected key: ``'token'`` with the raw JWT string value.

        Returns:
            A :class:`User` object if JWT validation and user lookup
            succeed; ``None`` if any step fails.

        Note:
            Token contents are never logged.  Only metadata (event type,
            username on success) is recorded for security.
        """
        token: Optional[str] = credentials.get("token")
        if not token:
            return None

        # Step 1: Decode and verify the JWT token
        payload: Optional[Dict[str, Any]] = self._decode_token(token)
        if payload is None:
            return None

        # Step 2: Validate additional custom claims
        if not self._validate_claims(payload):
            self.logger.warning(
                "JWT custom claim validation failed for realm '%s'.",
                self.REALM_NAME,
            )
            return None

        # Step 3: Extract username from the 'sub' claim
        username: Optional[str] = payload.get("sub")
        if not username:
            self.logger.warning("JWT token missing 'sub' claim after decode.")
            return None

        # Step 4: Look up the user in the database
        try:
            user: Optional[User] = User.query.filter_by(
                user_id=username
            ).first()
        except Exception:
            self.logger.exception(
                "Database error during JWT user lookup for realm '%s'.",
                self.REALM_NAME,
            )
            return None

        if user is None:
            self.logger.warning(
                "JWT authentication failed: user '%s' not found in database.",
                username,
            )
            return None

        # Step 5: Verify user is active
        if user.status != "active":
            self.logger.warning(
                "JWT authentication failed: user '%s' has status '%s'.",
                username,
                user.status,
            )
            return None

        self.logger.info(
            "JWT authentication successful for user '%s' via realm '%s'.",
            username,
            self.REALM_NAME,
        )
        return user

    def supports(self, credentials: Dict[str, Any]) -> bool:
        """Determine whether the supplied credentials look like a JWT.

        JWT tokens have a distinctive three-segment structure:
        ``<header>.<payload>.<signature>``, separated by exactly two dots.
        This method performs a lightweight structural check and then
        attempts to parse the token header using
        :func:`jwt.get_unverified_header` to distinguish actual JWTs from
        other period-delimited tokens (e.g. API keys with dots).

        Args:
            credentials: Dictionary containing authentication credentials.

        Returns:
            ``True`` if the credentials contain a ``'token'`` key whose
            value has the structural properties of a JWT; ``False``
            otherwise.
        """
        token: Optional[str] = credentials.get("token")
        if not token or not isinstance(token, str):
            return False

        # Quick structural check: JWT has exactly three segments (two dots)
        parts: list[str] = token.split(".")
        if len(parts) != 3:
            return False

        # All three parts must be non-empty
        if not all(parts):
            return False

        # Attempt to read the JWT header to confirm valid JWT format
        try:
            header: Dict[str, Any] = jwt.get_unverified_header(token)
            # A valid JWT header must contain an 'alg' (algorithm) field
            return "alg" in header
        except (DecodeError, InvalidTokenError, Exception):
            return False

    def get_realm_name(self) -> str:
        """Return the unique identifier for this realm.

        Returns:
            The string ``'jwt'``.
        """
        return self.REALM_NAME

    # ------------------------------------------------------------------
    # Optional Override
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Check whether the JWT realm has the required configuration.

        The JWT realm requires at minimum a signing/verification key:

        - For **HS256**: ``JWT_SECRET_KEY`` or ``SECRET_KEY`` must be set.
        - For **RS256**: ``JWT_PUBLIC_KEY`` must be set.

        Returns:
            ``True`` if a suitable key is available; ``True`` also when
            outside Flask application context (to avoid blocking realm
            registration at import time).
        """
        try:
            key: Any = self._get_verification_key()
            return key is not None and key != "" and key != b""
        except RuntimeError:
            # Outside application context — cannot verify config.
            # Default to True so the realm is still registered; actual
            # authentication will fail-fast with a clear error message.
            self.logger.debug(
                "JWT realm is_configured called outside app context; "
                "defaulting to True."
            )
            return True

    # ==================================================================
    # Token Decoding and Validation (Private)
    # ==================================================================

    def _decode_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Decode and validate a JWT token using PyJWT.

        Reads all configuration from :data:`flask.current_app.config`:

        - ``JWT_SECRET_KEY`` / ``SECRET_KEY``: HS256 signing key
        - ``JWT_ALGORITHM``: ``'HS256'`` (default) or ``'RS256'``
        - ``JWT_ISSUER``: Expected issuer claim (optional)
        - ``JWT_AUDIENCE``: Expected audience claim (optional)
        - ``JWT_PUBLIC_KEY``: RSA public key for RS256 (optional)

        For RS256 the public key is used for verification; for HS256 the
        secret key is used.

        Args:
            token: The raw JWT string to decode and verify.

        Returns:
            The decoded payload dictionary on success; ``None`` on any
            error.  Specific error types are logged without exposing
            token contents.
        """
        algorithm: str = current_app.config.get("JWT_ALGORITHM", "HS256")
        issuer: Optional[str] = current_app.config.get("JWT_ISSUER", None)
        audience: Optional[str] = current_app.config.get("JWT_AUDIENCE", None)

        # Select the appropriate verification key based on algorithm
        verification_key: Any = self._get_verification_key()
        if (
            verification_key is None
            or verification_key == ""
            or verification_key == b""
        ):
            self.logger.error(
                "JWT verification key is not configured for algorithm '%s'.",
                algorithm,
            )
            return None

        try:
            payload: Dict[str, Any] = jwt.decode(
                token,
                verification_key,
                algorithms=[algorithm],
                issuer=issuer,
                audience=audience,
                options={
                    "require": ["exp", "sub", "iat"],
                    "verify_exp": True,
                    "verify_iss": issuer is not None,
                    "verify_aud": audience is not None,
                },
            )
            return payload

        except ExpiredSignatureError:
            self.logger.warning("JWT token expired.")
            return None
        except InvalidSignatureError:
            self.logger.warning("JWT signature verification failed.")
            return None
        except DecodeError:
            self.logger.warning("JWT decode error — malformed token.")
            return None
        except InvalidIssuerError:
            self.logger.warning("JWT issuer mismatch.")
            return None
        except InvalidAudienceError:
            self.logger.warning("JWT audience mismatch.")
            return None
        except InvalidTokenError as exc:
            self.logger.warning(
                "JWT validation error: %s", type(exc).__name__
            )
            return None
        except Exception:
            self.logger.exception("Unexpected error during JWT decode.")
            return None

    def _validate_claims(self, payload: Dict[str, Any]) -> bool:
        """Perform additional custom claim validation beyond PyJWT built-ins.

        Checks applied:
            1. ``sub`` (subject) is a non-empty string.
            2. ``iat`` (issued at) is not in the future, with a 30-second
               clock-skew tolerance.
            3. ``jti`` (JWT ID), if present, is not in the revocation
               blacklist.
            4. ``nexus_realm`` custom claim, if ``JWT_REQUIRED_REALM`` is
               configured, matches the expected value.

        Args:
            payload: The decoded JWT payload dictionary.

        Returns:
            ``True`` if all custom validations pass; ``False`` otherwise.
        """
        # 1. Check 'sub' is a non-empty string
        sub: Any = payload.get("sub")
        if not sub or not isinstance(sub, str) or not sub.strip():
            self.logger.warning(
                "JWT claim validation failed: 'sub' is empty or missing."
            )
            return False

        # 2. Check 'iat' (issued at) is not in the future
        iat: Any = payload.get("iat")
        if iat is not None:
            current_time: float = time.time()
            clock_skew_tolerance: int = 30  # seconds
            if isinstance(iat, (int, float)) and iat > (
                current_time + clock_skew_tolerance
            ):
                self.logger.warning(
                    "JWT claim validation failed: 'iat' is in the future "
                    "(clock skew exceeds %d seconds).",
                    clock_skew_tolerance,
                )
                return False

        # 3. Check 'jti' against revocation blacklist
        jti: Optional[str] = payload.get("jti")
        if jti is not None and self.is_token_revoked(jti):
            self.logger.warning(
                "JWT claim validation failed: token JTI is revoked."
            )
            return False

        # 4. Check custom 'nexus_realm' claim if configured
        try:
            expected_realm: Optional[str] = current_app.config.get(
                "JWT_REQUIRED_REALM", None
            )
            if expected_realm is not None:
                token_realm: Any = payload.get("nexus_realm")
                if token_realm != expected_realm:
                    self.logger.warning(
                        "JWT claim validation failed: 'nexus_realm' "
                        "claim mismatch."
                    )
                    return False
        except RuntimeError:
            # No application context — skip configuration-dependent checks
            pass

        return True

    # ==================================================================
    # Token Generation
    # ==================================================================

    def generate_token(self, user: User, expires_in: int = 3600) -> str:
        """Generate a new JWT token for the given user.

        Creates a signed JWT containing standard and Nexus-specific claims.

        Claims included:

        +----------+-----------------------------------------------------+
        | Claim    | Value                                               |
        +==========+=====================================================+
        | ``sub``  | :attr:`User.user_id` (subject / username)           |
        +----------+-----------------------------------------------------+
        | ``iat``  | Current UTC timestamp (issued at)                   |
        +----------+-----------------------------------------------------+
        | ``exp``  | ``iat + expires_in`` (expiration)                    |
        +----------+-----------------------------------------------------+
        | ``jti``  | Random UUID4 (JWT ID for revocation support)        |
        +----------+-----------------------------------------------------+
        | ``iss``  | Configured issuer or ``'nexus-repository'``         |
        +----------+-----------------------------------------------------+
        | ``email``| :attr:`User.email` or empty string                  |
        +----------+-----------------------------------------------------+
        | ``roles``| List of assigned role names                          |
        +----------+-----------------------------------------------------+
        | ``aud``  | Configured audience (omitted if not set)             |
        +----------+-----------------------------------------------------+

        The token is signed using the configured algorithm (HS256 or RS256)
        and the corresponding signing key.

        Args:
            user: The :class:`User` object to generate a token for.  Must
                have ``user_id``, ``email``, and ``roles`` attributes.
            expires_in: Token lifetime in seconds.  Defaults to 3 600
                (1 hour).

        Returns:
            The encoded JWT token string.

        Raises:
            RuntimeError: If the signing key is not configured.
        """
        now: datetime = datetime.now(tz=timezone.utc)
        algorithm: str = current_app.config.get("JWT_ALGORITHM", "HS256")
        issuer: str = current_app.config.get(
            "JWT_ISSUER", self._DEFAULT_ISSUER
        )

        # Build the JWT payload with all required and optional claims
        payload: Dict[str, Any] = {
            "sub": user.user_id,
            "iat": now,
            "exp": now + timedelta(seconds=expires_in),
            "jti": str(uuid.uuid4()),
            "iss": issuer,
            "email": user.email or "",
            "roles": self._get_user_role_names(user),
        }

        # Add audience claim if configured
        audience: Optional[str] = current_app.config.get(
            "JWT_AUDIENCE", None
        )
        if audience is not None:
            payload["aud"] = audience

        # Obtain the signing key
        signing_key: Any = self._get_signing_key()
        if (
            signing_key is None
            or signing_key == ""
            or signing_key == b""
        ):
            raise RuntimeError(
                f"JWT signing key is not configured for algorithm "
                f"'{algorithm}'.  Set JWT_SECRET_KEY (HS256) or "
                f"JWT_PRIVATE_KEY / JWT_PRIVATE_KEY_PATH (RS256)."
            )

        # Encode and sign the token using PyJWT
        token: str = jwt.encode(payload, signing_key, algorithm=algorithm)

        self.logger.info(
            "JWT token generated for user '%s' with expiry in %d seconds.",
            user.user_id,
            expires_in,
        )
        return token

    def _get_user_role_names(self, user: User) -> List[str]:
        """Retrieve role names for the user to embed in the JWT payload.

        Reads the ``roles`` relationship from the :class:`User` model and
        extracts the ``role_id`` from each associated :class:`Role` object.

        Args:
            user: The :class:`User` whose roles should be retrieved.

        Returns:
            A list of role name strings.  Returns an empty list if the
            roles relationship cannot be loaded (e.g. detached session).
        """
        try:
            return [role.role_id for role in user.roles]
        except Exception:
            self.logger.debug(
                "Could not load roles for user '%s'; returning empty list.",
                user.user_id,
            )
            return []

    # ==================================================================
    # Token Revocation
    # ==================================================================

    def revoke_token(self, jti: str) -> None:
        """Add a JWT ID (JTI) to the revocation blacklist.

        Once a JTI is blacklisted, any token carrying that JTI will be
        rejected by :meth:`_validate_claims` during authentication.

        The blacklist entry includes an expiry timestamp so that
        :meth:`cleanup_expired_blacklist` can prune naturally expired
        entries.  The expiry is set to ``now + _DEFAULT_REFRESH_GRACE_SECONDS``
        as a conservative upper bound.

        .. note::
            In production deployments this should be backed by Redis or a
            database table for durability across application restarts.

        Args:
            jti: The JWT ID string to revoke.
        """
        if not jti:
            self.logger.warning(
                "Attempted to revoke an empty JTI — ignored."
            )
            return

        # Store with an expiry timestamp for cleanup.
        expiry: float = time.time() + self._DEFAULT_REFRESH_GRACE_SECONDS
        self._blacklisted_jti[jti] = expiry

        self.logger.info("JWT token revoked (JTI blacklisted).")

    def is_token_revoked(self, jti: str) -> bool:
        """Check whether a token's JTI has been revoked.

        Args:
            jti: The JWT ID string to check.

        Returns:
            ``True`` if the JTI is in the blacklist (token is revoked);
            ``False`` if the JTI is not blacklisted (token is valid).
        """
        if not jti:
            return False
        return jti in self._blacklisted_jti

    def cleanup_expired_blacklist(self) -> None:
        """Remove naturally expired entries from the JTI blacklist.

        Iterates through the blacklist and removes entries whose expiry
        timestamp has passed.  This prevents unbounded growth of the
        in-memory blacklist.

        Designed to be called periodically by a scheduled task (e.g. via
        APScheduler — Feature F-402).
        """
        current_time: float = time.time()
        expired_jtis: List[str] = [
            jti
            for jti, expiry in self._blacklisted_jti.items()
            if expiry < current_time
        ]

        for jti in expired_jtis:
            del self._blacklisted_jti[jti]

        if expired_jtis:
            self.logger.info(
                "Cleaned up %d expired entries from JTI blacklist.  "
                "Remaining: %d entries.",
                len(expired_jtis),
                len(self._blacklisted_jti),
            )

    # ==================================================================
    # Token Refresh
    # ==================================================================

    def refresh_token(self, token: str) -> Optional[str]:
        """Refresh an existing (or recently expired) JWT token.

        Accepts a token and issues a new one with a refreshed expiration,
        provided:

        1. The token can be decoded (signature is valid).
        2. The token has not been expired for longer than the configured
           grace period (``JWT_REFRESH_GRACE_SECONDS``, default 7 days).
        3. The user referenced in the ``sub`` claim still exists and is
           active.
        4. The old token's JTI is not already revoked.

        The old token is revoked (its JTI is added to the blacklist) upon
        successful refresh to prevent replay.

        Args:
            token: The existing JWT token string to refresh.

        Returns:
            A new JWT token string on success; ``None`` if refresh is
            denied.
        """
        algorithm: str = current_app.config.get("JWT_ALGORITHM", "HS256")
        verification_key: Any = self._get_verification_key()

        if (
            verification_key is None
            or verification_key == ""
            or verification_key == b""
        ):
            self.logger.error(
                "JWT verification key not configured for token refresh."
            )
            return None

        # Decode without expiration verification to allow recently-expired
        # tokens through for refresh evaluation.
        try:
            payload: Dict[str, Any] = jwt.decode(
                token,
                verification_key,
                algorithms=[algorithm],
                options={
                    "verify_exp": False,
                    "require": ["sub", "iat"],
                },
            )
        except InvalidSignatureError:
            self.logger.warning("JWT refresh denied: invalid signature.")
            return None
        except DecodeError:
            self.logger.warning("JWT refresh denied: malformed token.")
            return None
        except InvalidTokenError as exc:
            self.logger.warning(
                "JWT refresh denied: %s", type(exc).__name__
            )
            return None
        except Exception:
            self.logger.exception(
                "Unexpected error during JWT refresh decode."
            )
            return None

        # Check the old JTI is not already revoked
        old_jti: Optional[str] = payload.get("jti")
        if old_jti and self.is_token_revoked(old_jti):
            self.logger.warning(
                "JWT refresh denied: token JTI is already revoked."
            )
            return None

        # Check the token hasn't been expired for too long
        exp_timestamp: Any = payload.get("exp")
        if exp_timestamp is not None:
            grace_seconds: int = current_app.config.get(
                "JWT_REFRESH_GRACE_SECONDS",
                self._DEFAULT_REFRESH_GRACE_SECONDS,
            )
            current_timestamp: float = time.time()
            if isinstance(exp_timestamp, (int, float)):
                expired_duration: float = current_timestamp - exp_timestamp
                if expired_duration > grace_seconds:
                    self.logger.warning(
                        "JWT refresh denied: token expired %.0f seconds ago, "
                        "exceeding grace period of %d seconds.",
                        expired_duration,
                        grace_seconds,
                    )
                    return None

        # Verify the user still exists and is active
        username: Optional[str] = payload.get("sub")
        if not username:
            self.logger.warning("JWT refresh denied: missing 'sub' claim.")
            return None

        try:
            user: Optional[User] = User.query.filter_by(
                user_id=username
            ).first()
        except Exception:
            self.logger.exception(
                "Database error during JWT refresh user lookup."
            )
            return None

        if user is None:
            self.logger.warning(
                "JWT refresh denied: user '%s' no longer exists.",
                username,
            )
            return None

        if user.status != "active":
            self.logger.warning(
                "JWT refresh denied: user '%s' has status '%s'.",
                username,
                user.status,
            )
            return None

        # Revoke the old token to prevent replay
        if old_jti:
            self.revoke_token(old_jti)

        # Generate and return a new token
        expires_in: int = current_app.config.get(
            "JWT_TOKEN_EXPIRY_SECONDS",
            self._DEFAULT_TOKEN_EXPIRY_SECONDS,
        )
        new_token: str = self.generate_token(user, expires_in=expires_in)

        self.logger.info("JWT token refreshed for user '%s'.", username)
        return new_token

    # ==================================================================
    # RSA Key Support (Private Helpers)
    # ==================================================================

    def _get_signing_key(self) -> Any:
        """Return the appropriate key for JWT signing.

        For **HS256** (symmetric): Returns the ``JWT_SECRET_KEY`` config
        value, falling back to ``SECRET_KEY``.

        For **RS256** (asymmetric): Returns the RSA private key, loaded
        from either ``JWT_PRIVATE_KEY`` (inline PEM string) or
        ``JWT_PRIVATE_KEY_PATH`` (filesystem path to PEM file).

        Returns:
            The signing key (``str`` or ``bytes``), or ``None`` if not
            configured.
        """
        algorithm: str = current_app.config.get("JWT_ALGORITHM", "HS256")

        if algorithm == "RS256":
            # Try inline private key first
            private_key: Optional[str] = current_app.config.get(
                "JWT_PRIVATE_KEY"
            )
            if private_key:
                if isinstance(private_key, str):
                    return private_key.encode("utf-8")
                return private_key

            # Try file-based private key
            private_key_path: Optional[str] = current_app.config.get(
                "JWT_PRIVATE_KEY_PATH"
            )
            if private_key_path:
                try:
                    with open(private_key_path, "rb") as key_file:
                        key_data: bytes = key_file.read()
                    self.logger.debug(
                        "Loaded RSA private key from file for JWT signing."
                    )
                    return key_data
                except FileNotFoundError:
                    self.logger.error(
                        "RSA private key file not found at configured path."
                    )
                    return None
                except OSError:
                    self.logger.exception(
                        "Error reading RSA private key file."
                    )
                    return None

            self.logger.error(
                "RS256 algorithm requires JWT_PRIVATE_KEY or "
                "JWT_PRIVATE_KEY_PATH configuration."
            )
            return None

        # HS256 (default): symmetric key
        secret_key: Optional[str] = current_app.config.get(
            "JWT_SECRET_KEY",
            current_app.config.get("SECRET_KEY", ""),
        )
        return secret_key

    def _get_verification_key(self) -> Any:
        """Return the appropriate key for JWT signature verification.

        For **HS256** (symmetric): Returns the same ``JWT_SECRET_KEY`` used
        for signing (symmetric algorithms use one key for both operations).

        For **RS256** (asymmetric): Returns the RSA public key from the
        ``JWT_PUBLIC_KEY`` configuration value.

        Returns:
            The verification key (``str`` or ``bytes``), or ``None`` if
            not configured.
        """
        algorithm: str = current_app.config.get("JWT_ALGORITHM", "HS256")

        if algorithm == "RS256":
            public_key: Optional[str] = current_app.config.get(
                "JWT_PUBLIC_KEY"
            )
            if public_key:
                if isinstance(public_key, str):
                    return public_key.encode("utf-8")
                return public_key

            self.logger.error(
                "RS256 algorithm requires JWT_PUBLIC_KEY configuration "
                "for token verification."
            )
            return None

        # HS256 (default): same key as signing
        secret_key: Optional[str] = current_app.config.get(
            "JWT_SECRET_KEY",
            current_app.config.get("SECRET_KEY", ""),
        )
        return secret_key


# ---------------------------------------------------------------------------
# Module Load Logging
# ---------------------------------------------------------------------------

logger.debug(
    "JWT realm module loaded — exports: %s",
    ", ".join(__all__),
)
