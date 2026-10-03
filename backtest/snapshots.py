"""
backtest/snapshots.py — point-in-time FEATURE SNAPSHOTS for (symbol, decision day T).

No backtest formulas. Every value comes from the code the live app runs, fed by backtest.replay.ReplayClient
(bars through the close of T only):

  stock   scanner.market_scanner.analyze_symbols -> data.market_data.fetch_daily_bars -> analysis.indicators
          .compute_metrics (no latest-trade override: price = the close of T) -> strategy.features.stock_values
  market  analyze_symbols for SPY / QQQ / SOXX and (only when needed) the fixed 80-stock breadth list ->
          insights.market.MarketInputs(macro_events=[], news=[], scanner_counts=None) -> build_market_insights ->
          strategy.features.market_values
  sector  analysis.sector_context.compute_sector_context(symbol, pct_change, replay) + the sector ETF's metrics ->
          strategy.features.sector_values

Warm-up: each feature needs a minimum number of sessions in its point-in-time window (MIN_SESSIONS, derived from the
same config constants the indicators use). With fewer, the value is withheld (None) and marked INSUFFICIENT_HISTORY,
so a condition on it is "not met" (strategy.evaluate never passes a missing value). The table lives here, not in the
Stage 3.1 registry, so registry fingerprints and strategy hashes are untouched.

Performance: days are independent, so large runs split the dates across a one-shot local process pool (spawned for
the run and closed after it). The result is identical to the in-process path (tested); only elapsed time differs.
"""
from __future__ import annotations

import math
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import config
from backtest.replay import BarSeries, ReplayClient, session_close
from data.sector_map import SECTOR_MAP, get_sector_for_symbol
from strategy import features as F
from strategy import spec as S

SPY = config.MARKET_PROXY_SYMBOL
QQQ = config.MARKET_TECH_PROXY_SYMBOL
SOXX = SECTOR_MAP["Semiconductors"]["etf"]

# minimum sessions in the point-in-time window (including T) before a feature is considered computable
_TREND = config.EMA_SLOW
_RSI = config.RSI_PERIOD + 1
_SR = config.SUPPORT_RESISTANCE_LOOKBACK_DAYS
MIN_SESSIONS: Dict[str, int] = {
    "stock.trend": _TREND, "stock.signal": _TREND,
    "stock.momentum": _RSI, "stock.momentum_score": _RSI, "stock.rsi_14": _RSI,
    "stock.extended": max(_RSI, config.MOMENTUM_5D_DAYS + 1),
    "stock.change_1d_pct": 2, "stock.gap_pct": 2, "stock.close": 2,
    "stock.momentum_5d_pct": config.MOMENTUM_5D_DAYS + 1, "stock.momentum_10d_pct": config.MOMENTUM_10D_DAYS + 1,
    "stock.relative_volume": config.VOLUME_AVG_LOOKBACK_DAYS + 1, "stock.volume_level": config.VOLUME_AVG_LOOKBACK_DAYS + 1,
    "stock.volume_expansion": config.VOLUME_EXPANSION_LONG_DAYS,
    "stock.atr_14": config.ATR_PERIOD + 1,
    "stock.volatility_20d_pct": config.VOLATILITY_LOOKBACK_DAYS + 1,
    "stock.volatility_expansion": config.VOLATILITY_LOOKBACK_DAYS + 1,
    "stock.price_location": _SR, "stock.support": _SR, "stock.resistance": _SR,
    "stock.dist_from_support_pct": _SR, "stock.dist_from_resistance_pct": _SR,
    "stock.dist_from_20d_high_pct": config.HIGH_LOW_LOOKBACK_DAYS, "stock.dist_from_20d_low_pct": config.HIGH_LOW_LOOKBACK_DAYS,
    # market labels are built from SPY's (and QQQ's / SOXX's) trend and volatility
    "market.trend": _TREND, "market.environment": _TREND, "market.volatility": _TREND, "market.breadth": _TREND,
    "market.breadth_advancing_pct": _TREND, "market.risk_appetite": _TREND, "market.spy_trend": _TREND,
    "market.qqq_trend": _TREND, "market.soxx_trend": _TREND,
    "sector.context": 2, "sector.stock_vs_sector_pct": 2, "sector.etf_trend": _TREND,
}
BASKET_FEATURES = {"market.environment", "market.breadth", "market.breadth_advancing_pct", "market.risk_appetite"}
QQQ_FEATURES = {"market.qqq_trend", "market.risk_appetite"}
SOXX_FEATURES = {"market.soxx_trend"}
ENTRY_LEVELS = ("stock.close", "stock.resistance", "stock.support")   # always recorded (exit methods + audit)
POOL_MIN_UNITS = 1500          # compute_metrics calls below which a process pool is not worth its start-up time
                               # (larger in-process runs also slow the server's other requests while they hold the GIL)


