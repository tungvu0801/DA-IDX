"""
database/excursion_migrations.py — Stage 3.6 FORWARD MFE / MAE (excursion) schema (additive). Idempotent; safe on any
database state.

Only NEW tables, indexes and triggers are created. Every Stage 1 – 3.5 table is left exactly as it is (the Stage 3.3
migration is only re-run, as it is idempotent, because excursions reference forward journals, sessions, observations and
reference fills). PRAGMA user_version is not touched, like every other Stage 3 migration.

What the database itself guarantees:
  * forward_test_excursion_tracking — one immutable row per journal: the captured session from which MFE / MAE is
    tracked. It is written by the first capture made with Stage 3.6 code (never earlier), so older sessions stay legacy.
  * forward_test_excursions — append-only excursion evidence of forward REFERENCE cycles. NO BACKFILL:
      - a row can only be added for the LATEST captured session of its journal (inside that capture);
      - a cycle is tracked only from its reference ENTRY session, and only when that session is on or after the
        journal's tracking start (the ENTRY_SESSION row needs the FILLED reference entry of that exact session);
      - every later row needs the cycle's ENTRY_SESSION row, so a legacy cycle (entered before tracking) can never gain
        rows, and nothing is added after a cycle's tracking has ended (COMPLETE or INCOMPLETE);
      - an EXIT_OPEN row needs the FILLED reference exit of that exact session;
      - one row per (journal, symbol, cycle, session, observation type); never UPDATEd or DELETEd;
      - cumulative MFE >= 0 and cumulative MAE <= 0 (Stage 3.2 clamps);
      - nothing can be inserted into an archived journal.
"""
import sqlite3

from database.forward_migrations import run_forward_migrations

EXCURSION_SCHEMA_VERSION = 1
OBSERVATION_TYPES = ("ENTRY_SESSION", "HELD_SESSION", "EXIT_OPEN", "CONTINUITY_GAP")
ROW_STATUSES = ("OBSERVED", "EXCURSION_DATA_UNAVAILABLE", "INCOMPLETE_DUE_TO_CONTINUITY_GAP")
TRACKING_STATUSES = ("TRACKING", "COMPLETE", "INCOMPLETE_DUE_TO_CONTINUITY_GAP", "INCOMPLETE_DATA_UNAVAILABLE")
TABLES = ("forward_test_excursion_tracking", "forward_test_excursions")


