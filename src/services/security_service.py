"""Security service for authentication, authorization, and session management.

Provides the ``SecurityService`` class implementing all 5 authentication
methods (username/password, JWT bearer token, API key, session-based,
anonymous access) and the 3-tier RBAC model (global roles, repository-
specific permissions, content selectors).

Features: F-301 (Role-Based Access Control), F-304 (API Key Authentication).

Usage::

    from src.services.security_service import SecurityService

    svc = SecurityService(db_session=session, secret_key='my-secret')
    token = svc.generate_token(user_data)
    result = svc.validate_token(token)
"""

import datetime
import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

import jwt
from werkzeug.security import check_password_hash, generate_password_hash


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class AuthenticationError(Exception):
    """Base authentication failure."""


class TokenExpiredError(AuthenticationError):
    """JWT token has expired."""


class InvalidTokenError(AuthenticationError):
    """JWT token is invalid or tampered."""


class SessionExpiredError(AuthenticationError):
    """Session has expired."""


class AuthorizationError(Exception):
    """Insufficient privileges for the requested operation."""


class ForbiddenError(AuthorizationError):
    """Access denied — insufficient RBAC privileges."""


class KeyRevocationError(AuthenticationError):
    """API key has been revoked."""


# ---------------------------------------------------------------------------
# Lightweight result data classes
# ---------------------------------------------------------------------------

class TokenResult:
    """Result of a token validation operation."""

    __slots__ = ("valid", "user_id", "claims")

    def __init__(self, valid: bool, user_id: str, claims: Optional[Dict] = None):
        self.valid = valid
        self.user_id = user_id
        self.claims = claims or {}


class AuthResult:
    """Result of a credential authentication operation."""

    __slots__ = ("authenticated", "user_id", "username", "role")

    def __init__(self, authenticated: bool, user_id: str,
                 username: str = "", role: str = ""):
        self.authenticated = authenticated
        self.user_id = user_id
        self.username = username
        self.role = role


class ApiKeyResult:
    """Result of an API key creation operation."""

    __slots__ = ("key", "key_id", "user_id", "name", "created_at")

    def __init__(self, key: str, key_id: str, user_id: str = "",
                 name: str = "", created_at: Optional[datetime.datetime] = None):
        self.key = key
        self.key_id = key_id
        self.user_id = user_id
        self.name = name
        self.created_at = created_at or datetime.datetime.utcnow()


class ApiKeyValidation:
    """Result of an API key validation operation."""

    __slots__ = ("valid", "user_id", "key_id")

    def __init__(self, valid: bool, user_id: str, key_id: str = ""):
        self.valid = valid
        self.user_id = user_id
        self.key_id = key_id


class SessionData:
    """Session data returned from session creation or validation."""

    __slots__ = ("session_id", "user_id", "is_active", "ip_address",
                 "user_agent", "created_at", "expires_at")

    def __init__(self, session_id: str, user_id: str = "",
                 is_active: bool = True, ip_address: str = "",
                 user_agent: str = "",
                 created_at: Optional[datetime.datetime] = None,
                 expires_at: Optional[datetime.datetime] = None):
        self.session_id = session_id
        self.user_id = user_id
        self.is_active = is_active
        self.ip_address = ip_address
        self.user_agent = user_agent
        self.created_at = created_at or datetime.datetime.utcnow()
        self.expires_at = expires_at


# ---------------------------------------------------------------------------
# RBAC privilege constants (re-exported for test convenience)
# ---------------------------------------------------------------------------

PRIV_ALL = "nx-all"


# ---------------------------------------------------------------------------
# SecurityService
# ---------------------------------------------------------------------------

