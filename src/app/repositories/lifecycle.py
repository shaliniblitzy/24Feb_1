"""
Repository Lifecycle State Machine.

This module manages the lifecycle state transitions for repository instances —
from creation (``NEW``) through operation (``STARTED``), quiescence
(``STOPPED``), and removal (``DELETED``).  It implements the **StateGuard**
pattern from the Java source system (``RepositoryImpl.java``) to enforce valid
state transitions and prevent operations on repositories in invalid states.

**Architecture Context:**

Replaces ``RepositoryImpl.java`` lifecycle management from Section 5.2.4 of
the Technical Specification.  The State Machine pattern is explicitly listed in
AAP Section 0.4.3 as a key design pattern.

**State Transition Diagram:**

.. code-block:: text

    ┌─────┐  start   ┌─────────┐  stop   ┌─────────┐
    │ NEW │ ───────► │ STARTED │ ──────► │ STOPPED │
    └──┬──┘          └────┬────┘         └────┬────┘
       │                  │   ◄───────────────┘
       │                  │       start (resume)
       │   delete         │  delete         │  delete
       ▼                  ▼                 ▼
    ┌─────────┐     ┌─────────┐       ┌─────────┐
    │ DELETED │     │ DELETED │       │ DELETED │
    └─────────┘     └─────────┘       └─────────┘
         (terminal — no further transitions)

**Exports:**

- :class:`RepositoryState` — enum of lifecycle states
- :data:`VALID_TRANSITIONS` — allowed state transition map
- :class:`LifecycleError` — base lifecycle exception
- :class:`InvalidStateTransitionError` — illegal transition attempt
- :class:`InvalidStateError` — operation in wrong state
- :class:`StateGuard` — decorator/guard for state-aware operations
- :class:`RepositoryLifecycle` — core state machine manager

**Event Integration:**

All state transitions emit :attr:`EventType.REPOSITORY_UPDATED` events.
Deletion additionally emits :attr:`EventType.REPOSITORY_DELETED`.

**Persistence:**

State is stored in ``repository.attributes['lifecycle']['state']`` (JSON
column) so it survives application restarts.

**Usage:**

.. code-block:: python

    from src.app.repositories.lifecycle import RepositoryLifecycle, RepositoryState

    lifecycle = RepositoryLifecycle(repository)
    lifecycle.start()       # NEW → STARTED
    lifecycle.stop()        # STARTED → STOPPED
    lifecycle.start()       # STOPPED → STARTED (resume)
    lifecycle.delete()      # STARTED → DELETED (terminal)
"""

from __future__ import annotations

import enum
import logging
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable

from flask import current_app

from src.app.extensions import db
from src.app.models.repository import Repository
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source.  Provides structured diagnostic output for lifecycle operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)


# ===========================================================================
# RepositoryState Enum
# ===========================================================================


class RepositoryState(str, enum.Enum):
    """Lifecycle state of a repository instance.

    Inheriting from ``str`` **and** ``enum.Enum`` provides two key benefits:

    1. **JSON serialisation** — values are plain strings, serialisable by
       ``json.dumps`` without a custom encoder.
    2. **Blinker signal compatibility** — values can be used directly as
       signal names (e.g. ``event_signals.signal(state.value)``).

    Members:

    =========  =========================================================
    State      Description
    =========  =========================================================
    NEW        Just created, not yet initialised.
    STARTED    Active and serving content.
    STOPPED    Temporarily quiesced, not serving requests.
    DELETED    Marked for removal — **terminal state** (no transitions).
    =========  =========================================================
    """

    NEW: str = "new"
    """Just created, not yet initialised."""

    STARTED: str = "started"
    """Active and serving content."""

    STOPPED: str = "stopped"
    """Temporarily quiesced, not serving requests."""

    DELETED: str = "deleted"
    """Marked for removal — terminal state."""

    # -- Convenience Properties ---------------------------------------------

    @property
    def is_active(self) -> bool:
        """Return ``True`` if the repository is actively serving content.

        Only the ``STARTED`` state is considered active.
        """
        return self == RepositoryState.STARTED

    @property
    def is_terminal(self) -> bool:
        """Return ``True`` if the state is terminal (no further transitions).

        Only the ``DELETED`` state is terminal.
        """
        return self == RepositoryState.DELETED


