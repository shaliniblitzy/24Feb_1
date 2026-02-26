"""
Docker format handler for Nexus Repository.

Implements the FormatHandler interface for Docker container images,
supporting the Docker Registry HTTP API V2 specification. Handles
Docker image manifest and layer (blob) upload/download with support
for Docker V2 Schema 2 and OCI Image Manifest formats.

Replaces the Docker format bundle from the Java source Nexus Repository system.
Feature: F-101-RQ-005

Coordinate extraction:
- namespace: registry namespace (e.g., 'library' for official images)
- name: image name (e.g., 'nginx')
- version: tag (e.g., 'latest', '1.25.3') or digest (e.g., 'sha256:abc...')

**Architecture Context:**

In the original Java system, Docker support was provided by the Docker format
OSGi bundle registered via the Karaf 4.4.4 module container.  This module
reimplements the same functionality as a :class:`FormatHandler` subclass with
a Flask Blueprint for the Docker Registry HTTP API V2.

**Design Patterns:**

- **Strategy Pattern** — ``DockerFormatHandler`` is a concrete strategy
  selected by ``format_name = 'docker'`` from the format registry.
- **Template Method** — Inherits shared helpers (``compute_checksums``,
  ``detect_content_type``, ``normalize_path``) from :class:`FormatHandler`.
- **Abstract Factory** — ``get_blueprint()`` returns the pre-configured
  Docker V2 Registry API Blueprint from ``registry_v2.py``.

**Docker V2 Specification Support:**

- Docker V2 Schema 2 manifests (``application/vnd.docker.distribution.manifest.v2+json``)
- Docker manifest lists (``application/vnd.docker.distribution.manifest.list.v2+json``)
- OCI Image Manifests (``application/vnd.oci.image.manifest.v1+json``)
- OCI Image Indexes (``application/vnd.oci.image.index.v1+json``)
- Content-addressable storage via SHA-256 digests for all blobs

Module-Level Exports:
    - :class:`DockerFormatHandler`
    - :data:`DOCKER_FORMAT_NAME`
    - :data:`DOCKER_CONTENT_TYPES`
    - :data:`MANIFEST_V2_TYPE`
    - :data:`MANIFEST_LIST_TYPE`
    - :data:`OCI_MANIFEST_TYPE`
    - :data:`OCI_INDEX_TYPE`
    - :data:`CONFIG_TYPE`
    - :data:`DIGEST_PATTERN`
    - :data:`TAG_PATTERN`
    - :data:`IMAGE_PATH_PATTERN`
    - :data:`DEFAULT_NAMESPACE`
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, TYPE_CHECKING

from flask import Blueprint

from src.app.formats.base import FormatHandler, FormatValidationError

if TYPE_CHECKING:
    from src.app.models.repository import Repository
    from src.app.models.component import Component
    from src.app.models.asset import Asset

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source.  Provides module-level diagnostics for the Docker format handler.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "DockerFormatHandler",
    "DOCKER_FORMAT_NAME",
    "DOCKER_CONTENT_TYPES",
    "MANIFEST_V2_TYPE",
    "MANIFEST_LIST_TYPE",
    "OCI_MANIFEST_TYPE",
    "OCI_INDEX_TYPE",
    "CONFIG_TYPE",
    "DIGEST_PATTERN",
    "TAG_PATTERN",
    "IMAGE_PATH_PATTERN",
    "DEFAULT_NAMESPACE",
]

# ===========================================================================
# Constants — Docker Media Types and Patterns
# ===========================================================================

# Docker Registry format identifier — matches Repository.format enum value 'docker'
DOCKER_FORMAT_NAME: str = "docker"
"""Canonical format name matching the ``Repository.format`` column enum."""

# ---------------------------------------------------------------------------
# Supported manifest media types
# ---------------------------------------------------------------------------

MANIFEST_V2_TYPE: str = "application/vnd.docker.distribution.manifest.v2+json"
"""Docker V2 Schema 2 image manifest media type."""

MANIFEST_LIST_TYPE: str = (
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
"""Docker V2 Schema 2 manifest list (multi-architecture) media type."""

OCI_MANIFEST_TYPE: str = "application/vnd.oci.image.manifest.v1+json"
"""OCI Image Manifest media type (equivalent to Docker V2 Schema 2)."""

OCI_INDEX_TYPE: str = "application/vnd.oci.image.index.v1+json"
"""OCI Image Index media type (equivalent to Docker manifest list)."""

CONFIG_TYPE: str = "application/vnd.docker.container.image.v1+json"
"""Docker container image configuration media type (config JSON blob)."""

# ---------------------------------------------------------------------------
# Aggregated content type list
# ---------------------------------------------------------------------------

DOCKER_CONTENT_TYPES: list[str] = [
    MANIFEST_V2_TYPE,
    MANIFEST_LIST_TYPE,
    OCI_MANIFEST_TYPE,
    OCI_INDEX_TYPE,
    CONFIG_TYPE,
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/octet-stream",
]
"""All content types this handler accepts/serves.

