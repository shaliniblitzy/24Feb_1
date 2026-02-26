"""
Proxy Repository Remote Fetch Service.

This module implements the proxy repository content resolution strategy —
fetching artifacts from remote upstream repositories, caching them locally
in the configured BlobStore, and serving cached content on subsequent requests.

**Replaces** ``HttpClientFacetImpl.java`` and ``ProxyFacetSupport.java`` from
the original Sonatype Nexus Repository Java source system.

**Feature Coverage:**

- **F-102** (Repository Types — Proxy): Full proxy repository resolution with
  local caching, negative cache, and stale-on-error resilience.
- **F-201** / **F-202** (BlobStore): Cached artifacts stored via pluggable
  BlobStore abstraction (File or S3).
- **F-303** (Audit Logging): ASSET_DOWNLOADED events emitted on artifact access.

**Resolution Algorithm:**

1. Check local cache (Asset table) for a previously cached version.
2. If cached **and** fresh (``content_max_age`` check): serve from BlobStore.
3. If cached but expired **or** not cached:

   a. Attempt remote fetch from the upstream URL.
   b. If remote fetch succeeds: cache locally and serve fresh content.
   c. If remote fetch fails **and** cached version exists: serve stale cached
      content (resilience — stale-on-error).
   d. If remote fetch fails **and** no cached version: check negative cache,
      return ``None``.

**Performance Targets (AAP Section 0.7.3):**

- Cached artifact resolution: **< 200 ms**

**Caching Configuration (per-repository, in ``attributes.proxy``):**

- ``contentMaxAge``: Minutes before cached content is re-validated
  (``-1`` = infinite cache; default 1440 = 24 h).
- ``metadataMaxAge``: Minutes before cached metadata is re-validated
  (default 1440 = 24 h).

**Negative Cache (per-repository, in ``attributes.negativeCache``):**

- ``enabled``: Whether negative caching is active (default ``True``).
- ``timeToLive``: Minutes before a negative cache entry expires
  (default 1440 = 24 h).

**Connection Pooling:**

HTTP clients are created per-repository and cached in memory for connection
reuse, matching the Java ``HttpClientFacetImpl`` per-repository lifecycle.
"""

from __future__ import annotations

import dataclasses
import io
import ipaddress
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO, Generator
from urllib.parse import urlparse

import defusedxml.ElementTree as ElementTree  # Secure XML parsing — prevents XXE (CWE-611)

from flask import current_app

from src.app.extensions import db
from src.app.models.repository import Repository
from src.app.models.component import Component
from src.app.models.asset import Asset
from src.app.models.blobstore_config import BlobStoreConfig
from src.app.utils.http_client import HttpClient, create_http_client
from src.app.utils.helpers import compute_checksums, detect_mime_type, generate_uuid
from src.app.events.event_bus import emit_event
from src.app.events.event_types import EventType, AssetEventPayload
from src.app.storage import create_blobstore_from_model

# ---------------------------------------------------------------------------
# Module-Level Logger
# ---------------------------------------------------------------------------
# Replaces SLF4J 1.7.36 + Logback 1.2.13 structured logging from the Java
# source.  Provides DEBUG-level tracing for proxy cache resolution, remote
# fetch operations, negative cache management, and HTTP client lifecycle.
# ---------------------------------------------------------------------------
logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — Default Configuration Values
# ---------------------------------------------------------------------------

# Default cache timings (in minutes)
DEFAULT_CONTENT_MAX_AGE: int = 1440
"""Content cache lifetime in minutes.  -1 = infinite.  Default: 24 hours."""

DEFAULT_METADATA_MAX_AGE: int = 1440
"""Metadata cache lifetime in minutes.  -1 = infinite.  Default: 24 hours."""

DEFAULT_NEGATIVE_CACHE_TTL: int = 1440
"""Negative cache entry lifetime in minutes.  Default: 24 hours."""

# HTTP timeout defaults (in seconds)
DEFAULT_CONNECT_TIMEOUT: int = 20
"""TCP connection timeout for remote upstream (seconds)."""

DEFAULT_READ_TIMEOUT: int = 60
"""Socket read timeout for remote upstream (seconds)."""

DEFAULT_RETRIES: int = 3
"""Maximum number of automatic retries for idempotent requests."""


# ===========================================================================
# ProxyService Class
# ===========================================================================


