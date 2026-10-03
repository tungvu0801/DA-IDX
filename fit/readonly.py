"""
fit/readonly.py — READ-ONLY access to the saved strategy, backtest and forward-journal tables for Strategy Fit.

The Stage 3.2 / 3.3 stores run migrations (and Stage 3.2 marks interrupted runs) when they are first opened. Strategy
Fit must write nothing, so it uses subclasses whose constructor does neither and whose every connection is opened
`mode=ro` — a write attempt would fail inside SQLite instead of silently changing evidence. Tables that do not exist
yet (a database where no backtest or journal was ever run) are reported as "no stored evidence", never created.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from backtest.store import BacktestStore
from forward.store import ForwardStore


def db_path() -> Path:
    """The app's database file (the same file every other stage uses)."""
    from database.database import get_db
    return Path(get_db().db_path)


class ReadOnlyBacktestStore(BacktestStore):
    """Stage 3.2's store, reads only: no migration, no run-status update, every connection read-only."""

    def __init__(self, path: Path):                       # noqa: D401 - deliberately skips BacktestStore.__init__
        self.db_path = Path(path)

    def _connect(self, readonly: bool = True):
        return BacktestStore._connect(self, readonly=True)


class ReadOnlyForwardStore(ForwardStore):
    """Stage 3.3's store, reads only: no migration, every connection read-only."""

    def __init__(self, path: Path):                       # noqa: D401 - deliberately skips ForwardStore.__init__
        self.db_path = Path(path)

    def _connect(self, readonly: bool = True):
        return ForwardStore._connect(self, readonly=True)


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000;")
    try:
        yield conn
    finally:
        conn.close()


def _name(spec_json: str, fallback: str) -> str:
    try:
        return json.loads(spec_json).get("name") or fallback
    except (ValueError, AttributeError):            # a corrupted row: eligibility reports it; the list still shows it
        return fallback


def saved_versions(path: Path, include_old: bool = False) -> List[dict]:
    """Every NON-ARCHIVED strategy's current (latest) version — and its older versions when asked. Identity only: each
    version is re-verified by the Stage 3.3 eligibility checks before anything is evaluated."""
    try:
        with connect(path) as conn:
            rows = conn.execute(
                "SELECT d.strategy_id, d.name AS first_name, v.version_id, v.version_number, v.spec_json, v.readiness "
                "FROM strategy_definitions d JOIN strategy_versions v ON v.strategy_id = d.strategy_id "
                "WHERE d.archived_at IS NULL ORDER BY d.strategy_id, v.version_number").fetchall()
    except sqlite3.OperationalError:                  # no strategy tables yet: nothing is saved
        return []
    by: dict = {}
    for r in rows:
        by.setdefault(r["strategy_id"], []).append(r)
    out = []
    for sid, vs in by.items():
        current = vs[-1]
        cur_name = _name(current["spec_json"], current["first_name"])
        for r in (vs if include_old else [current]):
            out.append({"strategy_id": sid, "strategy_version_id": r["version_id"], "version_number": r["version_number"],
                        "is_current": r["version_number"] == current["version_number"],
                        "current_version": current["version_number"], "strategy_name": cur_name,
                        "version_name": _name(r["spec_json"], cur_name), "readiness": r["readiness"]})
    return out


def strategy_symbols(path: Path) -> List[str]:
    """Symbols in the universes of the current versions of non-archived strategies (for the symbol picker)."""
    syms = set()
    try:
        with connect(path) as conn:
            for r in conn.execute(
                    "SELECT v.spec_json FROM strategy_versions v JOIN strategy_definitions d ON d.strategy_id = v.strategy_id "
                    "WHERE d.archived_at IS NULL AND v.version_number = (SELECT MAX(version_number) FROM strategy_versions "
                    "x WHERE x.strategy_id = v.strategy_id)"):
                try:
                    syms.update(json.loads(r["spec_json"])["universe"]["symbols"])
                except (ValueError, KeyError, TypeError):
                    continue
    except sqlite3.OperationalError:
        return []
    return sorted(s for s in syms if isinstance(s, str))


def table_exists(path: Path, name: str) -> Optional[bool]:
    try:
        with connect(path) as conn:
            return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None
    except sqlite3.Error:
        return None
