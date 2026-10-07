"""
rotation/store.py — Stage 4.7 SQLite access for the five portfolio_rotation_* tables (database/rotation_migrations.py).

Reads never create tables (no configuration yet = no tables) and open the database read-only. Every write runs the
additive, idempotent migration first and happens inside ONE `BEGIN IMMEDIATE` transaction: a configuration version is one
insert; a run is inserted together with ALL of its candidates, targets and rebalance items — all or nothing. Rows are
immutable (database triggers refuse UPDATE and DELETE); nothing here repairs, rewrites or removes data.

Canonicalisation: `canonical_json` sorts keys, uses compact separators and writes Decimals as plain decimal strings
(`format(d, "f")`, never exponent notation, never floats). `config_hash` and the stored-row `proposal_hash` are sha256 of
canonical JSON, so logically identical input in any key / symbol order gives the same hash.

This module never fetches market data, never calls Robinhood or Alpaca, performs no broker operation, computes no trading
factor and constructs no executable order. Configuration validation delegates to the pure rules (rotation/rules.py) so a
configuration that could never run is never persisted.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from database import rotation_migrations as M
from database.rotation_migrations import run_rotation_migrations
from fit import readonly as RO
from paper import alpaca_order_rules as RU                       # SYMBOL_RE only (pure constants of the frozen module)
from rotation import rules as R

CONFIG_KEYS = ("weights", "portfolio_size", "exit_rank", "cash_buffer_pct", "rebalance_threshold",
               "max_turnover_per_rotation", "max_position_weight", "min_position_weight", "min_price",
               "min_avg_dollar_volume", "min_history_sessions", "max_snapshot_age_min", "excluded_symbols", "benchmark")
CONFIG_DEFAULTS = {"min_price": "5.00", "min_avg_dollar_volume": "5000000.00", "min_history_sessions": 252,
                   "max_snapshot_age_min": 30, "excluded_symbols": [], "benchmark": M.BENCHMARK}
_RUN_COLS = ("run_id", "config_id", "config_hash", "run_at", "data_session", "benchmark", "universe_source", "universe_ref",
             "universe_json", "universe_hash", "portfolio_source", "portfolio_snapshot_at", "snapshot_status", "snapshot_cash",
             "positions_json", "source_meta_json", "source_mismatch_note", "reference_equity", "current_cash_weight",
             "target_cash_weight", "input_hash", "proposal_hash", "status", "status_detail", "n_universe", "n_eligible",
             "n_selected", "turnover", "market_data_requests", "completed_at")
_CAND_COLS = ("symbol", "eligible", "reasons_json", "raw_json", "scores_json", "composite", "rank", "reference_price",
              "avg_dollar_volume", "flags_json")
_TARGET_COLS = ("symbol", "rank", "target_weight", "reference_price", "target_notional", "est_target_qty", "reason", "flags_json")
_ITEM_COLS = ("symbol", "current_qty", "current_weight", "target_weight", "weight_diff", "est_qty_diff", "side_hint", "action",
              "reason", "handoff_allowed")
_HASHED_TARGET = ("symbol", "rank", "target_weight", "reference_price", "target_notional", "est_target_qty", "reason")
_HASHED_ITEM = ("symbol", "current_qty", "current_weight", "target_weight", "weight_diff", "est_qty_diff", "side_hint", "action",
                "reason", "handoff_allowed")


class StoreError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---- canonical values ---------------------------------------------------------------------------------------------------------------

def dec_str(d) -> Optional[str]:
    """A Decimal as a plain decimal string (no exponent); None stays None; strings pass through unchanged."""
    if d is None or isinstance(d, str):
        return d
    if isinstance(d, bool) or isinstance(d, float):
        raise StoreError("INVALID_VALUE", f"decimal values must be Decimal or str, not {type(d).__name__}")
    return format(d if isinstance(d, Decimal) else Decimal(d), "f")


def _default(o):
    if isinstance(o, Decimal):
        return format(o, "f")
    raise TypeError(f"not canonicalisable: {type(o).__name__}")


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id() -> str:
    return uuid.uuid4().hex


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _q(d: Decimal, places: str) -> str:
    return format(d.quantize(Decimal(places), rounding=ROUND_HALF_EVEN), "f")


def _loads(v, default=None):
    return default if v is None else json.loads(v)


# ---- configuration canonical form -----------------------------------------------------------------------------------------------------

def normalise_config(config: Dict) -> Dict:
    """Validate a configuration with the pure Stage 4.7 rules and return its canonical form (6-dp decimal strings,
    sorted unique excluded symbols, benchmark SPY). Raises StoreError / rules.ConfigError; nothing invalid is ever stored."""
    if not isinstance(config, dict):
        raise StoreError("INVALID_CONFIG", "config must be an object")
    unknown = set(config) - set(CONFIG_KEYS)
    if unknown:
        raise StoreError("INVALID_CONFIG", f"unknown config fields {sorted(unknown)}")
    cfg = {**CONFIG_DEFAULTS, **config}
    missing = [k for k in CONFIG_KEYS if k not in cfg]
    if missing:
        raise StoreError("INVALID_CONFIG", f"missing config fields {missing}")
    if cfg["benchmark"] != M.BENCHMARK:
        raise StoreError("INVALID_CONFIG", "benchmark is fixed to SPY in Stage 4.7 V1")
    weights = R.validate_weights(cfg["weights"])
    p = R.validate_portfolio_config(cfg["portfolio_size"], cfg["exit_rank"], cfg["cash_buffer_pct"], cfg["min_position_weight"],
                                    cfg["max_position_weight"], cfg["rebalance_threshold"], cfg["max_turnover_per_rotation"])
    min_price, min_adv = R.dec(cfg["min_price"], "min_price"), R.dec(cfg["min_avg_dollar_volume"], "min_avg_dollar_volume")
    if min_price <= 0 or min_adv < 0:
        raise StoreError("INVALID_CONFIG", "min_price must be > 0 and min_avg_dollar_volume >= 0")
    for k in ("min_history_sessions", "max_snapshot_age_min"):
        v = cfg[k]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise StoreError("INVALID_CONFIG", f"{k} must be an integer >= 1")
    if not isinstance(cfg["excluded_symbols"], (list, tuple)):
        raise StoreError("INVALID_CONFIG", "excluded_symbols must be a list")
    excluded = sorted({str(s).strip().upper() for s in cfg["excluded_symbols"]})
    bad = [s for s in excluded if not RU.SYMBOL_RE.match(s)]
    if bad:
        raise StoreError("INVALID_CONFIG", f"invalid excluded symbols {bad}")
    return {"weights": {k: format(weights[k], "f") for k in R.WEIGHT_KEYS}, "portfolio_size": p["portfolio_size"],
            "exit_rank": p["exit_rank"], "cash_buffer_pct": _q(p["cash_buffer_pct"], "0.000001"),
            "rebalance_threshold": _q(p["rebalance_threshold"], "0.000001"),
            "max_turnover_per_rotation": _q(p["max_turnover_per_rotation"], "0.000001"),
            "max_position_weight": _q(p["max_position_weight"], "0.000001"), "min_position_weight": _q(p["min_position_weight"], "0.000001"),
            "min_price": _q(min_price, "0.0001"), "min_avg_dollar_volume": _q(min_adv, "0.01"),
            "min_history_sessions": cfg["min_history_sessions"], "max_snapshot_age_min": cfg["max_snapshot_age_min"],
            "excluded_symbols": excluded, "benchmark": M.BENCHMARK}


def config_hash(canonical_config: Dict) -> str:
    return sha256_hex(canonical_json(canonical_config))


def proposal_hash(status: str, turnover, current_cash_weight, target_cash_weight, targets: Iterable[Dict],
                  items: Iterable[Dict]) -> str:
    """The stored-row proposal hash: status, turnover, both cash weights and the hashed fields of every target and
    rebalance item, symbols sorted. Computable from the engine's rows before insert and from the stored rows afterwards."""
    body = {"status": status, "turnover": dec_str(turnover), "current_cash_weight": dec_str(current_cash_weight),
            "target_cash_weight": dec_str(target_cash_weight),
            "targets": sorted(({k: (dec_str(t.get(k)) if k in ("target_weight", "reference_price", "target_notional") else t.get(k))
                                for k in _HASHED_TARGET} for t in targets), key=lambda t: t["symbol"]),
            "items": sorted(({k: (dec_str(i.get(k)) if k in ("current_qty", "current_weight", "target_weight", "weight_diff") else i.get(k))
                              for k in _HASHED_ITEM} for i in items), key=lambda i: i["symbol"])}
    return sha256_hex(canonical_json(body))


