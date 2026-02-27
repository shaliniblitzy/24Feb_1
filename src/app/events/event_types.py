"""
Event type constants and payload schema definitions for the Nexus Repository
event dispatch system.

This module is the most foundational file in the events package — it defines
ALL event types and their expected payload structures. Every event dispatched
through the event bus (event_bus.py) must use one of the EventType constants
defined here.

This module replaces the Guava EventBus event classes from the Java source
system. EventType inherits from ``str`` and ``Enum`` so that its values can be
used directly as Blinker signal names (e.g.
``event_signals.signal(EventType.REPOSITORY_CREATED.value)``).

Payload dataclasses document and optionally validate the data carried by each
event.  They use ``@dataclass`` with ``field(default_factory=dict)`` for
mutable default values.

No external package dependencies — this module uses only the Python standard
library (``enum``, ``dataclasses``, ``typing``).  No internal package
dependencies — this is the most foundational module in the events package.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# EventType Enum
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    """Enumeration of all event types dispatched through the application
    event bus.

    Inheriting from ``str`` enables each member's value to serve directly as
    a Blinker signal name — for example::

        from blinker import Namespace
        signals = Namespace()
        sig = signals.signal(EventType.REPOSITORY_CREATED.value)

    Values use a dotted ``<domain>.<action>`` convention for readability.
    """

    # -- Repository events ---------------------------------------------------
    REPOSITORY_CREATED: str = "repository.created"
    """Emitted when a new repository is created."""

    REPOSITORY_UPDATED: str = "repository.updated"
    """Emitted when repository configuration changes."""

    REPOSITORY_DELETED: str = "repository.deleted"
    """Emitted when a repository is deleted."""

    # -- Component events ----------------------------------------------------
    COMPONENT_UPLOADED: str = "component.uploaded"
    """Emitted when a component/artifact is uploaded to a hosted repository."""

    COMPONENT_DELETED: str = "component.deleted"
    """Emitted when a component is deleted from a repository."""

    # -- Asset events --------------------------------------------------------
    ASSET_DOWNLOADED: str = "asset.downloaded"
    """Emitted when an asset is downloaded (tracks download statistics for
    cleanup policies)."""

    # -- Security events -----------------------------------------------------
    USER_AUTHENTICATED: str = "user.authenticated"
    """Emitted on successful authentication (any realm)."""

    USER_AUTHORIZATION_FAILED: str = "user.authorization_failed"
    """Emitted when authorization is denied."""

    # -- Configuration events ------------------------------------------------
    CONFIG_CHANGED: str = "config.changed"
    """Emitted when system configuration is modified (F-404)."""

    # -- Task events ---------------------------------------------------------
    TASK_STARTED: str = "task.started"
    """Emitted when a scheduled task begins execution."""

    TASK_COMPLETED: str = "task.completed"
    """Emitted when a scheduled task finishes (success or failure)."""

    # -- Storage events ------------------------------------------------------
    BLOBSTORE_COMPACTED: str = "blobstore.compacted"
    """Emitted after BlobStore compaction completes."""

    # -- Cleanup events ------------------------------------------------------
    CLEANUP_COMPLETED: str = "cleanup.completed"
    """Emitted after cleanup policy evaluation completes."""


# ---------------------------------------------------------------------------
# Event Payload Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RepositoryEventPayload:
    """Payload schema for repository lifecycle events
    (REPOSITORY_CREATED, REPOSITORY_UPDATED, REPOSITORY_DELETED).

    Attributes:
        repository_name: Unique repository identifier.
        format: Repository format — one of ``'maven2'``, ``'npm'``,
            ``'docker'``, ``'nuget'``, ``'pypi'``, ``'apt'``, ``'raw'``.
        type: Repository type — one of ``'hosted'``, ``'proxy'``,
            ``'group'``.
        user_id: Identifier of the user who triggered the event (optional).
        ip_address: Client IP address (optional).
        attributes: Arbitrary additional metadata (optional).
    """

    repository_name: str
    format: str
    type: str
    user_id: str | None = None
    ip_address: str | None = None
    attributes: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class ComponentEventPayload:
    """Payload schema for component upload/delete events
    (COMPONENT_UPLOADED, COMPONENT_DELETED).

    Attributes:
        repository_name: Repository containing the component.
        component_name: Component name (artifact ID).
        component_version: Component version string (optional).
        namespace: Component namespace / group (optional, e.g. Maven groupId).
        format: Repository format string.
        user_id: Identifier of the user who triggered the event (optional).
        ip_address: Client IP address (optional).
        attributes: Arbitrary additional metadata (optional).
    """

    repository_name: str
    component_name: str
    component_version: str | None = None
    namespace: str | None = None
    format: str = ""
    user_id: str | None = None
    ip_address: str | None = None
    attributes: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class AssetEventPayload:
    """Payload schema for asset download events (ASSET_DOWNLOADED).

    Attributes:
        repository_name: Repository from which the asset was downloaded.
        asset_path: Full storage path of the asset.
        content_type: MIME content type (optional).
        size: Asset size in bytes (optional).
        user_id: Identifier of the user who triggered the download (optional).
        ip_address: Client IP address (optional).
    """

    repository_name: str
    asset_path: str
    content_type: str | None = None
    size: int | None = None
    user_id: str | None = None
    ip_address: str | None = None


@dataclass(frozen=True)
class SecurityEventPayload:
    """Payload schema for authentication/authorization events
    (USER_AUTHENTICATED, USER_AUTHORIZATION_FAILED).

    Attributes:
        user_id: Identifier of the authenticating/authorized user (optional —
            may be ``None`` for failed anonymous attempts).
        realm: Authentication realm — one of ``'local'``, ``'bearer_token'``,
            ``'jwt'``, ``'ldap'``, ``'sso'`` (optional).
        ip_address: Client IP address (optional).
        reason: Failure reason — e.g. ``'invalid_credentials'``,
            ``'account_locked'``, ``'insufficient_privileges'`` (optional).
        resource: Resource being accessed for authorization events (optional).
        action: Action being attempted for authorization events (optional).
    """

    user_id: str | None = None
    realm: str | None = None
    ip_address: str | None = None
    reason: str | None = None
    resource: str | None = None
    action: str | None = None


@dataclass(frozen=True)
class ConfigEventPayload:
    """Payload schema for configuration change events (CONFIG_CHANGED).

    Attributes:
        key: Dot-notation configuration key (e.g.
            ``'security.anonymousAccess'``).
        old_value: Previous configuration value (optional).
        new_value: New configuration value (optional).
        category: Configuration category grouping (optional).
        user_id: Identifier of the user who changed the config (optional).
        ip_address: Client IP address (optional).
    """

    key: str
    old_value: str | None = None
    new_value: str | None = None
    category: str | None = None
    user_id: str | None = None
    ip_address: str | None = None


@dataclass(frozen=True)
class TaskEventPayload:
    """Payload schema for scheduled task events
    (TASK_STARTED, TASK_COMPLETED).

    Attributes:
        task_id: Unique task definition identifier.
        task_type: Task type key (e.g. ``'repository.cleanup'``,
            ``'blobstore.compact'``).
        task_name: Human-readable task name.
        status: Completion status — one of ``'ok'``, ``'failed'``,
            ``'canceled'`` (used with TASK_COMPLETED; optional).
        duration_ms: Execution duration in milliseconds (used with
            TASK_COMPLETED; optional).
        error_message: Error description for failed tasks (optional).
        user_id: Identifier of the user who triggered the task (optional).
    """

    task_id: str
    task_type: str
    task_name: str
    status: str | None = None
    duration_ms: int | None = None
    error_message: str | None = None
    user_id: str | None = None


@dataclass(frozen=True)
class BlobStoreEventPayload:
    """Payload schema for BlobStore maintenance events
    (BLOBSTORE_COMPACTED).

    Attributes:
        blob_store_name: Name of the BlobStore that was maintained.
        operation: Maintenance operation — one of ``'compact'``,
            ``'cleanup_temp'``, ``'integrity_check'``.
        blobs_removed: Number of blobs removed during operation.
        space_reclaimed_bytes: Bytes of storage reclaimed.
        duration_ms: Operation duration in milliseconds (optional).
    """

    blob_store_name: str
    operation: str
    blobs_removed: int = 0
    space_reclaimed_bytes: int = 0
    duration_ms: int | None = None


@dataclass(frozen=True)
class CleanupEventPayload:
    """Payload schema for cleanup completion events (CLEANUP_COMPLETED).

    Attributes:
        policy_name: Cleanup policy that was evaluated.
        repository_name: Target repository (``None`` if cleanup ran across
            all repositories).
        components_deleted: Number of components removed.
        assets_deleted: Number of assets removed.
        space_reclaimed_bytes: Bytes of storage reclaimed.
        duration_ms: Cleanup duration in milliseconds (optional).
    """

    policy_name: str
    repository_name: str | None = None
    components_deleted: int = 0
    assets_deleted: int = 0
    space_reclaimed_bytes: int = 0
    duration_ms: int | None = None


# ---------------------------------------------------------------------------
# Event Type → Payload Schema Mapping
# ---------------------------------------------------------------------------

EVENT_PAYLOAD_SCHEMAS: dict[EventType, type] = {
    EventType.REPOSITORY_CREATED: RepositoryEventPayload,
    EventType.REPOSITORY_UPDATED: RepositoryEventPayload,
    EventType.REPOSITORY_DELETED: RepositoryEventPayload,
    EventType.COMPONENT_UPLOADED: ComponentEventPayload,
    EventType.COMPONENT_DELETED: ComponentEventPayload,
    EventType.ASSET_DOWNLOADED: AssetEventPayload,
    EventType.USER_AUTHENTICATED: SecurityEventPayload,
    EventType.USER_AUTHORIZATION_FAILED: SecurityEventPayload,
    EventType.CONFIG_CHANGED: ConfigEventPayload,
    EventType.TASK_STARTED: TaskEventPayload,
    EventType.TASK_COMPLETED: TaskEventPayload,
    EventType.BLOBSTORE_COMPACTED: BlobStoreEventPayload,
    EventType.CLEANUP_COMPLETED: CleanupEventPayload,
}
"""Mapping from each :class:`EventType` to its expected payload dataclass.

