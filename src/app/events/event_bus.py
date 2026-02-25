"""
Central Event Dispatch Module.

Replaces the Guava EventBus from the Java source system (Sonatype Nexus
Repository) with `Blinker 1.9.0 <https://pypi.org/project/blinker/>`_
signal-based event dispatch.

This module provides the core event dispatch API implementing the **Observer
pattern** (AAP Section 0.4.3):

- :func:`emit_event` — dispatch events to all registered subscribers
- :func:`subscribe` — register event handlers
- :func:`unsubscribe` — remove event handlers
- :func:`get_signal` — retrieve / create Blinker signals per event type
- :func:`has_subscribers` / :func:`subscriber_count` — introspection helpers
- :func:`clear_all_subscribers` — testing cleanup utility

**Architecture Context:**

Events are dispatched **synchronously** within the same thread, matching Guava
EventBus default behaviour.  Thread-safe signal management is guaranteed by a
module-level :class:`threading.Lock`.

**Feature Dependencies:**

- Audit logging (Feature F-303)
- Webhook dispatch (Feature F-503)
- Configuration propagation (Feature F-404)
- Cleanup triggers (Feature F-204)

**Fault Tolerance:**

Subscriber exceptions are caught and logged but **never** re-raised — event
dispatch failures must not break the calling code.

**Usage Example:**

.. code-block:: python

    from src.app.events.event_bus import emit_event, subscribe
    from src.app.events.event_types import EventType

    def on_repo_created(sender, **kwargs):
        payload = kwargs['payload']
        print(f"Repository created: {payload.get('repository_name')}")

    subscribe(EventType.REPOSITORY_CREATED, on_repo_created)
    emit_event(EventType.REPOSITORY_CREATED, payload={
        'repository_name': 'my-repo',
        'format': 'maven2',
        'type': 'hosted',
    })
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Union

from blinker import NamedSignal

from src.app.extensions import event_signals
from src.app.events.event_types import EventType

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 structured logging from the Java source.
# Provides DEBUG-level event dispatch tracing, subscription tracking,
# and ERROR-level fault reporting when subscriber exceptions occur.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-Level Synchronization Primitive
# ---------------------------------------------------------------------------
# Thread-safe lock for Blinker Namespace signal registry access.
# While Blinker's internal dict operations are generally atomic in CPython
# (protected by the GIL), the explicit lock provides extra safety for
# concurrent signal creation / retrieval across multiple threads and is
# robust against alternative Python implementations.
# ---------------------------------------------------------------------------

_signal_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Default Anonymous Sender
# ---------------------------------------------------------------------------
# Used as the sender when emit_event() is called without an explicit sender.
# This is a unique sentinel object that Blinker receivers can identify when
# the emitter does not provide sender context.
# ---------------------------------------------------------------------------

_DEFAULT_SENDER: object = object()


# ---------------------------------------------------------------------------
# Signal Management
# ---------------------------------------------------------------------------

def get_signal(event_type: Union[EventType, str]) -> NamedSignal:
    """Get or create a Blinker :class:`~blinker.NamedSignal` for a specific
    event type.

    Blinker's :meth:`Namespace.signal` is **idempotent** — calling it
    multiple times with the same name returns the same signal instance.
    The module-level lock provides additional thread safety for concurrent
    signal registry access.

    Args:
        event_type: The event type to retrieve a signal for.  Accepts
            both :class:`EventType` enum values and plain strings for
            flexibility.

    Returns:
        A :class:`~blinker.NamedSignal` instance for the specified event
        type.
    """
    # Normalise to string — EventType inherits from str, so .value yields
    # a Blinker-compatible signal name (e.g. "repository.created").
    signal_name: str = (
        event_type.value if isinstance(event_type, EventType) else str(event_type)
    )

    with _signal_lock:
        signal: NamedSignal = event_signals.signal(signal_name)

    logger.debug("Signal retrieved/created for event type: %s", signal_name)
    return signal


# ---------------------------------------------------------------------------
# Primary Event Dispatch API
# ---------------------------------------------------------------------------

def emit_event(
    event_type: Union[EventType, str],
    payload: Optional[Dict[str, Any]] = None,
    sender: Any = None,
) -> None:
    """Dispatch an event to all registered subscribers.

    This is the **primary API** for event dispatch in the application,
    replacing ``EventBus.post()`` from Guava.

    The *payload* dictionary is automatically enriched with:

    - ``'timestamp'`` — ISO 8601 UTC timestamp (only if not already set)
    - ``'event_type'`` — string representation of the event type (only if
      not already set)

    Events are dispatched **synchronously** within the calling thread.
    If any subscriber raises an exception, it is caught and logged at
    ERROR level but **never re-raised** — ensuring fault tolerance.

    Args:
        event_type: The type of event being dispatched.
        payload: Optional dictionary containing event-specific data.
            Defaults to an empty dict if ``None``.
        sender: Optional sender object passed to Blinker's
            :meth:`Signal.send`.  Defaults to an internal anonymous
            sentinel.
    """
    try:
        # Normalise payload — callers may pass None
        payload = payload if payload is not None else {}

        # Auto-inject temporal and type metadata.
        # setdefault preserves any caller-provided values.
        payload.setdefault(
            "timestamp",
            datetime.now(timezone.utc).isoformat(),
        )
        payload.setdefault(
            "event_type",
            event_type.value if isinstance(event_type, EventType) else str(event_type),
        )

        # Retrieve (or create) the signal for this event type
        signal: NamedSignal = get_signal(event_type)

        # Optimisation: skip Blinker dispatch overhead when no receivers
        # are registered — avoids unnecessary dict iteration and call setup.
        if not signal.receivers:
            logger.debug(
                "Event %s skipped — no receivers registered.",
                event_type,
            )
            return

        receiver_count: int = len(signal.receivers)

        # Synchronous dispatch to all subscribers (mirrors Guava EventBus)
        signal.send(
            sender if sender is not None else _DEFAULT_SENDER,
            event_type=event_type,
            payload=payload,
        )

        logger.debug(
            "Event dispatched: %s with %d receiver(s)",
            event_type,
            receiver_count,
        )

    except Exception as exc:
        # CRITICAL: Never re-raise — subscriber exceptions must not
        # propagate to the emitter.  This guarantees that business logic
        # calling emit_event() is never interrupted by faulty subscribers.
        logger.error(
            "Error dispatching event %s: %s",
            event_type,
            str(exc),
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Event Subscription API
# ---------------------------------------------------------------------------

def subscribe(
    event_type: Union[EventType, str],
    handler: Callable,
    sender: Any = None,
) -> None:
    """Register an event handler / callback for a specific event type.

    Replaces the ``@Subscribe`` annotation from Guava EventBus.

    The *handler* must accept the Blinker signal convention::

        def handler(sender, **kwargs):
            event_type = kwargs['event_type']
            payload = kwargs['payload']

    Args:
        event_type: The event type to subscribe to.
        handler: Callable to invoke when the event fires.  Must accept
            ``(sender, **kwargs)`` signature per Blinker convention.
        sender: Optional specific sender to filter on.  If ``None``
            (default), the handler receives events from **any** sender.
    """
    signal: NamedSignal = get_signal(event_type)

    if sender is None:
        # Receive from any sender
        signal.connect(handler)
    else:
        # Filter to a specific sender object
        signal.connect(handler, sender=sender)

    handler_name: str = (
        handler.__name__ if hasattr(handler, "__name__") else str(handler)
    )
    logger.debug(
        "Subscribed handler '%s' to event %s",
        handler_name,
        event_type,
    )


# ---------------------------------------------------------------------------
# Event Unsubscription API
# ---------------------------------------------------------------------------

def unsubscribe(
    event_type: Union[EventType, str],
    handler: Callable,
) -> None:
    """Remove a previously registered event handler.

    Silently ignores errors if the handler was not connected — this is
    consistent with the fail-safe design principle of the event bus.

    Args:
        event_type: The event type to unsubscribe from.
        handler: The handler callable to remove.
    """
    try:
        signal: NamedSignal = get_signal(event_type)
        signal.disconnect(handler)

        handler_name: str = (
            handler.__name__ if hasattr(handler, "__name__") else str(handler)
        )
        logger.debug(
            "Unsubscribed handler '%s' from event %s",
            handler_name,
            event_type,
        )
    except Exception as exc:
        # Silently handle disconnect errors (e.g. handler not connected,
        # or weakref already garbage-collected).
        logger.debug(
            "Unsubscribe ignored for event %s: %s",
            event_type,
            str(exc),
        )


# ---------------------------------------------------------------------------
# Introspection Utilities
# ---------------------------------------------------------------------------

def has_subscribers(event_type: Union[EventType, str]) -> bool:
    """Check whether the given event type has any registered subscribers.

    Args:
        event_type: The event type to check.

    Returns:
        ``True`` if at least one subscriber is registered; ``False``
        otherwise.
    """
    signal: NamedSignal = get_signal(event_type)
    return bool(signal.receivers)


def subscriber_count(event_type: Union[EventType, str]) -> int:
    """Return the number of subscribers for a given event type.

    Args:
        event_type: The event type to check.

    Returns:
        Non-negative integer count of registered subscribers.
    """
    signal: NamedSignal = get_signal(event_type)
    return len(signal.receivers)


# ---------------------------------------------------------------------------
# Testing Cleanup Utility
# ---------------------------------------------------------------------------

def clear_all_subscribers() -> None:
    """Remove **ALL** subscribers from **ALL** event signals.

    This is a **destructive** operation intended for testing cleanup and
    application shutdown.  It iterates through every :class:`EventType`
    enum member and clears all Blinker signal internals (receivers,
    sender-based routing, and receiver-based routing).

    .. warning::

        Calling this in production will disable all event-driven features
        (audit logging, webhooks, configuration propagation, cleanup
        triggers) until subscribers re-register.
    """
    logger.warning(
        "Clearing ALL event subscribers — this is a destructive operation."
    )

    cleared_count: int = 0
    for event_type_member in EventType:
        signal: NamedSignal = get_signal(event_type_member)
        receiver_count_before: int = len(signal.receivers)

        if receiver_count_before > 0:
            # Clear the three internal Blinker data structures that track
            # signal connections:
            #   - receivers: {receiver_id: weakref(callable)}
            #   - _by_sender: {sender_id: set(receiver_ids)}
            #   - _by_receiver: {receiver_id: set(sender_ids)}
            signal.receivers.clear()
            if hasattr(signal, "_by_sender"):
                signal._by_sender.clear()
            if hasattr(signal, "_by_receiver"):
                signal._by_receiver.clear()

            cleared_count += receiver_count_before

    logger.warning(
        "All event subscribers cleared: %d receiver(s) across %d event types.",
        cleared_count,
        len(EventType),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "emit_event",
    "subscribe",
    "unsubscribe",
    "get_signal",
    "has_subscribers",
    "subscriber_count",
    "clear_all_subscribers",
]
