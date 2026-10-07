# Stage 4.8 — Historical Rotation Backtest (design, V1)

Research only. A deterministic replay of the frozen Stage 4.7 Portfolio Rotation model over historical daily bars. It
answers how a fixed rotation configuration would have behaved, versus SPY, under explicit cost assumptions. It never
trades, never builds an order, never touches Stage 4.6A/4.6B, Robinhood or an LLM, and never tunes parameters.

## 1. Core principle — one strategy engine

Every rebalance decision is produced by the EXISTING `rotation.engine.compute_rotation` (eligibility E1–E10, Phase 1
factors, percentile scores, composite, ranking, rank buffer, static equal weights, actions, turnover limit). The backtest
only supplies that pure function with (a) the stored rotation configuration, (b) the resolved universe, (c) bar series
truncated to the signal session T, (d) a `PortfolioSnapshot` built from the SIMULATED cash and holdings, and (e) T.
There is no second ranking or allocation code path. New code is limited to: the calendar, point-in-time truncation,
execution and accounting, metrics, persistence, API and a minimal UI.

```
historical daily bars (Stage 3.2 bar layer, read-only)
  → session calendar (SPY) → rebalance dates (WEEKLY | MONTHLY)
  → for each signal session T: series truncated to ≤ T → compute_rotation(config, universe, series≤T, simulated snapshot, T)
  → target weights / actions / est. whole-share differences (Stage 4.7 output)
  → simulated execution at the NEXT session's OPEN (T+1): sells first, then buys in rank order; slippage + cost
  → simulated holdings / cash → daily mark at completed closes → equity curve → metrics (vs SPY)
```

## 2. No future leakage (hard rules)

* For signal session T the engine receives `BarSeries` objects containing ONLY bars dated ≤ T (`truncate(series, T)`),
  so no later bar can influence a factor even by accident; the engine additionally slices to T itself.
* Factor windows end at T (Stage 4.7 reads closes `[:index(T)+1]`).
* Execution happens at the open of the first session AFTER T on the benchmark calendar. A signal is never filled at T's
  close. The last schedule date needs a following session inside the backtest range; otherwise that rebalance is
  recorded as `NO_NEXT_SESSION` and no trade happens.
* Daily marking uses the completed close of each session; the day-T equity (used as the next decision's reference) is
  known at T's close, the trades are priced at T+1's open.
* Test coverage: mutating every bar dated > T must not change the proposal at T (`mutate_after`), factor windows end at
  T, fills equal T+1 open ± slippage, no fill equals a T close, a missing T+1 bar fails the run.

## 3. Data

* Source: the Stage 3.2 immutable bar cache / `fit.current.load_bars` (daily OHLCV, split+dividend adjusted, complete
  sessions only). The backtest reads bars for `universe ∪ {SPY}` from `start_date − 420 calendar days` (so 252 completed
  sessions precede the first signal) through `end_date`. At most ONE batched market-data request (the existing path).
* The SPY series defines the session calendar. A universe symbol missing a bar on T is simply ineligible at T (Stage 4.7
  rule E6/E3); a HELD symbol missing a close on a marking day or an open on an execution day is a run FAILURE
  (`MISSING_CLOSE` / `MISSING_OPEN`) — prices are never interpolated or carried forward.
* Benchmark: SPY must have a bar on every session of the range; the first missing one fails the run (`MISSING_BENCHMARK`).
* Universe: the Stage 4.7 universe sources (WATCHLIST, SAVED_SCAN, SAVED_UNIVERSE, CUSTOM) resolved through
  `rotation.universe.resolve_universe` and stored (content + hash) with the run. **Survivorship bias**: V1 replays a
  static universe supplied today; no point-in-time index membership exists, so results are NOT survivorship-bias-free
  and every run, API response and the UI say so (`universe_note`).

## 4. BacktestConfig (immutable, hashed)

```
config_id (Stage 4.7 rotation config version) + config_hash   start_date, end_date (ISO, end ≥ start)
rebalance_frequency ∈ {WEEKLY, MONTHLY}                        initial_cash (decimal > 0)
benchmark = SPY (fixed)                                         transaction_cost_bps (≥ 0), slippage_bps (≥ 0)
execution_price = NEXT_OPEN (fixed in V1)                       universe {source, ref, symbols} → resolved symbols + hash
cash_interest = 0 (V1)                                          whole_shares = true (V1)
```
`backtest_config_hash` = sha256 of the canonical JSON of all of the above (resolved universe hash included). Two runs with
the same hash and the same bars give identical results (`result_hash`).

## 5. Rebalance calendar

Sessions = SPY bar dates within [start_date, end_date]. WEEKLY: the first session of each ISO week; MONTHLY: the first
session of each calendar month. The first schedule date is the first session ≥ start_date that satisfies the rule (the
first session of the range always qualifies for its own week / month). Each schedule date is a SIGNAL session T; its
execution session is the next session in the calendar.

## 6. Simulation at a rebalance

1. snapshot = (cash, holdings) of the simulation at T's close, as a `PortfolioSnapshot` (source LOCAL_SIMULATOR — only a
   label the engine requires; nothing is read from the real simulator).
