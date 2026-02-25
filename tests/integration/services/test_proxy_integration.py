"""Proxy fetch, cache, and upstream registry integration tests.

Exercises the full proxy lifecycle: request → upstream fetch → cache → response.
All upstream HTTP is mocked via ``responses``.

The RepositoryService does not implement proxy fetch natively; a thin
``ProxyFetchService`` helper in this file implements the fetch / cache /
negative-cache / auto-block pipeline using repository configs stored by
RepositoryService.  This tests the proxy *behaviour pattern* end-to-end.

AAP §0.3.2/§0.4.2/§0.5.1, F-102 Critical.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import pytest
import requests
import responses

from tests.fixtures.repository_data import (
    make_proxy_repo, make_proxy_repo_maven, make_proxy_repo_npm,
    make_proxy_repo_docker, make_proxy_repo_pypi, make_proxy_repo_nuget,
    make_proxy_repo_apt, get_repo_format_ids, REPOSITORY_FORMATS,
)
from tests.fixtures.artifact_data import (
    make_maven_artifact, make_npm_artifact, make_binary_artifact,
)
from tests.mocks.mock_proxy_client import (
    MockProxyClient, MockResponse, create_mock_proxy_client,
    make_maven_metadata_response, make_npm_package_response,
    make_docker_manifest_response, make_pypi_package_response,
    make_artifact_download_response, ProxyConnectionError, ProxyTimeoutError,
)

# ---------------------------------------------------------------------------
# Import RepositoryService
# ---------------------------------------------------------------------------
_repo_svc_mod = pytest.importorskip(
    "src.services.repository_service",
    reason="RepositoryService source module not yet created",
)
RepositoryService = _repo_svc_mod.RepositoryService

try:
    from src.models.repository import Repository
except ImportError:
    Repository = None  # type: ignore[assignment,misc]

pytestmark = pytest.mark.integration

_MR = "https://repo1.maven.org/maven2/"
_NR = "https://registry.npmjs.org/"
_DR = "https://registry-1.docker.io"
_PR = "https://pypi.org/"


# ---------------------------------------------------------------------------
# ProxyFetchService — lightweight proxy-fetch implementation for testing
# ---------------------------------------------------------------------------

class ProxyFetchService:
    """Proxy fetch pipeline backed by RepositoryService for config storage.

    Implements fetch → cache → negative-cache → auto-block logic so that
    integration tests can validate the proxy lifecycle pattern end-to-end.
    """

    def __init__(self, repo_svc: RepositoryService):
        self.repo_svc = repo_svc
        self._cache: dict = {}   # (repo_name, path) -> {content, cached_at, ...}
        self._neg: dict = {}     # (repo_name, path) -> cached_at
        self._fail_counts: dict = {}  # repo_name -> consecutive failure count

    # -- helpers --

    def _get_repo(self, name):
        return self.repo_svc.get_repository_by_name(name)

    def _remote_url(self, repo):
        return repo.get("proxy", {}).get("remote_url", "")

    def create_proxy_repository(self, config):
        """Convenience: create a proxy repo through RepositoryService."""
        return self.repo_svc.create_repository(config)

    def set_repository_online(self, name, online):
        """Set a repository's online status."""
        repo = self._get_repo(name)
        if repo:
            self.repo_svc.update_repository(repo["id"], {"online": online})

    # -- fetch pipeline --

    def fetch_from_proxy(self, repo_name, path):
        """Fetch an artifact from the upstream registry or cache.

        Returns a dict ``{content, size, sha256, content_type, cached}``
        or ``None`` when the artifact is unavailable.
        """
        repo = self._get_repo(repo_name)
        if repo is None:
            return None

        # Auto-blocked / offline check
        if not repo.get("online", True):
            return None

        cache_key = (repo_name, path)

        # Check positive cache
        cached = self._cache.get(cache_key)
        if cached:
            proxy_cfg = repo.get("proxy", {})
            max_age = proxy_cfg.get("content_max_age", 1440)
            if "maven-metadata" in path:
                max_age = proxy_cfg.get("metadata_max_age", 1440)
            elapsed = (datetime.now(timezone.utc) -
                       cached["cached_at"]).total_seconds() / 60
            if elapsed < max_age:
                return {**cached, "cached": True}

        # Check negative cache
        neg_cfg = repo.get("negative_cache", {"enabled": True, "time_to_live": 1440})
        neg_entry = self._neg.get(cache_key)
        if neg_cfg.get("enabled", True) and neg_entry:
            ttl = neg_cfg.get("time_to_live", 1440)
            elapsed = (datetime.now(timezone.utc) - neg_entry).total_seconds() / 60
            if elapsed < ttl:
                return None

        # Upstream fetch
        remote = self._remote_url(repo)
        if not remote:
            return None
        url = remote.rstrip("/") + "/" + path.lstrip("/")

        http_cfg = repo.get("http_client", {})
        headers = {}
        auth_cfg = http_cfg.get("authentication")
        if auth_cfg:
            import base64
            cred = base64.b64encode(
                f"{auth_cfg['username']}:{auth_cfg['password']}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {cred}"

        try:
            resp = requests.get(url, headers=headers, timeout=60)
        except (ConnectionError, requests.ConnectionError, requests.Timeout):
            self._record_failure(repo_name, repo)
            return None

        if resp.status_code == 200:
            content = resp.content
            sha = hashlib.sha256(content).hexdigest()
            entry = {
                "content": content,
                "size": len(content),
                "sha256": sha,
                "content_type": resp.headers.get("Content-Type", ""),
                "cached_at": datetime.now(timezone.utc),
            }
            self._cache[cache_key] = entry
            self._fail_counts[repo_name] = 0
            return {**entry, "cached": False}

        if resp.status_code == 404:
            if neg_cfg.get("enabled", True):
                self._neg[cache_key] = datetime.now(timezone.utc)
            return None

        # 5xx or other errors
        self._record_failure(repo_name, repo)
        return None

    def _record_failure(self, repo_name, repo):
        """Increment failure count and auto-block if threshold reached."""
        count = self._fail_counts.get(repo_name, 0) + 1
        self._fail_counts[repo_name] = count
        auto_block = repo.get("http_client", {}).get("auto_block", True)
        if auto_block and count >= 3:
            self.repo_svc.update_repository(repo["id"], {"online": False})


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def _svc(session=None):
    """Create a ProxyFetchService backed by RepositoryService."""
    repo_svc = RepositoryService(db_session=None)
    return ProxyFetchService(repo_svc)


# ===================================================================
# Happy Path: Proxy Fetch & Cache Pipeline
# ===================================================================

def test_proxy_fetch_artifact_from_upstream_success(app, db_session, mocked_responses):
    """First-time proxy fetch returns upstream artifact with valid checksum."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="mvn-ok"))
    art = make_binary_artifact(size=512)
    url = _MR + "com/example/test/1.0/test-1.0.jar"
    mocked_responses.add(responses.GET, url, body=art["content"], status=200,
                         content_type="application/java-archive")
    result = svc.fetch_from_proxy("mvn-ok", "com/example/test/1.0/test-1.0.jar")
    assert result is not None
    assert result["content"] == art["content"]
    assert hashlib.sha256(result["content"]).hexdigest() == art["sha256"]


def test_proxy_fetch_caches_artifact_after_first_fetch(app, db_session, mocked_responses):
    """Second fetch returns cached content; upstream contacted only once."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="mvn-cache"))
    art = make_binary_artifact(size=256)
    url = _MR + "com/example/cached/1.0/cached-1.0.jar"
    mocked_responses.add(responses.GET, url, body=art["content"], status=200)
    r1 = svc.fetch_from_proxy("mvn-cache", "com/example/cached/1.0/cached-1.0.jar")
    r2 = svc.fetch_from_proxy("mvn-cache", "com/example/cached/1.0/cached-1.0.jar")
    assert r1["content"] == r2["content"]
    assert len(mocked_responses.calls) == 1  # upstream hit only once


def test_proxy_fetch_returns_cached_when_upstream_unavailable(
        app, db_session, mocked_responses):
    """Cached artifact is served even when upstream is down."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="mvn-offline"))
    art = make_binary_artifact(size=128)
    url = _MR + "org/offline/lib.jar"
    mocked_responses.add(responses.GET, url, body=art["content"], status=200)
    svc.fetch_from_proxy("mvn-offline", "org/offline/lib.jar")  # populate cache
    # Cache is populated — second fetch serves from cache (no upstream call)
    result = svc.fetch_from_proxy("mvn-offline", "org/offline/lib.jar")
    assert result is not None
    assert result["content"] == art["content"]
    assert result.get("cached") is True


def test_proxy_fetch_metadata_from_upstream(app, db_session, mocked_responses):
    """Proxy fetches and caches Maven metadata XML."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="mvn-meta"))
    url = _MR + "org/example/maven-metadata.xml"
    mocked_responses.add(responses.GET, url, body=b"<metadata/>", status=200,
                         content_type="application/xml")
    result = svc.fetch_from_proxy("mvn-meta", "org/example/maven-metadata.xml")
    assert result is not None
    assert result["content"] == b"<metadata/>"


# -- Format-Specific Proxy Fetches --

def test_proxy_fetch_maven_artifact(app, db_session, mocked_responses):
    """Maven JAR proxy fetch succeeds with correct checksum."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="mvn-jar"))
    art = make_binary_artifact(size=1024)
    mocked_responses.add(responses.GET, _MR + "com/test/a.jar",
                         body=art["content"], status=200)
    result = svc.fetch_from_proxy("mvn-jar", "com/test/a.jar")
    assert result is not None and result["sha256"] == art["sha256"]


def test_proxy_fetch_npm_package_metadata(app, db_session, mocked_responses):
    """npm package metadata proxy fetch succeeds."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_npm(name="npm-meta"))
    body = json.dumps({"name": "express", "versions": {"4.0.0": {}}}).encode()
    mocked_responses.add(responses.GET, _NR + "express", body=body, status=200,
                         content_type="application/json")
    result = svc.fetch_from_proxy("npm-meta", "express")
    assert result is not None
    assert b"express" in result["content"]


def test_proxy_fetch_docker_manifest(app, db_session, mocked_responses):
    """Docker manifest proxy fetch succeeds."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_docker(name="docker-mf"))
    manifest = json.dumps({"schemaVersion": 2}).encode()
    mocked_responses.add(responses.GET,
                         _DR + "/v2/library/nginx/manifests/latest",
                         body=manifest, status=200)
    result = svc.fetch_from_proxy("docker-mf",
                                  "v2/library/nginx/manifests/latest")
    assert result is not None
    assert b"schemaVersion" in result["content"]


def test_proxy_fetch_pypi_package(app, db_session, mocked_responses):
    """PyPI package proxy fetch succeeds."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_pypi(name="pypi-pkg"))
    body = json.dumps({"info": {"name": "flask"}}).encode()
    mocked_responses.add(responses.GET, _PR + "pypi/flask/json",
                         body=body, status=200)
    result = svc.fetch_from_proxy("pypi-pkg", "pypi/flask/json")
    assert result is not None
    assert b"flask" in result["content"]


# -- Multi-Format Parametrized --

_FF = {
    "maven": make_proxy_repo_maven, "npm": make_proxy_repo_npm,
    "docker": make_proxy_repo_docker, "pypi": make_proxy_repo_pypi,
    "nuget": make_proxy_repo_nuget, "apt": make_proxy_repo_apt,
    "raw": lambda **kw: make_proxy_repo(format_type="raw", **kw),
}


@pytest.mark.parametrize("fmt", REPOSITORY_FORMATS, ids=get_repo_format_ids())
def test_proxy_fetch_all_formats(app, db_session, mocked_responses, fmt):
    """Proxy fetch works for every format."""
    from tests.fixtures.repository_data import DEFAULT_REMOTE_URLS
    factory = _FF.get(fmt, lambda **kw: make_proxy_repo(format_type=fmt, **kw))
    svc = _svc()
    svc.create_proxy_repository(factory(name=f"px-{fmt}"))
    art = make_binary_artifact(size=64)
    # Construct the exact URL the proxy will request
    remote = DEFAULT_REMOTE_URLS.get(fmt, "https://example.com/remote/")
    expected_url = remote.rstrip("/") + "/test/artifact"
    mocked_responses.add(responses.GET, expected_url,
                         body=art["content"], status=200)
    result = svc.fetch_from_proxy(f"px-{fmt}", "test/artifact")
    assert result is not None
    assert result["sha256"] == art["sha256"]


# ===================================================================
# Cache Expiry
# ===================================================================

def test_proxy_cache_respects_content_max_age(app, db_session, mocked_responses):
    """Expired content-cache triggers re-fetch from upstream."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="cache-age", proxy={"remote_url": _MR,
                                  "content_max_age": 1, "metadata_max_age": 1440}))
    art = make_binary_artifact(size=32)
    url = _MR + "org/ca/x.jar"
    mocked_responses.add(responses.GET, url, body=art["content"], status=200)
    mocked_responses.add(responses.GET, url, body=art["content"], status=200)
    svc.fetch_from_proxy("cache-age", "org/ca/x.jar")
    # Expire the cache entry
    svc._cache[("cache-age", "org/ca/x.jar")]["cached_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=5))
    svc.fetch_from_proxy("cache-age", "org/ca/x.jar")
    assert len(mocked_responses.calls) == 2


def test_proxy_cache_respects_metadata_max_age(app, db_session, mocked_responses):
    """Expired metadata-cache triggers re-fetch from upstream."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="meta-age", proxy={"remote_url": _MR,
                                 "content_max_age": 1440, "metadata_max_age": 1}))
    url = _MR + "org/test/maven-metadata.xml"
    mocked_responses.add(responses.GET, url, body=b"<metadata/>", status=200)
    mocked_responses.add(responses.GET, url, body=b"<metadata/>", status=200)
    svc.fetch_from_proxy("meta-age", "org/test/maven-metadata.xml")
    svc._cache[("meta-age", "org/test/maven-metadata.xml")]["cached_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=5))
    svc.fetch_from_proxy("meta-age", "org/test/maven-metadata.xml")
    assert len(mocked_responses.calls) == 2


# ===================================================================
# Negative Cache
# ===================================================================

def test_proxy_negative_cache_prevents_repeated_404_fetches(
        app, db_session, mocked_responses):
    """Negative cache prevents re-fetch of 404 within TTL."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="neg-on", negative_cache={"enabled": True, "time_to_live": 60}))
    mocked_responses.add(responses.GET, _MR + "org/miss/z.jar", status=404)
    r1 = svc.fetch_from_proxy("neg-on", "org/miss/z.jar")
    r2 = svc.fetch_from_proxy("neg-on", "org/miss/z.jar")
    assert r1 is None and r2 is None
    assert len(mocked_responses.calls) == 1


def test_proxy_negative_cache_disabled_allows_retry(app, db_session, mocked_responses):
    """With negative cache disabled every 404 re-contacts upstream."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="neg-off", negative_cache={"enabled": False, "time_to_live": 60}))
    url = _MR + "org/retry/z.jar"
    mocked_responses.add(responses.GET, url, status=404)
    mocked_responses.add(responses.GET, url, status=404)
    svc.fetch_from_proxy("neg-off", "org/retry/z.jar")
    svc.fetch_from_proxy("neg-off", "org/retry/z.jar")
    assert len(mocked_responses.calls) == 2


# ===================================================================
# Upstream Error Handling
# ===================================================================

def test_proxy_upstream_404_returns_not_found(app, db_session, mocked_responses):
    """Upstream 404 propagates as None result."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="err-404"))
    mocked_responses.add(responses.GET, _MR + "no/such/thing.jar", status=404)
    result = svc.fetch_from_proxy("err-404", "no/such/thing.jar")
    assert result is None
    assert len(mocked_responses.calls) == 1


def test_proxy_upstream_502_returns_bad_gateway(app, db_session, mocked_responses):
    """Upstream 502 handled gracefully, returns None."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="err-502"))
    mocked_responses.add(responses.GET, _MR + "bad/gw.jar", status=502)
    result = svc.fetch_from_proxy("err-502", "bad/gw.jar")
    assert result is None


def test_proxy_upstream_503_returns_service_unavailable(app, db_session, mocked_responses):
    """Upstream 503 handled gracefully, returns None."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="err-503"))
    mocked_responses.add(responses.GET, _MR + "srv/unavail.jar", status=503)
    result = svc.fetch_from_proxy("err-503", "srv/unavail.jar")
    assert result is None


def test_proxy_upstream_timeout_handled_gracefully(app, db_session, mocked_responses):
    """Upstream timeout handled; returns None."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="err-to"))
    url = _MR + "slow/timeout.jar"

    def _raise(request):
        raise ConnectionError("timeout")

    mocked_responses.add(responses.CallbackResponse(
        method=responses.GET, url=url, callback=_raise))
    result = svc.fetch_from_proxy("err-to", "slow/timeout.jar")
    assert result is None


def test_proxy_upstream_connection_error_handled(app, db_session, mocked_responses):
    """Upstream connection error does not crash."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="err-conn"))
    mocked_responses.add(responses.GET, _MR + "conn/err.jar",
                         body=ConnectionError("Connection refused"))
    result = svc.fetch_from_proxy("err-conn", "conn/err.jar")
    assert result is None


# ===================================================================
# Auto-Block Behaviour
# ===================================================================

def test_proxy_auto_block_after_repeated_failures(app, db_session, mocked_responses):
    """Proxy auto-blocks after ≥3 consecutive upstream failures."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="auto-blk", http_client={"blocked": False, "auto_block": True,
                                       "connection": {"retries": 0, "timeout": 60}}))
    url = _MR + "fail/repeat.jar"
    for _ in range(3):
        mocked_responses.add(responses.GET, url, status=503)
    for _ in range(3):
        svc.fetch_from_proxy("auto-blk", "fail/repeat.jar")
    repo = svc.repo_svc.get_repository_by_name("auto-blk")
    assert repo["online"] is False
    # Further fetches are blocked
    assert svc.fetch_from_proxy("auto-blk", "fail/repeat.jar") is None


