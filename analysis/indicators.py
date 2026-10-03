"""
analysis/indicators.py — Core technical indicator math, the TickerMetrics
result type, and compute_metrics(), the single entry point that turns raw
daily bars into a fully-computed result.

IMPORTANT: no function in this module (or analysis.levels / .signals /
.scoring) ever looks at data past the most recent bar it is given. All
indicators are computed strictly from history up to "today" (the last row
in the input DataFrame). Fields are left as None rather than fabricated
when there isn't enough history to compute them.
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

import config
from analysis import levels, scoring, signals


@dataclass
class TickerMetrics:
    """All computed data for a single symbol as of its most recent bar.

    `price_source` and `as_of` exist specifically so callers can tell the
    user whether "price" is a live/latest trade or a prior daily close —
    this project never presents delayed data as if it were real-time.
    """

    symbol: str
    price: float
    price_source: str          # "latest_trade" or "daily_close"
    as_of: pd.Timestamp        # timestamp of the price's source bar/trade

    prev_close: float
    pct_change: float
    gap_pct: Optional[float]

    volume: float
    avg_volume: Optional[float]
    relative_volume: Optional[float]
    volume_expansion: Optional[float]   # short-avg / long-avg volume ratio

    rsi: Optional[float]
    ema_fast: Optional[float]
    ema_medium: Optional[float]
    ema_slow: Optional[float]
    trend: str

    atr: Optional[float]

    high_20d: Optional[float]
    low_20d: Optional[float]
    dist_from_high_pct: Optional[float]
    dist_from_low_pct: Optional[float]

    support: Optional[float]
    resistance: Optional[float]
    dist_from_support_pct: Optional[float]
    dist_from_resistance_pct: Optional[float]

    momentum_5d_pct: Optional[float]
    momentum_10d_pct: Optional[float]

    volatility_pct: Optional[float]
    volatility_expansion: Optional[float]  # short-window vol / long-window vol ratio

    momentum_score: int
    relative_volume_score: int
    trend_strength_score: int
    volatility_score: int
    attention_score: int

    signal: str


# ---------------------------------------------------------------------------
# Core indicator math
# ---------------------------------------------------------------------------
def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average. Returns NaN wherever there isn't enough history."""
    if len(series) < span:
        return pd.Series([np.nan] * len(series), index=series.index)
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Returns NaN wherever there isn't enough history."""
    if len(series) < period + 1:
        return pd.Series([np.nan] * len(series), index=series.index)

    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # Where average loss is 0 (all gains), RSI is 100.
    result = result.where(avg_loss != 0, 100.0)
    return result


