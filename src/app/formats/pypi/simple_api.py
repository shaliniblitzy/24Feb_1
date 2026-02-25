"""
PEP 503 Simple Repository API implementation for PyPI format.

Implements the Simple Repository API endpoints:
- Root index page (/simple/) listing all packages
- Per-package page (/simple/{package}/) listing all files/versions
- Package name normalization per PEP 503
- data-requires-python and data-dist-info-metadata attributes on links
- Hash fragment (#sha256=...) on download links for integrity

Also handles the PyPI legacy upload API (POST to /) with multipart
form data per the legacy upload protocol used by twine and setuptools.

Generates text/html responses for Simple API pages.
Used by PypiFormatHandler for metadata requests and group merge.
"""

import hashlib
import html
import logging
import re
from typing import Any
from urllib.parse import quote, urljoin


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — PEP 503 Simple Repository API
# ---------------------------------------------------------------------------

# Content type for Simple API HTML responses
SIMPLE_HTML_CONTENT_TYPE: str = "text/html; charset=utf-8"

# PEP 503 package name normalization regex — replaces runs of [-_.] with '-'
PEP503_NORMALIZE_RE: re.Pattern[str] = re.compile(r"[-_.]+")

# Default hash algorithm for download link fragments.
# PEP 503 mandates at least one hash; SHA-256 is the standard choice.
HASH_ALGORITHM: str = "sha256"

# Set of hash algorithms accepted in download link fragments
SUPPORTED_HASH_ALGORITHMS: set[str] = {"md5", "sha256", "sha384", "sha512"}

# HTML template for the Simple API root index page (/simple/).
# The ``{links}`` placeholder is filled with ``<a>`` tags, one per package.
SIMPLE_INDEX_TEMPLATE: str = (
    "<!DOCTYPE html>\n"
    "<html>\n"
    '<head><meta name="pypi:repository-version" content="1.0">'
    "<title>Simple index</title></head>\n"
    "<body>\n"
    "{links}\n"
    "</body>\n"
    "</html>"
)

# HTML template for the per-package detail page (/simple/{package}/).
# ``{package_name}`` and ``{links}`` are filled at render time.
PACKAGE_PAGE_TEMPLATE: str = (
    "<!DOCTYPE html>\n"
    "<html>\n"
    '<head><meta name="pypi:repository-version" content="1.0">'
    "<title>Links for {package_name}</title></head>\n"
    "<body>\n"
    "<h1>Links for {package_name}</h1>\n"
    "{links}\n"
    "</body>\n"
    "</html>"
)

# Valid ``:action`` values in the legacy upload protocol
VALID_UPLOAD_ACTIONS: set[str] = {"file_upload", "submit"}

# Maximum number of packages rendered on the root index page
MAX_INDEX_PACKAGES: int = 100_000


# ---------------------------------------------------------------------------
# Internal Constants — Legacy Upload Form Field Names
# ---------------------------------------------------------------------------

_UPLOAD_FIELD_ACTION: str = ":action"
_UPLOAD_FIELD_FILE: str = "content"
_UPLOAD_FIELD_NAME: str = "name"
_UPLOAD_FIELD_VERSION: str = "version"
_UPLOAD_FIELD_FILETYPE: str = "filetype"
_UPLOAD_FIELD_PYVERSION: str = "pyversion"
_UPLOAD_FIELD_MD5_DIGEST: str = "md5_digest"
_UPLOAD_FIELD_SHA256_DIGEST: str = "sha256_digest"
_UPLOAD_FIELD_METADATA_VERSION: str = "metadata_version"
_UPLOAD_FIELD_SUMMARY: str = "summary"
_UPLOAD_FIELD_AUTHOR: str = "author"
_UPLOAD_FIELD_AUTHOR_EMAIL: str = "author_email"
_UPLOAD_FIELD_LICENSE: str = "license"
_UPLOAD_FIELD_REQUIRES_PYTHON: str = "requires_python"
_UPLOAD_FIELD_CLASSIFIERS: str = "classifiers"
_UPLOAD_FIELD_REQUIRES_DIST: str = "requires_dist"

