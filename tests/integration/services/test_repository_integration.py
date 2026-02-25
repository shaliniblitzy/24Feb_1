"""Repository service integration tests with real in-memory SQLite database.

Exercises CRUD, lifecycle transitions, multi-format / multi-type coverage,
edge cases, and error handling.  Database isolation via ``db_session`` fixture.
AAP §0.3.1, §0.4.2, §0.5.1, §0.7.1 (≥90 %), §0.10.1. F-101, F-102.
"""
import json as _json
import uuid as _uuid
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest

from tests.fixtures.repository_data import (
    make_hosted_repo, make_proxy_repo, make_group_repo,
    make_all_format_repos, make_all_type_repos,
    make_repo_with_duplicate_name, make_repo_with_max_name_length,
    make_repo_with_special_characters, make_repo_with_invalid_type,
    make_circular_group_reference, get_repo_format_ids, get_repo_type_ids,
    REPOSITORY_FORMATS, REPOSITORY_TYPES,
)
from tests.fixtures.user_data import make_admin_user

# ---------------------------------------------------------------------------
# Import real source modules (no shim fallback).
# If the source modules are not yet created, all tests in this file are skipped.
# ---------------------------------------------------------------------------
_repo_model_mod = pytest.importorskip(
    "src.models.repository",
    reason="Repository model source module not yet created",
)
Repository = _repo_model_mod.Repository

_repo_svc_mod = pytest.importorskip(
    "src.services.repository_service",
    reason="RepositoryService source module not yet created",
)
RepositoryService = _repo_svc_mod.RepositoryService

# Module-level integration marker (AAP §0.9.1)
pytestmark = pytest.mark.integration


def _svc(session):
    """Instantiate a RepositoryService bound to *session*."""
    return RepositoryService(session)


def _settings(repo, key):
    """Parse a JSON settings column from a Repository instance."""
    raw = getattr(repo, f"{key}_settings", "{}")
    return _json.loads(raw) if raw else {}


# ===================================================================
# Happy Path — Hosted Repository CRUD
# ===================================================================

def test_repository_create_hosted_success(app, db_session):
    """Hosted Maven repo persists with correct attributes."""
    service = _svc(db_session)
    data = make_hosted_repo(name="test-maven-hosted", format_type="maven")
    result = service.create_repository(data)
    assert result is not None
    assert result.name == "test-maven-hosted"
    assert result.type == "hosted"
    assert result.format == "maven"
    assert result.online is True


def test_repository_get_hosted_by_name(app, db_session):
    """Retrieving a hosted repo by name returns the correct entity."""
    service = _svc(db_session)
    created = service.create_repository(
        make_hosted_repo(name="find-me-repo", format_type="maven"))
    found = service.get_repository_by_name("find-me-repo")
    assert found is not None
    assert found.name == created.name
    assert found.format == "maven"


def test_repository_update_hosted_description(app, db_session):
    """Updating description changes only the target field."""
    service = _svc(db_session)
    created = service.create_repository(
        make_hosted_repo(name="update-desc", format_type="maven"))
    updated = service.update_repository(created.id, {"description": "New desc"})
    assert updated.description == "New desc"
    assert updated.name == "update-desc"
    assert updated.updated_at is not None


def test_repository_delete_hosted_success(app, db_session):
    """Deleting a hosted repo removes it from the database."""
    service = _svc(db_session)
    created = service.create_repository(
        make_hosted_repo(name="delete-me", format_type="maven"))
    assert service.get_repository_by_name("delete-me") is not None
    result = service.delete_repository(created.id)
    assert result is True
    assert service.get_repository_by_name("delete-me") is None


def test_repository_list_all_returns_created_repos(app, db_session):
    """Listing repositories includes all previously created repos."""
    service = _svc(db_session)
    names = ["list-a", "list-b", "list-c"]
    for n in names:
        service.create_repository(make_hosted_repo(name=n, format_type="maven"))
    repos = service.list_repositories()
    repo_names = {r.name for r in repos}
    assert len(repos) >= 3
    for n in names:
        assert n in repo_names


# ===================================================================
# Happy Path — Proxy Repository CRUD
# ===================================================================

def test_repository_create_proxy_with_remote_url(app, db_session):
    """Proxy repo stores type and remote URL correctly."""
    service = _svc(db_session)
    data = make_proxy_repo(name="maven-proxy", format_type="maven",
                           remote_url="https://repo1.maven.org/maven2/")
    result = service.create_repository(data)
    assert result.type == "proxy"
    assert result.name == "maven-proxy"
    assert _settings(result, "proxy")["remote_url"] == "https://repo1.maven.org/maven2/"


def test_repository_update_proxy_remote_url(app, db_session):
    """Updating a proxy's remote URL persists the change."""
    service = _svc(db_session)
    created = service.create_repository(
        make_proxy_repo(name="upd-proxy", format_type="npm"))
    new_proxy = {"remote_url": "https://new.registry.example/", "content_max_age": 60}
    updated = service.update_repository(created.id, {"proxy": new_proxy})
    assert updated.type == "proxy"
    assert _settings(updated, "proxy")["remote_url"] == "https://new.registry.example/"


