"""
Elasticsearch integration for the Nexus Repository application.

This package provides content indexing and search functionality (Feature F-103),
replacing the Java Elasticsearch 2.4.3 client from the source system with
the elasticsearch-py 7.17.12 Python client library.

Modules:
    elasticsearch_client — Connection management and health checking
    index_manager — Index lifecycle, mappings, and document operations
    query_builder — Search query construction, pagination, and faceted search

Performance target: < 2 seconds for full-text search across 100K components.
"""

from __future__ import annotations

import logging

# Module-level logger for graceful degradation reporting.
_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API re-exports
#
# All imports are wrapped in a single try/except block so that the search
# package remains importable even when the ``elasticsearch`` library (or any
# of the submodule dependencies) is not installed.  When the imports fail,
# placeholder sentinels are created for every public name so that:
#
#   1. ``from src.app.search import init_elasticsearch`` never raises at
#      import time (allowing the Flask app to start without ES).
#   2. Callers that actually *invoke* a missing function will receive a clear
#      ``RuntimeError`` explaining that search features are unavailable.
#   3. ``__all__`` always contains the full list of expected names regardless
#      of whether ES dependencies are present.
# ---------------------------------------------------------------------------

_SEARCH_AVAILABLE: bool = False

try:
    from src.app.search.elasticsearch_client import (
        ElasticsearchClient,
        init_elasticsearch,
        get_es_client,
        get_es_wrapper,
        is_elasticsearch_available,
    )

    from src.app.search.index_manager import (
        IndexManager,
        initialize_indices,
        COMPONENT_INDEX,
        ASSET_INDEX,
    )

    from src.app.search.query_builder import (
        SearchQuery,
        QueryBuilder,
        execute_search,
        search_components,
        search_assets,
    )

    _SEARCH_AVAILABLE = True

except ImportError as _import_err:
    _logger.warning(
        "Elasticsearch dependencies not available. Search features disabled. "
        "Error: %s",
        _import_err,
    )

    # ------------------------------------------------------------------
    # Provide stub sentinels for every public name so that downstream
    # modules can import without crashing.  Each stub raises a clear
    # ``RuntimeError`` on invocation to prevent silent misbehaviour.
    # ------------------------------------------------------------------

    def _unavailable_factory(name: str):
        """Create a callable stub that raises ``RuntimeError`` when invoked."""

        def _stub(*args, **kwargs):
            raise RuntimeError(
                f"Search feature '{name}' is unavailable because the "
                "elasticsearch package is not installed. Install it with: "
                "pip install elasticsearch"
            )

        _stub.__name__ = name
        _stub.__qualname__ = name
        _stub.__doc__ = f"Stub for {name} — elasticsearch not installed."
        return _stub

    class _UnavailableMeta(type):
        """Metaclass that raises ``RuntimeError`` on instantiation."""

        def __call__(cls, *args, **kwargs):
            raise RuntimeError(
                f"Search class '{cls.__name__}' is unavailable because the "
                "elasticsearch package is not installed. Install it with: "
                "pip install elasticsearch"
            )

    class ElasticsearchClient(metaclass=_UnavailableMeta):  # type: ignore[no-redef]
        """Stub — elasticsearch not installed."""

        client = None
        is_connected = False
        url = ""

        def check_health(self): ...
        def ping(self): ...
        def get_cluster_info(self): ...
        def connect(self): ...
        def close(self): ...
        def reconnect(self): ...

    class IndexManager(metaclass=_UnavailableMeta):  # type: ignore[no-redef]
        """Stub — elasticsearch not installed."""

        def create_index(self, *a, **kw): ...
        def delete_index(self, *a, **kw): ...
        def index_exists(self, *a, **kw): ...
        def update_mapping(self, *a, **kw): ...
        def create_alias(self, *a, **kw): ...
        def delete_alias(self, *a, **kw): ...
        def swap_alias(self, *a, **kw): ...
        def rebuild_index(self, *a, **kw): ...
        def reindex(self, *a, **kw): ...
        def get_index_stats(self, *a, **kw): ...
        def refresh_index(self, *a, **kw): ...
        def index_document(self, *a, **kw): ...
        def bulk_index(self, *a, **kw): ...
        def delete_document(self, *a, **kw): ...
        def update_document(self, *a, **kw): ...
        def index_component(self, *a, **kw): ...
        def index_asset(self, *a, **kw): ...
        def remove_component(self, *a, **kw): ...
        def remove_asset(self, *a, **kw): ...

    class SearchQuery(metaclass=_UnavailableMeta):  # type: ignore[no-redef]
        """Stub — elasticsearch not installed."""

        q = ""
        keyword = None
        format = None
        repository = None
        namespace = None
        name = None
        version = None
        group = None
        tags = None
        page = 1
        page_size = 50
        sort_by = "_score"
        sort_order = "desc"
        include_facets = False

    class QueryBuilder(metaclass=_UnavailableMeta):  # type: ignore[no-redef]
        """Stub — elasticsearch not installed."""

        def build_query(self, *a, **kw): ...
        def build_pagination(self, *a, **kw): ...
        def build_sort(self, *a, **kw): ...
        def build_aggregations(self, *a, **kw): ...
        def build_highlight(self, *a, **kw): ...
        def build(self, *a, **kw): ...

    init_elasticsearch = _unavailable_factory("init_elasticsearch")  # type: ignore[assignment]
    get_es_client = _unavailable_factory("get_es_client")  # type: ignore[assignment]
    get_es_wrapper = _unavailable_factory("get_es_wrapper")  # type: ignore[assignment]
    is_elasticsearch_available = _unavailable_factory("is_elasticsearch_available")  # type: ignore[assignment]
    initialize_indices = _unavailable_factory("initialize_indices")  # type: ignore[assignment]
    execute_search = _unavailable_factory("execute_search")  # type: ignore[assignment]
    search_components = _unavailable_factory("search_components")  # type: ignore[assignment]
    search_assets = _unavailable_factory("search_assets")  # type: ignore[assignment]

    # Stub constants — downstream code that references these at import time
    # will receive sensible default strings rather than a NameError.
    COMPONENT_INDEX: str = "nexus_components"  # type: ignore[no-redef]
    ASSET_INDEX: str = "nexus_assets"  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Public API manifest
# ---------------------------------------------------------------------------

__all__ = [
    # elasticsearch_client
    "ElasticsearchClient",
    "init_elasticsearch",
    "get_es_client",
    "get_es_wrapper",
    "is_elasticsearch_available",
    # index_manager
    "IndexManager",
    "initialize_indices",
    "COMPONENT_INDEX",
    "ASSET_INDEX",
    # query_builder
    "SearchQuery",
    "QueryBuilder",
    "execute_search",
    "search_components",
    "search_assets",
]
