"""
Sonatype Nexus Repository - Test Suite.

This package contains the complete test suite for the Python/Flask
reimplementation of the Sonatype Nexus Repository Manager backend.

Test categories:
- unit/     — Unit tests for models, services, auth, formats, storage, scheduler
- integration/ — Integration tests for API endpoints, repository lifecycle, proxy, search, blobstore
- fixtures/    — Shared test data, sample artifacts, and test data factories

Testing stack:
- pytest 8.3.4 (test framework)
- pytest-cov 6.0.0 (coverage reporting)
- pytest-flask 1.3.0 (Flask testing utilities)
- pytest-mock 3.14.0 (mocking utilities)
- factory-boy 3.3.1 (test data factories)
- Faker 33.1.0 (fake data generation)
- moto 5.0.27 (AWS S3 mocking)
"""
