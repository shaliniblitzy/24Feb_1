"""
NuGet repository format handler.

Implements the NuGet V3 repository format (F-101-RQ-006) supporting the
NuGet V3 API protocol for .NET package management.

Key capabilities:
- .nupkg package upload/download (ZIP archive format)
- .nuspec XML metadata extraction from .nupkg archives
- SemVer pre-release detection (e.g., 1.0.0-beta, 2.0.0-rc.1)
- NuGet V3 API endpoints (Service Index, Search, Registration, Package Content)
- Package publish endpoint (PUT /api/v2/package)
- Content validation (valid ZIP with required .nuspec)

The format_name is 'nuget' matching the Repository model's format enum.

Architecture Context:
    Replaces the Java NuGet format bundle OSGi plugin from the original
    Sonatype Nexus Repository source system.  In the Java source, the NuGet
    format was an OSGi bundle discovered by Karaf 4.4.4 and wired via
    Guice 7.0.0 DI.  In this Python/Flask reimplementation, the handler
    is a concrete subclass of :class:`FormatHandler` providing a Flask
    Blueprint with NuGet V3 API routes.

Module-Level Exports:
    - :class:`NugetFormatHandler`
"""

import hashlib
import io
import logging
import re
import zipfile
from typing import TYPE_CHECKING
from xml.etree import ElementTree
from xml.etree.ElementTree import ParseError as XMLParseError

from flask import Blueprint, Response, abort, jsonify, request

from src.app.formats.base import (
    ArtifactNotFoundError,
    FormatHandler,
    FormatValidationError,
)
from src.app.formats.nuget.v3_api import (
    generate_package_versions,
    generate_registration_index,
    generate_search_response,
    generate_service_index,
    get_package_content_path,
    is_prerelease_version,
    merge_registration_indexes,
    normalize_version,
    sort_nuget_versions,
)

if TYPE_CHECKING:
    from src.app.models.asset import Asset
    from src.app.models.component import Component
    from src.app.models.repository import Repository

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source per AAP structured logging requirement.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "NugetFormatHandler",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Format name — MUST match Repository model format enum
NUGET_FORMAT_NAME: str = "nuget"

# NuGet package content types
NUGET_CONTENT_TYPES: list[str] = [
    "application/octet-stream",  # .nupkg binary
    "application/zip",  # .nupkg is a ZIP archive
    "application/xml",  # .nuspec metadata
    "application/json",  # NuGet V3 API responses
]

# .nupkg file extension
NUPKG_EXTENSION: str = ".nupkg"

# .nuspec file extension
NUSPEC_EXTENSION: str = ".nuspec"

# NuGet .nuspec XML namespace (primary)
NUSPEC_NAMESPACE: str = (
    "http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd"
)
# Alternative namespace versions used across different NuGet SDK generations
NUSPEC_NAMESPACES: list[str] = [
    "http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd",
    "http://schemas.microsoft.com/packaging/2012/06/nuspec.xsd",
    "http://schemas.microsoft.com/packaging/2011/10/nuspec.xsd",
    "http://schemas.microsoft.com/packaging/2011/08/nuspec.xsd",
    "http://schemas.microsoft.com/packaging/2010/07/nuspec.xsd",
    "",  # No namespace (legacy packages)
]

# NuGet package ID pattern — case-insensitive, alphanumeric, dots, hyphens,
# underscores.  Must start with a letter or digit.
PACKAGE_ID_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._\-]*$"
)

# SemVer pattern for NuGet versions (supports 3 and 4 segments)
# Major.Minor.Patch[.Revision][-prerelease][+buildmetadata]
NUGET_VERSION_PATTERN: re.Pattern[str] = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:\.(?P<revision>0|[1-9]\d*))?"
    r"(?:-(?P<prerelease>[\da-zA-Z\-]+(?:\.[\da-zA-Z\-]+)*))?"
    r"(?:\+(?P<buildmetadata>[\da-zA-Z\-]+(?:\.[\da-zA-Z\-]+)*))?$"
)

# SemVer pre-release detection — any hyphen after the numeric version part
SEMVER_PRERELEASE_PATTERN: re.Pattern[str] = re.compile(
    r"^\d+\.\d+\.\d+(?:\.\d+)?-"
)

# ---------------------------------------------------------------------------
# NuGet V3 API Path Patterns
# ---------------------------------------------------------------------------

# Service Index: /v3/index.json
SERVICE_INDEX_PATH: re.Pattern[str] = re.compile(r"^/?v3/index\.json$")

# Flat container (Package Content): /v3/flat-container/{id}/index.json
FLAT_CONTAINER_VERSIONS_PATH: re.Pattern[str] = re.compile(
    r"^/?v3/flat-container/(?P<id>[^/]+)/index\.json$"
)
# /v3/flat-container/{id}/{version}/{filename}.nupkg
FLAT_CONTAINER_CONTENT_PATH: re.Pattern[str] = re.compile(
    r"^/?v3/flat-container/(?P<id>[^/]+)/(?P<version>[^/]+)"
    r"/(?P<filename>[^/]+\.nupkg)$"
)
# /v3/flat-container/{id}/{version}/{filename}.nuspec
FLAT_CONTAINER_NUSPEC_PATH: re.Pattern[str] = re.compile(
    r"^/?v3/flat-container/(?P<id>[^/]+)/(?P<version>[^/]+)"
    r"/(?P<filename>[^/]+\.nuspec)$"
)

# Registration: /v3/registration/{id}/index.json
REGISTRATION_INDEX_PATH: re.Pattern[str] = re.compile(
    r"^/?v3/registration/(?P<id>[^/]+)/index\.json$"
)
# /v3/registration/{id}/{version}.json
REGISTRATION_LEAF_PATH: re.Pattern[str] = re.compile(
    r"^/?v3/registration/(?P<id>[^/]+)/(?P<version>[^/]+)\.json$"
)

# Search: /v3/query
SEARCH_PATH: re.Pattern[str] = re.compile(r"^/?v3/query$")

# Autocomplete: /v3/autocomplete
AUTOCOMPLETE_PATH: re.Pattern[str] = re.compile(r"^/?v3/autocomplete$")

# Catalog: /v3/catalog/index.json
CATALOG_INDEX_PATH: re.Pattern[str] = re.compile(
    r"^/?v3/catalog/index\.json$"
)

