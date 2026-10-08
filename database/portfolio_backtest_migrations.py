"""
database/portfolio_backtest_migrations.py — Stage 4.8 HISTORICAL ROTATION BACKTEST persistence (additive). Idempotent;
safe on any database state. Six NEW tables only; no other table is touched and PRAGMA user_version is left alone (like
every other Stage 3 / 4 migration). Research records only: a historical SIMULATION of the Stage 4.7 rotation model —
no broker order, no credential, no account identifier is ever stored here.

Created by the first store write; reads never create these tables. Ids are TEXT 32-hex (uuid4 hex), hashes 64-hex sha256,
decimals canonical strings, JSON columns canonical JSON. Immutability: no UPDATE and no DELETE on any of the six tables.

  * portfolio_backtest_configs     one immutable backtest definition per backtest_config_hash (rotation config version,
                                   dates, frequency, cash, cost assumptions, execution convention, resolved universe)
  * portfolio_backtest_runs        one immutable run: COMPLETED (results stored) or FAILED (fail-closed reason stored)
  * portfolio_backtest_rebalances  one row per scheduled signal session: engine status, execution session, turnover, costs
  * portfolio_backtest_trades      every simulated fill (whole shares, next-session open ± slippage, cost debited)
  * portfolio_backtest_equity      one row per session: cash, positions value, equity, benchmark index, cash weight
  * portfolio_backtest_metrics     one JSON row per run with the documented metric conventions
"""
import sqlite3

FREQUENCIES = ("WEEKLY", "MONTHLY")
RUN_STATUSES = ("COMPLETED", "FAILED")
UNIVERSE_SOURCES = ("WATCHLIST", "SAVED_SCAN", "SAVED_UNIVERSE", "CUSTOM")
SIDES = ("BUY", "SELL")
ACTIONS = ("ADD", "INCREASE", "DECREASE", "EXIT")
BENCHMARK = "SPY"
EXECUTION_PRICE = "NEXT_OPEN"
TABLES = ("portfolio_backtest_configs", "portfolio_backtest_runs", "portfolio_backtest_rebalances", "portfolio_backtest_trades",
          "portfolio_backtest_equity", "portfolio_backtest_metrics")

