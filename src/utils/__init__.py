"""
Nexus Repository Manager — Shared Utilities Package

Provides cross-cutting helper functions used across all feature modules:
- hashing: Content-addressable hash computation (SHA-1, SHA-256, MD5) for blob deduplication
- pagination: Offset-based and cursor-based pagination helpers for REST API list endpoints
- validation: Request validation decorators and marshmallow schema integration
- http_client: Outbound HTTP client for proxy repository upstream fetches

Architecture:
- Foundation layer — consumed by all other src packages
- Thread-safe implementations for multi-worker Gunicorn deployment
- No dependencies on other src packages except src/exceptions.py and src/security/ssl_manager.py

Submodule Usage (import directly from submodules to avoid circular imports):
    from src.utils.hashing import compute_sha256, compute_sha1, compute_md5
    from src.utils.pagination import paginate, PageResponse
    from src.utils.validation import validate_request, validate_schema
    from src.utils.http_client import HttpClient
"""

# Expose submodule names for discoverability.
# Consumers should import directly from submodules rather than from this
# package to avoid circular import issues during application startup.
# Example:
#     from src.utils.hashing import compute_sha256
#     from src.utils.pagination import paginate
#     from src.utils.validation import validate_request
#     from src.utils.http_client import HttpClient
__all__ = [
    'hashing',
    'pagination',
    'validation',
    'http_client',
]
