"""
Multi-Realm Authentication Chain for Sonatype Nexus Repository (Python/Flask).

This module implements the multi-realm authentication chain that replaces
``NexusAuthenticationFilter`` + ``FirstSuccessfulModularRealmAuthenticator``
from the original Java source system (Apache Shiro 2.0.0).

**Architecture Pattern — Chain of Responsibility (AAP Section 0.4.3):**

The :class:`AuthenticationChain` iterates through an ordered list of
configured authentication realms until one succeeds (first-successful
strategy).  This mirrors Shiro's ``FirstSuccessfulModularRealmAuthenticator``
behaviour where realm order determines priority.

**Default Realm Order:**

1. ``local``          — Username / password against the local database
2. ``bearer_token``   — API key authentication (Feature F-304)
3. ``jwt``            — JWT token validation (PyJWT 2.10.1)
4. ``ldap``           — LDAP / Active Directory bind
5. ``sso``            — SAML 2.0 / OpenID Connect federation

**Flask-HTTPAuth Integration:**

The chain registers callbacks with :attr:`basic_auth` (HTTP Basic) and
:attr:`token_auth` (HTTP Bearer) from ``src.app.extensions``.  The
``MultiAuth`` instance (``multi_auth``) combines both schemes with a
first-successful strategy, making this module the single entry point for
*all* HTTP authentication.

**Event-Driven Audit (Feature F-303):**

Authentication outcomes are broadcast as Blinker signals (``auth_success``
and ``auth_failure``), enabling the audit-logging subscriber to record
events without coupling the authentication layer to the persistence layer.

**Anonymous Access (Feature F-404):**

When no credentials are presented and the ``security.anonymousAccess``
system configuration flag is ``true``, requests are authenticated as the
``anonymous`` user with privileges defined by the ``nx-anonymous`` role.

Exports:
    authenticate_request    : Module-level convenience function for
                              ``before_request`` hooks.
    AuthenticationChain     : Core orchestrator class.
    login_required          : Decorator requiring authentication.
    get_current_user        : Helper to retrieve the authenticated user.
    AuthenticationResult    : Dataclass encapsulating auth outcomes.
    auth_success            : Blinker signal emitted on successful auth.
    auth_failure            : Blinker signal emitted on failed auth.

Security Invariants:
    - Passwords and token values are **NEVER** logged.
    - Only metadata (username, realm name, outcome, IP) appear in logs.
    - All credential comparisons are performed within realm implementations
      using constant-time algorithms.
    - Account lockout after configurable failed-login threshold is delegated
      to the ``LocalRealm`` via ``User.record_failed_login()``.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from flask import abort, current_app, g, request

from src.app.extensions import basic_auth, event_signals, multi_auth, token_auth
from src.app.models.user import User

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for authentication chain operations,
# realm iteration events, and credential extraction diagnostics.
# CRITICAL: Passwords and token values must NEVER appear in log output.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blinker Signals for Authentication Events
# ---------------------------------------------------------------------------
# Replaces Guava EventBus from the Java source system.
# These signals trigger AuditEvent creation via subscribers (Feature F-303).
# Subscribers connect to these signals in the events/subscribers.py module.
# ---------------------------------------------------------------------------

auth_success = event_signals.signal("auth-success")
"""Signal emitted on successful authentication.

Keyword arguments sent with the signal:

- ``user`` (:class:`User`) — the authenticated user object.
- ``realm`` (str) — name of the realm that authenticated the user.
- ``ip`` (str) — client IP address from ``request.remote_addr``.
"""

auth_failure = event_signals.signal("auth-failure")
"""Signal emitted on failed authentication.

Keyword arguments sent with the signal:

