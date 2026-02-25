"""
Marshmallow serialization schemas for system configuration and status.

This module defines Marshmallow 3.x schema classes for system management
API endpoints, supporting Feature F-404 (System Configuration Management)
and Feature F-401 (Health Checks and Monitoring). These schemas replace
the system management Jackson 2.16.1 DTOs from the Java source system.

Schemas defined:
    - SystemConfigSchema: Serializes/deserializes system configuration entries
    - SystemConfigUpdateSchema: Validates updates to individual config entries
    - SystemConfigBulkSchema: Validates bulk configuration update requests
    - SystemStatusSchema: Read-only system status and health overview
    - LicenseInfoSchema: Read-only license information response

All DateTime fields serialize to ISO 8601 format by default via Marshmallow.
The ``value`` field in config schemas is always stored as a string; type
conversions (to bool, int, list) are handled at the model/service layer.
"""

from __future__ import annotations

from marshmallow import (
    Schema,
    fields,
    validate,
    validates,
    ValidationError,
    pre_load,
    post_dump,
)
from marshmallow.validate import Length, OneOf


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Valid system configuration categories matching the SystemConfig model.
VALID_CATEGORIES: list[str] = [
    "security",
    "http",
    "system",
    "email",
    "repository",
    "cleanup",
    "scheduling",
]

#: Valid system status values.
VALID_STATUSES: list[str] = [
    "running",
    "starting",
    "stopping",
    "error",
]

#: Valid edition identifiers.
VALID_EDITIONS: list[str] = [
    "OSS",
    "PRO",
]

#: Valid license types.
VALID_LICENSE_TYPES: list[str] = [
    "open-source",
    "commercial",
]

#: Valid database backend types.
VALID_DATABASE_TYPES: list[str] = [
    "sqlite",
    "postgresql",
]


# ---------------------------------------------------------------------------
# SystemConfigSchema
# ---------------------------------------------------------------------------

class SystemConfigSchema(Schema):
    """Schema for serializing and deserializing system configuration entries.

    Mirrors the ``SystemConfig`` SQLAlchemy model from
    ``src/app/models/system_config.py``.  The ``key`` field uses
    dot-notation (e.g. ``'security.anonymousAccess'``,
    ``'http.proxy.host'``) and acts as the primary identifier.

    The ``value`` field is always persisted as a string.  Conversion to
    native Python types (``bool``, ``int``, ``list``) is the responsibility
    of the model's convenience properties (``as_bool``, ``as_int``,
    ``as_list``).

    Example payload::

        {
            "key": "security.anonymousAccess",
            "value": "true",
            "category": "security",
            "description": "Allow anonymous read access to public repositories"
        }
    """

    key = fields.String(
        required=True,
        validate=Length(min=1, max=500),
        metadata={
            "description": (
                "Dot-notation configuration key. "
                "Examples: 'security.anonymousAccess', 'http.proxy.host', "
                "'system.baseUrl', 'email.server.host', "
                "'repository.blobstore.default'."
            ),
        },
    )

    value = fields.String(
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Configuration value stored as text. Booleans encoded as "
                "'true'/'false', integers as digit strings, lists as JSON "
                "arrays."
            ),
        },
    )

    category = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        validate=[
            Length(max=100),
            OneOf(
                VALID_CATEGORIES,
                error=(
                    "Category must be one of: {choices}."
                ),
            ),
        ],
        metadata={
            "description": (
                "Logical grouping for the configuration entry. "
                "Valid values: 'security', 'http', 'system', 'email', "
                "'repository', 'cleanup', 'scheduling'."
            ),
        },
    )

    description = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            "description": "Human-readable description of the configuration entry.",
        },
    )

    created_at = fields.DateTime(
        dump_only=True,
        metadata={
            "description": "Timestamp when the configuration entry was created (ISO 8601).",
        },
    )

    updated_at = fields.DateTime(
        dump_only=True,
        metadata={
            "description": "Timestamp when the configuration entry was last updated (ISO 8601).",
        },
    )

    # -- Validators -----------------------------------------------------------

    @validates("key")
    def validate_key(self, value: str) -> None:
        """Ensure the configuration key is a valid dot-notation identifier.

        Keys must not be blank and should contain only alphanumeric
        characters, dots, hyphens, and underscores.
        """
        if not value or not value.strip():
            raise ValidationError("Configuration key must not be blank.")

        # Allow alphanumeric, dots, hyphens, and underscores in keys
        allowed_chars = set(
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789._-"
        )
        if not all(ch in allowed_chars for ch in value):
            raise ValidationError(
                "Configuration key must contain only alphanumeric "
                "characters, dots, hyphens, and underscores."
            )

    @pre_load
    def strip_key_whitespace(self, data: dict, **kwargs) -> dict:
        """Strip leading/trailing whitespace from the key field on load."""
        if isinstance(data, dict) and "key" in data:
            raw_key = data["key"]
            if isinstance(raw_key, str):
                data["key"] = raw_key.strip()
        return data

    @post_dump
    def remove_none_optional_fields(self, data: dict, **kwargs) -> dict:
        """Remove ``None``-valued optional fields for cleaner JSON output."""
        optional_fields = ("category", "description")
        return {
            key: val
            for key, val in data.items()
            if not (val is None and key in optional_fields)
        }


