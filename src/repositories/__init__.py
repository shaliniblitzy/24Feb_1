"""
Repository Management Module — Flask Blueprint (F-101 through F-104)

Implements the core Repository Management feature category:
- F-101: Multi-format artifact support (Maven, npm, Docker, NuGet, PyPI, APT, Raw)
- F-102: Three repository types (Hosted, Proxy, Group)
- F-103: Content indexing and search
- F-104: Browse tree navigation

Replaces the Java-based repository management module with OSGi bundles
from the original Sonatype Nexus stack.

Blueprint routes:
- /service/rest/v1/repositories/** — Repository CRUD and management
- /service/rest/v1/components/** — Component listing and details
- /service/rest/v1/assets/** — Asset listing, details, and download
- /service/rest/v1/search/** — Full-text component/asset search
- /service/rest/v1/browse/** — Hierarchical content tree navigation
- /repository/{name}/** — Format-native protocol endpoints (Maven, npm, PyPI, APT, NuGet, Raw)
- /v2/{name}/** — Docker Registry HTTP API V2 endpoints
"""

from flask import Blueprint

# Create the repository management blueprint.
# Blueprint name 'repositories' must be unique across all blueprints in the application.
# No url_prefix is set here because this blueprint serves multiple URL patterns:
#   - REST API routes: /service/rest/v1/...
#   - Format protocol routes: /repository/...
#   - Docker V2 routes: /v2/...
# Individual route functions define their own full URL paths.
repo_bp = Blueprint('repositories', __name__)

# Explicit module exports for use by src/app.py:
#   from src.repositories import repo_bp
#   app.register_blueprint(repo_bp)
__all__ = ['repo_bp']

# Import routes to register them on the blueprint.
# This MUST happen AFTER repo_bp is created because routes.py
# imports repo_bp to use @repo_bp.route() decorator.
# The import triggers route registration as a side-effect.
try:
    from src.repositories import routes  # noqa: F401, E402
except ImportError:
    # Routes module or its dependencies may not yet be available during
    # incremental project build-up. Routes will be registered once all
    # dependent modules (models, services, search, browse, security
    # decorators, etc.) are created and importable.
    pass
