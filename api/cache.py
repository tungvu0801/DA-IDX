"""
api/cache.py — Minimal in-memory TTL cache so the web dashboard doesn't
re-hit Alpaca on every browser poll.

This reuses the *meaning* of config.MARKET_SCAN_INTERVAL /
config.WATCHLIST_SCAN_INTERVAL from the CLI — the same intervals just get
enforced as a cache TTL here instead of a sleep loop.
"""
import time
from typing import Any, Callable, Dict, Tuple


class TTLCache:
    def __init__(self) -> None:
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get_or_set(self, key: str, ttl_seconds: int, factory: Callable[[], Any]) -> Any:
        now = time.time()
        cached = self._store.get(key)
        if cached is not None and now - cached[0] < ttl_seconds:
            return cached[1]
        value = factory()
        self._store[key] = (now, value)
        return value

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


cache = TTLCache()
