"""
brief/daily.py — Stage 4.2 READ-ONLY DAILY BRIEF: "what changed in my strategy system at one completed session?"

A deterministic summary of what earlier stages already STORED for one decision session:

  * WHAT CHANGED        Stage 4.1 saved-scan snapshots and RULES MET change alert events of that session (the exact
                        stored events and the Stage 4.1 alert sentences; a baseline is never shown as a change)
  * FORWARD JOURNALS    Stage 3.3 / 3.7 stored sessions of that date (CAPTURED or MISSED), each symbol's stored decision
                        and state before / after, reference fills resolved at that session's open, MISSED sessions
                        recorded by that capture, and reference cycles whose reference exit was stored at that session
                        (values from the Stage 3.5 / 3.6 Evidence layer — MFE / MAE exactly as stored, legacy wording kept)
  * EVIDENCE STATUS     the Stage 3.5 Evidence view of every strategy version represented in that session's activity
                        (stored sample labels, completed cycles, continuity) — as stored now, never recomputed here
  * DATA / CONTINUITY   the stored capture warnings, MISSED sessions, journal continuity, stored scan statuses that are
                        not decided (INCOMPLETE / STALE / unavailable) and evidence read errors — system issues, not risks

The session is the latest one represented in stored data unless one is requested; navigation moves between stored
sessions only. Everything is read over `mode=ro` connections (no migration, no write, no read-state change); there is
no market-data, event, research, broker or Claude call, nothing runs on a timer, and there is no score, ranking or forecast.
"""
from __future__ import annotations

import bisect
import copy
import json
import re
import sqlite3
import time
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from comparison import view as V
from database.automation_migrations import SETTING_KEYS
from fit import current as FC
from fit import readonly as RO
from fit import saved_scans as SS
from fit import scanner as SC

ENGINE_VERSION = "4.2.0"
NOTE = ("The Daily Brief summarises what the app already stored for one completed session. Stored data only: no new "
        "market data, no AI, no broker, no orders. It describes stored rule states, captures and evidence only — no "
        "ranking, advice or forecast.")
EMPTY_TEXT = "No new stored strategy activity for this session."
EVIDENCE_NOTE = ("Evidence status is the Stage 3.5 Evidence view of each strategy version in this brief, as stored now "
                 "(all stored sessions) — nothing is recalculated here. Open Evidence for the historical / forward comparison.")
LINK_NOTE = ("Open Strategy Fit shows the current evaluation at the latest completed close — a current view, not this "
             "stored session.")
SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
K_ENABLED, K_TIME, K_LAST = SETTING_KEYS
UNDECIDED = {FC.INCOMPLETE_DATA: "warn", FC.STALE_DATA: "alert", FC.DATA_UNAVAILABLE: "alert",
             FC.INTEGRITY_ERROR: "bad", FC.REGISTRY_MISMATCH: "bad", FC.UNSUPPORTED: "info"}
UNDECIDED_TEXT = {FC.INCOMPLETE_DATA: "the rule result could not be decided", FC.STALE_DATA: "no bar for the decision session",
                  FC.DATA_UNAVAILABLE: "market data was unavailable", FC.INTEGRITY_ERROR: "the strategy failed its integrity check",
                  FC.REGISTRY_MISMATCH: "the feature registry differs from the saved version", FC.UNSUPPORTED: "not supported"}
WARNING_LEVEL = {"MISSED_FORWARD_SESSION": "alert", "FORWARD_CONTINUITY_GAP": "alert", "EVENT_DATA_UNAVAILABLE": "warn",
                 "RESEARCH_UNAVAILABLE": "warn", "NO_BAR_FOR_SESSION": "warn", "CAPTURED_AFTER_NEXT_OPEN": "warn",
                 "POST_CLOSE_FORWARD_CONTEXT": "info", "EARNINGS_CALENDAR_UNAVAILABLE": "info", "PRICE_BASIS_RESTATED": "info"}
DECISIONS = ("ENTER", "EXIT", "HOLD", "SKIP")
PENDING_TEXT = {"ENTER": "Reference entry: pending the next session's open.",
                "EXIT": "Reference exit: pending the next session's open."}


_EVIDENCE_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()   # (db, version, journal, stored-data stamp) -> summary
_CACHE_MAX = 256


class BriefError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ================================================================================================================
# reads (read-only connections; a table that does not exist yet simply has no rows)
# ================================================================================================================

@contextmanager
def _open(path: Path):
    if not Path(path).exists():                        # never create a database file
        yield None
        return
    with RO.connect(path) as conn:
        yield conn


