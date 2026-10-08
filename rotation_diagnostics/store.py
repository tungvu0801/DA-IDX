"""
rotation_diagnostics/store.py — append-only persistence for Stage 5.1 diagnostic runs (DESIGN_51 §8).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from database.attribution_diagnostic_migrations import TABLES, run_attribution_diagnostic_migrations
from fit import readonly as RO
from rotation.store import canonical_json, dec_str

_RUN_COLS = ("diag_id", "diag_hash", "campaign_id", "campaign_hash", "universe_hash", "sector_map_hash", "engine_version", "flags_version", "status", "failure_code",
             "failure_detail", "run_at", "completed_at", "start_date", "end_date", "n_windows", "n_configs", "n_evaluations", "n_cache_hits", "runtime_s", "data_hash",
             "result_hash", "market_data_requests", "definition_json", "universe_note")


def new_id() -> str:
    return uuid.uuid4().hex


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "quantize"):
        return dec_str(v)
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return v


class RotationDiagnosticStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    def _ro(self):
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def exists(self) -> bool:
        if not self.path.exists():
            return False
        conn = self._ro()
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
        return set(TABLES) <= names

    def read(self, sql: str, args=()) -> List[dict]:
        if not self.exists():
            return []
        conn = self._ro()
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    def run(self, diag_id: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM rotation_diagnostic_runs WHERE diag_id = ?", (diag_id,))
        return {**rows[0], "definition": json.loads(rows[0].pop("definition_json"))} if rows else None

    def runs(self, limit: int = 50) -> List[dict]:
        return [{k: v for k, v in r.items() if k != "definition_json"} for r in self.read("SELECT * FROM rotation_diagnostic_runs ORDER BY run_at DESC, rowid DESC LIMIT ?", (limit,))]

    def benchmarks(self, diag_id: str) -> List[dict]:
        return [{**r, "definition": json.loads(r.pop("definition_json")), "metrics": json.loads(r.pop("metrics_json")), "windows": json.loads(r.pop("windows_json"))} for r in
                self.read("SELECT * FROM rotation_diagnostic_benchmarks WHERE diag_id = ? ORDER BY benchmark, transaction_cost_bps", (diag_id,))]

    def symbols(self, diag_id: str, config_hash: Optional[str] = None) -> List[dict]:
        q = "SELECT * FROM rotation_diagnostic_symbol_attribution WHERE diag_id = ?" + (" AND config_hash = ?" if config_hash else "") + " ORDER BY config_hash, CAST(pnl AS REAL) DESC"
        return self.read(q, (diag_id, config_hash) if config_hash else (diag_id,))

    def sectors(self, diag_id: str, config_hash: Optional[str] = None) -> List[dict]:
        q = "SELECT * FROM rotation_diagnostic_sector_attribution WHERE diag_id = ?" + (" AND config_hash = ?" if config_hash else "") + " ORDER BY config_hash, CAST(pnl AS REAL) DESC"
        return self.read(q, (diag_id, config_hash) if config_hash else (diag_id,))

    def leave_one_out(self, diag_id: str) -> List[dict]:
        return [{**r, "deltas": json.loads(r.pop("deltas_json")), "dominant": bool(r["dominant"])} for r in
                self.read("SELECT * FROM rotation_diagnostic_leave_one_out WHERE diag_id = ? ORDER BY config_hash, symbol", (diag_id,))]

    def leave_sector_out(self, diag_id: str) -> List[dict]:
        return [{**r, "deltas": json.loads(r.pop("deltas_json")), "dependent": bool(r["dependent"])} for r in
                self.read("SELECT * FROM rotation_diagnostic_leave_sector_out WHERE diag_id = ? ORDER BY config_hash, sector", (diag_id,))]

    def windows(self, diag_id: str) -> List[dict]:
        return [{**json.loads(r["row_json"]), "config_hash": r["config_hash"], "window_index": r["window_index"]} for r in
                self.read("SELECT * FROM rotation_diagnostic_windows WHERE diag_id = ? ORDER BY config_hash, window_index", (diag_id,))]

    def scorecards(self, diag_id: str) -> List[dict]:
        return [{**r, "flags": json.loads(r.pop("flags_json")), "scorecard": json.loads(r.pop("scorecard_json")), "summary": json.loads(r.pop("summary_json"))} for r in
                self.read("SELECT * FROM rotation_diagnostic_scorecards WHERE diag_id = ? ORDER BY role, config_hash", (diag_id,))]

    @contextmanager
    def write(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            run_attribution_diagnostic_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert_run(self, run: dict, benchmarks: List[dict], per_config: List[dict]) -> str:
        did = new_id()
        row = {k: run.get(k) for k in _RUN_COLS if k != "diag_id"}
        if not isinstance(row.get("definition_json"), str):
            row["definition_json"] = canonical_json(_jsonable(row.get("definition_json") or {}))
        row["runtime_s"] = str(row.get("runtime_s") or "0")
        with self.write() as c:
            c.execute(f"INSERT INTO rotation_diagnostic_runs ({', '.join(_RUN_COLS)}) VALUES ({', '.join('?' * len(_RUN_COLS))})",
                      tuple(did if k == "diag_id" else row[k] for k in _RUN_COLS))
            for b in benchmarks:
                c.execute("INSERT INTO rotation_diagnostic_benchmarks (diag_id, benchmark, transaction_cost_bps, slippage_bps, definition_json, metrics_json, windows_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (did, b["benchmark"], b["transaction_cost_bps"], b["slippage_bps"], canonical_json(_jsonable(b["definition"])), canonical_json(_jsonable(b["metrics"])),
                           canonical_json(_jsonable(b["windows"]))))
            for pc in per_config:
                h = pc["config_hash"]
                for srow in pc["summary"]["attribution"]["symbols"]:
                    c.execute("INSERT INTO rotation_diagnostic_symbol_attribution (diag_id, config_hash, symbol, sector, pnl, share_of_positive, windows_held) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (did, h, srow["symbol"], srow["sector"], srow["pnl"], srow["share_of_positive"], srow["windows_held"]))
                for sec in pc["summary"]["attribution"]["sectors"]:
                    c.execute("INSERT INTO rotation_diagnostic_sector_attribution (diag_id, config_hash, sector, pnl, share_of_positive, avg_weight, max_weight) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (did, h, sec["sector"], sec["pnl"], sec["share_of_positive"], sec["avg_weight"], sec["max_weight"]))
                for r in pc["summary"]["leave_one_out"]:
                    c.execute("INSERT INTO rotation_diagnostic_leave_one_out (diag_id, config_hash, symbol, status, deltas_json, dominant) VALUES (?, ?, ?, ?, ?, ?)",
                              (did, h, r["symbol"], r["status"], canonical_json(_jsonable(r["deltas"])), 1 if r["dominant"] else 0))
                for r in pc["summary"]["leave_sector_out"]:
                    c.execute("INSERT INTO rotation_diagnostic_leave_sector_out (diag_id, config_hash, sector, status, deltas_json, dependent) VALUES (?, ?, ?, ?, ?, ?)",
                              (did, h, r["sector"], r["status"], canonical_json(_jsonable(r["deltas"])), 1 if r["dependent"] else 0))
                for w in pc["summary"]["windows"]:
                    c.execute("INSERT INTO rotation_diagnostic_windows (diag_id, config_hash, window_index, row_json) VALUES (?, ?, ?, ?)",
                              (did, h, w["window_index"], canonical_json(_jsonable({k: v for k, v in w.items() if k != "window_index"}))))
                c.execute("INSERT INTO rotation_diagnostic_scorecards (diag_id, config_hash, label, role, flags_json, scorecard_json, summary_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (did, h, pc["label"], pc["role"], canonical_json(pc["flags"]), canonical_json(_jsonable(pc["scorecard"])), canonical_json(_jsonable(pc["summary"]))))
        return did