# ---------------------------------------------------------------------------
# SystemConfigUpdateSchema
# ---------------------------------------------------------------------------

class SystemConfigUpdateSchema(Schema):
    """Minimal schema for PUT/PATCH updates to individual config entries.

    Only the ``value`` and ``description`` fields are accepted.  The
    ``key`` is taken from the URL path parameter rather than the request
    body.

    Example payload::

        {
            "value": "false",
            "description": "Disable anonymous access"
        }
    """

    value = fields.String(
        required=True,
        allow_none=True,
        metadata={
            "description": "New configuration value (string-encoded).",
        },
    )

    description = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            "description": "Optional updated description for the config entry.",
        },
    )


# ---------------------------------------------------------------------------
# SystemConfigBulkSchema
# ---------------------------------------------------------------------------

class SystemConfigBulkSchema(Schema):
    """Schema for bulk system configuration updates.

    Wraps a list of ``SystemConfigSchema`` entries so that multiple
    configuration values can be set in a single API call.

    Example payload::

        {
            "configs": [
                {"key": "security.anonymousAccess", "value": "false"},
                {"key": "http.proxy.host", "value": "proxy.example.com"}
            ]
        }
    """

    configs = fields.List(
        fields.Nested(SystemConfigSchema),
        required=True,
        validate=Length(min=1),
        metadata={
            "description": "List of system configuration entries to create or update.",
        },
    )

    @validates("configs")
    def validate_configs_not_empty(self, value: list) -> None:
        """Ensure at least one configuration entry is provided."""
        if not value:
            raise ValidationError(
                "At least one configuration entry must be provided."
            )

    @validates("configs")
    def validate_unique_keys(self, value: list) -> None:
        """Ensure no duplicate keys exist within a single bulk request."""
        keys: list[str] = []
        for entry in value:
            key = entry.get("key")
            if key is not None:
                if key in keys:
                    raise ValidationError(
                        f"Duplicate configuration key: '{key}'. "
                        "Each key must appear only once in a bulk update."
                    )
                keys.append(key)


# ---------------------------------------------------------------------------
# SystemStatusSchema
# ---------------------------------------------------------------------------

