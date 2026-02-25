"""
APT/Debian Repository Metadata Generation.

This module implements all APT repository metadata generation logic for the
Nexus Repository Python/Flask reimplementation.  It handles the creation and
management of every metadata file required by APT-based package managers
(``apt-get``, ``aptitude``, ``apt``) to interact with a Debian repository.

Corresponds to **Feature F-101-RQ-003** (APT format support) from the AAP.

**Metadata files produced:**

- ``Packages``  — uncompressed package index
- ``Packages.gz`` — gzip-compressed package index
- ``Packages.bz2`` — bzip2-compressed package index
- ``Sources`` — source package index
- ``Sources.gz`` — gzip-compressed source index
- ``Release`` — distribution metadata with checksums
- ``InRelease`` — inline-signed Release file
- ``Release.gpg`` — detached GPG signature

**Architecture:**

Replaces the Java APT format bundle's metadata generation logic (OSGi).
All cryptographic signing operations use the ``cryptography`` library
(44.0.0), replacing BouncyCastle 1.78.1 from the Java source.
"""

from __future__ import annotations

import base64
import bz2
import gzip
import hashlib
import io
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

if TYPE_CHECKING:
    from src.app.models.asset import Asset
    from src.app.models.component import Component
    from src.app.models.repository import Repository

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
__all__: list[str] = [
    "AptMetadataManager",
    "PackagesIndexGenerator",
    "ReleaseFileGenerator",
    "InReleaseGenerator",
    "SourcesIndexGenerator",
    "parse_deb_control",
    "compute_file_hashes",
    "format_rfc2822_date",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Standard APT repository directory structure paths
DISTS_PREFIX: str = "dists"
POOL_PREFIX: str = "pool"

# Index file names
PACKAGES_INDEX: str = "Packages"
PACKAGES_GZ: str = "Packages.gz"
PACKAGES_BZ2: str = "Packages.bz2"
RELEASE_FILE: str = "Release"
INRELEASE_FILE: str = "InRelease"
RELEASE_GPG: str = "Release.gpg"
SOURCES_INDEX: str = "Sources"
SOURCES_GZ: str = "Sources.gz"

# Default distribution and component
DEFAULT_DISTRIBUTION: str = "bionic"
DEFAULT_COMPONENT: str = "main"

# Supported architectures
SUPPORTED_ARCHITECTURES: list[str] = ["amd64", "i386", "arm64", "armhf", "all"]

# Content types for APT metadata files
CONTENT_TYPE_PACKAGES: str = "text/plain"
CONTENT_TYPE_RELEASE: str = "text/plain"
CONTENT_TYPE_GPG_SIGNATURE: str = "application/pgp-signature"
CONTENT_TYPE_GZIP: str = "application/gzip"
CONTENT_TYPE_BZIP2: str = "application/x-bzip2"

# Ordered list of required fields that appear first in a Packages entry,
# followed by optional fields.  The ordering follows Debian Policy §5.3.
_PACKAGES_REQUIRED_FIELDS: list[str] = [
    "Package",
    "Version",
    "Architecture",
    "Maintainer",
    "Description",
]

_PACKAGES_OPTIONAL_FIELDS: list[str] = [
    "Section",
    "Priority",
    "Essential",
    "Installed-Size",
    "Pre-Depends",
    "Depends",
    "Recommends",
    "Suggests",
    "Conflicts",
    "Replaces",
    "Provides",
    "Breaks",
    "Enhances",
    "Homepage",
    "Built-Using",
    "Source",
    "Tag",
]

# Fields appended at the end of a Packages stanza (size/checksum/filename).
_PACKAGES_TRAILING_FIELDS: list[str] = [
    "Filename",
    "Size",
    "MD5sum",
    "SHA1",
    "SHA256",
]


# ===========================================================================
# PackagesIndexGenerator
# ===========================================================================


class PackagesIndexGenerator:
    """Generate Debian ``Packages`` index files.

    Produces the per-architecture index that lists every ``.deb`` package
    available in a repository component.  The index is available in three
    variants: uncompressed, gzip-compressed, and bzip2-compressed.

    The generated output is **deterministic** — entries are sorted by
    ``(Package, Version, Architecture)`` and fields within each stanza
    follow a consistent, policy-compliant ordering.
    """

    def __init__(self) -> None:
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.PackagesIndexGenerator"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_packages_entry(self, package_metadata: dict) -> str:
        """Generate a single ``Packages`` index stanza for one ``.deb`` package.

        The *package_metadata* dictionary contains fields extracted from the
        ``.deb`` control file and augmented with storage information such as
        ``Filename``, ``Size``, ``MD5sum``, ``SHA1``, and ``SHA256``.

        Multi-line ``Description`` values are handled correctly: continuation
        lines are prefixed with a single space, and bare empty continuation
        lines use a dot (`` .``) per Debian Policy §5.6.13.

        Args:
            package_metadata: Dictionary of control-file key/value pairs.

        Returns:
            A formatted stanza string (without trailing blank line).
        """
        if not package_metadata:
            self.logger.warning("Empty package metadata provided; returning empty entry")
            return ""

        lines: list[str] = []

        # Emit required fields first, in policy order.
        for field in _PACKAGES_REQUIRED_FIELDS:
            value = package_metadata.get(field)
            if value is not None:
                lines.extend(self._format_field(field, str(value)))

        # Emit optional fields if present.
        for field in _PACKAGES_OPTIONAL_FIELDS:
            value = package_metadata.get(field)
            if value is not None:
                lines.extend(self._format_field(field, str(value)))

        # Emit trailing storage fields.
        for field in _PACKAGES_TRAILING_FIELDS:
            value = package_metadata.get(field)
            if value is not None:
                lines.extend(self._format_field(field, str(value)))

        # Emit any remaining fields not already covered.
        emitted = set(_PACKAGES_REQUIRED_FIELDS) | set(
            _PACKAGES_OPTIONAL_FIELDS
        ) | set(_PACKAGES_TRAILING_FIELDS)
        for key, value in sorted(package_metadata.items()):
            if key not in emitted and value is not None:
                lines.extend(self._format_field(key, str(value)))

        return "\n".join(lines)

    def generate_packages_index(self, packages: list[dict]) -> bytes:
        """Generate a complete ``Packages`` index from a list of metadata dicts.

        Entries are sorted by ``(Package, Version, Architecture)`` for
        deterministic, reproducible output.

        Args:
            packages: List of package-metadata dictionaries.

        Returns:
            UTF-8 encoded ``Packages`` index content.
        """
        if not packages:
            self.logger.debug("No packages provided; returning empty index")
            return b""

        # Sort for deterministic output.
        sorted_packages = sorted(
            packages,
            key=lambda p: (
                p.get("Package", ""),
                p.get("Version", ""),
                p.get("Architecture", ""),
            ),
        )

        entries: list[str] = []
        for pkg in sorted_packages:
            entry = self.generate_packages_entry(pkg)
            if entry:
                entries.append(entry)

        # Debian Packages files separate stanzas with a blank line and end
        # with a trailing newline.
        content = "\n\n".join(entries)
        if content:
            content += "\n"

        self.logger.debug(
            "Generated Packages index with %d entries (%d bytes)",
            len(entries),
            len(content),
        )
        return content.encode("utf-8")

    def generate_packages_gz(self, packages: list[dict]) -> bytes:
        """Generate a gzip-compressed ``Packages.gz`` index.

        Args:
            packages: List of package-metadata dictionaries.

        Returns:
            Gzip-compressed bytes at maximum compression (level 9).
        """
        raw = self.generate_packages_index(packages)
        compressed = gzip.compress(raw, compresslevel=9)
        self.logger.debug(
            "Compressed Packages index: %d → %d bytes (gzip)",
            len(raw),
            len(compressed),
        )
        return compressed

    def generate_packages_bz2(self, packages: list[dict]) -> bytes:
        """Generate a bzip2-compressed ``Packages.bz2`` index.

        Args:
            packages: List of package-metadata dictionaries.

        Returns:
            Bzip2-compressed bytes at maximum compression (level 9).
        """
        raw = self.generate_packages_index(packages)
        compressed = bz2.compress(raw, compresslevel=9)
        self.logger.debug(
            "Compressed Packages index: %d → %d bytes (bzip2)",
            len(raw),
            len(compressed),
        )
        return compressed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_field(key: str, value: str) -> list[str]:
        """Format a single control-file field, handling multi-line values.

        For multi-line ``Description`` (or any field whose value contains
        embedded newlines), continuation lines are indented with a single
        leading space.  Blank continuation lines are replaced with `` .``
        (space-dot) per Debian Policy.

        Returns:
            A list of formatted lines (without trailing ``\\n``).
        """
        parts = value.split("\n")
        result: list[str] = [f"{key}: {parts[0]}"]
        for continuation in parts[1:]:
            if continuation.strip() == "":
                result.append(" .")
            elif continuation.startswith(" ") or continuation.startswith("\t"):
                result.append(continuation)
            else:
                result.append(f" {continuation}")
        return result


# ===========================================================================
# ReleaseFileGenerator
# ===========================================================================


class ReleaseFileGenerator:
    """Generate ``Release`` files for APT distributions.

    A Release file lists available components, architectures, and — most
    critically — the MD5Sum, SHA1, and SHA256 checksums for every index file
    in the distribution.  APT clients use these checksums to verify the
    integrity of downloaded index files.

    The output is **deterministic**: checksum lines are sorted by file path
    and the header fields follow a consistent ordering.
    """

    def __init__(self) -> None:
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.ReleaseFileGenerator"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_release(
        self,
        distribution: str,
        components: list[str],
        architectures: list[str],
        index_files: dict[str, bytes],
        label: str | None = None,
        origin: str | None = None,
        description: str | None = None,
        codename: str | None = None,
    ) -> bytes:
        """Generate a complete ``Release`` file for an APT distribution.

        Args:
            distribution: Distribution name (e.g. ``'bionic'``, ``'focal'``).
            components: Component names (e.g. ``['main', 'contrib']``).
            architectures: Architecture names (e.g. ``['amd64', 'i386']``).
            index_files: Mapping of *relative* file paths (within the
                ``dists/<distribution>/`` tree) to their raw bytes content.
            label: Optional repository label.
            origin: Optional repository origin.
            description: Optional human-readable description.
            codename: Optional distribution codename.

        Returns:
            UTF-8 encoded ``Release`` file content.
        """
        lines: list[str] = []

        # -- Header fields ---------------------------------------------------
        lines.append(f"Origin: {origin or 'Nexus Repository'}")
        if label:
            lines.append(f"Label: {label}")
        lines.append(f"Suite: {distribution}")
        if codename:
            lines.append(f"Codename: {codename}")
        lines.append(f"Date: {format_rfc2822_date()}")
        lines.append(f"Architectures: {' '.join(architectures)}")
        lines.append(f"Components: {' '.join(components)}")
        if description:
            lines.append(f"Description: {description}")

        # -- Compute checksums for every index file --------------------------
        file_checksums: list[dict] = []
        for path in sorted(index_files.keys()):
            data = index_files[path]
            checksums = self._compute_file_checksums(data)
            file_checksums.append(
                {
                    "path": path,
                    "size": len(data),
                    "md5": checksums["md5"],
                    "sha1": checksums["sha1"],
                    "sha256": checksums["sha256"],
                }
            )

        # -- Checksum blocks -------------------------------------------------
        lines.append("MD5Sum:")
        lines.append(self._format_checksum_lines(file_checksums, "md5"))
        lines.append("SHA1:")
        lines.append(self._format_checksum_lines(file_checksums, "sha1"))
        lines.append("SHA256:")
        lines.append(self._format_checksum_lines(file_checksums, "sha256"))

        content = "\n".join(lines) + "\n"
        self.logger.debug(
            "Generated Release file for '%s' with %d index files (%d bytes)",
            distribution,
            len(index_files),
            len(content),
        )
        return content.encode("utf-8")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_file_checksums(self, data: bytes) -> dict[str, str]:
        """Compute MD5, SHA-1, and SHA-256 hex digests for *data*.

        Args:
            data: Raw bytes to hash.

        Returns:
            Dictionary with keys ``'md5'``, ``'sha1'``, ``'sha256'``.
        """
        return {
            "md5": hashlib.md5(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def _format_checksum_lines(
        self, checksums: list[dict], algorithm: str
    ) -> str:
        """Format checksum lines for a Release file block.

        Each line follows the format::

            `` <checksum> <size> <path>``

        (Note the leading space — required by the APT Release format.)

        Lines are sorted by path for deterministic output.

        Args:
            checksums: List of dicts, each with ``'path'``, ``'size'``, and
                the algorithm key (``'md5'``, ``'sha1'``, or ``'sha256'``).
            algorithm: One of ``'md5'``, ``'sha1'``, ``'sha256'``.

        Returns:
            A multi-line string of formatted checksum entries.
        """
        lines: list[str] = []
        for entry in sorted(checksums, key=lambda e: e["path"]):
            digest = entry[algorithm]
            size = entry["size"]
            path = entry["path"]
            lines.append(f" {digest} {size:>16} {path}")
        return "\n".join(lines)


# ===========================================================================
# InReleaseGenerator
# ===========================================================================


class InReleaseGenerator:
    """Generate ``InRelease`` files with inline GPG-style cryptographic signatures.

    When a PEM-encoded private key is provided at construction time, the
    generator signs Release content using RSA (PKCS1v15) or EC (ECDSA) with
    SHA-256.  If no key is provided, the generator operates in *unsigned*
    mode and returns the Release content verbatim.

    Uses the ``cryptography`` library (v44.0.0) — replacing BouncyCastle
    1.78.1 from the Java source system.
    """

    def __init__(self, private_key_pem: bytes | None = None) -> None:
        """Initialise the generator, optionally loading a signing key.

        Args:
            private_key_pem: PEM-encoded private key bytes.  Pass ``None``
                to disable signing (the generator will emit unsigned content).
        """
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.InReleaseGenerator"
        )
        self._private_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | None = None

        if private_key_pem is not None:
            try:
                loaded_key = serialization.load_pem_private_key(
                    private_key_pem, password=None, backend=default_backend()
                )
                if isinstance(
                    loaded_key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)
                ):
                    self._private_key = loaded_key
                    self.logger.info(
                        "Repository signing enabled (key type: %s)",
                        type(loaded_key).__name__,
                    )
                else:
                    self.logger.warning(
                        "Unsupported private key type %s — signing disabled",
                        type(loaded_key).__name__,
                    )
            except Exception:
                self.logger.exception(
                    "Failed to load signing key — signing disabled"
                )
        else:
            self.logger.info("No signing key provided — signing disabled")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_inrelease(self, release_content: bytes) -> bytes:
        """Generate an ``InRelease`` file with an inline GPG-style signature.

        If a signing key is available the output follows the PGP Cleartext
        Signature Framework (RFC 4880 section 7)::

            -----BEGIN PGP SIGNED MESSAGE-----
            Hash: SHA256

            <Release file content>
            -----BEGIN PGP SIGNATURE-----

            <Base64-encoded signature>
            -----END PGP SIGNATURE-----

        If no signing key is configured the raw *release_content* is returned
        unchanged.

        Args:
            release_content: Raw bytes of the ``Release`` file.

        Returns:
            UTF-8 encoded ``InRelease`` content (signed or unsigned).
        """
        if self._private_key is None:
            self.logger.debug("Signing disabled; returning unsigned InRelease")
            return release_content

        signature_bytes = self.sign_data(release_content)
        encoded_sig = base64.b64encode(signature_bytes).decode("ascii")

        # Wrap the base64 signature to 76-character lines per PGP convention.
        wrapped_sig = "\n".join(
            encoded_sig[i : i + 76] for i in range(0, len(encoded_sig), 76)
        )

        release_text = release_content.decode("utf-8")
        inrelease = (
            "-----BEGIN PGP SIGNED MESSAGE-----\n"
            "Hash: SHA256\n"
            "\n"
            f"{release_text}"
            "-----BEGIN PGP SIGNATURE-----\n"
            "\n"
            f"{wrapped_sig}\n"
            "-----END PGP SIGNATURE-----\n"
        )

        self.logger.debug(
            "Generated signed InRelease (%d bytes)", len(inrelease)
        )
        return inrelease.encode("utf-8")

    def generate_detached_signature(self, content: bytes) -> bytes | None:
        """Generate a detached GPG-style signature (``Release.gpg``).

        Args:
            content: Data bytes to sign.

        Returns:
            Base64-encoded signature bytes, or ``None`` if signing is
            disabled.
        """
        if self._private_key is None:
            self.logger.debug(
                "Signing disabled; no detached signature generated"
            )
            return None

        signature_bytes = self.sign_data(content)
        encoded = base64.b64encode(signature_bytes)

        # Produce an armored-style output.
        encoded_str = encoded.decode("ascii")
        wrapped = "\n".join(
            encoded_str[i : i + 76] for i in range(0, len(encoded_str), 76)
        )
        armored = (
            "-----BEGIN PGP SIGNATURE-----\n"
            "\n"
            f"{wrapped}\n"
            "-----END PGP SIGNATURE-----\n"
        )
        self.logger.debug(
            "Generated detached signature (%d bytes)", len(armored)
        )
        return armored.encode("utf-8")

    def sign_data(self, data: bytes) -> bytes:
        """Sign *data* using the configured private key.

        Supports RSA (PKCS#1 v1.5 + SHA-256) and EC (ECDSA + SHA-256) keys.

        Args:
            data: Bytes to sign.

        Returns:
            Raw signature bytes.

        Raises:
            ValueError: If no private key is configured.
        """
        if self._private_key is None:
            raise ValueError(
                "Cannot sign data: no private key configured for this generator"
            )

        if isinstance(self._private_key, rsa.RSAPrivateKey):
            return self._private_key.sign(
                data,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )

        if isinstance(self._private_key, ec.EllipticCurvePrivateKey):
            return self._private_key.sign(
                data,
                ec.ECDSA(hashes.SHA256()),
            )

        # Defensive: should never reach here given __init__ validation.
        raise ValueError(
            f"Unsupported private key type: {type(self._private_key).__name__}"
        )

    def verify_signature(
        self, data: bytes, signature: bytes, public_key_pem: bytes
    ) -> bool:
        """Verify a cryptographic signature against *data*.

        Supports RSA and EC public keys.  Used for validating signatures
        on proxy'd APT repositories.

        Args:
            data: The original data that was signed.
            signature: The raw signature bytes to verify.
            public_key_pem: PEM-encoded public key bytes.

        Returns:
            ``True`` if the signature is valid, ``False`` otherwise.
        """
        try:
            public_key = serialization.load_pem_public_key(
                public_key_pem, backend=default_backend()
            )
        except Exception:
            self.logger.exception(
                "Failed to load public key for verification"
            )
            return False

        try:
            if isinstance(public_key, rsa.RSAPublicKey):
                public_key.verify(
                    signature,
                    data,
                    padding.PKCS1v15(),
                    hashes.SHA256(),
                )
                return True

            if isinstance(public_key, ec.EllipticCurvePublicKey):
                public_key.verify(
                    signature,
                    data,
                    ec.ECDSA(hashes.SHA256()),
                )
                return True

            self.logger.warning(
                "Unsupported public key type for verification: %s",
                type(public_key).__name__,
            )
            return False
        except Exception:
            self.logger.debug("Signature verification failed", exc_info=True)
            return False


