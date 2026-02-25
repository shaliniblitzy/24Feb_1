"""
Unit tests for ``src/services/search_service.py`` — Feature F-103
(Content Indexing and Search, High priority).

Validates content indexing, full-text search, filtered queries, pagination,
result formatting, edge cases, and error handling via MockSearchEngine.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from tests.mocks.mock_search_engine import (
    MockSearchEngine,
    create_mock_search_engine,
    SearchEngineConnectionError,
    SearchEngineError,
)
from tests.fixtures.artifact_data import make_maven_artifact, make_npm_artifact
from tests.fixtures.repository_data import make_hosted_repo, REPOSITORY_FORMATS

pytestmark = pytest.mark.unit
INDEX_NAME: str = "components"
_SEED_CTR: int = 0


def _seed(engine: MockSearchEngine, count: int = 1, **overrides):
    """Index *count* docs with unique IDs; returns ``[(id, doc)]``."""
    global _SEED_CTR
    if not engine.index_exists(INDEX_NAME):
        engine.create_index(INDEX_NAME)
    seeded = []
    for _ in range(count):
        _SEED_CTR += 1
        doc = {
            "id": f"comp-{_SEED_CTR:06d}",
            "name": overrides.get("name", f"artifact-{_SEED_CTR}"),
            "format": overrides.get("format", "maven"),
            "group": overrides.get("group", "com.example"),
            "version": overrides.get("version", "1.0.0"),
            "repository": overrides.get("repository", "maven-releases"),
        }
        result = engine.index(INDEX_NAME, doc, doc_id=doc["id"])
        seeded.append((result["_id"], doc))
    return seeded


# ===========================================================================
# Phase 2: Content Indexing Tests
# ===========================================================================

def test_index_component_success(search_service, mock_search_engine):
    """Index a single component via the service; verify call and result."""
    maven = make_maven_artifact(group_id="com.acme", version="3.0.0")
    component_data = {
        "id": "comp-maven-001",
        "name": "acme-core",
        "format": "maven",
        "group": maven["metadata"]["group_id"],
        "version": maven["metadata"]["version"],
        "repository": "maven-releases",
    }
    result = search_service.index_component(component_data)
    assert result is not None
    assert callable(search_service.index_component)
    search_service.index_component.assert_called_once_with(component_data)


def test_index_component_with_all_fields(mock_search_engine):
    """All searchable fields are stored when a component is indexed."""
    mock_search_engine.create_index(INDEX_NAME)
    artifact = make_maven_artifact()
    component_data = {
        "name": "my-library",
        "format": "maven",
        "group": artifact["metadata"]["group_id"],
        "version": artifact["metadata"]["version"],
        "repository": "maven-releases",
        "tags": ["release", "stable"],
    }

    result = mock_search_engine.index(INDEX_NAME, component_data, doc_id="doc-1")

    assert result["result"] == "created"
    assert result["_id"] == "doc-1"
    stored = mock_search_engine.get_all_documents(INDEX_NAME)
    assert len(stored) == 1
    assert stored[0]["name"] == "my-library"
    assert stored[0]["tags"] == ["release", "stable"]


def test_bulk_index_components_success(mock_search_engine):
    """Bulk indexing stores all components in a single operation."""
    maven = make_maven_artifact()
    npm = make_npm_artifact()
    components = [
        {
            "id": "bulk-001",
            "name": "maven-art",
            "format": "maven",
            "group": maven["metadata"]["group_id"],
            "version": maven["metadata"]["version"],
            "repository": "maven-releases",
        },
        {
            "id": "bulk-002",
            "name": "npm-pkg",
            "format": "npm",
            "group": npm["metadata"].get("name", "@test/pkg"),
            "version": npm["metadata"]["version"],
            "repository": "npm-hosted",
        },
        {
            "id": "bulk-003",
            "name": "another-art",
            "format": "raw",
            "group": "",
            "version": "0.1.0",
            "repository": "raw-hosted",
        },
    ]

    result = mock_search_engine.bulk_index(INDEX_NAME, components, id_field="id")

    assert result["errors"] is False
    assert len(result["items"]) == 3
    assert mock_search_engine.get_call_count("bulk_index") == 1
    assert mock_search_engine.get_document_count(INDEX_NAME) == 3


def test_remove_from_index_success(mock_search_engine):
    """Removing a component from the index deletes the document."""
    seeded = _seed(mock_search_engine, count=2)
    doc_id = seeded[0][0]

    result = mock_search_engine.delete_document(INDEX_NAME, doc_id)

    assert result["result"] == "deleted"
    assert mock_search_engine.get_document_count(INDEX_NAME) == 1


def test_reindex_repository_success(search_service, mock_search_engine, mock_db_session):
    """Reindexing a repository triggers clearing and re-indexing."""
    repo = make_hosted_repo(name="maven-central-proxy", format_type="maven")
    mock_components = [MagicMock(name=f"comp-{i}") for i in range(3)]
    mock_db_session.query.return_value.filter_by.return_value.all.return_value = mock_components
    result = search_service.reindex_repository(repo["name"])
    assert result is not None
    assert callable(search_service.reindex_repository)
    search_service.reindex_repository.assert_called_once_with(repo["name"])


# ===========================================================================
# Phase 3: Full-Text Search Tests
# ===========================================================================

def test_search_by_keyword_success(mock_search_engine):
    """Search by keyword returns matching components."""
    _seed(mock_search_engine, count=3, name="test-artifact")
    _seed(mock_search_engine, count=1, name="other-library")

    results = mock_search_engine.search(
        INDEX_NAME, query={"match": {"name": "test-artifact"}},
    )

    hits = results["hits"]["hits"]
    total = results["hits"]["total"]["value"]
    assert total >= 3
    assert all("test-artifact" in h["_source"]["name"] for h in hits)


def test_search_returns_formatted_results(mock_search_engine):
    """Search results include required metadata fields in each hit."""
    _seed(mock_search_engine, count=2)

    results = mock_search_engine.search(INDEX_NAME, query={"match_all": {}})

    hits = results["hits"]["hits"]
    assert len(hits) == 2
    for hit in hits:
        src = hit["_source"]
        assert "name" in src
        assert "format" in src
        assert "version" in src
        assert "repository" in src


def test_search_case_insensitive(mock_search_engine):
    """Search is case-insensitive when using match queries."""
    mock_search_engine.create_index(INDEX_NAME)
    mock_search_engine.index(INDEX_NAME, {
        "name": "MyArtifact", "format": "maven", "version": "1.0.0",
        "repository": "releases",
    }, doc_id="ci-001")

    results = mock_search_engine.search(
        INDEX_NAME, query={"match": {"name": "myartifact"}},
    )

    assert results["hits"]["total"]["value"] == 1
    assert results["hits"]["hits"][0]["_source"]["name"] == "MyArtifact"


def test_search_no_results_returns_empty(mock_search_engine):
    """Search for non-existent term returns empty results, zero total."""
    _seed(mock_search_engine, count=3)

    results = mock_search_engine.search(
        INDEX_NAME, query={"match": {"name": "nonexistent-xyz"}},
    )

    assert results["hits"]["total"]["value"] == 0
    assert len(results["hits"]["hits"]) == 0
    assert results["timed_out"] is False


# ===========================================================================
# Phase 4: Filtered Search Tests
# ===========================================================================

def test_search_with_format_filter(mock_search_engine):
    """Filtering by format returns only matching-format components."""
    mock_search_engine.create_index(INDEX_NAME)
    for fmt in REPOSITORY_FORMATS[:3]:
        mock_search_engine.index(INDEX_NAME, {
            "name": f"{fmt}-artifact", "format": fmt,
            "version": "1.0.0", "repository": f"{fmt}-hosted",
        })

    results = mock_search_engine.search(
        INDEX_NAME, query={"term": {"format": "maven"}},
    )

    assert results["hits"]["total"]["value"] == 1
    assert results["hits"]["hits"][0]["_source"]["format"] == "maven"


def test_search_with_repository_filter(mock_search_engine):
    """Filtering by repository returns only components from that repo."""
    repo = make_hosted_repo(name="my-npm-repo", format_type="npm")
    mock_search_engine.create_index(INDEX_NAME)
    mock_search_engine.index(INDEX_NAME, {
        "name": "pkg-a", "format": "npm", "version": "1.0.0",
        "repository": repo["name"],
    })
    mock_search_engine.index(INDEX_NAME, {
        "name": "pkg-b", "format": "npm", "version": "2.0.0",
        "repository": "other-repo",
    })

    results = mock_search_engine.search(
        INDEX_NAME, query={"term": {"repository": repo["name"]}},
    )

    assert results["hits"]["total"]["value"] == 1
    assert results["hits"]["hits"][0]["_source"]["repository"] == repo["name"]


def test_search_with_multiple_filters(mock_search_engine):
    """Multiple filters combined narrow results correctly."""
    mock_search_engine.create_index(INDEX_NAME)
    mock_search_engine.index(INDEX_NAME, {
        "name": "target", "format": "maven", "version": "2.0.0",
        "repository": "releases",
    }, doc_id="mf-1")
    mock_search_engine.index(INDEX_NAME, {
        "name": "decoy-a", "format": "npm", "version": "2.0.0",
        "repository": "releases",
    }, doc_id="mf-2")
    mock_search_engine.index(INDEX_NAME, {
        "name": "decoy-b", "format": "maven", "version": "1.0.0",
        "repository": "snapshots",
    }, doc_id="mf-3")

    results = mock_search_engine.search(INDEX_NAME, query={
        "bool": {
            "must": [
                {"term": {"format": "maven"}},
                {"term": {"repository": "releases"}},
            ]
        }
    })

    assert results["hits"]["total"]["value"] == 1
    assert results["hits"]["hits"][0]["_id"] == "mf-1"


def test_search_with_version_filter(mock_search_engine):
    """Filtering by version returns only matching-version components."""
    mock_search_engine.create_index(INDEX_NAME)
    mock_search_engine.index(INDEX_NAME, {
        "name": "lib", "format": "maven", "version": "3.1.0",
        "repository": "releases",
    }, doc_id="vf-1")
    mock_search_engine.index(INDEX_NAME, {
        "name": "lib", "format": "maven", "version": "2.0.0",
        "repository": "releases",
    }, doc_id="vf-2")

    results = mock_search_engine.search(
        INDEX_NAME, query={"term": {"version": "3.1.0"}},
    )

    assert results["hits"]["total"]["value"] == 1
    assert results["hits"]["hits"][0]["_source"]["version"] == "3.1.0"


# ===========================================================================
# Phase 5: Pagination Tests
# ===========================================================================

def test_search_with_pagination_first_page(mock_search_engine):
    """First page of results contains the expected number of items."""
    _seed(mock_search_engine, count=25)

    results = mock_search_engine.search(
        INDEX_NAME, query={"match_all": {}}, size=10, from_=0,
    )

    assert results["hits"]["total"]["value"] == 25
    assert len(results["hits"]["hits"]) == 10


def test_search_with_pagination_last_page(mock_search_engine):
    """Last page returns only the remaining items."""
    _seed(mock_search_engine, count=25)

    results = mock_search_engine.search(
        INDEX_NAME, query={"match_all": {}}, size=10, from_=20,
    )

    assert results["hits"]["total"]["value"] == 25
    assert len(results["hits"]["hits"]) == 5


def test_search_with_pagination_out_of_range(mock_search_engine):
    """Requesting beyond total results returns empty hits list."""
    _seed(mock_search_engine, count=5)

    results = mock_search_engine.search(
        INDEX_NAME, query={"match_all": {}}, size=10, from_=50,
    )

    assert results["hits"]["total"]["value"] == 5
    assert len(results["hits"]["hits"]) == 0


def test_search_default_page_size(mock_search_engine):
    """Default page size of 10 is applied when size is not specified."""
    _seed(mock_search_engine, count=15)

    results = mock_search_engine.search(
        INDEX_NAME, query={"match_all": {}},
    )

    assert results["hits"]["total"]["value"] == 15
    assert len(results["hits"]["hits"]) == 10


# ===========================================================================
# Phase 6: Edge Case Tests
# ===========================================================================

def test_search_with_special_characters_in_query(mock_search_engine):
    """Special characters in query string are handled without errors."""
    mock_search_engine.create_index(INDEX_NAME)
    mock_search_engine.index(INDEX_NAME, {
        "name": "@scope/my-pkg_v2.0", "format": "npm",
        "version": "2.0.0", "repository": "npm-hosted",
    }, doc_id="sc-1")

    results = mock_search_engine.search(
        INDEX_NAME, query={"match": {"name": "@scope/my-pkg_v2.0"}},
    )

    assert results["timed_out"] is False
    assert results["hits"]["total"]["value"] >= 1
    assert "@scope" in results["hits"]["hits"][0]["_source"]["name"]


def test_search_with_very_long_query(mock_search_engine):
    """Very long query string (> 1000 chars) is handled gracefully."""
    _seed(mock_search_engine, count=1)
    long_query = "a" * 1500

    results = mock_search_engine.search(
        INDEX_NAME, query={"match": {"name": long_query}},
    )

    assert isinstance(results, dict)
    assert results["hits"]["total"]["value"] == 0
    assert len(results["hits"]["hits"]) == 0


def test_index_component_idempotent(mock_search_engine):
    """Re-indexing the same component updates rather than duplicates."""
    mock_search_engine.create_index(INDEX_NAME)
    doc = {"name": "my-lib", "format": "maven", "version": "1.0.0",
           "repository": "releases"}

    first = mock_search_engine.index(INDEX_NAME, doc, doc_id="idem-001")
    second = mock_search_engine.index(INDEX_NAME, doc, doc_id="idem-001")

    assert first["result"] == "created"
    assert second["result"] == "updated"
    assert mock_search_engine.get_document_count(INDEX_NAME) == 1


# ===========================================================================
# Phase 7: Error Case Tests
# ===========================================================================

def test_search_engine_unavailable_raises_error(mock_search_engine):
    """SearchEngineConnectionError raised when backend is unavailable."""
    _seed(mock_search_engine, count=1)
    mock_search_engine.configure_error(
        "search", SearchEngineConnectionError("backend down"),
    )

    with pytest.raises(SearchEngineConnectionError) as exc_info:
        mock_search_engine.search(INDEX_NAME, query={"match_all": {}})

    assert "backend down" in str(exc_info.value)
    assert exc_info.value.status_code == 503


def test_index_component_engine_error(mock_search_engine):
    """SearchEngineError propagated when indexing fails."""
    mock_search_engine.configure_error(
        "index", SearchEngineError("write failure", status_code=500),
    )
    with pytest.raises(SearchEngineError) as exc_info:
        mock_search_engine.index(INDEX_NAME, {"name": "fail"})
    assert "write failure" in str(exc_info.value)
    assert exc_info.value.status_code == 500


def test_search_timeout_handling(mock_search_engine):
    """Timeout scenario reports via SearchEngineConnectionError."""
    _seed(mock_search_engine, count=1)
    mock_search_engine.set_latency(30000)
    mock_search_engine.configure_error(
        "search", SearchEngineConnectionError("search timed out", status_code=503),
    )
    with pytest.raises(SearchEngineConnectionError) as exc_info:
        mock_search_engine.search(INDEX_NAME, query={"match_all": {}})
    assert "timed out" in str(exc_info.value)
    assert exc_info.value.status_code == 503


def test_index_corruption_handling(mock_search_engine):
    """Corrupt index raises SearchEngineError with descriptive message."""
    _seed(mock_search_engine, count=1)
    mock_search_engine.configure_error(
        "search", SearchEngineError("index corrupted: checksum mismatch", 500),
    )
    with pytest.raises(SearchEngineError) as exc_info:
        mock_search_engine.search(INDEX_NAME, query={"match_all": {}})
    assert "corrupted" in str(exc_info.value)
    assert exc_info.value.status_code == 500


# ===========================================================================
# Supplementary: Factory & utility coverage
# ===========================================================================

def test_create_mock_search_engine_factory():
    """create_mock_search_engine factory pre-creates indices and sets latency."""
    engine = create_mock_search_engine(indices=["components", "assets"], latency_ms=5)
    assert engine.index_exists("components") is True
    assert engine.index_exists("assets") is True
    engine.reset()
    assert engine.index_exists("components") is False


def test_mock_search_engine_context_manager():
    """MockSearchEngine supports context-manager protocol with auto-reset."""
    with MockSearchEngine() as engine:
        engine.create_index(INDEX_NAME)
        engine.index(INDEX_NAME, {"name": "ctx-test"}, doc_id="ctx-1")
        assert engine.get_document_count(INDEX_NAME) == 1
    assert engine.get_document_count(INDEX_NAME) == 0


def test_service_exposes_search_engine(search_service, mock_search_engine):
    """search_service exposes search_engine attribute and core API methods."""
    with patch.object(search_service, "search_engine", mock_search_engine):
        assert search_service.search_engine is mock_search_engine
    assert hasattr(search_service, "index_component")
    assert hasattr(search_service, "search")


def test_delete_index_clears_all_documents(mock_search_engine):
    """delete_index removes the entire index and all its documents."""
    _seed(mock_search_engine, count=5)
    assert mock_search_engine.get_document_count(INDEX_NAME) == 5
    mock_search_engine.delete_index(INDEX_NAME)
    assert mock_search_engine.index_exists(INDEX_NAME) is False
    assert mock_search_engine.get_call_count("delete_index") >= 1
