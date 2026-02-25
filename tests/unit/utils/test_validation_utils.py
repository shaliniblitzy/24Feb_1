"""
Unit tests for ``src/utils/validation_utils.py`` — input validation, name
sanitization, path normalization, and size limit enforcement.

Coverage target: >= 90% (AAP Section 0.7.1).
All tests use AAA pattern with plain ``assert`` statements.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from src.utils.validation_utils import (
    validate_name,
    validate_repository_name,
    sanitize_name,
    normalize_path,
    validate_size,
    validate_content_length,
    validate_required_fields,
    is_valid_url,
    is_valid_email,
    validate_path_component,
)

pytestmark = pytest.mark.unit

# --- Test constants (no magic numbers) ---
MAX_NAME_LENGTH = 255
MIN_NAME_LENGTH = 1
MAX_PATH_LENGTH = 1024
MAX_CONTENT_LENGTH = 1024 * 1024 * 1024  # 1 GB
MAX_UPLOAD_SIZE = 100 * 1024 * 1024       # 100 MB

VALID_REPO_NAMES = [
    "my-repo", "maven-central", "docker-hub-proxy",
    "npm_registry", "pypi.hosted", "repo123",
]
INVALID_REPO_NAMES = [
    "", "   ", "../escape", "/absolute/path",
    "name with spaces", "name/with/slashes", "a" * (MAX_NAME_LENGTH + 1),
]

@pytest.mark.parametrize("name", VALID_REPO_NAMES)
def test_validate_name_valid_names_pass(name):
    result = validate_name(name)
    assert result is True
    assert isinstance(result, bool)

@pytest.mark.parametrize("name", INVALID_REPO_NAMES)
def test_validate_name_invalid_names_fail(name):
    result = validate_name(name)
    assert result is False
    assert isinstance(result, bool)

@pytest.mark.parametrize("name", [
    "myrepository123", "my-maven-repo", "my_npm_registry", "com.example.repo",
])
def test_validate_name_char_types_accepted(name):
    result = validate_name(name)
    assert result is True
    assert isinstance(result, bool)

def test_validate_name_none_raises_error():
    with pytest.raises(TypeError, match="string"):
        validate_name(None)
    with pytest.raises(TypeError):
        validate_name(None)

def test_validate_name_with_leading_trailing_spaces():
    """Leading/trailing whitespace is stripped before validation."""
    result = validate_name("  my-repo  ")
    assert result is True
    assert isinstance(result, bool)

@pytest.mark.parametrize("char", list("@#$%^&*()"))
def test_validate_name_with_special_characters_returns_false(char):
    result = validate_name(f"repo{char}name")
    assert result is False
    assert isinstance(result, bool)

def test_validate_name_max_length_boundary():
    name = "a" * MAX_NAME_LENGTH
    result = validate_name(name)
    assert result is True
    assert len(name) == MAX_NAME_LENGTH

def test_validate_name_exceeds_max_length():
    name = "a" * (MAX_NAME_LENGTH + 1)
    result = validate_name(name)
    assert result is False
    assert len(name) > MAX_NAME_LENGTH

def test_validate_name_single_character_valid():
    result = validate_name("a")
    assert result is True
    assert isinstance(result, bool)

def test_validate_repository_name_delegates_correctly():
    assert validate_repository_name("my-repo") is True
    assert validate_repository_name("") is False


def test_sanitize_name_removes_special_characters():
    result = sanitize_name("my@repo#name!")
    assert "@" not in result and "#" not in result
    assert len(result) > 0

def test_sanitize_name_preserves_valid_characters():
    result = sanitize_name("valid-name_123.test")
    assert result == "valid-name_123.test"
    assert isinstance(result, str)

def test_sanitize_name_lowercases_input():
    result = sanitize_name("MyRepoName")
    assert result == result.lower()
    assert "M" not in result

def test_sanitize_name_strips_whitespace():
    result = sanitize_name("  my-repo  ")
    assert result == "my-repo"
    assert not result.startswith(" ")

def test_sanitize_name_replaces_spaces_with_hyphens():
    result = sanitize_name("my repo name")
    assert " " not in result
    assert isinstance(result, str)

def test_sanitize_name_empty_string_returns_empty():
    result = sanitize_name("")
    assert result == ""
    assert isinstance(result, str)

def test_sanitize_name_unicode_characters():
    result = sanitize_name("répo-nàme")
    assert isinstance(result, str)
    assert len(result) >= 0

def test_sanitize_name_path_traversal_attempts_stripped():
    result = sanitize_name("../../../etc/passwd")
    assert ".." not in result
    assert "/" not in result

def test_sanitize_name_consecutive_special_chars_collapsed():
    result = sanitize_name("my---repo___name")
    assert "---" not in result
    assert isinstance(result, str)

@pytest.mark.parametrize("dangerous_input", [
    "<script>alert(1)</script>",
    '"; DROP TABLE repos; --',
    "repo\x00name",
    "../../../",
])
def test_sanitize_name_dangerous_inputs(dangerous_input):
    result = sanitize_name(dangerous_input)
    assert "<" not in result and ">" not in result
    assert "\x00" not in result

def test_sanitize_name_very_long_input():
    result = sanitize_name("a" * 10_000)
    assert len(result) <= MAX_NAME_LENGTH
    assert isinstance(result, str)


def test_normalize_path_forward_slashes():
    result = normalize_path("com/example/lib/1.0.0/lib.jar")
    assert "/" in result
    assert "\\" not in result

def test_normalize_path_backslashes_to_forward_slashes():
    result = normalize_path("com\\example\\lib\\1.0.0\\lib.jar")
    assert "\\" not in result
    assert result == "com/example/lib/1.0.0/lib.jar"

def test_normalize_path_removes_leading_slash():
    result = normalize_path("/com/example/lib/lib.jar")
    assert not result.startswith("/")
    assert result.startswith("com")

def test_normalize_path_removes_trailing_slash():
    result = normalize_path("com/example/")
    assert not result.endswith("/")
    assert result.endswith("example")

def test_normalize_path_collapses_double_slashes():
    result = normalize_path("com//example///lib/lib.jar")
    assert "//" not in result
    assert result == "com/example/lib/lib.jar"

def test_normalize_path_removes_dot_segments():
    result = normalize_path("com/./example/../example/lib/lib.jar")
    assert result == "com/example/lib/lib.jar"
    assert ".." not in result

def test_normalize_path_prevents_directory_traversal():
    with pytest.raises(ValueError, match="[Tt]raversal"):
        normalize_path("../../etc/passwd")
    with pytest.raises(ValueError):
        normalize_path("../secret")

def test_normalize_path_empty_string():
    result = normalize_path("")
    assert result == ""
    assert isinstance(result, str)

def test_normalize_path_preserves_valid_path():
    path = "com/example/mylib/1.0.0/mylib-1.0.0.jar"
    result = normalize_path(path)
    assert result == path
    assert isinstance(result, str)

def test_normalize_path_none_input_raises_error():
    with pytest.raises(TypeError, match="string"):
        normalize_path(None)
    with pytest.raises(TypeError):
        normalize_path(None)

def test_normalize_path_max_length_boundary():
    segment = "abcde/"
    path = (segment * (MAX_PATH_LENGTH // len(segment)))[:MAX_PATH_LENGTH]
    result = normalize_path(path)
    assert len(result) <= MAX_PATH_LENGTH
    assert isinstance(result, str)

def test_normalize_path_exceeds_max_length():
    path = "a/" * (MAX_PATH_LENGTH + 1)
    with pytest.raises(ValueError, match="maximum length"):
        normalize_path(path)
    with pytest.raises(ValueError):
        normalize_path(path)

@pytest.mark.parametrize("path", ["a/b\\c", "a\\b/c", "a//b\\\\c"])
def test_normalize_path_various_separators(path):
    result = normalize_path(path)
    assert "\\" not in result
    assert isinstance(result, str)


def test_validate_size_within_limit_returns_true():
    result = validate_size(1024, MAX_UPLOAD_SIZE)
    assert result is True
    assert isinstance(result, bool)

def test_validate_size_at_exact_limit_returns_true():
    result = validate_size(MAX_UPLOAD_SIZE, MAX_UPLOAD_SIZE)
    assert result is True
    assert isinstance(result, bool)

def test_validate_size_exceeds_limit_returns_false():
    result = validate_size(MAX_UPLOAD_SIZE + 1, MAX_UPLOAD_SIZE)
    assert result is False
    assert not result

def test_validate_size_zero_bytes():
    result = validate_size(0, MAX_UPLOAD_SIZE)
    assert result is True
    assert isinstance(result, bool)

def test_validate_size_negative_value_returns_false():
    result = validate_size(-1, MAX_UPLOAD_SIZE)
    assert result is False
    assert not result

def test_validate_size_very_large_value():
    result = validate_size(MAX_CONTENT_LENGTH + 1, MAX_CONTENT_LENGTH)
    assert result is False
    assert not result

@pytest.mark.parametrize("size,expected", [
    (0, True), (1, True),
    (MAX_UPLOAD_SIZE - 1, True), (MAX_UPLOAD_SIZE, True),
    (MAX_UPLOAD_SIZE + 1, False),
])
def test_validate_size_boundary_values(size, expected):
    result = validate_size(size, MAX_UPLOAD_SIZE)
    assert result is expected
    assert isinstance(result, bool)

def test_validate_size_with_custom_max():
    assert validate_size(500, 1000) is True
    assert validate_content_length(500, 1000) is True

def test_validate_content_length_string_size_raises_error():
    with pytest.raises(TypeError):
        validate_content_length("1024", MAX_UPLOAD_SIZE)
    with pytest.raises(TypeError):
        validate_content_length(None, MAX_UPLOAD_SIZE)

def test_validate_content_length_non_numeric_raises_error():
    with pytest.raises(TypeError):
        validate_content_length({}, MAX_UPLOAD_SIZE)
    with pytest.raises(TypeError):
        validate_content_length("abc", MAX_UPLOAD_SIZE)


def test_validate_required_fields_all_present_returns_true():
    data = {"name": "repo", "format": "maven", "type": "hosted"}
    result = validate_required_fields(data, ["name", "format", "type"])
    assert result is True
    assert isinstance(result, bool)

def test_validate_required_fields_missing_field_raises_error():
    with pytest.raises(ValueError, match="format"):
        validate_required_fields({"name": "repo"}, ["name", "format"])
    with pytest.raises(ValueError):
        validate_required_fields({}, ["type"])

def test_validate_required_fields_empty_dict_raises_error():
    with pytest.raises(ValueError, match="name"):
        validate_required_fields({}, ["name"])
    with pytest.raises(ValueError):
        validate_required_fields({}, ["format"])

def test_validate_required_fields_none_value_treated_as_missing():
    with pytest.raises(ValueError, match="name"):
        validate_required_fields({"name": None}, ["name"])
    with pytest.raises(ValueError):
        validate_required_fields({"x": None}, ["x"])

def test_validate_required_fields_no_required_fields_passes():
    assert validate_required_fields({}, []) is True
    assert validate_required_fields({"a": 1}, []) is True

def test_validate_required_fields_extra_fields_ignored():
    result = validate_required_fields({"name": "repo", "extra": "val"}, ["name"])
    assert result is True
    assert isinstance(result, bool)

def test_validate_required_fields_empty_string_handling():
    result = validate_required_fields({"name": ""}, ["name"])
    assert result is True
    assert isinstance(result, bool)

def test_validate_required_fields_with_mock_data():
    mock_val = MagicMock()
    result = validate_required_fields({"name": mock_val, "type": "hosted"}, ["name", "type"])
    assert result is True
    assert isinstance(result, bool)


@pytest.mark.parametrize("url", [
    "https://example.com", "http://localhost:8080",
    "https://repo1.maven.org/maven2/", "http://archive.ubuntu.com/ubuntu/",
])
def test_is_valid_url_valid_urls(url):
    result = is_valid_url(url)
    assert result is True
    assert isinstance(result, bool)

@pytest.mark.parametrize("url", [
    "not-a-url", "ftp://wrong-scheme", "",
    "javascript:alert(1)", "://missing-scheme",
])
def test_is_valid_url_invalid_urls(url):
    result = is_valid_url(url)
    assert result is False
    assert isinstance(result, bool)

def test_is_valid_url_none_returns_false():
    result = is_valid_url(None)
    assert result is False
    assert not result


@pytest.mark.parametrize("email", [
    "user@example.com", "admin@repo.org", "test+tag@gmail.com",
])
def test_is_valid_email_valid_emails(email):
    result = is_valid_email(email)
    assert result is True
    assert isinstance(result, bool)

@pytest.mark.parametrize("email", [
    "not-an-email", "@missing-user.com", "user@", "user@.com", "",
])
def test_is_valid_email_invalid_emails(email):
    result = is_valid_email(email)
    assert result is False
    assert isinstance(result, bool)

def test_is_valid_email_none_returns_false():
    result = is_valid_email(None)
    assert result is False
    assert not result


def test_validate_path_component_valid_component():
    result = validate_path_component("my-artifact")
    assert result is True
    assert isinstance(result, bool)

def test_validate_path_component_with_dots_rejected():
    result = validate_path_component("..")
    assert result is False
    assert not result

def test_validate_path_component_with_slash_rejected():
    result = validate_path_component("path/with/slash")
    assert result is False
    assert not result

def test_validate_path_component_null_byte_rejected():
    result = validate_path_component("name\x00evil")
    assert result is False
    assert not result


def test_validate_name_with_unicode_letters():
    result = validate_name("münchen-repo")
    assert isinstance(result, bool)
    assert result is False

def test_validate_name_maximum_boundary_plus_one():
    name = "a" * (MAX_NAME_LENGTH + 1)
    result = validate_name(name)
    assert result is False
    assert len(name) == MAX_NAME_LENGTH + 1

def test_normalize_path_with_encoded_characters():
    result = normalize_path("com%2Fexample%2Flib")
    assert isinstance(result, str)
    assert len(result) > 0

def test_validate_size_float_value():
    result = validate_size(1024.5, MAX_UPLOAD_SIZE)
    assert isinstance(result, bool)
    assert result is True

def test_normalize_path_null_bytes_stripped():
    result = normalize_path("com/example\x00/lib")
    assert "\x00" not in result
    assert isinstance(result, str)

def test_sanitize_name_with_patched_max_length():
    with patch("src.utils.validation_utils.MAX_NAME_LENGTH", 10):
        result = sanitize_name("a" * 100)
        assert len(result) <= 10
        assert isinstance(result, str)


def test_sanitize_name_non_string_raises_type_error():
    with pytest.raises(TypeError):
        sanitize_name(12345)
    with pytest.raises(TypeError):
        sanitize_name(None)

def test_validate_size_bool_raises_type_error():
    with pytest.raises(TypeError):
        validate_size(True, MAX_UPLOAD_SIZE)
    with pytest.raises(TypeError):
        validate_size(False, MAX_UPLOAD_SIZE)

def test_validate_content_length_bool_and_list_raises_type_error():
    with pytest.raises(TypeError):
        validate_content_length(True, MAX_UPLOAD_SIZE)
    with pytest.raises(TypeError):
        validate_content_length([1, 2], MAX_UPLOAD_SIZE)

def test_validate_required_fields_non_list_required_raises_error():
    with pytest.raises(TypeError):
        validate_required_fields({"name": "x"}, 123)
    with pytest.raises(TypeError):
        validate_required_fields({"name": "x"}, "name")

def test_validate_path_component_non_string_raises_type_error():
    with pytest.raises(TypeError):
        validate_path_component(None)
    with pytest.raises(TypeError):
        validate_path_component(12345)

def test_validate_path_component_empty_returns_false():
    result = validate_path_component("")
    assert result is False
    assert not result


def test_validate_name_integer_input_raises_type_error():
    with pytest.raises(TypeError, match="string"):
        validate_name(12345)
    with pytest.raises(TypeError):
        validate_name(12345)

def test_validate_required_fields_non_dict_data_raises_error():
    with pytest.raises(TypeError):
        validate_required_fields("not a dict", ["name"])
    with pytest.raises(TypeError):
        validate_required_fields(42, ["name"])

def test_normalize_path_list_input_raises_type_error():
    with pytest.raises(TypeError, match="string"):
        normalize_path(["com", "example"])
    with pytest.raises(TypeError):
        normalize_path(["com", "example"])

def test_validate_size_none_input_raises_error():
    with pytest.raises(TypeError):
        validate_size(None, MAX_UPLOAD_SIZE)
    with pytest.raises(TypeError):
        validate_size("not a number", MAX_UPLOAD_SIZE)
