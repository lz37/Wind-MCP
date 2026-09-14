"""Tests for resilience layer — timeout, retry, stale fallback."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from wind_mcp.core.cache import CacheEntry, WindCache

# --- Stale cache fallback ---


def test_stale_get_returns_expired_data():
    cache = WindCache()
    key = cache._make_key("func", "arg1")
    cache._store[key] = CacheEntry(data={"result": 42}, timestamp=time.time() - 10000, ttl=1)
    # Stale get should return data even though expired (called before get which deletes)
    stale = cache.stale_get("func", "arg1")
    assert stale == {"result": 42}
    # Normal get should miss (expired) and delete the entry
    assert cache.get("func", "arg1") is None
    # After deletion, stale_get also returns None
    assert cache.stale_get("func", "arg1") is None


def test_stale_get_returns_none_if_absent():
    cache = WindCache()
    assert cache.stale_get("nonexistent", "arg") is None


# --- Cache LRU eviction ---


def test_cache_eviction_on_maxsize():
    cache = WindCache(maxsize=10)
    for i in range(15):
        cache.set("func", f"data_{i}", "snapshot", f"arg_{i}")
    # Should have evicted to stay at/below maxsize
    assert len(cache._store) <= 10
    assert cache._evictions > 0


def test_cache_eviction_preserves_recent():
    cache = WindCache(maxsize=5)
    # Insert 5 entries
    for i in range(5):
        cache.set("func", f"data_{i}", "snapshot", f"arg_{i}")
        time.sleep(0.01)

    # Access entry 0 to make it recently used
    cache.get("func", "arg_0")

    # Insert 3 more to trigger eviction
    for i in range(5, 8):
        cache.set("func", f"data_{i}", "snapshot", f"arg_{i}")

    # Entry 0 should survive (recently accessed)
    assert cache.get("func", "arg_0") == "data_0"


# --- Cache stats ---


def test_cache_stats_include_evictions():
    cache = WindCache(maxsize=5)
    for i in range(10):
        cache.set("func", f"data_{i}", "snapshot", f"arg_{i}")
    stats = cache.stats()
    assert "evictions" in stats
    assert stats["evictions"] > 0
    assert stats["maxsize"] == 5
