"""
Marshmallow serialization schemas for Component metadata.

This module defines Marshmallow 3.x schema classes for component serialization
and deserialization, supporting the Component management API (F-501-RQ-002).
These schemas mirror the Component SQLAlchemy model from
``src.app.models.component`` and replace component-related Jackson 2.16.1 DTOs
from the Java source system (Sonatype Nexus Repository).

Components represent logical packages/artifacts within a repository.  Coordinate
fields (``namespace``, ``name``, ``version``) vary by format:

- **Maven**: namespace=groupId, name=artifactId, version=version
- **npm**: namespace=@scope, name=package, version=version
- **Docker**: namespace=library, name=image, version=tag
- **NuGet**: namespace=null, name=packageId, version=version
- **PyPI**: namespace=null, name=project, version=version
- **APT**: namespace=null, name=package, version=version
- **Raw**: namespace=null, name=filename, version=null

Schemas provided:

- ``ComponentSchema``: Base component serialization with coordinate fields
- ``ComponentDetailSchema``: Extended schema with nested assets and format info
- ``ComponentSearchResultSchema``: Optimized for Elasticsearch search results
- ``SearchResultAssetBriefSchema``: Compact asset info for search results
- ``ComponentUploadSchema``: Validates component upload request metadata
- ``ComponentListQuerySchema``: Validates query params for listing/filtering

Features supported:

- **F-101**: Multi-Format Repository Support (all 7 format coordinate patterns)
- **F-103**: Content Indexing and Search (search result schemas with score)
- **F-501-RQ-002**: Component management REST API serialization
"""

from marshmallow import (
    Schema,
    fields,
    validate,
    validates,
    ValidationError,
    pre_load,
    post_dump,
)
from marshmallow.validate import Length, Range, OneOf


# ---------------------------------------------------------------------------
# Constants for validation
# ---------------------------------------------------------------------------

# Maximum length for repository name (matches Repository model PK length)
MAX_REPOSITORY_NAME_LENGTH: int = 200

# Maximum length for component namespace (matches Component model column)
MAX_NAMESPACE_LENGTH: int = 512

# Maximum length for component name (matches Component model column)
MAX_NAME_LENGTH: int = 512

# Maximum length for version string (matches Component model column)
MAX_VERSION_LENGTH: int = 256

# Supported repository formats (F-101) — must stay in sync with
# src.app.models.repository.VALID_FORMATS
VALID_FORMATS: tuple[str, ...] = (
    "maven2",
    "npm",
    "docker",
    "nuget",
    "pypi",
    "apt",
    "raw",
)

# Pagination defaults and limits
DEFAULT_PAGE: int = 1
DEFAULT_PAGE_SIZE: int = 50
MAX_PAGE_SIZE: int = 200


# ===========================================================================
# ComponentSchema — Base Component Serialization
# ===========================================================================


