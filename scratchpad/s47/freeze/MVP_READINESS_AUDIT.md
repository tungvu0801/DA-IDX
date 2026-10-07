# Stock Agent MVP Readiness / Freeze Audit (Stage 4.7 + research workflow)

Date: 2026-10-07 · Audit type: read-only review + test runs. No product source changed. No real Alpaca, Robinhood or Claude call was made.

## 1. Git / branch state

| Item | Value | Expected | Result |
|---|---|---|---|
| Branch | stage-47 | stage-47 | OK |
| HEAD | d9e037f (Improve agent research workflow orchestration) | d9e037f | OK |
| main | 6e65e5a (Stage 4.6B merge, freeze tree 42ab918) | 6e65e5a | OK |
| origin/stage-47 | d9e037f | == local HEAD | OK |
| origin/main | 6e65e5a | unchanged | OK |
| Working tree | clean (0 entries) before this audit | clean | OK |
| Merged? | `git merge-base --is-ancestor stage-47 main` → not merged | not merged | OK |

Commits on stage-47 above main: cc66b2f (P1 core) · 8973f90 (P2 store) · f84aff5 (P3 engine) · 4ec4ba3 (P4 API/UI) · 07fc730 (P5 handoff) · d9e037f (research workflow).

`git diff --stat main..stage-47`: 32 files, 5654 insertions, 2 deletions. Every change is a NEW file except six additive edits:
`api/server.py` (+4: two router registrations), `browser_tests/app_server.py` (+3: harness clock), `browser_tests/flows.py` (EXTRA_FLOWS),
`browser_tests/run.py` (+8/−2: EXTRA_FLOWS selection, dict-aware gateway verb check), `frontend/index.html` (+3: css/js/div), and
`tests/test_regression_isolation.py` (+1: the ONE approved Stage 4.7 exemption line for `rotation/snapshots.py`).

## 2. Test results

| Suite | Tests | Result |
|---|---|---|
| Stage 4.7 P1 `tests/test_rotation_47.py` (factors, rules) | 26 | passed |
| Stage 4.7 P2 `tests/test_rotation_store_47.py` (migrations, immutable store) | 17 | passed |
| Stage 4.7 P3 `tests/test_rotation_engine_47.py` (snapshots, universe, engine) | 29 | passed |
| Stage 4.7 P4 `tests/test_rotation_api_47.py` (API + UI pins) | 17 | passed |
| Stage 4.7 P5 `tests/test_rotation_handoff_47.py` (handoff, protected files) | 11 | passed |
| Research workflow `tests/test_research_flow_47.py` | 14 | passed |
| Stage 4.6A `tests/test_alpaca_paper_46.py` | 33 | passed |
| Stage 4.6B `tests/test_alpaca_orders_46b.py` | 83 | passed |
| Stage 4.5 `tests/test_paper_45.py` | 23 | passed |
| Regression isolation `tests/test_regression_isolation.py` | 9 | passed |
| AI gating `tests/test_explain_38.py`, `tests/test_history_budget_39.py` | 27 + 21 | passed |
| Harness pins `tests/test_browser_harness_39.py` | 7 | passed |
| **Focused total (one pytest run)** | **317** | **317 passed** |
| rh_gateway suite (`rh_gateway/tests`, own venv) | 149 | 149 passed |
| **Full stock-agent suite** (`pytest tests`) | | **1065 passed, 0 failed (9 min 38 s)** |

Browser harness (Chrome via CDP, fake broker + fake gateway, external calls blocked):

| Run | Result |
|---|---|
| Default pinned flows (19 flows incl. paper_portfolio, alpaca_paper, alpaca_orders) | PASS · 385 checks, 0 failed, 0 JS errors, 0 console errors, 0 HTTP 5xx, 0 blocked external calls |
| `--only portfolio_rotation` (Phase 5 flow) | PASS · 28 checks, 0 failed, 0 JS errors, 0 Claude calls, 0 preview/confirm requests, 0 broker POSTs, gateway GETs only |

