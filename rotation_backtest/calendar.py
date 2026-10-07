"""
rotation_backtest/calendar.py — the session calendar and the deterministic rebalance schedule (DESIGN_48 §5).

Sessions are the benchmark's (SPY) bar dates inside [start, end]. WEEKLY = the first session of each ISO week; MONTHLY =
the first session of each calendar month. A schedule date is a SIGNAL session T; the trade happens at the open of the
next session in the calendar (never at T's close).
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional, Sequence

from backtest.replay import BarSeries

WEEKLY, MONTHLY = "WEEKLY", "MONTHLY"


def sessions_in_range(benchmark: BarSeries, start: date, end: date) -> List[date]:
    return [d for d in benchmark.dates if start <= d <= end]


def schedule(sessions: Sequence[date], frequency: str) -> List[date]:
    """The signal sessions: the first session of each ISO week (WEEKLY) or calendar month (MONTHLY)."""
    if frequency == WEEKLY:
        key = lambda d: d.isocalendar()[:2]          # noqa: E731 - (ISO year, ISO week)
    elif frequency == MONTHLY:
        key = lambda d: (d.year, d.month)            # noqa: E731
    else:
        raise ValueError(f"unknown rebalance frequency {frequency!r}")
    out, seen = [], set()
    for d in sessions:
        k = key(d)
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def next_session(sessions: Sequence[date], T: date) -> Optional[date]:
    """The first session strictly after T inside the range, or None."""
    for d in sessions:
        if d > T:
            return d
    return None
