"""
database/attribution_diagnostic_migrations.py — Stage 5.1 ATTRIBUTION / BENCHMARK DIAGNOSTICS persistence (additive). Idempotent;
eight NEW tables only; no other table is touched and PRAGMA user_version is left alone. Research records only.
"""
import sqlite3

STATUSES = ("COMPLETED", "FAILED")
BENCHMARKS = ("SPY", "EW_REBALANCED", "BUY_HOLD")
VERDICTS = ("PASS", "WARN", "FAIL")
TABLES = ("rotation_diagnostic_runs", "rotation_diagnostic_benchmarks", "rotation_diagnostic_symbol_attribution", "rotation_diagnostic_sector_attribution",
          "rotation_diagnostic_leave_one_out", "rotation_diagnostic_leave_sector_out", "rotation_diagnostic_windows", "rotation_diagnostic_scorecards")

_ID = "CHECK (length({c}) = 32 AND {c} NOT GLOB '*[^0-9a-f]*')"
_H64 = "CHECK (length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*')"
_JSON = "CHECK (json_valid({c}) AND length({c}) <= {n})"
_INT0 = "CHECK (typeof({c}) = 'integer' AND {c} >= 0)"
_SYM = "CHECK (length({c}) BETWEEN 1 AND 8 AND {c} NOT GLOB '*[^A-Z.]*')"
_Q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731


def _opt(check: str, c: str, **kw) -> str:
    inner = check.format(c=c, **kw)[len("CHECK ("):-1]
    return f"CHECK ({c} IS NULL OR ({inner}))"


def _no_update_delete(table: str, prefix: str):
    return [f"""CREATE TRIGGER IF NOT EXISTS {prefix}_no_update BEFORE UPDATE ON {table}
       BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS {prefix}_no_delete BEFORE DELETE ON {table}
       BEGIN SELECT RAISE(ABORT, '{table} rows are never deleted'); END"""]


