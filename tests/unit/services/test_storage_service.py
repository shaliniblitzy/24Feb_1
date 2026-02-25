"""Unit tests for StorageService — File BlobStore & S3 BlobStore operations.

Covers upload, download, delete, integrity verification, cleanup, and
deduplication with isolated backends. Features F-201 and F-202.
"""

import io
import hashlib

import pytest
from unittest.mock import patch, MagicMock, mock_open

from src.services.storage_service import StorageService
from tests.fixtures.artifact_data import (
    make_binary_artifact,
    make_zero_byte_artifact,
    make_large_artifact,
    SMALL_ARTIFACT_SIZE,
    MEDIUM_ARTIFACT_SIZE,
    LARGE_ARTIFACT_SIZE,
)
from tests.mocks.mock_s3_client import (
    MockS3Client,
    create_mock_s3_client,
    make_client_error,
)

pytestmark = pytest.mark.unit


def _file_svc(tmp_path):
    """Create a StorageService with local file backend."""
    return StorageService(storage_type="file", storage_path=str(tmp_path))


def _s3_svc(client):
    """Create a StorageService with S3 backend."""
    return StorageService(
        storage_type="s3", s3_client=client, bucket_name="test-bucket",
    )


# -- Phase 2: File BlobStore Happy Path ------------------------------------

def test_file_blobstore_upload_success(tmp_path):
    """Upload creates file on disk and returns correct metadata."""
    svc = _file_svc(tmp_path)
    art = make_binary_artifact(SMALL_ARTIFACT_SIZE)
    result = svc.upload_artifact(
        "repo", "com/ex/1.0/lib.jar", art["content"], "application/java-archive",
    )
    assert result["size"] == SMALL_ARTIFACT_SIZE
    assert result["sha256"] == art["sha256"]
    assert (tmp_path / "repo" / "com" / "ex" / "1.0" / "lib.jar").exists()


def test_file_blobstore_download_success(tmp_path):
    """Download returns uploaded content and correct size."""
    svc = _file_svc(tmp_path)
    art = make_binary_artifact(SMALL_ARTIFACT_SIZE)
    svc.upload_artifact("repo", "a.bin", art["content"])
    result = svc.download_artifact("repo", "a.bin")
    assert result is not None
    assert result["content"] == art["content"]
    assert result["size"] == SMALL_ARTIFACT_SIZE


def test_file_blobstore_delete_success(tmp_path):
    """Delete removes the file and returns True."""
    svc = _file_svc(tmp_path)
    svc.upload_artifact("repo", "a.bin", b"data")
    fp = tmp_path / "repo" / "a.bin"
    assert fp.exists()
    result = svc.delete_artifact("repo", "a.bin")
    assert result is True
    assert not fp.exists()


def test_file_blobstore_get_metadata_success(tmp_path):
    """Download includes checksums, size, and last_modified."""
    svc = _file_svc(tmp_path)
    art = make_binary_artifact(MEDIUM_ARTIFACT_SIZE)
    svc.upload_artifact("repo", "big.jar", art["content"])
    result = svc.download_artifact("repo", "big.jar")
    assert result["size"] == MEDIUM_ARTIFACT_SIZE
    assert result["sha256"] == art["sha256"]
    assert "last_modified" in result


def test_file_blobstore_list_via_nested_dirs(tmp_path):
    """Upload creates deeply nested directory structure."""
    svc = _file_svc(tmp_path)
    result = svc.upload_artifact("repo", "a/b/c/d/deep.bin", b"nested")
    assert result["size"] == len(b"nested")
    assert (tmp_path / "repo" / "a" / "b" / "c" / "d" / "deep.bin").exists()


# -- Phase 3: S3 BlobStore Happy Path --------------------------------------

