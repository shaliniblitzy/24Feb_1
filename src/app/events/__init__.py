"""
Blinker signal-based event system for the Nexus Repository application.

This package replaces the Guava EventBus from the Java source system,
providing decoupled event dispatch using Blinker 1.9.0 signals.

The event system supports:
- Audit logging (F-303) via AuditLogSubscriber
- Metrics collection (F-401) via MetricsSubscriber
- Configuration propagation (F-404) via CacheInvalidationSubscriber
- Cleanup triggers (F-204) via CleanupTriggerSubscriber
- Webhook dispatch (F-503) via external webhook module integration

Usage:
    from src.app.events import emit_event, subscribe, EventType

    # Emit an event
    emit_event(EventType.REPOSITORY_CREATED, {
        'repository_name': 'maven-releases',
        'format': 'maven2',
        'type': 'hosted',
        'user_id': 'admin'
    })

    # Subscribe to an event
    def on_repo_created(sender, **kwargs):
        print(f"Repository created: {kwargs['payload']}")

    subscribe(EventType.REPOSITORY_CREATED, on_repo_created)
"""

# ---------------------------------------------------------------------------
# Re-exports from event_bus — Core event dispatch API
# ---------------------------------------------------------------------------
# These functions replace Guava EventBus.post() and @Subscribe from the
# Java source system.  Re-exported here to enable clean package-level
# imports like ``from src.app.events import emit_event, subscribe``.
# ---------------------------------------------------------------------------

from src.app.events.event_bus import emit_event  # noqa: F401
from src.app.events.event_bus import get_signal  # noqa: F401
from src.app.events.event_bus import subscribe  # noqa: F401
from src.app.events.event_bus import unsubscribe  # noqa: F401

# ---------------------------------------------------------------------------
# Re-exports from event_types — Event type constants
# ---------------------------------------------------------------------------
# EventType is a ``str``-based ``Enum`` defining all 13 event type constants.
# Re-exported here so consumers can write:
#   ``from src.app.events import EventType``
# ---------------------------------------------------------------------------

from src.app.events.event_types import EventType  # noqa: F401

# ---------------------------------------------------------------------------
# Re-exports from subscribers — Subscriber registration entry point
# ---------------------------------------------------------------------------
# register_all_subscribers() is called during Flask application factory
# initialisation in ``src/app/factory.py`` to set up all built-in event
# subscribers (AuditLogSubscriber, MetricsSubscriber,
# CacheInvalidationSubscriber, CleanupTriggerSubscriber).
# ---------------------------------------------------------------------------

from src.app.events.subscribers import register_all_subscribers  # noqa: F401

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "emit_event",
    "subscribe",
    "unsubscribe",
    "get_signal",
    "EventType",
    "register_all_subscribers",
]
