"""
Typed exception hierarchy for the Nexus Repository Manager.

Defines all application-specific exceptions organized into four categories
matching the original Java implementation:

    1. Client Errors (4xx) — Invalid requests, authentication/authorization failures
    2. System Errors (5xx) — Unexpected internal failures
    3. Configuration Errors (5xx) — Invalid settings or state transitions
    4. Security Errors (5xx) — Security infrastructure failures

All exceptions inherit from ``NexusError`` and expose a consistent interface:

    - ``message``     – Human-readable error description
    - ``error_type``  – Machine-readable error category string
    - ``status_code`` – HTTP status code for API responses
    - ``details``     – Optional dict carrying additional context
    - ``to_dict()``   – Serialises the error into a JSON-compatible dict

The ``to_dict()`` output matches the backward-compatible error response format
documented in AAP Section 0.7.3::

    {
        "error": "<error_type>",
        "message": "<human-readable message>",
        "status": <http_status_code>,
        "details": { ... }
    }

Important design notes
----------------------
* **No internal imports** — this module must *never* import from other ``src``
  packages to prevent circular dependencies.
* **NexusSystemError** is used instead of ``SystemError`` to avoid shadowing
  Python's built-in ``SystemError``.
* **Authentication vs Authorisation** — ``AuthenticationError`` (401) and
  ``AuthorizationError`` (403) are deliberately separate; 401 is returned
  for unauthenticated requests and 403 only after successful authentication
  (AAP Section 0.7.2).
"""

from __future__ import annotations

from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Base exception
# ---------------------------------------------------------------------------

