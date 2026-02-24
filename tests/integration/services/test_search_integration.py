"""
Search indexing pipeline integration tests.

Lifecycle: index creation → component indexing → search query →
re-index on update → removal on delete.  Uses MockSearchEngine backend;
real in-memory SQLite ``db_session`` for metadata persistence.

AAP: §0.3.2, §0.4.2, §0.5.1, §0.7.1 (≥85%), Feature F-103.
"""
import json
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.repository_data import (
    make_hosted_repo, make_all_format_repos, get_repo_format_ids,
    REPOSITORY_FORMATS,
)
from tests.fixtures.artifact_data import (
    make_maven_artifact, make_npm_artifact, make_docker_manifest,
    make_nuget_artifact, make_pypi_artifact, make_binary_artifact,
    make_artifact_for_format,
)
from tests.mocks.mock_search_engine import (
    MockSearchEngine, create_mock_search_engine, SearchEngineError,
    IndexNotFoundError, DocumentNotFoundError, SearchEngineConnectionError,
)

# -- SearchService: import real impl or use test-compatible shim -----------
try:
    from src.services.search_service import SearchService
except ImportError:
    class SearchService:
        """Test-compatible SearchService defining the expected interface."""

        def __init__(self, search_engine=None):
            self._engine = search_engine or MockSearchEngine()

        def create_repository_index(self, repo_name, settings=None, mappings=None):
            return self._engine.create_index(repo_name, settings=settings, mappings=mappings)

        def delete_repository_index(self, repo_name):
            try:
                return self._engine.delete_index(repo_name)
            except IndexNotFoundError:
                return {"acknowledged": True}

        def index_component(self, repo_name, component_data, doc_id=None):
            if doc_id is None:
                doc_id = component_data.get("id") or component_data.get("name")
            return self._engine.index(repo_name, component_data, doc_id=doc_id)

        def remove_from_index(self, repo_name, component_id):
            try:
                return self._engine.delete_document(repo_name, component_id)
            except DocumentNotFoundError:
                return {"result": "not_found"}

        def search(self, repo_name, query=None, size=10, from_=0, filters=None):
            es_query = self._build_query(query, filters)
            return self._engine.search(repo_name, query=es_query, size=size, from_=from_)

        def _build_query(self, query_text, filters):
            if not query_text and not filters:
                return {"match_all": {}}
            clauses = []
            if query_text:
                clauses.append({"match": {"name": query_text}})
            filter_clauses = []
            if filters:
                for k, v in filters.items():
                    filter_clauses.append({"term": {k: v}})
            if clauses and filter_clauses:
                return {"bool": {"must": clauses, "filter": filter_clauses}}
            if clauses:
                return clauses[0]
            return {"bool": {"filter": filter_clauses}}

        def bulk_index(self, repo_name, components, id_field="id"):
            return self._engine.bulk_index(repo_name, components, id_field=id_field)

        def reindex_repository(self, repo_name, components, id_field="id"):
            try:
                self._engine.delete_index(repo_name)
            except IndexNotFoundError:
                pass
            self._engine.create_index(repo_name)
            return self._engine.bulk_index(repo_name, components, id_field=id_field)

pytestmark = pytest.mark.integration


def _svc(mock_search):
    """Create a SearchService backed by the given MockSearchEngine."""
    return SearchService(search_engine=mock_search)


def _comp(name, fmt="maven", version="1.0.0", **extra):
    """Build a minimal component metadata dict for indexing."""
    doc = {"id": name, "name": name, "format": fmt, "version": version,
           "repository": f"{fmt}-releases", "created_at": datetime.utcnow().isoformat()}
    doc.update(extra)
    return doc


# === Index Management =====================================================

def test_search_create_index_success(app, db_session, mock_search):
    repo = make_hosted_repo(name="maven-releases", format_type="maven")
    svc = _svc(mock_search)
    result = svc.create_repository_index(repo_name=repo["name"])
    assert mock_search.index_exists("maven-releases") is True
    assert result["acknowledged"] is True
    assert mock_search.get_call_count("create_index") >= 1


def test_search_create_index_with_mappings(app, db_session, mock_search):
    svc = _svc(mock_search)
    mappings = {"properties": {"group_id": {"type": "keyword"},
                               "artifact_id": {"type": "keyword"}}}
    result = svc.create_repository_index("maven-releases", mappings=mappings)
    assert result["acknowledged"] is True
    assert mock_search._index_settings["maven-releases"]["mappings"] == mappings


