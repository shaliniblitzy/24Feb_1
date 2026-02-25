"""
Unit tests for ``src/utils/crypto_utils.py`` — hash computation (SHA-1,
SHA-256, MD5), checksum validation, and encryption utilities.
Coverage target: >= 90%.  AAA pattern, plain asserts, < 500 ms each.
"""
from __future__ import annotations

import hashlib
import io
import os
from unittest.mock import patch, MagicMock

import pytest

from src.utils.crypto_utils import (
    compute_sha1,
    compute_sha256,
    compute_md5,
    compute_hash,
    validate_checksum,
    generate_checksum_set,
    encrypt_value,
    decrypt_value,
    generate_key,
    compute_file_hash,
    SUPPORTED_ALGORITHMS,
)

# Module-level marker — @pytest.mark.unit on every test in this file
pytestmark = pytest.mark.unit

# Pre-computed reference hashes (no magic strings)
TEST_CONTENT: bytes = b"Hello, Binary Repository!"
TEST_CONTENT_SHA1: str = hashlib.sha1(TEST_CONTENT).hexdigest()
TEST_CONTENT_SHA256: str = hashlib.sha256(TEST_CONTENT).hexdigest()
TEST_CONTENT_MD5: str = hashlib.md5(TEST_CONTENT).hexdigest()

# Well-known empty-content hashes
EMPTY_SHA1: str = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
EMPTY_SHA256: str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
EMPTY_MD5: str = "d41d8cd98f00b204e9800998ecf8427e"

HASH_ALGORITHMS: list[str] = ["sha1", "sha256", "md5"]
HASH_EXPECTED_LENGTHS: dict[str, int] = {"sha1": 40, "sha256": 64, "md5": 32}
HEX_CHARS = set("0123456789abcdef")

# Reusable encryption key for deterministic encryption tests
_TEST_KEY: bytes = generate_key()


# --- SHA-1 hash computation tests ---

def test_compute_sha1_known_content_returns_correct_hash():
    result = compute_sha1(TEST_CONTENT)
    assert result == TEST_CONTENT_SHA1
    assert len(result) == 40

def test_compute_sha1_empty_bytes_returns_known_hash():
    result = compute_sha1(b"")
    assert result == EMPTY_SHA1
    assert len(result) == 40

def test_compute_sha1_binary_content_returns_hex_string():
    content = os.urandom(1024)
    result = compute_sha1(content)
    assert len(result) == 40
    assert set(result).issubset(HEX_CHARS)

def test_compute_sha1_same_content_returns_same_hash():
    first = compute_sha1(TEST_CONTENT)
    second = compute_sha1(TEST_CONTENT)
    assert first == second
    assert first == TEST_CONTENT_SHA1

def test_compute_sha1_different_content_returns_different_hash():
    hash_a = compute_sha1(b"alpha")
    hash_b = compute_sha1(b"beta")
    assert hash_a != hash_b
    assert len(hash_a) == len(hash_b) == 40

def test_compute_sha1_large_content_completes_successfully():
    large = os.urandom(1024 * 1024)
    result = compute_sha1(large)
    assert len(result) == 40
    assert set(result).issubset(HEX_CHARS)


# --- SHA-256 hash computation tests ---

def test_compute_sha256_known_content_returns_correct_hash():
    result = compute_sha256(TEST_CONTENT)
    assert result == TEST_CONTENT_SHA256
    assert len(result) == 64

def test_compute_sha256_empty_bytes_returns_known_hash():
    result = compute_sha256(b"")
    assert result == EMPTY_SHA256
    assert len(result) == 64

def test_compute_sha256_binary_content_returns_hex_string():
    content = os.urandom(1024)
    result = compute_sha256(content)
    assert len(result) == 64
    assert set(result).issubset(HEX_CHARS)

def test_compute_sha256_deterministic():
    first = compute_sha256(TEST_CONTENT)
    second = compute_sha256(TEST_CONTENT)
    assert first == second
    assert first == TEST_CONTENT_SHA256


# --- MD5 hash computation tests ---

def test_compute_md5_known_content_returns_correct_hash():
    result = compute_md5(TEST_CONTENT)
    assert result == TEST_CONTENT_MD5
    assert len(result) == 32

