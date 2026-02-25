"""
Secure Password Hashing Utilities for Nexus Repository.

This module replaces BouncyCastle 1.78.1 credential hashing from the Java
source system (AAP Section 0.1.2). It provides enterprise-grade password
hashing, constant-time verification, strength validation, and cryptographic
key generation using the ``cryptography`` Python library (v44.0.0) and
optional ``bcrypt`` support.

This is a **standalone utility module** with ZERO Flask dependencies — it
can be used by any module that needs password or secret-key operations.

Key design decisions
--------------------
* **bcrypt** is the preferred (DEFAULT_ALGORITHM) password hash — it is
  the industry standard and is constant-time by design.
* **scrypt** (via ``cryptography.hazmat.primitives.kdf.scrypt.Scrypt``) is
  the automatic fallback when bcrypt is unavailable.
* All verification paths use **timing-safe comparison** to prevent
  side-channel attacks (``hmac.compare_digest`` for scrypt,
  ``bcrypt.checkpw`` for bcrypt).
* Plaintext passwords are **never** stored, logged, or returned by any
  function in this module.
* API key and secret key generation use the ``secrets`` module which draws
  from the operating system's cryptographically secure random source.

Feature coverage
----------------
* F-301 — Role-Based Access Control (password hashing for local auth)
* F-304 — API Key Authentication (``generate_api_key``)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets


from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.exceptions import InvalidKey

# ---------------------------------------------------------------------------
# Module-level logger (replaces SLF4J 1.7.36 from the Java source)
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conditional bcrypt import — graceful fallback to scrypt when unavailable
# ---------------------------------------------------------------------------
try:
    import bcrypt

    BCRYPT_AVAILABLE: bool = True
except ImportError:  # pragma: no cover
    BCRYPT_AVAILABLE = False
    logger.info(
        "bcrypt library not available; scrypt will be used as the "
        "password hashing algorithm."
    )

# ---------------------------------------------------------------------------
# Password hashing constants
# ---------------------------------------------------------------------------

# Default algorithm preference: bcrypt (primary), scrypt (fallback)
DEFAULT_ALGORITHM: str = "bcrypt"
FALLBACK_ALGORITHM: str = "scrypt"

# bcrypt cost factor — 2^12 = 4,096 iterations (OWASP minimum recommendation)
BCRYPT_ROUNDS: int = 12

# scrypt parameters — OWASP recommended values
SCRYPT_SALT_LENGTH: int = 32   # 256-bit random salt
SCRYPT_KEY_LENGTH: int = 64    # 512-bit derived key
SCRYPT_N: int = 2 ** 14        # CPU/memory cost parameter (16,384)
SCRYPT_R: int = 8              # block size
SCRYPT_P: int = 1              # parallelisation factor

# Minimum acceptable bcrypt cost factor — hashes below this trigger rehash
_MIN_BCRYPT_ROUNDS: int = 10

# Weak / commonly-known passwords that must always be rejected
_COMMON_WEAK_PASSWORDS: frozenset[str] = frozenset(
    {
        "password",
        "123456",
        "12345678",
        "qwerty",
        "abc123",
        "monkey",
        "1234567",
        "letmein",
        "trustno1",
        "dragon",
        "baseball",
        "iloveyou",
        "master",
        "sunshine",
        "ashley",
        "bailey",
        "shadow",
        "passw0rd",
        "123456789",
        "654321",
        "superman",
        "qazwsx",
        "michael",
        "football",
        "password1",
        "password123",
        "welcome",
        "admin",
        "login",
        "starwars",
    }
)

# ---------------------------------------------------------------------------
# Hash format documentation
# ---------------------------------------------------------------------------
# bcrypt stores the algorithm, cost, salt, and hash in its own standard
# Modular Crypt Format string:
#   $2b$12$<22-char salt><31-char hash>
#
# scrypt uses a custom format:
#   $scrypt$n=<N>,r=<R>,p=<P>$<salt_b64>$<key_b64>
# ---------------------------------------------------------------------------


# ===================================================================
# Core Hashing Functions
# ===================================================================


def hash_password(
    password: str,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """Hash a plaintext password and return a storable hash string.

    The returned string self-describes its algorithm and parameters so
    that ``verify_password`` can later auto-detect the correct
    verification strategy.

    Parameters
    ----------
    password:
        The plaintext password to hash. Must not be empty.
    algorithm:
        Hashing algorithm to use — ``'bcrypt'`` (default) or
        ``'scrypt'``.  If bcrypt is requested but unavailable the
        function transparently falls back to scrypt.

    Returns
    -------
    str
        The formatted password hash string suitable for storage in
        ``User.password_hash``.

    Raises
    ------
    ValueError
        If *password* is empty or *algorithm* is unknown.
    """

    if not password:
        raise ValueError("Password must not be empty.")

    if algorithm == "bcrypt" and BCRYPT_AVAILABLE:
        return _hash_bcrypt(password)

    if algorithm == "scrypt" or (algorithm == "bcrypt" and not BCRYPT_AVAILABLE):
        # Transparently fall back to scrypt when bcrypt is unavailable
        if algorithm == "bcrypt":
            logger.debug(
                "bcrypt unavailable; falling back to scrypt for password hashing."
            )
        return _hash_scrypt(password)

    raise ValueError(
        f"Unknown hashing algorithm '{algorithm}'. "
        f"Supported algorithms: 'bcrypt', 'scrypt'."
    )


def _hash_bcrypt(password: str) -> str:
    """Hash *password* using bcrypt with the configured cost factor.

    Uses ``bcrypt.gensalt`` for random salt generation and
    ``bcrypt.hashpw`` for hashing.
    """
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def _hash_scrypt(
    password: str,
    salt: bytes | None = None,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> str:
    """Hash *password* using scrypt via the ``cryptography`` library.

    Returns a self-describing hash string in the format:
    ``$scrypt$n=<N>,r=<R>,p=<P>$<salt_b64>$<key_b64>``
    """
    if salt is None:
        salt = os.urandom(SCRYPT_SALT_LENGTH)

    kdf = Scrypt(
        salt=salt,
        length=SCRYPT_KEY_LENGTH,
        n=n,
        r=r,
        p=p,
    )
    key = kdf.derive(password.encode("utf-8"))

    salt_b64 = base64.b64encode(salt).decode("utf-8")
    key_b64 = base64.b64encode(key).decode("utf-8")
    return f"$scrypt$n={n},r={r},p={p}${salt_b64}${key_b64}"


# ===================================================================
# Password Verification
# ===================================================================


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext *password* against a stored *password_hash*.

    The algorithm is auto-detected from the hash string format.
    Verification is performed using **constant-time comparison** to
    prevent timing-based side-channel attacks.

    Parameters
    ----------
    password:
        The plaintext password to verify.
    password_hash:
        The stored hash string (bcrypt or scrypt format).

    Returns
    -------
    bool
        ``True`` if the password matches, ``False`` otherwise.
    """

    if not password or not password_hash:
        return False

    try:
        if password_hash.startswith("$2") and BCRYPT_AVAILABLE:
            # bcrypt format ($2a$, $2b$, $2y$ variants)
            return bcrypt.checkpw(
                password.encode("utf-8"),
                password_hash.encode("utf-8"),
            )

        if password_hash.startswith("$scrypt$"):
            return _verify_scrypt(password, password_hash)

        logger.warning(
            "Unknown password hash format encountered (prefix: %s…).",
            password_hash[:6] if len(password_hash) > 6 else "short",
        )
        return False

    except BaseException:
        # Catch *all* exceptions including pyo3_runtime.PanicException
        # raised by the bcrypt Rust backend on malformed hash input.
        # Using BaseException because PanicException does not inherit
        # from Exception.  Any malformed hash or runtime error is
        # treated as "does not match".
        logger.debug(
            "Password verification failed with an exception.",
            exc_info=True,
        )
        return False


