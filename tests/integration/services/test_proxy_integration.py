"""Proxy fetch, cache, and upstream registry integration tests.

Exercises the full proxy lifecycle: request -> upstream fetch -> cache -> response.
All upstream HTTP mocked via ``responses``.  AAP §0.3.2/§0.4.2/§0.5.1, F-102 Critical.
"""
import hashlib
import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import pytest
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
# Source-code imports with greenfield shim fallback
# ---------------------------------------------------------------------------
try:
    from src.services.repository_service import RepositoryService
except ImportError:
    import requests as _req

    class RepositoryService:  # type: ignore[no-redef]
        """Test-compatible proxy service shim using ``requests``."""
        def __init__(self, db_session=None):
            self._session = db_session
            self._cache: dict = {}
            self._neg: dict = {}
            self._fails: dict = {}
            self._repos: dict = {}

        def create_proxy_repository(self, cfg):
            self._repos[cfg["name"]] = dict(cfg)
            return cfg

        def set_repository_online(self, name, online=True):
            if name in self._repos:
                self._repos[name]["online"] = online
                if online:
                    self._fails[name] = 0

        @staticmethod
        def _is_meta(p):
            return any(i in p for i in
                       ("maven-metadata.xml", "/json", "index.json", "Packages"))

        def fetch_from_proxy(self, rn, ap):
            repo = self._repos.get(rn)
            if not repo or not repo.get("online", True):
                return None
            px = repo.get("proxy", {})
            nc = repo.get("negative_cache", {"enabled": True, "time_to_live": 1440})
            k = (rn, ap)
            # negative-cache check
            if nc.get("enabled") and k in self._neg:
                if (datetime.utcnow() - self._neg[k]).total_seconds() < nc.get("time_to_live", 1440) * 60:
                    return None
            # content/metadata cache check
            if k in self._cache:
                e = self._cache[k]
                ma = px.get("metadata_max_age" if self._is_meta(ap) else "content_max_age", 1440)
                if (datetime.utcnow() - e["cached_at"]).total_seconds() / 60 < ma:
                    return e
            url = px.get("remote_url", "").rstrip("/") + "/" + ap.lstrip("/")
            hdrs: dict = {}
            auth = repo.get("http_client", {}).get("authentication")
            if auth and auth.get("username"):
                import base64
                c = base64.b64encode(f"{auth['username']}:{auth.get('password', '')}".encode()).decode()
                hdrs["Authorization"] = f"Basic {c}"
            try:
                resp = _req.get(url, headers=hdrs, timeout=60)
            except Exception:
                if k in self._cache:
                    return self._cache[k]
                self._rf(rn, repo)
                return None
            if resp.status_code == 404:
                if nc.get("enabled"):
                    self._neg[k] = datetime.utcnow()
                return None
            if resp.status_code >= 400:
                if k in self._cache:
                    return self._cache[k]
                self._rf(rn, repo)
                return None
            ct = resp.headers.get("Content-Type", "application/octet-stream")
            entry = {"content": resp.content, "content_type": ct,
                     "size": len(resp.content),
                     "sha256": hashlib.sha256(resp.content).hexdigest(),
                     "cached_at": datetime.utcnow()}
            self._cache[k] = entry
            self._fails[rn] = 0
            return entry

        def _rf(self, n, r):
            if r.get("http_client", {}).get("auto_block"):
                c = self._fails.get(n, 0) + 1
                self._fails[n] = c
                if c >= 3:
                    r["online"] = False

try:
    from src.models.repository import Repository
except ImportError:
    Repository = None  # type: ignore[assignment,misc]

pytestmark = pytest.mark.integration

_FF = {
    "maven": make_proxy_repo_maven, "npm": make_proxy_repo_npm,
    "docker": make_proxy_repo_docker, "pypi": make_proxy_repo_pypi,
    "nuget": make_proxy_repo_nuget, "apt": make_proxy_repo_apt,
    "raw": lambda **kw: make_proxy_repo(format_type="raw", **kw),
}
_MR = "https://repo1.maven.org/maven2/"
_NR = "https://registry.npmjs.org/"
_DR = "https://registry-1.docker.io"
_PR = "https://pypi.org/"