def test_compute_md5_empty_bytes_returns_known_hash():
    result = compute_md5(b"")
    assert result == EMPTY_MD5
    assert len(result) == 32

def test_compute_md5_binary_content_returns_hex_string():
    content = os.urandom(1024)
    result = compute_md5(content)
    assert len(result) == 32
    assert set(result).issubset(HEX_CHARS)


# --- Generic hash computation tests ---

@pytest.mark.parametrize(
    "algorithm, expected_length",
    [("sha1", 40), ("sha256", 64), ("md5", 32)],
    ids=["sha1-40", "sha256-64", "md5-32"],
)
def test_compute_hash_all_algorithms(algorithm, expected_length):
    result = compute_hash(TEST_CONTENT, algorithm)
    assert len(result) == expected_length
    assert set(result).issubset(HEX_CHARS)

def test_compute_hash_unsupported_algorithm_raises_error():
    with pytest.raises(ValueError, match="Unsupported hash algorithm"):
        compute_hash(TEST_CONTENT, "sha512")
    with pytest.raises(ValueError):
        compute_hash(TEST_CONTENT, "invalid")

def test_compute_hash_none_content_raises_error():
    with pytest.raises(TypeError, match="Expected bytes"):
        compute_hash(None, "sha256")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_hash(None, "sha1")  # type: ignore[arg-type]


# --- Checksum validation tests ---

def test_validate_checksum_correct_sha256_returns_true():
    result = validate_checksum(TEST_CONTENT, TEST_CONTENT_SHA256, "sha256")
    assert result is True
    assert isinstance(result, bool)

def test_validate_checksum_incorrect_hash_returns_false():
    result = validate_checksum(TEST_CONTENT, "deadbeef" * 8, "sha256")
    assert result is False
    assert isinstance(result, bool)

def test_validate_checksum_correct_sha1_returns_true():
    result = validate_checksum(TEST_CONTENT, TEST_CONTENT_SHA1, "sha1")
    assert result is True
    assert isinstance(result, bool)

def test_validate_checksum_correct_md5_returns_true():
    result = validate_checksum(TEST_CONTENT, TEST_CONTENT_MD5, "md5")
    assert result is True
    assert isinstance(result, bool)

@pytest.mark.parametrize(
    "algorithm, expected_hash",
    [
        ("sha1", TEST_CONTENT_SHA1),
        ("sha256", TEST_CONTENT_SHA256),
        ("md5", TEST_CONTENT_MD5),
    ],
    ids=HASH_ALGORITHMS,
)
def test_validate_checksum_all_algorithms(algorithm, expected_hash):
    result = validate_checksum(TEST_CONTENT, expected_hash, algorithm)
    assert result is True
    assert isinstance(result, bool)

def test_validate_checksum_case_insensitive():
    upper_hash = TEST_CONTENT_SHA256.upper()
    result = validate_checksum(TEST_CONTENT, upper_hash, "sha256")
    assert result is True
    assert upper_hash != TEST_CONTENT_SHA256

def test_validate_checksum_empty_content_with_correct_hash():
    result = validate_checksum(b"", EMPTY_SHA256, "sha256")
    assert result is True
    assert isinstance(result, bool)

def test_validate_checksum_empty_expected_hash_returns_false():
    result = validate_checksum(TEST_CONTENT, "", "sha256")
    assert result is False
    assert isinstance(result, bool)


# --- Generate checksum set tests ---

def test_generate_checksum_set_returns_all_three_hashes():
    result = generate_checksum_set(TEST_CONTENT)
    assert set(result.keys()) == {"sha1", "sha256", "md5"}
    assert all(isinstance(v, str) for v in result.values())

def test_generate_checksum_set_values_match_individual_computations():
    result = generate_checksum_set(TEST_CONTENT)
    assert result["sha1"] == TEST_CONTENT_SHA1
    assert result["sha256"] == TEST_CONTENT_SHA256
    assert result["md5"] == TEST_CONTENT_MD5

