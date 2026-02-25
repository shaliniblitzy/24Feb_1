"""
Mock HTTP client for upstream proxy registry responses.

Provides a reusable mock HTTP client class that simulates responses from
upstream proxy registries (Maven Central, npmjs.org, Docker Hub, PyPI,
NuGet Gallery, Ubuntu APT repos) with configurable status codes and payloads.

Used by proxy integration tests (``tests/integration/services/test_proxy_integration.py``)
and proxy workflow functional tests (``tests/functional/test_proxy_workflow.py``).

All responses are pre-configured — no real network calls are made. The mock
is stateful, recording every request in a call log for test assertions, and
supports error injection for connection-failure and timeout scenario testing.

Supports all 7 repository formats: Maven, npm, Docker, NuGet, PyPI, APT, Raw.

Typical usage::

    client = MockProxyClient()
    client.register_json_response("GET", "https://registry.npmjs.org/lodash",
                                  json_data={"name": "lodash", "versions": {}})
    resp = client.get("https://registry.npmjs.org/lodash")
    assert resp.status_code == 200
    assert resp.json()["name"] == "lodash"

    # Error simulation
    client.configure_error("get", ProxyTimeoutError("Upstream timed out",
                           url="https://registry.npmjs.org", timeout_seconds=30))
    with pytest.raises(ProxyTimeoutError):
        client.get("https://registry.npmjs.org/lodash")
"""

from typing import Any, Dict, List, Optional, Tuple, Union
from io import BytesIO
from datetime import datetime, timezone
from copy import deepcopy
import json
import hashlib
import re


# ---------------------------------------------------------------------------
# Registry URL constants
# ---------------------------------------------------------------------------

MAVEN_CENTRAL_URL: str = "https://repo1.maven.org/maven2/"
"""Base URL for Maven Central Repository."""

NPM_REGISTRY_URL: str = "https://registry.npmjs.org/"
"""Base URL for the npm public registry."""

DOCKER_HUB_URL: str = "https://registry-1.docker.io"
"""Base URL for Docker Hub registry API."""

PYPI_URL: str = "https://pypi.org/"
"""Base URL for the Python Package Index."""

NUGET_URL: str = "https://api.nuget.org/v3/index.json"
"""Base URL for the NuGet V3 service index."""

APT_UBUNTU_URL: str = "http://archive.ubuntu.com/ubuntu/"
"""Base URL for the Ubuntu APT package archive."""


# ---------------------------------------------------------------------------
# HTTP status code constants
# ---------------------------------------------------------------------------

HTTP_OK: int = 200
"""HTTP 200 OK — successful request."""

HTTP_NOT_FOUND: int = 404
"""HTTP 404 Not Found — resource does not exist."""

HTTP_UNAUTHORIZED: int = 401
"""HTTP 401 Unauthorized — authentication required."""

HTTP_FORBIDDEN: int = 403
"""HTTP 403 Forbidden — insufficient permissions."""

HTTP_BAD_GATEWAY: int = 502
"""HTTP 502 Bad Gateway — upstream server returned invalid response."""

HTTP_SERVICE_UNAVAILABLE: int = 503
"""HTTP 503 Service Unavailable — upstream server temporarily unavailable."""

HTTP_GATEWAY_TIMEOUT: int = 504
"""HTTP 504 Gateway Timeout — upstream server did not respond in time."""


# ---------------------------------------------------------------------------
# Custom exception hierarchy
# ---------------------------------------------------------------------------

class ProxyConnectionError(Exception):
    """Simulates ``requests.exceptions.ConnectionError`` for proxy fetches.

    Raised when the mock client is configured to simulate a connection failure
    to an upstream registry (DNS resolution failure, connection refused, etc.).

    Attributes:
        message: Human-readable description of the connection error.
        url: The URL that was being requested when the error occurred.
    """

    def __init__(self, message: str = "Connection error", url: str = "") -> None:
        self.message = message
        self.url = url
        super().__init__(self.message)

    def __str__(self) -> str:
        if self.url:
            return f"ProxyConnectionError({self.message!r}, url={self.url!r})"
        return f"ProxyConnectionError({self.message!r})"

    def __repr__(self) -> str:
        return self.__str__()


