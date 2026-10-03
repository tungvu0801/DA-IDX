"""
agents/ai_cache.py — Caches AI-agent results per (symbol, analysis_type) so
a stock isn't re-analyzed by Claude when nothing meaningful has changed.
`analysis_type` is e.g. "technical", "catalyst", or "risk" — each agent
gets its own cache slot per symbol so they never collide or overwrite
each other.

A cached entry is considered stale (and gets replaced by a fresh call)
when any of:
  - it's older than config.AI_ANALYSIS_CACHE_MINUTES
  - price moved by >= config.AI_REANALYZE_PRICE_CHANGE_PERCENT since analysis
  - Attention Score moved by >= config.AI_REANALYZE_ATTENTION_CHANGE since analysis
  - a newer news item exists than the one seen at analysis time (when available)

In-memory only for this milestone — fine for a single local dashboard
process; move to SQLite later if the cache needs to survive a restart.
"""
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import config


@dataclass
class CachedAnalysis:
    symbol: str
    analysis_type: str
    analyzed_at: datetime
    price_at_analysis: float
    attention_score_at_analysis: int
    latest_news_id_at_analysis: Optional[str]
    analysis: str
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class AIAnalysisCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: Dict[Tuple[str, str], CachedAnalysis] = {}

    def get_fresh(
        self,
        symbol: str,
        analysis_type: str,
        current_price: float,
        current_attention_score: int,
        latest_news_id: Optional[str] = None,
    ) -> Optional[CachedAnalysis]:
        """Return the cached analysis for (symbol, analysis_type) if it's still fresh, else None."""
        with self._lock:
            entry = self._store.get((symbol, analysis_type))
        if entry is None:
            return None

        now = datetime.now(timezone.utc)
        if now - entry.analyzed_at > timedelta(minutes=config.AI_ANALYSIS_CACHE_MINUTES):
            return None

        if entry.price_at_analysis:
            price_change_pct = abs(current_price - entry.price_at_analysis) / entry.price_at_analysis * 100.0
            if price_change_pct >= config.AI_REANALYZE_PRICE_CHANGE_PERCENT:
                return None

        if abs(current_attention_score - entry.attention_score_at_analysis) >= config.AI_REANALYZE_ATTENTION_CHANGE:
            return None

        if (
            latest_news_id is not None
            and entry.latest_news_id_at_analysis is not None
            and latest_news_id != entry.latest_news_id_at_analysis
        ):
            return None

        return entry

    def store(self, entry: CachedAnalysis) -> None:
        with self._lock:
            self._store[(entry.symbol, entry.analysis_type)] = entry


ai_cache = AIAnalysisCache()
