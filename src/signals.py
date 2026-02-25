"""
Blinker signal definitions for the Nexus Repository application event system.

This module replaces the Guava EventBus pattern from the original Java 21
Sonatype Nexus Repository implementation with Python's blinker signal library.
Blinker is Flask's native signal library (auto-installed with Flask >= 3.1.3
as a dependency via ``blinker >= 1.9``).

All application-wide event signals are defined in a dedicated
:class:`blinker.Namespace` (``nexus_signals``) to avoid collision with
Flask's built-in signals.  Blinker signals are **thread-safe** by default,
so no additional synchronization is required.

Cross-Cutting Concerns Served by These Signals
-----------------------------------------------
* **Audit logging** — ``src/security/audit.py`` subscribes to all signals to
  persist JSON-serialized audit events for SIEM integration (F-303).
* **Webhook dispatch** — ``src/integration/webhooks.py`` subscribes to
  repository, component, config, and user/role signals to send HMAC-signed
  HTTP POST notifications to configured URLs (F-503).
* **Search reindexing** — ``src/repositories/search.py`` subscribes to
  component and repository signals to keep the full-text search index
  up-to-date (F-103).

Publishing a Signal
-------------------
Emit an event by calling :meth:`~blinker.base.NamedSignal.send` on the
signal object, passing the *sender* (typically ``self`` or the current
module) and any keyword arguments that describe the event payload::

    from src.signals import repository_created

    repository_created.send(
        current_app._get_current_object(),
        repository_id=repo.id,
        name=repo.name,
        format=repo.format,
        type=repo.type,
    )

Subscribing to a Signal
-----------------------
Register a handler function via :meth:`~blinker.base.NamedSignal.connect`::

    from src.signals import repository_created

    def on_repository_created(sender, **kwargs):
        print(f"Repository created: {kwargs['name']}")

    repository_created.connect(on_repository_created)

To disconnect a previously registered handler use
:meth:`~blinker.base.NamedSignal.disconnect`::

    repository_created.disconnect(on_repository_created)

Reference
---------
See AAP Section 0.4.4 — Event System Integration Points for the complete
publisher/subscriber mapping table.
"""

from blinker import Namespace

# ---------------------------------------------------------------------------
# Application Signal Namespace
# ---------------------------------------------------------------------------
# A dedicated blinker Namespace isolates Nexus application signals from
# Flask's internal signals, preventing accidental name collisions.
# Retrieve or create any signal via ``nexus_signals.signal('signal-name')``.
nexus_signals: Namespace = Namespace()

# ===================================================================
# Repository Lifecycle Signals
# ===================================================================

repository_created = nexus_signals.signal('repository-created')
"""Signal emitted when a new repository is created and persisted.

**Publisher(s):** ``src/repositories/services.py`` (RepositoryManager)

**Subscriber(s):**
* ``src/security/audit.py`` — Audit event persistence
* ``src/integration/webhooks.py`` — Webhook notification dispatch

**Expected keyword arguments:**

:param repository_id: Unique identifier of the newly created repository.
:type repository_id: str
:param name: Human-readable repository name.
:type name: str
:param format: Repository format (e.g. ``'maven'``, ``'npm'``, ``'docker'``).
:type format: str
:param type: Repository type (``'hosted'``, ``'proxy'``, or ``'group'``).
:type type: str
"""

repository_deleted = nexus_signals.signal('repository-deleted')
"""Signal emitted when a repository is permanently deleted.

**Publisher(s):** ``src/repositories/services.py`` (RepositoryManager)

**Subscriber(s):**
* ``src/security/audit.py`` — Audit event persistence
* ``src/integration/webhooks.py`` — Webhook notification dispatch
* ``src/repositories/search.py`` — Search index entry removal

**Expected keyword arguments:**

:param repository_id: Unique identifier of the deleted repository.
:type repository_id: str
:param name: Human-readable repository name.
:type name: str
"""

# ===================================================================
# Component / Asset Signals
# ===================================================================

component_uploaded = nexus_signals.signal('component-uploaded')
"""Signal emitted when a new component (artifact) is uploaded to a hosted repository.

**Publisher(s):** ``src/repositories/types/hosted.py``

**Subscriber(s):**
* ``src/repositories/search.py`` — Search index update
* ``src/security/audit.py`` — Audit event persistence
* ``src/integration/webhooks.py`` — Webhook notification dispatch

**Expected keyword arguments:**

:param component_id: Unique identifier of the uploaded component.
:type component_id: str
:param repository_id: Identifier of the owning repository.
:type repository_id: str
:param namespace: Component namespace (e.g. Maven groupId, npm scope).
:type namespace: str
:param name: Component name (e.g. artifact ID, package name).
:type name: str
:param version: Component version string.
:type version: str
"""

