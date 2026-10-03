"""
database/migrations.py — Schema definition + idempotent migration runner.

Only "migration 0" (the initial schema) exists today; this file is the
seam future schema changes go through (e.g. `ALTER TABLE ... ADD COLUMN`
guarded by a check against `PRAGMA user_version`). Every statement here
uses `IF NOT EXISTS` so `run_migrations()` is always safe to call on
startup, on an existing database, or on a brand-new empty file.
"""
import sqlite3

_CREATE_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS research_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                  TEXT NOT NULL,
    created_at              TEXT NOT NULL,
    market_timestamp        TEXT NOT NULL,
    price                   REAL NOT NULL,
    price_source            TEXT NOT NULL,
    price_timestamp         TEXT NOT NULL,
    daily_change_pct        REAL,
    volume                  REAL,
    relative_volume         REAL,
    rsi                     REAL,
    ema_9                   REAL,
    ema_20                  REAL,
    ema_50                  REAL,
    atr                     REAL,
    volatility_pct          REAL,
    momentum_5d_pct         REAL,
    momentum_10d_pct        REAL,
    support                 REAL,
    resistance              REAL,
    attention_score         INTEGER,
    scanner_signal          TEXT,
    evidence_technical      REAL,
    evidence_catalyst       REAL,
    evidence_risk           REAL,
    evidence_market         REAL,
    evidence_sector         REAL,
    bullish_pct             INTEGER NOT NULL,
    neutral_pct             INTEGER NOT NULL,
    bearish_pct             INTEGER NOT NULL,
    research_view           TEXT NOT NULL,
    data_quality_level      TEXT NOT NULL,
    catalysts_json          TEXT NOT NULL DEFAULT '[]',
    risk_flags_json         TEXT NOT NULL DEFAULT '[]',
    sector_name             TEXT,
    sector_etf              TEXT,
    sector_pct_change       REAL,
    market_pct_change       REAL,
    setup_entry_low         REAL,
    setup_entry_high        REAL,
    setup_invalidation      REAL,
    setup_target_1          REAL,
    setup_target_2          REAL,
    setup_risk_reward_ratio REAL,
    setup_time_horizon      TEXT,
    narrative_json          TEXT,
    engine_version          TEXT NOT NULL,
    evidence_version        TEXT NOT NULL,
    prompt_version          TEXT,
    llm_provider            TEXT,
    llm_model               TEXT,
    research_schema_version TEXT NOT NULL,
    fingerprint             TEXT NOT NULL UNIQUE,
    events_json             TEXT,
    event_risk_level        TEXT,
    event_data_quality      TEXT,
    event_schema_version    TEXT
);
"""

# Stage 2.6 additive columns -- for a database file that already existed
# before this stage (CREATE TABLE IF NOT EXISTS above is a no-op against
# it). Each is NULLABLE with no default, so ALTER TABLE ADD COLUMN is a
# metadata-only operation that never rewrites or touches existing rows;
# a pre-Stage-2.6 row simply reads back NULL for all four forever, with no
# backfill anywhere in this codebase.
_STAGE_2_6_SNAPSHOT_COLUMNS = [
    ("events_json", "TEXT"),
    ("event_risk_level", "TEXT"),
    ("event_data_quality", "TEXT"),
    ("event_schema_version", "TEXT"),
]


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

_CREATE_OUTCOMES_TABLE = """
CREATE TABLE IF NOT EXISTS research_outcomes (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id                 INTEGER NOT NULL REFERENCES research_snapshots(id) ON DELETE CASCADE,
    horizon_trading_days        INTEGER NOT NULL CHECK (horizon_trading_days IN (1, 3, 5)),
    status                      TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'COMPLETED', 'ERROR')),
    price_at_horizon           REAL,
    return_pct                 REAL,
    highest_price               REAL,
    lowest_price                 REAL,
    max_favorable_excursion_pct  REAL,
    max_adverse_excursion_pct    REAL,
    did_hit_support             INTEGER,
    did_break_support            INTEGER,
    did_hit_resistance           INTEGER,
    did_break_resistance         INTEGER,
    did_hit_invalidation         INTEGER,
    did_hit_target_1             INTEGER,
    did_hit_target_2             INTEGER,
    setup_entry_triggered        INTEGER,
    invalidation_after_entry     INTEGER,
    target_1_after_entry         INTEGER,
    target_2_after_entry         INTEGER,
    sequencing_ambiguous         INTEGER NOT NULL DEFAULT 0,
    outcome_schema_version       TEXT NOT NULL,
    error_message                TEXT,
    updated_at                   TEXT NOT NULL,
    UNIQUE (snapshot_id, horizon_trading_days)
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_outcomes_status ON research_outcomes(status);",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_symbol ON research_snapshots(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_research_view ON research_snapshots(research_view);",
]


def run_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent: safe to call every time the app starts, on any DB state."""
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(_CREATE_SNAPSHOTS_TABLE)
    conn.execute(_CREATE_OUTCOMES_TABLE)
    for stmt in _CREATE_INDEXES:
        conn.execute(stmt)
    for column, ddl_type in _STAGE_2_6_SNAPSHOT_COLUMNS:
        _add_column_if_missing(conn, "research_snapshots", column, ddl_type)
    conn.commit()
