"""
research_flow/store.py — the local research cache and batch observability (database/research_cache_migrations.py).

Reads never create tables and open the database read-only; every write runs the additive migration first inside ONE
`BEGIN IMMEDIATE` transaction. Rows are append-only (triggers refuse UPDATE / DELETE): the latest row for a cache key is the
cache entry, a user refresh appends a newer one. Nothing here calls a model, a broker or the network.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from database import research_cache_migrations as M
from fit import readonly as RO


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class ResearchStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    def _ro(self):
        conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    def exists(self) -> bool:
        if not self.path.exists():
            return False
        conn = self._ro()
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        return set(M.TABLES) <= names

    def read(self, sql: str, args=()) -> List[dict]:
        if not self.exists():
            return []
        conn = self._ro()
        try:
            return [self._out(dict(r)) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    @staticmethod
    def _out(r: dict) -> dict:
        for k in ("request_json", "result_json", "report_json"):
            if k in r:
                r[k[:-5]] = json.loads(r[k]) if r[k] is not None else None
                del r[k]
        return r

    @contextmanager
    def write(self):
        conn = sqlite3.connect(str(self.path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=15000;")
            M.run_research_cache_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    # ---- cache -----------------------------------------------------------------------------------------------------------
    def latest(self, cache_key: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM research_results WHERE cache_key = ? ORDER BY id DESC LIMIT 1", (cache_key,))
        return rows[0] if rows else None

    def latest_for_symbol(self, symbol: str, request_type: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM research_results WHERE symbol = ? AND request_type = ? ORDER BY id DESC LIMIT 1", (symbol, request_type))
        return rows[0] if rows else None

    def refreshes_since(self, symbol: str, request_type: str, since_iso: str) -> int:
        rows = self.read("SELECT count(*) AS n FROM research_results WHERE symbol = ? AND request_type = ? AND refresh = 1 AND created_at >= ?",
                         (symbol, request_type, since_iso))
        return int(rows[0]["n"]) if rows else 0

    def insert_result(self, *, cache_key: str, symbol: str, request_type: str, bucket: str, context_hash: str, run_id: Optional[str], status: str,
                      reason: Optional[str], request: dict, result: Optional[dict], model: Optional[str], prompt_version: str, claude_calls: int,
                      refresh: bool, created_at: datetime, stale_after: str) -> int:
        with self.write() as c:
            cur = c.execute("INSERT INTO research_results (cache_key, symbol, request_type, bucket, context_hash, run_id, status, reason, "
                            "request_json, result_json, model, prompt_version, claude_calls, refresh, created_at, stale_after) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (cache_key, symbol, request_type, bucket, context_hash, run_id, status, reason, json.dumps(request, sort_keys=True),
                             None if result is None else json.dumps(result, sort_keys=True), model, prompt_version, int(claude_calls),
                             1 if refresh else 0, _iso(created_at), stale_after))
            return int(cur.lastrowid)

    # ---- observability -----------------------------------------------------------------------------------------------------
    def insert_batch(self, run_id: str, report: dict, created_at: datetime) -> int:
        with self.write() as c:
            cur = c.execute("INSERT INTO research_batches (run_id, created_at, report_json) VALUES (?, ?, ?)",
                            (run_id, _iso(created_at), json.dumps(report, sort_keys=True)))
            return int(cur.lastrowid)

    def batches(self, run_id: str, limit: int = 20) -> List[dict]:
        return self.read("SELECT * FROM research_batches WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run_id, int(limit)))

    def results_for_run(self, run_id: str) -> List[dict]:
        return self.read("SELECT * FROM research_results WHERE run_id = ? ORDER BY symbol, id", (run_id,))
