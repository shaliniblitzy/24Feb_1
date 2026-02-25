"""
Input validation utilities for the Flask Binary Repository Management System.

Provides comprehensive input validation, name sanitization, path normalization,
and size limit enforcement used across the application to ensure data integrity
and prevent injection attacks.

Functions:
    validate_name        — Validate a name string for use as an entity identifier
    validate_repository_name — Validate a repository name specifically
    sanitize_name        — Clean and normalize a name by removing dangerous characters
    normalize_path       — Normalize a file path for safe storage and retrieval
    validate_size        — Validate that a numeric size is within acceptable limits
    validate_content_length — Validate HTTP content length with strict type checking
    validate_required_fields — Validate that required fields are present in a dict
    is_valid_url         — Validate a URL string (http/https only)
    is_valid_email       — Validate an email address format
    validate_path_component — Validate a single path component (no slashes, traversal)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Union
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_NAME_LENGTH: int = 255
"""Maximum allowed length for entity names (repositories, users, assets)."""

MIN_NAME_LENGTH: int = 1
"""Minimum allowed length for entity names."""

MAX_PATH_LENGTH: int = 1024
"""Maximum allowed length for normalized file paths."""

VALID_NAME_PATTERN: re.Pattern = re.compile(r"^[a-zA-Z0-9._-]+$")
"""Compiled regex for allowed characters in entity names: alphanumeric, dot,
underscore, and hyphen."""

DANGEROUS_CHARS_PATTERN: re.Pattern = re.compile(
    r"""[<>"';`\\&|$(){}[\]!@#%^*+=~,?]"""
)
"""Compiled regex matching characters considered dangerous for name input."""

EMAIL_PATTERN: re.Pattern = re.compile(
    r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
)
"""Compiled regex for basic email address format validation."""


# ---------------------------------------------------------------------------
# Name Validation
# ---------------------------------------------------------------------------


def validate_name(name: Any, max_length: int = MAX_NAME_LENGTH) -> bool:
    """Validate a name string for use as a repository, user, or asset identifier.

    Accepts names containing only ASCII alphanumeric characters, hyphens,
    underscores, and dots.  Leading and trailing whitespace is stripped
    before validation.

    Parameters
    ----------
    name : str
        The name to validate.  Must be a non-empty string after stripping.
    max_length : int, optional
        Maximum allowed length after stripping (default ``255``).

    Returns
    -------
    bool
        ``True`` if the name is valid, ``False`` otherwise.

    Raises
    ------
    TypeError
        If *name* is not a string (e.g. ``None``, ``int``).
    """
    if name is None:
        raise TypeError("Name must be a string, not NoneType")
    if not isinstance(name, str):
        raise TypeError(f"Name must be a string, not {type(name).__name__}")

    stripped = name.strip()
    if not stripped:
        return False
    if len(stripped) > max_length:
        return False
    if not VALID_NAME_PATTERN.match(stripped):
        return False
    return True


def validate_repository_name(name: Any) -> bool:
    """Validate a repository name.

    Delegates to :func:`validate_name` with the default constraints.

    Parameters
    ----------
    name : str
        The repository name to validate.

    Returns
    -------
    bool
        ``True`` if the repository name is valid.
    """
    return validate_name(name)


# ---------------------------------------------------------------------------
# Name Sanitization
# ---------------------------------------------------------------------------