def _verify_scrypt(password: str, password_hash: str) -> bool:
    """Verify *password* against a scrypt hash string.

    Parses the hash format ``$scrypt$n=<N>,r=<R>,p=<P>$<salt_b64>$<key_b64>``
    and uses ``Scrypt.verify()`` for constant-time safe comparison.
    Falls back to manual derivation with ``hmac.compare_digest`` if
    ``verify()`` is not available.
    """

    try:
        parts = password_hash.split("$")
        # Expected parts: ['', 'scrypt', 'n=...,r=...,p=...', '<salt_b64>', '<key_b64>']
        if len(parts) != 5 or parts[1] != "scrypt":
            logger.warning("Malformed scrypt hash string.")
            return False

        param_str = parts[2]
        salt_b64 = parts[3]
        stored_key_b64 = parts[4]

        # Parse parameters
        params = _parse_scrypt_params(param_str)
        n = params["n"]
        r = params["r"]
        p = params["p"]

        salt = base64.b64decode(salt_b64)
        stored_key = base64.b64decode(stored_key_b64)

        # Primary path: use Scrypt.verify() which performs constant-time
        # comparison internally (cryptography >= 2.5).
        try:
            kdf = Scrypt(
                salt=salt,
                length=len(stored_key),
                n=n,
                r=r,
                p=p,
            )
            kdf.verify(password.encode("utf-8"), stored_key)
            # verify() returns None on success, raises InvalidKey on mismatch
            return True
        except InvalidKey:
            return False
        except AttributeError:
            # Fallback path: older cryptography versions without verify()
            # Use manual derivation + hmac.compare_digest for timing safety
            kdf_fallback = Scrypt(
                salt=salt,
                length=len(stored_key),
                n=n,
                r=r,
                p=p,
            )
            derived_key = kdf_fallback.derive(password.encode("utf-8"))
            return hmac.compare_digest(derived_key, stored_key)

    except (ValueError, KeyError, Exception):
        logger.debug(
            "scrypt verification failed due to parsing or derivation error.",
            exc_info=True,
        )
        return False


