"""
Component Management REST API Blueprint (F-501-RQ-002).

This module implements the Flask-smorest Blueprint for component (artifact /
package) management, providing CRUD endpoints for components within
repositories.  It replaces the Java Component management REST API resources
from the original Sonatype Nexus Repository source system.

**Endpoints:**

+--------+--------------------------------------+-------------------------+
| Method | Path                                 | Description             |
+========+======================================+=========================+
| GET    | ``/api/v1/components``               | List / filter / search  |
+--------+--------------------------------------+-------------------------+
| GET    | ``/api/v1/components/<component_id>``| Get component detail    |
+--------+--------------------------------------+-------------------------+
| POST   | ``/api/v1/components``               | Upload a new component  |
+--------+--------------------------------------+-------------------------+
| DELETE | ``/api/v1/components/<component_id>``| Delete component        |
+--------+--------------------------------------+-------------------------+

**Feature Coverage:**

- **F-101** (Multi-Format Repository Support): Format-agnostic component
  listing and detail retrieval with format-specific metadata in the
  ``attributes`` JSON column.
- **F-102** (Repository Types): Upload restricted to ``hosted`` repositories;
  proxy and group repositories reject uploads.
- **F-501-RQ-002** (Component Management): Full CRUD lifecycle for components.

**Architecture Context:**

- Uses :class:`RepositoryManager` from ``src.app.services.repository_manager``
  as the business logic delegate for listing, detail retrieval, and deletion.
- Uses :class:`UploadManager` from ``src.app.services.upload_manager`` for
  multipart file upload handling with write policy enforcement, checksum
  computation, and BlobStore persistence.
- Emits Blinker events (``COMPONENT_UPLOADED``, ``COMPONENT_DELETED``) for
  audit logging (F-303) and webhook dispatch (F-503).
- All endpoints require authentication via the :func:`login_required` decorator.
- Upload and delete require repository-scoped RBAC permissions via
  :func:`require_repository_permission`.

**Performance Targets (AAP Section 0.7.3):**

- REST API response time: < 500 ms average
- Cached artifact resolution: < 200 ms

**Write Policies (for hosted repositories):**

- ``ALLOW`` (default): Overwrites permitted; existing assets are updated.
- ``ALLOW_ONCE``: First write succeeds; duplicates rejected (immutable).
- ``DENY``: Repository is read-only; all uploads are rejected.

Exports:
    components_bp : Flask-smorest Blueprint instance registered at
                    ``/api/v1/components``.
"""

from __future__ import annotations

import logging
from typing import Optional

from flask import current_app, g, request, send_file
from flask_smorest import Blueprint, abort

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_repository_permission
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.extensions import db
from src.app.models.asset import Asset
from src.app.models.component import Component
from src.app.models.repository import Repository
from src.app.schemas.component import (
    ComponentDetailSchema,
    ComponentListQuerySchema,
    ComponentSchema,
    ComponentSearchResultSchema,
    ComponentUploadSchema,
)
from src.app.services.repository_manager import (
    RepositoryManager,
    RepositoryNotFoundError,
    RepositoryOfflineError,
)
from src.app.services.upload_manager import UploadError, UploadManager

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for component API operations — upload events,
# deletion events, error conditions, and request tracing.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------
# Replaces the JAX-RS resource class for component management from the
# RESTEasy 6.2.7 framework in the Java source system.  The Blueprint
# provides:
#   - Automatic OpenAPI 3.x documentation generation via flask-smorest
#   - Route registration with schema-based validation
#   - Consistent JSON error responses via abort()
# ---------------------------------------------------------------------------

components_bp: Blueprint = Blueprint(
    "components",
    __name__,
    url_prefix="/api/v1/components",
    description="Component upload, download, and management (F-501-RQ-002)",
)

# ---------------------------------------------------------------------------
# Pagination Constants
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE: int = 50
MAX_PAGE_SIZE: int = 200


# ===========================================================================
# GET /api/v1/components — List Components
# ===========================================================================


