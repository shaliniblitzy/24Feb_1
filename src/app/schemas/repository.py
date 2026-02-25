"""
Marshmallow serialization schemas for Repository CRUD operations.

This module defines Marshmallow 3.x schema classes that handle request
validation and response serialization for the Repository entity — the
central domain object of the Nexus Repository Manager system.

Schemas defined:
    - RepositoryCreateSchema: Validates repository creation requests
    - RepositoryUpdateSchema: Validates partial repository update requests
    - RepositoryResponseSchema: Serializes full repository detail responses
    - RepositoryListResponseSchema: Serializes lightweight repository list items
    - ProxyConfigSchema: Validates proxy repository configuration attributes
    - GroupConfigSchema: Validates group repository configuration attributes

Replaces Jackson 2.16.1 DTOs from the Java source system
(RepositoryManagerRESTAdapter). Supports Features F-101 (Multi-Format
Repository Support), F-102 (Repository Types), and F-501 (REST API).

Repository formats (F-101): maven2, npm, docker, nuget, pypi, apt, raw
Repository types (F-102): hosted, proxy, group
"""

from __future__ import annotations

import re
from typing import Any

from marshmallow import (
    EXCLUDE,
    Schema,
    ValidationError,
    fields,
    post_dump,
    pre_load,
    validate,
    validates,
    validates_schema,
)
from marshmallow.validate import Length, OneOf, Range, Regexp, URL

# ---------------------------------------------------------------------------
# Constants — Enumerations for repository formats and types
# ---------------------------------------------------------------------------

#: All supported repository formats per Feature F-101.
SUPPORTED_FORMATS: list[str] = [
    "maven2",
    "npm",
    "docker",
    "nuget",
    "pypi",
    "apt",
    "raw",
]

#: All supported repository types per Feature F-102.
SUPPORTED_TYPES: list[str] = [
    "hosted",
    "proxy",
    "group",
]

#: Human-readable labels for each repository format.
FORMAT_LABELS: dict[str, str] = {
    "maven2": "Maven2",
    "npm": "npm",
    "docker": "Docker",
    "nuget": "NuGet",
    "pypi": "PyPI",
    "apt": "APT",
    "raw": "Raw",
}

#: Regular expression pattern for valid repository names.
#: Must start with an alphanumeric character and may contain alphanumeric,
#: dots, hyphens, and underscores.
REPO_NAME_PATTERN: str = r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"


# ---------------------------------------------------------------------------
# Helper — URL validation for proxy remote URLs
# ---------------------------------------------------------------------------

def _validate_url(value: str) -> bool:
    """Return True if *value* is a valid HTTP/HTTPS URL.

    Uses a simple regex check rather than a heavyweight URL library so that
    the schema layer remains lightweight and dependency-free beyond
    Marshmallow itself.
    """
    url_pattern = re.compile(
        r"^https?://"  # scheme
        r"[^\s/$.?#]"  # at least one char after scheme
        r"[^\s]*$",    # rest of URL
        re.IGNORECASE,
    )
    return bool(url_pattern.match(value))


# ===========================================================================
# ProxyConfigSchema — Nested schema for proxy-type repository attributes
# ===========================================================================

class ProxyConfigSchema(Schema):
    """Schema for validating proxy repository configuration attributes.

    Proxy repositories cache artifacts from a remote upstream repository.
    This schema validates the ``proxy`` section of the ``attributes`` dict
    for proxy-type repositories.

    Fields:
        remote_url: The upstream repository URL (HTTP/HTTPS). Required.
        content_max_age: Maximum age (in minutes) of cached content before
            re-validation against the remote.  Defaults to 1440 (24 hours).
            A value of ``-1`` means infinite (never re-validate).
        metadata_max_age: Maximum age (in minutes) of cached metadata
            before re-validation.  Defaults to 1440 (24 hours).
    """

    class Meta:
        """Marshmallow Meta configuration."""

        unknown = EXCLUDE

    remote_url = fields.String(
        required=True,
        validate=[
            Length(min=1, max=2048),
            URL(
                relative=False,
                schemes={"http", "https"},
                error="Remote URL must be a valid HTTP or HTTPS URL.",
            ),
        ],
        metadata={
            "description": "Upstream repository URL (http:// or https://)",
            "example": "https://repo1.maven.org/maven2/",
        },
    )

    content_max_age = fields.Integer(
        required=False,
        load_default=1440,
        validate=Range(min=-1, error="content_max_age must be >= -1 (-1 = infinite)."),
        metadata={
            "description": "Maximum age in minutes for cached content. -1 for infinite.",
        },
    )

    metadata_max_age = fields.Integer(
        required=False,
        load_default=1440,
        validate=Range(min=-1, error="metadata_max_age must be >= -1 (-1 = infinite)."),
        metadata={
            "description": "Maximum age in minutes for cached metadata. -1 for infinite.",
        },
    )


