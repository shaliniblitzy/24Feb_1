"""
Nexus Repository Manager — Test Suite

Comprehensive test suite for the Sonatype Nexus Repository Manager
Python/Flask implementation. Organized in a multi-tier testing pyramid:

- tests/unit/        — Unit tests for individual services, models, and utilities
- tests/integration/ — Integration tests for REST API endpoints and cross-module flows
- tests/e2e/         — End-to-end tests for complete workflows across all layers

Testing Rules (AAP Section 0.7.6):
- Minimum 80% line coverage, 70% branch coverage
- Every test must be independent — no shared mutable state
- Mock external dependencies (S3 via moto, LDAP, upstream registries)
- Each format handler has dedicated unit tests

Fixtures are defined in tests/conftest.py and include:
- app: Flask test application with TestingConfig
- client: Flask test HTTP client
- db_session: Transactional database session with rollback
- auth_headers: Pre-authenticated admin request headers
"""
