# Stage 3.2 historical backtester — method

The backtester answers one question: **what would this exact, saved strategy version have done historically under
these explicit assumptions?** It does not choose strategies, optimise parameters, rank versions, recommend stocks,
paper trade, place orders or call Claude. `strategy/CONTRACTS.md` (Stage 3.1) is authoritative and unchanged; this
file documents how Stage 3.2 implements it.

## Eligibility (re-checked at preflight, at run creation and inside the run job)

A run needs a saved version that exists, sits in the trigger-protected `strategy_versions` table, re-hashes to its
`spec_hash`, re-computes its `rules_hash`, still validates unchanged, and matches the running schema version, feature
registry version and registry fingerprint, with readiness `BACKTEST_READY` (stored and recomputed). Rejections are
structured: `NOT_FOUND`, `INTEGRITY_ERROR`, `SCHEMA_VERSION`, `REGISTRY_MISMATCH`, `FORWARD_TEST_ONLY`,
`UNSUPPORTED`, `INVALID_DATE_RANGE`, `INVALID_CONFIG`, `DATA_UNAVAILABLE`, `INSUFFICIENT_WARMUP`. Nothing is
downgraded or substituted.

## Data

* Source: the existing `data.market_data.fetch_daily_bars` (Alpaca **market data** only) with the app's own settings —
  daily timeframe, `feed = config.DATA_FEED` (IEX by default), `adjustment = all` (split + dividend adjusted). No
  trading client exists anywhere in the project.
* Cache: `historical_bar_datasets` + `historical_daily_bars`. One explicit download = one immutable dataset per symbol
  (source, feed, adjustment, requested range, fetch time, SHA-256 of its bars). A refresh stores a new dataset; old
  datasets are never overwritten, so every completed run keeps the exact bars it used (`data_json` records dataset ids
  and content hashes; they are re-verified when a run loads them).
* Only complete sessions are cached (dates before today in New York). Missing bars are never created. A symbol the
  provider returned nothing for is cached as an empty dataset and reported. If one batched request returns nothing
  for every symbol it is treated as a failed request and nothing is cached.
* Session calendar: the market proxy's (SPY's) session dates inside the requested period.

## Point-in-time snapshots (decision at the close of T)

`backtest.replay.ReplayClient` replaces the Alpaca data client inside the unchanged `fetch_daily_bars`, so
`scanner.market_scanner.analyze_symbols`, `analysis.indicators.compute_metrics`, `insights.market.build_market_insights`
and `analysis.sector_context.compute_sector_context` run exactly as in the live app. At T the replay serves, per
symbol, the bars dated `T-119 .. T` (the live app's `DAILY_BAR_LOOKBACK_DAYS = 120` calendar-day window ending at the
close of T) and nothing later. A symbol with no bar dated T is not evaluated at T (a stale bar is never treated as the
decision day). Market inputs use `macro_events = []`, `news = []`, `scanner_counts = None`; price is the close of T
(no latest-trade override). Feature values come only from `strategy.features.stock_values / market_values /
sector_values`.

Warm-up: bars before the start are fetched (`start - 119` days, rounded to the month start) so the first session has a
full window. Each feature needs a minimum number of sessions in its window (`backtest.snapshots.MIN_SESSIONS`, from
the same config constants the indicators use — e.g. 50 for EMA-based trend labels, 60 for support / resistance). With
fewer, the value is withheld and marked `INSUFFICIENT_HISTORY`; a condition on it is not met. Warm-up bars never
create signals: the simulation only walks sessions inside `[start, end]`.

Market features that need the fixed 80-stock breadth list (environment, breadth, breadth %, risk appetite) load it
only when the strategy uses them; `market.trend` (SPY only) is always recorded for the market-state breakdown.

## Simulation (`backtest.engine.simulate`)

Per session T: **open** — pending exits fill, then pending entries fill (selection-policy order); **close** — open
positions record T's high / low and count T as a holding day, exit rules are checked, entry rules are checked for
symbols with no open or pending position, positions are marked to T's close.