_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS rotation_diagnostic_runs (
        diag_id               TEXT PRIMARY KEY {_ID.format(c="diag_id")},
        diag_hash             TEXT NOT NULL {_H64.format(c="diag_hash")},
        campaign_id           TEXT NOT NULL {_ID.format(c="campaign_id")},
        campaign_hash         TEXT NOT NULL {_H64.format(c="campaign_hash")},
        universe_hash         TEXT NOT NULL {_H64.format(c="universe_hash")},
        sector_map_hash       TEXT NOT NULL {_H64.format(c="sector_map_hash")},
        engine_version        TEXT NOT NULL CHECK (length(engine_version) BETWEEN 1 AND 20),
        flags_version         TEXT NOT NULL CHECK (length(flags_version) BETWEEN 1 AND 20),
        status                TEXT NOT NULL CHECK (status IN ({_Q(STATUSES)})),
        failure_code          TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 40),
        failure_detail        TEXT CHECK (failure_detail IS NULL OR length(failure_detail) <= 300),
        run_at                TEXT NOT NULL,
        completed_at          TEXT NOT NULL,
        start_date            TEXT NOT NULL,
        end_date              TEXT NOT NULL,
        n_windows             INTEGER NOT NULL {_INT0.format(c="n_windows")},
        n_configs             INTEGER NOT NULL {_INT0.format(c="n_configs")},
        n_evaluations         INTEGER NOT NULL {_INT0.format(c="n_evaluations")},
        n_cache_hits          INTEGER NOT NULL {_INT0.format(c="n_cache_hits")},
        runtime_s             TEXT NOT NULL,
        data_hash             TEXT NOT NULL {_H64.format(c="data_hash")},
        result_hash           TEXT {_opt(_H64, "result_hash")},
        market_data_requests  INTEGER NOT NULL {_INT0.format(c="market_data_requests")},
        definition_json       TEXT NOT NULL {_JSON.format(c="definition_json", n=120000)},
        universe_note         TEXT NOT NULL CHECK (length(universe_note) BETWEEN 1 AND 300),
        CHECK (status = 'FAILED' OR (failure_code IS NULL AND result_hash IS NOT NULL)),
        CHECK (status = 'COMPLETED' OR failure_code IS NOT NULL)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rdr_run_at ON rotation_diagnostic_runs (run_at)",
    f"""
    CREATE TABLE IF NOT EXISTS rotation_diagnostic_benchmarks (
        diag_id               TEXT NOT NULL REFERENCES rotation_diagnostic_runs(diag_id) {_ID.format(c="diag_id")},
        benchmark             TEXT NOT NULL CHECK (benchmark IN ({_Q(BENCHMARKS)})),
        transaction_cost_bps  TEXT NOT NULL,
        slippage_bps          TEXT NOT NULL,
        definition_json       TEXT NOT NULL {_JSON.format(c="definition_json", n=8000)},
        metrics_json          TEXT NOT NULL {_JSON.format(c="metrics_json", n=20000)},
        windows_json          TEXT NOT NULL {_JSON.format(c="windows_json", n=40000)},
        PRIMARY KEY (diag_id, benchmark, transaction_cost_bps, slippage_bps)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS rotation_diagnostic_symbol_attribution (
        diag_id               TEXT NOT NULL REFERENCES rotation_diagnostic_runs(diag_id) {_ID.format(c="diag_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        symbol                TEXT NOT NULL {_SYM.format(c="symbol")},
        sector                TEXT NOT NULL CHECK (length(sector) BETWEEN 1 AND 40),
        pnl                   TEXT NOT NULL,
        share_of_positive     REAL,
        windows_held          INTEGER NOT NULL {_INT0.format(c="windows_held")},
        PRIMARY KEY (diag_id, config_hash, symbol)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS rotation_diagnostic_sector_attribution (
        diag_id               TEXT NOT NULL REFERENCES rotation_diagnostic_runs(diag_id) {_ID.format(c="diag_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        sector                TEXT NOT NULL CHECK (length(sector) BETWEEN 1 AND 40),
        pnl                   TEXT NOT NULL,
        share_of_positive     REAL,
        avg_weight            REAL,
        max_weight            REAL,
        PRIMARY KEY (diag_id, config_hash, sector)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS rotation_diagnostic_leave_one_out (
        diag_id               TEXT NOT NULL REFERENCES rotation_diagnostic_runs(diag_id) {_ID.format(c="diag_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        symbol                TEXT NOT NULL {_SYM.format(c="symbol")},
        status                TEXT NOT NULL CHECK (length(status) BETWEEN 1 AND 40),
        deltas_json           TEXT NOT NULL {_JSON.format(c="deltas_json", n=4000)},
        dominant              INTEGER NOT NULL CHECK (dominant IN (0, 1)),
        PRIMARY KEY (diag_id, config_hash, symbol)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS rotation_diagnostic_leave_sector_out (
        diag_id               TEXT NOT NULL REFERENCES rotation_diagnostic_runs(diag_id) {_ID.format(c="diag_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        sector                TEXT NOT NULL CHECK (length(sector) BETWEEN 1 AND 40),
        status                TEXT NOT NULL CHECK (length(status) BETWEEN 1 AND 40),
        deltas_json           TEXT NOT NULL {_JSON.format(c="deltas_json", n=4000)},
        dependent             INTEGER NOT NULL CHECK (dependent IN (0, 1)),
        PRIMARY KEY (diag_id, config_hash, sector)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS rotation_diagnostic_windows (
        diag_id               TEXT NOT NULL REFERENCES rotation_diagnostic_runs(diag_id) {_ID.format(c="diag_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        window_index          INTEGER NOT NULL {_INT0.format(c="window_index")},
        row_json              TEXT NOT NULL {_JSON.format(c="row_json", n=8000)},
        PRIMARY KEY (diag_id, config_hash, window_index)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS rotation_diagnostic_scorecards (
        diag_id               TEXT NOT NULL REFERENCES rotation_diagnostic_runs(diag_id) {_ID.format(c="diag_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        label                 TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
        role                  TEXT NOT NULL CHECK (length(role) BETWEEN 1 AND 40),
        flags_json            TEXT NOT NULL {_JSON.format(c="flags_json", n=4000)},
        scorecard_json        TEXT NOT NULL {_JSON.format(c="scorecard_json", n=12000)},
        summary_json          TEXT NOT NULL {_JSON.format(c="summary_json", n=120000)},
        PRIMARY KEY (diag_id, config_hash)
    )""",
    *_no_update_delete("rotation_diagnostic_runs", "rdr"),
    *_no_update_delete("rotation_diagnostic_benchmarks", "rdb"),
    *_no_update_delete("rotation_diagnostic_symbol_attribution", "rdsa"),
    *_no_update_delete("rotation_diagnostic_sector_attribution", "rdse"),
    *_no_update_delete("rotation_diagnostic_leave_one_out", "rdloo"),
    *_no_update_delete("rotation_diagnostic_leave_sector_out", "rdlso"),
    *_no_update_delete("rotation_diagnostic_windows", "rdw"),
    *_no_update_delete("rotation_diagnostic_scorecards", "rdsc"),
]


def run_attribution_diagnostic_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