- ``username`` (str) — attempted username (or ``'<token>'`` for tokens).
- ``realm`` (str) — name of the realm that was attempted.
- ``reason`` (str) — human-readable failure reason.
- ``ip`` (str) — client IP address from ``request.remote_addr``.
"""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANONYMOUS_USER_ID: str = "anonymous"
"""Default user_id for the anonymous access pseudo-user."""

_DEFAULT_REALM_ORDER: List[str] = ["local", "bearer_token", "jwt"]
"""Default list of enabled realm names when ``AUTH_REALMS`` is not set."""

_FULL_REALM_ORDER: List[str] = [
    "local",
    "bearer_token",
    "jwt",
    "ldap",
    "sso",
]
"""Complete realm order including optional enterprise realms."""


# ===========================================================================
# AuthenticationResult — Structured Auth Outcome
# ===========================================================================


@dataclass
class AuthenticationResult:
    """Encapsulates the outcome of an authentication attempt.

    Replaces the ``AuthenticationInfo`` return type from the Apache Shiro
    2.0.0 framework.  Provides a structured value object that is consumed
    by ``before_request`` hooks, the ``login_required`` decorator, and
    any module that needs to inspect authentication state.

    Attributes:
        authenticated: ``True`` if authentication succeeded.
        user:          The authenticated :class:`User` on success, or
                       ``None`` on failure.
        realm_name:    Name of the realm that authenticated the user
                       (e.g. ``'local'``, ``'jwt'``, ``'anonymous'``).
        error_message: Human-readable error description on failure.
    """

    authenticated: bool
    user: Optional[User] = None
    realm_name: str = ""
    error_message: str = ""


# ===========================================================================
# AuthenticationChain — Core Authentication Orchestrator
# ===========================================================================


class AuthenticationChain:
    """Multi-realm authentication orchestrator using first-successful strategy.

    Replaces ``FirstSuccessfulModularRealmAuthenticator`` from the Java
    source system (Apache Shiro 2.0.0).  Maintains an ordered list of
    :class:`~src.app.auth.realms.RealmBase` instances and iterates through
    them for each authentication request until one succeeds.

    **Lifecycle:**

    1. Instantiated in ``factory.py`` (or test setup) without an app.
    2. ``init_app(app)`` is called during application creation, which:
       - Reads enabled realm names from ``app.config['AUTH_REALMS']``.
       - Registers built-in realms in the global
         :data:`~src.app.auth.realms.realm_registry`.
       - Obtains configured realm instances via
         :meth:`~src.app.auth.realms.RealmRegistry.get_ordered_realms`.
       - Registers Flask-HTTPAuth callbacks on ``basic_auth`` and
         ``token_auth`` extension instances.
       - Stores itself in ``app.extensions['auth_chain']``.
    3. On each request, Flask-HTTPAuth invokes the registered callbacks,
       which delegate to ``_authenticate_basic`` or ``_authenticate_token``.

    Class Attributes:
        DEFAULT_REALM_ORDER: Default list of enabled realm names.
    """

    DEFAULT_REALM_ORDER: List[str] = _DEFAULT_REALM_ORDER

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self, app: Any = None) -> None:
        """Initialize the authentication chain.

        Args:
            app: Optional Flask application instance.  If provided,
                 :meth:`init_app` is called immediately.
        """
        self.app: Any = app
        self.realms: List[Any] = []
        self.logger: logging.Logger = logging.getLogger(__name__)
        self._callbacks_registered: bool = False

        if app is not None:
            self.init_app(app)

    # ------------------------------------------------------------------
    # Flask Extension Init
    # ------------------------------------------------------------------

    def init_app(self, app: Any) -> None:
        """Bind the authentication chain to a Flask application.

        Reads configuration, instantiates realms, registers Flask-HTTPAuth
        callbacks, and stores the chain in ``app.extensions['auth_chain']``
        for retrieval by the module-level ``authenticate_request()``
        convenience function.

        Args:
            app: The Flask application instance to initialize with.

        Configuration Keys:
            ``AUTH_REALMS`` (list[str]):
                Ordered list of enabled realm names.  Defaults to
                ``['local', 'bearer_token', 'jwt']``.

        Side Effects:
            - Calls :func:`_register_builtin_realms` to populate the
              global realm registry (idempotent).
            - Registers ``@basic_auth.verify_password`` and
              ``@token_auth.verify_token`` callbacks.
            - Stores ``self`` in ``app.extensions['auth_chain']``.
        """
        # Lazy imports to avoid circular dependencies at module load time.
        # Realm sub-modules import from src.app.auth.realms (RealmBase),
        # which in turn imports from src.app.models.user; this chain is
        # safe because none of them import authentication.py.  However,
        # keeping imports deferred follows the AAP guidance for realm imports.
        from src.app.auth.realms import (
            _register_builtin_realms,
            realm_registry,
        )
        from src.app.auth.realms.bearer_token_realm import BearerTokenRealm
        from src.app.auth.realms.jwt_realm import JWTRealm
        from src.app.auth.realms.ldap_realm import LDAPRealm
        from src.app.auth.realms.local_realm import LocalRealm
        from src.app.auth.realms.sso_realm import SSORealm

        self.app = app

        # Ensure built-in realms are registered (idempotent — re-registration
        # silently overwrites existing entries in the registry).
        try:
            _register_builtin_realms()
        except Exception:
            self.logger.exception(
                "Failed to register built-in authentication realms."
            )

        # Read the ordered list of enabled realm names from app config.
        enabled_realms: List[str] = app.config.get(
            "AUTH_REALMS", list(self.DEFAULT_REALM_ORDER)
        )

        # Obtain configured, instantiated realm instances from the registry.
        # get_ordered_realms() filters out realms that are not registered or
        # whose is_configured() returns False.
        self.realms = realm_registry.get_ordered_realms(enabled_realms)

        # Register Flask-HTTPAuth callbacks for Basic and Bearer schemes.
        self._register_auth_callbacks()

        # Store in app extensions for retrieval by the convenience function.
        app.extensions["auth_chain"] = self

        self.logger.info(
            "Authentication chain initialized with %d realm(s): [%s]",
            len(self.realms),
            ", ".join(r.get_realm_name() for r in self.realms),
        )

    # ------------------------------------------------------------------
    # Flask-HTTPAuth Callback Registration
    # ------------------------------------------------------------------

    def _register_auth_callbacks(self) -> None:
        """Register verification callbacks with Flask-HTTPAuth extensions.

        Registers two callbacks:

        - ``@basic_auth.verify_password`` — Invoked when a client sends
          ``Authorization: Basic <base64>`` headers.  Decodes the header
          into username/password and delegates to :meth:`_authenticate_basic`.

        - ``@token_auth.verify_token`` — Invoked when a client sends
          ``Authorization: Bearer <token>`` headers.  Delegates the raw
          token string to :meth:`_authenticate_token`.

        The callbacks return a :class:`User` on success or ``None`` on
        failure, which Flask-HTTPAuth uses to set ``auth.current_user()``.

        This method is idempotent; repeated calls are silently ignored.
        """
        if self._callbacks_registered:
            self.logger.debug(
                "Auth callbacks already registered; skipping re-registration."
            )
            return

        # Capture ``self`` (the chain instance) in the closure so that
        # the inner functions can call instance methods.
        chain: AuthenticationChain = self

        @basic_auth.verify_password
        def _verify_password(
            username: str, password: str
        ) -> Optional[User]:
            """Flask-HTTPAuth Basic auth callback."""
            return chain._authenticate_basic(username, password)

        @token_auth.verify_token
        def _verify_token(token: str) -> Optional[User]:
            """Flask-HTTPAuth Bearer token callback."""
            return chain._authenticate_token(token)

        self._callbacks_registered = True
        self.logger.debug(
            "Flask-HTTPAuth callbacks registered for basic_auth and "
            "token_auth."
        )

    # ------------------------------------------------------------------
    # Main Authentication Entry Point
    # ------------------------------------------------------------------

    def authenticate_request(self) -> AuthenticationResult:
        """Authenticate the current incoming Flask request.

        This is the main entry point for authenticating requests.  It
        examines the ``Authorization`` header, determines the credential
        type, and delegates to the appropriate realm-iteration method.

        **Credential Routing:**

        - ``Authorization: Basic <base64>`` → :meth:`_authenticate_basic`
        - ``Authorization: Bearer <token>`` → :meth:`_authenticate_token`
        - ``NX-ANTI-CSRF-TOKEN`` header    → :meth:`_authenticate_token`
        - No credentials                   → :meth:`_handle_anonymous_access`

        Returns:
            An :class:`AuthenticationResult` indicating success or failure.
        """
        auth_header: str = request.headers.get("Authorization", "").strip()

        # ----- Basic Authentication (username : password) -----
        if auth_header.upper().startswith("BASIC "):
            return self._handle_basic_auth_header(auth_header)

        # ----- Bearer Token Authentication (API key or JWT) -----
        if auth_header.upper().startswith("BEARER "):
            token: str = auth_header[7:].strip()
            return self._handle_bearer_auth(token)

        # ----- NX-ANTI-CSRF-TOKEN header (session token) -----
        nx_token: Optional[str] = request.headers.get("NX-ANTI-CSRF-TOKEN")
        if nx_token:
            return self._handle_bearer_auth(nx_token)

        # ----- No credentials — attempt anonymous access -----
        return self._handle_anonymous_access()

    # ------------------------------------------------------------------
    # Private Authentication Helpers
    # ------------------------------------------------------------------

    def _handle_basic_auth_header(
        self, auth_header: str
    ) -> AuthenticationResult:
        """Decode a Basic Authorization header and authenticate.

        Args:
            auth_header: The full ``Authorization: Basic <base64>`` string.

        Returns:
            An :class:`AuthenticationResult`.
        """
        encoded: str = auth_header[6:].strip()  # Strip "Basic "
        try:
            decoded: str = base64.b64decode(encoded).decode("utf-8")
            if ":" not in decoded:
                self.logger.warning(
                    "Malformed Basic auth header: missing colon separator."
                )
                return AuthenticationResult(
                    authenticated=False,
                    error_message="Malformed Basic auth header",
                )
            username, password = decoded.split(":", 1)
        except Exception:
            self.logger.warning(
                "Failed to decode Basic auth header (invalid base64)."
            )
            return AuthenticationResult(
                authenticated=False,
                error_message="Malformed Basic auth header",
            )

        user: Optional[User] = self._authenticate_basic(username, password)
        if user is not None:
            return AuthenticationResult(
                authenticated=True,
                user=user,
                realm_name=getattr(g, "_auth_realm_name", "local"),
            )

        return AuthenticationResult(
            authenticated=False,
            error_message="Invalid username or password",
        )

    def _handle_bearer_auth(self, token: str) -> AuthenticationResult:
        """Authenticate a Bearer token (API key or JWT).

        Args:
            token: The raw token string (after stripping ``Bearer ``).

        Returns:
            An :class:`AuthenticationResult`.
        """
        if not token:
            return AuthenticationResult(
                authenticated=False,
                error_message="Empty bearer token",
            )

        user: Optional[User] = self._authenticate_token(token)
        if user is not None:
            return AuthenticationResult(
                authenticated=True,
                user=user,
                realm_name=getattr(g, "_auth_realm_name", "bearer_token"),
            )

        return AuthenticationResult(
            authenticated=False,
            error_message="Invalid or expired bearer token",
        )

    # ==================================================================
    # Realm-Iteration Authentication Methods
    # ==================================================================

    def _authenticate_basic(
        self, username: str, password: str
    ) -> Optional[User]:
        """Authenticate via username / password through all supporting realms.

        Iterates through the configured realm list using the
        **first-successful** strategy.  Each realm that supports
        ``{'username': ..., 'password': ...}`` credentials is tried in
        order.  The first realm to return a :class:`User` wins.

        On success:
            - Sets ``g.current_user`` and ``g._auth_realm_name``.
            - Emits the :data:`auth_success` Blinker signal.
            - Logs the successful authentication.

        On failure (all realms exhausted):
            - Emits the :data:`auth_failure` Blinker signal.
            - Logs the failed attempt.

        Args:
            username: The login identifier.
            password: The plaintext password (NEVER logged).

        Returns:
            The authenticated :class:`User` or ``None``.
        """
        if not username or not password:
            return None

        credentials: Dict[str, Any] = {
            "username": username,
            "password": password,
        }

        client_ip: str = request.remote_addr or "unknown"

        for realm in self.realms:
            realm_name: str = realm.get_realm_name()

            if not realm.supports(credentials):
                continue

            self.logger.debug(
                "Attempting basic authentication for user '%s' via "
                "realm '%s'.",
                username,
                realm_name,
            )

            try:
                user: Optional[User] = realm.authenticate(credentials)
            except Exception:
                self.logger.exception(
                    "Unexpected error in realm '%s' during basic auth "
                    "for user '%s'.",
                    realm_name,
                    username,
                )
                continue

            if user is not None:
                # ---- SUCCESS ----
                g.current_user = user
                g._auth_realm_name = realm_name  # type: ignore[attr-defined]

                self._emit_auth_success(
                    user=user, realm_name=realm_name, ip=client_ip
                )

                self.logger.info(
                    "Authentication successful: user='%s', realm='%s', "
                    "ip='%s'.",
                    user.user_id,
                    realm_name,
                    client_ip,
                )
                return user

        # ---- ALL REALMS FAILED ----
        self._emit_auth_failure(
            username=username,
            realm_name="basic",
            reason="All realms rejected credentials",
            ip=client_ip,
        )

        self.logger.info(
            "Authentication failed: user='%s', ip='%s' "
            "(all basic-auth realms exhausted).",
            username,
            client_ip,
        )
        return None

    def _authenticate_token(self, token: str) -> Optional[User]:
        """Authenticate via bearer token through all supporting realms.

        Iterates through the configured realm list using the
        **first-successful** strategy.  Each realm that supports
        ``{'token': ...}`` credentials is tried in order.

        The ``BearerTokenRealm`` (API key) is tried before ``JWTRealm``
        because API key lookup is a fast hash comparison, while JWT
        validation involves cryptographic signature verification.

        On success:
            - Sets ``g.current_user`` and ``g._auth_realm_name``.
            - Emits the :data:`auth_success` Blinker signal.

        On failure:
            - Emits the :data:`auth_failure` Blinker signal.

        Args:
            token: The raw bearer token string (NEVER logged).

        Returns:
            The authenticated :class:`User` or ``None``.
        """
        if not token:
            return None

        credentials: Dict[str, Any] = {"token": token}
        client_ip: str = request.remote_addr or "unknown"

        for realm in self.realms:
            realm_name: str = realm.get_realm_name()

            if not realm.supports(credentials):
                continue

            self.logger.debug(
                "Attempting token authentication via realm '%s'.",
                realm_name,
            )

            try:
                user: Optional[User] = realm.authenticate(credentials)
            except Exception:
                self.logger.exception(
                    "Unexpected error in realm '%s' during token auth.",
                    realm_name,
                )
                continue

            if user is not None:
                # ---- SUCCESS ----
                g.current_user = user
                g._auth_realm_name = realm_name  # type: ignore[attr-defined]

                self._emit_auth_success(
                    user=user, realm_name=realm_name, ip=client_ip
                )

                self.logger.info(
                    "Token authentication successful: user='%s', "
                    "realm='%s', ip='%s'.",
                    user.user_id,
                    realm_name,
                    client_ip,
                )
                return user

        # ---- ALL REALMS FAILED ----
        self._emit_auth_failure(
            username="<token>",
            realm_name="token",
            reason="No realm accepted the bearer token",
            ip=client_ip,
        )

        self.logger.info(
            "Token authentication failed: ip='%s' "
            "(all token-auth realms exhausted).",
            client_ip,
        )
        return None

    def _authenticate_ldap(
        self, username: str, password: str
    ) -> Optional[User]:
        """Authenticate directly via the LDAP realm.

        This is a specialised entry point that targets the LDAP realm
        specifically, bypassing the general realm iteration.  Used when
        the caller knows the credentials should be verified against LDAP
        (e.g. an explicit LDAP login form).

        Args:
            username: The login identifier.
            password: The plaintext password (NEVER logged).

        Returns:
            The authenticated :class:`User` or ``None``.
        """
        if not username or not password:
            return None

        credentials: Dict[str, Any] = {
            "username": username,
            "password": password,
        }
        client_ip: str = request.remote_addr or "unknown"

        for realm in self.realms:
            realm_name: str = realm.get_realm_name()
            if realm_name != "ldap":
                continue

            # Check if realm is configured before attempting auth.
            if hasattr(realm, "is_configured") and not realm.is_configured():
                self.logger.debug(
                    "LDAP realm is not configured; skipping."
                )
                return None

            if not realm.supports(credentials):
                return None

            self.logger.debug(
                "Attempting LDAP authentication for user '%s'.",
                username,
            )

            try:
                user: Optional[User] = realm.authenticate(credentials)
            except Exception:
                self.logger.exception(
                    "LDAP authentication error for user '%s'.",
                    username,
                )
                return None

            if user is not None:
                g.current_user = user
                g._auth_realm_name = "ldap"  # type: ignore[attr-defined]

                self._emit_auth_success(
                    user=user, realm_name="ldap", ip=client_ip
                )

                self.logger.info(
                    "LDAP authentication successful: user='%s', ip='%s'.",
                    user.user_id,
                    client_ip,
                )
                return user

            # LDAP realm found but auth failed
            self._emit_auth_failure(
                username=username,
                realm_name="ldap",
                reason="LDAP bind or user lookup failed",
                ip=client_ip,
            )
            return None

        self.logger.debug("LDAP realm not found in the configured realm list.")
        return None

    def _authenticate_sso(self, token: str) -> Optional[User]:
        """Authenticate directly via the SSO realm.

        This is a specialised entry point that targets the SSO realm
        specifically, handling both SAML 2.0 assertions and OIDC id_tokens.
        Typically called from SSO callback endpoints (``/auth/saml/callback``
        or ``/auth/oidc/callback``).

        Args:
            token: The SSO credential — either a base64-encoded SAML
                   assertion or an OIDC id_token JWT string.

        Returns:
            The authenticated :class:`User` or ``None``.
        """
        if not token:
            return None

        # Determine credential type based on token structure.
        # OIDC tokens are JWTs (contain exactly two dots).
        # SAML assertions are base64-encoded XML (no dots typically).
        if token.count(".") == 2:
            credentials: Dict[str, Any] = {"oidc_token": token}
        else:
            credentials = {"saml_assertion": token}

        client_ip: str = request.remote_addr or "unknown"

        for realm in self.realms:
            realm_name: str = realm.get_realm_name()
            if realm_name != "sso":
                continue

            if hasattr(realm, "is_configured") and not realm.is_configured():
                self.logger.debug(
                    "SSO realm is not configured; skipping."
                )
                return None

            if not realm.supports(credentials):
                self.logger.debug(
                    "SSO realm does not support the provided credential type."
                )
                return None

            self.logger.debug("Attempting SSO authentication.")

            try:
                user: Optional[User] = realm.authenticate(credentials)
            except Exception:
                self.logger.exception("SSO authentication error.")
                return None

            if user is not None:
                g.current_user = user
                g._auth_realm_name = "sso"  # type: ignore[attr-defined]

                self._emit_auth_success(
                    user=user, realm_name="sso", ip=client_ip
                )

                self.logger.info(
                    "SSO authentication successful: user='%s', ip='%s'.",
                    user.user_id,
                    client_ip,
                )
                return user

            self._emit_auth_failure(
                username="<sso>",
                realm_name="sso",
                reason="SSO assertion/token validation failed",
                ip=client_ip,
            )
            return None

        self.logger.debug("SSO realm not found in the configured realm list.")
        return None

    # ------------------------------------------------------------------
    # Anonymous Access Support
    # ------------------------------------------------------------------

    def _handle_anonymous_access(self) -> AuthenticationResult:
        """Handle requests with no credentials by checking anonymous access.

        If the ``security.anonymousAccess`` system configuration flag is
        ``true``, authenticates the request as the ``anonymous`` user with
        privileges defined by the ``nx-anonymous`` role.

        Returns:
            An :class:`AuthenticationResult` — authenticated as anonymous
            if enabled, or unauthenticated with an error message.
        """
        from src.app.models.system_config import SystemConfig

        try:
            anon_config: Optional[str] = SystemConfig.get_value(
                "security.anonymousAccess", default="false"
            )
            anon_enabled: bool = str(anon_config).lower() in (
                "true",
                "1",
                "yes",
                "on",
            )
        except Exception:
            self.logger.debug(
                "Could not read anonymous access configuration; "
                "defaulting to disabled.",
                exc_info=True,
            )
            anon_enabled = False

        if anon_enabled:
            try:
                anon_user: Optional[User] = User.query.filter_by(
                    user_id=ANONYMOUS_USER_ID
                ).first()
            except Exception:
                self.logger.debug(
                    "Database error while looking up anonymous user.",
                    exc_info=True,
                )
                anon_user = None

            if anon_user is not None and anon_user.status == "active":
                g.current_user = anon_user
                self.logger.debug(
                    "Anonymous access granted for request from ip='%s'.",
                    request.remote_addr or "unknown",
                )
                return AuthenticationResult(
                    authenticated=True,
                    user=anon_user,
                    realm_name="anonymous",
                )

            self.logger.debug(
                "Anonymous access is enabled but no active anonymous "
                "user found in the database."
            )

        return AuthenticationResult(
            authenticated=False,
            error_message="Authentication required",
        )

    # ------------------------------------------------------------------
    # Signal Emission Helpers
    # ------------------------------------------------------------------

    def _emit_auth_success(
        self,
        user: User,
        realm_name: str,
        ip: str,
    ) -> None:
        """Emit the ``auth_success`` Blinker signal.

        Wrapped in a try/except to ensure that a subscriber error never
        breaks the authentication flow.

        Args:
            user:       The authenticated user.
            realm_name: Name of the realm that succeeded.
            ip:         Client IP address.
        """
        try:
            auth_success.send(
                current_app._get_current_object(),
                user=user,
                realm=realm_name,
                ip=ip,
            )
        except Exception:
            self.logger.debug(
                "Error emitting auth_success signal.",
                exc_info=True,
            )

    def _emit_auth_failure(
        self,
        username: str,
        realm_name: str,
        reason: str,
        ip: str,
    ) -> None:
        """Emit the ``auth_failure`` Blinker signal.

        Wrapped in a try/except to ensure that a subscriber error never
        breaks the authentication flow.

        Args:
            username:   The attempted username or ``'<token>'``.
            realm_name: Name of the realm that was attempted.
            reason:     Human-readable failure reason.
            ip:         Client IP address.
        """
        try:
            auth_failure.send(
                current_app._get_current_object(),
                username=username,
                realm=realm_name,
                reason=reason,
                ip=ip,
            )
        except Exception:
            self.logger.debug(
                "Error emitting auth_failure signal.",
                exc_info=True,
            )


# ===========================================================================
# Module-Level Convenience Functions
# ===========================================================================


def authenticate_request() -> AuthenticationResult:
    """Authenticate the current request via the application auth chain.

    This is a module-level convenience function intended for use in Flask
    ``before_request`` hooks and by the :func:`login_required` decorator.
    It retrieves the :class:`AuthenticationChain` from
    ``current_app.extensions['auth_chain']`` and delegates to its
    :meth:`~AuthenticationChain.authenticate_request` method.

    Returns:
        An :class:`AuthenticationResult` indicating success or failure.
        If the authentication chain has not been initialized, returns
        a failure result with an appropriate error message.
    """
    chain: Optional[AuthenticationChain] = current_app.extensions.get(
        "auth_chain"
    )
    if chain is not None:
        return chain.authenticate_request()
    logger.error(
        "authenticate_request() called but AuthenticationChain is not "
        "registered in app.extensions. Ensure init_app() was called."
    )
    return AuthenticationResult(
        authenticated=False,
        error_message="Authentication chain not initialized",
    )


def login_required(f: Any) -> Any:
    """Decorator to require authentication for a Flask view function.

    Authenticates the current request.  If authentication fails, the
    request is aborted with HTTP 401 Unauthorized.  On success, the
    authenticated :class:`User` is stored in ``g.current_user`` for use
    by the decorated view function.

    Usage::

        @app.route('/api/protected')
        @login_required
        def protected_endpoint():
            user = g.current_user
            return jsonify({"user": user.user_id})

    Args:
        f: The view function to wrap.

    Returns:
        The wrapped function.
    """

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        result: AuthenticationResult = authenticate_request()
        if not result.authenticated:
            abort(401, description=result.error_message or "Authentication required")
        g.current_user = result.user
        return f(*args, **kwargs)

    return decorated_function


def get_current_user() -> Optional[User]:
    """Get the currently authenticated user from the Flask ``g`` context.

    Returns:
        The authenticated :class:`User` if present, or ``None`` if the
        request has not been authenticated.
    """
    return getattr(g, "current_user", None)


# ===========================================================================
# Public Export List
# ===========================================================================

__all__: list[str] = [
    "authenticate_request",
    "AuthenticationChain",
    "login_required",
    "get_current_user",
    "AuthenticationResult",
    "auth_success",
    "auth_failure",
]

# ---------------------------------------------------------------------------
# Module Load Logging
# ---------------------------------------------------------------------------

logger.debug(
    "Authentication module loaded — exports: %s",
    ", ".join(__all__),
)
