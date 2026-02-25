"""
Search Indexing and Query Service.

This module implements Feature F-103 (Content Indexing and Search) for the
Nexus Repository Flask application.  It provides a high-level service API
consumed by the REST API layer (``src.app.api.search``) for indexing
components/assets and executing full-text search queries.

**Architecture Context:**

Replaces the Java Elasticsearch 2.4.3 integration from the original Sonatype
Nexus Repository system.  The service delegates to the lower-level
``src.app.search`` package for actual Elasticsearch operations:

+-----------------------------+---------------------------------------------+
| Sub-module                  | Responsibility                              |
+=============================+=============================================+
| ``elasticsearch_client``    | Connection management, health checking      |
+-----------------------------+---------------------------------------------+
| ``index_manager``           | Index lifecycle, document CRUD, bulk ops    |
+-----------------------------+---------------------------------------------+
| ``query_builder``           | Query DSL construction, result parsing      |
+-----------------------------+---------------------------------------------+

**Graceful Degradation:**

When Elasticsearch is unavailable (either not installed or not reachable),
the service automatically falls back to SQL-based ``LIKE`` queries against
the SQLAlchemy ``Component`` model.  This ensures the application remains
functional — albeit with reduced search capabilities — without requiring
Elasticsearch.

**Performance Target:**

< 2 seconds for full-text search across 100,000 components (AAP Section 0.7.3).

**Exports:**

- :class:`SearchService` — main service class with all search and indexing ops
- :data:`DEFAULT_PAGE_SIZE` — default page size for search queries (50)
- :data:`MAX_PAGE_SIZE` — hard upper limit for page sizes (500)
- :data:`ES_AVAILABLE` — boolean flag indicating whether ES modules loaded
"""

from __future__ import annotations

import logging
from typing import Any

from flask import current_app
from sqlalchemy import or_

from src.app.extensions import db
from src.app.models.component import Component
from src.app.models.asset import Asset
from src.app.models.repository import Repository

# ---------------------------------------------------------------------------
# Elasticsearch imports — wrapped in try/except for graceful degradation.
# When the ``elasticsearch`` package is not installed or the search sub-
# modules are unavailable, the service falls back to SQL-based search.
# ---------------------------------------------------------------------------
try:
    from src.app.search.elasticsearch_client import get_es_client, get_es_wrapper
    from src.app.search.index_manager import IndexManager
    from src.app.search.query_builder import (
        QueryBuilder,
        SearchQuery,
        execute_search,
        search_components as _es_search_components,
        search_assets as _es_search_assets,
    )
    ES_AVAILABLE: bool = True
except ImportError:
    ES_AVAILABLE = False

# ---------------------------------------------------------------------------
# Module-level logger — replaces SLF4J 1.7.36 + Logback 1.2.13
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_PAGE_SIZE: int = 50
"""Default number of results returned per search page."""

MAX_PAGE_SIZE: int = 500
"""Hard upper limit on results per page to prevent resource exhaustion and
maintain the < 2-second performance target for 100K component datasets."""


# ═══════════════════════════════════════════════════════════════════════════
# SearchService class
# ═══════════════════════════════════════════════════════════════════════════


