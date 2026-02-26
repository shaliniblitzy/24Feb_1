"""
System Configuration REST API Blueprint — Features F-404, F-501-RQ-004, F-403.

This module implements the system management REST API endpoints for the
Sonatype Nexus Repository Flask application, replacing the Java System
management REST API resource classes from the original source system.

**Endpoints:**

+--------+-------------------------------------------+--------------------------+
| Method | URL                                       | Description              |
+========+===========================================+==========================+
| GET    | /api/v1/system/status                     | System status overview   |
+--------+-------------------------------------------+--------------------------+
| GET    | /api/v1/system/config                     | List all config entries  |
+--------+-------------------------------------------+--------------------------+
| GET    | /api/v1/system/config/<key>               | Get single config value  |
+--------+-------------------------------------------+--------------------------+
| PUT    | /api/v1/system/config/<key>               | Update config value      |
+--------+-------------------------------------------+--------------------------+
| DELETE | /api/v1/system/config/<key>               | Delete config entry      |
+--------+-------------------------------------------+--------------------------+
| POST   | /api/v1/system/support-zip                | Generate support ZIP     |
+--------+-------------------------------------------+--------------------------+
| GET    | /api/v1/system/license                    | License information      |
+--------+-------------------------------------------+--------------------------+
| POST   | /api/v1/system/config/email/verify        | Verify SMTP config       |
+--------+-------------------------------------------+--------------------------+

**Feature Coverage:**

- **F-404** — System Configuration Management (CRUD for system settings)
- **F-501-RQ-004** — System Management REST API
- **F-403** — Support ZIP Generation (diagnostic bundle download)
- **F-401** — Health Checks and Monitoring (system status endpoint)

**Architecture Context:**

Replaces the Java system management REST API resource classes from the
RESTEasy 6.2.7 JAX-RS framework.  Each endpoint is implemented as a
Flask-smorest decorated function-based view with automatic OpenAPI 3.x
documentation generation and Marshmallow schema serialization.

**Security:**

All endpoints require authentication (``@login_required``).  Endpoints
that modify configuration require ``require_permission('settings', 'update')``.
The support ZIP endpoint requires ``require_permission('system', 'admin')``.
Sensitive configuration values (passwords, secrets, tokens, keys) are
automatically masked in API responses.

**Event-Driven Architecture:**

Configuration changes (PUT/DELETE) emit ``EventType.CONFIG_CHANGED`` events
via the Blinker-based event bus, triggering cache invalidation, audit
logging (Feature F-303), and webhook dispatch (Feature F-503).

Exports:
    system_bp : flask_smorest.Blueprint
        The system API blueprint, registered at ``/api/v1/system``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard Library Imports
# ---------------------------------------------------------------------------

import importlib.metadata
import logging
import platform
import smtplib
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Third-Party Imports
# ---------------------------------------------------------------------------

import flask
from flask import current_app, g, request, send_file
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields as ma_fields

# ---------------------------------------------------------------------------
# Internal Imports
# ---------------------------------------------------------------------------

from src.app.extensions import db
from src.app.models.system_config import SystemConfig
from src.app.schemas.system import (
    LicenseInfoSchema,
    SystemConfigSchema,
    SystemConfigUpdateSchema,
    SystemStatusSchema,
)
from src.app.services.config_service import ConfigService
from src.app.services.support_zip_service import SupportZipService
from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# Provides structured logging for configuration CRUD operations, system
# status requests, support ZIP generation, and email verification.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-Level Constants
# ---------------------------------------------------------------------------

_start_time: float = time.monotonic()
"""Monotonic clock snapshot at module import time for uptime calculation.
Using monotonic clock prevents wall-clock drift and NTP jump issues."""

_start_datetime: datetime = datetime.now(timezone.utc)
"""UTC datetime captured at module import time for ``started_at`` reporting."""

_SENSITIVE_PATTERNS: List[str] = [
    "password",
    "secret",
    "token",
    "key",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
    "secret_key",
]
"""Substrings (case-insensitive) that identify a configuration key as
sensitive.  Any config key whose lowercase representation contains one
of these patterns will have its value replaced with ``_MASK`` in API
responses to prevent credential leakage."""

_MASK: str = "********"
"""Replacement string for sensitive configuration values in API responses."""

_OSS_FEATURES: List[str] = [
    "repository-management",
    "multi-format-support",
    "hosted-repositories",
    "proxy-repositories",
    "group-repositories",
    "security-rbac",
    "content-selectors",
    "search",
    "cleanup-policies",
    "scheduled-tasks",
    "rest-api",
    "health-monitoring",
    "audit-logging",
    "file-blobstore",
]
"""Feature identifiers enabled by the OSS (open-source) edition license."""

_VALID_CONFIG_CATEGORIES: List[str] = [
    "security",
    "repository",
    "storage",
    "email",
    "http",
    "system",
    "cleanup",
    "scheduling",
    "network",
]
"""Valid system configuration category identifiers for API filtering."""


# ===========================================================================
# Blueprint Definition
# ===========================================================================

system_bp: Blueprint = Blueprint(
    "system",
    __name__,
    url_prefix="/api/v1/system",
    description="System configuration and management (F-404, F-501-RQ-004)",
)
"""Flask-smorest Blueprint for the system management API endpoint group.

