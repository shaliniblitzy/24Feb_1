"""
Mock notification/email service for webhook and alert testing.

Provides a reusable mock notification service class that captures sent
notifications (emails, webhooks, alerts) for assertion without performing
actual dispatch. Used by webhook service unit tests, audit service tests,
and integration/functional tests involving notification dispatch.

All dispatched messages are stored in-memory for assertion in tests.
No real network calls, SMTP connections, or HTTP requests are made.

Typical usage::

    service = MockEmailService()
    service.send_email(recipients=["user@example.com"], subject="Test")
    service.assert_email_sent(recipient="user@example.com")

    # Error simulation
    service.configure_error("send_email", NotificationServiceError("SMTP down"))
    with pytest.raises(NotificationServiceError):
        service.send_email(recipients=["user@example.com"], subject="Fail")
"""

import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

NOTIFICATION_EMAIL: str = "email"
"""Constant identifying the email notification channel."""

NOTIFICATION_WEBHOOK: str = "webhook"
"""Constant identifying the webhook notification channel."""

NOTIFICATION_ALERT: str = "alert"
"""Constant identifying the alert notification channel."""

NOTIFICATION_SLACK: str = "slack"
"""Constant identifying the Slack notification channel (extensibility)."""

DEFAULT_SENDER: str = "noreply@binaryrepo.test"
"""Default sender address used when no explicit sender is provided."""


# ---------------------------------------------------------------------------
# Notification type enumeration
# ---------------------------------------------------------------------------

class NotificationType(Enum):
    """Enumeration of supported notification types for type-safe identification."""

    EMAIL = "email"
    WEBHOOK = "webhook"
    ALERT = "alert"
    SLACK = "slack"


# ---------------------------------------------------------------------------
# Custom exception hierarchy
# ---------------------------------------------------------------------------

class NotificationServiceError(Exception):
    """Base exception for all notification service failures.

    Attributes:
        message: Human-readable description of the error.
    """

    def __init__(self, message: str = "Notification service error") -> None:
        self.message = message
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r})"


class EmailDeliveryError(NotificationServiceError):
    """Raised when simulating email delivery failures.

    Attributes:
        recipient: The email address that delivery failed for.
        message: Human-readable description of the delivery failure.
    """

    def __init__(self, recipient: str, message: str = "Email delivery failed") -> None:
        self.recipient = recipient
        super().__init__(message)

    def __repr__(self) -> str:
        return (
            f"EmailDeliveryError(recipient={self.recipient!r}, "
            f"message={self.message!r})"
        )


class WebhookDeliveryError(NotificationServiceError):
    """Raised when simulating webhook delivery failures.

    Attributes:
        url: The webhook endpoint URL that delivery failed for.
        status_code: The HTTP status code received (or simulated).
        message: Human-readable description of the delivery failure.
    """

    def __init__(
        self,
        url: str,
        status_code: int = 500,
        message: str = "Webhook delivery failed",
    ) -> None:
        self.url = url
        self.status_code = status_code
        super().__init__(message)

    def __repr__(self) -> str:
        return (
            f"WebhookDeliveryError(url={self.url!r}, "
            f"status_code={self.status_code}, message={self.message!r})"
        )


# ---------------------------------------------------------------------------
# Captured message data classes
# ---------------------------------------------------------------------------

@dataclass
class CapturedEmail:
    """Structured record of a captured email message.

    Stores all parameters passed to ``send_email`` for later assertion in
    tests.  Instances are created internally by :class:`MockEmailService`
    and returned via ``get_sent_emails``, ``get_last_email``, and
    ``assert_email_sent``.
    """

    sender: str
    recipients: List[str]
    subject: str
    body: str
    html_body: Optional[str] = None
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class CapturedWebhook:
    """Structured record of a captured webhook dispatch.

    Stores all parameters passed to ``send_webhook`` for later assertion in
    tests.
    """

    url: str
    method: str
    payload: Dict[str, Any]
    headers: Dict[str, str] = field(default_factory=dict)
    content_type: str = "application/json"
    timestamp: datetime = field(default_factory=datetime.utcnow)
    retry_count: int = 0


@dataclass
class CapturedAlert:
    """Structured record of a captured alert notification.

    Stores all parameters passed to ``send_alert`` for later assertion in
    tests.
    """

    level: str
    message: str
    details: Optional[Dict[str, Any]] = None
    source: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# MockEmailService — primary mock class
# ---------------------------------------------------------------------------

