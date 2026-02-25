"""
Nexus Repository Manager — Content-Addressable Hash Computation

Provides SHA-1, SHA-256, and MD5 hash functions for blob deduplication
and artifact integrity verification. This is the most foundational utility
module in the application — it has ZERO external or internal dependencies,
using only Python's standard library.

Architecture:
    - SHA-256 is the PRIMARY algorithm for content-addressable blob storage
      (per AAP Section 0.7.4: Binary artifacts must be stored using
      content-based addressing via SHA-256 hash)
    - SHA-1 and MD5 are provided for format-specific compatibility:
      * Maven requires .sha1 and .md5 sidecar files
      * Docker uses SHA-256 digests for manifest/blob verification
      * npm, NuGet, and PyPI use SHA-256 for package integrity
    - All functions support both bytes objects and file-like objects (BinaryIO),
      enabling streaming hash computation for large artifacts (Docker images,
      Maven JARs) without loading them entirely into memory
    - All functions are stateless and thread-safe for multi-worker Gunicorn
      deployment

Consumers:
    - src/storage/blobstore/file_blobstore.py — Content-addressable storage
    - src/storage/blobstore/s3_blobstore.py — Content verification after S3 upload
    - src/repositories/formats/maven.py — Checksum verification (SHA-1, MD5)
    - src/repositories/formats/npm.py — Package integrity (SHA-256/SHA-512)
    - src/repositories/formats/docker.py — Digest verification (SHA-256)
    - src/repositories/formats/nuget.py — Package hash verification
    - src/repositories/formats/pypi.py — Package hash verification (SHA-256)
    - src/storage/maintenance.py — Integrity verification of stored blobs
"""

import hashlib
import io
import logging
from typing import BinaryIO, Dict, Optional, Union

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default chunk size for streaming hash computation.
# 8 KB provides a good balance between memory usage and I/O efficiency
# for the typical mix of small metadata files and large binary artifacts.
DEFAULT_CHUNK_SIZE: int = 8192

# Tuple of hash algorithm names supported by this module.
# Order: sha1, sha256, md5 — matching the most common usage patterns
# across all supported repository formats.
SUPPORTED_ALGORITHMS: tuple = ('sha1', 'sha256', 'md5')


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _compute_hash(
    algorithm: str,
    data: Union[bytes, BinaryIO],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> str:
    """Compute a single hash digest for the given data.

    Handles bytes objects, bytearray, memoryview, and file-like objects
    transparently.  File-like objects are read in chunks so that arbitrarily
    large artifacts can be hashed without loading them into memory.

    Args:
        algorithm: Hash algorithm name recognised by :func:`hashlib.new`
            (e.g. ``'sha256'``, ``'sha1'``, ``'md5'``).
        data: Raw bytes **or** a readable binary stream.  When a stream is
            provided it is consumed from the *current* file position — the
            caller is responsible for seeking to the desired position before
            calling this function.
        chunk_size: Number of bytes to read per iteration when *data* is a
            file-like object.  Defaults to :data:`DEFAULT_CHUNK_SIZE` (8 KB).

    Returns:
        Lowercase hexadecimal digest string.

    Raises:
        ValueError: If *algorithm* is not available in the current Python
            build's ``hashlib`` implementation.
    """
    try:
        hasher = hashlib.new(algorithm)
    except ValueError:
        logger.warning(
            "Unsupported hash algorithm requested: %s", algorithm,
        )
        raise

    if isinstance(data, bytes):
        hasher.update(data)
    elif isinstance(data, (bytearray, memoryview)):
        hasher.update(bytes(data))
    else:
        # Treat as a file-like object supporting .read(size)
        while True:
            chunk = data.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)

    digest = hasher.hexdigest()
    logger.debug(
        "Computed %s hash: %s (first 12 chars: %s)",
        algorithm,
        digest[:12] + "…",
        digest[:12],
    )
    return digest


# ---------------------------------------------------------------------------
# Single-algorithm convenience functions
# ---------------------------------------------------------------------------

