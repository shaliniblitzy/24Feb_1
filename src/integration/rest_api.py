"""
REST API Framework with OpenAPI Specification Generation (Feature F-501).

Implements the comprehensive REST API documentation layer for the Nexus Repository
Manager, providing automatic OpenAPI 3.0.3 specification generation from Flask
routes. This module replaces the Swagger 2.2.20 + JAX-RS annotation-based
documentation system from the original Java stack.

Architecture:
    - Primary: Uses flask-smorest 0.45.0 for standards-compliant OpenAPI 3.x
      specification generation when available.
    - Fallback: Provides a manual OpenAPI spec generation engine that introspects
      Flask URL rules and constructs a valid OpenAPI 3.0.3 document, ensuring
      documentation is always available regardless of flask-smorest integration
      status.

Endpoints served:
    - /service/rest/api-spec.json  — Machine-readable OpenAPI specification
    - /swagger.json               — Alias for backward compatibility
    - /service/rest/swagger-ui    — Interactive Swagger UI (when flask-smorest active)
    - /service/rest/redoc         — ReDoc documentation viewer (when flask-smorest active)

Tag grouping follows the five feature categories:
    - repositories: Repository Management (F-101 through F-104)
    - storage: Storage Management (F-201 through F-204)
    - security: Security Management (F-301 through F-304)
    - admin: Administration (F-401 through F-404)
    - integration: Integration & Extensibility (F-501 through F-504)

Security schemes documented:
    - BearerAuth: API Key or JWT token via Authorization header
    - BasicAuth: Username/password via HTTP Basic Authentication

Usage:
    from src.integration.rest_api import rest_api_manager

    # In application factory (src/app.py):
    rest_api_manager.init_app(app)

    # Manually register additional endpoint documentation:
    rest_api_manager.register_endpoint(
        path='/service/rest/v1/repositories',
        methods=['GET', 'POST'],
        summary='Repository management',
        tag='repositories',
    )

    # Generate the full OpenAPI spec programmatically:
    spec = rest_api_manager.generate_openapi_spec()
"""

import logging
from typing import Optional, Dict, Any, List

from flask import Flask, current_app, jsonify

from flask_smorest import Api as SmorestApi


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OpenAPI / flask-smorest configuration constants
# ---------------------------------------------------------------------------
REST_API_CONFIG: Dict[str, Any] = {
    'API_TITLE': 'Nexus Repository Manager REST API',
    'API_VERSION': 'v1',
    'OPENAPI_VERSION': '3.0.3',
    'OPENAPI_URL_PREFIX': '/service/rest',
    'OPENAPI_JSON_PATH': 'api-spec.json',
    'OPENAPI_SWAGGER_UI_PATH': '/swagger-ui',
    'OPENAPI_SWAGGER_UI_URL': 'https://cdn.jsdelivr.net/npm/swagger-ui-dist/',
    'OPENAPI_REDOC_PATH': '/redoc',
    'OPENAPI_REDOC_URL': (
        'https://cdn.jsdelivr.net/npm/redoc@latest/bundles/redoc.standalone.js'
    ),
}
"""Default REST API configuration applied to the Flask app.

Keys correspond to flask-smorest configuration parameters:
    - API_TITLE / API_VERSION: Metadata shown in the OpenAPI info block.
    - OPENAPI_VERSION: Target OpenAPI spec version (3.0.3).
    - OPENAPI_URL_PREFIX: URL prefix under which the spec and UI are served.
    - OPENAPI_JSON_PATH: Filename of the JSON spec under the URL prefix.
    - OPENAPI_SWAGGER_UI_PATH / _URL: Swagger UI location and CDN.
    - OPENAPI_REDOC_PATH / _URL: ReDoc viewer location and CDN.
"""


# ---------------------------------------------------------------------------
# Predefined OpenAPI tag definitions
# ---------------------------------------------------------------------------
_OPENAPI_TAGS: List[Dict[str, str]] = [
    {
        'name': 'repositories',
        'description': 'Repository Management (F-101 through F-104)',
    },
    {
        'name': 'storage',
        'description': 'Storage Management (F-201 through F-204)',
    },
    {
        'name': 'security',
        'description': 'Security Management (F-301 through F-304)',
    },
    {
        'name': 'admin',
        'description': 'Administration (F-401 through F-404)',
    },
    {
        'name': 'integration',
        'description': 'Integration & Extensibility (F-501 through F-504)',
    },
]

