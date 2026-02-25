"""
HTTP Client Wrapper for the Nexus Repository Flask application.

This module provides a configurable, production-grade HTTP client using the
``requests`` library (v2.32.3) with connection pooling, retry logic with
exponential backoff, proxy support, SSL/TLS certificate verification, and
authentication header injection.

It replaces the following Java components from the original Nexus source:

* ``HttpClientFacetImpl.java`` — Proxy repository remote artifact fetching.
* ``HttpClientManagerImpl.java`` — HTTP client lifecycle and connection pool
  management (HikariCP-like pooling replaced by ``requests.Session`` +
  ``HTTPAdapter``).
* ``DefaultsCustomizer`` — Default timeout, user-agent, and pool
  configuration constants.

Primary consumers:

* ``src.app.services.proxy_service`` — Remote artifact fetching (Feature F-102).
* ``src.app.webhooks.dispatcher`` — Webhook HTTP POST delivery (Feature F-503).
* ``src.app.search.elasticsearch_client`` — Elasticsearch communication
  (Feature F-103).
"""

from __future__ import annotations

import logging
import time
from typing import Any, BinaryIO, Generator
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Module-level logger — replaces SLF4J 1.7.36 from Java source
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default constants — replaces ``DefaultsCustomizer`` Java class
# ---------------------------------------------------------------------------

DEFAULT_CONNECT_TIMEOUT: float = 10.0
"""Connection timeout in seconds — matches Java's ``defaultConnectTimeout``."""

DEFAULT_READ_TIMEOUT: float = 30.0
"""Read (socket) timeout in seconds — matches Java's ``defaultSocketTimeout``."""

DEFAULT_MAX_RETRIES: int = 3
"""Maximum number of automatic retries for idempotent requests."""

DEFAULT_BACKOFF_FACTOR: float = 0.5
"""Exponential backoff multiplier between retries (sleep = factor × 2^attempt)."""

DEFAULT_RETRY_STATUS_CODES: tuple[int, ...] = (500, 502, 503, 504)
"""HTTP status codes that trigger an automatic retry on idempotent methods."""

DEFAULT_POOL_CONNECTIONS: int = 10
"""Maximum number of connection pools (per-host) — replaces HikariCP-like pooling."""

DEFAULT_POOL_MAXSIZE: int = 20
"""Maximum total connections in the pool."""

DEFAULT_USER_AGENT: str = "Nexus-Repository/1.0 (Python/Flask)"
"""Default ``User-Agent`` header sent with every outbound request."""

DEFAULT_CHUNK_SIZE: int = 8192
"""Chunk size (8 KB) used for streaming downloads of remote artifacts."""


