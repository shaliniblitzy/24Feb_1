"""
Cryptographic Utility Functions Module.

Replaces BouncyCastle 1.78.1 cryptographic operations and Java's java.security
framework from the original Java source system. Uses cryptography 44.0.0 as the
primary library.

This module provides:
- Secure random generation (replaces java.security.SecureRandom)
- Hash computation (SHA-1, SHA-256, SHA-512, MD5 for checksums)
- HMAC generation and verification (for webhook payload signing — Feature F-503)
- RSA and EC key pair generation (replaces BouncyCastle key generators)
- X.509 Certificate Signing Request (CSR) generation
- Self-signed certificate generation for development/testing
- PBKDF2 key derivation and verification
- PEM/DER format conversion utilities
- Token/secret/API key generation utilities

This is the MOST FOUNDATIONAL file in the src/app/security/ package — both
ssl_manager.py and certificate_store.py depend on it.

SECURITY NOTES:
- Private key material must NEVER be logged
- HMAC verification uses constant-time comparison (hmac.compare_digest)
- RSA key sizes must be >= 2048 bits
- PBKDF2 iterations must be >= 600,000 (OWASP 2024 recommendation)
"""

import os
import hmac as _hmac_module
import hashlib
import secrets
import logging
from typing import Any
from datetime import datetime, timezone, timedelta
from ipaddress import ip_address

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, ec, padding
from cryptography.hazmat.primitives.asymmetric.types import (
    CertificateIssuerPrivateKeyTypes,
)
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    PrivateFormat,
    NoEncryption,
    BestAvailableEncryption,
)
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidKey

# ---------------------------------------------------------------------------
# Module-level logger — structured logging for all cryptographic operations.
# Replaces SLF4J 1.7.36 + Logback 1.2.13 from the Java source system.
# CRITICAL: Private key material must NEVER appear in log output.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hash algorithm identifiers — used across the security package for
# consistent algorithm specification.
HASH_SHA1: str = "sha1"
HASH_SHA256: str = "sha256"
HASH_SHA512: str = "sha512"
HASH_MD5: str = "md5"

# Internal set of supported hash algorithms for validation
_SUPPORTED_HASH_ALGORITHMS: frozenset[str] = frozenset(
    {HASH_SHA1, HASH_SHA256, HASH_SHA512, HASH_MD5}
)

# Default RSA key sizes
DEFAULT_RSA_KEY_SIZE: int = 2048
RSA_KEY_SIZE_4096: int = 4096

# Minimum RSA key size — reject anything smaller for security compliance
_MIN_RSA_KEY_SIZE: int = 2048

# Default elliptic curve selections (NIST standards)
DEFAULT_EC_CURVE: ec.EllipticCurve = ec.SECP256R1()  # P-256 curve
EC_CURVE_P384: ec.EllipticCurve = ec.SECP384R1()
EC_CURVE_P521: ec.EllipticCurve = ec.SECP521R1()

# Token generation defaults
DEFAULT_TOKEN_LENGTH: int = 32  # bytes (256 bits)
DEFAULT_API_KEY_LENGTH: int = 40  # characters

# HMAC defaults
DEFAULT_HMAC_ALGORITHM: str = "sha256"

# PBKDF2 defaults — OWASP 2024 recommendation for SHA-256
PBKDF2_ITERATIONS: int = 600_000
PBKDF2_KEY_LENGTH: int = 32  # bytes (256 bits)

# Internal mapping from algorithm string to cryptography hash class.
# Used by compute_fingerprint to resolve algorithm identifiers to
# cryptography library hash objects.
_HASH_ALGORITHM_MAP: dict[str, type[hashes.HashAlgorithm]] = {
    HASH_SHA1: hashes.SHA1,
    HASH_SHA256: hashes.SHA256,
    HASH_SHA512: hashes.SHA512,
}


# ---------------------------------------------------------------------------
# Secure Random Generation
# ---------------------------------------------------------------------------


