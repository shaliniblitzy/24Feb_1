"""
Shared utilities and helper functions for the Nexus Repository Flask application.

This package replaces Apache Commons and Google Guava utility classes
from the original Java source system. It provides:
- helpers: General utility functions (string, date, JSON, checksum, etc.)
- validators: Input validation utilities (name, version, path, email, URL, cron)
- http_client: HTTP client wrapper with retry logic and connection pooling
- file_utils: File system utilities with atomic operations and path sanitization

Convenience re-exports are provided so that the most commonly used symbols
can be imported directly from ``src.app.utils`` rather than from their
individual submodules::

    from src.app.utils import generate_uuid, HttpClient, validate_path

For less commonly used symbols, import from the specific submodule::

    from src.app.utils.helpers import camel_to_snake
    from src.app.utils.file_utils import list_files
"""

# ---------------------------------------------------------------------------
# Re-exports from helpers submodule
# ---------------------------------------------------------------------------
# General utility functions used extensively across the entire application.
# Replaces java.util.UUID, com.google.common.hash.Hashing, Apache Commons IO,
# javax.activation, java.time.Instant, and SQLAlchemy query pagination helpers.
from src.app.utils.helpers import (
    generate_uuid,
    compute_checksum,
    format_file_size,
    detect_mime_type,
    iso_now,
    paginate_query,
    slugify,
)

# ---------------------------------------------------------------------------
# Re-exports from validators submodule
# ---------------------------------------------------------------------------
# Input validation functions replacing Java Bean Validation (JSR 380)
# constraints from the source system.  Used across API routes, services,
# and model pre-save hooks.
from src.app.utils.validators import (
    validate_repository_name,
    validate_version_string,
    validate_path,
    validate_email,
    validate_url,
    validate_cron_expression,
)

# ---------------------------------------------------------------------------
# Re-export from http_client submodule
# ---------------------------------------------------------------------------
# Production-grade HTTP client wrapping requests.Session.  Replaces
# Apache HttpClient 4.5.14 and HttpClientManagerImpl.java.
from src.app.utils.http_client import HttpClient

# ---------------------------------------------------------------------------
# Re-exports from file_utils submodule
# ---------------------------------------------------------------------------
# File system utilities critical for BlobStore data integrity (F-201, F-202).
# atomic_write replaces Java Files.move(ATOMIC_MOVE), ensure_directory creates
# directory hierarchies, and sanitize_path prevents traversal attacks.
from src.app.utils.file_utils import (
    atomic_write,
    ensure_directory,
    sanitize_path,
)

# ---------------------------------------------------------------------------
# Public API — explicit __all__ to control ``from src.app.utils import *``
# ---------------------------------------------------------------------------
__all__: list[str] = [
    # helpers
    "generate_uuid",
    "compute_checksum",
    "format_file_size",
    "detect_mime_type",
    "iso_now",
    "paginate_query",
    "slugify",
    # validators
    "validate_repository_name",
    "validate_version_string",
    "validate_path",
    "validate_email",
    "validate_url",
    "validate_cron_expression",
    # http_client
    "HttpClient",
    # file_utils
    "atomic_write",
    "ensure_directory",
    "sanitize_path",
]
