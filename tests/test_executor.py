"""Tests for the Wind API executor — runs without Wind Terminal.

Regression coverage for the per-session teardown poisoning bug: FastMCP runs
the server lifespan once per streamable-http session, and its teardown used
to shut down the module-global single-thread executor. Every later Wind call
then failed with "cannot schedule new futures after shutdown" until restart.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wind_mcp.core import executor


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