class ProxyTimeoutError(Exception):
    """Simulates ``requests.exceptions.Timeout`` for proxy fetches.

    Raised when the mock client is configured to simulate a timeout waiting
    for an upstream registry response.

    Attributes:
        message: Human-readable description of the timeout.
        url: The URL that was being requested when the timeout occurred.
        timeout_seconds: The number of seconds before the timeout triggered.
    """

    def __init__(
        self,
        message: str = "Request timed out",
        url: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.message = message
        self.url = url
        self.timeout_seconds = timeout_seconds
        super().__init__(self.message)

    def __str__(self) -> str:
        return (
            f"ProxyTimeoutError({self.message!r}, url={self.url!r}, "
            f"timeout_seconds={self.timeout_seconds})"
        )

    def __repr__(self) -> str:
        return self.__str__()


class UpstreamRegistryError(Exception):
    """General upstream registry error carrying an HTTP status code.

    Raised when the upstream registry returns an error response that the
    proxy client wishes to propagate as an exception (e.g. 502 Bad Gateway).

    Attributes:
        message: Human-readable description of the upstream error.
        status_code: The HTTP status code returned by the upstream registry.
        url: The URL that produced the error.
    """

    def __init__(
        self,
        message: str = "Upstream registry error",
        status_code: int = HTTP_BAD_GATEWAY,
        url: str = "",
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.url = url
        super().__init__(self.message)

    def __str__(self) -> str:
        return (
            f"UpstreamRegistryError({self.message!r}, "
            f"status_code={self.status_code}, url={self.url!r})"
        )

    def __repr__(self) -> str:
        return self.__str__()


# ---------------------------------------------------------------------------
# MockResponse — simulates requests.Response
# ---------------------------------------------------------------------------

class MockResponse:
    """Simulates a ``requests.Response`` object for HTTP mocking.

    Provides the same primary interface as a real ``requests.Response`` so
    that code under test can interact with it transparently:

    * ``status_code``, ``ok``, ``url``, ``headers``, ``encoding``
    * ``content`` (bytes), ``text`` (str property), ``json()``
    * ``raise_for_status()`` — raises on 4xx/5xx
    * ``iter_content(chunk_size)`` — streaming download simulation
    * Context-manager support for ``with`` statements (stream mode)

    Example::

        resp = MockResponse(status_code=200, json_data={"key": "value"})
        assert resp.ok
        assert resp.json()["key"] == "value"
    """

    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b"",
        headers: Optional[Dict[str, str]] = None,
        json_data: Any = None,
        url: str = "",
    ) -> None:
        self.status_code: int = status_code
        self.url: str = url
        self.encoding: str = "utf-8"
        self.ok: bool = 200 <= status_code < 400

        # Resolve headers — default depends on whether JSON data is provided
        if headers is not None:
            self.headers: Dict[str, str] = dict(headers)
        elif json_data is not None:
            self.headers = {"Content-Type": "application/json"}
        else:
            self.headers = {"Content-Type": "application/octet-stream"}

        # Store private JSON data for fast .json() access
        self._json_data: Any = json_data

        # Resolve body content
        if json_data is not None and not content:
            self.content: bytes = json.dumps(json_data).encode(self.encoding)
            # Ensure Content-Type reflects JSON if not explicitly overridden
            if "Content-Type" not in (headers or {}):
                self.headers["Content-Type"] = "application/json"
        else:
            self.content = content

    # -- text property -------------------------------------------------------

    @property
    def text(self) -> str:
        """Decode the response body as a string using the configured encoding."""
        return self.content.decode(self.encoding)

    # -- json() method -------------------------------------------------------

    def json(self) -> Any:
        """Parse the response body as JSON.

        Returns the pre-set ``json_data`` if it was provided at construction
        time; otherwise falls back to parsing ``self.content``.

        Raises:
            ValueError: If the content cannot be decoded as JSON.
        """
        if self._json_data is not None:
            return deepcopy(self._json_data)
        return json.loads(self.content)

    # -- raise_for_status() --------------------------------------------------

    def raise_for_status(self) -> None:
        """Raise :class:`UpstreamRegistryError` for 4xx / 5xx responses.

        Mirrors the behaviour of ``requests.Response.raise_for_status()``.
        Does nothing if the status code indicates success (< 400).
        """
        if self.status_code >= 400:
            raise UpstreamRegistryError(
                message=f"HTTP {self.status_code} for url: {self.url}",
                status_code=self.status_code,
                url=self.url,
            )

    # -- iter_content() for streaming ----------------------------------------

    def iter_content(self, chunk_size: int = 1024):
        """Yield the response body in fixed-size chunks.

        Simulates streaming download behaviour used by proxy fetch logic
        when downloading large artifacts from upstream registries.

        Args:
            chunk_size: Maximum number of bytes per chunk. Defaults to 1024.

        Yields:
            Chunks of ``bytes`` from the response body.
        """
        stream = BytesIO(self.content)
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    # -- Context-manager support ---------------------------------------------

    def __enter__(self) -> "MockResponse":
        """Enter the context manager (used by ``requests.get(..., stream=True)``)."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the context manager — no resource cleanup needed for mocks."""
        pass


# ---------------------------------------------------------------------------
# MockProxyClient — simulates an HTTP client for upstream proxy interactions
# ---------------------------------------------------------------------------

class MockProxyClient:
    """Mock HTTP client for upstream proxy registry interactions.

    Pre-configure responses for specific URLs and methods, then use
    the client in place of real HTTP calls during proxy fetch testing.
    Supports all 7 repository formats: Maven, npm, Docker, NuGet, PyPI, APT, Raw.

    Features:

    * **Response registration** — map (method, URL-pattern) pairs to
      ``MockResponse`` objects.  URL patterns support ``*`` glob wildcards.
    * **Convenience registrars** — ``register_json_response``,
      ``register_binary_response``, ``register_error_response``.
    * **Error injection** — ``configure_error`` causes the named HTTP method
      to raise the given exception on every call until cleared.
    * **Call logging** — every request is recorded with method, URL, kwargs,
      and a UTC timestamp, enabling rich test assertions.
    * **Context-manager** — ``with`` usage auto-resets state on exit.

    Example::

        client = MockProxyClient()
        client.register_json_response("GET", "https://pypi.org/pypi/flask/json",
                                      {"info": {"name": "flask"}})
        resp = client.get("https://pypi.org/pypi/flask/json")
        assert resp.json()["info"]["name"] == "flask"
        assert client.was_called(method="GET")
    """

    def __init__(self, **kwargs: Any) -> None:
        # Maps (METHOD, url_pattern) tuple → MockResponse
        self._responses: Dict[Tuple[str, str], MockResponse] = {}
        # Fallback responses keyed by HTTP method
        self._default_responses: Dict[str, MockResponse] = {}
        # Chronological log of every request dispatched through this client
        self._call_log: List[Dict[str, Any]] = []
        # Method-name → Exception mapping for error injection
        self._error_config: Dict[str, Exception] = {}
        # Cumulative request counter
        self._request_count: int = 0
        # Simulated latency in milliseconds (metadata only — no real delay)
        self._latency_ms: int = kwargs.get("latency_ms", 0)

    # -- Response Registration -----------------------------------------------

    def register_response(
        self, method: str, url_pattern: str, response: MockResponse
    ) -> None:
        """Register a pre-configured response for a specific method + URL.

        Args:
            method: HTTP method (``GET``, ``HEAD``, ``POST``, etc.).
                    Automatically upper-cased.
            url_pattern: Exact URL string **or** a glob pattern using ``*``
                         as a wildcard (e.g. ``https://pypi.org/pypi/*/json``).
            response: The :class:`MockResponse` to return when matched.
        """
        key: Tuple[str, str] = (method.upper(), url_pattern)
        self._responses[key] = response

    def register_json_response(
        self,
        method: str,
        url_pattern: str,
        json_data: Any,
        status_code: int = HTTP_OK,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        """Convenience: register a JSON-body response.

        Creates a :class:`MockResponse` with the supplied *json_data* and
        registers it for the given method + URL pattern.
        """
        resp = MockResponse(
            status_code=status_code,
            json_data=json_data,
            headers=headers,
            url=url_pattern,
        )
        self.register_response(method, url_pattern, resp)

    def register_binary_response(
        self,
        method: str,
        url_pattern: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        status_code: int = HTTP_OK,
    ) -> None:
        """Convenience: register a binary-body response (artifact downloads).

        Args:
            content: Raw bytes to serve as the response body.
            content_type: MIME type for the ``Content-Type`` header.
        """
        resp = MockResponse(
            status_code=status_code,
            content=content,
            headers={"Content-Type": content_type},
            url=url_pattern,
        )
        self.register_response(method, url_pattern, resp)

    def register_error_response(
        self,
        method: str,
        url_pattern: str,
        status_code: int,
        message: str = "",
    ) -> None:
        """Convenience: register an HTTP error response (404, 502, 503, etc.).

        The response body is a JSON object ``{"error": message}`` when a
        *message* is provided, or an empty body otherwise.
        """
        error_body: Optional[Dict[str, str]] = None
        if message:
            error_body = {"error": message}

        resp = MockResponse(
            status_code=status_code,
            json_data=error_body,
            url=url_pattern,
        )
        self.register_response(method, url_pattern, resp)

    def set_default_response(self, method: str, response: MockResponse) -> None:
        """Set a fallback response for a method when no URL pattern matches.

        Args:
            method: HTTP method (upper-cased internally).
            response: The :class:`MockResponse` to use as fallback.
        """
        self._default_responses[method.upper()] = response

    # -- HTTP Method Dispatchers ---------------------------------------------

    def get(self, url: str, **kwargs: Any) -> MockResponse:
        """Simulate an HTTP GET request.

        Records the call, checks for configured errors, looks up a matching
        registered response, and returns it.  Raises
        :class:`ProxyConnectionError` if no registered response matches.
        """
        return self._dispatch("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> MockResponse:
        """Simulate an HTTP HEAD request."""
        return self._dispatch("HEAD", url, **kwargs)

    def post(
        self, url: str, data: Any = None, json: Any = None, **kwargs: Any
    ) -> MockResponse:
        """Simulate an HTTP POST request (includes data/json in call log)."""
        kwargs["data"] = data
        kwargs["json"] = json
        return self._dispatch("POST", url, **kwargs)

    def put(self, url: str, data: Any = None, **kwargs: Any) -> MockResponse:
        """Simulate an HTTP PUT request."""
        kwargs["data"] = data
        return self._dispatch("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> MockResponse:
        """Simulate an HTTP DELETE request."""
        return self._dispatch("DELETE", url, **kwargs)

    # -- Internal dispatch logic ---------------------------------------------

    def _dispatch(self, method: str, url: str, **kwargs: Any) -> MockResponse:
        """Core dispatch: log → error-check → match → return.

        This method is the single internal entry point used by ``get``,
        ``head``, ``post``, ``put``, and ``delete``.
        """
        # Record the call
        log_entry: Dict[str, Any] = {
            "method": method,
            "url": url,
            "kwargs": deepcopy(kwargs),
            "timestamp": datetime.now(timezone.utc),
        }
        self._call_log.append(log_entry)
        self._request_count += 1

        # Check for configured errors (keyed by lower-case method name)
        error = self._error_config.get(method.lower())
        if error is not None:
            raise error

        # Find a matching registered response
        matched = self._find_matching_response(method, url)
        if matched is not None:
            return matched

        # No match found — raise ProxyConnectionError
        raise ProxyConnectionError(
            message=f"No registered response for {method} {url}",
            url=url,
        )

    def _find_matching_response(
        self, method: str, url: str
    ) -> Optional[MockResponse]:
        """Search registered responses for a match.

        Resolution order:
        1. Exact ``(method, url)`` key match.
        2. Glob-pattern match — ``*`` wildcards converted to ``.*`` regex.
        3. Default response for the HTTP method.
        4. ``None`` if nothing matched.
        """
        method_upper = method.upper()

        # 1. Exact match
        exact_key: Tuple[str, str] = (method_upper, url)
        if exact_key in self._responses:
            return self._responses[exact_key]

        # 2. Glob-pattern match (iterate registered patterns)
        for (reg_method, pattern), response in self._responses.items():
            if reg_method != method_upper:
                continue
            # Skip entries that are exact URLs (already tried above)
            if "*" not in pattern:
                continue
            # Convert glob pattern to regex: escape everything except *
            regex_pattern = re.escape(pattern).replace(r"\*", ".*")
            if re.fullmatch(regex_pattern, url):
                return response

        # 3. Default response for this method
        if method_upper in self._default_responses:
            return self._default_responses[method_upper]

        # 4. No match
        return None

    # -- Error Injection -----------------------------------------------------

    def configure_error(self, method_name: str, error: Exception) -> None:
        """Configure an exception to be raised when the named method is called.

        Args:
            method_name: Lower-case HTTP method name (``"get"``, ``"head"``, etc.).
            error: The exception instance to raise.  Typically
                   :class:`ProxyConnectionError` or :class:`ProxyTimeoutError`.
        """
        self._error_config[method_name.lower()] = error

    def clear_error(self, method_name: str) -> None:
        """Remove a previously configured error for a method.

        Args:
            method_name: The method whose error configuration should be cleared.
        """
        self._error_config.pop(method_name.lower(), None)

    def clear_all_errors(self) -> None:
        """Remove all configured errors."""
        self._error_config.clear()

    # -- Call Logging & Assertions -------------------------------------------

    def get_call_log(self) -> List[Dict[str, Any]]:
        """Return a deep copy of the chronological call log.

        Each entry is a dict with keys ``method``, ``url``, ``kwargs``,
        and ``timestamp`` (a :class:`datetime.datetime`).
        """
        return deepcopy(self._call_log)

    def get_call_count(self, method: Optional[str] = None) -> int:
        """Return the number of requests dispatched through this client.

        Args:
            method: If provided, count only requests with this HTTP method
                    (case-insensitive).  If ``None``, return the total count.
        """
        if method is None:
            return self._request_count
        method_upper = method.upper()
        return sum(1 for entry in self._call_log if entry["method"] == method_upper)

    def get_calls_to_url(self, url_pattern: str) -> List[Dict[str, Any]]:
        """Return all logged calls whose URL matches *url_pattern*.

        Supports glob-style ``*`` wildcards in *url_pattern*.

        Args:
            url_pattern: Exact URL or glob pattern to match against.

        Returns:
            List of matching call-log entries (deep copies).
        """
        results: List[Dict[str, Any]] = []
        for entry in self._call_log:
            entry_url: str = entry["url"]
            if entry_url == url_pattern:
                results.append(deepcopy(entry))
            elif "*" in url_pattern:
                regex_pattern = re.escape(url_pattern).replace(r"\*", ".*")
                if re.fullmatch(regex_pattern, entry_url):
                    results.append(deepcopy(entry))
        return results

    def was_called(
        self, method: Optional[str] = None, url: Optional[str] = None
    ) -> bool:
        """Check whether a matching call was logged.

        Args:
            method: If provided, only consider calls with this HTTP method.
            url: If provided, only consider calls to this exact URL.

        Returns:
            ``True`` if at least one matching call exists in the log.
        """
        for entry in self._call_log:
            method_match = method is None or entry["method"] == method.upper()
            url_match = url is None or entry["url"] == url
            if method_match and url_match:
                return True
        return False

    # -- State Reset ---------------------------------------------------------

    def reset(self) -> None:
        """Reset the client to its initial empty state.

        Clears all registered responses, default responses, the call log,
        error configurations, and the request counter.
        """
        self._responses.clear()
        self._default_responses.clear()
        self._call_log.clear()
        self._error_config.clear()
        self._request_count = 0

    # -- Context Manager Support ---------------------------------------------

    def __enter__(self) -> "MockProxyClient":
        """Enter context manager — returns self for use in ``with`` blocks."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit context manager — auto-resets all state."""
        self.reset()


# ---------------------------------------------------------------------------
# Format-Specific Response Builders
# ---------------------------------------------------------------------------


def make_maven_metadata_response(
    group_id: str,
    artifact_id: str,
    versions: List[str],
) -> MockResponse:
    """Build a Maven ``maven-metadata.xml`` response.

    Creates a well-formed Maven metadata XML document containing the group,
    artifact, and version information that a proxy client would receive from
    Maven Central when requesting ``/<group-path>/<artifact>/maven-metadata.xml``.

    Args:
        group_id: Maven group ID (e.g. ``"org.apache.commons"``).
        artifact_id: Maven artifact ID (e.g. ``"commons-lang3"``).
        versions: List of published version strings, ordered chronologically.

    Returns:
        A :class:`MockResponse` with status 200, Content-Type ``application/xml``,
        and the rendered XML body.
    """
    latest = versions[-1] if versions else ""
    release = latest  # In Maven, release typically equals latest for releases

    version_elements = "\n".join(
        f"      <version>{v}</version>" for v in versions
    )

    xml_body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<metadata>\n"
        f"  <groupId>{group_id}</groupId>\n"
        f"  <artifactId>{artifact_id}</artifactId>\n"
        "  <versioning>\n"
        f"    <latest>{latest}</latest>\n"
        f"    <release>{release}</release>\n"
        "    <versions>\n"
        f"{version_elements}\n"
        "    </versions>\n"
        f"    <lastUpdated>{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}</lastUpdated>\n"
        "  </versioning>\n"
        "</metadata>\n"
    )

    return MockResponse(
        status_code=HTTP_OK,
        content=xml_body.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
        url=f"{MAVEN_CENTRAL_URL}{group_id.replace('.', '/')}/{artifact_id}/maven-metadata.xml",
    )


def make_npm_package_response(
    name: str,
    versions: Dict[str, dict],
) -> MockResponse:
    """Build an npm registry package metadata response (JSON).

    Produces a JSON structure mirroring the npm public registry's package
    endpoint (``GET /<package-name>``), including ``name``, ``versions``,
    and ``dist-tags``.

    Args:
        name: npm package name (e.g. ``"lodash"``).
        versions: Mapping of version string to metadata dict.  Each metadata
                  dict may contain ``name``, ``version``, ``description``,
                  and ``dist`` (with ``tarball`` URL and ``shasum``).

    Returns:
        A :class:`MockResponse` with status 200 and JSON body.
    """
    version_keys = list(versions.keys())
    latest_version = version_keys[-1] if version_keys else "0.0.0"

    enriched_versions: Dict[str, Any] = {}
    for ver, meta in versions.items():
        entry: Dict[str, Any] = {
            "name": name,
            "version": ver,
            "description": meta.get("description", f"{name} v{ver}"),
        }
        if "dist" not in meta:
            tarball_content = f"{name}-{ver}".encode("utf-8")
            entry["dist"] = {
                "tarball": f"{NPM_REGISTRY_URL}{name}/-/{name}-{ver}.tgz",
                "shasum": hashlib.sha1(tarball_content).hexdigest(),
            }
        else:
            entry["dist"] = meta["dist"]
        for k, v in meta.items():
            if k not in entry:
                entry[k] = v
        enriched_versions[ver] = entry

    payload: Dict[str, Any] = {
        "name": name,
        "dist-tags": {"latest": latest_version},
        "versions": enriched_versions,
    }

    return MockResponse(
        status_code=HTTP_OK,
        json_data=payload,
        headers={"Content-Type": "application/json"},
        url=f"{NPM_REGISTRY_URL}{name}",
    )


def make_docker_manifest_response(
    repository: str,
    tag: str,
    layers: List[dict],
) -> MockResponse:
    """Build a Docker V2 manifest response.

    Produces a JSON manifest conforming to the Docker Image Manifest V2,
    Schema 2 specification, as returned by a Docker registry's
    ``GET /v2/<name>/manifests/<reference>`` endpoint.

    Args:
        repository: Docker repository name (e.g. ``"library/nginx"``).
        tag: Image tag (e.g. ``"latest"``).
        layers: List of layer descriptor dicts, each with ``mediaType``,
                ``size``, and ``digest``.

    Returns:
        A :class:`MockResponse` with status 200 and Docker V2 manifest body.
    """
    config_content = f"{repository}:{tag}".encode("utf-8")
    config_digest = f"sha256:{hashlib.sha256(config_content).hexdigest()}"

    enriched_layers: List[Dict[str, Any]] = []
    for layer in layers:
        enriched: Dict[str, Any] = {
            "mediaType": layer.get(
                "mediaType",
                "application/vnd.docker.image.rootfs.diff.tar.gzip",
            ),
            "size": layer.get("size", 1024),
            "digest": layer.get(
                "digest",
                f"sha256:{hashlib.sha256(json.dumps(layer).encode()).hexdigest()}",
            ),
        }
        enriched_layers.append(enriched)

    manifest: Dict[str, Any] = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "size": len(config_content),
            "digest": config_digest,
        },
        "layers": enriched_layers,
    }

    return MockResponse(
        status_code=HTTP_OK,
        json_data=manifest,
        headers={
            "Content-Type": "application/vnd.docker.distribution.manifest.v2+json",
            "Docker-Content-Digest": config_digest,
        },
        url=f"{DOCKER_HUB_URL}/v2/{repository}/manifests/{tag}",
    )


def make_pypi_package_response(
    name: str,
    versions: List[str],
) -> MockResponse:
    """Build a PyPI JSON API response for a package.

    Produces a JSON structure mirroring the PyPI JSON API endpoint
    (``GET /pypi/<project>/json``).

    Args:
        name: Python package name (e.g. ``"flask"``).
        versions: List of published version strings.

    Returns:
        A :class:`MockResponse` with status 200 and JSON body.
    """
    latest_version = versions[-1] if versions else "0.0.0"

    releases: Dict[str, List[Dict[str, Any]]] = {}
    for ver in versions:
        filename = f"{name}-{ver}.tar.gz"
        file_content = f"{name}-{ver}".encode("utf-8")
        releases[ver] = [
            {
                "filename": filename,
                "url": f"https://files.pythonhosted.org/packages/{filename}",
                "size": len(file_content),
                "digests": {
                    "md5": hashlib.md5(file_content).hexdigest(),
                    "sha256": hashlib.sha256(file_content).hexdigest(),
                },
                "packagetype": "sdist",
                "python_requires": ">=3.8",
            }
        ]

    payload: Dict[str, Any] = {
        "info": {
            "name": name,
            "version": latest_version,
            "summary": f"{name} package",
            "home_page": f"https://pypi.org/project/{name}/",
            "author": "Test Author",
            "license": "MIT",
            "requires_python": ">=3.8",
        },
        "releases": releases,
        "urls": releases.get(latest_version, []),
    }

    return MockResponse(
        status_code=HTTP_OK,
        json_data=payload,
        headers={"Content-Type": "application/json"},
        url=f"{PYPI_URL}pypi/{name}/json",
    )


def make_nuget_index_response() -> MockResponse:
    """Build a NuGet V3 service index response.

    Produces the JSON document returned by the NuGet V3 service index
    endpoint (``GET /v3/index.json``), listing available API resources
    such as package content, search, and registration.

    Returns:
        A :class:`MockResponse` with status 200 and JSON body.
    """
    payload: Dict[str, Any] = {
        "version": "3.0.0",
        "resources": [
            {
                "@id": "https://api.nuget.org/v3-flatcontainer/",
                "@type": "PackageBaseAddress/3.0.0",
                "comment": "Base URL of Azure storage where NuGet package content is stored",
            },
            {
                "@id": "https://api.nuget.org/v3/registration5-semver1/",
                "@type": "RegistrationsBaseUrl",
                "comment": "Base URL of registration blobs",
            },
            {
                "@id": "https://azuresearch-usnc.nuget.org/query",
                "@type": "SearchQueryService",
                "comment": "Query endpoint of NuGet Search service",
            },
            {
                "@id": "https://azuresearch-usnc.nuget.org/autocomplete",
                "@type": "SearchAutocompleteService",
                "comment": "Autocomplete endpoint of NuGet Search service",
            },
            {
                "@id": "https://api.nuget.org/v3/catalog0/index.json",
                "@type": "Catalog/3.0.0",
                "comment": "Index of the NuGet package catalog",
            },
        ],
    }

    return MockResponse(
        status_code=HTTP_OK,
        json_data=payload,
        headers={"Content-Type": "application/json"},
        url=NUGET_URL,
    )


def make_apt_packages_response(packages: List[dict]) -> MockResponse:
    """Build an APT ``Packages`` file response.

    Produces a plain-text response in Debian control-file format, as would
    be returned when fetching a ``Packages`` (or ``Packages.gz``) index from
    an APT repository.

    Args:
        packages: List of package descriptor dicts.  Each dict should contain
                  keys such as ``Package``, ``Version``, ``Architecture``,
                  ``Filename``, ``Size``, ``SHA256``, and ``Description``.

    Returns:
        A :class:`MockResponse` with status 200, Content-Type ``text/plain``,
        and the rendered Packages body.
    """
    entries: List[str] = []
    for pkg in packages:
        pkg_name = pkg.get("Package", "unknown")
        pkg_version = pkg.get("Version", "0.0.0")
        architecture = pkg.get("Architecture", "amd64")
        filename = pkg.get(
            "Filename",
            f"pool/main/{pkg_name[0]}/{pkg_name}/{pkg_name}_{pkg_version}_{architecture}.deb",
        )
        size = pkg.get("Size", "1024")
        description = pkg.get("Description", f"{pkg_name} package")

        content_blob = f"{pkg_name}-{pkg_version}".encode("utf-8")
        sha256 = pkg.get("SHA256", hashlib.sha256(content_blob).hexdigest())
        md5sum = pkg.get("MD5sum", hashlib.md5(content_blob).hexdigest())
        sha1_val = pkg.get("SHA1", hashlib.sha1(content_blob).hexdigest())

        entry_lines = [
            f"Package: {pkg_name}",
            f"Version: {pkg_version}",
            f"Architecture: {architecture}",
            f"Filename: {filename}",
            f"Size: {size}",
            f"MD5sum: {md5sum}",
            f"SHA1: {sha1_val}",
            f"SHA256: {sha256}",
            f"Description: {description}",
        ]
        for key, value in pkg.items():
            if key not in {
                "Package", "Version", "Architecture", "Filename",
                "Size", "MD5sum", "SHA1", "SHA256", "Description",
            }:
                entry_lines.append(f"{key}: {value}")

        entries.append("\n".join(entry_lines))

    body = "\n\n".join(entries) + "\n"

    return MockResponse(
        status_code=HTTP_OK,
        content=body.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
        url=f"{APT_UBUNTU_URL}dists/jammy/main/binary-amd64/Packages",
    )


def make_artifact_download_response(
    content: bytes,
    content_type: str = "application/octet-stream",
) -> MockResponse:
    """Build a binary artifact download response (any format).

    Creates a :class:`MockResponse` representing the raw binary download of
    an artifact from an upstream registry.  Applicable to any repository
    format (Maven JAR, npm tarball, Docker layer blob, PyPI sdist, etc.).

    Args:
        content: The raw bytes of the artifact.
        content_type: MIME type for the ``Content-Type`` header.

    Returns:
        A :class:`MockResponse` with status 200 and binary body.
    """
    sha256_digest = hashlib.sha256(content).hexdigest()

    return MockResponse(
        status_code=HTTP_OK,
        content=content,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(content)),
            "X-Checksum-Sha256": sha256_digest,
        },
    )


