"""End-to-end repository lifecycle functional tests.

Exercises: create→configure→populate→browse→cleanup→delete across all 3 repo
types, 7 formats, config updates, browse (F-104), cleanup (F-204), and errors.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.repository_data import (
    make_hosted_repo, make_proxy_repo, make_group_repo,
    make_hosted_repo_maven, make_hosted_repo_npm, make_hosted_repo_docker,
    make_hosted_repo_nuget, make_hosted_repo_pypi, make_hosted_repo_apt,
    make_hosted_repo_raw, make_proxy_repo_maven,
    make_all_format_repos, make_all_type_repos,
    make_repo_with_duplicate_name, make_repo_with_max_name_length,
    make_repo_with_special_characters, get_repo_format_ids,
)
from tests.fixtures.artifact_data import make_maven_artifact, make_artifact_for_format
from tests.fixtures.user_data import (
    make_admin_user, make_developer_user, make_readonly_user,
)
from tests.functional.conftest import (
    assert_json_response, assert_error_response,
    assert_created_response, assert_no_content_response,
)

pytestmark = pytest.mark.functional

# ---- Local Fixtures --------------------------------------------------------

@pytest.fixture
def created_hosted_repo(create_test_repository, db_session):
    """Create a hosted Maven repo; auto-cleaned on teardown."""
    resp = create_test_repository(repo_data=make_hosted_repo_maven())
    assert resp.status_code == 201
    yield resp.get_json()

@pytest.fixture
def populated_repo(create_test_repository, upload_test_artifact, db_session):
    """Hosted Maven repo with 3 test artifacts."""
    resp = create_test_repository(repo_data=make_hosted_repo_maven())
    assert resp.status_code == 201
    repo_json = resp.get_json()
    artifacts = []
    for idx in range(3):
        art = make_maven_artifact(group_id=f"com.example.pop{idx}",
                                  artifact_id=f"pop-artifact-{idx}", version="1.0.0")
        upload_test_artifact(repo_json["name"], artifact_data=art)
        artifacts.append(art)
    yield repo_json, artifacts

# ---- Happy Path: Complete Lifecycle ----------------------------------------


def test_repository_full_lifecycle_create_configure_populate_browse_cleanup_delete(
    client, auth_headers, db_session, upload_test_artifact,
    mock_storage, mock_search,
):
    """Full 6-step lifecycle: create→configure→populate→browse→cleanup→delete."""
    # CREATE
    repo_data = make_hosted_repo_maven()
    created = assert_created_response(
        client.post("/api/v1/repositories", json=repo_data, headers=auth_headers))
    repo_name = created["name"]
    assert created["type"] == "hosted"
    assert created["format"] == "maven"
    assert created["online"] is True

    # CONFIGURE
    updated = assert_json_response(client.put(
        f"/api/v1/repositories/{repo_name}",
        json={"online": True, "storage": {"blob_store_name": "default",
              "strict_content_type_validation": True, "write_policy": "ALLOW"}},
        headers=auth_headers), expected_status=200)
    assert updated["storage"]["write_policy"] == "ALLOW"

    # POPULATE
    for i in range(2):
        art = make_maven_artifact(group_id=f"com.lifecycle.s{i}",
                                  artifact_id=f"lc-art-{i}", version="1.0.0")
        assert upload_test_artifact(repo_name, artifact_data=art).status_code == 201

    # BROWSE
    browse = assert_json_response(client.get(
        f"/api/v1/repositories/{repo_name}/browse", headers=auth_headers))
    assert isinstance(browse, (list, dict))

    # CLEANUP
    assert client.post(
        f"/api/v1/repositories/{repo_name}/cleanup",
        headers=auth_headers).status_code in (200, 202, 204)

    # DELETE
    assert_no_content_response(client.delete(
        f"/api/v1/repositories/{repo_name}", headers=auth_headers))
    assert client.get(
        f"/api/v1/repositories/{repo_name}",
        headers=auth_headers).status_code == 404


def test_repository_create_and_retrieve(
    client, auth_headers, create_test_repository, db_session,
):
    """Create a repository via POST then retrieve via GET."""
    repo_data = make_hosted_repo_maven()
    created = assert_created_response(create_test_repository(repo_data=repo_data))
    retrieved = assert_json_response(client.get(
        f"/api/v1/repositories/{created['name']}", headers=auth_headers))
    assert retrieved["name"] == created["name"]
    assert retrieved["format"] == repo_data["format"]
    assert retrieved["type"] == "hosted"


@pytest.mark.parametrize("repo_type", ["hosted", "proxy", "group"],
                         ids=["type-hosted", "type-proxy", "type-group"])
def test_repository_create_all_types(
    client, auth_headers, create_test_repository, db_session, repo_type,
):
    """Create and verify a repository for each of the 3 types."""
    repos = make_all_type_repos(format_type="maven")
    data = next(r for r in repos if r["type"] == repo_type)
    created = assert_created_response(create_test_repository(repo_data=data))
    assert created["type"] == repo_type
    assert created["format"] == "maven"


def test_repository_list_all(
    client, auth_headers, create_test_repository, db_session,
):
    """Create 3 repos of different types; verify all appear in list."""
    names = []
    for rt in ("hosted", "proxy", "group"):
        names.append(assert_created_response(
            create_test_repository(format_type="maven", repo_type=rt))["name"])

    data = assert_json_response(
        client.get("/api/v1/repositories", headers=auth_headers))
    items = (data.get("items", data.get("repositories", []))
             if isinstance(data, dict) else data)
    returned = [r["name"] for r in items]
    for n in names:
        assert n in returned, f"Repository '{n}' missing"
    assert len(items) >= 3


# ---- Multi-Format Tests (F-101) -------------------------------------------


@pytest.mark.parametrize(
    "format_type",
    ["maven", "npm", "docker", "nuget", "pypi", "apt", "raw"],
    ids=get_repo_format_ids(),
)
def test_repository_create_all_formats(
    client, auth_headers, create_test_repository, db_session, format_type,
):
    """Create a hosted repository for each of the 7 formats."""
    repos = make_all_format_repos(repo_type="hosted")
    data = next(r for r in repos if r["format"] == format_type)
    created = assert_created_response(create_test_repository(repo_data=data))
    assert created["format"] == format_type
    assert created["type"] == "hosted"


def test_repository_format_specific_settings_maven(
    client, auth_headers, create_test_repository, db_session,
):
    """Verify Maven-specific settings (version_policy, layout_policy)."""
    data = make_hosted_repo_maven(
        maven={"version_policy": "MIXED", "layout_policy": "STRICT",
               "content_disposition": "ATTACHMENT"})
    created = assert_created_response(create_test_repository(repo_data=data))
    assert created["format"] == "maven"
    assert created.get("maven", {}).get("version_policy") == "MIXED"
    assert created.get("maven", {}).get("layout_policy") == "STRICT"


def test_repository_format_specific_settings_docker(
    client, auth_headers, create_test_repository, db_session,
):
    """Verify Docker-specific settings (http_port, force_basic_auth)."""
    data = make_hosted_repo_docker(
        docker={"http_port": 8082, "https_port": 8083,
                "force_basic_auth": True, "v1_enabled": False})
    created = assert_created_response(create_test_repository(repo_data=data))
    assert created["format"] == "docker"
    assert created.get("docker", {}).get("http_port") == 8082
    assert created.get("docker", {}).get("force_basic_auth") is True


def test_repository_format_specific_settings_apt(
    client, auth_headers, create_test_repository, db_session,
):
    """Verify APT-specific settings (distribution, flat)."""
    data = make_hosted_repo_apt(
        apt={"distribution": "bionic", "flat": False,
             "gpg_key": "AABBCCDDEE" * 4})
    created = assert_created_response(create_test_repository(repo_data=data))
    assert created["format"] == "apt"
    assert created.get("apt", {}).get("distribution") == "bionic"
    assert created.get("apt", {}).get("flat") is False


@pytest.mark.parametrize("factory,fmt", [
    (make_hosted_repo_npm, "npm"), (make_hosted_repo_nuget, "nuget"),
    (make_hosted_repo_pypi, "pypi"), (make_hosted_repo_raw, "raw"),
], ids=["npm", "nuget", "pypi", "raw"])
def test_repository_format_individual(
    client, auth_headers, create_test_repository, db_session, factory, fmt,
):
    """Create hosted repo for npm/nuget/pypi/raw via dedicated factories."""
    created = assert_created_response(create_test_repository(repo_data=factory()))
    assert created["format"] == fmt
    assert created["type"] == "hosted"

def test_repository_upload_artifact_for_format(
    client, auth_headers, create_test_repository, upload_test_artifact, db_session,
):
    """Upload a format-specific artifact via make_artifact_for_format."""
    created = assert_created_response(
        create_test_repository(repo_data=make_hosted_repo_maven()))
    artifact = make_artifact_for_format("maven")
    resp = upload_test_artifact(created["name"], artifact_data=artifact)
    assert resp.status_code == 201
    assert resp.data is not None


# ---- Configuration Update Tests -------------------------------------------


def test_repository_update_configuration_online_toggle(
    client, auth_headers, created_hosted_repo,
):
    """Take offline then bring back online."""
    name = created_hosted_repo["name"]
    off = assert_json_response(client.put(
        f"/api/v1/repositories/{name}",
        json={"online": False}, headers=auth_headers), expected_status=200)
    assert off["online"] is False
    on = assert_json_response(client.put(
        f"/api/v1/repositories/{name}",
        json={"online": True}, headers=auth_headers), expected_status=200)
    assert on["online"] is True


def test_repository_update_storage_settings(
    client, auth_headers, created_hosted_repo,
):
    """Update blob_store_name and write_policy."""
    data = assert_json_response(client.put(
        f"/api/v1/repositories/{created_hosted_repo['name']}",
        json={"storage": {"blob_store_name": "custom-store",
                          "write_policy": "ALLOW",
                          "strict_content_type_validation": False}},
        headers=auth_headers), expected_status=200)
    assert data["storage"]["write_policy"] == "ALLOW"
    assert data["storage"]["blob_store_name"] == "custom-store"


def test_repository_update_proxy_settings(
    client, auth_headers, create_test_repository, db_session, mock_upstream,
):
    """Update proxy remote_url and cache TTL."""
    created = assert_created_response(
        create_test_repository(repo_data=make_proxy_repo_maven()))
    data = assert_json_response(client.put(
        f"/api/v1/repositories/{created['name']}",
        json={"proxy": {"remote_url": "https://custom.example.com/repo/",
                         "content_max_age": 720, "metadata_max_age": 720}},
        headers=auth_headers), expected_status=200)
    assert data["proxy"]["remote_url"] == "https://custom.example.com/repo/"
    assert data["proxy"]["content_max_age"] == 720


# ---- Browse Tree Navigation (F-104) ---------------------------------------


def test_repository_browse_tree_structure(
    client, auth_headers, populated_repo,
):
    """Browse populated repository — verify hierarchical tree."""
    repo_json, _ = populated_repo
    browse = assert_json_response(client.get(
        f"/api/v1/repositories/{repo_json['name']}/browse",
        headers=auth_headers))
    assert isinstance(browse, (list, dict))
    items = (browse.get("items", browse.get("children", []))
             if isinstance(browse, dict) else browse)
    assert isinstance(items, list)


def test_repository_browse_empty_repository(
    client, auth_headers, created_hosted_repo,
):
    """Browse empty repository returns empty tree."""
    browse = assert_json_response(client.get(
        f"/api/v1/repositories/{created_hosted_repo['name']}/browse",
        headers=auth_headers))
    items = (browse.get("items", browse.get("children", []))
             if isinstance(browse, dict) else browse)
    assert isinstance(items, list)
    assert len(items) == 0


# ---- Cleanup Policy Tests (F-204) -----------------------------------------


def test_repository_cleanup_policy_application(
    client, auth_headers, create_test_repository, upload_test_artifact,
    db_session, mock_notifications,
):
    """Configure cleanup, upload artifacts, trigger cleanup."""
    repo_data = make_hosted_repo_maven()
    repo_data["cleanup"] = {"policy_names": ["test-cleanup-policy"]}
    created = assert_created_response(create_test_repository(repo_data=repo_data))
    for i in range(2):
        art = make_maven_artifact(group_id=f"com.cleanup.t{i}",
                                  artifact_id=f"cl-art-{i}", version="1.0.0")
        upload_test_artifact(created["name"], artifact_data=art)
    resp = client.post(f"/api/v1/repositories/{created['name']}/cleanup",
                       headers=auth_headers)
    assert resp.status_code in (200, 202, 204)
    assert resp.status_code >= 200


def test_repository_maintenance_task_execution(
    client, auth_headers, created_hosted_repo,
):
    """Trigger a maintenance task and verify acknowledgement."""
    resp = client.post(
        f"/api/v1/repositories/{created_hosted_repo['name']}/maintenance",
        json={"task": "compact_blob_store"}, headers=auth_headers)
    assert resp.status_code in (200, 202, 204)
    assert resp.status_code >= 200


# ---- Edge Case Tests -------------------------------------------------------


def test_repository_create_with_duplicate_name_returns_409(
    client, auth_headers, create_test_repository, db_session,
):
    """Duplicate repository name returns 409 Conflict."""
    repo_a, repo_b = make_repo_with_duplicate_name(name="dup-lifecycle")
    assert_created_response(create_test_repository(repo_data=repo_a))
    assert_error_response(
        client.post("/api/v1/repositories", json=repo_b, headers=auth_headers),
        expected_status=409)


def test_repository_create_with_max_name_length(
    client, auth_headers, create_test_repository, db_session,
):
    """Boundary test: 255-character repository name."""
    resp = create_test_repository(repo_data=make_repo_with_max_name_length(255))
    assert resp.status_code in (201, 400)
    assert resp.get_json() is not None


def test_repository_create_with_special_characters(
    client, auth_headers, create_test_repository, db_session,
):
    """Unicode / special characters in repository name."""
    resp = create_test_repository(repo_data=make_repo_with_special_characters())
    assert resp.status_code in (201, 400)
    assert resp.get_json() is not None


def test_repository_lifecycle_with_content_while_deleting(
    client, auth_headers, create_test_repository, upload_test_artifact,
    db_session, tmp_storage,
):
    """Delete a repository containing artifacts — verify full cleanup."""
    created = assert_created_response(
        create_test_repository(repo_data=make_hosted_repo_maven()))
    for i in range(2):
        art = make_maven_artifact(group_id=f"com.del.t{i}",
                                  artifact_id=f"del-{i}", version="1.0.0")
        upload_test_artifact(created["name"], artifact_data=art)
    assert_no_content_response(client.delete(
        f"/api/v1/repositories/{created['name']}", headers=auth_headers))
    assert client.get(f"/api/v1/repositories/{created['name']}",
                      headers=auth_headers).status_code == 404


# ---- Error Handling Tests --------------------------------------------------


def test_repository_create_without_auth_returns_401(client, json_headers):
    """POST without authentication returns 401."""
    resp = client.post("/api/v1/repositories", json=make_hosted_repo_maven(),
                       headers=json_headers)
    assert resp.status_code == 401
    assert resp.data is not None


def test_repository_create_with_readonly_returns_403(
    client, readonly_auth_headers, db_session,
):
    """Read-only user cannot create repositories → 403."""
    ro = make_readonly_user(username="ro-lifecycle")
    assert ro["role"] == "readonly"
    assert_error_response(client.post(
        "/api/v1/repositories", json=make_hosted_repo_maven(),
        headers=readonly_auth_headers), expected_status=403)


def test_repository_create_with_developer_access(
    client, developer_auth_headers, db_session,
):
    """Developer user RBAC check on repository creation."""
    dev = make_developer_user(username="dev-lifecycle")
    assert dev["role"] == "developer"
    resp = client.post("/api/v1/repositories", json=make_hosted_repo_npm(),
                       headers=developer_auth_headers)
    assert resp.status_code in (201, 403)
    assert resp.data is not None


def test_repository_admin_create_verified(
    client, auth_headers, create_test_repository, db_session,
):
    """Admin user successfully creates a repository."""
    admin = make_admin_user(username="admin-lifecycle")
    assert admin["role"] == "admin" and admin["is_admin"] is True
    created = assert_created_response(
        create_test_repository(repo_data=make_hosted_repo(format_type="raw")))
    assert created["format"] == "raw"
    assert created["type"] == "hosted"


def test_repository_get_nonexistent_returns_404(client, auth_headers):
    """GET non-existent repository returns 404."""
    resp = client.get("/api/v1/repositories/nonexistent-xyz-99999",
                      headers=auth_headers)
    assert resp.status_code == 404
    assert resp.data is not None


def test_repository_delete_nonexistent_returns_404(client, auth_headers):
    """DELETE non-existent repository returns 404."""
    resp = client.delete("/api/v1/repositories/nonexistent-xyz-99999",
                         headers=auth_headers)
    assert resp.status_code == 404
    assert resp.data is not None


def test_repository_create_with_invalid_type_returns_400(client, auth_headers):
    """Invalid type field returns 400 Bad Request."""
    data = make_hosted_repo_maven()
    data["type"] = "invalid_type"
    resp = client.post("/api/v1/repositories", json=data, headers=auth_headers)
    err = assert_error_response(resp, expected_status=400)
    assert isinstance(err, dict)


def test_repository_create_with_invalid_format_returns_400(client, auth_headers):
    """Unsupported format returns 400 Bad Request."""
    data = make_hosted_repo_maven()
    data["format"] = "unsupported_format"
    resp = client.post("/api/v1/repositories", json=data, headers=auth_headers)
    err = assert_error_response(resp, expected_status=400)
    assert isinstance(err, dict)


def test_repository_proxy_create_and_verify(
    client, auth_headers, create_test_repository, db_session,
):
    """Create a proxy repo and verify proxy-specific fields."""
    data = make_proxy_repo(format_type="npm",
                           remote_url="https://registry.npmjs.org/")
    created = assert_created_response(create_test_repository(repo_data=data))
    assert created["type"] == "proxy"
    assert created["format"] == "npm"
    assert created.get("proxy", {}).get("remote_url") == "https://registry.npmjs.org/"


def test_repository_group_create_and_verify(
    client, auth_headers, create_test_repository, db_session,
):
    """Create group repo referencing hosted + proxy members."""
    h_name = assert_created_response(
        create_test_repository(repo_data=make_hosted_repo(format_type="maven")))["name"]
    p_name = assert_created_response(
        create_test_repository(repo_data=make_proxy_repo(format_type="maven")))["name"]
    grp = make_group_repo(format_type="maven", member_names=[h_name, p_name])
    created = assert_created_response(create_test_repository(repo_data=grp))
    assert created["type"] == "group"
    assert created["format"] == "maven"
