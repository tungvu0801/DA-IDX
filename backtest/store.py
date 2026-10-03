"""
backtest/store.py — SQLite access for Stage 3.2 (same database file as the rest of the app).

Cached bar datasets and backtest results are append-only (enforced by triggers in database/backtest_migrations.py).
A run's results are written in ONE transaction together with its COMPLETED status, so a crash can never leave partial
results labelled complete. Runs left PENDING / RUNNING by a previous server process are marked FAILED (INTERRUPTED)
when the store is first opened in a new process: jobs are one-shot threads, so nothing can resume them.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from database.backtest_migrations import run_backtest_migrations

BarRow = Tuple[str, str, float, float, float, float, float]   # session_date, bar_timestamp, open, high, low, close, volume


class BacktestError(Exception):
    def __init__(self, code: str, message: str, detail=None, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.detail, self.status = code, message, detail, status


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bars_content_hash(rows: Sequence[BarRow]) -> str:
    """SHA-256 over the canonical bar rows in session order (identifies the exact data a run used)."""
    return sha256(canonical([list(r) for r in rows]))


class BacktestStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        with self._connect() as conn:
            run_backtest_migrations(conn)
            conn.execute("UPDATE backtest_runs SET status = 'FAILED', finished_at = ?, error_code = 'INTERRUPTED', "
                         "error_message = 'The server stopped before this one-shot run finished; nothing was saved. "
                         "Start a new run.' WHERE status IN ('PENDING', 'RUNNING')", (now_iso(),))
            conn.commit()

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

    # ---- bar datasets (immutable) ----------------------------------------------------------------------------------
    def insert_dataset(self, symbol: str, source: str, feed: str, adjustment: str, requested_start: str,
                       requested_end: str, rows: List[BarRow], fetched_at: str) -> dict:
        rows = sorted(rows, key=lambda r: r[0])
        ds = {"dataset_id": uuid.uuid4().hex, "symbol": symbol, "source": source, "feed": feed, "adjustment": adjustment,
              "timeframe": "1Day", "requested_start": requested_start, "requested_end": requested_end,
              "first_session": rows[0][0] if rows else None, "last_session": rows[-1][0] if rows else None,
              "bar_count": len(rows), "content_hash": bars_content_hash(rows), "fetched_at": fetched_at}
        with self._connect() as conn:
            conn.execute("INSERT INTO historical_bar_datasets (dataset_id, symbol, source, feed, adjustment, timeframe, "
                         "requested_start, requested_end, first_session, last_session, bar_count, content_hash, "
                         "fetched_at) VALUES (:dataset_id, :symbol, :source, :feed, :adjustment, :timeframe, "
                         ":requested_start, :requested_end, :first_session, :last_session, :bar_count, :content_hash, "
                         ":fetched_at)", ds)
            conn.executemany("INSERT INTO historical_daily_bars (dataset_id, session_date, bar_timestamp, open, high, low, "
                             "close, volume) VALUES (?,?,?,?,?,?,?,?)", [(ds["dataset_id"], *r) for r in rows])
            conn.commit()
        return ds

    def covering_dataset(self, symbol: str, feed: str, adjustment: str, need_start: str, need_end: str) -> Optional[dict]:
        """The most recently fetched dataset whose REQUESTED range covers [need_start, need_end] (newest wins)."""
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM historical_bar_datasets WHERE symbol = ? AND feed = ? AND adjustment = ? "
                             "AND requested_start <= ? AND requested_end >= ? ORDER BY fetched_at DESC, rowid DESC "
                             "LIMIT 1", (symbol, feed, adjustment, need_start, need_end)).fetchone()
        return dict(r) if r else None

    def dataset(self, dataset_id: str) -> Optional[dict]:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM historical_bar_datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
        return dict(r) if r else None

    def dataset_rows(self, dataset_id: str, verify_hash: Optional[str] = None) -> List[BarRow]:
        with self._connect(readonly=True) as conn:
            rows = [tuple(r) for r in conn.execute(
                "SELECT session_date, bar_timestamp, open, high, low, close, volume FROM historical_daily_bars "
                "WHERE dataset_id = ? ORDER BY session_date", (dataset_id,))]
        if verify_hash is not None and bars_content_hash(rows) != verify_hash:
            raise BacktestError("DATA_INTEGRITY_ERROR", f"Cached bars for dataset {dataset_id[:12]} no longer match their "
                                "content hash.", status=409)
        return rows

    def dataset_count(self) -> int:
        with self._connect(readonly=True) as conn:
            return conn.execute("SELECT COUNT(*) FROM historical_bar_datasets").fetchone()[0]

    # ---- runs ------------------------------------------------------------------------------------------------------
    def create_run(self, ref: dict, cfg_json: str, cfg_hash: str, data_json: str, data_hash: str,
                   preflight_json: str, engine_version: str, start: str, end: str) -> str:
        run_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO backtest_runs (run_id, strategy_id, strategy_version_id, version_number, spec_hash, "
                "rules_hash, feature_registry_version, feature_registry_fingerprint, engine_version, start_date, end_date, "
                "config_json, config_hash, data_json, data_hash, preflight_json, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',?)",
                (run_id, ref["strategy_id"], ref["strategy_version_id"], ref["version_number"], ref["spec_hash"],
                 ref["rules_hash"], ref["feature_registry_version"], ref["feature_registry_fingerprint"], engine_version,
                 start, end, cfg_json, cfg_hash, data_json, data_hash, preflight_json, now_iso()))
            conn.commit()
        return run_id

    def mark_running(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE backtest_runs SET status = 'RUNNING', started_at = ? WHERE run_id = ? AND status = 'PENDING'",
                         (now_iso(), run_id))
            conn.commit()

    def fail_run(self, run_id: str, code: str, message: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE backtest_runs SET status = 'FAILED', finished_at = ?, error_code = ?, error_message = ? "
                         "WHERE run_id = ? AND status IN ('PENDING', 'RUNNING')", (now_iso(), code, message[:500], run_id))
            conn.commit()

    def complete_run(self, run_id: str, trades: List[dict], signals: List[dict], equity: List[dict], result: dict,
                     point_in_time_safe: bool) -> float:
        """Insert every result row and mark COMPLETED in one transaction. Returns seconds spent writing."""
        import time
        t0 = time.perf_counter()
        with self._connect() as conn:
            try:
                conn.execute("BEGIN")
                if trades:
                    cols = list(trades[0])
                    conn.executemany(f"INSERT INTO backtest_trades (run_id, {', '.join(cols)}) VALUES "
                                     f"(?{', ?' * len(cols)})", [(run_id, *[t[c] for c in cols]) for t in trades])
                conn.executemany("INSERT INTO backtest_signals (run_id, seq, session_date, symbol, event_type, reason_code, "
                                 "trade_no, detail_json) VALUES (?,?,?,?,?,?,?,?)",
                                 [(run_id, s["seq"], s["session_date"], s["symbol"], s["event_type"], s["reason_code"],
                                   s["trade_no"], s["detail_json"]) for s in signals])
                conn.executemany("INSERT INTO backtest_equity (run_id, session_date, cash, market_value, equity, "
                                 "open_positions, daily_return, drawdown, spy_benchmark_equity, universe_benchmark_equity) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 [(run_id, e["session_date"], e["cash"], e["market_value"], e["equity"], e["open_positions"],
                                   e["daily_return"], e["drawdown"], e["spy_benchmark_equity"],
                                   e["universe_benchmark_equity"]) for e in equity])
                result = {**result, "timings": {**result.get("timings", {}), "database_write_s": round(time.perf_counter() - t0, 3)}}
                cur = conn.execute("UPDATE backtest_runs SET status = 'COMPLETED', finished_at = ?, result_json = ?, "
                                   "point_in_time_safe = ? WHERE run_id = ? AND status = 'RUNNING'",
                                   (now_iso(), canonical(result), 1 if point_in_time_safe else 0, run_id))
                if cur.rowcount != 1:
                    raise sqlite3.IntegrityError("run is not RUNNING")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return time.perf_counter() - t0

    @staticmethod
    def _run(row: sqlite3.Row, full: bool) -> dict:
        d = dict(row)
        for k in ("config_json", "data_json", "preflight_json", "result_json"):
            d[k.replace("_json", "")] = json.loads(d.pop(k)) if d.get(k) else None
        d["point_in_time_safe"] = None if d["point_in_time_safe"] is None else bool(d["point_in_time_safe"])
        if not full:
            for k in ("data", "preflight"):
                d.pop(k, None)
            res = d.pop("result")
            d["summary"] = None if not res else {k: res["metrics"].get(k) for k in (
                "initial_equity", "ending_equity", "total_return_pct", "closed_trades", "open_positions_at_end",
                "max_drawdown_pct")} | {"sample": res.get("sample")}
        return d

    def get_run(self, run_id: str) -> dict:
        with self._connect(readonly=True) as conn:
            r = conn.execute("SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,)).fetchone()
        if r is None:
            raise BacktestError("NOT_FOUND", "Backtest run not found.", status=404)
        return self._run(r, full=True)

    def list_runs(self, strategy_id: Optional[str] = None, version_number: Optional[int] = None, limit: int = 100) -> List[dict]:
        q, args = "SELECT * FROM backtest_runs", []
        where = []
        if strategy_id:
            where.append("strategy_id = ?")
            args.append(strategy_id)
        if version_number is not None:
            where.append("version_number = ?")
            args.append(version_number)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with self._connect(readonly=True) as conn:
            rows = conn.execute(q, (*args, limit)).fetchall()
        return [self._run(r, full=False) for r in rows]

    def trades(self, run_id: str) -> List[dict]:
        with self._connect(readonly=True) as conn:
            rows = conn.execute("SELECT * FROM backtest_trades WHERE run_id = ? ORDER BY trade_no", (run_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("all_exit_reasons", "entry_feature_snapshot", "entry_evaluation_trace", "exit_feature_snapshot",
                      "exit_evaluation_trace"):
                d[k] = json.loads(d[k]) if d[k] else None
            out.append(d)
        return out

    def signals(self, run_id: str) -> List[dict]:
        with self._connect(readonly=True) as conn:
            rows = conn.execute("SELECT seq, session_date, symbol, event_type, reason_code, trade_no, detail_json FROM "
                                "backtest_signals WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        return [{**{k: r[k] for k in r.keys() if k != "detail_json"}, "detail": json.loads(r["detail_json"])} for r in rows]

    def equity(self, run_id: str) -> List[dict]:
        with self._connect(readonly=True) as conn:
            rows = conn.execute("SELECT session_date, cash, market_value, equity, open_positions, daily_return, drawdown, "
                                "spy_benchmark_equity, universe_benchmark_equity FROM backtest_equity WHERE run_id = ? "
                                "ORDER BY session_date", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def strategy_version_row(self, strategy_id: str, version_number: int) -> Tuple[Optional[dict], Optional[dict], List[str]]:
        """(definition, version, immutability triggers present) — read-only, straight from the Stage 3.1 tables."""
        with self._connect(readonly=True) as conn:
            d = conn.execute("SELECT * FROM strategy_definitions WHERE strategy_id = ?", (strategy_id,)).fetchone()
            v = conn.execute("SELECT * FROM strategy_versions WHERE strategy_id = ? AND version_number = ?",
                             (strategy_id, version_number)).fetchone()
            trig = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = "
                                               "'strategy_versions'")]
        return (dict(d) if d else None), (dict(v) if v else None), sorted(trig)


_stores: Dict[str, BacktestStore] = {}
_lock = threading.Lock()


def get_backtest_store() -> BacktestStore:
    """Same database file as the rest of the app (database.database.get_db().db_path); tables created on first use."""
    from database.database import get_db
    path = str(Path(get_db().db_path))
    with _lock:
        if path not in _stores:
            _stores[path] = BacktestStore(Path(path))
        return _stores[path]
