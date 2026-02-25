"""
Search query construction module for the Nexus Repository Flask application.

Implements Feature F-103 (Content Indexing and Search) by building
Elasticsearch 7.x query DSL for full-text search across component and asset
metadata.  Replaces the Java Elasticsearch 2.4.3 query building logic from the
original Sonatype Nexus Repository system.

Key capabilities:
- Multi-field full-text search with boosted relevance ranking
- Boolean keyword queries (AND, OR, NOT) via ``query_string``
- Faceted search with aggregations on format, repository, and namespace
- Configurable pagination with bounds enforcement (max 500 results per page)
- Multiple sort strategies (relevance, name, date)
- Highlighted result snippets for matched fields
- Graceful degradation when Elasticsearch is unavailable

Performance target: < 2 seconds for full-text search across 100K components
(AAP Section 0.7.3).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from elasticsearch import Elasticsearch

from src.app.search.elasticsearch_client import get_es_client

# ---------------------------------------------------------------------------
# Module-level logger — replaces SLF4J 1.7.36 + Logback 1.2.13
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE: int = 50
"""Default number of results returned per search page."""

MAX_PAGE_SIZE: int = 500
"""Hard upper limit on results per page to prevent excessive memory usage and
ensure the < 2-second performance target for 100K component datasets."""

DEFAULT_SORT_FIELD: str = "_score"
"""Default sort field — relevance score — so that the most relevant results
appear first."""

SEARCHABLE_FIELDS: list[str] = [
    "name",
    "name.keyword",
    "namespace",
    "namespace.keyword",
    "version",
    "format",
    "repository_name",
    "tags",
    "group",  # Maven groupId alias
]
"""Fields included in multi-field full-text search queries.  The list covers
the primary component and asset metadata attributes that end-users are most
likely to search by."""

FACET_FIELDS: list[str] = ["format", "repository_name", "namespace"]
"""Fields used for faceted aggregation queries, enabling drill-down navigation
in search results by format type, repository, and namespace."""

# Internal constant for recognised sort-by fields.  Used by SearchQuery
# validation to reject unrecognised sort field names.
_VALID_SORT_FIELDS: set[str] = {
    "_score",
    "name",
    "date",
    "updated_at",
    "version",
    "repository_name",
    "format",
    "namespace",
}


# ═══════════════════════════════════════════════════════════════════════════
# SearchQuery — Parameter container
# ═══════════════════════════════════════════════════════════════════════════


class SearchQuery:
    """Encapsulates all parameters for an Elasticsearch search request.

    Provides input validation on construction — page bounds are clamped, sort
    order is normalised, and unrecognised sort fields fall back to
    ``DEFAULT_SORT_FIELD``.  Instances are plain data containers that are
    passed to :class:`QueryBuilder` for query DSL generation.

    Parameters
    ----------
    q:
        Free-text query string for multi-field search (``multi_match``).
    keyword:
        Advanced keyword query supporting boolean operators (AND, OR, NOT)
        via Elasticsearch's ``query_string`` syntax.
    format:
        Filter by repository format (e.g. ``"maven2"``, ``"npm"``).
    repository:
        Filter by repository name.
    namespace:
        Filter by component namespace (e.g. Maven groupId).
    name:
        Filter by component name (exact match).
    version:
        Filter by component version (exact match).
    group:
        Filter by group — alias for Maven ``groupId``.
    tags:
        Filter by one or more tags (any tag matches).
    page:
        1-based page number (clamped to a minimum of 1).
    page_size:
        Number of results per page (clamped to ``[1, MAX_PAGE_SIZE]``).
    sort_by:
        Field to sort by.  Recognised values: ``"_score"``, ``"name"``,
        ``"date"``, ``"updated_at"``, ``"version"``, ``"repository_name"``,
        ``"format"``, ``"namespace"``.  Unrecognised values fall back to
        ``DEFAULT_SORT_FIELD``.
    sort_order:
        Sort direction — ``"asc"`` or ``"desc"`` (defaults to ``"desc"``).
    include_facets:
        When ``True``, the query includes aggregation clauses for faceted
        drill-down.
    """

    __slots__ = (
        "q",
        "keyword",
        "format",
        "repository",
        "namespace",
        "name",
        "version",
        "group",
        "tags",
        "page",
        "page_size",
        "sort_by",
        "sort_order",
        "include_facets",
    )

    def __init__(
        self,
        q: str = "",
        keyword: Optional[str] = None,
        format: Optional[str] = None,
        repository: Optional[str] = None,
        namespace: Optional[str] = None,
        name: Optional[str] = None,
        version: Optional[str] = None,
        group: Optional[str] = None,
        tags: Optional[list[str]] = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        sort_by: str = DEFAULT_SORT_FIELD,
        sort_order: str = "desc",
        include_facets: bool = False,
    ) -> None:
        # Store free-text and keyword parameters as-is.
        self.q: str = q if q is not None else ""
        self.keyword: Optional[str] = keyword

        # Filter fields — stored verbatim; None means "no filter".
        self.format: Optional[str] = format
        self.repository: Optional[str] = repository
        self.namespace: Optional[str] = namespace
        self.name: Optional[str] = name
        self.version: Optional[str] = version
        self.group: Optional[str] = group
        self.tags: Optional[list[str]] = tags

        # Pagination — clamp to safe bounds.
        self.page: int = max(1, page)
        self.page_size: int = max(1, min(page_size, MAX_PAGE_SIZE))

        # Sort — validate field and direction.
        if sort_by in _VALID_SORT_FIELDS:
            self.sort_by: str = sort_by
        else:
            logger.warning(
                "Unrecognised sort field '%s'; falling back to '%s'",
                sort_by,
                DEFAULT_SORT_FIELD,
            )
            self.sort_by = DEFAULT_SORT_FIELD

        if sort_order.lower() in ("asc", "desc"):
            self.sort_order: str = sort_order.lower()
        else:
            logger.warning(
                "Invalid sort order '%s'; falling back to 'desc'",
                sort_order,
            )
            self.sort_order = "desc"

        self.include_facets: bool = include_facets

    def __repr__(self) -> str:  # pragma: no cover — convenience
        parts: list[str] = []
        if self.q:
            parts.append(f"q={self.q!r}")
        if self.keyword:
            parts.append(f"keyword={self.keyword!r}")
        if self.format:
            parts.append(f"format={self.format!r}")
        if self.repository:
            parts.append(f"repository={self.repository!r}")
        parts.append(f"page={self.page}")
        parts.append(f"page_size={self.page_size}")
        parts.append(f"sort_by={self.sort_by!r}")
        return f"SearchQuery({', '.join(parts)})"


# ═══════════════════════════════════════════════════════════════════════════
# QueryBuilder — Elasticsearch query DSL generator
# ═══════════════════════════════════════════════════════════════════════════


class QueryBuilder:
    """Constructs Elasticsearch 7.x query DSL dictionaries from
    :class:`SearchQuery` parameter objects.

    The builder follows the Elasticsearch query/filter/aggregation model:

    * **must** clauses — full-text scoring queries (multi_match, query_string)
    * **filter** clauses — exact-match filters (cached, no scoring overhead)
    * **aggregations** — faceted search buckets for drill-down navigation
    * **highlight** — snippet extraction for matched fields
    * **sort** — configurable relevance or field-based ordering
    * **pagination** — ``from`` / ``size`` based on page and page_size

    The design prioritises performance by placing exact-match conditions in
    ``filter`` context (leveraging Elasticsearch's filter cache) and only using
    scoring queries when free-text or keyword search is requested.

    Parameters
    ----------
    index_name:
        The Elasticsearch index to query (default ``"components"``).
    """

    def __init__(self, index_name: str = "components") -> None:
        self.index_name: str = index_name
        self._client: Optional[Elasticsearch] = get_es_client()

    # ------------------------------------------------------------------
    # build_query — Core query DSL
    # ------------------------------------------------------------------

    def build_query(self, search_query: SearchQuery) -> dict[str, Any]:
        """Build the Elasticsearch ``query`` clause from *search_query*.

        The generated structure is a ``bool`` query with:

        * ``must`` — full-text queries (multi_match, query_string)
        * ``filter`` — exact-match term filters (format, repository, etc.)

        When no scoring queries are present (only filters), a ``match_all``
        is used as the base to ensure every filtered document receives a
        score of 1.0.

        Parameters
        ----------
        search_query:
            The search parameters to translate into query DSL.

        Returns
        -------
        dict[str, Any]
            A dictionary suitable for the ``"query"`` key of an
            Elasticsearch request body.
        """
        must_clauses: list[dict[str, Any]] = []
        filter_clauses: list[dict[str, Any]] = []

        # --- Free-text multi-field search ---
        if search_query.q and search_query.q.strip():
            must_clauses.append(
                {
                    "multi_match": {
                        "query": search_query.q.strip(),
                        "fields": [
                            "name^3",
                            "namespace^2",
                            "version",
                            "format",
                            "repository_name",
                            "tags",
                        ],
                        "type": "best_fields",
                        "fuzziness": "AUTO",
                        "operator": "and",
                    }
                }
            )

        # --- Advanced keyword search with boolean operators ---
        if search_query.keyword and search_query.keyword.strip():
            must_clauses.append(
                {
                    "query_string": {
                        "query": search_query.keyword.strip(),
                        "default_field": "name",
                        "default_operator": "AND",
                        "analyze_wildcard": True,
                    }
                }
            )

        # --- Exact-match filters (placed in filter context for caching) ---
        _filter_map: list[tuple[str, Optional[str]]] = [
            ("format", search_query.format),
            ("repository_name", search_query.repository),
            ("namespace", search_query.namespace),
            ("name.keyword", search_query.name),
            ("version", search_query.version),
            ("group", search_query.group),
        ]
        for field_name, value in _filter_map:
            if value is not None:
                filter_clauses.append({"term": {field_name: value}})

        # --- Tags filter (terms query for multi-value match) ---
        if search_query.tags:
            filter_clauses.append({"terms": {"tags": search_query.tags}})

        # --- Assemble the bool query ---
        bool_query: dict[str, Any] = {}

        if must_clauses:
            bool_query["must"] = must_clauses
        else:
            # When there are no scoring queries, use match_all as the base.
            bool_query["must"] = [{"match_all": {}}]

        if filter_clauses:
            bool_query["filter"] = filter_clauses

        return {"bool": bool_query}

    # ------------------------------------------------------------------
    # build_pagination — from / size calculation
    # ------------------------------------------------------------------

    def build_pagination(self, search_query: SearchQuery) -> dict[str, int]:
        """Calculate the ``from`` (offset) and ``size`` pagination parameters.

        Parameters
        ----------
        search_query:
            The search parameters containing ``page`` and ``page_size``.

        Returns
        -------
        dict[str, int]
            A dictionary with ``"from"`` and ``"size"`` keys.
        """
        offset: int = (search_query.page - 1) * search_query.page_size
        return {
            "from": offset,
            "size": search_query.page_size,
        }

    # ------------------------------------------------------------------
    # build_sort — Sort clause
    # ------------------------------------------------------------------

    def build_sort(self, search_query: SearchQuery) -> list[dict[str, Any]]:
        """Build the ``sort`` clause for the Elasticsearch request.

        The sort strategy varies by field:

        * ``_score`` — pure relevance ranking (always descending)
        * ``name`` — alphabetical sort on the keyword sub-field, with
          ``_score`` as a secondary tie-breaker
        * ``date`` / ``updated_at`` — chronological ordering with ``_score``
          as a secondary tie-breaker
        * Other recognised fields — single-field sort with ``_score``
          tie-breaker

        Parameters
        ----------
        search_query:
            The search parameters containing ``sort_by`` and ``sort_order``.

        Returns
        -------
        list[dict[str, Any]]
            A list of sort clause dictionaries.
        """
        sort_clauses: list[dict[str, Any]] = []
        order: str = search_query.sort_order

        if search_query.sort_by == "_score":
            # Relevance-only sort — always descending for highest-first.
            sort_clauses.append({"_score": {"order": "desc"}})
        elif search_query.sort_by == "name":
            # Alphabetical sort on the keyword sub-field.
            sort_clauses.append({"name.keyword": {"order": order}})
            sort_clauses.append({"_score": {"order": "desc"}})
        elif search_query.sort_by in ("date", "updated_at"):
            # Chronological sort on the updated_at timestamp.
            sort_clauses.append({"updated_at": {"order": order}})
            sort_clauses.append({"_score": {"order": "desc"}})
        else:
            # Generic field sort with relevance tie-breaker.
            sort_clauses.append({search_query.sort_by: {"order": order}})
            sort_clauses.append({"_score": {"order": "desc"}})

        return sort_clauses

    # ------------------------------------------------------------------
    # build_aggregations — Faceted search
    # ------------------------------------------------------------------

    def build_aggregations(
        self, search_query: SearchQuery
    ) -> dict[str, Any]:
        """Build aggregation clauses for faceted drill-down navigation.

        Aggregations are only included when ``search_query.include_facets``
        is ``True``.  Three bucket aggregations are generated corresponding
        to the :data:`FACET_FIELDS` constant:

        * ``format_facet`` — top 20 repository formats
        * ``repository_facet`` — top 50 repository names
        * ``namespace_facet`` — top 50 namespaces

        Parameters
        ----------
        search_query:
            The search parameters (used for future extensibility — e.g.
            dynamic facet field selection).

        Returns
        -------
        dict[str, Any]
            A dictionary of aggregation definitions, suitable for the
            ``"aggs"`` key of an Elasticsearch request body.
        """
        return {
            "format_facet": {
                "terms": {"field": "format", "size": 20},
            },
            "repository_facet": {
                "terms": {"field": "repository_name", "size": 50},
            },
            "namespace_facet": {
                "terms": {"field": "namespace.keyword", "size": 50},
            },
        }

    # ------------------------------------------------------------------
    # build_highlight — Snippet extraction
    # ------------------------------------------------------------------

    def build_highlight(self) -> dict[str, Any]:
        """Build the highlight configuration for matched snippet extraction.

        Highlights are returned for the ``name``, ``namespace``, and
        ``version`` fields using ``<em>`` / ``</em>`` tags.
        ``number_of_fragments`` is set to ``0`` so that the *entire* field
        value is returned with highlighting rather than a truncated
        fragment.

        Returns
        -------
        dict[str, Any]
            A dictionary suitable for the ``"highlight"`` key of an
            Elasticsearch request body.
        """
        return {
            "fields": {
                "name": {"number_of_fragments": 0},
                "namespace": {"number_of_fragments": 0},
                "version": {"number_of_fragments": 0},
            },
            "pre_tags": ["<em>"],
            "post_tags": ["</em>"],
        }

    # ------------------------------------------------------------------
    # build — Complete request body assembly
    # ------------------------------------------------------------------

    def build(self, search_query: SearchQuery) -> dict[str, Any]:
        """Assemble a complete Elasticsearch request body from *search_query*.

        Combines the outputs of :meth:`build_query`, :meth:`build_pagination`,
        :meth:`build_sort`, :meth:`build_highlight`, and optionally
        :meth:`build_aggregations` into a single dictionary ready for
        submission to ``client.search()``.

        Parameters
        ----------
        search_query:
            The search parameters to translate.

        Returns
        -------
        dict[str, Any]
            The complete Elasticsearch request body dictionary.
        """
        body: dict[str, Any] = {
            "query": self.build_query(search_query),
            **self.build_pagination(search_query),
            "sort": self.build_sort(search_query),
            "highlight": self.build_highlight(),
        }

        if search_query.include_facets:
            body["aggs"] = self.build_aggregations(search_query)

        return body


# ═══════════════════════════════════════════════════════════════════════════
# Module-level convenience functions
# ═══════════════════════════════════════════════════════════════════════════


def execute_search(
    search_query: SearchQuery, index_name: str = "components"
) -> dict[str, Any]:
    """Execute a search against Elasticsearch and return parsed results.

    This is the primary entry point for performing searches.  It constructs
    the query DSL via :class:`QueryBuilder`, obtains the ES client from
    :func:`get_es_client`, executes the query, and parses the response into
    a standardised result dictionary.

    **Graceful degradation:** when Elasticsearch is unavailable, an empty
    result set is returned with a warning log — the application continues
    to function without search capabilities.

    Parameters
    ----------
    search_query:
        Fully initialised :class:`SearchQuery` with search parameters.
    index_name:
        Elasticsearch index to search (default ``"components"``).

    Returns
    -------
    dict[str, Any]
        Parsed search results with the following keys:

        * ``items`` — list of matched documents (with ``_score``, ``_id``,
          and optional ``_highlight``)
        * ``total`` — total number of matching documents
        * ``page`` — current page number
        * ``page_size`` — results per page
        * ``total_pages`` — calculated total page count
        * ``facets`` — (optional) aggregation facets when requested
        * ``error`` — (optional) error description string on failure
    """
    builder = QueryBuilder(index_name=index_name)
    body: dict[str, Any] = builder.build(search_query)

    client: Optional[Elasticsearch] = get_es_client()

    if client is None:
        logger.warning(
            "Elasticsearch client not available, returning empty results"
        )
        return {
            "items": [],
            "total": 0,
            "page": search_query.page,
            "page_size": search_query.page_size,
            "total_pages": 0,
        }

    try:
        response: dict[str, Any] = client.search(
            index=index_name, body=body
        )
        return parse_search_response(response, search_query)
    except Exception as exc:
        logger.error("Search execution failed: %s", exc)
        return {
            "items": [],
            "total": 0,
            "page": search_query.page,
            "page_size": search_query.page_size,
            "total_pages": 0,
            "error": str(exc),
        }


def parse_search_response(
    response: dict[str, Any], search_query: SearchQuery
) -> dict[str, Any]:
    """Parse an Elasticsearch search response into a standardised dictionary.

    Handles the ES 7.x response format where ``hits.total`` is an object
    with ``value`` and ``relation`` keys, as well as the legacy integer
    format for backwards compatibility.

    Parameters
    ----------
    response:
        The raw Elasticsearch search response dictionary.
    search_query:
        The :class:`SearchQuery` that produced the response (used for
        pagination metadata).

    Returns
    -------
    dict[str, Any]
        Parsed result dictionary (see :func:`execute_search` for the schema).
    """
    hits: dict[str, Any] = response.get("hits", {})
    total_raw: Any = hits.get("total", 0)

    # ES 7.x returns {"value": N, "relation": "eq"/"gte"} for total.
    if isinstance(total_raw, dict):
        total_count: int = int(total_raw.get("value", 0))
    else:
        total_count = int(total_raw)

    items: list[dict[str, Any]] = []
    for hit in hits.get("hits", []):
        item: dict[str, Any] = dict(hit.get("_source", {}))
        item["_score"] = hit.get("_score")
        item["_id"] = hit.get("_id")
        if "highlight" in hit:
            item["_highlight"] = hit["highlight"]
        items.append(item)

    # Calculate total pages — protect against division by zero.
    if search_query.page_size > 0:
        total_pages: int = (
            (total_count + search_query.page_size - 1) // search_query.page_size
        )
    else:
        total_pages = 0

    result: dict[str, Any] = {
        "items": items,
        "total": total_count,
        "page": search_query.page,
        "page_size": search_query.page_size,
        "total_pages": total_pages,
    }

    # Append facets only when they were requested and present.
    if search_query.include_facets and "aggregations" in response:
        result["facets"] = parse_facets(response["aggregations"])

    return result


def parse_facets(aggregations: dict[str, Any]) -> dict[str, Any]:
    """Convert Elasticsearch aggregation buckets into a simplified facet map.

    Each aggregation key is expected to follow the ``<field>_facet`` naming
    convention (e.g. ``"format_facet"``).  The ``_facet`` suffix is stripped
    to produce a clean facet name in the output.

    Parameters
    ----------
    aggregations:
        The ``"aggregations"`` section of an Elasticsearch search response.

    Returns
    -------
    dict[str, Any]
        A dictionary mapping facet names to lists of
        ``{"value": <key>, "count": <doc_count>}`` entries.

    Example
    -------
    >>> parse_facets({
    ...     "format_facet": {
    ...         "buckets": [
    ...             {"key": "maven2", "doc_count": 120},
    ...             {"key": "npm", "doc_count": 85}
    ...         ]
    ...     }
    ... })
    {'format': [{'value': 'maven2', 'count': 120}, {'value': 'npm', 'count': 85}]}
    """
    facets: dict[str, list[dict[str, Any]]] = {}

    for key, agg_data in aggregations.items():
        facet_name: str = key.replace("_facet", "")
        buckets: list[dict[str, Any]] = agg_data.get("buckets", [])
        facets[facet_name] = [
            {"value": bucket.get("key", ""), "count": bucket.get("doc_count", 0)}
            for bucket in buckets
        ]

    return facets


def search_components(q: str = "", **kwargs: Any) -> dict[str, Any]:
    """Convenience function: search the ``components`` index.

    Creates a :class:`SearchQuery` from the provided arguments, executes it
    against the ``"components"`` index, and returns the parsed results.

    Parameters
    ----------
    q:
        Free-text search query.
    **kwargs:
        Additional keyword arguments forwarded to :class:`SearchQuery`
        (e.g. ``format``, ``repository``, ``page``, ``page_size``).

    Returns
    -------
    dict[str, Any]
        Parsed search results (see :func:`execute_search`).
    """
    search_query = SearchQuery(q=q, **kwargs)
    return execute_search(search_query, index_name="components")


def search_assets(q: str = "", **kwargs: Any) -> dict[str, Any]:
    """Convenience function: search the ``assets`` index.

    Creates a :class:`SearchQuery` from the provided arguments, executes it
    against the ``"assets"`` index, and returns the parsed results.

    Parameters
    ----------
    q:
        Free-text search query.
    **kwargs:
        Additional keyword arguments forwarded to :class:`SearchQuery`
        (e.g. ``format``, ``repository``, ``page``, ``page_size``).

    Returns
    -------
    dict[str, Any]
        Parsed search results (see :func:`execute_search`).
    """
    search_query = SearchQuery(q=q, **kwargs)
    return execute_search(search_query, index_name="assets")
