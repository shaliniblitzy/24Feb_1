"""
Plugin Management REST API Blueprint — Feature F-504 (Plugin Architecture).

This module implements the plugin management REST API endpoints for the
Sonatype Nexus Repository Flask application.  It provides read-only
discovery endpoints that expose the :class:`PluginManager` and
:class:`ExtensionRegistry` programmatic interfaces as REST resources,
enabling administrators to list installed plugins and discover available
extension points.

Endpoints:
    GET  /api/v1/plugins                  — List all installed plugins
    GET  /api/v1/plugins/extension-points — List available extension points

Architecture Context:
    In the Java source system, OSGi/Karaf 4.4.4 provided bundle listing
    and capability introspection via the Karaf shell and REST management
    API.  ``CapabilityRegistryBooter.java`` activated capabilities and
    tracked their lifecycle state.  This Python reimplementation replaces
    those components with a lightweight Flask-smorest Blueprint backed by
    the ``PluginManager`` singleton.

    +-------------------------------+--------------------------------------+
    | Java Component                | Python Replacement                   |
    +===============================+======================================+
    | OSGi Bundle Repository        | PluginManager.get_all_plugins()      |
    +-------------------------------+--------------------------------------+
    | OSGi Service Registry         | ExtensionRegistry                    |
    +-------------------------------+--------------------------------------+
    | Karaf Shell (bundle:list)     | GET /api/v1/plugins                  |
    +-------------------------------+--------------------------------------+
    | Capability introspection      | GET /api/v1/plugins/extension-points |
    +-------------------------------+--------------------------------------+

Security Model:
    - All endpoints require authentication (``@login_required``).
    - Read operations require ``plugins:read`` system-wide (Tier 1)
      privilege enforced via ``@require_permission``.
    - All operations are logged for audit trail compliance (Feature F-303).

Exports:
    plugins_bp : flask_smorest.Blueprint
        The plugins API blueprint, registered at ``/api/v1/plugins``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from flask import current_app, jsonify
from flask_smorest import Blueprint, abort
from werkzeug.exceptions import HTTPException

from src.app.auth.authentication import login_required
from src.app.auth.authorization import require_permission
from src.app.plugins.plugin_manager import (
    PluginInfo,
    PluginManager,
    PluginState,
    get_plugin_manager,
)
from src.app.plugins.extension_points import (
    ExtensionRegistry,
    FormatHandlerExtension,
    AuthRealmExtension,
    StorageBackendExtension,
    ScheduledTaskExtension,
    EventSubscriberExtension,
)

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source system.  Provides diagnostic output for plugin discovery queries.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ===========================================================================
# Blueprint Definition
# ===========================================================================

plugins_bp: Blueprint = Blueprint(
    "plugins",
    __name__,
    url_prefix="/api/v1/plugins",
    description="Plugin management and extension point discovery (F-504)",
)
"""Flask-smorest Blueprint for the plugins API endpoint group.

