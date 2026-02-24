"""
Reusable mock objects for the Flask Binary Repository Management System test suite.

This package provides mock implementations of external dependencies to enable
testing without real service connections:

- mock_s3_client: Mock boto3 S3 client for BlobStore operations
- mock_search_engine: Mock search backend for indexing and query operations
- mock_proxy_client: Mock HTTP client for upstream proxy registry responses
- mock_email_service: Mock notification service for webhook and alert testing

All mocks are stateful where appropriate and support error injection for
testing failure scenarios. Mock interfaces match the real service interfaces
they replace to ensure test fidelity.
"""