def test_generate_checksum_set_empty_content():
    result = generate_checksum_set(b"")
    assert result["sha1"] == EMPTY_SHA1
    assert result["sha256"] == EMPTY_SHA256
    assert result["md5"] == EMPTY_MD5

def test_generate_checksum_set_returns_correct_types():
    result = generate_checksum_set(TEST_CONTENT)
    for key, value in result.items():
        assert isinstance(value, str)
        assert set(value).issubset(HEX_CHARS), f"{key} has non-hex chars"


# --- Encryption utility tests ---

def test_encrypt_value_returns_non_empty_string():
    plaintext = "my-secret-api-key"
    result = encrypt_value(plaintext, _TEST_KEY)
    assert result  # non-empty
    assert result != plaintext

def test_decrypt_value_returns_original_plaintext():
    plaintext = "repository-admin-password"
    encrypted = encrypt_value(plaintext, _TEST_KEY)
    decrypted = decrypt_value(encrypted, _TEST_KEY)
    assert decrypted == plaintext
    assert isinstance(decrypted, str)

def test_encrypt_decrypt_round_trip():
    """Round-trip encrypt/decrypt preserves data for various inputs."""
    samples = ["short", "a-longer-api-key-1234567890",
               "special: !@#$%^&*()", "unicode: café résumé"]
    for sample in samples:
        assert decrypt_value(encrypt_value(sample, _TEST_KEY), _TEST_KEY) == sample
    assert len(samples) == 4

def test_encrypt_value_different_content_produces_different_ciphertext():
    enc_a = encrypt_value("alpha", _TEST_KEY)
    enc_b = encrypt_value("beta", _TEST_KEY)
    assert enc_a != enc_b
    assert isinstance(enc_a, str) and isinstance(enc_b, str)

def test_decrypt_value_wrong_key_raises_error():
    from cryptography.fernet import InvalidToken
    encrypted = encrypt_value("secret", _TEST_KEY)
    wrong_key = generate_key()
    with pytest.raises(InvalidToken):
        decrypt_value(encrypted, wrong_key)
    assert wrong_key != _TEST_KEY

def test_encrypt_value_empty_string():
    encrypted = encrypt_value("", _TEST_KEY)
    assert encrypted  # non-empty even for empty input
    assert decrypt_value(encrypted, _TEST_KEY) == ""

def test_generate_key_returns_valid_key():
    key = generate_key()
    assert isinstance(key, bytes)
    assert len(key) == 44  # Fernet key length

def test_generate_key_returns_unique_keys():
    key_a = generate_key()
    key_b = generate_key()
    assert key_a != key_b
    assert isinstance(key_a, bytes) and isinstance(key_b, bytes)


# --- File-based hash computation tests ---

def test_compute_hash_from_file_object():
    file_obj = io.BytesIO(TEST_CONTENT)
    result = compute_file_hash(file_obj, "sha256")
    assert result == TEST_CONTENT_SHA256
    assert len(result) == 64

def test_compute_hash_from_file_path(tmp_path):
    test_file = tmp_path / "artifact.bin"
    test_file.write_bytes(TEST_CONTENT)
    result = compute_file_hash(str(test_file), "sha256")
    assert result == TEST_CONTENT_SHA256
    assert test_file.read_bytes() == TEST_CONTENT  # file unmodified


# --- Edge cases ---

def test_compute_hash_single_byte():
    data = b"\x00"
    result = compute_sha256(data)
    expected = hashlib.sha256(data).hexdigest()
    assert result == expected
    assert len(result) == 64

