# Stage 3.9 — AI explanation history and separate AI budgets

Product infrastructure only: no financial calculation, prompt (`strategy_explain_v1`, `evidence_explain_v2`) or
explanation semantics changed.

## Explanation history (optional, local, append-only)

* **OFF by default.** Strategy Lab → **AI history** → **Enable history** (or `POST /api/explanation-history/settings
  {"enabled": true}`). While OFF, nothing is written — not even the table.
* **What is saved:** only an explanation Stage 3.8 *accepted* — status `OK` (Claude, every guard passed) or `LOCAL` (the
  no-AI explanation; saved as provider `LOCAL`, model `NONE`, 0 calls). Never a provider error, a cut-off or a withheld
  answer. A row holds the exact structured explanation shown, the exact references (strategy version + symbol + decision
  session, or version + resolved run + journal), fingerprint, prompt version, provider / model, cache hit, Claude calls,
  generated / saved times, and a compact grounding summary (fit status, condition trace, sample sizes, continuity,
  MFE / MAE tracking, compatible differences). No keys, headers, prompts or broker data.
* **Append-only, enforced by SQLite** (`ai_explanation_history`): triggers reject UPDATE and DELETE; an insert that would
  replace a row, or repeat the same explanation for the same input, is skipped. A cache hit or the same local text shown
  again therefore adds nothing; a changed deterministic input has a new fingerprint and adds a new row. Old rows are
  never rewritten.
* **Reading** (`GET /api/explanation-history`, filters `kind=all|fit|evidence`, `origin=all|local|claude`, newest first,
  50 per page with a cursor; `GET /api/explanation-history/{id}`) makes 0 Claude calls, never regenerates text and never
  touches the AI cache. History is not evidence: no Stage 3.2–3.8 calculation reads it.
* **Storage:** two additive tables in the app database (`ai_explanation_settings`, `ai_explanation_history`), created on
  first enable (`database/explanation_history_migrations.py`, idempotent). The setting has its own table because Stage
  3.7's `app_settings` allows only its own keys in its schema.

## Separate AI budgets

| Budget | Covers | Env (defaults) |
|---|---|---|
| Research | every AI feature that existed before Stage 3.8 (Research Analyze / batch, chat, portfolio and insight explanations) | `AI_RESEARCH_HOURLY_LIMIT` (10), `AI_RESEARCH_DAILY_LIMIT` (30); the older `AI_MAX_CALLS_PER_HOUR` / `AI_MAX_CALLS_PER_DAY` still work |
| Explanation | Stage 3.8 "Explain this setup" / "Explain this evidence" | `AI_EXPLANATION_HOURLY_LIMIT` (10), `AI_EXPLANATION_DAILY_LIMIT` (30) |

* The category comes from the request type (`agents.usage_tracker.category_of`); each budget counts only its own
  successful calls, so one never reduces the other. Research limit text and counting are unchanged.
* Cache hits and local explanations consume nothing; a real explanation consumes 1 Explanation call; identical
  concurrent requests share one call. When the Explanation budget is used up the preview says *"AI explanation limit
  reached. The deterministic result is still available."* — Strategy Fit, Evidence, the journal and backtests keep
  working, and Research is unaffected (and vice versa).
* Display: the header shows `AI · Research x/30 · Explain y/30` (details on hover and on the dashboard);
  `GET /api/ai/usage` keeps its top-level fields as the Research budget and adds `budgets.research` / `budgets.explanation`;
  the explanation confirmation shows the Explanation budget only.
* The usage tracker is in memory (unchanged): counts reset when the server restarts.
