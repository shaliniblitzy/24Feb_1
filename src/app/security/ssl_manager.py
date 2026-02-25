"""
SSL/TLS Configuration Management Module.

Implements Feature F-302 (SSL/TLS Support) for the Nexus Repository Flask
application. Replaces Java's ``javax.net.ssl.SSLContext``, ``KeyManager``,
``TrustManager``, BouncyCastle 1.78.1 SSL configuration, and Jetty 12.0.5's
``SslContextFactory`` from the Java source system.

Uses the Python ``cryptography 44.0.0`` library for certificate parsing,
PKCS12 keystore operations, and certificate chain validation; and the Python
``ssl`` stdlib module for SSL/TLS context creation and protocol enforcement.

This module manages:
- TLS protocol version enforcement (TLS 1.2+ minimum)
- Cipher suite configuration and restriction
- Keystore/truststore management equivalent (Python SSL context)
- Certificate chain validation
- Automatic certificate renewal tracking
- SSL context creation for both server (Gunicorn) and client (proxy fetches) use

Thread Safety:
    SSLManager instances may be shared across threads safely.  SSL context
    creation methods produce new ``ssl.SSLContext`` objects on each call,
    and internal state is read-only after initialisation.

Security Notes:
- TLS 1.0 and TLS 1.1 are ALWAYS disabled (SSLv2/SSLv3 also disabled)
- Only strong, modern cipher suites with forward secrecy (ECDHE/DHE) are
  permitted by default
- Private key passwords are never logged
- Certificate verification should only be disabled in controlled test
  environments
"""

import ssl
import os
import logging
from dataclasses import dataclass, field
from typing import Any
from datetime import datetime, timezone, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization.pkcs12 import (
    load_key_and_certificates,
    serialize_key_and_certificates,
)

from src.app.security.certificate_store import CertificateStore
from src.app.security.crypto_utils import generate_rsa_key_pair

# ---------------------------------------------------------------------------
# Module-level logger — structured logging for all SSL/TLS operations.
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mapping from human-readable TLS version strings to ssl.TLSVersion enum
# members.  Used by SSLConfig.tls_version_enum and internal context creation.
_TLS_VERSION_MAP: dict[str, ssl.TLSVersion] = {
    "TLSv1.2": ssl.TLSVersion.TLSv1_2,
    "TLSv1.3": ssl.TLSVersion.TLSv1_3,
}

# Reverse mapping for display purposes (ssl.TLSVersion → string label).
_TLS_VERSION_LABEL: dict[ssl.TLSVersion, str] = {
    v: k for k, v in _TLS_VERSION_MAP.items()
}

# Default strong cipher suites — AES-GCM and ChaCha20 with forward secrecy.
# TLS 1.3 ciphers (TLS_* prefix) are handled automatically by OpenSSL 1.1.1+
# and cannot be controlled via set_ciphers(); they are listed here for
# documentation and configuration completeness.
_DEFAULT_CIPHER_SUITES: list[str] = [
    # TLS 1.3 cipher suites (auto-managed by OpenSSL 1.1.1+)
    "TLS_AES_256_GCM_SHA384",
    "TLS_CHACHA20_POLY1305_SHA256",
    "TLS_AES_128_GCM_SHA256",
    # TLS 1.2 cipher suites (managed via set_ciphers)
    "ECDHE-ECDSA-AES256-GCM-SHA384",
    "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-AES128-GCM-SHA256",
    "ECDHE-RSA-AES128-GCM-SHA256",
    "DHE-RSA-AES256-GCM-SHA384",
    "DHE-RSA-AES128-GCM-SHA256",
]


# ---------------------------------------------------------------------------
# SSLConfig Dataclass
# ---------------------------------------------------------------------------


