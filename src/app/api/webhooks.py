"""
Webhook Configuration REST API Blueprint — Feature F-503 (Webhook Integration).

This module implements the webhook management REST API endpoints for the
Sonatype Nexus Repository Flask application.  It provides a complete CRUD
interface for configuring outbound HTTP POST webhooks that fire when specific
application events occur (repository changes, component uploads, asset
downloads, etc.).

**Replaces:**
    - Java webhook integration REST API resource classes
    - RESTEasy 6.2.7 JAX-RS webhook endpoints
    - Jackson 2.16.1 webhook DTO serialization

**Architecture Context (AAP Section 0.4.3 — Observer Pattern):**
    The webhook API delegates all dispatch logic to the
    :class:`~src.app.webhooks.dispatcher.WebhookDispatcher`, which is stored
    in ``current_app.extensions['webhook_dispatcher']``.  This module is
    responsible only for HTTP request/response handling, input validation,
    and authorization enforcement.

**Endpoints:**

    GET    /api/v1/webhooks                      — List all webhooks
    GET    /api/v1/webhooks/<webhook_id>          — Get specific webhook
    POST   /api/v1/webhooks                      — Create a new webhook
    PUT    /api/v1/webhooks/<webhook_id>          — Update an existing webhook
    DELETE /api/v1/webhooks/<webhook_id>          — Delete a webhook
    POST   /api/v1/webhooks/<webhook_id>/test     — Send test delivery
    GET    /api/v1/webhooks/<webhook_id>/deliveries — Recent delivery history
    GET    /api/v1/webhooks/event-types           — Available event types
    GET    /api/v1/webhooks/stats                 — Delivery statistics

**Security:**
    All endpoints require authentication via :func:`login_required` and
    RBAC authorization via :func:`require_permission` with the ``'webhooks'``
    domain and appropriate action (``'read'``, ``'create'``, ``'update'``,
    ``'delete'``).

**Secret Key Masking:**
    Secret keys are never exposed in full in API responses.  Only the first
    4 and last 4 characters are shown, with the middle replaced by asterisks.
    Secrets shorter than 10 characters are fully masked.

Exports:
    webhooks_bp : flask_smorest.Blueprint
        The webhooks API blueprint, registered at ``/api/v1/webhooks``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import current_app, jsonify, request
from flask_smorest import Blueprint, abort
from marshmallow import Schema, fields, validate

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.events.event_types import EventType
from src.app.extensions import db
from src.app.webhooks.dispatcher import (
    WebhookConfig,
    WebhookDeliveryResult,
    WebhookDispatcher,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source system.  Provides structured logging for webhook CRUD operations,
# test delivery attempts, and error conditions.
# Required by AAP Section 0.7.2 for JSON-structured SIEM-compatible logging.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ===========================================================================
# Blueprint Definition
# ===========================================================================

webhooks_bp: Blueprint = Blueprint(
    "webhooks",
    __name__,
    url_prefix="/api/v1/webhooks",
    description="Webhook configuration for event-driven integrations (F-503)",
)
"""Flask-smorest Blueprint for the webhooks API endpoint group.

