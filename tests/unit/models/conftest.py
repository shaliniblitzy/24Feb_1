"""
Model-specific test fixtures with in-memory SQLite database for isolated
model testing within the ``tests/unit/models/`` directory.

Provides SQLAlchemy session fixtures with per-test cleanup for isolation.
Creates all model tables in-memory before tests and cleans up after each
test to guarantee a pristine database state.

Session-scoped fixtures (created once per test session):
    - ``model_app`` — Flask application with testing configuration
    - ``model_db`` — SQLAlchemy instance with all tables created

Function-scoped fixtures (fresh per test):
    - ``db_session`` — Database session with per-test cleanup
    - ``sample_*`` — Pre-built model instances for common test scenarios
    - ``all_format_repositories`` / ``all_type_repositories`` — Bulk fixtures
    - ``rbac_roles`` / ``rbac_privileges`` — RBAC hierarchy fixtures
    - ``model_factory`` — Generic model creation helper

Inherits the ``@pytest.mark.unit`` marker from ``tests/unit/conftest.py``
via pytest's automatic fixture discovery — do NOT set pytestmark here.

Usage example in a model unit test::

    def test_user_creation(db_session, sample_user):
        assert sample_user.username is not None
        assert sample_user.role == "developer"

    def test_all_formats(all_format_repositories):
        assert len(all_format_repositories) == 7
        assert "maven" in all_format_repositories
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import pytest

from src.app import create_app
from src.extensions import db as _db
from tests.fixtures.config_data import make_testing_config
from tests.fixtures.repository_data import (
    make_hosted_repo,
    make_proxy_repo,
    make_group_repo,
)
from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_readonly_user,
    DEFAULT_PASSWORD,
)


# ---------------------------------------------------------------------------
# Model class imports — conditional to support incremental development.
# In this greenfield project, some model modules may not yet be created by
# other agents.  When an import fails, the corresponding fixture will
# gracefully skip via ``pytest.skip()``.
# ---------------------------------------------------------------------------

try:
    from src.models.user import User
except ImportError:
    User = None  # type: ignore[assignment,misc]

try:
    from src.models.repository import Repository
except ImportError:
    Repository = None  # type: ignore[assignment,misc]

try:
    from src.models.asset import Asset
except ImportError:
    Asset = None  # type: ignore[assignment,misc]

try:
    from src.models.blobstore import BlobStore
except ImportError:
    BlobStore = None  # type: ignore[assignment,misc]

try:
    from src.models.task import Task
except ImportError:
    Task = None  # type: ignore[assignment,misc]

try:
    from src.models.security import Role, Privilege, ContentSelector
except ImportError:
    Role = None  # type: ignore[assignment,misc]
    Privilege = None  # type: ignore[assignment,misc]
    ContentSelector = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Module-level constants — mirrors of the 7 supported repository formats
# and 3 repository types from the technical specification, used by bulk
# fixture generators.
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS: List[str] = [
    "maven", "npm", "docker", "nuget", "pypi", "apt", "raw",
]
"""All seven supported repository formats (F-101)."""

SUPPORTED_TYPES: List[str] = ["hosted", "proxy", "group"]
"""All three supported repository types (F-102)."""

# User factory registry — makes all three user factory functions available
# as a dict for programmatic use by model_factory and parametrized tests.
USER_ROLE_FACTORIES: Dict[str, Callable[..., Dict[str, Any]]] = {
    "admin": make_admin_user,
    "developer": make_developer_user,
    "readonly": make_readonly_user,
}
"""Mapping of role name → user data factory function."""

# Re-export DEFAULT_PASSWORD for convenience; model tests that need to
# verify password hashing or authentication behaviour can import it from
# this conftest module rather than reaching into tests.fixtures.
MODEL_TEST_PASSWORD: str = DEFAULT_PASSWORD
"""Default plaintext password used for test user creation."""


# ===========================================================================
# Section 1: Application and Database Fixtures (Session-Scoped)
# ===========================================================================


@pytest.fixture(scope="session")
def model_app():
    """Create a Flask application instance configured for model testing.

    Uses ``make_testing_config()`` from the config data fixture factory
    to produce a consistent testing configuration:

    - ``TESTING = True``
    - ``SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'``
    - ``SECRET_KEY = 'test-secret-key-not-for-production'``
    - ``SQLALCHEMY_TRACK_MODIFICATIONS = False``

    Session-scoped for efficiency — the application is created once and
    reused across all model tests in the session.

    Yields
    ------
    Flask
        The configured Flask application with an active application context.
    """
    config = make_testing_config()
    app = create_app(config)

    # Push an application context for the entire session so that
    # ``current_app``, ``db``, and other context-dependent objects
    # are available to all model tests.
    ctx = app.app_context()
    ctx.push()

    yield app

    # Teardown: pop the session-scoped application context
    ctx.pop()


@pytest.fixture(scope="session")
def model_db(model_app):
    """Create all database tables and provide the SQLAlchemy instance.

    Calls ``db.create_all()`` to materialise every ORM-mapped table in
    the in-memory SQLite database.  Tables are dropped during teardown.

    Session-scoped — tables are created once and reused across all model
    tests for efficiency.  Per-test data isolation is handled by the
    ``db_session`` fixture's cleanup mechanism.

    Parameters
    ----------
    model_app : Flask
        The session-scoped Flask application fixture (ensures the
        application context is active during table creation).

    Yields
    ------
    SQLAlchemy
        The Flask-SQLAlchemy database instance with all tables created.
    """
    _db.create_all()

    yield _db

    # Teardown: remove the scoped session and drop all tables
    _db.session.remove()
    _db.drop_all()


@pytest.fixture(scope="function")
def db_session(model_db, model_app):
    """Provide a database session with per-test isolation via cleanup.

    This is the PRIMARY fixture used by all model tests for database
    interactions.  After each test completes (success or failure):

    1. Any uncommitted changes are rolled back.
    2. All committed data is deleted from every table in reverse
       dependency order to respect foreign key constraints.
    3. The session is committed to finalise cleanup.

    This guarantees that every test starts with a completely empty
    database, enabling independent and parallel execution with
    ``pytest-xdist``.

    Parameters
    ----------
    model_db : SQLAlchemy
        The session-scoped database instance with tables created.
    model_app : Flask
        The session-scoped Flask application (ensures app context).

    Yields
    ------
    scoped_session
        The SQLAlchemy scoped session ready for database operations.
    """
    yield model_db.session

    # Phase 1: Roll back any uncommitted changes from the test
    model_db.session.rollback()

    # Phase 2: Delete all committed data from every table in reverse
    # dependency order to respect foreign key constraints
    for table in reversed(model_db.metadata.sorted_tables):
        model_db.session.execute(table.delete())
    model_db.session.commit()


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
# Section 3: User Model Instance Fixtures (Function-Scoped)
# ===========================================================================


def _build_user_from_factory(user_data: Dict[str, Any]) -> Any:
    """Instantiate a User model from factory-produced data.

    Extracts common User fields from the data dict.  Uses
    ``DEFAULT_PASSWORD`` as the basis for the password hash when the
    factory data does not provide one.

    Parameters
    ----------
    user_data : dict
        Dictionary produced by one of the ``make_*_user`` factory
        functions from ``tests.fixtures.user_data``.

    Returns
    -------
    User
        A User ORM instance (not yet added to a session).
    """
    now = datetime.now(timezone.utc)
    return User(
        id=user_data.get("id", str(uuid.uuid4())),
        username=user_data["username"],
        email=user_data.get("email", f"test-{uuid.uuid4().hex[:8]}@test.example.com"),
        first_name=user_data.get("first_name", "Test"),
        last_name=user_data.get("last_name", "User"),
        role=user_data.get("role", "developer"),
        status=user_data.get("status", "active"),
        is_admin=user_data.get("is_admin", False),
        password_hash=user_data.get(
            "password_hash",
            f"sha256$fallback-for-{MODEL_TEST_PASSWORD}",
        ),
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def sample_user(db_session):
    """Create a default User instance (developer role).

    Uses ``make_developer_user()`` as the data source with
    ``DEFAULT_PASSWORD`` as the authentication password.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    User
        A developer user persisted in the test database.
    """
    if User is None:
        pytest.skip("User model not available yet")

    user_data = make_developer_user()
    user = _build_user_from_factory(user_data)
    db_session.add(user)
    db_session.flush()
    yield user


@pytest.fixture
def sample_admin_user(db_session):
    """Create an admin User instance using ``make_admin_user()`` data.

    The admin user has ``is_admin=True`` and the ``admin`` role,
    granting full system privileges.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    User
        An admin user persisted in the test database.
    """
    if User is None:
        pytest.skip("User model not available yet")

    user_data = make_admin_user()
    now = datetime.now(timezone.utc)
    user = User(
        id=user_data.get("id", str(uuid.uuid4())),
        username=user_data["username"],
        email=user_data.get(
            "email", f"admin-{uuid.uuid4().hex[:8]}@test.example.com"
        ),
        first_name=user_data.get("first_name", "Test"),
        last_name=user_data.get("last_name", "Admin"),
        role="admin",
        status="active",
        is_admin=True,
        password_hash=user_data.get(
            "password_hash",
            f"sha256$fallback-for-{DEFAULT_PASSWORD}",
        ),
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    db_session.flush()
    yield user


# ===========================================================================
# Section 4: Asset Model Instance Fixtures (Function-Scoped)
# ===========================================================================


@pytest.fixture
def sample_asset(db_session):
    """Create an Asset model instance with default values.

    Produces a Maven JAR artifact with realistic SHA-1 and SHA-256
    checksums, linked to a test path under ``/org/example/``.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    Asset
        An asset (component) persisted in the test database.
    """
    if Asset is None:
        pytest.skip("Asset model not available yet")

    now = datetime.now(timezone.utc)
    asset = Asset(
        id=str(uuid.uuid4()),
        name="test-artifact-1.0.0.jar",
        format="maven",
        content_type="application/java-archive",
        size=1024,
        sha1="da39a3ee5e6b4b0d3255bfef95601890afd80709",
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        path="/org/example/test-artifact/1.0.0/test-artifact-1.0.0.jar",
        created_at=now,
        updated_at=now,
    )
    db_session.add(asset)
    db_session.flush()
    yield asset


# ===========================================================================
# Section 5: BlobStore Model Instance Fixtures (Function-Scoped)
# ===========================================================================


@pytest.fixture
def sample_blobstore(db_session):
    """Create a BlobStore model instance (file type, default path).

    Produces a file-based BlobStore configuration pointing to a local
    storage path.  Suitable for tests that validate BlobStore model
    field constraints and serialisation.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    BlobStore
        A file BlobStore configuration persisted in the test database.
    """
    if BlobStore is None:
        pytest.skip("BlobStore model not available yet")

    now = datetime.now(timezone.utc)
    blobstore = BlobStore(
        id=str(uuid.uuid4()),
        name="default",
        type="file",
        path="/data/blobstore/default",
        created_at=now,
        updated_at=now,
    )
    db_session.add(blobstore)
    db_session.flush()
    yield blobstore


# ===========================================================================
# Section 6: Task Model Instance Fixtures (Function-Scoped)
# ===========================================================================


@pytest.fixture
def sample_task(db_session):
    """Create a Task model instance with a cron schedule.

    Produces a scheduled cleanup task configured to run daily at 02:00
    UTC.  The task is in ``idle`` state and enabled, suitable for tests
    that validate task scheduling, state transitions, and cron parsing.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    Task
        A scheduled task persisted in the test database.
    """
    if Task is None:
        pytest.skip("Task model not available yet")

    now = datetime.now(timezone.utc)
    task = Task(
        id=str(uuid.uuid4()),
        name="cleanup-snapshots",
        type="repository.cleanup",
        schedule="0 2 * * *",
        enabled=True,
        status="idle",
        created_at=now,
        updated_at=now,
    )
    db_session.add(task)
    db_session.flush()
    yield task


# ===========================================================================
# Section 7: Security Model Instance Fixtures (Function-Scoped)
# ===========================================================================


@pytest.fixture
def sample_role(db_session):
    """Create a Role model instance with standard developer privileges.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    Role
        A role persisted in the test database.
    """
    if Role is None:
        pytest.skip("Role model not available yet")

    role = Role(
        id=str(uuid.uuid4()),
        name="test-developer-role",
        description="Developer role with read/write and upload privileges",
    )
    db_session.add(role)
    db_session.flush()
    yield role


@pytest.fixture
def sample_privilege(db_session):
    """Create a Privilege model instance for repository viewing.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    Privilege
        A privilege persisted in the test database.
    """
    if Privilege is None:
        pytest.skip("Privilege model not available yet")

    privilege = Privilege(
        id=str(uuid.uuid4()),
        name="nx-repository-view",
        description="View repository contents and browse artefacts",
        type="repository",
    )
    db_session.add(privilege)
    db_session.flush()
    yield privilege


@pytest.fixture
def sample_content_selector(db_session):
    """Create a ContentSelector model instance with a CSEL expression.

    The selector targets all Maven artefacts under the ``org.example``
    namespace, useful for testing fine-grained RBAC content selectors.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Yields
    ------
    ContentSelector
        A content selector persisted in the test database.
    """
    if ContentSelector is None:
        pytest.skip("ContentSelector model not available yet")

    selector = ContentSelector(
        id=str(uuid.uuid4()),
        name="test-maven-selector",
        description="Selects all Maven artefacts in org.example namespace",
        type="csel",
        expression='format == "maven2" and path =^ "/org/example/"',
    )
    db_session.add(selector)
    db_session.flush()
    yield selector


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


# ===========================================================================
# Section 10: Utility Fixtures
# ===========================================================================


@pytest.fixture
def model_factory(db_session):
    """Provide a generic helper function for creating and persisting models.

    Returns a callable ``create(model_class, **kwargs)`` that:

    1. Instantiates ``model_class(**kwargs)``
    2. Adds the instance to the session
    3. Flushes to assign database-generated values (e.g., defaults)
    4. Returns the persisted instance

    If the requested ``model_class`` is ``None`` (not yet available),
    the test is skipped automatically.

    Parameters
    ----------
    db_session : scoped_session
        The per-test database session.

    Returns
    -------
    Callable[[type, ...], Any]
        A ``create(model_class, **kwargs)`` factory function.

    Example
    -------
    ::

        def test_custom_user(model_factory):
            user = model_factory(User, username="custom", role="admin")
            assert user.id is not None

        def test_with_readonly_data(model_factory):
            data = make_readonly_user()
            user = model_factory(
                User,
                username=data["username"],
                role=data["role"],
                email=data["email"],
            )
            assert user.role == "readonly"
    """
    created_instances: list = []

    def create(model_class: Optional[type], **kwargs: Any) -> Any:
        """Create, persist, and return an ORM model instance.

        Parameters
        ----------
        model_class : type or None
            The SQLAlchemy model class to instantiate.  If ``None``,
            the calling test is skipped.
        **kwargs : Any
            Keyword arguments passed to the model constructor.

        Returns
        -------
        Any
            The persisted model instance with database-generated fields
            populated.

        Raises
        ------
        pytest.skip
            If ``model_class`` is ``None``.
        """
        if model_class is None:
            pytest.skip(
                "Requested model class is not available yet — "
                "ensure the corresponding source module has been created."
            )

        # Provide a default ``id`` if the model expects a string UUID PK
        # and the caller did not supply one.
        if "id" not in kwargs:
            kwargs.setdefault("id", str(uuid.uuid4()))

        instance = model_class(**kwargs)
        db_session.add(instance)
        db_session.flush()
        created_instances.append(instance)
        return instance

    return create
