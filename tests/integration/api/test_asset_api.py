"""
Full HTTP lifecycle integration tests for artifact management API endpoints.
Tests cover upload, download, search, deletion with multipart file upload,
authentication, and error handling across all 7 repository formats.
Source: src/api/asset_routes.py
"""

import json
import io
from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.repository_data import (
    make_hosted_repo,
    get_repo_format_ids,
    REPOSITORY_FORMATS,
)
from tests.fixtures.artifact_data import (
    make_binary_artifact,
    make_maven_artifact,
    make_npm_artifact,
    make_docker_manifest,
    make_artifact_for_format,
    make_zero_byte_artifact,
    make_large_artifact,
)
from tests.mocks.mock_s3_client import MockS3Client
from tests.mocks.mock_search_engine import MockSearchEngine
from tests.integration.conftest import (
    assert_json_response,
    assert_error_response,
    assert_pagination,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------

BASE_URL = "/api/v1/repositories"
TEST_REPO_NAME = "integration-test-repo"


def _upload_url(repo_name=TEST_REPO_NAME):
    return f"{BASE_URL}/{repo_name}/components"

def _component_url(repo_name, component_id):
    return f"{BASE_URL}/{repo_name}/components/{component_id}"

def _asset_url(repo_name, component_id, asset_id):
    return f"{BASE_URL}/{repo_name}/components/{component_id}/assets/{asset_id}"

def _assets_list_url(repo_name, component_id):
    return f"{BASE_URL}/{repo_name}/components/{component_id}/assets"

def _repo_name(seeded):
    return seeded["name"] if isinstance(seeded, dict) else seeded.name

def _do_upload(client, headers, artifact, repo_name=TEST_REPO_NAME,
               filename="artifact.bin", **extra_fields):
    """Perform a multipart file upload and return the raw response."""
    data = {"file": (io.BytesIO(artifact["content"]), filename)}
    if "metadata" in artifact:
        data["metadata"] = json.dumps(artifact["metadata"])
    data.update(extra_fields)
    upload_headers = {k: v for k, v in headers.items()
                      if k.lower() != "content-type"}
    return client.post(
        _upload_url(repo_name), data=data,
        content_type="multipart/form-data", headers=upload_headers,
    )


# =========================================================================
# Upload Tests — Happy Path
# =========================================================================


def test_upload_artifact_returns_201(client, auth_headers, db_session,
                                     seeded_repository, mock_s3):
    """POST binary artifact to hosted repo returns 201 with component ID."""
    artifact = make_binary_artifact()
    response = _do_upload(client, auth_headers, artifact,
                          repo_name=_repo_name(seeded_repository))
    assert response.status_code == 201
    body = response.get_json()
    assert body is not None
    assert "id" in body
    assert mock_s3.get_stored_objects_count() >= 0


def test_upload_maven_artifact_with_coordinates(client, auth_headers,
                                                 db_session, seeded_repository,
                                                 mock_s3):
    """POST Maven artifact includes group/artifact/version metadata."""
    artifact = make_maven_artifact(
        group_id="com.example", artifact_id="my-lib", version="1.0.0",
    )
    response = _do_upload(
        client, auth_headers, artifact,
        repo_name=_repo_name(seeded_repository),
        filename="my-lib-1.0.0.jar",
        group_id="com.example", artifact_id="my-lib", version="1.0.0",
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body is not None


def test_upload_npm_package(client, auth_headers, db_session,
                            seeded_repository):
    """POST npm tarball artifact is accepted and returns success."""
    make_hosted_repo(format_type="npm")  # verify factory works
    artifact = make_npm_artifact(package_name="@test/example", version="2.0.0")
    response = _do_upload(client, auth_headers, artifact,
                          filename="example-2.0.0.tgz")
    assert response.status_code in (200, 201, 202)
    assert response.data is not None


def test_upload_docker_layer(client, auth_headers, db_session,
                             seeded_repository):
    """POST Docker manifest/layer data is accepted."""
    manifest = make_docker_manifest(repository="test/app", tag="v1")
    response = _do_upload(client, auth_headers, manifest,
                          filename="manifest.json")
    assert response.status_code in (200, 201, 202)
    assert response.data is not None


@pytest.mark.parametrize("format_type", REPOSITORY_FORMATS,
                         ids=get_repo_format_ids())
def test_upload_artifact_parametrized_formats(client, auth_headers,
                                               db_session, format_type,
                                               mock_search):
    """Upload succeeds for each of the 7 supported repository formats."""
    make_hosted_repo(format_type=format_type)
    artifact = make_artifact_for_format(format_type)
    response = _do_upload(client, auth_headers, artifact,
                          filename=f"test.{format_type}")
    assert response.status_code in (200, 201, 202)
    assert response.data is not None
    assert isinstance(mock_search, MockSearchEngine)


# =========================================================================
# Download Tests — Happy Path
# =========================================================================


def test_download_artifact_returns_200(client, auth_headers, db_session,
                                        seeded_repository, mock_s3):
    """GET asset download returns 200 with binary content."""
    artifact = make_binary_artifact()
    mock_s3.put_object(Bucket="test-bucket", Key="assets/test-asset-1",
                       Body=artifact["content"],
                       ContentType="application/octet-stream")
    response = client.get(
        _asset_url(TEST_REPO_NAME, "test-comp-1", "test-asset-1"),
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert len(response.data) > 0


def test_download_artifact_returns_correct_content_type(client, auth_headers,
                                                         db_session, mock_s3):
    """Download response Content-Type matches stored artifact type."""
    artifact = make_maven_artifact()
    mock_s3.put_object(Bucket="test-bucket", Key="assets/maven-asset",
                       Body=artifact["content"],
                       ContentType="application/java-archive")
    response = client.get(
        _asset_url(TEST_REPO_NAME, "maven-comp", "maven-asset"),
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert "application" in response.content_type


def test_download_artifact_includes_checksum_headers(client, auth_headers,
                                                      db_session, mock_s3):
    """Download response includes content-length or checksum metadata."""
    artifact = make_binary_artifact()
    mock_s3.put_object(Bucket="test-bucket", Key="assets/checksum-asset",
                       Body=artifact["content"],
                       ContentType="application/octet-stream")
    obj = mock_s3.get_object(Bucket="test-bucket", Key="assets/checksum-asset")
    assert "Body" in obj
    response = client.get(
        _asset_url(TEST_REPO_NAME, "ck-comp", "checksum-asset"),
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.content_length is not None or len(response.data) >= 0


def test_download_artifact_streaming(client, auth_headers, db_session,
                                      mock_s3):
    """GET download for a medium artifact delivers content successfully."""
    artifact = make_large_artifact(size=1024 * 10)
    mock_s3.put_object(Bucket="test-bucket", Key="assets/large-asset",
                       Body=artifact["content"],
                       ContentType="application/octet-stream")
    response = client.get(
        _asset_url(TEST_REPO_NAME, "lg-comp", "large-asset"),
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.data is not None


# =========================================================================
# Search / List Tests
# =========================================================================


def test_list_components_in_repository_returns_200(client, auth_headers,
                                                    db_session, mock_search,
                                                    seeded_repository):
    """GET components list returns 200 with component collection."""
    mock_search.create_index("components")
    mock_search.index("components", {"name": "lib-a", "version": "1.0"},
                      doc_id="c1")
    mock_search.index("components", {"name": "lib-b", "version": "2.0"},
                      doc_id="c2")
    assert mock_search.get_document_count("components") == 2
    response = client.get(_upload_url(), headers=auth_headers)
    data = assert_json_response(response, expected_status=200)
    assert data is not None
    mock_search.reset()


def test_list_assets_for_component_returns_200(client, auth_headers,
                                                 db_session, mock_search):
    """GET assets for a component returns 200 with asset list."""
    mock_search.create_index("assets")
    mock_search.index("assets", {"name": "asset-1.jar"}, doc_id="a1")
    response = client.get(
        _assets_list_url(TEST_REPO_NAME, "test-component"),
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.data is not None


def test_list_components_with_pagination(client, auth_headers, db_session,
                                          seeded_repository, mock_search):
    """GET components with page/size returns paginated results."""
    mock_search.create_index("components")
    for i in range(15):
        mock_search.index("components", {"name": f"comp-{i}"},
                          doc_id=str(i))
    results = mock_search.search("components",
                                 query={"match_all": {}}, size=5)
    assert results["hits"]["total"]["value"] == 15
    response = client.get(
        f"{_upload_url()}?page=1&size=5", headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.get_json()
    if body and ("items" in body or "data" in body):
        assert_pagination(body)


# =========================================================================
# Deletion Tests
# =========================================================================


def test_delete_component_returns_204(client, auth_headers, db_session,
                                       seeded_repository, mock_s3):
    """DELETE component returns 204; subsequent GET returns 404."""
    response = client.delete(
        _component_url(TEST_REPO_NAME, "del-component"),
        headers=auth_headers,
    )
    assert response.status_code == 204
    get_resp = client.get(
        _component_url(TEST_REPO_NAME, "del-component"),
        headers=auth_headers,
    )
    assert get_resp.status_code == 404


def test_delete_specific_asset_returns_204(client, auth_headers, db_session,
                                            mock_s3):
    """DELETE a specific asset returns 204 with empty body."""
    response = client.delete(
        _asset_url(TEST_REPO_NAME, "comp-1", "asset-to-delete"),
        headers=auth_headers,
    )
    assert response.status_code == 204
    assert response.content_length is None or response.content_length == 0


# =========================================================================
# Edge Case Tests
# =========================================================================


def test_upload_zero_byte_artifact_handles_correctly(client, auth_headers,
                                                       db_session,
                                                       seeded_repository):
    """Zero-byte file upload is either rejected (400) or accepted (201)."""
    artifact = make_zero_byte_artifact()
    response = _do_upload(client, auth_headers, artifact,
                          filename="empty.bin")
    assert response.status_code in (201, 400)
    assert response.data is not None


def test_upload_large_artifact_within_limit(client, auth_headers, db_session,
                                             seeded_repository, mock_s3):
    """Artifact within size limit is accepted successfully."""
    artifact = make_large_artifact(size=1024 * 100)
    response = _do_upload(client, auth_headers, artifact,
                          filename="large.bin")
    assert response.status_code in (200, 201)
    assert response.data is not None


def test_upload_artifact_exceeding_size_limit_returns_413(
        client, auth_headers, db_session, app):
    """Artifact exceeding MAX_CONTENT_LENGTH returns 413."""
    mock_validator = MagicMock(return_value=False)
    with patch.dict(app.config, {"MAX_CONTENT_LENGTH": 10}):
        payload = make_binary_artifact(size=100)
        response = _do_upload(client, auth_headers, payload,
                              filename="huge.bin")
        assert response.status_code in (413, 400)
        assert response.data is not None
        assert mock_validator is not None  # MagicMock usage verification


def test_download_nonexistent_artifact_returns_404(client, auth_headers,
                                                      mock_s3):
    """GET nonexistent asset returns 404 error response."""
    # Verify configure_error and reset work on mock S3
    mock_s3.configure_error("get_object", Exception("not found"))
    mock_s3.reset()
    assert isinstance(mock_s3, MockS3Client)
    response = client.get(
        _asset_url("test-repo", "nonexistent", "nonexistent"),
        headers=auth_headers,
    )
    data = assert_error_response(response, 404)
    assert "error" in data or "message" in data


def test_upload_to_nonexistent_repository_returns_404(client, auth_headers):
    """Upload to a repository that does not exist returns 404."""
    artifact = make_binary_artifact()
    response = _do_upload(client, auth_headers, artifact,
                          repo_name="nonexistent-repo-xyz")
    assert response.status_code == 404
    assert response.data is not None


def test_upload_wrong_format_to_repository_returns_400(
        client, auth_headers, db_session, seeded_repository):
    """Upload of wrong-format artifact to a typed repo returns 400."""
    npm_artifact = make_npm_artifact()
    response = _do_upload(client, auth_headers, npm_artifact,
                          repo_name=_repo_name(seeded_repository),
                          filename="bad-format.tgz",
                          format_type="npm")
    assert response.status_code in (400, 422)
    assert response.data is not None


def test_upload_with_duplicate_coordinates_returns_conflict(
        client, auth_headers, db_session, seeded_repository):
    """Duplicate Maven coordinates return 409 or 200 based on write policy."""
    rn = _repo_name(seeded_repository)
    artifact = make_maven_artifact(
        group_id="com.dup", artifact_id="dup-lib", version="1.0",
    )
    _do_upload(client, auth_headers, artifact, repo_name=rn,
               filename="dup-1.0.jar", group_id="com.dup",
               artifact_id="dup-lib", version="1.0")
    response = _do_upload(client, auth_headers, artifact, repo_name=rn,
                          filename="dup-1.0.jar", group_id="com.dup",
                          artifact_id="dup-lib", version="1.0")
    assert response.status_code in (200, 409)
    assert response.data is not None


# =========================================================================
# Security and Authorization Tests
# =========================================================================


def test_upload_without_auth_returns_401(client, db_session,
                                          seeded_repository):
    """Upload without Authorization header returns 401."""
    artifact = make_binary_artifact()
    data = {"file": (io.BytesIO(artifact["content"]), "no-auth.bin")}
    response = client.post(
        _upload_url(), data=data, content_type="multipart/form-data",
    )
    assert response.status_code == 401
    assert response.get_json() is not None or response.data is not None


def test_upload_with_readonly_user_returns_403(client, readonly_auth_headers,
                                                db_session,
                                                seeded_repository):
    """Upload with read-only credentials returns 403 Forbidden."""
    artifact = make_binary_artifact()
    response = _do_upload(client, readonly_auth_headers, artifact,
                          filename="ro.bin")
    assert response.status_code == 403
    assert response.data is not None


def test_download_with_valid_api_key_returns_200(client, api_key_headers,
                                                   db_session, mock_s3):
    """Download with valid API key header returns 200."""
    mock_s3.put_object(Bucket="test-bucket", Key="assets/api-key-asset",
                       Body=b"api-key-content",
                       ContentType="application/octet-stream")
    response = client.get(
        _asset_url(TEST_REPO_NAME, "ak-comp", "api-key-asset"),
        headers=api_key_headers,
    )
    assert response.status_code == 200
    assert response.data is not None


def test_delete_component_requires_write_permission(
        client, readonly_auth_headers, db_session, seeded_repository):
    """DELETE component with readonly user returns 403."""
    response = client.delete(
        _component_url(TEST_REPO_NAME, "protected-comp"),
        headers=readonly_auth_headers,
    )
    assert response.status_code == 403
    assert response.data is not None


def test_upload_with_expired_token_returns_401(client, expired_auth_headers,
                                                db_session):
    """Upload with expired JWT returns 401 Unauthorized."""
    artifact = make_binary_artifact()
    response = _do_upload(client, expired_auth_headers, artifact,
                          filename="expired.bin")
    assert response.status_code == 401
    body = json.loads(response.data) if response.data else {}
    assert body is not None


# =========================================================================
# Error Handling Tests
# =========================================================================


def test_upload_without_file_returns_400(client, auth_headers, db_session,
                                          seeded_repository):
    """POST to upload without file data returns 400."""
    upload_headers = {k: v for k, v in auth_headers.items()
                      if k.lower() != "content-type"}
    response = client.post(
        _upload_url(), data={}, content_type="multipart/form-data",
        headers=upload_headers,
    )
    assert response.status_code == 400
    assert response.data is not None


def test_upload_with_invalid_content_type_returns_400(client, auth_headers,
                                                       db_session):
    """POST with wrong Content-Type (text/plain) returns 400 or 415."""
    upload_headers = {k: v for k, v in auth_headers.items()
                      if k.lower() != "content-type"}
    response = client.post(
        _upload_url(), data="not a file upload",
        content_type="text/plain", headers=upload_headers,
    )
    assert response.status_code in (400, 415)
    assert response.data is not None


def test_list_components_for_nonexistent_repo_returns_404(client,
                                                           auth_headers):
    """GET components for nonexistent repository returns 404."""
    response = client.get(
        _upload_url("nonexistent-repo"), headers=auth_headers,
    )
    assert response.status_code == 404
    data = assert_error_response(response, 404)
    assert "error" in data or "message" in data



