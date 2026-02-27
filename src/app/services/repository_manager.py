"""
Repository Management Service — Central Facade.

This module implements the **RepositoryManager** service, the central Facade
for ALL repository operations in the Nexus Repository Flask application.  It is
the most critical service module, implementing:

- **Feature F-101** — Multi-Format Repository Support (7 formats)
- **Feature F-102** — Repository Types (Hosted, Proxy, Group)
- **Feature F-104** — Browse Tree Navigation
- **Feature F-404** — System Configuration Management for repositories

**Architecture Context:**

Replaces ``RepositoryManagerImpl.java`` from the original Java source system.
Implements three core design patterns from AAP Section 0.4.3:

- **Facade pattern**: Unified interface for repository CRUD, lifecycle state
  management, format validation, component/asset management, and browse.
- **State Machine pattern**: Repository lifecycle transitions (NEW → STARTED
  → STOPPED → DELETED) with bidirectional STOPPED ↔ STARTED support.
- **Factory pattern**: Repository creation with format-specific and
  type-specific configuration ("recipes").

All repository-related API endpoints delegate to this service.

**Event-Driven Architecture:**

CRUD operations emit events via the Blinker-based event system for audit
logging (F-303) and webhook dispatch (F-503):
  - ``REPOSITORY_CREATED``
  - ``REPOSITORY_UPDATED``
  - ``REPOSITORY_DELETED``
  - ``COMPONENT_DELETED``

**Performance Targets (AAP Section 0.7.3):**

- REST API response time: < 500 ms average
- Cached artifact resolution: < 200 ms

**Exports:**

- ``RepositoryManager`` — Service class with 17 public methods
- ``RepositoryState`` — Lifecycle state enum (NEW, STARTED, STOPPED, DELETED)
- ``RepositoryError`` / subclasses — Domain exception hierarchy
- ``SUPPORTED_FORMATS``, ``SUPPORTED_TYPES`` — Valid domain value sets
- ``VALID_TRANSITIONS`` — State machine transition map
- ``DEFAULT_PROXY_ATTRIBUTES``, ``DEFAULT_HOSTED_ATTRIBUTES`` — Config defaults
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from flask import current_app, g, has_request_context

from src.app.extensions import db
from src.app.models.repository import Repository
from src.app.models.component import Component
from src.app.models.asset import Asset
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.utils.helpers import generate_uuid, deep_merge
from src.app.utils.validators import validate_repository_name

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 structured logging from the Java source.
# Provides diagnostic output for repository CRUD, state transitions, and errors.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ===========================================================================
# Constants
# ===========================================================================

# Supported repository formats — all 7 from AAP Section 0.1.1 (Feature F-101)
SUPPORTED_FORMATS: set[str] = {
    "maven2",
    "npm",
    "docker",
    "nuget",
    "pypi",
    "apt",
    "raw",
}

# Supported repository types — from AAP Section 0.2.2 (Feature F-102)
SUPPORTED_TYPES: set[str] = {
    "hosted",
    "proxy",
    "group",
}


# ---------------------------------------------------------------------------
# Repository State Machine Enum
# ---------------------------------------------------------------------------
# Replaces RepositoryImpl.java StateGuard from the Java source.
# Inherits from ``str`` so values can be serialised directly to JSON.
# ---------------------------------------------------------------------------


class RepositoryState(str, Enum):
    """Repository lifecycle states.

    State machine transitions (AAP Section 0.4.3):
        NEW → STARTED
        STARTED → STOPPED
        STOPPED → STARTED   (restart)
        STOPPED → DELETED    (terminal)
    """

    NEW = "new"
    STARTED = "started"
    STOPPED = "stopped"
    DELETED = "deleted"


# Valid state transitions — edges in the state machine graph.
VALID_TRANSITIONS: dict[RepositoryState, set[RepositoryState]] = {
    RepositoryState.NEW: {RepositoryState.STARTED},
    RepositoryState.STARTED: {RepositoryState.STOPPED},
    RepositoryState.STOPPED: {RepositoryState.STARTED, RepositoryState.DELETED},
    RepositoryState.DELETED: set(),  # Terminal state — no outgoing edges
}


# ---------------------------------------------------------------------------
# Default Repository Attributes (Type-Specific Configuration Recipes)
# ---------------------------------------------------------------------------
# These defaults are deep-copied when creating a new repository so that
# module-level constants are never mutated.
# ---------------------------------------------------------------------------

DEFAULT_PROXY_ATTRIBUTES: dict[str, Any] = {
    "connection": {
        "timeout": 20,
        "retries": 3,
        "user_agent_customization": "",
    },
    "proxy": {
        "content_max_age": 1440,        # minutes (24 hours)
        "metadata_max_age": 1440,       # minutes
        "negative_cache_enabled": True,
        "negative_cache_ttl": 1440,     # minutes
    },
}

DEFAULT_HOSTED_ATTRIBUTES: dict[str, Any] = {
    "storage": {
        "write_policy": "ALLOW",  # ALLOW | ALLOW_ONCE | DENY
        "strict_content_type_validation": True,
    },
}


# ---------------------------------------------------------------------------
# Request Context Helper
# ---------------------------------------------------------------------------
# Safely extracts the authenticated user's ID from the Flask request
# context (g.current_user) for inclusion in event payloads.  Returns
# None when called outside a request context (e.g., scheduled tasks).
# ---------------------------------------------------------------------------


def _get_request_user_id() -> str | None:
    """Extract the current authenticated user_id from Flask g context.

    Returns:
        The user_id string if a user is authenticated in the current
        request, or ``None`` if called outside a request context or
        when no user is authenticated.
    """
    if not has_request_context():
        return None
    try:
        current_user = getattr(g, "current_user", None)
        if current_user is not None:
            return getattr(current_user, "user_id", None)
    except RuntimeError:
        pass
    return None


# ===========================================================================
# Custom Exception Hierarchy
# ===========================================================================
# Replaces InvalidStateException.java, BypassHttpErrorException.java, and
# other custom exception classes from the Java source system.
# ===========================================================================


class RepositoryError(Exception):
    """Base exception for all repository-related errors.

    Attributes:
        message: Human-readable description of the error.
        repository_name: Name of the repository involved (may be ``None``).
    """

    def __init__(
        self, message: str, repository_name: str | None = None
    ) -> None:
        self.message = message
        self.repository_name = repository_name
        super().__init__(message)


class RepositoryNotFoundError(RepositoryError):
    """Raised when a requested repository does not exist.

    Attributes:
        message: Human-readable error description.
        repository_name: Name of the missing repository.
    """

    def __init__(
        self, message: str | None = None, repository_name: str | None = None
    ) -> None:
        if message is None:
            message = (
                f"Repository '{repository_name}' not found"
                if repository_name
                else "Repository not found"
            )
        super().__init__(message, repository_name=repository_name)


class RepositoryExistsError(RepositoryError):
    """Raised when creating a repository whose name already exists.

    Attributes:
        message: Human-readable error description.
        repository_name: Name of the conflicting repository.
    """

    def __init__(
        self, message: str | None = None, repository_name: str | None = None
    ) -> None:
        if message is None:
            message = (
                f"Repository '{repository_name}' already exists"
                if repository_name
                else "Repository already exists"
            )
        super().__init__(message, repository_name=repository_name)


class InvalidRepositoryConfigError(RepositoryError):
    """Raised for invalid repository configuration or parameters.

    Covers format validation, type validation, missing required fields,
    and invalid attribute values.

    Attributes:
        message: Human-readable error description.
        repository_name: Name of the repository (may be ``None`` if error
            occurs before name validation).
    """

    def __init__(
        self, message: str, repository_name: str | None = None
    ) -> None:
        super().__init__(message, repository_name=repository_name)


class InvalidStateTransitionError(RepositoryError):
    """Raised when a lifecycle state transition violates the state machine.

    Replaces ``InvalidStateException.java`` from the Java source system.

    Attributes:
        message: Human-readable error description.
        repository_name: Name of the repository.
    """

    def __init__(
        self, message: str, repository_name: str | None = None
    ) -> None:
        super().__init__(message, repository_name=repository_name)


class RepositoryOfflineError(RepositoryError):
    """Raised when attempting to access a repository that is offline.

    Attributes:
        message: Human-readable error description.
        repository_name: Name of the offline repository.
    """

    def __init__(
        self, message: str | None = None, repository_name: str | None = None
    ) -> None:
        if message is None:
            message = (
                f"Repository '{repository_name}' is offline"
                if repository_name
                else "Repository is offline"
            )
        super().__init__(message, repository_name=repository_name)


# ===========================================================================
# RepositoryManager — Central Facade Service
# ===========================================================================


class RepositoryManager:
    """Central Facade for ALL repository operations.

    Implements the Facade pattern (AAP Section 0.4.3), providing a unified
    interface for:

    - Repository CRUD (create, read, update, delete)
    - Repository lifecycle state management (State Machine pattern)
    - Format and type validation
    - Component and asset management
    - Browse tree navigation (Feature F-104)
    - Repository status and statistics

    All repository-related API endpoints in ``src.app.api.repositories`` (and
    related blueprints) delegate to an instance of this class.

    Usage::

        manager = RepositoryManager()
        repo = manager.create_repository(
            name="maven-central",
            format_type="maven2",
            repo_type="proxy",
            remote_url="https://repo1.maven.org/maven2/",
        )
        manager.start_repository("maven-central")
        components = manager.get_components("maven-central", page=1)
    """

    def __init__(self) -> None:
        """Initialise the RepositoryManager.

        Creates a dedicated logger instance for this service.
        """
        self.logger: logging.Logger = logging.getLogger(__name__)

    # ===================================================================
    # Repository CRUD Operations
    # ===================================================================

    def create_repository(
        self,
        name: str,
        format_type: str,
        repo_type: str,
        blob_store_name: str = "default",
        online: bool = True,
        attributes: dict | None = None,
        **kwargs: Any,
    ) -> Repository:
        """Create a new repository with format-specific configuration.

        Implements the Factory pattern (AAP Section 0.4.3) — repository
        creation uses type-specific "recipes" that apply default configuration
        before merging any user-provided overrides.

        Args:
            name: Unique repository name (validated against naming rules).
            format_type: Repository format — one of ``SUPPORTED_FORMATS``.
            repo_type: Repository type — one of ``SUPPORTED_TYPES``.
            blob_store_name: Name of the BlobStore backend (default: 'default').
            online: Whether the repository starts accepting requests.
            attributes: Optional dict of additional attributes to merge.
            **kwargs: Type-specific parameters:
                - ``remote_url`` (str): Required for proxy repositories.
                - ``member_names`` (list[str]): Required for group repos.
                - ``write_policy`` (str): Optional for hosted.

        Returns:
            The newly created ``Repository`` model instance.

        Raises:
            InvalidRepositoryConfigError: If validation fails.
            RepositoryExistsError: If a repository with the given name exists.
        """
        # --- Validation Phase ---
        self._validate_name(name)
        self._validate_format(format_type)
        self._validate_type(repo_type)
        self._validate_blobstore(blob_store_name)

        # Uniqueness check
        existing = db.session.get(Repository, name)
        if existing is not None:
            raise RepositoryExistsError(repository_name=name)

        # Type-specific validation
        if repo_type == "proxy":
            remote_url = kwargs.get("remote_url") or (
                attributes.get("proxy", {}).get("remote_url")
                if attributes
                else None
            )
            if not remote_url:
                raise InvalidRepositoryConfigError(
                    "Proxy repositories require a 'remote_url' parameter",
                    repository_name=name,
                )
            self._validate_url(remote_url, name)

        elif repo_type == "group":
            member_names = kwargs.get("member_names") or (
                attributes.get("group", {}).get("memberNames")
                if attributes
                else None
            )
            if not member_names:
                raise InvalidRepositoryConfigError(
                    "Group repositories require a 'member_names' parameter",
                    repository_name=name,
                )
            self._validate_group_config(
                {"group": {"memberNames": member_names}},
                format_type,
                repository_name=name,
            )

        # --- Build Attributes Phase ---
        repo_attributes: dict[str, Any] = {}

        if repo_type == "proxy":
            repo_attributes = copy.deepcopy(DEFAULT_PROXY_ATTRIBUTES)
            remote_url_val = kwargs.get("remote_url") or (
                attributes.get("proxy", {}).get("remote_url")
                if attributes
                else ""
            )
            repo_attributes["proxy"]["remote_url"] = remote_url_val

        elif repo_type == "hosted":
            repo_attributes = copy.deepcopy(DEFAULT_HOSTED_ATTRIBUTES)
            write_policy = kwargs.get("write_policy")
            if write_policy:
                repo_attributes["storage"]["write_policy"] = write_policy

        elif repo_type == "group":
            member_names_val = kwargs.get("member_names", [])
            if not member_names_val and attributes:
                member_names_val = (
                    attributes.get("group", {}).get("memberNames", [])
                )
            repo_attributes["group"] = {
                "memberNames": list(member_names_val),
            }

        # Deep merge user-provided attributes over type defaults
        if attributes:
            repo_attributes = deep_merge(repo_attributes, attributes)

        # Inject initial lifecycle state
        repo_attributes["state"] = RepositoryState.NEW.value

        # --- Persistence Phase ---
        try:
            repository = Repository(
                name=name,
                format=format_type,
                type=repo_type,
                blob_store_name=blob_store_name,
                online=online,
                attributes=repo_attributes,
            )
            db.session.add(repository)
            db.session.commit()

            self.logger.info(
                "Repository created: name=%s, format=%s, type=%s, "
                "blobstore=%s",
                name,
                format_type,
                repo_type,
                blob_store_name,
            )

        except Exception:
            db.session.rollback()
            self.logger.error(
                "Failed to create repository '%s'",
                name,
                exc_info=True,
            )
            raise

        # NOTE: Event emission for REPOSITORY_CREATED is handled by the API
        # layer (src.app.api.repositories) which enriches the payload with
        # request context (ip_address, source).  Emitting here would cause
        # duplicate audit events for every API-driven creation.

        # Auto-start if online
        if online:
            try:
                self._set_state(repository, RepositoryState.STARTED)
            except InvalidStateTransitionError:
                self.logger.warning(
                    "Could not auto-start repository '%s' after creation",
                    name,
                )

        return repository

    def get_repository(self, name: str) -> Repository | None:
        """Retrieve a repository by name.

        Args:
            name: Unique repository name (primary key).

        Returns:
            The ``Repository`` instance if found, or ``None``.
        """
        return db.session.get(Repository, name)

    def get_repository_or_raise(self, name: str) -> Repository:
        """Retrieve a repository by name, raising if not found.

        Args:
            name: Unique repository name (primary key).

        Returns:
            The ``Repository`` instance.

        Raises:
            RepositoryNotFoundError: If no repository with the given name
                exists.
        """
        repository = db.session.get(Repository, name)
        if repository is None:
            raise RepositoryNotFoundError(repository_name=name)
        return repository

    def list_repositories(
        self,
        format_type: str | None = None,
        repo_type: str | None = None,
        online: bool | None = None,
    ) -> list[Repository]:
        """List repositories with optional filters.

        Args:
            format_type: Filter by format (e.g., ``'maven2'``, ``'npm'``).
            repo_type: Filter by type (e.g., ``'hosted'``, ``'proxy'``).
            online: Filter by online status (``True`` / ``False``).

        Returns:
            List of matching ``Repository`` instances, ordered by name.
        """
        q = Repository.query

        if format_type is not None:
            q = q.filter(Repository.format == format_type)
        if repo_type is not None:
            q = q.filter(Repository.type == repo_type)
        if online is not None:
            q = q.filter(Repository.online == online)

        return q.order_by(Repository.name).all()

    def update_repository(
        self,
        name: str,
        online: bool | None = None,
        attributes: dict | None = None,
        blob_store_name: str | None = None,
    ) -> Repository:
        """Update an existing repository's configuration.

        Only specified (non-``None``) fields are updated.  Attributes are
        deep-merged with the existing attribute dict so that partial updates
        are supported without losing unspecified keys.

        Args:
            name: Name of the repository to update.
            online: New online status (or ``None`` to leave unchanged).
            attributes: Dict to deep-merge into existing attributes.
            blob_store_name: New BlobStore name (validated for existence).

        Returns:
            The updated ``Repository`` instance.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
            InvalidRepositoryConfigError: If the new blob_store_name is
                invalid.
        """
        repository = self.get_repository_or_raise(name)

        try:
            if online is not None:
                repository.online = online
                self.logger.info(
                    "Repository '%s' online status updated to %s",
                    name,
                    online,
                )

            if blob_store_name is not None:
                self._validate_blobstore(blob_store_name)
                repository.blob_store_name = blob_store_name
                self.logger.info(
                    "Repository '%s' blob store updated to '%s'",
                    name,
                    blob_store_name,
                )

            if attributes is not None:
                existing_attrs = repository.attributes or {}
                merged = deep_merge(existing_attrs, attributes)
                repository.attributes = merged
                self.logger.info(
                    "Repository '%s' attributes updated", name,
                )

            db.session.commit()

        except RepositoryError:
            db.session.rollback()
            raise
        except Exception:
            db.session.rollback()
            self.logger.error(
                "Failed to update repository '%s'",
                name,
                exc_info=True,
            )
            raise

        # NOTE: Event emission for REPOSITORY_UPDATED is handled by the API
        # layer (src.app.api.repositories) which enriches the payload with
        # request context (ip_address, source).  Emitting here would cause
        # duplicate audit events for every API-driven update.

        return repository

    def delete_repository(self, name: str, force: bool = False) -> bool:
        """Delete a repository and all its contents.

        Deletes all assets, components, and the repository record itself.
        By default, the repository must be in STOPPED state.  Use
        ``force=True`` to bypass state and dependency checks.

        Args:
            name: Name of the repository to delete.
            force: If ``True``, bypass state check and group dependency
                check.

        Returns:
            ``True`` on successful deletion.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
            InvalidStateTransitionError: If the repository is not stopped
                (and ``force`` is ``False``).
            InvalidRepositoryConfigError: If the repository is used as a
                group member (and ``force`` is ``False``).
        """
        repository = self.get_repository_or_raise(name)

        # --- State Check ---
        if not force:
            current_state = self._get_state(repository)
            if current_state not in (
                RepositoryState.STOPPED,
                RepositoryState.NEW,
            ):
                raise InvalidStateTransitionError(
                    f"Repository '{name}' must be stopped before deletion "
                    f"(current state: {current_state.value})",
                    repository_name=name,
                )

        # --- Group Dependency Check ---
        if not force:
            dependents = self._find_group_dependents(name)
            if dependents:
                dependent_names = ", ".join(dependents)
                raise InvalidRepositoryConfigError(
                    f"Repository '{name}' is a member of group "
                    f"repositories: {dependent_names}. Remove it from "
                    f"groups first or use force=True.",
                    repository_name=name,
                )

        try:
            # Delete all assets for this repository
            Asset.query.filter(
                Asset.repository_name == name
            ).delete(synchronize_session="fetch")

            # Delete all components for this repository
            Component.query.filter(
                Component.repository_name == name
            ).delete(synchronize_session="fetch")

            # Delete the repository record
            db.session.delete(repository)
            db.session.commit()

            self.logger.info(
                "Repository deleted: name=%s, force=%s", name, force,
            )

        except Exception:
            db.session.rollback()
            self.logger.error(
                "Failed to delete repository '%s'",
                name,
                exc_info=True,
            )
            raise

        # NOTE: Event emission for REPOSITORY_DELETED is handled by the API
        # layer (src.app.api.repositories) which enriches the payload with
        # request context (ip_address, source).  Emitting here would cause
        # duplicate audit events for every API-driven deletion.

        return True

    # ===================================================================
    # Repository Lifecycle State Machine
    # ===================================================================

    def _get_state(self, repository: Repository) -> RepositoryState:
        """Read the current lifecycle state from repository attributes.

        Args:
            repository: The repository model instance.

        Returns:
            Current ``RepositoryState``.  Defaults to ``NEW`` if the state
            attribute has not been set.
        """
        attrs = repository.attributes or {}
        state_value = attrs.get("state", RepositoryState.NEW.value)
        try:
            return RepositoryState(state_value)
        except ValueError:
            self.logger.warning(
                "Repository '%s' has invalid state '%s'; defaulting to NEW",
                repository.name,
                state_value,
            )
            return RepositoryState.NEW

    def _set_state(
        self, repository: Repository, new_state: RepositoryState
    ) -> None:
        """Transition repository to a new lifecycle state.

        Validates the transition against ``VALID_TRANSITIONS`` and raises
        ``InvalidStateTransitionError`` if the transition is not allowed.

        Args:
            repository: The repository model instance.
            new_state: Target state to transition to.

        Raises:
            InvalidStateTransitionError: If the transition is not valid.
        """
        current_state = self._get_state(repository)
        allowed = VALID_TRANSITIONS.get(current_state, set())

        if new_state not in allowed:
            raise InvalidStateTransitionError(
                f"Invalid state transition for repository "
                f"'{repository.name}': {current_state.value} → "
                f"{new_state.value}. Allowed transitions: "
                f"{', '.join(s.value for s in allowed) or 'none'}",
                repository_name=repository.name,
            )

        # Update state via set_attribute for SQLAlchemy mutation tracking
        repository.set_attribute("state", new_state.value)
        db.session.commit()

        self.logger.info(
            "Repository '%s' state transition: %s → %s",
            repository.name,
            current_state.value,
            new_state.value,
        )

    def start_repository(self, name: str) -> Repository:
        """Start a repository (NEW → STARTED or STOPPED → STARTED).

        Sets the repository online and transitions its lifecycle state.

        Args:
            name: Name of the repository to start.

        Returns:
            The started ``Repository`` instance.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
            InvalidStateTransitionError: If the current state does not
                allow starting.
        """
        repository = self.get_repository_or_raise(name)
        self._set_state(repository, RepositoryState.STARTED)
        repository.online = True
        db.session.commit()
        self.logger.info("Repository '%s' started", name)
        return repository

    def stop_repository(self, name: str) -> Repository:
        """Stop a repository (STARTED → STOPPED).

        Sets the repository offline and transitions its lifecycle state.

        Args:
            name: Name of the repository to stop.

        Returns:
            The stopped ``Repository`` instance.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
            InvalidStateTransitionError: If the current state does not
                allow stopping.
        """
        repository = self.get_repository_or_raise(name)
        self._set_state(repository, RepositoryState.STOPPED)
        repository.online = False
        db.session.commit()
        self.logger.info("Repository '%s' stopped", name)
        return repository

    # ===================================================================
    # Component and Asset Operations
    # ===================================================================

    def get_components(
        self,
        repository_name: str,
        namespace: str | None = None,
        name: str | None = None,
        version: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """List components in a repository with optional filters and pagination.

        Args:
            repository_name: Name of the repository to query.
            namespace: Filter by namespace (e.g., Maven groupId).
            name: Filter by component name.
            version: Filter by version.
            page: Page number (1-based).
            page_size: Number of items per page (max 200).

        Returns:
            Dict with keys ``'items'``, ``'total_count'``, ``'page'``,
            ``'page_size'``.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
            RepositoryOfflineError: If the repository is offline.
        """
        repository = self.get_repository_or_raise(repository_name)
        if not repository.online:
            raise RepositoryOfflineError(repository_name=repository_name)

        # Clamp page_size to [1, 200]
        page_size = max(1, min(page_size, 200))
        page = max(1, page)

        q = Component.query.filter(
            Component.repository_name == repository_name
        )

        if namespace is not None:
            q = q.filter(Component.namespace == namespace)
        if name is not None:
            q = q.filter(Component.name == name)
        if version is not None:
            q = q.filter(Component.version == version)

        total_count = q.count()
        offset = (page - 1) * page_size
        items = q.order_by(Component.name, Component.version).offset(
            offset
        ).limit(page_size).all()

        return {
            "items": items,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
        }

    def get_component(
        self, repository_name: str, component_id: int
    ) -> Component | None:
        """Retrieve a specific component by ID within a repository.

        Args:
            repository_name: Name of the owning repository.
            component_id: Component primary key.

        Returns:
            The ``Component`` instance if found and belongs to the
            specified repository, or ``None``.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
        """
        self.get_repository_or_raise(repository_name)
        component = Component.query.filter_by(
            id=component_id, repository_name=repository_name
        ).first()
        return component

    def get_assets(
        self,
        repository_name: str,
        component_id: int | None = None,
        path: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """List assets in a repository with optional filters and pagination.

        Args:
            repository_name: Name of the repository to query.
            component_id: Filter by parent component ID.
            path: Filter by asset path prefix.
            page: Page number (1-based).
            page_size: Number of items per page (max 200).

        Returns:
            Dict with keys ``'items'``, ``'total_count'``, ``'page'``,
            ``'page_size'``.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
            RepositoryOfflineError: If the repository is offline.
        """
        repository = self.get_repository_or_raise(repository_name)
        if not repository.online:
            raise RepositoryOfflineError(repository_name=repository_name)

        page_size = max(1, min(page_size, 200))
        page = max(1, page)

        q = Asset.query.filter(
            Asset.repository_name == repository_name
        )

        if component_id is not None:
            q = q.filter(Asset.component_id == component_id)
        if path is not None:
            q = q.filter(Asset.path.like(f"{path}%"))

        total_count = q.count()
        offset = (page - 1) * page_size
        items = q.order_by(Asset.path).offset(offset).limit(
            page_size
        ).all()

        return {
            "items": items,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
        }

    def get_asset(
        self, repository_name: str, asset_id: int
    ) -> Asset | None:
        """Retrieve a specific asset by ID within a repository.

        Args:
            repository_name: Name of the owning repository.
            asset_id: Asset primary key.

        Returns:
            The ``Asset`` instance if found and belongs to the specified
            repository, or ``None``.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
        """
        self.get_repository_or_raise(repository_name)
        asset = Asset.query.filter_by(
            id=asset_id, repository_name=repository_name
        ).first()
        return asset

    def delete_component(
        self, repository_name: str, component_id: int
    ) -> bool:
        """Delete a component and all its assets.

        Removes the component, cascades to all owned assets, and emits
        a ``COMPONENT_DELETED`` event for audit logging and webhooks.

        Args:
            repository_name: Name of the owning repository.
            component_id: Component primary key.

        Returns:
            ``True`` on successful deletion.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
            RepositoryNotFoundError: If the component is not found in
                the specified repository.
        """
        self.get_repository_or_raise(repository_name)

        component = Component.query.filter_by(
            id=component_id, repository_name=repository_name
        ).first()

        if component is None:
            raise RepositoryNotFoundError(
                message=(
                    f"Component {component_id} not found in repository "
                    f"'{repository_name}'"
                ),
                repository_name=repository_name,
            )

        component_name = component.name
        component_version = component.version
        component_namespace = component.namespace

        try:
            # Delete all assets belonging to this component
            Asset.query.filter(
                Asset.component_id == component_id
            ).delete(synchronize_session="fetch")

            # Delete the component
            db.session.delete(component)
            db.session.commit()

            self.logger.info(
                "Component deleted: id=%d, name=%s, repo=%s",
                component_id,
                component_name,
                repository_name,
            )

        except Exception:
            db.session.rollback()
            self.logger.error(
                "Failed to delete component %d in repository '%s'",
                component_id,
                repository_name,
                exc_info=True,
            )
            raise

        # Emit component deleted event
        emit_event(
            EventType.COMPONENT_DELETED,
            {
                "repository_name": repository_name,
                "component_id": component_id,
                "component_name": component_name,
                "component_version": component_version,
                "namespace": component_namespace,
                "user_id": _get_request_user_id(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        return True

    def delete_asset(
        self, repository_name: str, asset_id: int
    ) -> bool:
        """Delete a single asset.

        If the parent component has no remaining assets after deletion,
        the orphaned component is also removed.

        Args:
            repository_name: Name of the owning repository.
            asset_id: Asset primary key.

        Returns:
            ``True`` on successful deletion.

        Raises:
            RepositoryNotFoundError: If the repository or asset is not
                found.
        """
        self.get_repository_or_raise(repository_name)

        asset = Asset.query.filter_by(
            id=asset_id, repository_name=repository_name
        ).first()

        if asset is None:
            raise RepositoryNotFoundError(
                message=(
                    f"Asset {asset_id} not found in repository "
                    f"'{repository_name}'"
                ),
                repository_name=repository_name,
            )

        parent_component_id = asset.component_id

        try:
            db.session.delete(asset)
            db.session.commit()

            self.logger.info(
                "Asset deleted: id=%d, path=%s, repo=%s",
                asset_id,
                asset.path,
                repository_name,
            )

            # Orphan detection: if parent component has no remaining
            # assets, remove the orphaned component
            if parent_component_id is not None:
                remaining = Asset.query.filter_by(
                    component_id=parent_component_id
                ).count()
                if remaining == 0:
                    orphan = db.session.get(Component, parent_component_id)
                    if orphan is not None:
                        db.session.delete(orphan)
                        db.session.commit()
                        self.logger.info(
                            "Orphaned component %d removed after last "
                            "asset deletion",
                            parent_component_id,
                        )

        except Exception:
            db.session.rollback()
            self.logger.error(
                "Failed to delete asset %d in repository '%s'",
                asset_id,
                repository_name,
                exc_info=True,
            )
            raise

        return True

    # ===================================================================
    # Browse Tree Navigation (Feature F-104)
    # ===================================================================

    def browse_repository(
        self, repository_name: str, path: str = "/"
    ) -> dict[str, Any]:
        """Build a virtual directory tree from asset paths in a repository.

        Implements Feature F-104 (Browse Tree Navigation).  Asset paths are
        parsed to construct a folder hierarchy, presenting the repository
        contents as a navigable directory structure.

        Args:
            repository_name: Name of the repository to browse.
            path: Directory path to list (default: root ``'/'``).

        Returns:
            Dict with keys:
              - ``'path'``: The queried path.
              - ``'children'``: List of child entries (folders and files).
              - ``'leaf_assets'``: Assets located exactly at this path.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
        """
        self.get_repository_or_raise(repository_name)

        # Normalise path: ensure leading / and trailing /
        if not path.startswith("/"):
            path = "/" + path
        browse_path = path.rstrip("/") + "/" if path != "/" else "/"

        # Query all assets under this path prefix
        assets = Asset.query.filter(
            Asset.repository_name == repository_name,
            Asset.path.like(f"{browse_path}%"),
        ).all()

        children: list[dict[str, Any]] = []
        leaf_assets: list[dict[str, Any]] = []
        seen_folders: set[str] = set()

        prefix_len = len(browse_path)

        for asset in assets:
            # Get the relative path after the browse prefix
            relative = asset.path[prefix_len:]
            if not relative:
                continue

            parts = relative.split("/")

            if len(parts) == 1:
                # This is a leaf file at the current directory level
                leaf_assets.append({
                    "name": parts[0],
                    "type": "file",
                    "path": asset.path,
                    "size": asset.size,
                    "id": asset.id,
                })
            else:
                # This asset is in a subdirectory — register the folder
                folder_name = parts[0]
                if folder_name and folder_name not in seen_folders:
                    seen_folders.add(folder_name)
                    folder_path = browse_path + folder_name + "/"
                    children.append({
                        "name": folder_name,
                        "type": "folder",
                        "path": folder_path,
                    })

        # Sort children alphabetically: folders first, then files
        children.sort(key=lambda c: c["name"])
        leaf_assets.sort(key=lambda a: a["name"])

        return {
            "path": path,
            "children": children + leaf_assets,
            "leaf_assets": leaf_assets,
        }

    # ===================================================================
    # Repository Information and Statistics
    # ===================================================================

    def get_repository_status(self, name: str) -> dict[str, Any]:
        """Return comprehensive status information for a repository.

        Args:
            name: Name of the repository.

        Returns:
            Dict containing name, format, type, online status, lifecycle
            state, blob store, component count, asset count, and total
            storage size.

        Raises:
            RepositoryNotFoundError: If the repository does not exist.
        """
        repository = self.get_repository_or_raise(name)
        state = self._get_state(repository)

        # Compute total storage size from asset sizes
        total_size_result = db.session.query(
            db.func.coalesce(db.func.sum(Asset.size), 0)
        ).filter(
            Asset.repository_name == name
        ).scalar()

        return {
            "name": name,
            "format": repository.format,
            "type": repository.type,
            "online": repository.online,
            "state": state.value,
            "blob_store": repository.blob_store_name,
            "component_count": repository.component_count,
            "asset_count": repository.asset_count,
            "total_size": int(total_size_result or 0),
        }

    def get_all_repository_status(self) -> list[dict[str, Any]]:
        """Return status information for all repositories.

        Returns:
            List of status dicts (same structure as
            ``get_repository_status``).
        """
        repositories = Repository.query.order_by(Repository.name).all()
        results: list[dict[str, Any]] = []

        for repo in repositories:
            state = self._get_state(repo)

            # Compute total size per repository
            total_size_result = db.session.query(
                db.func.coalesce(db.func.sum(Asset.size), 0)
            ).filter(
                Asset.repository_name == repo.name
            ).scalar()

            results.append({
                "name": repo.name,
                "format": repo.format,
                "type": repo.type,
                "online": repo.online,
                "state": state.value,
                "blob_store": repo.blob_store_name,
                "component_count": repo.component_count,
                "asset_count": repo.asset_count,
                "total_size": int(total_size_result or 0),
            })

        return results

    # ===================================================================
    # Validation Helpers (Private)
    # ===================================================================

    def _validate_name(self, name: str) -> None:
        """Validate repository name format.

        Delegates to ``validate_repository_name`` from validators module
        and converts any ``ValueError`` to ``InvalidRepositoryConfigError``.

        Args:
            name: Repository name to validate.

        Raises:
            InvalidRepositoryConfigError: If the name is invalid.
        """
        try:
            validate_repository_name(name)
        except ValueError as exc:
            raise InvalidRepositoryConfigError(
                str(exc), repository_name=name
            ) from exc

    def _validate_format(self, format_type: str) -> None:
        """Validate that the format is supported.

        Args:
            format_type: Format string to validate.

        Raises:
            InvalidRepositoryConfigError: If the format is not in
                ``SUPPORTED_FORMATS``.
        """
        if format_type not in SUPPORTED_FORMATS:
            raise InvalidRepositoryConfigError(
                f"Unsupported repository format: '{format_type}'. "
                f"Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}"
            )

    def _validate_type(self, repo_type: str) -> None:
        """Validate that the repository type is supported.

        Args:
            repo_type: Type string to validate.

        Raises:
            InvalidRepositoryConfigError: If the type is not in
                ``SUPPORTED_TYPES``.
        """
        if repo_type not in SUPPORTED_TYPES:
            raise InvalidRepositoryConfigError(
                f"Unsupported repository type: '{repo_type}'. "
                f"Supported types: {', '.join(sorted(SUPPORTED_TYPES))}"
            )

    def _validate_url(self, url: str, repository_name: str | None = None) -> None:
        """Validate a remote URL for proxy repositories.

        Args:
            url: URL string to validate.
            repository_name: Repository name for error context.

        Raises:
            InvalidRepositoryConfigError: If the URL is malformed or uses
                an unsupported scheme.
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                raise InvalidRepositoryConfigError(
                    f"Remote URL must use http or https scheme: '{url}'",
                    repository_name=repository_name,
                )
            if not parsed.netloc:
                raise InvalidRepositoryConfigError(
                    f"Remote URL has no host: '{url}'",
                    repository_name=repository_name,
                )
        except InvalidRepositoryConfigError:
            raise
        except Exception as exc:
            raise InvalidRepositoryConfigError(
                f"Invalid remote URL: '{url}' — {exc}",
                repository_name=repository_name,
            ) from exc

    def _validate_proxy_config(self, attributes: dict[str, Any]) -> None:
        """Validate proxy-specific configuration attributes.

        Ensures ``remote_url`` is present and valid, and that connection
        parameters are positive.

        Args:
            attributes: Repository attributes dict to validate.

        Raises:
            InvalidRepositoryConfigError: If proxy config is invalid.
        """
        proxy_config = attributes.get("proxy", {})
        remote_url = proxy_config.get("remote_url")
        if not remote_url:
            raise InvalidRepositoryConfigError(
                "Proxy configuration requires 'remote_url'"
            )
        self._validate_url(remote_url)

        connection = attributes.get("connection", {})
        timeout = connection.get("timeout")
        if timeout is not None and (
            not isinstance(timeout, (int, float)) or timeout <= 0
        ):
            raise InvalidRepositoryConfigError(
                f"Connection timeout must be a positive number, got: "
                f"{timeout}"
            )

        retries = connection.get("retries")
        if retries is not None and (
            not isinstance(retries, int) or retries < 0
        ):
            raise InvalidRepositoryConfigError(
                f"Connection retries must be a non-negative integer, "
                f"got: {retries}"
            )

    def _validate_group_config(
        self,
        attributes: dict[str, Any],
        format_type: str,
        repository_name: str | None = None,
    ) -> None:
        """Validate group-specific configuration attributes.

        Checks that:
        - ``member_names`` is a non-empty list.
        - Each member repository exists.
        - Each member has the same format.
        - No circular references exist.

        Args:
            attributes: Repository attributes dict to validate.
            format_type: Expected format for all members.
            repository_name: Name of the group being validated (for
                circular reference detection).

        Raises:
            InvalidRepositoryConfigError: If group config is invalid.
        """
        group_config = attributes.get("group", {})
        member_names = group_config.get("memberNames", [])

        if not member_names or not isinstance(member_names, (list, tuple)):
            raise InvalidRepositoryConfigError(
                "Group configuration requires a non-empty 'member_names' "
                "list",
                repository_name=repository_name,
            )

        for member_name in member_names:
            member = db.session.get(Repository, member_name)
            if member is None:
                raise InvalidRepositoryConfigError(
                    f"Group member repository '{member_name}' does not "
                    f"exist",
                    repository_name=repository_name,
                )
            if member.format != format_type:
                raise InvalidRepositoryConfigError(
                    f"Group member '{member_name}' has format "
                    f"'{member.format}' but group requires "
                    f"'{format_type}'",
                    repository_name=repository_name,
                )

        # Circular reference detection
        if repository_name:
            self._detect_circular_group(
                repository_name, member_names, format_type
            )

    def _detect_circular_group(
        self,
        group_name: str,
        member_names: list[str],
        format_type: str,
        visited: set[str] | None = None,
    ) -> None:
        """Detect circular references in group repository membership.

        Uses depth-first traversal to check if any member (or transitive
        member) references the group being created/updated.

        Args:
            group_name: Name of the group repository being validated.
            member_names: Direct member names to check.
            format_type: Expected format type.
            visited: Set of already-visited repository names (for DFS).

        Raises:
            InvalidRepositoryConfigError: If a circular reference is
                detected.
        """
        if visited is None:
            visited = set()

        visited.add(group_name)

        for member_name in member_names:
            if member_name in visited:
                raise InvalidRepositoryConfigError(
                    f"Circular group reference detected: "
                    f"'{group_name}' → '{member_name}'",
                    repository_name=group_name,
                )

            member = db.session.get(Repository, member_name)
            if member is not None and member.type == "group":
                nested_members = member.group_members
                if nested_members:
                    self._detect_circular_group(
                        group_name,
                        nested_members,
                        format_type,
                        visited=set(visited),
                    )

    def _validate_blobstore(self, blob_store_name: str) -> None:
        """Validate that a named BlobStore exists.

        Args:
            blob_store_name: BlobStore name to validate.

        Raises:
            InvalidRepositoryConfigError: If the BlobStore does not exist.
        """
        blobstore = db.session.get(BlobStoreConfig, blob_store_name)
        if blobstore is None:
            raise InvalidRepositoryConfigError(
                f"BlobStore '{blob_store_name}' does not exist"
            )

    def _find_group_dependents(self, name: str) -> list[str]:
        """Find all group repositories that include the given repository.

        Scans all group-type repositories and checks if ``name`` appears
        in their ``member_names`` attribute.

        Args:
            name: Repository name to search for as a group member.

        Returns:
            List of group repository names that reference the given
            repository.
        """
        dependents: list[str] = []
        groups = Repository.query.filter(
            Repository.type == "group"
        ).all()

        for group in groups:
            members = group.group_members
            if name in members:
                dependents.append(group.name)

        return dependents
