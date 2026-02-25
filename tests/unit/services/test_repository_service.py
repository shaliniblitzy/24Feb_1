"""Unit tests for the Repository Management Service.

Tests real ``RepositoryService`` instances with controlled inputs, covering
CRUD operations, type validation (Hosted/Proxy/Group), and format handling
across all 7 formats (Maven, npm, Docker, NuGet, PyPI, APT, Raw).
Flat functional test style per AAP §0.10.1.

Features: F-101 Multi-Format Support (Critical), F-102 Repository Types (Critical)
Coverage target: ≥ 90 %  (AAP § 0.7.1)
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from src.services.repository_service import (
    RepositoryService,
    REPOSITORY_FORMATS,
    REPOSITORY_TYPES,
)
from tests.fixtures.repository_data import (
    make_hosted_repo, make_proxy_repo, make_group_repo,
    make_all_format_repos, make_all_type_repos,
    get_repo_format_ids, get_repo_type_ids,
    make_repo_with_duplicate_name, make_repo_with_max_name_length,
    make_repo_with_special_characters, make_repo_with_invalid_type,
    make_circular_group_reference,
)

pytestmark = pytest.mark.unit


def _svc(db_session=None):
    """Create a real RepositoryService with a mocked db session."""
    return RepositoryService(db_session=db_session or MagicMock())


# ===== Happy-Path CRUD ====================================================

def test_create_repository_hosted_success(mock_db_session):
    """Creating a hosted repository returns the config with an assigned ID."""
    svc = _svc(mock_db_session)
    cfg = make_hosted_repo()
    result = svc.create_repository(cfg)
    assert result is not None
    assert result["type"] == "hosted"
    assert result["format"] in REPOSITORY_FORMATS
    assert "id" in result


def test_create_repository_proxy_success(mock_db_session):
    """Creating a proxy repository stores the remote URL."""
    svc = _svc(mock_db_session)
    cfg = make_proxy_repo()
    result = svc.create_repository(cfg)
    assert result is not None
    assert result["type"] == "proxy"
    assert result["proxy"]["remote_url"] is not None


def test_create_repository_group_success(mock_db_session):
    """Creating a group repository stores group member list."""
    svc = _svc(mock_db_session)
    cfg = make_group_repo()
    result = svc.create_repository(cfg)
    assert result is not None
    assert result["type"] == "group"
    assert "group" in result


def test_get_repository_by_id_success(mock_db_session):
    """Get by ID returns the previously created repository."""
    svc = _svc(mock_db_session)
    cfg = make_hosted_repo()
    created = svc.create_repository(cfg)
    result = svc.get_repository(created["id"])
    assert result is not None
    assert result["id"] == created["id"]


def test_get_repository_by_name_success(mock_db_session):
    """Get by name returns the matching repository."""
    svc = _svc(mock_db_session)
    cfg = make_hosted_repo()
    created = svc.create_repository(cfg)
    result = svc.get_repository_by_name(created["name"])
    assert result is not None
    assert result["name"] == created["name"]


def test_list_repositories_success(mock_db_session):
    """List returns all created repositories."""
    svc = _svc(mock_db_session)
    for _ in range(3):
        svc.create_repository(make_hosted_repo())
    result = svc.list_repositories()
    assert isinstance(result, list)
    assert len(result) == 3


def test_update_repository_success(mock_db_session):
    """Update modifies the specified fields."""
    svc = _svc(mock_db_session)
    created = svc.create_repository(make_hosted_repo())
    result = svc.update_repository(created["id"], {"description": "Updated desc"})
    assert result is not None
    assert result["description"] == "Updated desc"


def test_delete_repository_success(mock_db_session):
    """Delete removes the repository and returns True."""
    svc = _svc(mock_db_session)
    created = svc.create_repository(make_hosted_repo())
    result = svc.delete_repository(created["id"])
    assert result is True
    assert svc.get_repository(created["id"]) is None


# ===== Parametrized Multi-Format (F-101) ==================================

@pytest.mark.parametrize("format_type", list(REPOSITORY_FORMATS),
                         ids=list(REPOSITORY_FORMATS))
def test_create_hosted_all_formats(mock_db_session, format_type):
    """Hosted repository created for each of 7 formats."""
    svc = _svc(mock_db_session)
    cfg = make_hosted_repo(format_type=format_type)
    result = svc.create_repository(cfg)
    assert result["format"] == format_type
    assert result["type"] == "hosted"
    assert result["name"] is not None


@pytest.mark.parametrize("format_type", list(REPOSITORY_FORMATS))
def test_create_proxy_all_formats(mock_db_session, format_type):
    """Proxy repository created for each format with valid remote URL."""
    svc = _svc(mock_db_session)
    cfg = make_proxy_repo(format_type=format_type)
    result = svc.create_repository(cfg)
    assert result["format"] == format_type
    assert result["proxy"]["remote_url"] is not None


@pytest.mark.parametrize("repo_type", list(REPOSITORY_TYPES),
                         ids=list(REPOSITORY_TYPES))
def test_create_repository_all_types(mock_db_session, repo_type):
    """Repository created for each type (hosted, proxy, group)."""
    svc = _svc(mock_db_session)
    factories = {"hosted": make_hosted_repo, "proxy": make_proxy_repo,
                 "group": make_group_repo}
    cfg = factories[repo_type]()
    result = svc.create_repository(cfg)
    assert result["type"] == repo_type
    assert result["format"] in REPOSITORY_FORMATS


@pytest.mark.parametrize("format_type", list(REPOSITORY_FORMATS),
                         ids=list(REPOSITORY_FORMATS))
def test_format_specific_settings_preserved(mock_db_session, format_type):
    """Format-specific storage settings are preserved after creation."""
    svc = _svc(mock_db_session)
    cfg = make_hosted_repo(format_type=format_type)
    result = svc.create_repository(cfg)
    assert result["format"] == format_type
    assert "storage" in result


# ===== Type-Specific Behaviour ============================================

def test_hosted_storage_config(mock_db_session):
    """Hosted repository has default blob_store_name and write_policy."""
    svc = _svc(mock_db_session)
    hosted = make_hosted_repo()
    result = svc.create_repository(hosted)
    assert result["storage"]["blob_store_name"] == "default"
    assert result["storage"]["write_policy"] == "ALLOW_ONCE"


def test_proxy_remote_url_validation(mock_db_session):
    """Proxy remote URL starts with https://."""
    svc = _svc(mock_db_session)
    proxy = make_proxy_repo(format_type="npm")
    result = svc.create_repository(proxy)
    assert result["proxy"]["remote_url"].startswith("https://")
    assert result["type"] == "proxy"


