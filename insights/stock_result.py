"""
insights/stock_result.py — "Show full analysis" data for one stock (Stage 2.7G.3). Read-only extraction.

Everything comes from research the app ALREADY has: the research bundle kept in services.analysis_cache
after an Analyze (this session), or else the latest saved research snapshot (Stage 2.5 table, SELECT only).
Nothing is recomputed and no Claude call is made. Missing values stay None and are shown as unavailable.
"""
from __future__ import annotations

import json
from typing import Any, Optional


def _r(v, n=2):
    return None if v is None else round(v, n)


def _ai_text(t: Optional[str]) -> Optional[str]:
    """AI-written text, or None when the AI part was unavailable (gating messages start with 'AI analysis')."""
    if not t or str(t).startswith("AI analysis"):
        return None
    return t


def from_bundle(cached: Any) -> dict:
    b, m = cached.bundle, cached.bundle.metrics
    ev = b.events
    nearest = getattr(ev, "nearest_event", None) if ev is not None else None
    return {
        "source": "FRESH_RESEARCH", "as_of": cached.generated_at,
        "technical": {"trend": m.trend, "ema_9": _r(m.ema_fast), "ema_20": _r(m.ema_medium), "ema_50": _r(m.ema_slow),
                      "rsi": _r(m.rsi, 1), "momentum_5d_pct": _r(m.momentum_5d_pct),
                      "momentum_10d_pct": _r(m.momentum_10d_pct), "momentum_score": m.momentum_score},
        "volume": {"volume": m.volume, "avg_volume": m.avg_volume, "relative_volume": _r(m.relative_volume)},
        "levels": {"support": _r(m.support), "resistance": _r(m.resistance), "high_20d": _r(m.high_20d),
                   "low_20d": _r(m.low_20d), "dist_from_support_pct": _r(m.dist_from_support_pct),
                   "dist_from_resistance_pct": _r(m.dist_from_resistance_pct)},
        "volatility": {"atr": _r(m.atr), "volatility_pct": _r(m.volatility_pct)},
        "evidence": {"research_view": b.research_view, "bullish_pct": b.evidence.bullish_pct,
                     "neutral_pct": b.evidence.neutral_pct, "bearish_pct": b.evidence.bearish_pct,
                     "categories": [{"category": x.category, "score": _r(x.score), "weight": _r(x.weight)}
                                    for x in b.evidence.breakdown]},
        "data_quality": {"level": b.data_quality.level, "explanation": b.data_quality.explanation},
        "catalysts": {"items": [{"title": c.title, "source": c.source, "published_at": c.published_at,
                                 "sentiment": c.sentiment, "reason": c.reason} for c in b.catalyst.items],
                      "note": b.catalyst.message or None},
        "risk": {"flags": [{"code": f.code, "severity": f.severity, "description": f.description} for f in b.risk_flags],
                 "explanation_ai": _ai_text(b.risk_explanation)},
        "narrative_ai": None if _ai_text(b.narrative.whats_happening) is None else {
            "whats_happening": b.narrative.whats_happening, "why": b.narrative.why,
            "whats_good": list(b.narrative.whats_good or []), "be_careful": list(b.narrative.be_careful or [])},
        "sector": None if b.sector is None else {
            "name": b.sector.sector_name, "etf": b.sector.sector_etf, "pct_change": _r(b.sector.sector_pct_change),
            "market_symbol": b.sector.market_proxy_symbol, "market_pct_change": _r(b.sector.market_pct_change)},
        "events": None if ev is None else {
            "event_risk": ev.event_risk_level,
            "nearest": None if nearest is None else {"title": nearest.title, "date": str(nearest.event_date)}},
    }


def from_snapshot(s: Any) -> dict:
    def load(raw):
        try:
            v = json.loads(raw or "[]")
            return v if isinstance(v, list) else []
        except ValueError:
            return []
    return {
        "source": "SAVED_SNAPSHOT", "as_of": s.created_at,
        "technical": {"trend": None, "ema_9": _r(s.ema_9), "ema_20": _r(s.ema_20), "ema_50": _r(s.ema_50),
                      "rsi": _r(s.rsi, 1), "momentum_5d_pct": _r(s.momentum_5d_pct),
                      "momentum_10d_pct": _r(s.momentum_10d_pct), "momentum_score": None},
        "volume": {"volume": s.volume, "avg_volume": None, "relative_volume": _r(s.relative_volume)},
        "levels": {"support": _r(s.support), "resistance": _r(s.resistance), "high_20d": None, "low_20d": None,
                   "dist_from_support_pct": None, "dist_from_resistance_pct": None},
        "volatility": {"atr": _r(s.atr), "volatility_pct": _r(s.volatility_pct)},
        "evidence": {"research_view": s.research_view, "bullish_pct": s.bullish_pct, "neutral_pct": s.neutral_pct,
                     "bearish_pct": s.bearish_pct,
                     "categories": [{"category": k, "score": _r(v), "weight": None} for k, v in (
                         ("technical", s.evidence_technical), ("catalyst", s.evidence_catalyst),
                         ("risk", s.evidence_risk), ("market", s.evidence_market), ("sector", s.evidence_sector))]},
        "data_quality": {"level": s.data_quality_level, "explanation": None},
        "catalysts": {"items": [{"title": c.get("title"), "source": c.get("source"), "published_at": c.get("published_at"),
                                 "sentiment": c.get("sentiment"), "reason": None} for c in load(s.catalysts_json)],
                      "note": None},
        "risk": {"flags": [{"code": f.get("code"), "severity": f.get("severity"), "description": f.get("description")}
                           for f in load(s.risk_flags_json)], "explanation_ai": None},
        "narrative_ai": None,
        "sector": None if not s.sector_name else {"name": s.sector_name, "etf": s.sector_etf,
                                                  "pct_change": _r(s.sector_pct_change), "market_symbol": None,
                                                  "market_pct_change": _r(s.market_pct_change)},
        "events": None,
    }
