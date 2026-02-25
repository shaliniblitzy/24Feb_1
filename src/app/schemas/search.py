"""
Search Marshmallow Schemas.

This module defines Marshmallow 3.x schema classes for search query parameter
validation and search result serialization, supporting Feature F-103 (Content
Indexing and Search).  These schemas replace the search-related Jackson 2.16.1
DTOs from the original Java source system's Elasticsearch integration.

**Architecture Context:**

- ``SearchQuerySchema`` validates and normalizes incoming search API requests
  (query string, format/repository/namespace filters, pagination, sort).
- ``SearchResultAssetSchema`` serializes individual asset records within search
  results, with a computed ``download_url`` field.
- ``SearchResultItemSchema`` serializes individual component-level search
  results, each containing a nested list of assets and an Elasticsearch
  BM25 relevance ``score``.
- ``SearchResultSchema`` wraps a paginated list of search result items with
  ``total_count``, ``continuation_token`` (for Elasticsearch scroll/search_after),
  and ``facets`` (aggregation counts for UI filter refinement).

**Feature Coverage:**

+-------+------------------------------------+-----------------------------+
| ID    | Feature Name                       | Schema Role                 |
+=======+====================================+=============================+
| F-103 | Content Indexing and Search         | Query + result serialization|
+-------+------------------------------------+-----------------------------+
| F-101 | Multi-Format Repository Support    | 7-format OneOf validation   |
+-------+------------------------------------+-----------------------------+
| F-501 | REST API                           | Request/response contracts  |
+-------+------------------------------------+-----------------------------+

**Design Notes:**

- Schemas are **standalone** Marshmallow schemas — they do **NOT** import
  SQLAlchemy models directly.  Field definitions are informed by the
  ``Component`` and ``Asset`` models (design-time references only).
- Pagination follows a ``page`` / ``page_size`` pattern with an optional
  ``continuation_token`` for Elasticsearch scroll/search_after.
- All 7 repository formats (maven2, npm, docker, nuget, pypi, apt, raw)
  are validated via ``OneOf``.
- Sort options (name, version, group, repository, format) correspond to
  Elasticsearch field names.
- The ``facets`` dictionary carries Elasticsearch aggregation bucket counts
  for client-side filter refinement (e.g., format and repository facets).

**Usage Example:**

.. code-block:: python

    from src.app.schemas.search import SearchQuerySchema, SearchResultSchema

    query_schema = SearchQuerySchema()
    result_schema = SearchResultSchema()

    # Validate incoming search parameters
    params = query_schema.load(request.args)

    # Serialize search results for the API response
    response = result_schema.dump(search_results)
"""

from __future__ import annotations

import logging
from typing import Any

from marshmallow import (
    Schema,
    fields,
    validate,
    validates,
    ValidationError,
    pre_load,
    post_dump,
)
from marshmallow.validate import Length, Range, OneOf

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# The 7 repository formats from Feature F-101 (Multi-Format Repository Support).
SUPPORTED_FORMATS: list[str] = [
    "maven2",
    "npm",
    "docker",
    "nuget",
    "pypi",
    "apt",
    "raw",
]

# Valid sort field names — these map directly to Elasticsearch document fields.
VALID_SORT_FIELDS: list[str] = [
    "name",
    "version",
    "group",
    "repository",
    "format",
]

# Valid sort direction values.
VALID_SORT_DIRECTIONS: list[str] = [
    "asc",
    "desc",
]

# Maximum allowed length for the ``q`` search query string (prevents abuse).
MAX_QUERY_LENGTH: int = 500

# Default and maximum page sizes for pagination.
DEFAULT_PAGE_SIZE: int = 50
MAX_PAGE_SIZE: int = 200

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "SearchQuerySchema",
    "SearchResultAssetSchema",
    "SearchResultItemSchema",
    "SearchResultSchema",
]


# ===========================================================================
# SearchQuerySchema
# ===========================================================================