def test_proxy_caching_config(mock_db_session):
    """Proxy repository stores content and metadata max-age settings."""
    svc = _svc(mock_db_session)
    proxy = make_proxy_repo()
    result = svc.create_repository(proxy)
    assert result["proxy"]["content_max_age"] == 1440
    assert result["proxy"]["metadata_max_age"] == 1440


def test_group_member_management(mock_db_session):
    """Group repository stores ordered member list."""
    svc = _svc(mock_db_session)
    group = make_group_repo(member_names=["hosted-1", "proxy-1"])
    result = svc.create_repository(group)
    assert "hosted-1" in result["group"]["member_names"]
    assert len(result["group"]["member_names"]) == 2


def test_group_member_resolution_order(mock_db_session):
    """Group member list preserves insertion order."""
    svc = _svc(mock_db_session)
    group = make_group_repo(member_names=["primary", "secondary"])
    result = svc.create_repository(group)
    assert result["group"]["member_names"][0] == "primary"
    assert len(result["group"]["member_names"]) >= 1


# ===== Edge Cases =========================================================

def test_duplicate_name_returns_conflict(mock_db_session):
    """Creating two repos with the same name raises ValueError."""
    svc = _svc(mock_db_session)
    repo_a, repo_b = make_repo_with_duplicate_name()
    svc.create_repository(repo_a)
    with pytest.raises(ValueError, match="duplicate"):
        svc.create_repository(repo_b)
    assert repo_a["name"] == repo_b["name"]


def test_max_name_length_accepted(mock_db_session):
    """Repository with 255-character name is accepted."""
    svc = _svc(mock_db_session)
    long_repo = make_repo_with_max_name_length(255)
    result = svc.create_repository(long_repo)
    assert result is not None
    assert len(result["name"]) == 255


def test_special_characters_in_name(mock_db_session):
    """Repository with special characters in name is accepted."""
    svc = _svc(mock_db_session)
    special = make_repo_with_special_characters()
    result = svc.create_repository(special)
    assert result is not None
    assert len(result["name"]) > 0


def test_circular_group_reference_raises(mock_db_session):
    """Circular group reference detected and raises ValueError."""
    svc = _svc(mock_db_session)
    group_a, group_b = make_circular_group_reference()
    svc.create_repository(group_a)
    with pytest.raises(ValueError, match="circular"):
        svc.create_repository(group_b)
    assert group_a["group"]["member_names"] == ["circular-group-b"]


