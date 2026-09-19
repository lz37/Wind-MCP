"""Tests for the Wind API executor — runs without Wind Terminal.

Regression coverage for the per-session teardown poisoning bug: FastMCP runs
the server lifespan once per streamable-http session, and its teardown used
to shut down the module-global single-thread executor. Every later Wind call
then failed with "cannot schedule new futures after shutdown" until restart.
"""

import asyncio
import os
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mock_wind import MockWindAPI, MockWindData

from wind_mcp.core import executor


class WindPyModule(types.ModuleType):
    w = MockWindAPI()


# Keep the import-time SDK binding stable; individual tests patch session.w.
sys.modules.setdefault("WindPy", WindPyModule("WindPy"))


class StaleWindAPI(MockWindAPI):
    """A stale SDK that lies about connectivity and needs stop/start to heal."""

    def __init__(self):
        super().__init__()
        self.stale = True
        self.allow_recovery = True
        self.fail_restart = False
        self.error_code = -40521010
        self.queries = 0
        self.stops = 0
        self.restarts = 0
        self.sdk_threads = set()
        self._busy = threading.Lock()
        self.block_first_query = False
        self.query_entered = threading.Event()
        self.query_release = threading.Event()

    @contextmanager
    def _operation(self):
        assert self._busy.acquire(blocking=False), "Concurrent Wind SDK operations"
        self.sdk_threads.add(threading.get_ident())
        try:
            yield
        finally:
            self._busy.release()

    def isconnected(self):
        return True

    def stop(self):
        with self._operation():
            self.stops += 1

    def start(self):
        with self._operation():
            if self.stops > self.restarts:
                self.restarts += 1
                if self.fail_restart:
                    return MockWindData(-40520004, ["Login Failed"])
                if self.allow_recovery:
                    self.stale = False
            return MockWindData()

    def wsd(self, *args, **kwargs):
        with self._operation():
            self.queries += 1
            if self.block_first_query and self.queries == 1:
                self.query_entered.set()
                assert self.query_release.wait(2), "Test query was not released"
            if self.stale:
                return MockWindData(
                    self.error_code,
                    [f"CWSDService:: Internet Timeout. query={self.queries}"],
                )
            return MockWindData(
                data=[[1.25]],
                fields=["CLOSE"],
                codes=["510300.SH"],
                times=[datetime(2026, 9, 11, tzinfo=timezone.utc)],
            )


@pytest.fixture(autouse=True)
def wind_sdk(monkeypatch):
    sdk = StaleWindAPI()

    from wind_mcp.core import session
    from wind_mcp.core.cache import get_cache

    executor.shutdown_executor()
    session.WindSession._shutdown()
    monkeypatch.setattr(session, "w", sdk)
    session.WindSession.get()
    sdk.sdk_threads.clear()
    get_cache().clear()
    try:
        yield sdk
    finally:
        sdk.query_release.set()
        executor.shutdown_executor()
        session.WindSession._shutdown()
        get_cache().clear()


def historical_query():
    from wind_mcp.handlers.historical import handle_historical
    from wind_mcp.models.inputs import HistoricalInput

    return handle_historical(
        HistoricalInput(
            codes="510300.SH",
            fields="close",
            begin_date="2026-09-11",
            end_date="2026-09-11",
        )
    )


def test_run_wind_sync_roundtrip():
    assert executor.run_wind_sync(lambda: 42) == 42


def test_executor_self_heals_after_shutdown():
    """After shutdown_executor(), the next call must work on a fresh executor.

    Before the fix, _executor was created once at module import, so a
    per-session teardown permanently disabled every subsequent Wind call.
    """
    assert executor.run_wind_sync(lambda: "before") == "before"
    executor.shutdown_executor()
    assert executor.run_wind_sync(lambda: "after") == "after"


def test_run_wind_async_after_shutdown():
    executor.shutdown_executor()

    async def call():
        return await executor.run_wind(lambda: "async-ok")

    assert asyncio.run(call()) == "async-ok"