class SearchService:
    """High-level search and indexing service for the Nexus Repository system.

    Provides a unified API for:

    - **Searching** components and assets (with Elasticsearch or SQL fallback)
    - **Indexing** individual components/assets into the search index
    - **Removing** documents from the search index
    - **Reindexing** entire repositories or the whole system
    - **Health checking** the search backend status

    The service is designed for use by API route handlers and other services.
    It should be instantiated once per application (or per request context)
    and reused.

    Example::

        service = SearchService()
        results = service.search(query="commons-lang", format_type="maven2")
        status = service.get_search_status()
    """

    def __init__(self) -> None:
        """Initialise the SearchService.

        Sets up the instance logger and prepares lazy initialisation of the
        :class:`IndexManager`.  No Elasticsearch connection is established
        until the first operation that requires it.
        """
        self.logger: logging.Logger = logging.getLogger(__name__)
        self._index_manager: Any = None
        self.logger.debug("SearchService initialised (ES_AVAILABLE=%s)", ES_AVAILABLE)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def index_manager(self) -> Any:
        """Lazily initialised :class:`IndexManager` instance.

        Returns ``None`` when Elasticsearch modules are not available,
        enabling graceful degradation in callers that check before use.

        Returns
        -------
        IndexManager or None
            The index manager instance, or ``None`` if ES is unavailable.
        """
        if not ES_AVAILABLE:
            return None
        if self._index_manager is None:
            try:
                self._index_manager = IndexManager()
                self.logger.debug("IndexManager lazily initialised")
            except Exception as exc:
                self.logger.error("Failed to initialise IndexManager: %s", exc)
                return None
        return self._index_manager

    # ------------------------------------------------------------------
    # Search Query Execution
    # ------------------------------------------------------------------

    def search(
        self,
        query: str | None = None,
        format_type: str | None = None,
        repository: str | None = None,
        namespace: str | None = None,
        name: str | None = None,
        version: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        sort: str = "name",
        direction: str = "asc",
    ) -> dict[str, Any]:
        """Execute a full-text search across components.

        This is the **main search entry point** consumed by the API layer.
        When Elasticsearch is available, the query is delegated to
        :func:`execute_search` from the query_builder module.  When ES is
        unavailable, the method falls back to SQL-based ``LIKE`` queries
        against the ``Component`` model.

        Parameters
        ----------
        query:
            Free-text search string matched against component name, namespace,
            and other searchable fields.
        format_type:
            Filter by repository format (e.g. ``"maven2"``, ``"npm"``).
        repository:
            Filter by repository name.
        namespace:
            Filter by component namespace (e.g. Maven groupId, npm scope).
        name:
            Filter by component name (partial match in SQL fallback).
        version:
            Filter by component version (exact match).
        page:
            1-based page number (minimum 1).
        page_size:
            Results per page (clamped to ``[1, MAX_PAGE_SIZE]``).
        sort:
            Sort field — ``"name"``, ``"date"``, ``"_score"``, etc.
        direction:
            Sort direction — ``"asc"`` or ``"desc"``.

        Returns
        -------
        dict[str, Any]
            A dictionary with the following keys:

            - ``items`` — list of matched component dictionaries
            - ``total_count`` — total number of matching components
            - ``page`` — current page number
            - ``page_size`` — results per page
            - ``facets`` — aggregation facets (empty dict in SQL fallback)
        """
        # Enforce pagination bounds.
        effective_page_size: int = max(1, min(page_size, MAX_PAGE_SIZE))
        effective_page: int = max(1, page)

        self.logger.debug(
            "search() called: query=%r, format=%r, repo=%r, page=%d, size=%d",
            query,
            format_type,
            repository,
            effective_page,
            effective_page_size,
        )

        # Attempt Elasticsearch-based search first.
        if ES_AVAILABLE:
            try:
                es_client = get_es_client()
                if es_client is not None:
                    search_query = SearchQuery(
                        q=query or "",
                        keyword=query,
                        format=format_type,
                        repository=repository,
                        namespace=namespace,
                        name=name,
                        version=version,
                        page=effective_page,
                        page_size=effective_page_size,
                        sort_by=sort,
                        sort_order=direction,
                        include_facets=True,
                    )
                    result: dict[str, Any] = execute_search(search_query, index_name="components")

                    # Normalise the response to the service-layer contract.
                    return {
                        "items": result.get("items", []),
                        "total_count": result.get("total", 0),
                        "page": result.get("page", effective_page),
                        "page_size": result.get("page_size", effective_page_size),
                        "facets": result.get("facets", {}),
                    }
                else:
                    self.logger.warning(
                        "Elasticsearch client unavailable — falling back to SQL search"
                    )
            except Exception as exc:
                self.logger.error(
                    "Elasticsearch search failed, falling back to SQL: %s", exc
                )

        # SQL fallback when Elasticsearch is not available or encountered an error.
        self.logger.info("Executing SQL fallback search for query=%r", query)
        return self._sql_search(
            query=query,
            format_type=format_type,
            repository=repository,
            namespace=namespace,
            name=name,
            version=version,
            page=effective_page,
            page_size=effective_page_size,
        )

    def search_components(self, **kwargs: Any) -> dict[str, Any]:
        """Convenience wrapper for component-specific search.

        Delegates to :func:`search_components` from the query_builder module
        when Elasticsearch is available, or falls back to the SQL-based
        :meth:`search` method.

        Parameters
        ----------
        **kwargs:
            Keyword arguments forwarded to :class:`SearchQuery` — for example
            ``q``, ``format``, ``repository``, ``page``, ``page_size``.

        Returns
        -------
        dict[str, Any]
            Paginated component search results.
        """
        if ES_AVAILABLE:
            try:
                es_client = get_es_client()
                if es_client is not None:
                    result: dict[str, Any] = _es_search_components(**kwargs)
                    return {
                        "items": result.get("items", []),
                        "total_count": result.get("total", 0),
                        "page": result.get("page", 1),
                        "page_size": result.get("page_size", DEFAULT_PAGE_SIZE),
                        "facets": result.get("facets", {}),
                    }
            except Exception as exc:
                self.logger.error(
                    "ES search_components failed, falling back to SQL: %s", exc
                )

        # Fallback to the general search method with kwargs mapped.
        return self.search(
            query=kwargs.get("q", ""),
            format_type=kwargs.get("format"),
            repository=kwargs.get("repository"),
            namespace=kwargs.get("namespace"),
            name=kwargs.get("name"),
            version=kwargs.get("version"),
            page=kwargs.get("page", 1),
            page_size=kwargs.get("page_size", DEFAULT_PAGE_SIZE),
        )

    def search_assets(self, **kwargs: Any) -> dict[str, Any]:
        """Convenience wrapper for asset-specific search.

        Delegates to :func:`search_assets` from the query_builder module
        when Elasticsearch is available, or falls back to a SQL-based
        query against the ``Asset`` model.

        Parameters
        ----------
        **kwargs:
            Keyword arguments forwarded to :class:`SearchQuery` — for example
            ``q``, ``format``, ``repository``, ``page``, ``page_size``.

        Returns
        -------
        dict[str, Any]
            Paginated asset search results.
        """
        if ES_AVAILABLE:
            try:
                es_client = get_es_client()
                if es_client is not None:
                    result: dict[str, Any] = _es_search_assets(**kwargs)
                    return {
                        "items": result.get("items", []),
                        "total_count": result.get("total", 0),
                        "page": result.get("page", 1),
                        "page_size": result.get("page_size", DEFAULT_PAGE_SIZE),
                        "facets": result.get("facets", {}),
                    }
            except Exception as exc:
                self.logger.error(
                    "ES search_assets failed, falling back to SQL: %s", exc
                )

        # SQL fallback for asset search.
        return self._sql_asset_search(**kwargs)

    # ------------------------------------------------------------------
    # SQL Fallback Search
    # ------------------------------------------------------------------

    def _sql_search(
        self,
        query: str | None,
        format_type: str | None,
        repository: str | None,
        namespace: str | None,
        name: str | None,
        version: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        """SQL-based fallback search when Elasticsearch is unavailable.

        Builds a SQLAlchemy query against the ``Component`` model using
        ``LIKE`` filters for partial text matching.  Provides no relevance
        scoring and no faceted aggregations — just basic filtered, paginated
        results.

        Parameters
        ----------
        query:
            Free-text search (matched with ``LIKE`` against name and namespace).
        format_type:
            Filter by repository format.
        repository:
            Filter by repository name.
        namespace:
            Exact namespace filter.
        name:
            Partial name filter (LIKE).
        version:
            Exact version filter.
        page:
            1-based page number.
        page_size:
            Results per page.

        Returns
        -------
        dict[str, Any]
            Paginated results matching the service-layer response contract.
        """
        try:
            q = Component.query

            # Free-text query: match against both name and namespace columns.
            if query:
                q = q.filter(
                    or_(
                        Component.name.ilike(f"%{query}%"),
                        Component.namespace.ilike(f"%{query}%"),
                    )
                )

            # Exact repository filter.
            if repository:
                q = q.filter(Component.repository_name == repository)

            # Format filter requires a join or subquery against Repository.
            if format_type:
                q = q.join(Repository, Component.repository_name == Repository.name).filter(
                    Repository.format == format_type
                )

            # Exact namespace filter.
            if namespace:
                q = q.filter(Component.namespace == namespace)

            # Partial name filter.
            if name:
                q = q.filter(Component.name.ilike(f"%{name}%"))

            # Exact version filter.
            if version:
                q = q.filter(Component.version == version)

            # Count total before pagination.
            total_count: int = q.count()

            # Apply pagination.
            offset: int = (page - 1) * page_size
            components = q.offset(offset).limit(page_size).all()

            # Serialise results to dictionaries.
            items: list[dict[str, Any]] = []
            for comp in components:
                item: dict[str, Any] = {
                    "id": comp.id,
                    "repository_name": comp.repository_name,
                    "namespace": comp.namespace,
                    "name": comp.name,
                    "version": comp.version,
                }
                # Include format from repository if relationship is loaded.
                if hasattr(comp, "repository") and comp.repository is not None:
                    item["format"] = getattr(comp.repository, "format", None)
                items.append(item)

            self.logger.debug(
                "SQL fallback search returned %d/%d items (page=%d)",
                len(items),
                total_count,
                page,
            )

            return {
                "items": items,
                "total_count": total_count,
                "page": page,
                "page_size": page_size,
                "facets": {},
            }

        except Exception as exc:
            self.logger.error("SQL fallback search failed: %s", exc)
            return {
                "items": [],
                "total_count": 0,
                "page": page,
                "page_size": page_size,
                "facets": {},
            }

    def _sql_asset_search(self, **kwargs: Any) -> dict[str, Any]:
        """SQL-based fallback search for assets.

        Builds a SQLAlchemy query against the ``Asset`` model for basic
        filtered, paginated results.

        Parameters
        ----------
        **kwargs:
            Search filters — ``q``, ``repository``, ``page``, ``page_size``.

        Returns
        -------
        dict[str, Any]
            Paginated asset results matching the service-layer response contract.
        """
        try:
            query_text: str | None = kwargs.get("q")
            repository_filter: str | None = kwargs.get("repository")
            page_num: int = max(1, kwargs.get("page", 1))
            size: int = max(1, min(kwargs.get("page_size", DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))

            q = Asset.query

            # Free-text query against path.
            if query_text:
                q = q.filter(Asset.path.ilike(f"%{query_text}%"))

            # Repository filter.
            if repository_filter:
                q = q.filter_by(repository_name=repository_filter)

            total_count: int = q.count()
            offset: int = (page_num - 1) * size
            assets = q.offset(offset).limit(size).all()

            items: list[dict[str, Any]] = []
            for asset in assets:
                items.append({
                    "id": asset.id,
                    "repository_name": asset.repository_name,
                    "path": asset.path,
                    "content_type": getattr(asset, "content_type", None),
                    "size": getattr(asset, "size", None),
                })

            return {
                "items": items,
                "total_count": total_count,
                "page": page_num,
                "page_size": size,
                "facets": {},
            }

        except Exception as exc:
            self.logger.error("SQL asset search failed: %s", exc)
            page_num = kwargs.get("page", 1)
            size = kwargs.get("page_size", DEFAULT_PAGE_SIZE)
            return {
                "items": [],
                "total_count": 0,
                "page": page_num,
                "page_size": size,
                "facets": {},
            }

    # ------------------------------------------------------------------
    # Index Management
    # ------------------------------------------------------------------

    def index_component(self, component: Component) -> bool:
        """Index a single component in Elasticsearch.

        Should be called after component create or update operations to keep
        the search index in sync with the database.

        Parameters
        ----------
        component:
            The ``Component`` model instance to index.

        Returns
        -------
        bool
            ``True`` if the component was successfully indexed, ``False`` if
            Elasticsearch is unavailable or an error occurred.
        """
        mgr = self.index_manager
        if mgr is None:
            self.logger.debug(
                "index_component skipped — ES unavailable (component.id=%s)",
                getattr(component, "id", "?"),
            )
            return False

        try:
            result: bool = mgr.index_component(component)
            if result:
                self.logger.info(
                    "Indexed component id=%s name=%r in search index",
                    component.id,
                    component.name,
                )
            return result
        except Exception as exc:
            self.logger.error(
                "Failed to index component id=%s: %s",
                getattr(component, "id", "?"),
                exc,
            )
            return False

    def index_asset(self, asset: Asset) -> bool:
        """Index a single asset in Elasticsearch.

        Should be called after asset create or update operations to keep the
        search index in sync with the database.

        Parameters
        ----------
        asset:
            The ``Asset`` model instance to index.

        Returns
        -------
        bool
            ``True`` if the asset was successfully indexed, ``False`` if
            Elasticsearch is unavailable or an error occurred.
        """
        mgr = self.index_manager
        if mgr is None:
            self.logger.debug(
                "index_asset skipped — ES unavailable (asset.id=%s)",
                getattr(asset, "id", "?"),
            )
            return False

        try:
            result: bool = mgr.index_asset(asset)
            if result:
                self.logger.info(
                    "Indexed asset id=%s path=%r in search index",
                    asset.id,
                    getattr(asset, "path", "?"),
                )
            return result
        except Exception as exc:
            self.logger.error(
                "Failed to index asset id=%s: %s",
                getattr(asset, "id", "?"),
                exc,
            )
            return False

    def remove_component_from_index(
        self, component_id: int, repository_name: str
    ) -> bool:
        """Remove a component from the search index.

        Should be called when a component is deleted from the database to keep
        the search index in sync.

        Parameters
        ----------
        component_id:
            Primary key of the component to remove.
        repository_name:
            Name of the owning repository (used for logging context).

        Returns
        -------
        bool
            ``True`` if the component was successfully removed, ``False`` if
            Elasticsearch is unavailable or an error occurred.
        """
        mgr = self.index_manager
        if mgr is None:
            self.logger.debug(
                "remove_component_from_index skipped — ES unavailable "
                "(component_id=%s, repo=%s)",
                component_id,
                repository_name,
            )
            return False

        try:
            result: bool = mgr.remove_component(component_id)
            if result:
                self.logger.info(
                    "Removed component id=%s (repo=%s) from search index",
                    component_id,
                    repository_name,
                )
            return result
        except Exception as exc:
            self.logger.error(
                "Failed to remove component id=%s from index: %s",
                component_id,
                exc,
            )
            return False

    def remove_asset_from_index(
        self, asset_id: int, repository_name: str
    ) -> bool:
        """Remove an asset from the search index.

        Should be called when an asset is deleted from the database to keep
        the search index in sync.

        Parameters
        ----------
        asset_id:
            Primary key of the asset to remove.
        repository_name:
            Name of the owning repository (used for logging context).

        Returns
        -------
        bool
            ``True`` if the asset was successfully removed, ``False`` if
            Elasticsearch is unavailable or an error occurred.
        """
        mgr = self.index_manager
        if mgr is None:
            self.logger.debug(
                "remove_asset_from_index skipped — ES unavailable "
                "(asset_id=%s, repo=%s)",
                asset_id,
                repository_name,
            )
            return False

        try:
            result: bool = mgr.remove_asset(asset_id)
            if result:
                self.logger.info(
                    "Removed asset id=%s (repo=%s) from search index",
                    asset_id,
                    repository_name,
                )
            return result
        except Exception as exc:
            self.logger.error(
                "Failed to remove asset id=%s from index: %s",
                asset_id,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Reindexing Operations
    # ------------------------------------------------------------------

    def reindex_repository(self, repository_name: str) -> dict[str, Any]:
        """Reindex all components and assets for a specific repository.

        Retrieves all components and assets belonging to the repository from
        the database and bulk-indexes them into Elasticsearch.

        Parameters
        ----------
        repository_name:
            Name of the repository to reindex.

        Returns
        -------
        dict[str, Any]
            Summary dictionary with keys:

            - ``components_indexed`` — number of components successfully indexed
            - ``assets_indexed`` — number of assets successfully indexed
            - ``errors`` — total number of indexing errors encountered
        """
        summary: dict[str, Any] = {
            "components_indexed": 0,
            "assets_indexed": 0,
            "errors": 0,
        }

        mgr = self.index_manager
        if mgr is None:
            self.logger.warning(
                "reindex_repository(%s) skipped — ES unavailable",
                repository_name,
            )
            return summary

        self.logger.info("Starting reindex for repository '%s'", repository_name)

        try:
            # Look up the repository to verify it exists and access relationships.
            repo = Repository.query.filter_by(name=repository_name).first()
            if repo is None:
                self.logger.warning(
                    "Repository '%s' not found — cannot reindex",
                    repository_name,
                )
                return summary

            # --- Bulk-index components ---
            components = repo.components.all()
            if components:
                component_docs: list[dict[str, Any]] = []
                for comp in components:
                    repo_format = getattr(repo, "format", None)
                    tags: list[str] = []
                    if hasattr(comp, "get_attribute"):
                        tags = comp.get_attribute("tags", [])
                    elif isinstance(getattr(comp, "attributes", None), dict):
                        tags = comp.attributes.get("tags", [])

                    component_docs.append({
                        "id": comp.id,
                        "repository_name": comp.repository_name,
                        "namespace": comp.namespace,
                        "name": comp.name,
                        "version": comp.version,
                        "format": repo_format,
                        "group": comp.namespace,
                        "tags": tags,
                        "attributes": getattr(comp, "attributes", None) or {},
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
                    })

                from src.app.search.index_manager import COMPONENT_INDEX
                success_count, errors = mgr.bulk_index(
                    COMPONENT_INDEX, component_docs, id_field="id"
                )
                summary["components_indexed"] = success_count
                summary["errors"] += len(errors) if isinstance(errors, list) else 0

            # --- Bulk-index assets ---
            assets = repo.assets.all()
            if assets:
                asset_docs: list[dict[str, Any]] = []
                for asset_obj in assets:
                    repo_format = getattr(repo, "format", None)
                    asset_docs.append({
                        "id": asset_obj.id,
                        "component_id": getattr(asset_obj, "component_id", None),
                        "repository_name": asset_obj.repository_name,
                        "path": getattr(asset_obj, "path", None),
                        "content_type": getattr(asset_obj, "content_type", None),
                        "checksum_sha1": getattr(asset_obj, "checksum_sha1", None),
                        "checksum_sha256": getattr(asset_obj, "checksum_sha256", None),
                        "size": getattr(asset_obj, "size", None),
                        "format": repo_format,
                        "last_downloaded": (
                            asset_obj.last_downloaded.isoformat()
                            if getattr(asset_obj, "last_downloaded", None) is not None
                            else None
                        ),
                        "created_at": (
                            asset_obj.created_at.isoformat()
                            if getattr(asset_obj, "created_at", None) is not None
                            else None
                        ),
                        "updated_at": (
                            asset_obj.updated_at.isoformat()
                            if getattr(asset_obj, "updated_at", None) is not None
                            else None
                        ),
                    })

                from src.app.search.index_manager import ASSET_INDEX
                success_count, errors = mgr.bulk_index(
                    ASSET_INDEX, asset_docs, id_field="id"
                )
                summary["assets_indexed"] = success_count
                summary["errors"] += len(errors) if isinstance(errors, list) else 0

            self.logger.info(
                "Reindex for repository '%s' complete: %s",
                repository_name,
                summary,
            )
            return summary

        except Exception as exc:
            self.logger.error(
                "Reindex for repository '%s' failed: %s",
                repository_name,
                exc,
            )
            summary["errors"] += 1
            return summary

    def reindex_all(self) -> dict[str, Any]:
        """Reindex all repositories in the system.

        Iterates through every repository and reindexes its components and
        assets.  Uses alias swapping (via ``IndexManager``) for zero-downtime
        reindexing when available.

        Returns
        -------
        dict[str, Any]
            Aggregate summary dictionary with keys:

            - ``repositories_processed`` — number of repositories processed
            - ``total_components_indexed`` — total components indexed
            - ``total_assets_indexed`` — total assets indexed
            - ``total_errors`` — total number of indexing errors
            - ``per_repository`` — dict mapping repo names to individual summaries
        """
        aggregate: dict[str, Any] = {
            "repositories_processed": 0,
            "total_components_indexed": 0,
            "total_assets_indexed": 0,
            "total_errors": 0,
            "per_repository": {},
        }

        mgr = self.index_manager
        if mgr is None:
            self.logger.warning("reindex_all() skipped — ES unavailable")
            return aggregate

        self.logger.info("Starting full system reindex")

        try:
            repositories = Repository.query.all()
            self.logger.info(
                "Found %d repositories to reindex", len(repositories)
            )

            for repo in repositories:
                repo_summary: dict[str, Any] = self.reindex_repository(repo.name)
                aggregate["repositories_processed"] += 1
                aggregate["total_components_indexed"] += repo_summary.get(
                    "components_indexed", 0
                )
                aggregate["total_assets_indexed"] += repo_summary.get(
                    "assets_indexed", 0
                )
                aggregate["total_errors"] += repo_summary.get("errors", 0)
                aggregate["per_repository"][repo.name] = repo_summary

            self.logger.info(
                "Full system reindex complete: %d repos, %d components, "
                "%d assets, %d errors",
                aggregate["repositories_processed"],
                aggregate["total_components_indexed"],
                aggregate["total_assets_indexed"],
                aggregate["total_errors"],
            )
            return aggregate

        except Exception as exc:
            self.logger.error("Full system reindex failed: %s", exc)
            aggregate["total_errors"] += 1
            return aggregate

    # ------------------------------------------------------------------
    # Health Check
    # ------------------------------------------------------------------

    def get_search_status(self) -> dict[str, Any]:
        """Return the current health status of the search backend.

        Queries the Elasticsearch cluster for health information and index
        statistics.  When Elasticsearch is unavailable, returns a degraded
        status indicating that the search service is operating in SQL-fallback
        mode.

        Returns
        -------
        dict[str, Any]
            Status dictionary with the following keys:

            - ``available`` — whether Elasticsearch is reachable
            - ``cluster_status`` — ``"green"`` / ``"yellow"`` / ``"red"`` /
              ``"unavailable"``
            - ``component_count`` — number of indexed component documents
            - ``asset_count`` — number of indexed asset documents
            - ``index_size`` — human-readable total index size
        """
        # Default degraded status.
        status: dict[str, Any] = {
            "available": False,
            "cluster_status": "unavailable",
            "component_count": 0,
            "asset_count": 0,
            "index_size": "0 B",
        }

        if not ES_AVAILABLE:
            self.logger.debug("get_search_status: ES modules not available")
            return status

        try:
            wrapper = get_es_wrapper()
            if wrapper is None:
                self.logger.debug(
                    "get_search_status: ES wrapper not initialised"
                )
                return status

            # Check cluster health.
            health: dict[str, Any] = wrapper.check_health()
            cluster_status: str = health.get("status", "unavailable")
            is_available: bool = cluster_status in ("green", "yellow", "red")

            status["available"] = is_available
            status["cluster_status"] = cluster_status

            if not is_available:
                return status

            # Retrieve index statistics for component and asset counts.
            mgr = self.index_manager
            if mgr is not None:
                from src.app.search.index_manager import COMPONENT_INDEX, ASSET_INDEX

                comp_stats = mgr.get_index_stats(COMPONENT_INDEX)
                if comp_stats is not None:
                    status["component_count"] = comp_stats.get("doc_count", 0)

                asset_stats = mgr.get_index_stats(ASSET_INDEX)
                if asset_stats is not None:
                    status["asset_count"] = asset_stats.get("doc_count", 0)

                # Calculate human-readable total index size.
                total_bytes: int = 0
                if comp_stats is not None:
                    total_bytes += comp_stats.get("size_in_bytes", 0)
                if asset_stats is not None:
                    total_bytes += asset_stats.get("size_in_bytes", 0)
                status["index_size"] = self._format_bytes(total_bytes)

            return status

        except Exception as exc:
            self.logger.error("get_search_status failed: %s", exc)
            return status

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_bytes(num_bytes: int) -> str:
        """Convert a byte count to a human-readable string.

        Examples: ``"0 B"``, ``"1.5 KB"``, ``"3.2 MB"``, ``"1.1 GB"``.

        Parameters
        ----------
        num_bytes:
            The number of bytes to format.

        Returns
        -------
        str
            Human-readable size string.
        """
        if num_bytes < 0:
            num_bytes = 0
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if abs(num_bytes) < 1024.0:
                return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} {unit}"
            num_bytes /= 1024.0  # type: ignore[assignment]
        return f"{num_bytes:.1f} PB"
