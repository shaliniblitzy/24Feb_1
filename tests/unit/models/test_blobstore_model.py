"""
BlobStore configuration model unit tests.

Validates the ``BlobStore`` ORM model from ``src/models/blobstore.py``
covering instantiation, type-specific settings (file path vs S3
bucket/prefix), default values, field validation, serialisation, edge
cases, error handling, and database persistence.

Features: F-201 File BlobStore (Critical), F-202 S3 BlobStore (Critical).
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from src.models.blobstore import BlobStore, VALID_BLOBSTORE_TYPES
from tests.fixtures.config_data import (
    make_blobstore_config,
    make_config_with_s3_storage,
    make_config_with_file_storage,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_bs(**kw):
    """Create a file-type BlobStore with sensible defaults."""
    defaults = dict(
        id=str(uuid.uuid4()), name=f"file-{uuid.uuid4().hex[:8]}",
        type="file", path="/data/blobstore",
    )
    defaults.update(kw)
    return BlobStore(**defaults)


def _s3_bs(**kw):
    """Create an S3-type BlobStore with sensible defaults."""
    defaults = dict(
        id=str(uuid.uuid4()), name=f"s3-{uuid.uuid4().hex[:8]}",
        type="s3", bucket_name="test-bucket", prefix="artifacts/",
        region="us-east-1", endpoint_url="https://s3.amazonaws.com",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    )
    defaults.update(kw)
    return BlobStore(**defaults)


# =========================================================================
# Phase 2 — Happy Path: Model Instantiation
# =========================================================================

def test_blobstore_model_instantiation_with_required_fields():
    bs = BlobStore(name="primary", type="file")
    assert bs is not None
    assert bs.name == "primary"
    assert bs.type == "file"


def test_blobstore_model_default_values():
    bs = BlobStore(name="defaults-test", type="file")
    assert bs.path is None
    assert bs.bucket_name is None
    assert bs.soft_quota is None


def test_file_blobstore_creation():
    bs = _file_bs(name="file-default", path="/data/blobstore")
    assert bs.type == "file"
    assert bs.path == "/data/blobstore"
    assert bs.bucket_name is None
    assert bs.endpoint_url is None


def test_s3_blobstore_creation():
    bs = _s3_bs(name="s3-default", bucket_name="my-bucket", prefix="art/")
    assert bs.type == "s3"
    assert bs.bucket_name == "my-bucket"
    assert bs.prefix == "art/"
    assert bs.path is None


def test_blobstore_with_name_set():
    bs = BlobStore(name="default", type="file")
    assert bs.name == "default"
    assert isinstance(bs.name, str)


# =========================================================================
# Phase 3 — Type-Specific Settings: File BlobStore
# =========================================================================

def test_file_blobstore_path_field():
    bs = _file_bs(path="/var/data/blobstore")
    assert bs.path == "/var/data/blobstore"
    assert isinstance(bs.path, str) and len(bs.path) > 0


def test_file_blobstore_default_path():
    bs = BlobStore(name="no-path", type="file")
    assert bs.path is None
    assert bs.type == "file"


def test_file_blobstore_path_with_trailing_slash():
    bs = _file_bs(path="/data/blobstore/")
    assert bs.path == "/data/blobstore/"
    assert bs.path.endswith("/")


def test_file_blobstore_does_not_have_s3_settings():
    bs = _file_bs()
    assert bs.bucket_name is None
    assert bs.prefix is None
    assert bs.endpoint_url is None
    assert bs.region is None


# =========================================================================
# Phase 4 — Type-Specific Settings: S3 BlobStore
# =========================================================================

def test_s3_blobstore_bucket_name_field():
    bs = _s3_bs(bucket_name="test-bucket")
    assert bs.bucket_name == "test-bucket"
    assert isinstance(bs.bucket_name, str) and len(bs.bucket_name) > 0


def test_s3_blobstore_prefix_field():
    bs = _s3_bs(prefix="artifacts/")
    assert bs.prefix == "artifacts/"
    assert isinstance(bs.prefix, str)


def test_s3_blobstore_endpoint_url_field():
    bs = _s3_bs(endpoint_url="https://s3.amazonaws.com")
    assert bs.endpoint_url == "https://s3.amazonaws.com"
    assert bs.endpoint_url.startswith("https://")


def test_s3_blobstore_region_field():
    bs = _s3_bs(region="us-east-1")
    assert bs.region == "us-east-1"
    assert isinstance(bs.region, str)


def test_s3_blobstore_access_credentials():
    bs = _s3_bs(
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/bPxRfiCYEXAMPLEKEY",
    )
    assert bs.access_key_id is not None and len(bs.access_key_id) > 0
    assert bs.secret_access_key is not None


def test_s3_blobstore_does_not_have_file_path():
    bs = _s3_bs()
    assert bs.path is None
    assert bs.type == "s3"


# =========================================================================
# Phase 5 — Parametrized Type Tests
# =========================================================================

@pytest.mark.parametrize("store_type", ["file", "s3"])
def test_blobstore_type_parametrized(store_type):
    config = make_blobstore_config(store_type=store_type)
    bs = BlobStore(name=config["name"], type=config["type"])
    assert bs.type == store_type
    assert bs.name == config["name"]


@pytest.mark.parametrize(
    "store_type,expected_key",
    [("file", "path"), ("s3", "bucket")],
    ids=["file-has-path", "s3-has-bucket"],
)
def test_blobstore_config_by_type(store_type, expected_key):
    config = make_blobstore_config(store_type=store_type)
    assert config["type"] == store_type
    assert expected_key in config


# =========================================================================
# Phase 6 — Field Validation
# =========================================================================

def test_blobstore_name_required(db_session):
    bs = BlobStore(id=str(uuid.uuid4()), type="file")
    assert bs.name is None  # name was not provided
    db_session.add(bs)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_blobstore_type_required():
    # The @validates decorator rejects None before the DB constraint fires.
    with pytest.raises(ValueError, match="Invalid BlobStore type"):
        BlobStore(name="no-type-test", type=None)
    # Additionally confirm that the only accepted types are 'file' and 's3'.
    assert "file" in VALID_BLOBSTORE_TYPES
    assert "s3" in VALID_BLOBSTORE_TYPES


def test_blobstore_name_uniqueness(db_session):
    bs1 = _file_bs(name="unique-test")
    bs2 = _file_bs(name="unique-test")
    assert bs1.name == bs2.name  # both share the same name
    db_session.add(bs1)
    db_session.flush()
    db_session.add(bs2)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_blobstore_invalid_type_raises_error():
    assert "invalid_type" not in VALID_BLOBSTORE_TYPES
    with pytest.raises(ValueError, match="Invalid BlobStore type"):
        BlobStore(name="bad-type", type="invalid_type")


# =========================================================================
# Phase 7 — Serialization
# =========================================================================

def _ts():
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_blobstore_to_dict_serialization():
    bs = _file_bs(name="serial-test", path="/data/store")
    bs.created_at, bs.updated_at = _ts(), _ts()
    result = bs.to_dict()
    assert result["name"] == "serial-test"
    assert result["type"] == "file"
    assert "path" in result


def test_file_blobstore_serialization_includes_path():
    bs = _file_bs(path="/var/store")
    bs.created_at, bs.updated_at = _ts(), _ts()
    result = bs.to_dict()
    assert "path" in result
    assert result["path"] == "/var/store"


def test_s3_blobstore_serialization_includes_bucket():
    bs = _s3_bs(bucket_name="ser-bucket", prefix="pfx/")
    bs.created_at, bs.updated_at = _ts(), _ts()
    result = bs.to_dict()
    assert "bucket_name" in result and result["bucket_name"] == "ser-bucket"
    assert "prefix" in result and result["prefix"] == "pfx/"


def test_blobstore_serialization_excludes_secrets():
    bs = _s3_bs(secret_access_key="SUPER_SECRET_VALUE")
    bs.created_at, bs.updated_at = _ts(), _ts()
    result = bs.to_dict()
    assert "secret_access_key" not in result
    assert bs.secret_access_key == "SUPER_SECRET_VALUE"


def test_blobstore_repr_string():
    bs = _file_bs(name="repr-store")
    r = repr(bs)
    assert "repr-store" in r
    assert "file" in r


# =========================================================================
# Phase 8 — Edge Cases
# =========================================================================

def test_blobstore_with_empty_prefix():
    bs = _s3_bs(prefix="")
    assert bs.prefix == ""
    assert bs.type == "s3"


def test_blobstore_with_long_name(db_session):
    long_name = "x" * 255
    bs = _file_bs(name=long_name)
    db_session.add(bs)
    db_session.flush()
    assert bs.name == long_name
    assert len(bs.name) == 255


def test_blobstore_file_path_absolute_vs_relative():
    absolute = _file_bs(name="abs", path="/data/store")
    relative = _file_bs(name="rel", path="data/store")
    assert absolute.path.startswith("/")
    assert not relative.path.startswith("/")


def test_blobstore_s3_bucket_name_format():
    bs = _s3_bs(bucket_name="my-valid-bucket-123")
    assert bs.bucket_name == "my-valid-bucket-123"
    assert bs.bucket_name == bs.bucket_name.lower()


def test_blobstore_valid_types_constant():
    assert "file" in VALID_BLOBSTORE_TYPES
    assert "s3" in VALID_BLOBSTORE_TYPES
    assert len(VALID_BLOBSTORE_TYPES) == 2


def test_config_with_s3_storage_returns_s3_type():
    config = make_config_with_s3_storage()
    assert config["STORAGE_TYPE"] == "s3"
    assert "S3_BUCKET_NAME" in config


def test_config_with_file_storage_returns_file_type():
    config = make_config_with_file_storage()
    assert config["STORAGE_TYPE"] == "file"
    assert "STORAGE_PATH" in config


# =========================================================================
# Phase 9 — Error Cases
# =========================================================================

def test_blobstore_null_name_raises_error(db_session):
    bs = BlobStore(id=str(uuid.uuid4()), type="file")
    assert bs.name is None  # explicitly confirm name is absent
    db_session.add(bs)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_blobstore_null_type_raises_error():
    # The @validates('type') decorator raises ValueError before DB NOT NULL.
    with pytest.raises(ValueError, match="Invalid BlobStore type"):
        BlobStore(name="null-type-test", type=None)
    # Confirm valid types remain unchanged.
    assert len(VALID_BLOBSTORE_TYPES) == 2


def test_blobstore_duplicate_name_constraint(db_session):
    name = f"dup-{uuid.uuid4().hex[:8]}"
    bs1 = _file_bs(name=name)
    bs2 = _s3_bs(name=name)
    assert bs1.name == bs2.name  # same name, different types
    db_session.add(bs1)
    db_session.flush()
    db_session.add(bs2)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# =========================================================================
# Phase 10 — Database Persistence
# =========================================================================

def test_blobstore_persists_to_database(db_session):
    bs = _file_bs(name="persist-test", path="/data/persist")
    db_session.add(bs)
    db_session.flush()
    found = db_session.query(BlobStore).filter_by(name="persist-test").first()
    assert found is not None
    assert found.name == "persist-test"
    assert found.path == "/data/persist"


def test_blobstore_query_by_type(db_session):
    db_session.add_all([_file_bs(name="qf"), _s3_bs(name="qs")])
    db_session.flush()
    file_results = db_session.query(BlobStore).filter_by(type="file").all()
    s3_results = db_session.query(BlobStore).filter_by(type="s3").all()
    assert len(file_results) >= 1 and all(r.type == "file" for r in file_results)
    assert len(s3_results) >= 1 and all(r.type == "s3" for r in s3_results)


def test_blobstore_update_persists(db_session):
    bs = _file_bs(name="update-test", path="/old/path")
    db_session.add(bs)
    db_session.flush()
    bs.path = "/new/path"
    db_session.flush()
    found = db_session.query(BlobStore).filter_by(name="update-test").first()
    assert found is not None
    assert found.path == "/new/path"


def test_blobstore_delete_removes_from_db(db_session):
    bs = _file_bs(name="delete-test")
    db_session.add(bs)
    db_session.flush()
    bs_id = bs.id
    db_session.delete(bs)
    db_session.flush()
    assert db_session.query(BlobStore).filter_by(id=bs_id).first() is None
    assert db_session.query(BlobStore).filter_by(name="delete-test").first() is None


def test_blobstore_created_at_is_set(db_session):
    bs = _file_bs(name="ts-test")
    db_session.add(bs)
    db_session.flush()
    found = db_session.query(BlobStore).filter_by(name="ts-test").first()
    assert found is not None
    assert found.created_at is not None
    assert isinstance(found.created_at, datetime)


def test_blobstore_sample_fixture_works(sample_blobstore):
    assert sample_blobstore is not None
    assert sample_blobstore.name == "default"
    assert sample_blobstore.type == "file"


def test_blobstore_model_factory_creates_instance(model_factory):
    bs = model_factory(
        BlobStore, name=f"factory-{uuid.uuid4().hex[:8]}",
        type="s3", bucket_name="factory-bucket",
    )
    assert bs is not None
    assert bs.type == "s3"
    assert bs.bucket_name == "factory-bucket"
