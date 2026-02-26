"""
BlobStore CRUD and Configuration REST API Blueprint.

Implements the BlobStore management endpoints for Features F-201 (File
BlobStore) and F-202 (S3 BlobStore).  Provides full CRUD operations on
BlobStore configurations, real-time storage metrics retrieval, and quota
management.

**Architecture Context:**

This module replaces the Java BlobStore management REST API from the
original Sonatype Nexus Repository system (RESTEasy 6.2.7 JAX-RS resource
classes, Swagger/OpenAPI 2.2.20 documentation).  The Flask-smorest Blueprint
provides equivalent route definition with automatic OpenAPI 3.x
documentation generation.

**Supported BlobStore Types:**

+----------+-----------------------------------------------------------+
| Type     | Description                                               |
+==========+===========================================================+
| ``file`` | Local filesystem storage (F-201) — atomic writes with     |
|          | temp staging + rename-on-commit, soft-delete support.     |
+----------+-----------------------------------------------------------+
| ``s3``   | Amazon S3 storage (F-202) — SSE encryption, multipart     |
|          | uploads, configurable region and endpoint.                |
+----------+-----------------------------------------------------------+

**Security:**

- All endpoints require authentication via ``@login_required``.
- RBAC permissions are enforced via ``@require_permission('blobstores', ...)``
  for create, read, update, and delete operations.
- S3 credentials (``secretAccessKey``, ``accessKeyId``) are **always** masked
  in API responses via ``BlobStoreConfig.to_dict_secure()``.

**Endpoints:**

+--------+-----------------------------------------+-------------------+
| Method | Path                                    | Description       |
+========+=========================================+===================+
| GET    | ``/api/v1/blobstores``                  | List all stores   |
+--------+-----------------------------------------+-------------------+
| GET    | ``/api/v1/blobstores/<name>``           | Get single store  |
+--------+-----------------------------------------+-------------------+
| POST   | ``/api/v1/blobstores``                  | Create new store  |
+--------+-----------------------------------------+-------------------+
| PUT    | ``/api/v1/blobstores/<name>``           | Update config     |
+--------+-----------------------------------------+-------------------+
| DELETE | ``/api/v1/blobstores/<name>``           | Delete store      |
+--------+-----------------------------------------+-------------------+
| PUT    | ``/api/v1/blobstores/<name>/quota``     | Set quota         |
+--------+-----------------------------------------+-------------------+
| GET    | ``/api/v1/blobstores/<name>/metrics``   | Get metrics       |
+--------+-----------------------------------------+-------------------+

Exports:
    blobstores_bp : Flask-smorest Blueprint registered at
                    ``/api/v1/blobstores``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import jsonify
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields, validate

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.extensions import db
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.services.blobstore_service import (
    BlobStoreConfigError,
    BlobStoreError,
    BlobStoreInUseError,
    BlobStoreNotFoundError,
    BlobStoreService,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for all BlobStore CRUD operations,
# configuration validation, deletion protection checks, metrics retrieval,
# and error conditions per AAP Section 0.7.2.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Marshmallow Schemas (Inline — no dedicated schema file for blobstores)
# ---------------------------------------------------------------------------
# Replaces Jackson 2.16.1 JSON serialization DTOs from the Java source.
# Defined inline because there is no src/app/schemas/blobstore.py.
# ---------------------------------------------------------------------------


class BlobStoreConfigurationSchema(Schema):
    """Schema for the backend-specific configuration payload.

    Accepts an arbitrary dictionary since the required keys differ by
    BlobStore type:

    - **file**: ``{"path": "/data/blobs/default"}``
    - **s3**: ``{"bucket": "...", "region": "...", ...}``
    """

    class Meta:
        strict = True


class BlobStoreCreateSchema(Schema):
    """Validates POST ``/api/v1/blobstores`` request bodies.

    Required Fields:
        name: Unique BlobStore identifier (1–200 characters).
        type: Backend type — ``'file'`` or ``'s3'``.
        configuration: Type-specific configuration dictionary.
    """

    name = fields.String(
        required=True,
        validate=validate.Length(min=1, max=200),
        metadata={
            "description": "Unique BlobStore name (alphanumeric, hyphens, underscores).",
            "example": "my-blobstore",
        },
    )
    type = fields.String(
        required=True,
        validate=validate.OneOf(["file", "s3"]),
        metadata={
            "description": "Backend type: 'file' for local filesystem, 's3' for Amazon S3.",
            "example": "file",
        },
    )
    configuration = fields.Dict(
        required=True,
        keys=fields.String(),
        values=fields.Raw(),
        metadata={
            "description": (
                "Type-specific configuration. "
                "File: {path: '/data/blobs'}. "
                "S3: {bucket, region, accessKeyId, secretAccessKey, ...}."
            ),
        },
    )


class BlobStoreUpdateSchema(Schema):
    """Validates PUT ``/api/v1/blobstores/<name>`` request bodies.

    Only the ``configuration`` field is updatable.  The BlobStore ``type``
    cannot be changed after creation (file ↔ s3 migration not supported).
    """

    configuration = fields.Dict(
        required=True,
        keys=fields.String(),
        values=fields.Raw(),
        metadata={
            "description": "Updated type-specific configuration dictionary.",
        },
    )


class BlobStoreQuotaSchema(Schema):
    """Validates PUT ``/api/v1/blobstores/<name>/quota`` request bodies.

    Quota Types:
        spaceUsedQuota: Maximum total size in bytes.
        spaceRemainingQuota: Minimum free space in bytes.
    """

    quota_type = fields.String(
        required=True,
        validate=validate.OneOf(["spaceUsedQuota", "spaceRemainingQuota"]),
        metadata={
            "description": "Quota type: 'spaceUsedQuota' or 'spaceRemainingQuota'.",
            "example": "spaceUsedQuota",
        },
    )
    quota_limit = fields.Integer(
        required=True,
        metadata={
            "description": "Quota limit value in bytes.",
            "example": 107374182400,
        },
    )
    enabled = fields.Boolean(
        load_default=True,
        metadata={
            "description": "Whether the quota enforcement is enabled.",
        },
    )


class BlobStoreResponseSchema(Schema):
    """Serializes BlobStore configuration for API responses.

    S3 credentials are always masked via ``to_dict_secure()``.
    """

    blob_store_name = fields.String(
        metadata={"description": "Unique BlobStore identifier."},
    )
    type = fields.String(
        metadata={"description": "Backend type: 'file' or 's3'."},
    )
    configuration = fields.Dict(
        keys=fields.String(),
        values=fields.Raw(),
        metadata={
            "description": (
                "Backend configuration with sensitive fields masked."
            ),
        },
    )
    total_size = fields.Integer(
        metadata={"description": "Total storage consumed in bytes."},
    )
    blob_count = fields.Integer(
        metadata={"description": "Total number of blobs stored."},
    )
    available_space = fields.Integer(
        allow_none=True,
        metadata={
            "description": "Available space in bytes (null for S3).",
        },
    )
    created_at = fields.DateTime(
        allow_none=True,
        metadata={"description": "ISO 8601 creation timestamp."},
    )
    updated_at = fields.DateTime(
        allow_none=True,
        metadata={"description": "ISO 8601 last-modified timestamp."},
    )


class BlobStoreMetricsResponseSchema(Schema):
    """Serializes real-time BlobStore storage metrics."""

    blob_store_name = fields.String(
        metadata={"description": "BlobStore identifier."},
    )
    total_size = fields.Integer(
        metadata={"description": "Total storage consumed in bytes."},
    )
    blob_count = fields.Integer(
        metadata={"description": "Total number of blobs stored."},
    )
    available_space = fields.Integer(
        allow_none=True,
        metadata={"description": "Available space in bytes."},
    )
    usage_percentage = fields.String(
        allow_none=True,
        metadata={"description": "Storage usage percentage."},
    )
    available = fields.Boolean(
        metadata={"description": "Whether the BlobStore runtime is active."},
    )
    timestamp = fields.String(
        metadata={"description": "ISO 8601 snapshot timestamp."},
    )


# ---------------------------------------------------------------------------
# Blueprint Definition
# ---------------------------------------------------------------------------
# CRITICAL: Export name MUST be ``blobstores_bp``.
# Registered by ``src/app/api/__init__.py`` in ``register_api_blueprints()``.
# URL prefix ``/api/v1/blobstores`` aligns with AAP Section 0.5.1.
# ---------------------------------------------------------------------------

blobstores_bp: Blueprint = Blueprint(
    "blobstores",
    __name__,
    url_prefix="/api/v1/blobstores",
    description="BlobStore management and configuration (F-201, F-202)",
)


# ---------------------------------------------------------------------------
# Helper: Instantiate BlobStoreService
# ---------------------------------------------------------------------------


def _get_service() -> BlobStoreService:
    """Return a :class:`BlobStoreService` instance.

    The service is lightweight and stateless from a request perspective;
    a fresh instance is created per request.  Runtime BlobStore caches
    are maintained internally by the service.

    Returns:
        A ready-to-use :class:`BlobStoreService` instance.
    """
    return BlobStoreService()


# ===========================================================================
# Phase 2: List BlobStores — GET /api/v1/blobstores
# ===========================================================================


@blobstores_bp.route("/", methods=["GET"])
@login_required
@require_permission("blobstores", "read")
def list_blobstores() -> tuple:
    """List all configured BlobStores.

    Returns all BlobStore configurations with type, storage statistics,
    and masked credentials (S3).

    **Authentication:** Required (any authenticated user with
    ``blobstores:read`` privilege).

    **Response:** JSON array of BlobStore configuration objects.
    """
    logger.info("Listing all BlobStore configurations.")

    service: BlobStoreService = _get_service()
    configs: List[BlobStoreConfig] = service.list_blobstores()

    # CRITICAL SECURITY: Use to_dict_secure() to mask S3 credentials
    result: List[Dict[str, Any]] = [
        config.to_dict_secure() for config in configs
    ]

    logger.info("Returned %d BlobStore configuration(s).", len(result))
    return jsonify(result), 200


# ===========================================================================
# Phase 3: Get BlobStore — GET /api/v1/blobstores/<blob_store_name>
# ===========================================================================


@blobstores_bp.route("/<string:blob_store_name>", methods=["GET"])
@login_required
@require_permission("blobstores", "read")
def get_blobstore(blob_store_name: str) -> tuple:
    """Retrieve a specific BlobStore configuration by name.

    Returns the full BlobStore configuration with credentials masked
    and current storage metrics.

    Args:
        blob_store_name: The unique BlobStore identifier (URL path param).

    **Authentication:** Required (``blobstores:read`` privilege).

    **Responses:**
        200: BlobStore configuration with masked credentials.
        404: BlobStore not found.
    """
    logger.info(
        "Retrieving BlobStore configuration: name='%s'.",
        blob_store_name,
    )

    service: BlobStoreService = _get_service()

    try:
        config: BlobStoreConfig = service.get_blobstore_or_raise(
            blob_store_name,
        )
    except BlobStoreNotFoundError as exc:
        logger.warning(
            "BlobStore not found: name='%s'. %s",
            blob_store_name,
            exc.message,
        )
        abort(404, message=exc.message)

    # Build response with secure masking and include status info
    result: Dict[str, Any] = config.to_dict_secure()

    # Attempt to enrich with runtime status
    try:
        status: Dict[str, Any] = service.get_blobstore_status(
            blob_store_name,
        )
        result["available"] = status.get("available", False)
    except Exception:
        result["available"] = False

    logger.info(
        "Returned BlobStore configuration: name='%s', type='%s'.",
        blob_store_name,
        config.type,
    )
    return jsonify(result), 200


# ===========================================================================
# Phase 4: Create BlobStore — POST /api/v1/blobstores
# ===========================================================================


@blobstores_bp.route("/", methods=["POST"])
@login_required
@require_permission("blobstores", "create")
@blobstores_bp.arguments(BlobStoreCreateSchema)
def create_blobstore(validated: Dict[str, Any]) -> tuple:
    """Create a new BlobStore configuration.

    Supports two backend types:

    - **file**: Requires ``path`` in configuration.
    - **s3**: Requires ``bucket``, ``region`` in configuration.

    **Authentication:** Required (``blobstores:create`` privilege).

    **Request Body (JSON):**
        name (str): Unique BlobStore name.
        type (str): Backend type — ``'file'`` or ``'s3'``.
        configuration (dict): Type-specific configuration.

    **Responses:**
        201: Created BlobStore configuration.
        400: Invalid configuration.
        409: BlobStore name already exists.
    """
    name: str = validated["name"]
    store_type: str = validated["type"]
    configuration: Dict[str, Any] = validated["configuration"]

    logger.info(
        "Creating BlobStore: name='%s', type='%s'.",
        name,
        store_type,
    )

    # -- Delegate to BlobStoreService ----------------------------------------
    service: BlobStoreService = _get_service()

    try:
        config: BlobStoreConfig = service.create_blobstore(
            name=name,
            store_type=store_type,
            configuration=configuration,
        )
    except BlobStoreConfigError as exc:
        # Distinguish "already exists" from other config errors — map
        # duplicate name to 409 Conflict per REST API conventions.
        if "already exists" in exc.message.lower():
            logger.warning(
                "BlobStore creation conflict: name='%s'. %s",
                name,
                exc.message,
            )
            abort(409, message=exc.message)
        logger.warning(
            "BlobStore creation failed (config error): name='%s'. %s",
            name,
            exc.message,
        )
        abort(400, message=exc.message)
    except BlobStoreError as exc:
        logger.error(
            "BlobStore creation failed: name='%s'. %s",
            name,
            exc.message,
        )
        abort(400, message=exc.message)

    # -- Return created configuration with secure masking -------------------
    result: Dict[str, Any] = config.to_dict_secure()

    logger.info(
        "BlobStore created successfully: name='%s', type='%s'.",
        name,
        store_type,
    )
    return jsonify(result), 201


# ===========================================================================
# Phase 5: Update BlobStore — PUT /api/v1/blobstores/<blob_store_name>
# ===========================================================================


@blobstores_bp.route("/<string:blob_store_name>", methods=["PUT"])
@login_required
@require_permission("blobstores", "update")
@blobstores_bp.arguments(BlobStoreUpdateSchema)
def update_blobstore(validated: Dict[str, Any], blob_store_name: str) -> tuple:
    """Update the configuration of an existing BlobStore.

    Only the ``configuration`` dictionary may be updated.  The BlobStore
    ``type`` cannot be changed after creation (file ↔ s3 migration is
    not supported).

    For S3 BlobStores: allows updating region, encryption settings, etc.
    Credentials must be re-submitted if changed.

    Args:
        validated: Deserialized request body from ``BlobStoreUpdateSchema``
            (injected by ``@blobstores_bp.arguments``).
        blob_store_name: The unique BlobStore identifier (URL path param).

    **Authentication:** Required (``blobstores:update`` privilege).

    **Request Body (JSON):**
        configuration (dict): Updated type-specific configuration.

    **Responses:**
        200: Updated BlobStore configuration.
        400: Invalid configuration.
        404: BlobStore not found.
    """
    configuration: Dict[str, Any] = validated["configuration"]

    logger.info(
        "Updating BlobStore configuration: name='%s'.",
        blob_store_name,
    )

    # -- Delegate to BlobStoreService ----------------------------------------
    service: BlobStoreService = _get_service()

    try:
        config: BlobStoreConfig = service.update_blobstore(
            name=blob_store_name,
            configuration=configuration,
        )
    except BlobStoreNotFoundError as exc:
        logger.warning(
            "BlobStore not found for update: name='%s'. %s",
            blob_store_name,
            exc.message,
        )
        abort(404, message=exc.message)
    except BlobStoreConfigError as exc:
        logger.warning(
            "BlobStore update failed (config error): name='%s'. %s",
            blob_store_name,
            exc.message,
        )
        abort(400, message=exc.message)
    except BlobStoreError as exc:
        logger.error(
            "BlobStore update failed: name='%s'. %s",
            blob_store_name,
            exc.message,
        )
        abort(400, message=exc.message)

    # -- Return updated configuration with secure masking -------------------
    result: Dict[str, Any] = config.to_dict_secure()

    logger.info(
        "BlobStore configuration updated: name='%s', type='%s'.",
        blob_store_name,
        config.type,
    )
    return jsonify(result), 200


# ===========================================================================
# Phase 6: Delete BlobStore — DELETE /api/v1/blobstores/<blob_store_name>
# ===========================================================================


@blobstores_bp.route("/<string:blob_store_name>", methods=["DELETE"])
@login_required
@require_permission("blobstores", "delete")
def delete_blobstore(blob_store_name: str) -> tuple:
    """Delete a BlobStore configuration.

    **Deletion Protection:** A BlobStore that is referenced by one or more
    repositories cannot be deleted.  Repositories must be migrated to a
    different BlobStore first.

    The system default BlobStore (``'default'``) cannot be deleted.

    Args:
        blob_store_name: The unique BlobStore identifier (URL path param).

    **Authentication:** Required (``blobstores:delete`` privilege).

    **Responses:**
        204: BlobStore deleted successfully.
        404: BlobStore not found.
        409: BlobStore is in use by repositories.
    """
    logger.info(
        "Deleting BlobStore: name='%s'.",
        blob_store_name,
    )

    service: BlobStoreService = _get_service()

    try:
        service.delete_blobstore(blob_store_name)
    except BlobStoreNotFoundError as exc:
        logger.warning(
            "BlobStore not found for deletion: name='%s'. %s",
            blob_store_name,
            exc.message,
        )
        abort(404, message=exc.message)
    except BlobStoreInUseError as exc:
        logger.warning(
            "BlobStore deletion blocked (in use): name='%s'. %s",
            blob_store_name,
            exc.message,
        )
        abort(409, message=exc.message)
    except BlobStoreConfigError as exc:
        logger.warning(
            "BlobStore deletion failed (config error): name='%s'. %s",
            blob_store_name,
            exc.message,
        )
        abort(400, message=exc.message)
    except BlobStoreError as exc:
        logger.error(
            "BlobStore deletion failed: name='%s'. %s",
            blob_store_name,
            exc.message,
        )
        abort(500, message=exc.message)

    logger.info(
        "BlobStore deleted successfully: name='%s'.",
        blob_store_name,
    )
    return "", 204


# ===========================================================================
# Phase 7: BlobStore Quota — PUT /api/v1/blobstores/<blob_store_name>/quota
# ===========================================================================


@blobstores_bp.route("/<string:blob_store_name>/quota", methods=["PUT"])
@login_required
@require_permission("blobstores", "update")
@blobstores_bp.arguments(BlobStoreQuotaSchema)
def set_blobstore_quota(validated: Dict[str, Any], blob_store_name: str) -> tuple:
    """Set or update a storage quota (soft limit) for a BlobStore.

    Quota types:
        - ``spaceUsedQuota``: Maximum total storage in bytes.
        - ``spaceRemainingQuota``: Minimum free space in bytes.

    Quotas are **soft limits** — they trigger warnings and health check
    degradation but do not block writes.

    Args:
        validated: Deserialized request body from ``BlobStoreQuotaSchema``
            (injected by ``@blobstores_bp.arguments``).
        blob_store_name: The unique BlobStore identifier (URL path param).

    **Authentication:** Required (``blobstores:update`` privilege).

    **Request Body (JSON):**
        quota_type (str): ``'spaceUsedQuota'`` or ``'spaceRemainingQuota'``.
        quota_limit (int): Limit value in bytes.
        enabled (bool): Whether quota enforcement is enabled (default True).

    **Responses:**
        200: Quota configuration updated.
        400: Invalid quota configuration.
        404: BlobStore not found.
    """
    quota_type: str = validated["quota_type"]
    quota_limit: int = validated["quota_limit"]
    enabled: bool = validated.get("enabled", True)

    logger.info(
        "Setting quota for BlobStore '%s': type='%s', limit=%d, enabled=%s.",
        blob_store_name,
        quota_type,
        quota_limit,
        enabled,
    )

    # -- Verify BlobStore exists ---------------------------------------------
    service: BlobStoreService = _get_service()

    try:
        config: BlobStoreConfig = service.get_blobstore_or_raise(
            blob_store_name,
        )
    except BlobStoreNotFoundError as exc:
        logger.warning(
            "BlobStore not found for quota update: name='%s'. %s",
            blob_store_name,
            exc.message,
        )
        abort(404, message=exc.message)

    # -- Apply quota to BlobStore configuration -----------------------------
    current_config: Dict[str, Any] = dict(config.configuration or {})

    # Store quota settings in the configuration JSON under a 'quota' key
    current_config["quota"] = {
        "type": quota_type,
        "limit": quota_limit,
        "enabled": enabled,
    }

    # Update the configuration through the service for validation & events
    try:
        updated: BlobStoreConfig = service.update_blobstore(
            name=blob_store_name,
            configuration=current_config,
        )
    except BlobStoreConfigError as exc:
        logger.warning(
            "Quota update failed for BlobStore '%s': %s",
            blob_store_name,
            exc.message,
        )
        abort(400, message=exc.message)
    except BlobStoreError as exc:
        logger.error(
            "Quota update failed for BlobStore '%s': %s",
            blob_store_name,
            exc.message,
        )
        abort(500, message=exc.message)

    result: Dict[str, Any] = {
        "blob_store_name": blob_store_name,
        "quota_type": quota_type,
        "quota_limit": quota_limit,
        "enabled": enabled,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "Quota updated for BlobStore '%s': type='%s', limit=%d.",
        blob_store_name,
        quota_type,
        quota_limit,
    )
    return jsonify(result), 200


# ===========================================================================
# Phase 8: BlobStore Metrics — GET /api/v1/blobstores/<name>/metrics
# ===========================================================================


@blobstores_bp.route("/<string:blob_store_name>/metrics", methods=["GET"])
@login_required
@require_permission("blobstores", "read")
def get_blobstore_metrics(blob_store_name: str) -> tuple:
    """Retrieve real-time storage metrics for a specific BlobStore.

    Returns current statistics including total storage consumed, blob
    count, available space, and usage percentage.  Metrics are fetched
    from the runtime BlobStore instance when available, falling back to
    database-stored values.

    Args:
        blob_store_name: The unique BlobStore identifier (URL path param).

    **Authentication:** Required (``blobstores:read`` privilege).

    **Responses:**
        200: Storage metrics snapshot.
        404: BlobStore not found.
    """
    logger.info(
        "Retrieving metrics for BlobStore: name='%s'.",
        blob_store_name,
    )

    service: BlobStoreService = _get_service()

    # -- Verify BlobStore exists and get status ------------------------------
    try:
        status: Dict[str, Any] = service.get_blobstore_status(
            blob_store_name,
        )
    except BlobStoreNotFoundError as exc:
        logger.warning(
            "BlobStore not found for metrics: name='%s'. %s",
            blob_store_name,
            exc.message,
        )
        abort(404, message=exc.message)
    except BlobStoreError as exc:
        logger.error(
            "Failed to retrieve metrics for BlobStore '%s': %s",
            blob_store_name,
            exc.message,
        )
        abort(500, message=exc.message)

    # -- Build metrics response ----------------------------------------------
    metrics: Dict[str, Any] = status.get("metrics", {})
    total_size: int = metrics.get("total_size", 0)
    available_space: int = metrics.get("available_space", -1)

    # Calculate usage percentage if available_space is known
    usage_percentage: Optional[str] = None
    if available_space is not None and available_space > 0:
        total_capacity: int = total_size + available_space
        if total_capacity > 0:
            pct: float = (total_size / total_capacity) * 100.0
            usage_percentage = f"{pct:.1f}%"

    result: Dict[str, Any] = {
        "blob_store_name": blob_store_name,
        "total_size": total_size,
        "blob_count": metrics.get("blob_count", 0),
        "available_space": available_space if available_space >= 0 else None,
        "usage_percentage": usage_percentage,
        "available": status.get("available", False),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "Returned metrics for BlobStore '%s': total_size=%d, "
        "blob_count=%d, available=%s.",
        blob_store_name,
        total_size,
        metrics.get("blob_count", 0),
        status.get("available", False),
    )
    return jsonify(result), 200


# ---------------------------------------------------------------------------
# BlobStore Maintenance REST Endpoints (Feature F-203)
# ---------------------------------------------------------------------------
# These endpoints expose maintenance operations as on-demand REST actions.
# The same operations are also available via the scheduled-task system
# (task types: blobstore.compact, blobstore.purge-unused) for automated,
# recurring execution.
# ---------------------------------------------------------------------------


@blobstores_bp.route("/<string:blob_store_name>/compact", methods=["POST"])
@login_required
def compact_blobstore(blob_store_name: str) -> tuple:
    """Trigger immediate compaction for a specific BlobStore.

    Compaction permanently removes soft-deleted blobs that have exceeded
    the retention period.  Returns 202 Accepted with a summary of the
    compaction result.

    Args:
        blob_store_name: Name of the target BlobStore.

    Returns:
        Tuple of (JSON response body, 202) on success.

    Raises:
        404: BlobStore with the given name does not exist.
    """
    # Verify the BlobStore exists in the database
    config: Optional[BlobStoreConfig] = BlobStoreConfig.query.filter_by(
        blob_store_name=blob_store_name
    ).first()
    if config is None:
        abort(
            404,
            message=f"BlobStore '{blob_store_name}' not found.",
        )

    # Instantiate maintenance service and run compaction
    from src.app.storage.maintenance import BlobStoreMaintenanceService

    maintenance_svc: BlobStoreMaintenanceService = BlobStoreMaintenanceService()

    # Resolve a concrete BlobStore instance from the service layer so that
    # the maintenance service can operate on actual blob data.
    try:
        bs_service: BlobStoreService = BlobStoreService()
        blobstore_instance = bs_service.get_active_store(blob_store_name)
        maintenance_svc.register_blobstore(blob_store_name, blobstore_instance)
    except Exception:
        logger.debug(
            "Could not resolve live BlobStore instance for '%s' — "
            "maintenance service will operate in standalone mode.",
            blob_store_name,
        )

    compact_result: Dict[str, Any] = maintenance_svc.compact(blob_store_name)

    logger.info(
        "On-demand compaction triggered for BlobStore '%s': status=%s",
        blob_store_name,
        compact_result.get("status", "unknown"),
    )

    return jsonify({
        "blob_store_name": blob_store_name,
        "operation": "compact",
        "result": compact_result,
    }), 202


@blobstores_bp.route(
    "/<string:blob_store_name>/verify-integrity", methods=["POST"]
)
@login_required
def verify_blobstore_integrity(blob_store_name: str) -> tuple:
    """Trigger immediate integrity verification for a specific BlobStore.

    Performs read-only checksum validation across all blobs in the store.
    Returns 202 Accepted with a summary of the verification result.

    Args:
        blob_store_name: Name of the target BlobStore.

    Returns:
        Tuple of (JSON response body, 202) on success.

    Raises:
        404: BlobStore with the given name does not exist.
    """
    config: Optional[BlobStoreConfig] = BlobStoreConfig.query.filter_by(
        blob_store_name=blob_store_name
    ).first()
    if config is None:
        abort(
            404,
            message=f"BlobStore '{blob_store_name}' not found.",
        )

    from src.app.storage.maintenance import BlobStoreMaintenanceService

    maintenance_svc: BlobStoreMaintenanceService = BlobStoreMaintenanceService()

    try:
        bs_service: BlobStoreService = BlobStoreService()
        blobstore_instance = bs_service.get_active_store(blob_store_name)
        maintenance_svc.register_blobstore(blob_store_name, blobstore_instance)
    except Exception:
        logger.debug(
            "Could not resolve live BlobStore instance for '%s' — "
            "verification will operate in standalone mode.",
            blob_store_name,
        )

    verify_result: Dict[str, Any] = maintenance_svc.verify_integrity(
        blob_store_name,
    )

    logger.info(
        "On-demand integrity verification triggered for BlobStore '%s': "
        "status=%s",
        blob_store_name,
        verify_result.get("status", "unknown"),
    )

    return jsonify({
        "blob_store_name": blob_store_name,
        "operation": "verify-integrity",
        "result": verify_result,
    }), 202


@blobstores_bp.route(
    "/<string:blob_store_name>/cleanup-temp", methods=["POST"]
)
@login_required
def cleanup_temp_files(blob_store_name: str) -> tuple:
    """Trigger immediate temporary file cleanup for a specific BlobStore.

    Removes orphaned staging / temporary files left behind by interrupted
    write operations.  Returns 202 Accepted with a summary of the result.

    Args:
        blob_store_name: Name of the target BlobStore.

    Returns:
        Tuple of (JSON response body, 202) on success.

    Raises:
        404: BlobStore with the given name does not exist.
    """
    config: Optional[BlobStoreConfig] = BlobStoreConfig.query.filter_by(
        blob_store_name=blob_store_name
    ).first()
    if config is None:
        abort(
            404,
            message=f"BlobStore '{blob_store_name}' not found.",
        )

    from src.app.storage.maintenance import BlobStoreMaintenanceService

    maintenance_svc: BlobStoreMaintenanceService = BlobStoreMaintenanceService()

    try:
        bs_service: BlobStoreService = BlobStoreService()
        blobstore_instance = bs_service.get_active_store(blob_store_name)
        maintenance_svc.register_blobstore(blob_store_name, blobstore_instance)
    except Exception:
        logger.debug(
            "Could not resolve live BlobStore instance for '%s' — "
            "temp cleanup will operate in standalone mode.",
            blob_store_name,
        )

    cleanup_result: Dict[str, Any] = maintenance_svc.cleanup_temp_files(
        blob_store_name,
    )

    logger.info(
        "On-demand temp file cleanup triggered for BlobStore '%s': status=%s",
        blob_store_name,
        cleanup_result.get("status", "unknown"),
    )

    return jsonify({
        "blob_store_name": blob_store_name,
        "operation": "cleanup-temp",
        "result": cleanup_result,
    }), 202


# ---------------------------------------------------------------------------
# Blueprint Error Handlers
# ---------------------------------------------------------------------------
# Register blueprint-scoped error handlers for consistent JSON error
# responses across all BlobStore endpoints.
# ---------------------------------------------------------------------------


def _extract_error_message(error: Any, default: str) -> str:
    """Extract a human-readable message from an HTTP exception.

    flask-smorest's ``abort()`` stores the message in ``error.data['message']``
    while Werkzeug stores it in ``error.description``.  This helper tries
    both locations (preferring the flask-smorest location) so that custom
    messages passed to ``abort(code, message=...)`` are always surfaced.

    Args:
        error: The HTTP exception instance.
        default: Fallback message if neither location contains a string.

    Returns:
        The extracted or default error message string.
    """
    # flask-smorest stores custom messages in error.data['message']
    if hasattr(error, "data") and isinstance(error.data, dict):
        msg = error.data.get("message")
        if msg and isinstance(msg, str):
            return msg

    # Werkzeug stores message in error.description (may be the default
    # Werkzeug text, so we only use it if there is no flask-smorest data)
    desc = getattr(error, "description", None)
    if desc and isinstance(desc, str):
        return desc

    return default


@blobstores_bp.errorhandler(400)
def handle_bad_request(error: Any) -> tuple:
    """Handle HTTP 400 Bad Request errors."""
    message: str = _extract_error_message(error, "Bad request")
    return jsonify({"error": {"code": 400, "message": message}}), 400


@blobstores_bp.errorhandler(404)
def handle_not_found(error: Any) -> tuple:
    """Handle HTTP 404 Not Found errors."""
    message: str = _extract_error_message(error, "Resource not found")
    return jsonify({"error": {"code": 404, "message": message}}), 404


@blobstores_bp.errorhandler(409)
def handle_conflict(error: Any) -> tuple:
    """Handle HTTP 409 Conflict errors."""
    message: str = _extract_error_message(error, "Resource conflict")
    return jsonify({"error": {"code": 409, "message": message}}), 409


@blobstores_bp.errorhandler(500)
def handle_internal_error(error: Any) -> tuple:
    """Handle HTTP 500 Internal Server Error."""
    message: str = _extract_error_message(error, "Internal server error")
    logger.error("Internal server error in blobstores API: %s", message)
    return jsonify({"error": {"code": 500, "message": message}}), 500


# ---------------------------------------------------------------------------
# Public Export List
# ---------------------------------------------------------------------------

__all__: list[str] = ["blobstores_bp"]

# ---------------------------------------------------------------------------
# Module Load Logging
# ---------------------------------------------------------------------------

logger.debug(
    "BlobStore API blueprint module loaded — "
    "url_prefix='/api/v1/blobstores', exports: %s",
    ", ".join(__all__),
)
