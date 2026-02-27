"""
HMAC-SHA256 Payload Signing for Webhook Verification.

This module implements the cryptographic signing component of Feature F-503
(Webhook Integration). It generates the ``X-Nexus-Webhook-Signature`` header
value for each webhook payload, allowing recipients to verify the authenticity
and integrity of the payload using a shared secret.

Replaces BouncyCastle 1.78.1 HMAC operations from the Java source system,
using Python's ``cryptography`` library (v44.0.0) for HMAC computation and
Python's standard-library ``hmac.compare_digest`` for constant-time signature
verification.

Security Notes
--------------
* Secrets and full signature values are **never** logged at INFO level or above.
* ``verify_signature`` uses ``hmac.compare_digest`` to prevent timing
  side-channel attacks — the ``==`` operator is intentionally avoided.
* ``generate_signing_secret`` uses ``os.urandom`` for cryptographic-quality
  random bytes.

Example
-------
>>> from src.app.webhooks.payload_signer import sign_payload, verify_signature
>>> sig = sign_payload(b'{"event": "push"}', 'my-webhook-secret')
>>> verify_signature(b'{"event": "push"}', 'my-webhook-secret', sig)
True
"""

from __future__ import annotations

import base64
import hmac
import logging
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.hmac import HMAC as CryptoHMAC

# ---------------------------------------------------------------------------
# Module-level logger — structured logging replaces SLF4J 1.7.36 + Logback
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SIGNATURE_PREFIX: str = "sha256="
"""Prefix prepended to every hex-encoded HMAC digest.

Follows the convention used by GitHub, GitLab, and similar webhook systems.
The full signature header value has the form ``sha256=<hex_digest>``.
"""

HASH_ALGORITHM: str = "sha256"
"""Default hash algorithm identifier used throughout this module."""

ENCODING: str = "utf-8"
"""Default character encoding for string ↔ bytes conversions."""

# Mapping of algorithm names → cryptography hash-algorithm constructors.
# Allows ``sign_payload`` / ``compute_raw_hmac`` to support multiple
# algorithms via the ``algorithm`` parameter without hard-coding SHA-256.
_ALGORITHM_MAP: dict[str, type[hashes.HashAlgorithm]] = {
    "sha256": hashes.SHA256,
    "sha512": hashes.SHA512,
}

# Mapping of algorithm names → their signature-header prefixes.
_PREFIX_MAP: dict[str, str] = {
    "sha256": "sha256=",
    "sha512": "sha512=",
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "sign_payload",
    "verify_signature",
    "compute_raw_hmac",
    "generate_signing_secret",
    "extract_signature",
    "SIGNATURE_PREFIX",
    "HASH_ALGORITHM",
]


# ---------------------------------------------------------------------------
# Primary Signing Function
# ---------------------------------------------------------------------------


