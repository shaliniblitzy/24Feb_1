"""
SSL/TLS Certificate Management Package.

This package implements Feature F-302 (SSL/TLS Support) for the Nexus Repository
Flask application. It replaces BouncyCastle 1.78.1 and Java's javax.net.ssl
infrastructure from the original Java source system.

Modules:
- ssl_manager: SSL/TLS configuration management (TLS versions, cipher suites, keystores)
- certificate_store: Certificate storage, import/export, and expiration monitoring
- crypto_utils: Cryptographic utility functions (hashing, key generation, CSR, HMAC)

Primary dependency: cryptography 44.0.0 (PyPI)
"""

from src.app.security.crypto_utils import (
    generate_random_bytes,
    compute_hash,
    compute_hmac,
    generate_rsa_key_pair,
    generate_ec_key_pair,
    generate_csr,
)
from src.app.security.certificate_store import CertificateStore

# SSLManager and SSLConfig will be available once ssl_manager.py is created.
# Import them conditionally to avoid ImportError during incremental build.
try:
    from src.app.security.ssl_manager import SSLManager, SSLConfig  # noqa: F401
except ImportError:
    SSLManager = None  # type: ignore[assignment,misc]
    SSLConfig = None  # type: ignore[assignment,misc]


__all__ = [
    "SSLManager",
    "SSLConfig",
    "CertificateStore",
    "generate_random_bytes",
    "compute_hash",
    "compute_hmac",
    "generate_rsa_key_pair",
    "generate_ec_key_pair",
    "generate_csr",
]
