"""
rotation_backtest/runner.py — orchestration: resolve the rotation configuration version and the universe, canonicalise
the backtest definition, load historical bars through the EXISTING read-only Stage 3.2 bar layer (≤ 1 batched market-data
request), replay with the Stage 4.7 engine, compute metrics, persist atomically. Research only: no broker, no snapshot
refresh, no model call.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

from backtest import bars as B
from backtest.replay import BarSeries
from fit import current as FC
from fit import readonly as RO
from rotation import engine as E
from rotation import universe as U
from rotation.store import RotationStore, canonical_json, sha256_hex

from rotation_backtest import ENGINE_VERSION
from rotation_backtest import config as C
from rotation_backtest import metrics as MX
from rotation_backtest import simulator as SIM
from rotation_backtest.store import PortfolioBacktestStore

HISTORY_CALENDAR_DAYS = E.HISTORY_CALENDAR_DAYS                 # 420: 252 completed sessions before the first signal


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def bar_hashes(series: Dict[str, BarSeries], end: date) -> Dict[str, Optional[str]]:
    return {s: E._bars_hash(series.get(s), end) for s in sorted(series)}


def run_backtest(store: PortfolioBacktestStore, body: dict, *, now: Optional[datetime] = None, path: Optional[Path] = None,
                 cache: Optional[FC.BarCache] = None, fetch_fn=None, client=None, watchlist_fn=None, persist: bool = True) -> dict:
    """Validate → resolve → load bars → simulate → metrics → persist. Raises BacktestConfigError / UniverseError for a
    pre-run input problem (nothing persisted); a data problem during the replay is a persisted FAILED run."""
    now = now or datetime.now(timezone.utc)
    path = Path(path) if path else store.path
    rot = RotationStore(path)
    cfg = rot.config(str(body.get("config_id") or ""))
    if cfg is None:
        raise C.BacktestConfigError("INVALID_CONFIG", "Unknown rotation configuration.", 404)
    if body.get("config_hash") and body["config_hash"] != cfg["config_hash"]:
        raise C.BacktestConfigError("CONFIG_HASH_MISMATCH", "The configuration hash does not match the stored version.", 409)
    uspec = body.get("universe") or {}
    universe = U.resolve_universe(uspec.get("source"), uspec.get("ref"), uspec.get("symbols"), path=path, watchlist_fn=watchlist_fn)
    canon = C.normalise(body, cfg, universe)
    last_complete = B.last_complete_session_date(now)
    end = date.fromisoformat(canon["end_date"])
    if end > last_complete:
        raise C.BacktestConfigError("INVALID_DATE_RANGE", f"end_date must be a completed session (on or before {last_complete.isoformat()}).")
    start = date.fromisoformat(canon["start_date"])
    symbols = sorted(set(universe.symbols) | {SIM.BENCHMARK})
    try:
        series, _prov, fetched = FC.load_bars(RO.ReadOnlyBacktestStore(path), symbols, start - timedelta(days=HISTORY_CALENDAR_DAYS), end, now,
                                              cache if cache is not None else FC.BAR_CACHE, fetch_fn=fetch_fn, client=client)
    except Exception as exc:  # noqa: BLE001 - market data unavailable: a persisted FAILED run, type name only
        series, fetched = {}, {"requests": 0}
        res = SIM.SimulationResult(status="FAILED", failure_code="DATA_UNAVAILABLE", failure_detail=f"Market data unavailable ({type(exc).__name__}).")
    else:
        res = SIM.simulate(cfg, universe, series, canon, now=now)
    requests = int(fetched.get("requests", 0))
    hashes = bar_hashes(series, end)
    metrics = None
    if res.status == "COMPLETED":
        metrics = MX.compute(canon, res.equity, res.trades, res.rebalances, res.completed_positions, res.total_costs)
    run = {"backtest_config_hash": C.definition_hash(canon), "config_id": cfg["config_id"], "config_hash": cfg["config_hash"],
           "universe_hash": universe.universe_hash, "engine_version": ENGINE_VERSION, "status": res.status, "failure_code": res.failure_code,
           "failure_detail": res.failure_detail, "run_at": _iso(now), "completed_at": _iso(now),
           "first_session": res.sessions[0].isoformat() if res.sessions else None, "last_session": res.sessions[-1].isoformat() if res.sessions else None,
           "n_sessions": len(res.equity), "n_rebalances": len(res.rebalances), "n_rebalances_executed": sum(1 for r in res.rebalances if r["executed"]),
           "n_trades": len(res.trades), "initial_cash": canon["initial_cash"],
           "final_equity": res.equity[-1]["equity"] if res.status == "COMPLETED" and res.equity else None,
           "final_cash": res.final_cash if res.status == "COMPLETED" else None, "total_costs": res.total_costs if res.status == "COMPLETED" else None,
           "data_hash": sha256_hex(canonical_json(hashes)), "bars_json": hashes, "result_hash": res.result_hash(), "market_data_requests": requests,
           "universe_note": C.UNIVERSE_NOTE}
    out = {"definition": C.public(canon), "run": run, "rebalances": res.rebalances, "trades": res.trades, "equity": res.equity, "metrics": metrics,
           "conventions": MX.CONVENTIONS, "final_holdings": res.final_holdings}
    if persist:
        cfg_row = store.ensure_config(canon, now)
        run["backtest_config_id"] = cfg_row["backtest_config_id"]
        run["run_id"] = store.insert_run(run, res.rebalances, res.trades, res.equity, metrics)
    return out
