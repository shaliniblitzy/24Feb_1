"""
Maven metadata.xml generation and merging utilities.

Generates and merges maven-metadata.xml files at three levels:
- Group level: lists all artifactIds under a groupId
- Artifact level: lists all versions with latest/release designation
- Version level: SNAPSHOT build information with timestamps and buildNumbers

Also generates MD5 and SHA-1 checksum sidecar files alongside metadata.

Used by MavenFormatHandler for metadata requests and by group repository
resolution for merging metadata from multiple member repositories.
"""

import functools
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any
import defusedxml.ElementTree as DefusedET
from xml.etree.ElementTree import Element, SubElement, tostring

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maven metadata XML model version
METADATA_MODEL_VERSION: str = '1.1.0'

# XML namespace (Maven metadata.xml typically has no namespace)
MAVEN_METADATA_NS: str = ''

# SNAPSHOT version suffix
SNAPSHOT_SUFFIX: str = '-SNAPSHOT'

# Timestamped SNAPSHOT pattern: {base}-{yyyyMMdd.HHmmss}-{buildNumber}
TIMESTAMPED_SNAPSHOT_PATTERN: re.Pattern[str] = re.compile(
    r'^(?P<base>.+)-(?P<timestamp>\d{8}\.\d{6})-(?P<build_number>\d+)$'
)

# Timestamp format for SNAPSHOT builds
SNAPSHOT_TIMESTAMP_FORMAT: str = '%Y%m%d.%H%M%S'

# Last updated timestamp format
LAST_UPDATED_FORMAT: str = '%Y%m%d%H%M%S'

# XML declaration
XML_DECLARATION: str = '<?xml version="1.0" encoding="UTF-8"?>\n'

# Maven qualifier ordering for version comparison.
# Lower index means lower precedence.
# An empty string represents a *release* (no qualifier).
_QUALIFIER_ORDER: dict[str, int] = {
    'alpha': 0,
    'a': 0,
    'beta': 1,
    'b': 1,
    'milestone': 2,
    'm': 2,
    'rc': 3,
    'cr': 3,
    'snapshot': 4,
    '': 5,           # release (no qualifier)
    'ga': 5,
    'final': 5,
    'release': 5,
    'sp': 6,
}

# Sentinel rank for release-equivalent qualifiers
_RELEASE_RANK: int = _QUALIFIER_ORDER['']


# ---------------------------------------------------------------------------
# Helper Utilities
# ---------------------------------------------------------------------------


def is_snapshot_version(version: str) -> bool:
    """Return ``True`` if *version* ends with ``-SNAPSHOT`` (case-insensitive).

    Args:
        version: A Maven version string such as ``'1.0-SNAPSHOT'`` or ``'2.3.1'``.

    Returns:
        ``True`` when the version represents a SNAPSHOT build.
    """
    return version.upper().endswith(SNAPSHOT_SUFFIX.upper())


def get_base_version(version: str) -> str:
    """Strip the ``-SNAPSHOT`` suffix from a SNAPSHOT version.

    For non-SNAPSHOT versions the string is returned unchanged.

    Args:
        version: A Maven version string.

    Returns:
        The base version without the SNAPSHOT suffix,
        e.g. ``'1.0-SNAPSHOT'`` → ``'1.0'``.
    """
    if is_snapshot_version(version):
        return version[: -len(SNAPSHOT_SUFFIX)]
    return version


