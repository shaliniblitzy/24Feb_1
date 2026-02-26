"""
Asset Operations REST API Blueprint.

Implements Feature F-501-RQ-002 (Asset Management) — handles asset listing,
metadata retrieval, binary download, and deletion operations.  Assets are the
binary blobs (JARs, tarballs, Docker layers, wheel files, etc.) stored in
BlobStores and tracked in the database.

**Architecture Context:**

Replaces JAX-RS resource classes from the original Java source system
(RESTEasy 6.2.7 + Swagger/OpenAPI 2.2.20) with a Flask-smorest Blueprint
that provides:

- Declarative route definitions with ``@bp.route()``
- Marshmallow schema–driven request/response serialization
- Automatic OpenAPI 3.x documentation generation
- HTTP error responses via ``abort()``

**Feature Coverage:**

+-------+--------------------------------------+-------------------------------+
| ID    | Feature Name                         | Asset API Role                |
+=======+======================================+===============================+
| F-101 | Multi-Format Repository Support      | Serves assets for all formats |
+-------+--------------------------------------+-------------------------------+
| F-103 | Content Indexing and Search           | Asset metadata retrieval      |
+-------+--------------------------------------+-------------------------------+
| F-104 | Browse Tree Navigation               | Path-prefix asset filtering   |
+-------+--------------------------------------+-------------------------------+
| F-204 | Cleanup Policies                     | last_downloaded tracking      |
+-------+--------------------------------------+-------------------------------+
| F-303 | Audit Logging                        | ASSET_DOWNLOADED events       |
+-------+--------------------------------------+-------------------------------+
| F-501 | REST API                             | Asset management endpoints    |
+-------+--------------------------------------+-------------------------------+
| F-503 | Webhook Integration                  | Download event dispatch       |
+-------+--------------------------------------+-------------------------------+

**Endpoints:**

- ``GET  /api/v1/assets/``                     — List assets (paginated)
- ``GET  /api/v1/assets/<asset_id>``           — Get asset metadata
- ``GET  /api/v1/assets/<asset_id>/download``  — Download binary content
- ``DELETE /api/v1/assets/<asset_id>``         — Delete asset and blob

**Performance Targets (AAP Section 0.7.3):**

- REST API response time: < 500 ms average
- Cached artifact resolution: < 200 ms

**Exports:**

- ``assets_bp`` — Flask-smorest Blueprint instance (registered in factory.py)
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from flask import current_app, g, request, Response, send_file
from flask_smorest import Blueprint, abort

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_repository_permission
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.extensions import db
from src.app.models.asset import Asset
from src.app.models.repository import Repository
from src.app.schemas.asset import (
    AssetListQuerySchema,
    AssetResponseSchema,
    AssetSchema,
)
from src.app.services.repository_manager import RepositoryManager

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for all asset API operations — listing,
# downloads, deletions, errors, and performance diagnostics.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------
# Replaces the JAX-RS ``@Path("/api/v1/assets")`` annotation from the Java
# source.  The ``url_prefix`` mirrors the original REST API path contract.
# ---------------------------------------------------------------------------

assets_bp: Blueprint = Blueprint(
    "assets",
    __name__,
    url_prefix="/api/v1/assets",
    description="Asset download, metadata, and management (F-501-RQ-002)",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CONTENT_TYPE: str = "application/octet-stream"
"""Fallback MIME type when an asset has no recorded content_type."""

_STREAMING_THRESHOLD_BYTES: int = 10 * 1024 * 1024  # 10 MiB
"""Assets larger than this threshold are streamed in chunks to avoid
loading the entire blob into memory.  Matches the AAP guidance for
streaming responses for large artifacts (> 10 MB)."""

_STREAM_CHUNK_SIZE: int = 8192  # 8 KiB
"""Chunk size for streaming large asset downloads."""

_DEFAULT_PAGE_SIZE: int = 50
"""Default number of assets per page when ``page_size`` is omitted."""

_MAX_PAGE_SIZE: int = 200
"""Hard upper limit for ``page_size`` to prevent excessive memory usage."""


# ===========================================================================
# Internal Helper Functions
# ===========================================================================


def _get_asset_or_404(asset_id: int) -> Asset:
    """Retrieve an asset by its primary key or abort with HTTP 404.

    Uses ``Asset.query.get()`` for efficient primary-key lookup.

    Args:
        asset_id: The asset's auto-incremented BigInteger primary key.

    Returns:
        The :class:`Asset` instance.

    Raises:
        werkzeug.exceptions.NotFound: If no asset with the given ID exists.
    """
    asset: Optional[Asset] = db.session.get(Asset, asset_id)
    if asset is None:
        logger.debug("Asset %d not found.", asset_id)
        abort(404, message=f"Asset {asset_id} not found")
    return asset


def _get_repository_or_404(repository_name: str) -> Repository:
    """Retrieve a repository by name or abort with HTTP 404.

    Args:
        repository_name: Unique repository identifier string.

    Returns:
        The :class:`Repository` instance.

    Raises:
        werkzeug.exceptions.NotFound: If no repository with the given name
        exists.
    """
    repo: Optional[Repository] = Repository.query.filter_by(
        name=repository_name
    ).first()
    if repo is None:
        logger.debug("Repository '%s' not found.", repository_name)
        abort(404, message=f"Repository '{repository_name}' not found")
    return repo


def _check_repository_online(repo: Repository) -> None:
    """Abort with HTTP 409 Conflict if the repository is offline.

    Offline repositories reject read and write operations until an
    administrator brings them back online via the system management API.

    Args:
        repo: The :class:`Repository` instance to check.

    Raises:
        werkzeug.exceptions.Conflict: If ``repo.online`` is ``False``.
    """
    if not repo.online:
        logger.debug("Repository '%s' is offline.", repo.name)
        abort(409, message=f"Repository '{repo.name}' is currently offline")


def _check_asset_repository_permission(
    repository_name: str,
    action: str,
) -> None:
    """Check repository-scoped RBAC permission for an asset operation.

    Asset API endpoints resolve ``repository_name`` from the asset record
    (via ``asset.repository_name``), **not** from URL path parameters.
    The :func:`require_repository_permission` decorator extracts
    ``repository_name`` from ``kwargs``, which is not available for
    ``/assets/<asset_id>``-style routes.  This helper performs the
    **equivalent** three-tier RBAC evaluation (system-wide →
    repository-scoped → CSEL) by instantiating the same
    :class:`AuthorizationEngine` used by the decorator.

    Args:
        repository_name: Name of the repository that owns the asset.
        action: The permission action to check (e.g., ``'read'``,
            ``'delete'``).

    Raises:
        werkzeug.exceptions.Unauthorized: If no authenticated user is
            present on ``g.current_user``.
        werkzeug.exceptions.Forbidden: If the user lacks the required
            privilege for the target repository.
    """
    user = getattr(g, "current_user", None)
    if user is None:
        abort(401, message="Authentication required")

    # Lazy import — reuses the same AuthorizationEngine that powers
    # require_repository_permission, avoiding circular imports at
    # module level.
    from src.app.auth.authorization import AuthorizationEngine

    engine: AuthorizationEngine = AuthorizationEngine()
    if not engine.check_repository_privilege(
        user, repository_name, "*", action
    ):
        logger.warning(
            "Repository permission denied: user='%s', repo='%s', "
            "action='%s'.",
            user.user_id,
            repository_name,
            action,
        )
        abort(
            403,
            message=(
                f"Insufficient repository privileges: "
                f"{repository_name}:{action}"
            ),
        )


def _verify_asset_in_repository(
    asset_id: int,
    repository_name: str,
) -> Optional[Asset]:
    """Verify an asset belongs to a specific repository.

    Uses ``Asset.query.filter_by()`` to perform a scoped lookup that
    ensures the asset exists AND belongs to the given repository.

    Args:
        asset_id: Asset primary key.
        repository_name: Expected owning repository name.

    Returns:
        The :class:`Asset` if found and owned by the repository,
        or ``None`` otherwise.
    """
    return Asset.query.filter_by(
        id=asset_id, repository_name=repository_name
    ).first()


def _resolve_blob_content(
    asset: Asset,
) -> tuple[Any, str, str]:
    """Resolve binary content for an asset from its BlobStore reference.

    Locates and opens the binary file referenced by ``asset.blob_ref``.
    For File BlobStore (F-201) the reference resolves to a local
    filesystem path; for S3 BlobStore (F-202) it would resolve to an S3
    object key (delegated to the appropriate storage backend).

    The function checks both absolute paths and paths relative to the
    configured ``BLOBSTORE_ROOT`` directory.

    Args:
        asset: The :class:`Asset` instance whose content to resolve.

    Returns:
        A 3-tuple ``(content_stream, content_type, filename)``:
        - ``content_stream``: An open file-like object positioned at the
          beginning of the blob.
        - ``content_type``: MIME type for the ``Content-Type`` header.
        - ``filename``: Suggested download filename (basename of path).

    Raises:
        werkzeug.exceptions.NotFound: If ``blob_ref`` is ``None`` or the
        referenced file does not exist on the filesystem.
    """
    content_type: str = asset.content_type or _DEFAULT_CONTENT_TYPE
    filename: str = (
        asset.path.rsplit("/", 1)[-1] if asset.path else "download"
    )

    if not asset.blob_ref:
        logger.warning(
            "Asset %d has no blob_ref — content not available.",
            asset.id,
        )
        abort(
            404,
            message="Asset binary content not available (no blob reference)",
        )

    blob_ref: str = asset.blob_ref

    # Attempt 1: blob_ref is an absolute path
    if os.path.isabs(blob_ref) and os.path.isfile(blob_ref):
        logger.debug(
            "Resolved asset %d blob via absolute path: %s",
            asset.id,
            blob_ref,
        )
        return open(blob_ref, "rb"), content_type, filename  # noqa: SIM115

    # Attempt 2: blob_ref relative to configured BlobStore root
    blob_store_root: str = current_app.config.get(
        "BLOBSTORE_ROOT",
        os.path.join(current_app.instance_path, "blobs"),
    )
    full_path: str = os.path.join(blob_store_root, blob_ref)

    if os.path.isfile(full_path):
        logger.debug(
            "Resolved asset %d blob via BLOBSTORE_ROOT: %s",
            asset.id,
            full_path,
        )
        return open(full_path, "rb"), content_type, filename  # noqa: SIM115

    logger.warning(
        "Blob content not found for asset %d: blob_ref='%s', "
        "tried paths: absolute='%s', relative='%s'.",
        asset.id,
        blob_ref,
        blob_ref,
        full_path,
    )
    abort(
        404,
        message=(
            f"Asset binary content not found at storage reference: "
            f"{blob_ref}"
        ),
    )


def _stream_file_chunks(stream: Any, chunk_size: int = _STREAM_CHUNK_SIZE):
    """Generator that yields fixed-size chunks from a binary stream.

    Used for streaming large artifacts (> 10 MiB) to avoid loading the
    entire blob into memory.  Closes the stream automatically after
    exhaustion or on error.

    Args:
        stream: A readable file-like object supporting ``.read(n)``.
        chunk_size: Number of bytes per chunk (default 8 KiB).

    Yields:
        ``bytes`` chunks of up to ``chunk_size`` bytes.
    """
    try:
        while True:
            chunk: bytes = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        if hasattr(stream, "close"):
            stream.close()


# ===========================================================================
# API Endpoints
# ===========================================================================


# ---------------------------------------------------------------------------
# GET /api/v1/assets/ — List Assets (paginated, filterable)
# ---------------------------------------------------------------------------

@assets_bp.route("/", methods=["GET"])
@assets_bp.arguments(AssetListQuerySchema, location="query")
@assets_bp.response(200, AssetResponseSchema(many=True))
@login_required
def list_assets(args: dict) -> list[Asset]:
    """List assets with optional filtering and pagination.

    Supports filtering by repository name, path prefix, content type,
    and parent component ID.  Results are ordered by ``path`` for
    deterministic pagination.

    When a ``repository`` filter is provided, the endpoint delegates to
    :meth:`RepositoryManager.get_assets` for service-layer validation
    (repository existence and online status checks).  Without a
    repository filter, a direct database query is used.

    OpenAPI tags: Assets
    """
    repository_name: Optional[str] = args.get("repository")
    path_prefix: Optional[str] = args.get("path_prefix")
    content_type_filter: Optional[str] = args.get("content_type")
    component_id: Optional[int] = args.get("component_id")
    page: int = max(1, args.get("page", 1))
    page_size: int = max(1, min(args.get("page_size", _DEFAULT_PAGE_SIZE), _MAX_PAGE_SIZE))

    # ----- Repository-scoped listing via RepositoryManager -----
    if repository_name:
        try:
            repo_manager: RepositoryManager = RepositoryManager()
            result: dict[str, Any] = repo_manager.get_assets(
                repository_name=repository_name,
                component_id=component_id,
                path=path_prefix,
                page=page,
                page_size=page_size,
            )
            assets: list[Asset] = result.get("items", [])

            # Apply content_type filter post-retrieval (not supported
            # by RepositoryManager.get_assets signature).
            if content_type_filter:
                assets = [
                    a for a in assets
                    if a.content_type == content_type_filter
                ]

            logger.debug(
                "Listed %d assets via RepositoryManager (repo=%s, "
                "page=%d, page_size=%d, total=%d).",
                len(assets),
                repository_name,
                page,
                page_size,
                result.get("total_count", 0),
            )
            return assets

        except Exception as exc:
            # Map service-layer exceptions to HTTP errors
            exc_name: str = type(exc).__name__
            if "NotFound" in exc_name:
                abort(
                    404,
                    message=f"Repository '{repository_name}' not found",
                )
            if "Offline" in exc_name:
                abort(
                    409,
                    message=(
                        f"Repository '{repository_name}' is currently offline"
                    ),
                )
            logger.error(
                "Unexpected error listing assets for repo '%s': %s",
                repository_name,
                str(exc),
                exc_info=True,
            )
            abort(500, message="Internal error listing assets")

    # ----- Global listing (no repository filter) -----
    # Accept optional query parameters directly from request.args for
    # any additional filters not covered by the Marshmallow schema
    # (forward-compatibility for future API enhancements).
    sort_order: Optional[str] = request.args.get("sort", "asc")

    query = Asset.query

    if path_prefix:
        query = query.filter(Asset.path.like(f"{path_prefix}%"))

    if content_type_filter:
        query = query.filter(Asset.content_type == content_type_filter)

    if component_id is not None:
        query = query.filter(Asset.component_id == component_id)

    # Count before pagination
    total_count: int = query.count()
    offset: int = (page - 1) * page_size

    assets = (
        query.order_by(Asset.path)
        .offset(offset)
        .limit(page_size)
        .all()
    )

    logger.debug(
        "Listed %d/%d assets globally (page=%d, page_size=%d, "
        "path_prefix=%s, content_type=%s, component_id=%s).",
        len(assets),
        total_count,
        page,
        page_size,
        path_prefix,
        content_type_filter,
        component_id,
    )

    return assets


# ---------------------------------------------------------------------------
# GET /api/v1/assets/<asset_id> — Get Asset Metadata
# ---------------------------------------------------------------------------

@assets_bp.route("/<int:asset_id>", methods=["GET"])
@assets_bp.response(200, AssetResponseSchema)
@login_required
def get_asset(asset_id: int) -> Asset:
    """Retrieve comprehensive asset metadata by primary key.

    Returns asset metadata including path, checksums (SHA-1, SHA-256,
    MD5), file size, content type, download timestamps, and a computed
    download URL.  **Does not** return binary content — use the
    ``/<asset_id>/download`` endpoint for that.

    OpenAPI tags: Assets

    Args:
        asset_id: Unique asset identifier (BigInteger auto-increment PK).

    Returns:
        Serialized asset metadata via :class:`AssetResponseSchema`.
    """
    asset: Asset = _get_asset_or_404(asset_id)

    # Support conditional requests via If-None-Match header for cache
    # validation based on asset checksum (ETag).
    if_none_match: Optional[str] = request.headers.get("If-None-Match")
    if if_none_match and asset.checksum_sha256:
        etag: str = f'"{asset.checksum_sha256}"'
        if if_none_match == etag:
            # Asset hasn't changed — return 304 Not Modified
            return Response(status=304, headers={"ETag": etag})

    logger.debug(
        "Retrieved asset metadata: id=%d, path=%s, repo=%s, "
        "content_type=%s, size=%s.",
        asset.id,
        asset.path,
        asset.repository_name,
        asset.content_type,
        asset.size,
    )

    return asset


# ---------------------------------------------------------------------------
# GET /api/v1/assets/<asset_id>/download — Download Binary Content
# ---------------------------------------------------------------------------

@assets_bp.route("/<int:asset_id>/download", methods=["GET"])
@login_required
def download_asset(asset_id: int) -> Response:
    """Download the binary content of an asset.

    Retrieves the binary blob from the configured BlobStore backend
    (File BlobStore F-201 or S3 BlobStore F-202) and streams it to the
    client with correct ``Content-Type`` and ``Content-Disposition``
    headers.

    **Side effects:**

    1. Updates ``Asset.last_downloaded`` to the current UTC timestamp,
       which is critical for cleanup policy evaluation (Feature F-204).
    2. Emits an ``ASSET_DOWNLOADED`` event via the Blinker event bus for
       audit logging (Feature F-303) and webhook dispatch (Feature F-503).

    **Performance target:** < 200 ms for cached artifact resolution
    (AAP Section 0.7.3).

    **Streaming:** Artifacts larger than 10 MiB are streamed in 8 KiB
    chunks via a :class:`Response` generator to avoid excessive memory
    consumption.  Smaller artifacts use Flask's ``send_file()`` for
    optimal performance.

    OpenAPI tags: Assets

    Args:
        asset_id: Unique asset identifier (BigInteger auto-increment PK).

    Returns:
        Binary file response with ``Content-Type`` matching the asset's
        recorded MIME type and ``Content-Disposition: attachment``.
    """
    # Step 1: Look up the asset record (PK lookup)
    asset: Asset = _get_asset_or_404(asset_id)

    # Step 2: Validate repository existence and online status
    repo: Repository = _get_repository_or_404(asset.repository_name)
    _check_repository_online(repo)

    # Log the BlobStore context for diagnostic tracing
    current_app.logger.debug(
        "Download request for asset %d in repo '%s' (blob_store='%s').",
        asset_id,
        repo.name,
        repo.blob_store_name,
    )

    # Step 3: Authorization — require read permission on the asset's
    #   repository.  Equivalent to @require_repository_permission('read')
    #   but with repository_name resolved from the asset record.
    _check_asset_repository_permission(asset.repository_name, "read")

    # Step 4: Validated retrieval via RepositoryManager service layer
    repo_manager: RepositoryManager = RepositoryManager()
    validated_asset: Optional[Asset] = repo_manager.get_asset(
        asset.repository_name, asset_id
    )
    if validated_asset is None:
        abort(404, message="Asset not found in repository context")

    # Step 5: Resolve binary content from BlobStore
    content_stream, content_type, filename = _resolve_blob_content(
        validated_asset
    )

    # Step 6: Update last_downloaded timestamp (F-204 cleanup tracking)
    try:
        validated_asset.record_download()
    except Exception:
        # Fallback: direct session manipulation if record_download() fails
        try:
            validated_asset.last_downloaded = datetime.now(timezone.utc)
            db.session.add(validated_asset)
            db.session.commit()
        except Exception as ts_exc:
            # Non-fatal: timestamp failure must not block the download
            logger.warning(
                "Failed to update last_downloaded for asset %d: %s",
                asset_id,
                str(ts_exc),
            )

    # Step 7: Emit ASSET_DOWNLOADED event for audit (F-303) and webhooks (F-503)
    user = getattr(g, "current_user", None)
    user_id: Optional[str] = (
        getattr(user, "user_id", None) if user else None
    )

    emit_event(
        EventType.ASSET_DOWNLOADED,
        payload={
            "repository_name": validated_asset.repository_name,
            "asset_path": validated_asset.path,
            "user_id": user_id,
            "asset_id": validated_asset.id,
            "content_type": validated_asset.content_type,
            "size": validated_asset.size,
        },
    )

    logger.info(
        "Asset downloaded: id=%d, path=%s, repo=%s, user=%s, size=%s.",
        asset_id,
        validated_asset.path,
        validated_asset.repository_name,
        user_id or "anonymous",
        validated_asset.size,
    )

    # Step 8: Return binary response — stream large files, send_file for small
    if (
        validated_asset.size is not None
        and validated_asset.size > _STREAMING_THRESHOLD_BYTES
    ):
        # Streaming response for large artifacts (> 10 MiB)
        return Response(
            _stream_file_chunks(content_stream),
            mimetype=content_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                ),
                "Content-Length": str(validated_asset.size),
            },
        )

    # Standard response for smaller artifacts (leverages Flask optimisations)
    return send_file(
        content_stream,
        mimetype=content_type,
        as_attachment=True,
        download_name=filename,
    )


# ---------------------------------------------------------------------------
# DELETE /api/v1/assets/<asset_id> — Delete Asset and Blob
# ---------------------------------------------------------------------------

@assets_bp.route("/<int:asset_id>", methods=["DELETE"])
@assets_bp.response(204)
@login_required
def delete_asset(asset_id: int) -> None:
    """Delete an asset record and its binary blob from BlobStore.

    Removes both the database record and the associated binary object
    from the BlobStore.  If the parent component has no remaining assets
    after this deletion, the orphaned component is also cleaned up
    automatically by :meth:`RepositoryManager.delete_asset`.

    Requires ``delete`` permission on the asset's repository.

    OpenAPI tags: Assets

    Args:
        asset_id: Unique asset identifier (BigInteger auto-increment PK).

    Returns:
        ``204 No Content`` on success.
    """
    # Step 1: Look up the asset record
    asset: Asset = _get_asset_or_404(asset_id)

    # Step 2: Authorization — require delete permission on repository.
    #   Equivalent to @require_repository_permission('delete') but with
    #   repository_name resolved from the asset record.
    _check_asset_repository_permission(asset.repository_name, "delete")

    # Step 3: Capture metadata for logging before deletion
    asset_path: str = asset.path
    repository_name: str = asset.repository_name
    user = getattr(g, "current_user", None)
    user_id: Optional[str] = (
        getattr(user, "user_id", None) if user else None
    )

    # Step 4: Delete via RepositoryManager (handles DB + BlobStore + orphan cleanup)
    try:
        repo_manager: RepositoryManager = RepositoryManager()
        repo_manager.delete_asset(repository_name, asset_id)
    except Exception as exc:
        exc_name: str = type(exc).__name__
        if "NotFound" in exc_name:
            # The repository or asset was already removed — attempt
            # direct model deletion as a cleanup fallback.
            try:
                asset.delete()
                logger.info(
                    "Cleaned up orphaned asset %d via direct deletion.",
                    asset_id,
                )
            except Exception as del_exc:
                logger.error(
                    "Failed to delete orphaned asset %d: %s",
                    asset_id,
                    str(del_exc),
                )
                abort(500, message="Failed to delete asset")
        else:
            logger.error(
                "Failed to delete asset %d in repo '%s': %s",
                asset_id,
                repository_name,
                str(exc),
                exc_info=True,
            )
            abort(500, message="Failed to delete asset")

    logger.info(
        "Asset deleted: id=%d, path=%s, repo=%s, user=%s.",
        asset_id,
        asset_path,
        repository_name,
        user_id or "anonymous",
    )

    return None


# ---------------------------------------------------------------------------
# Module Initialization Logging
# ---------------------------------------------------------------------------

logger.debug(
    "Assets API blueprint module loaded — url_prefix='/api/v1/assets'."
)