Registered at ``/api/v1/plugins`` by the application factory in
``src.app.factory.create_app()``.  Provides OpenAPI 3.x auto-generated
documentation for plugin management endpoints.
"""

# ===========================================================================
# Well-Known Extension Point Types
# ===========================================================================
# These are the built-in extension point abstract base classes that plugins
# can implement.  They are listed here for serialisation in the extension
# points API response.
# ===========================================================================

EXTENSION_POINT_TYPES: List[Dict[str, Any]] = [
    {
        "name": "FormatHandlerExtension",
        "description": "Custom repository format handlers (e.g., Helm, Cargo, Conda)",
        "category": "repository",
        "interface": "src.app.plugins.extension_points.FormatHandlerExtension",
        "required_methods": [
            "format_name",
            "content_types",
            "validate_upload",
            "extract_metadata",
        ],
    },
    {
        "name": "AuthRealmExtension",
        "description": "Custom authentication backends (e.g., OAuth2, Kerberos)",
        "category": "security",
        "interface": "src.app.plugins.extension_points.AuthRealmExtension",
        "required_methods": [
            "realm_name",
            "authenticate",
            "supports",
        ],
    },
    {
        "name": "StorageBackendExtension",
        "description": "Custom BlobStore storage backends (e.g., Azure Blob, GCS)",
        "category": "storage",
        "interface": "src.app.plugins.extension_points.StorageBackendExtension",
        "required_methods": [
            "backend_type",
            "store_blob",
            "get_blob",
            "delete_blob",
            "blob_exists",
        ],
    },
    {
        "name": "ScheduledTaskExtension",
        "description": "Custom background task types (e.g., report generation, data export)",
        "category": "scheduling",
        "interface": "src.app.plugins.extension_points.ScheduledTaskExtension",
        "required_methods": [
            "task_type",
            "execute",
        ],
    },
    {
        "name": "EventSubscriberExtension",
        "description": "Custom event listeners for system events",
        "category": "events",
        "interface": "src.app.plugins.extension_points.EventSubscriberExtension",
        "required_methods": [
            "get_subscribed_events",
            "handle_event",
        ],
    },
]

# Map from extension point class to its type name for serialisation
_EXTENSION_TYPE_MAP: Dict[type, str] = {
    FormatHandlerExtension: "FormatHandlerExtension",
    AuthRealmExtension: "AuthRealmExtension",
    StorageBackendExtension: "StorageBackendExtension",
    ScheduledTaskExtension: "ScheduledTaskExtension",
    EventSubscriberExtension: "EventSubscriberExtension",
}


# ===========================================================================
# Helper Functions
# ===========================================================================


def _get_plugin_manager() -> PluginManager:
    """Retrieve the PluginManager singleton.

    Attempts to read from ``current_app.extensions['plugin_manager']``
    first (set by ``init_plugins()`` during factory startup).  Falls back
    to the module-level singleton factory ``get_plugin_manager()`` if the
    Flask extension is not yet initialised.

    Returns:
        The :class:`PluginManager` singleton instance.
    """
    manager: Optional[PluginManager] = current_app.extensions.get(
        "plugin_manager"
    )
    if manager is not None:
        return manager
    return get_plugin_manager()


def _serialize_plugin(info: PluginInfo) -> Dict[str, Any]:
    """Serialise a :class:`PluginInfo` dataclass to a JSON-safe dictionary.

    Excludes non-serialisable attributes (``module`` and ``instance``
    references) and converts enum values to their string representations.

    Args:
        info: The plugin metadata to serialise.

    Returns:
        A dictionary suitable for JSON serialisation.
    """
    return {
        "name": info.name,
        "version": info.version,
        "description": info.description,
        "author": info.author,
        "state": info.state.value if isinstance(info.state, PluginState) else str(info.state),
        "module_path": info.module_path,
        "entry_point": info.entry_point,
        "error_message": info.error_message,
        "dependencies": info.dependencies,
        "provides": info.provides,
    }


# ===========================================================================
# Endpoint 1 — List Plugins (GET /api/v1/plugins)
# ===========================================================================


@plugins_bp.route("/", methods=["GET"])
@plugins_bp.response(200, description="List of all installed plugins")
@login_required
@require_permission("plugins", "read")
def list_plugins() -> Any:
    """List all installed and discovered plugins.

    Returns a JSON array of plugin metadata objects, each containing
    the plugin's name, version, description, lifecycle state, author,
    dependencies, and provided extension points.

    The response includes plugins in all lifecycle states
    (DISCOVERED, LOADED, ACTIVATED, DEACTIVATED, FAILED).

    **Authentication:** Required (``@login_required``).
    **Authorization:** ``plugins:read`` system-wide privilege.

    HTTP Status Codes:
        200: Plugin list returned successfully.
        401: Authentication required.
        403: Insufficient privileges.
        500: Internal server error.

    Returns:
        JSON array of plugin metadata objects.
    """
    logger.info("Listing all installed plugins.")

    try:
        manager: PluginManager = _get_plugin_manager()
        all_plugins: List[PluginInfo] = manager.get_all_plugins()

        serialized: List[Dict[str, Any]] = [
            _serialize_plugin(info) for info in all_plugins
        ]

        # Build summary counts by state for the response wrapper
        state_counts: Dict[str, int] = {}
        for info in all_plugins:
            state_val = info.state.value if isinstance(info.state, PluginState) else str(info.state)
            state_counts[state_val] = state_counts.get(state_val, 0) + 1

        response: Dict[str, Any] = {
            "plugins": serialized,
            "total": len(serialized),
            "state_summary": state_counts,
        }

        logger.info(
            "Listed %d plugin(s) (states: %s).",
            len(serialized),
            state_counts,
        )

        return jsonify(response), 200

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Unexpected error listing plugins: %s", str(exc)
        )
        abort(500, message="An unexpected error occurred while listing plugins.")


# ===========================================================================
# Endpoint 2 — List Extension Points (GET /api/v1/plugins/extension-points)
# ===========================================================================


@plugins_bp.route("/extension-points", methods=["GET"])
@plugins_bp.response(200, description="List of available extension points")
@login_required
@require_permission("plugins", "read")
def list_extension_points() -> Any:
    """List all available extension points and their registered implementations.

    Returns a JSON array of extension point type descriptors.  Each
    descriptor includes the extension point name, description, category,
    interface path, required methods, and a count (and listing) of
    currently registered implementations from active plugins.

    **Authentication:** Required (``@login_required``).
    **Authorization:** ``plugins:read`` system-wide privilege.

    HTTP Status Codes:
        200: Extension point list returned successfully.
        401: Authentication required.
        403: Insufficient privileges.
        500: Internal server error.

    Returns:
        JSON object with extension point descriptors and summary.
    """
    logger.info("Listing available extension points.")

    try:
        manager: PluginManager = _get_plugin_manager()
        registry: ExtensionRegistry = manager.extension_registry

        extension_points_data: List[Dict[str, Any]] = []

        for ep_info in EXTENSION_POINT_TYPES:
            # Find the corresponding class for querying the registry
            ep_class: Optional[type] = None
            for cls, name in _EXTENSION_TYPE_MAP.items():
                if name == ep_info["name"]:
                    ep_class = cls
                    break

            registered_extensions: List[Dict[str, str]] = []
            if ep_class is not None:
                for ext in registry.get_extensions(ep_class):
                    registered_extensions.append({
                        "name": ext.name,
                        "description": ext.description,
                        "priority": ext.get_priority(),
                    })

            extension_points_data.append({
                "name": ep_info["name"],
                "description": ep_info["description"],
                "category": ep_info["category"],
                "interface": ep_info["interface"],
                "required_methods": ep_info["required_methods"],
                "registered_count": len(registered_extensions),
                "registered_extensions": registered_extensions,
            })

        total_registered: int = sum(
            ep["registered_count"] for ep in extension_points_data
        )

        response: Dict[str, Any] = {
            "extension_points": extension_points_data,
            "total_extension_points": len(extension_points_data),
            "total_registered_extensions": total_registered,
        }

        logger.info(
            "Listed %d extension point(s) with %d registered extension(s).",
            len(extension_points_data),
            total_registered,
        )

        return jsonify(response), 200

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Unexpected error listing extension points: %s", str(exc)
        )
        abort(
            500,
            message="An unexpected error occurred while listing extension points.",
        )


# ===========================================================================
# Public API
# ===========================================================================

__all__: List[str] = [
    "plugins_bp",
]