class ComponentSchema(Schema):
    """Marshmallow schema for full component serialization and deserialization.

    This schema handles both loading (deserialization from JSON/dict) and
    dumping (serialization to JSON/dict) of component data.  It mirrors the
    ``Component`` SQLAlchemy model with complete field definitions and
    validation constraints.

    Fields:
        id:               Auto-incremented primary key (read-only).
        repository_name:  Foreign key to the parent repository (required).
        namespace:        Format-specific namespace (nullable).
        name:             Component name — always required, never null.
        version:          Version string (nullable for some formats).
        attributes:       Format-specific extensible JSON metadata (nullable).
        created_at:       Creation timestamp, ISO 8601 (read-only).
        updated_at:       Last update timestamp, ISO 8601 (read-only).

    A ``@post_dump`` hook computes a ``coordinates`` string from the
    namespace, name, and version fields, following the GAV-like format
    ``{namespace}:{name}:{version}`` (omitting null parts).
    """

    # -----------------------------------------------------------------------
    # Primary key — auto-incremented, read-only
    # -----------------------------------------------------------------------
    id = fields.Integer(
        dump_only=True,
        metadata={
            "description": "Auto-incremented unique component identifier.",
            "example": 17,
        },
    )

    # -----------------------------------------------------------------------
    # Foreign key to repository — required on load
    # -----------------------------------------------------------------------
    repository_name = fields.String(
        required=True,
        validate=Length(min=1, max=MAX_REPOSITORY_NAME_LENGTH),
        metadata={
            "description": "Name of the repository this component belongs to.",
            "example": "maven-central",
        },
    )

    # -----------------------------------------------------------------------
    # Format-specific namespace (nullable)
    # -----------------------------------------------------------------------
    namespace = fields.String(
        allow_none=True,
        load_default=None,
        validate=Length(max=MAX_NAMESPACE_LENGTH),
        metadata={
            "description": (
                "Format-specific namespace for grouping components. "
                "Maven: groupId (e.g., 'org.apache.commons'); "
                "npm: scope (e.g., '@angular'); "
                "Docker: registry namespace (e.g., 'library'); "
                "NuGet/PyPI/APT/Raw: typically null."
            ),
            "example": "org.apache.commons",
        },
    )

    # -----------------------------------------------------------------------
    # Component name — required, never null
    # -----------------------------------------------------------------------
    name = fields.String(
        required=True,
        validate=Length(min=1, max=MAX_NAME_LENGTH),
        metadata={
            "description": (
                "Component name — the core identifier within its namespace. "
                "Maven: artifactId; npm: package name; Docker: image name; "
                "NuGet: package ID; PyPI: project name; APT: package name; "
                "Raw: filename."
            ),
            "example": "commons-lang3",
        },
    )

    # -----------------------------------------------------------------------
    # Version string (nullable)
    # -----------------------------------------------------------------------
    version = fields.String(
        allow_none=True,
        load_default=None,
        validate=Length(max=MAX_VERSION_LENGTH),
        metadata={
            "description": (
                "Version string for this component. Can be null for some "
                "format types (e.g., Raw format files, Docker digest-only "
                "references)."
            ),
            "example": "3.14.0",
        },
    )

    # -----------------------------------------------------------------------
    # Format-specific extensible metadata
    # -----------------------------------------------------------------------
    attributes = fields.Dict(
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Format-specific extensible metadata stored as a JSON object. "
                "Contents vary by repository format: Maven POM info, npm "
                "package.json fields, Docker manifest digest, NuGet package "
                "metadata, PyPI project metadata, etc."
            ),
        },
    )

    # -----------------------------------------------------------------------
    # Audit timestamps — read-only
    # -----------------------------------------------------------------------
    created_at = fields.DateTime(
        dump_only=True,
        metadata={
            "description": "Timestamp when the component was first created. ISO 8601.",
        },
    )

    updated_at = fields.DateTime(
        dump_only=True,
        metadata={
            "description": "Timestamp of the most recent update. ISO 8601.",
        },
    )

    # -----------------------------------------------------------------------
    # Coordinate computation hook
    # -----------------------------------------------------------------------

    @post_dump
    def compute_coordinates(self, data: dict, **kwargs) -> dict:
        """Compute a GAV-like ``coordinates`` string from namespace, name, version.

        The coordinates string follows the pattern
        ``{namespace}:{name}:{version}``, omitting segments whose value is
        ``None`` or missing.  This provides a convenient human-readable
        identifier for the component.

        Examples:
            - Maven: ``org.apache.commons:commons-lang3:3.14.0``
            - npm scoped: ``@angular:core:18.2.0``
            - Docker: ``library:nginx:latest``
            - PyPI (no namespace): ``flask:3.1.3``
            - Raw (no namespace, no version): ``config.yaml``

        Args:
            data: The serialized output dictionary.

        Returns:
            The enriched output dictionary with the ``coordinates`` field.
        """
        parts: list[str] = []

        namespace_val = data.get("namespace")
        name_val = data.get("name")
        version_val = data.get("version")

        if namespace_val:
            parts.append(namespace_val)
        if name_val:
            parts.append(name_val)
        if version_val:
            parts.append(version_val)

        if parts:
            data["coordinates"] = ":".join(parts)

        return data

    class Meta:
        """Schema meta-configuration."""

        ordered = True


# ===========================================================================
# ComponentDetailSchema — Extended Component with Assets
# ===========================================================================


class ComponentDetailSchema(ComponentSchema):
    """Extended component schema with nested asset details.

    Inherits all fields from :class:`ComponentSchema` and adds:

    - ``assets``: Nested list of :class:`AssetResponseSchema` (from
      ``src.app.schemas.asset``) providing full asset details.
    - ``asset_count``: Integer count of associated assets.
    - ``format``: Repository format string derived from the parent repository
      relationship (not stored on the component itself).

    Used when retrieving full component details via
    ``GET /api/v1/components/{id}``.

    Note:
        The ``assets`` field uses a string reference
        ``'AssetResponseSchema'`` to leverage Marshmallow's class registry
        for lazy resolution, avoiding circular import issues between
        component and asset schema modules.
    """

    # -----------------------------------------------------------------------
    # Nested asset list — resolved via Marshmallow class registry
    # -----------------------------------------------------------------------
    assets = fields.List(
        fields.Nested("AssetResponseSchema"),
        dump_only=True,
        load_default=[],
        metadata={
            "description": (
                "List of assets (physical files) associated with this "
                "component. Each entry follows the AssetResponseSchema format."
            ),
        },
    )

    # -----------------------------------------------------------------------
    # Asset count — convenience field for UI display
    # -----------------------------------------------------------------------
    asset_count = fields.Integer(
        dump_only=True,
        load_default=0,
        metadata={
            "description": "Total number of assets belonging to this component.",
            "example": 3,
        },
    )

    # -----------------------------------------------------------------------
    # Repository format — derived from the repository relationship
    # -----------------------------------------------------------------------
    format = fields.String(
        dump_only=True,
        metadata={
            "description": (
                "Repository format type (e.g., 'maven2', 'npm', 'docker'). "
                "Derived from the parent repository's format column, not "
                "stored on the component itself."
            ),
            "example": "maven2",
        },
    )