@dataclass(frozen=True)
class Needs:
    universe: Tuple[str, ...]
    stock_features: Tuple[str, ...]
    market_features: Tuple[str, ...]
    sector_features: Tuple[str, ...]
    index_symbols: Tuple[str, ...]
    basket: Tuple[str, ...]
    sector_etfs: Tuple[Tuple[str, str], ...]          # (stock, sector ETF) for mapped universe symbols

    def symbols(self) -> Tuple[str, ...]:
        return tuple(sorted({*self.universe, *self.index_symbols, *self.basket, *(e for _, e in self.sector_etfs)}))

    def context_symbols(self) -> Tuple[str, ...]:
        return tuple(s for s in self.symbols() if s not in self.universe)

    def work_units(self, n_dates: int) -> int:
        return n_dates * (len(self.universe) + len(self.index_symbols) + len(self.basket) + 3 * len(self.sector_etfs))

    def public(self) -> dict:
        return asdict(self)


def needs_for(spec: dict) -> Needs:
    used = [fid for fid, _ in S.features_used(spec)]
    for fid in used:
        f = F.get(fid)
        if f is None or not f.historical_support:
            raise ValueError(f"{fid} cannot be rebuilt historically")
    universe = tuple(spec["universe"]["symbols"])
    stock = sorted({f for f in used if F.get(f).scope == "STOCK"} | set(ENTRY_LEVELS))
    market_used = {f for f in used if F.get(f).scope == "MARKET"}
    basket_needed = bool(market_used & BASKET_FEATURES)
    market = sorted(market_used | {"market.trend"} | ({"market.environment"} if basket_needed else set()))
    sector = sorted(f for f in used if F.get(f).scope == "SECTOR")
    idx = {SPY} | ({QQQ} if set(market) & QQQ_FEATURES else set()) | ({SOXX} if set(market) & SOXX_FEATURES else set())
    etfs = tuple((s, SECTOR_MAP[get_sector_for_symbol(s)]["etf"]) for s in universe if get_sector_for_symbol(s)) if sector else ()
    return Needs(universe, tuple(stock), tuple(market), tuple(sector), tuple(sorted(idx)),
                 tuple(config.FALLBACK_UNIVERSE) if basket_needed else (), etfs)


def _plain(v):
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _cell(value, n: int, need: int) -> dict:
    value = _plain(value)
    if n < need:
        return {"v": None, "a": "INSUFFICIENT_HISTORY"}
    if value is None or value == "UNAVAILABLE":
        return {"v": None, "a": "UNAVAILABLE"}
    return {"v": value, "a": "AVAILABLE"}


def day_snapshot(series: Dict[str, BarSeries], needs: Needs, T: date) -> dict:
    """Everything any rule may read at the close of T, computed from bars dated <= T only."""
    from analysis.sector_context import compute_sector_context
    from insights.market import MarketInputs, build_market_insights
    from scanner.market_scanner import analyze_symbols
    rc = ReplayClient(series, T)
    close_dt = session_close(T)
    wl = {s: (series[s].window_len(T) if s in series else 0) for s in needs.symbols()}

    idx = analyze_symbols(rc, list(needs.index_symbols))
    basket = list(analyze_symbols(rc, list(needs.basket)).values()) if needs.basket else []
    inp = MarketInputs(indices={s: idx.get(s) for s in needs.index_symbols}, basket=basket,
                       basket_requested=len(config.FALLBACK_UNIVERSE), scanner_counts=None, sector_etf_changes={},
                       macro_events=[], news=[], fetched_at=close_dt, errors=[])
    mv = F.market_values(build_market_insights(inp, close_dt))
    market = {}
    for fid in needs.market_features:
        syms = {SPY} | ({QQQ} if fid in QQQ_FEATURES else set()) | ({SOXX} if fid in SOXX_FEATURES else set())
        market[fid] = _cell(mv.get(fid), min(wl.get(s, 0) for s in syms), MIN_SESSIONS[fid])
    breadth_members = len(basket) if needs.basket else None

    stocks = analyze_symbols(rc, list(needs.universe))
    etf_of = dict(needs.sector_etfs)
    etf_metrics = analyze_symbols(rc, sorted(set(etf_of.values()))) if needs.sector_features and etf_of else {}
    out = {}
    for sym in needs.universe:
        m = stocks.get(sym)
        if m is None:                       # no bar dated T (or < 2 bars): this symbol is not evaluated at T
            continue
        n = wl[sym]
        sv = F.stock_values(m)
        cells = {fid: _cell(sv.get(fid), n, MIN_SESSIONS[fid]) for fid in needs.stock_features}
        if needs.sector_features:
            etf = etf_of.get(sym)
            if etf is None:
                cells.update({fid: {"v": None, "a": "NO_SECTOR_MAPPING"} for fid in needs.sector_features})
            elif etf_metrics.get(etf) is None:
                cells.update({fid: {"v": None, "a": "CONTEXT_DATA_MISSING"} for fid in needs.sector_features})
            else:
                secv = F.sector_values(compute_sector_context(sym, m.pct_change, rc), etf_metrics[etf])
                for fid in needs.sector_features:
                    cells[fid] = _cell(secv.get(fid), min(n, wl.get(etf, 0)), MIN_SESSIONS[fid])
        out[sym] = {"n": n, "as_of": m.as_of.isoformat(), "cells": cells}
    return {"T": T.isoformat(), "evaluated_at": close_dt.isoformat(), "market": market, "symbols": out,
            "spy_sessions": wl.get(SPY, 0), "breadth_members": breadth_members,
            "replay": {"requests": rc.requests, "violations": rc.violations}}


