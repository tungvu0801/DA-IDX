# Stage 3.5 evidence comparison — method

The Evidence workspace answers one question: **how does the stored forward behaviour of this exact saved strategy
version compare, descriptively, with its stored historical backtest evidence?** It is a derived, read-only view over
immutable Stage 3.2 and Stage 3.3 rows. It never re-runs a backtest, captures or reconstructs a forward session,
fetches market data, generates research, re-reads old event context, recomputes an old journal decision, calls Claude,
reads a broker account or places an order. It never scores, ranks, recommends, optimises or forecasts.
`strategy/CONTRACTS.md`, `backtest/METHOD.md`, `forward/METHOD.md` and `fit/METHOD.md` are unchanged.

## Exact version isolation

Everything joins through the exact `strategy_version_id`. The version itself must pass Stage 3.2's integrity checks
(stored spec re-hashes to `spec_hash`, `rules_hash` re-computes, schema version) — otherwise the whole view is refused
with `EVIDENCE_INTEGRITY_ERROR`. A registry-fingerprint change does not hide stored evidence (it was recorded against
this version) but is shown as `REGISTRY_CHANGED`.

* A run or journal id of **another** version is refused: `EVIDENCE_VERSION_MISMATCH` (409). Names are never used to
  join — two versions with the same name are different evidence.
* The selected run must name the version in its row (`strategy_id`, `strategy_version_id`, `version_number`,
  `spec_hash`, `rules_hash`), in its stored configuration (which must re-hash to `config_hash`) and in every stored
  trade row; its stored trades must match its stored trade counts.
* The selected journal must name the version in its row and stored configuration (re-hashed to `config_hash`), and
  every captured session must carry the same `spec_hash`.
* A failure marks only that side `EVIDENCE_INTEGRITY_ERROR` (with the failed checks); the other side is still shown.
  Nothing is compared across a side that failed.

## Selection (never by performance)

* **Historical run:** the version's runs are listed most recent first; the default is the **most recent COMPLETED
  stored run**, labelled "Most recent stored run". The user may pick any other completed run. Runs are never combined,
  averaged or chosen by return.
* **Forward journal:** journals are listed non-archived first, then newest first. The default is the current
  (ACTIVE / CONTINUITY_BLOCKED) journal; when only archived journals exist the most recent archived one is shown,
  labelled, and the user can pick another. Journals are never combined. Archived and blocked journals stay viewable.

## Historical side (values as stored)

All metrics come from the run's stored `result_json` (`backtest.metrics.core_metrics` output, stored at completion):
closed trades, open positions at end, total / annualized return, maximum drawdown, win rate, average / median trade
return, average winner / loser, profit factor, average / median holding days, average MFE / MAE, time in market,
costs, and the Stage 3.2 sample label. Assumptions (period, initial equity, slippage, commission, execution model, data
source / feed / adjustment, selection policy, risk limits), exit-reason counts, the entry audit and the stored warnings
are shown as stored. The per-symbol rows use the stored symbol breakdown; open-at-end and positive-trade counts per
symbol are counted from the immutable trade rows (as the ledger shows them). The equity curve is not redrawn here —
"Open historical equity curve" opens the stored run in the Stage 3.2 panel.

## Forward side (stored rows only)

Counts come from the stored sessions, observations and reference fills through the unchanged Stage 3.3 journal
summary: captured / missed sessions, ENTER / EXIT / HOLD / SKIP decisions (with SKIP reasons), open / pending /
blocked shadow states, filled / unfilled reference entries. Journal status (ACTIVE, CONTINUITY_BLOCKED, ARCHIVED) and
continuity (CONTINUOUS, GAPPED, CONTINUITY_BLOCKED) are shown prominently; a gapped or blocked journal carries a note
that the forward evidence is incomplete.

**Reference cycles** (`comparison/cycles.py`) — Stage 3.3 stores no cycle row, so a completed cycle is reassembled
from exactly four stored records of one `(journal, symbol, cycle_no)`: the ENTER observation, the FILLED reference
ENTRY fill, the EXIT observation and the FILLED reference EXIT fill. Primary keys guarantee each row once; duplicates,
orphans (a fill or EXIT without its ENTER), mismatched signal sessions or a stored move that disagrees with its own
stored prices are integrity errors. Anything incomplete is **OPEN / INCOMPLETE REFERENCE CYCLE** (or **INCOMPLETE —
CONTINUITY BLOCKED**, or **ENTRY UNFILLED**) and is excluded from every completed-cycle statistic; open cycles are
never force-closed.

