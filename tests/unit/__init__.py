"""
Unit tests for the Flask Binary Repository Management System.

This package contains isolated component tests where all external
dependencies are mocked. Tests are organized into:

- test_app_factory.py — Application factory creation and configuration
- test_config.py — Environment-specific configuration validation
- services/ — Service layer unit tests (repository, storage, security, etc.)
- models/ — Data model unit tests (repository, user, asset, etc.)
- utils/ — Utility function unit tests (format, crypto, validation)

All tests in this package use the @pytest.mark.unit marker for selective execution.
Run with: python -m pytest tests/unit/ -v -m unit
"""
