"""
signal_research/store.py — append-only persistence for Stage 5.2 research runs (DESIGN_52 §9).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from database.signal_research_migrations import TABLES, run_signal_research_migrations
from fit import readonly as RO
from rotation.store import canonical_json, dec_str

_RUN_COLS = ("run_id", "run_hash", "campaign_id", "campaign_hash", "universe_hash", "sector_map_hash", "diagnostic_run_ids_json", "engine_version", "rules_version", "flags_version",
             "criteria_version", "status", "failure_code", "failure_detail", "run_at", "completed_at", "start_date", "end_date", "n_windows", "n_variants", "n_evaluations", "n_cache_hits",
             "runtime_s", "data_hash", "result_hash", "market_data_requests", "run_flags_json", "benchmarks_json", "definition_json", "universe_note")


def new_id() -> str:
    return uuid.uuid4().hex


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, "quantize"):
        return dec_str(v)
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    return v


def _j(v) -> str:
    return canonical_json(_jsonable(v))


class SignalResearchStore:
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

    @staticmethod
    def _run_row(r: dict, full: bool) -> dict:
        out = {k: v for k, v in r.items() if not k.endswith("_json")}
        out["diagnostic_run_ids"] = json.loads(r["diagnostic_run_ids_json"])
        out["run_flags"] = json.loads(r["run_flags_json"])
        if full:
            out["definition"] = json.loads(r["definition_json"])
            out["benchmarks"] = json.loads(r["benchmarks_json"])
        return out

    def run(self, run_id: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM signal_research_runs WHERE run_id = ?", (run_id,))
        return self._run_row(rows[0], True) if rows else None

    def runs(self, limit: int = 50) -> List[dict]:
        return [self._run_row(r, False) for r in self.read("SELECT * FROM signal_research_runs ORDER BY run_at DESC, rowid DESC LIMIT ?", (limit,))]

    def variants(self, run_id: str, with_summary: bool = False) -> List[dict]:
        out = []
        for r in self.read("SELECT * FROM signal_research_variants WHERE run_id = ? ORDER BY position", (run_id,)):
            row = {k: v for k, v in r.items() if not k.endswith("_json")}
            row.update(config=json.loads(r["config_json"]), research=json.loads(r["research_json"]), flags=json.loads(r["flags_json"]), research_flags=json.loads(r["research_flags_json"]),
                       criteria=json.loads(r["criteria_json"]), oos=json.loads(r["oos_json"]))
            if with_summary:
                row["summary"] = json.loads(r["summary_json"])
            out.append(row)
        return out

    def factors(self, run_id: str) -> List[dict]:
        return [{**{k: v for k, v in r.items() if k != "comparisons_json"}, "comparisons": json.loads(r["comparisons_json"])} for r in
                self.read("SELECT * FROM signal_research_factor_ablation WHERE run_id = ? ORDER BY factor", (run_id,))]

    def sector_tests(self, run_id: str) -> List[dict]:
        return [{**{k: v for k, v in r.items() if k != "delta_json"}, "delta": json.loads(r["delta_json"])} for r in
                self.read("SELECT * FROM signal_research_sector_tests WHERE run_id = ? ORDER BY ranking, max_sector_weight", (run_id,))]

    def regime_tests(self, run_id: str) -> List[dict]:
        return [{**{k: v for k, v in r.items() if not k.endswith("_json")}, "exposure_by_regime": json.loads(r["exposure_json"]), "delta": json.loads(r["delta_json"])} for r in
                self.read("SELECT * FROM signal_research_regime_tests WHERE run_id = ? ORDER BY regime_overlay, config_hash", (run_id,))]

    def combinations(self, run_id: str) -> List[dict]:
        return [{**{k: v for k, v in r.items() if not k.endswith("_json")}, "components": json.loads(r["components_json"]), "delta": json.loads(r["delta_json"]), "criteria_met": bool(r["criteria_met"])}
                for r in self.read("SELECT * FROM signal_research_combinations WHERE run_id = ? ORDER BY config_hash", (run_id,))]

    def comparisons(self, run_id: str) -> List[dict]:
        return self.read("SELECT * FROM signal_research_benchmark_comparisons WHERE run_id = ? ORDER BY config_hash, benchmark, CAST(transaction_cost_bps AS REAL)", (run_id,))

    def scorecards(self, run_id: str) -> List[dict]:
        return [{**{k: v for k, v in r.items() if not k.endswith("_json")}, "scorecard": json.loads(r["scorecard_json"]), "criteria": json.loads(r["criteria_json"]),
                 "research_flags": json.loads(r["research_flags_json"])} for r in self.read("SELECT * FROM signal_research_scorecards WHERE run_id = ? ORDER BY config_hash", (run_id,))]

    @contextmanager
    def write(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            run_signal_research_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert_run(self, run: dict, variants: List[dict], factors: List[dict], sector_tests: List[dict], regime_tests: List[dict], combinations: List[dict], comparisons: List[dict]) -> str:
        rid = new_id()
        row = {k: run.get(k) for k in _RUN_COLS if k != "run_id"}
        row["diagnostic_run_ids_json"] = _j(run.get("diagnostic_run_ids") or [])
        row["run_flags_json"] = _j(run.get("run_flags") or [])
        row["benchmarks_json"] = _j(run.get("benchmarks") or [])
        row["definition_json"] = _j(run.get("definition_json") or {})
        row["runtime_s"] = str(row.get("runtime_s") or "0")
        with self.write() as c:
            c.execute(f"INSERT INTO signal_research_runs ({', '.join(_RUN_COLS)}) VALUES ({', '.join('?' * len(_RUN_COLS))})", tuple(rid if k == "run_id" else row[k] for k in _RUN_COLS))
            for v in variants:
                c.execute("INSERT INTO signal_research_variants (run_id, config_hash, label, family, position, config_json, research_json, flags_json, research_flags_json, criteria_json, oos_json, summary_json) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (rid, v["config_hash"], v["label"], v["family"], int(v["position"]), _j(v["config"]), _j(v["research"]), _j(v["flags"]), _j(v["research_flags"]), _j(v["criteria"]), _j(v["oos"]),
                           _j(v["summary"])))
                c.execute("INSERT INTO signal_research_scorecards (run_id, config_hash, label, family, scorecard_json, criteria_json, research_flags_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (rid, v["config_hash"], v["label"], v["family"], _j(v["scorecard"]), _j(v["criteria"]), _j(v["research_flags"])))
            for f in factors:
                c.execute("INSERT INTO signal_research_factor_ablation (run_id, factor, variant_hash, verdict, n_worse, n_better, comparisons_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (rid, f["factor"], f["variant_hash"], f["verdict"], int(f["worse"]), int(f["better"]), _j(f["comparisons"])))
            for s in sector_tests:
                c.execute("INSERT INTO signal_research_sector_tests (run_id, config_hash, ranking, max_sector_weight, top_sector, top_sector_share, max_sector_weight_observed, top3_share, "
                          "cap_changed_selection, cap_infeasible, delta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (rid, s["config_hash"], s["ranking"], s["max_sector_weight"], s["top_sector"], s["top_sector_share"], s["max_sector_weight_observed"], s["top3_share"],
                           int(s["cap_changed_selection"]), int(s["cap_infeasible"]), _j(s["delta"])))
            for g in regime_tests:
                c.execute("INSERT INTO signal_research_regime_tests (run_id, config_hash, regime_overlay, counterpart_hash, mean_exposure, exposure_json, delta_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (rid, g["config_hash"], g["regime_overlay"], g["counterpart_hash"], g["mean_exposure"], _j(g["exposure_by_regime"]), _j(g["delta"])))
            for cb in combinations:
                c.execute("INSERT INTO signal_research_combinations (run_id, config_hash, components_json, criteria_met, delta_json) VALUES (?, ?, ?, ?, ?)",
                          (rid, cb["config_hash"], _j(cb["components"]), 1 if cb["criteria_met"] else 0, _j(cb["delta"])))
            for x in comparisons:
                c.execute("INSERT INTO signal_research_benchmark_comparisons (run_id, config_hash, benchmark, transaction_cost_bps, slippage_bps, strategy_total_return, benchmark_total_return, excess, "
                          "strategy_sharpe, benchmark_sharpe, pct_windows_beating, median_excess) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (rid, x["config_hash"], x["benchmark"], x["transaction_cost_bps"], x["slippage_bps"], x["strategy_total_return"], x["benchmark_total_return"], x["excess"],
                           x["strategy_sharpe"], x["benchmark_sharpe"], x["pct_windows_beating"], x["median_excess"]))
        return rid
