"""
Cryptographic utility functions for the Flask Binary Repository Management System.

Provides hash computation (SHA-1, SHA-256, MD5), checksum validation for
artifact integrity verification, and encryption/decryption utilities for
sensitive data protection.  Used extensively by the storage service layer
(Features F-201 File BlobStore and F-202 S3 BlobStore) for artifact integrity.

Functions:
    compute_sha1          — Compute SHA-1 hex digest of bytes content
    compute_sha256        — Compute SHA-256 hex digest of bytes content
    compute_md5           — Compute MD5 hex digest of bytes content
    compute_hash          — Generic hash computation with algorithm selection
    validate_checksum     — Verify computed hash matches an expected value
    generate_checksum_set — Generate dict containing SHA-1, SHA-256, MD5 hashes
    encrypt_value         — Encrypt a plaintext string using Fernet symmetric encryption
    decrypt_value         — Decrypt a ciphertext string using Fernet symmetric encryption
    generate_key          — Generate a new Fernet-compatible encryption key
    compute_file_hash     — Compute hash from a file path or file-like object
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import BinaryIO, Dict, Union

from cryptography.fernet import Fernet, InvalidToken


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_ALGORITHMS: Dict[str, int] = {
    "sha1": 40,
    "sha256": 64,
    "md5": 32,
}
"""Mapping of supported hash algorithm names to their hex-digest lengths."""

FILE_READ_CHUNK_SIZE: int = 8192
"""Chunk size in bytes used when reading files for incremental hashing."""


# ---------------------------------------------------------------------------
# Individual hash functions
# ---------------------------------------------------------------------------


def compute_sha1(data: bytes) -> str:
    """Compute the SHA-1 hex digest of *data*.

    Parameters
    ----------
    data : bytes
        The binary content to hash.

    Returns
    -------
    str
        A 40-character lowercase hexadecimal string.

    Raises
    ------
    TypeError
        If *data* is not ``bytes`` or ``bytearray``.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(
            f"Expected bytes or bytearray, got {type(data).__name__}"
        )
    return hashlib.sha1(data).hexdigest()


def compute_sha256(data: bytes) -> str:
    """Compute the SHA-256 hex digest of *data*.

    Parameters
    ----------
    data : bytes
        The binary content to hash.

    Returns
    -------
    str
        A 64-character lowercase hexadecimal string.

    Raises
    ------
    TypeError
        If *data* is not ``bytes`` or ``bytearray``.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(
            f"Expected bytes or bytearray, got {type(data).__name__}"
        )
    return hashlib.sha256(data).hexdigest()


def compute_md5(data: bytes) -> str:
    """Compute the MD5 hex digest of *data*.

    Parameters
    ----------
    data : bytes
        The binary content to hash.

    Returns
    -------
    str
        A 32-character lowercase hexadecimal string.

    Raises
    ------
    TypeError
        If *data* is not ``bytes`` or ``bytearray``.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(
            f"Expected bytes or bytearray, got {type(data).__name__}"
        )
    return hashlib.md5(data).hexdigest()


# ---------------------------------------------------------------------------
# Generic hash computation
# ---------------------------------------------------------------------------


def compute_hash(data: bytes, algorithm: str = "sha256") -> str:
    """Compute the hex digest of *data* using the specified *algorithm*.

    Parameters
    ----------
    data : bytes
        The binary content to hash.
    algorithm : str
        One of ``'sha1'``, ``'sha256'``, or ``'md5'`` (case-insensitive).

    Returns
    -------
    str
        The lowercase hexadecimal hash string whose length depends on the
        chosen algorithm.

    Raises
    ------
    TypeError
        If *data* is not ``bytes`` or ``bytearray``.
    ValueError
        If *algorithm* is not one of the supported algorithms.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(
            f"Expected bytes or bytearray, got {type(data).__name__}"
        )
    algo = algorithm.lower()
    if algo not in SUPPORTED_ALGORITHMS:
        raise ValueError(
            f"Unsupported hash algorithm: '{algorithm}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_ALGORITHMS))}"
        )
    h = hashlib.new(algo)
    h.update(data)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Checksum validation
# ---------------------------------------------------------------------------


def validate_checksum(
    data: bytes,
    expected_hash: str,
    algorithm: str = "sha256",
) -> bool:
    """Verify that the hash of *data* matches *expected_hash*.

    Comparison is case-insensitive.  Docker-style prefixed hashes
    (e.g. ``"sha256:abcdef..."`` ) are supported — the prefix is stripped
    before comparison if it matches a known algorithm name.

    Parameters
    ----------
    data : bytes
        The binary content to verify.
    expected_hash : str
        The expected hex digest string.
    algorithm : str
        The hashing algorithm to use (default ``'sha256'``).

    Returns
    -------
    bool
        ``True`` if the computed hash matches *expected_hash*, ``False``
        otherwise (including when *expected_hash* is empty).

    Raises
    ------
    TypeError
        If *data* is not ``bytes``/``bytearray`` or *expected_hash* is not
        a string.
    ValueError
        If *algorithm* is not supported.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(
            f"Expected bytes for data, got {type(data).__name__}"
        )
    if not isinstance(expected_hash, str):
        raise TypeError(
            f"Expected string for expected_hash, got {type(expected_hash).__name__}"
        )
    if not expected_hash:
        return False

    # Strip known algorithm prefixes (e.g. "sha256:abc123...")
    clean_hash = expected_hash
    for prefix in SUPPORTED_ALGORITHMS:
        tag = f"{prefix}:"
        if clean_hash.startswith(tag):
            clean_hash = clean_hash[len(tag):]
            break

    computed = compute_hash(data, algorithm)
    return computed.lower() == clean_hash.lower()


