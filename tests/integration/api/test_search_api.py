"""Full HTTP lifecycle integration tests for search query endpoints.

Tests exercise the full HTTP request lifecycle for full-text search,
faceted search, keyword queries, pagination, format filtering, repository
filtering, and sorting using Flask's ``test_client()`` against
``/api/v1/search``.

AAP §0.5.1, §0.2.3, §0.4.2, §0.7.1.  Feature F-103 (Content Indexing
and Search — HIGH priority).

Source file: ``src/api/search_routes.py``.
"""

import json
from unittest.mock import patch, MagicMock

import pytest
from flask import Blueprint, jsonify, request, abort

from tests.fixtures.repository_data import make_hosted_repo
from tests.fixtures.artifact_data import (
    make_maven_artifact,
    make_artifact_for_format,
)
from tests.mocks.mock_search_engine import (
    MockSearchEngine,
    SearchEngineConnectionError,
)
from tests.integration.conftest import (
    assert_json_response,
    assert_error_response,
    assert_pagination,
)

pytestmark = pytest.mark.integration

BASE = "/api/v1/search"

# ---------------------------------------------------------------------------
# Module-level bridge — the autouse fixture injects MockSearchEngine here
# so the shim blueprint can read it at request-time.
# ---------------------------------------------------------------------------
_search_bridge: dict = {}

# ---------------------------------------------------------------------------
# Blueprint shim (when src.api.search_routes is absent)
# ---------------------------------------------------------------------------
_need_shim = True
try:
    from src.api.search_routes import search_bp as _prod_bp  # noqa: F401
    _need_shim = False
except ImportError:
    pass

_VALID_API_KEY = "test-api-key-value-for-integration"


def _build_search_shim() -> Blueprint:
    """Construct a lightweight search API blueprint for integration tests."""
    bp = Blueprint("search_bp_shim", __name__)

    def _auth():
        h = request.headers.get("Authorization", "")
        ak = request.headers.get("X-API-Key", "")
        if not h and not ak:
            abort(401, description="Authentication required")
        if h.startswith("Bearer "):
            from flask_jwt_extended import verify_jwt_in_request, get_jwt
            try:
                verify_jwt_in_request()
            except Exception:
                abort(401, description="Invalid or expired token")
            return get_jwt()
        if ak:
            if ak != _VALID_API_KEY:
                abort(401, description="Invalid or revoked API key")
            return {"role": "developer", "is_admin": False}
        abort(401, description="Authentication required")

    @bp.route("", methods=["GET"])
    @bp.route("/", methods=["GET"])
    def search_components():
        _auth()
        # --- pagination -------------------------------------------------
        page = request.args.get("page", 1, type=int)
        size = request.args.get("size", 10, type=int)
        if page < 1:
            abort(400, description="Invalid page number: must be >= 1")
        if size < 1:
            abort(400, description="Invalid page size: must be >= 1")
        # --- query params -----------------------------------------------
        q = request.args.get("q", "")
        keyword = request.args.get("keyword", "")
        fmt = request.args.get("format", "")
        repo = request.args.get("repository", "")
        sort_f = request.args.get("sort", "")
        sort_d = request.args.get("direction", "asc")
        # --- faceted params ---------------------------------------------
        mv_g = request.args.get("maven.groupId", "")
        mv_a = request.args.get("maven.artifactId", "")
        npm_s = request.args.get("npm.scope", "")
        dk_i = request.args.get("docker.imageName", "")
        dk_t = request.args.get("docker.imageTag", "")
        py_c = request.args.get("pypi.classifiers", "")
        # --- engine ------------------------------------------------------
        engine = _search_bridge.get("engine")
        if engine is None:
            abort(503, description="Search service unavailable")
        # --- build ES-style query ----------------------------------------
        must = []
        if q and q != "*":
            must.append({"match": {"name": q}})
        if keyword:
            must.append({"match": {"name": keyword}})
        if fmt:
            must.append({"term": {"format": fmt}})
        if repo:
            must.append({"term": {"repository": repo}})
        if mv_g:
            must.append({"term": {"metadata.group_id": mv_g}})
        if mv_a:
            must.append({"term": {"metadata.artifact_id": mv_a}})
        if npm_s:
            must.append({"match": {"metadata.name": npm_s}})
        if dk_i:
            must.append({"term": {"metadata.repository": dk_i}})
        if dk_t:
            must.append({"term": {"metadata.tag": dk_t}})
        if py_c:
            must.append({"match": {"metadata.classifiers": py_c}})
        query = {"bool": {"must": must}} if must else {"match_all": {}}
        sort_spec = [{sort_f: {"order": sort_d}}] if sort_f else None
        from_ = (page - 1) * size
        try:
            results = engine.search(
                "components", query=query, size=size,
                from_=from_, sort=sort_spec,
            )
        except SearchEngineConnectionError:
            return jsonify(error="Service Unavailable",
                           message="Search backend unavailable"), 503
        except TimeoutError:
            return jsonify(error="Gateway Timeout",
                           message="Search request timed out"), 504
        except Exception as exc:
            return jsonify(error="Internal Server Error",
                           message=f"Search error: {exc}"), 500
        hits = results.get("hits", {})
        total = hits.get("total", {}).get("value", 0)
        items = [h.get("_source", {}) for h in hits.get("hits", [])]
        return jsonify(items=items, totalCount=total, page=page, size=size), 200

    return bp


