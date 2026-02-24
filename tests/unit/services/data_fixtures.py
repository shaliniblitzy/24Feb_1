"""
Test data convenience fixtures and pre-configured mock model instances
for service unit tests.

Extracted from ``tests/unit/services/conftest.py`` to comply with the
500-line file length limit (AAP §0.7.2).  All fixtures here are
auto-discovered by pytest via the ``pytest_plugins`` directive in the
parent ``conftest.py``.

Sections included:
    - Test data convenience fixtures (sample repos, users, artifacts)
    - Helper mock fixtures (pre-configured mock model instances)
    - Service configuration fixture
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Dict, List

import pytest
from unittest.mock import MagicMock

from tests.fixtures.config_data import make_testing_config
from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_readonly_user,
)
from tests.fixtures.repository_data import (
    make_hosted_repo,
    make_proxy_repo,
    make_group_repo,
    make_all_format_repos,
)
from tests.fixtures.artifact_data import make_binary_artifact


# ===========================================================================
# Section 4: Test Data Convenience Fixtures
# ===========================================================================


@pytest.fixture
def sample_hosted_repo() -> Dict[str, Any]:
    """Provide a pre-built hosted repository configuration dictionary.

    Convenience wrapper around ``make_hosted_repo()`` that returns a fresh
    repo dict for every test invocation, ensuring test isolation.

    Returns
    -------
    Dict[str, Any]
        A hosted repository configuration with storage, cleanup, and
        component settings populated.
    """
    return make_hosted_repo()


@pytest.fixture
def sample_proxy_repo() -> Dict[str, Any]:
    """Provide a pre-built proxy repository configuration dictionary.

    Convenience wrapper around ``make_proxy_repo()``.

    Returns
    -------
    Dict[str, Any]
        A proxy repository configuration with upstream URL, caching
        parameters, and HTTP client settings populated.
    """
    return make_proxy_repo()


@pytest.fixture
def sample_group_repo() -> Dict[str, Any]:
    """Provide a pre-built group repository configuration dictionary.

    Convenience wrapper around ``make_group_repo()``.

    Returns
    -------
    Dict[str, Any]
        A group repository configuration with member aggregation settings.
    """
    return make_group_repo()


@pytest.fixture
def sample_admin_user() -> Dict[str, Any]:
    """Provide a pre-built admin user data dictionary.

    Returns a fresh admin user dict on every invocation with full system
    privileges (``PRIV_ALL`` and the ``nx-admin`` global role).

    Returns
    -------
    Dict[str, Any]
        An admin user configuration dictionary.
    """
    return make_admin_user()


@pytest.fixture
def sample_developer_user() -> Dict[str, Any]:
    """Provide a pre-built developer user data dictionary.

    Returns a fresh developer user dict with read/write and upload privileges.

    Returns
    -------
    Dict[str, Any]
        A developer user configuration dictionary.
    """
    return make_developer_user()


@pytest.fixture
def sample_readonly_user() -> Dict[str, Any]:
    """Provide a pre-built read-only user data dictionary.

    Returns a fresh read-only user dict with browse/search only privileges.

    Returns
    -------
    Dict[str, Any]
        A read-only user configuration dictionary.
    """
    return make_readonly_user()


@pytest.fixture
def sample_artifact() -> Dict[str, Any]:
    """Provide a pre-built binary artifact data dictionary.

    Returns a fresh artifact dict with random binary content, pre-computed
    checksums (SHA-1, SHA-256, MD5), size, and content type.

    Returns
    -------
    Dict[str, Any]
        An artifact dictionary with ``content``, ``size``, ``sha1``,
        ``sha256``, ``md5``, and ``content_type`` keys.
    """
    return make_binary_artifact()


@pytest.fixture
def all_format_repos() -> List[Dict[str, Any]]:
    """Provide one hosted repository configuration per supported format.

    Returns a list of 7 hosted repository dicts — one for each of the
    supported formats: Maven, npm, Docker, NuGet, PyPI, APT, and Raw.
    Useful for parametrized tests exercising format-specific behavior.

    Returns
    -------
    List[Dict[str, Any]]
        Seven hosted repository configuration dicts.
    """
    return make_all_format_repos("hosted")


# ===========================================================================
# Section 5: Helper Fixtures
# ===========================================================================


@pytest.fixture
def mock_repository_model() -> MagicMock:
    """Provide a pre-configured mock simulating a Repository model instance.

    The mock is pre-populated with common model fields used across service
    tests, making it convenient to plug into ``query().first()`` return
    values without manually configuring every field.

    Returns
    -------
    MagicMock
        A mock simulating a Repository ORM model instance.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    model = MagicMock(name="MockRepositoryModel")
    model.id = str(uuid.uuid4())
    model.name = "test-maven-hosted"
    model.format = "maven"
    model.type = "hosted"
    model.online = True
    model.description = "Test hosted Maven repository"
    model.created_at = now
    model.updated_at = now
    model.last_modified = now
    model.storage = {
        "blob_store_name": "default",
        "strict_content_type_validation": True,
        "write_policy": "ALLOW_ONCE",
    }
    model.cleanup = {"policy_names": []}
    model.component = {"proprietary_components": True}
    # Provide a to_dict/serialize helper
    model.to_dict.return_value = {
        "id": model.id,
        "name": model.name,
        "format": model.format,
        "type": model.type,
        "online": model.online,
        "description": model.description,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    return model


@pytest.fixture
def mock_user_model() -> MagicMock:
    """Provide a pre-configured mock simulating a User model instance.

    The mock is pre-populated with common user fields, making it suitable
    for plugging into ``query().filter_by(username=...).first()`` return
    values in security and audit service tests.

    Returns
    -------
    MagicMock
        A mock simulating a User ORM model instance.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    model = MagicMock(name="MockUserModel")
    model.id = str(uuid.uuid4())
    model.username = "test-developer"
    model.email = "developer@example.com"
    model.role = "developer"
    model.password_hash = "sha256$fakehashvalue"
    model.is_active = True
    model.is_admin = False
    model.status = "active"
    model.created_at = now
    model.updated_at = now
    model.last_login = None
    model.privileges = ["nx-repository-view", "nx-repository-edit", "nx-search-read"]
    model.global_roles = ["nx-developer"]
    # Provide a to_dict/serialize helper
    model.to_dict.return_value = {
        "id": model.id,
        "username": model.username,
        "email": model.email,
        "role": model.role,
        "is_active": model.is_active,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    # Password checking helper
    model.check_password.return_value = True
    return model


@pytest.fixture
def service_config() -> Dict[str, Any]:
    """Provide the standard testing configuration for service instantiation.

    Convenience wrapper around ``make_testing_config()`` from the config
    data fixture factory module.

    Returns
    -------
    Dict[str, Any]
        A Flask testing configuration dictionary with ``TESTING=True``,
        in-memory SQLite, and test-only secret keys.
    """
    return make_testing_config()