No test failed, so no fix was needed and no frozen test was touched.

## 3. Architecture summary (as built on stage-47)

- **Stage 4.7 Phase 1 — deterministic core** `rotation/factors.py`, `rotation/rules.py`: momentum / trend / relative strength (vs fixed SPY benchmark) / volatility / drawdown / liquidity factors, composite score, rank, ADD/INCREASE/HOLD/DECREASE/EXIT actions, turnover and weight caps, cash buffer, whole-share estimates. Pure functions, Decimal strings.
- **Phase 2 — persistence** `database/rotation_migrations.py`, `rotation/store.py`: five `portfolio_rotation_*` / `portfolio_rebalance_items` tables, additive `CREATE … IF NOT EXISTS`, no-update/no-delete triggers, `user_version` untouched, 32-hex ids.
- **Phase 3 — engine** `rotation/snapshots.py` (ALPACA_PAPER_VIEW via the frozen 4.6A read-only view; ROBINHOOD_READ_ONLY via `provider_factory` `get_portfolio` / `get_positions` only; LOCAL_SIMULATOR via Stage 4.5), `rotation/universe.py` (WATCHLIST / SAVED_SCAN / SAVED_UNIVERSE / CUSTOM), `rotation/engine.py` (run → candidates → targets → items; statuses VALID / DATA_STALE / INPUT_ERROR; `handoff_allowed` only on a VALID run for actionable items with est_qty_diff > 0).
- **Phase 4 — API/UI** `api/routes/portfolio_rotation.py` (`/api/portfolio-rotation`: configs, snapshot, run, run detail; strict bodies), `frontend/portfolio_rotation.js/.css` (source pills, snapshot card, run controls, rebalance table, detail card).
- **Phase 5 — handoff** `rotation/handoff.py` (pure eligibility; ALPACA_PAPER_VIEW → `ALPACA_PAPER_PREFILL`, others → `DISPLAY_ONLY`; draft limited to symbol / BUY|SELL / whole shares 1..MAX_QTY), browser-only prefill of the existing Stage 4.6B Manual Orders form through a MutationObserver; Preview and Confirm remain the user's explicit clicks in the frozen 4.6B pane.
- **Research workflow** `research_flow/shortlist.py` (deterministic shortlist: TOP_N, HELD_DETERIORATION, RANK_MOVER, USER_SELECTED; capped by `max_symbols_per_research_run`), `research_flow/contracts.py` (ResearchRequest from app context only; bounded JSON ResearchResult; forbidden recommendation / action / side / size / price / score / probability fields; Stage 3.8 text guards; fails closed), `research_flow/store.py` + `database/research_cache_migrations.py` (append-only `research_results` / `research_batches`), `research_flow/orchestrator.py` (cache first; `max_llm_calls_per_run`; refresh limit per window; one attempt per symbol via the existing `agents.gating.run_gated_agent` + `ai_explain.service.one_request`; batch reports), `api/routes/research_workflow.py` (`/api/research-workflow`: shortlist, research, results), RESEARCH SHORTLIST panel inside the rotation page.

Matches the approved `DESIGN_47_PORTFOLIO_ROTATION.md` (closed decisions: three sources, SPY benchmark, B-lite browser prefill, Alpaca source only). No TODO / FIXME / placeholder in the Stage 4.7 or research paths (the only `NotImplementedError`s are the two never-used `chat` / `run_tool_loop` methods of the `_Counting` provider wrapper, intentional).

## 4. Protected / frozen state