Registered at ``/api/v1/system`` by the application factory in
``src.app.api.register_api_blueprints()``.

**CRITICAL**: Export name MUST be ``system_bp`` as specified in the AAP.
"""


# ===========================================================================
# Inline Query Parameter Schemas
# ===========================================================================


class ConfigListQuerySchema(Schema):
    """Query parameter schema for the config listing endpoint.

    Allows filtering system configuration entries by category, providing
    the admin UI the ability to display categorised settings pages
    (e.g., all ``'security'`` settings, all ``'email'`` settings).

    Attributes:
        category: Optional category filter.  When provided, only config
            entries matching this category are returned.
    """

    category = ma_fields.String(
        load_default=None,
        metadata={
            "description": (
                "Filter configuration entries by category. "
                "Valid values: 'security', 'repository', 'storage', "
                "'email', 'http', 'system', 'cleanup', 'scheduling'."
            ),
        },
    )


class EmailVerifyRequestSchema(Schema):
    """Request schema for the email/SMTP verification endpoint.

    Allows the caller to optionally provide an explicit recipient address
    for the test email.  If omitted, the SMTP connection is tested without
    sending a message.

    Attributes:
        recipient: Optional email address to send a test message to.
    """

    recipient = ma_fields.String(
        load_default=None,
        metadata={
            "description": "Optional recipient email for the test message.",
        },
    )


class EmailVerifyResponseSchema(Schema):
    """Response schema for the email/SMTP verification endpoint.

    Attributes:
        success: Whether the SMTP verification succeeded.
        message: Human-readable status message.
        smtp_host: The SMTP host that was tested.
        smtp_port: The SMTP port that was tested.
    """

    success = ma_fields.Boolean(
        dump_only=True,
        metadata={"description": "Whether SMTP verification succeeded."},
    )
    message = ma_fields.String(
        dump_only=True,
        metadata={"description": "Human-readable verification result message."},
    )
    smtp_host = ma_fields.String(
        dump_only=True,
        allow_none=True,
        metadata={"description": "SMTP server hostname tested."},
    )
    smtp_port = ma_fields.Integer(
        dump_only=True,
        allow_none=True,
        metadata={"description": "SMTP server port tested."},
    )


class MessageResponseSchema(Schema):
    """Generic message response schema for simple acknowledgments.

    Attributes:
        message: Human-readable status message.
    """

    message = ma_fields.String(
        dump_only=True,
        metadata={"description": "Human-readable status message."},
    )


# ===========================================================================
# Helper Functions
# ===========================================================================


def _is_sensitive_key(key: str) -> bool:
    """Determine whether a configuration key is sensitive.

    A key is considered sensitive if its lowercase representation contains
    any of the substrings defined in :data:`_SENSITIVE_PATTERNS`.  This
    supplements :meth:`ConfigService.is_sensitive_key` (which uses exact
    matching) with a broader pattern-based check per the AAP requirement
    that config values containing 'password', 'secret', 'token', 'key'
    must be masked in responses.

    Args:
        key: Dot-notation configuration key to check.

    Returns:
        ``True`` if the key matches any sensitive pattern.
    """
    key_lower: str = key.lower()
    return any(pattern in key_lower for pattern in _SENSITIVE_PATTERNS)


def _mask_config_entry(config: SystemConfig) -> Dict[str, Any]:
    """Convert a SystemConfig model instance to a dict with sensitive masking.

    If the configuration key matches any sensitive pattern, the value is
    replaced with :data:`_MASK` to prevent credential leakage in API
    responses.

    Args:
        config: A SystemConfig SQLAlchemy model instance.

    Returns:
        A dictionary suitable for Marshmallow schema serialization.
    """
    masked_value: Optional[str] = config.value
    if config.key and _is_sensitive_key(config.key):
        masked_value = _MASK

    return {
        "key": config.key,
        "value": masked_value,
        "category": config.category,
        "description": getattr(config, "description", None),
        "created_at": getattr(config, "created_at", None),
        "updated_at": getattr(config, "updated_at", None),
    }


def _detect_database_type() -> str:
    """Detect the active database backend from the SQLAlchemy database URI.

    Inspects ``SQLALCHEMY_DATABASE_URI`` from the Flask app configuration
    to determine whether the application is using SQLite (standalone) or
    PostgreSQL (clustered/enterprise).

    Returns:
        ``'sqlite'`` or ``'postgresql'``.  Defaults to ``'sqlite'`` if
        detection fails.
    """
    try:
        db_uri: str = current_app.config.get("SQLALCHEMY_DATABASE_URI", "")
        if "postgresql" in db_uri.lower() or "postgres" in db_uri.lower():
            return "postgresql"
        return "sqlite"
    except RuntimeError:
        return "sqlite"


def _get_flask_version() -> str:
    """Return the installed Flask framework version string.

    Uses ``importlib.metadata`` (preferred) with a fallback to
    ``flask.__version__`` for environments where package metadata is
    unavailable.

    Returns:
        The Flask version string, e.g. ``'3.1.3'``.
    """
    try:
        return importlib.metadata.version("flask")
    except importlib.metadata.PackageNotFoundError:
        return getattr(flask, "__version__", "unknown")


def _get_config_service() -> ConfigService:
    """Retrieve or create a ConfigService instance.

    Looks for a persistent ConfigService stored in
    ``current_app.extensions['config_service']``.  If not found, creates
    a new instance.

    Returns:
        A :class:`ConfigService` instance.
    """
    try:
        svc: Optional[ConfigService] = current_app.extensions.get(
            "config_service"
        )
        if svc is not None:
            return svc
    except RuntimeError:
        pass
    return ConfigService()


def _get_entity_count(model_class: Any) -> int:
    """Safely count rows in a database table.

    Args:
        model_class: A SQLAlchemy model class with a ``query`` attribute.

    Returns:
        Row count, or ``0`` if the query fails (e.g. table does not exist
        yet during early startup).
    """
    try:
        return db.session.query(model_class).count()
    except Exception:
        logger.debug(
            "Failed to count rows for %s; returning 0.",
            getattr(model_class, "__tablename__", "unknown"),
        )
        return 0


# ===========================================================================
# Endpoint 1 — System Status (GET /api/v1/system/status)
# ===========================================================================


@system_bp.route("/status", methods=["GET"])
@system_bp.response(200, SystemStatusSchema)
@login_required
def get_system_status() -> Dict[str, Any]:
    """Return system status and health overview.

    Provides runtime metadata including application version, edition,
    uptime, database type, and aggregate statistics.  Replaces the Java
    system status endpoint from the RESTEasy management API.

    **Authentication required** — any authenticated user may access this
    endpoint.

    HTTP Status Codes:
        200: System status returned successfully.
        401: Authentication required.

    Returns:
        A dictionary serialized via :class:`SystemStatusSchema`.
    """
    logger.debug("System status requested by user=%s",
                 getattr(g, "current_user", None))

    uptime_seconds: int = int(time.monotonic() - _start_time)

    # Detect Elasticsearch availability
    es_available: bool = False
    try:
        es_url: str = current_app.config.get("ELASTICSEARCH_URL", "")
        if es_url:
            es_available = True
    except RuntimeError:
        pass

    # Collect entity counts safely — import models only for counting
    blob_store_count: int = 0
    repository_count: int = 0
    component_count: int = 0
    asset_count: int = 0
    user_count: int = 0

    try:
        from src.app.models.blobstore_config import BlobStoreConfig
        blob_store_count = _get_entity_count(BlobStoreConfig)
    except ImportError:
        logger.debug("BlobStoreConfig model not available for counting.")

    try:
        from src.app.models.repository import Repository
        repository_count = _get_entity_count(Repository)
    except ImportError:
        logger.debug("Repository model not available for counting.")

    try:
        from src.app.models.component import Component
        component_count = _get_entity_count(Component)
    except ImportError:
        logger.debug("Component model not available for counting.")

    try:
        from src.app.models.asset import Asset
        asset_count = _get_entity_count(Asset)
    except ImportError:
        logger.debug("Asset model not available for counting.")

    try:
        from src.app.models.user import User
        user_count = _get_entity_count(User)
    except ImportError:
        logger.debug("User model not available for counting.")

    status_data: Dict[str, Any] = {
        "version": current_app.config.get("APP_VERSION", "1.0.0"),
        "edition": current_app.config.get("APP_EDITION", "OSS"),
        "status": "running",
        "uptime_seconds": uptime_seconds,
        "started_at": _start_datetime,
        "node_id": current_app.config.get("NODE_ID", platform.node()),
        "python_version": platform.python_version(),
        "flask_version": _get_flask_version(),
        "database_type": _detect_database_type(),
        "elasticsearch_available": es_available,
        "blob_stores": blob_store_count,
        "repositories": repository_count,
        "components": component_count,
        "assets": asset_count,
        "users": user_count,
    }

    logger.info(
        "System status: version=%s, edition=%s, uptime=%ds, db=%s",
        status_data["version"],
        status_data["edition"],
        uptime_seconds,
        status_data["database_type"],
    )

    return status_data


# ===========================================================================
# Endpoint 2 — List System Configs (GET /api/v1/system/config)
# ===========================================================================


@system_bp.route("/config", methods=["GET"])
@system_bp.arguments(ConfigListQuerySchema, location="query", as_kwargs=True)
@system_bp.response(200, SystemConfigSchema(many=True))
@login_required
@require_permission("settings", "read")
def list_configs(*, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all system configuration entries with optional category filter.

    Returns key-value configuration pairs with sensitive values masked.
    Supports filtering by category (e.g., ``'security'``, ``'repository'``,
    ``'storage'``) via the ``category`` query parameter.

    Delegates to :meth:`ConfigService.get_by_category` for category-filtered
    queries and :meth:`ConfigService.get_all_masked` for full listings with
    sensitive value masking.

    **Requires** ``settings:read`` permission.

    Query Parameters:
        category: Optional category filter string.

    HTTP Status Codes:
        200: Configuration entries returned successfully.
        401: Authentication required.
        403: Insufficient privileges (settings:read required).

    Returns:
        A list of dictionaries serialized via :class:`SystemConfigSchema`.
    """
    logger.debug(
        "Config list requested: category=%s, user=%s",
        category,
        getattr(getattr(g, "current_user", None), "user_id", "unknown"),
    )

    config_service: ConfigService = _get_config_service()

    try:
        if category:
            # Validate category
            if category not in _VALID_CONFIG_CATEGORIES:
                logger.warning(
                    "Invalid category filter requested: '%s'", category
                )
                # Still allow the query — may return empty results for
                # custom categories.  Log a warning but don't abort.

            # Use ConfigService.get_by_category() for optimised category
            # filtering with parsed values
            category_values: Dict[str, Any] = config_service.get_by_category(
                category
            )

            # Fetch full model objects to include metadata (description,
            # timestamps) — filter by category and optionally by non-null
            # SystemConfig.value for entries that have been explicitly set
            configs: List[SystemConfig] = (
                SystemConfig.query
                .filter(SystemConfig.category == category)
                .order_by(SystemConfig.key)
                .all()
            )
        else:
            # Use ConfigService.get_all_masked() for the full listing with
            # automatic sensitive value masking
            masked_values: Dict[str, Any] = config_service.get_all_masked()
            logger.debug(
                "ConfigService.get_all_masked() returned %d entries.",
                len(masked_values),
            )

            configs = (
                SystemConfig.query
                .order_by(SystemConfig.key)
                .all()
            )
    except Exception as exc:
        logger.error("Database error listing configs: %s", str(exc))
        abort(500, message="Failed to retrieve system configuration.")

    # Build result list with sensitive value masking.  For entries where
    # SystemConfig.value is None, the config is reported with null value.
    result: List[Dict[str, Any]] = [
        _mask_config_entry(config) for config in configs
    ]

    logger.info(
        "Returned %d config entries (category=%s).",
        len(result),
        category or "all",
    )

    return result