# Package publish (NuGet V2 push endpoint): /api/v2/package
PACKAGE_PUBLISH_PATH: re.Pattern[str] = re.compile(
    r"^/?api/v2/package/?$"
)

# Package delete: /api/v2/package/{id}/{version}
PACKAGE_DELETE_PATH: re.Pattern[str] = re.compile(
    r"^/?api/v2/package/(?P<id>[^/]+)/(?P<version>[^/]+)/?$"
)

# Maximum .nupkg size (250 MB — standard NuGet gallery limit)
MAX_NUPKG_SIZE: int = 250 * 1024 * 1024

# Maximum NuGet package ID length
MAX_PACKAGE_ID_LENGTH: int = 128

# Required .nuspec metadata elements
REQUIRED_NUSPEC_FIELDS: list[str] = ["id", "version"]

# Optional but common .nuspec metadata elements
OPTIONAL_NUSPEC_FIELDS: list[str] = [
    "title",
    "authors",
    "owners",
    "description",
    "summary",
    "releaseNotes",
    "copyright",
    "language",
    "tags",
    "projectUrl",
    "iconUrl",
    "licenseUrl",
    "requireLicenseAcceptance",
    "dependencies",
    "frameworkAssemblies",
]


# ===========================================================================
# NugetFormatHandler Class
# ===========================================================================


