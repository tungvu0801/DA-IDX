"""
database/alpaca_order_migrations.py — Stage 4.6B ALPACA PAPER manual orders (additive). Idempotent; safe on any database
state. Only three NEW tables; no Stage 4.5 paper_* table, no Stage 4.6A state (4.6A has none) and no other table is touched,
and PRAGMA user_version is left alone. No credential is ever stored — the Alpaca account appears only as a sha256
fingerprint and a masked number.

Created by the first explicit settings write (Link account); reads never create these tables. Row ids are plain
INTEGER PRIMARY KEYs (no AUTOINCREMENT, so no sqlite_sequence table is added): since rows can never be deleted, an id is
never reused.

  * alpaca_paper_order_settings   one row (id = 1): manual orders enabled (default OFF) and the explicitly linked account.
  * alpaca_paper_order_intents    one row per preview: the immutable preview + exact payload + client_order_id, then the
                                  order's lifecycle (DESIGN_46B_FINAL §4). Identity / payload / preview / reference columns
                                  never change; the Alpaca order id is set once; a terminal row never changes.
  * alpaca_paper_order_events     append-only audit trail (no update, no delete).
Nothing is ever deleted.
"""
import sqlite3

STATES = ("PREVIEWED", "SUPERSEDED", "EXPIRED", "CONFIRMING", "CONFIRM_REJECTED", "SUBMISSION_PENDING", "SUBMIT_NOT_SENT",
          "RECONCILIATION_REQUIRED", "SUBMITTED", "BROKER_ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCELED",
          "BROKER_REJECTED", "ABANDONED")
TERMINAL = ("SUPERSEDED", "EXPIRED", "CONFIRM_REJECTED", "FILLED", "CANCELED", "BROKER_REJECTED", "ABANDONED")
EVENT_KINDS = ("LINKED", "RELINKED", "ENABLED", "DISABLED", "CONFIG_CHECK", "PREVIEWED", "SUPERSEDED", "EXPIRED",
               "CONFIRM_STARTED", "CONFIRM_REJECTED", "SUBMIT_STARTED", "SUBMIT_RESULT", "LOOKUP", "STATUS_CHANGE", "RETRY",
               "ABANDONED")
IMMUTABLE = ("client_order_id", "symbol", "side", "qty", "order_type", "time_in_force", "payload_json", "payload_sha256",
             "reference_price", "reference_session", "reference_source", "estimated_notional", "preview_json",
             "preview_hash", "account_fp", "account_masked", "previewed_at", "expires_at")