@components_bp.route("/", methods=["GET"])
@components_bp.arguments(ComponentListQuerySchema, location="query")
@components_bp.response(200, ComponentSearchResultSchema)
@login_required
def list_components(args: dict) -> dict:
    """List and filter components across repositories.

    Supports pagination and optional filtering by repository, namespace,
    name, version, and format.  Delegates to
    :meth:`RepositoryManager.get_components` for paginated database queries.

    Query Parameters:
        repository (str, optional):  Filter by repository name.
        namespace (str, optional):   Filter by namespace / group.
        name (str, optional):        Filter by component name.
        version (str, optional):     Filter by version.
        format (str, optional):      Filter by repository format.
        page (int, optional):        Page number (1-based, default 1).
        page_size (int, optional):   Results per page (1-200, default 50).

    Returns:
        JSON object with ``items`` (list of component summaries),
        ``total_count``, ``page``, and ``page_size``.

    Status Codes:
        200: Successful retrieval of component list.
        401: Authentication required.
        404: Specified repository not found.
        422: Invalid query parameters.
    """
    repository_name: Optional[str] = args.get("repository")
    namespace: Optional[str] = args.get("namespace")
    name: Optional[str] = args.get("name")
    version: Optional[str] = args.get("version")
    page: int = args.get("page", 1)
    page_size: int = args.get("page_size", DEFAULT_PAGE_SIZE)

    logger.debug(
        "list_components: repository=%s, namespace=%s, name=%s, "
        "version=%s, page=%d, page_size=%d",
        repository_name,
        namespace,
        name,
        version,
        page,
        page_size,
    )

    manager: RepositoryManager = RepositoryManager()

    # If a specific repository is requested, validate it exists and is online
    if repository_name:
        try:
            result = manager.get_components(
                repository_name=repository_name,
                namespace=namespace,
                name=name,
                version=version,
                page=page,
                page_size=page_size,
            )
        except RepositoryNotFoundError:
            logger.warning(
                "list_components: repository '%s' not found.",
                repository_name,
            )
            abort(404, message=f"Repository '{repository_name}' not found")
        except RepositoryOfflineError:
            logger.warning(
                "list_components: repository '%s' is offline.",
                repository_name,
            )
            abort(
                503,
                message=f"Repository '{repository_name}' is currently offline",
            )
    else:
        # No specific repository — query across all repositories using
        # direct ORM query for flexibility
        query = Component.query

        if namespace is not None:
            query = query.filter(Component.namespace == namespace)
        if name is not None:
            query = query.filter(Component.name == name)
        if version is not None:
            query = query.filter(Component.version == version)

        # Apply format filter if present — requires a join to Repository
        format_filter: Optional[str] = args.get("format")
        if format_filter is not None:
            query = query.join(
                Repository,
                Component.repository_name == Repository.name,
            ).filter(Repository.format == format_filter)

        total_count: int = query.count()
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        page = max(1, page)
        offset: int = (page - 1) * page_size

        items = (
            query.order_by(Component.name, Component.version)
            .offset(offset)
            .limit(page_size)
            .all()
        )

        result = {
            "items": items,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
        }

    # Build the serialisable response structure
    component_schema = ComponentSchema(many=True)
    serialized_items = component_schema.dump(result["items"])

    return {
        "items": serialized_items,
        "total_count": result["total_count"],
        "page": result["page"],
        "page_size": result["page_size"],
    }


# ===========================================================================
# GET /api/v1/components/<component_id> — Get Component Detail
# ===========================================================================