# ===========================================================================
# State Transition Map
# ===========================================================================

VALID_TRANSITIONS: dict[RepositoryState, set[RepositoryState]] = {
    RepositoryState.NEW: {RepositoryState.STARTED, RepositoryState.DELETED},
    RepositoryState.STARTED: {RepositoryState.STOPPED, RepositoryState.DELETED},
    RepositoryState.STOPPED: {RepositoryState.STARTED, RepositoryState.DELETED},
    RepositoryState.DELETED: set(),  # Terminal — no transitions allowed
}
"""Allowed state transitions.

Encodes the following transition rules from AAP Section 0.4.3:

- ``NEW → STARTED`` — initialisation complete
- ``NEW → DELETED`` — abort creation
- ``STARTED → STOPPED`` — quiesce
- ``STARTED → DELETED`` — remove
- ``STOPPED → STARTED`` — resume
- ``STOPPED → DELETED`` — remove
- ``DELETED → ∅`` — terminal; no further transitions
"""


# ===========================================================================
# Custom Exception Hierarchy
# ===========================================================================


class LifecycleError(Exception):
    """Base exception for all repository lifecycle violations.

    Attributes:
        message: Human-readable description of the error.
        repository_name: Name of the repository that triggered the error,
            or ``None`` if unknown.
    """

    def __init__(
        self,
        message: str = "Lifecycle error occurred",
        repository_name: str | None = None,
    ) -> None:
        self.message: str = message
        self.repository_name: str | None = repository_name
        super().__init__(self.message)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"repository_name={self.repository_name!r})"
        )


class InvalidStateTransitionError(LifecycleError):
    """Raised when an illegal state transition is attempted.

    Replaces ``InvalidStateException.java`` from the source system
    (AAP Section 0.8.5).

    Attributes:
        from_state: The current state when the transition was attempted.
        to_state: The target state that was rejected.
        message: Formatted description including repository name and states.
        repository_name: Name of the affected repository.
    """

    def __init__(
        self,
        from_state: RepositoryState,
        to_state: RepositoryState,
        repository_name: str | None = None,
    ) -> None:
        self.from_state: RepositoryState = from_state
        self.to_state: RepositoryState = to_state
        message = (
            f"Invalid state transition for repository "
            f"'{repository_name or 'unknown'}': "
            f"{from_state.value} \u2192 {to_state.value}"
        )
        super().__init__(message=message, repository_name=repository_name)


class InvalidStateError(LifecycleError):
    """Raised when an operation is attempted on a repository in an invalid state.

    Replaces the StateGuard assertion pattern from ``RepositoryImpl.java``.

    Attributes:
        current_state: The actual state of the repository.
        required_states: The set of states in which the operation is valid.
        message: Formatted description of the state mismatch.
        repository_name: Name of the affected repository.
    """

    def __init__(
        self,
        current_state: RepositoryState,
        required_states: set[RepositoryState],
        repository_name: str | None = None,
    ) -> None:
        self.current_state: RepositoryState = current_state
        self.required_states: set[RepositoryState] = set(required_states)
        state_names = ", ".join(sorted(s.value for s in required_states))
        message = (
            f"Operation requires repository "
            f"'{repository_name or 'unknown'}' in state(s) "
            f"{{{state_names}}}, but current state is {current_state.value}"
        )
        super().__init__(message=message, repository_name=repository_name)


# ===========================================================================
# StateGuard — State-Aware Operation Guard
# ===========================================================================


