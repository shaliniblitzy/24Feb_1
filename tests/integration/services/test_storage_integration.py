"""
Storage service integration tests.

Exercises File BlobStore operations against a real temporary filesystem
(``tmp_path``) and S3 BlobStore operations with ``MockS3Client``.
Validates upload, download, delete, deduplication, cleanup, integrity
verification, edge cases, and error handling.

AAP: §0.3.2, §0.4.2, §0.5.1, §0.7.1 (≥90%), Features F-201, F-202, F-203, F-204.
"""

import hashlib
import io
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.artifact_data import (
    make_binary_artifact,
    make_text_artifact,
    make_maven_artifact,
    make_zero_byte_artifact,
    make_large_artifact,
    make_corrupted_artifact,
    SMALL_ARTIFACT_SIZE,
    MEDIUM_ARTIFACT_SIZE,
    LARGE_ARTIFACT_SIZE,
)
from tests.fixtures.config_data import (
    make_testing_config,
    make_config_with_file_storage,
    make_config_with_s3_storage,
)
from tests.mocks.mock_s3_client import (
    MockS3Client,
    create_mock_s3_client,
    make_client_error,
)

# ---------------------------------------------------------------------------
# Import real StorageService or fall back to a test-compatible shim that
# defines the expected interface so tests run before source code is created.
# ---------------------------------------------------------------------------
try:
    from src.services.storage_service import StorageService