def generate_random_bytes(length: int = DEFAULT_TOKEN_LENGTH) -> bytes:
    """Generate cryptographically secure random bytes.

    Replaces java.security.SecureRandom.nextBytes() from the Java source system.
    Uses os.urandom() which draws from the OS-level cryptographic random pool.

    Args:
        length: Number of random bytes to generate. Must be a positive integer.
                Defaults to DEFAULT_TOKEN_LENGTH (32 bytes / 256 bits).

    Returns:
        Cryptographically secure random bytes of the specified length.

    Raises:
        ValueError: If length is not a positive integer.

    Usage:
        Salt generation, nonce generation, session tokens, IV generation.
    """
    if not isinstance(length, int) or length <= 0:
        raise ValueError(
            f"Length must be a positive integer, got {length!r}"
        )
    return os.urandom(length)


def generate_token(length: int = DEFAULT_TOKEN_LENGTH) -> str:
    """Generate a secure random token as a hexadecimal string.

    Uses secrets.token_hex() which produces 2 * length hexadecimal characters
    from cryptographically secure random bytes.

    Args:
        length: Number of random bytes (the resulting hex string will be
                2 * length characters). Defaults to DEFAULT_TOKEN_LENGTH (32).

    Returns:
        Hexadecimal string of length 2 * length characters.

    Raises:
        ValueError: If length is not a positive integer.

    Usage:
        Session tokens, CSRF tokens, temporary tokens, email verification tokens.
    """
    if not isinstance(length, int) or length <= 0:
        raise ValueError(
            f"Length must be a positive integer, got {length!r}"
        )
    return secrets.token_hex(length)


def generate_api_key(length: int = DEFAULT_API_KEY_LENGTH) -> str:
    """Generate a URL-safe API key string.

    Uses secrets.token_urlsafe() which produces a URL-safe base64-encoded
    string from cryptographically secure random bytes. Replaces Java's
    UUID.randomUUID() for API key generation.

    Args:
        length: Number of random bytes to use for key generation.
                The resulting string will be approximately 4/3 * length
                characters. Defaults to DEFAULT_API_KEY_LENGTH (40).

    Returns:
        URL-safe base64-encoded API key string.

    Raises:
        ValueError: If length is not a positive integer.

    Usage:
        User API keys (Feature F-304 — API Key Authentication).
    """
    if not isinstance(length, int) or length <= 0:
        raise ValueError(
            f"Length must be a positive integer, got {length!r}"
        )
    return secrets.token_urlsafe(length)


def generate_secret_key(length: int = 64) -> str:
    """Generate a secret key suitable for Flask SECRET_KEY or JWT signing.

    Produces a hexadecimal string of 2 * length characters from
    cryptographically secure random bytes. Default produces a 128-character
    hex string (512-bit key).

    Args:
        length: Number of random bytes. Defaults to 64 (produces 128 hex chars).

    Returns:
        Hexadecimal secret key string.

    Raises:
        ValueError: If length is not a positive integer.

    Usage:
        Flask SECRET_KEY configuration, JWT signing secrets,
        encryption key material.
    """
    if not isinstance(length, int) or length <= 0:
        raise ValueError(
            f"Length must be a positive integer, got {length!r}"
        )
    return secrets.token_hex(length)


# ---------------------------------------------------------------------------
# Hash Computation
# ---------------------------------------------------------------------------