_Q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
_H64 = "CHECK (length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*')"

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS alpaca_paper_order_settings (
        id                    INTEGER PRIMARY KEY CHECK (id = 1),
        enabled               INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
        bound_account_fp      TEXT CHECK (bound_account_fp IS NULL OR (length(bound_account_fp) = 64 AND bound_account_fp NOT GLOB '*[^0-9a-f]*')),
        bound_account_masked  TEXT CHECK (bound_account_masked IS NULL OR length(bound_account_masked) <= 12),
        bound_at              TEXT,
        last_no_shorting      INTEGER CHECK (last_no_shorting IS NULL OR last_no_shorting IN (0, 1)),
        last_config_check_at  TEXT,
        updated_at            TEXT NOT NULL,
        CHECK (enabled = 0 OR bound_account_fp IS NOT NULL)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS alpaca_paper_order_intents (
        intent_id             INTEGER PRIMARY KEY,
        client_order_id       TEXT NOT NULL UNIQUE CHECK (length(client_order_id) = 38 AND client_order_id GLOB 'sa46b-*'
                                                          AND substr(client_order_id, 7) NOT GLOB '*[^0-9a-f]*'),
        symbol                TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 8 AND symbol NOT GLOB '*[^A-Z.]*'),
        side                  TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        qty                   INTEGER NOT NULL CHECK (typeof(qty) = 'integer' AND qty BETWEEN 1 AND 10000),
        order_type            TEXT NOT NULL CHECK (order_type = 'market'),
        time_in_force         TEXT NOT NULL CHECK (time_in_force = 'day'),
        payload_json          TEXT NOT NULL,
        payload_sha256        TEXT NOT NULL {_H64.format(c="payload_sha256")},
        reference_price       TEXT,
        reference_session     TEXT,
        reference_source      TEXT,
        estimated_notional    TEXT,
        preview_json          TEXT NOT NULL,
        preview_hash          TEXT NOT NULL UNIQUE {_H64.format(c="preview_hash")},
        account_fp            TEXT NOT NULL {_H64.format(c="account_fp")},
        account_masked        TEXT NOT NULL CHECK (length(account_masked) <= 12),
        previewed_at          TEXT NOT NULL,
        expires_at            TEXT NOT NULL,
        state                 TEXT NOT NULL CHECK (state IN ({_Q(STATES)})),
        confirmed_at          TEXT,
        submit_attempts       INTEGER NOT NULL DEFAULT 0 CHECK (submit_attempts BETWEEN 0 AND 3),
        last_submit_at        TEXT,
        lookups_not_found     INTEGER NOT NULL DEFAULT 0 CHECK (lookups_not_found >= 0),
        first_not_found_at    TEXT,
        last_lookup_at        TEXT,
        alpaca_order_id       TEXT UNIQUE CHECK (alpaca_order_id IS NULL OR length(alpaca_order_id) = 36),
        broker_status         TEXT CHECK (broker_status IS NULL OR length(broker_status) <= 32),
        broker_status_at      TEXT,
        filled_qty            TEXT,
        filled_avg_price      TEXT,
        error_code            TEXT CHECK (error_code IS NULL OR length(error_code) <= 40),
        http_status           INTEGER,
        broker_error_code     INTEGER,
        error_text            TEXT CHECK (error_text IS NULL OR length(error_text) <= 200),
        updated_at            TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_apo_intents_state ON alpaca_paper_order_intents (state)",
    "CREATE INDEX IF NOT EXISTS idx_apo_intents_symbol ON alpaca_paper_order_intents (symbol, state)",
    f"""
    CREATE TABLE IF NOT EXISTS alpaca_paper_order_events (
        event_id              INTEGER PRIMARY KEY,
        intent_id             INTEGER REFERENCES alpaca_paper_order_intents(intent_id),
        at                    TEXT NOT NULL,
        kind                  TEXT NOT NULL CHECK (kind IN ({_Q(EVENT_KINDS)})),
        from_state            TEXT,
        to_state              TEXT,
        http_status           INTEGER,
        broker_status         TEXT CHECK (broker_status IS NULL OR length(broker_status) <= 32),
        code                  TEXT CHECK (code IS NULL OR length(code) <= 40),
        latency_ms            REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_apo_events_intent ON alpaca_paper_order_events (intent_id, event_id)",
    # ---- guards: nothing is deleted; events never change; identity never changes; the broker id is set once; terminal = final
    """CREATE TRIGGER IF NOT EXISTS apo_settings_no_delete BEFORE DELETE ON alpaca_paper_order_settings
       BEGIN SELECT RAISE(ABORT, 'alpaca_paper_order_settings rows are never deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS apo_intents_no_delete BEFORE DELETE ON alpaca_paper_order_intents
       BEGIN SELECT RAISE(ABORT, 'alpaca_paper_order_intents rows are never deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS apo_events_no_delete BEFORE DELETE ON alpaca_paper_order_events
       BEGIN SELECT RAISE(ABORT, 'alpaca_paper_order_events rows are never deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS apo_events_no_update BEFORE UPDATE ON alpaca_paper_order_events
       BEGIN SELECT RAISE(ABORT, 'alpaca_paper_order_events rows are append-only'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS apo_intents_terminal_final BEFORE UPDATE ON alpaca_paper_order_intents
       WHEN OLD.state IN ({_Q(TERMINAL)})
       BEGIN SELECT RAISE(ABORT, 'a terminal alpaca paper order intent is final'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS apo_intents_immutable BEFORE UPDATE ON alpaca_paper_order_intents
       WHEN {" OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in IMMUTABLE)}
       BEGIN SELECT RAISE(ABORT, 'alpaca paper order identity, payload, preview and reference columns are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS apo_intents_order_id_once BEFORE UPDATE OF alpaca_order_id ON alpaca_paper_order_intents
       WHEN OLD.alpaca_order_id IS NOT NULL AND NEW.alpaca_order_id IS NOT OLD.alpaca_order_id
       BEGIN SELECT RAISE(ABORT, 'the Alpaca order id of an intent is set once'); END""",
]


def run_alpaca_order_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