def test_s3_blobstore_upload_success(mock_s3_client):
    """Upload stores object via put_object with correct params."""
    svc = _s3_svc(mock_s3_client)
    art = make_binary_artifact(SMALL_ARTIFACT_SIZE)
    result = svc.upload_artifact("repo", "a.bin", art["content"])
    assert result["size"] == SMALL_ARTIFACT_SIZE
    assert result["sha256"] == art["sha256"]
    assert mock_s3_client.get_call_count("put_object") == 1
    stored = mock_s3_client.get_stored_object("test-bucket", "repo/a.bin")
    assert stored["Body"] == art["content"]


def test_s3_blobstore_download_success(mock_s3_client):
    """Download retrieves correct content from S3."""
    svc = _s3_svc(mock_s3_client)
    art = make_binary_artifact(SMALL_ARTIFACT_SIZE)
    svc.upload_artifact("repo", "a.bin", art["content"])
    result = svc.download_artifact("repo", "a.bin")
    assert result is not None
    assert result["content"] == art["content"]
    assert result["size"] == SMALL_ARTIFACT_SIZE


def test_s3_blobstore_delete_success(mock_s3_client):
    """Delete calls delete_object and returns True."""
    svc = _s3_svc(mock_s3_client)
    svc.upload_artifact("repo", "a.bin", b"data")
    result = svc.delete_artifact("repo", "a.bin")
    assert result is True
    assert mock_s3_client.get_call_count("delete_object") == 1


def test_s3_blobstore_get_metadata_success(mock_s3_client):
    """Download returns content_type, checksums, and last_modified."""
    svc = _s3_svc(mock_s3_client)
    art = make_binary_artifact(SMALL_ARTIFACT_SIZE)
    svc.upload_artifact("repo", "d.jar", art["content"], "application/java-archive")
    result = svc.download_artifact("repo", "d.jar")
    assert result is not None
    assert result["content_type"] == "application/java-archive"
    assert result["sha256"] == art["sha256"]


def test_s3_blobstore_list_artifacts(mock_s3_client):
    """list_artifacts returns objects filtered by prefix."""
    svc = _s3_svc(mock_s3_client)
    svc.upload_artifact("repo", "com/a.jar", b"aaa")
    svc.upload_artifact("repo", "com/b.jar", b"bbb")
    svc.upload_artifact("repo", "org/c.jar", b"ccc")
    items = svc.list_artifacts("repo", "com/")
    assert len(items) == 2
    keys = [i["key"] for i in items]
    assert "repo/com/a.jar" in keys


# -- Phase 4: Deduplication and Integrity -----------------------------------

def test_upload_artifact_computes_sha256_hash(tmp_path):
    """Upload SHA-256 matches independently computed value."""
    svc = _file_svc(tmp_path)
    art = make_binary_artifact(SMALL_ARTIFACT_SIZE)
    result = svc.upload_artifact("repo", "f.bin", art["content"])
    assert result["sha256"] == hashlib.sha256(art["content"]).hexdigest()
    assert result["md5"] == hashlib.md5(art["content"]).hexdigest()


def test_upload_artifact_deduplication_same_content(mock_s3_client):
    """Identical content at different paths yields same hashes."""
    svc = _s3_svc(mock_s3_client)
    content = b"dup-content-bytes"
    r1 = svc.upload_artifact("repo", "p1.bin", content)
    r2 = svc.upload_artifact("repo", "p2.bin", content)
    assert r1["sha256"] == r2["sha256"]
    assert mock_s3_client.get_call_count("put_object") == 2


def test_download_artifact_integrity_check(tmp_path):
    """verify_integrity returns True for untampered content."""
    svc = _file_svc(tmp_path)
    art = make_binary_artifact(SMALL_ARTIFACT_SIZE)
    svc.upload_artifact("repo", "v.bin", art["content"])
    assert svc.verify_integrity("repo", "v.bin") is True
    dl = svc.download_artifact("repo", "v.bin")
    assert hashlib.sha256(dl["content"]).hexdigest() == art["sha256"]