def compute_hash(data: bytes, algorithm: str = HASH_SHA256) -> str:
    """Compute hash digest of byte data using the specified algorithm.

    Replaces Java's java.security.MessageDigest. Available here for
    security-context hashing; bulk file hashing is in
    src/app/utils/helpers.py.

    Args:
        data: Byte data to hash.
        algorithm: Hash algorithm identifier. One of: "sha1", "sha256",
                   "sha512", "md5". Defaults to HASH_SHA256.

    Returns:
        Lowercase hexadecimal digest string.

    Raises:
        ValueError: If the algorithm is not supported.
        TypeError: If data is not bytes.
    """
    if not isinstance(data, bytes):
        raise TypeError(
            f"Data must be bytes, got {type(data).__name__}"
        )

    algorithm_lower = algorithm.lower()
    if algorithm_lower not in _SUPPORTED_HASH_ALGORITHMS:
        raise ValueError(
            f"Unsupported hash algorithm: {algorithm!r}. "
            f"Supported: {sorted(_SUPPORTED_HASH_ALGORITHMS)}"
        )

    hasher = hashlib.new(algorithm_lower)
    hasher.update(data)
    return hasher.hexdigest()


def compute_fingerprint(cert_pem: bytes, algorithm: str = HASH_SHA256) -> str:
    """Compute the fingerprint of an X.509 certificate.

    Parses a PEM-encoded certificate and computes its fingerprint using the
    specified hash algorithm. Output is formatted with colons for readability
    (e.g., "AB:CD:EF:12:34:...").

    Args:
        cert_pem: PEM-encoded X.509 certificate bytes.
        algorithm: Hash algorithm for fingerprint. Supports "sha256", "sha1",
                   and "sha512". Defaults to HASH_SHA256.

    Returns:
        Colon-separated uppercase hexadecimal fingerprint string.

    Raises:
        ValueError: If the certificate cannot be parsed or the algorithm is
                    not supported for fingerprinting.

    Usage:
        Used by certificate_store.py for certificate identification.
    """
    algorithm_lower = algorithm.lower()
    if algorithm_lower not in _HASH_ALGORITHM_MAP:
        raise ValueError(
            f"Unsupported fingerprint algorithm: {algorithm!r}. "
            f"Supported: {sorted(_HASH_ALGORITHM_MAP.keys())}"
        )

    try:
        cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
    except Exception as exc:
        raise ValueError(f"Failed to parse PEM certificate: {exc}") from exc

    hash_instance = _HASH_ALGORITHM_MAP[algorithm_lower]()
    fingerprint_bytes = cert.fingerprint(hash_instance)
    fingerprint_hex = fingerprint_bytes.hex().upper()

    # Format with colons for readability: "AB:CD:EF:..."
    return ":".join(
        fingerprint_hex[i : i + 2] for i in range(0, len(fingerprint_hex), 2)
    )


# ---------------------------------------------------------------------------
# HMAC Generation and Verification
# ---------------------------------------------------------------------------


def compute_hmac(
    key: bytes | str,
    message: bytes | str,
    algorithm: str = DEFAULT_HMAC_ALGORITHM,
) -> str:
    """Compute HMAC of a message using the given key.

    CRITICAL for webhook payload signing (Feature F-503). Uses Python's
    hmac module with hashlib digest algorithms.

    Args:
        key: HMAC key. If str, encoded to UTF-8 before use.
        message: Message to authenticate. If str, encoded to UTF-8.
        algorithm: Hash algorithm for HMAC. Supports "sha256", "sha1",
                   "sha512", "md5". Defaults to DEFAULT_HMAC_ALGORITHM.

    Returns:
        Lowercase hexadecimal HMAC digest string.

    Raises:
        ValueError: If the algorithm is not supported.

    Usage:
        Used by src/app/webhooks/payload_signer.py for webhook signatures.
    """
    algorithm_lower = algorithm.lower()
    if algorithm_lower not in _SUPPORTED_HASH_ALGORITHMS:
        raise ValueError(
            f"Unsupported HMAC algorithm: {algorithm!r}. "
            f"Supported: {sorted(_SUPPORTED_HASH_ALGORITHMS)}"
        )

    # Encode string inputs to bytes
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    msg_bytes = message.encode("utf-8") if isinstance(message, str) else message

    return _hmac_module.new(key_bytes, msg_bytes, algorithm_lower).hexdigest()


