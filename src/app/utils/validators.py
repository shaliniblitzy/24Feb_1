"""
Input validation utilities for the Nexus Repository Flask application.

This module replaces Java Bean Validation (JSR 380) and custom validation logic
from the Nexus Repository Java source. It provides reusable validation functions
used across all layers of the application — API routes, services, and model
pre-save hooks.

Validators enforce business rules for repository names, version strings, file
paths, email addresses, URLs, cron expressions, and content selector expressions
as documented in the Technical Specification.

All validation functions follow the contract:
- Return ``True`` when the input is valid.
- Raise ``ValidationError`` (a subclass of ``ValueError``) when invalid.
- Never silently return ``False``.

All compiled regex patterns are defined at module level for performance, matching
the AAP requirement to avoid per-call compilation overhead.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Module-level logger (replaces SLF4J 1.7.36)
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation error message constants
# ---------------------------------------------------------------------------
INVALID_REPO_NAME_MSG: str = (
    "Repository name must contain only alphanumeric characters, "
    "hyphens, underscores, and dots"
)
INVALID_PATH_MSG: str = "Path contains illegal traversal sequences"
INVALID_VERSION_MSG: str = "Invalid version string format"
INVALID_EMAIL_MSG: str = "Invalid email address format"
INVALID_URL_MSG: str = "Invalid URL format"
INVALID_CRON_MSG: str = "Invalid cron expression format"

# ---------------------------------------------------------------------------
# Compiled regex patterns (module-level for performance)
# ---------------------------------------------------------------------------

# Repository name: alphanumeric start/end, allows ._- in middle, 1-200 chars.
REPO_NAME_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$"
)

# Consecutive dots or hyphens detector.
_CONSECUTIVE_SPECIAL_PATTERN: re.Pattern[str] = re.compile(r"\.\.|--")

# Semantic Versioning (strict): X.Y.Z with optional pre-release and build.
_SEMVER_PATTERN: re.Pattern[str] = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-[a-zA-Z0-9]+(\.[a-zA-Z0-9]+)*)?"
    r"(\+[a-zA-Z0-9]+(\.[a-zA-Z0-9]+)*)?$"
)

# Maven version: digits, dots, hyphens, alphanumeric qualifiers (e.g. 1.0.0-SNAPSHOT).
_MAVEN_VERSION_PATTERN: re.Pattern[str] = re.compile(
    r"^[0-9]+(\.[0-9]+)*(-[a-zA-Z][a-zA-Z0-9]*)?$"
)

# npm artifact version: strict semver (same as SEMVER_PATTERN for artifacts).
_NPM_VERSION_PATTERN: re.Pattern[str] = _SEMVER_PATTERN

# Generic version: non-empty, max 256 chars, no control characters (ASCII 0-31).
_GENERIC_VERSION_PATTERN: re.Pattern[str] = re.compile(
    r"^[^\x00-\x1f]{1,256}$"
)

# Path traversal / illegal character patterns.
_PATH_TRAVERSAL_PATTERN: re.Pattern[str] = re.compile(r"(^|/)\.\.(/|$)")
_CONTROL_CHAR_PATTERN: re.Pattern[str] = re.compile(r"[\x00-\x1f]")
_DOUBLE_SLASH_PATTERN: re.Pattern[str] = re.compile(r"//")

# Email: RFC 5322 simplified.
EMAIL_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)

# Cron field patterns — validated individually per field.
_CRON_MINUTE_RE: re.Pattern[str] = re.compile(
    r"^(\*|([0-5]?\d)(,[0-5]?\d)*"
    r"|([0-5]?\d)-([0-5]?\d)"
    r"|\*/([1-9]\d?))$"
)
_CRON_HOUR_RE: re.Pattern[str] = re.compile(
    r"^(\*|(1?\d|2[0-3])(,(1?\d|2[0-3]))*"
    r"|(1?\d|2[0-3])-(1?\d|2[0-3])"
    r"|\*/([1-9]\d?))$"
)
_CRON_DOM_RE: re.Pattern[str] = re.compile(
    r"^(\*|([1-9]|[12]\d|3[01])(,([1-9]|[12]\d|3[01]))*"
    r"|([1-9]|[12]\d|3[01])-([1-9]|[12]\d|3[01])"
    r"|\*/([1-9]\d?)"
    r"|L|LW|[1-9]W|[12]\dW|3[01]W)$"
)
_MONTH_NAMES = r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
_CRON_MONTH_RE: re.Pattern[str] = re.compile(
    r"^(\*"
    r"|([1-9]|1[0-2])(,([1-9]|1[0-2]))*"
    r"|([1-9]|1[0-2])-([1-9]|1[0-2])"
    r"|\*/([1-9]\d?)"
    r"|" + _MONTH_NAMES + r"(," + _MONTH_NAMES + r")*"
    r"|" + _MONTH_NAMES + r"-" + _MONTH_NAMES + r")$",
    re.IGNORECASE,
)
_DOW_NAMES = r"(?:SUN|MON|TUE|WED|THU|FRI|SAT)"
_CRON_DOW_RE: re.Pattern[str] = re.compile(
    r"^(\*"
    r"|[0-6](,[0-6])*"
    r"|[0-6]-[0-6]"
    r"|\*/[1-6]"
    r"|" + _DOW_NAMES + r"(," + _DOW_NAMES + r")*"
    r"|" + _DOW_NAMES + r"-" + _DOW_NAMES + r""
    r"|[0-6]#[1-5]"
    r"|" + _DOW_NAMES + r"#[1-5]"
    r"|L)$",
    re.IGNORECASE,
)
_CRON_SECOND_RE: re.Pattern[str] = _CRON_MINUTE_RE  # 0-59, same as minute

# Content Selector Expression Language (CSEL) tokens.
_CSEL_VALID_FIELDS: frozenset[str] = frozenset(
    {
        "format",
        "path",
        "coordinate.groupid",
        "coordinate.artifactid",
        "coordinate.version",
        "coordinate.extension",
        "coordinate.classifier",
    }
)
_CSEL_OPERATORS: frozenset[str] = frozenset({"==", "!=", "=^", "=~"})
_CSEL_LOGICAL: frozenset[str] = frozenset({"and", "or", "not"})

# Reserved repository names that must not be used.
_RESERVED_REPO_NAMES: frozenset[str] = frozenset(
    {
        "api",
        "service",
        "health",
        "system",
        "admin",
        "login",
        "logout",
        "static",
        "docs",
        "swagger",
        "metrics",
        "prometheus",
        "internal",
    }
)

# Characters allowed in sanitised path components.
_SAFE_PATH_CHARS: re.Pattern[str] = re.compile(r"[^a-zA-Z0-9._\-]")


# ===================================================================
# Custom Validation Exception
# ===================================================================


class ValidationError(ValueError):
    """Raised when input validation fails.

    Replaces ``InvalidStateException.java`` and Java Bean Validation
    constraint violations.

    Attributes:
        field: Optional name of the input field that failed validation.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        self.field = field
        super().__init__(message)


