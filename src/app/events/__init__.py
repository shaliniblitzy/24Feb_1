"""
Event system package for the Nexus Repository Flask application.

Implements the Observer pattern using Blinker signals, replacing the
Guava EventBus from the Java source system. Provides:
- ``event_types.py``:  Event type constants and payload dataclasses
- ``event_bus.py``:    Signal-based event dispatch (future checkpoint)
- ``subscribers.py``:  Event subscribers for audit logging, webhooks, etc. (future checkpoint)
"""

from src.app.events.event_types import EventType  # noqa: F401
from src.app.events.event_types import (  # noqa: F401
    RepositoryEventPayload,
    ComponentEventPayload,
    AssetEventPayload,
    SecurityEventPayload,
    ConfigEventPayload,
    TaskEventPayload,
    BlobStoreEventPayload,
    CleanupEventPayload,
)