def verify_hmac(
    key: bytes | str,
    message: bytes | str,
    expected_hmac: str,
    algorithm: str = DEFAULT_HMAC_ALGORITHM,
) -> bool:
    """Verify an HMAC by computing and comparing with constant-time comparison.

    MUST use hmac.compare_digest() to prevent timing attacks. This is a
    security-critical function.

    Args:
        key: HMAC key. If str, encoded to UTF-8.
        message: Original message. If str, encoded to UTF-8.
        expected_hmac: Expected HMAC hex digest to verify against.
        algorithm: Hash algorithm. Defaults to DEFAULT_HMAC_ALGORITHM.

    Returns:
        True if the computed HMAC matches expected_hmac, False otherwise.

    Usage:
        Webhook signature verification, API request authentication.
    """
    computed = compute_hmac(key, message, algorithm)
    # SECURITY: Constant-time comparison prevents timing-based side-channel attacks
    return _hmac_module.compare_digest(computed, expected_hmac)


# ---------------------------------------------------------------------------
# Key Pair Generation
# ---------------------------------------------------------------------------


def generate_rsa_key_pair(
    key_size: int = DEFAULT_RSA_KEY_SIZE,
    password: str | None = None,
) -> tuple[bytes, bytes]:
    """Generate an RSA key pair and return PEM-encoded private and public keys.

    Replaces BouncyCastle RSA key generation from the Java source system.
    Uses the standard RSA public exponent of 65537 (F4).

    Args:
        key_size: RSA key size in bits. Must be >= 2048 for security
                  compliance. Defaults to DEFAULT_RSA_KEY_SIZE (2048).
        password: Optional password to encrypt the private key using
                  BestAvailableEncryption. If None, private key is
                  stored unencrypted.

    Returns:
        Tuple of (private_key_pem, public_key_pem) as bytes. Both are
        PEM-encoded. Private key uses PKCS8 format; public key uses
        SubjectPublicKeyInfo format.

    Raises:
        ValueError: If key_size < 2048 (security requirement).

    Security:
        - Key size must be >= 2048 bits per security policy.
        - Private key material is NEVER logged — only key size metadata.
    """
    if key_size < _MIN_RSA_KEY_SIZE:
        raise ValueError(
            f"RSA key size must be >= {_MIN_RSA_KEY_SIZE} bits for security. "
            f"Got {key_size} bits."
        )

    logger.info("Generating RSA key pair with key size %d bits", key_size)

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
        backend=default_backend(),
    )

    # Serialize private key to PEM format (PKCS8)
    encryption: serialization.KeySerializationEncryption
    if password:
        encryption = BestAvailableEncryption(password.encode("utf-8"))
    else:
        encryption = NoEncryption()

    private_key_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )

    # Serialize public key to PEM format (SubjectPublicKeyInfo)
    public_key_pem = private_key.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    )

    logger.info(
        "RSA key pair generated successfully (key size: %d bits)", key_size
    )
    return private_key_pem, public_key_pem


def generate_ec_key_pair(
    curve: ec.EllipticCurve = DEFAULT_EC_CURVE,
    password: str | None = None,
) -> tuple[bytes, bytes]:
    """Generate an Elliptic Curve key pair and return PEM-encoded keys.

    Args:
        curve: Elliptic curve to use. Supports SECP256R1 (P-256),
               SECP384R1 (P-384), SECP521R1 (P-521). Defaults to
               DEFAULT_EC_CURVE (P-256 / SECP256R1).
        password: Optional password to encrypt the private key.

    Returns:
        Tuple of (private_key_pem, public_key_pem) as bytes. Both are
        PEM-encoded.

    Security:
        - Private key material is NEVER logged — only curve name.
    """
    curve_name = curve.name if hasattr(curve, "name") else str(curve)
    logger.info("Generating EC key pair with curve %s", curve_name)

    private_key = ec.generate_private_key(
        curve=curve,
        backend=default_backend(),
    )

    # Serialize private key to PEM format (PKCS8)
    encryption: serialization.KeySerializationEncryption
    if password:
        encryption = BestAvailableEncryption(password.encode("utf-8"))
    else:
        encryption = NoEncryption()

    private_key_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )

    # Serialize public key to PEM format (SubjectPublicKeyInfo)
    public_key_pem = private_key.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    )

    logger.info("EC key pair generated successfully (curve: %s)", curve_name)
    return private_key_pem, public_key_pem


