"""
strategy/store.py — immutable, versioned strategy storage in the Stage 3 SQLite tables (same database file).

Only validated specs are stored, as canonical JSON with their SHA-256 hashes. There is no method that changes a saved
version: editing creates version n+1 (optionally only if the caller's base is still the latest), and archiving only
sets strategy_definitions.archived_at. On every read the stored JSON is re-hashed; a mismatch is reported as
INTEGRITY_ERROR instead of being trusted.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from database.strategy_migrations import run_strategy_migrations
from strategy import spec as S


class StrategyError(Exception):
    def __init__(self, code: str, message: str, detail=None):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StrategyStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        with self._connect() as conn:
            run_strategy_migrations(conn)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            conn.close()

    # ---- writes (append-only) -----------------------------------------------------------------------------------
    @staticmethod
    def _prepare(spec: dict) -> dict:
        rep = S.report(spec)
        if not rep["valid"]:
            raise StrategyError("INVALID_SPEC", "The strategy is not valid.", rep["errors"])
        return rep

    def _insert_version(self, conn, strategy_id: str, number: int, rep: dict, notes: Optional[str]) -> None:
        spec = rep["spec"]
        conn.execute(
            "INSERT INTO strategy_versions (version_id, strategy_id, version_number, schema_version, "
            "feature_registry_version, feature_registry_fingerprint, spec_json, spec_hash, rules_hash, readiness, "
            "created_at, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, strategy_id, number, spec["schema_version"], spec["feature_registry_version"],
             spec["feature_registry_fingerprint"], rep["canonical_json"], rep["spec_hash"], rep["rules_hash"],
             rep["readiness"]["status"], _now(), (notes or "").strip()[:500] or None))

    def create(self, spec: dict, notes: Optional[str] = None) -> dict:
        rep = self._prepare(spec)
        sid = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("INSERT INTO strategy_definitions (strategy_id, name, created_at) VALUES (?,?,?)",
                         (sid, rep["spec"]["name"], _now()))
            self._insert_version(conn, sid, 1, rep, notes)
            conn.commit()
        return self.get(sid)

    def add_version(self, strategy_id: str, spec: dict, notes: Optional[str] = None,
                    based_on_version: Optional[int] = None) -> dict:
        rep = self._prepare(spec)
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM strategy_definitions WHERE strategy_id = ?", (strategy_id,)).fetchone() is None:
                raise StrategyError("NOT_FOUND", "Strategy not found.")
            last = conn.execute("SELECT version_number, spec_hash FROM strategy_versions WHERE strategy_id = ? "
                                "ORDER BY version_number DESC LIMIT 1", (strategy_id,)).fetchone()
            if based_on_version is not None and based_on_version != last["version_number"]:
                raise StrategyError("VERSION_CONFLICT", f"v{last['version_number']} was saved after v{based_on_version}; "
                                    "reload before creating a new version.")
            if last["spec_hash"] == rep["spec_hash"]:
                raise StrategyError("NO_CHANGE", f"Nothing changed since v{last['version_number']}.")
            try:
                self._insert_version(conn, strategy_id, last["version_number"] + 1, rep, notes)
                conn.commit()
            except sqlite3.IntegrityError as exc:              # two saves raced for the same number
                raise StrategyError("VERSION_CONFLICT", "Another version was saved at the same time; reload.") from exc
        return self.get(strategy_id)

    def set_archived(self, strategy_id: str, archived: bool) -> dict:
        with self._connect() as conn:
            cur = conn.execute("UPDATE strategy_definitions SET archived_at = ? WHERE strategy_id = ?",
                               (_now() if archived else None, strategy_id))
            conn.commit()
            if cur.rowcount == 0:
                raise StrategyError("NOT_FOUND", "Strategy not found.")
        return self.get(strategy_id)

    # ---- reads -----------------------------------------------------------------------------------------------------
    @staticmethod
    def _version(row: sqlite3.Row) -> dict:
        spec = json.loads(row["spec_json"])
        ok = S.canonical_json(spec) == row["spec_json"] and S.spec_hash(spec) == row["spec_hash"]
        rd = S.readiness(spec) if ok else None
        return {"version_id": row["version_id"], "version_number": row["version_number"],
                "schema_version": row["schema_version"], "feature_registry_version": row["feature_registry_version"],
                "feature_registry_fingerprint": row["feature_registry_fingerprint"], "spec": spec,
                "spec_hash": row["spec_hash"], "rules_hash": row["rules_hash"], "readiness": row["readiness"],
                "readiness_detail": rd, "created_at": row["created_at"], "notes": row["notes"],
                "integrity": "OK" if ok else "INTEGRITY_ERROR", "summary": S.summary(spec) if ok else None,
                "entry_count": len(S._conditions_of(spec["entry"])) if ok else None,
                "exit_count": len(S.exit_rule_texts(spec["exit"])) if ok else None}

    def get(self, strategy_id: str) -> dict:
        with self._connect() as conn:
            d = conn.execute("SELECT * FROM strategy_definitions WHERE strategy_id = ?", (strategy_id,)).fetchone()
            if d is None:
                raise StrategyError("NOT_FOUND", "Strategy not found.")
            rows = conn.execute("SELECT * FROM strategy_versions WHERE strategy_id = ? ORDER BY version_number",
                                (strategy_id,)).fetchall()
        versions = [self._version(r) for r in rows]
        latest = versions[-1]
        return {"strategy_id": d["strategy_id"], "name": latest["spec"]["name"], "first_name": d["name"],
                "created_at": d["created_at"], "archived_at": d["archived_at"], "current_version": latest["version_number"],
                "updated_at": latest["created_at"], "versions": versions}

    def version(self, strategy_id: str, number: int) -> dict:
        for v in self.get(strategy_id)["versions"]:
            if v["version_number"] == number:
                return v
        raise StrategyError("NOT_FOUND", f"v{number} not found.")

    def list(self, include_archived: bool = False) -> List[dict]:
        with self._connect() as conn:
            ids = [r["strategy_id"] for r in conn.execute(
                "SELECT d.strategy_id FROM strategy_definitions d JOIN strategy_versions v ON v.strategy_id = d.strategy_id "
                + ("" if include_archived else "WHERE d.archived_at IS NULL ")
                + "GROUP BY d.strategy_id ORDER BY MAX(v.created_at) DESC, d.strategy_id")]
        out = []
        for sid in ids:
            s = self.get(sid)
            cur = s["versions"][-1]
            out.append({k: s[k] for k in ("strategy_id", "name", "created_at", "archived_at", "current_version", "updated_at")}
                       | {"readiness": cur["readiness"], "universe": cur["spec"]["universe"]["symbols"],
                          "entry_count": cur["entry_count"], "exit_count": cur["exit_count"], "spec_hash": cur["spec_hash"],
                          "integrity": cur["integrity"], "version_count": len(s["versions"])})
        return out


_store: Optional[StrategyStore] = None
_lock = threading.Lock()


def get_store() -> StrategyStore:
    """Same database file as the rest of the app (database.database.get_db().db_path); tables created on first use."""
    global _store
    from database.database import get_db
    path = get_db().db_path
    with _lock:
        if _store is None or _store.db_path != Path(path):
            _store = StrategyStore(path)
    return _store
