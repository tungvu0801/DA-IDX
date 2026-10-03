# Stage 3.3 forward-test signal journal — method

The journal answers one question: **what did this exact, saved strategy version say at each market close that was
captured after that session actually happened?** It is not a historical backtest (Stage 3.2), not a paper account and
not automatic. It never sizes positions, never creates an order object, never calls a broker or Claude, and never runs
on its own: every capture is the user pressing **Record latest completed close**. `strategy/CONTRACTS.md` (Stage 3.1)
and `backtest/METHOD.md` (Stage 3.2) are unchanged; this file documents how Stage 3.3 uses them.

## Eligibility (checked at creation, at every preflight and before every capture)

Stage 3.2's integrity checks are reused unchanged (`backtest.runs.eligibility`): the version exists, its table is
trigger-protected, its stored JSON re-hashes to `spec_hash`, `rules_hash` re-computes, it still validates unchanged, and
schema / registry version / registry fingerprint match the running code. Only the readiness gate is widened:
`BACKTEST_READY` **and** `FORWARD_TEST_ONLY` are accepted (stored readiness must equal recomputed readiness), and every
feature the rules use must have `forward_support = true`. Rejections: `NOT_FOUND`, `STRATEGY_INTEGRITY_ERROR`,
`REGISTRY_MISMATCH`, `FORWARD_UNSUPPORTED` (live-portfolio features). A journal is pinned to its version: a capture is
refused if the version's id, `spec_hash` or `rules_hash` no longer match the journal.

## Forward means forward

* `created_session_date` = the journal's creation date in New York; `forward_start_date` = the next calendar day.
  The first eligible session is the first market session **on or after** `forward_start_date` — strictly after the
  creation date, even if the journal was created before that day's close.
* A session is recorded only when it is **complete** — its date has ended in New York (Stage 3.2's rule; the in-progress
  daily bar is never downloaded or cached) — and only the **latest** completed session can be recorded.
* **No backfill.** Eligible sessions between the previous capture and the latest completed session that nobody captured
  are stored as `MISSED` (with the capture that found them). They are never reconstructed from today's research, news,
  events or bars. The database enforces it: a session row can only be inserted for a date after every session already
  captured and not before `forward_start_date`; observations can only be added to the latest captured session; a
  capture must be timestamped after the session's close.
* Recording a session that is already captured returns `ALREADY_RECORDED` with the stored record — nothing is
  recalculated (no newer research, events or bars can replace it).

## Session calendar and data

The market proxy's (SPY's) daily bars define the sessions. Bars come from the **Stage 3.2 cache**
(`historical_bar_datasets` / `historical_daily_bars`): a capture reuses a cached dataset whose requested range covers
`[needed start, last complete session]`, otherwise it downloads the missing symbols in **one batched, read-only**
request through `backtest.bars.fetch_and_store` → `data.market_data.fetch_daily_bars` (same feed and adjustment as the
app). Every download is a new immutable dataset; nothing is overwritten. The needed start is the earlier of the full
indicator window (120 calendar days + 10) before the last complete session, the previous capture (to find missed
sessions) and the entry signal of every open cycle (to rebase entry levels, below). A calendar hole of two or more
weekdays blocks the capture (`DATA_UNAVAILABLE`), as in Stage 3.2. Each captured session stores its dataset ids,
content hashes and `data_hash` = SHA-256 of `{symbol: content_hash}`.

Preflight reads the cache; if the market proxy is not cached for the latest complete date it makes one small read-only
SPY request that is **not stored**, only to name the latest completed session. Preflight writes nothing.

## Feature snapshots

* **Technical / market / sector** values are exactly Stage 3.2's point-in-time snapshot: `backtest.snapshots.day_snapshot`
  (the replay client serves bars dated `T-119 … T` only, the live engine computes, the Stage 3.1 extractors map, the same
  warm-up rules apply). Dependencies come from Stage 3.2's `needs_for`, applied to a copy of the spec that keeps only
  daily-bar conditions (used for dependencies only, never evaluated), so the fixed 80-stock breadth list loads only when a
  rule uses environment / breadth / risk appetite.