# Recognised package file extensions for upload validation
_VALID_FILE_EXTENSIONS: tuple[str, ...] = (
    ".whl",
    ".tar.gz",
    ".tar.bz2",
    ".zip",
    ".egg",
)


# ---------------------------------------------------------------------------
# Internal Compiled Regex Patterns
# ---------------------------------------------------------------------------

# Matches ``<a ...>text</a>`` for HTML parsing (group 1 = attrs, group 2 = text)
_ANCHOR_RE: re.Pattern[str] = re.compile(
    r"<a\s+([^>]*)>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)

# Matches ``attribute="value"`` pairs inside an HTML tag
_ATTR_RE: re.Pattern[str] = re.compile(
    r'([a-zA-Z][a-zA-Z0-9_:-]*)="([^"]*)"',
)

# Validates PyPI package names (start/end with alphanum, middle allows -._)
_VALID_PACKAGE_NAME_RE: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$",
)

# Parses hash fragments like ``sha256=abcdef0123456789...``
_HASH_FRAGMENT_RE: re.Pattern[str] = re.compile(
    r"^([a-zA-Z][a-zA-Z0-9_-]*)=([a-fA-F0-9]+)$",
)


# =========================================================================
# PEP 503 Package Name Normalization
# =========================================================================

def normalize_name(name: str) -> str:
    """Normalize a PyPI package name per PEP 503.

    Replaces all consecutive runs of ``[-_.]`` with a single hyphen and
    converts to lowercase.  This is THE canonical normalization used for all
    package name lookups, comparisons, and URL generation.

    Examples::

        >>> normalize_name('Flask')
        'flask'
        >>> normalize_name('my_package')
        'my-package'
        >>> normalize_name('My.Package.Name')
        'my-package-name'
        >>> normalize_name('SQLAlchemy')
        'sqlalchemy'
        >>> normalize_name('Jinja2')
        'jinja2'

    Args:
        name: Raw package name (e.g. from upload form or user input).

    Returns:
        PEP 503 normalized name suitable for URLs and lookups.
    """
    return PEP503_NORMALIZE_RE.sub("-", name).lower()


# =========================================================================
# Hash Computation
# =========================================================================

def compute_file_hash(content: bytes, algorithm: str = HASH_ALGORITHM) -> str:
    """Compute the hex digest of *content* using the specified hash algorithm.

    Used to generate the mandatory hash fragment (``#sha256=...``) on PEP 503
    download links so that ``pip`` can verify package integrity.

    Args:
        content: Raw bytes of the file to hash.
        algorithm: Hash algorithm name (default ``'sha256'``).  Must be one of
            :data:`SUPPORTED_HASH_ALGORITHMS`.

    Returns:
        Lower-case hex-encoded digest string.

    Raises:
        ValueError: If *algorithm* is not in :data:`SUPPORTED_HASH_ALGORITHMS`.
    """
    if algorithm not in SUPPORTED_HASH_ALGORITHMS:
        raise ValueError(
            f"Unsupported hash algorithm '{algorithm}'. "
            f"Supported: {sorted(SUPPORTED_HASH_ALGORITHMS)}"
        )
    return hashlib.new(algorithm, content).hexdigest()


# =========================================================================
# URL and Fragment Utilities
# =========================================================================

def build_download_url(base_url: str, filename: str) -> str:
    """Construct a download URL for a package file.

    Joins *base_url* with *filename*, ensuring proper URL encoding of
    special characters in the filename.

    Args:
        base_url: Base URL prefix (e.g. ``'/packages/'``).  A trailing ``/``
            is appended if absent so that :func:`urllib.parse.urljoin`
            resolves the filename correctly.
        filename: Package filename (e.g. ``'Flask-3.1.3.tar.gz'``).

    Returns:
        Fully constructed download URL string.
    """
    if not base_url.endswith("/"):
        base_url += "/"
    encoded_filename = quote(filename, safe="@:!$&'()*+,;=")
    return urljoin(base_url, encoded_filename)


