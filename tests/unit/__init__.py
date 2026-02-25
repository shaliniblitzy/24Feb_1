"""
Unit Test Package for Sonatype Nexus Repository (Python/Flask).

This package contains unit tests for all core backend components:
- test_models: SQLAlchemy model tests for all 14 data entities
- test_services: Business logic service layer tests (10 services)
- test_auth: Authentication chain and authorization tests (multi-realm, RBAC, CSEL)
- test_formats: Format-specific protocol handler tests (7 formats)
- test_storage: BlobStore backend tests (File and S3)
- test_scheduler: APScheduler task scheduling tests

Testing Stack:
- pytest 8.3.4 (test framework)
- pytest-mock 3.14.0 (mocking)
- factory-boy 3.3.1 (test data factories)
- Faker 33.1.0 (fake data generation)
- moto 5.0.27 (AWS S3 mocking)

All tests use in-memory SQLite via TestingConfig and shared
fixtures from tests/conftest.py.
"""
