"""
NuGet V3 API protocol implementation.

Implements the NuGet V3 REST API protocol resources:
- Service Index (/v3/index.json): entry point listing available resources
- Package Content (flat container): direct .nupkg download and version listing
- Search Query Service: full-text package search with pagination
- Search Autocomplete Service: package ID and version autocomplete
- Package Publish: PUT endpoint for pushing .nupkg packages
- Registration: package metadata with version listings and dependency info
- Catalog: append-only log of package changes for client synchronization

All responses conform to the NuGet V3 protocol specification as expected
by the dotnet CLI, Visual Studio, and NuGet.exe clients.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NuGet V3 Service Index resource types (@type values)
# These are well-known identifiers used by NuGet clients
# ---------------------------------------------------------------------------
RESOURCE_TYPE_PACKAGE_BASE_ADDRESS = "PackageBaseAddress/3.0.0"
RESOURCE_TYPE_SEARCH_QUERY = "SearchQueryService/3.5.0"
RESOURCE_TYPE_SEARCH_AUTOCOMPLETE = "SearchAutocompleteService/3.5.0"
RESOURCE_TYPE_PACKAGE_PUBLISH = "PackagePublish/2.0.0"
RESOURCE_TYPE_REGISTRATIONS_BASE = "RegistrationsBaseUrl/3.6.0"
RESOURCE_TYPE_CATALOG = "Catalog/3.0.0"
RESOURCE_TYPE_SYMBOL_PACKAGE_PUBLISH = "SymbolPackagePublish/4.9.0"

# Service Index version
SERVICE_INDEX_VERSION = "3.0.0"

# NuGet V3 content types
NUGET_JSON_CONTENT_TYPE = "application/json"
NUGET_PACKAGE_CONTENT_TYPE = "application/octet-stream"

# Registration page size (number of versions per page)
REGISTRATION_PAGE_SIZE: int = 64

# Search results default and maximum page sizes
SEARCH_DEFAULT_PAGE_SIZE: int = 20
SEARCH_MAX_PAGE_SIZE: int = 1000

# Catalog page size
CATALOG_PAGE_SIZE: int = 550

# NuGet V3 context URLs (JSON-LD context)
NUGET_CONTEXT_URL = "https://schema.nuget.org/schema#"
NUGET_CATALOG_CONTEXT_URL = "https://schema.nuget.org/catalog#"

# Registration entry @type values
REGISTRATION_TYPE_CATALOG_ENTRY = "PackageDetails"
REGISTRATION_TYPE_PACKAGE = "Package"

# Catalog entry @type values
CATALOG_TYPE_PACKAGE_DETAILS = "nuget:PackageDetails"
CATALOG_TYPE_PACKAGE_DELETE = "nuget:PackageDelete"

# SemVer pattern for NuGet version parsing.
# Handles NuGet's extended SemVer 2.0.0 format with optional 4th revision
# segment: major.minor.patch[.revision][-prerelease][+buildmetadata]
NUGET_SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:\.(?P<revision>0|[1-9]\d*))?"
    r"(?:-(?P<prerelease>[\da-zA-Z\-]+(?:\.[\da-zA-Z\-]+)*))?"
    r"(?:\+(?P<buildmetadata>[\da-zA-Z\-]+(?:\.[\da-zA-Z\-]+)*))?$"
)


# ===================================================================
# Timestamp Utilities
# ===================================================================

def format_nuget_timestamp(dt: datetime | None = None) -> str:
    """Format a datetime as a NuGet protocol timestamp string.

    NuGet uses ISO 8601 with exactly 7 fractional digits for microseconds,
    terminated by ``Z`` for UTC:  ``2024-06-15T12:00:00.0000000Z``.

    Args:
        dt: The datetime to format.  If *None*, the current UTC time is used.

    Returns:
        A NuGet-formatted ISO 8601 timestamp string.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    # Ensure UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    # NuGet expects exactly 7 fractional digits
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    microseconds = dt.microsecond
    # Pad to 7 digits (Python only gives 6 for microseconds; append a trailing zero)
    fractional = f"{microseconds:06d}0"
    return f"{base}.{fractional}Z"


def parse_nuget_timestamp(timestamp_str: str) -> datetime:
    """Parse a NuGet protocol timestamp string back to a *datetime* object.

    Handles common ISO 8601 variants that appear in NuGet V3 responses,
    including 7-digit fractional seconds and the ``Z`` suffix.

    Args:
        timestamp_str: An ISO 8601 timestamp string (e.g.
            ``"2024-06-15T12:00:00.0000000Z"``).

    Returns:
        A timezone-aware *datetime* in UTC.

    Raises:
        ValueError: If the timestamp string cannot be parsed.
    """
    cleaned = timestamp_str.strip()
    # Replace trailing Z with +00:00 for fromisoformat compatibility
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    # Python's fromisoformat handles up to 6 fractional digits; NuGet sends 7.
    # Trim the 7th fractional digit if present.
    # Pattern: digits after '.' before '+' or '-' timezone offset (or end)
    dot_idx = cleaned.find(".")
    if dot_idx != -1:
        # Find where the fractional part ends (at '+' or '-' after the dot)
        rest = cleaned[dot_idx + 1 :]
        frac_end = len(rest)
        for i, ch in enumerate(rest):
            if ch in ("+", "-"):
                frac_end = i
                break
        frac_part = rest[:frac_end]
        tz_part = rest[frac_end:]
        if len(frac_part) > 6:
            frac_part = frac_part[:6]
        cleaned = cleaned[: dot_idx + 1] + frac_part + tz_part
    result = datetime.fromisoformat(cleaned)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


# ===================================================================
# Version Utilities
# ===================================================================