def _q(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


_LATEST_CAPTURED = ("(SELECT MAX(session_date) FROM forward_test_sessions WHERE journal_id = NEW.journal_id "
                    "AND kind = 'CAPTURED')")
_OPEN_JOURNAL = ("(SELECT status FROM forward_test_journals WHERE journal_id = NEW.journal_id) IS NOT 'ACTIVE' AND "
                 "(SELECT status FROM forward_test_journals WHERE journal_id = NEW.journal_id) IS NOT 'CONTINUITY_BLOCKED'")

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS forward_test_excursion_tracking (
        journal_id              TEXT PRIMARY KEY REFERENCES forward_test_journals(journal_id) ON DELETE RESTRICT,
        activated_in_session    TEXT NOT NULL,
        activated_at            TEXT NOT NULL,
        engine_version          TEXT NOT NULL
    ) WITHOUT ROWID;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS forward_test_excursion_tracking_forward_only BEFORE INSERT ON forward_test_excursion_tracking
    WHEN NEW.activated_in_session IS NOT {_LATEST_CAPTURED} OR {_OPEN_JOURNAL}
    BEGIN SELECT RAISE(ABORT, 'excursion tracking starts with the session being captured (no backfill)'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS forward_test_excursions (
        journal_id              TEXT NOT NULL,
        symbol                  TEXT NOT NULL,
        cycle_no                INTEGER NOT NULL CHECK (cycle_no >= 1),
        session_date            TEXT NOT NULL,
        observation_type        TEXT NOT NULL CHECK (observation_type IN ({_q(OBSERVATION_TYPES)})),
        status                  TEXT NOT NULL CHECK (status IN ({_q(ROW_STATUSES)})),
        reason_code             TEXT,
        tracking_status         TEXT NOT NULL CHECK (tracking_status IN ({_q(TRACKING_STATUSES)})),
        entry_session_date      TEXT NOT NULL,
        entry_reference_price   REAL NOT NULL CHECK (entry_reference_price > 0),
        basis_entry_open        REAL CHECK (basis_entry_open IS NULL OR basis_entry_open > 0),
        price_basis             TEXT CHECK (price_basis IS NULL OR price_basis IN ('THIS_CAPTURE', 'AS_CAPTURED')),
        observed_open           REAL,
        observed_high           REAL,
        observed_low            REAL,
        session_high_pct        REAL,
        session_low_pct         REAL,
        cumulative_mfe_pct      REAL NOT NULL CHECK (cumulative_mfe_pct >= 0),
        cumulative_mae_pct      REAL NOT NULL CHECK (cumulative_mae_pct <= 0),
        dataset_id              TEXT,
        content_hash            TEXT CHECK (content_hash IS NULL OR length(content_hash) = 64),
        engine_version          TEXT NOT NULL,
        captured_at             TEXT NOT NULL,
        PRIMARY KEY (journal_id, symbol, cycle_no, session_date, observation_type),
        FOREIGN KEY (journal_id, session_date, symbol)
            REFERENCES forward_test_observations(journal_id, session_date, symbol) ON DELETE RESTRICT,
        CHECK (session_date >= entry_session_date),
        CHECK (status != 'OBSERVED' OR (basis_entry_open IS NOT NULL AND session_high_pct IS NOT NULL
                                        AND session_low_pct IS NOT NULL AND dataset_id IS NOT NULL)),
        CHECK (observation_type != 'ENTRY_SESSION' OR (status = 'OBSERVED' AND session_date = entry_session_date
                                                       AND observed_high IS NOT NULL AND observed_low IS NOT NULL)),
        CHECK (observation_type != 'EXIT_OPEN' OR (status = 'OBSERVED' AND tracking_status = 'COMPLETE'
                                                   AND observed_open IS NOT NULL AND observed_high IS NULL
                                                   AND observed_low IS NULL)),
        CHECK (observation_type != 'CONTINUITY_GAP' OR (status = 'INCOMPLETE_DUE_TO_CONTINUITY_GAP'
                                                        AND tracking_status = 'INCOMPLETE_DUE_TO_CONTINUITY_GAP')),
        CHECK (tracking_status != 'COMPLETE' OR observation_type = 'EXIT_OPEN')
    ) WITHOUT ROWID;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS forward_test_excursions_latest_session_only BEFORE INSERT ON forward_test_excursions
    WHEN NEW.session_date IS NOT {_LATEST_CAPTURED}
    BEGIN SELECT RAISE(ABORT, 'excursions belong to the session being captured only (no backfill)'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_excursions_tracked_journal BEFORE INSERT ON forward_test_excursions
    WHEN NOT EXISTS (SELECT 1 FROM forward_test_excursion_tracking t WHERE t.journal_id = NEW.journal_id
                     AND t.activated_in_session <= NEW.entry_session_date)
    BEGIN SELECT RAISE(ABORT, 'only cycles entered after excursion tracking started can be tracked (no backfill)'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_excursions_entry_is_the_reference_entry BEFORE INSERT ON forward_test_excursions
    WHEN NEW.observation_type = 'ENTRY_SESSION' AND NOT EXISTS (
        SELECT 1 FROM forward_test_reference_fills f WHERE f.journal_id = NEW.journal_id AND f.symbol = NEW.symbol
        AND f.cycle_no = NEW.cycle_no AND f.fill_type = 'ENTRY' AND f.status = 'FILLED'
        AND f.fill_session_date = NEW.session_date AND f.reference_open_price = NEW.entry_reference_price)
    BEGIN SELECT RAISE(ABORT, 'an entry excursion needs the filled reference entry of that session'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_excursions_tracked_from_entry BEFORE INSERT ON forward_test_excursions
    WHEN NEW.observation_type != 'ENTRY_SESSION' AND NOT EXISTS (
        SELECT 1 FROM forward_test_excursions e WHERE e.journal_id = NEW.journal_id AND e.symbol = NEW.symbol
        AND e.cycle_no = NEW.cycle_no AND e.observation_type = 'ENTRY_SESSION'
        AND e.entry_reference_price = NEW.entry_reference_price AND e.entry_session_date = NEW.entry_session_date)
    BEGIN SELECT RAISE(ABORT, 'a cycle is tracked only from its reference entry session (legacy cycles are never tracked)'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_excursions_until_tracking_ends BEFORE INSERT ON forward_test_excursions
    WHEN EXISTS (SELECT 1 FROM forward_test_excursions e WHERE e.journal_id = NEW.journal_id AND e.symbol = NEW.symbol
                 AND e.cycle_no = NEW.cycle_no AND e.tracking_status != 'TRACKING')
    BEGIN SELECT RAISE(ABORT, 'excursion tracking of this cycle has ended'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS forward_test_excursions_exit_is_the_reference_exit BEFORE INSERT ON forward_test_excursions
    WHEN NEW.observation_type = 'EXIT_OPEN' AND NOT EXISTS (
        SELECT 1 FROM forward_test_reference_fills f WHERE f.journal_id = NEW.journal_id AND f.symbol = NEW.symbol
        AND f.cycle_no = NEW.cycle_no AND f.fill_type = 'EXIT' AND f.status = 'FILLED'
        AND f.fill_session_date = NEW.session_date AND f.reference_open_price = NEW.observed_open)
    BEGIN SELECT RAISE(ABORT, 'an exit excursion needs the filled reference exit of that session'); END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS forward_test_excursions_not_archived BEFORE INSERT ON forward_test_excursions
    WHEN {_OPEN_JOURNAL}
    BEGIN SELECT RAISE(ABORT, 'nothing can be recorded in an archived or unknown journal'); END;
    """,
]
for _t in TABLES:
    _STATEMENTS += [
        f"CREATE TRIGGER IF NOT EXISTS {_t}_no_update BEFORE UPDATE ON {_t} "
        f"BEGIN SELECT RAISE(ABORT, 'forward excursion evidence is immutable'); END;",
        f"CREATE TRIGGER IF NOT EXISTS {_t}_no_delete BEFORE DELETE ON {_t} "
        f"BEGIN SELECT RAISE(ABORT, 'forward excursion evidence is immutable'); END;",
    ]


def run_excursion_migrations(conn: sqlite3.Connection) -> None:
    run_forward_migrations(conn)           # idempotent; excursions reference journals, sessions, observations and fills
    conn.execute("PRAGMA foreign_keys = ON;")
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
