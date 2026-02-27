"""
Extension Point Definitions and Registry.

Replaces the OSGi service registry and service interface patterns from the
Java source system (Sonatype Nexus Repository) with Python ABC-based
extension points.  Implements Feature F-504 (Plugin Architecture) by
providing a typed, discoverable contract for plugin authors.

Each extension point is an abstract base class (:class:`abc.ABC`) that defines
the contract a plugin must implement.  The :class:`ExtensionRegistry` tracks
all registered extension instances by their extension point type.

**Architecture Context:**

This module is the **most foundational** file in the ``src.app.plugins``
package — both ``plugin_manager.py`` and ``__init__.py`` depend on it.  It
has **NO** dependencies on other ``src.app.plugins`` modules.  The only
internal import is a **lazy** import of ``subscribe``/``unsubscribe`` from
``src.app.events.event_bus`` inside :class:`EventSubscriberExtension`
``activate()``/``deactivate()`` method bodies.

**Extension Point Categories:**

1. :class:`FormatHandlerExtension` — custom repository format handlers
2. :class:`AuthRealmExtension` — custom authentication backends
3. :class:`StorageBackendExtension` — custom BlobStore implementations
4. :class:`ScheduledTaskExtension` — custom background task types
5. :class:`EventSubscriberExtension` — custom event listeners

**Usage Example:**

.. code-block:: python

    from src.app.plugins.extension_points import (
        FormatHandlerExtension,
        ExtensionRegistry,
    )

    class HelmFormatHandler(FormatHandlerExtension):
        @property
        def name(self) -> str:
            return 'helm'
        ...

    registry = ExtensionRegistry()
    registry.register(FormatHandlerExtension, HelmFormatHandler())
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Type, TypeVar

# ---------------------------------------------------------------------------
# Module-Level TypeVar for Generic Extension Typing
# ---------------------------------------------------------------------------
# Enables type-safe registry operations: ``get_extensions(FormatHandlerExtension)``
# returns ``List[FormatHandlerExtension]`` rather than ``List[ExtensionPoint]``.
# ---------------------------------------------------------------------------

T = TypeVar('T', bound='ExtensionPoint')

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source system.  Provides INFO-level registration messages, WARNING-level
# destructive operation alerts, and DEBUG-level tracing.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)


# ============================================================================
# ExtensionPoint — Root Abstract Base Class
# ============================================================================

class ExtensionPoint(ABC):
    """Base abstract class for all plugin extension points.

    Replaces the OSGi service interface pattern from the Java source system.
    All extension points inherit from this class, providing a common type
    for the plugin registry to track and manage.

    Plugin authors implement concrete subclasses of specific extension points
    (e.g., :class:`FormatHandlerExtension`, :class:`AuthRealmExtension`) to
    extend the system.

    Lifecycle:
        - :meth:`activate` is called when the plugin is activated
        - :meth:`deactivate` is called when the plugin is deactivated
        - :attr:`name` returns the unique identifier for this extension
        - :attr:`description` returns a human-readable description
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the unique identifier for this extension point implementation.

        Must be unique within the same extension point type.

        Examples:
            ``'maven'``, ``'npm'``, ``'custom-auth-realm'``
        """

    @property
    @abstractmethod
    def description(self) -> str:
        """Return a human-readable description of this extension.

        Used for admin UI display and diagnostic logging.
        """

    def activate(self) -> None:
        """Called when the plugin providing this extension is activated.

        Override for custom activation logic such as registering routes,
        initialising state, or acquiring resources.  The default
        implementation is a no-op.
        """

    def deactivate(self) -> None:
        """Called when the plugin providing this extension is deactivated.

        Override for cleanup logic such as unregistering routes, releasing
        resources, or flushing buffers.  The default implementation is a
        no-op.
        """

    def get_priority(self) -> int:
        """Return the priority of this extension.

        Higher values mean the extension is processed first when multiple
        extensions of the same type exist.  The default priority is ``0``.

        Returns:
            An integer priority value.  Higher = earlier in processing order.
        """
        return 0


# ============================================================================
# FormatHandlerExtension — Custom Repository Format Handlers
# ============================================================================

class FormatHandlerExtension(ExtensionPoint):
    """Extension point for custom repository format handlers.

    Replaces the OSGi format plugin bundle contract from the Java source.
    Plugins implement this to add support for new repository formats beyond
    the built-in formats (Maven, npm, Docker, NuGet, PyPI, APT, Raw).

    Each format handler defines:

    - Format name and content types
    - Upload validation and processing
    - Download/fetch behaviour
    - Metadata extraction (coordinates, checksums)
    - Protocol-specific route handlers (as Flask Blueprints)

    Usage::

        class HelmFormatHandler(FormatHandlerExtension):
            @property
            def name(self) -> str:
                return 'helm'

            @property
            def format_name(self) -> str:
                return 'helm'

            @property
            def description(self) -> str:
                return 'Helm chart repository format handler'

            @property
            def content_types(self) -> List[str]:
                return ['application/gzip', 'application/x-tar']

            def validate_upload(self, file_data, filename, metadata):
                ...

            def extract_metadata(self, file_data, filename):
                ...

            def get_routes(self):
                # Return Flask Blueprint with format-specific routes
                ...
    """

    @property
    @abstractmethod
    def format_name(self) -> str:
        """Return the format identifier (e.g. ``'helm'``, ``'conda'``, ``'cargo'``).

        Used for repository configuration and routing.
        """

    @property
    @abstractmethod
    def content_types(self) -> List[str]:
        """Return list of MIME content types this format handles.

        Examples:
            ``['application/gzip', 'application/x-tar']``
        """

    @abstractmethod
    def validate_upload(
        self,
        file_data: bytes,
        filename: str,
        metadata: Dict[str, Any],
    ) -> bool:
        """Validate an artifact upload for this format.

        Args:
            file_data: Raw bytes of the uploaded file.
            filename: Original filename of the upload.
            metadata: Additional metadata supplied with the upload.

        Returns:
            ``True`` if the upload is valid.

        Raises:
            ValueError: If the upload fails validation, with a descriptive
                message indicating the reason.
        """

    @abstractmethod
    def extract_metadata(
        self,
        file_data: bytes,
        filename: str,
    ) -> Dict[str, Any]:
        """Extract format-specific metadata from an uploaded artifact.

        Args:
            file_data: Raw bytes of the artifact.
            filename: Original filename.

        Returns:
            A dictionary containing extracted metadata.  Expected keys
            include ``'namespace'``, ``'name'``, ``'version'``, and
            ``'attributes'``.
        """

    def get_routes(self) -> Optional[Any]:
        """Return a Flask Blueprint with format-specific protocol routes.

        Override to register format-specific protocol endpoints such as
        Docker Registry V2 API or NuGet V3 service index.

        Returns:
            A :class:`flask.Blueprint` instance, or ``None`` if this format
            uses only the generic REST API (the default).
        """
        return None

    def get_supported_repository_types(self) -> List[str]:
        """Return repository types this format supports.

        Returns:
            A list of supported repository type strings.  The default
            implementation returns all three types:
            ``['hosted', 'proxy', 'group']``.
        """
        return ['hosted', 'proxy', 'group']


# ============================================================================
# AuthRealmExtension — Custom Authentication Realms
# ============================================================================

class AuthRealmExtension(ExtensionPoint):
    """Extension point for custom authentication realms.

    Replaces the Apache Shiro custom realm interface from the Java source.
    Plugins implement this to add custom authentication backends beyond
    the built-in realms (local, bearer_token, JWT, LDAP, SSO).

    Each auth realm defines:

    - Authentication method and supported credentials
    - Credential validation logic
    - User principal extraction
    - Realm ordering priority
    """

    @property
    @abstractmethod
    def realm_name(self) -> str:
        """Return the realm identifier (e.g. ``'custom-oauth2'``, ``'kerberos'``)."""

    @abstractmethod
    def authenticate(
        self,
        credentials: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Attempt to authenticate with the provided credentials.

        Args:
            credentials: Dictionary containing credential material.  The
                structure depends on the credential type (e.g. ``'username'``
                and ``'password'`` for basic auth).

        Returns:
            A user principal dictionary if authentication succeeds.  The
            dict should contain:

            - ``'user_id'`` — unique user identifier
            - ``'username'`` — human-readable username
            - ``'email'`` — user email address
            - ``'roles'`` — list of role identifiers
            - ``'realm'`` — name of the authenticating realm

            Returns ``None`` if this realm cannot handle the credentials
            (i.e. wrong credential type).

        Raises:
            Exception: If credentials are recognised but invalid for this
                realm (e.g. wrong password).
        """

    @abstractmethod
    def supports(self, credential_type: str) -> bool:
        """Return whether this realm can handle the given credential type.

        Args:
            credential_type: One of ``'basic'``, ``'bearer'``, ``'api_key'``,
                ``'certificate'``, or ``'custom'``.

        Returns:
            ``True`` if this realm supports the credential type.
        """

    def get_priority(self) -> int:
        """Return the realm ordering priority.

        Custom realms default to ``100`` so that they are checked **after**
        the built-in realms.  Lower values mean earlier in the
        authentication chain.

        Returns:
            An integer priority (default ``100``).
        """
        return 100