- 22 protected Stage 4.6A/4.6B files (the exact list pinned in `tests/test_rotation_handoff_47.py`) + 8 Stage 4.5 files: **30/30 byte-identical** to the Stage 4.6B freeze manifest (manifest sha256 == stage-47 git blob == working tree, CRLF normalised). Their sha256 prefixes are also pinned by `tests/test_rotation_handoff_47.py` and `tests/test_research_flow_47.py`, both passing.
- Whole freeze manifest (301 files): 301/301 match on `main`; 295/301 match on `stage-47`. The 6 differences are exactly the six additive edits listed in §1, none of them broker, paper, order or execution code.
- Prior frozen tests: unchanged except the single approved allowlist line in `tests/test_regression_isolation.py` (exact `Path("rotation/snapshots.py")` entry, pattern not broadened). `tests/test_paper_45.py`, `tests/test_alpaca_paper_46.py`, `tests/test_alpaca_orders_46b.py`, `tests/test_explain_38.py`, `tests/test_history_budget_39.py`: identical to the freeze.
- No hidden alternate broker writer: `paper/alpaca_order_writer.py` is imported only by `paper/alpaca_orders.py` (product) and the harness/test fakes; `_post_once` is reachable only from the explicit `confirm` / `retry` paths; no new module imports `paper.alpaca_order_writer`, `paper.alpaca_orders` or `requests`/`httpx` POST.

## 5. Execution boundaries

| Boundary | Evidence | Result |
|---|---|---|
| Robinhood READ ONLY | `portfolio/provider.py` has only `_get`; `rh_gateway/server.py` routes are GET-only (`/status /accounts /portfolio /positions /tax-lots /orders /realized-pnl /pnl-history`), policy tests reject submit/modify/replace/close; `rotation/snapshots.py` calls only `get_portfolio` / `get_positions`; harness: gateway saw GETs only | OK |
| ROBINHOOD_READ_ONLY runs DISPLAY ONLY | `rotation/handoff.py` `mode_for_source` → `DISPLAY_ONLY`, `evaluate` refuses with SOURCE_DISPLAY_ONLY; JS `eligibleDraft` requires `run.portfolio_source === ALPACA`; harness: injected handoff button on a Robinhood run refused | OK |
| LOCAL_SIMULATOR runs DISPLAY ONLY | same path; harness "Local simulator run: display only" | OK |
| Only ALPACA_PAPER_VIEW can hand off | server-side `handoff` metadata per item + JS re-check of server data (no DOM attribute grants eligibility) | OK |
| Handoff only prefills | `handoff()` sets three input values of the existing 4.6B form; the only `.click()` opens the Manual Orders *view*; 0 preview / confirm requests in the harness | OK |
| Preview and Confirm explicit | untouched `frontend/alpaca_orders.js` / `api/routes/alpaca_paper_orders.py` (byte-identical) | OK |
| Stage 4.6B the only order writer | §4; static search: `/v2/orders` appears only in `paper/alpaca_order_writer.py` (POST), `paper/alpaca_order_reads.py` and `paper/alpaca_readonly.py` (GET) | OK |
| No automatic POST retry | `paper/alpaca_orders.py` unchanged ("No automatic POST retry"; `retry` is explicit, same client_order_id, exact lookup first) | OK |
| client_order_id, fingerprint, no_shorting (V15), regular hours / near-close (V16/V17) | `paper/alpaca_order_rules.py` byte-identical to the freeze | OK |
| No shorting / leverage / options / autonomous execution path introduced | new modules import no broker client; `forward/automation.py` and notification modules import no paper/alpaca/rotation/research module; no scheduler reaches the writer | OK |

Account state outside the code (from the 2026-10-06 operator PATCH, read-only verified afterwards): paper account ••••LTN9, `no_shorting = true`, other configuration fields unchanged, fingerprint bound. This is not re-verified here (no broker call allowed in this audit).

## 6. Research workflow

