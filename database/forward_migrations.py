"""
database/forward_migrations.py — Stage 3.3 FORWARD-TEST SIGNAL JOURNAL schema (additive). Idempotent; safe on any
database state.

Only NEW tables, indexes and triggers are created. research_snapshots / research_outcomes, the Stage 3.1 strategy
tables, the Stage 3.2 backtest and bar-cache tables, database/migrations.py and PRAGMA user_version are not touched
(the Stage 3.2 migration is only re-run, as it is idempotent, because journals reference strategy versions and reuse
the Stage 3.2 bar cache).

What the database itself guarantees:
  * forward_test_journals — never DELETEd; identity / configuration columns never change; status only moves
    ACTIVE -> CONTINUITY_BLOCKED -> ARCHIVED (or ACTIVE -> ARCHIVED) and an ARCHIVED journal never changes again;
    at most ONE non-archived journal per strategy version.
  * forward_test_sessions — one row per (journal, market session): CAPTURED (recorded forward) or MISSED (an eligible
    session nobody captured — never reconstructed later). Never UPDATEd or DELETEd. NO BACKFILL: a session row can
    only be inserted for a date after every session already captured, and never before the journal's forward start
    (the day after the journal was created), and a capture must be timestamped after that session's close.
  * forward_test_observations — one immutable row per (journal, captured session, symbol): the feature snapshot,
    evaluation trace, decision (ENTER / EXIT / HOLD / SKIP) and shadow state before / after. Only legal state
    transitions are accepted; rows can only be added to the latest captured session.
  * forward_test_reference_fills — append-only REFERENCE fills (the next session's actual open; not orders, not
    broker fills). A reference fill is always strictly after its signal session and points at the observation that
    produced the signal.
  * nothing can be inserted into an ARCHIVED journal.
"""
import sqlite3

from database.backtest_migrations import run_backtest_migrations

FORWARD_SCHEMA_VERSION = 1

STATES = ("FLAT", "ENTRY_PENDING", "OPEN", "EXIT_PENDING", "CONTINUITY_BLOCKED")
DECISIONS = ("ENTER", "EXIT", "HOLD", "SKIP")
# every legal shadow-state transition within one captured session (before > after)
TRANSITIONS = (
    "FLAT>FLAT", "FLAT>ENTRY_PENDING",
    "ENTRY_PENDING>OPEN", "ENTRY_PENDING>EXIT_PENDING", "ENTRY_PENDING>FLAT", "ENTRY_PENDING>CONTINUITY_BLOCKED",
    "OPEN>OPEN", "OPEN>EXIT_PENDING", "OPEN>CONTINUITY_BLOCKED",
    "EXIT_PENDING>EXIT_PENDING", "EXIT_PENDING>FLAT", "EXIT_PENDING>ENTRY_PENDING", "EXIT_PENDING>CONTINUITY_BLOCKED",
    "CONTINUITY_BLOCKED>CONTINUITY_BLOCKED",
)
EVIDENCE_TABLES = ("forward_test_sessions", "forward_test_observations", "forward_test_reference_fills")


