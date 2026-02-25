"""
Maven repository format handler.

Implements the Maven2 repository format (F-101-RQ-001), the most feature-rich
format handler. Supports Maven2 repository layout conventions, POM XML parsing,
coordinate resolution, and SNAPSHOT version handling.

The format_name is 'maven2' (NOT 'maven') to match the original Nexus Repository
Java implementation and the Repository model format enum.

**Replaces:** The Java Maven format bundle OSGi plugin from the source system.

This module provides:

- :class:`MavenFormatHandler` — concrete ``FormatHandler`` subclass for Maven2
- Constants for path patterns, content types, and checksum extensions
- POM XML validation and coordinate extraction
- Flask Blueprint registration for Maven repository endpoints
- SNAPSHOT versioning support (``is_prerelease`` detection)
- Checksum sidecar file handling (``.md5``, ``.sha1``, ``.sha256``, ``.sha512``)
- Maven metadata.xml generation and merging via the ``metadata.py`` module
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import TYPE_CHECKING
from xml.etree import ElementTree
from xml.etree.ElementTree import ParseError as XMLParseError

from flask import Blueprint, Response, abort, jsonify, request, send_file

from src.app.formats.base import (
    ArtifactNotFoundError,
    FormatHandler,
    FormatValidationError,
)
from src.app.formats.maven.metadata import (
    generate_artifact_metadata,
    generate_checksum_content,
    generate_group_metadata,
    generate_snapshot_metadata,
    merge_metadata as _merge_metadata_impl,
)

if TYPE_CHECKING:
    from src.app.models.asset import Asset
    from src.app.models.component import Component
    from src.app.models.repository import Repository

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source.  Provides module-level diagnostics for the Maven format handler.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "MavenFormatHandler",
    "MAVEN_FORMAT_NAME",
    "MAVEN_CONTENT_TYPES",
    "CHECKSUM_EXTENSIONS",
    "SNAPSHOT_VERSION_PATTERN",
    "MAVEN_ARTIFACT_PATH_PATTERN",
    "MAVEN_METADATA_FILENAME",
    "POM_EXTENSION",
    "TIMESTAMPED_SNAPSHOT_PATTERN",
]

# ===========================================================================
# Constants and Regex Patterns
# ===========================================================================

# CRITICAL: format_name must be 'maven2' not 'maven'
MAVEN_FORMAT_NAME: str = "maven2"
"""Canonical format name matching the ``Repository.format`` column enum."""

# Maven2 repository layout path pattern
# Format: /{groupId-with-slashes}/{artifactId}/{version}/{filename}
# Example: /org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar
MAVEN_ARTIFACT_PATH_PATTERN: re.Pattern[str] = re.compile(
    r"^/?(?P<group_path>.+)/(?P<artifact_id>[^/]+)"
    r"/(?P<version>[^/]+)/(?P<filename>[^/]+)$"
)

# Filename pattern for Maven artifacts
# {artifactId}-{version}[-{classifier}].{extension}
MAVEN_FILENAME_PATTERN: re.Pattern[str] = re.compile(
    r"^(?P<artifact_id>[^-]+(?:-[^-]+)*?)-"
    r"(?P<version>\d+\.\d+[\w.\-]*?)"
    r"(?:-(?P<classifier>[^.]+))?"
    r"\.(?P<extension>.+)$"
)

# Metadata file pattern
MAVEN_METADATA_FILENAME: str = "maven-metadata.xml"
MAVEN_METADATA_PATH_PATTERN: re.Pattern[str] = re.compile(
    r"^(?P<base_path>.+)/maven-metadata\.xml(?P<checksum_ext>\.[a-z0-9]+)?$"
)

# Checksum sidecar file extensions
CHECKSUM_EXTENSIONS: frozenset[str] = frozenset(
    {".md5", ".sha1", ".sha256", ".sha512"}
)

# SNAPSHOT version pattern
SNAPSHOT_VERSION_PATTERN: re.Pattern[str] = re.compile(
    r"-SNAPSHOT$", re.IGNORECASE
)

# Timestamped SNAPSHOT pattern (e.g., 1.0-20240115.103000-1)
TIMESTAMPED_SNAPSHOT_PATTERN: re.Pattern[str] = re.compile(
    r"^(?P<base_version>.+)-(?P<timestamp>\d{8}\.\d{6})"
    r"-(?P<build_number>\d+)$"
)

# POM file extension
POM_EXTENSION: str = ".pom"

# Maven content types
MAVEN_CONTENT_TYPES: list[str] = [
    "application/java-archive",      # .jar
    "application/xml",               # .pom, .xml
    "application/x-maven-pom+xml",   # POM files
    "application/octet-stream",      # generic binary
    "text/xml",                      # XML metadata
]

# Maven XML namespace used in POM files
_MAVEN_POM_NS: str = "http://maven.apache.org/POM/4.0.0"

# Extension-to-content-type mapping for Maven-specific types
_MAVEN_CONTENT_TYPE_MAP: dict[str, str] = {
    ".pom": "application/xml",
    ".jar": "application/java-archive",
    ".war": "application/java-archive",
    ".ear": "application/java-archive",
    ".xml": "application/xml",
    ".md5": "text/plain",
    ".sha1": "text/plain",
    ".sha256": "text/plain",
    ".sha512": "text/plain",
    ".aar": "application/java-archive",
    ".nbm": "application/java-archive",
    ".tar.gz": "application/gzip",
    ".zip": "application/zip",
    ".asc": "text/plain",
}


# ===========================================================================
# MavenFormatHandler Class
# ===========================================================================


class MavenFormatHandler(FormatHandler):
    """Maven2 repository format handler implementing F-101-RQ-001.

    This is the primary and most complex format handler in the Nexus Repository
    system.  It implements the full Maven2 repository layout protocol:

    - **Artifact upload/download** via Maven2 path conventions
    - **Maven coordinate extraction** (groupId, artifactId, version, classifier,
      extension) from repository path structure
    - **POM XML validation** (well-formedness + required Maven coordinate
      elements, including parent POM inheritance)
    - **Flask Blueprint registration** for Maven repository endpoints (GET,
      PUT, HEAD, DELETE)
    - **SNAPSHOT versioning support** (``is_prerelease`` detection for
      ``-SNAPSHOT`` suffixed versions)
    - **Checksum sidecar file handling** (``.md5``, ``.sha1``, ``.sha256``,
      ``.sha512``)
    - **Maven metadata.xml generation and merging** via the ``metadata.py``
      module for group repository support

    **Replaces:** The Java Maven format bundle OSGi plugin from the source
    system (OSGi/Karaf 4.4.4 module container).

    **CRITICAL:** ``format_name`` is ``'maven2'`` (NOT ``'maven'``), matching
    the original Java implementation and the ``Repository`` model format enum.

    Attributes:
        format_name: ``'maven2'`` — canonical format identifier.
        content_types: MIME types this handler recognises.
        path_pattern: Regex matching valid Maven artifact paths.
    """

    # ------------------------------------------------------------------
    # Class-Level Attributes
    # ------------------------------------------------------------------

    format_name: str = MAVEN_FORMAT_NAME
    """``'maven2'`` — MUST match the ``Repository`` model format enum."""

    content_types: list[str] = MAVEN_CONTENT_TYPES

    path_pattern: str = r"^/?(?:.+)/(?:[^/]+)/(?:[^/]+)/(?:[^/]+)$"
    """Regex pattern for valid Maven2 artifact paths."""

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the Maven format handler.

        Calls the parent ``FormatHandler.__init__()`` which validates that
        ``format_name`` is set and creates a format-specific logger.  Then
        creates a Maven-specific child logger for fine-grained diagnostics.
        """
        super().__init__()
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.MavenFormatHandler"
        )

    # ==================================================================
    # Flask Blueprint Registration
    # ==================================================================

    @classmethod
    def get_blueprint(cls) -> Blueprint:
        """Return a Flask Blueprint with Maven-specific route handlers.

        Registers route handlers for:

        - ``GET /<path:repo_path>`` — artifact downloads and metadata requests
        - ``PUT /<path:repo_path>`` — artifact uploads (hosted repositories)
        - ``HEAD /<path:repo_path>`` — artifact existence checks
        - ``DELETE /<path:repo_path>`` — artifact deletion (hosted repositories)

        The blueprint is designed for registration via the format registry
        in ``src.app.formats.__init__`` and ultimately in ``factory.py``
        through ``create_app()``.

        Returns:
            A fully configured Flask Blueprint ready for registration.
        """
        bp = Blueprint("maven", __name__)

        @bp.route("/<path:repo_path>", methods=["GET"])
        def maven_download(repo_path: str) -> Response:
            """Handle Maven artifact download or metadata request."""
            handler = cls()
            normalized = handler.normalize_path(repo_path)

            # Check for metadata requests
            if handler.is_metadata_path(normalized):
                metadata_result = handler.generate_metadata(
                    repository_name="",
                    path=normalized,
                )
                if metadata_result is not None:
                    xml_bytes, content_type = metadata_result
                    return Response(
                        xml_bytes,
                        content_type=content_type,
                        headers={"Content-Length": str(len(xml_bytes))},
                    )

            # Check for checksum sidecar requests
            if handler.is_checksum_path(normalized):
                checksum_type = handler.get_checksum_type(normalized)
                base_path = handler.get_base_artifact_path(normalized)
                handler.logger.debug(
                    "Checksum request: type=%s base=%s",
                    checksum_type,
                    base_path,
                )
                # Delegate to service layer for actual content retrieval
                abort(404)

            # Regular artifact download — delegate to service layer
            content_type = handler.detect_content_type(normalized)
            handler.logger.debug(
                "Download request for path=%s content_type=%s",
                normalized,
                content_type,
            )
            # The actual BlobStore retrieval is handled by the service layer
            abort(404)

        @bp.route("/<path:repo_path>", methods=["PUT"])
        def maven_upload(repo_path: str) -> Response:
            """Handle Maven artifact upload to a hosted repository."""
            handler = cls()
            content = request.data
            content_type_header = (
                request.content_type or "application/octet-stream"
            )

            try:
                result = handler.handle_upload(
                    repository_name="",
                    path=repo_path,
                    content=content,
                    content_type=content_type_header,
                )
                return jsonify(result), 201
            except FormatValidationError as exc:
                handler.logger.warning(
                    "Upload validation failed: %s", exc.message
                )
                abort(400, description=str(exc))

        @bp.route("/<path:repo_path>", methods=["HEAD"])
        def maven_head(repo_path: str) -> Response:
            """Check if a Maven artifact exists."""
            handler = cls()
            normalized = handler.normalize_path(repo_path)
            content_type = handler.detect_content_type(normalized)
            handler.logger.debug(
                "HEAD request for path=%s", normalized
            )
            # Delegate to service layer for existence check
            abort(404)

        @bp.route("/<path:repo_path>", methods=["DELETE"])
        def maven_delete(repo_path: str) -> Response:
            """Delete a Maven artifact from a hosted repository."""
            handler = cls()
            normalized = handler.normalize_path(repo_path)
            handler.logger.debug(
                "DELETE request for path=%s", normalized
            )
            # Delegate to service layer for deletion
            abort(404)

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
        """Handle a Maven artifact upload.

        Processes the incoming content, validates format-specific constraints
        (POM XML well-formedness for ``.pom`` files), extracts Maven
        coordinates, computes checksums, and returns metadata about the
        created component and asset.

        Args:
            repository_name: Name of the target hosted repository.
            path: Storage path within the repository.
            content: Raw binary content of the artifact being uploaded.
            content_type: MIME type of the uploaded content.
            attributes: Optional dictionary of additional metadata.

        Returns:
            Dictionary containing component and asset metadata with keys:
            ``namespace``, ``name``, ``version``, ``classifier``,
            ``extension``, ``path``, ``content_type``, ``size``,
            ``checksums``, ``is_snapshot``.

        Raises:
            FormatValidationError: If POM validation fails.
        """
        normalized_path = self.normalize_path(path)
        self.logger.info(
            "Processing upload: repo=%s path=%s size=%d",
            repository_name,
            normalized_path,
            len(content),
        )

        # Handle checksum sidecar uploads
        if self.is_checksum_path(normalized_path):
            checksum_type = self.get_checksum_type(normalized_path)
            base_path = self.get_base_artifact_path(normalized_path)
            self.logger.debug(
                "Checksum sidecar upload: type=%s base_path=%s",
                checksum_type,
                base_path,
            )
            return {
                "path": normalized_path,
                "content_type": "text/plain",
                "size": len(content),
                "checksums": {},
                "is_checksum_sidecar": True,
                "checksum_type": checksum_type,
                "base_artifact_path": base_path,
            }

        # Extract Maven coordinates from path (and POM content if applicable)
        coordinates = self.extract_coordinates(normalized_path, content)

        # Validate POM XML content if this is a POM file
        _, ext = os.path.splitext(normalized_path)
        if ext.lower() == POM_EXTENSION:
            self.validate_content(normalized_path, content)
            self.logger.debug(
                "POM validation passed for path=%s", normalized_path
            )

        # Compute checksums for the uploaded content
        checksums = self.compute_checksums(content)

        # Determine the effective content type
        effective_content_type = content_type or self.detect_content_type(
            normalized_path
        )

        version = coordinates.get("version") or ""

        result: dict = {
            "namespace": coordinates.get("namespace"),
            "name": coordinates.get("name"),
            "version": version,
            "classifier": coordinates.get("classifier"),
            "extension": coordinates.get("extension"),
            "path": normalized_path,
            "content_type": effective_content_type,
            "size": len(content),
            "checksums": checksums,
            "is_snapshot": self.is_prerelease(version),
        }

        if attributes:
            result["attributes"] = attributes

        self.logger.info(
            "Upload processed: repo=%s namespace=%s name=%s version=%s",
            repository_name,
            result["namespace"],
            result["name"],
            result["version"],
        )
        return result

    def handle_download(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str, dict]:
        """Handle a Maven artifact download request.

        Checks if the requested path is a metadata endpoint or a checksum
        sidecar, and delegates accordingly.  For regular artifact paths, the
        actual BlobStore retrieval is expected to be handled by the service
        layer.

        Args:
            repository_name: Name of the repository to download from.
            path: Artifact path within the repository.

        Returns:
            A three-element tuple of ``(content_bytes, content_type, headers)``.

        Raises:
            ArtifactNotFoundError: If the artifact cannot be found.
        """
        normalized_path = self.normalize_path(path)
        self.logger.debug(
            "Download request: repo=%s path=%s",
            repository_name,
            normalized_path,
        )

        # Handle metadata requests
        if self.is_metadata_path(normalized_path):
            metadata_result = self.generate_metadata(
                repository_name, normalized_path
            )
            if metadata_result is not None:
                xml_bytes, ct = metadata_result
                return (
                    xml_bytes,
                    ct,
                    {"Content-Length": str(len(xml_bytes))},
                )

        # Handle checksum sidecar requests
        if self.is_checksum_path(normalized_path):
            checksum_type = self.get_checksum_type(normalized_path)
            base_path = self.get_base_artifact_path(normalized_path)
            self.logger.debug(
                "Checksum download: type=%s base=%s",
                checksum_type,
                base_path,
            )
            # The service layer should retrieve the stored checksum or
            # compute it from the base artifact.  Raise not found as a
            # fallback to let the service layer handle it.
            raise ArtifactNotFoundError(repository_name, normalized_path)

        # For regular artifacts, the service layer handles BlobStore retrieval.
        # This method provides the format-level processing; the actual I/O
        # is orchestrated by the repository service.
        content_type = self.detect_content_type(normalized_path)
        self.logger.debug(
            "Artifact download: repo=%s path=%s content_type=%s",
            repository_name,
            normalized_path,
            content_type,
        )
        raise ArtifactNotFoundError(repository_name, normalized_path)

    def extract_coordinates(
        self,
        path: str,
        content: bytes | None = None,
    ) -> dict:
        """Extract Maven coordinates from an artifact path.

        Parses the Maven2 repository layout path to determine the component's
        logical identity.  Maven paths follow the convention::

            /{groupId-with-slashes}/{artifactId}/{version}/{filename}

        Where:

        - ``groupId`` is encoded as directory separators
          (e.g., ``org/apache/commons`` -> ``org.apache.commons``)
        - ``artifactId`` is the directory before the version
        - ``version`` is the directory before the filename
        - ``filename`` follows ``{artifactId}-{version}[-{classifier}].{ext}``

        Args:
            path: Artifact path within the repository.
            content: Optional raw content (used for POM coordinate extraction).

        Returns:
            Dictionary with keys: ``namespace`` (groupId), ``name``
            (artifactId), ``version``, ``classifier``, ``extension``,
            ``group_path``.

        Raises:
            FormatValidationError: If the path does not conform to Maven2
                layout conventions.
        """
        normalized = self.normalize_path(path)
        self.logger.debug("Extracting coordinates from path: %s", normalized)

        # Handle checksum sidecar paths — strip the checksum extension
        effective_path = normalized
        if self.is_checksum_path(normalized):
            effective_path = self.get_base_artifact_path(normalized)

        # Handle metadata paths
        if self.is_metadata_path(effective_path):
            return self._extract_metadata_coordinates(effective_path)

        # Parse the artifact path
        parsed = self._parse_maven_path(effective_path)
        if parsed is None:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    "Path does not conform to Maven2 repository layout: "
                    "expected /{groupId}/{artifactId}/{version}/{filename}"
                ),
                path=normalized,
            )

        group_path = parsed["group_path"]
        group_id = self._group_path_to_group_id(group_path)
        artifact_id = parsed["artifact_id"]
        version = parsed["version"]
        classifier = parsed.get("classifier")
        extension = parsed.get("extension", "jar")

        # If content is provided and this is a POM file, also extract from XML
        if content and effective_path.endswith(POM_EXTENSION):
            try:
                pom_coords = self.parse_pom_coordinates(content)
                self.logger.debug(
                    "POM coordinates extracted: %s", pom_coords
                )
            except (FormatValidationError, XMLParseError):
                # Path-based coordinates take precedence; POM parsing
                # is supplementary
                self.logger.debug(
                    "POM coordinate extraction failed for %s; "
                    "using path-based coordinates",
                    effective_path,
                )

        return {
            "namespace": group_id,
            "name": artifact_id,
            "version": version,
            "classifier": classifier,
            "extension": extension,
            "group_path": group_path,
        }

    def generate_metadata(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str] | None:
        """Generate maven-metadata.xml for a given path.

        Determines the metadata level from the path structure and delegates
        to the appropriate function from ``metadata.py``:

        - **Group level**: ``/{groupId-path}/maven-metadata.xml``
          calls ``generate_group_metadata()``
        - **Artifact level**: ``/{groupId-path}/{artifactId}/maven-metadata.xml``
          calls ``generate_artifact_metadata()``
        - **Version level** (SNAPSHOT):
          ``/{groupId-path}/{artifactId}/{version}/maven-metadata.xml``
          calls ``generate_snapshot_metadata()``

        Args:
            repository_name: Name of the repository.
            path: Requested metadata path.

        Returns:
            Tuple of ``(xml_bytes, 'application/xml')`` or ``None`` if the
            path is not a metadata endpoint.
        """
        normalized = self.normalize_path(path)
        if not self.is_metadata_path(normalized):
            return None

        self.logger.debug(
            "Generating metadata: repo=%s path=%s",
            repository_name,
            normalized,
        )

        # Strip the maven-metadata.xml filename to get the base path
        base_path = normalized
        if base_path.endswith("/" + MAVEN_METADATA_FILENAME):
            base_path = base_path[: -(len(MAVEN_METADATA_FILENAME) + 1)]
        elif base_path == MAVEN_METADATA_FILENAME:
            base_path = ""

        parts = [p for p in base_path.split("/") if p]

        if len(parts) == 0:
            # Root metadata — unusual, return None
            return None

        # Determine metadata level by the number of path segments.
        # In Maven2 layout:
        #   - >=3 parts and last looks like a version -> version-level
        #   - >=2 parts -> could be artifact-level
        #   - otherwise -> group-level
        if len(parts) >= 3 and self._looks_like_version(parts[-1]):
            # Version-level metadata (SNAPSHOT only is meaningful)
            version = parts[-1]
            artifact_id = parts[-2]
            group_path = "/".join(parts[:-2])
            group_id = self._group_path_to_group_id(group_path)

            if self.is_prerelease(version):
                xml_bytes = generate_snapshot_metadata(
                    group_id=group_id,
                    artifact_id=artifact_id,
                    version=version,
                )
            else:
                # Non-SNAPSHOT version-level metadata is not standard,
                # but we generate a minimal artifact-level metadata
                xml_bytes = generate_artifact_metadata(
                    group_id=group_id,
                    artifact_id=artifact_id,
                    versions=[version],
                )
            return (xml_bytes, "application/xml")

        if len(parts) >= 2:
            # Artifact-level metadata
            artifact_id = parts[-1]
            group_path = "/".join(parts[:-1])
            group_id = self._group_path_to_group_id(group_path)

            # In a real deployment the service layer would query the
            # database for all versions.  Here we generate a placeholder
            # that the service layer will populate.
            xml_bytes = generate_artifact_metadata(
                group_id=group_id,
                artifact_id=artifact_id,
                versions=[],
            )
            return (xml_bytes, "application/xml")

        # Group-level metadata (single path segment = groupId part)
        group_id = self._group_path_to_group_id("/".join(parts))
        xml_bytes = generate_group_metadata(
            group_id=group_id,
            artifact_ids=[],
        )
        return (xml_bytes, "application/xml")

    def validate_path(self, path: str) -> bool:
        """Validate that a path conforms to Maven2 repository layout.

        Checks against the Maven artifact path pattern, the metadata path
        pattern, and allows checksum sidecar paths.

        Args:
            path: Artifact path to validate.

        Returns:
            ``True`` if the path is valid for Maven2, ``False`` otherwise.
        """
        normalized = self.normalize_path(path)
        if not normalized:
            return False

        # Metadata paths are always valid
        if self.is_metadata_path(normalized):
            return True

        # Checksum sidecar paths — validate the base path
        if self.is_checksum_path(normalized):
            base_path = self.get_base_artifact_path(normalized)
            # Recursively validate the base path
            if self.is_metadata_path(base_path):
                return True
            return self._parse_maven_path(base_path) is not None

        # Standard artifact path validation
        return self._parse_maven_path(normalized) is not None

    def validate_content(self, path: str, content: bytes) -> bool:
        """Validate artifact content against Maven format conventions.

        For POM files (``.pom`` extension): validates XML well-formedness and
        checks for required Maven coordinate elements (``<groupId>``,
        ``<artifactId>``, ``<version>``).  These may be present directly or
        inherited from a ``<parent>`` element.

        For non-POM files: returns ``True`` (no content validation needed).

        Args:
            path: Artifact path (provides context for validation).
            content: Raw binary content to validate.

        Returns:
            ``True`` if the content passes validation.

        Raises:
            FormatValidationError: If POM validation fails.
        """
        _, ext = os.path.splitext(path)
        if ext.lower() != POM_EXTENSION:
            return True

        is_valid, error_message = self._validate_pom_xml(content)
        if not is_valid:
            self.logger.warning(
                "POM validation failed for %s: %s", path, error_message
            )
            raise FormatValidationError(
                format_name=self.format_name,
                message=error_message,
                path=path,
            )

        self.logger.debug("POM content validated successfully: %s", path)
        return True

    # ==================================================================
    # Overridden Concrete Methods
    # ==================================================================

    def is_prerelease(self, component_version: str) -> bool:
        """Determine whether a Maven version is a pre-release (SNAPSHOT).

        Returns ``True`` if the version string ends with ``-SNAPSHOT``
        (case-insensitive).  Used by ``cleanup_service.py`` for Maven-specific
        pre-release detection (F-204).

        Args:
            component_version: The version string to evaluate.

        Returns:
            ``True`` if the version ends with ``-SNAPSHOT``.
        """
        if not component_version:
            return False
        return bool(SNAPSHOT_VERSION_PATTERN.search(component_version))

    def merge_metadata(self, metadata_list: list[bytes]) -> bytes | None:
        """Merge maven-metadata.xml from multiple group repository members.

        Delegates to ``merge_metadata()`` from ``metadata.py`` which handles
        artifact-level version list merging, SNAPSHOT timestamp resolution,
        and group-level artifact ID list merging.

        Args:
            metadata_list: List of raw ``maven-metadata.xml`` byte strings
                from member repositories.

        Returns:
            Merged XML as bytes, or ``None`` if the input is empty.
        """
        if not metadata_list:
            self.logger.debug("No metadata to merge (empty list)")
            return None

        self.logger.debug(
            "Merging metadata from %d member(s)", len(metadata_list)
        )
        return _merge_metadata_impl(metadata_list)

    def detect_content_type(self, path: str) -> str:
        """Detect MIME type for a Maven artifact based on file extension.

        Overrides the base class to provide Maven-specific content type
        mappings for common Maven artifact extensions.

        Args:
            path: Artifact path whose content type should be determined.

        Returns:
            A MIME type string.
        """
        _, ext = os.path.splitext(path)
        ext_lower = ext.lower()

        if ext_lower in _MAVEN_CONTENT_TYPE_MAP:
            return _MAVEN_CONTENT_TYPE_MAP[ext_lower]

        # Fall back to base class detection
        return super().detect_content_type(path)

    # ==================================================================
    # Maven-Specific Public Methods
    # ==================================================================

    def is_checksum_path(self, path: str) -> bool:
        """Check if the path is a checksum sidecar file.

        Returns ``True`` if the path ends with any of the checksum extensions
        (``.md5``, ``.sha1``, ``.sha256``, ``.sha512``).

        Args:
            path: Path to check.

        Returns:
            ``True`` if the path is a checksum sidecar.
        """
        _, ext = os.path.splitext(path)
        return ext.lower() in CHECKSUM_EXTENSIONS

    def is_metadata_path(self, path: str) -> bool:
        """Check if the path is a maven-metadata.xml request.

        Returns ``True`` if the path ends with ``maven-metadata.xml`` or
        a checksum variant thereof (e.g., ``maven-metadata.xml.sha1``).

        Args:
            path: Path to check.

        Returns:
            ``True`` if the path requests Maven metadata.
        """
        normalized = path.rstrip("/")
        # Direct metadata path
        if normalized.endswith("/" + MAVEN_METADATA_FILENAME):
            return True
        if normalized == MAVEN_METADATA_FILENAME:
            return True
        # Metadata checksum path (e.g., maven-metadata.xml.sha1)
        if self.is_checksum_path(normalized):
            base = self.get_base_artifact_path(normalized)
            if base.endswith("/" + MAVEN_METADATA_FILENAME):
                return True
            if base == MAVEN_METADATA_FILENAME:
                return True
        return False

    def get_checksum_type(self, path: str) -> str | None:
        """Extract the checksum algorithm name from a sidecar file path.

        Args:
            path: Checksum sidecar path (e.g., ``foo.jar.sha1``).

        Returns:
            Algorithm name (``'md5'``, ``'sha1'``, ``'sha256'``,
            ``'sha512'``) or ``None`` if not a checksum path.
        """
        _, ext = os.path.splitext(path)
        ext_lower = ext.lower()
        # Map extension to algorithm name (strip the leading dot)
        if ext_lower in CHECKSUM_EXTENSIONS:
            return ext_lower[1:]  # '.sha1' -> 'sha1'
        return None

    def get_base_artifact_path(self, checksum_path: str) -> str:
        """Return the base artifact path from a checksum sidecar path.

        Strips the checksum extension to recover the original artifact path.

        Args:
            checksum_path: Path ending in a checksum extension.

        Returns:
            The base artifact path without the checksum suffix.

        Example::

            handler.get_base_artifact_path('/org/example/foo-1.0.jar.sha1')
            # => '/org/example/foo-1.0.jar'
        """
        base, ext = os.path.splitext(checksum_path)
        if ext.lower() in CHECKSUM_EXTENSIONS:
            return base
        return checksum_path

    def parse_pom_coordinates(self, pom_content: bytes) -> dict:
        """Parse Maven coordinates from POM XML content.

        Extracts ``<groupId>``, ``<artifactId>``, and ``<version>`` from
        the POM XML, including parent POM inheritance.  The ``artifactId``
        MUST be present directly (not inherited); ``groupId`` and ``version``
        may be inherited from the ``<parent>`` element.

        Args:
            pom_content: Raw POM XML bytes.

        Returns:
            Dictionary with ``namespace`` (groupId), ``name`` (artifactId),
            and ``version``.

        Raises:
            FormatValidationError: If mandatory elements are missing.
        """
        try:
            root = ElementTree.fromstring(pom_content)
        except XMLParseError as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"POM XML is not well-formed: {exc}",
            ) from exc

        # Determine the namespace prefix (POM 4.0 may use a namespace)
        ns = ""
        root_tag = root.tag
        if root_tag.startswith("{"):
            ns_end = root_tag.index("}")
            ns = root_tag[1:ns_end]

        def _find(parent: ElementTree.Element, tag: str) -> str | None:
            """Find text in a direct child element, with or without NS."""
            if ns:
                elem = parent.find(f"{{{ns}}}{tag}")
            else:
                elem = parent.find(tag)
            if elem is not None and elem.text:
                return elem.text.strip()
            return None

        # Extract from root level
        group_id = _find(root, "groupId")
        artifact_id = _find(root, "artifactId")
        version = _find(root, "version")

        # Check parent element for inherited coordinates
        parent_elem = root.find(f"{{{ns}}}parent" if ns else "parent")
        if parent_elem is not None:
            if group_id is None:
                group_id = _find(parent_elem, "groupId")
            if version is None:
                version = _find(parent_elem, "version")

        # Validate mandatory fields
        if artifact_id is None:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    "POM is missing required <artifactId> element "
                    "(cannot be inherited from parent)"
                ),
            )

        if group_id is None:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    "POM is missing <groupId> — not found in project or "
                    "parent element"
                ),
            )

        if version is None:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    "POM is missing <version> — not found in project or "
                    "parent element"
                ),
            )

        self.logger.debug(
            "POM coordinates: groupId=%s artifactId=%s version=%s",
            group_id,
            artifact_id,
            version,
        )
        return {
            "namespace": group_id,
            "name": artifact_id,
            "version": version,
        }

    # ==================================================================
    # Private Helper Methods
    # ==================================================================

    def _validate_pom_xml(self, content: bytes) -> tuple[bool, str]:
        """Validate a POM XML document.

        Checks:

        1. XML is well-formed (parseable by ElementTree).
        2. Root element is ``<project>`` (with or without Maven namespace).
        3. ``<groupId>`` is present (directly or via ``<parent>``).
        4. ``<artifactId>`` is present (MUST be direct, not inherited).
        5. ``<version>`` is present (directly or via ``<parent>``).

        Args:
            content: Raw POM XML bytes.

        Returns:
            Tuple of ``(True, '')`` if valid, or ``(False, error_message)``
            if invalid.
        """
        if not content or not content.strip():
            return (False, "POM content is empty")

        try:
            root = ElementTree.fromstring(content)
        except XMLParseError as exc:
            return (False, f"POM XML is not well-formed: {exc}")

        # Check root element is <project>
        root_tag = root.tag
        local_name = root_tag
        ns = ""
        if root_tag.startswith("{"):
            ns_end = root_tag.index("}")
            ns = root_tag[1:ns_end]
            local_name = root_tag[ns_end + 1:]

        if local_name != "project":
            return (
                False,
                f"POM root element must be <project>, found <{local_name}>",
            )

        def _find(parent: ElementTree.Element, tag: str) -> str | None:
            if ns:
                elem = parent.find(f"{{{ns}}}{tag}")
            else:
                elem = parent.find(tag)
            if elem is not None and elem.text:
                return elem.text.strip()
            return None

        # Extract coordinates
        group_id = _find(root, "groupId")
        artifact_id = _find(root, "artifactId")
        version = _find(root, "version")

        # Check parent for inherited values
        parent_elem = root.find(f"{{{ns}}}parent" if ns else "parent")
        parent_group_id = None
        parent_version = None
        if parent_elem is not None:
            parent_group_id = _find(parent_elem, "groupId")
            parent_version = _find(parent_elem, "version")

        # Validate required fields
        if artifact_id is None:
            return (
                False,
                "POM is missing required <artifactId> element",
            )

        effective_group_id = group_id or parent_group_id
        if effective_group_id is None:
            return (
                False,
                "POM is missing <groupId> — not found in project or "
                "parent element",
            )

        effective_version = version or parent_version
        if effective_version is None:
            return (
                False,
                "POM is missing <version> — not found in project or "
                "parent element",
            )

        return (True, "")

    def _parse_maven_path(self, path: str) -> dict | None:
        """Parse a Maven2 repository layout path into components.

        The path structure is::

            {group_path}/{artifact_id}/{version}/{filename}

        Where ``group_path`` may contain multiple ``/`` segments.

        Args:
            path: Normalised artifact path (no leading/trailing slashes).

        Returns:
            Dictionary with ``group_path``, ``artifact_id``, ``version``,
            ``filename``, ``classifier``, ``extension``; or ``None``
            if the path is invalid.
        """
        # Strip leading/trailing slashes
        cleaned = path.strip("/")
        if not cleaned:
            return None

        segments = cleaned.split("/")

        # Minimum segments: groupId(1+) + artifactId + version + filename = 4
        if len(segments) < 4:
            return None

        filename = segments[-1]
        version = segments[-2]
        artifact_id = segments[-3]
        group_path = "/".join(segments[:-3])

        if not group_path or not artifact_id or not version or not filename:
            return None

        # Parse filename for classifier and extension
        file_parts = self._parse_maven_filename(
            filename, artifact_id, version
        )

        return {
            "group_path": group_path,
            "artifact_id": artifact_id,
            "version": version,
            "filename": filename,
            "classifier": file_parts.get("classifier"),
            "extension": file_parts.get("extension", "jar"),
        }

    def _parse_maven_filename(
        self,
        filename: str,
        artifact_id: str,
        version: str,
    ) -> dict:
        """Parse a Maven artifact filename to extract classifier and extension.

        The filename follows the pattern::

            {artifactId}-{version}[-{classifier}].{extension}

        Handles edge cases including SNAPSHOT versions, timestamped snapshots,
        multi-hyphen artifact IDs, and compound extensions.

        Args:
            filename: The filename segment.
            artifact_id: Expected artifactId from path structure.
            version: Expected version from path structure.

        Returns:
            Dictionary with ``classifier`` (str | None) and ``extension`` (str).
        """
        # Build the expected prefix: {artifactId}-{version}
        prefix = f"{artifact_id}-{version}"

        if filename.startswith(prefix):
            remainder = filename[len(prefix):]

            if remainder.startswith("."):
                # No classifier: {artifactId}-{version}.{ext}
                return {
                    "classifier": None,
                    "extension": remainder[1:],  # strip leading dot
                }
            elif remainder.startswith("-"):
                # Has classifier: {artifactId}-{version}-{classifier}.{ext}
                rest = remainder[1:]  # strip leading hyphen
                dot_pos = rest.find(".")
                if dot_pos > 0:
                    return {
                        "classifier": rest[:dot_pos],
                        "extension": rest[dot_pos + 1:],
                    }
                else:
                    # No extension found — treat entire rest as classifier
                    return {"classifier": rest, "extension": ""}

        # Handle timestamped SNAPSHOT filenames
        # e.g., commons-lang3-1.0-20240115.103000-1.jar
        if SNAPSHOT_VERSION_PATTERN.search(version):
            base_version = SNAPSHOT_VERSION_PATTERN.sub("", version)
            ts_prefix = f"{artifact_id}-{base_version}-"
            if filename.startswith(ts_prefix):
                rest = filename[len(ts_prefix):]
                # Try to match timestamped pattern in the rest
                ts_match = re.match(
                    r"^(\d{8}\.\d{6}-\d+)(?:-([^.]+))?\.(.+)$", rest
                )
                if ts_match:
                    classifier = ts_match.group(2)
                    extension = ts_match.group(3)
                    return {
                        "classifier": classifier,
                        "extension": extension,
                    }

        # Fallback: use os.path.splitext for simple extension detection
        base, ext = os.path.splitext(filename)
        if ext:
            return {"classifier": None, "extension": ext[1:]}

        return {"classifier": None, "extension": ""}

    def _extract_metadata_coordinates(self, path: str) -> dict:
        """Extract coordinates from a maven-metadata.xml path.

        Args:
            path: Normalised metadata path.

        Returns:
            Dictionary with ``namespace``, ``name``, ``version``,
            ``classifier``, ``extension``, ``group_path``.
        """
        cleaned = path.strip("/")

        # Strip maven-metadata.xml suffix
        if cleaned.endswith("/" + MAVEN_METADATA_FILENAME):
            cleaned = cleaned[: -(len(MAVEN_METADATA_FILENAME) + 1)]
        elif cleaned == MAVEN_METADATA_FILENAME:
            cleaned = ""

        parts = [p for p in cleaned.split("/") if p]

        if len(parts) >= 3 and self._looks_like_version(parts[-1]):
            # Version-level metadata
            version = parts[-1]
            artifact_id = parts[-2]
            group_path = "/".join(parts[:-2])
            return {
                "namespace": self._group_path_to_group_id(group_path),
                "name": artifact_id,
                "version": version,
                "classifier": None,
                "extension": "xml",
                "group_path": group_path,
            }

        if len(parts) >= 2:
            # Artifact-level metadata
            artifact_id = parts[-1]
            group_path = "/".join(parts[:-1])
            return {
                "namespace": self._group_path_to_group_id(group_path),
                "name": artifact_id,
                "version": None,
                "classifier": None,
                "extension": "xml",
                "group_path": group_path,
            }

        # Group-level metadata
        group_path = "/".join(parts)
        return {
            "namespace": self._group_path_to_group_id(group_path) if parts else "",
            "name": None,
            "version": None,
            "classifier": None,
            "extension": "xml",
            "group_path": group_path,
        }

    def _group_path_to_group_id(self, group_path: str) -> str:
        """Convert directory-based groupId path to dot-notation groupId.

        Args:
            group_path: Directory path (e.g., ``'org/apache/commons'``).

        Returns:
            Dot-notation groupId (e.g., ``'org.apache.commons'``).
        """
        return group_path.replace("/", ".")

    def _group_id_to_group_path(self, group_id: str) -> str:
        """Convert dot-notation groupId to directory path.

        Args:
            group_id: Dot-notation groupId (e.g., ``'org.apache.commons'``).

        Returns:
            Directory path (e.g., ``'org/apache/commons'``).
        """
        return group_id.replace(".", "/")

    @staticmethod
    def _looks_like_version(segment: str) -> bool:
        """Heuristically determine if a path segment looks like a version.

        A segment is considered version-like if it:

        - Starts with a digit, or
        - Contains ``SNAPSHOT`` (case-insensitive)

        Args:
            segment: Path segment to evaluate.

        Returns:
            ``True`` if the segment appears to be a version string.
        """
        if not segment:
            return False
        # Starts with a digit
        if segment[0].isdigit():
            return True
        # Contains SNAPSHOT
        if "SNAPSHOT" in segment.upper():
            return True
        return False
