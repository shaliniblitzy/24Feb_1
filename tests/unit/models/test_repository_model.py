"""
Unit tests for the Repository data model.

Validates model instantiation, field constraints, relationships, serialization,
and database persistence for all 3 repository types (Hosted, Proxy, Group) and
all 7 formats (Maven, npm, Docker, NuGet, PyPI, APT, Raw).

Uses in-memory SQLite via ``db_session`` fixture with per-test cleanup.
"""

import pytest
from datetime import datetime, timezone
from sqlalchemy.exc import IntegrityError

from src.models.repository import Repository, VALID_REPOSITORY_FORMATS, VALID_REPOSITORY_TYPES
from src.models.asset import Asset
from tests.fixtures.repository_data import (
    make_hosted_repo,
    make_proxy_repo,
    make_group_repo,
    make_all_format_repos,
    make_all_type_repos,
    make_repo_with_duplicate_name,
    make_repo_with_max_name_length,
    make_repo_with_special_characters,
    make_repo_with_invalid_type,
    get_repo_format_ids,
    get_repo_type_ids,
    REPOSITORY_FORMATS,
    REPOSITORY_TYPES,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers — create a Repository instance from fixture dict
# ---------------------------------------------------------------------------

def _repo_from_dict(data: dict) -> Repository:
    """Build a ``Repository`` instance using only columns the ORM knows about."""
    return Repository(
        name=data["name"],
        format=data["format"],
        type=data["type"],
        online=data.get("online", True),
        description=data.get("description"),
        url=data.get("proxy", {}).get("remote_url") if data.get("type") == "proxy" else None,
    )


# ===========================================================================
# Phase 2 — Happy-path model instantiation
# ===========================================================================


def test_repository_model_instantiation_with_required_fields():
    """Model accepts name, format and type and stores them correctly."""
    repo = Repository(name="test-repo", format="maven", type="hosted")
    assert repo is not None
    assert repo.name == "test-repo"
    assert repo.format == "maven"
    assert repo.type == "hosted"


def test_repository_model_default_values(db_session):
    """Column defaults (online, created_at) are applied after flush."""
    repo = Repository(name="defaults-repo", format="npm", type="hosted")
    db_session.add(repo)
    db_session.flush()
    assert repo.online is True
    assert repo.description is None
    assert repo.created_at is not None


def test_hosted_repository_creation():
    """Hosted repository stores correct type and format."""
    data = make_hosted_repo(format_type="maven")
    repo = _repo_from_dict(data)
    assert repo.type == "hosted"
    assert repo.format == "maven"
    assert repo.name is not None


def test_proxy_repository_creation():
    """Proxy repository stores remote URL."""
    data = make_proxy_repo(format_type="npm")
    repo = _repo_from_dict(data)
    assert repo.type == "proxy"
    assert repo.url is not None
    assert repo.url.startswith("http")


def test_group_repository_creation():
    """Group repository stores correct type; url may be absent."""
    data = make_group_repo(format_type="docker")
    repo = _repo_from_dict(data)
    assert repo.type == "group"
    assert repo.format == "docker"


# ===========================================================================
# Phase 3 — Parametrised format and type tests
# ===========================================================================


@pytest.mark.parametrize("fmt", REPOSITORY_FORMATS, ids=get_repo_format_ids())
def test_repository_creation_all_formats(fmt):
    """Each of the 7 supported formats is accepted by the model."""
    repo = Repository(name=f"repo-{fmt}", format=fmt, type="hosted")
    assert repo.format == fmt
    assert repo.name == f"repo-{fmt}"


@pytest.mark.parametrize("rtype", REPOSITORY_TYPES, ids=get_repo_type_ids())
def test_repository_creation_all_types(rtype):
    """Each of the 3 supported types is accepted by the model."""
    repo = Repository(name=f"repo-{rtype}", format="maven", type=rtype)
    assert repo.type == rtype
    assert repo.format == "maven"


@pytest.mark.parametrize("fmt", REPOSITORY_FORMATS)
@pytest.mark.parametrize("rtype", REPOSITORY_TYPES)
def test_repository_format_type_combinations(fmt, rtype):
    """All 21 format × type combinations produce valid model instances."""
    repo = Repository(name=f"{fmt}-{rtype}", format=fmt, type=rtype)
    assert repo.format == fmt
    assert repo.type == rtype


# ===========================================================================
# Phase 4 — Field validation / constraints
# ===========================================================================


def test_repository_name_field_required(db_session):
    """NOT NULL constraint on name raises IntegrityError."""
    repo = Repository(name=None, format="maven", type="hosted")
    assert repo.name is None
    db_session.add(repo)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_repository_format_field_required(db_session):
    """NOT NULL constraint on format raises IntegrityError."""
    repo = Repository(name="no-format", format=None, type="hosted")
    assert repo.format is None
    db_session.add(repo)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_repository_type_field_required(db_session):
    """NOT NULL constraint on type raises IntegrityError."""
    repo = Repository(name="no-type", format="maven", type=None)
    assert repo.type is None
    db_session.add(repo)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_repository_name_uniqueness(db_session):
    """UNIQUE constraint on name prevents duplicate names."""
    dup_a, dup_b = make_repo_with_duplicate_name()
    repo_a = _repo_from_dict(dup_a)
    repo_b = _repo_from_dict(dup_b)
    assert repo_a.name == repo_b.name
    db_session.add(repo_a)
    db_session.flush()
    db_session.add(repo_b)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_repository_name_max_length(db_session):
    """Repository with 255-char name is accepted by the schema."""
    data = make_repo_with_max_name_length(length=255)
    repo = _repo_from_dict(data)
    assert len(repo.name) == 255
    db_session.add(repo)
    db_session.flush()
    assert repo.id is not None


def test_repository_name_special_characters(db_session):
    """Unicode and special characters in name are stored and retrieved."""
    data = make_repo_with_special_characters()
    repo = _repo_from_dict(data)
    db_session.add(repo)
    db_session.flush()
    fetched = db_session.get(Repository, repo.id)
    assert fetched.name == data["name"]
    assert "ünîcödé" in fetched.name


def test_repository_online_field_is_boolean():
    """Online field stores and returns booleans for True and False."""
    repo_on = Repository(name="on-repo", format="raw", type="hosted", online=True)
    repo_off = Repository(name="off-repo", format="raw", type="hosted", online=False)
    assert repo_on.online is True
    assert repo_off.online is False


# ===========================================================================
# Phase 5 — Relationship tests
# ===========================================================================


def test_repository_has_assets_relationship(db_session):
    """Repository.assets dynamic relationship is navigable."""
    repo = Repository(name="rel-repo", format="maven", type="hosted")
    db_session.add(repo)
    db_session.flush()
    asset = Asset(
        name="artifact.jar",
        path="/com/example/artifact.jar",
        repository_id=repo.id,
    )
    db_session.add(asset)
    db_session.flush()
    assert repo.assets.count() >= 1
    assert repo.assets.first().name == "artifact.jar"


def test_repository_cascade_deletes_assets(db_session):
    """Deleting a repository cascades to its assets."""
    repo = Repository(name="cascade-repo", format="npm", type="hosted")
    db_session.add(repo)
    db_session.flush()
    asset = Asset(
        name="pkg.tgz",
        path="/pkg/pkg.tgz",
        repository_id=repo.id,
    )
    db_session.add(asset)
    db_session.flush()
    asset_id = asset.id
    db_session.delete(repo)
    db_session.flush()
    assert db_session.get(Asset, asset_id) is None
    assert db_session.get(Repository, repo.id) is None


# ===========================================================================
# Phase 6 — Serialisation
# ===========================================================================


def test_repository_to_dict_serialization():
    """to_dict() returns dict with all expected key-value pairs."""
    repo = Repository(
        name="ser-repo",
        format="pypi",
        type="hosted",
        online=True,
        description="A PyPI repo",
        url=None,
    )
    d = repo.to_dict()
    assert isinstance(d, dict)
    assert d["name"] == "ser-repo"
    assert d["format"] == "pypi"
    assert d["type"] == "hosted"
    assert d["online"] is True
    assert d["description"] == "A PyPI repo"


def test_repository_to_dict_contains_required_fields():
    """Serialised dict contains the full set of required keys."""
    repo = Repository(name="keys-repo", format="docker", type="proxy")
    d = repo.to_dict()
    required = {"id", "name", "format", "type", "online", "description",
                "url", "created_at", "updated_at"}
    assert required.issubset(d.keys())
    assert d["name"] == "keys-repo"


def test_repository_repr_string():
    """__repr__ includes name, format, and type."""
    repo = Repository(name="repr-repo", format="apt", type="group")
    r = repr(repo)
    assert "repr-repo" in r
    assert "apt" in r
    assert "group" in r


# ===========================================================================
# Phase 7 — Edge cases
# ===========================================================================


def test_repository_with_empty_description():
    """Model accepts an empty string for description."""
    repo = Repository(name="empty-desc", format="raw", type="hosted", description="")
    assert repo.description == ""
    assert repo.name == "empty-desc"


def test_repository_with_none_optional_fields():
    """Optional fields (description, url) accept None."""
    repo = Repository(
        name="none-opts",
        format="maven",
        type="hosted",
        description=None,
        url=None,
    )
    assert repo.description is None
    assert repo.url is None


def test_repository_format_enum_boundaries():
    """All 7 known formats are present in VALID_REPOSITORY_FORMATS."""
    expected = {"maven", "npm", "docker", "nuget", "pypi", "apt", "raw"}
    assert VALID_REPOSITORY_FORMATS == expected
    assert len(VALID_REPOSITORY_FORMATS) == 7


def test_repository_type_enum_boundaries():
    """All 3 known types are present in VALID_REPOSITORY_TYPES."""
    expected = {"hosted", "proxy", "group"}
    assert VALID_REPOSITORY_TYPES == expected
    assert len(VALID_REPOSITORY_TYPES) == 3


def test_repository_online_toggle():
    """Toggling online from True to False is reflected immediately."""
    repo = Repository(name="toggle", format="nuget", type="hosted", online=True)
    assert repo.online is True
    repo.online = False
    assert repo.online is False


# ===========================================================================
# Phase 8 — Error cases
# ===========================================================================


def test_repository_with_invalid_format_not_in_valid_set():
    """An invalid format string is NOT in VALID_REPOSITORY_FORMATS."""
    data = {"name": "bad-fmt", "format": "invalid_format", "type": "hosted"}
    repo = Repository(**data)
    assert repo.format not in VALID_REPOSITORY_FORMATS
    assert repo.format == "invalid_format"


def test_repository_with_invalid_type_not_in_valid_set():
    """make_repo_with_invalid_type produces a type outside the valid set."""
    inv = make_repo_with_invalid_type()
    repo = _repo_from_dict(inv)
    assert repo.type not in VALID_REPOSITORY_TYPES
    assert repo.type == "invalid_type_xyz"


def test_repository_duplicate_name_constraint_violation(db_session):
    """Committing two repos with the same name raises IntegrityError."""
    r1 = Repository(name="dup-name", format="maven", type="hosted")
    r2 = Repository(name="dup-name", format="npm", type="proxy")
    assert r1.name == r2.name
    db_session.add(r1)
    db_session.flush()
    db_session.add(r2)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_repository_null_name_raises_error(db_session):
    """Flushing a repo with name=None raises IntegrityError."""
    repo = Repository(name=None, format="docker", type="hosted")
    assert repo.format == "docker"
    db_session.add(repo)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# ===========================================================================
# Phase 9 — Database persistence
# ===========================================================================


def test_repository_persists_to_database(db_session):
    """Committed repo is queryable and has a generated id."""
    repo = Repository(name="persist-repo", format="maven", type="hosted")
    db_session.add(repo)
    db_session.flush()
    fetched = db_session.get(Repository, repo.id)
    assert fetched is not None
    assert fetched.name == "persist-repo"
    assert fetched.id is not None


def test_repository_query_by_format(db_session):
    """Filtering by format returns only matching rows."""
    for fmt in ("maven", "npm", "docker"):
        db_session.add(Repository(name=f"qf-{fmt}", format=fmt, type="hosted"))
    db_session.flush()
    maven_repos = db_session.query(Repository).filter_by(format="maven").all()
    assert len(maven_repos) >= 1
    assert all(r.format == "maven" for r in maven_repos)


def test_repository_query_by_type(db_session):
    """Filtering by type returns only matching rows."""
    db_session.add(Repository(name="qt-hosted", format="raw", type="hosted"))
    db_session.add(Repository(name="qt-proxy", format="raw", type="proxy"))
    db_session.flush()
    hosted = db_session.query(Repository).filter_by(type="hosted").all()
    assert len(hosted) >= 1
    assert all(r.type == "hosted" for r in hosted)


def test_repository_update_persists(db_session):
    """Field update is reflected after flush."""
    repo = Repository(name="update-me", format="pypi", type="hosted", description="old")
    db_session.add(repo)
    db_session.flush()
    repo.description = "new"
    db_session.flush()
    fetched = db_session.get(Repository, repo.id)
    assert fetched.description == "new"
    assert fetched.name == "update-me"


def test_repository_delete_removes_from_db(db_session):
    """Deleted repo no longer appears in queries."""
    repo = Repository(name="delete-me", format="apt", type="group")
    db_session.add(repo)
    db_session.flush()
    rid = repo.id
    db_session.delete(repo)
    db_session.flush()
    assert db_session.get(Repository, rid) is None
    assert db_session.query(Repository).filter_by(name="delete-me").first() is None