# ===========================================================================
# SourcesIndexGenerator
# ===========================================================================


class SourcesIndexGenerator:
    """Generate ``Sources`` index files listing Debian source packages.

    Source packages consist of the upstream tarball(s), a Debian diff or
    patches archive, and a ``.dsc`` control file.  The Sources index
    describes each source package's metadata and the checksums of its
    constituent files.
    """

    def __init__(self) -> None:
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.SourcesIndexGenerator"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_sources_entry(self, source_metadata: dict) -> str:
        """Generate a single ``Sources`` index stanza.

        Expected keys in *source_metadata*:

        - ``Package`` — source package name
        - ``Version`` — upstream + Debian version
        - ``Binary`` — comma-separated list of binary package names
        - ``Maintainer`` — package maintainer
        - ``Architecture`` — build architectures
        - ``Standards-Version`` — Debian Policy version
        - ``Format`` — source format (e.g. ``'3.0 (quilt)'``)
        - ``Directory`` — pool path to source files
        - ``Files`` — list of dicts: ``{'md5sum', 'size', 'name'}``
        - ``Checksums-Sha1`` — list of dicts: ``{'sha1', 'size', 'name'}``
        - ``Checksums-Sha256`` — list of dicts: ``{'sha256', 'size', 'name'}``

        Returns:
            Formatted stanza string (without trailing blank line).
        """
        if not source_metadata:
            self.logger.warning(
                "Empty source metadata provided; returning empty entry"
            )
            return ""

        # Required header fields in order.
        _SOURCES_FIELD_ORDER: list[str] = [
            "Package",
            "Version",
            "Binary",
            "Maintainer",
            "Architecture",
            "Standards-Version",
            "Format",
            "Build-Depends",
            "Homepage",
            "Directory",
        ]

        lines: list[str] = []

        # Emit ordered header fields.
        for field in _SOURCES_FIELD_ORDER:
            value = source_metadata.get(field)
            if value is not None:
                lines.append(f"{field}: {value}")

        # Emit file checksum blocks.
        for block_name, hash_key in [
            ("Files", "md5sum"),
            ("Checksums-Sha1", "sha1"),
            ("Checksums-Sha256", "sha256"),
        ]:
            file_entries = source_metadata.get(block_name)
            if file_entries:
                lines.append(f"{block_name}:")
                for entry in sorted(file_entries, key=lambda e: e.get("name", "")):
                    digest = entry.get(hash_key, "")
                    size = entry.get("size", 0)
                    name = entry.get("name", "")
                    lines.append(f" {digest} {size} {name}")

        # Emit any remaining fields not already covered.
        emitted = set(_SOURCES_FIELD_ORDER) | {
            "Files",
            "Checksums-Sha1",
            "Checksums-Sha256",
        }
        for key, value in sorted(source_metadata.items()):
            if key not in emitted and value is not None:
                if not isinstance(value, (list, dict)):
                    lines.append(f"{key}: {value}")

        return "\n".join(lines)

    def generate_sources_index(self, sources: list[dict]) -> bytes:
        """Generate a complete ``Sources`` index from a list of metadata dicts.

        Entries are sorted by ``(Package, Version)`` for deterministic output.

        Args:
            sources: List of source-metadata dictionaries.

        Returns:
            UTF-8 encoded ``Sources`` index content.
        """
        if not sources:
            self.logger.debug("No sources provided; returning empty index")
            return b""

        sorted_sources = sorted(
            sources,
            key=lambda s: (s.get("Package", ""), s.get("Version", "")),
        )

        entries: list[str] = []
        for src in sorted_sources:
            entry = self.generate_sources_entry(src)
            if entry:
                entries.append(entry)

        content = "\n\n".join(entries)
        if content:
            content += "\n"

        self.logger.debug(
            "Generated Sources index with %d entries (%d bytes)",
            len(entries),
            len(content),
        )
        return content.encode("utf-8")

    def generate_sources_gz(self, sources: list[dict]) -> bytes:
        """Generate a gzip-compressed ``Sources.gz`` index.

        Args:
            sources: List of source-metadata dictionaries.

        Returns:
            Gzip-compressed bytes at maximum compression (level 9).
        """
        raw = self.generate_sources_index(sources)
        compressed = gzip.compress(raw, compresslevel=9)
        self.logger.debug(
            "Compressed Sources index: %d -> %d bytes (gzip)",
            len(raw),
            len(compressed),
        )
        return compressed