class SearchQuerySchema(Schema):
    """Marshmallow schema for validating and normalizing search query parameters.

    Accepts incoming search API request arguments (typically from query-string
    parameters) and applies validation rules for the full-text query, format
    and repository filters, component coordinate filters (group, name, version),
    pagination, and sorting.

    **Fields:**

    +-----------+--------+----------+--------------------------------------------+
    | Field     | Type   | Required | Description                                |
    +===========+========+==========+============================================+
    | q         | String | No       | Full-text search query string              |
    +-----------+--------+----------+--------------------------------------------+
    | format    | String | No       | Filter by repository format (OneOf 7)      |
    +-----------+--------+----------+--------------------------------------------+
    | repository| String | No       | Filter by repository name                  |
    +-----------+--------+----------+--------------------------------------------+
    | group     | String | No       | Filter by component namespace/group        |
    +-----------+--------+----------+--------------------------------------------+
    | name      | String | No       | Filter by component name                   |
    +-----------+--------+----------+--------------------------------------------+
    | version   | String | No       | Filter by exact component version          |
    +-----------+--------+----------+--------------------------------------------+
    | sort      | String | No       | Sort field (default: 'name')               |
    +-----------+--------+----------+--------------------------------------------+
    | direction | String | No       | Sort direction: 'asc' or 'desc'            |
    +-----------+--------+----------+--------------------------------------------+
    | page      | Int    | No       | Page number (≥ 1, default: 1)              |
    +-----------+--------+----------+--------------------------------------------+
    | page_size | Int    | No       | Results per page (1–200, default: 50)      |
    +-----------+--------+----------+--------------------------------------------+

    Example::

        schema = SearchQuerySchema()
        params = schema.load({
            "q": "commons-lang",
            "format": "maven2",
            "sort": "name",
            "direction": "asc",
            "page": 1,
            "page_size": 25,
        })
    """

    # -- Full-text query field -----------------------------------------------
    q = fields.String(
        required=False,
        load_default=None,
        allow_none=True,
        validate=Length(max=MAX_QUERY_LENGTH),
        metadata={
            "description": (
                "Full-text search query string.  Searches across component "
                "name, namespace, version, and asset path.  May be omitted "
                "for browse-style queries using only filters."
            ),
            "example": "commons-lang",
        },
    )

    # -- Filter fields -------------------------------------------------------
    format = fields.String(
        required=False,
        load_default=None,
        allow_none=True,
        validate=OneOf(
            SUPPORTED_FORMATS,
            error="Invalid repository format.  Must be one of: {choices}.",
        ),
        metadata={
            "description": (
                "Filter results to a specific repository format.  "
                "Supported values: maven2, npm, docker, nuget, pypi, apt, raw."
            ),
            "example": "maven2",
        },
    )

    repository = fields.String(
        required=False,
        load_default=None,
        allow_none=True,
        metadata={
            "description": (
                "Filter results to a specific repository by name."
            ),
            "example": "maven-central",
        },
    )

    group = fields.String(
        required=False,
        load_default=None,
        allow_none=True,
        metadata={
            "description": (
                "Filter by component namespace/group.  "
                "Maven: groupId, npm: scope, Docker: namespace."
            ),
            "example": "org.apache.commons",
        },
    )

    name = fields.String(
        required=False,
        load_default=None,
        allow_none=True,
        metadata={
            "description": (
                "Filter by component name.  "
                "Maven: artifactId, npm: package name, Docker: image name."
            ),
            "example": "commons-lang3",
        },
    )

    version = fields.String(
        required=False,
        load_default=None,
        allow_none=True,
        metadata={
            "description": "Filter by exact component version.",
            "example": "3.14.0",
        },
    )

    # -- Sort fields ---------------------------------------------------------
    sort = fields.String(
        required=False,
        load_default="name",
        validate=OneOf(
            VALID_SORT_FIELDS,
            error="Invalid sort field.  Must be one of: {choices}.",
        ),
        metadata={
            "description": (
                "Sort field for search results.  "
                "Options: name, version, group, repository, format."
            ),
            "example": "name",
        },
    )

    direction = fields.String(
        required=False,
        load_default="asc",
        validate=OneOf(
            VALID_SORT_DIRECTIONS,
            error="Invalid sort direction.  Must be 'asc' or 'desc'.",
        ),
        metadata={
            "description": "Sort direction: 'asc' (ascending) or 'desc' (descending).",
            "example": "asc",
        },
    )

    # -- Pagination fields ---------------------------------------------------
    page = fields.Integer(
        required=False,
        load_default=1,
        validate=Range(min=1, error="Page number must be at least 1."),
        metadata={
            "description": "Page number for paginated results (1-based).",
            "example": 1,
        },
    )

    page_size = fields.Integer(
        required=False,
        load_default=DEFAULT_PAGE_SIZE,
        validate=Range(
            min=1,
            max=MAX_PAGE_SIZE,
            error=f"Page size must be between 1 and {MAX_PAGE_SIZE}.",
        ),
        metadata={
            "description": (
                f"Number of results per page (1–{MAX_PAGE_SIZE}, default {DEFAULT_PAGE_SIZE})."
            ),
            "example": 50,
        },
    )

    # -- Field-Level Validators ----------------------------------------------

    @validates("q")
    def validate_q(self, value: str | None) -> None:
        """Validate the full-text search query string.

        Rules:
        - If provided, must be at least 1 character (no empty-string searches).
        - Maximum length of 500 characters to prevent abuse and Elasticsearch
          query clause explosion.

        Args:
            value: The raw ``q`` parameter value, or ``None``.

        Raises:
            ValidationError: If the value violates length constraints.
        """
        if value is None:
            # None / missing is perfectly valid — browse-style query.
            return

        if not isinstance(value, str):
            raise ValidationError("Search query must be a string.")

        if len(value) == 0:
            raise ValidationError(
                "Search query must be at least 1 character.  "
                "Omit the 'q' parameter for unfiltered browsing."
            )

        if len(value) > MAX_QUERY_LENGTH:
            raise ValidationError(
                f"Search query must not exceed {MAX_QUERY_LENGTH} characters.  "
                f"Received {len(value)} characters."
            )

    # -- Pre-Load Hook -------------------------------------------------------

    @pre_load
    def normalize_params(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Normalize incoming query parameters before validation.

        Transformations applied:
        1. Strip leading/trailing whitespace from the ``q`` parameter.
        2. Lowercase the ``sort`` and ``direction`` parameters for
           case-insensitive matching.
        3. Strip whitespace from filter string fields.

        Args:
            data: The raw deserialized input dictionary.

        Returns:
            The normalized input dictionary.
        """
        if not isinstance(data, dict):
            return data

        # Strip whitespace from search query
        if "q" in data and isinstance(data["q"], str):
            stripped = data["q"].strip()
            # Convert empty-after-strip to None so it's treated as omitted
            data["q"] = stripped if stripped else None

        # Normalize sort field (case-insensitive)
        if "sort" in data and isinstance(data["sort"], str):
            data["sort"] = data["sort"].strip().lower()

        # Normalize direction (case-insensitive)
        if "direction" in data and isinstance(data["direction"], str):
            data["direction"] = data["direction"].strip().lower()

        # Strip whitespace from filter fields
        for field_name in ("format", "repository", "group", "name", "version"):
            if field_name in data and isinstance(data[field_name], str):
                stripped = data[field_name].strip()
                data[field_name] = stripped if stripped else None

        return data


# ===========================================================================
# SearchResultAssetSchema
# ===========================================================================


class SearchResultAssetSchema(Schema):
    """Marshmallow schema for asset details within search results.

    Serializes individual asset records returned as part of a component-level
    search result.  Each asset represents a physical file stored in the
    repository's BlobStore.

    **Design Reference:**

    Field definitions are informed by the ``Asset`` SQLAlchemy model
    (``src/app/models/asset.py``), specifically the columns:

    - ``path`` (String 2048)
    - ``content_type`` (String 255, nullable)
    - ``checksum_sha1`` (String 40, nullable)
    - ``checksum_sha256`` (String 64, nullable)
    - ``size`` (BigInteger, nullable)
    - ``last_downloaded`` (DateTime, nullable)

    The ``download_url`` field is a **computed** / ``dump_only`` field that
    generates the artifact download link for API consumers.

    Example serialized output::

        {
            "path": "/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar",
            "content_type": "application/java-archive",
            "checksum_sha1": "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3",
            "checksum_sha256": "9f86d081884c7d659a2feaa0c55ad015...",
            "size": 1048576,
            "last_downloaded": "2024-12-15T10:30:00Z",
            "download_url": "/repository/maven-central/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar"
        }
    """

    path = fields.String(
        required=True,
        metadata={
            "description": "Asset path within the repository.",
            "example": "/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar",
        },
    )

    content_type = fields.String(
        allow_none=True,
        load_default=None,
        metadata={
            "description": "MIME type of the stored file.",
            "example": "application/java-archive",
        },
    )

    checksum_sha1 = fields.String(
        allow_none=True,
        load_default=None,
        metadata={
            "description": "SHA-1 hash of the asset content (40-char hex).",
            "example": "a94a8fe5ccb19ba61c4c0873d391e987982fbbd3",
        },
    )

    checksum_sha256 = fields.String(
        allow_none=True,
        load_default=None,
        metadata={
            "description": "SHA-256 hash of the asset content (64-char hex).",
            "example": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        },
    )

    size = fields.Integer(
        allow_none=True,
        load_default=None,
        metadata={
            "description": "File size in bytes.",
            "example": 1048576,
        },
    )

    last_downloaded = fields.DateTime(
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Timestamp of the most recent download (ISO 8601).  "
                "Critical for cleanup policy (F-204) evaluation."
            ),
            "example": "2024-12-15T10:30:00+00:00",
        },
    )

    download_url = fields.String(
        dump_only=True,
        metadata={
            "description": (
                "Computed artifact download URL.  Generated by the search "
                "service — not stored in the database."
            ),
            "example": "/repository/maven-central/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar",
        },
    )


# ===========================================================================
# SearchResultItemSchema
# ===========================================================================


class SearchResultItemSchema(Schema):
    """Marshmallow schema for individual search result items.

    Each item represents a component (logical package/artifact) returned by
    the Elasticsearch search service.  The item includes the component's
    coordinates (repository, group/namespace, name, version) and a nested
    list of associated assets.

    **Design Reference:**

    Field definitions are informed by the ``Component`` SQLAlchemy model
    (``src/app/models/component.py``), specifically the columns:

    - ``id`` (Integer, PK)
    - ``repository_name`` (String 200, FK) → serialized as ``repository``
    - ``namespace`` (String 512, nullable) → serialized as ``group``
    - ``name`` (String 512, NOT NULL)
    - ``version`` (String 256, nullable)

    The ``score`` field carries the Elasticsearch BM25 relevance score.

    Example serialized output::

        {
            "id": 42,
            "repository": "maven-central",
            "format": "maven2",
            "group": "org.apache.commons",
            "name": "commons-lang3",
            "version": "3.14.0",
            "assets": [ ... ],
            "score": 12.5
        }
    """

    id = fields.Integer(
        required=True,
        metadata={
            "description": "Component or asset ID.",
            "example": 42,
        },
    )

    repository = fields.String(
        required=True,
        metadata={
            "description": "Repository name containing this component.",
            "example": "maven-central",
        },
    )

    format = fields.String(
        required=True,
        metadata={
            "description": "Repository format (e.g., maven2, npm, docker).",
            "example": "maven2",
        },
    )

    group = fields.String(
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Component namespace/group.  "
                "Maven: groupId, npm: scope, Docker: namespace."
            ),
            "example": "org.apache.commons",
        },
    )

    name = fields.String(
        required=True,
        metadata={
            "description": (
                "Component name.  "
                "Maven: artifactId, npm: package name, Docker: image name."
            ),
            "example": "commons-lang3",
        },
    )

    version = fields.String(
        allow_none=True,
        load_default=None,
        metadata={
            "description": "Component version string.",
            "example": "3.14.0",
        },
    )

    assets = fields.List(
        fields.Nested(SearchResultAssetSchema),
        load_default=[],
        metadata={
            "description": "List of assets (files) associated with this component.",
        },
    )

    score = fields.Float(
        dump_only=True,
        load_default=None,
        allow_none=True,
        metadata={
            "description": (
                "Elasticsearch BM25 relevance score.  Higher values "
                "indicate stronger matches to the search query."
            ),
            "example": 12.5,
        },
    )

    # -- Post-Dump Hook ------------------------------------------------------

    @post_dump
    def remove_null_fields(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Remove keys with ``None`` values from the serialized output.

        This produces cleaner JSON responses by omitting null fields rather
        than explicitly including ``"field": null`` entries.  Fields like
        ``group``, ``version``, and ``score`` are commonly null and benefit
        from this cleanup.

        Args:
            data: The serialized dictionary.

        Returns:
            The dictionary with null-valued keys removed.
        """
        return {key: value for key, value in data.items() if value is not None}


# ===========================================================================
# SearchResultSchema (Paginated Wrapper)
# ===========================================================================


class SearchResultSchema(Schema):
    """Marshmallow schema for paginated search responses.

    Wraps a list of :class:`SearchResultItemSchema` items with pagination
    metadata, an optional Elasticsearch continuation token, aggregation facets,
    and the current sort configuration.

    **Facets:**

    The ``facets`` dictionary carries Elasticsearch aggregation bucket counts,
    enabling client-side filter refinement.  Example:

    .. code-block:: json

        {
            "format": {"maven2": 150, "npm": 75, "docker": 30},
            "repository": {"maven-central": 200, "npm-proxy": 55}
        }

    **Continuation Token:**

    For large result sets, Elasticsearch's ``search_after`` mechanism provides
    an opaque ``continuation_token`` that the client can send on the next
    request to efficiently retrieve the subsequent page without deep pagination.

    Example serialized output::

        {
            "items": [ ... ],
            "total_count": 1234,
            "page": 1,
            "page_size": 50,
            "continuation_token": "eyJzb3J0IjpbImNvbW1vbnMtbGFuZzMiXX0=",
            "facets": {
                "format": {"maven2": 900, "npm": 200, "docker": 134},
                "repository": {"maven-central": 800, "npm-proxy": 434}
            },
            "sort": "name",
            "direction": "asc"
        }
    """

    items = fields.List(
        fields.Nested(SearchResultItemSchema),
        load_default=[],
        metadata={
            "description": "List of search result items for the current page.",
        },
    )

    total_count = fields.Integer(
        required=True,
        metadata={
            "description": "Total number of matching results across all pages.",
            "example": 1234,
        },
    )

    page = fields.Integer(
        required=True,
        metadata={
            "description": "Current page number (1-based).",
            "example": 1,
        },
    )

    page_size = fields.Integer(
        required=True,
        metadata={
            "description": "Number of items per page.",
            "example": 50,
        },
    )

    continuation_token = fields.String(
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Opaque token for retrieving the next page of results.  "
                "Based on Elasticsearch search_after / scroll mechanism."
            ),
            "example": "eyJzb3J0IjpbImNvbW1vbnMtbGFuZzMiXX0=",
        },
    )

    facets = fields.Dict(
        keys=fields.String(),
        values=fields.Dict(keys=fields.String(), values=fields.Integer()),
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Elasticsearch aggregation facets for UI filter refinement.  "
                "Keys are facet names (e.g., 'format', 'repository'), "
                "values are dictionaries mapping bucket names to counts."
            ),
            "example": {
                "format": {"maven2": 150, "npm": 75, "docker": 30},
                "repository": {"maven-central": 200, "npm-proxy": 55},
            },
        },
    )

    sort = fields.String(
        load_default="name",
        metadata={
            "description": "Current sort field applied to results.",
            "example": "name",
        },
    )

    direction = fields.String(
        load_default="asc",
        metadata={
            "description": "Current sort direction: 'asc' or 'desc'.",
            "example": "asc",
        },
    )


# ---------------------------------------------------------------------------
# Module Load Diagnostic
# ---------------------------------------------------------------------------
logger.debug(
    "Search schema module loaded — exports: %s",
    ", ".join(__all__),
)
