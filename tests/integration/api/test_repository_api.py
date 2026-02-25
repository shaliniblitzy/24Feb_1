"""Full HTTP lifecycle integration tests for repository CRUD endpoints.

POST/GET/PUT/DELETE on /api/v1/repositories across 3 types × 7 formats.
AAP §0.5.1, §0.5.2, §0.4.2, §0.7.1, §0.10.1.  Features F-101, F-102.
"""
import json
from unittest.mock import patch, MagicMock
import pytest
from tests.fixtures.repository_data import (
    make_hosted_repo, make_proxy_repo, make_group_repo,
    make_hosted_repo_maven, make_hosted_repo_docker,
    get_repo_format_ids, get_repo_type_ids,
    make_repo_with_duplicate_name, make_repo_with_max_name_length,
    make_repo_with_special_characters, make_repo_with_invalid_type,
    make_circular_group_reference, make_all_format_repos,
    REPOSITORY_FORMATS, REPOSITORY_TYPES,
)
from tests.mocks.mock_proxy_client import MockProxyClient

# ---------------------------------------------------------------------------
# Import real source modules (no shim fallback).
# If source modules are not yet created, all tests in this file are skipped.
# ---------------------------------------------------------------------------
pytest.importorskip(
    "src.api.repository_routes",
    reason="Repository routes module not yet created",
)
pytest.importorskip(
    "src.models.repository",
    reason="Repository model module not yet created",
)

pytestmark = pytest.mark.integration
BASE = "/api/v1/repositories"

# Helpers
def _post(c, p, h): return c.post(BASE, json=p, headers=h)
def _get(c, path, h, **kw): return c.get(f"{BASE}{path}", headers=h, **kw)
def _put(c, n, p, h): return c.put(f"{BASE}/{n}", json=p, headers=h)
def _del(c, n, h): return c.delete(f"{BASE}/{n}", headers=h)


# ===================================================================
# Phase 2 — Create (happy path)
# ===================================================================
def test_create_hosted_repository_returns_201(client, auth_headers, db_session):
    """POST hosted repo → 201 with correct type, format, and online flag."""
    resp = _post(client, make_hosted_repo(name="api-hosted-1"), auth_headers)
    assert resp.status_code == 201
    d = resp.get_json()
    assert d["name"] == "api-hosted-1"
    assert d["type"] == "hosted"
    assert d["online"] is True


def test_create_proxy_repository_returns_201(client, auth_headers, db_session):
    """POST proxy repo → 201 with proxy.remote_url present."""
    resp = _post(client, make_proxy_repo(name="api-proxy-1"), auth_headers)
    assert resp.status_code == 201
    d = resp.get_json()
    assert d["type"] == "proxy"
    assert "remote_url" in d.get("proxy", {})


def test_create_group_repository_returns_201(client, auth_headers, db_session):
    """POST group repo → 201 with member_names list."""
    _post(client, make_hosted_repo(name="grp-m-a"), auth_headers)
    _post(client, make_hosted_repo(name="grp-m-b"), auth_headers)
    payload = make_group_repo(name="api-grp-1", member_names=["grp-m-a", "grp-m-b"])
    resp = _post(client, payload, auth_headers)
    assert resp.status_code == 201
    assert set(resp.get_json()["group"]["member_names"]) == {"grp-m-a", "grp-m-b"}


@pytest.mark.parametrize("format_type", REPOSITORY_FORMATS, ids=get_repo_format_ids())
def test_create_repository_all_formats(client, auth_headers, db_session, format_type):
    """POST hosted repo for each of the 7 formats → 201."""
    resp = _post(client, make_hosted_repo(format_type=format_type), auth_headers)
    assert resp.status_code == 201
    assert resp.get_json()["format"] == format_type


@pytest.mark.parametrize("repo_type", REPOSITORY_TYPES, ids=get_repo_type_ids())
def test_create_repository_all_types(client, auth_headers, db_session, repo_type):
    """POST each repository type → 201."""
    if repo_type == "group":
        m = make_hosted_repo(name=f"tm-{repo_type}")
        _post(client, m, auth_headers)
        payload = make_group_repo(member_names=[m["name"]])
    elif repo_type == "proxy":
        payload = make_proxy_repo()
    else:
        payload = make_hosted_repo()
    resp = _post(client, payload, auth_headers)
    assert resp.status_code == 201
    assert resp.get_json()["type"] == repo_type


# ===================================================================
# Phase 3 — Read (happy path)
# ===================================================================
def test_list_repositories_returns_200(client, auth_headers, db_session):
    """GET list → 200 with items array."""
    _post(client, make_hosted_repo(name="lst-a"), auth_headers)
    _post(client, make_proxy_repo(name="lst-b"), auth_headers)
    resp = _get(client, "", auth_headers)
    assert resp.status_code == 200
    assert len(resp.get_json()["items"]) >= 2


