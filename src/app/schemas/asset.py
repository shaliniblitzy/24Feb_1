"""
Marshmallow serialization schemas for Asset metadata.

This module defines Marshmallow 3.x schema classes for asset serialization
and deserialization, supporting the Asset management API (F-501-RQ-002).
These schemas mirror the Asset SQLAlchemy model from src.app.models.asset
and replace the Jackson 2.16.1 asset-related DTOs from the Java source system.

Schemas provided:
- AssetSchema: Full asset serialization for internal use (load and dump)
- AssetResponseSchema: API response serialization with computed download_url
- AssetUploadSchema: Validates metadata portion of asset upload requests
- AssetListQuerySchema: Validates query parameters for listing/filtering assets

Features supported:
- F-101: Multi-Format Repository Support (all format-specific asset metadata)
- F-103: Content Indexing and Search (asset search and browse)
- F-204: Cleanup Policies (last_downloaded tracking for cleanup evaluation)
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
from marshmallow.validate import Length, Range, Regexp


# ---------------------------------------------------------------------------
# Constants for validation patterns
# ---------------------------------------------------------------------------

# SHA-1 hex string pattern: exactly 40 lowercase hex characters
SHA1_PATTERN = r'^[0-9a-f]{40}$'

# SHA-256 hex string pattern: exactly 64 lowercase hex characters
SHA256_PATTERN = r'^[0-9a-f]{64}$'

# MD5 hex string pattern: exactly 32 lowercase hex characters
MD5_PATTERN = r'^[0-9a-f]{32}$'

# Maximum path length for deeply nested Maven coordinate paths
MAX_PATH_LENGTH = 2048

# Maximum repository name length
MAX_REPOSITORY_NAME_LENGTH = 200

# Maximum content type length
MAX_CONTENT_TYPE_LENGTH = 255

# Maximum blob reference length
MAX_BLOB_REF_LENGTH = 500

# Pagination defaults and limits
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class AssetSchema(Schema):
    """Marshmallow schema for full asset serialization and deserialization.

    This schema handles both loading (deserialization from JSON/dict) and
    dumping (serialization to JSON/dict) of asset data. It mirrors the
    Asset SQLAlchemy model with complete field definitions and validation
    constraints.

    Fields include:
    - Identification: id, component_id, repository_name
    - Content metadata: path, content_type, size
    - Integrity checksums: checksum_sha1, checksum_sha256, checksum_md5
    - Lifecycle tracking: last_downloaded, created_at, updated_at
    - Storage reference: blob_ref (internal, dump_only)
    - Extensibility: attributes (format-specific JSON metadata)

    All checksum fields validate against strict hex string patterns.
    DateTime fields serialize as ISO 8601 format strings.
    """

    # -----------------------------------------------------------------------
    # Primary key — auto-incremented, read-only
    # -----------------------------------------------------------------------
    id = fields.Integer(
        dump_only=True,
        metadata={
            'description': 'Auto-incremented unique asset identifier.',
            'example': 42,
        },
    )

    # -----------------------------------------------------------------------
    # Foreign key to parent component (nullable for metadata files)
    # -----------------------------------------------------------------------
    component_id = fields.Integer(
        allow_none=True,
        load_default=None,
        metadata={
            'description': (
                'Foreign key reference to the parent Component. '
                'Null for standalone metadata files (e.g., maven-metadata.xml).'
            ),
            'example': 17,
        },
    )

    # -----------------------------------------------------------------------
    # Foreign key to repository — required on load
    # -----------------------------------------------------------------------
    repository_name = fields.String(
        required=True,
        validate=Length(min=1, max=MAX_REPOSITORY_NAME_LENGTH),
        metadata={
            'description': 'Name of the repository this asset belongs to.',
            'example': 'maven-central',
        },
    )

    # -----------------------------------------------------------------------
    # Storage path within the repository — required on load
    # -----------------------------------------------------------------------
    path = fields.String(
        required=True,
        validate=Length(min=1, max=MAX_PATH_LENGTH),
        metadata={
            'description': (
                'Hierarchical path of the asset within its repository. '
                'Must start with "/" for consistency.'
            ),
            'example': '/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar',
        },
    )

    # -----------------------------------------------------------------------
    # MIME content type
    # -----------------------------------------------------------------------
    content_type = fields.String(
        allow_none=True,
        load_default=None,
        validate=Length(max=MAX_CONTENT_TYPE_LENGTH),
        metadata={
            'description': 'MIME content type of the asset.',
            'example': 'application/java-archive',
        },
    )

    # -----------------------------------------------------------------------
    # Integrity checksums — SHA-1, SHA-256, MD5
    # -----------------------------------------------------------------------
    checksum_sha1 = fields.String(
        allow_none=True,
        load_default=None,
        validate=Regexp(SHA1_PATTERN, error='SHA-1 checksum must be exactly 40 lowercase hex characters.'),
        metadata={
            'description': 'SHA-1 hash of the asset content as a 40-character hex string.',
            'example': 'da39a3ee5e6b4b0d3255bfef95601890afd80709',
        },
    )

    checksum_sha256 = fields.String(
        allow_none=True,
        load_default=None,
        validate=Regexp(SHA256_PATTERN, error='SHA-256 checksum must be exactly 64 lowercase hex characters.'),
        metadata={
            'description': 'SHA-256 hash of the asset content as a 64-character hex string.',
            'example': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
        },
    )

    checksum_md5 = fields.String(
        allow_none=True,
        load_default=None,
        validate=Regexp(MD5_PATTERN, error='MD5 checksum must be exactly 32 lowercase hex characters.'),
        metadata={
            'description': 'MD5 hash of the asset content as a 32-character hex string.',
            'example': 'd41d8cd98f00b204e9800998ecf8427e',
        },
    )

    # -----------------------------------------------------------------------
    # File size in bytes (supports multi-GB files via Python native int)
    # -----------------------------------------------------------------------
    size = fields.Integer(
        allow_none=True,
        load_default=None,
        validate=Range(min=0),
        metadata={
            'description': 'File size in bytes. Supports large files (multi-GB).',
            'example': 1048576,
        },
    )

    # -----------------------------------------------------------------------
    # Last download timestamp — critical for cleanup policy evaluation (F-204)
    # -----------------------------------------------------------------------
    last_downloaded = fields.DateTime(
        allow_none=True,
        load_default=None,
        metadata={
            'description': (
                'Timestamp of the last time this asset was downloaded. '
                'Used by cleanup policies (F-204) for determining stale assets. '
                'Serialized as ISO 8601.'
            ),
        },
    )

    # -----------------------------------------------------------------------
    # BlobStore reference — internal storage pointer, read-only
    # -----------------------------------------------------------------------
    blob_ref = fields.String(
        allow_none=True,
        dump_only=True,
        validate=Length(max=MAX_BLOB_REF_LENGTH),
        metadata={
            'description': (
                'Internal BlobStore reference for the underlying storage object. '
                'This is an opaque storage pointer not exposed in public API responses.'
            ),
        },
    )

    # -----------------------------------------------------------------------
    # Format-specific extensible metadata
    # -----------------------------------------------------------------------
    attributes = fields.Dict(
        allow_none=True,
        load_default=None,
        metadata={
            'description': (
                'Format-specific extensible metadata stored as a JSON object. '
                'Contents vary by repository format (Maven POM info, npm package.json '
                'fields, Docker manifest digest, etc.).'
            ),
        },
    )

    # -----------------------------------------------------------------------
    # Audit timestamps — read-only
    # -----------------------------------------------------------------------
    created_at = fields.DateTime(
        dump_only=True,
        metadata={'description': 'Timestamp when the asset was first created. ISO 8601.'},
    )

    updated_at = fields.DateTime(
        dump_only=True,
        metadata={'description': 'Timestamp of the most recent update. ISO 8601.'},
    )

    # -------------------------------------------------------------------
    # Field-level validators
    # -------------------------------------------------------------------

    @validates('path')
    def validate_path(self, value: str) -> None:
        """Ensure the asset path starts with '/' for consistency.

        All asset paths within a repository should use an absolute-style
        leading slash, matching the convention used by Maven, npm, Docker,
        and other repository format handlers.

        Args:
            value: The path string to validate.

        Raises:
            ValidationError: If the path does not start with '/'.
        """
        if value and not value.startswith('/'):
            raise ValidationError(
                "Asset path must start with '/' for consistency "
                "(e.g., '/org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar')."
            )

    @pre_load
    def normalize_checksums(self, data: dict, **kwargs) -> dict:
        """Normalize checksum values to lowercase before validation.

        Checksums may arrive in mixed case from clients; normalizing to
        lowercase ensures consistent storage and comparison.

        Args:
            data: The raw input dictionary.

        Returns:
            The input dictionary with lowercase checksum values.
        """
        for checksum_field in ('checksum_sha1', 'checksum_sha256', 'checksum_md5'):
            if checksum_field in data and isinstance(data[checksum_field], str):
                data[checksum_field] = data[checksum_field].lower()
        return data

    class Meta:
        """Schema meta-configuration."""


class AssetResponseSchema(Schema):
    """Marshmallow schema for asset API response serialization.

    This schema is specifically tuned for REST API responses. All fields are
    ``dump_only`` since responses are read-only. It includes a computed
    ``download_url`` field that provides a direct artifact download link
    derived from ``repository_name`` and ``path``.

    The ``blob_ref`` field from ``AssetSchema`` is intentionally excluded
    from responses as it is an internal storage reference.

    A ``@post_dump`` hook computes the ``download_url`` and optionally
    formats the file size as a human-readable string.
    """

    # -----------------------------------------------------------------------
    # All fields are dump_only for response serialization
    # -----------------------------------------------------------------------
    id = fields.Integer(
        dump_only=True,
        metadata={'description': 'Unique asset identifier.'},
    )

    repository_name = fields.String(
        dump_only=True,
        metadata={'description': 'Repository this asset belongs to.'},
    )

    path = fields.String(
        dump_only=True,
        metadata={'description': 'Hierarchical path within the repository.'},
    )

    content_type = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={'description': 'MIME content type.'},
    )

    checksum_sha1 = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={'description': 'SHA-1 checksum (40 hex chars).'},
    )

    checksum_sha256 = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={'description': 'SHA-256 checksum (64 hex chars).'},
    )

    checksum_md5 = fields.String(
        dump_only=True,
        allow_none=True,
        metadata={'description': 'MD5 checksum (32 hex chars).'},
    )

    size = fields.Integer(
        dump_only=True,
        allow_none=True,
        metadata={'description': 'File size in bytes.'},
    )

    last_downloaded = fields.DateTime(
        dump_only=True,
        allow_none=True,
        metadata={'description': 'Last download timestamp (ISO 8601).'},
    )

    download_url = fields.String(
        dump_only=True,
        metadata={
            'description': (
                'Computed artifact download URL. '
                'Format: /repository/{repository_name}{path}'
            ),
        },
    )

    component_id = fields.Integer(
        dump_only=True,
        allow_none=True,
        metadata={'description': 'Parent component ID (null for metadata files).'},
    )

    attributes = fields.Dict(
        dump_only=True,
        allow_none=True,
        metadata={'description': 'Format-specific extensible metadata.'},
    )

    created_at = fields.DateTime(
        dump_only=True,
        metadata={'description': 'Creation timestamp (ISO 8601).'},
    )

    updated_at = fields.DateTime(
        dump_only=True,
        metadata={'description': 'Last update timestamp (ISO 8601).'},
    )

    @post_dump
    def compute_download_url(self, data: dict, **kwargs) -> dict:
        """Compute the download_url from repository_name and path.

        If ``download_url`` is not already present in the serialized data,
        this hook constructs it using the pattern::

            /repository/{repository_name}{path}

        Additionally, this hook:
        - Adds a ``size_human`` field with a human-readable file size string
        - Strips fields whose value is ``None`` for cleaner JSON output

        Args:
            data: The serialized output dictionary.

        Returns:
            The enriched output dictionary with computed fields.
        """
        # Compute download_url if missing — ensure a '/' separator is
        # always present between repository name and path even when the
        # stored path does not start with '/'.
        repo_name = data.get('repository_name')
        asset_path = data.get('path')
        if not data.get('download_url') and repo_name and asset_path:
            # Normalise: strip any trailing '/' from repo_name, strip any
            # leading '/' from asset_path, then join with exactly one '/'.
            clean_repo = repo_name.rstrip('/')
            clean_path = asset_path.lstrip('/')
            data['download_url'] = f'/repository/{clean_repo}/{clean_path}'

        # Add human-readable file size
        raw_size = data.get('size')
        if raw_size is not None:
            data['size_human'] = _format_file_size(raw_size)

        # Strip None-valued fields for cleaner JSON
        keys_to_remove = [key for key, value in data.items() if value is None]
        for key in keys_to_remove:
            del data[key]

        return data

    class Meta:
        """Schema meta-configuration."""


class AssetUploadSchema(Schema):
    """Marshmallow schema for asset upload request metadata validation.

    Validates the JSON metadata portion of upload requests. The actual
    binary file content is transmitted as multipart form data alongside
    this JSON body and is not represented in this schema.

    Fields:
    - repository_name: Target repository for the upload (required)
    - path: Storage path for the uploaded asset (required)
    - content_type: Optional MIME type override
    - attributes: Optional format-specific metadata
    """

    repository_name = fields.String(
        required=True,
        validate=Length(min=1, max=MAX_REPOSITORY_NAME_LENGTH),
        metadata={
            'description': 'Target repository name for the asset upload.',
            'example': 'maven-releases',
        },
    )

    path = fields.String(
        required=True,
        validate=Length(min=1, max=MAX_PATH_LENGTH),
        metadata={
            'description': (
                'Storage path for the uploaded asset within the repository. '
                'Must start with "/".'
            ),
            'example': '/com/example/mylib/1.0.0/mylib-1.0.0.jar',
        },
    )

    content_type = fields.String(
        required=False,
        allow_none=True,
        load_default=None,
        validate=Length(max=MAX_CONTENT_TYPE_LENGTH),
        metadata={
            'description': (
                'Optional MIME content type override. If not provided, '
                'the server will attempt automatic detection.'
            ),
            'example': 'application/java-archive',
        },
    )

    attributes = fields.Dict(
        required=False,
        allow_none=True,
        load_default=None,
        metadata={
            'description': 'Optional format-specific metadata for the uploaded asset.',
        },
    )

    @validates('path')
    def validate_upload_path(self, value: str) -> None:
        """Ensure the upload path starts with '/' for consistency.

        Args:
            value: The path string to validate.

        Raises:
            ValidationError: If the path does not start with '/'.
        """
        if value and not value.startswith('/'):
            raise ValidationError(
                "Upload path must start with '/' "
                "(e.g., '/com/example/mylib/1.0.0/mylib-1.0.0.jar')."
            )

    class Meta:
        """Schema meta-configuration."""


class AssetListQuerySchema(Schema):
    """Marshmallow schema for asset listing query parameter validation.

    Validates query parameters for the ``GET /api/v1/assets`` endpoint.
    All fields are optional to support flexible filtering and pagination.

    Supported filters:
    - repository: Filter by repository name
    - path_prefix: Filter by path prefix (for browse tree navigation, F-104)
    - content_type: Filter by MIME content type
    - component_id: Filter by parent component

    Pagination:
    - page: Page number (1-based, default 1)
    - page_size: Results per page (1–200, default 50)
    """

    repository = fields.String(
        required=False,
        load_default=None,
        metadata={
            'description': 'Filter assets by repository name.',
            'example': 'maven-central',
        },
    )

    path_prefix = fields.String(
        required=False,
        load_default=None,
        metadata={
            'description': (
                'Filter assets whose path starts with this prefix. '
                'Useful for browse tree navigation (F-104).'
            ),
            'example': '/org/apache/commons/',
        },
    )

    content_type = fields.String(
        required=False,
        load_default=None,
        metadata={
            'description': 'Filter assets by MIME content type.',
            'example': 'application/java-archive',
        },
    )

    component_id = fields.Integer(
        required=False,
        load_default=None,
        metadata={
            'description': 'Filter assets belonging to a specific component.',
            'example': 42,
        },
    )

    page = fields.Integer(
        required=False,
        load_default=DEFAULT_PAGE,
        validate=Range(min=1),
        metadata={
            'description': 'Page number for pagination (1-based).',
            'example': 1,
        },
    )

    page_size = fields.Integer(
        required=False,
        load_default=DEFAULT_PAGE_SIZE,
        validate=Range(min=1, max=MAX_PAGE_SIZE),
        metadata={
            'description': f'Number of results per page (1–{MAX_PAGE_SIZE}).',
            'example': 50,
        },
    )

    class Meta:
        """Schema meta-configuration."""


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _format_file_size(size_bytes: int) -> str:
    """Convert a file size in bytes to a human-readable string.

    Uses binary prefixes (KiB, MiB, GiB, TiB) for accuracy.

    Args:
        size_bytes: The size in bytes to format.

    Returns:
        A human-readable string representation of the file size.

    Examples:
        >>> _format_file_size(0)
        '0 B'
        >>> _format_file_size(1024)
        '1.0 KiB'
        >>> _format_file_size(1048576)
        '1.0 MiB'
        >>> _format_file_size(5368709120)
        '5.0 GiB'
    """
    if size_bytes == 0:
        return '0 B'

    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
    unit_index = 0
    size = float(size_bytes)

    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1

    if unit_index == 0:
        return f'{int(size)} B'

    return f'{size:.1f} {units[unit_index]}'


# ---------------------------------------------------------------------------
# Module-level exports
# ---------------------------------------------------------------------------

__all__ = [
    'AssetSchema',
    'AssetResponseSchema',
    'AssetUploadSchema',
    'AssetListQuerySchema',
]