class StateGuard:
    """Decorator that guards operations against invalid repository states.

    Ensures the repository is in one of the required states before allowing
    the decorated method to proceed.  Raises :class:`InvalidStateError` if
    the repository is not in an acceptable state.

    This replaces the Java ``StateGuard`` pattern from
    ``RepositoryImpl.java`` (AAP Section 0.8.5).

    **Decorator usage:**

    .. code-block:: python

        @StateGuard(RepositoryState.STARTED)
        def upload_artifact(self, path, content):
            ...

    The decorated method's ``self`` must expose a ``_lifecycle`` attribute
    (a :class:`RepositoryLifecycle` instance) and a ``name`` attribute.

    Attributes:
        required_states: Set of states in which the operation is permitted.
    """

    def __init__(self, *required_states: RepositoryState) -> None:
        if not required_states:
            raise ValueError("StateGuard requires at least one required state")
        self.required_states: set[RepositoryState] = set(required_states)

    def __call__(self, func: Callable) -> Callable:
        """Wrap *func* with a state check performed before invocation.

        The wrapper inspects ``instance._lifecycle.current_state`` and
        raises :class:`InvalidStateError` if it is not in
        ``self.required_states``.

        Args:
            func: The method to guard.

        Returns:
            A wrapped function that enforces state constraints.
        """

        @wraps(func)
        def wrapper(instance: Any, *args: Any, **kwargs: Any) -> Any:
            lifecycle: RepositoryLifecycle | None = getattr(
                instance, "_lifecycle", None
            )
            if lifecycle is not None and (
                lifecycle.current_state not in self.required_states
            ):
                raise InvalidStateError(
                    current_state=lifecycle.current_state,
                    required_states=self.required_states,
                    repository_name=getattr(instance, "name", "unknown"),
                )
            return func(instance, *args, **kwargs)

        return wrapper

    def __repr__(self) -> str:  # pragma: no cover
        states = ", ".join(s.value for s in self.required_states)
        return f"StateGuard({states})"


# ===========================================================================
# RepositoryLifecycle — State Machine Core
# ===========================================================================