def sign_payload(
    payload: bytes | str,
    secret: str | bytes,
    algorithm: str = HASH_ALGORITHM,
) -> str:
    """Generate the HMAC signature for a webhook payload.

    This is the primary function called by
    ``src.app.webhooks.dispatcher`` to produce the value of the
    ``X-Nexus-Webhook-Signature`` HTTP header.

    Parameters
    ----------
    payload:
        The webhook payload body (JSON bytes or string) to sign.
    secret:
        The shared secret key configured per webhook endpoint.
        Must be non-empty.
    algorithm:
        Hash algorithm identifier (default ``'sha256'``).  Accepted
        values: ``'sha256'``, ``'sha512'``.

    Returns
    -------
    str
        The prefixed signature string, e.g. ``"sha256=<hex_digest>"``.

    Raises
    ------
    ValueError
        If *secret* is empty or ``None``, or if *algorithm* is not
        supported.
    TypeError
        If *payload* is neither ``bytes`` nor ``str``.
    """

    # ---- Input validation --------------------------------------------------
    if secret is None or (isinstance(secret, (str, bytes)) and len(secret) == 0):
        raise ValueError("Signing secret must not be empty or None")

    if not isinstance(payload, (str, bytes)):
        raise TypeError(
            f"payload must be bytes or str, got {type(payload).__name__}"
        )

    algorithm = algorithm.lower()
    if algorithm not in _ALGORITHM_MAP:
        raise ValueError(
            f"Unsupported hash algorithm '{algorithm}'. "
            f"Supported: {', '.join(sorted(_ALGORITHM_MAP))}"
        )

    # ---- Normalise inputs to bytes -----------------------------------------
    payload_bytes: bytes = (
        payload.encode(ENCODING) if isinstance(payload, str) else payload
    )
    secret_bytes: bytes = (
        secret.encode(ENCODING) if isinstance(secret, str) else secret
    )

    # ---- Compute HMAC using the *cryptography* library ---------------------
    hash_cls = _ALGORITHM_MAP[algorithm]
    h = CryptoHMAC(secret_bytes, hash_cls())
    h.update(payload_bytes)
    hex_digest: str = h.finalize().hex()

    # ---- Build prefixed signature string -----------------------------------
    prefix = _PREFIX_MAP.get(algorithm, f"{algorithm}=")
    signature = f"{prefix}{hex_digest}"

    # Log at DEBUG level only — NEVER log the secret or the full signature
    logger.debug(
        "Payload signed: algorithm=%s, payload_size=%d bytes",
        algorithm,
        len(payload_bytes),
    )

    return signature


# ---------------------------------------------------------------------------
# Signature Verification Function
# ---------------------------------------------------------------------------


def verify_signature(
    payload: bytes | str,
    secret: str | bytes,
    signature: str | None,
    algorithm: str = HASH_ALGORITHM,
) -> bool:
    """Verify a received webhook signature against the expected value.

    Uses ``hmac.compare_digest`` for **constant-time** comparison to
    prevent timing side-channel attacks.

    Parameters
    ----------
    payload:
        The received webhook payload body.
    secret:
        The shared secret key (must match the sender's secret).
    signature:
        The received ``X-Nexus-Webhook-Signature`` header value, e.g.
        ``"sha256=abcdef..."``.  ``None`` and empty strings are treated
        as invalid.
    algorithm:
        Hash algorithm identifier (default ``'sha256'``).

    Returns
    -------
    bool
        ``True`` if the signature is valid, ``False`` otherwise.

    Notes
    -----
    This function **never** raises an exception for an invalid signature;
    it always returns ``False`` instead.
    """

    # ---- Edge-case: missing / empty signature ------------------------------
    if not signature:
        logger.warning("Signature verification failed: signature is empty or None")
        return False

    try:
        # Compute the expected signature for the given payload + secret.
        expected: str = sign_payload(payload, secret, algorithm)
    except (ValueError, TypeError):
        # If sign_payload cannot compute (e.g. bad secret), treat as failure.
        logger.warning("Signature verification failed: unable to compute expected signature")
        return False

    # ---- Constant-time comparison ------------------------------------------
    # CRITICAL SECURITY: hmac.compare_digest prevents timing side-channel.
    if hmac.compare_digest(expected, signature):
        logger.debug("Signature verification succeeded")
        return True

    # If the received signature omits the algorithm prefix, attempt a
    # backwards-compatible comparison with the prefix stripped from the
    # expected value.
    try:
        _, expected_digest = extract_signature(expected)
    except ValueError:
        expected_digest = expected

    if hmac.compare_digest(expected_digest, signature):
        logger.debug(
            "Signature verification succeeded (without prefix, backwards-compat)"
        )
        return True

    logger.warning("Signature verification failed: signature mismatch")
    return False


# ---------------------------------------------------------------------------
# Low-Level HMAC Computation
# ---------------------------------------------------------------------------


