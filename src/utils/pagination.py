"""
Nexus Repository Manager — Pagination Helpers

Provides both offset-based (``paginate(query, page, per_page)``) and cursor-based
(continuation token) pagination for all REST API list endpoints.  Returns a
standardised :class:`PageResponse` dataclass with ``items``, ``total_count``,
``page``, ``per_page``, ``has_next``, ``has_prev``, and an optional
``continuation_token`` for keyset pagination.

Used by every blueprint that exposes list endpoints:

* ``src/repositories/routes.py``  — List repositories, components, assets
* ``src/security/routes.py``      — List users, roles, privileges
* ``src/admin/routes.py``         — List tasks, system config entries
* ``src/repositories/search.py``  — Search results pagination
* ``src/repositories/browse.py``  — Browse tree pagination

Design notes
------------
* **Pure utility** — zero imports from other ``src`` packages.  This module can
  be imported by any module without risk of circular imports.
* **Thread-safe** — every function is stateless and uses only its arguments,
  making it safe for concurrent use in a multi-worker Gunicorn deployment.
* **SQLAlchemy + list compatible** — ``paginate()`` auto-detects whether it
  receives a SQLAlchemy Query (or Select) object or a plain Python list, so it
  works identically in production code and unit tests.
* **Backward-compatible JSON** — ``PageResponse.to_dict()`` outputs camelCase
  keys to preserve the original Nexus REST API response format (AAP §0.7.3).
"""

from __future__ import annotations

import base64
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Generic, List, Optional, Tuple, TypeVar

# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# Generic type variable for potential future generic typing of page items
T = TypeVar("T")