Includes manifest types, layer types, and generic binary fallback.
"""

# ---------------------------------------------------------------------------
# Manifest-specific media type set for quick lookup
# ---------------------------------------------------------------------------

_MANIFEST_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        MANIFEST_V2_TYPE,
        MANIFEST_LIST_TYPE,
        OCI_MANIFEST_TYPE,
        OCI_INDEX_TYPE,
    }
)
"""Set of media types that indicate Docker/OCI manifests (not blobs/layers)."""

# ---------------------------------------------------------------------------
# Single-image manifest types (have config + layers)
# ---------------------------------------------------------------------------

_SINGLE_MANIFEST_TYPES: frozenset[str] = frozenset(
    {
        MANIFEST_V2_TYPE,
        OCI_MANIFEST_TYPE,
    }
)
"""Manifest types for single-architecture images (config + layers)."""

# ---------------------------------------------------------------------------
# Manifest list / index types (have manifests array)
# ---------------------------------------------------------------------------

_INDEX_MANIFEST_TYPES: frozenset[str] = frozenset(
    {
        MANIFEST_LIST_TYPE,
        OCI_INDEX_TYPE,
    }
)
"""Manifest types for multi-architecture manifest lists / indexes."""

# ---------------------------------------------------------------------------
# Regex Patterns
# ---------------------------------------------------------------------------

DIGEST_PATTERN: re.Pattern[str] = re.compile(r"^sha256:[a-fA-F0-9]{64}$")
"""Compiled regex for validating Docker sha256 digest references.

Docker content-addressable storage uses ``sha256:<64-hex-chars>`` digests
for all blob and manifest references.
"""

TAG_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,127}$"
)
"""Compiled regex for validating Docker tag names per the OCI distribution spec.

Tags must start with an alphanumeric character or underscore, followed by up
to 127 characters of alphanumerics, periods, hyphens, and underscores.
"""

# Docker image name path pattern
# Matches: library/nginx, myorg/myapp, registry.example.com/org/image
IMAGE_PATH_PATTERN: str = r"/v2/(?P<name>.+?)/(manifests|blobs|tags)"
"""Regex pattern matching Docker V2 API paths with an image name component.

Captures the image name (which may contain slashes for multi-segment names)
and the resource type (manifests, blobs, or tags).
"""

# Compiled path patterns for validate_path()
_V2_BASE_PATTERN: re.Pattern[str] = re.compile(r"^/v2/?$")
_V2_CATALOG_PATTERN: re.Pattern[str] = re.compile(r"^/v2/_catalog/?$")
_V2_MANIFESTS_PATTERN: re.Pattern[str] = re.compile(
    r"^/v2/(?P<name>.+)/manifests/(?P<reference>[^/]+)$"
)
_V2_BLOBS_PATTERN: re.Pattern[str] = re.compile(
    r"^/v2/(?P<name>.+)/blobs/(?P<digest>[^/]+)$"
)
_V2_UPLOADS_INITIATE_PATTERN: re.Pattern[str] = re.compile(
    r"^/v2/(?P<name>.+)/blobs/uploads/?$"
)
_V2_UPLOADS_SESSION_PATTERN: re.Pattern[str] = re.compile(
    r"^/v2/(?P<name>.+)/blobs/uploads/(?P<uuid>[^/]+)$"
)
_V2_TAGS_PATTERN: re.Pattern[str] = re.compile(
    r"^/v2/(?P<name>.+)/tags/list/?$"
)

# Default namespace for single-segment image names (e.g., 'nginx' → 'library/nginx')
DEFAULT_NAMESPACE: str = "library"
"""Default registry namespace for single-segment Docker image names.