@components_bp.route("/<int:component_id>", methods=["GET"])
@components_bp.response(200, ComponentDetailSchema)
@login_required
def get_component(component_id: int) -> dict:
    """Retrieve detailed information for a specific component.

    Includes the component's coordinate fields, attributes, timestamps,
    and a full list of associated assets.  The ``format`` field is derived
    from the parent repository.

    Path Parameters:
        component_id (int): Auto-incremented primary key of the component.

    Returns:
        JSON object with full component detail and nested asset list.

    Status Codes:
        200: Component found and returned.
        401: Authentication required.
        404: Component not found.
    """
    logger.debug("get_component: component_id=%d", component_id)

    component: Optional[Component] = db.session.get(Component, component_id)

    if component is None:
        logger.warning(
            "get_component: component with id=%d not found.",
            component_id,
        )
        abort(404, message=f"Component with id {component_id} not found")

    # Fetch associated assets for the detail view
    assets = Asset.query.filter(Asset.component_id == component_id).all()

    # Derive format from the parent repository
    repository: Optional[Repository] = Repository.query.get(
        component.repository_name
    )
    format_name: str = repository.format if repository else "unknown"

    # Build detail response
    detail_schema = ComponentDetailSchema()
    component_data = detail_schema.dump(component)
    asset_data = []
    for asset in assets:
        asset_data.append(
            {
                "id": asset.id,
                "path": asset.path,
                "content_type": asset.content_type,
                "size": asset.size,
                "checksum_sha1": getattr(asset, "checksum_sha1", None),
                "checksum_sha256": getattr(asset, "checksum_sha256", None),
                "checksum_md5": getattr(asset, "checksum_md5", None),
                "repository_name": asset.repository_name,
                "created_at": (
                    asset.created_at.isoformat()
                    if hasattr(asset, "created_at") and asset.created_at
                    else None
                ),
                "updated_at": (
                    asset.updated_at.isoformat()
                    if hasattr(asset, "updated_at") and asset.updated_at
                    else None
                ),
            }
        )

    component_data["assets"] = asset_data
    component_data["asset_count"] = len(assets)
    component_data["format"] = format_name

    return component_data


# ===========================================================================
# POST /api/v1/components — Upload Component
# ===========================================================================


