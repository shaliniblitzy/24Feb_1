"""
Integration tests for the Sonatype Nexus Repository Python/Flask backend.

These tests exercise multiple components working together through the Flask
test client, verifying end-to-end behavior including:
- REST API endpoints (all 16 blueprints)
- Repository lifecycle state machine (hosted, proxy, group)
- Proxy remote artifact fetching and caching
- Elasticsearch search integration with SQL fallback
- File and S3 BlobStore storage operations

Tests use shared fixtures from tests/conftest.py including:
- Flask test client with TestingConfig (SQLite in-memory)
- Per-test database transaction rollback for isolation
- JWT authentication headers
- moto-mocked S3 and patched Elasticsearch
"""
