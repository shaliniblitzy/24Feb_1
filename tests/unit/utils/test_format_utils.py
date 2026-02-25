"""
Unit tests for ``src/utils/format_utils.py`` — format detection, content type
mapping, and coordinate parsing for all 7 repository formats (Maven, npm,
Docker, NuGet, PyPI, APT, Raw).  Coverage target: >= 90%.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from src.utils.format_utils import (
    detect_format,
    get_content_type,
    parse_coordinates,
    get_format_content_types,
    is_supported_format,
    normalize_format_name,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------
SUPPORTED_FORMATS: list[str] = [
    "maven", "npm", "docker", "nuget", "pypi", "apt", "raw",
]
FORMAT_CONTENT_TYPES: dict[str, str] = {
    "maven": "application/java-archive",
    "npm": "application/gzip",
    "docker": "application/vnd.docker.distribution.manifest.v2+json",
    "nuget": "application/zip",
    "pypi": "application/gzip",
    "apt": "application/vnd.debian.binary-package",
    "raw": "application/octet-stream",
}
_M = "com/example/mylib/1.0.0/mylib-1.0.0.jar"
_N = "@scope/package/-/package-1.0.0.tgz"
_D = "library/nginx:1.25"
_U = "Newtonsoft.Json/13.0.3/newtonsoft.json.13.0.3.nupkg"
_P = "packages/source/r/requests/requests-2.31.0.tar.gz"
_A = "pool/main/n/nginx/nginx_1.24.0-1_amd64.deb"
_R = "path/to/file.bin"
FORMAT_DETECTION_SAMPLES: list[tuple[str, str]] = [
    (_M, "maven"), (_N, "npm"), ("v2/library/nginx/manifests/1.25", "docker"),
    (_U, "nuget"), (_P, "pypi"), (_A, "apt"), (_R, "raw"),
]
COORDINATE_SAMPLES: list[tuple[str, str]] = [
    ("maven", _M), ("npm", _N), ("docker", _D), ("nuget", _U),
    ("pypi", _P), ("apt", _A), ("raw", _R),
]
MAVEN_STANDARD_PATH, NPM_SCOPED_PATH, DOCKER_TAGGED_REF = _M, _N, _D
NUGET_PACKAGE_PATH, PYPI_SDIST_PATH, APT_DEB_PATH, RAW_FILE_PATH = _U, _P, _A, _R
NPM_UNSCOPED_PATH = "lodash/-/lodash-4.17.21.tgz"
PYPI_WHEEL_PATH = "requests-2.31.0-py3-none-any.whl"


# --- detect_format ---

def test_detect_format_maven_jar_returns_maven():
    result = detect_format(MAVEN_STANDARD_PATH)
    assert result == "maven"
    assert isinstance(result, str)

def test_detect_format_maven_pom_returns_maven():
    result = detect_format("com/example/mylib/1.0.0/mylib-1.0.0.pom")
    assert result == "maven"
    assert result is not None

def test_detect_format_npm_tgz_returns_npm():
    result = detect_format(NPM_SCOPED_PATH)
    assert result == "npm"
    assert isinstance(result, str)

def test_detect_format_docker_manifest_returns_docker():
    docker_ct = "application/vnd.docker.distribution.manifest.v2+json"
    result = detect_format("v2/library/nginx/manifests/latest", content_type=docker_ct)
    assert result == "docker"
    assert isinstance(result, str)

def test_detect_format_nuget_nupkg_returns_nuget():
    result = detect_format(NUGET_PACKAGE_PATH)
    assert result == "nuget"
    assert isinstance(result, str)

def test_detect_format_pypi_sdist_returns_pypi():
    result = detect_format(PYPI_SDIST_PATH)
    assert result == "pypi"
    assert isinstance(result, str)

def test_detect_format_apt_deb_returns_apt():
    result = detect_format(APT_DEB_PATH)
    assert result == "apt"
    assert isinstance(result, str)

def test_detect_format_raw_binary_returns_raw():
    result = detect_format(RAW_FILE_PATH)
    assert result == "raw"
    assert isinstance(result, str)

@pytest.mark.parametrize("file_path,expected_format",
    FORMAT_DETECTION_SAMPLES, ids=SUPPORTED_FORMATS)
def test_detect_format_all_formats(file_path: str, expected_format: str):
    result = detect_format(file_path)
    assert result == expected_format
    assert result in SUPPORTED_FORMATS

def test_detect_format_unknown_extension_returns_raw():
    result = detect_format("data/archive.xyz")
    assert result == "raw"
    assert isinstance(result, str)

def test_detect_format_empty_path_handles_gracefully():
    result = detect_format("")
    assert result in ("raw", None)
    assert result is None or isinstance(result, str)

def test_detect_format_none_input_raises_error():
    with pytest.raises((TypeError, ValueError)):
        detect_format(None)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        detect_format(None)  # type: ignore[arg-type]

def test_detect_format_pypi_wheel_returns_pypi():
    result = detect_format(PYPI_WHEEL_PATH)
    assert result == "pypi"
    assert isinstance(result, str)

def test_detect_format_tar_bz2_extension():
    result = detect_format("archive/data.tar.bz2")
    assert isinstance(result, str)
    assert result in SUPPORTED_FORMATS

def test_detect_format_unknown_content_type_falls_through():
    result = detect_format(MAVEN_STANDARD_PATH, content_type="application/unknown")
    assert result == "maven"
    assert isinstance(result, str)

def test_detect_format_tgz_pypi_path():
    result = detect_format("packages/source/f/foo/foo-1.0.tgz")
    assert result in ("pypi", "npm")
    assert isinstance(result, str)

def test_detect_format_apt_metadata_returns_apt():
    result = detect_format("dists/stable/main/binary-amd64/Packages.gz")
    assert result == "apt"
    assert isinstance(result, str)

def test_detect_format_docker_v2_blobs():
    result = detect_format("v2/myimage/blobs/sha256:abc123")
    assert result == "docker"
    assert isinstance(result, str)

def test_detect_format_maven_war_returns_maven():
    result = detect_format("com/example/web/1.0/web-1.0.war")
    assert result == "maven"
    assert isinstance(result, str)

def test_detect_format_plain_tgz_defaults_npm():
    """A bare .tgz without clear path context defaults to npm."""
    result = detect_format("some-package-1.0.0.tgz")
    assert result == "npm"
    assert isinstance(result, str)


# --- get_content_type ---

@pytest.mark.parametrize("format_name,expected_type",
    list(FORMAT_CONTENT_TYPES.items()), ids=SUPPORTED_FORMATS)
def test_get_content_type_all_formats(format_name: str, expected_type: str):
    result = get_content_type(format_name)
    assert result == expected_type
    assert isinstance(result, str)

def test_get_content_type_maven_returns_java_archive():
    result = get_content_type("maven")
    assert "java-archive" in result
    assert result.startswith("application/")

def test_get_content_type_docker_returns_docker_manifest():
    result = get_content_type("docker")
    assert "docker" in result
    assert "/" in result

def test_get_content_type_unknown_format_returns_octet_stream():
    result = get_content_type("unknown_format")
    assert result == "application/octet-stream"
    assert isinstance(result, str)

def test_get_content_type_case_insensitive():
    lower = get_content_type("maven")
    upper = get_content_type("MAVEN")
    mixed = get_content_type("Maven")
    assert lower == upper == mixed
    assert isinstance(lower, str)

def test_get_content_type_none_input_raises_error():
    with pytest.raises(TypeError):
        get_content_type(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        get_content_type(None)  # type: ignore[arg-type]

def test_get_content_type_empty_string_returns_octet_stream():
    result = get_content_type("")
    assert result == "application/octet-stream"
    assert isinstance(result, str)


# --- get_format_content_types ---

def test_get_format_content_types_returns_all_formats():
    result = get_format_content_types()
    assert isinstance(result, dict)
    assert len(result) >= len(SUPPORTED_FORMATS)

def test_get_format_content_types_values_are_strings():
    result = get_format_content_types()
    for fmt, ct in result.items():
        assert isinstance(ct, str)
        assert "/" in ct


# --- parse_coordinates: Maven ---

def test_parse_coordinates_maven_standard_jar():
    result = parse_coordinates("maven", MAVEN_STANDARD_PATH)
    assert result["group_id"] == "com.example"
    assert result["artifact_id"] == "mylib"
    assert result["version"] == "1.0.0"
    assert result["extension"] == "jar"

def test_parse_coordinates_maven_snapshot():
    path = "com/example/mylib/1.0.0-SNAPSHOT/mylib-1.0.0-SNAPSHOT.jar"
    result = parse_coordinates("maven", path)
    assert "SNAPSHOT" in result["version"]
    assert result["group_id"] == "com.example"
    assert result["artifact_id"] == "mylib"

def test_parse_coordinates_maven_with_classifier():
    path = "com/example/mylib/1.0.0/mylib-1.0.0-sources.jar"
    result = parse_coordinates("maven", path)
    assert result.get("classifier") == "sources"
    assert result["artifact_id"] == "mylib"
    assert result["version"] == "1.0.0"

def test_parse_coordinates_maven_nested_group():
    path = "org/apache/commons/commons-lang3/3.12.0/commons-lang3-3.12.0.jar"
    result = parse_coordinates("maven", path)
    assert result["group_id"] == "org.apache.commons"
    assert result["artifact_id"] == "commons-lang3"
    assert result["version"] == "3.12.0"

def test_parse_coordinates_maven_three_segments_raises():
    with pytest.raises(ValueError):
        parse_coordinates("maven", "artifact/1.0/file.jar")
    with pytest.raises(ValueError):
        parse_coordinates("maven", "a/b/c.jar")


# --- parse_coordinates: npm ---

def test_parse_coordinates_npm_scoped_package():
    result = parse_coordinates("npm", NPM_SCOPED_PATH)
    assert "@scope" in result.get("name", "") or result.get("scope") == "@scope"
    assert result["version"] == "1.0.0"

def test_parse_coordinates_npm_unscoped_package():
    result = parse_coordinates("npm", NPM_UNSCOPED_PATH)
    assert result["name"] == "lodash"
    assert result["version"] == "4.17.21"

def test_parse_coordinates_npm_invalid_raises():
    with pytest.raises(ValueError):
        parse_coordinates("npm", "not-npm-path.txt")
    with pytest.raises(ValueError):
        parse_coordinates("npm", "plain")


# --- parse_coordinates: Docker ---

def test_parse_coordinates_docker_image_with_tag():
    result = parse_coordinates("docker", DOCKER_TAGGED_REF)
    assert result["repository"] == "library/nginx"
    assert result["tag"] == "1.25"

def test_parse_coordinates_docker_image_with_digest():
    digest = "sha256:" + "a" * 64
    ref = f"library/nginx@{digest}"
    result = parse_coordinates("docker", ref)
    assert result.get("digest") == digest or "sha256" in str(result.get("digest", ""))
    assert result["repository"] == "library/nginx"

def test_parse_coordinates_docker_v2_manifest():
    result = parse_coordinates("docker", "v2/myrepo/manifests/latest")
    assert result["repository"] == "myrepo"
    assert result["tag"] == "latest"

def test_parse_coordinates_docker_v2_blob_digest():
    digest = "sha256:" + "b" * 64
    result = parse_coordinates("docker", f"v2/myrepo/blobs/{digest}")
    assert result["repository"] == "myrepo"
    assert result["digest"] == digest

def test_parse_coordinates_docker_plain_repo():
    result = parse_coordinates("docker", "myimage")
    assert result["repository"] == "myimage"
    assert result["tag"] == "latest"


# --- parse_coordinates: NuGet ---

def test_parse_coordinates_nuget_package():
    result = parse_coordinates("nuget", NUGET_PACKAGE_PATH)
    assert result["id"] == "Newtonsoft.Json"
    assert result["version"] == "13.0.3"

def test_parse_coordinates_nuget_single_filename():
    result = parse_coordinates("nuget", "MyPackage.1.2.3.nupkg")
    assert isinstance(result, dict)
    assert len(result) >= 2


# --- parse_coordinates: PyPI ---

def test_parse_coordinates_pypi_sdist():
    result = parse_coordinates("pypi", PYPI_SDIST_PATH)
    assert result["name"] == "requests"
    assert result["version"] == "2.31.0"

def test_parse_coordinates_pypi_wheel():
    result = parse_coordinates("pypi", PYPI_WHEEL_PATH)
    assert result["name"] == "requests"
    assert result["version"] == "2.31.0"

def test_parse_coordinates_pypi_invalid_raises():
    with pytest.raises(ValueError):
        parse_coordinates("pypi", "not-a-distribution")
    with pytest.raises(ValueError):
        parse_coordinates("pypi", "plain.txt")


# --- parse_coordinates: APT ---

def test_parse_coordinates_apt_deb_package():
    result = parse_coordinates("apt", APT_DEB_PATH)
    assert result["package"] == "nginx"
    assert result["version"] == "1.24.0-1"
    assert result["architecture"] == "amd64"

def test_parse_coordinates_apt_invalid_raises():
    with pytest.raises(ValueError):
        parse_coordinates("apt", "random_file.txt")
    with pytest.raises(ValueError):
        parse_coordinates("apt", "nodebformat")


# --- parse_coordinates: Raw ---

def test_parse_coordinates_raw_file():
    result = parse_coordinates("raw", RAW_FILE_PATH)
    assert result["path"] == RAW_FILE_PATH
    assert isinstance(result, dict)


# --- parse_coordinates: parametrized cross-format ---

@pytest.mark.parametrize("fmt,path", COORDINATE_SAMPLES, ids=SUPPORTED_FORMATS)
def test_parse_coordinates_all_formats_return_dict(fmt: str, path: str):
    result = parse_coordinates(fmt, path)
    assert isinstance(result, dict)
    assert len(result) > 0


# --- is_supported_format ---

@pytest.mark.parametrize("fmt", SUPPORTED_FORMATS)
def test_is_supported_format_all_valid_formats(fmt: str):
    result = is_supported_format(fmt)
    assert result is True
    assert isinstance(result, bool)

def test_is_supported_format_invalid_format_returns_false():
    result = is_supported_format("unsupported")
    assert result is False
    assert isinstance(result, bool)

def test_is_supported_format_empty_string_returns_false():
    result = is_supported_format("")
    assert result is False
    assert isinstance(result, bool)

def test_is_supported_format_none_handles_gracefully():
    result = is_supported_format(None)  # type: ignore[arg-type]
    assert result is False
    assert isinstance(result, bool)


# --- normalize_format_name ---

def test_normalize_format_name_uppercase_to_lowercase():
    result = normalize_format_name("MAVEN")
    assert result == "maven"
    assert result == result.lower()

def test_normalize_format_name_mixed_case():
    result = normalize_format_name("Docker")
    assert result == "docker"
    assert result.islower()

@pytest.mark.parametrize("alias,canonical", [
    ("maven2", "maven"), ("debian", "apt"), ("deb", "apt"),
    ("python", "pypi"), ("pip", "pypi"), ("nodejs", "npm"), ("node", "npm"),
], ids=["maven2", "debian", "deb", "python", "pip", "nodejs", "node"])
def test_normalize_format_name_aliases(alias: str, canonical: str):
    result = normalize_format_name(alias)
    assert result == canonical
    assert isinstance(result, str)

def test_normalize_format_name_already_canonical():
    for fmt in SUPPORTED_FORMATS:
        result = normalize_format_name(fmt)
        assert result == fmt
        assert isinstance(result, str)

def test_normalize_format_name_none_raises_type_error():
    with pytest.raises(TypeError):
        normalize_format_name(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        normalize_format_name(123)  # type: ignore[arg-type]

# --- Edge cases ---

def test_detect_format_path_with_dots_in_directory():
    result = detect_format("com.example/lib/file.jar")
    assert result == "maven"
    assert isinstance(result, str)

def test_detect_format_very_long_path():
    long_dir = "/".join(["segment"] * 200)
    result = detect_format(f"{long_dir}/file.bin")
    assert isinstance(result, str) or result is None
    assert result is not None

def test_parse_coordinates_maven_empty_version():
    with pytest.raises((ValueError, KeyError)):
        parse_coordinates("maven", "com/example/mylib.jar")
    with pytest.raises((ValueError, KeyError)):
        parse_coordinates("maven", "com/example/mylib.jar")

def test_parse_coordinates_with_special_unicode_chars():
    result = parse_coordinates("raw", "données/fichier-résumé.bin")
    assert isinstance(result, dict)
    assert len(result) > 0


# --- Error cases ---

def test_parse_coordinates_invalid_maven_path_raises_error():
    with pytest.raises((ValueError, KeyError)):
        parse_coordinates("maven", "too/few")
    with pytest.raises((ValueError, KeyError)):
        parse_coordinates("maven", "")

def test_parse_coordinates_unsupported_format_raises_error():
    with pytest.raises(ValueError, match="(?i)unsupported|unknown|invalid"):
        parse_coordinates("invalid_format", "some/path")
    with pytest.raises(ValueError):
        parse_coordinates("invalid_format", "other/path")

def test_detect_format_with_conflicting_signals():
    docker_ct = "application/vnd.docker.distribution.manifest.v2+json"
    result_a = detect_format("something.jar", content_type=docker_ct)
    result_b = detect_format("something.jar", content_type=docker_ct)
    assert result_a == result_b
    assert isinstance(result_a, str)

def test_parse_coordinates_non_string_format_raises_type_error():
    with pytest.raises(TypeError):
        parse_coordinates(123, "some/path")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_coordinates("maven", 456)  # type: ignore[arg-type]

def test_parse_coordinates_maven_tar_gz_extension():
    """Maven artifact with .tar.gz extension parses correctly."""
    path = "com/example/data/1.0/data-1.0.tar.gz"
    result = parse_coordinates("maven", path)
    assert result["extension"] == "tar.gz"
    assert result["version"] == "1.0"

def test_parse_coordinates_npm_scoped_mismatched_tarball():
    """Scoped npm package where tarball name differs from package name."""
    result = parse_coordinates("npm", "@myorg/utils/-/old-utils-2.0.tgz")
    assert "@myorg" in result.get("name", "") or result.get("scope") == "@myorg"
    assert isinstance(result["version"], str)

def test_parse_coordinates_npm_unscoped_mismatched():
    """Unscoped npm where tarball base differs from package name."""
    result = parse_coordinates("npm", "mylib/-/other-1.5.tgz")
    assert result["name"] == "mylib"
    assert isinstance(result["version"], str)