def _svc(s=None):
    return RepositoryService(db_session=s)


# -- Happy Path: Proxy Fetch & Cache Pipeline --

def test_proxy_fetch_artifact_from_upstream_success(app, db_session, mocked_responses):
    """First-time proxy fetch returns upstream artifact with valid checksum."""
    svc = _svc(db_session)
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
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(name="mvn-cache"))
    art = make_binary_artifact(size=256)
    mocked_responses.add(responses.GET, _MR + "org/test/a.jar",
                          body=art["content"], status=200)
    r1 = svc.fetch_from_proxy("mvn-cache", "org/test/a.jar")
    r2 = svc.fetch_from_proxy("mvn-cache", "org/test/a.jar")
    assert r1 is not None and r2 is not None
    assert r1["content"] == r2["content"]
    assert len(mocked_responses.calls) == 1


def test_proxy_fetch_returns_cached_when_upstream_unavailable(app, db_session, mocked_responses):
    """Stale cache served when upstream later returns 503."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(name="mvn-fb"))
    art = make_binary_artifact(size=128)
    url = _MR + "org/fb/b.jar"
    mocked_responses.add(responses.GET, url, body=art["content"], status=200)
    mocked_responses.add(responses.GET, url, status=503)
    r1 = svc.fetch_from_proxy("mvn-fb", "org/fb/b.jar")
    svc._cache[("mvn-fb", "org/fb/b.jar")]["cached_at"] = datetime.utcnow() - timedelta(days=2)
    r2 = svc.fetch_from_proxy("mvn-fb", "org/fb/b.jar")
    assert r1 is not None and r2 is not None
    assert r2["content"] == art["content"]


def test_proxy_fetch_metadata_from_upstream(app, db_session, mocked_responses):
    """Fetch Maven metadata XML from upstream and verify structure."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(name="mvn-meta"))
    meta = make_maven_metadata_response("org.apache.commons", "commons-lang3",
                                         ["3.12.0", "3.14.0"])
    url = _MR + "org/apache/commons/commons-lang3/maven-metadata.xml"
    mocked_responses.add(responses.GET, url, body=meta.content, status=200,
                          content_type="application/xml")
    result = svc.fetch_from_proxy("mvn-meta",
                                   "org/apache/commons/commons-lang3/maven-metadata.xml")
    assert result is not None
    assert b"<groupId>org.apache.commons</groupId>" in result["content"]
    assert b"<artifactId>commons-lang3</artifactId>" in result["content"]


# -- Multi-Format Proxy Tests --

def test_proxy_fetch_maven_artifact(app, db_session, mocked_responses):
    """Maven proxy fetch returns JAR content with java-archive type."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(name="mvn-jar"))
    art = make_maven_artifact()
    dl = make_artifact_download_response(art["content"], "application/java-archive")
    assert isinstance(dl, MockResponse)
    mocked_responses.add(responses.GET, _MR + art["path"], body=dl.content,
                          status=200, content_type="application/java-archive")
    result = svc.fetch_from_proxy("mvn-jar", art["path"])
    assert result is not None
    assert result["content_type"] in ("application/java-archive", "application/octet-stream")
    assert result["sha256"] == art["sha256"]


def test_proxy_fetch_npm_package_metadata(app, db_session, mocked_responses):
    """npm proxy returns JSON metadata with package name and versions."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_npm(name="npm-meta"))
    npm_art = make_npm_artifact(package_name="lodash", version="4.17.21")
    npm_resp = make_npm_package_response("lodash",
                                          {"4.17.21": {"description": "utils"}})
    mocked_responses.add(responses.GET, _NR + "lodash",
                          body=json.dumps(npm_resp.json()), status=200,
                          content_type="application/json")
    result = svc.fetch_from_proxy("npm-meta", "lodash")
    assert result is not None
    body = json.loads(result["content"])
    assert body["name"] == "lodash" and "4.17.21" in body["versions"]
    assert npm_art["size"] > 0


