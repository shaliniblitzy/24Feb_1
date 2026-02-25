"""
Sample artifact generators for the Flask Binary Repository Management System.

Provides factory functions that generate binary artifact payloads for each of the
7 supported repository formats: Maven, npm, Docker, NuGet, PyPI, APT, and Raw.
All artifacts are generated entirely in-memory with deterministic content (via seeded
Faker) and include pre-computed SHA-1, SHA-256, and MD5 checksums for integrity
verification in upload/download, storage, and integration tests.

Usage::

    from tests.fixtures.artifact_data import (
        make_maven_artifact,
        make_npm_artifact,
        make_artifact_for_format,
        SMALL_ARTIFACT_SIZE,
    )

    artifact = make_maven_artifact(group_id='com.acme', version='2.0.0')
    assert artifact['sha256'] == hashlib.sha256(artifact['content']).hexdigest()

All factory functions follow the ``make_*`` naming convention and return dicts
containing at minimum: ``content`` (bytes), ``size`` (int), ``sha1``, ``sha256``,
``md5`` (hex digest strings), and ``content_type`` (MIME string).
"""

import io
import hashlib
import os
import json
import typing
import zipfile
import tarfile
import datetime

from faker import Faker

# ---------------------------------------------------------------------------
# Deterministic Faker instance — seed guarantees reproducible output
# ---------------------------------------------------------------------------
fake = Faker()
Faker.seed(12345)