def test_shutdown_is_idempotent():
    executor.shutdown_executor()
    executor.shutdown_executor()
    assert executor.run_wind_sync(lambda: "still-ok") == "still-ok"


def test_stale_connected_session_recovers_real_handler_result(wind_sdk):
    from wind_mcp.core.session import WindSession

    assert WindSession.health_check()["connected"] is True
    assert historical_query() == [{"date": "2026-09-11", "close": 1.25}]
    assert wind_sdk.queries == 2
    assert wind_sdk.restarts == 1
    assert wind_sdk.stops == 1
    assert len(wind_sdk.sdk_threads) == 1
    assert threading.get_ident() not in wind_sdk.sdk_threads
    assert WindSession.health_check()["connected"] is True


def test_persistent_timeout_surfaces_last_error_and_invalidates_session(wind_sdk):
    from wind_mcp.core.parser import WindAPIError
    from wind_mcp.core.session import WindSession

    wind_sdk.allow_recovery = False
    with pytest.raises(WindAPIError, match="query=2") as failure:
        historical_query()
    assert failure.value.error_code == -40521010
    assert wind_sdk.queries == 2
    assert wind_sdk.restarts == 1
    assert WindSession.health_check()["connected"] is False

    wind_sdk.allow_recovery = True
    assert historical_query() == [{"date": "2026-09-11", "close": 1.25}]
    assert wind_sdk.queries == 3
    assert wind_sdk.restarts == 2


def test_login_failure_is_not_retried(wind_sdk):
    from wind_mcp.core.parser import WindAPIError

    wind_sdk.error_code = -40520004
    with pytest.raises(WindAPIError) as failure:
        historical_query()
    assert failure.value.error_code == -40520004
    assert wind_sdk.queries == 1
    assert wind_sdk.restarts == 0
    assert wind_sdk.stops == 0


def test_failed_reconnect_preserves_query_error_without_reissuing(wind_sdk):
    from wind_mcp.core.parser import WindAPIError
    from wind_mcp.core.session import WindSession

    wind_sdk.fail_restart = True
    with pytest.raises(WindAPIError, match="query=1") as failure:
        historical_query()
    assert failure.value.error_code == -40521010
    assert wind_sdk.queries == 1
    assert wind_sdk.restarts == 1
    assert WindSession.health_check()["connected"] is False


def test_queued_queries_share_serialized_recovery(wind_sdk):
    from wind_mcp.core.parser import parse_wsd

    wind_sdk.block_first_query = True
    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(executor.run_wind_sync, wind_sdk.wsd)
        try:
            assert wind_sdk.query_entered.wait(2)
            second = callers.submit(executor.run_wind_sync, wind_sdk.wsd)
        finally:
            wind_sdk.query_release.set()
        expected = [{"date": "2026-09-11", "close": 1.25}]
        assert parse_wsd(first.result(timeout=3)) == expected
        assert parse_wsd(second.result(timeout=3)) == expected
    assert wind_sdk.queries == 3
    assert wind_sdk.restarts == 1
    assert len(wind_sdk.sdk_threads) == 1


def test_wedged_call_times_out_and_executor_is_recycled(wind_sdk, monkeypatch):
    """A Wind SDK call that never returns must not starve every later call.

    Regression: run_wind used to bare-await the executor task, so one stuck
    COM call wedged the single-thread executor and the whole server stopped
    answering while the process stayed alive.
    """
    monkeypatch.setenv("WIND_MCP_CALL_TIMEOUT", "0.2")
    release = threading.Event()

    def wedged():
        release.wait(30)
        return "unreachable"

    async def run():
        try:
            with pytest.raises(executor.WindCallTimeoutError):
                await executor.run_wind(wedged)
            return await executor.run_wind(lambda: "recovered")
        finally:
            release.set()

    assert asyncio.run(run()) == "recovered"
    assert wind_sdk.restarts >= 1