def _parse_nuget_version(
    version: str,
) -> tuple[int, int, int, int, list[str] | None, list[str] | None]:
    """Parse a NuGet version string into its constituent parts.

    NuGet uses SemVer 2.0.0 extended with an optional 4th *revision* segment:
    ``major.minor.patch[.revision][-prerelease][+buildmetadata]``.

    Args:
        version: A version string such as ``"13.0.3"`` or ``"1.0.0.0-beta.1"``.

    Returns:
        A tuple of ``(major, minor, patch, revision, prerelease_parts,
        buildmetadata_parts)`` where *revision* defaults to ``0`` and the
        label lists are *None* when absent.
    """
    match = NUGET_SEMVER_PATTERN.match(version.strip())
    if match:
        major = int(match.group("major"))
        minor = int(match.group("minor"))
        patch = int(match.group("patch"))
        revision_str = match.group("revision")
        revision = int(revision_str) if revision_str is not None else 0
        pre_str = match.group("prerelease")
        prerelease = pre_str.split(".") if pre_str else None
        build_str = match.group("buildmetadata")
        buildmeta = build_str.split(".") if build_str else None
        return (major, minor, patch, revision, prerelease, buildmeta)
    # Fallback: try a lenient parse for versions with leading zeros
    parts = version.strip().split("-", 1)
    numeric_part = parts[0].split("+", 1)[0]
    segments = numeric_part.split(".")
    try:
        major = int(segments[0]) if len(segments) > 0 else 0
        minor = int(segments[1]) if len(segments) > 1 else 0
        patch = int(segments[2]) if len(segments) > 2 else 0
        revision = int(segments[3]) if len(segments) > 3 else 0
    except ValueError:
        major, minor, patch, revision = 0, 0, 0, 0
    pre_str = parts[1].split("+", 1)[0] if len(parts) > 1 else None
    prerelease = pre_str.split(".") if pre_str else None
    build_raw = version.strip().split("+", 1)
    buildmeta = build_raw[1].split(".") if len(build_raw) > 1 else None
    return (major, minor, patch, revision, prerelease, buildmeta)


def compare_nuget_versions(version_a: str, version_b: str) -> int:
    """Compare two NuGet version strings using SemVer ordering.

    Comparison rules (SemVer 2.0.0 + NuGet 4-segment extension):
    1. Compare major, minor, patch, revision numerically.
    2. A version **without** a pre-release label has *higher* precedence than
       the same numeric version **with** a pre-release label.
    3. Pre-release identifiers are compared dot-segment by dot-segment:
       numeric identifiers by integer value, alphanumeric identifiers by ASCII
       sort order.  Numeric identifiers always have lower precedence than
       alphanumeric identifiers.
    4. Build metadata is **ignored** for comparison purposes.

    Args:
        version_a: First version string.
        version_b: Second version string.

    Returns:
        A negative integer if *version_a* < *version_b*, zero if equal,
        or a positive integer if *version_a* > *version_b*.
    """
    a = _parse_nuget_version(version_a)
    b = _parse_nuget_version(version_b)

    # Compare numeric segments: major, minor, patch, revision
    for i in range(4):
        if a[i] != b[i]:
            return -1 if a[i] < b[i] else 1

    # Pre-release comparison (index 4)
    a_pre = a[4]
    b_pre = b[4]

    if a_pre is None and b_pre is None:
        return 0
    if a_pre is not None and b_pre is None:
        # Pre-release has lower precedence than release
        return -1
    if a_pre is None and b_pre is not None:
        return 1

    # Both have pre-release labels — compare dot-separated identifiers
    assert a_pre is not None and b_pre is not None
    max_len = max(len(a_pre), len(b_pre))
    for i in range(max_len):
        if i >= len(a_pre):
            return -1  # Fewer fields = lower precedence
        if i >= len(b_pre):
            return 1
        ai, bi = a_pre[i], b_pre[i]
        a_numeric = ai.isdigit()
        b_numeric = bi.isdigit()
        if a_numeric and b_numeric:
            diff = int(ai) - int(bi)
            if diff != 0:
                return diff
        elif a_numeric:
            # Numeric identifiers have lower precedence than alphanumeric
            return -1
        elif b_numeric:
            return 1
        else:
            if ai < bi:
                return -1
            if ai > bi:
                return 1
    return 0


def sort_nuget_versions(versions: list[str]) -> list[str]:
    """Sort a list of NuGet version strings in ascending SemVer order.

    Uses :func:`compare_nuget_versions` internally for pairwise comparison.

    Args:
        versions: An unsorted list of NuGet version strings.

    Returns:
        A new list sorted in ascending order.
    """
    import functools
    return sorted(
        versions,
        key=functools.cmp_to_key(compare_nuget_versions),
    )


def normalize_version(version: str) -> str:
    """Normalize a NuGet version string.

    Normalization rules:
    * Strip leading zeros from numeric segments (``01.02.03`` -> ``1.2.3``).
    * Remove a trailing zero revision segment when it is the default
      (``1.0.0.0`` -> ``1.0.0``).
    * Preserve pre-release and build metadata labels.

    Args:
        version: A raw NuGet version string.

    Returns:
        The normalized version string.
    """
    parsed = _parse_nuget_version(version)
    major, minor, patch, revision, prerelease, buildmeta = parsed

    if revision != 0:
        base = f"{major}.{minor}.{patch}.{revision}"
    else:
        base = f"{major}.{minor}.{patch}"

    if prerelease:
        base += "-" + ".".join(prerelease)
    if buildmeta:
        base += "+" + ".".join(buildmeta)
    return base


