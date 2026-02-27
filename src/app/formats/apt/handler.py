"""
APT/Debian Format Handler — Feature F-101-RQ-003.

This module implements the :class:`AptFormatHandler` class, the primary format
handler for APT/Debian repositories.  It extends the abstract
:class:`~src.app.formats.base.FormatHandler` base class and provides:

- ``.deb`` package upload and download handling
- Extraction of Debian control metadata (Package, Version, Architecture,
  Depends, Maintainer, Description, etc.) from ar archives
- Flask Blueprint registration for APT repository endpoints (``dists/``,
  ``pool/``)
- Validation of ``.deb`` file structure (ar archive containing
  ``control.tar.*`` and ``data.tar.*``)
- Component coordinate extraction: namespace=distribution,
  name=package-name, version=debian-version
- ``is_prerelease()`` check for ``~alpha``, ``~beta``, ``~rc`` version
  suffixes (Debian tilde convention)
- Integration with :class:`~src.app.formats.apt.metadata.AptMetadataManager`
  for repository index regeneration

**Architecture Context:**

Replaces the Java APT format OSGi bundle from the original Sonatype Nexus
Repository source system.  In the Java implementation, APT support was an
OSGi bundle registered via Karaf 4.4.4 and discovered through Guice 7.0.0.
In this Python/Flask reimplementation the handler is a
:class:`FormatHandler` subclass with a Flask Blueprint for APT-specific
HTTP routes replacing JAX-RS resource classes from RESTEasy 6.2.7.

**Supported APT Repository Structure:**

::

    <repository>/
    ├── dists/
    │   └── <distribution>/
    │       ├── Release
    │       ├── InRelease
    │       ├── Release.gpg
    │       └── <component>/
    │           └── binary-<arch>/
    │               ├── Packages
    │               ├── Packages.gz
    │               └── Packages.bz2
    └── pool/
        └── <component>/
            └── <prefix>/
                └── <package-name>/
                    └── <name>_<version>_<arch>.deb

**Module-Level Exports:**

- :class:`AptFormatHandler`
"""

from __future__ import annotations

import io
import logging
import mimetypes
import re
import struct
import tarfile
from typing import TYPE_CHECKING

from flask import Blueprint, Response, abort, jsonify, request, send_file

from src.app.formats.apt.metadata import (
    AptMetadataManager,
    PackagesIndexGenerator,
    compute_file_hashes,
    parse_deb_control,
)
from src.app.formats.base import (
    ArtifactNotFoundError,
    FormatHandler,
    FormatValidationError,
)

if TYPE_CHECKING:
    from src.app.models.asset import Asset
    from src.app.models.component import Component
    from src.app.models.repository import Repository

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source.  Provides module-level diagnostics for the APT format handler.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = ["AptFormatHandler"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# APT format identifier — must match Repository model format enum value
FORMAT_NAME: str = "apt"

# Content types for Debian packages
DEB_CONTENT_TYPE: str = "application/vnd.debian.binary-package"
DEB_CONTENT_TYPE_ALT: str = "application/x-deb"
DEB_CONTENT_TYPES: list[str] = [
    DEB_CONTENT_TYPE,
    DEB_CONTENT_TYPE_ALT,
    "application/x-debian-package",
]

# .deb file structure constants (ar archive format)
DEB_MAGIC: bytes = b"!<arch>\n"
DEB_MAGIC_LEN: int = 8
AR_HEADER_SIZE: int = 60  # 16 + 12 + 6 + 6 + 8 + 10 + 2
AR_HEADER_FORMAT: str = "16s12s6s6s8s10s2s"
AR_HEADER_MAGIC: bytes = b"`\n"
DEBIAN_BINARY_MEMBER: str = "debian-binary"
CONTROL_TAR_PREFIX: str = "control.tar"
DATA_TAR_PREFIX: str = "data.tar"

# Supported compression extensions for control/data tarballs
SUPPORTED_COMPRESSIONS: list[str] = [".gz", ".xz", ".bz2", ".zst", ""]

# Regex for valid Debian package filenames
# Format: <name>_<version>_<arch>.deb
DEB_FILENAME_PATTERN: re.Pattern[str] = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9.+\-]+)_"
    r"(?P<version>[a-zA-Z0-9.+\-:~]+)_"
    r"(?P<arch>[a-z0-9]+)\.deb$"
)

# Pre-release version suffixes for Debian packages (tilde convention)
PRERELEASE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"~alpha"),
    re.compile(r"~beta"),
    re.compile(r"~rc\d*"),
    re.compile(r"~pre"),
    re.compile(r"~dev"),
]

