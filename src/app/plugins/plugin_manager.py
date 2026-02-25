"""
Plugin Discovery, Loading, Registration, and Lifecycle Management.

Replaces ``CapabilityRegistryBooter.java`` capability activation and
OSGi/Karaf 4.4.4 bundle lifecycle management from the Java source system.
Implements Feature F-504 (Plugin Architecture) — the heart of the extensible
plugin framework.

**Responsibilities:**

1. **Discovery** — finding plugins via Python entry points
   (``setuptools`` ``entry_points`` group) or configured plugin directories.
2. **Loading** — importing plugin modules and validating they implement
   required extension point interfaces.
3. **Registration** — registering plugin-provided extensions with the
   :class:`~src.app.plugins.extension_points.ExtensionRegistry`.
4. **Lifecycle** — managing plugin state transitions:
   ``DISCOVERED -> LOADED -> ACTIVATED -> DEACTIVATED -> FAILED``.

This replaces the OSGi bundle lifecycle model
(INSTALLED -> RESOLVED -> STARTING -> ACTIVE -> STOPPING -> UNINSTALLED)
with a simplified Python-native state machine.

**Thread Safety:**

All mutable state mutations are protected by a :class:`threading.RLock`.
The module-level :func:`get_plugin_manager` singleton factory uses
double-checked locking via :class:`threading.Lock`.

**Event Dispatch:**

Plugin lifecycle events (``plugin.activated``, ``plugin.deactivated``) are
emitted via the Blinker-based event bus.  The ``emit_event`` function is
imported **lazily** (inside method bodies) to avoid circular imports at
module load time — ``event_bus.py`` depends on ``extension_points.py``
which must load before this module.

**Usage Example:**

.. code-block:: python

    from src.app.plugins.plugin_manager import (
        PluginManager, PluginState, get_plugin_manager, init_plugins,
    )

    manager = get_plugin_manager()
    manager.add_plugin_directory('/opt/nexus/plugins')
    manager.discover_plugins()
    manager.load_all_plugins()
    manager.activate_all_plugins()
"""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Type

from flask import current_app

from src.app.plugins.extension_points import ExtensionPoint, ExtensionRegistry

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source system.  Provides DEBUG-level lifecycle tracing, INFO-level
# discovery/loading/activation summaries, WARNING-level for disabled
# plugins, and ERROR-level fault reporting.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)


# ============================================================================
# PluginState — Plugin Lifecycle State Enum
# ============================================================================

class PluginState(str, Enum):
    """Plugin lifecycle states.

    Replaces the OSGi bundle states::

        INSTALLED -> RESOLVED -> STARTING -> ACTIVE -> STOPPING -> UNINSTALLED

    with a simplified Python-native state model.  Inheriting from
    :class:`str` enables direct serialisation and human-readable logging.
    """

    DISCOVERED = 'discovered'
    """Plugin found but not yet loaded."""

    LOADED = 'loaded'
    """Plugin module imported successfully."""

    ACTIVATED = 'activated'
    """Plugin extensions registered and active."""

    DEACTIVATED = 'deactivated'
    """Plugin deactivated — extensions unregistered."""

    FAILED = 'failed'
    """Plugin failed during loading or activation."""


# ============================================================================
# PluginInfo — Plugin Metadata Dataclass
# ============================================================================

@dataclass
class PluginInfo:
    """Metadata about a discovered plugin.

    Replaces OSGi bundle metadata (``Bundle-SymbolicName``,
    ``Bundle-Version``, ``Bundle-Description``, etc.) with Python package
    metadata.

    Attributes:
        name: Unique plugin identifier (e.g. ``'nexus-format-maven'``).
        version: Plugin version string.
        description: Human-readable description.
        author: Plugin author.
        module_path: Python module path
            (e.g. ``'nexus_format_maven.plugin'``).
        entry_point: Entry point ``group:name`` if discovered via
            ``setuptools`` entry points.
        plugin_dir: Directory path if discovered via directory scanning.
        state: Current lifecycle state.
        error_message: Error details if state is FAILED.
        dependencies: Required plugin dependencies (names).
        provides: Extension point type names this plugin provides.
        module: Reference to the loaded Python module object.
        instance: Reference to the plugin instance (if applicable).
    """

    name: str
    version: str = '0.0.0'
    description: str = ''
    author: str = ''
    module_path: str = ''
    entry_point: Optional[str] = None
    plugin_dir: Optional[str] = None
    state: PluginState = PluginState.DISCOVERED
    error_message: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
    module: Any = None
    instance: Any = None


