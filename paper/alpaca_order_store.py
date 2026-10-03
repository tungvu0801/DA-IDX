"""
paper/alpaca_order_store.py — Stage 4.6B SQLite access for manual Alpaca PAPER orders (the three alpaca_paper_order_*
tables only; the Stage 4.5 paper_* tables are never touched).

Reads never create tables (no linked account yet = no tables). Every write runs the additive, idempotent migration first
and happens inside ONE `BEGIN IMMEDIATE` transaction together with its audit event. State changes are compare-and-set
(`UPDATE … WHERE state IN (expected)`) and must be an allowed transition of the state machine; the database triggers
additionally keep identity / payload / preview columns immutable, the Alpaca order id set-once and terminal rows final.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from database.alpaca_order_migrations import run_alpaca_order_migrations
from fit import readonly as RO
from paper import alpaca_order_rules as RU

_TABLES = ("alpaca_paper_order_settings", "alpaca_paper_order_intents", "alpaca_paper_order_events")
_INTENT_FIELDS = {"confirmed_at", "submit_attempts", "last_submit_at", "lookups_not_found", "first_not_found_at",
                  "last_lookup_at", "alpaca_order_id", "broker_status", "broker_status_at", "filled_qty", "filled_avg_price",
                  "error_code", "http_status", "broker_error_code", "error_text"}


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class TransitionError(RuntimeError):
    pass


class OrderStore:
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
        return set(_TABLES) <= names

    def read(self, sql: str, args=()) -> List[dict]:
        if not self.exists():
            return []
        conn = self._ro()
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    def settings(self) -> Optional[dict]:
        rows = self.read("SELECT * FROM alpaca_paper_order_settings WHERE id = 1")
        return rows[0] if rows else None

    def intent(self, intent_id: int) -> Optional[dict]:
        rows = self.read("SELECT * FROM alpaca_paper_order_intents WHERE intent_id = ?", (int(intent_id),))
        return rows[0] if rows else None

    def intents(self, limit: int = 100) -> List[dict]:
        return self.read("SELECT * FROM alpaca_paper_order_intents WHERE state NOT IN ('SUPERSEDED', 'EXPIRED') OR "
                         "intent_id = (SELECT max(intent_id) FROM alpaca_paper_order_intents) "
                         "ORDER BY intent_id DESC LIMIT ?", (int(limit),))

    def in_states(self, states: Iterable[str], order: str = "intent_id") -> List[dict]:
        states = sorted(states)
        q = ",".join("?" * len(states))
        return self.read(f"SELECT * FROM alpaca_paper_order_intents WHERE state IN ({q}) ORDER BY {order}", tuple(states))

    def events(self, intent_id: int, limit: int = 50) -> List[dict]:
        return self.read("SELECT at, kind, from_state, to_state, http_status, broker_status, code, latency_ms "
                         "FROM alpaca_paper_order_events WHERE intent_id = ? ORDER BY event_id DESC LIMIT ?",
                         (int(intent_id), int(limit)))

    def submitted_since(self, iso_from: str) -> List[str]:
        return [r["confirmed_at"] for r in self.read(
            "SELECT confirmed_at FROM alpaca_paper_order_intents WHERE submit_attempts > 0 AND confirmed_at >= ?", (iso_from,))]

    # ---- writes ---------------------------------------------------------------------------------------------------------
    @contextmanager
    def write(self):
        conn = sqlite3.connect(str(self.path), timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=15000;")
            conn.execute("PRAGMA foreign_keys=ON;")
            run_alpaca_order_migrations(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    @staticmethod
    def event(conn, intent_id, kind: str, now: datetime, from_state=None, to_state=None, http_status=None,
              broker_status=None, code=None, latency_ms=None) -> None:
        conn.execute("INSERT INTO alpaca_paper_order_events (intent_id, at, kind, from_state, to_state, http_status, "
                     "broker_status, code, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (intent_id, _iso(now), kind, from_state, to_state, http_status, broker_status, code, latency_ms))

    @staticmethod
    def get(conn, intent_id: int) -> Optional[dict]:
        r = conn.execute("SELECT * FROM alpaca_paper_order_intents WHERE intent_id = ?", (int(intent_id),)).fetchone()
        return dict(r) if r else None

    def save_settings(self, conn, now: datetime, **fields) -> None:
        cur = conn.execute("SELECT * FROM alpaca_paper_order_settings WHERE id = 1").fetchone()
        if cur is None:
            conn.execute("INSERT INTO alpaca_paper_order_settings (id, enabled, updated_at) VALUES (1, 0, ?)", (_iso(now),))
        allowed = {"enabled", "bound_account_fp", "bound_account_masked", "bound_at", "last_no_shorting", "last_config_check_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown settings fields {bad}")
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE alpaca_paper_order_settings SET {sets}, updated_at = ? WHERE id = 1",
                         (*fields.values(), _iso(now)))

    def supersede_previews(self, conn, now: datetime, except_id: Optional[int] = None) -> List[int]:
        ids = [r[0] for r in conn.execute("SELECT intent_id FROM alpaca_paper_order_intents WHERE state = 'PREVIEWED'"
                                          + ("" if except_id is None else " AND intent_id != ?"),
                                          () if except_id is None else (except_id,))]
        for i in ids:
            self.transition(conn, i, {RU.PREVIEWED}, RU.SUPERSEDED, now)
        return ids

    def insert_intent(self, conn, row: dict, now: datetime) -> int:
        self.supersede_previews(conn, now)
        cols = ["client_order_id", "symbol", "side", "qty", "order_type", "time_in_force", "payload_json", "payload_sha256",
                "reference_price", "reference_session", "reference_source", "estimated_notional", "preview_json",
                "preview_hash", "account_fp", "account_masked", "previewed_at", "expires_at"]
        cur = conn.execute(f"INSERT INTO alpaca_paper_order_intents ({', '.join(cols)}, state, updated_at) "
                           f"VALUES ({', '.join('?' * len(cols))}, 'PREVIEWED', ?)", (*[row[c] for c in cols], _iso(now)))
        iid = cur.lastrowid
        self.event(conn, iid, "PREVIEWED", now, to_state=RU.PREVIEWED)
        return iid

    def transition(self, conn, intent_id: int, expected: set, new_state: str, now: datetime, kind: Optional[str] = None,
                   **fields) -> bool:
        """Compare-and-set: moves the intent from one of `expected` to `new_state` (an allowed transition) and records an
        event. Returns False (and changes nothing) when the intent is no longer in an expected state."""
        cur = self.get(conn, intent_id)
        if cur is None or cur["state"] not in expected:
            return False
        if not RU.transition_ok(cur["state"], new_state):
            raise TransitionError(f"{cur['state']} -> {new_state} is not an allowed transition")
        bad = set(fields) - _INTENT_FIELDS
        if bad:
            raise ValueError(f"unknown intent fields {bad}")
        sets = ", ".join([f"{k} = ?" for k in fields] + ["state = ?", "updated_at = ?"])
        states = sorted(expected)
        n = conn.execute(f"UPDATE alpaca_paper_order_intents SET {sets} WHERE intent_id = ? AND state IN "
                         f"({','.join('?' * len(states))})", (*fields.values(), new_state, _iso(now), intent_id, *states)).rowcount
        if n != 1:
            return False
        if kind is not False:
            self.event(conn, intent_id, kind or ("STATUS_CHANGE" if new_state != cur["state"] else "LOOKUP"), now,
                       from_state=cur["state"], to_state=new_state, http_status=fields.get("http_status"),
                       broker_status=fields.get("broker_status"), code=fields.get("error_code"))
        return True