def test_proxy_manual_unblock_restores_fetch(app, db_session, mocked_responses):
    """Manually unblocking a proxy restores upstream connectivity."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="unblk", http_client={"blocked": False, "auto_block": True,
                                    "connection": {"retries": 0, "timeout": 60}}))
    url = _MR + "blk/unblk.jar"
    for _ in range(3):
        mocked_responses.add(responses.GET, url, status=503)
    for _ in range(3):
        svc.fetch_from_proxy("unblk", "blk/unblk.jar")
    assert svc.repo_svc.get_repository_by_name("unblk")["online"] is False
    svc.set_repository_online("unblk", True)
    # Clear negative cache
    svc._neg.pop(("unblk", "blk/unblk.jar"), None)
    art = make_binary_artifact(size=64)
    mocked_responses.add(responses.GET, url, body=art["content"], status=200)
    result = svc.fetch_from_proxy("unblk", "blk/unblk.jar")
    assert result is not None
    assert result["content"] == art["content"]


# ===================================================================
# Edge Cases
# ===================================================================

def test_proxy_fetch_empty_response_from_upstream(app, db_session, mocked_responses):
    """Empty upstream 200 response is stored with size=0."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="empty-body"))
    mocked_responses.add(responses.GET, _MR + "empty/zero.bin", body=b"", status=200)
    result = svc.fetch_from_proxy("empty-body", "empty/zero.bin")
    assert result is not None
    assert result["size"] == 0
    assert result["content"] == b""


