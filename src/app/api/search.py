"""
Full-Text Search REST API Blueprint (Feature F-103).

This module implements the search API endpoints for the Nexus Repository
Flask application, providing Elasticsearch-backed component and asset search
across all repositories with format-specific coordinate search.

**Architecture Context:**

Replaces the Java Elasticsearch 2.4.3 search REST API from the original
Sonatype Nexus Repository system.  All four search endpoints delegate to
:class:`~src.app.services.search_service.SearchService`, which tries
Elasticsearch first and gracefully degrades to SQL ``LIKE`` queries when
Elasticsearch is unavailable.

**Endpoints:**

+---------+------------------------------------------+---------------------------------+
| Method  | URL                                      | Description                     |
+=========+==========================================+=================================+
| GET     | ``/api/v1/search``                       | Full-text component search      |
+---------+------------------------------------------+---------------------------------+
| GET     | ``/api/v1/search/assets``                | Asset-level search              |
+---------+------------------------------------------+---------------------------------+
| GET     | ``/api/v1/search/assets/sha1/<sha1>``    | Find assets by SHA-1 checksum   |
+---------+------------------------------------------+---------------------------------+
| GET     | ``/api/v1/search/browse/<repo_name>``    | Hierarchical browse tree (F-104)|
+---------+------------------------------------------+---------------------------------+

**Performance Target:**

< 2 seconds for full-text search across 100,000 components (AAP Section 0.7.3).

**Authentication:**

All endpoints require authentication via the multi-realm chain
(:func:`~src.app.auth.authentication.login_required`).  The browse tree
endpoint additionally requires repository-level read permission
(:func:`~src.app.auth.authorization.require_repository_permission`).

Exports:
    search_bp : flask_smorest.Blueprint
        The search API blueprint registered at ``/api/v1/search``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from flask import current_app
from flask_smorest import Blueprint, abort

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_repository_permission
from src.app.schemas.search import (
    SearchQuerySchema,
    SearchResultAssetSchema,
    SearchResultItemSchema,
    SearchResultSchema,
)
from src.app.services.search_service import SearchService

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for search queries, ES connectivity status,
# SQL fallback decisions, and browse tree navigation.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------
# Replaces the RESTEasy 6.2.7 JAX-RS search resource class from the Java
# source system.  The Blueprint is registered in src/app/api/__init__.py
# via register_api_blueprints() and is bound to the Flask app during
# create_app() in src/app/factory.py.
# ---------------------------------------------------------------------------

search_bp: Blueprint = Blueprint(
    "search",
    __name__,
    url_prefix="/api/v1/search",
    description="Full-text search across repositories (F-103)",
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "search_bp",
]


# ===========================================================================
# Helper: Instantiate SearchService
# ===========================================================================


def _get_search_service() -> SearchService:
    """Retrieve or instantiate the :class:`SearchService`.

    Uses Flask application context to check for a cached service instance.
    Falls back to direct instantiation if the extensions registry does not
    contain a pre-configured instance.

    Returns:
        A :class:`SearchService` ready for query execution.
    """
    # Attempt to retrieve a pre-initialised instance from app extensions.
    service: Optional[SearchService] = None
    try:
        service = current_app.extensions.get("search_service")  # type: ignore[union-attr]
    except (AttributeError, RuntimeError):
        pass

    if service is None:
        service = SearchService()
        logger.debug("Created ad-hoc SearchService instance for request")

    return service


# ===========================================================================
# Helper: Build browse tree entries from assets
# ===========================================================================


def _build_browse_entries(
    assets: list[Dict[str, Any]],
    base_path: str,
) -> list[Dict[str, Any]]:
    """Build a hierarchical folder-like browse listing from flat asset paths.

    Given a list of asset dictionaries (each with a ``path`` key) and a
    current ``base_path``, this function computes the set of immediate
    children — both "folders" (path segments) and "files" (leaf assets) —
    that would be visible when navigating to ``base_path``.

    This implements Feature F-104 (Browse Tree Navigation).

    Args:
        assets: List of asset dictionaries with at least a ``path`` key.
        base_path: The current browsing directory path.  Should start and
            end with ``/`` (e.g. ``"/"``, ``"/org/apache/"``).

    Returns:
        A list of entry dictionaries, each containing:

        - ``name`` — display name of the entry (folder name or file name)
        - ``path`` — full path of the entry
        - ``type`` — ``"folder"`` or ``"file"``
        - ``leaf`` — ``True`` for files, ``False`` for folders
        - ``content_type`` — MIME type for files, ``None`` for folders
        - ``size`` — byte size for files, ``None`` for folders
    """
    # Normalise base_path to end with "/" for reliable prefix stripping.
    if not base_path.endswith("/"):
        base_path = base_path + "/"

    # Track folders and files at this level.
    folders_seen: set[str] = set()
    entries: list[Dict[str, Any]] = []

    for asset in assets:
        asset_path: str = asset.get("path", "")

        # Normalise leading slash if missing.
        if not asset_path.startswith("/"):
            asset_path = "/" + asset_path

        # Only consider assets under the current base_path.
        if not asset_path.startswith(base_path):
            continue

        # Compute relative path from base_path.
        relative: str = asset_path[len(base_path):]
        if not relative:
            continue

        parts: list[str] = relative.split("/")

        if len(parts) == 1:
            # This is a direct file child at this level.
            entries.append(
                {
                    "name": parts[0],
                    "path": asset_path,
                    "type": "file",
                    "leaf": True,
                    "content_type": asset.get("content_type"),
                    "size": asset.get("size"),
                }
            )
        else:
            # This asset is deeper — the first segment is a folder.
            folder_name: str = parts[0]
            if folder_name not in folders_seen:
                folders_seen.add(folder_name)
                entries.append(
                    {
                        "name": folder_name,
                        "path": base_path + folder_name + "/",
                        "type": "folder",
                        "leaf": False,
                        "content_type": None,
                        "size": None,
                    }
                )

    # Sort folders first, then files, both alphabetically.
    entries.sort(key=lambda e: (0 if e["type"] == "folder" else 1, e["name"]))

    return entries


# ===========================================================================
# Endpoint 1: Search Components — GET /api/v1/search
# ===========================================================================


@search_bp.route("/", methods=["GET"])
@search_bp.arguments(SearchQuerySchema, location="query")
@search_bp.response(200, SearchResultSchema)
@search_bp.doc(
    summary="Search components",
    description=(
        "Full-text search across all repositories.  Supports keyword search, "
        "format-specific coordinate filters (Maven groupId, npm scope, Docker "
        "image), pagination, and sorting.  Uses Elasticsearch when available "
        "with automatic SQL LIKE fallback.  Performance target: < 2 seconds "
        "for 100K components (AAP Section 0.7.3)."
    ),
)
@login_required
def search_components(args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a full-text component search.

    Delegates to :meth:`SearchService.search` which tries Elasticsearch
    first and falls back to SQL-based ``LIKE`` queries when ES is
    unavailable.

    Args:
        args: Validated query parameters from :class:`SearchQuerySchema`.
            Keys include ``q``, ``format``, ``repository``, ``group``,
            ``name``, ``version``, ``sort``, ``direction``, ``page``,
            ``page_size``.

    Returns:
        A dictionary matching :class:`SearchResultSchema` with keys
        ``items``, ``total_count``, ``page``, ``page_size``,
        ``continuation_token``, and ``facets``.
    """
    logger.info(
        "Component search: q=%r, format=%r, repository=%r, page=%d",
        args.get("q"),
        args.get("format"),
        args.get("repository"),
        args.get("page", 1),
    )

    service: SearchService = _get_search_service()

    try:
        result: Dict[str, Any] = service.search(
            query=args.get("q"),
            format_type=args.get("format"),
            repository=args.get("repository"),
            namespace=args.get("group"),
            name=args.get("name"),
            version=args.get("version"),
            page=args.get("page", 1),
            page_size=args.get("page_size", 50),
            sort=args.get("sort", "name"),
            direction=args.get("direction", "asc"),
        )
    except Exception as exc:
        logger.error("Component search failed: %s", exc, exc_info=True)
        # Return empty result set on failure rather than 500 — the search
        # degradation model treats search outages as non-fatal.
        result = {
            "items": [],
            "total_count": 0,
            "page": args.get("page", 1),
            "page_size": args.get("page_size", 50),
            "facets": {},
        }

    # Normalise response keys to match SearchResultSchema field names.
    response: Dict[str, Any] = {
        "items": result.get("items", []),
        "total_count": result.get("total_count", 0),
        "page": result.get("page", args.get("page", 1)),
        "page_size": result.get("page_size", args.get("page_size", 50)),
        "continuation_token": result.get("continuation_token"),
        "facets": result.get("facets"),
        "sort": args.get("sort", "name"),
        "direction": args.get("direction", "asc"),
    }

    logger.debug(
        "Component search returned %d items (total=%d, page=%d)",
        len(response["items"]),
        response["total_count"],
        response["page"],
    )

    return response