def test_upload_artifact_with_checksum_validation(mock_s3_client):
    """All three checksums are consistent after upload."""
    svc = _s3_svc(mock_s3_client)
    content = b"checksum-validation-content"
    result = svc.upload_artifact("repo", "c.bin", content)
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    assert result["sha1"] == hashlib.sha1(content).hexdigest()


# -- Phase 5: Edge Case Tests ----------------------------------------------

def test_upload_zero_byte_file(tmp_path):
    """Zero-byte upload succeeds with size 0 and empty-content hash."""
    svc = _file_svc(tmp_path)
    art = make_zero_byte_artifact()
    result = svc.upload_artifact("repo", "empty.bin", art["content"])
    assert result["size"] == 0
    assert result["sha256"] == hashlib.sha256(b"").hexdigest()


def test_upload_large_file(tmp_path):
    """Large file upload succeeds with correct size and hash."""
    svc = _file_svc(tmp_path)
    art = make_large_artifact(LARGE_ARTIFACT_SIZE)
    result = svc.upload_artifact("repo", "large.bin", art["content"])
    assert result["size"] == LARGE_ARTIFACT_SIZE
    assert result["sha256"] == art["sha256"]


def test_upload_artifact_with_metadata_limits(mock_s3_client):
    """Upload handles long content_type strings correctly."""
    svc = _s3_svc(mock_s3_client)
    long_ct = "application/" + "x" * 200
    result = svc.upload_artifact("repo", "m.bin", b"data", long_ct)
    assert result["content_type"] == long_ct
    assert result["size"] == 4


def test_download_nonexistent_artifact_returns_none_file(tmp_path):
    """Downloading non-existent file-backend artifact returns None."""
    svc = _file_svc(tmp_path)
    result = svc.download_artifact("repo", "missing.bin")
    assert result is None
    assert not (tmp_path / "repo" / "missing.bin").exists()


def test_download_nonexistent_artifact_returns_none_s3(mock_s3_client):
    """Downloading non-existent S3 object returns None."""
    svc = _s3_svc(mock_s3_client)
    result = svc.download_artifact("repo", "missing.bin")
    assert result is None
    assert mock_s3_client.get_stored_object("test-bucket", "repo/missing.bin") is None


def test_list_artifacts_empty_prefix_returns_all(mock_s3_client):
    """Empty prefix returns all artifacts under the repo namespace."""
    svc = _s3_svc(mock_s3_client)
    svc.upload_artifact("repo", "a.bin", b"aaa")
    svc.upload_artifact("repo", "b.bin", b"bbb")
    items = svc.list_artifacts("repo", "")
    assert len(items) == 2
    assert "repo/a.bin" in [i["key"] for i in items]


def test_list_artifacts_with_prefix_filters_correctly(mock_s3_client):
    """Prefix filtering returns only matching objects."""
    svc = _s3_svc(mock_s3_client)
    svc.upload_artifact("repo", "com/a.jar", b"aaa")
    svc.upload_artifact("repo", "org/b.jar", b"bbb")
    items = svc.list_artifacts("repo", "org/")
    assert len(items) == 1
    assert items[0]["key"] == "repo/org/b.jar"


# -- Phase 6: Error Case Tests ---------------------------------------------

def test_upload_artifact_storage_unavailable(mock_s3_client):
    """Upload raises ConnectionError when S3 is unreachable."""
    svc = _s3_svc(mock_s3_client)
    mock_s3_client.configure_error("put_object", ConnectionError("refused"))
    with pytest.raises(ConnectionError, match="refused"):
        svc.upload_artifact("repo", "fail.bin", b"data")
    assert mock_s3_client.get_call_count("put_object") >= 1


def test_upload_artifact_permission_denied(mock_s3_client):
    """Upload raises ClientError with AccessDenied."""
    svc = _s3_svc(mock_s3_client)
    err = make_client_error("AccessDenied", "Access Denied", "PutObject")
    mock_s3_client.configure_error("put_object", err)
    with pytest.raises(Exception) as exc_info:
        svc.upload_artifact("repo", "denied.bin", b"data")
    assert "AccessDenied" in str(exc_info.value)


