"""
Abstract Base Class for Format Handlers — Plugin Contract (F-504).

This module defines the :class:`FormatHandler` abstract base class that **all**
format-specific handlers (Maven, npm, Docker, NuGet, PyPI, APT, Raw) must
extend.  It establishes the plugin contract for the format handler subsystem,
replacing the OSGi format plugin bundle interfaces from the original Java
source (Sonatype Nexus Repository).

**Architecture Context:**

In the original Java system, each repository format was an OSGi bundle
registered via the Karaf 4.4.4 module container and discovered through the
Guice 7.0.0 dependency injection framework.  In this Python/Flask
reimplementation, the ``FormatHandler`` ABC serves as the equivalent plugin
contract.  Each format handler subclass:

- Provides a Flask :class:`~flask.Blueprint` with format-specific routes
  (replacing JAX-RS resource classes from RESTEasy 6.2.7).
- Implements upload/download logic for that format's artifact structure.
- Extracts component coordinates (namespace, name, version) from format-
  specific paths and content.
- Generates format-native metadata (e.g., ``maven-metadata.xml``,
  ``Packages.gz``).
- Validates paths and content according to format conventions.

**Design Patterns:**

- **Strategy Pattern** — Format handlers are strategies selected by the
  ``format_name`` attribute.  The :mod:`src.app.formats` registry maps format
  name strings to handler classes.
- **Template Method** — Concrete helper methods (``compute_checksums``,
  ``detect_content_type``, ``normalize_path``) provide shared behaviour that
  subclasses inherit or override.
- **Abstract Factory** — The ``get_blueprint()`` class method returns a
  pre-configured Flask Blueprint for each format.

**Supported Formats:**

======== ====================================================
Format   Description
======== ====================================================
maven2   Apache Maven 2 / 3 repository layout (GAV coordinates)
npm      npm registry protocol (scoped + unscoped packages)
docker   Docker Registry API v2 (manifests + layers)
nuget    NuGet V3 API (.nupkg packages)
pypi     PEP 503 Simple Repository API (wheels + sdists)
apt      Debian APT repository (.deb packages)
raw      Raw / arbitrary binary file storage
======== ====================================================

**Exception Hierarchy:**

- :class:`FormatValidationError` — format-specific validation failures
- :class:`UnsupportedFormatError` — unknown or unsupported format requested
- :class:`ArtifactNotFoundError` — requested artifact does not exist

**Usage Example:**

.. code-block:: python

    from src.app.formats.base import FormatHandler

    class MavenFormatHandler(FormatHandler):
        format_name = "maven2"
        content_types = ["application/java-archive", "application/xml"]
        path_pattern = r"^[a-zA-Z0-9_./-]+$"

        @classmethod
        def get_blueprint(cls) -> Blueprint:
            bp = Blueprint("maven", __name__, url_prefix="/repository")
            # ... register routes ...
            return bp

        # ... implement remaining abstract methods ...

Module-Level Exports:
    - :class:`FormatHandler`
    - :class:`FormatValidationError`
    - :class:`UnsupportedFormatError`
    - :class:`ArtifactNotFoundError`
"""

from __future__ import annotations

import abc
import hashlib
import logging
import mimetypes
import re
from typing import TYPE_CHECKING

from flask import Blueprint, Request, Response  # noqa: F401

if TYPE_CHECKING:
    from src.app.models.asset import Asset  # noqa: F401
    from src.app.models.component import Component  # noqa: F401
    from src.app.models.repository import Repository  # noqa: F401

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source.  Each FormatHandler instance also creates a format-specific child
# logger for fine-grained diagnostics.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "FormatHandler",
    "FormatValidationError",
    "UnsupportedFormatError",
    "ArtifactNotFoundError",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_FORMAT_NAMES: frozenset[str] = frozenset(
    {
        "maven2",
        "npm",
        "docker",
        "nuget",
        "pypi",
        "apt",
        "raw",
    }
)
"""Canonical set of supported format names matching the ``Repository.format``
column enum (F-101).  Used by :class:`UnsupportedFormatError` to generate
helpful error messages."""

_DEFAULT_CONTENT_TYPE: str = "application/octet-stream"
"""Fallback MIME type returned by :meth:`FormatHandler.detect_content_type`
when no format-specific or extension-based match is found."""