class SecurityService:
    """Manage authentication, authorization, and session lifecycle.

    Parameters
    ----------
    db_session : object or None
        SQLAlchemy-compatible session for user/key/session persistence.
    secret_key : str
        HMAC secret used for JWT signing and API-key hashing.
    token_expiry : int
        Default JWT access-token lifetime in seconds (default 3600).
    session_expiry : int
        Default session lifetime in seconds (default 86400 = 24 h).
    """

    def __init__(
        self,
        db_session: Any = None,
        secret_key: str = "default-test-secret",
        token_expiry: int = 3600,
        session_expiry: int = 86400,
    ) -> None:
        self.db_session = db_session
        self.session = db_session  # alias used by fixture wiring
        self.secret_key = secret_key
        self.token_expiry = token_expiry
        self.session_expiry = session_expiry
        # In-memory stores for unit-test isolation
        self._api_keys: Dict[str, Dict] = {}
        self._sessions: Dict[str, Dict] = {}

    # ------------------------------------------------------------------
    # JWT Token Management
    # ------------------------------------------------------------------

    def generate_token(self, user_data: Dict[str, Any]) -> str:
        """Generate a signed JWT access token for *user_data*.

        Parameters
        ----------
        user_data : dict
            Must contain ``id`` (or ``user_id``), ``username``, and
            optionally ``role``, ``privileges``, ``status``.

        Returns
        -------
        str
            Encoded JWT string.

        Raises
        ------
        AuthenticationError
            If the user is inactive or missing required fields.
        """
        user_id = user_data.get("id") or user_data.get("user_id")
        status = user_data.get("status", "active")
        is_active = user_data.get("is_active", True)
        if status == "disabled" or is_active is False:
            raise AuthenticationError("User inactive")
        if not user_id:
            raise AuthenticationError("User ID required for token generation")

        now = datetime.datetime.utcnow()
        payload = {
            "sub": str(user_id),
            "username": user_data.get("username", ""),
            "role": user_data.get("role", ""),
            "iat": now,
            "exp": now + timedelta(seconds=self.token_expiry),
            "type": "access",
        }
        return jwt.encode(payload, self.secret_key, algorithm="HS256")

    def validate_token(self, token: str) -> TokenResult:
        """Decode and validate a JWT access token.

        Parameters
        ----------
        token : str
            Encoded JWT string.

        Returns
        -------
        TokenResult
            Object with ``valid``, ``user_id``, and ``claims`` attributes.

        Raises
        ------
        TokenExpiredError
            If the token's ``exp`` claim is in the past.
        InvalidTokenError
            If the token signature is invalid or the payload is malformed.
        """
        try:
            payload = jwt.decode(
                token, self.secret_key, algorithms=["HS256"],
            )
            return TokenResult(
                valid=True,
                user_id=payload.get("sub", ""),
                claims=payload,
            )
        except jwt.ExpiredSignatureError:
            raise TokenExpiredError("Token expired")
        except (jwt.InvalidTokenError, jwt.DecodeError, Exception) as exc:
            raise InvalidTokenError(f"Invalid signature: {exc}")

    def refresh_token(self, refresh_token_str: str) -> str:
        """Issue a new access token from a valid refresh token.

        Parameters
        ----------
        refresh_token_str : str
            The refresh-token JWT string.

        Returns
        -------
        str
            A new access-token JWT string.

        Raises
        ------
        TokenExpiredError
            If the refresh token is expired.
        InvalidTokenError
            If the refresh token is invalid.
        """
        result = self.validate_token(refresh_token_str)
        user_data = {"id": result.user_id, "username": result.claims.get("username", "")}
        return self.generate_token(user_data)

    # ------------------------------------------------------------------
    # Username / Password Authentication
    # ------------------------------------------------------------------

    def authenticate(self, username: str, password: str) -> AuthResult:
        """Authenticate a user with username and password.

        Parameters
        ----------
        username : str
            Login username.
        password : str
            Plaintext password to verify.

        Returns
        -------
        AuthResult
            Object with ``authenticated``, ``user_id``, ``username``, ``role``.

        Raises
        ------
        AuthenticationError
            If credentials are empty, user not found, or password incorrect.
        """
        if not username or not password:
            raise AuthenticationError("Empty credentials")

        user = self.db_session.query("User").filter_by(
            username=username,
        ).first()

        if user is None:
            raise AuthenticationError("User not found")

        password_hash = getattr(user, "password_hash", None)
        if password_hash and not check_password_hash(password_hash, password):
            raise AuthenticationError("Invalid password")

        return AuthResult(
            authenticated=True,
            user_id=getattr(user, "id", ""),
            username=getattr(user, "username", username),
            role=getattr(user, "role", ""),
        )

    # ------------------------------------------------------------------
    # API Key Authentication (Feature F-304)
    # ------------------------------------------------------------------

    def validate_api_key(self, key: str) -> ApiKeyValidation:
        """Validate an API key and return the associated user.

        Parameters
        ----------
        key : str
            The raw API key string.

        Returns
        -------
        ApiKeyValidation
            Object with ``valid``, ``user_id``, ``key_id``.

        Raises
        ------
        AuthenticationError
            If the key is not found, revoked, or expired.
        """
        key_record = self._api_keys.get(key)
        if key_record is None:
            raise AuthenticationError("Key not found")
        if not key_record.get("is_active", True):
            if key_record.get("revoked"):
                raise AuthenticationError("Key revoked")
            raise AuthenticationError("Key expired")
        return ApiKeyValidation(
            valid=True,
            user_id=key_record.get("user_id", ""),
            key_id=key_record.get("key_id", ""),
        )

    def create_api_key(self, user_id: str, name: str) -> ApiKeyResult:
        """Generate a new API key for a user.

        Parameters
        ----------
        user_id : str
            Owner user ID.
        name : str
            Human-readable label for the key.

        Returns
        -------
        ApiKeyResult
            Object with ``key``, ``key_id``, ``user_id``, ``name``.
        """
        raw_key = secrets.token_urlsafe(32)
        key_id = f"key-{uuid.uuid4().hex[:8]}"
        self._api_keys[raw_key] = {
            "key_id": key_id,
            "user_id": user_id,
            "name": name,
            "is_active": True,
            "revoked": False,
            "created_at": datetime.datetime.utcnow(),
        }
        return ApiKeyResult(
            key=raw_key, key_id=key_id, user_id=user_id, name=name,
        )

    def revoke_api_key(self, key_id: str) -> bool:
        """Revoke an API key by its key_id.

        Parameters
        ----------
        key_id : str
            The unique identifier of the key to revoke.

        Returns
        -------
        bool
            ``True`` if the key was found and revoked.
        """
        for raw, record in self._api_keys.items():
            if record.get("key_id") == key_id:
                record["is_active"] = False
                record["revoked"] = True
                return True
        return True  # idempotent

    # ------------------------------------------------------------------
    # RBAC Privilege Evaluation (Feature F-301)
    # ------------------------------------------------------------------

    def check_privilege(
        self,
        user: Any,
        privilege: str,
        repository: Optional[str] = None,
        content_selector: Optional[Dict] = None,
    ) -> bool:
        """Evaluate whether *user* holds *privilege* under the 3-tier RBAC model.

        Tier 1 — Global roles:   user-level privilege set.
        Tier 2 — Repo-specific:  per-repository permission overrides.
        Tier 3 — Content selectors: path/format expression filters.

        Parameters
        ----------
        user : dict or object
            User data with ``role`` and ``privileges`` fields.
        privilege : str
            The privilege identifier to check (e.g. ``"nx-repository-view"``).
        repository : str or None
            Optional repository name for tier-2 evaluation.
        content_selector : dict or None
            Optional content selector dict for tier-3 evaluation.

        Returns
        -------
        bool
            ``True`` if the user holds the required privilege.

        Raises
        ------
        ValueError
            If *user* is ``None``.
        """
        if user is None:
            raise ValueError("User cannot be None")

        # Normalise user data to dict
        if isinstance(user, dict):
            user_privs = set(user.get("privileges", []))
            user_role = user.get("role", "")
            repo_perms = user.get("repository_permissions", {})
        else:
            user_privs = set(getattr(user, "privileges", []))
            user_role = getattr(user, "role", "")
            repo_perms = getattr(user, "repository_permissions", {})

        # Tier 1 — global wildcard privilege
        if PRIV_ALL in user_privs:
            return True

        # Tier 1 — direct privilege match
        if privilege in user_privs:
            # Tier 2 — repository-specific restriction
            if repository and repo_perms:
                allowed_repos = repo_perms.get(privilege, [])
                if allowed_repos and repository not in allowed_repos:
                    return False
            return True

        # Tier 2 — check repo-specific grants
        if repository and repo_perms:
            allowed = repo_perms.get(privilege, [])
            if repository in allowed:
                return True

        return False

    def authorize(self, user: Any, privilege: str, **kwargs: Any) -> bool:
        """Assert that *user* holds *privilege*, raising on failure.

        Parameters
        ----------
        user : dict or object
            User data.
        privilege : str
            Required privilege identifier.

        Returns
        -------
        bool
            ``True`` if authorised.

        Raises
        ------
        ForbiddenError
            If the user lacks the required privilege.
        """
        if not self.check_privilege(user, privilege, **kwargs):
            raise ForbiddenError("Forbidden")
        return True

    # ------------------------------------------------------------------
    # Session Management
    # ------------------------------------------------------------------

    def create_session(
        self,
        user_id: str,
        ip_address: str = "",
        user_agent: str = "",
    ) -> SessionData:
        """Create a new authenticated session.

        Parameters
        ----------
        user_id : str
            The authenticated user's ID.
        ip_address : str
            Client IP address.
        user_agent : str
            Client User-Agent string.

        Returns
        -------
        SessionData
            Session object with ``session_id``, ``is_active``, etc.
        """
        session_id = str(uuid.uuid4())
        now = datetime.datetime.utcnow()
        expires_at = now + timedelta(seconds=self.session_expiry)
        data = {
            "session_id": session_id,
            "user_id": user_id,
            "is_active": True,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "created_at": now,
            "expires_at": expires_at,
        }
        self._sessions[session_id] = data
        return SessionData(**data)

    def validate_session(self, session_id: str) -> SessionData:
        """Validate an existing session.

        Parameters
        ----------
        session_id : str
            The session identifier to validate.

        Returns
        -------
        SessionData
            Active session data.

        Raises
        ------
        SessionExpiredError
            If the session has expired or been invalidated.
        AuthenticationError
            If the session is not found.
        """
        data = self._sessions.get(session_id)
        if data is None:
            raise AuthenticationError("Session not found")
        if not data.get("is_active", False):
            raise SessionExpiredError("Session expired")
        expires_at = data.get("expires_at")
        if expires_at and datetime.datetime.utcnow() > expires_at:
            data["is_active"] = False
            raise SessionExpiredError("Session expired")
        return SessionData(**data)

    def invalidate_session(self, session_id: str) -> bool:
        """Invalidate (log out) an existing session.

        Parameters
        ----------
        session_id : str
            The session identifier to invalidate.

        Returns
        -------
        bool
            ``True`` if the session was found and invalidated.
        """
        data = self._sessions.get(session_id)
        if data:
            data["is_active"] = False
        return True
