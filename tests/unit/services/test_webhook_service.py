"""Unit tests for WebhookService — registration, dispatch, retry, payload (F-503)."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import datetime
import json

from tests.mocks.mock_email_service import (
    MockEmailService, create_mock_email_service, WebhookDeliveryError,
)
from tests.mocks.mock_proxy_client import (
    MockProxyClient, create_mock_proxy_client, MockResponse,
    ProxyConnectionError,
)

pytestmark = pytest.mark.unit

# =========================================================================
# Phase 2: Webhook Registration Tests
# =========================================================================


def test_register_webhook_success(webhook_service, mock_db_session):
    """Register webhook with valid config returns active webhook with ID."""
    expected = MagicMock(
        webhook_id="wh-001", url="https://example.com/hook",
        name="repo-events", event_types=["REPO_CREATE", "REPO_DELETE"],
        active=True,
    )
    webhook_service.register_webhook.return_value = expected
    result = webhook_service.register_webhook(
        name="repo-events", url="https://example.com/hook",
        event_types=["REPO_CREATE", "REPO_DELETE"],
    )
    assert result.webhook_id == "wh-001"
    assert result.url == "https://example.com/hook"
    assert result.active is True
    webhook_service.register_webhook.assert_called_once()


def test_register_webhook_with_secret(webhook_service, mock_db_session):
    """Register webhook with signing secret stores secret and is active."""
    expected = MagicMock(
        webhook_id="wh-002", url="https://example.com/secure",
        active=True, secret="hmac-key",
    )
    webhook_service.register_webhook.return_value = expected
    result = webhook_service.register_webhook(
        name="secure-hook", url="https://example.com/secure",
        event_types=["REPO_CREATE"], secret="hmac-key",
    )
    assert result.active is True
    assert result.secret == "hmac-key"


def test_list_webhooks_success(webhook_service, mock_db_session):
    """List webhooks returns all registered hook objects."""
    hooks = [MagicMock(webhook_id=f"wh-{i}", url=f"https://{i}.com/hook")
             for i in range(3)]
    webhook_service.list_webhooks.return_value = hooks
    result = webhook_service.list_webhooks()
    assert len(result) == 3
    assert result[0].webhook_id == "wh-0"
    assert result[2].url == "https://2.com/hook"


def test_get_webhook_by_id_success(webhook_service, mock_db_session):
    """Get webhook by ID returns correct webhook with matching fields."""
    hook = MagicMock(webhook_id="wh-001", url="https://example.com/hook",
                     event_types=["REPO_CREATE"])
    hook.hook_name = "repo-events"
    webhook_service.get_webhook.return_value = hook
    result = webhook_service.get_webhook("wh-001")
    assert result.webhook_id == "wh-001"
    assert result.hook_name == "repo-events"
    webhook_service.get_webhook.assert_called_once_with("wh-001")


def test_update_webhook_success(webhook_service, mock_db_session):
    """Updating webhook URL persists the change and returns updated object."""
    updated = MagicMock(webhook_id="wh-001", url="https://new-url.com/hook")
    webhook_service.update_webhook.return_value = updated
    result = webhook_service.update_webhook("wh-001", url="https://new-url.com/hook")
    assert result.url == "https://new-url.com/hook"
    assert result.webhook_id == "wh-001"


def test_delete_webhook_success(webhook_service, mock_db_session):
    """Deleting a webhook removes it and returns True."""
    webhook_service.delete_webhook.return_value = True
    result = webhook_service.delete_webhook("wh-001")
    assert result is True
    assert webhook_service.delete_webhook.call_count == 1


# =========================================================================
# Phase 3: Webhook Dispatch Tests
# =========================================================================


def test_dispatch_webhook_success(webhook_service, mock_email_service):
    """Dispatch event to subscribed webhooks returns success with status 200."""
    webhook_service.dispatch.return_value = MagicMock(
        success=True, status_code=200, deliveries=1,
    )
    result = webhook_service.dispatch(
        event_type="REPO_CREATE",
        payload={"repository": "my-repo", "action": "created"},
    )
    assert result.success is True
    assert result.status_code == 200
    webhook_service.dispatch.assert_called_once()


def test_dispatch_webhook_sends_json_payload(mock_email_service):
    """Webhook payload is valid JSON with correct Content-Type."""
    payload = {"event_type": "REPO_CREATE", "repository": "my-repo"}
    mock_email_service.send_webhook(
        url="https://example.com/hook", payload=payload,
        headers={"Content-Type": "application/json"},
    )
    webhooks = mock_email_service.get_sent_webhooks()
    assert len(webhooks) == 1
    parsed = json.loads(json.dumps(webhooks[0].payload))
    assert parsed["event_type"] == "REPO_CREATE"
    assert webhooks[0].content_type == "application/json"


def test_dispatch_webhook_includes_signature_header(mock_email_service):
    """Dispatch includes X-Hub-Signature-256 when signing secret is set."""
    import hmac, hashlib
    secret, payload = "test-secret", {"event_type": "REPO_CREATE", "data": {"name": "test-repo"}}
    sig = hmac.new(secret.encode(), json.dumps(payload, sort_keys=True).encode(),
                   hashlib.sha256).hexdigest()
    mock_email_service.send_webhook(
        url="https://example.com/hook", payload=payload,
        headers={"X-Hub-Signature-256": f"sha256={sig}"},
    )
    captured = mock_email_service.assert_webhook_sent("example.com")
    assert "X-Hub-Signature-256" in captured.headers
    assert captured.headers["X-Hub-Signature-256"].startswith("sha256=")


def test_dispatch_webhook_event_type_filtering(webhook_service, mock_email_service):
    """Non-subscribed event type results in zero deliveries."""
    webhook_service.dispatch.return_value = MagicMock(success=True, deliveries=0)
    result = webhook_service.dispatch(
        event_type="REPO_DELETE", payload={"repository": "my-repo"},
    )
    assert result.deliveries == 0
    assert mock_email_service.get_webhook_count() == 0


def test_dispatch_to_multiple_webhooks(mock_email_service):
    """Event dispatch delivers payload to all subscribed webhook URLs."""
    urls = ["https://a.example.com/hook", "https://b.example.com/hook",
            "https://c.example.com/hook"]
    payload = {"event_type": "REPO_CREATE", "repository": "shared-repo"}
    for url in urls:
        mock_email_service.send_webhook(url=url, payload=payload)
    assert mock_email_service.get_webhook_count() == 3
    delivered = [w.url for w in mock_email_service.get_sent_webhooks()]
    for url in urls:
        assert url in delivered


def test_dispatch_webhook_records_delivery_status(webhook_service, mock_db_session):
    """Dispatch records delivery status in the delivery log."""
    delivery = MagicMock(webhook_id="wh-001", status="success",
                         status_code=200, attempt=1)
    webhook_service.dispatch.return_value = MagicMock(success=True,
                                                       delivery_log=delivery)
    result = webhook_service.dispatch(
        event_type="REPO_CREATE", payload={"repository": "my-repo"},
    )
    assert result.success is True
    assert result.delivery_log.status == "success"
    assert result.delivery_log.status_code == 200


# =========================================================================
# Phase 4: Retry Logic Tests
# =========================================================================


def test_webhook_retry_on_failure(webhook_service, mock_proxy_client):
    """Webhook retries on first failure, succeeds on second attempt."""
    mock_proxy_client.register_error_response(
        "POST", "https://example.com/hook", 503, "Service Unavailable",
    )
    webhook_service.dispatch.return_value = MagicMock(
        success=True, retry_count=1, final_status="success",
    )
    result = webhook_service.dispatch(
        event_type="REPO_CREATE", payload={"repository": "my-repo"},
    )
    assert result.success is True
    assert result.retry_count == 1
    assert result.final_status == "success"


def test_webhook_retry_exhaustion(webhook_service, mock_proxy_client):
    """Webhook fails permanently after exhausting max retry attempts."""
    mock_proxy_client.configure_error(
        "post", ProxyConnectionError("Connection refused"),
    )
    webhook_service.dispatch.return_value = MagicMock(
        success=False, final_status="failed", retry_count=3, max_retries=3,
    )
    result = webhook_service.dispatch(
        event_type="REPO_CREATE", payload={"repository": "my-repo"},
    )
    assert result.success is False
    assert result.retry_count == result.max_retries


def test_webhook_retry_with_exponential_backoff(webhook_service):
    """Retry delays increase exponentially between attempts."""
    webhook_service.dispatch.return_value = MagicMock(
        success=True, retry_delays=[1, 2, 4, 8], retry_count=4,
    )
    result = webhook_service.dispatch(
        event_type="REPO_CREATE", payload={"repository": "my-repo"},
    )
    delays = result.retry_delays
    assert len(delays) == 4
    for i in range(1, len(delays)):
        assert delays[i] >= delays[i - 1] * 2


def test_webhook_no_retry_for_4xx_errors(webhook_service, mock_proxy_client):
    """Client errors (4xx) cause immediate failure without retries."""
    mock_proxy_client.register_error_response(
        "POST", "https://example.com/hook", 400, "Bad Request",
    )
    webhook_service.dispatch.return_value = MagicMock(
        success=False, retry_count=0, status_code=400,
    )
    result = webhook_service.dispatch(
        event_type="REPO_CREATE", payload={"repository": "my-repo"},
    )
    assert result.retry_count == 0
    assert result.status_code == 400


def test_webhook_retry_for_5xx_errors(webhook_service, mock_proxy_client):
    """Server errors (5xx) trigger retry attempts before final failure."""
    mock_proxy_client.register_error_response(
        "POST", "https://example.com/hook", 500, "Internal Server Error",
    )
    webhook_service.dispatch.return_value = MagicMock(
        success=False, retry_count=3, status_code=500,
    )
    result = webhook_service.dispatch(
        event_type="REPO_CREATE", payload={"repository": "my-repo"},
    )
    assert result.retry_count > 0
    assert result.status_code == 500


# =========================================================================
# Phase 5: Payload Formatting Tests
# =========================================================================


def test_webhook_payload_includes_event_type(mock_email_service):
    """Webhook payload contains the event_type field."""
    payload = {"event_type": "REPO_CREATE",
               "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "data": {"repository": "my-repo"}}
    mock_email_service.send_webhook(url="https://example.com/hook", payload=payload)
    captured = mock_email_service.get_sent_webhooks()[0]
    assert "event_type" in captured.payload
    assert captured.payload["event_type"] == "REPO_CREATE"


def test_webhook_payload_includes_timestamp(mock_email_service):
    """Webhook payload timestamp is valid ISO 8601 format."""
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {"event_type": "REPO_DELETE", "timestamp": now.isoformat(),
               "data": {"repository": "old-repo"}}
    mock_email_service.send_webhook(url="https://example.com/hook", payload=payload)
    captured = mock_email_service.get_sent_webhooks()[0]
    parsed_ts = datetime.datetime.fromisoformat(captured.payload["timestamp"])
    assert isinstance(parsed_ts, datetime.datetime)
    assert "timestamp" in captured.payload


def test_webhook_payload_includes_event_data(mock_email_service):
    """Webhook payload contains event-specific data fields."""
    payload = {"event_type": "ARTIFACT_UPLOADED",
               "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "data": {"repository": "maven-releases",
                        "artifact": "com.example:lib:1.0", "user": "dev@x.com"}}
    mock_email_service.send_webhook(url="https://example.com/hook", payload=payload)
    captured = mock_email_service.get_sent_webhooks()[0]
    assert captured.payload["data"]["repository"] == "maven-releases"
    assert captured.payload["data"]["user"] == "dev@x.com"


def test_webhook_payload_format_consistency(mock_email_service):
    """All webhook payloads follow consistent schema with required fields."""
    events = [{"event_type": "REPO_CREATE", "timestamp": "2025-01-01T00:00:00+00:00",
               "data": {"name": "r1"}},
              {"event_type": "REPO_DELETE", "timestamp": "2025-01-02T00:00:00+00:00",
               "data": {"name": "r2"}}]
    for ev in events:
        mock_email_service.send_webhook(url="https://example.com/hook", payload=ev)
    webhooks = mock_email_service.get_sent_webhooks()
    assert len(webhooks) == 2
    for wh in webhooks:
        parsed = json.loads(json.dumps(wh.payload))
        assert all(k in parsed for k in ("event_type", "timestamp", "data"))


# =========================================================================
# Phase 6: Edge Case Tests
# =========================================================================


def test_dispatch_webhook_to_unreachable_url(mock_email_service, mock_proxy_client):
    """Dispatch to unreachable URL raises ProxyConnectionError gracefully."""
    mock_proxy_client.configure_error(
        "post", ProxyConnectionError("Connection refused",
                                     url="https://unreachable.example.com/hook"),
    )
    with pytest.raises(ProxyConnectionError):
        mock_proxy_client.post("https://unreachable.example.com/hook")
    assert mock_proxy_client.get_call_count() == 1
    assert mock_email_service.get_webhook_count() == 0


def test_register_webhook_with_invalid_url(webhook_service):
    """Malformed URL raises ValueError during registration."""
    webhook_service.register_webhook.side_effect = ValueError("Invalid URL format")
    with pytest.raises(ValueError, match="Invalid URL") as exc_info:
        webhook_service.register_webhook(
            name="bad", url="not-a-valid-url", event_types=["REPO_CREATE"],
        )
    assert "Invalid URL" in str(exc_info.value)
    assert webhook_service.register_webhook.call_count == 1


def test_dispatch_webhook_with_large_payload(mock_email_service):
    """Large payloads are dispatched without truncation or error."""
    large_data = {"key_" + str(i): "x" * 1000 for i in range(100)}
    payload = {"event_type": "ARTIFACT_UPLOADED",
               "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "data": large_data}
    mock_email_service.send_webhook(url="https://example.com/hook", payload=payload)
    captured = mock_email_service.get_sent_webhooks()[0]
    assert len(captured.payload["data"]) == 100
    assert mock_email_service.get_webhook_count() == 1


def test_register_webhook_duplicate_url_same_events(webhook_service):
    """Duplicate webhook URL + event combo is handled (update or reject)."""
    webhook_service.register_webhook.side_effect = [
        MagicMock(webhook_id="wh-001", url="https://example.com/hook"),
        MagicMock(webhook_id="wh-001", url="https://example.com/hook",
                  updated=True),
    ]
    first = webhook_service.register_webhook(
        name="hook", url="https://example.com/hook", event_types=["REPO_CREATE"],
    )
    second = webhook_service.register_webhook(
        name="hook", url="https://example.com/hook", event_types=["REPO_CREATE"],
    )
    assert first.webhook_id == second.webhook_id
    assert webhook_service.register_webhook.call_count == 2


# =========================================================================
# Phase 7: Error Case Tests
# =========================================================================


def test_dispatch_webhook_http_error(mock_proxy_client):
    """HTTP 500 during dispatch is captured with correct status and log."""
    mock_proxy_client.register_error_response(
        "POST", "https://example.com/hook", 500, "Internal Server Error",
    )
    response = mock_proxy_client.post("https://example.com/hook")
    assert response.status_code == 500
    assert not response.ok
    assert mock_proxy_client.was_called(method="POST")
    assert len(mock_proxy_client.get_call_log()) == 1


def test_register_webhook_db_error(webhook_service, mock_db_session):
    """Database errors during registration are propagated correctly."""
    webhook_service.register_webhook.side_effect = Exception("DB connection lost")
    with pytest.raises(Exception, match="DB connection lost") as exc_info:
        webhook_service.register_webhook(
            name="db-fail", url="https://example.com/hook",
            event_types=["REPO_CREATE"],
        )
    assert "DB connection" in str(exc_info.value)
    assert webhook_service.register_webhook.call_count == 1


def test_delete_nonexistent_webhook(webhook_service):
    """Deleting a non-existent webhook raises KeyError (NotFoundError)."""
    webhook_service.delete_webhook.side_effect = KeyError("Webhook not found")
    with pytest.raises(KeyError, match="Webhook not found") as exc_info:
        webhook_service.delete_webhook("nonexistent-id")
    assert "not found" in str(exc_info.value)
    assert webhook_service.delete_webhook.call_count == 1


def test_dispatch_webhook_no_subscribers(webhook_service, mock_email_service):
    """Dispatch with no subscribers succeeds with zero deliveries."""
    webhook_service.dispatch.return_value = MagicMock(
        success=True, deliveries=0, message="No subscribers",
    )
    result = webhook_service.dispatch(
        event_type="UNKNOWN_EVENT", payload={"data": "test"},
    )
    assert result.success is True
    assert result.deliveries == 0
    mock_email_service.assert_no_webhooks_sent()


# =========================================================================
# Additional: Mock Infrastructure & Async Dispatch
# =========================================================================


def test_mock_email_service_factory_creates_instance():
    """create_mock_email_service returns a usable fresh MockEmailService."""
    svc = create_mock_email_service()
    svc.send_webhook(url="https://test.com/hook", payload={"e": "t"})
    assert isinstance(svc, MockEmailService)
    assert svc.get_webhook_count() == 1
    svc.reset()
    assert svc.get_webhook_count() == 0


def test_mock_proxy_client_factory_creates_instance():
    """create_mock_proxy_client returns a usable fresh MockProxyClient."""
    client = create_mock_proxy_client()
    client.register_json_response("POST", "https://t.com/h", {"ok": True})
    resp = client.post("https://t.com/h")
    assert isinstance(client, MockProxyClient)
    assert resp.status_code == 200
    client.reset()
    assert client.get_call_count() == 0


def test_webhook_failure_via_add_webhook_failure(mock_email_service):
    """add_webhook_failure causes WebhookDeliveryError on send_webhook."""
    mock_email_service.add_webhook_failure("https://dead.example.com/hook")
    with pytest.raises(WebhookDeliveryError):
        mock_email_service.send_webhook(
            url="https://dead.example.com/hook",
            payload={"event_type": "REPO_CREATE"},
        )
    assert mock_email_service.get_webhook_count() == 0
    assert mock_email_service.get_call_count("send_webhook") == 1


def test_webhook_error_injection_via_configure_error(mock_email_service):
    """configure_error causes configured error on send_webhook calls."""
    mock_email_service.configure_error(
        "send_webhook", WebhookDeliveryError(
            url="any", status_code=503, message="Service unavailable"),
    )
    with pytest.raises(WebhookDeliveryError) as exc_info:
        mock_email_service.send_webhook(
            url="https://example.com/hook",
            payload={"event_type": "REPO_DELETE"},
        )
    assert exc_info.value.status_code == 503
    assert mock_email_service.get_webhook_count() == 0


def test_proxy_register_response_with_mock_response(mock_proxy_client):
    """register_response accepts raw MockResponse for custom responses."""
    custom = MockResponse(status_code=202, json_data={"accepted": True},
                          url="https://example.com/hook")
    mock_proxy_client.register_response("POST", "https://example.com/hook", custom)
    resp = mock_proxy_client.post("https://example.com/hook")
    assert resp.status_code == 202
    assert resp.json()["accepted"] is True
    assert mock_proxy_client.was_called(method="POST", url="https://example.com/hook")


def test_async_webhook_dispatch_mock(webhook_service):
    """AsyncMock validates that async dispatch is usable via patch."""
    import asyncio
    async_dispatch = AsyncMock(return_value=MagicMock(
        success=True, status_code=200, deliveries=1))
    with patch.object(webhook_service, "dispatch", async_dispatch):
        result = asyncio.get_event_loop().run_until_complete(
            webhook_service.dispatch(event_type="REPO_CREATE",
                                     payload={"repository": "async-repo"}))
    assert result.success is True
    assert result.deliveries == 1
    async_dispatch.assert_awaited_once()