component_deleted = nexus_signals.signal('component-deleted')
"""Signal emitted when a component is deleted by a cleanup policy or manual action.

**Publisher(s):** ``src/storage/cleanup.py``

**Subscriber(s):**
* ``src/repositories/search.py`` — Search index entry removal
* ``src/security/audit.py`` — Audit event persistence

**Expected keyword arguments:**

:param component_id: Unique identifier of the deleted component.
:type component_id: str
:param repository_id: Identifier of the owning repository.
:type repository_id: str
"""

# ===================================================================
# Authentication Signals
# ===================================================================

auth_success = nexus_signals.signal('auth-success')
"""Signal emitted after a successful authentication attempt.

**Publisher(s):** ``src/security/auth/chain.py``
(FirstSuccessfulAuthenticator)

**Subscriber(s):**
* ``src/security/audit.py`` — Audit event persistence

**Expected keyword arguments:**

:param user_id: Unique identifier of the authenticated user.
:type user_id: str
:param username: Login username.
:type username: str
:param realm: Name of the realm that authenticated the user
    (e.g. ``'local'``, ``'bearer'``, ``'jwt'``, ``'ldap'``).
:type realm: str
:param source_ip: IP address of the originating request.
:type source_ip: str
"""

auth_failure = nexus_signals.signal('auth-failure')
"""Signal emitted after a failed authentication attempt.

Per AAP Section 0.7.2, all authentication failures **must** be audited.

**Publisher(s):** ``src/security/auth/chain.py``
(FirstSuccessfulAuthenticator)

**Subscriber(s):**
* ``src/security/audit.py`` — Audit event persistence

**Expected keyword arguments:**

:param username: Login username that was attempted.
:type username: str
:param realm: Name of the realm that rejected the attempt, or ``'all'``
    if every configured realm failed.
:type realm: str
:param reason: Human-readable failure reason
    (e.g. ``'invalid_password'``, ``'account_disabled'``).
:type reason: str
:param source_ip: IP address of the originating request.
:type source_ip: str
"""

# ===================================================================
# Configuration Signals
# ===================================================================

config_changed = nexus_signals.signal('config-changed')
"""Signal emitted when a system configuration value is created, updated, or deleted.

**Publisher(s):** ``src/admin/system_config.py``

**Subscriber(s):**
* ``src/security/audit.py`` — Audit event persistence
* ``src/integration/webhooks.py`` — Webhook notification dispatch

**Expected keyword arguments:**

:param key: Configuration key that was modified.
:type key: str
:param old_value: Previous value (``None`` for newly created keys).
:type old_value: str or None
:param new_value: New value (``None`` when a key is deleted).
:type new_value: str or None
:param user_id: Identifier of the user who made the change.
:type user_id: str
"""

# ===================================================================
# Task / Scheduler Signals
# ===================================================================

task_completed = nexus_signals.signal('task-completed')
"""Signal emitted when a scheduled task finishes execution.

**Publisher(s):** ``src/admin/scheduler.py``

**Subscriber(s):**
* ``src/security/audit.py`` — Audit event persistence

**Expected keyword arguments:**

:param task_id: Unique identifier of the completed task execution.
:type task_id: str
:param task_type: Type/category of the task (e.g. ``'cleanup'``,
    ``'compaction'``, ``'integrity_check'``).
:type task_type: str
:param status: Execution outcome (``'success'``, ``'failure'``, ``'cancelled'``).
:type status: str
:param duration: Wall-clock execution duration in seconds.
:type duration: float
"""

# ===================================================================
# User / Role Management Signals
# ===================================================================

user_modified = nexus_signals.signal('user-modified')
"""Signal emitted when a user account is created, updated, or deleted.

**Publisher(s):** ``src/security/routes.py``

**Subscriber(s):**
* ``src/security/audit.py`` — Audit event persistence
* ``src/integration/webhooks.py`` — Webhook notification dispatch

**Expected keyword arguments:**

:param user_id: Unique identifier of the affected user.
:type user_id: str
:param action: The modification action — one of ``'created'``,
    ``'updated'``, or ``'deleted'``.
:type action: str
:param modified_by: Identifier of the administrator who performed the action.
:type modified_by: str
"""

role_modified = nexus_signals.signal('role-modified')
"""Signal emitted when a role definition is created, updated, or deleted.

**Publisher(s):** ``src/security/routes.py``

**Subscriber(s):**
* ``src/security/audit.py`` — Audit event persistence

**Expected keyword arguments:**

:param role_id: Unique identifier of the affected role.
:type role_id: str
:param action: The modification action — one of ``'created'``,
    ``'updated'``, or ``'deleted'``.
:type action: str
:param modified_by: Identifier of the administrator who performed the action.
:type modified_by: str
"""

# ===================================================================
# Public API — Convenience collection of all signal objects
# ===================================================================
# Provides a programmatic listing for introspection, testing, or
# dynamically wiring subscribers during application startup.

ALL_SIGNALS = (
    repository_created,
    repository_deleted,
    component_uploaded,
    component_deleted,
    auth_success,
    auth_failure,
    config_changed,
    task_completed,
    user_modified,
    role_modified,
)
"""Tuple of all Nexus application signals for programmatic iteration."""
