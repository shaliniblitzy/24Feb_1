"""
Format Handler Registry and Factory — src.app.formats package.

This module serves as the **central registry and factory** for all 7 supported
repository format handlers in the Nexus Repository Python/Flask reimplementation.
It replaces the OSGi format plugin bundle discovery and registration mechanism
from the original Java Sonatype Nexus Repository source system (Karaf 4.4.4 /
Guice 7.0.0 DI).

**Responsibilities:**

1. **FORMAT_REGISTRY** — Module-level dictionary mapping canonical format name
   strings to their concrete :class:`FormatHandler` subclasses.  The keys
   (``'maven2'``, ``'npm'``, ``'docker'``, ``'nuget'``, ``'pypi'``, ``'apt'``,
   ``'raw'``) correspond exactly to the ``Repository.format`` column enum values
   in the data model (Section 6.2.1.2).

2. **Factory functions** — ``get_format_handler(format_name)`` instantiates a
   handler by name; ``get_format_handler_class(format_name)`` returns the class
   without instantiation.

3. **Blueprint registration** — ``register_format_blueprints(app)`` iterates
   all handlers, obtains their Flask Blueprints, and registers them with the
   Flask application.  Called by ``factory.py`` during ``create_app()``.

4. **Re-exports** — The :class:`FormatHandler` abstract base class and all 7
   concrete handler classes are re-exported for convenient access by consumers
   (services, API routes, tests).

**Architecture Context:**

In the original Java system, each repository format was an OSGi bundle
registered via the Karaf 4.4.4 module container and discovered through the
Guice 7.0.0 dependency injection framework.  In this Python/Flask
reimplementation, the ``FORMAT_REGISTRY`` dictionary and factory functions
serve as the equivalent discovery and instantiation mechanism.

**Design Patterns:**

- **Registry Pattern** — ``FORMAT_REGISTRY`` maps format names to handler
  classes for runtime lookup and dispatch.
- **Abstract Factory** — ``get_format_handler()`` creates instances of the
  appropriate concrete :class:`FormatHandler` subclass.
- **Strategy Pattern** — Each handler is a strategy selected by format name.

**Supported Formats (Feature F-101):**

======== ============= =============================================
Key      Handler Class Description
======== ============= =============================================
maven2   MavenFormatHandler    Apache Maven 2/3 (GAV coordinates, POM)
npm      NpmFormatHandler      npm registry protocol (scoped packages)
docker   DockerFormatHandler   Docker Registry API v2 (manifests/layers)
nuget    NugetFormatHandler    NuGet V3 API (.nupkg packages)
pypi     PypiFormatHandler     PEP 503 Simple Repository API
apt      AptFormatHandler      Debian APT (.deb packages)
raw      RawFormatHandler      Arbitrary binary file storage
======== ============= =============================================

Module-Level Exports:
    - :data:`FORMAT_REGISTRY` — ``dict[str, type[FormatHandler]]``
    - :data:`SUPPORTED_FORMATS` — ``frozenset[str]``
    - :func:`register_format_blueprints` — registers all format Blueprints
    - :func:`get_format_handler` — factory function (instantiates handler)
    - :func:`get_format_handler_class` — returns class without instantiation
    - :func:`is_supported_format` — validation predicate
    - :func:`get_supported_formats` — sorted list of format names
    - :class:`FormatHandler` — abstract base class (re-export)
    - :class:`MavenFormatHandler` — Maven2 handler (re-export)
    - :class:`NpmFormatHandler` — npm handler (re-export)
    - :class:`DockerFormatHandler` — Docker handler (re-export)
    - :class:`NugetFormatHandler` — NuGet handler (re-export)
    - :class:`PypiFormatHandler` — PyPI handler (re-export)
    - :class:`AptFormatHandler` — APT/Debian handler (re-export)
    - :class:`RawFormatHandler` — Raw handler (re-export)
"""

from __future__ import annotations

import logging

from flask import Flask

# ---------------------------------------------------------------------------
# Internal Imports — Abstract Base Class
# ---------------------------------------------------------------------------
# Re-export the FormatHandler ABC for convenient access by consumers.
# This import also makes the exception classes available transitively
# through base.py.
# ---------------------------------------------------------------------------

from src.app.formats.base import FormatHandler

# ---------------------------------------------------------------------------
# Internal Imports — Concrete Format Handler Classes
# ---------------------------------------------------------------------------
# Each handler is imported from its respective sub-package and registered
# in FORMAT_REGISTRY below.  These imports also serve as re-exports so
# that consumers can do:
#   from src.app.formats import MavenFormatHandler
# instead of the longer:
#   from src.app.formats.maven.handler import MavenFormatHandler
# ---------------------------------------------------------------------------