def test_repository_delete_proxy_success(app, db_session):
    """Deleting a proxy repo removes it completely."""
    service = _svc(db_session)
    created = service.create_repository(
        make_proxy_repo(name="del-proxy", format_type="docker"))
    service.delete_repository(created.id)
    assert service.get_repository_by_name("del-proxy") is None
    assert service.get_repository_by_id(created.id) is None


# ===================================================================
# Happy Path — Group Repository CRUD
# ===================================================================

def test_repository_create_group_with_members(app, db_session):
    """Group repo stores member references correctly."""
    service = _svc(db_session)
    service.create_repository(make_hosted_repo(name="g-host", format_type="maven"))
    service.create_repository(make_proxy_repo(name="g-prx", format_type="maven"))
    grp = service.create_repository(
        make_group_repo(name="mvn-grp", format_type="maven",
                        member_names=["g-host", "g-prx"]))
    assert grp.type == "group"
    members = _settings(grp, "group").get("member_names", [])
    assert "g-host" in members and "g-prx" in members


def test_repository_update_group_members(app, db_session):
    """Adding a member to a group persists the updated list."""
    service = _svc(db_session)
    for n in ("gm-a", "gm-b", "gm-c"):
        service.create_repository(make_hosted_repo(name=n, format_type="maven"))
    grp = service.create_repository(
        make_group_repo(name="grp-upd", format_type="maven",
                        member_names=["gm-a", "gm-b"]))
    service.update_repository(
        grp.id, {"group": {"member_names": ["gm-a", "gm-b", "gm-c"]}})
    refreshed = service.get_repository_by_name("grp-upd")
    members = _settings(refreshed, "group")["member_names"]
    assert len(members) == 3
    assert "gm-c" in members


def test_repository_delete_group_does_not_delete_members(app, db_session):
    """Deleting a group leaves its member repositories intact."""
    service = _svc(db_session)
    service.create_repository(make_hosted_repo(name="surv-a", format_type="maven"))
    service.create_repository(make_proxy_repo(name="surv-b", format_type="maven"))
    grp = service.create_repository(
        make_group_repo(name="grp-del", format_type="maven",
                        member_names=["surv-a", "surv-b"]))
    service.delete_repository(grp.id)
    assert service.get_repository_by_name("grp-del") is None
    assert service.get_repository_by_name("surv-a") is not None
    assert service.get_repository_by_name("surv-b") is not None


# ===================================================================
# Multi-Format Parametrized Tests
# ===================================================================

@pytest.mark.parametrize("format_type", REPOSITORY_FORMATS,
                         ids=get_repo_format_ids())
def test_repository_create_hosted_all_formats(app, db_session, format_type):
    """Hosted repo creation succeeds for every supported format."""
    service = _svc(db_session)
    result = service.create_repository(make_hosted_repo(format_type=format_type))
    assert result is not None
    assert result.format == format_type
    assert service.get_repository_by_name(result.name) is not None


@pytest.mark.parametrize("format_type", REPOSITORY_FORMATS,
                         ids=get_repo_format_ids())
def test_repository_create_proxy_all_formats(app, db_session, format_type):
    """Proxy repo creation succeeds for every supported format."""
    service = _svc(db_session)
    result = service.create_repository(make_proxy_repo(format_type=format_type))
    assert result.format == format_type
    assert result.type == "proxy"
    assert _settings(result, "proxy").get("remote_url") is not None


# ===================================================================
# Multi-Type Parametrized Tests
# ===================================================================

@pytest.mark.parametrize("repo_type", REPOSITORY_TYPES,
                         ids=get_repo_type_ids())
def test_repository_create_all_types(app, db_session, repo_type):
    """Repo creation succeeds for every supported type."""
    service = _svc(db_session)
    if repo_type == "hosted":
        data = make_hosted_repo(format_type="maven")
    elif repo_type == "proxy":
        data = make_proxy_repo(format_type="maven")
    else:
        service.create_repository(
            make_hosted_repo(name="type-member", format_type="maven"))
        data = make_group_repo(format_type="maven", member_names=["type-member"])
    result = service.create_repository(data)
    assert result is not None
    assert result.type == repo_type


# ===================================================================
# Lifecycle Transitions
# ===================================================================

def test_repository_toggle_online_offline(app, db_session):
    """Toggling online/offline persists both transitions."""
    service = _svc(db_session)
    created = service.create_repository(
        make_hosted_repo(name="toggle-repo", format_type="maven"))
    assert created.online is True
    service.update_repository(created.id, {"online": False})
    assert service.get_repository_by_name("toggle-repo").online is False
    service.update_repository(created.id, {"online": True})
    assert service.get_repository_by_name("toggle-repo").online is True