# ===========================================================================
# SearchResultAssetBriefSchema — Compact Asset Info for Search Results
# ===========================================================================


class SearchResultAssetBriefSchema(Schema):
    """Compact asset representation for inclusion in search result listings.

    This schema provides a minimal subset of asset information optimized
    for search result display.  It includes only the fields needed to
    identify and access the asset, without the full metadata present in
    :class:`AssetResponseSchema`.

    Fields:
        path:          Asset path within the repository.
        content_type:  MIME content type (nullable).
        size:          File size in bytes (nullable).
        download_url:  Computed download URL (read-only).
    """

    path = fields.String(
        metadata={
            "description": "Hierarchical path of the asset within its repository.",
            "example": "/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar",
        },
    )

    content_type = fields.String(
        allow_none=True,
        metadata={
            "description": "MIME content type of the asset.",
            "example": "application/java-archive",
        },
    )

    size = fields.Integer(
        allow_none=True,
        metadata={
            "description": "File size in bytes.",
            "example": 1048576,
        },
    )

    download_url = fields.String(
        dump_only=True,
        metadata={
            "description": (
                "Computed artifact download URL. "
                "Format: /repository/{repository_name}{path}"
            ),
            "example": "/repository/maven-central/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar",
        },
    )

    class Meta:
        """Schema meta-configuration."""

        ordered = True


# ===========================================================================
# ComponentSearchResultSchema — Search Result Representation
# ===========================================================================


class ComponentSearchResultSchema(Schema):
    """Component representation optimized for Elasticsearch search results.

    This schema is designed for the search API (F-103) where less detail
    is needed compared to :class:`ComponentDetailSchema`.  It includes a
    ``score`` field for Elasticsearch relevance ranking and uses
    :class:`SearchResultAssetBriefSchema` for compact asset listings.

    Fields:
        id:               Component ID.
        repository_name:  Repository name.
        format:           Repository format (derived).
        namespace:        Component namespace (nullable).
        name:             Component name.
        version:          Version string (nullable).
        assets:           Compact asset list for search results (read-only).
        score:            Elasticsearch relevance score (read-only).
    """

    id = fields.Integer(
        metadata={
            "description": "Unique component identifier.",
            "example": 42,
        },
    )

    repository_name = fields.String(
        metadata={
            "description": "Name of the repository this component belongs to.",
            "example": "maven-central",
        },
    )

    format = fields.String(
        metadata={
            "description": (
                "Repository format type (e.g., 'maven2', 'npm', 'docker'). "
                "Derived from the parent repository."
            ),
            "example": "maven2",
        },
    )

    namespace = fields.String(
        allow_none=True,
        metadata={
            "description": "Format-specific namespace (nullable).",
            "example": "org.apache.commons",
        },
    )

    name = fields.String(
        metadata={
            "description": "Component name.",
            "example": "commons-lang3",
        },
    )

    version = fields.String(
        allow_none=True,
        metadata={
            "description": "Version string (nullable).",
            "example": "3.14.0",
        },
    )

    assets = fields.List(
        fields.Nested("SearchResultAssetBriefSchema"),
        dump_only=True,
        load_default=[],
        metadata={
            "description": (
                "Simplified asset list for search result display. "
                "Uses SearchResultAssetBriefSchema for compact representation."
            ),
        },
    )

    score = fields.Float(
        dump_only=True,
        metadata={
            "description": (
                "Elasticsearch relevance score for this search result. "
                "Higher values indicate better match quality."
            ),
            "example": 12.45,
        },
    )

    class Meta:
        """Schema meta-configuration."""

        ordered = True


# ===========================================================================
# ComponentUploadSchema — Upload Request Validation
# ===========================================================================