def test_get_repository_by_name_returns_200(client, auth_headers, db_session):
    """GET by name → 200 with full details."""
    _post(client, make_hosted_repo(name="get-me"), auth_headers)
    resp = _get(client, "/get-me", auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "get-me"


def test_list_repositories_filtered_by_format(client, auth_headers, db_session):
    """GET ?format=maven → only maven repos."""
    _post(client, make_hosted_repo(name="f-mvn", format_type="maven"), auth_headers)
    _post(client, make_hosted_repo(name="f-npm", format_type="npm"), auth_headers)
    items = _get(client, "?format=maven", auth_headers).get_json()["items"]
    assert all(r["format"] == "maven" for r in items)
    assert len(items) >= 1


def test_list_repositories_filtered_by_type(client, auth_headers, db_session):
    """GET ?type=hosted → only hosted repos."""
    _post(client, make_hosted_repo(name="t-h"), auth_headers)
    _post(client, make_proxy_repo(name="t-p"), auth_headers)
    items = _get(client, "?type=hosted", auth_headers).get_json()["items"]
    assert all(r["type"] == "hosted" for r in items)
    assert len(items) >= 1


def test_list_repositories_with_pagination(client, auth_headers, db_session):
    """GET ?page=1&size=3 → correct page size and total count."""
    for i in range(7):
        _post(client, make_hosted_repo(name=f"pg-{i}"), auth_headers)
    d = _get(client, "?page=1&size=3", auth_headers).get_json()
    assert len(d["items"]) == 3
    assert d["total"] >= 7


def test_list_repositories_empty_returns_200(client, auth_headers, db_session):
    """GET on empty DB → 200 with empty list."""
    d = _get(client, "", auth_headers).get_json()
    assert d["items"] == []
    assert d["total"] == 0


# ===================================================================
# Phase 4 — Update
# ===================================================================
def test_update_repository_returns_200(client, auth_headers, db_session):
    """PUT updates description and online flag."""
    _post(client, make_hosted_repo(name="upd-1"), auth_headers)
    d = _put(client, "upd-1", {"online": False, "description": "changed"}, auth_headers).get_json()
    assert d["online"] is False
    assert d["description"] == "changed"


def test_update_proxy_remote_url(client, auth_headers, db_session):
    """PUT updates proxy remote_url."""
    _post(client, make_proxy_repo(name="upd-px"), auth_headers)
    resp = _put(client, "upd-px", {"proxy": {"remote_url": "https://new.io/"}}, auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()["proxy"]["remote_url"] == "https://new.io/"


def test_update_group_members(client, auth_headers, db_session):
    """PUT updates group member list."""
    _post(client, make_hosted_repo(name="gm1"), auth_headers)
    _post(client, make_hosted_repo(name="gm2"), auth_headers)
    _post(client, make_group_repo(name="upd-g", member_names=["gm1"]), auth_headers)
    resp = _put(client, "upd-g", {"group": {"member_names": ["gm1", "gm2"]}}, auth_headers)
    assert resp.status_code == 200
    assert set(resp.get_json()["group"]["member_names"]) == {"gm1", "gm2"}


def test_toggle_online_status(client, auth_headers, db_session):
    """PUT toggles online → False."""
    _post(client, make_hosted_repo(name="tgl"), auth_headers)
    resp = _put(client, "tgl", {"online": False}, auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()["online"] is False


# ===================================================================
# Phase 5 — Delete
# ===================================================================
def test_delete_repository_returns_204(client, auth_headers, db_session):
    """DELETE → 204, subsequent GET → 404."""
    _post(client, make_hosted_repo(name="del-1"), auth_headers)
    assert _del(client, "del-1", auth_headers).status_code == 204
    assert _get(client, "/del-1", auth_headers).status_code == 404


def test_delete_with_cascade(client, auth_headers, db_session):
    """DELETE repo with data → 204 or 409."""
    _post(client, make_hosted_repo(name="del-c"), auth_headers)
    resp = _del(client, "del-c", auth_headers)
    assert resp.status_code in (204, 409)
    assert resp.status_code != 500


# ===================================================================
# Phase 6 — Edge Cases
# ===================================================================
def test_duplicate_name_returns_409(client, auth_headers, db_session):
    """POST duplicate name → 409."""
    a, b = make_repo_with_duplicate_name("dup-n")
    _post(client, a, auth_headers)
    resp = _post(client, b, auth_headers)
    assert resp.status_code == 409
    assert "already exists" in resp.get_json().get("message", "").lower()


def test_max_name_length(client, auth_headers, db_session):
    """POST 255-char name → 201 (within limit) or 400."""
    resp = _post(client, make_repo_with_max_name_length(255), auth_headers)
    assert resp.status_code in (201, 400)
    if resp.status_code == 201:
        assert len(resp.get_json()["name"]) == 255


def test_special_characters_in_name(client, auth_headers, db_session):
    """POST special-char name → 400 or 201."""
    resp = _post(client, make_repo_with_special_characters(), auth_headers)
    assert resp.status_code in (201, 400)
    assert resp.get_json() is not None


def test_invalid_type_returns_400(client, auth_headers, db_session):
    """POST invalid type → 400."""
    resp = _post(client, make_repo_with_invalid_type(), auth_headers)
    assert resp.status_code == 400
    assert "invalid" in resp.get_json().get("message", "").lower()


def test_circular_group_reference_returns_400(client, auth_headers, db_session):
    """POST circular group → 400."""
    ga, gb = make_circular_group_reference()
    _post(client, ga, auth_headers)
    resp = _post(client, gb, auth_headers)
    assert resp.status_code == 400
    assert "circular" in resp.get_json().get("message", "").lower()


def test_get_nonexistent_returns_404(client, auth_headers, db_session):
    """GET missing name → 404."""
    resp = _get(client, "/no-such-repo", auth_headers)
    assert resp.status_code == 404
    assert "not found" in resp.get_json().get("message", "").lower()


def test_delete_nonexistent_returns_404(client, auth_headers, db_session):
    """DELETE missing name → 404."""
    resp = _del(client, "no-del", auth_headers)
    assert resp.status_code == 404
    assert "not found" in resp.get_json().get("message", "").lower()


def test_proxy_invalid_remote_url_returns_400(client, auth_headers, db_session):
    """POST proxy with bad URL → 400."""
    p = make_proxy_repo(name="bad-px")
    p["proxy"]["remote_url"] = "not-valid"
    resp = _post(client, p, auth_headers)
    assert resp.status_code == 400
    assert "remote url" in resp.get_json().get("message", "").lower()


# ===================================================================
# Phase 7 — Security / Authorization
# ===================================================================
def test_no_auth_returns_401(client, db_session):
    """POST without auth → 401."""
    resp = client.post(BASE, json=make_hosted_repo())
    assert resp.status_code == 401
    assert resp.get_json() is not None


def test_readonly_returns_403(client, readonly_auth_headers, db_session):
    """POST with readonly → 403."""
    resp = _post(client, make_hosted_repo(), readonly_auth_headers)
    assert resp.status_code == 403
    assert "write" in resp.get_json().get("message", "").lower()


def test_developer_can_list(client, developer_auth_headers, db_session):
    """GET with developer → 200."""
    resp = _get(client, "", developer_auth_headers)
    assert resp.status_code == 200
    assert "items" in resp.get_json()


def test_delete_requires_admin(client, auth_headers, developer_auth_headers, db_session):
    """DELETE with developer → 403."""
    _post(client, make_hosted_repo(name="adm-del"), auth_headers)
    resp = _del(client, "adm-del", developer_auth_headers)
    assert resp.status_code == 403
    assert "admin" in resp.get_json().get("message", "").lower()


def test_expired_token_returns_401(client, auth_headers, expired_auth_headers, db_session):
    """PUT with expired JWT → 401."""
    _post(client, make_hosted_repo(name="exp-upd"), auth_headers)
    resp = _put(client, "exp-upd", {"online": False}, expired_auth_headers)
    assert resp.status_code == 401
    assert resp.get_json() is not None


def test_api_key_can_list(client, api_key_headers, db_session):
    """GET with API key → 200."""
    resp = _get(client, "", api_key_headers)
    assert resp.status_code == 200
    assert "items" in resp.get_json()


# ===================================================================
# Phase 8 — Error Handling
# ===================================================================
def test_missing_name_returns_400(client, auth_headers, db_session):
    """POST without name → 400."""
    resp = _post(client, {"format": "maven", "type": "hosted"}, auth_headers)
    assert resp.status_code == 400
    assert "name" in resp.get_json().get("message", "").lower()


def test_missing_format_returns_400(client, auth_headers, db_session):
    """POST without format → 400."""
    resp = _post(client, {"name": "nf", "type": "hosted"}, auth_headers)
    assert resp.status_code == 400
    assert "format" in resp.get_json().get("message", "").lower()


def test_invalid_json_returns_400(client, auth_headers, db_session):
    """POST malformed JSON → 400."""
    resp = client.post(BASE, data="{{bad", headers=auth_headers, content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json() is not None


def test_type_immutable_returns_400(client, auth_headers, db_session):
    """PUT changing type → 400."""
    _post(client, make_hosted_repo(name="imm"), auth_headers)
    resp = _put(client, "imm", {"type": "proxy"}, auth_headers)
    assert resp.status_code == 400
    assert "immutable" in resp.get_json().get("message", "").lower()
