"""
Unit tests for all 14 SQLAlchemy entity models in the Sonatype Nexus Repository
Python/Flask backend.

This module replaces MyBatis 3.5.15 mapper tests from the Java source system.
Tests cover model creation, field validation, relationship loading, JSON
attribute storage, query methods, model-specific behaviours, and cross-model
relationships for:

    Repository, Component, Asset, User, Role, RoleAssignment, Privilege,
    ContentSelector, TaskDefinition, TaskExecution, AuditEvent,
    BlobStoreConfig, CleanupPolicy, SystemConfig

All tests use the in-memory SQLite database configured via ``tests/conftest.py``
shared fixtures (``app``, ``init_db``, ``db_session``, ``clean_db``).

Test Framework: pytest 8.3.4 (replaces JUnit 5.10.1 + Spock from Java source)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

# ---------------------------------------------------------------------------
# Internal model imports — all from src.app.models package hierarchy
# ---------------------------------------------------------------------------
from src.app.models.repository import Repository
from src.app.models.component import Component
from src.app.models.asset import Asset
from src.app.models.user import User
from src.app.models.role import Role, RoleAssignment
from src.app.models.privilege import Privilege
from src.app.models.content_selector import ContentSelector
from src.app.models.task import TaskDefinition, TaskExecution
from src.app.models.audit_event import AuditEvent
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.models.cleanup_policy import CleanupPolicy
from src.app.models.system_config import SystemConfig

# Base model and mixins (tested in TestBaseModelMixins)
from src.app.models.base import (
    BaseModel,
    JSONAttributesMixin,
    SoftDeleteMixin,
    TimestampMixin,
)

# Flask-SQLAlchemy database instance — used for session operations
from src.app.extensions import db


# ============================================================================
# 1. Repository Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestRepositoryModel:
    """Tests for the Repository SQLAlchemy model (F-101, F-102, F-404)."""

    def test_create_repository(self, app):
        """Create a repository with all required fields and verify persistence."""
        with app.app_context():
            repo = Repository(
                name="test-repo",
                format="maven2",
                type="hosted",
                blob_store_name="default",
                online=True,
            )
            db.session.add(repo)
            db.session.commit()

            fetched = db.session.query(Repository).filter_by(name="test-repo").first()
            assert fetched is not None
            assert fetched.name == "test-repo"
            assert fetched.format == "maven2"
            assert fetched.type == "hosted"
            assert fetched.blob_store_name == "default"
            assert fetched.online is True

    @pytest.mark.parametrize(
        "fmt",
        ["maven2", "npm", "docker", "nuget", "pypi", "apt", "raw"],
    )
    def test_repository_formats(self, app, fmt):
        """All 7 repository formats are accepted and persisted correctly."""
        with app.app_context():
            repo = Repository(
                name=f"repo-{fmt}",
                format=fmt,
                type="hosted",
                blob_store_name="default",
                online=True,
            )
            db.session.add(repo)
            db.session.commit()

            fetched = db.session.query(Repository).filter_by(name=f"repo-{fmt}").first()
            assert fetched is not None
            assert fetched.format == fmt

    @pytest.mark.parametrize("repo_type", ["hosted", "proxy", "group"])
    def test_repository_types(self, app, repo_type):
        """All 3 repository types are accepted and persisted correctly."""
        with app.app_context():
            repo = Repository(
                name=f"repo-{repo_type}",
                format="maven2",
                type=repo_type,
                blob_store_name="default",
                online=True,
            )
            db.session.add(repo)
            db.session.commit()

            fetched = db.session.query(Repository).filter_by(
                name=f"repo-{repo_type}"
            ).first()
            assert fetched is not None
            assert fetched.type == repo_type

    def test_repository_primary_key_is_name(self, app):
        """Repository PK is 'name'; duplicate names raise IntegrityError."""
        with app.app_context():
            repo1 = Repository(
                name="unique-repo",
                format="maven2",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo1)
            db.session.commit()

            repo2 = Repository(
                name="unique-repo",
                format="npm",
                type="proxy",
                blob_store_name="other",
            )
            db.session.add(repo2)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_repository_attributes_json(self, app):
        """Attributes JSON field stores and retrieves dict properly."""
        with app.app_context():
            attrs = {"proxy": {"remote_url": "https://repo1.maven.org"}}
            repo = Repository(
                name="proxy-repo",
                format="maven2",
                type="proxy",
                blob_store_name="default",
                attributes=attrs,
            )
            db.session.add(repo)
            db.session.commit()

            fetched = db.session.query(Repository).filter_by(name="proxy-repo").first()
            assert fetched.attributes is not None
            assert fetched.attributes["proxy"]["remote_url"] == "https://repo1.maven.org"

    def test_repository_default_online_true(self, app):
        """Default online status is True when not explicitly set."""
        with app.app_context():
            repo = Repository(
                name="default-online-repo",
                format="raw",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            fetched = db.session.query(Repository).filter_by(
                name="default-online-repo"
            ).first()
            assert fetched.online is True

    def test_repository_relationships(self, app):
        """Repository has one-to-many relationships to Component and Asset."""
        with app.app_context():
            repo = Repository(
                name="rel-repo",
                format="maven2",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            comp = Component(
                repository_name="rel-repo",
                namespace="org.test",
                name="lib",
                version="1.0",
            )
            db.session.add(comp)
            db.session.commit()

            asset = Asset(
                repository_name="rel-repo",
                path="/org/test/lib/1.0/lib-1.0.jar",
                content_type="application/java-archive",
                size=512,
            )
            db.session.add(asset)
            db.session.commit()

            fetched_repo = db.session.query(Repository).filter_by(name="rel-repo").first()
            assert fetched_repo.components.count() == 1
            assert fetched_repo.assets.count() == 1

    def test_repository_repr(self, app):
        """__repr__ returns a meaningful string containing name, format, type."""
        with app.app_context():
            repo = Repository(
                name="maven-central",
                format="maven2",
                type="proxy",
                blob_store_name="default",
            )
            repr_str = repr(repo)
            assert "maven-central" in repr_str
            assert "maven2" in repr_str
            assert "proxy" in repr_str

    def test_repository_to_dict(self, app):
        """to_dict() returns a dictionary with all expected keys."""
        with app.app_context():
            repo = Repository(
                name="dict-repo",
                format="npm",
                type="hosted",
                blob_store_name="default",
                online=True,
            )
            db.session.add(repo)
            db.session.commit()

            result = repo.to_dict()
            assert isinstance(result, dict)
            assert result["name"] == "dict-repo"
            assert result["format"] == "npm"
            assert result["type"] == "hosted"
            assert result["blob_store_name"] == "default"
            assert result["online"] is True


# ============================================================================
# 2. Component Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestComponentModel:
    """Tests for the Component SQLAlchemy model (F-101, F-103)."""

    def _create_repo(self, app, name="comp-test-repo"):
        """Helper to create a parent repository."""
        repo = Repository(
            name=name,
            format="maven2",
            type="hosted",
            blob_store_name="default",
        )
        db.session.add(repo)
        db.session.commit()
        return repo

    def test_create_component(self, app):
        """Create a component with auto-incremented id and all fields."""
        with app.app_context():
            self._create_repo(app)
            comp = Component(
                repository_name="comp-test-repo",
                namespace="org.apache.commons",
                name="commons-lang3",
                version="3.14.0",
            )
            db.session.add(comp)
            db.session.commit()

            assert comp.id is not None
            assert comp.id > 0
            fetched = db.session.query(Component).filter_by(id=comp.id).first()
            assert fetched.repository_name == "comp-test-repo"
            assert fetched.namespace == "org.apache.commons"
            assert fetched.name == "commons-lang3"
            assert fetched.version == "3.14.0"

    def test_component_composite_unique(self, app):
        """Composite unique constraint on (repository_name, namespace, name, version)."""
        with app.app_context():
            self._create_repo(app, name="uniq-repo")
            comp1 = Component(
                repository_name="uniq-repo",
                namespace="org.test",
                name="lib",
                version="1.0",
            )
            db.session.add(comp1)
            db.session.commit()

            comp2 = Component(
                repository_name="uniq-repo",
                namespace="org.test",
                name="lib",
                version="1.0",
            )
            db.session.add(comp2)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_component_namespace_nullable(self, app):
        """Namespace can be None (e.g., npm packages without scope)."""
        with app.app_context():
            self._create_repo(app, name="ns-null-repo")
            comp = Component(
                repository_name="ns-null-repo",
                namespace=None,
                name="lodash",
                version="4.17.21",
            )
            db.session.add(comp)
            db.session.commit()

            fetched = db.session.query(Component).filter_by(id=comp.id).first()
            assert fetched.namespace is None

    def test_component_relationships(self, app):
        """Component has FK relationship to Repository and one-to-many to Asset."""
        with app.app_context():
            self._create_repo(app, name="crel-repo")
            comp = Component(
                repository_name="crel-repo",
                namespace="org.test",
                name="lib",
                version="1.0",
            )
            db.session.add(comp)
            db.session.commit()

            asset = Asset(
                repository_name="crel-repo",
                component_id=comp.id,
                path="/org/test/lib/1.0/lib-1.0.jar",
                content_type="application/java-archive",
                size=256,
            )
            db.session.add(asset)
            db.session.commit()

            fetched = db.session.query(Component).filter_by(id=comp.id).first()
            assert fetched.repository is not None
            assert fetched.repository.name == "crel-repo"
            assert fetched.assets.count() == 1

    def test_component_attributes_json(self, app):
        """Component attributes JSON field stores and retrieves metadata."""
        with app.app_context():
            self._create_repo(app, name="attr-repo")
            comp = Component(
                repository_name="attr-repo",
                namespace="org.test",
                name="lib",
                version="1.0",
                attributes={"packaging": "jar", "classifier": "sources"},
            )
            db.session.add(comp)
            db.session.commit()

            fetched = db.session.query(Component).filter_by(id=comp.id).first()
            assert fetched.attributes["packaging"] == "jar"
            assert fetched.attributes["classifier"] == "sources"

    def test_component_query_by_coordinates(self, app):
        """Query components by namespace/name/version combination."""
        with app.app_context():
            self._create_repo(app, name="qry-repo")
            comp = Component(
                repository_name="qry-repo",
                namespace="com.example",
                name="mylib",
                version="2.0.0",
            )
            db.session.add(comp)
            db.session.commit()

            result = (
                db.session.query(Component)
                .filter_by(
                    repository_name="qry-repo",
                    namespace="com.example",
                    name="mylib",
                    version="2.0.0",
                )
                .first()
            )
            assert result is not None
            assert result.id == comp.id


# ============================================================================
# 3. Asset Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestAssetModel:
    """Tests for the Asset SQLAlchemy model (F-101, F-103, F-204)."""

    def _setup_repo_and_component(self, app, repo_name="asset-repo"):
        """Helper to create a parent repository and component."""
        repo = Repository(
            name=repo_name,
            format="maven2",
            type="hosted",
            blob_store_name="default",
        )
        db.session.add(repo)
        db.session.commit()
        comp = Component(
            repository_name=repo_name,
            namespace="org.test",
            name="lib",
            version="1.0",
        )
        db.session.add(comp)
        db.session.commit()
        return repo, comp

    def test_create_asset(self, app):
        """Create an asset with all core fields and verify persistence."""
        with app.app_context():
            repo, comp = self._setup_repo_and_component(app)
            asset = Asset(
                repository_name=repo.name,
                component_id=comp.id,
                path="/org/test/lib/1.0/lib-1.0.jar",
                content_type="application/java-archive",
                checksum_sha1="aabbccdd" * 5,
                size=2048,
                blob_ref="default@asset-repo/org/test/lib/1.0/lib-1.0.jar",
            )
            db.session.add(asset)
            db.session.commit()

            fetched = db.session.query(Asset).filter_by(id=asset.id).first()
            assert fetched is not None
            assert fetched.id > 0
            assert fetched.path == "/org/test/lib/1.0/lib-1.0.jar"
            assert fetched.content_type == "application/java-archive"
            assert fetched.blob_ref is not None

    def test_asset_component_nullable(self, app):
        """Component FK can be None (orphan assets like maven-metadata.xml)."""
        with app.app_context():
            repo = Repository(
                name="orphan-repo",
                format="maven2",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            asset = Asset(
                repository_name="orphan-repo",
                component_id=None,
                path="/maven-metadata.xml",
                content_type="application/xml",
                size=100,
            )
            db.session.add(asset)
            db.session.commit()

            fetched = db.session.query(Asset).filter_by(id=asset.id).first()
            assert fetched.component_id is None
            assert fetched.component is None

    def test_asset_path_unique_per_repo(self, app):
        """Path is unique within a repository (composite unique on repo + path)."""
        with app.app_context():
            repo = Repository(
                name="dup-repo",
                format="maven2",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            a1 = Asset(
                repository_name="dup-repo",
                path="/same/path.jar",
                size=100,
            )
            db.session.add(a1)
            db.session.commit()

            a2 = Asset(
                repository_name="dup-repo",
                path="/same/path.jar",
                size=200,
            )
            db.session.add(a2)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_asset_checksums(self, app):
        """Checksum fields (sha1, sha256, md5) store and retrieve correctly."""
        with app.app_context():
            repo = Repository(
                name="chk-repo",
                format="raw",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            sha1_val = "a" * 40
            sha256_val = "b" * 64
            md5_val = "c" * 32
            asset = Asset(
                repository_name="chk-repo",
                path="/file.bin",
                checksum_sha1=sha1_val,
                checksum_sha256=sha256_val,
                checksum_md5=md5_val,
                size=512,
            )
            db.session.add(asset)
            db.session.commit()

            fetched = db.session.query(Asset).filter_by(id=asset.id).first()
            assert fetched.checksum_sha1 == sha1_val
            assert fetched.checksum_sha256 == sha256_val
            assert fetched.checksum_md5 == md5_val

    def test_asset_size_big_integer(self, app):
        """Size field handles large values (BigInteger for multi-GB files)."""
        with app.app_context():
            repo = Repository(
                name="big-repo",
                format="docker",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            large_size = 10 * 1024 * 1024 * 1024  # 10 GB
            asset = Asset(
                repository_name="big-repo",
                path="/layers/sha256-big.tar.gz",
                size=large_size,
            )
            db.session.add(asset)
            db.session.commit()

            fetched = db.session.query(Asset).filter_by(id=asset.id).first()
            assert fetched.size == large_size

    def test_asset_last_downloaded(self, app):
        """last_downloaded datetime field stores and retrieves correctly."""
        with app.app_context():
            repo = Repository(
                name="dl-repo",
                format="raw",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            now = datetime.now(timezone.utc)
            asset = Asset(
                repository_name="dl-repo",
                path="/downloaded.bin",
                size=64,
                last_downloaded=now,
            )
            db.session.add(asset)
            db.session.commit()

            fetched = db.session.query(Asset).filter_by(id=asset.id).first()
            assert fetched.last_downloaded is not None

    def test_asset_relationships(self, app):
        """Asset has FK relationships to Component and Repository."""
        with app.app_context():
            repo, comp = self._setup_repo_and_component(app, repo_name="arel-repo")
            asset = Asset(
                repository_name="arel-repo",
                component_id=comp.id,
                path="/org/test/lib/1.0/lib-1.0.pom",
                content_type="application/xml",
                size=128,
            )
            db.session.add(asset)
            db.session.commit()

            fetched = db.session.query(Asset).filter_by(id=asset.id).first()
            assert fetched.repository is not None
            assert fetched.repository.name == "arel-repo"
            assert fetched.component is not None
            assert fetched.component.id == comp.id


# ============================================================================
# 4. User Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestUserModel:
    """Tests for the User SQLAlchemy model (F-301, F-303, F-304)."""

    def test_create_user(self, app):
        """Create a user with user_id PK, password_hash, status, and email."""
        with app.app_context():
            user = User(
                user_id="testuser",
                password_hash="hashed_pw_placeholder",
                status="active",
                email="test@example.com",
            )
            db.session.add(user)
            db.session.commit()

            fetched = db.session.query(User).filter_by(user_id="testuser").first()
            assert fetched is not None
            assert fetched.user_id == "testuser"
            assert fetched.status == "active"
            assert fetched.email == "test@example.com"

    @pytest.mark.parametrize("status", ["active", "disabled", "locked"])
    def test_user_status_values(self, app, status):
        """All three status values (active, disabled, locked) are accepted."""
        with app.app_context():
            user = User(
                user_id=f"user-{status}",
                status=status,
            )
            db.session.add(user)
            db.session.commit()

            fetched = db.session.query(User).filter_by(user_id=f"user-{status}").first()
            assert fetched.status == status

    def test_user_api_key_unique(self, app):
        """api_key field has a unique constraint."""
        with app.app_context():
            u1 = User(
                user_id="apiuser1",
                api_key="unique-key-123",
                status="active",
            )
            db.session.add(u1)
            db.session.commit()

            u2 = User(
                user_id="apiuser2",
                api_key="unique-key-123",
                status="active",
            )
            db.session.add(u2)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_user_external_id(self, app):
        """external_id field stores LDAP DN or SSO subject for federated users."""
        with app.app_context():
            user = User(
                user_id="ldap-user",
                external_id="CN=John Doe,OU=Users,DC=example,DC=com",
                status="active",
            )
            db.session.add(user)
            db.session.commit()

            fetched = db.session.query(User).filter_by(user_id="ldap-user").first()
            assert fetched.external_id == "CN=John Doe,OU=Users,DC=example,DC=com"

    def test_user_role_many_to_many(self, app):
        """Many-to-many relationship to Role via RoleAssignment junction table."""
        with app.app_context():
            user = User(user_id="role-user", status="active")
            db.session.add(user)
            db.session.commit()

            role = Role(
                role_id="nx-admin",
                name="Administrator",
                privileges=["nx-all"],
                source="internal",
            )
            db.session.add(role)
            db.session.commit()

            assignment = RoleAssignment(user_id="role-user", role_id="nx-admin")
            db.session.add(assignment)
            db.session.commit()

            fetched_user = db.session.query(User).filter_by(user_id="role-user").first()
            roles_list = fetched_user.roles.all()
            assert len(roles_list) == 1
            assert roles_list[0].role_id == "nx-admin"

    def test_user_to_dict_excludes_sensitive(self, app):
        """to_dict() MUST exclude password_hash and api_key."""
        with app.app_context():
            user = User(
                user_id="secret-user",
                password_hash="super-secret-hash",
                api_key="secret-api-key",
                status="active",
                email="secret@example.com",
            )
            db.session.add(user)
            db.session.commit()

            result = user.to_dict()
            assert "password_hash" not in result
            assert "api_key" not in result
            assert result["user_id"] == "secret-user"
            assert result["email"] == "secret@example.com"

    def test_user_attributes_json(self, app):
        """Attributes JSON field stores extensible user metadata."""
        with app.app_context():
            user = User(
                user_id="attrs-user",
                status="active",
                attributes={"theme": "dark", "language": "en"},
            )
            db.session.add(user)
            db.session.commit()

            fetched = db.session.query(User).filter_by(user_id="attrs-user").first()
            assert fetched.attributes["theme"] == "dark"
            assert fetched.attributes["language"] == "en"


# ============================================================================
# 5. Role and RoleAssignment Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestRoleModel:
    """Tests for the Role and RoleAssignment SQLAlchemy models (F-301)."""

    def test_create_role(self, app):
        """Create a role with role_id PK, name, description, privileges, source."""
        with app.app_context():
            role = Role(
                role_id="nx-admin",
                name="Administrator",
                description="Full system administration",
                privileges=["nx-all"],
                source="internal",
            )
            db.session.add(role)
            db.session.commit()

            fetched = db.session.query(Role).filter_by(role_id="nx-admin").first()
            assert fetched is not None
            assert fetched.name == "Administrator"
            assert fetched.description == "Full system administration"
            assert fetched.source == "internal"

    def test_role_privileges_json_array(self, app):
        """Privileges field stores a JSON array of privilege IDs."""
        with app.app_context():
            privs = ["nx-repository-admin-*", "nx-component-upload", "nx-search-read"]
            role = Role(
                role_id="deploy-role",
                name="Deployer",
                privileges=privs,
                source="internal",
            )
            db.session.add(role)
            db.session.commit()

            fetched = db.session.query(Role).filter_by(role_id="deploy-role").first()
            assert isinstance(fetched.privileges, list)
            assert len(fetched.privileges) == 3
            assert "nx-component-upload" in fetched.privileges

    @pytest.mark.parametrize("source", ["internal", "external"])
    def test_role_source_values(self, app, source):
        """Both 'internal' and 'external' source values are accepted."""
        with app.app_context():
            role = Role(
                role_id=f"role-{source}",
                name=f"Role {source}",
                privileges=[],
                source=source,
            )
            db.session.add(role)
            db.session.commit()

            fetched = db.session.query(Role).filter_by(role_id=f"role-{source}").first()
            assert fetched.source == source

    def test_role_assignment_composite_pk(self, app):
        """RoleAssignment has composite PK (user_id + role_id)."""
        with app.app_context():
            user = User(user_id="pk-user", status="active")
            db.session.add(user)
            role = Role(
                role_id="pk-role",
                name="PK Role",
                privileges=[],
                source="internal",
            )
            db.session.add(role)
            db.session.commit()

            ra1 = RoleAssignment(user_id="pk-user", role_id="pk-role")
            db.session.add(ra1)
            db.session.commit()

            # Duplicate should fail
            ra2 = RoleAssignment(user_id="pk-user", role_id="pk-role")
            db.session.add(ra2)
            with pytest.raises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_role_assignment_junction(self, app):
        """Create junction records linking multiple users and roles."""
        with app.app_context():
            u1 = User(user_id="junction-u1", status="active")
            u2 = User(user_id="junction-u2", status="active")
            r1 = Role(
                role_id="junction-r1",
                name="Role 1",
                privileges=[],
                source="internal",
            )
            r2 = Role(
                role_id="junction-r2",
                name="Role 2",
                privileges=[],
                source="internal",
            )
            db.session.add_all([u1, u2, r1, r2])
            db.session.commit()

            db.session.add_all([
                RoleAssignment(user_id="junction-u1", role_id="junction-r1"),
                RoleAssignment(user_id="junction-u1", role_id="junction-r2"),
                RoleAssignment(user_id="junction-u2", role_id="junction-r1"),
            ])
            db.session.commit()

            count = db.session.query(RoleAssignment).count()
            assert count >= 3

    def test_user_roles_through_assignment(self, app):
        """Verify M2M traversal from User → RoleAssignment → Role."""
        with app.app_context():
            user = User(user_id="traverse-user", status="active")
            db.session.add(user)
            r1 = Role(
                role_id="traverse-r1",
                name="Traverse Role 1",
                privileges=["priv-a"],
                source="internal",
            )
            r2 = Role(
                role_id="traverse-r2",
                name="Traverse Role 2",
                privileges=["priv-b"],
                source="internal",
            )
            db.session.add_all([r1, r2])
            db.session.commit()

            db.session.add_all([
                RoleAssignment(user_id="traverse-user", role_id="traverse-r1"),
                RoleAssignment(user_id="traverse-user", role_id="traverse-r2"),
            ])
            db.session.commit()

            fetched_user = db.session.query(User).filter_by(
                user_id="traverse-user"
            ).first()
            role_ids = [r.role_id for r in fetched_user.roles.all()]
            assert "traverse-r1" in role_ids
            assert "traverse-r2" in role_ids


# ============================================================================
# 6. Privilege Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestPrivilegeModel:
    """Tests for the Privilege SQLAlchemy model (F-301)."""

    def test_create_privilege(self, app):
        """Create a privilege with privilege_id PK, type, name, properties JSON."""
        with app.app_context():
            priv = Privilege(
                privilege_id="nx-all",
                type="wildcard",
                name="All Privileges",
                properties={},
            )
            db.session.add(priv)
            db.session.commit()

            fetched = db.session.query(Privilege).filter_by(
                privilege_id="nx-all"
            ).first()
            assert fetched is not None
            assert fetched.type == "wildcard"
            assert fetched.name == "All Privileges"

    @pytest.mark.parametrize(
        "priv_type",
        [
            "application",
            "repository-admin",
            "repository-view",
            "repository-content-selector",
            "wildcard",
        ],
    )
    def test_privilege_types(self, app, priv_type):
        """All 5 privilege types are accepted and persisted correctly."""
        with app.app_context():
            priv = Privilege(
                privilege_id=f"priv-{priv_type}",
                type=priv_type,
                name=f"Test {priv_type}",
                properties={},
            )
            db.session.add(priv)
            db.session.commit()

            fetched = db.session.query(Privilege).filter_by(
                privilege_id=f"priv-{priv_type}"
            ).first()
            assert fetched.type == priv_type

    def test_privilege_properties_json(self, app):
        """Properties JSON stores format, repository, and actions."""
        with app.app_context():
            props = {
                "format": "maven2",
                "repository": "*",
                "actions": ["read", "browse"],
            }
            priv = Privilege(
                privilege_id="nx-repo-view-maven",
                type="repository-view",
                name="Maven View",
                properties=props,
            )
            db.session.add(priv)
            db.session.commit()

            fetched = db.session.query(Privilege).filter_by(
                privilege_id="nx-repo-view-maven"
            ).first()
            assert fetched.properties["format"] == "maven2"
            assert fetched.properties["repository"] == "*"
            assert "read" in fetched.properties["actions"]

    def test_privilege_matches_method(self, app):
        """Test matches() method for privilege matching logic."""
        with app.app_context():
            # Wildcard privilege matches everything
            wildcard = Privilege(
                privilege_id="nx-all",
                type="wildcard",
                name="All",
                properties={},
            )
            assert wildcard.matches("maven2", "my-repo", "read") is True

            # Application privilege does not match repository-level checks
            app_priv = Privilege(
                privilege_id="nx-users-all",
                type="application",
                name="User Admin",
                properties={"domain": "users", "actions": ["create", "read"]},
            )
            assert app_priv.matches("maven2", "my-repo", "read") is False

            # Repository-view with format and repo matching
            view_priv = Privilege(
                privilege_id="nx-repo-view",
                type="repository-view",
                name="Maven View",
                properties={
                    "format": "maven2",
                    "repository": "*",
                    "actions": ["read", "browse"],
                },
            )
            assert view_priv.matches("maven2", "any-repo", "read") is True
            assert view_priv.matches("maven2", "any-repo", "delete") is False
            assert view_priv.matches("npm", "any-repo", "read") is False

            # Repository-admin (no action restriction)
            admin_priv = Privilege(
                privilege_id="nx-repo-admin",
                type="repository-admin",
                name="Docker Admin",
                properties={"format": "docker", "repository": "docker-hosted"},
            )
            assert admin_priv.matches("docker", "docker-hosted", "delete") is True
            assert admin_priv.matches("docker", "other-repo", "delete") is False


# ============================================================================
# 7. ContentSelector Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestContentSelectorModel:
    """Tests for the ContentSelector SQLAlchemy model (F-301 Tier 3)."""

    def test_create_content_selector(self, app):
        """Create a ContentSelector with selector_id PK, name, type, expression."""
        with app.app_context():
            cs = ContentSelector(
                selector_id="maven-internal",
                name="Maven Internal Packages",
                type="csel",
                expression='format == "maven2" and path =^ "/org/internal/"',
            )
            db.session.add(cs)
            db.session.commit()

            fetched = db.session.query(ContentSelector).filter_by(
                selector_id="maven-internal"
            ).first()
            assert fetched is not None
            assert fetched.name == "Maven Internal Packages"
            assert fetched.type == "csel"

    @pytest.mark.parametrize("sel_type", ["csel", "jexl"])
    def test_content_selector_types(self, app, sel_type):
        """Both 'csel' and 'jexl' expression types are accepted."""
        with app.app_context():
            cs = ContentSelector(
                selector_id=f"sel-{sel_type}",
                name=f"Selector {sel_type}",
                type=sel_type,
                expression='format == "npm"',
            )
            db.session.add(cs)
            db.session.commit()

            fetched = db.session.query(ContentSelector).filter_by(
                selector_id=f"sel-{sel_type}"
            ).first()
            assert fetched.type == sel_type

    def test_content_selector_expression(self, app):
        """Expression field stores CSEL expressions with special operators."""
        with app.app_context():
            expr = 'format == "maven2" and path =^ "/org/apache/"'
            cs = ContentSelector(
                selector_id="apache-maven",
                name="Apache Maven",
                type="csel",
                expression=expr,
            )
            db.session.add(cs)
            db.session.commit()

            fetched = db.session.query(ContentSelector).filter_by(
                selector_id="apache-maven"
            ).first()
            assert fetched.expression == expr
            assert "=^" in fetched.expression


# ============================================================================
# 8. Task Models Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestTaskModels:
    """Tests for TaskDefinition and TaskExecution SQLAlchemy models (F-402)."""

    def test_create_task_definition(self, app):
        """TaskDefinition with task_id PK, type, name, cron, enabled, config JSON."""
        with app.app_context():
            task = TaskDefinition(
                task_id="cleanup-daily",
                type="repository.cleanup",
                name="Daily Cleanup",
                cron_expression="0 0 * * *",
                enabled=True,
                configuration={"repositoryName": "*"},
            )
            db.session.add(task)
            db.session.commit()

            fetched = db.session.query(TaskDefinition).filter_by(
                task_id="cleanup-daily"
            ).first()
            assert fetched is not None
            assert fetched.type == "repository.cleanup"
            assert fetched.name == "Daily Cleanup"
            assert fetched.cron_expression == "0 0 * * *"
            assert fetched.enabled is True

    def test_task_definition_configuration_json(self, app):
        """Configuration JSON stores task-specific parameters."""
        with app.app_context():
            config = {
                "repositoryName": "maven-hosted",
                "policyNames": ["cleanup-snapshots"],
                "dryRun": False,
            }
            task = TaskDefinition(
                task_id="cleanup-config",
                type="repository.cleanup",
                name="Config Test",
                configuration=config,
            )
            db.session.add(task)
            db.session.commit()

            fetched = db.session.query(TaskDefinition).filter_by(
                task_id="cleanup-config"
            ).first()
            assert fetched.configuration["repositoryName"] == "maven-hosted"
            assert "cleanup-snapshots" in fetched.configuration["policyNames"]
            assert fetched.configuration["dryRun"] is False

    def test_create_task_execution(self, app):
        """TaskExecution with execution_id auto PK, FK, times, status, duration."""
        with app.app_context():
            task = TaskDefinition(
                task_id="exec-task",
                type="blobstore.compact",
                name="Compact Blobs",
            )
            db.session.add(task)
            db.session.commit()

            now = datetime.now(timezone.utc)
            execution = TaskExecution(
                task_id="exec-task",
                start_time=now,
                status="running",
            )
            db.session.add(execution)
            db.session.commit()

            fetched = db.session.query(TaskExecution).filter_by(
                execution_id=execution.execution_id
            ).first()
            assert fetched is not None
            assert fetched.execution_id > 0
            assert fetched.task_id == "exec-task"
            assert fetched.status == "running"
            assert fetched.start_time is not None

    def test_task_execution_relationship(self, app):
        """FK relationship between TaskExecution and TaskDefinition."""
        with app.app_context():
            task = TaskDefinition(
                task_id="rel-task",
                type="db.backup",
                name="Database Backup",
            )
            db.session.add(task)
            db.session.commit()

            execution = TaskExecution(
                task_id="rel-task",
                start_time=datetime.now(timezone.utc),
                status="ok",
                duration_ms=5000,
            )
            db.session.add(execution)
            db.session.commit()

            fetched = db.session.query(TaskExecution).filter_by(
                execution_id=execution.execution_id
            ).first()
            assert fetched.task_definition is not None
            assert fetched.task_definition.task_id == "rel-task"
            assert fetched.task_definition.name == "Database Backup"

    @pytest.mark.parametrize(
        "status", ["running", "ok", "failed", "canceled", "waiting"]
    )
    def test_task_execution_statuses(self, app, status):
        """Test all execution status values: running, ok, failed, canceled, waiting."""
        with app.app_context():
            task = TaskDefinition(
                task_id=f"status-task-{status}",
                type="test.task",
                name=f"Task {status}",
            )
            db.session.add(task)
            db.session.commit()

            execution = TaskExecution(
                task_id=f"status-task-{status}",
                start_time=datetime.now(timezone.utc),
                status=status,
            )
            db.session.add(execution)
            db.session.commit()

            fetched = db.session.query(TaskExecution).filter_by(
                execution_id=execution.execution_id
            ).first()
            assert fetched.status == status


# ============================================================================
# 9. AuditEvent Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestAuditEventModel:
    """Tests for the AuditEvent SQLAlchemy model (F-303)."""

    def test_create_audit_event(self, app):
        """AuditEvent with event_id auto PK, all fields populated."""
        with app.app_context():
            event = AuditEvent(
                user_id="admin",
                event_type="REPOSITORY_CREATED",
                domain="repository",
                attributes={"repositoryName": "maven-releases", "format": "maven2"},
                ip_address="192.168.1.100",
            )
            db.session.add(event)
            db.session.commit()

            fetched = db.session.query(AuditEvent).filter_by(
                event_id=event.event_id
            ).first()
            assert fetched is not None
            assert fetched.event_id > 0
            assert fetched.user_id == "admin"
            assert fetched.event_type == "REPOSITORY_CREATED"
            assert fetched.domain == "repository"
            assert fetched.ip_address == "192.168.1.100"

    def test_audit_event_immutable(self, app):
        """AuditEvent should be treated as immutable compliance records.

        Verify that once created, the core fields are preserved. The model
        itself does not enforce physical immutability at the ORM level
        (as audit events are permanent records — see docstring), but we
        verify that the data is stored accurately and consistently.
        """
        with app.app_context():
            event = AuditEvent(
                user_id="auditor",
                event_type="CONFIG_CHANGED",
                domain="configuration",
                attributes={"key": "security.anonymousAccess"},
            )
            db.session.add(event)
            db.session.commit()

            original_id = event.event_id
            original_type = event.event_type

            # Re-fetch and verify original data is preserved
            fetched = db.session.query(AuditEvent).filter_by(
                event_id=original_id
            ).first()
            assert fetched.event_type == original_type
            assert fetched.user_id == "auditor"

    def test_audit_event_ip_address_ipv6(self, app):
        """ip_address field handles IPv6 addresses (up to 45 chars)."""
        with app.app_context():
            ipv6 = "2001:0db8:85a3:0000:0000:8a2e:0370:7334"
            event = AuditEvent(
                event_type="AUTHENTICATION_SUCCESS",
                ip_address=ipv6,
            )
            db.session.add(event)
            db.session.commit()

            fetched = db.session.query(AuditEvent).filter_by(
                event_id=event.event_id
            ).first()
            assert fetched.ip_address == ipv6

            # Also test IPv4-mapped IPv6
            ipv6_mapped = "::ffff:192.168.1.1"
            event2 = AuditEvent(
                event_type="AUTHENTICATION_SUCCESS",
                ip_address=ipv6_mapped,
            )
            db.session.add(event2)
            db.session.commit()
            fetched2 = db.session.query(AuditEvent).filter_by(
                event_id=event2.event_id
            ).first()
            assert fetched2.ip_address == ipv6_mapped

    def test_audit_event_attributes_json(self, app):
        """Attributes JSON field stores extensible audit data."""
        with app.app_context():
            payload = {
                "repositoryName": "maven-releases",
                "format": "maven2",
                "type": "hosted",
                "componentCount": 42,
            }
            event = AuditEvent(
                event_type="REPOSITORY_CREATED",
                domain="repository",
                attributes=payload,
            )
            db.session.add(event)
            db.session.commit()

            fetched = db.session.query(AuditEvent).filter_by(
                event_id=event.event_id
            ).first()
            assert fetched.attributes["repositoryName"] == "maven-releases"
            assert fetched.attributes["componentCount"] == 42

    def test_audit_event_timestamp_default(self, app):
        """Timestamp defaults to current UTC time when not explicitly set."""
        with app.app_context():
            before = datetime.now(timezone.utc)
            event = AuditEvent(
                event_type="AUTHENTICATION_SUCCESS",
                user_id="timer",
            )
            db.session.add(event)
            db.session.commit()

            fetched = db.session.query(AuditEvent).filter_by(
                event_id=event.event_id
            ).first()
            assert fetched.timestamp is not None


# ============================================================================
# 10. BlobStoreConfig Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestBlobStoreConfigModel:
    """Tests for the BlobStoreConfig SQLAlchemy model (F-201, F-202)."""

    def test_create_blobstore_config(self, app):
        """BlobStoreConfig with blob_store_name PK, type, config JSON, total_size."""
        with app.app_context():
            bs = BlobStoreConfig(
                blob_store_name="default",
                type="file",
                configuration={"path": "/nexus-data/blobs/default"},
                total_size=0,
            )
            db.session.add(bs)
            db.session.commit()

            fetched = db.session.query(BlobStoreConfig).filter_by(
                blob_store_name="default"
            ).first()
            assert fetched is not None
            assert fetched.type == "file"
            assert fetched.total_size == 0

    @pytest.mark.parametrize("bs_type", ["file", "s3"])
    def test_blobstore_type_values(self, app, bs_type):
        """Both 'file' and 's3' BlobStore types are accepted."""
        with app.app_context():
            config = (
                {"path": "/data/blobs"}
                if bs_type == "file"
                else {"bucket": "nexus-blobs", "region": "us-east-1"}
            )
            bs = BlobStoreConfig(
                blob_store_name=f"store-{bs_type}",
                type=bs_type,
                configuration=config,
            )
            db.session.add(bs)
            db.session.commit()

            fetched = db.session.query(BlobStoreConfig).filter_by(
                blob_store_name=f"store-{bs_type}"
            ).first()
            assert fetched.type == bs_type

    def test_blobstore_configuration_json(self, app):
        """Configuration JSON stores backend-specific settings."""
        with app.app_context():
            s3_config = {
                "bucket": "nexus-blobs-prod",
                "region": "eu-west-1",
                "prefix": "nexus/",
                "accessKeyId": "AKIAIOSFODNN7EXAMPLE",
                "secretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "encryption": {"type": "s3ManagedEncryption", "key": None},
                "forcePathStyle": False,
            }
            bs = BlobStoreConfig(
                blob_store_name="s3-prod",
                type="s3",
                configuration=s3_config,
            )
            db.session.add(bs)
            db.session.commit()

            fetched = db.session.query(BlobStoreConfig).filter_by(
                blob_store_name="s3-prod"
            ).first()
            assert fetched.configuration["bucket"] == "nexus-blobs-prod"
            assert fetched.configuration["region"] == "eu-west-1"
            assert fetched.configuration["accessKeyId"] == "AKIAIOSFODNN7EXAMPLE"

    def test_blobstore_to_dict_secure(self, app):
        """to_dict_secure() MUST mask S3 credentials (access_key, secret_key)."""
        with app.app_context():
            s3_config = {
                "bucket": "secure-bucket",
                "region": "us-east-1",
                "accessKeyId": "AKIAIOSFODNN7EXAMPLE",
                "secretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            }
            bs = BlobStoreConfig(
                blob_store_name="secure-store",
                type="s3",
                configuration=s3_config,
            )
            db.session.add(bs)
            db.session.commit()

            secure_dict = bs.to_dict_secure()
            config_output = secure_dict.get("configuration", {})

            # accessKeyId and secretAccessKey must be masked
            assert config_output.get("accessKeyId") == "********"
            assert config_output.get("secretAccessKey") == "********"

            # Non-sensitive fields remain intact
            assert config_output.get("bucket") == "secure-bucket"
            assert config_output.get("region") == "us-east-1"


# ============================================================================
# 11. CleanupPolicy Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestCleanupPolicyModel:
    """Tests for the CleanupPolicy SQLAlchemy model (F-204)."""

    def test_create_cleanup_policy(self, app):
        """CleanupPolicy with policy_id PK, name, format, criteria JSON."""
        with app.app_context():
            policy = CleanupPolicy(
                policy_id="cleanup-snapshots",
                name="Cleanup Maven Snapshots",
                format="maven2",
                criteria={"lastDownloadedBefore": 30, "regexPattern": ".*-SNAPSHOT"},
            )
            db.session.add(policy)
            db.session.commit()

            fetched = db.session.query(CleanupPolicy).filter_by(
                policy_id="cleanup-snapshots"
            ).first()
            assert fetched is not None
            assert fetched.name == "Cleanup Maven Snapshots"
            assert fetched.format == "maven2"

    def test_cleanup_policy_format_nullable(self, app):
        """Format field can be None for format-agnostic policies."""
        with app.app_context():
            policy = CleanupPolicy(
                policy_id="cleanup-all",
                name="Cleanup All Formats",
                format=None,
                criteria={"lastDownloadedBefore": 90},
            )
            db.session.add(policy)
            db.session.commit()

            fetched = db.session.query(CleanupPolicy).filter_by(
                policy_id="cleanup-all"
            ).first()
            assert fetched.format is None

    def test_cleanup_policy_criteria_json(self, app):
        """Criteria JSON stores cleanup rules correctly."""
        with app.app_context():
            criteria = {
                "lastDownloadedBefore": 90,
                "isPrerelease": True,
                "regexPattern": ".*-SNAPSHOT",
            }
            policy = CleanupPolicy(
                policy_id="criteria-test",
                name="Criteria Test",
                criteria=criteria,
            )
            db.session.add(policy)
            db.session.commit()

            fetched = db.session.query(CleanupPolicy).filter_by(
                policy_id="criteria-test"
            ).first()
            assert fetched.criteria["lastDownloadedBefore"] == 90
            assert fetched.criteria["isPrerelease"] is True
            assert "SNAPSHOT" in fetched.criteria["regexPattern"]


# ============================================================================
# 12. SystemConfig Model Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestSystemConfigModel:
    """Tests for the SystemConfig SQLAlchemy model (F-404)."""

    def test_create_system_config(self, app):
        """SystemConfig with key PK, value Text, and category."""
        with app.app_context():
            config = SystemConfig(
                key="system.baseUrl",
                value="http://localhost:8081",
                category="system",
            )
            db.session.add(config)
            db.session.commit()

            fetched = db.session.query(SystemConfig).filter_by(
                key="system.baseUrl"
            ).first()
            assert fetched is not None
            assert fetched.value == "http://localhost:8081"
            assert fetched.category == "system"

    def test_system_config_key_dot_notation(self, app):
        """Key uses dot-notation like 'nexus.proxy.timeout'."""
        with app.app_context():
            config = SystemConfig(
                key="nexus.proxy.timeout",
                value="300",
                category="proxy",
            )
            db.session.add(config)
            db.session.commit()

            fetched = db.session.query(SystemConfig).filter_by(
                key="nexus.proxy.timeout"
            ).first()
            assert fetched is not None
            assert "." in fetched.key

    def test_system_config_type_conversion(self, app):
        """Test type conversion properties: as_bool, as_int, as_list."""
        with app.app_context():
            # as_bool: "true" → True
            bool_config = SystemConfig(
                key="security.anonymousAccess",
                value="true",
                category="security",
            )
            db.session.add(bool_config)
            db.session.commit()
            assert bool_config.as_bool is True

            # as_bool: "false" → False
            bool_config2 = SystemConfig(
                key="security.strictMode",
                value="false",
                category="security",
            )
            db.session.add(bool_config2)
            db.session.commit()
            assert bool_config2.as_bool is False

            # as_int: "3600" → 3600
            int_config = SystemConfig(
                key="http.timeout",
                value="3600",
                category="http",
            )
            db.session.add(int_config)
            db.session.commit()
            assert int_config.as_int == 3600

            # as_list: JSON array string → list
            list_config = SystemConfig(
                key="security.realms",
                value='["NexusAuthenticatingRealm", "LdapRealm"]',
                category="security",
            )
            db.session.add(list_config)
            db.session.commit()
            result_list = list_config.as_list
            assert isinstance(result_list, list)
            assert "NexusAuthenticatingRealm" in result_list
            assert "LdapRealm" in result_list

    def test_system_config_value_text(self, app):
        """Value field handles long text content without truncation."""
        with app.app_context():
            long_value = "x" * 10000
            config = SystemConfig(
                key="system.longConfig",
                value=long_value,
                category="system",
            )
            db.session.add(config)
            db.session.commit()

            fetched = db.session.query(SystemConfig).filter_by(
                key="system.longConfig"
            ).first()
            assert fetched.value == long_value
            assert len(fetched.value) == 10000


# ============================================================================
# 13. Base Model / Mixin Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestBaseModelMixins:
    """Tests for BaseModel, TimestampMixin, SoftDeleteMixin, JSONAttributesMixin."""

    def test_timestamp_mixin(self, app):
        """TimestampMixin adds created_at and updated_at with auto-population."""
        with app.app_context():
            # Repository uses TimestampMixin
            repo = Repository(
                name="ts-repo",
                format="raw",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            fetched = db.session.query(Repository).filter_by(name="ts-repo").first()
            assert fetched.created_at is not None
            assert fetched.updated_at is not None
            assert isinstance(fetched.created_at, datetime)
            assert isinstance(fetched.updated_at, datetime)

    def test_soft_delete_mixin(self, app):
        """SoftDeleteMixin adds deleted_at, is_deleted, soft_delete(), restore()."""
        with app.app_context():
            repo = Repository(
                name="sd-repo",
                format="raw",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            # Initially not deleted
            fetched = db.session.query(Repository).filter_by(name="sd-repo").first()
            assert fetched.is_deleted is False
            assert fetched.deleted_at is None

            # Soft-delete
            fetched.soft_delete()
            refetched = db.session.query(Repository).filter_by(name="sd-repo").first()
            assert refetched.is_deleted is True
            assert refetched.deleted_at is not None

            # Restore
            refetched.restore()
            restored = db.session.query(Repository).filter_by(name="sd-repo").first()
            assert restored.is_deleted is False
            assert restored.deleted_at is None

    def test_json_attributes_mixin(self, app):
        """JSONAttributesMixin provides get/set/remove_attribute methods."""
        with app.app_context():
            repo = Repository(
                name="ja-repo",
                format="raw",
                type="hosted",
                blob_store_name="default",
                attributes={},
            )
            db.session.add(repo)
            db.session.commit()

            # set_attribute
            repo.set_attribute("proxy", {"remoteUrl": "https://example.com"})
            db.session.commit()

            fetched = db.session.query(Repository).filter_by(name="ja-repo").first()
            assert fetched.get_attribute("proxy") == {"remoteUrl": "https://example.com"}

            # remove_attribute
            fetched.remove_attribute("proxy")
            db.session.commit()

            refetched = db.session.query(Repository).filter_by(name="ja-repo").first()
            assert refetched.get_attribute("proxy") is None


# ============================================================================
# 14. Cross-Model Relationship Tests
# ============================================================================


@pytest.mark.usefixtures("app", "init_db", "db_session")
class TestModelRelationships:
    """Tests for cross-model relationships and cascade behaviours."""

    def test_repository_cascade_components(self, app):
        """Deleting a repository cascades to delete its components."""
        with app.app_context():
            repo = Repository(
                name="cascade-repo",
                format="maven2",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            comp = Component(
                repository_name="cascade-repo",
                namespace="org.cascade",
                name="lib",
                version="1.0",
            )
            db.session.add(comp)
            db.session.commit()
            comp_id = comp.id

            # Delete the repository
            db.session.delete(repo)
            db.session.commit()

            # Component should be gone due to cascade
            orphan = db.session.query(Component).filter_by(id=comp_id).first()
            assert orphan is None

    def test_component_cascade_assets(self, app):
        """Deleting a component cascades to delete its assets."""
        with app.app_context():
            repo = Repository(
                name="comp-casc-repo",
                format="maven2",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            comp = Component(
                repository_name="comp-casc-repo",
                namespace="org.test",
                name="lib",
                version="1.0",
            )
            db.session.add(comp)
            db.session.commit()

            asset = Asset(
                repository_name="comp-casc-repo",
                component_id=comp.id,
                path="/org/test/lib/1.0/lib-1.0.jar",
                size=512,
            )
            db.session.add(asset)
            db.session.commit()
            asset_id = asset.id

            # Delete the component
            db.session.delete(comp)
            db.session.commit()

            # Asset should be gone due to cascade
            orphan_asset = db.session.query(Asset).filter_by(id=asset_id).first()
            assert orphan_asset is None

    def test_user_role_assignment_cascade(self, app):
        """Deleting a user should remove associated role assignments."""
        with app.app_context():
            user = User(user_id="del-user", status="active")
            role = Role(
                role_id="del-role",
                name="Delete Role",
                privileges=[],
                source="internal",
            )
            db.session.add_all([user, role])
            db.session.commit()

            assignment = RoleAssignment(user_id="del-user", role_id="del-role")
            db.session.add(assignment)
            db.session.commit()

            # Verify assignment exists
            ra = (
                db.session.query(RoleAssignment)
                .filter_by(user_id="del-user", role_id="del-role")
                .first()
            )
            assert ra is not None

            # Delete the user — cascade should remove assignments
            db.session.delete(user)
            db.session.commit()

            ra_after = (
                db.session.query(RoleAssignment)
                .filter_by(user_id="del-user")
                .first()
            )
            assert ra_after is None

    def test_full_repository_hierarchy(self, app):
        """Create full hierarchy: Repository → Component → Asset, verify traversal."""
        with app.app_context():
            repo = Repository(
                name="full-repo",
                format="maven2",
                type="hosted",
                blob_store_name="default",
            )
            db.session.add(repo)
            db.session.commit()

            comp = Component(
                repository_name="full-repo",
                namespace="com.example",
                name="app",
                version="1.0.0",
            )
            db.session.add(comp)
            db.session.commit()

            asset1 = Asset(
                repository_name="full-repo",
                component_id=comp.id,
                path="/com/example/app/1.0.0/app-1.0.0.jar",
                content_type="application/java-archive",
                size=1024,
            )
            asset2 = Asset(
                repository_name="full-repo",
                component_id=comp.id,
                path="/com/example/app/1.0.0/app-1.0.0.pom",
                content_type="application/xml",
                size=256,
            )
            db.session.add_all([asset1, asset2])
            db.session.commit()

            # Traverse downward: Repository → Components → Assets
            fetched_repo = db.session.query(Repository).filter_by(
                name="full-repo"
            ).first()
            assert fetched_repo.components.count() == 1

            fetched_comp = fetched_repo.components.first()
            assert fetched_comp.name == "app"
            assert fetched_comp.assets.count() == 2

            # Traverse upward: Asset → Component → Repository
            fetched_asset = db.session.query(Asset).filter_by(
                id=asset1.id
            ).first()
            assert fetched_asset.component is not None
            assert fetched_asset.component.name == "app"
            assert fetched_asset.repository is not None
            assert fetched_asset.repository.name == "full-repo"