# ---------------------------------------------------------------------------
# Checksum set generation
# ---------------------------------------------------------------------------


def generate_checksum_set(data: bytes) -> Dict[str, str]:
    """Compute SHA-1, SHA-256 and MD5 hashes for *data* in a single pass.

    Parameters
    ----------
    data : bytes
        The binary content to hash.

    Returns
    -------
    dict
        A dictionary with keys ``'sha1'``, ``'sha256'``, ``'md5'`` whose
        values are the corresponding lowercase hex digest strings.

    Raises
    ------
    TypeError
        If *data* is not ``bytes`` or ``bytearray``.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(
            f"Expected bytes or bytearray, got {type(data).__name__}"
        )
    return {
        "sha1": compute_sha1(data),
        "sha256": compute_sha256(data),
        "md5": compute_md5(data),
    }


# ---------------------------------------------------------------------------
# Encryption utilities (Fernet symmetric encryption)
# ---------------------------------------------------------------------------


def encrypt_value(plaintext: str, key: bytes) -> str:
    """Encrypt *plaintext* using Fernet symmetric encryption.

    Parameters
    ----------
    plaintext : str
        The string to encrypt.
    key : bytes
        A valid Fernet key (use :func:`generate_key` to create one).

    Returns
    -------
    str
        The encrypted ciphertext as a URL-safe base64-encoded string.

    Raises
    ------
    TypeError
        If *plaintext* is not a ``str`` or *key* is not ``bytes``.
    """
    if not isinstance(plaintext, str):
        raise TypeError(
            f"Expected string for plaintext, got {type(plaintext).__name__}"
        )
    if not isinstance(key, bytes):
        raise TypeError(
            f"Expected bytes for key, got {type(key).__name__}"
        )
    f = Fernet(key)
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(ciphertext: str, key: bytes) -> str:
    """Decrypt *ciphertext* using Fernet symmetric encryption.

    Parameters
    ----------
    ciphertext : str
        The encrypted string produced by :func:`encrypt_value`.
    key : bytes
        The same Fernet key that was used for encryption.

    Returns
    -------
    str
        The original plaintext string.

    Raises
    ------
    TypeError
        If *ciphertext* is not a ``str`` or *key* is not ``bytes``.
    cryptography.fernet.InvalidToken
        If *key* is incorrect or the ciphertext has been tampered with.
    """
    if not isinstance(ciphertext, str):
        raise TypeError(
            f"Expected string for ciphertext, got {type(ciphertext).__name__}"
        )
    if not isinstance(key, bytes):
        raise TypeError(
            f"Expected bytes for key, got {type(key).__name__}"
        )
    f = Fernet(key)
    return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def generate_key() -> bytes:
    """Generate a new Fernet encryption key.

    Returns
    -------
    bytes
        A 44-byte URL-safe base64-encoded key suitable for use with
        :func:`encrypt_value` and :func:`decrypt_value`.
    """
    return Fernet.generate_key()


# ---------------------------------------------------------------------------
# File-based hashing
# ---------------------------------------------------------------------------


def compute_file_hash(
    source: Union[str, Path, BinaryIO],
    algorithm: str = "sha256",
) -> str:
    """Compute the hex digest of a file or file-like object.

    For large files the content is read in chunks of
    :data:`FILE_READ_CHUNK_SIZE` bytes to limit memory usage.

    Parameters
    ----------
    source : str | Path | BinaryIO
        A filesystem path (string or ``Path``) or a readable binary
        file-like object (e.g. ``io.BytesIO``).
    algorithm : str
        Hash algorithm name (default ``'sha256'``).

    Returns
    -------
    str
        The lowercase hexadecimal hash string.

    Raises
    ------
    TypeError
        If *source* is not a recognised type.
    ValueError
        If *algorithm* is not supported.
    FileNotFoundError
        If *source* is a path that does not exist.
    """
    algo = algorithm.lower()
    if algo not in SUPPORTED_ALGORITHMS:
        raise ValueError(
            f"Unsupported hash algorithm: '{algorithm}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_ALGORITHMS))}"
        )

    h = hashlib.new(algo)

    if isinstance(source, (str, Path)):
        with open(source, "rb") as fh:
            while True:
                chunk = fh.read(FILE_READ_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
    elif hasattr(source, "read"):
        if hasattr(source, "seek"):
            source.seek(0)
        while True:
            chunk = source.read(FILE_READ_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    else:
        raise TypeError(
            f"Expected file path or file-like object, got {type(source).__name__}"
        )

    return h.hexdigest()
