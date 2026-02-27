"""
Webhook HTTP dispatch with HMAC signing for the Nexus Repository application.

This package implements Feature F-503 (Webhook Integration), providing
HTTP POST dispatch of repository events to configured webhook endpoints
with HMAC-SHA256 payload signing for verification.

The webhook system integrates with the Blinker event system (src.app.events)
to automatically dispatch configured events to external webhook endpoints.

Key components:
- dispatcher: Webhook HTTP POST delivery with retry logic and exponential backoff
- payload_signer: HMAC-SHA256 payload signing for webhook verification

Usage:
    from src.app.webhooks import WebhookDispatcher, sign_payload

    dispatcher = WebhookDispatcher()
    dispatcher.dispatch_event(event_type='repository.created', payload={...})
"""

from src.app.webhooks.dispatcher import WebhookDispatcher
from src.app.webhooks.payload_signer import sign_payload, verify_signature

__all__ = [
    "WebhookDispatcher",
    "sign_payload",
    "verify_signature",
]