from src.app.formats.maven.handler import MavenFormatHandler
from src.app.formats.npm.handler import NpmFormatHandler
from src.app.formats.docker.handler import DockerFormatHandler
from src.app.formats.nuget.handler import NugetFormatHandler
from src.app.formats.pypi.handler import PypiFormatHandler
from src.app.formats.apt.handler import AptFormatHandler
from src.app.formats.raw.handler import RawFormatHandler

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source.  Used to log format blueprint registration events and factory
# operations during application startup and runtime.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API — __all__
# ---------------------------------------------------------------------------

__all__: list[str] = [
    # Registry and constants
    "FORMAT_REGISTRY",
    "SUPPORTED_FORMATS",
    # Factory and utility functions
    "register_format_blueprints",
    "get_format_handler",
    "get_format_handler_class",
    "is_supported_format",
    "get_supported_formats",
    # Abstract base class (re-export from base.py)
    "FormatHandler",
    # Concrete handler classes (re-exports from sub-packages)
    "MavenFormatHandler",
    "NpmFormatHandler",
    "DockerFormatHandler",
    "NugetFormatHandler",
    "PypiFormatHandler",
    "AptFormatHandler",
    "RawFormatHandler",
]

# ===========================================================================
# FORMAT_REGISTRY — Central Format Handler Registry
# ===========================================================================
#
# Maps canonical format name strings to their concrete FormatHandler
# subclasses.  The keys correspond exactly to the ``Repository.format``
# column enum values used throughout the data model.
#
# CRITICAL: The Maven format key is 'maven2' (NOT 'maven'), consistent
# with the original Java Nexus Repository implementation and the
# Repository model's format enum.  All other keys match 1:1 with
# their format_name class attributes.
#
# This dictionary is the single source of truth for which formats are
# available.  All factory functions and the blueprint registration
# function derive their behaviour from this registry.
# ===========================================================================

FORMAT_REGISTRY: dict[str, type[FormatHandler]] = {
    "maven2": MavenFormatHandler,
    "npm": NpmFormatHandler,
    "docker": DockerFormatHandler,
    "nuget": NugetFormatHandler,
    "pypi": PypiFormatHandler,
    "apt": AptFormatHandler,
    "raw": RawFormatHandler,
}
"""Central mapping of format name strings to handler classes.

Keys are the canonical format identifiers matching ``Repository.format``
enum values.  Values are the corresponding :class:`FormatHandler` subclasses
(not instances — use :func:`get_format_handler` to obtain instances).

Members:
    maven2: :class:`MavenFormatHandler`
    npm:    :class:`NpmFormatHandler`
    docker: :class:`DockerFormatHandler`
    nuget:  :class:`NugetFormatHandler`
    pypi:   :class:`PypiFormatHandler`
    apt:    :class:`AptFormatHandler`
    raw:    :class:`RawFormatHandler`
"""

# ===========================================================================
# SUPPORTED_FORMATS — Immutable Set of Supported Format Names
# ===========================================================================

SUPPORTED_FORMATS: frozenset[str] = frozenset(FORMAT_REGISTRY.keys())
"""Immutable set of all supported format name strings.

Derived from :data:`FORMAT_REGISTRY` keys for O(1) membership testing.
Use :func:`is_supported_format` for a convenient boolean check, or
:func:`get_supported_formats` for a sorted list.

Current members: ``{'apt', 'docker', 'maven2', 'npm', 'nuget', 'pypi', 'raw'}``
"""


# ===========================================================================
# Factory Functions
# ===========================================================================


def get_format_handler(format_name: str) -> FormatHandler:
    """Instantiate and return a format handler by canonical format name.

    Looks up the given ``format_name`` in :data:`FORMAT_REGISTRY`,
    instantiates the corresponding :class:`FormatHandler` subclass, and
    returns it.

    This is the primary factory function for obtaining format handler
    instances at runtime.  It replaces the Guice 7.0.0 ``@Inject``
    mechanism from the original Java source.

    Args:
        format_name: Canonical format identifier (e.g., ``'maven2'``,
            ``'npm'``, ``'docker'``).  Must match a key in
            :data:`FORMAT_REGISTRY`.

    Returns:
        A newly instantiated :class:`FormatHandler` subclass instance
        for the requested format.

    Raises:
        ValueError: If ``format_name`` is not found in the registry.
            The error message includes the list of supported formats
            to guide the caller toward a valid choice.

    Example::

        handler = get_format_handler('maven2')
        assert isinstance(handler, MavenFormatHandler)
        assert handler.format_name == 'maven2'
    """
    handler_class = FORMAT_REGISTRY.get(format_name)
    if handler_class is None:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise ValueError(
            f"Unsupported repository format: '{format_name}'. "
            f"Supported formats are: {supported}"
        )

    logger.debug(
        "Instantiating format handler for '%s': %s",
        format_name,
        handler_class.__name__,
    )
    return handler_class()