@components_bp.route("/", methods=["POST"])
@login_required
def upload_component() -> tuple:
    """Upload a component with one or more assets to a hosted repository.

    Accepts multipart form data with component metadata and one or more
    file uploads.  The upload manager handles format-specific validation,
    write policy enforcement, checksum computation, content type detection,
    and BlobStore storage.

    Form Data:
        repository_name (str, required): Target hosted repository name.
        namespace (str, optional):       Component namespace / group.
        name (str, required):            Component name.
        version (str, optional):         Component version string.
        attributes (str, optional):      JSON-encoded format metadata.

    File Data:
        One or more files attached as multipart form data.

    Returns:
        JSON object with the created component detail (201 Created).

    Status Codes:
        201: Component successfully uploaded and created.
        400: Invalid request data or upload error.
        401: Authentication required.
        403: Insufficient repository permissions.
        404: Repository not found.
        409: Write policy conflict (ALLOW_ONCE duplicate).
        422: Validation error in form data.
    """
    logger.debug("upload_component: processing upload request")

    # ---- Extract and validate form metadata ----
    upload_schema = ComponentUploadSchema()

    form_data = {
        "repository_name": request.form.get("repository_name", "").strip(),
        "namespace": request.form.get("namespace", "").strip() or None,
        "name": request.form.get("name", "").strip(),
        "version": request.form.get("version", "").strip() or None,
    }

    # Validate optional JSON attributes
    raw_attributes: Optional[str] = request.form.get("attributes")
    if raw_attributes:
        import json

        try:
            form_data["attributes"] = json.loads(raw_attributes)
        except (json.JSONDecodeError, TypeError):
            abort(400, message="Invalid JSON in 'attributes' form field")
    else:
        form_data["attributes"] = None

    # Run Marshmallow validation on form metadata
    try:
        validated = upload_schema.load(form_data)
    except Exception as exc:
        logger.warning(
            "upload_component: validation error: %s",
            str(exc),
        )
        abort(422, message=f"Validation error: {exc}")

    repository_name: str = validated["repository_name"]
    namespace: Optional[str] = validated.get("namespace")
    name: str = validated["name"]
    version: Optional[str] = validated.get("version")

    # ---- Validate repository exists and is hosted ----
    repository: Optional[Repository] = Repository.query.get(repository_name)
    if repository is None:
        logger.warning(
            "upload_component: repository '%s' not found.",
            repository_name,
        )
        abort(404, message=f"Repository '{repository_name}' not found")

    if not repository.is_hosted:
        logger.warning(
            "upload_component: repository '%s' is not a hosted repository "
            "(type='%s'). Uploads are only accepted by hosted repositories.",
            repository_name,
            repository.type,
        )
        abort(
            400,
            message=(
                f"Repository '{repository_name}' is of type "
                f"'{repository.type}'. Only hosted repositories accept "
                f"uploads."
            ),
        )

    if not repository.online:
        logger.warning(
            "upload_component: repository '%s' is offline.",
            repository_name,
        )
        abort(
            503,
            message=f"Repository '{repository_name}' is currently offline",
        )

    # ---- Check repository-scoped 'add' permission ----
    user = getattr(g, "current_user", None)
    if user is not None:
        from src.app.auth.authorization import AuthorizationEngine

        engine = AuthorizationEngine()
        format_name: str = repository.format or "*"
        if not engine.check_repository_privilege(
            user, repository_name, format_name, "add"
        ):
            logger.warning(
                "upload_component: user '%s' lacks 'add' permission "
                "for repository '%s'.",
                getattr(user, "user_id", "unknown"),
                repository_name,
            )
            abort(
                403,
                message=(
                    f"Insufficient privileges: {repository_name}:add"
                ),
            )

    # ---- Extract uploaded files ----
    uploaded_files = request.files.getlist("file")
    if not uploaded_files:
        # Fall back to checking for any file in the request
        uploaded_files = list(request.files.values())

    if not uploaded_files:
        abort(400, message="No files uploaded. At least one file is required.")

    # Build the asset list for the upload manager
    assets: list[dict] = []
    for file_storage in uploaded_files:
        if file_storage.filename:
            assets.append(
                {
                    "filename": file_storage.filename,
                    "data": file_storage.stream,
                    "content_type": file_storage.content_type,
                }
            )

    if not assets:
        abort(400, message="No valid files found in the upload request.")

    # ---- Delegate to UploadManager ----
    upload_manager: UploadManager = UploadManager()

    try:
        component = upload_manager.upload_component(
            repository_name=repository_name,
            namespace=namespace,
            name=name,
            version=version,
            assets=assets,
        )
    except UploadError as exc:
        logger.warning(
            "upload_component: upload failed for repository '%s': %s",
            repository_name,
            exc.message,
        )
        status_code: int = getattr(exc, "status_code", 400)
        abort(status_code, message=exc.message)
    except Exception as exc:
        logger.error(
            "upload_component: unexpected error during upload to '%s'",
            repository_name,
            exc_info=True,
        )
        abort(500, message="Internal server error during component upload")

    # ---- Emit COMPONENT_UPLOADED event ----
    emit_event(
        EventType.COMPONENT_UPLOADED,
        {
            "repository_name": repository_name,
            "component_id": component.id,
            "component_name": component.name,
            "component_version": component.version,
            "namespace": component.namespace,
            "format": repository.format,
            "user_id": (
                getattr(g, "current_user", None)
                and getattr(g.current_user, "user_id", None)
            ),
            "ip_address": request.remote_addr,
        },
    )

    logger.info(
        "Component uploaded: id=%d, name=%s, version=%s, repo=%s",
        component.id,
        component.name,
        component.version,
        repository_name,
    )

    # ---- Return 201 Created with component details ----
    component_schema = ComponentDetailSchema()
    component_data = component_schema.dump(component)

    # Enrich with format from repository
    component_data["format"] = repository.format

    # Fetch assets that were created for this component
    created_assets = Asset.query.filter(
        Asset.component_id == component.id
    ).all()
    component_data["asset_count"] = len(created_assets)

    return component_data, 201


# ===========================================================================
# DELETE /api/v1/components/<component_id> — Delete Component
# ===========================================================================


