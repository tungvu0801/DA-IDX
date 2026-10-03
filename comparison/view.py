"""
comparison/view.py — Stage 3.5 EVIDENCE COMPARISON of one exact saved strategy version (read-only, no AI, no network).

Question answered: how does the STORED forward behaviour of this exact version compare, descriptively, with its STORED
historical backtest evidence?

  * Historical evidence = ONE selected Stage 3.2 run (default: the most recent COMPLETED stored run — never the "best"
    one). Its metrics are the values the run stored; nothing is re-run or recomputed.
  * Forward evidence = ONE selected Stage 3.3 journal (default: the non-archived one; otherwise the most recent
    archived one, selectable). Counts come from the stored rows; completed reference cycles are reassembled from stored
    ENTER / reference-entry / EXIT / reference-exit rows only (comparison/cycles.py).
  * Everything joins through the exact strategy_version_id, and the version's spec_hash / rules_hash must agree with
    every evidence record. A run or journal of another version is refused (EVIDENCE_VERSION_MISMATCH); disagreeing
    hashes are reported (EVIDENCE_INTEGRITY_ERROR), never silently accepted.
  * The comparison lists only measures with the same meaning on both sides, with simple arithmetic differences in
    percentage points (never "better / worse", never a relative %). Everything else is shown on its own side only.

Database access is read-only (`mode=ro` connections, no migration). Nothing is persisted; no market data, research,
event, broker or Claude call is made. There is no score, ranking, recommendation, optimisation or forecast.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backtest import runs as R
from backtest.store import BacktestError, canonical, sha256
from comparison import cycles as CY
from fit import readonly as RO
from forward import excursions as XC
from forward import journal as J
from forward.store import ForwardError
from strategy import spec as S

ENGINE_VERSION = "3.5.0"
NOTE = ("Historical and forward evidence use different observation periods and different assumptions. This view is "
        "descriptive and does not combine them into one score.")
HISTORICAL_NOTE = ("One stored Stage 3.2 run of this exact version — a simulated backtest with its own period, capital, "
                   "costs and data. Values are the ones the run stored.")
FORWARD_NOTE = ("One stored Stage 3.3 journal of this exact version — decisions captured one completed close at a time, "
                "with reference fills at the next session's open. There was no broker execution.")
DIFFERENCE_TEXT = "difference = forward − historical, in percentage points for % measures (never a relative %)"
MFE_MAE_NOT_TRACKED = "Not tracked in Stage 3.3"
ERROR_STATUS = {"NOT_FOUND": 404, "EVIDENCE_VERSION_MISMATCH": 409, "EVIDENCE_INTEGRITY_ERROR": 409}
INTEGRITY_CODES = ("INTEGRITY_ERROR", "SCHEMA_VERSION")
FORWARD_ASSUMPTIONS = [
    ("NO_ACCOUNT_EQUITY", "No account equity, cash or portfolio — nothing is marked to market."),
    ("NO_POSITION_SIZING", "No position sizing and no position limits: every symbol is observed on its own "
                           "(the backtest applies max position % and max open positions)."),
    ("NO_COSTS", "No slippage and no commission."),
    ("NO_BROKER_FILLS", "No order and no broker fill — nothing was executed."),
    ("REFERENCE_NEXT_OPEN", J.REFERENCE_FILL_TEXT),
    ("MANUAL_CAPTURE", "Each session was captured by pressing Record latest completed close; an uncaptured session is "
                       "stored as MISSED and never reconstructed."),
]
TIMING_LABEL = {J.STRICT: "Strict-forward sessions", J.POST: "Post-close-context sessions",
                "CAPTURED_AFTER_NEXT_OPEN": "Captured after the next open"}


class CompareError(Exception):
    def __init__(self, code: str, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status or ERROR_STATUS.get(code, 400)


# ================================================================================================================
# helpers
# ================================================================================================================

def r2(x: Optional[float]) -> Optional[float]:
    """Round half-up to 2 decimals (the displayed precision)."""
    if x is None:
        return None
    return float(Decimal(repr(float(x))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def difference(forward: Optional[float], historical: Optional[float]) -> Optional[float]:
    """forward − historical, from the two DISPLAYED (2-decimal) values, so the shown arithmetic always adds up."""
    if forward is None or historical is None:
        return None
    return float(Decimal(repr(r2(forward))) - Decimal(repr(r2(historical))))


def _pp_text(d: Optional[float], unit: str) -> Optional[str]:
    if d is None:
        return None
    sign = "+" if d > 0 else "−" if d < 0 else ""
    word = "percentage points" if unit == "pp" else "sessions"
    return f"{sign}{abs(d):.2f} {word}"


def _median(xs: List[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def _version_row(path: Path, strategy_version_id: str) -> dict:
    try:
        with RO.connect(path) as conn:
            r = conn.execute("SELECT v.version_id, v.strategy_id, v.version_number, v.spec_json, v.spec_hash, v.rules_hash, "
                             "v.readiness, v.feature_registry_version, v.feature_registry_fingerprint, d.name AS first_name, "
                             "d.archived_at, (SELECT MAX(version_number) FROM strategy_versions x WHERE x.strategy_id = "
                             "v.strategy_id) AS current_version FROM strategy_versions v JOIN strategy_definitions d ON "
                             "d.strategy_id = v.strategy_id WHERE v.version_id = ?", (strategy_version_id,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r is None:
        raise CompareError("NOT_FOUND", "Strategy version not found.")
    return dict(r)


def _name(spec_json: str, fallback: str) -> str:
    try:
        return json.loads(spec_json).get("name") or fallback
    except (ValueError, AttributeError):
        return fallback


def _runs_of(path: Path, vid: str) -> List[dict]:
    """Every stored run of the version, most recent first (identity + summary only)."""
    try:
        with RO.connect(path) as conn:
            rows = conn.execute("SELECT run_id, status, created_at, finished_at, start_date, end_date, config_json, "
                                "error_code, result_json IS NOT NULL AS has_result FROM backtest_runs WHERE "
                                "strategy_version_id = ? ORDER BY created_at DESC, rowid DESC", (vid,)).fetchall()
    except sqlite3.OperationalError:                   # backtesting was never used in this database
        return []
    out = []
    for r in rows:
        try:
            cfg = json.loads(r["config_json"])
        except (TypeError, ValueError):
            cfg = {}
        out.append({"run_id": r["run_id"], "status": r["status"], "created_at": r["created_at"],
                    "finished_at": r["finished_at"], "period": {"start": r["start_date"], "end": r["end_date"]},
                    "initial_equity": (cfg.get("capital") or {}).get("initial_equity"),
                    "slippage_bps_per_side": (cfg.get("costs") or {}).get("slippage_bps_per_side"),
                    "commission_per_order": (cfg.get("costs") or {}).get("commission_per_order"),
                    "error_code": r["error_code"]})
    return out


def _journals_of(path: Path, vid: str) -> List[dict]:
    """Every journal of the version: non-archived first, then by creation date (newest first)."""
    try:
        with RO.connect(path) as conn:
            rows = conn.execute("SELECT journal_id, status, created_at, forward_start_date, archived_at FROM "
                                "forward_test_journals WHERE strategy_version_id = ? ORDER BY (status = 'ARCHIVED'), "
                                "created_at DESC, rowid DESC", (vid,)).fetchall()
            counts = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT s.journal_id, SUM(s.kind = 'CAPTURED'), SUM(s.kind = 'MISSED') FROM forward_test_sessions s JOIN "
                "forward_test_journals j ON j.journal_id = s.journal_id WHERE j.strategy_version_id = ? GROUP BY "
                "s.journal_id", (vid,))}
    except sqlite3.OperationalError:                   # forward journals were never used in this database
        return []
    return [{**dict(r), "sessions_captured": int((counts.get(r["journal_id"]) or (0, 0))[0] or 0),
             "sessions_missed": int((counts.get(r["journal_id"]) or (0, 0))[1] or 0)} for r in rows]


def _owner(path: Path, table: str, key: str, value: str) -> Optional[str]:
    try:
        with RO.connect(path) as conn:
            r = conn.execute(f"SELECT strategy_version_id FROM {table} WHERE {key} = ?", (value,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return r[0] if r else None


# ================================================================================================================
# 1. VERSION IDENTITY — Stage 3.2 integrity checks (read-only); readiness only explains a missing backtest
# ================================================================================================================

def _identity(bs, path: Path, vid: str) -> Tuple[dict, dict, List[dict]]:
    v = _version_row(path, vid)
    ref, spec, checks, errors = R.eligibility(bs, v["strategy_id"], v["version_number"])
    bad = [e for e in errors if e["code"] in INTEGRITY_CODES or e["code"] == "NOT_FOUND"]
    if ref is None or spec is None or bad:
        raise CompareError("EVIDENCE_INTEGRITY_ERROR", (bad[0]["message"] if bad else "The saved version could not be "
                           "verified.") + " Its evidence is not shown because it cannot be tied to trusted rules.")
    if ref["strategy_version_id"] != vid or ref["spec_hash"] != v["spec_hash"] or ref["rules_hash"] != v["rules_hash"]:
        raise CompareError("EVIDENCE_INTEGRITY_ERROR", "The saved version's identity does not verify.")
    warnings = []
    if any(e["code"] == "REGISTRY_MISMATCH" for e in errors):
        warnings.append({"code": "REGISTRY_CHANGED", "side": "both", "text": "The feature registry changed since this "
                         "version was saved. Its stored evidence is still shown as recorded, but the version cannot be "
                         "run or evaluated again unchanged."})
    rd = S.readiness(spec)
    name = _name(v["spec_json"], v["first_name"])
    identity = {"strategy_id": v["strategy_id"], "strategy_name": name, "strategy_version_id": vid,
                "version_number": v["version_number"], "is_current": v["version_number"] == v["current_version"],
                "current_version": v["current_version"], "strategy_archived": v["archived_at"] is not None,
                "readiness": v["readiness"], "readiness_label": S.READINESS_LABEL.get(v["readiness"], v["readiness"]),
                "readiness_explanation": rd["explanation"],
                "forward_only_features": [{"feature": r["feature"], "name": r["name"], "why": r["why"]}
                                          for r in rd["reasons"] if r["state"] == "FORWARD_ONLY"],
                "spec_hash": v["spec_hash"], "rules_hash": v["rules_hash"],
                "feature_registry_version": v["feature_registry_version"],
                "feature_registry_fingerprint": v["feature_registry_fingerprint"],
                "universe": list(spec["universe"]["symbols"]),
                "integrity": {"verified": True, "checks": [c for c in checks if c["code"] != "READINESS"]}}
    return identity, spec, warnings


def _ident_checks(rec: dict, identity: dict, what: str) -> List[dict]:
    return [{"code": f"{what}_STRATEGY_ID", "ok": rec.get("strategy_id") == identity["strategy_id"], "label": "Strategy id matches"},
            {"code": f"{what}_VERSION_ID", "ok": rec.get("strategy_version_id") == identity["strategy_version_id"],
             "label": "Exact strategy_version_id"},
            {"code": f"{what}_VERSION_NUMBER", "ok": rec.get("version_number") == identity["version_number"],
             "label": "Version number matches"},
            {"code": f"{what}_SPEC_HASH", "ok": rec.get("spec_hash") == identity["spec_hash"], "label": "spec_hash matches"},
            {"code": f"{what}_RULES_HASH", "ok": rec.get("rules_hash") == identity["rules_hash"], "label": "rules_hash matches"}]


def _integrity_block(checks: List[dict], side_note: str) -> dict:
    failed = [c for c in checks if not c["ok"]]
    return {"status": "EVIDENCE_INTEGRITY_ERROR", "message": "This evidence does not verify against the saved version ("
            + ", ".join(c["label"] for c in failed) + "). It is not shown or compared.", "checks": checks, "note": side_note}


# ================================================================================================================
# 2. HISTORICAL — one stored Stage 3.2 run, values as stored
# ================================================================================================================

METRIC_KEYS = ("initial_equity", "ending_equity", "sessions", "closed_trades", "open_positions_at_end", "total_return_pct",
               "annualized_return_pct", "annualized_note", "max_drawdown_pct", "win_rate_pct", "winning_trades",
               "losing_trades", "breakeven_trades", "average_trade_return_pct", "median_trade_return_pct",
               "average_winner_pct", "average_loser_pct", "profit_factor", "average_holding_days", "median_holding_days",
               "average_mfe_pct", "average_mae_pct", "time_in_market_pct", "realized_pnl", "unrealized_pnl",
               "total_commission", "total_slippage_impact")


def _trade_rows(bs, run_id: str) -> List[dict]:
    """The run's stored round trips — only the columns this view shows (snapshots and traces are not read)."""
    with bs._connect() as conn:
        rows = conn.execute("SELECT trade_no, status, strategy_id, strategy_version_id, spec_hash, rules_hash, symbol, "
                            "entry_signal_date, entry_fill_date, exit_signal_date, exit_fill_date, return_pct, pnl_dollars, "
                            "holding_days, mfe_pct, mae_pct, primary_exit_reason, all_exit_reasons, mark_date FROM "
                            "backtest_trades WHERE run_id = ? ORDER BY trade_no", (run_id,)).fetchall()
    return [{**dict(r), "all_exit_reasons": json.loads(r["all_exit_reasons"]) if r["all_exit_reasons"] else []} for r in rows]