class SystemStatusSchema(Schema):
    """Read-only schema for the system status / health overview response.

    All fields are ``dump_only`` because this schema is strictly for
    serializing system status information — it is never deserialized from
    client input.

    Supports Feature F-401 (Health Checks and Monitoring) by exposing
    live statistics (repository count, component count, etc.) alongside
    runtime metadata (Python version, Flask version, database type).

    Example response::

        {
            "version": "1.0.0",
            "edition": "OSS",
            "status": "running",
            "uptime_seconds": 86400,
            "started_at": "2026-02-24T00:00:00Z",
            "node_id": "node-1",
            "python_version": "3.12.3",
            "flask_version": "3.1.3",
            "database_type": "postgresql",
            "elasticsearch_available": true,
            "blob_stores": 2,
            "repositories": 15,
            "components": 50000,
            "assets": 120000,
            "users": 42
        }
    """

    version = fields.String(
        dump_only=True,
        metadata={"description": "Application version string (e.g. '1.0.0')."},
    )

    edition = fields.String(
        dump_only=True,
        validate=OneOf(VALID_EDITIONS),
        metadata={"description": "Application edition ('OSS' or 'PRO')."},
    )

    status = fields.String(
        dump_only=True,
        validate=OneOf(VALID_STATUSES),
        metadata={
            "description": (
                "Current system status. One of 'running', 'starting', "
                "'stopping', 'error'."
            ),
        },
    )

    uptime_seconds = fields.Integer(
        dump_only=True,
        metadata={"description": "System uptime in seconds since last start."},
    )

    started_at = fields.DateTime(
        dump_only=True,
        metadata={"description": "Timestamp when the system was started (ISO 8601)."},
    )

    node_id = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={
            "description": (
                "Cluster node identifier. Null for standalone deployments."
            ),
        },
    )

    python_version = fields.String(
        dump_only=True,
        metadata={"description": "Python runtime version (e.g. '3.12.3')."},
    )

    flask_version = fields.String(
        dump_only=True,
        metadata={"description": "Flask framework version (e.g. '3.1.3')."},
    )

    database_type = fields.String(
        dump_only=True,
        validate=OneOf(VALID_DATABASE_TYPES),
        metadata={
            "description": "Active database backend ('sqlite' or 'postgresql').",
        },
    )

    elasticsearch_available = fields.Boolean(
        dump_only=True,
        metadata={
            "description": "Whether an Elasticsearch connection is currently active.",
        },
    )

    blob_stores = fields.Integer(
        dump_only=True,
        metadata={"description": "Number of configured BlobStores."},
    )

    repositories = fields.Integer(
        dump_only=True,
        metadata={"description": "Total number of configured repositories."},
    )

    components = fields.Integer(
        dump_only=True,
        metadata={"description": "Total number of indexed components."},
    )

    assets = fields.Integer(
        dump_only=True,
        metadata={"description": "Total number of indexed assets."},
    )

    users = fields.Integer(
        dump_only=True,
        metadata={"description": "Total number of registered users."},
    )


# ---------------------------------------------------------------------------
# LicenseInfoSchema
# ---------------------------------------------------------------------------

class LicenseInfoSchema(Schema):
    """Read-only schema for license information.

    Used by ``GET /api/v1/system/license``.  Supports both the OSS
    edition (no expiration, ``license_type='open-source'``) and the PRO
    edition (with expiration, ``license_type='commercial'``).

    Example response (OSS)::

        {
            "product_name": "Sonatype Nexus Repository",
            "edition": "OSS",
            "license_type": "open-source",
            "features": ["repository-management", "security", "search"],
            "expiration_date": null,
            "is_valid": true,
            "contact_email": null
        }

    Example response (PRO)::

        {
            "product_name": "Sonatype Nexus Repository",
            "edition": "PRO",
            "license_type": "commercial",
            "features": [
                "repository-management", "security", "search",
                "high-availability", "s3-blobstore", "saml-sso"
            ],
            "expiration_date": "2027-02-24T23:59:59Z",
            "is_valid": true,
            "contact_email": "support@sonatype.com"
        }
    """

    product_name = fields.String(
        dump_only=True,
        metadata={"description": "Product name ('Sonatype Nexus Repository')."},
    )

    edition = fields.String(
        dump_only=True,
        validate=OneOf(VALID_EDITIONS),
        metadata={"description": "Edition name ('OSS' or 'PRO')."},
    )

    license_type = fields.String(
        dump_only=True,
        validate=OneOf(VALID_LICENSE_TYPES),
        metadata={"description": "License type ('open-source' or 'commercial')."},
    )

    features = fields.List(
        fields.String(),
        dump_only=True,
        metadata={
            "description": "List of feature identifiers enabled by this license.",
        },
    )

    expiration_date = fields.DateTime(
        dump_only=True,
        allow_none=True,
        metadata={
            "description": (
                "License expiration date (ISO 8601). Null for OSS editions "
                "which do not expire."
            ),
        },
    )

    is_valid = fields.Boolean(
        dump_only=True,
        metadata={"description": "Whether the license is currently valid."},
    )

    contact_email = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={"description": "Support contact email address."},
    )


# ---------------------------------------------------------------------------
# Module-level convenience exports
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "SystemConfigSchema",
    "SystemConfigUpdateSchema",
    "SystemConfigBulkSchema",
    "SystemStatusSchema",
    "LicenseInfoSchema",
]
