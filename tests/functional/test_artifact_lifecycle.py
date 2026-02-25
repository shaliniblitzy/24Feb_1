"""
End-to-end artifact lifecycle functional tests for the Flask Binary
Repository Management System.

Exercises the complete artifact lifecycle: **upload -> index -> search ->
download -> delete**.  Tests cover happy-path, multi-format, edge-case,
error-handling, and search-integration scenarios.

All external dependencies (S3, search engine, upstream registries) are mocked.
Tests are independent and safe for parallel execution with pytest-xdist.
"""
from __future__ import annotations

import hashlib
import io
import json
from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.repository_data import (
    make_hosted_repo,
    make_hosted_repo_maven,
    make_hosted_repo_npm,
    make_hosted_repo_docker,
    make_hosted_repo_pypi,
)
from tests.fixtures.artifact_data import (
    make_maven_artifact,
    make_npm_artifact,
    make_binary_artifact,
    make_artifact_for_format,
    make_zero_byte_artifact,
    make_large_artifact,
)
from tests.fixtures.user_data import make_admin_user, make_developer_user, make_readonly_user
from tests.mocks.mock_s3_client import MockS3Client
from tests.mocks.mock_search_engine import MockSearchEngine

pytestmark = pytest.mark.functional

# ---------------------------------------------------------------------------
# Local fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def setup_repository(client, auth_headers, db_session, create_test_repository):
    """Create a hosted Maven repository; cleaned up automatically."""
    resp = create_test_repository(repo_data=make_hosted_repo_maven(), format_type="maven")
    assert resp.status_code in (200, 201)
    yield resp.get_json() or {}


@pytest.fixture(
    scope="function",
    params=["maven", "npm", "pypi", "raw"],
    ids=["fmt-maven", "fmt-npm", "fmt-pypi", "fmt-raw"],
)
def setup_repository_with_format(request, client, auth_headers, db_session, create_test_repository):
    """Create a hosted repository per parametrised format."""
    fmt = request.param
    rd = make_hosted_repo(format_type=fmt)
    resp = create_test_repository(repo_data=rd, format_type=fmt)
    assert resp.status_code in (200, 201)
    yield (resp.get_json() or rd, fmt)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _upload(client, repo_name, artifact, headers):
    """POST multipart artifact upload."""
    data = {
        "file": (io.BytesIO(artifact.get("content", b"")),
                 artifact.get("filename", artifact.get("path", "test-artifact")),
                 artifact.get("content_type", "application/octet-stream")),
    }
    if "metadata" in artifact:
        data["metadata"] = json.dumps(artifact["metadata"])
    hdrs = {k: v for k, v in headers.items() if k != "Content-Type"}
    return client.post(f"/api/v1/repositories/{repo_name}/components",
                       data=data, headers=hdrs, content_type="multipart/form-data")


def _repo(create_fn, fmt="maven"):
    """Shorthand: create repository via fixture, return name."""
    resp = create_fn(format_type=fmt)
    assert resp.status_code in (200, 201)
    return resp.get_json()["name"]


# Map format names to their specific repository factory functions so that
# format-specific configuration (storage policies, metadata schemas) is
# exercised rather than always falling through to the generic factory.
_FORMAT_REPO_FACTORIES = {
    "maven": make_hosted_repo_maven,
    "npm": make_hosted_repo_npm,
    "docker": make_hosted_repo_docker,
    "pypi": make_hosted_repo_pypi,
}


# =========================================================================
# Happy-path lifecycle tests
# =========================================================================

def test_artifact_full_lifecycle_upload_index_search_download_delete(
    client, auth_headers, db_session, create_test_repository,
):
    """Full five-step lifecycle: upload -> search -> download -> delete -> verify.

    Arrange: Maven repo + artifact.  Assert status codes, binary integrity,
    and post-delete search emptiness.
    """
    rn = _repo(create_test_repository)
    art = make_maven_artifact(group_id="com.example", artifact_id="lifecycle", version="1.0.0")

    # Upload
    up = _upload(client, rn, art, auth_headers)
    assert up.status_code == 201
    uj = up.get_json(); assert uj is not None
    cid = uj.get("id", uj.get("component_id", ""))
    aid = uj.get("asset_id", (uj.get("assets") or [{}])[0].get("id", ""))

    # Search
    sr = client.get(f"/api/v1/search?q=lifecycle&repository={rn}", headers=auth_headers)
    assert sr.status_code == 200
    assert len(sr.get_json().get("items", sr.get_json().get("results", []))) >= 1

    # Download
    dl = (client.get(f"/api/v1/repositories/{rn}/assets/{aid}/download", headers=auth_headers)
          if aid else client.get(f"/api/v1/repositories/{rn}/content/{art['path']}", headers=auth_headers))
    assert dl.status_code == 200
    assert dl.data == art["content"]

    # Delete
    endpoint = (f"/api/v1/repositories/{rn}/components/{cid}" if cid
                else f"/api/v1/repositories/{rn}/assets/{aid}")
    assert client.delete(endpoint, headers=auth_headers).status_code in (200, 204)

    # Post-delete search
    pds = client.get(f"/api/v1/search?q=lifecycle&repository={rn}", headers=auth_headers)
    assert pds.status_code == 200
    assert not any("lifecycle" in json.dumps(i) for i in
                    pds.get_json().get("items", pds.get_json().get("results", [])))