# ===========================================================================
# GroupConfigSchema — Nested schema for group-type repository attributes
# ===========================================================================

class GroupConfigSchema(Schema):
    """Schema for validating group repository configuration attributes.

    Group repositories aggregate content from an ordered list of member
    repositories.  Requests are resolved by iterating members in order
    until a match is found.

    Fields:
        member_names: Ordered list of member repository names.  At least
            one member is required.
    """

    class Meta:
        """Marshmallow Meta configuration."""

        unknown = EXCLUDE

    member_names = fields.List(
        fields.String(
            required=True,
            validate=Length(min=1, max=200),
        ),
        required=True,
        validate=Length(min=1, error="Group repository must have at least one member."),
        metadata={
            "description": "Ordered list of member repository names.",
            "example": ["maven-releases", "maven-central"],
        },
    )


# ===========================================================================
# RepositoryAttributesSchema — Unified nested schema for type-specific attrs
# ===========================================================================

class RepositoryAttributesSchema(Schema):
    """Schema that provides nested validation of type-specific attributes.

    This schema uses ``fields.Nested`` to embed :class:`ProxyConfigSchema`
    and :class:`GroupConfigSchema` as optional sub-schemas.  It is used
    internally by the :func:`validate_attributes` helper when deeper
    validation of the ``attributes`` dictionary is desired.

    All sections are optional because only one section is relevant per
    repository type (proxy or group).
    """

    class Meta:
        """Marshmallow Meta configuration."""

        unknown = EXCLUDE

    proxy = fields.Nested(
        ProxyConfigSchema,
        required=False,
        allow_none=True,
        load_default=None,
        metadata={"description": "Proxy repository configuration."},
    )

    group = fields.Nested(
        GroupConfigSchema,
        required=False,
        allow_none=True,
        load_default=None,
        metadata={"description": "Group repository configuration."},
    )


# ===========================================================================
# RepositoryCreateSchema — Request validation for repository creation
# ===========================================================================