def format_hash_fragment(algorithm: str, hex_digest: str) -> str:
    """Format a hash as a URL fragment for inclusion in download links.

    Args:
        algorithm: Hash algorithm name (e.g. ``'sha256'``).
        hex_digest: Hex-encoded digest string.

    Returns:
        Fragment string including the leading ``#``,
        e.g. ``'#sha256=abc123...'``.
    """
    return f"#{algorithm}={hex_digest}"


def parse_hash_fragment(fragment: str) -> tuple[str, str] | None:
    """Parse a URL hash fragment to extract algorithm and digest.

    Handles fragments with or without the leading ``#`` character.

    Args:
        fragment: Hash fragment string, e.g. ``'sha256=abc123...'`` or
            ``'#sha256=abc123...'``.

    Returns:
        Tuple of ``(algorithm, hex_digest)`` — both lower-cased — or
        ``None`` if the fragment cannot be parsed.
    """
    text = fragment.lstrip("#")
    match = _HASH_FRAGMENT_RE.match(text)
    if match is None:
        return None
    algo = match.group(1).lower()
    digest = match.group(2).lower()
    return (algo, digest)


# =========================================================================
# Package Name Validation
# =========================================================================

def is_valid_package_name(name: str) -> bool:
    """Validate a PyPI package name.

    A valid name must:

    * Be non-empty.
    * Start with a letter or digit.
    * End with a letter or digit.
    * Contain only ASCII letters, digits, hyphens, underscores, and periods.

    Args:
        name: Package name to validate.

    Returns:
        ``True`` if the name is valid, ``False`` otherwise.
    """
    if not name:
        return False
    return _VALID_PACKAGE_NAME_RE.match(name) is not None


# =========================================================================
# File Link Generation
# =========================================================================

def generate_file_link(
    filename: str,
    url: str,
    sha256: str,
    requires_python: str | None = None,
    dist_info_metadata: bool = False,
    gpg_sig: bool = False,
    yanked: str | None = None,
) -> str:
    """Generate a single ``<a>`` tag for a package file on the per-package page.

    Produces an anchor element with the mandatory hash fragment and optional
    PEP 503 / PEP 658 / PEP 592 data attributes as consumed by ``pip``.

    Args:
        filename: Display name of the file (used as link text).
        url: Download URL for the file (without hash fragment).
        sha256: SHA-256 hex digest for the hash fragment.
        requires_python: Optional Python version specifier (PEP 503
            ``data-requires-python``).  Will be HTML-escaped.
        dist_info_metadata: If ``True``, adds PEP 658
            ``data-dist-info-metadata="true"`` attribute.
        gpg_sig: If ``True``, adds ``data-gpg-sig="true"`` attribute.
        yanked: Optional PEP 592 yank reason.  Will be HTML-escaped.

    Returns:
        Complete ``<a>`` tag as an HTML string.
    """
    # Build href with hash fragment for integrity verification
    href = f"{url}#{HASH_ALGORITHM}={sha256}"

    # Accumulate optional data-* attributes
    attrs: list[str] = []
    if requires_python is not None:
        escaped_rp = html.escape(requires_python, quote=True)
        attrs.append(f'data-requires-python="{escaped_rp}"')
    if dist_info_metadata:
        attrs.append('data-dist-info-metadata="true"')
    if gpg_sig:
        attrs.append('data-gpg-sig="true"')
    if yanked is not None:
        escaped_reason = html.escape(yanked, quote=True)
        attrs.append(f'data-yanked="{escaped_reason}"')

    # Build the complete <a> tag
    attr_str = (" " + " ".join(attrs)) if attrs else ""
    escaped_filename = html.escape(filename)
    return f'<a href="{href}"{attr_str}>{escaped_filename}</a>'


# =========================================================================
# Root Index Page Generation
# =========================================================================

