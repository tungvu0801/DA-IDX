"""
data/events/cache.py — Generic in-memory TTL cache for event-provider
fetches (FOMC calendar page, FRED release dates, Alpaca corporate
actions), so a slow-changing external calendar isn't re-fetched on every
request. Mirrors the pattern already used by services/analysis_cache.py.

Failures are never cached: a provider only calls `.set()` after a
successful fetch, so a failed fetch simply retries on the next call
rather than being "stuck" unavailable for the TTL window.
"""
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


@dataclass
class _CacheEntry:
    value: Any
    expires_at: datetime


class TTLCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: Dict[str, _CacheEntry] = {}

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if datetime.now(timezone.utc) >= entry.expires_at:
                del self._store[key]
                return None
            return entry.value

    def set(self, key: str, value: Any, ttl_minutes: float) -> None:
        with self._lock:
            self._store[key] = _CacheEntry(
                value=value, expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
            )


event_cache = TTLCache()
