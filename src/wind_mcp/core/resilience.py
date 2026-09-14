"""Bounded recovery for WindPy calls."""

import logging
from collections.abc import Callable
from typing import ParamSpec, TypeVar

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

_TRANSPORT_ERROR = -40521010


def _is_transport_error(result: object) -> bool:
    """Return whether a raw WindData response is the observed transport error."""
    return getattr(result, "ErrorCode", None) == _TRANSPORT_ERROR


def wind_call_with_resilience(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run one Wind call, recovering the observed stale-session error once.

    The callback and any recovery run inline on the caller's Wind executor
    worker.  The raw WindData response is returned unchanged so the existing
    parser remains responsible for raising ``WindAPIError``.
    """
    # Keep WindPy out of executor import-time dependencies.  WindSession.get()
    # below is deliberately inside the shared lock so readiness is checked
    # again after a queued call has waited behind an invalidation.
    from .session import WindSession

    with WindSession._lock:
        WindSession.get()
        result = func(*args, **kwargs)
        if not _is_transport_error(result):
            return result

        WindSession.invalidate()
        try:
            WindSession.reconnect(max_retries=1)
        except ConnectionError:
            logger.warning(
                "Wind reconnect failed after transport error; preserving raw "
                "response with ErrorCode=%s",
                _TRANSPORT_ERROR,
                exc_info=True,
            )
            return result

        retry_result = func(*args, **kwargs)
        if _is_transport_error(retry_result):
            WindSession.invalidate()
        return retry_result