class RepositoryLifecycle:
    """Manages the lifecycle state machine for a repository instance.

    Enforces valid state transitions, persists state to the repository's
    ``attributes`` JSON column, emits lifecycle events via the event bus,
    and provides convenience query properties.

    Replaces ``RepositoryImpl.java`` lifecycle management from Section 5.2.4
    of the Technical Specification.

    Args:
        repository: The :class:`Repository` model instance to manage.

    Attributes:
        repository: Bound :class:`Repository` instance.
        name: Repository name (shortcut for ``repository.name``).
        logger: Per-repository logger.
    """

    def __init__(self, repository: Repository) -> None:
        self.repository: Repository = repository
        self.name: str = repository.name
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.{repository.name}"
        )

        # Load current state from persisted repository attributes.
        # Default to NEW for brand-new repositories.
        attrs: dict = repository.attributes or {}
        state_value: str = attrs.get("lifecycle", {}).get("state", "new")
        try:
            self._current_state: RepositoryState = RepositoryState(state_value)
        except ValueError:
            self.logger.warning(
                "Unknown lifecycle state '%s' for repository '%s'; "
                "defaulting to NEW.",
                state_value,
                self.name,
            )
            self._current_state = RepositoryState.NEW

        # In-memory transition history for the current process lifetime.
        self._state_history: list[dict[str, str]] = []

        self.logger.debug(
            "RepositoryLifecycle initialised for '%s' in state '%s'.",
            self.name,
            self._current_state.value,
        )

    # -- State Properties ---------------------------------------------------

    @property
    def current_state(self) -> RepositoryState:
        """The current lifecycle state of the repository."""
        return self._current_state

    @property
    def state_history(self) -> list[dict[str, str]]:
        """Ordered list of state transition records for the current session.

        Each record is a dict with keys ``from_state``, ``to_state``, and
        ``timestamp`` (ISO 8601 UTC).

        Returns a **copy** to prevent external mutation.
        """
        return list(self._state_history)

    # -- Core State Transition ----------------------------------------------

    def transition_to(self, target_state: RepositoryState) -> None:
        """Perform a state transition to *target_state*.

        This is the **core transition method**.  It validates the transition
        against :data:`VALID_TRANSITIONS`, records history, persists the new
        state to ``repository.attributes['lifecycle']``, and emits a
        :attr:`EventType.REPOSITORY_UPDATED` event.

        Args:
            target_state: The desired new state.

        Raises:
            InvalidStateTransitionError: If the transition from the current
                state to *target_state* is not allowed.
        """
        # Step 1 — Validate transition
        allowed: set[RepositoryState] = VALID_TRANSITIONS.get(
            self._current_state, set()
        )
        if target_state not in allowed:
            self.logger.error(
                "Invalid transition attempted for '%s': %s -> %s. "
                "Allowed: %s",
                self.name,
                self._current_state.value,
                target_state.value,
                ", ".join(s.value for s in allowed) if allowed else "(none)",
            )
            raise InvalidStateTransitionError(
                from_state=self._current_state,
                to_state=target_state,
                repository_name=self.name,
            )

        # Step 2 — Record transition in history
        transition_record: dict[str, str] = {
            "from_state": self._current_state.value,
            "to_state": target_state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._state_history.append(transition_record)

        # Step 3 — Update in-memory state
        previous_state: RepositoryState = self._current_state
        self._current_state = target_state

        # Step 4 — Persist to repository attributes (JSON column)
        # SQLAlchemy JSON mutation tracking requires top-level reassignment
        # of the column value — in-place dict mutation is NOT detected.
        attrs: dict = dict(self.repository.attributes or {})
        lifecycle_attrs: dict = attrs.get("lifecycle", {})
        lifecycle_attrs["state"] = target_state.value
        lifecycle_attrs["last_transition"] = transition_record
        attrs["lifecycle"] = lifecycle_attrs
        self.repository.attributes = attrs
        db.session.add(self.repository)
        db.session.commit()

        # Step 5 — Emit lifecycle event
        emit_event(
            EventType.REPOSITORY_UPDATED,
            {
                "repository_name": self.name,
                "event_subtype": "state_transition",
                "from_state": previous_state.value,
                "to_state": target_state.value,
            },
        )

        # Step 6 — Log the transition
        self.logger.info(
            "Repository '%s' transitioned: %s -> %s",
            self.name,
            previous_state.value,
            target_state.value,
        )

    # -- Transition Query Methods -------------------------------------------

    def can_transition_to(self, target_state: RepositoryState) -> bool:
        """Check whether a transition to *target_state* is valid.

        This is a **read-only** check — no state mutation occurs.

        Args:
            target_state: The state to test.

        Returns:
            ``True`` if the transition would be accepted, ``False`` otherwise.
        """
        return target_state in VALID_TRANSITIONS.get(
            self._current_state, set()
        )

    def get_valid_transitions(self) -> set[RepositoryState]:
        """Return the set of states reachable from the current state.

        Returns a **copy** to prevent external mutation.
        """
        return VALID_TRANSITIONS.get(self._current_state, set()).copy()

    # -- Convenience Lifecycle Methods --------------------------------------

    def start(self) -> None:
        """Transition the repository to :attr:`RepositoryState.STARTED`.

        Convenience wrapper around :meth:`transition_to`.

        Raises:
            InvalidStateTransitionError: If the transition is not allowed
                from the current state.
        """
        self.transition_to(RepositoryState.STARTED)
        self.logger.info("Repository '%s' started.", self.name)

    def stop(self) -> None:
        """Transition the repository to :attr:`RepositoryState.STOPPED`.

        Convenience wrapper around :meth:`transition_to`.

        Raises:
            InvalidStateTransitionError: If the transition is not allowed
                from the current state.
        """
        self.transition_to(RepositoryState.STOPPED)
        self.logger.info("Repository '%s' stopped.", self.name)

    def delete(self) -> None:
        """Transition the repository to :attr:`RepositoryState.DELETED`.

        In addition to the standard :attr:`EventType.REPOSITORY_UPDATED`
        event emitted by :meth:`transition_to`, this method also emits an
        :attr:`EventType.REPOSITORY_DELETED` event for audit logging
        (Feature F-303) and webhook dispatch (Feature F-503).

        Raises:
            InvalidStateTransitionError: If the transition is not allowed
                from the current state.
        """
        self.transition_to(RepositoryState.DELETED)

        # Emit additional REPOSITORY_DELETED event
        emit_event(
            EventType.REPOSITORY_DELETED,
            {
                "repository_name": self.name,
                "event_subtype": "repository_deleted",
            },
        )

        self.logger.info(
            "Repository '%s' marked for deletion.", self.name
        )

    # -- State Query Properties ---------------------------------------------

    @property
    def is_started(self) -> bool:
        """``True`` if the repository is in the ``STARTED`` state."""
        return self._current_state == RepositoryState.STARTED

    @property
    def is_stopped(self) -> bool:
        """``True`` if the repository is in the ``STOPPED`` state."""
        return self._current_state == RepositoryState.STOPPED

    @property
    def is_new(self) -> bool:
        """``True`` if the repository is in the ``NEW`` state."""
        return self._current_state == RepositoryState.NEW

    @property
    def is_deleted(self) -> bool:
        """``True`` if the repository is in the ``DELETED`` state."""
        return self._current_state == RepositoryState.DELETED

    @property
    def is_active(self) -> bool:
        """``True`` if the repository is actively serving content.

        Alias for :attr:`is_started`.
        """
        return self.is_started

    # -- State Guard Helpers ------------------------------------------------

    def ensure_started(self) -> None:
        """Raise :class:`InvalidStateError` unless the repository is STARTED.

        Used as a precondition check before content-serving operations.

        Raises:
            InvalidStateError: If the repository is not in the ``STARTED``
                state.
        """
        if self._current_state != RepositoryState.STARTED:
            raise InvalidStateError(
                current_state=self._current_state,
                required_states={RepositoryState.STARTED},
                repository_name=self.name,
            )

    def ensure_not_deleted(self) -> None:
        """Raise :class:`InvalidStateError` if the repository is DELETED.

        Used as a precondition check before any modification operation.

        Raises:
            InvalidStateError: If the repository is in the ``DELETED``
                state.
        """
        if self._current_state == RepositoryState.DELETED:
            raise InvalidStateError(
                current_state=self._current_state,
                required_states={
                    RepositoryState.NEW,
                    RepositoryState.STARTED,
                    RepositoryState.STOPPED,
                },
                repository_name=self.name,
            )

    # -- Status Reporting ---------------------------------------------------

    def get_lifecycle_status(self) -> dict[str, Any]:
        """Return a comprehensive snapshot of the lifecycle status.

        Returns:
            A dictionary containing:

            - ``current_state`` — string value of the current state
            - ``is_active`` — whether the repository is serving content
            - ``valid_transitions`` — list of reachable state values
            - ``transition_count`` — number of transitions in this session
            - ``last_transition`` — last transition record, or ``None``
        """
        return {
            "current_state": self._current_state.value,
            "is_active": self.is_active,
            "valid_transitions": [
                s.value for s in self.get_valid_transitions()
            ],
            "transition_count": len(self._state_history),
            "last_transition": (
                self._state_history[-1] if self._state_history else None
            ),
        }


# ===========================================================================
# Module-Level Export List
# ===========================================================================

__all__: list[str] = [
    "RepositoryState",
    "VALID_TRANSITIONS",
    "LifecycleError",
    "InvalidStateTransitionError",
    "InvalidStateError",
    "StateGuard",
    "RepositoryLifecycle",
]