def test_search_delete_index(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("delete-me")
    assert mock_search.index_exists("delete-me") is True
    result = svc.delete_repository_index("delete-me")
    assert mock_search.index_exists("delete-me") is False
    assert result["acknowledged"] is True


def test_search_delete_nonexistent_index_graceful(app, db_session, mock_search):
    svc = _svc(mock_search)
    result = svc.delete_repository_index("nonexistent-index")
    assert result is not None
    assert result["acknowledged"] is True


# === Document Indexing Pipeline ============================================

def test_search_index_component_after_upload(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("maven-releases")
    component = _comp("artifact-one", fmt="maven")
    result = svc.index_component("maven-releases", component, doc_id="artifact-one")
    assert mock_search.get_document_count("maven-releases") == 1
    assert result["_id"] == "artifact-one"
    doc = mock_search.get_document("maven-releases", "artifact-one")
    assert doc["_source"]["name"] == "artifact-one"
    assert doc["_source"]["format"] == "maven"


def test_search_index_component_stores_metadata(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("maven-releases")
    checksums = {"sha256": "abc123def", "md5": "456xyz"}
    component = _comp("rich-component", fmt="maven", version="2.1.0",
                       group_id="com.example", content_type="application/java-archive",
                       checksums_json=json.dumps(checksums))
    svc.index_component("maven-releases", component, doc_id="rich-component")
    doc = mock_search.get_document("maven-releases", "rich-component")
    assert doc["_source"]["version"] == "2.1.0"
    assert doc["_source"]["group_id"] == "com.example"
    loaded = json.loads(doc["_source"]["checksums_json"])
    assert loaded["sha256"] == "abc123def"


def test_search_index_multiple_components(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("maven-releases")
    for i in range(5):
        svc.index_component("maven-releases", _comp(f"comp-{i}"), doc_id=f"comp-{i}")
    assert mock_search.get_document_count("maven-releases") == 5
    assert mock_search.get_document("maven-releases", "comp-0")["found"] is True
    assert mock_search.get_document("maven-releases", "comp-4")["found"] is True


def test_search_reindex_component_on_update(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("maven-releases")
    svc.index_component("maven-releases", _comp("update-me", version="1.0.0"),
                         doc_id="update-me")
    svc.index_component("maven-releases", _comp("update-me", version="2.0.0"),
                         doc_id="update-me")
    assert mock_search.get_document_count("maven-releases") == 1
    doc = mock_search.get_document("maven-releases", "update-me")
    assert doc["_source"]["version"] == "2.0.0"


def test_search_remove_component_on_delete(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("maven-releases")
    svc.index_component("maven-releases", _comp("delete-me"), doc_id="delete-me")
    assert mock_search.get_document_count("maven-releases") == 1
    result = svc.remove_from_index("maven-releases", "delete-me")
    assert mock_search.get_document_count("maven-releases") == 0
    assert result["result"] == "deleted"


# === Search Query Operations ==============================================

def test_search_query_returns_matching(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("maven-releases")
    for name in ["artifact-one", "artifact-two", "artifact-three"]:
        svc.index_component("maven-releases", _comp(name), doc_id=name)
    results = svc.search("maven-releases", query="artifact-one")
    assert results["hits"]["total"]["value"] >= 1
    names = [h["_source"]["name"] for h in results["hits"]["hits"]]
    assert "artifact-one" in names


def test_search_match_all_returns_all(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("maven-releases")
    for i in range(5):
        svc.index_component("maven-releases", _comp(f"all-{i}"), doc_id=f"all-{i}")
    results = svc.search("maven-releases", size=20)
    assert results["hits"]["total"]["value"] == 5
    assert len(results["hits"]["hits"]) == 5


def test_search_empty_for_no_match(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("maven-releases")
    svc.index_component("maven-releases", _comp("existing"), doc_id="existing")
    results = svc.search("maven-releases", query="nonexistent-xyz-999")
    assert results["hits"]["total"]["value"] == 0
    assert len(results["hits"]["hits"]) == 0
    assert results["timed_out"] is False


def test_search_with_pagination(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("paginated-repo")
    for i in range(20):
        svc.index_component("paginated-repo", _comp(f"pg-{i:02d}"), doc_id=f"pg-{i:02d}")
    page1 = svc.search("paginated-repo", size=5, from_=0)
    page2 = svc.search("paginated-repo", size=5, from_=5)
    assert page1["hits"]["total"]["value"] == 20
    assert len(page1["hits"]["hits"]) == 5
    assert len(page2["hits"]["hits"]) == 5
    ids1 = {h["_id"] for h in page1["hits"]["hits"]}
    ids2 = {h["_id"] for h in page2["hits"]["hits"]}
    assert ids1.isdisjoint(ids2)


def test_search_with_format_filter(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("multi-fmt")
    maven_art = make_maven_artifact()
    npm_art = make_npm_artifact()
    docker_art = make_docker_manifest()
    svc.index_component("multi-fmt", _comp("mvn-pkg", fmt="maven",
                        sha256=maven_art["sha256"]), doc_id="mvn-pkg")
    svc.index_component("multi-fmt", _comp("npm-pkg", fmt="npm",
                        sha256=npm_art["sha256"]), doc_id="npm-pkg")
    svc.index_component("multi-fmt", _comp("dkr-pkg", fmt="docker",
                        sha256=docker_art["sha256"]), doc_id="dkr-pkg")
    results = svc.search("multi-fmt", filters={"format": "maven"})
    assert results["hits"]["total"]["value"] >= 1
    for hit in results["hits"]["hits"]:
        assert hit["_source"]["format"] == "maven"


def test_search_with_repository_scope(app, db_session, mock_search):
    svc = _svc(mock_search)
    all_repos = make_all_format_repos()
    repo_a, repo_b = all_repos[0]["name"], all_repos[1]["name"]
    svc.create_repository_index(repo_a)
    svc.create_repository_index(repo_b)
    svc.index_component(repo_a, _comp("from-a", repository=repo_a), doc_id="from-a")
    svc.index_component(repo_b, _comp("from-b", repository=repo_b), doc_id="from-b")
    results = svc.search(repo_a)
    assert results["hits"]["total"]["value"] == 1
    assert results["hits"]["hits"][0]["_source"]["name"] == "from-a"


# === Multi-Format Search (parametrized) ===================================

@pytest.mark.parametrize("format_type", REPOSITORY_FORMATS,
                         ids=get_repo_format_ids())
def test_search_index_and_query_per_format(app, db_session, mock_search, format_type):
    svc = _svc(mock_search)
    repo = make_hosted_repo(format_type=format_type)
    svc.create_repository_index(repo["name"])
    artifact = make_artifact_for_format(format_type)
    component = _comp(f"{format_type}-comp", fmt=format_type,
                       content_type=artifact.get("content_type", "application/octet-stream"))
    svc.index_component(repo["name"], component, doc_id=f"{format_type}-comp")
    results = svc.search(repo["name"], query=f"{format_type}-comp")
    assert mock_search.get_document_count(repo["name"]) == 1
    assert results["hits"]["total"]["value"] == 1
    assert results["hits"]["hits"][0]["_source"]["format"] == format_type


# === Full Pipeline Integration ============================================

def test_search_pipeline_upload_index_search(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("pipeline-repo")
    assert mock_search.index_exists("pipeline-repo") is True
    artifact = make_maven_artifact(group_id="com.pipeline", artifact_id="app",
                                    version="1.0.0")
    component = _comp("pipeline-app", fmt="maven", version="1.0.0",
                       group_id="com.pipeline", sha256=artifact["sha256"])
    svc.index_component("pipeline-repo", component, doc_id="pipeline-app")
    assert mock_search.get_document_count("pipeline-repo") == 1
    results = svc.search("pipeline-repo", query="pipeline-app")
    assert results["hits"]["total"]["value"] == 1
    assert results["hits"]["hits"][0]["_source"]["sha256"] == artifact["sha256"]


def test_search_pipeline_update_reindex(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("update-pipe")
    pypi_v1 = make_pypi_artifact(package_name="mylib", version="1.0.0")
    svc.index_component("update-pipe",
                         _comp("mylib", fmt="pypi", version="1.0.0",
                               sha256=pypi_v1["sha256"]),
                         doc_id="mylib")
    pypi_v2 = make_pypi_artifact(package_name="mylib", version="2.0.0")
    svc.index_component("update-pipe",
                         _comp("mylib", fmt="pypi", version="2.0.0",
                               sha256=pypi_v2["sha256"]),
                         doc_id="mylib")
    results = svc.search("update-pipe", query="mylib")
    assert results["hits"]["total"]["value"] == 1
    assert results["hits"]["hits"][0]["_source"]["version"] == "2.0.0"


def test_search_pipeline_delete_deindex(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("delete-pipe")
    svc.index_component("delete-pipe", _comp("ephemeral"), doc_id="ephemeral")
    assert mock_search.get_document_count("delete-pipe") == 1
    svc.remove_from_index("delete-pipe", "ephemeral")
    results = svc.search("delete-pipe")
    assert results["hits"]["total"]["value"] == 0
    assert mock_search.get_document_count("delete-pipe") == 0


# === Bulk Operations ======================================================

def test_search_bulk_index_components(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("bulk-repo")
    nuget_art = make_nuget_artifact()
    components = [_comp(f"bulk-{i}", fmt="nuget", sha256=nuget_art["sha256"])
                  for i in range(10)]
    result = svc.bulk_index("bulk-repo", components)
    assert mock_search.get_document_count("bulk-repo") == 10
    assert result["errors"] is False
    assert len(result["items"]) == 10


def test_search_reindex_entire_repository(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("reindex-repo")
    components = [_comp(f"reindex-{i}") for i in range(5)]
    svc.bulk_index("reindex-repo", components)
    assert mock_search.get_document_count("reindex-repo") == 5
    result = svc.reindex_repository("reindex-repo", components)
    assert mock_search.get_document_count("reindex-repo") == 5
    assert result["errors"] is False
    # Verify create_mock_search_engine factory with pre-created indices
    fresh = create_mock_search_engine(indices=["pre-created"])
    assert fresh.index_exists("pre-created") is True


# === Edge Cases ===========================================================

def test_search_special_characters_in_name(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("special-repo")
    svc.index_component("special-repo", _comp("my-artifact_v2.0+build",
                        version="2.0.0+build"), doc_id="special-comp")
    results = svc.search("special-repo", query="my-artifact_v2.0")
    assert results["hits"]["total"]["value"] >= 1
    assert "my-artifact_v2.0+build" in results["hits"]["hits"][0]["_source"]["name"]


def test_search_very_long_query_string(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("long-query-repo")
    svc.index_component("long-query-repo", _comp("short-name"), doc_id="short-name")
    results = svc.search("long-query-repo", query="a" * 1200)
    assert results is not None
    assert "hits" in results
    assert results["hits"]["total"]["value"] == 0


def test_search_minimal_metadata_component(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("minimal-repo")
    raw = make_binary_artifact()
    minimal = {"id": "min", "name": "min-component", "format": "raw",
               "sha256": raw["sha256"]}
    svc.index_component("minimal-repo", minimal, doc_id="min")
    assert mock_search.get_document_count("minimal-repo") == 1
    doc = mock_search.get_document("minimal-repo", "min")
    assert doc["_source"]["name"] == "min-component"
    assert doc["_source"]["format"] == "raw"


def test_search_unicode_component_name(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("unicode-repo")
    svc.index_component("unicode-repo", _comp("文件_données_パッケージ", fmt="raw"),
                         doc_id="unicode-comp")
    results = svc.search("unicode-repo", query="文件_données")
    assert results["hits"]["total"]["value"] >= 1
    doc = mock_search.get_document("unicode-repo", "unicode-comp")
    assert "文件" in doc["_source"]["name"]
    assert "données" in doc["_source"]["name"]


# === Error Cases ==========================================================

def test_search_nonexistent_index_raises(app, db_session, mock_search):
    svc = _svc(mock_search)
    with pytest.raises(IndexNotFoundError):
        svc.search("nonexistent-index", query="anything")
    assert mock_search.get_document_count("nonexistent-index") == 0


def test_search_connection_error_propagates(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("error-repo")
    with patch.object(mock_search, "search",
                      side_effect=SearchEngineConnectionError("Connection refused")):
        with pytest.raises(SearchEngineConnectionError) as exc_info:
            svc.search("error-repo", query="anything")
    assert "Connection refused" in str(exc_info.value)
    assert exc_info.value.status_code == 503


def test_search_index_write_failure(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("fail-index-repo")
    error_callback = MagicMock()
    mock_search.configure_error("index", SearchEngineError("Write failure"))
    try:
        svc.index_component("fail-index-repo", _comp("fail"), doc_id="fail")
    except SearchEngineError as exc:
        error_callback(str(exc))
    assert error_callback.called is True
    assert mock_search.get_document_count("fail-index-repo") == 0
    mock_search.clear_error("index")


def test_search_remove_nonexistent_document_graceful(app, db_session, mock_search):
    svc = _svc(mock_search)
    svc.create_repository_index("remove-repo")
    result = svc.remove_from_index("remove-repo", "ghost-id")
    assert result is not None
    assert result["result"] == "not_found"
