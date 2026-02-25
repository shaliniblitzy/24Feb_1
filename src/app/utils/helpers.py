"""
General utility functions for the Nexus Repository Flask application.

This module replaces Apache Commons and Google Guava utility classes from the
Java source system. It provides commonly used helper functions for:

- UUID generation (replaces java.util.UUID.randomUUID())
- ISO 8601 date/time formatting (replaces java.time.Instant)
- Cryptographic checksum computation (replaces com.google.common.hash.Hashing)
- Human-readable file size formatting (replaces Apache Commons IO FileUtils)
- MIME type detection (replaces javax.activation.MimetypesFileTypeMap)
- String manipulation (replaces Apache Commons Lang StringUtils)
- JSON serialization helpers (replaces Jackson ObjectMapper utilities)
- Pagination utilities for SQLAlchemy queries
- Deep dictionary merging, chunked iteration, and retry-with-backoff

This is the MOST FOUNDATIONAL utility module — used extensively across the
entire application by models, services, API routes, storage backends, and
format handlers.

All functions are designed to be stateless and side-effect-free where possible.
"""

import base64
import hashlib
import hmac
import json
import logging
import math
import mimetypes
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Generator, Sequence

# ---------------------------------------------------------------------------
# Module-level logger (replaces SLF4J 1.7.36)
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Size formatting constants (used by format_file_size / parse_file_size)
SIZE_UNITS: list[str] = ["B", "KB", "MB", "GB", "TB", "PB"]
SIZE_FACTOR: int = 1024

# Checksum algorithm identifiers (used by compute_checksum / verify_checksum)
CHECKSUM_SHA1: str = "sha1"
CHECKSUM_SHA256: str = "sha256"
CHECKSUM_MD5: str = "md5"

# Slug generation constants
SLUG_SEPARATOR: str = "-"
SLUG_MAX_LENGTH: int = 200

# Pagination defaults (enforced to prevent resource exhaustion)
DEFAULT_PAGE_SIZE: int = 50
MAX_PAGE_SIZE: int = 200

# Internal constants
_CHECKSUM_CHUNK_SIZE: int = 8192  # 8 KB read chunks for streaming hash

