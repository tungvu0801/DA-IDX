"""
database/database.py — ResearchDatabase: the only place SQL gets written
for Stage 2.5. Every caller elsewhere in the app only ever sees plain
Python methods and database/models.py dataclasses — never a raw
sqlite3.Row, a connection, or a query string. Migrating to PostgreSQL
later means rewriting the bodies of the methods below (placeholder style
`?` -> `%s`, `AUTOINCREMENT` -> `IDENTITY`); no caller changes.

Concurrency: FastAPI runs sync route handlers in a thread pool, so a
connection must never be shared across threads. Each method opens its own
short-lived connection (opening a local SQLite file is sub-millisecond) —
simpler and safer than a shared connection + lock for a local single-user
app. WAL mode lets reads and the writer proceed without blocking each other.
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from database.migrations import run_migrations
from database.models import ResearchOutcome, ResearchSnapshot

_SNAPSHOT_COLUMNS = [
    "symbol", "created_at", "market_timestamp", "price", "price_source", "price_timestamp",
    "daily_change_pct", "volume", "relative_volume",
    "rsi", "ema_9", "ema_20", "ema_50", "atr", "volatility_pct", "momentum_5d_pct", "momentum_10d_pct",
    "support", "resistance", "attention_score", "scanner_signal",
    "evidence_technical", "evidence_catalyst", "evidence_risk", "evidence_market", "evidence_sector",
    "bullish_pct", "neutral_pct", "bearish_pct", "research_view", "data_quality_level",
    "catalysts_json", "risk_flags_json",
    "sector_name", "sector_etf", "sector_pct_change", "market_pct_change",
    "setup_entry_low", "setup_entry_high", "setup_invalidation", "setup_target_1", "setup_target_2",
    "setup_risk_reward_ratio", "setup_time_horizon", "narrative_json",
    "engine_version", "evidence_version", "prompt_version", "llm_provider", "llm_model",
    "research_schema_version", "fingerprint",
    "events_json", "event_risk_level", "event_data_quality", "event_schema_version",
]

_OUTCOME_COLUMNS = [
    "snapshot_id", "horizon_trading_days", "status",
    "price_at_horizon", "return_pct", "highest_price", "lowest_price",
    "max_favorable_excursion_pct", "max_adverse_excursion_pct",
    "did_hit_support", "did_break_support", "did_hit_resistance", "did_break_resistance",
    "did_hit_invalidation", "did_hit_target_1", "did_hit_target_2",
    "setup_entry_triggered", "invalidation_after_entry", "target_1_after_entry", "target_2_after_entry",
    "sequencing_ambiguous", "outcome_schema_version", "error_message", "updated_at",
]

_BOOL_OUTCOME_FIELDS = {
    "did_hit_support", "did_break_support", "did_hit_resistance", "did_break_resistance",
    "did_hit_invalidation", "did_hit_target_1", "did_hit_target_2",
    "setup_entry_triggered", "invalidation_after_entry", "target_1_after_entry", "target_2_after_entry",
    "sequencing_ambiguous",
}


def _dt_to_str(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _str_to_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _bool_to_int(value: Optional[bool]) -> Optional[int]:
    return None if value is None else int(bool(value))


def _int_to_bool(value) -> Optional[bool]:
    return None if value is None else bool(value)


class ResearchDatabase:
    """Repository for research_snapshots / research_outcomes. See module docstring."""

    def __init__(self, db_path: Path = config.RESEARCH_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        with self._connect() as conn:
            run_migrations(conn)

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

    # ----------------------------------------------------------------
    # Snapshots
    # ----------------------------------------------------------------
    def save_snapshot(self, snapshot: ResearchSnapshot) -> Tuple[int, bool]:
        """
        Insert `snapshot` unless its fingerprint already exists. Returns
        (id, created) — created=False means an identical snapshot was
        already saved and that existing row's id is returned instead.
        """
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM research_snapshots WHERE fingerprint = ?", (snapshot.fingerprint,)
            ).fetchone()
            if existing is not None:
                return existing["id"], False

            row = self._snapshot_to_row(snapshot)
            placeholders = ", ".join("?" for _ in _SNAPSHOT_COLUMNS)
            columns_sql = ", ".join(_SNAPSHOT_COLUMNS)
            try:
                cursor = conn.execute(
                    f"INSERT INTO research_snapshots ({columns_sql}) VALUES ({placeholders})",
                    [row[c] for c in _SNAPSHOT_COLUMNS],
                )
                conn.commit()
                return cursor.lastrowid, True
            except sqlite3.IntegrityError:
                # Race: another call inserted the same fingerprint between our SELECT and INSERT.
                existing = conn.execute(
                    "SELECT id FROM research_snapshots WHERE fingerprint = ?", (snapshot.fingerprint,)
                ).fetchone()
                if existing is not None:
                    return existing["id"], False
                raise

    def get_snapshot(self, snapshot_id: int) -> Optional[ResearchSnapshot]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM research_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        return self._row_to_snapshot(row) if row is not None else None

    def list_snapshots(
        self,
        symbol: Optional[str] = None,
        research_view: Optional[str] = None,
        data_quality: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ResearchSnapshot]:
        clauses = []
        params: List[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.strip().upper())
        if research_view:
            clauses.append("research_view = ?")
            params.append(research_view)
        if data_quality:
            clauses.append("data_quality_level = ?")
            params.append(data_quality)
        if date_from:
            clauses.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("created_at <= ?")
            params.append(date_to)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM research_snapshots {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def delete_snapshot(self, snapshot_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM research_snapshots WHERE id = ?", (snapshot_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ----------------------------------------------------------------
    # Outcomes
    # ----------------------------------------------------------------
    def create_pending_outcomes(self, snapshot_id: int, horizons: List[int]) -> None:
        """Insert a PENDING placeholder row per horizon right after a snapshot is created."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for horizon in horizons:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO research_outcomes
                        (snapshot_id, horizon_trading_days, status, outcome_schema_version, updated_at)
                    VALUES (?, ?, 'PENDING', ?, ?)
                    """,
                    (snapshot_id, horizon, config.OUTCOME_SCHEMA_VERSION, now),
                )
            conn.commit()

    def get_outcomes_for_snapshot(self, snapshot_id: int) -> List[ResearchOutcome]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM research_outcomes WHERE snapshot_id = ? ORDER BY horizon_trading_days",
                (snapshot_id,),
            ).fetchall()
        return [self._row_to_outcome(r) for r in rows]

    def get_pending_outcomes_with_snapshots(self, limit: int = 500) -> List[Tuple[ResearchOutcome, ResearchSnapshot]]:
        # Deliberately NOT "SELECT o.*, s.*": both tables have an `id` column,
        # and dict(sqlite3.Row) collapses duplicate column names by last-write-
        # wins, silently corrupting the outcome's id with the snapshot's id.
        # Two simple queries avoid that whole bug class.
        with self._connect() as conn:
            outcome_rows = conn.execute(
                "SELECT * FROM research_outcomes WHERE status = 'PENDING' ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
            outcomes = [self._row_to_outcome(r) for r in outcome_rows]
            snapshot_ids = sorted({o.snapshot_id for o in outcomes})
            snapshots_by_id = self._fetch_snapshots_by_ids(conn, snapshot_ids)
        return [(o, snapshots_by_id[o.snapshot_id]) for o in outcomes if o.snapshot_id in snapshots_by_id]

    def update_outcome(self, outcome: ResearchOutcome) -> None:
        outcome.updated_at = datetime.now(timezone.utc)
        row = self._outcome_to_row(outcome)
        set_clause = ", ".join(f"{c} = ?" for c in _OUTCOME_COLUMNS if c not in ("snapshot_id", "horizon_trading_days"))
        update_cols = [c for c in _OUTCOME_COLUMNS if c not in ("snapshot_id", "horizon_trading_days")]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE research_outcomes SET {set_clause} WHERE snapshot_id = ? AND horizon_trading_days = ?",
                [row[c] for c in update_cols] + [outcome.snapshot_id, outcome.horizon_trading_days],
            )
            conn.commit()

    def get_completed_outcomes_with_snapshots(self) -> List[Tuple[ResearchOutcome, ResearchSnapshot]]:
        with self._connect() as conn:
            outcome_rows = conn.execute("SELECT * FROM research_outcomes WHERE status = 'COMPLETED'").fetchall()
            outcomes = [self._row_to_outcome(r) for r in outcome_rows]
            snapshot_ids = sorted({o.snapshot_id for o in outcomes})
            snapshots_by_id = self._fetch_snapshots_by_ids(conn, snapshot_ids)
        return [(o, snapshots_by_id[o.snapshot_id]) for o in outcomes if o.snapshot_id in snapshots_by_id]

    def _fetch_snapshots_by_ids(self, conn: sqlite3.Connection, ids: List[int]) -> Dict[int, ResearchSnapshot]:
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = conn.execute(f"SELECT * FROM research_snapshots WHERE id IN ({placeholders})", ids).fetchall()
        return {r["id"]: self._row_to_snapshot(r) for r in rows}

    def count_snapshots(self) -> Dict[str, int]:
        """
        A snapshot counts as "pending" if any of its horizons still has a
        PENDING outcome row; "completed" means every horizon has resolved
        (COMPLETED or ERROR) — total = pending + completed always.
        """
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM research_snapshots").fetchone()["n"]
            pending = conn.execute(
                "SELECT COUNT(DISTINCT snapshot_id) AS n FROM research_outcomes WHERE status = 'PENDING'"
            ).fetchone()["n"]
        return {"total": total, "pending": pending, "completed": total - pending}

    # ----------------------------------------------------------------
    # Row <-> dataclass conversion
    # ----------------------------------------------------------------
    def _snapshot_to_row(self, s: ResearchSnapshot) -> Dict[str, Any]:
        return {
            "symbol": s.symbol.strip().upper(),
            "created_at": _dt_to_str(s.created_at),
            "market_timestamp": s.market_timestamp,
            "price": s.price,
            "price_source": s.price_source,
            "price_timestamp": _dt_to_str(s.price_timestamp),
            "daily_change_pct": s.daily_change_pct,
            "volume": s.volume,
            "relative_volume": s.relative_volume,
            "rsi": s.rsi,
            "ema_9": s.ema_9,
            "ema_20": s.ema_20,
            "ema_50": s.ema_50,
            "atr": s.atr,
            "volatility_pct": s.volatility_pct,
            "momentum_5d_pct": s.momentum_5d_pct,
            "momentum_10d_pct": s.momentum_10d_pct,
            "support": s.support,
            "resistance": s.resistance,
            "attention_score": s.attention_score,
            "scanner_signal": s.scanner_signal,
            "evidence_technical": s.evidence_technical,
            "evidence_catalyst": s.evidence_catalyst,
            "evidence_risk": s.evidence_risk,
            "evidence_market": s.evidence_market,
            "evidence_sector": s.evidence_sector,
            "bullish_pct": s.bullish_pct,
            "neutral_pct": s.neutral_pct,
            "bearish_pct": s.bearish_pct,
            "research_view": s.research_view,
            "data_quality_level": s.data_quality_level,
            "catalysts_json": s.catalysts_json,
            "risk_flags_json": s.risk_flags_json,
            "sector_name": s.sector_name,
            "sector_etf": s.sector_etf,
            "sector_pct_change": s.sector_pct_change,
            "market_pct_change": s.market_pct_change,
            "setup_entry_low": s.setup_entry_low,
            "setup_entry_high": s.setup_entry_high,
            "setup_invalidation": s.setup_invalidation,
            "setup_target_1": s.setup_target_1,
            "setup_target_2": s.setup_target_2,
            "setup_risk_reward_ratio": s.setup_risk_reward_ratio,
            "setup_time_horizon": s.setup_time_horizon,
            "narrative_json": s.narrative_json,
            "engine_version": s.engine_version,
            "evidence_version": s.evidence_version,
            "prompt_version": s.prompt_version,
            "llm_provider": s.llm_provider,
            "llm_model": s.llm_model,
            "research_schema_version": s.research_schema_version,
            "fingerprint": s.fingerprint,
            "events_json": s.events_json,
            "event_risk_level": s.event_risk_level,
            "event_data_quality": s.event_data_quality,
            "event_schema_version": s.event_schema_version,
        }

    def _row_to_snapshot(self, row: sqlite3.Row) -> ResearchSnapshot:
        d = dict(row)
        return ResearchSnapshot(
            id=d["id"],
            symbol=d["symbol"],
            created_at=_str_to_dt(d["created_at"]),
            market_timestamp=d["market_timestamp"],
            price=d["price"],
            price_source=d["price_source"],
            price_timestamp=_str_to_dt(d["price_timestamp"]),
            daily_change_pct=d["daily_change_pct"],
            volume=d["volume"],
            relative_volume=d["relative_volume"],
            rsi=d["rsi"],
            ema_9=d["ema_9"],
            ema_20=d["ema_20"],
            ema_50=d["ema_50"],
            atr=d["atr"],
            volatility_pct=d["volatility_pct"],
            momentum_5d_pct=d["momentum_5d_pct"],
            momentum_10d_pct=d["momentum_10d_pct"],
            support=d["support"],
            resistance=d["resistance"],
            attention_score=d["attention_score"],
            scanner_signal=d["scanner_signal"],
            evidence_technical=d["evidence_technical"],
            evidence_catalyst=d["evidence_catalyst"],
            evidence_risk=d["evidence_risk"],
            evidence_market=d["evidence_market"],
            evidence_sector=d["evidence_sector"],
            bullish_pct=d["bullish_pct"],
            neutral_pct=d["neutral_pct"],
            bearish_pct=d["bearish_pct"],
            research_view=d["research_view"],
            data_quality_level=d["data_quality_level"],
            catalysts_json=d["catalysts_json"],
            risk_flags_json=d["risk_flags_json"],
            sector_name=d["sector_name"],
            sector_etf=d["sector_etf"],
            sector_pct_change=d["sector_pct_change"],
            market_pct_change=d["market_pct_change"],
            setup_entry_low=d["setup_entry_low"],
            setup_entry_high=d["setup_entry_high"],
            setup_invalidation=d["setup_invalidation"],
            setup_target_1=d["setup_target_1"],
            setup_target_2=d["setup_target_2"],
            setup_risk_reward_ratio=d["setup_risk_reward_ratio"],
            setup_time_horizon=d["setup_time_horizon"],
            narrative_json=d["narrative_json"],
            engine_version=d["engine_version"],
            evidence_version=d["evidence_version"],
            prompt_version=d["prompt_version"],
            llm_provider=d["llm_provider"],
            llm_model=d["llm_model"],
            research_schema_version=d["research_schema_version"],
            fingerprint=d["fingerprint"],
            events_json=d["events_json"],
            event_risk_level=d["event_risk_level"],
            event_data_quality=d["event_data_quality"],
            event_schema_version=d["event_schema_version"],
        )

    def _outcome_to_row(self, o: ResearchOutcome) -> Dict[str, Any]:
        row = {
            "snapshot_id": o.snapshot_id,
            "horizon_trading_days": o.horizon_trading_days,
            "status": o.status,
            "price_at_horizon": o.price_at_horizon,
            "return_pct": o.return_pct,
            "highest_price": o.highest_price,
            "lowest_price": o.lowest_price,
            "max_favorable_excursion_pct": o.max_favorable_excursion_pct,
            "max_adverse_excursion_pct": o.max_adverse_excursion_pct,
            "outcome_schema_version": o.outcome_schema_version,
            "error_message": o.error_message,
            "updated_at": _dt_to_str(o.updated_at),
        }
        for field_name in _BOOL_OUTCOME_FIELDS:
            row[field_name] = _bool_to_int(getattr(o, field_name))
        return row

    def _row_to_outcome(self, row: sqlite3.Row) -> ResearchOutcome:
        d = dict(row)
        kwargs = dict(
            id=d["id"],
            snapshot_id=d["snapshot_id"],
            horizon_trading_days=d["horizon_trading_days"],
            status=d["status"],
            price_at_horizon=d["price_at_horizon"],
            return_pct=d["return_pct"],
            highest_price=d["highest_price"],
            lowest_price=d["lowest_price"],
            max_favorable_excursion_pct=d["max_favorable_excursion_pct"],
            max_adverse_excursion_pct=d["max_adverse_excursion_pct"],
            outcome_schema_version=d["outcome_schema_version"],
            error_message=d["error_message"],
            updated_at=_str_to_dt(d["updated_at"]),
        )
        for field_name in _BOOL_OUTCOME_FIELDS:
            kwargs[field_name] = _int_to_bool(d[field_name])
        return ResearchOutcome(**kwargs)


_db_instance: Optional[ResearchDatabase] = None
_db_lock = threading.Lock()


def get_db() -> ResearchDatabase:
    """Process-wide singleton — migrations only need to run once per process."""
    global _db_instance
    if _db_instance is None:
        with _db_lock:
            if _db_instance is None:
                _db_instance = ResearchDatabase()
    return _db_instance
