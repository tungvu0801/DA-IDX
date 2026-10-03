"""
database/backtest_migrations.py — Stage 3.2 schema (additive). Idempotent; safe on any database state.

Only NEW tables, indexes and triggers are created. research_snapshots / research_outcomes, the Stage 3.1 strategy
tables, database/migrations.py and PRAGMA user_version are not touched (the Stage 3.1 migration is only re-run, as it
is idempotent, because backtest runs reference strategy versions by foreign key).

Immutability is enforced by the database itself:
  * historical_bar_datasets / historical_daily_bars — a dataset is ONE fetch of one symbol (source, feed, adjustment,
    requested range, content hash). Rows are never UPDATEd or DELETEd; an explicit refresh stores a NEW dataset, so
    the exact bars an old run used stay available and verifiable (adjusted history can change after a split or
    dividend, which is why bars are never overwritten in place).
  * backtest_runs — never DELETEd; identity / configuration / data columns never change; status only moves
    PENDING -> RUNNING -> COMPLETED | FAILED (or PENDING -> FAILED), and a COMPLETED or FAILED run never changes again.
    A FAILED run can hold no result; a COMPLETED run must hold one.
  * backtest_trades / backtest_signals / backtest_equity — insertable only while their run is RUNNING (inside the
    single transaction that completes it), never UPDATEd or DELETEd. A fill can never be on or before its signal day.
"""
import sqlite3

from database.strategy_migrations import run_strategy_migrations

BACKTEST_SCHEMA_VERSION = 1

RESULT_TABLES = ("backtest_trades", "backtest_signals", "backtest_equity")


def _immutable(table: str, what: str) -> list:
    return [
        f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{what} are immutable'); END;",
        f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{what} are immutable'); END;",
    ]


