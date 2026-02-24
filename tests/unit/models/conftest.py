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

Additional model instance fixtures (repository, user, asset, blobstore,
task, security, RBAC, and the generic ``model_factory``) are defined in
extracted modules registered via ``pytest_plugins`` below:

    - ``tests.unit.models.fixtures_repository`` — repository, multi-format,
      multi-type, and RBAC fixtures
    - ``tests.unit.models.fixtures_instances`` — user, asset, blobstore,
      task, security model instance fixtures and ``model_factory``

Inherits the ``@pytest.mark.unit`` marker from ``tests/unit/conftest.py``.

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
# Fixture re-exports — import all fixtures from extracted modules so that
# pytest auto-discovers them in this conftest namespace.  This keeps
# conftest.py under the 500-line limit (AAP §0.7.2) while still exposing
# the full fixture catalog to model unit tests.
# ---------------------------------------------------------------------------

from tests.unit.models.fixtures_repository import (  # noqa: F401
    sample_repository,
    sample_hosted_repository,
    sample_proxy_repository,
    sample_group_repository,
    all_format_repositories,
    all_type_repositories,
    rbac_roles,
    rbac_privileges,
)
from tests.unit.models.fixtures_instances import (  # noqa: F401
    sample_user,
    sample_admin_user,
    sample_asset,
    sample_blobstore,
    sample_task,
    sample_role,
    sample_privilege,
    sample_content_selector,
    model_factory,
)


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
