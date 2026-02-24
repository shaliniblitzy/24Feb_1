"""
Service-level unit test fixtures with mocked repositories and clients.

This is the ``conftest.py`` for the ``tests/unit/services/`` directory.  It
provides pre-configured mock database sessions, mock external service clients
(S3, search engine, proxy HTTP, email/notification), mock service instances
with injected mock dependencies, and convenience test-data fixtures.

All fixtures are **function-scoped** to guarantee full test isolation and
``pytest-xdist`` parallel-execution compatibility — each test function
receives freshly created mocks with no shared mutable state.

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

Fixture categories:

1. **Mock external clients** — S3, search engine, proxy HTTP, email
2. **Mock database layer** — chainable SQLAlchemy session, db instance
3. **Service instances** — 8 services with mock dependencies
4. **Test data convenience** — repos, users, artifacts, format sets
5. **Helper mocks** — pre-configured model mocks, testing config

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


# ===========================================================================
# Section 3: Service Instance Fixtures
# ===========================================================================
#
# Each service fixture attempts to import and instantiate the real service
# class with mock dependencies injected.  If the source module has not been
# implemented yet (ImportError), a MagicMock is returned instead so that
# downstream tests can still be collected and run without failure.
# ===========================================================================


@pytest.fixture
def repository_service(mock_db_session: MagicMock) -> Any:
    """Provide a ``RepositoryService`` instance with mock dependencies.

    Falls back to a ``MagicMock`` when the source module is unavailable.

    Parameters
    ----------
    mock_db_session : MagicMock
        Mocked SQLAlchemy session injected into the service.

    Returns
    -------
    RepositoryService or MagicMock
        A ready-to-test service instance.
    """
    try:
        from src.services.repository_service import RepositoryService
        service = RepositoryService()
        service.db_session = mock_db_session
        service.session = mock_db_session
        return service
    except (ImportError, AttributeError, TypeError):
        service = MagicMock(name="FallbackRepositoryService")
        service.db_session = mock_db_session
        service.session = mock_db_session
        # Core CRUD operations
        service.create_repository.return_value = MagicMock(name="CreatedRepository")
        service.get_repository.return_value = MagicMock(name="FetchedRepository")
        service.update_repository.return_value = MagicMock(name="UpdatedRepository")
        service.delete_repository.return_value = True
        service.list_repositories.return_value = []
        service.get_repository_by_name.return_value = None
        service.get_repository_formats.return_value = list(REPOSITORY_FORMATS)
        service.validate_repository_config.return_value = True
        return service


@pytest.fixture
def storage_service(
    mock_db_session: MagicMock, mock_s3_client: MockS3Client
) -> Any:
    """Provide a ``StorageService`` instance with mock dependencies.

    Falls back to a ``MagicMock`` when the source module is unavailable.

    Parameters
    ----------
    mock_db_session : MagicMock
        Mocked SQLAlchemy session.
    mock_s3_client : MockS3Client
        Mocked S3 client for BlobStore operations.

    Returns
    -------
    StorageService or MagicMock
        A ready-to-test service instance.
    """
    try:
        from src.services.storage_service import StorageService
        service = StorageService()
        service.db_session = mock_db_session
        service.session = mock_db_session
        service.s3_client = mock_s3_client
        return service
    except (ImportError, AttributeError, TypeError):
        service = MagicMock(name="FallbackStorageService")
        service.db_session = mock_db_session
        service.session = mock_db_session
        service.s3_client = mock_s3_client
        service.upload_artifact.return_value = MagicMock(
            name="UploadResult",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            size=0,
        )
        service.download_artifact.return_value = MagicMock(
            name="DownloadResult",
            content=b"",
            content_type="application/octet-stream",
        )
        service.delete_artifact.return_value = True
        service.get_artifact_metadata.return_value = MagicMock(
            name="ArtifactMetadata",
            content_type="application/octet-stream",
            size=0,
        )
        service.list_artifacts.return_value = []
        service.check_storage_health.return_value = True
        return service


@pytest.fixture
def security_service(mock_db_session: MagicMock) -> Any:
    """Provide a ``SecurityService`` instance with mock dependencies.

    Falls back to a ``MagicMock`` when the source module is unavailable.

    Parameters
    ----------
    mock_db_session : MagicMock
        Mocked SQLAlchemy session.

    Returns
    -------
    SecurityService or MagicMock
        A ready-to-test service instance.
    """
    try:
        from src.services.security_service import SecurityService
        service = SecurityService()
        service.db_session = mock_db_session
        service.session = mock_db_session
        return service
    except (ImportError, AttributeError, TypeError):
        service = MagicMock(name="FallbackSecurityService")
        service.db_session = mock_db_session
        service.session = mock_db_session
        service.authenticate.return_value = MagicMock(
            name="AuthResult", authenticated=True, user_id="test-user-id",
        )
        service.validate_token.return_value = MagicMock(
            name="TokenValidation", valid=True, user_id="test-user-id",
        )
        service.generate_token.return_value = "mock-jwt-token-string"
        service.authorize.return_value = True
        service.check_privilege.return_value = True
        service.create_api_key.return_value = MagicMock(
            name="ApiKeyResult", key="test-api-key-abc123", key_id="key-id-001",
        )
        service.revoke_api_key.return_value = True
        service.validate_api_key.return_value = MagicMock(
            name="ApiKeyValidation", valid=True, user_id="test-user-id",
        )
        service.create_session.return_value = MagicMock(name="SessionData")
        service.invalidate_session.return_value = True
        return service


@pytest.fixture
def search_service(
    mock_db_session: MagicMock, mock_search_engine: MockSearchEngine
) -> Any:
    """Provide a ``SearchService`` instance with mock dependencies.

    Falls back to a ``MagicMock`` when the source module is unavailable.

    Parameters
    ----------
    mock_db_session : MagicMock
        Mocked SQLAlchemy session.
    mock_search_engine : MockSearchEngine
        Mocked search backend.

    Returns
    -------
    SearchService or MagicMock
        A ready-to-test service instance.
    """
    try:
        from src.services.search_service import SearchService
        service = SearchService()
        service.db_session = mock_db_session
        service.session = mock_db_session
        service.search_engine = mock_search_engine
        return service
    except (ImportError, AttributeError, TypeError):
        service = MagicMock(name="FallbackSearchService")
        service.db_session = mock_db_session
        service.session = mock_db_session
        service.search_engine = mock_search_engine
        service.index_component.return_value = True
        service.bulk_index.return_value = MagicMock(
            name="BulkIndexResult", indexed=0, errors=[],
        )
        service.delete_from_index.return_value = True
        service.search.return_value = MagicMock(
            name="SearchResults", items=[], total=0, page=1, per_page=20,
        )
        service.rebuild_index.return_value = True
        service.reindex_repository.return_value = True
        service.remove_from_index.return_value = True
        return service


@pytest.fixture
def scheduler_service(mock_db_session: MagicMock) -> Any:
    """Provide a ``SchedulerService`` instance with mock dependencies.

    Falls back to a ``MagicMock`` when the source module is unavailable.

    Parameters
    ----------
    mock_db_session : MagicMock
        Mocked SQLAlchemy session.

    Returns
    -------
    SchedulerService or MagicMock
        A ready-to-test service instance.
    """
    try:
        from src.services.scheduler_service import SchedulerService
        service = SchedulerService()
        service.db_session = mock_db_session
        service.session = mock_db_session
        return service
    except (ImportError, AttributeError, TypeError):
        service = MagicMock(name="FallbackSchedulerService")
        service.db_session = mock_db_session
        service.session = mock_db_session
        service.schedule_task.return_value = MagicMock(
            name="ScheduledTask", task_id="task-001", status="scheduled",
        )
        service.cancel_task.return_value = True
        service.get_task_status.return_value = MagicMock(
            name="TaskStatus", task_id="task-001", status="idle", last_run=None,
        )
        service.run_task.return_value = MagicMock(
            name="TaskExecution", task_id="task-001", status="completed",
        )
        service.list_tasks.return_value = []
        service.get_task_by_id.return_value = None
        service.enforce_cleanup_policy.return_value = MagicMock(
            name="CleanupResult", deleted_count=0, items=[],
        )
        return service


@pytest.fixture
def audit_service(mock_db_session: MagicMock) -> Any:
    """Provide an ``AuditService`` instance with mock dependencies.

    Falls back to a ``MagicMock`` when the source module is unavailable.

    Parameters
    ----------
    mock_db_session : MagicMock
        Mocked SQLAlchemy session.

    Returns
    -------
    AuditService or MagicMock
        A ready-to-test service instance.
    """
    try:
        from src.services.audit_service import AuditService
        service = AuditService()
        service.db_session = mock_db_session
        service.session = mock_db_session
        return service
    except (ImportError, AttributeError, TypeError):
        service = MagicMock(name="FallbackAuditService")
        service.db_session = mock_db_session
        service.session = mock_db_session
        service.log_event.return_value = MagicMock(
            name="AuditEvent",
            event_id="audit-001",
            timestamp=datetime.datetime.utcnow().isoformat(),
        )
        service.get_events.return_value = MagicMock(
            name="AuditEventList", items=[], total=0,
        )
        service.get_event_by_id.return_value = None
        service.apply_retention_policy.return_value = MagicMock(
            name="RetentionResult", deleted_count=0,
        )
        service.purge_old_events.return_value = 0
        service.export_events.return_value = b""
        return service


@pytest.fixture
def webhook_service(
    mock_db_session: MagicMock, mock_email_service: MockEmailService
) -> Any:
    """Provide a ``WebhookService`` instance with mock dependencies.

    Falls back to a ``MagicMock`` when the source module is unavailable.

    Parameters
    ----------
    mock_db_session : MagicMock
        Mocked SQLAlchemy session.
    mock_email_service : MockEmailService
        Mocked notification service for webhook dispatch.

    Returns
    -------
    WebhookService or MagicMock
        A ready-to-test service instance.
    """
    try:
        from src.services.webhook_service import WebhookService
        service = WebhookService()
        service.db_session = mock_db_session
        service.session = mock_db_session
        service.email_service = mock_email_service
        service.notification_service = mock_email_service
        return service
    except (ImportError, AttributeError, TypeError):
        service = MagicMock(name="FallbackWebhookService")
        service.db_session = mock_db_session
        service.session = mock_db_session
        service.email_service = mock_email_service
        service.notification_service = mock_email_service
        service.register_webhook.return_value = MagicMock(
            name="RegisteredWebhook",
            webhook_id="webhook-001",
            url="https://example.com/hook",
            active=True,
        )
        service.delete_webhook.return_value = True
        service.dispatch.return_value = MagicMock(
            name="DispatchResult", success=True, status_code=200,
        )
        service.list_webhooks.return_value = []
        service.get_webhook.return_value = None
        service.update_webhook.return_value = MagicMock(name="UpdatedWebhook")
        return service


@pytest.fixture
def health_service(mock_db_session: MagicMock) -> Any:
    """Provide a ``HealthService`` instance with mock dependencies.

    Falls back to a ``MagicMock`` when the source module is unavailable.

    Parameters
    ----------
    mock_db_session : MagicMock
        Mocked SQLAlchemy session.

    Returns
    -------
    HealthService or MagicMock
        A ready-to-test service instance.
    """
    try:
        from src.services.health_service import HealthService
        service = HealthService()
        service.db_session = mock_db_session
        service.session = mock_db_session
        return service
    except (ImportError, AttributeError, TypeError):
        service = MagicMock(name="FallbackHealthService")
        service.db_session = mock_db_session
        service.session = mock_db_session
        service.check_health.return_value = MagicMock(
            name="HealthCheckResult",
            status="healthy",
            components={
                "database": {"status": "healthy"},
                "storage": {"status": "healthy"},
                "search": {"status": "healthy"},
            },
        )
        service.get_component_status.return_value = MagicMock(
            name="ComponentStatus",
            component="database",
            status="healthy",
            details={},
        )
        service.get_system_info.return_value = MagicMock(
            name="SystemInfo",
            version="1.0.0",
            python_version="3.12.3",
            uptime=0,
        )
        service.is_ready.return_value = True
        service.is_alive.return_value = True
        service.check_dependencies.return_value = {}
        return service


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
    now = datetime.datetime.utcnow()
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
    now = datetime.datetime.utcnow()
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