def sanitize_name(name: Any) -> str:
    """Clean and normalize a name by removing dangerous characters.

    The sanitization pipeline:

    1. Strip leading / trailing whitespace.
    2. Replace spaces with hyphens.
    3. Remove null bytes.
    4. Remove dangerous characters (``<>``, quotes, semicolons, etc.).
    5. Remove path-traversal sequences (``../``, ``..\\``, ``..``, ``/``, ``\\``).
    6. Convert to lowercase.
    7. Collapse consecutive hyphens, underscores, and dots.
    8. Strip leading / trailing hyphens, underscores, and dots.
    9. Truncate to :data:`MAX_NAME_LENGTH`.

    Parameters
    ----------
    name : str
        The raw name to sanitize.

    Returns
    -------
    str
        The sanitized name.  May be empty if the input consisted entirely
        of disallowed characters.

    Raises
    ------
    TypeError
        If *name* is not a string.
    """
    if not isinstance(name, str):
        raise TypeError(f"Name must be a string, not {type(name).__name__}")

    result = name.strip()

    # Replace spaces with hyphens
    result = result.replace(" ", "-")

    # Remove null bytes
    result = result.replace("\x00", "")

    # Remove dangerous characters
    result = re.sub(r"""[<>"';`\\&|$(){}[\]!@#%^*+=~,?]""", "", result)

    # Remove path-traversal components
    result = result.replace("../", "").replace("..\\", "").replace("..", "")
    result = result.replace("/", "").replace("\\", "")

    # Lowercase
    result = result.lower()

    # Collapse consecutive hyphens / underscores / dots
    result = re.sub(r"-{2,}", "-", result)
    result = re.sub(r"_{2,}", "_", result)
    result = re.sub(r"\.{2,}", ".", result)

    # Strip leading / trailing separators
    result = result.strip("-_.")

    # Truncate
    if len(result) > MAX_NAME_LENGTH:
        result = result[:MAX_NAME_LENGTH]

    return result


# ---------------------------------------------------------------------------
# Path Normalization
# ---------------------------------------------------------------------------


def normalize_path(path: Any, max_length: int = MAX_PATH_LENGTH) -> str:
    """Normalize a file path for safe storage and retrieval.

    The normalization pipeline:

    1. Remove null bytes.
    2. Convert backslashes to forward slashes.
    3. Collapse multiple consecutive slashes.
    4. Strip leading and trailing slashes.
    5. Resolve ``.`` (current-directory) and ``..`` (parent-directory)
       segments.  A ``..`` that would escape the base directory raises
       :class:`ValueError`.
    6. Enforce maximum path length.

    Parameters
    ----------
    path : str
        The raw file path to normalize.
    max_length : int, optional
        Maximum allowed length for the normalized path (default ``1024``).

    Returns
    -------
    str
        The normalized path.

    Raises
    ------
    TypeError
        If *path* is not a string.
    ValueError
        If the path contains directory-traversal that escapes the base
        directory, or if the normalized path exceeds *max_length*.
    """
    if path is None:
        raise TypeError("Path must be a string, not NoneType")
    if not isinstance(path, str):
        raise TypeError(f"Path must be a string, not {type(path).__name__}")

    if not path:
        return ""

    # Remove null bytes
    result = path.replace("\x00", "")

    # Convert backslashes to forward slashes
    result = result.replace("\\", "/")

    # Collapse multiple consecutive slashes
    result = re.sub(r"/+", "/", result)

    # Strip leading and trailing slashes
    result = result.strip("/")

    # Resolve . and .. segments
    parts = result.split("/")
    resolved: list[str] = []
    for part in parts:
        if part == ".":
            continue
        elif part == "..":
            if resolved:
                resolved.pop()
            else:
                raise ValueError(
                    "Path traversal detected: path escapes base directory"
                )
        elif part:
            resolved.append(part)

    result = "/".join(resolved)

    # Enforce maximum length
    if len(result) > max_length:
        raise ValueError(
            f"Path exceeds maximum length of {max_length} characters"
        )

    return result


# ---------------------------------------------------------------------------
# Size Validation
# ---------------------------------------------------------------------------


def validate_size(size: Any, max_size: int) -> bool:
    """Validate that a numeric size is within acceptable limits.

    Parameters
    ----------
    size : int or float
        The size value to validate.
    max_size : int
        The upper bound (inclusive).

    Returns
    -------
    bool
        ``True`` if ``0 <= size <= max_size``.

    Raises
    ------
    TypeError
        If *size* is not a number (``int`` or ``float``), or is ``None``
        or ``bool``.
    """
    if size is None:
        raise TypeError("Size must be a number, not NoneType")
    if isinstance(size, bool):
        raise TypeError("Size must be a number, not bool")
    if not isinstance(size, (int, float)):
        raise TypeError(f"Size must be a number, not {type(size).__name__}")

    if size < 0:
        return False
    if size > max_size:
        return False
    return True