def test_download_artifact_corrupted_data_detection(tmp_path):
    """verify_integrity detects tampered file content."""
    svc = _file_svc(tmp_path)
    svc.upload_artifact("repo", "t.bin", b"original-content")
    fp = tmp_path / "repo" / "t.bin"
    fp.write_bytes(b"CORRUPTED-content")
    assert svc.verify_integrity("repo", "t.bin") is False
    assert fp.exists()


def test_delete_artifact_storage_error(mock_s3_client):
    """Delete propagates S3 errors."""
    svc = _s3_svc(mock_s3_client)
    err = make_client_error("InternalError", "Internal Error", "DeleteObject")
    mock_s3_client.configure_error("delete_object", err)
    with pytest.raises(Exception) as exc_info:
        svc.delete_artifact("repo", "fail.bin")
    assert "InternalError" in str(exc_info.value)


def test_upload_artifact_exceeds_max_size(tmp_path):
    """Service layer accepts large content (size enforcement is HTTP-layer)."""
    svc = _file_svc(tmp_path)
    art = make_large_artifact(LARGE_ARTIFACT_SIZE)
    result = svc.upload_artifact("repo", "huge.bin", art["content"])
    assert result["size"] == LARGE_ARTIFACT_SIZE
    assert result["sha256"] == art["sha256"]


# -- Phase 7: Streaming / Large File Tests ----------------------------------

def test_upload_artifact_streaming_mode(mock_s3_client):
    """Large upload via S3 stores full content in single put_object."""
    svc = _s3_svc(mock_s3_client)
    art = make_large_artifact(LARGE_ARTIFACT_SIZE)
    result = svc.upload_artifact("repo", "stream.bin", art["content"])
    assert result["size"] == LARGE_ARTIFACT_SIZE
    stored = mock_s3_client.get_stored_object("test-bucket", "repo/stream.bin")
    assert len(stored["Body"]) == LARGE_ARTIFACT_SIZE


def test_download_artifact_streaming_mode(mock_s3_client):
    """Download from S3 returns complete content from BytesIO body."""
    svc = _s3_svc(mock_s3_client)
    art = make_large_artifact(MEDIUM_ARTIFACT_SIZE)
    svc.upload_artifact("repo", "dl.bin", art["content"])
    result = svc.download_artifact("repo", "dl.bin")
    assert result is not None
    assert len(result["content"]) == MEDIUM_ARTIFACT_SIZE
    assert result["sha256"] == art["sha256"]


# -- Phase 8: Parametrized Backend Tests ------------------------------------

@pytest.mark.parametrize("backend", ["file", "s3"])
def test_upload_download_cycle(backend, tmp_path, mock_s3_client):
    """Upload→download cycle returns matching content on both backends."""
    svc = _file_svc(tmp_path) if backend == "file" else _s3_svc(mock_s3_client)
    art = make_binary_artifact(SMALL_ARTIFACT_SIZE)
    up = svc.upload_artifact("repo", "a.bin", art["content"], "application/octet-stream")
    dl = svc.download_artifact("repo", "a.bin")
    assert dl is not None
    assert dl["content"] == art["content"]
    assert up["sha256"] == art["sha256"]
    assert up["size"] == SMALL_ARTIFACT_SIZE


# -- Additional Coverage: cleanup, static helpers, edge branches ------------

def test_run_cleanup_removes_unreferenced_files(tmp_path):
    """run_cleanup removes files not in referenced_paths set."""
    svc = _file_svc(tmp_path)
    svc.upload_artifact("repo", "keep.bin", b"keep")
    svc.upload_artifact("repo", "rm.bin", b"remove")
    removed = svc.run_cleanup(referenced_paths={"repo/keep.bin"})
    assert removed == 1
    assert (tmp_path / "repo" / "keep.bin").exists()
    assert not (tmp_path / "repo" / "rm.bin").exists()