# ---------------------------------------------------------------------------
# Certificate Signing Request (CSR) Generation
# ---------------------------------------------------------------------------


def generate_csr(
    private_key_pem: bytes,
    common_name: str,
    organization: str | None = None,
    country: str | None = None,
    state: str | None = None,
    locality: str | None = None,
    email: str | None = None,
    san_dns_names: list[str] | None = None,
    san_ip_addresses: list[str] | None = None,
    key_password: str | None = None,
) -> bytes:
    """Generate a Certificate Signing Request (CSR).

    Builds a CSR with the provided subject attributes and optional Subject
    Alternative Names (SANs). The CSR is signed with the provided private key.

    Args:
        private_key_pem: PEM-encoded private key bytes.
        common_name: Common Name (CN) for the certificate subject.
        organization: Organization name (O).
        country: Country code (C), e.g., "US". Must be 2 characters.
        state: State or province name (ST).
        locality: Locality/city name (L).
        email: Email address for the subject.
        san_dns_names: List of DNS names for Subject Alternative Names.
        san_ip_addresses: List of IP address strings for SANs (IPv4 or IPv6).
        key_password: Password for the private key, if encrypted.

    Returns:
        PEM-encoded CSR bytes.

    Raises:
        ValueError: If the private key cannot be loaded or CSR generation fails.
    """
    logger.info("Generating CSR for CN=%s", common_name)

    # Load the private key
    password_bytes = key_password.encode("utf-8") if key_password else None
    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem,
            password=password_bytes,
            backend=default_backend(),
        )
    except Exception as exc:
        raise ValueError(f"Failed to load private key: {exc}") from exc

    # Build subject name attributes
    name_attributes: list[x509.NameAttribute] = [
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ]
    if organization:
        name_attributes.append(
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization)
        )
    if country:
        name_attributes.append(
            x509.NameAttribute(NameOID.COUNTRY_NAME, country)
        )
    if state:
        name_attributes.append(
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, state)
        )
    if locality:
        name_attributes.append(
            x509.NameAttribute(NameOID.LOCALITY_NAME, locality)
        )
    if email:
        name_attributes.append(
            x509.NameAttribute(NameOID.EMAIL_ADDRESS, email)
        )

    subject = x509.Name(name_attributes)

    # Build CSR
    builder = x509.CertificateSigningRequestBuilder().subject_name(subject)

    # Add Subject Alternative Names (SANs) if provided
    san_entries: list[x509.GeneralName] = []
    if san_dns_names:
        san_entries.extend([x509.DNSName(name) for name in san_dns_names])
    if san_ip_addresses:
        san_entries.extend(
            [x509.IPAddress(ip_address(ip)) for ip in san_ip_addresses]
        )
    if san_entries:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )

    # Sign the CSR with the private key using SHA-256
    try:
        csr = builder.sign(private_key, hashes.SHA256(), default_backend())
    except Exception as exc:
        raise ValueError(f"Failed to sign CSR: {exc}") from exc

    logger.info("CSR generated successfully for CN=%s", common_name)
    return csr.public_bytes(Encoding.PEM)


# ---------------------------------------------------------------------------
# Self-Signed Certificate Generation
# ---------------------------------------------------------------------------