# ===================================================================
# Repository Name Validation  (Phase 3)
# ===================================================================


def validate_repository_name(name: str) -> bool:
    """Validate a repository name against Nexus naming rules.

    Rules enforced:
    - Only alphanumeric characters, hyphens (``-``), underscores (``_``),
      and dots (``.``) are allowed.
    - Must not start or end with a hyphen or dot.
    - Length must be between 1 and 200 characters.
    - No consecutive dots or hyphens.
    - No path separators (``/``, ``\\``).
    - Must not be a reserved name.

    Args:
        name: The repository name to validate.

    Returns:
        ``True`` when the name is valid.

    Raises:
        ValidationError: When the name is invalid.
    """
    if not name or not isinstance(name, str):
        logger.warning("Repository name validation failed: empty or non-string value")
        raise ValidationError(
            "Repository name must not be empty",
            field="name",
        )

    stripped = name.strip()
    if not stripped:
        raise ValidationError(
            "Repository name must not be empty or whitespace-only",
            field="name",
        )

    if len(stripped) > 200:
        raise ValidationError(
            f"Repository name exceeds maximum length of 200 characters (got {len(stripped)})",
            field="name",
        )

    # Reject path separators.
    if "/" in stripped or "\\" in stripped:
        logger.warning(
            "Repository name validation failed: path separator detected in '%s'",
            stripped,
        )
        raise ValidationError(INVALID_REPO_NAME_MSG, field="name")

    # Regex check — alphanumeric start/end, allowed inner chars.
    if not REPO_NAME_PATTERN.fullmatch(stripped):
        logger.debug(
            "Repository name validation failed: pattern mismatch for '%s'",
            stripped,
        )
        raise ValidationError(INVALID_REPO_NAME_MSG, field="name")

    # Consecutive dots or hyphens.
    if _CONSECUTIVE_SPECIAL_PATTERN.search(stripped):
        raise ValidationError(
            "Repository name must not contain consecutive dots or hyphens",
            field="name",
        )

    # Reserved name check (case-insensitive).
    if stripped.lower() in _RESERVED_REPO_NAMES:
        raise ValidationError(
            f"Repository name '{stripped}' is reserved and cannot be used",
            field="name",
        )

    return True


