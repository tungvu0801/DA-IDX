"""
rotation_backtest/store.py — append-only persistence for Stage 4.8 backtest definitions, runs and results (DESIGN_48 §9).
Reads never create tables; the first write runs the additive migration. Every row is immutable (triggers).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from database.portfolio_backtest_migrations import TABLES, run_portfolio_backtest_migrations
from fit import readonly as RO
from rotation.store import canonical_json, dec_str

from rotation_backtest import ENGINE_VERSION
from rotation_backtest import config as C
from rotation_backtest import metrics as MX

_CONFIG_COLS = ("backtest_config_id", "backtest_config_hash", "config_id", "config_hash", "start_date", "end_date", "rebalance_frequency",
                "initial_cash", "transaction_cost_bps", "slippage_bps", "benchmark", "execution_price", "universe_source", "universe_ref",
                "universe_json", "universe_hash", "definition_json", "created_at")
_RUN_COLS = ("run_id", "backtest_config_id", "backtest_config_hash", "config_id", "config_hash", "universe_hash", "engine_version", "status",
             "failure_code", "failure_detail", "run_at", "completed_at", "first_session", "last_session", "n_sessions", "n_rebalances",
             "n_rebalances_executed", "n_trades", "initial_cash", "final_equity", "final_cash", "total_costs", "data_hash", "bars_json",
             "result_hash", "market_data_requests", "universe_note")
_REB_COLS = ("seq", "signal_session", "execution_session", "engine_status", "status_detail", "executed", "skip_reason", "reference_equity",
             "cash_before", "turnover", "n_eligible", "n_selected", "n_orders", "n_fills", "traded_notional", "total_cost", "input_hash",
             "proposal_hash", "proposal_json")
_TRADE_COLS = ("seq", "order_index", "signal_session", "execution_session", "symbol", "side", "action", "rank", "requested_qty", "filled_qty",
               "open_price", "fill_price", "notional", "cost", "cash_after", "qty_after", "realised_pnl", "note")
_EQ_COLS = ("session_date", "cash", "positions_value", "equity", "benchmark_index", "n_positions", "cash_weight")
_REB_DEC = ("reference_equity", "cash_before", "turnover", "traded_notional", "total_cost")
_TRADE_DEC = ("open_price", "fill_price", "notional", "cost", "cash_after", "realised_pnl")
_EQ_DEC = ("cash", "positions_value", "equity", "benchmark_index", "cash_weight")


def new_id() -> str:
    return uuid.uuid4().hex


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class BacktestStoreError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class PortfolioBacktestStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    # ---- reads (never create tables) ----
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

    def configs(self, limit: int = 100) -> List[dict]:
        return [self._config_out(r) for r in self.read("SELECT * FROM portfolio_backtest_configs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))]

    def config_by_hash(self, h: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM portfolio_backtest_configs WHERE backtest_config_hash = ?", (h,))
        return self._config_out(rows[0]) if rows else None

    @staticmethod
    def _config_out(r: dict) -> dict:
        return {**r, "universe": json.loads(r["universe_json"]), "definition": json.loads(r["definition_json"]), "universe_note": C.UNIVERSE_NOTE}

    def run(self, run_id: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM portfolio_backtest_runs WHERE run_id = ?", (run_id,))
        return rows[0] if rows else None

    def runs(self, limit: int = 50) -> List[dict]:
        return self.read("SELECT * FROM portfolio_backtest_runs ORDER BY run_at DESC, rowid DESC LIMIT ?", (limit,))

    def rebalances(self, run_id: str) -> List[dict]:
        return [{**r, "proposal": json.loads(r.pop("proposal_json"))} for r in
                self.read("SELECT * FROM portfolio_backtest_rebalances WHERE run_id = ? ORDER BY seq", (run_id,))]

    def trades(self, run_id: str) -> List[dict]:
        return self.read("SELECT * FROM portfolio_backtest_trades WHERE run_id = ? ORDER BY seq, order_index", (run_id,))

    def equity(self, run_id: str) -> List[dict]:
        return self.read("SELECT * FROM portfolio_backtest_equity WHERE run_id = ? ORDER BY session_date", (run_id,))

    def metrics(self, run_id: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM portfolio_backtest_metrics WHERE run_id = ?", (run_id,))
        return {"metrics": json.loads(rows[0]["metrics_json"]), "conventions": json.loads(rows[0]["conventions_json"])} if rows else None

    # ---- writes ----
    @contextmanager
    def write(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            run_portfolio_backtest_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_config(self, canon: dict, now: datetime) -> dict:
        """Insert the immutable definition unless the same hash exists; returns the stored row."""
        h = C.definition_hash(canon)
        found = self.config_by_hash(h)
        if found:
            return {**found, "created": False}
        row = {"backtest_config_id": new_id(), "backtest_config_hash": h, "config_id": canon["config_id"], "config_hash": canon["config_hash"],
               "start_date": canon["start_date"], "end_date": canon["end_date"], "rebalance_frequency": canon["rebalance_frequency"],
               "initial_cash": canon["initial_cash"], "transaction_cost_bps": canon["transaction_cost_bps"], "slippage_bps": canon["slippage_bps"],
               "benchmark": canon["benchmark"], "execution_price": canon["execution_price"], "universe_source": canon["universe_source"],
               "universe_ref": canon["universe_ref"], "universe_json": canonical_json(canon["universe_symbols"]), "universe_hash": canon["universe_hash"],
               "definition_json": canonical_json(canon), "created_at": _iso(now)}
        with self.write() as c:
            c.execute(f"INSERT INTO portfolio_backtest_configs ({', '.join(_CONFIG_COLS)}) VALUES ({', '.join('?' * len(_CONFIG_COLS))})",
                      tuple(row[k] for k in _CONFIG_COLS))
        return {**self.config_by_hash(h), "created": True}

    def insert_run(self, run: dict, rebalances: List[dict], trades: List[dict], equity: List[dict], metrics: Optional[dict]) -> str:
        """Persist one run atomically (run row + every child). Decimals may be Decimal or string."""
        run_id = new_id()
        row = {k: run.get(k) for k in _RUN_COLS if k != "run_id"}
        row["engine_version"] = row.get("engine_version") or ENGINE_VERSION
        for k in ("initial_cash", "final_equity", "final_cash", "total_costs"):
            row[k] = dec_str(row.get(k))
        if not isinstance(row.get("bars_json"), str):
            row["bars_json"] = canonical_json(row.get("bars_json") or {})
        with self.write() as c:
            c.execute(f"INSERT INTO portfolio_backtest_runs ({', '.join(_RUN_COLS)}) VALUES ({', '.join('?' * len(_RUN_COLS))})",
                      tuple(run_id if k == "run_id" else row[k] for k in _RUN_COLS))
            for r in rebalances:
                v = {**r, **{k: dec_str(r.get(k)) for k in _REB_DEC}, "proposal_json": canonical_json(r.get("proposal") or {})}
                c.execute(f"INSERT INTO portfolio_backtest_rebalances (run_id, {', '.join(_REB_COLS)}) VALUES (?, {', '.join('?' * len(_REB_COLS))})",
                          (run_id, *[v.get(k) for k in _REB_COLS]))
            for t in trades:
                v = {**t, **{k: dec_str(t.get(k)) for k in _TRADE_DEC}}
                c.execute(f"INSERT INTO portfolio_backtest_trades (trade_id, run_id, {', '.join(_TRADE_COLS)}) "
                          f"VALUES (?, ?, {', '.join('?' * len(_TRADE_COLS))})", (new_id(), run_id, *[v.get(k) for k in _TRADE_COLS]))
            for e in equity:
                v = {**e, **{k: dec_str(e.get(k)) for k in _EQ_DEC}}
                c.execute(f"INSERT INTO portfolio_backtest_equity (run_id, {', '.join(_EQ_COLS)}) VALUES (?, {', '.join('?' * len(_EQ_COLS))})",
                          (run_id, *[v.get(k) for k in _EQ_COLS]))
            if metrics is not None:
                c.execute("INSERT INTO portfolio_backtest_metrics (run_id, metrics_json, conventions_json) VALUES (?, ?, ?)",
                          (run_id, canonical_json(metrics), canonical_json(MX.CONVENTIONS)))
        return run_id