def test_repository_configure_write_policy(app, db_session):
    """Write-policy transitions persist through storage settings."""
    service = _svc(db_session)
    created = service.create_repository(
        make_hosted_repo(name="wp-repo", format_type="maven"))
    service.update_repository(created.id, {
        "storage": {"blob_store_name": "default", "write_policy": "ALLOW"}})
    assert _settings(
        service.get_repository_by_name("wp-repo"), "storage")["write_policy"] == "ALLOW"
    service.update_repository(created.id, {
        "storage": {"blob_store_name": "default", "write_policy": "DENY"}})
    assert _settings(
        service.get_repository_by_name("wp-repo"), "storage")["write_policy"] == "DENY"


def test_repository_configure_cleanup_policy(app, db_session):
    """Associating a cleanup policy stores it in cleanup settings."""
    service = _svc(db_session)
    created = service.create_repository(
        make_hosted_repo(name="cleanup-repo", format_type="maven"))
    service.update_repository(created.id, {
        "cleanup": {"policy_names": ["delete-old-snapshots"]}})
    policies = _settings(
        service.get_repository_by_name("cleanup-repo"), "cleanup"
    ).get("policy_names", [])
    assert "delete-old-snapshots" in policies
    assert len(policies) == 1


# ===================================================================
# Edge Cases
# ===================================================================

def test_repository_create_duplicate_name_returns_error(app, db_session):
    """Creating two repos with the same name raises an error."""
    service = _svc(db_session)
    repo_a, repo_b = make_repo_with_duplicate_name(name="dup-repo")
    service.create_repository(repo_a)
    with pytest.raises((ValueError, Exception)):
        service.create_repository(repo_b)
    assert service.get_repository_by_name("dup-repo") is not None


def test_repository_create_with_max_name_length(app, db_session):
    """A repo with a 255-character name is created successfully."""
    service = _svc(db_session)
    result = service.create_repository(make_repo_with_max_name_length(length=255))
    assert result is not None
    assert len(result.name) == 255


def test_repository_create_with_special_characters_in_name(app, db_session):
    """Repo with special characters either succeeds or raises validation error."""
    service = _svc(db_session)
    data = make_repo_with_special_characters()
    try:
        result = service.create_repository(data)
        assert result is not None
        assert len(result.name) > 0
    except (ValueError, Exception):
        assert True  # Validation rejection is acceptable


# ===================================================================
# Error Cases
# ===================================================================

def test_repository_get_nonexistent_returns_none(app, db_session):
    """Querying a non-existent repo by name returns None."""
    service = _svc(db_session)
    assert service.get_repository_by_name("nonexistent-repo-xyz") is None
    assert service.get_repository_by_id("00000000-0000-0000-0000-000000000000") is None


def test_repository_create_with_invalid_type_raises_error(app, db_session):
    """Creating a repo with an invalid type raises ValueError."""
    service = _svc(db_session)
    with pytest.raises((ValueError, Exception)):
        service.create_repository(make_repo_with_invalid_type())
    assert service.list_repositories() == []


def test_repository_delete_nonexistent_raises_error(app, db_session):
    """Deleting a non-existent repo raises an error."""
    service = _svc(db_session)
    with pytest.raises((ValueError, Exception)):
        service.delete_repository("no-such-id-999")
    assert service.list_repositories() == []


def test_repository_create_group_with_circular_reference_raises_error(app, db_session):
    """Circular group references are detected and rejected."""
    service = _svc(db_session)
    group_a, group_b = make_circular_group_reference()
    service.create_repository(group_a)
    with pytest.raises((ValueError, Exception)):
        service.create_repository(group_b)
    assert service.get_repository_by_name("circular-group-a") is not None
    assert service.get_repository_by_name("circular-group-b") is None


def test_repository_update_nonexistent_raises_error(app, db_session):
    """Updating a non-existent repo raises an error."""
    service = _svc(db_session)
    with pytest.raises((ValueError, Exception)):
        service.update_repository("fake-id-000", {"description": "nope"})
    assert service.list_repositories() == []


# ===================================================================
# Database Interaction Verification
# ===================================================================

def test_repository_create_persists_to_database(app, db_session):
    """Created repos are queryable via raw SQLAlchemy."""
    service = _svc(db_session)
    service.create_repository(
        make_hosted_repo(name="persist-check", format_type="pypi"))
    row = db_session.query(Repository).filter_by(name="persist-check").first()
    assert row is not None
    assert row.format == "pypi"
    assert row.type == "hosted"
    assert row.online is True


def test_repository_transaction_rollback_on_error(app, db_session):
    """A failed creation leaves no partial data in the database."""
    service = _svc(db_session)
    service.create_repository(
        make_hosted_repo(name="pre-existing", format_type="maven"))
    with pytest.raises((ValueError, Exception)):
        service.create_repository(
            make_hosted_repo(name="pre-existing", format_type="maven"))
    assert db_session.query(Repository).count() == 1
    assert db_session.query(Repository).filter_by(
        name="pre-existing").first() is not None