# ===================================================================
# Version String Validation  (Phase 4)
# ===================================================================


def validate_version_string(
    version: str,
    format_type: str = "generic",
) -> bool:
    """Validate a version string according to the specified format.

    Supported *format_type* values:

    * ``'semver'`` — Strict Semantic Versioning (X.Y.Z with optional
      pre-release / build metadata).
    * ``'maven'`` — Maven convention (``X.Y.Z-qualifier``, e.g.
      ``1.0.0-SNAPSHOT``).
    * ``'npm'`` — npm artifact version (strict semver for stored
      artifacts).
    * ``'generic'`` — Non-empty, max 256 chars, no control characters.

    Args:
        version: The version string to validate.
        format_type: One of ``'semver'``, ``'maven'``, ``'npm'``,
            or ``'generic'``.

    Returns:
        ``True`` when the version is valid.

    Raises:
        ValidationError: When the version is invalid.
    """
    if not version or not isinstance(version, str):
        raise ValidationError(
            "Version string must not be empty",
            field="version",
        )

    pattern_map: dict[str, re.Pattern[str]] = {
        "semver": _SEMVER_PATTERN,
        "maven": _MAVEN_VERSION_PATTERN,
        "npm": _NPM_VERSION_PATTERN,
        "generic": _GENERIC_VERSION_PATTERN,
    }

    fmt = format_type.lower()
    pattern = pattern_map.get(fmt)
    if pattern is None:
        raise ValidationError(
            f"Unsupported format type '{format_type}'. "
            f"Allowed: {', '.join(sorted(pattern_map))}",
            field="format_type",
        )

    if not pattern.fullmatch(version):
        logger.debug(
            "Version string validation failed: '%s' does not match '%s' format",
            version,
            fmt,
        )
        raise ValidationError(
            f"{INVALID_VERSION_MSG}: '{version}' is not a valid {fmt} version",
            field="version",
        )

    return True


# ===================================================================
# Path Validation  (Phase 5) — SECURITY-CRITICAL
# ===================================================================