class HttpClient:
    """Production-grade HTTP client wrapping :class:`requests.Session`.

    Provides connection pooling, configurable retry logic with exponential
    backoff (only for idempotent methods), proxy support, SSL/TLS certificate
    verification, and authentication header injection.

    Usage as a context manager is recommended to ensure proper resource
    clean-up::

        with HttpClient(base_url="https://repo.example.com") as client:
            response = client.get("/v2/catalog")
            data = response.json()
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        base_url: str | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        retry_status_codes: tuple[int, ...] = DEFAULT_RETRY_STATUS_CODES,
        pool_connections: int = DEFAULT_POOL_CONNECTIONS,
        pool_maxsize: int = DEFAULT_POOL_MAXSIZE,
        user_agent: str = DEFAULT_USER_AGENT,
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        verify_ssl: bool = True,
        client_cert: str | None = None,
        client_key: str | None = None,
        auth: tuple[str, str] | None = None,
        bearer_token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialise the HTTP client.

        Parameters
        ----------
        base_url:
            Optional base URL prepended to every request path via
            :func:`urllib.parse.urljoin`.
        connect_timeout:
            TCP connection timeout in seconds.
        read_timeout:
            Socket read timeout in seconds.
        max_retries:
            Maximum number of retries for *idempotent* methods
            (GET, HEAD, OPTIONS).
        backoff_factor:
            Multiplier for exponential backoff between retries.
        retry_status_codes:
            HTTP status codes that trigger an automatic retry.
        pool_connections:
            Number of connection pools to maintain (per host).
        pool_maxsize:
            Maximum number of connections in each pool.
        user_agent:
            ``User-Agent`` header value.
        proxy_host:
            HTTP proxy hostname.
        proxy_port:
            HTTP proxy port (defaults to 80 when *proxy_host* is given).
        verify_ssl:
            Whether to verify the server's TLS certificate.
        client_cert:
            Path to a PEM-encoded client certificate for mutual TLS.
        client_key:
            Path to the private key corresponding to *client_cert*.
        auth:
            ``(username, password)`` tuple for HTTP Basic Authentication.
        bearer_token:
            Bearer token string for ``Authorization: Bearer <token>`` header.
        headers:
            Additional default headers merged into every request.
        """

        self._base_url: str | None = base_url.rstrip("/") if base_url else None
        self._connect_timeout: float = connect_timeout
        self._read_timeout: float = read_timeout

        # -- Create requests.Session with connection pooling ----------------
        self._session: requests.Session = requests.Session()

        # Configure retry strategy (idempotent methods only)
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=list(retry_status_codes),
            allowed_methods=["GET", "HEAD", "OPTIONS"],
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        # -- Proxy configuration -------------------------------------------
        if proxy_host:
            proxy_url = f"http://{proxy_host}:{proxy_port or 80}"
            self._session.proxies = {"http": proxy_url, "https": proxy_url}
            logger.debug("Proxy configured: %s", proxy_url)

        # -- SSL / TLS configuration ---------------------------------------
        self._session.verify = verify_ssl
        if client_cert:
            self._session.cert = (
                (client_cert, client_key) if client_key else client_cert
            )

        # -- Default headers -----------------------------------------------
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json",
            }
        )
        if headers:
            self._session.headers.update(headers)

        # -- Authentication ------------------------------------------------
        if auth:
            self._session.auth = auth
        if bearer_token:
            self._session.headers["Authorization"] = f"Bearer {bearer_token}"

        logger.debug(
            "HttpClient initialised (base_url=%s, connect_timeout=%s, "
            "read_timeout=%s, verify_ssl=%s)",
            self._base_url,
            self._connect_timeout,
            self._read_timeout,
            verify_ssl,
        )

    # ------------------------------------------------------------------
    # Internal request dispatcher
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Execute an HTTP request through the underlying session.

        All public HTTP verb methods (``get``, ``post``, ``put``, ``delete``,
        ``head``) delegate to this single dispatcher, which resolves the full
        URL, applies default timeouts, and performs DEBUG-level logging of the
        request/response cycle.

        Parameters
        ----------
        method:
            HTTP method (e.g. ``"GET"``, ``"POST"``).
        path:
            Relative or absolute URL.  If :attr:`_base_url` is set and *path*
            does not start with ``http://`` or ``https://``, the two are
            joined via :func:`urllib.parse.urljoin`.
        **kwargs:
            Passed through to :meth:`requests.Session.request`.

        Returns
        -------
        requests.Response
            The HTTP response object.

        Raises
        ------
        requests.RequestException
            On connection errors, timeouts, or other transport-level failures.
        """

        # Resolve full URL
        if self._base_url and not path.startswith(("http://", "https://")):
            url = urljoin(self._base_url + "/", path.lstrip("/"))
        else:
            url = path

        # Apply default timeout if not explicitly provided
        kwargs.setdefault("timeout", (self._connect_timeout, self._read_timeout))

        start = time.monotonic()
        try:
            response: requests.Response = self._session.request(method, url, **kwargs)
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.debug(
                "%s %s -> %s (%.1f ms)",
                method,
                url,
                response.status_code,
                elapsed_ms,
            )
            return response
        except requests.RequestException:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.exception(
                "%s %s failed after %.1f ms",
                method,
                url,
                elapsed_ms,
            )
            raise

    # ------------------------------------------------------------------
    # Public HTTP verb methods
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        """Perform an HTTP GET request.

        Parameters
        ----------
        path:
            Relative or absolute URL.
        **kwargs:
            Passed through to :meth:`requests.Session.request`.

        Returns
        -------
        requests.Response
        """
        return self._request("GET", path, **kwargs)

    def post(
        self,
        path: str,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Perform an HTTP POST request.

        Parameters
        ----------
        path:
            Relative or absolute URL.
        data:
            Raw request body (bytes, string, or file-like).
        json:
            JSON-serialisable payload (sets ``Content-Type: application/json``).
        **kwargs:
            Passed through to :meth:`requests.Session.request`.

        Returns
        -------
        requests.Response
        """
        return self._request("POST", path, data=data, json=json, **kwargs)

    def put(
        self,
        path: str,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Perform an HTTP PUT request.

        Parameters
        ----------
        path:
            Relative or absolute URL.
        data:
            Raw request body (bytes, string, or file-like).
        json:
            JSON-serialisable payload.
        **kwargs:
            Passed through to :meth:`requests.Session.request`.

        Returns
        -------
        requests.Response
        """
        return self._request("PUT", path, data=data, json=json, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        """Perform an HTTP DELETE request.

        Parameters
        ----------
        path:
            Relative or absolute URL.
        **kwargs:
            Passed through to :meth:`requests.Session.request`.

        Returns
        -------
        requests.Response
        """
        return self._request("DELETE", path, **kwargs)

    def head(self, path: str, **kwargs: Any) -> requests.Response:
        """Perform an HTTP HEAD request.

        Primarily used by proxy repositories to check remote artifact
        existence before attempting a full download.

        Parameters
        ----------
        path:
            Relative or absolute URL.
        **kwargs:
            Passed through to :meth:`requests.Session.request`.

        Returns
        -------
        requests.Response
        """
        return self._request("HEAD", path, **kwargs)

    # ------------------------------------------------------------------
    # Streaming download support (critical for proxy repos — Feature F-102)
    # ------------------------------------------------------------------

    def stream_get(
        self,
        path: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        **kwargs: Any,
    ) -> Generator[bytes, None, None]:
        """Stream an HTTP GET response in fixed-size chunks.

        This is the primary mechanism for downloading remote artifacts
        through proxy repositories without loading the entire payload into
        memory.  It replaces ``HttpClientFacetImpl.java``'s streaming proxy
        fetch.

        Parameters
        ----------
        path:
            Relative or absolute URL.
        chunk_size:
            Number of bytes per yielded chunk (default 8 KB).
        **kwargs:
            Passed through to :meth:`requests.Session.request`.

        Yields
        ------
        bytes
            Successive chunks of the response body.

        Raises
        ------
        requests.HTTPError
            If the server responds with a non-2xx status code.
        requests.RequestException
            On transport-level failures.
        """

        kwargs["stream"] = True
        response: requests.Response = self._request("GET", path, **kwargs)

        try:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:  # filter out keep-alive empty chunks
                    yield chunk
        finally:
            response.close()

    def download_to_file(
        self,
        path: str,
        target_path: str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        **kwargs: Any,
    ) -> int:
        """Download remote content directly to a local file.

        Uses streaming to avoid loading large artifacts into memory.  The
        caller is responsible for atomic placement (e.g. via
        ``src.app.utils.file_utils.atomic_write``).

        Parameters
        ----------
        path:
            Relative or absolute URL of the remote resource.
        target_path:
            Local filesystem path where content will be written.
        chunk_size:
            Number of bytes per I/O chunk (default 8 KB).
        **kwargs:
            Passed through to :meth:`requests.Session.request`.

        Returns
        -------
        int
            Total number of bytes written to *target_path*.

        Raises
        ------
        requests.HTTPError
            If the server responds with a non-2xx status code.
        requests.RequestException
            On transport-level failures.
        OSError
            If writing to *target_path* fails.
        """

        total_bytes = 0
        kwargs["stream"] = True
        response: requests.Response = self._request("GET", path, **kwargs)

        try:
            response.raise_for_status()
            with open(target_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        fh.write(chunk)
                        total_bytes += len(chunk)
        finally:
            response.close()

        logger.debug(
            "Downloaded %d bytes from %s to %s",
            total_bytes,
            path,
            target_path,
        )
        return total_bytes

    # ------------------------------------------------------------------
    # Response handling utilities
    # ------------------------------------------------------------------

    def fetch_json(
        self,
        path: str,
        method: str = "GET",
        **kwargs: Any,
    ) -> dict[str, Any] | list[Any]:
        """Perform an HTTP request and return the parsed JSON body.

        Parameters
        ----------
        path:
            Relative or absolute URL.
        method:
            HTTP method (default ``"GET"``).
        **kwargs:
            Passed through to :meth:`requests.Session.request`.

        Returns
        -------
        dict or list
            Parsed JSON response.

        Raises
        ------
        ValueError
            If the response body is not valid JSON.
        requests.HTTPError
            If the server responds with a non-2xx status code.
        requests.RequestException
            On transport-level failures.
        """

        response = self._request(method, path, **kwargs)
        response.raise_for_status()

        try:
            return response.json()  # type: ignore[return-value]
        except ValueError as exc:
            logger.error(
                "Failed to parse JSON from %s %s (status=%s): %s",
                method,
                path,
                response.status_code,
                exc,
            )
            raise ValueError(
                f"Response from {method} {path} is not valid JSON"
            ) from exc

    def check_remote_available(self, url: str, timeout: float = 5.0) -> bool:
        """Check whether a remote URL is reachable.

        Sends an HTTP HEAD request and returns ``True`` when the response
        status code is below 400.  All exceptions are silently caught and
        result in ``False``.

        Used by ``src.app.monitoring.health_checks`` for remote registry
        health probes.

        Parameters
        ----------
        url:
            Absolute URL to probe.
        timeout:
            Request timeout in seconds (default 5 s).

        Returns
        -------
        bool
            ``True`` if the remote URL is accessible, ``False`` otherwise.
        """

        try:
            response = self._session.head(
                url, timeout=timeout, allow_redirects=True
            )
            available = response.status_code < 400
            logger.debug(
                "Remote availability check %s -> %s (status=%s)",
                url,
                available,
                response.status_code,
            )
            return available
        except requests.RequestException as exc:
            logger.debug(
                "Remote availability check %s -> False (error=%s)",
                url,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Authentication helpers
    # ------------------------------------------------------------------

    def set_basic_auth(self, username: str, password: str) -> None:
        """Update the session's HTTP Basic Authentication credentials.

        Parameters
        ----------
        username:
            Basic-auth username.
        password:
            Basic-auth password.
        """
        self._session.auth = (username, password)
        logger.debug("Basic authentication configured for user '%s'", username)

    def set_bearer_token(self, token: str) -> None:
        """Set (or replace) the session's Bearer token.

        Parameters
        ----------
        token:
            Bearer token string (without the ``Bearer `` prefix).
        """
        self._session.headers["Authorization"] = f"Bearer {token}"
        logger.debug("Bearer token authentication configured")

    def clear_auth(self) -> None:
        """Remove all authentication from the session."""
        self._session.auth = None
        self._session.headers.pop("Authorization", None)
        logger.debug("Authentication cleared")

    # ------------------------------------------------------------------
    # Resource cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying :class:`requests.Session` and release all
        connection pool resources.

        This method is called automatically when the client is used as a
        context manager.
        """
        self._session.close()
        logger.debug("HttpClient session closed (base_url=%s)", self._base_url)

    # ------------------------------------------------------------------
    # Context manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> HttpClient:
        """Enter the context manager — returns *self*."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit the context manager — closes the session."""
        self.close()


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


def create_http_client(app_config: dict[str, Any] | None = None) -> HttpClient:
    """Create an :class:`HttpClient` from Flask application configuration.

    Reads well-known configuration keys from *app_config* (typically
    ``current_app.config``) and applies sensible defaults when keys are
    absent.

    Recognised configuration keys:

    * ``HTTP_CLIENT_CONNECT_TIMEOUT`` — TCP connect timeout in seconds.
    * ``HTTP_CLIENT_READ_TIMEOUT`` — Socket read timeout in seconds.
    * ``HTTP_CLIENT_MAX_RETRIES`` — Maximum retries for idempotent requests.
    * ``HTTP_CLIENT_BACKOFF_FACTOR`` — Backoff multiplier between retries.
    * ``HTTP_PROXY_HOST`` — Outbound HTTP proxy hostname.
    * ``HTTP_PROXY_PORT`` — Outbound HTTP proxy port.
    * ``HTTP_CLIENT_VERIFY_SSL`` — Enable/disable TLS certificate verification.
    * ``HTTP_CLIENT_USER_AGENT`` — Custom ``User-Agent`` header value.
    * ``HTTP_CLIENT_POOL_CONNECTIONS`` — Connection pools per host.
    * ``HTTP_CLIENT_POOL_MAXSIZE`` — Max connections per pool.
    * ``HTTP_CLIENT_CERT`` — Path to PEM client certificate.
    * ``HTTP_CLIENT_KEY`` — Path to PEM client private key.

    Parameters
    ----------
    app_config:
        Mapping of configuration values (e.g. ``flask.Flask.config``).  If
        ``None``, all defaults are used.

    Returns
    -------
    HttpClient
        A fully configured client instance ready for use.
    """

    if app_config is None:
        app_config = {}

    connect_timeout = float(
        app_config.get("HTTP_CLIENT_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)
    )
    read_timeout = float(
        app_config.get("HTTP_CLIENT_READ_TIMEOUT", DEFAULT_READ_TIMEOUT)
    )
    max_retries = int(
        app_config.get("HTTP_CLIENT_MAX_RETRIES", DEFAULT_MAX_RETRIES)
    )
    backoff_factor = float(
        app_config.get("HTTP_CLIENT_BACKOFF_FACTOR", DEFAULT_BACKOFF_FACTOR)
    )
    proxy_host: str | None = app_config.get("HTTP_PROXY_HOST")
    proxy_port: int | None = app_config.get("HTTP_PROXY_PORT")
    verify_ssl: bool = app_config.get("HTTP_CLIENT_VERIFY_SSL", True)
    user_agent: str = app_config.get(
        "HTTP_CLIENT_USER_AGENT", DEFAULT_USER_AGENT
    )
    pool_connections = int(
        app_config.get("HTTP_CLIENT_POOL_CONNECTIONS", DEFAULT_POOL_CONNECTIONS)
    )
    pool_maxsize = int(
        app_config.get("HTTP_CLIENT_POOL_MAXSIZE", DEFAULT_POOL_MAXSIZE)
    )
    client_cert: str | None = app_config.get("HTTP_CLIENT_CERT")
    client_key: str | None = app_config.get("HTTP_CLIENT_KEY")

    logger.debug(
        "Creating HttpClient from config (connect_timeout=%s, read_timeout=%s, "
        "proxy=%s, verify_ssl=%s)",
        connect_timeout,
        read_timeout,
        proxy_host,
        verify_ssl,
    )

    return HttpClient(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        user_agent=user_agent,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        verify_ssl=verify_ssl,
        client_cert=client_cert,
        client_key=client_key,
    )
