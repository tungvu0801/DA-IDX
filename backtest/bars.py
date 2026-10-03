"""
backtest/bars.py — the Stage 3.2 historical daily-bar cache.

Data source: the EXISTING Alpaca MARKET DATA path, unchanged — data.market_data.get_data_client() and
data.market_data.fetch_daily_bars(client, symbols, lookback_days=...) — so the backtest sees exactly the bars the
live app would request: daily timeframe, feed = config.DATA_FEED (IEX unless configured otherwise), adjustment = ALL
(split AND dividend adjusted). Nothing here changes the feed or adjustment for backtesting. No trading client exists.

Caching rules
  * One explicit download = one immutable DATASET per symbol (source, feed, adjustment, requested range, fetch time,
    SHA-256 of its bars). Refreshing never overwrites: it stores a NEW dataset, so an old run's exact bars remain.
  * Only COMPLETE sessions are stored: bars dated before today in New York. Today's (possibly in-progress) bar is
    never cached.
  * A symbol the provider returned nothing for is stored as an EMPTY dataset (bar_count 0) — reported, never filled in.
  * If the batched request returned nothing for EVERY symbol, it is treated as a failed request and nothing is cached
    (data.market_data.fetch_daily_bars logs and swallows request errors, so "all empty" cannot be trusted as data).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import config
from backtest.store import BacktestError, BacktestStore, BarRow, now_iso

NY = ZoneInfo("America/New_York")
SOURCE = "alpaca-market-data"
ADJUSTMENT = "all"          # data.market_data.fetch_daily_bars always requests Adjustment.ALL
SESSIONS_PER_YEAR = 252


def feed() -> str:
    """The feed data.market_data.fetch_daily_bars will actually use (same fallback rule: unknown -> IEX)."""
    from alpaca.data.enums import DataFeed
    try:
        return DataFeed(config.DATA_FEED).value
    except ValueError:
        return DataFeed.IEX.value


def last_complete_session_date(now: Optional[datetime] = None) -> date:
    """The latest calendar date whose session has certainly finished: the day before today in New York."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(NY).date() - timedelta(days=1)


def session_date_of(ts) -> date:
    import pandas as pd
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(NY).date()


def estimate_sessions(start: date, end: date) -> int:
    return max(1, math.ceil((end - start).days * SESSIONS_PER_YEAR / 365.25))


def fetch_and_store(store: BacktestStore, symbols: Iterable[str], fetch_start: date,
                    now: Optional[datetime] = None, fetch_fn=None, client=None) -> Dict[str, dict]:
    """ONE batched request (the existing fetch_daily_bars) for every symbol, from `fetch_start` through the last
    complete session; stores one dataset per symbol. Returns {symbol: dataset row}. Explicit user action only."""
    from data.market_data import fetch_daily_bars, get_data_client
    symbols = sorted(set(symbols))
    if not symbols:
        return {}
    now = now or datetime.now(timezone.utc)
    req_end = last_complete_session_date(now)
    if fetch_start > req_end:
        raise BacktestError("INVALID_DATE_RANGE", "The download range starts after the last complete session.")
    fetch_fn = fetch_fn or fetch_daily_bars
    try:
        client = client or get_data_client()
    except RuntimeError as exc:
        raise BacktestError("DATA_UNAVAILABLE", "Alpaca market data is not configured on the server.", status=503) from exc
    lookback_days = (now.date() - fetch_start).days + 2
    frames = fetch_fn(client, symbols, lookback_days=lookback_days)
    if not frames:
        raise BacktestError("FETCH_FAILED", "Alpaca returned no bars for any requested symbol, so the request probably "
                            "failed (network, credentials or limits). Nothing was cached.", status=502)
    fetched_at = now_iso()
    out = {}
    for sym in symbols:
        df = frames.get(sym)
        rows: List[BarRow] = []
        if df is not None:
            for r in df.itertuples(index=False):
                d = session_date_of(r.timestamp)
                if fetch_start <= d <= req_end:
                    ts = r.timestamp.tz_convert("UTC") if r.timestamp.tzinfo else r.timestamp.tz_localize("UTC")
                    rows.append((d.isoformat(), ts.isoformat(), float(r.open), float(r.high), float(r.low),
                                 float(r.close), float(r.volume)))
        dedup = {}
        for row in rows:                       # one bar per session (the provider never sends two; guard anyway)
            dedup.setdefault(row[0], row)
        out[sym] = store.insert_dataset(sym, SOURCE, feed(), ADJUSTMENT, fetch_start.isoformat(), req_end.isoformat(),
                                        list(dedup.values()), fetched_at)
    return out
