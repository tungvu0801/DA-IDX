"""
rotation_campaign/store.py — append-only persistence for Stage 5.0 campaigns (DESIGN_50 §5).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from database.model_campaign_migrations import TABLES, run_model_campaign_migrations
from fit import readonly as RO
from rotation.store import canonical_json, dec_str

from rotation_campaign import config as C

_RUN_COLS = ("campaign_id", "campaign_hash", "base_config_hash", "universe_hash", "engine_version", "walkforward_version", "backtest_version", "robustness_version",
             "status", "failure_code", "failure_detail", "run_at", "completed_at", "n_candidates", "n_rejected", "n_discarded", "n_windows", "n_eligible",
             "n_finalists", "n_evaluations", "n_cache_hits", "runtime_s", "data_hash", "walkforward_result_hash", "result_hash", "market_data_requests",
             "definition_json", "universe_note")


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


class ModelCampaignStore:
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

    def campaign(self, cid: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM model_campaigns WHERE campaign_id = ?", (cid,))
        return {**rows[0], "definition": json.loads(rows[0].pop("definition_json"))} if rows else None

    def campaigns(self, limit: int = 50) -> List[dict]:
        return [{k: v for k, v in r.items() if k != "definition_json"} for r in
                self.read("SELECT * FROM model_campaigns ORDER BY run_at DESC, rowid DESC LIMIT ?", (limit,))]

    def candidates(self, cid: str) -> List[dict]:
        return [{**r, "config": json.loads(r.pop("config_json")), "evidence": json.loads(r.pop("evidence_json"))} for r in
                self.read("SELECT * FROM model_campaign_candidates WHERE campaign_id = ? ORDER BY config_hash", (cid,))]

    def leaderboard(self, cid: str) -> List[dict]:
        return [{**json.loads(r["row_json"]), "rank": r["rank"], "eligible": bool(r["eligible"]), "exclusions": json.loads(r["exclusion_json"])} for r in
                self.read("SELECT * FROM model_campaign_leaderboard WHERE campaign_id = ? ORDER BY rank", (cid,))]

    def finalists(self, cid: str) -> List[dict]:
        return self.read("SELECT * FROM model_campaign_finalists WHERE campaign_id = ? ORDER BY finalist_rank", (cid,))

    def cards(self, cid: str) -> List[dict]:
        return [json.loads(r["card_json"]) for r in self.read("SELECT * FROM model_campaign_model_cards WHERE campaign_id = ? ORDER BY config_hash", (cid,))]

    def metrics(self, cid: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM model_campaign_metrics WHERE campaign_id = ?", (cid,))
        if not rows:
            return None
        r = rows[0]
        return {"comparison": json.loads(r["comparison_json"]), "stability": json.loads(r["stability_json"]), "conventions": json.loads(r["conventions_json"])}

    @contextmanager
    def write(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            run_model_campaign_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert_campaign(self, run: dict, candidates: List[dict], leaderboard: List[dict], fins: List[dict], cards: List[dict], comparison: Optional[dict],
                        stability: Optional[dict], conventions: dict) -> str:
        cid = new_id()
        row = {k: run.get(k) for k in _RUN_COLS if k != "campaign_id"}
        if not isinstance(row.get("definition_json"), str):
            row["definition_json"] = canonical_json(_jsonable(row.get("definition_json") or {}))
        row["runtime_s"] = str(row.get("runtime_s") or "0")
        with self.write() as c:
            c.execute(f"INSERT INTO model_campaigns ({', '.join(_RUN_COLS)}) VALUES ({', '.join('?' * len(_RUN_COLS))})",
                      tuple(cid if k == "campaign_id" else row[k] for k in _RUN_COLS))
            for cand in candidates:
                c.execute("INSERT INTO model_campaign_candidates (campaign_id, config_hash, label, config_json, evidence_json) VALUES (?, ?, ?, ?, ?)",
                          (cid, cand["config_hash"], cand["label"], canonical_json(cand["config"]), canonical_json(_jsonable(cand["evidence"]))))
            for r in leaderboard:
                c.execute("INSERT INTO model_campaign_leaderboard (campaign_id, rank, config_hash, label, eligible, exclusion_json, row_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (cid, r["rank"], r["config_hash"], r["label"], 1 if r["eligible"] else 0, canonical_json(_jsonable(r["exclusions"])),
                           canonical_json(_jsonable({k: v for k, v in r.items() if k not in ("rank", "eligible", "exclusions")}))))
            for f in fins:
                c.execute("INSERT INTO model_campaign_finalists (campaign_id, finalist_rank, config_hash, label, role, robustness_score) VALUES (?, ?, ?, ?, ?, ?)",
                          (cid, f["finalist_rank"], f["config_hash"], f["label"], f["role"], float(f["robustness_score"])))
            for card in cards:
                c.execute("INSERT INTO model_campaign_model_cards (campaign_id, config_hash, card_json) VALUES (?, ?, ?)", (cid, card["config_hash"], canonical_json(_jsonable(card))))
            if comparison is not None:
                c.execute("INSERT INTO model_campaign_metrics (campaign_id, comparison_json, stability_json, conventions_json) VALUES (?, ?, ?, ?)",
                          (cid, canonical_json(_jsonable(comparison)), canonical_json(_jsonable(stability or {})), canonical_json(conventions)))
        return cid
