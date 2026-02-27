"""
Authentication and Authorization package for Nexus Repository.

This package replaces Apache Shiro 2.0.0 from the Java source system,
providing multi-backend authentication and three-tier RBAC authorization.

Modules:
    authentication
        Multi-realm authentication chain (replaces ``NexusAuthenticationFilter``
        + ``FirstSuccessfulModularRealmAuthenticator``).
    authorization
        Three-tier RBAC authorization engine (replaces ``SecurityComponent``).
    rbac
        Role-privilege mapping and low-level permission checking engine.
    content_selector
        CSEL (Content Selector Expression Language) parser and evaluator for
        Tier 3 sub-repository access control.
    password_utils
        Secure password hashing utilities using bcrypt/scrypt (replaces
        BouncyCastle 1.78.1 credential hashing).
    realms/
        Authentication realm implementations:
        - ``local_realm``        — Username / password (``AuthenticatingRealmImpl``)
        - ``bearer_token_realm`` — API key authentication (``BearerTokenRealm``)
        - ``jwt_realm``          — JWT token validation (``JwtSecurityFilter``)
        - ``ldap_realm``         — LDAP / Active Directory integration
        - ``sso_realm``          — SAML 2.0 / OpenID Connect federation

Convenience Re-exports:
    The following symbols are re-exported from this package for ergonomic
    access by the rest of the application (``factory.py``, API blueprints,
    service layer modules)::

        from src.app.auth import authenticate_request
        from src.app.auth import AuthenticationChain
        from src.app.auth import authorize_request
        from src.app.auth import AuthorizationEngine
        from src.app.auth import RBACEnforcer
        from src.app.auth import ContentSelectorEvaluator
        from src.app.auth import hash_password
        from src.app.auth import verify_password

    All imports are **lazily loaded** via the module-level ``__getattr__``
    hook (PEP 562) to prevent circular import issues.  Heavy initialization
    happens only when a specific symbol is first accessed, not at package
    import time.

Compatibility:
    Python 3.12+, zero Java dependencies.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Public API — explicit list of re-exported symbols
# ---------------------------------------------------------------------------
# Defines the contract for ``from src.app.auth import *`` and enables static
# analysis tools to discover the package's public surface.
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "authenticate_request",
    "AuthenticationChain",
    "authorize_request",
    "AuthorizationEngine",
    "RBACEnforcer",
    "ContentSelectorEvaluator",
    "hash_password",
    "verify_password",
]

# ---------------------------------------------------------------------------
# Lazy Import Map
# ---------------------------------------------------------------------------
# Maps each public symbol name to the submodule path from which it should
# be imported on first access.  This structure powers the ``__getattr__``
# hook defined below and centralises all import knowledge in one place.
#
# Format:
#   "SymbolName": ("dotted.module.path", "SymbolName")
#
# Using a dictionary avoids repeating module paths and makes it trivial
# to add new re-exports in the future.
# ---------------------------------------------------------------------------

_LAZY_IMPORT_MAP: dict[str, tuple[str, str]] = {
    # From src.app.auth.authentication
    "authenticate_request": (
        "src.app.auth.authentication",
        "authenticate_request",
    ),
    "AuthenticationChain": (
        "src.app.auth.authentication",
        "AuthenticationChain",
    ),
    # From src.app.auth.authorization
    "authorize_request": (
        "src.app.auth.authorization",
        "authorize_request",
    ),
    "AuthorizationEngine": (
        "src.app.auth.authorization",
        "AuthorizationEngine",
    ),
    # From src.app.auth.rbac
    "RBACEnforcer": (
        "src.app.auth.rbac",
        "RBACEnforcer",
    ),
    # From src.app.auth.content_selector
    "ContentSelectorEvaluator": (
        "src.app.auth.content_selector",
        "ContentSelectorEvaluator",
    ),
    # From src.app.auth.password_utils
    "hash_password": (
        "src.app.auth.password_utils",
        "hash_password",
    ),
    "verify_password": (
        "src.app.auth.password_utils",
        "verify_password",
    ),
}


# ---------------------------------------------------------------------------
# Module-Level __getattr__ — Lazy Loading (PEP 562)
# ---------------------------------------------------------------------------
# This hook is invoked by the Python import system whenever an attribute is
# accessed on the ``src.app.auth`` package that has not already been resolved.
# It looks up the requested *name* in ``_LAZY_IMPORT_MAP``, performs the
# deferred import, caches the result on the module's ``__dict__`` for
# subsequent zero-cost access, and returns the resolved object.
#
# **Why lazy loading?**
#
# Several submodules (``authentication.py``, ``authorization.py``) import
# from ``src.app.extensions``, ``src.app.models.*``, and
# ``src.app.auth.realms.*``.  Eagerly importing them at package-init time
# would create circular dependency chains when ``factory.py`` or API
# blueprint modules import from this package during application bootstrap.
# Lazy loading breaks the cycle because no submodule code executes until
# the symbol is actually *used*, which occurs after the import graph has
# been fully constructed.
#
# **Performance:**
#
# The first access of each symbol incurs a single ``importlib.import_module``
# call.  After the result is cached in ``globals()``, subsequent accesses
# are plain dictionary lookups with zero overhead.
# ---------------------------------------------------------------------------


def __getattr__(name: str) -> object:
    """Lazily import and return a re-exported symbol from a submodule.

    This function is called automatically by Python's attribute lookup
    machinery whenever *name* is not found in the module's ``__dict__``.
    It consults ``_LAZY_IMPORT_MAP`` to locate the correct submodule and
    symbol, performs the import, caches the result, and returns it.

    Args:
        name: The attribute name being accessed on ``src.app.auth``.

    Returns:
        The requested symbol (function, class, or object).

    Raises:
        AttributeError: If *name* is not a recognised public symbol of
            this package.
    """
    mapping = _LAZY_IMPORT_MAP.get(name)
    if mapping is not None:
        module_path, attr_name = mapping
        # Perform the deferred import.  ``importlib.import_module`` is
        # used instead of a bare ``import`` statement so that the module
        # path can be a runtime string from the mapping dictionary.
        import importlib

        module = importlib.import_module(module_path)
        value = getattr(module, attr_name)
        # Cache the resolved value directly on the package's globals so
        # that future accesses bypass ``__getattr__`` entirely.
        globals()[name] = value
        return value

    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )


# ---------------------------------------------------------------------------
# Module-Level __dir__ — Discoverability
# ---------------------------------------------------------------------------
# Override ``dir(src.app.auth)`` to include all lazily-exported symbols,
# ensuring that auto-completion in IDEs and ``help()`` show the full
# public surface of this package, even before any lazy import has fired.
# ---------------------------------------------------------------------------


def __dir__() -> list[str]:
    """Return a sorted list of all names available in this package.

    Merges the module's actual ``__dict__`` keys with the lazily-exported
    symbols from ``__all__`` to provide a complete listing for ``dir()``,
    IDE auto-completion, and introspection tools.

    Returns:
        Sorted list of available attribute names.
    """
    # Start with all names already present in the module's namespace
    # (including dunder attributes, the lazy-import map, and any
    # previously resolved symbols).
    names: set[str] = set(globals().keys())
    # Add all lazily-exported public names so they appear in dir() even
    # before they have been accessed / imported.
    names.update(__all__)
    return sorted(names)