# Precompiled regex for duplicate-slash normalisation in paths.
_MULTI_SLASH_RE: re.Pattern[str] = re.compile(r"/{2,}")


# ===========================================================================
# Exception Classes
# ===========================================================================


class FormatValidationError(Exception):
    """Raised when format-specific validation of an artifact fails.

    Carries contextual information about *which* format, *what* message, and
    *which* path (if applicable) triggered the failure.  This exception is
    caught by API route handlers and translated into an appropriate HTTP 400
    (Bad Request) response.

    Attributes:
        format_name: The repository format that raised the error
                     (e.g., ``'maven2'``, ``'npm'``).
        message:     A human-readable description of the validation failure.
        path:        The artifact path that failed validation, or ``None``
                     if the error is not path-specific.
    """

    def __init__(
        self,
        format_name: str,
        message: str,
        path: str | None = None,
    ) -> None:
        self.format_name: str = format_name
        self.message: str = message
        self.path: str | None = path

        # Build a descriptive super() message including all available context.
        parts: list[str] = [f"[{format_name}]"]
        if path is not None:
            parts.append(f"path='{path}'")
        parts.append(message)
        super().__init__(" ".join(parts))

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"format_name={self.format_name!r}, "
            f"message={self.message!r}, "
            f"path={self.path!r})"
        )


class UnsupportedFormatError(Exception):
    """Raised when an unsupported repository format is requested.

    The error message automatically includes the list of supported formats
    to guide the caller toward a valid choice.

    Attributes:
        format_name: The unsupported format name that was requested.
    """

    def __init__(self, format_name: str) -> None:
        self.format_name: str = format_name
        supported: str = ", ".join(sorted(SUPPORTED_FORMAT_NAMES))
        super().__init__(
            f"Unsupported repository format: '{format_name}'. "
            f"Supported formats are: {supported}"
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(format_name={self.format_name!r})"


class ArtifactNotFoundError(Exception):
    """Raised when a requested artifact cannot be found in the repository.

    Carries the repository name and artifact path for diagnostic purposes.
    This exception is caught by API route handlers and translated into an
    HTTP 404 (Not Found) response.

    Attributes:
        repository_name: The repository that was searched.
        path:            The artifact path that was not found.
    """

    def __init__(self, repository_name: str, path: str) -> None:
        self.repository_name: str = repository_name
        self.path: str = path
        super().__init__(
            f"Artifact not found in repository '{repository_name}': {path}"
        )

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"repository_name={self.repository_name!r}, "
            f"path={self.path!r})"
        )


# ===========================================================================
# FormatHandler Abstract Base Class
# ===========================================================================