class ComponentUploadSchema(Schema):
    """Marshmallow schema for component upload request metadata validation.

    Validates the JSON metadata portion of component upload requests.  The
    actual binary asset content is transmitted as multipart form data and
    is handled separately by the upload manager service.

    Used by ``UploadManagerImpl`` (``src.app.services.upload_manager``) for
    artifact uploads.

    Fields:
        repository_name:  Target repository for the upload (required).
        namespace:        Component namespace (optional, nullable).
        name:             Component name (required).
        version:          Component version (optional, nullable).
        attributes:       Additional format-specific metadata (optional).
    """

    repository_name = fields.String(
        required=True,
        validate=Length(min=1, max=MAX_REPOSITORY_NAME_LENGTH),
        metadata={
            "description": "Target repository name for the component upload.",
            "example": "maven-releases",
        },
    )

    namespace = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        validate=Length(max=MAX_NAMESPACE_LENGTH),
        metadata={
            "description": (
                "Component namespace (format-specific). "
                "Maven: groupId; npm: scope; Docker: namespace."
            ),
            "example": "org.apache.commons",
        },
    )

    name = fields.String(
        required=True,
        validate=Length(min=1, max=MAX_NAME_LENGTH),
        metadata={
            "description": "Component name for the uploaded artifact.",
            "example": "commons-lang3",
        },
    )

    version = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        validate=Length(max=MAX_VERSION_LENGTH),
        metadata={
            "description": "Component version string (optional for some formats).",
            "example": "3.14.0",
        },
    )

    attributes = fields.Dict(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            "description": (
                "Optional format-specific metadata for the uploaded component. "
                "Maven: {packaging: 'jar', classifier: 'sources'}; "
                "npm: {dist-tags: {latest: '1.0.0'}}."
            ),
        },
    )

    @validates("repository_name")
    def validate_repository_name(self, value: str) -> None:
        """Ensure repository name contains only valid characters.

        Repository names should not contain path separators or special
        characters that could cause issues with URL routing.

        Args:
            value: The repository name to validate.

        Raises:
            ValidationError: If the name contains forbidden characters.
        """
        forbidden_chars = set("/\\?#[]@!$&'()*+,;=")
        found = set(value) & forbidden_chars
        if found:
            raise ValidationError(
                f"Repository name contains forbidden characters: "
                f"{', '.join(sorted(found))}. "
                f"Only alphanumeric characters, hyphens, underscores, and "
                f"dots are allowed."
            )

    @validates("name")
    def validate_component_name(self, value: str) -> None:
        """Ensure component name is not empty or whitespace-only.

        Args:
            value: The component name to validate.

        Raises:
            ValidationError: If the name is empty or whitespace-only.
        """
        if not value.strip():
            raise ValidationError(
                "Component name must not be empty or consist solely of "
                "whitespace characters."
            )

    class Meta:
        """Schema meta-configuration."""

        ordered = True


# ===========================================================================
# ComponentListQuerySchema — Listing/Filtering Query Parameters
# ===========================================================================


class ComponentListQuerySchema(Schema):
    """Marshmallow schema for component listing query parameter validation.

    Validates query parameters for the ``GET /api/v1/components`` endpoint.
    All fields are optional to support flexible filtering and pagination.

    Supported filters:
        - repository: Filter by repository name
        - format: Filter by repository format (OneOf 7 supported formats)
        - namespace: Filter by component namespace
        - name: Filter by component name (supports partial match)
        - version: Filter by component version

    Pagination:
        - page: Page number (1-based, default 1)
        - page_size: Results per page (1–200, default 50)
    """

    repository = fields.String(
        required=False,
        load_default=None,
        metadata={
            "description": "Filter components by repository name.",
            "example": "maven-central",
        },
    )

    format = fields.String(
        required=False,
        load_default=None,
        validate=OneOf(
            VALID_FORMATS,
            error="Invalid format. Must be one of: {choices}.",
        ),
        metadata={
            "description": (
                "Filter components by repository format. "
                "Valid values: maven2, npm, docker, nuget, pypi, apt, raw."
            ),
            "example": "maven2",
        },
    )

    namespace = fields.String(
        required=False,
        load_default=None,
        metadata={
            "description": "Filter components by namespace (exact or prefix match).",
            "example": "org.apache.commons",
        },
    )

    name = fields.String(
        required=False,
        load_default=None,
        metadata={
            "description": "Filter components by name (supports partial match).",
            "example": "commons-lang",
        },
    )

    version = fields.String(
        required=False,
        load_default=None,
        metadata={
            "description": "Filter components by version string.",
            "example": "3.14.0",
        },
    )

    page = fields.Integer(
        required=False,
        load_default=DEFAULT_PAGE,
        validate=Range(min=1),
        metadata={
            "description": "Page number for pagination (1-based).",
            "example": 1,
        },
    )

    page_size = fields.Integer(
        required=False,
        load_default=DEFAULT_PAGE_SIZE,
        validate=Range(min=1, max=MAX_PAGE_SIZE),
        metadata={
            "description": f"Number of results per page (1–{MAX_PAGE_SIZE}).",
            "example": 50,
        },
    )

    class Meta:
        """Schema meta-configuration."""

        ordered = True


# ---------------------------------------------------------------------------
# Module-level exports
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "ComponentSchema",
    "ComponentDetailSchema",
    "ComponentSearchResultSchema",
    "SearchResultAssetBriefSchema",
    "ComponentUploadSchema",
    "ComponentListQuerySchema",
]