# ---- series loading (read-only; also used by pool workers, which must never write) ---------------------------------

def load_series(db_path: str, datasets: Dict[str, str]) -> Dict[str, BarSeries]:
    conn = sqlite3.connect(f"file:{db_path.replace(os.sep, '/')}?mode=ro", uri=True, timeout=10.0)
    try:
        out = {}
        for sym, ds in datasets.items():
            rows = conn.execute("SELECT session_date, bar_timestamp, open, high, low, close, volume FROM "
                                "historical_daily_bars WHERE dataset_id = ? ORDER BY session_date", (ds,)).fetchall()
            out[sym] = BarSeries(sym, rows)
        return out
    finally:
        conn.close()


_W: dict = {}


def _init_worker(db_path: str, datasets: Dict[str, str], needs: dict) -> None:
    _W["series"] = load_series(db_path, datasets)
    _W["needs"] = Needs(**{k: tuple(tuple(x) if isinstance(x, list) else x for x in v) for k, v in needs.items()})


def _run_chunk(days: List[str]) -> Dict[str, dict]:
    return {d: day_snapshot(_W["series"], _W["needs"], date.fromisoformat(d)) for d in days}


def default_workers() -> int:
    raw = os.getenv("BACKTEST_WORKERS")
    if raw is not None and raw.strip().isdigit():
        return int(raw)
    return max(1, min(8, (os.cpu_count() or 2) - 2))


def compute_snapshots(db_path: str, datasets: Dict[str, str], series: Dict[str, BarSeries], needs: Needs,
                      days: Sequence[date], workers: Optional[int] = None,
                      progress: Optional[Callable[[int, int], None]] = None) -> Tuple[Dict[date, dict], dict]:
    """{T: day_snapshot} for every session, plus how it was computed. Pool or in-process: identical values."""
    workers = default_workers() if workers is None else workers
    days = list(days)
    total = len(days)
    use_pool = workers > 1 and needs.work_units(total) >= POOL_MIN_UNITS and total >= workers * 2
    out: Dict[date, dict] = {}
    fallback = None
    if use_pool:
        import multiprocessing
        n_chunks = min(total, workers * 4)
        chunks = [[d.isoformat() for d in days[i::n_chunks]] for i in range(n_chunks)]
        try:
            with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                     initializer=_init_worker, initargs=(db_path, datasets, needs.public())) as ex:
                futs = [ex.submit(_run_chunk, c) for c in chunks]
                for fut in as_completed(futs):
                    for d, snap in fut.result().items():
                        out[date.fromisoformat(d)] = snap
                    if progress:
                        progress(len(out), total)
            return out, {"mode": "process_pool", "workers": workers, "chunks": n_chunks}
        except Exception as exc:  # noqa: BLE001 - a pool that cannot start falls back to the identical in-process path
            out.clear()
            fallback = type(exc).__name__
    for i, d in enumerate(days, 1):
        out[d] = day_snapshot(series, needs, d)
        if progress and (i % 10 == 0 or i == total):
            progress(i, total)
    return out, {"mode": "in_process", "workers": 1, **({"pool_fallback": fallback} if fallback else {})}
