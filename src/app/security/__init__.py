"""
SSL/TLS Certificate Management Package.

This package implements Feature F-302 (SSL/TLS Support) for the Nexus Repository
Flask application. It replaces BouncyCastle 1.78.1 and Java's javax.net.ssl
infrastructure from the original Java source system.

Modules:
    ssl_manager:
        SSL/TLS configuration management — TLS protocol version enforcement,
        cipher suite configuration, keystore/truststore management, certificate
        chain validation, automatic renewal tracking, and SSL context creation
        for both Gunicorn server-side and proxy client-side connections.
        Replaces Jetty 12.0.5 SslContextFactory and javax.net.ssl.SSLContext.

    certificate_store:
        Certificate storage, import/export (PEM, DER, PKCS12), expiration
        monitoring, and metadata extraction.  Replaces java.security.KeyStore
        (JKS/PKCS12) and BouncyCastle 1.78.1 certificate handling.

    crypto_utils:
        Low-level cryptographic utility functions — secure random generation,
        hash computation (SHA-1, SHA-256, SHA-512, MD5), HMAC generation for
        webhook payload signing (Feature F-503), RSA and EC key pair generation,
        Certificate Signing Request (CSR) generation.  Replaces
        java.security.SecureRandom, java.security.MessageDigest, and
        BouncyCastle 1.78.1 key generators.

Primary dependency: cryptography 44.0.0 (PyPI)

Usage::

    # Import classes directly from the package
    from src.app.security import SSLManager, SSLConfig, CertificateStore

    # Import cryptographic utility functions
    from src.app.security import (
        generate_random_bytes,
        compute_hash,
        compute_hmac,
        generate_rsa_key_pair,
        generate_ec_key_pair,
        generate_csr,
    )

    # Create an SSL configuration and manager
    config = SSLConfig(
        cert_file="/path/to/server.pem",
        key_file="/path/to/server-key.pem",
    )
    manager = SSLManager(config)
    ssl_ctx = manager.create_server_ssl_context()

    # Compute a SHA-256 hash
    digest = compute_hash(b"data to hash", "sha256")
"""

# ---------------------------------------------------------------------------
# Convenience re-exports from submodules
# ---------------------------------------------------------------------------
# Import order follows the dependency graph:
#   1. crypto_utils  (no internal dependencies — foundational)
#   2. certificate_store  (depends on crypto_utils)
#   3. ssl_manager  (depends on certificate_store and crypto_utils)
# ---------------------------------------------------------------------------

# Cryptographic utility functions — foundational, no internal dependencies
from src.app.security.crypto_utils import (
    generate_random_bytes,
    compute_hash,
    compute_hmac,
    generate_rsa_key_pair,
    generate_ec_key_pair,
    generate_csr,
)

# Certificate storage and management
from src.app.security.certificate_store import CertificateStore

# SSL/TLS configuration management
from src.app.security.ssl_manager import SSLManager, SSLConfig


# ---------------------------------------------------------------------------
# Public API — explicit listing of all names exported by this package
# ---------------------------------------------------------------------------
__all__ = [
    # SSL/TLS configuration management (from ssl_manager)
    "SSLManager",
    "SSLConfig",
    # Certificate storage (from certificate_store)
    "CertificateStore",
    # Cryptographic utilities (from crypto_utils)
    "generate_random_bytes",
    "compute_hash",
    "compute_hmac",
    "generate_rsa_key_pair",
    "generate_ec_key_pair",
    "generate_csr",
]
