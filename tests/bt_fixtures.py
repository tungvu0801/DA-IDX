"""Stage 3.2 test fixtures: deterministic synthetic daily bars, a temp database with saved strategy versions, and
cached datasets — no network, no Claude."""
from __future__ import annotations

import random
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import config
from backtest.bars import ADJUSTMENT, NY, SOURCE, feed
from backtest.store import BacktestStore
from strategy import features as F
from strategy.store import StrategyStore

HOLIDAYS = {date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18), date(2025, 5, 26),
            date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
            date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2024, 11, 28), date(2024, 12, 25),
            date(2024, 9, 2), date(2024, 7, 4), date(2024, 6, 19), date(2024, 5, 27)}


def sessions(start: date, end: date) -> List[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


def ts(d: date) -> str:
    return datetime.combine(d, time(0, 0), tzinfo=NY).astimezone(timezone.utc).isoformat()


def walk(symbol: str, days: List[date], start_price: float = 100.0, drift: float = 0.0006, vol: float = 0.018,
         seed: Optional[int] = None) -> List[tuple]:
    rng = random.Random(seed if seed is not None else sum(map(ord, symbol)) * 7919)
    px, rows = start_price, []
    for d in days:
        o = px * (1 + rng.gauss(0, vol / 3))
        c = o * (1 + rng.gauss(drift, vol))
        h = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        low = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        v = float(int(1_000_000 * (1 + abs(rng.gauss(0, 0.5)))))
        rows.append((d.isoformat(), ts(d), round(o, 4), round(h, 4), round(low, 4), round(c, 4), v))
        px = c
    return rows


def rows_from(prices: Dict[date, tuple]) -> List[tuple]:
    """Explicit bars: {date: (open, high, low, close[, volume])}."""
    out = []
    for d in sorted(prices):
        o, h, low, c, *v = prices[d]
        out.append((d.isoformat(), ts(d), float(o), float(h), float(low), float(c), float(v[0] if v else 1_000_000)))
    return out


def spec(symbols=("AMD", "MU", "NVDA"), entry=None, exit_=None, risk=None, name="Test strategy") -> dict:
    return {"schema_version": 1, "feature_registry_version": F.REGISTRY_VERSION,
            "feature_registry_fingerprint": F.fingerprint(), "name": name, "description": "", "direction": "LONG_ONLY",
            "timeframe": "1D", "execution": {"decision": "AT_CLOSE", "fill": "NEXT_SESSION_OPEN"},
            "universe": {"type": "EXPLICIT_SYMBOLS", "symbols": list(symbols), "origin": "MANUAL"},
            "entry": entry or {"logic": "ALL", "conditions": [{"feature": "stock.trend", "op": "==", "value": "UPTREND"}]},
            "exit": exit_ or {"logic": "ANY", "conditions": [], "invalidation": {"method": "PCT_BELOW_ENTRY", "pct": 5},
                              "target": {"method": "PCT_ABOVE_ENTRY", "pct": 10}, "max_holding_days": 10},
            "risk": risk or {"max_position_pct": 20, "max_open_positions": 3}}


class Lab:
    """A throwaway database with the Stage 3.1 + 3.2 tables."""

    def __init__(self):
        self.path = Path(tempfile.mkdtemp(prefix="bt32-")) / "bt.db"
        self.strategies = StrategyStore(self.path)
        self.store = BacktestStore(self.path)

    def save(self, s: dict) -> dict:
        return self.strategies.create(s)

    def cache(self, symbol: str, rows: List[tuple], requested_start: str = "2000-01-01",
              requested_end: str = "2026-09-25", fetched_at: str = "2026-09-26T00:00:00+00:00") -> dict:
        return self.store.insert_dataset(symbol, SOURCE, feed(), ADJUSTMENT, requested_start, requested_end, rows, fetched_at)


def body(sid: str, version: int = 1, start: str = "2025-06-02", end: str = "2026-09-25", **kw) -> dict:
    return {"strategy_id": sid, "version_number": version, "start_date": start, "end_date": end,
            "initial_equity": 100000.0, "slippage_bps_per_side": 0.0, "commission_per_order": 0.0, **kw}


NOW = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
SPY = config.MARKET_PROXY_SYMBOL
