"""
analysis/levels.py — Price-level calculations: 20-day high/low, and simple
swing-based support/resistance detection.

Support/resistance use a plain, deterministic swing-point method (a bar is
a swing low/high if its low/high is the most extreme value within a small
window of bars on either side) — no curve fitting, no lookahead: only bars
already in the given window are ever considered relative to "today."
"""
from typing import Optional, Tuple

import pandas as pd

import config


def distance_from_level_pct(price: float, level: Optional[float]) -> Optional[float]:
    """Percent distance of `price` from `level` (negative = below, 0 = at level)."""
    if level is None or pd.isna(level) or level == 0:
        return None
    return (price - level) / level * 100.0


def high_low_levels(bars: pd.DataFrame, lookback: int) -> Tuple[Optional[float], Optional[float]]:
    """Highest high / lowest low over the trailing `lookback` bars (including today)."""
    window = bars.tail(lookback)
    if window.empty:
        return None, None
    return float(window["high"].max()), float(window["low"].min())


def find_support_resistance(
    bars: pd.DataFrame,
    price: float,
    lookback: int = config.SUPPORT_RESISTANCE_LOOKBACK_DAYS,
    swing_window: int = config.SWING_WINDOW_DAYS,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Nearest support = highest recent swing-low below the current price.
    Nearest resistance = lowest recent swing-high above the current price.

    A bar is a "swing low"/"swing high" if its low/high is the most extreme
    value within `swing_window` bars on either side of it. Returns
    (None, None) if there isn't enough history to find any swing points.
    """
    window = bars.tail(lookback).reset_index(drop=True)
    n = len(window)
    if n < swing_window * 2 + 1:
        return None, None

    swing_lows = []
    swing_highs = []
    for i in range(swing_window, n - swing_window):
        segment_low = window["low"].iloc[i - swing_window : i + swing_window + 1]
        if window["low"].iloc[i] == segment_low.min():
            swing_lows.append(float(window["low"].iloc[i]))
        segment_high = window["high"].iloc[i - swing_window : i + swing_window + 1]
        if window["high"].iloc[i] == segment_high.max():
            swing_highs.append(float(window["high"].iloc[i]))

    supports_below = [lvl for lvl in swing_lows if lvl < price]
    resistances_above = [lvl for lvl in swing_highs if lvl > price]

    nearest_support = max(supports_below) if supports_below else None
    nearest_resistance = min(resistances_above) if resistances_above else None
    return nearest_support, nearest_resistance
