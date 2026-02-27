"""
npm registry metadata generation and merging utilities.

Generates npm registry-compatible JSON metadata documents:
- Full package document: complete metadata for all versions
- Abbreviated (corgi) document: stripped metadata for faster installs
- Version-specific metadata with tarball URLs and integrity hashes
- Search endpoint integration

Handles npm publish payload processing and dist-tag management.
Generates integrity hashes (sha512 SRI format) for tarballs.

Used by NpmFormatHandler for metadata requests and by group repository
resolution for merging metadata from multiple member repositories.
"""

import base64
import copy
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# npm registry response content type
NPM_JSON_CONTENT_TYPE: str = 'application/json'

# Abbreviated (corgi) document content type
# npm client sends this in Accept header for faster installs
NPM_ABBREVIATED_CONTENT_TYPE: str = 'application/vnd.npm.install-v1+json'

# Default dist-tag
DEFAULT_DIST_TAG: str = 'latest'

# Standard dist-tags recognised by npm
STANDARD_DIST_TAGS: set[str] = {
    'latest', 'next', 'beta', 'alpha', 'canary', 'rc', 'dev', 'experimental',
}

# Integrity hash algorithm (npm uses sha512 by default)
INTEGRITY_ALGORITHM: str = 'sha512'

# Legacy checksum algorithm (npm uses sha1 for backward compatibility)
LEGACY_CHECKSUM_ALGORITHM: str = 'sha1'

# Fields included in abbreviated (corgi) document per version
ABBREVIATED_VERSION_FIELDS: set[str] = {
    'name', 'version', 'dependencies', 'optionalDependencies',
    'devDependencies', 'peerDependencies', 'peerDependenciesMeta',
    'bundleDependencies', 'bundledDependencies', 'bin', 'directories',
    'engines', 'dist', '_hasShrinkwrap', 'hasInstallScript',
    'deprecated', 'os', 'cpu', 'libc',
}

# Fields for full package document per version (all fields preserved)
FULL_VERSION_FIELDS: None = None

# Standard metadata fields at the package level
PACKAGE_LEVEL_FIELDS: set[str] = {
    'name', 'description', 'dist-tags', 'versions', 'time',
    'maintainers', 'author', 'repository', 'readme', 'readmeFilename',
    'homepage', 'keywords', 'bugs', 'license', 'users',
}

# Semver regex for parsing version strings per semver 2.0.0 spec
_SEMVER_RE = re.compile(
    r'^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)'
    r'(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)'
    r'(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?'
    r'(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$'
)

# Fallback pattern for loose prerelease detection
_LOOSE_PRERELEASE_RE = re.compile(r'^\d+\.\d+\.\d+-')

# npm timestamp format (ISO 8601 with milliseconds and Z suffix)
_NPM_TS_FMT = '%Y-%m-%dT%H:%M:%S.%fZ'


# ---------------------------------------------------------------------------
# Utility functions (placed first — used by higher-level generators)
# ---------------------------------------------------------------------------


def is_scoped_package(name: str) -> bool:
    """Return ``True`` if *name* is a scoped npm package (starts with ``@``).

    Examples::

        >>> is_scoped_package('@angular/core')
        True
        >>> is_scoped_package('express')
        False
    """
    return name.startswith('@') and '/' in name


def parse_package_name(name: str) -> tuple[str | None, str]:
    """Parse an npm package name into ``(scope, bare_name)``.

    For scoped packages the scope **includes** the leading ``@``::

        >>> parse_package_name('@myorg/mypackage')
        ('@myorg', 'mypackage')
        >>> parse_package_name('express')
        (None, 'express')

    Returns:
        A 2-tuple ``(scope_or_none, bare_name)``.
    """
    if is_scoped_package(name):
        scope, _, bare = name.partition('/')
        return (scope, bare)
    return (None, name)