def validate_content_length(size: Any, max_size: int) -> bool:
    """Validate HTTP content length with stricter type checking.

    Behaves like :func:`validate_size` but additionally rejects ``str``
    values with a clear error message.

    Parameters
    ----------
    size : int or float
        The content length value.
    max_size : int
        The upper bound (inclusive).

    Returns
    -------
    bool
        ``True`` if the content length is valid.

    Raises
    ------
    TypeError
        If *size* is not a numeric type.
    """
    if size is None:
        raise TypeError("Size must be a number, not NoneType")
    if isinstance(size, str):
        raise TypeError("Size must be a number, not str")
    if isinstance(size, bool):
        raise TypeError("Size must be a number, not bool")
    if not isinstance(size, (int, float)):
        raise TypeError(f"Size must be a number, not {type(size).__name__}")
    return validate_size(size, max_size)


# ---------------------------------------------------------------------------
# Required Fields Validation
# ---------------------------------------------------------------------------


def validate_required_fields(
    data: Any,
    required: Sequence[str],
) -> bool:
    """Validate that all required fields are present and non-None in *data*.

    A field whose value is ``None`` is treated as missing.  Empty strings
    and other falsy values (``0``, ``False``, ``[]``) are considered
    present.

    Parameters
    ----------
    data : dict
        The data dictionary to validate.
    required : list of str
        Field names that must be present with non-None values.

    Returns
    -------
    bool
        ``True`` when all required fields are present.

    Raises
    ------
    TypeError
        If *data* is not a ``dict`` or *required* is not a list/tuple.
    ValueError
        If a required field is missing or has a ``None`` value.
    """
    if not isinstance(data, dict):
        raise TypeError(f"Data must be a dict, not {type(data).__name__}")
    if not isinstance(required, (list, tuple)):
        raise TypeError("Required fields must be a list or tuple")

    for field in required:
        if field not in data or data[field] is None:
            raise ValueError(f"Missing required field: '{field}'")

    return True


# ---------------------------------------------------------------------------
# URL Validation
# ---------------------------------------------------------------------------


def is_valid_url(url: Any) -> bool:
    """Validate a URL string (only ``http`` and ``https`` schemes).

    Parameters
    ----------
    url : str
        The URL to validate.

    Returns
    -------
    bool
        ``True`` if the URL is valid with an ``http`` or ``https`` scheme.
    """
    if url is None or not isinstance(url, str):
        return False
    if not url.strip():
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Email Validation
# ---------------------------------------------------------------------------


def is_valid_email(email: Any) -> bool:
    """Validate an email address against a basic format pattern.

    Parameters
    ----------
    email : str
        The email address to validate.

    Returns
    -------
    bool
        ``True`` if the email matches the expected format.
    """
    if email is None or not isinstance(email, str):
        return False
    if not email.strip():
        return False
    return bool(EMAIL_PATTERN.match(email))


# ---------------------------------------------------------------------------
# Path Component Validation
# ---------------------------------------------------------------------------


def validate_path_component(component: Any) -> bool:
    """Validate a single path component (file or directory name).

    Rejects components containing slashes, null bytes, or directory-traversal
    sequences (``.`` and ``..``).

    Parameters
    ----------
    component : str
        The path component to validate.

    Returns
    -------
    bool
        ``True`` if the component is safe for use in a file path.

    Raises
    ------
    TypeError
        If *component* is not a string.
    """
    if component is None:
        raise TypeError("Component must be a string, not NoneType")
    if not isinstance(component, str):
        raise TypeError(
            f"Component must be a string, not {type(component).__name__}"
        )

    if not component:
        return False

    # Reject null bytes
    if "\x00" in component:
        return False

    # Reject slashes
    if "/" in component or "\\" in component:
        return False

    # Reject directory traversal
    if component in (".", ".."):
        return False

    return True