def format_last_updated(dt: datetime | None = None) -> str:
    """Format a *datetime* as a Maven ``lastUpdated`` string.

    The format is ``yyyyMMddHHmmss`` (14 digits, no separators).

    Args:
        dt: An optional datetime.  When ``None`` the current UTC time is used.

    Returns:
        Formatted timestamp, e.g. ``'20240115103000'``.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime(LAST_UPDATED_FORMAT)


def parse_last_updated(last_updated_str: str) -> datetime:
    """Parse a Maven ``lastUpdated`` timestamp string back to a *datetime*.

    Args:
        last_updated_str: A 14-digit timestamp such as ``'20240115103000'``.

    Returns:
        A timezone-aware (UTC) datetime object.
    """
    dt = datetime.strptime(last_updated_str, LAST_UPDATED_FORMAT)
    return dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Internal XML Helpers
# ---------------------------------------------------------------------------


def _indent_xml(elem: Element, level: int = 0) -> None:
    """Add indentation whitespace to an XML element tree for pretty output.

    Recursively sets ``.text`` and ``.tail`` attributes so that the
    serialised XML is human-readable.

    Args:
        elem: The root (or sub-) element to indent.
        level: Current nesting depth (used by recursion).
    """
    indent = '\n' + '  ' * (level + 1)
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent
        for i, child in enumerate(elem):
            _indent_xml(child, level + 1)
            if i < len(elem) - 1:
                if not child.tail or not child.tail.strip():
                    child.tail = indent
            else:
                if not child.tail or not child.tail.strip():
                    child.tail = '\n' + '  ' * level
    if level and (not elem.tail or not elem.tail.strip()):
        elem.tail = '\n' + '  ' * (level - 1)


def _serialize_xml(root: Element) -> bytes:
    """Serialize an XML *Element* to UTF-8 bytes with an XML declaration.

    Pretty-printing is applied via :func:`_indent_xml` before serialization.

    Args:
        root: The root ``Element`` of the XML document.

    Returns:
        UTF-8 encoded bytes including the XML declaration header.
    """
    _indent_xml(root)
    xml_body = tostring(root, encoding='unicode')
    return (XML_DECLARATION + xml_body + '\n').encode('utf-8')


def _find_text(parent: Element, tag: str) -> str | None:
    """Find the text content of a **direct** child element.

    Searches only the immediate children of *parent* for an element with
    the given *tag*.  Also handles the (rare) case where the XML uses a
    namespace prefix.

    .. note::

       This intentionally does **not** perform a descendant search so that
       ``<artifactId>`` nested inside ``<plugin>`` elements in group-level
       metadata is not incorrectly picked up as a top-level field.

    Args:
        parent: The parent element to search within.
        tag: The local tag name (e.g. ``'groupId'``).

    Returns:
        The stripped text content of the first matching child, or ``None``.
    """
    # Direct child lookup (most common — no namespace)
    elem = parent.find(tag)
    if elem is not None and elem.text:
        return elem.text.strip()
    # Fallback: try with namespace if one is configured
    if MAVEN_METADATA_NS:
        elem = parent.find(f'{{{MAVEN_METADATA_NS}}}{tag}')
        if elem is not None and elem.text:
            return elem.text.strip()
    return None


# ---------------------------------------------------------------------------
# Version Sorting Utilities
# ---------------------------------------------------------------------------


def _parse_maven_version(version: str) -> list[Any]:
    """Parse a Maven version string into a list of comparable segments.

    The version string is split on ``'.'`` and ``'-'`` separators.  Numeric
    tokens are converted to ``int`` so that ``2 < 10`` (numeric comparison),
    while string tokens are resolved against the Maven qualifier ordering
    table.

    Tokens that combine a qualifier and a number (e.g. ``'alpha1'``,
    ``'sp2'``, ``'rc3'``) are split into the qualifier part and the
    numeric suffix so that ``alpha1 < alpha2`` and ``sp1 < sp2``.

    Each segment is stored as a tuple whose first element indicates the kind:

    - ``(0, <int>)`` for numeric segments
    - ``(1, <rank>, <lowercase_token>)`` for qualifier segments

    Args:
        version: A Maven version string, e.g. ``'1.2.3-beta-2'``.

    Returns:
        A list of tuples suitable for ordered comparison.
    """
    raw_tokens = re.split(r'[.\-]', version)
    segments: list[Any] = []
    for token in raw_tokens:
        if not token:
            continue
        if token.isdigit():
            segments.append((0, int(token)))
        else:
            lower = token.lower()
            # Check if the token is directly in the qualifier table
            if lower in _QUALIFIER_ORDER:
                segments.append((1, _QUALIFIER_ORDER[lower], lower))
            else:
                # Try to split trailing digits: e.g. 'alpha1' → ('alpha', 1)
                match = re.match(r'^([a-zA-Z]+)(\d+)$', token)
                if match:
                    qual_str = match.group(1).lower()
                    qual_num = int(match.group(2))
                    qual_rank = _QUALIFIER_ORDER.get(qual_str, 3)
                    segments.append((1, qual_rank, qual_str))
                    segments.append((0, qual_num))
                else:
                    # Unknown qualifier — assign default rank like 'rc'
                    qual_rank = _QUALIFIER_ORDER.get(lower, 3)
                    segments.append((1, qual_rank, lower))
    return segments


def _compare_segments(seg_a: list[Any], seg_b: list[Any]) -> int:
    """Compare two parsed Maven version segment lists.

    Shorter lists are right-padded with the *release* qualifier so that
    ``1.0`` compares correctly against ``1.0-alpha``.

    Returns:
        Negative if *seg_a* < *seg_b*, zero if equal, positive otherwise.
    """
    max_len = max(len(seg_a), len(seg_b))
    release_pad = (1, _RELEASE_RANK, '')

    for i in range(max_len):
        a = seg_a[i] if i < len(seg_a) else release_pad
        b = seg_b[i] if i < len(seg_b) else release_pad

        a_kind = a[0]
        b_kind = b[0]

        if a_kind == 0 and b_kind == 0:
            # Both numeric
            if a[1] != b[1]:
                return -1 if a[1] < b[1] else 1
        elif a_kind == 1 and b_kind == 1:
            # Both qualifiers – compare rank first, then lexicographic
            if a[1] != b[1]:
                return -1 if a[1] < b[1] else 1
            if a[2] != b[2]:
                return -1 if a[2] < b[2] else 1
        elif a_kind == 0 and b_kind == 1:
            # Numeric vs qualifier
            if b[1] >= _RELEASE_RANK:
                return 1 if a[1] > 0 else (-1 if b[1] > _RELEASE_RANK else 0)
            return 1
        else:
            # Qualifier vs numeric (mirror)
            if a[1] >= _RELEASE_RANK:
                return -1 if b[1] > 0 else (1 if a[1] > _RELEASE_RANK else 0)
            return -1

    return 0


def compare_maven_versions(version_a: str, version_b: str) -> int:
    """Compare two Maven version strings following Maven version ordering.

    Maven version ordering is **not** simple string comparison.  Numeric
    segments are compared numerically, and well-known qualifiers (``alpha``,
    ``beta``, ``rc``, ``snapshot``, ``sp``, …) have a defined precedence.
    SNAPSHOT versions sort *lower* than the equivalent release.

    Args:
        version_a: First version string.
        version_b: Second version string.

    Returns:
        Negative integer if *version_a* < *version_b*, ``0`` if equal,
        positive integer if *version_a* > *version_b*.
    """
    seg_a = _parse_maven_version(version_a)
    seg_b = _parse_maven_version(version_b)
    return _compare_segments(seg_a, seg_b)


def sort_maven_versions(versions: list[str]) -> list[str]:
    """Sort a list of Maven version strings using Maven comparison rules.

    The returned list is in **ascending** order (oldest first, newest last).

    Args:
        versions: List of version strings to sort.

    Returns:
        A new sorted list.

    Example::

        >>> sort_maven_versions(['1.1', '1.0-alpha', '1.0', '1.0-SNAPSHOT'])
        ['1.0-alpha', '1.0-SNAPSHOT', '1.0', '1.1']
    """
    sorted_versions = sorted(
        versions,
        key=functools.cmp_to_key(compare_maven_versions),
    )
    logger.debug('Sorted %d Maven versions', len(sorted_versions))
    return sorted_versions


# ---------------------------------------------------------------------------
# Metadata Parsing
# ---------------------------------------------------------------------------


def detect_metadata_level(xml_bytes: bytes) -> str:
    """Determine the metadata level from raw XML bytes.

    The level is one of:

    - ``'snapshot'`` — version-level metadata for a SNAPSHOT version
    - ``'artifact'`` — artifact-level metadata listing all versions
    - ``'group'`` — group-level metadata listing artifacts (plugins)

    Args:
        xml_bytes: Raw maven-metadata.xml content.

    Returns:
        One of ``'group'``, ``'artifact'``, or ``'snapshot'``.

    Raises:
        xml.etree.ElementTree.ParseError: If the XML is malformed.
    """
    root = DefusedET.fromstring(xml_bytes)
    version_text = _find_text(root, 'version')
    artifact_id_text = _find_text(root, 'artifactId')

    if version_text and is_snapshot_version(version_text):
        return 'snapshot'
    if artifact_id_text is not None:
        return 'artifact'
    return 'group'


def parse_metadata_xml(xml_bytes: bytes) -> dict[str, Any]:
    """Parse a ``maven-metadata.xml`` into a structured dictionary.

    The returned dictionary contains all relevant fields regardless of the
    metadata level; fields that are not applicable are set to ``None`` or
    empty lists.

    Args:
        xml_bytes: Raw XML bytes of a maven-metadata.xml file.

    Returns:
        A dictionary with keys:

        - ``group_id`` (str)
        - ``artifact_id`` (str | None)
        - ``version`` (str | None)
        - ``latest`` (str | None)
        - ``release`` (str | None)
        - ``versions`` (list[str])
        - ``last_updated`` (str | None)
        - ``snapshot_timestamp`` (str | None)
        - ``snapshot_build_number`` (int | None)
        - ``snapshot_versions`` (list[dict])
        - ``level`` ('group' | 'artifact' | 'snapshot')
    """
    root = DefusedET.fromstring(xml_bytes)

    group_id: str = _find_text(root, 'groupId') or ''
    artifact_id: str | None = _find_text(root, 'artifactId')
    version: str | None = _find_text(root, 'version')

    latest: str | None = None
    release: str | None = None
    versions: list[str] = []
    last_updated: str | None = None
    snapshot_timestamp: str | None = None
    snapshot_build_number: int | None = None
    snapshot_versions: list[dict[str, Any]] = []

    versioning = root.find('versioning')
    if versioning is not None:
        latest = _find_text(versioning, 'latest')
        release = _find_text(versioning, 'release')
        last_updated = _find_text(versioning, 'lastUpdated')

        versions_elem = versioning.find('versions')
        if versions_elem is not None:
            for ver_elem in versions_elem.findall('version'):
                if ver_elem.text:
                    versions.append(ver_elem.text.strip())

        snapshot_elem = versioning.find('snapshot')
        if snapshot_elem is not None:
            ts_text = _find_text(snapshot_elem, 'timestamp')
            bn_text = _find_text(snapshot_elem, 'buildNumber')
            if ts_text:
                snapshot_timestamp = ts_text
            if bn_text:
                try:
                    snapshot_build_number = int(bn_text)
                except ValueError:
                    snapshot_build_number = None

        sv_container = versioning.find('snapshotVersions')
        if sv_container is not None:
            for sv_elem in sv_container.findall('snapshotVersion'):
                entry: dict[str, Any] = {}
                classifier_val = _find_text(sv_elem, 'classifier')
                extension_val = _find_text(sv_elem, 'extension')
                value_val = _find_text(sv_elem, 'value')
                updated_val = _find_text(sv_elem, 'updated')
                if classifier_val:
                    entry['classifier'] = classifier_val
                if extension_val:
                    entry['extension'] = extension_val
                if value_val:
                    entry['value'] = value_val
                if updated_val:
                    entry['updated'] = updated_val
                snapshot_versions.append(entry)

    # Determine level
    if version and is_snapshot_version(version):
        level = 'snapshot'
    elif artifact_id is not None:
        level = 'artifact'
    else:
        level = 'group'

    return {
        'group_id': group_id,
        'artifact_id': artifact_id,
        'version': version,
        'latest': latest,
        'release': release,
        'versions': versions,
        'last_updated': last_updated,
        'snapshot_timestamp': snapshot_timestamp,
        'snapshot_build_number': snapshot_build_number,
        'snapshot_versions': snapshot_versions,
        'level': level,
    }


# ---------------------------------------------------------------------------
# Metadata Generation — Group Level
# ---------------------------------------------------------------------------


def generate_group_metadata(group_id: str, artifact_ids: list[str]) -> bytes:
    """Generate group-level ``maven-metadata.xml``.

    Group-level metadata lists all artifact IDs (as Maven plugins) under a
    given *group_id*.  This is the least common metadata level — primarily
    used by Maven for plugin resolution.

    Args:
        group_id: The Maven groupId (dot-notation),
            e.g. ``'org.apache.commons'``.
        artifact_ids: Artifact IDs to include (sorted alphabetically in
            output).

    Returns:
        UTF-8 encoded bytes of the generated XML document.
    """
    root = Element('metadata')
    SubElement(root, 'groupId').text = group_id

    plugins_elem = SubElement(root, 'plugins')
    for aid in sorted(artifact_ids):
        plugin = SubElement(plugins_elem, 'plugin')
        SubElement(plugin, 'name').text = aid
        SubElement(plugin, 'prefix').text = aid
        SubElement(plugin, 'artifactId').text = aid

    logger.debug(
        'Generated group-level metadata for %s with %d artifact(s)',
        group_id,
        len(artifact_ids),
    )
    return _serialize_xml(root)


# ---------------------------------------------------------------------------
# Metadata Generation — Artifact Level
# ---------------------------------------------------------------------------


def generate_artifact_metadata(
    group_id: str,
    artifact_id: str,
    versions: list[str],
    latest: str | None = None,
    release: str | None = None,
    last_updated: str | None = None,
) -> bytes:
    """Generate artifact-level ``maven-metadata.xml``.

    This is the most commonly requested metadata level.  It lists all
    available versions for a given *group_id* / *artifact_id* pair together
    with ``<latest>`` and ``<release>`` pointers.

    Args:
        group_id: Maven groupId.
        artifact_id: Maven artifactId.
        versions: List of version strings to include.
        latest: Explicit latest version.  Auto-computed when ``None``.
        release: Explicit latest release (non-SNAPSHOT) version.
            Auto-computed when ``None``.
        last_updated: Timestamp (``yyyyMMddHHmmss``).  Defaults to now (UTC).

    Returns:
        UTF-8 encoded bytes of the generated XML document.
    """
    sorted_versions = sort_maven_versions(versions) if versions else []

    # Auto-compute latest (highest version overall)
    if latest is None and sorted_versions:
        latest = sorted_versions[-1]

    # Auto-compute release (highest non-SNAPSHOT version)
    if release is None and sorted_versions:
        non_snapshot = [v for v in sorted_versions if not is_snapshot_version(v)]
        if non_snapshot:
            release = non_snapshot[-1]

    if last_updated is None:
        last_updated = format_last_updated()

    root = Element('metadata', attrib={'modelVersion': METADATA_MODEL_VERSION})
    SubElement(root, 'groupId').text = group_id
    SubElement(root, 'artifactId').text = artifact_id

    versioning = SubElement(root, 'versioning')
    if latest:
        SubElement(versioning, 'latest').text = latest
    if release:
        SubElement(versioning, 'release').text = release

    versions_elem = SubElement(versioning, 'versions')
    for ver in sorted_versions:
        SubElement(versions_elem, 'version').text = ver

    SubElement(versioning, 'lastUpdated').text = last_updated

    logger.debug(
        'Generated artifact-level metadata for %s:%s (%d version(s))',
        group_id,
        artifact_id,
        len(sorted_versions),
    )
    return _serialize_xml(root)


# ---------------------------------------------------------------------------
# Metadata Generation — SNAPSHOT Version Level
# ---------------------------------------------------------------------------


def generate_snapshot_metadata(
    group_id: str,
    artifact_id: str,
    version: str,
    snapshot_builds: list[dict[str, Any]] | None = None,
    last_updated: str | None = None,
) -> bytes:
    """Generate version-level SNAPSHOT ``maven-metadata.xml``.

    This metadata level describes the timestamped snapshot builds for a
    particular SNAPSHOT version.  It is only applicable when *version* ends
    with ``-SNAPSHOT``.

    Args:
        group_id: Maven groupId.
        artifact_id: Maven artifactId.
        version: The SNAPSHOT version, e.g. ``'1.0-SNAPSHOT'``.
        snapshot_builds: Optional list of build dicts with keys
            ``'timestamp'``, ``'build_number'``, ``'extension'``, and
            optionally ``'classifier'``.
        last_updated: Optional timestamp.  Defaults to current UTC time.

    Returns:
        UTF-8 encoded bytes of the generated XML document.
    """
    if last_updated is None:
        last_updated = format_last_updated()

    root = Element('metadata', attrib={'modelVersion': METADATA_MODEL_VERSION})
    SubElement(root, 'groupId').text = group_id
    SubElement(root, 'artifactId').text = artifact_id
    SubElement(root, 'version').text = version

    versioning = SubElement(root, 'versioning')
    base_ver = get_base_version(version)

    # Determine the latest snapshot build info
    if snapshot_builds:
        latest_build = max(
            snapshot_builds,
            key=lambda b: int(b.get('build_number', 0)),
        )
        snap_timestamp = str(latest_build.get('timestamp', ''))
        snap_build_number = str(latest_build.get('build_number', 1))
    else:
        now = datetime.now(timezone.utc)
        snap_timestamp = now.strftime(SNAPSHOT_TIMESTAMP_FORMAT)
        snap_build_number = '1'

    snapshot_elem = SubElement(versioning, 'snapshot')
    SubElement(snapshot_elem, 'timestamp').text = snap_timestamp
    SubElement(snapshot_elem, 'buildNumber').text = snap_build_number

    SubElement(versioning, 'lastUpdated').text = last_updated

    if snapshot_builds:
        sv_container = SubElement(versioning, 'snapshotVersions')
        for build in snapshot_builds:
            sv = SubElement(sv_container, 'snapshotVersion')
            classifier = build.get('classifier')
            if classifier:
                SubElement(sv, 'classifier').text = str(classifier)
            extension = build.get('extension', 'jar')
            SubElement(sv, 'extension').text = str(extension)

            ts = str(build.get('timestamp', snap_timestamp))
            bn = str(build.get('build_number', snap_build_number))
            value = f'{base_ver}-{ts}-{bn}'
            SubElement(sv, 'value').text = value

            updated = str(build.get('updated', last_updated))
            SubElement(sv, 'updated').text = updated

    logger.debug(
        'Generated SNAPSHOT metadata for %s:%s:%s (%d build(s))',
        group_id,
        artifact_id,
        version,
        len(snapshot_builds) if snapshot_builds else 0,
    )
    return _serialize_xml(root)


# ---------------------------------------------------------------------------
# Metadata Merging for Group Repositories
# ---------------------------------------------------------------------------


def merge_metadata(metadata_list: list[bytes]) -> bytes | None:
    """Merge ``maven-metadata.xml`` documents from multiple repositories.

    This is **critical for group repository support**.  When a group
    repository aggregates metadata from its ordered members this function
    unions version lists, resolves the latest/release pointers, and picks
    the most recent ``lastUpdated`` timestamp.

    Merge strategy is determined by the metadata level detected from the
    first parseable entry:

    - *artifact*: union of all version lists, re-sorted.
    - *snapshot*: the member with the latest timestamp / build number wins.
    - *group*: union of all artifact ID lists.

    Args:
        metadata_list: Raw ``maven-metadata.xml`` byte strings from
            different member repositories.

    Returns:
        Merged XML as bytes, or ``None`` if the input is empty.
    """
    if not metadata_list:
        return None

    if len(metadata_list) == 1:
        return metadata_list[0]

    # Parse all entries, skipping malformed XML gracefully
    parsed_items: list[dict[str, Any]] = []
    raw_indices: list[int] = []
    for idx, raw_xml in enumerate(metadata_list):
        try:
            parsed = parse_metadata_xml(raw_xml)
            parsed_items.append(parsed)
            raw_indices.append(idx)
        except DefusedET.ParseError:
            logger.warning(
                'Skipping unparseable maven-metadata.xml at index %d during merge',
                idx,
            )

    if not parsed_items:
        logger.warning('No parseable metadata found during merge')
        return None

    level = parsed_items[0]['level']

    if level == 'artifact':
        return _merge_artifact_metadata(parsed_items)
    if level == 'snapshot':
        return _merge_snapshot_metadata(parsed_items, metadata_list, raw_indices)
    if level == 'group':
        return _merge_group_metadata(parsed_items)

    logger.warning(
        'Unknown metadata level %r during merge — returning first entry', level,
    )
    return metadata_list[0]


def _merge_artifact_metadata(items: list[dict[str, Any]]) -> bytes:
    """Merge artifact-level metadata: union of version lists, re-sorted."""
    group_id = items[0]['group_id']
    artifact_id = items[0]['artifact_id'] or ''

    all_versions: set[str] = set()
    latest_timestamp: str = ''
    for item in items:
        all_versions.update(item.get('versions', []))
        ts = item.get('last_updated') or ''
        if ts > latest_timestamp:
            latest_timestamp = ts

    sorted_versions = sort_maven_versions(list(all_versions))
    latest = sorted_versions[-1] if sorted_versions else None
    non_snapshot = [v for v in sorted_versions if not is_snapshot_version(v)]
    release = non_snapshot[-1] if non_snapshot else None

    return generate_artifact_metadata(
        group_id=group_id,
        artifact_id=artifact_id,
        versions=sorted_versions,
        latest=latest,
        release=release,
        last_updated=latest_timestamp or None,
    )


def _merge_snapshot_metadata(
    items: list[dict[str, Any]],
    raw_list: list[bytes],
    raw_indices: list[int],
) -> bytes:
    """Merge SNAPSHOT metadata: latest timestamp / build number wins."""
    best_idx = 0
    best_ts: str = ''
    best_bn: int = 0
    for idx, item in enumerate(items):
        ts = item.get('snapshot_timestamp') or ''
        bn = item.get('snapshot_build_number') or 0
        if (ts, bn) > (best_ts, best_bn):
            best_ts = ts
            best_bn = bn
            best_idx = idx

    # Return the raw XML from the member with the latest build
    return raw_list[raw_indices[best_idx]]


def _merge_group_metadata(items: list[dict[str, Any]]) -> bytes:
    """Merge group-level metadata: union of artifact ID lists."""
    group_id = items[0]['group_id']
    all_artifact_ids: set[str] = set()
    for item in items:
        aid = item.get('artifact_id')
        if aid:
            all_artifact_ids.add(aid)
    return generate_group_metadata(group_id, sorted(all_artifact_ids))


# ---------------------------------------------------------------------------
# Checksum Sidecar Generation
# ---------------------------------------------------------------------------


def generate_checksum_content(content: bytes, algorithm: str) -> bytes:
    """Compute a cryptographic hash of *content* and return the hex digest.

    Used to generate Maven checksum sidecar files (``.md5``, ``.sha1``,
    ``.sha256``, ``.sha512``).

    Args:
        content: The bytes to hash.
        algorithm: Hash algorithm name — ``'md5'``, ``'sha1'``, ``'sha256'``,
            or ``'sha512'``.

    Returns:
        The hex digest encoded as UTF-8 bytes.

    Raises:
        ValueError: If *algorithm* is not supported by :mod:`hashlib`.
    """
    h = hashlib.new(algorithm)
    h.update(content)
    return h.hexdigest().encode('utf-8')


def generate_metadata_checksums(metadata_xml: bytes) -> dict[str, bytes]:
    """Generate MD5 and SHA-1 checksums for a metadata XML file.

    These checksums are written as companion sidecar files alongside
    ``maven-metadata.xml`` (``maven-metadata.xml.md5`` and
    ``maven-metadata.xml.sha1``).

    Args:
        metadata_xml: The raw XML bytes of a maven-metadata.xml file.

    Returns:
        Dictionary mapping ``'md5'`` and ``'sha1'`` to their respective
        hex digest bytes.
    """
    return {
        'md5': generate_checksum_content(metadata_xml, 'md5'),
        'sha1': generate_checksum_content(metadata_xml, 'sha1'),
    }
