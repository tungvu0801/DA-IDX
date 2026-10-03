"""
database/saved_scan_migrations.py — Stage 4.1 SAVED STRATEGY SCANS + RULES-MET CHANGE ALERTS (additive). Idempotent; safe
on any database state. Only NEW tables are created: no strategy, backtest, forward, excursion, research, portfolio,
AI-history or Stage 3.7 table is touched, and PRAGMA user_version is left alone (like every other Stage 3 / 4 migration).

  * saved_strategy_scans     one saved scanner configuration. Its IDENTITY (strategy version, list source, custom list)
                             can never change — a different configuration is a new saved scan. Only the name, the alert
                             switch and the archive stamp (set once, never cleared) may be updated. No row is deleted.
  * saved_scan_snapshots     one immutable compact snapshot per saved scan and decision session (UNIQUE): the resolved
                             symbols, each symbol's scanner status, the strategy hashes and a fingerprint. No update, no delete.
  * saved_scan_alert_events  at most one immutable event per saved scan and session (UNIQUE) when the RULES MET set changed.
                             Only read_at (administrative read state) may be updated. No delete.
"""
import sqlite3

_ID = "CHECK (length({c}) = 32)"
_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS saved_strategy_scans (
        saved_scan_id        TEXT PRIMARY KEY {_ID.format(c="saved_scan_id")},
        strategy_id          TEXT NOT NULL {_ID.format(c="strategy_id")},
        strategy_version_id  TEXT NOT NULL {_ID.format(c="strategy_version_id")},
        list_source          TEXT NOT NULL CHECK (list_source IN ('SAVED_UNIVERSE', 'WATCHLIST', 'CUSTOM')),
        custom_symbols_json  TEXT CHECK (custom_symbols_json IS NULL OR (json_valid(custom_symbols_json)
                                                                         AND length(custom_symbols_json) <= 4000)),
        identity_hash        TEXT NOT NULL CHECK (length(identity_hash) = 64),
        name                 TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
        alerts_enabled       INTEGER NOT NULL CHECK (alerts_enabled IN (0, 1)),
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL,
        archived_at          TEXT,
        CHECK ((list_source = 'CUSTOM') = (custom_symbols_json IS NOT NULL))
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_saved_strategy_scans_active ON saved_strategy_scans (archived_at, alerts_enabled);",
    """
    CREATE TRIGGER IF NOT EXISTS saved_strategy_scans_identity_fixed
    BEFORE UPDATE OF saved_scan_id, strategy_id, strategy_version_id, list_source, custom_symbols_json, identity_hash,
                     created_at ON saved_strategy_scans
    BEGIN SELECT RAISE(ABORT, 'a saved scan''s strategy version, list source and symbols never change; save a new scan'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS saved_strategy_scans_archive_final
    BEFORE UPDATE OF archived_at ON saved_strategy_scans WHEN OLD.archived_at IS NOT NULL
    BEGIN SELECT RAISE(ABORT, 'an archived saved scan stays archived'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS saved_strategy_scans_no_delete BEFORE DELETE ON saved_strategy_scans
    BEGIN SELECT RAISE(ABORT, 'saved scans are archived, never deleted'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS saved_scan_snapshots (
        snapshot_id           TEXT PRIMARY KEY {_ID.format(c="snapshot_id")},
        saved_scan_id         TEXT NOT NULL REFERENCES saved_strategy_scans(saved_scan_id) ON DELETE RESTRICT,
        decision_session      TEXT NOT NULL CHECK (length(decision_session) = 10),
        evaluated_at          TEXT NOT NULL,
        trigger               TEXT NOT NULL CHECK (trigger IN ('MANUAL_CHECK', 'SCHEDULED', 'RETRY')),
        strategy_version_id   TEXT NOT NULL {_ID.format(c="strategy_version_id")},
        spec_hash             TEXT NOT NULL CHECK (length(spec_hash) = 64),
        rules_hash            TEXT NOT NULL CHECK (length(rules_hash) = 64),
        registry_fingerprint  TEXT NOT NULL,
        scanner_engine        TEXT NOT NULL,
        resolved_symbols_json TEXT NOT NULL CHECK (json_valid(resolved_symbols_json)),
        status_map_json       TEXT NOT NULL CHECK (json_valid(status_map_json)),
        groups_json           TEXT NOT NULL CHECK (json_valid(groups_json)),
        conditions_json       TEXT NOT NULL CHECK (json_valid(conditions_json)),
        list_changes_json     TEXT NOT NULL CHECK (json_valid(list_changes_json)),
        context_timing        TEXT,
        previous_snapshot_id  TEXT,
        is_baseline           INTEGER NOT NULL CHECK (is_baseline IN (0, 1)),
        snapshot_fingerprint  TEXT NOT NULL CHECK (length(snapshot_fingerprint) = 64),
        UNIQUE (saved_scan_id, decision_session)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_saved_scan_snapshots_scan ON saved_scan_snapshots (saved_scan_id, decision_session DESC);",
    """
    CREATE TRIGGER IF NOT EXISTS saved_scan_snapshots_no_update BEFORE UPDATE ON saved_scan_snapshots
    BEGIN SELECT RAISE(ABORT, 'saved scan snapshots are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS saved_scan_snapshots_no_delete BEFORE DELETE ON saved_scan_snapshots
    BEGIN SELECT RAISE(ABORT, 'saved scan snapshots are immutable'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS saved_scan_alert_events (
        alert_id               TEXT PRIMARY KEY {_ID.format(c="alert_id")},
        saved_scan_id          TEXT NOT NULL REFERENCES saved_strategy_scans(saved_scan_id) ON DELETE RESTRICT,
        snapshot_id            TEXT NOT NULL UNIQUE REFERENCES saved_scan_snapshots(snapshot_id) ON DELETE RESTRICT,
        previous_snapshot_id   TEXT NOT NULL REFERENCES saved_scan_snapshots(snapshot_id) ON DELETE RESTRICT,
        decision_session       TEXT NOT NULL CHECK (length(decision_session) = 10),
        previous_session       TEXT NOT NULL CHECK (length(previous_session) = 10),
        newly_rules_met_json   TEXT NOT NULL CHECK (json_valid(newly_rules_met_json)),
        no_longer_rules_met_json TEXT NOT NULL CHECK (json_valid(no_longer_rules_met_json)),
        alert_fingerprint      TEXT NOT NULL UNIQUE CHECK (length(alert_fingerprint) = 64),
        created_at             TEXT NOT NULL,
        read_at                TEXT,
        CHECK (json_array_length(newly_rules_met_json) + json_array_length(no_longer_rules_met_json) > 0),
        UNIQUE (saved_scan_id, decision_session)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_saved_scan_alert_events_new ON saved_scan_alert_events (read_at, decision_session DESC);",
    """
    CREATE TRIGGER IF NOT EXISTS saved_scan_alert_events_content_fixed
    BEFORE UPDATE OF alert_id, saved_scan_id, snapshot_id, previous_snapshot_id, decision_session, previous_session,
                     newly_rules_met_json, no_longer_rules_met_json, alert_fingerprint, created_at ON saved_scan_alert_events
    BEGIN SELECT RAISE(ABORT, 'alert events are immutable (only the read state changes)'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS saved_scan_alert_events_no_delete BEFORE DELETE ON saved_scan_alert_events
    BEGIN SELECT RAISE(ABORT, 'alert events are kept'); END;
    """,
]


# an INSERT OR REPLACE would delete the existing row without firing the delete triggers: refuse any second row instead
for _t, _where in (("saved_strategy_scans", "saved_scan_id = NEW.saved_scan_id"),
                   ("saved_scan_snapshots", "snapshot_id = NEW.snapshot_id OR (saved_scan_id = NEW.saved_scan_id AND "
                                            "decision_session = NEW.decision_session)"),
                   ("saved_scan_alert_events", "alert_id = NEW.alert_id OR alert_fingerprint = NEW.alert_fingerprint OR "
                                               "(saved_scan_id = NEW.saved_scan_id AND decision_session = NEW.decision_session)")):
    _STATEMENTS.append(f"CREATE TRIGGER IF NOT EXISTS {_t}_no_replace BEFORE INSERT ON {_t} WHEN EXISTS (SELECT 1 FROM {_t} "
                       f"WHERE {_where}) BEGIN SELECT RAISE(ABORT, 'already stored: {_t} rows are never replaced'); END;")


def run_saved_scan_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