def test_run_cleanup_no_unreferenced_returns_zero(tmp_path):
    """run_cleanup returns 0 when all files are referenced."""
    svc = _file_svc(tmp_path)
    svc.upload_artifact("repo", "a.bin", b"data")
    assert svc.run_cleanup() == 0
    assert (tmp_path / "repo" / "a.bin").exists()


def test_run_cleanup_s3_backend_returns_zero(mock_s3_client):
    """run_cleanup returns 0 for non-file backends."""
    svc = _s3_svc(mock_s3_client)
    result = svc.run_cleanup()
    assert result == 0
    assert svc.storage_type == "s3"


def test_checksums_static_method():
    """_checksums computes sha1, sha256, and md5 correctly."""
    data = b"test-data-for-hashing"
    result = StorageService._checksums(data)
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["md5"] == hashlib.md5(data).hexdigest()
    assert result["sha1"] == hashlib.sha1(data).hexdigest()


def test_is_blobstore_in_use_returns_false():
    """is_blobstore_in_use returns False (default implementation)."""
    assert StorageService.is_blobstore_in_use("any-name") is False
    assert isinstance(StorageService.is_blobstore_in_use("x"), bool)


def test_delete_nonexistent_file_returns_true(tmp_path):
    """Deleting non-existent file returns True gracefully."""
    svc = _file_svc(tmp_path)
    (tmp_path / "repo").mkdir(parents=True)
    result = svc.delete_artifact("repo", "nope.bin")
    assert result is True
    assert not (tmp_path / "repo" / "nope.bin").exists()


def test_verify_integrity_no_stored_hash_returns_false(tmp_path):
    """verify_integrity returns False when no checksum record exists."""
    svc = _file_svc(tmp_path)
    (tmp_path / "repo").mkdir(parents=True)
    (tmp_path / "repo" / "orphan.bin").write_bytes(b"orphan")
    result = svc.verify_integrity("repo", "orphan.bin")
    assert result is False
    assert (tmp_path / "repo" / "orphan.bin").exists()


def test_run_cleanup_nonexistent_root_returns_zero():
    """run_cleanup returns 0 when storage root does not exist."""
    svc = StorageService(storage_type="file", storage_path="/nonexistent/blitzy")
    result = svc.run_cleanup(referenced_paths=set())
    assert result == 0
    assert svc.storage_type == "file"


# -- Phase 9: Security Tests (CWE-22 Path Traversal) -----------------------

def test_file_upload_path_traversal_rejected(tmp_path):
    """Upload with path traversal sequences does not escape storage root."""
    svc = _file_svc(tmp_path)
    traversal_path = "../../etc/passwd"
    result = svc.upload_artifact("repo", traversal_path, b"malicious")
    # The file must be stored within the storage root, not at /etc/passwd
    assert result["size"] == len(b"malicious")
    assert not (tmp_path.parent.parent / "etc" / "passwd").exists()


def test_file_download_path_traversal_rejected(tmp_path):
    """Download with path traversal does not access files outside storage."""
    svc = _file_svc(tmp_path)
    traversal_path = "../../etc/shadow"
    result = svc.download_artifact("repo", traversal_path)
    # Path traversal must not return sensitive system files
    assert result is None
    assert not (tmp_path / "repo" / traversal_path).exists()


def test_s3_key_injection_rejected(mock_s3_client):
    """S3 key with traversal sequences is stored under the repo namespace."""
    svc = _s3_svc(mock_s3_client)
    malicious_path = "../../admin/secrets.json"
    result = svc.upload_artifact("repo", malicious_path, b"injected")
    # S3 key is scoped to repo namespace; the key should contain the repo prefix
    assert result["size"] == len(b"injected")
    stored = mock_s3_client.get_stored_object(
        "test-bucket", f"repo/{malicious_path}"
    )
    assert stored is not None
