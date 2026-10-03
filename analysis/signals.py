"""
analysis/signals.py — The signal engine: transparent, rule-based
classification of a stock's current activity.

Every threshold is a plain constant below, so the rules stay easy to audit
and tune. Signals describe what already happened in the data — they are
descriptive, not predictions, and deliberately not buy/sell instructions.
Combining a technical signal like this with catalyst and risk context into
an actionable conclusion is future work (see project plan) — one signal
alone isn't enough context for that.
"""
from typing import Optional

import pandas as pd

RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0

LARGE_MOVE_PCT = 5.0          # +/- daily change considered a "large" move
STRONG_MOMENTUM_PCT = 3.0     # daily change considered strong (but not "large")
PULLBACK_PCT = -2.0           # daily drop, while still in an uptrend, is a "pullback"

UNUSUAL_VOLUME_RVOL = 2.0     # relative volume multiple considered unusual
BREAKOUT_PROXIMITY_PCT = 1.0  # within this % of the 20-day high counts as "near the high"

GAP_PCT_THRESHOLD = 2.0       # open-vs-prior-close gap considered notable


def generate_signal(
    pct_change_value: Optional[float],
    rsi_value: Optional[float],
    rvol: Optional[float],
    trend: str,
    dist_from_high_pct: Optional[float],
) -> str:
    """Return one descriptive, rule-based signal label. See module docstring."""
    pct = pct_change_value if pct_change_value is not None else 0.0

    if pct >= LARGE_MOVE_PCT:
        return "LARGE GAIN"
    if pct <= -LARGE_MOVE_PCT:
        return "LARGE DROP"

    if rsi_value is not None and not pd.isna(rsi_value):
        if rsi_value >= RSI_OVERBOUGHT:
            return "OVERBOUGHT"
        if rsi_value <= RSI_OVERSOLD:
            return "OVERSOLD"

    if rvol is not None and rvol >= UNUSUAL_VOLUME_RVOL:
        return "UNUSUAL VOLUME"

    if (
        dist_from_high_pct is not None
        and -BREAKOUT_PROXIMITY_PCT <= dist_from_high_pct <= 0
        and trend == "Bullish"
    ):
        return "BREAKOUT WATCH"

    if trend == "Bullish" and pct <= PULLBACK_PCT:
        return "PULLBACK"

    if trend == "Bullish" and pct >= STRONG_MOMENTUM_PCT:
        return "STRONG MOMENTUM"

    return "NORMAL"
