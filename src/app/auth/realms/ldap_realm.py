"""
LDAP/Active Directory Authentication Realm.

This module implements the ``LDAPRealm`` class that provides enterprise
directory authentication by performing LDAP bind operations against an
external LDAP/Active Directory server.  It replaces the Apache Shiro 2.0.0
LDAP realm from the original Java source system (AAP Section 0.2.2).

**Realm Chain Position:**
    Position 4 in the multi-backend authentication chain:
    ``local → bearer_token → jwt → ldap → sso``

**Authentication Strategies:**
    - **Direct bind**: Constructs the user DN from a template and binds
      directly.  Suitable for LDAP directories with predictable DN formats.
    - **Search-then-bind**: Uses a system/service account to search for the
      user's DN, then binds with the found DN and the user's password.
      Required for Active Directory or directories with complex DN structures.

**Auto-Provisioning:**
    On first successful LDAP authentication, a local ``User`` record is
    created automatically in the application database.  On subsequent logins,
    the user's profile attributes (email, display name) are updated from the
    directory.

**Group-to-Role Mapping (Feature F-301):**
    LDAP group memberships (``memberOf`` attribute or group search) are mapped
    to local ``Role`` objects via the configurable ``LDAP_GROUP_ROLE_MAPPING``
    dictionary.  This implements Role-Based Access Control (RBAC) for
    LDAP-authenticated users.

**Active Directory Compatibility:**
    Supports ``sAMAccountName``, ``userPrincipalName``, and ``memberOf``
    attributes.  The ``user_dn_template`` config supports AD-style
    ``{username}@{domain}`` patterns.

**Security:**
    - LDAPS (SSL) and StartTLS connection security are both supported.
    - LDAP injection is prevented via ``ldap.filter.escape_filter_chars()``.
    - Passwords and LDAP credentials are **never** logged.

**Graceful Degradation:**
    If ``python-ldap`` (3.4.4) is not installed, the module exports
    ``LDAP_AVAILABLE = False`` and the realm disables itself without crashing.

**Configuration (Flask app.config keys):**
    - ``LDAP_SERVER_URL``          — LDAP server URI (e.g. ``ldap://ldap.example.com:389``)
    - ``LDAP_USE_STARTTLS``        — Enable StartTLS (bool, default ``False``)
    - ``LDAP_USE_SSL``             — Enable LDAPS (bool, default ``False``)
    - ``LDAP_USER_BASE_DN``        — User search base DN
    - ``LDAP_USER_DN_TEMPLATE``    — DN template for direct bind
    - ``LDAP_USER_FILTER``         — Search filter for search-then-bind
    - ``LDAP_BIND_STRATEGY``       — ``'direct'`` or ``'search_then_bind'``
    - ``LDAP_SYSTEM_DN``           — System account DN (for search-then-bind)
    - ``LDAP_SYSTEM_PASSWORD``     — System account password
    - ``LDAP_GROUP_BASE_DN``       — Group search base DN
    - ``LDAP_GROUP_FILTER``        — Group membership search filter
    - ``LDAP_USER_ATTRIBUTES``     — List of attributes to fetch
    - ``LDAP_TIMEOUT``             — Network timeout in seconds (default ``10``)
    - ``LDAP_GROUP_ROLE_MAPPING``  — Dict mapping LDAP group DNs to role IDs

Exports:
    LDAPRealm       : Concrete realm implementation for LDAP/AD authentication.
    LDAP_AVAILABLE  : Boolean flag indicating whether python-ldap is installed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# python-ldap import with graceful degradation (AAP Section 0.6.1)
# ---------------------------------------------------------------------------
# python-ldap 3.4.4 replaces Apache Shiro 2.0.0 LDAP realm from the Java
# source system.  If the package is not installed, the realm disables itself
# via the LDAP_AVAILABLE flag and all LDAP operations are no-ops.
# ---------------------------------------------------------------------------

try:
    import ldap  # type: ignore[import-untyped]
    from ldap.filter import escape_filter_chars  # type: ignore[import-untyped]

    LDAP_AVAILABLE: bool = True
except ImportError:
    LDAP_AVAILABLE = False

    # Provide a stub for escape_filter_chars so type-checkers and code paths
    # that reference it without actually executing do not raise NameError.
    def escape_filter_chars(assertion_value: str, *args: Any, **kwargs: Any) -> str:  # type: ignore[misc]
        """Stub: python-ldap is not installed."""
        return assertion_value

from flask import current_app

from src.app.auth.realms import RealmBase
from src.app.extensions import db
from src.app.models.role import Role
from src.app.models.user import User

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "LDAPRealm",
    "LDAP_AVAILABLE",
]


# ===========================================================================
# LDAPRealm — LDAP/Active Directory Authentication Realm
# ===========================================================================


class LDAPRealm(RealmBase):
    """LDAP/Active Directory authentication realm.

    Extends :class:`~src.app.auth.realms.RealmBase` and implements the
    three abstract methods (``authenticate``, ``supports``,
    ``get_realm_name``), plus overrides ``is_configured`` to verify that
    the LDAP server URL and user base DN are present in the Flask app
    configuration.

    This realm is position **4** in the authentication chain:
    ``local → bearer_token → jwt → ldap → sso``

    It supports both **direct bind** and **search-then-bind** strategies
    and provides auto-provisioning of local ``User`` records on first
    LDAP login as well as LDAP-group-to-local-role mapping.
    """

    # ------------------------------------------------------------------
    # Class-Level Attributes
    # ------------------------------------------------------------------

    REALM_NAME: str = "ldap"
    """Unique realm identifier used by the authentication chain and registry."""

    SUPPORTED_CREDENTIAL_TYPES: List[str] = ["username_password"]
    """Credential types this realm can authenticate."""

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialize the LDAP realm.

        Calls :meth:`RealmBase.__init__` to set up the structured logger
        and initialises the connection-pool placeholder.  LDAP connections
        are managed per-request (bind → operate → unbind).
        """
        super().__init__()
        self.logger: logging.Logger = logging.getLogger(__name__)
        self._connection_pool: Optional[Any] = None

    # ==================================================================
    # RealmBase Interface Implementation
    # ==================================================================

    def authenticate(self, credentials: Dict[str, Any]) -> Optional[User]:
        """Attempt to authenticate via LDAP bind.

        This is the main entry point called by the authentication chain
        when :meth:`supports` returns ``True``.

        Workflow:
            1. Extract ``username`` and ``password`` from *credentials*.
            2. Validate that LDAP is available and configured.
            3. Perform an LDAP bind (direct or search-then-bind).
            4. On success, search for user attributes in the directory.
            5. Resolve or auto-create a local ``User`` record.
            6. Map LDAP group memberships to local roles.
            7. Return the local ``User`` object.

        Args:
            credentials: Dictionary with ``'username'`` and ``'password'``
                keys.

        Returns:
            A :class:`~src.app.models.user.User` on success; ``None``
            on failure (invalid credentials, LDAP unavailable, or error).

        Note:
            Passwords are **never** logged.
        """
        username: Optional[str] = credentials.get("username")
        password: Optional[str] = credentials.get("password")

        # Guard: both username and password must be present
        if not username or not password:
            return None

        # Guard: python-ldap must be installed
        if not LDAP_AVAILABLE:
            self.logger.warning(
                "LDAP authentication unavailable: python-ldap is not installed."
            )
            return None

        # Guard: LDAP must be configured
        if not self.is_configured():
            self.logger.debug(
                "LDAP realm is not configured; skipping authentication."
            )
            return None

        conn: Optional[Any] = None
        try:
            # Step 1 & 2: Bind user
            bind_success, conn = self._bind_user(username, password)
            if not bind_success or conn is None:
                return None

            self.logger.info(
                "LDAP bind successful for user: %s", username
            )

            # Step 3: Retrieve user attributes from LDAP
            config: Dict[str, Any] = self._get_ldap_config()

            # Determine the user DN for attribute/group lookup
            if config.get("bind_strategy", "direct") == "search_then_bind":
                # For search-then-bind, we need to re-search for the DN
                # because the bind already succeeded.  The conn is now
                # bound as the user.
                user_dn: Optional[str] = self._search_user_dn_as_user(
                    conn, username, config
                )
            else:
                dn_template: str = config.get(
                    "user_dn_template", "uid={username},{base_dn}"
                )
                user_dn = dn_template.format(
                    username=escape_filter_chars(username),
                    base_dn=config.get("user_base_dn", ""),
                )

            if user_dn is None:
                self.logger.warning(
                    "Could not determine user DN for: %s", username
                )
                return None

            # Step 4: Fetch user attributes
            ldap_attributes: Dict[str, Any] = self._search_user_attributes(
                conn, user_dn, config
            )

            # Step 5: Fetch group memberships
            ldap_groups: List[str] = self._search_user_groups(
                conn, user_dn, config
            )

            # Step 6: Resolve or create local user record
            user: User = self._resolve_or_create_user(
                username, ldap_attributes
            )

            # Step 7: Map LDAP groups → local roles
            self._map_ldap_groups_to_roles(user, ldap_groups)

            self.logger.info(
                "LDAP authentication succeeded for user: %s "
                "(groups=%d, attributes=%d)",
                username,
                len(ldap_groups),
                len(ldap_attributes),
            )
            return user

        except Exception:
            self.logger.exception(
                "Unexpected error during LDAP authentication for user: %s",
                username,
            )
            return None
        finally:
            self._close_connection(conn)

    def supports(self, credentials: Dict[str, Any]) -> bool:
        """Check whether this realm can handle the supplied credentials.

        Returns ``True`` only if:
            1. ``python-ldap`` is installed (``LDAP_AVAILABLE``).
            2. The LDAP realm is configured (server URL + base DN).
            3. The credentials contain both ``'username'`` and ``'password'``.

        Args:
            credentials: Dictionary of authentication credentials.

        Returns:
            ``True`` if this realm should attempt authentication; ``False``
            otherwise.
        """
        if not LDAP_AVAILABLE:
            return False

        if not self.is_configured():
            return False

        has_username: bool = bool(credentials.get("username"))
        has_password: bool = bool(credentials.get("password"))
        return has_username and has_password

    def get_realm_name(self) -> str:
        """Return the unique name identifier for this realm.

        Returns:
            ``'ldap'`` — the canonical realm identifier.
        """
        return self.REALM_NAME

    def is_configured(self) -> bool:
        """Check whether the LDAP realm has the minimum required config.

        Returns ``True`` if **all** of the following conditions are met:
            1. ``python-ldap`` is installed.
            2. ``LDAP_SERVER_URL`` is set in the Flask app configuration.
            3. ``LDAP_USER_BASE_DN`` is set in the Flask app configuration.

        Returns:
            ``True`` if the realm is ready for use; ``False`` otherwise.
        """
        if not LDAP_AVAILABLE:
            return False

        try:
            server_url: str = current_app.config.get("LDAP_SERVER_URL", "")
            user_base_dn: str = current_app.config.get("LDAP_USER_BASE_DN", "")
            return bool(server_url) and bool(user_base_dn)
        except RuntimeError:
            # Outside of Flask application context — cannot read config.
            return False

    # ==================================================================
    # LDAP Connection Management
    # ==================================================================

    def _create_connection(self) -> Any:
        """Create and configure a new LDAP connection.

        Reads the LDAP server URL, protocol version, referral handling,
        network timeout, and TLS settings from Flask app configuration.

        Returns:
            An initialised (but unbound) LDAP connection object.

        Raises:
            ldap.LDAPError: If the server cannot be contacted.
        """
        config: Dict[str, Any] = self._get_ldap_config()
        server_url: str = config["server_url"]

        conn = ldap.initialize(server_url)

        # Protocol settings
        conn.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
        conn.set_option(ldap.OPT_REFERRALS, 0)
        conn.set_option(
            ldap.OPT_NETWORK_TIMEOUT, config.get("timeout", 10)
        )

        # TLS certificate verification (CWE-295): enforce cert validation
        # for both StartTLS and LDAPS connections to prevent MITM attacks
        # on LDAP credential transmission.
        use_ssl: bool = config.get("use_ssl", False)
        use_starttls: bool = config.get("use_starttls", False)

        if use_ssl or use_starttls:
            # Demand full certificate verification by default
            conn.set_option(
                ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND
            )

            # Allow configurable CA certificate path for custom CAs
            ca_cert_file: str = config.get("ca_cert_file", "")
            if ca_cert_file:
                conn.set_option(ldap.OPT_X_TLS_CACERTFILE, ca_cert_file)
                self.logger.debug(
                    "LDAP TLS: using custom CA certificate: %s", ca_cert_file
                )

            # Apply TLS options by creating a new TLS context
            conn.set_option(ldap.OPT_X_TLS_NEWCTX, 0)

        # StartTLS upgrade (if configured and not already using ldaps://)
        if use_starttls:
            conn.start_tls_s()
            self.logger.debug("StartTLS negotiation completed successfully.")

        return conn

    def _bind_user(
        self, username: str, password: str
    ) -> Tuple[bool, Optional[Any]]:
        """Attempt to bind (authenticate) as the specified user.

        Supports two strategies selected via ``LDAP_BIND_STRATEGY``:

        **Direct bind** (default):
            Constructs the user DN from ``LDAP_USER_DN_TEMPLATE`` and
            performs ``simple_bind_s()`` with the user's password.

        **Search-then-bind**:
            Binds with a system/service account, searches for the user DN
            using ``LDAP_USER_FILTER``, then re-binds with the found DN
            and the user's password.

        Args:
            username: The username to authenticate.
            password: The user's plaintext password (**never logged**).

        Returns:
            A ``(success, connection)`` tuple.  On success, the
            connection is bound as the authenticated user.  On failure,
            ``(False, None)`` is returned.
        """
        config: Dict[str, Any] = self._get_ldap_config()
        conn: Optional[Any] = None
        try:
            conn = self._create_connection()

            bind_strategy: str = config.get("bind_strategy", "direct")

            if bind_strategy == "search_then_bind":
                # Step 1: Search for the user DN using a system account
                user_dn: Optional[str] = self._search_user_dn(
                    conn, username, config
                )
                if user_dn is None:
                    self.logger.debug(
                        "LDAP user not found via search-then-bind: %s",
                        username,
                    )
                    self._close_connection(conn)
                    return (False, None)

                # Step 2: Re-create connection and bind as the user
                self._close_connection(conn)
                conn = self._create_connection()
            else:
                # Direct bind: construct DN from template
                dn_template: str = config.get(
                    "user_dn_template", "uid={username},{base_dn}"
                )
                user_dn = dn_template.format(
                    username=escape_filter_chars(username),
                    base_dn=config.get("user_base_dn", ""),
                )

            # Perform the actual user bind
            conn.simple_bind_s(user_dn, password)
            return (True, conn)

        except ldap.INVALID_CREDENTIALS:
            self.logger.debug(
                "LDAP invalid credentials for user: %s", username
            )
            self._close_connection(conn)
            return (False, None)

        except ldap.SERVER_DOWN:
            self.logger.error(
                "LDAP server is unreachable: %s",
                config.get("server_url", "unknown"),
            )
            self._close_connection(conn)
            return (False, None)

        except ldap.LDAPError as exc:
            self.logger.error(
                "LDAP error during bind for user '%s': %s",
                username,
                exc,
            )
            self._close_connection(conn)
            return (False, None)

    # ==================================================================
    # LDAP User Search Methods
    # ==================================================================

    def _search_user_dn(
        self,
        conn: Any,
        username: str,
        config: Dict[str, Any],
    ) -> Optional[str]:
        """Search for a user's Distinguished Name using a system account.

        Used by the **search-then-bind** strategy.  Binds with the
        configured system account, then searches for the user using
        ``LDAP_USER_FILTER``.

        Args:
            conn: An initialised (unbound) LDAP connection.
            username: The username to search for.
            config: LDAP configuration dictionary.

        Returns:
            The user's DN string if found; ``None`` otherwise.
        """
        system_dn: str = config.get("system_dn", "")
        system_password: str = config.get("system_password", "")

        if not system_dn:
            self.logger.error(
                "Search-then-bind strategy requires LDAP_SYSTEM_DN to be set."
            )
            return None

        try:
            # Bind as the system account
            conn.simple_bind_s(system_dn, system_password)

            # Build the search filter with injection prevention
            user_filter: str = config.get(
                "user_filter", "(uid={username})"
            )
            search_filter: str = user_filter.format(
                username=escape_filter_chars(username)
            )
            base_dn: str = config.get("user_base_dn", "")

            result = conn.search_s(
                base_dn, ldap.SCOPE_SUBTREE, search_filter, ["dn"]
            )

            # Filter out referral entries (dn is None for referrals)
            entries = [
                (dn, attrs) for dn, attrs in result if dn is not None
            ]

            if not entries:
                self.logger.debug(
                    "LDAP user not found: %s (filter=%s, base=%s)",
                    username,
                    search_filter,
                    base_dn,
                )
                return None

            if len(entries) > 1:
                self.logger.warning(
                    "LDAP search returned %d entries for user '%s'; "
                    "using the first entry.",
                    len(entries),
                    username,
                )

            user_dn: str = entries[0][0]
            self.logger.debug(
                "LDAP user DN resolved: %s → %s", username, user_dn
            )
            return user_dn

        except ldap.SERVER_DOWN:
            self.logger.error(
                "LDAP server unreachable during user DN search."
            )
            return None

        except ldap.LDAPError as exc:
            self.logger.error(
                "LDAP error searching for user '%s': %s", username, exc
            )
            return None

    def _search_user_dn_as_user(
        self,
        conn: Any,
        username: str,
        config: Dict[str, Any],
    ) -> Optional[str]:
        """Search for the authenticated user's own DN.

        After a successful user bind, the connection is bound as the user.
        This method searches for the user's own entry to retrieve the DN,
        which is needed for attribute and group lookups.

        Args:
            conn: An LDAP connection bound as the authenticated user.
            username: The authenticated username.
            config: LDAP configuration dictionary.

        Returns:
            The user's DN string if found; ``None`` otherwise.
        """
        try:
            user_filter: str = config.get(
                "user_filter", "(uid={username})"
            )
            search_filter: str = user_filter.format(
                username=escape_filter_chars(username)
            )
            base_dn: str = config.get("user_base_dn", "")

            result = conn.search_s(
                base_dn, ldap.SCOPE_SUBTREE, search_filter, ["dn"]
            )

            entries = [
                (dn, attrs) for dn, attrs in result if dn is not None
            ]

            if not entries:
                return None

            return entries[0][0]

        except ldap.LDAPError as exc:
            self.logger.error(
                "LDAP error searching for user DN (as user) '%s': %s",
                username,
                exc,
            )
            return None

    def _search_user_attributes(
        self,
        conn: Any,
        user_dn: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Retrieve user attributes from LDAP after a successful bind.

        Fetches configurable attributes (mail, displayName, memberOf, etc.)
        from the user's LDAP entry and returns them as a normalised
        Python dictionary with decoded string values.

        Args:
            conn: An LDAP connection bound as the authenticated user.
            user_dn: The user's Distinguished Name.
            config: LDAP configuration dictionary.

        Returns:
            A dictionary mapping attribute names to their values.
            Multi-valued attributes (e.g. ``memberOf``) are returned as
            lists of strings.  Single-valued attributes are returned as
            strings.  Returns an empty dict on error.
        """
        attrs_to_fetch: List[str] = config.get(
            "user_attributes",
            ["mail", "displayName", "memberOf", "cn", "sn", "givenName"],
        )

        try:
            result = conn.search_s(
                user_dn,
                ldap.SCOPE_BASE,
                "(objectClass=*)",
                attrs_to_fetch,
            )

            if not result:
                return {}

            # LDAP returns a list of (dn, attributes) tuples.  For a
            # SCOPE_BASE search there should be exactly one entry.
            _, raw_attrs = result[0]
            parsed: Dict[str, Any] = {}

            for attr_name, attr_values in raw_attrs.items():
                decoded_values: List[str] = []
                for value in attr_values:
                    if isinstance(value, bytes):
                        decoded_values.append(value.decode("utf-8", errors="replace"))
                    else:
                        decoded_values.append(str(value))

                # For single-valued attributes, unwrap the list
                if len(decoded_values) == 1:
                    parsed[attr_name] = decoded_values[0]
                else:
                    parsed[attr_name] = decoded_values

            self.logger.debug(
                "Retrieved %d LDAP attributes for DN: %s",
                len(parsed),
                user_dn,
            )
            return parsed

        except ldap.LDAPError as exc:
            self.logger.error(
                "LDAP error retrieving attributes for DN '%s': %s",
                user_dn,
                exc,
            )
            return {}

    def _search_user_groups(
        self,
        conn: Any,
        user_dn: str,
        config: Dict[str, Any],
    ) -> List[str]:
        """Retrieve LDAP group memberships for a user.

        Two strategies are supported:

        1. **memberOf attribute** (Active Directory style):
           Read the ``memberOf`` attribute from the user's own entry.
           This is attempted first.

        2. **Group search** (standard LDAP):
           Search group entries whose ``member`` attribute contains the
           user's DN.  Used when the ``memberOf`` attribute is absent.

        Args:
            conn: An LDAP connection bound as the authenticated user.
            user_dn: The user's Distinguished Name.
            config: LDAP configuration dictionary.

        Returns:
            A list of group Distinguished Names (full DNs).  Returns an
            empty list if no groups are found or on error.
        """
        groups: List[str] = []

        # Strategy 1: memberOf attribute on user entry
        try:
            result = conn.search_s(
                user_dn,
                ldap.SCOPE_BASE,
                "(objectClass=*)",
                ["memberOf"],
            )
            if result:
                _, raw_attrs = result[0]
                member_of_values = raw_attrs.get("memberOf", [])
                for value in member_of_values:
                    if isinstance(value, bytes):
                        groups.append(value.decode("utf-8", errors="replace"))
                    else:
                        groups.append(str(value))

        except ldap.LDAPError as exc:
            self.logger.debug(
                "Could not read memberOf for DN '%s': %s", user_dn, exc
            )

        # Strategy 2: Group search (if memberOf yielded nothing)
        if not groups:
            groups = self._search_groups_by_member(conn, user_dn, config)

        self.logger.debug(
            "Resolved %d LDAP groups for DN: %s", len(groups), user_dn
        )
        return groups

    def _search_groups_by_member(
        self,
        conn: Any,
        user_dn: str,
        config: Dict[str, Any],
    ) -> List[str]:
        """Search for groups that contain the user as a member.

        Searches the group base DN using the configured group filter
        (default: ``(member={user_dn})``).

        Args:
            conn: An LDAP connection bound as the authenticated user.
            user_dn: The user's Distinguished Name.
            config: LDAP configuration dictionary.

        Returns:
            A list of group Distinguished Names.
        """
        group_base_dn: str = config.get("group_base_dn", "")
        if not group_base_dn:
            # Fall back to user_base_dn if group_base_dn is not set
            group_base_dn = config.get("user_base_dn", "")

        group_filter: str = config.get(
            "group_filter", "(member={user_dn})"
        )
        search_filter: str = group_filter.format(
            user_dn=escape_filter_chars(user_dn)
        )

        groups: List[str] = []
        try:
            result = conn.search_s(
                group_base_dn,
                ldap.SCOPE_SUBTREE,
                search_filter,
                ["dn"],
            )

            for dn, _ in result:
                if dn is not None:
                    groups.append(dn)

        except ldap.LDAPError as exc:
            self.logger.error(
                "LDAP error searching groups for DN '%s': %s",
                user_dn,
                exc,
            )

        return groups

    # ==================================================================
    # User Provisioning from LDAP
    # ==================================================================

    def _resolve_or_create_user(
        self,
        username: str,
        ldap_attributes: Dict[str, Any],
    ) -> User:
        """Resolve an existing user or auto-create a new one from LDAP.

        On first LDAP login, a local ``User`` record is created with
        profile attributes populated from the LDAP directory.  On
        subsequent logins, the user's attributes are updated from the
        directory to keep them in sync.

        Args:
            username: The authenticated username (``user_id``).
            ldap_attributes: Dictionary of LDAP user attributes.

        Returns:
            The resolved or newly created :class:`User` instance.
        """
        user: Optional[User] = User.query.filter_by(
            user_id=username
        ).first()

        # Extract common LDAP attributes with safe defaults
        email: str = self._extract_attribute(ldap_attributes, "mail", "")
        display_name: str = self._extract_attribute(
            ldap_attributes, "displayName", username
        )
        first_name: str = self._extract_attribute(
            ldap_attributes, "givenName", ""
        )
        last_name: str = self._extract_attribute(ldap_attributes, "sn", "")

        if user is not None:
            # Update existing user with latest LDAP attributes
            if email:
                user.email = email
            if first_name:
                user.first_name = first_name
            if last_name:
                user.last_name = last_name

            # Update JSON attributes
            current_attrs: Dict[str, Any] = user.attributes or {}
            current_attrs["source"] = "ldap"
            current_attrs["display_name"] = display_name
            current_attrs["first_name"] = first_name
            current_attrs["last_name"] = last_name
            user.attributes = current_attrs

            # Ensure active status for LDAP users
            if user.status != "active":
                self.logger.info(
                    "Reactivating LDAP user '%s' (was: %s).",
                    username,
                    user.status,
                )
                user.status = "active"

            db.session.commit()
            self.logger.debug(
                "Updated existing user from LDAP: %s", username
            )
        else:
            # Create a new local user for the first-time LDAP login
            user = User(
                user_id=username,
                email=email,
                status="active",
                password_hash="",  # No local password for LDAP users
                first_name=first_name,
                last_name=last_name,
                attributes={
                    "source": "ldap",
                    "display_name": display_name,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            )
            db.session.add(user)
            db.session.commit()
            self.logger.info(
                "Auto-provisioned new user from LDAP: %s", username
            )

        return user

    def _map_ldap_groups_to_roles(
        self,
        user: User,
        ldap_groups: List[str],
    ) -> None:
        """Map LDAP group memberships to local roles.

        Reads the ``LDAP_GROUP_ROLE_MAPPING`` configuration dictionary
        which maps LDAP group DNs (or CN substrings) to local role IDs.

        Example config::

            LDAP_GROUP_ROLE_MAPPING = {
                "CN=Nexus-Admins,OU=Groups,DC=example,DC=com": "nx-admin",
                "CN=Developers,OU=Groups,DC=example,DC=com": "nx-developer",
            }

        Args:
            user: The :class:`User` instance to assign roles to.
            ldap_groups: List of LDAP group Distinguished Names.
        """
        config: Dict[str, Any] = self._get_ldap_config()
        mapping: Dict[str, str] = config.get("group_role_mapping", {})

        if not mapping:
            self.logger.debug(
                "No LDAP group-to-role mapping configured; skipping."
            )
            return

        roles_assigned: List[str] = []

        for ldap_group_dn in ldap_groups:
            # Exact match on full DN
            role_id: Optional[str] = mapping.get(ldap_group_dn)

            # Case-insensitive match fallback
            if role_id is None:
                ldap_group_dn_lower: str = ldap_group_dn.lower()
                for mapped_dn, mapped_role in mapping.items():
                    if mapped_dn.lower() == ldap_group_dn_lower:
                        role_id = mapped_role
                        break

            if role_id is not None:
                role: Optional[Role] = Role.query.filter_by(
                    role_id=role_id
                ).first()
                if role is not None:
                    # Check if the role is already assigned
                    existing_role_ids: List[str] = [
                        r.role_id for r in user.roles
                    ]
                    if role.role_id not in existing_role_ids:
                        user.roles.append(role)
                        roles_assigned.append(role.role_id)
                else:
                    self.logger.warning(
                        "Mapped role '%s' does not exist in the database; "
                        "skipping assignment for LDAP group '%s'.",
                        role_id,
                        ldap_group_dn,
                    )

        if roles_assigned:
            db.session.commit()
            self.logger.info(
                "Assigned %d roles to LDAP user '%s': %s",
                len(roles_assigned),
                user.user_id,
                ", ".join(roles_assigned),
            )

    # ==================================================================
    # LDAP Configuration
    # ==================================================================

    def _get_ldap_config(self) -> Dict[str, Any]:
        """Read LDAP configuration from Flask application config.

        Follows the twelve-factor app methodology (AAP Section 0.7.2)
        by reading all LDAP settings from environment-driven
        ``current_app.config``.

        Returns:
            A dictionary containing all LDAP configuration parameters
            with sensible defaults.
        """
        return {
            "server_url": current_app.config.get("LDAP_SERVER_URL", ""),
            "use_starttls": current_app.config.get("LDAP_USE_STARTTLS", False),
            "use_ssl": current_app.config.get("LDAP_USE_SSL", False),
            "user_base_dn": current_app.config.get("LDAP_USER_BASE_DN", ""),
            "user_dn_template": current_app.config.get(
                "LDAP_USER_DN_TEMPLATE", "uid={username},{base_dn}"
            ),
            "user_filter": current_app.config.get(
                "LDAP_USER_FILTER", "(uid={username})"
            ),
            "bind_strategy": current_app.config.get(
                "LDAP_BIND_STRATEGY", "direct"
            ),
            "system_dn": current_app.config.get("LDAP_SYSTEM_DN", ""),
            "system_password": current_app.config.get(
                "LDAP_SYSTEM_PASSWORD", ""
            ),
            "group_base_dn": current_app.config.get("LDAP_GROUP_BASE_DN", ""),
            "group_filter": current_app.config.get(
                "LDAP_GROUP_FILTER", "(member={user_dn})"
            ),
            "user_attributes": current_app.config.get(
                "LDAP_USER_ATTRIBUTES",
                ["mail", "displayName", "memberOf", "cn", "sn", "givenName"],
            ),
            "timeout": current_app.config.get("LDAP_TIMEOUT", 10),
            "group_role_mapping": current_app.config.get(
                "LDAP_GROUP_ROLE_MAPPING", {}
            ),
            "ca_cert_file": current_app.config.get(
                "LDAP_TLS_CACERT_FILE", ""
            ),
        }

    # ==================================================================
    # Connection Cleanup
    # ==================================================================

    def _close_connection(self, conn: Optional[Any]) -> None:
        """Unbind and close an LDAP connection.

        Safely handles ``None`` connections and suppresses errors during
        cleanup to prevent masking the original exception.

        Args:
            conn: An LDAP connection object, or ``None``.
        """
        if conn is None:
            return
        try:
            conn.unbind_s()
        except Exception:
            # Suppress all errors during cleanup — unbind failures are
            # not actionable and should not mask the original error.
            pass

    # ==================================================================
    # Utility Methods
    # ==================================================================

    @staticmethod
    def _extract_attribute(
        attrs: Dict[str, Any],
        key: str,
        default: str = "",
    ) -> str:
        """Extract a single string value from an LDAP attributes dict.

        Handles the fact that LDAP attributes may be strings, lists of
        strings, or absent entirely.

        Args:
            attrs: The LDAP attribute dictionary.
            key: The attribute key to extract.
            default: The default value if the key is missing.

        Returns:
            A single string value.
        """
        value: Any = attrs.get(key, default)
        if isinstance(value, list):
            return value[0] if value else default
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if value is None:
            return default
        return str(value)


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------

logger.debug(
    "LDAP realm module loaded — LDAP_AVAILABLE=%s, exports: %s",
    LDAP_AVAILABLE,
    ", ".join(__all__),
)