def compute_sha256(data: Union[bytes, BinaryIO]) -> str:
    """Compute the SHA-256 hash of *data*.

    This is the **primary** hash algorithm for content-addressable blob
    storage.  Per AAP Section 0.7.4 every binary artifact in the BlobStore
    must be stored using its SHA-256 hash as the content address; blob
    deduplication is achieved through hash comparison before storage.

    Args:
        data: A ``bytes`` object **or** a binary file-like object.
            When a file-like object is supplied it is read in 8 KB chunks
            for memory efficiency.

    Returns:
        Lowercase hexadecimal SHA-256 digest (64 characters).

    Examples:
        >>> compute_sha256(b'hello world')
        'b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9'

        >>> import io
        >>> compute_sha256(io.BytesIO(b'hello world'))
        'b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9'
    """
    return _compute_hash('sha256', data)


def compute_sha1(data: Union[bytes, BinaryIO]) -> str:
    """Compute the SHA-1 hash of *data*.

    Required by the Maven repository format for checksum verification —
    Maven stores ``.sha1`` sidecar files alongside every artifact.

    Args:
        data: A ``bytes`` object or binary file-like object.

    Returns:
        Lowercase hexadecimal SHA-1 digest (40 characters).

    Examples:
        >>> compute_sha1(b'hello world')
        '2aae6c35c94fcfb415dbe95f408b9ce91ee846ed'
    """
    return _compute_hash('sha1', data)


def compute_md5(data: Union[bytes, BinaryIO]) -> str:
    """Compute the MD5 hash of *data*.

    Required by the Maven repository format for checksum verification —
    Maven stores ``.md5`` sidecar files alongside every artifact.  Also used
    for legacy compatibility with some older artifact formats.

    .. note::

        MD5 is **cryptographically broken** for security purposes but is still
        required by format specifications for integrity checking.  Do **not**
        use MD5 for any security-sensitive operation.

    Args:
        data: A ``bytes`` object or binary file-like object.

    Returns:
        Lowercase hexadecimal MD5 digest (32 characters).

    Examples:
        >>> compute_md5(b'hello world')
        '5eb63bbbe01eeed093cb22bb8f5acdc3'
    """
    return _compute_hash('md5', data)


# ---------------------------------------------------------------------------
# Multi-algorithm streaming computation
# ---------------------------------------------------------------------------

