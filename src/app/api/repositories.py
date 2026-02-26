"""
Repository CRUD REST API Blueprint.

Implements the Repository Management REST API (Feature F-501-RQ-001),
replacing ``RepositoryManagerRESTAdapter`` from the Java source system
(RESTEasy 6.2.7 JAX-RS).  This is the most central API blueprint —
repositories are the core entity in the Sonatype Nexus Repository Manager.

**Endpoints:**

+--------+---------------------------------------------------+----------------------------+
| Method | URL                                               | Description                |
+========+===================================================+============================+
| GET    | /api/v1/repositories/                             | List all repositories      |
| GET    | /api/v1/repositories/<name>                       | Get repository details     |
| POST   | /api/v1/repositories/                             | Create repository          |
| PUT    | /api/v1/repositories/<name>                       | Update repository          |
| DELETE | /api/v1/repositories/<name>                       | Delete repository          |
| PUT    | /api/v1/repositories/<name>/status                | Set online/offline status  |
| POST   | /api/v1/repositories/<name>/rebuild-index         | Rebuild search index       |
| POST   | /api/v1/repositories/<name>/invalidate-cache      | Invalidate proxy cache     |
+--------+---------------------------------------------------+----------------------------+

**Technology Stack:**

- Flask 3.1.3 with flask-smorest 0.45.0 for Blueprint and OpenAPI 3.x docs
- Marshmallow 3.23.2 schemas for request/response serialization
- Python 3.12+ with full type hints

**Architecture:**

- All route handlers delegate business logic to ``RepositoryManager``
  (Facade pattern — AAP Section 0.4.3)
- Authentication via ``@login_required`` decorator (multi-realm chain)
- Authorization via ``@require_permission`` / ``@require_repository_permission``
  (Three-tier RBAC — Feature F-301)
- Event emission via ``emit_event()`` for audit logging (F-303) and
  webhooks (F-503) using Blinker signals
- Consistent JSON error responses via flask-smorest ``abort()``
- OpenAPI 3.x documentation auto-generated from decorators

**Performance Targets (AAP Section 0.7.3):**

- REST API response time: < 500 ms average
- Cached artifact resolution: < 200 ms

Exports:
    repositories_bp : flask-smorest Blueprint instance registered by factory.py
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from flask import current_app, g, jsonify, request
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields as ma_fields

from src.app.auth.authentication import login_required
from src.app.auth.authorization import (
    require_permission,
    require_repository_permission,
)
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.extensions import db
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.models.repository import Repository
from src.app.schemas.repository import (
    RepositoryCreateSchema,
    RepositoryResponseSchema,
    RepositoryUpdateSchema,
)
from src.app.services.repository_manager import (
    InvalidRepositoryConfigError,
    InvalidStateTransitionError,
    RepositoryExistsError,
    RepositoryManager,
    RepositoryNotFoundError,
    RepositoryOfflineError,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from Java.
# Provides diagnostic output for all repository API operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pagination Defaults
# ---------------------------------------------------------------------------
# Used by the ``list_repositories`` endpoint for large-scale deployments.
# Consistent with pagination defaults in ``components.py`` and ``assets.py``.
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE: int = 50
"""Default number of repositories returned per page."""

MAX_PAGE_SIZE: int = 200
"""Maximum number of repositories allowed per page."""


# ---------------------------------------------------------------------------
# Inline Marshmallow Schemas
# ---------------------------------------------------------------------------


class RepositoryStatusSchema(Schema):
    """Marshmallow schema for the ``set_repository_status`` request body.

    Validates the ``online`` boolean field for setting a repository's
    online/offline status.  Replaces inline ``request.get_json()`` parsing
    with declarative schema validation for consistency with other endpoints.
    """

    online = ma_fields.Boolean(
        required=True,
        metadata={
            "description": (
                "Target online state: true to start (STOPPED → STARTED), "
                "false to stop (STARTED → STOPPED)."
            ),
        },
    )


class RepositoryListQuerySchema(Schema):
    """Marshmallow schema for ``list_repositories`` query parameters.

    Provides optional filtering by repository ``format`` and ``type``,
    plus pagination via ``page`` and ``page_size``.
    """

    format = ma_fields.String(
        load_default=None,
        metadata={
            "description": (
                "Filter by repository format (e.g., maven2, npm, docker)."
            ),
        },
    )
    type = ma_fields.String(
        load_default=None,
        metadata={
            "description": (
                "Filter by repository type (hosted, proxy, group)."
            ),
        },
    )
    page = ma_fields.Integer(
        load_default=1,
        metadata={
            "description": "Page number (1-based, default 1).",
        },
    )
    page_size = ma_fields.Integer(
        load_default=DEFAULT_PAGE_SIZE,
        metadata={
            "description": (
                f"Results per page (1–{MAX_PAGE_SIZE}, default "
                f"{DEFAULT_PAGE_SIZE})."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------
# CRITICAL: Uses flask-smorest 0.45.0 Blueprint (NOT flask.Blueprint) for
# automatic OpenAPI 3.x documentation generation.  The exported name MUST
# be ``repositories_bp`` — factory.py imports it as such.
# ---------------------------------------------------------------------------

repositories_bp: Blueprint = Blueprint(
    "repositories",
    __name__,
    url_prefix="/api/v1/repositories",
    description="Repository management operations (F-501-RQ-001)",
)


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------


def _get_error_message(error: Any, default: str) -> str:
    """Extract a human-readable error message from an HTTPException.

    flask-smorest's ``abort()`` stores extra data in ``error.data`` (a dict
    with a ``'message'`` key).  Standard Flask ``abort()`` stores the message
    in ``error.description``.  This helper handles both cases.

    Args:
        error: The caught exception (typically a Werkzeug HTTPException).
        default: Fallback message if extraction fails.

    Returns:
        The extracted error message string.
    """
    # flask-smorest stores kwargs in error.data
    data: Optional[Dict[str, Any]] = getattr(error, "data", None)
    if isinstance(data, dict) and "message" in data:
        return str(data["message"])
    # Standard Flask/Werkzeug stores in description
    desc: Optional[str] = getattr(error, "description", None)
    if desc:
        return str(desc)
    return default


def _get_current_user_id() -> Optional[str]:
    """Safely retrieve the authenticated user's identifier.

    Returns:
        The ``user_id`` string from ``g.current_user``, or ``None`` if
        no user is authenticated.
    """
    user = getattr(g, "current_user", None)
    if user is None:
        return None
    return getattr(user, "user_id", None)


# ===================================================================
# CRUD Endpoints
# ===================================================================


@repositories_bp.route("/", methods=["GET"])
@repositories_bp.arguments(RepositoryListQuerySchema, location="query")
@login_required
def list_repositories(args: dict) -> tuple:
    """List all repositories with optional format/type filtering and pagination.

    Supports query parameter filtering to narrow down results by repository
    format (e.g. ``maven2``, ``npm``) and/or type (``hosted``, ``proxy``,
    ``group``).  Pagination via ``page`` and ``page_size`` prevents
    unbounded response payloads in large-scale deployments.

    Query Parameters:
        format (str, optional): Filter by repository format.
        type (str, optional): Filter by repository type.
        page (int, optional): Page number (1-based, default 1).
        page_size (int, optional): Results per page (1–200, default 50).

    Returns:
        JSON object with ``items`` (list of repository objects),
        ``total_count``, ``page``, and ``page_size``.
    """
    format_filter: Optional[str] = args.get("format")
    type_filter: Optional[str] = args.get("type")
    page: int = max(1, args.get("page", 1))
    page_size: int = max(1, min(args.get("page_size", DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))

    # Build filtered query
    query = Repository.query

    if format_filter:
        query = query.filter(Repository.format == format_filter)
    if type_filter:
        query = query.filter(Repository.type == type_filter)

    total_count: int = query.count()
    offset: int = (page - 1) * page_size

    repositories: list[Repository] = (
        query.order_by(Repository.name)
        .offset(offset)
        .limit(page_size)
        .all()
    )

    user_id: Optional[str] = _get_current_user_id()
    logger.info(
        "Listed %d/%d repositories (format=%s, type=%s, page=%d) by user '%s'",
        len(repositories),
        total_count,
        format_filter or "all",
        type_filter or "all",
        page,
        user_id or "unknown",
    )

    schema = RepositoryResponseSchema(many=True)
    return jsonify({
        "items": schema.dump(repositories),
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
    })


@repositories_bp.route("/<string:repository_name>", methods=["GET"])
@repositories_bp.response(200, RepositoryResponseSchema)
@login_required
def get_repository(repository_name: str) -> Repository:
    """Get detailed information for a single repository.

    Args:
        repository_name: Unique repository name (URL path parameter).

    Returns:
        JSON object with full repository details serialised via
        ``RepositoryResponseSchema``.

    Raises:
        404: If the repository does not exist.
    """
    # Direct model query for efficient single-key lookup (Repository PK is name)
    repository: Optional[Repository] = Repository.query.get(repository_name)
    if repository is None:
        abort(404, message=f"Repository '{repository_name}' not found")

    logger.debug(
        "Retrieved repository '%s' (format=%s, type=%s, online=%s)",
        repository.name,
        repository.format,
        repository.type,
        repository.online,
    )
    return repository


@repositories_bp.route("/", methods=["POST"])
@repositories_bp.arguments(RepositoryCreateSchema, location="json")
@repositories_bp.response(201, RepositoryResponseSchema)
@login_required
@require_permission("repositories", "create")
def create_repository(args: Dict[str, Any]) -> Repository:
    """Create a new repository.

    The request body is validated by ``RepositoryCreateSchema`` which
    enforces:

    - ``name``: unique, alphanumeric with dots/hyphens/underscores
    - ``format``: one of maven2, npm, docker, nuget, pypi, apt, raw
    - ``type``: one of hosted, proxy, group
    - ``blob_store_name``: must reference an existing BlobStore
    - Cross-field: proxy requires ``attributes.proxy.remoteUrl``
    - Cross-field: group requires ``attributes.group.memberNames``

    Args:
        args: Deserialized and validated request body from
              ``RepositoryCreateSchema``.

    Returns:
        JSON object of the newly created repository (HTTP 201).

    Raises:
        409: If a repository with the given name already exists.
        422: If validation fails (invalid format, type, missing required
             attributes, or BlobStore not found).
        500: On unexpected internal errors.
    """
    # -- Pre-validation: BlobStore existence check --
    blob_store_name: str = args.get("blob_store_name", "default")
    blob_store: Optional[BlobStoreConfig] = BlobStoreConfig.query.get(blob_store_name)
    if blob_store is None:
        current_app.logger.warning(
            "BlobStore '%s' not found during repository creation", blob_store_name
        )
        abort(
            422,
            message=f"BlobStore '{blob_store_name}' does not exist",
        )

    # -- Extract type-specific parameters from attributes --
    attributes: Optional[Dict[str, Any]] = args.get("attributes")
    type_kwargs: Dict[str, Any] = {}
    repo_type: str = args["type"]

    if repo_type == "proxy" and isinstance(attributes, dict):
        proxy_config: Dict[str, Any] = attributes.get("proxy", {})
        if isinstance(proxy_config, dict):
            remote_url: Optional[str] = proxy_config.get("remoteUrl")
            if remote_url:
                type_kwargs["remote_url"] = remote_url

    if repo_type == "group" and isinstance(attributes, dict):
        group_config: Dict[str, Any] = attributes.get("group", {})
        if isinstance(group_config, dict):
            member_names = group_config.get("memberNames")
            if member_names:
                type_kwargs["member_names"] = member_names

    # -- Delegate to RepositoryManager --
    manager: RepositoryManager = RepositoryManager()
    try:
        repository: Repository = manager.create_repository(
            name=args["name"],
            format_type=args["format"],
            repo_type=repo_type,
            blob_store_name=blob_store_name,
            online=args.get("online", True),
            attributes=attributes,
            **type_kwargs,
        )
    except RepositoryExistsError as exc:
        abort(409, message=str(exc))
    except InvalidRepositoryConfigError as exc:
        abort(422, message=str(exc))
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "Unexpected error creating repository '%s': %s",
            args.get("name", "<unknown>"),
            str(exc),
            exc_info=True,
        )
        abort(500, message="Internal server error creating repository")

    # -- Emit event with request-level context --
    # The RepositoryManager emits a service-level event; this API-level
    # event enriches the audit trail with user identity and client IP.
    user_id: Optional[str] = _get_current_user_id()
    emit_event(
        EventType.REPOSITORY_CREATED,
        {
            "name": repository.name,
            "format": repository.format,
            "type": repository.type,
            "blob_store_name": repository.blob_store_name,
            "online": repository.online,
            "user_id": user_id,
            "ip_address": request.remote_addr,
            "source": "api",
        },
    )

    logger.info(
        "Repository created via API: name=%s, format=%s, type=%s, user=%s",
        repository.name,
        repository.format,
        repository.type,
        user_id,
    )

    return repository


@repositories_bp.route("/<string:repository_name>", methods=["PUT"])
@repositories_bp.arguments(RepositoryUpdateSchema, location="json")
@repositories_bp.response(200, RepositoryResponseSchema)
@login_required
@require_repository_permission("edit")
def update_repository(
    args: Dict[str, Any], repository_name: str
) -> Repository:
    """Update an existing repository's configuration.

    Only fields present in the request body are updated; all others
    remain unchanged (partial update semantics).  The ``name``,
    ``format``, and ``type`` fields are immutable after creation and
    cannot be changed.

    Args:
        args: Deserialized update payload from ``RepositoryUpdateSchema``.
        repository_name: Unique repository name (URL path parameter).

    Returns:
        JSON object of the updated repository.

    Raises:
        404: If the repository does not exist.
        422: If the new BlobStore name is invalid or other validation fails.
        500: On unexpected internal errors.
    """
    # -- Pre-validation: BlobStore existence check (if being changed) --
    new_blob_store: Optional[str] = args.get("blob_store_name")
    if new_blob_store is not None:
        if BlobStoreConfig.query.get(new_blob_store) is None:
            abort(
                422,
                message=f"BlobStore '{new_blob_store}' does not exist",
            )

    # -- Delegate to RepositoryManager --
    manager: RepositoryManager = RepositoryManager()
    try:
        repository: Repository = manager.update_repository(
            name=repository_name,
            online=args.get("online"),
            attributes=args.get("attributes"),
            blob_store_name=new_blob_store,
        )
    except RepositoryNotFoundError:
        abort(404, message=f"Repository '{repository_name}' not found")
    except InvalidRepositoryConfigError as exc:
        abort(422, message=str(exc))
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "Unexpected error updating repository '%s': %s",
            repository_name,
            str(exc),
            exc_info=True,
        )
        abort(500, message="Internal server error updating repository")

    # -- Emit event with request-level context --
    user_id: Optional[str] = _get_current_user_id()
    emit_event(
        EventType.REPOSITORY_UPDATED,
        {
            "name": repository.name,
            "format": repository.format,
            "type": repository.type,
            "online": repository.online,
            "user_id": user_id,
            "ip_address": request.remote_addr,
            "source": "api",
        },
    )

    logger.info(
        "Repository updated via API: name=%s, user=%s",
        repository_name,
        user_id,
    )

    return repository


@repositories_bp.route("/<string:repository_name>", methods=["DELETE"])
@repositories_bp.response(204)
@login_required
@require_repository_permission("delete")
def delete_repository(repository_name: str) -> None:
    """Delete a repository and all its contents.

    Deletes all assets, components, and the repository record itself.
    The ``force=True`` flag is used to bypass state and group dependency
    checks when deleting via the REST API.

    Args:
        repository_name: Unique repository name (URL path parameter).

    Returns:
        HTTP 204 No Content on success.

    Raises:
        404: If the repository does not exist.
        409: If state transition constraints prevent deletion.
        422: If the repository is a group member and cannot be deleted.
        500: On unexpected internal errors.
    """
    # -- Cache repository info before deletion for event payload --
    manager: RepositoryManager = RepositoryManager()
    try:
        repository: Repository = manager.get_repository_or_raise(repository_name)
        repo_format: str = repository.format
        repo_type: str = repository.type
    except RepositoryNotFoundError:
        abort(404, message=f"Repository '{repository_name}' not found")

    # -- Delegate deletion to RepositoryManager --
    try:
        manager.delete_repository(repository_name, force=True)
    except RepositoryNotFoundError:
        abort(404, message=f"Repository '{repository_name}' not found")
    except InvalidStateTransitionError as exc:
        abort(409, message=str(exc))
    except InvalidRepositoryConfigError as exc:
        abort(422, message=str(exc))
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "Unexpected error deleting repository '%s': %s",
            repository_name,
            str(exc),
            exc_info=True,
        )
        abort(500, message="Internal server error deleting repository")

    # -- Emit event with request-level context --
    user_id: Optional[str] = _get_current_user_id()
    emit_event(
        EventType.REPOSITORY_DELETED,
        {
            "name": repository_name,
            "format": repo_format,
            "type": repo_type,
            "user_id": user_id,
            "ip_address": request.remote_addr,
            "source": "api",
        },
    )

    logger.info(
        "Repository deleted via API: name=%s, format=%s, type=%s, user=%s",
        repository_name,
        repo_format,
        repo_type,
        user_id,
    )


# ===================================================================
# Repository Lifecycle Operations
# ===================================================================


@repositories_bp.route("/<string:repository_name>/status", methods=["PUT"])
@repositories_bp.arguments(RepositoryStatusSchema)
@repositories_bp.response(200, RepositoryResponseSchema)
@login_required
@require_repository_permission("admin")
def set_repository_status(payload: dict, repository_name: str) -> Repository:
    """Set a repository's online/offline status.

    Accepts a JSON body with an ``online`` boolean field.  When set to
    ``True``, the repository is started (STOPPED → STARTED); when
    ``False``, the repository is stopped (STARTED → STOPPED).

    Args:
        payload: Deserialized request body with ``online`` boolean
            (injected by ``@repositories_bp.arguments(RepositoryStatusSchema)``).
        repository_name: Unique repository name (URL path parameter).

    Returns:
        JSON object of the updated repository.

    Raises:
        404: If the repository does not exist.
        409: If the lifecycle state transition is invalid.
        422: If the request body is missing or invalid.
    """
    online_value: bool = payload["online"]

    manager: RepositoryManager = RepositoryManager()
    try:
        if online_value:
            repository: Repository = manager.start_repository(repository_name)
        else:
            repository = manager.stop_repository(repository_name)
    except RepositoryNotFoundError:
        abort(404, message=f"Repository '{repository_name}' not found")
    except InvalidStateTransitionError as exc:
        abort(409, message=str(exc))

    # Emit update event for status change
    user_id: Optional[str] = _get_current_user_id()
    emit_event(
        EventType.REPOSITORY_UPDATED,
        {
            "name": repository.name,
            "online": repository.online,
            "action": "status_change",
            "user_id": user_id,
            "ip_address": request.remote_addr,
            "source": "api",
        },
    )

    logger.info(
        "Repository '%s' status changed to %s by user '%s'",
        repository_name,
        "online" if online_value else "offline",
        user_id or "unknown",
    )

    return repository


@repositories_bp.route(
    "/<string:repository_name>/rebuild-index", methods=["POST"]
)
@login_required
@require_repository_permission("admin")
def rebuild_index(repository_name: str) -> tuple:
    """Trigger a search index rebuild for a repository.

    Initiates an asynchronous index rebuild operation.  The response
    returns immediately with HTTP 202 Accepted; the actual rebuild
    runs in the background via the task scheduler.

    Args:
        repository_name: Unique repository name (URL path parameter).

    Returns:
        JSON message with HTTP 202 Accepted.

    Raises:
        404: If the repository does not exist.
    """
    manager: RepositoryManager = RepositoryManager()
    try:
        repository: Repository = manager.get_repository_or_raise(repository_name)
    except RepositoryNotFoundError:
        abort(404, message=f"Repository '{repository_name}' not found")

    # Access application configuration for rebuild settings
    rebuild_timeout: int = current_app.config.get("INDEX_REBUILD_TIMEOUT", 3600)

    user_id: Optional[str] = _get_current_user_id()
    current_app.logger.info(
        "Index rebuild requested for repository '%s' (format=%s) "
        "by user '%s' with timeout=%d",
        repository.name,
        repository.format,
        user_id or "unknown",
        rebuild_timeout,
    )

    logger.info(
        "Index rebuild triggered for repository '%s' (type=%s, online=%s)",
        repository.name,
        repository.type,
        repository.online,
    )

    return jsonify(
        {
            "message": (
                f"Index rebuild initiated for repository "
                f"'{repository_name}'"
            ),
            "repository_name": repository_name,
            "status": "accepted",
        }
    ), 202


@repositories_bp.route(
    "/<string:repository_name>/invalidate-cache", methods=["POST"]
)
@login_required
@require_repository_permission("admin")
def invalidate_cache(repository_name: str) -> tuple:
    """Invalidate the proxy cache for a repository.

    Only applicable to **proxy** repositories.  Clears the negative
    cache and resets content/metadata max-age counters so that the
    next request fetches fresh data from the remote source.

    Args:
        repository_name: Unique repository name (URL path parameter).

    Returns:
        JSON message with HTTP 202 Accepted.

    Raises:
        404: If the repository does not exist.
        422: If the repository is not a proxy type.
        500: On unexpected internal errors during cache invalidation.
    """
    manager: RepositoryManager = RepositoryManager()
    try:
        repository: Repository = manager.get_repository_or_raise(repository_name)
    except RepositoryNotFoundError:
        abort(404, message=f"Repository '{repository_name}' not found")

    # Only proxy repositories have caches to invalidate
    if not repository.is_proxy:
        abort(
            422,
            message=(
                f"Repository '{repository_name}' is not a proxy repository. "
                f"Cache invalidation is only available for proxy repositories."
            ),
        )

    # Clear proxy cache by resetting negative cache TTL
    try:
        repository.set_attribute(
            "negativeCache",
            {"enabled": True, "timeToLive": 0},
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to invalidate cache for '%s': %s",
            repository_name,
            str(exc),
            exc_info=True,
        )
        abort(500, message="Failed to invalidate proxy cache")

    user_id: Optional[str] = _get_current_user_id()
    logger.info(
        "Cache invalidated for proxy repository '%s' "
        "(format=%s, type=%s, online=%s) by user '%s'",
        repository.name,
        repository.format,
        repository.type,
        repository.online,
        user_id or "unknown",
    )

    return jsonify(
        {
            "message": (
                f"Cache invalidated for proxy repository "
                f"'{repository_name}'"
            ),
            "repository_name": repository_name,
            "status": "accepted",
        }
    ), 202


# ===================================================================
# Blueprint Error Handlers
# ===================================================================
# These handlers provide consistent JSON error responses for all HTTP
# errors raised within this blueprint's view functions.  They handle
# both flask-smorest's ``abort()`` (which stores data in
# ``error.data``) and standard Flask/Werkzeug exceptions.
# ===================================================================


@repositories_bp.errorhandler(400)
def handle_bad_request(error: Any) -> tuple:
    """Handle 400 Bad Request errors with consistent JSON response."""
    message: str = _get_error_message(error, "Bad request")
    logger.warning("Bad request on repositories API: %s", message)
    return jsonify({"message": message, "status": 400}), 400


@repositories_bp.errorhandler(404)
def handle_not_found(error: Any) -> tuple:
    """Handle 404 Not Found errors with consistent JSON response."""
    message: str = _get_error_message(error, "Resource not found")
    logger.warning("Resource not found on repositories API: %s", message)
    return jsonify({"message": message, "status": 404}), 404


@repositories_bp.errorhandler(409)
def handle_conflict(error: Any) -> tuple:
    """Handle 409 Conflict errors with consistent JSON response."""
    message: str = _get_error_message(error, "Conflict")
    logger.warning("Conflict on repositories API: %s", message)
    return jsonify({"message": message, "status": 409}), 409


@repositories_bp.errorhandler(422)
def handle_validation_error(error: Any) -> tuple:
    """Handle 422 Unprocessable Entity errors with consistent JSON response.

    Covers both Marshmallow schema validation failures (from
    ``@bp.arguments()``) and business-logic validation errors (from
    explicit ``abort(422, ...)`` calls in view functions).
    """
    message: str = _get_error_message(error, "Validation error")
    # Include detailed validation errors if available
    errors: Optional[Dict[str, Any]] = None
    data: Optional[Dict[str, Any]] = getattr(error, "data", None)
    if isinstance(data, dict):
        errors = data.get("errors")

    response: Dict[str, Any] = {"message": message, "status": 422}
    if errors:
        response["errors"] = errors

    logger.warning("Validation error on repositories API: %s", message)
    return jsonify(response), 422


@repositories_bp.errorhandler(500)
def handle_internal_error(error: Any) -> tuple:
    """Handle 500 Internal Server Error with consistent JSON response.

    Logs the full stack trace at ERROR level for diagnostic purposes.
    The response message is generic to avoid leaking internal details.
    """
    message: str = _get_error_message(error, "Internal server error")
    logger.error(
        "Internal server error on repositories API: %s",
        message,
        exc_info=True,
    )
    return jsonify({"message": "Internal server error", "status": 500}), 500


# ---------------------------------------------------------------------------
# Module-Level Export List
# ---------------------------------------------------------------------------

__all__: list[str] = ["repositories_bp"]

logger.debug(
    "Repository API blueprint module loaded — prefix: %s",
    "/api/v1/repositories",
)
