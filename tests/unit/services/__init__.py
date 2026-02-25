"""
Unit tests for service layer components of the Flask Binary Repository Management System.

This package contains isolated service tests where all external
dependencies (database, S3, search engine, HTTP clients) are mocked.
Tests are organized by service module:

- test_repository_service.py — Repository CRUD, multi-format, multi-type tests
- test_storage_service.py — File BlobStore and S3 BlobStore operations
- test_security_service.py — JWT, API key, RBAC, session management
- test_search_service.py — Content indexing, full-text search, filtered queries
- test_scheduler_service.py — Task scheduling, execution, cleanup policies
- test_audit_service.py — Audit log creation, retrieval, filtering, retention
- test_webhook_service.py — Webhook registration, dispatch, retry logic
- test_health_service.py — Health check aggregation, component status

All tests use the @pytest.mark.unit marker for selective execution.
Run with: python -m pytest tests/unit/services/ -v -m unit
"""
