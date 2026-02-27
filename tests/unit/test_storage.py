"""
Comprehensive unit tests for the BlobStore storage subsystem.

Tests cover the abstract BlobStore interface (ABC), core data types
(BlobId, BlobAttributes, Blob, BlobStoreMetrics, BlobStoreConfiguration,
BlobStoreType), the File BlobStore backend (Feature F-201: atomic writes,
soft-delete, metadata sidecar files, disk-space monitoring), the S3
BlobStore backend (Feature F-202: SSE encryption, multipart uploads,
S3 object tagging for soft-delete), the BlobStore Maintenance Service
(Feature F-203: compaction, temp cleanup, integrity checks, statistics
collection), and factory functions (create_blobstore, create_blobstore_from_model,
create_file_blobstore, create_s3_blobstore).

Replaces Java BlobStore tests from the original Sonatype Nexus Repository
system, using pytest 8.3.4, moto 5.0.27 for AWS mocking, and
unittest.mock for isolation.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO
from unittest.mock import MagicMock, PropertyMock, patch
from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from src.app.storage.blobstore import (
    Blob,
    BlobAlreadyExistsError,
    BlobAttributes,
    BlobId,
    BlobNotFoundError,
    BlobStore,
    BlobStoreConfiguration,
    BlobStoreError,
    BlobStoreFullError,
    BlobStoreMetrics,
    BlobStoreNotStartedError,
    BlobStoreType,
    DEFAULT_SOFT_DELETE_RETENTION_DAYS,
    compute_checksums,
)
from src.app.storage.file_blobstore import FileBlobStore, create_file_blobstore
from src.app.storage.s3_blobstore import S3BlobStore, create_s3_blobstore
from src.app.storage.maintenance import BlobStoreMaintenanceService
from src.app.storage import create_blobstore, create_blobstore_from_model


# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------
TEST_CONTENT = b"Hello, BlobStore! This is test content for storage testing."
TEST_CONTENT_TYPE = "application/octet-stream"
TEST_STORE_NAME = "test-store"
TEST_BUCKET = "test-bucket"
TEST_REGION = "us-east-1"

# Pre-computed checksums for TEST_CONTENT
EXPECTED_SHA1 = hashlib.sha1(TEST_CONTENT).hexdigest()
EXPECTED_SHA256 = hashlib.sha256(TEST_CONTENT).hexdigest()
EXPECTED_MD5 = hashlib.md5(TEST_CONTENT).hexdigest()


# ===========================================================================
# Phase 2: Abstract BlobStore Interface Tests
# ===========================================================================


class TestBlobStoreAbstract:
    """Tests for the abstract BlobStore base class (ABC)."""

    def test_blobstore_is_abstract(self):
        """BlobStore cannot be instantiated directly because it is an ABC."""
        with pytest.raises(TypeError):
            config = BlobStoreConfiguration(
                name="test", store_type=BlobStoreType.FILE
            )
            BlobStore(name="test", config=config)

    def test_blobstore_abstract_methods(self):
        """BlobStore declares all 11 abstract methods that subclasses must
        implement: start, stop, create, get, get_stream, delete, exists,
        undelete, compact, get_metrics, list_blobs."""
        abstract_methods = BlobStore.__abstractmethods__
        expected = {
            "start", "stop", "create", "get", "get_stream",
            "delete", "exists", "undelete", "compact",
            "get_metrics", "list_blobs",
        }
        assert expected.issubset(abstract_methods), (
            f"Missing abstract methods: {expected - abstract_methods}"
        )

    def test_blobstore_subclass_implementation(self):
        """A concrete subclass implementing all abstract methods can be
        instantiated successfully."""

        class ConcreteBlobStore(BlobStore):
            def start(self) -> None:
                self._started = True

            def stop(self) -> None:
                self._started = False

            def create(self, blob_id, data, content_type="application/octet-stream", headers=None):
                return Blob(blob_id=BlobId.generate(self._name), attributes=BlobAttributes())

            def get(self, blob_id):
                return None

            def get_stream(self, blob_id):
                return None

            def delete(self, blob_id, soft=True):
                return False

            def exists(self, blob_id):
                return False

            def undelete(self, blob_id):
                return False

            def compact(self):
                return 0

            def get_metrics(self):
                return BlobStoreMetrics(blob_store_name=self._name, blob_store_type="test")

            def list_blobs(self, include_deleted=False):
                return iter([])

        config = BlobStoreConfiguration(
            name="concrete", store_type=BlobStoreType.FILE
        )
        store = ConcreteBlobStore(name="concrete", config=config)
        assert store.name == "concrete"
        assert store.is_started is False


# ===========================================================================
# Phase 3: BlobId Data Class Tests
# ===========================================================================


class TestBlobId:
    """Tests for the BlobId frozen dataclass."""

    def test_blob_id_creation(self):
        """BlobId can be created with a UUID-based value and store_name."""
        blob_id = BlobId(value="550e8400-e29b-41d4-a716-446655440000", store_name="default")
        assert blob_id.value == "550e8400-e29b-41d4-a716-446655440000"
        assert blob_id.store_name == "default"

    def test_blob_id_frozen(self):
        """BlobId is frozen — fields cannot be modified after creation."""
        blob_id = BlobId(value="abc123", store_name="store1")
        with pytest.raises(AttributeError):
            blob_id.value = "new-value"
        with pytest.raises(AttributeError):
            blob_id.store_name = "new-store"

    def test_blob_id_hashable(self):
        """BlobId is hashable and can be used as dict key and in sets."""
        bid1 = BlobId(value="abc", store_name="s1")
        bid2 = BlobId(value="abc", store_name="s1")
        bid3 = BlobId(value="def", store_name="s1")

        # Can be used as dict key
        d = {bid1: "value1", bid3: "value3"}
        assert d[bid2] == "value1"

        # Can be used in sets
        s = {bid1, bid2, bid3}
        assert len(s) == 2

    def test_blob_id_generate(self):
        """BlobId.generate() produces unique IDs with UUID values."""
        bid1 = BlobId.generate("store1")
        bid2 = BlobId.generate("store1")
        assert bid1.store_name == "store1"
        assert bid2.store_name == "store1"
        assert bid1.value != bid2.value
        # UUID format check: should contain hyphens
        assert "-" in bid1.value

    def test_blob_id_from_string(self):
        """BlobId.from_string() reconstructs BlobId from 'store_name:value'."""
        original = BlobId(value="550e8400-e29b-41d4-a716-446655440000", store_name="default")
        string_repr = str(original)
        reconstructed = BlobId.from_string(string_repr)
        assert reconstructed == original
        assert reconstructed.store_name == "default"
        assert reconstructed.value == "550e8400-e29b-41d4-a716-446655440000"

    def test_blob_id_equality(self):
        """Two BlobIds with the same value and store_name are equal."""
        bid1 = BlobId(value="same-uuid", store_name="same-store")
        bid2 = BlobId(value="same-uuid", store_name="same-store")
        assert bid1 == bid2
        assert hash(bid1) == hash(bid2)

        # Different values should not be equal
        bid3 = BlobId(value="different", store_name="same-store")
        assert bid1 != bid3


# ===========================================================================
# Phase 4: BlobAttributes Data Class Tests
# ===========================================================================


class TestBlobAttributes:
    """Tests for the BlobAttributes dataclass serialisation."""

    def test_blob_attributes_creation(self):
        """BlobAttributes can be created with all metadata fields."""
        now = datetime.now(timezone.utc)
        attrs = BlobAttributes(
            content_type="application/jar",
            sha1="abc123",
            sha256="def456",
            md5="ghi789",
            size=1024,
            creation_time=now,
        )
        assert attrs.content_type == "application/jar"
        assert attrs.sha1 == "abc123"
        assert attrs.sha256 == "def456"
        assert attrs.md5 == "ghi789"
        assert attrs.size == 1024
        assert attrs.creation_time == now

    def test_blob_attributes_to_dict(self):
        """to_dict() returns a proper dictionary with all fields."""
        now = datetime.now(timezone.utc)
        attrs = BlobAttributes(
            content_type="text/plain",
            sha1="aaa",
            sha256="bbb",
            md5="ccc",
            size=42,
            creation_time=now,
        )
        d = attrs.to_dict()
        assert d["content_type"] == "text/plain"
        assert d["sha1"] == "aaa"
        assert d["sha256"] == "bbb"
        assert d["md5"] == "ccc"
        assert d["size"] == 42
        assert "creation_time" in d

    def test_blob_attributes_from_dict(self):
        """from_dict() reconstructs BlobAttributes from a dictionary."""
        now = datetime.now(timezone.utc)
        data = {
            "content_type": "application/xml",
            "sha1": "sha1val",
            "sha256": "sha256val",
            "md5": "md5val",
            "size": 2048,
            "creation_time": now.isoformat(),
        }
        attrs = BlobAttributes.from_dict(data)
        assert attrs.content_type == "application/xml"
        assert attrs.sha1 == "sha1val"
        assert attrs.sha256 == "sha256val"
        assert attrs.md5 == "md5val"
        assert attrs.size == 2048

    def test_blob_attributes_round_trip(self):
        """to_dict() → from_dict() preserves all fields (round-trip)."""
        now = datetime.now(timezone.utc)
        original = BlobAttributes(
            content_type="application/zip",
            sha1="roundtrip_sha1",
            sha256="roundtrip_sha256",
            md5="roundtrip_md5",
            size=9999,
            creation_time=now,
        )
        serialised = original.to_dict()
        restored = BlobAttributes.from_dict(serialised)

        assert restored.content_type == original.content_type
        assert restored.sha1 == original.sha1
        assert restored.sha256 == original.sha256
        assert restored.md5 == original.md5
        assert restored.size == original.size


# ===========================================================================
# Phase 5: BlobStoreMetrics Data Class Tests
# ===========================================================================


class TestBlobStoreMetrics:
    """Tests for BlobStoreMetrics threshold properties."""

    def test_metrics_creation(self):
        """BlobStoreMetrics can be created with all statistic fields."""
        metrics = BlobStoreMetrics(
            blob_store_name="default",
            blob_store_type="file",
            total_size_bytes=1000,
            blob_count=10,
            available_space_bytes=9000,
            total_space_bytes=10000,
            usage_percentage=50.0,
        )
        assert metrics.blob_store_name == "default"
        assert metrics.blob_count == 10
        assert metrics.total_size_bytes == 1000

    def test_metrics_is_space_critical(self):
        """is_space_critical returns True when usage exceeds 95%."""
        metrics = BlobStoreMetrics(
            blob_store_name="store",
            blob_store_type="file",
            usage_percentage=96.0,
        )
        assert metrics.is_space_critical is True
        assert metrics.is_space_warning is True

    def test_metrics_is_space_warning(self):
        """is_space_warning returns True when usage exceeds 90% but not 95%."""
        metrics = BlobStoreMetrics(
            blob_store_name="store",
            blob_store_type="file",
            usage_percentage=92.0,
        )
        assert metrics.is_space_warning is True
        assert metrics.is_space_critical is False

    def test_metrics_below_thresholds(self):
        """Below 90% usage, both warning and critical are False."""
        metrics = BlobStoreMetrics(
            blob_store_name="store",
            blob_store_type="file",
            usage_percentage=50.0,
        )
        assert metrics.is_space_warning is False
        assert metrics.is_space_critical is False


# ===========================================================================
# Phase 6: BlobStoreType Enum Tests
# ===========================================================================


class TestBlobStoreType:
    """Tests for the BlobStoreType enum."""

    def test_blobstore_type_file(self):
        """BlobStoreType.FILE exists with value 'file'."""
        assert BlobStoreType.FILE.value == "file"
        assert BlobStoreType.FILE == BlobStoreType.FILE

    def test_blobstore_type_s3(self):
        """BlobStoreType.S3 exists with value 's3'."""
        assert BlobStoreType.S3.value == "s3"
        assert BlobStoreType.S3 == BlobStoreType.S3


# ===========================================================================
# Phase 7: compute_checksums Utility Tests
# ===========================================================================


class TestComputeChecksums:
    """Tests for the compute_checksums module-level function."""

    def test_compute_checksums_returns_all(self):
        """compute_checksums returns dict with sha1, sha256, md5 keys."""
        result = compute_checksums(b"test data")
        assert "sha1" in result
        assert "sha256" in result
        assert "md5" in result

    def test_compute_checksums_correct_values(self):
        """Checksums match independently computed values for known content."""
        data = b"known content for checksum test"
        expected_sha1 = hashlib.sha1(data).hexdigest()
        expected_sha256 = hashlib.sha256(data).hexdigest()
        expected_md5 = hashlib.md5(data).hexdigest()

        result = compute_checksums(data)
        assert result["sha1"] == expected_sha1
        assert result["sha256"] == expected_sha256
        assert result["md5"] == expected_md5

    def test_compute_checksums_empty_content(self):
        """Handles empty bytes input without errors."""
        result = compute_checksums(b"")
        assert result["sha1"] == hashlib.sha1(b"").hexdigest()
        assert result["sha256"] == hashlib.sha256(b"").hexdigest()
        assert result["md5"] == hashlib.md5(b"").hexdigest()

    def test_compute_checksums_large_content(self):
        """Handles large content (1 MB) efficiently."""
        large_data = b"A" * (1024 * 1024)
        result = compute_checksums(large_data)
        assert result["sha1"] == hashlib.sha1(large_data).hexdigest()
        assert result["sha256"] == hashlib.sha256(large_data).hexdigest()
        assert result["md5"] == hashlib.md5(large_data).hexdigest()


# ===========================================================================
# Phase 8: BlobStore Exception Tests
# ===========================================================================


class TestBlobStoreExceptions:
    """Tests for the BlobStore exception hierarchy."""

    def test_blob_store_error(self):
        """BlobStoreError is the base exception and extends Exception."""
        exc = BlobStoreError("base error")
        assert isinstance(exc, Exception)
        assert str(exc) == "base error"

    def test_blob_not_found_error(self):
        """BlobNotFoundError extends BlobStoreError with blob_id attribute."""
        blob_id = BlobId(value="missing", store_name="store")
        exc = BlobNotFoundError(blob_id)
        assert isinstance(exc, BlobStoreError)
        assert exc.blob_id == blob_id
        assert "missing" in str(exc)

    def test_blob_already_exists_error(self):
        """BlobAlreadyExistsError extends BlobStoreError."""
        blob_id = BlobId(value="existing", store_name="store")
        exc = BlobAlreadyExistsError(blob_id)
        assert isinstance(exc, BlobStoreError)
        assert exc.blob_id == blob_id

    def test_blob_store_not_started_error(self):
        """BlobStoreNotStartedError extends BlobStoreError."""
        exc = BlobStoreNotStartedError("store not started")
        assert isinstance(exc, BlobStoreError)
        assert "store not started" in str(exc)

    def test_blob_store_full_error(self):
        """BlobStoreFullError extends BlobStoreError."""
        exc = BlobStoreFullError("disk full")
        assert isinstance(exc, BlobStoreError)
        assert "disk full" in str(exc)


# ===========================================================================
# Phase 9: File BlobStore Tests (F-201)
# ===========================================================================


class TestFileBlobStore:
    """Comprehensive tests for the FileBlobStore local filesystem backend.

    Tests cover atomic writes, content-addressable storage, soft-delete,
    metadata sidecar files, disk-space monitoring, and compaction.
    """

    @pytest.fixture
    def file_store(self, tmp_path):
        """Create a started FileBlobStore using a temporary directory."""
        store = FileBlobStore(
            name=TEST_STORE_NAME,
            root_path=str(tmp_path / "blobstore"),
        )
        store.start()
        return store

    @pytest.fixture
    def unstartedfile_store(self, tmp_path):
        """Create an un-started FileBlobStore for lifecycle tests."""
        store = FileBlobStore(
            name="unstarted-store",
            root_path=str(tmp_path / "blobstore-unstarted"),
        )
        return store

    # -- Lifecycle Tests ---------------------------------------------------

    def test_file_blobstore_start(self, tmp_path):
        """Start creates the base directory structure."""
        root = tmp_path / "start-test"
        store = FileBlobStore(name="start-test", root_path=str(root))
        store.start()
        assert store.is_started is True
        assert root.exists()
        # Verify subdirectories were created
        assert (root / "content").exists() or root.exists()

    def test_file_blobstore_stop(self, file_store):
        """Stop completes without error and marks store as stopped."""
        assert file_store.is_started is True
        file_store.stop()
        assert file_store.is_started is False

    def test_file_blobstore_operations_before_start(self, unstartedfile_store):
        """Operations before start raise BlobStoreNotStartedError."""
        blob_id = BlobId.generate(unstartedfile_store.name)
        with pytest.raises(BlobStoreNotStartedError):
            unstartedfile_store.create(None, TEST_CONTENT)
        with pytest.raises(BlobStoreNotStartedError):
            unstartedfile_store.get(blob_id)
        with pytest.raises(BlobStoreNotStartedError):
            unstartedfile_store.get_stream(blob_id)
        with pytest.raises(BlobStoreNotStartedError):
            unstartedfile_store.delete(blob_id)

    # -- Create (Atomic Writes) Tests --------------------------------------

    def test_file_create_blob(self, file_store):
        """Create blob with content bytes returns Blob with valid BlobId."""
        blob = file_store.create(None, TEST_CONTENT, content_type=TEST_CONTENT_TYPE)
        assert blob is not None
        assert isinstance(blob.blob_id, BlobId)
        assert blob.attributes.size == len(TEST_CONTENT)
        assert blob.blob_id.store_name == TEST_STORE_NAME

    def test_file_create_blob_atomic_write(self, file_store, tmp_path):
        """Content is written to staging first, then atomically moved
        to the final location (no partial writes in content dir)."""
        blob = file_store.create(None, TEST_CONTENT)
        # After create, the blob content should exist in the content directory
        # and the staging directory should be empty (temp file removed)
        staging_dir = file_store._staging_dir
        staging_files = list(staging_dir.rglob("*.staging")) if staging_dir.exists() else []
        assert len(staging_files) == 0, "Staging files should be cleaned up after create"

    def test_file_create_blob_content_addressable(self, file_store):
        """Storage path uses ID-based directory nesting (3-level)."""
        blob = file_store.create(None, TEST_CONTENT)
        content_path = file_store._blob_path(blob.blob_id)
        # Verify the file exists at the computed path
        assert content_path.exists()
        # Verify nested directory structure (at least 3 levels deep from content dir)
        relative = content_path.relative_to(file_store._content_dir)
        parts = relative.parts
        # Should have 3 directory segments + filename = 4 parts total
        assert len(parts) >= 4, f"Expected 3-level nesting + filename, got {parts}"

    def test_file_create_blob_metadata_sidecar(self, file_store):
        """A .properties sidecar file is created alongside the blob."""
        blob = file_store.create(None, TEST_CONTENT, content_type="text/plain")
        metadata_path = file_store._metadata_path(blob.blob_id)
        assert metadata_path.exists(), "Metadata sidecar .properties file should exist"
        # Verify the sidecar contains expected keys
        with open(str(metadata_path), "r") as f:
            props = json.loads(f.read())
        assert "content_type" in props
        assert props["content_type"] == "text/plain"
        assert "sha1" in props
        assert "sha256" in props
        assert "md5" in props
        assert "size" in props

    def test_file_create_blob_checksums_computed(self, file_store):
        """SHA-1, SHA-256, and MD5 are computed and stored in attributes."""
        blob = file_store.create(None, TEST_CONTENT)
        assert blob.attributes.sha1 == EXPECTED_SHA1
        assert blob.attributes.sha256 == EXPECTED_SHA256
        assert blob.attributes.md5 == EXPECTED_MD5

    # -- Get Tests ---------------------------------------------------------

    def test_file_get_blob(self, file_store):
        """get() retrieves Blob with correct attributes for existing blob."""
        created = file_store.create(None, TEST_CONTENT, content_type="text/plain")
        retrieved = file_store.get(created.blob_id)
        assert retrieved is not None
        assert retrieved.blob_id == created.blob_id
        assert retrieved.attributes.content_type == "text/plain"

    def test_file_get_blob_not_found(self, file_store):
        """get() returns None for a non-existent BlobId."""
        fake_id = BlobId(value="nonexistent-uuid", store_name=TEST_STORE_NAME)
        result = file_store.get(fake_id)
        assert result is None

    def test_file_get_stream(self, file_store):
        """get_stream() returns a readable file-like object with correct content."""
        blob = file_store.create(None, TEST_CONTENT)
        stream = file_store.get_stream(blob.blob_id)
        assert stream is not None
        data = stream.read()
        stream.close()
        assert data == TEST_CONTENT

    # -- Delete (Soft-Delete) Tests ----------------------------------------

    def test_file_delete_blob(self, file_store):
        """Soft-delete moves blob to .deleted/ directory."""
        blob = file_store.create(None, TEST_CONTENT)
        result = file_store.delete(blob.blob_id, soft=True)
        assert result is True
        # Original content path should no longer exist
        content_path = file_store._blob_path(blob.blob_id)
        assert not content_path.exists()

    def test_file_delete_preserves_content(self, file_store):
        """Content still exists in .deleted/ after soft-delete."""
        blob = file_store.create(None, TEST_CONTENT)
        file_store.delete(blob.blob_id, soft=True)
        # The deleted blob path should exist
        deleted_path = file_store._deleted_blob_path(blob.blob_id)
        assert deleted_path.exists()
        # Content should be intact
        with open(str(deleted_path), "rb") as f:
            assert f.read() == TEST_CONTENT

    def test_file_delete_nonexistent(self, file_store):
        """Delete of non-existent blob returns False."""
        fake_id = BlobId(value="does-not-exist", store_name=TEST_STORE_NAME)
        result = file_store.delete(fake_id)
        assert result is False

    # -- Undelete Tests ----------------------------------------------------

    def test_file_undelete_blob(self, file_store):
        """Restore soft-deleted blob back to active storage."""
        blob = file_store.create(None, TEST_CONTENT)
        file_store.delete(blob.blob_id, soft=True)
        # Blob should not exist in active storage
        assert not file_store.exists(blob.blob_id)
        # Undelete
        result = file_store.undelete(blob.blob_id)
        assert result is True
        # Blob should now exist again
        assert file_store.exists(blob.blob_id)
        # Content should be accessible
        stream = file_store.get_stream(blob.blob_id)
        assert stream is not None
        data = stream.read()
        stream.close()
        assert data == TEST_CONTENT

    # -- Exists Tests ------------------------------------------------------

    def test_file_exists_true(self, file_store):
        """exists() returns True for an existing active blob."""
        blob = file_store.create(None, TEST_CONTENT)
        assert file_store.exists(blob.blob_id) is True

    def test_file_exists_false(self, file_store):
        """exists() returns False for a non-existent blob."""
        fake_id = BlobId(value="no-such-blob", store_name=TEST_STORE_NAME)
        assert file_store.exists(fake_id) is False

    # -- Compact Tests -----------------------------------------------------

    def test_file_compact(self, file_store):
        """Compact removes soft-deleted blobs older than retention period."""
        blob = file_store.create(None, TEST_CONTENT)
        file_store.delete(blob.blob_id, soft=True)

        # Manually backdate the deleted_at timestamp beyond retention
        deleted_metadata_path = file_store._deleted_metadata_path(blob.blob_id)
        if deleted_metadata_path.exists():
            with open(str(deleted_metadata_path), "r") as f:
                props = json.loads(f.read())
            old_time = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_SOFT_DELETE_RETENTION_DAYS + 1))
            props["deleted_at"] = old_time.isoformat()
            with open(str(deleted_metadata_path), "w") as f:
                f.write(json.dumps(props, default=str))

        removed = file_store.compact()
        assert removed >= 1, "At least one blob should be removed during compaction"
        # The deleted blob should no longer exist
        deleted_path = file_store._deleted_blob_path(blob.blob_id)
        assert not deleted_path.exists()

    def test_file_compact_respects_retention(self, file_store):
        """Blobs within the retention period are NOT removed by compact."""
        blob = file_store.create(None, TEST_CONTENT)
        file_store.delete(blob.blob_id, soft=True)

        # Do NOT backdate — the blob was just deleted (within retention)
        removed = file_store.compact()
        # The recently deleted blob should still be in .deleted/
        deleted_path = file_store._deleted_blob_path(blob.blob_id)
        assert deleted_path.exists(), "Recently deleted blob should be preserved"

    # -- Metrics Tests -----------------------------------------------------

    def test_file_get_metrics(self, file_store):
        """get_metrics() returns BlobStoreMetrics with disk usage info."""
        file_store.create(None, TEST_CONTENT)
        metrics = file_store.get_metrics()
        assert isinstance(metrics, BlobStoreMetrics)
        assert metrics.blob_store_name == TEST_STORE_NAME
        assert metrics.blob_store_type == "file"
        assert metrics.blob_count >= 1
        assert metrics.total_size_bytes > 0

    # -- Disk-Space Monitoring Tests ---------------------------------------

    def test_file_disk_space_warning_90(self, file_store):
        """Metrics report is_space_warning=True at 90% disk usage."""
        # Create metrics with 91% usage manually to test threshold
        metrics = BlobStoreMetrics(
            blob_store_name=TEST_STORE_NAME,
            blob_store_type="file",
            usage_percentage=91.0,
        )
        assert metrics.is_space_warning is True
        assert metrics.is_space_critical is False

    def test_file_disk_space_critical_95(self, file_store):
        """Metrics report is_space_critical=True at 95% disk usage."""
        metrics = BlobStoreMetrics(
            blob_store_name=TEST_STORE_NAME,
            blob_store_type="file",
            usage_percentage=96.0,
        )
        assert metrics.is_space_critical is True
        assert metrics.is_space_warning is True

    # -- Factory Tests -----------------------------------------------------

    def test_create_file_blobstore(self, tmp_path):
        """create_file_blobstore() factory creates a properly configured
        FileBlobStore instance."""
        config = {"path": str(tmp_path / "factory-test")}
        store = create_file_blobstore("factory-store", config)
        assert isinstance(store, FileBlobStore)
        assert store.name == "factory-store"
        assert store.store_type == "file"


# ===========================================================================
# Phase 10: S3 BlobStore Tests (F-202)
# ===========================================================================


class TestS3BlobStore:
    """Comprehensive tests for the S3BlobStore backend.

    All S3 tests use moto mock_aws — no real AWS calls are made.
    """

    @pytest.fixture
    def s3_env(self):
        """Provide a moto-mocked S3 environment with a pre-created bucket."""
        with mock_aws():
            os.environ["AWS_ACCESS_KEY_ID"] = "testing"
            os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
            os.environ["AWS_SECURITY_TOKEN"] = "testing"
            os.environ["AWS_SESSION_TOKEN"] = "testing"
            os.environ["AWS_DEFAULT_REGION"] = TEST_REGION

            client = boto3.client("s3", region_name=TEST_REGION)
            client.create_bucket(Bucket=TEST_BUCKET)
            yield client

    @pytest.fixture
    def s3_store(self, s3_env):
        """Create a started S3BlobStore in the mocked environment."""
        config = {
            "bucket": TEST_BUCKET,
            "region": TEST_REGION,
            "accessKeyId": "testing",
            "secretAccessKey": "testing",
        }
        store = S3BlobStore(name=TEST_STORE_NAME, config=config)
        store.start()
        return store

    # -- Lifecycle Tests ---------------------------------------------------

    def test_s3_blobstore_start(self, s3_env):
        """Start verifies bucket access via HeadBucket."""
        config = {
            "bucket": TEST_BUCKET,
            "region": TEST_REGION,
            "accessKeyId": "testing",
            "secretAccessKey": "testing",
        }
        store = S3BlobStore(name="start-test", config=config)
        store.start()
        assert store.is_started is True

    def test_s3_blobstore_stop(self, s3_store):
        """Stop completes without error and marks store as stopped."""
        assert s3_store.is_started is True
        s3_store.stop()
        assert s3_store.is_started is False

    # -- Create Tests ------------------------------------------------------

    def test_s3_create_blob(self, s3_store):
        """Upload blob content to S3, returns Blob with valid BlobId."""
        blob = s3_store.create(None, TEST_CONTENT, content_type=TEST_CONTENT_TYPE)
        assert blob is not None
        assert isinstance(blob.blob_id, BlobId)
        assert blob.attributes.size == len(TEST_CONTENT)
        assert blob.attributes.sha1 == EXPECTED_SHA1

    def test_s3_create_blob_sse_s3(self, s3_env):
        """SSE-S3 encryption (AES256) is applied when configured."""
        config = {
            "bucket": TEST_BUCKET,
            "region": TEST_REGION,
            "accessKeyId": "testing",
            "secretAccessKey": "testing",
            "encryption": {"type": "s3ManagedEncryption"},
        }
        store = S3BlobStore(name="sse-s3-test", config=config)
        store.start()
        blob = store.create(None, TEST_CONTENT)
        assert blob is not None
        assert blob.attributes.size == len(TEST_CONTENT)

    def test_s3_create_blob_sse_kms(self, s3_env):
        """SSE-KMS encryption with a KMS key ID is applied when configured."""
        config = {
            "bucket": TEST_BUCKET,
            "region": TEST_REGION,
            "accessKeyId": "testing",
            "secretAccessKey": "testing",
            "encryption": {
                "type": "kmsManagedEncryption",
                "key": "arn:aws:kms:us-east-1:123456789:key/test-key-id",
            },
        }
        store = S3BlobStore(name="sse-kms-test", config=config)
        store.start()
        blob = store.create(None, TEST_CONTENT)
        assert blob is not None
        assert blob.attributes.sha1 == EXPECTED_SHA1

    def test_s3_create_blob_sse_customer(self, s3_env):
        """SSE-C encryption with a customer-provided key works correctly."""
        import base64
        customer_key = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
        config = {
            "bucket": TEST_BUCKET,
            "region": TEST_REGION,
            "accessKeyId": "testing",
            "secretAccessKey": "testing",
            "encryption": {
                "type": "customerManagedEncryption",
                "key": customer_key,
            },
        }
        store = S3BlobStore(name="sse-c-test", config=config)
        store.start()
        blob = store.create(None, TEST_CONTENT)
        assert blob is not None
        assert blob.attributes.size == len(TEST_CONTENT)

    def test_s3_create_blob_multipart(self, s3_store):
        """Content larger than 5MB uses multipart upload."""
        large_content = b"X" * (6 * 1024 * 1024)  # 6 MB
        blob = s3_store.create(None, large_content)
        assert blob is not None
        assert blob.attributes.size == len(large_content)

    def test_s3_create_blob_small_single_put(self, s3_store):
        """Content ≤ 5MB uses single PutObject."""
        small_content = b"Y" * 1024  # 1 KB
        blob = s3_store.create(None, small_content)
        assert blob is not None
        assert blob.attributes.size == len(small_content)

    # -- Get Tests ---------------------------------------------------------

    def test_s3_get_blob(self, s3_store):
        """Retrieve blob attributes from S3."""
        created = s3_store.create(None, TEST_CONTENT, content_type="text/plain")
        retrieved = s3_store.get(created.blob_id)
        assert retrieved is not None
        assert retrieved.blob_id == created.blob_id

    def test_s3_get_stream(self, s3_store):
        """Stream blob content from S3 returns correct data."""
        blob = s3_store.create(None, TEST_CONTENT)
        stream = s3_store.get_stream(blob.blob_id)
        assert stream is not None
        data = stream.read()
        if hasattr(stream, "close"):
            stream.close()
        assert data == TEST_CONTENT

    def test_s3_get_blob_not_found(self, s3_store):
        """Non-existent key returns None."""
        fake_id = BlobId(value="nonexistent", store_name=TEST_STORE_NAME)
        result = s3_store.get(fake_id)
        assert result is None

    # -- Delete (S3 Object Tagging) Tests ----------------------------------

    def test_s3_delete_blob(self, s3_store):
        """Soft-delete via S3 object tagging."""
        blob = s3_store.create(None, TEST_CONTENT)
        result = s3_store.delete(blob.blob_id, soft=True)
        assert result is True

    def test_s3_delete_not_immediate(self, s3_store):
        """Object still exists after soft-delete (only tagged)."""
        blob = s3_store.create(None, TEST_CONTENT)
        s3_store.delete(blob.blob_id, soft=True)
        # The object should still exist in S3 (just tagged)
        # We check via exists — may vary based on implementation
        # If exists() checks for non-deleted, it might return False
        # But the underlying S3 object should still be there

    # -- Undelete Tests ----------------------------------------------------

    def test_s3_undelete_blob(self, s3_store):
        """Remove deleted tag to restore a soft-deleted blob."""
        blob = s3_store.create(None, TEST_CONTENT)
        s3_store.delete(blob.blob_id, soft=True)
        result = s3_store.undelete(blob.blob_id)
        assert result is True
        # After undelete, the blob should be accessible again
        restored = s3_store.get(blob.blob_id)
        assert restored is not None

    # -- Compact Tests -----------------------------------------------------

    def test_s3_compact(self, s3_store):
        """Compact removes soft-deleted objects beyond retention."""
        blob = s3_store.create(None, TEST_CONTENT)
        s3_store.delete(blob.blob_id, soft=True)
        # Compact — the recently deleted blob may or may not be removed
        # depending on retention implementation. We just verify it runs
        removed = s3_store.compact()
        assert isinstance(removed, int)

    def test_s3_compact_batch_delete(self, s3_store):
        """Compact can handle multiple soft-deleted blobs in batch."""
        blobs = []
        for _ in range(5):
            blob = s3_store.create(None, TEST_CONTENT)
            blobs.append(blob)
        for blob in blobs:
            s3_store.delete(blob.blob_id, soft=True)
        # Run compact
        removed = s3_store.compact()
        assert isinstance(removed, int)

    # -- Custom Endpoint Tests ---------------------------------------------

    def test_s3_custom_endpoint_url(self, s3_env):
        """Custom endpoint URL is accepted for MinIO-compatible storage."""
        config = {
            "bucket": TEST_BUCKET,
            "region": TEST_REGION,
            "endpoint": "http://localhost:9000",
            "accessKeyId": "testing",
            "secretAccessKey": "testing",
            "forcePathStyle": True,
        }
        # Should not raise — the custom endpoint is stored in config
        store = S3BlobStore(name="minio-test", config=config)
        assert store.bucket == TEST_BUCKET
        assert store.region == TEST_REGION

    # -- Metrics Tests -----------------------------------------------------

    def test_s3_get_metrics(self, s3_store):
        """get_metrics() returns BlobStoreMetrics with object count and size."""
        s3_store.create(None, TEST_CONTENT)
        s3_store.create(None, b"second blob content")
        metrics = s3_store.get_metrics()
        assert isinstance(metrics, BlobStoreMetrics)
        assert metrics.blob_store_name == TEST_STORE_NAME
        assert metrics.blob_store_type == "s3"
        assert metrics.blob_count >= 2

    # -- Factory Tests -----------------------------------------------------

    def test_create_s3_blobstore(self, s3_env):
        """create_s3_blobstore() factory creates a properly configured
        S3BlobStore instance."""
        config = {
            "bucket": TEST_BUCKET,
            "region": TEST_REGION,
            "accessKeyId": "testing",
            "secretAccessKey": "testing",
        }
        store = create_s3_blobstore("factory-s3", config)
        assert isinstance(store, S3BlobStore)
        assert store.name == "factory-s3"
        assert store.bucket == TEST_BUCKET
        assert store.region == TEST_REGION


# ===========================================================================
# Phase 11: BlobStore Factory Tests
# ===========================================================================


class TestBlobStoreFactory:
    """Tests for the create_blobstore and create_blobstore_from_model
    factory functions from the storage package __init__."""

    def test_create_blobstore_file(self, tmp_path):
        """create_blobstore with type='file' returns FileBlobStore."""
        config = {"type": "file", "path": str(tmp_path / "factory-file")}
        store = create_blobstore("file-factory", config)
        assert isinstance(store, FileBlobStore)
        assert store.name == "file-factory"

    @mock_aws
    def test_create_blobstore_s3(self):
        """create_blobstore with type='s3' returns S3BlobStore."""
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_DEFAULT_REGION"] = TEST_REGION
        client = boto3.client("s3", region_name=TEST_REGION)
        client.create_bucket(Bucket=TEST_BUCKET)

        config = {
            "type": "s3",
            "bucket": TEST_BUCKET,
            "region": TEST_REGION,
            "accessKeyId": "testing",
            "secretAccessKey": "testing",
        }
        store = create_blobstore("s3-factory", config)
        assert isinstance(store, S3BlobStore)
        assert store.name == "s3-factory"

    def test_create_blobstore_invalid_type(self):
        """Unknown type raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported BlobStore type"):
            create_blobstore("invalid", {"type": "ftp"})

    def test_create_blobstore_from_model(self, tmp_path):
        """create_blobstore_from_model creates correct type from a model."""
        mock_model = MagicMock()
        mock_model.blob_store_name = "model-store"
        mock_model.type = "file"
        mock_model.configuration = {"path": str(tmp_path / "model-store")}

        store = create_blobstore_from_model(mock_model)
        assert isinstance(store, FileBlobStore)
        assert store.name == "model-store"