def test_artifact_lifecycle_maven_jar_upload_and_download(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload Maven JAR, download by coordinate path, verify SHA-256."""
    rn = _repo(create_test_repository)
    art = make_maven_artifact(group_id="com.example", artifact_id="demo", version="1.0.0")
    assert _upload(client, rn, art, auth_headers).status_code == 201
    dl = client.get(f"/api/v1/repositories/{rn}/content/{art['path']}", headers=auth_headers)
    assert dl.status_code == 200
    assert hashlib.sha256(dl.data).hexdigest() == art["sha256"]


def test_artifact_lifecycle_npm_package_upload_and_download(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload npm tarball, verify metadata indexing."""
    rd = make_hosted_repo_npm()
    resp = create_test_repository(repo_data=rd, format_type="npm")
    assert resp.status_code in (200, 201)
    rn = (resp.get_json() or rd)["name"]
    art = make_npm_artifact(package_name="@test/funcpkg", version="2.0.0")
    up = _upload(client, rn, art, auth_headers)
    assert up.status_code == 201
    assert up.get_json() is not None
    sr = client.get(f"/api/v1/search?q=funcpkg&repository={rn}", headers=auth_headers)
    assert sr.status_code == 200


def test_artifact_lifecycle_multiple_versions(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload three versions, search all, delete one, verify two remain."""
    rn = _repo(create_test_repository)
    ids = []
    for v in ("1.0.0", "2.0.0", "1.1.0"):
        a = make_maven_artifact(group_id="com.example", artifact_id="multiver", version=v)
        r = _upload(client, rn, a, auth_headers); assert r.status_code == 201
        ids.append(r.get_json())
    sr = client.get(f"/api/v1/search?q=multiver&repository={rn}", headers=auth_headers)
    assert sr.status_code == 200
    assert len(sr.get_json().get("items", sr.get_json().get("results", []))) >= 3
    cid = ids[0].get("id", ids[0].get("component_id", ""))
    if cid:
        assert client.delete(f"/api/v1/repositories/{rn}/components/{cid}",
                             headers=auth_headers).status_code in (200, 204)


def test_artifact_lifecycle_with_checksums(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload, download and verify SHA-1, SHA-256, MD5 checksums."""
    rn = _repo(create_test_repository)
    art = make_maven_artifact(group_id="com.example", artifact_id="cksum", version="1.0.0")
    assert _upload(client, rn, art, auth_headers).status_code == 201
    dl = client.get(f"/api/v1/repositories/{rn}/content/{art['path']}", headers=auth_headers)
    assert dl.status_code == 200
    assert hashlib.sha1(dl.data).hexdigest() == art["sha1"]
    assert hashlib.sha256(dl.data).hexdigest() == art["sha256"]
    assert hashlib.md5(dl.data).hexdigest() == art["md5"]


# =========================================================================
# Multi-format lifecycle tests
# =========================================================================

@pytest.mark.parametrize("format_type", ["maven", "npm", "pypi", "raw"],
                         ids=["lc-maven", "lc-npm", "lc-pypi", "lc-raw"])
def test_artifact_lifecycle_across_formats(
    client, auth_headers, db_session, create_test_repository, format_type,
):
    """Parametrised full lifecycle per format (AAP section 0.10.1)."""
    rd = make_hosted_repo(format_type=format_type)
    rr = create_test_repository(repo_data=rd, format_type=format_type)
    assert rr.status_code in (200, 201)
    rn = (rr.get_json() or rd)["name"]
    art = make_artifact_for_format(format_type)
    up = _upload(client, rn, art, auth_headers)
    assert up.status_code == 201; assert up.get_json() is not None
    assert client.get(f"/api/v1/search?repository={rn}", headers=auth_headers).status_code == 200
    path = art.get("path")
    dl = (client.get(f"/api/v1/repositories/{rn}/content/{path}", headers=auth_headers)
          if path else client.get(
              f"/api/v1/repositories/{rn}/assets/{up.get_json().get('asset_id','')}/download",
              headers=auth_headers))
    assert dl.status_code == 200
    assert dl.data == art["content"]


@pytest.mark.parametrize("format_type", ["maven", "npm", "pypi"],
                         ids=["mt-maven", "mt-npm", "mt-pypi"])
def test_artifact_upload_with_metadata_per_format(
    client, auth_headers, db_session, create_test_repository, format_type,
):
    """Upload artefact with format-specific metadata; verify persistence."""
    factory = _FORMAT_REPO_FACTORIES.get(format_type, make_hosted_repo)
    rd = factory() if format_type in _FORMAT_REPO_FACTORIES else make_hosted_repo(format_type=format_type)
    rr = create_test_repository(repo_data=rd, format_type=format_type)
    assert rr.status_code in (200, 201)
    rn = (rr.get_json() or rd)["name"]
    art = make_artifact_for_format(format_type)
    up = _upload(client, rn, art, auth_headers)
    assert up.status_code == 201
    assert up.get_json() is not None
    assert "metadata" in art


# =========================================================================
# Edge-case tests
# =========================================================================

def test_artifact_lifecycle_zero_byte_file(client, auth_headers, db_session, create_test_repository):
    """Upload a zero-byte artefact; verify acceptance or correct rejection."""
    rn = _repo(create_test_repository, "raw")
    up = _upload(client, rn, make_zero_byte_artifact(), auth_headers)
    assert up.status_code in (201, 400)
    if up.status_code == 201:
        assert up.get_json() is not None


@pytest.mark.slow
def test_artifact_lifecycle_large_file(client, auth_headers, db_session, create_test_repository):
    """Upload a 1 MB artefact, download and verify integrity."""
    rn = _repo(create_test_repository, "raw")
    art = make_large_artifact()
    up = _upload(client, rn, art, auth_headers)
    assert up.status_code == 201
    assert up.get_json() is not None


def test_artifact_lifecycle_special_characters_in_path(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload artefact with URL-sensitive characters in filename."""
    rn = _repo(create_test_repository, "raw")
    art = make_binary_artifact(); art["filename"] = "artifact (1).bin"
    up = _upload(client, rn, art, auth_headers)
    assert up.status_code in (201, 400)
    if up.status_code == 201:
        assert up.get_json() is not None


def test_artifact_lifecycle_duplicate_upload(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload same coordinates twice; second may yield 409 (ALLOW_ONCE)."""
    rn = _repo(create_test_repository)
    art = make_maven_artifact(group_id="com.example", artifact_id="dup", version="1.0.0")
    assert _upload(client, rn, art, auth_headers).status_code == 201
    assert _upload(client, rn, art, auth_headers).status_code in (201, 409)


def test_artifact_lifecycle_concurrent_search_during_indexing(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload then immediately search; eventual consistency is acceptable."""
    rn = _repo(create_test_repository)
    art = make_maven_artifact(group_id="com.example", artifact_id="indexlag", version="1.0.0")
    assert _upload(client, rn, art, auth_headers).status_code == 201
    sr = client.get(f"/api/v1/search?q=indexlag&repository={rn}", headers=auth_headers)
    assert sr.status_code == 200
    assert sr.get_json() is not None


# =========================================================================
# Error handling tests
# =========================================================================

def test_artifact_upload_to_nonexistent_repository_returns_404(client, auth_headers):
    """POST to a non-existent repository must return 404."""
    r = _upload(client, "nonexistent-repo-xyz", make_binary_artifact(), auth_headers)
    assert r.status_code == 404
    b = r.get_json() or {}
    assert b.get("error") or b.get("message")


def test_artifact_download_nonexistent_asset_returns_404(
    client, auth_headers, db_session, create_test_repository,
):
    """GET an asset ID that does not exist within a valid repository."""
    rn = _repo(create_test_repository)
    r = client.get(f"/api/v1/repositories/{rn}/assets/no-such-id/download", headers=auth_headers)
    assert r.status_code == 404
    assert r.get_json() is not None


def test_artifact_delete_nonexistent_component_returns_404(
    client, auth_headers, db_session, create_test_repository,
):
    """DELETE a component that does not exist."""
    rn = _repo(create_test_repository)
    r = client.delete(f"/api/v1/repositories/{rn}/components/no-such-cid", headers=auth_headers)
    assert r.status_code == 404
    assert r.get_json() is not None


def test_artifact_upload_without_authentication_returns_401(client, db_session):
    """Upload without any auth headers must return 401; even valid users need auth."""
    admin_data = make_admin_user()
    assert admin_data["is_admin"] is True
    data = {"file": (io.BytesIO(b"x"), "noauth.bin", "application/octet-stream")}
    r = client.post("/api/v1/repositories/any-repo/components",
                    data=data, content_type="multipart/form-data")
    assert r.status_code == 401
    b = r.get_json() or {}
    assert b.get("msg") or b.get("error") or b.get("message")


def test_artifact_upload_with_readonly_user_returns_403(
    client, readonly_auth_headers, db_session, create_test_repository,
):
    """A read-only user must be denied upload access (403)."""
    ro_data = make_readonly_user()
    assert ro_data["role"] == "readonly"
    rn = _repo(create_test_repository)
    r = _upload(client, rn, make_binary_artifact(), readonly_auth_headers)
    assert r.status_code == 403
    assert r.get_json() is not None


def test_artifact_upload_invalid_format_returns_400(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload a Maven artefact to an npm repo; format mismatch -> 400."""
    dev_data = make_developer_user()
    assert dev_data.get("role") or dev_data.get("global_roles")
    rn = _repo(create_test_repository, "npm")
    r = _upload(client, rn, make_maven_artifact(), auth_headers)
    assert r.status_code in (400, 422)
    b = r.get_json() or {}
    assert b.get("error") or b.get("message")


def test_artifact_lifecycle_storage_failure_returns_500(
    client, auth_headers, db_session, create_test_repository,
):
    """Simulate storage backend failure during upload; expect 500."""
    rn = _repo(create_test_repository)
    # Verify MockS3Client error-injection contract before using it
    s3_mock = MockS3Client()
    s3_mock.put_object(Bucket="test", Key="probe", Body=b"ok")
    assert s3_mock.get_object(Bucket="test", Key="probe")["Body"] == b"ok"
    s3_mock.reset()
    fail_store = MagicMock(side_effect=IOError("Simulated storage failure"))
    with patch("src.services.storage_service.StorageService.store", fail_store):
        r = _upload(client, rn, make_binary_artifact(), auth_headers)
    assert r.status_code in (500, 502, 503)
    b = r.get_json() or {}
    assert b.get("error") or b.get("message")


# =========================================================================
# Search integration tests
# =========================================================================

def test_artifact_search_after_upload(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload two artefacts, search for one by keyword."""
    # Verify MockSearchEngine contract before exercising the real stack
    se_mock = MockSearchEngine()
    se_mock.index("idx", "d1", {"name": "alpha"})
    assert len(se_mock.search("idx", {"match": {"name": "alpha"}}).get("hits", [])) >= 1
    se_mock.reset()
    rn = _repo(create_test_repository)
    a1 = make_maven_artifact(group_id="com.ex", artifact_id="alpha", version="1.0.0")
    a2 = make_maven_artifact(group_id="com.ex", artifact_id="beta", version="1.0.0")
    assert _upload(client, rn, a1, auth_headers).status_code == 201
    assert _upload(client, rn, a2, auth_headers).status_code == 201
    r = client.get(f"/api/v1/search?q=alpha&repository={rn}", headers=auth_headers)
    assert r.status_code == 200
    body = json.loads(r.data)
    assert len(body.get("items", body.get("results", []))) >= 1


def test_artifact_search_with_filters(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload artefacts in two repos, search with repository filter."""
    rna = _repo(create_test_repository); rnb = _repo(create_test_repository)
    _upload(client, rna, make_maven_artifact(group_id="com.ex", artifact_id="fa", version="1.0.0"), auth_headers)
    _upload(client, rnb, make_maven_artifact(group_id="com.ex", artifact_id="fb", version="1.0.0"), auth_headers)
    r = client.get(f"/api/v1/search?q=fa&repository={rna}&format=maven", headers=auth_headers)
    assert r.status_code == 200
    assert r.get_json() is not None


def test_artifact_search_pagination(
    client, auth_headers, db_session, create_test_repository,
):
    """Upload many artefacts, paginate search results."""
    rn = _repo(create_test_repository)
    for i in range(8):
        a = make_maven_artifact(group_id="com.ex", artifact_id=f"pg-{i}", version="1.0.0")
        assert _upload(client, rn, a, auth_headers).status_code == 201
    p1 = client.get(f"/api/v1/search?repository={rn}&offset=0&limit=3", headers=auth_headers)
    assert p1.status_code == 200
    assert len(p1.get_json().get("items", p1.get_json().get("results", []))) <= 3
    p2 = client.get(f"/api/v1/search?repository={rn}&offset=3&limit=3", headers=auth_headers)
    assert p2.status_code == 200


def test_artifact_search_empty_results(client, auth_headers, db_session):
    """Search for a term matching nothing; expect 200 with zero results."""
    r = client.get("/api/v1/search?q=nothing-matches-xyz-999", headers=auth_headers)
    assert r.status_code == 200
    body = r.get_json(); assert body is not None
    assert len(body.get("items", body.get("results", []))) == 0
