"""Stage 3.3 test fixtures: a deterministic fake Alpaca market-data provider (explicit bars, split restatement, a
partial "today" bar), a fake saved-research table, fake Stage 2.6 event providers, and a throwaway database with the
Stage 3.1 + 3.2 + 3.3 tables. No network, no Claude."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional

import pandas as pd

import bt_fixtures as X
from backtest.bars import NY
from forward import journal as J
from forward.store import ForwardStore

D = date.fromisoformat


def frames(rows_by_sym: Dict[str, List[tuple]]) -> Dict[str, pd.DataFrame]:
    return {s: pd.DataFrame({"timestamp": pd.to_datetime([r[1] for r in rows], utc=True), "open": [r[2] for r in rows],
                             "high": [r[3] for r in rows], "low": [r[4] for r in rows], "close": [r[5] for r in rows],
                             "volume": [r[6] for r in rows]}) for s, rows in rows_by_sym.items() if rows}


class Market:
    """Fake market data. `fetch` has the signature of data.market_data.fetch_daily_bars; backtest.bars.fetch_and_store
    keeps only bars dated before "today" (New York) of the capture clock."""

    def __init__(self, symbols: Iterable[str] = ("AMD", "MU", "NVDA", "SPY"), start: date = date(2025, 11, 3),
                 end: date = date(2026, 12, 31), vol: float = 0.018, skip: Iterable[date] = ()):
        skip = set(skip)
        self.days = [d for d in X.sessions(start, end) if d not in skip]
        self.rows: Dict[str, Dict[date, tuple]] = {}
        for s in symbols:
            self.rows[s] = {D(r[0]): r for r in X.walk(s, self.days, vol=vol)}
        self.calls: List[tuple] = []
        self.split: Optional[tuple] = None          # (symbol, ex_date, ratio): the walk is the ADJUSTED series; before
        self.fail = False                           # the ex-date is visible the provider returns pre-split prices
        self.now: Optional[datetime] = None

    def set_bar(self, sym: str, d: date, o: float, h: float, low: float, c: float, v: float = 1_000_000.0) -> None:
        self.rows[sym][d] = (d.isoformat(), X.ts(d), float(o), float(h), float(low), float(c), float(v))

    def drop(self, sym: str, d: date) -> None:
        self.rows[sym].pop(d, None)

    def mutate_after(self, T: date) -> None:
        for sym, rows in self.rows.items():
            for d, r in list(rows.items()):
                if d > T:
                    rows[d] = (r[0], r[1], r[2] * 3.0, r[3] * 9.0, r[4] * 0.2, r[5] * 0.1, r[6] * 50)

    def fetch(self, client, symbols, lookback_days):
        self.calls.append(tuple(symbols))
        if self.fail:
            return {}
        out = {}
        for s in symbols:
            rows = [self.rows[s][d] for d in sorted(self.rows.get(s, {}))]
            if self.split and self.split[0] == s:
                _, ex, ratio = self.split
                if not (self.now and self.now.astimezone(NY).date() > ex):      # split not happened yet: raw prices
                    rows = [(r[0], r[1], r[2] * ratio, r[3] * ratio, r[4] * ratio, r[5] * ratio, r[6] / ratio) for r in rows]
            out[s] = rows
        return frames(out)


class ResearchDB:
    """Stands in for database.database.ResearchDatabase.list_snapshots (latest first)."""

    def __init__(self):
        self.snaps: Dict[str, list] = {}

    def add(self, sym: str, created: datetime, view: str = "BULLISH BIAS", catalyst: float = 0.4, bull: int = 55,
            sid: int = 1):
        self.snaps.setdefault(sym, []).insert(0, SimpleNamespace(
            id=sid, symbol=sym, created_at=created, research_view=view, bullish_pct=bull, neutral_pct=100 - bull - 10,
            bearish_pct=10, evidence_technical=0.3, evidence_catalyst=catalyst, evidence_risk=0.0, evidence_market=0.1,
            evidence_sector=0.1, data_quality_level="HIGH", fingerprint=f"fp-{sym}-{sid}",
            market_timestamp=created.date().isoformat(), price_timestamp=created, research_schema_version="test"))

    def list_snapshots(self, symbol=None, limit=100, **kw):
        return list(self.snaps.get(symbol, []))[:limit]


COMPLETE = {"fred": {"CPI": True, "PPI": True, "EMPLOYMENT_SITUATION": True, "PCE": True}, "fomc": True, "corporate": True}


class Events:
    def __init__(self):
        self.level: Dict[str, str] = {}
        self.earnings = False
        self.coverage = dict(COMPLETE)
        self.macro: list = []
        self.raise_for: set = set()
        self.calls: List[str] = []

    def build(self, sym):
        self.calls.append(sym)
        if sym in self.raise_for:
            raise RuntimeError("provider down")
        return SimpleNamespace(event_risk_level=self.level.get(sym, "NONE"), earnings_available=self.earnings,
                               earnings_reason=None if self.earnings else "Earnings provider unavailable.",
                               data_quality="HIGH", nearest_event=None, macro_events=list(self.macro), company_events=[])

    def cov(self, sym):
        return self.coverage


def at(T: date, hour_utc: int = 12) -> datetime:
    """A capture clock on the calendar day after T: T is then the latest completed session (before the next open)."""
    return datetime.combine(T + timedelta(days=1), time(hour_utc, 0), tzinfo=timezone.utc)


def ny(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=NY).astimezone(timezone.utc)


class FLab(X.Lab):
    def __init__(self, market: Optional[Market] = None):
        super().__init__()
        self.fs = ForwardStore(self.path)
        self.market = market or Market()
        self.research = ResearchDB()
        self.events = Events()

    def journal(self, spec: dict, created: datetime) -> dict:
        sid = self.save(spec)["strategy_id"]
        return J.create_journal(self.fs, self.store, sid, 1, now=created)

    def kw(self) -> dict:
        return {"fetch_fn": self.market.fetch, "client": object(), "research_db": lambda: self.research}

    def record(self, jid: str, now: datetime) -> dict:
        self.market.now = now
        return J.record(self.fs, self.store, jid, now=now, events_fn=self.events.build, coverage_fn=self.events.cov,
                        **self.kw())

    def preflight(self, jid: str, now: datetime) -> dict:
        self.market.now = now
        return J.preflight(self.fs, self.store, jid, now=now, **self.kw())

    def obs(self, jid: str, T: date) -> Dict[str, dict]:
        return {o["symbol"]: o for o in self.fs.observations(jid, T.isoformat())}
