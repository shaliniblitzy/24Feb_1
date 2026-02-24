"""
Mock search engine backend for testing search service operations.

Provides a fully stateful, in-memory mock that simulates an Elasticsearch-like
search engine. Used by search service unit tests, search integration tests,
and functional tests to validate indexing, querying, and document management
without requiring a running search cluster.

Features:
- Elasticsearch-style response formats (_source, _id, hits.total.value, took)
- In-memory document storage with deep-copy isolation
- Basic query matching: match_all, match (substring), term (exact),
  bool (must/should/must_not), wildcard, prefix
- Pagination support via from_ offset and size limit
- Sorting support for search results
- Configurable error injection for failure scenario testing
- Full call logging for test assertions
- Context manager protocol for automatic cleanup
- Bulk indexing for batch operations

All mocked operations are synchronous and produce no network calls.

Typical usage in tests::

    from tests.mocks.mock_search_engine import MockSearchEngine

    engine = MockSearchEngine()
    engine.create_index('components')
    engine.index('components', {'name': 'flask', 'version': '3.1.0'}, doc_id='1')
    results = engine.search('components', query={'match': {'name': 'flask'}})
    assert results['hits']['total']['value'] == 1
"""

from typing import Dict, Any, Optional, List, Set, Tuple
import re
import copy
import datetime
import uuid


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DEFAULT_INDEX: str = "components"
"""Primary index name for artifacts and components."""

MAX_RESULTS_DEFAULT: int = 10
"""Default page size for search results when *size* is not specified."""

DEFAULT_SCROLL_SIZE: int = 100
"""Default number of documents returned in a single scroll batch."""


# ---------------------------------------------------------------------------
# Custom exception hierarchy
# ---------------------------------------------------------------------------


class SearchEngineError(Exception):
    """Base exception for all search engine failures.

    Attributes:
        message: Human-readable description of the error.
        status_code: HTTP-style status code associated with the error.
    """

    def __init__(self, message: str = "Search engine error", status_code: int = 500) -> None:
        self.message: str = message
        self.status_code: int = status_code
        super().__init__(self.message)


class IndexNotFoundError(SearchEngineError):
    """Raised when an operation targets a non-existent index.

    Attributes:
        message: Human-readable description of the error.
        status_code: HTTP-style status code (always 404).
    """

    def __init__(self, message: str = "Index not found", status_code: int = 404) -> None:
        super().__init__(message=message, status_code=status_code)


class DocumentNotFoundError(SearchEngineError):
    """Raised when an operation targets a non-existent document.

    Attributes:
        message: Human-readable description of the error.
        status_code: HTTP-style status code (always 404).
    """

    def __init__(self, message: str = "Document not found", status_code: int = 404) -> None:
        super().__init__(message=message, status_code=status_code)


class IndexAlreadyExistsError(SearchEngineError):
    """Raised when attempting to create an index that already exists.

    Attributes:
        message: Human-readable description of the error.
        status_code: HTTP-style status code (always 400).
    """

    def __init__(self, message: str = "Index already exists", status_code: int = 400) -> None:
        super().__init__(message=message, status_code=status_code)


class SearchEngineConnectionError(SearchEngineError):
    """Raised to simulate search engine connectivity failures.

    Attributes:
        message: Human-readable description of the error.
        status_code: HTTP-style status code (always 503).
    """

    def __init__(
        self, message: str = "Search engine connection failed", status_code: int = 503
    ) -> None:
        super().__init__(message=message, status_code=status_code)


# ---------------------------------------------------------------------------
# MockSearchEngine
# ---------------------------------------------------------------------------


