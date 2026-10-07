# Stage 4.7 MVP — acceptance and freeze record

Frozen code: branch stage-47 @ **59fb134** (manifest `FROZEN_47_MVP_MANIFEST.txt`, 329 tracked files, sha256 per git blob).
Freeze artifacts are committed on top of that tree as "Freeze Stage 4.7 MVP". The Stage 4.6B freeze of main 6e65e5a
(`s46b/freeze/frozen_46b_manifest.txt`, tree 42ab918) is preserved unchanged as the historical record of main before Stage 4.7.

## MVP scope

Stock Agent MVP = deterministic research and portfolio-rotation proposals with a manual, explicit Alpaca PAPER execution path:

- Stages 1–4.6B as frozen on main (scanner, strategy lab, backtests, forward journal, strategy fit, evidence, grounded AI explanations,
  saved scans, daily brief, local paper simulator, Alpaca PAPER read-only view, Alpaca PAPER manual orders).
- **Stage 4.7 Phase 1** deterministic rotation core (`rotation/factors.py`, `rotation/rules.py`): factor scores vs the fixed SPY
  benchmark, composite ranking, ADD / INCREASE / HOLD / DECREASE / EXIT actions, turnover, weight and cash-buffer limits.
- **Phase 2** persistence (`database/rotation_migrations.py`, `rotation/store.py`): additive, idempotent migrations; append-only tables
  with no-update / no-delete triggers; `user_version` untouched.
- **Phase 3** engine and snapshots (`rotation/snapshots.py`, `rotation/universe.py`, `rotation/engine.py`): portfolio sources
  ALPACA_PAPER_VIEW (frozen 4.6A read-only view), ROBINHOOD_READ_ONLY (gateway GET-only: portfolio and positions), LOCAL_SIMULATOR
  (Stage 4.5); universes WATCHLIST / SAVED_SCAN / SAVED_UNIVERSE / CUSTOM; run statuses VALID / DATA_STALE / INPUT_ERROR.
- **Phase 4** API and UI (`api/routes/portfolio_rotation.py`, `frontend/portfolio_rotation.js/.css`): configs, snapshot, run, detail;
  strict request bodies; display-only proposal table.
- **Phase 5** Alpaca Paper handoff (`rotation/handoff.py` + browser prefill): only ALPACA_PAPER_VIEW runs are eligible; the browser
  fills the EXISTING Stage 4.6B Manual Orders form (symbol, BUY | SELL, whole shares) and the user still clicks Preview, then Confirm.
  ROBINHOOD_READ_ONLY and LOCAL_SIMULATOR runs are DISPLAY ONLY (no draft, no prefill).
- **Research workflow** (`research_flow/`, `api/routes/research_workflow.py`, RESEARCH SHORTLIST panel): deterministic shortlist before
  any model call (TOP_N, HELD_DETERIORATION, RANK_MOVER, USER_SELECTED; capped), explicit Claude input / output contracts (bounded JSON
  note; recommendation / action / side / size / price / score / probability fields forbidden; Stage 3.8 guards; fails closed),
  append-only research cache with staleness, per-run budgets (max symbols 8, max LLM calls 5, 2 refreshes per symbol per 6 h), one
  attempt per symbol, batch reports. The rotation and scanner work without Claude; research failures never touch execution.
- **Approved Stage 4.6B fix** (commit 59fb134): `paper/alpaca_orders._reference` called `FC._utc()` without its required argument,
  crashing every real Preview; fixed to `FC._utc(None)`; regression test `tests/test_alpaca_orders_46b_utc_regression.py` runs Preview
  with the real signature; pins updated 60f4af04 → 391cd2f2. No other protected file changed.

## Boundaries (verified by tests, harness and static audit)

- **Robinhood is read-only**: gateway routes GET-only; provider has only `_get`; the rotation snapshot uses `get_portfolio` /
  `get_positions` only; ROBINHOOD_READ_ONLY runs are display-only and an injected handoff is refused.
- **Local simulator** runs are display-only; no broker handoff.
- **Alpaca Paper**: only ALPACA_PAPER_VIEW is handoff-eligible; the handoff only prefills; Preview and Confirm stay explicit clicks in
  the untouched 4.6B pane; `paper/alpaca_order_writer.py` is the ONLY broker writer (imported only by `paper/alpaca_orders.py`,
  reachable only from explicit confirm / retry); no automatic POST retry; client_order_id `sa46b-` + uuid created once per preview;
  account fingerprint (V8), no_shorting (V15), regular hours (V16) and 5-minute close block (V17) intact and byte-identical.
- **Research**: no `paper/` import, no order route, no sizing authority; the LLM never sees the whole universe.
- **No auto-trading**: no scheduler → broker path, no research → broker path, no automatic confirmation, no autonomous submission.
  Manual orders are OFF after the validation.

## Live validation evidence

`ALPACA_PAPER_VALIDATION_FINAL.md`: on 2026-10-07 at 13:38 ET the frozen Stage 4.6B path executed Enable → Preview BUY 1 F → one
Confirm → exactly one POST /v2/orders to paper-api.alpaca.markets → order `cbd4f557-…` filled, 1 share at 12.10 → exact-ID
reconciliation found and consistent → duplicate check: one order for the client_order_id → manual orders returned to OFF.
No live Alpaca trading endpoint, no Robinhood write, no automatic trading.

## Final test evidence (stage-47 @ 59fb134)

| Suite | Result |
|---|---|
| Stage 4.7 Phases 1–5 (26 + 17 + 29 + 17 + 11) | 100 passed |
| Research workflow | 14 passed |
| Stage 4.6A / 4.6B / UTC regression / 4.5 (33 + 83 + 3 + 23) | 142 passed |
| Regression isolation, AI gating, harness pins (9 + 27 + 21 + 7) | 64 passed |
| Focused total (one pytest run) | 320 passed |
| rh_gateway | 149 passed |
| Full suite | 1068 passed, 1 warning in 461.17s  |
| Browser harness default sequence (19 flows incl. alpaca_orders) | PASS, 385 checks, 0 errors |
| Browser harness portfolio_rotation flow | PASS, 28 checks, 0 errors |
| Protected files | 21 byte-identical to the 4.6B freeze + `paper/alpaca_orders.py` at approved pin 391cd2f2 |

## Deferred after the MVP (not started)

Stage 4.8 historical rotation backtest · Stage 4.9 walk-forward · automated / scheduled paper trading and any auto-confirm · charting ·
ML · fractional broker orders · live-money deployment · UI redesign · scheduler-to-broker execution · harness coverage of the research
panel buttons · `known_event_context` population · research for Robinhood / local-sourced runs beyond display.

## Operator follow-ups (outside the code)

- Rotate the Alpaca paper key pair used on 2026-10-07 and update all four `ALPACA_*` variables in .env (never committed).
- The Stage 4.6B settings row keeps account ••••LTN9 linked with manual orders OFF.
