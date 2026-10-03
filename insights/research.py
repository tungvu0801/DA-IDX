"""
insights/research.py — Read existing Stage 2 research for a symbol (read-only, Stage 2.7E).

Sources, in order:
  1. a FRESH research bundle the user just generated through the existing
     GET /api/stocks/{symbol}/research endpoint (looked up by its analysis_id via
     services.analysis_cache.get — the public API; nothing is recomputed here)
  2. the latest SAVED research snapshot (Stage 2.5 table, SELECT only)
  3. none -> research UNAVAILABLE (never fabricated)

This module never recomputes, re-weights or overrides evidence. It only copies the
values Stage 2 already produced and reports how old they are.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

import config


@dataclass
class ResearchFacts:
    symbol: str
    source: str                          # FRESH_RESEARCH | SAVED_SNAPSHOT | UNAVAILABLE
    research_view: Optional[str] = None
    bullish_pct: Optional[int] = None
    neutral_pct: Optional[int] = None
    bearish_pct: Optional[int] = None
    category_scores: Dict[str, Optional[float]] = field(default_factory=dict)  # technical/catalyst/risk/market/sector
    data_quality: Optional[str] = None
    as_of: Optional[datetime] = None
    age_hours: Optional[float] = None
    stale: bool = True
    analysis_id: Optional[str] = None
    note: str = ""

    @property
    def available(self) -> bool:
        return self.source != "UNAVAILABLE" and self.research_view is not None


def _age(as_of: Optional[datetime], now: datetime) -> Optional[float]:
    if as_of is None:
        return None
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    return round((now - as_of).total_seconds() / 3600.0, 1)


def from_bundle(symbol: str, cached, now: datetime, analysis_id: str) -> ResearchFacts:
    b = cached.bundle
    ev = b.evidence
    scores = {x.category: x.score for x in ev.breakdown}
    age = _age(cached.generated_at, now)
    return ResearchFacts(symbol, "FRESH_RESEARCH", b.research_view, ev.bullish_pct, ev.neutral_pct, ev.bearish_pct,
                         scores, b.data_quality.level, cached.generated_at, age,
                         stale=age is None or age > config.BEGINNER_RESEARCH_STALE_HOURS, analysis_id=analysis_id,
                         note="Research generated just now by the Stage 2 research engine.")


def from_snapshot(symbol: str, s, now: datetime) -> ResearchFacts:
    age = _age(s.created_at, now)
    return ResearchFacts(
        symbol, "SAVED_SNAPSHOT", s.research_view, s.bullish_pct, s.neutral_pct, s.bearish_pct,
        {"technical": s.evidence_technical, "catalyst": s.evidence_catalyst, "risk": s.evidence_risk,
         "market": s.evidence_market, "sector": s.evidence_sector},
        s.data_quality_level, s.created_at, age, stale=age is None or age > config.BEGINNER_RESEARCH_STALE_HOURS,
        note="Latest saved research snapshot.")


def lookup_research(symbol: str, now: datetime, *, analysis_id: Optional[str] = None,
                    cache_get: Optional[Callable] = None, db_getter: Optional[Callable] = None) -> ResearchFacts:
    symbol = symbol.strip().upper()
    if analysis_id and cache_get is not None:
        cached = cache_get(analysis_id)
        if cached is not None and cached.symbol.upper() == symbol:
            return from_bundle(symbol, cached, now, analysis_id)
    if db_getter is not None:
        try:
            rows = db_getter().list_snapshots(symbol=symbol, limit=1)
        except Exception:  # noqa: BLE001 - research context is optional
            rows = []
        if rows:
            return from_snapshot(symbol, rows[0], now)
    return ResearchFacts(symbol, "UNAVAILABLE", note="No research is available yet for this stock.")


def research_summary(r: ResearchFacts) -> dict:
    return {
        "symbol": r.symbol, "source": r.source, "available": r.available, "research_view": r.research_view,
        "bullish_pct": r.bullish_pct, "neutral_pct": r.neutral_pct, "bearish_pct": r.bearish_pct,
        "category_scores": {k: (None if v is None else round(v, 2)) for k, v in r.category_scores.items()},
        "data_quality": r.data_quality, "as_of": r.as_of.isoformat() if r.as_of else None,
        "age_hours": r.age_hours, "stale": r.stale if r.available else True,
        "stale_after_hours": config.BEGINNER_RESEARCH_STALE_HOURS, "note": r.note,
    }
