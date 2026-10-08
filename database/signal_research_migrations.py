"""
database/signal_research_migrations.py — Stage 5.2 SIGNAL RESEARCH persistence (additive). Idempotent; eight NEW tables only; no
other table is touched and PRAGMA user_version is left alone. Research records only — nothing here is a configuration anything acts on.
"""
import sqlite3

STATUSES = ("COMPLETED", "FAILED")
BENCHMARKS = ("SPY", "EW_REBALANCED", "BUY_HOLD")
FAMILIES = ("baseline", "ablation", "control", "sector", "regime", "combined")
VERDICTS = ("FACTOR_HELPFUL", "FACTOR_NEUTRAL", "FACTOR_HARMFUL")
TABLES = ("signal_research_runs", "signal_research_variants", "signal_research_factor_ablation", "signal_research_sector_tests", "signal_research_regime_tests",
          "signal_research_combinations", "signal_research_benchmark_comparisons", "signal_research_scorecards")

_ID = "CHECK (length({c}) = 32 AND {c} NOT GLOB '*[^0-9a-f]*')"
_H64 = "CHECK (length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*')"
_JSON = "CHECK (json_valid({c}) AND length({c}) <= {n})"
_INT0 = "CHECK (typeof({c}) = 'integer' AND {c} >= 0)"
_Q = lambda xs: ", ".join(f"'{x}'" for x in xs)  # noqa: E731


def _opt(check: str, c: str, **kw) -> str:
    inner = check.format(c=c, **kw)[len("CHECK ("):-1]
    return f"CHECK ({c} IS NULL OR ({inner}))"


def _no_update_delete(table: str, prefix: str):
    return [f"""CREATE TRIGGER IF NOT EXISTS {prefix}_no_update BEFORE UPDATE ON {table}
       BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END""",
            f"""CREATE TRIGGER IF NOT EXISTS {prefix}_no_delete BEFORE DELETE ON {table}
       BEGIN SELECT RAISE(ABORT, '{table} rows are never deleted'); END"""]


