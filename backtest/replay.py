"""
backtest/replay.py — point-in-time bar replay.

`ReplayClient` stands in for Alpaca's StockHistoricalDataClient INSIDE the existing, unchanged
data.market_data.fetch_daily_bars. That function (and therefore scanner.market_scanner.analyze_symbols and
analysis.sector_context.compute_sector_context) runs exactly as in the live app; the only difference is where bars
come from:

  * the replay answers "now" = the CLOSE of session T: it returns, per symbol, the bars dated within the same
    calendar lookback the live app requests (config.DAILY_BAR_LOOKBACK_DAYS, i.e. sessions dated T-119 .. T) and
    NOTHING dated after T;
  * a symbol with no bar dated T is returned as having no data for that decision (it is not evaluated), so a stale
    bar is never presented as the decision day;
  * every served window is checked to end exactly at T (`violations` must stay 0 — recorded in each run).

The request's own start/end (derived from the wall clock by fetch_daily_bars) are ignored by design; its timeframe
and adjustment are checked so an unexpected request can never be answered with the wrong kind of data.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import namedtuple
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional, Sequence

import pandas as pd

import config
from backtest.bars import NY

Bar = namedtuple("Bar", "timestamp open high low close volume")


class BarSeries:
    """One symbol's cached bars, ascending, with O(1) session lookup."""

    def __init__(self, symbol: str, rows: Sequence[tuple]):
        self.symbol = symbol
        self.dates: List[date] = [date.fromisoformat(r[0]) for r in rows]
        self.bars: List[Bar] = [Bar(pd.Timestamp(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6]))
                                for r in rows]
        self.index: Dict[date, int] = {d: i for i, d in enumerate(self.dates)}

    def __len__(self) -> int:
        return len(self.dates)

    def has(self, d: date) -> bool:
        return d in self.index

    def bar(self, d: date) -> Optional[Bar]:
        i = self.index.get(d)
        return None if i is None else self.bars[i]

    def window(self, T: date, lookback_days: int = config.DAILY_BAR_LOOKBACK_DAYS) -> Optional[List[Bar]]:
        """Bars dated T-(lookback-1) .. T, or None when there is no bar dated T."""
        i = self.index.get(T)
        if i is None:
            return None
        lo = bisect_left(self.dates, T - timedelta(days=lookback_days - 1))
        return self.bars[lo:i + 1]

    def window_len(self, T: date, lookback_days: int = config.DAILY_BAR_LOOKBACK_DAYS) -> int:
        i = self.index.get(T)
        if i is None:
            return 0
        return i + 1 - bisect_left(self.dates, T - timedelta(days=lookback_days - 1))

    def last_on_or_before(self, T: date) -> Optional[int]:
        i = bisect_left(self.dates, T + timedelta(days=1)) - 1
        return i if i >= 0 else None


def session_close(T: date) -> datetime:
    """The nominal decision time: 16:00 New York on session T (early-close days are still 'the close of T')."""
    return datetime.combine(T, time(16, 0), tzinfo=NY)


class ReplayClient:
    def __init__(self, series: Dict[str, BarSeries], T: date, lookback_days: int = config.DAILY_BAR_LOOKBACK_DAYS):
        self.series, self.T, self.lookback_days = series, T, lookback_days
        self.requests = 0
        self.violations = 0

    def get_stock_bars(self, request):
        tf = getattr(request, "timeframe", None)
        adj = getattr(getattr(request, "adjustment", None), "value", getattr(request, "adjustment", None))
        if str(getattr(tf, "unit", getattr(tf, "unit_value", ""))).lower().find("day") < 0 or getattr(tf, "amount", 1) != 1:
            raise ValueError("replay serves daily bars only")
        if str(adj).lower() != "all":
            raise ValueError("replay serves split+dividend adjusted bars only (the app's convention)")
        syms = request.symbol_or_symbols
        syms = [syms] if isinstance(syms, str) else list(syms)
        self.requests += 1
        data = {}
        for s in syms:
            ser = self.series.get(s)
            w = ser.window(self.T, self.lookback_days) if ser is not None else None
            if w:
                if ser.dates[ser.index[self.T]] != self.T or w[-1] is not ser.bars[ser.index[self.T]]:
                    self.violations += 1
                    continue
                data[s] = w
        return SimpleNamespace(data=data)
