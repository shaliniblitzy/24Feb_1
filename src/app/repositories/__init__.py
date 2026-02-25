"""
Repository type logic package for the Nexus Repository Flask application.

This package replaces the Repository facets (Section 5.2.4) from the Java
Sonatype Nexus Repository Manager source system. It implements the three
repository types (Hosted, Proxy, Group) with lifecycle state machine
management (Feature F-102).

**Architecture Context:**

The Java source system used OSGi bundles for repository type isolation and
Google Guice for dependency injection.  In the Python/Flask target, each
repository type is a plain Python class composed with cross-cutting facets
(storage, security, search) via composition, and the OSGi bundle registry
is replaced by :data:`REPOSITORY_TYPE_REGISTRY` — a simple dictionary
mapping type name strings to handler classes.

**Modules:**

- ``lifecycle``  — Repository lifecycle state machine (state transitions)
- ``facets``     — Repository facet abstractions (common behaviours)
- ``hosted``     — Hosted repository logic (local BlobStore, write policies)
- ``proxy``      — Proxy repository logic (remote caching, negative cache)
- ``group``      — Group repository logic (member aggregation, cycle detection)

**Design Patterns Applied:**

+----------------------------+------------------------------------------------+
| Pattern                    | Application                                    |
+============================+================================================+
| Factory (AAP 0.4.3)       | ``REPOSITORY_TYPE_REGISTRY`` maps type strings  |
|                            | to handler classes for factory-based creation   |
+----------------------------+------------------------------------------------+
| State Machine (AAP 0.4.3) | ``RepositoryLifecycle`` enforces valid state     |
|                            | transitions (NEW → STARTED → STOPPED → DELETED)|
+----------------------------+------------------------------------------------+
| Strategy                   | ``StorageFacet`` wraps pluggable BlobStore      |
|                            | backends (File vs S3)                          |
+----------------------------+------------------------------------------------+
| Composite / Registry       | ``FacetRegistry`` (in facets module) manages    |
|                            | lifecycle of all facets per repository          |
+----------------------------+------------------------------------------------+

**Usage:**

.. code-block:: python

    from src.app.repositories import (
        HostedRepository,
        ProxyRepository,
        GroupRepository,
    )
    from src.app.repositories import RepositoryLifecycle, RepositoryState
    from src.app.repositories import get_repository_handler, get_supported_types

    # Factory-based creation via the registry
    handler_cls = get_repository_handler('hosted')  # returns HostedRepository
    handler = handler_cls(repository_model_instance)

    # Query supported types
    supported = get_supported_types()  # ['hosted', 'proxy', 'group']
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Provides diagnostic output for package-level registry operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ===========================================================================
# Re-exports — Lifecycle Module
# ===========================================================================
# These components implement the repository state machine (Feature F-102).
# RepositoryLifecycle manages transitions: NEW → STARTED → STOPPED → DELETED.
# RepositoryState is the str-based enum of lifecycle states.
# VALID_TRANSITIONS defines the allowed transition graph.
# InvalidStateTransitionError is raised on illegal state transitions.
# ===========================================================================

from src.app.repositories.lifecycle import (  # noqa: E402
    InvalidStateTransitionError,
    RepositoryLifecycle,
    RepositoryState,
    VALID_TRANSITIONS,
)

# ===========================================================================
# Re-exports — Facet Module
# ===========================================================================
# These abstract base classes define cross-cutting behaviours composed by
# all repository type handlers.
# - RepositoryFacet: abstract base class for all facets
# - StorageFacet: BlobStore content operations (F-201, F-202)
# - SecurityFacet: three-tier RBAC permission enforcement (F-301)
# - SearchFacet: Elasticsearch indexing and query capabilities (F-103)
# ===========================================================================

from src.app.repositories.facets import (  # noqa: E402
    RepositoryFacet,
    SearchFacet,
    SecurityFacet,
    StorageFacet,
)

# ===========================================================================
# Re-exports — Repository Type Handlers
# ===========================================================================
# Each handler implements a distinct resolution strategy:
# - HostedRepository: serves exclusively from local BlobStore
# - ProxyRepository: caches from remote upstream with negative cache
# - GroupRepository: aggregates ordered members with first-match-wins
# ===========================================================================

from src.app.repositories.hosted import HostedRepository  # noqa: E402
from src.app.repositories.proxy import ProxyRepository  # noqa: E402
from src.app.repositories.group import GroupRepository  # noqa: E402


# ===========================================================================
# Repository Type Registry
# ===========================================================================
# Replaces the OSGi bundle registration mechanism from the Java source.
# Maps repository type identifier strings to their handler classes, enabling
# factory-based repository creation in services/repository_manager.py.
#
# This registry is the single source of truth for all supported repository
# types.  To add a new repository type, register it here.
# ===========================================================================

REPOSITORY_TYPE_REGISTRY: dict[str, type] = {
    "hosted": HostedRepository,
    "proxy": ProxyRepository,
    "group": GroupRepository,
}
"""Mapping of repository type name strings to handler classes.

