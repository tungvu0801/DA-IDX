"""
rotation_walkforward/store.py — append-only persistence for Stage 4.9 definitions, runs and results (DESIGN_49 §10).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from database.portfolio_walkforward_migrations import TABLES, run_portfolio_walkforward_migrations
from fit import readonly as RO
from rotation.store import canonical_json, dec_str

from rotation_walkforward import config as C

_CONFIG_COLS = ("wf_config_id", "wf_config_hash", "base_config_id", "base_config_hash", "start_date", "end_date", "train_months", "test_months",
                "step_months", "rebalance_frequency", "selection_metric", "initial_cash", "transaction_cost_bps", "slippage_bps", "benchmark",
                "universe_source", "universe_ref", "universe_json", "universe_hash", "candidates_json", "definition_json", "created_at")
_RUN_COLS = ("run_id", "wf_config_id", "wf_config_hash", "base_config_hash", "universe_hash", "engine_version", "backtest_version", "robustness_version",
             "status", "failure_code", "failure_detail", "run_at", "completed_at", "n_windows", "n_candidates", "n_rejected", "n_discarded",
             "n_evaluations", "n_cache_hits", "overlapping_tests", "runtime_s", "data_hash", "bars_json", "result_hash", "market_data_requests",
             "universe_note")
_WIN_COLS = ("window_index", "window_hash", "train_start", "train_end", "test_start", "test_end", "train_first_session", "train_last_session",
             "test_first_session", "test_last_session", "n_train_sessions", "n_test_sessions")
_CAND_COLS = ("window_index", "config_hash", "label", "train_rank", "train_status", "train_metric_value", "train_metrics_json")
_SEL_COLS = ("window_index", "config_hash", "label", "selection_metric", "train_metric_value", "tie_break_json", "config_json", "frozen_at")
_OOS_COLS = ("window_index", "config_hash", "test_status", "test_metrics_json", "equity_json", "result_hash")


def new_id() -> str:
    return uuid.uuid4().hex


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "quantize"):
        return dec_str(v)
    return v


class PortfolioWalkForwardStore:
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

    def config_by_hash(self, h: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM portfolio_walkforward_configs WHERE wf_config_hash = ?", (h,))
        if not rows:
            return None
        r = rows[0]
        return {**r, "universe": json.loads(r["universe_json"]), "candidates": json.loads(r["candidates_json"]), "definition": json.loads(r["definition_json"]),
                "universe_note": C.UNIVERSE_NOTE}

    def run(self, run_id: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM portfolio_walkforward_runs WHERE run_id = ?", (run_id,))
        return rows[0] if rows else None

    def runs(self, limit: int = 50) -> List[dict]:
        return self.read("SELECT * FROM portfolio_walkforward_runs ORDER BY run_at DESC, rowid DESC LIMIT ?", (limit,))

    def windows(self, run_id: str) -> List[dict]:
        return self.read("SELECT * FROM portfolio_walkforward_windows WHERE run_id = ? ORDER BY window_index", (run_id,))

    def candidates(self, run_id: str) -> List[dict]:
        return [{**r, "train_metrics": json.loads(r.pop("train_metrics_json"))} for r in
                self.read("SELECT * FROM portfolio_walkforward_candidates WHERE run_id = ? ORDER BY window_index, train_rank", (run_id,))]

    def selections(self, run_id: str) -> List[dict]:
        return [{**r, "tie_break": json.loads(r.pop("tie_break_json")), "config": json.loads(r.pop("config_json"))} for r in
                self.read("SELECT * FROM portfolio_walkforward_selections WHERE run_id = ? ORDER BY window_index", (run_id,))]

    def oos(self, run_id: str, with_equity: bool = False) -> List[dict]:
        rows = self.read("SELECT * FROM portfolio_walkforward_oos_results WHERE run_id = ? ORDER BY window_index", (run_id,))
        out = []
        for r in rows:
            eq = r.pop("equity_json")
            out.append({**r, "test_metrics": json.loads(r.pop("test_metrics_json")), **({"equity": json.loads(eq)} if with_equity else {})})
        return out

    def sensitivity(self, run_id: str, kind: Optional[str] = None) -> List[dict]:
        rows = self.read("SELECT * FROM portfolio_walkforward_sensitivity WHERE run_id = ? " + ("AND kind = ? " if kind else "") + "ORDER BY kind, key",
                         (run_id, kind) if kind else (run_id,))
        return [{**r, "payload": json.loads(r.pop("payload_json"))} for r in rows]

    def metrics(self, run_id: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM portfolio_walkforward_metrics WHERE run_id = ?", (run_id,))
        if not rows:
            return None
        r = rows[0]
        return {"metrics": json.loads(r["metrics_json"]), "robustness": json.loads(r["robustness_json"]), "conventions": json.loads(r["conventions_json"])}

    @contextmanager
    def write(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            run_portfolio_walkforward_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_config(self, canon: dict, now: datetime) -> dict:
        h = C.definition_hash(canon)
        found = self.config_by_hash(h)
        if found:
            return {**found, "created": False}
        row = {"wf_config_id": new_id(), "wf_config_hash": h, "base_config_id": canon["base_config_id"], "base_config_hash": canon["base_config_hash"],
               "start_date": canon["start_date"], "end_date": canon["end_date"], "train_months": canon["train_months"], "test_months": canon["test_months"],
               "step_months": canon["step_months"], "rebalance_frequency": canon["rebalance_frequency"], "selection_metric": canon["selection_metric"],
               "initial_cash": canon["initial_cash"], "transaction_cost_bps": canon["transaction_cost_bps"], "slippage_bps": canon["slippage_bps"],
               "benchmark": canon["benchmark"], "universe_source": canon["universe_source"], "universe_ref": canon["universe_ref"],
               "universe_json": canonical_json(canon["universe_symbols"]), "universe_hash": canon["universe_hash"],
               "candidates_json": canonical_json(canon["candidates"]), "definition_json": canonical_json(canon), "created_at": _iso(now)}
        with self.write() as c:
            c.execute(f"INSERT INTO portfolio_walkforward_configs ({', '.join(_CONFIG_COLS)}) VALUES ({', '.join('?' * len(_CONFIG_COLS))})",
                      tuple(row[k] for k in _CONFIG_COLS))
        return {**self.config_by_hash(h), "created": True}

    def insert_run(self, run: dict, windows: List[dict], candidates: List[dict], selections: List[dict], oos: List[dict], sensitivity: List[dict],
                   metrics: Optional[dict], robustness: Optional[dict], conventions: dict) -> str:
        run_id = new_id()
        row = {k: run.get(k) for k in _RUN_COLS if k != "run_id"}
        row["runtime_s"] = dec_str(row.get("runtime_s") if row.get("runtime_s") is not None else 0)
        if not isinstance(row.get("bars_json"), str):
            row["bars_json"] = canonical_json(row.get("bars_json") or {})
        row["overlapping_tests"] = 1 if row.get("overlapping_tests") else 0
        with self.write() as c:
            c.execute(f"INSERT INTO portfolio_walkforward_runs ({', '.join(_RUN_COLS)}) VALUES ({', '.join('?' * len(_RUN_COLS))})",
                      tuple(run_id if k == "run_id" else row[k] for k in _RUN_COLS))
            for w in windows:
                c.execute(f"INSERT INTO portfolio_walkforward_windows (run_id, {', '.join(_WIN_COLS)}) VALUES (?, {', '.join('?' * len(_WIN_COLS))})",
                          (run_id, *[w[k] for k in _WIN_COLS]))
            for cand in candidates:
                v = {**cand, "train_metrics_json": canonical_json(_jsonable(cand.get("train_metrics") or {}))}
                c.execute(f"INSERT INTO portfolio_walkforward_candidates (run_id, {', '.join(_CAND_COLS)}) VALUES (?, {', '.join('?' * len(_CAND_COLS))})",
                          (run_id, *[v.get(k) for k in _CAND_COLS]))
            for s in selections:
                v = {**s, "tie_break_json": canonical_json(_jsonable(s.get("tie_break") or {})), "config_json": canonical_json(s["config"])}
                c.execute(f"INSERT INTO portfolio_walkforward_selections (run_id, {', '.join(_SEL_COLS)}) VALUES (?, {', '.join('?' * len(_SEL_COLS))})",
                          (run_id, *[v.get(k) for k in _SEL_COLS]))
            for o in oos:
                v = {**o, "test_metrics_json": canonical_json(_jsonable(o.get("test_metrics") or {})), "equity_json": canonical_json(_jsonable(o.get("equity") or []))}
                c.execute(f"INSERT INTO portfolio_walkforward_oos_results (run_id, {', '.join(_OOS_COLS)}) VALUES (?, {', '.join('?' * len(_OOS_COLS))})",
                          (run_id, *[v.get(k) for k in _OOS_COLS]))
            for s in sensitivity:
                c.execute("INSERT INTO portfolio_walkforward_sensitivity (run_id, kind, key, payload_json) VALUES (?, ?, ?, ?)",
                          (run_id, s["kind"], s["key"], canonical_json(_jsonable(s["payload"]))))
            if metrics is not None:
                c.execute("INSERT INTO portfolio_walkforward_metrics (run_id, metrics_json, robustness_json, conventions_json) VALUES (?, ?, ?, ?)",
                          (run_id, canonical_json(_jsonable(metrics)), canonical_json(_jsonable(robustness or {})), canonical_json(conventions)))
        return run_id
