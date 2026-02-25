"""
Unit test package for the Nexus Repository Manager Python/Flask rewrite.

Contains unit tests for individual services, models, format handlers,
authentication realms, CSEL parser, and utility functions. This is the
foundation of the testing pyramid — the most granular level of testing.

Test modules:
- test_repositories: Repository management services and lifecycle state machine
- test_storage: DataStore and BlobStore operations (file and S3)
- test_security: Authentication chain, RBAC, CSEL, audit, and SSL
- test_admin: Health checks, scheduler, support ZIP, and system config
- test_models: SQLAlchemy model validation and relationships
- test_formats: Format handlers (Maven, npm, Docker, NuGet, PyPI, APT, Raw)

Testing conventions:
- All tests are isolated — no shared mutable state between test functions
- External dependencies (database, S3, LDAP) are mocked using pytest-mock
- Shared fixtures are defined in tests/conftest.py
- Minimum coverage targets: 80% line, 70% branch (AAP Section 0.7.6)
"""
