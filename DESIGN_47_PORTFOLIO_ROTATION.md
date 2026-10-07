# DESIGN_47_PORTFOLIO_ROTATION — Stage 4.7 Deterministic Portfolio Rotation Engine (V1, FINAL AMENDED, APPROVED)

Approved 2026-10-05. Built on the frozen Stage 4.6B main (merge `6e65e5a`, tree `42ab91817117a2fbd219c5c4e14e8b5b2b5e7b9d`).

## 1. Executive summary
Stage 4.7 adds a deterministic, versioned factor-ranking layer that turns an explicit universe into an immutable
**rotation run**: eligibility (E1–E10) → six Decimal factors at the latest completed session T → average-rank
percentiles → composite → Top-N with a rank buffer → static EQUAL_WEIGHT targets with a cash buffer → a rebalance
proposal (ADD / INCREASE / DECREASE / EXIT / HOLD / NONE) with cash-aware turnover. Every valuation uses
`reference_equity` derived from completed-session prices, never broker-reported equity. The output is a proposal, never
an order. The only path to a broker remains the frozen Stage 4.6B pane, reached by a **browser-only prefill** of its form
(B-lite). Current-portfolio sources: `ALPACA_PAPER_VIEW`, `ROBINHOOD_READ_ONLY`, `LOCAL_SIMULATOR`, all normalised into one
`PortfolioSnapshot`. V1 is pure Python / Decimal: 0 Claude, 0 broker writes, 0 broker or gateway requests inside a run,
≤ 1 batched market-data request per run, additive migration, zero frozen-test exemptions, and an explicit list of
protected Stage 4.6B files.

## 2. Existing architecture findings
- Bars / sessions: `fit.current.load_bars` (Stage 3.2 cache → `BAR_CACHE` → one batched read-only market-data request),
  `fit.current.resolve_session` (one completed decision session T, SPY-dated); `BarSeries` bars carry
  open / high / low / close / volume, so average dollar volume is computable.
- Universe helpers: `fit.scanner.normalise_symbols`, `fit.scanner.MAX_SYMBOLS` (100), `scanner.watchlist.load_watchlist`,
  saved-scan snapshots (`fit.saved_scans.SavedScanStore.latest`) with per-symbol statuses, saved strategy universes.
- Current-portfolio paths: Stage 4.6A `paper.alpaca_view.view()` (last in-memory refresh: `refreshed_at`, `account`,
  `positions`; 0 requests), Stage 4.5 `paper.store.PaperStore` + `paper.accounting.positions`, and the read-only Robinhood
  path `api.routes.portfolio.provider_factory()` → `portfolio.provider.RobinhoodGatewayPortfolioProvider` (loopback HTTP to
  the local `rh_gateway`, secret header, approved account alias; `UnavailablePortfolioProvider` when
  `PORTFOLIO_AWARENESS_ENABLED=false`). Stage 3.1 / 4.0 already use it for holdings (`get_holding_symbols`). The gateway's
  tool allowlist and HTTP routes are read-only (`/portfolio`, `/positions`, `/orders`, tax lots, P&L — all GET), it caches
  successful results (portfolio / positions TTL 30 s, STALE served up to `STALE_MAX_S`, never silently), and the
  stock-agent persists no Robinhood snapshot.
- Database conventions: one additive `database/<stage>_migrations.py`, `IF NOT EXISTS`, `PRAGMA user_version` untouched,
  run lazily by the store's `write()`; TEXT 32-hex ids, decimals as TEXT with GLOB checks, immutability by triggers,
  nothing deleted.
- API conventions: `APIRouter(prefix=…)`, strict Pydantic bodies (`extra="forbid"`), `{"status", "message"}` error bodies,
  routers appended in `api/server.py`. Frontend: IIFE modules registered with `window.StrategyFit.addWorkspace`, panes
  rendered lazily, no timers, `esc()` everywhere.
- Constraining tests (all satisfiable without edits): no non-GET path containing "order"; no path containing "paper"
  outside the exempt sets; only `api/routes/alpaca_paper_orders.py` may import `paper.alpaca_orders`;
  `database/*alpaca*|*broker*|*fit*|*compar*|*evidence*` globs banned; `FORBIDDEN_TOKENS` (MCP / broker tool names) banned
  in production files; word-ban tests are file-scoped; the harness pins `FLOWS[0]` and `FLOWS[-8:]`.

