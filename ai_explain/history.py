"""
ai_explain/history.py — Stage 3.9 OPTIONAL, APPEND-ONLY AI explanation history.

OFF by default. While OFF nothing is written (the setting is read through a read-only connection; an untouched database
means OFF). While ON, an explanation is saved only after Stage 3.8 accepted it — status OK (Claude, all guards passed)
or LOCAL (the no-AI explanation) — never a provider error, a cut-off or withheld answer. The saved object is exactly the
structured explanation the user was shown plus a compact grounding summary of the deterministic view it explained.

The same explanation shown again (a cache hit, or the same local text) is one row: the database skips an identical
insert. A changed deterministic input has a new fingerprint and therefore a new row; older rows are never rewritten.
Opening, filtering or reading history makes no Claude call and never touches the AI cache. Nothing in any financial
calculation reads these tables.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from database.explanation_history_migrations import run_explanation_history_migrations

KEY = "ai_explanation_history_enabled"
FIT, EVIDENCE = "STRATEGY_FIT_EXPLANATION", "EVIDENCE_EXPLANATION"
KINDS = {"fit": FIT, "evidence": EVIDENCE}
PAGE_MAX = 100


def db_path() -> Path:
    from fit.readonly import db_path as app_db
    return app_db()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")      # orders rows saved within one second


@contextmanager
def _connect(readonly: bool, path: Optional[Path] = None):
    p = Path(path or db_path())
    if readonly:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=10.0)
    else:
        conn = sqlite3.connect(str(p), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000;")
    try:
        yield conn
    finally:
        conn.close()


# ---- the opt-in setting -------------------------------------------------------------------------------------------------

def enabled(path: Optional[Path] = None) -> bool:
    try:
        with _connect(True, path) as conn:
            r = conn.execute("SELECT value FROM ai_explanation_settings WHERE key = ?", (KEY,)).fetchone()
    except sqlite3.OperationalError:                  # never enabled (no table yet): OFF
        return False
    return bool(r) and r["value"] == "true"


def set_enabled(on: bool, path: Optional[Path] = None) -> bool:
    with _connect(False, path) as conn:
        run_explanation_history_migrations(conn)
        conn.execute("INSERT INTO ai_explanation_settings (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                     "SET value = excluded.value, updated_at = excluded.updated_at", (KEY, "true" if on else "false", _now()))
        conn.commit()
    return enabled(path)


# ---- the compact grounding snapshot (from the Stage 3.8 payload the explanation was built from) ----------------------------

def _leaves(conditions):
    return [{"condition": c.get("saved_condition"), "result": c.get("result"), "observed": c.get("observed_value")}
            for c in conditions or [] if "saved_condition" in c][:12]


def grounding(kind: str, ident: dict, payload: dict, result: dict) -> dict:
    s = payload.get("strategy") or {}
    base = {"label": f"{s.get('name')} v{s.get('version')}", "strategy_name": s.get("name"), "version": s.get("version"),
            "grounded_in": (result.get("grounded_in") or {}).get("label")}
    if kind == FIT:
        f, g = payload.get("fit") or {}, payload.get("grounded_in") or {}
        return {**base, "symbol": ident.get("symbol"), "decision_session": ident.get("decision_session"),
                "context_timing": g.get("context_timing"), "fit_status": f.get("status"), "status_text": f.get("status_text"),
                "entry_rule_logic": f.get("entry_rule_logic"),
                "conditions": ({"met": f.get("conditions_met"), "not_met": f.get("conditions_not_met"),
                                "unavailable": f.get("conditions_unavailable")}
                               if f.get("status") in ("RULES MET", "RULES NOT MET", "INCOMPLETE DATA") else None),
                "condition_trace": _leaves(payload.get("conditions")),
                "unavailable_inputs": (payload.get("unavailable_inputs") or [])[:6]}
    h, fw = payload.get("historical") or {}, payload.get("forward") or {}
    mm = fw.get("mfe_mae")
    return {**base, "backtest_run_id": ident.get("backtest_run_id"), "forward_journal_id": ident.get("forward_journal_id"),
            "historical": {k: h.get(k) for k in ("status", "run", "selection", "period", "closed_trades", "sample") if h.get(k) is not None},
            "forward": {k: fw.get(k) for k in ("status", "journal_status", "continuity", "captured_sessions", "missed_sessions",
                                               "completed_reference_cycles", "sample") if fw.get(k) is not None},
            "mfe_mae_tracking": mm.get("tracked_sample") if isinstance(mm, dict) else mm,
            "compatible_differences": [{"measure": r.get("measure"), "difference": r.get("difference_forward_minus_historical")}
                                       for r in (payload.get("comparison") or {}).get("compatible_measures") or []
                                       if r.get("difference_forward_minus_historical")][:10]}


# ---- save (append-only) ----------------------------------------------------------------------------------------------------

def _canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def save(kind: str, ident: dict, payload: dict, result: dict, path: Optional[Path] = None) -> dict:
    """Append one accepted explanation (status OK or LOCAL). Returns {"saved": new row?, "history_id": the row shown}."""
    status = result.get("status")
    if status not in ("OK", "LOCAL") or not isinstance(result.get("explanation"), dict):
        return {"saved": False, "history_id": None}
    local = status == "LOCAL"
    explanation = result["explanation"]
    row = {
        "history_id": uuid.uuid4().hex, "explanation_type": kind, "strategy_version_id": ident["strategy_version_id"],
        "symbol": ident.get("symbol") if kind == FIT else None,
        "decision_session": ident.get("decision_session") if kind == FIT else None,
        "backtest_run_id": ident.get("backtest_run_id") if kind == EVIDENCE else None,
        "forward_journal_id": ident.get("forward_journal_id") if kind == EVIDENCE else None,
        "input_fingerprint": result["input_fingerprint"],
        "prompt_version": "NONE" if local else result["prompt_version"],
        "provider": "LOCAL" if local else result["provider"], "model": "NONE" if local else result["model"],
        "cache_hit": 0 if local else int(bool(result.get("cache_hit"))),
        "claude_calls": 0 if local else int(result.get("claude_calls") or 0),
        "generated_at": result.get("generated_at") or _now(), "saved_at": _now(),
        "structured_explanation_json": json.dumps(explanation, ensure_ascii=False),
        "grounding_summary_json": json.dumps(grounding(kind, ident, payload, result), ensure_ascii=False),
        "explanation_sha256": hashlib.sha256(_canon(explanation).encode("utf-8")).hexdigest(), "status": status,
    }
    cols = list(row)
    with _connect(False, path) as conn:
        run_explanation_history_migrations(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:                                               # the exact immutable reference: version id -> its strategy
            r = conn.execute("SELECT strategy_id FROM strategy_versions WHERE version_id = ?", (row["strategy_version_id"],)).fetchone()
        except sqlite3.OperationalError:                   # no strategy tables in this database
            r = None
        conn.execute(f"INSERT INTO ai_explanation_history (strategy_id, {', '.join(cols)}) VALUES ({', '.join('?' * (len(cols) + 1))})",
                     [r["strategy_id"] if r else None, *row.values()])
        hit = conn.execute("SELECT history_id FROM ai_explanation_history WHERE explanation_type = ? AND input_fingerprint = ? "
                           "AND prompt_version = ? AND provider = ? AND model = ? AND explanation_sha256 = ?",
                           (kind, row["input_fingerprint"], row["prompt_version"], row["provider"], row["model"],
                            row["explanation_sha256"])).fetchone()
        conn.commit()
    return {"saved": bool(hit) and hit["history_id"] == row["history_id"], "history_id": hit["history_id"] if hit else None}


def after_explain(kind: str, ident: dict, payload: dict, result: dict) -> dict:
    """The additive "history" block of an explanation response. Writes only when history is ON."""
    on = enabled()
    if not on:
        return {"enabled": False, "saved": False, "history_id": None}
    return {"enabled": True, **save(kind, ident, payload, result)}


# ---- read (0 Claude calls; never the AI cache) -------------------------------------------------------------------------------

_LIST_COLS = ("history_id, explanation_type, strategy_id, strategy_version_id, symbol, decision_session, backtest_run_id, "
              "forward_journal_id, input_fingerprint, prompt_version, provider, model, cache_hit, claude_calls, generated_at, "
              "saved_at, status, grounding_summary_json")


def _origin(r) -> str:
    return "local" if r["provider"] == "LOCAL" else "cached" if r["cache_hit"] else "generated"


def _brief(r) -> dict:
    g = json.loads(r["grounding_summary_json"])
    return {"history_id": r["history_id"], "explanation_type": r["explanation_type"], "strategy_version_id": r["strategy_version_id"],
            "strategy_id": r["strategy_id"], "label": g.get("label"), "symbol": r["symbol"], "decision_session": r["decision_session"],
            "backtest_run_id": r["backtest_run_id"], "forward_journal_id": r["forward_journal_id"],
            "fit_status": g.get("fit_status"), "continuity": (g.get("forward") or {}).get("continuity"),
            "provider": r["provider"], "model": r["model"], "origin": _origin(r), "claude_calls": r["claude_calls"],
            "prompt_version": r["prompt_version"], "input_fingerprint": r["input_fingerprint"],
            "generated_at": r["generated_at"], "saved_at": r["saved_at"]}


def list_recent(kind: str = "all", origin: str = "all", strategy_version_id: Optional[str] = None,
                symbol: Optional[str] = None, limit: int = 50, before: Optional[str] = None,
                path: Optional[Path] = None) -> dict:
    """Newest first (saved time). Filters: kind all|fit|evidence, origin all|local|claude. `before` is the cursor
    returned as `next` ("saved_at|history_id")."""
    limit = max(1, min(int(limit), PAGE_MAX))
    where, args = [], []
    if kind in KINDS:
        where.append("explanation_type = ?")
        args.append(KINDS[kind])
    if origin == "local":
        where.append("provider = 'LOCAL'")
    elif origin == "claude":
        where.append("provider <> 'LOCAL'")
    if strategy_version_id:
        where.append("strategy_version_id = ?")
        args.append(strategy_version_id)
    if symbol:
        where.append("symbol = ?")
        args.append(symbol.upper())
    if before and "|" in before:
        at, hid = before.split("|", 1)
        where.append("(saved_at < ? OR (saved_at = ? AND history_id < ?))")
        args += [at, at, hid]
    sql = (f"SELECT {_LIST_COLS} FROM ai_explanation_history {'WHERE ' + ' AND '.join(where) if where else ''} "
           "ORDER BY saved_at DESC, history_id DESC LIMIT ?")
    try:
        with _connect(True, path) as conn:
            rows = conn.execute(sql, [*args, limit + 1]).fetchall()
    except sqlite3.OperationalError:                  # history never enabled: nothing saved
        rows = []
    items = [_brief(r) for r in rows[:limit]]
    nxt = f"{rows[limit - 1]['saved_at']}|{rows[limit - 1]['history_id']}" if len(rows) > limit else None
    return {"items": items, "next": nxt, "limit": limit}


def get(history_id: str, path: Optional[Path] = None) -> Optional[dict]:
    """The stored explanation exactly as saved (never regenerated)."""
    try:
        with _connect(True, path) as conn:
            r = conn.execute(f"SELECT {_LIST_COLS}, structured_explanation_json, explanation_sha256 FROM ai_explanation_history "
                             "WHERE history_id = ?", (history_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if r is None:
        return None
    return {**_brief(r), "explanation": json.loads(r["structured_explanation_json"]),
            "grounding_summary": json.loads(r["grounding_summary_json"]), "explanation_sha256": r["explanation_sha256"]}