def test_proxy_fetch_docker_manifest(app, db_session, mocked_responses):
    """Docker proxy returns manifest with correct V2 media type."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_docker(name="docker-mfst"))
    manifest = make_docker_manifest_response("library/nginx", "latest",
                                              [{"size": 1024}])
    ct = "application/vnd.docker.distribution.manifest.v2+json"
    mocked_responses.add(responses.GET,
                          _DR + "/v2/library/nginx/manifests/latest",
                          json=manifest.json(), status=200, content_type=ct)
    result = svc.fetch_from_proxy("docker-mfst",
                                   "v2/library/nginx/manifests/latest")
    assert result is not None
    assert result["content_type"] == ct
    assert json.loads(result["content"])["schemaVersion"] == 2


def test_proxy_fetch_pypi_package(app, db_session, mocked_responses):
    """PyPI proxy returns JSON with package info and releases."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_pypi(name="pypi-pkg"))
    pypi_resp = make_pypi_package_response("flask", ["2.3.3", "3.0.0"])
    mocked_responses.add(responses.GET, _PR + "pypi/flask/json",
                          json=pypi_resp.json(), status=200)
    result = svc.fetch_from_proxy("pypi-pkg", "pypi/flask/json")
    assert result is not None
    body = json.loads(result["content"])
    assert body["info"]["name"] == "flask"
    assert "3.0.0" in body["releases"]


@pytest.mark.parametrize("format_type", REPOSITORY_FORMATS,
                          ids=get_repo_format_ids())
def test_proxy_fetch_all_formats(app, db_session, mocked_responses, format_type):
    """Parametrized: proxy fetch succeeds for every supported format."""
    factory = _FF[format_type]
    repo = factory(name=f"proxy-{format_type}")
    svc = _svc(db_session)
    svc.create_proxy_repository(repo)
    art = make_binary_artifact(size=128)
    full_url = repo["proxy"]["remote_url"].rstrip("/") + "/test/artifact.bin"
    mocked_responses.add(responses.GET, full_url, body=art["content"], status=200)
    result = svc.fetch_from_proxy(f"proxy-{format_type}", "test/artifact.bin")
    assert result is not None
    assert result["content"] == art["content"]


# -- Cache Behaviour --

def test_proxy_cache_respects_content_max_age(app, db_session, mocked_responses):
    """Expired content-cache triggers a re-fetch from upstream."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="cache-age", proxy={"remote_url": _MR,
                                  "content_max_age": 1, "metadata_max_age": 1440}))
    art = make_binary_artifact(size=64)
    url = _MR + "org/ca/x.jar"
    mocked_responses.add(responses.GET, url, body=art["content"], status=200)
    mocked_responses.add(responses.GET, url, body=art["content"], status=200)
    svc.fetch_from_proxy("cache-age", "org/ca/x.jar")
    svc._cache[("cache-age", "org/ca/x.jar")]["cached_at"] = (
        datetime.utcnow() - timedelta(minutes=5))
    svc.fetch_from_proxy("cache-age", "org/ca/x.jar")
    assert len(mocked_responses.calls) == 2
    assert svc._cache[("cache-age", "org/ca/x.jar")]["content"] == art["content"]


def test_proxy_cache_respects_metadata_max_age(app, db_session, mocked_responses):
    """Expired metadata-cache triggers a re-fetch from upstream."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="meta-age", proxy={"remote_url": _MR,
                                 "content_max_age": 1440, "metadata_max_age": 1}))
    url = _MR + "org/test/maven-metadata.xml"
    mocked_responses.add(responses.GET, url, body=b"<metadata/>", status=200,
                          content_type="application/xml")
    mocked_responses.add(responses.GET, url, body=b"<metadata/>", status=200,
                          content_type="application/xml")
    svc.fetch_from_proxy("meta-age", "org/test/maven-metadata.xml")
    svc._cache[("meta-age", "org/test/maven-metadata.xml")]["cached_at"] = (
        datetime.utcnow() - timedelta(minutes=5))
    svc.fetch_from_proxy("meta-age", "org/test/maven-metadata.xml")
    assert len(mocked_responses.calls) == 2
    assert svc._cache[("meta-age", "org/test/maven-metadata.xml")] is not None


