"""
Group Repository Type Handler.

This module implements the **Group** repository type for Feature F-102
(Repository Types — Group).  Group repositories aggregate content from
multiple ordered member repositories (Hosted, Proxy, and even other Groups),
resolving requests by querying members in a defined order.

**Resolution Strategy:**

Group repositories implement a **first-match-wins** strategy:

1. Members are queried sequentially in the configured order.
2. The first member that contains the requested artifact / component wins.
3. Nested groups are resolved recursively with circular reference detection.
4. Offline members are silently skipped (warning logged).
5. Listing operations (``list_components``, ``list_assets``, ``browse``)
   aggregate across all members with deduplication by member priority.

**Architecture Context:**

Replaces the Java Group facet resolution logic from Section 5.2.4 of the
Technical Specification.  Works in tandem with
``src.app.services.group_service`` which provides additional service-level
group operations.

**Circular Reference Detection:**

Groups can contain other groups, forming a directed graph.  To prevent
infinite recursion, all recursive operations track visited group names in
a ``set`` and raise :class:`CircularGroupReferenceError` if a cycle is
detected.  Additionally, a hard depth limit (:data:`MAX_GROUP_DEPTH`)
provides a safety net.

**Event Integration:**

Member management operations (``update_members``, ``add_member``,
``remove_member``) emit :attr:`EventType.REPOSITORY_UPDATED` events for
audit logging (Feature F-303) and webhook dispatch (Feature F-503).

**Exports:**

- :class:`GroupRepository` — main group handler class
- :class:`GroupRepositoryError` — base exception
- :class:`CircularGroupReferenceError` — cycle detection exception
- :class:`MemberFormatMismatchError` — format validation exception
- :class:`MemberNotFoundError` — member lookup exception
- :data:`MAX_GROUP_DEPTH` — recursion depth limit
- :data:`DEFAULT_GROUP_ATTRIBUTES` — default group config template
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, BinaryIO

from flask import current_app

from sqlalchemy.orm.attributes import flag_modified

from src.app.extensions import db
from src.app.models.repository import Repository
from src.app.models.component import Component
from src.app.models.asset import Asset
from src.app.repositories.facets import RepositoryFacet
from src.app.repositories.lifecycle import RepositoryLifecycle, RepositoryState
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source.  Provides diagnostic output for group resolution tracing, circular
# reference detection, offline member warnings, and member management ops.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_GROUP_DEPTH: int = 10
"""Maximum depth for nested group resolution.

Prevents infinite recursion when groups contain nested groups.  If the
resolution depth exceeds this limit, recursion is halted and a warning
is logged.  This is a safety net in addition to the cycle detection
performed via the ``visited`` set in :meth:`GroupRepository.get_members_recursive`.
"""

DEFAULT_GROUP_ATTRIBUTES: dict[str, Any] = {
    "group": {
        "memberNames": [],  # Ordered list of member repository names
    },
}
"""Default attributes template for newly-created group repositories.