# ===========================================================================
# AptMetadataManager — Facade
# ===========================================================================


class AptMetadataManager:
    """High-level manager that coordinates generation of all APT metadata files.

    Acts as a **Facade** over the individual generators
    (:class:`PackagesIndexGenerator`, :class:`ReleaseFileGenerator`,
    :class:`InReleaseGenerator`, :class:`SourcesIndexGenerator`) and is
    consumed by the ``AptFormatHandler`` to regenerate metadata whenever
    repository content changes.
    """

    def __init__(self, signing_key_pem: bytes | None = None) -> None:
        """Create the manager, instantiating all sub-generators.

        Args:
            signing_key_pem: Optional PEM private key for InRelease signing.
        """
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.AptMetadataManager"
        )
        self._packages_gen = PackagesIndexGenerator()
        self._release_gen = ReleaseFileGenerator()
        self._inrelease_gen = InReleaseGenerator(signing_key_pem)
        self._sources_gen = SourcesIndexGenerator()
        self.logger.info("AptMetadataManager initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_repository_metadata(
        self,
        distribution: str,
        components: list[str],
        architectures: list[str],
        packages: list[dict],
        sources: list[dict] | None = None,
        origin: str = "Nexus Repository",
        label: str | None = None,
    ) -> dict[str, bytes]:
        """Generate **all** metadata files for an APT distribution.

        The returned dictionary maps relative file paths (rooted at the
        repository base) to their raw bytes content.

        Algorithm:
            1. For each ``(component, architecture)`` pair, generate the
               three Packages index variants (plain, .gz, .bz2).
            2. For each component, generate the Sources index variants
               (if *sources* is provided).
            3. Collect all generated index files with their relative paths.
            4. Generate the ``Release`` file with checksums of all indices.
            5. Generate ``InRelease`` (signed) and ``Release.gpg`` (detached).

        Args:
            distribution: Distribution name (e.g. ``'bionic'``).
            components: Component names (e.g. ``['main']``).
            architectures: Architecture names (e.g. ``['amd64', 'all']``).
            packages: List of package-metadata dicts for the Packages index.
            sources: Optional list of source-metadata dicts for the Sources
                index.  If ``None``, Sources indices are not generated.
            origin: Repository origin for the Release file header.
            label: Optional label for the Release file header.

        Returns:
            ``dict[str, bytes]`` mapping relative paths to file content.
        """
        metadata: dict[str, bytes] = {}
        # Collect index files whose checksums are included in the Release file.
        # Keys are paths *relative to* ``dists/<distribution>/``.
        index_files: dict[str, bytes] = {}

        # -- 1. Packages indices per component x architecture ----------------
        for comp in components:
            for arch in architectures:
                prefix = f"{comp}/binary-{arch}"
                full_prefix = f"{DISTS_PREFIX}/{distribution}/{prefix}"

                packages_raw = self._packages_gen.generate_packages_index(
                    packages
                )
                packages_gz = self._packages_gen.generate_packages_gz(packages)
                packages_bz2 = self._packages_gen.generate_packages_bz2(
                    packages
                )

                metadata[f"{full_prefix}/{PACKAGES_INDEX}"] = packages_raw
                metadata[f"{full_prefix}/{PACKAGES_GZ}"] = packages_gz
                metadata[f"{full_prefix}/{PACKAGES_BZ2}"] = packages_bz2

                # Paths relative to dists/<distribution>/ for Release checksum.
                index_files[f"{prefix}/{PACKAGES_INDEX}"] = packages_raw
                index_files[f"{prefix}/{PACKAGES_GZ}"] = packages_gz
                index_files[f"{prefix}/{PACKAGES_BZ2}"] = packages_bz2

        # -- 2. Sources indices per component --------------------------------
        if sources:
            for comp in components:
                source_prefix = f"{comp}/source"
                full_source_prefix = (
                    f"{DISTS_PREFIX}/{distribution}/{source_prefix}"
                )

                sources_raw = self._sources_gen.generate_sources_index(sources)
                sources_gz = self._sources_gen.generate_sources_gz(sources)

                metadata[f"{full_source_prefix}/{SOURCES_INDEX}"] = sources_raw
                metadata[f"{full_source_prefix}/{SOURCES_GZ}"] = sources_gz

                index_files[f"{source_prefix}/{SOURCES_INDEX}"] = sources_raw
                index_files[f"{source_prefix}/{SOURCES_GZ}"] = sources_gz

        # -- 3. Release file with checksums ----------------------------------
        release_bytes = self._release_gen.generate_release(
            distribution=distribution,
            components=components,
            architectures=architectures,
            index_files=index_files,
            origin=origin,
            label=label,
        )
        dist_prefix = f"{DISTS_PREFIX}/{distribution}"
        metadata[f"{dist_prefix}/{RELEASE_FILE}"] = release_bytes

        # -- 4. InRelease (inline-signed) ------------------------------------
        inrelease_bytes = self._inrelease_gen.generate_inrelease(release_bytes)
        metadata[f"{dist_prefix}/{INRELEASE_FILE}"] = inrelease_bytes

        # -- 5. Release.gpg (detached signature) -----------------------------
        detached_sig = self._inrelease_gen.generate_detached_signature(
            release_bytes
        )
        if detached_sig is not None:
            metadata[f"{dist_prefix}/{RELEASE_GPG}"] = detached_sig

        self.logger.info(
            "Generated %d metadata files for distribution '%s'",
            len(metadata),
            distribution,
        )
        return metadata

    def get_metadata_paths(
        self,
        distribution: str,
        components: list[str],
        architectures: list[str],
    ) -> list[str]:
        """Return all expected metadata file paths for a distribution.

        Useful for validation, cleanup, and cache invalidation.

        Args:
            distribution: Distribution name.
            components: Component names.
            architectures: Architecture names.

        Returns:
            Sorted list of relative path strings.
        """
        paths: list[str] = []
        dist_prefix = f"{DISTS_PREFIX}/{distribution}"

        for comp in components:
            for arch in architectures:
                prefix = f"{dist_prefix}/{comp}/binary-{arch}"
                paths.append(f"{prefix}/{PACKAGES_INDEX}")
                paths.append(f"{prefix}/{PACKAGES_GZ}")
                paths.append(f"{prefix}/{PACKAGES_BZ2}")

            # Sources
            source_prefix = f"{dist_prefix}/{comp}/source"
            paths.append(f"{source_prefix}/{SOURCES_INDEX}")
            paths.append(f"{source_prefix}/{SOURCES_GZ}")

        # Distribution-level files
        paths.append(f"{dist_prefix}/{RELEASE_FILE}")
        paths.append(f"{dist_prefix}/{INRELEASE_FILE}")
        paths.append(f"{dist_prefix}/{RELEASE_GPG}")

        return sorted(paths)