# ===========================================================================
# Endpoint 3 — Get Config Value (GET /api/v1/system/config/<key>)
# ===========================================================================


@system_bp.route("/config/<key>", methods=["GET"])
@system_bp.response(200, SystemConfigSchema)
@login_required
@require_permission("settings", "read")
def get_config(key: str) -> Dict[str, Any]:
    """Retrieve a specific configuration value by its dot-notation key.

    Sensitive values (passwords, tokens, secrets, keys) are automatically
    masked in the response per AAP security requirements.

    **Requires** ``settings:read`` permission.

    Path Parameters:
        key: Dot-notation configuration key (e.g., ``'security.anonymousAccess'``).

    HTTP Status Codes:
        200: Configuration entry returned successfully.
        401: Authentication required.
        403: Insufficient privileges (settings:read required).
        404: Configuration key not found.

    Returns:
        A dictionary serialized via :class:`SystemConfigSchema`.
    """
    logger.debug("Config get requested: key='%s'", key)

    try:
        config: Optional[SystemConfig] = db.session.get(SystemConfig, key)
    except Exception as exc:
        logger.error(
            "Database error retrieving config key '%s': %s", key, str(exc)
        )
        abort(500, message="Failed to retrieve configuration value.")

    if config is None:
        logger.debug("Config key '%s' not found.", key)
        abort(404, message=f"Configuration key '{key}' not found.")

    result: Dict[str, Any] = _mask_config_entry(config)

    logger.info("Config retrieved: key='%s'", key)
    return result