_Q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
_ID = "CHECK (length({c}) = 32 AND {c} NOT GLOB '*[^0-9a-f]*')"
_H64 = "CHECK (length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*')"
_DEC = "CHECK ({c} GLOB '[0-9]*' AND {c} NOT GLOB '*[^0-9.]*' AND length({c}) <= 32)"               # unsigned decimal
_SDEC = "CHECK (({c} GLOB '[0-9]*' OR {c} GLOB '-[0-9]*') AND {c} NOT GLOB '*[^0-9.-]*' AND length({c}) <= 33)"  # signed
_JSON = "CHECK (json_valid({c}) AND length({c}) <= {n})"
_SYM = "CHECK (length({c}) BETWEEN 1 AND 8 AND {c} NOT GLOB '*[^A-Z.]*')"
_DATE = "CHECK ({c} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')"
_INT0 = "CHECK (typeof({c}) = 'integer' AND {c} >= 0)"


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
    CREATE TABLE IF NOT EXISTS portfolio_backtest_configs (
        backtest_config_id    TEXT PRIMARY KEY {_ID.format(c="backtest_config_id")},
        backtest_config_hash  TEXT NOT NULL UNIQUE {_H64.format(c="backtest_config_hash")},
        config_id             TEXT NOT NULL {_ID.format(c="config_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        start_date            TEXT NOT NULL {_DATE.format(c="start_date")},
        end_date              TEXT NOT NULL {_DATE.format(c="end_date")},
        rebalance_frequency   TEXT NOT NULL CHECK (rebalance_frequency IN ({_Q(FREQUENCIES)})),
        initial_cash          TEXT NOT NULL {_DEC.format(c="initial_cash")},
        transaction_cost_bps  TEXT NOT NULL {_DEC.format(c="transaction_cost_bps")},
        slippage_bps          TEXT NOT NULL {_DEC.format(c="slippage_bps")},
        benchmark             TEXT NOT NULL CHECK (benchmark = '{BENCHMARK}'),
        execution_price       TEXT NOT NULL CHECK (execution_price = '{EXECUTION_PRICE}'),
        universe_source       TEXT NOT NULL CHECK (universe_source IN ({_Q(UNIVERSE_SOURCES)})),
        universe_ref          TEXT CHECK (universe_ref IS NULL OR length(universe_ref) <= 120),
        universe_json         TEXT NOT NULL {_JSON.format(c="universe_json", n=20000)},
        universe_hash         TEXT NOT NULL {_H64.format(c="universe_hash")},
        definition_json       TEXT NOT NULL {_JSON.format(c="definition_json", n=30000)},
        created_at            TEXT NOT NULL,
        CHECK (end_date >= start_date)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_backtest_runs (
        run_id                TEXT PRIMARY KEY {_ID.format(c="run_id")},
        backtest_config_id    TEXT NOT NULL REFERENCES portfolio_backtest_configs(backtest_config_id) {_ID.format(c="backtest_config_id")},
        backtest_config_hash  TEXT NOT NULL {_H64.format(c="backtest_config_hash")},
        config_id             TEXT NOT NULL {_ID.format(c="config_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        universe_hash         TEXT NOT NULL {_H64.format(c="universe_hash")},
        engine_version        TEXT NOT NULL CHECK (length(engine_version) BETWEEN 1 AND 20),
        status                TEXT NOT NULL CHECK (status IN ({_Q(RUN_STATUSES)})),
        failure_code          TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 40),
        failure_detail        TEXT CHECK (failure_detail IS NULL OR length(failure_detail) <= 300),
        run_at                TEXT NOT NULL,
        completed_at          TEXT NOT NULL,
        first_session         TEXT {_opt(_DATE, "first_session")},
        last_session          TEXT {_opt(_DATE, "last_session")},
        n_sessions            INTEGER NOT NULL {_INT0.format(c="n_sessions")},
        n_rebalances          INTEGER NOT NULL {_INT0.format(c="n_rebalances")},
        n_rebalances_executed INTEGER NOT NULL {_INT0.format(c="n_rebalances_executed")},
        n_trades              INTEGER NOT NULL {_INT0.format(c="n_trades")},
        initial_cash          TEXT NOT NULL {_DEC.format(c="initial_cash")},
        final_equity          TEXT {_opt(_DEC, "final_equity")},
        final_cash            TEXT {_opt(_DEC, "final_cash")},
        total_costs           TEXT {_opt(_DEC, "total_costs")},
        data_hash             TEXT NOT NULL {_H64.format(c="data_hash")},
        bars_json             TEXT NOT NULL {_JSON.format(c="bars_json", n=40000)},
        result_hash           TEXT {_opt(_H64, "result_hash")},
        market_data_requests  INTEGER NOT NULL {_INT0.format(c="market_data_requests")},
        universe_note         TEXT NOT NULL CHECK (length(universe_note) BETWEEN 1 AND 300),
        CHECK (status = 'FAILED' OR (failure_code IS NULL AND result_hash IS NOT NULL AND final_equity IS NOT NULL)),
        CHECK (status = 'COMPLETED' OR failure_code IS NOT NULL)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pbr_config ON portfolio_backtest_runs (backtest_config_id)",
    "CREATE INDEX IF NOT EXISTS idx_pbr_run_at ON portfolio_backtest_runs (run_at)",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_backtest_rebalances (
        run_id                TEXT NOT NULL REFERENCES portfolio_backtest_runs(run_id) {_ID.format(c="run_id")},
        seq                   INTEGER NOT NULL CHECK (typeof(seq) = 'integer' AND seq >= 1),
        signal_session        TEXT NOT NULL {_DATE.format(c="signal_session")},
        execution_session     TEXT {_opt(_DATE, "execution_session")},
        engine_status         TEXT NOT NULL CHECK (length(engine_status) BETWEEN 1 AND 40),
        status_detail         TEXT CHECK (status_detail IS NULL OR length(status_detail) <= 300),
        executed              INTEGER NOT NULL CHECK (executed IN (0, 1)),
        skip_reason           TEXT CHECK (skip_reason IS NULL OR length(skip_reason) <= 40),
        reference_equity      TEXT {_opt(_DEC, "reference_equity")},
        cash_before           TEXT NOT NULL {_DEC.format(c="cash_before")},
        turnover              TEXT {_opt(_DEC, "turnover")},
        n_eligible            INTEGER NOT NULL {_INT0.format(c="n_eligible")},
        n_selected            INTEGER NOT NULL {_INT0.format(c="n_selected")},
        n_orders              INTEGER NOT NULL {_INT0.format(c="n_orders")},
        n_fills               INTEGER NOT NULL {_INT0.format(c="n_fills")},
        traded_notional       TEXT NOT NULL {_DEC.format(c="traded_notional")},
        total_cost            TEXT NOT NULL {_DEC.format(c="total_cost")},
        input_hash            TEXT NOT NULL {_H64.format(c="input_hash")},
        proposal_hash         TEXT NOT NULL {_H64.format(c="proposal_hash")},
        proposal_json         TEXT NOT NULL {_JSON.format(c="proposal_json", n=60000)},
        PRIMARY KEY (run_id, seq),
        UNIQUE (run_id, signal_session)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_backtest_trades (
        trade_id              TEXT PRIMARY KEY {_ID.format(c="trade_id")},
        run_id                TEXT NOT NULL REFERENCES portfolio_backtest_runs(run_id) {_ID.format(c="run_id")},
        seq                   INTEGER NOT NULL CHECK (typeof(seq) = 'integer' AND seq >= 1),
        order_index           INTEGER NOT NULL {_INT0.format(c="order_index")},
        signal_session        TEXT NOT NULL {_DATE.format(c="signal_session")},
        execution_session     TEXT NOT NULL {_DATE.format(c="execution_session")},
        symbol                TEXT NOT NULL {_SYM.format(c="symbol")},
        side                  TEXT NOT NULL CHECK (side IN ({_Q(SIDES)})),
        action                TEXT NOT NULL CHECK (action IN ({_Q(ACTIONS)})),
        rank                  INTEGER CHECK (rank IS NULL OR (typeof(rank) = 'integer' AND rank >= 1)),
        requested_qty         INTEGER NOT NULL {_INT0.format(c="requested_qty")},
        filled_qty            INTEGER NOT NULL {_INT0.format(c="filled_qty")},
        open_price            TEXT NOT NULL {_DEC.format(c="open_price")},
        fill_price            TEXT NOT NULL {_DEC.format(c="fill_price")},
        notional              TEXT NOT NULL {_DEC.format(c="notional")},
        cost                  TEXT NOT NULL {_DEC.format(c="cost")},
        cash_after            TEXT NOT NULL {_DEC.format(c="cash_after")},
        qty_after             INTEGER NOT NULL {_INT0.format(c="qty_after")},
        realised_pnl          TEXT {_opt(_SDEC, "realised_pnl")},
        note                  TEXT CHECK (note IS NULL OR length(note) <= 40),
        UNIQUE (run_id, seq, order_index),
        CHECK (filled_qty <= requested_qty)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pbt_run ON portfolio_backtest_trades (run_id)",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_backtest_equity (
        run_id                TEXT NOT NULL REFERENCES portfolio_backtest_runs(run_id) {_ID.format(c="run_id")},
        session_date          TEXT NOT NULL {_DATE.format(c="session_date")},
        cash                  TEXT NOT NULL {_DEC.format(c="cash")},
        positions_value       TEXT NOT NULL {_DEC.format(c="positions_value")},
        equity                TEXT NOT NULL {_DEC.format(c="equity")},
        benchmark_index       TEXT NOT NULL {_DEC.format(c="benchmark_index")},
        n_positions           INTEGER NOT NULL {_INT0.format(c="n_positions")},
        cash_weight           TEXT NOT NULL {_DEC.format(c="cash_weight")},
        PRIMARY KEY (run_id, session_date)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_backtest_metrics (
        run_id                TEXT PRIMARY KEY REFERENCES portfolio_backtest_runs(run_id) {_ID.format(c="run_id")},
        metrics_json          TEXT NOT NULL {_JSON.format(c="metrics_json", n=20000)},
        conventions_json      TEXT NOT NULL {_JSON.format(c="conventions_json", n=8000)}
    )""",
    *_no_update_delete("portfolio_backtest_configs", "pbc"),
    *_no_update_delete("portfolio_backtest_runs", "pbr"),
    *_no_update_delete("portfolio_backtest_rebalances", "pbreb"),
    *_no_update_delete("portfolio_backtest_trades", "pbt"),
    *_no_update_delete("portfolio_backtest_equity", "pbe"),
    *_no_update_delete("portfolio_backtest_metrics", "pbm"),
]


def run_portfolio_backtest_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
