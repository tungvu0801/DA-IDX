"""
analysis/evidence_scoring.py — Deterministic evidence-percentage engine.

Turns up to five independent -1..+1 category scores (Technical, Catalyst,
Risk, Market, Sector) into three percentages — bullish/neutral/bearish —
that always sum to exactly 100. This is pure Python arithmetic; Claude is
never involved in computing a percentage anywhere in this file, and these
percentages are NOT a probability of future returns — they describe how
currently-available, already-verified evidence is distributed right now.

Missing-data handling: a category with no data (score=None) is EXCLUDED
from the weighted average — its weight is redistributed proportionally
among the categories that do have data, rather than defaulting to 0 (which
would silently and incorrectly count "no data" as neutral evidence). If
every category is unavailable, the result is INSUFFICIENT DATA (net=None),
not a fabricated 33/33/34 split.

Why bullish/bearish are computed BEFORE summing to one net number, not
after: collapsing straight to a single net composite first (then splitting
that into buckets) lets a strong bullish category and a strong bearish
category cancel out into a falsely calm "mostly neutral" reading. Splitting
each category's own bullish/bearish contribution first, then summing,
keeps genuinely conflicting evidence visible as material weight on BOTH
sides — e.g. strong upward momentum plus a real overextension risk becomes
"40% bullish / 30% bearish / 30% neutral", not "10% bullish / 90% neutral".
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

import config
from analysis.indicators import TickerMetrics
from analysis.sector_context import SectorContext

CATEGORIES = ("technical", "catalyst", "risk", "market", "sector")


def technical_score(m: TickerMetrics) -> float:
    """
    -1..+1 from trend direction (45%), momentum (30%), and position within
    the 20-day high/low range (25%). Always available whenever `m` exists.
    """
    trend_dir = {"Bullish": 1.0, "Bearish": -1.0}.get(m.trend, 0.0)
    momentum_dir = _clamp((m.momentum_score - 50) / 50.0)
    if m.high_20d is not None and m.low_20d is not None and m.high_20d != m.low_20d:
        range_dir = _clamp(2 * (m.price - m.low_20d) / (m.high_20d - m.low_20d) - 1)
    else:
        range_dir = 0.0
    return _clamp(0.45 * trend_dir + 0.30 * momentum_dir + 0.25 * range_dir)


def catalyst_score(sentiments: List[str]) -> Optional[float]:
    """
    -1..+1 from classified catalyst sentiments ("POSITIVE"/"NEUTRAL"/
    "NEGATIVE"/"UNCERTAIN"). UNCERTAIN entries are excluded from the count
    (they neither confirm nor deny a direction). None if no relevant
    catalysts were classified at all — never defaulted to 0.
    """
    relevant = [s for s in sentiments if s in ("POSITIVE", "NEUTRAL", "NEGATIVE")]
    if not relevant:
        return None
    positive = relevant.count("POSITIVE")
    negative = relevant.count("NEGATIVE")
    return (positive - negative) / len(relevant)


def market_score(sector: Optional[SectorContext]) -> Optional[float]:
    if sector is None or sector.market_pct_change is None:
        return None
    return _clamp(sector.market_pct_change / config.MARKET_NORM_PCT)


def sector_score(sector: Optional[SectorContext]) -> Optional[float]:
    if sector is None or sector.sector_pct_change is None:
        return None
    return _clamp(sector.sector_pct_change / config.SECTOR_NORM_PCT)


def _category_weights() -> Dict[str, float]:
    return {
        "technical": config.EVIDENCE_WEIGHT_TECHNICAL,
        "catalyst": config.EVIDENCE_WEIGHT_CATALYST,
        "risk": config.EVIDENCE_WEIGHT_RISK,
        "market": config.EVIDENCE_WEIGHT_MARKET,
        "sector": config.EVIDENCE_WEIGHT_SECTOR,
    }


@dataclass
class EvidenceBreakdown:
    """One category's contribution — kept for transparency/debugging/UI display."""

    category: str
    score: Optional[float]  # -1..+1, or None if unavailable
    weight: float  # renormalized weight actually used (0 if unavailable)


@dataclass
class EvidenceResult:
    bullish_pct: int
    neutral_pct: int
    bearish_pct: int
    net: Optional[float]  # (bullish_pct - bearish_pct) / 100, or None if no data at all
    available_categories: List[str]
    unavailable_categories: List[str]
    breakdown: List[EvidenceBreakdown]

    @property
    def insufficient_data(self) -> bool:
        return self.net is None


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _largest_remainder_round(values: List[float]) -> List[int]:
    """
    Round a list of floats that sum to ~100 into integers that sum to
    EXACTLY 100, using the largest-remainder method (avoids the common bug
    where independently-rounded percentages sum to 99 or 101).
    """
    floors = [int(v) for v in values]
    remainder = max(0, min(len(values), 100 - sum(floors)))
    order = sorted(range(len(values)), key=lambda i: values[i] - floors[i], reverse=True)
    for i in order[:remainder]:
        floors[i] += 1
    return floors


def compute_evidence(scores: Dict[str, Optional[float]]) -> EvidenceResult:
    """
    `scores` maps each of CATEGORIES to a -1..+1 float, or None if that
    category's data is unavailable. Unknown keys are ignored; a missing key
    is treated the same as an explicit None.
    """
    weights = _category_weights()
    available = {cat: _clamp(scores[cat]) for cat in CATEGORIES if scores.get(cat) is not None}
    unavailable = [cat for cat in CATEGORIES if cat not in available]
    total_weight = sum(weights[cat] for cat in available)

    breakdown = [
        EvidenceBreakdown(
            category=cat,
            score=available.get(cat),
            weight=(weights[cat] / total_weight) if cat in available and total_weight > 0 else 0.0,
        )
        for cat in CATEGORIES
    ]

    if total_weight <= 0:
        return EvidenceResult(
            bullish_pct=0,
            neutral_pct=0,
            bearish_pct=0,
            net=None,
            available_categories=[],
            unavailable_categories=list(CATEGORIES),
            breakdown=breakdown,
        )

    bullish_raw = sum(b.weight * max(b.score, 0.0) for b in breakdown if b.score is not None)
    bearish_raw = sum(b.weight * max(-b.score, 0.0) for b in breakdown if b.score is not None)
    neutral_raw = 1.0 - bullish_raw - bearish_raw

    bullish_pct, neutral_pct, bearish_pct = _largest_remainder_round(
        [bullish_raw * 100, neutral_raw * 100, bearish_raw * 100]
    )
    net = (bullish_pct - bearish_pct) / 100.0

    return EvidenceResult(
        bullish_pct=bullish_pct,
        neutral_pct=neutral_pct,
        bearish_pct=bearish_pct,
        net=net,
        available_categories=list(available.keys()),
        unavailable_categories=unavailable,
        breakdown=breakdown,
    )
