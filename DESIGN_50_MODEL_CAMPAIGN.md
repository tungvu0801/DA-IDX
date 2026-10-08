# Stage 5.0 — Model Evaluation Campaign / Leaderboard (design, V1)

Research only. A campaign compares a BOUNDED set of deterministic Stage 4.7 rotation configurations with the Stage 4.8
replay and the Stage 4.9 walk-forward machinery, applies transparent eligibility gates, ranks a leaderboard and names 2–3
"paper-forward-test candidates". It evaluates existing configurations; it is not a strategy engine, not ML, not random or
Bayesian search, not deployment, not a recommendation and never a broker path. Nothing it produces is activated anywhere.

## 1. Flow

```
campaign definition (base config, universe, range, train/test/step, frequency, grid spec, gates, finalist_count)
  → candidates: rotation_walkforward.grid.generate (base + explicit + bounded grid; cap default 50, hard cap 100)
  → one bar load; Stage 4.9 windows (train / test / step)
  → per candidate, hold-out evidence: the candidate replayed on EVERY TEST window (Stage 4.8 via the Stage 4.9 Evaluator)
    → Stage 4.9 aggregate (medians, worst / mean drawdown, positive and benchmark-beating %, dispersion, turnover, stitched OOS)
    → cost sensitivity: the same TEST windows at 0/0 and 20/20 bps
    → parameter neighbourhood over the full range (Stage 4.9 rule) → fragile flag and fragility ratio
    → robustness score rs_v1 (Stage 4.9 formula, unchanged)
  → selection stability: ONE Stage 4.9 walk-forward across the whole candidate set (TRAIN-only selection) → times each
    candidate was frozen
  → eligibility gates → leaderboard ranking → top finalist_count eligible candidates (possibly none → NO_FINALIST)
  → comparisons (baseline vs finalists, finalist vs finalist, Pareto-style leaders) and a model card per finalist
  → append-only persistence
```
TEST data never alters candidate generation: the candidate set is fixed before any replay. Every candidate's full
evidence stays in the leaderboard whether eligible or not.

## 2. Eligibility gates (configurable, transparent, moderate defaults — NO_FINALIST is a valid outcome)

| gate | default | excluded when |
|---|---|---|
| min_windows | 3 | completed TEST windows < min_windows |
| max_failed_windows | 0 | FAILED test replays > max |
| max_drawdown_floor | −0.35 | worst OOS max drawdown < floor |
| min_positive_window_pct | 0.50 | positive windows / completed < min |
| min_benchmark_beating_pct | 0.40 | benchmark-beating windows / completed < min |
| max_turnover | 0.90 | mean OOS turnover per rebalance > max |
| max_fragility | 0.50 | fragility ratio > max, or the Stage 4.9 fragile flag is set |
| max_cost_sensitivity | 0.75 | cost sensitivity (Stage 4.9 definition) > max |

`fragility_ratio = max(0, Sharpe_base − min Sharpe_neighbour) / max(|Sharpe_base|, 1.0)` over valid neighbours of the
candidate's own neighbourhood (worst small change; the 1.0 floor keeps near-zero bases from exploding). Every gate failure is
recorded as an exclusion reason; a candidate may carry several.

## 3. Leaderboard order (deterministic)

1. eligible before ineligible · 2. robustness score desc · 3. median OOS Sharpe desc · 4. worst OOS max drawdown less severe ·
5. cost sensitivity lower · 6. fragility ratio lower · 7. mean OOS turnover lower · 8. config hash asc.
Full-history CAGR is shown for information only and never enters the order. Each row shows config hash, parameter summary, OOS
window count, median OOS CAGR / Sharpe / Sortino, worst OOS return, worst OOS drawdown, positive %, benchmark-beating %, median
excess, turnover, cost sensitivity, fragility, times selected by the walk-forward, robustness score, eligibility and reasons.

## 4. Finalists and model cards

The top `finalist_count` (default 3) eligible rows, in leaderboard order, stored as research records labelled
"paper-forward-test candidate". No deploy, activate, overwrite, schedule or order action exists. Pareto-style flags across the
candidates: return leader (median OOS CAGR), drawdown leader (least severe worst drawdown), lowest turnover, most stable
(lowest fragility, then dispersion). No single "best strategy" label beyond the leaderboard order.
Model card per finalist: config hash, factor weights, portfolio / rank rules, historical interval and windows, universe and
its limitation, OOS metrics, robustness metrics, weak regimes (worst regime by return from the Stage 4.9 breakdown of its
stitched OOS curve), cost sensitivity, fragility, known limitations, why it qualified (gates passed with values), what would
invalidate it during paper-forward testing (gate thresholds restated as live invalidation triggers). No buy / sell signal.

## 5. Storage (additive, immutable)

`model_campaigns` (one run: definition + hash, versions, counts, status, result hash) · `model_campaign_candidates` (every
candidate with its full evidence JSON) · `model_campaign_leaderboard` (ordered rows with eligibility and reasons) ·
`model_campaign_finalists` (ordered finalists, label "paper-forward-test candidate") · `model_campaign_model_cards` ·
`model_campaign_metrics` (comparisons, Pareto flags, walk-forward stability, conventions). `CREATE … IF NOT EXISTS`,
no-update / no-delete triggers, `user_version` untouched.

## 6. API / UI

`/api/model-campaign` (no `order`, `trade`, `backtest`, `paper`, `execute`, `broker`, `deploy` in any path): `GET /config`,
`POST /run`, `GET /runs`, `/runs/{id}`, `/runs/{id}/leaderboard`, `/runs/{id}/finalists`, `/runs/{id}/cards`,
`/runs/{id}/comparison`. UI: a "Model Campaign" workspace in the Strategy Lab with eligibility, leaderboard, finalist
comparison, model cards and exclusion reasons. No deploy, broker, handoff or "trade this model" control.

## 7. Modes

Synthetic / test (fake bars) and a user-triggered real historical campaign through the existing read-only bar layer
(≤ 1 batched market-data request, explicit user action only). Tests use synthetic data exclusively.

## 8. Limitations and deferrals

Static universe supplied today (not survivorship-bias-free) on every result. Deferred: point-in-time universes, multi-level
grids, search of any kind, ML, scheduled campaigns, charts, automatic promotion of finalists.