def validate_path(path: str, allow_absolute: bool = False) -> bool:
    """Validate a filesystem/asset path and reject traversal attacks.

    **CRITICAL SECURITY FUNCTION** — prevents path traversal exploits.

    Rejected patterns:
    - ``..`` path traversal sequences.
    - Null bytes (``\\x00``).
    - Control characters (ASCII 0–31).
    - Backslash path separators (normalised to ``/``).
    - Double forward slashes (``//``).
    - Leading ``/`` when *allow_absolute* is ``False``.

    Args:
        path: The path to validate.
        allow_absolute: When ``True``, paths starting with ``/`` are
            accepted.

    Returns:
        ``True`` when the path is valid.

    Raises:
        ValidationError: When the path is invalid or contains traversal
            sequences.
    """
    if not path or not isinstance(path, str):
        raise ValidationError(
            "Path must not be empty",
            field="path",
        )

    # Reject null bytes immediately.
    if "\x00" in path:
        logger.warning("Path validation blocked null byte in path")
        raise ValidationError(
            "Path must not contain null bytes",
            field="path",
        )

    # Reject control characters (ASCII 0-31).
    if _CONTROL_CHAR_PATTERN.search(path):
        logger.warning("Path validation blocked control character in path")
        raise ValidationError(
            "Path must not contain control characters",
            field="path",
        )

    # Normalise backslashes to forward slashes.
    normalised = path.replace("\\", "/")

    # Reject absolute paths unless explicitly allowed.
    if not allow_absolute and normalised.startswith("/"):
        raise ValidationError(
            "Absolute paths are not allowed",
            field="path",
        )

    # Reject double slashes.
    if _DOUBLE_SLASH_PATTERN.search(normalised):
        raise ValidationError(
            "Path must not contain double slashes",
            field="path",
        )

    # Reject path traversal sequences (.. at boundary).
    if _PATH_TRAVERSAL_PATTERN.search(normalised):
        logger.warning(
            "Path validation blocked traversal attempt: '%s'",
            path,
        )
        raise ValidationError(INVALID_PATH_MSG, field="path")

    # Also catch a bare '..' that is the entire path.
    if normalised == ".." or normalised.startswith("../") or normalised.endswith("/.."):
        logger.warning(
            "Path validation blocked traversal attempt: '%s'",
            path,
        )
        raise ValidationError(INVALID_PATH_MSG, field="path")

    return True


def sanitize_path_component(component: str) -> str:
    """Remove or replace characters that are not safe for filesystem paths.

    Transformations applied:
    1. Replace whitespace with underscores.
    2. Remove characters not in ``[a-zA-Z0-9._-]``.
    3. Strip leading/trailing dots and spaces.
    4. Truncate to 255 characters (max filesystem component length).

    Args:
        component: A single path component (file or directory name).

    Returns:
        The sanitised component string.
    """
    if not component or not isinstance(component, str):
        return ""

    # Replace whitespace runs with a single underscore.
    sanitised = re.sub(r"\s+", "_", component)

    # Remove unsafe characters.
    sanitised = _SAFE_PATH_CHARS.sub("", sanitised)

    # Strip leading/trailing dots and spaces.
    sanitised = sanitised.strip(". ")

    # Enforce max length.
    if len(sanitised) > 255:
        sanitised = sanitised[:255]

    return sanitised


# ===================================================================
# Email Validation  (Phase 6)
# ===================================================================


def validate_email(email: str) -> bool:
    """Validate an email address against a simplified RFC 5322 pattern.

    Length constraints (per RFC 5321):
    - Total address: max 254 characters.
    - Local part (before ``@``): max 64 characters.

    Args:
        email: The email address to validate.

    Returns:
        ``True`` when the email is valid.

    Raises:
        ValidationError: When the email is invalid.
    """
    if not email or not isinstance(email, str):
        raise ValidationError(
            "Email address must not be empty",
            field="email",
        )

    if len(email) > 254:
        raise ValidationError(
            "Email address exceeds maximum length of 254 characters",
            field="email",
        )

    # Local part length check.
    at_idx = email.find("@")
    if at_idx < 0:
        raise ValidationError(INVALID_EMAIL_MSG, field="email")

    local_part = email[:at_idx]
    if len(local_part) > 64:
        raise ValidationError(
            "Email local part exceeds maximum length of 64 characters",
            field="email",
        )

    if not EMAIL_PATTERN.fullmatch(email):
        logger.debug("Email validation failed for: '%s'", email)
        raise ValidationError(INVALID_EMAIL_MSG, field="email")

    return True


# ===================================================================
# URL Validation  (Phase 7)
# ===================================================================


