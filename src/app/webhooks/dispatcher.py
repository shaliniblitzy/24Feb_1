"""
Webhook HTTP POST Dispatch — Feature F-503 (Webhook Integration).

This module provides the core webhook dispatch engine for the Nexus Repository
application.  It receives events from the Blinker-based event system
(:mod:`src.app.events`) and dispatches them as HTTP POST requests to
configured external webhook endpoints.

**Capabilities:**

- Event-to-webhook mapping with type and repository filtering
- Asynchronous delivery via :class:`~concurrent.futures.ThreadPoolExecutor`
- Retry logic with exponential backoff (configurable via
  ``WEBHOOK_MAX_RETRIES``)
- HMAC-SHA256 payload signing via :func:`src.app.webhooks.payload_signer.sign_payload`
- Response logging for debugging and audit trail

**Java Source Equivalents Replaced:**

- Webhook integration module from the Nexus Java backend
- Apache HttpClient 4.5.14 for outbound HTTP POST delivery
- Guava EventBus subscriber pattern for webhook dispatch

**Architecture:**

Uses ``requests 2.32.3`` (replacing Apache HttpClient 4.5.14 per AAP
Section 0.1.2) for outbound HTTP.  Integrates with Blinker signals from
:mod:`src.app.events` (AAP Section 0.4.3 — Observer pattern).  Structured
logging via Python's ``logging`` module (AAP Section 0.7.2).

Example
-------
>>> from src.app.webhooks.dispatcher import WebhookDispatcher, WebhookConfig
>>> dispatcher = WebhookDispatcher()
>>> cfg = WebhookConfig(name='ci', url='https://ci.example.com/hook')
>>> dispatcher.register_webhook(cfg)
>>> dispatcher.dispatch_event('repository.created', {'repository_name': 'my-repo'})
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import requests

from src.app.events.event_bus import subscribe
from src.app.events.event_types import EventType
from src.app.webhooks.payload_signer import sign_payload

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source system.  Provides DEBUG-level delivery tracing, INFO-level
# registration / success events, and WARNING-level delivery failures.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants and Configuration
# ---------------------------------------------------------------------------

DEFAULT_MAX_RETRIES: int = 3
"""Maximum retry attempts for failed webhook deliveries.

Configurable per-webhook via :attr:`WebhookConfig.max_retries` or globally
via the ``WEBHOOK_MAX_RETRIES`` environment variable.
"""

DEFAULT_RETRY_BACKOFF_BASE: float = 2.0
"""Base multiplier (seconds) for exponential backoff between retry attempts.

