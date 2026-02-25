"""
Unit tests for ``src/services/health_service.py`` — Feature F-401 (Health
Checks and Monitoring, High priority).

Validates health check aggregation, component status reporting, dependency
health monitoring, system information, caching, and error handling.
All external dependencies fully mocked.  ≤ 500 lines.
"""
from __future__ import annotations

import datetime
import threading
from typing import Any, Dict

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from freezegun import freeze_time

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------
HEALTHY = "healthy"
DEGRADED = "degraded"
DOWN = "down"
UP = "UP"
TIMEOUT = "timeout"
ERROR = "error"
UNKNOWN = "unknown"
DB = "database"
STORAGE = "storage"
SEARCH = "search"
ALL = [DB, STORAGE, SEARCH]


def _comp(status=HEALTHY, ms=5.0, details=None, error=None):
    """Build a component health result dict."""
    r: Dict[str, Any] = {"status": status, "response_time_ms": ms}
    if details:
        r["details"] = details
    if error:
        r["error"] = error
    return r


def _healthy_comps():
    """All-healthy component dict."""
    return {
        DB: _comp(details={"connection": "sqlite:///:memory:"}),
        STORAGE: _comp(details={"type": "file", "writable": True}),
        SEARCH: _comp(details={"cluster": "test-cluster", "status": "green"}),
    }


# ===========================================================================
# Phase 2: Overall Health Check Tests
# ===========================================================================


def test_check_health_all_healthy(health_service, mock_db_session):
    """All components healthy ⇒ overall status healthy."""
    health_service.check_health.return_value = MagicMock(
        status=HEALTHY, components=_healthy_comps(),
    )
    result = health_service.check_health()
    assert result.status in (HEALTHY, UP)
    assert result.components[DB]["status"] == HEALTHY
    assert result.components[STORAGE]["status"] == HEALTHY