def compute_hashes_streaming(
    file_obj: BinaryIO,
    algorithms: Optional[tuple] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Dict[str, str]:
    """Compute multiple hash digests simultaneously in a single streaming pass.

    This is the most efficient way to obtain several checksums for a large
    file because the data is read from disk (or network) **only once** and
    every hash object is updated with each chunk in parallel.

    Critical for BlobStore operations where both SHA-256 (content addressing)
    and SHA-1 / MD5 (Maven format compatibility) are needed.

    .. important::

        The file position is **not** reset after reading.  If callers need
        to read the file again they must call ``file_obj.seek(0)`` themselves.

    Args:
        file_obj: A binary file-like object that supports ``.read(size)``.
        algorithms: A tuple of algorithm names to compute.  Defaults to
            ``SUPPORTED_ALGORITHMS`` (``('sha1', 'sha256', 'md5')``) when
            *None*.
        chunk_size: Number of bytes to read per iteration.  Defaults to
            :data:`DEFAULT_CHUNK_SIZE` (8 KB).

    Returns:
        A dictionary mapping each algorithm name to its lowercase hexadecimal
        digest string, e.g.::

            {
                'sha1':   '2aae6c35c94fcfb415dbe95f408b9ce91ee846ed',
                'sha256': 'b94d27b9934d3e08a52e52d7da7dabfac484efe37a...',
                'md5':    '5eb63bbbe01eeed093cb22bb8f5acdc3',
            }

    Raises:
        ValueError: If any algorithm name is not available.

    Examples:
        >>> import io
        >>> hashes = compute_hashes_streaming(io.BytesIO(b'test'))
        >>> sorted(hashes.keys())
        ['md5', 'sha1', 'sha256']

        >>> hashes = compute_hashes_streaming(
        ...     io.BytesIO(b'test'), algorithms=('sha256',)
        ... )
        >>> list(hashes.keys())
        ['sha256']
    """
    if algorithms is None:
        algorithms = SUPPORTED_ALGORITHMS

    # Validate and create hash objects for every requested algorithm
    hashers: Dict[str, "hashlib._Hash"] = {}
    for alg in algorithms:
        try:
            hashers[alg] = hashlib.new(alg)
        except ValueError:
            logger.warning("Unsupported hash algorithm requested: %s", alg)
            raise

    # Stream through the file, updating all hashers with each chunk
    bytes_processed = 0
    while True:
        chunk = file_obj.read(chunk_size)
        if not chunk:
            break
        for hasher in hashers.values():
            hasher.update(chunk)
        bytes_processed += len(chunk)

    logger.debug(
        "Streamed %d bytes through %d hash algorithms: %s",
        bytes_processed,
        len(hashers),
        ", ".join(algorithms),
    )

    return {alg: hasher.hexdigest() for alg, hasher in hashers.items()}


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def verify_hash(
    data: Union[bytes, BinaryIO],
    expected_hash: str,
    algorithm: str = 'sha256',
) -> bool:
    """Verify that *data* matches an expected hash digest.

    Used for artifact integrity verification after download or storage.
    The comparison is **case-insensitive** so that callers do not need to
    normalise the expected hash beforehand.

    Args:
        data: A ``bytes`` object or binary file-like object to verify.
        expected_hash: The expected hexadecimal digest string.
        algorithm: Hash algorithm to use.  Defaults to ``'sha256'``.

    Returns:
        ``True`` if the computed hash matches *expected_hash*; ``False``
        otherwise.

    Examples:
        >>> verify_hash(b'hello world',
        ...     'b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9')
        True

        >>> verify_hash(b'hello world', 'wrong_hash')
        False
    """
    computed = _compute_hash(algorithm, data)
    match = computed.lower() == expected_hash.lower()
    if not match:
        logger.debug(
            "Hash mismatch (%s): computed=%s expected=%s",
            algorithm,
            computed[:16] + "…",
            expected_hash[:16] + "…",
        )
    return match


def verify_checksums(
    file_obj: BinaryIO,
    expected_checksums: Dict[str, str],
) -> Dict[str, bool]:
    """Verify multiple checksums for a file in a single streaming pass.

    Computes all requested hashes simultaneously via
    :func:`compute_hashes_streaming` and compares each against the
    corresponding expected value.

    Args:
        file_obj: A binary file-like object to verify.
        expected_checksums: A dictionary mapping algorithm names to their
            expected hexadecimal digest strings, e.g.
            ``{'sha256': 'abc…', 'md5': 'def…'}``.

    Returns:
        A dictionary mapping each algorithm name to a boolean indicating
        whether the computed digest matches the expected one, e.g.
        ``{'sha256': True, 'md5': False}``.

    Examples:
        >>> import io
        >>> data = b'hello world'
        >>> checksums = {
        ...     'sha256': 'b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9',
        ...     'md5': '5eb63bbbe01eeed093cb22bb8f5acdc3',
        ... }
        >>> verify_checksums(io.BytesIO(data), checksums)
        {'sha256': True, 'md5': True}
    """
    if not expected_checksums:
        logger.debug("verify_checksums called with empty expected_checksums")
        return {}

    algorithms = tuple(expected_checksums.keys())
    computed = compute_hashes_streaming(file_obj, algorithms=algorithms)

    results: Dict[str, bool] = {}
    for alg in algorithms:
        match = computed[alg].lower() == expected_checksums[alg].lower()
        if not match:
            logger.debug(
                "Checksum mismatch (%s): computed=%s expected=%s",
                alg,
                computed[alg][:16] + "…",
                expected_checksums[alg][:16] + "…",
            )
        results[alg] = match

    return results


# ---------------------------------------------------------------------------
# Content-addressable blob ID generation
# ---------------------------------------------------------------------------

def generate_blob_id(data: Union[bytes, BinaryIO]) -> str:
    """Generate a content-addressable blob ID using SHA-256.

    Per AAP Section 0.7.4 binary artifacts in the BlobStore must be stored
    using content-based addressing (SHA-256 hash).  Blob deduplication is
    achieved by comparing blob IDs before storage — if two uploads produce
    the same blob ID they reference the same content and need not be stored
    twice.

    Args:
        data: A ``bytes`` object or binary file-like object.

    Returns:
        A 64-character lowercase hexadecimal SHA-256 digest string suitable
        for use as a unique blob identifier.

    Examples:
        >>> generate_blob_id(b'binary artifact content')  # doctest: +SKIP
        '3a7bd3e2...'  # 64-character SHA-256 hex string

        >>> import io
        >>> blob_id = generate_blob_id(io.BytesIO(b'artifact'))
        >>> len(blob_id)
        64
    """
    return compute_sha256(data)