class MockEmailService:
    """Mock notification service that captures all sent messages in memory.

    Supports email, webhook, and alert notification types.  All dispatched
    messages are stored for assertion in tests.  No real network calls, SMTP
    connections, or HTTP requests are made.

    The service supports:
    - **Stateful capture** of all sent notifications (emails, webhooks, alerts)
    - **Assertion helpers** for verifying notifications in tests
    - **Error injection** via ``configure_error`` and delivery failure lists
    - **Call logging** for all method invocations
    - **Context manager** protocol for automatic cleanup

    Example::

        svc = MockEmailService()
        svc.send_email(recipients=["dev@example.com"], subject="Deploy OK")
        assert svc.get_email_count() == 1
        captured = svc.assert_email_sent(recipient="dev@example.com")
        assert captured.subject == "Deploy OK"
    """

    VALID_ALERT_LEVELS = ("info", "warning", "error", "critical")
    """Allowed alert severity levels."""

    def __init__(self, **kwargs: Any) -> None:
        """Initialise the mock service with empty internal state.

        Args:
            **kwargs: Arbitrary keyword arguments stored as service
                configuration (e.g. ``default_sender``).
        """
        self._sent_emails: List[CapturedEmail] = []
        self._sent_webhooks: List[CapturedWebhook] = []
        self._sent_alerts: List[CapturedAlert] = []
        self._call_log: List[Dict[str, Any]] = []
        self._error_config: Dict[str, Exception] = {}
        self._delivery_failures: List[str] = []
        self._webhook_failures: List[str] = []
        self._config: Dict[str, Any] = dict(kwargs)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "MockEmailService":
        """Enter the context manager — returns *self*."""
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        """Exit the context manager — resets all captured state."""
        self.reset()

    # ------------------------------------------------------------------
    # Core dispatch methods
    # ------------------------------------------------------------------

    def send_email(
        self,
        sender: str = DEFAULT_SENDER,
        recipients: Optional[List[str]] = None,
        subject: str = "",
        body: str = "",
        html_body: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Simulate sending an email — captures the message without dispatch.

        Args:
            sender: The sender email address.
            recipients: List of recipient email addresses.
            subject: Email subject line.
            body: Plain-text email body.
            html_body: Optional HTML email body.
            attachments: Optional list of attachment dicts (name, content, mime).
            headers: Optional additional email headers.

        Returns:
            A dict with ``status``, ``message_id``, and ``recipients``.

        Raises:
            NotificationServiceError: If a general error is configured via
                :meth:`configure_error`.
            EmailDeliveryError: If any recipient is in the delivery failure
                list configured via :meth:`add_delivery_failure`.
        """
        resolved_recipients = recipients if recipients is not None else []
        resolved_attachments = attachments if attachments is not None else []
        resolved_headers = headers if headers is not None else {}

        # Record the call in the log
        self._record_call(
            "send_email",
            sender=sender,
            recipients=resolved_recipients,
            subject=subject,
            body=body,
            html_body=html_body,
            attachments=resolved_attachments,
            headers=resolved_headers,
        )

        # Check for configured general error
        self._check_error_config("send_email")

        # Check for per-recipient delivery failures
        for recipient in resolved_recipients:
            if recipient in self._delivery_failures:
                raise EmailDeliveryError(
                    recipient=recipient,
                    message=f"Simulated delivery failure for {recipient}",
                )

        # Capture the email
        captured = CapturedEmail(
            sender=sender,
            recipients=list(resolved_recipients),
            subject=subject,
            body=body,
            html_body=html_body,
            attachments=list(resolved_attachments),
            headers=dict(resolved_headers),
            timestamp=datetime.utcnow(),
        )
        self._sent_emails.append(captured)

        return {
            "status": "sent",
            "message_id": str(uuid.uuid4()),
            "recipients": list(resolved_recipients),
        }

    def send_webhook(
        self,
        url: str,
        payload: Dict[str, Any],
        method: str = "POST",
        headers: Optional[Dict[str, str]] = None,
        content_type: str = "application/json",
        retry_count: int = 0,
    ) -> Dict[str, Any]:
        """Simulate dispatching a webhook — captures without HTTP request.

        Args:
            url: The target webhook endpoint URL.
            payload: The JSON-serialisable payload dict.
            method: HTTP method (default ``POST``).
            headers: Optional additional HTTP headers.
            content_type: Content-Type header value.
            retry_count: Current retry attempt number.

        Returns:
            A dict with ``status``, ``url``, ``status_code``, and ``retry_count``.

        Raises:
            NotificationServiceError: If a general error is configured.
            WebhookDeliveryError: If the URL is in the webhook failure list.
        """
        resolved_headers = headers if headers is not None else {}

        self._record_call(
            "send_webhook",
            url=url,
            payload=payload,
            method=method,
            headers=resolved_headers,
            content_type=content_type,
            retry_count=retry_count,
        )

        self._check_error_config("send_webhook")

        if url in self._webhook_failures:
            raise WebhookDeliveryError(
                url=url,
                status_code=503,
                message=f"Simulated connection failure for {url}",
            )

        captured = CapturedWebhook(
            url=url,
            method=method,
            payload=copy.deepcopy(payload),
            headers=dict(resolved_headers),
            content_type=content_type,
            timestamp=datetime.utcnow(),
            retry_count=retry_count,
        )
        self._sent_webhooks.append(captured)

        return {
            "status": "delivered",
            "url": url,
            "status_code": 200,
            "retry_count": retry_count,
        }

    def send_alert(
        self,
        level: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        source: str = "",
    ) -> Dict[str, Any]:
        """Simulate sending an alert notification.

        Args:
            level: Alert severity — must be one of ``info``, ``warning``,
                ``error``, ``critical``.
            message: Alert message text.
            details: Optional structured details dict.
            source: Optional source identifier for the alert origin.

        Returns:
            A dict with ``status`` and ``level``.

        Raises:
            NotificationServiceError: If a general error is configured.
            ValueError: If *level* is not a recognised alert level.
        """
        self._record_call(
            "send_alert",
            level=level,
            message=message,
            details=details,
            source=source,
        )

        self._check_error_config("send_alert")

        if level not in self.VALID_ALERT_LEVELS:
            raise ValueError(
                f"Invalid alert level '{level}'. "
                f"Must be one of: {', '.join(self.VALID_ALERT_LEVELS)}"
            )

        captured = CapturedAlert(
            level=level,
            message=message,
            details=copy.deepcopy(details) if details else None,
            source=source,
            timestamp=datetime.utcnow(),
        )
        self._sent_alerts.append(captured)

        return {"status": "recorded", "level": level}

    # ------------------------------------------------------------------
    # Query / getter methods
    # ------------------------------------------------------------------

    def get_sent_emails(
        self,
        recipient: Optional[str] = None,
        subject_contains: Optional[str] = None,
    ) -> List[CapturedEmail]:
        """Return a filtered list of captured emails.

        Args:
            recipient: If provided, only emails where this address appears
                in the recipients list are returned.
            subject_contains: If provided, only emails whose subject
                contains this substring are returned.

        Returns:
            Deep copies of matching :class:`CapturedEmail` instances.
        """
        results = list(self._sent_emails)
        if recipient is not None:
            results = [e for e in results if recipient in e.recipients]
        if subject_contains is not None:
            results = [e for e in results if subject_contains in e.subject]
        return [copy.deepcopy(e) for e in results]

    def get_sent_webhooks(
        self,
        url_contains: Optional[str] = None,
    ) -> List[CapturedWebhook]:
        """Return a filtered list of captured webhook dispatches.

        Args:
            url_contains: If provided, only webhooks whose URL contains
                this substring are returned.

        Returns:
            Deep copies of matching :class:`CapturedWebhook` instances.
        """
        results = list(self._sent_webhooks)
        if url_contains is not None:
            results = [w for w in results if url_contains in w.url]
        return [copy.deepcopy(w) for w in results]

    def get_sent_alerts(
        self,
        level: Optional[str] = None,
    ) -> List[CapturedAlert]:
        """Return a filtered list of captured alert notifications.

        Args:
            level: If provided, only alerts with this severity level are
                returned.

        Returns:
            Deep copies of matching :class:`CapturedAlert` instances.
        """
        results = list(self._sent_alerts)
        if level is not None:
            results = [a for a in results if a.level == level]
        return [copy.deepcopy(a) for a in results]

    def get_email_count(self) -> int:
        """Return the number of captured emails."""
        return len(self._sent_emails)

    def get_webhook_count(self) -> int:
        """Return the number of captured webhook dispatches."""
        return len(self._sent_webhooks)

    def get_alert_count(self) -> int:
        """Return the number of captured alert notifications."""
        return len(self._sent_alerts)

    def get_total_notification_count(self) -> int:
        """Return the total count across all notification types."""
        return (
            self.get_email_count()
            + self.get_webhook_count()
            + self.get_alert_count()
        )

    def get_last_email(self) -> Optional[CapturedEmail]:
        """Return the most recently captured email, or ``None``."""
        if not self._sent_emails:
            return None
        return copy.deepcopy(self._sent_emails[-1])

    def get_last_webhook(self) -> Optional[CapturedWebhook]:
        """Return the most recently captured webhook, or ``None``."""
        if not self._sent_webhooks:
            return None
        return copy.deepcopy(self._sent_webhooks[-1])

    def get_last_alert(self) -> Optional[CapturedAlert]:
        """Return the most recently captured alert, or ``None``."""
        if not self._sent_alerts:
            return None
        return copy.deepcopy(self._sent_alerts[-1])

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    def assert_email_sent(
        self,
        recipient: str,
        subject_contains: Optional[str] = None,
    ) -> CapturedEmail:
        """Assert that at least one email was sent to *recipient*.

        Args:
            recipient: The expected recipient address.
            subject_contains: Optional substring that must appear in the
                email subject.

        Returns:
            The first matching :class:`CapturedEmail`.

        Raises:
            AssertionError: If no matching email was found.
        """
        matches = self.get_sent_emails(
            recipient=recipient,
            subject_contains=subject_contains,
        )
        if not matches:
            sent_summary = (
                f"Emails sent ({len(self._sent_emails)}): "
                + ", ".join(
                    f"to={e.recipients!r} subj={e.subject!r}"
                    for e in self._sent_emails
                )
                if self._sent_emails
                else "No emails sent"
            )
            subject_info = (
                f" with subject containing '{subject_contains}'"
                if subject_contains
                else ""
            )
            raise AssertionError(
                f"Expected email to '{recipient}'{subject_info} "
                f"but none found. {sent_summary}"
            )
        return matches[0]

    def assert_webhook_sent(
        self,
        url_contains: str,
    ) -> CapturedWebhook:
        """Assert that at least one webhook was dispatched to a matching URL.

        Args:
            url_contains: Substring that must appear in the webhook URL.

        Returns:
            The first matching :class:`CapturedWebhook`.

        Raises:
            AssertionError: If no matching webhook was found.
        """
        matches = self.get_sent_webhooks(url_contains=url_contains)
        if not matches:
            sent_summary = (
                f"Webhooks sent ({len(self._sent_webhooks)}): "
                + ", ".join(w.url for w in self._sent_webhooks)
                if self._sent_webhooks
                else "No webhooks sent"
            )
            raise AssertionError(
                f"Expected webhook to URL containing '{url_contains}' "
                f"but none found. {sent_summary}"
            )
        return matches[0]

    def assert_alert_sent(
        self,
        level: str,
        message_contains: Optional[str] = None,
    ) -> CapturedAlert:
        """Assert that at least one alert was sent with the given *level*.

        Args:
            level: The expected alert severity level.
            message_contains: Optional substring that must appear in the
                alert message.

        Returns:
            The first matching :class:`CapturedAlert`.

        Raises:
            AssertionError: If no matching alert was found.
        """
        candidates = self.get_sent_alerts(level=level)
        if message_contains is not None:
            candidates = [
                a for a in candidates if message_contains in a.message
            ]
        if not candidates:
            sent_summary = (
                f"Alerts sent ({len(self._sent_alerts)}): "
                + ", ".join(
                    f"level={a.level} msg={a.message!r}"
                    for a in self._sent_alerts
                )
                if self._sent_alerts
                else "No alerts sent"
            )
            msg_info = (
                f" with message containing '{message_contains}'"
                if message_contains
                else ""
            )
            raise AssertionError(
                f"Expected alert with level '{level}'{msg_info} "
                f"but none found. {sent_summary}"
            )
        return candidates[0]

    def assert_no_emails_sent(self) -> None:
        """Assert that no emails have been sent.

        Raises:
            AssertionError: If any emails have been captured.
        """
        if self._sent_emails:
            summary = ", ".join(
                f"to={e.recipients!r} subj={e.subject!r}"
                for e in self._sent_emails
            )
            raise AssertionError(
                f"Expected no emails sent but found {len(self._sent_emails)}: "
                f"{summary}"
            )

    def assert_no_webhooks_sent(self) -> None:
        """Assert that no webhooks have been dispatched.

        Raises:
            AssertionError: If any webhooks have been captured.
        """
        if self._sent_webhooks:
            summary = ", ".join(w.url for w in self._sent_webhooks)
            raise AssertionError(
                f"Expected no webhooks sent but found "
                f"{len(self._sent_webhooks)}: {summary}"
            )

    def assert_no_notifications_sent(self) -> None:
        """Assert that no notifications of any type have been sent.

        Raises:
            AssertionError: If any notifications have been captured.
        """
        total = self.get_total_notification_count()
        if total > 0:
            raise AssertionError(
                f"Expected no notifications sent but found {total} "
                f"(emails={self.get_email_count()}, "
                f"webhooks={self.get_webhook_count()}, "
                f"alerts={self.get_alert_count()})"
            )

    # ------------------------------------------------------------------
    # Error configuration methods
    # ------------------------------------------------------------------

    def configure_error(self, method_name: str, error: Exception) -> None:
        """Configure an exception to be raised when *method_name* is called.

        This is used to simulate SMTP failures, webhook delivery failures,
        and other service errors during testing.

        Args:
            method_name: The method name (e.g. ``'send_email'``).
            error: The exception instance to raise.
        """
        self._error_config[method_name] = error

    def clear_error(self, method_name: str) -> None:
        """Remove the configured error for *method_name*.

        Does nothing if no error is configured for the given method.

        Args:
            method_name: The method name to clear the error for.
        """
        self._error_config.pop(method_name, None)

    def clear_all_errors(self) -> None:
        """Remove all configured errors."""
        self._error_config.clear()

    def add_delivery_failure(self, recipient: str) -> None:
        """Add a recipient that will trigger an :class:`EmailDeliveryError`.

        When ``send_email`` is called with this recipient in the recipients
        list, an :class:`EmailDeliveryError` will be raised, simulating an
        undeliverable address.

        Args:
            recipient: The email address to mark as undeliverable.
        """
        if recipient not in self._delivery_failures:
            self._delivery_failures.append(recipient)

    def add_webhook_failure(self, url: str) -> None:
        """Add a URL that will trigger a :class:`WebhookDeliveryError`.

        When ``send_webhook`` is called with this URL, a
        :class:`WebhookDeliveryError` will be raised, simulating an
        unreachable webhook endpoint.

        Args:
            url: The webhook URL to mark as unreachable.
        """
        if url not in self._webhook_failures:
            self._webhook_failures.append(url)

    def clear_delivery_failures(self) -> None:
        """Clear all configured email delivery failures and webhook failures."""
        self._delivery_failures.clear()
        self._webhook_failures.clear()

    # ------------------------------------------------------------------
    # Call logging methods
    # ------------------------------------------------------------------

    def get_call_log(self) -> List[Dict[str, Any]]:
        """Return a deep copy of the complete call log.

        Each entry is a dict with keys ``method``, ``args``, ``kwargs``,
        and ``timestamp``.

        Returns:
            List of call log entry dicts.
        """
        return copy.deepcopy(self._call_log)

    def get_call_count(self, method_name: Optional[str] = None) -> int:
        """Return the number of recorded method calls.

        Args:
            method_name: If provided, count only calls to this method.
                If ``None``, return the total call count.

        Returns:
            The number of matching calls.
        """
        if method_name is None:
            return len(self._call_log)
        return sum(1 for c in self._call_log if c["method"] == method_name)

    # ------------------------------------------------------------------
    # Reset / lifecycle
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset the service to its initial empty state.

        Clears all captured messages (emails, webhooks, alerts), call logs,
        error configurations, and failure lists.
        """
        self._sent_emails.clear()
        self._sent_webhooks.clear()
        self._sent_alerts.clear()
        self._call_log.clear()
        self._error_config.clear()
        self._delivery_failures.clear()
        self._webhook_failures.clear()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _record_call(self, method_name: str, **kwargs: Any) -> None:
        """Append an entry to the internal call log.

        Args:
            method_name: Name of the method being called.
            **kwargs: All keyword arguments passed to the method.
        """
        self._call_log.append(
            {
                "method": method_name,
                "args": (),
                "kwargs": dict(kwargs),
                "timestamp": datetime.utcnow(),
            }
        )

    def _check_error_config(self, method_name: str) -> None:
        """Raise the configured error for *method_name* if one exists.

        Args:
            method_name: The method name to check.

        Raises:
            Exception: Whatever exception was configured via
                :meth:`configure_error`.
        """
        if method_name in self._error_config:
            raise self._error_config[method_name]


# ---------------------------------------------------------------------------
# Module-level factory function
# ---------------------------------------------------------------------------

def create_mock_email_service(**kwargs: Any) -> MockEmailService:
    """Convenience factory that creates and returns a :class:`MockEmailService`.

    Args:
        **kwargs: Keyword arguments forwarded to the
            :class:`MockEmailService` constructor.

    Returns:
        A new :class:`MockEmailService` instance.

    Example::

        svc = create_mock_email_service(default_sender="ci@test.com")
    """
    return MockEmailService(**kwargs)