def generate_simple_index(
    package_names: list[str],
    base_url: str = "/simple/",
) -> bytes:
    """Generate the PEP 503 Simple Repository root index page.

    Served at ``GET /simple/`` to list all available packages as anchor links.
    Package names are normalized, de-duplicated, and sorted alphabetically for
    deterministic output.

    Args:
        package_names: Iterable of raw package names to include.
        base_url: URL prefix for per-package page links (default
            ``'/simple/'``).

    Returns:
        UTF-8 encoded HTML bytes of the root index page.
    """
    # Normalize, de-duplicate, and sort
    normalized: list[str] = sorted(
        {normalize_name(n) for n in package_names if n}
    )

    # Enforce practical limit for very large repositories
    if len(normalized) > MAX_INDEX_PACKAGES:
        logger.warning(
            "Simple index truncated from %d to %d packages",
            len(normalized),
            MAX_INDEX_PACKAGES,
        )
        normalized = normalized[:MAX_INDEX_PACKAGES]

    # Ensure base_url ends with '/'
    if base_url and not base_url.endswith("/"):
        base_url += "/"

    # Generate one <a> tag per package
    link_lines: list[str] = []
    for name in normalized:
        escaped_name = html.escape(name)
        encoded_name = quote(name, safe="")
        href = f"{base_url}{encoded_name}/"
        link_lines.append(f'<a href="{href}">{escaped_name}</a>')

    links_html = "\n".join(link_lines)
    page = SIMPLE_INDEX_TEMPLATE.format(links=links_html)
    return page.encode("utf-8")


# =========================================================================
# Per-Package Page Generation
# =========================================================================

def generate_package_page(
    package_name: str,
    files: list[dict[str, Any]],
    base_url: str = "/packages/",
) -> bytes:
    """Generate the PEP 503 per-package page listing all files/versions.

    Served at ``GET /simple/{package}/`` for a specific package.

    Each entry in *files* is a dict with at least ``filename``, ``url``, and
    ``sha256`` keys.  Optional keys:

    * ``requires_python`` — Python version specifier (HTML-escaped in output)
    * ``dist_info_metadata`` — boolean for PEP 658
    * ``gpg_sig`` — boolean for GPG signature availability
    * ``yanked`` — PEP 592 yank reason string, or ``None``

    Files are sorted by filename for deterministic output.

    Args:
        package_name: Human-readable package name (will be normalized).
        files: List of file info dicts.
        base_url: URL prefix for download links (default ``'/packages/'``).

    Returns:
        UTF-8 encoded HTML bytes of the per-package page.
    """
    normalized = normalize_name(package_name)

    # Sort files by filename for deterministic output
    sorted_files = sorted(files, key=lambda f: f.get("filename", ""))

    # Generate one <a> tag per file
    link_lines: list[str] = []
    for file_info in sorted_files:
        filename = file_info.get("filename", "")
        file_url = file_info.get("url", "")
        sha256_hash = file_info.get("sha256", "")
        rp = file_info.get("requires_python")
        dist_info = bool(file_info.get("dist_info_metadata", False))
        gpg = bool(file_info.get("gpg_sig", False))
        yank_reason = file_info.get("yanked")

        link = generate_file_link(
            filename=filename,
            url=file_url,
            sha256=sha256_hash,
            requires_python=rp,
            dist_info_metadata=dist_info,
            gpg_sig=gpg,
            yanked=yank_reason,
        )
        link_lines.append(link)

    links_html = "\n".join(link_lines)
    escaped_name = html.escape(normalized)
    page = PACKAGE_PAGE_TEMPLATE.format(
        package_name=escaped_name,
        links=links_html,
    )
    return page.encode("utf-8")


# =========================================================================
# Legacy Upload Form Parsing
# =========================================================================

