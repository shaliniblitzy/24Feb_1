"""
Certificate Storage and Retrieval Module.

Implements certificate management aspects of Feature F-302 (SSL/TLS Support).
Replaces Java's java.security.KeyStore (JKS/PKCS12), BouncyCastle 1.78.1
certificate handling, and Java truststore management from the source system.

Uses cryptography 44.0.0 library for certificate parsing, validation, and
format conversion.

This module manages:
- Trusted CA certificates for proxy repository remote connections
- Client certificates for mutual TLS authentication
- Certificate import/export in PEM, DER, and PKCS12 formats
- Certificate expiration monitoring and alerting
- Certificate metadata extraction and indexing

Thread Safety:
    All mutable state access is protected by a threading.Lock to ensure safe
    concurrent access from multiple threads (e.g., Flask request handlers and
    background scheduler tasks).
"""

import os
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtensionOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    PrivateFormat,
    NoEncryption,
    BestAvailableEncryption,
)
from cryptography.hazmat.primitives.serialization.pkcs12 import (
    load_key_and_certificates,
    serialize_key_and_certificates,
)
from cryptography.hazmat.backends import default_backend

from src.app.security.crypto_utils import compute_hash
from src.app.utils.file_utils import (
    atomic_write,
    ensure_directory,
    sanitize_path,
    list_files,
)

# ---------------------------------------------------------------------------
# Module-level logger — structured logging for all certificate operations.
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Supported certificate formats
CERT_FORMAT_PEM: str = "PEM"
CERT_FORMAT_DER: str = "DER"
CERT_FORMAT_PKCS12: str = "PKCS12"

# Default certificate store directory
DEFAULT_CERT_STORE_DIR: str = "data/certificates"

# Certificate categories
CERT_CATEGORY_TRUSTED_CA: str = "trusted_ca"
CERT_CATEGORY_CLIENT: str = "client"
CERT_CATEGORY_SERVER: str = "server"

# File extensions by format
FORMAT_EXTENSIONS: dict[str, str] = {
    CERT_FORMAT_PEM: ".pem",
    CERT_FORMAT_DER: ".der",
    CERT_FORMAT_PKCS12: ".p12",
}

# Expiration warning thresholds (days)
EXPIRY_WARNING_DAYS: int = 30
EXPIRY_CRITICAL_DAYS: int = 7

# ---------------------------------------------------------------------------
# Internal Constants
# ---------------------------------------------------------------------------

# Reverse map: file extension → certificate format for auto-detection in
# import_from_file.  Supports common extensions for each format family.
_EXTENSION_TO_FORMAT: dict[str, str] = {
    ".pem": CERT_FORMAT_PEM,
    ".crt": CERT_FORMAT_PEM,
    ".cer": CERT_FORMAT_PEM,
    ".der": CERT_FORMAT_DER,
    ".p12": CERT_FORMAT_PKCS12,
    ".pfx": CERT_FORMAT_PKCS12,
}


# ---------------------------------------------------------------------------
# CertificateInfo Dataclass
# ---------------------------------------------------------------------------