# ============================================================================
# PluginManager — Central Plugin Management System
# ============================================================================

class PluginManager:
    """Central plugin management system.

    Replaces ``CapabilityRegistryBooter.java`` and OSGi/Karaf 4.4.4 bundle
    lifecycle management.  Responsible for plugin discovery, loading,
    registration, and lifecycle management.

    Supports two plugin discovery mechanisms:

    1. Python entry points (``setuptools`` ``entry_points`` group:
       ``'nexus.plugins'``).
    2. Configured plugin directories (scanned for Python modules).

    Thread-safe for concurrent plugin operations — all mutable state
    mutations are protected by a :class:`threading.RLock`.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the PluginManager with empty registries."""
        # Registry of all discovered plugins, keyed by unique name
        self._plugins: Dict[str, PluginInfo] = {}

        # Central extension point registry (shared across all plugins)
        self._extension_registry: ExtensionRegistry = ExtensionRegistry()

        # Directories to scan for plugin Python modules / packages
        self._plugin_dirs: List[Path] = []

        # setuptools entry point group name for plugin discovery
        self._entry_point_group: str = 'nexus.plugins'

        # Reentrant lock for thread-safe state mutations
        self._lock: threading.RLock = threading.RLock()

        # Flag tracking whether initial discovery has been performed
        self._initialized: bool = False

        # Per-class logger for structured output
        self.logger: logging.Logger = logging.getLogger(
            f'{__name__}.PluginManager'
        )

        self.logger.debug('PluginManager initialized')

    # ------------------------------------------------------------------
    # Plugin Discovery
    # ------------------------------------------------------------------

    def discover_plugins(self) -> List[PluginInfo]:
        """Discover all available plugins from configured sources.

        Scans both Python entry points and configured plugin directories.
        Newly discovered plugins are added with state
        :attr:`PluginState.DISCOVERED`.

        Returns:
            A list of all :class:`PluginInfo` objects (newly discovered
            and previously known).
        """
        with self._lock:
            ep_plugins = self._discover_from_entry_points()
            dir_plugins = self._discover_from_directories()

            new_count = len(ep_plugins) + len(dir_plugins)
            total_count = len(self._plugins)

            self._initialized = True

            self.logger.info(
                'Plugin discovery complete: %d new plugin(s) found, '
                '%d total plugin(s) registered',
                new_count,
                total_count,
            )

            return list(self._plugins.values())

    def _discover_from_entry_points(self) -> List[PluginInfo]:
        """Discover plugins registered via Python setuptools entry points.

        Uses ``importlib.metadata.entry_points(group=...)`` to find
        plugins registered under the configured entry point group
        (default: ``'nexus.plugins'``).

        Returns:
            A list of newly discovered :class:`PluginInfo` objects.
        """
        discovered: List[PluginInfo] = []

        try:
            eps = importlib.metadata.entry_points(
                group=self._entry_point_group,
            )
        except Exception as exc:
            self.logger.error(
                'Failed to query entry points for group %s: %s',
                self._entry_point_group,
                str(exc),
            )
            return discovered

        for ep in eps:
            try:
                # Skip if a plugin with this name is already registered
                if ep.name in self._plugins:
                    self.logger.debug(
                        'Entry point plugin %s already registered — skipping',
                        ep.name,
                    )
                    continue

                plugin_info = PluginInfo(
                    name=ep.name,
                    module_path=ep.value,
                    entry_point=f'{self._entry_point_group}:{ep.name}',
                    state=PluginState.DISCOVERED,
                )

                # Attempt to read version / description from the
                # distribution that provides this entry point.
                try:
                    dist = ep.dist
                    if dist is not None:
                        plugin_info.version = dist.metadata.get(
                            'Version', '0.0.0'
                        )
                        plugin_info.description = dist.metadata.get(
                            'Summary', ''
                        )
                        plugin_info.author = dist.metadata.get(
                            'Author', ''
                        ) or dist.metadata.get('Author-email', '')
                except Exception:
                    # Non-critical — keep defaults
                    pass

                self._plugins[plugin_info.name] = plugin_info
                discovered.append(plugin_info)

                self.logger.debug(
                    'Discovered entry-point plugin: %s (%s)',
                    plugin_info.name,
                    plugin_info.version,
                )

            except Exception as exc:
                self.logger.error(
                    'Error processing entry point %s: %s',
                    getattr(ep, 'name', '<unknown>'),
                    str(exc),
                )

        return discovered

    def _discover_from_directories(self) -> List[PluginInfo]:
        """Scan configured plugin directories for Python modules / packages.

        For each directory in :attr:`_plugin_dirs`:

        - Verifies the directory exists and is readable.
        - Scans for ``.py`` files (standalone modules) and sub-directories
          containing ``__init__.py`` (Python packages).
        - Creates a :class:`PluginInfo` entry for each discovered module.

        Returns:
            A list of newly discovered :class:`PluginInfo` objects.
        """
        discovered: List[PluginInfo] = []

        for plugin_dir in self._plugin_dirs:
            if not plugin_dir.exists():
                self.logger.warning(
                    'Plugin directory does not exist: %s', plugin_dir,
                )
                continue

            if not plugin_dir.is_dir():
                self.logger.warning(
                    'Plugin path is not a directory: %s', plugin_dir,
                )
                continue

            self.logger.debug(
                'Scanning plugin directory: %s', plugin_dir,
            )

            try:
                for item in sorted(plugin_dir.iterdir()):
                    try:
                        plugin_name: Optional[str] = None
                        module_path_str: str = ''

                        if item.is_file() and item.suffix == '.py':
                            # Standalone Python module — exclude dunder files
                            if item.stem.startswith('__'):
                                continue
                            plugin_name = item.stem
                            module_path_str = item.stem

                        elif item.is_dir():
                            # Python package — must contain __init__.py
                            init_file = item / '__init__.py'
                            if not init_file.exists():
                                continue
                            plugin_name = item.name
                            module_path_str = item.name

                        if plugin_name is None:
                            continue

                        # Skip if already registered under this name
                        if plugin_name in self._plugins:
                            self.logger.debug(
                                'Directory plugin %s already registered — '
                                'skipping',
                                plugin_name,
                            )
                            continue

                        plugin_info = PluginInfo(
                            name=plugin_name,
                            module_path=module_path_str,
                            plugin_dir=str(plugin_dir),
                            state=PluginState.DISCOVERED,
                        )

                        self._plugins[plugin_info.name] = plugin_info
                        discovered.append(plugin_info)

                        self.logger.debug(
                            'Discovered directory plugin: %s in %s',
                            plugin_name,
                            plugin_dir,
                        )

                    except Exception as exc:
                        self.logger.error(
                            'Error scanning item %s in %s: %s',
                            item,
                            plugin_dir,
                            str(exc),
                        )

            except PermissionError as exc:
                self.logger.error(
                    'Permission denied scanning plugin directory %s: %s',
                    plugin_dir,
                    str(exc),
                )

        return discovered

    # ------------------------------------------------------------------
    # Plugin Loading
    # ------------------------------------------------------------------

    def load_plugin(self, plugin_name: str) -> bool:
        """Load (import) a discovered plugin module.

        Imports the plugin module into the Python runtime.  Three
        loading strategies are attempted in order:

        1. Entry-point based loading (``ep.load()``).
        2. Directory-based loading (add ``plugin_dir`` to ``sys.path``).
        3. Direct ``importlib.import_module()`` via ``module_path``.

        Args:
            plugin_name: The unique name of the plugin to load.

        Returns:
            ``True`` if the plugin was loaded successfully, ``False``
            otherwise.
        """
        with self._lock:
            plugin_info = self._plugins.get(plugin_name)

            if plugin_info is None:
                self.logger.warning(
                    'Cannot load unknown plugin: %s', plugin_name,
                )
                return False

            if plugin_info.state != PluginState.DISCOVERED:
                self.logger.warning(
                    'Cannot load plugin %s — current state is %s '
                    '(expected DISCOVERED)',
                    plugin_name,
                    plugin_info.state.value,
                )
                return False

            try:
                loaded_module: Any = None

                # Strategy 1: Entry-point based plugin
                if plugin_info.entry_point is not None:
                    loaded_module = self._load_via_entry_point(plugin_info)

                # Strategy 2: Plugin discovered from a directory
                elif plugin_info.plugin_dir is not None:
                    loaded_module = self._load_via_directory(plugin_info)

                # Strategy 3: Explicit module_path
                elif plugin_info.module_path:
                    loaded_module = importlib.import_module(
                        plugin_info.module_path,
                    )

                else:
                    raise ImportError(
                        f'Plugin {plugin_name} has no loadable source '
                        f'(no entry_point, plugin_dir, or module_path)'
                    )

                plugin_info.module = loaded_module
                plugin_info.state = PluginState.LOADED

                self.logger.info(
                    'Plugin loaded: %s (v%s)',
                    plugin_name,
                    plugin_info.version,
                )
                return True

            except Exception as exc:
                plugin_info.state = PluginState.FAILED
                plugin_info.error_message = str(exc)
                self.logger.error(
                    'Failed to load plugin %s: %s',
                    plugin_name,
                    str(exc),
                    exc_info=True,
                )
                return False

    def _load_via_entry_point(self, plugin_info: PluginInfo) -> Any:
        """Load a plugin module using its setuptools entry point.

        Args:
            plugin_info: Plugin metadata with a valid ``entry_point``.

        Returns:
            The loaded module or callable object from the entry point.

        Raises:
            ImportError: If the entry point cannot be found.
        """
        eps = importlib.metadata.entry_points(
            group=self._entry_point_group,
        )
        for ep in eps:
            if ep.name == plugin_info.name:
                loaded = ep.load()
                # entry_point.load() returns the attr (class/function).
                # We also want the module reference for introspection.
                if inspect.ismodule(loaded):
                    return loaded
                # If it loaded a class/function, get the parent module.
                module = inspect.getmodule(loaded)
                if module is not None:
                    plugin_info.instance = loaded
                    return module
                return loaded

        raise ImportError(
            f"Entry point '{plugin_info.entry_point}' not found in "
            f"group '{self._entry_point_group}'"
        )

    def _load_via_directory(self, plugin_info: PluginInfo) -> Any:
        """Load a plugin module from a directory by adjusting sys.path.

        Temporarily adds the plugin directory to ``sys.path`` so that
        Python's import machinery can resolve the module.

        Args:
            plugin_info: Plugin metadata with a valid ``plugin_dir``.

        Returns:
            The imported Python module.

        Raises:
            ImportError: If the module cannot be imported.
        """
        plugin_dir = plugin_info.plugin_dir
        if plugin_dir is None:
            raise ImportError('plugin_dir is None')

        added_to_path = False
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
            added_to_path = True

        try:
            module = importlib.import_module(plugin_info.module_path)
            return module
        finally:
            # Remove the temporary sys.path entry to keep the
            # environment clean, unless it was already present.
            if added_to_path and plugin_dir in sys.path:
                try:
                    sys.path.remove(plugin_dir)
                except ValueError:
                    pass

    def load_all_plugins(self) -> Dict[str, bool]:
        """Load all plugins currently in the DISCOVERED state.

        Returns:
            A dictionary mapping plugin name to ``True``/``False``
            indicating load success per plugin.
        """
        results: Dict[str, bool] = {}
        plugins_to_load = [
            name for name, info in self._plugins.items()
            if info.state == PluginState.DISCOVERED
        ]

        for name in plugins_to_load:
            results[name] = self.load_plugin(name)

        loaded_count = sum(1 for v in results.values() if v)
        failed_count = sum(1 for v in results.values() if not v)

        self.logger.info(
            'Bulk load complete: %d loaded, %d failed out of %d',
            loaded_count,
            failed_count,
            len(results),
        )
        return results

    # ------------------------------------------------------------------
    # Plugin Activation
    # ------------------------------------------------------------------

    def activate_plugin(self, plugin_name: str) -> bool:
        """Activate a loaded plugin — register its extensions.

        Scans the loaded plugin module for :class:`ExtensionPoint`
        subclasses and registers instances with the
        :class:`ExtensionRegistry`.  Emits a ``plugin.activated`` event
        via the event bus.

        Args:
            plugin_name: The unique name of the plugin to activate.

        Returns:
            ``True`` if the plugin was activated successfully.
        """
        with self._lock:
            plugin_info = self._plugins.get(plugin_name)

            if plugin_info is None:
                self.logger.warning(
                    'Cannot activate unknown plugin: %s', plugin_name,
                )
                return False

            if plugin_info.state != PluginState.LOADED:
                self.logger.warning(
                    'Cannot activate plugin %s — current state is %s '
                    '(expected LOADED)',
                    plugin_name,
                    plugin_info.state.value,
                )
                return False

            try:
                extensions_registered = self._register_extensions(
                    plugin_info,
                )

                plugin_info.state = PluginState.ACTIVATED

                # Emit lifecycle event via the Blinker event bus.
                # Lazy import to prevent circular dependency at module
                # load time — event_bus.py depends on extension_points.py
                # which must be fully loaded before this module.
                try:
                    from src.app.events.event_bus import emit_event
                    emit_event('plugin.activated', payload={
                        'plugin_name': plugin_name,
                        'version': plugin_info.version,
                        'extensions': list(plugin_info.provides),
                    })
                except Exception as event_exc:
                    # Event emission failure must NOT prevent activation
                    self.logger.warning(
                        'Failed to emit plugin.activated event for %s: %s',
                        plugin_name,
                        str(event_exc),
                    )

                self.logger.info(
                    'Plugin activated: %s with %d extension(s)',
                    plugin_name,
                    extensions_registered,
                )
                return True

            except Exception as exc:
                plugin_info.state = PluginState.FAILED
                plugin_info.error_message = str(exc)
                self.logger.error(
                    'Failed to activate plugin %s: %s',
                    plugin_name,
                    str(exc),
                    exc_info=True,
                )
                return False

    def _register_extensions(self, plugin_info: PluginInfo) -> int:
        """Discover and register extensions from a loaded plugin module.

        Uses three strategies (in order):

        1. Call ``plugin_info.module.get_extensions()`` if it exists.
        2. Instantiate a plugin class from ``plugin_info.instance``.
        3. Scan the module for concrete :class:`ExtensionPoint` subclasses.

        Args:
            plugin_info: Metadata for the loaded plugin.

        Returns:
            The number of extensions registered.
        """
        registered_count: int = 0
        module = plugin_info.module

        # Strategy 1: Module exposes a get_extensions() factory function.
        if hasattr(module, 'get_extensions') and callable(
            getattr(module, 'get_extensions')
        ):
            extensions = module.get_extensions()
            if isinstance(extensions, (list, tuple)):
                for ext in extensions:
                    registered_count += self._register_single_extension(
                        ext, plugin_info,
                    )
            elif isinstance(extensions, dict):
                for _ext_type, ext_instance in extensions.items():
                    if isinstance(ext_instance, (list, tuple)):
                        for inst in ext_instance:
                            registered_count += (
                                self._register_single_extension(
                                    inst, plugin_info,
                                )
                            )
                    else:
                        registered_count += (
                            self._register_single_extension(
                                ext_instance, plugin_info,
                            )
                        )
            return registered_count

        # Strategy 2: Entry-point loaded a class / instance directly.
        if plugin_info.instance is not None:
            instance = plugin_info.instance
            if inspect.isclass(instance) and issubclass(
                instance, ExtensionPoint
            ):
                ext_obj = instance()
                registered_count += self._register_single_extension(
                    ext_obj, plugin_info,
                )
                return registered_count
            elif isinstance(instance, ExtensionPoint):
                registered_count += self._register_single_extension(
                    instance, plugin_info,
                )
                return registered_count

        # Strategy 3: Scan the module for ExtensionPoint subclasses.
        for _attr_name, obj in inspect.getmembers(module):
            if (
                inspect.isclass(obj)
                and issubclass(obj, ExtensionPoint)
                and obj is not ExtensionPoint
                and not getattr(obj, '__abstractmethods__', None)
            ):
                try:
                    ext_instance = obj()
                    registered_count += self._register_single_extension(
                        ext_instance, plugin_info,
                    )
                except Exception as exc:
                    self.logger.warning(
                        'Could not instantiate %s from plugin %s: %s',
                        obj.__name__,
                        plugin_info.name,
                        str(exc),
                    )

        return registered_count

    def _register_single_extension(
        self,
        extension: ExtensionPoint,
        plugin_info: PluginInfo,
    ) -> int:
        """Register a single extension instance with the registry.

        Determines the most-specific :class:`ExtensionPoint` subclass
        type for registry keying.  Calls :meth:`ExtensionPoint.activate`
        on the extension after registration.

        Args:
            extension: The extension instance to register.
            plugin_info: The owning plugin's metadata.

        Returns:
            ``1`` on successful registration, ``0`` on failure.
        """
        try:
            ext_type = self._resolve_extension_type(extension)

            self._extension_registry.register(ext_type, extension)

            # Call the extension's activate() lifecycle hook.
            extension.activate()

            # Track what the plugin provides.
            type_name = ext_type.__name__
            if type_name not in plugin_info.provides:
                plugin_info.provides.append(type_name)

            self.logger.debug(
                'Registered extension %s (%s) from plugin %s',
                extension.name,
                type_name,
                plugin_info.name,
            )
            return 1

        except Exception as exc:
            self.logger.error(
                'Failed to register extension from plugin %s: %s',
                plugin_info.name,
                str(exc),
            )
            return 0

    @staticmethod
    def _resolve_extension_type(
        extension: ExtensionPoint,
    ) -> Type[ExtensionPoint]:
        """Resolve the canonical ExtensionPoint base for registry keying.

        Walks the MRO of the extension's class to find the first class
        that is a **direct** subclass of :class:`ExtensionPoint` (i.e.
        one of the five canonical extension point ABCs).

        Args:
            extension: An extension instance.

        Returns:
            The resolved extension point type.
        """
        for cls in type(extension).__mro__:
            if (
                cls is not ExtensionPoint
                and cls is not type(extension)
                and issubclass(cls, ExtensionPoint)
                and ExtensionPoint in cls.__mro__
            ):
                return cls
        # Fallback: use ExtensionPoint itself.
        return ExtensionPoint

    def activate_all_plugins(self) -> Dict[str, bool]:
        """Activate all plugins currently in the LOADED state.

        Respects dependency ordering — plugins whose dependencies are
        listed in :attr:`PluginInfo.dependencies` are activated first.

        Returns:
            A dictionary mapping plugin name to ``True``/``False``
            indicating activation success per plugin.
        """
        results: Dict[str, bool] = {}

        activation_order = self._resolve_dependency_order()

        for name in activation_order:
            info = self._plugins.get(name)
            if info is not None and info.state == PluginState.LOADED:
                results[name] = self.activate_plugin(name)

        activated_count = sum(1 for v in results.values() if v)
        failed_count = sum(1 for v in results.values() if not v)

        self.logger.info(
            'Bulk activation complete: %d activated, %d failed out of %d',
            activated_count,
            failed_count,
            len(results),
        )
        return results

    # ------------------------------------------------------------------
    # Plugin Deactivation
    # ------------------------------------------------------------------

    def deactivate_plugin(self, plugin_name: str) -> bool:
        """Deactivate an active plugin — unregister its extensions.

        Removes all extensions provided by the plugin from the
        :class:`ExtensionRegistry` and emits a ``plugin.deactivated``
        event.

        Args:
            plugin_name: The unique name of the plugin to deactivate.

        Returns:
            ``True`` if the plugin was deactivated successfully.
        """
        with self._lock:
            plugin_info = self._plugins.get(plugin_name)

            if plugin_info is None:
                self.logger.warning(
                    'Cannot deactivate unknown plugin: %s', plugin_name,
                )
                return False

            if plugin_info.state != PluginState.ACTIVATED:
                self.logger.warning(
                    'Cannot deactivate plugin %s — current state is %s '
                    '(expected ACTIVATED)',
                    plugin_name,
                    plugin_info.state.value,
                )
                return False

            try:
                self._unregister_plugin_extensions(plugin_info)

                plugin_info.state = PluginState.DEACTIVATED

                # Emit lifecycle event (lazy import).
                try:
                    from src.app.events.event_bus import emit_event
                    emit_event('plugin.deactivated', payload={
                        'plugin_name': plugin_name,
                        'version': plugin_info.version,
                        'extensions': list(plugin_info.provides),
                    })
                except Exception as event_exc:
                    self.logger.warning(
                        'Failed to emit plugin.deactivated event '
                        'for %s: %s',
                        plugin_name,
                        str(event_exc),
                    )

                self.logger.info(
                    'Plugin deactivated: %s', plugin_name,
                )
                return True

            except Exception as exc:
                self.logger.error(
                    'Failed to deactivate plugin %s: %s',
                    plugin_name,
                    str(exc),
                    exc_info=True,
                )
                return False

    def _unregister_plugin_extensions(
        self, plugin_info: PluginInfo,
    ) -> None:
        """Unregister all extensions belonging to a specific plugin.

        Iterates through all registered extension types and removes any
        extension whose defining module matches the plugin's loaded
        module.  Calls :meth:`ExtensionPoint.deactivate` on each
        extension before removal.

        Args:
            plugin_info: The plugin whose extensions should be removed.
        """
        if plugin_info.module is not None:
            for ext_type in list(
                self._extension_registry.get_extension_types()
            ):
                for ext in list(
                    self._extension_registry.get_extensions(ext_type)
                ):
                    ext_module = inspect.getmodule(type(ext))
                    if ext_module is plugin_info.module:
                        try:
                            ext.deactivate()
                        except Exception as exc:
                            self.logger.warning(
                                'Error deactivating extension %s: %s',
                                getattr(ext, 'name', '<unknown>'),
                                str(exc),
                            )
                        self._extension_registry.unregister(ext_type, ext)
        else:
            # Fallback: clear provides types (best-effort)
            for type_name in plugin_info.provides:
                for ext_type in list(
                    self._extension_registry.get_extension_types()
                ):
                    if ext_type.__name__ == type_name:
                        self._extension_registry.unregister_all(ext_type)

        plugin_info.provides.clear()

    def deactivate_all_plugins(self) -> None:
        """Deactivate all plugins currently in the ACTIVATED state.

        Plugins are deactivated in reverse dependency order — dependents
        are deactivated before their dependencies.
        """
        activation_order = self._resolve_dependency_order()
        deactivation_order = list(reversed(activation_order))

        deactivated_count = 0
        for name in deactivation_order:
            info = self._plugins.get(name)
            if info is not None and info.state == PluginState.ACTIVATED:
                if self.deactivate_plugin(name):
                    deactivated_count += 1

        self.logger.info(
            'Bulk deactivation complete: %d plugin(s) deactivated',
            deactivated_count,
        )

    # ------------------------------------------------------------------
    # Plugin Query Methods
    # ------------------------------------------------------------------

    def get_plugin(self, plugin_name: str) -> Optional[PluginInfo]:
        """Retrieve metadata for a specific plugin by name.

        Args:
            plugin_name: The unique name of the plugin.

        Returns:
            The :class:`PluginInfo` instance, or ``None`` if not found.
        """
        return self._plugins.get(plugin_name)

    def get_all_plugins(self) -> List[PluginInfo]:
        """Retrieve metadata for all known plugins.

        Returns:
            A list of :class:`PluginInfo` objects.
        """
        return list(self._plugins.values())

    def get_plugins_by_state(self, state: PluginState) -> List[PluginInfo]:
        """Retrieve all plugins in a specific lifecycle state.

        Args:
            state: The :class:`PluginState` to filter by.

        Returns:
            A list of :class:`PluginInfo` objects matching the state.
        """
        return [
            info for info in self._plugins.values()
            if info.state == state
        ]

    def is_plugin_active(self, plugin_name: str) -> bool:
        """Check whether a named plugin is currently active.

        Args:
            plugin_name: The unique name of the plugin.

        Returns:
            ``True`` if the plugin exists and is in ACTIVATED state.
        """
        info = self._plugins.get(plugin_name)
        return info is not None and info.state == PluginState.ACTIVATED

    # ------------------------------------------------------------------
    # Configuration Methods
    # ------------------------------------------------------------------

    def add_plugin_directory(self, directory: str | Path) -> None:
        """Add a directory to the plugin scan path.

        Args:
            directory: Filesystem path to a directory containing plugin
                Python modules or packages.
        """
        dir_path = (
            Path(directory) if not isinstance(directory, Path) else directory
        )

        if not dir_path.exists():
            self.logger.warning(
                'Plugin directory does not exist (will scan when '
                'available): %s',
                dir_path,
            )

        if dir_path not in self._plugin_dirs:
            self._plugin_dirs.append(dir_path)
            self.logger.debug('Added plugin directory: %s', dir_path)
        else:
            self.logger.debug(
                'Plugin directory already registered: %s', dir_path,
            )

    def set_entry_point_group(self, group: str) -> None:
        """Override the default entry point group name.

        Args:
            group: The setuptools entry point group name to use for
                plugin discovery (default: ``'nexus.plugins'``).
        """
        self._entry_point_group = group
        self.logger.debug('Entry point group set to: %s', group)

    # ------------------------------------------------------------------
    # Extension Registry Access
    # ------------------------------------------------------------------

    @property
    def extension_registry(self) -> ExtensionRegistry:
        """Access the central extension registry.

        Returns:
            The :class:`ExtensionRegistry` instance managed by this
            plugin manager.
        """
        return self._extension_registry

    # ------------------------------------------------------------------
    # Dependency Resolution
    # ------------------------------------------------------------------

    def _resolve_dependency_order(self) -> List[str]:
        """Compute a topological ordering of plugins by dependencies.

        Uses depth-first traversal to produce an activation order where
        dependencies appear before their dependents.  Circular
        dependencies are detected and logged as warnings — cycles are
        broken by skipping the back-edge.

        Returns:
            A list of plugin names in dependency-first order.
        """
        visited: Set[str] = set()
        in_stack: Set[str] = set()
        order: List[str] = []

        def _visit(name: str) -> None:
            if name in visited:
                return
            if name in in_stack:
                self.logger.warning(
                    'Circular dependency detected involving plugin: %s',
                    name,
                )
                return

            in_stack.add(name)

            info = self._plugins.get(name)
            if info is not None:
                for dep_name in info.dependencies:
                    if dep_name in self._plugins:
                        _visit(dep_name)
                    else:
                        self.logger.warning(
                            'Plugin %s depends on unknown plugin %s',
                            name,
                            dep_name,
                        )

            in_stack.discard(name)
            visited.add(name)
            order.append(name)

        for plugin_name in list(self._plugins.keys()):
            _visit(plugin_name)

        return order


