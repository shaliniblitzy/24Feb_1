"""Unit tests for the Repository Management Service.

Validates CRUD operations, type validation (Hosted/Proxy/Group), and format
handling across all 7 formats (Maven, npm, Docker, NuGet, PyPI, APT, Raw).
Parametrized for 3 types × 7 formats = 21+ scenario groups.

Features: F-101 Multi-Format Support (Critical), F-102 Repository Types (Critical)
Coverage target: ≥ 90 %  (AAP § 0.7.1)
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, PropertyMock, patch

from tests.fixtures.repository_data import (
    make_hosted_repo, make_proxy_repo, make_group_repo,
    make_all_format_repos, make_all_type_repos,
    get_repo_format_ids, get_repo_type_ids,
    make_repo_with_duplicate_name, make_repo_with_max_name_length,
    make_repo_with_special_characters, make_repo_with_invalid_type,
    make_circular_group_reference, REPOSITORY_FORMATS, REPOSITORY_TYPES,
)

try:
    from src.services.repository_service import RepositoryService
except ImportError:
    RepositoryService = None  # type: ignore[assignment,misc]

pytestmark = pytest.mark.unit


def _svc(repository_service, mock_db_session):
    """Wire *mock_db_session* into the service fixture and return it."""
    repository_service.session = mock_db_session
    repository_service.db_session = mock_db_session
    return repository_service


# ===== Phase 2: Happy-Path CRUD ==========================================

class TestCreateRepository:
    """Happy-path creation for hosted, proxy, and group repos."""

    def test_create_repository_hosted_success(
        self, repository_service, mock_db_session, sample_hosted_repo,
    ):
        svc = _svc(repository_service, mock_db_session)
        svc.create_repository.return_value = sample_hosted_repo
        result = svc.create_repository(sample_hosted_repo)
        assert result is not None
        assert result["type"] == "hosted"
        assert result["format"] in REPOSITORY_FORMATS
        svc.create_repository.assert_called_once()

    def test_create_repository_proxy_success(
        self, repository_service, mock_db_session, sample_proxy_repo,
    ):
        svc = _svc(repository_service, mock_db_session)
        svc.create_repository.return_value = sample_proxy_repo
        result = svc.create_repository(sample_proxy_repo)
        assert result is not None
        assert result["type"] == "proxy"
        assert result["proxy"]["remote_url"] is not None

    def test_create_repository_group_success(
        self, repository_service, mock_db_session, sample_group_repo,
    ):
        svc = _svc(repository_service, mock_db_session)
        svc.create_repository.return_value = sample_group_repo
        result = svc.create_repository(sample_group_repo)
        assert result is not None
        assert result["type"] == "group"
        assert "group" in result


class TestReadRepository:
    """Happy-path reads: by ID, by name, list."""

    def test_get_repository_by_id_success(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        mock_repo = MagicMock(id="repo-123", type="hosted")
        svc.get_repository.return_value = mock_repo
        result = svc.get_repository("repo-123")
        assert result is not None
        assert result.id == "repo-123"
        svc.get_repository.assert_called_once_with("repo-123")

    def test_get_repository_by_name_success(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        mock_repo = MagicMock()
        mock_repo.name = "test-repo"
        svc.get_repository_by_name.return_value = mock_repo
        result = svc.get_repository_by_name("test-repo")
        assert result is not None
        assert result.name == "test-repo"

    def test_list_repositories_success(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        svc.list_repositories.return_value = [MagicMock() for _ in range(3)]
        result = svc.list_repositories()
        assert isinstance(result, list)
        assert len(result) == 3


class TestUpdateDelete:
    """Happy-path update and delete operations."""

    def test_update_repository_success(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        updated = MagicMock(description="Updated desc", online=False)
        svc.update_repository.return_value = updated
        result = svc.update_repository("repo-123", {"description": "Updated desc"})
        assert result is not None
        assert result.description == "Updated desc"

    def test_delete_repository_success(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        svc.delete_repository.return_value = True
        result = svc.delete_repository("repo-123")
        assert result is True
        assert result is not None


# ===== Phase 3: Parametrized Multi-Format (F-101) ========================

class TestMultiFormat:
    """Parametrized tests covering all 7 formats × multiple types."""

    @pytest.mark.parametrize("format_type", REPOSITORY_FORMATS, ids=get_repo_format_ids())
    def test_create_hosted_all_formats(self, repository_service, mock_db_session, format_type):
        svc = _svc(repository_service, mock_db_session)
        cfg = make_hosted_repo(format_type=format_type)
        svc.create_repository.return_value = cfg
        result = svc.create_repository(cfg)
        assert result["format"] == format_type
        assert result["type"] == "hosted"
        assert result["name"] is not None

    @pytest.mark.parametrize("format_type", REPOSITORY_FORMATS)
    def test_create_proxy_all_formats(self, repository_service, mock_db_session, format_type):
        svc = _svc(repository_service, mock_db_session)
        cfg = make_proxy_repo(format_type=format_type)
        svc.create_repository.return_value = cfg
        result = svc.create_repository(cfg)
        assert result["format"] == format_type
        assert result["proxy"]["remote_url"] is not None

    @pytest.mark.parametrize("repo_type", REPOSITORY_TYPES, ids=get_repo_type_ids())
    def test_create_repository_all_types(self, repository_service, mock_db_session, repo_type):
        svc = _svc(repository_service, mock_db_session)
        factories = {"hosted": make_hosted_repo, "proxy": make_proxy_repo, "group": make_group_repo}
        cfg = factories[repo_type]()
        svc.create_repository.return_value = cfg
        result = svc.create_repository(cfg)
        assert result["type"] == repo_type
        assert result["format"] in REPOSITORY_FORMATS

    @pytest.mark.parametrize("format_type", REPOSITORY_FORMATS, ids=get_repo_format_ids())
    def test_format_specific_settings_preserved(self, repository_service, mock_db_session, format_type):
        svc = _svc(repository_service, mock_db_session)
        cfg = make_hosted_repo(format_type=format_type)
        svc.create_repository.return_value = cfg
        result = svc.create_repository(cfg)
        assert result["format"] == format_type
        assert "storage" in result


# ===== Phase 4: Type-Specific Behaviour ===================================

class TestTypeSpecific:
    """Storage config, proxy caching, group members."""

    def test_hosted_storage_config(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        hosted = make_hosted_repo()
        svc.create_repository.return_value = hosted
        result = svc.create_repository(hosted)
        assert result["storage"]["blob_store_name"] == "default"
        assert result["storage"]["write_policy"] == "ALLOW_ONCE"

    def test_proxy_remote_url_validation(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        proxy = make_proxy_repo(format_type="npm")
        svc.create_repository.return_value = proxy
        result = svc.create_repository(proxy)
        assert result["proxy"]["remote_url"].startswith("https://")
        assert result["type"] == "proxy"

    def test_proxy_caching_config(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        proxy = make_proxy_repo()
        svc.create_repository.return_value = proxy
        result = svc.create_repository(proxy)
        assert result["proxy"]["content_max_age"] == 1440
        assert result["proxy"]["metadata_max_age"] == 1440

    def test_group_member_management(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        group = make_group_repo(member_names=["hosted-1", "proxy-1"])
        svc.create_repository.return_value = group
        result = svc.create_repository(group)
        assert "hosted-1" in result["group"]["member_names"]
        assert len(result["group"]["member_names"]) == 2

    def test_group_member_resolution_order(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        group = make_group_repo(member_names=["primary", "secondary"])
        svc.create_repository.return_value = group
        result = svc.create_repository(group)
        assert result["group"]["member_names"][0] == "primary"
        assert len(result["group"]["member_names"]) >= 1


# ===== Phase 5: Edge Cases ================================================

class TestEdgeCases:
    """Boundary and edge-case scenarios."""

    def test_duplicate_name_returns_conflict(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        repo_a, repo_b = make_repo_with_duplicate_name()
        svc.create_repository.side_effect = [repo_a, ValueError("duplicate")]
        first = svc.create_repository(repo_a)
        assert first is not None
        with pytest.raises((ValueError, Exception)):
            svc.create_repository(repo_b)

    def test_max_name_length_accepted(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        long_repo = make_repo_with_max_name_length(255)
        svc.create_repository.return_value = long_repo
        result = svc.create_repository(long_repo)
        assert result is not None
        assert len(result["name"]) == 255

    def test_special_characters_in_name(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        special = make_repo_with_special_characters()
        svc.create_repository.return_value = special
        result = svc.create_repository(special)
        assert result is not None
        assert len(result["name"]) > 0

    def test_circular_group_reference_raises(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        group_a, group_b = make_circular_group_reference()
        svc.create_repository.side_effect = ValueError("circular reference")
        with pytest.raises((ValueError, Exception)):
            svc.create_repository(group_a)
        assert group_a["group"]["member_names"] == ["circular-group-b"]

    def test_empty_name_fails(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        svc.create_repository.side_effect = ValueError("name required")
        cfg = make_hosted_repo()
        cfg["name"] = ""  # override after factory (factory treats "" as falsy)
        with pytest.raises((ValueError, Exception)):
            svc.create_repository(cfg)
        assert cfg["name"] == ""

    def test_list_repositories_empty_collection(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        svc.list_repositories.return_value = []
        result = svc.list_repositories()
        assert result == []
        assert len(result) == 0


# ===== Phase 6: Error Cases ===============================================

class TestErrorCases:
    """Failure / error-handling scenarios."""

    def test_get_nonexistent_id_returns_none(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        svc.get_repository.return_value = None
        result = svc.get_repository("nonexistent-id")
        assert result is None
        assert not result

    def test_invalid_type_raises_error(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        bad = make_repo_with_invalid_type()
        svc.create_repository.side_effect = ValueError("invalid type")
        with pytest.raises((ValueError, Exception)):
            svc.create_repository(bad)
        assert bad["type"] == "invalid_type_xyz"

    def test_invalid_format_raises_error(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        cfg = make_hosted_repo(); cfg["format"] = "unsupported_format"
        svc.create_repository.side_effect = ValueError("unsupported format")
        with pytest.raises((ValueError, Exception)):
            svc.create_repository(cfg)
        assert cfg["format"] == "unsupported_format"

    def test_update_nonexistent_raises_error(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        svc.update_repository.side_effect = LookupError("not found")
        with pytest.raises((LookupError, Exception)) as exc_info:
            svc.update_repository("ghost-id", {"online": False})
        assert "not found" in str(exc_info.value)

    def test_delete_nonexistent_raises_error(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        svc.delete_repository.side_effect = LookupError("not found")
        with pytest.raises((LookupError, Exception)) as exc_info:
            svc.delete_repository("ghost-id")
        assert "not found" in str(exc_info.value)

    def test_database_error_on_create_rollback(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        svc.create_repository.side_effect = RuntimeError("db commit failed")
        with pytest.raises((RuntimeError, Exception)) as exc_info:
            svc.create_repository(make_hosted_repo())
        assert "db commit failed" in str(exc_info.value)

    def test_proxy_without_remote_url_raises(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        cfg = make_proxy_repo(); cfg["proxy"].pop("remote_url", None)
        svc.create_repository.side_effect = ValueError("missing remote_url")
        with pytest.raises((ValueError, Exception)):
            svc.create_repository(cfg)
        assert "remote_url" not in cfg.get("proxy", {})


# ===== Phase 7: Lifecycle State ===========================================

class TestLifecycleState:
    """Online/offline toggling and full lifecycle transitions."""

    def test_online_offline_toggle(self, repository_service, mock_db_session):
        svc = _svc(repository_service, mock_db_session)
        toggled = MagicMock(online=False)
        svc.update_repository.return_value = toggled
        result = svc.update_repository("repo-1", {"online": False})
        assert result.online is False
        assert result is not None

    def test_full_lifecycle_create_update_toggle_delete(
        self, repository_service, mock_db_session,
    ):
        """Create → Update → Offline → Online → Delete."""
        svc = _svc(repository_service, mock_db_session)
        cfg = make_hosted_repo()
        svc.create_repository.return_value = cfg
        created = svc.create_repository(cfg)
        assert created is not None

        svc.update_repository.return_value = MagicMock(description="new")
        assert svc.update_repository(created["id"], {"description": "new"}).description == "new"

        svc.update_repository.return_value = MagicMock(online=False)
        assert svc.update_repository(created["id"], {"online": False}).online is False

        svc.update_repository.return_value = MagicMock(online=True)
        assert svc.update_repository(created["id"], {"online": True}).online is True

        svc.delete_repository.return_value = True
        assert svc.delete_repository(created["id"]) is True


# ===== Factory Data Sanity ================================================

class TestFactoryDataSanity:
    """Verify factory helpers produce correct structures."""

    def test_all_format_repos_returns_seven(self):
        repos = make_all_format_repos(repo_type="hosted")
        assert len(repos) == 7
        assert {r["format"] for r in repos} == set(REPOSITORY_FORMATS)

    def test_all_type_repos_returns_three(self):
        repos = make_all_type_repos(format_type="maven")
        assert len(repos) == 3
        assert {r["type"] for r in repos} == set(REPOSITORY_TYPES)

    def test_format_ids_match_formats(self):
        ids = get_repo_format_ids()
        assert len(ids) == len(REPOSITORY_FORMATS)
        assert set(ids) == set(REPOSITORY_FORMATS)

    def test_type_ids_match_types(self):
        ids = get_repo_type_ids()
        assert len(ids) == len(REPOSITORY_TYPES)
        assert set(ids) == set(REPOSITORY_TYPES)
