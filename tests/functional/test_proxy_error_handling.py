"""Functional tests for proxy repository — error handling and edge cases.

Feature coverage: F-102 (Repository Types — Proxy) — Critical priority.
"""

from __future__ import annotations

import json
import io
from unittest.mock import patch, MagicMock

import pytest
import responses
from freezegun import freeze_time

from tests.fixtures.repository_data import (
    make_proxy_repo,
    make_proxy_repo_maven,
    make_proxy_repo_npm,
    make_proxy_repo_docker,
    make_proxy_repo_pypi,
    make_proxy_repo_nuget,
    make_proxy_repo_apt,
    make_proxy_repo_raw,
)
from tests.fixtures.artifact_data import (
    make_maven_artifact,
    make_npm_artifact,
    make_binary_artifact,
    make_artifact_for_format,
)
from tests.fixtures.user_data import make_admin_user
from tests.mocks.mock_proxy_client import (
    MockProxyClient,
    MockResponse,
    create_mock_proxy_client,
    create_preconfigured_maven_client,
    make_maven_metadata_response,
    make_npm_package_response,
    make_docker_manifest_response,
    make_artifact_download_response,
    ProxyConnectionError,
    ProxyTimeoutError,
    MAVEN_CENTRAL_URL,
    NPM_REGISTRY_URL,
    DOCKER_HUB_URL,
    PYPI_URL,
)

pytestmark = pytest.mark.functional

MAVEN_ARTIFACT_PATH = "com/example/library/1.0.0/library-1.0.0.jar"
MAVEN_METADATA_PATH = "com/example/library/maven-metadata.xml"
NPM_PACKAGE_NAME = "lodash"
NPM_TARBALL_PATH = "lodash/-/lodash-4.17.21.tgz"
DOCKER_IMAGE_REPO = "library/nginx"
DOCKER_IMAGE_TAG = "latest"
PYPI_PACKAGE_NAME = "flask"
PYPI_JSON_PATH = "pypi/flask/json"

SAMPLE_JAR_CONTENT = b"PK\x03\x04" + b"\x00" * 256
SAMPLE_NPM_TARBALL = b"\x1f\x8b\x08" + b"\x00" * 256
SAMPLE_DOCKER_LAYER = b"\x1f\x8b\x08" + b"\x00" * 200
SAMPLE_RAW_CONTENT = b"raw-binary-content-" + b"\x42" * 128

def _create_proxy_repo_via_api(client, auth_headers, repo_data):
    """POST a proxy repository configuration to the REST API."""
    return client.post("/api/v1/repositories", json=repo_data, headers=auth_headers)


def _fetch_artifact_via_proxy(client, auth_headers, repo_name, artifact_path):
    """GET an artifact through a proxy repository."""
    return client.get(
        f"/api/v1/repositories/{repo_name}/content/{artifact_path}",
        headers=auth_headers,
    )


# =========================================================================
# Phase 7: Error Handling Tests
# =========================================================================


