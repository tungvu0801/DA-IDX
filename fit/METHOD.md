# Stage 3.4 — Strategy Fit (method)

**Question.** For this chosen stock, which of my saved strategy versions currently have entry rules that are met, which
rules are not met, and which required inputs are unavailable?

Strategy Fit is current conditions vs exact saved rules. It is not a recommendation, a ranking, a prediction, a
probability, a trade or a paper trade. It writes nothing and makes 0 Claude, broker and order calls.

## Versions

* Default: the **current** (latest) version of every **non-archived** strategy. "Show older versions" adds the older
  immutable versions of those strategies. Archived strategies are never evaluated.
* Every version is re-verified with the Stage 3.3 eligibility checks (Stage 3.2 integrity: immutability triggers,
  spec hash, rules hash, schema, registry version + fingerprint, still validates; then forward support).
  Failures are per version: `INTEGRITY_ERROR`, `REGISTRY_MISMATCH`, `UNSUPPORTED` (live-portfolio features). Nothing is
  substituted and one broken version never stops the others.
* `BACKTEST_READY` and `FORWARD_TEST_ONLY` versions are both evaluable (this is a current, forward-looking evaluation).

## Universe

A version whose saved universe does not contain the symbol is **OUTSIDE UNIVERSE**: its rules are not evaluated for
that symbol, no condition count is shown, and when no version includes the symbol no market data is requested at all.
The universe of a hash-verified spec is trusted even when that version is not evaluable (registry mismatch, unsupported
features), so those versions read OUTSIDE UNIVERSE for other symbols; a spec that fails its integrity check is not
trusted at all and reads INTEGRITY ERROR for every symbol.

## Decision session

The Stage 3.3 completed-session rule: the latest market (SPY) session whose date has **ended in New York**. Today's
unfinished daily bar and intraday quotes are never used — not even after 16:00 on the same day. If the latest session
cannot be confirmed (two or more weekdays without a SPY bar, or SPY missing a day other symbols have) the result is
`DATA_UNAVAILABLE`; one weekday without any bar is treated as a market holiday and says so
(`SESSION_ASSUMED_HOLIDAY`). A symbol without a bar on the decision session is `STALE_DATA`; a symbol with no bars at
all is `DATA_UNAVAILABLE`. An older session is never used instead.

## One dependency plan, one feature environment

1. The **entry** groups of every evaluable version are turned into Stage 3.2 `Needs` (via `forward.capture.price_needs`
   on an entry-only copy for the one symbol) and merged: stock features, market features, index symbols, the 80-stock
   breadth list only if some entry rule needs it, the sector ETF only if a sector feature is used. Exit rules are shown
   but never evaluated, so they add no dependency.
2. Bars: the Stage 3.2 immutable cache when a dataset already covers the window (read-only, content hash verified),
   else an in-memory copy fetched earlier in this server process (30 min), else **one** batched read-only request
   through `backtest.bars.fetch_and_store` with an in-memory stand-in for its dataset writer — the same "complete
   sessions only" rule, nothing written.
3. **One** `backtest.snapshots.day_snapshot` for (symbol, decision session) — the Stage 3.2 / 3.3 point-in-time
   snapshot (bars through the close only; live-engine calculations; Stage 3.1 extractors; warm-up rules).
4. Research: `forward.capture.research_context` — the latest **saved** research snapshot, read once. Nothing is
   generated. No snapshot → `research.*` UNAVAILABLE (`RESEARCH_UNAVAILABLE`). Saved after the close → labelled
   `POST_CLOSE`, never as if it existed at the close.
5. Events: `forward.capture.event_context` — Stage 2.6 providers, resolved once, only if an entry rule uses events.
   Incomplete calendars never become LOW / FALSE / safe (`EVENT_DATA_INCOMPLETE`, `EVENT_DATA_UNAVAILABLE`). There is
   no verified earnings calendar, so a stock event risk below HIGH is unavailable.

Every version is evaluated against that same environment, so a feature has one value per symbol and session.

## Rules and statuses

The entry group is evaluated only by `strategy.evaluate.group_met`, through the Stage 3.3 helper
`forward.journal._group_eval` (same trace, annotations and unavailable-data logic the forward journal stores):

| status | meaning |
|---|---|
| RULES MET | `group_met` is true (e.g. ANY with one true condition, even if another is unavailable) |
| RULES NOT MET | `group_met` is false and no unavailable value could change that |
| INCOMPLETE DATA | `group_met` is false only because an unavailable value could change the result |
| OUTSIDE UNIVERSE / UNSUPPORTED / INTEGRITY ERROR / REGISTRY MISMATCH / STALE DATA / DATA UNAVAILABLE | not evaluated |

Condition counts (`conditions_met / evaluable / total / unavailable`) are leaf counts for display only. They never
decide the result, are never turned into a percentage, and are not a quality measure.

Context timing per version (and overall): `STRICT_CLOSE_CONTEXT` (every input fixed at the close),
`POST_CLOSE_CONTEXT` (research created after the close, research freshness, or event calendars read after the close),
`INCOMPLETE_CONTEXT` (an input the entry rules use is unavailable).

## Evidence (separate, stored, exact version)

* Historical: every Stage 3.2 run of that `strategy_version_id`, newest first, each with its own period, costs, data
  hash and metrics. Never combined; the "most recent stored run" is labelled as such (not a best run).
* Forward: the version's Stage 3.3 journal (status, continuity, captured / missed sessions) and, for the symbol, the
  latest stored decision, shadow state and completed reference cycles. An open shadow state is shown as a separate
  FORWARD JOURNAL STATE card, never merged into entry fit.
* No evidence is shown as "no evidence" — not as a poor fit. Evidence never changes the fit.

## What is never used

Robinhood positions, P&L, concentration, cash or account size (portfolio features are `UNSUPPORTED`); risk limits
(shown read-only); exit rules (shown as the collapsed exit plan); unsaved builder drafts.

## Read-only

No table is created or migrated and nothing is written: strategy, backtest and forward tables are read through
`mode=ro` connections (`fit/readonly.py`); a database without Stage 3.2 / 3.3 tables simply has no stored evidence.

## Known limitations

* Each universe is the explicit list the user saved.
* Breadth uses today's fixed list of 80 large stocks (survivorship caveat).
* The sector map is static.
* No verified earnings calendar.
* Saved research may have been created after the evaluated close.
* Daily-close resolution only; a session counts as completed only after its New York date ends.
* Strategy Fit describes current rule alignment, not future performance.