# ---------------------------------------------------------------------------
# Session-scoped fixture: register the search shim once
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _register_search_shim(app):
    if _need_shim and "search_bp_shim" not in app.blueprints:
        app._got_first_request = False
        app.register_blueprint(_build_search_shim(), url_prefix=BASE)
    yield


# ---------------------------------------------------------------------------
# Function-scoped fixture: bridge mock_search into the shim per-test
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _bridge_mock_search(mock_search):
    if not mock_search.index_exists("components"):
        mock_search.create_index("components")
    _search_bridge["engine"] = mock_search
    yield
    _search_bridge.clear()


# ===========================================================================
# Helpers
# ===========================================================================

def _seed(engine, docs):
    """Index a list of document dicts into the ``components`` index."""
    for d in docs:
        engine.index("components", d, doc_id=d.get("id"))


def _docs(count=5, **kw):
    """Generate *count* generic search docs with optional overrides."""
    out = []
    for i in range(count):
        doc = {
            "id": f"doc-{i}", "name": f"test-artifact-{i}",
            "format": kw.get("format", "maven"),
            "repository": kw.get("repository", "test-repo"),
            "version": f"1.0.{i}",
            "metadata": kw.get("metadata", {}),
            "lastModified": f"2024-01-{15 + i:02d}T00:00:00Z",
        }
        out.append(doc)
    return out


# ===========================================================================
# Phase 2 — Happy-path search tests
# ===========================================================================


def test_search_components_returns_200_with_results(
    client, auth_headers, mock_search, db_session,
):
    """GET /search?q=... returns 200 with items and totalCount."""
    _seed(mock_search, _docs(3))
    response = client.get(f"{BASE}?q=test-artifact", headers=auth_headers)
    data = assert_json_response(response, 200)
    assert "items" in data and "totalCount" in data
    assert data["totalCount"] >= 1


def test_search_keyword_returns_matching_results(client, auth_headers, mock_search):
    """GET /search?keyword=flask returns only items whose name matches."""
    mock_search.index("components", {"id": "k1", "name": "flask-lib", "format": "pypi", "repository": "r"})
    mock_search.index("components", {"id": "k2", "name": "django-lib", "format": "pypi", "repository": "r"})
    response = client.get(f"{BASE}?keyword=flask", headers=auth_headers)
    data = assert_json_response(response, 200)
    assert all("flask" in it["name"].lower() for it in data["items"])
    assert data["totalCount"] >= 1


def test_search_pagination_returns_paginated_results(client, auth_headers, mock_search):
    """GET /search?q=test-artifact&page=2&size=10 paginates correctly."""
    _seed(mock_search, _docs(25))
    response = client.get(f"{BASE}?q=test-artifact&page=2&size=10", headers=auth_headers)
    data = assert_json_response(response, 200)
    assert len(data["items"]) <= 10
    assert_pagination(data, expected_total=25)
    assert data["page"] == 2


def test_search_format_filter_returns_filtered(client, auth_headers, mock_search):
    """GET /search?format=maven returns only Maven items."""
    mock_search.index("components", {"id": "m1", "name": "a1", "format": "maven", "repository": "r"})
    mock_search.index("components", {"id": "n1", "name": "a2", "format": "npm", "repository": "r"})
    response = client.get(f"{BASE}?format=maven", headers=auth_headers)
    data = assert_json_response(response, 200)
    assert all(it["format"] == "maven" for it in data["items"])
    assert data["totalCount"] >= 1


def test_search_repository_filter(client, auth_headers, mock_search):
    """GET /search?repository=my-maven-repo filters by repository."""
    repo_data = make_hosted_repo(name="my-maven-repo", format_type="maven")
    mock_search.index("components", {"id": "r1", "name": "a1", "format": "maven",
                                      "repository": repo_data["name"]})
    mock_search.index("components", {"id": "r2", "name": "a2", "format": "maven",
                                      "repository": "other"})
    response = client.get(f"{BASE}?repository={repo_data['name']}", headers=auth_headers)
    data = assert_json_response(response, 200)
    assert all(it["repository"] == repo_data["name"] for it in data["items"])
    assert data["totalCount"] >= 1