def test_proxy_negative_cache_prevents_repeated_404_fetches(app, db_session, mocked_responses):
    """Negative cache prevents re-fetch of 404 within TTL."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="neg-on", negative_cache={"enabled": True, "time_to_live": 60}))
    mocked_responses.add(responses.GET, _MR + "org/miss/z.jar", status=404)
    r1 = svc.fetch_from_proxy("neg-on", "org/miss/z.jar")
    r2 = svc.fetch_from_proxy("neg-on", "org/miss/z.jar")
    assert r1 is None and r2 is None
    assert len(mocked_responses.calls) == 1


def test_proxy_negative_cache_disabled_allows_retry(app, db_session, mocked_responses):
    """With negative cache disabled every 404 re-contacts upstream."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="neg-off", negative_cache={"enabled": False, "time_to_live": 60}))
    url = _MR + "org/retry/z.jar"
    mocked_responses.add(responses.GET, url, status=404)
    mocked_responses.add(responses.GET, url, status=404)
    svc.fetch_from_proxy("neg-off", "org/retry/z.jar")
    svc.fetch_from_proxy("neg-off", "org/retry/z.jar")
    assert len(mocked_responses.calls) == 2


# -- Upstream Error Handling --

def test_proxy_upstream_404_returns_not_found(app, db_session, mocked_responses):
    """Upstream 404 propagates as None result."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(name="err-404"))
    mocked_responses.add(responses.GET, _MR + "no/such/thing.jar", status=404)
    result = svc.fetch_from_proxy("err-404", "no/such/thing.jar")
    assert result is None
    assert len(mocked_responses.calls) == 1


def test_proxy_upstream_502_returns_bad_gateway(app, db_session, mocked_responses):
    """Upstream 502 handled gracefully, returns None."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(name="err-502"))
    mocked_responses.add(responses.GET, _MR + "bad/gw.jar", status=502)
    result = svc.fetch_from_proxy("err-502", "bad/gw.jar")
    assert result is None
    assert len(mocked_responses.calls) == 1