def validate_url(
    url: str,
    allowed_schemes: list[str] | None = None,
) -> bool:
    """Validate a URL's structure, scheme, and hostname.

    Args:
        url: The URL string to validate.
        allowed_schemes: Permitted URL schemes. Defaults to
            ``['http', 'https']``.

    Returns:
        ``True`` when the URL is valid.

    Raises:
        ValidationError: When the URL is malformed or uses a
            disallowed scheme.
    """
    if not url or not isinstance(url, str):
        raise ValidationError(
            "URL must not be empty",
            field="url",
        )

    if len(url) > 2048:
        raise ValidationError(
            "URL exceeds maximum length of 2048 characters",
            field="url",
        )

    schemes = allowed_schemes if allowed_schemes is not None else ["http", "https"]

    try:
        parsed = urlparse(url)
    except Exception:
        raise ValidationError(INVALID_URL_MSG, field="url")

    if not parsed.scheme:
        raise ValidationError(
            "URL must include a scheme (e.g. http:// or https://)",
            field="url",
        )

    if parsed.scheme.lower() not in [s.lower() for s in schemes]:
        raise ValidationError(
            f"URL scheme '{parsed.scheme}' is not allowed. "
            f"Allowed schemes: {', '.join(schemes)}",
            field="url",
        )

    if not parsed.netloc:
        raise ValidationError(
            "URL must include a valid hostname",
            field="url",
        )

    return True


# ===================================================================
# Cron Expression Validation  (Phase 8)
# ===================================================================

# Mapping field index → (name, compiled regex).
_CRON_FIELD_VALIDATORS: list[tuple[str, re.Pattern[str]]] = [
    ("minute", _CRON_MINUTE_RE),
    ("hour", _CRON_HOUR_RE),
    ("day_of_month", _CRON_DOM_RE),
    ("month", _CRON_MONTH_RE),
    ("day_of_week", _CRON_DOW_RE),
]


def validate_cron_expression(expression: str) -> bool:
    """Validate a cron expression compatible with APScheduler.

    Accepted field counts:
    - **5 fields**: ``minute hour day_of_month month day_of_week``
    - **6 fields**: ``second minute hour day_of_month month day_of_week``

    Each field is validated independently against its legal value range and
    supported tokens (``*``, ``*/n``, comma-separated lists, ranges, ``L``,
    ``W``, ``#``).

    Args:
        expression: The cron expression string.

    Returns:
        ``True`` when the expression is valid.

    Raises:
        ValidationError: When the expression is malformed.
    """
    if not expression or not isinstance(expression, str):
        raise ValidationError(
            "Cron expression must not be empty",
            field="cron_expression",
        )

    fields = expression.strip().split()

    if len(fields) == 6:
        # Optional leading seconds field.
        second_field = fields[0]
        if not _CRON_SECOND_RE.fullmatch(second_field):
            raise ValidationError(
                f"{INVALID_CRON_MSG}: invalid seconds field '{second_field}'",
                field="cron_expression",
            )
        remaining_fields = fields[1:]
    elif len(fields) == 5:
        remaining_fields = fields
    else:
        raise ValidationError(
            f"{INVALID_CRON_MSG}: expected 5 or 6 fields, got {len(fields)}",
            field="cron_expression",
        )

    for idx, (field_name, pattern) in enumerate(_CRON_FIELD_VALIDATORS):
        value = remaining_fields[idx]
        if not pattern.fullmatch(value):
            logger.debug(
                "Cron validation failed: field '%s' value '%s' is invalid",
                field_name,
                value,
            )
            raise ValidationError(
                f"{INVALID_CRON_MSG}: invalid {field_name} field '{value}'",
                field="cron_expression",
            )

    return True


# ===================================================================
# Content Selector Expression Validation  (Phase 9)
# ===================================================================


