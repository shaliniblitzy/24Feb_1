"""
Group Repository Member Resolution Service.

This module implements the ``GroupService`` class which provides ordered member
resolution and merged responses for **group-type repositories**.  Group
repositories aggregate content from multiple member repositories (both hosted
and proxy types), resolving requests by querying members in a defined order.

**Feature Support:**

- **F-102** (Repository Types — Hosted, Proxy, Group):  This service implements
  the Group repository resolution strategy — members are queried in defined
  order with "first match wins" semantics.
- **F-104** (Browse Tree Navigation):  ``resolve_assets()`` supports
  ``path_prefix`` filtering for virtual directory tree browsing.

**Design Pattern:**

Implements the **Facade Pattern** (AAP Section 0.4.3) for group repository
content resolution — providing a unified interface for all group repository
operations including member resolution, component/asset lookup, content
streaming, metadata aggregation, and member management.

**Architecture Context:**

Replaces the Java Group facet resolution logic from the original Sonatype
Nexus Repository system.  In the Java architecture, group resolution was
implemented via OSGi bundle facets with ``GroupFacetImpl``; here, the same
behaviour is encapsulated in a single Python service class operating on
SQLAlchemy models via the Flask-SQLAlchemy ``db`` session.

**Key Behavioural Rules:**

- Members are queried in the order defined in ``attributes['group']['memberNames']``
- First match wins — the first member that contains a matching component/asset
  is returned; subsequent matches are ignored
- Nested groups are supported — a group member can itself be a group, resolved
  recursively with circular reference detection
- Offline members are silently skipped (warning logged, no error raised)
- All members must share the same repository format as the group
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from flask import current_app

from src.app.extensions import db
from src.app.models.asset import Asset
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.models.component import Component
from src.app.models.repository import Repository
from src.app.storage import create_blobstore_from_model
from src.app.storage.blobstore import BlobId

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 per-service structured logging
# from the Java source system.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = ["GroupService"]


# ===========================================================================
# GroupService Class
# ===========================================================================


class GroupService:
    """Service for group repository member resolution and content aggregation.

    A group repository presents a *virtual* unified view over an ordered list
    of member repositories.  Content requests are resolved by walking the
    member list in order and returning the first match.  Listing requests
    aggregate results from all members with first-occurrence deduplication.

    Thread Safety:
        Instances of this class are **not** thread-safe.  In a typical Flask
        deployment each request creates its own service instance or accesses
        a request-scoped singleton.

    Example::

        svc = GroupService()
        members = svc.get_member_repositories("maven-public")
        component = svc.resolve_component("maven-public", "org.example", "lib", "1.0")
    """

    def __init__(self) -> None:
        """Initialise the GroupService with a dedicated logger."""
        self.logger: logging.Logger = logging.getLogger(__name__)
        self.logger.debug("GroupService instance created.")

    # -----------------------------------------------------------------------
    # Helper — Load and validate a group repository
    # -----------------------------------------------------------------------

    def _load_group_repository(self, group_name: str) -> Repository:
        """Load a repository by name and verify that it is a group type.

        Args:
            group_name: The unique repository name.

        Returns:
            The ``Repository`` model instance.

        Raises:
            ValueError: If the repository does not exist or is not a group.
        """
        repo: Repository | None = db.session.get(Repository, group_name)
        if repo is None:
            raise ValueError(
                f"Group repository '{group_name}' not found."
            )
        if not repo.is_group:
            raise ValueError(
                f"Repository '{group_name}' is of type '{repo.type}', "
                f"not 'group'."
            )
        return repo

    # ===================================================================
    # Phase 3 — Member Resolution
    # ===================================================================

    def get_member_repositories(
        self,
        group_name: str,
    ) -> list[Repository]:
        """Return the ordered list of resolved member repositories.

        For simple (non-nested) groups this returns the direct member list.
        For nested groups (a member is itself a group), the member's own
        members are recursively flattened into the result list.  Circular
        group references are detected and logged as warnings — they do
        **not** raise exceptions.

        Members that are offline or cannot be found are silently skipped
        (a warning is logged for each).

        Args:
            group_name: Name of the group repository.

        Returns:
            An ordered list of ``Repository`` objects representing the
            resolved (flattened) member list.

        Raises:
            ValueError: If *group_name* does not exist or is not a group.
        """
        self._load_group_repository(group_name)
        self.logger.info(
            "Resolving member repositories for group '%s'.",
            group_name,
        )
        resolved = self._resolve_members_recursive(group_name, visited=None)
        self.logger.info(
            "Group '%s' resolved to %d member repositories.",
            group_name,
            len(resolved),
        )
        return resolved

    def _resolve_members_recursive(
        self,
        group_name: str,
        visited: set[str] | None = None,
    ) -> list[Repository]:
        """Recursively resolve group members with circular reference detection.

        This internal helper walks the member list for *group_name*.  If a
        member is itself a group, it recurses into that group.  The
        ``visited`` set tracks group names already being resolved so that
        circular chains (e.g., A → B → A) are detected and short-circuited
        without raising an exception.

        Args:
            group_name: Name of the group to resolve.
            visited:    Set of group names already in the current resolution
                        chain.  ``None`` on first call (auto-initialised).

        Returns:
            Flattened ordered list of non-group ``Repository`` objects.
        """
        if visited is None:
            visited = set()

        # Circular reference detection
        if group_name in visited:
            self.logger.warning(
                "Circular group reference detected: '%s' is already in the "
                "resolution chain %s.  Skipping to prevent infinite loop.",
                group_name,
                visited,
            )
            return []

        visited.add(group_name)

        repo: Repository | None = db.session.get(Repository, group_name)
        if repo is None:
            self.logger.warning(
                "Group member '%s' not found in database.  Skipping.",
                group_name,
            )
            return []

        if not repo.is_group:
            # Non-group member — return it directly (leaf node)
            if not repo.online:
                self.logger.warning(
                    "Member repository '%s' is offline.  Skipping.",
                    group_name,
                )
                return []
            return [repo]

        # Extract ordered member names
        member_names: list[str] = repo.group_members
        if not member_names:
            self.logger.debug(
                "Group '%s' has no configured members.",
                group_name,
            )
            return []

        resolved: list[Repository] = []
        for member_name in member_names:
            member: Repository | None = db.session.get(Repository, member_name)
            if member is None:
                self.logger.warning(
                    "Member '%s' of group '%s' not found.  Skipping.",
                    member_name,
                    group_name,
                )
                continue

            if member.is_group:
                # Recursively resolve nested group
                nested = self._resolve_members_recursive(
                    member_name, visited=set(visited),
                )
                resolved.extend(nested)
            else:
                if not member.online:
                    self.logger.warning(
                        "Member '%s' of group '%s' is offline.  Skipping.",
                        member_name,
                        group_name,
                    )
                    continue
                resolved.append(member)

        return resolved

    # ===================================================================
    # Phase 4 — Content Resolution: Component Lookup
    # ===================================================================

    def resolve_component(
        self,
        group_name: str,
        namespace: str | None,
        name: str,
        version: str | None,
    ) -> Component | None:
        """Resolve a single component by coordinates across group members.

        Implements the **first-match-wins** strategy: members are queried
        in order and the first member that contains a component matching
        ``(repository_name, namespace, name, version)`` provides the result.

        Args:
            group_name: Name of the group repository.
            namespace:  Component namespace (Maven groupId, npm scope, …).
                        ``None`` for formats that do not use namespaces.
            name:       Component name.
            version:    Component version.  ``None`` matches any version
                        (though typically a specific version is requested).

        Returns:
            The matching ``Component`` or ``None`` if no member contains it.

        Raises:
            ValueError: If *group_name* does not exist or is not a group.
        """
        members = self.get_member_repositories(group_name)
        self.logger.debug(
            "Resolving component (namespace=%s, name=%s, version=%s) "
            "across %d members of group '%s'.",
            namespace,
            name,
            version,
            len(members),
            group_name,
        )

        for member in members:
            query = db.session.query(Component).filter(
                Component.repository_name == member.name,
                Component.name == name,
            )
            if namespace is not None:
                query = query.filter(Component.namespace == namespace)
            else:
                query = query.filter(Component.namespace.is_(None))

            if version is not None:
                query = query.filter(Component.version == version)
            else:
                query = query.filter(Component.version.is_(None))

            component: Component | None = query.first()
            if component is not None:
                self.logger.debug(
                    "Component '%s' resolved from member '%s' in group '%s'.",
                    component.coordinates,
                    member.name,
                    group_name,
                )
                return component

        self.logger.debug(
            "Component (namespace=%s, name=%s, version=%s) not found "
            "in any member of group '%s'.",
            namespace,
            name,
            version,
            group_name,
        )
        return None

    def resolve_components(
        self,
        group_name: str,
        namespace: str | None = None,
        name: str | None = None,
    ) -> list[Component]:
        """Aggregate components from all group members with deduplication.

        Components are collected from each member in order.  When multiple
        members contain a component with the same ``(namespace, name, version)``
        coordinates, only the first occurrence (from the highest-priority
        member) is included.

        Args:
            group_name: Name of the group repository.
            namespace:  Optional namespace filter.
            name:       Optional name filter.

        Returns:
            Deduplicated list of ``Component`` objects ordered by member
            priority, then by component name.

        Raises:
            ValueError: If *group_name* does not exist or is not a group.
        """
        members = self.get_member_repositories(group_name)
        self.logger.debug(
            "Aggregating components across %d members of group '%s' "
            "(namespace=%s, name=%s).",
            len(members),
            group_name,
            namespace,
            name,
        )

        seen_coordinates: set[str] = set()
        result: list[Component] = []

        for member in members:
            query = db.session.query(Component).filter(
                Component.repository_name == member.name,
            )
            if namespace is not None:
                query = query.filter(Component.namespace == namespace)
            if name is not None:
                query = query.filter(Component.name == name)

            query = query.order_by(Component.name, Component.version)
            components: list[Component] = query.all()

            for comp in components:
                coord_key = comp.coordinates
                if coord_key not in seen_coordinates:
                    seen_coordinates.add(coord_key)
                    result.append(comp)

        self.logger.debug(
            "Aggregated %d unique components from group '%s'.",
            len(result),
            group_name,
        )
        return result

    # ===================================================================
    # Phase 5 — Content Resolution: Asset Lookup
    # ===================================================================

    def resolve_asset(
        self,
        group_name: str,
        asset_path: str,
    ) -> tuple[Asset, Repository] | None:
        """Resolve a single asset by path across group members.

        Uses first-match-wins: the first member that contains an asset at
        *asset_path* provides the result.  The owning member ``Repository``
        is also returned because the caller needs it to identify the correct
        BlobStore for content retrieval.

        Args:
            group_name: Name of the group repository.
            asset_path: The full asset storage path to resolve.

        Returns:
            A ``(asset, member_repository)`` tuple, or ``None`` if no member
            contains the asset at the given path.

        Raises:
            ValueError: If *group_name* does not exist or is not a group.
        """
        members = self.get_member_repositories(group_name)
        self.logger.debug(
            "Resolving asset path '%s' across %d members of group '%s'.",
            asset_path,
            len(members),
            group_name,
        )

        for member in members:
            asset: Asset | None = (
                db.session.query(Asset)
                .filter(
                    Asset.repository_name == member.name,
                    Asset.path == asset_path,
                )
                .first()
            )
            if asset is not None:
                self.logger.debug(
                    "Asset '%s' resolved from member '%s' in group '%s'.",
                    asset_path,
                    member.name,
                    group_name,
                )
                return (asset, member)

        self.logger.debug(
            "Asset '%s' not found in any member of group '%s'.",
            asset_path,
            group_name,
        )
        return None

    def resolve_assets(
        self,
        group_name: str,
        path_prefix: str | None = None,
    ) -> list[tuple[Asset, Repository]]:
        """Aggregate assets from all group members with path deduplication.

        Collects assets from each member in order.  When multiple members
        contain an asset at the same path, only the first occurrence (from
        the highest-priority member) is included.

        Supports optional path-prefix filtering for browse tree navigation
        (Feature F-104).

        Args:
            group_name:  Name of the group repository.
            path_prefix: Optional prefix to filter asset paths.

        Returns:
            A list of ``(Asset, Repository)`` tuples preserving member
            order priority.

        Raises:
            ValueError: If *group_name* does not exist or is not a group.
        """
        members = self.get_member_repositories(group_name)
        self.logger.debug(
            "Aggregating assets across %d members of group '%s' "
            "(path_prefix=%s).",
            len(members),
            group_name,
            path_prefix,
        )

        seen_paths: set[str] = set()
        result: list[tuple[Asset, Repository]] = []

        for member in members:
            query = db.session.query(Asset).filter(
                Asset.repository_name == member.name,
            )
            if path_prefix is not None:
                query = query.filter(Asset.path.startswith(path_prefix))

            query = query.order_by(Asset.path)
            assets: list[Asset] = query.all()

            for asset in assets:
                if asset.path not in seen_paths:
                    seen_paths.add(asset.path)
                    result.append((asset, member))

        self.logger.debug(
            "Aggregated %d unique assets from group '%s'.",
            len(result),
            group_name,
        )
        return result

    # ===================================================================
    # Phase 6 — Content Streaming
    # ===================================================================

    def stream_asset_content(
        self,
        group_name: str,
        asset_path: str,
    ) -> tuple[Any, str, int] | None:
        """Resolve an asset from group members and stream its content.

        First resolves the asset via :meth:`resolve_asset`, then reads its
        binary content from the member repository's configured BlobStore.

        Args:
            group_name: Name of the group repository.
            asset_path: The full asset storage path.

        Returns:
            A ``(content_stream, content_type, size)`` tuple on success,
            where *content_stream* is a readable binary I/O object, or
            ``None`` if the asset could not be found in any member.

        Raises:
            ValueError: If *group_name* does not exist or is not a group.
        """
        resolution = self.resolve_asset(group_name, asset_path)
        if resolution is None:
            self.logger.debug(
                "Cannot stream asset '%s' from group '%s' — not found.",
                asset_path,
                group_name,
            )
            return None

        asset, member_repo = resolution

        # Determine content metadata
        content_type: str = asset.content_type or "application/octet-stream"
        size: int = asset.size or 0
        blob_ref: str | None = asset.blob_ref

        if not blob_ref:
            self.logger.warning(
                "Asset '%s' in member '%s' has no blob_ref.  "
                "Cannot stream content.",
                asset_path,
                member_repo.name,
            )
            return None

        # Load the BlobStore for the member repository
        try:
            blobstore_config: BlobStoreConfig | None = db.session.get(
                BlobStoreConfig, member_repo.blob_store_name,
            )
            if blobstore_config is None:
                self.logger.error(
                    "BlobStoreConfig '%s' not found for member '%s'.  "
                    "Cannot stream asset '%s'.",
                    member_repo.blob_store_name,
                    member_repo.name,
                    asset_path,
                )
                return None

            blobstore = create_blobstore_from_model(blobstore_config)
            blob_id = BlobId.from_string(blob_ref)
            content_stream = blobstore.get_stream(blob_id)

            if content_stream is None:
                self.logger.warning(
                    "Blob '%s' not found in BlobStore '%s' for asset '%s'.",
                    blob_ref,
                    member_repo.blob_store_name,
                    asset_path,
                )
                return None

            self.logger.debug(
                "Streaming asset '%s' (type=%s, size=%d) from member '%s' "
                "via BlobStore '%s'.",
                asset_path,
                content_type,
                size,
                member_repo.name,
                member_repo.blob_store_name,
            )
            return (content_stream, content_type, size)

        except Exception:
            self.logger.exception(
                "Error streaming asset '%s' from BlobStore '%s' "
                "for member '%s' in group '%s'.",
                asset_path,
                member_repo.blob_store_name,
                member_repo.name,
                group_name,
            )
            return None

    # ===================================================================
    # Phase 7 — Group Metadata Aggregation
    # ===================================================================

    def get_group_metadata(self, group_name: str) -> dict:
        """Return aggregated metadata about the group and its members.

        Collects summary counts (total components, total assets) across all
        resolved member repositories plus basic member information.

        Args:
            group_name: Name of the group repository.

        Returns:
            A dictionary with group name, format, member list, and
            aggregated content counts.

        Raises:
            ValueError: If *group_name* does not exist or is not a group.
        """
        repo = self._load_group_repository(group_name)
        members = self.get_member_repositories(group_name)

        total_component_count: int = 0
        total_asset_count: int = 0
        member_info: list[dict[str, Any]] = []

        for member in members:
            comp_count = member.component_count
            asset_count = member.asset_count
            total_component_count += comp_count
            total_asset_count += asset_count
            member_info.append(
                {
                    "name": member.name,
                    "type": member.type,
                    "online": member.online,
                    "component_count": comp_count,
                    "asset_count": asset_count,
                }
            )

        metadata: dict[str, Any] = {
            "name": group_name,
            "format": repo.format,
            "member_count": len(members),
            "members": member_info,
            "total_components": total_component_count,
            "total_assets": total_asset_count,
        }
        self.logger.info(
            "Group '%s' metadata: %d members, %d components, %d assets.",
            group_name,
            len(members),
            total_component_count,
            total_asset_count,
        )
        return metadata

    # ===================================================================
    # Phase 8 — Member Management
    # ===================================================================

    def update_members(
        self,
        group_name: str,
        member_names: list[str],
    ) -> Repository:
        """Replace the ordered member list of a group repository.

        Every name in *member_names* is validated:

        1. The referenced repository must exist.
        2. The referenced repository must **not** be the group itself
           (self-reference prevention).
        3. The referenced repository must have the same ``format`` as the
           group repository.

        On success the group's ``attributes['group']['memberNames']`` is
        updated and the change is committed to the database.

        Args:
            group_name:   Name of the group repository to update.
            member_names: New ordered list of member repository names.

        Returns:
            The updated ``Repository`` model instance.

        Raises:
            ValueError: If validation fails for any member name, or if
                *group_name* does not exist or is not a group.
        """
        repo = self._load_group_repository(group_name)
        old_members = repo.group_members

        # Validate each member
        for member_name in member_names:
            if member_name == group_name:
                raise ValueError(
                    f"Cannot add group '{group_name}' as a member of itself."
                )
            member: Repository | None = db.session.get(
                Repository, member_name,
            )
            if member is None:
                raise ValueError(
                    f"Member repository '{member_name}' does not exist."
                )
            if member.format != repo.format:
                raise ValueError(
                    f"Member '{member_name}' has format '{member.format}', "
                    f"but group '{group_name}' requires format "
                    f"'{repo.format}'."
                )

        # Update attributes
        attrs: dict = copy.deepcopy(repo.attributes) if repo.attributes else {}
        if "group" not in attrs:
            attrs["group"] = {}
        attrs["group"]["memberNames"] = list(member_names)
        repo.attributes = attrs

        db.session.add(repo)
        db.session.commit()

        self.logger.info(
            "Updated members of group '%s': %s → %s.",
            group_name,
            old_members,
            member_names,
        )
        return repo

    def add_member(
        self,
        group_name: str,
        member_name: str,
        position: int | None = None,
    ) -> Repository:
        """Add a single member repository at a specific position.

        If *position* is ``None`` the member is appended to the end of the
        list.  The method validates existence, format match, and prevents
        duplicate membership.

        Args:
            group_name:  Name of the group repository.
            member_name: Name of the repository to add as a member.
            position:    Zero-based insertion index, or ``None`` to append.

        Returns:
            The updated ``Repository`` model instance.

        Raises:
            ValueError: If validation fails or the member is already present.
        """
        repo = self._load_group_repository(group_name)

        if member_name == group_name:
            raise ValueError(
                f"Cannot add group '{group_name}' as a member of itself."
            )

        member: Repository | None = db.session.get(Repository, member_name)
        if member is None:
            raise ValueError(
                f"Member repository '{member_name}' does not exist."
            )
        if member.format != repo.format:
            raise ValueError(
                f"Member '{member_name}' has format '{member.format}', "
                f"but group '{group_name}' requires format "
                f"'{repo.format}'."
            )

        current_members: list[str] = list(repo.group_members)
        if member_name in current_members:
            raise ValueError(
                f"Repository '{member_name}' is already a member of "
                f"group '{group_name}'."
            )

        if position is not None and 0 <= position <= len(current_members):
            current_members.insert(position, member_name)
        else:
            current_members.append(member_name)

        attrs: dict = copy.deepcopy(repo.attributes) if repo.attributes else {}
        if "group" not in attrs:
            attrs["group"] = {}
        attrs["group"]["memberNames"] = current_members
        repo.attributes = attrs

        db.session.add(repo)
        db.session.commit()

        self.logger.info(
            "Added member '%s' to group '%s' at position %s.  "
            "Members: %s.",
            member_name,
            group_name,
            position if position is not None else "end",
            current_members,
        )
        return repo

    def remove_member(
        self,
        group_name: str,
        member_name: str,
    ) -> Repository:
        """Remove a member repository from the group.

        If *member_name* is not currently a member, a ``ValueError`` is
        raised.

        Args:
            group_name:  Name of the group repository.
            member_name: Name of the repository to remove.

        Returns:
            The updated ``Repository`` model instance.

        Raises:
            ValueError: If *member_name* is not a member, or if
                *group_name* does not exist or is not a group.
        """
        repo = self._load_group_repository(group_name)
        current_members: list[str] = list(repo.group_members)

        if member_name not in current_members:
            raise ValueError(
                f"Repository '{member_name}' is not a member of "
                f"group '{group_name}'."
            )

        current_members.remove(member_name)

        attrs: dict = copy.deepcopy(repo.attributes) if repo.attributes else {}
        if "group" not in attrs:
            attrs["group"] = {}
        attrs["group"]["memberNames"] = current_members
        repo.attributes = attrs

        db.session.add(repo)
        db.session.commit()

        self.logger.info(
            "Removed member '%s' from group '%s'.  Remaining: %s.",
            member_name,
            group_name,
            current_members,
        )
        return repo