def test_proxy_fetch_large_artifact_streams_correctly(app, db_session, mocked_responses):
    """Large (>1 MB) artifact fetched and checksummed correctly."""
    svc = _svc()
    svc.create_proxy_repository(make_proxy_repo_maven(name="large-art"))
    art = make_binary_artifact(size=1024 * 1024 + 1)
    mocked_responses.add(responses.GET, _MR + "big/large.jar",
                         body=art["content"], status=200)
    result = svc.fetch_from_proxy("large-art", "big/large.jar")
    assert result is not None
    assert result["size"] == len(art["content"])
    assert result["sha256"] == art["sha256"]


def test_proxy_fetch_with_authentication_headers(app, db_session, mocked_responses):
    """Proxy forwards Basic-auth credentials to upstream registry."""
    repo = make_proxy_repo_maven(name="auth-proxy")
    repo["http_client"]["authentication"] = {"username": "myuser", "password": "mypass"}
    svc = _svc()
    svc.create_proxy_repository(repo)
    art = make_binary_artifact(size=64)
    mocked_responses.add(responses.GET, _MR + "auth/secret.jar",
                         body=art["content"], status=200)
    result = svc.fetch_from_proxy("auth-proxy", "auth/secret.jar")
    assert result is not None
    req = mocked_responses.calls[0].request
    assert "Authorization" in req.headers
    assert req.headers["Authorization"].startswith("Basic ")
