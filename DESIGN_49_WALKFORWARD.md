# Stage 4.9 — Walk-Forward + Robustness Evaluation (design, V1)

Research only, built on Stage 4.8. It asks whether a Stage 4.7 rotation configuration keeps working on UNSEEN historical
periods, how stable the answer is across windows, how sensitive it is to nearby parameters and to costs, and how it
behaves across simple market regimes. It never trades, never deploys, never switches a live configuration, never calls
a broker or a model, and never tunes anything automatically beyond a bounded, explicit, deterministic candidate grid.

## 1. Core principle — train only, then test only, then advance

For every window the candidates are evaluated on TRAIN data only (Stage 4.8 replay restricted to the train dates), ranked
with TRAIN metrics only, ONE configuration is frozen, and only then is it replayed on the TEST dates. The test result never
feeds back into any selection, tie-break or score of any window. Test windows are non-overlapping by default
(`step_months == test_months`); an overlapping mode (`step_months < test_months`) is allowed but flagged in the run
(`overlapping_tests = true`) and the stitched out-of-sample curve and its aggregate return metrics are then NOT produced.

```
bars (Stage 3.2 layer, one load)  →  windows (train / test / step in months on the SPY calendar)
  for each window:  candidates × Stage 4.8 replay on TRAIN  →  rank (TRAIN metrics, deterministic tie-breaks)  →  freeze 1 config
                    →  Stage 4.8 replay of the frozen config on TEST  →  store train + test metrics separately
  across windows:   median / worst / dispersion of TEST metrics, positive and benchmark-beating window %, selection stability,
                    stitched OOS equity (non-overlapping only), cost matrix, parameter neighbourhood, regime breakdown,
                    robustness score (documented, not a probability)
```

## 2. Windows

Calendar months on the SPY session calendar. Window k: `train_start = start_date + k·step_months`, `train_end = train_start +
train_months − 1 day`, `test_start = train_end + 1 day`, `test_end = test_start + test_months − 1 day`. A window exists only
when `test_end ≤ end_date`. Each boundary is stored as the calendar date AND the first / last SPY session inside it; a window
with fewer than `min_train_sessions` train sessions or `min_test_sessions` test sessions makes the run FAIL
(`INSUFFICIENT_TRAIN_DATA` / `INSUFFICIENT_TEST_DATA`) — nothing is shortened silently. Zero windows → `NO_WINDOWS`.
The replay of a window uses Stage 4.8 unchanged: signals at completed closes inside the window, fills at the next session's
open, factors from bars ≤ T (history before the window is allowed — it is the past). A test replay starts from
`initial_cash`; nothing from the train replay (holdings, cash) carries over.

## 3. Candidates (bounded, explicit, deterministic)

* `candidates`: explicit Stage 4.7 configuration dicts, validated by the existing `rotation.store.normalise_config`.
* `grid`: a bounded one-level grid around the base configuration: `dimensions = {field: [values]}` over
  `portfolio_size`, `exit_rank`, `cash_buffer_pct`, `max_turnover_per_rotation`, `rebalance_threshold`,
  `weights.<key>` (one weight set to a value, the other five rescaled proportionally so the sum stays exactly 1.000000 at
  6 dp, residual to the largest other weight). The cartesian product is enumerated in a fixed order (dimensions sorted,
  values in the given order), every candidate is validated, invalid ones are REJECTED WITH THE REASON (never silently
  dropped), duplicates (same config hash) are removed keeping the first, and the list is cut at `max_candidates`
  (default 20, hard cap 100) with the number of discarded candidates recorded. No random search.
* Every candidate is identified by its Stage 4.7 `config_hash`; nothing is written to `portfolio_rotation_configs`.

## 4. TRAIN selection

`selection_metric` ∈ SHARPE (default) · SORTINO · CAGR · DD_CONSTRAINED_SHARPE (Sharpe, but a train max drawdown worse than
`max_drawdown_limit` (default −0.25) ranks below every unconstrained candidate) · COMPOSITE (0.5·clamp(Sharpe/2) +
0.25·clamp(1 + max_dd/0.5) + 0.25·clamp(1 − mean turnover)). Ordering: metric desc (None last) → max drawdown higher (less
negative) → mean turnover lower → config_hash asc. Only TRAIN metrics enter.

## 5. Out-of-sample aggregation