# ============================================================================
# StorageBackendExtension — Custom BlobStore Storage Backends
# ============================================================================

class StorageBackendExtension(ExtensionPoint):
    """Extension point for custom BlobStore storage backends.

    Replaces the BlobStore SPI from the Java source.
    Plugins implement this to add custom storage backends beyond
    the built-in File and S3 BlobStore implementations.

    Each storage backend defines:

    - Backend type identifier
    - Blob CRUD operations (store, get, delete, exists)
    - Configuration schema
    - Maintenance operations
    """

    @property
    @abstractmethod
    def backend_type(self) -> str:
        """Return the backend type identifier.

        Examples:
            ``'azure-blob'``, ``'gcs'``, ``'nfs'``
        """

    @abstractmethod
    def store_blob(
        self,
        blob_id: str,
        data: bytes,
        headers: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Store a blob with the given ID and data.

        Args:
            blob_id: Unique identifier for the blob.
            data: Raw bytes to store.
            headers: Optional HTTP-style headers (e.g. ``Content-Type``)
                to associate with the blob as metadata.

        Returns:
            ``True`` on successful storage.
        """

    @abstractmethod
    def get_blob(self, blob_id: str) -> Optional[bytes]:
        """Retrieve blob data by ID.

        Args:
            blob_id: Unique identifier for the blob.

        Returns:
            The raw bytes if the blob is found, ``None`` otherwise.
        """

    @abstractmethod
    def delete_blob(self, blob_id: str, soft: bool = True) -> bool:
        """Delete a blob.

        Soft-delete by default, matching the built-in BlobStore pattern
        where blobs are flagged as deleted but not immediately purged.

        Args:
            blob_id: Unique identifier for the blob.
            soft: If ``True`` (default), perform a soft-delete.  If
                ``False``, permanently remove the blob data.

        Returns:
            ``True`` if the blob existed and was deleted.
        """

    @abstractmethod
    def blob_exists(self, blob_id: str) -> bool:
        """Check if a blob exists.

        Args:
            blob_id: Unique identifier for the blob.

        Returns:
            ``True`` if the blob exists in the store.
        """

    def get_configuration_schema(self) -> Dict[str, Any]:
        """Return a JSON Schema describing accepted configuration.

        Override to provide a schema that describes what configuration
        keys and value types this backend accepts.

        Returns:
            A dictionary conforming to JSON Schema, or an empty dict
            (the default) if no special configuration is required.
        """
        return {}


# ============================================================================
# ScheduledTaskExtension — Custom Scheduled Task Types
# ============================================================================

class ScheduledTaskExtension(ExtensionPoint):
    """Extension point for custom scheduled task types.

    Replaces the Quartz Job interface from the Java source.
    Plugins implement this to add custom background task types beyond
    the built-in tasks (cleanup, compaction, integrity check).

    Each scheduled task defines:

    - Task type identifier
    - Execution logic
    - Configuration schema
    - Default schedule
    """

    @property
    @abstractmethod
    def task_type(self) -> str:
        """Return the task type identifier.

        Examples:
            ``'custom-report-generator'``, ``'data-export'``
        """

    @abstractmethod
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the task with the given context configuration.

        Args:
            context: Dictionary containing task configuration and runtime
                parameters (e.g. repository names, date ranges, limits).

        Returns:
            A result dictionary with at least:

            - ``'status'`` — ``'ok'`` on success, ``'failed'`` on failure
            - ``'message'`` — human-readable summary
            - ``'details'`` — (optional) additional result data
        """

    def get_default_schedule(self) -> Optional[str]:
        """Return a cron expression for the default schedule.

        Returns ``None`` for tasks that are manual-only (no automatic
        scheduling).

        Examples:
            ``'0 2 * * *'`` — daily at 2 AM

        Returns:
            A cron expression string, or ``None`` (the default).
        """
        return None

    def get_configuration_schema(self) -> Dict[str, Any]:
        """Return a JSON Schema for task configuration options.

        Override to describe the configuration keys and value types
        this task accepts.

        Returns:
            A dictionary conforming to JSON Schema, or an empty dict
            (the default) if no configuration is required.
        """
        return {}


# ============================================================================
# EventSubscriberExtension — Custom Event Listeners
# ============================================================================

class EventSubscriberExtension(ExtensionPoint):
    """Extension point for custom event subscribers.

    Replaces the Guava EventBus ``@Subscribe`` annotation from the Java
    source.  Plugins implement this to add custom event listeners that react
    to system events (repository changes, security events, task completions,
    etc.).

    Each event subscriber defines:

    - The event types it listens to
    - Handler logic for each event type
    - Registration with the event bus

    The default :meth:`activate` implementation automatically registers
    this extension's :meth:`_event_handler_wrapper` with the event bus for
    all subscribed event types.  The default :meth:`deactivate` removes
    the registrations.
    """

    @abstractmethod
    def get_subscribed_events(self) -> List[str]:
        """Return the event type strings this subscriber listens to.

        Examples:
            ``['repository.created', 'component.uploaded']``

        Returns:
            A list of event type identifier strings.
        """

    @abstractmethod
    def handle_event(
        self,
        sender: Any,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        """Handle an incoming event.

        Called by the event bus when a subscribed event is dispatched.
        Implementations should be **fault-tolerant**: exceptions should be
        caught and logged internally, not propagated.

        Args:
            sender: The object that emitted the event (may be an
                anonymous sentinel if no sender was specified).
            event_type: String identifier of the event type.
            payload: Dictionary containing event-specific data.
        """

    def activate(self) -> None:
        """Register this extension with the event bus.

        Lazily imports :func:`src.app.events.event_bus.subscribe` to avoid
        circular imports at module load time.  Registers
        :meth:`_event_handler_wrapper` for every event type returned by
        :meth:`get_subscribed_events`.
        """
        from src.app.events.event_bus import subscribe  # lazy import

        subscribed_events = self.get_subscribed_events()
        for event_type in subscribed_events:
            subscribe(event_type, self._event_handler_wrapper)
        logger.debug(
            "EventSubscriberExtension '%s' activated — subscribed to %d event(s): %s",
            self.name,
            len(subscribed_events),
            subscribed_events,
        )

    def deactivate(self) -> None:
        """Unregister this extension from the event bus.

        Lazily imports :func:`src.app.events.event_bus.unsubscribe` and
        removes the handler wrapper from all subscribed event types.
        """
        from src.app.events.event_bus import unsubscribe  # lazy import

        subscribed_events = self.get_subscribed_events()
        for event_type in subscribed_events:
            unsubscribe(event_type, self._event_handler_wrapper)
        logger.debug(
            "EventSubscriberExtension '%s' deactivated — unsubscribed from %d event(s).",
            self.name,
            len(subscribed_events),
        )

    def _event_handler_wrapper(self, sender: Any, **kwargs: Any) -> None:
        """Adapt Blinker's signal signature to the plugin's handler API.

        Blinker dispatches signals as ``(sender, **kwargs)`` where kwargs
        contains ``'event_type'`` and ``'payload'``.  This wrapper extracts
        those values and calls :meth:`handle_event` with the expected
        positional signature.

        Fault-tolerant: any exception raised by :meth:`handle_event` is
        caught and logged at ERROR level to prevent subscriber failures
        from breaking event dispatch.

        Args:
            sender: The object that emitted the event.
            **kwargs: Keyword arguments from Blinker signal dispatch.
                Expected keys: ``'event_type'``, ``'payload'``.
        """
        event_type: str = str(kwargs.get('event_type', ''))
        payload: Dict[str, Any] = kwargs.get('payload', {})
        try:
            self.handle_event(sender, event_type, payload)
        except Exception as exc:
            logger.error(
                "EventSubscriberExtension '%s' failed handling event '%s': %s",
                self.name,
                event_type,
                str(exc),
                exc_info=True,
            )


# ============================================================================
# ExtensionRegistry — Central Extension Registration and Lookup
# ============================================================================

class ExtensionRegistry:
    """Registry for tracking all registered plugin extensions.

    Replaces the OSGi Service Registry from the Java source system.
    Maintains a mapping from extension point types to registered instances.
    Thread-safe for concurrent access using a :class:`threading.RLock`.

    Usage::

        registry = ExtensionRegistry()
        registry.register(FormatHandlerExtension, maven_handler)
        handlers = registry.get_extensions(FormatHandlerExtension)
    """

    def __init__(self) -> None:
        """Initialise the extension registry with empty mappings."""
        self._extensions: Dict[Type[ExtensionPoint], List[ExtensionPoint]] = {}
        self._lock: threading.RLock = threading.RLock()
        self.logger: logging.Logger = logging.getLogger(
            f'{__name__}.ExtensionRegistry'
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, extension_type: Type[T], extension: T) -> None:
        """Register an extension instance under a specific extension point type.

        Thread-safe.  Validates that *extension* is an instance of
        *extension_type*.  After registration the internal list is re-sorted
        by priority (descending — higher priority first).

        Args:
            extension_type: The extension point class (e.g.
                :class:`FormatHandlerExtension`).
            extension: An instance of that class.

        Raises:
            TypeError: If *extension* is not an instance of *extension_type*.
        """
        if not isinstance(extension, extension_type):
            raise TypeError(
                f"Extension must be an instance of {extension_type.__name__}, "
                f"got {type(extension).__name__}"
            )

        with self._lock:
            if extension_type not in self._extensions:
                self._extensions[extension_type] = []

            self._extensions[extension_type].append(extension)

            # Sort by priority descending — higher priority first
            self._extensions[extension_type].sort(
                key=lambda ext: ext.get_priority(),
                reverse=True,
            )

        self.logger.info(
            "Registered '%s' for extension point %s (priority=%d)",
            extension.name,
            extension_type.__name__,
            extension.get_priority(),
        )

    # ------------------------------------------------------------------
    # Unregistration
    # ------------------------------------------------------------------

    def unregister(self, extension_type: Type[T], extension: T) -> bool:
        """Remove an extension instance from the registry.

        Thread-safe.

        Args:
            extension_type: The extension point class.
            extension: The instance to remove.

        Returns:
            ``True`` if the extension was found and removed, ``False``
            otherwise.
        """
        with self._lock:
            extensions = self._extensions.get(extension_type)
            if extensions is None:
                return False

            try:
                extensions.remove(extension)
            except ValueError:
                return False

            # Clean up empty lists
            if not extensions:
                del self._extensions[extension_type]

        self.logger.info(
            "Unregistered '%s' from extension point %s",
            extension.name,
            extension_type.__name__,
        )
        return True

    def unregister_all(
        self,
        extension_type: Optional[Type[ExtensionPoint]] = None,
    ) -> int:
        """Remove all extensions, optionally filtered by type.

        This is a **destructive** operation.

        Args:
            extension_type: If provided, remove only extensions of this
                type.  If ``None``, remove **all** extensions across every
                type.

        Returns:
            The number of extension instances that were removed.
        """
        with self._lock:
            if extension_type is not None:
                extensions = self._extensions.pop(extension_type, [])
                removed = len(extensions)
                self.logger.warning(
                    "Unregistered all %d extension(s) for %s",
                    removed,
                    extension_type.__name__,
                )
            else:
                removed = sum(len(exts) for exts in self._extensions.values())
                self._extensions.clear()
                self.logger.warning(
                    "Unregistered ALL %d extension(s) across all types",
                    removed,
                )
        return removed

    # ------------------------------------------------------------------
    # Query / Lookup
    # ------------------------------------------------------------------

    def get_extensions(self, extension_type: Type[T]) -> List[T]:
        """Return all registered extensions of the given type.

        Thread-safe read.  Returns a **defensive copy** of the internal
        list so that callers cannot mutate the registry.

        Args:
            extension_type: The extension point class to query.

        Returns:
            A list of extension instances (possibly empty).
        """
        with self._lock:
            return list(self._extensions.get(extension_type, []))

    def get_extension_by_name(
        self,
        extension_type: Type[T],
        name: str,
    ) -> Optional[T]:
        """Find a specific extension by name within a given type.

        Args:
            extension_type: The extension point class to search within.
            name: The unique name of the extension.

        Returns:
            The matching extension instance, or ``None`` if not found.
        """
        with self._lock:
            for ext in self._extensions.get(extension_type, []):
                if ext.name == name:
                    return ext  # type: ignore[return-value]
        return None

    def has_extensions(self, extension_type: Type[ExtensionPoint]) -> bool:
        """Check whether any extensions are registered for the given type.

        Args:
            extension_type: The extension point class to check.

        Returns:
            ``True`` if at least one extension is registered.
        """
        with self._lock:
            return bool(self._extensions.get(extension_type))

    def get_extension_types(self) -> List[Type[ExtensionPoint]]:
        """Return all extension point types that have registered extensions.

        Returns:
            A list of extension point classes.
        """
        with self._lock:
            return list(self._extensions.keys())

    def get_extension_count(
        self,
        extension_type: Optional[Type[ExtensionPoint]] = None,
    ) -> int:
        """Return the number of registered extensions.

        Args:
            extension_type: If provided, count only extensions of this type.
                If ``None``, return the total count across all types.

        Returns:
            Non-negative integer count.
        """
        with self._lock:
            if extension_type is not None:
                return len(self._extensions.get(extension_type, []))
            return sum(len(exts) for exts in self._extensions.values())


# ============================================================================
# Public API
# ============================================================================

__all__: List[str] = [
    'ExtensionPoint',
    'FormatHandlerExtension',
    'AuthRealmExtension',
    'StorageBackendExtension',
    'ScheduledTaskExtension',
    'EventSubscriberExtension',
    'ExtensionRegistry',
]