@dataclass
class SSLConfig:
    """SSL/TLS configuration settings.

    Immutable (by convention) configuration for all SSL/TLS behaviour in the
    application.  All certificate paths accept PEM-encoded files.  The cipher
    suite list contains both TLS 1.3 (``TLS_*``) and TLS 1.2 (``ECDHE-*``,
    ``DHE-*``) cipher names.

    Attributes:
        min_tls_version: Minimum allowed TLS protocol version.  Must be
            ``"TLSv1.2"`` or ``"TLSv1.3"``.  Defaults to ``"TLSv1.2"``.
        max_tls_version: Maximum allowed TLS protocol version.  Defaults to
            ``"TLSv1.3"``.
        cert_file: Filesystem path to the server certificate (PEM-encoded).
        key_file: Filesystem path to the server private key (PEM-encoded).
        ca_cert_file: Filesystem path to the CA certificate bundle for
            verifying client certificates or upstream servers.
        client_cert_file: Path to the client certificate for mutual TLS.
        client_key_file: Path to the client private key for mutual TLS.
        key_password: Password for encrypted private keys.  ``None`` if the
            keys are not encrypted.
        cipher_suites: Ordered list of acceptable cipher suite names.
        verify_hostname: Whether to verify the hostname in certificates
            during TLS handshake.
        verify_certificates: Whether to verify peer certificates at all.
        renewal_warning_days: Days before expiry to trigger a warning alert.
        renewal_critical_days: Days before expiry to trigger a critical alert.
    """

    # TLS protocol version control
    min_tls_version: str = "TLSv1.2"
    max_tls_version: str = "TLSv1.3"

    # Server certificate paths (PEM-encoded)
    cert_file: str | None = None
    key_file: str | None = None
    ca_cert_file: str | None = None

    # Client certificate paths for mutual TLS
    client_cert_file: str | None = None
    client_key_file: str | None = None

    # Private key password
    key_password: str | None = None

    # Cipher suite configuration — strong, modern ciphers with forward secrecy
    cipher_suites: list[str] = field(
        default_factory=lambda: list(_DEFAULT_CIPHER_SUITES)
    )

    # Hostname and certificate verification flags
    verify_hostname: bool = True
    verify_certificates: bool = True

    # Certificate renewal alert thresholds (days before expiry)
    renewal_warning_days: int = 30
    renewal_critical_days: int = 7

    # ------------------------------------------------------------------
    # Computed Properties
    # ------------------------------------------------------------------

    @property
    def tls_version_enum(self) -> ssl.TLSVersion:
        """Return the ``ssl.TLSVersion`` enum for the minimum TLS version.

        Maps ``"TLSv1.2"`` → ``ssl.TLSVersion.TLSv1_2`` and
        ``"TLSv1.3"`` → ``ssl.TLSVersion.TLSv1_3``.  Falls back to
        TLS 1.2 if the configured string is unrecognised.
        """
        return _TLS_VERSION_MAP.get(
            self.min_tls_version, ssl.TLSVersion.TLSv1_2
        )

    @property
    def has_server_cert(self) -> bool:
        """Return ``True`` if both server certificate and key paths are set."""
        return self.cert_file is not None and self.key_file is not None

    @property
    def has_client_cert(self) -> bool:
        """Return ``True`` if both client certificate and key paths are set."""
        return (
            self.client_cert_file is not None
            and self.client_key_file is not None
        )


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _extract_common_name(name: x509.Name) -> str:
    """Extract the Common Name (CN) from an X.509 Name object.

    Args:
        name: An ``x509.Name`` object (subject or issuer).

    Returns:
        The CN string, or ``"unknown"`` if no CN attribute is present.
    """
    try:
        cn_attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        if cn_attrs:
            return str(cn_attrs[0].value)
    except Exception:
        pass
    return "unknown"


def _get_tls12_ciphers(cipher_suites: list[str]) -> list[str]:
    """Filter cipher suite list to only TLS 1.2 ciphers.

    TLS 1.3 cipher suites (names starting with ``TLS_``) are managed
    internally by OpenSSL 1.1.1+ and cannot be set via
    ``ssl.SSLContext.set_ciphers()``.  This helper separates TLS 1.2
    ciphers that are compatible with the ``set_ciphers()`` API.

    Args:
        cipher_suites: Full cipher suite list including TLS 1.3 entries.

    Returns:
        Filtered list containing only TLS 1.2 cipher names.
    """
    return [c for c in cipher_suites if not c.startswith("TLS_")]


# ---------------------------------------------------------------------------
# SSLManager Class
# ---------------------------------------------------------------------------


