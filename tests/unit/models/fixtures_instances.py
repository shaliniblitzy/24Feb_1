"""
User, asset, blobstore, task, security model instance fixtures and the
generic ``model_factory`` utility fixture.

Extracted from ``tests/unit/models/conftest.py`` to comply with the 500-line
file length limit (AAP §0.7.2).  All fixtures here are auto-discovered by
pytest via the ``pytest_plugins`` directive in the parent ``conftest.py``.

Sections included:
    - User model instance fixtures (developer, admin)
    - Asset model instance fixtures
    - BlobStore model instance fixtures
    - Task model instance fixtures
    - Security model instance fixtures (role, privilege, content selector)
    - Generic model_factory utility fixture
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import pytest

from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    DEFAULT_PASSWORD,
)

# ---------------------------------------------------------------------------
# Conditional model imports — mirrors the pattern in conftest.py.
# ---------------------------------------------------------------------------

try:
    from src.models.user import User
except ImportError:
    User = None  # type: ignore[assignment,misc]

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

# Re-export DEFAULT_PASSWORD for convenience
MODEL_TEST_PASSWORD: str = DEFAULT_PASSWORD
"""Default plaintext password used for test user creation."""


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
