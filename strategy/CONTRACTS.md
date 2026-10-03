# Stage 3 strategy contracts

Stage 3.1 defines strategies only. This file fixes what later stages consume and record so that no backtest,
signal or paper trade can silently use a changed strategy. Record types are in `strategy/contracts.py`
(definitions only — nothing is implemented).

## Identity

Every consumer references one immutable version by all three of:

| field | meaning |
|---|---|
| `strategy_id` | the strategy (32 hex) |
| `strategy_version_id` / `version_number` | the exact saved version (never updated or deleted; DB triggers) |
| `strategy_spec_hash` | SHA-256 of the canonical spec JSON (`strategy.spec.canonical_json`) |

A consumer MUST re-hash the stored `spec_json` and refuse to run if it differs from `spec_hash`
(`StrategyStore` reports this as `INTEGRITY_ERROR`). `rules_hash` identifies behaviour only (name, description and
universe origin excluded) — two versions with the same `rules_hash` have identical rules.

## Feature registry pinning

Each spec pins `feature_registry_version` and `feature_registry_fingerprint` (SHA-256 of every feature definition
AND the configuration thresholds it uses, e.g. the "extended" momentum %). A consumer MUST refuse to evaluate a
version whose fingerprint differs from the running registry unless it can load that exact registry version —
otherwise a threshold change would silently change the strategy.

## Stage 3.2 historical backtester — input contract

For each `(strategy version, symbol, evaluation day T)` the backtester receives / builds:

1. the validated spec (from the stored canonical JSON) and its hashes;
2. **daily bars through the close of T only** — never T+1 or later bars, later research, later news, later event
   revisions or any portfolio state;
3. a feature SNAPSHOT built only with the registry extractors (`strategy.features.stock_values`, `market_values`,
   `sector_values`) on objects computed by the EXISTING engine from those bars:
   * `analysis.indicators.compute_metrics(symbol, bars_through_T)` **without** a latest-trade override
     (price = the close of T);
   * `insights.market.build_market_insights` on `MarketInputs` whose indices / breadth basket / sector ETF changes
     come from bars through T, with `macro_events=[]` and `news=[]` (those inputs cannot be rebuilt, and the
     historical market features do not read them);
   * `analysis.sector_context` values from the sector ETF / market proxy bars through T;
4. evaluation of `entry` / `exit` groups ONLY through `strategy.evaluate.group_met` (a missing value never meets a
   condition; `between` is inclusive).

Timing, fixed by `execution` in the hashed spec: decisions use the close of T; any simulated entry or exit happens at
the **next session's open**. `exit.max_holding_days` counts **trading days** (sessions in the bar data).
Exit methods refer to values captured on the entry day: `CLOSE_BELOW_ENTRY_SUPPORT` uses `stock.support` at entry,
`CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE` uses `stock.resistance` at entry, `PCT_*` use the simulated entry fill.
An exit fires when the exit condition group is met OR invalidation OR target OR the maximum holding period.

Readiness gates what a backtester may run: only `BACKTEST_READY` versions. `FORWARD_TEST_ONLY` versions contain
features whose history cannot be reconstructed (research, events); `UNSUPPORTED` versions contain live-portfolio
features and must not be run by anything yet.

Stage 3.2 must never reinterpret a strategy: no default values, no feature substitutions, no re-ordering beyond
what the spec states. Anything it cannot evaluate is reported, not guessed.

## Future Alpaca PAPER bot — record contract (not implemented)

Every paper signal / trade must record (see `PaperSignalRecord` / `PaperTradeRecord`):
`strategy_id`, `strategy_version_id`, `strategy_spec_hash`, `symbol`, `signal_timestamp` (the close of T),
`feature_snapshot` (the exact values the decision used, with the registry fingerprint), `decision`
(ENTER / EXIT / HOLD / SKIP with the evaluation trace), `paper_order_id`, `paper_fill` (price, time, quantity),
`exit_reason` (condition / invalidation / target / max holding), and `outcome` (filled in only after the exit).

Paper trading is a later stage. Stage 3.1 adds no broker client, no order endpoint and no scheduler.