# ---------------------------------------------------------------------------
# Size constants (bytes) for artifact payload generation
# ---------------------------------------------------------------------------
SMALL_ARTIFACT_SIZE: int = 1024                # 1 KB
MEDIUM_ARTIFACT_SIZE: int = 1024 * 100         # 100 KB
LARGE_ARTIFACT_SIZE: int = 1024 * 1024         # 1 MB
ZERO_BYTE_SIZE: int = 0                        # Edge-case: empty artifact


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_checksums(content: bytes) -> dict:
    """Compute SHA-1, SHA-256 and MD5 hex digests for *content*.

    Returns a dict with keys ``sha1``, ``sha256``, ``md5``.
    """
    return {
        "sha1": hashlib.sha1(content).hexdigest(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "md5": hashlib.md5(content).hexdigest(),
    }


def _build_artifact_dict(
    content: bytes,
    content_type: str,
    **extra,
) -> dict:
    """Assemble the standard artifact dict returned by every generator.

    Computes checksums automatically from *content* and merges any *extra*
    key/value pairs (e.g. ``metadata``, ``path``, ``filename``).
    """
    result = {
        "content": content,
        "size": len(content),
        "content_type": content_type,
    }
    result.update(_compute_checksums(content))
    result.update(extra)
    return result


# ===================================================================
# Core artifact generators
# ===================================================================

def make_binary_artifact(size: int = SMALL_ARTIFACT_SIZE) -> dict:
    """Generate a random binary artifact of the requested *size* in bytes.

    This is the foundational generator used internally by most format-specific
    generators.  The payload is produced via :func:`os.urandom` so it is
    non-deterministic binary noise, but the returned checksums are always
    correct for the generated content.

    Args:
        size: Number of random bytes to generate.  Defaults to
              :data:`SMALL_ARTIFACT_SIZE` (1 KB).

    Returns:
        dict with keys ``content``, ``size``, ``sha1``, ``sha256``, ``md5``,
        ``content_type`` (``'application/octet-stream'``).
    """
    content = os.urandom(size)
    return _build_artifact_dict(content, "application/octet-stream")


def make_text_artifact(content: str | None = None) -> dict:
    """Generate a plain-text artifact.

    If *content* is ``None`` a paragraph is produced via Faker.  The returned
    ``content`` value is always ``bytes`` (UTF-8 encoded).

    Args:
        content: Optional text string.  When omitted, Faker generates a
                 paragraph of lorem-ipsum text.

    Returns:
        dict with keys ``content`` (bytes), ``size``, checksums,
        ``content_type`` (``'text/plain'``).
    """
    if content is None:
        content = fake.paragraph(nb_sentences=5)
    content_bytes = content.encode("utf-8")
    return _build_artifact_dict(content_bytes, "text/plain")


# ===================================================================
# Format-specific generators
# ===================================================================

# ---- Maven ----------------------------------------------------------

def make_maven_artifact(
    group_id: str | None = None,
    artifact_id: str | None = None,
    version: str | None = None,
    packaging: str = "jar",
) -> dict:
    """Generate a Maven-style artifact (JAR, WAR, POM, or EAR).

    For ``jar``, ``war``, and ``ear`` packaging the artifact is a valid ZIP
    archive containing a ``META-INF/MANIFEST.MF`` file.  For ``pom``
    packaging the content is a minimal XML ``pom.xml`` document.

    Args:
        group_id:    Maven group coordinate.  Defaults to ``'com.example.test'``.
        artifact_id: Maven artifact coordinate.  Defaults to a Faker word.
        version:     Version string.  Defaults to ``'1.0.0'``.
        packaging:   One of ``'jar'``, ``'war'``, ``'pom'``, ``'ear'``.

    Returns:
        dict with ``content``, checksums, ``content_type``, ``metadata``, and
        ``path`` (the Maven coordinate path).
    """
    group_id = group_id or "com.example.test"
    artifact_id = artifact_id or fake.word()
    version = version or "1.0.0"

    content_type_map = {
        "jar": "application/java-archive",
        "war": "application/java-archive",
        "ear": "application/java-archive",
        "pom": "application/xml",
    }
    ct = content_type_map.get(packaging, "application/octet-stream")

    if packaging in ("jar", "war", "ear"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            manifest = (
                "Manifest-Version: 1.0\n"
                f"Created-By: test-fixture\n"
                f"Implementation-Title: {artifact_id}\n"
                f"Implementation-Version: {version}\n"
            )
            zf.writestr("META-INF/MANIFEST.MF", manifest)
            # Add a small class-like entry so the archive isn't trivially empty
            zf.writestr(
                f"{group_id.replace('.', '/')}/App.class",
                os.urandom(128),
            )
        content = buf.getvalue()
    else:
        # POM packaging — valid minimal XML
        pom_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<project xmlns="http://maven.apache.org/POM/4.0.0"\n'
            '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 '
            'http://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
            "  <modelVersion>4.0.0</modelVersion>\n"
            f"  <groupId>{group_id}</groupId>\n"
            f"  <artifactId>{artifact_id}</artifactId>\n"
            f"  <version>{version}</version>\n"
            f"  <packaging>{packaging}</packaging>\n"
            "</project>\n"
        )
        content = pom_xml.encode("utf-8")

    group_path = group_id.replace(".", "/")
    coordinate_path = (
        f"{group_path}/{artifact_id}/{version}/"
        f"{artifact_id}-{version}.{packaging}"
    )

    metadata = {
        "group_id": group_id,
        "artifact_id": artifact_id,
        "version": version,
        "packaging": packaging,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    return _build_artifact_dict(
        content, ct, metadata=metadata, path=coordinate_path,
    )


# ---- npm ------------------------------------------------------------

def make_npm_artifact(
    package_name: str | None = None,
    version: str | None = None,
) -> dict:
    """Generate an npm-style ``.tgz`` tarball artifact.

    The tarball contains a ``package/package.json`` entry with ``name``,
    ``version``, and ``description`` fields, plus a small ``package/index.js``
    entry.

    Args:
        package_name: Scoped or unscoped package name.
                      Defaults to ``'@test/<faker-word>'``.
        version:      Semver version string.  Defaults to ``'1.0.0'``.

    Returns:
        dict with ``content`` (gzipped tarball bytes), checksums,
        ``content_type`` (``'application/gzip'``), ``metadata``.
    """
    package_name = package_name or f"@test/{fake.word()}"
    version = version or "1.0.0"

    pkg_json = json.dumps({
        "name": package_name,
        "version": version,
        "description": fake.sentence(),
        "main": "index.js",
        "license": "MIT",
    }).encode("utf-8")

    index_js = b"'use strict';\nmodule.exports = {};\n"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # package.json
        info_pkg = tarfile.TarInfo(name="package/package.json")
        info_pkg.size = len(pkg_json)
        tf.addfile(info_pkg, io.BytesIO(pkg_json))
        # index.js
        info_idx = tarfile.TarInfo(name="package/index.js")
        info_idx.size = len(index_js)
        tf.addfile(info_idx, io.BytesIO(index_js))
    content = buf.getvalue()

    metadata = {
        "name": package_name,
        "version": version,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return _build_artifact_dict(content, "application/gzip", metadata=metadata)


# ---- Docker ---------------------------------------------------------

def make_docker_manifest(
    repository: str | None = None,
    tag: str | None = None,
) -> dict:
    """Generate a Docker V2 Schema 2 image manifest (JSON).

    The manifest references a single config blob and a single layer, both
    with plausible digests computed from random data.

    Args:
        repository: Image repository name.  Defaults to ``'test/<faker-word>'``.
        tag:        Image tag.  Defaults to ``'latest'``.

    Returns:
        dict with ``content`` (JSON bytes), checksums,
        ``content_type`` (``'application/vnd.docker.distribution.manifest.v2+json'``),
        ``metadata``.
    """
    repository = repository or f"test/{fake.word()}"
    tag = tag or "latest"

    # Produce deterministic-looking digests from random bytes
    config_bytes = os.urandom(256)
    layer_bytes = os.urandom(512)
    config_digest = f"sha256:{hashlib.sha256(config_bytes).hexdigest()}"
    layer_digest = f"sha256:{hashlib.sha256(layer_bytes).hexdigest()}"

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "size": len(config_bytes),
            "digest": config_digest,
        },
        "layers": [
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "size": len(layer_bytes),
                "digest": layer_digest,
            }
        ],
    }

    content = json.dumps(manifest, indent=2).encode("utf-8")
    metadata = {
        "repository": repository,
        "tag": tag,
        "config_digest": config_digest,
        "layer_digests": [layer_digest],
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return _build_artifact_dict(
        content,
        "application/vnd.docker.distribution.manifest.v2+json",
        metadata=metadata,
    )


def make_docker_layer(size: int = SMALL_ARTIFACT_SIZE) -> dict:
    """Generate a Docker image layer blob (gzipped tar).

    The layer contains a single random file of the requested *size*.

    Args:
        size: Payload size in bytes.  Defaults to :data:`SMALL_ARTIFACT_SIZE`.

    Returns:
        dict with ``content``, ``digest`` (``'sha256:…'``), checksums,
        ``content_type`` (``'application/vnd.docker.image.rootfs.diff.tar.gzip'``).
    """
    payload = os.urandom(size)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="layer.bin")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    content = buf.getvalue()

    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    return _build_artifact_dict(
        content,
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
        digest=digest,
    )


