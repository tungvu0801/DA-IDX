# Stage 4.8 + 4.9 — research stack acceptance and freeze record

Frozen code: branch stage-49 @ **213d28d** (manifest `FROZEN_49_RESEARCH_MANIFEST.txt`, sha256 per git blob of every tracked file).
Stage 4.8 commit 34d4a1f (stage-48), Stage 4.9 commit 213d28d (stage-49, on top of stage-48). Freeze artifacts are committed on
top as "Freeze Stage 4.8 and 4.9 research stack". Earlier freeze records are preserved unchanged: Stage 4.6B
(`s46b/freeze/frozen_46b_manifest.txt`, main 6e65e5a) and Stage 4.7 MVP (`scratchpad/s47/freeze/`, tree 59fb134, main 729c45f).

## Scope

Research only. Neither stage trades, previews or prepares an order, touches Stage 4.6A/4.6B, reads Robinhood, calls a model,
activates a configuration or schedules anything. Both carry the static-universe limitation on every run, API response and UI.

### Stage 4.8 — Historical Rotation Backtest (`rotation_backtest/`, `DESIGN_48_HISTORICAL_BACKTEST.md`)
* **One strategy engine**: every rebalance decision comes from the Stage 4.7 `rotation.engine.compute_rotation`, fed bars truncated to
  the signal session T and a snapshot built from the simulated cash and holdings. No second ranking or allocation code.
* **Execution convention**: signal at the completed close of T; fills at the OPEN of the next benchmark session; SELLs first
  (alphabetical) then BUYs in rank order; BUY fill = open × (1 + slippage bps), SELL fill = open × (1 − slippage bps), 4 dp;
  transaction cost = notional × cost bps, debited separately (nothing counted twice); whole shares, residual cash kept; a BUY the
  cash cannot fund is cut to the affordable shares (CASH_LIMITED); no shorting, no leverage. Non-tradable engine statuses
  (turnover breach, no eligible names, data errors) skip the whole rebalance, as Stage 4.7 does.
* **No future leakage**: the engine only ever receives bars dated ≤ T; factor windows end at T; a signal never fills at T's close;
  a signal on the last session trades nothing (NO_NEXT_SESSION). Tests mutate every bar after T and assert the proposal at T is
  byte-identical; fills are asserted to equal the next open ± slippage and never a close.
* **Fail closed**: a held symbol without a close on a marking day, a pending symbol without a bar on the execution day, a
  benchmark gap, a non-positive price or an empty calendar makes the run FAILED; prices are never interpolated or carried forward.
* **Calendar**: SPY sessions; WEEKLY = first session of each ISO week, MONTHLY = first session of each calendar month.
* **Metrics** (`rotation_backtest/metrics.py`, conventions stored with each run): total return, CAGR (252 sessions), annualised
  volatility, Sharpe and Sortino (rf 0), max drawdown and duration, SPY buy-and-hold benchmark, excess and simple annualised excess,
  turnover, costs, win rate and holding period of completed positions, best / worst month, worst rolling 3 months, cash weight.
* **Storage**: six append-only tables (`portfolio_backtest_*`), immutability triggers, `user_version` untouched; definition, data and
  result hashes make a run reproducible. API `/api/rotation-replay` (the words `trade` and `backtest` are reserved by prior-stage
  route rules); workspace "Rotation Backtest".

### Stage 4.9 — Walk-Forward + Robustness (`rotation_walkforward/`, `DESIGN_49_WALKFORWARD.md`)
* **TRAIN / TEST separation**: window k trains on `[start + k·step, + train_months)` and tests on the following `test_months`; the
  candidates are replayed on TRAIN only (Stage 4.8), ranked with TRAIN metrics only, ONE configuration is frozen, and only then
  replayed on the unseen TEST window. Test metrics never enter any ranking, tie-break or score. Tests rebuild each window's TRAIN
  ranking from a world that ends at the train boundary and assert the same selection; mutating data after the last train boundary
  changes only the last TEST result. Non-overlapping tests by default (`step = test`); overlapping mode is flagged and never stitched.
