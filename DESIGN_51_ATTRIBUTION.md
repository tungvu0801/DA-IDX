# Stage 5.1 — Attribution & Benchmark Diagnostics (design, V1)

Diagnostics only. For configurations already evaluated by a Stage 5.0 campaign (referenced by the campaign's immutable id
and the candidates' config hashes), answer whether the rotation logic adds value versus simply owning the same static
universe. Nothing here changes Stage 4.7 signals, Stage 4.8 execution, Stage 4.9 selection or Stage 5.0 gates; nothing
tunes, promotes, deploys or trades. Research flags are not recommendations.

## 1. Reuse

* Replays: `rotation_walkforward.engine.Evaluator` (Stage 4.8 `simulate` + metrics, memoised).
* Windows: the campaign's own train / test / step geometry rebuilt with `rotation_walkforward.windows.build_windows`
  (same start, end, months, minimums) — the TEST windows are the OOS intervals of every diagnostic.
* Aggregation / stitching: `rotation_walkforward.robustness.aggregate` (chain-linked stitched OOS curve).
* Regimes: `rotation_walkforward.regimes.labels` (SPY SMA200 trend, 20-session vol, previous-close attribution).
* Bars: one `fit.current.load_bars` call for the campaign universe + SPY (≤ 1 batched market-data request).

## 2. Benchmarks (same universe, same OOS windows, same data, same execution convention)

Both universe benchmarks are Stage 4.8 replays of a derived Stage 4.7 configuration, so they use the identical
next-open fills, slippage, costs, whole shares, cash buffer and eligibility rules (min price, min dollar volume, min history):
* **EW_REBALANCED** — `portfolio_size = exit_rank = N` (N = universe size): every eligible symbol is selected at equal weight
  `(1 − cash_buffer)/N`; the base configuration's `rebalance_threshold` keeps weights near equal at every rebalance (same
  frequency as the tested strategy). `min_position_weight = 0`, `max_position_weight = 1`, `max_turnover = 1`.
* **BUY_HOLD** — the same, but `rebalance_threshold = 1.000000`: after the initial equal-weight deployment nothing is
  increased or decreased. The existing rules still apply deterministically: a name that becomes ineligible (no bar, below
  min price / liquidity) is sold at the next rebalance and a name that becomes eligible later is added at the equal target —
  no hindsight removal of laggards.
* **SPY** — the Stage 4.8 `benchmark_index` (buy-and-hold index of SPY from the first session), now also carrying max
  drawdown, drawdown duration and worst month.

## 3. Attribution (per selected configuration, from the deterministic TEST replays)

Symbol P&L = Σ(sell notional − cost) − Σ(buy notional + cost) + final quantity × last close, per window, summed across windows;
contribution share = positive P&L of the symbol / total positive P&L; top-1 / 3 / 5 concentration; sector P&L from an explicit
research sector map stored with the run (its sha256 is recorded; unmapped symbols → `UNMAPPED`); sector exposure per rebalance
from the proposal target weights → average sector weights and the maximum sector weight at any rebalance; holdings overlap
with the previous rebalance `|A∩B| / |A|`, names added / removed, churn `(added + removed) / (2·N)`; return by OOS window and
best-window share of total log return. Totals reconcile: Σ symbol P&L = Σ sector P&L = final equity − initial cash of every window.

## 4. Leave-one-out and leave-sector-out

The selected configuration is replayed on the same TEST windows with the universe minus one symbol (or minus one sector), same
costs, no re-selection; deltas vs the full universe for stitched OOS CAGR, Sharpe, max drawdown and excess vs SPY. Flags
(`dx_v1`): `DOMINANT_CONTRIBUTOR` when removing one symbol cuts stitched CAGR by ≥ 25 % of its absolute value or turns the
stitched excess vs SPY from positive to non-positive; `SECTOR_DEPENDENCE` with the same rule per sector.

## 5. Cost-adjusted excess

Cost points 0/0, 5/5, 10/10, 20/20 bps for the strategy and both universe benchmarks (SPY carries no cost): stitched total
return, CAGR, Sharpe, max drawdown, excess vs SPY and vs EW_REBALANCED at the same cost point; first cost point where each
excess is non-positive. No extrapolation.

## 6. Drawdown, windows, regimes

Max drawdown, drawdown duration (sessions) and worst month for strategy, EW, BH and SPY on the stitched curves. Per window:
returns of the four, excess vs each, turnover, dominant contributor, trend / vol regime mix; % windows beating each benchmark,
median excess vs each, worst relative window. Regime-conditioned excess over the stitched curves with the Stage 4.9 labels:
return, benchmark return, excess, exposure, turnover, sessions; `WEAK_REGIME_SAMPLE` when a regime has < 60 sessions or < 10 %
of the OOS sessions.

## 7. Flags and scorecard (`dx_v1`, documented thresholds)

BENCHMARK_UNDERPERFORM (stitched return < EW) · ROTATION_VALUE_ADDED (beats SPY, EW and BH stitched, beats EW in ≥ 60 % of
windows, excess vs EW still > 0 at 10/10 bps, no DOMINANT_CONTRIBUTOR, no SECTOR_DEPENDENCE, top-3 share ≤ 50 %) · UNIVERSE_ALPHA_DOMINANT (EW beats SPY and the EW − SPY
excess exceeds the strategy − EW excess) · SYMBOL_CONCENTRATION_HIGH (top-3 share > 0.50) · SECTOR_CONCENTRATION_HIGH (top
sector share of positive P&L > 0.50 or max sector weight > 0.50) · COST_FRAGILE (excess vs EW or vs SPY ≤ 0 at or before 10/10) ·
DRAW_DOWN_NOT_IMPROVED (strategy max drawdown not better than EW) · WINDOW_CONCENTRATION_HIGH (best window > 0.50 of total log
return) · SELECTION_UNSTABLE (campaign distinct selections / windows > 0.60) · WEAK_REGIME_SAMPLE · DOMINANT_CONTRIBUTOR ·
SECTOR_DEPENDENCE. Scorecard rows (PASS / WARN / FAIL): beats SPY, beats EW, beats BH, improves drawdown, survives 10 bps,
survives 20 bps, top-3 share, sector concentration, leave-one-out stability, leave-sector-out stability, regime evidence,
selection stability. No single composite score.

## 8. Storage, API, UI

Eight append-only tables `rotation_diagnostic_*` (runs, benchmarks, symbol_attribution, sector_attribution, leave_one_out,
leave_sector_out, windows, scorecards), triggers, `user_version` untouched; the run stores the source campaign id and hash,
config hashes, benchmark definitions, date range, universe and sector-map hashes, costs, flags and a result hash.
`/api/rotation-diagnostics` (config, run, runs, run detail, benchmarks, attribution, ablation, windows, regimes, scorecard);
"Diagnostics" workspace in the Strategy Lab. No broker, handoff or deploy control.

## 9. Limitations
Static universe supplied today (not survivorship-free); OOS windows few; benchmarks share the strategy's eligibility rules by
design; recovery time beyond drawdown duration is deferred.