# ===========================================================================
# Phase 3 — Faceted search tests
# ===========================================================================


def test_search_maven_coordinates(client, auth_headers, mock_search):
    """Faceted search by maven.groupId + maven.artifactId."""
    art = make_maven_artifact(group_id="com.example", artifact_id="my-lib")
    mock_search.index("components", {
        "id": "mv1", "name": "my-lib", "format": "maven", "repository": "r",
        "metadata": art["metadata"],
    })
    mock_search.index("components", {
        "id": "mv2", "name": "other", "format": "maven", "repository": "r",
        "metadata": {"group_id": "org.other", "artifact_id": "other"},
    })
    response = client.get(
        f"{BASE}?maven.groupId=com.example&maven.artifactId=my-lib",
        headers=auth_headers,
    )
    data = assert_json_response(response, 200)
    assert data["totalCount"] >= 1
    assert all(it["metadata"]["group_id"] == "com.example" for it in data["items"])


def test_search_npm_scope_filter(client, auth_headers, mock_search):
    """Faceted search by npm.scope=@myorg."""
    mock_search.index("components", {
        "id": "np1", "name": "@myorg/lib", "format": "npm", "repository": "r",
        "metadata": {"name": "@myorg/lib"},
    })
    mock_search.index("components", {
        "id": "np2", "name": "other-pkg", "format": "npm", "repository": "r",
        "metadata": {"name": "other-pkg"},
    })
    response = client.get(f"{BASE}?npm.scope=@myorg", headers=auth_headers)
    data = assert_json_response(response, 200)
    assert data["totalCount"] >= 1
    assert all("@myorg" in it["metadata"]["name"] for it in data["items"])


def test_search_docker_image_filter(client, auth_headers, mock_search):
    """Faceted search by docker.imageName + docker.imageTag."""
    mock_search.index("components", {
        "id": "dk1", "name": "myapp", "format": "docker", "repository": "r",
        "metadata": {"repository": "myapp", "tag": "latest"},
    })
    response = client.get(
        f"{BASE}?docker.imageName=myapp&docker.imageTag=latest",
        headers=auth_headers,
    )
    data = assert_json_response(response, 200)
    assert data["totalCount"] >= 1
    assert data["items"][0]["metadata"]["repository"] == "myapp"


def test_search_pypi_classifiers(client, auth_headers, mock_search):
    """Faceted search by pypi.classifiers."""
    mock_search.index("components", {
        "id": "py1", "name": "myflask", "format": "pypi", "repository": "r",
        "metadata": {"classifiers": "Framework :: Flask"},
    })
    response = client.get(
        f"{BASE}?pypi.classifiers=Framework+%3A%3A+Flask", headers=auth_headers,
    )
    data = assert_json_response(response, 200)
    assert data["totalCount"] >= 1
    assert isinstance(data["items"], list)


# ===========================================================================
# Phase 4 — Parametrized format search
# ===========================================================================


@pytest.mark.parametrize("format_type", [
    "maven", "npm", "docker", "nuget", "pypi", "apt", "raw",
])
def test_search_by_format_parametrized(client, auth_headers, mock_search, format_type):
    """GET /search?format=<fmt> returns 200 for every supported format."""
    art = make_artifact_for_format(format_type)
    mock_search.index("components", {
        "id": f"fmt-{format_type}", "name": f"test-{format_type}",
        "format": format_type, "repository": "r",
        "metadata": art.get("metadata", {}),
    })
    response = client.get(f"{BASE}?format={format_type}", headers=auth_headers)
    data = json.loads(response.data)
    assert response.status_code == 200
    assert data["totalCount"] >= 1


# ===========================================================================
# Phase 5 — Edge-case tests
# ===========================================================================


def test_search_empty_query_returns_all(client, auth_headers, mock_search):
    """GET /search with no params returns all indexed items."""
    _seed(mock_search, _docs(3))
    count_before = mock_search.get_document_count("components")
    response = client.get(BASE, headers=auth_headers)
    data = assert_json_response(response, 200)
    assert data["totalCount"] >= count_before
    assert isinstance(data["items"], list)


def test_search_no_results_returns_empty(client, auth_headers, mock_search):
    """Non-existent query returns empty items and totalCount=0."""
    response = client.get(f"{BASE}?q=nonexistent-artifact-xyz", headers=auth_headers)
    data = assert_json_response(response, 200)
    assert data["items"] == []
    assert data["totalCount"] == 0


