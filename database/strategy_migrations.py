"""
database/strategy_migrations.py — Stage 3 schema (additive). Idempotent; safe on any database state.

Only NEW tables, indexes and triggers are created. The Stage 1–2.8 tables (research_snapshots, research_outcomes)
and database/migrations.py are not touched, and PRAGMA user_version is left alone.

Immutability is enforced by the database itself, not only by the API:
  * strategy_versions rows can never be UPDATEd or DELETEd (spec_json / spec_hash / version_number are permanent);
  * strategy_definitions rows can never be DELETEd, and only archived_at may change (archiving is metadata).
"""
import sqlite3

STRATEGY_SCHEMA_VERSION = 1

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS strategy_definitions (
        strategy_id   TEXT PRIMARY KEY CHECK (length(strategy_id) = 32),
        name          TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        archived_at   TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_versions (
        version_id                    TEXT PRIMARY KEY CHECK (length(version_id) = 32),
        strategy_id                   TEXT NOT NULL REFERENCES strategy_definitions(strategy_id) ON DELETE RESTRICT,
        version_number                INTEGER NOT NULL CHECK (version_number >= 1),
        schema_version                INTEGER NOT NULL,
        feature_registry_version      INTEGER NOT NULL,
        feature_registry_fingerprint  TEXT NOT NULL CHECK (length(feature_registry_fingerprint) = 64),
        spec_json                     TEXT NOT NULL,
        spec_hash                     TEXT NOT NULL CHECK (length(spec_hash) = 64),
        rules_hash                    TEXT NOT NULL CHECK (length(rules_hash) = 64),
        readiness                     TEXT NOT NULL CHECK (readiness IN ('BACKTEST_READY', 'FORWARD_TEST_ONLY', 'UNSUPPORTED')),
        created_at                    TEXT NOT NULL,
        notes                         TEXT,
        UNIQUE (strategy_id, version_number)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_strategy_versions_strategy ON strategy_versions(strategy_id, version_number);",
    "CREATE INDEX IF NOT EXISTS idx_strategy_versions_hash ON strategy_versions(spec_hash);",
    """
    CREATE TRIGGER IF NOT EXISTS strategy_versions_immutable_update BEFORE UPDATE ON strategy_versions
    BEGIN SELECT RAISE(ABORT, 'strategy versions are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS strategy_versions_immutable_delete BEFORE DELETE ON strategy_versions
    BEGIN SELECT RAISE(ABORT, 'strategy versions are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS strategy_definitions_no_delete BEFORE DELETE ON strategy_definitions
    BEGIN SELECT RAISE(ABORT, 'strategies are archived, never deleted'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS strategy_definitions_fixed_fields BEFORE UPDATE OF strategy_id, name, created_at
    ON strategy_definitions BEGIN SELECT RAISE(ABORT, 'only archived_at may change'); END;
    """,
]


def run_strategy_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON;")
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