class NexusError(Exception):
    """Base exception for all Nexus Repository errors.

    Every application exception inherits from this class so that a single
    Flask error handler can catch all application errors and render a
    structured JSON response.

    Attributes:
        message: Human-readable error description.
        error_type: Machine-readable error category (e.g. ``'client_error'``).
        status_code: HTTP status code for the API response.
        details: Optional dictionary with additional context about the error.
    """

    status_code: int = 500
    error_type: str = "nexus_error"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        resolved_message = message or "An unexpected error occurred"
        super().__init__(resolved_message)
        self.message: str = resolved_message
        self.details: Dict[str, Any] = details if details is not None else {}

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the exception to a JSON-compatible dictionary.

        The structure follows the backward-compatible error response format
        required by AAP Section 0.7.3.  Stack traces are intentionally
        *excluded* — the Flask error handler in ``src/app.py`` is responsible
        for deciding whether to include debug information.

        Returns:
            Dictionary with keys ``error``, ``message``, ``status`` and,
            when present, ``details``.
        """
        result: Dict[str, Any] = {
            "error": self.error_type,
            "message": self.message,
            "status": self.status_code,
        }
        if self.details:
            result["details"] = self.details
        return result

    def __repr__(self) -> str:  # pragma: no cover – convenience repr
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"status_code={self.status_code}, "
            f"error_type={self.error_type!r})"
        )


# ---------------------------------------------------------------------------
# Client errors (4xx)
# ---------------------------------------------------------------------------

class ClientError(NexusError):
    """General client-side error for malformed requests.

    Covers malformed request bodies, missing required parameters, and
    invalid query arguments.  More specific sub-classes (e.g.
    ``ValidationError``) should be preferred where they apply.

    HTTP status: **400 Bad Request**
    """

    status_code: int = 400
    error_type: str = "client_error"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message or "Bad request",
            details=details,
        )


class AuthenticationError(NexusError):
    """Raised when a request cannot be authenticated.

    This exception maps to HTTP **401 Unauthorized** and is used for:

    * Missing ``Authorization`` header on a protected endpoint
    * Invalid or expired credentials / tokens
    * Disabled or locked user accounts

    Per AAP Section 0.7.2 the auth chain must *fully authenticate* the
    request before any RBAC evaluation occurs.  Unauthenticated requests
    always receive 401 — **never** 403.
    """

    status_code: int = 401
    error_type: str = "authentication_error"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message or "Authentication required",
            details=details,
        )


class AuthorizationError(NexusError):
    """Raised when an authenticated user lacks required permissions.

    This exception maps to HTTP **403 Forbidden** and is used *only after*
    successful authentication (AAP Section 0.7.2: "Authentication Before
    Authorization").  It covers:

    * Insufficient system-wide privileges
    * Missing repository-scoped permissions
    * Content Selector Expression Language (CSEL) denial at the
      sub-repository level

    If the user has *not* been authenticated yet, raise
    ``AuthenticationError`` instead.
    """

    status_code: int = 403
    error_type: str = "authorization_error"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message or "Insufficient permissions",
            details=details,
        )


class NotFoundError(NexusError):
    """Raised when a requested resource does not exist.

    HTTP status: **404 Not Found**

    Typical resources: repositories, components, assets, users, roles,
    privileges, BlobStore configurations, cleanup policies, tasks.

    Callers are encouraged to populate ``details`` with the resource type
    and identifier for easier debugging::

        raise NotFoundError(
            message="Repository not found",
            details={"resource_type": "repository", "resource_id": "maven-central"},
        )
    """

    status_code: int = 404
    error_type: str = "not_found"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message or "Resource not found",
            details=details,
        )


# ---------------------------------------------------------------------------
# Server errors (5xx)
# ---------------------------------------------------------------------------

class NexusSystemError(NexusError):
    """Raised on unexpected internal system failures.

    HTTP status: **500 Internal Server Error**

    Covers scenarios such as:

    * Unrecoverable database errors
    * Unexpected BlobStore I/O failures
    * Third-party service outages

    .. note::
        Named ``NexusSystemError`` instead of ``SystemError`` to avoid
        shadowing Python's built-in ``SystemError`` exception.
    """

    status_code: int = 500
    error_type: str = "system_error"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message or "An internal system error occurred",
            details=details,
        )


class ConfigError(NexusError):
    """Raised for configuration and state-transition errors.

    HTTP status: **500 Internal Server Error**

    Typical causes:

    * Missing required configuration keys
    * Invalid configuration values
    * Illegal repository lifecycle state transitions
      (AAP Section 0.7.1: *"Invalid transitions must raise ConfigError"*)
    * Incompatible component configuration combinations
    """

    status_code: int = 500
    error_type: str = "configuration_error"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message or "Configuration error",
            details=details,
        )


# ---------------------------------------------------------------------------
# Specialised / domain-specific exceptions
# ---------------------------------------------------------------------------

class ValidationError(ClientError):
    """Raised when request body or parameter validation fails.

    Inherits from ``ClientError`` (HTTP **400**) and is the preferred
    exception for marshmallow schema failures, field-level constraint
    violations, and request body format issues.

    Callers should populate ``details`` with per-field error information::

        raise ValidationError(
            message="Request validation failed",
            details={
                "fields": {
                    "name": ["Field is required."],
                    "format": ["Invalid format 'xyz'. Must be one of: maven, npm, docker."],
                }
            },
        )
    """

    status_code: int = 400
    error_type: str = "validation_error"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message or "Validation failed",
            details=details,
        )


class ConflictError(NexusError):
    """Raised on resource conflicts such as duplicate creation attempts.

    HTTP status: **409 Conflict**

    Typical causes:

    * Attempting to create a resource whose unique key already exists
      (e.g. duplicate repository name, duplicate username)
    * Optimistic locking failures where a concurrent update has already
      modified the resource
    """

    status_code: int = 409
    error_type: str = "conflict"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message or "Resource conflict",
            details=details,
        )


class BlobStoreError(NexusError):
    """Raised when a BlobStore operation fails.

    HTTP status: **500 Internal Server Error**

    Covers failures specific to the storage layer:

    * File BlobStore I/O errors (disk full, permission denied)
    * S3 BlobStore errors (network timeout, access denied, bucket not found)
    * Integrity verification failures during compaction or read-back
    * Soft-delete or hard-delete failures
    """

    status_code: int = 500
    error_type: str = "blobstore_error"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message or "BlobStore operation failed",
            details=details,
        )


class SecurityError(NexusError):
    """Raised for security infrastructure failures.

    HTTP status: **500 Internal Server Error**

    Unlike ``AuthenticationError`` (401) and ``AuthorizationError`` (403)
    which represent normal access-control outcomes, ``SecurityError``
    indicates that the security *infrastructure itself* has failed:

    * LDAP / Active Directory connection failures
    * SSL/TLS certificate loading or validation errors
    * Cryptographic operation failures (HMAC computation, key generation)
    * SAML / OpenID Connect provider communication errors
    """

    status_code: int = 500
    error_type: str = "security_error"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message or "Security infrastructure error",
            details=details,
        )


# ---------------------------------------------------------------------------
# Convenience collection for use by Flask error handler registration
# ---------------------------------------------------------------------------

# All exception classes exposed by this module, ordered from base to leaf.
# This list can be used by ``src/app.py`` to register error handlers for
# every application exception type in a single loop.
__all__ = [
    "NexusError",
    "ClientError",
    "AuthenticationError",
    "AuthorizationError",
    "NotFoundError",
    "NexusSystemError",
    "ConfigError",
    "ValidationError",
    "ConflictError",
    "BlobStoreError",
    "SecurityError",
]