The ``group.memberNames`` list defines the ordered set of member
repository names.  Members are resolved in list order during artifact
lookups (first-match-wins) and content aggregation operations.
"""


# ═══════════════════════════════════════════════════════════════════════════
# Custom Exception Hierarchy
# ═══════════════════════════════════════════════════════════════════════════


class GroupRepositoryError(Exception):
    """Base exception for all group repository operations.

    Provides a structured error with an optional ``repository_name``
    attribute for identifying the group repository that triggered the
    error.

    Attributes:
        message: Human-readable error description.
        repository_name: Name of the group repository (may be ``None``).
    """

    def __init__(
        self,
        message: str = "Group repository error occurred",
        repository_name: str | None = None,
    ) -> None:
        self.message: str = message
        self.repository_name: str | None = repository_name
        super().__init__(self.message)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"repository_name={self.repository_name!r})"
        )


class CircularGroupReferenceError(GroupRepositoryError):
    """Raised when a circular reference is detected in group membership.

    Circular references occur when a chain of group memberships leads
    back to the originating group (e.g., group-A → group-B → group-A).
    The error message includes the detected circular chain for debugging.

    Attributes:
        message: Description including the circular chain path.
        repository_name: Name of the group where the cycle was detected.
    """

    def __init__(
        self,
        message: str = "Circular group reference detected",
        repository_name: str | None = None,
    ) -> None:
        super().__init__(message=message, repository_name=repository_name)


class MemberFormatMismatchError(GroupRepositoryError):
    """Raised when a prospective member has a different format than the group.

    All members of a group repository must share the same format
    (e.g., all Maven, all npm).  Attempting to add a member with a
    mismatched format raises this exception.

    Attributes:
        message: Description including the expected and actual formats.
        repository_name: Name of the group repository.
    """

    def __init__(
        self,
        message: str = "Member format does not match group format",
        repository_name: str | None = None,
    ) -> None:
        super().__init__(message=message, repository_name=repository_name)


class MemberNotFoundError(GroupRepositoryError):
    """Raised when a referenced member repository does not exist.

    Attributes:
        message: Description including the missing member name.
        repository_name: Name of the group repository.
    """

    def __init__(
        self,
        message: str = "Member repository not found",
        repository_name: str | None = None,
    ) -> None:
        super().__init__(message=message, repository_name=repository_name)


# ═══════════════════════════════════════════════════════════════════════════
# GroupRepository — Main Class
# ═══════════════════════════════════════════════════════════════════════════


class GroupRepository:
    """Group repository type handler.

    Aggregates content from ordered member repositories with a
    first-match-wins resolution strategy.  Supports nested groups with
    circular reference detection.

    A group repository does **not** store content directly — it delegates
    all read operations to its member repositories in the configured
    order.  Write operations are not supported on group repositories
    (artifacts must be uploaded to the individual hosted members).

    Args:
        repository: The :class:`Repository` model instance that this
            handler wraps.  Must have ``type == 'group'``.

    Raises:
        TypeError: If the repository type is not ``'group'``.

    Attributes:
        repository: The bound :class:`Repository` model.
        name: Shorthand for ``repository.name``.
    """

    def __init__(self, repository: Repository) -> None:
        if repository.type != "group":
            raise TypeError(
                f"GroupRepository requires a repository with type='group', "
                f"but got type='{repository.type}' for repository "
                f"'{repository.name}'."
            )

        self.repository: Repository = repository
        self.name: str = repository.name
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.{repository.name}"
        )
        self._lifecycle: RepositoryLifecycle = RepositoryLifecycle(repository)

        self.logger.debug(
            "GroupRepository initialised for '%s' (format=%s, online=%s).",
            self.name,
            repository.format,
            repository.online,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # Member Resolution
    # ═══════════════════════════════════════════════════════════════════════

    def get_members(self) -> list[Repository]:
        """Load member repositories in the configured order.

        Reads the ``group.memberNames`` list from the repository's
        ``attributes`` JSON column, loads each member by name, and
        returns them in order.  Members that are offline or not found
        in the database are silently skipped with a warning log entry.

        Returns:
            Ordered list of :class:`Repository` objects representing
            the group's active, online members.
        """
        member_names: list[str] = (
            self.repository.attributes or {}
        ).get("group", {}).get("memberNames", [])

        if not member_names:
            self.logger.debug(
                "Group '%s' has no configured members.", self.name
            )
            return []

        members: list[Repository] = []
        for member_name in member_names:
            member: Repository | None = Repository.query.filter_by(
                name=member_name
            ).first()

            if member is None:
                self.logger.warning(
                    "Group '%s': member '%s' not found in database — skipping.",
                    self.name,
                    member_name,
                )
                continue

            if not member.online:
                self.logger.warning(
                    "Group '%s': member '%s' is offline — skipping.",
                    self.name,
                    member_name,
                )
                continue

            members.append(member)

        self.logger.debug(
            "Group '%s': resolved %d of %d configured members.",
            self.name,
            len(members),
            len(member_names),
        )
        return members

    def get_members_recursive(
        self,
        visited: set[str] | None = None,
        depth: int = 0,
    ) -> list[Repository]:
        """Recursively resolve all non-group member repositories.

        For nested group hierarchies, this method flattens the member
        tree into a single ordered list of concrete (hosted / proxy)
        repositories.  Group members are expanded recursively; hosted
        and proxy members are included directly.

        **Circular Reference Detection:**

        A ``visited`` set tracks all group repository names encountered
        during recursion.  If the current group's name is already in the
        set, a :class:`CircularGroupReferenceError` is raised.

        **Depth Limiting:**

        If ``depth`` exceeds :data:`MAX_GROUP_DEPTH`, recursion stops
        and a warning is logged.  This is a safety net complementing
        the cycle detection.

        Args:
            visited: Set of group names already visited in the current
                recursion chain.  ``None`` on the initial call.
            depth: Current recursion depth (0-based).

        Returns:
            Flattened, ordered list of non-group :class:`Repository`
            objects.

        Raises:
            CircularGroupReferenceError: If a circular reference is
                detected in the group membership chain.
        """
        if visited is None:
            visited = set()

        # Circular reference detection
        if self.name in visited:
            chain_str = " -> ".join(visited) + f" -> {self.name}"
            raise CircularGroupReferenceError(
                message=(
                    f"Circular group reference detected: {chain_str}"
                ),
                repository_name=self.name,
            )

        visited.add(self.name)

        # Depth safety limit
        if depth > MAX_GROUP_DEPTH:
            self.logger.warning(
                "Group '%s': maximum nesting depth (%d) exceeded at "
                "depth %d — stopping recursion.",
                self.name,
                MAX_GROUP_DEPTH,
                depth,
            )
            return []

        result: list[Repository] = []
        for member in self.get_members():
            if member.type == "group":
                # Recursively resolve nested group
                nested_group = GroupRepository(member)
                nested_members = nested_group.get_members_recursive(
                    visited=set(visited),  # Copy to isolate branches
                    depth=depth + 1,
                )
                result.extend(nested_members)
            else:
                # Hosted or Proxy — include directly
                result.append(member)

        self.logger.debug(
            "Group '%s': recursive resolution yielded %d concrete members "
            "(depth=%d).",
            self.name,
            len(result),
            depth,
        )
        return result

    # ═══════════════════════════════════════════════════════════════════════
    # Content Resolution — Artifact Lookup (Feature F-102)
    # ═══════════════════════════════════════════════════════════════════════

    def get_artifact(
        self, path: str
    ) -> tuple[BinaryIO | None, str | None, int | None, Repository | None]:
        """Resolve an artifact by path using first-match-wins strategy.

        Iterates through member repositories in configured order.  For
        each member:

        - **Hosted members**: Query the ``Asset`` table directly for a
          matching path.
        - **Proxy members**: Query the ``Asset`` table for cached content
          (actual remote fetch is delegated to the proxy service layer).
        - **Group members**: Recursively delegate to the nested group's
          ``get_artifact``.

        The first member that contains an asset at the given path wins.

        Args:
            path: The artifact path to resolve (e.g.,
                ``'/org/example/lib/1.0/lib-1.0.jar'``).

        Returns:
            A 4-tuple of ``(content_stream, content_type, size,
            source_repository)``:

            - ``content_stream``: ``BinaryIO`` stream or ``None``
            - ``content_type``: MIME type string or ``None``
            - ``size``: File size in bytes or ``None``
            - ``source_repository``: The member that served the content

            Returns ``(None, None, None, None)`` if no member has the
            artifact.
        """
        # Guard: repository must be online and started
        if not self.repository.online:
            self.logger.warning(
                "Group '%s' is offline — cannot serve artifact '%s'.",
                self.name,
                path,
            )
            return (None, None, None, None)

        if not self._lifecycle.is_started:
            self.logger.warning(
                "Group '%s' is not in STARTED state (current: %s) — "
                "cannot serve artifact '%s'.",
                self.name,
                self._lifecycle.current_state.value,
                path,
            )
            return (None, None, None, None)

        members = self.get_members()
        for member in members:
            if member.type == "group":
                # Recursively resolve from nested group
                try:
                    nested_group = GroupRepository(member)
                    content_stream, content_type, size, source_repo = (
                        nested_group.get_artifact(path)
                    )
                    if source_repo is not None:
                        self.logger.debug(
                            "Group '%s': artifact '%s' resolved from "
                            "nested group '%s' (source: '%s').",
                            self.name,
                            path,
                            member.name,
                            source_repo.name,
                        )
                        return (content_stream, content_type, size, source_repo)
                except GroupRepositoryError as exc:
                    self.logger.warning(
                        "Group '%s': error resolving artifact '%s' from "
                        "nested group '%s': %s",
                        self.name,
                        path,
                        member.name,
                        str(exc),
                    )
                    continue
            else:
                # Hosted or Proxy — query Asset table directly
                asset: Asset | None = Asset.query.filter_by(
                    repository_name=member.name,
                    path=path,
                ).first()

                if asset is not None:
                    self.logger.debug(
                        "Group '%s': artifact '%s' found in member '%s'.",
                        self.name,
                        path,
                        member.name,
                    )
                    # Return asset metadata — actual BlobStore content
                    # retrieval is handled by the calling service layer
                    return (None, asset.content_type, asset.size, member)

        self.logger.debug(
            "Group '%s': artifact '%s' not found in any member.",
            self.name,
            path,
        )
        return (None, None, None, None)

    def resolve_component(
        self,
        namespace: str | None,
        name: str,
        version: str | None,
    ) -> tuple[Component | None, Repository | None]:
        """Resolve a component by coordinates using first-match-wins.

        Iterates through members in configured order, querying each
        member's components for a match on the provided coordinates.

        Args:
            namespace: Component namespace (e.g., Maven groupId).
                ``None`` for formats without namespaces.
            name: Component name (e.g., Maven artifactId).
            version: Component version string.  ``None`` for
                version-agnostic lookups.

        Returns:
            A 2-tuple of ``(component, source_repository)`` or
            ``(None, None)`` if no member contains the component.
        """
        # Guard: repository must be online and started
        if not self.repository.online:
            self.logger.warning(
                "Group '%s' is offline — cannot resolve component "
                "'%s:%s:%s'.",
                self.name,
                namespace,
                name,
                version,
            )
            return (None, None)

        if not self._lifecycle.is_started:
            self.logger.warning(
                "Group '%s' is not in STARTED state — cannot resolve "
                "component '%s:%s:%s'.",
                self.name,
                namespace,
                name,
                version,
            )
            return (None, None)

        members = self.get_members()
        for member in members:
            if member.type == "group":
                # Recursively resolve from nested group
                try:
                    nested_group = GroupRepository(member)
                    component, source_repo = nested_group.resolve_component(
                        namespace, name, version,
                    )
                    if component is not None:
                        return (component, source_repo)
                except GroupRepositoryError as exc:
                    self.logger.warning(
                        "Group '%s': error resolving component from "
                        "nested group '%s': %s",
                        self.name,
                        member.name,
                        str(exc),
                    )
                    continue
            else:
                # Build query for this member's components
                query = Component.query.filter_by(
                    repository_name=member.name,
                    name=name,
                )
                if namespace is not None:
                    query = query.filter_by(namespace=namespace)
                if version is not None:
                    query = query.filter_by(version=version)

                component: Component | None = query.first()
                if component is not None:
                    self.logger.debug(
                        "Group '%s': component '%s:%s:%s' found in "
                        "member '%s'.",
                        self.name,
                        namespace,
                        name,
                        version,
                        member.name,
                    )
                    return (component, member)

        self.logger.debug(
            "Group '%s': component '%s:%s:%s' not found in any member.",
            self.name,
            namespace,
            name,
            version,
        )
        return (None, None)

    # ═══════════════════════════════════════════════════════════════════════
    # Content Aggregation — Listing Operations
    # ═══════════════════════════════════════════════════════════════════════

    def list_components(
        self,
        namespace: str | None = None,
        name: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """Aggregate and deduplicate components across all members.

        Queries components from each member repository in order and
        deduplicates by ``(namespace, name, version)`` — the first
        occurrence (from the highest-priority member) wins.

        Args:
            namespace: Optional namespace filter.
            name: Optional component name filter.
            page: 1-based page number for pagination.
            page_size: Number of items per page.

        Returns:
            A dict with keys:

            - ``'items'``: List of :class:`Component` objects for the
              requested page.
            - ``'total'``: Total count of deduplicated components.
            - ``'page'``: Current page number.
            - ``'page_size'``: Requested page size.
            - ``'pages'``: Total number of pages.
        """
        all_components: list[tuple[Component, str]] = []

        for member in self.get_members():
            if member.type == "group":
                # For nested groups, recursively list and tag with
                # the nested group's name for dedup tracking
                try:
                    nested_group = GroupRepository(member)
                    nested_result = nested_group.list_components(
                        namespace=namespace,
                        name=name,
                        page=1,
                        page_size=999999,  # Get all for deduplication
                    )
                    for comp in nested_result.get("items", []):
                        all_components.append((comp, member.name))
                except GroupRepositoryError as exc:
                    self.logger.warning(
                        "Group '%s': error listing components from "
                        "nested group '%s': %s",
                        self.name,
                        member.name,
                        str(exc),
                    )
                    continue
            else:
                query = Component.query.filter_by(
                    repository_name=member.name,
                )
                if namespace is not None:
                    query = query.filter_by(namespace=namespace)
                if name is not None:
                    query = query.filter_by(name=name)

                member_components = query.all()
                for comp in member_components:
                    all_components.append((comp, member.name))

        # Deduplicate by (namespace, name, version)
        deduplicated = self._deduplicate_components(all_components)

        # Paginate
        total = len(deduplicated)
        pages = max(1, (total + page_size - 1) // page_size)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_items = deduplicated[start_idx:end_idx]

        return {
            "items": page_items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    def list_assets(
        self,
        path_prefix: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        """Aggregate and deduplicate assets across all members.

        Queries assets from each member repository in order and
        deduplicates by ``path`` — the first occurrence (from the
        highest-priority member) wins.

        Args:
            path_prefix: Optional path prefix filter (e.g.,
                ``'/org/apache'``).
            page: 1-based page number for pagination.
            page_size: Number of items per page.

        Returns:
            A dict with keys:

            - ``'items'``: List of :class:`Asset` objects for the
              requested page.
            - ``'total'``: Total count of deduplicated assets.
            - ``'page'``: Current page number.
            - ``'page_size'``: Requested page size.
            - ``'pages'``: Total number of pages.
        """
        all_assets: list[tuple[Asset, str]] = []

        for member in self.get_members():
            if member.type == "group":
                try:
                    nested_group = GroupRepository(member)
                    nested_result = nested_group.list_assets(
                        path_prefix=path_prefix,
                        page=1,
                        page_size=999999,
                    )
                    for asset in nested_result.get("items", []):
                        all_assets.append((asset, member.name))
                except GroupRepositoryError as exc:
                    self.logger.warning(
                        "Group '%s': error listing assets from "
                        "nested group '%s': %s",
                        self.name,
                        member.name,
                        str(exc),
                    )
                    continue
            else:
                query = Asset.query.filter_by(
                    repository_name=member.name,
                )
                if path_prefix is not None:
                    query = query.filter(
                        Asset.path.startswith(path_prefix)
                    )

                member_assets = query.all()
                for asset in member_assets:
                    all_assets.append((asset, member.name))

        # Deduplicate by path
        deduplicated = self._deduplicate_assets(all_assets)

        # Paginate
        total = len(deduplicated)
        pages = max(1, (total + page_size - 1) // page_size)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_items = deduplicated[start_idx:end_idx]

        return {
            "items": page_items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    def _deduplicate_components(
        self, all_components: list[tuple[Component, str]]
    ) -> list[Component]:
        """Deduplicate components by (namespace, name, version).

        First occurrence by member priority order wins.  This ensures
        that when the same component exists in multiple members, only
        the one from the highest-priority member is returned.

        Args:
            all_components: List of ``(component, source_repo_name)``
                tuples in member priority order.

        Returns:
            Deduplicated list of :class:`Component` objects preserving
            member priority order.
        """
        seen: set[tuple[str | None, str, str | None]] = set()
        result: list[Component] = []

        for component, _source_repo in all_components:
            key = (component.namespace, component.name, component.version)
            if key not in seen:
                seen.add(key)
                result.append(component)

        return result

    def _deduplicate_assets(
        self, all_assets: list[tuple[Asset, str]]
    ) -> list[Asset]:
        """Deduplicate assets by path.

        First occurrence by member priority order wins.

        Args:
            all_assets: List of ``(asset, source_repo_name)`` tuples
                in member priority order.

        Returns:
            Deduplicated list of :class:`Asset` objects preserving
            member priority order.
        """
        seen: set[str] = set()
        result: list[Asset] = []

        for asset, _source_repo in all_assets:
            if asset.path not in seen:
                seen.add(asset.path)
                result.append(asset)

        return result

    # ═══════════════════════════════════════════════════════════════════════
    # Browse Operations (Feature F-104)
    # ═══════════════════════════════════════════════════════════════════════

    def browse(self, path: str = "/") -> dict:
        """Build a merged virtual directory tree from all members.

        Aggregates asset paths from all member repositories, building a
        virtual tree structure.  When the same path exists in multiple
        members, the first member's asset takes precedence (member
        priority deduplication).

        The browse structure mirrors that returned by
        ``HostedRepository.browse()``.

        Args:
            path: Directory path to browse.  Defaults to root ``'/'``.

        Returns:
            A dict with keys:

            - ``'path'``: The browsed directory path.
            - ``'children'``: List of child entries, each a dict with:
              - ``'name'``: Entry name (file or directory name).
              - ``'path'``: Full path to the entry.
              - ``'type'``: ``'directory'`` or ``'file'``.
              - ``'content_type'``: MIME type (files only).
              - ``'size'``: Size in bytes (files only).
              - ``'repository'``: Source member repository name.
        """
        # Normalise path
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/") or "/"

        # Collect all assets from all members (with deduplication)
        all_assets: list[tuple[Asset, str]] = []
        for member in self.get_members():
            if member.type == "group":
                try:
                    nested_group = GroupRepository(member)
                    nested_result = nested_group.list_assets(
                        path_prefix=None, page=1, page_size=999999,
                    )
                    for asset in nested_result.get("items", []):
                        all_assets.append((asset, member.name))
                except GroupRepositoryError:
                    continue
            else:
                member_assets = Asset.query.filter_by(
                    repository_name=member.name,
                ).all()
                for asset in member_assets:
                    all_assets.append((asset, member.name))

        # Deduplicate by path
        deduped_assets = self._deduplicate_assets(all_assets)

        # Build mapping of asset path → (asset, source_repo)
        asset_map: dict[str, tuple[Asset, str]] = {}
        for asset, source in all_assets:
            if asset.path not in asset_map:
                asset_map[asset.path] = (asset, source)

        # Build directory tree for the requested path
        children: dict[str, dict[str, Any]] = {}

        for asset in deduped_assets:
            asset_path = asset.path
            if not asset_path.startswith("/"):
                asset_path = "/" + asset_path

            # Check if this asset is under the browse path
            if path == "/":
                relative = asset_path.lstrip("/")
            elif asset_path.startswith(path + "/"):
                relative = asset_path[len(path) + 1:]
            elif asset_path == path:
                relative = asset_path.rsplit("/", 1)[-1]
            else:
                continue

            if not relative:
                continue

            # Extract the immediate child (first path segment)
            parts = relative.split("/", 1)
            child_name = parts[0]

            if not child_name:
                continue

            if len(parts) > 1:
                # This asset is nested deeper — represent as directory
                if child_name not in children:
                    child_path = (
                        f"{path}/{child_name}" if path != "/" else
                        f"/{child_name}"
                    )
                    source_repo = asset_map.get(
                        asset.path, (asset, "")
                    )[1]
                    children[child_name] = {
                        "name": child_name,
                        "path": child_path,
                        "type": "directory",
                        "repository": source_repo,
                    }
            else:
                # This asset is a direct child — represent as file
                child_path = (
                    f"{path}/{child_name}" if path != "/" else
                    f"/{child_name}"
                )
                source_repo = asset_map.get(
                    asset.path, (asset, "")
                )[1]
                children[child_name] = {
                    "name": child_name,
                    "path": child_path,
                    "type": "file",
                    "content_type": asset.content_type,
                    "size": asset.size,
                    "repository": source_repo,
                }

        return {
            "path": path,
            "children": sorted(
                children.values(), key=lambda c: (c["type"], c["name"])
            ),
        }

    # ═══════════════════════════════════════════════════════════════════════
    # Member Management
    # ═══════════════════════════════════════════════════════════════════════

    def update_members(self, member_names: list[str]) -> None:
        """Replace the entire ordered member list.

        Validates all prospective members and updates the group's
        ``attributes['group']['memberNames']`` configuration.  Emits a
        :attr:`EventType.REPOSITORY_UPDATED` event on success.

        Validation rules:
        - All member names must reference existing repositories.
        - No self-reference (the group cannot contain itself).
        - All members must share the same format as this group.
        - No circular references through nested group chains.

        Args:
            member_names: Ordered list of member repository names.

        Raises:
            MemberNotFoundError: If any member does not exist.
            MemberFormatMismatchError: If any member has a different
                format.
            CircularGroupReferenceError: If adding any member would
                create a circular reference.
            GroupRepositoryError: If the group contains itself.
        """
        # Validate self-reference
        if self.name in member_names:
            raise GroupRepositoryError(
                message=(
                    f"Group '{self.name}' cannot include itself as a member."
                ),
                repository_name=self.name,
            )

        # Validate each member
        for member_name in member_names:
            validated = self._validate_member(member_name)
            # Circular reference check for group-type members
            if validated.type == "group":
                self._check_circular_reference(member_name)

        # Persist the updated member list
        attrs: dict = dict(self.repository.attributes or {})
        group_attrs: dict = dict(attrs.get("group", {}))
        old_members = list(group_attrs.get("memberNames", []))
        group_attrs["memberNames"] = list(member_names)
        group_attrs["lastModified"] = datetime.now(timezone.utc).isoformat()
        attrs["group"] = group_attrs
        self.repository.attributes = attrs
        # Explicitly mark JSON column as modified for SQLAlchemy
        # mutation tracking (required for mutable JSON column types).
        flag_modified(self.repository, "attributes")
        db.session.add(self.repository)
        db.session.commit()

        # Emit event for audit logging and webhook dispatch
        emit_event(
            EventType.REPOSITORY_UPDATED,
            payload={
                "repository_name": self.name,
                "event_subtype": "group_members_updated",
                "format": self.repository.format,
                "type": "group",
                "old_members": old_members,
                "new_members": list(member_names),
            },
        )

        self.logger.info(
            "Group '%s': members updated from %s to %s.",
            self.name,
            old_members,
            list(member_names),
        )

    def add_member(
        self, member_name: str, position: int | None = None
    ) -> None:
        """Add a single member to the group.

        Validates the prospective member and inserts it at the specified
        position (or appends to the end if no position given).  Emits a
        :attr:`EventType.REPOSITORY_UPDATED` event on success.

        Args:
            member_name: Name of the repository to add as a member.
            position: Optional 0-based insertion index.  ``None``
                appends to the end of the member list.

        Raises:
            MemberNotFoundError: If the member repository does not exist.
            MemberFormatMismatchError: If the member has a different format.
            CircularGroupReferenceError: If adding would create a cycle.
            GroupRepositoryError: If the member is already in the group
                or is the group itself.
        """
        # Self-reference check
        if member_name == self.name:
            raise GroupRepositoryError(
                message=(
                    f"Group '{self.name}' cannot include itself as a member."
                ),
                repository_name=self.name,
            )

        # Validate the member
        validated = self._validate_member(member_name)

        # Check for duplicates
        current_members: list[str] = (
            self.repository.attributes or {}
        ).get("group", {}).get("memberNames", [])

        if member_name in current_members:
            raise GroupRepositoryError(
                message=(
                    f"Member '{member_name}' is already in group "
                    f"'{self.name}'."
                ),
                repository_name=self.name,
            )

        # Circular reference check for group-type members
        if validated.type == "group":
            self._check_circular_reference(member_name)

        # Insert at position or append
        new_members = list(current_members)
        if position is not None and 0 <= position <= len(new_members):
            new_members.insert(position, member_name)
        else:
            new_members.append(member_name)

        # Persist
        attrs: dict = dict(self.repository.attributes or {})
        group_attrs: dict = dict(attrs.get("group", {}))
        group_attrs["memberNames"] = new_members
        group_attrs["lastModified"] = datetime.now(timezone.utc).isoformat()
        attrs["group"] = group_attrs
        self.repository.attributes = attrs
        flag_modified(self.repository, "attributes")
        db.session.add(self.repository)
        db.session.commit()

        # Emit event
        emit_event(
            EventType.REPOSITORY_UPDATED,
            payload={
                "repository_name": self.name,
                "event_subtype": "group_member_added",
                "format": self.repository.format,
                "type": "group",
                "member_added": member_name,
                "position": position,
                "members": new_members,
            },
        )

        self.logger.info(
            "Group '%s': member '%s' added at position %s.",
            self.name,
            member_name,
            position if position is not None else "end",
        )

    def remove_member(self, member_name: str) -> None:
        """Remove a member from the group.

        If the member is not currently in the group, this method is a
        no-op (idempotent removal).  Emits a
        :attr:`EventType.REPOSITORY_UPDATED` event on success.

        Args:
            member_name: Name of the repository to remove.
        """
        current_members: list[str] = (
            self.repository.attributes or {}
        ).get("group", {}).get("memberNames", [])

        if member_name not in current_members:
            self.logger.debug(
                "Group '%s': member '%s' not in member list — nothing "
                "to remove.",
                self.name,
                member_name,
            )
            return

        # Remove and persist
        new_members = [m for m in current_members if m != member_name]
        attrs: dict = dict(self.repository.attributes or {})
        group_attrs: dict = dict(attrs.get("group", {}))
        group_attrs["memberNames"] = new_members
        group_attrs["lastModified"] = datetime.now(timezone.utc).isoformat()
        attrs["group"] = group_attrs
        self.repository.attributes = attrs
        flag_modified(self.repository, "attributes")
        db.session.add(self.repository)
        db.session.commit()

        # Emit event
        emit_event(
            EventType.REPOSITORY_UPDATED,
            payload={
                "repository_name": self.name,
                "event_subtype": "group_member_removed",
                "format": self.repository.format,
                "type": "group",
                "member_removed": member_name,
                "members": new_members,
            },
        )

        self.logger.info(
            "Group '%s': member '%s' removed. Remaining: %s.",
            self.name,
            member_name,
            new_members,
        )

    def _validate_member(self, member_name: str) -> Repository:
        """Validate a prospective member repository.

        Checks that the member:
        1. Exists in the database.
        2. Has the same format as this group repository.
        3. Is not this group repository itself.

        Args:
            member_name: Name of the repository to validate.

        Returns:
            The validated :class:`Repository` object.

        Raises:
            MemberNotFoundError: If the repository does not exist.
            MemberFormatMismatchError: If the format does not match.
            GroupRepositoryError: If the member is this group itself.
        """
        if member_name == self.name:
            raise GroupRepositoryError(
                message=(
                    f"Group '{self.name}' cannot include itself as a member."
                ),
                repository_name=self.name,
            )

        member: Repository | None = Repository.query.filter_by(
            name=member_name
        ).first()

        if member is None:
            raise MemberNotFoundError(
                message=(
                    f"Member repository '{member_name}' not found."
                ),
                repository_name=self.name,
            )

        if member.format != self.repository.format:
            raise MemberFormatMismatchError(
                message=(
                    f"Member '{member_name}' has format '{member.format}' "
                    f"but group '{self.name}' requires format "
                    f"'{self.repository.format}'."
                ),
                repository_name=self.name,
            )

        return member

    def _check_circular_reference(
        self, member_name: str, visited: set[str] | None = None
    ) -> None:
        """Check if adding a member would create a circular reference.

        If the prospective member is a group repository, recursively
        examines its membership chain to ensure no path leads back to
        this group.

        Args:
            member_name: Name of the prospective member to check.
            visited: Set of group names already visited in the current
                check chain.  ``None`` on the initial call.

        Raises:
            CircularGroupReferenceError: If a circular reference would
                be created.
        """
        if visited is None:
            visited = {self.name}

        if member_name in visited:
            chain_str = " -> ".join(visited) + f" -> {member_name}"
            raise CircularGroupReferenceError(
                message=(
                    f"Adding member '{member_name}' to group "
                    f"'{self.name}' would create a circular reference: "
                    f"{chain_str}"
                ),
                repository_name=self.name,
            )

        # Look up the member; if it's a group, check its members recursively
        member: Repository | None = Repository.query.filter_by(
            name=member_name
        ).first()

        if member is None or member.type != "group":
            return

        # The member is a group — check its children
        visited.add(member_name)
        child_names: list[str] = (
            member.attributes or {}
        ).get("group", {}).get("memberNames", [])

        for child_name in child_names:
            self._check_circular_reference(child_name, visited=set(visited))

    # ═══════════════════════════════════════════════════════════════════════
    # Repository Status
    # ═══════════════════════════════════════════════════════════════════════

    def get_status(self) -> dict:
        """Return a comprehensive status snapshot of this group repository.

        Includes member information, total component/asset counts across
        all members, and lifecycle state.

        Returns:
            A dict with keys:

            - ``'name'``: Group repository name.
            - ``'type'``: ``'group'``.
            - ``'format'``: Repository format.
            - ``'online'``: Whether the group is online.
            - ``'state'``: Current lifecycle state value.
            - ``'member_count'``: Number of configured members.
            - ``'members'``: List of member status dicts.
            - ``'total_component_count'``: Aggregated component count.
            - ``'total_asset_count'``: Aggregated asset count.
        """
        member_names: list[str] = (
            self.repository.attributes or {}
        ).get("group", {}).get("memberNames", [])

        resolved_members = self.get_members()

        # Count components and assets across all members
        total_components: int = 0
        total_assets: int = 0
        for member in resolved_members:
            try:
                total_components += Component.query.filter_by(
                    repository_name=member.name
                ).count()
                total_assets += Asset.query.filter_by(
                    repository_name=member.name
                ).count()
            except Exception as exc:
                self.logger.warning(
                    "Group '%s': error counting content for member '%s': %s",
                    self.name,
                    member.name,
                    str(exc),
                )

        member_statuses: list[dict[str, Any]] = [
            {
                "name": m.name,
                "type": m.type,
                "online": m.online,
            }
            for m in resolved_members
        ]

        return {
            "name": self.name,
            "type": "group",
            "format": self.repository.format,
            "online": self.repository.online,
            "state": self._lifecycle.current_state.value,
            "member_count": len(member_names),
            "members": member_statuses,
            "total_component_count": total_components,
            "total_asset_count": total_assets,
        }


# ===========================================================================
# Module-Level Export List
# ===========================================================================

__all__: list[str] = [
    "GroupRepository",
    "GroupRepositoryError",
    "CircularGroupReferenceError",
    "MemberFormatMismatchError",
    "MemberNotFoundError",
    "MAX_GROUP_DEPTH",
    "DEFAULT_GROUP_ATTRIBUTES",
]
