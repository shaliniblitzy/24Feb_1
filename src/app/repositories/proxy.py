"""
Proxy Repository Type Implementation.

This module implements the **Proxy** repository type for Feature F-102
(Repository Types — Proxy).  A proxy repository caches artifacts from a
remote upstream repository and serves them locally on subsequent requests,
reducing latency and network traffic while providing resilience when the
upstream is unavailable.

**Architecture Context:**

Replaces ``ProxyFacetSupport.java`` (proxy behaviour base class) and
``HttpClientFacetImpl.java`` (remote HTTP fetch support) from the original
Sonatype Nexus Repository Java source system (Section 5.2.4).

**Key Behaviours:**

- **Cache-through resolution**: Check local BlobStore → if expired, fetch
  from remote → cache fresh content → serve.
- **Resilience**: If the remote upstream is unreachable, serve stale cached
  content rather than returning an error.
- **Negative cache**: Paths known to not exist upstream are cached for a
  configurable TTL to avoid repeated 404 fetches.
- **Content max age**: Configurable per repository in minutes.  A value of
  ``-1`` means infinite cache (content never expires).
- **Metadata max age**: Separate, shorter TTL for metadata files.
- **Connection pooling**: HTTP client instances are cached per repository for
  connection reuse and efficiency.

**Performance Target:**

Cached artifact resolution must complete in **< 200 ms** (AAP Section 0.7.3).

**Event Integration:**

Emits :attr:`EventType.ASSET_DOWNLOADED` events after successful artifact
delivery for audit logging (F-303) and webhook dispatch (F-503).

**Exports:**

- :class:`ProxyRepository` — main proxy repository handler
- :class:`ProxyRepositoryError` — base exception
- :class:`RemoteFetchError` — remote fetch failure exception
- :class:`ProxyConfigurationError` — invalid configuration exception
- Constants: ``DEFAULT_CONTENT_MAX_AGE``, ``DEFAULT_METADATA_MAX_AGE``,
  ``DEFAULT_NEGATIVE_CACHE_TTL``, ``DEFAULT_CONNECT_TIMEOUT``,
  ``DEFAULT_READ_TIMEOUT``, ``DEFAULT_MAX_RETRIES``,
  ``DEFAULT_PROXY_ATTRIBUTES``
"""

from __future__ import annotations

import io
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO

from flask import current_app

from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType
from src.app.extensions import db
from src.app.models.asset import Asset
from src.app.models.component import Component
from src.app.models.repository import Repository
from src.app.repositories.facets import RepositoryFacet, StorageFacet
from src.app.repositories.lifecycle import RepositoryLifecycle, RepositoryState
from src.app.utils.helpers import compute_checksums, detect_mime_type
from src.app.utils.http_client import HttpClient

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 per-repository logging from the
# Java source.  Provides structured logging for proxy fetch attempts, cache
# hits / misses, remote failures, and negative cache operations.
# ---------------------------------------------------------------------------

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — Default Proxy Cache Timings (in minutes)
# ---------------------------------------------------------------------------
# These defaults replicate the values documented in the Java source's
# DefaultsCustomizer and ProxyFacetSupport configuration.
# ---------------------------------------------------------------------------

DEFAULT_CONTENT_MAX_AGE: int = 1440
"""How long cached content is considered fresh, in minutes (24 hours)."""

DEFAULT_METADATA_MAX_AGE: int = 1440
"""How long cached metadata is considered fresh, in minutes (24 hours)."""

DEFAULT_NEGATIVE_CACHE_TTL: int = 1440
"""How long known-missing paths remain in the negative cache, in minutes."""

# ---------------------------------------------------------------------------
# Constants — Connection Defaults (replaces DefaultsCustomizer from Java)
# ---------------------------------------------------------------------------

DEFAULT_CONNECT_TIMEOUT: int = 20
"""TCP connection timeout for upstream fetches, in seconds."""

DEFAULT_READ_TIMEOUT: int = 60
"""Socket read timeout for upstream fetches, in seconds."""

DEFAULT_MAX_RETRIES: int = 3
"""Maximum automatic retry attempts for idempotent HTTP requests."""

# ---------------------------------------------------------------------------
# Constants — Default Proxy Repository Attributes
# ---------------------------------------------------------------------------
# Template merged into ``repository.attributes`` when a proxy repository is
# created.  The ``remoteUrl`` MUST be overridden by the caller.
# ---------------------------------------------------------------------------