# ===========================================================================
# Endpoint 2: Search Assets — GET /api/v1/search/assets
# ===========================================================================


@search_bp.route("/assets", methods=["GET"])
@search_bp.arguments(SearchQuerySchema, location="query")
@search_bp.response(200, SearchResultSchema)
@search_bp.doc(
    summary="Search assets",
    description=(
        "Asset-level search returning individual files (JARs, tarballs, "
        "Docker layers, etc.) with path, checksum, size, and content type.  "
        "Supports the same query parameters as component search."
    ),
)
@login_required
def search_assets(args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute an asset-level search.

    Delegates to :meth:`SearchService.search_assets` which tries
    Elasticsearch first and falls back to SQL-based queries when ES is
    unavailable.

    Args:
        args: Validated query parameters from :class:`SearchQuerySchema`.

    Returns:
        A dictionary matching :class:`SearchResultSchema` with asset-level
        items containing ``path``, ``checksum_sha1``, ``size``, and
        ``content_type`` fields.
    """
    logger.info(
        "Asset search: q=%r, repository=%r, page=%d",
        args.get("q"),
        args.get("repository"),
        args.get("page", 1),
    )

    service: SearchService = _get_search_service()

    try:
        result: Dict[str, Any] = service.search_assets(
            q=args.get("q"),
            format=args.get("format"),
            repository=args.get("repository"),
            namespace=args.get("group"),
            name=args.get("name"),
            version=args.get("version"),
            page=args.get("page", 1),
            page_size=args.get("page_size", 50),
            sort_by=args.get("sort", "name"),
            sort_order=args.get("direction", "asc"),
        )
    except Exception as exc:
        logger.error("Asset search failed: %s", exc, exc_info=True)
        result = {
            "items": [],
            "total_count": 0,
            "page": args.get("page", 1),
            "page_size": args.get("page_size", 50),
            "facets": {},
        }

    response: Dict[str, Any] = {
        "items": result.get("items", []),
        "total_count": result.get("total_count", 0),
        "page": result.get("page", args.get("page", 1)),
        "page_size": result.get("page_size", args.get("page_size", 50)),
        "continuation_token": result.get("continuation_token"),
        "facets": result.get("facets"),
        "sort": args.get("sort", "name"),
        "direction": args.get("direction", "asc"),
    }

    logger.debug(
        "Asset search returned %d items (total=%d)",
        len(response["items"]),
        response["total_count"],
    )

    return response


# ===========================================================================
# Endpoint 3: Search Assets by SHA1 — GET /api/v1/search/assets/sha1/<sha1>
# ===========================================================================


@search_bp.route("/assets/sha1/<string:sha1>", methods=["GET"])
@search_bp.response(200, SearchResultSchema)
@search_bp.doc(
    summary="Find assets by SHA-1 checksum",
    description=(
        "Locate assets matching an exact SHA-1 hex digest.  Useful for "
        "build tool integration — verifying artifact integrity by checksum "
        "before download.  Returns all assets with the matching checksum "
        "across all repositories."
    ),
)
@login_required
def search_assets_by_sha1(sha1: str) -> Dict[str, Any]:
    """Find assets matching an exact SHA-1 checksum.

    Performs a targeted search using the SHA-1 hash as a filter.  This
    endpoint is critical for build-tool integration where clients verify
    artifact integrity by querying the repository manager for an artifact
    with a known checksum.

    Args:
        sha1: The 40-character hexadecimal SHA-1 digest to search for.

    Returns:
        A dictionary matching :class:`SearchResultSchema` containing all
        assets with the given SHA-1 checksum.
    """
    # Validate SHA-1 format (40-char hex string).
    sha1_clean: str = sha1.strip().lower()

    if len(sha1_clean) != 40:
        logger.warning(
            "SHA1 search rejected: invalid length %d (expected 40)",
            len(sha1_clean),
        )
        abort(
            400,
            message=(
                f"Invalid SHA-1 checksum: expected 40 hexadecimal characters, "
                f"received {len(sha1_clean)}"
            ),
        )

    try:
        int(sha1_clean, 16)
    except ValueError:
        logger.warning("SHA1 search rejected: non-hex characters in '%s'", sha1_clean)
        abort(
            400,
            message="Invalid SHA-1 checksum: must contain only hexadecimal characters",
        )

    logger.info("SHA1 asset search: sha1=%s", sha1_clean)

    service: SearchService = _get_search_service()

    # Use the general search_assets method with the SHA1 as the query.
    # The SearchService SQL fallback handles this via Asset.checksum_sha1.
    try:
        # Attempt a direct database query for SHA1 — this is the most
        # efficient path and doesn't require Elasticsearch.
        from src.app.models.asset import Asset

        assets = Asset.query.filter_by(checksum_sha1=sha1_clean).all()

        items: list[Dict[str, Any]] = []
        for asset in assets:
            item: Dict[str, Any] = {
                "id": asset.id,
                "path": asset.path,
                "content_type": getattr(asset, "content_type", None),
                "checksum_sha1": getattr(asset, "checksum_sha1", None),
                "checksum_sha256": getattr(asset, "checksum_sha256", None),
                "size": getattr(asset, "size", None),
                "repository": getattr(asset, "repository_name", None),
            }
            items.append(item)

        result: Dict[str, Any] = {
            "items": items,
            "total_count": len(items),
            "page": 1,
            "page_size": len(items) or 50,
            "facets": {},
        }
    except Exception as exc:
        logger.error("SHA1 search failed: %s", exc, exc_info=True)
        result = {
            "items": [],
            "total_count": 0,
            "page": 1,
            "page_size": 50,
            "facets": {},
        }

    response: Dict[str, Any] = {
        "items": result.get("items", []),
        "total_count": result.get("total_count", 0),
        "page": 1,
        "page_size": result.get("page_size", 50),
        "continuation_token": None,
        "facets": result.get("facets"),
        "sort": "name",
        "direction": "asc",
    }

    logger.debug(
        "SHA1 search returned %d assets for sha1=%s",
        response["total_count"],
        sha1_clean,
    )

    return response


# ===========================================================================
# Endpoint 4: Browse Tree — GET /api/v1/search/browse/<repository_name>
# ===========================================================================


@search_bp.route("/browse/<string:repository_name>", methods=["GET"])
@search_bp.doc(
    summary="Browse repository content tree",
    description=(
        "Hierarchical folder-like browsing of repository contents "
        "(Feature F-104).  Returns directories and files at the specified "
        "path within the given repository.  Use ``path`` query parameter "
        "to navigate deeper into the tree."
    ),
)
@login_required
@require_repository_permission("read")
def browse_repository(repository_name: str) -> Dict[str, Any]:
    """Browse the content tree of a repository.

    Provides a hierarchical folder-like view of repository contents,
    implementing Feature F-104 (Browse Tree Navigation).  The endpoint
    reconstructs a virtual directory structure from the flat asset paths
    stored in the database.

    Args:
        repository_name: The name of the repository to browse.

    Query Parameters:
        path: The current browsing path (default ``"/"``).  Must start
            with ``/``.

    Returns:
        A dictionary containing:

        - ``repository`` — the repository name
        - ``path`` — the current browsing path
        - ``entries`` — list of folder and file entries at this path
        - ``total_count`` — number of entries at this path level
    """
    from flask import request

    browse_path: str = request.args.get("path", "/")

    # Normalise path — ensure it starts with "/".
    if not browse_path.startswith("/"):
        browse_path = "/" + browse_path

    logger.info(
        "Browse tree: repository=%r, path=%r",
        repository_name,
        browse_path,
    )

    # Validate repository exists.
    try:
        from src.app.models.repository import Repository

        repo = Repository.query.filter_by(name=repository_name).first()
        if repo is None:
            logger.warning(
                "Browse tree 404: repository %r not found",
                repository_name,
            )
            abort(404, message=f"Repository '{repository_name}' not found")
    except Exception as exc:
        if hasattr(exc, "code"):
            raise
        logger.error("Repository lookup failed: %s", exc)
        abort(404, message=f"Repository '{repository_name}' not found")

    # Retrieve all assets for this repository to build the tree.
    try:
        from src.app.models.asset import Asset

        assets_query = Asset.query.filter_by(repository_name=repository_name)

        # Optimise by filtering assets whose path starts with browse_path.
        if browse_path != "/":
            assets_query = assets_query.filter(
                Asset.path.like(f"{browse_path}%")
            )

        raw_assets = assets_query.all()

        # Convert to dictionaries for the browse tree builder.
        asset_dicts: list[Dict[str, Any]] = []
        for asset in raw_assets:
            asset_dicts.append(
                {
                    "path": asset.path,
                    "content_type": getattr(asset, "content_type", None),
                    "size": getattr(asset, "size", None),
                    "checksum_sha1": getattr(asset, "checksum_sha1", None),
                }
            )

        entries: list[Dict[str, Any]] = _build_browse_entries(
            asset_dicts, browse_path
        )

    except Exception as exc:
        logger.error(
            "Browse tree asset retrieval failed for repo=%r, path=%r: %s",
            repository_name,
            browse_path,
            exc,
        )
        entries = []

    response: Dict[str, Any] = {
        "repository": repository_name,
        "path": browse_path,
        "entries": entries,
        "total_count": len(entries),
    }

    logger.debug(
        "Browse tree returned %d entries for repo=%r, path=%r",
        len(entries),
        repository_name,
        browse_path,
    )

    return response


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------
logger.debug(
    "Search API blueprint loaded — url_prefix=%s, exports=%s",
    search_bp.url_prefix,
    ", ".join(__all__),
)
