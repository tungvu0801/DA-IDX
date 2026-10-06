"""
database/rotation_migrations.py — Stage 4.7 PORTFOLIO ROTATION persistence (additive). Idempotent; safe on any database
state. Only five NEW tables; no Stage 4.5 paper_* table, no Stage 4.6B alpaca_paper_order_* table and no other table is
touched, and PRAGMA user_version is left alone (like every other Stage 3 / 4 migration). Nothing here stores a credential,
an account number or an order: the tables hold immutable configuration versions and immutable rotation RUNS — portfolio
PROPOSALS (ADD / INCREASE / DECREASE / EXIT / HOLD / NONE), never broker orders.

Created by the first store write (a config or a run); reads never create these tables. Ids are TEXT 32-hex (uuid4 hex,
the Stage 4.1 / 4.5 convention); hashes are 64-hex sha256; decimals are canonical strings; JSON columns are canonical
JSON. Immutability is enforced by triggers: no UPDATE and no DELETE on any of the five tables.

  * portfolio_rotation_configs     one immutable configuration VERSION per row (several versions per name; there is no
                                   mutable "active" configuration — a run references config_id + config_hash)
  * portfolio_rotation_runs        one immutable completed run (VALID or a persisted runtime failure)
  * portfolio_rotation_candidates  every evaluated symbol of a run: eligibility, reasons, raw factors, scores, rank
  * portfolio_rotation_targets     the selected symbols with their static equal weights
  * portfolio_rebalance_items      the proposal per symbol: current / target weight, estimated whole-share difference,
                                   action — informational, never executable
"""
import sqlite3

PORTFOLIO_SOURCES = ("ALPACA_PAPER_VIEW", "ROBINHOOD_READ_ONLY", "LOCAL_SIMULATOR")
UNIVERSE_SOURCES = ("WATCHLIST", "SAVED_SCAN", "SAVED_UNIVERSE", "CUSTOM")
RUN_STATUSES = ("VALID", "NO_ELIGIBLE_CANDIDATES", "INSUFFICIENT_CANDIDATES", "TURNOVER_LIMIT_EXCEEDED", "DATA_STALE",
                "INPUT_ERROR")
SNAPSHOT_STATUSES = ("OK", "STALE")
TARGET_REASONS = ("TOP_N", "RETAINED_RANK_BUFFER")
ACTIONS = ("ADD", "INCREASE", "DECREASE", "EXIT", "HOLD", "NONE")
SIDE_HINTS = ("BUY", "SELL")
BENCHMARK = "SPY"
TABLES = ("portfolio_rotation_configs", "portfolio_rotation_runs", "portfolio_rotation_candidates",
          "portfolio_rotation_targets", "portfolio_rebalance_items")

_Q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731
_ID = "CHECK (length({c}) = 32 AND {c} NOT GLOB '*[^0-9a-f]*')"
_H64 = "CHECK (length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*')"
_DEC = "CHECK ({c} GLOB '[0-9]*' AND {c} NOT GLOB '*[^0-9.]*' AND length({c}) <= 32)"               # unsigned decimal
_SDEC = "CHECK (({c} GLOB '[0-9]*' OR {c} GLOB '-[0-9]*') AND {c} NOT GLOB '*[^0-9.-]*' AND length({c}) <= 33)"  # signed
_JSON = "CHECK (json_valid({c}) AND length({c}) <= {n})"
_SYM = "CHECK (length({c}) BETWEEN 1 AND 8 AND {c} NOT GLOB '*[^A-Z.]*')"
_DATE = "CHECK ({c} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')"


def _opt(check: str, c: str, **kw) -> str:
    """A CHECK that also allows NULL."""
    inner = check.format(c=c, **kw)[len("CHECK ("):-1]
    return f"CHECK ({c} IS NULL OR ({inner}))"


def _no_update_delete(table: str, prefix: str):
    return [f"""CREATE TRIGGER IF NOT EXISTS {prefix}_no_update BEFORE UPDATE ON {table}
       BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS {prefix}_no_delete BEFORE DELETE ON {table}
       BEGIN SELECT RAISE(ABORT, '{table} rows are never deleted'); END"""]