def is_prerelease_version(version: str) -> bool:
    """Check whether a NuGet version string contains a pre-release identifier.

    Examples of pre-release versions: ``1.0.0-beta``, ``2.0.0-rc.1``,
    ``1.0.0-alpha.1``.

    Args:
        version: A NuGet version string.

    Returns:
        True if the version includes a pre-release label, False otherwise.
    """
    parsed = _parse_nuget_version(version)
    return parsed[4] is not None


# ===================================================================
# URL Construction Utilities
# ===================================================================

def construct_base_url(server_url: str, repository_name: str) -> str:
    """Construct the base URL for NuGet V3 resource URLs.

    Format: ``{server_url}/repository/{repository_name}``

    Trailing slashes on *server_url* are normalized.

    Args:
        server_url: The root server URL (e.g. ``https://nexus.example.com``).
        repository_name: The repository name.

    Returns:
        The constructed base URL without a trailing slash.
    """
    return f"{server_url.rstrip('/')}/repository/{repository_name}"


def construct_package_content_url(
    base_url: str, package_id: str, version: str
) -> str:
    """Construct the download URL for a .nupkg file.

    All path segments are lowercase per the NuGet flat container convention.

    Args:
        base_url: The repository base URL.
        package_id: The package identifier.
        version: The package version.

    Returns:
        The full download URL for the .nupkg artifact.
    """
    pid = package_id.lower()
    ver = version.lower()
    return f"{base_url.rstrip('/')}/v3/flat-container/{pid}/{ver}/{pid}.{ver}.nupkg"


def construct_registration_url(base_url: str, package_id: str) -> str:
    """Construct the registration index URL for a package.

    Format: ``{base_url}/v3/registration/{lowercase-id}/index.json``

    Args:
        base_url: The repository base URL.
        package_id: The package identifier.

    Returns:
        The registration index URL.
    """
    pid = package_id.lower()
    return f"{base_url.rstrip('/')}/v3/registration/{pid}/index.json"


def construct_search_url(base_url: str) -> str:
    """Construct the search query service URL.

    Format: ``{base_url}/v3/query``

    Args:
        base_url: The repository base URL.

    Returns:
        The search query URL.
    """
    return f"{base_url.rstrip('/')}/v3/query"


# ===================================================================
# Package Content (Flat Container) Resources
# ===================================================================

def get_package_content_path(package_id: str, version: str) -> str:
    """Construct the flat container relative path for a .nupkg file.

    All path segments are lowercase per the NuGet flat container convention.

    Path format::

        v3/flat-container/{id}/{ver}/{id}.{ver}.nupkg

    Args:
        package_id: The package identifier.
        version: The package version.

    Returns:
        The relative path string.
    """
    pid = package_id.lower()
    ver = version.lower()
    return f"v3/flat-container/{pid}/{ver}/{pid}.{ver}.nupkg"


def get_nuspec_content_path(package_id: str, version: str) -> str:
    """Construct the flat container relative path for the .nuspec file.

    Path format::

        v3/flat-container/{id}/{ver}/{id}.nuspec

    Args:
        package_id: The package identifier.
        version: The package version.

    Returns:
        The relative path string.
    """
    pid = package_id.lower()
    ver = version.lower()
    return f"v3/flat-container/{pid}/{ver}/{pid}.nuspec"


def generate_package_versions(package_id: str, versions: list[str]) -> bytes:
    """Generate the flat container version listing for a package.

    Returned at ``GET /v3/flat-container/{package-id}/index.json``.

    Version strings are normalized to lowercase and sorted in ascending
    SemVer order, consistent with the NuGet flat container specification.

    Args:
        package_id: The package identifier (used for logging context).
        versions: A list of version strings.

    Returns:
        JSON-encoded bytes containing the ``{"versions": [...]}`` document.
    """
    sorted_versions = sort_nuget_versions(versions)
    lowercase_versions = [v.lower() for v in sorted_versions]
    document: dict[str, Any] = {"versions": lowercase_versions}
    return json.dumps(document, indent=2).encode("utf-8")


# ===================================================================
# Service Index Generation
# ===================================================================

def generate_service_index(base_url: str, repository_name: str) -> bytes:
    """Generate the NuGet V3 Service Index (``/v3/index.json``).

    The service index is the entry point for all NuGet V3 clients.  It is a
    JSON document listing every available resource with its URL and ``@type``
    identifier.  Clients use these URLs for all subsequent operations (search,
    download, registration, etc.).

    Args:
        base_url: The root server URL (e.g. ``https://nexus.example.com``).
        repository_name: The name of the NuGet repository.

    Returns:
        JSON-encoded bytes of the service index.
    """
    repo_base = construct_base_url(base_url, repository_name)

    resources: list[dict[str, str]] = [
        {
            "@id": f"{repo_base}/v3/flat-container/",
            "@type": RESOURCE_TYPE_PACKAGE_BASE_ADDRESS,
            "comment": "Base URL for package content (flat container)",
        },
        {
            "@id": f"{repo_base}/v3/query",
            "@type": RESOURCE_TYPE_SEARCH_QUERY,
            "comment": "Search query endpoint",
        },
        {
            "@id": f"{repo_base}/v3/autocomplete",
            "@type": RESOURCE_TYPE_SEARCH_AUTOCOMPLETE,
            "comment": "Autocomplete endpoint for package IDs and versions",
        },
        {
            "@id": f"{repo_base}/api/v2/package",
            "@type": RESOURCE_TYPE_PACKAGE_PUBLISH,
            "comment": "Package publish endpoint",
        },
        {
            "@id": f"{repo_base}/v3/registration/",
            "@type": RESOURCE_TYPE_REGISTRATIONS_BASE,
            "comment": "Package registration (metadata) base URL",
        },
        {
            "@id": f"{repo_base}/v3/catalog/",
            "@type": RESOURCE_TYPE_CATALOG,
            "comment": "Package change catalog (append-only log)",
        },
    ]

    document: dict[str, Any] = {
        "version": SERVICE_INDEX_VERSION,
        "resources": resources,
        "@context": {
            "@vocab": "http://schema.nuget.org/services#",
            "comment": "http://schema.nuget.org/services#comment",
        },
    }
    return json.dumps(document, indent=2).encode("utf-8")