2. `res = compute_rotation(cfg, universe, series≤T, snapshot, T)` → status, targets (weights), items (actions,
   est_qty_diff from reference prices at T's close and reference equity).
3. Trade only when status ∈ {VALID, INSUFFICIENT_CANDIDATES} (fewer names than slots is a legitimate historical state).
   `TURNOVER_LIMIT_EXCEEDED`, `NO_ELIGIBLE_CANDIDATES`, `DATA_STALE`, `INPUT_ERROR`: no trade that rebalance (recorded).
   This mirrors Stage 4.7, where a turnover breach blocks the whole proposal.
4. Orders (whole shares): EXIT → sell all held; DECREASE → sell est_qty_diff; ADD / INCREASE → buy est_qty_diff; HOLD /
   NONE → nothing. SELLs first (alphabetical), then BUYs in rank order.
5. Fill at the execution session's OPEN: BUY `open × (1 + slippage_bps/10⁴)`, SELL `open × (1 − slippage_bps/10⁴)`
   (prices quantised to 0.0001). Transaction cost = `|shares × fill| × transaction_cost_bps/10⁴` (0.01), deducted from cash
   separately — slippage is in the price, cost is a cash debit, nothing is counted twice.
6. Cash can never go negative: a BUY is reduced (floor) to the whole shares that `cash_available / (fill × (1 + cost))`
   allows; a reduced or skipped buy is recorded (`CASH_LIMITED` / `NO_CASH`). No shorting (a SELL never exceeds the held
   quantity), no leverage, no fractional shares, residual cash stays in cash.
7. Rebalance record: T, execution session, engine status, reference equity, turnover (Stage 4.7 weight turnover), traded
   notional, costs, number of trades, and the per-symbol proposal rows (JSON) for audit.

## 7. Daily marking and benchmark

Equity(s) = cash + Σ qty × close(s) on every session s of the range (holdings valued only with their own completed close).
Benchmark: SPY buy-and-hold index = initial_cash × close(s) / close(first session); recorded alongside. Portfolio and
benchmark share the same sessions by construction.

## 8. Metrics (documented conventions)

Daily simple returns r_s from the equity curve (first session's return is 0). Sessions per year = 252; risk-free = 0.
* total_return = E_end / E_0 − 1 · CAGR = (E_end / E_0)^(252 / n_sessions) − 1 (n_sessions = sessions after the first)
* annualized_volatility = stdev(r, sample) × √252 · Sharpe = mean(r) / stdev(r) × √252 · Sortino = mean(r) / downside × √252
  where downside = √(mean(min(r, 0)²)); undefined (null) when the denominator is 0 or n < 2
* max_drawdown = min(E_s / peak_s − 1) · max_drawdown_sessions = longest span from a peak to the recovery (or end)
* benchmark_total_return, benchmark_cagr, excess_return = total − benchmark total, annualized_excess = CAGR − benchmark CAGR
  (labelled "simple annualized excess", not a regression alpha)
* turnover: mean Stage 4.7 weight turnover per executed rebalance, and total traded notional / mean equity
* total_transaction_costs, n_rebalances (scheduled / executed), n_trades
* completed positions (quantity back to 0): win_rate (realised P&L > 0, average-cost basis), average_holding_sessions
* best_month / worst_month (calendar-month returns from month-end equity), worst_rolling_3m (63-session rolling return)
* cash_weight mean / min / max over sessions
Nothing here is a statistical-significance claim; the UI labels results "historical simulation, static universe".

## 9. Storage (additive, immutable)

`portfolio_backtest_configs` (one immutable backtest definition per hash) · `portfolio_backtest_runs` (status
COMPLETED | FAILED, hashes, counts, failure detail) · `portfolio_backtest_rebalances` · `portfolio_backtest_trades` ·
`portfolio_backtest_equity` · `portfolio_backtest_metrics` (one JSON row per run). `CREATE … IF NOT EXISTS`, no-update /
no-delete triggers, `user_version` untouched, 32-hex ids, canonical JSON, decimal strings. Created on the first write.
A run stores: backtest config snapshot + hash, rotation config_id + config_hash, universe content + hash, bars content hash
per symbol, date range, execution convention, cost assumptions, engine version — enough to reproduce it.

## 10. API / UI (research only)

`POST /api/rotation-replay/run` · `GET /runs` · `GET /runs/{id}` · `/equity` · `/fills` (simulated fills; the Stage 2 isolation rule bans any `trade` / `order` path) · `/rebalances` · (prefix `rotation-replay`: the Stage 3.1 route rule reserves `backtest` in a path for `/api/backtests`) ·
`GET /config` (defaults, limits, rotation configs). Strict bodies. No path contains "order"; no handoff fields; no broker
import. UI: a BACKTEST card in the Strategy Lab tab (`frontend/portfolio_backtest.js`): pick a rotation config, universe,
dates, frequency, cost / slippage, Run; summary metrics, equity table (JSON for later charts), rebalances, trades. No
broker buttons, no prefill, no link to the Stage 4.6B form.

## 11. Out of scope (deferred)

Stage 4.9 walk-forward, parameter sweeps / optimisation, point-in-time universes, dividends-as-cash, cash interest,
intraday execution, fractional shares, charts beyond the equity table, automated or scheduled runs, any broker action.