def format_timestamp(dt: datetime | None = None) -> str:
    """Format a *datetime* as an npm-compatible ISO 8601 timestamp string.

    If *dt* is ``None`` the current UTC time is used.  The output always ends
    with ``Z`` and includes millisecond precision, e.g.
    ``'2024-06-15T12:00:00.000Z'``.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    # Ensure UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Produce ISO 8601 with milliseconds and trailing 'Z'
    formatted = dt.strftime('%Y-%m-%dT%H:%M:%S')
    ms = dt.microsecond // 1000
    return f'{formatted}.{ms:03d}Z'


def parse_timestamp(timestamp_str: str) -> datetime:
    """Parse an npm timestamp string back to a timezone-aware *datetime*.

    Accepts several ISO 8601 variants commonly emitted by npm clients:
    - ``2024-06-15T12:00:00.000Z``
    - ``2024-06-15T12:00:00Z``
    - ``2024-06-15T12:00:00+00:00``
    - ``2024-06-15T12:00:00``
    """
    ts = timestamp_str.strip()
    # Normalise trailing 'Z' → '+00:00' so fromisoformat can parse it
    if ts.endswith('Z') or ts.endswith('z'):
        ts = ts[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        # Fallback: try strptime with common npm format
        try:
            dt = datetime.strptime(timestamp_str.strip(), _NPM_TS_FMT)
        except ValueError:
            # Last resort: strip fractional seconds
            base = timestamp_str.strip().rstrip('Z').split('.')[0]
            dt = datetime.strptime(base, '%Y-%m-%dT%H:%M:%S')
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def construct_tarball_url(
    base_url: str,
    scope: str | None,
    name: str,
    version: str,
) -> str:
    """Build the full tarball download URL for a package version.

    For scoped packages the URL includes the scope directory segment::

        >>> construct_tarball_url('https://r.example.com/repo', '@myorg', 'pkg', '1.0.0')
        'https://r.example.com/repo/@myorg/pkg/-/pkg-1.0.0.tgz'

    For unscoped packages::

        >>> construct_tarball_url('https://r.example.com/repo', None, 'express', '4.18.2')
        'https://r.example.com/repo/express/-/express-4.18.2.tgz'
    """
    base = base_url.rstrip('/')
    filename = f'{name}-{version}.tgz'
    if scope:
        scope_str = scope if scope.startswith('@') else f'@{scope}'
        return f'{base}/{scope_str}/{name}/-/{filename}'
    return f'{base}/{name}/-/{filename}'


def is_prerelease_version(version: str) -> bool:
    """Return ``True`` if *version* contains a semver pre-release identifier.

    Examples::

        >>> is_prerelease_version('1.0.0-beta.1')
        True
        >>> is_prerelease_version('2.0.0-rc.2')
        True
        >>> is_prerelease_version('1.0.0')
        False
        >>> is_prerelease_version('2.3.4')
        False
    """
    m = _SEMVER_RE.match(version)
    if m:
        return m.group('prerelease') is not None
    # Loose fallback for non-strict versions
    return bool(_LOOSE_PRERELEASE_RE.match(version))


# ---------------------------------------------------------------------------
# Semver parsing and comparison
# ---------------------------------------------------------------------------


def _parse_semver(version: str) -> tuple[int, int, int, list[str | int] | None, list[str] | None]:
    """Parse a semver string into structured components.

    Returns:
        ``(major, minor, patch, prerelease_parts, build_parts)``

        *prerelease_parts* is ``None`` when no pre-release tag is present.
        Each numeric pre-release identifier is converted to ``int`` for
        correct comparison semantics.

    Raises:
        ValueError: If *version* cannot be parsed.
    """
    m = _SEMVER_RE.match(version)
    if not m:
        # Attempt lenient parse: just major.minor.patch
        parts = version.split('-', 1)
        core = parts[0].split('+', 1)[0]
        segments = core.split('.')
        try:
            major = int(segments[0]) if len(segments) > 0 else 0
            minor = int(segments[1]) if len(segments) > 1 else 0
            patch = int(segments[2]) if len(segments) > 2 else 0
        except (ValueError, IndexError):
            raise ValueError(f'Cannot parse semver version: {version!r}')
        pre_str = parts[1].split('+', 1)[0] if len(parts) > 1 else None
        build_str = None
        if '+' in version:
            build_str = version.split('+', 1)[1]
        pre_parts = _split_pre(pre_str) if pre_str else None
        build_parts = build_str.split('.') if build_str else None
        return (major, minor, patch, pre_parts, build_parts)

    major = int(m.group('major'))
    minor = int(m.group('minor'))
    patch = int(m.group('patch'))
    pre_str = m.group('prerelease')
    build_str = m.group('build')

    pre_parts = _split_pre(pre_str) if pre_str else None
    build_parts = build_str.split('.') if build_str else None
    return (major, minor, patch, pre_parts, build_parts)


def _split_pre(pre_str: str) -> list[str | int]:
    """Split a pre-release string into typed identifiers."""
    result: list[str | int] = []
    for part in pre_str.split('.'):
        if part.isdigit():
            result.append(int(part))
        else:
            result.append(part)
    return result


def compare_semver(version_a: str, version_b: str) -> int:
    """Compare two semver version strings.

    Returns a negative integer if *version_a* < *version_b*, zero if they are
    equal, and a positive integer if *version_a* > *version_b*.

    Comparison follows the semver 2.0.0 specification:

    1. Major, minor, patch are compared numerically.
    2. A version with a pre-release tag has *lower* precedence than the
       corresponding release (``1.0.0-alpha < 1.0.0``).
    3. Pre-release identifiers are compared left-to-right; numeric identifiers
       compare by value, alphanumeric by ASCII sort order; numeric has lower
       precedence than alphanumeric.
    4. A shorter set of pre-release identifiers has lower precedence when all
       preceding identifiers are equal.
    5. Build metadata is **ignored** for precedence.
    """
    try:
        a_maj, a_min, a_pat, a_pre, _ = _parse_semver(version_a)
    except ValueError:
        # Unparseable versions sort before valid ones
        return -1 if version_a < version_b else (1 if version_a > version_b else 0)
    try:
        b_maj, b_min, b_pat, b_pre, _ = _parse_semver(version_b)
    except ValueError:
        return 1

    # 1. Compare major.minor.patch
    for a_val, b_val in [(a_maj, b_maj), (a_min, b_min), (a_pat, b_pat)]:
        if a_val != b_val:
            return a_val - b_val

    # 2. Pre-release precedence
    if a_pre is None and b_pre is None:
        return 0
    if a_pre is not None and b_pre is None:
        # Pre-release < release
        return -1
    if a_pre is None and b_pre is not None:
        return 1

    # Both have pre-release: compare identifiers left-to-right
    assert a_pre is not None and b_pre is not None
    for ai, bi in zip(a_pre, b_pre):
        if type(ai) is type(bi):
            if ai < bi:  # type: ignore[operator]
                return -1
            if ai > bi:  # type: ignore[operator]
                return 1
        else:
            # Numeric identifiers always have lower precedence than string
            if isinstance(ai, int):
                return -1
            return 1

    # All compared identifiers equal — shorter list has lower precedence
    return len(a_pre) - len(b_pre)


def sort_npm_versions(versions: list[str]) -> list[str]:
    """Sort npm version strings using semver ordering (ascending).

    The returned list is ordered from oldest (lowest) to newest (highest).
    Pre-release versions sort before their corresponding release, e.g.::

        1.0.0-alpha < 1.0.0-beta < 1.0.0-rc.1 < 1.0.0 < 1.0.1 < 1.1.0
    """
    import functools
    return sorted(versions, key=functools.cmp_to_key(compare_semver))


# ---------------------------------------------------------------------------
# Integrity / checksum helpers
# ---------------------------------------------------------------------------


def generate_integrity_hash(content: bytes, algorithm: str = INTEGRITY_ALGORITHM) -> str:
    """Compute an integrity hash in SRI (Subresource Integrity) format.

    npm uses SHA-512 by default.  The output format is
    ``{algorithm}-{base64_digest}``, for example
    ``'sha512-AbCdEfGhIjKlMnOpQr...=='``.

    Args:
        content: Raw bytes to hash.
        algorithm: Hash algorithm name (default ``sha512``).

    Returns:
        SRI integrity string.
    """
    hash_obj = hashlib.new(algorithm)
    hash_obj.update(content)
    digest = hash_obj.digest()
    b64 = base64.b64encode(digest).decode('ascii')
    return f'{algorithm}-{b64}'


def generate_shasum(content: bytes) -> str:
    """Compute the SHA-1 hex digest for npm's legacy ``shasum`` field.

    Returns:
        40-character lowercase hex string.
    """
    return hashlib.sha1(content).hexdigest()


def verify_integrity(content: bytes, integrity: str) -> bool:
    """Verify *content* against an SRI integrity hash string.

    Args:
        content: Raw bytes to verify.
        integrity: SRI string, e.g. ``'sha512-abc...'``.

    Returns:
        ``True`` if the computed hash matches *integrity*.
    """
    try:
        # SRI format: algorithm-base64digest
        dash_idx = integrity.index('-')
        algorithm = integrity[:dash_idx]
        expected_b64 = integrity[dash_idx + 1:]

        hash_obj = hashlib.new(algorithm)
        hash_obj.update(content)
        actual_b64 = base64.b64encode(hash_obj.digest()).decode('ascii')
        return actual_b64 == expected_b64
    except (ValueError, KeyError) as exc:
        logger.warning('Failed to verify integrity hash: %s', exc)
        return False


# ---------------------------------------------------------------------------
# Version-specific metadata generation
# ---------------------------------------------------------------------------


def generate_version_metadata(
    package_name: str,
    version: str,
    package_json: dict[str, Any],
    tarball_url: str,
    tarball_sha1: str,
    tarball_integrity: str,
    tarball_size: int,
) -> dict[str, Any]:
    """Generate complete metadata for a single package version.

    This is the dict that gets stored in the ``versions`` map of a full
    npm package document.

    Args:
        package_name: Full package name (e.g. ``'@myorg/mypkg'``).
        version: Semver version string.
        package_json: Parsed ``package.json`` from the tarball.
        tarball_url: URL for downloading the tarball.
        tarball_sha1: SHA-1 hex digest (legacy ``shasum``).
        tarball_integrity: SHA-512 SRI hash.
        tarball_size: Tarball size in bytes.

    Returns:
        Complete version metadata dict.
    """
    meta: dict[str, Any] = copy.deepcopy(package_json)

    # Canonical identification fields
    meta['name'] = package_name
    meta['version'] = version
    meta['_id'] = f'{package_name}@{version}'

    # dist information — critical for npm install
    dist: dict[str, Any] = {
        'tarball': tarball_url,
        'shasum': tarball_sha1,
        'integrity': tarball_integrity,
    }
    if tarball_size > 0:
        dist['unpackedSize'] = tarball_size
    meta['dist'] = dist

    # Preserve or default optional metadata fields
    meta.setdefault('_npmVersion', meta.get('_npmVersion', ''))
    meta.setdefault('_nodeVersion', meta.get('_nodeVersion', ''))
    meta.setdefault('_hasShrinkwrap', False)

    return meta


# ---------------------------------------------------------------------------
# dist-tags computation
# ---------------------------------------------------------------------------


def compute_dist_tags(
    versions: list[dict[str, Any]],
    existing_tags: dict[str, str] | None = None,
) -> dict[str, str]:
    """Compute dist-tags from a list of version metadata dicts.

    Algorithm:
    1. Preserve any manually-set tags from *existing_tags*.
    2. Determine the highest non-pre-release version → ``'latest'``.
    3. Determine the highest pre-release version; if it is greater than
       ``'latest'``, tag it as ``'next'``.
    4. If no stable release exists, fall back to the highest version overall.

    Args:
        versions: List of version metadata dicts; each **must** contain a
            ``'version'`` key.
        existing_tags: Previously set tags to preserve.

    Returns:
        Dict mapping tag names to version strings.
    """
    tags: dict[str, str] = dict(existing_tags) if existing_tags else {}

    version_strings = [v.get('version', '') for v in versions if v.get('version')]
    if not version_strings:
        return tags

    sorted_versions = sort_npm_versions(version_strings)

    # Highest stable (non-prerelease) version
    stable_versions = [v for v in sorted_versions if not is_prerelease_version(v)]
    prerelease_versions = [v for v in sorted_versions if is_prerelease_version(v)]

    if stable_versions:
        highest_stable = stable_versions[-1]
        # Only set 'latest' if not already manually overridden
        if 'latest' not in tags:
            tags['latest'] = highest_stable
    elif sorted_versions:
        # No stable versions — use highest overall as latest
        if 'latest' not in tags:
            tags['latest'] = sorted_versions[-1]

    # Set 'next' tag for highest pre-release if it exceeds latest
    if prerelease_versions and 'next' not in tags:
        highest_pre = prerelease_versions[-1]
        latest = tags.get('latest', '')
        if latest and compare_semver(highest_pre, latest) > 0:
            tags['next'] = highest_pre

    return tags


# ---------------------------------------------------------------------------
# Full package document generation
# ---------------------------------------------------------------------------


def generate_package_document(
    package_name: str,
    versions: list[dict[str, Any]],
    dist_tags: dict[str, str] | None = None,
    repository_url: str = '',
    extra_metadata: dict[str, Any] | None = None,
) -> bytes:
    """Generate a full npm package document containing metadata for all versions.

    This is the primary JSON response for ``GET /{package-name}`` requests.
    The resulting document includes every version's metadata, dist-tags, time
    entries, and top-level fields like maintainers, keywords, and readme.

    Args:
        package_name: Full package name (e.g. ``'@myorg/mypkg'``).
        versions: List of per-version metadata dicts.  Each **must** contain at
            least ``'version'`` and ideally ``'dist'`` keys.
        dist_tags: Explicit tag -> version mapping.  If ``None``, tags are
            computed automatically via :func:`compute_dist_tags`.
        repository_url: Base URL used when constructing tarball URLs for
            versions that lack a ``dist.tarball`` entry.
        extra_metadata: Additional top-level fields to merge into the document.

    Returns:
        JSON-encoded bytes of the full package document.
    """
    scope, bare_name = parse_package_name(package_name)

    # Build the versions dict keyed by version string
    versions_map: dict[str, dict[str, Any]] = {}
    time_map: dict[str, str] = {}

    for vdata in versions:
        ver = vdata.get('version')
        if not ver:
            continue
        entry = copy.deepcopy(vdata)
        entry.setdefault('name', package_name)
        entry.setdefault('_id', f'{package_name}@{ver}')

        # Ensure dist.tarball URL exists
        dist = entry.get('dist', {})
        if not dist.get('tarball') and repository_url:
            dist['tarball'] = construct_tarball_url(repository_url, scope, bare_name, ver)
        entry['dist'] = dist

        versions_map[ver] = entry

        # Collect per-version timestamps
        ts = vdata.get('time') or vdata.get('_time')
        if isinstance(ts, str):
            time_map[ver] = ts
        elif isinstance(ts, datetime):
            time_map[ver] = format_timestamp(ts)

    # Compute dist-tags
    effective_tags = dist_tags if dist_tags else compute_dist_tags(versions)

    # Build time dict
    all_ts = list(time_map.values())
    if all_ts:
        time_map['created'] = min(all_ts)
        time_map['modified'] = max(all_ts)
    else:
        now = format_timestamp()
        time_map.setdefault('created', now)
        time_map.setdefault('modified', now)

    # Extract top-level metadata from the "latest" version
    latest_ver = effective_tags.get('latest', '')
    latest_meta = versions_map.get(latest_ver, {})
    if not latest_meta and versions_map:
        # Fallback: use the last version in sorted order
        sorted_keys = sort_npm_versions(list(versions_map.keys()))
        latest_meta = versions_map[sorted_keys[-1]] if sorted_keys else {}

    # Construct the root document
    doc: dict[str, Any] = {
        '_id': package_name,
        '_rev': '1-0',
        'name': package_name,
        'description': latest_meta.get('description', ''),
        'dist-tags': effective_tags,
        'versions': versions_map,
        'time': time_map,
        'maintainers': latest_meta.get('maintainers', []),
        'author': latest_meta.get('author', {}),
        'repository': latest_meta.get('repository', {}),
        'readme': latest_meta.get('readme', ''),
        'readmeFilename': latest_meta.get('readmeFilename', ''),
        'homepage': latest_meta.get('homepage', ''),
        'keywords': latest_meta.get('keywords', []),
        'bugs': latest_meta.get('bugs', {}),
        'license': latest_meta.get('license', ''),
    }

    # Merge extra metadata
    if extra_metadata:
        for key, value in extra_metadata.items():
            doc[key] = value

    logger.debug(
        'Generated full package document for %s with %d version(s)',
        package_name,
        len(versions_map),
    )
    return json.dumps(doc, separators=(',', ':'), sort_keys=False).encode('utf-8')


# ---------------------------------------------------------------------------
# Abbreviated (corgi) document generation
# ---------------------------------------------------------------------------


def generate_abbreviated_document(
    package_name: str,
    versions: list[dict[str, Any]],
    dist_tags: dict[str, str] | None = None,
    repository_url: str = '',
) -> bytes:
    """Generate an abbreviated (corgi) npm package document.

    This stripped-down document is returned when the client sends
    ``Accept: application/vnd.npm.install-v1+json`` and is significantly
    smaller than the full document, resulting in faster installs.

    Args:
        package_name: Full package name.
        versions: List of per-version metadata dicts.
        dist_tags: Explicit tag -> version mapping.
        repository_url: Base URL for tarball URLs.

    Returns:
        JSON-encoded bytes of the abbreviated document.
    """
    scope, bare_name = parse_package_name(package_name)
    effective_tags = dist_tags if dist_tags else compute_dist_tags(versions)

    abbrev_versions: dict[str, dict[str, Any]] = {}
    latest_time: str = ''

    for vdata in versions:
        ver = vdata.get('version')
        if not ver:
            continue

        # Filter to abbreviated fields only
        filtered: dict[str, Any] = {}
        for field in ABBREVIATED_VERSION_FIELDS:
            if field in vdata:
                filtered[field] = vdata[field]

        # Always include name and version
        filtered['name'] = package_name
        filtered['version'] = ver

        # Ensure dist field with tarball URL
        dist = filtered.get('dist', vdata.get('dist', {}))
        if isinstance(dist, dict) and not dist.get('tarball') and repository_url:
            dist = dict(dist)
            dist['tarball'] = construct_tarball_url(repository_url, scope, bare_name, ver)
        filtered['dist'] = dist

        abbrev_versions[ver] = filtered

        # Track most recent timestamp
        ts = vdata.get('time') or vdata.get('_time')
        if isinstance(ts, str) and (not latest_time or ts > latest_time):
            latest_time = ts

    doc: dict[str, Any] = {
        'name': package_name,
        'dist-tags': effective_tags,
        'versions': abbrev_versions,
        'modified': latest_time or format_timestamp(),
    }

    logger.debug(
        'Generated abbreviated document for %s with %d version(s)',
        package_name,
        len(abbrev_versions),
    )
    return json.dumps(doc, separators=(',', ':'), sort_keys=False).encode('utf-8')


# ---------------------------------------------------------------------------
# Package document merging for group repositories
# ---------------------------------------------------------------------------


def merge_package_documents(documents: list[bytes]) -> bytes | None:
    """Merge npm package documents from multiple group repository members.

    Documents are merged with *ordered priority*: earlier documents in the list
    take precedence when the same version exists in multiple members.

    Algorithm:
    1. Empty list -> ``None``
    2. Single document -> returned as-is
    3. Multiple documents: union versions (first-writer-wins), merge dist-tags
       (prefer earlier member), union time entries, take top-level metadata
       from the first member that provides it.

    Args:
        documents: Ordered list of JSON-encoded package document bytes.

    Returns:
        Merged JSON-encoded bytes, or ``None`` if *documents* is empty.
    """
    if not documents:
        return None
    if len(documents) == 1:
        return documents[0]

    parsed: list[dict[str, Any]] = []
    for idx, doc_bytes in enumerate(documents):
        try:
            parsed.append(json.loads(doc_bytes))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(
                'Skipping unparseable npm document at index %d during merge: %s',
                idx,
                exc,
            )

    if not parsed:
        return None
    if len(parsed) == 1:
        return json.dumps(parsed[0], separators=(',', ':'), sort_keys=False).encode('utf-8')

    # Use the first document as the base
    merged = copy.deepcopy(parsed[0])

    merged_versions: dict[str, Any] = merged.get('versions', {})
    merged_time: dict[str, str] = merged.get('time', {})
    merged_tags: dict[str, str] = merged.get('dist-tags', {})

    # Merge subsequent documents
    for doc in parsed[1:]:
        # Versions: add only if version key does not already exist (first-writer-wins)
        for ver_key, ver_data in doc.get('versions', {}).items():
            if ver_key not in merged_versions:
                merged_versions[ver_key] = ver_data

        # Time: union of all timestamp entries (first-writer-wins for conflicts)
        for time_key, time_val in doc.get('time', {}).items():
            if time_key not in merged_time:
                merged_time[time_key] = time_val

        # dist-tags: preserve earlier member tags (first-writer-wins)
        for tag_key, tag_val in doc.get('dist-tags', {}).items():
            if tag_key not in merged_tags:
                merged_tags[tag_key] = tag_val

        # Top-level metadata: fill from later members only if missing
        for field in ('description', 'maintainers', 'author', 'repository',
                      'readme', 'readmeFilename', 'homepage', 'keywords',
                      'bugs', 'license'):
            if not merged.get(field) and doc.get(field):
                merged[field] = doc[field]

    merged['versions'] = merged_versions
    merged['time'] = merged_time
    merged['dist-tags'] = merged_tags

    # Recompute 'created' and 'modified' from merged time entries
    version_times = [v for k, v in merged_time.items() if k not in ('created', 'modified')]
    if version_times:
        merged_time['created'] = min(version_times)
        merged_time['modified'] = max(version_times)

    # Ensure 'latest' dist-tag points to the actual highest stable version
    all_ver_strs = list(merged_versions.keys())
    if all_ver_strs:
        stable = [v for v in all_ver_strs if not is_prerelease_version(v)]
        if stable:
            sorted_stable = sort_npm_versions(stable)
            merged_tags['latest'] = sorted_stable[-1]
        elif 'latest' not in merged_tags:
            merged_tags['latest'] = sort_npm_versions(all_ver_strs)[-1]

    logger.debug(
        'Merged %d npm package documents into %d version(s)',
        len(parsed),
        len(merged_versions),
    )
    return json.dumps(merged, separators=(',', ':'), sort_keys=False).encode('utf-8')


# ---------------------------------------------------------------------------
# Search integration
# ---------------------------------------------------------------------------


def generate_search_result(
    package_name: str,
    version_metadata: dict[str, Any],
    score: float = 1.0,
) -> dict[str, Any]:
    """Generate a search result entry for ``GET /-/v1/search``.

    The returned dict conforms to the npm search response object spec.

    Args:
        package_name: Full package name.
        version_metadata: Metadata dict for the latest/relevant version.
        score: Search relevance score (0.0 - 1.0).

    Returns:
        Search result dict.
    """
    scope, bare_name = parse_package_name(package_name)

    # Extract links
    repo = version_metadata.get('repository', {})
    repo_url = repo.get('url', '') if isinstance(repo, dict) else ''
    bugs = version_metadata.get('bugs', {})
    bugs_url = bugs.get('url', '') if isinstance(bugs, dict) else ''

    # Extract author / publisher / maintainers
    author = version_metadata.get('author', {})
    if isinstance(author, str):
        author = {'name': author}

    maintainers = version_metadata.get('maintainers', [])
    search_maintainers: list[dict[str, str]] = []
    for m in maintainers:
        if isinstance(m, dict):
            search_maintainers.append({
                'username': m.get('name', m.get('username', '')),
                'email': m.get('email', ''),
            })

    publisher: dict[str, str] = {}
    if search_maintainers:
        publisher = search_maintainers[0]
    elif isinstance(author, dict):
        publisher = {
            'username': author.get('name', ''),
            'email': author.get('email', ''),
        }

    # Build the search result object
    result: dict[str, Any] = {
        'package': {
            'name': package_name,
            'scope': scope.lstrip('@') if scope else 'unscoped',
            'version': version_metadata.get('version', ''),
            'description': version_metadata.get('description', ''),
            'keywords': version_metadata.get('keywords', []),
            'date': version_metadata.get('time', format_timestamp()),
            'links': {
                'npm': f'https://www.npmjs.com/package/{package_name}',
                'homepage': version_metadata.get('homepage', ''),
                'repository': repo_url,
                'bugs': bugs_url,
            },
            'author': author if isinstance(author, dict) else {'name': str(author)},
            'publisher': publisher,
            'maintainers': search_maintainers,
        },
        'score': {
            'final': score,
            'detail': {
                'quality': score,
                'popularity': round(score * 0.8, 4),
                'maintenance': score,
            },
        },
        'searchScore': score,
    }
    return result


def generate_search_response(
    results: list[dict[str, Any]],
    total: int,
    from_index: int = 0,
    size: int = 20,
) -> bytes:
    """Generate the full search response envelope for ``GET /-/v1/search``.

    Args:
        results: List of search result dicts from :func:`generate_search_result`.
        total: Total number of matching packages (for pagination).
        from_index: Starting offset (for pagination tracking).
        size: Page size (for pagination tracking).

    Returns:
        JSON-encoded bytes of the search response.
    """
    response: dict[str, Any] = {
        'objects': results,
        'total': total,
        'time': format_timestamp(),
    }
    return json.dumps(response, separators=(',', ':'), sort_keys=False).encode('utf-8')
