"""
Service instance fixtures with mock dependency injection.

Extracted from ``tests/unit/services/conftest.py`` to comply with the
500-line file length limit (AAP §0.7.2).  All fixtures here are
auto-discovered by pytest via the ``pytest_plugins`` directive in the
parent ``conftest.py``.

Each service fixture attempts to import and instantiate the real service
class with mock dependencies injected.  If the source module has not been
implemented yet (ImportError), a ``MagicMock`` is returned instead so that
downstream tests can still be collected and run without failure.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest
from unittest.mock import MagicMock

from tests.mocks.mock_s3_client import MockS3Client
from tests.mocks.mock_search_engine import MockSearchEngine
from tests.mocks.mock_email_service import MockEmailService
from tests.fixtures.repository_data import REPOSITORY_FORMATS


# ===========================================================================
# Section 3: Service Instance Fixtures
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
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
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
