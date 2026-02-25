"""
Test Data Factories and Fixtures for Sonatype Nexus Repository.

This module provides comprehensive **factory-boy** factory classes for ALL
SQLAlchemy models defined in ``src/app/models/``.  It is the foundational
test data layer consumed by both ``tests/unit/`` and ``tests/integration/``
test suites.

**Purpose:**

- Generate valid, realistic model instances for unit and integration testing
- Replace manual test data setup with deterministic, seeded data generation
- Provide trait-based factory variants for testing different entity states
  (e.g., admin vs. regular user, hosted vs. proxy repository)
- Support reproducible tests via ``seed_factories()`` and ``set_session()``

**Factory Inventory (14 factories covering all DataStore entities):**

+---------------------------+----------------------------+-----------------+
| Factory Class             | SQLAlchemy Model           | Feature Support |
+===========================+============================+=================+
| RepositoryFactory         | Repository                 | F-101, F-102    |
+---------------------------+----------------------------+-----------------+
| ComponentFactory          | Component                  | F-101, F-103    |
+---------------------------+----------------------------+-----------------+
| AssetFactory              | Asset                      | F-101, F-204    |
+---------------------------+----------------------------+-----------------+
| UserFactory               | User                       | F-301, F-304    |
+---------------------------+----------------------------+-----------------+
| RoleFactory               | Role                       | F-301           |
+---------------------------+----------------------------+-----------------+
| PrivilegeFactory          | Privilege                  | F-301           |
+---------------------------+----------------------------+-----------------+
| ContentSelectorFactory    | ContentSelector            | F-301           |
+---------------------------+----------------------------+-----------------+
| TaskFactory               | TaskDefinition             | F-402           |
+---------------------------+----------------------------+-----------------+
| TaskExecutionFactory      | TaskExecution              | F-402           |
+---------------------------+----------------------------+-----------------+
| AuditEventFactory         | AuditEvent                 | F-303           |
+---------------------------+----------------------------+-----------------+
| BlobStoreConfigFactory    | BlobStoreConfig            | F-201, F-202    |
+---------------------------+----------------------------+-----------------+
| CleanupPolicyFactory      | CleanupPolicy              | F-204           |
+---------------------------+----------------------------+-----------------+
| SystemConfigFactory       | SystemConfig               | F-404           |
+---------------------------+----------------------------+-----------------+

**Usage Example::

    from tests.fixtures.test_data import (
        RepositoryFactory, ComponentFactory, seed_factories, set_session
    )

    # In conftest.py:
    seed_factories(42)
    set_session(db.session)

    # In tests:
    repo = RepositoryFactory(hosted=True, maven=True)
    component = ComponentFactory(repository_name=repo.name, maven=True)

**Python 3.12+ | factory-boy 3.3.1 | Faker 33.1.0**
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import factory
from factory import Faker, LazyAttribute, LazyFunction, Sequence, SubFactory, fuzzy
from factory.alchemy import SQLAlchemyModelFactory

from src.app.extensions import db
from src.app.models.asset import Asset
from src.app.models.audit_event import AuditEvent
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.models.cleanup_policy import CleanupPolicy
from src.app.models.component import Component
from src.app.models.content_selector import ContentSelector
from src.app.models.privilege import Privilege
from src.app.models.repository import Repository
from src.app.models.role import Role, RoleAssignment
from src.app.models.system_config import SystemConfig
from src.app.models.task import TaskDefinition, TaskExecution
from src.app.models.user import User


# ---------------------------------------------------------------------------
# Module-Level Exports
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "BaseFactory",
    "RepositoryFactory",
    "ComponentFactory",
    "AssetFactory",
    "UserFactory",
    "RoleFactory",
    "PrivilegeFactory",
    "ContentSelectorFactory",
    "TaskFactory",
    "TaskExecutionFactory",
    "AuditEventFactory",
    "BlobStoreConfigFactory",
    "CleanupPolicyFactory",
    "SystemConfigFactory",
    "seed_factories",
    "set_session",
]


# ===========================================================================
# Base Factory
# ===========================================================================


class BaseFactory(SQLAlchemyModelFactory):
    """Abstract base factory for all SQLAlchemy model factories.

    All entity-specific factory classes inherit from this base.  The
    ``sqlalchemy_session`` is initially ``None`` and **must** be configured
    at test time via :func:`set_session` (typically called in ``conftest.py``).

    The ``sqlalchemy_session_persistence = 'commit'`` setting means that
    ``create()`` calls will commit objects to the database, matching the
    behavior expected by integration tests that verify persistence.

    Inherited Methods (from ``SQLAlchemyModelFactory``):
        - ``create(**kwargs)``: Create and persist an instance to the DB.
        - ``build(**kwargs)``: Create an in-memory instance without persisting.
        - ``create_batch(n, **kwargs)``: Create and persist ``n`` instances.
        - ``build_batch(n, **kwargs)``: Create ``n`` in-memory instances.
        - ``stub(**kwargs)``: Create a stub object with computed attributes.
    """

    class Meta:
        abstract = True
        sqlalchemy_session = None
        sqlalchemy_session_persistence = "commit"


# ===========================================================================
# RepositoryFactory — Features F-101, F-102
# ===========================================================================


class RepositoryFactory(BaseFactory):
    """Factory for creating :class:`Repository` test instances.

    Generates repositories with random format/type combinations from the 7
    supported formats (maven2, npm, docker, nuget, pypi, apt, raw) and 3
    types (hosted, proxy, group).

    **Traits:**

    Type-specific:
        - ``hosted=True``: Hosted repository with ALLOW write policy
        - ``proxy=True``: Proxy repository with remote URL and negative cache
        - ``group=True``: Group repository with two default members

    Format-specific:
        - ``maven=True``: Maven format with ``maven-repo-*`` naming
        - ``npm=True``: npm format with ``npm-repo-*`` naming
        - ``docker=True``: Docker format with ``docker-repo-*`` naming

    Example::

        repo = RepositoryFactory(hosted=True, maven=True)
        assert repo.type == 'hosted'
        assert repo.format == 'maven2'
    """

    class Meta:
        model = Repository

    # -- Field Definitions ---------------------------------------------------

    name = Sequence(lambda n: f"test-repo-{n}")
    format = fuzzy.FuzzyChoice(
        ["maven2", "npm", "docker", "nuget", "pypi", "apt", "raw"]
    )
    type = fuzzy.FuzzyChoice(["hosted", "proxy", "group"])
    blob_store_name = "default"
    online = True
    attributes = LazyAttribute(lambda o: {})
    routing_rule = None
    cleanup_policies = None

    # -- Traits for type-specific configurations -----------------------------

    class Params:
        """Factory parameters for type-specific and format-specific traits."""

        hosted = factory.Trait(
            type="hosted",
            attributes={
                "storage": {
                    "writePolicy": "ALLOW",
                    "strictContentTypeValidation": True,
                },
            },
        )
        proxy = factory.Trait(
            type="proxy",
            attributes={
                "proxy": {
                    "remoteUrl": "https://repo1.maven.org/maven2/",
                    "contentMaxAge": 1440,
                    "metadataMaxAge": 1440,
                },
                "negativeCache": {
                    "enabled": True,
                    "timeToLive": 1440,
                },
            },
        )
        group = factory.Trait(
            type="group",
            attributes={
                "group": {
                    "memberNames": ["member-1", "member-2"],
                },
            },
        )
        maven = factory.Trait(
            format="maven2",
            name=Sequence(lambda n: f"maven-repo-{n}"),
        )
        npm = factory.Trait(
            format="npm",
            name=Sequence(lambda n: f"npm-repo-{n}"),
        )
        docker = factory.Trait(
            format="docker",
            name=Sequence(lambda n: f"docker-repo-{n}"),
        )


# ===========================================================================
# ComponentFactory — Features F-101, F-103
# ===========================================================================


class ComponentFactory(BaseFactory):
    """Factory for creating :class:`Component` test instances.

    Generates components with realistic coordinate tuples
    (repository_name, namespace, name, version) and format-specific
    traits for Maven and npm.

    **Traits:**

    Format-specific:
        - ``maven=True``: Maven GAV coordinates with domain-based namespace
        - ``npm=True``: npm scoped packages with ``@scope-xxxx`` namespace

    Example::

        comp = ComponentFactory(maven=True, repository_name='maven-central')
        assert comp.namespace  # domain_name from Faker
    """

    class Meta:
        model = Component

    # -- Field Definitions ---------------------------------------------------

    repository_name = Sequence(lambda n: f"test-repo-{n}")
    namespace = Faker("domain_word")
    name = Faker("word")
    version = Sequence(
        lambda n: f"{(n // 100) + 1}.{(n // 10) % 10}.{n % 10}"
    )
    attributes = LazyAttribute(lambda o: {})

    # -- Format-specific Traits ----------------------------------------------

    class Params:
        """Factory parameters for format-specific component traits."""

        maven = factory.Trait(
            namespace=Faker("domain_name"),
            name=Sequence(lambda n: f"artifact-{n}"),
        )
        npm = factory.Trait(
            namespace=LazyFunction(
                lambda: f"@scope-{secrets.token_hex(2)}"
            ),
            name=Sequence(lambda n: f"package-{n}"),
        )


# ===========================================================================
# AssetFactory — Features F-101, F-103, F-204
# ===========================================================================


class AssetFactory(BaseFactory):
    """Factory for creating :class:`Asset` test instances.

    Generates assets with valid cryptographic checksums (SHA-1, SHA-256, MD5),
    realistic MIME content types, file sizes, and BlobStore references.

    **Checksum Validation:**

    - ``checksum_sha1``: 40-character hex string (SHA-1 digest)
    - ``checksum_sha256``: 64-character hex string (SHA-256 digest)
    - ``checksum_md5``: 32-character hex string (MD5 digest)

    Example::

        asset = AssetFactory(repository_name='maven-central')
        assert len(asset.checksum_sha1) == 40
        assert len(asset.checksum_sha256) == 64
        assert len(asset.checksum_md5) == 32
    """

    class Meta:
        model = Asset

    # -- Field Definitions ---------------------------------------------------

    component_id = None
    repository_name = Sequence(lambda n: f"test-repo-{n}")
    path = Sequence(lambda n: f"/path/to/artifact-{n}.jar")
    content_type = fuzzy.FuzzyChoice(
        [
            "application/java-archive",
            "application/json",
            "application/octet-stream",
            "text/xml",
            "application/gzip",
        ]
    )
    checksum_sha1 = LazyFunction(
        lambda: hashlib.sha1(secrets.token_bytes(32)).hexdigest()
    )
    checksum_sha256 = LazyFunction(
        lambda: hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    )
    checksum_md5 = LazyFunction(
        lambda: hashlib.md5(secrets.token_bytes(32)).hexdigest()
    )
    size = fuzzy.FuzzyInteger(100, 10_000_000)
    last_downloaded = None
    blob_ref = LazyAttribute(
        lambda o: f"default@{hashlib.sha1(o.path.encode()).hexdigest()}"
    )
    attributes = LazyAttribute(lambda o: {})


# ===========================================================================
# UserFactory — Features F-301, F-304
# ===========================================================================


class UserFactory(BaseFactory):
    """Factory for creating :class:`User` test instances.

    Generates users with simulated Werkzeug-format password hashes,
    Faker-generated emails and names, and traits for common user states.

    **Traits:**

    Account types:
        - ``admin=True``: Admin user with ``user_id='admin'``
        - ``with_api_key=True``: User with an API key (F-304)
        - ``ldap=True``: LDAP-authenticated user (no local password)

    Account states:
        - ``disabled=True``: Disabled user account
        - ``locked=True``: Locked user (5 failed login attempts)

    Example::

        admin = UserFactory(admin=True)
        assert admin.user_id == 'admin'

        api_user = UserFactory(with_api_key=True)
        assert api_user.api_key is not None
    """

    class Meta:
        model = User

    # -- Field Definitions ---------------------------------------------------

    user_id = Sequence(lambda n: f"user-{n}")
    password_hash = LazyFunction(
        lambda: "pbkdf2:sha256:600000$test$" + secrets.token_hex(32)
    )
    status = "active"
    email = Faker("email")
    first_name = Faker("first_name")
    last_name = Faker("last_name")
    api_key = None
    external_id = None
    attributes = LazyAttribute(lambda o: {})
    last_login = None
    failed_login_count = 0

    # -- Traits for user state variants --------------------------------------

    class Params:
        """Factory parameters for user state traits."""

        admin = factory.Trait(
            user_id="admin",
            email="admin@nexus.local",
        )
        with_api_key = factory.Trait(
            api_key=LazyFunction(lambda: secrets.token_urlsafe(32)),
        )
        ldap = factory.Trait(
            external_id=Faker("name"),
            password_hash=None,
        )
        disabled = factory.Trait(
            status="disabled",
        )
        locked = factory.Trait(
            status="locked",
            failed_login_count=5,
        )


# ===========================================================================
# RoleFactory — Feature F-301
# ===========================================================================


class RoleFactory(BaseFactory):
    """Factory for creating :class:`Role` test instances.

    Generates roles with privilege JSON arrays and traits for the system's
    default roles (admin, anonymous) and external source roles.

    **Traits:**

    Default roles:
        - ``admin=True``: nx-admin role with ``['nx-all']`` privileges
        - ``anonymous=True``: nx-anonymous role with read-only privileges

    Source types:
        - ``external=True``: Externally-mapped role (LDAP/SSO source)

    Example::

        admin_role = RoleFactory(admin=True)
        assert admin_role.role_id == 'nx-admin'
        assert 'nx-all' in admin_role.privileges
    """

    class Meta:
        model = Role

    # -- Field Definitions ---------------------------------------------------

    role_id = Sequence(lambda n: f"role-{n}")
    name = Sequence(lambda n: f"Test Role {n}")
    description = Faker("sentence")
    privileges = LazyAttribute(
        lambda o: [
            "nx-repository-view-*-*-read",
            "nx-repository-view-*-*-browse",
        ]
    )
    source = "internal"
    attributes = LazyAttribute(lambda o: {})

    # -- Traits for role variants --------------------------------------------

    class Params:
        """Factory parameters for role variant traits."""

        admin = factory.Trait(
            role_id="nx-admin",
            name="Administrator",
            privileges=["nx-all"],
        )
        anonymous = factory.Trait(
            role_id="nx-anonymous",
            name="Anonymous",
            privileges=["nx-repository-view-*-*-read"],
        )
        external = factory.Trait(
            source="external",
        )


# ===========================================================================
# PrivilegeFactory — Feature F-301
# ===========================================================================


class PrivilegeFactory(BaseFactory):
    """Factory for creating :class:`Privilege` test instances.

    Generates privilege descriptors with 5 privilege types (application,
    repository-admin, repository-view, repository-content-selector, wildcard)
    and traits for common privilege configurations.

    **Traits:**

    Privilege types:
        - ``wildcard=True``: nx-all super-admin privilege
        - ``repo_view=True``: Repository view (read/browse) privilege
        - ``repo_admin=True``: Repository admin privilege

    Example::

        wildcard = PrivilegeFactory(wildcard=True)
        assert wildcard.privilege_id == 'nx-all'
        assert wildcard.type == 'wildcard'
    """

    class Meta:
        model = Privilege

    # -- Field Definitions ---------------------------------------------------

    privilege_id = Sequence(lambda n: f"nx-privilege-{n}")
    type = fuzzy.FuzzyChoice(
        [
            "application",
            "repository-admin",
            "repository-view",
            "repository-content-selector",
            "wildcard",
        ]
    )
    name = Sequence(lambda n: f"Test Privilege {n}")
    description = Faker("sentence")
    properties = LazyAttribute(
        lambda o: {
            "format": "*",
            "repository": "*",
            "actions": ["read", "browse"],
        }
    )
    attributes = LazyAttribute(lambda o: {})

    # -- Traits for privilege type variants ----------------------------------

    class Params:
        """Factory parameters for privilege type traits."""

        wildcard = factory.Trait(
            type="wildcard",
            privilege_id="nx-all",
            name="All permissions",
            properties={},
        )
        repo_view = factory.Trait(
            type="repository-view",
            properties={
                "format": "maven2",
                "repository": "*",
                "actions": ["read", "browse"],
            },
        )
        repo_admin = factory.Trait(
            type="repository-admin",
            properties={
                "format": "maven2",
                "repository": "*",
            },
        )


# ===========================================================================
# ContentSelectorFactory — Feature F-301 Tier 3 RBAC
# ===========================================================================


class ContentSelectorFactory(BaseFactory):
    """Factory for creating :class:`ContentSelector` test instances.

    Generates CSEL expression instances for sub-repository access control
    testing.  ContentSelector is one of the 14 DataStore entities and is
    referenced by ``repository-content-selector`` type privileges.

    Example::

        selector = ContentSelectorFactory()
        assert selector.type == 'csel'
        assert 'maven2' in selector.expression
    """

    class Meta:
        model = ContentSelector

    # -- Field Definitions ---------------------------------------------------

    selector_id = Sequence(lambda n: f"selector-{n}")
    name = Sequence(lambda n: f"Test Selector {n}")
    type = "csel"
    expression = 'format == "maven2" and path =^ "/org/"'
    description = Faker("sentence")


# ===========================================================================
# TaskFactory — Feature F-402
# ===========================================================================


class TaskFactory(BaseFactory):
    """Factory for creating :class:`TaskDefinition` test instances.

    Generates scheduled task definitions with cron expressions, task-specific
    JSON configuration, and traits for common task types.

    **Traits:**

    Task types:
        - ``one_time=True``: One-time task (no cron schedule)
        - ``disabled=True``: Disabled task
        - ``cleanup=True``: Repository cleanup task

    Example::

        daily_cleanup = TaskFactory(cleanup=True)
        assert daily_cleanup.type == 'repository.cleanup'
        assert 'policyNames' in daily_cleanup.configuration
    """

    class Meta:
        model = TaskDefinition

    # -- Field Definitions ---------------------------------------------------

    task_id = Sequence(lambda n: f"task-{n}")
    type = fuzzy.FuzzyChoice(
        [
            "repository.cleanup",
            "blobstore.compact",
            "db.backup",
            "repository.rebuild-index",
            "blobstore.delete-temp",
        ]
    )
    name = Sequence(lambda n: f"Test Task {n}")
    cron_expression = "0 0 * * *"
    enabled = True
    configuration = LazyAttribute(lambda o: {"repositoryName": "*"})
    attributes = LazyAttribute(lambda o: {})
    last_run_status = None
    next_run_time = None

    # -- Traits for task type variants ---------------------------------------

    class Params:
        """Factory parameters for task type traits."""

        one_time = factory.Trait(
            cron_expression=None,
        )
        disabled = factory.Trait(
            enabled=False,
        )
        cleanup = factory.Trait(
            type="repository.cleanup",
            configuration={
                "repositoryName": "*",
                "policyNames": ["cleanup-snapshots"],
            },
        )


# ===========================================================================
# TaskExecutionFactory — Feature F-402
# ===========================================================================


class TaskExecutionFactory(BaseFactory):
    """Factory for creating :class:`TaskExecution` test instances.

    Generates task execution records with timing data, status, and progress.
    Duration is automatically computed from start_time and end_time.

    **Traits:**

    Execution states:
        - ``running=True``: Currently running task (no end_time)
        - ``failed=True``: Failed task with error message

    Example::

        running = TaskExecutionFactory(running=True)
        assert running.status == 'running'
        assert running.end_time is None
    """

    class Meta:
        model = TaskExecution

    # -- Field Definitions ---------------------------------------------------

    task_id = Sequence(lambda n: f"task-{n}")
    start_time = LazyFunction(
        lambda: datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    end_time = LazyFunction(lambda: datetime.now(timezone.utc))
    status = "ok"
    duration_ms = LazyAttribute(
        lambda o: (
            int((o.end_time - o.start_time).total_seconds() * 1000)
            if o.end_time and o.start_time
            else None
        )
    )
    progress = "Completed successfully"
    error_message = None

    # -- Traits for execution state variants ---------------------------------

    class Params:
        """Factory parameters for execution state traits."""

        running = factory.Trait(
            status="running",
            end_time=None,
            duration_ms=None,
            progress="In progress...",
        )
        failed = factory.Trait(
            status="failed",
            error_message="Task failed with unexpected error",
        )


# ===========================================================================
# AuditEventFactory — Feature F-303
# ===========================================================================


class AuditEventFactory(BaseFactory):
    """Factory for creating :class:`AuditEvent` test instances.

    Generates audit log entries with event types across all domains
    (security, repository, component, configuration, task, blobstore),
    timestamps, IP addresses, and event-specific attribute payloads.

    **Traits:**

    Event types:
        - ``auth_success=True``: Successful authentication event
        - ``auth_failure=True``: Failed authentication event
        - ``repo_created=True``: Repository creation event
        - ``system_event=True``: System-generated event (no user_id)

    Example::

        failure = AuditEventFactory(auth_failure=True)
        assert failure.event_type == 'AUTHENTICATION_FAILURE'
        assert failure.domain == 'security'
    """

    class Meta:
        model = AuditEvent

    # -- Field Definitions ---------------------------------------------------

    user_id = Sequence(lambda n: f"user-{n}")
    event_type = fuzzy.FuzzyChoice(
        [
            "AUTHENTICATION_SUCCESS",
            "AUTHENTICATION_FAILURE",
            "REPOSITORY_CREATED",
            "REPOSITORY_UPDATED",
            "REPOSITORY_DELETED",
            "COMPONENT_UPLOADED",
            "COMPONENT_DELETED",
            "USER_CREATED",
            "CONFIG_CHANGED",
            "TASK_EXECUTED",
        ]
    )
    timestamp = LazyFunction(lambda: datetime.now(timezone.utc))
    domain = fuzzy.FuzzyChoice(
        [
            "security",
            "repository",
            "component",
            "configuration",
            "task",
            "blobstore",
        ]
    )
    attributes = LazyAttribute(lambda o: {})
    ip_address = Faker("ipv4")
    node_id = None

    # -- Traits for event type variants --------------------------------------

    class Params:
        """Factory parameters for event type traits."""

        auth_success = factory.Trait(
            event_type="AUTHENTICATION_SUCCESS",
            domain="security",
            attributes={"method": "local"},
        )
        auth_failure = factory.Trait(
            event_type="AUTHENTICATION_FAILURE",
            domain="security",
            attributes={"reason": "invalid_credentials"},
        )
        repo_created = factory.Trait(
            event_type="REPOSITORY_CREATED",
            domain="repository",
            attributes={
                "repositoryName": "test-repo",
                "format": "maven2",
                "type": "hosted",
            },
        )
        system_event = factory.Trait(
            user_id=None,
            domain="task",
        )


# ===========================================================================
# BlobStoreConfigFactory — Features F-201, F-202
# ===========================================================================


class BlobStoreConfigFactory(BaseFactory):
    """Factory for creating :class:`BlobStoreConfig` test instances.

    Generates BlobStore configuration instances for both file (local
    filesystem) and S3 (AWS) storage backends with type-dependent
    configuration JSON and storage statistics.

    **Traits:**

    Backend types:
        - ``file_store=True``: Local filesystem BlobStore (F-201)
        - ``s3_store=True``: Amazon S3 BlobStore (F-202)
        - ``default=True``: Default BlobStore (named 'default', file type)

    Example::

        s3 = BlobStoreConfigFactory(s3_store=True)
        assert s3.type == 's3'
        assert 'bucket' in s3.configuration
    """

    class Meta:
        model = BlobStoreConfig

    # -- Field Definitions ---------------------------------------------------

    blob_store_name = Sequence(lambda n: f"blobstore-{n}")
    type = "file"
    configuration = LazyAttribute(
        lambda o: (
            {"path": f"/nexus-data/blobs/{o.blob_store_name}"}
            if o.type == "file"
            else {
                "bucket": "test-bucket",
                "region": "us-east-1",
                "prefix": "nexus/",
                "accessKeyId": "AKIAIOSFODNN7EXAMPLE",
                "secretAccessKey": "***MASKED***",
                "endpoint": None,
                "encryption": {
                    "type": "s3ManagedEncryption",
                    "key": None,
                },
                "forcePathStyle": False,
            }
        )
    )
    total_size = fuzzy.FuzzyInteger(0, 1_000_000_000)
    blob_count = fuzzy.FuzzyInteger(0, 10_000)
    available_space = fuzzy.FuzzyInteger(1_000_000_000, 100_000_000_000)
    attributes = LazyAttribute(lambda o: {})

    # -- Traits for BlobStore type variants ----------------------------------

    class Params:
        """Factory parameters for BlobStore type traits."""

        file_store = factory.Trait(
            type="file",
            blob_store_name=Sequence(lambda n: f"file-store-{n}"),
            configuration={"path": "/nexus-data/blobs/default"},
        )
        s3_store = factory.Trait(
            type="s3",
            blob_store_name=Sequence(lambda n: f"s3-store-{n}"),
            configuration={
                "bucket": "nexus-blobs",
                "region": "us-east-1",
                "prefix": "nexus/",
                "accessKeyId": "AKIAIOSFODNN7EXAMPLE",
                "secretAccessKey": "***MASKED***",
                "endpoint": None,
                "encryption": {
                    "type": "s3ManagedEncryption",
                    "key": None,
                },
                "forcePathStyle": False,
            },
        )
        default = factory.Trait(
            blob_store_name="default",
            type="file",
            configuration={"path": "/nexus-data/blobs/default"},
        )


# ===========================================================================
# CleanupPolicyFactory — Feature F-204
# ===========================================================================


class CleanupPolicyFactory(BaseFactory):
    """Factory for creating :class:`CleanupPolicy` test instances.

    Generates cleanup policy definitions with criteria JSON structures
    and traits for common cleanup scenarios.

    **Traits:**

    Cleanup types:
        - ``maven_snapshots=True``: Maven SNAPSHOT cleanup policy
        - ``stale_downloads=True``: Stale download cleanup (90/180 days)
        - ``format_specific=True``: npm format-specific policy

    Example::

        policy = CleanupPolicyFactory(maven_snapshots=True)
        assert policy.format == 'maven2'
        assert policy.criteria['isPrerelease'] is True
    """

    class Meta:
        model = CleanupPolicy

    # -- Field Definitions ---------------------------------------------------

    policy_id = Sequence(lambda n: f"cleanup-policy-{n}")
    name = Sequence(lambda n: f"Cleanup Policy {n}")
    format = None
    criteria = LazyAttribute(lambda o: {"lastDownloadedBefore": 30})
    description = Faker("sentence")
    attributes = LazyAttribute(lambda o: {})

    # -- Traits for cleanup policy variants ----------------------------------

    class Params:
        """Factory parameters for cleanup policy traits."""

        maven_snapshots = factory.Trait(
            format="maven2",
            name="Cleanup Maven Snapshots",
            criteria={
                "regexPattern": ".*-SNAPSHOT",
                "isPrerelease": True,
            },
        )
        stale_downloads = factory.Trait(
            criteria={
                "lastDownloadedBefore": 90,
                "lastBlobUpdatedBefore": 180,
            },
        )
        format_specific = factory.Trait(
            format="npm",
        )


# ===========================================================================
# SystemConfigFactory — Feature F-404
# ===========================================================================


class SystemConfigFactory(BaseFactory):
    """Factory for creating :class:`SystemConfig` test instances.

    Generates system configuration key-value entries with dot-notation keys,
    category groupings, and traits for common configuration types.

    **Traits:**

    Configuration types:
        - ``security=True``: Security configuration (anonymous access)
        - ``system_url=True``: System base URL configuration
        - ``boolean_true=True``: Boolean true config value
        - ``boolean_false=True``: Boolean false config value

    Example::

        config = SystemConfigFactory(security=True)
        assert config.key == 'security.anonymousAccess'
        assert config.value == 'true'
    """

    class Meta:
        model = SystemConfig

    # -- Field Definitions ---------------------------------------------------

    key = Sequence(lambda n: f"test.config.key-{n}")
    value = Faker("word")
    category = fuzzy.FuzzyChoice(
        [
            "security",
            "http",
            "system",
            "email",
            "repository",
            "cleanup",
            "scheduling",
        ]
    )
    description = Faker("sentence")

    # -- Traits for configuration type variants ------------------------------

    class Params:
        """Factory parameters for configuration type traits."""

        security = factory.Trait(
            key="security.anonymousAccess",
            value="true",
            category="security",
            description="Enable anonymous access",
        )
        system_url = factory.Trait(
            key="system.baseUrl",
            value="http://localhost:8081",
            category="system",
            description="System base URL",
        )
        boolean_true = factory.Trait(
            value="true",
        )
        boolean_false = factory.Trait(
            value="false",
        )


# ===========================================================================
# Helper Functions
# ===========================================================================


def seed_factories(seed: int = 42) -> None:
    """Seed all factory-boy factories for deterministic test data generation.

    Calling this function before test execution ensures that Faker-generated
    values and fuzzy choices are reproducible across runs, making tests
    deterministic and debuggable.

    Args:
        seed: Integer seed value.  Default ``42`` provides a well-known
            baseline for consistent test data across all developers and CI.

    Example::

        # In conftest.py:
        from tests.fixtures.test_data import seed_factories
        seed_factories(42)
    """
    Faker._DEFAULT_LOCALE = "en_US"
    factory.random.reseed_random(seed)


def set_session(session: Any) -> None:
    """Configure all factory classes to use the given SQLAlchemy session.

    This function must be called at test setup time (typically in
    ``conftest.py``) to bind all factory classes to the active test
    database session.  Without this, ``create()`` calls will fail
    because ``BaseFactory.Meta.sqlalchemy_session`` is initially ``None``.

    Args:
        session: A SQLAlchemy scoped session instance (e.g., ``db.session``).

    Example::

        # In conftest.py:
        from tests.fixtures.test_data import set_session
        from src.app.extensions import db

        @pytest.fixture(autouse=True)
        def setup_factories(app, init_db):
            with app.app_context():
                set_session(db.session)
                yield
    """
    factory_classes = [
        RepositoryFactory,
        ComponentFactory,
        AssetFactory,
        UserFactory,
        RoleFactory,
        PrivilegeFactory,
        ContentSelectorFactory,
        TaskFactory,
        TaskExecutionFactory,
        AuditEventFactory,
        BlobStoreConfigFactory,
        CleanupPolicyFactory,
        SystemConfigFactory,
    ]
    for factory_class in factory_classes:
        factory_class._meta.sqlalchemy_session = session
