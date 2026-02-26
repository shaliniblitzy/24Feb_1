"""
API Blueprint Registration and OpenAPI/Swagger Setup.

This is the foundational ``__init__`` module for the ``src.app.api`` package.
It imports all 15 Flask-smorest Blueprint instances from their respective
sub-modules and provides a :func:`register_api_blueprints` function that is
called by ``src/app/factory.py`` during application creation.

**Architecture Context:**

Replaces the RESTEasy 6.2.7 JAX-RS router configuration from the original
Java source system (Sonatype Nexus Repository).  In the Java architecture,
JAX-RS resource classes were discovered and registered by the RESTEasy
framework via classpath scanning inside OSGi/Karaf 4.4.4 bundles.  In this
Python/Flask reimplementation, each functional area is encapsulated as a
Flask-smorest Blueprint and explicitly registered with the ``Api`` instance
during application startup.

**OpenAPI/Swagger Integration:**

The ``flask-smorest 0.45.0`` ``Api`` class handles:

- Flask Blueprint registration (route mounting)
- Automatic OpenAPI 3.x specification generation from decorated routes
- Interactive Swagger UI serving at ``/api/docs`` (configurable)
- Marshmallow schema integration for request/response documentation

**Blueprint Registration Order:**

Blueprints are registered in a deliberate order:

1. ``health_bp`` — registered first so health probes are available as soon
   as possible during application startup (Kubernetes liveness/readiness).
2. Core CRUD blueprints (repositories, components, assets, search).
3. Security blueprints (users, roles, privileges, SSL/TLS).
4. Administration blueprints (tasks, system).
5. Storage blueprints (blobstores, cleanup).
6. Integration blueprints (scripts, webhooks).

**Usage in factory.py:**

.. code-block:: python

    from src.app.api import register_api_blueprints

    def create_app(config_name=None):
        app = Flask(__name__)
        # ... config loading ...
        api = Api(app)
        register_api_blueprints(api)
        return app

**Blueprint URL Prefix Reference:**

+------------------+-----------------------------------+
| Blueprint        | URL Prefix                        |
+==================+===================================+
| health_bp        | /api/v1/health                    |
| repositories_bp  | /api/v1/repositories              |
| components_bp    | /api/v1/components                |
| assets_bp        | /api/v1/assets                    |
| search_bp        | /api/v1/search                    |
| users_bp         | /api/v1/security/users            |
| roles_bp         | /api/v1/security/roles            |
| privileges_bp    | /api/v1/security/privileges       |
| security_bp      | /api/v1/security/ssl              |
| tasks_bp         | /api/v1/tasks                     |
| system_bp        | /api/v1/system                    |
| blobstores_bp    | /api/v1/blobstores                |
| cleanup_bp       | /api/v1/cleanup-policies          |
| scripts_bp       | /api/v1/scripts                   |
| webhooks_bp      | /api/v1/webhooks                  |
+------------------+-----------------------------------+

Exports:
    register_api_blueprints : Registers all 15 API blueprints with a
        flask-smorest Api instance.
    ALL_BLUEPRINTS : Ordered list of all blueprint instances.
    repositories_bp : Repository CRUD blueprint (F-501-RQ-001).
    components_bp : Component management blueprint (F-501-RQ-002).
    assets_bp : Asset operations blueprint (F-501-RQ-002).
    users_bp : User management blueprint (F-501-RQ-003).
    roles_bp : Role management blueprint (F-501-RQ-003).
    privileges_bp : Privilege management blueprint (F-501-RQ-003).
    tasks_bp : Task scheduling blueprint (F-402, F-501-RQ-004).
    system_bp : System configuration blueprint (F-404, F-501-RQ-004).
    search_bp : Full-text search blueprint (F-103).
    security_bp : SSL/TLS management blueprint (F-302).
    blobstores_bp : BlobStore management blueprint (F-201, F-202).
    cleanup_bp : Cleanup policy management blueprint (F-204).
    scripts_bp : Script management blueprint (F-502).
    webhooks_bp : Webhook integration blueprint (F-503).
    health_bp : Health check and monitoring blueprint (F-401).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

# ---------------------------------------------------------------------------
# External Imports
# ---------------------------------------------------------------------------
# flask-smorest 0.45.0 — Api class for OpenAPI 3.x specification generation
# and Blueprint registration.  Replaces RESTEasy 6.2.7 JAX-RS router from
# the Java source system.
#
# The Api type is imported under TYPE_CHECKING for the function signature
# annotation.  At runtime, the function receives an already-initialized
# Api instance from factory.py / extensions.py.
# ---------------------------------------------------------------------------

if TYPE_CHECKING:
    from flask_smorest import Api

# ---------------------------------------------------------------------------
# Internal Imports — All 15 API Blueprints
# ---------------------------------------------------------------------------
# Each sub-module exports exactly one flask-smorest Blueprint instance.
# The import order follows the logical registration order defined in the
# ALL_BLUEPRINTS list below.
#
# CRITICAL: ALL 15 imports MUST be present.  Missing any import will result
# in an unregistered API surface and broken endpoint routes.
# ---------------------------------------------------------------------------

# Health and monitoring (F-401) — registered first for early probe availability
from src.app.api.health import health_bp

# Core repository management (F-501-RQ-001, F-501-RQ-002, F-103)
from src.app.api.repositories import repositories_bp
from src.app.api.components import components_bp
from src.app.api.assets import assets_bp
from src.app.api.search import search_bp

# Security management (F-501-RQ-003, F-302)
from src.app.api.users import users_bp
from src.app.api.roles import roles_bp
from src.app.api.privileges import privileges_bp
from src.app.api.security import security_bp

# Administration (F-501-RQ-004, F-402, F-404)
from src.app.api.tasks import tasks_bp
from src.app.api.system import system_bp

# Storage management (F-201, F-202, F-204)
from src.app.api.blobstores import blobstores_bp
from src.app.api.cleanup import cleanup_bp

# Integration and extensibility (F-502, F-503)
from src.app.api.scripts import scripts_bp
from src.app.api.webhooks import webhooks_bp

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source.
# Provides structured logging for blueprint registration diagnostics.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Blueprint Collection
# ---------------------------------------------------------------------------
# Ordered list of all API blueprints.  The registration order is deliberate:
#
#   1. Health (F-401) — earliest availability for Kubernetes probes and
#      load-balancer health checks.
#   2. Core CRUD — repositories, components, assets, search.
#   3. Security — users, roles, privileges, SSL/TLS certificates.
#   4. Administration — tasks, system configuration.
#   5. Storage — blobstores, cleanup policies.
#   6. Integration — scripts, webhooks.
#
# This matches the logical layering from the AAP Section 0.4.1 target
# structure and mirrors the OSGi bundle activation order from the original
# Java Karaf 4.4.4 container.
# ---------------------------------------------------------------------------

ALL_BLUEPRINTS: List = [
    # ── Health and Monitoring ──────────────────────────────────────────
    health_bp,          # F-401: Health checks, liveness/readiness probes

    # ── Core Repository Management ────────────────────────────────────
    repositories_bp,    # F-501-RQ-001: Repository CRUD and lifecycle
    components_bp,      # F-501-RQ-002: Component upload/download/delete
    assets_bp,          # F-501-RQ-002: Asset operations and binary download
    search_bp,          # F-103: Full-text search and browse tree navigation

    # ── Security Management ───────────────────────────────────────────
    users_bp,           # F-501-RQ-003: User CRUD and API key management
    roles_bp,           # F-501-RQ-003: Role CRUD and role-user assignment
    privileges_bp,      # F-501-RQ-003: Privilege and content selector mgmt
    security_bp,        # F-302: SSL/TLS certificate management

    # ── Administration ────────────────────────────────────────────────
    tasks_bp,           # F-402, F-501-RQ-004: Task scheduling and history
    system_bp,          # F-404, F-501-RQ-004: System config and support ZIP

    # ── Storage Management ────────────────────────────────────────────
    blobstores_bp,      # F-201, F-202: BlobStore CRUD, metrics, quotas
    cleanup_bp,         # F-204: Cleanup policy CRUD and dry-run preview

    # ── Integration and Extensibility ─────────────────────────────────
    scripts_bp,         # F-502: Script CRUD and sandboxed execution
    webhooks_bp,        # F-503: Webhook configuration and delivery history
]

# ---------------------------------------------------------------------------
# Blueprint Registration Function
# ---------------------------------------------------------------------------


def register_api_blueprints(api: "Api") -> None:
    """Register all API blueprints with the flask-smorest Api instance.

    Called by ``src/app/factory.py`` during ``create_app()`` initialization.
    The *api* parameter is a :class:`flask_smorest.Api` instance that handles:

    - Flask Blueprint registration (mounting route handlers)
    - OpenAPI 3.x specification generation from decorator metadata
    - Swagger UI endpoint serving at ``/api/docs``

    Each blueprint's ``url_prefix`` is defined within the blueprint itself
    (set via :class:`flask_smorest.Blueprint` constructor arguments).  This
    function simply iterates the ordered ``ALL_BLUEPRINTS`` list and calls
    ``api.register_blueprint()`` for each one.

    The registration order is deliberate — health endpoints are registered
    first to ensure they are available for Kubernetes liveness probes as
    early as possible in the application lifecycle.

    Args:
        api: A :class:`flask_smorest.Api` instance (from
            ``src/app/extensions.py``).  This is **not** a raw Flask app —
            flask-smorest's ``Api`` wraps the Flask app and provides
            additional OpenAPI functionality.

    Raises:
        ValueError: If *api* is ``None``.
        RuntimeError: If a blueprint has already been registered with
            the same name (propagated from Flask's internal registration).

    Example::

        from flask import Flask
        from flask_smorest import Api
        from src.app.api import register_api_blueprints

        app = Flask(__name__)
        app.config.update({
            "API_TITLE": "Nexus Repository Manager",
            "API_VERSION": "v1",
            "OPENAPI_VERSION": "3.0.3",
        })
        api = Api(app)
        register_api_blueprints(api)
        # All 15 blueprints are now registered; OpenAPI spec is generated.
    """
    if api is None:
        raise ValueError(
            "Cannot register API blueprints: the 'api' parameter must be "
            "a flask_smorest.Api instance, got None."
        )

    registered_count: int = 0

    for bp in ALL_BLUEPRINTS:
        try:
            api.register_blueprint(bp)
            registered_count += 1
            logger.debug(
                "Registered API blueprint: %s (prefix: %s)",
                bp.name,
                getattr(bp, "url_prefix", "N/A"),
            )
        except Exception:
            logger.exception(
                "Failed to register API blueprint: %s",
                getattr(bp, "name", repr(bp)),
            )
            raise

    logger.info(
        "All %d API blueprints registered successfully.", registered_count
    )


# ---------------------------------------------------------------------------
# Module Public API — Explicit __all__ Declaration
# ---------------------------------------------------------------------------
# Lists all public symbols exported from this package.  Consumers can use:
#
#   from src.app.api import register_api_blueprints
#   from src.app.api import ALL_BLUEPRINTS
#   from src.app.api import health_bp, repositories_bp, ...
#
# 17 exports total:  1 function + 1 constant + 15 blueprint instances.
# ---------------------------------------------------------------------------

__all__: List[str] = [
    # Registration function
    "register_api_blueprints",

    # Ordered blueprint collection
    "ALL_BLUEPRINTS",

    # Individual blueprint re-exports (alphabetical for discoverability)
    "assets_bp",
    "blobstores_bp",
    "cleanup_bp",
    "components_bp",
    "health_bp",
    "privileges_bp",
    "repositories_bp",
    "roles_bp",
    "scripts_bp",
    "search_bp",
    "security_bp",
    "system_bp",
    "tasks_bp",
    "users_bp",
    "webhooks_bp",
]