class RepositoryCreateSchema(Schema):
    """Schema for validating repository creation requests (POST).

    The Repository is the central domain entity of the Nexus Repository
    Manager.  Creating a repository requires a unique *name*, a *format*
    (one of the 7 supported package formats), a *type* (hosted, proxy, or
    group), and a reference to an existing *blob_store_name*.

    Cross-field validation ensures that:
    - Proxy repositories include a valid ``remoteUrl`` in ``attributes.proxy``
    - Group repositories include a non-empty ``memberNames`` list in
      ``attributes.group``
    """

    class Meta:
        """Marshmallow Meta configuration."""

        unknown = EXCLUDE

    # --- Required fields ---------------------------------------------------

    name = fields.String(
        required=True,
        validate=[
            Length(min=1, max=200),
            Regexp(
                REPO_NAME_PATTERN,
                error=(
                    "Repository name must start with an alphanumeric "
                    "character and contain only alphanumeric characters, "
                    "dots, hyphens, and underscores."
                ),
            ),
        ],
        metadata={
            "description": "Unique repository name (also serves as primary key).",
            "example": "maven-central",
        },
    )

    format = fields.String(
        required=True,
        validate=OneOf(
            SUPPORTED_FORMATS,
            error="Repository format must be one of: {choices}.",
        ),
        metadata={
            "description": "Package format for this repository (F-101).",
            "example": "maven2",
        },
    )

    type = fields.String(
        required=True,
        validate=OneOf(
            SUPPORTED_TYPES,
            error="Repository type must be one of: {choices}.",
        ),
        metadata={
            "description": "Repository type: hosted, proxy, or group (F-102).",
            "example": "proxy",
        },
    )

    blob_store_name = fields.String(
        required=True,
        validate=Length(min=1, max=200),
        metadata={
            "description": "Name of the BlobStore used for artifact storage.",
            "example": "default",
        },
    )

    # --- Optional fields ---------------------------------------------------

    online = fields.Boolean(
        required=False,
        load_default=True,
        metadata={
            "description": "Whether the repository starts in online mode.",
        },
    )

    attributes = fields.Dict(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Type-specific and format-specific configuration. "
                "Proxy repos require 'proxy.remoteUrl'; group repos "
                "require 'group.memberNames'."
            ),
        },
    )

    routing_rule = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        validate=Length(max=200),
        metadata={
            "description": "Optional routing rule name.",
        },
    )

    cleanup_policies = fields.List(
        fields.String(validate=Length(min=1, max=200)),
        required=False,
        load_default=[],
        metadata={
            "description": "List of cleanup policy names to apply.",
            "example": ["cleanup-snapshots"],
        },
    )

    # --- Pre-load hook -----------------------------------------------------

    @pre_load
    def _strip_name(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Strip leading/trailing whitespace from the repository name."""
        if "name" in data and isinstance(data["name"], str):
            data["name"] = data["name"].strip()
        return data

    # --- Field-level validation --------------------------------------------

    @validates("name")
    def _validate_name_not_reserved(self, value: str, **kwargs: Any) -> None:
        """Reject reserved repository names that conflict with system routes.

        Certain names are reserved for internal routing or convention and
        cannot be used as repository identifiers.
        """
        reserved_names: set[str] = {
            "api",
            "service",
            "static",
            "system",
            "internal",
            "admin",
            "health",
            "metrics",
        }
        if value.lower() in reserved_names:
            raise ValidationError(
                f"Repository name '{value}' is reserved and cannot be used."
            )

    # --- Cross-field validation --------------------------------------------

    @validates_schema
    def _validate_type_specific_attributes(
        self, data: dict[str, Any], **kwargs: Any
    ) -> None:
        """Validate that type-specific attributes are present and valid.

        Rules:
            - **proxy**: ``attributes.proxy.remoteUrl`` must be a valid
              HTTP/HTTPS URL.
            - **group**: ``attributes.group.memberNames`` must be a
              non-empty list of strings.
        """
        repo_type: str | None = data.get("type")
        attributes: dict[str, Any] | None = data.get("attributes")

        if repo_type == "proxy":
            self._validate_proxy_attributes(attributes)

        if repo_type == "group":
            self._validate_group_attributes(attributes)

    # --- Private helpers ---------------------------------------------------

    @staticmethod
    def _validate_proxy_attributes(attributes: dict[str, Any] | None) -> None:
        """Ensure proxy attributes contain a valid remoteUrl."""
        if not attributes or not isinstance(attributes.get("proxy"), dict):
            raise ValidationError(
                "Proxy repositories require 'attributes.proxy.remoteUrl'.",
                field_name="attributes",
            )

        proxy_config: dict[str, Any] = attributes["proxy"]
        remote_url: Any = proxy_config.get("remoteUrl")

        if not remote_url or not isinstance(remote_url, str):
            raise ValidationError(
                "Proxy repositories require a non-empty 'attributes.proxy.remoteUrl' string.",
                field_name="attributes",
            )

        if not _validate_url(remote_url):
            raise ValidationError(
                f"Invalid proxy remote URL '{remote_url}'. "
                "Must be a valid HTTP or HTTPS URL.",
                field_name="attributes",
            )

    @staticmethod
    def _validate_group_attributes(attributes: dict[str, Any] | None) -> None:
        """Ensure group attributes contain a non-empty memberNames list."""
        if not attributes or not isinstance(attributes.get("group"), dict):
            raise ValidationError(
                "Group repositories require 'attributes.group.memberNames'.",
                field_name="attributes",
            )

        group_config: dict[str, Any] = attributes["group"]
        member_names: Any = group_config.get("memberNames")

        if not isinstance(member_names, list) or len(member_names) == 0:
            raise ValidationError(
                "Group repositories require a non-empty list of member "
                "repository names in 'attributes.group.memberNames'.",
                field_name="attributes",
            )

        # Validate each member name is a non-empty string
        for idx, member in enumerate(member_names):
            if not isinstance(member, str) or not member.strip():
                raise ValidationError(
                    f"Member name at index {idx} must be a non-empty string.",
                    field_name="attributes",
                )


# ===========================================================================
# RepositoryUpdateSchema — Request validation for repository updates
# ===========================================================================

class RepositoryUpdateSchema(Schema):
    """Schema for validating repository update requests (PUT/PATCH).

    Only mutable fields are included.  ``name``, ``format``, and ``type``
    are **immutable** after creation and therefore excluded from this
    schema.  All fields are optional to support partial updates.
    """

    class Meta:
        """Marshmallow Meta configuration."""

        unknown = EXCLUDE

    online = fields.Boolean(
        required=False,
        metadata={
            "description": "Toggle repository online/offline status.",
        },
    )

    blob_store_name = fields.String(
        required=False,
        validate=Length(min=1, max=200),
        metadata={
            "description": "Change the BlobStore reference.",
        },
    )

    attributes = fields.Dict(
        required=False,
        allow_none=True,
        metadata={
            "description": "Updated type-specific and format-specific configuration.",
        },
    )

    routing_rule = fields.String(
        required=False,
        allow_none=True,
        validate=Length(max=200),
        metadata={
            "description": "Updated routing rule name (set to null to remove).",
        },
    )

    cleanup_policies = fields.List(
        fields.String(validate=Length(min=1, max=200)),
        required=False,
        metadata={
            "description": "Updated list of cleanup policy names.",
        },
    )

    # --- Cross-field validation --------------------------------------------

    @validates_schema
    def _validate_conditional_attributes(
        self, data: dict[str, Any], **kwargs: Any
    ) -> None:
        """Validate type-specific attributes when present in the update payload.

        If the caller includes ``attributes`` in the update, this method
        checks that any proxy or group configuration sections are well-formed.
        """
        attributes: dict[str, Any] | None = data.get("attributes")
        if not attributes or not isinstance(attributes, dict):
            return

        # Validate proxy.remoteUrl if present
        proxy_section: Any = attributes.get("proxy")
        if isinstance(proxy_section, dict):
            remote_url: Any = proxy_section.get("remoteUrl")
            if remote_url is not None:
                if not isinstance(remote_url, str) or not remote_url.strip():
                    raise ValidationError(
                        "'attributes.proxy.remoteUrl' must be a non-empty string.",
                        field_name="attributes",
                    )
                if not _validate_url(remote_url):
                    raise ValidationError(
                        f"Invalid proxy remote URL '{remote_url}'. "
                        "Must be a valid HTTP or HTTPS URL.",
                        field_name="attributes",
                    )

        # Validate group.memberNames if present
        group_section: Any = attributes.get("group")
        if isinstance(group_section, dict):
            member_names: Any = group_section.get("memberNames")
            if member_names is not None:
                if not isinstance(member_names, list) or len(member_names) == 0:
                    raise ValidationError(
                        "'attributes.group.memberNames' must be a non-empty list of strings.",
                        field_name="attributes",
                    )
                for idx, member in enumerate(member_names):
                    if not isinstance(member, str) or not member.strip():
                        raise ValidationError(
                            f"Member name at index {idx} must be a non-empty string.",
                            field_name="attributes",
                        )


# ===========================================================================
# RepositoryResponseSchema — API response serialization
# ===========================================================================

class RepositoryResponseSchema(Schema):
    """Schema for serializing full repository detail responses (GET).

    All fields are ``dump_only`` since this schema is exclusively used
    for serialization (not deserialization).  Computed fields such as
    ``component_count``, ``asset_count``, ``status``, ``url``, and
    ``format_label`` are derived during the ``@post_dump`` phase.
    """

    class Meta:
        """Marshmallow Meta configuration."""

        unknown = EXCLUDE

    # --- Core fields -------------------------------------------------------

    name = fields.String(
        dump_only=True,
        metadata={"description": "Unique repository name (primary key)."},
    )

    format = fields.String(
        dump_only=True,
        metadata={"description": "Package format (F-101)."},
    )

    type = fields.String(
        dump_only=True,
        metadata={"description": "Repository type: hosted, proxy, or group."},
    )

    blob_store_name = fields.String(
        dump_only=True,
        metadata={"description": "BlobStore reference name."},
    )

    online = fields.Boolean(
        dump_only=True,
        metadata={"description": "Whether the repository is currently online."},
    )

    attributes = fields.Dict(
        dump_only=True,
        allow_none=True,
        metadata={"description": "Type-specific and format-specific configuration."},
    )

    routing_rule = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={"description": "Applied routing rule name."},
    )

    cleanup_policies = fields.List(
        fields.String(),
        dump_only=True,
        metadata={"description": "Applied cleanup policy names."},
    )

    # --- Computed / aggregate fields ---------------------------------------

    component_count = fields.Integer(
        dump_only=True,
        load_default=0,
        metadata={"description": "Total number of components in this repository."},
    )

    asset_count = fields.Integer(
        dump_only=True,
        load_default=0,
        metadata={"description": "Total number of assets in this repository."},
    )

    status = fields.Dict(
        dump_only=True,
        metadata={
            "description": "Repository health/status information.",
            "example": {"online": True, "description": "Ready to receive requests"},
        },
    )

    # --- Timestamps --------------------------------------------------------

    created_at = fields.DateTime(
        dump_only=True,
        format="iso",
        metadata={"description": "Creation timestamp (ISO 8601)."},
    )

    updated_at = fields.DateTime(
        dump_only=True,
        format="iso",
        metadata={"description": "Last update timestamp (ISO 8601)."},
    )

    # --- Post-dump enrichment ----------------------------------------------

    @post_dump
    def _enrich_response(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Add computed fields to the serialized output.

        Computed fields:
            url: The repository content URL (``/repository/{name}``).
            format_label: Human-readable format label.
            status: Default status dict if not already set.
        """
        # Compute repository content URL
        name: str | None = data.get("name")
        if name:
            data["url"] = f"/repository/{name}"
        else:
            data["url"] = None

        # Compute human-readable format label
        repo_format: str | None = data.get("format")
        if repo_format:
            data["format_label"] = FORMAT_LABELS.get(repo_format, repo_format)
        else:
            data["format_label"] = None

        # Provide default status dict when the source object didn't set one
        if "status" not in data or data.get("status") is None:
            is_online: bool = data.get("online", False)
            if is_online:
                data["status"] = {
                    "online": True,
                    "description": "Ready to receive requests",
                }
            else:
                data["status"] = {
                    "online": False,
                    "description": "Repository is offline",
                }

        return data


# ===========================================================================
# RepositoryListResponseSchema — Lightweight list serialization
# ===========================================================================

class RepositoryListResponseSchema(Schema):
    """Schema for serializing repository list items.

    A lightweight schema used by list endpoints where full detail is not
    required.  Includes the computed ``url`` field for convenience.
    """

    class Meta:
        """Marshmallow Meta configuration."""

        unknown = EXCLUDE

    name = fields.String(
        metadata={"description": "Repository name."},
    )

    format = fields.String(
        metadata={"description": "Package format."},
    )

    type = fields.String(
        metadata={"description": "Repository type."},
    )

    online = fields.Boolean(
        metadata={"description": "Whether the repository is online."},
    )

    url = fields.String(
        dump_only=True,
        metadata={"description": "Computed repository content URL."},
    )

    @post_dump
    def _compute_url(self, data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """Compute the ``url`` field from the repository name."""
        name: str | None = data.get("name")
        if name and not data.get("url"):
            data["url"] = f"/repository/{name}"
        return data


# ---------------------------------------------------------------------------
# Module-level __all__ for explicit public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "RepositoryCreateSchema",
    "RepositoryUpdateSchema",
    "RepositoryResponseSchema",
    "RepositoryListResponseSchema",
    "ProxyConfigSchema",
    "GroupConfigSchema",
    "RepositoryAttributesSchema",
    "SUPPORTED_FORMATS",
    "SUPPORTED_TYPES",
    "FORMAT_LABELS",
]