class SSLManager:
    """SSL/TLS configuration management for the Nexus Repository server.

    Provides SSL context creation for server-side (Gunicorn WSGI server)
    and client-side (outbound proxy repository fetches) TLS connections.
    Manages certificate chain validation, PKCS12 keystore operations,
    truststore updates, and certificate renewal tracking.

    Replaces:
    - Java's ``javax.net.ssl.SSLContext`` and ``SSLContextFactory``
    - Jetty 12.0.5 ``SslContextFactory`` for server-side SSL
    - ``HttpClientManagerImpl.java`` TrustManager for client-side SSL
    - BouncyCastle 1.78.1 keystore and certificate management

    Args:
        config: SSL/TLS configuration.  Uses defaults if ``None``.
        certificate_store: Optional ``CertificateStore`` for managing
            trusted CA certificates and certificate lifecycle.

    Example::

        config = SSLConfig(
            cert_file="/etc/ssl/server.crt",
            key_file="/etc/ssl/server.key",
        )
        manager = SSLManager(config)
        server_ctx = manager.create_server_ssl_context()
    """

    def __init__(
        self,
        config: SSLConfig | None = None,
        certificate_store: CertificateStore | None = None,
    ) -> None:
        """Initialise the SSL manager.

        Args:
            config: SSL/TLS configuration settings.  A default
                ``SSLConfig`` is used if ``None``.
            certificate_store: Certificate store for trusted CA
                management.  May be ``None`` if certificate store
                features are not needed.
        """
        self._config: SSLConfig = config if config is not None else SSLConfig()
        self._certificate_store: CertificateStore | None = certificate_store

        logger.info(
            "SSL manager initialised: min_tls=%s, max_tls=%s, "
            "server_cert=%s, client_cert=%s, verify=%s",
            self._config.min_tls_version,
            self._config.max_tls_version,
            self._config.has_server_cert,
            self._config.has_client_cert,
            self._config.verify_certificates,
        )

    # ------------------------------------------------------------------
    # SSL Context Creation
    # ------------------------------------------------------------------

    def create_server_ssl_context(self) -> ssl.SSLContext:
        """Create an SSL context for the Gunicorn WSGI server.

        Builds a server-side TLS context with enforced minimum TLS 1.2,
        strong cipher suites, and optional server certificate loading.
        Replaces Jetty 12.0.5 ``SslContextFactory`` configuration.

        Returns:
            Configured ``ssl.SSLContext`` suitable for server-side TLS.

        Raises:
            ssl.SSLError: If certificate files cannot be loaded or cipher
                suites are invalid.
            FileNotFoundError: If specified certificate or key files do
                not exist on disk.
        """
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

        # Enforce minimum and maximum TLS versions
        ctx.minimum_version = _TLS_VERSION_MAP.get(
            self._config.min_tls_version, ssl.TLSVersion.TLSv1_2
        )
        ctx.maximum_version = _TLS_VERSION_MAP.get(
            self._config.max_tls_version, ssl.TLSVersion.TLSv1_3
        )

        # Explicitly disable insecure protocol versions
        ctx.options |= (
            ssl.OP_NO_SSLv2
            | ssl.OP_NO_SSLv3
            | ssl.OP_NO_TLSv1
            | ssl.OP_NO_TLSv1_1
        )

        # Configure cipher suites (TLS 1.2 only — TLS 1.3 ciphers are
        # managed automatically by OpenSSL)
        tls12_ciphers = _get_tls12_ciphers(self._config.cipher_suites)
        if tls12_ciphers:
            cipher_string = ":".join(tls12_ciphers)
            try:
                ctx.set_ciphers(cipher_string)
            except ssl.SSLError as exc:
                logger.error(
                    "Failed to set cipher suites: %s (ciphers: %s)",
                    exc,
                    cipher_string,
                )
                raise

        # Load server certificate and private key
        if self._config.has_server_cert:
            cert_path = self._config.cert_file
            key_path = self._config.key_file

            if cert_path and not os.path.exists(cert_path):
                raise FileNotFoundError(
                    f"Server certificate file not found: {cert_path}"
                )
            if key_path and not os.path.exists(key_path):
                raise FileNotFoundError(
                    f"Server key file not found: {key_path}"
                )

            ctx.load_cert_chain(
                certfile=self._config.cert_file,
                keyfile=self._config.key_file,
                password=self._config.key_password,
            )
            logger.info(
                "Loaded server certificate: cert=%s, key=%s",
                self._config.cert_file,
                self._config.key_file,
            )

        # Load CA certificates for client certificate verification
        if self._config.ca_cert_file:
            if not os.path.exists(self._config.ca_cert_file):
                raise FileNotFoundError(
                    f"CA certificate file not found: {self._config.ca_cert_file}"
                )
            ctx.load_verify_locations(cafile=self._config.ca_cert_file)
            logger.info(
                "Loaded CA certificate bundle: %s",
                self._config.ca_cert_file,
            )

        logger.info(
            "Server SSL context created: min_tls=%s, max_tls=%s, "
            "ciphers=%d configured",
            self._config.min_tls_version,
            self._config.max_tls_version,
            len(tls12_ciphers),
        )
        return ctx

    def create_client_ssl_context(
        self, verify: bool | None = None
    ) -> ssl.SSLContext:
        """Create an SSL context for outbound HTTPS connections.

        Builds a client-side TLS context for proxy repository fetches
        and other outbound connections.  Supports optional certificate
        verification disable (for testing environments only) and mutual
        TLS via client certificates.

        Replaces Java's ``HttpClientManagerImpl.java`` TrustManager
        configuration.

        Args:
            verify: Whether to verify server certificates.  ``None``
                uses the value from ``SSLConfig.verify_certificates``.
                ``True`` enables full verification with system and
                custom CA certificates.  ``False`` disables verification
                (WARNING: only for testing).

        Returns:
            Configured ``ssl.SSLContext`` suitable for client-side TLS.

        Raises:
            ssl.SSLError: If certificate files cannot be loaded.
            FileNotFoundError: If specified certificate files do not
                exist on disk.
        """
        should_verify = (
            verify if verify is not None else self._config.verify_certificates
        )

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        # Enforce minimum TLS version
        ctx.minimum_version = _TLS_VERSION_MAP.get(
            self._config.min_tls_version, ssl.TLSVersion.TLSv1_2
        )
        ctx.maximum_version = _TLS_VERSION_MAP.get(
            self._config.max_tls_version, ssl.TLSVersion.TLSv1_3
        )

        # Disable insecure protocol versions
        ctx.options |= (
            ssl.OP_NO_SSLv2
            | ssl.OP_NO_SSLv3
            | ssl.OP_NO_TLSv1
            | ssl.OP_NO_TLSv1_1
        )

        # Configure cipher suites
        tls12_ciphers = _get_tls12_ciphers(self._config.cipher_suites)
        if tls12_ciphers:
            try:
                ctx.set_ciphers(":".join(tls12_ciphers))
            except ssl.SSLError as exc:
                logger.error("Failed to set client cipher suites: %s", exc)
                raise

        if should_verify:
            # Enable full certificate verification
            ctx.check_hostname = self._config.verify_hostname
            ctx.verify_mode = ssl.CERT_REQUIRED

            # Load system default CA certificates
            ctx.load_default_certs(purpose=ssl.Purpose.SERVER_AUTH)

            # Load custom CA certificate file if configured
            if self._config.ca_cert_file and os.path.exists(
                self._config.ca_cert_file
            ):
                ctx.load_verify_locations(cafile=self._config.ca_cert_file)
                logger.info(
                    "Loaded custom CA bundle for client context: %s",
                    self._config.ca_cert_file,
                )

            # Load additional trusted CA certificates from the
            # CertificateStore (for proxy repository connections to
            # registries with custom CAs)
            if self._certificate_store is not None:
                trusted_pems = (
                    self._certificate_store.get_trusted_ca_certificates()
                )
                if trusted_pems:
                    combined_pem = b"".join(trusted_pems)
                    try:
                        ctx.load_verify_locations(
                            cadata=combined_pem.decode("ascii")
                        )
                        logger.info(
                            "Loaded %d trusted CA certificate(s) from "
                            "certificate store into client context",
                            len(trusted_pems),
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to load CertificateStore CAs into "
                            "client SSL context: %s",
                            exc,
                        )
        else:
            # Disable certificate verification — testing only
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            logger.warning(
                "Certificate verification DISABLED for client SSL context. "
                "This should only be used in controlled test environments."
            )

        # Load client certificate for mutual TLS if configured
        if self._config.has_client_cert:
            client_cert = self._config.client_cert_file
            client_key = self._config.client_key_file

            if client_cert and not os.path.exists(client_cert):
                raise FileNotFoundError(
                    f"Client certificate file not found: {client_cert}"
                )
            if client_key and not os.path.exists(client_key):
                raise FileNotFoundError(
                    f"Client key file not found: {client_key}"
                )

            ctx.load_cert_chain(
                certfile=self._config.client_cert_file,
                keyfile=self._config.client_key_file,
                password=self._config.key_password,
            )
            logger.info(
                "Loaded client certificate for mutual TLS: cert=%s",
                self._config.client_cert_file,
            )

        logger.info(
            "Client SSL context created: verify=%s, hostname_check=%s",
            should_verify,
            self._config.verify_hostname if should_verify else False,
        )
        return ctx

    # ------------------------------------------------------------------
    # Certificate Chain Validation
    # ------------------------------------------------------------------

    def validate_certificate_chain(
        self,
        cert_pem: bytes,
        chain_pem: bytes | None = None,
    ) -> dict[str, Any]:
        """Validate a certificate and optionally its issuer chain.

        Parses the leaf certificate, checks its validity period, and if
        a chain is provided, validates each certificate in the chain for
        basic structural integrity and temporal validity.

        Args:
            cert_pem: PEM-encoded leaf certificate bytes.
            chain_pem: Optional PEM-encoded chain certificates (may
                contain multiple PEM blocks concatenated).

        Returns:
            Validation result dictionary::

                {
                    "valid": True,
                    "subject": "CN=example.com",
                    "issuer": "CN=Let's Encrypt Authority",
                    "not_before": "2024-01-01T00:00:00+00:00",
                    "not_after": "2025-01-01T00:00:00+00:00",
                    "days_until_expiry": 180,
                    "is_self_signed": False,
                    "errors": []
                }
        """
        errors: list[str] = []
        now = datetime.now(timezone.utc)

        # Parse the leaf certificate
        try:
            cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
        except Exception as exc:
            return {
                "valid": False,
                "subject": "unknown",
                "issuer": "unknown",
                "not_before": None,
                "not_after": None,
                "days_until_expiry": 0,
                "is_self_signed": False,
                "errors": [f"Failed to parse certificate: {exc}"],
            }

        subject_cn = _extract_common_name(cert.subject)
        issuer_cn = _extract_common_name(cert.issuer)
        not_before = cert.not_valid_before_utc
        not_after = cert.not_valid_after_utc
        is_self_signed = cert.subject == cert.issuer
        days_until_expiry = (not_after - now).days

        # Check temporal validity
        if now < not_before:
            errors.append(
                f"Certificate is not yet valid (not before: "
                f"{not_before.isoformat()})"
            )
        if now > not_after:
            errors.append(
                f"Certificate has expired (not after: "
                f"{not_after.isoformat()})"
            )

        # Validate chain certificates if provided
        if chain_pem is not None:
            chain_errors = self._validate_chain_certificates(chain_pem, now)
            errors.extend(chain_errors)

        valid = len(errors) == 0

        result: dict[str, Any] = {
            "valid": valid,
            "subject": f"CN={subject_cn}",
            "issuer": f"CN={issuer_cn}",
            "not_before": not_before.isoformat(),
            "not_after": not_after.isoformat(),
            "days_until_expiry": days_until_expiry,
            "is_self_signed": is_self_signed,
            "errors": errors,
        }

        logger.debug(
            "Certificate validation result: subject=%s, valid=%s, "
            "days_until_expiry=%d, errors=%d",
            subject_cn,
            valid,
            days_until_expiry,
            len(errors),
        )
        return result

    def _validate_chain_certificates(
        self, chain_pem: bytes, now: datetime
    ) -> list[str]:
        """Validate each certificate in a PEM chain.

        Splits the chain PEM data into individual certificates and checks
        each one for temporal validity.

        Args:
            chain_pem: Concatenated PEM-encoded chain certificates.
            now: Current UTC datetime for validity comparison.

        Returns:
            List of error messages (empty if all chain certs are valid).
        """
        errors: list[str] = []
        pem_blocks = self._split_pem_chain(chain_pem)

        for idx, pem_block in enumerate(pem_blocks):
            try:
                chain_cert = x509.load_pem_x509_certificate(
                    pem_block, default_backend()
                )
            except Exception as exc:
                errors.append(
                    f"Chain certificate [{idx}]: failed to parse — {exc}"
                )
                continue

            chain_cn = _extract_common_name(chain_cert.subject)
            chain_not_before = chain_cert.not_valid_before_utc
            chain_not_after = chain_cert.not_valid_after_utc

            if now < chain_not_before:
                errors.append(
                    f"Chain certificate [{idx}] (CN={chain_cn}): "
                    f"not yet valid (not before: "
                    f"{chain_not_before.isoformat()})"
                )
            if now > chain_not_after:
                errors.append(
                    f"Chain certificate [{idx}] (CN={chain_cn}): "
                    f"expired (not after: {chain_not_after.isoformat()})"
                )

        return errors

    @staticmethod
    def _split_pem_chain(chain_pem: bytes) -> list[bytes]:
        """Split concatenated PEM blocks into individual certificate bytes.

        Each PEM block is delimited by ``-----BEGIN CERTIFICATE-----``
        and ``-----END CERTIFICATE-----``.

        Args:
            chain_pem: Concatenated PEM-encoded certificates.

        Returns:
            List of individual PEM certificate byte blocks.
        """
        blocks: list[bytes] = []
        begin_marker = b"-----BEGIN CERTIFICATE-----"
        end_marker = b"-----END CERTIFICATE-----"

        data = chain_pem
        while begin_marker in data:
            start = data.index(begin_marker)
            end_pos = data.find(end_marker, start)
            if end_pos == -1:
                break
            end_pos += len(end_marker)
            blocks.append(data[start:end_pos] + b"\n")
            data = data[end_pos:]

        return blocks

    def check_certificate_expiry(self, cert_pem: bytes) -> dict[str, Any]:
        """Check how many days until a certificate expires.

        Parses the certificate and computes the expiry status based on
        the thresholds configured in ``SSLConfig``.  Used by health check
        monitoring (Feature F-401) and scheduled tasks (Feature F-402).

        Args:
            cert_pem: PEM-encoded certificate bytes.

        Returns:
            Expiry status dictionary::

                {
                    "subject": "CN=example.com",
                    "not_after": datetime(...),
                    "days_until_expiry": 25,
                    "status": "warning"
                }

            Status values:
            - ``"ok"``: More than ``renewal_warning_days`` until expiry.
            - ``"warning"``: Between critical and warning thresholds.
            - ``"critical"``: Fewer than ``renewal_critical_days`` remaining.
            - ``"expired"``: Certificate has already expired.
        """
        now = datetime.now(timezone.utc)

        try:
            cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
        except Exception as exc:
            logger.error("Failed to parse certificate for expiry check: %s", exc)
            return {
                "subject": "unknown",
                "not_after": None,
                "days_until_expiry": 0,
                "status": "expired",
            }

        subject_cn = _extract_common_name(cert.subject)
        not_after = cert.not_valid_after_utc
        days_until_expiry = (not_after - now).days

        # Determine status based on thresholds
        if days_until_expiry < 0:
            status = "expired"
        elif days_until_expiry <= self._config.renewal_critical_days:
            status = "critical"
        elif days_until_expiry <= self._config.renewal_warning_days:
            status = "warning"
        else:
            status = "ok"

        return {
            "subject": f"CN={subject_cn}",
            "not_after": not_after,
            "days_until_expiry": days_until_expiry,
            "status": status,
        }

    # ------------------------------------------------------------------
    # Keystore / Truststore Management
    # ------------------------------------------------------------------

    def load_keystore(
        self,
        keystore_path: str | Path,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Load a PKCS12 (.p12/.pfx) keystore file.

        Replaces Java's ``KeyStore.getInstance("PKCS12")`` for loading
        keystores containing private keys, certificates, and CA chains.

        Args:
            keystore_path: Filesystem path to the PKCS12 keystore file.
            password: Password for the keystore.  ``None`` if unprotected.

        Returns:
            Dictionary with extracted components::

                {
                    "private_key": b"-----BEGIN PRIVATE KEY-----...",
                    "certificate": b"-----BEGIN CERTIFICATE-----...",
                    "chain": [b"-----BEGIN CERTIFICATE-----...", ...]
                }

            Values are ``None`` if the corresponding component is absent.

        Raises:
            FileNotFoundError: If the keystore file does not exist.
            ValueError: If the PKCS12 data cannot be parsed.
        """
        path = Path(keystore_path).resolve()

        if not path.is_file():
            raise FileNotFoundError(
                f"Keystore file not found: {path}"
            )

        keystore_data = path.read_bytes()
        password_bytes = password.encode("utf-8") if password else None

        try:
            private_key, certificate, additional_certs = (
                load_key_and_certificates(
                    keystore_data, password_bytes, default_backend()
                )
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to parse PKCS12 keystore '{path}': {exc}"
            ) from exc

        result: dict[str, Any] = {
            "private_key": None,
            "certificate": None,
            "chain": [],
        }

        # Serialize private key to PEM
        if private_key is not None:
            result["private_key"] = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )

        # Serialize certificate to PEM
        if certificate is not None:
            result["certificate"] = certificate.public_bytes(
                serialization.Encoding.PEM
            )

        # Serialize chain certificates to PEM
        if additional_certs:
            result["chain"] = [
                chain_cert.public_bytes(serialization.Encoding.PEM)
                for chain_cert in additional_certs
            ]

        logger.info(
            "Loaded PKCS12 keystore: path=%s, has_key=%s, has_cert=%s, "
            "chain_certs=%d",
            path,
            result["private_key"] is not None,
            result["certificate"] is not None,
            len(result["chain"]),
        )
        return result

    def create_keystore(
        self,
        private_key_pem: bytes,
        cert_pem: bytes,
        chain_pem: list[bytes] | None = None,
        password: str | None = None,
    ) -> bytes:
        """Create a PKCS12 keystore from a private key and certificate.

        Replaces Java's ``KeyStore.store()`` for creating PKCS12 files
        from individual PEM-encoded components.

        Args:
            private_key_pem: PEM-encoded private key bytes.
            cert_pem: PEM-encoded certificate bytes.
            chain_pem: Optional list of PEM-encoded CA chain certificates.
            password: Password to protect the PKCS12 output.  ``None``
                for unencrypted output.

        Returns:
            PKCS12-encoded bytes that can be written to a ``.p12`` file.

        Raises:
            ValueError: If the PEM data cannot be parsed.
        """
        # Parse private key
        try:
            private_key = serialization.load_pem_private_key(
                private_key_pem,
                password=None,
                backend=default_backend(),
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to parse PEM private key: {exc}"
            ) from exc

        # Parse certificate
        try:
            certificate = x509.load_pem_x509_certificate(
                cert_pem, default_backend()
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to parse PEM certificate: {exc}"
            ) from exc

        # Parse chain certificates
        cas: list[x509.Certificate] | None = None
        if chain_pem:
            cas = []
            for idx, ca_pem in enumerate(chain_pem):
                try:
                    ca_cert = x509.load_pem_x509_certificate(
                        ca_pem, default_backend()
                    )
                    cas.append(ca_cert)
                except Exception as exc:
                    raise ValueError(
                        f"Failed to parse chain certificate [{idx}]: {exc}"
                    ) from exc

        # Determine encryption algorithm
        encryption: serialization.KeySerializationEncryption
        if password:
            encryption = serialization.BestAvailableEncryption(
                password.encode("utf-8")
            )
        else:
            encryption = serialization.NoEncryption()

        # Extract a friendly name from the certificate CN
        friendly_name = _extract_common_name(certificate.subject)

        pkcs12_data = serialize_key_and_certificates(
            name=friendly_name.encode("utf-8"),
            key=private_key,
            cert=certificate,
            cas=cas,
            encryption_algorithm=encryption,
        )

        logger.info(
            "Created PKCS12 keystore: subject=%s, chain_certs=%d, "
            "password_protected=%s",
            friendly_name,
            len(cas) if cas else 0,
            password is not None,
        )
        return pkcs12_data

    def update_truststore(
        self, trusted_certs: list[bytes]
    ) -> ssl.SSLContext:
        """Create an SSL context with additional trusted CA certificates.

        Builds a client-side TLS context pre-loaded with both the system
        default CA bundle and the provided custom CA certificates.  Used
        for proxy repository connections to registries that use custom or
        private CAs.

        Args:
            trusted_certs: List of PEM-encoded CA certificate bytes to
                trust in addition to system defaults.

        Returns:
            An ``ssl.SSLContext`` with the combined trust store loaded.

        Raises:
            ssl.SSLError: If any certificate data is invalid.
        """
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

        # Enforce minimum TLS version
        ctx.minimum_version = _TLS_VERSION_MAP.get(
            self._config.min_tls_version, ssl.TLSVersion.TLSv1_2
        )
        ctx.options |= (
            ssl.OP_NO_SSLv2
            | ssl.OP_NO_SSLv3
            | ssl.OP_NO_TLSv1
            | ssl.OP_NO_TLSv1_1
        )

        # Load system default CA certificates
        ctx.load_default_certs(purpose=ssl.Purpose.SERVER_AUTH)

        # Load each custom trusted CA certificate
        loaded_count = 0
        for idx, cert_pem in enumerate(trusted_certs):
            try:
                ctx.load_verify_locations(cadata=cert_pem.decode("ascii"))
                loaded_count += 1
            except Exception as exc:
                logger.warning(
                    "Failed to load trusted certificate [%d] into "
                    "truststore: %s",
                    idx,
                    exc,
                )

        logger.info(
            "Truststore updated: %d/%d custom CA certificates loaded",
            loaded_count,
            len(trusted_certs),
        )
        return ctx

    # ------------------------------------------------------------------
    # Gunicorn SSL Configuration
    # ------------------------------------------------------------------

    def get_gunicorn_ssl_config(self) -> dict[str, Any]:
        """Return a dictionary suitable for Gunicorn SSL configuration.

        Maps the ``SSLConfig`` settings to the Gunicorn SSL parameter
        names.  Only includes non-``None`` values to allow Gunicorn to
        use its own defaults for unconfigured parameters.

        Replaces Jetty 12.0.5 SSL connector configuration.

        Returns:
            Dictionary of Gunicorn SSL configuration parameters::

                {
                    "keyfile": "/path/to/key.pem",
                    "certfile": "/path/to/cert.pem",
                    "ca_certs": "/path/to/ca-bundle.pem",
                    "ssl_version": ssl.PROTOCOL_TLS_SERVER,
                    "ciphers": "ECDHE-RSA-AES256-GCM-SHA384:...",
                }
        """
        config: dict[str, Any] = {}

        if self._config.key_file is not None:
            config["keyfile"] = self._config.key_file
        if self._config.cert_file is not None:
            config["certfile"] = self._config.cert_file
        if self._config.ca_cert_file is not None:
            config["ca_certs"] = self._config.ca_cert_file

        config["ssl_version"] = ssl.PROTOCOL_TLS_SERVER

        # Build cipher string — only TLS 1.2 ciphers for Gunicorn/OpenSSL
        tls12_ciphers = _get_tls12_ciphers(self._config.cipher_suites)
        if tls12_ciphers:
            config["ciphers"] = ":".join(tls12_ciphers)

        logger.debug(
            "Gunicorn SSL config generated: keys=%s",
            list(config.keys()),
        )
        return config

    # ------------------------------------------------------------------
    # Certificate Renewal Tracking
    # ------------------------------------------------------------------

    def get_renewal_status(self) -> list[dict[str, Any]]:
        """Check expiry status of all managed certificates.

        Iterates through all certificates in the ``CertificateStore``
        and evaluates each against the configured renewal thresholds.
        Results are sorted by urgency (fewest days until expiry first).

        Used by scheduled tasks (Feature F-402) for automated certificate
        monitoring and alerting.

        Returns:
            List of expiry status dictionaries, sorted by
            ``days_until_expiry`` ascending (most urgent first).  Empty
            list if no ``CertificateStore`` is configured.
        """
        if self._certificate_store is None:
            logger.debug(
                "No certificate store configured — skipping renewal status"
            )
            return []

        statuses: list[dict[str, Any]] = []
        certificates = self._certificate_store.list_certificates()

        for cert_info in certificates:
            cert_pem = self._certificate_store.get_certificate_pem(
                cert_info.alias
            )
            if cert_pem is None:
                logger.warning(
                    "Certificate PEM not found for alias: %s",
                    cert_info.alias,
                )
                continue

            expiry_result = self.check_certificate_expiry(cert_pem)
            expiry_result["alias"] = cert_info.alias
            expiry_result["category"] = cert_info.category
            statuses.append(expiry_result)

        # Sort by days_until_expiry ascending (most urgent first)
        statuses.sort(key=lambda s: s.get("days_until_expiry", 0))

        logger.info(
            "Certificate renewal status checked: total=%d, "
            "warning=%d, critical=%d, expired=%d",
            len(statuses),
            sum(1 for s in statuses if s.get("status") == "warning"),
            sum(1 for s in statuses if s.get("status") == "critical"),
            sum(1 for s in statuses if s.get("status") == "expired"),
        )
        return statuses

    def get_ssl_info(self) -> dict[str, Any]:
        """Return comprehensive SSL/TLS configuration information.

        Aggregates the current configuration settings and certificate
        store statistics into a single dictionary.  Used by the health
        check API (Feature F-401) and system configuration endpoints.

        Returns:
            Dictionary with SSL/TLS configuration summary::

                {
                    "tls_min_version": "TLSv1.2",
                    "tls_max_version": "TLSv1.3",
                    "cipher_suites": [...],
                    "server_cert_configured": True,
                    "client_cert_configured": False,
                    "certificate_count": 5,
                    "certificates_expiring_soon": 1,
                }
        """
        certificate_count = 0
        certificates_expiring_soon = 0

        if self._certificate_store is not None:
            all_certs = self._certificate_store.list_certificates()
            certificate_count = len(all_certs)

            # Use CertificateStore's check_expirations for efficient counting
            expiration_report = self._certificate_store.check_expirations(
                warning_days=self._config.renewal_warning_days,
                critical_days=self._config.renewal_critical_days,
            )
            certificates_expiring_soon = (
                expiration_report.get("warning", 0)
                + expiration_report.get("critical", 0)
                + expiration_report.get("expired", 0)
            )

        return {
            "tls_min_version": self._config.min_tls_version,
            "tls_max_version": self._config.max_tls_version,
            "cipher_suites": list(self._config.cipher_suites),
            "server_cert_configured": self._config.has_server_cert,
            "client_cert_configured": self._config.has_client_cert,
            "certificate_count": certificate_count,
            "certificates_expiring_soon": certificates_expiring_soon,
        }


# ---------------------------------------------------------------------------
# Factory Function
# ---------------------------------------------------------------------------


def create_ssl_manager(
    app_config: dict[str, Any] | None = None,
) -> SSLManager:
    """Create an ``SSLManager`` from a Flask application configuration.

    Factory function that reads SSL/TLS settings from a configuration
    dictionary (typically ``flask.current_app.config``) and constructs
    a fully configured ``SSLManager`` instance.

    Follows the twelve-factor app methodology by reading configuration
    from environment-driven dictionaries.

    Args:
        app_config: Configuration dictionary (e.g. Flask ``app.config``).
            Recognised keys:

            - ``SSL_MIN_TLS_VERSION`` (str): Minimum TLS version.
            - ``SSL_MAX_TLS_VERSION`` (str): Maximum TLS version.
            - ``SSL_CERT_FILE`` (str): Server certificate path.
            - ``SSL_KEY_FILE`` (str): Server private key path.
            - ``SSL_CA_CERT_FILE`` (str): CA certificate bundle path.
            - ``SSL_CLIENT_CERT_FILE`` (str): Client certificate path.
            - ``SSL_CLIENT_KEY_FILE`` (str): Client private key path.
            - ``SSL_KEY_PASSWORD`` (str): Private key password.
            - ``SSL_CIPHER_SUITES`` (list[str]): Cipher suite list.
            - ``SSL_VERIFY_HOSTNAME`` (bool): Hostname verification.
            - ``SSL_VERIFY_CERTIFICATES`` (bool): Cert verification.
            - ``SSL_RENEWAL_WARNING_DAYS`` (int): Warning threshold.
            - ``SSL_RENEWAL_CRITICAL_DAYS`` (int): Critical threshold.

    Returns:
        Configured ``SSLManager`` instance.
    """
    if app_config is None:
        app_config = {}

    # Build SSLConfig from application configuration
    ssl_config = SSLConfig(
        min_tls_version=app_config.get(
            "SSL_MIN_TLS_VERSION", "TLSv1.2"
        ),
        max_tls_version=app_config.get(
            "SSL_MAX_TLS_VERSION", "TLSv1.3"
        ),
        cert_file=app_config.get("SSL_CERT_FILE"),
        key_file=app_config.get("SSL_KEY_FILE"),
        ca_cert_file=app_config.get("SSL_CA_CERT_FILE"),
        client_cert_file=app_config.get("SSL_CLIENT_CERT_FILE"),
        client_key_file=app_config.get("SSL_CLIENT_KEY_FILE"),
        key_password=app_config.get("SSL_KEY_PASSWORD"),
        cipher_suites=app_config.get(
            "SSL_CIPHER_SUITES", list(_DEFAULT_CIPHER_SUITES)
        ),
        verify_hostname=app_config.get("SSL_VERIFY_HOSTNAME", True),
        verify_certificates=app_config.get(
            "SSL_VERIFY_CERTIFICATES", True
        ),
        renewal_warning_days=app_config.get(
            "SSL_RENEWAL_WARNING_DAYS", 30
        ),
        renewal_critical_days=app_config.get(
            "SSL_RENEWAL_CRITICAL_DAYS", 7
        ),
    )

    logger.info(
        "Creating SSLManager from application config: "
        "min_tls=%s, server_cert=%s, verify=%s",
        ssl_config.min_tls_version,
        ssl_config.has_server_cert,
        ssl_config.verify_certificates,
    )

    return SSLManager(ssl_config)