| Requirement | Evidence | Result |
|---|---|---|
| Deterministic shortlist before LLM | `shortlist_for_run` runs on stored run data; `research_run` iterates only the shortlist; tests 1–3, 15 | OK |
| `max_symbols_per_research_run` enforced | cap with explicit SHORTLIST_CAP skips (test 3) | OK |
| LLM call budget enforced | `max_llm_calls_per_run` → BUDGET skips, deterministic by rank (test 10); refresh limit per window (test 8b) | OK |
| Cache behaviour | second run 0 calls / all cache hits (test 4–5); stale rows shown and re-researched (test 6–7); append-only tables (test cache_tables) | OK |
| Malformed output fails closed | NOT_JSON / CUT_OFF / NOT_OBJECT / FORBIDDEN_FIELD / UNEXPECTED_FIELDS / MISSING_FIELDS / BAD_SECTION / GUARD → WITHHELD, nothing inferred (tests 9, 16, 17) | OK |
| No BUY/SELL/order/size authority | forbidden-field list + Stage 3.8 guards; result carries only qualitative sections; `rank`/`score` echoed from the deterministic run; no route in `research_workflow.py` touches orders | OK |
| Rotation / scanner work without Claude | provider failure → FAILED state, run and ranks unchanged (test 8/14); rotation API has no research dependency | OK |
| Research failures do not affect execution | research modules import nothing from `paper/`; failures only change research states | OK |
| No broker dependency | import set of `research_flow/*` and the route: agents, ai_explain, database, fit.readonly, rotation.store, stdlib, fastapi/pydantic only | OK |
| Harness | research panel renders inside the rotation page with 0 JS errors; the harness flow does not press the research buttons (API-level tests cover them) | OK (gap noted in §9) |

## 7. Database / migrations

Fresh database (temp file): `ResearchDatabase` + `RotationStore.write()` + `ResearchStore.write()` initialise cleanly; all five `portfolio_rotation_*`/`portfolio_rebalance_items` tables and `research_results` / `research_batches` present; 14 triggers; `PRAGMA user_version` 0 → 0 → 0 (untouched); second and third passes produce an identical `sqlite_master` (idempotent); `PRAGMA integrity_check` = ok. Both migrations use only `CREATE … IF NOT EXISTS` (no DROP / ALTER / DELETE / UPDATE), immutability via no-update / no-delete triggers. Store tests confirm the triggers fire (P2: 17 tests; research: cache_tables test).

## 8. UI / browser flow

Portfolio Rotation → source visible (pills + label; Robinhood tagged read-only) → load snapshot (fresh/stale tag, 0 broker POSTs) → run rotation (VALID, handoff mode shown) → rebalance table (actions, est. shares, handoff cell) → RESEARCH SHORTLIST panel (Build shortlist = 0 AI calls, Research shortlist ≤ budget, per-row state / cache / refresh, note view keeps the deterministic rank) → eligible Alpaca Paper item → Prepare Paper Order → existing Stage 4.6B Manual Orders pane with Preview still untouched.

- Broken buttons: none (385 + 28 checks, 0 JS / console errors).
- Stale source state: switching source calls `clearRun()` (run, items, research cleared); harness "switching to Robinhood clears the Alpaca proposal".
- Stale proposal state: a new run clears the previous run and research; handoff checks are against server data per click.
- Hidden handoff on Robinhood / local: controls rendered disabled with explanation; injected button refused.
- Empty / error states: "No snapshot loaded yet", "Build the shortlist to see…", "No research note (STATE: reason)", API errors surface as notices.
- Accidental trade action from research UI: research actions are `shortlist`, `research`, `research-refresh`, `rdetail` only; the research panel has no handoff control and the single `fetch(` helper only targets rotation / research routes.

## 9. Static safety search

Searched stock-agent, rh_gateway and robinhood (product code, excluding tests/harness) for `v2/orders`, `submit_order`, `place_order`, `create_order`, `order_buy`, `order_sell`, `cancel_order`, `replace_order`, `/orders`, `requests.post`, `httpx.post`, `"POST"`.