def test_empty_name_fails(mock_db_session):
    """Empty repository name raises ValueError."""
    svc = _svc(mock_db_session)
    cfg = make_hosted_repo()
    cfg["name"] = ""
    with pytest.raises(ValueError, match="name required"):
        svc.create_repository(cfg)
    assert cfg["name"] == ""


def test_list_repositories_empty_collection(mock_db_session):
    """Listing with no repos returns an empty list."""
    svc = _svc(mock_db_session)
    result = svc.list_repositories()
    assert result == []
    assert len(result) == 0


# ===== Error Cases ========================================================

def test_get_nonexistent_id_returns_none(mock_db_session):
    """Querying non-existent ID returns None."""
    svc = _svc(mock_db_session)
    result = svc.get_repository("nonexistent-id")
    assert result is None
    assert not result


def test_invalid_type_raises_error(mock_db_session):
    """Invalid repository type raises ValueError."""
    svc = _svc(mock_db_session)
    bad = make_repo_with_invalid_type()
    with pytest.raises(ValueError, match="invalid type"):
        svc.create_repository(bad)
    assert bad["type"] == "invalid_type_xyz"


def test_invalid_format_raises_error(mock_db_session):
    """Unsupported format raises ValueError."""
    svc = _svc(mock_db_session)
    cfg = make_hosted_repo()
    cfg["format"] = "unsupported_format"
    with pytest.raises(ValueError, match="unsupported format"):
        svc.create_repository(cfg)
    assert cfg["format"] == "unsupported_format"


def test_update_nonexistent_raises_error(mock_db_session):
    """Updating a non-existent repository raises LookupError."""
    svc = _svc(mock_db_session)
    with pytest.raises(LookupError, match="not found") as exc_info:
        svc.update_repository("ghost-id", {"online": False})
    assert "not found" in str(exc_info.value)


def test_delete_nonexistent_raises_error(mock_db_session):
    """Deleting a non-existent repository raises LookupError."""
    svc = _svc(mock_db_session)
    with pytest.raises(LookupError, match="not found") as exc_info:
        svc.delete_repository("ghost-id")
    assert "not found" in str(exc_info.value)


def test_proxy_without_remote_url_raises(mock_db_session):
    """Proxy repo without remote_url raises ValueError."""
    svc = _svc(mock_db_session)
    cfg = make_proxy_repo()
    cfg["proxy"].pop("remote_url", None)
    with pytest.raises(ValueError, match="missing remote_url"):
        svc.create_repository(cfg)
    assert "remote_url" not in cfg.get("proxy", {})


# ===== Lifecycle State ====================================================

def test_online_offline_toggle(mock_db_session):
    """Toggling online flag persists correctly."""
    svc = _svc(mock_db_session)
    created = svc.create_repository(make_hosted_repo())
    result = svc.update_repository(created["id"], {"online": False})
    assert result["online"] is False
    assert result is not None


def test_full_lifecycle_create_update_toggle_delete(mock_db_session):
    """Full lifecycle: Create → Update → Offline → Online → Delete."""
    svc = _svc(mock_db_session)
    cfg = make_hosted_repo()
    created = svc.create_repository(cfg)
    assert created is not None

    updated = svc.update_repository(created["id"], {"description": "new"})
    assert updated["description"] == "new"

    toggled = svc.update_repository(created["id"], {"online": False})
    assert toggled["online"] is False

    restored = svc.update_repository(created["id"], {"online": True})
    assert restored["online"] is True

    assert svc.delete_repository(created["id"]) is True


# ===== Factory Data Sanity ================================================

def test_all_format_repos_returns_seven():
    """make_all_format_repos returns exactly 7 format repos."""
    repos = make_all_format_repos(repo_type="hosted")
    assert len(repos) == 7
    assert {r["format"] for r in repos} == set(REPOSITORY_FORMATS)


def test_all_type_repos_returns_three():
    """make_all_type_repos returns exactly 3 type repos."""
    repos = make_all_type_repos(format_type="maven")
    assert len(repos) == 3
    assert {r["type"] for r in repos} == set(REPOSITORY_TYPES)


def test_format_ids_match_formats():
    """get_repo_format_ids returns IDs matching all formats."""
    ids = get_repo_format_ids()
    assert len(ids) == len(REPOSITORY_FORMATS)
    assert set(ids) == set(REPOSITORY_FORMATS)


def test_type_ids_match_types():
    """get_repo_type_ids returns IDs matching all types."""
    ids = get_repo_type_ids()
    assert len(ids) == len(REPOSITORY_TYPES)
    assert set(ids) == set(REPOSITORY_TYPES)
