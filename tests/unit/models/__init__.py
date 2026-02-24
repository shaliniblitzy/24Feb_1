"""
Unit tests for data models of the Flask Binary Repository Management System.

This package contains isolated model tests using an in-memory SQLite database
for real SQL execution without a persistent database. Tests are organized by
model domain:

- test_repository_model.py — Repository model (3 types × 7 formats)
- test_user_model.py — User model (password hashing, roles, API keys)
- test_asset_model.py — Asset/component model (hashes, BlobStore refs)
- test_blobstore_model.py — BlobStore config model (file vs S3 types)
- test_task_model.py — Scheduled task model (cron, state transitions)
- test_security_model.py — Role, Privilege, ContentSelector models (RBAC)

All tests in this package use the @pytest.mark.unit marker.
Model fixtures are provided by conftest.py with per-test transaction rollback.
"""