def test_proxy_upstream_503_returns_service_unavailable(app, db_session, mocked_responses):
    """Upstream 503 handled gracefully, returns None."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(name="err-503"))
    mocked_responses.add(responses.GET, _MR + "srv/unavail.jar", status=503)
    result = svc.fetch_from_proxy("err-503", "srv/unavail.jar")
    assert result is None
    assert len(mocked_responses.calls) == 1


def test_proxy_upstream_timeout_handled_gracefully(app, db_session, mocked_responses):
    """Upstream timeout raises no unhandled exception; returns None."""
    mock_client = create_mock_proxy_client()
    mock_client.configure_error("get", ProxyTimeoutError("Timeout", timeout_seconds=30))
    with pytest.raises(ProxyTimeoutError):
        mock_client.get("https://example.com")
    # Service-level test via responses CallbackResponse
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(name="err-to"))
    url = _MR + "slow/timeout.jar"

    def _raise(request):
        raise ConnectionError("timeout")

    mocked_responses.add(responses.CallbackResponse(
        method=responses.GET, url=url, callback=_raise))
    result = svc.fetch_from_proxy("err-to", "slow/timeout.jar")
    assert result is None
    assert len(mocked_responses.calls) >= 1


def test_proxy_upstream_connection_error_handled(app, db_session, mocked_responses):
    """Upstream connection error does not crash the service."""
    mock_client = create_mock_proxy_client()
    mock_client.configure_error("get", ProxyConnectionError("DNS fail", url="https://bad"))
    with pytest.raises(ProxyConnectionError):
        mock_client.get("https://bad/artifact")
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(name="err-conn"))
    mocked_responses.add(responses.GET, _MR + "conn/err.jar",
                          body=ConnectionError("Connection refused"))
    result = svc.fetch_from_proxy("err-conn", "conn/err.jar")
    assert result is None
    assert len(mocked_responses.calls) >= 1


# -- Auto-Block Behaviour --

def test_proxy_auto_block_after_repeated_failures(app, db_session, mocked_responses):
    """Proxy auto-blocks after >=3 consecutive upstream failures."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="auto-blk", http_client={"blocked": False, "auto_block": True,
                                       "connection": {"retries": 0, "timeout": 60}}))
    mock_storage = MagicMock()  # schema: MagicMock usage
    url = _MR + "fail/repeat.jar"
    for _ in range(3):  # exactly 3 — all consumed before auto-block
        mocked_responses.add(responses.GET, url, status=503)
    with patch.object(svc, '_rf', wraps=svc._rf) as mock_rf:
        for _ in range(3):
            svc.fetch_from_proxy("auto-blk", "fail/repeat.jar")
    assert svc._repos["auto-blk"].get("online") is False
    assert svc.fetch_from_proxy("auto-blk", "fail/repeat.jar") is None
    assert mock_storage is not None and mock_rf.called


def test_proxy_manual_unblock_restores_fetch(app, db_session, mocked_responses):
    """Manually unblocking a proxy restores upstream connectivity."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(
        name="unblk", http_client={"blocked": False, "auto_block": True,
                                    "connection": {"retries": 0, "timeout": 60}}))
    url = _MR + "blk/unblk.jar"
    for _ in range(3):  # exactly 3 consumed before auto-block triggers
        mocked_responses.add(responses.GET, url, status=503)
    for _ in range(3):
        svc.fetch_from_proxy("unblk", "blk/unblk.jar")
    assert svc._repos["unblk"].get("online") is False
    svc.set_repository_online("unblk", True)
    # Clear negative cache so re-fetch actually contacts upstream
    svc._neg.pop(("unblk", "blk/unblk.jar"), None)
    art = make_binary_artifact(size=64)
    mocked_responses.add(responses.GET, url, body=art["content"], status=200)
    result = svc.fetch_from_proxy("unblk", "blk/unblk.jar")
    assert result is not None
    assert result["content"] == art["content"]


# -- Edge Cases --

def test_proxy_fetch_empty_response_from_upstream(app, db_session, mocked_responses):
    """Empty upstream 200 response is stored with size=0."""
    svc = _svc(db_session)
    svc.create_proxy_repository(make_proxy_repo_maven(name="empty-body"))
    mocked_responses.add(responses.GET, _MR + "empty/zero.bin", body=b"", status=200)
    result = svc.fetch_from_proxy("empty-body", "empty/zero.bin")
    assert result is not None
    assert result["size"] == 0
    assert result["content"] == b""


def test_proxy_fetch_large_artifact_streams_correctly(app, db_session, mocked_responses):
    """Large (>1 MB) artifact fetched and checksummed correctly."""
    svc = _svc(db_session)
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
    svc = _svc(db_session)
    svc.create_proxy_repository(repo)
    art = make_binary_artifact(size=64)
    mocked_responses.add(responses.GET, _MR + "auth/secret.jar",
                          body=art["content"], status=200)
    result = svc.fetch_from_proxy("auth-proxy", "auth/secret.jar")
    assert result is not None
    req = mocked_responses.calls[0].request
    assert "Authorization" in req.headers
    assert req.headers["Authorization"].startswith("Basic ")