DEFAULT_PROXY_ATTRIBUTES: dict[str, Any] = {
    "proxy": {
        "remoteUrl": "",  # REQUIRED — upstream registry URL
        "contentMaxAge": DEFAULT_CONTENT_MAX_AGE,
        "metadataMaxAge": DEFAULT_METADATA_MAX_AGE,
    },
    "negativeCache": {
        "enabled": True,
        "timeToLive": DEFAULT_NEGATIVE_CACHE_TTL,
    },
    "httpClient": {
        "connection": {
            "timeout": DEFAULT_CONNECT_TIMEOUT,
            "retries": DEFAULT_MAX_RETRIES,
            "useTrustStore": True,
        },
        "authentication": None,
        # Optional: {"type": "basic", "username": "…", "password": "…"}
    },
}


# ===========================================================================
# Custom Exception Hierarchy
# ===========================================================================


class ProxyRepositoryError(Exception):
    """Base exception for all proxy repository operations.

    Attributes:
        message: Human-readable description of the error.
        repository_name: Name of the proxy repository that triggered the
            error, or ``None`` if unknown.
    """

    def __init__(
        self,
        message: str = "Proxy repository error",
        repository_name: str | None = None,
    ) -> None:
        self.message: str = message
        self.repository_name: str | None = repository_name
        super().__init__(self.message)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"repository_name={self.repository_name!r})"
        )


class RemoteFetchError(ProxyRepositoryError):
    """Raised when fetching content from the remote upstream fails.

    Captures the upstream URL, HTTP status code, and any additional error
    details for diagnostic purposes.

    Attributes:
        remote_url: The upstream URL that was requested.
        status_code: The HTTP status code returned, or ``None`` on
            connection-level failures.
        error_details: Additional error context (e.g. response body excerpt).
    """

    def __init__(
        self,
        message: str = "Remote fetch failed",
        repository_name: str | None = None,
        remote_url: str | None = None,
        status_code: int | None = None,
        error_details: str | None = None,
    ) -> None:
        self.remote_url: str | None = remote_url
        self.status_code: int | None = status_code
        self.error_details: str | None = error_details
        super().__init__(message=message, repository_name=repository_name)


class ProxyConfigurationError(ProxyRepositoryError):
    """Raised when the proxy repository configuration is invalid.

    Common causes:
    - Missing ``remoteUrl`` in ``proxy`` attributes.
    - Malformed ``httpClient.authentication`` block.
    """

    def __init__(
        self,
        message: str = "Invalid proxy configuration",
        repository_name: str | None = None,
    ) -> None:
        super().__init__(message=message, repository_name=repository_name)


# ===========================================================================
# ProxyRepository — Main Proxy Repository Handler
# ===========================================================================


