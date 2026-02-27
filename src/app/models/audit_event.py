"""
AuditEvent SQLAlchemy Model — Persistent Audit Trail for System Actions.

This module defines the ``AuditEvent`` model that replaces the AUDIT_EVENT
entity from the DataStore schema (Section 6.2.1.2) of the original Java source
system (Sonatype Nexus Repository).

**Feature Context:**
Supports Feature F-303 (Audit Logging).  AuditEvents provide the persistent
audit trail for all significant system actions:

- **Authentication events**: login success/failure, token issuance
- **Authorization decisions**: access grants and denials
- **Configuration changes**: system settings modifications
- **Content modifications**: artifact uploads, deletions, repository changes
- **Administrative operations**: user/role management, task execution

**Architecture Context:**
In the Java source, the ``AUDIT_EVENT`` entity was managed by MyBatis 3.5.15
mappers with the Guava EventBus dispatching events to subscribers.  In the
Python/Flask implementation:

- The Blinker signal-based event system (``src.app.events.event_bus``)
  dispatches signals that result in ``AuditEvent`` record creation.
- SQLAlchemy 2.0.36 replaces MyBatis for ORM data access.
- The model supports both SQLite (standalone) and PostgreSQL (clustered)
  deployments via portable SQLAlchemy column types.

**Immutability Contract:**
Audit events are **immutable** compliance records.  Once created, they must
NEVER be modified or deleted.  For this reason:

- ``SoftDeleteMixin`` is explicitly **NOT** used.
- ``JSONAttributesMixin`` is explicitly **NOT** used; the ``attributes``
  column is defined directly on the model with event-specific payload
  semantics.

**Performance Considerations:**
This is a high-volume table.  Multiple performance-critical indexes are
defined in ``__table_args__`` to support common query patterns:

- By ``event_type`` for filtering by action category
- By ``timestamp`` for time-range queries and chronological ordering
- By ``domain`` for filtering by logical domain (security, repository, etc.)
- Composite ``(user_id, timestamp)`` for per-user audit trail queries

**Compatibility:**
- Python 3.12+ (uses type hints throughout per AAP Section 0.7.2)
- SQLite (standalone deployments) and PostgreSQL (clustered deployments)
- No Java dependencies (AAP Section 0.7.2)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, DateTime, Integer, JSON, String, Text, func

from src.app.extensions import db
from src.app.models.base import BaseModel, TimestampMixin

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 logging from the Java source.
# Provides structured logging for audit event model operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = ["AuditEvent"]


# ===========================================================================
# AuditEvent Model
# ===========================================================================


class AuditEvent(BaseModel, TimestampMixin):
    """Persistent audit trail entry for a single system action.

    Each ``AuditEvent`` records one significant action performed within the
    Nexus Repository system — authentication attempts, repository operations,
    configuration changes, content modifications, and administrative tasks.

    **Immutability:**
    Audit events are permanent compliance records.  The ``save()`` method
    inherited from ``BaseModel`` is used *only* during initial creation.
    The ``delete()`` and ``update()`` methods are inherited but should **not**
    be invoked on ``AuditEvent`` instances under normal operation, as audit
    records must be preserved intact for compliance auditing.

    **Columns (per AAP Section 0.2.3 AUDIT_EVENT entity):**

    +--------------+---------------+-------------------------------------------+
    | Column       | Type          | Description                               |
    +==============+===============+===========================================+
    | event_id     | BigInteger PK | Monotonically increasing event identifier |
    +--------------+---------------+-------------------------------------------+
    | user_id      | String(200)   | User who triggered the event (nullable)   |
    +--------------+---------------+-------------------------------------------+
    | event_type   | String(100)   | Event category (NOT NULL)                 |
    +--------------+---------------+-------------------------------------------+
    | timestamp    | DateTime      | When the event occurred (server default)  |
    +--------------+---------------+-------------------------------------------+
    | domain       | String(100)   | Logical domain (security, repository…)    |
    +--------------+---------------+-------------------------------------------+
    | attributes   | JSON          | Event-specific payload (varies by type)   |
    +--------------+---------------+-------------------------------------------+
    | ip_address   | String(45)    | Client IP (IPv4 or IPv6)                  |
    +--------------+---------------+-------------------------------------------+
    | node_id      | String(100)   | Cluster node identifier                   |
    +--------------+---------------+-------------------------------------------+
    | created_at   | DateTime      | Record creation timestamp (from mixin)    |
    +--------------+---------------+-------------------------------------------+
    | updated_at   | DateTime      | Last modification timestamp (from mixin)  |
    +--------------+---------------+-------------------------------------------+

    **Event Type Examples:**
    ``'AUTHENTICATION_SUCCESS'``, ``'AUTHENTICATION_FAILURE'``,
    ``'REPOSITORY_CREATED'``, ``'REPOSITORY_UPDATED'``,
    ``'REPOSITORY_DELETED'``, ``'COMPONENT_UPLOADED'``,
    ``'COMPONENT_DELETED'``, ``'USER_CREATED'``, ``'USER_ROLE_CHANGED'``,
    ``'CONFIG_CHANGED'``, ``'TASK_EXECUTED'``, ``'BLOBSTORE_CREATED'``

    **Domain Examples:**
    ``'security'``, ``'repository'``, ``'component'``, ``'configuration'``,
    ``'task'``, ``'blobstore'``

    **Attributes Payload Examples:**

    - ``REPOSITORY_CREATED``: ``{"repositoryName": "maven-releases",
      "format": "maven2", "type": "hosted"}``
    - ``AUTHENTICATION_FAILURE``: ``{"reason": "invalid_credentials",
      "attemptedUser": "admin"}``
    - ``CONFIG_CHANGED``: ``{"key": "security.anonymousAccess",
      "oldValue": "true", "newValue": "false"}``

    Usage::

        # Create via factory method (preferred):
        event = AuditEvent.create_event(
            event_type="REPOSITORY_CREATED",
            user_id="admin",
            domain="repository",
            attributes={"repositoryName": "maven-releases", "format": "maven2"},
            ip_address="192.168.1.100",
        )

        # Check event properties:
        event.is_security_event  # False
        event.is_system_event    # False (user_id is set)
    """

    __tablename__: str = "audit_events"

    # -- Performance-critical indexes for high-volume audit queries ----------
    # These indexes support the common query patterns documented in the AAP:
    #   - Filter by event_type (e.g., all AUTHENTICATION_FAILURE events)
    #   - Filter by timestamp range (e.g., last 24 hours)
    #   - Filter by domain (e.g., all security events)
    #   - Per-user audit trail (user_id + timestamp composite)
    __table_args__ = (
        db.Index("ix_audit_events_event_type", "event_type"),
        db.Index("ix_audit_events_timestamp", "timestamp"),
        db.Index("ix_audit_events_domain", "domain"),
        db.Index("ix_audit_events_user_timestamp", "user_id", "timestamp"),
    )

    # -- Columns -------------------------------------------------------------

    event_id: int = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
        doc="Monotonically increasing event identifier.  Uses BigInteger on "
        "PostgreSQL to accommodate high-volume audit logging without overflow "
        "concerns; falls back to Integer on SQLite (which uses 64-bit integers "
        "internally, so no range loss).  Serves as the primary key and "
        "provides natural temporal ordering.",
    )

    user_id: Optional[str] = Column(
        String(200),
        nullable=True,
        index=True,
        doc="The user ID of the principal who triggered the event.  Nullable "
        "because system-generated events (scheduled tasks, background "
        "processes, startup/shutdown) have no associated user.  Length of "
        "200 accommodates LDAP distinguished names and SSO subject IDs.",
    )

    event_type: str = Column(
        String(100),
        nullable=False,
        doc="The type of event, expressed as an uppercase identifier.  "
        "Examples: 'AUTHENTICATION_SUCCESS', 'REPOSITORY_CREATED', "
        "'CONFIG_CHANGED'.  Used as the primary filter dimension for "
        "audit trail queries.  NOT NULL — every event must have a type.",
    )

    timestamp: datetime = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        doc="When the event occurred.  Uses both a Python-side default "
        "(``datetime.now(timezone.utc)``) and a server-side default "
        "(``func.now()``) for accurate timing regardless of whether "
        "the record is created through SQLAlchemy or raw SQL.  "
        "Compatible with both SQLite and PostgreSQL.",
    )

    domain: Optional[str] = Column(
        String(100),
        nullable=True,
        doc="Logical domain of the event — groups events by functional area.  "
        "Examples: 'security', 'repository', 'component', 'configuration', "
        "'task', 'blobstore'.  Nullable because some events may not "
        "belong to a specific domain.",
    )

    attributes: Optional[dict[str, Any]] = Column(
        JSON,
        nullable=True,
        doc="Event-specific payload stored as a JSON object.  The structure "
        "varies by event_type, providing flexibility for different event "
        "categories.  Uses SQLAlchemy's portable JSON type which maps to "
        "TEXT with JSON emulation on SQLite and native json/jsonb on "
        "PostgreSQL.",
    )

    ip_address: Optional[str] = Column(
        String(45),
        nullable=True,
        doc="Client IP address from which the action originated.  Length of "
        "45 supports the longest possible IPv6 representation "
        "(e.g., '::ffff:192.168.1.1').  Nullable for system-generated "
        "events that have no network origin.",
    )

    node_id: Optional[str] = Column(
        String(100),
        nullable=True,
        doc="Cluster node identifier — identifies which node in a multi-node "
        "deployment generated the event.  Nullable for standalone "
        "single-node deployments.  Essential for distributed audit trail "
        "correlation in clustered environments.",
    )

    # -- Representation ------------------------------------------------------

    def __repr__(self) -> str:
        """Return a developer-friendly string representation.

        Includes the event ID, event type, and triggering user for quick
        identification during debugging and log output.

        Returns:
            A string in the format ``<AuditEvent {id}: {type} by {user}>``.
        """
        return f"<AuditEvent {self.event_id}: {self.event_type} by {self.user_id}>"

    # -- Serialization -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Convert this audit event to a plain dictionary.

        Extends ``BaseModel.to_dict()`` to ensure all audit-specific fields
        are included with proper serialization.  ``datetime`` values are
        converted to ISO-8601 strings for JSON compatibility.

        Returns:
            A dictionary containing all column values with datetime fields
            serialized as ISO-8601 strings.

        Example::

            event.to_dict()
            # {
            #     "event_id": 42,
            #     "user_id": "admin",
            #     "event_type": "REPOSITORY_CREATED",
            #     "timestamp": "2024-01-15T10:30:00",
            #     "domain": "repository",
            #     "attributes": {"repositoryName": "maven-releases"},
            #     "ip_address": "192.168.1.100",
            #     "node_id": "node-01",
            #     "created_at": "2024-01-15T10:30:00",
            #     "updated_at": "2024-01-15T10:30:00",
            # }
        """
        # Leverage BaseModel.to_dict() which iterates over mapped columns
        # and handles datetime serialization via isoformat()
        result: dict[str, Any] = super().to_dict()
        return result

    # -- Factory Method ------------------------------------------------------

    @classmethod
    def create_event(
        cls,
        event_type: str,
        user_id: Optional[str] = None,
        domain: Optional[str] = None,
        attributes: Optional[dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> "AuditEvent":
        """Factory method for creating and persisting a new audit event.

        This is the **preferred** way to create audit events.  It validates
        the required ``event_type`` parameter, constructs the model instance,
        persists it to the database, and returns the saved instance.

        The method uses ``db.session.add()`` and ``db.session.commit()`` to
        immediately persist the event, ensuring that audit records are
        durable even if the calling code encounters an error afterwards.

        Args:
            event_type: The type of event (e.g., ``'AUTHENTICATION_SUCCESS'``,
                ``'REPOSITORY_CREATED'``).  Must not be empty.
            user_id: The user who triggered the event.  ``None`` for
                system-generated events (scheduled tasks, startup, etc.).
            domain: Logical domain of the event (e.g., ``'security'``,
                ``'repository'``, ``'configuration'``).
            attributes: Event-specific JSON payload.  Structure varies by
                ``event_type``.  ``None`` for events with no additional data.
            ip_address: Client IP address (IPv4 or IPv6, max 45 chars).
                ``None`` for system-generated events.
            node_id: Cluster node identifier.  ``None`` for standalone
                single-node deployments.

        Returns:
            The newly created and persisted ``AuditEvent`` instance with
            its ``event_id`` populated by the database.

        Raises:
            ValueError: If ``event_type`` is ``None`` or empty/whitespace.
            sqlalchemy.exc.SQLAlchemyError: On any database-level error
                during persistence.

        Example::

            event = AuditEvent.create_event(
                event_type="AUTHENTICATION_SUCCESS",
                user_id="admin",
                domain="security",
                attributes={"method": "local"},
                ip_address="10.0.0.1",
                node_id="node-01",
            )
            print(event.event_id)  # auto-generated BigInteger
        """
        # Validate the required event_type parameter
        if not event_type or not event_type.strip():
            raise ValueError(
                "event_type is required and must not be empty.  "
                "Audit events cannot be created without a type identifier."
            )

        # Validate ip_address length if provided (IPv6 max = 45 chars)
        if ip_address is not None and len(ip_address) > 45:
            raise ValueError(
                f"ip_address exceeds maximum length of 45 characters: "
                f"'{ip_address}' (length={len(ip_address)}).  "
                f"IPv6 addresses should be at most 45 characters."
            )

        # Construct the audit event instance
        event = cls(
            event_type=event_type.strip(),
            user_id=user_id,
            domain=domain,
            attributes=attributes,
            ip_address=ip_address,
            node_id=node_id,
            timestamp=datetime.now(timezone.utc),
        )

        # Persist immediately — audit records must be durable
        db.session.add(event)
        db.session.commit()

        logger.info(
            "Audit event created: event_id=%s, event_type='%s', user_id='%s', "
            "domain='%s', ip_address='%s', node_id='%s'",
            event.event_id,
            event.event_type,
            event.user_id,
            event.domain,
            event.ip_address,
            event.node_id,
        )

        return event

    # -- Properties ----------------------------------------------------------

    @property
    def is_security_event(self) -> bool:
        """Check whether this event belongs to the security domain.

        Returns:
            ``True`` if the event's ``domain`` is ``'security'``, indicating
            an authentication, authorization, or credential-related action.
            ``False`` otherwise (including when ``domain`` is ``None``).
        """
        return self.domain == "security"

    @property
    def is_system_event(self) -> bool:
        """Check whether this is a system-generated event (no user context).

        System events are those triggered by background processes, scheduled
        tasks, startup/shutdown sequences, and other automated operations
        that have no associated human user.

        Returns:
            ``True`` if ``user_id`` is ``None``, indicating the event was
            generated by the system rather than a specific user.
        """
        return self.user_id is None


# ---------------------------------------------------------------------------
# Module initialization logging
# ---------------------------------------------------------------------------

logger.debug(
    "AuditEvent model loaded — tablename='%s', exports=%s",
    AuditEvent.__tablename__,
    __all__,
)