* `reference_move_pct = (reference exit open / reference entry open − 1) × 100` — long only, no slippage, no
  commission, no sizing. Both opens are on one price basis: the exit capture's data (the entry session's open re-read
  from the same split / dividend adjusted dataset, as Stage 3.3 stores in the exit fill's `price_basis`). The derived
  value must equal the stored `reference_move_pct`.
* Holding = the EXIT observation's stored `holding_sessions` (the symbol's captured sessions from the reference-entry
  session, day 1, through the exit-signal session — Stage 3.3's trading-session convention; calendar days are never
  counted). If a cycle lacks it, holding is shown as unavailable.
* Metrics of completed cycles: count, reference moves, average / median move, positive / negative / flat cycles,
  positive-cycle rate (always shown with its count), average / median holding sessions. With zero completed cycles no
  average, rate or 0 % is shown — "No completed forward cycles yet".
* Sample labels (counts, not quality): 0 `NO_COMPLETED_FORWARD_CYCLES`, 1–9 `VERY_SMALL_FORWARD_SAMPLE`, 10–29
  `SMALL_FORWARD_SAMPLE`, 30+ the count with "not statistical proof".
* Context timing per captured session, counted separately: `STRICT_FORWARD`, `POST_CLOSE_FORWARD_CONTEXT`, and
  `CAPTURED_AFTER_NEXT_OPEN` (the Stage 3.3 session warning, an extra flag on post-close sessions).
* **Forward MFE / MAE** (Stage 3.6, `forward/EXCURSIONS.md`): shown only for a journal with stored Stage 3.6 excursion
  evidence — the sample ("3 tracked cycles of 7 completed cycles"), and averages over TRACKED completed cycles only
  (legacy or incomplete cycles are never counted as 0; open cycles show values so far and are excluded). A journal without
  Stage 3.6 evidence keeps the Stage 3.5 view exactly: **Not tracked in Stage 3.3.** Nothing is backfilled from later data.
* No forward dollars, equity, portfolio return, annualization or extrapolation.

## Side by side

Only measures with the same meaning share a row: sample (closed trades / completed reference cycles — not
differenced), average and median return / move, positive outcomes (win rate / positive-cycle rate with counts),
average and median holding (both count the symbol's sessions from the entry-fill session through the exit-signal
session), MFE / MAE (forward: the Stage 3.6 tracked-cycle average with its sample, differenced only when the tracked
sample is at least 1; otherwise not tracked), continuity, and still-open positions / cycles. The difference column is
`forward − historical`, computed from the two displayed (2-decimal, half-up) values, in **percentage points** for %
measures and sessions for holding — never a relative %, never labelled better / worse. Total return, annualized
return, drawdown, profit factor, time in market, costs and entry frequency are shown on one side only, with the reason.
Each row carries a note on what each side measures (trade return after costs and sizing vs. open-to-open reference
move with none).

Evidence notes list limitations only (different observation periods, small samples, continuity gaps / blocks,
post-close context, capture after the next open, survivorship and adjusted-price warnings stored with the run, daily-bar
resolution, no historical event / research reconstruction, position limits applied only in the backtest, restated
forward price basis). They are never turned into a score.

## Read-only

The view opens the database with `mode=ro` connections through the Stage 3.4 read-only store subclasses (no
migration, no interrupted-run update). There is no new table, migration or write, and no result is persisted. A
database without the Stage 3.2 / 3.3 tables is read as "no stored evidence", never migrated. The frontend keeps
already-opened views in memory for the page session only, labels them with the time they were loaded, and refreshes
only on "Refresh evidence".

## Known limitations

* Historical backtests and forward journals have different assumptions (sizing, costs, position limits) and periods.
* The forward journal has no sizing, so there are no forward dollars; its fills are reference opens, not executions.
* Forward MFE / MAE exist only for cycles entered after Stage 3.6 tracking started; earlier cycles stay untracked.
* A forward sample stays very small for a long time; manual capture can leave gaps or block a symbol.
* Historical survivorship (universe chosen today, fixed breadth list, static sector map) and adjusted-price caveats
  remain; historical events and research were never reconstructed.
* Only strategies that are not archived appear in the picker (as in Strategy Fit).
