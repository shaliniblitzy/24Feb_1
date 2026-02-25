"""
Integration test package for the Nexus Repository Manager Flask application.

Contains integration tests that verify cross-module interactions, REST API
endpoint behavior, and service-to-database flows. These tests exercise multiple
layers working together while mocking external dependencies.

Test modules:
- test_api_repositories: Repository management REST API endpoints
- test_api_security: Security management REST API endpoints
- test_api_admin: Administration REST API endpoints
- test_proxy_fetch: Proxy repository upstream fetching
- test_blobstore_s3: S3 BlobStore operations (moto mocking)
- test_auth_chain: Multi-realm authentication flow
- test_scheduler: APScheduler task execution
- test_webhooks: Webhook event dispatch and HMAC verification

All tests use:
- Flask test client from conftest.py fixtures
- In-memory SQLite database with transactional rollback
- Mocked external dependencies (S3 via moto, HTTP via unittest.mock)
"""
