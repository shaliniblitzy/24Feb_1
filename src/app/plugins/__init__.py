"""
Plugin Architecture Framework for the Nexus Repository application.

This package replaces the OSGi/Karaf 4.4.4 plugin framework and
CapabilityRegistryBooter.java from the Java source system.
It implements Feature F-504 (Plugin Architecture) for extensible
format support and capability management.

The plugin system supports the following extension points:

- **Format handlers**: Custom repository format protocol implementations
  (e.g., Helm, Conda, Cargo) via :class:`FormatHandlerExtension`.
- **Authentication realms**: Custom authentication backends
  (e.g., OAuth2, Kerberos) via :class:`AuthRealmExtension`.
- **Storage backends**: Custom BlobStore implementations
  (e.g., Azure Blob, GCS) via :class:`StorageBackendExtension`.
- **Scheduled tasks**: Custom background task types
  (e.g., report generation, data export) via :class:`ScheduledTaskExtension`.
- **Event subscribers**: Custom event listeners
  (e.g., audit integrations, notifications) via :class:`EventSubscriberExtension`.

Plugins are discovered via:

- Python entry points (setuptools ``entry_points`` group, default: ``'nexus.plugins'``)
- Configured plugin directories on the filesystem
- Direct programmatic registration through the :class:`ExtensionRegistry`

Usage::

    from src.app.plugins import PluginManager, ExtensionPoint

    # Get the singleton plugin manager instance
    manager = get_plugin_manager()

    # Discover and load all plugins
    manager.discover_plugins()
    manager.load_all_plugins()
    manager.activate_all_plugins()

    # Query registered format handler extensions
    from src.app.plugins import FormatHandlerExtension
    handlers = manager.extension_registry.get_extensions(FormatHandlerExtension)

    # Check if a specific plugin is active
    if manager.is_plugin_active('my-custom-plugin'):
        plugin_info = manager.get_plugin('my-custom-plugin')

Architecture Notes:

- All business logic resides in :mod:`src.app.plugins.plugin_manager` and
  :mod:`src.app.plugins.extension_points`.  This ``__init__.py`` is a
  pure re-export façade for clean import ergonomics.
- Import order matters: ``extension_points`` has zero intra-package
  dependencies and is imported first; ``plugin_manager`` depends on
  ``extension_points`` and is imported second.  This prevents circular
  import issues.
- The :func:`get_plugin_manager` factory uses thread-safe double-checked
  locking to guarantee a single :class:`PluginManager` instance per process.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Re-exports from extension_points (loaded first — no intra-package deps)
# ---------------------------------------------------------------------------
# These classes define the contracts that plugin authors implement.
# Importing extension_points before plugin_manager avoids any potential
# circular import issues since plugin_manager depends on extension_points.
# ---------------------------------------------------------------------------

from src.app.plugins.extension_points import (
    AuthRealmExtension,
    EventSubscriberExtension,
    ExtensionPoint,
    ExtensionRegistry,
    FormatHandlerExtension,
    ScheduledTaskExtension,
    StorageBackendExtension,
)

# ---------------------------------------------------------------------------
# Re-exports from plugin_manager (loaded second — depends on extension_points)
# ---------------------------------------------------------------------------
# These provide the core plugin lifecycle management API.
# ---------------------------------------------------------------------------

from src.app.plugins.plugin_manager import (
    PluginInfo,
    PluginManager,
    PluginState,
    get_plugin_manager,
)

# ---------------------------------------------------------------------------
# Public API — explicit list of all names exported from this package
# ---------------------------------------------------------------------------
# This __all__ enables ``from src.app.plugins import *`` (though explicit
# imports are preferred) and serves as the canonical list of the package's
# public interface.
# ---------------------------------------------------------------------------

__all__: list[str] = [
    # Plugin management (from plugin_manager)
    "PluginManager",
    "PluginInfo",
    "PluginState",
    "get_plugin_manager",
    # Extension point base class and concrete types (from extension_points)
    "ExtensionPoint",
    "FormatHandlerExtension",
    "AuthRealmExtension",
    "StorageBackendExtension",
    "ScheduledTaskExtension",
    "EventSubscriberExtension",
    # Extension registry (from extension_points)
    "ExtensionRegistry",
]