def _parse_scrypt_params(param_str: str) -> dict[str, int]:
    """Parse a scrypt parameter string like ``n=16384,r=8,p=1``.

    Returns a dictionary with integer values for ``n``, ``r``, and ``p``.

    Raises
    ------
    ValueError
        If any expected parameter is missing or not an integer.
    """
    result: dict[str, int] = {}
    for pair in param_str.split(","):
        key, _, value = pair.partition("=")
        key = key.strip().lower()
        if key in ("n", "r", "p"):
            result[key] = int(value.strip())

    for required in ("n", "r", "p"):
        if required not in result:
            raise ValueError(f"Missing scrypt parameter: {required}")

    return result


# ===================================================================
# Password Strength Validation
# ===================================================================


def validate_password_strength(
    password: str,
    min_length: int = 8,
    require_uppercase: bool = True,
    require_lowercase: bool = True,
    require_digit: bool = True,
    require_special: bool = True,
) -> tuple[bool, list[str]]:
    """Validate a password against configurable strength rules.

    Parameters
    ----------
    password:
        The plaintext password to validate.
    min_length:
        Minimum acceptable length (default ``8``).
    require_uppercase:
        Require at least one uppercase letter.
    require_lowercase:
        Require at least one lowercase letter.
    require_digit:
        Require at least one numeric digit.
    require_special:
        Require at least one special character.

    Returns
    -------
    tuple[bool, list[str]]
        A two-element tuple ``(is_valid, violations)`` where
        *is_valid* is ``True`` when no violations are found and
        *violations* is an ordered list of human-readable failure
        messages.
    """

    violations: list[str] = []

    if len(password) < min_length:
        violations.append(
            f"Password must be at least {min_length} characters"
        )

    if require_uppercase and not re.search(r"[A-Z]", password):
        violations.append(
            "Password must contain at least one uppercase letter"
        )

    if require_lowercase and not re.search(r"[a-z]", password):
        violations.append(
            "Password must contain at least one lowercase letter"
        )

    if require_digit and not re.search(r"\d", password):
        violations.append(
            "Password must contain at least one digit"
        )

    if require_special and not re.search(
        r'[!@#$%^&*()\-_=+\[\]{};:\'",.<>?/\\|`~]', password
    ):
        violations.append(
            "Password must contain at least one special character"
        )

    # Check against known-weak passwords (case-insensitive)
    if password.lower() in _COMMON_WEAK_PASSWORDS:
        violations.append(
            "Password is too common and easily guessable"
        )

    is_valid = len(violations) == 0
    return (is_valid, violations)


# ===================================================================
# API Key & Secret Key Generation (Feature F-304)
# ===================================================================