# ===================================================================
# Registration (Package Metadata) Resources
# ===================================================================

def _build_dependency_groups(dependencies: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert dependency information into NuGet V3 dependency group format.

    Each dependency group has a ``targetFramework`` and a list of dependencies.
    Each dependency within a group has ``id``, ``range`` (version range), and
    an optional ``registration`` URL.

    Args:
        dependencies: A list of dependency dicts, each optionally containing
            ``targetFramework``, ``id``, ``range``, and ``registration`` keys.
            May be *None* or empty.

    Returns:
        A list of formatted dependency group dicts ready for JSON
        serialization.
    """
    if not dependencies:
        return []

    # Group dependencies by target framework
    groups: dict[str, list[dict[str, Any]]] = {}
    for dep in dependencies:
        framework = dep.get("targetFramework", "")
        if framework not in groups:
            groups[framework] = []
        dep_entry: dict[str, Any] = {
            "@id": dep.get("@id", ""),
            "@type": "PackageDependency",
            "id": dep.get("id", ""),
            "range": dep.get("range", dep.get("version_range", "")),
        }
        if dep.get("registration"):
            dep_entry["registration"] = dep["registration"]
        groups[framework].append(dep_entry)

    result: list[dict[str, Any]] = []
    for framework, deps in groups.items():
        group: dict[str, Any] = {
            "@id": "",
            "@type": "PackageDependencyGroup",
            "dependencies": deps,
        }
        if framework:
            group["targetFramework"] = framework
        result.append(group)
    return result


def _build_catalog_entry(
    package_id: str,
    version_metadata: dict[str, Any],
    base_url: str,
    repository_name: str,
) -> dict[str, Any]:
    """Build a catalog entry dict for a single package version.

    Extracts standard NuGet metadata fields (id, version, authors,
    description, etc.) and constructs the ``packageContent`` URL pointing to
    the ``.nupkg`` download location.

    Args:
        package_id: The package identifier (original casing).
        version_metadata: A dict containing version-specific metadata.
        base_url: The root server URL.
        repository_name: The repository name.

    Returns:
        A catalog entry dict suitable for embedding in a registration page.
    """
    repo_base = construct_base_url(base_url, repository_name)
    pid_lower = package_id.lower()
    version = version_metadata.get("version", "0.0.0")
    version_lower = version.lower()

    # Deterministic @id based on package coordinates
    entry_id_hash = hashlib.sha256(
        f"{package_id}:{version}".encode("utf-8")
    ).hexdigest()[:12]
    catalog_entry_id = (
        f"{repo_base}/v3/registration/{pid_lower}/{version_lower}.json#catalogentry"
    )

    published_raw = version_metadata.get("published")
    if isinstance(published_raw, datetime):
        published = format_nuget_timestamp(published_raw)
    elif isinstance(published_raw, str):
        published = published_raw
    else:
        published = format_nuget_timestamp()

    tags_raw = version_metadata.get("tags", [])
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    else:
        tags = list(tags_raw) if tags_raw else []

    dependency_groups = _build_dependency_groups(
        version_metadata.get("dependencies") or version_metadata.get("dependencyGroups")
    )

    entry: dict[str, Any] = {
        "@id": catalog_entry_id,
        "@type": REGISTRATION_TYPE_CATALOG_ENTRY,
        "id": package_id,
        "version": version,
        "authors": version_metadata.get("authors", ""),
        "description": version_metadata.get("description", ""),
        "summary": version_metadata.get("summary", ""),
        "title": version_metadata.get("title", package_id),
        "licenseUrl": version_metadata.get("licenseUrl", ""),
        "projectUrl": version_metadata.get("projectUrl", ""),
        "iconUrl": version_metadata.get("iconUrl", ""),
        "tags": tags,
        "listed": version_metadata.get("listed", True),
        "published": published,
        "dependencyGroups": dependency_groups,
        "packageContent": construct_package_content_url(
            repo_base, package_id, version
        ),
    }
    return entry


def generate_registration_index(
    package_id: str,
    versions: list[dict[str, Any]],
    base_url: str,
    repository_name: str,
) -> bytes:
    """Generate a NuGet V3 registration index for a package.

    The registration index provides all version metadata for a specific
    package ID.  NuGet clients use this to discover available versions,
    dependencies, and download URLs.

    Versions are sorted by SemVer order and paginated into pages of
    :data:`REGISTRATION_PAGE_SIZE` items.

    Args:
        package_id: The package identifier (original casing).
        versions: A list of version metadata dicts (each must contain at least
            a ``"version"`` key).
        base_url: The root server URL.
        repository_name: The repository name.

    Returns:
        JSON-encoded bytes of the registration index document.
    """
    repo_base = construct_base_url(base_url, repository_name)
    pid_lower = package_id.lower()

    # Sort versions by SemVer order
    sorted_versions = sorted(
        versions,
        key=lambda v: _parse_nuget_version(v.get("version", "0.0.0"))[:5]
        if v.get("version") else (0, 0, 0, 0, None),
    )

    # Paginate
    pages: list[dict[str, Any]] = []
    for page_idx in range(0, max(len(sorted_versions), 1), REGISTRATION_PAGE_SIZE):
        page_versions = sorted_versions[page_idx : page_idx + REGISTRATION_PAGE_SIZE]
        if not page_versions:
            continue
        lower_version = page_versions[0].get("version", "0.0.0")
        upper_version = page_versions[-1].get("version", "0.0.0")

        # Build items for this page
        items: list[dict[str, Any]] = []
        for v_meta in page_versions:
            ver = v_meta.get("version", "0.0.0")
            ver_lower = ver.lower()
            leaf_id = f"{repo_base}/v3/registration/{pid_lower}/{ver_lower}.json"
            catalog_entry = _build_catalog_entry(
                package_id, v_meta, base_url, repository_name
            )
            items.append({
                "@id": leaf_id,
                "@type": REGISTRATION_TYPE_PACKAGE,
                "catalogEntry": catalog_entry,
                "packageContent": catalog_entry["packageContent"],
                "registration": f"{repo_base}/v3/registration/{pid_lower}/index.json",
            })

        page_number = page_idx // REGISTRATION_PAGE_SIZE
        page_obj: dict[str, Any] = {
            "@id": f"{repo_base}/v3/registration/{pid_lower}/page/{page_number}.json",
            "@type": "catalog:CatalogPage",
            "count": len(items),
            "lower": lower_version,
            "upper": upper_version,
            "items": items,
        }
        pages.append(page_obj)

    document: dict[str, Any] = {
        "@id": f"{repo_base}/v3/registration/{pid_lower}/index.json",
        "@type": ["catalog:CatalogRoot", "PackageRegistration", "catalog:Permalink"],
        "count": len(pages),
        "items": pages,
    }
    return json.dumps(document, indent=2).encode("utf-8")


def generate_registration_page(
    package_id: str,
    page_versions: list[dict[str, Any]],
    page_index: int,
    base_url: str,
    repository_name: str,
) -> bytes:
    """Generate a single registration page for paginated results.

    Used when there are more versions than :data:`REGISTRATION_PAGE_SIZE`.

    Args:
        package_id: The package identifier (original casing).
        page_versions: Version metadata dicts for this page.
        page_index: Zero-based page index.
        base_url: The root server URL.
        repository_name: The repository name.

    Returns:
        JSON-encoded bytes of the registration page.
    """
    repo_base = construct_base_url(base_url, repository_name)
    pid_lower = package_id.lower()

    items: list[dict[str, Any]] = []
    for v_meta in page_versions:
        ver = v_meta.get("version", "0.0.0")
        ver_lower = ver.lower()
        leaf_id = f"{repo_base}/v3/registration/{pid_lower}/{ver_lower}.json"
        catalog_entry = _build_catalog_entry(
            package_id, v_meta, base_url, repository_name
        )
        items.append({
            "@id": leaf_id,
            "@type": REGISTRATION_TYPE_PACKAGE,
            "catalogEntry": catalog_entry,
            "packageContent": catalog_entry["packageContent"],
            "registration": f"{repo_base}/v3/registration/{pid_lower}/index.json",
        })

    lower_version = page_versions[0].get("version", "0.0.0") if page_versions else ""
    upper_version = page_versions[-1].get("version", "0.0.0") if page_versions else ""

    document: dict[str, Any] = {
        "@id": f"{repo_base}/v3/registration/{pid_lower}/page/{page_index}.json",
        "@type": "catalog:CatalogPage",
        "count": len(items),
        "lower": lower_version,
        "upper": upper_version,
        "items": items,
        "parent": f"{repo_base}/v3/registration/{pid_lower}/index.json",
    }
    return json.dumps(document, indent=2).encode("utf-8")


def generate_registration_leaf(
    package_id: str,
    version_metadata: dict[str, Any],
    base_url: str,
    repository_name: str,
) -> bytes:
    """Generate a registration leaf entry for a single version.

    Contains the full ``catalogEntry`` for one specific version of a package.

    Args:
        package_id: The package identifier (original casing).
        version_metadata: Metadata dict for the version.
        base_url: The root server URL.
        repository_name: The repository name.

    Returns:
        JSON-encoded bytes of the registration leaf.
    """
    repo_base = construct_base_url(base_url, repository_name)
    pid_lower = package_id.lower()
    ver = version_metadata.get("version", "0.0.0")
    ver_lower = ver.lower()
    leaf_id = f"{repo_base}/v3/registration/{pid_lower}/{ver_lower}.json"

    catalog_entry = _build_catalog_entry(
        package_id, version_metadata, base_url, repository_name
    )

    document: dict[str, Any] = {
        "@id": leaf_id,
        "@type": [REGISTRATION_TYPE_PACKAGE, "catalog:Permalink"],
        "listed": version_metadata.get("listed", True),
        "catalogEntry": catalog_entry,
        "packageContent": catalog_entry["packageContent"],
        "published": catalog_entry.get("published", format_nuget_timestamp()),
        "registration": f"{repo_base}/v3/registration/{pid_lower}/index.json",
    }
    return json.dumps(document, indent=2).encode("utf-8")


# ===================================================================
# Search Query Service
# ===================================================================

def generate_search_result_entry(
    package_id: str,
    latest_version: str,
    metadata: dict[str, Any],
    versions: list[dict[str, Any]],
    base_url: str,
    repository_name: str,
) -> dict[str, Any]:
    """Generate a single search result entry for the search response.

    Extracts metadata fields (description, summary, title, tags, authors,
    etc.) and builds a versions array with per-version download counts.

    Args:
        package_id: The package identifier (original casing).
        latest_version: The latest stable version string.
        metadata: Package-level metadata dict.
        versions: List of version info dicts, each with ``version`` and
            optionally ``downloads`` and ``@id`` keys.
        base_url: The root server URL.
        repository_name: The repository name.

    Returns:
        A search result entry dict.
    """
    repo_base = construct_base_url(base_url, repository_name)
    pid_lower = package_id.lower()
    registration_url = f"{repo_base}/v3/registration/{pid_lower}/index.json"

    tags_raw = metadata.get("tags", [])
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    else:
        tags = list(tags_raw) if tags_raw else []

    authors_raw = metadata.get("authors", "")
    if isinstance(authors_raw, str):
        authors = [authors_raw] if authors_raw else []
    elif isinstance(authors_raw, list):
        authors = authors_raw
    else:
        authors = []

    total_downloads = sum(
        v.get("downloads", 0) for v in versions if isinstance(v.get("downloads"), (int, float))
    )

    version_entries: list[dict[str, Any]] = []
    for v in versions:
        ver = v.get("version", "")
        ver_lower = ver.lower()
        v_entry: dict[str, Any] = {
            "version": ver,
            "downloads": v.get("downloads", 0),
            "@id": v.get(
                "@id",
                f"{repo_base}/v3/registration/{pid_lower}/{ver_lower}.json",
            ),
        }
        version_entries.append(v_entry)

    entry: dict[str, Any] = {
        "@id": registration_url,
        "@type": REGISTRATION_TYPE_PACKAGE,
        "registration": registration_url,
        "id": package_id,
        "version": latest_version,
        "description": metadata.get("description", ""),
        "summary": metadata.get("summary", ""),
        "title": metadata.get("title", package_id),
        "iconUrl": metadata.get("iconUrl", ""),
        "licenseUrl": metadata.get("licenseUrl", ""),
        "projectUrl": metadata.get("projectUrl", ""),
        "tags": tags,
        "authors": authors,
        "totalDownloads": total_downloads,
        "verified": metadata.get("verified", False),
        "versions": version_entries,
    }
    return entry


def generate_search_response(
    results: list[dict[str, Any]],
    total_hits: int,
    skip: int = 0,
    take: int = SEARCH_DEFAULT_PAGE_SIZE,
) -> bytes:
    """Generate the search query response.

    Returned at ``GET /v3/query?q=...&skip=...&take=...``.

    The response contains a ``@context``, ``totalHits`` for pagination, and a
    ``data`` array of package search result entries.

    Args:
        results: Pre-built search result entry dicts (typically from
            :func:`generate_search_result_entry`).
        total_hits: Total number of matching packages.
        skip: Pagination offset (for informational purposes only; the caller
            is expected to have already sliced *results*).
        take: Page size (for informational purposes only).

    Returns:
        JSON-encoded bytes of the search response document.
    """
    document: dict[str, Any] = {
        "@context": {"@vocab": "http://schema.nuget.org/schema#"},
        "totalHits": total_hits,
        "data": results,
    }
    return json.dumps(document, indent=2).encode("utf-8")


# ===================================================================
# Search Autocomplete Service
# ===================================================================

def generate_autocomplete_response(
    package_ids: list[str], total_hits: int
) -> bytes:
    """Generate the package ID autocomplete response.

    Returned at ``GET /v3/autocomplete?q=...&skip=...&take=...``.

    Args:
        package_ids: A list of matching package ID strings.
        total_hits: The total number of matching package IDs.

    Returns:
        JSON-encoded bytes with a ``data`` array of package ID strings.
    """
    document: dict[str, Any] = {
        "@context": {"@vocab": "http://schema.nuget.org/schema#"},
        "totalHits": total_hits,
        "data": package_ids,
    }
    return json.dumps(document, indent=2).encode("utf-8")


def generate_version_autocomplete_response(versions: list[str]) -> bytes:
    """Generate the version autocomplete response for a specific package.

    Returned at ``GET /v3/autocomplete?id=PackageName``.

    Args:
        versions: A list of version strings for the specified package.

    Returns:
        JSON-encoded bytes with a ``data`` array of version strings.
    """
    document: dict[str, Any] = {
        "@context": {"@vocab": "http://schema.nuget.org/schema#"},
        "totalHits": len(versions),
        "data": versions,
    }
    return json.dumps(document, indent=2).encode("utf-8")


# ===================================================================
# Catalog Resources (Change Tracking)
# ===================================================================

def generate_catalog_index(
    pages: list[dict[str, Any]], base_url: str, repository_name: str
) -> bytes:
    """Generate the catalog index (root of the catalog resource).

    The catalog is an append-only log of package changes used by clients for
    synchronization.

    Args:
        pages: A list of page descriptor dicts.  Each dict should contain
            ``count`` (number of entries in the page) and optionally
            ``commitTimeStamp``.
        base_url: The root server URL.
        repository_name: The repository name.

    Returns:
        JSON-encoded bytes of the catalog index document.
    """
    repo_base = construct_base_url(base_url, repository_name)
    now_ts = format_nuget_timestamp()

    # Compute a deterministic commit ID from the page data
    commit_data = json.dumps(pages, sort_keys=True, default=str)
    commit_id = hashlib.sha256(commit_data.encode("utf-8")).hexdigest()

    page_items: list[dict[str, Any]] = []
    for idx, page in enumerate(pages):
        page_commit_data = json.dumps(page, sort_keys=True, default=str)
        page_commit_id = hashlib.sha256(page_commit_data.encode("utf-8")).hexdigest()
        page_items.append({
            "@id": f"{repo_base}/v3/catalog/page/{idx}.json",
            "@type": "CatalogPage",
            "commitId": page_commit_id,
            "commitTimeStamp": page.get("commitTimeStamp", now_ts),
            "count": page.get("count", 0),
        })

    document: dict[str, Any] = {
        "@id": f"{repo_base}/v3/catalog/index.json",
        "@type": ["CatalogRoot", "AppendOnlyCatalog", "Permalink"],
        "commitId": commit_id,
        "commitTimeStamp": now_ts,
        "count": len(page_items),
        "items": page_items,
        "@context": {
            "@vocab": NUGET_CATALOG_CONTEXT_URL,
            "nuget": NUGET_CONTEXT_URL,
        },
    }
    return json.dumps(document, indent=2).encode("utf-8")


def generate_catalog_page(
    entries: list[dict[str, Any]],
    page_index: int,
    base_url: str,
    repository_name: str,
) -> bytes:
    """Generate a single catalog page containing change entries.

    Each entry references a catalog leaf (package details or package delete).

    Args:
        entries: A list of catalog entry dicts.  Each should contain at least
            ``package_id``, ``version``, ``action`` (``"PackageDetails"`` or
            ``"PackageDelete"``), and optionally ``commitTimeStamp``.
        page_index: Zero-based page index.
        base_url: The root server URL.
        repository_name: The repository name.

    Returns:
        JSON-encoded bytes of the catalog page document.
    """
    repo_base = construct_base_url(base_url, repository_name)
    now_ts = format_nuget_timestamp()

    commit_data = json.dumps(entries, sort_keys=True, default=str)
    commit_id = hashlib.sha256(commit_data.encode("utf-8")).hexdigest()

    items: list[dict[str, Any]] = []
    for entry in entries:
        pkg_id = entry.get("package_id", entry.get("id", ""))
        ver = entry.get("version", "0.0.0")
        action = entry.get("action", "PackageDetails")
        ts = entry.get("commitTimeStamp", now_ts)

        # Construct timestamp-based data path
        ts_path = ts.replace("-", ".").replace(":", ".").replace("T", ".").split(".")[:-1]
        ts_str = ".".join(ts_path[:6]) if len(ts_path) >= 6 else "0000.00.00.00.00.00"
        leaf_id = (
            f"{repo_base}/v3/catalog/data/{ts_str}/"
            f"{pkg_id.lower()}.{ver.lower()}.json"
        )

        entry_commit_data = f"{pkg_id}:{ver}:{ts}"
        entry_commit_id = hashlib.sha256(
            entry_commit_data.encode("utf-8")
        ).hexdigest()

        catalog_type = (
            CATALOG_TYPE_PACKAGE_DETAILS
            if action == "PackageDetails"
            else CATALOG_TYPE_PACKAGE_DELETE
        )

        items.append({
            "@id": leaf_id,
            "@type": catalog_type,
            "commitId": entry_commit_id,
            "commitTimeStamp": ts,
            "nuget:id": pkg_id,
            "nuget:version": ver,
        })

    document: dict[str, Any] = {
        "@id": f"{repo_base}/v3/catalog/page/{page_index}.json",
        "@type": "CatalogPage",
        "commitId": commit_id,
        "commitTimeStamp": now_ts,
        "count": len(items),
        "parent": f"{repo_base}/v3/catalog/index.json",
        "items": items,
        "@context": {
            "@vocab": NUGET_CATALOG_CONTEXT_URL,
            "nuget": NUGET_CONTEXT_URL,
        },
    }
    return json.dumps(document, indent=2).encode("utf-8")


def generate_catalog_leaf(
    package_id: str,
    version: str,
    metadata: dict[str, Any],
    action: str,
    base_url: str,
    repository_name: str,
) -> bytes:
    """Generate a catalog leaf entry for a single package change.

    A catalog leaf represents a single change event (publish, update, or
    delete) in the append-only catalog.

    Args:
        package_id: The package identifier (original casing).
        version: The package version string.
        metadata: Package metadata dict.
        action: Either ``"PackageDetails"`` (publish/update) or
            ``"PackageDelete"`` (deletion).
        base_url: The root server URL.
        repository_name: The repository name.

    Returns:
        JSON-encoded bytes of the catalog leaf document.
    """
    repo_base = construct_base_url(base_url, repository_name)
    now_ts = format_nuget_timestamp()

    commit_data = f"{package_id}:{version}:{now_ts}"
    commit_id = hashlib.sha256(commit_data.encode("utf-8")).hexdigest()

    ts_path = now_ts.replace("-", ".").replace(":", ".").replace("T", ".").split(".")[:-1]
    ts_str = ".".join(ts_path[:6]) if len(ts_path) >= 6 else "0000.00.00.00.00.00"
    leaf_id = (
        f"{repo_base}/v3/catalog/data/{ts_str}/"
        f"{package_id.lower()}.{version.lower()}.json"
    )

    catalog_type = (
        CATALOG_TYPE_PACKAGE_DETAILS
        if action == "PackageDetails"
        else CATALOG_TYPE_PACKAGE_DELETE
    )

    tags_raw = metadata.get("tags", [])
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    else:
        tags = list(tags_raw) if tags_raw else []

    authors_raw = metadata.get("authors", "")
    if isinstance(authors_raw, str):
        authors = authors_raw
    elif isinstance(authors_raw, list):
        authors = ", ".join(authors_raw)
    else:
        authors = ""

    dependency_groups = _build_dependency_groups(
        metadata.get("dependencies") or metadata.get("dependencyGroups")
    )

    document: dict[str, Any] = {
        "@id": leaf_id,
        "@type": [catalog_type, "catalog:Permalink"],
        "catalog:commitId": commit_id,
        "catalog:commitTimeStamp": now_ts,
        "id": package_id,
        "version": version,
        "authors": authors,
        "description": metadata.get("description", ""),
        "summary": metadata.get("summary", ""),
        "title": metadata.get("title", package_id),
        "licenseUrl": metadata.get("licenseUrl", ""),
        "projectUrl": metadata.get("projectUrl", ""),
        "iconUrl": metadata.get("iconUrl", ""),
        "tags": tags,
        "listed": metadata.get("listed", True),
        "published": now_ts,
        "created": metadata.get("created", now_ts),
        "lastEdited": metadata.get("lastEdited", now_ts),
        "dependencyGroups": dependency_groups,
        "packageContent": construct_package_content_url(
            repo_base, package_id, version
        ),
        "isPrerelease": is_prerelease_version(version),
        "@context": {
            "@vocab": NUGET_CATALOG_CONTEXT_URL,
            "nuget": NUGET_CONTEXT_URL,
            "catalog": "http://schema.nuget.org/catalog#",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
            "dependencies": {
                "@id": "dependency",
                "@container": "@set",
            },
        },
    }
    return json.dumps(document, indent=2).encode("utf-8")


# ===================================================================
# Response Merging for Group Repositories
# ===================================================================

def merge_registration_indexes(indexes: list[bytes]) -> bytes | None:
    """Merge registration indexes from multiple member repositories.

    This is **critical for group repository support**.  When a group
    repository resolves a registration request, it fetches the registration
    index from each ordered member repository and merges the results.

    Merge strategy:
    1. If *indexes* is empty, return *None*.
    2. If only one index is present, return it as-is.
    3. Parse each index as JSON.  Skip unparseable entries (logged as
       warnings).
    4. Union of all version entries across members.  If the same version
       appears in multiple members, the **first** member's entry wins
       (ordered priority).
    5. Re-sort versions by SemVer order.
    6. Re-paginate into pages of :data:`REGISTRATION_PAGE_SIZE`.

    Args:
        indexes: A list of JSON-encoded registration index bytes, one per
            member repository.  Ordered by group member priority
            (highest-priority first).

    Returns:
        Merged JSON-encoded bytes, or *None* if *indexes* is empty.
    """
    if not indexes:
        return None
    if len(indexes) == 1:
        return indexes[0]

    # Collect all version entries, keyed by normalized version string
    merged_versions: dict[str, dict[str, Any]] = {}
    package_id: str = ""
    base_id: str = ""

    for raw in indexes:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "Failed to parse registration index during merge: %s", exc
            )
            continue

        if not package_id:
            # Extract package_id from the first parseable index
            base_id = data.get("@id", "")
            # Try to extract package ID from page items
            for page in data.get("items", []):
                for item in page.get("items", []):
                    entry = item.get("catalogEntry", {})
                    if entry.get("id"):
                        package_id = entry["id"]
                        break
                if package_id:
                    break

        # Extract all version entries from all pages
        for page in data.get("items", []):
            for item in page.get("items", []):
                entry = item.get("catalogEntry", {})
                ver = entry.get("version", "")
                if ver:
                    ver_key = ver.lower()
                    if ver_key not in merged_versions:
                        # First member wins — preserve the full item
                        merged_versions[ver_key] = item

    if not merged_versions:
        return indexes[0]

    # Sort merged versions by SemVer order
    sorted_keys = sort_nuget_versions(list(merged_versions.keys()))
    sorted_items = [merged_versions[k.lower()] for k in sorted_keys]

    # Re-paginate
    pages: list[dict[str, Any]] = []
    for page_idx in range(0, max(len(sorted_items), 1), REGISTRATION_PAGE_SIZE):
        page_items = sorted_items[page_idx : page_idx + REGISTRATION_PAGE_SIZE]
        if not page_items:
            continue

        # Extract lower/upper version from catalog entries
        first_entry = page_items[0].get("catalogEntry", {})
        last_entry = page_items[-1].get("catalogEntry", {})
        lower_ver = first_entry.get("version", "0.0.0")
        upper_ver = last_entry.get("version", "0.0.0")

        page_number = page_idx // REGISTRATION_PAGE_SIZE

        # Build page URL from base_id pattern if available
        page_url = base_id.replace("/index.json", f"/page/{page_number}.json") if base_id else ""

        page_obj: dict[str, Any] = {
            "@id": page_url,
            "@type": "catalog:CatalogPage",
            "count": len(page_items),
            "lower": lower_ver,
            "upper": upper_ver,
            "items": page_items,
        }
        pages.append(page_obj)

    document: dict[str, Any] = {
        "@id": base_id,
        "@type": ["catalog:CatalogRoot", "PackageRegistration", "catalog:Permalink"],
        "count": len(pages),
        "items": pages,
    }
    return json.dumps(document, indent=2).encode("utf-8")


def merge_search_responses(responses: list[bytes]) -> bytes | None:
    """Merge search responses from multiple group repository members.

    Merge strategy:
    * Combine results from all members.
    * Deduplicate by package ID (case-insensitive).  The **first** member's
      entry wins (ordered priority).
    * Recalculate ``totalHits``.

    Args:
        responses: A list of JSON-encoded search response bytes, one per
            member repository.  Ordered by group member priority.

    Returns:
        Merged JSON-encoded bytes, or *None* if *responses* is empty.
    """
    if not responses:
        return None
    if len(responses) == 1:
        return responses[0]

    seen_ids: set[str] = set()
    merged_data: list[dict[str, Any]] = []

    for raw in responses:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "Failed to parse search response during merge: %s", exc
            )
            continue

        for entry in data.get("data", []):
            pkg_id = entry.get("id", "")
            pkg_id_lower = pkg_id.lower()
            if pkg_id_lower and pkg_id_lower not in seen_ids:
                seen_ids.add(pkg_id_lower)
                merged_data.append(entry)

    document: dict[str, Any] = {
        "@context": {"@vocab": "http://schema.nuget.org/schema#"},
        "totalHits": len(merged_data),
        "data": merged_data,
    }
    return json.dumps(document, indent=2).encode("utf-8")
