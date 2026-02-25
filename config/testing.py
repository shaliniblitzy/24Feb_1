"""
config/testing.py - Test Environment Configuration

Test-specific configuration class that inherits from DefaultConfig.
Configures the Flask application for automated test execution with an
in-memory SQLite database, deterministic security keys, disabled external
services, and reduced bcrypt rounds for fast test performance.

Used by ``tests/conftest.py`` shared fixtures to create the test application
via the Flask application factory: ``create_app('testing')``.

Characteristics:
    - TESTING = True   — enables Flask test mode (disables error catching)
    - In-memory SQLite — fast, isolated; no cleanup between test runs
    - Fixed secret keys — deterministic token generation
    - External services disabled (Elasticsearch, LDAP, SSO, webhooks)
    - Reduced bcrypt rounds (4) for fast password hashing in tests
    - Scheduler disabled — tests trigger tasks manually

Architecture mapping:
    Java H2 in-memory test DB    →  SQLite :memory: via SQLAlchemy
    JUnit/Spock test harness     →  pytest + pytest-flask
    Mockito service mocking      →  pytest-mock
    AWS LocalStack               →  moto (S3 BlobStore mocking)
"""

import os
import tempfile

from config.default import DefaultConfig

# Base directory — absolute path of the directory containing this file.
_basedir: str = os.path.abspath(os.path.dirname(__file__))


class TestingConfig(DefaultConfig):
    """Testing configuration with in-memory SQLite and mock-friendly defaults.

    All external service integrations (Elasticsearch, LDAP, SSO, webhooks,
    scheduler) are disabled by default so that unit and integration tests
    can run in complete isolation.  Security keys are fixed strings for
    deterministic token generation and verification.  The bcrypt cost
    factor is reduced to the minimum safe value (4) to keep password
    hashing fast during test execution.
    """

    # ==================================================================
    # 1. Flask Core — Test Mode
    # ==================================================================

    # Enable Flask testing mode — disables error catching during request
    # handling so that test assertions receive actual exceptions rather
    # than generic 500 responses.
    TESTING: bool = True

    # Enable debug for detailed error information in test failures.
    DEBUG: bool = True

    # Logical environment name.
    ENV: str = "testing"

    # ==================================================================
    # 2. Database — In-Memory SQLite
    # ==================================================================

    # In-memory SQLite provides an isolated, ephemeral database that is
    # created fresh for each test session and automatically discarded.
    # No cleanup scripts or teardown queries are needed between runs.
    SQLALCHEMY_DATABASE_URI: str = "sqlite:///:memory:"

    # Suppress SQL echo during tests to reduce console noise.
    SQLALCHEMY_ECHO: bool = False

    # Disable modification tracking for performance.
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False

    # Override engine options for in-memory SQLite compatibility.
    # StaticPool ensures the same in-memory database is shared across
    # all connections within a single test session.
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "pool_pre_ping": False,
    }

    # ==================================================================
    # 3. Security — Deterministic Keys for Reproducible Tests
    # ==================================================================

    # Fixed secret key so that session cookies and signed tokens are
    # reproducible across test runs and CI environments.
    SECRET_KEY: str = "test-secret-key-not-for-production"

    # Fixed JWT signing key for deterministic token generation.
    JWT_SECRET_KEY: str = "test-jwt-secret-key-not-for-production"

    # 1-hour access tokens — long enough for any test scenario.
    JWT_ACCESS_TOKEN_EXPIRES: int = 3600

    # Minimum safe bcrypt cost factor for fast test execution.
    # Production uses 12+ rounds; 4 is sufficient for correctness tests.
    PASSWORD_HASH_ROUNDS: int = 4

    # Disable CSRF protection for simpler test request construction.
    WTF_CSRF_ENABLED: bool = False

    # Keep authentication active so that auth tests work correctly.
    LOGIN_DISABLED: bool = False

    # ==================================================================
    # 4. External Services — All Disabled / Mocked
    # ==================================================================

    # Disable Elasticsearch — search tests should use a mocked client.
    ELASTICSEARCH_URL: str = ""

    # File-based BlobStore in a temporary directory that is cleaned up
    # by the OS.  S3 tests should use the moto library for mocking.
    BLOBSTORE_TYPE: str = "file"
    BLOBSTORE_PATH: str = os.path.join(
        tempfile.gettempdir(), "nexus_test_blobs"
    )

    # Disable S3 storage backend — use moto for S3 BlobStore tests.
    S3_BUCKET: str = ""

    # Disable LDAP directory authentication.
    LDAP_ENABLED: bool = False

    # Disable SSO federation.
    SSO_ENABLED: bool = False

    # Disable Prometheus metrics collection.
    METRICS_ENABLED: bool = False

    # Disable outbound webhook dispatch.
    WEBHOOK_ENABLED: bool = False

    # ==================================================================
    # 5. Scheduler — Disabled (Tests Trigger Tasks Manually)
    # ==================================================================

    # Disable APScheduler — scheduled task tests should invoke task
    # functions directly rather than relying on timed execution.
    SCHEDULER_ENABLED: bool = False

    # ==================================================================
    # 6. Logging — Minimal Noise
    # ==================================================================

    # Only log warnings and above during tests to reduce output noise.
    LOG_LEVEL: str = "WARNING"

    # Human-readable format for debugging test failures in the console.
    LOG_FORMAT: str = "standard"

    # ==================================================================
    # 7. CORS — Permissive for Test Requests
    # ==================================================================

    CORS_ORIGINS: str = "*"

    # ==================================================================
    # 8. Server Identification for URL Generation
    # ==================================================================

    # Fixed server name for deterministic URL generation in tests.
    SERVER_NAME: str = "localhost"

    # HTTP scheme for test URLs (no SSL in tests).
    PREFERRED_URL_SCHEME: str = "http"
