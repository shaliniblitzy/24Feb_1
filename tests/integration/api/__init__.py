"""
API integration tests for the Flask Binary Repository Management System.

This package contains full HTTP lifecycle integration tests for all REST API
endpoint groups. Tests use Flask's test_client() to issue real HTTP requests
through the complete application stack: route handler → service → model → database.

Test modules:
- test_repository_api: Repository CRUD endpoints (POST/GET/PUT/DELETE)
- test_asset_api: Artifact upload, download, search, and deletion
- test_auth_api: Authentication (login, JWT refresh, API keys, sessions)
- test_user_api: User management (CRUD, role assignment, passwords)
- test_search_api: Search queries (full-text, faceted, keyword)
- test_admin_api: Administration (config, health, support ZIP)
- test_task_api: Scheduled task management
- test_blobstore_api: BlobStore configuration and management

All tests inherit @pytest.mark.integration from the parent conftest.py.
Individual tests must complete in < 2 seconds.
"""
