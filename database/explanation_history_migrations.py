"""
database/explanation_history_migrations.py — Stage 3.9 optional AI EXPLANATION HISTORY (additive). Idempotent; safe on any
database state. Only NEW tables are created; no research, strategy, backtest, forward-evidence or Stage 3.7 table is
touched, and PRAGMA user_version is left alone (like every other Stage 3 migration).

  * ai_explanation_settings — one allowed key, enforced by the database:
      ai_explanation_history_enabled  "true" / "false" (absent = false: history is OFF until the user enables it)
    (Stage 3.7's app_settings enforces its own three keys in its schema, so it cannot hold this one without rebuilding
    a frozen table; this table follows the same pattern.)
  * ai_explanation_history — what the app showed the user, append-only: triggers reject every UPDATE and DELETE, and an
    INSERT that would replace an existing row (same history_id, or the same explanation content for the same input) is
    silently skipped, so a cached explanation shown again never creates a duplicate and nothing is ever overwritten.

History is product history, not financial evidence: no Stage 3.2-3.8 calculation reads these tables.
"""
import sqlite3

SETTING_KEYS = ("ai_explanation_history_enabled",)
TYPES = ("STRATEGY_FIT_EXPLANATION", "EVIDENCE_EXPLANATION")

_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS ai_explanation_settings (
        key          TEXT PRIMARY KEY CHECK (key IN ('ai_explanation_history_enabled')),
        value        TEXT NOT NULL CHECK (value IN ('true', 'false')),
        updated_at   TEXT NOT NULL
    ) WITHOUT ROWID;
    """,
    """
    CREATE TABLE IF NOT EXISTS ai_explanation_history (
        history_id                   TEXT PRIMARY KEY CHECK (length(history_id) = 32),
        explanation_type             TEXT NOT NULL CHECK (explanation_type IN ('STRATEGY_FIT_EXPLANATION', 'EVIDENCE_EXPLANATION')),
        strategy_id                  TEXT CHECK (strategy_id IS NULL OR length(strategy_id) = 32),
        strategy_version_id          TEXT NOT NULL CHECK (length(strategy_version_id) = 32),
        symbol                       TEXT CHECK (symbol IS NULL OR length(symbol) BETWEEN 1 AND 12),
        decision_session             TEXT CHECK (decision_session IS NULL OR length(decision_session) = 10),
        backtest_run_id              TEXT CHECK (backtest_run_id IS NULL OR length(backtest_run_id) = 32),
        forward_journal_id           TEXT CHECK (forward_journal_id IS NULL OR length(forward_journal_id) = 32),
        input_fingerprint            TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
        prompt_version               TEXT NOT NULL CHECK (length(prompt_version) BETWEEN 1 AND 64),
        provider                     TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 32),
        model                        TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 64),
        cache_hit                    INTEGER NOT NULL CHECK (cache_hit IN (0, 1)),
        claude_calls                 INTEGER NOT NULL CHECK (claude_calls IN (0, 1)),
        generated_at                 TEXT NOT NULL,
        saved_at                     TEXT NOT NULL,
        structured_explanation_json  TEXT NOT NULL CHECK (json_valid(structured_explanation_json)
                                                          AND length(structured_explanation_json) <= 50000),
        grounding_summary_json       TEXT NOT NULL CHECK (json_valid(grounding_summary_json)
                                                          AND length(grounding_summary_json) <= 50000),
        explanation_sha256           TEXT NOT NULL CHECK (length(explanation_sha256) = 64),
        status                       TEXT NOT NULL CHECK (status IN ('OK', 'LOCAL')),
        CHECK ((status = 'LOCAL') = (provider = 'LOCAL')),
        CHECK (provider <> 'LOCAL' OR (model = 'NONE' AND claude_calls = 0 AND cache_hit = 0)),
        CHECK ((explanation_type = 'STRATEGY_FIT_EXPLANATION' AND symbol IS NOT NULL AND decision_session IS NOT NULL
                AND backtest_run_id IS NULL AND forward_journal_id IS NULL)
            OR (explanation_type = 'EVIDENCE_EXPLANATION' AND symbol IS NULL AND decision_session IS NULL))
    );
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_explanation_history_content ON ai_explanation_history
        (explanation_type, input_fingerprint, prompt_version, provider, model, explanation_sha256);
    """,
    "CREATE INDEX IF NOT EXISTS ix_ai_explanation_history_saved ON ai_explanation_history (saved_at DESC, history_id DESC);",
    """
    CREATE TRIGGER IF NOT EXISTS ai_explanation_history_no_update BEFORE UPDATE ON ai_explanation_history
    BEGIN SELECT RAISE(ABORT, 'AI explanation history is append-only: saved explanations cannot be changed'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ai_explanation_history_no_delete BEFORE DELETE ON ai_explanation_history
    BEGIN SELECT RAISE(ABORT, 'AI explanation history is append-only: saved explanations cannot be deleted'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ai_explanation_history_no_replace BEFORE INSERT ON ai_explanation_history
    WHEN EXISTS (SELECT 1 FROM ai_explanation_history h
                 WHERE h.history_id = NEW.history_id
                    OR (h.explanation_type = NEW.explanation_type AND h.input_fingerprint = NEW.input_fingerprint
                        AND h.prompt_version = NEW.prompt_version AND h.provider = NEW.provider AND h.model = NEW.model
                        AND h.explanation_sha256 = NEW.explanation_sha256))
    BEGIN SELECT RAISE(IGNORE); END;
    """,
]


def run_explanation_history_migrations(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    conn.commit()