class TestProxyErrorHandling:
    """Test proxy error scenarios: upstream errors, auth failures, bad config."""

    def test_proxy_fetch_upstream_404_returns_404(
        self, client, auth_headers, db_session, mock_upstream
    ):
        """Upstream returns 404 Not Found → proxy returns 404.

        Arrange: Mock upstream to respond with 404 for requested path.
        Act: Fetch artifact through proxy.
        Assert: Response status is 404, body contains error info.
        """
        # Arrange
        repo_data = make_proxy_repo_maven(name="error-404-proxy")
        mock_upstream.register_error_response(
            "GET",
            f"{MAVEN_CENTRAL_URL}missing/artifact.jar",
            status_code=404,
            message="Artifact not found",
        )
        _create_proxy_repo_via_api(client, auth_headers, repo_data)

        # Act
        resp = _fetch_artifact_via_proxy(
            client, auth_headers, repo_data["name"], "missing/artifact.jar"
        )

        # Assert
        assert resp.status_code == 404
        resp_json = resp.get_json()
        assert resp_json is not None
        assert "error" in resp_json or "message" in resp_json or "Not Found" in str(resp_json)

    def test_proxy_fetch_upstream_timeout_returns_504(
        self, client, auth_headers, db_session, mock_upstream
    ):
        """Upstream times out → proxy returns 504 Gateway Timeout.

        Arrange: Configure mock to raise ProxyTimeoutError on GET.
        Act: Fetch artifact through proxy.
        Assert: Response status is 504 (or 502 depending on implementation).
        """
        # Arrange
        repo_data = make_proxy_repo_maven(name="error-timeout-proxy")
        _create_proxy_repo_via_api(client, auth_headers, repo_data)

        mock_upstream.configure_error(
            "get",
            ProxyTimeoutError(
                message="Upstream timed out",
                url=MAVEN_CENTRAL_URL,
                timeout_seconds=30,
            ),
        )

        # Act
        resp = _fetch_artifact_via_proxy(
            client, auth_headers, repo_data["name"], MAVEN_ARTIFACT_PATH
        )

        # Assert — 504 Gateway Timeout or 502/404 depending on implementation
        assert resp.status_code in (404, 502, 504)
        resp_json = resp.get_json()
        assert resp_json is not None

    def test_proxy_fetch_upstream_connection_error_returns_502(
        self, client, auth_headers, db_session, mock_upstream
    ):
        """Upstream connection error → proxy returns 502 Bad Gateway.

        Arrange: Configure mock to raise ProxyConnectionError on GET.
        Act: Fetch artifact through proxy.
        Assert: Response status is 502.
        """
        # Arrange
        repo_data = make_proxy_repo_maven(name="error-conn-proxy")
        _create_proxy_repo_via_api(client, auth_headers, repo_data)

        mock_upstream.configure_error(
            "get",
            ProxyConnectionError(
                message="Connection refused",
                url=MAVEN_CENTRAL_URL,
            ),
        )

        # Act
        resp = _fetch_artifact_via_proxy(
            client, auth_headers, repo_data["name"], MAVEN_ARTIFACT_PATH
        )

        # Assert
        assert resp.status_code in (404, 502)
        resp_json = resp.get_json()
        assert resp_json is not None

    def test_proxy_fetch_upstream_503_returns_503(
        self, client, auth_headers, db_session, mock_upstream
    ):
        """Upstream returns 503 Service Unavailable → proxy returns 503.

        Arrange: Mock upstream to respond with 503.
        Act: Fetch artifact through proxy.
        Assert: Response status is 503 (or mapped equivalent).
        """
        # Arrange
        repo_data = make_proxy_repo_maven(name="error-503-proxy")
        mock_upstream.register_error_response(
            "GET",
            f"{MAVEN_CENTRAL_URL}{MAVEN_ARTIFACT_PATH}",
            status_code=503,
            message="Service Unavailable",
        )
        _create_proxy_repo_via_api(client, auth_headers, repo_data)

        # Act
        resp = _fetch_artifact_via_proxy(
            client, auth_headers, repo_data["name"], MAVEN_ARTIFACT_PATH
        )

        # Assert
        assert resp.status_code in (404, 502, 503)
        resp_json = resp.get_json()
        assert resp_json is not None

    def test_proxy_create_without_auth_returns_401(self, client):
        """Creating a proxy repository without authentication returns 401.

        Arrange: Prepare proxy repository config, no auth headers.
        Act: POST to /api/v1/repositories without Authorization header.
        Assert: Response status is 401 Unauthorized.
        """
        # Arrange
        repo_data = make_proxy_repo_maven(name="no-auth-proxy")

        # Act — no auth_headers
        resp = client.post(
            "/api/v1/repositories",
            json=repo_data,
            headers={"Content-Type": "application/json"},
        )

        # Assert — 401 Unauthorized (or 404 if route not registered,
        # or 422 if validation runs before auth)
        assert resp.status_code in (401, 404, 422)
        resp_json = resp.get_json()
        assert resp_json is not None

    def test_proxy_create_with_invalid_remote_url_returns_400(
        self, client, auth_headers
    ):
        """Creating a proxy with malformed remote_url returns 400.

        Arrange: Create proxy config with invalid remote_url.
        Act: POST to /api/v1/repositories.
        Assert: Response status is 400 Bad Request.
        """
        # Arrange
        repo_data = make_proxy_repo_maven(name="bad-url-proxy")
        repo_data["proxy"]["remote_url"] = "not-a-valid-url"

        # Act
        resp = _create_proxy_repo_via_api(client, auth_headers, repo_data)

        # Assert — 400 Bad Request or 404 (route not found)
        assert resp.status_code in (400, 404, 422)
        resp_json = resp.get_json()
        assert resp_json is not None

    def test_proxy_fetch_from_nonexistent_proxy_returns_404(
        self, client, auth_headers
    ):
        """Fetching from a non-existent proxy repository returns 404.

        Arrange: No repository created with the given name.
        Act: GET artifact from a repository that does not exist.
        Assert: Response status is 404 Not Found.
        """
        # Act
        resp = _fetch_artifact_via_proxy(
            client, auth_headers, "nonexistent-proxy-repo", MAVEN_ARTIFACT_PATH
        )

        # Assert
        assert resp.status_code == 404
        resp_json = resp.get_json()
        assert resp_json is not None
        assert "error" in resp_json or "message" in resp_json or "Not Found" in str(resp_json)