def get_format_handler_class(format_name: str) -> type[FormatHandler] | None:
    """Return the handler class for a format name without instantiation.

    Unlike :func:`get_format_handler`, this function returns the class
    itself rather than an instance.  Returns ``None`` if the format name
    is not found in the registry (does not raise an exception).

    This is useful when you need to inspect class attributes (e.g.,
    ``format_name``, ``content_types``, ``path_pattern``) or call class
    methods (e.g., ``get_blueprint()``) without creating an instance.

    Args:
        format_name: Canonical format identifier (e.g., ``'maven2'``).

    Returns:
        The :class:`FormatHandler` subclass for the requested format,
        or ``None`` if not found.

    Example::

        cls = get_format_handler_class('npm')
        assert cls is NpmFormatHandler
        assert cls.format_name == 'npm'

        unknown = get_format_handler_class('unknown')
        assert unknown is None
    """
    return FORMAT_REGISTRY.get(format_name)


# ===========================================================================
# Validation Utilities
# ===========================================================================


def is_supported_format(format_name: str) -> bool:
    """Check whether a format name corresponds to a supported format.

    Performs an O(1) membership test against :data:`SUPPORTED_FORMATS`.

    Args:
        format_name: The format identifier to check.

    Returns:
        ``True`` if ``format_name`` is a key in :data:`FORMAT_REGISTRY`,
        ``False`` otherwise.

    Example::

        assert is_supported_format('maven2') is True
        assert is_supported_format('maven') is False  # must use 'maven2'
        assert is_supported_format('unknown') is False
    """
    return format_name in SUPPORTED_FORMATS


def get_supported_formats() -> list[str]:
    """Return a sorted list of all supported format name strings.

    The list is derived from :data:`FORMAT_REGISTRY` keys and sorted
    alphabetically for deterministic ordering in API responses and
    error messages.

    Returns:
        A sorted list of canonical format names.

    Example::

        formats = get_supported_formats()
        # ['apt', 'docker', 'maven2', 'npm', 'nuget', 'pypi', 'raw']
    """
    return sorted(SUPPORTED_FORMATS)


# ===========================================================================
# Blueprint Registration
# ===========================================================================


def register_format_blueprints(app: Flask) -> None:
    """Register all format-specific Flask Blueprints with the application.

    Iterates over every handler class in :data:`FORMAT_REGISTRY`, calls
    each handler's :meth:`~FormatHandler.get_blueprint` class method to
    obtain its pre-configured Flask Blueprint, and registers it with the
    Flask application via :meth:`Flask.register_blueprint`.

    This function is called by ``factory.py``'s ``create_app()`` during
    application startup.  It replaces the OSGi bundle activation lifecycle
    from the original Java Karaf 4.4.4 container.

    Each blueprint is registered with a URL prefix of
    ``/v2/formats/{format_name}`` to namespace format-specific routes
    under a common API path.  Individual blueprints may define their own
    ``url_prefix`` which will be composed with this prefix.

    Args:
        app: The Flask application instance to register blueprints with.

    Example::

        from flask import Flask
        app = Flask(__name__)
        register_format_blueprints(app)
        # All 7 format blueprints are now registered
    """
    registered_count = 0

    for format_name, handler_class in FORMAT_REGISTRY.items():
        try:
            blueprint = handler_class.get_blueprint()
            url_prefix = f"/v2/formats/{format_name}"
            app.register_blueprint(blueprint, url_prefix=url_prefix)
            registered_count += 1
            logger.info(
                "Registered format blueprint: '%s' (%s) at prefix '%s'",
                format_name,
                handler_class.__name__,
                url_prefix,
            )
        except Exception:
            logger.exception(
                "Failed to register blueprint for format '%s' (%s)",
                format_name,
                handler_class.__name__,
            )

    logger.info(
        "Format blueprint registration complete: %d/%d formats registered",
        registered_count,
        len(FORMAT_REGISTRY),
    )
