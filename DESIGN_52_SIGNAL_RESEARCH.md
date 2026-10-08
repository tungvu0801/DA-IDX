# Stage 5.2 — Deterministic Signal Research (design, V1)

Signal research, not deployment. Research variants of the Stage 4.7 signal are evaluated through the existing Stage 4.8
replay, Stage 4.9 windows / stitching / regimes and Stage 5.1 benchmarks, attribution, cost grid, flags and scorecard.
The production Stage 4.7 baseline is never mutated: variants live in the `signal_research` namespace, are explicit,
versioned (`sr_v1` rules, `rf_v1` flags, `sc_v1` criteria, `fv_v1` factor verdicts), hashed and point-in-time safe. The
task succeeds even when every variant fails; `NO_SIGNAL_IMPROVEMENT` is a valid outcome. No ML, no random or adaptive
search, no broker, no model, nothing activated.

## 1. Reuse and the single hook

* Factors, eligibility E1–E10, scores and the production composite come from `rotation.engine.compute_rotation`
  (unchanged). The research engine (`signal_research.variant_engine.compute_variant`) calls it first and only then
  re-ranks, caps and scales the proposal with the same `rotation.rules` primitives (allocate, classify, turnover …).
  With `GLOBAL_RANK`, no cap and `NO_OVERLAY` the variant reproduces the production proposal exactly (pinned by a test).
* `rotation_backtest.simulator.simulate` gains ONE optional keyword `engine` (default: the production engine). Fills,
  costs, slippage, whole shares, cash accounting and the Stage 4.8 conventions are untouched; a research replay differs
  only in the proposal function. `signal_research.variant_engine.ResearchEvaluator` extends the Stage 4.9 `Evaluator`
  and passes the hook for candidates that carry `research` rules; benchmark candidates still use the production engine.
* Windows: the source campaign's geometry (`rotation_walkforward.windows.build_windows`); stitching, regimes, metrics,
  benchmarks (`rotation_diagnostics.benchmarks`), attribution and the Stage 5.1 flags / scorecard are reused as is.

## 2. Factor ablation (Part A)

Weights are the six Stage 4.7 keys (drawdown stays 0). Remove-one variants set one key to 0 and rescale the others
proportionally with `rotation_walkforward.grid.rescale_weights` (6 dp, ROUND_DOWN, residual to the largest remaining
weight): the result sums to exactly 1.000000 and the exact weights are stored. Subset controls keep a set of keys and
rescale them proportionally the same way (`keep_weights`); single-factor controls are `1.000000` on one key.
Variants: FULL baseline; no_momentum, no_trend, no_relative_strength, no_volatility, no_liquidity; momentum_only,
trend_only, relative_strength_only, momentum+trend, momentum+relative_strength, trend+relative_strength (12).

Factor verdict (`fv_v1`), remove-X versus the baseline on the same TEST windows: five comparisons — stitched CAGR
(tolerance 0.01), median OOS Sharpe (0.05), stitched max drawdown (0.01), median excess vs EW (0.005), % windows beating
EW (any difference). `FACTOR_HELPFUL` when removing the factor is worse on ≥ 3 comparisons and better on ≤ 1;
`FACTOR_HARMFUL` when better on ≥ 3 and worse on ≤ 1; otherwise `FACTOR_NEUTRAL`. No single metric decides.

## 3. Sector rules (Part B)

A static research sector map (explicit symbol → sector, upper-cased, sha256 of the canonical JSON stored with the run;
unmapped symbols form the sector `UNMAPPED`). No external classification, no time-varying membership.

* `GLOBAL_RANK` — the production order (composite desc, relative-strength score desc, liquidity score desc, ticker asc).
* `SECTOR_NEUTRAL_RANK` — within each sector the eligible names are ordered by the production order; the within-sector
  percentile is `(n − position) / (n − 1) × 100` (position 1 = best; a one-name sector scores 50), 6 dp. The global
  order is percentile desc, then production composite desc, liquidity score desc, ticker asc. Selection, rank buffer
  and equal weights then run unchanged on that order, so the top of every sector competes first.
* `SECTOR_CAP` (`max_sector_weight` ∈ {0.25, 0.30, 0.35} researched) — the ranking is unchanged; construction allows at
  most `cap_count = floor(max_sector_weight / equal_weight)` names per sector (equal_weight = (1 − cash_buffer) /
  portfolio_size). Holdings are retained in rank order while their rank ≤ exit_rank, slots remain and the sector is not
  full (`EXIT_SECTOR_CAP` otherwise); free slots are filled by the highest-ranked non-holdings whose sector is not full
  (`TOP_N_SECTOR_CAP` marks a name that entered because a higher-ranked name was blocked). Nothing is redistributed: when
  the cap leaves slots empty that the uncapped selection would have filled, the proposal status is
  `SECTOR_CAP_INFEASIBLE` (not tradable — the rebalance is skipped and counted). A cap below the equal weight or a map
  with too few sectors (`n_sectors × cap_count < portfolio_size`) is refused when the variant is built.

## 4. Regime exposure overlay (Part C)

