"""
Wind API executor — serializes all WindPy calls through a single thread.

FastMCP is async, but WindPy is synchronous and NOT thread-safe.
All Wind API calls MUST go through this executor to prevent state corruption.

Also provides in-flight request deduplication: if the same query is already
running, subsequent callers await the same Future instead of firing a duplicate.
"""

import asyncio
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Single-thread executor — guarantees WindPy calls are serialized.
#
# The executor is created lazily and recreated automatically after shutdown:
# FastMCP runs the server lifespan once per streamable-http session (not once
# per process), so one client disconnecting can trigger teardown while the
# process keeps serving other sessions. A permanently shut-down executor made
# every later Wind call fail with "cannot schedule new futures after shutdown".
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_executor_running = False

# In-flight dedup: cache_key -> asyncio.Task
_inflight: dict[str, asyncio.Task] = {}
_inflight_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """Return the shared single-thread executor, recreating it after shutdown."""
    global _executor, _executor_running
    with _executor_lock:
        if not _executor_running or _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wind-api")
            _executor_running = True
        return _executor


def _make_dedup_key(func: Callable, args: tuple, kwargs: dict) -> str:
    """Build a dedup key from function name + arguments."""
    raw = json.dumps(
        {"func": getattr(func, "__name__", str(func)), "args": args, "kwargs": kwargs},
        sort_keys=True,
        default=str,
    )
    return hashlib.md5(raw.encode()).hexdigest()


async def run_wind(func: Callable, *args: Any, **kwargs: Any) -> Any:
    """
    Submit a synchronous Wind API call to the single-thread executor.

    Features:
    - Serialized execution (single thread, no concurrent WindPy calls)
    - In-flight dedup (same query reuses the running Future)

    Usage:
        result = await run_wind(session.w.wss, codes, fields, options)
    """
    key = _make_dedup_key(func, args, kwargs)

    with _inflight_lock:
        if key in _inflight:
            task = _inflight[key]
            logger.debug(f"Dedup hit: reusing in-flight request {key[:8]}")
            return await task

        loop = asyncio.get_running_loop()

        async def _execute():
            try:
                result = await loop.run_in_executor(
                    _get_executor(), lambda: func(*args, **kwargs)
                )
                return result
            finally:
                with _inflight_lock:
                    _inflight.pop(key, None)

        task = asyncio.ensure_future(_execute())
        _inflight[key] = task

    return await task


def run_wind_sync(func: Callable, *args: Any, **kwargs: Any) -> Any:
    """
    Synchronous version for use in non-async contexts (e.g., tests).
    Still serializes through the single-thread executor.
    """
    future = _get_executor().submit(func, *args, **kwargs)
    return future.result()


def shutdown_executor() -> None:
    """Shut down the current executor, if one is running.

    Safe to call from per-session teardown and multiple times: the next Wind
    API call transparently creates a fresh executor instead of failing with
    "cannot schedule new futures after shutdown".
    """
    global _executor_running
    with _executor_lock:
        executor = _executor
        _executor_running = False
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)
        logger.info("Wind API executor shut down.")