def generate_self_signed_certificate(
    private_key_pem: bytes,
    common_name: str,
    validity_days: int = 365,
    organization: str | None = None,
    san_dns_names: list[str] | None = None,
    key_password: str | None = None,
) -> bytes:
    """Generate a self-signed X.509 certificate.

    Creates a self-signed certificate suitable for development servers and
    test fixtures. The certificate is signed with the provided private key
    using SHA-256.

    Args:
        private_key_pem: PEM-encoded private key bytes.
        common_name: Common Name (CN) for the certificate.
        validity_days: Certificate validity period in days. Defaults to 365.
        organization: Organization name (O).
        san_dns_names: List of DNS names for Subject Alternative Names.
        key_password: Password for the private key, if encrypted.

    Returns:
        PEM-encoded self-signed certificate bytes.

    Raises:
        ValueError: If the private key cannot be loaded or cert generation fails.

    Usage:
        Development server SSL, test fixtures, local testing.
    """
    logger.info(
        "Generating self-signed certificate for CN=%s (validity: %d days)",
        common_name,
        validity_days,
    )

    # Load the private key
    password_bytes = key_password.encode("utf-8") if key_password else None
    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem,
            password=password_bytes,
            backend=default_backend(),
        )
    except Exception as exc:
        raise ValueError(f"Failed to load private key: {exc}") from exc

    # Build subject/issuer name
    name_attributes: list[x509.NameAttribute] = [
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ]
    if organization:
        name_attributes.append(
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization)
        )

    # For self-signed certs, subject and issuer are the same
    subject = issuer = x509.Name(name_attributes)

    now = datetime.now(timezone.utc)

    # Build certificate
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)  # Self-signed: issuer == subject
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
    )

    # Add Subject Alternative Names if provided
    if san_dns_names:
        san_entries = [x509.DNSName(name) for name in san_dns_names]
        builder = builder.add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )

    # Sign the certificate with the private key using SHA-256
    try:
        cert = builder.sign(private_key, hashes.SHA256(), default_backend())
    except Exception as exc:
        raise ValueError(f"Failed to sign certificate: {exc}") from exc

    logger.info("Self-signed certificate generated for CN=%s", common_name)
    return cert.public_bytes(Encoding.PEM)


# ---------------------------------------------------------------------------
# Key Derivation (PBKDF2)
# ---------------------------------------------------------------------------


def derive_key(
    password: str,
    salt: bytes | None = None,
    iterations: int = PBKDF2_ITERATIONS,
    key_length: int = PBKDF2_KEY_LENGTH,
) -> tuple[bytes, bytes]:
    """Derive a cryptographic key from a password using PBKDF2-HMAC-SHA256.

    If no salt is provided, generates a cryptographically secure 16-byte
    random salt. The caller MUST store the salt alongside the derived key
    for later verification.

    Args:
        password: Password string to derive key from.
        salt: Random salt bytes. If None, generates a 16-byte random salt.
        iterations: Number of PBKDF2 iterations. Defaults to PBKDF2_ITERATIONS
                    (600,000 per OWASP 2024 recommendation for SHA-256).
        key_length: Desired key length in bytes. Defaults to PBKDF2_KEY_LENGTH
                    (32 bytes / 256 bits).

    Returns:
        Tuple of (derived_key, salt). Caller must store the salt for
        subsequent verification with verify_derived_key().

    Usage:
        Encrypting sensitive configuration values at rest.
    """
    if salt is None:
        salt = os.urandom(16)

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=key_length,
        salt=salt,
        iterations=iterations,
        backend=default_backend(),
    )
    derived_key = kdf.derive(password.encode("utf-8"))
    return derived_key, salt