* **Research** (`research.*`): the latest **saved** research snapshot (Stage 2.5 table, read only) through
  `insights.research.from_snapshot` and `strategy.features.research_values`. Nothing is generated — no Analyze, no Claude.
  No snapshot → `UNAVAILABLE` / `RESEARCH_UNAVAILABLE` ("Research required — no current saved research"). Provenance:
  snapshot id, fingerprint, created time, market / price timestamps, age at capture, whether it existed at the close.
  `research.freshness` is measured at capture time.
* **Events** (`event.risk_level`, `market.major_event_within_24h`): `services.event_context.build_event_context` (Stage 2.6)
  with `strategy.features.event_values` / `market_values`, read at capture time. The Stage 2.6 providers turn failures
  into empty lists, so the capture also reads their success cache (`data/events/cache.py` keys for each FRED release,
  the FOMC calendar and the stock's corporate actions) to know whether each calendar really loaded. **Missing event data
  is never read as low risk:** a stock event risk is available only when every calendar loaded (including earnings) or
  when it is already `HIGH` (missing calendars can only add events); otherwise it is withheld as `EVENT_DATA_INCOMPLETE`.
  The macro-event flag is available when it is `true`, or `false` with the FRED and FOMC calendars loaded.

Every observation stores a snapshot of every strategy-relevant feature (rule features, entry levels, the market trend):
feature id, value, availability, reason if unavailable, registry source, source timestamp, timing (`AT_CLOSE` /
`POST_CLOSE`), the session, the market-close time and the capture time.

## Context timing

* `STRICT_FORWARD` — every input the rules used was fixed as of the close: bars through the close, and research only
  from snapshots saved before the close.
* `POST_CLOSE_FORWARD_CONTEXT` — some forward-only input was read after the close (research saved after the close,
  research freshness, any event value). Still forward evidence, but not identical to Stage 3.2's at-the-close
  assumption; the session says so. If the capture ran after the next session probably opened (09:30 New York on the
  next weekday), it adds `CAPTURED_AFTER_NEXT_OPEN`.

## Shadow state and decisions

Per symbol: `FLAT`, `ENTRY_PENDING`, `OPEN`, `EXIT_PENDING`, `CONTINUITY_BLOCKED`. No equity, cash, shares, sizing,
dollar P&L, order object or broker; `max_position_pct` / `max_open_positions` are not applied (each symbol is observed
on its own — Stage 3.2 applies them, so a later comparison must account for contention). Each captured session T runs
in Stage 3.2's day order:

| step | rule |
|---|---|
| open of T | `EXIT_PENDING` with a bar at T → **reference exit** = T's open → `FLAT`. `ENTRY_PENDING` → **reference entry** = T's open (T is the very next session) → `OPEN`; no bar at T → `UNFILLED` (`NEXT_SESSION_BAR_MISSING`) → `FLAT` |
| close of T, `OPEN` | T counts as a holding session (fill day = 1); exit rules: condition group OR invalidation OR target OR max holding; every true reason stored (contract order) → `EXIT` / `EXIT_PENDING` |
| close of T, `FLAT` | entry group → `ENTER` / `ENTRY_PENDING`; entry-time support / resistance and close are captured. A required entry level missing → `SKIP` (`REQUIRED_ENTRY_SUPPORT_UNAVAILABLE` / `…RESISTANCE…`) |
| no bar at T | `SKIP` (`NO_BAR_FOR_SESSION`): not evaluated, not counted as a holding session |

Decisions use the Stage 3.1 vocabulary: `ENTER`, `EXIT`, `HOLD` (no state change — shown as "No entry" when flat and
"Hold / no exit signal" when open), `SKIP` (required data unavailable). Rules are evaluated only by
`strategy.evaluate.group_met`. `SKIP` versus `HOLD` is decided from group_met's own per-condition results: when an
unavailable value could still change the group's outcome, the decision is `SKIP` (`REQUIRED_DATA_UNAVAILABLE`), never a
false `HOLD`; a missing value can never produce `ENTER` (Stage 3.1: missing = not met), so the lifecycle is identical to
Stage 3.2's. Condition counts ("2 / 3 entry conditions met") are for display only — not a score or probability.

The next-open and exit conventions are Stage 3.2's (entry-time levels frozen at the signal; `CLOSE_BELOW_ENTRY_SUPPORT`
= close < entry support; `PCT_BELOW_ENTRY` = close ≤ entry × (1 − pct); targets ≥; max holding counts the symbol's
sessions; re-entry first evaluated at the close of the exit-fill session). A test runs the Stage 3.2 engine and the
journal on the same bars and requires identical signals, fills, exit reasons and holding counts.

