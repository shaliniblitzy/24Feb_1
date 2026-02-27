"""
Authentication Realm Sub-Package for Nexus Repository.

This package contains the authentication realm implementations that replace
the Apache Shiro 2.0.0 realm system from the Java source. Each realm handles
a specific authentication method in the multi-backend authentication chain.

Realm Order (configurable, default):
1. LocalRealm - Username/password against local database
2. BearerTokenRealm - API key authentication (Feature F-304)
3. JWTRealm - JWT token validation
4. LDAPRealm - LDAP/Active Directory integration
5. SSORealm - SAML 2.0 / OpenID Connect federation

The AuthenticationChain in authentication.py iterates through enabled realms
using a first-successful strategy — the first realm to successfully authenticate
the request wins.

Architecture Context:
    - Replaces Apache Shiro 2.0.0 modular realm system (AAP Section 0.2.2)
    - Implements Chain of Responsibility pattern (AAP Section 0.4.3)
    - Supports custom realm registration for plugin architecture (F-504)
    - Python 3.12+ with full type annotations (AAP Section 0.7.2)

Exports:
    RealmBase:                Abstract base class for all authentication realms
    RealmRegistry:            Registry for realm discovery, registration, ordering
    realm_registry:           Global singleton RealmRegistry instance
    _register_builtin_realms: Deferred registration of built-in realms
    LocalRealm:               (lazy) Username/password authentication
    BearerTokenRealm:         (lazy) API key authentication (F-304)
    JWTRealm:                 (lazy) JWT token validation
    LDAPRealm:                (lazy) LDAP/Active Directory integration
    SSORealm:                 (lazy) SAML 2.0 / OpenID Connect federation
"""

from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

from src.app.models.user import User

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)


# ===========================================================================
# RealmBase — Abstract Base Class
# ===========================================================================