Used by :func:`get_repository_handler` and
:class:`~src.app.services.repository_manager.RepositoryManager` for
factory-based repository creation.

Keys are the canonical lowercase type identifiers stored in the
``repository.type`` database column.
"""


# ===========================================================================
# Registry Helper Functions
# ===========================================================================


def get_repository_handler(repo_type: str) -> type:
    """Look up a repository handler class by type identifier.

    Retrieves the handler class registered for the given repository type
    string from :data:`REPOSITORY_TYPE_REGISTRY`.  Returns the **class**
    itself (not an instance) — the caller is responsible for instantiation
    with the appropriate :class:`~src.app.models.repository.Repository`
    model instance.

    This function is the primary entry point for the Factory Pattern
    (AAP Section 0.4.3) used by
    :class:`~src.app.services.repository_manager.RepositoryManager`.

    Args:
        repo_type: The repository type identifier string.  Must be one of
            the keys in :data:`REPOSITORY_TYPE_REGISTRY` (e.g. ``'hosted'``,
            ``'proxy'``, ``'group'``).  The comparison is case-insensitive
            — the value is normalised to lowercase before lookup.

    Returns:
        The handler class (a subclass or concrete class) corresponding to
        the requested repository type.

    Raises:
        ValueError: If ``repo_type`` is not a recognised repository type.

    Examples:
        >>> handler_cls = get_repository_handler('hosted')
        >>> handler_cls is HostedRepository
        True

        >>> get_repository_handler('invalid')  # doctest: +ELLIPSIS
        Traceback (most recent call last):
            ...
        ValueError: Unsupported repository type 'invalid'. ...
    """
    if not isinstance(repo_type, str):
        raise ValueError(
            f"repo_type must be a string, got {type(repo_type).__name__}"
        )

    normalised = repo_type.strip().lower()

    handler_cls = REPOSITORY_TYPE_REGISTRY.get(normalised)
    if handler_cls is None:
        supported = ", ".join(sorted(REPOSITORY_TYPE_REGISTRY.keys()))
        raise ValueError(
            f"Unsupported repository type '{repo_type}'. "
            f"Supported types are: {supported}"
        )

    logger.debug(
        "Resolved repository handler for type '%s': %s",
        normalised,
        handler_cls.__name__,
    )
    return handler_cls


def get_supported_types() -> list[str]:
    """Return the list of supported repository type identifier strings.

    Queries :data:`REPOSITORY_TYPE_REGISTRY` for all currently registered
    repository type keys.

    Returns:
        A list of repository type strings (e.g. ``['hosted', 'proxy',
        'group']``).  The order matches the insertion order of the
        registry dictionary (Python 3.7+ guarantees dict ordering).

    Examples:
        >>> types = get_supported_types()
        >>> 'hosted' in types
        True
        >>> 'proxy' in types
        True
        >>> 'group' in types
        True
    """
    return list(REPOSITORY_TYPE_REGISTRY.keys())


# ===========================================================================
# Public API — __all__
# ===========================================================================
# Explicitly declare the public interface of this package.  This controls
# what is exported by ``from src.app.repositories import *`` and serves as
# documentation for IDE autocompletion and static analysis tools.
# ===========================================================================

__all__: list[str] = [
    # Lifecycle
    "RepositoryLifecycle",
    "RepositoryState",
    "VALID_TRANSITIONS",
    "InvalidStateTransitionError",
    # Facets
    "RepositoryFacet",
    "StorageFacet",
    "SecurityFacet",
    "SearchFacet",
    # Repository Types
    "HostedRepository",
    "ProxyRepository",
    "GroupRepository",
    # Registry
    "REPOSITORY_TYPE_REGISTRY",
    "get_repository_handler",
    "get_supported_types",
]
