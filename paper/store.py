"""
paper/store.py — Stage 4.5 SQLite access for the local paper portfolio (the five paper tables only).

Writes run the additive paper migration first; reads never create anything (a database without the paper tables simply
has no paper account yet). Every fill is written inside ONE transaction together with its lot / lot closures and the
order's FILLED status (paper/execution.py), so a fill can never be half-recorded.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from database.paper_migrations import run_paper_migrations
from fit import readonly as RO


class PaperStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else RO.db_path()

    @contextmanager
    def connect(self, readonly: bool = False):
        if readonly:
            conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=15.0)
        else:
            conn = sqlite3.connect(str(self.path), timeout=15.0)
            conn.execute("PRAGMA journal_mode=WAL;")
            run_paper_migrations(conn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            conn.close()

    def read(self, sql: str, args=()) -> List[dict]:
        if not self.path.exists():
            return []
        try:
            with self.connect(readonly=True) as conn:
                return [dict(r) for r in conn.execute(sql, args)]
        except sqlite3.OperationalError:              # no paper account was ever created here
            return []

    # ---- reads (outside a transaction) -------------------------------------------------------------------------------
    def account(self) -> Optional[dict]:
        rows = self.read("SELECT * FROM paper_accounts WHERE status = 'ACTIVE' ORDER BY created_at, rowid LIMIT 1")
        return rows[0] if rows else None

    def orders(self, account_id: str, status: Optional[str] = None, limit: int = 500) -> List[dict]:
        q, args = "SELECT * FROM paper_orders WHERE account_id = ?", [account_id]
        if status:
            q += " AND status = ?"
            args.append(status)
        return self.read(q + " ORDER BY submitted_at DESC, rowid DESC LIMIT ?", (*args, int(limit)))

    def order(self, order_id: str) -> Optional[dict]:
        rows = self.read("SELECT * FROM paper_orders WHERE order_id = ?", (order_id,))
        return rows[0] if rows else None

    def fills(self, account_id: str, limit: Optional[int] = None) -> List[dict]:
        q = "SELECT rowid AS seq, * FROM paper_fills WHERE account_id = ? ORDER BY fill_session, rowid"
        rows = self.read(q, (account_id,))
        return rows if limit is None else rows[-int(limit):]

    def ledger(self, account_id: str, conn: Optional[sqlite3.Connection] = None):
        """(fills, lots, closures) — inside `conn` when given (the fill transaction), else read-only."""
        qs = ("SELECT rowid AS seq, * FROM paper_fills WHERE account_id = ? ORDER BY fill_session, rowid",
              "SELECT rowid AS seq, * FROM paper_lots WHERE account_id = ? ORDER BY entry_session, rowid",
              "SELECT rowid AS seq, * FROM paper_lot_closures WHERE account_id = ? ORDER BY rowid")
        if conn is not None:
            return tuple([dict(r) for r in conn.execute(q, (account_id,))] for q in qs)
        return tuple(self.read(q, (account_id,)) for q in qs)

    # ---- writes ---------------------------------------------------------------------------------------------------
    @staticmethod
    def insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
        conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
