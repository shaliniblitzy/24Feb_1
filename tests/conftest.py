"""
Shared Test Fixtures and Configuration — ``tests/conftest.py``

This module is the central pytest conftest that provides shared fixtures for
the entire Nexus Repository test suite.  It replaces the Java test
configuration infrastructure (JUnit 5.10.1 + Spock test setup, Apache Shiro
test realms, MyBatis test session management) with Python/Flask equivalents.

**Testing Stack (AAP Section 0.6.1):**

+----------------------------+--------------+-------------------------------+
| Package                    | Version      | Replaces                      |
+============================+==============+===============================+
| pytest                     | 8.3.4        | JUnit 5.10.1 + Spock          |
+----------------------------+--------------+-------------------------------+
| pytest-flask               | 1.3.0        | Custom Flask test utilities    |
+----------------------------+--------------+-------------------------------+
| pytest-mock                | 3.14.0       | Mockito 5.8.0                 |
+----------------------------+--------------+-------------------------------+
| factory-boy                | 3.3.1        | Test data factories            |
+----------------------------+--------------+-------------------------------+
| Faker                      | 33.1.0       | Fake data generation           |
+----------------------------+--------------+-------------------------------+
| moto                       | 5.0.27       | AWS service mocking (S3)       |
+----------------------------+--------------+-------------------------------+

**Fixture Categories:**

1. **Application Fixtures** — ``app``: session-scoped Flask application
   created via the Application Factory pattern.
2. **Database Fixtures** — ``init_db``, ``db_session``, ``clean_db``:
   schema creation, per-test transactional isolation, and automatic cleanup.
3. **HTTP Client Fixtures** — ``client``, ``runner``: Flask test client
   and CLI test runner.
4. **Authentication Fixtures** — ``admin_user``, ``auth_headers``,
   ``api_key_headers``: user creation and JWT / API key header generation.
5. **Sample Data Fixtures** — ``sample_repository``, ``sample_component``,
   ``sample_asset``: pre-populated domain objects for test scenarios.
6. **Mock Service Fixtures** — ``mock_elasticsearch``, ``mock_s3``,
   ``mock_blobstore``: isolated external service replacements.
7. **Utility Helpers** — ``get_json_response``, ``post_json``: HTTP test
   convenience functions.

**Compatibility:**

- Python 3.12+ (AAP Section 0.7.2)
- No Java dependencies (AAP Section 0.7.2)
- Discovered automatically by pytest for all tests in ``tests/unit/``
  and ``tests/integration/`` subdirectories.

Exports:
    app               : Session-scoped Flask application fixture.
    init_db           : Session-scoped database schema initialiser.
    db_session        : Function-scoped transactional database session.
    client            : Function-scoped Flask test client.
    runner            : Function-scoped Flask CLI test runner.
    admin_user        : Admin user fixture for authenticated endpoint tests.
    auth_headers      : JWT Bearer token authentication headers.
    api_key_headers   : API key authentication headers.
    sample_repository : Pre-populated repository for tests.
    sample_component  : Pre-populated component for tests.
    sample_asset      : Pre-populated asset for tests.
    mock_elasticsearch: Mocked Elasticsearch client.
    mock_s3           : Mocked AWS S3 client (via moto).
    mock_blobstore    : Temporary filesystem BlobStore path.
    clean_db          : Autouse fixture for inter-test database cleanup.
    get_json_response : GET + JSON parse helper function.
    post_json         : POST + JSON helper function.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import jwt
import pytest

# ---------------------------------------------------------------------------
# Internal imports — Application Factory and Extensions
# ---------------------------------------------------------------------------
# ``create_app`` is the Flask Application Factory (replaces Launcher.java +
# Karaf bootstrap).  ``db`` is the Flask-SQLAlchemy instance (replaces
# MyBatis 3.5.15 + HikariCP 4.0.3 from the Java source).
# ---------------------------------------------------------------------------

from src.app.factory import create_app
from src.app.extensions import db

# ---------------------------------------------------------------------------
# Internal imports — SQLAlchemy Models
# ---------------------------------------------------------------------------
# Importing the models package ensures all 14 entity tables are registered
# with SQLAlchemy's metadata, which is a prerequisite for ``db.create_all()``
# in the ``init_db`` fixture.  Individual model classes are imported for use
# in data-creation fixtures (admin_user, sample_repository, etc.).
# ---------------------------------------------------------------------------

import src.app.models  # noqa: F401 — registers all model tables
from src.app.models import (
    Repository,
    Component,
    Asset,
    User,
    Role,
    Privilege,
    ContentSelector,
    TaskDefinition,
    TaskExecution,
    AuditEvent,
    BlobStoreConfig,
    CleanupPolicy,
    SystemConfig,
)


# ============================================================================
# 1. Application Fixture — Session-Scoped
# ============================================================================
# Replaces Java Launcher.java + Karaf OSGi container bootstrap.
# Uses the Flask Application Factory pattern (create_app) to create a
# configured application instance with TestingConfig:
#   - TESTING = True
#   - SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'
#   - SCHEDULER_ENABLED = False
#   - ELASTICSEARCH_URL = None
# ============================================================================


@pytest.fixture(scope="session")
def app():
    """Create the Flask application for testing.

    The application is created once per test session (``scope='session'``)
    for performance — creating the Flask app, initialising extensions, and
    registering blueprints is expensive and need not be repeated for every
    individual test function.

    The ``'testing'`` configuration is loaded which sets:
        - ``TESTING = True`` — enables Flask test mode
        - ``SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'`` — ephemeral DB
        - ``SECRET_KEY`` and ``JWT_SECRET_KEY`` — fixed for deterministic tokens
        - ``SCHEDULER_ENABLED = False`` — no background scheduling
        - ``ELASTICSEARCH_URL = None`` — no Elasticsearch connection

    Yields:
        flask.Flask: A fully configured Flask application instance.
    """
    # Set environment hint for any code that inspects os.environ directly
    os.environ.setdefault("FLASK_CONFIG", "testing")

    application = create_app("testing")

    # Sanity assertions — verify the testing configuration was loaded
    assert application.config["TESTING"] is True, (
        "Expected TESTING=True in TestingConfig"
    )
    assert "sqlite" in application.config["SQLALCHEMY_DATABASE_URI"], (
        "Expected in-memory SQLite URI in TestingConfig"
    )

    yield application


# ============================================================================
# 2. Database Fixtures
# ============================================================================
# Replaces MyBatis 3.5.15 test session management and Flyway 8.5.13 test
# schema creation from the Java source.
# ============================================================================


@pytest.fixture(scope="session")
def init_db(app):
    """Initialise the test database schema.

    Creates all SQLAlchemy model tables in the in-memory SQLite database
    using ``db.create_all()``.  The schema persists for the entire test
    session (``scope='session'``) and is torn down via ``db.drop_all()``
    after all tests have completed.

    This fixture must be requested by any fixture or test that needs
    database access to ensure the schema exists.

    Args:
        app: The session-scoped Flask application fixture.

    Yields:
        flask_sqlalchemy.SQLAlchemy: The initialised ``db`` extension
        instance, ready for use in downstream fixtures.
    """
    # Create tables in a short-lived context so the session-scope
    # ``init_db`` fixture does NOT keep an app context alive for the
    # entire run.  A persistent session-scope context collides with
    # the function-scope ``client`` fixture's own app context, producing
    # "Popped wrong app context" errors during teardown.
    with app.app_context():
        # Create all 14 entity tables (Repository, Component, Asset,
        # User, Role, RoleAssignment, Privilege, ContentSelector,
        # TaskDefinition, TaskExecution, AuditEvent, BlobStoreConfig,
        # CleanupPolicy, SystemConfig) plus base mixin columns
        # (timestamps, soft-delete, JSON attributes).
        db.create_all()

    yield db

    # Teardown — drop tables in their own context
    with app.app_context():
        db.drop_all()


@pytest.fixture(scope="function")
def db_session(app, init_db):
    """Provide a clean, transaction-isolated database session for each test.

    Each test function receives a SQLAlchemy session that is **bound to a
    dedicated connection** with an open transaction.  After the test
    completes (whether passing or failing), the transaction is rolled back
    — ensuring complete isolation between tests without the cost of
    dropping/recreating the schema.

    **Isolation Mechanism (Flask-SQLAlchemy 3.x pattern):**

    1. A raw connection is opened from the engine pool.
    2. A top-level transaction is started on that connection.
    3. The scoped session factory is *reconfigured* to bind to this
       connection via ``db.session.configure(bind=connection)``.  This
       ensures all ORM operations (queries, inserts, commits) executed
       through ``db.session`` flow through the **same** underlying
       connection and transaction — rather than obtaining a separate
       pooled connection.
    4. On teardown, the session is rolled back, removed from the scoped
       registry, the connection transaction is rolled back, and the
       connection is closed.  The session factory is then restored to
       bind to the default engine.

    This replaces the MyBatis 3.5.15 test session management from the
    Java source system.

    Args:
        app: The session-scoped Flask application fixture.
        init_db: The session-scoped database initialiser (ensures schema
            exists).

    Yields:
        sqlalchemy.orm.scoping.scoped_session: A scoped SQLAlchemy session
        bound to a test-specific connection that will be rolled back after
        the test completes.
    """
    with app.app_context():
        connection = db.engine.connect()
        transaction = connection.begin()

        # Remove any existing session from the scoped registry, then
        # reconfigure the session factory to bind to the test connection.
        # This is the Flask-SQLAlchemy 3.x testing pattern: the scoped
        # session now routes all ORM operations through ``connection``,
        # which has an uncommitted ``transaction``.  Any ``db.session.commit()``
        # calls inside the test flush to this connection but do NOT reach
        # the database — the outer ``transaction.rollback()`` in teardown
        # discards everything.
        db.session.remove()
        db.session.configure(bind=connection)

        yield db.session

        # Teardown — roll back everything and restore default binding.
        db.session.rollback()
        db.session.remove()
        transaction.rollback()
        connection.close()

        # Restore the session factory to use the engine (default pool)
        # so that subsequent fixtures and tests get a clean session
        # bound to the engine rather than the now-closed connection.
        db.session.configure(bind=db.engine)


# ============================================================================
# 3. Automatic Database Cleanup — Autouse
# ============================================================================
# Ensures every test starts with a clean database state by truncating all
# tables after each test completes.  The autouse=True parameter makes this
# fixture run automatically for every test without explicit request.
# ============================================================================


@pytest.fixture(autouse=True)
def clean_db(request):
    """Automatically clean database state between tests.

    Runs after every test function (``autouse=True``) to guarantee test
    isolation.  The cleanup strategy is:

    1. Roll back any uncommitted transaction (prevents cascading failures
       from partial commits in earlier tests).
    2. Truncate all tables in reverse dependency order (respecting foreign
       key constraints) while preserving the schema.
    3. Commit the deletes so the clean state is visible to the next test.

    This is more reliable than relying solely on ``db_session`` rollback
    because some tests may use ``commit()`` explicitly.

    The fixture dynamically resolves the ``app`` fixture at any scope
    (session, module, or function) to avoid ScopeMismatch errors when
    test modules define their own ``app`` fixture at a narrower scope.

    Args:
        request: The pytest request object (used to access the ``app``
            fixture dynamically).
    """
    yield

    # Dynamically get the app fixture — this avoids hard scope
    # dependencies that cause ScopeMismatch when test modules
    # define their own app fixture at module or function scope.
    try:
        app = request.getfixturevalue("app")
    except Exception:
        # If no app fixture is available, skip cleanup silently
        return

    # Only push a *new* app context if one is not already active.
    # When the ``client`` fixture is in use its ``with app.app_context()``
    # block is still alive during teardown.  Pushing another context
    # causes "Popped wrong app context" errors on exit.
    #
    # Uses Flask's public ``has_app_context()`` API instead of the private
    # ``flask.globals._cv_app`` ContextVar, which is an undocumented
    # internal that may change across Flask versions.
    from flask import current_app, has_app_context

    needs_context = True
    if has_app_context():
        try:
            needs_context = current_app._get_current_object() is not app
        except RuntimeError:
            pass  # No app context after all — needs_context stays True
    if needs_context:
        ctx = app.app_context()
        ctx.push()

    try:
        # Roll back any pending transaction
        db.session.rollback()

        # Truncate all tables in reverse dependency order
        for table in reversed(db.metadata.sorted_tables):
            db.session.execute(table.delete())

        db.session.commit()
    except Exception:
        # If cleanup itself fails, ensure we at least rollback
        try:
            db.session.rollback()
        except Exception:
            pass
    finally:
        if needs_context:
            ctx.pop()


# ============================================================================
# 4. HTTP Client Fixtures
# ============================================================================
# Flask's built-in test client replaces an HTTP client for making API
# requests without a running server.
# ============================================================================


@pytest.fixture(scope="function")
def client(app, init_db):
    """Create a Flask test client for HTTP request testing.

    The test client allows making GET, POST, PUT, DELETE, and PATCH
    requests to Flask route handlers without starting a real HTTP server.
    It operates within the application context so that database access
    and other Flask extensions work correctly.

    Used by API integration tests (``tests/integration/test_api.py``)
    and any unit tests that need to exercise HTTP endpoints.

    Args:
        app: The session-scoped Flask application fixture.
        init_db: The session-scoped database initialiser.

    Yields:
        flask.testing.FlaskClient: A Flask test client instance.
    """
    # Push a *single* app context for the test and yield the client.
    # Using ``app.test_client()`` as a context manager together with a
    # nested ``with app.app_context():`` caused "Popped wrong app
    # context" errors because Flask's test client can push/pop request
    # contexts that interfere with a manually-pushed outer app context.
    ctx = app.app_context()
    ctx.push()
    testing_client = app.test_client()
    yield testing_client
    ctx.pop()


@pytest.fixture(scope="function")
def runner(app):
    """Create a Flask CLI test runner.

    Returns a ``FlaskCliRunner`` that can invoke Flask CLI commands
    (registered via ``@app.cli.command()``) in a test-friendly manner.

    Example usage in a test::

        result = runner.invoke(args=['db', 'init'])
        assert result.exit_code == 0

    Args:
        app: The session-scoped Flask application fixture.

    Returns:
        flask.testing.FlaskCliRunner: A CLI test runner instance.
    """
    return app.test_cli_runner()


# ============================================================================
# 5. Authentication Fixtures
# ============================================================================
# Replace Apache Shiro 2.0.0 test security realm setup and Java-JWT 4.4.0
# test token generation from the Java source system.
# ============================================================================


@pytest.fixture
def admin_user(app, db_session):
    """Create an admin user for testing authenticated endpoints.

    Creates a ``User`` instance with:
        - ``user_id='admin'``
        - ``email='admin@test.com'``
        - ``status='active'``
        - Password set to ``'admin123'`` via ``set_password()``

    The user is persisted to the test database and available for
    authentication in downstream fixtures (``auth_headers``,
    ``api_key_headers``).

    Replaces the Apache Shiro 2.0.0 test security realm setup from the
    Java source system.

    Args:
        app: The session-scoped Flask application fixture.
        db_session: The function-scoped database session fixture.

    Returns:
        src.app.models.user.User: The created admin user instance.
    """
    with app.app_context():
        user = User(
            user_id="admin",
            email="admin@test.com",
            status="active",
        )
        # set_password() hashes via bcrypt/scrypt (replacing BouncyCastle 1.78.1)
        # Password must meet strength requirements: min 8 chars, uppercase,
        # lowercase, digit, and special character.
        user.set_password("Admin123!")
        db.session.add(user)
        db.session.commit()
        # Eagerly load all attributes before yielding so that accessing
        # properties outside this context doesn't trigger a lazy load
        # on a detached instance.
        db.session.refresh(user)
        # Expunge from session so tests can read attrs without needing
        # an active session binding.
        db.session.expunge(user)
        return user


@pytest.fixture
def auth_headers(app, admin_user):
    """Provide JWT Bearer token authentication headers.

    Generates a valid JWT token for the ``admin_user`` with:
        - ``user_id`` claim set to the admin user's ID
        - ``exp`` claim set to 1 hour from now (UTC)
        - Signed with the app's ``JWT_SECRET_KEY`` using HS256

    The returned dictionary contains:
        - ``Authorization: Bearer <token>`` — for route authentication
        - ``Content-Type: application/json`` — for JSON request bodies

    Replaces Java-JWT 4.4.0 test token generation from the Java source.

    Args:
        app: The session-scoped Flask application fixture.
        admin_user: The admin user fixture.

    Returns:
        dict: HTTP headers with JWT Bearer token and JSON content type.
    """
    # Use timezone-aware UTC datetime for token expiry.
    # The JWT realm requires "sub", "iat", and "exp" claims.
    # "sub" is used by jwt_realm.py to identify the user (payload.get("sub")).
    token_payload = {
        "sub": admin_user.user_id,
        "iat": datetime.now(tz=timezone.utc),
        "exp": datetime.now(tz=timezone.utc) + timedelta(hours=1),
    }

    # Retrieve the JWT secret key from app config (TestingConfig sets a
    # fixed deterministic value for reproducible test tokens).
    secret_key = app.config.get(
        "JWT_SECRET_KEY",
        app.config.get("SECRET_KEY", "test-secret-key-not-for-production"),
    )

    token = jwt.encode(
        token_payload,
        secret_key,
        algorithm="HS256",
    )

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def api_key_headers(app, admin_user):
    """Provide API key authentication headers with a matching DB record.

    Returns headers suitable for testing the NX-API-Key authentication
    scheme (Feature F-304).  Unlike a static-only fixture, this also
    persists the SHA-256 hash of the test API key into the ``admin_user``'s
    ``api_key`` column so that the :class:`BearerTokenRealm` can validate
    the key against the database backend during integration tests.

    **How it works:**

    1. The plaintext test key ``'test-api-key-12345'`` is hashed with
       SHA-256 (matching :meth:`BearerTokenRealm._hash_api_key`).
    2. The resulting hex digest is stored in ``admin_user.api_key``.
    3. The header dict returned contains the **plaintext** key for the
       test client to send.

    This ensures that tests exercising the full authentication chain —
    from HTTP header extraction through :class:`BearerTokenRealm`
    database lookup — work end-to-end without mocking auth.

    Args:
        app: The session-scoped Flask application fixture.
        admin_user: The admin user fixture (ensures user exists).

    Returns:
        dict: HTTP headers with NX-API-Key and JSON content type.
    """
    import hashlib

    test_api_key = "test-api-key-12345"

    # Persist the SHA-256 hash of the test API key in the admin_user's
    # api_key column.  This mirrors how BearerTokenRealm._find_user_by_api_key
    # performs Phase 1 lookup: it hashes the incoming token and queries
    # User.api_key for an exact match.
    key_hash = hashlib.sha256(test_api_key.encode("utf-8")).hexdigest()

    with app.app_context():
        user = db.session.get(User, admin_user.user_id)
        if user is not None:
            user.api_key = key_hash
            db.session.commit()

    return {
        "NX-API-Key": test_api_key,
        "Content-Type": "application/json",
    }


# ============================================================================
# 6. Sample Data Fixtures
# ============================================================================
# Pre-populated domain objects for repository management, component, and
# asset test scenarios.  These fixtures create realistic test data that
# mirrors common usage patterns in the Nexus Repository system.
# ============================================================================


@pytest.fixture
def sample_repository(app, db_session):
    """Create a sample Maven hosted repository for testing.

    Creates a ``Repository`` instance with:
        - ``name='test-maven-hosted'``
        - ``format='maven2'``
        - ``type='hosted'``
        - ``blob_store_name='default'``
        - ``online=True``

    Used by repository-related API endpoint tests and service layer tests.

    Args:
        app: The session-scoped Flask application fixture.
        db_session: The function-scoped database session fixture.

    Returns:
        src.app.models.repository.Repository: The created repository.
    """
    with app.app_context():
        repo = Repository(
            name="test-maven-hosted",
            format="maven2",
            type="hosted",
            blob_store_name="default",
            online=True,
            attributes={},
        )
        db.session.add(repo)
        db.session.commit()
        db.session.refresh(repo)
        db.session.expunge(repo)
        return repo


@pytest.fixture
def sample_component(app, db_session, sample_repository):
    """Create a sample Maven component for testing.

    Creates a ``Component`` instance linked to ``sample_repository`` with:
        - ``repository_name=sample_repository.name``
        - ``namespace='org.example'``
        - ``name='test-artifact'``
        - ``version='1.0.0'``

    The component represents a typical Maven artifact coordinate
    (groupId:artifactId:version = org.example:test-artifact:1.0.0).

    Args:
        app: The session-scoped Flask application fixture.
        db_session: The function-scoped database session fixture.
        sample_repository: The parent repository fixture.

    Returns:
        src.app.models.component.Component: The created component with
        a valid ``id`` after database commit.
    """
    with app.app_context():
        comp = Component(
            repository_name=sample_repository.name,
            namespace="org.example",
            name="test-artifact",
            version="1.0.0",
            attributes={},
        )
        db.session.add(comp)
        db.session.commit()
        db.session.refresh(comp)
        db.session.expunge(comp)
        return comp


@pytest.fixture
def sample_asset(app, db_session, sample_component, sample_repository):
    """Create a sample JAR asset for testing.

    Creates an ``Asset`` instance linked to ``sample_component`` with:
        - ``component_id=sample_component.id``
        - ``repository_name=sample_repository.name``
        - ``path='/org/example/test-artifact/1.0.0/test-artifact-1.0.0.jar'``
        - ``content_type='application/java-archive'``
        - ``checksum_sha1='da39a3ee5e6b4b0d3255bfef95601890afd80709'``
        - ``size=1024``

    The asset represents a typical Maven JAR file with a valid SHA-1
    checksum (empty file hash used as placeholder for test purposes).

    Args:
        app: The session-scoped Flask application fixture.
        db_session: The function-scoped database session fixture.
        sample_component: The parent component fixture.
        sample_repository: The parent repository fixture (provides
            ``repository_name`` required by the NOT NULL FK constraint).

    Returns:
        src.app.models.asset.Asset: The created asset with a valid ``id``
        after database commit.
    """
    with app.app_context():
        asset = Asset(
            component_id=sample_component.id,
            repository_name=sample_repository.name,
            path="/org/example/test-artifact/1.0.0/test-artifact-1.0.0.jar",
            content_type="application/java-archive",
            checksum_sha1="da39a3ee5e6b4b0d3255bfef95601890afd80709",
            size=1024,
            attributes={},
        )
        db.session.add(asset)
        db.session.commit()
        db.session.refresh(asset)
        db.session.expunge(asset)
        return asset


# ============================================================================
# 7. Mock Service Fixtures
# ============================================================================
# Replace external service dependencies with test doubles.  These fixtures
# enable isolated testing of components that depend on Elasticsearch (F-103),
# AWS S3 BlobStore (F-202), and the local File BlobStore (F-201).
# ============================================================================


@pytest.fixture
def mock_elasticsearch(mocker):
    """Mock the Elasticsearch client for tests that don't need real ES.

    Patches ``src.app.search.elasticsearch_client.Elasticsearch`` with a
    ``MagicMock`` that returns sensible default responses for ``search()``
    and ``index()`` operations.

    The mock is configured to return:
        - ``search()``: Empty hit list ``{'hits': {'total': {'value': 0}, 'hits': []}}``
        - ``index()``: Success response ``{'result': 'created'}``

    Args:
        mocker: The ``pytest-mock`` mocker fixture.

    Returns:
        unittest.mock.MagicMock: The patched Elasticsearch class mock.
    """
    mock_es = mocker.patch("src.app.search.elasticsearch_client.Elasticsearch")

    # Configure default return values for common ES operations
    mock_es.return_value.search.return_value = {
        "hits": {
            "total": {"value": 0},
            "hits": [],
        }
    }
    mock_es.return_value.index.return_value = {"result": "created"}

    return mock_es


@pytest.fixture
def mock_s3():
    """Mock AWS S3 for BlobStore testing using moto.

    Uses ``moto.mock_aws()`` (v5.0.27) to intercept all boto3 S3 API
    calls and provide an in-memory S3 simulation.  Creates a test bucket
    named ``'test-bucket'`` for BlobStore integration tests.

    The mock supports all standard S3 operations:
        - ``put_object()`` — Upload objects
        - ``get_object()`` — Download objects
        - ``delete_object()`` — Remove objects
        - ``list_objects_v2()`` — List bucket contents

    Replaces the need for a real AWS S3 bucket during testing (Feature
    F-202 — S3 BlobStore).

    Yields:
        botocore.client.S3: A moto-backed S3 client with 'test-bucket'
        pre-created.
    """
    from moto import mock_aws

    with mock_aws():
        import boto3

        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket="test-bucket")
        yield s3_client


@pytest.fixture
def mock_blobstore(tmp_path):
    """Create a temporary file BlobStore directory for testing.

    Provides a clean temporary directory suitable for use as a local
    File BlobStore (Feature F-201).  The directory is automatically
    cleaned up by pytest's ``tmp_path`` fixture after the test session.

    Uses ``tempfile.mkdtemp()`` within the pytest-managed ``tmp_path``
    to create an isolated blob storage location.

    Args:
        tmp_path: pytest's built-in temporary directory fixture.

    Returns:
        str: Absolute path to the temporary BlobStore directory.
    """
    blobstore_path = tmp_path / "blobs"
    blobstore_path.mkdir(parents=True, exist_ok=True)

    # Also create via tempfile.mkdtemp for compatibility verification
    tempfile.mkdtemp(prefix="nexus_test_blobs_", dir=str(tmp_path))

    return str(blobstore_path)


# ============================================================================
# 8. Utility Helper Functions
# ============================================================================
# Convenience functions for common HTTP test patterns.  These are plain
# functions (not fixtures) so they can be imported and called directly in
# test modules.
# ============================================================================


def get_json_response(client, url, headers=None):
    """Make a GET request and return the status code with parsed JSON body.

    A convenience wrapper around ``client.get()`` that automatically
    parses the JSON response body.  Handles cases where the response
    body is not valid JSON by returning ``None`` as the parsed data.

    Args:
        client: A Flask test client instance.
        url: The URL path to request (e.g., ``'/api/v1/repositories'``).
        headers: Optional dict of HTTP headers to include in the request.

    Returns:
        tuple[int, dict | None]: A ``(status_code, json_body)`` tuple.
        ``json_body`` is ``None`` if the response is not valid JSON.

    Example::

        status, data = get_json_response(client, '/api/v1/health')
        assert status == 200
        assert data['status'] == 'healthy'
    """
    response = client.get(url, headers=headers)
    data = response.get_json(silent=True)
    return response.status_code, data


def post_json(client, url, data, headers=None):
    """Make a POST request with JSON payload and return status + parsed body.

    A convenience wrapper around ``client.post()`` that:
    1. Serialises the ``data`` dict as a JSON request body.
    2. Parses the JSON response body.
    3. Returns both the HTTP status code and parsed response.

    The ``json=data`` parameter automatically sets ``Content-Type:
    application/json`` on the request.

    Args:
        client: A Flask test client instance.
        url: The URL path to request (e.g., ``'/api/v1/repositories'``).
        data: A dict to serialise as the JSON request body.
        headers: Optional dict of HTTP headers to include in the request.

    Returns:
        tuple[int, dict | None]: A ``(status_code, json_body)`` tuple.
        ``json_body`` is ``None`` if the response is not valid JSON.

    Example::

        status, result = post_json(client, '/api/v1/repositories', {
            'name': 'my-repo',
            'format': 'maven2',
            'type': 'hosted',
        }, headers=auth_headers)
        assert status == 201
        assert result['name'] == 'my-repo'
    """
    # Use json.dumps and json.loads for explicit serialization verification
    # when needed, but Flask's client.post(json=...) handles this natively
    json_payload = json.dumps(data) if isinstance(data, str) else None

    if json_payload is not None:
        # data was already a string; send as raw data with content-type
        merged_headers = {"Content-Type": "application/json"}
        if headers:
            merged_headers.update(headers)
        response = client.post(url, data=json_payload, headers=merged_headers)
    else:
        # data is a dict; use Flask's native json parameter
        response = client.post(url, json=data, headers=headers)

    response_data = response.get_json(silent=True)
    return response.status_code, response_data