Official Docker Hub images (e.g., ``nginx``, ``redis``, ``ubuntu``) are
implicitly under the ``library`` namespace.
"""


# ===========================================================================
# DockerFormatHandler Class
# ===========================================================================


class DockerFormatHandler(FormatHandler):
    """Docker container image format handler implementing F-101-RQ-005.

    Implements the :class:`FormatHandler` plugin contract for Docker container
    images, supporting the Docker Registry HTTP API V2 specification.  Handles
    Docker image manifest and layer (blob) upload/download with support for
    Docker V2 Schema 2 and OCI Image Manifest formats.

    **Replaces:** The Docker format bundle OSGi plugin from the Java source
    system (OSGi/Karaf 4.4.4 module container).

    **Supported Manifest Formats:**

    - Docker V2 Schema 2 (``application/vnd.docker.distribution.manifest.v2+json``)
    - Docker manifest lists (``application/vnd.docker.distribution.manifest.list.v2+json``)
    - OCI Image Manifest (``application/vnd.oci.image.manifest.v1+json``)
    - OCI Image Index (``application/vnd.oci.image.index.v1+json``)

    **Coordinate Mapping:**

    - ``namespace``: Registry namespace (e.g., ``'library'`` for official images)
    - ``name``: Image name (e.g., ``'nginx'``)
    - ``version``: Tag (e.g., ``'latest'``) or digest (e.g., ``'sha256:abc...'``)

    Attributes:
        format_name: ``'docker'`` — canonical format identifier.
        content_types: Docker-specific MIME types this handler recognises.
        path_pattern: Regex matching Docker V2 API image name paths.
    """

    # ------------------------------------------------------------------
    # Class-Level Attributes (per FormatHandler contract)
    # ------------------------------------------------------------------

    format_name: str = DOCKER_FORMAT_NAME
    """``'docker'`` — MUST match the ``Repository`` model format enum."""

    content_types: list[str] = DOCKER_CONTENT_TYPES
    """Docker-specific MIME types this handler recognises and can process."""

    path_pattern: str = IMAGE_PATH_PATTERN
    """Regex matching Docker V2 API paths with an image name component."""

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the Docker format handler.

        Calls the parent :meth:`FormatHandler.__init__` which validates
        that ``format_name`` is set and creates a format-specific logger.
        Then creates a Docker-specific child logger for fine-grained
        diagnostics.
        """
        super().__init__()
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.DockerFormatHandler"
        )

    # ==================================================================
    # Flask Blueprint Registration
    # ==================================================================

    @classmethod
    def get_blueprint(cls) -> Blueprint:
        """Return the Flask Blueprint for Docker Registry V2 API routes.

        The Docker format uses the V2 Registry API Blueprint which implements
        all Docker-specific HTTP endpoints (manifests, blobs, uploads, tags,
        catalog).  The Blueprint is imported via a deferred import to avoid
        circular dependencies between ``handler.py`` and ``registry_v2.py``.

        Returns:
            A fully configured Flask Blueprint containing all Docker V2
            Registry API route handlers ready for registration with the
            Flask application.
        """
        from src.app.formats.docker.registry_v2 import docker_v2_bp
        return docker_v2_bp

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
        """Handle a Docker artifact upload (manifest or blob/layer).

        Processes the incoming content, determines whether it is a manifest
        or a blob/layer, validates the content, extracts coordinates and
        metadata, computes checksums, and returns a result dictionary.

        For manifests:
            1. Validate manifest JSON structure (schemaVersion, config, layers)
            2. Extract metadata (mediaType, config digest, layer digests)
            3. Compute sha256 digest of raw manifest bytes
            4. Extract coordinates (namespace, name, tag/digest)

        For blobs/layers:
            1. Compute sha256 digest
            2. Verify digest matches expected (if provided in attributes)
            3. Return basic metadata

        Args:
            repository_name: Name of the target hosted repository.
            path: Storage path within the repository (Docker V2 API path).
            content: Raw binary content of the artifact being uploaded.
            content_type: MIME type of the uploaded content.
            attributes: Optional dictionary of additional metadata.  May
                contain ``'expected_digest'`` for blob digest verification.

        Returns:
            Dictionary containing metadata about the upload:

            - ``'path'`` — normalised storage path
            - ``'digest'`` — sha256 digest of the content
            - ``'size'`` — content size in bytes
            - ``'content_type'`` — effective content type
            - ``'coordinates'`` — dict with ``'namespace'``, ``'name'``,
              ``'version'``
            - ``'checksums'`` — dict with ``'md5'``, ``'sha1'``, ``'sha256'``
            - ``'is_manifest'`` — whether this is a manifest upload
            - ``'metadata'`` — Docker-specific metadata (for manifests)

        Raises:
            FormatValidationError: If manifest validation fails (invalid JSON,
                missing required fields, malformed digest).
        """
        self.logger.info(
            "Processing Docker upload: repo=%s path=%s size=%d content_type=%s",
            repository_name,
            path,
            len(content),
            content_type,
        )

        digest = self._compute_digest(content)
        checksums = self.compute_checksums(content)
        is_manifest = self._is_manifest_content_type(content_type)

        # Extract coordinates from the path
        coordinates = self.extract_coordinates(path, content)

        result: dict[str, Any] = {
            "path": path,
            "digest": digest,
            "size": len(content),
            "content_type": content_type or "application/octet-stream",
            "coordinates": coordinates,
            "checksums": checksums,
            "is_manifest": is_manifest,
        }

        if is_manifest:
            # Validate and extract manifest metadata
            try:
                self.validate_content(path, content)
            except FormatValidationError:
                raise
            except Exception as exc:
                raise FormatValidationError(
                    format_name=self.format_name,
                    message=f"Docker manifest validation failed: {exc}",
                    path=path,
                ) from exc

            manifest_metadata = self._extract_manifest_metadata(
                content, content_type
            )
            result["metadata"] = manifest_metadata

            self.logger.info(
                "Docker manifest upload processed: repo=%s digest=%s "
                "namespace=%s name=%s version=%s",
                repository_name,
                digest,
                coordinates.get("namespace"),
                coordinates.get("name"),
                coordinates.get("version"),
            )
        else:
            # Blob/layer upload — verify expected digest if provided
            expected_digest = (attributes or {}).get("expected_digest")
            if expected_digest and expected_digest != digest:
                raise FormatValidationError(
                    format_name=self.format_name,
                    message=(
                        f"Digest mismatch: expected {expected_digest}, "
                        f"computed {digest}"
                    ),
                    path=path,
                )

            result["metadata"] = {
                "digest": digest,
                "size": len(content),
                "content_type": content_type or "application/octet-stream",
            }

            self.logger.info(
                "Docker blob upload processed: repo=%s digest=%s size=%d",
                repository_name,
                digest,
                len(content),
            )

        if attributes:
            result["attributes"] = attributes

        return result

    def handle_download(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str, dict]:
        """Handle a Docker artifact download request (manifest or blob).

        Parses the Docker V2 API path to determine the artifact type
        (manifest or blob) and extracts the relevant reference (tag, digest,
        or blob digest).  The actual BlobStore retrieval is expected to be
        handled by the service layer; this method provides format-specific
        path parsing and response header construction.

        For manifests:
            - Parses image name and reference (tag or digest) from path
            - Returns Docker-specific response headers
              (``Docker-Content-Digest``, ``Docker-Distribution-Api-Version``)

        For blobs:
            - Parses digest from path
            - Returns appropriate response headers

        Args:
            repository_name: Name of the repository to download from.
            path: Docker V2 API path (e.g.,
                ``/v2/library/nginx/manifests/latest``).

        Returns:
            A three-element tuple of ``(content_bytes, content_type, headers)``.
            Returns empty content with appropriate headers as this method
            primarily performs path parsing — actual content retrieval is
            delegated to the service layer.

        Raises:
            FormatValidationError: If the path cannot be parsed as a valid
                Docker V2 API path.
        """
        self.logger.debug(
            "Docker download request: repo=%s path=%s",
            repository_name,
            path,
        )

        headers: dict[str, str] = {
            "Docker-Distribution-Api-Version": "registry/2.0",
        }

        # Check for manifest request
        manifest_match = _V2_MANIFESTS_PATTERN.match(path)
        if manifest_match:
            image_name = manifest_match.group("name")
            reference = manifest_match.group("reference")
            namespace, name = self._parse_image_name(image_name)

            self.logger.debug(
                "Docker manifest download: namespace=%s name=%s ref=%s",
                namespace,
                name,
                reference,
            )

            # Default content type for manifests — the service layer should
            # negotiate the correct type based on Accept headers and stored
            # manifest type.
            default_ct = MANIFEST_V2_TYPE

            # The service layer handles actual content retrieval from BlobStore.
            # We return empty bytes as a placeholder; the route handlers in
            # registry_v2.py perform the actual retrieval.
            return (b"", default_ct, headers)

        # Check for blob request
        blob_match = _V2_BLOBS_PATTERN.match(path)
        if blob_match:
            blob_digest = blob_match.group("digest")
            self.logger.debug(
                "Docker blob download: repo=%s digest=%s",
                repository_name,
                blob_digest,
            )

            headers["Docker-Content-Digest"] = blob_digest
            return (b"", "application/octet-stream", headers)

        # Unrecognised path — raise a validation error
        self.logger.warning(
            "Unrecognised Docker download path: repo=%s path=%s",
            repository_name,
            path,
        )
        raise FormatValidationError(
            format_name=self.format_name,
            message=f"Unrecognised Docker V2 API path: {path}",
            path=path,
        )

    def extract_coordinates(
        self,
        path: str,
        content: bytes | None = None,
    ) -> dict:
        """Extract Docker image coordinates from a Docker V2 API path.

        Parses the Docker Registry V2 API path to determine the image's
        namespace, name, and version (tag or digest).

        Docker coordinate mapping:

        - ``namespace``: Registry namespace (e.g., ``'library'`` for
          official images, ``'myorg'`` for organisational images)
        - ``name``: Image name (e.g., ``'nginx'``, ``'redis'``)
        - ``version``: Tag (e.g., ``'latest'``, ``'1.25.3'``) or digest
          (e.g., ``'sha256:abc...'``)

        Parsing rules for image names:

        - Single segment: ``nginx`` → namespace=``'library'``,
          name=``'nginx'``
        - Two segments: ``myorg/myapp`` → namespace=``'myorg'``,
          name=``'myapp'``
        - Multi-segment: ``registry.com/org/app`` →
          namespace=``'registry.com/org'``, name=``'app'``

        Args:
            path: Docker V2 API path (e.g.,
                ``/v2/library/nginx/manifests/latest``).
            content: Optional raw content (not used for Docker coordinate
                extraction — coordinates come from the URL path).

        Returns:
            Dictionary with keys ``'namespace'``, ``'name'``, ``'version'``.
            Values are ``None`` if extraction fails.
        """
        empty_result: dict[str, str | None] = {
            "namespace": None,
            "name": None,
            "version": None,
        }

        # Try manifest path pattern
        manifest_match = _V2_MANIFESTS_PATTERN.match(path)
        if manifest_match:
            image_name = manifest_match.group("name")
            reference = manifest_match.group("reference")
            namespace, name = self._parse_image_name(image_name)

            self.logger.debug(
                "Extracted Docker coordinates from manifest path: "
                "namespace=%s name=%s version=%s",
                namespace,
                name,
                reference,
            )
            return {
                "namespace": namespace,
                "name": name,
                "version": reference,
            }

        # Try blob path pattern — blobs don't carry tag/version info
        blob_match = _V2_BLOBS_PATTERN.match(path)
        if blob_match:
            image_name = blob_match.group("name")
            blob_digest = blob_match.group("digest")
            namespace, name = self._parse_image_name(image_name)

            self.logger.debug(
                "Extracted Docker coordinates from blob path: "
                "namespace=%s name=%s digest=%s",
                namespace,
                name,
                blob_digest,
            )
            return {
                "namespace": namespace,
                "name": name,
                "version": blob_digest,
            }

        # Try upload session path
        upload_match = _V2_UPLOADS_SESSION_PATTERN.match(path)
        if upload_match:
            image_name = upload_match.group("name")
            namespace, name = self._parse_image_name(image_name)
            return {
                "namespace": namespace,
                "name": name,
                "version": None,
            }

        # Try upload initiate path
        upload_init_match = _V2_UPLOADS_INITIATE_PATTERN.match(path)
        if upload_init_match:
            image_name = upload_init_match.group("name")
            namespace, name = self._parse_image_name(image_name)
            return {
                "namespace": namespace,
                "name": name,
                "version": None,
            }

        # Try tags list path
        tags_match = _V2_TAGS_PATTERN.match(path)
        if tags_match:
            image_name = tags_match.group("name")
            namespace, name = self._parse_image_name(image_name)
            return {
                "namespace": namespace,
                "name": name,
                "version": None,
            }

        self.logger.debug(
            "Unable to extract Docker coordinates from path: %s", path
        )
        return empty_result

    def generate_metadata(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str] | None:
        """Generate Docker-specific metadata for a given path.

        For Docker repositories, metadata generation covers:

        - **Tag list** (``/v2/{name}/tags/list``): Returns a JSON document
          listing all tags for a Docker image.
        - **Catalog** (``/v2/_catalog``): Returns a JSON document listing
          all repository names.

        Other paths return ``None`` to indicate no metadata generation is
        applicable.

        Note:
            The actual tag list and catalog data come from the database via
            the service layer. This method provides the format-specific
            structure and content type. The route handlers in
            ``registry_v2.py`` perform the actual data retrieval.

        Args:
            repository_name: Name of the Docker repository.
            path: Requested path within the Docker V2 API.

        Returns:
            A two-element tuple of ``(metadata_bytes, content_type)`` if the
            path is a metadata endpoint, or ``None`` if the path does not
            correspond to a Docker metadata resource.
        """
        # Handle tag list requests
        tags_match = _V2_TAGS_PATTERN.match(path)
        if tags_match:
            image_name = tags_match.group("name")
            self.logger.debug(
                "Generating tag list metadata for repo=%s image=%s",
                repository_name,
                image_name,
            )

            # Generate a tag list response structure.
            # The service layer should populate the actual tags; we provide
            # the structural template here.
            tag_list: dict[str, Any] = {
                "name": image_name,
                "tags": [],
            }
            tag_list_bytes = json.dumps(
                tag_list, indent=None, separators=(",", ":")
            ).encode("utf-8")
            return (tag_list_bytes, "application/json")

        # Handle catalog requests
        if _V2_CATALOG_PATTERN.match(path):
            self.logger.debug(
                "Generating catalog metadata for repo=%s",
                repository_name,
            )
            catalog: dict[str, list[str]] = {
                "repositories": [],
            }
            catalog_bytes = json.dumps(
                catalog, indent=None, separators=(",", ":")
            ).encode("utf-8")
            return (catalog_bytes, "application/json")

        # No metadata generation for other paths
        return None

    def validate_path(self, path: str) -> bool:
        """Validate that a path is a valid Docker Registry V2 API path.

        Checks the path against all known Docker V2 API endpoint patterns:

        - ``/v2/`` — version check / auth challenge
        - ``/v2/_catalog`` — repository catalog
        - ``/v2/{name}/manifests/{reference}`` — manifest operations
        - ``/v2/{name}/blobs/{digest}`` — blob operations
        - ``/v2/{name}/blobs/uploads/`` — initiate blob upload
        - ``/v2/{name}/blobs/uploads/{uuid}`` — blob upload session
        - ``/v2/{name}/tags/list`` — tag listing

        Args:
            path: Path string to validate.

        Returns:
            ``True`` if the path matches any valid Docker V2 API pattern,
            ``False`` otherwise.
        """
        if not path:
            return False

        # Check against all known Docker V2 API patterns
        if _V2_BASE_PATTERN.match(path):
            return True

        if _V2_CATALOG_PATTERN.match(path):
            return True

        if _V2_MANIFESTS_PATTERN.match(path):
            return True

        if _V2_BLOBS_PATTERN.match(path):
            return True

        if _V2_UPLOADS_INITIATE_PATTERN.match(path):
            return True

        if _V2_UPLOADS_SESSION_PATTERN.match(path):
            return True

        if _V2_TAGS_PATTERN.match(path):
            return True

        return False

    def validate_content(self, path: str, content: bytes) -> bool:
        """Validate Docker content based on content type and structure.

        For manifests (determined by path pattern containing ``/manifests/``):

        1. Parse JSON — content must be valid JSON
        2. Check ``schemaVersion`` field exists and equals 2
        3. For single manifests (V2 / OCI):
           a. Check ``config`` section exists with ``mediaType``, ``digest``,
              ``size``
           b. Check ``layers`` list exists and is non-empty
           c. Verify each layer has ``mediaType``, ``digest``, ``size``
           d. Verify all digests match the sha256 format
        4. For manifest lists / indexes:
           a. Check ``manifests`` array exists
           b. Each entry has ``mediaType``, ``digest``, ``size``

        For blobs/layers: basic validation (non-empty content).

        Args:
            path: Artifact path (provides context for content validation).
            content: Raw binary content to validate.

        Returns:
            ``True`` if the content passes validation.

        Raises:
            FormatValidationError: If the content fails validation, with a
                descriptive error message.
        """
        if not content:
            raise FormatValidationError(
                format_name=self.format_name,
                message="Empty content is not allowed",
                path=path,
            )

        # Determine if this is a manifest based on the path
        is_manifest_path = "/manifests/" in path

        if not is_manifest_path:
            # Blob/layer content — just check it's non-empty (already checked)
            return True

        # Manifest validation
        try:
            manifest = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FormatValidationError(
                format_name=self.format_name,
                message=f"Invalid manifest JSON: {exc}",
                path=path,
            ) from exc

        if not isinstance(manifest, dict):
            raise FormatValidationError(
                format_name=self.format_name,
                message="Manifest must be a JSON object",
                path=path,
            )

        # Validate schemaVersion
        schema_version = manifest.get("schemaVersion")
        if schema_version != 2:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Unsupported or missing schemaVersion: {schema_version}. "
                    f"Expected schemaVersion 2."
                ),
                path=path,
            )

        # Determine manifest type from mediaType field
        media_type = manifest.get("mediaType", "")

        # Manifest list / OCI index validation
        if media_type in _INDEX_MANIFEST_TYPES or "manifests" in manifest:
            return self._validate_manifest_list(manifest, path)

        # Single manifest (V2 Schema 2 / OCI) validation
        return self._validate_single_manifest(manifest, path)

    # ==================================================================
    # Helper Methods
    # ==================================================================

    def _parse_image_name(self, name: str) -> tuple[str, str]:
        """Parse a Docker image name into (namespace, image_name).

        Docker image names can be structured as:

        - Single segment: ``nginx`` → ``('library', 'nginx')``
        - Two segments: ``myorg/myapp`` → ``('myorg', 'myapp')``
        - Multi-segment: ``registry.com/org/app`` →
          ``('registry.com/org', 'app')``

        Args:
            name: The raw Docker image name path segment from the V2 API URL.

        Returns:
            Tuple of ``(namespace, image_name)``.
        """
        if "/" not in name:
            return DEFAULT_NAMESPACE, name

        last_slash = name.rfind("/")
        namespace = name[:last_slash]
        image_name = name[last_slash + 1:]
        return namespace, image_name

    def _compute_digest(self, data: bytes) -> str:
        """Compute a SHA-256 digest of binary data in Docker format.

        Docker content-addressable storage uses ``sha256:<hex_digest>`` format
        for all blob and manifest references.

        Args:
            data: Binary content to hash.

        Returns:
            Digest string in ``sha256:<hex_digest>`` format (64 hex chars).
        """
        return f"sha256:{hashlib.sha256(data).hexdigest()}"

    def _is_manifest_content_type(self, content_type: str | None) -> bool:
        """Check if a content type indicates a Docker manifest.

        Returns ``True`` for all known Docker/OCI manifest media types
        (V2 Schema 2, manifest list, OCI manifest, OCI index).

        Args:
            content_type: MIME type string to check, or ``None``.

        Returns:
            ``True`` if the content type is a manifest type, ``False``
            otherwise (including when ``content_type`` is ``None``).
        """
        if content_type is None:
            return False
        return content_type in _MANIFEST_MEDIA_TYPES

    def _extract_layer_digests(self, manifest: dict) -> list[str]:
        """Extract all layer and config blob digests from a parsed manifest.

        Collects the config digest and all layer digests from a Docker V2
        Schema 2 or OCI Image Manifest.  For manifest lists / OCI indexes,
        collects digests of all referenced platform manifests.

        Args:
            manifest: A parsed Docker/OCI manifest dictionary.

        Returns:
            List of digest strings in ``sha256:<hex>`` format.  Includes
            the config digest (if present) followed by all layer digests.
        """
        digests: list[str] = []

        # Extract config digest (single manifests)
        config = manifest.get("config")
        if isinstance(config, dict):
            config_digest = config.get("digest")
            if config_digest and isinstance(config_digest, str):
                digests.append(config_digest)

        # Extract layer digests (single manifests)
        layers = manifest.get("layers")
        if isinstance(layers, list):
            for layer in layers:
                if isinstance(layer, dict):
                    layer_digest = layer.get("digest")
                    if layer_digest and isinstance(layer_digest, str):
                        digests.append(layer_digest)

        # Extract manifest digests (manifest lists / indexes)
        manifests_list = manifest.get("manifests")
        if isinstance(manifests_list, list):
            for entry in manifests_list:
                if isinstance(entry, dict):
                    entry_digest = entry.get("digest")
                    if entry_digest and isinstance(entry_digest, str):
                        digests.append(entry_digest)

        return digests

    def _is_digest(self, reference: str) -> bool:
        """Check if a Docker reference is a digest (vs a tag).

        Digests have the format ``sha256:<64-hex-chars>``.  All other
        reference strings are treated as tags.

        Args:
            reference: A tag or digest string.

        Returns:
            ``True`` if *reference* matches the sha256 digest pattern,
            ``False`` otherwise.
        """
        return bool(DIGEST_PATTERN.match(reference))

    # ==================================================================
    # Private Validation Helpers
    # ==================================================================

    def _validate_single_manifest(
        self, manifest: dict, path: str
    ) -> bool:
        """Validate a single-architecture Docker/OCI manifest.

        Checks for the required ``config`` and ``layers`` sections, and
        verifies that all digests match the sha256 format.

        Args:
            manifest: Parsed manifest dictionary.
            path: Original request path for error reporting.

        Returns:
            ``True`` if the manifest is valid.

        Raises:
            FormatValidationError: If required fields are missing or malformed.
        """
        # Validate config section
        config = manifest.get("config")
        if not isinstance(config, dict):
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    "Manifest missing required 'config' object. "
                    "Docker V2 Schema 2 and OCI manifests must include "
                    "a config descriptor with mediaType, digest, and size."
                ),
                path=path,
            )

        for required_field in ("mediaType", "digest", "size"):
            if required_field not in config:
                raise FormatValidationError(
                    format_name=self.format_name,
                    message=(
                        f"Manifest config missing required field: "
                        f"'{required_field}'"
                    ),
                    path=path,
                )

        # Validate config digest format
        config_digest = config.get("digest", "")
        if isinstance(config_digest, str) and not self._is_digest(config_digest):
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    f"Invalid config digest format: '{config_digest}'. "
                    f"Expected sha256:<64-hex-chars>."
                ),
                path=path,
            )

        # Validate layers section
        layers = manifest.get("layers")
        if not isinstance(layers, list) or len(layers) == 0:
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    "Manifest missing required 'layers' array or layers "
                    "array is empty. Docker V2 Schema 2 and OCI manifests "
                    "must include at least one layer descriptor."
                ),
                path=path,
            )

        # Validate each layer descriptor
        for idx, layer in enumerate(layers):
            if not isinstance(layer, dict):
                raise FormatValidationError(
                    format_name=self.format_name,
                    message=f"Layer at index {idx} must be a JSON object",
                    path=path,
                )

            for required_field in ("mediaType", "digest", "size"):
                if required_field not in layer:
                    raise FormatValidationError(
                        format_name=self.format_name,
                        message=(
                            f"Layer at index {idx} missing required field: "
                            f"'{required_field}'"
                        ),
                        path=path,
                    )

            layer_digest = layer.get("digest", "")
            if isinstance(layer_digest, str) and not self._is_digest(layer_digest):
                raise FormatValidationError(
                    format_name=self.format_name,
                    message=(
                        f"Invalid layer digest format at index {idx}: "
                        f"'{layer_digest}'. Expected sha256:<64-hex-chars>."
                    ),
                    path=path,
                )

        return True

    def _validate_manifest_list(
        self, manifest: dict, path: str
    ) -> bool:
        """Validate a Docker manifest list or OCI image index.

        Checks for the required ``manifests`` array and verifies that each
        entry contains the required descriptor fields (mediaType, digest,
        size).

        Args:
            manifest: Parsed manifest list/index dictionary.
            path: Original request path for error reporting.

        Returns:
            ``True`` if the manifest list is valid.

        Raises:
            FormatValidationError: If required fields are missing or malformed.
        """
        manifests_list = manifest.get("manifests")
        if not isinstance(manifests_list, list):
            raise FormatValidationError(
                format_name=self.format_name,
                message=(
                    "Manifest list/index missing required 'manifests' array"
                ),
                path=path,
            )

        for idx, entry in enumerate(manifests_list):
            if not isinstance(entry, dict):
                raise FormatValidationError(
                    format_name=self.format_name,
                    message=(
                        f"Manifest entry at index {idx} must be a JSON object"
                    ),
                    path=path,
                )

            for required_field in ("mediaType", "digest", "size"):
                if required_field not in entry:
                    raise FormatValidationError(
                        format_name=self.format_name,
                        message=(
                            f"Manifest entry at index {idx} missing required "
                            f"field: '{required_field}'"
                        ),
                        path=path,
                    )

            entry_digest = entry.get("digest", "")
            if isinstance(entry_digest, str) and not self._is_digest(entry_digest):
                raise FormatValidationError(
                    format_name=self.format_name,
                    message=(
                        f"Invalid manifest entry digest at index {idx}: "
                        f"'{entry_digest}'. Expected sha256:<64-hex-chars>."
                    ),
                    path=path,
                )

        return True

    def _extract_manifest_metadata(
        self,
        content: bytes,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Extract Docker-specific metadata from manifest content.

        Parses the manifest JSON and extracts key fields for storage as
        component/asset attributes.

        For Docker V2 Schema 2 / OCI manifests:
            Returns mediaType, schemaVersion, config descriptor, layers list,
            total_layers count, and total_size sum.

        For manifest lists / OCI indexes:
            Returns mediaType, schemaVersion, manifests array, and
            total_manifests count.

        For non-JSON content (blobs/layers):
            Returns basic digest, size, and content_type.

        Args:
            content: Raw manifest content bytes.
            content_type: MIME type of the content, or ``None``.

        Returns:
            Dictionary of Docker-specific metadata fields.
        """
        try:
            manifest = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Not a valid JSON manifest — return basic metadata
            return {
                "digest": self._compute_digest(content),
                "size": len(content),
                "content_type": content_type or "application/octet-stream",
            }

        if not isinstance(manifest, dict):
            return {
                "digest": self._compute_digest(content),
                "size": len(content),
                "content_type": content_type or "application/octet-stream",
            }

        media_type = manifest.get("mediaType", content_type or "")
        schema_version = manifest.get("schemaVersion")

        # Manifest list / OCI index
        if media_type in _INDEX_MANIFEST_TYPES or "manifests" in manifest:
            manifests_list = manifest.get("manifests", [])
            return {
                "mediaType": media_type,
                "schemaVersion": schema_version,
                "manifests": manifests_list,
                "total_manifests": len(manifests_list),
                "digest": self._compute_digest(content),
            }

        # Single manifest (V2 Schema 2 / OCI)
        config = manifest.get("config", {})
        layers = manifest.get("layers", [])
        total_size = sum(
            layer.get("size", 0)
            for layer in layers
            if isinstance(layer, dict)
        )

        return {
            "mediaType": media_type,
            "schemaVersion": schema_version,
            "config": config,
            "layers": layers,
            "total_layers": len(layers),
            "total_size": total_size,
            "digest": self._compute_digest(content),
        }
