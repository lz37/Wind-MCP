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
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ParamSpec, TypeVar

from .resilience import wind_call_with_resilience

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


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
_inflight: dict[str, asyncio.Task[Any]] = {}
_inflight_lock = threading.Lock()


class WindCallTimeoutError(TimeoutError):
    """A Wind SDK call exceeded the per-call timeout and was abandoned."""


_DEFAULT_CALL_TIMEOUT = 600.0


def _call_timeout() -> float:
    raw = os.environ.get("WIND_MCP_CALL_TIMEOUT", "")
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_CALL_TIMEOUT
    return value if value > 0 else _DEFAULT_CALL_TIMEOUT


def _recycle_executor() -> None:
    """Replace the shared executor without waiting on a wedged worker thread.

    Python cannot kill a thread stuck inside a Wind SDK (COM) call.  The only
    way to keep the service responsive is to abandon the old executor — its
    single worker stays wedged as a leaked thread — and let the next request
    run on a fresh one.
    """
    global _executor, _executor_running
    with _executor_lock:
        old = _executor
        _executor = None
        _executor_running = False
    if old is not None:
        old.shutdown(wait=False)
        logger.warning("Abandoned wedged Wind executor; next call starts a fresh one")

def _get_executor() -> ThreadPoolExecutor:
    """Return the shared single-thread executor, recreating it after shutdown."""
    global _executor, _executor_running
    with _executor_lock:
        if not _executor_running or _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wind-api")
            _executor_running = True
        return _executor


def _make_dedup_key(
    func: Callable[..., object], args: tuple[object, ...], kwargs: dict[str, object]
) -> str:
    """Build a dedup key from function name + arguments."""
    raw = json.dumps(
        {"func": getattr(func, "__name__", str(func)), "args": args, "kwargs": kwargs},
        sort_keys=True,
        default=str,
    )
    return hashlib.md5(raw.encode()).hexdigest()


async def run_wind(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """
    Submit a synchronous Wind API call to the single-thread executor.

    Features:
    - Serialized execution (single thread, no concurrent WindPy calls)
    - In-flight dedup (same query reuses the running Future)
    - Per-call timeout (WIND_MCP_CALL_TIMEOUT, default 600s): on timeout the
      wedged executor is abandoned and the session invalidated, so one stuck
      COM call cannot starve every later request forever.

    Usage:
        result = await run_wind(session.w.wss, codes, fields, options)
    """
    key = _make_dedup_key(func, args, kwargs)

    with _inflight_lock:
        if key in _inflight:
            task = _inflight[key]
            logger.debug(f"Dedup hit: reusing in-flight request {key[:8]}")
            return await _await_with_timeout(key, task)

        loop = asyncio.get_running_loop()

        async def _execute() -> R:
            try:
                result = await loop.run_in_executor(
                    _get_executor(),
                    lambda: wind_call_with_resilience(func, *args, **kwargs),
                )
                return result
            finally:
                with _inflight_lock:
                    _inflight.pop(key, None)

        task = asyncio.ensure_future(_execute())
        _inflight[key] = task

    return await _await_with_timeout(key, task)


async def _await_with_timeout(key: str, task: asyncio.Task[Any]) -> Any:
    """Await an in-flight Wind task with a bounded timeout.

    The underlying task is shielded: a caller timing out must not cancel the
    executor work (the stuck thread cannot be cancelled anyway).  On timeout
    the task is evicted from the dedup map so the next identical request is
    resubmitted to a fresh executor instead of waiting on the wedged one.
    """
    try:
        return await asyncio.wait_for(asyncio.shield(task), _call_timeout())
    except TimeoutError:
        with _inflight_lock:
            _inflight.pop(key, None)
        _recycle_executor()
        # Invalidate the session so the next call rebuilds the Wind connection
        # on the fresh executor instead of reusing a possibly poisoned one.
        from .session import WindSession

        WindSession.invalidate()
        raise WindCallTimeoutError(
            f"Wind call timed out after {_call_timeout()}s; executor recycled, "
            "session invalidated"
        ) from None


def run_wind_sync(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """
    Synchronous version for use in non-async contexts (e.g., tests).
    Still serializes through the single-thread executor.
    """
    future = _get_executor().submit(wind_call_with_resilience, func, *args, **kwargs)
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
