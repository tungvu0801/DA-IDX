"""
services/analysis_cache.py — Short-lived in-memory cache of already-
generated research bundles, keyed by a generated `analysis_id`.

Why this exists: when a user clicks "Save Research Snapshot" on a page
that's already showing a beginner research view, the snapshot must record
EXACTLY what they saw — not a freshly re-generated analysis, which could
differ from what's on screen (different Claude output, a moved price,
etc.) and would defeat the entire point of "what the agent believed at
this moment." GET /api/stocks/{symbol}/research stores its result here and
returns the analysis_id; POST /api/research/snapshots looks it up and
persists it verbatim, making zero new Claude calls.

Entries expire after config.ANALYSIS_CACHE_TTL_MINUTES — long enough for a
user to read the page and decide to save, short enough that this never
turns into an unbounded memory leak on a long-running process.
"""
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import config
from agents.beginner_agent import BeginnerResearch
from analysis.research_setup import ResearchSetup


@dataclass
class CachedBundle:
    symbol: str
    bundle: BeginnerResearch
    setup: Optional[ResearchSetup]
    generated_at: datetime


class AnalysisBundleCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: Dict[str, CachedBundle] = {}

    def store(self, symbol: str, bundle: BeginnerResearch, setup: Optional[ResearchSetup]) -> str:
        analysis_id = uuid.uuid4().hex
        entry = CachedBundle(symbol=symbol, bundle=bundle, setup=setup, generated_at=datetime.now(timezone.utc))
        with self._lock:
            self._store[analysis_id] = entry
            self._evict_expired_locked()
        return analysis_id

    def get(self, analysis_id: str) -> Optional[CachedBundle]:
        with self._lock:
            entry = self._store.get(analysis_id)
            if entry is None:
                return None
            if datetime.now(timezone.utc) - entry.generated_at > timedelta(minutes=config.ANALYSIS_CACHE_TTL_MINUTES):
                del self._store[analysis_id]
                return None
            return entry

    def _evict_expired_locked(self) -> None:
        """Called with self._lock already held. Keeps the cache from growing unbounded on a long-running process."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.ANALYSIS_CACHE_TTL_MINUTES)
        expired = [k for k, v in self._store.items() if v.generated_at < cutoff]
        for k in expired:
            del self._store[k]


analysis_cache = AnalysisBundleCache()