# ---------------------------------------------------------------------------
# Module-Level Factory Functions
# ---------------------------------------------------------------------------


def create_mock_proxy_client(**kwargs: Any) -> MockProxyClient:
    """Convenience factory: create a fresh :class:`MockProxyClient`.

    Any keyword arguments are forwarded to the ``MockProxyClient`` constructor.

    Returns:
        A new :class:`MockProxyClient` instance.
    """
    return MockProxyClient(**kwargs)


def create_preconfigured_maven_client() -> MockProxyClient:
    """Create a :class:`MockProxyClient` pre-loaded with Maven Central responses.

    Registers common Maven metadata and artifact download responses for
    typical test paths (e.g. ``commons-lang3`` and ``junit``).

    Returns:
        A :class:`MockProxyClient` configured for Maven Central testing.
    """
    client = MockProxyClient()

    # Register maven-metadata.xml for commons-lang3
    metadata_resp = make_maven_metadata_response(
        group_id="org.apache.commons",
        artifact_id="commons-lang3",
        versions=["3.12.0", "3.13.0", "3.14.0"],
    )
    client.register_response(
        "GET",
        f"{MAVEN_CENTRAL_URL}org/apache/commons/commons-lang3/maven-metadata.xml",
        metadata_resp,
    )

    # Register maven-metadata.xml for junit
    junit_metadata = make_maven_metadata_response(
        group_id="junit",
        artifact_id="junit",
        versions=["4.12", "4.13", "4.13.2"],
    )
    client.register_response(
        "GET",
        f"{MAVEN_CENTRAL_URL}junit/junit/maven-metadata.xml",
        junit_metadata,
    )

    # Register a generic artifact download for commons-lang3 JAR
    jar_content = b"PK\x03\x04" + b"\x00" * 100  # Minimal ZIP/JAR magic bytes
    artifact_resp = make_artifact_download_response(
        content=jar_content,
        content_type="application/java-archive",
    )
    client.register_response(
        "GET",
        f"{MAVEN_CENTRAL_URL}org/apache/commons/commons-lang3/3.14.0/commons-lang3-3.14.0.jar",
        artifact_resp,
    )

    # Register a default 404 for unmatched GETs
    client.set_default_response(
        "GET",
        MockResponse(status_code=HTTP_NOT_FOUND, json_data={"error": "Not Found"}),
    )

    return client


