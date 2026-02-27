"""
Repository Facet Abstractions for the Nexus Repository Flask application.

This module defines the foundational repository facet abstraction layer —
the common behavioural contracts (abstract base classes and concrete facets)
shared by all three repository types (Hosted, Proxy, Group).  Facets
encapsulate cross-cutting repository behaviours — storage access, security
enforcement, search indexing — that are *composed* into concrete repository
type handlers.

**Architecture Context:**

Replaces the Java repository facet abstraction system from Section 5.2.4
of the Technical Specification.  In the Java source, facets were OSGi-managed
beans attached to repository instances via Guice dependency injection; in the
Python/Flask target, they become abstract base classes and mixins composed by
repository type classes in ``hosted.py``, ``proxy.py``, and ``group.py``.

**Design Patterns Applied:**

+-----------------------------+------------------------------------------------+
| Pattern                     | Application                                    |
+=============================+================================================+
| Abstract Base Class (ABC)   | ``RepositoryFacet`` — prevents direct           |
|                             | instantiation, enforces behavioural contract    |
+-----------------------------+------------------------------------------------+
| Strategy                    | ``StorageFacet`` wraps pluggable ``BlobStore``  |
|                             | backends (File vs S3)                          |
+-----------------------------+------------------------------------------------+
| Registry / Composite        | ``FacetRegistry`` manages lifecycle of all      |
|                             | facets attached to a repository instance        |
+-----------------------------+------------------------------------------------+
| Template Method             | ``_ensure_attached()`` guard used by all        |
|                             | subclass methods before executing logic         |
+-----------------------------+------------------------------------------------+

**Feature Coverage:**

- **F-101** Multi-Format Repository Support (via StorageFacet + SearchFacet)
- **F-102** Repository Types — facets composed by Hosted, Proxy, and Group
- **F-103** Content Indexing and Search (SearchFacet → Elasticsearch)
- **F-201/F-202** File/S3 BlobStore (StorageFacet → BlobStore abstraction)
- **F-301** RBAC (SecurityFacet → three-tier authorisation)
- **F-303** Audit Logging (logging integration)

**Exports:**

- :class:`RepositoryFacet` — abstract base class for all repository facets
- :class:`StorageFacet` — storage access (BlobStore delegation)
- :class:`SecurityFacet` — permission enforcement
- :class:`SearchFacet` — Elasticsearch indexing and querying
- :class:`FacetRegistry` — facet lifecycle management
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, BinaryIO

from flask import current_app

from src.app.extensions import db
from src.app.models.repository import Repository
from src.app.models.component import Component
from src.app.models.asset import Asset  # noqa: F401 — imported for type reference

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for facet operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "RepositoryFacet",
    "StorageFacet",
    "SecurityFacet",
    "SearchFacet",
    "FacetRegistry",
]


# ═══════════════════════════════════════════════════════════════════════════
# RepositoryFacet — Abstract Base Facet Class
# ═══════════════════════════════════════════════════════════════════════════


class RepositoryFacet(ABC):
    """Abstract base class for all repository facets.

    A facet encapsulates a specific cross-cutting behaviour that can be
    composed into repository type implementations.  Each facet is bound to
    a specific :class:`Repository` instance and follows an explicit
    **attach / detach** lifecycle that mirrors the OSGi service lifecycle
    from the original Java source.

    Subclasses:
        - :class:`StorageFacet`  — BlobStore delegation
        - :class:`SecurityFacet` — permission enforcement
        - :class:`SearchFacet`   — Elasticsearch indexing

    Attributes:
        repository: The :class:`Repository` model instance this facet is
            bound to.
        name: Shorthand for ``repository.name``.
        logger: Per-instance logger namespaced by facet class and
            repository name.
    """

    def __init__(self, repository: Repository) -> None:
        """Initialise the facet and bind it to a repository.

        Args:
            repository: The :class:`Repository` model that owns this facet.
        """
        self.repository: Repository = repository
        self.name: str = repository.name
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.{self.__class__.__name__}.{repository.name}"
        )
        self._attached: bool = False

    # -- Lifecycle methods --------------------------------------------------

    def attach(self) -> None:
        """Mark the facet as attached to the repository.

        Must be called before the facet can service requests.  The
        :class:`FacetRegistry` calls this automatically during
        :meth:`FacetRegistry.register`.
        """
        self._attached = True
        self.logger.debug(
            "Facet %s attached to repository '%s'",
            self.__class__.__name__,
            self.name,
        )

    def detach(self) -> None:
        """Mark the facet as detached from the repository.

        After detachment the facet will refuse to service requests
        (via :meth:`_ensure_attached`).  The :class:`FacetRegistry`
        calls this during :meth:`FacetRegistry.detach_all`.
        """
        self._attached = False
        self.logger.debug(
            "Facet %s detached from repository '%s'",
            self.__class__.__name__,
            self.name,
        )

    @property
    def is_attached(self) -> bool:
        """Return ``True`` if the facet is currently attached."""
        return self._attached

    def _ensure_attached(self) -> None:
        """Guard method — raise if the facet is not attached.

        Subclasses should call this at the start of every public method
        that performs work.

        Raises:
            RuntimeError: If the facet has not been attached via
                :meth:`attach`.
        """
        if not self._attached:
            raise RuntimeError(
                f"Facet {self.__class__.__name__} is not attached to "
                f"repository '{self.name}'.  Call attach() first."
            )


# ═══════════════════════════════════════════════════════════════════════════
# StorageFacet — Storage Access Abstraction
# ═══════════════════════════════════════════════════════════════════════════


class StorageFacet(RepositoryFacet):
    """Facet providing storage access for repository content.

    Wraps :class:`BlobStore` operations and provides content read / write /
    delete capabilities bound to the repository's configured blob store.
    The concrete ``BlobStore`` backend (File or S3) is resolved lazily on
    first access to avoid circular imports at module load time.

    **Java Equivalent:**
    Replaces the storage facet from the OSGi bundle system and the
    ``BlobStore`` API wiring performed by Guice in the Java source.
    """

    def __init__(self, repository: Repository) -> None:
        """Initialise the storage facet.

        Args:
            repository: The :class:`Repository` whose blob store will be
                used for content operations.
        """
        super().__init__(repository)
        self._blob_store_name: str = repository.blob_store_name
        self._blob_store: Any | None = None  # Lazy-loaded BlobStore instance

    # -- Blob Store resolution ----------------------------------------------

    @property
    def blob_store(self) -> Any:
        """Lazily resolve and return the :class:`BlobStore` instance.

        The instance is resolved from the application-level blob store
        registry stored in ``current_app.config['BLOBSTORE_REGISTRY']``.
        If no registry exists, a :class:`RuntimeError` is raised.

        Returns:
            The concrete :class:`BlobStore` instance for this repository.

        Raises:
            RuntimeError: If the blob store cannot be resolved.
        """
        if self._blob_store is not None:
            return self._blob_store

        # Inline import to avoid circular dependency:
        # storage.blobstore → models → facets chain
        from src.app.storage.blobstore import BlobStore  # noqa: F811

        try:
            # Attempt to resolve from app-level registry first
            registry = current_app.config.get("BLOBSTORE_REGISTRY", {})
            if isinstance(registry, dict) and self._blob_store_name in registry:
                store = registry[self._blob_store_name]
                if isinstance(store, BlobStore):
                    self._blob_store = store
                    self.logger.debug(
                        "Resolved BlobStore '%s' from registry",
                        self._blob_store_name,
                    )
                    return self._blob_store

            # Fallback: check for a factory function in config
            factory = current_app.config.get("BLOBSTORE_FACTORY")
            if callable(factory):
                store = factory(self._blob_store_name)
                if store is not None:
                    self._blob_store = store
                    self.logger.debug(
                        "Resolved BlobStore '%s' via factory",
                        self._blob_store_name,
                    )
                    return self._blob_store

            raise RuntimeError(
                f"Cannot resolve BlobStore '{self._blob_store_name}' for "
                f"repository '{self.name}'.  Ensure the BlobStore is "
                f"registered in current_app.config['BLOBSTORE_REGISTRY'] "
                f"or a BLOBSTORE_FACTORY is configured."
            )
        except RuntimeError:
            raise
        except Exception as exc:
            self.logger.error(
                "Failed to resolve BlobStore '%s': %s",
                self._blob_store_name,
                exc,
            )
            raise RuntimeError(
                f"Failed to resolve BlobStore '{self._blob_store_name}' "
                f"for repository '{self.name}': {exc}"
            ) from exc

    # -- Content operations -------------------------------------------------

    def store_content(
        self,
        path: str,
        content: BinaryIO,
        content_type: str,
        size: int | None = None,
        checksums: dict[str, str] | None = None,
    ) -> str:
        """Store binary content in the blob store.

        Delegates to the :class:`BlobStore`'s ``create()`` method, wrapping
        the raw storage call with logging, error handling, and metadata
        tracking.

        Args:
            path: The logical artifact path within the repository
                (e.g. ``'/org/example/lib/1.0/lib-1.0.jar'``).
            content: Readable binary stream of the content to store.
            content_type: MIME type of the content
                (e.g. ``'application/java-archive'``).
            size: Optional content size in bytes.  When provided it is
                passed as metadata to the blob store.
            checksums: Optional pre-computed checksums as a dict with keys
                ``'sha1'``, ``'sha256'``, ``'md5'``.

        Returns:
            The blob ID as a string (``store_name:uuid`` format).

        Raises:
            RuntimeError: If the facet is not attached or the blob store
                is unreachable.
        """
        self._ensure_attached()

        from src.app.storage.blobstore import BlobId  # noqa: F811

        store = self.blob_store
        headers: dict[str, str] = {
            "BlobStore.blob-name": path,
            "BlobStore.repo-name": self.name,
            "BlobStore.content-type": content_type,
        }
        if size is not None:
            headers["BlobStore.content-length"] = str(size)
        if checksums:
            for algo, digest in checksums.items():
                headers[f"BlobStore.checksum-{algo}"] = digest

        try:
            blob = store.create(
                blob_id=None,
                data=content,
                content_type=content_type,
                headers=headers,
            )
            blob_id_str = str(blob.blob_id)
            self.logger.debug(
                "Stored content at path '%s' as blob '%s' in store '%s'",
                path,
                blob_id_str,
                self._blob_store_name,
            )
            return blob_id_str
        except Exception as exc:
            self.logger.error(
                "Failed to store content at path '%s' in store '%s': %s",
                path,
                self._blob_store_name,
                exc,
            )
            raise

    def get_content(
        self, blob_id: str
    ) -> tuple[BinaryIO | None, dict | None]:
        """Retrieve content from the blob store by blob ID.

        Args:
            blob_id: String representation of the blob identifier
                (``store_name:uuid`` format).

        Returns:
            A 2-tuple of ``(content_stream, metadata_dict)`` on success,
            or ``(None, None)`` if the blob is not found.
        """
        self._ensure_attached()

        from src.app.storage.blobstore import BlobId as _BlobId  # noqa: F811

        store = self.blob_store

        try:
            parsed_id = _BlobId.from_string(blob_id)
        except ValueError:
            self.logger.warning("Invalid blob ID format: '%s'", blob_id)
            return None, None

        try:
            blob = store.get(parsed_id)
            if blob is None:
                self.logger.debug("Blob '%s' not found in store", blob_id)
                return None, None

            stream = store.get_stream(parsed_id)
            metadata = blob.attributes.to_dict() if blob.attributes else {}
            return stream, metadata
        except Exception as exc:
            self.logger.error(
                "Failed to retrieve content for blob '%s': %s",
                blob_id,
                exc,
            )
            return None, None

    def delete_content(self, blob_id: str) -> bool:
        """Delete content from the blob store using soft-delete.

        The blob is marked as deleted but not physically removed.  Actual
        cleanup occurs during scheduled maintenance compaction tasks
        (Feature F-203).

        Args:
            blob_id: String representation of the blob identifier.

        Returns:
            ``True`` if the blob was successfully marked for deletion,
            ``False`` if the blob was not found or deletion failed.
        """
        self._ensure_attached()

        from src.app.storage.blobstore import BlobId as _BlobId  # noqa: F811

        store = self.blob_store

        try:
            parsed_id = _BlobId.from_string(blob_id)
        except ValueError:
            self.logger.warning(
                "Invalid blob ID format for deletion: '%s'", blob_id
            )
            return False

        try:
            result = store.delete(parsed_id, soft=True)
            if result:
                self.logger.debug(
                    "Soft-deleted blob '%s' from store '%s'",
                    blob_id,
                    self._blob_store_name,
                )
            else:
                self.logger.debug(
                    "Blob '%s' not found for deletion in store '%s'",
                    blob_id,
                    self._blob_store_name,
                )
            return result
        except Exception as exc:
            self.logger.error(
                "Failed to delete blob '%s' from store '%s': %s",
                blob_id,
                self._blob_store_name,
                exc,
            )
            return False

    def get_content_size(self, blob_id: str) -> int | None:
        """Get the size of stored content without fetching it.

        Args:
            blob_id: String representation of the blob identifier.

        Returns:
            Content size in bytes, or ``None`` if the blob is not found.
        """
        self._ensure_attached()

        from src.app.storage.blobstore import BlobId as _BlobId  # noqa: F811

        store = self.blob_store

        try:
            parsed_id = _BlobId.from_string(blob_id)
        except ValueError:
            self.logger.warning(
                "Invalid blob ID format for size query: '%s'", blob_id
            )
            return None

        try:
            blob = store.get(parsed_id)
            if blob is None:
                return None
            return blob.size
        except Exception as exc:
            self.logger.error(
                "Failed to get size for blob '%s': %s", blob_id, exc
            )
            return None

    def content_exists(self, blob_id: str) -> bool:
        """Check if content exists in the blob store.

        Args:
            blob_id: String representation of the blob identifier.

        Returns:
            ``True`` if the blob exists and is **not** soft-deleted.
        """
        self._ensure_attached()

        from src.app.storage.blobstore import BlobId as _BlobId  # noqa: F811

        store = self.blob_store

        try:
            parsed_id = _BlobId.from_string(blob_id)
        except ValueError:
            self.logger.warning(
                "Invalid blob ID format for exists check: '%s'", blob_id
            )
            return False

        try:
            return store.exists(parsed_id)
        except Exception as exc:
            self.logger.error(
                "Failed to check existence of blob '%s': %s", blob_id, exc
            )
            return False

    def get_blob_store_metrics(self) -> dict:
        """Return current storage metrics for this repository's blob store.

        Returns:
            Dictionary with storage metrics including blob store name,
            total size, blob count, and available space.  Returns a
            fallback dict with zeroed values on failure.
        """
        self._ensure_attached()
        store = self.blob_store

        try:
            metrics = store.get_metrics()
            return {
                "blob_store_name": self._blob_store_name,
                "total_size_bytes": metrics.total_size_bytes,
                "blob_count": metrics.blob_count,
                "available_space_bytes": metrics.available_space_bytes,
            }
        except Exception as exc:
            self.logger.error(
                "Failed to retrieve metrics for blob store '%s': %s",
                self._blob_store_name,
                exc,
            )
            return {
                "blob_store_name": self._blob_store_name,
                "total_size_bytes": 0,
                "blob_count": 0,
                "available_space_bytes": -1,
            }


# ═══════════════════════════════════════════════════════════════════════════
# SecurityFacet — Security Enforcement Abstraction
# ═══════════════════════════════════════════════════════════════════════════


class SecurityFacet(RepositoryFacet):
    """Facet providing security enforcement for repository operations.

    Integrates with the auth subsystem to check permissions before allowing
    read, write, or admin operations on repository content.  Implements
    the three-tier authorisation model from AAP Section 6.4:

    1. **System-wide** — global admin or anonymous access
    2. **Repository-scoped** — per-repository read/write/admin privileges
    3. **Sub-repository** — asset-level access via Content Selector
       Expressions (CSEL) (Feature F-301, Section 6.4.2)

    The ``check_*`` methods return booleans; the ``ensure_*`` variants
    raise :class:`PermissionError` on denial for ergonomic use in route
    handlers.

    **Java Equivalent:**
    Replaces ``SecurityComponent`` and the Apache Shiro 2.0.0 realm-based
    authorisation filter chain.
    """

    def __init__(self, repository: Repository) -> None:
        """Initialise the security facet.

        Args:
            repository: The :class:`Repository` to enforce permissions on.
        """
        super().__init__(repository)

    # -- Permission check methods (return bool) -----------------------------

    def check_read_permission(self, user: Any = None) -> bool:
        """Check if the user has read permission on this repository.

        The method resolves the current user from Flask's request context
        when *user* is ``None``.  It then evaluates:

        1. System-level admin override (always permitted).
        2. Repository-scoped ``nx-repository-view-*-*-read`` privilege.
        3. Anonymous read if the repository allows it.

        Args:
            user: An optional user object.  When ``None``, the current
                request's authenticated identity is used.

        Returns:
            ``True`` if the user is permitted to read, ``False`` otherwise.
        """
        self._ensure_attached()
        try:
            resolved_user = self._resolve_user(user)
            if self._is_system_admin(resolved_user):
                return True
            return self._has_repository_privilege(
                resolved_user, "read"
            )
        except Exception as exc:
            self.logger.warning(
                "Permission check (read) failed for repository '%s': %s",
                self.name,
                exc,
            )
            return False

    def check_write_permission(self, user: Any = None) -> bool:
        """Check if the user has write permission on this repository.

        Write permission allows uploading artifacts, creating components,
        and modifying assets.

        Args:
            user: Optional user object; uses request context when ``None``.

        Returns:
            ``True`` if write access is granted, ``False`` otherwise.
        """
        self._ensure_attached()
        try:
            resolved_user = self._resolve_user(user)
            if self._is_system_admin(resolved_user):
                return True
            return self._has_repository_privilege(
                resolved_user, "edit"
            )
        except Exception as exc:
            self.logger.warning(
                "Permission check (write) failed for repository '%s': %s",
                self.name,
                exc,
            )
            return False

    def check_admin_permission(self, user: Any = None) -> bool:
        """Check if the user has admin permission on this repository.

        Admin permission allows configuration changes, repository deletion,
        and privilege management.

        Args:
            user: Optional user object; uses request context when ``None``.

        Returns:
            ``True`` if admin access is granted, ``False`` otherwise.
        """
        self._ensure_attached()
        try:
            resolved_user = self._resolve_user(user)
            if self._is_system_admin(resolved_user):
                return True
            return self._has_repository_privilege(
                resolved_user, "admin"
            )
        except Exception as exc:
            self.logger.warning(
                "Permission check (admin) failed for repository '%s': %s",
                self.name,
                exc,
            )
            return False

    # -- Permission enforcement methods (raise on denial) -------------------

    def ensure_read_permission(self, user: Any = None) -> None:
        """Ensure read permission; raise :class:`PermissionError` if denied.

        Args:
            user: Optional user object; uses request context when ``None``.

        Raises:
            PermissionError: If the user lacks read access.
        """
        if not self.check_read_permission(user):
            raise PermissionError(
                f"Read access denied for repository '{self.name}'."
            )

    def ensure_write_permission(self, user: Any = None) -> None:
        """Ensure write permission; raise :class:`PermissionError` if denied.

        Args:
            user: Optional user object; uses request context when ``None``.

        Raises:
            PermissionError: If the user lacks write access.
        """
        if not self.check_write_permission(user):
            raise PermissionError(
                f"Write access denied for repository '{self.name}'."
            )

    def ensure_admin_permission(self, user: Any = None) -> None:
        """Ensure admin permission; raise :class:`PermissionError` if denied.

        Args:
            user: Optional user object; uses request context when ``None``.

        Raises:
            PermissionError: If the user lacks admin access.
        """
        if not self.check_admin_permission(user):
            raise PermissionError(
                f"Admin access denied for repository '{self.name}'."
            )

    # -- Content Selector Expression (CSEL) ---------------------------------

    def get_content_selector_filter(self) -> Any | None:
        """Return the CSEL filter applied to this user for this repository.

        Content Selector Expressions provide sub-repository (asset-level)
        access control as defined in Feature F-301, Section 6.4.2.  When
        a CSEL is configured for the current user's role, only assets
        matching the expression are visible.

        Returns:
            A CSEL filter expression (typically a string or compiled
            expression object), or ``None`` if no CSEL is applied.
        """
        self._ensure_attached()
        try:
            resolved_user = self._resolve_user(None)
            if self._is_system_admin(resolved_user):
                return None  # Admins bypass content selectors

            # Attempt to resolve CSEL from the auth subsystem
            from src.app.auth.content_selector import evaluate_content_selectors
            return evaluate_content_selectors(
                resolved_user, self.name, self.repository.format
            )
        except ImportError:
            # Auth module not yet available — no CSEL filtering
            self.logger.debug(
                "Content selector module not available; "
                "no CSEL filtering applied for repository '%s'",
                self.name,
            )
            return None
        except Exception as exc:
            self.logger.warning(
                "Failed to evaluate content selectors for "
                "repository '%s': %s",
                self.name,
                exc,
            )
            return None

    # -- Internal helpers ---------------------------------------------------

    @staticmethod
    def _resolve_user(user: Any | None) -> Any:
        """Resolve the current user from Flask's request context.

        Args:
            user: An explicit user object, or ``None`` to resolve from
                the request context.

        Returns:
            The resolved user object (may be ``None`` for anonymous
            access).
        """
        if user is not None:
            return user
        try:
            from flask import g
            return getattr(g, "current_user", None)
        except RuntimeError:
            # Outside request context — return None (anonymous)
            return None

    @staticmethod
    def _is_system_admin(user: Any) -> bool:
        """Check if the user has system-wide admin privileges.

        Args:
            user: The user object to check.

        Returns:
            ``True`` if the user is a system administrator.
        """
        if user is None:
            return False
        # Check common admin indicators
        if hasattr(user, "is_admin"):
            return bool(user.is_admin)
        if hasattr(user, "roles"):
            roles = user.roles
            if isinstance(roles, (list, tuple, set)):
                return "nx-admin" in roles or "admin" in roles
        return False

    def _has_repository_privilege(
        self, user: Any, action: str
    ) -> bool:
        """Check if the user has a specific privilege on this repository.

        The privilege name follows the Nexus convention:
        ``nx-repository-view-<format>-<repo_name>-<action>``

        For anonymous access (``user is None``), read access is granted
        if the repository has an ``anonymous_access`` attribute set to
        ``True`` in its configuration.

        Args:
            user: The user object to evaluate.
            action: One of ``'read'``, ``'edit'``, ``'admin'``.

        Returns:
            ``True`` if the privilege is held, ``False`` otherwise.
        """
        if user is None:
            # Anonymous user — only allow read if configured
            if action == "read":
                attrs = self.repository.attributes or {}
                return bool(attrs.get("anonymous_access", False))
            return False

        # Check for privilege via user object
        repo_format = self.repository.format
        repo_name = self.name
        required_privilege = (
            f"nx-repository-view-{repo_format}-{repo_name}-{action}"
        )
        wildcard_privilege = f"nx-repository-view-*-*-{action}"

        if hasattr(user, "has_privilege"):
            return bool(
                user.has_privilege(required_privilege)
                or user.has_privilege(wildcard_privilege)
            )

        if hasattr(user, "privileges"):
            privs = user.privileges
            if isinstance(privs, (list, tuple, set)):
                return (
                    required_privilege in privs
                    or wildcard_privilege in privs
                )

        # Fallback — allow read access by default in development
        if action == "read":
            try:
                if current_app.config.get("DEBUG", False):
                    return True
            except RuntimeError:
                pass

        return False


# ═══════════════════════════════════════════════════════════════════════════
# SearchFacet — Search Indexing Abstraction
# ═══════════════════════════════════════════════════════════════════════════


class SearchFacet(RepositoryFacet):
    """Facet providing search indexing and query capabilities.

    Integrates with the Elasticsearch search backend to maintain the
    component search index (Feature F-103).  Provides per-repository
    indexing, removal, querying, reindexing, and index lifecycle
    management.

    Each repository gets its own logical index name following the pattern
    ``nexus-<format>-<repo_name>`` to enable format-specific search
    behaviour and per-repository index management.

    **Java Equivalent:**
    Replaces the Elasticsearch 2.4.3 index management and search query
    building from the original Sonatype Nexus Repository system.
    """

    def __init__(self, repository: Repository) -> None:
        """Initialise the search facet.

        Args:
            repository: The :class:`Repository` whose components will
                be indexed and searchable.
        """
        super().__init__(repository)
        self._index_name: str = (
            f"nexus-{repository.format}-{repository.name}"
        )

    # -- Index operations ---------------------------------------------------

    def index_component(self, component: Component) -> None:
        """Add or update a component in the search index.

        Builds an Elasticsearch document from the component's attributes
        (namespace, name, version, format, repository_name) and delegates
        to the :class:`IndexManager` for persistence.

        Args:
            component: The :class:`Component` model to index.
        """
        self._ensure_attached()

        try:
            # Inline import to avoid circular dependency at module load time
            from src.app.search.index_manager import IndexManager

            manager = IndexManager()
            result = manager.index_component(component)
            if result:
                self.logger.debug(
                    "Indexed component %s (%s:%s:%s) in repository '%s'",
                    component.id,
                    component.namespace,
                    component.name,
                    component.version,
                    component.repository_name,
                )
            else:
                self.logger.warning(
                    "Failed to index component %s in repository '%s'",
                    component.id,
                    self.name,
                )
        except Exception as exc:
            self.logger.error(
                "Error indexing component %s in repository '%s': %s",
                getattr(component, "id", "?"),
                self.name,
                exc,
            )

    def remove_component(self, component: Component) -> None:
        """Remove a component from the search index.

        Args:
            component: The :class:`Component` model to remove.
        """
        self._ensure_attached()

        try:
            from src.app.search.index_manager import IndexManager

            manager = IndexManager()
            component_id = component.id
            result = manager.remove_component(component_id)
            if result:
                self.logger.debug(
                    "Removed component %s from search index for "
                    "repository '%s'",
                    component_id,
                    self.name,
                )
            else:
                self.logger.warning(
                    "Failed to remove component %s from search index "
                    "for repository '%s'",
                    component_id,
                    self.name,
                )
        except Exception as exc:
            self.logger.error(
                "Error removing component %s from search index for "
                "repository '%s': %s",
                getattr(component, "id", "?"),
                self.name,
                exc,
            )

    def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """Execute a search query against this repository's search index.

        Delegates query construction to :class:`SearchQuery` and execution
        to :func:`execute_search`, filtering results to this repository.

        Args:
            query: Free-text search query string.
            page: 1-based page number (default 1).
            page_size: Number of results per page (default 50).

        Returns:
            Paginated result dictionary::

                {
                    'items': [...],
                    'total_count': <int>,
                    'page': <int>,
                    'page_size': <int>,
                }
        """
        self._ensure_attached()

        try:
            # Inline import to avoid circular dependency
            from src.app.search.query_builder import (
                SearchQuery,
                execute_search,
            )

            search_query = SearchQuery(
                q=query,
                repository=self.name,
                page=page,
                page_size=page_size,
            )
            result = execute_search(search_query)

            # Normalise output to the contract's expected keys
            return {
                "items": result.get("items", []),
                "total_count": result.get("total", 0),
                "page": result.get("page", page),
                "page_size": result.get("page_size", page_size),
            }
        except Exception as exc:
            self.logger.error(
                "Search failed for repository '%s' with query '%s': %s",
                self.name,
                query,
                exc,
            )
            return {
                "items": [],
                "total_count": 0,
                "page": page,
                "page_size": page_size,
            }

    def reindex(self) -> int:
        """Trigger a full reindex of all components in this repository.

        Queries all active components from the database and bulk-indexes
        them into Elasticsearch.  This is a potentially long-running
        operation and should typically be invoked via a scheduled task
        rather than inline.

        Returns:
            The number of successfully indexed documents.
        """
        self._ensure_attached()

        try:
            from src.app.search.index_manager import IndexManager

            manager = IndexManager()

            # Query all active components for this repository
            components = (
                db.session.query(Component)
                .filter(Component.repository_name == self.name)
                .all()
            )

            if not components:
                self.logger.info(
                    "No components to reindex for repository '%s'",
                    self.name,
                )
                return 0

            # Build document list for bulk indexing
            documents: list[dict] = []
            for comp in components:
                repo_format: str | None = None
                if comp.repository is not None:
                    repo_format = getattr(comp.repository, "format", None)

                tags: list[str] = []
                if hasattr(comp, "get_attribute"):
                    tags = comp.get_attribute("tags", [])
                elif isinstance(getattr(comp, "attributes", None), dict):
                    tags = comp.attributes.get("tags", [])

                doc: dict[str, Any] = {
                    "id": comp.id,
                    "repository_name": comp.repository_name,
                    "namespace": comp.namespace,
                    "name": comp.name,
                    "version": comp.version,
                    "format": repo_format,
                    "group": comp.namespace,
                    "tags": tags,
                    "attributes": comp.attributes or {},
                    "created_at": (
                        comp.created_at.isoformat()
                        if getattr(comp, "created_at", None) is not None
                        else None
                    ),
                    "updated_at": (
                        comp.updated_at.isoformat()
                        if getattr(comp, "updated_at", None) is not None
                        else None
                    ),
                }
                documents.append(doc)

            from src.app.search.index_manager import COMPONENT_INDEX

            success_count, errors = manager.bulk_index(
                COMPONENT_INDEX, documents
            )

            self.logger.info(
                "Reindexed %d/%d components for repository '%s' "
                "(%d errors)",
                success_count,
                len(documents),
                self.name,
                len(errors),
            )
            return success_count
        except Exception as exc:
            self.logger.error(
                "Failed to reindex repository '%s': %s", self.name, exc
            )
            return 0

    def delete_index(self) -> bool:
        """Delete the search index for this repository.

        Used during repository deletion to clean up Elasticsearch
        resources.  The operation is idempotent — deleting a non-existent
        index is treated as success.

        Returns:
            ``True`` if the index was successfully deleted or did not
            exist, ``False`` on failure.
        """
        self._ensure_attached()

        try:
            from src.app.search.index_manager import IndexManager

            manager = IndexManager()
            result = manager.delete_index(
                self._index_name, ignore_missing=True
            )
            if result:
                self.logger.info(
                    "Deleted search index '%s' for repository '%s'",
                    self._index_name,
                    self.name,
                )
            return result
        except Exception as exc:
            self.logger.error(
                "Failed to delete search index '%s' for repository '%s': %s",
                self._index_name,
                self.name,
                exc,
            )
            return False


# ═══════════════════════════════════════════════════════════════════════════
# FacetRegistry — Facet Lifecycle Management
# ═══════════════════════════════════════════════════════════════════════════


class FacetRegistry:
    """Registry that manages facet instances for a repository.

    Provides lifecycle management (attach / detach) and lookup by facet
    type.  Used by repository type classes (``HostedRepository``,
    ``ProxyRepository``, ``GroupRepository``) to compose their required
    facets.

    The registry replaces the OSGi service component lifecycle management
    from the Java source, where facets were injected and activated by the
    Karaf container.

    Usage::

        registry = FacetRegistry(repository)
        registry.register(StorageFacet(repository))
        registry.register(SecurityFacet(repository))
        registry.register(SearchFacet(repository))

        # Access via convenience properties
        storage = registry.storage
        security = registry.security

        # Shutdown
        registry.detach_all()
    """

    def __init__(self, repository: Repository) -> None:
        """Initialise the registry for a repository.

        Args:
            repository: The :class:`Repository` model that owns the
                registered facets.
        """
        self.repository: Repository = repository
        self._facets: dict[type, RepositoryFacet] = {}
        self._logger: logging.Logger = logging.getLogger(
            f"{__name__}.FacetRegistry.{repository.name}"
        )

    def register(self, facet: RepositoryFacet) -> None:
        """Register a facet instance and attach it.

        The facet is stored by its concrete type and automatically
        attached (i.e. :meth:`RepositoryFacet.attach` is called).
        Registering a second facet of the same type replaces the
        previous one (the old facet is detached first).

        Args:
            facet: The facet instance to register.
        """
        facet_type = type(facet)

        # Detach existing facet of the same type, if any
        existing = self._facets.get(facet_type)
        if existing is not None:
            existing.detach()
            self._logger.debug(
                "Replaced existing %s facet", facet_type.__name__
            )

        self._facets[facet_type] = facet
        facet.attach()
        self._logger.debug(
            "Registered and attached %s facet for repository '%s'",
            facet_type.__name__,
            self.repository.name,
        )

    def get(self, facet_type: type) -> RepositoryFacet | None:
        """Retrieve a facet by its type.

        Args:
            facet_type: The class of the facet to look up (e.g.
                ``StorageFacet``).

        Returns:
            The registered facet instance, or ``None`` if no facet of
            that type is registered.
        """
        return self._facets.get(facet_type)

    def detach_all(self) -> None:
        """Detach all registered facets and clear the registry.

        Used during repository shutdown or deletion to release all
        facet resources.
        """
        for facet_type, facet in self._facets.items():
            try:
                facet.detach()
            except Exception as exc:
                self._logger.warning(
                    "Error detaching %s facet for repository '%s': %s",
                    facet_type.__name__,
                    self.repository.name,
                    exc,
                )
        self._facets.clear()
        self._logger.debug(
            "All facets detached and registry cleared for repository '%s'",
            self.repository.name,
        )

    # -- Convenience properties ---------------------------------------------

    @property
    def storage(self) -> StorageFacet | None:
        """Return the registered :class:`StorageFacet`, or ``None``."""
        return self.get(StorageFacet)  # type: ignore[return-value]

    @property
    def security(self) -> SecurityFacet | None:
        """Return the registered :class:`SecurityFacet`, or ``None``."""
        return self.get(SecurityFacet)  # type: ignore[return-value]

    @property
    def search(self) -> SearchFacet | None:
        """Return the registered :class:`SearchFacet`, or ``None``."""
        return self.get(SearchFacet)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Module Load Diagnostic
# ---------------------------------------------------------------------------
logger.debug(
    "Repository facets module loaded — exports: %s",
    ", ".join(__all__),
)