def historical(bs, identity: dict, runs: List[dict], run_id: Optional[str], selection: str) -> dict:
    t0 = time.perf_counter()
    base = {"runs": runs, "selection": selection, "note": HISTORICAL_NOTE, "mfe_mae_tracked": True}
    if identity["readiness"] != S.READY and not runs:
        return {**base, "status": "UNAVAILABLE_FOR_VERSION", "selected_run": None,
                "message": "Unavailable for this strategy version.",
                "reason": ("Only BACKTEST READY versions can be backtested. This version uses data that cannot be rebuilt "
                           "for past dates (FORWARD TEST ONLY)." if identity["readiness"] == S.FORWARD_ONLY else
                           "This version uses live-account data that no strategy may use yet (UNSUPPORTED)."),
                "readiness_explanation": identity["readiness_explanation"]}
    if not runs:
        return {**base, "status": "NO_BACKTEST", "selected_run": None, "message": "No stored backtest for this version yet."}
    if run_id is None:
        return {**base, "status": "NO_COMPLETED_RUN", "selected_run": None,
                "message": "This version has stored runs, but none completed — there are no historical results to show."}
    run = bs.get_run(run_id)                          # full stored run (config, data, preflight, result)
    checks = _ident_checks(run, identity, "RUN")
    cfg = run.get("config") or {}
    cs = cfg.get("strategy") or {}
    checks.append({"code": "RUN_CONFIG_HASH", "ok": R.config_hash(cfg) == run["config_hash"],
                   "label": "Run configuration re-hashes to its config_hash"})
    checks.append({"code": "RUN_CONFIG_STRATEGY", "ok": all(cs.get(k) == identity[k] for k in (
        "strategy_id", "strategy_version_id", "version_number", "spec_hash", "rules_hash")),
                   "label": "Run configuration names this exact version"})
    trades = _trade_rows(bs, run_id) if run["status"] == "COMPLETED" else []
    checks.append({"code": "TRADES_VERSION", "ok": all(t["strategy_version_id"] == identity["strategy_version_id"]
                                                       and t["strategy_id"] == identity["strategy_id"]
                                                       and t["spec_hash"] == identity["spec_hash"]
                                                       and t["rules_hash"] == identity["rules_hash"] for t in trades),
                   "label": "Every stored trade names this exact version and hashes"})
    sel = next((x for x in runs if x["run_id"] == run_id), None)
    if run["status"] != "COMPLETED":
        if not all(c["ok"] for c in checks):
            return {**base, **_integrity_block(checks, HISTORICAL_NOTE), "runs": runs, "selection": selection, "selected_run": sel}
        return {**base, "status": "RUN_NOT_COMPLETED", "selected_run": sel, "checks": checks,
                "message": f"Run {run_id[:6]} is {run['status']}" + (f" ({run['error_code']})" if run.get("error_code") else "")
                           + " — no results were stored for it."}
    t_read = time.perf_counter()
    res = run["result"] or {}
    m = res.get("metrics") or {}
    closed = [t for t in trades if t["status"] == "CLOSED"]
    checks.append({"code": "TRADES_MATCH_METRICS", "ok": len(closed) == m.get("closed_trades")
                   and sum(1 for t in trades if t["status"] == "OPEN_AT_END") == m.get("open_positions_at_end"),
                   "label": "Stored trades match the stored trade counts"})
    if not all(c["ok"] for c in checks):
        return {**base, **_integrity_block(checks, HISTORICAL_NOTE), "runs": runs, "selection": selection, "selected_run": sel}

    bd = res.get("breakdowns") or {}
    audit = res.get("audit") or {}
    by_sym: Dict[str, dict] = {}
    for g in bd.get("symbol") or []:
        by_sym[g["group"]] = {"closed_trades": g["closed_trades"], "average_return_pct": g["average_return_pct"],
                              "win_rate_pct": g["win_rate_pct"], "average_holding_days": g["average_holding_days"],
                              "average_mfe_pct": g["average_mfe_pct"], "average_mae_pct": g["average_mae_pct"],
                              "sample": g["sample"]}
    universe = sorted(set((res.get("needs") or {}).get("universe") or identity["universe"]) | {t["symbol"] for t in trades})
    symbols = {}
    for s in universe:
        mine = [t for t in trades if t["symbol"] == s]
        row = by_sym.get(s) or {"closed_trades": 0, "average_return_pct": None, "win_rate_pct": None,
                                "average_holding_days": None, "average_mfe_pct": None, "average_mae_pct": None,
                                "sample": "NO_TRADES"}
        symbols[s] = {**row, "positive_trades": sum(1 for t in mine if t["status"] == "CLOSED" and t["pnl_dollars"] > 0),
                      "open_at_end": sum(1 for t in mine if t["status"] == "OPEN_AT_END")}
    timeline = [{k: t[k] for k in ("trade_no", "status", "symbol", "entry_signal_date", "entry_fill_date", "exit_signal_date",
                                   "exit_fill_date", "return_pct", "holding_days", "primary_exit_reason", "mark_date")}
                | {"exit_reasons": t["all_exit_reasons"]}
                for t in sorted(trades, key=lambda t: (t["entry_fill_date"], t["trade_no"]))]
    costs = cfg.get("costs") or {}
    return {**base, "status": "AVAILABLE", "selected_run": sel, "checks": checks,
            "run": {"run_id": run_id, "created_at": run["created_at"], "finished_at": run["finished_at"],
                    "engine_version": run["engine_version"], "config_hash": run["config_hash"], "data_hash": run["data_hash"],
                    "point_in_time_safe": run["point_in_time_safe"], "period": cfg.get("period")},
            "assumptions": {"period": cfg.get("period"), "sessions": m.get("sessions"),
                            "initial_equity": (cfg.get("capital") or {}).get("initial_equity"),
                            "slippage_bps_per_side": costs.get("slippage_bps_per_side"),
                            "commission_per_order": costs.get("commission_per_order"),
                            "zero_cost": costs.get("slippage_bps_per_side") == 0 and costs.get("commission_per_order") == 0,
                            "execution": cfg.get("execution"), "execution_text": (res.get("execution_model") or {}).get("text"),
                            "selection_policy": res.get("selection_policy"), "data": cfg.get("data"),
                            "risk": res.get("risk")},
            "metrics": {k: m.get(k) for k in METRIC_KEYS}, "sample": res.get("sample"),
            "formulas": res.get("formulas"),
            "exit_reasons": {"groups": [{"group": g["group"], "closed_trades": g["closed_trades"]} for g in bd.get("exit_reason") or []],
                             "appearances": bd.get("exit_reason_appearances") or {},
                             "scope": "closed trades (exit filled) of this run"},
            "entry_frequency": {"entry_evaluations": audit.get("entry_evaluations"),
                                "entry_signals": audit.get("signals_considered"),
                                "entries_filled": audit.get("signals_filled"),
                                "skipped_max_open_positions": audit.get("skipped_max_positions"),
                                "skipped_insufficient_cash": audit.get("skipped_insufficient_cash"),
                                "text": "Entry evaluations = flat symbol-sessions whose entry rules were checked; entry "
                                        "signals include signals later skipped for position limits or cash."},
            "symbol_breakdown": symbols, "trades": timeline,
            "warnings": res.get("warnings") or [],
            "timings": {"read_s": round(t_read - t0, 4), "derive_s": round(time.perf_counter() - t_read, 4)}}


