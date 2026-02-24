"""
Integration tests for the Flask Binary Repository Management System.

This package contains component interaction tests that verify multiple
application layers work together correctly. Tests use Flask's test_client()
to issue real HTTP requests through the application stack with an in-memory
SQLite test database.

Subpackages:
- api/ — Full HTTP lifecycle tests for all REST API endpoint groups
- services/ — Service layer integration tests with real database interactions

All tests in this package are marked with @pytest.mark.integration for
selective execution. Individual integration tests must complete in < 2 seconds.
Full integration suite must complete in < 10 minutes.
"""