Used for optional payload validation and API documentation.  Every entry in
:class:`EventType` **must** have a corresponding entry here.
"""


# ---------------------------------------------------------------------------
# Event Type → Domain Mapping
# ---------------------------------------------------------------------------

EVENT_DOMAINS: dict[EventType, str] = {
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
"""Mapping from each :class:`EventType` to its logical domain string.

Used by subscribers (e.g. ``AuditLogSubscriber``) to determine the audit
event domain.
"""


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------

def get_payload_schema(event_type: EventType) -> type | None:
    """Return the expected payload dataclass for *event_type*.

    Args:
        event_type: An :class:`EventType` member.

    Returns:
        The dataclass type associated with *event_type*, or ``None`` if no
        schema is registered.
    """
    return EVENT_PAYLOAD_SCHEMAS.get(event_type)


def validate_payload(event_type: EventType, payload: dict[str, object]) -> bool:
    """Optionally validate that *payload* contains all required fields for
    *event_type*.

    A field is considered **required** if it has no default value in its
    dataclass definition.  This helper is intentionally lenient — event
    dispatch does **not** require validation, and extra keys in *payload*
    are silently accepted.

    Args:
        event_type: An :class:`EventType` member.
        payload: Dictionary of event data to validate.

    Returns:
        ``True`` if validation passes (all required fields present) or if no
        schema is registered for the event type.  ``False`` if any required
        field is missing from *payload*.
    """
    schema_cls = EVENT_PAYLOAD_SCHEMAS.get(event_type)
    if schema_cls is None:
        # No schema registered — treat as valid (permissive by design).
        return True

    # Inspect dataclass fields to find those without defaults.
    for dc_field in dataclasses.fields(schema_cls):
        has_default = (
            dc_field.default is not dataclasses.MISSING
            or dc_field.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        )
        if not has_default and dc_field.name not in payload:
            return False

    return True


def get_event_domain(event_type: EventType | str) -> str:
    """Return the logical domain string for *event_type*.

    If *event_type* is a plain string it is first converted to an
    :class:`EventType` member.  Returns ``'system'`` for any event type not
    present in :data:`EVENT_DOMAINS`.

    Args:
        event_type: An :class:`EventType` member **or** its string value.

    Returns:
        Domain string such as ``'repository'``, ``'security'``, etc., or
        ``'system'`` as a fallback.
    """
    if isinstance(event_type, str) and not isinstance(event_type, EventType):
        try:
            event_type = EventType(event_type)
        except ValueError:
            return "system"

    return EVENT_DOMAINS.get(event_type, "system")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "EventType",
    "RepositoryEventPayload",
    "ComponentEventPayload",
    "AssetEventPayload",
    "SecurityEventPayload",
    "ConfigEventPayload",
    "TaskEventPayload",
    "BlobStoreEventPayload",
    "CleanupEventPayload",
    "EVENT_PAYLOAD_SCHEMAS",
    "EVENT_DOMAINS",
    "get_payload_schema",
    "validate_payload",
    "get_event_domain",
]