_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS historical_bar_datasets (
        dataset_id       TEXT PRIMARY KEY CHECK (length(dataset_id) = 32),
        symbol           TEXT NOT NULL,
        source           TEXT NOT NULL,
        feed             TEXT NOT NULL,
        adjustment       TEXT NOT NULL,
        timeframe        TEXT NOT NULL CHECK (timeframe = '1Day'),
        requested_start  TEXT NOT NULL,
        requested_end    TEXT NOT NULL,
        first_session    TEXT,
        last_session     TEXT,
        bar_count        INTEGER NOT NULL CHECK (bar_count >= 0),
        content_hash     TEXT NOT NULL CHECK (length(content_hash) = 64),
        fetched_at       TEXT NOT NULL,
        CHECK (requested_start <= requested_end)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_bar_datasets_lookup ON historical_bar_datasets(symbol, feed, adjustment, fetched_at);",
    """
    CREATE TABLE IF NOT EXISTS historical_daily_bars (
        dataset_id     TEXT NOT NULL REFERENCES historical_bar_datasets(dataset_id) ON DELETE RESTRICT,
        session_date   TEXT NOT NULL,
        bar_timestamp  TEXT NOT NULL,
        open           REAL NOT NULL,
        high           REAL NOT NULL,
        low            REAL NOT NULL,
        close          REAL NOT NULL,
        volume         REAL NOT NULL,
        PRIMARY KEY (dataset_id, session_date)
    ) WITHOUT ROWID;
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        run_id                        TEXT PRIMARY KEY CHECK (length(run_id) = 32),
        strategy_id                   TEXT NOT NULL REFERENCES strategy_definitions(strategy_id) ON DELETE RESTRICT,
        strategy_version_id           TEXT NOT NULL REFERENCES strategy_versions(version_id) ON DELETE RESTRICT,
        version_number                INTEGER NOT NULL CHECK (version_number >= 1),
        spec_hash                     TEXT NOT NULL CHECK (length(spec_hash) = 64),
        rules_hash                    TEXT NOT NULL CHECK (length(rules_hash) = 64),
        feature_registry_version      INTEGER NOT NULL,
        feature_registry_fingerprint  TEXT NOT NULL CHECK (length(feature_registry_fingerprint) = 64),
        engine_version                TEXT NOT NULL,
        start_date                    TEXT NOT NULL,
        end_date                      TEXT NOT NULL,
        config_json                   TEXT NOT NULL,
        config_hash                   TEXT NOT NULL CHECK (length(config_hash) = 64),
        data_json                     TEXT NOT NULL,
        data_hash                     TEXT NOT NULL CHECK (length(data_hash) = 64),
        preflight_json                TEXT NOT NULL,
        status                        TEXT NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
        created_at                    TEXT NOT NULL,
        started_at                    TEXT,
        finished_at                   TEXT,
        error_code                    TEXT,
        error_message                 TEXT,
        point_in_time_safe            INTEGER CHECK (point_in_time_safe IN (0, 1)),
        result_json                   TEXT,
        CHECK (start_date < end_date),
        CHECK (status != 'COMPLETED' OR (result_json IS NOT NULL AND point_in_time_safe IS NOT NULL
                                         AND finished_at IS NOT NULL AND error_code IS NULL)),
        CHECK (status != 'FAILED' OR (error_code IS NOT NULL AND result_json IS NULL AND point_in_time_safe IS NULL))
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_backtest_runs_version ON backtest_runs(strategy_id, version_number, created_at);",
    """
    CREATE TRIGGER IF NOT EXISTS backtest_runs_no_delete BEFORE DELETE ON backtest_runs
    BEGIN SELECT RAISE(ABORT, 'backtest runs are historical evidence and are never deleted'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS backtest_runs_final BEFORE UPDATE ON backtest_runs
    WHEN OLD.status IN ('COMPLETED', 'FAILED')
    BEGIN SELECT RAISE(ABORT, 'completed and failed backtest runs are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS backtest_runs_fixed_fields BEFORE UPDATE OF run_id, strategy_id, strategy_version_id,
        version_number, spec_hash, rules_hash, feature_registry_version, feature_registry_fingerprint, engine_version,
        start_date, end_date, config_json, config_hash, data_json, data_hash, preflight_json, created_at ON backtest_runs
    BEGIN SELECT RAISE(ABORT, 'backtest run identity, configuration and data are immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS backtest_runs_status_flow BEFORE UPDATE OF status ON backtest_runs
    WHEN NOT ((OLD.status = 'PENDING' AND NEW.status IN ('RUNNING', 'FAILED'))
              OR (OLD.status = 'RUNNING' AND NEW.status IN ('COMPLETED', 'FAILED')))
    BEGIN SELECT RAISE(ABORT, 'invalid backtest status transition'); END;
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_trades (
        run_id                     TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE RESTRICT,
        trade_no                   INTEGER NOT NULL CHECK (trade_no >= 1),
        status                     TEXT NOT NULL CHECK (status IN ('CLOSED', 'OPEN_AT_END')),
        strategy_id                TEXT NOT NULL,
        strategy_version_id        TEXT NOT NULL,
        spec_hash                  TEXT NOT NULL,
        rules_hash                 TEXT NOT NULL,
        symbol                     TEXT NOT NULL,
        entry_signal_date          TEXT NOT NULL,
        entry_fill_date            TEXT NOT NULL,
        entry_open_price           REAL NOT NULL,
        entry_fill_price           REAL NOT NULL,
        exit_signal_date           TEXT,
        exit_fill_date             TEXT,
        exit_open_price            REAL,
        exit_fill_price            REAL,
        shares                     INTEGER NOT NULL CHECK (shares >= 1),
        entry_value                REAL NOT NULL,
        exit_value                 REAL,
        entry_commission           REAL NOT NULL,
        exit_commission            REAL,
        commission                 REAL NOT NULL,
        slippage_impact            REAL NOT NULL,
        pnl_dollars                REAL,
        return_pct                 REAL,
        mark_date                  TEXT,
        mark_price                 REAL,
        unrealized_pnl             REAL,
        holding_days               INTEGER NOT NULL CHECK (holding_days >= 0),
        mfe_pct                    REAL NOT NULL CHECK (mfe_pct >= 0),
        mae_pct                    REAL NOT NULL CHECK (mae_pct <= 0),
        entry_support              REAL,
        entry_resistance           REAL,
        invalidation_level         REAL,
        target_level               REAL,
        primary_exit_reason        TEXT,
        all_exit_reasons           TEXT NOT NULL,
        exit_fill_delay_sessions   INTEGER,
        market_trend_at_entry      TEXT,
        market_environment_at_entry TEXT,
        entry_feature_snapshot     TEXT NOT NULL,
        entry_evaluation_trace     TEXT NOT NULL,
        exit_feature_snapshot      TEXT,
        exit_evaluation_trace      TEXT,
        PRIMARY KEY (run_id, trade_no),
        CHECK (entry_fill_date > entry_signal_date),
        CHECK (exit_fill_date IS NULL OR exit_fill_date > exit_signal_date),
        CHECK (status != 'CLOSED' OR (exit_fill_date IS NOT NULL AND pnl_dollars IS NOT NULL AND return_pct IS NOT NULL)),
        CHECK (status != 'OPEN_AT_END' OR (exit_fill_date IS NULL AND mark_price IS NOT NULL))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_signals (
        run_id        TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE RESTRICT,
        seq           INTEGER NOT NULL CHECK (seq >= 1),
        session_date  TEXT NOT NULL,
        symbol        TEXT NOT NULL,
        event_type    TEXT NOT NULL CHECK (event_type IN ('ENTRY_SIGNAL', 'ENTRY_FILLED', 'ENTRY_SKIPPED', 'EXIT_SIGNAL',
                                                          'EXIT_FILLED', 'UNFILLED_ENTRY', 'UNFILLED_EXIT')),
        reason_code   TEXT,
        trade_no      INTEGER,
        detail_json   TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_backtest_signals_type ON backtest_signals(run_id, event_type);",
    """
    CREATE TABLE IF NOT EXISTS backtest_equity (
        run_id                     TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE RESTRICT,
        session_date               TEXT NOT NULL,
        cash                       REAL NOT NULL CHECK (cash >= -0.01),
        market_value               REAL NOT NULL,
        equity                     REAL NOT NULL,
        open_positions             INTEGER NOT NULL CHECK (open_positions >= 0),
        daily_return               REAL NOT NULL,
        drawdown                   REAL NOT NULL CHECK (drawdown <= 0),
        spy_benchmark_equity       REAL,
        universe_benchmark_equity  REAL,
        PRIMARY KEY (run_id, session_date)
    ) WITHOUT ROWID;
    """,
]
_STATEMENTS += _immutable("historical_bar_datasets", "cached bar datasets")
_STATEMENTS += _immutable("historical_daily_bars", "cached daily bars")
for _t in RESULT_TABLES:
    _STATEMENTS += _immutable(_t, "backtest results")
    _STATEMENTS.append(
        f"CREATE TRIGGER IF NOT EXISTS {_t}_only_while_running BEFORE INSERT ON {_t} "
        f"WHEN (SELECT status FROM backtest_runs WHERE run_id = NEW.run_id) IS NOT 'RUNNING' "
        f"BEGIN SELECT RAISE(ABORT, 'backtest results can only be written while the run is RUNNING'); END;")


def run_backtest_migrations(conn: sqlite3.Connection) -> None:
    run_strategy_migrations(conn)          # idempotent; backtest runs reference strategy versions
    conn.execute("PRAGMA foreign_keys = ON;")
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
