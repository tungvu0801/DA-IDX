"""
database/model_campaign_migrations.py — Stage 5.0 MODEL EVALUATION CAMPAIGN persistence (additive). Idempotent; six NEW
tables only; no other table is touched and PRAGMA user_version is left alone. Research records only: leaderboards and
"paper-forward-test candidates" — never an order, a deployment or an active configuration.
"""
import sqlite3

STATUSES = ("COMPLETED", "NO_FINALIST", "FAILED")
TABLES = ("model_campaigns", "model_campaign_candidates", "model_campaign_leaderboard", "model_campaign_finalists",
          "model_campaign_model_cards", "model_campaign_metrics")
FINALIST_LABEL = "paper-forward-test candidate"

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


_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS model_campaigns (
        campaign_id           TEXT PRIMARY KEY {_ID.format(c="campaign_id")},
        campaign_hash         TEXT NOT NULL {_H64.format(c="campaign_hash")},
        base_config_hash      TEXT NOT NULL {_H64.format(c="base_config_hash")},
        universe_hash         TEXT NOT NULL {_H64.format(c="universe_hash")},
        engine_version        TEXT NOT NULL CHECK (length(engine_version) BETWEEN 1 AND 20),
        walkforward_version   TEXT NOT NULL CHECK (length(walkforward_version) BETWEEN 1 AND 20),
        backtest_version      TEXT NOT NULL CHECK (length(backtest_version) BETWEEN 1 AND 20),
        robustness_version    TEXT NOT NULL CHECK (length(robustness_version) BETWEEN 1 AND 20),
        status                TEXT NOT NULL CHECK (status IN ({_Q(STATUSES)})),
        failure_code          TEXT CHECK (failure_code IS NULL OR length(failure_code) <= 40),
        failure_detail        TEXT CHECK (failure_detail IS NULL OR length(failure_detail) <= 300),
        run_at                TEXT NOT NULL,
        completed_at          TEXT NOT NULL,
        n_candidates          INTEGER NOT NULL {_INT0.format(c="n_candidates")},
        n_rejected            INTEGER NOT NULL {_INT0.format(c="n_rejected")},
        n_discarded           INTEGER NOT NULL {_INT0.format(c="n_discarded")},
        n_windows             INTEGER NOT NULL {_INT0.format(c="n_windows")},
        n_eligible            INTEGER NOT NULL {_INT0.format(c="n_eligible")},
        n_finalists           INTEGER NOT NULL {_INT0.format(c="n_finalists")},
        n_evaluations         INTEGER NOT NULL {_INT0.format(c="n_evaluations")},
        n_cache_hits          INTEGER NOT NULL {_INT0.format(c="n_cache_hits")},
        runtime_s             TEXT NOT NULL,
        data_hash             TEXT NOT NULL {_H64.format(c="data_hash")},
        walkforward_result_hash TEXT {_opt(_H64, "walkforward_result_hash")},
        result_hash           TEXT {_opt(_H64, "result_hash")},
        market_data_requests  INTEGER NOT NULL {_INT0.format(c="market_data_requests")},
        definition_json       TEXT NOT NULL {_JSON.format(c="definition_json", n=200000)},
        universe_note         TEXT NOT NULL CHECK (length(universe_note) BETWEEN 1 AND 300),
        CHECK (status = 'FAILED' OR (failure_code IS NULL AND result_hash IS NOT NULL)),
        CHECK (status = 'COMPLETED' OR status = 'NO_FINALIST' OR failure_code IS NOT NULL),
        CHECK (status != 'NO_FINALIST' OR n_finalists = 0)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mc_run_at ON model_campaigns (run_at)",
    f"""
    CREATE TABLE IF NOT EXISTS model_campaign_candidates (
        campaign_id           TEXT NOT NULL REFERENCES model_campaigns(campaign_id) {_ID.format(c="campaign_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        label                 TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
        config_json           TEXT NOT NULL {_JSON.format(c="config_json", n=8000)},
        evidence_json         TEXT NOT NULL {_JSON.format(c="evidence_json", n=400000)},
        PRIMARY KEY (campaign_id, config_hash)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS model_campaign_leaderboard (
        campaign_id           TEXT NOT NULL REFERENCES model_campaigns(campaign_id) {_ID.format(c="campaign_id")},
        rank                  INTEGER NOT NULL CHECK (typeof(rank) = 'integer' AND rank >= 1),
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        label                 TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
        eligible              INTEGER NOT NULL CHECK (eligible IN (0, 1)),
        exclusion_json        TEXT NOT NULL {_JSON.format(c="exclusion_json", n=4000)},
        row_json              TEXT NOT NULL {_JSON.format(c="row_json", n=20000)},
        PRIMARY KEY (campaign_id, rank),
        UNIQUE (campaign_id, config_hash)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS model_campaign_finalists (
        campaign_id           TEXT NOT NULL REFERENCES model_campaigns(campaign_id) {_ID.format(c="campaign_id")},
        finalist_rank         INTEGER NOT NULL CHECK (typeof(finalist_rank) = 'integer' AND finalist_rank >= 1),
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        label                 TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 120),
        role                  TEXT NOT NULL CHECK (role = '{FINALIST_LABEL}'),
        robustness_score      REAL NOT NULL,
        PRIMARY KEY (campaign_id, finalist_rank),
        UNIQUE (campaign_id, config_hash)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS model_campaign_model_cards (
        campaign_id           TEXT NOT NULL REFERENCES model_campaigns(campaign_id) {_ID.format(c="campaign_id")},
        config_hash           TEXT NOT NULL {_H64.format(c="config_hash")},
        card_json             TEXT NOT NULL {_JSON.format(c="card_json", n=60000)},
        PRIMARY KEY (campaign_id, config_hash)
    )""",
    f"""
    CREATE TABLE IF NOT EXISTS model_campaign_metrics (
        campaign_id           TEXT PRIMARY KEY REFERENCES model_campaigns(campaign_id) {_ID.format(c="campaign_id")},
        comparison_json       TEXT NOT NULL {_JSON.format(c="comparison_json", n=200000)},
        stability_json        TEXT NOT NULL {_JSON.format(c="stability_json", n=60000)},
        conventions_json      TEXT NOT NULL {_JSON.format(c="conventions_json", n=20000)}
    )""",
    *_no_update_delete("model_campaigns", "mc"),
    *_no_update_delete("model_campaign_candidates", "mcc"),
    *_no_update_delete("model_campaign_leaderboard", "mcl"),
    *_no_update_delete("model_campaign_finalists", "mcf"),
    *_no_update_delete("model_campaign_model_cards", "mcm"),
    *_no_update_delete("model_campaign_metrics", "mcx"),
]


def run_model_campaign_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