def parse_upload_form(
    form_data: dict[str, Any],
    files: dict[str, Any],
) -> dict[str, Any]:
    """Parse multipart form data from a PyPI legacy upload request.

    Handles the upload protocol used by ``twine`` and ``setuptools``.  The
    request is a ``POST`` with ``multipart/form-data`` containing the
    package file and associated metadata fields.

    Args:
        form_data: Dictionary of form field values.  Multi-valued fields
            (``classifiers``, ``requires_dist``) may be lists or single
            strings.
        files: Dictionary mapping field names to file-like objects or raw
            bytes.  The uploaded package is expected under the key
            ``'content'``.

    Returns:
        Structured dict with normalised package name, file content, and
        parsed metadata.

    Raises:
        ValueError: If ``:action`` is missing or invalid.
        ValueError: If required fields (``name``, ``version``, ``content``)
            are absent.
    """
    # -- Validate :action field ------------------------------------------------
    action = str(form_data.get(_UPLOAD_FIELD_ACTION, "")).strip()
    if not action:
        raise ValueError(
            "Missing required field ':action' in upload form data"
        )
    if action not in VALID_UPLOAD_ACTIONS:
        raise ValueError(
            f"Invalid ':action' value '{action}'. "
            f"Must be one of: {sorted(VALID_UPLOAD_ACTIONS)}"
        )

    # -- Extract uploaded file --------------------------------------------------
    uploaded_file = files.get(_UPLOAD_FIELD_FILE)
    if uploaded_file is None:
        raise ValueError("Missing required file field 'content' in upload")

    # Support file-like objects (Flask FileStorage) and raw bytes
    if hasattr(uploaded_file, "read"):
        content: bytes = uploaded_file.read()
        filename: str = getattr(uploaded_file, "filename", "") or ""
        content_type: str = (
            getattr(uploaded_file, "content_type", None)
            or "application/octet-stream"
        )
    elif isinstance(uploaded_file, bytes):
        content = uploaded_file
        filename = str(form_data.get("filename", ""))
        content_type = str(
            form_data.get("content_type", "application/octet-stream")
        )
    else:
        raise ValueError(
            "Uploaded file must be a file-like object or bytes"
        )

    # -- Required metadata fields -----------------------------------------------
    name = str(form_data.get(_UPLOAD_FIELD_NAME, "")).strip()
    if not name:
        raise ValueError("Missing required field 'name' in upload form data")
    normalized_name = normalize_name(name)

    version = str(form_data.get(_UPLOAD_FIELD_VERSION, "")).strip()
    if not version:
        raise ValueError(
            "Missing required field 'version' in upload form data"
        )

    # -- Optional scalar fields -------------------------------------------------
    filetype = str(form_data.get(_UPLOAD_FIELD_FILETYPE, "")).strip()
    pyversion = str(form_data.get(_UPLOAD_FIELD_PYVERSION, "")).strip()
    md5_digest = str(form_data.get(_UPLOAD_FIELD_MD5_DIGEST, "")).strip()
    sha256_digest = str(
        form_data.get(_UPLOAD_FIELD_SHA256_DIGEST, "")
    ).strip()

    # -- Metadata fields --------------------------------------------------------
    metadata_version = str(
        form_data.get(_UPLOAD_FIELD_METADATA_VERSION, "")
    ).strip()
    summary = str(form_data.get(_UPLOAD_FIELD_SUMMARY, "")).strip()
    author = str(form_data.get(_UPLOAD_FIELD_AUTHOR, "")).strip()
    author_email = str(
        form_data.get(_UPLOAD_FIELD_AUTHOR_EMAIL, "")
    ).strip()
    license_str = str(form_data.get(_UPLOAD_FIELD_LICENSE, "")).strip()
    requires_python = str(
        form_data.get(_UPLOAD_FIELD_REQUIRES_PYTHON, "")
    ).strip()

    # -- List fields (may appear once or multiple times) -------------------------
    raw_classifiers = form_data.get(_UPLOAD_FIELD_CLASSIFIERS, [])
    if isinstance(raw_classifiers, str):
        classifiers: list[str] = (
            [raw_classifiers] if raw_classifiers else []
        )
    else:
        classifiers = list(raw_classifiers)

    raw_requires_dist = form_data.get(_UPLOAD_FIELD_REQUIRES_DIST, [])
    if isinstance(raw_requires_dist, str):
        requires_dist: list[str] = (
            [raw_requires_dist] if raw_requires_dist else []
        )
    else:
        requires_dist = list(raw_requires_dist)

    return {
        "action": action,
        "filename": filename,
        "content": content,
        "content_type": content_type,
        "name": normalized_name,
        "version": version,
        "filetype": filetype,
        "pyversion": pyversion,
        "md5_digest": md5_digest,
        "sha256_digest": sha256_digest,
        "metadata": {
            "metadata_version": metadata_version,
            "summary": summary,
            "author": author,
            "author_email": author_email,
            "license": license_str,
            "requires_python": requires_python,
            "classifiers": classifiers,
            "requires_dist": requires_dist,
        },
    }


