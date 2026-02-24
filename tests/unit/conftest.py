"""
Unit test-scoped fixtures with fully mocked dependencies.

This is the ``conftest.py`` for the ``tests/unit/`` directory.  It provides
unit test-scoped fixtures where **all** external dependencies are fully mocked,
ensuring that no real database, network, filesystem, or external service calls
are made during unit test execution.

It inherits the root fixtures from ``tests/conftest.py`` (``app``, ``client``,
``db_instance``, ``db_session``, ``auth_headers``) via pytest's automatic
fixture discovery mechanism.  On top of that, this module adds unit-test-specific
fixtures with mock objects suitable for isolating individual components.

Fixture categories provided here:

- **Mock external service clients** — ``mock_s3_client``, ``mock_search_engine``,
  ``mock_proxy_client``, ``mock_email_service``
- **Mock database layer** — ``mock_db_session``, ``mock_db``
- **Mock application services** — ``mock_repository_service``,
  ``mock_storage_service``, ``mock_security_service``, ``mock_search_service``,
  ``mock_scheduler_service``, ``mock_audit_service``, ``mock_webhook_service``,
  ``mock_health_service``
- **Test data convenience fixtures** — ``sample_admin_user``,
  ``sample_developer_user``, ``sample_readonly_user``, ``sample_hosted_repo``,
  ``sample_proxy_repo``, ``sample_group_repo``

All fixtures are **function-scoped** for full test isolation and
``pytest-xdist`` parallel-execution compatibility.  Mock client fixtures use
the ``yield`` pattern with post-test ``reset()`` for clean-up.

Usage example in a unit test::

    def test_repository_creation(mock_repository_service, sample_hosted_repo):
        mock_repository_service.create_repository.return_value = sample_hosted_repo
        result = mock_repository_service.create_repository(sample_hosted_repo)
        assert result["type"] == "hosted"
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

# ---------------------------------------------------------------------------
# Internal mock imports — custom mock client classes from tests/mocks/
# ---------------------------------------------------------------------------
from tests.mocks.mock_s3_client import MockS3Client, create_mock_s3_client
from tests.mocks.mock_search_engine import MockSearchEngine, create_mock_search_engine
from tests.mocks.mock_proxy_client import MockProxyClient, create_mock_proxy_client
from tests.mocks.mock_email_service import MockEmailService, create_mock_email_service

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
)


# ===========================================================================
# Module-level marker — automatically applies @pytest.mark.unit to ALL tests
# discovered under the tests/unit/ directory tree.
# ===========================================================================

pytestmark = pytest.mark.unit


# ===========================================================================
# Section 1: Mock External Service Client Fixtures
# ===========================================================================


@pytest.fixture
def mock_s3_client() -> MockS3Client:
    """Provide a fresh ``MockS3Client`` instance for S3 BlobStore unit tests.

    Creates an in-memory mock of the boto3 S3 client that supports
    ``put_object``, ``get_object``, ``delete_object``, ``list_objects_v2``,
    ``head_object``, ``create_bucket``, and ``delete_bucket`` operations.

    The client is automatically reset after the test completes, clearing
    all stored objects, call logs, and error configurations.

    Yields
    ------
    MockS3Client
        A freshly initialised mock S3 client ready for test use.
    """
    client = create_mock_s3_client()
    yield client
    client.reset()


@pytest.fixture
def mock_search_engine() -> MockSearchEngine:
    """Provide a fresh ``MockSearchEngine`` instance for search service tests.

    Creates an in-memory mock of an Elasticsearch-like search backend
    supporting ``create_index``, ``index``, ``search``, ``delete``,
    ``delete_index``, and ``bulk_index`` operations.

    The engine is automatically reset after the test completes.

    Yields
    ------
    MockSearchEngine
        A freshly initialised mock search engine ready for test use.
    """
    engine = create_mock_search_engine()
    yield engine
    engine.reset()


@pytest.fixture
def mock_proxy_client() -> MockProxyClient:
    """Provide a fresh ``MockProxyClient`` for upstream proxy registry tests.

    Creates a mock HTTP client that simulates responses from upstream
    registries (Maven Central, npm, Docker Hub, PyPI, NuGet, APT) with
    configurable status codes and payloads.

    The client is automatically reset after the test completes, clearing
    all registered responses, call logs, and error configurations.

    Yields
    ------
    MockProxyClient
        A freshly initialised mock proxy client ready for test use.
    """
    client = create_mock_proxy_client()
    yield client
    client.reset()


@pytest.fixture
def mock_email_service() -> MockEmailService:
    """Provide a fresh ``MockEmailService`` for webhook and alert tests.

    Creates a mock notification service that captures sent emails, webhooks,
    and alerts in-memory for assertion without performing actual dispatch.

    The service is automatically reset after the test completes.

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
    """Build a ``MagicMock`` that mimics SQLAlchemy's chainable query API.

    The returned mock supports method chaining such as::

        session.query(Model).filter(...).filter_by(...).order_by(...).first()

    Every chainable method (``filter``, ``filter_by``, ``join``, ``outerjoin``,
    ``group_by``, ``having``, ``order_by``, ``limit``, ``offset``,
    ``distinct``, ``subquery``) returns the query mock itself.  Terminal
    methods (``first``, ``all``, ``one_or_none``, ``one``, ``count``,
    ``scalar``, ``exists``, ``paginate``, ``delete``, ``update``,
    ``with_entities``) return fresh ``MagicMock`` instances.

    Returns
    -------
    MagicMock
        A mock object replicating the SQLAlchemy Query interface.
    """
    query_mock = MagicMock(name="QueryMock")

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
    """Provide a fully mocked SQLAlchemy session for unit tests.

    The mock session supports all common SQLAlchemy session operations
    (``add``, ``commit``, ``rollback``, ``query``, ``delete``, ``flush``,
    ``refresh``, ``close``, ``execute``, ``merge``, ``expunge``) with
    sensible defaults.

    The ``query()`` method returns a chainable mock supporting the full
    SQLAlchemy query builder pattern (``filter``, ``filter_by``, ``first``,
    ``all``, ``one_or_none``, ``count``, ``order_by``, ``limit``,
    ``offset``, ``paginate``).

    This is the **primary fixture** for unit tests needing to mock
    SQLAlchemy interactions.

    Returns
    -------
    MagicMock
        A mock simulating a ``db.session`` SQLAlchemy session.
    """
    session = MagicMock(name="MockDBSession")

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

    The mock reuses the ``mock_db_session`` fixture for its ``.session``
    attribute, and also provides stubs for ``create_all()``, ``drop_all()``,
    ``engine``, and ``Model``.

    Parameters
    ----------
    mock_db_session : MagicMock
        The mocked database session fixture, wired as ``db.session``.

    Returns
    -------
    MagicMock
        A mock simulating the ``flask_sqlalchemy.SQLAlchemy`` instance.
    """
    db_mock = MagicMock(name="MockDB")

    # Wire the shared session mock
    db_mock.session = mock_db_session

    # Schema management
    db_mock.create_all.return_value = None
    db_mock.drop_all.return_value = None

    # Engine mock with basic interface
    db_mock.engine = MagicMock(
        name="MockEngine",
        dispose=MagicMock(return_value=None),
        execute=MagicMock(return_value=MagicMock(fetchall=MagicMock(return_value=[]))),
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
# Section 3: Mock Application Service Layer Fixtures
# ===========================================================================


@pytest.fixture
def mock_repository_service() -> MagicMock:
    """Provide a mocked ``RepositoryService`` for unit tests.

    Simulates repository management operations without real database or
    storage interactions.  Every method returns a ``MagicMock`` by default;
    individual tests can override ``.return_value`` as needed.

    Returns
    -------
    MagicMock
        A mock simulating the ``RepositoryService`` interface.
    """
    service = MagicMock(name="MockRepositoryService")

    # Core CRUD operations
    service.create_repository.return_value = MagicMock(name="CreatedRepository")
    service.get_repository.return_value = MagicMock(name="FetchedRepository")
    service.update_repository.return_value = MagicMock(name="UpdatedRepository")
    service.delete_repository.return_value = True

    # Query / listing operations
    service.list_repositories.return_value = []
    service.get_repository_by_name.return_value = None

    # Format-specific operations
    service.get_repository_formats.return_value = [
        "maven", "npm", "docker", "nuget", "pypi", "apt", "raw",
    ]
    service.validate_repository_config.return_value = True

    return service


@pytest.fixture
def mock_storage_service() -> MagicMock:
    """Provide a mocked ``StorageService`` for unit tests.

    Simulates artifact upload, download, deletion, and metadata retrieval
    without real file system or S3 interactions.

    Returns
    -------
    MagicMock
        A mock simulating the ``StorageService`` interface.
    """
    service = MagicMock(name="MockStorageService")

    # Artifact operations
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

    # Metadata / listing
    service.get_artifact_metadata.return_value = MagicMock(
        name="ArtifactMetadata",
        content_type="application/octet-stream",
        size=0,
    )
    service.list_artifacts.return_value = []

    # BlobStore management
    service.get_blob_store_info.return_value = MagicMock(name="BlobStoreInfo")
    service.check_storage_health.return_value = True

    return service


@pytest.fixture
def mock_security_service() -> MagicMock:
    """Provide a mocked ``SecurityService`` for unit tests.

    Simulates authentication (JWT, API key), authorization (RBAC), and
    session management without real cryptographic operations or database
    lookups.

    Returns
    -------
    MagicMock
        A mock simulating the ``SecurityService`` interface.
    """
    service = MagicMock(name="MockSecurityService")

    # Authentication operations
    service.authenticate.return_value = MagicMock(
        name="AuthResult",
        authenticated=True,
        user_id="test-user-id",
    )
    service.validate_token.return_value = MagicMock(
        name="TokenValidation",
        valid=True,
        user_id="test-user-id",
    )
    service.generate_token.return_value = "mock-jwt-token-string"

    # Authorization operations
    service.authorize.return_value = True
    service.check_privilege.return_value = True

    # API key operations
    service.create_api_key.return_value = MagicMock(
        name="ApiKeyResult",
        key="test-api-key-abc123",
        key_id="key-id-001",
    )
    service.revoke_api_key.return_value = True
    service.validate_api_key.return_value = MagicMock(
        name="ApiKeyValidation",
        valid=True,
        user_id="test-user-id",
    )

    # Session management
    service.create_session.return_value = MagicMock(name="SessionData")
    service.invalidate_session.return_value = True

    return service


@pytest.fixture
def mock_search_service() -> MagicMock:
    """Provide a mocked ``SearchService`` for unit tests.

    Simulates content indexing, full-text search, and index management
    without a running search cluster.

    Returns
    -------
    MagicMock
        A mock simulating the ``SearchService`` interface.
    """
    service = MagicMock(name="MockSearchService")

    # Indexing operations
    service.index_component.return_value = True
    service.bulk_index.return_value = MagicMock(
        name="BulkIndexResult",
        indexed=0,
        errors=[],
    )
    service.delete_from_index.return_value = True

    # Search / query operations
    service.search.return_value = MagicMock(
        name="SearchResults",
        items=[],
        total=0,
        page=1,
        per_page=20,
    )
    service.rebuild_index.return_value = True

    return service


@pytest.fixture
def mock_scheduler_service() -> MagicMock:
    """Provide a mocked ``SchedulerService`` for unit tests.

    Simulates task scheduling, cancellation, status queries, and manual
    execution without running background threads or cron systems.

    Returns
    -------
    MagicMock
        A mock simulating the ``SchedulerService`` interface.
    """
    service = MagicMock(name="MockSchedulerService")

    # Task lifecycle operations
    service.schedule_task.return_value = MagicMock(
        name="ScheduledTask",
        task_id="task-001",
        status="scheduled",
    )
    service.cancel_task.return_value = True
    service.get_task_status.return_value = MagicMock(
        name="TaskStatus",
        task_id="task-001",
        status="idle",
        last_run=None,
    )
    service.run_task.return_value = MagicMock(
        name="TaskExecution",
        task_id="task-001",
        status="completed",
    )

    # Listing
    service.list_tasks.return_value = []
    service.get_task_by_id.return_value = None

    return service


@pytest.fixture
def mock_audit_service() -> MagicMock:
    """Provide a mocked ``AuditService`` for unit tests.

    Simulates audit log creation, retrieval, and filtering without real
    database persistence.

    Returns
    -------
    MagicMock
        A mock simulating the ``AuditService`` interface.
    """
    service = MagicMock(name="MockAuditService")

    # Audit event operations
    service.log_event.return_value = MagicMock(
        name="AuditEvent",
        event_id="audit-001",
        timestamp="2025-01-01T00:00:00Z",
    )
    service.get_events.return_value = MagicMock(
        name="AuditEventList",
        items=[],
        total=0,
    )
    service.get_event_by_id.return_value = None

    # Retention / cleanup
    service.purge_old_events.return_value = 0
    service.export_events.return_value = b""

    return service


@pytest.fixture
def mock_webhook_service() -> MagicMock:
    """Provide a mocked ``WebhookService`` for unit tests.

    Simulates webhook registration, dispatch, listing, and deletion
    without real HTTP callbacks.

    Returns
    -------
    MagicMock
        A mock simulating the ``WebhookService`` interface.
    """
    service = MagicMock(name="MockWebhookService")

    # Registration operations
    service.register_webhook.return_value = MagicMock(
        name="RegisteredWebhook",
        webhook_id="webhook-001",
        url="https://example.com/hook",
        active=True,
    )
    service.delete_webhook.return_value = True

    # Dispatch operations
    service.dispatch.return_value = MagicMock(
        name="DispatchResult",
        success=True,
        status_code=200,
    )

    # Listing / query operations
    service.list_webhooks.return_value = []
    service.get_webhook_by_id.return_value = None

    # Retry / configuration
    service.update_webhook.return_value = MagicMock(name="UpdatedWebhook")
    service.test_webhook.return_value = MagicMock(
        name="TestResult",
        success=True,
    )

    return service


@pytest.fixture
def mock_health_service() -> MagicMock:
    """Provide a mocked ``HealthService`` for unit tests.

    Simulates health check aggregation, component status reporting, and
    system information retrieval without probing real services.

    Returns
    -------
    MagicMock
        A mock simulating the ``HealthService`` interface.
    """
    service = MagicMock(name="MockHealthService")

    # Health check operations
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

    # Readiness / liveness probes
    service.is_ready.return_value = True
    service.is_alive.return_value = True

    return service


# ===========================================================================
# Section 4: Test Data Convenience Fixtures
# ===========================================================================


@pytest.fixture
def sample_admin_user() -> dict:
    """Provide a pre-built admin user data dictionary.

    Convenience wrapper around ``make_admin_user()`` from the user data
    fixture factory module.  Returns a fresh user dict on every invocation,
    ensuring test isolation.

    Returns
    -------
    dict
        An admin user configuration dictionary with full system privileges.
    """
    return make_admin_user()


@pytest.fixture
def sample_developer_user() -> dict:
    """Provide a pre-built developer user data dictionary.

    Convenience wrapper around ``make_developer_user()``.

    Returns
    -------
    dict
        A developer user configuration dictionary with read/write privileges.
    """
    return make_developer_user()


@pytest.fixture
def sample_readonly_user() -> dict:
    """Provide a pre-built read-only user data dictionary.

    Convenience wrapper around ``make_readonly_user()``.

    Returns
    -------
    dict
        A read-only user configuration dictionary with browse/search only.
    """
    return make_readonly_user()


@pytest.fixture
def sample_hosted_repo() -> dict:
    """Provide a pre-built hosted repository configuration dictionary.

    Convenience wrapper around ``make_hosted_repo()``.

    Returns
    -------
    dict
        A hosted repository configuration with storage, cleanup, and
        component settings populated.
    """
    return make_hosted_repo()


@pytest.fixture
def sample_proxy_repo() -> dict:
    """Provide a pre-built proxy repository configuration dictionary.

    Convenience wrapper around ``make_proxy_repo()``.

    Returns
    -------
    dict
        A proxy repository configuration with upstream URL, caching
        parameters, and HTTP client settings populated.
    """
    return make_proxy_repo()


@pytest.fixture
def sample_group_repo() -> dict:
    """Provide a pre-built group repository configuration dictionary.

    Convenience wrapper around ``make_group_repo()``.

    Returns
    -------
    dict
        A group repository configuration with member aggregation settings.
    """
    return make_group_repo()