def validate_content_selector_expression(expression: str) -> bool:
    """Validate a CSEL (Content Selector Expression Language) expression.

    CSEL is a simple expression language used for content-based access
    control in Nexus Repository.

    **Syntax examples**::

        format == "maven2"
        format == "maven2" and path =^ "/org/example/"
        path =~ ".*\\\\.jar"

    Supported comparison operators: ``==``, ``!=``, ``=^``, ``=~``.
    Supported logical operators: ``and``, ``or``, ``not``.
    Supported fields: ``format``, ``path``, ``coordinate.groupId``,
    ``coordinate.artifactId``, ``coordinate.version``, etc.

    This function performs **syntax** validation only (no semantic
    evaluation).

    Args:
        expression: The CSEL expression string.

    Returns:
        ``True`` when the syntax is valid.

    Raises:
        ValidationError: When the syntax is invalid.
    """
    if not expression or not isinstance(expression, str):
        raise ValidationError(
            "Content selector expression must not be empty",
            field="expression",
        )

    expr = expression.strip()
    if not expr:
        raise ValidationError(
            "Content selector expression must not be empty or whitespace-only",
            field="expression",
        )

    # ---------------------------------------------------------------
    # Tokenise — split while preserving quoted strings.
    # ---------------------------------------------------------------
    tokens: list[str] = []
    i = 0
    length = len(expr)
    while i < length:
        ch = expr[i]
        # Skip whitespace.
        if ch in (" ", "\t"):
            i += 1
            continue

        # Quoted string literal.
        if ch in ('"', "'"):
            quote_char = ch
            j = i + 1
            while j < length and expr[j] != quote_char:
                if expr[j] == "\\":
                    j += 1  # skip escaped character
                j += 1
            if j >= length:
                raise ValidationError(
                    "Content selector expression has unterminated string literal",
                    field="expression",
                )
            tokens.append(expr[i : j + 1])
            i = j + 1
            continue

        # Multi-character operators: ==, !=, =^, =~
        if i + 1 < length:
            two_char = expr[i : i + 2]
            if two_char in ("==", "!=", "=^", "=~"):
                tokens.append(two_char)
                i += 2
                continue

        # Parentheses.
        if ch in ("(", ")"):
            tokens.append(ch)
            i += 1
            continue

        # Identifiers and keywords (including dotted field names).
        if ch.isalpha() or ch == "_":
            j = i
            while j < length and (expr[j].isalnum() or expr[j] in ("_", ".")):
                j += 1
            tokens.append(expr[i:j])
            i = j
            continue

        # Unknown character — reject.
        raise ValidationError(
            f"Content selector expression has unexpected character '{ch}' "
            f"at position {i}",
            field="expression",
        )

    if not tokens:
        raise ValidationError(
            "Content selector expression is empty after tokenisation",
            field="expression",
        )

    # ---------------------------------------------------------------
    # Structural validation — walk the token list.
    # ---------------------------------------------------------------
    idx = 0
    token_count = len(tokens)
    paren_depth = 0

    def _expect_condition() -> int:  # noqa: C901 — intentionally simple state walk
        """Parse a single condition or a ``not`` prefix + condition."""
        nonlocal paren_depth
        pos = idx

        # Handle ``not`` prefix.
        while pos < token_count and tokens[pos].lower() == "not":
            pos += 1

        if pos >= token_count:
            raise ValidationError(
                "Content selector expression ends unexpectedly after 'not'",
                field="expression",
            )

        # Handle parenthesised sub-expression.
        if tokens[pos] == "(":
            paren_depth += 1
            pos += 1
            pos = _expect_expression(pos)
            if pos >= token_count or tokens[pos] != ")":
                raise ValidationError(
                    "Content selector expression has unmatched parenthesis",
                    field="expression",
                )
            paren_depth -= 1
            pos += 1
            return pos

        # Expect: <field> <operator> <string_literal>
        # Field name.
        field_token = tokens[pos]
        if field_token.startswith(("'", '"')) or field_token in ("(", ")"):
            raise ValidationError(
                f"Content selector expression expected a field name but got "
                f"'{field_token}'",
                field="expression",
            )
        # Validate field name is a known field (case-insensitive).
        if field_token.lower() not in _CSEL_VALID_FIELDS:
            logger.debug(
                "CSEL field '%s' is not a recognised field; allowing for extensibility",
                field_token,
            )
        pos += 1

        # Operator.
        if pos >= token_count:
            raise ValidationError(
                "Content selector expression ends unexpectedly after field name",
                field="expression",
            )
        op_token = tokens[pos]
        if op_token not in _CSEL_OPERATORS:
            raise ValidationError(
                f"Content selector expression has invalid operator '{op_token}'",
                field="expression",
            )
        pos += 1

        # String literal.
        if pos >= token_count:
            raise ValidationError(
                "Content selector expression ends unexpectedly after operator",
                field="expression",
            )
        val_token = tokens[pos]
        if not (val_token.startswith('"') or val_token.startswith("'")):
            raise ValidationError(
                f"Content selector expression expected a quoted string but got "
                f"'{val_token}'",
                field="expression",
            )
        pos += 1
        return pos

    def _expect_expression(start: int) -> int:
        """Parse an expression: condition ((and|or) condition)*."""
        pos = _expect_condition_at(start)
        while pos < token_count:
            tok_lower = tokens[pos].lower()
            if tok_lower in ("and", "or"):
                pos += 1
                pos = _expect_condition_at(pos)
            else:
                break
        return pos

    def _expect_condition_at(start: int) -> int:
        nonlocal idx
        idx = start
        return _expect_condition()

    final_pos = _expect_expression(0)

    if final_pos < token_count:
        raise ValidationError(
            f"Content selector expression has unexpected token "
            f"'{tokens[final_pos]}' at position {final_pos}",
            field="expression",
        )

    if paren_depth != 0:
        raise ValidationError(
            "Content selector expression has unmatched parenthesis",
            field="expression",
        )

    return True


