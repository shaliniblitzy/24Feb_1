"""
Asset/Component data model unit tests for ``src/models/asset.py``.

Covers instantiation, BlobStore references, metadata constraints, hash
storage (SHA-1, SHA-256, MD5), repository relationship, serialisation,
edge cases, error handling, and database persistence.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from src.models.asset import Asset
from src.models.repository import Repository
from tests.fixtures.artifact_data import (
    make_binary_artifact,
    make_maven_artifact,
    make_npm_artifact,
    make_zero_byte_artifact,
    make_large_artifact,
    make_unicode_filename_artifact,
    SMALL_ARTIFACT_SIZE,
    MEDIUM_ARTIFACT_SIZE,
)
from tests.fixtures.repository_data import make_hosted_repo

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_asset(**kw):
    """Create an Asset instance with sensible defaults."""
    defaults = dict(
        id=str(uuid.uuid4()),
        name="artifact-1.0.0.jar",
        path="/com/example/artifact/1.0/artifact-1.0.0.jar",
        format="maven",
        content_type="application/java-archive",
        size=SMALL_ARTIFACT_SIZE,
    )
    defaults.update(kw)
    return Asset(**defaults)


def _make_repo(db_session, **kw):
    """Create and persist a Repository instance with sensible defaults."""
    repo_data = make_hosted_repo(format_type=kw.pop("format_type", "maven"))
    repo = Repository(
        id=repo_data.get("id", str(uuid.uuid4())),
        name=repo_data["name"],
        format=repo_data["format"],
        type=repo_data["type"],
        online=repo_data.get("online", True),
        description=repo_data.get("description", ""),
    )
    for key, val in kw.items():
        setattr(repo, key, val)
    db_session.add(repo)
    db_session.flush()
    return repo


# =========================================================================
# Phase 2 — Happy Path: Model Instantiation
# =========================================================================


def test_asset_model_instantiation_with_required_fields():
    """Asset created with name, path, format, content_type stores all fields."""
    asset = Asset(
        name="lib-core-2.0.jar",
        path="/org/lib/core/2.0/lib-core-2.0.jar",
        format="maven",
        content_type="application/java-archive",
    )
    assert asset is not None
    assert asset.name == "lib-core-2.0.jar"
    assert asset.path == "/org/lib/core/2.0/lib-core-2.0.jar"
    assert asset.format == "maven"


def test_asset_model_default_values(db_session):
    """Asset with minimal required fields receives correct defaults after flush."""
    asset = Asset(id=str(uuid.uuid4()), name="defaults.bin", path="/defaults.bin")
    db_session.add(asset)
    db_session.flush()
    assert asset.size == 0
    assert asset.sha1 is None
    assert asset.sha256 is None
    assert asset.md5 is None


def test_asset_model_with_full_metadata():
    """Asset with every optional field stores all values correctly."""
    asset = _make_asset(
        sha1="a" * 40,
        sha256="b" * 64,
        md5="c" * 32,
        blob_ref="default",
    )
    assert asset.sha1 == "a" * 40
    assert asset.sha256 == "b" * 64
    assert asset.md5 == "c" * 32
    assert asset.blob_ref == "default"


def test_asset_model_with_maven_artifact_data():
    """Asset populated from make_maven_artifact() has maven format and path."""
    data = make_maven_artifact(group_id="com.acme", artifact_id="widget", version="3.0.0")
    asset = Asset(
        name=f"widget-3.0.0.jar",
        path=data["path"],
        format="maven",
        content_type=data["content_type"],
        size=data["size"],
        sha1=data["sha1"],
        sha256=data["sha256"],
        md5=data["md5"],
    )
    assert asset.format == "maven"
    assert "com/acme" in asset.path


def test_asset_model_with_npm_artifact_data():
    """Asset populated from make_npm_artifact() has npm format and gzip type."""
    data = make_npm_artifact(package_name="@test/widget", version="1.2.3")
    asset = Asset(
        name="widget-1.2.3.tgz",
        path="/@test/widget/-/widget-1.2.3.tgz",
        format="npm",
        content_type=data["content_type"],
        size=data["size"],
        sha256=data["sha256"],
    )
    assert asset.format == "npm"
    assert asset.content_type == "application/gzip"


# =========================================================================
# Phase 3 — Hash Storage Tests (SHA-1, SHA-256, MD5)
# =========================================================================


def test_asset_sha1_hash_storage():
    """SHA-1 field stores a valid 40-character lowercase hex string."""
    sha1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    asset = _make_asset(sha1=sha1)
    assert asset.sha1 == sha1
    assert len(asset.sha1) == 40


def test_asset_sha256_hash_storage():
    """SHA-256 field stores a valid 64-character hex string."""
    sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    asset = _make_asset(sha256=sha256)
    assert asset.sha256 == sha256
    assert len(asset.sha256) == 64


def test_asset_md5_hash_storage():
    """MD5 field stores a valid 32-character hex string."""
    md5 = "d41d8cd98f00b204e9800998ecf8427e"
    asset = _make_asset(md5=md5)
    assert asset.md5 == md5
    assert len(asset.md5) == 32


def test_asset_all_hashes_stored_together():
    """All three hash fields populated from make_binary_artifact() are valid."""
    data = make_binary_artifact()
    asset = _make_asset(sha1=data["sha1"], sha256=data["sha256"], md5=data["md5"])
    assert asset.sha1 is not None and len(asset.sha1) == 40
    assert asset.sha256 is not None and len(asset.sha256) == 64
    assert asset.md5 is not None and len(asset.md5) == 32


def test_asset_hash_fields_accept_valid_hex():
    """Each hash field accepts and stores a valid hex string of correct length."""
    asset = _make_asset(sha1="ab" * 20, sha256="cd" * 32, md5="ef" * 16)
    assert all(c in "0123456789abcdef" for c in asset.sha1)
    assert all(c in "0123456789abcdef" for c in asset.sha256)
    assert all(c in "0123456789abcdef" for c in asset.md5)


def test_asset_hash_consistency_with_content():
    """Hashes from make_binary_artifact() match when stored on Asset."""
    data = make_binary_artifact(size=MEDIUM_ARTIFACT_SIZE)
    asset = _make_asset(sha1=data["sha1"], sha256=data["sha256"], md5=data["md5"])
    assert asset.sha1 == data["sha1"]
    assert asset.sha256 == data["sha256"]
    assert asset.md5 == data["md5"]


# =========================================================================
# Phase 4 — BlobStore Reference Tests
# =========================================================================


def test_asset_blobstore_reference_set():
    """Asset with blob_ref stores the reference as a non-empty string."""
    asset = _make_asset(blob_ref="primary-store")
    assert asset.blob_ref == "primary-store"
    assert isinstance(asset.blob_ref, str) and len(asset.blob_ref) > 0


def test_asset_blobstore_reference_default_is_none():
    """Asset without explicit blob_ref defaults to None (nullable field)."""
    asset = Asset(name="no-blob.bin", path="/no-blob.bin")
    assert asset.blob_ref is None
    assert asset.name == "no-blob.bin"


def test_asset_blobstore_reference_points_to_valid_store(db_session, sample_blobstore):
    """Asset blob_ref matching a BlobStore name can be used for lookup."""
    asset = _make_asset(blob_ref=sample_blobstore.name)
    db_session.add(asset)
    db_session.flush()
    assert asset.blob_ref == sample_blobstore.name
    assert sample_blobstore.name is not None


# =========================================================================
# Phase 5 — Repository Relationship Tests
# =========================================================================


def test_asset_belongs_to_repository(db_session):
    """Asset linked to a Repository via repository_id navigates relationship."""
    repo = _make_repo(db_session)
    asset = _make_asset(repository_id=repo.id)
    db_session.add(asset)
    db_session.flush()
    assert asset.repository_id == repo.id
    assert asset.repository.name == repo.name


def test_asset_repository_id_foreign_key(db_session):
    """Asset with repository_id references the correct repository."""
    repo = _make_repo(db_session)
    asset = _make_asset(repository_id=repo.id)
    db_session.add(asset)
    db_session.flush()
    assert asset.repository_id == repo.id
    assert asset.repository is not None


def test_asset_without_repository_is_allowed(db_session):
    """Asset with repository_id=None persists successfully (nullable FK)."""
    asset = _make_asset(repository_id=None)
    db_session.add(asset)
    db_session.flush()
    assert asset.repository_id is None
    assert asset.id is not None


def test_multiple_assets_per_repository(db_session):
    """Repository with multiple linked assets lists them all."""
    repo = _make_repo(db_session)
    asset1 = _make_asset(name="a1.jar", path="/a1.jar", repository_id=repo.id)
    asset2 = _make_asset(name="a2.jar", path="/a2.jar", repository_id=repo.id)
    db_session.add_all([asset1, asset2])
    db_session.flush()
    result = repo.assets.all()
    assert len(result) == 2
    assert {a.name for a in result} == {"a1.jar", "a2.jar"}


def test_asset_deletion_does_not_delete_repository(db_session):
    """Deleting an asset leaves the parent repository intact."""
    repo = _make_repo(db_session)
    asset = _make_asset(repository_id=repo.id)
    db_session.add(asset)
    db_session.flush()
    asset_id = asset.id
    repo_id = repo.id
    db_session.delete(asset)
    db_session.flush()
    assert db_session.get(Repository, repo_id) is not None
    assert db_session.get(Asset, asset_id) is None


# =========================================================================
# Phase 6 — Metadata Constraint Tests
# =========================================================================


def test_asset_size_field_stores_integer():
    """Size field stores an integer value correctly."""
    asset = _make_asset(size=1024)
    assert asset.size == 1024
    assert isinstance(asset.size, int)


def test_asset_size_field_zero_bytes():
    """Zero-byte asset from make_zero_byte_artifact() is accepted."""
    data = make_zero_byte_artifact()
    asset = _make_asset(size=data["size"])
    assert asset.size == 0
    assert asset.name is not None


def test_asset_size_field_large_value():
    """Large size from make_large_artifact() is stored correctly."""
    data = make_large_artifact()
    asset = _make_asset(size=data["size"])
    assert asset.size == data["size"]
    assert asset.size > 0


def test_asset_content_type_field():
    """Content type stores a valid MIME string correctly."""
    asset = _make_asset(content_type="application/java-archive")
    assert asset.content_type == "application/java-archive"
    assert "/" in asset.content_type


def test_asset_path_field():
    """Path field stores a repository-tree path string."""
    path = "/com/example/artifact/1.0/artifact-1.0.jar"
    asset = _make_asset(path=path)
    assert asset.path == path
    assert asset.path.startswith("/")


def test_asset_name_field():
    """Name field stores the filename portion."""
    asset = _make_asset(name="artifact-1.0.jar")
    assert asset.name == "artifact-1.0.jar"
    assert isinstance(asset.name, str)


# =========================================================================
# Phase 7 — Serialization Tests
# =========================================================================


def test_asset_to_dict_serialization():
    """to_dict() returns all expected keys with correct values."""
    asset = _make_asset(
        sha1="a" * 40, sha256="b" * 64, md5="c" * 32, blob_ref="store-1",
    )
    d = asset.to_dict()
    assert d["name"] == asset.name
    assert d["path"] == asset.path
    assert d["format"] == "maven"
    assert d["size"] == SMALL_ARTIFACT_SIZE


def test_asset_to_dict_includes_hash_values():
    """Serialised dict includes sha1, sha256, md5 fields with values."""
    asset = _make_asset(sha1="a" * 40, sha256="b" * 64, md5="c" * 32)
    d = asset.to_dict()
    assert d["sha1"] == "a" * 40
    assert d["sha256"] == "b" * 64
    assert d["md5"] == "c" * 32


def test_asset_repr_string():
    """repr() includes the asset name and path."""
    asset = _make_asset(name="widget.jar", path="/widget.jar")
    r = repr(asset)
    assert "widget.jar" in r
    assert "Asset" in r


# =========================================================================
# Phase 8 — Edge Cases
# =========================================================================


def test_asset_with_unicode_filename():
    """Asset accepts Unicode characters in name field."""
    data = make_unicode_filename_artifact()
    asset = _make_asset(name=data["filename"], path=f"/{data['filename']}")
    assert asset.name == data["filename"]
    assert len(asset.name) > 0


def test_asset_with_very_long_path(db_session):
    """Asset with a 512+ character path persists successfully."""
    long_path = "/" + "/".join(["segment"] * 130)
    asset = _make_asset(path=long_path)
    db_session.add(asset)
    db_session.flush()
    assert len(asset.path) > 512
    assert asset.id is not None


def test_asset_with_empty_content_type():
    """Asset with empty string content_type stores it as-is."""
    asset = _make_asset(content_type="")
    assert asset.content_type == ""
    assert asset.name is not None


def test_asset_format_matches_repository_format(db_session):
    """Asset format matching its repository format is accepted."""
    repo = _make_repo(db_session, format_type="maven")
    asset = _make_asset(format="maven", repository_id=repo.id)
    db_session.add(asset)
    db_session.flush()
    assert asset.format == repo.format
    assert asset.repository_id == repo.id


# =========================================================================
# Phase 9 — Error Cases
# =========================================================================


def test_asset_null_name_raises_error(db_session):
    """NOT NULL constraint on name raises IntegrityError."""
    asset = Asset(id=str(uuid.uuid4()), name=None, path="/some/path")
    db_session.add(asset)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_asset_null_path_raises_error(db_session):
    """NOT NULL constraint on path raises IntegrityError."""
    asset = Asset(id=str(uuid.uuid4()), name="test.jar", path=None)
    db_session.add(asset)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_asset_invalid_hash_format_stores_value():
    """Non-hex hash strings are stored as-is (no model-level hash validation)."""
    asset = _make_asset(sha1="not-a-valid-hex-string-at-all-xxxx")
    assert asset.sha1 == "not-a-valid-hex-string-at-all-xxxx"
    assert asset.name is not None


def test_asset_negative_size_raises_error():
    """Negative size triggers a ValueError from the validates decorator."""
    with pytest.raises(ValueError, match="cannot be negative"):
        _make_asset(size=-1)


# =========================================================================
# Phase 10 — Database Persistence Tests
# =========================================================================


def test_asset_persists_to_database(db_session):
    """Asset round-trips through the database with all fields intact."""
    data = make_binary_artifact()
    asset = _make_asset(sha1=data["sha1"], sha256=data["sha256"], md5=data["md5"])
    db_session.add(asset)
    db_session.flush()
    fetched = db_session.get(Asset, asset.id)
    assert fetched is not None
    assert fetched.sha1 == data["sha1"]
    assert fetched.sha256 == data["sha256"]


def test_asset_query_by_repository(db_session):
    """Querying assets by repository_id returns the correct subset."""
    repo1 = _make_repo(db_session, format_type="maven")
    repo2 = _make_repo(db_session, format_type="npm")
    a1 = _make_asset(name="r1.jar", path="/r1.jar", repository_id=repo1.id)
    a2 = _make_asset(name="r2.tgz", path="/r2.tgz", repository_id=repo2.id)
    db_session.add_all([a1, a2])
    db_session.flush()
    results = db_session.query(Asset).filter_by(repository_id=repo1.id).all()
    assert len(results) == 1
    assert results[0].name == "r1.jar"


def test_asset_update_hash_persists(db_session):
    """Updated sha256 persists after commit and re-query."""
    asset = _make_asset(sha256="a" * 64)
    db_session.add(asset)
    db_session.flush()
    new_hash = "f" * 64
    asset.sha256 = new_hash
    db_session.flush()
    fetched = db_session.get(Asset, asset.id)
    assert fetched.sha256 == new_hash
    assert fetched.sha256 != "a" * 64


def test_asset_delete_removes_from_db(db_session):
    """Deleted asset is no longer found by primary key query."""
    asset = _make_asset()
    db_session.add(asset)
    db_session.flush()
    asset_id = asset.id
    db_session.delete(asset)
    db_session.flush()
    assert db_session.get(Asset, asset_id) is None
    assert asset_id is not None
