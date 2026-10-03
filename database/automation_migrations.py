"""
database/automation_migrations.py — Stage 3.7 opt-in automatic forward capture: local settings + a scheduler lease
(additive). Idempotent; safe on any database state.

Only NEW tables are created. No research, strategy, backtest or forward-evidence table is touched, and PRAGMA
user_version is left alone (like every other Stage 3 migration). Nothing here stores a strategy decision, signal or
result — those stay in the forward-evidence tables written by the ordinary capture.

  * app_settings — the few automation settings, one row per ALLOWED key (enforced by the database):
      forward_auto_capture_enabled   "true" / "false" (default: absent = false)
      forward_auto_capture_time_et   "HH:MM" in America/New_York
      forward_auto_capture_last_check  a short JSON summary of the last automatic check (times, counts, result codes)
  * app_leases — a short-lived lease so two scheduler loops (e.g. two accidental server processes) never run a capture
    check at the same time. The forward journal's own lock and unique constraints remain the final safety layer.
"""
import sqlite3

SETTING_KEYS = ("forward_auto_capture_enabled", "forward_auto_capture_time_et", "forward_auto_capture_last_check")


def _q(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS app_settings (
        key          TEXT PRIMARY KEY CHECK (key IN ({_q(SETTING_KEYS)})),
        value        TEXT NOT NULL CHECK (length(value) <= 20000),
        updated_at   TEXT NOT NULL
    ) WITHOUT ROWID;
    """,
    """
    CREATE TABLE IF NOT EXISTS app_leases (
        name         TEXT PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 64),
        owner        TEXT NOT NULL,
        acquired_at  TEXT NOT NULL,
        expires_at   TEXT NOT NULL CHECK (expires_at > acquired_at)
    ) WITHOUT ROWID;
    """,
]


def run_automation_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