def _q(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS forward_test_journals (
        journal_id                    TEXT PRIMARY KEY CHECK (length(journal_id) = 32),
        strategy_id                   TEXT NOT NULL REFERENCES strategy_definitions(strategy_id) ON DELETE RESTRICT,
        strategy_version_id           TEXT NOT NULL REFERENCES strategy_versions(version_id) ON DELETE RESTRICT,
        version_number                INTEGER NOT NULL CHECK (version_number >= 1),
        spec_hash                     TEXT NOT NULL CHECK (length(spec_hash) = 64),
        rules_hash                    TEXT NOT NULL CHECK (length(rules_hash) = 64),
        feature_registry_version      INTEGER NOT NULL,
        feature_registry_fingerprint  TEXT NOT NULL CHECK (length(feature_registry_fingerprint) = 64),
        readiness                     TEXT NOT NULL CHECK (readiness IN ('BACKTEST_READY', 'FORWARD_TEST_ONLY')),
        engine_version                TEXT NOT NULL,
        config_json                   TEXT NOT NULL,
        config_hash                   TEXT NOT NULL CHECK (length(config_hash) = 64),
        created_at                    TEXT NOT NULL,
        created_session_date          TEXT NOT NULL,
        forward_start_date            TEXT NOT NULL,
        status                        TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONTINUITY_BLOCKED', 'ARCHIVED')),
        archived_at                   TEXT,
        CHECK (forward_start_date > created_session_date),
        CHECK ((status = 'ARCHIVED') = (archived_at IS NOT NULL))
    );
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_forward_one_primary_journal ON forward_test_journals(strategy_version_id) "
    "WHERE status != 'ARCHIVED';",
    "CREATE INDEX IF NOT EXISTS idx_forward_journals_version ON forward_test_journals(strategy_id, version_number, created_at);",
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_journals_no_delete BEFORE DELETE ON forward_test_journals
    BEGIN SELECT RAISE(ABORT, 'forward journals are evidence and are never deleted (archive instead)'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_journals_archived_final BEFORE UPDATE ON forward_test_journals
    WHEN OLD.status = 'ARCHIVED'
    BEGIN SELECT RAISE(ABORT, 'an archived forward journal never changes'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_journals_fixed_fields BEFORE UPDATE OF journal_id, strategy_id,
        strategy_version_id, version_number, spec_hash, rules_hash, feature_registry_version,
        feature_registry_fingerprint, readiness, engine_version, config_json, config_hash, created_at,
        created_session_date, forward_start_date ON forward_test_journals
    BEGIN SELECT RAISE(ABORT, 'forward journal identity and configuration are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_journals_status_flow BEFORE UPDATE OF status ON forward_test_journals
    WHEN NOT (OLD.status = NEW.status
              OR (OLD.status = 'ACTIVE' AND NEW.status IN ('CONTINUITY_BLOCKED', 'ARCHIVED'))
              OR (OLD.status = 'CONTINUITY_BLOCKED' AND NEW.status = 'ARCHIVED'))
    BEGIN SELECT RAISE(ABORT, 'invalid forward journal status transition'); END;
    """,
    """
    CREATE TABLE IF NOT EXISTS forward_test_sessions (
        journal_id                   TEXT NOT NULL REFERENCES forward_test_journals(journal_id) ON DELETE RESTRICT,
        session_date                 TEXT NOT NULL,
        kind                         TEXT NOT NULL CHECK (kind IN ('CAPTURED', 'MISSED')),
        recorded_at                  TEXT NOT NULL,
        detected_with_session        TEXT,
        market_close_snapshot_time   TEXT,
        forward_context_captured_at  TEXT,
        context_timing               TEXT CHECK (context_timing IN ('STRICT_FORWARD', 'POST_CLOSE_FORWARD_CONTEXT')),
        engine_version               TEXT,
        spec_hash                    TEXT,
        data_json                    TEXT,
        data_hash                    TEXT CHECK (data_hash IS NULL OR length(data_hash) = 64),
        summary_json                 TEXT,
        warnings_json                TEXT,
        PRIMARY KEY (journal_id, session_date),
        CHECK (kind != 'CAPTURED' OR (market_close_snapshot_time IS NOT NULL AND context_timing IS NOT NULL
                                      AND engine_version IS NOT NULL AND spec_hash IS NOT NULL AND data_json IS NOT NULL
                                      AND data_hash IS NOT NULL AND summary_json IS NOT NULL AND warnings_json IS NOT NULL
                                      AND detected_with_session IS NULL
                                      AND recorded_at > market_close_snapshot_time)),
        CHECK (kind != 'MISSED' OR (detected_with_session IS NOT NULL AND detected_with_session > session_date
                                    AND data_json IS NULL AND context_timing IS NULL))
    ) WITHOUT ROWID;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_sessions_forward_only BEFORE INSERT ON forward_test_sessions
    WHEN NEW.session_date < (SELECT forward_start_date FROM forward_test_journals WHERE journal_id = NEW.journal_id)
    BEGIN SELECT RAISE(ABORT, 'sessions before the journal forward start cannot be recorded (no retroactive forward test)'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_sessions_no_backfill BEFORE INSERT ON forward_test_sessions
    WHEN NEW.session_date <= (SELECT MAX(session_date) FROM forward_test_sessions
                              WHERE journal_id = NEW.journal_id AND kind = 'CAPTURED')
    BEGIN SELECT RAISE(ABORT, 'no backfill: a session can only be added after every session already captured'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS forward_test_observations (
        journal_id             TEXT NOT NULL,
        session_date           TEXT NOT NULL,
        symbol                 TEXT NOT NULL,
        state_before           TEXT NOT NULL CHECK (state_before IN ({_q(STATES)})),
        decision               TEXT NOT NULL CHECK (decision IN ({_q(DECISIONS)})),
        state_after            TEXT NOT NULL CHECK (state_after IN ({_q(STATES)})),
        reason_code            TEXT NOT NULL,
        evaluated_side         TEXT CHECK (evaluated_side IS NULL OR evaluated_side IN ('ENTRY', 'EXIT')),
        rules_met              INTEGER CHECK (rules_met IS NULL OR rules_met >= 0),
        rules_total            INTEGER CHECK (rules_total IS NULL OR rules_total >= 0),
        exit_reasons_json      TEXT NOT NULL,
        unavailable_json       TEXT NOT NULL,
        feature_snapshot_json  TEXT NOT NULL,
        evaluation_trace_json  TEXT,
        lifecycle_json         TEXT NOT NULL,
        bar_json               TEXT,
        context_timing         TEXT NOT NULL CHECK (context_timing IN ('STRICT_FORWARD', 'POST_CLOSE_FORWARD_CONTEXT')),
        research_json          TEXT,
        event_json             TEXT,
        captured_at            TEXT NOT NULL,
        PRIMARY KEY (journal_id, session_date, symbol),
        FOREIGN KEY (journal_id, session_date) REFERENCES forward_test_sessions(journal_id, session_date) ON DELETE RESTRICT,
        CHECK (state_before || '>' || state_after IN ({_q(TRANSITIONS)})),
        CHECK (decision != 'ENTER' OR state_after = 'ENTRY_PENDING'),
        CHECK (state_after != 'ENTRY_PENDING' OR decision = 'ENTER'),
        CHECK (decision != 'EXIT' OR (state_after = 'EXIT_PENDING' AND state_before != 'EXIT_PENDING')),
        CHECK (state_after != 'CONTINUITY_BLOCKED' OR (decision = 'SKIP' AND reason_code = 'FORWARD_CONTINUITY_GAP')),
        CHECK (rules_met IS NULL OR rules_total IS NULL OR rules_met <= rules_total)
    ) WITHOUT ROWID;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_observations_captured_only BEFORE INSERT ON forward_test_observations
    WHEN (SELECT kind FROM forward_test_sessions WHERE journal_id = NEW.journal_id AND session_date = NEW.session_date)
         IS NOT 'CAPTURED'
         OR NEW.session_date != (SELECT MAX(session_date) FROM forward_test_sessions
                                 WHERE journal_id = NEW.journal_id AND kind = 'CAPTURED')
    BEGIN SELECT RAISE(ABORT, 'observations belong to the latest captured session only'); END;
    """,
    """
    CREATE TABLE IF NOT EXISTS forward_test_reference_fills (
        journal_id             TEXT NOT NULL,
        symbol                 TEXT NOT NULL,
        cycle_no               INTEGER NOT NULL CHECK (cycle_no >= 1),
        fill_type              TEXT NOT NULL CHECK (fill_type IN ('ENTRY', 'EXIT')),
        status                 TEXT NOT NULL CHECK (status IN ('FILLED', 'UNFILLED')),
        reason_code            TEXT,
        signal_session_date    TEXT NOT NULL,
        fill_session_date      TEXT,
        reference_open_price   REAL CHECK (reference_open_price IS NULL OR reference_open_price > 0),
        delay_sessions         INTEGER CHECK (delay_sessions IS NULL OR delay_sessions >= 0),
        reference_entry_price  REAL,
        reference_move_pct     REAL,
        price_basis_json       TEXT,
        dataset_id             TEXT,
        content_hash           TEXT CHECK (content_hash IS NULL OR length(content_hash) = 64),
        resolved_in_session    TEXT NOT NULL,
        created_at             TEXT NOT NULL,
        PRIMARY KEY (journal_id, symbol, cycle_no, fill_type),
        FOREIGN KEY (journal_id, resolved_in_session) REFERENCES forward_test_sessions(journal_id, session_date) ON DELETE RESTRICT,
        FOREIGN KEY (journal_id, signal_session_date, symbol)
            REFERENCES forward_test_observations(journal_id, session_date, symbol) ON DELETE RESTRICT,
        CHECK (fill_session_date IS NULL OR fill_session_date > signal_session_date),
        CHECK (resolved_in_session > signal_session_date),
        CHECK (status != 'FILLED' OR (fill_session_date IS NOT NULL AND reference_open_price IS NOT NULL
                                      AND dataset_id IS NOT NULL AND content_hash IS NOT NULL AND reason_code IS NULL)),
        CHECK (status != 'UNFILLED' OR (fill_session_date IS NULL AND reference_open_price IS NULL AND reason_code IS NOT NULL)),
        CHECK (fill_type = 'EXIT' OR (reference_move_pct IS NULL AND reference_entry_price IS NULL))
    ) WITHOUT ROWID;
    """,
    "CREATE INDEX IF NOT EXISTS idx_forward_fills_resolved ON forward_test_reference_fills(journal_id, resolved_in_session);",
]
for _t in EVIDENCE_TABLES:
    _STATEMENTS += [
        f"CREATE TRIGGER IF NOT EXISTS {_t}_no_update BEFORE UPDATE ON {_t} "
        f"BEGIN SELECT RAISE(ABORT, 'forward-test evidence is immutable'); END;",
        f"CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t} "
        f"BEGIN SELECT RAISE(ABORT, 'forward-test evidence is immutable'); END;",
        f"CREATE TRIGGER IF NOT EXISTS {_t}_not_archived BEFORE INSERT ON {_t} "
        f"WHEN (SELECT status FROM forward_test_journals WHERE journal_id = NEW.journal_id) IS NOT 'ACTIVE' "
        f"AND (SELECT status FROM forward_test_journals WHERE journal_id = NEW.journal_id) IS NOT 'CONTINUITY_BLOCKED' "
        f"BEGIN SELECT RAISE(ABORT, 'nothing can be recorded in an archived or unknown journal'); END;",
    ]


def run_forward_migrations(conn: sqlite3.Connection) -> None:
    run_backtest_migrations(conn)          # idempotent; journals reference strategy versions and reuse the bar cache
    conn.execute("PRAGMA foreign_keys = ON;")
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