def create_preconfigured_npm_client() -> MockProxyClient:
    """Create a :class:`MockProxyClient` pre-loaded with npmjs.org responses.

    Registers package metadata for commonly used test packages
    (``lodash`` and ``express``).

    Returns:
        A :class:`MockProxyClient` configured for npm registry testing.
    """
    client = MockProxyClient()

    # Register lodash package metadata
    lodash_resp = make_npm_package_response(
        name="lodash",
        versions={
            "4.17.20": {"description": "Lodash modular utilities"},
            "4.17.21": {"description": "Lodash modular utilities"},
        },
    )
    client.register_response("GET", f"{NPM_REGISTRY_URL}lodash", lodash_resp)

    # Register express package metadata
    express_resp = make_npm_package_response(
        name="express",
        versions={
            "4.18.2": {"description": "Fast web framework for Node.js"},
            "4.19.2": {"description": "Fast web framework for Node.js"},
        },
    )
    client.register_response("GET", f"{NPM_REGISTRY_URL}express", express_resp)

    # Register a tarball download for lodash
    tarball_content = b"\x1f\x8b\x08" + b"\x00" * 100  # Minimal gzip header
    tarball_resp = make_artifact_download_response(
        content=tarball_content,
        content_type="application/gzip",
    )
    client.register_response(
        "GET",
        f"{NPM_REGISTRY_URL}lodash/-/lodash-4.17.21.tgz",
        tarball_resp,
    )

    # Default 404 for unmatched GETs
    client.set_default_response(
        "GET",
        MockResponse(status_code=HTTP_NOT_FOUND, json_data={"error": "Not Found"}),
    )

    return client