# ---- the store ------------------------------------------------------------------------------------------------------------------------

class RotationStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    # ---- reads (never create anything) --------------------------------------------------------------------------------
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
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    def config(self, config_id: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM portfolio_rotation_configs WHERE config_id = ?", (str(config_id),))
        return self._config_out(rows[0]) if rows else None

    def config_by_hash(self, h: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM portfolio_rotation_configs WHERE config_hash = ?", (str(h),))
        return self._config_out(rows[0]) if rows else None

    def configs(self, name: Optional[str] = None) -> List[dict]:
        if name is None:
            rows = self.read("SELECT * FROM portfolio_rotation_configs ORDER BY name, version")
        else:
            rows = self.read("SELECT * FROM portfolio_rotation_configs WHERE name = ? ORDER BY version", (name,))
        return [self._config_out(r) for r in rows]

    @staticmethod
    def _config_out(r: dict) -> dict:
        return {**r, "config": json.loads(r["config_json"])}

    def run(self, run_id: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM portfolio_rotation_runs WHERE run_id = ?", (str(run_id),))
        return rows[0] if rows else None

    def runs(self, limit: int = 50) -> List[dict]:
        return self.read("SELECT * FROM portfolio_rotation_runs ORDER BY run_at DESC, run_id DESC LIMIT ?", (int(limit),))

    def candidates(self, run_id: str) -> List[dict]:
        """Eligible symbols first in rank order, then ineligible symbols alphabetically."""
        return self.read("SELECT * FROM portfolio_rotation_candidates WHERE run_id = ? "
                         "ORDER BY (rank IS NULL), rank, symbol", (str(run_id),))

    def targets(self, run_id: str) -> List[dict]:
        return self.read("SELECT * FROM portfolio_rotation_targets WHERE run_id = ? ORDER BY rank, symbol", (str(run_id),))

    def items(self, run_id: str) -> List[dict]:
        return self.read("SELECT * FROM portfolio_rebalance_items WHERE run_id = ? ORDER BY symbol", (str(run_id),))

    # ---- writes ---------------------------------------------------------------------------------------------------------
    @contextmanager
    def write(self):
        conn = sqlite3.connect(str(self.path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=15000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            run_rotation_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def create_config(self, name: str, config: Dict, now: datetime) -> dict:
        """Persist an immutable configuration version. The same name gets the next version number; a configuration whose
        canonical content already exists is returned as-is (`created` False) — nothing is ever updated."""
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise StoreError("INVALID_CONFIG", "name must be 1-80 characters")
        name = name.strip()
        canon = normalise_config(config)
        h = config_hash(canon)
        with self.write() as c:
            cur = c.execute("SELECT * FROM portfolio_rotation_configs WHERE config_hash = ?", (h,)).fetchone()
            if cur is not None:
                return {**self._config_out(dict(cur)), "created": False}
            version = c.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM portfolio_rotation_configs WHERE name = ?",
                                (name,)).fetchone()[0]
            cid = new_id()
            c.execute("INSERT INTO portfolio_rotation_configs (config_id, name, version, config_json, config_hash, benchmark, "
                      "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (cid, name, version, canonical_json(canon), h, M.BENCHMARK, _iso(now)))
        return {**self.config(cid), "created": True}

    def insert_run(self, run: Dict, candidates: List[Dict], targets: List[Dict], items: List[Dict]) -> str:
        """Persist ONE complete run atomically (run row + every child) and return its run_id. `run` carries the columns of
        portfolio_rotation_runs except run_id (generated here) and benchmark (fixed); Decimals may be passed as Decimal or
        string; JSON columns may be passed as Python objects. Any constraint failure rolls everything back."""
        run_id = new_id()
        row = {k: run.get(k) for k in _RUN_COLS if k not in ("run_id", "benchmark")}
        for k in ("universe_json", "positions_json", "source_meta_json"):
            if row.get(k) is not None and not isinstance(row[k], str):
                row[k] = canonical_json(row[k])
        for k in ("snapshot_cash", "reference_equity", "current_cash_weight", "target_cash_weight", "turnover"):
            row[k] = dec_str(row.get(k))
        self._check_children(candidates, targets, items)
        with self.write() as c:
            c.execute(f"INSERT INTO portfolio_rotation_runs ({', '.join(_RUN_COLS)}) VALUES ({', '.join('?' * len(_RUN_COLS))})",
                      tuple(run_id if k == "run_id" else M.BENCHMARK if k == "benchmark" else row[k] for k in _RUN_COLS))
            for cand in candidates:
                v = self._cand_row(cand)
                c.execute(f"INSERT INTO portfolio_rotation_candidates (run_id, {', '.join(_CAND_COLS)}) "
                          f"VALUES (?, {', '.join('?' * len(_CAND_COLS))})", (run_id, *[v[k] for k in _CAND_COLS]))
            for t in targets:
                v = self._target_row(t)
                c.execute(f"INSERT INTO portfolio_rotation_targets (run_id, {', '.join(_TARGET_COLS)}) "
                          f"VALUES (?, {', '.join('?' * len(_TARGET_COLS))})", (run_id, *[v[k] for k in _TARGET_COLS]))
            for it in items:
                v = self._item_row(it)
                c.execute(f"INSERT INTO portfolio_rebalance_items (item_id, run_id, {', '.join(_ITEM_COLS)}) "
                          f"VALUES (?, ?, {', '.join('?' * len(_ITEM_COLS))})", (new_id(), run_id, *[v[k] for k in _ITEM_COLS]))
        return run_id

    @staticmethod
    def _check_children(candidates, targets, items) -> None:
        for name, rows in (("candidates", candidates), ("targets", targets), ("items", items)):
            syms = [r.get("symbol") for r in rows]
            if len(set(syms)) != len(syms):
                raise StoreError("DUPLICATE_SYMBOL", f"duplicate symbol among {name}")
        known = {r.get("symbol") for r in candidates}
        stray = sorted({t.get("symbol") for t in targets} - known)
        if stray:
            raise StoreError("UNKNOWN_TARGET", f"targets without a candidate row: {stray}")

    @staticmethod
    def _json_col(v, default):
        return canonical_json(default if v is None else v) if not isinstance(v, str) else v

    def _cand_row(self, r: Dict) -> Dict:
        return {"symbol": r["symbol"], "eligible": 1 if r.get("eligible") else 0, "reasons_json": self._json_col(r.get("reasons"), []),
                "raw_json": self._json_col(r.get("raw"), {}),
                "scores_json": None if r.get("scores") is None else self._json_col(r.get("scores"), {}),
                "composite": dec_str(r.get("composite")), "rank": r.get("rank"), "reference_price": dec_str(r.get("reference_price")),
                "avg_dollar_volume": dec_str(r.get("avg_dollar_volume")), "flags_json": self._json_col(r.get("flags"), [])}

    def _target_row(self, r: Dict) -> Dict:
        return {"symbol": r["symbol"], "rank": r["rank"], "target_weight": dec_str(r["target_weight"]),
                "reference_price": dec_str(r["reference_price"]), "target_notional": dec_str(r["target_notional"]),
                "est_target_qty": r["est_target_qty"], "reason": r["reason"], "flags_json": self._json_col(r.get("flags"), [])}

    @staticmethod
    def _item_row(r: Dict) -> Dict:
        return {"symbol": r["symbol"], "current_qty": dec_str(r["current_qty"]), "current_weight": dec_str(r["current_weight"]),
                "target_weight": dec_str(r["target_weight"]), "weight_diff": dec_str(r["weight_diff"]), "est_qty_diff": r["est_qty_diff"],
                "side_hint": r.get("side_hint"), "action": r["action"], "reason": r.get("reason"),
                "handoff_allowed": 1 if r.get("handoff_allowed") else 0}

    # ---- integrity (report only; never repairs) ------------------------------------------------------------------------
    def verify_run(self, run_id: str) -> dict:
        """Child counts against the run's n_* columns, the weight identity and the stored-row proposal hash. Returns the
        findings; never changes a row."""
        run = self.run(run_id)
        if run is None:
            raise StoreError("NOT_FOUND", "no such run")
        cands, targets, items = self.candidates(run_id), self.targets(run_id), self.items(run_id)
        problems = []
        # a run that failed before any symbol was evaluated (DATA_STALE / INPUT_ERROR) records the universe size but has no
        # candidate rows; every evaluated run must have exactly one candidate row per universe symbol
        evaluated = bool(cands) or run["status"] not in ("DATA_STALE", "INPUT_ERROR")
        if evaluated and len(cands) != run["n_universe"]:
            problems.append("CANDIDATE_COUNT")
        if sum(1 for x in cands if x["eligible"]) != run["n_eligible"]:
            problems.append("ELIGIBLE_COUNT")
        if len(targets) != run["n_selected"]:
            problems.append("TARGET_COUNT")
        if targets and run["target_cash_weight"] is not None:
            total = sum((Decimal(t["target_weight"]) for t in targets), Decimal(0)) + Decimal(run["target_cash_weight"])
            if total != Decimal(1):
                problems.append("WEIGHT_IDENTITY")
        if proposal_hash(run["status"], run["turnover"], run["current_cash_weight"], run["target_cash_weight"], targets, items) \
                != run["proposal_hash"]:
            problems.append("PROPOSAL_HASH")
        return {"run_id": run_id, "ok": not problems, "problems": problems, "n_candidates": len(cands), "n_targets": len(targets),
                "n_items": len(items)}