class FormatHandler(abc.ABC):
    """Abstract base class defining the plugin contract for repository format
    handlers.

    Every supported repository format (Maven, npm, Docker, NuGet, PyPI, APT,
    Raw) **must** subclass ``FormatHandler`` and implement all abstract
    methods.  The base class provides:

    1. **Abstract interface** — Methods that each format *must* implement to
       participate in the repository system:
       - :meth:`get_blueprint` — Flask Blueprint with format-specific routes
       - :meth:`handle_upload` — Artifact upload processing
       - :meth:`handle_download` — Artifact download processing
       - :meth:`extract_coordinates` — Component coordinate extraction
       - :meth:`generate_metadata` — Format-native metadata generation
       - :meth:`validate_path` — Path validation
       - :meth:`validate_content` — Content validation

    2. **Concrete helpers** — Shared utility methods with sensible defaults
       that subclasses may override:
       - :meth:`detect_content_type` — MIME type detection
       - :meth:`compute_checksums` — MD5 / SHA-1 / SHA-256 digests
       - :meth:`normalize_path` — Path normalization
       - :meth:`get_remote_url` — Proxy URL construction
       - :meth:`is_prerelease` — Pre-release version detection
       - :meth:`merge_metadata` — Group repository metadata merging

    **Class-Level Attributes (must be set by subclasses):**

    - ``format_name`` — Canonical format identifier matching the
      ``Repository.format`` column (e.g., ``'maven2'``, ``'npm'``).
    - ``content_types`` — List of MIME types this format handles.
    - ``path_pattern`` — Regex pattern for valid artifact paths.

    **Replaces (Java Source):**

    - OSGi format plugin bundle interfaces
    - Format-specific ``Recipe`` and ``Facet`` classes
    - JAX-RS resource class contracts
    """

    # ------------------------------------------------------------------
    # Class-Level Attributes (subclasses MUST override these)
    # ------------------------------------------------------------------

    format_name: str = ""
    """Canonical format name (e.g., ``'maven2'``, ``'npm'``, ``'docker'``).

    Must match one of the valid values in the ``Repository.format`` column:
    ``'maven2'``, ``'npm'``, ``'docker'``, ``'nuget'``, ``'pypi'``,
    ``'apt'``, ``'raw'``.
    """

    content_types: list[str] = []
    """MIME types that this format handler recognises and can process.

    Examples:
        - Maven: ``['application/java-archive', 'application/xml',
          'text/xml', 'application/pom+xml']``
        - npm: ``['application/gzip', 'application/json']``
        - Docker: ``['application/vnd.docker.distribution.manifest.v2+json',
          'application/vnd.oci.image.manifest.v1+json']``
    """

    path_pattern: str = r"^.+$"
    """Regex pattern that valid artifact paths must match.

    Subclasses should override with a format-specific pattern.  For example,
    Maven uses a pattern that enforces the ``groupId/artifactId/version``
    directory structure.
    """

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        """Initialise the format handler.

        Sets up a format-specific child logger derived from the module logger.
        Validates that ``format_name`` has been set by the concrete subclass;
        raises :class:`NotImplementedError` if it is empty or missing.

        Raises:
            NotImplementedError: If the subclass has not set ``format_name``
                to a non-empty string.
        """
        if not self.format_name:
            raise NotImplementedError(
                f"{self.__class__.__name__} must define a non-empty "
                f"'format_name' class attribute."
            )

        # Per-instance logger with format-specific context, e.g.
        # "src.app.formats.base.maven2"
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.{self.format_name}"
        )
        self.logger.debug(
            "Initialised %s format handler", self.format_name
        )

    # ==================================================================
    # Abstract Methods — Plugin Contract (subclasses MUST implement)
    # ==================================================================

    @classmethod
    @abc.abstractmethod
    def get_blueprint(cls) -> Blueprint:
        """Return a Flask :class:`~flask.Blueprint` with format-specific routes.

        Each format handler registers its own URL patterns and route handlers
        on the returned Blueprint.  The Blueprint is registered with the Flask
        application during startup by the format registry
        (:func:`src.app.formats.register_format_blueprints`).

        The blueprint URL prefix should incorporate the format name (e.g.,
        ``/repository/maven2``, ``/repository/npm``).

        Returns:
            A fully configured Flask Blueprint ready for registration.

        Example (in a subclass)::

            @classmethod
            def get_blueprint(cls) -> Blueprint:
                bp = Blueprint("maven", __name__, url_prefix="/repository")

                @bp.route("/<repo_name>/<path:artifact_path>", methods=["GET"])
                def download(repo_name: str, artifact_path: str):
                    handler = cls()
                    content, ct, headers = handler.handle_download(
                        repo_name, artifact_path
                    )
                    return Response(content, content_type=ct, headers=headers)

                return bp
        """

    @abc.abstractmethod
    def handle_upload(
        self,
        repository_name: str,
        path: str,
        content: bytes,
        content_type: str,
        attributes: dict | None = None,
    ) -> dict:
        """Handle an artifact upload for this format.

        Processes the incoming content, validates format-specific constraints
        (e.g., POM XML well-formedness for Maven, ``package.json`` schema for
        npm), extracts component coordinates, and returns metadata about the
        created component and asset.

        Args:
            repository_name: Name of the target hosted repository.
            path: Storage path within the repository (format-specific layout).
            content: Raw binary content of the artifact being uploaded.
            content_type: MIME type of the uploaded content.
            attributes: Optional dictionary of additional metadata to associate
                with the uploaded artifact.

        Returns:
            A dictionary containing metadata about the created
            component and asset.  Expected keys include:

            - ``'component'`` — dict with ``'namespace'``, ``'name'``,
              ``'version'`` (matching :class:`Component` model fields).
            - ``'asset'`` — dict with ``'path'``, ``'content_type'``,
              ``'size'``, ``'checksums'`` (matching :class:`Asset` model
              fields).

        Raises:
            ValueError: If the content fails format-specific validation.
            FormatValidationError: If the path or content violates format
                conventions.
        """

    @abc.abstractmethod
    def handle_download(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str, dict]:
        """Handle an artifact download request for this format.

        Retrieves the artifact content from the repository's BlobStore and
        returns it along with content type and any format-specific response
        headers (e.g., checksums, cache-control directives).

        Args:
            repository_name: Name of the repository to download from.
            path: Artifact path within the repository.

        Returns:
            A three-element tuple of:

            - ``content`` (bytes) — Raw binary content of the artifact.
            - ``content_type`` (str) — MIME type of the content.
            - ``headers`` (dict) — Additional HTTP response headers.

        Raises:
            ArtifactNotFoundError: If the artifact does not exist at the
                given path.
            FileNotFoundError: If the underlying blob cannot be found.
        """

    @abc.abstractmethod
    def extract_coordinates(
        self,
        path: str,
        content: bytes | None = None,
    ) -> dict:
        """Extract component coordinates from an artifact path and/or content.

        Parses format-specific structure to determine the component's logical
        identity within the repository.  The returned dictionary keys align
        directly with the :class:`Component` model fields.

        Format-Specific Coordinate Mapping:
            - **Maven**: ``namespace`` = groupId (e.g., ``'org.apache.commons'``),
              ``name`` = artifactId (e.g., ``'commons-lang3'``),
              ``version`` = version (e.g., ``'3.14.0'``).
            - **npm**: ``namespace`` = scope (e.g., ``'@angular'``, or ``None``),
              ``name`` = package name (e.g., ``'core'``),
              ``version`` = version (e.g., ``'18.2.0'``).
            - **Docker**: ``namespace`` = registry namespace (e.g., ``'library'``),
              ``name`` = image name (e.g., ``'nginx'``),
              ``version`` = tag or digest.
            - **NuGet**: ``namespace`` = ``None``,
              ``name`` = package ID,
              ``version`` = NuGet version.
            - **PyPI**: ``namespace`` = ``None``,
              ``name`` = project name,
              ``version`` = PEP 440 version.
            - **APT**: ``namespace`` = section (e.g., ``'main'``),
              ``name`` = package name,
              ``version`` = Debian version string.
            - **Raw**: ``namespace`` = directory path prefix (or ``None``),
              ``name`` = filename,
              ``version`` = ``None``.

        Args:
            path: Artifact path within the repository.
            content: Optional raw content of the artifact (used by some formats
                to extract coordinates from file content, e.g., POM XML).

        Returns:
            A dictionary with the following keys:

            - ``'namespace'`` (str | None): Format-specific grouping.
            - ``'name'`` (str): Component name.
            - ``'version'`` (str | None): Version string.

        Raises:
            FormatValidationError: If the path does not conform to the
                expected format structure.
        """

    @abc.abstractmethod
    def generate_metadata(
        self,
        repository_name: str,
        path: str,
    ) -> tuple[bytes, str] | None:
        """Generate format-specific metadata for a given path.

        Some formats expose metadata endpoints that return dynamically
        generated content (e.g., Maven's ``maven-metadata.xml``, npm's
        package metadata JSON, APT's ``Packages`` / ``Release`` index files).

        Args:
            repository_name: Name of the repository.
            path: Requested metadata path.

        Returns:
            A two-element tuple of ``(metadata_bytes, content_type)`` if the
            path is a metadata endpoint, or ``None`` if the path does not
            correspond to a metadata resource.

        Format-Specific Metadata:
            - **Maven**: Generates ``maven-metadata.xml`` at group and
              artifact level.
            - **npm**: Generates package metadata JSON
              (``/{package-name}``).
            - **Docker**: Generates tag list JSON
              (``/v2/{name}/tags/list``).
            - **NuGet**: Generates service index and registration pages.
            - **PyPI**: Generates PEP 503 simple index HTML.
            - **APT**: Generates ``Packages``, ``Release``, ``InRelease``
              files.
            - **Raw**: Returns ``None`` (no metadata endpoints).
        """

    @abc.abstractmethod
    def validate_path(self, path: str) -> bool:
        """Validate that an artifact path conforms to this format's conventions.

        Checks the path structure against format-specific rules without
        accessing the actual content.  For example, Maven validates the
        ``groupId/artifactId/version/filename`` directory structure, while
        npm validates the scoped package path pattern.

        Args:
            path: Artifact path to validate.

        Returns:
            ``True`` if the path is valid for this format, ``False``
            otherwise.
        """

    @abc.abstractmethod
    def validate_content(self, path: str, content: bytes) -> bool:
        """Validate artifact content against format-specific rules.

        Performs deeper content inspection beyond path validation.  Examples
        include verifying POM XML well-formedness (Maven), validating
        ``package.json`` schema (npm), or checking ``.nupkg`` ZIP structure
        (NuGet).

        Args:
            path: Artifact path (provides context for content validation).
            content: Raw binary content to validate.

        Returns:
            ``True`` if the content passes validation, ``False`` otherwise.
        """

    # ==================================================================
    # Concrete Helper Methods — Shared Behaviour
    # ==================================================================

    def detect_content_type(self, path: str) -> str:
        """Detect MIME type for an artifact based on its path.

        Uses format-specific ``content_types`` as a hint first, then falls
        back to Python's :mod:`mimetypes` module for extension-based
        detection.  Returns ``'application/octet-stream'`` as the ultimate
        default when no match is found.

        Subclasses may override this method to implement format-specific
        detection rules (e.g., Docker manifest media types based on content
        inspection).

        This method's return value aligns with the :attr:`Asset.content_type`
        model field.

        Args:
            path: Artifact path whose content type should be determined.

        Returns:
            A MIME type string (e.g., ``'application/java-archive'``,
            ``'application/json'``, ``'application/octet-stream'``).
        """
        # Attempt extension-based detection via the standard library.
        guessed_type, _ = mimetypes.guess_type(path, strict=False)
        if guessed_type is not None:
            self.logger.debug(
                "Detected content type '%s' for path '%s' via mimetypes",
                guessed_type,
                path,
            )
            return guessed_type

        # If the handler declares recognised content types and the path
        # extension is unrecognisable, return the first declared type as a
        # format-level default.
        if self.content_types:
            self.logger.debug(
                "Falling back to first declared content type '%s' for "
                "path '%s'",
                self.content_types[0],
                path,
            )
            return self.content_types[0]

        self.logger.debug(
            "Unable to determine content type for path '%s'; returning "
            "default '%s'",
            path,
            _DEFAULT_CONTENT_TYPE,
        )
        return _DEFAULT_CONTENT_TYPE

    def compute_checksums(self, content: bytes) -> dict[str, str]:
        """Compute MD5, SHA-1, and SHA-256 checksums for artifact content.

        The returned dictionary keys match the :class:`Asset` model checksum
        fields: ``checksum_md5``, ``checksum_sha1``, ``checksum_sha256``.
        All values are lowercase hexadecimal digest strings.

        Uses the :mod:`hashlib` standard library with ``usedforsecurity=False``
        for the MD5 hash (to avoid FIPS-mode restrictions, since MD5 is used
        only for integrity checking, not security).

        Args:
            content: Raw binary content to hash.

        Returns:
            A dictionary with three keys:

            - ``'md5'`` — 32-character hex MD5 digest.
            - ``'sha1'`` — 40-character hex SHA-1 digest.
            - ``'sha256'`` — 64-character hex SHA-256 digest.

        Example::

            handler = SomeFormatHandler()
            checksums = handler.compute_checksums(b"hello world")
            # checksums == {
            #     'md5': '5eb63bbbe01eeed093cb22bb8f5acdc3',
            #     'sha1': '2aae6c35c94fcfb415dbe95f408b9ce91ee846ed',
            #     'sha256': 'b94d27b9934d3e08a52e52d7da7dabfa...',
            # }
        """
        md5_digest: str = hashlib.md5(
            content, usedforsecurity=False
        ).hexdigest()
        sha1_digest: str = hashlib.sha1(
            content, usedforsecurity=False
        ).hexdigest()
        sha256_digest: str = hashlib.sha256(content).hexdigest()

        self.logger.debug(
            "Computed checksums — MD5=%s, SHA1=%s, SHA256=%s",
            md5_digest,
            sha1_digest,
            sha256_digest,
        )
        return {
            "md5": md5_digest,
            "sha1": sha1_digest,
            "sha256": sha256_digest,
        }

    def normalize_path(self, path: str) -> str:
        """Normalise an artifact path for consistent storage and lookup.

        Performs the following transformations:

        1. Strips leading and trailing whitespace.
        2. Replaces backslash (``\\``) path separators with forward slash
           (``/``) for cross-platform consistency.
        3. Collapses multiple consecutive slashes (``//``) into a single
           slash.
        4. Strips leading and trailing slashes.

        The normalised path matches the format stored in the
        :attr:`Asset.path` model field.

        Args:
            path: Raw artifact path to normalise.

        Returns:
            The cleaned, normalised path string.  An empty string is
            returned only if the input was empty or consisted solely of
            slashes.

        Examples::

            handler.normalize_path("/org/apache//commons/")
            # => "org/apache/commons"

            handler.normalize_path("\\\\windows\\\\style\\\\path")
            # => "windows/style/path"
        """
        # Strip whitespace.
        cleaned: str = path.strip()
        # Replace backslash separators.
        cleaned = cleaned.replace("\\", "/")
        # Collapse duplicate slashes.
        cleaned = _MULTI_SLASH_RE.sub("/", cleaned)
        # Strip leading/trailing slashes.
        cleaned = cleaned.strip("/")
        return cleaned

    def get_remote_url(self, remote_base_url: str, path: str) -> str:
        """Construct a full remote URL for proxy repository fetches.

        Joins the remote repository's base URL with the artifact path,
        ensuring exactly one slash between the two parts.  This method is
        used by :mod:`src.app.services.proxy_service` to resolve upstream
        artifact URLs.

        Args:
            remote_base_url: Base URL of the remote repository
                (e.g., ``'https://repo1.maven.org/maven2'``).
            path: Artifact path to append (will be normalised first).

        Returns:
            The fully qualified remote URL.

        Example::

            handler.get_remote_url(
                "https://repo1.maven.org/maven2/",
                "org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar"
            )
            # => "https://repo1.maven.org/maven2/org/apache/commons/..."
        """
        # Normalise the path component.
        normalized_path: str = self.normalize_path(path)
        # Ensure the base URL does not have a trailing slash, then join.
        base: str = remote_base_url.rstrip("/")
        if not normalized_path:
            return base
        return f"{base}/{normalized_path}"

    def is_prerelease(self, component_version: str) -> bool:
        """Determine whether a component version is a pre-release.

        The default implementation returns ``False`` for all versions.
        Format-specific subclasses should override this method to implement
        their own pre-release detection logic.  For example:

        - **Maven**: Returns ``True`` for versions ending in ``-SNAPSHOT``.
        - **npm**: Returns ``True`` for versions containing a pre-release
          tag (e.g., ``1.0.0-beta.1``).
        - **NuGet**: Returns ``True`` for versions with a pre-release
          suffix (e.g., ``1.0.0-preview``).

        This method is used by :mod:`src.app.services.cleanup_service` for
        format-specific pre-release detection during cleanup policy
        evaluation (F-204).

        Args:
            component_version: The version string to evaluate.

        Returns:
            ``True`` if the version is a pre-release, ``False`` otherwise.
        """
        return False

    def merge_metadata(self, metadata_list: list[bytes]) -> bytes | None:
        """Merge metadata from multiple group repository members.

        When a group repository resolves a metadata request, it collects
        metadata from each member repository and then merges them into a
        single response.  This method implements the merge logic.

        The default implementation returns ``None``, indicating that this
        format does not support metadata merging.  Format-specific subclasses
        should override this method when applicable:

        - **Maven**: Merges ``maven-metadata.xml`` files by combining
          ``<version>`` elements and updating ``<lastUpdated>`` timestamps.
        - **npm**: Merges package version lists from multiple registries.
        - **Docker**: Merges tag lists from multiple registries.

        Args:
            metadata_list: List of raw metadata byte strings from each
                member repository.

        Returns:
            Merged metadata as bytes, or ``None`` if merging is not
            supported by this format.
        """
        return None

    # ==================================================================
    # Dunder Methods
    # ==================================================================

    def __repr__(self) -> str:
        """Return a developer-friendly string representation."""
        return (
            f"<{self.__class__.__name__} "
            f"format_name={self.format_name!r} "
            f"content_types={self.content_types!r}>"
        )

    def __str__(self) -> str:
        """Return a human-readable string representation."""
        return f"FormatHandler({self.format_name})"