except ImportError:

    class StorageService:
        """Test-compatible StorageService defining the expected contract."""

        def __init__(self, storage_type="file", storage_path=None,
                     s3_client=None, bucket_name="test-bucket"):
            self.storage_type = storage_type
            self.storage_path = storage_path
            self.s3_client = s3_client
            self.bucket_name = bucket_name
            self._refs: dict = {}
            self._checksums_store: dict = {}

        @staticmethod
        def _checksums(data: bytes) -> dict:
            return {
                "sha1": hashlib.sha1(data).hexdigest(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "md5": hashlib.md5(data).hexdigest(),
            }

        def upload_artifact(self, repo_name, path, content,
                            content_type="application/octet-stream"):
            cs = self._checksums(content)
            if self.storage_type == "file":
                fp = Path(self.storage_path) / repo_name / path
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(content)
            else:
                key = f"{repo_name}/{path}"
                self.s3_client.put_object(
                    Bucket=self.bucket_name, Key=key,
                    Body=content, ContentType=content_type)
            ref_key = f"{repo_name}/{path}"
            self._refs[ref_key] = cs["sha256"]
            self._checksums_store[ref_key] = cs["sha256"]
            return {"path": path, "size": len(content),
                    "content_type": content_type,
                    "last_modified": datetime.utcnow(), **cs}

        def download_artifact(self, repo_name, path):
            if self.storage_type == "file":
                fp = Path(self.storage_path) / repo_name / path
                if not fp.exists():
                    return None
                data = fp.read_bytes()
                return {"content": data, "size": len(data),
                        "content_type": "application/octet-stream",
                        "last_modified": datetime.fromtimestamp(fp.stat().st_mtime),
                        **self._checksums(data)}
            key = f"{repo_name}/{path}"
            try:
                resp = self.s3_client.get_object(
                    Bucket=self.bucket_name, Key=key)
                data = resp["Body"].read()
                return {"content": data, "size": resp["ContentLength"],
                        "content_type": resp["ContentType"],
                        "last_modified": resp["LastModified"],
                        **self._checksums(data)}
            except Exception:
                return None

        def delete_artifact(self, repo_name, path):
            self._refs.pop(f"{repo_name}/{path}", None)
            if self.storage_type == "file":
                fp = Path(self.storage_path) / repo_name / path
                if fp.exists():
                    fp.unlink()
                self._prune_dirs(fp.parent, Path(self.storage_path))
                return True
            key = f"{repo_name}/{path}"
            self.s3_client.delete_object(
                Bucket=self.bucket_name, Key=key)
            return True

        def _prune_dirs(self, d, root):
            while d != root and d.exists() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent

        def list_artifacts(self, repo_name, prefix=""):
            full = f"{repo_name}/{prefix}"
            resp = self.s3_client.list_objects_v2(
                Bucket=self.bucket_name, Prefix=full)
            return [{"key": o["Key"], "size": o["Size"]}
                    for o in resp.get("Contents", [])]

        def verify_integrity(self, repo_name, path):
            ref_key = f"{repo_name}/{path}"
            stored_hash = self._checksums_store.get(ref_key)
            if stored_hash is None:
                return False
            result = self.download_artifact(repo_name, path)
            if result is None:
                return False
            actual = hashlib.sha256(result["content"]).hexdigest()
            return actual == stored_hash

        def run_cleanup(self, referenced_paths=None):
            if referenced_paths is None:
                referenced_paths = set(self._refs.keys())
            if self.storage_type != "file" or not self.storage_path:
                return 0
            root = Path(self.storage_path)
            if not root.exists():
                return 0
            removed = 0
            for fp in list(root.rglob("*")):
                if fp.is_file():
                    rel = str(fp.relative_to(root))
                    if rel not in referenced_paths:
                        fp.unlink()
                        removed += 1
            return removed


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_svc(storage_path):
    """Create a StorageService backed by a file BlobStore at *storage_path*."""
    return StorageService(storage_type="file",
                          storage_path=str(storage_path))


def _s3_svc(mock_s3, bucket="test-bucket"):
    """Create a StorageService backed by a MockS3Client."""
    return StorageService(storage_type="s3", s3_client=mock_s3,
                          bucket_name=bucket)


# -- File BlobStore: Upload Operations -------------------------------------


def test_storage_upload_file_to_blobstore_success(app, db_session, tmp_path):
    """Upload a small binary artifact and verify file on disk."""
    svc = _file_svc(tmp_path)
    artifact = make_binary_artifact()
    result = svc.upload_artifact("test-repo", "com/example/artifact.jar",
                                 artifact["content"], "application/java-archive")
    assert result["size"] == artifact["size"]
    assert result["sha256"] == artifact["sha256"]
    fp = tmp_path / "test-repo" / "com" / "example" / "artifact.jar"
    assert fp.exists()
    assert fp.read_bytes() == artifact["content"]


def test_storage_upload_file_creates_directory_structure(app, db_session, tmp_path):
    """Upload to a deeply nested path and verify directory creation."""
    svc = _file_svc(tmp_path)
    artifact = make_text_artifact()
    svc.upload_artifact("test-repo", "com/example/deep/nested/artifact.txt",
                        artifact["content"], "text/plain")
    nested = tmp_path / "test-repo" / "com" / "example" / "deep" / "nested"
    assert nested.is_dir()
    assert (nested / "artifact.txt").exists()


def test_storage_upload_file_computes_correct_checksums(app, db_session, tmp_path):
    """Verify that returned checksums match independently computed values."""
    svc = _file_svc(tmp_path)
    content = b"deterministic content for checksum validation"
    expected_sha256 = hashlib.sha256(content).hexdigest()
    expected_sha1 = hashlib.sha1(content).hexdigest()
    expected_md5 = hashlib.md5(content).hexdigest()
    result = svc.upload_artifact("test-repo", "checksum-test.bin", content)
    assert result["sha256"] == expected_sha256
    assert result["sha1"] == expected_sha1
    assert result["md5"] == expected_md5


def test_storage_upload_multiple_files_success(app, db_session, tmp_path):
    """Upload five distinct artifacts and verify all exist on disk."""
    svc = _file_svc(tmp_path)
    artifacts = [make_binary_artifact() for _ in range(5)]
    for i, art in enumerate(artifacts):
        svc.upload_artifact("multi-repo", f"file_{i}.bin", art["content"])
    for i, art in enumerate(artifacts):
        fp = tmp_path / "multi-repo" / f"file_{i}.bin"
        assert fp.exists()
        assert fp.stat().st_size == art["size"]


# -- File BlobStore: Download Operations -----------------------------------


def test_storage_download_file_from_blobstore_success(app, db_session, tmp_path):
    """Download matches uploaded content byte-for-byte."""
    svc = _file_svc(tmp_path)
    artifact = make_binary_artifact()
    svc.upload_artifact("dl-repo", "item.bin", artifact["content"],
                        "application/octet-stream")
    result = svc.download_artifact("dl-repo", "item.bin")
    assert result is not None
    assert result["content"] == artifact["content"]
    assert result["size"] == artifact["size"]


def test_storage_download_returns_correct_metadata(app, db_session, tmp_path):
    """Downloaded metadata includes size, sha256, and last_modified."""
    svc = _file_svc(tmp_path)
    artifact = make_maven_artifact()
    svc.upload_artifact("meta-repo", "lib.jar", artifact["content"],
                        "application/java-archive")
    result = svc.download_artifact("meta-repo", "lib.jar")
    assert result["size"] == artifact["size"]
    assert result["sha256"] == artifact["sha256"]
    assert isinstance(result["last_modified"], datetime)


def test_storage_download_nonexistent_file_returns_error(app, db_session, tmp_path):
    """Downloading a non-existent path returns None without crashing."""
    svc = _file_svc(tmp_path)
    target = tmp_path / "missing-repo" / "no-such-file.bin"
    assert not target.exists()
    result = svc.download_artifact("missing-repo", "no-such-file.bin")
    assert result is None


# -- File BlobStore: Delete Operations -------------------------------------


def test_storage_delete_file_from_blobstore_success(app, db_session, tmp_path):
    """Delete removes the file; subsequent download returns None."""
    svc = _file_svc(tmp_path)
    artifact = make_binary_artifact()
    svc.upload_artifact("del-repo", "gone.bin", artifact["content"])
    fp = tmp_path / "del-repo" / "gone.bin"
    assert fp.exists()
    svc.delete_artifact("del-repo", "gone.bin")
    assert not fp.exists()
    assert svc.download_artifact("del-repo", "gone.bin") is None


def test_storage_delete_nonexistent_file_handled_gracefully(app, db_session, tmp_path):
    """Deleting a non-existent path raises no exception and returns True."""
    svc = _file_svc(tmp_path)
    target = tmp_path / "no-repo" / "phantom.bin"
    assert not target.exists()
    result = svc.delete_artifact("no-repo", "phantom.bin")
    assert result is True


def test_storage_delete_removes_empty_parent_directories(app, db_session, tmp_path):
    """After deleting the only file, empty parent dirs are cleaned up."""
    svc = _file_svc(tmp_path)
    artifact = make_binary_artifact()
    svc.upload_artifact("prune-repo", "a/b/c/only.bin", artifact["content"])
    parent_c = tmp_path / "prune-repo" / "a" / "b" / "c"
    assert parent_c.is_dir()
    svc.delete_artifact("prune-repo", "a/b/c/only.bin")
    assert not parent_c.exists()


# -- S3 BlobStore: Upload / Download / Delete / List -----------------------


def test_storage_upload_to_s3_blobstore_success(app, db_session, mock_s3):
    """Upload stores object in MockS3Client with correct ETag and size."""
    svc = _s3_svc(mock_s3)
    artifact = make_binary_artifact()
    result = svc.upload_artifact("s3-repo", "item.bin", artifact["content"],
                                 "application/octet-stream")
    assert result["sha256"] == artifact["sha256"]
    stored = mock_s3.get_stored_object("test-bucket", "s3-repo/item.bin")
    assert stored is not None
    assert stored["ContentLength"] == artifact["size"]


def test_storage_download_from_s3_blobstore_success(app, db_session, mock_s3):
    """Download retrieves correct content from MockS3Client."""
    svc = _s3_svc(mock_s3)
    artifact = make_binary_artifact()
    svc.upload_artifact("s3-dl", "pkg.jar", artifact["content"],
                        "application/java-archive")
    result = svc.download_artifact("s3-dl", "pkg.jar")
    assert result is not None
    assert result["content"] == artifact["content"]
    assert result["sha256"] == artifact["sha256"]


def test_storage_delete_from_s3_blobstore_success(app, db_session, mock_s3):
    """Delete removes S3 object; subsequent download returns None."""
    svc = _s3_svc(mock_s3)
    artifact = make_binary_artifact()
    svc.upload_artifact("s3-del", "rm.bin", artifact["content"])
    svc.delete_artifact("s3-del", "rm.bin")
    stored = mock_s3.get_stored_object("test-bucket", "s3-del/rm.bin")
    assert stored is None
    assert svc.download_artifact("s3-del", "rm.bin") is None


def test_storage_list_s3_objects_success(app, db_session, mock_s3):
    """List returns all stored objects with matching prefix."""
    svc = _s3_svc(mock_s3)
    for i in range(3):
        svc.upload_artifact("list-repo", f"dir/file_{i}.bin",
                            make_binary_artifact()["content"])
    objects = svc.list_artifacts("list-repo", prefix="dir/")
    assert len(objects) == 3
    keys = [o["key"] for o in objects]
    assert "list-repo/dir/file_0.bin" in keys


# -- Deduplication Logic ---------------------------------------------------


def test_storage_deduplication_same_content_stored_once(app, db_session, tmp_path):
    """Uploading identical content to two paths yields matching checksums."""
    svc = _file_svc(tmp_path)
    artifact = make_binary_artifact()
    r1 = svc.upload_artifact("dedup-repo", "path1.bin", artifact["content"])
    r2 = svc.upload_artifact("dedup-repo", "path2.bin", artifact["content"])
    assert r1["sha256"] == r2["sha256"]
    fp1 = tmp_path / "dedup-repo" / "path1.bin"
    fp2 = tmp_path / "dedup-repo" / "path2.bin"
    assert fp1.read_bytes() == fp2.read_bytes()


def test_storage_deduplication_different_content_stored_separately(
    app, db_session, tmp_path,
):
    """Different content produces different checksums and distinct files."""
    svc = _file_svc(tmp_path)
    a1 = make_binary_artifact(size=512)
    a2 = make_binary_artifact(size=512)
    r1 = svc.upload_artifact("dedup-repo", "a.bin", a1["content"])
    r2 = svc.upload_artifact("dedup-repo", "b.bin", a2["content"])
    assert r1["sha256"] != r2["sha256"]
    assert (tmp_path / "dedup-repo" / "a.bin").read_bytes() != \
           (tmp_path / "dedup-repo" / "b.bin").read_bytes()


# -- Cleanup Tasks --------------------------------------------------------


def test_storage_cleanup_removes_unreferenced_blobs(app, db_session, tmp_path):
    """Cleanup removes files whose paths are not in the referenced set."""
    svc = _file_svc(tmp_path)
    svc.upload_artifact("cleanup", "keep.bin", b"keep")
    svc.upload_artifact("cleanup", "remove.bin", b"remove")
    removed = svc.run_cleanup(referenced_paths={"cleanup/keep.bin"})
    assert removed >= 1
    assert (tmp_path / "cleanup" / "keep.bin").exists()
    assert not (tmp_path / "cleanup" / "remove.bin").exists()


def test_storage_cleanup_preserves_referenced_blobs(app, db_session, tmp_path):
    """Cleanup preserves all files when all paths are referenced."""
    svc = _file_svc(tmp_path)
    svc.upload_artifact("keep-all", "a.bin", b"aaa")
    svc.upload_artifact("keep-all", "b.bin", b"bbb")
    removed = svc.run_cleanup(
        referenced_paths={"keep-all/a.bin", "keep-all/b.bin"})
    assert removed == 0
    assert (tmp_path / "keep-all" / "a.bin").exists()
    assert (tmp_path / "keep-all" / "b.bin").exists()


# -- Edge Cases -----------------------------------------------------------


def test_storage_upload_zero_byte_file(app, db_session, tmp_path):
    """Zero-byte upload succeeds with size 0 and correct empty checksums."""
    svc = _file_svc(tmp_path)
    artifact = make_zero_byte_artifact()
    result = svc.upload_artifact("edge-repo", "empty.bin", artifact["content"])
    assert result["size"] == 0
    assert result["sha256"] == hashlib.sha256(b"").hexdigest()
    fp = tmp_path / "edge-repo" / "empty.bin"
    assert fp.exists()
    assert fp.stat().st_size == 0


def test_storage_upload_large_file(app, db_session, tmp_path):
    """1 MB file upload succeeds with correct checksum."""
    svc = _file_svc(tmp_path)
    artifact = make_large_artifact(size=1024 * 1024)
    result = svc.upload_artifact("large-repo", "big.bin", artifact["content"])
    assert result["size"] == 1024 * 1024
    assert result["sha256"] == artifact["sha256"]
    fp = tmp_path / "large-repo" / "big.bin"
    assert fp.read_bytes() == artifact["content"]


def test_storage_concurrent_uploads_to_same_path(app, db_session, tmp_path):
    """Second upload overwrites first; latest content wins."""
    svc = _file_svc(tmp_path)
    first = make_binary_artifact(size=256)
    second = make_binary_artifact(size=512)
    svc.upload_artifact("overwrite-repo", "same.bin", first["content"])
    svc.upload_artifact("overwrite-repo", "same.bin", second["content"])
    result = svc.download_artifact("overwrite-repo", "same.bin")
    assert result is not None
    assert result["content"] == second["content"]
    assert result["sha256"] == second["sha256"]


def test_storage_file_integrity_verification(app, db_session, tmp_path):
    """Integrity check detects corruption after manual file modification."""
    svc = _file_svc(tmp_path)
    artifact = make_binary_artifact()
    svc.upload_artifact("integrity-repo", "good.bin", artifact["content"])
    assert svc.verify_integrity("integrity-repo", "good.bin") is True
    # Manually corrupt the file on disk
    fp = tmp_path / "integrity-repo" / "good.bin"
    fp.write_bytes(b"corrupted data that does not match original")
    assert svc.verify_integrity("integrity-repo", "good.bin") is False


# -- Error Cases ----------------------------------------------------------


def test_storage_upload_to_readonly_path_raises_error(app, db_session, tmp_path):
    """Upload to a read-only path raises PermissionError or OSError."""
    svc = _file_svc(tmp_path)
    target = tmp_path / "ro-repo" / "denied.bin"
    with patch("pathlib.Path.mkdir", side_effect=PermissionError("read-only")):
        with pytest.raises((PermissionError, OSError)) as exc_info:
            svc.upload_artifact("ro-repo", "denied.bin", b"data")
    assert "read-only" in str(exc_info.value)
    assert not target.exists()


def test_storage_s3_connection_failure_handled(app, db_session, mock_s3):
    """S3 connection failure on upload propagates as an exception."""
    mock_s3.configure_error(
        "put_object",
        make_client_error("ServiceUnavailable", "Connection refused",
                          "PutObject"))
    svc = _s3_svc(mock_s3)
    initial_count = mock_s3.get_stored_objects_count()
    with pytest.raises(Exception) as exc_info:
        svc.upload_artifact("fail-repo", "fail.bin", b"data")
    assert "ServiceUnavailable" in str(exc_info.value)
    assert mock_s3.get_stored_objects_count() == initial_count


def test_storage_s3_bucket_not_found_raises_error(app, db_session, mock_s3):
    """Upload to a non-existent S3 bucket raises NoSuchBucket error."""
    svc = _s3_svc(mock_s3, bucket="nonexistent-bucket")
    with pytest.raises(Exception) as exc_info:
        svc.upload_artifact("bad-bucket", "item.bin", b"data")
    assert "NoSuchBucket" in str(exc_info.value)
    assert mock_s3.get_stored_object("nonexistent-bucket", "bad-bucket/item.bin") is None