Per test window: CAGR, Sharpe, Sortino, max drawdown, total return, benchmark total return, excess, mean turnover, costs.
Across windows: medians (CAGR, Sharpe, Sortino, excess), worst and mean max drawdown, worst window return, std of window
returns, positive-window %, benchmark-beating %, turnover std, number of distinct selected configs, selection changes,
parameter drift (per numeric field: number of changes and range across windows). Stitched OOS equity (non-overlapping only):
chain-linked window curves `E(t) = E_prev_end × equity_w(t) / initial_cash`, then the Stage 4.8 metrics on the stitched
curve. Overlapping windows are never concatenated.

## 6. Robustness score (`rs_v1`, documented, deterministic, not a probability)

Inputs clamped to [0, 1]: `s_sharpe = clamp(median_oos_sharpe / 2)`, `s_pos = positive_window_pct`, `s_beat =
benchmark_beating_pct`, `s_dd = clamp(1 + worst_oos_max_drawdown / 0.5)`, `s_turn = clamp(1 − mean_oos_turnover)`,
`s_stab = 1 − (distinct_selected − 1) / max(windows − 1, 1)`, `s_cost = clamp(1 − cost_sensitivity)` where
`cost_sensitivity = clamp((CAGR_0bps − CAGR_20bps) / max(|CAGR_0bps|, 0.01))`.
`score = 0.30·s_sharpe + 0.20·s_pos + 0.15·s_beat + 0.15·s_dd + 0.10·s_turn + 0.05·s_stab + 0.05·s_cost`. Favors
consistency over peak return, penalises drawdown, turnover, instability and cost fragility. Reported with every component.

## 7. Parameter sensitivity (descriptive)

Around the base configuration, one dimension at a time, within a fixed neighbourhood (`portfolio_size ±1`, `exit_rank ±1`,
`cash_buffer_pct ±0.05`, `max_turnover_per_rotation ±0.10`, each weight ±0.05 with proportional rescaling). Each neighbour
and the base are replayed over the full run range with Stage 4.8 (same costs); reported: metric delta (Sharpe, CAGR, max
drawdown), rank of the base among neighbours, and `fragile = true` when the median Sharpe deterioration over valid
neighbours exceeds 50 % of the base Sharpe (or the base Sharpe is positive and the median neighbour Sharpe is ≤ 0). Report
only; nothing is rejected or deployed.

## 8. Cost sensitivity

Matrix (cost bps / slippage bps): 0/0, 2.5/2.5, 5/5, 10/10, 20/20. The frozen per-window selections are replayed on their
TEST windows under each pair; reported per pair: median OOS CAGR, median OOS Sharpe, stitched OOS CAGR, and the first pair
at which the stitched OOS return turns non-positive (`break_even_hint`), if any. No extrapolation.

## 9. Regimes (descriptive, no look-ahead)

Labels per SPY session from bars ≤ that session: TREND_UP when close > SMA200, TREND_DOWN otherwise; HIGH_VOL / LOW_VOL
when the trailing 20-session annualised realised volatility is above / below 0.20 (a pre-defined threshold). A day's
out-of-sample return is attributed to the regime known at the PREVIOUS session's close. Per regime over the stitched OOS
curve: sessions, compounded return, Sharpe (≥ 20 sessions), max drawdown, mean exposure (1 − cash weight), mean turnover of
rebalances whose signal session carried that label.

## 10. Storage (additive, immutable)

`portfolio_walkforward_configs`, `_runs`, `_windows`, `_candidates`, `_selections`, `_oos_results`, `_sensitivity`,
`_metrics`: `CREATE … IF NOT EXISTS`, no-update / no-delete triggers, `user_version` untouched, canonical JSON, 32-hex ids,
64-hex hashes. A run stores the Stage 4.8 and 4.9 versions, the walk-forward definition hash, candidate hashes and
generation spec, universe hash, data hash, windows, selection rule, costs, robustness formula version, evaluation counts.

## 11. API / UI

`/api/rotation-walkforward` (no `order`, `trade`, `backtest`, `paper`, `execute`, `broker` in any path): `GET /config`,
`POST /run`, `GET /runs`, `/runs/{id}`, `/runs/{id}/windows`, `/runs/{id}/oos`, `/runs/{id}/sensitivity`,
`/runs/{id}/regimes`. UI: a "Walk-Forward" workspace in the Strategy Lab. No broker controls, no handoff, no deploy.

## 12. Static universe

Everything inherits the Stage 4.8 limitation: a static universe supplied today; not survivorship-bias-free. Every run,
API response and the UI carry the note.

## 13. Deferred

Point-in-time universes, train-derived regime thresholds, multi-level grids, random / Bayesian search, ML, automatic
promotion of candidates, scheduled runs, charts.