# ================================================================================================================
# 3. FORWARD — one stored Stage 3.3 journal, counts from stored rows, cycles reassembled from stored rows
# ================================================================================================================

def _skip_reasons(obs: List[dict]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for o in obs:
        if o["decision"] == "SKIP":
            out[o["reason_code"]] = out.get(o["reason_code"], 0) + 1
    return dict(sorted(out.items()))


def _timeline(sessions: List[dict], obs: List[dict], fills: List[dict], after_open: set) -> List[dict]:
    by_s: Dict[str, List[dict]] = {}
    for o in obs:
        by_s.setdefault(o["session_date"], []).append(o)
    fills_by: Dict[str, List[dict]] = {}
    for f in fills:
        fills_by.setdefault(f["resolved_in_session"], []).append(f)
    out = []
    for s in sessions:                               # ascending session order
        d = s["session_date"]
        groups: Dict[str, int] = {}
        notable = []
        for o in by_s.get(d, []):
            g = J._group(o)
            groups[g] = groups.get(g, 0) + 1
            if g in ("ENTER", "EXIT", "SKIPPED", "BLOCKED"):
                notable.append({"symbol": o["symbol"], "group": g, "decision": o["decision"], "reason_code": o["reason_code"],
                                "state_after": o["state_after"], "exit_reasons": o.get("exit_reasons") or []})
        out.append({"session_date": d, "kind": s["kind"], "context_timing": s.get("context_timing"),
                    "captured_after_next_open": d in after_open, "detected_with_session": s.get("detected_with_session"),
                    "groups": dict(sorted(groups.items())), "notable": notable,
                    "fills": [{k: f[k] for k in ("symbol", "cycle_no", "fill_type", "status", "fill_session_date",
                                                 "reference_open_price", "reference_move_pct", "reason_code")}
                              for f in sorted(fills_by.get(d, []), key=lambda f: (f["symbol"], f["cycle_no"], f["fill_type"]))]})
    return out


def forward(fs, identity: dict, journals: List[dict], journal_id: Optional[str], selection: str) -> dict:
    t0 = time.perf_counter()
    base = {"journals": journals, "selection": selection, "note": FORWARD_NOTE,
            "mfe_mae": {"tracked": False, "text": MFE_MAE_NOT_TRACKED}}
    if journal_id is None:
        return {**base, "status": "NO_JOURNAL", "selected_journal": None, "message": "No forward journal yet."}
    j = fs.journal(journal_id)
    cfg = j.get("config") or {}
    checks = _ident_checks(j, identity, "JOURNAL")
    checks.append({"code": "JOURNAL_CONFIG_HASH", "ok": sha256(canonical(cfg)) == j["config_hash"],
                   "label": "Journal configuration re-hashes to its config_hash"})
    cs = cfg.get("strategy") or {}
    checks.append({"code": "JOURNAL_CONFIG_STRATEGY", "ok": all(cs.get(k) == identity[k] for k in (
        "strategy_id", "strategy_version_id", "version_number", "spec_hash", "rules_hash")),
                   "label": "Journal configuration names this exact version"})
    sessions = fs.sessions(journal_id)
    checks.append({"code": "SESSIONS_SPEC_HASH", "ok": all(s["spec_hash"] == identity["spec_hash"] for s in sessions
                                                           if s["kind"] == "CAPTURED"),
                   "label": "Every captured session used this spec_hash"})
    sel = next((x for x in journals if x["journal_id"] == journal_id), None)
    if not all(c["ok"] for c in checks):
        return {**base, **_integrity_block(checks, FORWARD_NOTE), "journals": journals, "selection": selection,
                "selected_journal": sel}
    obs = fs.observations(journal_id, compact=True)
    fills = fs.fills(journal_id)
    tracking_x = fs.excursion_tracking(journal_id)            # Stage 3.6: None for journals without excursion evidence
    x_rows = fs.excursions(journal_id) if tracking_x else []
    t_read = time.perf_counter()
    latest = CY.latest_by_symbol(obs)
    summ = J._summary(sessions, obs, fills, latest)             # the Stage 3.3 journal summary, unchanged
    try:
        cyc = CY.derive(obs, fills, summ["blocked_symbols"])
    except CY.CycleIntegrityError as exc:
        checks.append({"code": "REFERENCE_CYCLES", "ok": False, "label": f"Reference cycles assemble from stored rows ({exc})"})
        return {**base, **_integrity_block(checks, FORWARD_NOTE), "journals": journals, "selection": selection,
                "selected_journal": sel}
    checks.append({"code": "REFERENCE_CYCLES", "ok": True, "label": "Reference cycles assemble from stored rows, each "
                   "row counted once, and every stored move matches its stored prices"})
    completed = [c for c in cyc if c["status"] == CY.COMPLETED]
    if len(completed) != summ["completed_reference_cycles"]:
        checks.append({"code": "CYCLE_COUNT", "ok": False, "label": "Completed cycles match the journal summary"})
        return {**base, **_integrity_block(checks, FORWARD_NOTE), "journals": journals, "selection": selection,
                "selected_journal": sel}
    cap = [s for s in sessions if s["kind"] == "CAPTURED"]
    after_open = {s["session_date"] for s in cap if any(w.get("code") == "CAPTURED_AFTER_NEXT_OPEN" for w in (s.get("warnings") or []))}
    warn_counts: Dict[str, dict] = {}
    for s in cap:
        for w in s.get("warnings") or []:
            x = warn_counts.setdefault(w["code"], {"code": w["code"], "sessions": 0, "text": w.get("text")})
            x["sessions"] += 1
    continuity = J.BLOCKED if summ["blocked_symbols"] else summ["continuity"]
    universe = list(cfg.get("universe") or identity["universe"])
    symbols = {}
    for s in sorted(set(universe) | set(latest)):
        mine = [o for o in obs if o["symbol"] == s]
        sc = [c for c in cyc if c["symbol"] == s]
        done = [c for c in sc if c["status"] == CY.COMPLETED]
        moves = [c["reference_move_pct"] for c in done]
        lo = latest.get(s)
        open_c = next((c for c in sc if c["status"] in (CY.OPEN_CYCLE, CY.BLOCKED_CYCLE)), None)
        symbols[s] = {"observations": len(mine), "evaluated": sum(1 for o in mine if o["decision"] != "SKIP"),
                      "enter_decisions": sum(1 for o in mine if o["decision"] == "ENTER"),
                      "exit_decisions": sum(1 for o in mine if o["decision"] == "EXIT"),
                      "skip_decisions": sum(1 for o in mine if o["decision"] == "SKIP"),
                      "completed_cycles": len(done), "reference_moves_pct": moves,
                      "average_reference_move_pct": (sum(moves) / len(moves)) if moves else None,
                      "positive_cycles": sum(1 for x in moves if x > 0),
                      "average_holding_sessions": (sum(c["holding_sessions"] for c in done) / len(done))
                      if done and all(isinstance(c["holding_sessions"], int) for c in done) else None,
                      "latest_state": lo["state_after"] if lo else None, "latest_session": lo["session_date"] if lo else None,
                      "blocked": s in summ["blocked_symbols"], "open_cycle": open_c}
    met = CY.metrics(cyc)
    xblock = _excursions(tracking_x, x_rows, fills, cyc, symbols)
    return {**base, "status": "AVAILABLE", "selected_journal": sel, "checks": checks,
            "journal": {"journal_id": journal_id, "status": j["status"], "created_at": j["created_at"],
                        "created_session_date": j["created_session_date"], "forward_start_date": j["forward_start_date"],
                        "archived_at": j["archived_at"], "engine_version": j["engine_version"], "config_hash": j["config_hash"],
                        "universe": universe},
            "journal_status": j["status"], "continuity": continuity, "continuity_summary": summ["continuity"],
            "continuity_complete": continuity == "CONTINUOUS",
            "captured_sessions": summ["sessions_captured"], "missed_sessions": summ["sessions_missed"],
            "first_session": summ["first_eligible_session"], "latest_captured_session": summ["latest_captured_session"],
            "decision_counts": {"ENTER": summ["enter_signals"], "EXIT": summ["exit_signals"], "HOLD": summ["hold_decisions"],
                                "SKIP": summ["skip_decisions"], "skip_reasons": _skip_reasons(obs)},
            "states": {"open_shadow_states": summ["open_shadow_states"], "pending_entries": summ["pending_entries"],
                       "pending_exits": summ["pending_exits"], "blocked_symbols": summ["blocked_symbols"]},
            "reference_entries": {"filled": summ["reference_entries_filled"], "unfilled": summ["reference_entries_unfilled"]},
            "completed_cycles": len(completed),
            "open_cycles": sum(1 for c in cyc if c["status"] == CY.OPEN_CYCLE),
            "blocked_cycles": sum(1 for c in cyc if c["status"] == CY.BLOCKED_CYCLE),
            "unfilled_entries": sum(1 for c in cyc if c["status"] == CY.ENTRY_UNFILLED),
            "cycles": cyc, "cycle_status_text": CY.STATUS_TEXT, "reference_cycle_metrics": met,
            "sample": CY.sample(len(completed)),
            "exit_reasons": {"groups": CY.exit_reason_groups(cyc), "appearances": CY.exit_reason_appearances(cyc),
                             "pending_exit_signals": summ["pending_exits"],
                             "scope": "completed reference cycles (reference exit stored) of this journal"},
            "entry_frequency": {"enter_decisions": summ["enter_signals"],
                                "entry_evaluations": sum(1 for o in obs if o["evaluated_side"] == "ENTRY" and o["decision"] != "SKIP"),
                                "captured_sessions": summ["sessions_captured"],
                                "text": "ENTER decisions / flat symbol-sessions whose entry rules were decided / captured "
                                        "sessions. No position limits apply here, unlike the backtest."},
            "timing_breakdown": {"strict_forward": sum(1 for s in cap if s["context_timing"] == J.STRICT),
                                 "post_close_forward_context": sum(1 for s in cap if s["context_timing"] == J.POST),
                                 "captured_after_next_open": len(after_open),
                                 "labels": TIMING_LABEL, "text": J.TIMING_TEXT,
                                 "note": "Captured-after-the-next-open is an extra flag on post-close-context sessions; the "
                                         "categories are counted separately and never merged."},
            "symbol_breakdown": symbols,
            "timeline": _timeline(sessions, obs, fills, after_open),
            "assumptions": {"items": [{"code": c, "text": t} for c, t in FORWARD_ASSUMPTIONS],
                            "data": cfg.get("data"), "execution": cfg.get("execution"),
                            "continuity": continuity, "journal_status": j["status"]},
            "warnings": sorted(warn_counts.values(), key=lambda w: w["code"]),
            "timings": {"read_s": round(t_read - t0, 4), "derive_s": round(time.perf_counter() - t_read, 4)}, **xblock}


def _excursions(tracking: Optional[dict], rows: List[dict], fills: List[dict], cyc: List[dict], symbols: Dict[str, dict]) -> dict:
    """Stage 3.6 (additive): stored forward MFE / MAE. A journal without excursion evidence gets nothing here, so its
    Stage 3.5 view is unchanged ("Not tracked in Stage 3.3"). Averages use TRACKED completed cycles only — a legacy or
    incomplete cycle is never counted as 0."""
    if not tracking:
        return {}
    xs = XC.cycle_summaries(rows, fills, tracking)
    by = {(s["symbol"], s["cycle_no"]): s for s in xs}
    for c in cyc:
        c["excursion"] = by.get((c["symbol"], c["cycle_no"])) or {
            "status": XC.NOT_ENTERED, "status_text": "No reference entry, so there is nothing to track.",
            "mfe_pct": None, "mae_pct": None, "final": False}
    xm = XC.completed_metrics(xs)
    for s, row in symbols.items():
        mine = [x for x in xs if x["symbol"] == s and x["completed"] and x["status"] == XC.COMPLETE]
        row["excursion"] = {"tracked_completed_cycles": len(mine),
                            "average_mfe_pct": (sum(x["mfe_pct"] for x in mine) / len(mine)) if mine else None,
                            "average_mae_pct": (sum(x["mae_pct"] for x in mine) / len(mine)) if mine else None}
    n, m = xm["tracked_completed_cycles"], xm["completed_cycles"]
    since = tracking["activated_in_session"]
    text = (f"MFE / MAE sample: {n} tracked cycle{'' if n == 1 else 's'} of {m} completed cycle{'' if m == 1 else 's'}" if n
            else f"No completed MFE / MAE-tracked cycle yet (tracked from the {since} capture onward)")
    return {"excursion_metrics": {**xm, "tracking_since": since, "tracking_started_at": tracking["activated_at"],
                                  "engine_version": tracking["engine_version"],
                                  "legacy_note": (f"{xm['legacy_untracked_completed_cycles']} earlier completed cycle"
                                                  f"{' was' if xm['legacy_untracked_completed_cycles'] == 1 else 's were'} not "
                                                  "MFE / MAE tracked." if xm["legacy_untracked_completed_cycles"] else None)},
            "mfe_mae": {"tracked": n > 0, "text": text, "tracking_since": since}}


# ================================================================================================================
# 4. COMPARISON — compatible measures only; arithmetic differences; no verdict
# ================================================================================================================

def _row(key, label, h_label, f_label, unit, h, f, note, diff=True):
    d = difference(f, h) if diff else None
    return {"key": key, "label": label, "historical_measure": h_label, "forward_measure": f_label, "unit": unit,
            "historical": h, "forward": f, "historical_display": r2(h) if unit != "count" else h,
            "forward_display": r2(f) if unit != "count" else f,
            "difference": d, "difference_unit": ("pp" if unit == "%" else "sessions" if unit == "sessions" else None) if diff else None,
            "difference_text": _pp_text(d, "pp" if unit == "%" else "sessions") if d is not None else None,
            "semantic_note": note}


def compare(hist: dict, fwd: dict) -> dict:
    h_ok, f_ok = hist.get("status") == "AVAILABLE", fwd.get("status") == "AVAILABLE"
    m = hist.get("metrics") or {}
    fm = fwd.get("reference_cycle_metrics") if f_ok else None
    has_f = fm is not None
    rows = []
    h_n = m.get("closed_trades") if h_ok else None
    f_n = fwd.get("completed_cycles") if f_ok else None
    rows.append({**_row("sample", "Sample", "closed trades", "completed reference cycles", "count", h_n, f_n,
                        "Counts from different observation periods; not differenced.", diff=False),
                 "historical_text": None if h_n is None else f"{h_n} closed trade{'' if h_n == 1 else 's'}",
                 "forward_text": None if f_n is None else f"{f_n} completed cycle{'' if f_n == 1 else 's'}"})
    move_note = ("Historical: closed-trade return after the run's slippage and commission, on the run's position sizes. "
                 "Forward: open-to-open reference move with no costs and no sizing. Related, not identical, measures.")
    rows.append(_row("average_return", "Average return / move", "average closed-trade return", "average reference move", "%",
                     m.get("average_trade_return_pct") if h_ok else None, fm["average_reference_move_pct"] if has_f else None,
                     move_note))
    rows.append(_row("median_return", "Median return / move", "median closed-trade return", "median reference move", "%",
                     m.get("median_trade_return_pct") if h_ok else None, fm["median_reference_move_pct"] if has_f else None,
                     move_note))
    pos = _row("positive_outcomes", "Positive outcomes", "win rate (P&L > 0, after costs)",
               "positive-cycle rate (reference move > 0)", "%", m.get("win_rate_pct") if h_ok else None,
               fm["positive_cycle_rate_pct"] if has_f else None,
               "Shown with its counts. A rate from a small sample can move a lot with one more outcome; it describes "
               "stored outcomes only.")
    pos.update({"historical_count": {"positive": m.get("winning_trades"), "of": m.get("closed_trades")} if h_ok else None,
                "forward_count": {"positive": fm["positive_cycles"], "of": fm["completed_cycles"]} if has_f else None})
    rows.append(pos)
    hold_note = ("Both count the symbol's sessions from the entry-fill session (day 1) through the exit-signal session "
                 "(Stage 3.2 holding days and Stage 3.3 holding sessions use the same convention).")
    rows.append(_row("average_holding", "Average holding", "average holding days (trading sessions)",
                     "average holding sessions", "sessions", m.get("average_holding_days") if h_ok else None,
                     fm["average_holding_sessions"] if has_f else None, hold_note))
    rows.append(_row("median_holding", "Median holding", "median holding days (trading sessions)",
                     "median holding sessions", "sessions", m.get("median_holding_days") if h_ok else None,
                     fm["median_holding_sessions"] if has_f else None, hold_note))
    xm = fwd.get("excursion_metrics") if f_ok else None
    for key, label, hk in (("mfe", "Average MFE", "average_mfe_pct"), ("mae", "Average MAE", "average_mae_pct")):
        if not xm:                                   # no Stage 3.6 evidence in this journal: exactly the Stage 3.5 row
            r = _row(key, label, label.lower(), MFE_MAE_NOT_TRACKED, "%", m.get(hk) if h_ok else None, None,
                     "Stage 3.3 does not store forward MFE / MAE; nothing is reconstructed from later data.", diff=False)
            r["forward_text"] = MFE_MAE_NOT_TRACKED
            rows.append(r)
            continue
        n = xm["tracked_completed_cycles"]
        r = _row(key, label, label.lower(), f"{label.lower()} of {n} tracked completed cycle{'' if n == 1 else 's'}", "%",
                 m.get(hk) if h_ok else None, xm[hk] if n else None,
                 "Both measure the move from the entry to the daily highs / lows while held and the exit open (Stage 3.2 "
                 "convention). Historical: from the entry fill after slippage. Forward: from the reference open, observed "
                 + (f"going forward — {n} of {xm['completed_cycles']} completed cycles were tracked; untracked cycles are "
                    "not counted as 0." if xm["completed_cycles"] else "going forward — no forward cycle has completed yet."),
                 diff=n > 0)
        r["forward_count"] = {"tracked": n, "of": xm["completed_cycles"]}
        if not n:
            r["forward_text"] = "No completed MFE / MAE-tracked cycle yet"
        rows.append(r)
    rows.append({"key": "continuity", "label": "Continuity", "historical_measure": None, "forward_measure": "journal continuity",
                 "unit": "text", "historical": None, "forward": fwd.get("continuity") if f_ok else None,
                 "historical_display": None, "forward_display": fwd.get("continuity") if f_ok else None,
                 "historical_text": "N/A", "difference": None, "difference_unit": None, "difference_text": None,
                 "semantic_note": "A backtest walks every session; a forward journal covers only captured sessions."})
    rows.append(_row("open_at_end", "Still open", "open positions at the end of the run", "open reference cycles now", "count",
                     m.get("open_positions_at_end") if h_ok else None, fwd.get("open_cycles") if f_ok else None,
                     "Open positions / cycles are excluded from the averages above on both sides.", diff=False))
    not_compared = [
        {"key": "total_return_pct", "label": "Total return", "why": "Portfolio result of the backtest's cash, sizing and "
         "position limits. The forward journal has no equity, so it has no counterpart (an average reference move is a "
         "different measure)."},
        {"key": "annualized_return_pct", "label": "Annualized return", "why": "Backtest only (≥ 252 sessions). The forward "
         "journal is never annualized or extrapolated."},
        {"key": "max_drawdown_pct", "label": "Maximum drawdown", "why": "Needs an equity curve; the forward journal has none."},
        {"key": "profit_factor", "label": "Profit factor", "why": "Uses dollar P&L; the forward journal has no dollars."},
        {"key": "time_in_market_pct", "label": "Time in market", "why": "Portfolio exposure of the backtest; forward symbols "
         "are observed independently, without position limits."},
        {"key": "costs", "label": "Costs", "why": "The backtest applies its slippage and commission; the forward journal has none."},
        {"key": "entry_frequency", "label": "Entry frequency", "why": "Denominators differ: the backtest skips signals for "
         "position limits and cash; the forward journal has no limits. Shown on each side separately."},
    ]
    semantic = [
        "Historical and forward evidence cover different periods and are never merged or spliced.",
        "Historical trades are simulated with sizing, cash and costs; forward reference cycles are open-to-open reference "
        "moves with no sizing and no costs, and no broker execution.",
        "Differences are simple arithmetic (forward − historical, in percentage points for % measures); they are not "
        "adjusted for sample size and do not judge the strategy.",
    ]
    text = None
    avg = rows[1]
    if avg["difference"] is not None:
        text = (f"Forward average reference move differs from the selected historical run's average trade return by "
                f"{avg['difference_text']}.")
    elif h_ok and f_ok and not has_f:
        text = "No completed forward cycles yet, so no forward averages exist to compare."
    return {"available": h_ok and f_ok, "compatible_metrics": rows, "not_compared": not_compared,
            "semantic_notes": semantic, "difference_rule": DIFFERENCE_TEXT, "summary_text": text}


# ================================================================================================================
# 5. PER-SYMBOL + EVIDENCE NOTES (limitations — never a quality score)
# ================================================================================================================

def symbols(identity: dict, hist: dict, fwd: dict) -> List[dict]:
    hs = hist.get("symbol_breakdown") or {} if hist.get("status") == "AVAILABLE" else {}
    fsb = fwd.get("symbol_breakdown") or {} if fwd.get("status") == "AVAILABLE" else {}
    out = []
    for s in sorted(set(hs) | set(fsb) | set(identity["universe"])):         # alphabetical — never by performance
        out.append({"symbol": s, "historical": hs.get(s), "forward": fsb.get(s),
                    "in_universe": s in identity["universe"]})
    return out


def quality_notes(identity: dict, hist: dict, fwd: dict, extra: List[dict]) -> List[dict]:
    notes = list(extra)
    h_ok, f_ok = hist.get("status") == "AVAILABLE", fwd.get("status") == "AVAILABLE"
    if h_ok and f_ok:
        notes.append({"code": "DIFFERENT_OBSERVATION_PERIODS", "side": "both", "text": "The historical run and the forward "
                      "journal cover different periods, with different assumptions."})
    if h_ok:
        sc = (hist.get("sample") or {}).get("code")
        if sc in ("NO_TRADES", "VERY_SMALL_SAMPLE", "SMALL_SAMPLE"):
            notes.append({"code": f"HISTORICAL_{sc}", "side": "historical", "text": hist["sample"]["text"]})
        codes = {w["code"]: w for w in hist.get("warnings") or []}
        for c, label in (("UNIVERSE_HINDSIGHT_WARNING", "HISTORICAL_SURVIVORSHIP"),
                         ("SURVIVORSHIP_BIAS_WARNING", "HISTORICAL_BREADTH_SURVIVORSHIP"),
                         ("ADJUSTED_PRICES_NOTE", "ADJUSTED_PRICES"), ("ADJUSTED_PRICE_LEVEL_WARNING", "ADJUSTED_PRICE_LEVELS"),
                         ("DAILY_BAR_RESOLUTION", "DAILY_BAR_RESOLUTION"),
                         ("NO_HISTORICAL_EVENTS_OR_RESEARCH", "NO_HISTORICAL_EVENT_OR_RESEARCH_RECONSTRUCTION"),
                         ("ZERO_COST_ASSUMPTIONS", "HISTORICAL_ZERO_COSTS")):
            if c in codes:
                notes.append({"code": label, "side": "historical", "text": codes[c]["text"]})
        skipped = (hist.get("entry_frequency") or {}).get("skipped_max_open_positions") or 0
        if skipped:
            notes.append({"code": "HISTORICAL_POSITION_LIMITS", "side": "historical", "text": f"{skipped} historical entry "
                          "signal(s) were skipped because the maximum number of positions was open. The forward journal "
                          "observes every symbol independently, so its entries are not limited this way."})
    if f_ok:
        sc = fwd["sample"]["code"]
        if sc != "FORWARD_SAMPLE_SIZE":
            notes.append({"code": sc, "side": "forward", "text": fwd["sample"]["text"]})
        if fwd["continuity"] == "GAPPED":
            notes.append({"code": "FORWARD_CONTINUITY_GAP", "side": "forward", "text": f"GAPPED — {fwd['missed_sessions']} "
                          "eligible session(s) were not captured. The forward evidence is incomplete and was not "
                          "reconstructed."})
        elif fwd["continuity"] == J.BLOCKED:
            notes.append({"code": "FORWARD_CONTINUITY_BLOCKED", "side": "forward", "text": "CONTINUITY BLOCKED — "
                          f"{', '.join(fwd['states']['blocked_symbols'])}: a session was missed while a cycle was pending "
                          "or open, so those lifecycles are unknown. The forward evidence is incomplete."})
        t = fwd["timing_breakdown"]
        if t["post_close_forward_context"]:
            notes.append({"code": "POST_CLOSE_CONTEXT_USED", "side": "forward", "text": f"{t['post_close_forward_context']} "
                          "captured session(s) read forward-only context (research / events) after the close."})
        if t["captured_after_next_open"]:
            notes.append({"code": "CAPTURED_AFTER_NEXT_OPEN", "side": "forward", "text": f"{t['captured_after_next_open']} "
                          "session(s) were captured after the next session probably opened."})
        if any(w["code"] == "PRICE_BASIS_RESTATED" for w in fwd["warnings"]):
            notes.append({"code": "FORWARD_PRICE_BASIS_RESTATED", "side": "forward", "text": "Split / dividend adjustment "
                          "restated prices during an open cycle; its move uses one consistent price basis."})
        xm = fwd.get("excursion_metrics")
        if not xm:
            notes.append({"code": "FORWARD_DAILY_BAR_RESOLUTION", "side": "forward", "text": "Daily closes and next-session "
                          "opens only; forward MFE / MAE are not tracked."})
        else:
            notes.append({"code": "FORWARD_DAILY_BAR_RESOLUTION", "side": "forward", "text": "Daily closes and next-session "
                          "opens only; forward MFE / MAE use daily highs and lows (no intraday path order)."})
            if xm["legacy_untracked_completed_cycles"] or xm["incomplete_completed_cycles"]:
                notes.append({"code": "FORWARD_MFE_MAE_PARTIAL_SAMPLE", "side": "forward", "text": (
                    f"MFE / MAE averages use {xm['tracked_completed_cycles']} tracked of {xm['completed_cycles']} completed "
                    f"cycles: {xm['legacy_untracked_completed_cycles']} started before tracking and "
                    f"{xm['incomplete_completed_cycles']} could not be tracked completely. They are not counted as 0.")})
    return notes


# ================================================================================================================
# 6. ENTRY POINTS
# ================================================================================================================

def _pick_run(path: Path, runs: List[dict], vid: str, requested: Optional[str]) -> Tuple[Optional[str], str]:
    if requested:
        owner = _owner(path, "backtest_runs", "run_id", requested)
        if owner is None:
            raise CompareError("NOT_FOUND", "Backtest run not found.")
        if owner != vid:
            raise CompareError("EVIDENCE_VERSION_MISMATCH", "That backtest run belongs to a different strategy version. "
                               "Evidence is only compared within one exact version.")
        return requested, "USER_SELECTED"
    done = next((r for r in runs if r["status"] == "COMPLETED"), None)
    return (done["run_id"], "MOST_RECENT_COMPLETED") if done else (None, "NONE")


def _pick_journal(path: Path, journals: List[dict], vid: str, requested: Optional[str]) -> Tuple[Optional[str], str]:
    if requested:
        owner = _owner(path, "forward_test_journals", "journal_id", requested)
        if owner is None:
            raise CompareError("NOT_FOUND", "Forward journal not found.")
        if owner != vid:
            raise CompareError("EVIDENCE_VERSION_MISMATCH", "That forward journal belongs to a different strategy version. "
                               "Evidence is only compared within one exact version.")
        return requested, "USER_SELECTED"
    if not journals:
        return None, "NONE"
    return journals[0]["journal_id"], ("CURRENT_JOURNAL" if journals[0]["status"] != "ARCHIVED" else "MOST_RECENT_ARCHIVED")


def _isolated(fn, side_note: str, *args) -> dict:
    """One evidence source: a read failure is reported on its own side and never hides the other side."""
    try:
        return fn(*args)
    except (BacktestError, ForwardError) as exc:
        return {"status": "READ_ERROR", "message": f"This evidence could not be read ({exc.code}): {exc.message}",
                "note": side_note}
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        return {"status": "READ_ERROR", "message": f"This evidence could not be read ({type(exc).__name__}).", "note": side_note}


def view(strategy_version_id: str, backtest_run_id: Optional[str] = None, forward_journal_id: Optional[str] = None,
         *, path: Optional[Path] = None) -> dict:
    t0 = time.perf_counter()
    path = Path(path) if path else RO.db_path()
    bs, fs = RO.ReadOnlyBacktestStore(path), RO.ReadOnlyForwardStore(path)
    identity, spec, extra = _identity(bs, path, strategy_version_id)
    runs, journals = _runs_of(path, strategy_version_id), _journals_of(path, strategy_version_id)
    run_id, run_sel = _pick_run(path, runs, strategy_version_id, backtest_run_id)
    jid, j_sel = _pick_journal(path, journals, strategy_version_id, forward_journal_id)
    t_read = time.perf_counter()
    hist = _isolated(historical, HISTORICAL_NOTE, bs, identity, runs, run_id, run_sel)
    hist.setdefault("runs", runs)
    hist.setdefault("selection", run_sel)
    t_hist = time.perf_counter()
    fwd = _isolated(forward, FORWARD_NOTE, fs, identity, journals, jid, j_sel)
    fwd.setdefault("journals", journals)
    fwd.setdefault("selection", j_sel)
    fwd.setdefault("mfe_mae", {"tracked": False, "text": MFE_MAE_NOT_TRACKED})
    t_fwd = time.perf_counter()
    cmp_ = compare(hist, fwd)
    syms = symbols(identity, hist, fwd)
    notes = quality_notes(identity, hist, fwd, extra)
    header = {"historical_period": ((hist.get("run") or {}).get("period")) if hist.get("status") == "AVAILABLE" else None,
              "historical_run_id": run_id, "run_selection": run_sel,
              "forward_start": (fwd.get("journal") or {}).get("forward_start_date"),
              "forward_latest_session": fwd.get("latest_captured_session"),
              "forward_journal_id": jid, "journal_selection": j_sel,
              "forward_journal_status": fwd.get("journal_status")}
    t_end = time.perf_counter()
    return {"engine_version": ENGINE_VERSION, "identity": identity, "header": header,
            "selection": {"backtest_run_id": run_id, "run_selection": run_sel, "forward_journal_id": jid,
                          "journal_selection": j_sel, "runs": runs, "journals": journals},
            "historical": hist, "forward": fwd, "comparison": cmp_, "symbols": syms, "quality_notes": notes,
            "note": NOTE,
            "timings": _timings(t0, t_read, t_hist, t_fwd, t_end, hist, fwd)}


def _timings(t0, t_read, t_hist, t_fwd, t_end, hist: dict, fwd: dict) -> dict:
    """Where the time went: database reads, forward cycle derivation, metric / comparison derivation."""
    h = hist.get("timings") or {"read_s": round(t_hist - t_read, 4), "derive_s": 0.0}
    f = fwd.get("timings") or {"read_s": round(t_fwd - t_hist, 4), "derive_s": 0.0}
    return {"db_read_s": round((t_read - t0) + h["read_s"] + f["read_s"], 4),
            "cycle_derivation_s": f["derive_s"],
            "metric_derivation_s": round(h["derive_s"] + (t_end - t_fwd), 4),
            "identity_and_lists_s": round(t_read - t0, 4), "historical_s": round(t_hist - t_read, 4),
            "forward_s": round(t_fwd - t_hist, 4), "comparison_s": round(t_end - t_fwd, 4),
            "total_s": round(t_end - t0, 4)}


def public_config(path: Optional[Path] = None) -> dict:
    """The saved versions that can be opened (with how much stored evidence each has) and the labels. Local reads only."""
    path = Path(path) if path else RO.db_path()
    versions = RO.saved_versions(path, include_old=True)
    runs: Dict[str, Tuple[int, int]] = {}
    journals: Dict[str, Tuple[int, int]] = {}
    try:
        with RO.connect(path) as conn:
            runs = {r[0]: (r[1], r[2] or 0) for r in conn.execute(
                "SELECT strategy_version_id, COUNT(*), SUM(status = 'COMPLETED') FROM backtest_runs GROUP BY strategy_version_id")}
    except sqlite3.OperationalError:
        pass
    try:
        with RO.connect(path) as conn:
            journals = {r[0]: (r[1], r[2] or 0) for r in conn.execute(
                "SELECT strategy_version_id, COUNT(*), SUM(status != 'ARCHIVED') FROM forward_test_journals GROUP BY "
                "strategy_version_id")}
    except sqlite3.OperationalError:
        pass
    out = [{**v, "readiness_label": S.READINESS_LABEL.get(v["readiness"], v["readiness"]),
            "runs": runs.get(v["strategy_version_id"], (0, 0))[0], "completed_runs": runs.get(v["strategy_version_id"], (0, 0))[1],
            "journals": journals.get(v["strategy_version_id"], (0, 0))[0],
            "open_journals": journals.get(v["strategy_version_id"], (0, 0))[1]}
           for v in sorted(versions, key=lambda v: (v["strategy_name"].casefold(), v["strategy_id"], -v["version_number"]))]
    return {"engine_version": ENGINE_VERSION, "versions": out, "note": NOTE, "historical_note": HISTORICAL_NOTE,
            "forward_note": FORWARD_NOTE, "difference_rule": DIFFERENCE_TEXT, "cycle_formula": CY.FORMULA,
            "cycle_status_text": CY.STATUS_TEXT, "timing_labels": TIMING_LABEL,
            "sample_bands": {"historical": "Stage 3.2 labels (closed trades)",
                             "forward": [{"from": lo, "to": hi, "code": c} for lo, hi, c, _ in CY.SAMPLE_BANDS]},
            "forward_assumptions": [{"code": c, "text": t} for c, t in FORWARD_ASSUMPTIONS],
            "mfe_mae_forward": MFE_MAE_NOT_TRACKED}