# Valid APT path patterns for validate_path()
_VALID_PATH_PATTERNS: list[re.Pattern[str]] = [
    # Pool package files: pool/<component>/<prefix>/<package>/<filename>.deb
    re.compile(r"^pool/[a-zA-Z0-9._\-]+/[a-zA-Z0-9]/[a-zA-Z0-9._+\-]+/[^/]+\.deb$"),
    # Release file
    re.compile(r"^dists/[a-zA-Z0-9._\-]+/Release$"),
    # InRelease file
    re.compile(r"^dists/[a-zA-Z0-9._\-]+/InRelease$"),
    # Release.gpg detached signature
    re.compile(r"^dists/[a-zA-Z0-9._\-]+/Release\.gpg$"),
    # Packages index (plain, .gz, .bz2)
    re.compile(
        r"^dists/[a-zA-Z0-9._\-]+/[a-zA-Z0-9._\-]+/"
        r"binary-[a-z0-9]+/Packages(?:\.gz|\.bz2)?$"
    ),
    # Sources index (plain, .gz)
    re.compile(
        r"^dists/[a-zA-Z0-9._\-]+/[a-zA-Z0-9._\-]+/"
        r"source/Sources(?:\.gz)?$"
    ),
]

# Metadata path regex for generate_metadata()
_PACKAGES_METADATA_RE: re.Pattern[str] = re.compile(
    r"^dists/(?P<dist>[^/]+)/(?P<comp>[^/]+)/"
    r"binary-(?P<arch>[^/]+)/(?P<filename>Packages(?:\.gz|\.bz2)?)$"
)

# Blueprint URL prefix for APT endpoints
APT_URL_PREFIX: str = "/repositories/apt"

# Default distribution/component for extraction fallback
DEFAULT_DISTRIBUTION: str = "stable"
DEFAULT_COMPONENT: str = "main"


# ===========================================================================
# AptFormatHandler
# ===========================================================================


