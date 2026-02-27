"""
Built-in Event Subscribers/Receivers Module.

Replaces the Guava EventBus ``@Subscribe``-annotated handlers from the original
Java source system (Sonatype Nexus Repository) with Blinker 1.9.0 signal
receivers implementing the **Observer pattern** (AAP Section 0.4.3).

Each subscriber class listens for specific event types dispatched via the
central event bus (:mod:`src.app.events.event_bus`) and performs side-effect
actions:

- :class:`AuditLogSubscriber` — persists audit trail records to the database
  (Feature F-303).
- :class:`MetricsSubscriber` — increments Prometheus counters and exposes
  histogram metrics (Feature F-401).
- :class:`CacheInvalidationSubscriber` — invalidates configuration caches on
  config changes (Feature F-404).
- :class:`CleanupTriggerSubscriber` — triggers cleanup policy evaluation when
  content or configuration changes (Feature F-204).

**Fault Tolerance Contract:**
All subscriber handlers wrap their logic in ``try/except`` blocks.  Subscriber
failures **NEVER** propagate to the event emitter — a failing subscriber must
not break the calling business logic.

**Registration:**
All subscribers are registered during Flask application factory initialization
via :func:`register_all_subscribers`, called from ``src/app/factory.py``.

**Architecture Mapping:**

+-------------------------------+-------------------------------------------+
| Java Component                | Python Replacement                        |
+===============================+===========================================+
| Guava EventBus @Subscribe     | Blinker signal receivers via subscribe()  |
+-------------------------------+-------------------------------------------+
| Dropwizard Metrics 4.2.25    | prometheus-client Counter / Histogram     |
+-------------------------------+-------------------------------------------+
| MyBatis AUDIT_EVENT mapper    | SQLAlchemy AuditEvent model               |
+-------------------------------+-------------------------------------------+

**Compatibility:**
- Python 3.12+ (type hints throughout per AAP Section 0.7.2)
- No Java dependencies (AAP Section 0.7.2)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from flask import current_app, g, has_request_context
from prometheus_client import Counter, Histogram

from src.app.events.event_bus import subscribe
from src.app.events.event_types import EventType
from src.app.extensions import db
from src.app.models.audit_event import AuditEvent

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 structured logging from the Java source.
# Provides DEBUG-level event handling traces, INFO-level registration
# messages, and ERROR-level fault reporting when subscriber exceptions occur.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared Domain Mapping
# ---------------------------------------------------------------------------
# Maps each EventType enum member to its logical domain string.  Used by
# both AuditLogSubscriber and MetricsSubscriber to categorise events.
# ---------------------------------------------------------------------------

_EVENT_DOMAIN_MAP: Dict[EventType, str] = {
    EventType.REPOSITORY_CREATED: "repository",
    EventType.REPOSITORY_UPDATED: "repository",
    EventType.REPOSITORY_DELETED: "repository",
    EventType.COMPONENT_UPLOADED: "component",
    EventType.COMPONENT_DELETED: "component",
    EventType.ASSET_DOWNLOADED: "asset",
    EventType.USER_AUTHENTICATED: "security",
    EventType.USER_AUTHORIZATION_FAILED: "security",
    EventType.CONFIG_CHANGED: "configuration",
    EventType.TASK_STARTED: "task",
    EventType.TASK_COMPLETED: "task",
    EventType.BLOBSTORE_COMPACTED: "blobstore",
    EventType.CLEANUP_COMPLETED: "cleanup",
}


def _resolve_domain(event_type: Any) -> str:
    """Resolve the logical domain for a given event type.

    Args:
        event_type: An :class:`EventType` enum member or a plain string
            representation of an event type.

    Returns:
        The domain string (e.g. ``'repository'``, ``'security'``).
        Falls back to ``'system'`` for unknown event types.
    """
    if isinstance(event_type, EventType):
        return _EVENT_DOMAIN_MAP.get(event_type, "system")

    # Handle string event types by matching against enum values
    event_type_str = str(event_type)
    for enum_member, domain in _EVENT_DOMAIN_MAP.items():
        if enum_member.value == event_type_str:
            return domain
    return "system"


# ===========================================================================
# AuditLogSubscriber (Feature F-303)
# ===========================================================================


class AuditLogSubscriber:
    """Persists security, repository, and system events to the audit trail.

    This subscriber writes audit events to the ``audit_events`` database table
    via the :class:`~src.app.models.audit_event.AuditEvent` SQLAlchemy model.
    It replaces the Java Guava EventBus subscriber for audit logging and
    supports **Feature F-303 (Audit Logging)**.

    The subscriber registers for **all** :class:`EventType` constants so that
    every significant system action is recorded in the persistent audit log.
    Audit records are immutable once created — they are never updated or
    deleted.

    **Fault Tolerance:**
    Database errors during audit event persistence are caught and logged but
    **never** re-raised.  A failing audit write must not interrupt the
    business operation that triggered the event.
    """

    def __init__(self) -> None:
        """Initialise the audit log subscriber.

        Creates a class-specific logger and builds the list of event types
        this subscriber handles (all members of :class:`EventType`).

        Also obtains a reference to the dedicated ``src.app.audit`` logger
        which is connected to the ``auditHandler`` in ``logging.conf``,
        ensuring that audit events are written to both the database and
        the ``logs/audit.log`` file for file-based backup (F-303).
        """
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.AuditLogSubscriber"
        )
        # Dedicated audit file logger — writes to logs/audit.log via the
        # auditHandler defined in logging.conf (qualname=src.app.audit).
        self._audit_file_logger: logging.Logger = logging.getLogger(
            "src.app.audit"
        )
        self._event_types: list[EventType] = list(EventType)

    def register(self) -> None:
        """Subscribe to all auditable event types via the event bus.

        Iterates through every :class:`EventType` enum member and registers
        :meth:`handle_event` as the callback via
        :func:`~src.app.events.event_bus.subscribe`.

        Logs the total number of subscriptions at INFO level upon completion.
        """
        count: int = 0
        for event_type in self._event_types:
            subscribe(event_type, self.handle_event)
            count += 1
        self.logger.info(
            "AuditLogSubscriber registered for %d event types", count
        )

    def handle_event(self, sender: Any, **kwargs: Any) -> None:
        """Handle a dispatched event by persisting an audit record.

        Extracts event metadata from *kwargs* (populated by
        :func:`~src.app.events.event_bus.emit_event`), creates an
        :class:`~src.app.models.audit_event.AuditEvent` instance, and
        commits it to the database.

        Args:
            sender: The object that emitted the event (Blinker convention).
            **kwargs: Must contain ``'event_type'`` (:class:`EventType` or
                ``str``) and ``'payload'`` (``dict``).  The payload may
                include ``'user_id'`` and ``'ip_address'`` keys.
        """
        try:
            event_type = kwargs.get("event_type")
            payload: dict = kwargs.get("payload", {})

            # Safely extract fields from payload
            if not isinstance(payload, dict):
                payload = {}

            user_id: Optional[str] = payload.get("user_id")
            ip_address: Optional[str] = payload.get("ip_address")

            # Fallback: extract user_id from the Flask request context
            # (g.current_user) when the event payload does not include it.
            # This ensures audit events always capture the authenticated
            # user who triggered the action, even if the emitting service
            # code did not explicitly pass user_id in the payload (F-303).
            if user_id is None and has_request_context():
                try:
                    current_user = getattr(g, "current_user", None)
                    if current_user is not None:
                        user_id = getattr(current_user, "user_id", None)
                except RuntimeError:
                    # Outside request context — keep user_id as None
                    pass

            # Fallback: extract ip_address from the Flask request when
            # the event payload does not include it.
            if ip_address is None and has_request_context():
                try:
                    from flask import request as _flask_request
                    ip_address = _flask_request.remote_addr
                except (RuntimeError, ImportError):
                    pass
            domain: str = self._get_domain(event_type)

            # Determine the string representation of the event type
            event_type_str: str = (
                event_type.value
                if hasattr(event_type, "value")
                else str(event_type)
            )

            # Check application configuration for audit logging toggle
            audit_enabled: bool = True
            try:
                audit_enabled = current_app.config.get(
                    "AUDIT_LOGGING_ENABLED", True
                )
            except RuntimeError:
                # Outside application context — default to enabled
                pass

            if not audit_enabled:
                self.logger.debug(
                    "Audit logging disabled, skipping event: %s",
                    event_type_str,
                )
                return

            # Sanitize the payload before persisting — strip known sensitive
            # keys (passwords, tokens, API keys, secrets) to prevent
            # credential leakage into the audit trail (CWE-532).
            sanitized_payload: dict = self._sanitize_payload(payload)

            audit_event = AuditEvent(
                event_type=event_type_str,
                user_id=user_id,
                domain=domain,
                attributes=sanitized_payload,
                ip_address=ip_address,
                timestamp=datetime.now(timezone.utc),
            )

            db.session.add(audit_event)
            db.session.commit()

            # Write to the dedicated audit log file (logs/audit.log) via the
            # src.app.audit logger.  This provides a file-based backup of the
            # audit trail in case the database is unavailable or corrupted.
            self._audit_file_logger.info(
                "AUDIT event_type=%s domain=%s user=%s ip=%s",
                event_type_str,
                domain,
                user_id or "anonymous",
                ip_address or "unknown",
            )

            self.logger.debug(
                "Audit event created: type=%s, domain=%s, user=%s",
                event_type_str,
                domain,
                user_id,
            )

        except Exception as exc:
            # Rollback the failed database transaction to prevent session
            # corruption, but never propagate the exception.
            try:
                db.session.rollback()
            except Exception:
                pass  # Rollback itself failed — nothing more we can do

            self.logger.error(
                "Failed to persist audit event: %s",
                str(exc),
                exc_info=True,
            )

    #: Keys in event payloads that contain sensitive data and must be
    #: stripped before persisting to the audit trail.  Covers common
    #: credential fields across authentication, S3, SMTP, and proxy configs.
    _SENSITIVE_PAYLOAD_KEYS: frozenset[str] = frozenset({
        "password",
        "password_hash",
        "new_password",
        "old_password",
        "token",
        "api_key",
        "secret",
        "secret_key",
        "secretAccessKey",
        "accessKeyId",
        "access_token",
        "refresh_token",
        "jwt",
        "authorization",
        "credentials",
    })

    #: Replacement value for redacted sensitive fields.
    _REDACTED: str = "**REDACTED**"

    def _sanitize_payload(self, payload: dict) -> dict:
        """Remove sensitive keys from an event payload before persistence.

        Performs a shallow copy of the payload dict and replaces values of
        known sensitive keys with a ``**REDACTED**`` marker.  Nested dicts
        are recursively sanitized to catch sensitive data at any depth.

        Args:
            payload: The raw event payload dictionary.

        Returns:
            A new dictionary with sensitive values redacted.
        """
        if not isinstance(payload, dict):
            return payload

        sanitized: dict = {}
        for key, value in payload.items():
            if key.lower() in {k.lower() for k in self._SENSITIVE_PAYLOAD_KEYS}:
                sanitized[key] = self._REDACTED
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_payload(value)
            else:
                sanitized[key] = value
        return sanitized

    def _get_domain(self, event_type: Any) -> str:
        """Map an event type to its logical audit domain.

        Args:
            event_type: An :class:`EventType` enum member or string.

        Returns:
            Domain string such as ``'repository'``, ``'security'``,
            ``'configuration'``, ``'task'``, ``'blobstore'``, ``'cleanup'``,
            ``'component'``, ``'asset'``, or ``'system'`` (default fallback).
        """
        return _resolve_domain(event_type)


# ===========================================================================
# MetricsSubscriber (Feature F-401)
# ===========================================================================


class MetricsSubscriber:
    """Tracks event counts and processing durations via Prometheus metrics.

    This subscriber increments Prometheus counters and records timing
    histograms when events are dispatched through the event bus.  It replaces
    Dropwizard Metrics 4.2.25 event counting from the Java source system and
    supports **Feature F-401 (Health Checks and Monitoring)**.

    **Class-Level Metrics:**

    - :attr:`EVENTS_TOTAL` — a :class:`~prometheus_client.Counter` labelled
      by ``event_type`` and ``domain``, tracking the total number of events.
    - :attr:`EVENTS_PROCESSING_TIME` — a :class:`~prometheus_client.Histogram`
      labelled by ``event_type``, measuring event processing duration in
      seconds.

    **Fault Tolerance:**
    Metrics recording errors are caught and logged but **never** re-raised.
    A metrics failure must not interrupt event processing.
    """

    # -- Class-Level Prometheus Metrics ------------------------------------
    # These are registered once with the default Prometheus registry.
    # Thread-safe by design — Counter.inc() and Histogram.observe() are
    # atomic operations protected by internal locks.

    EVENTS_TOTAL: Counter = Counter(
        "nexus_events_total",
        "Total number of events dispatched",
        ["event_type", "domain"],
    )

    EVENTS_PROCESSING_TIME: Histogram = Histogram(
        "nexus_event_processing_seconds",
        "Time spent processing events",
        ["event_type"],
    )

    def __init__(self) -> None:
        """Initialise the metrics subscriber with a class-specific logger."""
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.MetricsSubscriber"
        )

    def register(self) -> None:
        """Subscribe to all event types for metrics collection.

        This subscriber monitors **every** event type to provide
        comprehensive metrics coverage across the entire event system.
        """
        count: int = 0
        for event_type in EventType:
            subscribe(event_type, self.handle_event)
            count += 1
        self.logger.info(
            "MetricsSubscriber registered for %d event types", count
        )

    def handle_event(self, sender: Any, **kwargs: Any) -> None:
        """Handle a dispatched event by updating Prometheus metrics.

        Increments :attr:`EVENTS_TOTAL` with ``event_type`` and ``domain``
        labels.  Records event processing time in
        :attr:`EVENTS_PROCESSING_TIME`.

        Args:
            sender: The object that emitted the event (Blinker convention).
            **kwargs: Must contain ``'event_type'`` (:class:`EventType` or
                ``str``).
        """
        start_time: float = time.monotonic()
        try:
            event_type = kwargs.get("event_type")
            event_type_str: str = (
                event_type.value
                if hasattr(event_type, "value")
                else str(event_type)
            )

            domain: str = _resolve_domain(event_type)

            # Increment the total event counter with type and domain labels
            self.EVENTS_TOTAL.labels(
                event_type=event_type_str,
                domain=domain,
            ).inc()

            # Record the processing duration for this event handling
            elapsed: float = time.monotonic() - start_time
            self.EVENTS_PROCESSING_TIME.labels(
                event_type=event_type_str,
            ).observe(elapsed)

            self.logger.debug(
                "Metrics recorded for event: %s (domain=%s)",
                event_type_str,
                domain,
            )

        except Exception as exc:
            self.logger.error(
                "Failed to record metrics: %s",
                str(exc),
                exc_info=True,
            )


# ===========================================================================
# CacheInvalidationSubscriber (Feature F-404)
# ===========================================================================


class CacheInvalidationSubscriber:
    """Invalidates cached configuration values when system config changes.

    This subscriber listens for :attr:`EventType.CONFIG_CHANGED` events and
    invokes registered cache invalidation handlers whose configuration key
    prefix matches the changed key.  It supports **Feature F-404 (System
    Configuration Management)** by ensuring real-time propagation of
    configuration changes to all interested components.

    **Handler Registration:**
    Other modules register interest in specific configuration key prefixes
    via :meth:`register_cache_handler`.  When a ``CONFIG_CHANGED`` event
    arrives, all handlers whose prefix matches the changed key are invoked.

    **Example:**

    .. code-block:: python

        cache_sub = CacheInvalidationSubscriber()
        cache_sub.register()

        def on_security_config_changed(key, payload):
            # Invalidate security-related caches
            ...

        cache_sub.register_cache_handler('security.', on_security_config_changed)

    **Fault Tolerance:**
    Individual handler failures are caught and logged.  A failing handler
    does not prevent other matching handlers from executing.
    """

    def __init__(self) -> None:
        """Initialise the cache invalidation subscriber.

        Creates a class-specific logger and an empty dictionary for
        cache handler registrations keyed by configuration key prefix.
        """
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.CacheInvalidationSubscriber"
        )
        self._cache_handlers: Dict[str, Callable] = {}

    def register(self) -> None:
        """Subscribe to CONFIG_CHANGED events only.

        Unlike :class:`AuditLogSubscriber` and :class:`MetricsSubscriber`,
        this subscriber listens for a **single** event type because cache
        invalidation is triggered exclusively by configuration changes.
        """
        subscribe(EventType.CONFIG_CHANGED, self.handle_config_changed)
        self.logger.info(
            "CacheInvalidationSubscriber registered for CONFIG_CHANGED events"
        )

    def register_cache_handler(
        self, config_key_prefix: str, handler: Callable
    ) -> None:
        """Register a cache invalidation handler for a config key prefix.

        When a ``CONFIG_CHANGED`` event is dispatched and the changed
        configuration key starts with *config_key_prefix*, *handler* is
        invoked with ``(key, payload)`` arguments.

        Args:
            config_key_prefix: Dot-notation prefix to match against changed
                configuration keys (e.g. ``'security.'``, ``'storage.'``).
            handler: Callable accepting ``(key: str, payload: dict)``
                arguments.  Will be invoked when a matching config key
                changes.
        """
        self._cache_handlers[config_key_prefix] = handler
        self.logger.debug(
            "Cache handler registered for prefix: %s", config_key_prefix
        )

    def handle_config_changed(self, sender: Any, **kwargs: Any) -> None:
        """Handle a CONFIG_CHANGED event by invoking matching cache handlers.

        Extracts the configuration key from the event payload and iterates
        through all registered handlers, invoking those whose prefix matches
        the changed key.

        Args:
            sender: The object that emitted the event (Blinker convention).
            **kwargs: Must contain ``'payload'`` with a ``'key'`` entry
                identifying the changed configuration key.
        """
        try:
            payload: dict = kwargs.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}

            key: str = payload.get("key", "")

            matched_count: int = 0
            for prefix, handler in self._cache_handlers.items():
                if key.startswith(prefix):
                    try:
                        handler(key, payload)
                        matched_count += 1
                    except Exception as handler_exc:
                        self.logger.error(
                            "Cache handler error for prefix '%s': %s",
                            prefix,
                            str(handler_exc),
                            exc_info=True,
                        )

            self.logger.info(
                "Config changed: %s, invalidating %d cache handler(s)",
                key,
                matched_count,
            )

        except Exception as exc:
            self.logger.error(
                "CacheInvalidationSubscriber error: %s",
                str(exc),
                exc_info=True,
            )


# ===========================================================================
# CleanupTriggerSubscriber (Feature F-204)
# ===========================================================================


class CleanupTriggerSubscriber:
    """Evaluates cleanup policies in response to content or config changes.

    This subscriber triggers cleanup policy evaluation when specific events
    occur — namely component uploads and repository configuration changes.
    It supports **Feature F-204 (Cleanup Policies)** by ensuring that
    cleanup rules are re-evaluated whenever relevant content or configuration
    changes.

    **Callback Pattern:**
    The actual cleanup evaluation logic lives in the cleanup service.  This
    subscriber uses a callback pattern (:meth:`set_cleanup_callback`) to
    decouple from the cleanup service — avoiding circular imports and
    maintaining clean architectural boundaries.

    **Subscribed Events:**

    - :attr:`EventType.COMPONENT_UPLOADED` — new content may trigger cleanup
    - :attr:`EventType.REPOSITORY_UPDATED` — repository config changes may
      affect cleanup policy applicability
    - :attr:`EventType.CLEANUP_COMPLETED` — informational; logged but no
      action taken

    **Fault Tolerance:**
    Cleanup trigger errors are caught and logged but **never** re-raised.
    A failing cleanup trigger must not interrupt the upload or configuration
    change that triggered it.
    """

    def __init__(self) -> None:
        """Initialise the cleanup trigger subscriber.

        Creates a class-specific logger and sets the cleanup callback to
        ``None`` — it must be registered later by the cleanup service via
        :meth:`set_cleanup_callback`.
        """
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.CleanupTriggerSubscriber"
        )
        self._cleanup_callback: Optional[Callable] = None

    def register(self) -> None:
        """Subscribe to events that may trigger cleanup policy evaluation.

        Registers :meth:`handle_event` for:

        - :attr:`EventType.COMPONENT_UPLOADED`
        - :attr:`EventType.REPOSITORY_UPDATED`
        - :attr:`EventType.CLEANUP_COMPLETED`
        """
        subscribe(EventType.COMPONENT_UPLOADED, self.handle_event)
        subscribe(EventType.REPOSITORY_UPDATED, self.handle_event)
        subscribe(EventType.CLEANUP_COMPLETED, self.handle_event)
        self.logger.info(
            "CleanupTriggerSubscriber registered for 3 event types"
        )

    def set_cleanup_callback(self, callback: Callable) -> None:
        """Register the cleanup evaluation callback.

        This method is called by the cleanup service during initialisation
        to provide the actual cleanup evaluation function.  The callback
        should accept keyword arguments ``repository_name`` and
        ``event_type``.

        Args:
            callback: Callable to invoke when cleanup-triggering events
                are received.  Signature:
                ``callback(repository_name=str, event_type=EventType)``
        """
        self._cleanup_callback = callback
        self.logger.debug("Cleanup callback registered")

    def handle_event(self, sender: Any, **kwargs: Any) -> None:
        """Handle events that may trigger cleanup policy evaluation.

        For :attr:`EventType.COMPONENT_UPLOADED` and
        :attr:`EventType.REPOSITORY_UPDATED`, extracts the
        ``repository_name`` from the payload and invokes the registered
        cleanup callback.  For :attr:`EventType.CLEANUP_COMPLETED`,
        logs the event but takes no further action (informational only).

        Args:
            sender: The object that emitted the event (Blinker convention).
            **kwargs: Must contain ``'event_type'`` and ``'payload'``.
                The payload should include ``'repository_name'`` for
                component upload and repository update events.
        """
        try:
            event_type = kwargs.get("event_type")
            payload: dict = kwargs.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}

            self.logger.debug("Cleanup trigger received: %s", event_type)

            # No callback registered — skip silently
            if self._cleanup_callback is None:
                self.logger.debug(
                    "No cleanup callback registered, skipping"
                )
                return

            # CLEANUP_COMPLETED is informational — no action needed
            if event_type == EventType.CLEANUP_COMPLETED:
                self.logger.debug(
                    "Cleanup completed event received, no further action"
                )
                return

            # For upload and update events, invoke the cleanup callback
            repository_name: str = payload.get("repository_name", "")

            if event_type in (
                EventType.COMPONENT_UPLOADED,
                EventType.REPOSITORY_UPDATED,
            ):
                self._cleanup_callback(
                    repository_name=repository_name,
                    event_type=event_type,
                )
                self.logger.debug(
                    "Cleanup callback invoked for repository: %s (event=%s)",
                    repository_name,
                    event_type,
                )

        except Exception as exc:
            self.logger.error(
                "Cleanup trigger error: %s",
                str(exc),
                exc_info=True,
            )


# ===========================================================================
# Registration Helper
# ===========================================================================


def register_all_subscribers() -> dict:
    """Register all built-in event subscribers.

    Creates instances of all four subscriber classes, calls their
    ``register()`` methods to subscribe to the appropriate event types,
    and returns a dictionary of the instances for optional reference by
    the calling code.

    This function is the **single entry point** called during Flask
    application factory initialisation in ``src/app/factory.py``.

    Returns:
        A dictionary mapping subscriber names to their instances::

            {
                'audit': AuditLogSubscriber,
                'metrics': MetricsSubscriber,
                'cache': CacheInvalidationSubscriber,
                'cleanup': CleanupTriggerSubscriber,
            }
    """
    subscribers: Dict[str, Any] = {}

    # 1. Audit Log Subscriber (Feature F-303)
    audit_subscriber = AuditLogSubscriber()
    audit_subscriber.register()
    subscribers["audit"] = audit_subscriber

    # 2. Metrics Subscriber (Feature F-401)
    metrics_subscriber = MetricsSubscriber()
    metrics_subscriber.register()
    subscribers["metrics"] = metrics_subscriber

    # 3. Cache Invalidation Subscriber (Feature F-404)
    cache_subscriber = CacheInvalidationSubscriber()
    cache_subscriber.register()
    subscribers["cache"] = cache_subscriber

    # 4. Cleanup Trigger Subscriber (Feature F-204)
    cleanup_subscriber = CleanupTriggerSubscriber()
    cleanup_subscriber.register()
    subscribers["cleanup"] = cleanup_subscriber

    logger.info("All %d event subscribers registered", len(subscribers))
    return subscribers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "AuditLogSubscriber",
    "MetricsSubscriber",
    "CacheInvalidationSubscriber",
    "CleanupTriggerSubscriber",
    "register_all_subscribers",
]
