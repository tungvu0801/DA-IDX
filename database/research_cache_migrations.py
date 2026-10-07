"""
database/research_cache_migrations.py — research workflow persistence (additive). Idempotent; safe on any database state.
Two NEW append-only tables; nothing else is touched and PRAGMA user_version is left alone (like every other migration).

  * research_results   one row per Claude research attempt (or refusal) for one symbol: the deterministic request payload,
                       the validated structured result (or why it was withheld / failed), the cache key it answers to and
                       its stale_after time. The latest row for a cache key is the cache entry; a refresh adds a new row.
  * research_batches   one row per orchestration run: what was shortlisted, researched, served from cache and skipped (and
                       why) — the observability record. Never contains a credential, an order or a recommendation.
"""
import sqlite3

STATUSES = ("COMPLETE", "WITHHELD", "FAILED")
TABLES = ("research_results", "research_batches")
_H64 = "CHECK (length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*')"
_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS research_results (
        id              INTEGER PRIMARY KEY,
        cache_key       TEXT NOT NULL {_H64.format(c="cache_key")},
        symbol          TEXT NOT NULL CHECK (length(symbol) BETWEEN 1 AND 12 AND symbol NOT GLOB '*[^A-Z.-]*'),
        request_type    TEXT NOT NULL CHECK (length(request_type) BETWEEN 1 AND 40),
        bucket          TEXT NOT NULL CHECK (bucket GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        context_hash    TEXT NOT NULL {_H64.format(c="context_hash")},
        run_id          TEXT CHECK (run_id IS NULL OR (length(run_id) = 32 AND run_id NOT GLOB '*[^0-9a-f]*')),
        status          TEXT NOT NULL CHECK (status IN ({", ".join(f"'{s}'" for s in STATUSES)})),
        reason          TEXT CHECK (reason IS NULL OR length(reason) <= 120),
        request_json    TEXT NOT NULL CHECK (json_valid(request_json) AND length(request_json) <= 8000),
        result_json     TEXT CHECK (result_json IS NULL OR (json_valid(result_json) AND length(result_json) <= 12000)),
        model           TEXT CHECK (model IS NULL OR length(model) <= 80),
        prompt_version  TEXT NOT NULL CHECK (length(prompt_version) <= 40),
        claude_calls    INTEGER NOT NULL CHECK (typeof(claude_calls) = 'integer' AND claude_calls BETWEEN 0 AND 1),
        refresh         INTEGER NOT NULL CHECK (refresh IN (0, 1)),
        created_at      TEXT NOT NULL,
        stale_after     TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rr_key ON research_results (cache_key, id)",
    "CREATE INDEX IF NOT EXISTS idx_rr_symbol ON research_results (symbol, request_type, id)",
    """
    CREATE TABLE IF NOT EXISTS research_batches (
        id              INTEGER PRIMARY KEY,
        run_id          TEXT NOT NULL CHECK (length(run_id) = 32 AND run_id NOT GLOB '*[^0-9a-f]*'),
        created_at      TEXT NOT NULL,
        report_json     TEXT NOT NULL CHECK (json_valid(report_json) AND length(report_json) <= 40000)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rb_run ON research_batches (run_id, id)",
    """CREATE TRIGGER IF NOT EXISTS rr_no_update BEFORE UPDATE ON research_results
       BEGIN SELECT RAISE(ABORT, 'research_results rows are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS rr_no_delete BEFORE DELETE ON research_results
       BEGIN SELECT RAISE(ABORT, 'research_results rows are never deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS rb_no_update BEFORE UPDATE ON research_batches
       BEGIN SELECT RAISE(ABORT, 'research_batches rows are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS rb_no_delete BEFORE DELETE ON research_batches
       BEGIN SELECT RAISE(ABORT, 'research_batches rows are never deleted'); END""",
]


def run_research_cache_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