# Methods that should be excluded from OpenAPI path operations
_EXCLUDED_METHODS = frozenset({'OPTIONS', 'HEAD'})

# Flask static/internal endpoint prefixes to skip during spec generation
_SKIP_ENDPOINT_PREFIXES = frozenset({'static', '_debug_toolbar'})


# ---------------------------------------------------------------------------
# RestApiManager class
# ---------------------------------------------------------------------------
class RestApiManager:
    """REST API framework manager providing OpenAPI 3.0.3 documentation.

    Implements Feature F-501: Comprehensive REST API with OpenAPI documentation.
    Replaces Swagger 2.2.20 + JAX-RS annotations from the original Java stack.

    Responsibilities:
        - Configure flask-smorest for automatic OpenAPI spec generation.
        - Provide a fallback manual spec generator that introspects Flask routes.
        - Allow individual blueprints to manually register detailed endpoint
          metadata via ``register_endpoint()``.
        - Serve the OpenAPI specification JSON at ``/service/rest/api-spec.json``
          and ``/swagger.json``.

    The manager follows the *extension pattern* common in Flask:

        rest_api = RestApiManager()
        rest_api.init_app(app)

    Attributes:
        _api: Optional ``flask_smorest.Api`` instance when flask-smorest
            initializes successfully.
        _endpoint_registry: List of manually registered endpoint metadata
            dicts contributed by individual blueprint modules.
    """

    def __init__(self, app: Optional[Flask] = None) -> None:
        """Initialise the REST API manager.

        Args:
            app: Optional Flask application instance.  When provided,
                ``init_app()`` is called immediately.
        """
        self.logger = logging.getLogger(__name__)
        self._api: Optional[SmorestApi] = None
        self._endpoint_registry: List[Dict[str, Any]] = []
        self._initialized: bool = False
        if app is not None:
            self.init_app(app)

    # ------------------------------------------------------------------
    # Application initialisation
    # ------------------------------------------------------------------

    def init_app(self, app: Flask) -> None:
        """Initialise the REST API framework with a Flask application.

        This method is called during the application factory phase
        (``create_app()`` in ``src/app.py``).  It applies the default
        ``REST_API_CONFIG`` values to the application configuration and
        attempts to initialise flask-smorest for automatic OpenAPI spec
        generation.  If flask-smorest fails to initialise (for example,
        because the application uses vanilla Flask blueprints instead of
        smorest-style Blueprint classes), a manual fallback endpoint is
        registered so that the OpenAPI specification remains available.

        Args:
            app: The Flask application instance to configure.
        """
        # Apply default REST API configuration (do not overwrite existing keys)
        for key, value in REST_API_CONFIG.items():
            app.config.setdefault(key, value)

        try:
            self._api = SmorestApi(app)
            self.logger.info(
                'REST API OpenAPI documentation initialised via flask-smorest'
            )
        except Exception as exc:
            self.logger.warning(
                'flask-smorest initialisation skipped (%s); '
                'falling back to manual OpenAPI spec generation',
                exc,
            )
            self._api = None
            self._register_manual_spec_endpoint(app)

        self._initialized = True

    # ------------------------------------------------------------------
    # Fallback manual spec endpoint
    # ------------------------------------------------------------------

    def _register_manual_spec_endpoint(self, app: Flask) -> None:
        """Register a manual ``/swagger.json`` endpoint as a fallback.

        This is invoked when flask-smorest cannot fully integrate with the
        existing vanilla Flask blueprint routes.  The endpoint generates
        an OpenAPI 3.0.3 specification dynamically by introspecting the
        Flask application's registered URL rules.

        Two URL paths are registered for backward compatibility:
            - ``/swagger.json``
            - ``/service/rest/api-spec.json``

        Args:
            app: The Flask application instance.
        """
        # Capture ``self`` in the closure so the route handler can call
        # ``generate_openapi_spec()`` on this manager instance.
        manager = self

        @app.route('/swagger.json')
        @app.route('/service/rest/api-spec.json')
        def serve_openapi_spec():  # noqa: E501 – long line in decorator
            """Serve the auto-generated OpenAPI specification as JSON."""
            spec = manager.generate_openapi_spec()
            return jsonify(spec)

    # ------------------------------------------------------------------
    # OpenAPI specification generation
    # ------------------------------------------------------------------

    def generate_openapi_spec(self) -> Dict[str, Any]:
        """Generate an OpenAPI 3.0.3 specification from all Flask routes.

        Constructs the specification by:
            1. Defining metadata (title, version, description, contact).
            2. Iterating over all Flask URL rules and converting matching
               routes to OpenAPI path items.
            3. Merging any manually registered endpoint metadata.
            4. Grouping operations by blueprint-derived tags.
            5. Including security scheme definitions (Bearer + Basic).

        Returns:
            A fully-formed OpenAPI 3.0.3 specification dictionary ready
            to be serialised as JSON.
        """
        app: Flask = current_app._get_current_object()

        spec: Dict[str, Any] = {
            'openapi': '3.0.3',
            'info': {
                'title': REST_API_CONFIG.get(
                    'API_TITLE', 'Nexus Repository Manager REST API'
                ),
                'version': REST_API_CONFIG.get('API_VERSION', 'v1'),
                'description': (
                    'Universal binary repository manager supporting '
                    'Maven, npm, Docker, NuGet, PyPI, APT, and Raw '
                    'formats.  Provides hosted, proxy, and group '
                    'repository types with content-addressable blob '
                    'storage, three-tier RBAC, full-text search, and '
                    'scheduled maintenance.'
                ),
                'contact': {'name': 'Nexus Repository Admin'},
                'license': {
                    'name': 'Eclipse Public License 1.0',
                    'url': 'https://www.eclipse.org/legal/epl-v10.html',
                },
            },
            'servers': [
                {'url': '/', 'description': 'Current server'},
            ],
            'paths': {},
            'tags': list(_OPENAPI_TAGS),
            'components': {
                'securitySchemes': {
                    'BearerAuth': {
                        'type': 'http',
                        'scheme': 'bearer',
                        'bearerFormat': 'API Key or JWT',
                    },
                    'BasicAuth': {
                        'type': 'http',
                        'scheme': 'basic',
                    },
                },
            },
            'security': [
                {'BearerAuth': []},
                {'BasicAuth': []},
            ],
        }

        # ----- Auto-detected paths from Flask URL rules ----- #
        for rule in app.url_map.iter_rules():
            # Only document REST API and Docker V2 registry paths
            if not (
                rule.rule.startswith('/service/rest/')
                or rule.rule.startswith('/v2/')
                or rule.rule.startswith('/repository/')
            ):
                continue

            # Skip Flask's built-in static and debug endpoints
            endpoint_name = rule.endpoint or ''
            if any(
                endpoint_name.startswith(prefix)
                for prefix in _SKIP_ENDPOINT_PREFIXES
            ):
                continue

            path_entry = self._rule_to_path_entry(rule)
            if path_entry:
                # Convert Flask-style angle-bracket params to OpenAPI curly-brace
                openapi_path = self._flask_rule_to_openapi_path(rule.rule)
                # Merge with existing entry if path already registered
                if openapi_path in spec['paths']:
                    spec['paths'][openapi_path].update(path_entry)
                else:
                    spec['paths'][openapi_path] = path_entry

        # ----- Manually registered endpoints ----- #
        for entry in self._endpoint_registry:
            openapi_path = entry.get('path', '')
            if not openapi_path:
                continue

            existing = spec['paths'].get(openapi_path, {})
            tag = entry.get('tag', 'other')
            summary = entry.get('summary', '')

            for method in entry.get('methods', []):
                method_lower = method.lower()
                if method_lower in existing:
                    # Enrich existing operation with manual metadata
                    if summary:
                        existing[method_lower]['summary'] = summary
                    continue

                operation: Dict[str, Any] = {
                    'tags': [tag] if tag else [],
                    'summary': summary or f'{method} {openapi_path}',
                    'operationId': (
                        f'manual_{openapi_path.replace("/", "_")}'
                        f'_{method_lower}'
                    ),
                    'responses': {
                        '200': {'description': 'Successful operation'},
                        '401': {'description': 'Authentication required'},
                        '403': {'description': 'Insufficient permissions'},
                    },
                }

                # Include request body schema if provided
                request_schema = entry.get('request_schema')
                if request_schema is not None:
                    operation['requestBody'] = {
                        'required': True,
                        'content': {
                            'application/json': {
                                'schema': self._schema_to_openapi(
                                    request_schema
                                ),
                            },
                        },
                    }

                # Include response schema if provided
                response_schema = entry.get('response_schema')
                if response_schema is not None:
                    operation['responses']['200'] = {
                        'description': 'Successful operation',
                        'content': {
                            'application/json': {
                                'schema': self._schema_to_openapi(
                                    response_schema
                                ),
                            },
                        },
                    }

                existing[method_lower] = operation

            if existing:
                spec['paths'][openapi_path] = existing

        return spec

    # ------------------------------------------------------------------
    # Endpoint registration helper
    # ------------------------------------------------------------------

    def register_endpoint(
        self,
        path: str,
        methods: List[str],
        summary: str,
        tag: str,
        request_schema: Optional[Any] = None,
        response_schema: Optional[Any] = None,
    ) -> None:
        """Manually register an endpoint for OpenAPI documentation.

        Individual blueprint modules call this method to contribute detailed
        endpoint documentation that goes beyond what can be auto-detected
        from Flask URL rules alone.  Registered metadata is merged into the
        specification during ``generate_openapi_spec()``.

        Args:
            path: URL path pattern, e.g. ``'/service/rest/v1/repositories'``.
            methods: List of HTTP methods, e.g. ``['GET', 'POST']``.
            summary: Human-readable endpoint description.
            tag: OpenAPI tag name for grouping (one of the five feature
                categories or ``'other'``).
            request_schema: Optional marshmallow ``Schema`` class or
                instance describing the request body.
            response_schema: Optional marshmallow ``Schema`` class or
                instance describing the ``200`` response body.
        """
        self._endpoint_registry.append({
            'path': path,
            'methods': [m.upper() for m in methods],
            'summary': summary,
            'tag': tag,
            'request_schema': request_schema,
            'response_schema': response_schema,
        })
        self.logger.debug(
            'Endpoint registered for OpenAPI docs: %s %s',
            methods,
            path,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rule_to_path_entry(self, rule: Any) -> Optional[Dict[str, Any]]:
        """Convert a Flask ``Rule`` object to an OpenAPI path-item dict.

        Each HTTP method on the rule becomes a separate *operation* within
        the path item.  ``OPTIONS`` and ``HEAD`` methods are excluded by
        convention, as they are implicit in the OpenAPI specification.

        Args:
            rule: A ``werkzeug.routing.Rule`` instance from Flask's URL map.

        Returns:
            A dict mapping lowercase HTTP method names to OpenAPI operation
            objects, or ``None`` if no documentable methods are present.
        """
        entry: Dict[str, Any] = {}
        tag = self._determine_tag(rule)

        for method in sorted(rule.methods or set()):
            if method in _EXCLUDED_METHODS:
                continue

            operation: Dict[str, Any] = {
                'tags': [tag] if tag else [],
                'summary': f'{method} {rule.rule}',
                'operationId': self._make_operation_id(rule, method),
                'responses': {
                    '200': {'description': 'Successful operation'},
                    '401': {'description': 'Authentication required'},
                    '403': {'description': 'Insufficient permissions'},
                },
            }

            # Add path parameters derived from Flask route variables
            if rule.arguments:
                operation['parameters'] = [
                    {
                        'name': arg,
                        'in': 'path',
                        'required': True,
                        'schema': {'type': 'string'},
                    }
                    for arg in sorted(rule.arguments)
                ]

            # Add 404 for routes with path parameters
            if rule.arguments:
                operation['responses']['404'] = {
                    'description': 'Resource not found',
                }

            # Tag write operations with a request body hint
            if method in ('POST', 'PUT', 'PATCH'):
                operation['requestBody'] = {
                    'required': method != 'PATCH',
                    'content': {
                        'application/json': {
                            'schema': {'type': 'object'},
                        },
                    },
                }

            entry[method.lower()] = operation

        return entry if entry else None

    def _determine_tag(self, rule: Any) -> str:
        """Determine the OpenAPI tag for a route based on its endpoint name.

        Tags are inferred from the Flask endpoint string (which typically
        includes the blueprint name as a prefix, e.g. ``"security.list_users"``).
        The heuristic maps keywords in the endpoint name to the five standard
        feature category tags.

        Args:
            rule: A ``werkzeug.routing.Rule`` instance.

        Returns:
            One of the five feature-category tag names or ``'other'``.
        """
        endpoint = rule.endpoint or ''
        path = rule.rule or ''

        # Blueprint-based detection (highest priority)
        if endpoint.startswith('repositories.') or endpoint.startswith('repo.'):
            return 'repositories'
        if endpoint.startswith('storage.'):
            return 'storage'
        if endpoint.startswith('security.'):
            return 'security'
        if endpoint.startswith('admin.'):
            return 'admin'
        if endpoint.startswith('integration.'):
            return 'integration'

        # Keyword-based fallback detection
        if 'repositories' in endpoint or 'repo' in endpoint:
            return 'repositories'
        if 'storage' in endpoint or 'blobstore' in endpoint:
            return 'storage'
        if 'security' in endpoint:
            return 'security'
        if 'admin' in endpoint or 'status' in endpoint or 'tasks' in endpoint:
            return 'admin'
        if (
            'integration' in endpoint
            or 'script' in endpoint
            or 'webhook' in endpoint
        ):
            return 'integration'

        # Path-based fallback
        if '/v2/' in path:
            return 'repositories'
        if '/repository/' in path:
            return 'repositories'

        return 'other'

    @staticmethod
    def _flask_rule_to_openapi_path(flask_rule: str) -> str:
        """Convert a Flask URL rule to an OpenAPI path string.

        Flask uses ``<converter:name>`` syntax for path parameters while
        OpenAPI uses ``{name}`` syntax.  This method performs the conversion.

        Examples:
            ``'/service/rest/v1/repositories/<repo_id>'``
            → ``'/service/rest/v1/repositories/{repo_id}'``

        Args:
            flask_rule: The Flask URL rule string.

        Returns:
            The equivalent OpenAPI path string.
        """
        import re
        # Replace <type:name> or <name> with {name}
        return re.sub(r'<(?:[^:>]+:)?([^>]+)>', r'{\1}', flask_rule)

    @staticmethod
    def _make_operation_id(rule: Any, method: str) -> str:
        """Generate a unique OpenAPI ``operationId`` for a route/method pair.

        The operation ID is derived from the Flask endpoint name and the
        HTTP method, ensuring uniqueness across the specification.

        Args:
            rule: A ``werkzeug.routing.Rule`` instance.
            method: The HTTP method (e.g. ``'GET'``).

        Returns:
            A string suitable for use as an OpenAPI operationId.
        """
        endpoint = rule.endpoint or 'unknown'
        # Sanitise the endpoint name for operationId usage
        sanitised = endpoint.replace('.', '_').replace('-', '_')
        return f'{sanitised}_{method.lower()}'

    @staticmethod
    def _schema_to_openapi(schema: Any) -> Dict[str, Any]:
        """Convert a marshmallow Schema to a minimal OpenAPI schema dict.

        If the provided schema is a marshmallow ``Schema`` class or instance,
        an attempt is made to extract field information.  Otherwise, a
        generic ``object`` schema is returned.

        Args:
            schema: A marshmallow Schema class, instance, or ``None``.

        Returns:
            An OpenAPI schema object dictionary.
        """
        if schema is None:
            return {'type': 'object'}

        # Attempt marshmallow introspection
        try:
            # Handle both class and instance
            schema_instance = schema() if isinstance(schema, type) else schema
            if hasattr(schema_instance, 'fields'):
                properties: Dict[str, Any] = {}
                required_fields: List[str] = []
                for name, field in schema_instance.fields.items():
                    prop: Dict[str, Any] = {'type': 'string'}
                    if hasattr(field, 'metadata') and field.metadata:
                        description = field.metadata.get('description', '')
                        if description:
                            prop['description'] = description
                    if hasattr(field, 'required') and field.required:
                        required_fields.append(name)
                    properties[name] = prop

                result: Dict[str, Any] = {
                    'type': 'object',
                    'properties': properties,
                }
                if required_fields:
                    result['required'] = required_fields
                return result
        except Exception:
            pass

        return {'type': 'object'}


# ---------------------------------------------------------------------------
# Module-level singleton instance
# ---------------------------------------------------------------------------
rest_api_manager: RestApiManager = RestApiManager()
"""Module-level ``RestApiManager`` singleton.

This instance is initialised in ``src/app.py`` during the application factory
setup by calling ``rest_api_manager.init_app(app)``.  Other modules import
this instance to register additional endpoint documentation:

    from src.integration.rest_api import rest_api_manager
    rest_api_manager.register_endpoint(...)
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__ = ['RestApiManager', 'rest_api_manager', 'REST_API_CONFIG']
