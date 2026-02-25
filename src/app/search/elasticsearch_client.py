"""
Elasticsearch client wrapper module for the Nexus Repository Flask application.

This is the foundational module of the ``src.app.search`` package, providing
centralized connection management, health checking, cluster information
retrieval, and connection lifecycle operations.  It replaces the Java
Elasticsearch 2.4.3 client from the original Sonatype Nexus Repository system
using the ``elasticsearch-py`` 7.17.12 Python client.

The client reads its connection URL from the ``ELASTICSEARCH_URL`` environment
variable (or Flask application configuration) and handles connection failures
gracefully — the Flask application continues to function when Elasticsearch is
unavailable; search features simply degrade.

Supported Flask app config keys
-------------------------------
- ``ELASTICSEARCH_URL``: Connection URL (default: ``http://localhost:9200``)
- ``ELASTICSEARCH_TIMEOUT``: Request timeout in seconds (default: 30)
- ``ELASTICSEARCH_MAX_RETRIES``: Maximum retry attempts (default: 3)
- ``ELASTICSEARCH_RETRY_ON_TIMEOUT``: Retry on timeout (default: ``True``)
- ``ELASTICSEARCH_SNIFF_ON_START``: Sniff cluster topology on start (default: ``False``)
- ``ELASTICSEARCH_SNIFF_ON_FAIL``: Sniff on connection failure (default: ``False``)
"""

from __future__ import annotations

import os
import logging
from typing import Any

from elasticsearch import (
    Elasticsearch,
    ConnectionError as ESConnectionError,
    ConnectionTimeout,
    TransportError,
)

# ---------------------------------------------------------------------------
# Module-level logger (replaces SLF4J 1.7.36 + Logback 1.2.13)
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants — sensible defaults following the twelve-factor app
# methodology.  All values can be overridden via Flask app.config or
# environment variables.
# ---------------------------------------------------------------------------
DEFAULT_ELASTICSEARCH_URL: str = "http://localhost:9200"
"""Default Elasticsearch connection URL used when no explicit URL is provided."""

DEFAULT_TIMEOUT: int = 30
"""Default request timeout in seconds for all Elasticsearch operations."""

DEFAULT_MAX_RETRIES: int = 3
"""Default maximum number of retries for failed Elasticsearch requests."""

DEFAULT_RETRY_ON_TIMEOUT: bool = True
"""Whether to automatically retry Elasticsearch requests that time out."""