def validate_upload_form(parsed_form: dict[str, Any]) -> tuple[bool, str]:
    """Validate parsed upload form data.

    Checks required fields, file extension, and optional digest integrity.

    Args:
        parsed_form: Dict returned by :func:`parse_upload_form`.

    Returns:
        Tuple of ``(is_valid, error_message)``.  Returns ``(True, '')``
        when all checks pass.
    """
    # 1. Package name must be present and non-empty
    name = parsed_form.get("name", "")
    if not name:
        return (False, "Package name is required")

    # 2. Version must be present and non-empty
    version = parsed_form.get("version", "")
    if not version:
        return (False, "Package version is required")

    # 3. File content must be present and non-empty
    content = parsed_form.get("content", b"")
    if not content:
        return (False, "File content is required and must not be empty")

    # 4. Filename must have a recognized extension
    filename = parsed_form.get("filename", "")
    if filename:
        has_valid_ext = any(
            filename.lower().endswith(ext) for ext in _VALID_FILE_EXTENSIONS
        )
        if not has_valid_ext:
            return (
                False,
                f"Unrecognized file extension for '{filename}'. "
                f"Allowed: {', '.join(_VALID_FILE_EXTENSIONS)}",
            )

    # 5. Verify SHA-256 digest if provided
    sha256_digest = parsed_form.get("sha256_digest", "")
    if sha256_digest:
        computed = compute_file_hash(content, "sha256")
        if computed != sha256_digest.lower():
            logger.warning(
                "SHA-256 digest mismatch for %s: expected=%s computed=%s",
                filename,
                sha256_digest.lower(),
                computed,
            )
            return (
                False,
                "SHA-256 digest mismatch: file integrity check failed",
            )

    # 6. Verify MD5 digest if provided
    md5_digest = parsed_form.get("md5_digest", "")
    if md5_digest:
        computed_md5 = compute_file_hash(content, "md5")
        if computed_md5 != md5_digest.lower():
            logger.warning(
                "MD5 digest mismatch for %s: expected=%s computed=%s",
                filename,
                md5_digest.lower(),
                computed_md5,
            )
            return (
                False,
                "MD5 digest mismatch: file integrity check failed",
            )

    return (True, "")


# =========================================================================
# Internal HTML Parsing Helper
# =========================================================================

def _parse_simple_links(html_content: bytes) -> list[dict[str, str | None]]:
    """Parse anchor tags from a PEP 503 Simple API HTML page.

    Uses lightweight regex-based parsing (no external HTML parser dependency).
    Extracts ``href``, display text, hash fragments, and ``data-*`` attributes
    from each ``<a>`` tag.

    Args:
        html_content: UTF-8 encoded HTML bytes of a Simple API page.

    Returns:
        List of dicts, each containing:

        * ``href`` — full href value
        * ``url`` — href without fragment
        * ``filename`` — link text content
        * ``sha256`` — SHA-256 digest from fragment (or ``None``)
        * ``requires_python`` — ``data-requires-python`` value (or ``None``)
        * ``dist_info_metadata`` — ``data-dist-info-metadata`` value (or ``None``)
        * ``gpg_sig`` — ``data-gpg-sig`` value (or ``None``)
        * ``yanked`` — ``data-yanked`` value (or ``None``)
    """
    text = html_content.decode("utf-8", errors="replace")
    results: list[dict[str, str | None]] = []

    for anchor_match in _ANCHOR_RE.finditer(text):
        attrs_str = anchor_match.group(1)
        link_text = anchor_match.group(2).strip()

        # Extract all name="value" pairs
        attrs: dict[str, str] = {}
        for attr_match in _ATTR_RE.finditer(attrs_str):
            attr_name = attr_match.group(1).lower()
            attr_value = html.unescape(attr_match.group(2))
            attrs[attr_name] = attr_value

        href = attrs.get("href", "")

        # Separate URL from hash fragment
        url: str = href
        sha256_digest: str | None = None
        if "#" in href:
            url_part, fragment_part = href.rsplit("#", 1)
            url = url_part
            parsed = parse_hash_fragment(fragment_part)
            if parsed is not None:
                algo, digest = parsed
                if algo == "sha256":
                    sha256_digest = digest

        results.append({
            "href": href,
            "url": url,
            "filename": link_text,
            "sha256": sha256_digest,
            "requires_python": attrs.get("data-requires-python"),
            "dist_info_metadata": attrs.get("data-dist-info-metadata"),
            "gpg_sig": attrs.get("data-gpg-sig"),
            "yanked": attrs.get("data-yanked"),
        })

    return results


