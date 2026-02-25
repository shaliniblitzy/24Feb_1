"""
Service layer integration tests for the Flask Binary Repository Management System.

This package contains integration tests that validate service layer components
interact correctly with real database sessions, filesystem operations, and
mocked external services. Unlike unit tests which fully mock dependencies,
these tests exercise actual component interactions while still isolating
external systems (S3, search engine, upstream registries).

Test modules:
- test_repository_integration: Repository CRUD with real database across all types and formats
- test_proxy_integration: Proxy fetch, cache, and upstream registry interaction
- test_storage_integration: File BlobStore and S3 BlobStore operations with temp filesystem
- test_auth_integration: Full authentication chain (login → token → access → refresh → logout)
- test_search_integration: Search indexing pipeline (upload → index → search → delete)

All tests in this package:
- Use @pytest.mark.integration marker (inherited from parent conftest.py)
- Use real SQLAlchemy sessions with in-memory SQLite (per-test transaction rollback)
- Must complete in < 2 seconds individually
- Follow Arrange-Act-Assert (AAA) pattern
- Follow test_{service}_{scenario}_{expected_result} naming convention
"""