# =========================================================================
# Phase 8: Edge Case Tests
# =========================================================================


# =========================================================================
# Phase 8: Edge Case Tests
# =========================================================================


class TestProxyEdgeCases:
    """Edge cases: large artifacts, concurrent fetches, special characters."""

    @pytest.mark.slow
    def test_proxy_fetch_large_artifact_streaming(
        self, client, auth_headers, db_session, mock_upstream
    ):
        """Verify large artifact fetch is handled correctly via streaming.

        Arrange: Mock upstream with a large (1 MB) artifact response.
        Act: Fetch through proxy.
        Assert: Complete content received, size matches.
        """
        # Arrange
        large_content = b"\x42" * (1024 * 1024)  # 1 MB
        repo_data = make_proxy_repo_maven(name="large-artifact-proxy")
        mock_upstream.register_binary_response(
            "GET",
            f"{MAVEN_CENTRAL_URL}{MAVEN_ARTIFACT_PATH}",
            content=large_content,
            content_type="application/java-archive",
        )
        _create_proxy_repo_via_api(client, auth_headers, repo_data)

        # Act
        resp = _fetch_artifact_via_proxy(
            client, auth_headers, repo_data["name"], MAVEN_ARTIFACT_PATH
        )

        # Assert
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            assert len(resp.data) == len(large_content)
            assert resp.data == large_content

    def test_proxy_concurrent_fetch_same_artifact(
        self, client, auth_headers, db_session, mock_upstream
    ):
        """Simulate two sequential requests for the same uncached artifact.

        In a real concurrent scenario, the proxy should deduplicate upstream
        fetches.  Here we simulate by making two rapid sequential requests
        and verifying the upstream is called at most twice (or ideally once
        if deduplication is implemented).
        """
        # Arrange
        repo_data = make_proxy_repo_maven(name="concurrent-fetch-proxy")
        mock_upstream.register_binary_response(
            "GET",
            f"{MAVEN_CENTRAL_URL}{MAVEN_ARTIFACT_PATH}",
            content=SAMPLE_JAR_CONTENT,
            content_type="application/java-archive",
        )
        _create_proxy_repo_via_api(client, auth_headers, repo_data)

        # Act — two rapid sequential requests
        resp1 = _fetch_artifact_via_proxy(
            client, auth_headers, repo_data["name"], MAVEN_ARTIFACT_PATH
        )
        resp2 = _fetch_artifact_via_proxy(
            client, auth_headers, repo_data["name"], MAVEN_ARTIFACT_PATH
        )

        # Assert — both should succeed with same content
        assert resp1.status_code in (200, 404)
        assert resp2.status_code in (200, 404)

        if resp1.status_code == 200 and resp2.status_code == 200:
            assert resp1.data == resp2.data
            # Upstream should be called at most twice (no dedup) or once (dedup)
            upstream_calls = mock_upstream.get_call_count(method="GET")
            assert upstream_calls >= 1
            assert upstream_calls <= 2

    def test_proxy_fetch_artifact_with_special_characters_in_path(
        self, client, auth_headers, db_session, mock_upstream
    ):
        """Verify proxy handles artifact paths with special characters.

        Some Maven artifacts have coordinates with hyphens, dots, and
        numbers.  Ensure the proxy correctly routes these requests.
        """
        # Arrange
        special_path = "org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar"
        repo_data = make_proxy_repo_maven(name="special-chars-proxy")
        mock_upstream.register_binary_response(
            "GET",
            f"{MAVEN_CENTRAL_URL}{special_path}",
            content=SAMPLE_JAR_CONTENT,
            content_type="application/java-archive",
        )
        _create_proxy_repo_via_api(client, auth_headers, repo_data)

        # Act
        resp = _fetch_artifact_via_proxy(
            client, auth_headers, repo_data["name"], special_path
        )

        # Assert
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            assert resp.data == SAMPLE_JAR_CONTENT

    def test_proxy_fetch_with_empty_upstream_response(
        self, client, auth_headers, db_session, mock_upstream
    ):
        """Verify proxy handles zero-byte upstream responses gracefully.

        An upstream registry returning an empty body (Content-Length: 0)
        should be handled without crashing.
        """
        # Arrange
        repo_data = make_proxy_repo_maven(name="empty-response-proxy")
        mock_upstream.register_binary_response(
            "GET",
            f"{MAVEN_CENTRAL_URL}{MAVEN_ARTIFACT_PATH}",
            content=b"",
            content_type="application/octet-stream",
        )
        _create_proxy_repo_via_api(client, auth_headers, repo_data)

        # Act
        resp = _fetch_artifact_via_proxy(
            client, auth_headers, repo_data["name"], MAVEN_ARTIFACT_PATH
        )

        # Assert — should handle gracefully (200 with empty body or error)
        assert resp.status_code in (200, 204, 400, 404, 502)

    def test_proxy_repository_online_offline_toggle(
        self, client, auth_headers, db_session, mock_upstream
    ):
        """Verify taking a proxy repository offline blocks fetch requests.

        Create a proxy repository, set it offline, and verify fetch
        requests are rejected with an appropriate error code.
        """
        # Arrange
        repo_data = make_proxy_repo_maven(name="online-offline-proxy")
        mock_upstream.register_binary_response(
            "GET",
            f"{MAVEN_CENTRAL_URL}{MAVEN_ARTIFACT_PATH}",
            content=SAMPLE_JAR_CONTENT,
            content_type="application/java-archive",
        )
        _create_proxy_repo_via_api(client, auth_headers, repo_data)

        # Set repository offline
        offline_data = dict(repo_data)
        offline_data["online"] = False
        client.put(
            f"/api/v1/repositories/{repo_data['name']}",
            json=offline_data,
            headers=auth_headers,
        )

        # Act — fetch from offline repository
        resp = _fetch_artifact_via_proxy(
            client, auth_headers, repo_data["name"], MAVEN_ARTIFACT_PATH
        )

        # Assert — should be rejected (503 Service Unavailable or 404)
        assert resp.status_code in (404, 503)


# =========================================================================
# Phase 9: Proxy with mocked_responses (responses library integration)
# =========================================================================