## 3. Extension points and new files
Read-only use of: `fit.current.load_bars / resolve_session / BAR_CACHE`, `backtest.bars.last_complete_session_date`,
`backtest.store.bars_content_hash`, `fit.readonly.*`, `fit.scanner.normalise_symbols / MAX_SYMBOLS`,
`scanner.watchlist.load_watchlist`, `fit.saved_scans.SavedScanStore.latest`, `paper.alpaca_view.view`,
`paper.store.PaperStore` + `paper.accounting`, `paper.alpaca_order_rules.MAX_QTY / MAX_NOTIONAL / SYMBOL_RE` (pure
constants), `paper.alpaca_order_store.OrderStore.in_states` (read-only, for E9), `api.routes.portfolio.provider_factory`
(+ `PortfolioUnavailable`) with the methods `get_portfolio` and `get_positions` only.

New files: `rotation/__init__.py`, `rotation/factors.py`, `rotation/rules.py` (Phase 1, pure), `rotation/snapshots.py`,
`rotation/universe.py`, `rotation/engine.py`, `rotation/store.py`, `rotation/ROTATION.md`,
`database/rotation_migrations.py`, `api/routes/portfolio_rotation.py`, `frontend/portfolio_rotation.js / .css`,
`tests/test_rotation_47.py`, `tests/rotation_fixtures.py`, `tests/rotation_protected_46b.py`.
Integration files legitimately extended: `api/server.py`, `frontend/index.html`, `browser_tests/flows.py`,
`browser_tests/app_server.py` (and `tests/conftest.py` only if a Stage 4.7 tripwire is needed — none expected).

## 4. Safety boundaries
- Server-side Stage 4.7 code never imports or references `paper.alpaca_orders`, `paper.alpaca_order_writer`,
  `paper.alpaca_order_reads`, `alpaca.*`, `requests`, `anthropic`, `agents`, `portfolio.provider` internals, `rh_gateway`,
  MCP / broker tool names, `/v2/`, `/api/alpaca-paper-orders`. The only Robinhood touch point is
  `api.routes.portfolio.provider_factory()` with `get_portfolio` and `get_positions`.
- NO broker POST, NO automatic trading, NO live trading, NO Robinhood writes (the gateway has no write route), NO Alpaca
  configuration writes, NO Claude-generated orders, NO bypass of Stage 4.6B V1–V17, NO modification of the frozen
  Stage 4.6B writer / rules, NO shorting (weights ≥ 0), NO leverage (Σ security weights = 1 − cash_buffer_pct), NO options,
  NO stop-loss / take-profit automation, NO timers / scheduler / Stage 3.7 registration.
- The frontend makes exactly three POSTs (`…/configs`, `…/snapshot`, `…/run`); the Stage 4.6B prefill is a DOM write.
- SQL writes only in `rotation/store.py`, INSERT-only. The benchmark is fixed to SPY and used only for the
  relative-strength factor and display; it can never create a broker action.

## 5. Deterministic factor model (Decimal; completed daily bars; offsets in the symbol's own sessions; T = last bar)
| Factor | Definition | Direction |
|---|---|---|
| Momentum | `ret20 = C_T / C_{T−20} − 1`, `ret60 = C_T / C_{T−60} − 1`; factor score = mean of the two percentile scores | higher better |
| Trend | `C_T / SMA50 − 1`, `C_T / SMA200 − 1`; mean of the two percentile scores | higher better |
| Relative strength | `(1 + ret60_sym) / (1 + ret60_SPY) − 1` | higher better |
| Volatility | sample standard deviation of the 20 latest daily log returns (21 closes), annualised × √252 | lower better: score = 100 − percentile |
| Drawdown | `C_T / max(C over the latest 252 sessions) − 1` (≤ 0) | higher (shallower) better; stored and displayed; **default weight 0** |
| Liquidity | mean of `close × volume` over the latest 20 sessions | higher better |
Bar floats are converted exactly once with `Decimal(str(x))`; everything after is Decimal (context precision 34), raw
factors quantised to 6 dp as strings. A missing or non-positive input at any required offset → the factor is None → E7.