# ===========================================================================
# Utility Functions
# ===========================================================================


def parse_deb_control(control_data: bytes) -> dict:
    """Parse a Debian control file into a key-value dictionary.

    Handles multi-line values where continuation lines begin with
    whitespace (space or tab).  Per Debian Policy section 5.1, continuation
    lines are appended to the current field value, separated by a newline.

    Args:
        control_data: Raw bytes of the control file content (UTF-8 or
            latin-1 encoded).

    Returns:
        Dictionary mapping field names to their (possibly multi-line)
        string values.  Values have leading/trailing whitespace stripped
        from the *first* line only; continuation lines preserve their
        indentation to maintain formatting (e.g. for ``Description``).
    """
    result: dict[str, str] = {}
    current_key: str | None = None
    current_value_lines: list[str] = []

    try:
        text = control_data.decode("utf-8")
    except UnicodeDecodeError:
        text = control_data.decode("latin-1")

    for line in text.splitlines():
        if not line or line.isspace():
            # Blank line signals end of a stanza in multi-stanza files.
            # We only parse the first stanza (the control file itself).
            if current_key is not None:
                result[current_key] = "\n".join(current_value_lines)
                current_key = None
                current_value_lines = []
            continue

        if line[0] in (" ", "\t"):
            # Continuation line for the current field.
            if current_key is not None:
                current_value_lines.append(line)
            continue

        # New field line: ``Key: Value``
        if current_key is not None:
            result[current_key] = "\n".join(current_value_lines)

        colon_idx = line.find(":")
        if colon_idx == -1:
            # Malformed line — treat as continuation of previous field.
            if current_key is not None:
                current_value_lines.append(line)
            continue

        current_key = line[:colon_idx].strip()
        current_value_lines = [line[colon_idx + 1 :].strip()]

    # Flush the last field.
    if current_key is not None:
        result[current_key] = "\n".join(current_value_lines)

    return result


def format_rfc2822_date(dt: datetime | None = None) -> str:
    """Format a *datetime* as RFC 2822, as required by APT Release files.

    If *dt* is ``None`` the current UTC time is used.

    Args:
        dt: An optional timezone-aware datetime.  If naive, UTC is assumed.

    Returns:
        RFC 2822 formatted date string, e.g.
        ``'Tue, 25 Feb 2025 14:30:00 UTC'``.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.strftime("%a, %d %b %Y %H:%M:%S UTC")


def compute_file_hashes(data: bytes) -> dict[str, str]:
    """Compute MD5, SHA-1, and SHA-256 hex digests for *data*.

    Args:
        data: Raw bytes to hash.

    Returns:
        Dictionary with keys ``'md5sum'``, ``'sha1'``, ``'sha256'``
        containing lowercase hex digest strings.
    """
    return {
        "md5sum": hashlib.md5(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