# ===========================================================================
# Endpoint 4 — Update Config (PUT /api/v1/system/config/<key>)
# ===========================================================================


@system_bp.route("/config/<key>", methods=["PUT"])
@system_bp.arguments(SystemConfigUpdateSchema)
@system_bp.response(200, SystemConfigSchema)
@login_required
@require_permission("settings", "update")
def update_config(update_data: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Create or update a system configuration value.

    Accepts a JSON body with ``value`` (required) and ``description``
    (optional) fields.  The key is taken from the URL path parameter.

    On successful update, a ``CONFIG_CHANGED`` event is emitted via the
    Blinker event bus, triggering:

    - Cache invalidation (CacheInvalidationSubscriber)
    - Audit logging (Feature F-303)
    - Webhook dispatch (Feature F-503)

    **Requires** ``settings:update`` permission.

    Path Parameters:
        key: Dot-notation configuration key.

    Request Body:
        JSON object with ``value`` and optional ``description`` fields.

    HTTP Status Codes:
        200: Configuration value updated successfully.
        401: Authentication required.
        403: Insufficient privileges (settings:update required).
        422: Invalid request body.

    Returns:
        The updated configuration entry serialized via :class:`SystemConfigSchema`.
    """
    logger.debug(
        "Config update requested: key='%s', user=%s",
        key,
        getattr(getattr(g, "current_user", None), "user_id", "unknown"),
    )

    new_value: Optional[str] = update_data.get("value")
    new_description: Optional[str] = update_data.get("description")

    config_service: ConfigService = _get_config_service()

    try:
        # Capture old value for event payload
        old_config: Optional[SystemConfig] = db.session.get(SystemConfig, key)
        old_value: Optional[str] = old_config.value if old_config else None

        # Detect category from existing entry or auto-detect from key prefix
        category: Optional[str] = (
            old_config.category if old_config else None
        )

        # Delegate to ConfigService for transactional write with event emission
        updated_config: SystemConfig = config_service.set(
            key, new_value, category=category
        )

        # Update description if provided
        if new_description is not None and updated_config is not None:
            updated_config.description = new_description
            db.session.commit()

        # Emit additional CONFIG_CHANGED event with user context for audit trail
        user_id: str = getattr(
            getattr(g, "current_user", None), "user_id", "system"
        )
        emit_event(
            EventType.CONFIG_CHANGED,
            payload={
                "key": key,
                "old_value": old_value,
                "new_value": new_value,
                "user_id": user_id,
                "source": "api",
                "ip_address": request.remote_addr,
            },
        )

        logger.info(
            "Config updated: key='%s', user='%s', old='%s', new='%s'",
            key,
            user_id,
            old_value if not _is_sensitive_key(key) else _MASK,
            new_value if not _is_sensitive_key(key) else _MASK,
        )

        # Refresh from DB for accurate response
        refreshed: Optional[SystemConfig] = db.session.get(SystemConfig, key)
        if refreshed is None:
            abort(500, message="Failed to refresh configuration after update.")

        return _mask_config_entry(refreshed)

    except Exception as exc:
        db.session.rollback()
        logger.error(
            "Failed to update config key '%s': %s", key, str(exc),
            exc_info=True,
        )
        abort(500, message=f"Failed to update configuration key '{key}'.")


# ===========================================================================
# Endpoint 5 — Delete Config (DELETE /api/v1/system/config/<key>)
# ===========================================================================


@system_bp.route("/config/<key>", methods=["DELETE"])
@system_bp.response(204)
@login_required
@require_permission("settings", "update")
def delete_config(key: str) -> str:
    """Delete a system configuration entry by its dot-notation key.

    Removes the configuration entry from the database and in-memory cache.
    Emits a ``CONFIG_CHANGED`` event with ``new_value=None``.

    **Requires** ``settings:update`` permission.

    Path Parameters:
        key: Dot-notation configuration key.

    HTTP Status Codes:
        204: Configuration entry deleted successfully.
        401: Authentication required.
        403: Insufficient privileges (settings:update required).
        404: Configuration key not found.

    Returns:
        Empty response body with HTTP 204 status.
    """
    logger.debug(
        "Config delete requested: key='%s', user=%s",
        key,
        getattr(getattr(g, "current_user", None), "user_id", "unknown"),
    )

    config_service: ConfigService = _get_config_service()

    try:
        deleted: bool = config_service.delete(key)
    except Exception as exc:
        logger.error(
            "Failed to delete config key '%s': %s", key, str(exc),
            exc_info=True,
        )
        abort(500, message=f"Failed to delete configuration key '{key}'.")

    if not deleted:
        logger.debug("Config key '%s' not found for deletion.", key)
        abort(404, message=f"Configuration key '{key}' not found.")

    logger.info(
        "Config deleted: key='%s', user=%s",
        key,
        getattr(getattr(g, "current_user", None), "user_id", "unknown"),
    )

    return ""


# ===========================================================================
# Endpoint 6 — Support ZIP (POST /api/v1/system/support-zip)  [Feature F-403]
# ===========================================================================


@system_bp.route("/support-zip", methods=["POST"])
@login_required
@require_permission("system", "admin")
def generate_support_zip() -> Any:
    """Generate and download a diagnostic support ZIP bundle.

    Delegates to :class:`SupportZipService` which collects:

    - System configuration (sanitized — no passwords/secrets)
    - Application log files (tail-truncated to 10 MB)
    - Python thread dump (replaces Java jstack/JFR)
    - Database metrics (table row counts, connection pool stats)
    - BlobStore metrics (size, file counts)
    - Repository definitions with component counts
    - Scheduled task definitions and recent executions

    The ZIP is streamed as a downloadable response with
    ``Content-Type: application/zip``.

    **Requires** ``system:admin`` permission.

    HTTP Status Codes:
        200: Support ZIP generated and returned.
        401: Authentication required.
        403: Insufficient privileges (system:admin required).
        500: ZIP generation failed.

    Returns:
        A binary ZIP file response via :func:`flask.send_file`.
    """
    logger.info(
        "Support ZIP generation requested by user=%s, ip=%s",
        getattr(getattr(g, "current_user", None), "user_id", "unknown"),
        request.remote_addr,
    )

    try:
        service: SupportZipService = SupportZipService()
        buffer, filename = service.generate_support_zip()

        logger.info(
            "Support ZIP generated successfully: filename=%s", filename
        )

        return send_file(
            buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )

    except Exception as exc:
        logger.error(
            "Support ZIP generation failed: %s", str(exc), exc_info=True
        )
        abort(500, message="Failed to generate support ZIP bundle.")


# ===========================================================================
# Endpoint 7 — License Info (GET /api/v1/system/license)
# ===========================================================================


@system_bp.route("/license", methods=["GET"])
@system_bp.response(200, LicenseInfoSchema)
@login_required
def get_license_info() -> Dict[str, Any]:
    """Return license information for the current installation.

    Returns license type (OSS / PRO), enabled feature flags, expiration
    date (if applicable), and validity status.

    The OSS edition has no expiration and provides the base feature set.
    The PRO edition includes additional features (high-availability,
    S3 BlobStore, SAML/SSO) and has a license expiration date.

    **Authentication required** — any authenticated user may access this
    endpoint.

    HTTP Status Codes:
        200: License information returned successfully.
        401: Authentication required.

    Returns:
        A dictionary serialized via :class:`LicenseInfoSchema`.
    """
    logger.debug("License info requested.")

    edition: str = current_app.config.get("APP_EDITION", "OSS")

    # Determine features based on edition
    features: List[str] = list(_OSS_FEATURES)
    if edition == "PRO":
        features.extend([
            "high-availability",
            "s3-blobstore",
            "saml-sso",
            "ldap-integration",
            "staging-repositories",
            "custom-metadata",
        ])

    # Determine license type and expiration
    license_type: str = "open-source" if edition == "OSS" else "commercial"
    expiration_date: Optional[datetime] = None
    contact_email: Optional[str] = None

    if edition == "PRO":
        # PRO license details from configuration
        expiration_str: Optional[str] = current_app.config.get(
            "LICENSE_EXPIRATION"
        )
        if expiration_str:
            try:
                expiration_date = datetime.fromisoformat(expiration_str)
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid LICENSE_EXPIRATION format: '%s'", expiration_str
                )
        contact_email = current_app.config.get(
            "LICENSE_CONTACT_EMAIL", "support@sonatype.com"
        )

    # Determine validity
    is_valid: bool = True
    if expiration_date and expiration_date < datetime.now(timezone.utc):
        is_valid = False

    license_data: Dict[str, Any] = {
        "product_name": "Sonatype Nexus Repository",
        "edition": edition,
        "license_type": license_type,
        "features": features,
        "expiration_date": expiration_date,
        "is_valid": is_valid,
        "contact_email": contact_email,
    }

    logger.info(
        "License info: edition=%s, type=%s, valid=%s",
        edition,
        license_type,
        is_valid,
    )

    return license_data


# ===========================================================================
# Endpoint 8 — Email Verification (POST /api/v1/system/config/email/verify)
# ===========================================================================


@system_bp.route("/config/email/verify", methods=["POST"])
@system_bp.arguments(EmailVerifyRequestSchema)
@system_bp.response(200, EmailVerifyResponseSchema)
@login_required
@require_permission("settings", "update")
def verify_email_config(
    verify_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Verify SMTP email configuration by testing the connection.

    Reads the current email/SMTP configuration from the system settings
    and attempts to establish a connection to the configured SMTP server.
    Optionally sends a test email to the specified recipient.

    **Requires** ``settings:update`` permission.

    Request Body:
        JSON object with optional ``recipient`` field.

    HTTP Status Codes:
        200: Verification result returned (check ``success`` field).
        401: Authentication required.
        403: Insufficient privileges (settings:update required).

    Returns:
        A dictionary with verification results serialized via
        :class:`EmailVerifyResponseSchema`.
    """
    logger.info(
        "Email config verification requested by user=%s",
        getattr(getattr(g, "current_user", None), "user_id", "unknown"),
    )

    config_service: ConfigService = _get_config_service()

    # Read SMTP configuration from system settings
    smtp_host: str = config_service.get_str(
        "email.smtp.host", default=""
    )
    smtp_port_str: str = config_service.get_str(
        "email.smtp.port", default="587"
    )
    smtp_username: str = config_service.get_str(
        "email.smtp.username", default=""
    )
    smtp_password: str = config_service.get_str(
        "email.smtp.password", default=""
    )
    smtp_use_tls: bool = config_service.get_bool(
        "email.smtp.use_tls", default=True
    )

    # Also check Flask app config as fallback
    if not smtp_host:
        smtp_host = current_app.config.get("MAIL_SERVER", "")
    if not smtp_host:
        return {
            "success": False,
            "message": (
                "SMTP host not configured. Set 'email.smtp.host' "
                "in system configuration."
            ),
            "smtp_host": None,
            "smtp_port": None,
        }

    try:
        smtp_port: int = int(smtp_port_str)
    except (ValueError, TypeError):
        smtp_port = 587

    recipient: Optional[str] = verify_data.get("recipient")

    # Attempt SMTP connection
    try:
        logger.debug(
            "Testing SMTP connection: host=%s, port=%d, tls=%s",
            smtp_host,
            smtp_port,
            smtp_use_tls,
        )

        if smtp_use_tls:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
            server.ehlo()

        # Authenticate if credentials are provided
        if smtp_username and smtp_password:
            server.login(smtp_username, smtp_password)

        # Optionally send a test email
        if recipient:
            from_addr: str = config_service.get_str(
                "email.smtp.from_address",
                default=f"nexus@{platform.node()}",
            )
            test_message: str = (
                f"Subject: Nexus Repository SMTP Test\r\n"
                f"From: {from_addr}\r\n"
                f"To: {recipient}\r\n"
                f"\r\n"
                f"This is a test email from Sonatype Nexus Repository "
                f"to verify SMTP configuration.\r\n"
                f"Sent at: {datetime.now(timezone.utc).isoformat()}\r\n"
            )
            server.sendmail(from_addr, [recipient], test_message)
            logger.info("Test email sent to %s", recipient)

        server.quit()

        success_msg: str = "SMTP connection successful."
        if recipient:
            success_msg = f"SMTP connection successful. Test email sent to {recipient}."

        logger.info(
            "SMTP verification succeeded: host=%s, port=%d",
            smtp_host,
            smtp_port,
        )

        return {
            "success": True,
            "message": success_msg,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
        }

    except smtplib.SMTPAuthenticationError as exc:
        error_msg: str = f"SMTP authentication failed: {str(exc)}"
        logger.warning("SMTP auth error: %s", error_msg)
        return {
            "success": False,
            "message": error_msg,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
        }

    except smtplib.SMTPConnectError as exc:
        error_msg = f"SMTP connection failed: {str(exc)}"
        logger.warning("SMTP connect error: %s", error_msg)
        return {
            "success": False,
            "message": error_msg,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
        }

    except smtplib.SMTPException as exc:
        error_msg = f"SMTP error: {str(exc)}"
        logger.warning("SMTP error: %s", error_msg)
        return {
            "success": False,
            "message": error_msg,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
        }

    except OSError as exc:
        error_msg = f"Network error connecting to SMTP server: {str(exc)}"
        logger.warning("SMTP network error: %s", error_msg)
        return {
            "success": False,
            "message": error_msg,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
        }

    except Exception as exc:
        error_msg = f"Unexpected error during SMTP verification: {str(exc)}"
        logger.error("SMTP verification error: %s", error_msg, exc_info=True)
        return {
            "success": False,
            "message": error_msg,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
        }


# ===========================================================================
# Module Load Logging
# ===========================================================================

logger.debug(
    "System API module loaded — blueprint: %s (prefix: %s), "
    "endpoints: status, config, config/<key>, support-zip, license, "
    "config/email/verify",
    system_bp.name,
    system_bp.url_prefix,
)