# ---------------------------------------------------------------------------
# Module-level singleton state — only one ElasticsearchClient wrapper exists
# per application process.
# ---------------------------------------------------------------------------
_es_client: "ElasticsearchClient" | None = None
_is_initialized: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# ElasticsearchClient class
# ═══════════════════════════════════════════════════════════════════════════
class ElasticsearchClient:
    """High-level wrapper around the ``elasticsearch-py`` :class:`Elasticsearch`
    client providing centralised connection management, health checking, and
    connection lifecycle operations.

    The wrapper is intentionally thin — callers that need the full
    ``elasticsearch-py`` API can obtain the raw client via the :attr:`client`
    property.

    Parameters
    ----------
    url:
        Elasticsearch connection URL.  If *None*, the value is resolved from
        the ``ELASTICSEARCH_URL`` environment variable or
        :data:`DEFAULT_ELASTICSEARCH_URL`.
    **kwargs:
        Additional keyword arguments forwarded to the ``Elasticsearch``
        constructor.  Recognised keys include ``timeout``, ``max_retries``,
        ``retry_on_timeout``, ``sniff_on_start``, and
        ``sniff_on_connection_fail``.
    """

    def __init__(self, url: str | None = None, **kwargs: Any) -> None:
        # Resolve the Elasticsearch URL from the parameter, environment, or
        # default constant — in that priority order.
        resolved_url: str = url or os.environ.get(
            "ELASTICSEARCH_URL", DEFAULT_ELASTICSEARCH_URL
        )

        # Support a comma-separated list of hosts for multi-node clusters.
        # A single URL string becomes a single-element list.
        if "," in resolved_url:
            hosts: list[str] = [h.strip() for h in resolved_url.split(",") if h.strip()]
        else:
            hosts = [resolved_url]

        # Store configuration for later reference / reconnect.
        self._url: str = resolved_url
        self._kwargs: dict[str, Any] = kwargs
        self._connected: bool = False

        # Create the underlying elasticsearch-py client instance.
        self._client: Elasticsearch = Elasticsearch(
            hosts=hosts,
            timeout=kwargs.get("timeout", DEFAULT_TIMEOUT),
            max_retries=kwargs.get("max_retries", DEFAULT_MAX_RETRIES),
            retry_on_timeout=kwargs.get("retry_on_timeout", DEFAULT_RETRY_ON_TIMEOUT),
            sniff_on_start=kwargs.get("sniff_on_start", False),
            sniff_on_connection_fail=kwargs.get("sniff_on_connection_fail", False),
        )

        logger.info("Initializing Elasticsearch client with URL: %s", resolved_url)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def client(self) -> Elasticsearch:
        """Return the underlying ``elasticsearch-py`` client instance.

        This gives callers direct access to the full Elasticsearch API when
        the convenience methods on this wrapper are insufficient.
        """
        return self._client

    @property
    def is_connected(self) -> bool:
        """Return ``True`` if the most recent connectivity check succeeded."""
        return self._connected

    @property
    def url(self) -> str:
        """Return the configured Elasticsearch connection URL."""
        return self._url

    # ------------------------------------------------------------------
    # Health checking methods
    # ------------------------------------------------------------------
    def check_health(self) -> dict[str, Any]:
        """Check the Elasticsearch cluster health.

        Returns a dictionary with at least a ``"status"`` key.  When the
        cluster is reachable, additional keys such as ``cluster_name``,
        ``number_of_nodes``, ``active_shards``, ``relocating_shards``, and
        ``unassigned_shards`` are included.

        When the cluster is unreachable, the returned dictionary contains
        ``{"status": "unavailable", "error": "<message>"}`` and the
        :attr:`is_connected` flag is set to ``False``.
        """
        try:
            health: dict[str, Any] = self._client.cluster.health()
            self._connected = True
            logger.debug(
                "Elasticsearch cluster health: %s", health.get("status")
            )
            return {
                "status": health.get("status", "unknown"),
                "cluster_name": health.get("cluster_name"),
                "number_of_nodes": health.get("number_of_nodes"),
                "active_shards": health.get("active_shards"),
                "relocating_shards": health.get("relocating_shards"),
                "unassigned_shards": health.get("unassigned_shards"),
            }
        except (ESConnectionError, ConnectionTimeout, TransportError) as exc:
            self._connected = False
            logger.warning("Elasticsearch health check failed: %s", exc)
            return {"status": "unavailable", "error": str(exc)}
        except Exception as exc:  # pragma: no cover — unexpected errors
            self._connected = False
            logger.error("Unexpected error during health check: %s", exc)
            return {"status": "error", "error": str(exc)}

    def ping(self) -> bool:
        """Perform a simple connectivity check against Elasticsearch.

        Returns ``True`` if the cluster responded, ``False`` otherwise.  This
        method *never* raises an exception — connection failures are handled
        internally and the :attr:`is_connected` flag is updated accordingly.
        """
        try:
            result: bool = self._client.ping()
            self._connected = result
            return result
        except Exception:
            self._connected = False
            return False

    def get_cluster_info(self) -> dict[str, Any]:
        """Retrieve cluster information such as name, version, and tagline.

        Returns a dictionary with the cluster info payload on success, or an
        empty dictionary when Elasticsearch is unavailable.
        """
        try:
            info: dict[str, Any] = self._client.info()
            self._connected = True
            logger.debug("Elasticsearch cluster info retrieved successfully")
            return {
                "name": info.get("name"),
                "cluster_name": info.get("cluster_name"),
                "cluster_uuid": info.get("cluster_uuid"),
                "version": info.get("version", {}),
                "tagline": info.get("tagline"),
            }
        except (ESConnectionError, ConnectionTimeout, TransportError) as exc:
            self._connected = False
            logger.warning("Failed to retrieve cluster info: %s", exc)
            return {}
        except Exception as exc:  # pragma: no cover
            self._connected = False
            logger.error("Unexpected error retrieving cluster info: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        """Attempt to establish / verify the connection to Elasticsearch.

        This method is idempotent — it is safe to call multiple times.
        Returns ``True`` when the cluster is reachable, ``False`` otherwise.
        """
        result = self.ping()
        if result:
            logger.info("Successfully connected to Elasticsearch at %s", self._url)
        else:
            logger.warning(
                "Could not connect to Elasticsearch at %s — search features "
                "will be unavailable",
                self._url,
            )
        return result

    def close(self) -> None:
        """Close the Elasticsearch connection and release resources.

        Sets the :attr:`is_connected` flag to ``False`` regardless of whether
        the underlying client's ``close()`` call succeeds.
        """
        try:
            self._client.close()
            logger.info("Elasticsearch connection closed for %s", self._url)
        except Exception as exc:  # pragma: no cover
            logger.warning("Error closing Elasticsearch connection: %s", exc)
        finally:
            self._connected = False

    def reconnect(self) -> bool:
        """Attempt to reconnect to Elasticsearch after a failure.

        Closes any existing connection, re-creates the underlying
        ``elasticsearch-py`` client, and verifies connectivity via
        :meth:`connect`.
        """
        logger.info("Attempting to reconnect to Elasticsearch at %s", self._url)

        # Close the existing connection gracefully.
        self.close()

        # Re-create the underlying client with the same configuration that was
        # used during initial construction.
        if "," in self._url:
            hosts: list[str] = [h.strip() for h in self._url.split(",") if h.strip()]
        else:
            hosts = [self._url]

        self._client = Elasticsearch(
            hosts=hosts,
            timeout=self._kwargs.get("timeout", DEFAULT_TIMEOUT),
            max_retries=self._kwargs.get("max_retries", DEFAULT_MAX_RETRIES),
            retry_on_timeout=self._kwargs.get(
                "retry_on_timeout", DEFAULT_RETRY_ON_TIMEOUT
            ),
            sniff_on_start=self._kwargs.get("sniff_on_start", False),
            sniff_on_connection_fail=self._kwargs.get(
                "sniff_on_connection_fail", False
            ),
        )

        return self.connect()


# ═══════════════════════════════════════════════════════════════════════════
# Module-level singleton management functions
# ═══════════════════════════════════════════════════════════════════════════

def init_elasticsearch(app: Any = None) -> ElasticsearchClient:
    """Initialise the module-level singleton :class:`ElasticsearchClient`.

    When *app* (a Flask application instance) is provided, configuration is
    read from ``app.config``.  Otherwise, the ``ELASTICSEARCH_URL``
    environment variable (or the default constant) is used — this supports the
    twelve-factor app methodology for environment-based configuration.

    This function is intended to be called from ``factory.py`` during Flask
    application creation.  Repeated calls replace the previous singleton
    instance.

    Parameters
    ----------
    app:
        Optional Flask application instance whose ``config`` dictionary
        supplies Elasticsearch connection parameters.

    Returns
    -------
    ElasticsearchClient
        The newly created (and connected) wrapper instance.
    """
    global _es_client, _is_initialized  # noqa: PLW0603

    if app is not None:
        url: str = app.config.get("ELASTICSEARCH_URL", DEFAULT_ELASTICSEARCH_URL)
        timeout: int = app.config.get("ELASTICSEARCH_TIMEOUT", DEFAULT_TIMEOUT)
        max_retries: int = app.config.get(
            "ELASTICSEARCH_MAX_RETRIES", DEFAULT_MAX_RETRIES
        )
        retry_on_timeout: bool = app.config.get(
            "ELASTICSEARCH_RETRY_ON_TIMEOUT", DEFAULT_RETRY_ON_TIMEOUT
        )
        sniff_on_start: bool = app.config.get(
            "ELASTICSEARCH_SNIFF_ON_START", False
        )
        sniff_on_connection_fail: bool = app.config.get(
            "ELASTICSEARCH_SNIFF_ON_FAIL", False
        )
    else:
        url = os.environ.get("ELASTICSEARCH_URL", DEFAULT_ELASTICSEARCH_URL)
        timeout = DEFAULT_TIMEOUT
        max_retries = DEFAULT_MAX_RETRIES
        retry_on_timeout = DEFAULT_RETRY_ON_TIMEOUT
        sniff_on_start = False
        sniff_on_connection_fail = False

    _es_client = ElasticsearchClient(
        url=url,
        timeout=timeout,
        max_retries=max_retries,
        retry_on_timeout=retry_on_timeout,
        sniff_on_start=sniff_on_start,
        sniff_on_connection_fail=sniff_on_connection_fail,
    )

    # Attempt to establish the connection — but do not raise if ES is down.
    _es_client.connect()

    _is_initialized = True
    logger.info("Elasticsearch singleton client initialised (connected=%s)", _es_client.is_connected)
    return _es_client


def get_es_client() -> Elasticsearch | None:
    """Return the raw ``elasticsearch-py`` client from the singleton, or
    ``None`` if the singleton has not been initialised or is not connected.

    This is the **primary interface** used by :mod:`index_manager` and
    :mod:`query_builder` to obtain the Elasticsearch connection.  Callers
    must handle the ``None`` return value to support graceful degradation.
    """
    if _es_client is not None and _es_client.is_connected:
        return _es_client.client
    return None


def get_es_wrapper() -> ElasticsearchClient | None:
    """Return the :class:`ElasticsearchClient` wrapper instance, or ``None``
    if the singleton has not been initialised.

    Use this when you need access to health checking or connection management
    methods rather than raw Elasticsearch operations.
    """
    return _es_client


def is_elasticsearch_available() -> bool:
    """Quick check whether Elasticsearch is currently available.

    Returns ``True`` only when the singleton client is both initialised **and**
    connected.
    """
    return _es_client is not None and _es_client.is_connected