* **Candidates**: the base, explicit candidates and a bounded one-level grid (portfolio_size, exit_rank, cash_buffer_pct,
  max_turnover, rebalance_threshold, single weights with proportional rescaling to exactly 1.000000), validated by the Stage 4.7
  rules, invalid rejected WITH reason, duplicates removed by config hash, fixed order, cap default 20 / hard cap 100. No random search.
* **Selection**: SHARPE (default), SORTINO, CAGR, DD_CONSTRAINED_SHARPE, COMPOSITE; tie-breakers: metric, less negative max
  drawdown, lower turnover, config hash. Failed train replays rank last.
* **OOS aggregation**: medians (CAGR, Sharpe, Sortino, excess), worst / mean max drawdown, positive and benchmark-beating window %,
  worst window return, dispersion, turnover stability, distinct selections and changes, parameter drift, chain-linked stitched OOS
  curve (non-overlapping only) with Stage 4.8 metrics.
* **Robustness score `rs_v1`** (not a probability): 0.30·clamp(median OOS Sharpe/2) + 0.20·positive % + 0.15·benchmark-beating % +
  0.15·clamp(1 + worst OOS drawdown/0.5) + 0.10·clamp(1 − mean turnover) + 0.05·selection stability + 0.05·clamp(1 − cost
  sensitivity), cost sensitivity = clamp((CAGR 0 bps − CAGR 20 bps) / max(|CAGR 0 bps|, 0.01)).
* **Cost matrix**: 0/0, 2.5/2.5, 5/5, 10/10, 20/20 bps on the frozen selections' TEST windows; first non-positive stitched return hinted.
* **Parameter sensitivity**: one dimension at a time around the base (size ±1, exit rank ±1, buffer ±0.05, turnover cap ±0.10, each
  weight ±0.05); fragile when the median Sharpe deterioration exceeds half the base Sharpe. Report only.
* **Regimes**: TREND_UP when SPY close > SMA200, HIGH_VOL when trailing 20-session annualised vol > 20 %; labels from bars ≤ the
  session; a day's return carries the previous close's label. Descriptive only.
* **Storage**: eight append-only tables (`portfolio_walkforward_*`), triggers, `user_version` untouched; the run stores the 4.8 and
  4.9 versions, definition / data / result hashes, windows, candidate hashes, selection rule, costs, score version. API
  `/api/rotation-walkforward`; workspace "Walk-Forward".

## Static universe limitation
Both stages replay a universe supplied today. Historical index membership is unknown, so results are NOT free of survivorship bias
and are historical simulations, not forecasts. The note is on every run row, every API response and both workspaces.

## Final evidence (stage-49 @ 213d28d, re-run at freeze time)

| Suite | Result |
|---|---|
| Stage 4.8 `tests/test_portfolio_backtest_48.py` | 22 passed |
| Stage 4.9 `tests/test_walkforward_49.py` | 17 passed |
| Stage 4.7 P1–P5 (26 + 17 + 29 + 17 + 11) · research workflow 14 | 114 passed |
| Stage 4.6A 33 · Stage 4.6B 83 · UTC regression 3 · Stage 4.5 23 | 142 passed |
| Regression isolation 9 · Stage 3.1 route rules · AI gating 27 + 21 · harness pins 7 | passed |
| Focused run (one pytest invocation) | 401 passed |
| rh_gateway | 149 passed |
| Full suite | 1107 passed, 1 warning in 504.98s  |
| Browser harness default sequence | PASS, 385 checks, 0 errors |
| Browser harness portfolio_rotation flow | PASS, 28 checks, 0 errors |
| Protected files | 21 Stage 4.6A/4.6B files byte-identical to the 4.6B freeze + `paper/alpaca_orders.py` at the approved pin 391cd2f2 |

Golden evidence: the Stage 4.8 golden replay (fills 339 AAA @ 139.9199 and 819 BBB @ 57.9590, final equity 102929.52, result hash
60693fac…) and the Stage 4.9 golden walk-forward (selections BBB, BBB, base, CCC, base predicted by hand; positive 60 %, beating 40 %;
score 0.639; result hash af77fe28…) are pinned in the tests.

## Deferred
Point-in-time universes, dividends as cash, cash interest, fractional shares, charts, train-derived regime thresholds, multi-level
grids, random / Bayesian search, ML, automatic promotion of candidates, scheduled runs, harness flows for the two new workspaces.