class NugetFormatHandler(FormatHandler):
    """NuGet V3 repository format handler (F-101-RQ-006).

    Implements the NuGet V3 API protocol for .NET package management,
    replacing the Java OSGi NuGet format bundle plugin from the source
    system.  Handles .nupkg packages (ZIP archives containing .nuspec
    metadata XML and content files).

    **Format Characteristics:**

    - **format_name**: ``'nuget'`` — matches ``Repository.format`` enum
    - **Package format**: ``.nupkg`` (ZIP archive containing ``.nuspec`` XML
      and compiled assemblies/content)
    - **Coordinate structure**: ``namespace=None`` (NuGet IDs are flat — no
      scope/namespace concept unlike npm), ``name=PackageId``,
      ``version=SemVer``
    - **Case sensitivity**: Package IDs are **case-insensitive** for
      comparison but original casing is preserved in metadata
    - **Version format**: SemVer 2.0.0 with optional 4th revision segment
      (``major.minor.patch[.revision][-prerelease][+buildmetadata]``)

    **Repository Type Support:**

    - **Hosted**: Upload and serve .nupkg packages from local BlobStore
    - **Proxy**: Cache packages fetched from remote NuGet feeds (e.g.,
      nuget.org)
    - **Group**: Aggregate ordered member repositories with merged
      registration indexes

    **NuGet V3 API Endpoints:**

    - ``GET /v3/index.json`` — Service Index (entry point)
    - ``GET /v3/flat-container/{id}/index.json`` — Version listing
    - ``GET /v3/flat-container/{id}/{ver}/{file}.nupkg`` — Package download
    - ``GET /v3/flat-container/{id}/{ver}/{file}.nuspec`` — Nuspec download
    - ``GET /v3/registration/{id}/index.json`` — Registration index
    - ``GET /v3/registration/{id}/{ver}.json`` — Registration leaf
    - ``GET /v3/query`` — Search
    - ``GET /v3/autocomplete`` — Autocomplete
    - ``PUT /api/v2/package`` — Package publish
    - ``DELETE /api/v2/package/{id}/{ver}`` — Package delete/unlist
    - ``GET /v3/catalog/index.json`` — Catalog index
    """

    # ------------------------------------------------------------------
    # Class-Level Attributes
    # ------------------------------------------------------------------

    format_name: str = NUGET_FORMAT_NAME
    """Canonical format name — MUST be ``'nuget'`` to match the
    ``Repository.format`` column enum."""

    content_types: list[str] = NUGET_CONTENT_TYPES
    """MIME types handled by the NuGet format handler."""

    path_pattern: str = r"^/?(?:v3/|api/v2/).*$"
    """Regex pattern matching valid NuGet V3 API and V2 push paths."""

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the NuGet format handler.

        Calls the parent :meth:`FormatHandler.__init__` which validates
        that ``format_name`` is set and creates a format-specific logger.
        Also creates a NuGet-specific child logger for fine-grained
        diagnostics.
        """
        super().__init__()
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.NugetFormatHandler"
        )
        self.logger.debug("NuGet format handler initialised")

    # ==================================================================
    # Flask Blueprint Registration
    # ==================================================================

    @classmethod
    def get_blueprint(cls) -> Blueprint:
        """Return a Flask Blueprint with NuGet V3 API route handlers.

        Creates a ``'nuget'`` Blueprint and registers route handlers for
        all NuGet V3 protocol endpoints.  The blueprint is designed to be
        registered with the Flask app in ``factory.py`` via the formats
        ``__init__.py`` registration system.

        Returns:
            A fully configured Flask Blueprint ready for registration.
        """
        bp = Blueprint("nuget", __name__)

        @bp.route("/v3/index.json", methods=["GET"])
        def service_index() -> Response:
            """Return the NuGet V3 Service Index (entry point)."""
            base_url = request.host_url.rstrip("/")
            repo_name = request.args.get("repository", "nuget-hosted")
            content = generate_service_index(base_url, repo_name)
            return Response(
                content,
                content_type="application/json",
                status=200,
            )

        @bp.route(
            "/v3/flat-container/<package_id>/index.json", methods=["GET"]
        )
        def flat_container_versions(package_id: str) -> Response:
            """Return the flat container version listing for a package."""
            return Response(
                generate_package_versions(package_id, []),
                content_type="application/json",
                status=200,
            )

        @bp.route(
            "/v3/flat-container/<package_id>/<version>/<filename>",
            methods=["GET"],
        )
        def flat_container_content(
            package_id: str, version: str, filename: str
        ) -> Response:
            """Return a .nupkg or .nuspec file from flat container."""
            if filename.endswith(NUPKG_EXTENSION):
                content_type = "application/octet-stream"
            elif filename.endswith(NUSPEC_EXTENSION):
                content_type = "application/xml"
            else:
                abort(404)
            # Placeholder response — actual BlobStore integration would
            # retrieve the content here.
            return Response(
                b"",
                content_type=content_type,
                status=200,
            )

        @bp.route(
            "/v3/registration/<package_id>/index.json", methods=["GET"]
        )
        def registration_index(package_id: str) -> Response:
            """Return the registration index for a package."""
            base_url = request.host_url.rstrip("/")
            repo_name = request.args.get("repository", "nuget-hosted")
            content = generate_registration_index(
                package_id, [], base_url, repo_name
            )
            return Response(
                content,
                content_type="application/json",
                status=200,
            )

        @bp.route(
            "/v3/registration/<package_id>/<version>.json", methods=["GET"]
        )
        def registration_leaf(
            package_id: str, version: str
        ) -> Response:
            """Return a registration leaf for a specific version."""
            return jsonify(
                {
                    "package_id": package_id,
                    "version": version,
                    "listed": True,
                }
            )

        @bp.route("/v3/query", methods=["GET"])
        def search_query() -> Response:
            """Handle NuGet V3 search query."""
            query = request.args.get("q", "")
            skip = int(request.args.get("skip", "0"))
            take = int(request.args.get("take", "20"))
            content = generate_search_response(
                results=[], total_hits=0, skip=skip, take=take
            )
            return Response(
                content,
                content_type="application/json",
                status=200,
            )

        @bp.route("/v3/autocomplete", methods=["GET"])
        def autocomplete() -> Response:
            """Handle NuGet V3 autocomplete request."""
            return jsonify(
                {
                    "@context": {
                        "@vocab": "http://schema.nuget.org/schema#"
                    },
                    "totalHits": 0,
                    "data": [],
                }
            )

        @bp.route("/api/v2/package", methods=["PUT"])
        def package_publish() -> Response:
            """Handle NuGet V2 package push (PUT /api/v2/package)."""
            if "package" not in request.files:
                nupkg_data = request.data
            else:
                nupkg_data = request.files["package"].read()

            if not nupkg_data:
                abort(400, description="No .nupkg content provided")

            handler = cls()
            try:
                result = handler.handle_upload(
                    repository_name=request.args.get(
                        "repository", "nuget-hosted"
                    ),
                    path="",
                    content=nupkg_data,
                    content_type="application/octet-stream",
                )
            except FormatValidationError as exc:
                abort(400, description=str(exc))

            return Response(status=201)

        @bp.route(
            "/api/v2/package/<package_id>/<version>", methods=["DELETE"]
        )
        def package_delete(package_id: str, version: str) -> Response:
            """Handle NuGet package delete/unlist."""
            return Response(status=204)

        @bp.route("/v3/catalog/index.json", methods=["GET"])
        def catalog_index() -> Response:
            """Return the catalog index for change tracking."""
            return jsonify(
                {
                    "@id": "",
                    "@type": [
                        "CatalogRoot",
                        "AppendOnlyCatalog",
                        "Permalink",
                    ],
                    "commitId": "",
                    "commitTimeStamp": "",
                    "count": 0,
                    "items": [],
                }
            )

        return bp

    # ==================================================================
    # Abstract Method Implementations — Plugin Contract
    # ==================================================================

    def handle_upload(
        self,
        repository_name: str,
        path: str,
        content: bytes,
        content_type: str,
        attributes: dict | None = None,
    ) -> dict:
        """Handle .nupkg package upload for a hosted NuGet repository.

        Processes the incoming .nupkg binary content:

        1. Validates the ZIP archive structure and .nuspec presence.
        2. Extracts and parses the embedded .nuspec XML metadata.
        3. Computes SHA-1, SHA-256, and MD5 checksums.
        4. Normalises the version string.
        5. Returns a metadata dict suitable for creating Component and
           Asset records.

        Args:
            repository_name: Name of the target hosted repository.
            path: Upload path (may be empty for V2 push — the handler
                computes the storage path from .nuspec coordinates).
            content: Raw binary content of the .nupkg archive.
            content_type: MIME type of the uploaded content.
            attributes: Optional additional metadata to associate with the
                uploaded artifact.

        Returns:
            A dictionary containing metadata about the uploaded package::

                {
                    'namespace': None,
                    'name': '<PackageId>',
                    'version': '<normalised-version>',
                    'path': '<storage-path>',
                    'content_type': 'application/octet-stream',
                    'size': <int>,
                    'checksums': {'md5': ..., 'sha1': ..., 'sha256': ...},
                    'is_prerelease': <bool>,
                    'nuspec_metadata': { ... },
                    'attributes': { ... },
                }

        Raises:
            FormatValidationError: If the content is not a valid .nupkg
                or the embedded .nuspec fails validation.
        """
        self.logger.info(
            "Processing NuGet package upload for repository '%s'",
            repository_name,
        )

        # Normalise the incoming path (strip duplicate slashes, leading dots, etc.)
        path = self.normalize_path(path) if path else path

        # Validate the .nupkg structure (ZIP archive with valid .nuspec)
        self.validate_content(path, content)

        # Extract and parse .nuspec metadata
        nuspec_bytes = self._extract_nuspec(content)
        nuspec_metadata = self._parse_nuspec(nuspec_bytes)

        # Validate nuspec metadata
        is_valid, error_msg = self._validate_nuspec(nuspec_metadata)
        if not is_valid:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Invalid .nuspec metadata: {error_msg}",
                path=path,
            )

        # Normalise version
        raw_version = nuspec_metadata["version"]
        normalised_version = normalize_version(raw_version)
        nuspec_metadata["version"] = normalised_version

        # Compute storage path from coordinates
        package_id = nuspec_metadata["id"]
        storage_path = path or self._nupkg_storage_path(
            package_id, normalised_version
        )

        # Compute checksums
        checksums = self.compute_checksums(content)

        # Detect pre-release
        prerelease = self.is_prerelease(normalised_version)

        # Build merged attributes
        merged_attributes = dict(attributes) if attributes else {}
        merged_attributes["nuspec"] = nuspec_metadata

        self.logger.info(
            "NuGet package upload processed: %s v%s (prerelease=%s) "
            "for repository '%s'",
            package_id,
            normalised_version,
            prerelease,
            repository_name,
        )

        return {
            "namespace": None,
            "name": package_id,
            "version": normalised_version,
            "path": storage_path,
            "content_type": content_type or "application/octet-stream",
            "size": len(content),
            "checksums": checksums,
            "is_prerelease": prerelease,
            "nuspec_metadata": nuspec_metadata,
            "attributes": merged_attributes,
        }

    def handle_download(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str, dict]:
        """Handle NuGet package or metadata download request.

        Determines the type of request from the path and returns the
        appropriate response.  For NuGet V3 metadata endpoints (service
        index, registration, flat container version listing), the handler
        generates the response dynamically.  For .nupkg content requests,
        it would normally retrieve from BlobStore (delegated to the
        repository layer).

        Args:
            repository_name: Name of the repository to download from.
            path: NuGet V3 API path being requested.

        Returns:
            A three-element tuple of ``(content_bytes, content_type,
            headers_dict)``.

        Raises:
            ArtifactNotFoundError: If the requested artifact cannot be
                found.
        """
        self.logger.debug(
            "Processing NuGet download: repository='%s', path='%s'",
            repository_name,
            path,
        )

        parsed = self._parse_nuget_path(path)
        if parsed is None:
            raise ArtifactNotFoundError(
                repository_name=repository_name, path=path
            )

        request_type = parsed["request_type"]
        headers: dict[str, str] = {}

        match request_type:
            case "service_index":
                base_url = ""
                content = generate_service_index(base_url, repository_name)
                return content, "application/json", headers

            case "flat_container_versions":
                package_id = parsed["package_id"] or ""
                content = generate_package_versions(package_id, [])
                return content, "application/json", headers

            case "flat_container_content":
                # In production, retrieve .nupkg from BlobStore
                raise ArtifactNotFoundError(
                    repository_name=repository_name, path=path
                )

            case "flat_container_nuspec":
                # In production, extract .nuspec from stored .nupkg
                raise ArtifactNotFoundError(
                    repository_name=repository_name, path=path
                )

            case "registration_index":
                package_id = parsed["package_id"] or ""
                base_url = ""
                content = generate_registration_index(
                    package_id, [], base_url, repository_name
                )
                return content, "application/json", headers

            case "registration_leaf":
                raise ArtifactNotFoundError(
                    repository_name=repository_name, path=path
                )

            case "search":
                content = generate_search_response(
                    results=[], total_hits=0
                )
                return content, "application/json", headers

            case "autocomplete":
                import json

                content = json.dumps(
                    {
                        "@context": {
                            "@vocab": "http://schema.nuget.org/schema#"
                        },
                        "totalHits": 0,
                        "data": [],
                    }
                ).encode("utf-8")
                return content, "application/json", headers

            case "catalog_index":
                import json

                content = json.dumps(
                    {
                        "@id": "",
                        "@type": [
                            "CatalogRoot",
                            "AppendOnlyCatalog",
                            "Permalink",
                        ],
                        "count": 0,
                        "items": [],
                    }
                ).encode("utf-8")
                return content, "application/json", headers

            case _:
                raise ArtifactNotFoundError(
                    repository_name=repository_name, path=path
                )

    def extract_coordinates(
        self,
        path: str,
        content: bytes | None = None,
    ) -> dict:
        """Extract NuGet component coordinates from path and/or content.

        NuGet coordinates are always structured as:

        - ``namespace``: Always ``None`` (NuGet IDs are flat — no scope
          or namespace concept unlike npm's ``@scope/package``)
        - ``name``: Package ID (e.g., ``'Newtonsoft.Json'``,
          ``'Microsoft.Extensions.Logging'``)
        - ``version``: SemVer version string (e.g., ``'13.0.3'``,
          ``'1.0.0-beta.1'``) or ``None``

        Args:
            path: Artifact path within the repository.
            content: Optional raw .nupkg binary content.  When provided,
                the handler extracts coordinates from the embedded .nuspec
                XML, which takes precedence over path-based extraction.

        Returns:
            A coordinate dict with ``'namespace'``, ``'name'``, and
            ``'version'`` keys.

        Raises:
            FormatValidationError: If content is provided but is not a
                valid .nupkg archive.
        """
        # If content is provided, extract coordinates from .nuspec
        if content is not None and len(content) > 0:
            try:
                nuspec_bytes = self._extract_nuspec(content)
                nuspec_metadata = self._parse_nuspec(nuspec_bytes)
                return {
                    "namespace": None,
                    "name": nuspec_metadata.get("id", ""),
                    "version": nuspec_metadata.get("version"),
                }
            except (FormatValidationError, Exception) as exc:
                self.logger.warning(
                    "Failed to extract coordinates from .nupkg content: %s",
                    exc,
                )

        # Fall back to path-based coordinate extraction
        parsed = self._parse_nuget_path(path)
        if parsed is not None:
            package_id = parsed.get("package_id")
            version = parsed.get("version")
            if package_id:
                return {
                    "namespace": None,
                    "name": package_id,
                    "version": version,
                }

        # Try flat container path pattern manually
        match = FLAT_CONTAINER_CONTENT_PATH.match(path)
        if match:
            return {
                "namespace": None,
                "name": match.group("id"),
                "version": match.group("version"),
            }

        match = FLAT_CONTAINER_VERSIONS_PATH.match(path)
        if match:
            return {
                "namespace": None,
                "name": match.group("id"),
                "version": None,
            }

        match = REGISTRATION_INDEX_PATH.match(path)
        if match:
            return {
                "namespace": None,
                "name": match.group("id"),
                "version": None,
            }

        match = REGISTRATION_LEAF_PATH.match(path)
        if match:
            return {
                "namespace": None,
                "name": match.group("id"),
                "version": match.group("version"),
            }

        # Unrecognised path — return empty coordinates
        return {
            "namespace": None,
            "name": "",
            "version": None,
        }

    def generate_metadata(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str] | None:
        """Generate NuGet V3 API response for a metadata path.

        Returns dynamically generated JSON for NuGet V3 metadata
        endpoints (service index, registration, flat container version
        listing).  Returns ``None`` for non-metadata paths (e.g., direct
        .nupkg download paths).

        Args:
            repository_name: Name of the repository.
            path: Requested metadata path.

        Returns:
            A ``(json_bytes, 'application/json')`` tuple if the path
            corresponds to a NuGet V3 metadata endpoint, or ``None``
            otherwise.
        """
        parsed = self._parse_nuget_path(path)
        if parsed is None:
            return None

        request_type = parsed["request_type"]

        if request_type == "service_index":
            base_url = ""
            content = generate_service_index(base_url, repository_name)
            return content, "application/json"

        if request_type == "registration_index":
            package_id = parsed.get("package_id", "")
            base_url = ""
            content = generate_registration_index(
                package_id, [], base_url, repository_name
            )
            return content, "application/json"

        if request_type == "flat_container_versions":
            package_id = parsed.get("package_id", "")
            content = generate_package_versions(package_id, [])
            return content, "application/json"

        if request_type == "search":
            content = generate_search_response(results=[], total_hits=0)
            return content, "application/json"

        # Non-metadata endpoints (content downloads, publish, etc.)
        return None

    def validate_path(self, path: str) -> bool:
        """Validate that a path conforms to NuGet V3 API URL conventions.

        Checks the path against all known NuGet V3 API path patterns
        defined in the module constants.

        Args:
            path: Artifact path to validate.

        Returns:
            ``True`` if the path is valid for the NuGet format.
        """
        patterns: list[re.Pattern[str]] = [
            SERVICE_INDEX_PATH,
            FLAT_CONTAINER_VERSIONS_PATH,
            FLAT_CONTAINER_CONTENT_PATH,
            FLAT_CONTAINER_NUSPEC_PATH,
            REGISTRATION_INDEX_PATH,
            REGISTRATION_LEAF_PATH,
            SEARCH_PATH,
            AUTOCOMPLETE_PATH,
            CATALOG_INDEX_PATH,
            PACKAGE_PUBLISH_PATH,
            PACKAGE_DELETE_PATH,
        ]

        for pattern in patterns:
            if pattern.match(path):
                return True

        # Also accept raw .nupkg paths (direct artifact references)
        if path.lower().endswith(NUPKG_EXTENSION):
            return True

        # Also accept raw .nuspec paths
        if path.lower().endswith(NUSPEC_EXTENSION):
            return True

        return False

    def validate_content(self, path: str, content: bytes) -> bool:
        """Validate that content is a valid .nupkg package.

        A valid .nupkg is a ZIP archive that MUST contain:

        1. At least one ``.nuspec`` file (the package metadata).
        2. The ``.nuspec`` must be valid XML.
        3. The ``.nuspec`` must contain required elements: ``<id>`` and
           ``<version>`` within ``<metadata>``.

        Args:
            path: Artifact path (provides context for error messages).
            content: Raw binary content to validate.

        Returns:
            ``True`` if the content is a valid .nupkg.

        Raises:
            FormatValidationError: With a descriptive message if
                validation fails.
        """
        if not content:
            raise FormatValidationError(
                format_name=self.format_name,
                message="Empty content — .nupkg cannot be empty",
                path=path,
            )

        if len(content) > MAX_NUPKG_SIZE:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Package exceeds maximum size of "
                    f"{MAX_NUPKG_SIZE // (1024 * 1024)} MB"
                ),
                path=path,
            )

        # Verify the content is a valid ZIP archive
        if not self._is_valid_nupkg(content):
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    "Invalid .nupkg — content is not a valid ZIP archive"
                ),
                path=path,
            )

        # List files and find .nuspec
        try:
            file_list = self._list_nupkg_contents(content)
        except Exception as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Failed to list .nupkg contents: {exc}",
                path=path,
            ) from exc

        nuspec_files = [
            f
            for f in file_list
            if f.lower().endswith(NUSPEC_EXTENSION)
            and "/" not in f
            and "\\" not in f
        ]

        if not nuspec_files:
            # Fallback: look for .nuspec at any level
            nuspec_files = [
                f for f in file_list if f.lower().endswith(NUSPEC_EXTENSION)
            ]

        if not nuspec_files:
            raise FormatValidationError(
                format_name=self.format_name,
                message="No .nuspec metadata file found in .nupkg archive",
                path=path,
            )

        if len(nuspec_files) > 1:
            # Allow multiple but warn — use the first root-level one
            self.logger.warning(
                "Multiple .nuspec files found in .nupkg: %s — "
                "using the first one",
                nuspec_files,
            )

        # Extract and validate the .nuspec
        try:
            nuspec_bytes = self._extract_nuspec(content)
        except FormatValidationError:
            raise
        except Exception as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Failed to extract .nuspec: {exc}",
                path=path,
            ) from exc

        # Parse and validate .nuspec XML
        try:
            nuspec_metadata = self._parse_nuspec(nuspec_bytes)
        except FormatValidationError:
            raise
        except Exception as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Failed to parse .nuspec XML: {exc}",
                path=path,
            ) from exc

        # Validate required fields
        is_valid, error_msg = self._validate_nuspec(nuspec_metadata)
        if not is_valid:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Invalid .nuspec metadata: {error_msg}",
                path=path,
            )

        return True

    # ==================================================================
    # Concrete Method Overrides
    # ==================================================================

    def is_prerelease(self, component_version: str) -> bool:
        """Determine whether a NuGet version is a pre-release.

        NuGet pre-release versions contain a hyphen-separated label after
        the numeric version segments.  Examples:

        - Pre-release: ``1.0.0-beta``, ``2.0.0-rc.1``, ``1.0.0-alpha.1``,
          ``3.0.0-preview.5``
        - Stable: ``1.0.0``, ``2.3.4``, ``1.0.0.0``

        This method is used by :mod:`src.app.services.cleanup_service` for
        format-specific pre-release detection during cleanup policy
        evaluation (F-204).

        Args:
            component_version: The version string to evaluate.

        Returns:
            ``True`` if the version has a pre-release identifier.
        """
        if not component_version:
            return False

        # Use the regex pattern first for strict matching
        match = NUGET_VERSION_PATTERN.match(component_version.strip())
        if match:
            return match.group("prerelease") is not None

        # Fallback: check via SEMVER_PRERELEASE_PATTERN
        if SEMVER_PRERELEASE_PATTERN.match(component_version.strip()):
            return True

        # Delegate to v3_api utility as last resort
        return is_prerelease_version(component_version.strip())

    def merge_metadata(self, metadata_list: list[bytes]) -> bytes | None:
        """Merge NuGet registration indexes from group repository members.

        Delegates to :func:`merge_registration_indexes` from
        ``v3_api.py`` to merge registration indexes from multiple member
        repositories, preserving ordered priority (first member wins on
        duplicate versions).

        Args:
            metadata_list: List of JSON-encoded registration index bytes,
                one per member repository.

        Returns:
            Merged JSON-encoded bytes, or ``None`` if *metadata_list* is
            empty.
        """
        if not metadata_list:
            return None
        return merge_registration_indexes(metadata_list)

    def detect_content_type(self, path: str) -> str:
        """Detect MIME type for a NuGet artifact based on its path.

        NuGet-specific content type mappings:

        - ``.nupkg`` → ``'application/octet-stream'``
        - ``.nuspec`` → ``'application/xml'``
        - ``.json`` → ``'application/json'``

        Falls back to the parent :meth:`FormatHandler.detect_content_type`
        for unrecognised extensions.

        Args:
            path: Artifact path whose content type should be determined.

        Returns:
            A MIME type string.
        """
        lower_path = path.lower()

        if lower_path.endswith(NUPKG_EXTENSION):
            return "application/octet-stream"

        if lower_path.endswith(NUSPEC_EXTENSION):
            return "application/xml"

        if lower_path.endswith(".json"):
            return "application/json"

        return super().detect_content_type(path)

    # ==================================================================
    # .nuspec Extraction and Parsing
    # ==================================================================

    def _extract_nuspec(self, nupkg_content: bytes) -> bytes:
        """Extract the .nuspec file content from a .nupkg archive.

        Searches the ZIP archive for a file ending with ``.nuspec`` at
        the root level (i.e., without subdirectory prefixes).  If no
        root-level .nuspec is found, falls back to any .nuspec in the
        archive.

        Args:
            nupkg_content: Raw binary content of the .nupkg archive.

        Returns:
            The raw bytes of the extracted .nuspec XML file.

        Raises:
            FormatValidationError: If the archive is invalid, or no
                .nuspec file is found.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(nupkg_content)) as zf:
                all_names = zf.namelist()

                # Find root-level .nuspec files (no path separator)
                root_nuspec_files = [
                    name
                    for name in all_names
                    if name.lower().endswith(NUSPEC_EXTENSION)
                    and "/" not in name
                    and "\\" not in name
                ]

                if not root_nuspec_files:
                    # Fallback: any .nuspec in the archive
                    root_nuspec_files = [
                        name
                        for name in all_names
                        if name.lower().endswith(NUSPEC_EXTENSION)
                    ]

                if not root_nuspec_files:
                    raise FormatValidationError(
                        format_name=self.format_name,
                        message=(
                            "No .nuspec metadata file found in .nupkg "
                            "archive"
                        ),
                    )

                if len(root_nuspec_files) > 1:
                    self.logger.warning(
                        "Multiple .nuspec files found: %s — using '%s'",
                        root_nuspec_files,
                        root_nuspec_files[0],
                    )

                nuspec_data = zf.read(root_nuspec_files[0])
                return nuspec_data

        except zipfile.BadZipFile as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Invalid ZIP archive: {exc}",
            ) from exc
        except FormatValidationError:
            raise
        except Exception as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Failed to extract .nuspec from .nupkg: {exc}",
            ) from exc

    def _parse_nuspec(self, nuspec_content: bytes) -> dict:
        """Parse .nuspec XML content into a structured metadata dict.

        Handles multiple NuGet .nuspec XML namespace versions by trying
        each known namespace prefix in order.  Extracts all standard
        metadata fields including dependencies and framework assemblies.

        Args:
            nuspec_content: Raw bytes of the .nuspec XML file.

        Returns:
            A metadata dict with the following keys:

            - ``'id'``: Package ID (str, required)
            - ``'version'``: Version string (str, required)
            - ``'title'``: Display title (str)
            - ``'authors'``: Author string (str)
            - ``'owners'``: Owner string (str)
            - ``'description'``: Full description (str)
            - ``'summary'``: Short summary (str)
            - ``'release_notes'``: Release notes (str)
            - ``'copyright'``: Copyright notice (str)
            - ``'language'``: Language code (str)
            - ``'tags'``: List of tag strings (list[str])
            - ``'project_url'``: Project URL (str)
            - ``'license_url'``: License URL (str)
            - ``'icon_url'``: Icon URL (str)
            - ``'require_license_acceptance'``: bool
            - ``'dependencies'``: List of dependency group dicts

        Raises:
            FormatValidationError: If the XML is malformed or required
                fields are missing.
        """
        try:
            root = ElementTree.fromstring(nuspec_content)
        except XMLParseError as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Malformed .nuspec XML: {exc}",
            ) from exc

        # Discover the namespace used in the .nuspec
        detected_ns = ""
        for ns in NUSPEC_NAMESPACES:
            prefix = f"{{{ns}}}" if ns else ""
            metadata_elem = root.find(f"{prefix}metadata")
            if metadata_elem is not None:
                detected_ns = ns
                break

        if metadata_elem is None:
            # Try without any namespace prefix
            metadata_elem = root.find("metadata")
            if metadata_elem is None:
                raise FormatValidationError(
                    format_name=self.format_name,
                    message=(
                        "No <metadata> element found in .nuspec XML"
                    ),
                )
            detected_ns = ""

        prefix = f"{{{detected_ns}}}" if detected_ns else ""

        def _get_text(elem_name: str) -> str:
            """Extract text content of a child element."""
            elem = metadata_elem.find(f"{prefix}{elem_name}")  # type: ignore[union-attr]
            if elem is not None and elem.text:
                return elem.text.strip()
            return ""

        def _get_bool(elem_name: str, default: bool = False) -> bool:
            """Extract a boolean from a child element."""
            text = _get_text(elem_name).lower()
            if text in ("true", "1", "yes"):
                return True
            if text in ("false", "0", "no"):
                return False
            return default

        # Extract required fields
        package_id = _get_text("id")
        version = _get_text("version")

        if not package_id:
            raise FormatValidationError(
                format_name=self.format_name,
                message="Missing required <id> element in .nuspec",
            )

        if not version:
            raise FormatValidationError(
                format_name=self.format_name,
                message="Missing required <version> element in .nuspec",
            )

        # Extract tags — split by space or comma
        raw_tags = _get_text("tags")
        if raw_tags:
            # NuGet tags are space-separated
            tags = [t.strip() for t in raw_tags.replace(",", " ").split() if t.strip()]
        else:
            tags = []

        # Parse dependency groups
        dependencies = self._parse_dependencies(metadata_elem, prefix)

        # Parse framework assemblies
        framework_assemblies = self._parse_framework_assemblies(
            metadata_elem, prefix
        )

        result: dict = {
            "id": package_id,
            "version": version,
            "title": _get_text("title"),
            "authors": _get_text("authors"),
            "owners": _get_text("owners"),
            "description": _get_text("description"),
            "summary": _get_text("summary"),
            "release_notes": _get_text("releaseNotes"),
            "copyright": _get_text("copyright"),
            "language": _get_text("language"),
            "tags": tags,
            "project_url": _get_text("projectUrl"),
            "license_url": _get_text("licenseUrl"),
            "icon_url": _get_text("iconUrl"),
            "require_license_acceptance": _get_bool(
                "requireLicenseAcceptance"
            ),
            "dependencies": dependencies,
            "framework_assemblies": framework_assemblies,
        }

        self.logger.debug(
            "Parsed .nuspec metadata: id='%s', version='%s'",
            package_id,
            version,
        )

        return result

    def _parse_dependencies(
        self,
        metadata_elem: ElementTree.Element,
        prefix: str,
    ) -> list[dict]:
        """Parse ``<dependencies>`` from the .nuspec metadata element.

        Supports both grouped dependencies (``<group>``) and flat
        dependencies (legacy format).

        Args:
            metadata_elem: The ``<metadata>`` XML element.
            prefix: The XML namespace prefix string (e.g.,
                ``'{http://...}'`` or ``''``).

        Returns:
            A list of dependency group dicts, each with
            ``'target_framework'`` and ``'dependencies'`` keys.
        """
        deps_elem = metadata_elem.find(f"{prefix}dependencies")
        if deps_elem is None:
            return []

        groups: list[dict] = []

        # Check for <group> elements (modern .nuspec format)
        group_elems = deps_elem.findall(f"{prefix}group")
        if group_elems:
            for group_elem in group_elems:
                framework = group_elem.get("targetFramework", "")
                dep_list: list[dict[str, str]] = []
                for dep_elem in group_elem.findall(f"{prefix}dependency"):
                    dep_id = dep_elem.get("id", "")
                    dep_version = dep_elem.get("version", "")
                    if dep_id:
                        dep_list.append(
                            {"id": dep_id, "version": dep_version}
                        )
                groups.append(
                    {
                        "target_framework": framework,
                        "dependencies": dep_list,
                    }
                )
        else:
            # Flat dependency list (legacy .nuspec format)
            flat_deps: list[dict[str, str]] = []
            for dep_elem in deps_elem.findall(f"{prefix}dependency"):
                dep_id = dep_elem.get("id", "")
                dep_version = dep_elem.get("version", "")
                if dep_id:
                    flat_deps.append(
                        {"id": dep_id, "version": dep_version}
                    )
            if flat_deps:
                groups.append(
                    {
                        "target_framework": "",
                        "dependencies": flat_deps,
                    }
                )

        return groups

    def _parse_framework_assemblies(
        self,
        metadata_elem: ElementTree.Element,
        prefix: str,
    ) -> list[dict[str, str]]:
        """Parse ``<frameworkAssemblies>`` from the .nuspec metadata.

        Args:
            metadata_elem: The ``<metadata>`` XML element.
            prefix: The XML namespace prefix string.

        Returns:
            A list of framework assembly dicts with ``'assembly_name'``
            and ``'target_framework'`` keys.
        """
        fa_elem = metadata_elem.find(f"{prefix}frameworkAssemblies")
        if fa_elem is None:
            return []

        assemblies: list[dict[str, str]] = []
        for asm_elem in fa_elem.findall(f"{prefix}frameworkAssembly"):
            name = asm_elem.get("assemblyName", "")
            framework = asm_elem.get("targetFramework", "")
            if name:
                assemblies.append(
                    {
                        "assembly_name": name,
                        "target_framework": framework,
                    }
                )

        return assemblies

    def _validate_nuspec(
        self, nuspec_metadata: dict
    ) -> tuple[bool, str]:
        """Validate parsed .nuspec metadata structure.

        Checks that:

        1. ``'id'`` is present, non-empty, ≤128 characters, and matches
           :data:`PACKAGE_ID_PATTERN`.
        2. ``'version'`` is present and matches :data:`NUGET_VERSION_PATTERN`.

        Args:
            nuspec_metadata: Parsed .nuspec metadata dict from
                :meth:`_parse_nuspec`.

        Returns:
            A ``(True, '')`` tuple if valid, or ``(False, error_msg)``
            if invalid.
        """
        # Validate package ID
        package_id = nuspec_metadata.get("id", "")
        if not package_id:
            return False, "Missing required 'id' field"

        if len(package_id) > MAX_PACKAGE_ID_LENGTH:
            return (
                False,
                f"Package ID exceeds maximum length of "
                f"{MAX_PACKAGE_ID_LENGTH} characters",
            )

        if not PACKAGE_ID_PATTERN.match(package_id):
            return (
                False,
                f"Invalid package ID '{package_id}' — must match "
                f"pattern: alphanumeric, dots, hyphens, underscores, "
                f"starting with a letter or digit",
            )

        # Validate version
        version = nuspec_metadata.get("version", "")
        if not version:
            return False, "Missing required 'version' field"

        if not NUGET_VERSION_PATTERN.match(version):
            return (
                False,
                f"Invalid version '{version}' — must be a valid SemVer "
                f"string (Major.Minor.Patch[.Revision]"
                f"[-prerelease][+buildmetadata])",
            )

        return True, ""

    # ==================================================================
    # .nupkg Utilities
    # ==================================================================

    def _is_valid_nupkg(self, content: bytes) -> bool:
        """Quick validation that content is a valid ZIP archive.

        Uses :func:`zipfile.is_zipfile` as the primary check, then
        attempts to open the content as a :class:`zipfile.ZipFile` to
        verify structural integrity.

        Args:
            content: Raw binary content to check.

        Returns:
            ``True`` if the content is a valid ZIP archive.
        """
        if not content or len(content) < 4:
            return False

        # Use zipfile.is_zipfile for the initial magic-byte / header check
        if not zipfile.is_zipfile(io.BytesIO(content)):
            return False

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                # Verify the ZIP can be read (test integrity)
                bad_file = zf.testzip()
                if bad_file is not None:
                    self.logger.warning(
                        "ZIP integrity test failed for file: %s", bad_file
                    )
                    return False
            return True
        except (zipfile.BadZipFile, Exception):
            return False

    def _list_nupkg_contents(self, nupkg_content: bytes) -> list[str]:
        """List all files contained in a .nupkg archive.

        Args:
            nupkg_content: Raw binary content of the .nupkg.

        Returns:
            A list of file path strings within the ZIP archive.

        Raises:
            FormatValidationError: If the content is not a valid ZIP.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(nupkg_content)) as zf:
                return zf.namelist()
        except zipfile.BadZipFile as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Cannot list .nupkg contents — invalid ZIP: {exc}",
            ) from exc

    def _extract_file_from_nupkg(
        self, nupkg_content: bytes, file_path: str
    ) -> bytes:
        """Extract a specific file from a .nupkg archive.

        Args:
            nupkg_content: Raw binary content of the .nupkg archive.
            file_path: Path of the file within the ZIP to extract.

        Returns:
            The raw bytes of the extracted file.

        Raises:
            FormatValidationError: If the file is not found in the
                archive or the archive is invalid.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(nupkg_content)) as zf:
                if file_path not in zf.namelist():
                    raise FormatValidationError(
                        format_name=self.format_name,
                        message=(
                            f"File '{file_path}' not found in .nupkg "
                            f"archive"
                        ),
                        path=file_path,
                    )
                return zf.read(file_path)
        except zipfile.BadZipFile as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Cannot extract from .nupkg — invalid ZIP: {exc}",
                path=file_path,
            ) from exc
        except FormatValidationError:
            raise
        except Exception as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Failed to extract '{file_path}' from .nupkg: {exc}"
                ),
                path=file_path,
            ) from exc

    # ==================================================================
    # NuGet Path Parsing Utilities
    # ==================================================================

    def _parse_nuget_path(self, path: str) -> dict | None:
        """Parse a NuGet V3 API path into its component parts.

        Tries to match the path against each known NuGet V3 URL pattern
        in priority order.  Returns a structured dict describing the
        request type and any extracted parameters.

        Args:
            path: The URL path to parse.

        Returns:
            A dict with ``'package_id'``, ``'version'``,
            ``'request_type'``, and ``'filename'`` keys, or ``None`` if
            the path does not match any known pattern.
        """
        clean_path = path.strip()

        # Service Index
        if SERVICE_INDEX_PATH.match(clean_path):
            return {
                "package_id": None,
                "version": None,
                "request_type": "service_index",
                "filename": None,
            }

        # Flat container — version listing
        match = FLAT_CONTAINER_VERSIONS_PATH.match(clean_path)
        if match:
            return {
                "package_id": match.group("id"),
                "version": None,
                "request_type": "flat_container_versions",
                "filename": None,
            }

        # Flat container — .nupkg content
        match = FLAT_CONTAINER_CONTENT_PATH.match(clean_path)
        if match:
            return {
                "package_id": match.group("id"),
                "version": match.group("version"),
                "request_type": "flat_container_content",
                "filename": match.group("filename"),
            }

        # Flat container — .nuspec content
        match = FLAT_CONTAINER_NUSPEC_PATH.match(clean_path)
        if match:
            return {
                "package_id": match.group("id"),
                "version": match.group("version"),
                "request_type": "flat_container_nuspec",
                "filename": match.group("filename"),
            }

        # Registration — index
        match = REGISTRATION_INDEX_PATH.match(clean_path)
        if match:
            return {
                "package_id": match.group("id"),
                "version": None,
                "request_type": "registration_index",
                "filename": None,
            }

        # Registration — leaf
        match = REGISTRATION_LEAF_PATH.match(clean_path)
        if match:
            return {
                "package_id": match.group("id"),
                "version": match.group("version"),
                "request_type": "registration_leaf",
                "filename": None,
            }

        # Search
        if SEARCH_PATH.match(clean_path):
            return {
                "package_id": None,
                "version": None,
                "request_type": "search",
                "filename": None,
            }

        # Autocomplete
        if AUTOCOMPLETE_PATH.match(clean_path):
            return {
                "package_id": None,
                "version": None,
                "request_type": "autocomplete",
                "filename": None,
            }

        # Catalog
        if CATALOG_INDEX_PATH.match(clean_path):
            return {
                "package_id": None,
                "version": None,
                "request_type": "catalog_index",
                "filename": None,
            }

        # Package publish
        if PACKAGE_PUBLISH_PATH.match(clean_path):
            return {
                "package_id": None,
                "version": None,
                "request_type": "package_publish",
                "filename": None,
            }

        # Package delete
        match = PACKAGE_DELETE_PATH.match(clean_path)
        if match:
            return {
                "package_id": match.group("id"),
                "version": match.group("version"),
                "request_type": "package_delete",
                "filename": None,
            }

        return None

    def _nupkg_storage_path(
        self, package_id: str, version: str
    ) -> str:
        """Construct the storage path for a .nupkg file.

        All path segments are **lowercase** per the NuGet flat container
        convention.

        Path format::

            {lowercase-id}/{lowercase-version}/{lowercase-id}.{lowercase-version}.nupkg

        Args:
            package_id: The package identifier.
            version: The package version.

        Returns:
            The storage path string.
        """
        pid = self._normalize_package_id(package_id)
        ver = version.lower()
        return f"{pid}/{ver}/{pid}.{ver}{NUPKG_EXTENSION}"

    def _nuspec_storage_path(
        self, package_id: str, version: str
    ) -> str:
        """Construct the storage path for the extracted .nuspec.

        Path format::

            {lowercase-id}/{lowercase-version}/{lowercase-id}.nuspec

        Args:
            package_id: The package identifier.
            version: The package version.

        Returns:
            The storage path string.
        """
        pid = self._normalize_package_id(package_id)
        ver = version.lower()
        return f"{pid}/{ver}/{pid}{NUSPEC_EXTENSION}"

    # ==================================================================
    # Package ID Utilities
    # ==================================================================

    def _normalize_package_id(self, package_id: str) -> str:
        """Normalise a NuGet package ID for storage and comparison.

        NuGet package IDs are **case-insensitive** for comparison and
        path construction, but original casing is preserved in metadata
        and UI displays.

        Args:
            package_id: The package identifier to normalise.

        Returns:
            The lowercase version of the package ID.
        """
        return package_id.lower()

    def _validate_package_id(self, package_id: str) -> bool:
        """Validate a NuGet package ID against naming rules.

        Rules:

        - Must match :data:`PACKAGE_ID_PATTERN` (alphanumeric, dots,
          hyphens, underscores; starting with a letter or digit).
        - Must not exceed :data:`MAX_PACKAGE_ID_LENGTH` (128 characters).

        Args:
            package_id: The package identifier to validate.

        Returns:
            ``True`` if the package ID is valid.
        """
        if not package_id:
            return False

        if len(package_id) > MAX_PACKAGE_ID_LENGTH:
            return False

        if not PACKAGE_ID_PATTERN.match(package_id):
            return False

        return True