# ---------------------------------------------------------------------------
# PageResponse dataclass
# ---------------------------------------------------------------------------
@dataclass
class PageResponse:
    """Standardised pagination response used by all REST API list endpoints.

    Matches the pagination pattern documented in the tech spec for backward
    compatibility with existing API consumers (build tools, CI/CD pipelines).

    Attributes:
        items:              List of items for the current page.
        total_count:        Total number of items across all pages.
        page:               Current page number (1-indexed).
        per_page:           Number of items per page.
        has_next:           Whether there are more pages after this one.
        has_prev:           Whether there are pages before this one.
        continuation_token: Opaque token for cursor-based pagination (optional).
    """

    items: List[Any]
    total_count: int
    page: int
    per_page: int
    has_next: bool
    has_prev: bool = False
    continuation_token: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to the API response format expected by consumers.

        Returns a ``dict`` suitable for ``flask.jsonify()`` with camelCase keys
        that match the original Nexus API contract::

            {
                "items": [...],
                "totalCount": 42,
                "page": 1,
                "perPage": 20,
                "hasNext": true,
                "hasPrev": false,
                "continuationToken": "eyJsYXN0X2lkIjo..."
            }

        Returns:
            Dictionary with camelCase keys ready for JSON serialisation.
        """
        result: Dict[str, Any] = {
            "items": self.items,
            "totalCount": self.total_count,
            "page": self.page,
            "perPage": self.per_page,
            "hasNext": self.has_next,
            "hasPrev": self.has_prev,
        }
        if self.continuation_token is not None:
            result["continuationToken"] = self.continuation_token
        return result


# ---------------------------------------------------------------------------
# Offset-based pagination
# ---------------------------------------------------------------------------
def paginate(
    query: Any,
    page: int = 1,
    per_page: int = 20,
    max_per_page: int = 100,
) -> PageResponse:
    """Paginate a SQLAlchemy query or plain list using offset-based pagination.

    Supports both SQLAlchemy ``Query`` / ``Select`` objects **and** plain Python
    lists so that production code and tests share the same pagination logic.

    Args:
        query:        SQLAlchemy ``Query`` object **or** a plain ``list`` of items.
        page:         Page number (1-indexed).  Defaults to ``1``.
        per_page:     Items per page.  Defaults to ``20``.
        max_per_page: Maximum allowed ``per_page`` value.  Defaults to ``100``.

    Returns:
        A :class:`PageResponse` populated with the items for the requested
        page together with total count and navigation flags.

    Raises:
        ValueError: If *page* is less than ``1``.
    """
    # --- Input validation ------------------------------------------------
    if page < 1:
        raise ValueError(f"page must be >= 1, got {page}")

    # Clamp per_page into [1, max_per_page] (defensive, don't error)
    if per_page < 1:
        logger.debug("per_page=%d clamped to 1", per_page)
        per_page = 1
    if per_page > max_per_page:
        logger.debug(
            "per_page=%d exceeds max_per_page=%d; clamping",
            per_page,
            max_per_page,
        )
        per_page = max_per_page

    offset = (page - 1) * per_page

    # --- Detect query type and execute -----------------------------------
    if _is_query_like(query):
        # SQLAlchemy Query / ScalarResult / legacy Query object
        total_count: int = query.count()
        items: List[Any] = query.offset(offset).limit(per_page).all()
        logger.debug(
            "paginate (query): page=%d per_page=%d offset=%d total=%d fetched=%d",
            page,
            per_page,
            offset,
            total_count,
            len(items),
        )
    elif isinstance(query, list):
        total_count = len(query)
        items = query[offset: offset + per_page]
        logger.debug(
            "paginate (list): page=%d per_page=%d offset=%d total=%d fetched=%d",
            page,
            per_page,
            offset,
            total_count,
            len(items),
        )
    else:
        # Fall back to trying the list-like protocol (e.g. tuple, deque)
        try:
            total_count = len(query)  # type: ignore[arg-type]
            items = list(query)[offset: offset + per_page]  # type: ignore[arg-type]
        except TypeError as exc:
            raise TypeError(
                f"query must be a SQLAlchemy Query or a list-like object, "
                f"got {type(query).__name__}"
            ) from exc

    # --- Compute navigation flags ----------------------------------------
    total_pages = math.ceil(total_count / per_page) if per_page > 0 else 0
    has_next = (page * per_page) < total_count
    has_prev = page > 1

    return PageResponse(
        items=items,
        total_count=total_count,
        page=page,
        per_page=per_page,
        has_next=has_next,
        has_prev=has_prev,
    )


# ---------------------------------------------------------------------------
# Continuation token helpers
# ---------------------------------------------------------------------------
def create_continuation_token(
    last_id: Any,
    sort_field: Optional[str] = None,
    sort_value: Optional[Any] = None,
) -> str:
    """Create an opaque continuation token for cursor-based pagination.

    Encodes the cursor position as a URL-safe base64-encoded JSON string.
    The token is opaque to API consumers — they simply pass it back on the
    next request.

    Args:
        last_id:    ID of the last item on the current page.
        sort_field: Field name being sorted on (for keyset pagination).
        sort_value: Value of the sort field for the last item.

    Returns:
        A URL-safe base64-encoded string token.

    Example::

        token = create_continuation_token(
            last_id=42,
            sort_field="created_at",
            sort_value="2024-01-15T10:30:00Z",
        )
        # → 'eyJsYXN0X2lkIjo0Miwic29ydF9maWVsZCI6...'
    """
    payload: Dict[str, Any] = {"last_id": last_id}
    if sort_field:
        payload["sort_field"] = sort_field
    if sort_value is not None:
        payload["sort_value"] = str(sort_value)

    json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(json_bytes).decode("ascii")
    logger.debug("Created continuation token for last_id=%s", last_id)
    return token


def parse_continuation_token(token: str) -> Dict[str, Any]:
    """Parse and decode a previously issued continuation token.

    Args:
        token: Base64-encoded continuation token from a previous API response.

    Returns:
        Dictionary with cursor position data, e.g.::

            {"last_id": 42, "sort_field": "created_at", "sort_value": "..."}

    Raises:
        ValueError: If the token is malformed, empty, or cannot be decoded.
    """
    if not token or not isinstance(token, str):
        raise ValueError("Continuation token must be a non-empty string")

    try:
        # Add padding if needed — urlsafe_b64decode is tolerant of missing
        # padding on many Python builds, but being explicit is safer.
        padded = token + "=" * (-len(token) % 4)
        json_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(json_bytes)
        if not isinstance(data, dict):
            raise ValueError("Decoded token payload is not a JSON object")
        if "last_id" not in data:
            raise ValueError("Token payload missing required 'last_id' key")
        return data
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Failed to parse continuation token: %s", exc)
        raise ValueError(f"Invalid continuation token: {exc}") from exc


# ---------------------------------------------------------------------------
# Cursor-based pagination
# ---------------------------------------------------------------------------
def paginate_cursor(
    query: Any,
    continuation_token: Optional[str] = None,
    per_page: int = 20,
    max_per_page: int = 100,
    id_column: Any = None,
    sort_column: Any = None,
) -> PageResponse:
    """Paginate using cursor-based (keyset) pagination with continuation tokens.

    More efficient than offset pagination for large datasets because it avoids
    the SQL ``OFFSET`` clause which requires scanning (and discarding) all
    skipped rows.

    The function works with both SQLAlchemy ``Query`` objects and plain Python
    lists.  When a list is provided, ``id_column`` and ``sort_column`` are
    interpreted as the attribute name on each item (a ``str``) rather than a
    SQLAlchemy column descriptor.

    Args:
        query:               SQLAlchemy ``Query`` object **or** plain ``list``.
        continuation_token:  Token from a previous response, or ``None`` for
                             the first page.
        per_page:            Items per page.  Defaults to ``20``.
        max_per_page:        Maximum allowed ``per_page`` value.
        id_column:           SQLAlchemy column for the primary ID (e.g.
                             ``Model.id``), **or** a string attribute name when
                             *query* is a list.
        sort_column:         SQLAlchemy column for ordering (optional), **or** a
                             string attribute name when *query* is a list.

    Returns:
        A :class:`PageResponse` with items and a new ``continuation_token``
        if more pages exist.  ``total_count`` is set to ``-1`` because cursor
        pagination does not efficiently support total counting.
    """
    # Clamp per_page
    if per_page < 1:
        per_page = 1
    if per_page > max_per_page:
        per_page = max_per_page

    # Decode cursor if provided
    cursor: Optional[Dict[str, Any]] = None
    if continuation_token is not None:
        cursor = parse_continuation_token(continuation_token)
        logger.debug("Cursor decoded: %s", cursor)

    # --- List-based pagination -------------------------------------------
    if isinstance(query, list):
        return _paginate_cursor_list(query, cursor, per_page, id_column, sort_column)

    # --- SQLAlchemy Query pagination -------------------------------------
    if id_column is None:
        raise ValueError(
            "id_column is required for cursor-based pagination with query objects"
        )

    if cursor is not None:
        if sort_column is not None and "sort_value" in cursor:
            # Keyset pagination: (sort_value, id) composite cursor
            sort_val = cursor["sort_value"]
            last_id = cursor["last_id"]
            query = query.filter(
                (sort_column > sort_val)
                | ((sort_column == sort_val) & (id_column > last_id))
            )
        else:
            query = query.filter(id_column > cursor["last_id"])

    # Apply ordering
    if sort_column is not None:
        query = query.order_by(sort_column, id_column)
    else:
        query = query.order_by(id_column)

    # Fetch one extra row to detect whether a next page exists
    rows = query.limit(per_page + 1).all()
    has_next = len(rows) > per_page
    items = rows[:per_page]

    # Build the next continuation token
    next_token: Optional[str] = None
    if has_next and items:
        last_item = items[-1]
        last_id_val = _get_column_value(last_item, id_column)
        sort_field_name: Optional[str] = None
        sort_val_out: Optional[Any] = None
        if sort_column is not None:
            sort_field_name = _column_name(sort_column)
            sort_val_out = _get_column_value(last_item, sort_column)
        next_token = create_continuation_token(
            last_id=last_id_val,
            sort_field=sort_field_name,
            sort_value=sort_val_out,
        )

    return PageResponse(
        items=items,
        total_count=-1,  # Cursor pagination does not efficiently count
        page=1 if cursor is None else -1,
        per_page=per_page,
        has_next=has_next,
        has_prev=cursor is not None,
        continuation_token=next_token,
    )


# ---------------------------------------------------------------------------
# Request parameter extraction
# ---------------------------------------------------------------------------
def extract_pagination_params(
    request_args: Dict[str, Any],
    defaults: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Extract and validate pagination parameters from Flask request args.

    Supports both camelCase (``perPage``, ``continuationToken``) and snake_case
    (``per_page``, ``continuation_token``) query parameter names for maximum
    compatibility with diverse API consumers.

    Args:
        request_args: ``flask.request.args`` or any ``dict``-like mapping of
                      query-string parameters.
        defaults:     Optional dict that overrides the built-in defaults for
                      ``page``, ``per_page``, ``sort``, ``direction``, and
                      ``continuation_token``.

    Returns:
        Dictionary with validated keys::

            {
                "page": 1,
                "per_page": 20,
                "sort": None,
                "direction": "asc",
                "continuation_token": None,
            }
    """
    _defaults: Dict[str, Any] = {
        "page": 1,
        "per_page": 20,
        "sort": None,
        "direction": "asc",
        "continuation_token": None,
    }
    if defaults:
        _defaults.update(defaults)

    # --- page ---
    try:
        raw_page = request_args.get("page", _defaults["page"])
        page = max(1, int(raw_page))
    except (TypeError, ValueError):
        logger.warning("Invalid 'page' parameter; defaulting to 1")
        page = 1

    # --- per_page (support both camelCase and snake_case) ---
    try:
        raw_per_page = request_args.get(
            "per_page",
            request_args.get("perPage", _defaults["per_page"]),
        )
        per_page = min(100, max(1, int(raw_per_page)))
    except (TypeError, ValueError):
        logger.warning("Invalid 'per_page' parameter; defaulting to 20")
        per_page = 20

    # --- sort ---
    sort = request_args.get("sort", _defaults["sort"]) or None

    # --- direction ---
    direction = request_args.get("direction", _defaults["direction"])
    if direction not in ("asc", "desc"):
        logger.warning(
            "Invalid 'direction' parameter '%s'; defaulting to 'asc'",
            direction,
        )
        direction = "asc"

    # --- continuation_token (support both camelCase and snake_case) ---
    continuation_token = request_args.get(
        "continuationToken",
        request_args.get("continuation_token", _defaults["continuation_token"]),
    )

    return {
        "page": page,
        "per_page": per_page,
        "sort": sort,
        "direction": direction,
        "continuation_token": continuation_token,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------
def _is_query_like(obj: Any) -> bool:
    """Return ``True`` if *obj* looks like a SQLAlchemy Query.

    The detection is duck-typed so that the module does not need to import
    SQLAlchemy — avoiding a hard dependency on the ORM package.
    """
    return (
        hasattr(obj, "offset")
        and hasattr(obj, "limit")
        and hasattr(obj, "count")
        and callable(getattr(obj, "offset", None))
        and callable(getattr(obj, "limit", None))
    )


def _paginate_cursor_list(
    items_list: List[Any],
    cursor: Optional[Dict[str, Any]],
    per_page: int,
    id_attr: Any,
    sort_attr: Any,
) -> PageResponse:
    """Cursor pagination for an in-memory list.

    ``id_attr`` and ``sort_attr`` are expected to be string attribute names
    (or dict keys) on each item.
    """
    working = list(items_list)

    # Sort the working list if sort_attr is provided
    if sort_attr and isinstance(sort_attr, str):
        working.sort(key=lambda item: (
            _item_attr(item, sort_attr),
            _item_attr(item, id_attr) if id_attr else 0,
        ))
    elif id_attr and isinstance(id_attr, str):
        working.sort(key=lambda item: _item_attr(item, id_attr))

    # Apply cursor filter
    if cursor is not None:
        last_id = cursor.get("last_id")
        sort_value = cursor.get("sort_value")
        if sort_attr and isinstance(sort_attr, str) and sort_value is not None:
            working = [
                item
                for item in working
                if (
                    str(_item_attr(item, sort_attr)) > str(sort_value)
                    or (
                        str(_item_attr(item, sort_attr)) == str(sort_value)
                        and _item_attr(item, id_attr) > last_id
                    )
                )
            ]
        elif id_attr and isinstance(id_attr, str):
            working = [
                item for item in working if _item_attr(item, id_attr) > last_id
            ]

    # Fetch per_page + 1 to detect next page
    result_items = working[: per_page + 1]
    has_next = len(result_items) > per_page
    result_items = result_items[:per_page]

    # Build next token
    next_token: Optional[str] = None
    if has_next and result_items:
        last_item = result_items[-1]
        lid = _item_attr(last_item, id_attr) if id_attr and isinstance(id_attr, str) else None
        sfield = sort_attr if sort_attr and isinstance(sort_attr, str) else None
        sval = _item_attr(last_item, sort_attr) if sfield else None
        if lid is not None:
            next_token = create_continuation_token(
                last_id=lid,
                sort_field=sfield,
                sort_value=sval,
            )

    return PageResponse(
        items=result_items,
        total_count=-1,
        page=1 if cursor is None else -1,
        per_page=per_page,
        has_next=has_next,
        has_prev=cursor is not None,
        continuation_token=next_token,
    )


def _item_attr(item: Any, attr: Any) -> Any:
    """Get an attribute from an item, supporting both objects and dicts."""
    if isinstance(attr, str):
        if isinstance(item, dict):
            return item.get(attr)
        return getattr(item, attr, None)
    return None


def _get_column_value(row: Any, column: Any) -> Any:
    """Extract the value from a SQLAlchemy row for the given column.

    Supports both mapped attribute access (``row.id``) and the column's
    ``key`` attribute.
    """
    if hasattr(column, "key"):
        return getattr(row, column.key, None)
    if hasattr(column, "name"):
        return getattr(row, column.name, None)
    return None


def _column_name(column: Any) -> Optional[str]:
    """Return the string name of a SQLAlchemy column descriptor."""
    if hasattr(column, "key"):
        return str(column.key)
    if hasattr(column, "name"):
        return str(column.name)
    return None
