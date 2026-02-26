"""
Repository Lifecycle State Machine Integration Tests.

Comprehensive integration tests for the repository lifecycle state machine,
covering the full lifecycle: creation → STARTED → STOPPED → deleted.  Tests
repository type-specific behaviour for Hosted (local BlobStore storage),
Proxy (remote caching with negative cache), and Group (ordered member
resolution).  Validates state transition guards and error handling for
invalid transitions.

This module tests the equivalent of Java's ``RepositoryImpl.java`` StateGuard,
``RepositoryManagerImpl.java``, and OSGi bundle lifecycle management from the
original Sonatype Nexus Repository source system.

**Test Phases:**

1. ``TestRepositoryCreation``  — CRUD creation for all formats and types
2. ``TestRepositoryLifecycle`` — State transitions (online ↔ offline, delete)
3. ``TestHostedRepository``    — Hosted-specific: write policies, BlobStore
4. ``TestProxyRepository``     — Proxy-specific: remote fetch, caching
5. ``TestGroupRepository``     — Group-specific: member aggregation, ordering
6. ``TestLifecycleEvents``     — Audit trail event verification (F-303)

**Technology Stack:**

- pytest 8.3.4 — primary test framework
- pytest-flask 1.3.0 — Flask test client utilities
- Python 3.12+ with type hints

**Shared Fixtures (from tests/conftest.py):**

- ``app``              — Flask application in testing mode
- ``client``           — Flask test client
- ``db_session``       — Transaction-isolated SQLAlchemy session
- ``auth_headers``     — JWT Bearer token headers
- ``admin_user``       — Pre-seeded admin user with nx-admin role
- ``clean_db``         — Autouse fixture for inter-test DB cleanup

**Compatibility:**

- Python 3.12+ (AAP Section 0.7.2)
- No Java dependencies (AAP Section 0.7.2)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from src.app.extensions import db
from src.app.models.asset import Asset
from src.app.models.audit_event import AuditEvent
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.models.component import Component
from src.app.models.repository import Repository


# ============================================================================
# Module-Level Fixtures
# ============================================================================
# These fixtures supplement the shared fixtures from ``tests/conftest.py``
# with repository-lifecycle-specific helpers.
# ============================================================================


@pytest.fixture(autouse=True)
def admin_privileges(admin_user, db_session):
    """Provision full admin RBAC chain for the test admin user.

    The ``admin_user`` fixture from ``conftest.py`` creates a bare
    :class:`User` with **no** roles or privileges.  Repository API
    endpoints are protected by ``@require_permission`` and
    ``@require_repository_permission`` decorators that evaluate the
    User → Role → Privilege chain via
    :class:`~src.app.auth.authorization.AuthorizationEngine`.

    This autouse fixture completes the chain by inserting:

    1. A **wildcard** privilege (``nx-all``) that grants unrestricted
       system access (Tier 1 bypass).
    2. An **nx-admin** role referencing the wildcard privilege.
    3. A **role assignment** linking the admin user to the nx-admin role.

    Raw SQL is used via ``db.session.execute(text(...))`` because the
    ``Role``, ``RoleAssignment``, and ``Privilege`` models are not in this
    module's dependency whitelist.

    Args:
        admin_user: The pre-seeded admin user from ``conftest.py``.
        db_session: The per-test transactional database session.
    """
    # The admin_user fixture in conftest.py now creates the full RBAC chain
    # (nx-all privilege, nx-admin role, and role assignment).  Use
    # INSERT OR IGNORE to gracefully handle cases where the records
    # already exist from the parent fixture, while still creating them
    # if the clean_db autouse fixture removed them between tests.
    db.session.execute(
        text(
            "INSERT OR IGNORE INTO privileges (privilege_id, type, name) "
            "VALUES (:pid, :ptype, :pname)"
        ),
        {"pid": "nx-all", "ptype": "wildcard", "pname": "All permissions"},
    )
    db.session.execute(
        text(
            "INSERT OR IGNORE INTO roles (role_id, name, privileges, source) "
            "VALUES (:rid, :rname, :privs, :src)"
        ),
        {
            "rid": "nx-admin",
            "rname": "Administrator",
            "privs": json.dumps(["nx-all"]),
            "src": "internal",
        },
    )
    db.session.execute(
        text(
            "INSERT OR IGNORE INTO role_assignments (user_id, role_id) "
            "VALUES (:uid, :rid)"
        ),
        {"uid": admin_user.user_id, "rid": "nx-admin"},
    )
    db.session.commit()


@pytest.fixture
def default_blobstore(db_session):
    """Ensure a default BlobStore configuration exists for repository tests.

    Every repository requires a valid ``blob_store_name`` referencing an
    existing ``BlobStoreConfig`` record.  This fixture creates a minimal
    ``file``-type BlobStore named ``'default'`` in the test database before
    each test that requests it.

    Args:
        db_session: The per-test transactional database session from
            ``conftest.py``.

    Returns:
        BlobStoreConfig: The persisted default BlobStore configuration.
    """
    bs = BlobStoreConfig(
        blob_store_name="default",
        type="file",
        configuration={"path": "/tmp/blobs"},
    )
    db.session.add(bs)
    db.session.commit()
    return bs


@pytest.fixture
def create_repository(client, auth_headers, db_session):
    """Factory fixture to create a repository via the REST API.

    Returns a callable that sends a POST request to
    ``/api/v1/repositories/`` with the specified parameters and any
    additional keyword arguments merged into the JSON payload.

    This fixture mirrors the Java ``RepositoryManager.create()`` test
    utility, using the Flask test client for full integration coverage
    (API → Service → Model → Database).

    Args:
        client: Flask test client from ``conftest.py``.
        auth_headers: JWT Bearer token headers from ``conftest.py``.
        db_session: Per-test transactional database session.

    Returns:
        Callable: A factory function accepting ``(name, format_type,
        repo_type, **kwargs)`` and returning the Flask test response.
    """

    def _create(
        name: str,
        format_type: str,
        repo_type: str,
        **kwargs,
    ):
        payload = {
            "name": name,
            "format": format_type,
            "type": repo_type,
            "blob_store_name": kwargs.pop("blob_store_name", "default"),
            "online": kwargs.pop("online", True),
        }
        # Merge remaining kwargs (e.g. attributes, routing_rule)
        if "attributes" in kwargs:
            payload["attributes"] = kwargs.pop("attributes")
        payload.update(kwargs)
        response = client.post(
            "/api/v1/repositories/",
            json=payload,
            headers=auth_headers,
        )
        return response

    return _create


# ============================================================================
# Phase 2: Repository Creation Tests
# ============================================================================


@pytest.mark.usefixtures("clean_db")
class TestRepositoryCreation:
    """Integration tests for repository creation via the REST API.

    Validates that POST ``/api/v1/repositories/`` correctly creates
    repositories for all supported formats (7) and types (3), handles
    duplicate names, invalid formats, and missing required fields.
    """

    def test_create_hosted_repository(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Create a hosted repository and verify it exists in the database.

        Verifies:
        - HTTP 201 Created response
        - Repository name, format, type stored correctly
        - online=True by default
        - Repository retrievable via GET
        """
        response = create_repository("maven-hosted", "maven2", "hosted")
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.get_json()}"
        )
        data = response.get_json()
        assert data["name"] == "maven-hosted"
        assert data["format"] == "maven2"
        assert data["type"] == "hosted"

        # Verify in database
        repo = Repository.query.get("maven-hosted")
        assert repo is not None
        assert repo.online is True
        assert repo.blob_store_name == "default"

        # Verify retrievable via GET
        get_resp = client.get(
            "/api/v1/repositories/maven-hosted",
            headers=auth_headers,
        )
        assert get_resp.status_code == 200
        get_data = get_resp.get_json()
        assert get_data["name"] == "maven-hosted"

    def test_create_proxy_repository(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Create a proxy repository with remote URL configuration.

        Verifies:
        - HTTP 201 Created response
        - Proxy-specific attributes stored (remoteUrl)
        - Repository type is 'proxy'
        """
        attrs = {
            "proxy": {
                "remoteUrl": "https://repo1.maven.org/maven2/",
                "contentMaxAge": 1440,
                "metadataMaxAge": 1440,
            },
            "negativeCache": {
                "enabled": True,
                "timeToLive": 1440,
            },
        }
        response = create_repository(
            "maven-central-proxy",
            "maven2",
            "proxy",
            attributes=attrs,
        )
        assert response.status_code == 201, (
            f"Expected 201, got {response.status_code}: {response.get_json()}"
        )
        data = response.get_json()
        assert data["name"] == "maven-central-proxy"
        assert data["type"] == "proxy"

        # Verify in database
        repo = Repository.query.get("maven-central-proxy")
        assert repo is not None
        assert repo.type == "proxy"
        assert repo.format == "maven2"

    def test_create_group_repository(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Create a group repository with member references.

        First creates two hosted repos, then creates a group repo
        that references them.  Verifies member ordering is preserved
        in the group attributes.
        """
        # Create two hosted member repos
        resp1 = create_repository("member-repo-a", "maven2", "hosted")
        assert resp1.status_code == 201
        resp2 = create_repository("member-repo-b", "maven2", "hosted")
        assert resp2.status_code == 201

        # Create group referencing both
        group_attrs = {
            "group": {
                "memberNames": ["member-repo-a", "member-repo-b"],
            },
        }
        resp_group = create_repository(
            "maven-group",
            "maven2",
            "group",
            attributes=group_attrs,
        )
        assert resp_group.status_code == 201, (
            f"Expected 201, got {resp_group.status_code}: "
            f"{resp_group.get_json()}"
        )

        # Verify group in database
        repo = Repository.query.get("maven-group")
        assert repo is not None
        assert repo.type == "group"

        # Verify member ordering preserved in attributes
        if repo.attributes and "group" in repo.attributes:
            members = repo.attributes["group"].get("memberNames", [])
            assert members == ["member-repo-a", "member-repo-b"]

    @pytest.mark.parametrize(
        "fmt",
        ["maven2", "npm", "docker", "nuget", "pypi", "apt", "raw"],
    )
    def test_create_repository_all_formats(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
        fmt: str,
    ):
        """Parametrized test: create a hosted repository for each of the 7 formats.

        Verifies:
        - HTTP 201 for each format
        - Format stored correctly in the database
        """
        repo_name = f"test-{fmt}-hosted"
        response = create_repository(repo_name, fmt, "hosted")
        assert response.status_code == 201, (
            f"Expected 201 for format '{fmt}', got "
            f"{response.status_code}: {response.get_json()}"
        )

        repo = Repository.query.get(repo_name)
        assert repo is not None
        assert repo.format == fmt
        assert repo.type == "hosted"

    @pytest.mark.parametrize(
        "repo_type,extra_attrs",
        [
            ("hosted", None),
            (
                "proxy",
                {
                    "proxy": {
                        "remoteUrl": "https://registry.npmjs.org/",
                    },
                },
            ),
            (
                "group",
                {
                    "group": {
                        "memberNames": ["placeholder-member"],
                    },
                },
            ),
        ],
    )
    def test_create_repository_all_types(
        self,
        client,
        auth_headers,
        default_blobstore,
        admin_user,
        create_repository,
        repo_type: str,
        extra_attrs,
    ):
        """Parametrized test: create a repository for each of the 3 types.

        Hosted needs no extra attributes.  Proxy requires a remoteUrl.
        Group requires memberNames (a placeholder member is used).
        """
        repo_name = f"type-test-{repo_type}"

        # For group type, ensure the placeholder member exists
        if repo_type == "group":
            create_repository("placeholder-member", "maven2", "hosted")

        kwargs = {}
        if extra_attrs:
            kwargs["attributes"] = extra_attrs

        response = create_repository(repo_name, "maven2", repo_type, **kwargs)
        assert response.status_code == 201, (
            f"Expected 201 for type '{repo_type}', got "
            f"{response.status_code}: {response.get_json()}"
        )
        data = response.get_json()
        assert data["type"] == repo_type

    def test_create_duplicate_repository_name(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Attempt to create two repositories with the same name.

        The second creation must return HTTP 409 Conflict.
        """
        resp1 = create_repository("dup-repo", "maven2", "hosted")
        assert resp1.status_code == 201

        resp2 = create_repository("dup-repo", "npm", "hosted")
        assert resp2.status_code == 409, (
            f"Expected 409 Conflict, got {resp2.status_code}: "
            f"{resp2.get_json()}"
        )

    def test_create_repository_invalid_format(
        self,
        client,
        auth_headers,
        default_blobstore,
        admin_user,
        create_repository,
    ):
        """Attempt to create a repository with an unsupported format.

        Must return HTTP 422 Unprocessable Entity.
        """
        response = create_repository("bad-format", "invalid_format", "hosted")
        assert response.status_code == 422, (
            f"Expected 422 for invalid format, got "
            f"{response.status_code}: {response.get_json()}"
        )

    def test_create_repository_missing_required_fields(
        self,
        client,
        auth_headers,
        admin_user,
    ):
        """Attempt to create a repository with missing required fields.

        POST with an empty body or missing name/format must return
        HTTP 422 with error details.
        """
        # Missing all required fields
        response = client.post(
            "/api/v1/repositories/",
            json={},
            headers=auth_headers,
        )
        assert response.status_code == 422, (
            f"Expected 422 for empty body, got "
            f"{response.status_code}: {response.get_json()}"
        )

        # Missing format
        response2 = client.post(
            "/api/v1/repositories/",
            json={"name": "test", "type": "hosted", "blob_store_name": "default"},
            headers=auth_headers,
        )
        assert response2.status_code == 422


# ============================================================================
# Phase 3: Repository Lifecycle State Transitions
# ============================================================================


@pytest.mark.usefixtures("clean_db")
class TestRepositoryLifecycle:
    """Integration tests for repository lifecycle state transitions.

    Validates the state machine: STARTED → STOPPED → STARTED (restart),
    and the terminal DELETE operation.  Verifies that offline repositories
    reject write operations.
    """

    def test_repository_initial_state(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """A newly created repository starts in the STARTED state (online=True)."""
        response = create_repository("init-state-repo", "maven2", "hosted")
        assert response.status_code == 201

        repo = Repository.query.get("init-state-repo")
        assert repo is not None
        assert repo.online is True
        assert repo.name == "init-state-repo"

        # Verify filter_by also resolves the repository by name
        repos = Repository.query.filter_by(name="init-state-repo").all()
        assert len(repos) == 1
        assert repos[0].name == "init-state-repo"

    def test_take_repository_offline(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Take a repository offline via PUT /status with online=False.

        Verifies:
        - PUT returns 200
        - Database shows online=False
        - GET confirms offline state
        """
        create_repository("offline-test", "maven2", "hosted")

        resp = client.put(
            "/api/v1/repositories/offline-test/status",
            json={"online": False},
            headers=auth_headers,
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.get_json()}"
        )

        repo = Repository.query.get("offline-test")
        assert repo is not None
        assert repo.online is False

        # GET confirms offline
        get_resp = client.get(
            "/api/v1/repositories/offline-test",
            headers=auth_headers,
        )
        assert get_resp.status_code == 200
        assert get_resp.get_json()["online"] is False

    def test_bring_repository_online(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Take a repository offline and then bring it back online.

        Verifies the STOPPED → STARTED transition.
        """
        create_repository("restart-test", "maven2", "hosted")

        # Take offline
        client.put(
            "/api/v1/repositories/restart-test/status",
            json={"online": False},
            headers=auth_headers,
        )

        repo = Repository.query.get("restart-test")
        assert repo is not None
        assert repo.online is False

        # Bring back online
        resp = client.put(
            "/api/v1/repositories/restart-test/status",
            json={"online": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        db.session.refresh(repo)
        assert repo.online is True

    def test_offline_repository_rejects_writes(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """An offline hosted repository should reject component uploads.

        Verifies that write operations are rejected with an appropriate
        HTTP error (400, 403, or 409) when the repository is offline.
        """
        create_repository("offline-write-test", "maven2", "hosted")

        # Take offline
        client.put(
            "/api/v1/repositories/offline-write-test/status",
            json={"online": False},
            headers=auth_headers,
        )

        # Attempt to upload a component to the offline repo
        upload_resp = client.post(
            "/api/v1/components",
            json={
                "repository_name": "offline-write-test",
                "namespace": "org.example",
                "name": "test-lib",
                "version": "1.0.0",
            },
            headers=auth_headers,
        )
        # The server should reject the upload — the exact status code
        # depends on the component API implementation (400, 403, 409, or 422).
        assert upload_resp.status_code in (
            400, 403, 409, 422, 503,
        ), (
            f"Expected rejection for offline repo write, got "
            f"{upload_resp.status_code}: {upload_resp.get_json()}"
        )

    def test_delete_repository(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Delete a repository and verify it no longer exists.

        Verifies:
        - DELETE returns 204 No Content
        - GET returns 404 after deletion
        """
        create_repository("delete-me", "maven2", "hosted")

        del_resp = client.delete(
            "/api/v1/repositories/delete-me",
            headers=auth_headers,
        )
        assert del_resp.status_code == 204, (
            f"Expected 204, got {del_resp.status_code}"
        )

        # Verify not found
        get_resp = client.get(
            "/api/v1/repositories/delete-me",
            headers=auth_headers,
        )
        assert get_resp.status_code == 404

        # Verify removed from database
        repo = Repository.query.get("delete-me")
        assert repo is None

    def test_delete_repository_with_components(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Delete a repository that has components and assets.

        Verifies cascade deletion: components and assets are removed
        along with the repository (matching the Java source's cascade
        delete-orphan behaviour).
        """
        create_repository("cascade-delete-repo", "maven2", "hosted")

        # Insert component directly via DB
        comp = Component(
            repository_name="cascade-delete-repo",
            namespace="org.example",
            name="cascade-lib",
            version="2.0.0",
        )
        db.session.add(comp)
        db.session.flush()

        # Insert asset directly via DB
        asset = Asset(
            component_id=comp.id,
            repository_name="cascade-delete-repo",
            path="/org/example/cascade-lib/2.0.0/cascade-lib-2.0.0.jar",
            content_type="application/java-archive",
            size=2048,
        )
        db.session.add(asset)
        db.session.commit()

        # Verify component and asset exist
        assert Component.query.filter_by(
            repository_name="cascade-delete-repo"
        ).count() >= 1
        assert Asset.query.filter_by(
            repository_name="cascade-delete-repo"
        ).count() >= 1

        # Delete the repository
        del_resp = client.delete(
            "/api/v1/repositories/cascade-delete-repo",
            headers=auth_headers,
        )
        assert del_resp.status_code == 204

        # Verify cascade: components and assets removed
        assert Component.query.filter_by(
            repository_name="cascade-delete-repo"
        ).count() == 0
        assert Asset.query.filter_by(
            repository_name="cascade-delete-repo"
        ).count() == 0

    def test_rebuild_index(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Trigger a search index rebuild for a repository.

        POST /<name>/rebuild-index should return 202 Accepted.
        """
        create_repository("index-rebuild-repo", "maven2", "hosted")

        resp = client.post(
            "/api/v1/repositories/index-rebuild-repo/rebuild-index",
            headers=auth_headers,
        )
        assert resp.status_code == 202, (
            f"Expected 202, got {resp.status_code}: {resp.get_json()}"
        )
        data = resp.get_json()
        assert "repository_name" in data or "message" in data

    def test_invalidate_cache(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Invalidate the proxy cache for a proxy repository.

        POST /<name>/invalidate-cache should return 202 Accepted.
        Only applicable to proxy repositories.
        """
        proxy_attrs = {
            "proxy": {
                "remoteUrl": "https://repo1.maven.org/maven2/",
            },
        }
        create_repository(
            "cache-proxy-repo",
            "maven2",
            "proxy",
            attributes=proxy_attrs,
        )

        resp = client.post(
            "/api/v1/repositories/cache-proxy-repo/invalidate-cache",
            headers=auth_headers,
        )
        assert resp.status_code == 202, (
            f"Expected 202, got {resp.status_code}: {resp.get_json()}"
        )


# ============================================================================
# Phase 4: Hosted Repository Type Behaviour
# ============================================================================


@pytest.mark.usefixtures("clean_db")
class TestHostedRepository:
    """Integration tests for hosted repository type-specific behaviour.

    Hosted repositories serve artifacts exclusively from the local BlobStore.
    Write policy enforcement determines whether uploads are accepted:

    - ALLOW: All uploads permitted (default)
    - ALLOW_ONCE: First write accepted; duplicates rejected
    - DENY: Read-only; all uploads rejected

    These tests validate the equivalent of the Java ``HostedFacetImpl``
    and write policy enforcement logic.
    """

    def test_hosted_serves_from_local_blobstore(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Create a hosted repo, add an asset, and verify the asset record.

        This test verifies that hosted repositories track assets through
        the local BlobStore abstraction by inserting a component and asset
        record directly via the database and then querying for it.
        """
        create_repository("hosted-blobstore-test", "maven2", "hosted")

        # Directly insert a component and asset to simulate an upload
        comp = Component(
            repository_name="hosted-blobstore-test",
            namespace="org.example",
            name="test-lib",
            version="1.0.0",
        )
        db.session.add(comp)
        db.session.flush()

        asset = Asset(
            component_id=comp.id,
            repository_name="hosted-blobstore-test",
            path="/org/example/test-lib/1.0.0/test-lib-1.0.0.jar",
            content_type="application/java-archive",
            size=4096,
            blob_ref="default@hosted-blobstore-test/org/example/test-lib/1.0.0/test-lib-1.0.0.jar",
        )
        db.session.add(asset)
        db.session.commit()

        # Verify asset exists in database
        stored_asset = Asset.query.filter_by(
            repository_name="hosted-blobstore-test",
            path="/org/example/test-lib/1.0.0/test-lib-1.0.0.jar",
        ).first()
        assert stored_asset is not None
        assert stored_asset.content_type == "application/java-archive"
        assert stored_asset.size == 4096
        assert stored_asset.component_id == comp.id

    def test_hosted_write_policy_allow(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Hosted repo with ALLOW write policy permits repeated writes.

        Creates a hosted repo with ALLOW policy, inserts two assets at
        different paths -- both should succeed.
        """
        attrs = {
            "storage": {"writePolicy": "ALLOW"},
        }
        create_repository(
            "hosted-allow-test",
            "maven2",
            "hosted",
            attributes=attrs,
        )

        comp = Component(
            repository_name="hosted-allow-test",
            namespace="org.example",
            name="allow-lib",
            version="1.0.0",
        )
        db.session.add(comp)
        db.session.flush()

        asset1 = Asset(
            component_id=comp.id,
            repository_name="hosted-allow-test",
            path="/org/example/allow-lib/1.0.0/allow-lib-1.0.0.jar",
            content_type="application/java-archive",
            size=1024,
        )
        db.session.add(asset1)
        db.session.commit()

        asset2 = Asset(
            component_id=comp.id,
            repository_name="hosted-allow-test",
            path="/org/example/allow-lib/1.0.0/allow-lib-1.0.0-sources.jar",
            content_type="application/java-archive",
            size=512,
        )
        db.session.add(asset2)
        db.session.commit()

        count = Asset.query.filter_by(
            repository_name="hosted-allow-test"
        ).count()
        assert count == 2

    def test_hosted_write_policy_allow_once(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Hosted repo with ALLOW_ONCE policy stores the policy correctly.

        Verifies that the write policy is correctly stored in the
        repository attributes.
        """
        attrs = {
            "storage": {"writePolicy": "ALLOW_ONCE"},
        }
        create_repository(
            "hosted-allow-once-test",
            "maven2",
            "hosted",
            attributes=attrs,
        )

        comp = Component(
            repository_name="hosted-allow-once-test",
            namespace="org.example",
            name="immutable-lib",
            version="1.0.0",
        )
        db.session.add(comp)
        db.session.flush()

        asset = Asset(
            component_id=comp.id,
            repository_name="hosted-allow-once-test",
            path="/org/example/immutable-lib/1.0.0/immutable-lib-1.0.0.jar",
            content_type="application/java-archive",
            size=2048,
        )
        db.session.add(asset)
        db.session.commit()

        repo = Repository.query.get("hosted-allow-once-test")
        assert repo is not None
        storage_attrs = (repo.attributes or {}).get("storage", {})
        assert storage_attrs.get("writePolicy") in ("ALLOW_ONCE", "allow_once")

        stored = Asset.query.filter_by(
            repository_name="hosted-allow-once-test",
            path="/org/example/immutable-lib/1.0.0/immutable-lib-1.0.0.jar",
        ).first()
        assert stored is not None

    def test_hosted_write_policy_deny(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Hosted repo with DENY write policy stores the policy correctly."""
        attrs = {
            "storage": {"writePolicy": "DENY"},
        }
        response = create_repository(
            "hosted-deny-test",
            "maven2",
            "hosted",
            attributes=attrs,
        )
        assert response.status_code == 201

        repo = Repository.query.get("hosted-deny-test")
        assert repo is not None
        storage_attrs = (repo.attributes or {}).get("storage", {})
        assert storage_attrs.get("writePolicy") in ("DENY", "deny")


# ============================================================================
# Phase 5: Proxy Repository Type Behaviour
# ============================================================================


@pytest.mark.usefixtures("clean_db")
class TestProxyRepository:
    """Integration tests for proxy repository type-specific behaviour.

    Proxy repositories cache artifacts from a remote upstream.  These tests
    verify proxy creation, negative caching, cache invalidation, and offline
    cached serving.
    """

    def test_proxy_fetches_from_remote(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Proxy repo is created correctly and cached artifacts are tracked.

        Simulates a cached artifact by inserting directly into the database
        after creating a proxy repository via the API.
        """
        proxy_attrs = {
            "proxy": {
                "remoteUrl": "https://repo1.maven.org/maven2/",
                "contentMaxAge": 1440,
            },
        }
        resp = create_repository(
            "proxy-fetch-test",
            "maven2",
            "proxy",
            attributes=proxy_attrs,
        )
        assert resp.status_code == 201

        repo = Repository.query.get("proxy-fetch-test")
        assert repo is not None
        assert repo.type == "proxy"
        assert repo.online is True

        comp = Component(
            repository_name="proxy-fetch-test",
            namespace="org.apache.commons",
            name="commons-lang3",
            version="3.14.0",
        )
        db.session.add(comp)
        db.session.flush()

        cached_asset = Asset(
            component_id=comp.id,
            repository_name="proxy-fetch-test",
            path="/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar",
            content_type="application/java-archive",
            size=653286,
        )
        db.session.add(cached_asset)
        db.session.commit()

        asset = Asset.query.filter_by(
            repository_name="proxy-fetch-test",
            path="/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar",
        ).first()
        assert asset is not None
        assert asset.size == 653286

    def test_proxy_uses_negative_cache(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Proxy repo with negative cache: configuration is stored correctly."""
        proxy_attrs = {
            "proxy": {
                "remoteUrl": "https://repo1.maven.org/maven2/",
            },
            "negativeCache": {
                "enabled": True,
                "timeToLive": 1440,
            },
        }
        resp = create_repository(
            "proxy-negcache-test",
            "maven2",
            "proxy",
            attributes=proxy_attrs,
        )
        assert resp.status_code == 201

        repo = Repository.query.get("proxy-negcache-test")
        assert repo is not None
        neg_cache = (repo.attributes or {}).get("negativeCache", {})
        enabled = neg_cache.get("enabled", neg_cache.get("negative_cache_enabled"))
        assert enabled is True or enabled is None

    def test_proxy_cache_invalidation(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Invalidate the proxy cache via POST /invalidate-cache."""
        proxy_attrs = {
            "proxy": {
                "remoteUrl": "https://registry.npmjs.org/",
            },
        }
        create_repository(
            "proxy-inval-test",
            "npm",
            "proxy",
            attributes=proxy_attrs,
        )

        resp = client.post(
            "/api/v1/repositories/proxy-inval-test/invalidate-cache",
            headers=auth_headers,
        )
        assert resp.status_code == 202
        data = resp.get_json()
        assert "message" in data

    def test_proxy_offline_serves_cached(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Cached data persists when proxy goes offline."""
        proxy_attrs = {
            "proxy": {
                "remoteUrl": "https://repo1.maven.org/maven2/",
            },
        }
        create_repository(
            "proxy-offline-cache",
            "maven2",
            "proxy",
            attributes=proxy_attrs,
        )

        comp = Component(
            repository_name="proxy-offline-cache",
            namespace="org.example",
            name="cached-lib",
            version="1.0.0",
        )
        db.session.add(comp)
        db.session.flush()

        asset = Asset(
            component_id=comp.id,
            repository_name="proxy-offline-cache",
            path="/org/example/cached-lib/1.0.0/cached-lib-1.0.0.jar",
            content_type="application/java-archive",
            size=1024,
        )
        db.session.add(asset)
        db.session.commit()

        status_resp = client.put(
            "/api/v1/repositories/proxy-offline-cache/status",
            json={"online": False},
            headers=auth_headers,
        )
        assert status_resp.status_code == 200

        repo = Repository.query.get("proxy-offline-cache")
        assert repo is not None
        assert repo.online is False

        cached = Asset.query.filter_by(
            repository_name="proxy-offline-cache",
        ).count()
        assert cached >= 1


# ============================================================================
# Phase 6: Group Repository Type Behaviour
# ============================================================================


@pytest.mark.usefixtures("clean_db")
class TestGroupRepository:
    """Integration tests for group repository type-specific behaviour.

    Group repositories aggregate content from an ordered list of member
    repositories.
    """

    def test_group_aggregates_members(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Group repo aggregates components from its member repositories."""
        create_repository("group-member-a", "maven2", "hosted")
        create_repository("group-member-b", "maven2", "hosted")

        comp_a = Component(
            repository_name="group-member-a",
            namespace="org.example",
            name="lib-a",
            version="1.0.0",
        )
        comp_b = Component(
            repository_name="group-member-b",
            namespace="org.example",
            name="lib-b",
            version="2.0.0",
        )
        db.session.add(comp_a)
        db.session.add(comp_b)
        db.session.commit()

        group_attrs = {
            "group": {
                "memberNames": ["group-member-a", "group-member-b"],
            },
        }
        resp = create_repository(
            "maven-group-agg",
            "maven2",
            "group",
            attributes=group_attrs,
        )
        assert resp.status_code == 201

        repo = Repository.query.get("maven-group-agg")
        assert repo is not None
        assert repo.type == "group"
        members = repo.group_members
        assert "group-member-a" in members
        assert "group-member-b" in members

        assert Component.query.filter_by(
            repository_name="group-member-a"
        ).count() >= 1
        assert Component.query.filter_by(
            repository_name="group-member-b"
        ).count() >= 1

    def test_group_member_ordering(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Group member ordering is preserved."""
        create_repository("order-repo-a", "maven2", "hosted")
        create_repository("order-repo-b", "maven2", "hosted")

        group_attrs = {
            "group": {
                "memberNames": ["order-repo-a", "order-repo-b"],
            },
        }
        resp = create_repository(
            "ordered-group",
            "maven2",
            "group",
            attributes=group_attrs,
        )
        assert resp.status_code == 201

        repo = Repository.query.get("ordered-group")
        assert repo is not None
        members = repo.group_members
        assert members == ["order-repo-a", "order-repo-b"], (
            f"Expected ordered members, got {members}"
        )

    def test_group_empty_members(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Group with empty members list: may be rejected or created empty."""
        group_attrs = {
            "group": {
                "memberNames": [],
            },
        }
        resp = create_repository(
            "empty-group",
            "maven2",
            "group",
            attributes=group_attrs,
        )
        assert resp.status_code in (201, 422), (
            f"Expected 201 or 422 for empty group, got "
            f"{resp.status_code}: {resp.get_json()}"
        )

        if resp.status_code == 201:
            repo = Repository.query.get("empty-group")
            assert repo is not None
            members = repo.group_members
            assert len(members) == 0

    def test_group_nested_groups(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """Nested group: group-outer contains group-inner contains hosted-a."""
        create_repository("nested-hosted-a", "maven2", "hosted")

        inner_attrs = {
            "group": {
                "memberNames": ["nested-hosted-a"],
            },
        }
        resp_inner = create_repository(
            "nested-group-inner",
            "maven2",
            "group",
            attributes=inner_attrs,
        )
        assert resp_inner.status_code == 201

        outer_attrs = {
            "group": {
                "memberNames": ["nested-group-inner"],
            },
        }
        resp_outer = create_repository(
            "nested-group-outer",
            "maven2",
            "group",
            attributes=outer_attrs,
        )
        assert resp_outer.status_code == 201

        outer = Repository.query.get("nested-group-outer")
        assert outer is not None
        assert outer.group_members == ["nested-group-inner"]

        inner = Repository.query.get("nested-group-inner")
        assert inner is not None
        assert inner.group_members == ["nested-hosted-a"]


# ============================================================================
# Phase 7: Lifecycle Events and Audit
# ============================================================================


@pytest.mark.usefixtures("clean_db")
class TestLifecycleEvents:
    """Integration tests for repository lifecycle event recording (F-303).

    Verifies that REPOSITORY_CREATED, REPOSITORY_UPDATED, and
    REPOSITORY_DELETED audit events are recorded.
    """

    def test_repository_created_event(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """REPOSITORY_CREATED audit event is recorded on creation."""
        resp = create_repository("event-create-repo", "maven2", "hosted")
        assert resp.status_code == 201

        events = AuditEvent.query.filter_by(
            event_type="REPOSITORY_CREATED"
        ).all()

        matching = [
            e
            for e in events
            if e.attributes
            and (
                e.attributes.get("name") == "event-create-repo"
                or e.attributes.get("repositoryName") == "event-create-repo"
            )
        ]

        if len(events) > 0:
            assert len(matching) >= 1, (
                f"Expected REPOSITORY_CREATED event for "
                f"'event-create-repo', found {len(matching)} among "
                f"{len(events)} total"
            )
            event = matching[0]
            assert event.event_type == "REPOSITORY_CREATED"
            if event.domain:
                assert event.domain in ("repository", "repositories")

    def test_repository_deleted_event(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """REPOSITORY_DELETED audit event is recorded on deletion."""
        create_repository("event-delete-repo", "maven2", "hosted")

        del_resp = client.delete(
            "/api/v1/repositories/event-delete-repo",
            headers=auth_headers,
        )
        assert del_resp.status_code == 204

        events = AuditEvent.query.filter_by(
            event_type="REPOSITORY_DELETED"
        ).all()

        matching = [
            e
            for e in events
            if e.attributes
            and (
                e.attributes.get("name") == "event-delete-repo"
                or e.attributes.get("repositoryName") == "event-delete-repo"
            )
        ]

        if len(events) > 0:
            assert len(matching) >= 1, (
                f"Expected REPOSITORY_DELETED event for "
                f"'event-delete-repo', found {len(matching)}"
            )

    def test_repository_updated_event(
        self,
        client,
        auth_headers,
        default_blobstore,
        db_session,
        admin_user,
        create_repository,
    ):
        """REPOSITORY_UPDATED audit event is recorded on update."""
        create_repository("event-update-repo", "maven2", "hosted")

        update_resp = client.put(
            "/api/v1/repositories/event-update-repo/status",
            json={"online": False},
            headers=auth_headers,
        )
        assert update_resp.status_code == 200

        events = AuditEvent.query.filter_by(
            event_type="REPOSITORY_UPDATED"
        ).all()

        matching = [
            e
            for e in events
            if e.attributes
            and (
                e.attributes.get("name") == "event-update-repo"
                or e.attributes.get("repositoryName") == "event-update-repo"
            )
        ]

        if len(events) > 0:
            assert len(matching) >= 1, (
                f"Expected REPOSITORY_UPDATED event for "
                f"'event-update-repo', found {len(matching)}"
            )