def create_preconfigured_docker_client() -> MockProxyClient:
    """Create a :class:`MockProxyClient` pre-loaded with Docker Hub responses.

    Registers Docker V2 manifests for common test images
    (``library/nginx`` and ``library/alpine``).

    Returns:
        A :class:`MockProxyClient` configured for Docker Hub testing.
    """
    client = MockProxyClient()

    # Register nginx manifest
    nginx_manifest = make_docker_manifest_response(
        repository="library/nginx",
        tag="latest",
        layers=[
            {
                "size": 27_145_600,
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
            },
            {
                "size": 614,
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
            },
        ],
    )
    client.register_response(
        "GET",
        f"{DOCKER_HUB_URL}/v2/library/nginx/manifests/latest",
        nginx_manifest,
    )

    # Register alpine manifest
    alpine_manifest = make_docker_manifest_response(
        repository="library/alpine",
        tag="3.19",
        layers=[
            {
                "size": 3_408_896,
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
            },
        ],
    )
    client.register_response(
        "GET",
        f"{DOCKER_HUB_URL}/v2/library/alpine/manifests/3.19",
        alpine_manifest,
    )

    # Register a layer blob download (glob pattern for any blob digest)
    layer_blob = b"\x1f\x8b\x08" + b"\x00" * 200  # Minimal gzip
    layer_resp = make_artifact_download_response(
        content=layer_blob,
        content_type="application/vnd.docker.image.rootfs.diff.tar.gzip",
    )
    client.register_response(
        "GET",
        f"{DOCKER_HUB_URL}/v2/library/nginx/blobs/*",
        layer_resp,
    )

    # Default 404 for unmatched GETs
    client.set_default_response(
        "GET",
        MockResponse(
            status_code=HTTP_NOT_FOUND,
            json_data={"errors": [{"code": "MANIFEST_UNKNOWN"}]},
        ),
    )

    return client