| rule | convention |
|---|---|
| entry | conditions true at the close of T → fill at the open of the next market session; if the symbol has no bar that session → `UNFILLED_ENTRY / NEXT_SESSION_BAR_MISSING`; signal on the last session → `UNFILLED_ENTRY / NO_NEXT_SESSION_IN_RUN` |
| same-close execution | impossible: enforced by the engine guard and by `CHECK (entry_fill_date > entry_signal_date)` in the database |
| sizing | equity at the fill-day open (cash + positions at that open, or last close if no bar) × `max_position_pct`; whole shares; capped by cash after the entry commission and one reserved exit commission per open position — cash can never go negative; no leverage, no margin, long only |
| contention | more signals than free slots → the central `select_order` policy: `ALPHABETICAL` (a neutral, documented tie-break, not a ranking). Positions waiting for an exit fill keep their slot. Skips are `ENTRY_SKIPPED / MAX_OPEN_POSITIONS`; cash failures `INSUFFICIENT_CASH`; a target below one share `TARGET_BELOW_ONE_SHARE` |
| slippage | entry fill = open × (1 + bps/10000); exit fill = open × (1 − bps/10000); commission is a separate fixed $ per order |
| exits | at a close: exit condition group OR invalidation OR target OR max holding; every true reason is stored; the displayed primary reason follows the contract order CONDITION, INVALIDATION, TARGET, MAX_HOLDING; fill at the symbol's next open (a late fill after missing bars is recorded as a delay); no later bar before the end → `UNFILLED_EXIT`, position stays open |
| entry-based levels | frozen at the entry signal: `CLOSE_BELOW_ENTRY_SUPPORT` = close < entry `stock.support`; `CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE` = close ≥ entry `stock.resistance`; `PCT_BELOW_ENTRY` = close ≤ fill × (1 − pct/100); `PCT_ABOVE_ENTRY` = close ≥ fill × (1 + pct/100), using the actual fill after slippage. A missing required level skips the entry (`REQUIRED_ENTRY_SUPPORT_UNAVAILABLE` / `REQUIRED_ENTRY_RESISTANCE_UNAVAILABLE`) |
| holding days | the symbol's own sessions; entry fill day = day 1; reaching `max_holding_days` at a close signals the exit there (fill next open); a market session without a bar for the symbol is neither evaluated nor counted |
| re-entry | never on the exit-signal close; first evaluated at the close after the exit filled |
| end of run | no forced liquidation: open positions are marked to the last close, included in equity, excluded from closed-trade statistics and listed as open |
| MFE / MAE | Stage 2.5 convention, clamped (MFE ≥ 0, MAE ≤ 0), relative to the actual entry fill; uses the entry day's full high/low, full ranges of later held days, and only the open of the exit day |

## Metrics, benchmarks, breakdowns

Formulas are in `backtest.metrics.FORMULAS` and shown in the UI. Profit factor is `N/A — no losing trades` when there
are no losers (never a huge number) and 0 when there are losses but no winners. Expectancy is defined as the average
closed-trade return % (= win rate × average winner + loss rate × average loser). Annualized return is shown only with
at least 252 sessions. Sample-size labels (`NO_TRADES`, `VERY_SMALL_SAMPLE` 1–9, `SMALL_SAMPLE` 10–29, 30+) describe
counts, not quality. Benchmarks are context only: SPY and an equal-weight universe bought at the first session's open
with the same cost model, whole shares, uninvested remainder held as cash at 0 %. Breakdowns use the existing
`market.trend` label at entry (and `market.environment` when the breadth list was loaded), symbol and exit reason
(`MULTIPLE` when more than one reason was true).

## Runs

User-triggered only. One run at a time, executed as a one-shot background thread in the server process (status
`PENDING → RUNNING → COMPLETED | FAILED`, persisted); the UI polls that one run until it finishes. There is no
scheduler. Large runs split the sessions across a one-shot local process pool (identical results to the in-process
path; set `BACKTEST_WORKERS=0` to disable). Results are written in the same transaction that marks the run
`COMPLETED`; a failure stores only the error code and message. A run that was interrupted by a server stop is marked
`FAILED / INTERRUPTED` the next time the database is opened. Completed and failed runs, their trades, signals and
equity rows are immutable (database triggers); re-running creates a new run. `config_hash` covers the strategy version
and hashes, registry, period, capital, costs, execution model, selection policy and data assumptions; `data_hash`
covers the exact cached bars.

## Known limitations

* Adjusted prices: split / dividend adjustment restates old prices with later corporate actions — absolute dollar
  thresholds in conditions reflect post-decision information (warned); dividends appear in adjusted prices, not as cash.
* IEX feed (default): prices and volume from one venue; volume-based features use IEX volume. Alpaca's IEX daily
  history is continuous only from 2020-07-27 (one isolated bar in 2018, then a 20-month hole — measured 2026-09-28), so
  the earliest start with a full warm-up window is late November 2020. A session-calendar hole of two or more weekdays
  blocks a run (`DATA_UNAVAILABLE`) instead of being treated as consecutive sessions.
* Survivorship / hindsight: the universe is chosen today; the breadth list is today's fixed 80 stocks; the sector map
  is today's static map.
* Daily bars only: no intraday stops, gaps fill at the open, early-close days are ordinary sessions.
* No historical research, news, earnings or macro-event data (those features are forward-only and cannot run here).
