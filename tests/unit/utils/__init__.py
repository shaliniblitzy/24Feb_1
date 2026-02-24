"""
Unit tests for utility functions in the Flask Binary Repository Management System.

This package contains isolated unit tests for the utility modules:

- test_format_utils.py — Format detection, content type mapping,
  and coordinate parsing for all 7 repository formats
  (Maven, npm, Docker, NuGet, PyPI, APT, Raw)
- test_crypto_utils.py — Hash computation (SHA-1, SHA-256, MD5),
  checksum validation, and encryption utilities
- test_validation_utils.py — Input validation, name sanitization,
  path normalization, and size limit enforcement

All tests in this package use the @pytest.mark.unit marker for selective execution.
Run with: python -m pytest tests/unit/utils/ -v -m unit
"""
