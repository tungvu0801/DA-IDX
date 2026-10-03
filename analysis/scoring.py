"""
analysis/scoring.py — Descriptive 0-100 scores and the Attention Score.

None of these are investment advice. They measure how notable a stock's
activity is along one dimension (momentum, relative volume, trend
strength, volatility) so results can be sorted/prioritized — never a
buy/sell rating.
"""
from typing import Optional

import pandas as pd

import config


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> int:
    return int(round(max(low, min(high, value))))


def momentum_score(pct_change_value: Optional[float], rsi_value: Optional[float]) -> int:
    """Blends the size of today's move with RSI positioning. 50 = neutral."""
    if pct_change_value is None:
        return 0
    # Map +/-10% move to a 0-100 scale centered at 50.
    move_component = 50 + (pct_change_value / 10.0) * 50
    if rsi_value is not None and not pd.isna(rsi_value):
        rsi_component = rsi_value  # RSI is already 0-100
        score = (move_component * 0.7) + (rsi_component * 0.3)
    else:
        score = move_component
    return _clamp(score)


def relative_volume_score(rvol: Optional[float]) -> int:
    """Maps relative volume (1.0x = normal) onto 0-100. 4x+ volume saturates at 100."""
    if rvol is None:
        return 0
    return _clamp((rvol / 4.0) * 100.0)


def trend_strength_score(
    price: float,
    ema_fast: Optional[float],
    ema_medium: Optional[float],
    ema_slow: Optional[float],
) -> int:
    """
    Measures how far apart/aligned the EMAs are (as a % spread from price),
    which is a common proxy for trend conviction rather than just direction.
    """
    values = [ema_fast, ema_medium, ema_slow]
    if any(v is None or pd.isna(v) for v in values) or price == 0:
        return 0
    spread_pct = abs(ema_fast - ema_slow) / price * 100.0
    aligned_bullish = price > ema_fast > ema_medium > ema_slow
    aligned_bearish = price < ema_fast < ema_medium < ema_slow
    if not (aligned_bullish or aligned_bearish):
        # Mixed/choppy EMAs -> weak trend regardless of spread.
        return _clamp(spread_pct * 5.0, 0, 40)
    # Aligned EMAs -> scale spread of up to 8% to the full 0-100 range.
    return _clamp((spread_pct / 8.0) * 100.0)


def volatility_score(volatility_pct_value: Optional[float]) -> int:
    """Maps annualized volatility onto 0-100 (100%+ annualized volatility saturates)."""
    if volatility_pct_value is None:
        return 0
    return _clamp((volatility_pct_value / 100.0) * 100.0)


def attention_score(
    momentum: int,
    rvol_score: int,
    trend_score: int,
    vol_score: int,
) -> int:
    """
    "Attention Score" — a descriptive 0-100 measure of how much unusual or
    interesting activity a stock is showing right now (big move, unusual
    volume, strong trend, elevated volatility).

    This is NOT a buy/sell/investment-recommendation score. It exists only
    to help sort/prioritize which stocks are worth a closer look — including
    which ones cross config.AI_ANALYSIS_ATTENTION_THRESHOLD and get a
    Technical Agent explanation. Weights are configurable in config.py.
    """
    # Momentum score is centered at 50 (neutral); distance from 50 is what's
    # actually "attention-grabbing" either up or down.
    momentum_intensity = abs(momentum - 50) * 2  # 0-100
    score = (
        momentum_intensity * config.ATTENTION_WEIGHT_MOMENTUM
        + rvol_score * config.ATTENTION_WEIGHT_RVOL
        + trend_score * config.ATTENTION_WEIGHT_TREND
        + vol_score * config.ATTENTION_WEIGHT_VOLATILITY
    )
    return _clamp(score)