def verify_derived_key(
    password: str,
    salt: bytes,
    expected_key: bytes,
    iterations: int = PBKDF2_ITERATIONS,
    key_length: int = PBKDF2_KEY_LENGTH,
) -> bool:
    """Verify a password against a previously derived key.

    Uses PBKDF2HMAC.verify() which handles timing-safe comparison internally,
    preventing timing-based side-channel attacks.

    Args:
        password: Password to verify.
        salt: Salt used during original key derivation.
        expected_key: Previously derived key to compare against.
        iterations: Number of PBKDF2 iterations used during derivation.
        key_length: Key length in bytes used during derivation.

    Returns:
        True if the password produces the same derived key, False otherwise.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=key_length,
        salt=salt,
        iterations=iterations,
        backend=default_backend(),
    )
    try:
        kdf.verify(password.encode("utf-8"), expected_key)
        return True
    except InvalidKey:
        return False


# ---------------------------------------------------------------------------
# Key Loading Utilities
# ---------------------------------------------------------------------------


def load_private_key(pem_data: bytes, password: str | None = None) -> Any:
    """Load a PEM-encoded private key (RSA or EC).

    Args:
        pem_data: PEM-encoded private key bytes.
        password: Password for encrypted private keys. If None, the key
                  is assumed to be unencrypted.

    Returns:
        Private key object (RSAPrivateKey, EllipticCurvePrivateKey, etc.).

    Raises:
        ValueError: If the key data is invalid or cannot be loaded.
    """
    password_bytes = password.encode("utf-8") if password else None
    try:
        return serialization.load_pem_private_key(
            pem_data,
            password=password_bytes,
            backend=default_backend(),
        )
    except Exception as exc:
        raise ValueError(f"Failed to load private key: {exc}") from exc


def load_public_key(pem_data: bytes) -> Any:
    """Load a PEM-encoded public key.

    Args:
        pem_data: PEM-encoded public key bytes.

    Returns:
        Public key object (RSAPublicKey, EllipticCurvePublicKey, etc.).

    Raises:
        ValueError: If the key data is invalid or cannot be loaded.
    """
    try:
        return serialization.load_pem_public_key(
            pem_data,
            backend=default_backend(),
        )
    except Exception as exc:
        raise ValueError(f"Failed to load public key: {exc}") from exc


def get_key_info(key_pem: bytes) -> dict:
    """Extract information about a PEM-encoded key.

    Attempts to load the key first as a private key, then as a public key,
    and extracts type, size, curve (for EC), and privacy information.

    Args:
        key_pem: PEM-encoded key bytes (private or public).

    Returns:
        Dictionary with key information:
        {
            "type": "RSA" | "EC" | "Unknown",
            "size": 2048,          # bit size for RSA or EC
            "curve": "secp256r1",  # curve name for EC keys, None for RSA
            "is_private": True | False,
        }

    Raises:
        ValueError: If the key data cannot be parsed as any known key type.
    """
    info: dict[str, Any] = {
        "type": "Unknown",
        "size": 0,
        "curve": None,
        "is_private": False,
    }

    key: Any = None

    # Try loading as an unencrypted private key first
    try:
        key = serialization.load_pem_private_key(
            key_pem, password=None, backend=default_backend()
        )
        info["is_private"] = True
    except TypeError:
        # Encrypted private key (password=None raises TypeError for encrypted keys).
        # We know it is a private key, but cannot extract key details without password.
        info["is_private"] = True
        # Attempt to infer type from PEM header
        pem_str = key_pem.decode("utf-8", errors="replace")
        if "RSA" in pem_str:
            info["type"] = "RSA"
        elif "EC" in pem_str:
            info["type"] = "EC"
        return info
    except Exception:
        # Not a valid private key — try loading as a public key
        try:
            key = serialization.load_pem_public_key(
                key_pem, backend=default_backend()
            )
            info["is_private"] = False
        except Exception as exc:
            raise ValueError(f"Cannot parse key data: {exc}") from exc

    # Determine key type and attributes from the loaded key object
    if isinstance(key, (rsa.RSAPrivateKey, rsa.RSAPublicKey)):
        info["type"] = "RSA"
        info["size"] = key.key_size
    elif isinstance(key, (ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey)):
        info["type"] = "EC"
        info["curve"] = key.curve.name
        info["size"] = key.key_size

    return info


# ---------------------------------------------------------------------------
# PEM / DER Format Conversion Utilities
# ---------------------------------------------------------------------------


def pem_to_der(pem_data: bytes) -> bytes:
    """Convert PEM-encoded data to DER format.

    Handles certificates, private keys, public keys, and CSRs by detecting
    the PEM header type.

    Args:
        pem_data: PEM-encoded data bytes.

    Returns:
        DER-encoded data bytes.

    Raises:
        ValueError: If the PEM type cannot be detected or the data is invalid.
    """
    pem_str = pem_data.decode("utf-8", errors="replace")

    try:
        if "CERTIFICATE REQUEST" in pem_str:
            csr = x509.load_pem_x509_csr(pem_data, default_backend())
            return csr.public_bytes(Encoding.DER)
        elif "CERTIFICATE" in pem_str:
            cert = x509.load_pem_x509_certificate(pem_data, default_backend())
            return cert.public_bytes(Encoding.DER)
        elif "PRIVATE KEY" in pem_str:
            # Attempt to load as unencrypted private key for DER conversion
            key = serialization.load_pem_private_key(
                pem_data, password=None, backend=default_backend()
            )
            return key.private_bytes(
                encoding=Encoding.DER,
                format=PrivateFormat.PKCS8,
                encryption_algorithm=NoEncryption(),
            )
        elif "PUBLIC KEY" in pem_str:
            key = serialization.load_pem_public_key(
                pem_data, backend=default_backend()
            )
            return key.public_bytes(
                encoding=Encoding.DER,
                format=PublicFormat.SubjectPublicKeyInfo,
            )
        else:
            raise ValueError(
                "Unable to detect PEM type from header. Expected one of: "
                "CERTIFICATE, CERTIFICATE REQUEST, PRIVATE KEY, PUBLIC KEY."
            )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Failed to convert PEM to DER: {exc}") from exc


def der_to_pem(der_data: bytes, pem_type: str = "CERTIFICATE") -> bytes:
    """Convert DER-encoded data to PEM format.

    Uses the cryptography library to properly parse the DER data and
    re-serialize it as PEM, ensuring correct encoding.

    Args:
        der_data: DER-encoded data bytes.
        pem_type: Type of the PEM data. One of: "CERTIFICATE",
                  "PRIVATE KEY", "PUBLIC KEY", "CERTIFICATE REQUEST".
                  Defaults to "CERTIFICATE".

    Returns:
        PEM-encoded data bytes.

    Raises:
        ValueError: If pem_type is not supported or the DER data is invalid.
    """
    pem_type_upper = pem_type.upper()
    valid_types = {
        "CERTIFICATE",
        "PRIVATE KEY",
        "PUBLIC KEY",
        "CERTIFICATE REQUEST",
    }

    if pem_type_upper not in valid_types:
        raise ValueError(
            f"Unsupported PEM type: {pem_type!r}. "
            f"Supported: {sorted(valid_types)}"
        )

    try:
        if pem_type_upper == "CERTIFICATE":
            cert = x509.load_der_x509_certificate(
                der_data, default_backend()
            )
            return cert.public_bytes(Encoding.PEM)
        elif pem_type_upper == "PRIVATE KEY":
            key = serialization.load_der_private_key(
                der_data, password=None, backend=default_backend()
            )
            return key.private_bytes(
                encoding=Encoding.PEM,
                format=PrivateFormat.PKCS8,
                encryption_algorithm=NoEncryption(),
            )
        elif pem_type_upper == "PUBLIC KEY":
            key = serialization.load_der_public_key(
                der_data, backend=default_backend()
            )
            return key.public_bytes(
                encoding=Encoding.PEM,
                format=PublicFormat.SubjectPublicKeyInfo,
            )
        elif pem_type_upper == "CERTIFICATE REQUEST":
            csr = x509.load_der_x509_csr(der_data, default_backend())
            return csr.public_bytes(Encoding.PEM)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            f"Failed to convert DER to PEM ({pem_type_upper}): {exc}"
        ) from exc

    # Should not be reachable, but satisfy type checker
    raise ValueError(f"Conversion failed for PEM type: {pem_type_upper}")  # pragma: no cover