_REF = "TEXT NOT NULL REFERENCES signal_research_runs(run_id) " + _ID.format(c="run_id")
_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS signal_research_runs (
        run_id                 TEXT PRIMARY KEY {_ID.format(c="run_id")},
        run_hash               TEXT NOT NULL {_H64.format(c="run_hash")},
        campaign_id            TEXT NOT NULL {_ID.format(c="campaign_id")},
        campaign_hash          TEXT NOT NULL {_H64.format(c="campaign_hash")},
        universe_hash          TEXT NOT NULL {_H64.format(c="universe_hash")},
        sector_map_hash        TEXT NOT NULL {_H64.format(c="sector_map_hash")},
        diagnostic_run_ids_json TEXT NOT NULL {_JSON.format(c="diagnostic_run_ids_json", n=4000)},
        engine_version         TEXT NOT NULL CHECK (length(engine_version) BETWEEN 1 AND 20),
        rules_version          TEXT NOT NULL CHECK (length(rules_version) BETWEEN 1 AND 20),
        flags_version          TEXT NOT NULL CHECK (length(flags_version) BETWEEN 1 AND 20),
        criteria_version       TEXT NOT NULL CHECK (length(criteria_version) BETWEEN 1 AND 20),
        status                 TEXT NOT NULL CHECK (status IN ({_Q(STATUSES)})),
        failure_code           TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 40),
        failure_detail         TEXT CHECK (failure_detail IS NULL OR length(failure_detail) <= 300),
        run_at                 TEXT NOT NULL,
        completed_at           TEXT NOT NULL,
        start_date             TEXT NOT NULL,
        end_date               TEXT NOT NULL,
        n_windows              INTEGER NOT NULL {_INT0.format(c="n_windows")},
        n_variants             INTEGER NOT NULL {_INT0.format(c="n_variants")} CHECK (n_variants <= 20),
        n_evaluations          INTEGER NOT NULL {_INT0.format(c="n_evaluations")},
        n_cache_hits           INTEGER NOT NULL {_INT0.format(c="n_cache_hits")},
        runtime_s              TEXT NOT NULL,
        data_hash              TEXT NOT NULL {_H64.format(c="data_hash")},
        result_hash            TEXT {_opt(_H64, "result_hash")},
        market_data_requests   INTEGER NOT NULL {_INT0.format(c="market_data_requests")},
        run_flags_json         TEXT NOT NULL {_JSON.format(c="run_flags_json", n=2000)},
        benchmarks_json        TEXT NOT NULL {_JSON.format(c="benchmarks_json", n=60000)},
        definition_json        TEXT NOT NULL {_JSON.format(c="definition_json", n=200000)},
        universe_note          TEXT NOT NULL CHECK (length(universe_note) BETWEEN 1 AND 300),
        CHECK (status = 'FAILED' OR (failure_code IS NULL AND result_hash IS NOT NULL)),
        CHECK (status = 'COMPLETED' OR failure_code IS NOT NULL)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_srr_run_at ON signal_research_runs (run_at)",
    "CREATE INDEX IF NOT EXISTS idx_srr_campaign ON signal_research_runs (campaign_id)",
    f"""
    CREATE TABLE IF NOT EXISTS signal_research_variants (
        run_id                 {_REF},
        config_hash            TEXT NOT NULL {_H64.format(c="config_hash")},
        label                  TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
        family                 TEXT NOT NULL CHECK (family IN ({_Q(FAMILIES)})),
        position               INTEGER NOT NULL {_INT0.format(c="position")} CHECK (position < 20),
        config_json            TEXT NOT NULL {_JSON.format(c="config_json", n=8000)},
        research_json          TEXT NOT NULL {_JSON.format(c="research_json", n=4000)},
        flags_json             TEXT NOT NULL {_JSON.format(c="flags_json", n=4000)},
        research_flags_json    TEXT NOT NULL {_JSON.format(c="research_flags_json", n=4000)},
        criteria_json          TEXT NOT NULL {_JSON.format(c="criteria_json", n=12000)},
        oos_json               TEXT NOT NULL {_JSON.format(c="oos_json", n=4000)},
        summary_json           TEXT NOT NULL {_JSON.format(c="summary_json", n=400000)},
        PRIMARY KEY (run_id, config_hash)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS signal_research_factor_ablation (
        run_id                 {_REF},
        factor                 TEXT NOT NULL CHECK (length(factor) BETWEEN 1 AND 40),
        variant_hash           TEXT NOT NULL {_H64.format(c="variant_hash")},
        verdict                TEXT NOT NULL CHECK (verdict IN ({_Q(VERDICTS)})),
        n_worse                INTEGER NOT NULL {_INT0.format(c="n_worse")},
        n_better               INTEGER NOT NULL {_INT0.format(c="n_better")},
        comparisons_json       TEXT NOT NULL {_JSON.format(c="comparisons_json", n=8000)},
        PRIMARY KEY (run_id, factor)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS signal_research_sector_tests (
        run_id                 {_REF},
        config_hash            TEXT NOT NULL {_H64.format(c="config_hash")},
        ranking                TEXT NOT NULL CHECK (length(ranking) BETWEEN 1 AND 40),
        max_sector_weight      TEXT,
        top_sector             TEXT CHECK (top_sector IS NULL OR length(top_sector) BETWEEN 1 AND 40),
        top_sector_share       REAL,
        max_sector_weight_observed REAL,
        top3_share             REAL,
        cap_changed_selection  INTEGER NOT NULL {_INT0.format(c="cap_changed_selection")},
        cap_infeasible         INTEGER NOT NULL {_INT0.format(c="cap_infeasible")},
        delta_json             TEXT NOT NULL {_JSON.format(c="delta_json", n=4000)},
        PRIMARY KEY (run_id, config_hash)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS signal_research_regime_tests (
        run_id                 {_REF},
        config_hash            TEXT NOT NULL {_H64.format(c="config_hash")},
        regime_overlay         TEXT NOT NULL CHECK (length(regime_overlay) BETWEEN 1 AND 40),
        counterpart_hash       TEXT {_opt(_H64, "counterpart_hash")},
        mean_exposure          REAL,
        exposure_json          TEXT NOT NULL {_JSON.format(c="exposure_json", n=4000)},
        delta_json             TEXT NOT NULL {_JSON.format(c="delta_json", n=4000)},
        PRIMARY KEY (run_id, config_hash)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS signal_research_combinations (
        run_id                 {_REF},
        config_hash            TEXT NOT NULL {_H64.format(c="config_hash")},
        components_json        TEXT NOT NULL {_JSON.format(c="components_json", n=2000)},
        criteria_met           INTEGER NOT NULL CHECK (criteria_met IN (0, 1)),
        delta_json             TEXT NOT NULL {_JSON.format(c="delta_json", n=4000)},
        PRIMARY KEY (run_id, config_hash)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS signal_research_benchmark_comparisons (
        run_id                 {_REF},
        config_hash            TEXT NOT NULL {_H64.format(c="config_hash")},
        benchmark              TEXT NOT NULL CHECK (benchmark IN ({_Q(BENCHMARKS)})),
        transaction_cost_bps   TEXT NOT NULL,
        slippage_bps           TEXT NOT NULL,
        strategy_total_return  REAL,
        benchmark_total_return REAL,
        excess                 REAL,
        strategy_sharpe        REAL,
        benchmark_sharpe       REAL,
        pct_windows_beating    REAL,
        median_excess          REAL,
        PRIMARY KEY (run_id, config_hash, benchmark, transaction_cost_bps, slippage_bps)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS signal_research_scorecards (
        run_id                 {_REF},
        config_hash            TEXT NOT NULL {_H64.format(c="config_hash")},
        label                  TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
        family                 TEXT NOT NULL CHECK (family IN ({_Q(FAMILIES)})),
        scorecard_json         TEXT NOT NULL {_JSON.format(c="scorecard_json", n=12000)},
        criteria_json          TEXT NOT NULL {_JSON.format(c="criteria_json", n=12000)},
        research_flags_json    TEXT NOT NULL {_JSON.format(c="research_flags_json", n=4000)},
        PRIMARY KEY (run_id, config_hash)
    )""",
    *_no_update_delete("signal_research_runs", "srr"),
    *_no_update_delete("signal_research_variants", "srv"),
    *_no_update_delete("signal_research_factor_ablation", "srfa"),
    *_no_update_delete("signal_research_sector_tests", "srst"),
    *_no_update_delete("signal_research_regime_tests", "srrt"),
    *_no_update_delete("signal_research_combinations", "srco"),
    *_no_update_delete("signal_research_benchmark_comparisons", "srbc"),
    *_no_update_delete("signal_research_scorecards", "srsc"),
]


def run_signal_research_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