# ---- NuGet ----------------------------------------------------------

def make_nuget_artifact(
    package_id: str | None = None,
    version: str | None = None,
) -> dict:
    """Generate a NuGet package (``.nupkg``, ZIP format with ``.nuspec``).

    The archive contains a minimal ``.nuspec`` XML metadata file and a
    ``lib/netstandard2.0/placeholder.dll`` binary entry.

    Args:
        package_id: NuGet package identifier.
                    Defaults to ``'Test.<FakerWord>'``.
        version:    Version string.  Defaults to ``'1.0.0'``.

    Returns:
        dict with ``content``, checksums, ``content_type`` (``'application/zip'``),
        ``metadata``.
    """
    word = fake.word().capitalize()
    package_id = package_id or f"Test.{word}"
    version = version or "1.0.0"

    nuspec_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">\n'
        "  <metadata>\n"
        f"    <id>{package_id}</id>\n"
        f"    <version>{version}</version>\n"
        f"    <description>{fake.sentence()}</description>\n"
        "    <authors>test-fixture</authors>\n"
        "    <requireLicenseAcceptance>false</requireLicenseAcceptance>\n"
        "  </metadata>\n"
        "</package>\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{package_id}.nuspec", nuspec_xml)
        zf.writestr("lib/netstandard2.0/placeholder.dll", os.urandom(128))
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
            '  <Default Extension="nuspec" ContentType="application/xml" />\n'
            '  <Default Extension="dll" ContentType="application/octet-stream" />\n'
            "</Types>\n"
        ))
    content = buf.getvalue()

    metadata = {
        "package_id": package_id,
        "version": version,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return _build_artifact_dict(content, "application/zip", metadata=metadata)


# ---- PyPI -----------------------------------------------------------

def make_pypi_artifact(
    package_name: str | None = None,
    version: str | None = None,
    dist_format: str = "sdist",
) -> dict:
    """Generate a PyPI distribution artifact (sdist ``.tar.gz`` or wheel ``.whl``).

    For ``sdist`` the archive is a gzipped tarball with ``setup.py`` and a
    source module.  For ``wheel`` the archive is a ZIP file with a
    ``.dist-info`` directory.

    Args:
        package_name: Python package name.  Defaults to a Faker word.
        version:      Version string.  Defaults to ``'1.0.0'``.
        dist_format:  ``'sdist'`` (default) or ``'wheel'``.

    Returns:
        dict with ``content``, checksums, ``content_type``, ``metadata``
        (includes ``name``, ``version``, ``format``).
    """
    package_name = package_name or fake.word()
    version = version or "1.0.0"

    if dist_format == "wheel":
        # Wheel is ZIP-based with .dist-info metadata
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            dist_info = f"{package_name}-{version}.dist-info"
            zf.writestr(f"{dist_info}/METADATA", (
                "Metadata-Version: 2.1\n"
                f"Name: {package_name}\n"
                f"Version: {version}\n"
                f"Summary: {fake.sentence()}\n"
            ))
            zf.writestr(f"{dist_info}/WHEEL", (
                "Wheel-Version: 1.0\n"
                "Generator: test-fixture\n"
                "Root-Is-Purelib: true\n"
                "Tag: py3-none-any\n"
            ))
            zf.writestr(f"{dist_info}/RECORD", "")
            zf.writestr(f"{package_name}/__init__.py", "# auto-generated\n")
        content = buf.getvalue()
        ct = "application/zip"
    else:
        # sdist — gzipped tarball
        setup_py = (
            "from setuptools import setup\n"
            f"setup(name='{package_name}', version='{version}')\n"
        ).encode("utf-8")
        init_py = b"# auto-generated\n"

        prefix = f"{package_name}-{version}"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info_setup = tarfile.TarInfo(name=f"{prefix}/setup.py")
            info_setup.size = len(setup_py)
            tf.addfile(info_setup, io.BytesIO(setup_py))

            info_init = tarfile.TarInfo(
                name=f"{prefix}/{package_name}/__init__.py",
            )
            info_init.size = len(init_py)
            tf.addfile(info_init, io.BytesIO(init_py))
        content = buf.getvalue()
        ct = "application/gzip"

    metadata = {
        "name": package_name,
        "version": version,
        "format": dist_format,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return _build_artifact_dict(content, ct, metadata=metadata)


# ---- APT ------------------------------------------------------------

def make_apt_artifact(
    package_name: str | None = None,
    version: str | None = None,
    architecture: str = "amd64",
) -> dict:
    """Generate a minimal Debian package (``.deb``) structure.

    The archive is an ``ar``-style binary with a ``control.tar.gz`` member
    containing the package's control metadata and a ``data.tar.gz`` member
    with a small placeholder file.

    Args:
        package_name: Debian package name.  Defaults to ``'test-<faker-word>'``.
        version:      Version string.  Defaults to ``'1.0.0-1'``.
        architecture: Target architecture.  Defaults to ``'amd64'``.

    Returns:
        dict with ``content``, checksums,
        ``content_type`` (``'application/vnd.debian.binary-package'``),
        ``metadata``.
    """
    package_name = package_name or f"test-{fake.word()}"
    version = version or "1.0.0-1"

    control_content = (
        f"Package: {package_name}\n"
        f"Version: {version}\n"
        f"Architecture: {architecture}\n"
        "Maintainer: test-fixture <test@example.com>\n"
        f"Description: {fake.sentence()}\n"
        "Section: misc\n"
        "Priority: optional\n"
    ).encode("utf-8")

    # Build control.tar.gz
    control_buf = io.BytesIO()
    with tarfile.open(fileobj=control_buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="./control")
        info.size = len(control_content)
        tf.addfile(info, io.BytesIO(control_content))
    control_tar = control_buf.getvalue()

    # Build data.tar.gz — a small placeholder file
    placeholder = b"placeholder data\n"
    data_buf = io.BytesIO()
    with tarfile.open(fileobj=data_buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(
            name=f"./usr/share/doc/{package_name}/README",
        )
        info.size = len(placeholder)
        tf.addfile(info, io.BytesIO(placeholder))
    data_tar = data_buf.getvalue()

    # Assemble a simplified .deb (ar archive structure)
    # Real .deb = ar archive with debian-binary + control.tar.gz + data.tar.gz
    # We concatenate with the "!<arch>\n" header for a recognisable signature.
    deb_magic = b"!<arch>\n"
    debian_binary = b"2.0\n"

    def _ar_header(name: bytes, size: int) -> bytes:
        """Build a 60-byte AR member header."""
        return (
            name.ljust(16)
            + b"0           "  # mtime
            + b"0     "       # owner
            + b"0     "       # group
            + b"100644  "     # mode
            + str(size).encode().ljust(10)
            + b"\x60\n"      # end marker
        )

    parts = io.BytesIO()
    parts.write(deb_magic)

    parts.write(_ar_header(b"debian-binary", len(debian_binary)))
    parts.write(debian_binary)
    if len(debian_binary) % 2:
        parts.write(b"\n")

    parts.write(_ar_header(b"control.tar.gz", len(control_tar)))
    parts.write(control_tar)
    if len(control_tar) % 2:
        parts.write(b"\n")

    parts.write(_ar_header(b"data.tar.gz", len(data_tar)))
    parts.write(data_tar)
    if len(data_tar) % 2:
        parts.write(b"\n")

    content = parts.getvalue()

    metadata = {
        "package_name": package_name,
        "version": version,
        "architecture": architecture,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return _build_artifact_dict(
        content,
        "application/vnd.debian.binary-package",
        metadata=metadata,
    )


# ---- Raw ------------------------------------------------------------

def make_raw_artifact(
    filename: str | None = None,
    content_type: str | None = None,
    size: int = SMALL_ARTIFACT_SIZE,
) -> dict:
    """Generate a raw binary blob artifact with an arbitrary content type.

    Args:
        filename:     Target filename.  Defaults to a Faker-generated name.
        content_type: MIME type string.  Defaults to
                      ``'application/octet-stream'``.
        size:         Payload size in bytes.

    Returns:
        dict with ``content``, ``filename``, checksums, ``content_type``.
    """
    filename = filename or fake.file_name()
    content_type = content_type or "application/octet-stream"
    content = os.urandom(size)
    return _build_artifact_dict(
        content, content_type, filename=filename,
    )


# ===================================================================
# Collection / batch generators
# ===================================================================

# Map of format name → generator callable (populated lazily below)
_FORMAT_GENERATORS: dict[str, typing.Callable] = {}


def _get_format_generators() -> dict[str, typing.Callable]:
    """Return the format→generator mapping, building it on first call."""
    if not _FORMAT_GENERATORS:
        _FORMAT_GENERATORS.update({
            "maven": make_maven_artifact,
            "npm": make_npm_artifact,
            "docker": make_docker_manifest,
            "nuget": make_nuget_artifact,
            "pypi": make_pypi_artifact,
            "apt": make_apt_artifact,
            "raw": make_raw_artifact,
        })
    return _FORMAT_GENERATORS


def make_artifact_for_format(format_type: str, **kwargs) -> dict:
    """Dispatch to the correct format-specific generator.

    Args:
        format_type: One of ``'maven'``, ``'npm'``, ``'docker'``,
                     ``'nuget'``, ``'pypi'``, ``'apt'``, ``'raw'``.
        **kwargs:    Passed through to the underlying generator.

    Raises:
        ValueError: If *format_type* is not a recognised format.

    Returns:
        dict produced by the chosen format generator.
    """
    generators = _get_format_generators()
    if format_type not in generators:
        raise ValueError(
            f"Unsupported artifact format: '{format_type}'. "
            f"Supported formats: {sorted(generators.keys())}"
        )
    return generators[format_type](**kwargs)


def make_artifact_set(format_type: str = "maven", count: int = 3) -> list[dict]:
    """Generate a collection of artifacts for batch upload testing.

    Each artifact in the returned list is produced by the appropriate
    format-specific generator with default parameters (each call generates
    unique Faker-driven names).

    Args:
        format_type: Repository format (see :func:`make_artifact_for_format`).
        count:       Number of artifacts to produce.

    Returns:
        list of artifact dicts.
    """
    return [make_artifact_for_format(format_type) for _ in range(count)]


# ===================================================================
# Edge-case generators
# ===================================================================

def make_zero_byte_artifact() -> dict:
    """Generate a zero-byte artifact for boundary condition testing.

    Returns:
        dict with empty ``content``, zero ``size``, and checksums computed
        on the empty byte-string.
    """
    content = b""
    return _build_artifact_dict(content, "application/octet-stream")


def make_large_artifact(size: int = LARGE_ARTIFACT_SIZE) -> dict:
    """Generate a large binary artifact for size-limit and streaming tests.

    Args:
        size: Payload size in bytes.  Defaults to :data:`LARGE_ARTIFACT_SIZE`
              (1 MB).

    Returns:
        Standard artifact dict with computed checksums.
    """
    content = os.urandom(size)
    return _build_artifact_dict(content, "application/octet-stream")


def make_corrupted_artifact(base_format: str = "maven") -> dict:
    """Generate an artifact with intentionally corrupted content.

    For archive-based formats (``maven``, ``npm``, ``nuget``, ``pypi``,
    ``apt``) the content starts with a valid archive magic header but the
    remainder is random noise, producing an invalid archive.

    For metadata-based formats (``docker``) the content is syntactically
    malformed JSON.

    For ``raw`` the content is normal random bytes — there is no structural
    expectation to corrupt.

    Args:
        base_format: The format whose corruption pattern to mimic.

    Returns:
        dict with corrupted ``content``, correct checksums of that corrupted
        content, and ``corrupted`` flag set to ``True``.
    """
    if base_format in ("maven", "nuget"):
        # ZIP magic header followed by random noise
        header = b"PK\x03\x04"
        content = header + os.urandom(256)
    elif base_format in ("npm", "pypi", "apt"):
        # gzip magic header followed by random noise
        header = b"\x1f\x8b\x08"
        content = header + os.urandom(256)
    elif base_format == "docker":
        # Malformed JSON — truncated structure
        content = b'{"schemaVersion": 2, "config": {INVALID'
    else:
        # raw / unknown — just random bytes labelled corrupted
        content = os.urandom(256)

    result = _build_artifact_dict(content, "application/octet-stream")
    result["corrupted"] = True
    result["base_format"] = base_format
    return result


def make_unicode_filename_artifact(filename: str | None = None) -> dict:
    """Generate an artifact whose filename contains Unicode characters.

    Useful for verifying that the application handles internationalised
    filenames correctly (storage paths, HTTP headers, database columns).

    Args:
        filename: Unicode filename.  When omitted a name combining CJK,
                  Cyrillic, and emoji characters is generated.

    Returns:
        dict with ``content``, ``filename`` (Unicode), checksums,
        ``content_type``.
    """
    if filename is None:
        # Mix of CJK ideograph, Cyrillic, accented Latin, and emoji
        base_name = fake.file_name()
        name_part, _, ext = base_name.rpartition(".")
        filename = f"文件_{name_part}_файл_données_📦.{ext}"

    content = os.urandom(SMALL_ARTIFACT_SIZE)
    return _build_artifact_dict(
        content, "application/octet-stream", filename=filename,
    )
