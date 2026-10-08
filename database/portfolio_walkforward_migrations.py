"""
database/portfolio_walkforward_migrations.py — Stage 4.9 WALK-FORWARD / ROBUSTNESS persistence (additive). Idempotent;
eight NEW tables only; no other table is touched and PRAGMA user_version is left alone. Research records only.

  * portfolio_walkforward_configs      one immutable walk-forward definition per hash (base config, windows spec, candidates
                                       spec, selection rule, costs, universe)
  * portfolio_walkforward_runs         one immutable run: COMPLETED or FAILED, hashes, counts, timings
  * portfolio_walkforward_windows      train / test boundaries (dates + sessions) per window
  * portfolio_walkforward_candidates   every candidate per window with its TRAIN metrics and rank
  * portfolio_walkforward_selections   the ONE frozen configuration per window and the rule that chose it
  * portfolio_walkforward_oos_results  the TEST metrics (and equity series) of the frozen configuration per window
  * portfolio_walkforward_sensitivity  parameter-neighbourhood, cost-matrix and regime rows
  * portfolio_walkforward_metrics      aggregate OOS metrics, robustness score and conventions (one JSON row per run)
"""
import sqlite3

RUN_STATUSES = ("COMPLETED", "FAILED")
FREQUENCIES = ("WEEKLY", "MONTHLY")
SELECTION_METRICS = ("SHARPE", "SORTINO", "CAGR", "DD_CONSTRAINED_SHARPE", "COMPOSITE")
SENSITIVITY_KINDS = ("PARAMETER", "COST", "REGIME")
UNIVERSE_SOURCES = ("WATCHLIST", "SAVED_SCAN", "SAVED_UNIVERSE", "CUSTOM")
BENCHMARK = "SPY"
TABLES = ("portfolio_walkforward_configs", "portfolio_walkforward_runs", "portfolio_walkforward_windows", "portfolio_walkforward_candidates",
          "portfolio_walkforward_selections", "portfolio_walkforward_oos_results", "portfolio_walkforward_sensitivity",
          "portfolio_walkforward_metrics")