def _rows(conn, sql: str, args=()) -> List[dict]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.OperationalError:                   # the stage that owns this table never ran here
        return []


def _one(conn, sql: str, args=()):
    rows = _rows(conn, sql, args)
    return next(iter(rows[0].values())) if rows else None


def _json(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def stored_sessions(conn) -> List[str]:
    """Every decision session represented in stored brief sources (saved-scan snapshots / alerts, forward sessions)."""
    out = set()
    for sql in ("SELECT DISTINCT decision_session AS d FROM saved_scan_snapshots",
                "SELECT DISTINCT decision_session AS d FROM saved_scan_alert_events",
                "SELECT DISTINCT session_date AS d FROM forward_test_sessions"):
        out |= {r["d"] for r in _rows(conn, sql) if r["d"]}
    return sorted(out)


def _check_session(session: Optional[str]) -> Optional[str]:
    if session is None:
        return None
    try:
        if not SESSION_RE.match(session):
            raise ValueError
        date.fromisoformat(session)
    except ValueError:
        raise BriefError("INVALID_SESSION", "The session must be a date written YYYY-MM-DD.") from None
    return session


def _label(name: str, number) -> str:
    return f"{name} v{number if number is not None else '?'}"


def _sort_key(*parts) -> tuple:
    return tuple(p.casefold() if isinstance(p, str) else (p if p is not None else 0) for p in parts)


# ================================================================================================================
# the brief
# ================================================================================================================

def build(session: Optional[str] = None, *, path: Optional[Path] = None) -> dict:
    t0 = time.perf_counter()
    S = _check_session(session)
    path = Path(path) if path else RO.db_path()
    with _open(path) as conn:
        sessions = stored_sessions(conn)
        if S is None and sessions:
            S = sessions[-1]
        scans = {r["saved_scan_id"]: r for r in _rows(conn, "SELECT * FROM saved_strategy_scans")}
        snaps = _rows(conn, "SELECT * FROM saved_scan_snapshots WHERE decision_session = ?", (S,)) if S else []
        events = _rows(conn, "SELECT * FROM saved_scan_alert_events WHERE decision_session = ?", (S,)) if S else []
        journals = {r["journal_id"]: r for r in _rows(
            conn, "SELECT journal_id, strategy_id, strategy_version_id, version_number, status, archived_at, config_json, "
                  "forward_start_date FROM forward_test_journals")}
        fsess = _rows(conn, "SELECT journal_id, session_date, kind, recorded_at, detected_with_session, context_timing, "
                            "warnings_json FROM forward_test_sessions WHERE session_date = ? OR detected_with_session = ?",
                      (S, S)) if S else []
        obs = _rows(conn, "SELECT journal_id, symbol, state_before, decision, state_after, reason_code, evaluated_side, "
                          "rules_met, rules_total, exit_reasons_json, context_timing, lifecycle_json FROM "
                          "forward_test_observations WHERE session_date = ?", (S,)) if S else []
        fills = _rows(conn, "SELECT journal_id, symbol, cycle_no, fill_type, status, reason_code, signal_session_date, "
                            "fill_session_date, reference_open_price, reference_move_pct, delay_sessions FROM "
                            "forward_test_reference_fills WHERE resolved_in_session = ?", (S,)) if S else []
        settings = {r["key"]: r["value"] for r in _rows(conn, "SELECT key, value FROM app_settings")}
        unread_total = _one(conn, "SELECT COUNT(*) AS n FROM saved_scan_alert_events WHERE read_at IS NULL") or 0
        stamp_rows = {"runs": _rows(conn, "SELECT strategy_version_id AS k, run_id AS a, status AS b FROM backtest_runs"),
                      "sessions": _rows(conn, "SELECT journal_id AS k, COUNT(*) AS a, MAX(recorded_at) AS b FROM "
                                              "forward_test_sessions GROUP BY journal_id"),
                      "fills": _rows(conn, "SELECT journal_id AS k, COUNT(*) AS a FROM forward_test_reference_fills GROUP BY journal_id"),
                      "excursions": _rows(conn, "SELECT journal_id AS k, COUNT(*) AS a FROM forward_test_excursions GROUP BY journal_id"),
                      "tracking": _rows(conn, "SELECT journal_id AS k, activated_at AS a FROM forward_test_excursion_tracking")}
        fresh = {"latest_saved_scan_session": _one(conn, "SELECT MAX(decision_session) AS d FROM saved_scan_snapshots"),
                 "latest_alert_session": _one(conn, "SELECT MAX(decision_session) AS d FROM saved_scan_alert_events"),
                 "latest_forward_capture_session": _one(conn, "SELECT MAX(session_date) AS d FROM forward_test_sessions "
                                                              "WHERE kind = 'CAPTURED'")}
    versions = {v["strategy_version_id"]: v for v in RO.saved_versions(path, include_old=True)} if conn is not None else {}
    t_read = time.perf_counter()

    changes, checked, issues = _changes(scans, snaps, events, versions)
    forward, pending_cycles = _forward(journals, fsess, obs, fills, S, versions)
    represented = _represented(changes, checked, forward, versions, journals)
    t_rows = time.perf_counter()
    stamps = _stamps(stamp_rows, journals, versions) if conn is not None else {}
    evidence, cycles, ev_issues, hits = _evidence(represented, pending_cycles, path, S, sessions[-1] if sessions else None, stamps)
    t_ev = time.perf_counter()
    issues += ev_issues + _forward_issues(forward) + _automation_issues(settings, S)
    issues.sort(key=lambda i: _sort_key(i.get("strategy_name") or "", i.get("label") or "", i.get("symbol") or "", i["code"]))
    counts = _counts(changes, checked, forward, cycles, issues, represented)
    empty = not (snaps or events or forward)
    nav = _navigation(sessions, S)
    warnings = []
    if S and nav["latest"] and S != nav["latest"]:
        warnings.append({"code": "OLDER_SESSION", "text": "An older stored session. The session sections show what was "
                         "stored for it; Evidence status and current settings show what is stored now."})
    if S and empty:
        warnings.append({"code": "NO_STORED_ACTIVITY", "text": EMPTY_TEXT})
    t_end = time.perf_counter()
    return {"engine_version": ENGINE_VERSION, "brief_session": S, "requested_session": session,
            "generated_at": FC._utc(None).isoformat(timespec="seconds"),
            "stored_label": f"Stored after the {S} close" if S else None, "empty": empty,
            "empty_text": EMPTY_TEXT if empty else None,
            "navigation": nav, "summary_counts": counts, "headline": _headline(counts),
            "summary_text": _summary_text(S, counts, empty),
            "changes": changes, "saved_scans_checked": checked, "forward_activity": forward, "completed_cycles": cycles,
            "evidence_status": evidence, "data_issues": [i for i in issues if i["level"] != "info"],
            "notes": [i for i in issues if i["level"] == "info"],
            "system_status": _system(settings, scans, unread_total),
            "source_freshness": {**fresh, "brief_session": S}, "warnings": warnings,
            "note": NOTE, "evidence_note": EVIDENCE_NOTE, "link_note": LINK_NOTE,
            "timings": {"db_read_ms": round((t_read - t0) * 1000, 2), "rows_ms": round((t_rows - t_read) * 1000, 2),
                        "evidence_ms": round((t_ev - t_rows) * 1000, 2), "build_ms": round((t_end - t0) * 1000, 2),
                        "evidence_views": len(evidence), "evidence_cache_hits": hits}}


# ---- WHAT CHANGED: stored saved-scan snapshots + alert events --------------------------------------------------------------

def _scan_info(scan: Optional[dict], versions: Dict[str, dict]) -> dict:
    scan = scan or {}
    v = versions.get(scan.get("strategy_version_id")) or {}
    name = v.get("strategy_name") or scan.get("name") or "(unknown saved scan)"
    return {"saved_scan_id": scan.get("saved_scan_id"), "scan_name": scan.get("name"), "list_source": scan.get("list_source"),
            "source_label": SS.SOURCES.get(scan.get("list_source"), scan.get("list_source")),
            "strategy_id": scan.get("strategy_id"), "strategy_version_id": scan.get("strategy_version_id"),
            "strategy_name": name, "version_number": v.get("version_number"), "label": _label(name, v.get("version_number")),
            "is_current_version": v.get("is_current"), "archived": bool(scan.get("archived_at")),
            "alerts_enabled": bool(scan.get("alerts_enabled"))}


def _changes(scans, snaps, events, versions):
    by_snapshot = {e["snapshot_id"]: e for e in events}
    changes, checked, issues = [], [], []
    for e in events:
        info = _scan_info(scans.get(e["saved_scan_id"]), versions)
        ev = SS._event_out(e)                          # the Stage 4.1 decoder
        changes.append({**info, "alert_id": ev["alert_id"], "decision_session": ev["decision_session"],
                        "previous_session": ev["previous_session"], "newly_rules_met": ev["newly_rules_met"],
                        "no_longer_rules_met": ev["no_longer_rules_met"], "texts": SS.alert_texts(ev, info["label"]),
                        "unread": ev["read_at"] is None, "read_at": ev["read_at"], "alert_fingerprint": ev["alert_fingerprint"]})
    for r in snaps:
        info = _scan_info(scans.get(r["saved_scan_id"]), versions)
        s = SS._snap_out(r)                            # the Stage 4.1 decoder
        ev = by_snapshot.get(s["snapshot_id"])
        checked.append({**info, "snapshot_id": s["snapshot_id"], "evaluated_at": s["evaluated_at"], "trigger": s["trigger"],
                        "is_baseline": s["is_baseline"], "rules_met": s["rules_met"], "symbols": len(s["resolved_symbols"]),
                        "list_changes": s["list_changes"], "snapshot_fingerprint": s["snapshot_fingerprint"],
                        "result": "RULES_MET_CHANGED" if ev else "BASELINE" if s["is_baseline"] else "NO_CHANGE",
                        "alert_id": ev["alert_id"] if ev else None})
        undecided = defaultdict(list)
        for sym, st in sorted(s["status_map"].items()):
            if st in UNDECIDED:
                undecided[st].append(sym)
        for st, syms in sorted(undecided.items()):
            issues.append({"kind": "SAVED_SCAN", "code": f"SCAN_{st}", "level": UNDECIDED[st], "strategy_name": info["strategy_name"],
                           "label": info["label"], "symbol": syms[0], "symbols": syms, "saved_scan_id": info["saved_scan_id"],
                           "strategy_version_id": info["strategy_version_id"],
                           "text": f"{info['label']} · {info['source_label']}: {', '.join(syms)} "
                                   f"{SC.STATUS_LABEL.get(st, st)} in the stored scan ({UNDECIDED_TEXT[st]})."})
    changes.sort(key=lambda c: _sort_key(c["strategy_name"], c["version_number"], c["scan_name"] or ""))
    checked.sort(key=lambda c: _sort_key(c["strategy_name"], c["version_number"], c["scan_name"] or ""))
    return changes, checked, issues


# ---- FORWARD JOURNALS: stored sessions, observations and fills of that date -------------------------------------------------

def _journal_info(j: Optional[dict], versions: Dict[str, dict]) -> dict:
    j = j or {}
    cfg = _json(j.get("config_json"), {})
    name = ((cfg.get("strategy") or {}).get("name") or (versions.get(j.get("strategy_version_id")) or {}).get("strategy_name")
            or "(unknown strategy)")
    return {"journal_id": j.get("journal_id"), "strategy_id": j.get("strategy_id"), "strategy_version_id": j.get("strategy_version_id"),
            "strategy_name": name, "version_number": j.get("version_number"), "label": _label(name, j.get("version_number")),
            "journal_status": j.get("status"), "archived": j.get("status") == "ARCHIVED",
            "is_current_version": (versions.get(j.get("strategy_version_id")) or {}).get("is_current")}


def _forward(journals, fsess, obs, fills, S, versions):
    per = defaultdict(lambda: {"captured": None, "missed": None, "missed_recorded": []})
    for s in fsess:
        p = per[s["journal_id"]]
        if s["session_date"] == S and s["kind"] == "CAPTURED":
            p["captured"] = s
        elif s["session_date"] == S and s["kind"] == "MISSED":
            p["missed"] = s
        if s["kind"] == "MISSED" and s["detected_with_session"] == S:
            p["missed_recorded"].append(s["session_date"])
    obs_by = defaultdict(list)
    for o in obs:
        obs_by[o["journal_id"]].append(o)
    fills_by = defaultdict(list)
    for f in fills:
        fills_by[(f["journal_id"], f["symbol"])].append(f)
    out, pending = [], []
    for jid, p in per.items():
        if not (p["captured"] or p["missed"]):
            continue
        info = _journal_info(journals.get(jid), versions)
        cap = p["captured"]
        rows = []
        for o in sorted(obs_by.get(jid, []), key=lambda o: o["symbol"]):
            lc = _json(o["lifecycle_json"], {})
            resolved = [{"fill_type": f["fill_type"], "status": f["status"], "cycle_no": f["cycle_no"],
                         "reason_code": f["reason_code"], "signal_session_date": f["signal_session_date"],
                         "fill_session_date": f["fill_session_date"], "reference_open_price": f["reference_open_price"],
                         "reference_move_pct": f["reference_move_pct"], "delay_sessions": f["delay_sessions"]}
                        for f in sorted(fills_by.get((jid, o["symbol"]), []), key=lambda f: (f["cycle_no"], f["fill_type"]))]
            for f in resolved:
                if f["fill_type"] == "EXIT" and f["status"] == "FILLED":
                    pending.append((info, o["symbol"], f["cycle_no"], f))
            rows.append({"symbol": o["symbol"], "decision": o["decision"], "state_before": o["state_before"],
                         "state_after": o["state_after"], "reason_code": o["reason_code"], "evaluated_side": o["evaluated_side"],
                         "rules_met": o["rules_met"], "rules_total": o["rules_total"],
                         "exit_reasons": _json(o["exit_reasons_json"], []), "context_timing": o["context_timing"],
                         "cycle_no": lc.get("cycle_no"), "fills_resolved": resolved,
                         "reference_fill_pending": PENDING_TEXT.get(o["decision"])})
        out.append({**info, "session": S, "capture": "CAPTURED" if cap else "MISSED",
                    "recorded_at": cap["recorded_at"] if cap else p["missed"]["recorded_at"],
                    "detected_with_session": None if cap else p["missed"]["detected_with_session"],
                    "context_timing": cap["context_timing"] if cap else None,
                    "missed_recorded": sorted(p["missed_recorded"]),
                    "warnings": _json(cap["warnings_json"], []) if cap else [],
                    "decision_counts": {d: sum(1 for r in rows if r["decision"] == d) for d in DECISIONS},
                    "fills_resolved": sum(len(r["fills_resolved"]) for r in rows), "observations": rows})
    out.sort(key=lambda j: _sort_key(j["strategy_name"], j["version_number"], j["journal_id"] or ""))
    return out, pending


def _forward_issues(forward) -> List[dict]:
    issues = []
    for j in forward:
        base = {"kind": "FORWARD", "strategy_name": j["strategy_name"], "label": j["label"], "journal_id": j["journal_id"],
                "strategy_id": j["strategy_id"], "version_number": j["version_number"],
                "strategy_version_id": j["strategy_version_id"], "symbol": ""}
        if j["capture"] == "MISSED":
            issues.append({**base, "code": "SESSION_MISSED", "level": "alert",
                           "text": f"{j['label']}: this session was not captured by its forward journal — stored as MISSED "
                                   f"when the {j['detected_with_session']} session was captured (never reconstructed)."})
        for w in j["warnings"]:
            issues.append({**base, "code": w.get("code") or "CAPTURE_WARNING", "level": WARNING_LEVEL.get(w.get("code"), "warn"),
                           "text": f"{j['label']}: {w.get('text') or w.get('code')}"})
    return issues


# ---- EVIDENCE STATUS + completed cycles (the Stage 3.5 / 3.6 Evidence view — nothing recomputed) ----------------------------

def _represented(changes, checked, forward, versions, journals) -> Dict[str, dict]:
    """strategy_version_id -> {info, journal_id (the journal active in this session, if any)}; activity-bound only."""
    rep: Dict[str, dict] = {}
    for c in changes + checked:
        if c["strategy_version_id"]:
            rep.setdefault(c["strategy_version_id"], {"info": c, "journal_id": None})
    for j in forward:
        if j["strategy_version_id"]:
            rep[j["strategy_version_id"]] = {"info": j, "journal_id": j["journal_id"]}
    return rep


def _hist(h: dict) -> dict:
    sample = h.get("sample") if isinstance(h.get("sample"), dict) else {}
    return {"status": h.get("status"), "message": h.get("message"), "run_id": (h.get("run") or {}).get("run_id"),
            "period": (h.get("run") or {}).get("period"), "closed_trades": (h.get("metrics") or {}).get("closed_trades"),
            "sample_code": sample.get("code"), "sample_text": sample.get("text")}


def _fwd(f: dict) -> dict:
    sample = f.get("sample") or {}
    return {"status": f.get("status"), "message": f.get("message"), "journal_id": (f.get("journal") or {}).get("journal_id"),
            "journal_status": f.get("journal_status"), "completed_cycles": f.get("completed_cycles"),
            "open_cycles": f.get("open_cycles"), "blocked_cycles": f.get("blocked_cycles"),
            "sample_code": sample.get("code"), "sample_text": sample.get("text"),
            "mfe_mae_text": (f.get("mfe_mae") or {}).get("text"), "mfe_mae_tracked": (f.get("mfe_mae") or {}).get("tracked"),
            "continuity": f.get("continuity"), "captured_sessions": f.get("captured_sessions"),
            "missed_sessions": f.get("missed_sessions"), "latest_captured_session": f.get("latest_captured_session")}


def _stamps(rows: Dict[str, List[dict]], journals: Dict[str, dict], versions: Dict[str, dict]) -> Dict[str, tuple]:
    """Per strategy version: everything its Evidence view depends on that can change — its runs and their status, its
    journals with status, session count / last recorded_at, fill and excursion counts and tracking start, and whether it
    is still the current version. Evidence rows are append-only (database triggers), so an unchanged stamp means an
    unchanged Evidence view."""
    one = {k: {r["k"]: tuple(v for kk, v in r.items() if kk != "k") for r in rows[k]} for k in ("sessions", "fills", "excursions", "tracking")}
    runs: Dict[str, list] = defaultdict(list)
    for r in rows["runs"]:
        runs[r["k"]].append((r["a"], r["b"]))
    by_vid: Dict[str, list] = defaultdict(list)
    for jid, j in journals.items():
        by_vid[j["strategy_version_id"]].append((jid, j["status"], j["archived_at"], one["sessions"].get(jid), one["fills"].get(jid),
                                                 one["excursions"].get(jid), one["tracking"].get(jid)))
    return {vid: (tuple(sorted(runs.get(vid, []))), tuple(sorted(by_vid.get(vid, []), key=repr)),
                  (v.get("is_current"), v.get("readiness"), v.get("version_number"))) for vid, v in versions.items()}


def _view(vid: str, jid: Optional[str], path: Path, stamp) -> Tuple[dict, bool]:
    """The Evidence view's summary for one version (and the journal active in this session), cached by its stored-data
    stamp — repeated reads (Refresh brief, session navigation) never rebuild an unchanged view. Errors are never cached."""
    key = (str(Path(path).resolve()), vid, jid, stamp)
    hit = _EVIDENCE_CACHE.get(key) if stamp is not None else None
    if hit is not None:
        _EVIDENCE_CACHE.move_to_end(key)
        return copy.deepcopy(hit), True
    v = V.view(vid, None, jid, path=path)
    out = {"historical": _hist(v["historical"]), "forward": _fwd(v["forward"]), "readiness": v["identity"].get("readiness"),
           "journal_id": (v["forward"].get("journal") or {}).get("journal_id"), "cycles": v["forward"].get("cycles") or []}
    if stamp is not None:
        _EVIDENCE_CACHE[key] = copy.deepcopy(out)
        while len(_EVIDENCE_CACHE) > _CACHE_MAX:
            _EVIDENCE_CACHE.popitem(last=False)
    return out, False


def _evidence(represented: Dict[str, dict], pending_cycles, path: Path, S: Optional[str], latest: Optional[str],
              stamps: Optional[Dict[str, tuple]] = None):
    """ONE Evidence view per represented version (unavoidable N+1: the Evidence layer reads one exact version / journal at
    a time; the set is bounded by this session's activity, never every strategy in the database), cached by stored-data
    stamp. The Evidence view is what is stored NOW, so its journal-level states become data issues only where "now" is
    this session: a journal's continuity at its latest captured session, "no forward journal" in the latest brief."""
    evidence, issues, views, hits = [], [], {}, 0
    for vid, rep in represented.items():
        info = rep["info"]
        base = {k: info.get(k) for k in ("strategy_id", "strategy_version_id", "strategy_name", "version_number", "label",
                                         "is_current_version")}
        try:
            v, cached = _view(vid, rep["journal_id"], path, (stamps or {}).get(vid))
            hits += cached
        except V.CompareError as exc:
            evidence.append({**base, "status": "READ_ERROR", "message": exc.message, "historical": None, "forward": None})
            issues.append({"kind": "EVIDENCE", "code": "EVIDENCE_READ_ERROR", "level": "bad", "strategy_name": base["strategy_name"],
                           "label": base["label"], "symbol": "", "strategy_version_id": vid,
                           "text": f"{base['label']}: stored evidence could not be read ({exc.code})."})
            continue
        views[vid] = v
        h, f = v["historical"], v["forward"]
        evidence.append({**base, "status": "AVAILABLE", "historical": h, "forward": f, "readiness": v["readiness"]})
        for side, x in (("historical", h), ("forward", f)):
            if x["status"] in ("READ_ERROR", "INTEGRITY_ERROR") or str(x["status"]).startswith("INTEGRITY"):
                issues.append({"kind": "EVIDENCE", "code": f"{side.upper()}_{x['status']}", "level": "bad",
                               "strategy_name": base["strategy_name"], "label": base["label"], "symbol": "",
                               "strategy_version_id": vid, "text": f"{base['label']}: {side} evidence — {x['message'] or x['status']}"})
        if f["status"] == "NO_JOURNAL" and S == latest:
            issues.append({"kind": "EVIDENCE", "code": "NO_FORWARD_JOURNAL", "level": "info", "strategy_name": base["strategy_name"],
                           "label": base["label"], "symbol": "", "strategy_version_id": vid,
                           "text": f"No forward journal exists for {base['label']}."})
        elif f["continuity"] in ("GAPPED", "CONTINUITY_BLOCKED") and f["latest_captured_session"] == S:
            issues.append({"kind": "EVIDENCE", "code": f"JOURNAL_{f['continuity']}", "level": "alert",
                           "strategy_name": base["strategy_name"], "label": base["label"], "symbol": "",
                           "strategy_version_id": vid, "journal_id": f["journal_id"],
                           "text": f"{base['label']}: forward journal continuity is {f['continuity'].replace('_', ' ')} "
                                   f"({f['missed_sessions']} missed session{'' if f['missed_sessions'] == 1 else 's'} stored "
                                   "through this session)."})
    evidence.sort(key=lambda e: _sort_key(e["strategy_name"], e["version_number"]))
    cycles = []
    for info, sym, n, fill in pending_cycles:
        v = views.get(info["strategy_version_id"])
        cyc = None
        if v and v["journal_id"] == info["journal_id"]:
            cyc = next((c for c in v["cycles"] if c["symbol"] == sym and c["cycle_no"] == n), None)
        x = (cyc or {}).get("excursion")
        cycles.append({**{k: info[k] for k in ("journal_id", "strategy_id", "strategy_version_id", "strategy_name",
                                               "version_number", "label", "journal_status")},
                       "symbol": sym, "cycle_no": n, "available": cyc is not None,
                       "status": (cyc or {}).get("status"),
                       "entry_signal_session": (cyc or {}).get("entry_signal_session"),
                       "reference_entry_session": (cyc or {}).get("reference_entry_session"),
                       "reference_entry_open": (cyc or {}).get("reference_entry_open"),
                       "exit_signal_session": (cyc or {}).get("exit_signal_session") or fill["signal_session_date"],
                       "reference_exit_session": (cyc or {}).get("reference_exit_session") or fill["fill_session_date"],
                       "reference_exit_open": (cyc or {}).get("reference_exit_open") or fill["reference_open_price"],
                       "reference_move_pct": (cyc or {}).get("reference_move_pct") if cyc else fill["reference_move_pct"],
                       "holding_sessions": (cyc or {}).get("holding_sessions"), "exit_reasons": (cyc or {}).get("exit_reasons") or [],
                       "mfe_pct": (x or {}).get("mfe_pct"), "mae_pct": (x or {}).get("mae_pct"),
                       "excursion_status": (x or {}).get("status") if x else "NOT_TRACKED_STAGE_3_3",
                       "excursion_text": (x or {}).get("status_text") if x else V.MFE_MAE_NOT_TRACKED})
    cycles.sort(key=lambda c: _sort_key(c["strategy_name"], c["version_number"], c["symbol"], c["cycle_no"]))
    return evidence, cycles, issues, hits


# ---- stored automation status (Stage 3.7 app_settings, read directly — the automation module is never imported) ----------

def _last_check(settings: dict) -> Optional[dict]:
    return _json(settings.get(K_LAST), None)


def _automation_issues(settings: dict, S: Optional[str]) -> List[dict]:
    last = _last_check(settings)
    if not last or not S:
        return []
    out = []
    if last.get("latest_session") == S and last.get("result") in ("ERROR", "PARTIAL_FAILURE"):
        out.append({"kind": "AUTOMATION", "code": f"AUTO_CAPTURE_{last['result']}", "level": "bad" if last["result"] == "ERROR" else "alert",
                    "strategy_name": "", "label": "", "symbol": "",
                    "text": f"The last automatic check ({last.get('at')}) reported {last['result'].replace('_', ' ')} for "
                            "forward capture of this session."})
    x = (last.get("extensions") or {}).get("saved_scans") or {}
    if x.get("result") in ("ERROR", "PARTIAL_FAILURE") and any(s.get("decision_session") == S for s in x.get("scans") or []):
        n = (x.get("counts") or {}).get("errors", 0)
        out.append({"kind": "AUTOMATION", "code": f"SAVED_SCANS_{x['result']}", "level": "alert", "strategy_name": "",
                    "label": "", "symbol": "",
                    "text": f"The last automatic check ({last.get('at')}) returned {x['result'].replace('_', ' ')} for saved "
                            f"scans ({n} saved scan{'' if n == 1 else 's'} with an error)."})
    return out


def _system(settings: dict, scans: Dict[str, dict], unread_total: int) -> dict:
    last = _last_check(settings)
    active = [s for s in scans.values() if not s.get("archived_at")]
    x = ((last or {}).get("extensions") or {}).get("saved_scans")
    return {"automatic_capture": "ON" if settings.get(K_ENABLED) == "true" else "OFF",
            "saved_scans_active": len(active), "saved_scans_alerts_on": sum(1 for s in active if s.get("alerts_enabled")),
            "unread_rule_change_events_total": unread_total,
            "last_automatic_check": ({"at": last.get("at"), "trigger": last.get("trigger"), "result": last.get("result"),
                                      "saved_scans_result": (x or {}).get("result")} if last else None),
            "note": "Current settings and read state — not part of the stored session brief. Nothing is checked from here."}


# ---- counts, headline, deterministic sentences, navigation -----------------------------------------------------------------

def _counts(changes, checked, forward, cycles, issues, represented) -> dict:
    dec = {d: sum(j["decision_counts"][d] for j in forward) for d in DECISIONS}
    return {"saved_scans_checked": len(checked), "rule_change_events": len(changes),
            "newly_rules_met_symbols": sum(len(c["newly_rules_met"]) for c in changes),
            "no_longer_rules_met_symbols": sum(len(c["no_longer_rules_met"]) for c in changes),
            "unread_rule_change_events": sum(1 for c in changes if c["unread"]),
            "baselines_stored": sum(1 for c in checked if c["result"] == "BASELINE"),
            "forward_captures": sum(1 for j in forward if j["capture"] == "CAPTURED"),
            "forward_sessions_missed": sum(1 for j in forward if j["capture"] == "MISSED"),
            "missed_sessions_recorded": sum(len(j["missed_recorded"]) for j in forward),
            "decisions": dec, "reference_fills_resolved": sum(j["fills_resolved"] for j in forward),
            "completed_reference_cycles": len(cycles), "strategies_represented": len(represented),
            "data_issues": sum(1 for i in issues if i["level"] != "info"), "notes": sum(1 for i in issues if i["level"] == "info")}


def _n(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _headline(c: dict) -> List[str]:
    return [_n(c["rule_change_events"], "rule-state change"), _n(c["forward_captures"], "forward capture"),
            _n(c["completed_reference_cycles"], "completed reference cycle"), _n(c["data_issues"], "data issue")]


def _day(S: str) -> str:
    d = date.fromisoformat(S)
    return f"{d.strftime('%b')} {d.day}"


def _summary_text(S: Optional[str], c: dict, empty: bool) -> List[str]:
    if not S:
        return ["No stored strategy activity yet. Saved-scan checks and forward-journal captures appear here once they "
                "are stored."]
    if empty:
        return [EMPTY_TEXT]
    D, out = _day(S), []
    if c["rule_change_events"]:
        out.append(f"{_n(c['rule_change_events'], 'saved-scan RULES MET change')} {'was' if c['rule_change_events'] == 1 else 'were'} "
                   f"stored after the {D} close ({c['newly_rules_met_symbols']} newly met, "
                   f"{c['no_longer_rules_met_symbols']} no longer met).")
    elif c["saved_scans_checked"]:
        out.append(f"{_n(c['saved_scans_checked'], 'saved scan')} {'was' if c['saved_scans_checked'] == 1 else 'were'} checked "
                   f"after the {D} close; no RULES MET change was stored"
                   f"{' (a first check stores a baseline)' if c['baselines_stored'] else ''}.")
    if c["forward_captures"]:
        d = c["decisions"]
        out.append(f"{_n(c['forward_captures'], 'forward journal')} recorded this session (ENTER {d['ENTER']} · EXIT {d['EXIT']} "
                   f"· HOLD {d['HOLD']} · SKIP {d['SKIP']}).")
    if c["forward_sessions_missed"]:
        out.append(f"{_n(c['forward_sessions_missed'], 'forward journal')} did not capture this session (stored as MISSED).")
    if c["completed_reference_cycles"]:
        out.append(f"{_n(c['completed_reference_cycles'], 'reference cycle')} completed at this session's open.")
    out.append(f"{_n(c['data_issues'], 'data / continuity issue')} {'is' if c['data_issues'] == 1 else 'are'} listed below."
               if c["data_issues"] else "No data or continuity issue was stored for this session.")
    return out


def _navigation(sessions: List[str], S: Optional[str]) -> dict:
    if not S:
        return {"previous": None, "next": None, "latest": None, "count": 0, "recent": []}
    i = bisect.bisect_left(sessions, S)
    prev = sessions[i - 1] if i > 0 else None
    j = bisect.bisect_right(sessions, S)
    nxt = sessions[j] if j < len(sessions) else None
    return {"previous": prev, "next": nxt, "latest": sessions[-1] if sessions else None, "count": len(sessions),
            "recent": list(reversed(sessions[-40:])), "stored": S in sessions}