Backoff formula: ``delay = min(base * (2 ** attempt), max_delay)``
"""

DEFAULT_RETRY_BACKOFF_MAX: float = 60.0
"""Maximum backoff delay (seconds) to prevent excessively long waits."""

DEFAULT_REQUEST_TIMEOUT: float = 30.0
"""HTTP read timeout (seconds) for webhook POST requests."""

DEFAULT_CONNECT_TIMEOUT: float = 10.0
"""TCP connection timeout (seconds) for webhook POST requests."""

DEFAULT_THREAD_POOL_SIZE: int = 4
"""Maximum number of concurrent webhook delivery worker threads."""

WEBHOOK_SIGNATURE_HEADER: str = "X-Nexus-Webhook-Signature"
"""HTTP header carrying the HMAC-SHA256 payload signature."""

WEBHOOK_EVENT_HEADER: str = "X-Nexus-Webhook-Event"
"""HTTP header carrying the event type string."""

WEBHOOK_DELIVERY_HEADER: str = "X-Nexus-Webhook-Delivery"
"""HTTP header carrying the unique delivery UUID."""

WEBHOOK_TIMESTAMP_HEADER: str = "X-Nexus-Webhook-Timestamp"
"""HTTP header carrying the ISO 8601 delivery timestamp."""

CONTENT_TYPE_JSON: str = "application/json"
"""Default Content-Type for webhook POST bodies."""

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class WebhookConfig:
    """Configuration for a single webhook endpoint.

    Attributes:
        name: Webhook name / identifier — used as the registry key.
        url: Target URL for HTTP POST delivery.
        secret: Shared secret for HMAC-SHA256 signing.  When set the
            ``X-Nexus-Webhook-Signature`` header is included in every
            delivery.  When ``None`` no signature header is sent.
        enabled: Whether this webhook is active and eligible for dispatch.
        event_types: List of event type strings to subscribe to.
            An empty list means *all* events pass the filter.
        repository_filter: List of repository names to filter on.
            An empty list means *all* repositories pass the filter.
        max_retries: Maximum retry attempts for this specific webhook.
        content_type: Content-Type header value for the POST body.
    """

    name: str
    url: str
    secret: Optional[str] = None
    enabled: bool = True
    event_types: List[str] = field(default_factory=list)
    repository_filter: List[str] = field(default_factory=list)
    max_retries: int = DEFAULT_MAX_RETRIES
    content_type: str = CONTENT_TYPE_JSON


@dataclass
class WebhookDeliveryResult:
    """Result of a webhook delivery attempt.

    Attributes:
        webhook_name: Name of the webhook that was targeted.
        url: Target URL the POST was sent to.
        event_type: Event type string that triggered the delivery.
        delivery_id: Unique UUID assigned to this delivery for correlation.
        success: ``True`` if the remote returned a 2xx status code.
        status_code: HTTP response status code (``None`` on connection error).
        response_body: Truncated response body for diagnostics.
        error_message: Human-readable error description on failure.
        attempt: The 1-based attempt number of this result.
        total_attempts: Total number of attempts made (including retries).
        duration_ms: Wall-clock duration of the HTTP request in ms.
        timestamp: ISO 8601 UTC timestamp of the delivery attempt.
    """

    webhook_name: str
    url: str
    event_type: str
    delivery_id: str
    success: bool
    status_code: Optional[int] = None
    response_body: Optional[str] = None
    error_message: Optional[str] = None
    attempt: int = 1
    total_attempts: int = 1
    duration_ms: int = 0
    timestamp: str = ""


# ---------------------------------------------------------------------------
# WebhookDispatcher
# ---------------------------------------------------------------------------


class WebhookDispatcher:
    """Dispatch repository events to configured webhook endpoints via HTTP POST.

    Implements Feature F-503 (Webhook Integration) from the AAP.

    Capabilities:

    - Event-to-webhook mapping with type and repository filtering
    - Async delivery via :class:`~concurrent.futures.ThreadPoolExecutor`
    - Retry logic with exponential backoff
    - HMAC-SHA256 payload signing (via :mod:`~src.app.webhooks.payload_signer`)
    - Response logging for debugging and audit

    Replaces webhook dispatch functionality from the Java Nexus backend,
    using ``requests 2.32.3`` instead of Apache HttpClient 4.5.14.
    """

    # ------------------------------------------------------------------ init

    def __init__(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        thread_pool_size: int = DEFAULT_THREAD_POOL_SIZE,
    ) -> None:
        """Initialise the webhook dispatcher.

        Args:
            max_retries: Default maximum retry attempts for deliveries.
                Individual webhooks may override this via
                :attr:`WebhookConfig.max_retries`.
            thread_pool_size: Number of worker threads in the async
                delivery thread pool.
        """
        self._webhooks: Dict[str, WebhookConfig] = {}
        self._max_retries: int = max_retries
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=thread_pool_size,
            thread_name_prefix="webhook-",
        )
        self._delivery_log: List[WebhookDeliveryResult] = []
        self._delivery_log_max: int = 1000
        self._lock: threading.Lock = threading.Lock()
        self._shutdown_event: threading.Event = threading.Event()
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.WebhookDispatcher"
        )
        self.logger.info(
            "WebhookDispatcher initialised: max_retries=%d, pool_size=%d",
            max_retries,
            thread_pool_size,
        )

    # -------------------------------------------------------- registry CRUD

    def register_webhook(self, config: WebhookConfig) -> None:
        """Register or replace a webhook configuration.

        Thread-safe — acquires the internal lock before modifying the
        registry.

        Args:
            config: The webhook configuration to register.
        """
        with self._lock:
            self._webhooks[config.name] = config
        self.logger.info(
            "Webhook registered: %s -> %s", config.name, config.url
        )

    def unregister_webhook(self, name: str) -> bool:
        """Remove a webhook configuration by name.

        Args:
            name: The webhook name to remove.

        Returns:
            ``True`` if the webhook was found and removed, ``False``
            otherwise.
        """
        with self._lock:
            if name in self._webhooks:
                del self._webhooks[name]
                self.logger.info("Webhook unregistered: %s", name)
                return True
        self.logger.info(
            "Webhook unregister requested but not found: %s", name
        )
        return False

    def get_webhook(self, name: str) -> Optional[WebhookConfig]:
        """Return a webhook configuration by name.

        Args:
            name: The webhook name to look up.

        Returns:
            The :class:`WebhookConfig` if found, otherwise ``None``.
        """
        with self._lock:
            return self._webhooks.get(name)

    def list_webhooks(self) -> List[WebhookConfig]:
        """Return a snapshot of all registered webhook configurations.

        Returns:
            A list of :class:`WebhookConfig` instances.
        """
        with self._lock:
            return list(self._webhooks.values())

    # --------------------------------------------------------- event filter

    def _should_dispatch(
        self,
        webhook: WebhookConfig,
        event_type: str,
        payload: Dict[str, Any],
    ) -> bool:
        """Determine whether an event should be dispatched to a webhook.

        Checks are applied in order:

        1. ``webhook.enabled`` must be ``True``.
        2. If ``webhook.event_types`` is non-empty, ``event_type`` must
           appear in the list.
        3. If ``webhook.repository_filter`` is non-empty,
           ``payload['repository_name']`` must appear in the list.

        Args:
            webhook: The candidate webhook configuration.
            event_type: Normalised event type string.
            payload: Event payload dictionary.

        Returns:
            ``True`` if all checks pass and the event should be delivered.
        """
        if not webhook.enabled:
            return False
        if webhook.event_types and event_type not in webhook.event_types:
            return False
        if webhook.repository_filter:
            repo_name = payload.get("repository_name")
            if repo_name not in webhook.repository_filter:
                return False
        return True

    # ------------------------------------------------------- core dispatch

    def dispatch_event(
        self,
        event_type: Union[EventType, str],
        payload: Optional[Dict[str, Any]] = None,
    ) -> List[WebhookDeliveryResult]:
        """Dispatch an event to all matching webhooks.

        This is the primary public API.  It normalises the event type and
        payload, evaluates each registered webhook against the event
        filtering rules, and submits matching deliveries to the async
        thread pool.

        Args:
            event_type: The event being dispatched — accepts both
                :class:`EventType` enum values and plain strings.
            payload: Event-specific data dictionary.  If ``None`` an empty
                dict is used.

        Returns:
            A list of :class:`WebhookDeliveryResult` objects, one per
            webhook that was eligible for delivery.  Results may arrive
            asynchronously; this method collects futures and returns
            resolved results.
        """
        # Normalise event type to string
        event_type_str: str = (
            event_type.value
            if isinstance(event_type, EventType)
            else str(event_type)
        )

        # Normalise payload
        if payload is None:
            payload = {}
        payload.setdefault("event_type", event_type_str)
        payload.setdefault(
            "timestamp", datetime.now(timezone.utc).isoformat()
        )

        # Snapshot current webhooks under lock
        with self._lock:
            webhooks_snapshot = list(self._webhooks.values())

        # Determine matching webhooks
        matching: List[WebhookConfig] = [
            wh
            for wh in webhooks_snapshot
            if self._should_dispatch(wh, event_type_str, payload)
        ]

        self.logger.debug(
            "Dispatching event '%s' to %d/%d webhook(s)",
            event_type_str,
            len(matching),
            len(webhooks_snapshot),
        )

        if not matching:
            return []

        # Submit deliveries to the thread pool and collect futures
        futures = []
        for webhook in matching:
            future = self._executor.submit(
                self._deliver_with_retry, webhook, event_type_str, payload
            )
            futures.append(future)

        # Collect results — wait for all deliveries to complete
        results: List[WebhookDeliveryResult] = []
        for future in futures:
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:
                # Defensive: should never happen because _deliver_with_retry
                # catches all exceptions internally.
                self.logger.error(
                    "Unexpected error collecting delivery future: %s",
                    str(exc),
                    exc_info=True,
                )

        return results

    # ----------------------------------------- delivery with retry logic

    def _deliver_with_retry(
        self,
        webhook: WebhookConfig,
        event_type: str,
        payload: Dict[str, Any],
    ) -> WebhookDeliveryResult:
        """Deliver a webhook with exponential backoff retry.

        Retry policy:

        - 2xx responses → immediate success, no retry
        - 4xx responses → non-retryable permanent error, stop immediately
        - 5xx responses → retryable, apply exponential backoff
        - Connection errors → retryable, apply exponential backoff
        - Max attempts = ``webhook.max_retries + 1`` (initial + retries)

        Args:
            webhook: Target webhook configuration.
            event_type: Normalised event type string.
            payload: Event payload dictionary.

        Returns:
            The final :class:`WebhookDeliveryResult` after all attempts.
        """
        delivery_id: str = str(uuid.uuid4())
        max_attempts: int = webhook.max_retries + 1
        last_result: Optional[WebhookDeliveryResult] = None

        for attempt in range(max_attempts):
            # Exponential backoff for retries (not on first attempt)
            if attempt > 0:
                delay: float = min(
                    DEFAULT_RETRY_BACKOFF_BASE * (2 ** (attempt - 1)),
                    DEFAULT_RETRY_BACKOFF_MAX,
                )
                self.logger.debug(
                    "Webhook '%s' delivery %s: retrying in %.1fs "
                    "(attempt %d/%d)",
                    webhook.name,
                    delivery_id,
                    delay,
                    attempt + 1,
                    max_attempts,
                )
                # Use Event.wait() instead of time.sleep() so that
                # the retry can be interrupted immediately when
                # shutdown() is called — prevents the thread pool
                # from blocking during test teardown or app shutdown.
                if self._shutdown_event.wait(delay):
                    # Shutdown was requested — abort delivery retries.
                    self.logger.debug(
                        "Webhook '%s' delivery %s: shutdown requested, "
                        "aborting retries.",
                        webhook.name,
                        delivery_id,
                    )
                    break

            result = self._deliver_once(
                webhook, event_type, payload, delivery_id, attempt + 1
            )
            result.total_attempts = attempt + 1
            last_result = result

            # Success — stop immediately
            if result.success:
                self.logger.info(
                    "Webhook '%s' delivery %s succeeded: status=%s "
                    "duration=%dms attempt=%d/%d",
                    webhook.name,
                    delivery_id,
                    result.status_code,
                    result.duration_ms,
                    attempt + 1,
                    max_attempts,
                )
                self._append_delivery_log(result)
                return result

            # Non-retryable: 4xx client error — stop immediately
            if (
                result.status_code is not None
                and 400 <= result.status_code < 500
            ):
                self.logger.warning(
                    "Webhook '%s' delivery %s failed with non-retryable "
                    "status %d — aborting retries. error=%s",
                    webhook.name,
                    delivery_id,
                    result.status_code,
                    result.error_message or result.response_body,
                )
                result.total_attempts = attempt + 1
                self._append_delivery_log(result)
                return result

            # Retryable failure — continue loop
            self.logger.debug(
                "Webhook '%s' delivery %s attempt %d/%d failed: "
                "status=%s error=%s",
                webhook.name,
                delivery_id,
                attempt + 1,
                max_attempts,
                result.status_code,
                result.error_message,
            )

        # All attempts exhausted
        if last_result is not None:
            last_result.total_attempts = max_attempts
            self.logger.warning(
                "Webhook '%s' delivery %s exhausted all %d attempts. "
                "Last status=%s error=%s",
                webhook.name,
                delivery_id,
                max_attempts,
                last_result.status_code,
                last_result.error_message,
            )
            self._append_delivery_log(last_result)
            return last_result

        # Defensive fallback — should never reach here
        fallback = WebhookDeliveryResult(
            webhook_name=webhook.name,
            url=webhook.url,
            event_type=event_type,
            delivery_id=delivery_id,
            success=False,
            error_message="No delivery attempts were made",
            total_attempts=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._append_delivery_log(fallback)
        return fallback

    # ------------------------------------------------- single HTTP POST

    def _deliver_once(
        self,
        webhook: WebhookConfig,
        event_type: str,
        payload: Dict[str, Any],
        delivery_id: str,
        attempt: int,
    ) -> WebhookDeliveryResult:
        """Perform a single HTTP POST delivery attempt.

        This method is **fault-tolerant**: exceptions are caught and
        converted to :class:`WebhookDeliveryResult` with ``success=False``.
        Delivery failures **never** propagate to the caller.

        Args:
            webhook: Target webhook configuration.
            event_type: Normalised event type string.
            payload: Event payload dictionary.
            delivery_id: Unique delivery UUID for correlation.
            attempt: 1-based attempt counter.

        Returns:
            A :class:`WebhookDeliveryResult` describing the outcome.
        """
        now_iso: str = datetime.now(timezone.utc).isoformat()

        try:
            # Serialise payload to JSON
            body: str = json.dumps(payload, default=str)

            # Build HTTP headers
            headers: Dict[str, str] = {
                "Content-Type": webhook.content_type,
                WEBHOOK_EVENT_HEADER: event_type,
                WEBHOOK_DELIVERY_HEADER: delivery_id,
                WEBHOOK_TIMESTAMP_HEADER: now_iso,
            }

            # HMAC-SHA256 signature when a shared secret is configured
            if webhook.secret:
                signature: str = sign_payload(
                    body.encode("utf-8"), webhook.secret
                )
                headers[WEBHOOK_SIGNATURE_HEADER] = signature

            # Execute HTTP POST with configurable timeouts
            start_time: float = time.monotonic()
            response = requests.post(
                webhook.url,
                data=body,
                headers=headers,
                timeout=(DEFAULT_CONNECT_TIMEOUT, DEFAULT_REQUEST_TIMEOUT),
            )
            duration_ms: int = int(
                (time.monotonic() - start_time) * 1000
            )

            success: bool = 200 <= response.status_code < 300
            return WebhookDeliveryResult(
                webhook_name=webhook.name,
                url=webhook.url,
                event_type=event_type,
                delivery_id=delivery_id,
                success=success,
                status_code=response.status_code,
                response_body=response.text[:500] if response.text else None,
                error_message=(
                    None
                    if success
                    else f"HTTP {response.status_code}: {response.reason}"
                ),
                attempt=attempt,
                duration_ms=duration_ms,
                timestamp=now_iso,
            )

        except requests.RequestException as exc:
            duration_ms = int(
                (time.monotonic() - start_time) * 1000
            ) if "start_time" in locals() else 0
            self.logger.debug(
                "Webhook '%s' delivery %s request error: %s",
                webhook.name,
                delivery_id,
                str(exc),
            )
            return WebhookDeliveryResult(
                webhook_name=webhook.name,
                url=webhook.url,
                event_type=event_type,
                delivery_id=delivery_id,
                success=False,
                error_message=f"RequestException: {str(exc)}",
                attempt=attempt,
                duration_ms=duration_ms,
                timestamp=now_iso,
            )

        except Exception as exc:
            # Catch-all for robustness — delivery failures must never
            # propagate to the caller or the event bus.
            self.logger.error(
                "Webhook '%s' delivery %s unexpected error: %s",
                webhook.name,
                delivery_id,
                str(exc),
                exc_info=True,
            )
            return WebhookDeliveryResult(
                webhook_name=webhook.name,
                url=webhook.url,
                event_type=event_type,
                delivery_id=delivery_id,
                success=False,
                error_message=f"Unexpected: {str(exc)}",
                attempt=attempt,
                duration_ms=0,
                timestamp=now_iso,
            )

    # ----------------------------------------- delivery log management

    def _append_delivery_log(self, result: WebhookDeliveryResult) -> None:
        """Append a delivery result to the in-memory ring-buffer log.

        Thread-safe via the instance lock.

        Args:
            result: The delivery result to store.
        """
        with self._lock:
            self._delivery_log.append(result)
            # Trim to ring-buffer max
            if len(self._delivery_log) > self._delivery_log_max:
                self._delivery_log = self._delivery_log[
                    -self._delivery_log_max:
                ]

    # ----------------------------------------- Blinker event integration

    def register_event_listeners(self) -> None:
        """Subscribe to all :class:`EventType` signals in the event bus.

        For each member of :class:`EventType`, the dispatcher's internal
        ``_on_event`` handler is registered via :func:`subscribe`.

        This method should be called once during Flask application factory
        initialisation (in ``factory.py``).
        """
        count: int = 0
        for event_type in EventType:
            subscribe(event_type, self._on_event)
            count += 1
        self.logger.info(
            "Webhook dispatcher registered for %d event types", count
        )

    def _on_event(self, sender: Any, **kwargs: Any) -> None:
        """Blinker signal handler bridging events to webhook dispatch.

        Extracts ``event_type`` and ``payload`` from the signal keyword
        arguments and delegates to :meth:`dispatch_event`.

        This handler is **fault-tolerant**: exceptions are caught and
        logged but never re-raised to the event bus.

        Args:
            sender: The signal sender (unused).
            **kwargs: Signal keyword arguments containing ``event_type``
                and ``payload``.
        """
        try:
            event_type = kwargs.get("event_type")
            payload = kwargs.get("payload", {})
            if event_type is not None:
                self.logger.debug(
                    "Webhook _on_event triggered: event_type=%s",
                    event_type,
                )
                self.dispatch_event(event_type, payload)
            else:
                self.logger.debug(
                    "Webhook _on_event triggered without event_type — "
                    "ignoring."
                )
        except Exception as exc:
            # Event handler failures must NEVER propagate to the event bus.
            self.logger.error(
                "Error in webhook _on_event handler: %s",
                str(exc),
                exc_info=True,
            )

    # ----------------------------------------- configuration loading

    def load_webhooks_from_config(
        self, app_config: Optional[Dict[str, Any]] = None
    ) -> int:
        """Load webhook configurations from Flask app config or database.

        Looks for a ``WEBHOOKS`` key in the provided *app_config* dict
        (expected to be a list of dicts).  If *app_config* is ``None``,
        falls back to reading from the :class:`SystemConfig` model
        (database-stored configuration) where keys start with
        ``'webhook.'``.

        Each configuration dict is converted to a :class:`WebhookConfig`
        and registered via :meth:`register_webhook`.

        Args:
            app_config: Flask ``app.config`` dictionary.  If ``None`` the
                database fallback is used.

        Returns:
            The number of webhook configurations loaded.
        """
        loaded: int = 0
        webhook_defs: List[Dict[str, Any]] = []

        if app_config is not None:
            # Load from Flask app.config
            raw = app_config.get("WEBHOOKS", [])
            if isinstance(raw, list):
                webhook_defs = raw
            else:
                self.logger.warning(
                    "WEBHOOKS config key is not a list — ignoring."
                )
        else:
            # Fallback: load from SystemConfig database table
            try:
                from src.app.models.system_config import SystemConfig

                configs = SystemConfig.get_by_category("webhook")
                for cfg in configs:
                    if cfg.value:
                        try:
                            parsed = json.loads(cfg.value)
                            if isinstance(parsed, dict):
                                webhook_defs.append(parsed)
                            elif isinstance(parsed, list):
                                webhook_defs.extend(parsed)
                        except (json.JSONDecodeError, TypeError):
                            self.logger.warning(
                                "Could not parse webhook config from "
                                "SystemConfig key '%s'.",
                                cfg.key,
                            )

                # Also check for individual webhook keys
                webhook_list_raw = SystemConfig.get_value(
                    "webhook.list", default=None
                )
                if webhook_list_raw:
                    try:
                        parsed_list = json.loads(webhook_list_raw)
                        if isinstance(parsed_list, list):
                            webhook_defs.extend(parsed_list)
                    except (json.JSONDecodeError, TypeError):
                        self.logger.warning(
                            "Could not parse 'webhook.list' SystemConfig "
                            "value."
                        )
            except Exception as exc:
                self.logger.warning(
                    "Failed to load webhooks from SystemConfig: %s",
                    str(exc),
                )

        for wh_dict in webhook_defs:
            try:
                config = WebhookConfig(
                    name=str(wh_dict.get("name", f"webhook-{loaded}")),
                    url=str(wh_dict.get("url", "")),
                    secret=wh_dict.get("secret"),
                    enabled=bool(wh_dict.get("enabled", True)),
                    event_types=list(wh_dict.get("event_types", [])),
                    repository_filter=list(
                        wh_dict.get("repository_filter", [])
                    ),
                    max_retries=int(
                        wh_dict.get("max_retries", self._max_retries)
                    ),
                    content_type=str(
                        wh_dict.get("content_type", CONTENT_TYPE_JSON)
                    ),
                )
                if not config.url:
                    self.logger.warning(
                        "Skipping webhook '%s': missing URL.", config.name
                    )
                    continue
                self.register_webhook(config)
                loaded += 1
            except Exception as exc:
                self.logger.warning(
                    "Skipping invalid webhook config: %s — %s",
                    wh_dict,
                    str(exc),
                )

        self.logger.info("Loaded %d webhook configuration(s)", loaded)
        return loaded

    # ----------------------------------------- delivery log & stats

    def get_recent_deliveries(
        self, limit: int = 50
    ) -> List[WebhookDeliveryResult]:
        """Return the most recent delivery results from the in-memory log.

        Thread-safe — acquires the internal lock before reading.

        Args:
            limit: Maximum number of results to return.

        Returns:
            A list of up to *limit* most recent
            :class:`WebhookDeliveryResult` objects, newest last.
        """
        with self._lock:
            return list(self._delivery_log[-limit:])

    def get_delivery_stats(self) -> Dict[str, Any]:
        """Return aggregated delivery statistics.

        Returns:
            A dictionary containing:

            - ``total_deliveries`` — total entries in the log
            - ``successful`` — count of successful deliveries
            - ``failed`` — count of failed deliveries
            - ``success_rate`` — percentage of successful deliveries
            - ``avg_duration_ms`` — average delivery duration in ms
            - ``active_webhooks`` — number of currently registered webhooks
        """
        with self._lock:
            log_snapshot = list(self._delivery_log)
            active_count = len(self._webhooks)

        total: int = len(log_snapshot)
        successful: int = sum(1 for r in log_snapshot if r.success)
        failed: int = total - successful
        success_rate: float = (
            (successful / total * 100.0) if total > 0 else 0.0
        )
        avg_duration: float = (
            sum(r.duration_ms for r in log_snapshot) / total
            if total > 0
            else 0.0
        )

        return {
            "total_deliveries": total,
            "successful": successful,
            "failed": failed,
            "success_rate": round(success_rate, 2),
            "avg_duration_ms": round(avg_duration, 2),
            "active_webhooks": active_count,
        }

    # ----------------------------------------- shutdown / cleanup

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the delivery thread pool executor.

        Args:
            wait: If ``True`` (default), block until all pending
                deliveries complete.  If ``False``, cancel pending
                work immediately.

        Note:
            During interpreter shutdown or pytest teardown, logging
            handler streams may already be closed, causing the
            ``logging`` module's internal ``handleError`` to emit
            ``ValueError: I/O operation on closed file`` tracebacks.
            To prevent this, we temporarily disable logging on all
            handlers before emitting shutdown messages, then restore
            the original levels.
        """
        # Temporarily disable logging handlers to prevent
        # "I/O operation on closed file" errors during shutdown.
        # This avoids the logging module's internal handleError path.
        saved_levels = []
        try:
            for handler in logging.root.handlers:
                saved_levels.append((handler, handler.level))
                handler.setLevel(logging.CRITICAL + 1)
        except Exception:
            pass

        try:
            self.logger.info(
                "Webhook dispatcher shutting down (wait=%s)", wait,
            )
        except Exception:
            pass

        # Signal all sleeping _deliver_with_retry workers to wake up
        # and abort so the executor can shut down promptly.
        self._shutdown_event.set()

        self._executor.shutdown(wait=wait)

        try:
            self.logger.info("Webhook dispatcher shut down")
        except Exception:
            pass

        # Restore handler levels (best-effort — may fail if handlers
        # are already torn down).
        for handler, level in saved_levels:
            try:
                handler.setLevel(level)
            except Exception:
                pass

    def __del__(self) -> None:
        """Ensure the executor is shut down on garbage collection."""
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            # Silently ignore errors during GC finalisation
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: List[str] = [
    "WebhookDispatcher",
    "WebhookConfig",
    "WebhookDeliveryResult",
    "DEFAULT_MAX_RETRIES",
    "WEBHOOK_SIGNATURE_HEADER",
    "WEBHOOK_EVENT_HEADER",
]