## 6. Eligibility rules (every reason collected; machine-readable)
E1 `INVALID_SYMBOL` (`SYMBOL_RE`) · E2 `INSUFFICIENT_HISTORY` (< `min_history_sessions`, default 252) ·
E3 `NO_REFERENCE_PRICE` (no bar dated T or close ≤ 0) · E4 `BELOW_MIN_PRICE` (`min_price`, default 5.00) ·
E5 `LOW_LIQUIDITY` (`min_avg_dollar_volume`, default 5,000,000) · E6 `DATA_STALE` (latest bar session ≠ T) ·
E7 `FACTOR_INPUT_MISSING` · E8 `EXCLUDED` (config `excluded_symbols`) · E9 `CONFLICTING_STATE` (a Stage 4.6B intent in an
UNRESOLVED state; a pending Stage 4.5 order when the source is `LOCAL_SIMULATOR`; Robinhood holdings are portfolio state
only — no pending-order inference) · E10 `SCANNER_NOT_MET` (source `SAVED_SCAN` and the symbol's status ≠ RULES MET).
Output per symbol: `{"symbol", "eligible", "reasons": [...]}`. SPY failing E2 / E3 → run `INPUT_ERROR`; SPY latest
session ≠ T → run `DATA_STALE`.

## 7. Normalisation and ranking
For each factor over the n eligible symbols: ranks with **rank 1 = worst, rank n = best**, **average ranks for ties**;
`percentile = (average_rank − 1) / (n − 1) × 100` (6 dp); **n = 1 ⇒ 50**. Volatility score = 100 − percentile.
Momentum and Trend scores = mean of their two sub-percentiles. `composite = Σ weight_f × score_f` (6 dp). Weights are
Decimal, ≥ 0, sum exactly 1.000000, stored and versioned, never silently modified (default
momentum 0.30 · trend 0.25 · relative strength 0.25 · volatility 0.10 · drawdown 0 · liquidity 0.10).
Order: composite desc → relative-strength score desc → liquidity score desc → ticker asc. Final rank 1 = best. No random
ordering, no dict-order dependence.

## 8. Portfolio selection and rank buffer
Top-N = `portfolio_size` after the buffer (`exit_rank ≥ portfolio_size` enforced by validation): a current holding with
rank ≤ `exit_rank` is retained (in rank order, up to `portfolio_size` slots; a retained holding that would exceed the slots
exits with `RANK_BUFFER_OVERFLOW`); rank > `exit_rank` → EXIT `RANK_ABOVE_EXIT_RANK`; ineligible → EXIT `INELIGIBLE`.
Free slots are filled by the highest-ranked non-holdings. Statuses: `NO_ELIGIBLE_CANDIDATES` (n = 0),
`INSUFFICIENT_CANDIDATES` (selected < portfolio_size; the proposal is still computed, `handoff_allowed = false`).
Manual trigger only; no continuous rotation.

## 9. Allocation — static EQUAL_WEIGHT
`equal_weight = (1 − cash_buffer_pct) / portfolio_size` (6 dp, rounded down). Config is `INVALID_CONFIG` unless
`min_position_weight ≤ equal_weight ≤ max_position_weight`; there is no dynamic clamping or dropping. Every selected
name receives `equal_weight`; when all `portfolio_size` slots are filled the quantisation residual goes to rank 1 so that
`Σ target security weights = 1 − cash_buffer_pct` exactly; `target_cash_weight = 1 − Σ target security weights` (= the
cash buffer, plus unfilled slots' weight when the run is INSUFFICIENT_CANDIDATES).

## 10. Reference valuation (deterministic; source-independent)
`reference_equity = snapshot_cash + Σ quantity_i × reference_price_i(T)` with completed-session closes. Used for current
weights, target notionals (`target_weight × reference_equity`, 2 dp), `est_target_qty` (floor, capped at `MAX_QTY`),
`est_qty_diff` (`floor(|target_weight − current_weight| × reference_equity / reference_price)`) and turnover. A held
symbol without a valid reference price at T → run `INPUT_ERROR`. Broker-reported equity, buying power, market values and
last prices are stored as `source_meta` only and never reach the engine; the engine signature is
`compute(config, universe, bars, snapshot)` and `snapshot.source_meta` is not an input. Fractional quantities are valued
exactly; only `est_qty_diff` is floored to whole shares.

## 11. Rebalance actions and cash-aware turnover
Actions: not held & target > 0 → `ADD`; held & target = 0 → `EXIT`; |Δw| < `rebalance_threshold` → `HOLD` (held) /
`NONE` (not held); Δw > 0 → `INCREASE`; Δw < 0 → `DECREASE`; `est_qty_diff = 0` for ADD / INCREASE / DECREASE → `HOLD`
with reason `BELOW_ONE_SHARE`. `side_hint` BUY for ADD / INCREASE, SELL for DECREASE / EXIT (informational).
`turnover = ( Σ_securities |target_w − current_w| + |target_cash_w − current_cash_w| ) / 2` (6 dp); both cash weights are
stored. `turnover > max_turnover_per_rotation` → status `TURNOVER_LIMIT_EXCEEDED`, every item `handoff_allowed = false`.
Example: 100 % cash → 95 % invested produces turnover 0.95.

## 12. Current-portfolio sources and the PortfolioSnapshot
`PortfolioSnapshot = {source, snapshot_at, cash, positions: [{symbol, quantity}], source_meta}` (adapters in
`rotation/snapshots.py`):
| source | cash | positions | snapshot_at | source_meta (informational only) | network in the adapter |
|---|---|---|---|---|---|
| `ALPACA_PAPER_VIEW` | 4.6A `view()["account"]["cash"]` | `view()["positions"]` (long only; anything else → `INPUT_ERROR`) | `view()["refreshed_at"]` | masked account, broker equity / buying power, section states | 0 (the user refreshes in the 4.6A pane) |
| `ROBINHOOD_READ_ONLY` | `get_portfolio().data["cash"]` | `get_positions()` → `symbol`, `quantity` (fractional allowed) | earliest `fetched_at` of the two reads | gateway status per read, `cache_age_s`, `truncated`, reported market value / buying power, per-position last price, held / sellable quantity, account alias | 0 during a run; 2 loopback gateway GETs during the explicit snapshot load |
| `LOCAL_SIMULATOR` | `accounting.cash(...)` | `accounting.positions(...)["shares"]` | now (database is authoritative) | account id, pending 4.5 orders | 0 |
Normalisation (all sources): symbols upper-cased and validated (`SYMBOL_RE`; invalid → `INPUT_ERROR`, never dropped),
duplicates summed, `quantity ≤ 0` dropped, `cash` a finite Decimal ≥ 0, gateway `truncated = true` → `INPUT_ERROR`.

**Explicit snapshot loading.** A run never performs gateway or broker network activity. `ALPACA_PAPER_VIEW`: the user
clicks Refresh in the 4.6A pane; 4.7 reads `alpaca_view.view()`. `ROBINHOOD_READ_ONLY`:
`POST /api/portfolio-rotation/snapshot {"source": "ROBINHOOD_READ_ONLY"}` performs exactly two loopback gateway GETs
(`/portfolio`, `/positions`) through `provider_factory()`; Robinhood upstream traffic occurs only when the gateway's cache
is cold and is governed by the gateway. The snapshot is held in process memory (like 4.6A's last refresh) and copied into
the run row. `LOCAL_SIMULATOR` is read at run time. Missing snapshot → `INPUT_ERROR`; `snapshot_at` older than
`max_snapshot_age_min` (default 30) or a gateway `STALE` envelope → `DATA_STALE` (fail closed). Robinhood holdings are
portfolio state only; `held_quantity > 0` / `sellable_quantity < quantity` become the display flag `RH_SHARES_HELD`.
A Robinhood-sourced proposal's prefill still targets the Alpaca PAPER 4.6B pane and carries the notice
`source_mismatch_note` ("Proposal based on Robinhood holdings — the paper account's own positions and rules apply at
Preview"). No Robinhood action of any kind exists in Stage 4.7; no new Robinhood authentication behaviour.

## 13. Schema (`database/rotation_migrations.py`; additive; `IF NOT EXISTS`; `user_version` untouched)
Names contain none of fit / compar / evidence / alpaca / broker / order / paper. Ids TEXT 32-hex; decimals TEXT with GLOB
checks; timestamps ISO-8601.
- `portfolio_rotation_configs` — `config_id` PK, `name` (1–80), `version` INT (per name), `config_json` canonical,
  `config_hash` 64-hex UNIQUE, `benchmark` CHECK (= 'SPY'), `created_at`. Immutable (UPDATE aborts; no DELETE). Many named
  versions; **no mutable "active" config** — every run references `config_id` + `config_hash`.
- `portfolio_rotation_runs` — `run_id` PK, `config_id`, `config_hash`, `run_at`, `data_session`, `benchmark` CHECK 'SPY',
  `universe_source` CHECK IN (WATCHLIST, SAVED_SCAN, SAVED_UNIVERSE, CUSTOM), `universe_ref`, `universe_json`,
  `universe_hash`, `portfolio_source` CHECK IN (ALPACA_PAPER_VIEW, ROBINHOOD_READ_ONLY, LOCAL_SIMULATOR),
  `portfolio_snapshot_at`, `snapshot_status` (OK / STALE / NULL), `snapshot_cash`, `positions_json` (normalised
  `[{symbol, quantity}]`), `source_meta_json` (informational; never read back by the engine), `source_mismatch_note`,
  `reference_equity`, `current_cash_weight`, `target_cash_weight`, `input_hash`, `proposal_hash`, `status` CHECK IN (VALID,
  NO_ELIGIBLE_CANDIDATES, INSUFFICIENT_CANDIDATES, TURNOVER_LIMIT_EXCEEDED, DATA_STALE, INPUT_ERROR), `status_detail`,
  `n_universe`, `n_eligible`, `n_selected`, `turnover`, `market_data_requests`, `completed_at`. Inserted complete, with
  its children, in one `BEGIN IMMEDIATE`; no UPDATE, no DELETE.
- `portfolio_rotation_candidates` — (`run_id`, `symbol`) PK, `eligible`, `reasons_json`, `raw_json`, `scores_json`,
  `composite`, `rank`, `reference_price`, `avg_dollar_volume`, `flags_json`. Append-only.
- `portfolio_rotation_targets` — (`run_id`, `symbol`) PK, `rank`, `target_weight`, `reference_price`, `target_notional`,
  `est_target_qty`, `reason` CHECK IN (TOP_N, RETAINED_RANK_BUFFER), `flags_json`. Append-only.
- `portfolio_rebalance_items` — `item_id` PK, `run_id`, `symbol`, `current_qty` (decimal TEXT), `current_weight`,
  `target_weight`, `weight_diff`, `est_qty_diff` INT, `side_hint` CHECK IN (BUY, SELL) or NULL, `action` CHECK IN (ADD,
  INCREASE, DECREASE, EXIT, HOLD, NONE), `reason`, `handoff_allowed` 0/1. Append-only.
Runtime-failed runs (DATA_STALE, INPUT_ERROR, NO_ELIGIBLE_CANDIDATES, INSUFFICIENT_CANDIDATES, TURNOVER_LIMIT_EXCEEDED)
are persisted for audit; malformed API requests and rejected config creations are not.

## 14. Immutability, hashing and V1 verification
`canonical_json` = sorted keys, compact separators, Decimals as strings. `config_hash = sha256(config_json)`;
`universe_hash = sha256(canonical(source, ref, sorted symbols))`; `input_hash = sha256(config_hash ‖ universe_hash ‖
data_session ‖ benchmark ‖ per-symbol bars_content_hash (SPY included) ‖ canonical(cash, positions))`;
`proposal_hash = sha256(canonical(status, turnover, targets, items, current / target cash weights))`. Same inputs ⇒ same
factors, ranks, weights, proposal and `proposal_hash`; a differing hash for identical inputs is a determinism failure.
V1 verification = stable `input_hash` / `proposal_hash` on golden fixtures + stored-row integrity checks (child counts,
Σ weights, `proposal_hash` recomputable from the stored rows). There is no `/verify` endpoint; full historical replay
from stored inputs is DEFERRED because bar inputs are not persisted by Stage 4.7.

## 15. API (`/api/portfolio-rotation`; strict bodies; no path contains order / paper / trade / place / cancel / submit / execute)
`GET /config` (defaults, limits incl. `MAX_SYMBOLS`, sources, benchmark SPY) · `GET /configs` · `POST /configs`
`{name, weights{momentum, trend, relative_strength, volatility, drawdown, liquidity}, portfolio_size, exit_rank,
cash_buffer_pct, rebalance_threshold, max_turnover_per_rotation, max_position_weight, min_position_weight, min_price,
min_avg_dollar_volume, min_history_sessions, max_snapshot_age_min, excluded_symbols[]}` → a new immutable version or
`INVALID_CONFIG` · `POST /snapshot` `{source: ROBINHOOD_READ_ONLY}` (explicit read-only snapshot load; feature flag off →
422 `PORTFOLIO_UNAVAILABLE`) · `POST /run` `{config_id, config_hash, universe:{source, ref?, symbols?}, portfolio_source}`
(`config_hash` must match) · `GET /runs` · `GET /runs/{id}` · `GET /runs/{id}/candidates` · `GET /runs/{id}/targets` ·
`GET /runs/{id}/rebalance`. No DELETE / PUT / PATCH; no broker-write endpoint; no Stage 4.7 endpoint calls any Stage 4.6B
function.

## 16. UI — "Portfolio Rotation" workspace
Persistent banner "PROPOSAL ONLY — not an order. PAPER handoff requires the separate Alpaca Paper — Manual Orders preview
and confirm." Config picker (named versions, hash); universe picker; portfolio-source picker with
"Load Robinhood snapshot (read-only)" and the snapshot age; **Run Rotation** (one click = one POST, busy guard, disabled
without a fresh snapshot). Run header: status chip, data session, benchmark SPY, reference equity, current / target cash
weight, turnover vs limit, abbreviated hashes, `source_mismatch_note` when present. Candidate table
`Rank | Ticker | Composite | Mom | Trend | RS | Vol | DD | Liq | Current Wt | Target Wt | Action | Reason` (ineligible rows
greyed with reasons); per-ticker factor drill-down (raw, score, composite, rank); current-vs-target weight bars;
rebalance list with **"Prefill Manual Paper Preview"** per `handoff_allowed` item. All text through `esc()`; no timers;
no Claude; every number comes from the stored run.

## 17. Stage 4.6B handoff — B-lite browser-only prefill
The button switches the Paper Portfolio workspace to the Manual Orders view and writes `symbol`, `side` (BUY / SELL from
`side_hint`) and `quantity = est_qty_diff` into the existing Stage 4.6B inputs (`[data-apo-f=…]`) in the DOM — nothing
else. No request is made by Stage 4.7, no automatic Preview, no automatic Confirm: the user clicks **Preview Paper Order**
(V1–V17 on fresh reads) and then **Confirm Paper Order**. No server-side Stage 4.6B import or call. Disabled when
`handoff_allowed = false` or `est_qty_diff = 0`. Option A (display only) was rejected because it reintroduces
transcription errors; a server-side prefill is impossible without a frozen-test exemption.

## 18. Tests
1 Determinism (identical inputs → identical rows and hashes; shuffled input → same; golden hashes pinned) · 2 Eligibility
(E1–E10 isolated; reasons accumulate; SPY failures) · 3 Normalisation (worked n=5 example with a tie, n=1 → 50, volatility
inversion, every tie-breaker) · 4 Allocation (exact Σ, invalid min/max, residual to rank 1) · 5 Rank buffer (size 10 /
exit 15: rank 11 and 15 retained, 16 exits) · 6 Threshold (9.4→10.0 HOLD; 7.5→10 INCREASE; BELOW_ONE_SHARE) · 7 Turnover
(cash-aware formula; limit blocks handoff; 100 % cash → 0.95) · 8 Reference valuation (snapshot cash + qty × ref; broker
equity ignored; unpriced holding → INPUT_ERROR) · 9 Immutability (UPDATE / DELETE abort; same name → new version; wrong
`config_hash` → 409) · 10 Failed-run persistence (runtime statuses stored; rejected config / malformed request → no row) ·
11 Portfolio sources (stale / missing → fail closed; `LOCAL_SIMULATOR`) · 12 Hashing / integrity · 13 Stage 4.6B
isolation (AST / text scans; openapi path sets; protected-file byte check; the 4.6B import-graph tests still pass) ·
14 Network (0 broker requests; ≤ 1 market-data fetch per run; tripwires untouched) · 15 Regression (4.5 parity
`f4022378fbaf380c`, 4.6A parity `d1911f1e27f02174`, 14 regressions, golden pair) · 16 Harness flow `portfolio_rotation`
inserted after `scanner`, before `small_screens` · 17 Secret scan · 18 Universe cap (101 symbols → 422
`TOO_MANY_SYMBOLS`, never truncated) · 19 Robinhood snapshot normalisation (fractional, duplicate, lowercase, held
quantity, `truncated` → INPUT_ERROR) · 20 Realtime values ignored (different market values → identical proposal and
hashes) · 21 Source independence (same cash / quantities via all three sources → identical `proposal_hash`) · 22 Fail
closed (no snapshot, aged snapshot, gateway STALE, flag off → 422 with no row) · 23 Zero Robinhood writes / zero order
requests (fake gateway sees only `GET /portfolio` and `GET /positions`, once each per snapshot load, none during a run;
fake broker sees 0 requests) · 24 Static (no `FORBIDDEN_TOKENS`, no `get_orders | get_tax_lots | get_realized | get_pnl |
/orders`, no `requests` / `rh_gateway` / `portfolio.provider` import; only `get_portfolio` and `get_positions`) ·
25 Protected files byte-identical · 26 UI (three sources, Run disabled without a fresh snapshot, mismatch note, prefill
writes three inputs with 0 requests).

## 19. Freeze / parity strategy — protected files
Stage 4.6B protected implementation files (byte-identical, pinned by `tests/rotation_protected_46b.py` with the sha256
values of the Stage 4.6B freeze manifest): `paper/alpaca_orders.py`, `paper/alpaca_order_rules.py`,
`paper/alpaca_order_reads.py`, `paper/alpaca_order_writer.py`, `paper/alpaca_order_store.py`,
`database/alpaca_order_migrations.py`, `api/routes/alpaca_paper_orders.py`, `frontend/alpaca_orders.js`,
`frontend/alpaca_orders.css`, `paper/ALPACA_ORDERS.md`, `tests/alpaca_order_fakes.py`, `tests/test_alpaca_orders_46b.py`,
`tests/paper_endpoints.py`. Stage 4.6A files (pinned by its own manifest): `paper/alpaca_readonly.py`,
`paper/alpaca_view.py`, `paper/reconcile.py`, `paper/ALPACA_PAPER.md`, `api/routes/alpaca_paper.py`,
`frontend/alpaca_paper.js`, `frontend/alpaca_paper.css`, `tests/trading_client_allowlist.py`,
`tests/test_alpaca_paper_46.py`. Stage 4.5 files are untouched. Integration files that Stage 4.7 extends
(`api/server.py`, `frontend/index.html`, `browser_tests/flows.py`, `browser_tests/app_server.py`, `tests/conftest.py` if
needed) are not required to match the 301-file repository manifest. Expected prior-stage test edits: none.

## 20. Failure modes → status
Market data unavailable / benchmark missing / held symbol unpriced / invalid or non-long snapshot position / gateway
`truncated` → `INPUT_ERROR` (persisted) · benchmark latest session ≠ T, snapshot older than `max_snapshot_age_min`,
gateway `STALE` envelope → `DATA_STALE` (persisted; fail closed) · 0 eligible → `NO_ELIGIBLE_CANDIDATES` · selected <
portfolio_size → `INSUFFICIENT_CANDIDATES` · turnover above the limit → `TURNOVER_LIMIT_EXCEEDED` · bad config body or
`config_hash` mismatch → 422 / 409 `INVALID_CONFIG` (not persisted) · > `MAX_SYMBOLS` → 422 `TOO_MANY_SYMBOLS` ·
snapshot load with the feature flag off → 422 `PORTFOLIO_UNAVAILABLE`; gateway down / rejected / reauth / upstream error →
422 with the provider's reason code (not persisted) · concurrent run → 409 `RUN_IN_PROGRESS` (in-process lock) ·
stored-row integrity mismatch on read → `INTEGRITY_ERROR` surfaced in the GET, row untouched.

## 21. Network budgets
| Action | Market data | Gateway (loopback) | Robinhood upstream | Alpaca broker | Claude |
|---|---|---|---|---|---|
| Open pane / configs / runs / children | 0 | 0 | 0 | 0 | 0 |
| Create config | 0 | 0 | 0 | 0 | 0 |
| Load Robinhood snapshot (explicit) | 0 | 2 GET (`/portfolio`, `/positions`) | ≤ 2, only if the gateway cache is cold | 0 | 0 |
| Run (any source) | ≤ 1 batched daily-bars request (universe + SPY not cached) | 0 | 0 | 0 | 0 |
| Prefill | 0 | 0 | 0 | 0 | 0 (the later 4.6B Preview / Confirm keep their own budgets) |

## 22. Security considerations
Strict bodies; symbols via `SYMBOL_RE`, ≤ `MAX_SYMBOLS`; weights and thresholds parsed as Decimal strings (no float
ingestion); parameterised SQL; no env / secret access (Stage 4.7 never reads `.env`); no new external hosts; escaped
rendering; the prefill writes three input values and never clicks; account identifiers appear only masked or as aliases.
Static tests make every boundary durable.

## 23. Acceptance criteria
All Stage 4.7 tests pass; the full suite passes with 0 prior-stage test edits; the browser harness passes including the
new flow; the rh_gateway suite is unchanged and green; Stage 4.5 and 4.6A parity hashes unchanged; every protected file
byte-identical; golden `input_hash` / `proposal_hash` reproduced; 0 broker requests across the Stage 4.7 suite and harness;
the fake gateway saw only `GET /portfolio` and `GET /positions`, never during a run; ≤ 1 market-data request per run;
`source_meta` provably absent from the engine's inputs; secret scan 0 hits; `paper.alpaca_orders` importers unchanged.

## 24. Explicit non-goals (V1)
ML ranking (XGBoost, LightGBM, LSTM, Transformers), LLM trading decisions, news sentiment, mean-variance or risk-parity
optimisers, sector limits, configurable benchmark, MANUAL portfolio source, full replay verification, automatic or
scheduled execution, live trading, broker cancel / replace / close-position automation, shorting, margin / leverage,
options, stop-loss / take-profit automation, Robinhood pending-order inference.

## 25. Implementation order
Phase 0 branch `stage-47` + this document → Phase 1 `rotation/factors.py` + `rotation/rules.py` (pure) + unit tests →
Phase 2 migration + store + immutability / persistence tests → Phase 3 `snapshots.py`, `universe.py`, `engine.py`
(reference valuation, hashing) + determinism / golden tests → Phase 4 route + API / static / protected-file tests →
Phase 5 frontend + prefill → Phase 6 harness flow → Phase 7 regressions, parity, manifests, secret scan → report, STOP.

## 26. Decision classification
**CLOSED:** percentile formula (average rank, worst = 1, n = 1 → 50, volatility inverted after normalisation); static
equal weight with `min ≤ equal_weight ≤ max` validation and exact Σ = 1 − cash_buffer_pct, residual to rank 1; rank
buffer semantics incl. `RANK_BUFFER_OVERFLOW`; cash-aware turnover with both cash weights stored; reference-equity
valuation (broker / realtime values are metadata only); no `/verify`, V1 verification = stable hashes + golden fixtures +
stored-row integrity; protected-file list with integration files exempt; three portfolio sources with one
`PortfolioSnapshot`; `ROBINHOOD_READ_ONLY` via `api.routes.portfolio.provider_factory()` using only `get_portfolio` and
`get_positions`, explicit snapshot loading, 0 gateway / broker requests inside a run; staleness / failure rules;
Q1 default `ALPACA_PAPER_VIEW` with staleness guard, `LOCAL_SIMULATOR` selectable, missing / stale → fail closed;
Q2 persist runtime-failed runs only; Q3 drawdown stored / displayed, default weight 0; Q4 multiple immutable named
config versions, no active config; Q5 universe cap = `MAX_SYMBOLS` (100); Q6 label "Prefill Manual Paper Preview";
benchmark fixed to SPY on config and run; handoff B-lite (no server-side 4.6B import / call, no auto Preview / Confirm,
no broker request); eligibility E1–E10; tie-breakers; action and status vocabularies; schema; API surface; harness flow
placement; fractional quantities valued exactly with whole-share `est_qty_diff`.
**DEFERRED TO LATER STAGE:** configurable benchmark; MANUAL portfolio source; full historical replay verification;
sector limits; benchmark performance comparison; Robinhood pending-order state via `get_orders`.