def atr(bars: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's Average True Range. Returns NaN wherever there isn't enough history."""
    if len(bars) < period + 1:
        return pd.Series([np.nan] * len(bars), index=bars.index)

    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def percent_change(current: float, previous: float) -> Optional[float]:
    """Percent change from `previous` to `current`. None if previous is 0/invalid."""
    if previous in (0, None) or pd.isna(previous):
        return None
    return (current - previous) / previous * 100.0


def relative_volume(current_volume: float, avg_volume: Optional[float]) -> Optional[float]:
    """Today's volume divided by the recent average volume (a multiple, e.g. 2.3x)."""
    if not avg_volume or pd.isna(avg_volume) or avg_volume == 0:
        return None
    return current_volume / avg_volume


def ratio_of_averages(series: pd.Series, short_window: int, long_window: int) -> Optional[float]:
    """
    Short-window average divided by long-window average of `series`
    (volume or volatility) — a generic "is this expanding recently?" ratio.
    None if there isn't enough history for the longer window.
    """
    if len(series) < long_window:
        return None
    long_avg = series.tail(long_window).mean()
    if not long_avg or pd.isna(long_avg):
        return None
    short_avg = series.tail(short_window).mean()
    return float(short_avg / long_avg)


def momentum_pct(close: pd.Series, lookback: int) -> Optional[float]:
    """% change from `lookback` bars ago to the most recent bar."""
    if len(close) < lookback + 1:
        return None
    past = close.iloc[-(lookback + 1)]
    current = close.iloc[-1]
    return percent_change(float(current), float(past))


def historical_volatility_pct(close: pd.Series, window: int) -> Optional[float]:
    """
    Annualized historical volatility, as a percentage, based on the standard
    deviation of daily log returns over `window` trailing sessions.
    """
    if len(close) < window + 1:
        return None
    returns = np.log(close / close.shift(1)).dropna()
    recent = returns.tail(window)
    if len(recent) < 2:
        return None
    daily_std = recent.std()
    if pd.isna(daily_std):
        return None
    return float(daily_std * np.sqrt(252) * 100.0)


def determine_trend(
    price: float,
    ema_fast: Optional[float],
    ema_medium: Optional[float],
    ema_slow: Optional[float],
) -> str:
    """
    Classify trend from EMA stacking:
      Bullish  -> price > EMA9 > EMA20 > EMA50 (in proper alignment)
      Bearish  -> price < EMA9 < EMA20 < EMA50
      Neutral  -> anything else, or not enough history for all EMAs
    """
    values = [ema_fast, ema_medium, ema_slow]
    if any(v is None or pd.isna(v) for v in values):
        return "Neutral"

    if price > ema_fast > ema_medium > ema_slow:
        return "Bullish"
    if price < ema_fast < ema_medium < ema_slow:
        return "Bearish"
    return "Neutral"


# ---------------------------------------------------------------------------
# The single entry point both the market scanner and the watchlist use to
# turn raw daily bars into a fully-computed TickerMetrics record. Keeping
# this logic in one place avoids duplicating indicator math between modules.
# ---------------------------------------------------------------------------
def compute_metrics(
    symbol: str,
    bars: pd.DataFrame,
    latest_trade_price: Optional[float] = None,
    latest_trade_time: Optional[pd.Timestamp] = None,
) -> Optional[TickerMetrics]:
    """
    Compute all indicators/scores/signal for one symbol.

    `bars` must be a DataFrame of daily OHLCV data sorted ascending by time
    (oldest first, most recent last), with columns: open, high, low, close,
    volume. Only historical data already in `bars` is used — nothing here
    fabricates values for missing history; fields that can't be computed
    are left as None.

    `latest_trade_price`/`latest_trade_time`, if provided, are used as the
    "current" price (labeled price_source="latest_trade"). Otherwise the
    most recent daily bar's close is used (price_source="daily_close").
    """
    if bars is None or bars.empty:
        return None

    bars = bars.sort_values("timestamp").reset_index(drop=True)
    if len(bars) < 2:
        # Need at least two bars to compute a day-over-day change.
        return None

    close = bars["close"]
    volume = bars["volume"]

    latest_bar = bars.iloc[-1]
    prev_bar = bars.iloc[-2]

    prev_close = float(prev_bar["close"])

    if latest_trade_price is not None and latest_trade_time is not None:
        price = float(latest_trade_price)
        price_source = "latest_trade"
        as_of = latest_trade_time
    else:
        price = float(latest_bar["close"])
        price_source = "daily_close"
        as_of = latest_bar["timestamp"]

    pct_change_value = percent_change(price, prev_close)
    gap_pct = percent_change(float(latest_bar["open"]), prev_close)

    ema_fast_series = ema(close, config.EMA_FAST)
    ema_medium_series = ema(close, config.EMA_MEDIUM)
    ema_slow_series = ema(close, config.EMA_SLOW)
    rsi_series = rsi(close, config.RSI_PERIOD)
    atr_series = atr(bars, config.ATR_PERIOD)

    ema_fast_val = _last_valid(ema_fast_series)
    ema_medium_val = _last_valid(ema_medium_series)
    ema_slow_val = _last_valid(ema_slow_series)
    rsi_val = _last_valid(rsi_series)
    atr_val = _last_valid(atr_series)

    vol_window = bars.tail(config.VOLUME_AVG_LOOKBACK_DAYS + 1).iloc[:-1]  # exclude "today"
    avg_volume = float(vol_window["volume"].mean()) if len(vol_window) > 0 else None
    rvol = relative_volume(float(latest_bar["volume"]), avg_volume)
    volume_expansion = ratio_of_averages(
        volume, config.VOLUME_EXPANSION_SHORT_DAYS, config.VOLUME_EXPANSION_LONG_DAYS
    )

    high_20d, low_20d = levels.high_low_levels(bars, config.HIGH_LOW_LOOKBACK_DAYS)
    dist_from_high = levels.distance_from_level_pct(price, high_20d)
    dist_from_low = levels.distance_from_level_pct(price, low_20d)

    support, resistance = levels.find_support_resistance(bars, price)
    dist_from_support = levels.distance_from_level_pct(price, support)
    dist_from_resistance = levels.distance_from_level_pct(price, resistance)

    momentum_5d = momentum_pct(close, config.MOMENTUM_5D_DAYS)
    momentum_10d = momentum_pct(close, config.MOMENTUM_10D_DAYS)

    volatility = historical_volatility_pct(close, config.VOLATILITY_LOOKBACK_DAYS)
    short_volatility = historical_volatility_pct(close, config.VOLATILITY_SHORT_LOOKBACK_DAYS)
    volatility_expansion = (
        short_volatility / volatility if volatility and short_volatility is not None else None
    )

    trend = determine_trend(price, ema_fast_val, ema_medium_val, ema_slow_val)

    m_score = scoring.momentum_score(pct_change_value, rsi_val)
    rv_score = scoring.relative_volume_score(rvol)
    t_score = scoring.trend_strength_score(price, ema_fast_val, ema_medium_val, ema_slow_val)
    v_score = scoring.volatility_score(volatility)
    a_score = scoring.attention_score(m_score, rv_score, t_score, v_score)

    signal = signals.generate_signal(pct_change_value, rsi_val, rvol, trend, dist_from_high)

    return TickerMetrics(
        symbol=symbol,
        price=price,
        price_source=price_source,
        as_of=as_of,
        prev_close=prev_close,
        pct_change=pct_change_value if pct_change_value is not None else 0.0,
        gap_pct=gap_pct,
        volume=float(latest_bar["volume"]),
        avg_volume=avg_volume,
        relative_volume=rvol,
        volume_expansion=volume_expansion,
        rsi=rsi_val,
        ema_fast=ema_fast_val,
        ema_medium=ema_medium_val,
        ema_slow=ema_slow_val,
        trend=trend,
        atr=atr_val,
        high_20d=high_20d,
        low_20d=low_20d,
        dist_from_high_pct=dist_from_high,
        dist_from_low_pct=dist_from_low,
        support=support,
        resistance=resistance,
        dist_from_support_pct=dist_from_support,
        dist_from_resistance_pct=dist_from_resistance,
        momentum_5d_pct=momentum_5d,
        momentum_10d_pct=momentum_10d,
        volatility_pct=volatility,
        volatility_expansion=volatility_expansion,
        momentum_score=m_score,
        relative_volume_score=rv_score,
        trend_strength_score=t_score,
        volatility_score=v_score,
        attention_score=a_score,
        signal=signal,
    )


def _last_valid(series: pd.Series) -> Optional[float]:
    """Return the last non-NaN value in a series, or None if there isn't one."""
    valid = series.dropna()
    if valid.empty:
        return None
    return float(valid.iloc[-1])