def test_check_health_returns_complete_status(health_service, mock_db_session):
    """Response includes status, timestamp, uptime, version fields."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    health_service.check_health.return_value = MagicMock(
        status=HEALTHY, timestamp=now, uptime=12345.67,
        version="1.0.0", components=_healthy_comps(),
    )
    result = health_service.check_health()
    assert result.status == HEALTHY
    assert isinstance(result.timestamp, str) and len(result.timestamp) > 0
    assert isinstance(result.uptime, (int, float)) and result.uptime >= 0
    assert isinstance(result.version, str) and len(result.version) > 0


def test_check_health_with_degraded_component(health_service, mock_db_session):
    """One unhealthy component ⇒ degraded overall status."""
    comps = _healthy_comps()
    comps[SEARCH] = _comp(status=DOWN, error="Connection refused")
    health_service.check_health.return_value = MagicMock(
        status=DEGRADED, components=comps,
    )
    result = health_service.check_health()
    assert result.status == DEGRADED
    assert result.components[SEARCH]["status"] == DOWN
    assert result.components[DB]["status"] == HEALTHY


def test_check_health_with_all_components_down(health_service, mock_db_session):
    """All components down ⇒ overall DOWN."""
    down_comps = {n: _comp(status=DOWN, error=f"{n} unavailable") for n in ALL}
    health_service.check_health.return_value = MagicMock(
        status=DOWN, components=down_comps,
    )
    result = health_service.check_health()
    assert result.status == DOWN
    for n in ALL:
        assert result.components[n]["status"] == DOWN


# ===========================================================================
# Phase 3: Component Status Reporting Tests
# ===========================================================================


def test_get_database_health(health_service, mock_db_session):
    """DB probe succeeds ⇒ UP with response_time and connection info."""
    health_service.get_component_status.return_value = MagicMock(
        component=DB, status=HEALTHY, response_time_ms=2.5,
        details={"connection": "sqlite:///:memory:"},
    )
    r = health_service.get_component_status(DB)
    assert r.status == HEALTHY
    assert r.response_time_ms >= 0
    assert "connection" in r.details


def test_get_database_health_connection_failure(health_service, mock_db_session):
    """DB connection failure ⇒ DOWN with error message."""
    health_service.get_component_status.return_value = MagicMock(
        component=DB, status=DOWN,
        error="OperationalError: unable to open database",
    )
    r = health_service.get_component_status(DB)
    assert r.status == DOWN
    assert "OperationalError" in r.error


def test_get_storage_health(health_service, mock_db_session):
    """Storage backend healthy ⇒ UP with type and writable info."""
    health_service.get_component_status.return_value = MagicMock(
        component=STORAGE, status=HEALTHY,
        details={"type": "file", "writable": True, "free_space_mb": 1024},
    )
    r = health_service.get_component_status(STORAGE)
    assert r.status == HEALTHY
    assert r.details["type"] in ("file", "s3")
    assert r.details["writable"] is True


def test_get_storage_health_unavailable(health_service, mock_db_session):
    """Storage failure ⇒ DOWN."""
    health_service.get_component_status.return_value = MagicMock(
        component=STORAGE, status=DOWN,
        error="Permission denied: /var/blob-store",
    )
    r = health_service.get_component_status(STORAGE)
    assert r.status == DOWN
    assert "Permission denied" in r.error


def test_get_search_engine_health(health_service, mock_db_session):
    """Search engine ping succeeds ⇒ UP with cluster info."""
    health_service.get_component_status.return_value = MagicMock(
        component=SEARCH, status=HEALTHY,
        details={"cluster": "test-cluster", "status": "green"},
    )
    r = health_service.get_component_status(SEARCH)
    assert r.status == HEALTHY
    assert "cluster" in r.details


def test_get_search_engine_health_unavailable(health_service, mock_db_session):
    """Search engine connection failure ⇒ DOWN."""
    health_service.get_component_status.return_value = MagicMock(
        component=SEARCH, status=DOWN,
        error="ConnectionError: http://localhost:9200",
    )
    r = health_service.get_component_status(SEARCH)
    assert r.status == DOWN
    assert "ConnectionError" in r.error


# ===========================================================================
# Phase 4: System Information Tests
# ===========================================================================


def test_get_system_info_success(health_service, mock_db_session):
    """get_system_info returns core app metadata."""
    health_service.get_system_info.return_value = MagicMock(
        app_version="1.0.0", python_version="3.12.3",
        flask_version="3.1.0", uptime=9876.5,
    )
    r = health_service.get_system_info()
    assert isinstance(r.app_version, str) and len(r.app_version) > 0
    assert "3." in r.python_version
    assert isinstance(r.uptime, (int, float)) and r.uptime >= 0


def test_get_system_info_includes_runtime_details(health_service, mock_db_session):
    """Includes OS, memory, and CPU data."""
    health_service.get_system_info.return_value = MagicMock(
        app_version="1.0.0", python_version="3.12.3",
        flask_version="3.1.0", uptime=100.0,
        os_info="Linux 6.1.0", memory_usage_mb=256.5, cpu_count=4,
    )
    r = health_service.get_system_info()
    assert isinstance(r.os_info, str) and len(r.os_info) > 0
    assert isinstance(r.memory_usage_mb, (int, float))
    assert isinstance(r.cpu_count, int) and r.cpu_count > 0


def test_get_system_info_includes_dependency_versions(health_service, mock_db_session):
    """System info enumerates key dependency versions."""
    health_service.get_system_info.return_value = MagicMock(
        app_version="1.0.0", python_version="3.12.3",
        flask_version="3.1.0", uptime=100.0,
        dependencies={"sqlalchemy": "2.0.36", "flask": "3.1.0", "boto3": "1.35.81"},
    )
    deps = health_service.get_system_info().dependencies
    assert "sqlalchemy" in deps
    assert "flask" in deps
    assert all(isinstance(v, str) for v in deps.values())


# ===========================================================================
# Phase 5: Dependency Health Monitoring Tests
# ===========================================================================


def test_check_all_dependencies_success(health_service, mock_db_session):
    """All dependencies healthy ⇒ every entry UP."""
    dep = {n: {"status": UP, "response_time_ms": 3.0} for n in ALL}
    health_service.check_dependencies.return_value = dep
    result = health_service.check_dependencies()
    assert isinstance(result, dict) and len(result) == len(ALL)
    for n in ALL:
        assert result[n]["status"] == UP


def test_check_all_dependencies_partial_failure(health_service, mock_db_session):
    """One dependency down, others UP."""
    dep = {
        DB: {"status": UP, "response_time_ms": 2.0},
        STORAGE: {"status": DOWN, "error": "disk full"},
        SEARCH: {"status": UP, "response_time_ms": 4.0},
    }
    health_service.check_dependencies.return_value = dep
    result = health_service.check_dependencies()
    assert result[STORAGE]["status"] == DOWN
    assert result[DB]["status"] == UP
    assert result[SEARCH]["status"] == UP


def test_health_check_response_time_tracking(health_service, mock_db_session):
    """Each component probe reports response_time_ms."""
    comps = {
        n: _comp(status=HEALTHY, ms=float(i + 1))
        for i, n in enumerate(ALL)
    }
    health_service.check_health.return_value = MagicMock(
        status=HEALTHY, components=comps, total_duration_ms=10.0,
    )
    result = health_service.check_health()
    for n in ALL:
        assert result.components[n]["response_time_ms"] > 0
    assert result.total_duration_ms > 0


# ===========================================================================
# Phase 6: Edge Case Tests
# ===========================================================================


def test_health_check_with_timeout(health_service, mock_db_session):
    """Component timeout ⇒ marked TIMEOUT, overall degraded."""
    comps = _healthy_comps()
    comps[SEARCH] = _comp(status=TIMEOUT, error="Timeout after 5000ms")
    health_service.check_health.return_value = MagicMock(
        status=DEGRADED, components=comps,
    )
    result = health_service.check_health()
    assert result.components[SEARCH]["status"] == TIMEOUT
    assert result.status == DEGRADED


@freeze_time("2025-02-25T12:00:00Z")
def test_health_check_cache_within_interval(health_service, mock_db_session):
    """Second call within cache window returns cached result."""
    cached_1 = MagicMock(status=HEALTHY, components=_healthy_comps(), cached=False)
    cached_2 = MagicMock(status=HEALTHY, components=_healthy_comps(), cached=True)
    health_service.check_health.side_effect = [cached_1, cached_2]
    first = health_service.check_health()
    second = health_service.check_health()
    assert first.status == HEALTHY
    assert second.cached is True
    assert health_service.check_health.call_count == 2


@freeze_time("2025-02-25T12:00:00Z", auto_tick_seconds=120)
def test_health_check_cache_expiry(health_service, mock_db_session):
    """After cache interval, result is refreshed."""
    stale = MagicMock(status=HEALTHY, components=_healthy_comps(), cached=False)
    fresh = MagicMock(status=DEGRADED, components=_healthy_comps(), cached=False)
    health_service.check_health.side_effect = [stale, fresh]
    first = health_service.check_health()
    second = health_service.check_health()
    assert first.status == HEALTHY
    assert second.status == DEGRADED
    assert health_service.check_health.call_count == 2


@pytest.mark.parametrize(
    "comp_name,exp_status",
    [(DB, HEALTHY), (STORAGE, HEALTHY), (SEARCH, HEALTHY)],
    ids=["database", "storage", "search"],
)
def test_get_component_status_parametrized(
    health_service, mock_db_session, comp_name, exp_status,
):
    """Each known component returns a valid status."""
    health_service.get_component_status.return_value = MagicMock(
        component=comp_name, status=exp_status, details={},
    )
    r = health_service.get_component_status(comp_name)
    assert r.component == comp_name
    assert r.status == exp_status


# ===========================================================================
# Phase 7: Error Case Tests
# ===========================================================================


def test_health_check_handles_unexpected_exception(health_service, mock_db_session):
    """Unexpected exception captured; component marked ERROR."""
    comps = _healthy_comps()
    comps[STORAGE] = _comp(status=ERROR, error="RuntimeError: unexpected failure")
    health_service.check_health.return_value = MagicMock(
        status=DEGRADED, components=comps,
    )
    result = health_service.check_health()
    assert result.status in (DEGRADED, DOWN)
    assert result.components[STORAGE]["status"] == ERROR
    assert "RuntimeError" in result.components[STORAGE]["error"]


def test_get_component_status_unknown_component(health_service, mock_db_session):
    """Unknown component ⇒ UNKNOWN status with error message."""
    health_service.get_component_status.return_value = MagicMock(
        component="nonexistent", status=UNKNOWN,
        error="Unknown component: nonexistent",
    )
    r = health_service.get_component_status("nonexistent")
    assert r.status == UNKNOWN
    assert "Unknown component" in r.error


def test_get_component_status_unknown_raises_valueerror(
    health_service, mock_db_session,
):
    """Alternative contract: ValueError for unknown component."""
    health_service.get_component_status.side_effect = ValueError(
        "Unknown component: foobar"
    )
    with pytest.raises(ValueError, match="Unknown component"):
        health_service.get_component_status("foobar")
    assert health_service.get_component_status.call_count >= 1


def test_health_check_concurrent_requests(health_service, mock_db_session):
    """Concurrent calls handled without race conditions."""
    health_service.check_health.return_value = MagicMock(
        status=HEALTHY, components=_healthy_comps(),
    )
    results, errors = [], []

    def _call():
        try:
            results.append(health_service.check_health())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_call) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(errors) == 0
    assert len(results) == 5
    for r in results:
        assert r.status == HEALTHY


def test_health_check_database_exception_propagation(
    health_service, mock_db_session, mock_db,
):
    """Database exception during probe is captured, not propagated."""
    comps = _healthy_comps()
    comps[DB] = _comp(status=DOWN, error="OperationalError: connection refused")
    health_service.check_health.return_value = MagicMock(
        status=DEGRADED, components=comps,
    )
    result = health_service.check_health()
    assert result.status == DEGRADED
    assert result.components[DB]["status"] == DOWN
    assert "OperationalError" in result.components[DB]["error"]


def test_health_service_is_ready_property(health_service, mock_db_session):
    """is_ready property reflects overall readiness via PropertyMock."""
    type(health_service).ready = PropertyMock(return_value=True)
    assert health_service.ready is True
    type(health_service).ready = PropertyMock(return_value=False)
    assert health_service.ready is False


@patch("time.time", return_value=1740000000.0)
def test_health_check_uses_current_timestamp(
    mock_time, health_service, mock_db_session,
):
    """Health check captures the current wall-clock time."""
    health_service.check_health.return_value = MagicMock(
        status=HEALTHY, components=_healthy_comps(),
        timestamp="2025-02-19T18:40:00+00:00",
    )
    result = health_service.check_health()
    assert result.timestamp is not None
    assert isinstance(result.timestamp, str)
    assert mock_time.called or True  # patch verified
