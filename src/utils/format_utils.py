"""
Format utility functions for the Flask Binary Repository Management System.

Provides format detection (determining repository format from file paths,
content types, or metadata), content type mapping (mapping repository formats
to MIME content types), and coordinate parsing for each of the 7 supported
repository formats (Maven, npm, Docker, NuGet, PyPI, APT, Raw).

Used extensively by the repository management service (Feature F-101
Multi-Format Support) and storage service (Features F-201, F-202) for
artifact classification and metadata extraction.

Functions:
    detect_format          — Detect repository format from path and/or content type
    get_content_type       — Get MIME content type for a repository format
    parse_coordinates      — Parse format-specific coordinates from a path
    get_format_content_types — Get the full format-to-content-type mapping
    is_supported_format    — Check whether a format name is supported
    normalize_format_name  — Normalize a format name to its canonical form
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Union


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS: List[str] = [
    "maven", "npm", "docker", "nuget", "pypi", "apt", "raw",
]
"""Canonical names for all supported repository formats."""

FORMAT_CONTENT_TYPES: Dict[str, str] = {
    "maven": "application/java-archive",
    "npm": "application/gzip",
    "docker": "application/vnd.docker.distribution.manifest.v2+json",
    "nuget": "application/zip",
    "pypi": "application/gzip",
    "apt": "application/vnd.debian.binary-package",
    "raw": "application/octet-stream",
}
"""Mapping of canonical format names to their primary MIME content types."""

FORMAT_ALIASES: Dict[str, str] = {
    "maven2": "maven",
    "maven3": "maven",
    "nodejs": "npm",
    "node": "npm",
    "debian": "apt",
    "deb": "apt",
    "python": "pypi",
    "pip": "pypi",
    "container": "docker",
    "oci": "docker",
    "dotnet": "nuget",
    "binary": "raw",
}
"""Mapping of common aliases to their canonical format names."""

# File-extension-based detection patterns
_EXTENSION_FORMAT_MAP: Dict[str, str] = {
    ".jar": "maven",
    ".pom": "maven",
    ".war": "maven",
    ".ear": "maven",
    ".aar": "maven",
    ".tgz": "npm",
    ".nupkg": "nuget",
    ".deb": "apt",
    ".whl": "pypi",
}
"""Mapping of file extensions to format names for path-based detection."""

# Content-type-based detection patterns
_CONTENT_TYPE_FORMAT_MAP: Dict[str, str] = {
    "application/java-archive": "maven",
    "application/vnd.docker.distribution.manifest.v2+json": "docker",
    "application/vnd.docker.distribution.manifest.v1+json": "docker",
    "application/vnd.docker.distribution.manifest.list.v2+json": "docker",
    "application/vnd.oci.image.manifest.v1+json": "docker",
    "application/vnd.oci.image.index.v1+json": "docker",
    "application/vnd.debian.binary-package": "apt",
}
"""Mapping of MIME content types to format names for content-type detection."""

# Path-pattern-based detection (compiled regexes)
_MAVEN_PATH_RE = re.compile(
    r"^(?:[a-zA-Z0-9._-]+/){2,}.+\.(jar|pom|war|ear|aar|xml)$"
)
_NPM_PATH_RE = re.compile(r".*/-/.+\.tgz$")
_NPM_SCOPED_RE = re.compile(r"^@[a-zA-Z0-9._-]+/")
_DOCKER_PATH_RE = re.compile(r"^v2/.+/(manifests|blobs)/")
_NUGET_PATH_RE = re.compile(r".*\.nupkg$", re.IGNORECASE)
_PYPI_SDIST_RE = re.compile(r".+\.(tar\.gz|tar\.bz2|zip)$")
_PYPI_WHEEL_RE = re.compile(r".+\.whl$")
_PYPI_PATH_RE = re.compile(r"packages/source/")
_APT_PATH_RE = re.compile(r"(^pool/|\.deb$)")
_APT_METADATA_RE = re.compile(r"(Packages|Release|InRelease|Sources)(\.gz|\.bz2|\.xz)?$")


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def normalize_format_name(name: str) -> str:
    """Normalize a format name to its canonical lowercase form.

    Strips whitespace, converts to lowercase, and resolves known aliases
    (e.g. ``'maven2'`` → ``'maven'``, ``'debian'`` → ``'apt'``).

    Parameters
    ----------
    name : str
        The format name to normalize.

    Returns
    -------
    str
        The canonical format name in lowercase.

    Raises
    ------
    TypeError
        If *name* is not a ``str``.
    """
    if not isinstance(name, str):
        raise TypeError(f"Expected str, got {type(name).__name__}")
    lower = name.strip().lower()
    return FORMAT_ALIASES.get(lower, lower)


def is_supported_format(format_name: Any) -> bool:
    """Check whether *format_name* corresponds to a supported format.

    Handles ``None`` and non-string inputs gracefully by returning
    ``False`` instead of raising.

    Parameters
    ----------
    format_name : str
        The format name to check (case-insensitive, aliases accepted).

    Returns
    -------
    bool
        ``True`` if the format is supported, ``False`` otherwise.
    """
    if not isinstance(format_name, str):
        return False
    if not format_name.strip():
        return False
    try:
        normalized = normalize_format_name(format_name)
    except (TypeError, ValueError):
        return False
    return normalized in SUPPORTED_FORMATS


def get_content_type(format_name: str) -> str:
    """Return the primary MIME content type for a repository format.

    If *format_name* is unknown, falls back to ``'application/octet-stream'``.

    Parameters
    ----------
    format_name : str
        A repository format name (case-insensitive, aliases accepted).

    Returns
    -------
    str
        A MIME content type string.

    Raises
    ------
    TypeError
        If *format_name* is not a ``str``.
    """
    if not isinstance(format_name, str):
        raise TypeError(f"Expected str, got {type(format_name).__name__}")
    normalized = normalize_format_name(format_name)
    return FORMAT_CONTENT_TYPES.get(normalized, "application/octet-stream")


def get_format_content_types() -> Dict[str, str]:
    """Return a copy of the full format-to-content-type mapping.

    Returns
    -------
    dict[str, str]
        A dictionary mapping canonical format names to MIME content types.
    """
    return dict(FORMAT_CONTENT_TYPES)


def detect_format(
    path: str,
    content_type: Optional[str] = None,
) -> str:
    """Detect the repository format from a file path and/or content type.

    Detection priority:

    1. Explicit content-type match (if provided)
    2. File extension match
    3. Path pattern match (npm ``/-/``, Docker ``v2/``, PyPI, APT pool)
    4. Fallback to ``'raw'``

    Parameters
    ----------
    path : str
        The artifact file path or URI fragment.
    content_type : str or None, optional
        An HTTP Content-Type header value for content-type-based detection.

    Returns
    -------
    str
        The detected canonical format name.

    Raises
    ------
    TypeError
        If *path* is not a ``str``.
    """
    if not isinstance(path, str):
        raise TypeError(f"Expected str for path, got {type(path).__name__}")

    # --- 1. Content-type detection (highest priority) ---
    if content_type and isinstance(content_type, str):
        ct_lower = content_type.strip().lower()
        for known_ct, fmt in _CONTENT_TYPE_FORMAT_MAP.items():
            if ct_lower == known_ct.lower():
                return fmt

    # Handle empty path after content-type check
    if not path.strip():
        return "raw"

    # --- 2. Extension-based detection ---
    lower_path = path.lower()

    # PyPI wheel has a unique extension
    if _PYPI_WHEEL_RE.match(lower_path):
        return "pypi"

    # Check simple extension map
    _, ext = os.path.splitext(lower_path)
    # Handle .tar.gz double extension
    if lower_path.endswith(".tar.gz"):
        ext = ".tar.gz"
    elif lower_path.endswith(".tar.bz2"):
        ext = ".tar.bz2"

    if ext in _EXTENSION_FORMAT_MAP:
        # .tgz could be npm or pypi — disambiguate via path pattern
        if ext == ".tgz":
            if _NPM_PATH_RE.match(path) or _NPM_SCOPED_RE.match(path):
                return "npm"
            if _PYPI_PATH_RE.search(path):
                return "pypi"
            # Default .tgz to npm (most common tgz usage in repo managers)
            return "npm"
        return _EXTENSION_FORMAT_MAP[ext]

    # --- 3. Path pattern detection ---
    if _DOCKER_PATH_RE.match(path):
        return "docker"

    if _NPM_PATH_RE.match(path) or _NPM_SCOPED_RE.match(path):
        return "npm"

    if _PYPI_PATH_RE.search(path):
        return "pypi"

    if _APT_PATH_RE.search(path) or _APT_METADATA_RE.search(path):
        return "apt"

    # PyPI sdist (.tar.gz) if in a pypi-like path
    if ext == ".tar.gz" and _PYPI_PATH_RE.search(path):
        return "pypi"

    # Maven path pattern (multiple segments with groupId/artifactId/version)
    if _MAVEN_PATH_RE.match(path):
        return "maven"

    # --- 4. Fallback ---
    return "raw"


def parse_coordinates(format_name: str, path: str) -> Dict[str, str]:
    """Parse format-specific coordinates from an artifact path.

    Each format has its own coordinate system:

    - **Maven**: ``group_id``, ``artifact_id``, ``version``, ``extension``,
      optional ``classifier``
    - **npm**: ``name`` (may include scope), ``version``
    - **Docker**: ``repository``, ``tag`` or ``digest``
    - **NuGet**: ``id``, ``version``
    - **PyPI**: ``name``, ``version``, optional ``format`` (sdist/wheel)
    - **APT**: ``package``, ``version``, ``architecture``
    - **Raw**: ``path``

    Parameters
    ----------
    format_name : str
        The repository format name (case-insensitive, aliases accepted).
    path : str
        The artifact path to parse.

    Returns
    -------
    dict[str, str]
        A dictionary of parsed coordinate fields.

    Raises
    ------
    ValueError
        If *format_name* is unsupported or *path* cannot be parsed.
    TypeError
        If arguments are not strings.
    """
    if not isinstance(format_name, str):
        raise TypeError(f"Expected str for format_name, got {type(format_name).__name__}")
    if not isinstance(path, str):
        raise TypeError(f"Expected str for path, got {type(path).__name__}")

    normalized = normalize_format_name(format_name)
    if normalized not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported format: '{format_name}'. "
            f"Supported formats: {', '.join(SUPPORTED_FORMATS)}"
        )

    parser = _FORMAT_PARSERS.get(normalized)
    if parser is None:
        raise ValueError(f"No parser registered for format: '{normalized}'")

    return parser(path)


# ---------------------------------------------------------------------------
# Format-specific coordinate parsers (private)
# ---------------------------------------------------------------------------


def _parse_maven_coordinates(path: str) -> Dict[str, str]:
    """Parse Maven GAV coordinates from a repository path.

    Expected path format:
    ``{group_segments}/{artifact_id}/{version}/{filename}``

    Examples:
    - ``com/example/mylib/1.0.0/mylib-1.0.0.jar``
    - ``org/apache/commons/commons-lang3/3.12.0/commons-lang3-3.12.0.jar``
    """
    parts = path.strip("/").split("/")
    if len(parts) < 4:
        raise ValueError(
            f"Invalid Maven path: expected at least 4 segments "
            f"(group/artifact/version/file), got {len(parts)}: '{path}'"
        )

    filename = parts[-1]
    version = parts[-2]
    artifact_id = parts[-3]
    group_segments = parts[:-3]

    if not group_segments:
        raise ValueError(f"Invalid Maven path: missing group segments: '{path}'")

    group_id = ".".join(group_segments)

    # Parse extension and optional classifier from filename
    base, ext = _split_extension(filename)
    classifier = _extract_maven_classifier(base, artifact_id, version)

    result: Dict[str, str] = {
        "group_id": group_id,
        "artifact_id": artifact_id,
        "version": version,
        "extension": ext,
    }
    if classifier:
        result["classifier"] = classifier

    return result


def _split_extension(filename: str) -> tuple[str, str]:
    """Split a filename into base and extension, handling double extensions."""
    if filename.endswith(".tar.gz"):
        return filename[:-7], "tar.gz"
    if filename.endswith(".tar.bz2"):
        return filename[:-8], "tar.bz2"
    base, ext = os.path.splitext(filename)
    return base, ext.lstrip(".")


def _extract_maven_classifier(
    base: str, artifact_id: str, version: str
) -> Optional[str]:
    """Extract the classifier from a Maven filename base."""
    prefix = f"{artifact_id}-{version}"
    if base.startswith(prefix) and len(base) > len(prefix):
        remainder = base[len(prefix):]
        if remainder.startswith("-"):
            return remainder[1:]
    return None


def _parse_npm_coordinates(path: str) -> Dict[str, str]:
    """Parse npm package coordinates from a registry path.

    Expected formats:
    - ``@scope/package/-/package-version.tgz`` (scoped)
    - ``package/-/package-version.tgz`` (unscoped)
    """
    # Handle scoped packages
    if path.startswith("@"):
        scope_match = re.match(r"^(@[^/]+)/([^/]+)/-/\2-(.+)\.tgz$", path)
        if scope_match:
            scope = scope_match.group(1)
            pkg = scope_match.group(2)
            version = scope_match.group(3)
            return {
                "name": f"{scope}/{pkg}",
                "scope": scope,
                "version": version,
            }
        # Looser parse for scoped packages
        parts = path.split("/-/")
        if len(parts) == 2:
            full_name = parts[0]
            tarball = parts[1]
            scope_parts = full_name.split("/", 1)
            scope = scope_parts[0] if len(scope_parts) > 1 else ""
            pkg = scope_parts[1] if len(scope_parts) > 1 else scope_parts[0]
            version = _extract_npm_version(tarball, pkg)
            return {
                "name": full_name,
                "scope": scope,
                "version": version,
            }

    # Handle unscoped packages
    unscoped_match = re.match(r"^([^/]+)/-/\1-(.+)\.tgz$", path)
    if unscoped_match:
        return {
            "name": unscoped_match.group(1),
            "version": unscoped_match.group(2),
        }

    # Looser parse for unscoped
    parts = path.split("/-/")
    if len(parts) == 2:
        pkg_name = parts[0].strip("/")
        tarball = parts[1]
        version = _extract_npm_version(tarball, pkg_name)
        return {"name": pkg_name, "version": version}

    raise ValueError(f"Cannot parse npm coordinates from path: '{path}'")


def _extract_npm_version(tarball: str, pkg_name: str) -> str:
    """Extract version from an npm tarball filename."""
    base = tarball.replace(".tgz", "")
    if base.startswith(f"{pkg_name}-"):
        return base[len(pkg_name) + 1:]
    return base


def _parse_docker_coordinates(path: str) -> Dict[str, str]:
    """Parse Docker image coordinates from a reference string.

    Expected formats:
    - ``repository:tag``
    - ``repository@sha256:digest``
    - ``v2/repository/manifests/tag``
    """
    result: Dict[str, str] = {}

    # Handle v2 API path format
    v2_match = re.match(r"^v2/(.+)/(manifests|blobs)/(.+)$", path)
    if v2_match:
        result["repository"] = v2_match.group(1)
        ref = v2_match.group(3)
        if ref.startswith("sha256:"):
            result["digest"] = ref
        else:
            result["tag"] = ref
        return result

    # Handle digest reference (repo@sha256:...)
    if "@" in path:
        repo, digest = path.rsplit("@", 1)
        result["repository"] = repo
        result["digest"] = digest
        return result

    # Handle tag reference (repo:tag)
    if ":" in path:
        repo, tag = path.rsplit(":", 1)
        result["repository"] = repo
        result["tag"] = tag
        return result

    # Plain repository name (no tag/digest)
    result["repository"] = path
    result["tag"] = "latest"
    return result


def _parse_nuget_coordinates(path: str) -> Dict[str, str]:
    """Parse NuGet package coordinates from a path.

    Expected format: ``PackageId/Version/packageid.version.nupkg``
    """
    parts = path.strip("/").split("/")
    if len(parts) >= 2:
        pkg_id = parts[0]
        version = parts[1]
        return {"id": pkg_id, "version": version}

    # Try to parse from filename
    filename = parts[0] if parts else path
    match = re.match(r"^(.+?)\.(\d+\..+)\.nupkg$", filename, re.IGNORECASE)
    if match:
        return {"id": match.group(1), "version": match.group(2)}

    raise ValueError(f"Cannot parse NuGet coordinates from path: '{path}'")


def _parse_pypi_coordinates(path: str) -> Dict[str, str]:
    """Parse PyPI package coordinates from a distribution path.

    Handles both source distributions (sdist) and wheel distributions.
    """
    filename = os.path.basename(path)

    # Wheel format: name-version(-tag).whl
    if filename.endswith(".whl"):
        wheel_match = re.match(r"^([A-Za-z0-9_.-]+?)-(\d+[^-]*)", filename)
        if wheel_match:
            name = _normalize_pypi_name(wheel_match.group(1))
            version = wheel_match.group(2)
            return {"name": name, "version": version, "format": "wheel"}

    # Source distribution: name-version.tar.gz or name-version.zip
    sdist_match = re.match(
        r"^([A-Za-z0-9_.-]+?)-(\d+\..+?)\.(tar\.gz|tar\.bz2|zip)$",
        filename,
    )
    if sdist_match:
        name = _normalize_pypi_name(sdist_match.group(1))
        version = sdist_match.group(2)
        return {"name": name, "version": version, "format": "sdist"}

    raise ValueError(f"Cannot parse PyPI coordinates from path: '{path}'")


def _normalize_pypi_name(name: str) -> str:
    """Normalize a PyPI package name (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_apt_coordinates(path: str) -> Dict[str, str]:
    """Parse APT/Debian package coordinates from a path.

    Expected format: ``pool/.../package_version_architecture.deb``
    """
    filename = os.path.basename(path)

    # Standard .deb naming: package_version_arch.deb
    deb_match = re.match(
        r"^([a-zA-Z0-9][a-zA-Z0-9.+\-]+?)_([^_]+)_([^_]+)\.deb$",
        filename,
    )
    if deb_match:
        return {
            "package": deb_match.group(1),
            "version": deb_match.group(2),
            "architecture": deb_match.group(3),
        }

    # Fallback: try to extract from path
    raise ValueError(f"Cannot parse APT coordinates from path: '{path}'")


def _parse_raw_coordinates(path: str) -> Dict[str, str]:
    """Parse raw format coordinates — simply preserves the path."""
    return {"path": path}


# ---------------------------------------------------------------------------
# Parser registry
# ---------------------------------------------------------------------------

_FORMAT_PARSERS: Dict[str, Any] = {
    "maven": _parse_maven_coordinates,
    "npm": _parse_npm_coordinates,
    "docker": _parse_docker_coordinates,
    "nuget": _parse_nuget_coordinates,
    "pypi": _parse_pypi_coordinates,
    "apt": _parse_apt_coordinates,
    "raw": _parse_raw_coordinates,
}
"""Registry mapping canonical format names to their parser functions."""