Labels are the Stage 4.9 ones (SPY close vs SMA200; trailing 20-session annualised vol vs 0.20), computed from SPY bars
dated ≤ T only (the engine receives bars truncated at T and truncates again). Applied after ranking and allocation:
every selected name's equal weight is multiplied by the schedule's exposure for the label pair at T (6 dp), the
remainder is cash. Exposure ∈ [0, 1]: no leverage, no shorting, names stay equal-weighted. `UNKNOWN` labels (fewer
than 200 / 21 closes) mean exposure 1.0 — no information, no timing.
* `NO_OVERLAY` — 1.00 everywhere.
* `SIMPLE_RISK_OFF` — TREND_UP+LOW_VOL 1.00 · TREND_UP+HIGH_VOL 0.75 · TREND_DOWN+LOW_VOL 0.50 · TREND_DOWN+HIGH_VOL 0.25.
* `BINARY_TREND_FILTER` — TREND_UP 1.00 · TREND_DOWN 0.50 (either vol).
The schedule is a research input stored with the variant; it is never fitted.

## 5. Variant set (Part D) — at most 20, fixed order, no search

1 baseline · 2–6 remove-one · 7–12 subset controls · 13 sector_neutral · 14–16 sector_cap 0.25 / 0.30 / 0.35 ·
17 simple_risk_off · 18 binary_trend_filter · 19 sector_neutral + simple_risk_off · 20 sector_cap_0.30 + simple_risk_off.
The adaptive "best ablation" combinations are NOT run: the fixed set already reaches the hard cap of 20, and fixed,
named variants are the brief's stated alternative. Every variant hash = sha256(canonical {config, research rules}).

## 6. Evaluation per variant (same windows, data, costs and benchmarks as Campaign 2)

Stitched OOS curve at the run costs; per-window OOS CAGR / Sharpe / Sortino / max drawdown (median, worst, % positive);
windows vs SPY / EW_REBALANCED / BUY_HOLD (% beating, median excess, worst relative window); cost grid 0/0, 5/5, 10/10,
20/20 with excess vs EW and SPY at each point; attribution (symbol / sector P&L shares, top-1/3/5, sector weights,
holdings overlap and churn); regime-conditioned excess; Stage 5.1 flags and scorecard. Ablations are bounded to keep
the run inside budget: leave-one-out for the two largest positive contributors and leave-sector-out for the largest
sector (documented in the run). "Selection stability" for a research variant is holdings churn versus the baseline
variant (the Stage 5.0 TRAIN-selection notion needs a candidate family per window and is not re-run here).

## 7. Improvement criteria (`sc_v1`, predeclared, never relaxed)

All must hold for `IMPROVEMENT_CRITERIA_MET`: median excess vs EW ≥ 0 · windows beating EW ≥ 60 % (3/5) · median OOS
Sharpe ≥ EW's median OOS Sharpe − 0.10 · stitched max drawdown ≥ EW's − 0.02 · excess vs EW at 10/10 bps > 0 · top-3
share ≤ 0.50 · no SECTOR_DEPENDENCE · no DOMINANT_CONTRIBUTOR · TREND_DOWN and HIGH_VOL excess vs EW ≥ the baseline's −
0.02 · churn ≤ baseline churn + 0.05. Run-level `NO_SIGNAL_IMPROVEMENT` when no variant meets them.

## 8. Research flags (`rf_v1`) — relative to the baseline variant B or the overlay-free counterpart C

EW_STILL_DOMINANT (EW total return and Sharpe ≥ the variant's) · SPY_STILL_DOMINANT (same vs SPY) ·
CONCENTRATION_REDUCED (top-3 share or top-sector share ≤ B − 0.10) · COST_ROBUSTNESS_IMPROVED (excess vs EW at 20/20 ≥
B + 0.01) · DRAWDOWN_IMPROVED (max DD ≥ B + 0.02) · RETURN_SACRIFICED_FOR_RISK (DRAWDOWN_IMPROVED and CAGR ≤ B − 0.02) ·
SECTOR_NEUTRALITY_HELPFUL (sector-neutral ranking: top-sector share ≤ B − 0.10 and median Sharpe ≥ B − 0.05) ·
SECTOR_CAP_BINDING (the cap changed a selection or made one infeasible at least once) · REGIME_OVERLAY_HELPFUL (max DD ≥
C + 0.02, Sharpe ≥ C − 0.05, TREND_DOWN excess vs EW ≥ C's − 0.01) · REGIME_OVERLAY_OVERDEFENSIVE (CAGR ≤ C − 0.03 without a 0.02
drawdown gain) · FACTOR_HELPFUL / FACTOR_HARMFUL (the removed factor's verdict) · IMPROVEMENT_CRITERIA_MET ·
NO_SIGNAL_IMPROVEMENT (run level).

## 9. Storage, API, UI

Eight append-only tables `signal_research_*` (runs, variants, factor_ablation, sector_tests, regime_tests,
combinations, benchmark_comparisons, scorecards) with no-update / no-delete triggers; `user_version` untouched. The
run stores the campaign id / hash, the Stage 5.1 diagnostic run ids found for that campaign, the sector map and hash,
exact weights and rules per variant, benchmark metrics, flags, criteria and a result hash. `/api/signal-research`
(config, run, runs, run detail, ablation, sector, regime, combinations, benchmarks, scorecard) and a "Signal Research"
Strategy Lab workspace. No execution control, no handoff, no deploy, no broker.

## 10. Limitations

Five OOS windows; 2022–2026 is mostly TREND_UP / LOW_VOL, so overlay evidence rests on ~55 sessions; the universe is a
static list supplied today (survivorship); ablations are bounded (top-2 symbols, top sector); subset controls are
diagnostics, not candidates.