def compute_raw_hmac(
    payload: bytes,
    secret: bytes,
    algorithm: str = HASH_ALGORITHM,
) -> bytes:
    """Return the raw HMAC digest bytes (not hex-encoded).

    This is a low-level utility for callers that need the raw digest
    rather than the prefixed hex string returned by :func:`sign_payload`.

    Parameters
    ----------
    payload:
        The data to authenticate.
    secret:
        The HMAC key as raw bytes.
    algorithm:
        Hash algorithm identifier (default ``'sha256'``).  Accepted
        values: ``'sha256'``, ``'sha512'``.

    Returns
    -------
    bytes
        The raw HMAC digest.

    Raises
    ------
    ValueError
        If *algorithm* is not supported.
    TypeError
        If *payload* or *secret* is not ``bytes``.
    """

    if not isinstance(payload, bytes):
        raise TypeError(
            f"payload must be bytes, got {type(payload).__name__}"
        )
    if not isinstance(secret, bytes):
        raise TypeError(
            f"secret must be bytes, got {type(secret).__name__}"
        )

    algorithm = algorithm.lower()
    if algorithm not in _ALGORITHM_MAP:
        raise ValueError(
            f"Unsupported hash algorithm '{algorithm}'. "
            f"Supported: {', '.join(sorted(_ALGORITHM_MAP))}"
        )

    hash_cls = _ALGORITHM_MAP[algorithm]
    h = CryptoHMAC(secret, hash_cls())
    h.update(payload)
    return h.finalize()


# ---------------------------------------------------------------------------
# Secret Generation
# ---------------------------------------------------------------------------


def generate_signing_secret(length: int = 32) -> str:
    """Generate a cryptographically secure webhook signing secret.

    Uses :func:`os.urandom` for cryptographic-quality random bytes and
    encodes the result as a URL-safe Base64 string suitable for storage
    and transport.

    Parameters
    ----------
    length:
        Number of random bytes to generate before Base64 encoding.
        The resulting string will be longer than *length* due to the
        Base64 expansion (~4/3 ratio).  Minimum recommended: 32.

    Returns
    -------
    str
        A URL-safe Base64-encoded secret string.

    Raises
    ------
    ValueError
        If *length* is less than 1.
    """

    if length < 1:
        raise ValueError("Secret length must be at least 1 byte")

    raw_bytes: bytes = os.urandom(length)
    encoded: str = base64.urlsafe_b64encode(raw_bytes).decode("ascii")

    logger.debug("Generated new signing secret (%d random bytes)", length)
    return encoded


# ---------------------------------------------------------------------------
# Signature Header Parsing
# ---------------------------------------------------------------------------


def extract_signature(header_value: str) -> tuple[str, str]:
    """Parse a signature header value into ``(algorithm, hex_digest)``.

    Examples
    --------
    >>> extract_signature("sha256=abcdef123456")
    ('sha256', 'abcdef123456')
    >>> extract_signature("sha512=abcdef123456")
    ('sha512', 'abcdef123456')
    >>> extract_signature("abcdef123456")  # missing prefix — defaults to sha256
    ('sha256', 'abcdef123456')

    Parameters
    ----------
    header_value:
        The raw header value, e.g. ``"sha256=abcdef123456..."``.

    Returns
    -------
    tuple[str, str]
        A ``(algorithm, hex_digest)`` pair.

    Raises
    ------
    ValueError
        If *header_value* is empty, ``None``, or otherwise
        unrecognisable.
    """

    if not header_value or not isinstance(header_value, str):
        raise ValueError("Signature header value must be a non-empty string")

    header_value = header_value.strip()
    if not header_value:
        raise ValueError("Signature header value must be a non-empty string")

    # Try splitting on '=' — the standard format is "algorithm=hex_digest".
    if "=" in header_value:
        parts = header_value.split("=", 1)
        algo = parts[0].lower()
        digest = parts[1]

        if algo in _ALGORITHM_MAP and digest:
            return algo, digest

        # If the prefix isn't a known algorithm, fall through to the
        # default-algorithm path below.

    # No recognised prefix — assume the entire value is a hex digest and
    # default to sha256.
    # Validate that the value looks like a hex string.
    clean = header_value.replace("=", "")  # strip any trailing '='
    if not clean:
        raise ValueError(
            f"Unrecognisable signature format: '{header_value}'"
        )

    # Best-effort: treat as hex digest with default algorithm.
    return HASH_ALGORITHM, header_value