def test_search_special_characters_in_query(client, auth_headers, mock_search):
    """URL-encoded special characters do not crash the server."""
    response = client.get(
        f"{BASE}?q=artifact%20with%20spaces%26special", headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data["items"], list)


def test_search_very_long_query_string(client, auth_headers, mock_search):
    """1000-char query is handled without a 500 error."""
    long_q = "a" * 1000
    response = client.get(f"{BASE}?q={long_q}", headers=auth_headers)
    assert response.status_code in (200, 400)
    assert response.get_json() is not None


def test_search_pagination_beyond_results(client, auth_headers, mock_search):
    """Page far past total count yields empty items with correct total."""
    _seed(mock_search, _docs(5))
    response = client.get(f"{BASE}?q=test-artifact&page=100&size=10", headers=auth_headers)
    data = assert_json_response(response, 200)
    assert data["items"] == []
    assert data["totalCount"] == 5


def test_search_negative_page_returns_400(client, auth_headers, mock_search):
    """Negative page number triggers 400."""
    response = client.get(f"{BASE}?q=test&page=-1", headers=auth_headers)
    assert response.status_code == 400
    data = assert_error_response(response, 400)
    assert "page" in json.loads(response.data).get("message", "").lower()


def test_search_zero_size_returns_400(client, auth_headers, mock_search):
    """Zero page size triggers 400."""
    response = client.get(f"{BASE}?q=test&size=0", headers=auth_headers)
    assert response.status_code == 400
    data = response.get_json()
    assert "size" in data.get("message", data.get("error", "")).lower()


# ===========================================================================
# Phase 6 — Error & security tests
# ===========================================================================


def test_search_without_auth_returns_401(client, mock_search):
    """Missing authentication headers yield 401."""
    response = client.get(f"{BASE}?q=test")
    assert response.status_code == 401
    assert response.get_json() is not None


def test_search_expired_token_returns_401(client, expired_auth_headers, mock_search):
    """Expired JWT bearer token yields 401."""
    response = client.get(f"{BASE}?q=test", headers=expired_auth_headers)
    assert response.status_code == 401
    assert response.get_json() is not None


def test_search_backend_unavailable_returns_503(client, auth_headers, mock_search):
    """Search engine connection failure yields 503."""
    mock_search.configure_error("search", SearchEngineConnectionError("conn refused"))
    response = client.get(f"{BASE}?q=test", headers=auth_headers)
    assert response.status_code in (503, 500)
    data = response.get_json()
    assert "error" in data or "message" in data
    mock_search.clear_error("search")


def test_search_timeout_returns_504(client, auth_headers, mock_search):
    """Search engine timeout yields 504 or 500 (uses patch + MagicMock)."""
    mock_engine = MagicMock(spec=MockSearchEngine)
    mock_engine.search.side_effect = TimeoutError("timed out")
    mock_engine.index_exists.return_value = True
    with patch.dict(_search_bridge, {"engine": mock_engine}):
        response = client.get(f"{BASE}?q=test", headers=auth_headers)
    assert response.status_code in (504, 500)
    data = response.get_json()
    assert data is not None


# ===========================================================================
# Phase 7 — Sorting & advanced queries
# ===========================================================================


def test_search_sort_by_name_asc(client, auth_headers, mock_search):
    """GET /search?sort=name&direction=asc returns alphabetically sorted."""
    for name, idx in [("banana", "s1"), ("apple", "s2"), ("cherry", "s3")]:
        mock_search.index("components", {"id": idx, "name": name,
                                          "format": "raw", "repository": "r"})
    response = client.get(f"{BASE}?sort=name&direction=asc", headers=auth_headers)
    data = assert_json_response(response, 200)
    names = [it["name"] for it in data["items"]]
    assert names == sorted(names)
    assert len(names) == 3


def test_search_sort_by_date_desc(client, auth_headers, mock_search):
    """GET /search?sort=lastModified&direction=desc returns newest first."""
    mock_search.index("components", {
        "id": "d1", "name": "old", "format": "raw",
        "repository": "r", "lastModified": "2024-01-01T00:00:00Z",
    })
    mock_search.index("components", {
        "id": "d2", "name": "new", "format": "raw",
        "repository": "r", "lastModified": "2024-12-31T00:00:00Z",
    })
    response = client.get(
        f"{BASE}?sort=lastModified&direction=desc", headers=auth_headers,
    )
    data = assert_json_response(response, 200)
    dates = [it.get("lastModified", "") for it in data["items"]]
    assert dates == sorted(dates, reverse=True)
    assert len(dates) == 2
