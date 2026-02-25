"""
End-to-End (E2E) Test Package for Nexus Repository Manager.

This package contains end-to-end tests that exercise complete workflows
across all application layers — from REST API request through authentication,
authorization, repository management, storage, and back to response.

These are the highest-level tests in the testing pyramid, verifying that
all components work together correctly for real-world usage scenarios.

Test Modules:
    test_artifact_lifecycle:
        Full artifact lifecycle testing (upload → search → download → cleanup)
        across all 7 supported repository formats:
        - Maven (maven2), npm, Docker, NuGet, PyPI, APT, Raw
        Exercises: F-101 (multi-format), F-102 (repo types), F-103 (search),
                   F-104 (browse), F-201/F-202 (BlobStore), F-204 (cleanup)

    test_user_management:
        Complete user management workflow testing:
        user CRUD → role assignment → permission enforcement → audit verification
        Exercises: F-301 (RBAC + CSEL), F-302 (certificates), F-303 (audit),
                   F-304 (API key auth)

Testing Conventions:
    - Uses Flask test client from shared conftest.py fixtures
    - Pre-creates test repositories, users, and roles for workflow scenarios
    - Verifies cross-module integration (security + repository + storage)
    - Validates audit event generation through blinker signal system
    - Mocks external dependencies (S3 via moto, LDAP, no network access)
    - Tests backward-compatible REST API paths per tech spec
    - Each test is independent with no shared mutable state
    - Test isolation via transactional database rollback
    - Minimum coverage targets: 80% line, 70% branch (AAP Section 0.7.6)
"""
