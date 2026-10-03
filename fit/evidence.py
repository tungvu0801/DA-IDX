"""
fit/evidence.py — STORED evidence for one exact strategy version, read-only and never recomputed.

Historical evidence = Stage 3.2 backtest runs; forward evidence = Stage 3.3 forward journals. They are different kinds
of evidence and are returned separately. Nothing here merges them, averages runs, picks a "best" run or turns them into
a score, and neither can change a Strategy Fit result. Every join uses the exact strategy_version_id.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from backtest.store import BacktestStore
from fit.readonly import ReadOnlyBacktestStore, ReadOnlyForwardStore
from forward import journal as J

HISTORICAL_NOTE = ("Stored Stage 3.2 runs of this exact version, each with its own period, costs and data. Metrics are "
                   "never combined across runs, and past results do not change the current rule fit.")
FORWARD_NOTE = ("Stored Stage 3.3 observations of this exact version (captured one completed close at a time). Nothing "
                "is recalculated here, and they do not change the current rule fit.")


def historical(store: ReadOnlyBacktestStore, strategy_version_id: str, spec_hash: str) -> dict:
    """Every stored backtest run of the version, newest first, each summarised on its own."""
    try:
        with store._connect() as conn:
            rows = conn.execute("SELECT * FROM backtest_runs WHERE strategy_version_id = ? ORDER BY created_at DESC, "
                                "rowid DESC", (strategy_version_id,)).fetchall()
    except sqlite3.OperationalError:                    # backtesting was never used in this database
        return {"available": False, "count": 0, "completed": 0, "runs": [], "most_recent_completed_run_id": None,
                "note": HISTORICAL_NOTE}
    runs = []
    for r in rows:
        d = BacktestStore._run(r, full=False)          # the Stage 3.2 summary shape (this run's own metrics)
        cfg = d.get("config") or {}
        runs.append({"run_id": d["run_id"], "status": d["status"], "created_at": d["created_at"],
                     "finished_at": d.get("finished_at"), "period": {"start": d["start_date"], "end": d["end_date"]},
                     "initial_equity": (cfg.get("capital") or {}).get("initial_equity"),
                     "slippage_bps_per_side": (cfg.get("costs") or {}).get("slippage_bps_per_side"),
                     "commission_per_order": (cfg.get("costs") or {}).get("commission_per_order"),
                     "data_hash": d.get("data_hash"), "engine_version": d.get("engine_version"),
                     "spec_hash_matches": d.get("spec_hash") == spec_hash, "point_in_time_safe": d.get("point_in_time_safe"),
                     "error_code": d.get("error_code"), "metrics": d.get("summary")})
    completed = [x for x in runs if x["status"] == "COMPLETED"]
    return {"available": True, "count": len(runs), "completed": len(completed), "runs": runs,
            "most_recent_completed_run_id": completed[0]["run_id"] if completed else None, "note": HISTORICAL_NOTE}


def forward(store: ReadOnlyForwardStore, strategy_version_id: str, symbol: str) -> dict:
    """The version's forward journal (the active one, else the most recent archived one) and, for the selected
    symbol, its latest stored decision, shadow state and completed reference cycles."""
    try:
        with store._connect() as conn:
            ids = [(r[0], r[1]) for r in conn.execute(
                "SELECT journal_id, status FROM forward_test_journals WHERE strategy_version_id = ? "
                "ORDER BY created_at DESC, rowid DESC", (strategy_version_id,))]
    except sqlite3.OperationalError:                    # forward journals were never used in this database
        return {"available": False, "journals": 0, "archived_journals": 0, "journal": None, "symbol": None,
                "note": FORWARD_NOTE}
    out = {"available": True, "journals": len(ids), "archived_journals": sum(1 for _, s in ids if s == "ARCHIVED"),
           "journal": None, "symbol": None, "note": FORWARD_NOTE}
    if not ids:
        return out
    jid = next((j for j, s in ids if s != "ARCHIVED"), ids[0][0])
    j = store.journal(jid)
    sessions = store.sessions(jid)
    obs = store.observations(jid, compact=True)
    fills = store.fills(jid)
    latest = store.latest_observations(jid) if obs else {}
    summ = J._summary(sessions, obs, fills, latest)     # the Stage 3.3 journal summary, unchanged
    out["journal"] = {"journal_id": jid, "status": j["status"], "created_at": j["created_at"],
                      "forward_start_date": j["forward_start_date"], "archived_at": j["archived_at"],
                      "continuity": summ["continuity"], "sessions_captured": summ["sessions_captured"],
                      "sessions_missed": summ["sessions_missed"], "latest_captured_session": summ["latest_captured_session"],
                      "universe_includes_symbol": symbol in (j.get("config") or {}).get("universe", [])}
    o: Optional[dict] = latest.get(symbol)
    if o is not None:
        lc = o.get("lifecycle") or {}
        out["symbol"] = {"latest_session": o["session_date"], "decision": o["decision"], "state_before": o["state_before"],
                         "state_after": o["state_after"], "reason_code": o["reason_code"],
                         "evaluated_side": o["evaluated_side"], "rules_met": o["rules_met"], "rules_total": o["rules_total"],
                         "completed_reference_cycles": sum(1 for f in fills if f["symbol"] == symbol and
                                                           f["fill_type"] == "EXIT" and f["status"] == "FILLED"),
                         "open_cycle": ({k: lc.get(k) for k in ("cycle_no", "entry_signal_session", "entry_fill_session",
                                                                 "reference_entry_open", "holding_sessions",
                                                                 "exit_signal_session")}
                                        if o["state_after"] in (J.ENTRY_PENDING, J.OPEN, J.EXIT_PENDING) else None)}
    return out