# ============================================================================
# Module-Level Singleton Factory
# ============================================================================

_plugin_manager_instance: Optional[PluginManager] = None
_instance_lock: threading.Lock = threading.Lock()


def get_plugin_manager() -> PluginManager:
    """Get or create the singleton :class:`PluginManager` instance.

    Thread-safe singleton factory using the double-checked locking
    pattern.  Returns the same :class:`PluginManager` instance across
    all calls within the same process.

    Returns:
        The singleton :class:`PluginManager`.
    """
    global _plugin_manager_instance
    if _plugin_manager_instance is None:
        with _instance_lock:
            if _plugin_manager_instance is None:
                _plugin_manager_instance = PluginManager()
    return _plugin_manager_instance


# ============================================================================
# Flask Integration Helper
# ============================================================================

def init_plugins(app: Any) -> PluginManager:
    """Initialise the plugin system within a Flask application context.

    Called from ``src/app/factory.py`` during ``create_app()``.  Reads
    plugin configuration from ``app.config`` and performs full plugin
    lifecycle: discovery, loading, and activation.

    **Critical:** Plugin initialisation failure must **NOT** prevent
    the Flask application from starting.  All errors are caught and
    logged.

    Configuration keys read from ``app.config``:

    - ``PLUGIN_DIRS`` — list of plugin directory paths.
    - ``PLUGIN_ENTRY_POINT_GROUP`` — entry point group name
      (default: ``'nexus.plugins'``).
    - ``PLUGINS_ENABLED`` — master enable/disable switch
      (default: ``True``).

    Args:
        app: The Flask application instance.

    Returns:
        The initialised :class:`PluginManager` singleton.
    """
    manager = get_plugin_manager()

    try:
        plugins_enabled: bool = app.config.get('PLUGINS_ENABLED', True)

        if not plugins_enabled:
            logger.warning(
                'Plugin system is disabled via PLUGINS_ENABLED=False',
            )
            app.extensions['plugin_manager'] = manager
            return manager

        # Configure the entry point group.
        entry_point_group: str = app.config.get(
            'PLUGIN_ENTRY_POINT_GROUP', 'nexus.plugins',
        )
        manager.set_entry_point_group(entry_point_group)

        # Configure plugin directories.
        plugin_dirs: List[str] = app.config.get('PLUGIN_DIRS', [])
        for dir_path in plugin_dirs:
            manager.add_plugin_directory(dir_path)

        # Full lifecycle: discover -> load -> activate.
        discovered = manager.discover_plugins()
        load_results = manager.load_all_plugins()
        activation_results = manager.activate_all_plugins()

        active_count = sum(
            1 for info in manager.get_all_plugins()
            if info.state == PluginState.ACTIVATED
        )

        logger.info(
            'Plugin system initialised: %d discovered, %d loaded, '
            '%d activated',
            len(discovered),
            sum(1 for v in load_results.values() if v),
            active_count,
        )

    except Exception as exc:
        # CRITICAL: Plugin failure must never prevent app startup.
        logger.error(
            'Plugin system initialisation failed — application will '
            'continue without plugins: %s',
            str(exc),
            exc_info=True,
        )

    # Always store the manager on the app, even on failure.
    app.extensions['plugin_manager'] = manager

    return manager


# ============================================================================
# Public API
# ============================================================================

__all__: List[str] = [
    'PluginManager',
    'PluginInfo',
    'PluginState',
    'get_plugin_manager',
    'init_plugins',
]
