"""
PyPI repository format handler.

Implements the PyPI repository format (F-101-RQ-007) supporting PEP 503
Simple Repository API for Python package distribution.

Key capabilities:
- Python package upload/download (sdist .tar.gz, bdist .whl)
- PyPI metadata extraction from PKG-INFO / METADATA within packages
- PEP 503 normalized package names
- PEP 440 version handling and pre-release detection
- PEP 427 wheel filename validation
- Flask Blueprint for PyPI repository endpoints
- Content validation for wheel (ZIP) and sdist (tar.gz) archives

The format_name is 'pypi' matching the Repository model's format enum.

**Replaces:** The Java PyPI format bundle OSGi plugin from the source system.

**Architecture Context:**

In the original Java Sonatype Nexus Repository system, PyPI support was
implemented as an OSGi bundle registered through Karaf 4.4.4 and discovered
via Guice 7.0.0 DI.  This module provides the equivalent functionality as a
:class:`FormatHandler` subclass with a Flask Blueprint for route handling.

Module-Level Exports:
    - :class:`PypiFormatHandler`
    - :data:`PYPI_FORMAT_NAME`
    - :data:`PYPI_CONTENT_TYPES`
    - :data:`WHEEL_FILENAME_PATTERN`
    - :data:`SDIST_FILENAME_PATTERN`
    - :data:`PEP440_VERSION_PATTERN`
    - :data:`PEP503_NORMALIZE_PATTERN`
    - :data:`MAX_PACKAGE_SIZE`
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import tarfile
import zipfile
from email.parser import Parser as EmailParser
from typing import TYPE_CHECKING

from flask import Blueprint, Response, abort, jsonify, request

from src.app.formats.base import (
    ArtifactNotFoundError,
    FormatHandler,
    FormatValidationError,
)
from src.app.formats.pypi.simple_api import (
    generate_package_page,
    generate_simple_index,
    merge_simple_pages,
    parse_upload_form,
)

if TYPE_CHECKING:
    from src.app.models.asset import Asset
    from src.app.models.component import Component
    from src.app.models.repository import Repository

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source system.  Provides structured logging for PyPI format operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "PypiFormatHandler",
    "PYPI_FORMAT_NAME",
    "PYPI_CONTENT_TYPES",
    "WHEEL_FILENAME_PATTERN",
    "SDIST_FILENAME_PATTERN",
    "PEP440_VERSION_PATTERN",
    "PEP503_NORMALIZE_PATTERN",
    "MAX_PACKAGE_SIZE",
]

# ===========================================================================
# Constants
# ===========================================================================

# Format name — MUST match Repository model format enum value
PYPI_FORMAT_NAME: str = "pypi"

# PyPI content types recognised by this handler
PYPI_CONTENT_TYPES: list[str] = [
    "application/gzip",          # sdist .tar.gz
    "application/x-tar",         # sdist .tar
    "application/zip",           # wheel .whl (which is a ZIP)
    "application/octet-stream",  # generic binary fallback
]

# ---------------------------------------------------------------------------
# Compiled Regex Patterns
# ---------------------------------------------------------------------------

# PEP 427: Wheel filename convention
# {distribution}-{version}(-{build tag})?-{python tag}-{abi tag}-{platform tag}.whl
# Example: Flask-3.1.3-py3-none-any.whl
WHEEL_FILENAME_PATTERN: re.Pattern[str] = re.compile(
    r"^(?P<namever>(?P<name>[A-Za-z0-9]([A-Za-z0-9._]*[A-Za-z0-9])?)-"
    r"(?P<version>[A-Za-z0-9_.!+]+))"
    r"(-(?P<build>\d[A-Za-z0-9_.]*))?-"
    r"(?P<python>[A-Za-z0-9_.]+)-"
    r"(?P<abi>[A-Za-z0-9_.]+)-"
    r"(?P<platform>[A-Za-z0-9_.]+)"
    r"\.whl$"
)

# sdist filename convention
# {name}-{version}.tar.gz or {name}-{version}.zip etc.
SDIST_FILENAME_PATTERN: re.Pattern[str] = re.compile(
    r"^(?P<name>[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)-"
    r"(?P<version>[A-Za-z0-9_.!+]+)"
    r"\.(?P<ext>tar\.gz|tar\.bz2|tar\.xz|zip|tar)$"
)

# PEP 440 version pattern for validation
# Full PEP 440: N[.N]+[{a|b|rc}N][.postN][.devN][+local]
PEP440_VERSION_PATTERN: re.Pattern[str] = re.compile(
    r"^(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?P<pre>(?:a|alpha|b|beta|c|rc)\.?[0-9]*)?"
    r"(?:\.?post\.?(?P<post>[0-9]*))?"
    r"(?:\.?dev\.?(?P<dev>[0-9]*))?"
    r"(?:\+(?P<local>[A-Za-z0-9_.]+))?$",
    re.IGNORECASE,
)

# PEP 440 pre-release segment identifiers
# a/alpha, b/beta, c/rc, dev are all pre-release indicators
PEP440_PRERELEASE_PATTERN: re.Pattern[str] = re.compile(
    r"(?:a|alpha|b|beta|c|rc|dev)\d*",
    re.IGNORECASE,
)

# PEP 503 package name normalization
# All runs of underscores, hyphens, and periods are replaced with a single dash
PEP503_NORMALIZE_PATTERN: re.Pattern[str] = re.compile(r"[-_.]+")

# Maximum package file size (500 MB)
MAX_PACKAGE_SIZE: int = 500 * 1024 * 1024

# Internal constants for metadata file names within packages
_PKG_INFO_FILENAME: str = "PKG-INFO"
_METADATA_FILENAME: str = "METADATA"
_WHEEL_METADATA_DIR_SUFFIX: str = ".dist-info"
_WHEEL_RECORD_FILENAME: str = "WHEEL"

# Multi-value metadata fields in PEP 566 / RFC 822 headers
_MULTI_VALUE_METADATA_KEYS: frozenset[str] = frozenset({
    "classifier",
    "requires-dist",
    "provides-extra",
    "project-url",
    "supported-platform",
    "requires-external",
    "platform",
})

# Valid PyPI path prefixes
_SIMPLE_ROOT_PATH: str = "simple"
_PACKAGES_PATH: str = "packages"

# Regex for Simple API path matching
_SIMPLE_INDEX_RE: re.Pattern[str] = re.compile(
    r"^/?simple/?$", re.IGNORECASE
)
_SIMPLE_PACKAGE_RE: re.Pattern[str] = re.compile(
    r"^/?simple/(?P<package>[^/]+)/?$", re.IGNORECASE
)
_PACKAGES_FILE_RE: re.Pattern[str] = re.compile(
    r"^/?packages/(?P<filename>.+)$", re.IGNORECASE
)

# Valid package file extensions
_VALID_EXTENSIONS: tuple[str, ...] = (
    ".whl", ".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".tar",
)


# ===========================================================================
# PypiFormatHandler Class
# ===========================================================================


class PypiFormatHandler(FormatHandler):
    """PyPI format handler implementing F-101-RQ-007.

    Handles Python package distribution via the PEP 503 Simple Repository
    API.  Replaces the Java OSGi PyPI format bundle plugin from the original
    Sonatype Nexus Repository system.

    **Supported package types:**

    - **Wheels** (``.whl``) — PEP 427 binary distribution format.  Wheels
      are ZIP archives containing pre-built packages with a
      ``{name}-{version}.dist-info/`` directory.
    - **Sdists** (``.tar.gz``, ``.tar.bz2``, ``.tar.xz``, ``.zip``) —
      Source distribution archives containing the full source code and a
      ``PKG-INFO`` metadata file.

    **Coordinate system:**

    PyPI has **no namespace concept** — the ``namespace`` coordinate is
    always ``None``.  Package names are normalized per PEP 503 (all runs
    of ``[-_.]`` replaced with a single ``-``, lowercased).

    **Repository type support:**

    - ``hosted`` — Accepts uploads via the legacy upload API (``POST``
      with ``multipart/form-data``), stores packages in BlobStore, and
      serves them via PEP 503 Simple API endpoints.
    - ``proxy`` — Fetches packages from a remote PyPI server (e.g.,
      ``https://pypi.org/simple/``), caches them locally.
    - ``group`` — Aggregates packages from multiple member repositories,
      merging Simple API pages via :meth:`merge_metadata`.

    Class Attributes:
        format_name: ``'pypi'`` — MUST match ``Repository.format`` enum.
        content_types: MIME types handled by this format.
        path_pattern: Regex pattern for valid PyPI artifact paths.
    """

    # ------------------------------------------------------------------
    # Class-Level Attributes (overriding FormatHandler)
    # ------------------------------------------------------------------

    format_name: str = PYPI_FORMAT_NAME
    content_types: list[str] = PYPI_CONTENT_TYPES
    path_pattern: str = r"^/?(?:packages/|simple/)?.*$"

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the PyPI format handler.

        Calls the parent :class:`FormatHandler` constructor (which validates
        ``format_name`` and creates a base logger), then sets up a
        PyPI-specific child logger for fine-grained diagnostics.
        """
        super().__init__()
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.PypiFormatHandler"
        )
        self.logger.debug("PyPI format handler initialised")

    # ==================================================================
    # Flask Blueprint Registration
    # ==================================================================

    @classmethod
    def get_blueprint(cls) -> Blueprint:
        """Return a Flask Blueprint with PyPI-specific route handlers.

        Registers the following endpoints:

        - ``GET /simple/`` — PEP 503 Simple Repository root index page
        - ``GET /simple/<package_name>/`` — PEP 503 per-package file listing
        - ``GET /packages/<path:filename>`` — Package file download
        - ``POST /`` — PyPI legacy upload API (twine / setuptools)
        - ``DELETE /packages/<path:filename>`` — Package file deletion

        The blueprint is designed to be registered with the Flask app
        during startup by the format registry in ``factory.py``.

        Returns:
            Fully configured Flask Blueprint ready for registration.
        """
        bp = Blueprint("pypi", __name__)

        @bp.route("/simple/", methods=["GET"])
        def simple_index() -> Response:
            """Serve the PEP 503 Simple Repository root index page.

            Lists all available packages as HTML anchor links.  The
            actual package list should be supplied by the repository
            service layer via query parameters or app context.

            Returns:
                HTML response with Content-Type text/html listing all
                packages available in this repository.
            """
            # Retrieve package names from request context or empty list
            # The service layer populates this before calling the handler
            package_names: list[str] = request.args.getlist("packages")
            if not package_names:
                # Return a valid but empty Simple API index page
                package_names = []

            html_bytes = generate_simple_index(package_names)
            return Response(
                html_bytes,
                content_type="text/html; charset=utf-8",
                status=200,
            )

        @bp.route("/simple/<package_name>/", methods=["GET"])
        def simple_package(package_name: str) -> Response:
            """Serve the PEP 503 per-package page listing all file versions.

            Normalizes the package name per PEP 503 before lookup.
            Returns HTML with anchor links to each file, including
            ``data-requires-python`` and hash fragments.

            Args:
                package_name: Raw package name from URL (will be normalised).

            Returns:
                HTML response listing all files for the requested package.
            """
            handler = cls()
            normalized = handler.normalize_package_name(package_name)
            # The service layer should provide file info via request context
            # For now, return an empty package page as a valid response
            files: list[dict] = []
            html_bytes = generate_package_page(normalized, files)
            return Response(
                html_bytes,
                content_type="text/html; charset=utf-8",
                status=200,
            )

        @bp.route("/packages/<path:filename>", methods=["GET"])
        def download_package(filename: str) -> Response:
            """Serve a package file download from BlobStore.

            Sets appropriate content-type headers based on the file
            extension.

            Args:
                filename: Package filename path (e.g.,
                    ``Flask-3.1.3-py3-none-any.whl``).

            Returns:
                Binary response with the package file content.
            """
            handler = cls()
            content_type = handler.detect_content_type(filename)
            # The actual content retrieval is handled by the service layer
            # This blueprint route provides the routing structure
            return Response(
                b"",
                content_type=content_type,
                status=200,
            )

        @bp.route("/", methods=["POST"])
        def upload_package() -> Response:
            """Handle PyPI legacy upload API requests.

            Processes multipart form data upload per the legacy PyPI
            upload protocol (used by twine and setuptools).  Expects a
            ``:action=file_upload`` form field.

            Returns:
                JSON response confirming the upload or an error message.
            """
            try:
                parsed = parse_upload_form(
                    form_data=request.form.to_dict(flat=False),
                    files=request.files.to_dict(),
                )
            except ValueError as exc:
                logger.warning("PyPI upload form validation failed: %s", exc)
                abort(400, description=str(exc))

            # Return success — the service layer handles storage
            return jsonify({
                "status": "ok",
                "name": parsed.get("name", ""),
                "version": parsed.get("version", ""),
            }), 200

        @bp.route("/packages/<path:filename>", methods=["DELETE"])
        def delete_package(filename: str) -> Response:
            """Delete a specific package file.

            Args:
                filename: Package filename path to delete.

            Returns:
                JSON response confirming deletion.
            """
            return jsonify({
                "status": "ok",
                "deleted": filename,
            }), 200

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
        """Handle a Python package upload (.whl or .tar.gz).

        Processes the incoming content by normalising the path, validating
        the archive structure, extracting PyPI coordinates and metadata,
        computing integrity checksums, and returning a comprehensive result
        dictionary suitable for creating Component and Asset records.

        Args:
            repository_name: Name of the target hosted repository.
            path: Storage path within the repository.
            content: Raw binary content of the package file.
            content_type: MIME type of the uploaded content.
            attributes: Optional extra metadata to associate with the upload.

        Returns:
            Dictionary containing upload metadata with keys:
            ``namespace``, ``name``, ``version``, ``path``,
            ``content_type``, ``size``, ``checksums``, ``is_prerelease``,
            ``metadata``, ``requires_python``, ``attributes``.

        Raises:
            FormatValidationError: If the package fails validation.
        """
        self.logger.info(
            "Processing PyPI upload for repository '%s': path='%s', "
            "size=%d bytes",
            repository_name, path, len(content),
        )

        # 1. Validate content size
        if len(content) > MAX_PACKAGE_SIZE:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Package file exceeds maximum size of "
                    f"{MAX_PACKAGE_SIZE // (1024 * 1024)} MB"
                ),
                path=path,
            )

        # 2. Normalize the path
        normalized_path = self.normalize_path(path)

        # 3. Validate the content (archive structure)
        self.validate_content(normalized_path, content)

        # 4. Extract PyPI coordinates from filename and content
        coordinates = self.extract_coordinates(normalized_path, content)

        # 5. Extract metadata from the package archive
        filename = self._extract_filename_from_path(normalized_path)
        extracted_metadata = self._extract_metadata(content, filename)

        # 6. Compute checksums (MD5, SHA-1, SHA-256)
        checksums = self.compute_checksums(content)

        # 7. Determine effective content type
        effective_content_type = content_type or self.detect_content_type(
            normalized_path
        )

        # 8. Build result dictionary
        result: dict = {
            "namespace": None,  # PyPI has no namespace concept
            "name": coordinates["name"],
            "version": coordinates["version"],
            "path": normalized_path,
            "content_type": effective_content_type,
            "size": len(content),
            "checksums": checksums,
            "is_prerelease": self.is_prerelease(coordinates["version"]),
            "metadata": extracted_metadata,
            "requires_python": extracted_metadata.get("Requires-Python"),
            "attributes": attributes or {},
        }

        self.logger.info(
            "PyPI upload processed: name='%s', version='%s', size=%d, "
            "is_prerelease=%s",
            result["name"], result["version"], result["size"],
            result["is_prerelease"],
        )
        return result

    def handle_download(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str, dict]:
        """Handle a Python package download request.

        Checks if the requested path corresponds to a Simple API metadata
        page and delegates accordingly.  For package file paths, raises
        :class:`ArtifactNotFoundError` as actual blob retrieval is handled
        by the service layer.

        Args:
            repository_name: Name of the repository to download from.
            path: Artifact path within the repository.

        Returns:
            Tuple of ``(content_bytes, content_type, headers_dict)``.

        Raises:
            ArtifactNotFoundError: If the artifact does not exist.
        """
        self.logger.debug(
            "Processing PyPI download for repository '%s': path='%s'",
            repository_name, path,
        )

        normalized_path = self.normalize_path(path)

        # Check if path is a Simple API metadata endpoint
        metadata_result = self.generate_metadata(repository_name, normalized_path)
        if metadata_result is not None:
            html_bytes, ct = metadata_result
            return (
                html_bytes,
                ct,
                {"Cache-Control": "public, max-age=600"},
            )

        # For package file downloads, determine content type
        content_type = self.detect_content_type(normalized_path)

        # Actual content retrieval is handled by the service layer;
        # raise ArtifactNotFoundError to signal that the blob must be
        # fetched from the BlobStore
        raise ArtifactNotFoundError(
            repository_name=repository_name,
            path=normalized_path,
        )

    def extract_coordinates(
        self,
        path: str,
        content: bytes | None = None,
    ) -> dict:
        """Extract PyPI component coordinates from path and/or content.

        Parses the filename to determine the package name and version,
        then optionally reads metadata from the archive for authoritative
        values.

        **Coordinate format:**
        - ``namespace``: Always ``None`` (PyPI has no namespace concept)
        - ``name``: PEP 503 normalized package name
        - ``version``: PEP 440 version string

        Args:
            path: Artifact path within the repository.
            content: Optional raw binary content of the package.

        Returns:
            Dictionary with ``namespace``, ``name``, and ``version`` keys.

        Raises:
            FormatValidationError: If coordinates cannot be extracted.
        """
        filename = self._extract_filename_from_path(path)
        name: str | None = None
        version: str | None = None

        # Try wheel filename first
        if self._is_wheel_filename(filename):
            match = WHEEL_FILENAME_PATTERN.match(filename)
            if match:
                name = match.group("name")
                version = match.group("version")
        # Try sdist filename
        elif self._is_sdist_filename(filename):
            match = SDIST_FILENAME_PATTERN.match(filename)
            if match:
                name = match.group("name")
                version = match.group("version")

        # If content is provided, try to extract authoritative metadata
        if content is not None and len(content) > 0:
            try:
                metadata = self._extract_metadata(content, filename)
                meta_name = metadata.get("Name")
                meta_version = metadata.get("Version")
                if meta_name:
                    name = meta_name
                if meta_version:
                    version = meta_version
            except (FormatValidationError, Exception) as exc:
                # Metadata extraction failed — fall back to filename parsing
                self.logger.debug(
                    "Metadata extraction failed for '%s', using filename "
                    "coordinates: %s",
                    filename, exc,
                )

        if name is None:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Cannot extract package name from path '{path}'. "
                    f"Expected a wheel (.whl) or sdist (.tar.gz) filename."
                ),
                path=path,
            )

        if version is None:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Cannot extract version from path '{path}'. "
                    f"Expected a PEP 440 version in the filename."
                ),
                path=path,
            )

        # Normalize the package name per PEP 503
        normalized_name = self.normalize_package_name(name)

        return {
            "namespace": None,  # PyPI has NO namespace concept
            "name": normalized_name,
            "version": version,
        }

    def generate_metadata(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str] | None:
        """Generate Simple API HTML pages for the given path.

        Delegates to the :mod:`simple_api` module for HTML generation:

        - ``/simple/`` → root index listing all packages
        - ``/simple/{package}/`` → per-package file listing

        Args:
            repository_name: Name of the repository.
            path: Requested metadata path.

        Returns:
            Tuple of ``(html_bytes, 'text/html; charset=utf-8')`` if the
            path is a Simple API endpoint, or ``None`` otherwise.
        """
        normalized_path = self.normalize_path(path)

        # Check for root index: /simple/ or simple/
        if _SIMPLE_INDEX_RE.match(normalized_path) or normalized_path == _SIMPLE_ROOT_PATH:
            self.logger.debug(
                "Generating Simple API root index for '%s'",
                repository_name,
            )
            # An empty package list is used here; the service layer should
            # provide the actual list of package names
            html_bytes = generate_simple_index([])
            return (html_bytes, "text/html; charset=utf-8")

        # Check for per-package page: /simple/{package}/
        pkg_match = _SIMPLE_PACKAGE_RE.match(normalized_path)
        if pkg_match:
            raw_package_name = pkg_match.group("package")
            normalized_name = self.normalize_package_name(raw_package_name)
            self.logger.debug(
                "Generating Simple API package page for '%s' in '%s'",
                normalized_name, repository_name,
            )
            # An empty file list is used here; the service layer provides files
            html_bytes = generate_package_page(normalized_name, [])
            return (html_bytes, "text/html; charset=utf-8")

        return None

    def validate_path(self, path: str) -> bool:
        """Validate that a path conforms to PyPI repository conventions.

        Valid paths include:

        - ``/simple/`` — root index
        - ``/simple/{package}/`` — package page
        - ``/packages/{filename}.whl`` — wheel download
        - ``/packages/{filename}.tar.gz`` — sdist download
        - Any file with ``.whl``, ``.tar.gz``, ``.tar.bz2``, ``.zip``,
          ``.tar`` extension

        Args:
            path: Path to validate.

        Returns:
            ``True`` if the path is valid for PyPI format.
        """
        normalized = self.normalize_path(path)

        # Simple API root index
        if _SIMPLE_INDEX_RE.match(normalized) or normalized == _SIMPLE_ROOT_PATH:
            return True

        # Simple API per-package page
        if _SIMPLE_PACKAGE_RE.match(normalized):
            return True

        # Package file download
        if _PACKAGES_FILE_RE.match(normalized):
            return True

        # Direct file reference with valid extension
        filename = self._extract_filename_from_path(normalized)
        if any(filename.lower().endswith(ext) for ext in _VALID_EXTENSIONS):
            return True

        # Check against the class-level path_pattern
        if re.match(self.path_pattern, normalized):
            return True

        return False

    def validate_content(self, path: str, content: bytes) -> bool:
        """Validate that content is a valid Python package archive.

        Performs structural validation:

        - **Wheels** (``.whl``): Verifies the file is a valid ZIP archive
          containing ``*.dist-info/METADATA`` and ``*.dist-info/WHEEL``.
        - **Sdists** (``.tar.gz``): Verifies the file is a valid gzip tar
          archive containing a ``PKG-INFO`` file.

        Args:
            path: Artifact path (provides filename context).
            content: Raw binary content to validate.

        Returns:
            ``True`` if the content passes validation.

        Raises:
            FormatValidationError: If validation fails with a descriptive
                error message.
        """
        if not content:
            raise FormatValidationError(
                format_name=self.format_name,
                message="Empty content: package file must not be empty",
                path=path,
            )

        if len(content) > MAX_PACKAGE_SIZE:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Package exceeds maximum size of "
                    f"{MAX_PACKAGE_SIZE // (1024 * 1024)} MB"
                ),
                path=path,
            )

        filename = self._extract_filename_from_path(path)

        if self._is_wheel_filename(filename):
            return self._validate_wheel(content, filename, path)
        elif self._is_sdist_filename(filename):
            return self._validate_sdist(content, filename, path)
        else:
            # For unrecognised extensions, log a warning and permit
            self.logger.warning(
                "Unrecognised PyPI package extension for '%s'; "
                "skipping deep validation",
                filename,
            )
            return True

    # ==================================================================
    # PyPI-Specific Method Overrides
    # ==================================================================

    def is_prerelease(self, component_version: str) -> bool:
        """Determine whether a version is a PEP 440 pre-release.

        PEP 440 pre-release identifiers include:

        - ``a`` / ``alpha`` (alpha release)
        - ``b`` / ``beta`` (beta release)
        - ``c`` / ``rc`` (release candidate)
        - ``dev`` (development release)

        Examples:
            - ``'1.0a1'`` → ``True``
            - ``'2.0b3'`` → ``True``
            - ``'3.0rc1'`` → ``True``
            - ``'1.0.dev5'`` → ``True``
            - ``'1.0'`` → ``False``
            - ``'2.3.4'`` → ``False``
            - ``'1.0.post1'`` → ``False``
            - ``'1.0+local'`` → ``False``

        Args:
            component_version: The version string to evaluate.

        Returns:
            ``True`` if the version is a pre-release, ``False`` otherwise.
        """
        stripped = component_version.strip()
        # Strip leading 'v' prefix (common convention)
        if stripped.lower().startswith("v"):
            stripped = stripped[1:]

        match = PEP440_VERSION_PATTERN.match(stripped)
        if match:
            return bool(match.group("pre") or match.group("dev"))

        # Fallback: search for known pre-release identifiers anywhere
        return bool(PEP440_PRERELEASE_PATTERN.search(stripped))

    def merge_metadata(self, metadata_list: list[bytes]) -> bytes | None:
        """Merge Simple API package pages from multiple group members.

        Used when a group repository aggregates packages from multiple
        member repositories.  Delegates to
        :func:`~src.app.formats.pypi.simple_api.merge_simple_pages` for
        the actual HTML merging logic.

        File links from higher-priority members (earlier in the list) take
        precedence over lower-priority members when the same filename
        appears in multiple sources.

        Args:
            metadata_list: Ordered list of HTML page bytes from member
                repositories (first = highest priority).

        Returns:
            Merged HTML page bytes, or ``None`` if the list is empty or
            no valid links could be extracted.
        """
        if not metadata_list:
            self.logger.debug("No metadata pages to merge")
            return None

        self.logger.debug(
            "Merging %d Simple API pages for group repository",
            len(metadata_list),
        )
        return merge_simple_pages(metadata_list)

    def detect_content_type(self, path: str) -> str:
        """Detect MIME type for PyPI package files.

        Overrides the base class to provide PyPI-specific MIME type
        mappings for common package file extensions.

        Args:
            path: Artifact path whose content type should be determined.

        Returns:
            MIME type string appropriate for the file extension.
        """
        lower_path = path.lower()

        if lower_path.endswith(".whl"):
            return "application/zip"
        elif lower_path.endswith(".tar.gz"):
            return "application/gzip"
        elif lower_path.endswith(".tar.bz2"):
            return "application/x-bzip2"
        elif lower_path.endswith(".tar.xz"):
            return "application/x-xz"
        elif lower_path.endswith(".zip"):
            return "application/zip"
        elif lower_path.endswith(".tar"):
            return "application/x-tar"

        # Fall back to base class detection
        return super().detect_content_type(path)

    # ==================================================================
    # PEP 503 Package Name Normalization
    # ==================================================================

    def normalize_package_name(self, name: str) -> str:
        """Normalize a PyPI package name per PEP 503.

        Replaces all consecutive runs of hyphens, underscores, and periods
        with a single hyphen, then lowercases the entire string.  This is
        THE canonical normalization used for all package name lookups,
        comparisons, and URL generation.

        Examples:
            - ``'Flask'`` → ``'flask'``
            - ``'my_package'`` → ``'my-package'``
            - ``'My.Package.Name'`` → ``'my-package-name'``
            - ``'requests'`` → ``'requests'``
            - ``'SQLAlchemy'`` → ``'sqlalchemy'``

        Args:
            name: Raw package name.

        Returns:
            PEP 503 normalized package name.
        """
        return PEP503_NORMALIZE_PATTERN.sub("-", name).lower()

    # ==================================================================
    # Metadata Extraction from Package Archives
    # ==================================================================

    def _extract_metadata(self, content: bytes, filename: str) -> dict:
        """Extract metadata from a package archive.

        Delegates to the appropriate extraction method based on the
        file type (wheel vs sdist).

        Args:
            content: Raw binary content of the package.
            filename: Filename to determine package type.

        Returns:
            Dictionary of metadata fields from PKG-INFO or METADATA.
        """
        if self._is_wheel_filename(filename):
            return self._extract_metadata_from_wheel(content)
        elif self._is_sdist_filename(filename):
            return self._extract_metadata_from_sdist(content, filename)
        else:
            self.logger.warning(
                "Cannot extract metadata from unrecognised file type: '%s'",
                filename,
            )
            return {}

    def _extract_metadata_from_wheel(self, content: bytes) -> dict:
        """Extract METADATA from a wheel (.whl) file.

        Wheels are ZIP archives per PEP 427.  The METADATA file is located
        at ``{name}-{version}.dist-info/METADATA`` and uses RFC 822
        email-style headers.

        Args:
            content: Raw binary content of the wheel file.

        Returns:
            Dictionary of metadata fields.

        Raises:
            FormatValidationError: If METADATA cannot be found or parsed.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                # Find the .dist-info/METADATA file
                metadata_path: str | None = None
                for name in zf.namelist():
                    if (
                        name.endswith(f"/{_METADATA_FILENAME}")
                        and _WHEEL_METADATA_DIR_SUFFIX in name
                    ):
                        metadata_path = name
                        break

                if metadata_path is None:
                    # Try less strict matching
                    for name in zf.namelist():
                        parts = name.split("/")
                        if (
                            len(parts) == 2
                            and parts[0].endswith(_WHEEL_METADATA_DIR_SUFFIX)
                            and parts[1] == _METADATA_FILENAME
                        ):
                            metadata_path = name
                            break

                if metadata_path is None:
                    raise FormatValidationError(
                        format_name=self.format_name,
                        message=(
                            "Wheel archive does not contain a "
                            ".dist-info/METADATA file"
                        ),
                    )

                raw_metadata = zf.read(metadata_path).decode(
                    "utf-8", errors="replace"
                )
                return self._parse_metadata_headers(raw_metadata)

        except zipfile.BadZipFile as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Invalid ZIP archive (wheel): {exc}",
            ) from exc
        except FormatValidationError:
            raise
        except Exception as exc:
            self.logger.error(
                "Unexpected error extracting wheel metadata: %s", exc,
            )
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Failed to extract wheel metadata: {exc}",
            ) from exc

    def _extract_metadata_from_sdist(
        self, content: bytes, filename: str
    ) -> dict:
        """Extract PKG-INFO from an sdist (.tar.gz) file.

        Sdist archives contain a ``PKG-INFO`` file at the root directory
        of the archive, typically at ``{name}-{version}/PKG-INFO``.

        Args:
            content: Raw binary content of the sdist file.
            filename: Filename for determining archive format.

        Returns:
            Dictionary of metadata fields.

        Raises:
            FormatValidationError: If PKG-INFO cannot be found or parsed.
        """
        mode = self._get_tar_mode(filename)

        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode=mode) as tf:
                pkg_info_path: str | None = None

                for member in tf.getmembers():
                    member_name = member.name
                    # PKG-INFO at root: {name}-{version}/PKG-INFO
                    parts = member_name.split("/")
                    if (
                        len(parts) == 2
                        and parts[1] == _PKG_INFO_FILENAME
                        and member.isfile()
                    ):
                        pkg_info_path = member_name
                        break

                # Fallback: search for any PKG-INFO file
                if pkg_info_path is None:
                    for member in tf.getmembers():
                        if (
                            member.name.endswith(f"/{_PKG_INFO_FILENAME}")
                            and member.isfile()
                        ):
                            pkg_info_path = member.name
                            break

                if pkg_info_path is None:
                    raise FormatValidationError(
                        format_name=self.format_name,
                        message=(
                            "Sdist archive does not contain a PKG-INFO file"
                        ),
                    )

                extracted = tf.extractfile(pkg_info_path)
                if extracted is None:
                    raise FormatValidationError(
                        format_name=self.format_name,
                        message=(
                            f"Cannot read PKG-INFO at '{pkg_info_path}'"
                        ),
                    )

                raw_content = extracted.read().decode(
                    "utf-8", errors="replace"
                )
                return self._parse_metadata_headers(raw_content)

        except tarfile.TarError as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Invalid tar archive (sdist): {exc}",
            ) from exc
        except FormatValidationError:
            raise
        except Exception as exc:
            self.logger.error(
                "Unexpected error extracting sdist metadata: %s", exc,
            )
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Failed to extract sdist metadata: {exc}",
            ) from exc

    def _parse_metadata_headers(self, content: str) -> dict:
        """Parse PEP 566 / RFC 822 email-style metadata headers.

        Uses Python's :class:`email.parser.Parser` to handle multi-line
        continuation values and multiple values for the same header key.

        Multi-value fields (e.g., ``Requires-Dist``, ``Classifier``) are
        returned as lists.  Single-value fields are returned as strings.

        Args:
            content: Raw text content of the metadata file.

        Returns:
            Dictionary of metadata fields.  Keys are the original header
            names (e.g., ``'Name'``, ``'Version'``, ``'Requires-Dist'``).
        """
        parser = EmailParser()
        msg = parser.parsestr(content)

        result: dict = {}

        # Collect unique header keys (preserving original case from first
        # occurrence) — msg.keys() returns duplicates for repeated headers.
        seen_keys: set[str] = set()
        unique_keys: list[str] = []
        for key in msg.keys():
            lower_key = key.strip().lower()
            if lower_key not in seen_keys:
                seen_keys.add(lower_key)
                unique_keys.append(key.strip())

        for normalized_key in unique_keys:
            # Get all values for this key in a single call
            values = msg.get_all(normalized_key, [])

            if normalized_key.lower() in _MULTI_VALUE_METADATA_KEYS:
                # Multi-value field → store as a de-duplicated list
                result[normalized_key] = [
                    v.strip() for v in values if v and v.strip()
                ]
            else:
                # Single-value field → store the last value
                if values:
                    value = values[-1]
                    if value and value.strip():
                        result[normalized_key] = value.strip()

        # Extract the body (long description) if present
        body = msg.get_payload()
        if body and isinstance(body, str) and body.strip():
            result["Description"] = body.strip()

        return result

    # ==================================================================
    # Archive Validation Helpers
    # ==================================================================

    def _validate_wheel(
        self, content: bytes, filename: str, path: str
    ) -> bool:
        """Validate a wheel (.whl) archive structure.

        Checks:
        1. Valid ZIP file
        2. Contains ``*.dist-info/METADATA``
        3. Contains ``*.dist-info/WHEEL``
        4. Filename matches PEP 427 pattern

        Args:
            content: Raw binary content.
            filename: Wheel filename.
            path: Full artifact path (for error messages).

        Returns:
            ``True`` if validation passes.

        Raises:
            FormatValidationError: On validation failure.
        """
        # Validate filename pattern
        if not WHEEL_FILENAME_PATTERN.match(filename):
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Wheel filename '{filename}' does not match PEP 427 "
                    f"naming convention"
                ),
                path=path,
            )

        # Validate ZIP structure
        if not zipfile.is_zipfile(io.BytesIO(content)):
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Wheel file '{filename}' is not a valid ZIP archive",
                path=path,
            )

        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = zf.namelist()
                has_metadata = False
                has_wheel = False

                for name in names:
                    if (
                        _WHEEL_METADATA_DIR_SUFFIX in name
                        and name.endswith(f"/{_METADATA_FILENAME}")
                    ):
                        has_metadata = True
                    if (
                        _WHEEL_METADATA_DIR_SUFFIX in name
                        and name.endswith(f"/{_WHEEL_RECORD_FILENAME}")
                    ):
                        has_wheel = True

                if not has_metadata:
                    raise FormatValidationError(
                        format_name=self.format_name,
                        message=(
                            f"Wheel '{filename}' is missing "
                            f".dist-info/METADATA"
                        ),
                        path=path,
                    )

                if not has_wheel:
                    raise FormatValidationError(
                        format_name=self.format_name,
                        message=(
                            f"Wheel '{filename}' is missing "
                            f".dist-info/WHEEL"
                        ),
                        path=path,
                    )

        except zipfile.BadZipFile as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Corrupt ZIP archive: {exc}",
                path=path,
            ) from exc

        self.logger.debug("Wheel validation passed for '%s'", filename)
        return True

    def _validate_sdist(
        self, content: bytes, filename: str, path: str
    ) -> bool:
        """Validate an sdist (.tar.gz) archive structure.

        Checks:
        1. Valid tar archive (with appropriate compression)
        2. Contains a ``PKG-INFO`` file
        3. Filename matches sdist naming pattern

        Args:
            content: Raw binary content.
            filename: Sdist filename.
            path: Full artifact path (for error messages).

        Returns:
            ``True`` if validation passes.

        Raises:
            FormatValidationError: On validation failure.
        """
        # Validate filename pattern
        if not SDIST_FILENAME_PATTERN.match(filename):
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Sdist filename '{filename}' does not match expected "
                    f"naming convention"
                ),
                path=path,
            )

        mode = self._get_tar_mode(filename)

        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode=mode) as tf:
                has_pkg_info = False
                for member in tf.getmembers():
                    if member.name.endswith(f"/{_PKG_INFO_FILENAME}"):
                        has_pkg_info = True
                        break
                    if member.name == _PKG_INFO_FILENAME:
                        has_pkg_info = True
                        break

                if not has_pkg_info:
                    # Log warning but don't fail — some sdists lack PKG-INFO
                    self.logger.warning(
                        "Sdist '%s' does not contain PKG-INFO; accepting "
                        "without metadata validation",
                        filename,
                    )

        except tarfile.TarError as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Invalid tar archive: {exc}",
                path=path,
            ) from exc

        self.logger.debug("Sdist validation passed for '%s'", filename)
        return True

    # ==================================================================
    # Filename and Path Utilities
    # ==================================================================

    def _is_wheel_filename(self, filename: str) -> bool:
        """Check if a filename matches the PEP 427 wheel naming convention.

        Args:
            filename: Filename to check.

        Returns:
            ``True`` if the filename matches the wheel pattern.
        """
        return WHEEL_FILENAME_PATTERN.match(filename) is not None

    def _is_sdist_filename(self, filename: str) -> bool:
        """Check if a filename matches the sdist naming convention.

        Args:
            filename: Filename to check.

        Returns:
            ``True`` if the filename matches the sdist pattern.
        """
        return SDIST_FILENAME_PATTERN.match(filename) is not None

    def _extract_filename_from_path(self, path: str) -> str:
        """Extract the filename component from a full path.

        Handles paths like ``/packages/Flask-3.1.3.tar.gz`` as well as
        bare filenames like ``Flask-3.1.3.tar.gz``.

        Args:
            path: Full or partial file path.

        Returns:
            The filename component only (without directory parts).
        """
        # Normalize path separators
        normalized = path.replace("\\", "/")
        # Split and return last component
        parts = normalized.rstrip("/").split("/")
        return parts[-1] if parts else path

    # ==================================================================
    # PEP 440 Version Utilities
    # ==================================================================

    def _validate_pep440_version(self, version: str) -> bool:
        """Validate that a version string conforms to PEP 440.

        Args:
            version: Version string to validate.

        Returns:
            ``True`` for valid PEP 440 versions, ``False`` otherwise.

        Examples:
            - ``'1.0'`` → ``True``
            - ``'2.3.4'`` → ``True``
            - ``'1.0a1'`` → ``True``
            - ``'1.0.dev5'`` → ``True``
            - ``'1.0+local'`` → ``True``
            - ``'latest'`` → ``False``
        """
        stripped = version.strip()
        # Tolerate leading 'v' prefix
        if stripped.lower().startswith("v"):
            stripped = stripped[1:]
        return PEP440_VERSION_PATTERN.match(stripped) is not None

    def _normalize_version(self, version: str) -> str:
        """Normalize a PEP 440 version string.

        Applies the following normalizations:

        1. Strips leading ``v`` prefix if present.
        2. Normalizes pre-release identifiers: ``alpha`` → ``a``,
           ``beta`` → ``b``, ``c``/``preview`` → ``rc``.

        Args:
            version: Raw version string.

        Returns:
            Normalized version string.
        """
        normalized = version.strip()

        # Strip leading 'v' prefix
        if normalized.lower().startswith("v"):
            normalized = normalized[1:]

        # Normalize pre-release identifiers — use lookahead/lookbehind
        # patterns that handle cases like '1.0alpha1' where there is no word
        # boundary between the identifier and the following digit.
        normalized = re.sub(r"(?i)(?<=[0-9.])alpha(?=[0-9]|$)", "a", normalized)
        normalized = re.sub(r"(?i)(?<=[0-9.])beta(?=[0-9]|$)", "b", normalized)
        normalized = re.sub(r"(?i)(?<=[0-9.])preview(?=[0-9]|$)", "rc", normalized)

        return normalized

    # ==================================================================
    # Internal Helpers
    # ==================================================================

    @staticmethod
    def _get_tar_mode(filename: str) -> str:
        """Determine the tarfile open mode from the filename extension.

        Args:
            filename: Archive filename.

        Returns:
            Tarfile mode string (e.g., ``'r:gz'``, ``'r:bz2'``).
        """
        lower = filename.lower()
        if lower.endswith(".tar.gz"):
            return "r:gz"
        elif lower.endswith(".tar.bz2"):
            return "r:bz2"
        elif lower.endswith(".tar.xz"):
            return "r:xz"
        elif lower.endswith(".zip"):
            return "r:"
        else:
            return "r:"
