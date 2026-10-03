"""
database/delivery_migrations.py — Stage 4.3 OPT-IN DAILY BRIEF DESKTOP DELIVERY (additive). Idempotent; safe on any
database state. Only NEW tables: app_settings (closed to its three Stage 3.7 keys), app_leases and every earlier table
are untouched, and PRAGMA user_version is left alone (like every other Stage 3 / 4 migration).

  * daily_brief_delivery_settings  two keys: enabled (default OFF) and notify_only_if_activity (default ON).
  * daily_brief_deliveries         one row per delivery decision. kind SESSION = the automatic delivery of one brief
                                   session on one channel (UNIQUE brief_session + channel: never twice, never
                                   replaced); kind TEST = an explicit "Send test notification" (no brief session).
                                   A SESSION delivery is claimed as PENDING before the OS call and finalised once
                                   (PENDING -> DELIVERED / FAILED / UNSUPPORTED_PLATFORM); every other row is written
                                   final. Nothing else is ever updated, and no row is deleted.
"""
import sqlite3

SETTING_KEYS = ("enabled", "notify_only_if_activity")
CHANNELS = ("WINDOWS_DESKTOP",)
FINAL = ("DELIVERED", "FAILED", "UNSUPPORTED_PLATFORM")
STATUSES = ("PENDING",) + FINAL + ("SKIPPED_NO_ACTIVITY", "BASELINE")
_q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731

_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS daily_brief_delivery_settings (
        key         TEXT PRIMARY KEY CHECK (key IN ({_q(SETTING_KEYS)})),
        value       TEXT NOT NULL CHECK (value IN ('true', 'false')),
        updated_at  TEXT NOT NULL
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS daily_brief_delivery_settings_no_delete BEFORE DELETE ON daily_brief_delivery_settings
    BEGIN SELECT RAISE(ABORT, 'delivery settings are switched off, never deleted'); END;
    """,
    f"""
    CREATE TABLE IF NOT EXISTS daily_brief_deliveries (
        delivery_id          TEXT PRIMARY KEY CHECK (length(delivery_id) = 32),
        kind                 TEXT NOT NULL CHECK (kind IN ('SESSION', 'TEST')),
        brief_session        TEXT CHECK (brief_session IS NULL OR length(brief_session) = 10),
        channel              TEXT NOT NULL CHECK (channel IN ({_q(CHANNELS)})),
        status               TEXT NOT NULL CHECK (status IN ({_q(STATUSES)})),
        trigger              TEXT NOT NULL CHECK (trigger IN ('SCHEDULED', 'RETRY', 'MANUAL_CHECK', 'ENABLE', 'TEST')),
        created_at           TEXT NOT NULL,
        delivered_at         TEXT,
        summary_fingerprint  TEXT CHECK (summary_fingerprint IS NULL OR length(summary_fingerprint) = 64),
        title                TEXT CHECK (title IS NULL OR length(title) <= 64),
        body                 TEXT CHECK (body IS NULL OR length(body) <= 256),
        error_code           TEXT CHECK (error_code IS NULL OR length(error_code) <= 80),
        CHECK ((kind = 'SESSION') = (brief_session IS NOT NULL)),
        CHECK (kind = 'SESSION' OR status IN ({_q(FINAL)})),
        CHECK ((status = 'DELIVERED') = (delivered_at IS NOT NULL)),
        UNIQUE (brief_session, channel)
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_daily_brief_deliveries_recent ON daily_brief_deliveries (created_at DESC);",
    """
    CREATE TRIGGER IF NOT EXISTS daily_brief_deliveries_no_delete BEFORE DELETE ON daily_brief_deliveries
    BEGIN SELECT RAISE(ABORT, 'delivery history is kept'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS daily_brief_deliveries_final BEFORE UPDATE ON daily_brief_deliveries
    WHEN OLD.status != 'PENDING'
    BEGIN SELECT RAISE(ABORT, 'a final delivery row never changes'); END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS daily_brief_deliveries_pending_once BEFORE UPDATE ON daily_brief_deliveries
    WHEN NEW.status NOT IN ({_q(FINAL)}) OR NEW.delivery_id != OLD.delivery_id OR NEW.kind != OLD.kind
         OR NEW.brief_session IS NOT OLD.brief_session OR NEW.channel != OLD.channel OR NEW.trigger != OLD.trigger
         OR NEW.created_at != OLD.created_at OR NEW.summary_fingerprint IS NOT OLD.summary_fingerprint
         OR NEW.title IS NOT OLD.title OR NEW.body IS NOT OLD.body
    BEGIN SELECT RAISE(ABORT, 'a pending delivery only becomes DELIVERED, FAILED or UNSUPPORTED_PLATFORM'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS daily_brief_deliveries_no_replace BEFORE INSERT ON daily_brief_deliveries
    WHEN EXISTS (SELECT 1 FROM daily_brief_deliveries WHERE delivery_id = NEW.delivery_id OR
                 (NEW.brief_session IS NOT NULL AND brief_session = NEW.brief_session AND channel = NEW.channel))
    BEGIN SELECT RAISE(ABORT, 'already stored: a brief session is delivered at most once per channel'); END;
    """,
]


def run_delivery_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