@dataclass
class CertificateInfo:
    """Metadata extracted from an X.509 certificate.

    Represents the essential properties of a stored certificate, including
    identity information, validity window, fingerprints, and classification
    metadata.  Designed for JSON serialization via ``to_dict()`` and used by
    REST API endpoints, health monitoring, and certificate management
    operations throughout the application.

    Attributes:
        alias: Unique alias/identifier for this certificate within the store.
        subject: Subject common name (CN) extracted from the certificate.
        issuer: Issuer common name (CN) extracted from the certificate.
        serial_number: Certificate serial number as a hexadecimal string.
        not_before: Start of certificate validity period (UTC).
        not_after: End of certificate validity period (UTC).
        fingerprint_sha256: SHA-256 fingerprint of the DER-encoded cert (hex).
        fingerprint_sha1: SHA-1 fingerprint of the DER-encoded cert (hex).
        is_ca: True if the certificate has BasicConstraints CA=True.
        is_self_signed: True if subject and issuer names are identical.
        subject_alternative_names: DNS names, IPs, etc. from the SAN extension.
        key_type: Public key algorithm identifier ("RSA", "EC", etc.).
        key_size: Public key size in bits (e.g. 2048, 4096, 256).
        category: Classification — trusted_ca, client, or server.
    """

    alias: str
    subject: str
    issuer: str
    serial_number: str
    not_before: datetime
    not_after: datetime
    fingerprint_sha256: str
    fingerprint_sha1: str
    is_ca: bool = False
    is_self_signed: bool = False
    subject_alternative_names: list[str] = field(default_factory=list)
    key_type: str = ""
    key_size: int = 0
    category: str = CERT_CATEGORY_TRUSTED_CA

    # ---- Computed properties ------------------------------------------------

    @property
    def days_until_expiry(self) -> int:
        """Return number of days until the certificate expires.

        Negative values indicate the certificate has already expired by
        that many days.  The computation is always relative to the current
        UTC time.
        """
        now = datetime.now(timezone.utc)
        not_after_utc = self.not_after
        # Ensure timezone-aware comparison
        if not_after_utc.tzinfo is None:
            not_after_utc = not_after_utc.replace(tzinfo=timezone.utc)
        delta = not_after_utc - now
        return delta.days

    @property
    def is_expired(self) -> bool:
        """Return ``True`` if the certificate's validity period has passed."""
        now = datetime.now(timezone.utc)
        not_after_utc = self.not_after
        if not_after_utc.tzinfo is None:
            not_after_utc = not_after_utc.replace(tzinfo=timezone.utc)
        return now > not_after_utc

    @property
    def expiry_status(self) -> str:
        """Return the expiration status string.

        Returns one of:
        - ``"ok"``      — More than EXPIRY_WARNING_DAYS until expiry.
        - ``"warning"`` — Between EXPIRY_CRITICAL_DAYS and EXPIRY_WARNING_DAYS.
        - ``"critical"``— Fewer than EXPIRY_CRITICAL_DAYS remaining.
        - ``"expired"`` — Certificate has already expired.
        """
        if self.is_expired:
            return "expired"
        days = self.days_until_expiry
        if days <= EXPIRY_CRITICAL_DAYS:
            return "critical"
        if days <= EXPIRY_WARNING_DAYS:
            return "warning"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        """Serialize certificate info to a JSON-compatible dictionary.

        Datetime fields are formatted as ISO 8601 strings.  Computed
        properties (``days_until_expiry``, ``is_expired``, ``expiry_status``)
        are included as snapshot values.
        """
        return {
            "alias": self.alias,
            "subject": self.subject,
            "issuer": self.issuer,
            "serial_number": self.serial_number,
            "not_before": (
                self.not_before.isoformat() if self.not_before else None
            ),
            "not_after": (
                self.not_after.isoformat() if self.not_after else None
            ),
            "fingerprint_sha256": self.fingerprint_sha256,
            "fingerprint_sha1": self.fingerprint_sha1,
            "is_ca": self.is_ca,
            "is_self_signed": self.is_self_signed,
            "subject_alternative_names": list(self.subject_alternative_names),
            "key_type": self.key_type,
            "key_size": self.key_size,
            "category": self.category,
            "days_until_expiry": self.days_until_expiry,
            "is_expired": self.is_expired,
            "expiry_status": self.expiry_status,
        }


# ---------------------------------------------------------------------------
# CertificateStore
# ---------------------------------------------------------------------------