class AptFormatHandler(FormatHandler):
    """APT/Debian format handler for Feature F-101-RQ-003.

    Handles ``.deb`` package upload/download via ``pool/`` paths and serves
    APT metadata via ``dists/`` paths (Packages, Release, InRelease).
    Supports the Distribution/Component/Architecture hierarchy defined by
    the Debian repository format specification.

    Replaces the Java APT format OSGi bundle from the original source system.

    **Coordinate Mapping:**

    - ``namespace`` → distribution name (e.g. ``'bionic'``, ``'focal'``)
    - ``name`` → Debian package name (``Package`` control field)
    - ``version`` → Debian version string (``Version`` control field)

    **Class Attributes:**

    - ``format_name`` — ``'apt'`` (matches ``Repository.format`` enum)
    - ``content_types`` — List of MIME types for ``.deb`` files
    - ``path_pattern`` — Regex matching ``pool/`` and ``dists/`` paths
    """

    format_name: str = FORMAT_NAME
    content_types: list[str] = DEB_CONTENT_TYPES
    path_pattern: str = r"^(pool|dists)/.*"

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self, signing_key_pem: bytes | None = None) -> None:
        """Initialise the APT format handler.

        Args:
            signing_key_pem: Optional PEM-encoded private key bytes for
                GPG-style signing of Release/InRelease files.  When
                ``None``, metadata is generated unsigned.
        """
        super().__init__()
        self._metadata_manager: AptMetadataManager = AptMetadataManager(
            signing_key_pem=signing_key_pem
        )
        self._packages_gen: PackagesIndexGenerator = PackagesIndexGenerator()
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.AptFormatHandler"
        )
        self.logger.info(
            "AptFormatHandler initialised (signing=%s)",
            "enabled" if signing_key_pem else "disabled",
        )

    # ==================================================================
    # Flask Blueprint Registration
    # ==================================================================

    @classmethod
    def get_blueprint(cls) -> Blueprint:
        """Return a Flask Blueprint with all APT repository route handlers.

        Registers endpoints for:

        - **Pool routes** — ``.deb`` package upload and download
        - **Dists routes** — APT metadata (Release, InRelease, Packages)
        - **Browse routes** — repository root listing

        Returns:
            A fully configured Flask Blueprint ready for registration.
        """
        bp: Blueprint = Blueprint(
            "apt_format", __name__, url_prefix=APT_URL_PREFIX
        )

        # -- Pool routes (package content) ---------------------------------

        @bp.route("/<repo_name>/pool/<path:filepath>", methods=["GET"])
        def handle_pool_download(repo_name: str, filepath: str) -> Response:
            """Download a .deb package from the repository pool."""
            handler = cls()
            path = f"pool/{filepath}"
            try:
                content, content_type, headers = handler.handle_download(
                    repo_name, path
                )
                return Response(
                    content, content_type=content_type, headers=headers
                )
            except ArtifactNotFoundError:
                abort(
                    404,
                    description=f"Package not found: {path}",
                )

        @bp.route("/<repo_name>/pool/<path:filepath>", methods=["PUT"])
        def handle_pool_upload(repo_name: str, filepath: str) -> Response:
            """Upload a .deb package to the repository pool."""
            handler = cls()
            content = request.data
            ct = request.content_type or DEB_CONTENT_TYPE
            path = f"pool/{filepath}"
            attributes = dict(request.args) if request.args else None
            try:
                result = handler.handle_upload(
                    repo_name, path, content, ct, attributes=attributes
                )
                return jsonify(result), 201
            except FormatValidationError as exc:
                abort(400, description=str(exc))

        # -- Dists routes (metadata) ----------------------------------------

        @bp.route(
            "/<repo_name>/dists/<distribution>/Release", methods=["GET"]
        )
        def handle_release(
            repo_name: str, distribution: str
        ) -> Response:
            """Serve the Release metadata file."""
            handler = cls()
            path = f"dists/{distribution}/Release"
            metadata = handler.generate_metadata(repo_name, path)
            if metadata is None:
                abort(404, description=f"Metadata not found: {path}")
            content, content_type = metadata
            return Response(content, content_type=content_type)

        @bp.route(
            "/<repo_name>/dists/<distribution>/InRelease", methods=["GET"]
        )
        def handle_inrelease(
            repo_name: str, distribution: str
        ) -> Response:
            """Serve the InRelease (inline-signed) metadata file."""
            handler = cls()
            path = f"dists/{distribution}/InRelease"
            metadata = handler.generate_metadata(repo_name, path)
            if metadata is None:
                abort(404, description=f"Metadata not found: {path}")
            content, content_type = metadata
            return Response(content, content_type=content_type)

        @bp.route(
            "/<repo_name>/dists/<distribution>/Release.gpg",
            methods=["GET"],
        )
        def handle_release_gpg(
            repo_name: str, distribution: str
        ) -> Response:
            """Serve the detached GPG signature for the Release file."""
            handler = cls()
            path = f"dists/{distribution}/Release.gpg"
            metadata = handler.generate_metadata(repo_name, path)
            if metadata is None:
                abort(404, description=f"Signature not available: {path}")
            content, content_type = metadata
            return Response(content, content_type=content_type)

        @bp.route(
            "/<repo_name>/dists/<distribution>/<component>"
            "/binary-<arch>/Packages",
            methods=["GET"],
        )
        def handle_packages(
            repo_name: str,
            distribution: str,
            component: str,
            arch: str,
        ) -> Response:
            """Serve the Packages index (uncompressed)."""
            handler = cls()
            path = (
                f"dists/{distribution}/{component}/binary-{arch}/Packages"
            )
            metadata = handler.generate_metadata(repo_name, path)
            if metadata is None:
                abort(404, description=f"Packages index not found: {path}")
            content, content_type = metadata
            return Response(content, content_type=content_type)

        @bp.route(
            "/<repo_name>/dists/<distribution>/<component>"
            "/binary-<arch>/Packages.gz",
            methods=["GET"],
        )
        def handle_packages_gz(
            repo_name: str,
            distribution: str,
            component: str,
            arch: str,
        ) -> Response:
            """Serve the gzip-compressed Packages index."""
            handler = cls()
            path = (
                f"dists/{distribution}/{component}"
                f"/binary-{arch}/Packages.gz"
            )
            metadata = handler.generate_metadata(repo_name, path)
            if metadata is None:
                abort(404, description=f"Packages index not found: {path}")
            content, content_type = metadata
            return Response(content, content_type=content_type)

        @bp.route(
            "/<repo_name>/dists/<distribution>/<component>"
            "/binary-<arch>/Packages.bz2",
            methods=["GET"],
        )
        def handle_packages_bz2(
            repo_name: str,
            distribution: str,
            component: str,
            arch: str,
        ) -> Response:
            """Serve the bzip2-compressed Packages index."""
            handler = cls()
            path = (
                f"dists/{distribution}/{component}"
                f"/binary-{arch}/Packages.bz2"
            )
            metadata = handler.generate_metadata(repo_name, path)
            if metadata is None:
                abort(404, description=f"Packages index not found: {path}")
            content, content_type = metadata
            return Response(content, content_type=content_type)

        # -- Browse / info routes -------------------------------------------

        @bp.route("/<repo_name>/", methods=["GET"])
        def handle_root(repo_name: str) -> Response:
            """Repository root listing showing available top-level paths."""
            return jsonify(
                {
                    "repository": repo_name,
                    "format": FORMAT_NAME,
                    "paths": ["dists/", "pool/"],
                }
            )

        return bp

    # ==================================================================
    # Artifact Upload / Download
    # ==================================================================

    def handle_upload(
        self,
        repository_name: str,
        path: str,
        content: bytes,
        content_type: str,
        attributes: dict | None = None,
    ) -> dict:
        """Handle a ``.deb`` package upload.

        Validates the content, extracts Debian control metadata, computes
        checksums, and returns a comprehensive metadata dictionary suitable
        for creating :class:`Component` and :class:`Asset` records.

        Args:
            repository_name: Target hosted repository name.
            path: Upload path (typically ``pool/<comp>/<pfx>/<pkg>/<f>.deb``).
            content: Raw binary content of the ``.deb`` file.
            content_type: MIME type supplied by the client.
            attributes: Optional additional metadata (e.g. distribution hint).

        Returns:
            Dictionary containing extracted coordinates, checksums, size,
            and the full Debian control metadata.

        Raises:
            FormatValidationError: If the content is not a valid ``.deb``.
        """
        # 1. Validate the .deb file structure
        if not self.validate_content(path, content):
            raise FormatValidationError(
                format_name=self.format_name,
                message="Invalid .deb file: failed structural validation",
                path=path,
            )

        # 2. Extract control metadata from inside the .deb archive
        control_metadata: dict = self._extract_deb_control(content)

        # 3. Extract component coordinates
        coordinates: dict = self.extract_coordinates(
            path, content=content
        )

        # 4. Determine the canonical pool path
        package_name: str = control_metadata.get(
            "Package", coordinates.get("name", "unknown")
        )
        version: str = control_metadata.get(
            "Version", coordinates.get("version", "0")
        )
        architecture: str = control_metadata.get("Architecture", "all")

        # Determine the component from attributes or default
        distribution: str = coordinates.get(
            "namespace", DEFAULT_DISTRIBUTION
        )
        component: str = DEFAULT_COMPONENT
        if attributes and "component" in attributes:
            component = attributes["component"]

        # Build canonical pool path: pool/<comp>/<prefix>/<pkg>/<file>.deb
        prefix: str = (
            "lib" + package_name[0]
            if package_name.startswith("lib") and len(package_name) > 3
            else package_name[0]
        )
        filename: str = f"{package_name}_{version}_{architecture}.deb"
        pool_path: str = f"pool/{component}/{prefix}/{package_name}/{filename}"

        # 5. Compute checksums
        checksums: dict[str, str] = self.compute_checksums(content)

        # 6. Compute file-level hashes compatible with APT metadata format
        file_hashes: dict[str, str] = compute_file_hashes(content)

        # 7. Build the result
        result: dict = {
            "namespace": distribution,
            "name": package_name,
            "version": version,
            "architecture": architecture,
            "path": pool_path,
            "content_type": DEB_CONTENT_TYPE,
            "size": len(content),
            "checksums": checksums,
            "file_hashes": file_hashes,
            "metadata": control_metadata,
        }

        self.logger.info(
            "Uploaded .deb package to '%s': %s %s %s (%d bytes)",
            repository_name,
            package_name,
            version,
            architecture,
            len(content),
        )
        return result

    def handle_download(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str, dict]:
        """Handle an artifact download request.

        For ``dists/`` metadata paths, generates the appropriate metadata
        on-the-fly.  For ``pool/`` package paths, raises
        :class:`ArtifactNotFoundError` as the actual blob retrieval is
        handled by the storage/service layer.

        Args:
            repository_name: Repository to download from.
            path: Artifact path within the repository.

        Returns:
            Tuple of ``(content_bytes, content_type, response_headers)``.

        Raises:
            ArtifactNotFoundError: If the artifact cannot be resolved by
                the format handler (pool paths require storage integration).
        """
        normalized: str = self.normalize_path(path)
        content_type: str = self.detect_content_type(normalized)

        # Attempt metadata generation for dists/ paths
        if normalized.startswith("dists/"):
            metadata_result = self.generate_metadata(
                repository_name, normalized
            )
            if metadata_result is not None:
                meta_content, meta_ct = metadata_result
                headers: dict[str, str] = {
                    "Cache-Control": "public, max-age=300",
                }
                self.logger.info(
                    "Serving metadata for '%s': %s (%d bytes)",
                    repository_name,
                    normalized,
                    len(meta_content),
                )
                return meta_content, meta_ct, headers

        # Pool paths — actual blob retrieval handled by service layer
        raise ArtifactNotFoundError(
            repository_name=repository_name,
            path=normalized,
        )

    # ==================================================================
    # Component Coordinate Extraction
    # ==================================================================

    def extract_coordinates(
        self,
        path: str,
        content: bytes | None = None,
    ) -> dict:
        """Extract APT-specific component coordinates.

        For ``.deb`` files with content available, extracts coordinates from
        the embedded Debian control file.  When content is not provided,
        falls back to parsing the filename pattern.

        Coordinate mapping (per AAP requirements):

        - ``namespace`` → distribution (from path or ``DEFAULT_DISTRIBUTION``)
        - ``name`` → package name (``Package`` control field)
        - ``version`` → Debian version (``Version`` control field)

        Args:
            path: Artifact path within the repository.
            content: Optional raw ``.deb`` file content.

        Returns:
            Dictionary with keys ``'namespace'``, ``'name'``, ``'version'``.
        """
        normalized: str = self.normalize_path(path)
        namespace: str = DEFAULT_DISTRIBUTION
        name: str = "unknown"
        version: str = "0"

        # Try to extract distribution from path structure
        # Expected: pool/<component>/<prefix>/<package>/<file>.deb
        # or dists/<distribution>/...
        parts: list[str] = normalized.split("/")
        if parts and parts[0] == "dists" and len(parts) > 1:
            namespace = parts[1]

        # Extract from content if available (most accurate)
        if content is not None and len(content) > DEB_MAGIC_LEN:
            try:
                control = self._extract_deb_control(content)
                name = control.get("Package", name)
                version = control.get("Version", version)
                # If Section is provided, could inform namespace
                return {
                    "namespace": namespace,
                    "name": name,
                    "version": version,
                }
            except (FormatValidationError, Exception) as exc:
                self.logger.warning(
                    "Failed to extract coordinates from content: %s", exc
                )

        # Fallback: parse from filename pattern
        filename: str = parts[-1] if parts else ""
        match = DEB_FILENAME_PATTERN.match(filename)
        if match:
            name = match.group("name")
            version = match.group("version")

        return {
            "namespace": namespace,
            "name": name,
            "version": version,
        }

    # ==================================================================
    # .deb File Parsing
    # ==================================================================

    def _extract_deb_control(self, content: bytes) -> dict:
        """Extract control metadata from inside a ``.deb`` file.

        Parses the ar archive structure, extracts the ``control.tar.*``
        member, opens the compressed tarball, reads the ``./control`` file,
        and parses it using :func:`parse_deb_control`.

        Args:
            content: Raw binary content of the ``.deb`` file.

        Returns:
            Dictionary of Debian control file fields.

        Raises:
            FormatValidationError: If the ``.deb`` structure is invalid
                or the control file cannot be extracted.
        """
        # Parse the ar archive
        members: dict[str, bytes] = self._parse_ar_archive(content)

        # Find the control tarball member
        control_tar_name: str | None = None
        control_tar_data: bytes | None = None
        for member_name, member_data in members.items():
            if member_name.startswith(CONTROL_TAR_PREFIX):
                control_tar_name = member_name
                control_tar_data = member_data
                break

        if control_tar_name is None or control_tar_data is None:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    "Invalid .deb file: no control.tar.* member found in "
                    "ar archive"
                ),
            )

        # Determine compression from the member name extension
        compression: str = ""
        suffix: str = control_tar_name[len(CONTROL_TAR_PREFIX):]
        if suffix in (".gz", ".xz", ".bz2", ".zst"):
            compression = suffix[1:]  # strip the leading dot

        # Extract the control file from the tarball
        control_bytes: bytes = self._extract_control_from_tarball(
            control_tar_data, compression
        )

        # Parse the control file into a dictionary
        return parse_deb_control(control_bytes)

    def _parse_ar_archive(self, data: bytes) -> dict[str, bytes]:
        """Parse an ar archive (the container format for ``.deb`` files).

        The ar archive format consists of:

        - 8-byte global header: ``!<arch>\\n``
        - For each member:
          - 60-byte header: name(16) + mtime(12) + uid(6) + gid(6) +
            mode(8) + size(10) + magic(2)
          - Content bytes (padded to even length)

        Uses :func:`struct.unpack` with format ``'16s12s6s6s8s10s2s'`` to
        parse the fixed-width member headers.

        Args:
            data: Raw bytes of the complete ar archive.

        Returns:
            Dictionary mapping member names (stripped) to their content bytes.

        Raises:
            FormatValidationError: If the data is not a valid ar archive.
        """
        if len(data) < DEB_MAGIC_LEN:
            raise FormatValidationError(
                format_name=self.format_name,
                message="Data too short to be a valid ar archive",
            )

        if data[:DEB_MAGIC_LEN] != DEB_MAGIC:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Invalid ar archive magic: expected {DEB_MAGIC!r}, "
                    f"got {data[:DEB_MAGIC_LEN]!r}"
                ),
            )

        members: dict[str, bytes] = {}
        offset: int = DEB_MAGIC_LEN

        while offset < len(data):
            # Need at least AR_HEADER_SIZE bytes for the member header
            if offset + AR_HEADER_SIZE > len(data):
                self.logger.warning(
                    "Truncated ar archive: %d bytes remaining at offset %d "
                    "(need %d for header)",
                    len(data) - offset,
                    offset,
                    AR_HEADER_SIZE,
                )
                break

            # Unpack the 60-byte ar member header using struct
            header_bytes: bytes = data[offset : offset + AR_HEADER_SIZE]
            (
                raw_name,
                raw_mtime,
                raw_uid,
                raw_gid,
                raw_mode,
                raw_size,
                raw_magic,
            ) = struct.unpack(AR_HEADER_FORMAT, header_bytes)

            # Validate member header magic
            if raw_magic != AR_HEADER_MAGIC:
                self.logger.warning(
                    "Invalid ar member header magic at offset %d: %r",
                    offset,
                    raw_magic,
                )
                break

            # Decode and clean up the name (strip spaces and trailing /)
            member_name: str = raw_name.decode("ascii", errors="replace")
            member_name = member_name.strip().rstrip("/")

            # Parse the size field
            try:
                member_size: int = int(
                    raw_size.decode("ascii", errors="replace").strip()
                )
            except ValueError:
                self.logger.warning(
                    "Invalid ar member size at offset %d: %r",
                    offset,
                    raw_size,
                )
                break

            # Extract member content
            content_offset: int = offset + AR_HEADER_SIZE
            if content_offset + member_size > len(data):
                self.logger.warning(
                    "Truncated ar member '%s': expected %d bytes at "
                    "offset %d, but only %d available",
                    member_name,
                    member_size,
                    content_offset,
                    len(data) - content_offset,
                )
                # Include what we can
                members[member_name] = data[content_offset:]
                break

            members[member_name] = data[
                content_offset : content_offset + member_size
            ]

            # Advance offset: header + content + optional padding byte
            # ar archives pad to even byte boundaries
            offset = content_offset + member_size
            if member_size % 2 != 0:
                offset += 1  # skip padding byte

        self.logger.debug(
            "Parsed ar archive: %d members (%s)",
            len(members),
            ", ".join(members.keys()),
        )
        return members

    def _extract_control_from_tarball(
        self, tarball_data: bytes, compression: str
    ) -> bytes:
        """Extract the ``./control`` file from a compressed tarball.

        Opens the control tarball (``control.tar.gz``, ``control.tar.xz``,
        or ``control.tar.bz2``) and reads the Debian control file
        containing package metadata.

        Args:
            tarball_data: Raw bytes of the compressed tarball.
            compression: Compression type — ``'gz'``, ``'xz'``, ``'bz2'``,
                or ``''`` for uncompressed.

        Returns:
            Raw bytes of the ``./control`` file.

        Raises:
            FormatValidationError: If the control file cannot be found
                or the tarball cannot be opened.
        """
        # Map compression string to tarfile mode
        mode_map: dict[str, str] = {
            "gz": "r:gz",
            "xz": "r:xz",
            "bz2": "r:bz2",
            "zst": "r:*",
            "": "r:",
        }
        mode: str = mode_map.get(compression, "r:*")

        try:
            tar_stream = io.BytesIO(tarball_data)
            with tarfile.open(fileobj=tar_stream, mode=mode) as tar:
                # Look for the control file — may be named ./control or control
                for member in tar.getmembers():
                    basename: str = member.name.lstrip("./")
                    if basename == "control" and member.isfile():
                        extracted = tar.extractfile(member)
                        if extracted is not None:
                            return extracted.read()

                # If not found by iteration, try direct access
                for candidate in ("./control", "control"):
                    try:
                        member_info = tar.getmember(candidate)
                        extracted = tar.extractfile(member_info)
                        if extracted is not None:
                            return extracted.read()
                    except KeyError:
                        continue

        except (tarfile.TarError, OSError, EOFError) as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Failed to open control tarball "
                    f"(compression={compression!r}): {exc}"
                ),
            ) from exc

        raise FormatValidationError(
            format_name=self.format_name,
            message=(
                "Control file not found inside control.tar archive — "
                "expected ./control or control"
            ),
        )

    # ==================================================================
    # Validation Methods
    # ==================================================================

    def validate_path(self, path: str) -> bool:
        """Validate that a path conforms to APT repository conventions.

        Accepts:
            - ``pool/<comp>/<prefix>/<pkg>/<file>.deb`` — Package files
            - ``dists/<dist>/Release`` — Release file
            - ``dists/<dist>/InRelease`` — Signed Release
            - ``dists/<dist>/Release.gpg`` — Detached GPG signature
            - ``dists/<dist>/<comp>/binary-<arch>/Packages[.gz|.bz2]``

        Args:
            path: Artifact path to validate.

        Returns:
            ``True`` if the path matches a valid APT pattern.
        """
        normalized: str = self.normalize_path(path)

        for pattern in _VALID_PATH_PATTERNS:
            if pattern.match(normalized):
                return True

        return False

    def validate_content(self, path: str, content: bytes) -> bool:
        """Validate ``.deb`` file structure.

        Performs structural validation of the ar archive:

        1. Content starts with ar archive magic bytes (``!<arch>\\n``)
        2. Archive contains a ``debian-binary`` member
        3. Archive contains a ``control.tar.*`` member
        4. Archive contains a ``data.tar.*`` member
        5. ``debian-binary`` content is ``"2.0\\n"``

        Args:
            path: Artifact path (provides context for logging).
            content: Raw binary content to validate.

        Returns:
            ``True`` if all structural checks pass, ``False`` otherwise.
        """
        # Check 1: ar archive magic
        if len(content) < DEB_MAGIC_LEN:
            self.logger.warning(
                "Content too short for .deb file (%d bytes): %s",
                len(content),
                path,
            )
            return False

        if content[:DEB_MAGIC_LEN] != DEB_MAGIC:
            self.logger.warning(
                "Invalid ar archive magic for %s: expected %r, got %r",
                path,
                DEB_MAGIC,
                content[:DEB_MAGIC_LEN],
            )
            return False

        # Parse the archive members
        try:
            members: dict[str, bytes] = self._parse_ar_archive(content)
        except FormatValidationError as exc:
            self.logger.warning(
                "Failed to parse ar archive for %s: %s", path, exc
            )
            return False

        # Check 2: debian-binary member
        if DEBIAN_BINARY_MEMBER not in members:
            self.logger.warning(
                "Missing '%s' member in .deb file: %s",
                DEBIAN_BINARY_MEMBER,
                path,
            )
            return False

        # Check 5: debian-binary content version
        deb_version: str = (
            members[DEBIAN_BINARY_MEMBER].decode("ascii", errors="replace").strip()
        )
        if deb_version != "2.0":
            self.logger.warning(
                "Unexpected debian-binary version '%s' in %s (expected '2.0')",
                deb_version,
                path,
            )
            return False

        # Check 3: control.tar.* member
        has_control: bool = any(
            name.startswith(CONTROL_TAR_PREFIX) for name in members
        )
        if not has_control:
            self.logger.warning(
                "Missing control.tar.* member in .deb file: %s", path
            )
            return False

        # Check 4: data.tar.* member
        has_data: bool = any(
            name.startswith(DATA_TAR_PREFIX) for name in members
        )
        if not has_data:
            self.logger.warning(
                "Missing data.tar.* member in .deb file: %s", path
            )
            return False

        self.logger.debug("Content validation passed for: %s", path)
        return True

    # ==================================================================
    # Metadata Generation
    # ==================================================================

    def generate_metadata(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str] | None:
        """Generate APT-specific metadata for a given path.

        Delegates to :class:`AptMetadataManager` to produce the requested
        metadata file.  Returns ``None`` for paths that are not metadata
        endpoints.

        Handled metadata paths:

        - ``dists/<dist>/Release`` → ``text/plain``
        - ``dists/<dist>/InRelease`` → ``text/plain``
        - ``dists/<dist>/Release.gpg`` → ``application/pgp-signature``
        - ``dists/<dist>/<comp>/binary-<arch>/Packages`` → ``text/plain``
        - ``dists/<dist>/<comp>/binary-<arch>/Packages.gz`` →
          ``application/gzip``
        - ``dists/<dist>/<comp>/binary-<arch>/Packages.bz2`` →
          ``application/x-bzip2``

        Args:
            repository_name: Name of the repository.
            path: Requested metadata path.

        Returns:
            Tuple of ``(metadata_bytes, content_type)`` or ``None`` if the
            path is not a metadata endpoint.
        """
        normalized: str = self.normalize_path(path)

        if not normalized.startswith("dists/"):
            return None

        parts: list[str] = normalized.split("/")
        if len(parts) < 3:
            return None

        distribution: str = parts[1]

        # --- Distribution-level metadata ---

        # dists/<dist>/Release
        if normalized == f"dists/{distribution}/Release":
            return self._generate_dist_metadata(
                distribution, f"dists/{distribution}/Release"
            ), "text/plain"

        # dists/<dist>/InRelease
        if normalized == f"dists/{distribution}/InRelease":
            return self._generate_dist_metadata(
                distribution, f"dists/{distribution}/InRelease"
            ), "text/plain"

        # dists/<dist>/Release.gpg
        if normalized == f"dists/{distribution}/Release.gpg":
            content = self._generate_dist_metadata(
                distribution, f"dists/{distribution}/Release.gpg"
            )
            if content is not None:
                return content, "application/pgp-signature"
            return None

        # --- Packages index metadata ---
        packages_match = _PACKAGES_METADATA_RE.match(normalized)
        if packages_match:
            dist: str = packages_match.group("dist")
            comp: str = packages_match.group("comp")
            arch: str = packages_match.group("arch")
            filename: str = packages_match.group("filename")

            content = self._generate_dist_metadata(
                dist,
                f"dists/{dist}/{comp}/binary-{arch}/{filename}",
                components=[comp],
                architectures=[arch],
            )

            content_type: str = "text/plain"
            if filename == "Packages.gz":
                content_type = "application/gzip"
            elif filename == "Packages.bz2":
                content_type = "application/x-bzip2"

            if content is not None:
                return content, content_type
            return None

        return None

    def _generate_dist_metadata(
        self,
        distribution: str,
        target_key: str,
        components: list[str] | None = None,
        architectures: list[str] | None = None,
    ) -> bytes | None:
        """Generate all distribution metadata and return the requested file.

        Calls :meth:`AptMetadataManager.generate_repository_metadata` with
        the given parameters and returns the content for *target_key*.

        Args:
            distribution: Distribution name (e.g. ``'bionic'``).
            target_key: The specific metadata file path to return.
            components: Optional list of components (defaults to ``['main']``).
            architectures: Optional list of architectures (defaults to
                ``['amd64', 'all']``).

        Returns:
            Raw bytes content for the target file, or ``None`` if not found.
        """
        comps: list[str] = components or [DEFAULT_COMPONENT]
        archs: list[str] = architectures or ["amd64", "all"]

        all_metadata: dict[str, bytes] = (
            self._metadata_manager.generate_repository_metadata(
                distribution=distribution,
                components=comps,
                architectures=archs,
                packages=[],
            )
        )
        return all_metadata.get(target_key)

    # ==================================================================
    # Pre-release Detection
    # ==================================================================

    def is_prerelease(self, component_version: str) -> bool:
        """Detect Debian pre-release versions using tilde convention.

        In Debian versioning, the tilde (``~``) character sorts *before*
        any other character, making ``1.0~rc1`` sort before ``1.0``.  This
        convention is used for pre-release versions.

        Detected patterns:
            - ``~alpha``
            - ``~beta``
            - ``~rc`` (with optional numeric suffix)
            - ``~pre``
            - ``~dev``

        Args:
            component_version: The version string to evaluate.

        Returns:
            ``True`` if the version contains a pre-release indicator.
        """
        if not component_version:
            return False

        for pattern in PRERELEASE_PATTERNS:
            if pattern.search(component_version):
                return True

        return False

    # ==================================================================
    # Content Type Detection
    # ==================================================================

    def detect_content_type(self, path: str) -> str:
        """Detect MIME type with APT-specific content type rules.

        Overrides the base class to provide APT-aware detection for
        Debian package files, Packages indices, Release files, and
        GPG signature files.

        Detection rules (checked in order):

        1. ``*.deb`` → ``application/vnd.debian.binary-package``
        2. ``Packages`` → ``text/plain``
        3. ``Packages.gz`` → ``application/gzip``
        4. ``Packages.bz2`` → ``application/x-bzip2``
        5. ``Release``, ``InRelease`` → ``text/plain``
        6. ``Release.gpg`` → ``application/pgp-signature``
        7. ``Sources`` → ``text/plain``
        8. ``Sources.gz`` → ``application/gzip``
        9. Fallback to :func:`mimetypes.guess_type`
        10. Default: ``application/octet-stream``

        Args:
            path: Artifact path to detect content type for.

        Returns:
            MIME type string.
        """
        basename: str = path.rsplit("/", maxsplit=1)[-1] if "/" in path else path

        # APT-specific content type mapping
        apt_type_map: dict[str, str] = {
            "Packages": "text/plain",
            "Packages.gz": "application/gzip",
            "Packages.bz2": "application/x-bzip2",
            "Release": "text/plain",
            "InRelease": "text/plain",
            "Release.gpg": "application/pgp-signature",
            "Sources": "text/plain",
            "Sources.gz": "application/gzip",
        }

        # Check for exact basename match
        if basename in apt_type_map:
            return apt_type_map[basename]

        # Check for .deb extension
        if basename.endswith(".deb"):
            return DEB_CONTENT_TYPE

        # Fallback to mimetypes stdlib
        guessed, _ = mimetypes.guess_type(path, strict=False)
        if guessed is not None:
            return guessed

        return "application/octet-stream"

    # ==================================================================
    # Group Repository Metadata Merging
    # ==================================================================

    def merge_metadata(self, metadata_list: list[bytes]) -> bytes | None:
        """Merge Packages index files from multiple group member repositories.

        Concatenates all Packages entries from the provided index contents,
        deduplicates by ``(Package, Version, Architecture)`` tuple, sorts
        the combined entries, and returns the merged Packages index.

        Args:
            metadata_list: List of raw ``Packages`` index bytes from each
                member repository.

        Returns:
            Merged ``Packages`` index bytes, or ``None`` if the input list
            is empty.
        """
        if not metadata_list:
            return None

        # Parse all stanzas from all member repositories
        seen: set[tuple[str, str, str]] = set()
        unique_entries: list[dict[str, str]] = []

        for metadata_bytes in metadata_list:
            try:
                text: str = metadata_bytes.decode("utf-8")
            except UnicodeDecodeError:
                text = metadata_bytes.decode("latin-1")

            # Split into stanzas (separated by blank lines)
            stanzas: list[str] = text.strip().split("\n\n")

            for stanza in stanzas:
                stanza = stanza.strip()
                if not stanza:
                    continue

                # Parse the stanza into a dict
                fields: dict[str, str] = {}
                current_key: str | None = None
                current_lines: list[str] = []

                for line in stanza.split("\n"):
                    if line and line[0] in (" ", "\t"):
                        # Continuation line
                        if current_key is not None:
                            current_lines.append(line)
                        continue

                    # Flush previous field
                    if current_key is not None:
                        fields[current_key] = "\n".join(current_lines)

                    colon_idx: int = line.find(":")
                    if colon_idx != -1:
                        current_key = line[:colon_idx].strip()
                        current_lines = [line[colon_idx + 1:].strip()]
                    else:
                        current_key = None
                        current_lines = []

                if current_key is not None:
                    fields[current_key] = "\n".join(current_lines)

                if not fields:
                    continue

                # Deduplicate by (Package, Version, Architecture)
                dedup_key: tuple[str, str, str] = (
                    fields.get("Package", ""),
                    fields.get("Version", ""),
                    fields.get("Architecture", ""),
                )
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    unique_entries.append(fields)

        if not unique_entries:
            return None

        # Sort by (Package, Version, Architecture)
        unique_entries.sort(
            key=lambda f: (
                f.get("Package", ""),
                f.get("Version", ""),
                f.get("Architecture", ""),
            )
        )

        # Generate the merged Packages index using the PackagesIndexGenerator
        merged_content: bytes = self._packages_gen.generate_packages_index(
            unique_entries
        )

        self.logger.info(
            "Merged Packages index: %d unique entries from %d sources",
            len(unique_entries),
            len(metadata_list),
        )
        return merged_content

    # ==================================================================
    # Proxy URL Construction
    # ==================================================================

    def get_remote_url(self, remote_base_url: str, path: str) -> str:
        """Construct the full remote URL for proxy repository fetches.

        Ensures the path includes ``dists/`` or ``pool/`` correctly and
        joins it with the remote base URL, handling trailing slashes.

        Args:
            remote_base_url: Base URL of the remote APT repository
                (e.g. ``'http://archive.ubuntu.com/ubuntu'``).
            path: Artifact path to fetch (e.g.
                ``'dists/focal/main/binary-amd64/Packages.gz'``).

        Returns:
            Fully qualified remote URL.
        """
        normalized: str = self.normalize_path(path)
        base: str = remote_base_url.rstrip("/")

        if not normalized:
            return base

        return f"{base}/{normalized}"