_Q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
_ID = "CHECK (length({c}) = 32 AND {c} NOT GLOB '*[^0-9a-f]*')"
_H64 = "CHECK (length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*')"
_DEC = "CHECK ({c} GLOB '[0-9]*' AND {c} NOT GLOB '*[^0-9.]*' AND length({c}) <= 32)"
_JSON = "CHECK (json_valid({c}) AND length({c}) <= {n})"
_DATE = "CHECK ({c} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')"
_INT0 = "CHECK (typeof({c}) = 'integer' AND {c} >= 0)"
_INT1 = "CHECK (typeof({c}) = 'integer' AND {c} >= 1)"


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
    CREATE TABLE IF NOT EXISTS portfolio_walkforward_configs (
        wf_config_id          TEXT PRIMARY KEY {_ID.format(c="wf_config_id")},
        wf_config_hash        TEXT NOT NULL UNIQUE {_H64.format(c="wf_config_hash")},
        base_config_id        TEXT NOT NULL {_ID.format(c="base_config_id")},
        base_config_hash      TEXT NOT NULL {_H64.format(c="base_config_hash")},
        start_date            TEXT NOT NULL {_DATE.format(c="start_date")},
        end_date              TEXT NOT NULL {_DATE.format(c="end_date")},
        train_months          INTEGER NOT NULL {_INT1.format(c="train_months")},
        test_months           INTEGER NOT NULL {_INT1.format(c="test_months")},
        step_months           INTEGER NOT NULL {_INT1.format(c="step_months")},
        rebalance_frequency   TEXT NOT NULL CHECK (rebalance_frequency IN ({_Q(FREQUENCIES)})),
        selection_metric      TEXT NOT NULL CHECK (selection_metric IN ({_Q(SELECTION_METRICS)})),
        initial_cash          TEXT NOT NULL {_DEC.format(c="initial_cash")},
        transaction_cost_bps  TEXT NOT NULL {_DEC.format(c="transaction_cost_bps")},
        slippage_bps          TEXT NOT NULL {_DEC.format(c="slippage_bps")},
        benchmark             TEXT NOT NULL CHECK (benchmark = '{BENCHMARK}'),
        universe_source       TEXT NOT NULL CHECK (universe_source IN ({_Q(UNIVERSE_SOURCES)})),
        universe_ref          TEXT CHECK (universe_ref IS NULL OR length(universe_ref) <= 120),
        universe_json         TEXT NOT NULL {_JSON.format(c="universe_json", n=20000)},
        universe_hash         TEXT NOT NULL {_H64.format(c="universe_hash")},
        candidates_json       TEXT NOT NULL {_JSON.format(c="candidates_json", n=120000)},
        definition_json       TEXT NOT NULL {_JSON.format(c="definition_json", n=160000)},
        created_at            TEXT NOT NULL,
        CHECK (end_date >= start_date)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_walkforward_runs (
        run_id                TEXT PRIMARY KEY {_ID.format(c="run_id")},
        wf_config_id          TEXT NOT NULL REFERENCES portfolio_walkforward_configs(wf_config_id) {_ID.format(c="wf_config_id")},
        wf_config_hash        TEXT NOT NULL {_H64.format(c="wf_config_hash")},
        base_config_hash      TEXT NOT NULL {_H64.format(c="base_config_hash")},
        universe_hash         TEXT NOT NULL {_H64.format(c="universe_hash")},
        engine_version        TEXT NOT NULL CHECK (length(engine_version) BETWEEN 1 AND 20),
        backtest_version      TEXT NOT NULL CHECK (length(backtest_version) BETWEEN 1 AND 20),
        robustness_version    TEXT NOT NULL CHECK (length(robustness_version) BETWEEN 1 AND 20),
        status                TEXT NOT NULL CHECK (status IN ({_Q(RUN_STATUSES)})),
        failure_code          TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 40),
        failure_detail        TEXT CHECK (failure_detail IS NULL OR length(failure_detail) <= 300),
        run_at                TEXT NOT NULL,
        completed_at          TEXT NOT NULL,
        n_windows             INTEGER NOT NULL {_INT0.format(c="n_windows")},
        n_candidates          INTEGER NOT NULL {_INT0.format(c="n_candidates")},
        n_rejected            INTEGER NOT NULL {_INT0.format(c="n_rejected")},
        n_discarded           INTEGER NOT NULL {_INT0.format(c="n_discarded")},
        n_evaluations         INTEGER NOT NULL {_INT0.format(c="n_evaluations")},
        n_cache_hits          INTEGER NOT NULL {_INT0.format(c="n_cache_hits")},
        overlapping_tests     INTEGER NOT NULL CHECK (overlapping_tests IN (0, 1)),
        runtime_s             TEXT NOT NULL {_DEC.format(c="runtime_s")},
        data_hash             TEXT NOT NULL {_H64.format(c="data_hash")},
        bars_json             TEXT NOT NULL {_JSON.format(c="bars_json", n=40000)},
        result_hash           TEXT {_opt(_H64, "result_hash")},
        market_data_requests  INTEGER NOT NULL {_INT0.format(c="market_data_requests")},
        universe_note         TEXT NOT NULL CHECK (length(universe_note) BETWEEN 1 AND 300),
        CHECK (status = 'FAILED' OR (failure_code IS NULL AND result_hash IS NOT NULL)),
        CHECK (status = 'COMPLETED' OR failure_code IS NOT NULL)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pwr_run_at ON portfolio_walkforward_runs (run_at)",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_walkforward_windows (
        run_id                TEXT NOT NULL REFERENCES portfolio_walkforward_runs(run_id) {_ID.format(c="run_id")},
        window_index          INTEGER NOT NULL {_INT0.format(c="window_index")},
        window_hash           TEXT NOT NULL {_H64.format(c="window_hash")},
        train_start           TEXT NOT NULL {_DATE.format(c="train_start")},
        train_end             TEXT NOT NULL {_DATE.format(c="train_end")},
        test_start            TEXT NOT NULL {_DATE.format(c="test_start")},
        test_end              TEXT NOT NULL {_DATE.format(c="test_end")},
        train_first_session   TEXT NOT NULL {_DATE.format(c="train_first_session")},
        train_last_session    TEXT NOT NULL {_DATE.format(c="train_last_session")},
        test_first_session    TEXT NOT NULL {_DATE.format(c="test_first_session")},
        test_last_session     TEXT NOT NULL {_DATE.format(c="test_last_session")},
        n_train_sessions      INTEGER NOT NULL {_INT0.format(c="n_train_sessions")},
        n_test_sessions       INTEGER NOT NULL {_INT0.format(c="n_test_sessions")},
        PRIMARY KEY (run_id, window_index),
        CHECK (train_end < test_start AND train_last_session < test_first_session)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_walkforward_candidates (
        run_id                TEXT NOT NULL REFERENCES portfolio_walkforward_runs(run_id) {_ID.format(c="run_id")},
        window_index          INTEGER NOT NULL {_INT0.format(c="window_index")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        label                 TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
        train_rank            INTEGER NOT NULL {_INT1.format(c="train_rank")},
        train_status          TEXT NOT NULL CHECK (length(train_status) BETWEEN 1 AND 40),
        train_metric_value    REAL,
        train_metrics_json    TEXT NOT NULL {_JSON.format(c="train_metrics_json", n=20000)},
        PRIMARY KEY (run_id, window_index, config_hash)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_walkforward_selections (
        run_id                TEXT NOT NULL REFERENCES portfolio_walkforward_runs(run_id) {_ID.format(c="run_id")},
        window_index          INTEGER NOT NULL {_INT0.format(c="window_index")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        label                 TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
        selection_metric      TEXT NOT NULL CHECK (selection_metric IN ({_Q(SELECTION_METRICS)})),
        train_metric_value    REAL,
        tie_break_json        TEXT NOT NULL {_JSON.format(c="tie_break_json", n=2000)},
        config_json           TEXT NOT NULL {_JSON.format(c="config_json", n=8000)},
        frozen_at             TEXT NOT NULL,
        PRIMARY KEY (run_id, window_index)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_walkforward_oos_results (
        run_id                TEXT NOT NULL REFERENCES portfolio_walkforward_runs(run_id) {_ID.format(c="run_id")},
        window_index          INTEGER NOT NULL {_INT0.format(c="window_index")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        test_status           TEXT NOT NULL CHECK (length(test_status) BETWEEN 1 AND 40),
        test_metrics_json     TEXT NOT NULL {_JSON.format(c="test_metrics_json", n=20000)},
        equity_json           TEXT NOT NULL {_JSON.format(c="equity_json", n=400000)},
        result_hash           TEXT {_opt(_H64, "result_hash")},
        PRIMARY KEY (run_id, window_index)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_walkforward_sensitivity (
        run_id                TEXT NOT NULL REFERENCES portfolio_walkforward_runs(run_id) {_ID.format(c="run_id")},
        kind                  TEXT NOT NULL CHECK (kind IN ({_Q(SENSITIVITY_KINDS)})),
        key                   TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 120),
        payload_json          TEXT NOT NULL {_JSON.format(c="payload_json", n=40000)},
        PRIMARY KEY (run_id, kind, key)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_walkforward_metrics (
        run_id                TEXT PRIMARY KEY REFERENCES portfolio_walkforward_runs(run_id) {_ID.format(c="run_id")},
        metrics_json          TEXT NOT NULL {_JSON.format(c="metrics_json", n=60000)},
        robustness_json       TEXT NOT NULL {_JSON.format(c="robustness_json", n=8000)},
        conventions_json      TEXT NOT NULL {_JSON.format(c="conventions_json", n=12000)}
    )""",
    *_no_update_delete("portfolio_walkforward_configs", "pwc"),
    *_no_update_delete("portfolio_walkforward_runs", "pwr"),
    *_no_update_delete("portfolio_walkforward_windows", "pww"),
    *_no_update_delete("portfolio_walkforward_candidates", "pwcand"),
    *_no_update_delete("portfolio_walkforward_selections", "pwsel"),
    *_no_update_delete("portfolio_walkforward_oos_results", "pwoos"),
    *_no_update_delete("portfolio_walkforward_sensitivity", "pwsens"),
    *_no_update_delete("portfolio_walkforward_metrics", "pwm"),
]


def run_portfolio_walkforward_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