class MockSearchEngine:
    """Mock search backend that stores indexed documents in memory.

    Simulates an Elasticsearch-like search engine for testing search
    service operations without requiring a running search cluster.

    Internal storage layout::

        _indices = {
            'index_name': {
                'doc_id_1': { ... document dict ... },
                'doc_id_2': { ... document dict ... },
            }
        }

    Every public method records its invocation in ``_call_log`` so that
    tests can assert call counts and argument values.  Errors can be
    injected on a per-method basis via ``configure_error`` to simulate
    backend failures.
    """

    # ------------------------------------------------------------------
    # Construction / teardown
    # ------------------------------------------------------------------

    def __init__(self, **kwargs: Any) -> None:
        """Initialise an empty mock search engine.

        Args:
            **kwargs: Reserved for future configuration options.
        """
        self._indices: Dict[str, Dict[str, dict]] = {}
        self._index_settings: Dict[str, dict] = {}
        self._call_log: List[dict] = []
        self._error_config: Dict[str, Exception] = {}
        self._latency_ms: int = 0

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "MockSearchEngine":
        """Enter context — returns *self* for use in ``with`` blocks."""
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        """Exit context — resets all internal state."""
        self.reset()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _record_call(self, method: str, args: Tuple, kwargs: Dict[str, Any]) -> None:
        """Append an entry to the call log for later inspection."""
        self._call_log.append(
            {
                "method": method,
                "args": args,
                "kwargs": kwargs,
                "timestamp": datetime.datetime.utcnow(),
            }
        )

    def _check_error(self, method_name: str) -> None:
        """Raise a pre-configured exception for *method_name* if one exists."""
        if method_name in self._error_config:
            raise self._error_config[method_name]

    def _match_document(self, document: dict, query: dict) -> bool:
        """Evaluate whether *document* satisfies the Elasticsearch-style *query*.

        Supports the following query DSL subset:
        - ``match_all``: matches everything
        - ``match``: case-insensitive substring search on a field
        - ``term``: exact-value match on a field
        - ``bool``: compound query with ``must`` / ``should`` / ``must_not``
        - ``wildcard``: glob-style pattern matching on a field
        - ``prefix``: prefix match on a field

        Returns:
            ``True`` if the document matches; ``False`` otherwise.
        """
        if not query:
            return True

        # match_all — always matches
        if "match_all" in query:
            return True

        # match — case-insensitive substring
        if "match" in query:
            match_clause = query["match"]
            for field, value in match_clause.items():
                # Support both simple value and dict with 'query' key
                if isinstance(value, dict):
                    search_text = str(value.get("query", ""))
                else:
                    search_text = str(value)
                doc_value = self._resolve_field(document, field)
                if doc_value is None:
                    return False
                if not re.search(re.escape(search_text), str(doc_value), re.IGNORECASE):
                    return False
            return True

        # term — exact match
        if "term" in query:
            term_clause = query["term"]
            for field, value in term_clause.items():
                # Support both simple value and dict with 'value' key
                if isinstance(value, dict):
                    expected = value.get("value", value)
                else:
                    expected = value
                doc_value = self._resolve_field(document, field)
                if doc_value is None or doc_value != expected:
                    return False
            return True

        # bool — compound query
        if "bool" in query:
            bool_clause = query["bool"]
            must_clauses: List[dict] = bool_clause.get("must", [])
            should_clauses: List[dict] = bool_clause.get("should", [])
            must_not_clauses: List[dict] = bool_clause.get("must_not", [])
            filter_clauses: List[dict] = bool_clause.get("filter", [])

            # All must clauses must match (AND)
            for clause in must_clauses:
                if not self._match_document(document, clause):
                    return False

            # All filter clauses must match (AND, like must but no scoring)
            for clause in filter_clauses:
                if not self._match_document(document, clause):
                    return False

            # At least one should clause must match (OR) — only when
            # there are no must/filter clauses or when explicitly provided
            if should_clauses:
                if not must_clauses and not filter_clauses:
                    # When there are only should clauses, at least one must match
                    if not any(
                        self._match_document(document, clause) for clause in should_clauses
                    ):
                        return False
                # When must clauses exist alongside should clauses, the should
                # clauses are optional boosters (match already required by must).

            # None of the must_not clauses should match (NOT)
            for clause in must_not_clauses:
                if self._match_document(document, clause):
                    return False

            return True

        # wildcard — glob-style pattern matching
        if "wildcard" in query:
            wildcard_clause = query["wildcard"]
            for field, value in wildcard_clause.items():
                if isinstance(value, dict):
                    pattern = str(value.get("value", ""))
                else:
                    pattern = str(value)
                doc_value = self._resolve_field(document, field)
                if doc_value is None:
                    return False
                # Convert Elasticsearch wildcard syntax to regex:
                # * → .* and ? → .
                regex_pattern = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
                if not re.fullmatch(regex_pattern, str(doc_value), re.IGNORECASE):
                    return False
            return True

        # prefix — prefix matching
        if "prefix" in query:
            prefix_clause = query["prefix"]
            for field, value in prefix_clause.items():
                if isinstance(value, dict):
                    prefix_text = str(value.get("value", ""))
                else:
                    prefix_text = str(value)
                doc_value = self._resolve_field(document, field)
                if doc_value is None:
                    return False
                if not str(doc_value).lower().startswith(prefix_text.lower()):
                    return False
            return True

        # Unrecognised query type — fall back to matching everything
        return True

    @staticmethod
    def _resolve_field(document: dict, field: str) -> Any:
        """Resolve a possibly dot-notated *field* path against *document*.

        For example, ``_resolve_field(doc, "metadata.version")`` returns
        ``doc["metadata"]["version"]`` if it exists, or ``None``.
        """
        parts = field.split(".")
        current: Any = document
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
            if current is None:
                return None
        return current

    def _sort_results(
        self, results: List[Tuple[str, dict]], sort: Optional[list]
    ) -> List[Tuple[str, dict]]:
        """Sort *results* (list of ``(doc_id, doc)`` tuples) according to *sort*.

        Each element in *sort* may be a string (ascending on that field) or a
        dict ``{field: {'order': 'asc'|'desc'}}``.
        """
        if not sort:
            return results

        for sort_spec in reversed(sort):
            if isinstance(sort_spec, str):
                field = sort_spec
                reverse = False
            elif isinstance(sort_spec, dict):
                field = next(iter(sort_spec))
                order_info = sort_spec[field]
                if isinstance(order_info, dict):
                    reverse = order_info.get("order", "asc") == "desc"
                elif isinstance(order_info, str):
                    reverse = order_info == "desc"
                else:
                    reverse = False
            else:
                continue

            def _sort_key(item: Tuple[str, dict], _field: str = field) -> Any:
                val = self._resolve_field(item[1], _field)
                if val is None:
                    # Push None values to the end
                    return (1, "")
                return (0, val)

            results = sorted(results, key=_sort_key, reverse=reverse)

        return results

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def create_index(
        self,
        index_name: str,
        settings: Optional[dict] = None,
        mappings: Optional[dict] = None,
    ) -> dict:
        """Create a new index.

        Args:
            index_name: Name of the index to create.
            settings: Optional index settings (number_of_shards, etc.).
            mappings: Optional field mappings.

        Returns:
            Acknowledgement dict with ``acknowledged`` and ``index`` keys.

        Raises:
            IndexAlreadyExistsError: If the index already exists.
        """
        self._record_call("create_index", (index_name,), {"settings": settings, "mappings": mappings})
        self._check_error("create_index")

        if index_name in self._indices:
            raise IndexAlreadyExistsError(
                message=f"Index '{index_name}' already exists"
            )

        self._indices[index_name] = {}
        self._index_settings[index_name] = {
            "settings": settings or {},
            "mappings": mappings or {},
        }

        return {"acknowledged": True, "index": index_name}

    def delete_index(self, index_name: str) -> dict:
        """Delete an existing index and all its documents.

        Args:
            index_name: Name of the index to delete.

        Returns:
            Acknowledgement dict with ``acknowledged`` key.

        Raises:
            IndexNotFoundError: If the index does not exist.
        """
        self._record_call("delete_index", (index_name,), {})
        self._check_error("delete_index")

        if index_name not in self._indices:
            raise IndexNotFoundError(
                message=f"Index '{index_name}' not found"
            )

        del self._indices[index_name]
        self._index_settings.pop(index_name, None)

        return {"acknowledged": True}

    def index_exists(self, index_name: str) -> bool:
        """Check whether an index exists.

        Args:
            index_name: Name of the index.

        Returns:
            ``True`` if the index exists; ``False`` otherwise.
        """
        self._record_call("index_exists", (index_name,), {})
        return index_name in self._indices

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------

    def index(
        self,
        index_name: str,
        document: dict,
        doc_id: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        """Index (store) a document.

        If the target index does not exist it is auto-created, mirroring
        Elasticsearch's default dynamic index creation behaviour.

        Args:
            index_name: Target index.
            document: Document body to index.
            doc_id: Explicit document ID.  Generated automatically if omitted.
            **kwargs: Additional options (ignored, accepted for API compat).

        Returns:
            Result dict with ``_index``, ``_id``, ``result``, and ``_version``.
        """
        self._record_call(
            "index", (index_name, document), {"doc_id": doc_id, **kwargs}
        )
        self._check_error("index")

        # Auto-create index if it doesn't exist (Elasticsearch behaviour)
        if index_name not in self._indices:
            self._indices[index_name] = {}
            self._index_settings[index_name] = {"settings": {}, "mappings": {}}

        if doc_id is None:
            doc_id = uuid.uuid4().hex

        stored_doc = copy.deepcopy(document)
        stored_doc["_indexed_at"] = datetime.datetime.utcnow().isoformat()

        # Determine if this is a create or update
        result = "updated" if doc_id in self._indices[index_name] else "created"
        version = 1
        if doc_id in self._indices[index_name]:
            # Preserve a simple version counter
            prev = self._indices[index_name][doc_id]
            version = prev.get("_version", 1) + 1

        stored_doc["_version"] = version
        self._indices[index_name][doc_id] = stored_doc

        return {
            "_index": index_name,
            "_id": doc_id,
            "result": result,
            "_version": version,
        }

    def get_document(self, index_name: str, doc_id: str) -> dict:
        """Retrieve a single document by its ID.

        Args:
            index_name: Index containing the document.
            doc_id: Document identifier.

        Returns:
            Dict with ``_index``, ``_id``, ``_source`` (deep copy), and ``found``.

        Raises:
            IndexNotFoundError: If the index does not exist.
            DocumentNotFoundError: If the document does not exist in the index.
        """
        self._record_call("get_document", (index_name, doc_id), {})
        self._check_error("get_document")

        if index_name not in self._indices:
            raise IndexNotFoundError(
                message=f"Index '{index_name}' not found"
            )

        if doc_id not in self._indices[index_name]:
            raise DocumentNotFoundError(
                message=f"Document '{doc_id}' not found in index '{index_name}'"
            )

        source = copy.deepcopy(self._indices[index_name][doc_id])
        # Remove internal fields from _source
        source.pop("_indexed_at", None)
        source.pop("_version", None)

        return {
            "_index": index_name,
            "_id": doc_id,
            "_source": source,
            "found": True,
        }

    def delete_document(self, index_name: str, doc_id: str) -> dict:
        """Delete a document from an index.

        Args:
            index_name: Index containing the document.
            doc_id: Document identifier.

        Returns:
            Dict with ``_index``, ``_id``, and ``result`` set to ``'deleted'``.

        Raises:
            IndexNotFoundError: If the index does not exist.
            DocumentNotFoundError: If the document does not exist in the index.
        """
        self._record_call("delete_document", (index_name, doc_id), {})
        self._check_error("delete_document")

        if index_name not in self._indices:
            raise IndexNotFoundError(
                message=f"Index '{index_name}' not found"
            )

        if doc_id not in self._indices[index_name]:
            raise DocumentNotFoundError(
                message=f"Document '{doc_id}' not found in index '{index_name}'"
            )

        del self._indices[index_name][doc_id]

        return {
            "_index": index_name,
            "_id": doc_id,
            "result": "deleted",
        }

    # ------------------------------------------------------------------
    # Search / query
    # ------------------------------------------------------------------

    def search(
        self,
        index_name: str,
        query: Optional[dict] = None,
        size: int = MAX_RESULTS_DEFAULT,
        from_: int = 0,
        sort: Optional[list] = None,
        **kwargs: Any,
    ) -> dict:
        """Execute a search query against an index.

        Supports a simplified subset of the Elasticsearch Query DSL
        including ``match_all``, ``match``, ``term``, ``bool``,
        ``wildcard``, and ``prefix`` queries.

        Args:
            index_name: Index to search.
            query: Elasticsearch-style query dict.  ``None`` or empty
                dict returns all documents.
            size: Maximum number of hits to return (page size).
            from_: Offset into the result set (for pagination).
            sort: List of sort specifications.
            **kwargs: Additional options (ignored, accepted for API compat).

        Returns:
            Elasticsearch-style response dict with ``hits``, ``took``, and
            ``timed_out`` keys.

        Raises:
            IndexNotFoundError: If the index does not exist.
        """
        self._record_call(
            "search",
            (index_name,),
            {"query": query, "size": size, "from_": from_, "sort": sort, **kwargs},
        )
        self._check_error("search")

        if index_name not in self._indices:
            raise IndexNotFoundError(
                message=f"Index '{index_name}' not found"
            )

        index_data = self._indices[index_name]

        # Collect matching documents
        matched: List[Tuple[str, dict]] = []
        for doc_id, stored_doc in index_data.items():
            # Build a clean copy without internal bookkeeping fields
            clean_doc = copy.deepcopy(stored_doc)
            clean_doc.pop("_indexed_at", None)
            clean_doc.pop("_version", None)
            if self._match_document(clean_doc, query):
                matched.append((doc_id, clean_doc))

        total_matched = len(matched)

        # Sorting
        matched = self._sort_results(matched, sort)

        # Pagination
        paginated = matched[from_: from_ + size]

        hits = [
            {
                "_index": index_name,
                "_id": doc_id,
                "_score": 1.0,
                "_source": doc,
            }
            for doc_id, doc in paginated
        ]

        return {
            "hits": {
                "total": {"value": total_matched, "relation": "eq"},
                "max_score": 1.0 if hits else None,
                "hits": hits,
            },
            "took": self._latency_ms,
            "timed_out": False,
        }

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def bulk_index(
        self,
        index_name: str,
        documents: List[dict],
        id_field: str = "id",
    ) -> dict:
        """Index multiple documents in a single operation.

        Args:
            index_name: Target index.
            documents: List of document dicts to index.
            id_field: Field name within each document to use as the doc ID.
                Falls back to auto-generated UUIDs when the field is absent.

        Returns:
            Bulk response dict with ``items``, ``errors``, and ``took`` keys.
        """
        self._record_call(
            "bulk_index",
            (index_name, documents),
            {"id_field": id_field},
        )
        self._check_error("bulk_index")

        items: List[dict] = []
        has_errors = False

        for document in documents:
            doc_id = document.get(id_field)
            if doc_id is not None:
                doc_id = str(doc_id)

            try:
                result = self.index(index_name, document, doc_id=doc_id)
                items.append(
                    {
                        "index": {
                            "_index": result["_index"],
                            "_id": result["_id"],
                            "result": result["result"],
                            "_version": result["_version"],
                            "status": 201 if result["result"] == "created" else 200,
                        }
                    }
                )
            except SearchEngineError as exc:
                has_errors = True
                items.append(
                    {
                        "index": {
                            "_index": index_name,
                            "_id": doc_id or "",
                            "error": {
                                "type": type(exc).__name__,
                                "reason": exc.message,
                            },
                            "status": exc.status_code,
                        }
                    }
                )

        return {
            "items": items,
            "errors": has_errors,
            "took": self._latency_ms,
        }

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    def count(self, index_name: str, query: Optional[dict] = None) -> dict:
        """Count documents matching *query* without returning them.

        Args:
            index_name: Index to count in.
            query: Optional query dict; ``None`` counts all documents.

        Returns:
            Dict with a ``count`` key.

        Raises:
            IndexNotFoundError: If the index does not exist.
        """
        self._record_call("count", (index_name,), {"query": query})
        self._check_error("count")

        if index_name not in self._indices:
            raise IndexNotFoundError(
                message=f"Index '{index_name}' not found"
            )

        if not query or "match_all" in query:
            return {"count": len(self._indices[index_name])}

        num_matches = 0
        for stored_doc in self._indices[index_name].values():
            clean_doc = copy.deepcopy(stored_doc)
            clean_doc.pop("_indexed_at", None)
            clean_doc.pop("_version", None)
            if self._match_document(clean_doc, query):
                num_matches += 1

        return {"count": num_matches}

    # ------------------------------------------------------------------
    # Test utility methods — error injection
    # ------------------------------------------------------------------

    def configure_error(self, method_name: str, error: Exception) -> None:
        """Configure *error* to be raised when *method_name* is called.

        This allows test code to simulate search backend failures on demand.

        Args:
            method_name: Name of the public method (e.g. ``'search'``).
            error: Exception instance to raise.
        """
        self._error_config[method_name] = error

    def clear_error(self, method_name: str) -> None:
        """Remove the configured error for *method_name*.

        Args:
            method_name: Name of the method whose error should be cleared.
        """
        self._error_config.pop(method_name, None)

    def clear_all_errors(self) -> None:
        """Remove all configured errors."""
        self._error_config.clear()

    # ------------------------------------------------------------------
    # Test utility methods — call introspection
    # ------------------------------------------------------------------

    def get_call_log(self) -> List[dict]:
        """Return a deep copy of the call log for test assertions.

        Each entry contains:
        - ``method`` (str): method name
        - ``args`` (tuple): positional arguments
        - ``kwargs`` (dict): keyword arguments
        - ``timestamp`` (datetime): UTC timestamp of the call
        """
        return copy.deepcopy(self._call_log)

    def get_call_count(self, method_name: str) -> int:
        """Return how many times *method_name* has been called.

        Args:
            method_name: Name of the method to check.
        """
        return sum(1 for entry in self._call_log if entry["method"] == method_name)

    # ------------------------------------------------------------------
    # Test utility methods — state management
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset the engine to its initial empty state.

        Clears all indices, index settings, call logs, error configs,
        and latency settings.
        """
        self._indices.clear()
        self._index_settings.clear()
        self._call_log.clear()
        self._error_config.clear()
        self._latency_ms = 0

    def get_all_documents(self, index_name: str) -> List[dict]:
        """Return deep copies of all documents in *index_name*.

        Useful for test assertions that need to inspect stored state.

        Args:
            index_name: Index to retrieve documents from.

        Returns:
            List of document dicts (without internal bookkeeping fields).

        Raises:
            IndexNotFoundError: If the index does not exist.
        """
        if index_name not in self._indices:
            raise IndexNotFoundError(
                message=f"Index '{index_name}' not found"
            )

        docs: List[dict] = []
        for stored_doc in self._indices[index_name].values():
            doc = copy.deepcopy(stored_doc)
            doc.pop("_indexed_at", None)
            doc.pop("_version", None)
            docs.append(doc)
        return docs

    def get_document_count(self, index_name: str) -> int:
        """Return the number of documents stored in *index_name*.

        Args:
            index_name: Index to count.

        Returns:
            Integer document count, or ``0`` if the index does not exist.
        """
        if index_name not in self._indices:
            return 0
        return len(self._indices[index_name])

    def get_index_names(self) -> List[str]:
        """Return a sorted list of all index names currently stored."""
        index_set: Set[str] = set(self._indices.keys())
        return sorted(index_set)

    def set_latency(self, ms: int) -> None:
        """Set the simulated latency value reported in the ``took`` field.

        This does **not** introduce an actual delay — it only changes the
        numeric value returned in search and bulk operation responses.

        Args:
            ms: Simulated latency in milliseconds.
        """
        self._latency_ms = ms


# ---------------------------------------------------------------------------
# Module-level factory function
# ---------------------------------------------------------------------------


def create_mock_search_engine(**kwargs: Any) -> MockSearchEngine:
    """Convenience factory for creating a pre-configured MockSearchEngine.

    Optionally pre-creates indices if the ``indices`` keyword argument is
    provided.

    Args:
        **kwargs: Configuration options.  Recognised keys:

            - ``indices`` (List[str]): Index names to pre-create.
            - ``latency_ms`` (int): Initial simulated latency.

    Returns:
        A new, optionally pre-configured :class:`MockSearchEngine` instance.

    Example::

        engine = create_mock_search_engine(
            indices=['components', 'assets'],
            latency_ms=5,
        )
    """
    indices: Optional[List[str]] = kwargs.pop("indices", None)
    latency_ms: Optional[int] = kwargs.pop("latency_ms", None)

    engine = MockSearchEngine(**kwargs)

    if indices:
        for index_name in indices:
            engine.create_index(index_name)

    if latency_ms is not None:
        engine.set_latency(latency_ms)

    return engine