def test_compute_hash_unicode_raises_type_error():
    with pytest.raises(TypeError, match="Expected bytes"):
        compute_sha1("string not bytes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_sha256("string not bytes")  # type: ignore[arg-type]

def test_validate_checksum_with_hash_prefix():
    """Docker-style 'sha256:...' prefix is stripped automatically."""
    prefixed_hash = f"sha256:{TEST_CONTENT_SHA256}"
    result = validate_checksum(TEST_CONTENT, prefixed_hash, "sha256")
    assert result is True
    assert isinstance(result, bool)

def test_compute_hash_very_large_content():
    large = os.urandom(10 * 1024 * 1024)
    result = compute_sha256(large)
    assert len(result) == 64
    assert set(result).issubset(HEX_CHARS)


# --- Error cases ---

def test_compute_sha1_none_input_raises_type_error():
    with pytest.raises(TypeError, match="Expected bytes"):
        compute_sha1(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_md5(None)  # type: ignore[arg-type]

def test_validate_checksum_none_content_raises_error():
    with pytest.raises(TypeError, match="Expected bytes"):
        validate_checksum(None, TEST_CONTENT_SHA256, "sha256")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        validate_checksum(None, "abc", "md5")  # type: ignore[arg-type]

def test_validate_checksum_none_expected_raises_error():
    with pytest.raises(TypeError, match="Expected string"):
        validate_checksum(TEST_CONTENT, None, "sha256")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        validate_checksum(b"data", None, "md5")  # type: ignore[arg-type]

def test_encrypt_value_none_input_raises_error():
    with pytest.raises(TypeError, match="Expected string"):
        encrypt_value(None, _TEST_KEY)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Expected bytes"):
        encrypt_value("text", "not-bytes")  # type: ignore[arg-type]


# --- Additional coverage: compute_file_hash edge cases ---

def test_compute_file_hash_unsupported_algorithm():
    file_obj = io.BytesIO(TEST_CONTENT)
    with pytest.raises(ValueError, match="Unsupported hash algorithm"):
        compute_file_hash(file_obj, "sha512")
    assert True  # execution reached after expected exception

def test_compute_file_hash_invalid_source_type():
    with pytest.raises(TypeError, match="Expected file path or file-like"):
        compute_file_hash(12345, "sha256")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_file_hash(["not", "a", "file"], "sha256")  # type: ignore[arg-type]

@pytest.mark.parametrize("algorithm", HASH_ALGORITHMS, ids=HASH_ALGORITHMS)
def test_compute_file_hash_all_algorithms(tmp_path, algorithm):
    test_file = tmp_path / "test_artifact.bin"
    test_file.write_bytes(TEST_CONTENT)
    result = compute_file_hash(str(test_file), algorithm)
    expected = compute_hash(TEST_CONTENT, algorithm)
    assert result == expected
    assert len(result) == HASH_EXPECTED_LENGTHS[algorithm]

def test_compute_file_hash_file_not_found():
    with pytest.raises(FileNotFoundError):
        compute_file_hash("/nonexistent/path/file.bin", "sha256")
    assert True  # reached after expected exception

def test_generate_checksum_set_none_raises_type_error():
    with pytest.raises(TypeError, match="Expected bytes"):
        generate_checksum_set(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        generate_checksum_set(42)  # type: ignore[arg-type]

def test_compute_hash_bytearray_input():
    data = bytearray(TEST_CONTENT)
    result = compute_sha256(data)
    assert result == TEST_CONTENT_SHA256
    assert len(result) == 64

def test_validate_checksum_sha1_prefix_stripping():
    prefixed = f"sha1:{TEST_CONTENT_SHA1}"
    result = validate_checksum(TEST_CONTENT, prefixed, "sha1")
    assert result is True
    assert isinstance(result, bool)

def test_supported_algorithms_constant():
    assert "sha1" in SUPPORTED_ALGORITHMS
    assert "sha256" in SUPPORTED_ALGORITHMS
    assert "md5" in SUPPORTED_ALGORITHMS
    assert SUPPORTED_ALGORITHMS["sha1"] == 40

def test_decrypt_value_none_input_raises_type_error():
    with pytest.raises(TypeError, match="Expected string"):
        decrypt_value(None, _TEST_KEY)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Expected bytes"):
        decrypt_value("ciphertext", "not-bytes")  # type: ignore[arg-type]

def test_compute_file_hash_non_seekable_file_object():
    """File-like objects without seek() are still hashed correctly."""
    class NonSeekableIO:
        def __init__(self, data: bytes):
            self._stream = io.BytesIO(data)
        def read(self, n: int = -1) -> bytes:
            return self._stream.read(n)
    obj = NonSeekableIO(TEST_CONTENT)
    result = compute_file_hash(obj, "sha256")
    assert result == TEST_CONTENT_SHA256
    assert len(result) == 64