class RealmBase(ABC):
    """Abstract base class for authentication realms.

    All realm implementations must extend this class and implement:

    - :meth:`authenticate` — Attempt credential verification, returning a
      :class:`~src.app.models.user.User` on success or ``None`` on failure.
    - :meth:`supports` — Indicate whether this realm can handle the
      supplied credential type.
    - :meth:`get_realm_name` — Return a unique string identifier for the
      realm (e.g. ``'local'``, ``'jwt'``).

    Optionally, subclasses may override :meth:`is_configured` to report
    whether the realm has the required external configuration (e.g. LDAP
    server URL, SSO provider settings).

    This replaces the Shiro ``Realm`` interface from the Java source system.
    The authentication chain (see ``authentication.py``) iterates through
    registered realms in order, using a *first-successful* strategy — the
    first realm to return a non-``None`` ``User`` wins.
    """

    def __init__(self) -> None:
        """Initialize the realm with a per-class structured logger.

        Each realm instance receives a logger namespaced to
        ``<module>.<ClassName>`` for clear, structured log output that
        replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
        """
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.{self.__class__.__name__}"
        )

    # ------------------------------------------------------------------
    # Abstract Interface
    # ------------------------------------------------------------------

    @abstractmethod
    def authenticate(self, credentials: Dict[str, Any]) -> Optional[User]:
        """Attempt to authenticate the given credentials.

        This is the core contract method that every realm **must** implement.
        The authentication chain calls :meth:`supports` first; if it returns
        ``True``, this method is invoked.

        Args:
            credentials: Dictionary containing authentication credentials.
                Common keys vary by realm type:

                - ``'username'`` / ``'password'``: For username/password auth
                  (LocalRealm, LDAPRealm).
                - ``'token'``: For bearer token / JWT / API key auth
                  (BearerTokenRealm, JWTRealm).
                - ``'saml_assertion'``: For SAML SSO (SSORealm).
                - ``'oidc_token'``: For OIDC SSO (SSORealm).

        Returns:
            A :class:`~src.app.models.user.User` object if authentication
            succeeds; ``None`` if it fails or if this realm does not support
            the given credential type.

        Note:
            Implementations must **never** log plaintext passwords or
            secret tokens.  Only metadata (username, realm name, outcome)
            should appear in log messages.
        """
        pass  # pragma: no cover

    @abstractmethod
    def supports(self, credentials: Dict[str, Any]) -> bool:
        """Check whether this realm supports the given credential type.

        The authentication chain calls this method *before* calling
        :meth:`authenticate` to determine whether the realm should be
        tried for the supplied credentials.

        Args:
            credentials: Dictionary containing authentication credentials.

        Returns:
            ``True`` if this realm can attempt authentication with these
            credentials; ``False`` otherwise.
        """
        pass  # pragma: no cover

    @abstractmethod
    def get_realm_name(self) -> str:
        """Return the unique name identifier for this realm.

        Used for logging, configuration look-ups, and realm ordering in
        the authentication chain.

        Returns:
            A string identifier.  Built-in values are ``'local'``,
            ``'bearer_token'``, ``'jwt'``, ``'ldap'``, ``'sso'``.
        """
        pass  # pragma: no cover

    # ------------------------------------------------------------------
    # Optional Overrides
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Check whether this realm is properly configured and operational.

        Override in subclasses that require external configuration
        (e.g. LDAP server URL, SSO provider settings).  The default
        implementation returns ``True``, meaning the realm is always
        considered ready.

        Returns:
            ``True`` if the realm is configured and ready to use;
            ``False`` otherwise.
        """
        return True


# ===========================================================================
# RealmRegistry — Realm Discovery and Ordering
# ===========================================================================


class RealmRegistry:
    """Registry for authentication realm implementations.

    Provides realm discovery, registration, and ordering for the
    ``AuthenticationChain`` in ``authentication.py``.  Maintains an
    internal mapping of realm names → realm classes.

    The registry supports:

    - **Built-in realm registration** via :func:`_register_builtin_realms`.
    - **Custom / plugin realm registration** via :meth:`register`,
      enabling the plugin architecture (Feature F-504).
    - **Configurable ordering** via :meth:`get_ordered_realms`, which
      accepts an explicit list of enabled realm names or falls back to
      the default order: ``local → bearer_token → jwt → ldap → sso``.
    """

    def __init__(self) -> None:
        """Initialize the registry with an empty realm map and default order."""
        self._realms: Dict[str, Type[RealmBase]] = {}
        self._default_order: List[str] = [
            "local",
            "bearer_token",
            "jwt",
            "ldap",
            "sso",
        ]
        self.logger: logging.Logger = logging.getLogger(__name__)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, realm_class: Type[RealmBase]) -> None:
        """Register a realm class in the registry.

        The class is instantiated temporarily to obtain its
        :meth:`~RealmBase.get_realm_name` value, which serves as the
        registry key.  Duplicate registrations silently overwrite the
        previous entry (useful for plugin hot-reload scenarios).

        Args:
            realm_class: A concrete subclass of :class:`RealmBase`.

        Raises:
            TypeError: If *realm_class* is not a subclass of
                :class:`RealmBase`.
        """
        if not (isinstance(realm_class, type) and issubclass(realm_class, RealmBase)):
            raise TypeError(
                f"Expected a RealmBase subclass, got {realm_class!r}"
            )
        # Instantiate temporarily to discover the realm name.
        instance: RealmBase = realm_class()
        name: str = instance.get_realm_name()
        self._realms[name] = realm_class
        self.logger.debug("Registered authentication realm: %s", name)

    def get_realm_class(self, name: str) -> Optional[Type[RealmBase]]:
        """Look up a registered realm class by its name.

        Args:
            name: Realm identifier string (e.g. ``'local'``, ``'jwt'``).

        Returns:
            The realm class if found; ``None`` otherwise.
        """
        return self._realms.get(name)

    def get_ordered_realms(
        self, enabled_names: Optional[List[str]] = None
    ) -> List[RealmBase]:
        """Create and return realm instances in the requested order.

        Each returned instance has already passed the :meth:`is_configured`
        check — realms that report ``False`` are silently skipped.

        Args:
            enabled_names: Explicit list of realm names to enable, given
                in the desired authentication order.  If ``None``, the
                default order (``local → bearer_token → jwt → ldap → sso``)
                is used.

        Returns:
            A list of :class:`RealmBase` instances in the configured order,
            excluding realms that are not registered or not configured.
        """
        order: List[str] = enabled_names if enabled_names is not None else self._default_order
        realms: List[RealmBase] = []
        for name in order:
            realm_class: Optional[Type[RealmBase]] = self._realms.get(name)
            if realm_class is not None:
                try:
                    instance: RealmBase = realm_class()
                    if instance.is_configured():
                        realms.append(instance)
                    else:
                        self.logger.info(
                            "Realm '%s' is not configured, skipping", name
                        )
                except Exception:
                    self.logger.exception(
                        "Failed to instantiate realm '%s', skipping", name
                    )
            else:
                self.logger.warning(
                    "Realm '%s' not found in registry", name
                )
        return realms

    def list_registered(self) -> List[str]:
        """Return a list of all registered realm names.

        The order of the returned list is insertion order (Python 3.7+
        ``dict`` ordering guarantee).

        Returns:
            List of registered realm name strings.
        """
        return list(self._realms.keys())


# ===========================================================================
# Global Registry Instance
# ===========================================================================

# The canonical realm registry shared across the application.  Services
# and the authentication chain import this instance directly:
#
#   from src.app.auth.realms import realm_registry
#
realm_registry: RealmRegistry = RealmRegistry()


# ===========================================================================
# Deferred Built-In Realm Registration
# ===========================================================================


def _register_builtin_realms() -> None:
    """Register all built-in authentication realms.

    Uses **lazy imports** inside the function body to avoid circular
    dependency issues at module load time.  Each realm module
    (``local_realm``, ``bearer_token_realm``, etc.) imports
    :class:`RealmBase` from *this* package, so importing them at the
    top level would create a circular import chain.

    This function must be called **once** during application
    initialization — typically from the Flask application factory
    (``factory.py`` / ``create_app()``).  It is **not** called at
    module load time.
    """
    from src.app.auth.realms.local_realm import LocalRealm
    from src.app.auth.realms.bearer_token_realm import BearerTokenRealm
    from src.app.auth.realms.jwt_realm import JWTRealm
    from src.app.auth.realms.ldap_realm import LDAPRealm
    from src.app.auth.realms.sso_realm import SSORealm

    realm_registry.register(LocalRealm)
    realm_registry.register(BearerTokenRealm)
    realm_registry.register(JWTRealm)
    realm_registry.register(LDAPRealm)
    realm_registry.register(SSORealm)

    logger.info(
        "Built-in authentication realms registered: %s",
        ", ".join(realm_registry.list_registered()),
    )


# ===========================================================================
# Lazy Re-Exports via __getattr__
# ===========================================================================

# Mapping of convenience re-export names → fully-qualified module paths.
# Using module-level ``__getattr__`` (PEP 562) enables callers to write:
#
#   from src.app.auth.realms import LocalRealm
#
# without triggering eager imports that would cause circular dependencies.

_LAZY_IMPORTS: Dict[str, str] = {
    "LocalRealm": "src.app.auth.realms.local_realm",
    "BearerTokenRealm": "src.app.auth.realms.bearer_token_realm",
    "JWTRealm": "src.app.auth.realms.jwt_realm",
    "LDAPRealm": "src.app.auth.realms.ldap_realm",
    "SSORealm": "src.app.auth.realms.sso_realm",
}


def __getattr__(name: str) -> Any:
    """Lazy import of realm classes to avoid circular dependencies.

    Called automatically by the Python import machinery when an attribute
    is not found in the module namespace via normal look-up.  If *name*
    is a known realm class, the corresponding sub-module is imported on
    demand via :func:`importlib.import_module` and the requested class
    is returned.

    Args:
        name: Attribute name being looked up.

    Returns:
        The requested realm class.

    Raises:
        AttributeError: If *name* is not a recognised lazy export.
    """
    if name in _LAZY_IMPORTS:
        module = importlib.import_module(_LAZY_IMPORTS[name])
        value = getattr(module, name)
        # Cache in module namespace to avoid repeated dynamic imports.
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ===========================================================================
# Public Export List
# ===========================================================================

__all__: list[str] = [
    # Base class
    "RealmBase",
    # Registry
    "RealmRegistry",
    "realm_registry",
    "_register_builtin_realms",
    # Realm classes (lazy-loaded via __getattr__)
    "LocalRealm",
    "BearerTokenRealm",
    "JWTRealm",
    "LDAPRealm",
    "SSORealm",
]


# ---------------------------------------------------------------------------
# Module Load Logging
# ---------------------------------------------------------------------------

logger.debug(
    "Authentication realms package loaded — direct exports: %s",
    ", ".join(
        name for name in __all__ if name not in _LAZY_IMPORTS
    ),
)
