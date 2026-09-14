"""
WindPy session singleton manager.

Usage:
    session = WindSession.get()
    result = session.w.wss("600030.SH", "close,pe_ttm")

Thread safety: All Wind API calls should go through core.executor.run_wind()
to ensure serialized access. The Lock here is a belt-and-suspenders safeguard.
"""

import atexit
import logging
import threading
import time

from WindPy import w

logger = logging.getLogger(__name__)


class WindSession:
    """Singleton wrapper around WindPy connection."""

    _instance: "WindSession | None" = None
    _started: bool = False
    _needs_reconnect: bool = False
    _atexit_registered: bool = False
    _lock = threading.RLock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get(cls) -> "WindSession":
        """Get the WindSession singleton, starting or restoring it as needed."""
        instance = cls()
        with cls._lock:
            if cls._needs_reconnect:
                logger.warning("Wind connection invalidated. Attempting reconnect...")
                cls.reconnect()
            elif not cls._started:
                cls._start()
            elif not cls.is_connected():
                logger.warning("Wind connection lost. Attempting reconnect...")
                cls.reconnect()
        return instance

    @classmethod
    def _start(cls):
        """Start Wind connection while holding the shared lifecycle lock."""
        with cls._lock:
            logger.info("Starting Wind connection...")
            try:
                result = w.start()
            except Exception:
                cls._started = False
                raise
            if result.ErrorCode != 0:
                cls._started = False
                raise ConnectionError(
                    f"Wind start failed with error code {result.ErrorCode}: {result.Data}"
                )
            cls._started = True
            cls._needs_reconnect = False
            if not cls._atexit_registered:
                atexit.register(cls._shutdown)
                cls._atexit_registered = True
            logger.info("Wind connection established.")

    @classmethod
    def reconnect(cls, max_retries: int = 3, base_backoff: float = 1.0):
        """Stop and restart Wind with exponential backoff.

        The shared lock is acquired here as well as by callers, relying on its
        reentrancy when recovery is initiated from a queued Wind call.
        """
        with cls._lock:
            cls._started = False
            cls._needs_reconnect = True
            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(f"Reconnect attempt {attempt}/{max_retries}...")
                    try:
                        w.stop()
                    except Exception:
                        logger.debug("Wind stop failed before reconnect", exc_info=True)
                    result = w.start()
                    if result.ErrorCode == 0:
                        cls._started = True
                        cls._needs_reconnect = False
                        if not cls._atexit_registered:
                            atexit.register(cls._shutdown)
                            cls._atexit_registered = True
                        logger.info("Wind reconnection successful.")
                        return
                    logger.warning(
                        f"Reconnect attempt {attempt} failed: " f"ErrorCode={result.ErrorCode}"
                    )
                except Exception:
                    logger.warning("Reconnect attempt %s failed", attempt, exc_info=True)

                if attempt < max_retries:
                    backoff = base_backoff * (2 ** (attempt - 1))
                    logger.info(f"Waiting {backoff:.1f}s before next attempt...")
                    time.sleep(backoff)

            cls._started = False
            cls._needs_reconnect = True
            raise ConnectionError(f"Wind reconnection failed after {max_retries} attempts")

    @classmethod
    def invalidate(cls):
        """Mark the current SDK session unusable until a real restart succeeds."""
        with cls._lock:
            cls._needs_reconnect = True

    @classmethod
    def health_check(cls) -> dict:
        """Return connection health status without waiting on the query lock."""
        started = cls._started
        if not started or cls._needs_reconnect:
            return {"connected": False, "started": started}
        return {
            "connected": cls.is_connected(),
            "started": started,
        }

    @property
    def w(self):
        """Return the global WindPy `w` object."""
        return w

    @classmethod
    def _shutdown(cls):
        with cls._lock:
            if cls._started or cls._needs_reconnect:
                logger.info("Shutting down Wind connection...")
                try:
                    w.stop()
                except Exception:
                    logger.warning("Error during Wind shutdown", exc_info=True)
            cls._started = False
            cls._needs_reconnect = False

    @classmethod
    def is_connected(cls) -> bool:
        """Check if Wind is currently connected."""
        if not cls._started or cls._needs_reconnect:
            return False
        try:
            return bool(w.isconnected())
        except Exception:
            logger.debug("Wind connectivity check failed", exc_info=True)
            return False