# Pre-compiled regex patterns (compiled once at module level for performance)
_CAMEL_TO_SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")
_NON_ALPHANUM_RE = re.compile(r"[^a-z0-9]+")
_SIZE_PARSE_RE = re.compile(
    r"^\s*([\d]+(?:\.[\d]+)?)\s*(B|KB|MB|GB|TB|PB)\s*$", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# MIME type initialisation — register Nexus-specific extensions
# ---------------------------------------------------------------------------
_MIME_TYPES_INITIALISED: bool = False


def _init_mime_types() -> None:
    """Register Nexus-specific MIME types (called once on first use)."""
    global _MIME_TYPES_INITIALISED
    if _MIME_TYPES_INITIALISED:
        return
    mimetypes.init()
    mimetypes.add_type("application/xml", ".pom")
    mimetypes.add_type("application/java-archive", ".jar")
    mimetypes.add_type("application/java-archive", ".war")
    mimetypes.add_type("application/zip", ".nupkg")
    mimetypes.add_type("application/zip", ".whl")
    mimetypes.add_type("application/vnd.debian.binary-package", ".deb")
    mimetypes.add_type("application/x-rpm", ".rpm")
    _MIME_TYPES_INITIALISED = True


# ===========================================================================
# Phase 2 — UUID Generation
# ===========================================================================


def generate_uuid() -> str:
    """Generate a random UUID v4 string.

    Returns a lowercase hex string with hyphens, e.g.
    ``'550e8400-e29b-41d4-a716-446655440000'``.

    Replaces ``java.util.UUID.randomUUID().toString()``.
    Used everywhere: model IDs, blob IDs, request IDs, task IDs.
    """
    return str(uuid.uuid4())


def is_valid_uuid(value: str) -> bool:
    """Validate whether *value* is a well-formed UUID (versions 1-5).

    Accepts UUIDs with or without hyphens.  Returns ``True`` when
    *value* represents a valid UUID, ``False`` otherwise.
    """
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


# ===========================================================================
# Phase 3 — Date / Time Formatting (ISO 8601)
# ===========================================================================


def iso_now() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Output format example: ``'2024-01-15T10:30:00.000000+00:00'``

    Replaces ``java.time.Instant.now().toString()``.
    """
    return datetime.now(timezone.utc).isoformat()


def iso_format(dt: datetime) -> str:
    """Format a *datetime* object to an ISO 8601 string.

    If *dt* is timezone-naive it is assumed to represent UTC and a
    UTC timezone is attached before formatting.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_iso(date_string: str) -> datetime:
    """Parse an ISO 8601 date string into a timezone-aware *datetime*.

    Handles formats with and without milliseconds, with trailing ``Z``
    or explicit ``+00:00`` offsets.

    Raises:
        ValueError: If *date_string* cannot be parsed.
    """
    if not isinstance(date_string, str) or not date_string.strip():
        raise ValueError(f"Invalid ISO 8601 date string: {date_string!r}")

    cleaned = date_string.strip()
    # Replace trailing 'Z' with '+00:00' for fromisoformat compatibility
    if cleaned.endswith("Z") or cleaned.endswith("z"):
        cleaned = cleaned[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(
            f"Invalid ISO 8601 date string: {date_string!r}"
        ) from exc

    # Ensure timezone-aware (default to UTC for naive results)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def time_ago(dt: datetime) -> str:
    """Convert *dt* to a human-readable "time ago" string.

    Examples: ``'just now'``, ``'5 minutes ago'``, ``'2 hours ago'``,
    ``'3 days ago'``, ``'1 month ago'``, ``'2 years ago'``.
    """
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    delta = now - dt
    seconds = int(delta.total_seconds())

    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 2:
        return "1 minute ago"
    if minutes < 60:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    if hours < 2:
        return "1 hour ago"
    if hours < 24:
        return f"{hours} hours ago"
    days = hours // 24
    if days < 2:
        return "1 day ago"
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    if months < 2:
        return "1 month ago"
    if months < 12:
        return f"{months} months ago"
    years = days // 365
    if years < 2:
        return "1 year ago"
    return f"{years} years ago"


# ===========================================================================
# Phase 4 — Checksum Computation (CRITICAL for BlobStore)
# ===========================================================================


def compute_checksum(
    data: bytes | BinaryIO, algorithm: str = CHECKSUM_SHA1
) -> str:
    """Compute the hex-digest checksum of *data*.

    Parameters:
        data: Raw bytes **or** a readable file-like object.
        algorithm: One of ``'sha1'``, ``'sha256'``, ``'md5'``.

    Returns:
        Lowercase hex-digest string.

    CRITICAL for BlobStore artifact integrity (Features F-201, F-202).
    Replaces ``com.google.common.hash.Hashing``.
    """
    hasher = hashlib.new(algorithm)

    if isinstance(data, bytes):
        hasher.update(data)
    else:
        # File-like object — read in chunks to handle large artifacts
        while True:
            chunk = data.read(_CHECKSUM_CHUNK_SIZE)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            hasher.update(chunk)

    return hasher.hexdigest()


def compute_checksums(data: bytes | BinaryIO) -> dict[str, str]:
    """Compute SHA-1, SHA-256 **and** MD5 checksums in a single pass.

    For file-like objects the data is read in chunks once and fed to all
    three hashers simultaneously — much more efficient than calling
    :func:`compute_checksum` three times.

    Returns:
        ``{'sha1': '…', 'sha256': '…', 'md5': '…'}``
    """
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()

    if isinstance(data, bytes):
        sha1.update(data)
        sha256.update(data)
        md5.update(data)
    else:
        while True:
            chunk = data.read(_CHECKSUM_CHUNK_SIZE)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            sha1.update(chunk)
            sha256.update(chunk)
            md5.update(chunk)

    return {
        CHECKSUM_SHA1: sha1.hexdigest(),
        CHECKSUM_SHA256: sha256.hexdigest(),
        CHECKSUM_MD5: md5.hexdigest(),
    }


def verify_checksum(
    data: bytes | BinaryIO,
    expected: str,
    algorithm: str = CHECKSUM_SHA1,
) -> bool:
    """Verify that the checksum of *data* matches *expected*.

    Uses constant-time comparison (``hmac.compare_digest``) to prevent
    timing-based side-channel attacks on checksum validation.

    Returns:
        ``True`` when the computed digest equals *expected*.
    """
    actual = compute_checksum(data, algorithm)
    return hmac.compare_digest(actual.lower(), expected.lower())


# ===========================================================================
# Phase 5 — Human-Readable Size Formatting
# ===========================================================================


def format_file_size(size_bytes: int) -> str:
    """Convert a byte count to a human-readable string.

    Examples::

        format_file_size(0)          -> '0 B'
        format_file_size(512)        -> '512 B'
        format_file_size(1024)       -> '1.0 KB'
        format_file_size(1572864)    -> '1.5 MB'
        format_file_size(1073741824) -> '1.0 GB'

    Replaces ``org.apache.commons.io.FileUtils.byteCountToDisplaySize()``.
    """
    if size_bytes < 0:
        raise ValueError("size_bytes must be non-negative")
    if size_bytes == 0:
        return "0 B"

    # Determine the appropriate unit index
    unit_index = min(
        int(math.floor(math.log(size_bytes, SIZE_FACTOR))),
        len(SIZE_UNITS) - 1,
    )

    if unit_index == 0:
        return f"{size_bytes} B"

    value = size_bytes / math.pow(SIZE_FACTOR, unit_index)
    return f"{value:.1f} {SIZE_UNITS[unit_index]}"


def parse_file_size(size_string: str) -> int:
    """Parse a human-readable size string back to bytes.

    Accepted formats (case-insensitive):
    ``'10MB'``, ``'1.5 GB'``, ``'512 KB'``, ``'100B'``.

    Raises:
        ValueError: If *size_string* is not a recognised format.
    """
    if not isinstance(size_string, str) or not size_string.strip():
        raise ValueError(f"Invalid size string: {size_string!r}")

    match = _SIZE_PARSE_RE.match(size_string.strip())
    if not match:
        raise ValueError(f"Invalid size string: {size_string!r}")

    value = float(match.group(1))
    unit = match.group(2).upper()

    try:
        unit_index = SIZE_UNITS.index(unit)
    except ValueError as exc:
        raise ValueError(f"Unrecognised size unit: {unit!r}") from exc

    return int(math.ceil(value * math.pow(SIZE_FACTOR, unit_index)))


# ===========================================================================
# Phase 6 — MIME Type Detection
# ===========================================================================

# Magic-byte signatures for fallback detection
_MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"PK", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"%PDF", "application/pdf"),
    (b"\x89PNG", "image/png"),
    (b"GIF8", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
]

_DEFAULT_MIME_TYPE = "application/octet-stream"


def detect_mime_type(filename: str, data: bytes | None = None) -> str:
    """Detect the MIME type for *filename*, with optional magic-byte fallback.

    1. Try extension-based detection via :mod:`mimetypes` (including
       Nexus-specific types such as ``.pom``, ``.jar``, ``.nupkg``).
    2. If that yields nothing and *data* is provided, inspect the first
       bytes for well-known magic signatures.
    3. Default fallback: ``'application/octet-stream'``.

    Replaces ``javax.activation.MimetypesFileTypeMap`` from the Java source.
    """
    _init_mime_types()

    # Extension-based detection
    mime_type, _ = mimetypes.guess_type(filename, strict=False)
    if mime_type:
        return mime_type

    # Magic-byte fallback
    if data and len(data) >= 2:
        for signature, sig_mime in _MAGIC_SIGNATURES:
            if data[: len(signature)] == signature:
                logger.debug(
                    "MIME type for '%s' detected via magic bytes: %s",
                    filename,
                    sig_mime,
                )
                return sig_mime

    logger.debug(
        "MIME type for '%s' could not be determined; using fallback '%s'",
        filename,
        _DEFAULT_MIME_TYPE,
    )
    return _DEFAULT_MIME_TYPE


# ===========================================================================
# Phase 7 — String Manipulation Utilities
# ===========================================================================


def slugify(
    text: str,
    separator: str = SLUG_SEPARATOR,
    max_length: int = SLUG_MAX_LENGTH,
) -> str:
    """Convert arbitrary text to a URL-safe slug.

    * Lowercases the text
    * Replaces non-alphanumeric characters with *separator*
    * Collapses multiple separators
    * Strips leading/trailing separators
    * Truncates to *max_length*

    Example::

        slugify('My Repository Name!')  ->  'my-repository-name'
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    slug = text.lower().strip()
    # Replace non-alphanumeric chars with separator
    slug = _NON_ALPHANUM_RE.sub(separator, slug)
    # Strip leading/trailing separators
    slug = slug.strip(separator)
    # Truncate to max_length
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip(separator)
    return slug


def truncate(text: str, max_length: int, suffix: str = "...") -> str:
    """Truncate *text* to *max_length*, appending *suffix* if shortened.

    Returns *text* unchanged when its length does not exceed *max_length*.
    """
    if not isinstance(text, str):
        return ""
    if len(text) <= max_length:
        return text
    if max_length <= len(suffix):
        return suffix[:max_length]
    return text[: max_length - len(suffix)] + suffix


def snake_to_camel(name: str) -> str:
    """Convert ``snake_case`` to ``camelCase``.

    Example::

        snake_to_camel('my_field_name')  ->  'myFieldName'
    """
    if not name:
        return name
    parts = name.split("_")
    return parts[0] + "".join(word.capitalize() for word in parts[1:])


def camel_to_snake(name: str) -> str:
    """Convert ``camelCase`` (or ``PascalCase``) to ``snake_case``.

    Example::

        camel_to_snake('myFieldName')  ->  'my_field_name'
    """
    if not name:
        return name
    return _CAMEL_TO_SNAKE_RE.sub("_", name).lower()


# ===========================================================================
# Phase 8 — JSON Helpers
# ===========================================================================


def _json_default_handler(obj: Any) -> Any:
    """Custom default handler for :func:`json.dumps`.

    Serialises types that are not natively JSON-serialisable:

    * ``datetime`` → ISO 8601 string
    * ``uuid.UUID`` → string
    * ``bytes`` → base64-encoded string
    * ``set`` / ``frozenset`` → sorted list
    * ``Path`` → string
    """
    if isinstance(obj, datetime):
        return iso_format(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def safe_json_loads(data: str | bytes, default: Any = None) -> Any:
    """Parse a JSON string, returning *default* on failure.

    Catches :class:`json.JSONDecodeError` and logs a warning.
    """
    if data is None:
        return default
    try:
        return json.loads(data)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Failed to parse JSON data: %s", exc)
        return default


def safe_json_dumps(data: Any, pretty: bool = False) -> str:
    """Serialise *data* to a JSON string with extended type support.

    When *pretty* is ``True`` the output is indented with sorted keys.

    Non-serialisable types are handled by :func:`_json_default_handler`
    (datetime, UUID, bytes, set, Path).
    """
    kwargs: dict[str, Any] = {"default": _json_default_handler}
    if pretty:
        kwargs["indent"] = 2
        kwargs["sort_keys"] = True
    return json.dumps(data, **kwargs)


# ===========================================================================
# Phase 9 — Pagination Utilities
# ===========================================================================


def paginate_query(
    query: Any,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Apply pagination to a SQLAlchemy *query* and return metadata.

    Parameters:
        query: A SQLAlchemy ``Query`` or ``Select`` object.
        page: Page number (1-based). Clamped to >= 1.
        per_page: Items per page. Clamped between 1 and
                  :data:`MAX_PAGE_SIZE`.

    Returns a dict::

        {
            'items':    [row, …],
            'page':     int,
            'per_page': int,
            'total':    int,
            'pages':    int,
            'has_next': bool,
            'has_prev': bool,
        }
    """
    page = max(1, page)
    per_page = max(1, min(per_page, MAX_PAGE_SIZE))

    # Determine total count — support both SQLAlchemy queries and plain lists
    total: int
    is_sqla_query = hasattr(query, "offset") and hasattr(query, "limit")

    if is_sqla_query:
        try:
            total = query.count()
        except Exception:
            total = len(query.all())
    else:
        # Plain list or other sized collection
        if isinstance(query, (list, tuple)):
            total = len(query)
        else:
            total = len(list(query))

    total_pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page

    # Fetch the page of items
    if is_sqla_query:
        try:
            items = query.offset(offset).limit(per_page).all()
        except Exception:
            all_items = query.all()
            items = all_items[offset: offset + per_page]
    else:
        # Plain list or other iterable
        if isinstance(query, (list, tuple)):
            items = list(query[offset: offset + per_page])
        else:
            all_items = list(query)
            items = all_items[offset: offset + per_page]

    return {
        "items": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
    }


def parse_pagination_params(args: dict[str, Any]) -> tuple[int, int]:
    """Extract and validate pagination parameters from request args.

    Parameters:
        args: Dictionary of query parameters (e.g.
              ``request.args.to_dict()``).

    Returns:
        ``(page, per_page)`` tuple with validated values.
    """
    try:
        page = int(args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)

    try:
        per_page = int(args.get("per_page", DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        per_page = DEFAULT_PAGE_SIZE
    per_page = max(1, min(per_page, MAX_PAGE_SIZE))

    return page, per_page


# ===========================================================================
# Phase 10 — Miscellaneous Helpers
# ===========================================================================


def deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge two dictionaries; *override* values take precedence.

    Nested dicts are merged recursively.  All other types in *override*
    replace the corresponding value in *base*.

    Example::

        deep_merge({'a': {'b': 1}}, {'a': {'c': 2}})
        # -> {'a': {'b': 1, 'c': 2}}
    """
    result = base.copy()
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def chunk_iterable(
    iterable: Sequence, chunk_size: int
) -> Generator[list, None, None]:
    """Yield successive chunks of *chunk_size* from *iterable*.

    Example::

        list(chunk_iterable([1, 2, 3, 4, 5], 2))
        # -> [[1, 2], [3, 4], [5]]

    Raises:
        ValueError: If *chunk_size* is less than 1.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    for i in range(0, len(iterable), chunk_size):
        yield list(iterable[i: i + chunk_size])


def retry_with_backoff(
    func: Any,
    max_retries: int = 3,
    backoff_factor: float = 0.5,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Any:
    """Execute *func* with exponential-backoff retry on failure.

    Sleep duration between attempts: ``backoff_factor * (2 ** attempt)``
    seconds.  Only exceptions whose type is in *exceptions* trigger a
    retry; all other exceptions propagate immediately.

    If all *max_retries* attempts are exhausted the last exception is
    re-raised.

    Parameters:
        func: A callable (no arguments) to execute.
        max_retries: Maximum number of retry attempts.
        backoff_factor: Multiplier for the exponential delay.
        exceptions: Tuple of exception classes that should be retried.

    Returns:
        The return value of *func* on success.
    """
    last_exception: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except exceptions as exc:
            last_exception = exc
            if attempt < max_retries:
                sleep_time = backoff_factor * (2 ** attempt)
                logger.warning(
                    "Retry %d/%d for %s after %.2fs: %s",
                    attempt + 1,
                    max_retries,
                    getattr(func, "__name__", repr(func)),
                    sleep_time,
                    exc,
                )
                time.sleep(sleep_time)
    # All retries exhausted — raise the last captured exception
    if last_exception is not None:
        raise last_exception
    # Defensive — should never be reached
    raise RuntimeError("retry_with_backoff exhausted retries with no exception")