**Adjusted prices.** Every capture downloads split- and dividend-adjusted bars, so a corporate action restates earlier
prices between captures. Stored values are never changed; at each capture the entry-time support / resistance are
rebased by (signal-session close in this capture's data / signal close as captured) and percentage levels use the
reference-fill session's open in this capture's data — exactly the numbers Stage 3.2 would use on this dataset. The
factors are stored, and `PRICE_BASIS_RESTATED` is shown when they differ from 1. The reference move of a completed cycle
is open-to-open on one consistent price basis.

## Continuity

A journal is `CONTINUOUS` when no eligible session was missed and `GAPPED` otherwise — coverage, not quality.
A session missed while a symbol was `FLAT` only marks the gap: later sessions are still evaluated. A session missed while
a symbol was `ENTRY_PENDING`, `OPEN` or `EXIT_PENDING` makes its lifecycle unknowable: the symbol becomes
`CONTINUITY_BLOCKED` (`SKIP` / `FORWARD_CONTINUITY_GAP`) for the rest of the journal and the journal status becomes
`CONTINUITY_BLOCKED`. Other symbols keep being observed. There is no "pretend it happened" workflow: end the journal and
start a new one.

## Journals

One non-archived journal per strategy version (database unique index). Archiving stops captures and deletes nothing;
archived journals stay viewable; a new journal can then be started for the same version (its own forward start).
Changed rules need a new strategy version with its own journal. Journal identity / configuration never change; status
only moves `ACTIVE → CONTINUITY_BLOCKED → ARCHIVED` (or `ACTIVE → ARCHIVED`).

## Summary

Sessions captured / missed, ENTER / EXIT / HOLD / SKIP counts, open / pending / blocked shadow states, filled and
unfilled reference entries, completed reference cycles and — labelled descriptive — their average open-to-open
reference move. No return, score, ranking or verdict.

## For later stages (stored contract; nothing here implements them)

Each observation maps onto the Stage 3.1 `PaperSignalRecord` shape: strategy reference (journal → `strategy_id`,
`strategy_version_id`, `spec_hash`, registry fingerprint), `symbol`, signal time (`market_close_snapshot_time` of the
session), `feature_snapshot`, `decision` (ENTER / EXIT / HOLD / SKIP) and `evaluation_trace`, plus what a forward record
needs beyond it: capture time, context timing, research / event provenance, shadow state before / after, the cycle's
lifecycle, and the `data_hash` of the bars. Reference fills link to the observation that produced them. A strategy
matcher can read snapshots + traces; forward analysis can read cycles and missed sessions (continuity must be taken into
account); a paper bot would add its own order records on top, never modify these.

## Known limitations

* Recording is manual: a session nobody records is missed for good; a missed session during an open cycle blocks that
  symbol for the rest of the journal.
* A session can be recorded only after its New York date has ended (conservative; it can be up to a day later than the
  close).
* Research and event values are read at capture time (post-close); event calendars cannot prove what was known at the
  close. The earnings calendar has no verified provider, so a stock event risk below `HIGH` is always withheld.
* Only saved research snapshots are used; research shown on screen but not saved is not.
* No sizing, costs, slippage or position limits; reference fills are the printed open (IEX feed by default), not an
  achievable execution.
* Each capture stores a new window of bars per symbol (about 90 bars; a strategy using the breadth list stores ~84
  symbols per capture).