class ProxyRepository:
    """Proxy repository type handler.

    Caches artifacts from remote upstream repositories with configurable
    caching policies, negative cache, and content max age.  Replaces
    ``ProxyFacetSupport.java`` and ``HttpClientFacetImpl.java`` from the
    original Java source system.

    The resolution strategy follows this precedence order:

    1. **Negative cache** — immediately returns ``None`` for paths known
       to be absent upstream (avoids repeated 404 fetches).
    2. **Local cache** — serves from the local BlobStore if the cached
       copy is still fresh (per ``contentMaxAge``).
    3. **Remote fetch** — fetches from the upstream when the cache is
       expired or absent, then caches locally for future requests.
    4. **Stale cache fallback** — if the remote is unreachable *and* a
       stale cached version exists, it is served as a resilience measure.

    Args:
        repository: The :class:`Repository` model instance (must have
            ``type == 'proxy'``).

    Raises:
        TypeError: If ``repository.type`` is not ``'proxy'``.
        ProxyConfigurationError: If the remote URL is not configured.

    Attributes:
        repository: Bound :class:`Repository` instance.
        name: Repository name (shortcut for ``repository.name``).
        logger: Per-repository logger.
    """

    def __init__(self, repository: Repository) -> None:
        # --- Type validation ------------------------------------------------
        if repository.type != "proxy":
            raise TypeError(
                f"ProxyRepository requires a repository with type='proxy', "
                f"got type='{repository.type}' for repository '{repository.name}'"
            )

        self.repository: Repository = repository
        self.name: str = repository.name
        self.logger: logging.Logger = logging.getLogger(
            f"{__name__}.{repository.name}"
        )

        # --- Lifecycle state machine ----------------------------------------
        self._lifecycle: RepositoryLifecycle = RepositoryLifecycle(repository)

        # --- Storage facet for BlobStore delegation -------------------------
        self._storage_facet: StorageFacet = StorageFacet(repository)
        self._storage_facet.attach()

        # --- Negative cache (in-memory, per-instance) -----------------------
        # Maps artifact path → UTC expiration datetime.
        self._negative_cache: dict[str, datetime] = {}

        # --- Cached HTTP client (lazy-initialized) --------------------------
        self._http_client: HttpClient | None = None

        # --- Validate remote URL is configured ------------------------------
        attrs = repository.attributes or {}
        remote_url = attrs.get("proxy", {}).get("remoteUrl", "")
        if not remote_url:
            self.logger.warning(
                "Proxy repository '%s' created without a remote URL. "
                "Artifacts cannot be fetched until a remote URL is configured.",
                self.name,
            )

        self.logger.debug(
            "ProxyRepository initialised for '%s' (remote=%s)",
            self.name,
            remote_url or "<not configured>",
        )

    # ===================================================================
    # Content Resolution — Main Proxy Fetch
    # ===================================================================

    def get_artifact(
        self, path: str
    ) -> tuple[BinaryIO | None, str | None, int | None]:
        """Resolve and return a cached or freshly fetched artifact.

        This is the **primary entry point** for proxy artifact resolution.
        The resolution algorithm mirrors ``ProxyFacetSupport.java``'s
        ``resolve()`` method from the Java source.

        Args:
            path: Artifact path relative to the repository root
                (e.g. ``'/org/example/lib/1.0/lib-1.0.jar'``).

        Returns:
            A 3-tuple of ``(content_stream, content_type, content_length)``
            on success, or ``(None, None, None)`` if the artifact cannot
            be resolved.

        Raises:
            InvalidStateError: If the repository is not in ``STARTED`` state.
            ProxyRepositoryError: If the repository is offline.
        """
        start_time = time.time()

        # Step 1 — Lifecycle and online checks
        self._lifecycle.ensure_started()

        if not self.repository.online:
            self.logger.warning(
                "Proxy repository '%s' is offline — cannot serve artifacts.",
                self.name,
            )
            raise ProxyRepositoryError(
                message=f"Proxy repository '{self.name}' is offline",
                repository_name=self.name,
            )

        # Step 2 — Negative cache check
        if self._is_in_negative_cache(path):
            self.logger.debug(
                "Negative cache hit for '%s' in repository '%s'",
                path,
                self.name,
            )
            return None, None, None

        # Step 3 — Local cache lookup
        cached_asset, is_fresh = self._check_local_cache(path)

        # Step 4 — Serve fresh cached content
        if cached_asset is not None and is_fresh:
            self.logger.debug(
                "Cache hit (fresh) for '%s' in repository '%s'",
                path,
                self.name,
            )
            content_stream = self._get_cached_content(cached_asset)
            if content_stream is not None:
                # Update last_downloaded timestamp
                self._update_last_downloaded(cached_asset)
                self._emit_download_event(path, cached_asset)
                elapsed_ms = (time.time() - start_time) * 1000
                self.logger.debug(
                    "Served cached artifact '%s' from '%s' in %.1f ms",
                    path,
                    self.name,
                    elapsed_ms,
                )
                return (
                    content_stream,
                    cached_asset.content_type,
                    cached_asset.size,
                )

        # Step 5 — Remote fetch (cache miss or expired)
        self.logger.debug(
            "Cache %s for '%s' in repository '%s' — attempting remote fetch",
            "expired" if cached_asset else "miss",
            path,
            self.name,
        )

        content_bytes, content_type, content_length, headers = (
            self._fetch_from_remote(path)
        )

        if content_bytes is not None:
            # Step 5a — Remote fetch succeeded — cache and serve
            self.logger.debug(
                "Remote fetch succeeded for '%s' (%d bytes)",
                path,
                len(content_bytes),
            )
            try:
                asset = self._cache_content(
                    path, content_bytes, content_type, headers
                )
                self._emit_download_event(path, asset)
            except Exception as exc:
                self.logger.error(
                    "Failed to cache content for '%s': %s", path, exc
                )
                # Still serve the freshly fetched content even if caching fails
                return (
                    io.BytesIO(content_bytes),
                    content_type,
                    len(content_bytes),
                )

            elapsed_ms = (time.time() - start_time) * 1000
            self.logger.debug(
                "Served fresh artifact '%s' from remote in %.1f ms",
                path,
                elapsed_ms,
            )
            return io.BytesIO(content_bytes), content_type, len(content_bytes)

        # Step 5c — Remote fetch failed but stale cache exists (resilience)
        if cached_asset is not None:
            self.logger.info(
                "Remote fetch failed for '%s' — serving stale cached version "
                "from repository '%s' (resilience fallback)",
                path,
                self.name,
            )
            content_stream = self._get_cached_content(cached_asset)
            if content_stream is not None:
                self._update_last_downloaded(cached_asset)
                self._emit_download_event(path, cached_asset)
                return (
                    content_stream,
                    cached_asset.content_type,
                    cached_asset.size,
                )

        # Step 5d — Remote fetch failed, no cache — add to negative cache
        self.logger.debug(
            "Artifact '%s' not available from remote or cache in '%s'",
            path,
            self.name,
        )
        self._add_to_negative_cache(path)
        return None, None, None

    # ===================================================================
    # Local Cache Inspection
    # ===================================================================

    def _check_local_cache(self, path: str) -> tuple[Asset | None, bool]:
        """Check whether the artifact exists in the local cache.

        Queries the ``Asset`` table for a matching record in this repository
        and determines whether the cached version is still **fresh** based
        on the repository's ``contentMaxAge`` configuration.

        Args:
            path: Artifact path to look up.

        Returns:
            A 2-tuple ``(asset_or_none, is_fresh)``.  ``is_fresh`` is only
            meaningful when ``asset_or_none`` is not ``None``.
        """
        asset: Asset | None = Asset.query.filter_by(
            repository_name=self.name, path=path
        ).first()

        if asset is None:
            return None, False

        # Determine freshness based on contentMaxAge
        proxy_config: dict = (self.repository.attributes or {}).get("proxy", {})
        max_age_minutes: int = proxy_config.get(
            "contentMaxAge", DEFAULT_CONTENT_MAX_AGE
        )

        # -1 means infinite cache — content never expires
        if max_age_minutes < 0:
            return asset, True

        # Compute the cutoff time
        cutoff: datetime = datetime.now(timezone.utc) - timedelta(
            minutes=max_age_minutes
        )

        # Use updated_at as the cache timestamp
        asset_updated: datetime | None = asset.updated_at
        if asset_updated is None:
            # Fallback: treat as stale if no timestamp
            return asset, False

        # Ensure timezone-aware comparison
        if asset_updated.tzinfo is None:
            asset_updated = asset_updated.replace(tzinfo=timezone.utc)

        is_fresh: bool = asset_updated > cutoff
        return asset, is_fresh

    # ===================================================================
    # Remote Fetch
    # ===================================================================

    def _fetch_from_remote(
        self, path: str
    ) -> tuple[bytes | None, str | None, int | None, dict | None]:
        """Fetch an artifact from the remote upstream repository.

        Constructs the full remote URL and issues an HTTP GET request via
        the configured :class:`HttpClient`.

        Args:
            path: Artifact path (appended to the remote base URL).

        Returns:
            A 4-tuple ``(content_bytes, content_type, content_length,
            response_headers)`` on success, or ``(None, None, None, None)``
            on failure.
        """
        try:
            remote_url = self._get_remote_url()
        except ProxyConfigurationError:
            self.logger.error(
                "Cannot fetch from remote — no remote URL configured for '%s'",
                self.name,
            )
            return None, None, None, None

        # Build the full URL for the artifact
        full_url = f"{remote_url.rstrip('/')}/{path.lstrip('/')}"

        try:
            client = self._get_http_client()
            response = client.get(full_url)
        except Exception as exc:
            self.logger.warning(
                "Remote fetch failed for '%s' from '%s': %s",
                path,
                full_url,
                exc,
            )
            return None, None, None, None

        status_code: int = response.status_code

        if status_code == 200:
            content_bytes: bytes = response.content
            content_type: str | None = response.headers.get("Content-Type")
            content_length: int = len(content_bytes)
            response_headers: dict = dict(response.headers)
            self.logger.debug(
                "Remote fetch OK for '%s' (%d bytes, type=%s)",
                path,
                content_length,
                content_type,
            )
            return content_bytes, content_type, content_length, response_headers

        if status_code == 404:
            self.logger.debug(
                "Remote returned 404 for '%s' from '%s'",
                path,
                full_url,
            )
            return None, None, None, None

        if status_code in (401, 403):
            self.logger.warning(
                "Authentication failure (HTTP %d) fetching '%s' from '%s'. "
                "Check proxy authentication configuration.",
                status_code,
                path,
                full_url,
            )
            return None, None, None, None

        if status_code >= 500:
            self.logger.warning(
                "Remote server error (HTTP %d) fetching '%s' from '%s'",
                status_code,
                path,
                full_url,
            )
            return None, None, None, None

        # Unexpected status code
        self.logger.warning(
            "Unexpected HTTP %d fetching '%s' from '%s'",
            status_code,
            path,
            full_url,
        )
        return None, None, None, None

    def _get_http_client(self) -> Any:
        """Create or retrieve the cached HTTP client for this repository.

        The client is configured from ``repository.attributes['httpClient']``
        and cached on the instance for connection pooling.

        Returns:
            A configured :class:`HttpClient` instance.
        """
        if self._http_client is not None:
            return self._http_client

        attrs: dict = self.repository.attributes or {}
        http_config: dict = attrs.get("httpClient", {})
        conn_config: dict = http_config.get("connection", {})
        auth_config: dict | None = http_config.get("authentication")

        connect_timeout: int = conn_config.get(
            "timeout", DEFAULT_CONNECT_TIMEOUT
        )
        max_retries: int = conn_config.get("retries", DEFAULT_MAX_RETRIES)
        use_trust_store: bool = conn_config.get("useTrustStore", True)

        # Resolve read timeout from app config or use default
        try:
            app_read_timeout = current_app.config.get(
                "PROXY_READ_TIMEOUT", DEFAULT_READ_TIMEOUT
            )
        except RuntimeError:
            # Outside Flask app context
            app_read_timeout = DEFAULT_READ_TIMEOUT

        client = HttpClient(
            connect_timeout=float(connect_timeout),
            read_timeout=float(app_read_timeout),
            max_retries=max_retries,
            verify_ssl=use_trust_store,
        )

        # Configure basic authentication if specified
        if auth_config and isinstance(auth_config, dict):
            auth_type = auth_config.get("type", "")
            if auth_type == "basic":
                username = auth_config.get("username", "")
                password = auth_config.get("password", "")
                if username:
                    client.set_basic_auth(username, password)
                    self.logger.debug(
                        "HTTP client for '%s' configured with basic auth "
                        "(user=%s)",
                        self.name,
                        username,
                    )

        self._http_client = client
        self.logger.debug(
            "HTTP client created for proxy repository '%s' "
            "(timeout=%ds, retries=%d, ssl_verify=%s)",
            self.name,
            connect_timeout,
            max_retries,
            use_trust_store,
        )
        return client

    def _get_remote_url(self) -> str:
        """Extract and validate the remote URL from repository attributes.

        Returns:
            The configured upstream remote URL.

        Raises:
            ProxyConfigurationError: If the remote URL is not configured or
                is empty.
        """
        attrs: dict = self.repository.attributes or {}
        remote_url: str = attrs.get("proxy", {}).get("remoteUrl", "")
        if not remote_url or not remote_url.strip():
            raise ProxyConfigurationError(
                message=(
                    f"Proxy repository '{self.name}' has no remote URL "
                    f"configured in attributes.proxy.remoteUrl"
                ),
                repository_name=self.name,
            )
        return remote_url.strip()

    # ===================================================================
    # Cache Storage
    # ===================================================================

    def _cache_content(
        self,
        path: str,
        content: bytes,
        content_type: str | None,
        headers: dict | None = None,
    ) -> Asset:
        """Store fetched content in the local BlobStore and database.

        Creates or updates an :class:`Asset` record and persists the binary
        content via the :class:`StorageFacet`.

        Args:
            path: Artifact path within the repository.
            content: Raw binary content from the remote upstream.
            content_type: MIME type reported by the remote server, or
                ``None`` if unavailable.
            headers: Optional dict of response headers (for ETag /
                Last-Modified tracking).

        Returns:
            The created or updated :class:`Asset` record.
        """
        # Compute checksums for integrity verification
        checksums: dict[str, str] = compute_checksums(content)

        # Resolve content type — fall back to MIME detection
        resolved_type: str = content_type or detect_mime_type(path)

        # Store content blob via StorageFacet
        content_stream: io.BytesIO = io.BytesIO(content)
        try:
            blob_ref: str = self._storage_facet.store_content(
                path=path,
                content=content_stream,
                content_type=resolved_type,
                size=len(content),
                checksums=checksums,
            )
        except Exception as exc:
            self.logger.error(
                "BlobStore write failed for '%s' in repository '%s': %s",
                path,
                self.name,
                exc,
            )
            raise

        now: datetime = datetime.now(timezone.utc)

        # Create or update Asset record
        asset: Asset | None = Asset.query.filter_by(
            repository_name=self.name, path=path
        ).first()

        if asset is not None:
            # Update existing cached asset
            asset.checksum_sha1 = checksums.get("sha1")
            asset.checksum_sha256 = checksums.get("sha256")
            asset.checksum_md5 = checksums.get("md5")
            asset.size = len(content)
            asset.content_type = resolved_type
            asset.last_downloaded = now
            asset.blob_ref = blob_ref
            db.session.add(asset)
        else:
            # Create new cached asset
            asset = Asset(
                repository_name=self.name,
                path=path,
                content_type=resolved_type,
                checksum_sha1=checksums.get("sha1"),
                checksum_sha256=checksums.get("sha256"),
                checksum_md5=checksums.get("md5"),
                size=len(content),
                last_downloaded=now,
                blob_ref=blob_ref,
            )
            db.session.add(asset)

        db.session.commit()

        self.logger.debug(
            "Cached content for '%s' in repository '%s' "
            "(size=%d, sha1=%s, blob_ref=%s)",
            path,
            self.name,
            len(content),
            checksums.get("sha1", "")[:12] + "…",
            blob_ref,
        )
        return asset

    # ===================================================================
    # Negative Cache
    # ===================================================================

    def _is_in_negative_cache(self, path: str) -> bool:
        """Check whether an artifact path is in the negative cache.

        The negative cache prevents repeated 404 fetches for paths that
        are known to not exist on the upstream.

        Args:
            path: Artifact path to check.

        Returns:
            ``True`` if the path is in the negative cache and the TTL
            has not expired; ``False`` otherwise.
        """
        neg_config: dict = (self.repository.attributes or {}).get(
            "negativeCache", {}
        )

        # Negative cache disabled → always return False
        if not neg_config.get("enabled", True):
            return False

        if path in self._negative_cache:
            expiry: datetime = self._negative_cache[path]
            if datetime.now(timezone.utc) < expiry:
                return True
            else:
                # Expired entry — remove it
                del self._negative_cache[path]

        return False

    def _add_to_negative_cache(self, path: str) -> None:
        """Add an artifact path to the negative cache.

        The entry will expire after the configured ``timeToLive`` (in
        minutes) from ``negativeCache`` repository attributes.

        Args:
            path: Artifact path to cache as missing.
        """
        neg_config: dict = (self.repository.attributes or {}).get(
            "negativeCache", {}
        )

        # Only add if negative cache is enabled
        if not neg_config.get("enabled", True):
            return

        ttl_minutes: int = neg_config.get(
            "timeToLive", DEFAULT_NEGATIVE_CACHE_TTL
        )
        expiry: datetime = datetime.now(timezone.utc) + timedelta(
            minutes=ttl_minutes
        )
        self._negative_cache[path] = expiry

        self.logger.debug(
            "Added '%s' to negative cache for repository '%s' "
            "(expires in %d minutes)",
            path,
            self.name,
            ttl_minutes,
        )

    def invalidate_negative_cache(self, path: str | None = None) -> None:
        """Invalidate negative cache entries.

        Args:
            path: If specified, only this path is removed from the negative
                cache.  If ``None``, the entire negative cache for this
                repository is cleared.
        """
        if path is not None:
            removed = self._negative_cache.pop(path, None)
            if removed is not None:
                self.logger.debug(
                    "Invalidated negative cache entry for '%s' in "
                    "repository '%s'",
                    path,
                    self.name,
                )
        else:
            count = len(self._negative_cache)
            self._negative_cache.clear()
            self.logger.info(
                "Cleared entire negative cache for repository '%s' "
                "(%d entries removed)",
                self.name,
                count,
            )

    # ===================================================================
    # Metadata Operations
    # ===================================================================

    def get_metadata(self, metadata_path: str) -> dict | None:
        """Fetch format-specific metadata from the remote upstream.

        Metadata files (e.g. Maven ``metadata.xml``, npm ``package.json``)
        use the **metadataMaxAge** configuration, which typically has a
        shorter TTL than regular content.

        Args:
            metadata_path: Path to the metadata file within the repository.

        Returns:
            A dictionary with ``content`` (bytes), ``content_type`` (str),
            and ``path`` (str) keys, or ``None`` if the metadata is
            unavailable.
        """
        # Step 1 — Check if repository is in a valid state
        if not self._lifecycle.is_started:
            self.logger.warning(
                "Cannot fetch metadata — repository '%s' is not started",
                self.name,
            )
            return None

        # Step 2 — Check local cache with metadata-specific max age
        asset: Asset | None = Asset.query.filter_by(
            repository_name=self.name, path=metadata_path
        ).first()

        if asset is not None:
            proxy_config: dict = (self.repository.attributes or {}).get(
                "proxy", {}
            )
            metadata_max_age: int = proxy_config.get(
                "metadataMaxAge", DEFAULT_METADATA_MAX_AGE
            )

            is_fresh = False
            if metadata_max_age < 0:
                is_fresh = True
            elif asset.updated_at is not None:
                asset_updated = asset.updated_at
                if asset_updated.tzinfo is None:
                    asset_updated = asset_updated.replace(tzinfo=timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(
                    minutes=metadata_max_age
                )
                is_fresh = asset_updated > cutoff

            if is_fresh:
                content_stream = self._get_cached_content(asset)
                if content_stream is not None:
                    content_bytes = content_stream.read()
                    return {
                        "content": content_bytes,
                        "content_type": asset.content_type,
                        "path": metadata_path,
                    }

        # Step 3 — Fetch from remote
        content_bytes, content_type, content_length, headers = (
            self._fetch_from_remote(metadata_path)
        )

        if content_bytes is not None:
            try:
                self._cache_content(
                    metadata_path, content_bytes, content_type, headers
                )
            except Exception as exc:
                self.logger.warning(
                    "Failed to cache metadata '%s': %s", metadata_path, exc
                )

            return {
                "content": content_bytes,
                "content_type": content_type,
                "path": metadata_path,
            }

        return None

    def check_remote_health(self) -> dict:
        """Check whether the remote upstream repository is accessible.

        Sends an HTTP HEAD request to the remote base URL and measures
        round-trip latency.

        Returns:
            A dictionary with keys ``available`` (bool), ``latency_ms``
            (int), and ``status_code`` (int or ``None``).
        """
        try:
            remote_url = self._get_remote_url()
        except ProxyConfigurationError:
            return {
                "available": False,
                "latency_ms": 0,
                "status_code": None,
            }

        start_time = time.time()
        try:
            client = self._get_http_client()
            response = client.head(remote_url)
            latency_ms = int((time.time() - start_time) * 1000)
            available = response.status_code < 400

            self.logger.debug(
                "Remote health check for '%s': status=%d, latency=%dms, "
                "available=%s",
                self.name,
                response.status_code,
                latency_ms,
                available,
            )

            return {
                "available": available,
                "latency_ms": latency_ms,
                "status_code": response.status_code,
            }
        except Exception as exc:
            latency_ms = int((time.time() - start_time) * 1000)
            self.logger.warning(
                "Remote health check failed for '%s': %s (latency=%dms)",
                self.name,
                exc,
                latency_ms,
            )
            return {
                "available": False,
                "latency_ms": latency_ms,
                "status_code": None,
            }

    # ===================================================================
    # Repository Status and Configuration
    # ===================================================================

    def get_status(self) -> dict:
        """Return a comprehensive status snapshot of this proxy repository.

        Returns:
            A dictionary containing repository metadata, lifecycle state,
            remote URL, cache configuration, and asset statistics.
        """
        attrs: dict = self.repository.attributes or {}
        proxy_config: dict = attrs.get("proxy", {})
        neg_config: dict = attrs.get("negativeCache", {})

        try:
            remote_url = self._get_remote_url()
        except ProxyConfigurationError:
            remote_url = "<not configured>"

        return {
            "name": self.name,
            "type": "proxy",
            "format": self.repository.format,
            "online": self.repository.online,
            "state": self._lifecycle.current_state.value,
            "remote_url": remote_url,
            "cache_config": {
                "content_max_age": proxy_config.get(
                    "contentMaxAge", DEFAULT_CONTENT_MAX_AGE
                ),
                "metadata_max_age": proxy_config.get(
                    "metadataMaxAge", DEFAULT_METADATA_MAX_AGE
                ),
                "negative_cache_enabled": neg_config.get("enabled", True),
                "negative_cache_ttl": neg_config.get(
                    "timeToLive", DEFAULT_NEGATIVE_CACHE_TTL
                ),
            },
            "negative_cache_size": len(self._negative_cache),
            "cached_asset_count": Asset.query.filter_by(
                repository_name=self.name
            ).count(),
        }

    def update_configuration(self, attributes: dict) -> None:
        """Update proxy-specific configuration attributes.

        Performs a deep merge of the supplied ``attributes`` into the
        existing repository attributes.  If cache-related settings change,
        the negative cache is invalidated.

        Args:
            attributes: Dictionary of attribute updates to apply.

        Raises:
            ProxyConfigurationError: If a supplied ``remoteUrl`` is invalid.
        """
        # Validate remote URL if provided
        new_proxy = attributes.get("proxy", {})
        new_remote_url = new_proxy.get("remoteUrl")
        if new_remote_url is not None:
            if not isinstance(new_remote_url, str) or not new_remote_url.strip():
                raise ProxyConfigurationError(
                    message=(
                        f"Invalid remote URL for proxy repository '{self.name}'"
                    ),
                    repository_name=self.name,
                )

        # Deep merge attributes — use JSON round-trip for a true deep copy
        # to ensure SQLAlchemy detects the change on the JSON column
        import json as _json

        existing_attrs: dict = _json.loads(
            _json.dumps(self.repository.attributes or {})
        )
        _deep_merge(existing_attrs, attributes)

        # Persist to database
        self.repository.attributes = existing_attrs
        db.session.add(self.repository)
        db.session.commit()

        # Invalidate negative cache if cache settings changed
        cache_keys = {"contentMaxAge", "metadataMaxAge", "timeToLive", "enabled"}
        changed_keys = set()
        _collect_keys(attributes, changed_keys)
        if cache_keys & changed_keys:
            self.invalidate_negative_cache()
            self.logger.info(
                "Negative cache cleared for '%s' due to configuration change",
                self.name,
            )

        # Reset HTTP client if httpClient config changed
        if "httpClient" in attributes:
            if self._http_client is not None:
                try:
                    self._http_client.close()
                except Exception:
                    pass
                self._http_client = None
                self.logger.debug(
                    "HTTP client reset for '%s' due to configuration change",
                    self.name,
                )

        self.logger.info(
            "Configuration updated for proxy repository '%s'", self.name
        )

    # ===================================================================
    # Cleanup
    # ===================================================================

    def close(self) -> None:
        """Release all resources held by this proxy repository handler.

        Closes the HTTP client connection pool, clears the negative cache,
        and detaches the storage facet.  Called during repository stop or
        delete operations.
        """
        # Close HTTP client
        if self._http_client is not None:
            try:
                self._http_client.close()
            except Exception as exc:
                self.logger.warning(
                    "Error closing HTTP client for '%s': %s", self.name, exc
                )
            self._http_client = None

        # Clear negative cache
        self._negative_cache.clear()

        # Detach storage facet
        try:
            self._storage_facet.detach()
        except Exception as exc:
            self.logger.warning(
                "Error detaching storage facet for '%s': %s", self.name, exc
            )

        self.logger.info("Proxy repository '%s' closed.", self.name)

    # ===================================================================
    # Private Helpers
    # ===================================================================

    def _get_cached_content(self, asset: Asset) -> BinaryIO | None:
        """Retrieve the binary content stream for a cached asset.

        Uses the :class:`StorageFacet` to read from the underlying
        BlobStore.  Returns ``None`` if the blob reference is missing or
        the content cannot be read.

        Args:
            asset: The :class:`Asset` whose content should be retrieved.

        Returns:
            A readable binary stream, or ``None``.
        """
        blob_ref: str | None = asset.blob_ref
        if not blob_ref:
            self.logger.warning(
                "Asset '%s' in repository '%s' has no blob_ref — cannot "
                "retrieve content",
                asset.path,
                self.name,
            )
            return None

        try:
            stream, metadata = self._storage_facet.get_content(blob_ref)
            return stream
        except Exception as exc:
            self.logger.error(
                "Error reading blob '%s' for asset '%s' in repository '%s': "
                "%s",
                blob_ref,
                asset.path,
                self.name,
                exc,
            )
            return None

    def _update_last_downloaded(self, asset: Asset) -> None:
        """Update the ``last_downloaded`` timestamp on a cached asset.

        This is important for cleanup policy (F-204) evaluation — assets
        with recent download timestamps are retained.

        Args:
            asset: The :class:`Asset` to update.
        """
        try:
            asset.last_downloaded = datetime.now(timezone.utc)
            db.session.add(asset)
            db.session.commit()
        except Exception as exc:
            self.logger.warning(
                "Failed to update last_downloaded for asset '%s': %s",
                asset.path,
                exc,
            )

    def _emit_download_event(self, path: str, asset: Asset) -> None:
        """Emit an ASSET_DOWNLOADED event for audit logging.

        Args:
            path: The artifact path that was served.
            asset: The :class:`Asset` that was served.
        """
        try:
            emit_event(
                EventType.ASSET_DOWNLOADED,
                payload={
                    "repository_name": self.name,
                    "asset_path": path,
                    "content_type": asset.content_type,
                    "size": asset.size,
                },
            )
        except Exception as exc:
            # Event emission failures must never break artifact delivery
            self.logger.warning(
                "Failed to emit ASSET_DOWNLOADED event for '%s': %s",
                path,
                exc,
            )


# ===========================================================================
# Module-Level Utility Functions
# ===========================================================================


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (in-place).

    Nested dictionaries are merged; all other values are overwritten.

    Args:
        base: The base dictionary to merge into.
        override: The dictionary whose values take precedence.

    Returns:
        The mutated *base* dictionary.
    """
    for key, value in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(value, dict)
        ):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _collect_keys(d: dict, keys: set) -> None:
    """Recursively collect all keys from a nested dictionary.

    Args:
        d: The dictionary to traverse.
        keys: The set to which discovered keys are added.
    """
    for key, value in d.items():
        keys.add(key)
        if isinstance(value, dict):
            _collect_keys(value, keys)