class CertificateStore:
    """Certificate storage and management for SSL/TLS operations.

    Provides a complete certificate management system that replaces Java's
    ``java.security.KeyStore``.  Supports import/export in PEM, DER, and
    PKCS12 formats, certificate expiration monitoring, and categorised
    storage for trusted CAs, client certificates, and server certificates.

    Thread Safety:
        All operations that read or modify the certificate index, PEM cache,
        or private-key storage are protected by a ``threading.Lock``.

    Attributes:
        store_dir: Path to the on-disk certificate store directory.
    """

    def __init__(self, store_dir: str | Path | None = None) -> None:
        """Initialise the certificate store.

        Args:
            store_dir: Directory path for certificate file persistence.
                       Defaults to ``DEFAULT_CERT_STORE_DIR``.  The directory
                       is created (with parents) if it does not exist.
        """
        if store_dir is None:
            self._store_dir = Path(DEFAULT_CERT_STORE_DIR)
        else:
            self._store_dir = Path(store_dir)

        # Thread lock for safe concurrent access
        self._lock = threading.Lock()

        # Internal certificate index: alias → CertificateInfo
        self._index: dict[str, CertificateInfo] = {}

        # Raw PEM-bytes cache: alias → PEM bytes
        self._pem_cache: dict[str, bytes] = {}

        # Private-key storage: alias → PEM bytes (for client certs with keys)
        self._private_keys: dict[str, bytes] = {}

        # Ensure the store directory exists on disk
        ensure_directory(self._store_dir)

        # Load existing certificates from the store directory
        self._load_from_disk()

        logger.info(
            "Certificate store initialised: dir=%s, certificates=%d",
            self._store_dir,
            len(self._index),
        )

    # ------------------------------------------------------------------
    # Certificate Import Methods
    # ------------------------------------------------------------------

    def import_pem(
        self,
        pem_data: bytes,
        alias: str | None = None,
        category: str = CERT_CATEGORY_TRUSTED_CA,
    ) -> CertificateInfo:
        """Import a PEM-encoded X.509 certificate into the store.

        Parses the certificate, extracts metadata, assigns an alias, persists
        the PEM bytes to disk using atomic writes, and indexes the metadata
        for later retrieval.

        Args:
            pem_data: PEM-encoded certificate bytes.
            alias: Unique alias for the certificate.  If ``None``, an alias
                   is auto-generated from the subject common name.
            category: Certificate category — one of ``CERT_CATEGORY_TRUSTED_CA``,
                      ``CERT_CATEGORY_CLIENT``, or ``CERT_CATEGORY_SERVER``.

        Returns:
            ``CertificateInfo`` with the extracted metadata.

        Raises:
            ValueError: If the PEM data cannot be parsed as an X.509
                        certificate.
        """
        try:
            cert = x509.load_pem_x509_certificate(pem_data, default_backend())
        except Exception as exc:
            raise ValueError(
                f"Failed to parse PEM certificate: {exc}"
            ) from exc

        # Use compute_hash for deduplication check on raw certificate bytes
        cert_data_hash = compute_hash(pem_data, "sha256")
        logger.debug("Certificate data hash (SHA-256): %s", cert_data_hash)

        # Extract metadata (no lock needed — pure computation)
        info = self._extract_certificate_info(cert, alias or "", category)

        with self._lock:
            # Auto-generate alias inside the lock to guarantee uniqueness
            if not alias:
                alias = self._generate_alias_unlocked(info.subject)

            # Rebuild info with the finalised alias
            info = CertificateInfo(
                alias=alias,
                subject=info.subject,
                issuer=info.issuer,
                serial_number=info.serial_number,
                not_before=info.not_before,
                not_after=info.not_after,
                fingerprint_sha256=info.fingerprint_sha256,
                fingerprint_sha1=info.fingerprint_sha1,
                is_ca=info.is_ca,
                is_self_signed=info.is_self_signed,
                subject_alternative_names=info.subject_alternative_names,
                key_type=info.key_type,
                key_size=info.key_size,
                category=category,
            )

            # Store in memory
            self._index[alias] = info
            self._pem_cache[alias] = pem_data

        # Persist to disk (outside the lock to avoid holding it during I/O)
        self._save_to_disk(alias, pem_data)

        logger.info(
            "Imported PEM certificate: alias=%s, subject=%s, category=%s",
            alias,
            info.subject,
            category,
        )
        return info

    def import_der(
        self,
        der_data: bytes,
        alias: str | None = None,
        category: str = CERT_CATEGORY_TRUSTED_CA,
    ) -> CertificateInfo:
        """Import a DER-encoded X.509 certificate into the store.

        The certificate is converted to PEM for internal storage and then
        delegated to :meth:`import_pem`.

        Args:
            der_data: DER-encoded certificate bytes.
            alias: Unique alias.  Auto-generated from subject CN if ``None``.
            category: Certificate category.

        Returns:
            ``CertificateInfo`` with the extracted metadata.

        Raises:
            ValueError: If the DER data cannot be parsed.
        """
        try:
            cert = x509.load_der_x509_certificate(der_data, default_backend())
        except Exception as exc:
            raise ValueError(
                f"Failed to parse DER certificate: {exc}"
            ) from exc

        # Convert to PEM for internal storage
        pem_data = cert.public_bytes(Encoding.PEM)
        return self.import_pem(pem_data, alias=alias, category=category)

    def import_pkcs12(
        self,
        pkcs12_data: bytes,
        password: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        """Import a PKCS12 (.p12/.pfx) file containing key + cert + chain.

        Extracts the private key, primary certificate, and any additional CA
        certificates from the PKCS12 bundle.  Each certificate is stored
        individually with appropriate categories.

        Args:
            pkcs12_data: PKCS12 binary data.
            password: Password for the PKCS12 file.  ``None`` if unprotected.
            alias: Alias for the primary certificate.  Auto-generated if
                   ``None``.

        Returns:
            Dictionary with keys:
                - ``"certificate"``: ``CertificateInfo`` for the primary cert.
                - ``"private_key_present"``: ``bool`` — whether a key was found.
                - ``"chain_certificates"``: ``list[CertificateInfo]`` for CA
                  chain certificates.

        Raises:
            ValueError: If the PKCS12 data cannot be parsed.
        """
        password_bytes = password.encode("utf-8") if password else None

        try:
            private_key, certificate, additional_certs = (
                load_key_and_certificates(
                    pkcs12_data, password_bytes, default_backend()
                )
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to parse PKCS12 data: {exc}"
            ) from exc

        result: dict[str, Any] = {
            "certificate": None,
            "private_key_present": private_key is not None,
            "chain_certificates": [],
        }

        # Import the primary certificate
        if certificate is not None:
            pem_data = certificate.public_bytes(Encoding.PEM)
            # Client category if a private key accompanies it
            cat = (
                CERT_CATEGORY_CLIENT
                if private_key is not None
                else CERT_CATEGORY_TRUSTED_CA
            )
            cert_info = self.import_pem(pem_data, alias=alias, category=cat)
            result["certificate"] = cert_info

            # Store the private key alongside the certificate
            if private_key is not None:
                key_pem = private_key.private_bytes(
                    encoding=Encoding.PEM,
                    format=PrivateFormat.PKCS8,
                    encryption_algorithm=NoEncryption(),
                )
                with self._lock:
                    self._private_keys[cert_info.alias] = key_pem
                logger.info(
                    "Stored private key for certificate: alias=%s",
                    cert_info.alias,
                )

        # Import additional CA certificates in the chain
        if additional_certs:
            for idx, chain_cert in enumerate(additional_certs):
                chain_pem = chain_cert.public_bytes(Encoding.PEM)
                chain_alias_base = alias or "chain"
                chain_alias = f"{chain_alias_base}_ca_{idx}"
                chain_info = self.import_pem(
                    chain_pem,
                    alias=chain_alias,
                    category=CERT_CATEGORY_TRUSTED_CA,
                )
                result["chain_certificates"].append(chain_info)

        logger.info(
            "Imported PKCS12 bundle: primary=%s, private_key=%s, "
            "chain_certs=%d",
            (
                result["certificate"].alias
                if result["certificate"]
                else "none"
            ),
            result["private_key_present"],
            len(result["chain_certificates"]),
        )
        return result

    def import_from_file(
        self,
        file_path: str | Path,
        password: str | None = None,
        alias: str | None = None,
    ) -> CertificateInfo | dict[str, Any]:
        """Import a certificate from a file, auto-detecting the format.

        Format detection is based on file extension:
            - ``.pem``, ``.crt``, ``.cer`` → PEM format
            - ``.der`` → DER format
            - ``.p12``, ``.pfx`` → PKCS12 format

        Args:
            file_path: Path to the certificate file.
            password: Password for PKCS12 files.  Ignored for PEM/DER.
            alias: Alias for the certificate.  Auto-generated if ``None``.

        Returns:
            ``CertificateInfo`` for PEM/DER imports, or a ``dict`` for
            PKCS12 imports.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the format cannot be determined or the file data
                        is invalid.
        """
        path = Path(file_path).resolve()

        if not path.is_file():
            raise FileNotFoundError(
                f"Certificate file not found: {path}"
            )

        suffix = path.suffix.lower()
        cert_format = _EXTENSION_TO_FORMAT.get(suffix)

        if cert_format is None:
            raise ValueError(
                f"Unsupported certificate file extension: {suffix!r}. "
                f"Supported: {sorted(_EXTENSION_TO_FORMAT.keys())}"
            )

        file_data = path.read_bytes()

        if cert_format == CERT_FORMAT_PEM:
            return self.import_pem(file_data, alias=alias)
        elif cert_format == CERT_FORMAT_DER:
            return self.import_der(file_data, alias=alias)
        elif cert_format == CERT_FORMAT_PKCS12:
            return self.import_pkcs12(
                file_data, password=password, alias=alias
            )
        else:
            raise ValueError(
                f"Unhandled certificate format: {cert_format}"
            )

    # ------------------------------------------------------------------
    # Certificate Export Methods
    # ------------------------------------------------------------------

    def export_pem(self, alias: str) -> bytes:
        """Export a certificate in PEM format.

        Args:
            alias: Certificate alias to export.

        Returns:
            PEM-encoded certificate bytes.

        Raises:
            KeyError: If no certificate exists with the given alias.
        """
        with self._lock:
            pem_data = self._pem_cache.get(alias)

        if pem_data is None:
            raise KeyError(f"Certificate not found: {alias!r}")

        return pem_data

    def export_der(self, alias: str) -> bytes:
        """Export a certificate in DER format.

        The stored PEM is parsed and re-encoded as DER.

        Args:
            alias: Certificate alias to export.

        Returns:
            DER-encoded certificate bytes.

        Raises:
            KeyError: If no certificate exists with the given alias.
        """
        pem_data = self.export_pem(alias)
        cert = x509.load_pem_x509_certificate(pem_data, default_backend())
        return cert.public_bytes(Encoding.DER)

    def export_pkcs12(
        self,
        alias: str,
        private_key_pem: bytes | None = None,
        password: str | None = None,
    ) -> bytes:
        """Export a certificate (and optionally private key) as PKCS12.

        If *private_key_pem* is ``None``, the stored private key (imported
        via :meth:`import_pkcs12`) is used when available.

        Args:
            alias: Certificate alias to export.
            private_key_pem: Optional PEM-encoded private key to include.
            password: Password to protect the PKCS12 output.  ``None`` for
                      unencrypted output.

        Returns:
            PKCS12 binary data.

        Raises:
            KeyError: If no certificate exists with the given alias.
        """
        pem_data = self.export_pem(alias)
        cert = x509.load_pem_x509_certificate(pem_data, default_backend())

        # Resolve private key — explicit argument first, then stored key
        key = None
        if private_key_pem is not None:
            key = serialization.load_pem_private_key(
                private_key_pem, password=None, backend=default_backend()
            )
        else:
            with self._lock:
                stored_key_pem = self._private_keys.get(alias)
            if stored_key_pem is not None:
                key = serialization.load_pem_private_key(
                    stored_key_pem, password=None, backend=default_backend()
                )

        # Determine encryption algorithm
        encryption: serialization.KeySerializationEncryption
        if password:
            encryption = BestAvailableEncryption(password.encode("utf-8"))
        else:
            encryption = NoEncryption()

        return serialize_key_and_certificates(
            name=alias.encode("utf-8"),
            key=key,
            cert=cert,
            cas=None,
            encryption_algorithm=encryption,
        )

    def export_to_file(
        self,
        alias: str,
        file_path: str | Path,
        format: str = CERT_FORMAT_PEM,
        password: str | None = None,
    ) -> None:
        """Export a certificate to a file in the specified format.

        The file is written using atomic write (temp + rename) for data
        integrity.

        Args:
            alias: Certificate alias to export.
            file_path: Destination file path.
            format: Output format — ``CERT_FORMAT_PEM``, ``CERT_FORMAT_DER``,
                    or ``CERT_FORMAT_PKCS12``.
            password: Password for PKCS12 format.  Ignored for PEM/DER.

        Raises:
            KeyError: If no certificate exists with the given alias.
            ValueError: If the format is not supported.
        """
        if format == CERT_FORMAT_PEM:
            data = self.export_pem(alias)
        elif format == CERT_FORMAT_DER:
            data = self.export_der(alias)
        elif format == CERT_FORMAT_PKCS12:
            data = self.export_pkcs12(alias, password=password)
        else:
            raise ValueError(
                f"Unsupported export format: {format!r}. "
                f"Supported: {list(FORMAT_EXTENSIONS.keys())}"
            )

        atomic_write(file_path, data)
        logger.info(
            "Exported certificate: alias=%s, format=%s, path=%s",
            alias,
            format,
            file_path,
        )

    # ------------------------------------------------------------------
    # Certificate Retrieval and Query
    # ------------------------------------------------------------------

    def get_certificate(self, alias: str) -> CertificateInfo | None:
        """Retrieve certificate metadata by alias.

        Args:
            alias: Certificate alias to look up.

        Returns:
            ``CertificateInfo`` if found, ``None`` otherwise.
        """
        with self._lock:
            return self._index.get(alias)

    def get_certificate_pem(self, alias: str) -> bytes | None:
        """Retrieve raw PEM bytes for a certificate by alias.

        Args:
            alias: Certificate alias.

        Returns:
            PEM-encoded bytes if found, ``None`` otherwise.
        """
        with self._lock:
            return self._pem_cache.get(alias)

    def list_certificates(
        self, category: str | None = None
    ) -> list[CertificateInfo]:
        """List all certificates, optionally filtered by category.

        Args:
            category: If provided, only return certificates matching this
                      category (e.g. ``trusted_ca``, ``client``, ``server``).

        Returns:
            Sorted list of ``CertificateInfo`` objects (sorted by alias).
        """
        with self._lock:
            certs = list(self._index.values())

        if category is not None:
            certs = [c for c in certs if c.category == category]

        return sorted(certs, key=lambda c: c.alias)

    def get_trusted_ca_certificates(self) -> list[bytes]:
        """Return PEM-encoded bytes for all trusted CA certificates.

        Used by SSLManager to build trust stores for proxy repository
        remote connections.

        Returns:
            List of PEM-encoded certificate bytes for trusted CAs.
        """
        with self._lock:
            return [
                self._pem_cache[alias]
                for alias, info in self._index.items()
                if info.category == CERT_CATEGORY_TRUSTED_CA
                and alias in self._pem_cache
            ]

    def get_client_certificate(
        self, alias: str
    ) -> tuple[bytes, bytes] | None:
        """Retrieve a client certificate and its private key.

        Used for mutual TLS connections to proxy repository remote
        registries.

        Args:
            alias: Certificate alias.

        Returns:
            Tuple of ``(certificate_pem, private_key_pem)`` if both exist,
            ``None`` if either is missing.
        """
        with self._lock:
            cert_pem = self._pem_cache.get(alias)
            key_pem = self._private_keys.get(alias)

        if cert_pem is None or key_pem is None:
            return None

        return (cert_pem, key_pem)

    def find_by_fingerprint(
        self, fingerprint: str
    ) -> CertificateInfo | None:
        """Find a certificate by its SHA-256 fingerprint.

        Useful for deduplication checks and fingerprint-based lookups.

        Args:
            fingerprint: SHA-256 fingerprint hex string (case-insensitive).

        Returns:
            ``CertificateInfo`` if found, ``None`` otherwise.
        """
        fp_lower = fingerprint.lower()
        with self._lock:
            for info in self._index.values():
                if info.fingerprint_sha256.lower() == fp_lower:
                    return info
        return None

    # ------------------------------------------------------------------
    # Certificate Removal
    # ------------------------------------------------------------------

    def remove_certificate(self, alias: str) -> bool:
        """Remove a certificate from the store.

        Removes the certificate from the in-memory index, PEM cache, and
        private-key storage, then deletes the corresponding file from disk.

        Args:
            alias: Certificate alias to remove.

        Returns:
            ``True`` if the certificate was found and removed, ``False``
            if it was not found.
        """
        with self._lock:
            if alias not in self._index:
                return False
            del self._index[alias]
            self._pem_cache.pop(alias, None)
            self._private_keys.pop(alias, None)

        # Delete from disk
        safe_alias = sanitize_path(alias) or alias
        cert_file = self._store_dir / f"{safe_alias}.pem"
        try:
            if os.path.exists(str(cert_file)):
                os.unlink(str(cert_file))
        except OSError as exc:
            logger.warning(
                "Failed to delete certificate file %s: %s",
                cert_file,
                exc,
            )

        logger.info("Removed certificate: alias=%s", alias)
        return True

    # ------------------------------------------------------------------
    # Certificate Expiration Monitoring
    # ------------------------------------------------------------------

    def check_expirations(
        self,
        warning_days: int = EXPIRY_WARNING_DAYS,
        critical_days: int = EXPIRY_CRITICAL_DAYS,
    ) -> dict[str, Any]:
        """Check expiration status of all certificates in the store.

        Produces a summary report used by health check monitoring (F-401)
        and scheduled certificate check tasks (F-402).

        Args:
            warning_days: Days threshold for ``"warning"`` status.
            critical_days: Days threshold for ``"critical"`` status.

        Returns:
            Dictionary containing:
                - ``total``: Total certificate count.
                - ``ok``: Count of certificates with OK status.
                - ``warning``: Count nearing expiration.
                - ``critical``: Count critically close to expiration.
                - ``expired``: Count of already-expired certificates.
                - ``certificates``: Sorted list of per-certificate status
                  dicts (most urgent first).
        """
        with self._lock:
            certs = list(self._index.values())

        counts: dict[str, int] = {
            "ok": 0,
            "warning": 0,
            "critical": 0,
            "expired": 0,
        }
        cert_details: list[dict[str, Any]] = []

        for info in certs:
            days = info.days_until_expiry

            if info.is_expired:
                status = "expired"
            elif days <= critical_days:
                status = "critical"
            elif days <= warning_days:
                status = "warning"
            else:
                status = "ok"

            counts[status] += 1

            cert_details.append({
                "alias": info.alias,
                "subject": info.subject,
                "not_after": (
                    info.not_after.isoformat() if info.not_after else ""
                ),
                "days_until_expiry": days,
                "status": status,
            })

            # Log warnings for certificates nearing expiration
            if status == "warning":
                logger.warning(
                    "Certificate expiring soon: alias=%s, subject=%s, "
                    "days_until_expiry=%d",
                    info.alias,
                    info.subject,
                    days,
                )
            elif status == "critical":
                logger.warning(
                    "Certificate critically near expiration: alias=%s, "
                    "subject=%s, days_until_expiry=%d",
                    info.alias,
                    info.subject,
                    days,
                )
            elif status == "expired":
                logger.warning(
                    "Certificate has expired: alias=%s, subject=%s",
                    info.alias,
                    info.subject,
                )

        # Sort by urgency — most urgent (lowest days) first
        cert_details.sort(key=lambda c: c["days_until_expiry"])

        return {
            "total": len(certs),
            "ok": counts["ok"],
            "warning": counts["warning"],
            "critical": counts["critical"],
            "expired": counts["expired"],
            "certificates": cert_details,
        }

    # ------------------------------------------------------------------
    # Internal Helper Methods
    # ------------------------------------------------------------------

    def _extract_certificate_info(
        self,
        cert: x509.Certificate,
        alias: str,
        category: str,
    ) -> CertificateInfo:
        """Extract metadata from a ``cryptography`` x509.Certificate object.

        Handles missing attributes gracefully — certificates may lack a CN,
        SANs, or BasicConstraints extension.

        Args:
            cert: Parsed ``x509.Certificate`` object.
            alias: Alias for the certificate (may be empty if auto-generated
                   later).
            category: Certificate category classification.

        Returns:
            Populated ``CertificateInfo`` dataclass.
        """
        # Subject Common Name
        subject_cn = ""
        try:
            cn_attrs = cert.subject.get_attributes_for_oid(
                NameOID.COMMON_NAME
            )
            if cn_attrs:
                subject_cn = str(cn_attrs[0].value)
        except Exception:
            pass

        # Issuer Common Name
        issuer_cn = ""
        try:
            cn_attrs = cert.issuer.get_attributes_for_oid(
                NameOID.COMMON_NAME
            )
            if cn_attrs:
                issuer_cn = str(cn_attrs[0].value)
        except Exception:
            pass

        # Serial number (hex)
        serial_hex = hex(cert.serial_number)

        # Validity dates (UTC-aware)
        not_before = cert.not_valid_before_utc
        not_after = cert.not_valid_after_utc

        # Fingerprints
        fingerprint_sha256 = cert.fingerprint(hashes.SHA256()).hex()
        fingerprint_sha1 = cert.fingerprint(hashes.SHA1()).hex()

        # BasicConstraints — is CA?
        is_ca = False
        try:
            bc_ext = cert.extensions.get_extension_for_oid(
                ExtensionOID.BASIC_CONSTRAINTS
            )
            is_ca = bc_ext.value.ca
        except x509.ExtensionNotFound:
            pass
        except Exception:
            pass

        # Self-signed detection (subject == issuer)
        is_self_signed = cert.subject == cert.issuer

        # Subject Alternative Names
        sans: list[str] = []
        try:
            san_ext = cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME
            )
            for general_name in san_ext.value:
                sans.append(str(general_name.value))
        except x509.ExtensionNotFound:
            pass
        except Exception:
            pass

        # Public key type and size
        key_type = ""
        key_size = 0
        try:
            pub_key = cert.public_key()
            from cryptography.hazmat.primitives.asymmetric import (
                rsa as rsa_mod,
                ec as ec_mod,
                dsa as dsa_mod,
                ed25519 as ed25519_mod,
                ed448 as ed448_mod,
            )

            if isinstance(pub_key, rsa_mod.RSAPublicKey):
                key_type = "RSA"
                key_size = pub_key.key_size
            elif isinstance(pub_key, ec_mod.EllipticCurvePublicKey):
                key_type = "EC"
                key_size = pub_key.key_size
            elif isinstance(pub_key, dsa_mod.DSAPublicKey):
                key_type = "DSA"
                key_size = pub_key.key_size
            elif isinstance(pub_key, ed25519_mod.Ed25519PublicKey):
                key_type = "Ed25519"
                key_size = 256
            elif isinstance(pub_key, ed448_mod.Ed448PublicKey):
                key_type = "Ed448"
                key_size = 448
            else:
                key_type = type(pub_key).__name__
        except Exception:
            pass

        return CertificateInfo(
            alias=alias,
            subject=subject_cn,
            issuer=issuer_cn,
            serial_number=serial_hex,
            not_before=not_before,
            not_after=not_after,
            fingerprint_sha256=fingerprint_sha256,
            fingerprint_sha1=fingerprint_sha1,
            is_ca=is_ca,
            is_self_signed=is_self_signed,
            subject_alternative_names=sans,
            key_type=key_type,
            key_size=key_size,
            category=category,
        )

    def _load_from_disk(self) -> None:
        """Scan the store directory and load existing certificate files.

        Loads all ``.pem`` files found in the store directory.  Corrupt or
        unreadable files are logged as warnings and skipped.  Called once
        during ``__init__``.
        """
        cert_files = list_files(self._store_dir, pattern="*.pem")

        for cert_file in cert_files:
            try:
                pem_data = cert_file.read_bytes()
                cert = x509.load_pem_x509_certificate(
                    pem_data, default_backend()
                )

                # Derive alias from filename (strip .pem extension)
                alias = cert_file.stem

                info = self._extract_certificate_info(
                    cert, alias, CERT_CATEGORY_TRUSTED_CA
                )

                self._index[alias] = info
                self._pem_cache[alias] = pem_data

            except Exception as exc:
                logger.warning(
                    "Failed to load certificate from %s: %s",
                    cert_file,
                    exc,
                )

    def _save_to_disk(self, alias: str, pem_data: bytes) -> None:
        """Save a PEM certificate to the store directory using atomic write.

        The filename is derived from the alias after sanitisation.

        Args:
            alias: Certificate alias (used for filename).
            pem_data: PEM-encoded certificate bytes.

        Raises:
            OSError: If the file cannot be written.
        """
        safe_alias = sanitize_path(alias) or alias
        cert_path = self._store_dir / f"{safe_alias}.pem"

        try:
            atomic_write(cert_path, pem_data)
        except OSError as exc:
            logger.error(
                "Failed to save certificate to disk: alias=%s, path=%s, "
                "error=%s",
                alias,
                cert_path,
                exc,
            )
            raise

    def _generate_alias_unlocked(self, subject_cn: str) -> str:
        """Generate a unique alias from a subject common name.

        MUST be called while ``self._lock`` is held by the caller.  This
        method reads ``self._index`` without acquiring the lock.

        Sanitises the CN for filesystem safety and ensures uniqueness by
        appending a numeric suffix if the alias already exists.

        Args:
            subject_cn: Subject common name from the certificate.

        Returns:
            Unique alias string.
        """
        base_alias = (
            subject_cn.lower()
            .replace(" ", "_")
            .replace("*", "wildcard")
        )
        base_alias = "".join(
            c for c in base_alias if c.isalnum() or c in ("_", "-", ".")
        )
        if not base_alias:
            base_alias = "certificate"

        alias = base_alias
        counter = 1
        while alias in self._index:
            alias = f"{base_alias}_{counter}"
            counter += 1

        return alias