def generate_api_key(length: int = 40) -> str:
    """Generate a cryptographically secure random API key.

    Uses ``secrets.token_hex`` which draws from the OS CSPRNG.

    Parameters
    ----------
    length:
        Desired length of the hex-encoded key string (default ``40``).
        Must be a positive even integer.

    Returns
    -------
    str
        A hex-encoded random string of the requested length.
    """

    if length < 1:
        raise ValueError("API key length must be a positive integer.")
    # token_hex(n) returns 2*n hex characters
    return secrets.token_hex(length // 2)


def generate_secret_key(length: int = 64) -> str:
    """Generate a secure random string for Flask ``SECRET_KEY`` or
    similar application-level secrets.

    Uses ``secrets.token_urlsafe`` which produces URL-safe base64-encoded
    random bytes from the OS CSPRNG.

    Parameters
    ----------
    length:
        Number of random bytes to use (default ``64``).  The returned
        string will be slightly longer due to base64 encoding.

    Returns
    -------
    str
        A URL-safe random string.
    """

    if length < 1:
        raise ValueError("Secret key length must be a positive integer.")
    return secrets.token_urlsafe(length)


# ===================================================================
# Hash Migration Support
# ===================================================================


def get_hash_algorithm(password_hash: str) -> str:
    """Detect and return the algorithm identifier for a stored hash.

    Parameters
    ----------
    password_hash:
        The stored hash string to inspect.

    Returns
    -------
    str
        One of ``'bcrypt'``, ``'scrypt'``, or ``'unknown'``.
    """

    if not password_hash:
        return "unknown"

    if password_hash.startswith("$2"):
        # bcrypt variants: $2a$, $2b$, $2y$
        return "bcrypt"

    if password_hash.startswith("$scrypt$"):
        return "scrypt"

    return "unknown"


def needs_rehash(password_hash: str) -> bool:
    """Determine whether a stored hash should be re-computed.

    A rehash is recommended when:
    * The hash uses an unknown or deprecated algorithm.
    * The hash uses scrypt but bcrypt is now available
      (upgrade to preferred algorithm).
    * The hash uses bcrypt with a cost factor below the current
      minimum (``_MIN_BCRYPT_ROUNDS``).
    * The hash uses scrypt with parameters weaker than the current
      defaults.

    This function is intended to be called during a successful login so
    that password hashes can be transparently upgraded.

    Parameters
    ----------
    password_hash:
        The stored hash string to evaluate.

    Returns
    -------
    bool
        ``True`` if the hash should be regenerated using
        ``hash_password``.
    """

    if not password_hash:
        return True

    algo = get_hash_algorithm(password_hash)

    if algo == "unknown":
        # Unrecognised format — always rehash
        return True

    if algo == "scrypt" and BCRYPT_AVAILABLE:
        # Upgrade from scrypt to the preferred bcrypt algorithm
        return True

    if algo == "bcrypt":
        # Check whether the cost factor is too low
        cost = _extract_bcrypt_cost(password_hash)
        if cost is not None and cost < _MIN_BCRYPT_ROUNDS:
            return True

        # Also rehash if cost is below the configured BCRYPT_ROUNDS
        if cost is not None and cost < BCRYPT_ROUNDS:
            return True

    if algo == "scrypt":
        # Check whether scrypt parameters are below current defaults
        try:
            parts = password_hash.split("$")
            if len(parts) == 5:
                params = _parse_scrypt_params(parts[2])
                if (
                    params["n"] < SCRYPT_N
                    or params["r"] < SCRYPT_R
                    or params["p"] < SCRYPT_P
                ):
                    return True
        except (ValueError, KeyError):
            # Malformed — definitely needs rehash
            return True

    return False


def _extract_bcrypt_cost(password_hash: str) -> int | None:
    """Extract the cost (log-rounds) factor from a bcrypt hash string.

    bcrypt format: ``$2b$<cost>$<salt+hash>``

    Returns ``None`` if the cost cannot be determined.
    """

    try:
        # Split: ['', '2b', '12', '<rest>']
        parts = password_hash.split("$")
        if len(parts) >= 4:
            return int(parts[2])
    except (ValueError, IndexError):
        pass

    return None


# ===================================================================
# SHA-256 Utility (for non-password hashing needs)
# ===================================================================


def sha256_hex(data: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of *data*.

    This is a convenience wrapper around ``hashlib.sha256`` and is
    used for non-password operations such as checksum computation
    and content-addressable storage keys.
    """
    return hashlib.sha256(data).hexdigest()
