"""
forward/store.py — SQLite access for the Stage 3.3 forward-test journal (same database file as the rest of the app).

Everything a capture produces — MISSED session rows, the CAPTURED session row, one observation per symbol, reference
fills, Stage 3.6 excursion (MFE / MAE) rows and a journal status change — is written in ONE transaction, so a failure can never leave a half-recorded
session. Evidence rows are append-only (database triggers in database/forward_migrations.py); the only journal fields
that ever change are `status` and `archived_at`.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from backtest.store import canonical
from database.excursion_migrations import run_excursion_migrations


class ForwardError(Exception):
    def __init__(self, code: str, message: str, detail=None, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.detail, self.status = code, message, detail, status


_JSON_OBS = ("exit_reasons_json", "unavailable_json", "feature_snapshot_json", "evaluation_trace_json", "lifecycle_json",
             "bar_json", "research_json", "event_json")
_JSON_SESSION = ("data_json", "summary_json", "warnings_json")


def _decode(row: sqlite3.Row, keys) -> dict:
    d = dict(row)
    for k in keys:
        if k in d:
            d[k[:-5]] = json.loads(d.pop(k)) if d[k] else None
    return d


class ForwardStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        with self._connect() as conn:
            run_excursion_migrations(conn)       # Stage 3.3 schema + the additive Stage 3.6 excursion tables

    @contextmanager
    def _connect(self, readonly: bool = False):
        if readonly:
            conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            conn.close()

    # ---- journals ---------------------------------------------------------------------------------------------------
    def create_journal(self, ref: dict, engine_version: str, config_json: str, config_hash: str, created_at: str,
                       created_session_date: str, forward_start_date: str) -> dict:
        jid = uuid.uuid4().hex
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO forward_test_journals (journal_id, strategy_id, strategy_version_id, version_number, "
                    "spec_hash, rules_hash, feature_registry_version, feature_registry_fingerprint, readiness, "
                    "engine_version, config_json, config_hash, created_at, created_session_date, forward_start_date, "
                    "status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE')",
                    (jid, ref["strategy_id"], ref["strategy_version_id"], ref["version_number"], ref["spec_hash"],
                     ref["rules_hash"], ref["feature_registry_version"], ref["feature_registry_fingerprint"],
                     ref["readiness"], engine_version, config_json, config_hash, created_at, created_session_date,
                     forward_start_date))
                conn.commit()
            except sqlite3.IntegrityError as exc:
                existing = conn.execute("SELECT journal_id FROM forward_test_journals WHERE strategy_version_id = ? "
                                        "AND status != 'ARCHIVED'", (ref["strategy_version_id"],)).fetchone()
                if existing is not None:
                    raise ForwardError("JOURNAL_EXISTS", "This strategy version already has a forward journal. Archive "
                                       "it first to start a new one.", {"journal_id": existing[0]}, 409) from exc
                raise
        return self.journal(jid)

    def journal(self, journal_id: str) -> dict:
        with self._connect(readonly=True) as conn:
            r = conn.execute("SELECT * FROM forward_test_journals WHERE journal_id = ?", (journal_id,)).fetchone()
        if r is None:
            raise ForwardError("NOT_FOUND", "Forward journal not found.", status=404)
        d = dict(r)
        d["config"] = json.loads(d.pop("config_json"))
        return d

    def list_journals(self, strategy_id: Optional[str] = None, version_number: Optional[int] = None) -> List[dict]:
        q, args, where = "SELECT journal_id FROM forward_test_journals", [], []
        if strategy_id:
            where.append("strategy_id = ?")
            args.append(strategy_id)
        if version_number is not None:
            where.append("version_number = ?")
            args.append(version_number)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY created_at DESC, rowid DESC"
        with self._connect(readonly=True) as conn:
            ids = [r[0] for r in conn.execute(q, args)]
        return [self.journal(j) for j in ids]

    def archive(self, journal_id: str, when: str) -> dict:
        with self._connect() as conn:
            cur = conn.execute("UPDATE forward_test_journals SET status = 'ARCHIVED', archived_at = ? "
                               "WHERE journal_id = ? AND status != 'ARCHIVED'", (when, journal_id))
            conn.commit()
        if cur.rowcount != 1:
            j = self.journal(journal_id)            # NOT_FOUND raises here
            if j["status"] == "ARCHIVED":
                raise ForwardError("JOURNAL_ARCHIVED", "This journal is already archived.", status=409)
        return self.journal(journal_id)

    # ---- evidence (reads) ---------------------------------------------------------------------------------------------
    def sessions(self, journal_id: str) -> List[dict]:
        with self._connect(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM forward_test_sessions WHERE journal_id = ? ORDER BY session_date",
                                (journal_id,)).fetchall()
        return [_decode(r, _JSON_SESSION) for r in rows]

    def session(self, journal_id: str, session_date: str) -> Optional[dict]:
        with self._connect(readonly=True) as conn:
            r = conn.execute("SELECT * FROM forward_test_sessions WHERE journal_id = ? AND session_date = ?",
                             (journal_id, session_date)).fetchone()
        return _decode(r, _JSON_SESSION) if r else None

    def observations(self, journal_id: str, session_date: Optional[str] = None, compact: bool = False) -> List[dict]:
        cols = ("journal_id, session_date, symbol, state_before, decision, state_after, reason_code, evaluated_side, "
                "rules_met, rules_total, exit_reasons_json, context_timing, captured_at, lifecycle_json") if compact else "*"
        q = f"SELECT {cols} FROM forward_test_observations WHERE journal_id = ?"
        args: list = [journal_id]
        if session_date is not None:
            q += " AND session_date = ?"
            args.append(session_date)
        with self._connect(readonly=True) as conn:
            rows = conn.execute(q + " ORDER BY session_date, symbol", args).fetchall()
        return [_decode(r, _JSON_OBS) for r in rows]

    def latest_observations(self, journal_id: str) -> Dict[str, dict]:
        """The most recent observation per symbol (carries the shadow state and lifecycle after that session)."""
        with self._connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT o.* FROM forward_test_observations o JOIN (SELECT symbol, MAX(session_date) AS d FROM "
                "forward_test_observations WHERE journal_id = ? GROUP BY symbol) m ON m.symbol = o.symbol AND "
                "m.d = o.session_date WHERE o.journal_id = ?", (journal_id, journal_id)).fetchall()
        return {r["symbol"]: _decode(r, _JSON_OBS) for r in rows}

    def fills(self, journal_id: str) -> List[dict]:
        with self._connect(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM forward_test_reference_fills WHERE journal_id = ? "
                                "ORDER BY resolved_in_session, symbol, cycle_no, fill_type", (journal_id,)).fetchall()
        return [_decode(r, ("price_basis_json",)) for r in rows]

    # ---- Stage 3.6 excursion evidence (reads; a database without the tables has no tracked cycle) ----------------------
    def excursion_tracking(self, journal_id: str) -> Optional[dict]:
        """When MFE / MAE tracking started for this journal (None: every cycle so far is legacy)."""
        try:
            with self._connect(readonly=True) as conn:
                r = conn.execute("SELECT * FROM forward_test_excursion_tracking WHERE journal_id = ?", (journal_id,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(r) if r else None

    def excursions(self, journal_id: str, session_date: Optional[str] = None) -> List[dict]:
        q, args = "SELECT * FROM forward_test_excursions WHERE journal_id = ?", [journal_id]
        if session_date is not None:
            q += " AND session_date = ?"
            args.append(session_date)
        try:
            with self._connect(readonly=True) as conn:
                rows = conn.execute(q + " ORDER BY session_date, symbol, cycle_no, observation_type", args).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def excursion_context(self, journal_id: str):
        """(tables available, tracking start or None, stored rows) in ONE read-only connection — what a capture needs."""
        try:
            with self._connect(readonly=True) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'forward_test_excursions'").fetchone() is None:
                    return False, None, []
                tr = conn.execute("SELECT * FROM forward_test_excursion_tracking WHERE journal_id = ?", (journal_id,)).fetchone()
                rows = conn.execute("SELECT * FROM forward_test_excursions WHERE journal_id = ? ORDER BY session_date, symbol, "
                                    "cycle_no, observation_type", (journal_id,)).fetchall() if tr else []
        except sqlite3.Error:
            return False, None, []
        return True, (dict(tr) if tr else None), [dict(r) for r in rows]

    def excursion_tracking_available(self) -> bool:
        try:
            with self._connect(readonly=True) as conn:
                return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                                    "'forward_test_excursions'").fetchone() is not None
        except sqlite3.Error:
            return False

    def raw_rows(self, journal_id: str) -> Dict[str, list]:
        """Every stored evidence row, verbatim (used to prove immutability)."""
        with self._connect(readonly=True) as conn:
            return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} WHERE journal_id = ? ORDER BY 1, 2, 3", (journal_id,))]
                    for t in ("forward_test_sessions", "forward_test_observations", "forward_test_reference_fills")}

    # ---- evidence (the ONE write path) ---------------------------------------------------------------------------------
    def write_capture(self, journal_id: str, missed: List[dict], session: dict, observations: List[dict],
                      fills: List[dict], new_status: Optional[str], excursions: Optional[List[dict]] = None,
                      tracking: Optional[dict] = None) -> float:
        """Insert one captured session with everything it produced, atomically. Returns seconds spent writing.
        Stage 3.6: `tracking` (first capture with MFE / MAE tracking) and `excursions` are written in the same transaction."""
        import time
        t0 = time.perf_counter()
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                for m in missed:
                    conn.execute("INSERT INTO forward_test_sessions (journal_id, session_date, kind, recorded_at, "
                                 "detected_with_session) VALUES (?,?,'MISSED',?,?)",
                                 (journal_id, m["session_date"], m["recorded_at"], m["detected_with_session"]))
                conn.execute(
                    "INSERT INTO forward_test_sessions (journal_id, session_date, kind, recorded_at, "
                    "market_close_snapshot_time, forward_context_captured_at, context_timing, engine_version, spec_hash, "
                    "data_json, data_hash, summary_json, warnings_json) VALUES (?,?,'CAPTURED',?,?,?,?,?,?,?,?,?,?)",
                    (journal_id, session["session_date"], session["recorded_at"], session["market_close_snapshot_time"],
                     session["forward_context_captured_at"], session["context_timing"], session["engine_version"],
                     session["spec_hash"], canonical(session["data"]), session["data_hash"], canonical(session["summary"]),
                     canonical(session["warnings"])))
                for o in observations:
                    conn.execute(
                        "INSERT INTO forward_test_observations (journal_id, session_date, symbol, state_before, decision, "
                        "state_after, reason_code, evaluated_side, rules_met, rules_total, exit_reasons_json, "
                        "unavailable_json, feature_snapshot_json, evaluation_trace_json, lifecycle_json, bar_json, "
                        "context_timing, research_json, event_json, captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (journal_id, session["session_date"], o["symbol"], o["state_before"], o["decision"],
                         o["state_after"], o["reason_code"], o["evaluated_side"], o["rules_met"], o["rules_total"],
                         canonical(o["exit_reasons"]), canonical(o["unavailable"]), canonical(o["feature_snapshot"]),
                         None if o["evaluation_trace"] is None else canonical(o["evaluation_trace"]),
                         canonical(o["lifecycle"]), None if o["bar"] is None else canonical(o["bar"]),
                         o["context_timing"], None if o["research"] is None else canonical(o["research"]),
                         None if o["event"] is None else canonical(o["event"]), session["recorded_at"]))
                for f in fills:
                    conn.execute(
                        "INSERT INTO forward_test_reference_fills (journal_id, symbol, cycle_no, fill_type, status, "
                        "reason_code, signal_session_date, fill_session_date, reference_open_price, delay_sessions, "
                        "reference_entry_price, reference_move_pct, price_basis_json, dataset_id, content_hash, "
                        "resolved_in_session, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (journal_id, f["symbol"], f["cycle_no"], f["fill_type"], f["status"], f["reason_code"],
                         f["signal_session_date"], f["fill_session_date"], f["reference_open_price"],
                         f["delay_sessions"], f["reference_entry_price"], f["reference_move_pct"],
                         None if f["price_basis"] is None else canonical(f["price_basis"]), f["dataset_id"],
                         f["content_hash"], session["session_date"], session["recorded_at"]))
                if tracking:
                    conn.execute("INSERT INTO forward_test_excursion_tracking (journal_id, activated_in_session, activated_at, "
                                 "engine_version) VALUES (?,?,?,?)", (journal_id, tracking["activated_in_session"],
                                                                      tracking["activated_at"], tracking["engine_version"]))
                for x in excursions or []:
                    conn.execute(
                        "INSERT INTO forward_test_excursions (journal_id, symbol, cycle_no, session_date, observation_type, "
                        "status, reason_code, tracking_status, entry_session_date, entry_reference_price, basis_entry_open, "
                        "price_basis, observed_open, observed_high, observed_low, session_high_pct, session_low_pct, "
                        "cumulative_mfe_pct, cumulative_mae_pct, dataset_id, content_hash, engine_version, captured_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (journal_id, x["symbol"], x["cycle_no"], x["session_date"], x["observation_type"], x["status"],
                         x["reason_code"], x["tracking_status"], x["entry_session_date"], x["entry_reference_price"],
                         x["basis_entry_open"], x["price_basis"], x["observed_open"], x["observed_high"], x["observed_low"],
                         x["session_high_pct"], x["session_low_pct"], x["cumulative_mfe_pct"], x["cumulative_mae_pct"],
                         x["dataset_id"], x["content_hash"], x["engine_version"], x["captured_at"]))
                if new_status:
                    conn.execute("UPDATE forward_test_journals SET status = ? WHERE journal_id = ?", (new_status, journal_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return time.perf_counter() - t0


_stores: Dict[str, ForwardStore] = {}
_lock = threading.Lock()


def get_forward_store() -> ForwardStore:
    """Same database file as the rest of the app (database.database.get_db().db_path); tables created on first use."""
    from database.database import get_db
    path = str(Path(get_db().db_path))
    with _lock:
        if path not in _stores:
            _stores[path] = ForwardStore(Path(path))
        return _stores[path]
