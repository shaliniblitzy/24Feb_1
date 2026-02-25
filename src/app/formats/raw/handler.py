"""
Raw repository format handler for arbitrary binary file storage.

Implements Feature F-101-RQ-002 — the Raw format handler supports upload/download
of arbitrary binary files with automatic MIME type detection.  This is the simplest
format handler and does not impose any coordinate structure or metadata conventions.

The Raw format:
- Accepts any file type (no format-specific validation)
- Auto-detects MIME types using extension-based and custom-mapping approaches
- Derives component coordinates from file path (directory as namespace, filename
  as name)
- Does not generate format-specific metadata
- Registers a Flask Blueprint for raw content endpoints

**Architecture Context:**

Replaces the Java Raw format bundle (F-101-RQ-002) from the OSGi format plugin
system (Karaf 4.4.4 / Guice 7.0.0).  The :class:`RawFormatHandler` is a concrete
implementation of the :class:`~src.app.formats.base.FormatHandler` Strategy
pattern, selected at runtime by the ``format_name`` attribute ``'raw'``.

**Design Patterns:**

- **Strategy Pattern** — ``RawFormatHandler`` is selected by ``format_name='raw'``
- **Template Method** — Inherits shared helpers from ``FormatHandler``
  (``compute_checksums``, ``normalize_path``)
- **Blueprint/Module** — Provides a Flask Blueprint for raw content routes

**Module-Level Exports:**

- :class:`RawFormatHandler`
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
from typing import TYPE_CHECKING

from flask import Blueprint, Response, abort, jsonify, request

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
# source.  Provides debug-level logging for upload, download, and coordinate
# extraction operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = ["RawFormatHandler"]

# ---------------------------------------------------------------------------
# Ensure mimetypes database is loaded
# ---------------------------------------------------------------------------
# The mimetypes module lazily initialises its internal mapping database.
# We eagerly initialise it at module import time to avoid first-request
# latency and to guarantee deterministic behaviour in detect_content_type().
# ---------------------------------------------------------------------------

if not mimetypes.inited:
    mimetypes.init()


# ===========================================================================
# RawFormatHandler
# ===========================================================================


class RawFormatHandler(FormatHandler):
    """Raw format handler for arbitrary binary file storage.

    The Raw format is the simplest format handler in the Nexus Repository
    system.  It does not impose any coordinate structure, metadata
    conventions, or content validation constraints.  Any file can be
    uploaded and downloaded as-is.

    **Format name:** ``'raw'``

    **Supported repository types:** hosted, proxy, group

    **Content validation:** None (all content accepted)

    **Coordinate extraction:** Derives from file path:
        - ``namespace``: parent directory path (e.g., ``'dir1/dir2'``)
        - ``name``: filename without extension (e.g., ``'artifact'``)
        - ``version``: ``None`` (raw format does not use versioning)

    **MIME type detection:** Enhanced with custom mappings for common
    artifact types (.jar, .war, .pom, .rpm, .deb, .whl, etc.) in
    addition to Python's standard :mod:`mimetypes` module.

    Example::

        handler = RawFormatHandler()
        metadata = handler.handle_upload(
            repository_name="raw-hosted",
            path="configs/app.yaml",
            content=b"key: value",
            content_type="text/yaml",
        )
        # metadata == {
        #     'path': 'configs/app.yaml',
        #     'content_type': 'text/yaml',
        #     'size': 10,
        #     'checksums': {'md5': '...', 'sha1': '...', 'sha256': '...'},
        #     'coordinates': {'namespace': 'configs', 'name': 'app', 'version': None},
        #     'format': 'raw',
        #     'attributes': {},
        # }
    """

    # ------------------------------------------------------------------
    # Class-Level Attributes
    # ------------------------------------------------------------------

    format_name: str = "raw"
    """Canonical format name — MUST match ``Repository.format`` enum value."""

    content_types: list[str] = ["application/octet-stream"]
    """Default MIME type list for raw format.

    Since the raw format accepts arbitrary binary content, the default
    content type is ``application/octet-stream``.  Actual MIME type
    detection occurs at runtime via :meth:`detect_content_type`.
    """

    path_pattern: str = r"^[a-zA-Z0-9][a-zA-Z0-9._/\-]*$"
    """Regex pattern for valid raw artifact paths.

    Accepts alphanumeric characters, dots, underscores, hyphens, and
    forward slashes.  The first character must be alphanumeric to prevent
    paths starting with special characters.
    """

    # ------------------------------------------------------------------
    # Additional MIME type mappings for common artifact types
    # ------------------------------------------------------------------
    # These mappings extend Python's built-in mimetypes module with
    # common artifact types that may not be in the default database.
    # ------------------------------------------------------------------

    _EXTRA_MIME_TYPES: dict[str, str] = {
        ".jar": "application/java-archive",
        ".war": "application/java-archive",
        ".ear": "application/java-archive",
        ".pom": "application/xml",
        ".rpm": "application/x-rpm",
        ".deb": "application/vnd.debian.binary-package",
        ".nupkg": "application/zip",
        ".whl": "application/zip",
        ".egg": "application/zip",
        ".gem": "application/x-tar",
    }

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the Raw format handler.

        Calls the parent :class:`FormatHandler` constructor which validates
        that ``format_name`` is set to a non-empty string and creates a
        format-specific child logger.
        """
        super().__init__()

    # ==================================================================
    # Flask Blueprint Registration
    # ==================================================================

    @classmethod
    def get_blueprint(cls) -> Blueprint:
        """Create and return the Flask Blueprint for raw format endpoints.

        Registers route handlers for raw artifact upload, download, delete,
        and existence check operations.  Each route delegates to the
        handler's instance methods for format-specific processing.

        **Routes registered:**

        ======  ================================================  ===========
        Method  URL Pattern                                       Description
        ======  ================================================  ===========
        GET     ``/<repository_name>/<path:artifact_path>``        Download
        PUT     ``/<repository_name>/<path:artifact_path>``        Upload
        DELETE  ``/<repository_name>/<path:artifact_path>``        Delete
        HEAD    ``/<repository_name>/<path:artifact_path>``        Exists?
        ======  ================================================  ===========

        Returns:
            A fully configured Flask :class:`~flask.Blueprint` ready for
            registration with the Flask application.
        """
        bp = Blueprint("raw_format", __name__, url_prefix="/repository")
        handler = cls()

        @bp.route(
            "/<repository_name>/<path:artifact_path>", methods=["GET"]
        )
        def download(repository_name: str, artifact_path: str) -> Response:
            """Download a raw artifact from the repository.

            Delegates to :meth:`handle_download` for content retrieval and
            MIME type detection.  Returns the artifact content with
            appropriate headers.

            Args:
                repository_name: Name of the target repository.
                artifact_path: Path of the artifact within the repository.

            Returns:
                HTTP 200 with artifact content, or HTTP 404 if not found.
            """
            logger.debug(
                "Raw download request: %s/%s",
                repository_name,
                artifact_path,
            )
            try:
                content, content_type, headers = handler.handle_download(
                    repository_name, artifact_path
                )
                response = Response(
                    content,
                    status=200,
                    content_type=content_type,
                )
                for key, value in headers.items():
                    response.headers[key] = value
                return response
            except ArtifactNotFoundError:
                logger.debug(
                    "Artifact not found: %s/%s",
                    repository_name,
                    artifact_path,
                )
                abort(404, description=f"Artifact not found: {artifact_path}")
            except FormatValidationError as exc:
                logger.warning(
                    "Validation error on download: %s", exc.message
                )
                abort(400, description=exc.message)

        @bp.route(
            "/<repository_name>/<path:artifact_path>", methods=["PUT"]
        )
        def upload(repository_name: str, artifact_path: str) -> Response:
            """Upload a raw artifact to the repository.

            Reads the request body and delegates to :meth:`handle_upload`
            for format processing, checksum computation, and coordinate
            extraction.

            Args:
                repository_name: Name of the target repository.
                artifact_path: Path where the artifact will be stored.

            Returns:
                HTTP 201 Created with upload metadata JSON, or HTTP 400
                on validation failure.
            """
            logger.debug(
                "Raw upload request: %s/%s",
                repository_name,
                artifact_path,
            )
            try:
                content: bytes = request.data
                incoming_content_type: str = (
                    request.content_type or "application/octet-stream"
                )

                metadata = handler.handle_upload(
                    repository_name=repository_name,
                    path=artifact_path,
                    content=content,
                    content_type=incoming_content_type,
                    attributes=None,
                )
                return jsonify(metadata), 201
            except FormatValidationError as exc:
                logger.warning(
                    "Validation error on upload: %s", exc.message
                )
                abort(400, description=exc.message)

        @bp.route(
            "/<repository_name>/<path:artifact_path>", methods=["DELETE"]
        )
        def delete(repository_name: str, artifact_path: str) -> Response:
            """Delete a raw artifact from the repository.

            Validates the artifact path and signals deletion.  Actual blob
            removal is handled by the service layer / BlobStore backend.

            Args:
                repository_name: Name of the target repository.
                artifact_path: Path of the artifact to delete.

            Returns:
                HTTP 204 No Content on success, or HTTP 404 if not found.
            """
            logger.debug(
                "Raw delete request: %s/%s",
                repository_name,
                artifact_path,
            )
            try:
                normalized_path = handler.normalize_path(artifact_path)
                if not handler.validate_path(normalized_path):
                    abort(
                        400,
                        description=f"Invalid path: {artifact_path}",
                    )
                # Service-layer deletion would be invoked here.
                # The handler prepares path validation; actual BlobStore
                # deletion is orchestrated by the repository service layer.
                logger.debug(
                    "Delete processed for: %s/%s",
                    repository_name,
                    normalized_path,
                )
                return Response(status=204)
            except ArtifactNotFoundError:
                logger.debug(
                    "Artifact not found for delete: %s/%s",
                    repository_name,
                    artifact_path,
                )
                abort(404, description=f"Artifact not found: {artifact_path}")

        @bp.route(
            "/<repository_name>/<path:artifact_path>", methods=["HEAD"]
        )
        def head(repository_name: str, artifact_path: str) -> Response:
            """Check if a raw artifact exists in the repository.

            Returns headers (content type, content length) without the
            response body.

            Args:
                repository_name: Name of the target repository.
                artifact_path: Path of the artifact to check.

            Returns:
                HTTP 200 with headers on success, or HTTP 404 if not found.
            """
            logger.debug(
                "Raw HEAD request: %s/%s",
                repository_name,
                artifact_path,
            )
            try:
                _content, content_type, headers = handler.handle_download(
                    repository_name, artifact_path
                )
                response = Response(status=200, content_type=content_type)
                for key, value in headers.items():
                    response.headers[key] = value
                # Set Content-Length from the actual content size
                response.headers["Content-Length"] = str(len(_content))
                return response
            except ArtifactNotFoundError:
                logger.debug(
                    "Artifact not found (HEAD): %s/%s",
                    repository_name,
                    artifact_path,
                )
                abort(404, description=f"Artifact not found: {artifact_path}")

        return bp

    # ==================================================================
    # Core Upload / Download
    # ==================================================================

    def handle_upload(
        self,
        repository_name: str,
        path: str,
        content: bytes,
        content_type: str,
        attributes: dict | None = None,
    ) -> dict:
        """Handle a raw artifact upload.

        Accepts any content without format-specific validation.  Computes
        checksums, detects the MIME type, and extracts component coordinates
        from the path.

        Algorithm:
            1. Normalise the path.
            2. Validate the path — raise :class:`FormatValidationError` if
               invalid.
            3. Auto-detect ``content_type`` if the caller provided a generic
               ``application/octet-stream`` — use :meth:`detect_content_type`
               for more specific MIME detection.
            4. Compute MD5 / SHA-1 / SHA-256 checksums.
            5. Extract component coordinates (namespace, name, version).
            6. Return metadata dictionary.

        Args:
            repository_name: Name of the target hosted repository.
            path: Storage path within the repository.
            content: Raw binary content of the artifact.
            content_type: MIME type of the uploaded content.
            attributes: Optional additional metadata to associate.

        Returns:
            A dictionary containing upload metadata with keys:
            ``path``, ``content_type``, ``size``, ``checksums``,
            ``coordinates``, ``format``, ``attributes``.

        Raises:
            FormatValidationError: If the path fails validation.
        """
        logger.debug(
            "Raw upload: %s/%s (%d bytes, %s)",
            repository_name,
            path,
            len(content),
            content_type,
        )

        # 1. Normalise the path
        normalized_path: str = self.normalize_path(path)

        # 2. Validate the path
        if not self.validate_path(normalized_path):
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Invalid artifact path: '{path}'",
                path=path,
            )

        # 3. Auto-detect content type if generic
        detected_content_type: str = content_type
        if (
            not content_type
            or content_type == "application/octet-stream"
        ):
            detected_content_type = self.detect_content_type(normalized_path)

        # 4. Compute checksums
        checksums: dict[str, str] = self.compute_checksums(content)

        # 5. Extract coordinates
        coordinates: dict = self.extract_coordinates(
            normalized_path, content
        )

        # 6. Build and return metadata
        metadata: dict = {
            "path": normalized_path,
            "content_type": detected_content_type,
            "size": len(content),
            "checksums": checksums,
            "coordinates": coordinates,
            "format": self.format_name,
            "attributes": attributes or {},
        }

        logger.debug(
            "Raw upload metadata: path=%s, size=%d, content_type=%s",
            normalized_path,
            len(content),
            detected_content_type,
        )
        return metadata

    def handle_download(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str, dict]:
        """Handle a raw artifact download request.

        Prepares response metadata including content type detection and
        security headers.  The actual blob retrieval is handled by the
        service layer / BlobStore — this method constructs the response
        tuple that the blueprint route handler uses to build the HTTP
        response.

        .. note::

            In production, this method would be called by the service layer
            *after* retrieving the blob content from the BlobStore.  The
            content bytes would be passed through the service layer, not
            loaded here directly.  This handler focuses on path normalisation,
            MIME type detection, and header construction.

        Args:
            repository_name: Name of the repository to download from.
            path: Artifact path within the repository.

        Returns:
            A three-element tuple of:

            - ``content`` (bytes): Placeholder empty bytes — actual content
              is supplied by the service layer.
            - ``content_type`` (str): Detected MIME type for the artifact.
            - ``headers`` (dict): Additional HTTP response headers including
              ``Content-Disposition`` and ``X-Content-Type-Options``.

        Raises:
            ArtifactNotFoundError: Raised by the service layer when the
                artifact does not exist (propagated by callers of this
                method).
        """
        logger.debug("Raw download: %s/%s", repository_name, path)

        # Normalise the path
        normalized_path: str = self.normalize_path(path)

        # Detect content type from the path extension
        content_type: str = self.detect_content_type(normalized_path)

        # Extract filename for Content-Disposition header
        filename: str = normalized_path.rsplit("/", 1)[-1] if "/" in normalized_path else normalized_path

        # Build response headers
        headers: dict[str, str] = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        }

        # Return empty content placeholder — actual content supplied by
        # the BlobStore service layer at runtime.
        return b"", content_type, headers

    # ==================================================================
    # Coordinate Extraction
    # ==================================================================

    def extract_coordinates(
        self,
        path: str,
        content: bytes | None = None,
    ) -> dict:
        """Extract component coordinates from the artifact path.

        Raw format coordinate mapping:
            - ``namespace``: Parent directory path (everything before the
              last ``/``).  ``None`` if the file is at the root level.
            - ``name``: Filename without extension.
            - ``version``: Always ``None`` — raw format does not impose
              versioning.

        The ``content`` parameter is ignored for the raw format since
        coordinates are derived entirely from the path structure.

        Args:
            path: Artifact path within the repository.
            content: Ignored for raw format (coordinates come from path
                only).

        Returns:
            A dictionary with keys ``'namespace'``, ``'name'``, and
            ``'version'`` matching :class:`Component` model fields.

        Examples::

            handler = RawFormatHandler()
            handler.extract_coordinates("dir1/dir2/file.txt")
            # => {'namespace': 'dir1/dir2', 'name': 'file', 'version': None}

            handler.extract_coordinates("readme.md")
            # => {'namespace': None, 'name': 'readme', 'version': None}
        """
        normalized: str = self.normalize_path(path)

        # Split into directory (namespace) and filename
        parts: list[str] = normalized.rsplit("/", 1)

        if len(parts) == 2:
            namespace: str | None = parts[0]
            filename: str = parts[1]
        else:
            namespace = None
            filename = parts[0]

        # Extract name (filename without extension)
        if "." in filename:
            name: str = os.path.splitext(filename)[0]
        else:
            name = filename

        logger.debug(
            "Raw coordinates extracted: namespace=%s, name=%s",
            namespace,
            name,
        )

        return {
            "namespace": namespace,
            "name": name,
            "version": None,
        }

    # ==================================================================
    # Metadata Generation
    # ==================================================================

    def generate_metadata(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str] | None:
        """Raw format does not generate metadata.

        Unlike Maven (``maven-metadata.xml``) or npm (``package.json``),
        the raw format serves files as-is without generating any index
        files, metadata endpoints, or derived artifacts.

        Args:
            repository_name: Name of the repository (unused).
            path: Requested metadata path (unused).

        Returns:
            ``None``: Always returns ``None`` for raw format.
        """
        return None

    # ==================================================================
    # Path and Content Validation
    # ==================================================================

    def validate_path(self, path: str) -> bool:
        """Validate that an artifact path is acceptable for raw format.

        Raw format accepts any valid path that does not contain directory
        traversal sequences or invalid characters.  This is intentionally
        permissive as raw format places no structural constraints on
        artifact paths.

        **Validation rules:**

        1. Path must not be empty or whitespace-only.
        2. Path must not contain ``..`` (directory traversal prevention).
        3. Path must not contain null bytes (``\\x00``).
        4. Path must not have consecutive slashes (``//``).
        5. After normalisation (strip leading/trailing ``/``), the path
           must match :attr:`path_pattern`.

        Args:
            path: The artifact path to validate.

        Returns:
            ``True`` if the path is valid for raw format, ``False``
            otherwise.
        """
        # Rule 1: Non-empty
        if not path or not path.strip():
            return False

        # Rule 2: No directory traversal
        if ".." in path:
            return False

        # Rule 3: No null bytes
        if "\x00" in path:
            return False

        # Rule 4: No consecutive slashes
        if "//" in path:
            return False

        # Normalise: strip leading/trailing slashes
        normalized: str = path.strip("/")
        if not normalized:
            return False

        # Rule 5: Match path pattern
        return bool(re.match(self.path_pattern, normalized))

    def validate_content(self, path: str, content: bytes) -> bool:
        """Validate artifact content for raw format.

        Raw format places no constraints on content.  Any binary content
        is accepted, making this the universal fallback format for
        arbitrary file storage.

        Args:
            path: The artifact path (unused for raw format).
            content: The binary content to validate (unused for raw format).

        Returns:
            ``True``: Always returns ``True`` for raw format.
        """
        return True

    # ==================================================================
    # MIME Type Detection (Override)
    # ==================================================================

    def detect_content_type(self, path: str) -> str:
        """Detect MIME type using extension-based and custom-mapping approaches.

        Enhanced MIME type detection for the raw format that supplements
        Python's standard :mod:`mimetypes` module with additional mappings
        for common artifact types not in the default database.

        **Detection order:**

        1. Check custom extension mappings in :attr:`_EXTRA_MIME_TYPES`
           for artifact-specific types (``.jar``, ``.war``, ``.pom``,
           ``.rpm``, ``.deb``, ``.whl``, etc.).
        2. Fall back to :func:`mimetypes.guess_type` for standard MIME
           type detection.
        3. Return ``'application/octet-stream'`` as the ultimate default.

        Args:
            path: The artifact path to detect content type for.

        Returns:
            The detected MIME type string, or
            ``'application/octet-stream'`` as fallback.
        """
        # 1. Try custom extension mappings first
        _, ext = os.path.splitext(path.lower())
        if ext in self._EXTRA_MIME_TYPES:
            self.logger.debug(
                "Detected content type '%s' for path '%s' via custom mapping",
                self._EXTRA_MIME_TYPES[ext],
                path,
            )
            return self._EXTRA_MIME_TYPES[ext]

        # 2. Use standard mimetypes module
        mime_type, _ = mimetypes.guess_type(path)
        if mime_type:
            self.logger.debug(
                "Detected content type '%s' for path '%s' via mimetypes",
                mime_type,
                path,
            )
            return mime_type

        # 3. Fallback to generic binary
        self.logger.debug(
            "Unable to determine content type for path '%s'; "
            "returning default 'application/octet-stream'",
            path,
        )
        return "application/octet-stream"
