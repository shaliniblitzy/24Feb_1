"""
Service-level unit test fixtures with mocked repositories and clients.

This is the ``conftest.py`` for the ``tests/unit/services/`` directory.  It
provides pre-configured mock database sessions, mock external service clients
(S3, search engine, proxy HTTP, email/notification), and the chainable mock
SQLAlchemy query builder.

Additional service instance fixtures, test data convenience fixtures, and
pre-configured mock model instances are defined in extracted modules
registered via ``pytest_plugins`` below:

    - ``tests.unit.services.service_fixtures`` — 8 service instance
      fixtures with mock dependency injection
    - ``tests.unit.services.data_fixtures`` — test data convenience
      fixtures, mock model instances, and service configuration

This module **inherits** fixtures from the parent conftest hierarchy:

- ``tests/conftest.py`` — application factory, test client, DB session
- ``tests/unit/conftest.py`` — ``pytestmark = pytest.mark.unit`` marker,
  parent-level mock external clients and mock service stubs

The fixtures here *override* the parent's equally-named fixtures with
service-level versions tailored for isolated service unit tests.  In
particular, the **service instance fixtures** (``repository_service``,
``storage_service``, …) attempt to create real service class instances
with injected mock dependencies, falling back to ``MagicMock`` when the
source modules have not yet been implemented.

Fixture categories provided here:

1. **Mock external clients** — S3, search engine, proxy HTTP, email
2. **Mock database layer** — chainable SQLAlchemy session, db instance

Usage in a service unit test::

    def test_create_hosted_repo(repository_service, mock_db_session, sample_hosted_repo):
        mock_db_session.query().filter_by().first.return_value = None
        result = repository_service.create_repository(sample_hosted_repo)
        assert result is not None
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Dict, List

import pytest
from unittest.mock import MagicMock, patch, PropertyMock, create_autospec

# ---------------------------------------------------------------------------
# Internal mock imports — custom mock client classes from tests/mocks/
# ---------------------------------------------------------------------------
from tests.mocks.mock_s3_client import MockS3Client, create_mock_s3_client
from tests.mocks.mock_search_engine import (
    MockSearchEngine,
    create_mock_search_engine,
)
from tests.mocks.mock_proxy_client import (
    MockProxyClient,
    create_mock_proxy_client,
)
from tests.mocks.mock_email_service import (
    MockEmailService,
    create_mock_email_service,
)

# ---------------------------------------------------------------------------
# Internal fixture data imports — factory functions from tests/fixtures/
# ---------------------------------------------------------------------------
from tests.fixtures.config_data import make_testing_config
from tests.fixtures.user_data import (
    make_admin_user,
    make_developer_user,
    make_readonly_user,
    make_anonymous_user,
)
from tests.fixtures.repository_data import (
    make_hosted_repo,
    make_proxy_repo,
    make_group_repo,
    make_all_format_repos,
    REPOSITORY_FORMATS,
    REPOSITORY_TYPES,
)
from tests.fixtures.artifact_data import (
    make_binary_artifact,
    make_maven_artifact,
    make_npm_artifact,
)


# ---------------------------------------------------------------------------
# Fixture re-exports — import all fixtures from extracted modules so that
# pytest auto-discovers them in this conftest namespace.  This keeps
# conftest.py under the 500-line limit (AAP §0.7.2) while still exposing
# the full fixture catalog to service unit tests.
# ---------------------------------------------------------------------------

from tests.unit.services.service_fixtures import (  # noqa: F401
    repository_service,
    storage_service,
    security_service,
    search_service,
    scheduler_service,
    audit_service,
    webhook_service,
    health_service,
)
from tests.unit.services.data_fixtures import (  # noqa: F401
    sample_hosted_repo,
    sample_proxy_repo,
    sample_group_repo,
    sample_admin_user,
    sample_developer_user,
    sample_readonly_user,
    sample_artifact,
    all_format_repos,
    mock_repository_model,
    mock_user_model,
    service_config,
)


# ===========================================================================
# Section 1: Mock External Client Fixtures
# ===========================================================================


@pytest.fixture
def mock_s3_client() -> MockS3Client:
    """Provide a fresh ``MockS3Client`` for S3 BlobStore service tests.

    Creates an in-memory mock of the boto3 S3 client supporting
    ``put_object``, ``get_object``, ``delete_object``, ``list_objects_v2``,
    ``head_object``, ``create_bucket``, and ``delete_bucket`` operations.

    The client is **automatically reset** after the test completes, clearing
    all stored objects, call logs, and error configurations.

    Yields
    ------
    MockS3Client
        A freshly initialised mock S3 client.
    """
    client = create_mock_s3_client()
    yield client
    client.reset()


@pytest.fixture
def mock_search_engine() -> MockSearchEngine:
    """Provide a fresh ``MockSearchEngine`` for search service tests.

    Creates an in-memory mock of an Elasticsearch-like search backend
    supporting ``create_index``, ``index``, ``search``, ``delete``,
    ``delete_index``, and ``bulk_index`` operations.

    The engine is **automatically reset** after the test completes.

    Yields
    ------
    MockSearchEngine
        A freshly initialised mock search engine.
    """
    engine = create_mock_search_engine()
    yield engine
    engine.reset()


@pytest.fixture
def mock_proxy_client() -> MockProxyClient:
    """Provide a fresh ``MockProxyClient`` for upstream proxy tests.

    Creates a mock HTTP client simulating responses from upstream registries
    (Maven Central, npm, Docker Hub, PyPI, NuGet, APT) with configurable
    status codes and payloads.

    The client is **automatically reset** after the test completes.

    Yields
    ------
    MockProxyClient
        A freshly initialised mock proxy client.
    """
    client = create_mock_proxy_client()
    yield client
    client.reset()


@pytest.fixture
def mock_email_service() -> MockEmailService:
    """Provide a fresh ``MockEmailService`` for webhook and alert tests.

    Creates a mock notification service that captures sent emails, webhooks,
    and alerts in memory for assertion without performing actual dispatch.

    The service is **automatically reset** after the test completes.

    Yields
    ------
    MockEmailService
        A freshly initialised mock email/notification service.
    """
    service = create_mock_email_service()
    yield service
    service.reset()


# ===========================================================================
# Section 2: Mock Database Layer Fixtures
# ===========================================================================


def _build_chainable_query_mock() -> MagicMock:
    """Build a ``MagicMock`` replicating SQLAlchemy's chainable query API.

    The returned mock supports full method chaining such as::

        session.query(Model).filter_by(name='x').order_by(Model.id).first()

    Every chainable method returns the query mock itself.  Terminal methods
    return sensible default values (``None``, ``[]``, ``0``).

    Returns
    -------
    MagicMock
        A mock object replicating the SQLAlchemy Query interface.
    """
    query_mock = MagicMock(name="ServiceQueryMock")

    # Chainable methods — each returns the query itself for chaining
    chainable_methods = [
        "filter",
        "filter_by",
        "join",
        "outerjoin",
        "group_by",
        "having",
        "order_by",
        "limit",
        "offset",
        "distinct",
        "subquery",
        "options",
        "with_entities",
        "union",
        "union_all",
        "intersect",
        "except_",
    ]
    for method_name in chainable_methods:
        getattr(query_mock, method_name).return_value = query_mock

    # Terminal methods — return sensible defaults
    query_mock.first.return_value = None
    query_mock.all.return_value = []
    query_mock.one_or_none.return_value = None
    query_mock.one.return_value = MagicMock(name="SingleResult")
    query_mock.count.return_value = 0
    query_mock.scalar.return_value = None
    query_mock.exists.return_value = MagicMock(name="ExistsSubquery")
    query_mock.paginate.return_value = MagicMock(
        name="Pagination",
        items=[],
        total=0,
        pages=0,
        page=1,
        per_page=20,
        has_prev=False,
        has_next=False,
        prev_num=None,
        next_num=None,
    )
    query_mock.delete.return_value = 0
    query_mock.update.return_value = 0

    # get() — SQLAlchemy query.get(pk) shorthand
    query_mock.get.return_value = None
    query_mock.get_or_404.return_value = MagicMock(name="GetOr404Result")

    return query_mock


@pytest.fixture
def mock_db_session() -> MagicMock:
    """Provide a fully mocked SQLAlchemy session for service unit tests.

    The mock session supports all common SQLAlchemy session operations
    (``add``, ``commit``, ``rollback``, ``query``, ``delete``, ``flush``,
    ``refresh``, ``close``, ``execute``, ``merge``, ``expunge``) with
    sensible defaults.

    The ``query()`` method returns a **chainable** mock supporting the
    full SQLAlchemy query builder pattern — e.g.::

        session.query(Model).filter_by(name='x').first()

    This is the **primary fixture** for service unit tests needing to mock
    SQLAlchemy interactions.

    Returns
    -------
    MagicMock
        A mock simulating a ``db.session`` SQLAlchemy session.
    """
    session = MagicMock(name="ServiceMockDBSession")

    # Build a chainable query mock and wire it to session.query()
    query_mock = _build_chainable_query_mock()
    session.query.return_value = query_mock

    # Standard session mutators
    session.add.return_value = None
    session.add_all.return_value = None
    session.delete.return_value = None
    session.commit.return_value = None
    session.rollback.return_value = None
    session.flush.return_value = None
    session.refresh.return_value = None
    session.close.return_value = None
    session.expire.return_value = None
    session.expunge.return_value = None
    session.expunge_all.return_value = None
    session.merge.return_value = MagicMock(name="MergedObject")
    session.execute.return_value = MagicMock(
        name="ExecuteResult",
        fetchall=MagicMock(return_value=[]),
        fetchone=MagicMock(return_value=None),
        scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
    )

    # Context manager support (for ``with session.begin():`` blocks)
    session.begin.return_value.__enter__ = MagicMock(return_value=session)
    session.begin.return_value.__exit__ = MagicMock(return_value=False)
    session.begin_nested.return_value.__enter__ = MagicMock(return_value=session)
    session.begin_nested.return_value.__exit__ = MagicMock(return_value=False)

    # Boolean predicates
    session.is_active = True
    session.is_modified = MagicMock(return_value=False)
    session.new = []
    session.dirty = []
    session.deleted = []

    return session


@pytest.fixture
def mock_db(mock_db_session: MagicMock) -> MagicMock:
    """Provide a fully mocked SQLAlchemy ``db`` extension object.

    The mock reuses ``mock_db_session`` for its ``.session`` attribute and
    provides stubs for ``create_all()``, ``drop_all()``, ``engine``, and
    ``Model``.

    Parameters
    ----------
    mock_db_session : MagicMock
        The mocked database session fixture, wired as ``db.session``.

    Returns
    -------
    MagicMock
        A mock simulating the ``flask_sqlalchemy.SQLAlchemy`` instance.
    """
    db_mock = MagicMock(name="ServiceMockDB")

    # Wire the shared session mock
    db_mock.session = mock_db_session

    # Schema management
    db_mock.create_all.return_value = None
    db_mock.drop_all.return_value = None

    # Engine mock with basic interface
    db_mock.engine = MagicMock(
        name="ServiceMockEngine",
        dispose=MagicMock(return_value=None),
        execute=MagicMock(
            return_value=MagicMock(fetchall=MagicMock(return_value=[]))
        ),
        url=MagicMock(__str__=MagicMock(return_value="sqlite:///:memory:")),
    )

    # Model base class mock
    db_mock.Model = MagicMock(name="MockModelBase")

    # Column / relationship / metadata mocks for model definitions
    db_mock.Column = MagicMock(name="MockColumn")
    db_mock.String = MagicMock(name="MockString")
    db_mock.Integer = MagicMock(name="MockInteger")
    db_mock.Boolean = MagicMock(name="MockBoolean")
    db_mock.DateTime = MagicMock(name="MockDateTime")
    db_mock.Text = MagicMock(name="MockText")
    db_mock.ForeignKey = MagicMock(name="MockForeignKey")
    db_mock.relationship = MagicMock(name="MockRelationship")
    db_mock.metadata = MagicMock(name="MockMetadata")

    # Init-app mock
    db_mock.init_app = MagicMock(return_value=None)

    return db_mock