_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_rotation_configs (
        config_id             TEXT PRIMARY KEY {_ID.format(c="config_id")},
        name                  TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
        version               INTEGER NOT NULL CHECK (typeof(version) = 'integer' AND version >= 1),
        config_json           TEXT NOT NULL {_JSON.format(c="config_json", n=8000)},
        config_hash           TEXT NOT NULL UNIQUE {_H64.format(c="config_hash")},
        benchmark             TEXT NOT NULL CHECK (benchmark = '{BENCHMARK}'),
        created_at            TEXT NOT NULL,
        UNIQUE (name, version)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_rotation_runs (
        run_id                TEXT PRIMARY KEY {_ID.format(c="run_id")},
        config_id             TEXT NOT NULL REFERENCES portfolio_rotation_configs(config_id) {_ID.format(c="config_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        run_at                TEXT NOT NULL,
        data_session          TEXT {_opt(_DATE, "data_session")},
        benchmark             TEXT NOT NULL CHECK (benchmark = '{BENCHMARK}'),
        universe_source       TEXT NOT NULL CHECK (universe_source IN ({_Q(UNIVERSE_SOURCES)})),
        universe_ref          TEXT CHECK (universe_ref IS NULL OR length(universe_ref) <= 120),
        universe_json         TEXT NOT NULL {_JSON.format(c="universe_json", n=20000)},
        universe_hash         TEXT NOT NULL {_H64.format(c="universe_hash")},
        portfolio_source      TEXT NOT NULL CHECK (portfolio_source IN ({_Q(PORTFOLIO_SOURCES)})),
        portfolio_snapshot_at TEXT,
        snapshot_status       TEXT CHECK (snapshot_status IS NULL OR snapshot_status IN ({_Q(SNAPSHOT_STATUSES)})),
        snapshot_cash         TEXT {_opt(_DEC, "snapshot_cash")},
        positions_json        TEXT {_opt(_JSON, "positions_json", n=40000)},
        source_meta_json      TEXT {_opt(_JSON, "source_meta_json", n=20000)},
        source_mismatch_note  TEXT CHECK (source_mismatch_note IS NULL OR length(source_mismatch_note) <= 300),
        reference_equity      TEXT {_opt(_DEC, "reference_equity")},
        current_cash_weight   TEXT {_opt(_DEC, "current_cash_weight")},
        target_cash_weight    TEXT {_opt(_DEC, "target_cash_weight")},
        input_hash            TEXT NOT NULL {_H64.format(c="input_hash")},
        proposal_hash         TEXT NOT NULL {_H64.format(c="proposal_hash")},
        status                TEXT NOT NULL CHECK (status IN ({_Q(RUN_STATUSES)})),
        status_detail         TEXT CHECK (status_detail IS NULL OR length(status_detail) <= 300),
        n_universe            INTEGER NOT NULL CHECK (typeof(n_universe) = 'integer' AND n_universe >= 0),
        n_eligible            INTEGER NOT NULL CHECK (typeof(n_eligible) = 'integer' AND n_eligible >= 0),
        n_selected            INTEGER NOT NULL CHECK (typeof(n_selected) = 'integer' AND n_selected >= 0),
        turnover              TEXT {_opt(_DEC, "turnover")},
        market_data_requests  INTEGER NOT NULL CHECK (typeof(market_data_requests) = 'integer' AND market_data_requests >= 0),
        completed_at          TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_prr_config ON portfolio_rotation_runs (config_id)",
    "CREATE INDEX IF NOT EXISTS idx_prr_run_at ON portfolio_rotation_runs (run_at)",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_rotation_candidates (
        run_id                TEXT NOT NULL REFERENCES portfolio_rotation_runs(run_id) {_ID.format(c="run_id")},
        symbol                TEXT NOT NULL {_SYM.format(c="symbol")},
        eligible              INTEGER NOT NULL CHECK (eligible IN (0, 1)),
        reasons_json          TEXT NOT NULL {_JSON.format(c="reasons_json", n=2000)},
        raw_json              TEXT NOT NULL {_JSON.format(c="raw_json", n=2000)},
        scores_json           TEXT {_opt(_JSON, "scores_json", n=2000)},
        composite             TEXT {_opt(_DEC, "composite")},
        rank                  INTEGER CHECK (rank IS NULL OR (typeof(rank) = 'integer' AND rank >= 1)),
        reference_price       TEXT {_opt(_DEC, "reference_price")},
        avg_dollar_volume     TEXT {_opt(_DEC, "avg_dollar_volume")},
        flags_json            TEXT NOT NULL {_JSON.format(c="flags_json", n=2000)},
        PRIMARY KEY (run_id, symbol),
        CHECK (eligible = 1 OR (rank IS NULL AND composite IS NULL))
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_rotation_targets (
        run_id                TEXT NOT NULL REFERENCES portfolio_rotation_runs(run_id) {_ID.format(c="run_id")},
        symbol                TEXT NOT NULL {_SYM.format(c="symbol")},
        rank                  INTEGER NOT NULL CHECK (typeof(rank) = 'integer' AND rank >= 1),
        target_weight         TEXT NOT NULL {_DEC.format(c="target_weight")},
        reference_price       TEXT NOT NULL {_DEC.format(c="reference_price")},
        target_notional       TEXT NOT NULL {_DEC.format(c="target_notional")},
        est_target_qty        INTEGER NOT NULL CHECK (typeof(est_target_qty) = 'integer' AND est_target_qty >= 0),
        reason                TEXT NOT NULL CHECK (reason IN ({_Q(TARGET_REASONS)})),
        flags_json            TEXT NOT NULL {_JSON.format(c="flags_json", n=2000)},
        PRIMARY KEY (run_id, symbol)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS portfolio_rebalance_items (
        item_id               TEXT PRIMARY KEY {_ID.format(c="item_id")},
        run_id                TEXT NOT NULL REFERENCES portfolio_rotation_runs(run_id) {_ID.format(c="run_id")},
        symbol                TEXT NOT NULL {_SYM.format(c="symbol")},
        current_qty           TEXT NOT NULL {_DEC.format(c="current_qty")},
        current_weight        TEXT NOT NULL {_DEC.format(c="current_weight")},
        target_weight         TEXT NOT NULL {_DEC.format(c="target_weight")},
        weight_diff           TEXT NOT NULL {_SDEC.format(c="weight_diff")},
        est_qty_diff          INTEGER NOT NULL CHECK (typeof(est_qty_diff) = 'integer' AND est_qty_diff >= 0),
        side_hint             TEXT CHECK (side_hint IS NULL OR side_hint IN ({_Q(SIDE_HINTS)})),
        action                TEXT NOT NULL CHECK (action IN ({_Q(ACTIONS)})),
        reason                TEXT CHECK (reason IS NULL OR length(reason) <= 60),
        handoff_allowed       INTEGER NOT NULL CHECK (handoff_allowed IN (0, 1)),
        UNIQUE (run_id, symbol)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pri_run ON portfolio_rebalance_items (run_id)",
    *_no_update_delete("portfolio_rotation_configs", "prc"),
    *_no_update_delete("portfolio_rotation_runs", "prr"),
    *_no_update_delete("portfolio_rotation_candidates", "prcand"),
    *_no_update_delete("portfolio_rotation_targets", "prt"),
    *_no_update_delete("portfolio_rebalance_items", "pri"),
]


def run_rotation_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