- `/v2/orders` POST: only `paper/alpaca_order_writer.py` (frozen 4.6B writer). GET forms only in `paper/alpaca_order_reads.py`, `paper/alpaca_readonly.py`.
- `create_order` / `cancel_order`: `paper/execution.py` + `api/routes/paper.py` = Stage 4.5 LOCAL simulator (no broker), frozen and byte-identical.
- `/orders`: `portfolio/provider.py` and `rh_gateway` = GET-only Robinhood history reads; `robinhood/robinhood_client.py` = GET crypto order history.
- `rh_gateway/tests/test_policy.py` lists `submit_order`, `modify_order`, `replace_order`, `close_position` as denied tool names (test only).
- FastAPI `@router.post` endpoints added after main: `/api/portfolio-rotation/{configs,snapshot,run}` and `/api/research-workflow/{shortlist,research}` — none builds, previews or sends an order; `test_regression_isolation` (no non-GET path containing "order") passes.
- No scheduler → broker connection; no research → execution connection; no automatic confirmation.

Observation (not a blocker): the harness `portfolio_rotation` flow does not press the research buttons; the research UI is covered by API tests and JS static pins. A harness sub-flow could be added after the freeze.

## 10. MVP readiness checklist

### READY (completed and validated)
- Stage 4.7 Phases 1–5 (deterministic rotation, immutable store, engine with three snapshot sources, API/UI, Alpaca-only browser handoff).
- Agent research workflow (shortlist-before-LLM, contracts, cache, budgets, fallback, observability, panel, API).
- Stage 4.6A / 4.6B frozen implementation byte-identical to the freeze (22 files) + Stage 4.5 untouched (8 files).
- Robinhood read-only end to end (gateway GET-only, provider GET-only, snapshot reads only, display-only runs).
- Database: additive, idempotent, immutable-by-trigger migrations; fresh initialisation OK.
- Tests: focused 317 passed; rh_gateway 149 passed; full suite 1065 passed; harness 385 + 28 checks PASS.
- CI workflow (`.github/workflows/tests.yml`) and `.gitattributes` in place from the 4.6B PR.
- Alpaca paper account configuration: `no_shorting = true` set and read-back verified on 2026-10-06.

### PENDING BEFORE FREEZE (true blockers only)
1. **Controlled one-POST Alpaca PAPER validation during regular market hours** (09:30–15:55 ET): Enable → Preview BUY 1 F → one Confirm through the frozen 4.6B path → exact-ID reconciliation → record. Order POST count so far: 0. This is the only remaining blocker.
2. After it passes: open the stage-47 → main pull request, let CI run green, merge via the GitHub UI, verify main, then record the freeze manifest (same procedure as 4.6B). (Procedural, follows from item 1.)

### DEFERRED AFTER MVP
- Stage 4.8 historical rotation backtest.
- Stage 4.9 walk-forward evaluation.
- Automated / scheduled paper trading and any auto-confirm.
- Charting.
- ML models.
- Fractional broker-order support (4.6B is whole shares only).
- Live-money deployment (paper host only, by design).
- Harness coverage of the research panel buttons; research for Robinhood/local-sourced runs beyond display; `known_event_context` population; MCP connector authorization for the Robinhood tool (not used by the app).

## 11. Exact recommended next action (tomorrow)

During regular U.S. market hours (between 09:30 and 15:55 ET, not within 5 minutes of close), on branch stage-47 at d9e037f with the server started from the venv:
1. Read-only pre-check (max 2 GETs): clock open, account configuration still `no_shorting = true`, fingerprint matches the linked account.
2. In Alpaca Paper — Manual Orders: Enable, then Preview BUY 1 F, then exactly ONE Confirm. Never retry; never POST outside the frozen writer.
3. Reconcile by exact client_order_id / order id (GET only), capture the intent/event rows, and append the outcome to the session record `s47/live_validation/ALPACA_PAPER_ONE_POST_VALIDATION.md`.
4. If the clock is closed, the configuration differs, or any V-rule fails: HARD STOP before POST and report. Only after a successful validation: open the PR stage-47 → main.

## 12. Safety statements

- No real Alpaca call was made.
- No Robinhood call/write was made.
- No real Claude call was made.
- No automatic trading was introduced.
- Nothing was merged to main.
- No secret, key, .env content or unmasked account identifier appears in this document.