# ===================================================================
# Utility Validation Functions  (Phase 10)
# ===================================================================


def validate_not_empty(value: str, field_name: str = "value") -> bool:
    """Validate that a string is not ``None``, empty, or whitespace-only.

    Args:
        value: The string to validate.
        field_name: Name of the field (used in the error message).

    Returns:
        ``True`` when the value is non-empty.

    Raises:
        ValidationError: When the value is ``None``, empty, or
            whitespace-only.
    """
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"{field_name} must not be empty",
            field=field_name,
        )
    return True


def validate_max_length(
    value: str,
    max_length: int,
    field_name: str = "value",
) -> bool:
    """Validate that a string does not exceed *max_length*.

    Args:
        value: The string to validate.
        max_length: Maximum allowed length.
        field_name: Name of the field (used in the error message).

    Returns:
        ``True`` when the string is within the allowed length.

    Raises:
        ValidationError: When the string exceeds *max_length*.
    """
    if not isinstance(value, str):
        raise ValidationError(
            f"{field_name} must be a string",
            field=field_name,
        )
    if len(value) > max_length:
        raise ValidationError(
            f"{field_name} exceeds maximum length of {max_length} characters "
            f"(got {len(value)})",
            field=field_name,
        )
    return True


def validate_in_choices(
    value: str,
    choices: list[str],
    field_name: str = "value",
) -> bool:
    """Validate that *value* is one of the allowed *choices*.

    Args:
        value: The string to validate.
        choices: Allowed values.
        field_name: Name of the field (used in the error message).

    Returns:
        ``True`` when the value is within the allowed choices.

    Raises:
        ValidationError: When the value is not in *choices*.
    """
    if value not in choices:
        allowed = ", ".join(repr(c) for c in choices)
        raise ValidationError(
            f"{field_name} must be one of [{allowed}], got '{value}'",
            field=field_name,
        )
    return True


def validate_json_string(value: str) -> bool:
    """Validate that *value* is a well-formed JSON string.

    Args:
        value: The string to parse as JSON.

    Returns:
        ``True`` when the string is valid JSON.

    Raises:
        ValidationError: When the string is not valid JSON.
    """
    if not value or not isinstance(value, str):
        raise ValidationError(
            "JSON string must not be empty",
            field="json",
        )
    try:
        json.loads(value)
    except json.JSONDecodeError as exc:
        logger.debug("JSON validation failed: %s", exc)
        raise ValidationError(
            f"Invalid JSON: {exc}",
            field="json",
        ) from exc
    return True