class ProxyService:
    """Proxy repository content resolution service.

    Handles the full lifecycle of proxy content resolution:

    - Fetching artifacts from remote upstream repositories
    - Caching content locally in the configured BlobStore
    - Serving cached content on subsequent requests with freshness checks
    - Negative caching for paths known to not exist upstream
    - HTTP client connection pooling per repository
    - Health checking of remote upstream availability
    - Metadata fetching with separate cache TTL

    Thread Safety:
        The ``_negative_cache`` and ``_clients`` dictionaries are accessed
        in the context of Flask request threads.  For single-worker Gunicorn
        deployments this is safe; for multi-worker deployments each worker
        process has its own ``ProxyService`` instance with independent caches.
    """

    def __init__(self) -> None:
        """Initialise the proxy service.

        Creates empty caches for HTTP clients and negative cache entries.
        No external resources are acquired during initialisation — clients
        and BlobStore references are created lazily on first use.
        """
        self.logger: logging.Logger = logging.getLogger(__name__)
        self._clients: dict[str, HttpClient] = {}
        self._negative_cache: dict[str, datetime] = {}
        self.logger.debug("ProxyService initialised.")

    # ===================================================================
    # SSRF URL Validation (CWE-918)
    # ===================================================================

    @staticmethod
    def _validate_upstream_url(url: str) -> tuple[bool, str]:
        """Validate an upstream repository URL to mitigate SSRF attacks.

        Checks that the URL does not target private, reserved, or link-local
        IP address ranges (RFC 1918, RFC 6598, loopback, link-local) or
        well-known cloud metadata endpoints.  While upstream URLs are
        configured by administrators, this defense-in-depth measure protects
        against compromised admin accounts exfiltrating infrastructure data.

        Args:
            url: The upstream repository URL to validate.

        Returns:
            A ``(is_safe, reason)`` tuple.  ``is_safe`` is ``True`` if the URL
            is acceptable; ``False`` with a human-readable ``reason`` otherwise.
        """
        if not url or not url.strip():
            return (False, "URL is empty")

        try:
            parsed = urlparse(url)
        except ValueError:
            return (False, "URL is malformed")

        # Only allow HTTP and HTTPS schemes
        if parsed.scheme not in ("http", "https"):
            return (False, f"Unsupported URL scheme: {parsed.scheme}")

        hostname: str = parsed.hostname or ""
        if not hostname:
            return (False, "URL has no hostname")

        # Block well-known cloud metadata endpoints regardless of resolution
        _BLOCKED_HOSTNAMES: set[str] = {
            "metadata.google.internal",
            "metadata.goog",
        }
        if hostname.lower() in _BLOCKED_HOSTNAMES:
            return (False, f"Blocked cloud metadata hostname: {hostname}")

        # Attempt to parse as an IP address and check for private/reserved
        try:
            addr = ipaddress.ip_address(hostname)
            if addr.is_private:
                return (False, f"Private IP address not allowed: {hostname}")
            if addr.is_reserved:
                return (False, f"Reserved IP address not allowed: {hostname}")
            if addr.is_loopback:
                return (False, f"Loopback address not allowed: {hostname}")
            if addr.is_link_local:
                return (False, f"Link-local address not allowed: {hostname}")
            # Block AWS/Azure/GCP metadata IP (169.254.169.254)
            if str(addr) == "169.254.169.254":
                return (False, "Cloud metadata endpoint not allowed")
        except ValueError:
            # Hostname is not an IP literal — that's fine, it's a DNS name.
            # DNS rebinding is out of scope for this validation layer;
            # network-level controls should be used for full SSRF protection.
            pass

        return (True, "")

    # ===================================================================
    # Repository Lookup Helper
    # ===================================================================

    def _get_repository(self, repository_name: str) -> Repository | None:
        """Look up and validate a proxy repository by name.

        The repository must exist, be of type ``proxy``, and be ``online``.

        Args:
            repository_name: The unique repository identifier.

        Returns:
            The :class:`Repository` instance, or ``None`` if the repository
            does not exist, is not a proxy, or is offline.
        """
        repository: Repository | None = db.session.get(Repository, repository_name)
        if repository is None:
            self.logger.warning(
                "Repository '%s' not found.",
                repository_name,
            )
            return None
        if not repository.is_proxy:
            self.logger.warning(
                "Repository '%s' is not a proxy repository (type=%s).",
                repository_name,
                repository.type,
            )
            return None
        if not repository.online:
            self.logger.info(
                "Repository '%s' is offline — request rejected.",
                repository_name,
            )
            return None
        return repository

    # ===================================================================
    # Main Proxy Fetch — Public API
    # ===================================================================

    def fetch_artifact(
        self,
        repository_name: str,
        artifact_path: str,
    ) -> tuple[BinaryIO | None, str | None, int | None]:
        """Main entry point for proxy artifact resolution.

        Implements the full cache-first resolution strategy with stale-on-error
        resilience (AAP Section 0.7.1).

        Resolution algorithm:

        1. Look up the proxy repository.
        2. Check local cache (Asset table) for a cached version.
        3. If cached **and** fresh: serve directly from BlobStore.
        4. If not cached: check negative cache — return ``None`` if active.
        5. Attempt remote fetch from the upstream URL.
        6. If remote succeeds: cache locally and serve fresh content.
        7. If remote fails and stale cache exists: serve stale (resilience).
        8. If remote fails and no cache: return ``None``.

        Args:
            repository_name: Name of the proxy repository.
            artifact_path: Path of the artifact within the repository
                (e.g., ``org/example/lib/1.0/lib-1.0.jar``).

        Returns:
            Tuple of ``(content_stream, content_type, content_length)``.
            Returns ``(None, None, None)`` if the artifact cannot be resolved.
        """
        self.logger.debug(
            "fetch_artifact: repo=%s, path=%s",
            repository_name,
            artifact_path,
        )

        # Step 1 — Lookup repository
        repository = self._get_repository(repository_name)
        if repository is None:
            return (None, None, None)

        # Normalise the path (strip leading slashes)
        artifact_path = artifact_path.lstrip("/")

        # Step 2 — Check local cache
        cached_asset, is_fresh = self._check_cache(repository, artifact_path)

        # Step 3 — Serve fresh cache hit
        if cached_asset is not None and is_fresh:
            self.logger.debug(
                "Cache HIT (fresh) for %s:%s",
                repository_name,
                artifact_path,
            )
            content_stream = self._get_cached_content_stream(
                repository, cached_asset
            )
            if content_stream is not None:
                self._record_download_safely(
                    cached_asset, repository_name, artifact_path
                )
                self._emit_download_event(
                    repository_name,
                    artifact_path,
                    cached_asset.content_type,
                    cached_asset.size,
                )
                return (
                    content_stream,
                    cached_asset.content_type,
                    cached_asset.size,
                )
            # BlobStore content missing — fall through to remote fetch
            self.logger.warning(
                "Cache entry exists but BlobStore content missing for "
                "%s:%s — attempting remote fetch.",
                repository_name,
                artifact_path,
            )

        # Step 4 — Check negative cache (only if nothing cached)
        if cached_asset is None and self._check_negative_cache(
            repository, artifact_path
        ):
            self.logger.debug(
                "Negative cache HIT for %s:%s — skipping remote fetch.",
                repository_name,
                artifact_path,
            )
            return (None, None, None)

        # Step 5 — Attempt remote fetch
        content: bytes | None = None
        content_type: str | None = None
        content_length: int | None = None
        remote_headers: dict[str, str] | None = None
        try:
            content, content_type, content_length, remote_headers = (
                self._fetch_remote(repository, artifact_path)
            )
        except Exception as exc:
            self.logger.error(
                "Remote fetch error for %s:%s: %s",
                repository_name,
                artifact_path,
                exc,
            )

        # Step 6 — Remote fetch succeeded: cache and serve
        if content is not None:
            actual_length = len(content)
            self.logger.debug(
                "Remote fetch OK for %s:%s (%d bytes).",
                repository_name,
                artifact_path,
                actual_length,
            )
            try:
                asset = self._cache_content(
                    repository,
                    artifact_path,
                    content,
                    content_type,
                    remote_headers,
                )
            except Exception as exc:
                self.logger.error(
                    "Failed to cache content for %s:%s: %s — "
                    "serving un-cached content.",
                    repository_name,
                    artifact_path,
                    exc,
                )
                return (io.BytesIO(content), content_type, actual_length)

            # Invalidate any negative cache entry for this path
            cache_key = f"{repository_name}:{artifact_path}"
            self._negative_cache.pop(cache_key, None)

            self._emit_download_event(
                repository_name,
                artifact_path,
                content_type or asset.content_type,
                actual_length,
            )
            return (
                io.BytesIO(content),
                content_type or asset.content_type,
                actual_length,
            )

        # Step 7 — Remote failed: serve stale cached content (resilience)
        if cached_asset is not None:
            self.logger.info(
                "Remote fetch FAILED for %s:%s — serving stale cache.",
                repository_name,
                artifact_path,
            )
            content_stream = self._get_cached_content_stream(
                repository, cached_asset
            )
            if content_stream is not None:
                self._record_download_safely(
                    cached_asset, repository_name, artifact_path
                )
                self._emit_download_event(
                    repository_name,
                    artifact_path,
                    cached_asset.content_type,
                    cached_asset.size,
                )
                return (
                    content_stream,
                    cached_asset.content_type,
                    cached_asset.size,
                )

        # Step 8 — No cache, remote failed
        self.logger.info(
            "Artifact not available: %s:%s (remote unavailable, no cache).",
            repository_name,
            artifact_path,
        )
        return (None, None, None)

    # ===================================================================
    # Cache Freshness Check
    # ===================================================================

    def _check_cache(
        self,
        repository: Repository,
        artifact_path: str,
    ) -> tuple[Asset | None, bool]:
        """Check if the artifact exists in the local cache and is fresh.

        Freshness is determined by comparing the asset's ``updated_at``
        timestamp against the repository's ``contentMaxAge`` configuration.

        Args:
            repository: The proxy :class:`Repository` model instance.
            artifact_path: Artifact path within the repository.

        Returns:
            Tuple of ``(asset_or_none, is_fresh)``:

            - ``(None, False)`` — artifact is not cached.
            - ``(asset, True)`` — cached and within max-age window.
            - ``(asset, False)`` — cached but expired.
        """
        try:
            asset: Asset | None = Asset.query.filter_by(
                repository_name=repository.name,
                path=artifact_path,
            ).first()
        except Exception as exc:
            self.logger.error(
                "Error checking cache for %s:%s: %s",
                repository.name,
                artifact_path,
                exc,
            )
            return (None, False)

        if asset is None:
            return (None, False)

        # Read content max-age from proxy config
        proxy_config: dict[str, Any] = (
            repository.attributes.get("proxy", {})
            if repository.attributes
            else {}
        )
        max_age_minutes: int = proxy_config.get(
            "contentMaxAge", DEFAULT_CONTENT_MAX_AGE
        )

        # Negative max_age means infinite cache — always fresh
        if max_age_minutes < 0:
            return (asset, True)

        # Zero means always re-fetch (never fresh)
        if max_age_minutes == 0:
            return (asset, False)

        # Compute expiration cutoff
        cutoff: datetime = datetime.now(timezone.utc) - timedelta(
            minutes=max_age_minutes
        )

        cache_time: datetime | None = asset.updated_at
        if cache_time is None:
            return (asset, False)

        # Ensure timezone-aware comparison
        if cache_time.tzinfo is None:
            cache_time = cache_time.replace(tzinfo=timezone.utc)

        is_fresh: bool = cache_time > cutoff

        self.logger.debug(
            "Cache check for %s:%s — is_fresh=%s (max_age=%dm, "
            "cache_time=%s, cutoff=%s).",
            repository.name,
            artifact_path,
            is_fresh,
            max_age_minutes,
            cache_time.isoformat(),
            cutoff.isoformat(),
        )

        return (asset, is_fresh)

    # ===================================================================
    # Remote Fetch
    # ===================================================================

    def _fetch_remote(
        self,
        repository: Repository,
        artifact_path: str,
    ) -> tuple[bytes | None, str | None, int | None, dict[str, str] | None]:
        """Fetch an artifact from the remote upstream repository.

        Constructs the request URL from the repository's ``remoteUrl`` and
        the artifact path, then executes an HTTP GET via the pooled
        :class:`HttpClient`.

        HTTP status handling:

        - **200**: Read body, extract headers, return content.
        - **404**: Add to negative cache, return ``None``.
        - **401 / 403**: Log authentication failure, return ``None``.
        - **5xx**: Log server error, return ``None``.
        - **Other**: Log unexpected status, return ``None``.

        Args:
            repository: The proxy :class:`Repository` model instance.
            artifact_path: Artifact path within the repository.

        Returns:
            Tuple of ``(content_bytes, content_type, content_length, headers)``
            or ``(None, None, None, None)`` on failure.
        """
        client: HttpClient = self._get_http_client(repository)
        remote_path: str = artifact_path.lstrip("/")

        self.logger.debug(
            "Fetching remote artifact: repo=%s, path=%s, remote_base=%s",
            repository.name,
            remote_path,
            client._base_url,
        )

        try:
            response = client.get(remote_path)
        except Exception as exc:
            self.logger.error(
                "Remote connection error for %s/%s: %s",
                repository.name,
                remote_path,
                exc,
            )
            return (None, None, None, None)

        status_code: int = response.status_code

        if status_code == 200:
            content_bytes: bytes = response.content
            resp_content_type: str | None = response.headers.get(
                "Content-Type"
            )
            cl_header: str | None = response.headers.get("Content-Length")
            resp_content_length: int = (
                int(cl_header)
                if cl_header and cl_header.isdigit()
                else len(content_bytes)
            )

            # Extract useful response metadata headers
            remote_headers: dict[str, str] = {}
            for hdr in ("ETag", "Last-Modified", "Content-Type",
                        "Content-Length"):
                val = response.headers.get(hdr)
                if val:
                    remote_headers[hdr] = val

            self.logger.debug(
                "Remote fetch OK: %s/%s (%d bytes, type=%s).",
                repository.name,
                remote_path,
                resp_content_length,
                resp_content_type,
            )
            return (
                content_bytes,
                resp_content_type,
                resp_content_length,
                remote_headers,
            )

        if status_code == 404:
            self.logger.debug(
                "Remote 404 for %s/%s — adding to negative cache.",
                repository.name,
                remote_path,
            )
            self._add_to_negative_cache(repository, artifact_path)
            return (None, None, None, None)

        if status_code in (401, 403):
            self.logger.warning(
                "Authentication failure fetching %s/%s from remote "
                "(status=%d).  Check proxy authentication configuration.",
                repository.name,
                remote_path,
                status_code,
            )
            return (None, None, None, None)

        if status_code >= 500:
            self.logger.error(
                "Remote server error for %s/%s (status=%d).",
                repository.name,
                remote_path,
                status_code,
            )
            return (None, None, None, None)

        # Catch-all for other non-success codes
        self.logger.warning(
            "Unexpected response for %s/%s (status=%d).",
            repository.name,
            remote_path,
            status_code,
        )
        return (None, None, None, None)

    # ===================================================================
    # HTTP Client Management (Connection Pooling)
    # ===================================================================

    def _get_http_client(self, repository: Repository) -> HttpClient:
        """Get or create a cached HTTP client for a proxy repository.

        HTTP clients are cached by repository name for connection reuse,
        matching the per-repository ``HttpClientFacetImpl`` lifecycle from
        the Java source.

        Configuration is read from the repository's ``attributes`` JSON:

        - ``attributes.proxy.remoteUrl`` — base URL for all requests.
        - ``attributes.httpClient.connection.timeout`` — connect timeout.
        - ``attributes.httpClient.connection.readTimeout`` — read timeout.
        - ``attributes.httpClient.connection.retries`` — max retries.
        - ``attributes.httpClient.connection.useTrustStore`` — SSL verify.
        - ``attributes.httpClient.authentication`` — auth credentials.

        Args:
            repository: The proxy :class:`Repository` model instance.

        Returns:
            A configured :class:`HttpClient` for the repository.
        """
        repo_name: str = repository.name

        # Return cached client if available
        if repo_name in self._clients:
            return self._clients[repo_name]

        # Extract configuration from repository attributes
        attrs: dict[str, Any] = repository.attributes or {}
        proxy_config: dict[str, Any] = attrs.get("proxy", {})
        http_config: dict[str, Any] = attrs.get("httpClient", {})
        conn_config: dict[str, Any] = http_config.get("connection", {})
        auth_config: dict[str, Any] | None = http_config.get(
            "authentication"
        )

        remote_url: str = proxy_config.get("remoteUrl", "")

        # SSRF validation (CWE-918): warn on internal/private URLs
        url_safe, url_reason = self._validate_upstream_url(remote_url)
        if not url_safe:
            self.logger.warning(
                "Upstream URL for repository '%s' failed SSRF validation: %s "
                "(URL: %s). Proceeding with admin-configured URL, but this "
                "may pose a security risk.",
                repo_name,
                url_reason,
                remote_url,
            )

        # Determine authentication method
        auth_tuple: tuple[str, str] | None = None
        bearer_token: str | None = None

        if auth_config:
            auth_type: str = auth_config.get("type", "username")
            if auth_type in ("username", "basic"):
                username: str = auth_config.get("username", "")
                password: str = auth_config.get("password", "")
                if username:
                    auth_tuple = (username, password)
            elif auth_type == "bearer":
                bearer_token = auth_config.get("token")

        # Create the client
        client = HttpClient(
            base_url=remote_url,
            connect_timeout=float(
                conn_config.get("timeout", DEFAULT_CONNECT_TIMEOUT)
            ),
            read_timeout=float(
                conn_config.get("readTimeout", DEFAULT_READ_TIMEOUT)
            ),
            max_retries=int(
                conn_config.get("retries", DEFAULT_RETRIES)
            ),
            verify_ssl=bool(
                conn_config.get("useTrustStore", True)
            ),
            auth=auth_tuple,
            bearer_token=bearer_token,
        )

        # Cache for connection reuse
        self._clients[repo_name] = client

        self.logger.info(
            "Created HTTP client for proxy repository '%s' (remote=%s, "
            "format=%s, timeout=%ss, retries=%s).",
            repo_name,
            remote_url,
            repository.format,
            conn_config.get("timeout", DEFAULT_CONNECT_TIMEOUT),
            conn_config.get("retries", DEFAULT_RETRIES),
        )

        return client

    # ===================================================================
    # Cache Storage
    # ===================================================================

    def _cache_content(
        self,
        repository: Repository,
        artifact_path: str,
        content: bytes,
        content_type: str | None,
        remote_headers: dict[str, str] | None = None,
    ) -> Asset:
        """Store fetched content in the repository's BlobStore and update the Asset record.

        This method:

        1. Looks up the :class:`BlobStoreConfig` for the repository.
        2. Creates the appropriate BlobStore backend (File or S3).
        3. Computes SHA-1 / SHA-256 / MD5 checksums.
        4. Stores the binary content in the BlobStore.
        5. Creates or updates the :class:`Asset` record in the database.
        6. Optionally links the asset to a :class:`Component`.

        Args:
            repository: The proxy :class:`Repository` model instance.
            artifact_path: Artifact path within the repository.
            content: Binary content to cache.
            content_type: MIME type of the content (may be ``None``).
            remote_headers: HTTP headers from the remote response.

        Returns:
            The created or updated :class:`Asset` record.

        Raises:
            ValueError: If the BlobStore configuration is not found.
            Exception: On BlobStore write or database commit failure.
        """
        # 1 — Resolve BlobStore configuration
        blob_config: BlobStoreConfig | None = db.session.get(
            BlobStoreConfig, repository.blob_store_name
        )
        if blob_config is None:
            raise ValueError(
                f"BlobStore configuration '{repository.blob_store_name}' "
                f"not found for repository '{repository.name}'."
            )

        self.logger.debug(
            "Caching content for %s:%s using BlobStore '%s' (type=%s).",
            repository.name,
            artifact_path,
            blob_config.blob_store_name,
            blob_config.type,
        )

        blobstore = create_blobstore_from_model(blob_config)

        # Ensure BlobStore is started
        if not blobstore.is_started:
            blobstore.start()

        # 2 — Compute checksums
        checksums: dict[str, str] = compute_checksums(content)

        # 3 — Detect content type if not provided
        if not content_type:
            content_type = detect_mime_type(artifact_path)

        # 4 — Store in BlobStore
        blob = blobstore.create(
            blob_id=None,  # auto-generate via BlobId.generate()
            data=content,
            content_type=content_type,
        )
        blob_ref: str = str(blob.blob_id)

        # 5 — Create or update Asset record
        now: datetime = datetime.now(timezone.utc)
        asset: Asset | None = Asset.query.filter_by(
            repository_name=repository.name,
            path=artifact_path,
        ).first()

        if asset is None:
            # Generate a unique identifier for tracking
            asset_uuid: str = generate_uuid()
            asset = Asset(
                repository_name=repository.name,
                path=artifact_path,
                content_type=content_type,
                checksum_sha1=checksums.get("sha1"),
                checksum_sha256=checksums.get("sha256"),
                checksum_md5=checksums.get("md5"),
                size=len(content),
                blob_ref=blob_ref,
                last_downloaded=now,
            )
            # Store remote headers and asset UUID as attributes
            asset_attrs: dict[str, Any] = {"asset_uuid": asset_uuid}
            if remote_headers:
                asset_attrs["remote_headers"] = remote_headers
            asset.attributes = asset_attrs

            db.session.add(asset)
            self.logger.debug(
                "Created new cached asset: %s:%s (blob_ref=%s, uuid=%s).",
                repository.name,
                artifact_path,
                blob_ref,
                asset_uuid,
            )
        else:
            # Update existing cached asset with fresh content
            asset.content_type = content_type
            asset.checksum_sha1 = checksums.get("sha1")
            asset.checksum_sha256 = checksums.get("sha256")
            asset.checksum_md5 = checksums.get("md5")
            asset.size = len(content)
            asset.blob_ref = blob_ref
            asset.last_downloaded = now

            existing_attrs: dict[str, Any] = dict(asset.attributes or {})
            if remote_headers:
                existing_attrs["remote_headers"] = remote_headers
            asset.attributes = existing_attrs

            db.session.add(asset)
            self.logger.debug(
                "Updated cached asset: %s:%s (blob_ref=%s).",
                repository.name,
                artifact_path,
                blob_ref,
            )

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        # 6 — Link to component if applicable
        self._link_component_if_needed(repository, artifact_path, asset)

        return asset

    # ===================================================================
    # Component Linking Helper
    # ===================================================================

    def _link_component_if_needed(
        self,
        repository: Repository,
        artifact_path: str,
        asset: Asset,
    ) -> None:
        """Create or find a Component and link the asset to it.

        For proxy repositories, components are created lazily when content
        is first cached.  The component coordinates are derived from the
        artifact path using simple heuristics (the path structure varies
        by repository format).

        Args:
            repository: The proxy :class:`Repository` model instance.
            artifact_path: The artifact path.
            asset: The :class:`Asset` record to link.
        """
        if asset.component_id is not None:
            return  # Already linked

        parts: list[str] = artifact_path.strip("/").split("/")
        if not parts or parts == [""]:
            return

        # Derive component coordinates from path
        comp_name: str = parts[-1] if parts else artifact_path
        comp_namespace: str | None = (
            "/".join(parts[:-1]) if len(parts) > 1 else None
        )
        comp_version: str | None = None

        # Format-specific version extraction heuristic
        if len(parts) >= 2 and repository.format in ("maven2", "pypi"):
            comp_version = parts[-2] if len(parts) >= 2 else None
            comp_name = parts[-3] if len(parts) >= 3 else comp_name
            comp_namespace = (
                "/".join(parts[:-3]) if len(parts) > 3 else None
            )

        try:
            component: Component | None = Component.query.filter_by(
                repository_name=repository.name,
                namespace=comp_namespace,
                name=comp_name,
                version=comp_version,
            ).first()

            if component is None:
                component = Component(
                    repository_name=repository.name,
                    namespace=comp_namespace,
                    name=comp_name,
                    version=comp_version,
                )
                db.session.add(component)
                db.session.flush()  # Obtain the auto-generated ID
                self.logger.debug(
                    "Created component for %s: %s/%s/%s (id=%s).",
                    repository.name,
                    comp_namespace,
                    comp_name,
                    comp_version,
                    component.id,
                )

                # Emit component uploaded event
                self._emit_component_event(
                    repository, comp_namespace, comp_name, comp_version
                )

            asset.component_id = component.id
            db.session.add(asset)
            db.session.commit()
        except Exception as exc:
            self.logger.debug(
                "Could not link component for %s:%s: %s",
                repository.name,
                artifact_path,
                exc,
            )
            try:
                db.session.rollback()
            except Exception:
                pass

    # ===================================================================
    # Cached Content Retrieval
    # ===================================================================

    def _get_cached_content_stream(
        self,
        repository: Repository,
        asset: Asset,
    ) -> BinaryIO | None:
        """Retrieve cached artifact content from the BlobStore as a stream.

        Args:
            repository: The proxy :class:`Repository` model instance.
            asset: The :class:`Asset` record with ``blob_ref`` pointing to
                BlobStore content.

        Returns:
            A readable binary stream (``BinaryIO``), or ``None`` if the
            content cannot be retrieved.
        """
        if not asset.blob_ref:
            self.logger.warning(
                "Asset %s:%s has no blob_ref — cannot serve from cache.",
                repository.name,
                asset.path,
            )
            return None

        try:
            blob_config: BlobStoreConfig | None = db.session.get(
                BlobStoreConfig, repository.blob_store_name
            )
            if blob_config is None:
                self.logger.error(
                    "BlobStore '%s' not found for repository '%s'.",
                    repository.blob_store_name,
                    repository.name,
                )
                return None

            blobstore = create_blobstore_from_model(blob_config)
            if not blobstore.is_started:
                blobstore.start()

            # Parse the blob reference string into a BlobId
            from src.app.storage import BlobId

            blob_id = BlobId.from_string(asset.blob_ref)
            stream: BinaryIO | None = blobstore.get_stream(blob_id)
            return stream

        except Exception as exc:
            self.logger.error(
                "Error reading cached content for %s:%s (blob_ref=%s): %s",
                repository.name,
                asset.path,
                asset.blob_ref,
                exc,
            )
            return None

    # ===================================================================
    # Negative Cache
    # ===================================================================

    def _check_negative_cache(
        self,
        repository: Repository,
        artifact_path: str,
    ) -> bool:
        """Check if the artifact path is in the negative cache.

        The negative cache stores paths known to not exist on the remote
        upstream, avoiding repeated 404 requests.

        Args:
            repository: The proxy :class:`Repository` model instance.
            artifact_path: Artifact path to check.

        Returns:
            ``True`` if the path is in the negative cache and the entry
            has not expired; ``False`` otherwise.
        """
        neg_config: dict[str, Any] = (
            repository.attributes.get("negativeCache", {})
            if repository.attributes
            else {}
        )
        if not neg_config.get("enabled", True):
            return False

        cache_key: str = f"{repository.name}:{artifact_path}"

        if cache_key not in self._negative_cache:
            return False

        expiry: datetime = self._negative_cache[cache_key]
        now: datetime = datetime.now(timezone.utc)

        if now >= expiry:
            # Expired — evict and return False
            del self._negative_cache[cache_key]
            self.logger.debug(
                "Negative cache expired for %s:%s.",
                repository.name,
                artifact_path,
            )
            return False

        self.logger.debug(
            "Negative cache active for %s:%s (expires %s).",
            repository.name,
            artifact_path,
            expiry.isoformat(),
        )
        return True

    def _add_to_negative_cache(
        self,
        repository: Repository,
        artifact_path: str,
    ) -> None:
        """Add an artifact path to the negative cache.

        The entry will expire after the repository's configured
        ``negativeCache.timeToLive`` minutes.

        Args:
            repository: The proxy :class:`Repository` model instance.
            artifact_path: Artifact path that was not found on the remote.
        """
        neg_config: dict[str, Any] = (
            repository.attributes.get("negativeCache", {})
            if repository.attributes
            else {}
        )
        if not neg_config.get("enabled", True):
            return

        ttl_minutes: int = neg_config.get(
            "timeToLive", DEFAULT_NEGATIVE_CACHE_TTL
        )
        expiry: datetime = datetime.now(timezone.utc) + timedelta(
            minutes=ttl_minutes
        )

        cache_key: str = f"{repository.name}:{artifact_path}"
        self._negative_cache[cache_key] = expiry

        self.logger.debug(
            "Added to negative cache: %s:%s (TTL=%dm, expires=%s).",
            repository.name,
            artifact_path,
            ttl_minutes,
            expiry.isoformat(),
        )

    def invalidate_negative_cache(
        self,
        repository_name: str,
        path: str | None = None,
    ) -> None:
        """Invalidate negative cache entries.

        Can invalidate a specific path or all entries for a repository.
        Typically called when proxy configuration is updated.

        Args:
            repository_name: Name of the repository.
            path: Specific artifact path to invalidate.  If ``None``,
                all entries for the repository are invalidated.
        """
        if path is not None:
            cache_key = f"{repository_name}:{path}"
            if cache_key in self._negative_cache:
                del self._negative_cache[cache_key]
                self.logger.debug(
                    "Invalidated negative cache entry: %s.",
                    cache_key,
                )
        else:
            prefix = f"{repository_name}:"
            keys_to_remove: list[str] = [
                k for k in self._negative_cache if k.startswith(prefix)
            ]
            for key in keys_to_remove:
                del self._negative_cache[key]
            if keys_to_remove:
                self.logger.info(
                    "Invalidated %d negative cache entries for "
                    "repository '%s'.",
                    len(keys_to_remove),
                    repository_name,
                )

    # ===================================================================
    # Metadata Fetch
    # ===================================================================

    def fetch_metadata(
        self,
        repository_name: str,
        metadata_path: str,
    ) -> dict | None:
        """Fetch format-specific metadata from the remote upstream.

        Uses the shorter ``metadataMaxAge`` for caching decisions.  Parses
        the response as JSON or XML depending on the MIME content type.

        Examples of metadata files:

        - Maven: ``maven-metadata.xml``
        - npm: ``package.json``
        - Docker: ``v2/_catalog``
        - PyPI: ``simple/<project>/``

        Args:
            repository_name: Name of the proxy repository.
            metadata_path: Path to the metadata file.

        Returns:
            Parsed metadata as a dictionary, or ``None`` if not available.
        """
        self.logger.debug(
            "fetch_metadata: repo=%s, path=%s",
            repository_name,
            metadata_path,
        )

        repository = self._get_repository(repository_name)
        if repository is None:
            return None

        metadata_path = metadata_path.lstrip("/")

        # Determine metadata max age from repository config
        proxy_config: dict[str, Any] = (
            repository.attributes.get("proxy", {})
            if repository.attributes
            else {}
        )
        metadata_max_age: int = proxy_config.get(
            "metadataMaxAge", DEFAULT_METADATA_MAX_AGE
        )

        # Check cache
        cached_asset: Asset | None = Asset.query.filter_by(
            repository_name=repository.name,
            path=metadata_path,
        ).first()

        # Evaluate metadata freshness with metadata-specific TTL
        is_metadata_fresh: bool = False
        if cached_asset is not None and cached_asset.updated_at is not None:
            if metadata_max_age < 0:
                is_metadata_fresh = True
            elif metadata_max_age > 0:
                cache_time = cached_asset.updated_at
                if cache_time.tzinfo is None:
                    cache_time = cache_time.replace(tzinfo=timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(
                    minutes=metadata_max_age
                )
                is_metadata_fresh = cache_time > cutoff

        # Serve from fresh cache
        if cached_asset is not None and is_metadata_fresh:
            stream = self._get_cached_content_stream(repository, cached_asset)
            if stream is not None:
                try:
                    raw_data: bytes = stream.read()
                    return self._parse_metadata(
                        raw_data, cached_asset.content_type, metadata_path
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Error reading cached metadata %s:%s: %s",
                        repository_name,
                        metadata_path,
                        exc,
                    )

        # Fetch from remote
        content: bytes | None = None
        content_type: str | None = None
        remote_headers: dict[str, str] | None = None
        try:
            content, content_type, _, remote_headers = self._fetch_remote(
                repository, metadata_path
            )
        except Exception as exc:
            self.logger.error(
                "Metadata remote fetch error for %s:%s: %s",
                repository_name,
                metadata_path,
                exc,
            )

        if content is not None:
            # Cache the metadata
            try:
                self._cache_content(
                    repository,
                    metadata_path,
                    content,
                    content_type,
                    remote_headers,
                )
            except Exception as exc:
                self.logger.warning(
                    "Failed to cache metadata %s:%s: %s",
                    repository_name,
                    metadata_path,
                    exc,
                )
            return self._parse_metadata(content, content_type, metadata_path)

        # Serve stale cached metadata as fallback
        if cached_asset is not None:
            stream = self._get_cached_content_stream(repository, cached_asset)
            if stream is not None:
                try:
                    raw_data = stream.read()
                    return self._parse_metadata(
                        raw_data, cached_asset.content_type, metadata_path
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Error reading stale metadata %s:%s: %s",
                        repository_name,
                        metadata_path,
                        exc,
                    )

        return None

    # ===================================================================
    # Remote Health Check
    # ===================================================================

    def check_remote_health(
        self,
        repository_name: str,
    ) -> dict:
        """Check whether the remote upstream is accessible.

        Sends a HEAD request to the remote URL and measures response
        latency.  Uses ``time.monotonic()`` for high-precision timing.

        Args:
            repository_name: Name of the proxy repository.

        Returns:
            Dictionary with health status::

                {
                    'available': bool,
                    'latency_ms': int,
                    'status_code': int,
                    'repository_name': str,
                    'remote_url': str,
                }
        """
        default_result: dict[str, Any] = {
            "available": False,
            "latency_ms": 0,
            "status_code": 0,
            "repository_name": repository_name,
            "remote_url": "",
        }

        repository = self._get_repository(repository_name)
        if repository is None:
            return default_result

        proxy_config: dict[str, Any] = (
            repository.attributes.get("proxy", {})
            if repository.attributes
            else {}
        )
        remote_url: str = proxy_config.get("remoteUrl", "")

        if not remote_url:
            return {**default_result, "remote_url": ""}

        client: HttpClient = self._get_http_client(repository)

        # Try check_remote_available first (lightweight HEAD)
        start_time: float = time.monotonic()
        try:
            is_available: bool = client.check_remote_available(
                remote_url, timeout=5.0
            )
            elapsed_ms: int = int(
                (time.monotonic() - start_time) * 1000
            )

            if is_available:
                # Do a HEAD for the status code
                try:
                    head_response = client.head("/")
                    status_code: int = head_response.status_code
                except Exception:
                    status_code = 200  # check_remote_available passed
            else:
                status_code = 0

        except Exception as exc:
            elapsed_ms = int(
                (time.monotonic() - start_time) * 1000
            )
            self.logger.warning(
                "Health check failed for %s (%s): %s",
                repository_name,
                remote_url,
                exc,
            )
            return {
                "available": False,
                "latency_ms": elapsed_ms,
                "status_code": 0,
                "repository_name": repository_name,
                "remote_url": remote_url,
            }

        self.logger.debug(
            "Health check for %s: available=%s, latency=%dms, status=%d.",
            repository_name,
            is_available,
            elapsed_ms,
            status_code,
        )

        return {
            "available": is_available,
            "latency_ms": elapsed_ms,
            "status_code": status_code,
            "repository_name": repository_name,
            "remote_url": remote_url,
        }

    # ===================================================================
    # Event Dispatch Helpers
    # ===================================================================

    def _emit_download_event(
        self,
        repository_name: str,
        artifact_path: str,
        content_type: str | None,
        size: int | None,
    ) -> None:
        """Emit an ``ASSET_DOWNLOADED`` event for audit logging and webhooks.

        Args:
            repository_name: Repository name.
            artifact_path: Downloaded artifact path.
            content_type: MIME content type.
            size: Content size in bytes.
        """
        try:
            payload = AssetEventPayload(
                repository_name=repository_name,
                asset_path=artifact_path,
                content_type=content_type,
                size=size,
            )
            emit_event(
                EventType.ASSET_DOWNLOADED,
                payload=dataclasses.asdict(payload),
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to emit ASSET_DOWNLOADED event for %s:%s: %s",
                repository_name,
                artifact_path,
                exc,
            )

    def _emit_component_event(
        self,
        repository: Repository,
        namespace: str | None,
        name: str,
        version: str | None,
    ) -> None:
        """Emit a ``COMPONENT_UPLOADED`` event when a component is created.

        Args:
            repository: The proxy repository.
            namespace: Component namespace.
            name: Component name.
            version: Component version.
        """
        try:
            emit_event(
                EventType.COMPONENT_UPLOADED,
                payload={
                    "repository_name": repository.name,
                    "component_name": name,
                    "component_version": version,
                    "namespace": namespace,
                    "format": repository.format,
                },
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to emit COMPONENT_UPLOADED event: %s", exc
            )

    # ===================================================================
    # Download Recording Helper
    # ===================================================================

    def _record_download_safely(
        self,
        asset: Asset,
        repository_name: str,
        artifact_path: str,
    ) -> None:
        """Record a download event, handling database errors gracefully.

        Calls :meth:`Asset.record_download` to update ``last_downloaded``
        and wraps the operation in error handling so that a failed database
        write does not prevent serving the artifact.

        Args:
            asset: The :class:`Asset` record to update.
            repository_name: For logging.
            artifact_path: For logging.
        """
        try:
            asset.record_download()
        except Exception as exc:
            self.logger.warning(
                "Failed to record download for %s:%s: %s",
                repository_name,
                artifact_path,
                exc,
            )

    # ===================================================================
    # Metadata Parsing Helpers
    # ===================================================================

    def _parse_metadata(
        self,
        data: bytes,
        content_type: str | None,
        path: str,
    ) -> dict[str, Any]:
        """Parse metadata content as JSON or XML.

        Decision logic:

        1. If ``content_type`` contains ``json`` → parse as JSON.
        2. If ``content_type`` contains ``xml`` or path ends with ``.xml``
           → parse as XML.
        3. Otherwise → try JSON, fall back to raw text.

        Args:
            data: Raw metadata bytes.
            content_type: MIME content type.
            path: Metadata file path (used for extension-based detection).

        Returns:
            Parsed metadata as a dictionary.
        """
        # Strategy 1: JSON
        if content_type and "json" in content_type.lower():
            try:
                return json.loads(data)
            except (json.JSONDecodeError, ValueError) as exc:
                self.logger.warning(
                    "Failed to parse JSON metadata for %s: %s",
                    path,
                    exc,
                )
                return {"raw": data.decode("utf-8", errors="replace")}

        # Strategy 2: XML
        if (
            content_type and "xml" in content_type.lower()
        ) or path.endswith(".xml"):
            try:
                root: ElementTree.Element = ElementTree.fromstring(data)
                return self._xml_to_dict(root)
            except ElementTree.ParseError as exc:
                self.logger.warning(
                    "Failed to parse XML metadata for %s: %s",
                    path,
                    exc,
                )
                return {"raw": data.decode("utf-8", errors="replace")}

        # Strategy 3: Try JSON first, fall back to raw
        try:
            return json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return {"raw": data.decode("utf-8", errors="replace")}

    @staticmethod
    def _xml_to_dict(element: ElementTree.Element) -> dict[str, Any]:
        """Convert an XML element tree to a nested dictionary.

        Namespace prefixes are stripped for readability.  Repeated child
        tags are collected into lists; singleton children are stored as
        plain dicts.

        Args:
            element: Root XML :class:`Element`.

        Returns:
            Dictionary representation of the XML tree.
        """
        result: dict[str, Any] = {}

        # Strip namespace prefix if present
        tag: str = element.tag
        if "}" in tag:
            tag = tag.split("}", 1)[1]

        # Include text content
        if element.text and element.text.strip():
            result["_text"] = element.text.strip()

        # Include element attributes
        if element.attrib:
            result["_attributes"] = dict(element.attrib)

        # Process child elements
        children: dict[str, list[dict[str, Any]]] = {}
        for child in element:
            child_tag: str = child.tag
            if "}" in child_tag:
                child_tag = child_tag.split("}", 1)[1]

            child_dict: dict[str, Any] = ProxyService._xml_to_dict(child)
            if child_tag not in children:
                children[child_tag] = []
            children[child_tag].append(child_dict)

        for key, value_list in children.items():
            result[key] = (
                value_list[0] if len(value_list) == 1 else value_list
            )

        return {tag: result}

    # ===================================================================
    # Streaming Support
    # ===================================================================

    def _stream_remote_content(
        self,
        repository: Repository,
        artifact_path: str,
    ) -> Generator[bytes, None, None]:
        """Stream remote artifact content in chunks.

        Uses :meth:`HttpClient.stream_get` for memory-efficient transfer
        of large artifacts.  This method is used internally when streaming
        is preferred over loading the entire content into memory.

        Args:
            repository: The proxy :class:`Repository` model instance.
            artifact_path: Artifact path within the repository.

        Yields:
            Successive chunks of the response body.
        """
        client: HttpClient = self._get_http_client(repository)
        remote_path: str = artifact_path.lstrip("/")

        self.logger.debug(
            "Streaming remote content: repo=%s, path=%s",
            repository.name,
            remote_path,
        )

        yield from client.stream_get(remote_path)

    # ===================================================================
    # Cleanup
    # ===================================================================

    def close_clients(self) -> None:
        """Close all cached HTTP client connections.

        Called during application shutdown to release connection pool
        resources.  After this call all existing HTTP clients are invalid
        and new clients will be created on subsequent requests.

        Also clears the in-memory negative cache.
        """
        client_count: int = len(self._clients)

        for repo_name, client in self._clients.items():
            try:
                client.close()
                self.logger.debug(
                    "Closed HTTP client for repository '%s'.",
                    repo_name,
                )
            except Exception as exc:
                self.logger.warning(
                    "Error closing HTTP client for '%s': %s",
                    repo_name,
                    exc,
                )

        self._clients.clear()
        self._negative_cache.clear()

        self.logger.info(
            "Closed %d HTTP client(s) and cleared negative cache.",
            client_count,
        )
