"""
Security package for the Nexus Repository Flask application.

Provides SSL/TLS certificate management and cryptographic operations
(Feature F-302). Replaces BouncyCastle 1.78.1 from the Java source.

Modules:
- ``crypto_utils.py``:       Cryptographic operations (RSA, AES-GCM, HMAC, PBKDF2)
- ``ssl_manager.py``:        SSL/TLS configuration management (future checkpoint)
- ``certificate_store.py``:  Certificate storage and retrieval (future checkpoint)
"""