Registered at ``/api/v1/webhooks`` by the application factory
(``src.app.factory.create_app``).  Provides OpenAPI 3.x documentation
auto-generation via flask-smorest 0.45.0, replacing RESTEasy 6.2.7 +
Swagger/OpenAPI 2.2.20 from the Java source system.
"""

# ===========================================================================
# Marshmallow Schemas — Inline Request/Response Serialization
# ===========================================================================
# These schemas replace Jackson 2.16.1 JSON serialization from the Java
# source system.  Defined inline because no dedicated webhook schema file
# exists in the schema package.  Used with flask-smorest's ``@blp.arguments``
# and ``@blp.response`` decorators for automatic OpenAPI documentation.
# ===========================================================================


def _get_valid_event_type_values() -> List[str]:
    """Return all valid event type string values from the EventType enum.

    Used for Marshmallow ``OneOf`` validation on the ``event_types`` field
    in webhook create/update request schemas.

    Returns:
        A sorted list of all EventType member values (dotted strings).
    """
    return sorted(member.value for member in EventType)


class WebhookCreateSchema(Schema):
    """Marshmallow schema for POST /api/v1/webhooks request body.

    Validates and deserializes webhook creation payloads.  All required
    fields are enforced; optional fields use sensible defaults.

    Attributes:
        name:              Unique webhook identifier (required).
        url:               Target URL for HTTP POST delivery (required).
        event_types:       List of event type strings to subscribe to (required).
        secret:            HMAC-SHA256 signing key (optional).
        content_type:      Content-Type header value (optional, default JSON).
        enabled:           Whether the webhook is active (optional, default True).
        repository_filter: List of repository names to filter events on (optional).
        max_retries:       Maximum retry attempts for failed deliveries (optional).
    """

    name = fields.String(
        required=True,
        validate=validate.Length(min=1, max=255),
        metadata={"description": "Unique webhook name / identifier"},
    )
    url = fields.String(
        required=True,
        validate=validate.URL(schemes={"http", "https"}),
        metadata={"description": "Target URL for HTTP POST delivery"},
    )
    event_types = fields.List(
        fields.String(
            validate=validate.OneOf(_get_valid_event_type_values()),
        ),
        required=True,
        validate=validate.Length(min=1),
        metadata={
            "description": "List of event types to subscribe to",
            "example": ["repository.created", "component.uploaded"],
        },
    )
    secret = fields.String(
        load_default=None,
        metadata={
            "description": (
                "HMAC-SHA256 signing key for payload verification. "
                "When set, each delivery includes an "
                "X-Hub-Signature-256 header."
            ),
        },
    )
    content_type = fields.String(
        load_default="application/json",
        metadata={"description": "Content-Type header for webhook POST body"},
    )
    enabled = fields.Boolean(
        load_default=True,
        metadata={"description": "Whether the webhook is active"},
    )
    repository_filter = fields.List(
        fields.String(),
        load_default=[],
        metadata={
            "description": (
                "Optional list of repository names to filter events. "
                "An empty list means all repositories."
            ),
        },
    )
    max_retries = fields.Integer(
        load_default=3,
        validate=validate.Range(min=0, max=10),
        metadata={
            "description": "Maximum retry attempts for failed deliveries"
        },
    )


class WebhookUpdateSchema(Schema):
    """Marshmallow schema for PUT /api/v1/webhooks/<webhook_id> request body.

    All fields are optional — only provided fields will be updated.
    This allows partial updates (PATCH-like semantics on a PUT endpoint).

    Attributes:
        name:              Updated webhook name.
        url:               Updated target URL.
        event_types:       Updated event type subscriptions.
        secret:            Updated signing key (or null to remove).
        content_type:      Updated Content-Type.
        enabled:           Updated activation flag.
        repository_filter: Updated repository filter list.
        max_retries:       Updated retry count.
    """

    name = fields.String(
        validate=validate.Length(min=1, max=255),
        metadata={"description": "Updated webhook name"},
    )
    url = fields.String(
        validate=validate.URL(schemes={"http", "https"}),
        metadata={"description": "Updated target URL"},
    )
    event_types = fields.List(
        fields.String(
            validate=validate.OneOf(_get_valid_event_type_values()),
        ),
        validate=validate.Length(min=1),
        metadata={"description": "Updated event type subscriptions"},
    )
    secret = fields.String(
        allow_none=True,
        metadata={
            "description": (
                "Updated HMAC-SHA256 signing key. Set to null to remove."
            ),
        },
    )
    content_type = fields.String(
        metadata={"description": "Updated Content-Type header"},
    )
    enabled = fields.Boolean(
        metadata={"description": "Updated activation flag"},
    )
    repository_filter = fields.List(
        fields.String(),
        metadata={"description": "Updated repository filter list"},
    )
    max_retries = fields.Integer(
        validate=validate.Range(min=0, max=10),
        metadata={"description": "Updated maximum retry count"},
    )


class WebhookResponseSchema(Schema):
    """Marshmallow schema for webhook configuration in API responses.

    Used to serialize :class:`WebhookConfig` objects into JSON responses.
    The ``secret`` field is masked — full secret values are never exposed.

    Attributes:
        name:               Webhook name / identifier.
        url:                Target URL.
        event_types:        Subscribed event types.
        secret:             Masked secret (first/last 4 chars visible).
        content_type:       Content-Type header value.
        enabled:            Whether the webhook is active.
        repository_filter:  Repository filter list.
        max_retries:        Maximum retry count.
        created_at:         ISO 8601 creation timestamp.
        last_delivery_status: Most recent delivery success/failure.
        last_delivery_time:  Most recent delivery timestamp.
    """

    name = fields.String(metadata={"description": "Webhook name"})
    url = fields.String(metadata={"description": "Target URL"})
    event_types = fields.List(
        fields.String(),
        metadata={"description": "Subscribed event types"},
    )
    secret = fields.String(
        metadata={"description": "Masked secret key (first/last 4 chars)"},
    )
    content_type = fields.String(
        metadata={"description": "Content-Type for POST body"},
    )
    enabled = fields.Boolean(metadata={"description": "Active flag"})
    repository_filter = fields.List(
        fields.String(),
        metadata={"description": "Repository filter"},
    )
    max_retries = fields.Integer(
        metadata={"description": "Max retry attempts"},
    )
    created_at = fields.String(
        metadata={"description": "ISO 8601 creation timestamp"},
    )
    last_delivery_status = fields.String(
        allow_none=True,
        metadata={"description": "Last delivery status (success/failure)"},
    )
    last_delivery_time = fields.String(
        allow_none=True,
        metadata={"description": "ISO 8601 last delivery timestamp"},
    )


class WebhookDeliveryResponseSchema(Schema):
    """Marshmallow schema for webhook delivery result in API responses.

    Serializes :class:`WebhookDeliveryResult` objects for the delivery
    history and test delivery endpoints.

    Attributes:
        webhook_name: Name of the targeted webhook.
        event_type:   Event type that triggered the delivery.
        delivery_id:  Unique delivery UUID.
        success:      Whether the delivery succeeded (2xx response).
        status_code:  HTTP response status code.
        response_body: Truncated response body (max 500 chars).
        error_message: Error description on failure.
        attempt:      1-based attempt number.
        total_attempts: Total attempts including retries.
        duration_ms:  Request duration in milliseconds.
        timestamp:    ISO 8601 timestamp of the delivery.
    """

    webhook_name = fields.String(
        metadata={"description": "Webhook name"},
    )
    event_type = fields.String(
        metadata={"description": "Event type"},
    )
    delivery_id = fields.String(
        metadata={"description": "Unique delivery UUID"},
    )
    success = fields.Boolean(
        metadata={"description": "Whether delivery succeeded"},
    )
    status_code = fields.Integer(
        allow_none=True,
        metadata={"description": "HTTP response status code"},
    )
    response_body = fields.String(
        allow_none=True,
        metadata={"description": "Truncated response body"},
    )
    error_message = fields.String(
        allow_none=True,
        metadata={"description": "Error description on failure"},
    )
    attempt = fields.Integer(
        metadata={"description": "Attempt number (1-based)"},
    )
    total_attempts = fields.Integer(
        metadata={"description": "Total attempts including retries"},
    )
    duration_ms = fields.Integer(
        metadata={"description": "Request duration in milliseconds"},
    )
    timestamp = fields.String(
        metadata={"description": "ISO 8601 delivery timestamp"},
    )


class WebhookDeliveryStatsSchema(Schema):
    """Marshmallow schema for aggregated delivery statistics.

    Serializes the result of :meth:`WebhookDispatcher.get_delivery_stats`.

    Attributes:
        total_deliveries: Total entries in the delivery log.
        successful:       Count of successful deliveries.
        failed:           Count of failed deliveries.
        success_rate:     Percentage of successful deliveries.
        avg_duration_ms:  Average delivery duration in ms.
        active_webhooks:  Number of currently registered webhooks.
    """

    total_deliveries = fields.Integer(
        metadata={"description": "Total delivery attempts logged"},
    )
    successful = fields.Integer(
        metadata={"description": "Successful deliveries"},
    )
    failed = fields.Integer(
        metadata={"description": "Failed deliveries"},
    )
    success_rate = fields.Float(
        metadata={"description": "Success rate percentage"},
    )
    avg_duration_ms = fields.Float(
        metadata={"description": "Average duration in milliseconds"},
    )
    active_webhooks = fields.Integer(
        metadata={"description": "Number of registered webhooks"},
    )


class EventTypeResponseSchema(Schema):
    """Marshmallow schema for the available event types response.

    Attributes:
        event_types: List of available event type strings.
        count:       Total number of available event types.
    """

    event_types = fields.List(
        fields.String(),
        metadata={"description": "Available event type values"},
    )
    count = fields.Integer(
        metadata={"description": "Total number of event types"},
    )


# ===========================================================================
# Internal Helper Functions
# ===========================================================================


def _get_dispatcher() -> WebhookDispatcher:
    """Retrieve the application's :class:`WebhookDispatcher` instance.

    Looks for a persistent dispatcher stored in
    ``current_app.extensions['webhook_dispatcher']``.  If none is found
    (e.g. during early startup or in minimal test configurations), a fresh
    default dispatcher is created and stored for reuse.

    Returns:
        The active :class:`WebhookDispatcher` instance.
    """
    try:
        dispatcher: Optional[WebhookDispatcher] = current_app.extensions.get(
            "webhook_dispatcher"
        )
        if dispatcher is not None:
            return dispatcher
    except RuntimeError:
        # Outside of application context — fall back to creating a new one.
        pass

    logger.debug(
        "No persistent WebhookDispatcher found in app extensions; "
        "creating default instance."
    )
    dispatcher = WebhookDispatcher()
    try:
        current_app.extensions["webhook_dispatcher"] = dispatcher
    except RuntimeError:
        pass
    return dispatcher


def _mask_secret(secret: Optional[str]) -> Optional[str]:
    """Mask a secret key for safe inclusion in API responses.

    Reveals only the first 4 and last 4 characters of the secret,
    replacing the middle portion with asterisks.  Secrets shorter than
    10 characters are fully masked as ``'********'`` to prevent information
    leakage for short keys.

    Args:
        secret: The raw secret string, or ``None``.

    Returns:
        The masked secret string, or ``None`` if the input was ``None``.
    """
    if secret is None:
        return None
    if len(secret) < 10:
        return "********"
    return f"{secret[:4]}{'*' * (len(secret) - 8)}{secret[-4:]}"


def _webhook_config_to_dict(
    config: WebhookConfig,
    dispatcher: WebhookDispatcher,
) -> Dict[str, Any]:
    """Serialize a :class:`WebhookConfig` to a response dictionary.

    Masks the secret key and enriches the response with last delivery
    status information from the dispatcher's delivery log.

    Args:
        config:     The webhook configuration to serialize.
        dispatcher: The dispatcher instance for delivery history lookup.

    Returns:
        A dictionary suitable for JSON serialization.
    """
    # Look up last delivery for this webhook from recent deliveries
    last_delivery_status: Optional[str] = None
    last_delivery_time: Optional[str] = None

    recent_deliveries: List[WebhookDeliveryResult] = (
        dispatcher.get_recent_deliveries(limit=200)
    )
    for delivery in reversed(recent_deliveries):
        if delivery.webhook_name == config.name:
            last_delivery_status = "success" if delivery.success else "failure"
            last_delivery_time = delivery.timestamp
            break

    return {
        "name": config.name,
        "url": config.url,
        "event_types": list(config.event_types),
        "secret": _mask_secret(config.secret),
        "content_type": config.content_type,
        "enabled": config.enabled,
        "repository_filter": list(config.repository_filter),
        "max_retries": config.max_retries,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_delivery_status": last_delivery_status,
        "last_delivery_time": last_delivery_time,
    }


def _delivery_result_to_dict(
    result: WebhookDeliveryResult,
) -> Dict[str, Any]:
    """Serialize a :class:`WebhookDeliveryResult` to a response dictionary.

    Args:
        result: The delivery result to serialize.

    Returns:
        A dictionary suitable for JSON serialization.
    """
    return {
        "webhook_name": result.webhook_name,
        "event_type": result.event_type,
        "delivery_id": result.delivery_id,
        "success": result.success,
        "status_code": result.status_code,
        "response_body": (
            result.response_body[:500]
            if result.response_body
            else None
        ),
        "error_message": result.error_message,
        "attempt": result.attempt,
        "total_attempts": result.total_attempts,
        "duration_ms": result.duration_ms,
        "timestamp": result.timestamp,
    }


def _validate_event_types(event_types: List[str]) -> List[str]:
    """Validate that all event type strings are valid EventType values.

    Args:
        event_types: List of event type strings from user input.

    Returns:
        The validated list of event type strings.

    Raises:
        Calls ``abort(400)`` if any event type is invalid.
    """
    valid_types: set = {member.value for member in EventType}
    invalid: List[str] = [et for et in event_types if et not in valid_types]
    if invalid:
        abort(
            400,
            message=(
                f"Invalid event types: {invalid}. "
                f"Valid types: {sorted(valid_types)}"
            ),
        )
    return event_types


# ===========================================================================
# Route Handlers
# ===========================================================================


# ---------------------------------------------------------------------------
# GET /api/v1/webhooks/event-types — Available Event Types
# ---------------------------------------------------------------------------
# This endpoint is placed BEFORE the parameterized /<webhook_id> routes to
# ensure Flask does not interpret "event-types" as a webhook_id parameter.
# ---------------------------------------------------------------------------


@webhooks_bp.route("/event-types", methods=["GET"])
@login_required
@require_permission("webhooks", "read")
def list_event_types() -> Any:
    """List all available event types for webhook subscriptions.

    Returns the complete set of :class:`EventType` enum values that
    webhooks can subscribe to.  Uses the EventType enum members
    ``REPOSITORY_CREATED``, ``REPOSITORY_UPDATED``, ``REPOSITORY_DELETED``,
    ``COMPONENT_UPLOADED``, ``COMPONENT_DELETED``, ``ASSET_DOWNLOADED``,
    and all other defined event types.

    Returns:
        JSON response with ``event_types`` list and ``count``.

    Response Codes:
        200: Success — event types returned.
        401: Authentication required.
        403: Insufficient privileges.
    """
    logger.debug("Listing available event types for webhook subscriptions.")
    event_type_values: List[str] = _get_valid_event_type_values()
    return jsonify({
        "event_types": event_type_values,
        "count": len(event_type_values),
    })


# ---------------------------------------------------------------------------
# GET /api/v1/webhooks/stats — Delivery Statistics
# ---------------------------------------------------------------------------


@webhooks_bp.route("/stats", methods=["GET"])
@login_required
@require_permission("webhooks", "read")
def get_delivery_stats() -> Any:
    """Retrieve aggregated webhook delivery statistics.

    Returns summary statistics from the
    :meth:`WebhookDispatcher.get_delivery_stats` method, including total
    deliveries, success/failure counts, success rate, average duration,
    and active webhook count.

    Returns:
        JSON response with delivery statistics.

    Response Codes:
        200: Success — statistics returned.
        401: Authentication required.
        403: Insufficient privileges.
    """
    logger.debug("Retrieving webhook delivery statistics.")
    dispatcher: WebhookDispatcher = _get_dispatcher()
    stats: Dict[str, Any] = dispatcher.get_delivery_stats()
    return jsonify(stats)


# ---------------------------------------------------------------------------
# GET /api/v1/webhooks — List All Webhooks
# ---------------------------------------------------------------------------


@webhooks_bp.route("/", methods=["GET"])
@login_required
@require_permission("webhooks", "read")
def list_webhooks() -> Any:
    """List all configured webhook integrations.

    Returns all registered webhook configurations with masked secret keys
    and last delivery status information.  Uses
    :meth:`WebhookDispatcher.list_webhooks` to retrieve the current
    registry snapshot.

    Returns:
        JSON response with list of webhook configurations.

    Response Codes:
        200: Success — webhook list returned.
        401: Authentication required.
        403: Insufficient privileges (``webhooks:read``).
    """
    logger.debug("Listing all webhook configurations.")
    dispatcher: WebhookDispatcher = _get_dispatcher()
    webhooks: List[WebhookConfig] = dispatcher.list_webhooks()

    response_data: List[Dict[str, Any]] = [
        _webhook_config_to_dict(wh, dispatcher) for wh in webhooks
    ]

    logger.info(
        "Listed %d webhook configuration(s).", len(response_data)
    )
    return jsonify(response_data)


# ---------------------------------------------------------------------------
# POST /api/v1/webhooks — Create Webhook
# ---------------------------------------------------------------------------


@webhooks_bp.route("/", methods=["POST"])
@login_required
@require_permission("webhooks", "create")
@webhooks_bp.arguments(WebhookCreateSchema)
def create_webhook(data: Dict[str, Any]) -> Any:
    """Create a new webhook configuration.

    Validates the request body against :class:`WebhookCreateSchema`,
    constructs a :class:`WebhookConfig`, checks for name conflicts, and
    registers the webhook with the dispatcher.

    Args:
        data: Deserialized request body from ``WebhookCreateSchema``
            (injected by ``@webhooks_bp.arguments``).

    Request Body:
        name (str):              Required — unique webhook identifier.
        url (str):               Required — target URL (http/https).
        event_types (list[str]): Required — event types to subscribe to.
        secret (str):            Optional — HMAC-SHA256 signing key.
        content_type (str):      Optional — default ``application/json``.
        enabled (bool):          Optional — default ``true``.
        repository_filter (list[str]): Optional — repo name filter.
        max_retries (int):       Optional — default ``3``, range 0–10.

    Returns:
        JSON response with the created webhook configuration (masked secret).

    Response Codes:
        201: Webhook created successfully.
        400: Invalid request body (validation error).
        401: Authentication required.
        403: Insufficient privileges (``webhooks:create``).
        409: Webhook name already exists.
    """

    # Validate event types against EventType enum
    _validate_event_types(data["event_types"])

    webhook_name: str = data["name"]
    dispatcher: WebhookDispatcher = _get_dispatcher()

    # Check for duplicate name
    existing: Optional[WebhookConfig] = dispatcher.get_webhook(webhook_name)
    if existing is not None:
        logger.warning(
            "Webhook creation rejected: name '%s' already exists.",
            webhook_name,
        )
        abort(
            409,
            message=f"Webhook with name '{webhook_name}' already exists.",
        )

    # Build WebhookConfig
    config = WebhookConfig(
        name=webhook_name,
        url=data["url"],
        secret=data.get("secret"),
        enabled=data.get("enabled", True),
        event_types=data["event_types"],
        repository_filter=data.get("repository_filter", []),
        max_retries=data.get("max_retries", 3),
        content_type=data.get("content_type", "application/json"),
    )

    # Register with dispatcher (registers event bus handlers)
    dispatcher.register_webhook(config)

    logger.info(
        "Webhook created: name='%s', url='%s', events=%s, enabled=%s.",
        config.name,
        config.url,
        config.event_types,
        config.enabled,
    )

    response_dict: Dict[str, Any] = _webhook_config_to_dict(
        config, dispatcher
    )
    return jsonify(response_dict), 201


# ---------------------------------------------------------------------------
# GET /api/v1/webhooks/<webhook_id> — Get Specific Webhook
# ---------------------------------------------------------------------------


@webhooks_bp.route("/<string:webhook_id>", methods=["GET"])
@login_required
@require_permission("webhooks", "read")
def get_webhook(webhook_id: str) -> Any:
    """Retrieve a specific webhook configuration by name.

    Returns the webhook configuration with the masked secret key and
    last delivery status information.

    Args:
        webhook_id: The webhook name / identifier (URL path parameter).

    Returns:
        JSON response with the webhook configuration.

    Response Codes:
        200: Success — webhook returned.
        401: Authentication required.
        403: Insufficient privileges (``webhooks:read``).
        404: Webhook not found.
    """
    logger.debug("Retrieving webhook configuration: '%s'.", webhook_id)
    dispatcher: WebhookDispatcher = _get_dispatcher()
    config: Optional[WebhookConfig] = dispatcher.get_webhook(webhook_id)

    if config is None:
        logger.debug("Webhook not found: '%s'.", webhook_id)
        abort(404, message=f"Webhook '{webhook_id}' not found.")

    response_dict: Dict[str, Any] = _webhook_config_to_dict(
        config, dispatcher
    )
    return jsonify(response_dict)


# ---------------------------------------------------------------------------
# PUT /api/v1/webhooks/<webhook_id> — Update Webhook
# ---------------------------------------------------------------------------


@webhooks_bp.route("/<string:webhook_id>", methods=["PUT"])
@login_required
@require_permission("webhooks", "update")
@webhooks_bp.arguments(WebhookUpdateSchema)
def update_webhook(data: Dict[str, Any], webhook_id: str) -> Any:
    """Update an existing webhook configuration.

    Applies partial updates to the webhook identified by *webhook_id*.
    Only fields present in the request body are updated; absent fields
    retain their current values.

    If ``event_types`` are changed, the dispatcher re-registers event
    bus handlers for the new event type set.

    Args:
        data: Deserialized request body from ``WebhookUpdateSchema``
            (injected by ``@webhooks_bp.arguments``).
        webhook_id: The webhook name / identifier (URL path parameter).

    Request Body:
        See :class:`WebhookUpdateSchema` for available fields.

    Returns:
        JSON response with the updated webhook configuration (masked secret).

    Response Codes:
        200: Webhook updated successfully.
        400: Invalid request body.
        401: Authentication required.
        403: Insufficient privileges (``webhooks:update``).
        404: Webhook not found.
        409: Updated name conflicts with existing webhook.
    """
    logger.debug("Updating webhook configuration: '%s'.", webhook_id)
    dispatcher: WebhookDispatcher = _get_dispatcher()
    existing: Optional[WebhookConfig] = dispatcher.get_webhook(webhook_id)

    if existing is None:
        logger.debug("Webhook not found for update: '%s'.", webhook_id)
        abort(404, message=f"Webhook '{webhook_id}' not found.")

    # Validate event_types if provided
    if "event_types" in data:
        _validate_event_types(data["event_types"])

    # Check for name conflict if name is being changed
    new_name: str = data.get("name", existing.name)
    if new_name != existing.name:
        conflict: Optional[WebhookConfig] = dispatcher.get_webhook(new_name)
        if conflict is not None:
            abort(
                409,
                message=(
                    f"Cannot rename: webhook '{new_name}' already exists."
                ),
            )

    # Build updated config by merging existing with provided fields
    # If the name is changing, unregister old name first
    if new_name != existing.name:
        dispatcher.unregister_webhook(existing.name)

    updated_config = WebhookConfig(
        name=new_name,
        url=data.get("url", existing.url),
        secret=(
            data["secret"]
            if "secret" in data
            else existing.secret
        ),
        enabled=data.get("enabled", existing.enabled),
        event_types=data.get("event_types", list(existing.event_types)),
        repository_filter=data.get(
            "repository_filter", list(existing.repository_filter)
        ),
        max_retries=data.get("max_retries", existing.max_retries),
        content_type=data.get("content_type", existing.content_type),
    )

    # Re-register with potentially updated configuration
    dispatcher.register_webhook(updated_config)

    logger.info(
        "Webhook updated: name='%s' (was '%s'), url='%s', events=%s.",
        updated_config.name,
        webhook_id,
        updated_config.url,
        updated_config.event_types,
    )

    response_dict: Dict[str, Any] = _webhook_config_to_dict(
        updated_config, dispatcher
    )
    return jsonify(response_dict)


# ---------------------------------------------------------------------------
# DELETE /api/v1/webhooks/<webhook_id> — Delete Webhook
# ---------------------------------------------------------------------------


@webhooks_bp.route("/<string:webhook_id>", methods=["DELETE"])
@login_required
@require_permission("webhooks", "delete")
def delete_webhook(webhook_id: str) -> Any:
    """Delete a webhook configuration.

    Unregisters the webhook from the dispatcher, removing all associated
    event bus handlers.

    Args:
        webhook_id: The webhook name / identifier (URL path parameter).

    Returns:
        Empty response with HTTP 204 No Content.

    Response Codes:
        204: Webhook deleted successfully.
        401: Authentication required.
        403: Insufficient privileges (``webhooks:delete``).
        404: Webhook not found.
    """
    logger.debug("Deleting webhook: '%s'.", webhook_id)
    dispatcher: WebhookDispatcher = _get_dispatcher()

    # Verify the webhook exists before deleting
    existing: Optional[WebhookConfig] = dispatcher.get_webhook(webhook_id)
    if existing is None:
        logger.debug("Webhook not found for deletion: '%s'.", webhook_id)
        abort(404, message=f"Webhook '{webhook_id}' not found.")

    # Unregister from dispatcher (removes event bus handlers)
    removed: bool = dispatcher.unregister_webhook(webhook_id)

    if removed:
        logger.info("Webhook deleted: '%s'.", webhook_id)
    else:
        # Defensive — should not reach here due to prior existence check
        logger.warning(
            "Webhook '%s' existed but unregister returned False.",
            webhook_id,
        )

    return "", 204


# ---------------------------------------------------------------------------
# POST /api/v1/webhooks/<webhook_id>/test — Test Webhook Delivery
# ---------------------------------------------------------------------------


@webhooks_bp.route("/<string:webhook_id>/test", methods=["POST"])
@login_required
@require_permission("webhooks", "update")
def test_webhook(webhook_id: str) -> Any:
    """Send a test delivery to a webhook endpoint.

    Dispatches a synthetic test event payload to the specified webhook
    URL using :meth:`WebhookDispatcher.dispatch_event`.  The test event
    uses the ``repository.created`` event type with a clearly marked
    test payload.

    Args:
        webhook_id: The webhook name / identifier (URL path parameter).

    Returns:
        JSON response with the delivery result including status code,
        response body (truncated), and duration in milliseconds.

    Response Codes:
        200: Test delivery completed (check ``success`` field for outcome).
        401: Authentication required.
        403: Insufficient privileges (``webhooks:update``).
        404: Webhook not found.
    """
    logger.info("Testing webhook delivery: '%s'.", webhook_id)
    dispatcher: WebhookDispatcher = _get_dispatcher()
    config: Optional[WebhookConfig] = dispatcher.get_webhook(webhook_id)

    if config is None:
        logger.debug("Webhook not found for test: '%s'.", webhook_id)
        abort(404, message=f"Webhook '{webhook_id}' not found.")

    # Build test payload using EventType.REPOSITORY_CREATED value
    test_event_type: str = EventType.REPOSITORY_CREATED.value
    test_payload: Dict[str, Any] = {
        "event_type": test_event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "test",
        "test": True,
        "webhook_name": config.name,
        "repository_name": "test-repository",
        "message": (
            "This is a test event sent to verify webhook delivery. "
            "No actual repository was created."
        ),
    }

    # Temporarily ensure the webhook matches this test event by dispatching
    # directly.  We create a temporary config with event_types overridden
    # to accept our test event type if needed.
    original_event_types: List[str] = list(config.event_types)
    temp_config = WebhookConfig(
        name=config.name,
        url=config.url,
        secret=config.secret,
        enabled=True,
        event_types=[test_event_type],
        repository_filter=[],
        max_retries=0,
        content_type=config.content_type,
    )

    # Temporarily register the test config (overrides existing)
    dispatcher.register_webhook(temp_config)

    try:
        results: List[WebhookDeliveryResult] = dispatcher.dispatch_event(
            test_event_type, test_payload
        )
    finally:
        # Restore original configuration
        original_config = WebhookConfig(
            name=config.name,
            url=config.url,
            secret=config.secret,
            enabled=config.enabled,
            event_types=original_event_types,
            repository_filter=list(config.repository_filter),
            max_retries=config.max_retries,
            content_type=config.content_type,
        )
        dispatcher.register_webhook(original_config)

    if results:
        result: WebhookDeliveryResult = results[0]
        response_dict: Dict[str, Any] = _delivery_result_to_dict(result)
        logger.info(
            "Webhook test delivery result: name='%s', success=%s, "
            "status_code=%s, duration_ms=%d.",
            webhook_id,
            result.success,
            result.status_code,
            result.duration_ms,
        )
        return jsonify(response_dict)

    # No results — should not happen but handle defensively
    logger.warning(
        "Webhook test delivery returned no results: '%s'.", webhook_id
    )
    return jsonify({
        "webhook_name": webhook_id,
        "event_type": test_event_type,
        "delivery_id": None,
        "success": False,
        "status_code": None,
        "response_body": None,
        "error_message": "No delivery result — webhook may be disabled.",
        "attempt": 0,
        "total_attempts": 0,
        "duration_ms": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# GET /api/v1/webhooks/<webhook_id>/deliveries — Delivery History
# ---------------------------------------------------------------------------


@webhooks_bp.route("/<string:webhook_id>/deliveries", methods=["GET"])
@login_required
@require_permission("webhooks", "read")
def get_webhook_deliveries(webhook_id: str) -> Any:
    """Retrieve recent delivery history for a specific webhook.

    Returns delivery attempt records filtered by webhook name, ordered
    newest-last.  The ``limit`` query parameter controls the maximum
    number of records returned.

    Args:
        webhook_id: The webhook name / identifier (URL path parameter).

    Query Parameters:
        limit (int): Maximum records to return (default 50, max 200).

    Returns:
        JSON response with list of delivery results including timestamp,
        event_type, status_code, response_body (truncated), duration_ms,
        and success/failure indicator.

    Response Codes:
        200: Success — delivery history returned.
        401: Authentication required.
        403: Insufficient privileges (``webhooks:read``).
        404: Webhook not found.
    """
    logger.debug("Retrieving delivery history for webhook: '%s'.", webhook_id)
    dispatcher: WebhookDispatcher = _get_dispatcher()

    # Verify webhook exists
    config: Optional[WebhookConfig] = dispatcher.get_webhook(webhook_id)
    if config is None:
        logger.debug(
            "Webhook not found for delivery history: '%s'.", webhook_id
        )
        abort(404, message=f"Webhook '{webhook_id}' not found.")

    # Parse limit from query parameters
    limit_str: Optional[str] = request.args.get("limit", "50")
    try:
        limit: int = min(int(limit_str), 200)
        if limit < 1:
            limit = 50
    except (ValueError, TypeError):
        limit = 50

    # Retrieve and filter deliveries for this specific webhook
    all_deliveries: List[WebhookDeliveryResult] = (
        dispatcher.get_recent_deliveries(limit=1000)
    )
    webhook_deliveries: List[WebhookDeliveryResult] = [
        d for d in all_deliveries if d.webhook_name == webhook_id
    ]

    # Apply limit (take most recent)
    limited_deliveries: List[WebhookDeliveryResult] = (
        webhook_deliveries[-limit:]
    )

    response_data: List[Dict[str, Any]] = [
        _delivery_result_to_dict(d) for d in limited_deliveries
    ]

    logger.info(
        "Retrieved %d delivery record(s) for webhook '%s'.",
        len(response_data),
        webhook_id,
    )
    return jsonify(response_data)


# ===========================================================================
# Public API
# ===========================================================================

__all__: List[str] = ["webhooks_bp"]
