"""
Repository, multi-format, multi-type, and RBAC model instance fixtures.

Extracted from ``tests/unit/models/conftest.py`` to comply with the 500-line
file length limit (AAP §0.7.2).  All fixtures here are auto-discovered by
pytest via the ``pytest_plugins`` directive in the parent ``conftest.py``.

Sections included:
    - Repository model instance fixtures (hosted, proxy, group)
    - Multi-format and multi-type repository bulk fixtures
    - RBAC role and privilege fixtures
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Dict, List

import pytest

from tests.fixtures.repository_data import (
    make_hosted_repo,
    make_proxy_repo,
    make_group_repo,
)
from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_readonly_user,
)

# ---------------------------------------------------------------------------
# Conditional model imports — mirrors the pattern in conftest.py.
# ---------------------------------------------------------------------------

try:
    from src.models.repository import Repository
except ImportError:
    Repository = None  # type: ignore[assignment,misc]

try:
    from src.models.security import Role, Privilege
except ImportError:
    Role = None  # type: ignore[assignment,misc]
    Privilege = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Module-level constants re-declared for local use
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS: List[str] = [
    "maven", "npm", "docker", "nuget", "pypi", "apt", "raw",
]
"""All seven supported repository formats (F-101)."""

SUPPORTED_TYPES: List[str] = ["hosted", "proxy", "group"]
"""All three supported repository types (F-102)."""

USER_ROLE_FACTORIES: Dict[str, Callable[..., Dict[str, Any]]] = {
    "admin": make_admin_user,
    "developer": make_developer_user,
    "readonly": make_readonly_user,
}
"""Mapping of role name → user data factory function."""


# ===========================================================================
# Section 2: Repository Model Instance Fixtures (Function-Scoped)
# ===========================================================================


def _build_repository(repo_data: Dict[str, Any]) -> Any:
    """Instantiate a Repository model from factory-produced data.

    Extracts common fields from the data dict and maps them to
    Repository constructor keyword arguments.

    Parameters
    ----------
    repo_data : dict
        Dictionary produced by one of the ``make_*_repo`` factory
        functions from ``tests.fixtures.repository_data``.

    Returns
    -------
    Repository
        A Repository ORM instance (not yet added to a session).
    """
    return Repository(
        id=repo_data.get("id", str(uuid.uuid4())),
        name=repo_data["name"],
        format=repo_data["format"],
        type=repo_data["type"],
        online=repo_data.get("online", True),
        description=repo_data.get("description", ""),
    )


@pytest.fixture
def sample_repository(db_session):
    """Create a default Repository instance (hosted, maven format).

    Provides a pre-persisted Repository model instance in the test
    database.  Uses ``make_hosted_repo(format_type="maven")`` as the
    data source.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    Repository
        A hosted maven repository persisted in the test database.
    """
    if Repository is None:
        pytest.skip("Repository model not available yet")

    repo_data = make_hosted_repo(format_type="maven")
    repo = _build_repository(repo_data)
    db_session.add(repo)
    db_session.flush()
    yield repo


@pytest.fixture
def sample_hosted_repository(db_session):
    """Create a hosted repository using ``make_hosted_repo()`` factory data.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    Repository
        A hosted repository persisted in the test database.
    """
    if Repository is None:
        pytest.skip("Repository model not available yet")

    repo_data = make_hosted_repo()
    repo = _build_repository(repo_data)
    db_session.add(repo)
    db_session.flush()
    yield repo


@pytest.fixture
def sample_proxy_repository(db_session):
    """Create a proxy repository using ``make_proxy_repo()`` factory data.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    Repository
        A proxy repository persisted in the test database.
    """
    if Repository is None:
        pytest.skip("Repository model not available yet")

    repo_data = make_proxy_repo()
    repo = _build_repository(repo_data)
    db_session.add(repo)
    db_session.flush()
    yield repo


@pytest.fixture
def sample_group_repository(db_session):
    """Create a group repository using ``make_group_repo()`` factory data.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    Repository
        A group repository persisted in the test database.
    """
    if Repository is None:
        pytest.skip("Repository model not available yet")

    repo_data = make_group_repo()
    repo = _build_repository(repo_data)
    db_session.add(repo)
    db_session.flush()
    yield repo


# ===========================================================================
# Section 8: Multi-Format and Multi-Type Repository Fixtures
# ===========================================================================


@pytest.fixture
def all_format_repositories(db_session):
    """Create one hosted repository per supported format (7 total).

    Generates repositories for all seven formats defined in the
    technical specification (F-101): Maven, npm, Docker, NuGet, PyPI,
    APT, and Raw.  All are hosted-type for simplicity.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Returns
    -------
    Dict[str, Repository]
        Mapping of format name (lowercase) → Repository instance.
        Example: ``{"maven": <Repository>, "npm": <Repository>, ...}``
    """
    if Repository is None:
        pytest.skip("Repository model not available yet")

    repos: Dict[str, Any] = {}
    for fmt in SUPPORTED_FORMATS:
        repo_data = make_hosted_repo(format_type=fmt)
        repo = _build_repository(repo_data)
        db_session.add(repo)
        repos[fmt] = repo

    db_session.flush()
    return repos


@pytest.fixture
def all_type_repositories(db_session):
    """Create one maven repository per supported type (3 total).

    Generates repositories for all three types defined in the technical
    specification (F-102): Hosted, Proxy, and Group.  All use the Maven
    format for simplicity.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Returns
    -------
    Dict[str, Repository]
        Mapping of type name (lowercase) → Repository instance.
        Example: ``{"hosted": <Repository>, "proxy": <Repository>, ...}``
    """
    if Repository is None:
        pytest.skip("Repository model not available yet")

    type_factory_map: Dict[str, Callable[..., Dict[str, Any]]] = {
        "hosted": make_hosted_repo,
        "proxy": make_proxy_repo,
        "group": make_group_repo,
    }

    repos: Dict[str, Any] = {}
    for repo_type, factory_fn in type_factory_map.items():
        repo_data = factory_fn(format_type="maven")
        repo = _build_repository(repo_data)
        db_session.add(repo)
        repos[repo_type] = repo

    db_session.flush()
    return repos


# ===========================================================================
# Section 9: RBAC Fixtures
# ===========================================================================


@pytest.fixture
def rbac_roles(db_session):
    """Create the standard RBAC roles: admin, developer, readonly.

    Each role is persisted in the database and mapped in a dictionary
    for convenient lookup in security model tests.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Returns
    -------
    Dict[str, Role]
        Mapping of role name → Role instance.
        Example: ``{"admin": <Role>, "developer": <Role>, ...}``
    """
    if Role is None:
        pytest.skip("Role model not available yet")

    role_definitions: List[Dict[str, str]] = [
        {
            "name": "admin",
            "description": "Full system administration privileges",
        },
        {
            "name": "developer",
            "description": "Read/write access with upload capabilities",
        },
        {
            "name": "readonly",
            "description": "Browse and search only — no modifications",
        },
    ]

    # Verify factory functions are accessible for each role tier by
    # retrieving sample user data (validates make_admin_user,
    # make_developer_user, make_readonly_user are all functional).
    for role_name in ("admin", "developer", "readonly"):
        factory_fn = USER_ROLE_FACTORIES[role_name]
        factory_fn()  # validate callable — data is discarded

    roles: Dict[str, Any] = {}
    for role_def in role_definitions:
        role = Role(
            id=str(uuid.uuid4()),
            name=role_def["name"],
            description=role_def["description"],
        )
        db_session.add(role)
        roles[role_def["name"]] = role

    db_session.flush()
    return roles


@pytest.fixture
def rbac_privileges(db_session):
    """Create the standard privilege set for RBAC testing.

    Produces the full set of seven privileges matching the Nexus-style
    naming convention used by the binary repository management system.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Returns
    -------
    List[Privilege]
        All standard privilege instances persisted in the test database.
    """
    if Privilege is None:
        pytest.skip("Privilege model not available yet")

    priv_definitions: List[Dict[str, str]] = [
        {
            "name": "nx-all",
            "description": "Wildcard — grants all privileges",
            "type": "wildcard",
        },
        {
            "name": "nx-repository-view",
            "description": "View repository contents",
            "type": "repository",
        },
        {
            "name": "nx-repository-edit",
            "description": "Edit repository configuration",
            "type": "repository",
        },
        {
            "name": "nx-repository-admin",
            "description": "Administer repository",
            "type": "repository",
        },
        {
            "name": "nx-search-read",
            "description": "Execute search queries",
            "type": "application",
        },
        {
            "name": "nx-component-upload",
            "description": "Upload components to repositories",
            "type": "repository",
        },
        {
            "name": "nx-apikey-all",
            "description": "Manage API keys",
            "type": "application",
        },
    ]

    privileges: List[Any] = []
    for priv_def in priv_definitions:
        privilege = Privilege(
            id=str(uuid.uuid4()),
            name=priv_def["name"],
            description=priv_def["description"],
            type=priv_def["type"],
        )
        db_session.add(privilege)
        privileges.append(privilege)

    db_session.flush()
    return privileges