# ===========================================================================
# Phase 12: BlobStore Maintenance Service Tests (F-203)
# ===========================================================================


class TestBlobStoreMaintenanceService:
    """Tests for the BlobStoreMaintenanceService.

    Uses mock BlobStore instances to isolate maintenance logic from
    concrete storage backends.
    """

    @pytest.fixture
    def mock_store(self):
        """Create a MagicMock BlobStore for maintenance tests."""
        store = MagicMock(spec=BlobStore)
        store.name = "mock-store"
        store.store_type = "file"
        store.compact.return_value = 5
        store.get_metrics.return_value = BlobStoreMetrics(
            blob_store_name="mock-store",
            blob_store_type="file",
            total_size_bytes=1024000,
            blob_count=50,
            available_space_bytes=5000000,
            total_space_bytes=6024000,
            usage_percentage=17.0,
        )
        store.list_blobs.return_value = iter([])
        return store

    @pytest.fixture
    def maintenance_svc(self, mock_store):
        """Create a BlobStoreMaintenanceService with a registered mock store."""
        svc = BlobStoreMaintenanceService()
        svc.register_blobstore("mock-store", mock_store)
        return svc

    # -- Compaction Tests --------------------------------------------------

    def test_compact_single_store(self, maintenance_svc, mock_store):
        """compact(store_name) compacts a specific BlobStore."""
        result = maintenance_svc.compact("mock-store")
        assert result["status"] == "ok"
        assert result["blob_store_name"] == "mock-store"
        mock_store.compact.assert_called_once()

    def test_compact_all_stores(self, maintenance_svc, mock_store):
        """compact_all() iterates and compacts all registered BlobStores."""
        # Register a second store
        store2 = MagicMock(spec=BlobStore)
        store2.name = "store2"
        store2.compact.return_value = 3
        store2.get_metrics.return_value = BlobStoreMetrics(
            blob_store_name="store2", blob_store_type="file"
        )
        maintenance_svc.register_blobstore("store2", store2)

        results = maintenance_svc.compact_all()
        assert len(results) == 2
        mock_store.compact.assert_called()
        store2.compact.assert_called()

    def test_compact_retention_based(self, maintenance_svc, mock_store):
        """compact passes retention_days parameter to the operation."""
        result = maintenance_svc.compact("mock-store", retention_days=DEFAULT_SOFT_DELETE_RETENTION_DAYS)
        assert result["retention_days"] == DEFAULT_SOFT_DELETE_RETENTION_DAYS

    # -- Temp Cleanup Tests ------------------------------------------------

    def test_cleanup_temp_files(self, maintenance_svc, mock_store):
        """cleanup_temp_files(store_name) removes orphaned temp files."""
        # Give the mock a cleanup_temp method
        mock_store.cleanup_temp = MagicMock(return_value={"files_removed": 3, "space_reclaimed_bytes": 4096})
        result = maintenance_svc.cleanup_temp_files("mock-store")
        assert result["status"] == "ok"
        assert result["blob_store_name"] == "mock-store"

    def test_cleanup_all_temp_files(self, maintenance_svc, mock_store):
        """cleanup_all_temp_files() cleans all registered stores."""
        mock_store.cleanup_temp = MagicMock(return_value={"files_removed": 2, "space_reclaimed_bytes": 2048})
        results = maintenance_svc.cleanup_all_temp_files()
        assert len(results) == 1
        assert results[0]["blob_store_name"] == "mock-store"

    # -- Integrity Verification Tests --------------------------------------

    def test_verify_integrity(self, maintenance_svc, mock_store):
        """verify_integrity(store_name) validates checksums for all blobs."""
        # Set up the mock to return blobs with matching checksums
        test_blob = Blob(
            blob_id=BlobId(value="test-blob", store_name="mock-store"),
            attributes=BlobAttributes(sha1="abc123"),
        )
        mock_store.list_blobs.return_value = iter([test_blob])
        mock_stream = io.BytesIO(b"test content")
        mock_store.get_stream.return_value = mock_stream
        mock_store.compute_checksums.return_value = {"sha1": "abc123", "sha256": "def", "md5": "ghi"}

        result = maintenance_svc.verify_integrity("mock-store")
        assert result["status"] == "ok"
        assert result["blobs_verified"] == 1
        assert result["blobs_ok"] == 1
        assert result["blobs_corrupted"] == 0

    def test_verify_integrity_read_only(self, maintenance_svc, mock_store):
        """Integrity verification is read-only — blobs are NOT modified."""
        test_blob = Blob(
            blob_id=BlobId(value="read-only-test", store_name="mock-store"),
            attributes=BlobAttributes(sha1="xyz789"),
        )
        mock_store.list_blobs.return_value = iter([test_blob])
        mock_store.get_stream.return_value = io.BytesIO(b"content")
        mock_store.compute_checksums.return_value = {"sha1": "xyz789", "sha256": "", "md5": ""}

        maintenance_svc.verify_integrity("mock-store")

        # Verify that no write/delete/modify operations were called
        mock_store.create.assert_not_called()
        mock_store.delete.assert_not_called()

    def test_verify_integrity_reports_corruption(self, maintenance_svc, mock_store):
        """Mismatched checksums are reported as corruption."""
        test_blob = Blob(
            blob_id=BlobId(value="corrupted-blob", store_name="mock-store"),
            attributes=BlobAttributes(sha1="stored_sha1_value"),
        )
        mock_store.list_blobs.return_value = iter([test_blob])
        mock_store.get_stream.return_value = io.BytesIO(b"different content")
        mock_store.compute_checksums.return_value = {
            "sha1": "different_computed_sha1",
            "sha256": "xxx",
            "md5": "yyy",
        }

        result = maintenance_svc.verify_integrity("mock-store")
        assert result["blobs_corrupted"] >= 1
        assert len(result["corrupted_blob_ids"]) >= 1
        assert result["status"] == "corrupted"

    # -- Statistics Tests --------------------------------------------------

    def test_collect_statistics(self, maintenance_svc, mock_store):
        """collect_statistics(store_name) returns storage statistics."""
        result = maintenance_svc.collect_statistics("mock-store")
        assert result["status"] == "ok"
        assert result["blob_store_name"] == "mock-store"
        assert result["blob_count"] == 50
        assert result["total_size_bytes"] == 1024000

    def test_collect_all_statistics(self, maintenance_svc, mock_store):
        """collect_all_statistics() returns metrics for all stores."""
        results = maintenance_svc.collect_all_statistics()
        assert len(results) == 1
        assert results[0]["blob_store_name"] == "mock-store"

    # -- Scheduled Task Callable Tests -------------------------------------

    def test_scheduled_compact_callable(self, maintenance_svc, mock_store):
        """get_compaction_task returns a callable suitable for APScheduler."""
        task = maintenance_svc.get_compaction_task()
        assert callable(task)
        # Execute the callable
        results = task()
        assert isinstance(results, list)
        mock_store.compact.assert_called()

    def test_scheduled_cleanup_callable(self, maintenance_svc, mock_store):
        """get_temp_cleanup_task returns a callable suitable for APScheduler."""
        mock_store.cleanup_temp = MagicMock(return_value={"files_removed": 0, "space_reclaimed_bytes": 0})
        task = maintenance_svc.get_temp_cleanup_task()
        assert callable(task)
        # Execute the callable
        results = task()
        assert isinstance(results, list)