@components_bp.route("/<int:component_id>", methods=["DELETE"])
@components_bp.response(204)
@login_required
def delete_component(component_id: int) -> tuple:
    """Delete a component and all its associated assets.

    Cascades deletion to all owned assets and triggers BlobStore cleanup.
    Emits a ``COMPONENT_DELETED`` event for audit logging and webhooks.

    Path Parameters:
        component_id (int): Auto-incremented primary key of the component.

    Returns:
        Empty response body (204 No Content).

    Status Codes:
        204: Component successfully deleted.
        401: Authentication required.
        403: Insufficient repository permissions.
        404: Component not found.
    """
    logger.debug("delete_component: component_id=%d", component_id)

    # Fetch the component
    component: Optional[Component] = db.session.get(Component, component_id)

    if component is None:
        logger.warning(
            "delete_component: component with id=%d not found.",
            component_id,
        )
        abort(404, message=f"Component with id {component_id} not found")

    repository_name: str = component.repository_name

    # ---- Check repository-scoped 'delete' permission ----
    user = getattr(g, "current_user", None)
    if user is not None:
        repository: Optional[Repository] = Repository.query.get(
            repository_name
        )
        format_name: str = (
            repository.format if repository else "*"
        )

        from src.app.auth.authorization import AuthorizationEngine

        engine = AuthorizationEngine()
        if not engine.check_repository_privilege(
            user, repository_name, format_name, "delete"
        ):
            logger.warning(
                "delete_component: user '%s' lacks 'delete' permission "
                "for repository '%s'.",
                getattr(user, "user_id", "unknown"),
                repository_name,
            )
            abort(
                403,
                message=(
                    f"Insufficient repository privileges: "
                    f"{repository_name}:delete"
                ),
            )

    # Capture metadata for event emission before deletion
    component_name: str = component.name
    component_version: Optional[str] = component.version
    component_namespace: Optional[str] = component.namespace
    repository_obj: Optional[Repository] = Repository.query.get(
        repository_name
    )
    format_str: str = repository_obj.format if repository_obj else "unknown"

    # ---- Delegate to RepositoryManager for deletion ----
    manager: RepositoryManager = RepositoryManager()
    try:
        manager.delete_component(
            repository_name=repository_name,
            component_id=component_id,
        )
    except RepositoryNotFoundError as exc:
        logger.warning(
            "delete_component: %s",
            str(exc),
        )
        abort(404, message=str(exc))
    except Exception as exc:
        logger.error(
            "delete_component: unexpected error deleting component %d",
            component_id,
            exc_info=True,
        )
        abort(500, message="Internal server error during component deletion")

    # ---- Emit COMPONENT_DELETED event ----
    emit_event(
        EventType.COMPONENT_DELETED,
        {
            "repository_name": repository_name,
            "component_id": component_id,
            "component_name": component_name,
            "component_version": component_version,
            "namespace": component_namespace,
            "format": format_str,
            "user_id": (
                getattr(g, "current_user", None)
                and getattr(g.current_user, "user_id", None)
            ),
            "ip_address": request.remote_addr,
        },
    )

    logger.info(
        "Component deleted: id=%d, name=%s, repo=%s",
        component_id,
        component_name,
        repository_name,
    )

    return "", 204


# ===========================================================================
# Error Handlers for the Components Blueprint
# ===========================================================================


@components_bp.errorhandler(413)
def handle_request_entity_too_large(error):
    """Handle file uploads that exceed the maximum size limit.

    Returns:
        JSON error response with HTTP 413 status code.
    """
    logger.warning(
        "upload_component: request entity too large: %s",
        str(error),
    )
    return {
        "code": 413,
        "message": "Uploaded file exceeds the maximum allowed size.",
        "status": "Request Entity Too Large",
    }, 413


@components_bp.errorhandler(415)
def handle_unsupported_media_type(error):
    """Handle unsupported content types in upload requests.

    Returns:
        JSON error response with HTTP 415 status code.
    """
    logger.warning(
        "upload_component: unsupported media type: %s",
        str(error),
    )
    return {
        "code": 415,
        "message": "Unsupported media type for component upload.",
        "status": "Unsupported Media Type",
    }, 415


# ---------------------------------------------------------------------------
# Module-Level Exports
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "components_bp",
]

logger.debug(
    "Components API blueprint loaded — url_prefix=%s",
    components_bp.url_prefix,
)
