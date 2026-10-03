"""
insights/labels.py — Deterministic plain-language LABELS from facts the app already computes (Stage 2.7E).

Labels describe current conditions for a beginner. They are not scores, not predictions, and never
feed back into Stage 2 evidence, Research View, Stage 2.6 event scoring or portfolio policy.

Cutoff sources (existing constants are reused wherever one exists):
  trend          analysis.indicators.determine_trend (EMA stacking: "Bullish"/"Bearish"/"Neutral")
  momentum       analysis.scoring.momentum_score (0-100, 50 = neutral) vs config.BEGINNER_MOMENTUM_*_SCORE
  price location config.APPROACHING_LEVEL_PCT (existing "approaching support/resistance" rule)
  volume         analysis.signals.UNUSUAL_VOLUME_RVOL (2.0x) and config.RISK_LOW_RVOL_THRESHOLD (1.0x)
  extended       config.RISK_EXTENDED_MOMENTUM_PCT (5-day momentum) or analysis.signals.RSI_OVERBOUGHT
  context score  config.RESEARCH_VIEW_LEAN_THRESHOLD applied to Stage 2 market/sector category scores
"""
from __future__ import annotations

from typing import Optional

import config
from analysis import signals

UNAVAILABLE = "UNAVAILABLE"
BULLISH_VIEWS = {"BULLISH BIAS", "STRONG BULLISH BIAS"}
BEARISH_VIEWS = {"BEARISH BIAS", "STRONG BEARISH BIAS"}
MIXED_VIEW = "MIXED / WAIT"


def trend_label(trend: Optional[str]) -> str:
    return {"Bullish": "UPTREND", "Bearish": "DOWNTREND", "Neutral": "MIXED"}.get(trend or "", UNAVAILABLE)


def momentum_label(momentum_score: Optional[float]) -> str:
    if momentum_score is None:
        return UNAVAILABLE
    if momentum_score >= config.BEGINNER_MOMENTUM_STRONG_SCORE:
        return "STRONG"
    if momentum_score <= config.BEGINNER_MOMENTUM_WEAK_SCORE:
        return "WEAK"
    return "NORMAL"


def volume_label(relative_volume: Optional[float]) -> str:
    if relative_volume is None:
        return UNAVAILABLE
    if relative_volume >= signals.UNUSUAL_VOLUME_RVOL:
        return "STRONG"
    if relative_volume < config.RISK_LOW_RVOL_THRESHOLD:
        return "WEAK"
    return "NORMAL"


def pct_distance(price: Optional[float], level: Optional[float]) -> Optional[float]:
    if price is None or level is None or level == 0:
        return None
    return (price - level) / level * 100.0


def price_location(price: Optional[float], support: Optional[float], resistance: Optional[float]) -> str:
    """NEAR_SUPPORT | NEAR_RESISTANCE | MIDDLE_OF_RANGE | NO_RESISTANCE_ABOVE | NO_SUPPORT_BELOW | UNAVAILABLE
    (existing APPROACHING_LEVEL_PCT rule; the "NO_*" labels say a level was not found, never invent one)."""
    if price is None or (support is None and resistance is None):
        return UNAVAILABLE
    near = config.APPROACHING_LEVEL_PCT
    d_sup = pct_distance(price, support)          # >= 0 when price is above support
    d_res = pct_distance(resistance, price) if resistance is not None else None  # >= 0 when below resistance
    near_sup = d_sup is not None and 0 <= d_sup <= near
    near_res = d_res is not None and 0 <= d_res <= near
    if near_sup and near_res:
        return "NEAR_SUPPORT" if d_sup <= d_res else "NEAR_RESISTANCE"
    if near_sup:
        return "NEAR_SUPPORT"
    if near_res:
        return "NEAR_RESISTANCE"
    if resistance is None:
        return "NO_RESISTANCE_ABOVE"       # price is above every recent swing high
    if support is None:
        return "NO_SUPPORT_BELOW"          # price is below every recent swing low
    return "MIDDLE_OF_RANGE"


def is_extended(momentum_5d_pct: Optional[float], rsi: Optional[float]) -> bool:
    return bool((momentum_5d_pct is not None and momentum_5d_pct >= config.RISK_EXTENDED_MOMENTUM_PCT)
                or (rsi is not None and rsi >= signals.RSI_OVERBOUGHT))


def context_label(category_score: Optional[float]) -> str:
    """Stage 2 market/sector category score (-1..+1) -> SUPPORTIVE / MIXED / WEAK."""
    if category_score is None:
        return UNAVAILABLE
    lean = config.RESEARCH_VIEW_LEAN_THRESHOLD
    if category_score >= lean:
        return "SUPPORTIVE"
    if category_score <= -lean:
        return "WEAK"
    return "MIXED"


def research_lean(view: Optional[str]) -> str:
    if view in BULLISH_VIEWS:
        return "POSITIVE"
    if view in BEARISH_VIEWS:
        return "NEGATIVE"
    if view == MIXED_VIEW:
        return "MIXED"
    return UNAVAILABLE


PRICE_LOCATION_TEXT = {
    "NEAR_SUPPORT": "Price is closer to support.",
    "NEAR_RESISTANCE": "Price is closer to resistance.",
    "MIDDLE_OF_RANGE": "Price is in the middle of its recent range.",
    "NO_RESISTANCE_ABOVE": "Price is above its recent swing highs, so no nearby resistance level was found.",
    "NO_SUPPORT_BELOW": "Price is below its recent swing lows, so no nearby support level was found.",
    UNAVAILABLE: "Support/resistance levels are unavailable for this stock right now.",
}
SUPPORT_TEXT = "Support is an area where buyers have recently appeared. It does not guarantee the price will stop falling."
RESISTANCE_TEXT = ("Resistance is an area where selling has recently appeared. It does not guarantee the price will "
                   "stop rising.")