# =========================================================================
# Group Repository — Merge Simple API Pages
# =========================================================================

def merge_simple_pages(pages: list[bytes]) -> bytes | None:
    """Merge PEP 503 per-package pages from multiple group repository members.

    When a group repository aggregates several member repositories, this
    function combines the file listings from each member into a single
    unified page.  File links from higher-priority members (earlier in the
    *pages* list) take precedence over those from lower-priority members
    when the same filename appears in multiple members.

    Args:
        pages: Ordered list of UTF-8 encoded HTML page bytes, one per
            member repository (first = highest priority).

    Returns:
        Merged HTML page bytes, or ``None`` if *pages* is empty or no
        valid links could be extracted.
    """
    if not pages:
        return None
    if len(pages) == 1:
        return pages[0]

    seen_filenames: dict[str, dict[str, str | None]] = {}
    package_name: str | None = None

    for member_index, page_bytes in enumerate(pages):
        try:
            links = _parse_simple_links(page_bytes)
        except Exception:
            logger.warning(
                "Failed to parse Simple API page from group member %d; "
                "skipping",
                member_index,
            )
            continue

        # Extract package name from the first valid page
        if package_name is None:
            page_text = page_bytes.decode("utf-8", errors="replace")
            h1_match = re.search(
                r"<h1>Links for (.+?)</h1>", page_text, re.IGNORECASE,
            )
            if h1_match:
                package_name = h1_match.group(1).strip()
            else:
                title_match = re.search(
                    r"<title>Links for (.+?)</title>",
                    page_text,
                    re.IGNORECASE,
                )
                if title_match:
                    package_name = title_match.group(1).strip()

        # Merge links — first member wins on filename collision
        for link in links:
            fname = link.get("filename", "")
            if fname and fname not in seen_filenames:
                seen_filenames[fname] = link

    if not seen_filenames:
        return None

    if package_name is None:
        package_name = "unknown"

    # Build merged file list sorted by filename
    merged_files: list[dict[str, Any]] = []
    for fname in sorted(seen_filenames):
        link = seen_filenames[fname]
        merged_files.append({
            "filename": fname,
            "url": link.get("url") or "",
            "sha256": link.get("sha256") or "",
            "requires_python": link.get("requires_python"),
            "dist_info_metadata": link.get("dist_info_metadata") == "true",
            "gpg_sig": link.get("gpg_sig") == "true",
            "yanked": link.get("yanked"),
        })

    return generate_package_page(package_name, merged_files)


def merge_simple_indexes(indexes: list[bytes]) -> bytes | None:
    """Merge root index pages from multiple group repository members.

    Collects all unique package names (after PEP 503 normalization) across
    member repositories and produces a single de-duplicated, sorted root
    index page.

    Args:
        indexes: Ordered list of UTF-8 encoded root index HTML page bytes.

    Returns:
        Merged root index HTML page bytes, or ``None`` if *indexes* is
        empty or no valid package names could be extracted.
    """
    if not indexes:
        return None

    all_names: set[str] = set()

    for member_index, index_bytes in enumerate(indexes):
        try:
            links = _parse_simple_links(index_bytes)
        except Exception:
            logger.warning(
                "Failed to parse Simple index page from group member %d; "
                "skipping",
                member_index,
            )
            continue

        for link in links:
            raw_name = (link.get("filename") or "").strip()
            if raw_name:
                all_names.add(normalize_name(raw_name))

    if not all_names:
        return None

    return generate_simple_index(sorted(all_names))
