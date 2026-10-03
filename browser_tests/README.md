# Browser regression harness (Stage 3.9; Stage 4.0–4.5 flows added)

One command runs the major Stock Agent UI flows in headless Microsoft Edge against the real app — with no real Claude,
Alpaca, Robinhood or other network call and without touching the user's database:

```
cd stock-agent
.venv\Scripts\python.exe -m browser_tests.run                  # exit code 0 = pass, 1 = any failure
.venv\Scripts\python.exe -m browser_tests.run --only strategy_fit,evidence
.venv\Scripts\python.exe -m browser_tests.run --inject-failure --out %TEMP%\bh-selftest   # self-test: must exit 1
```

* **Dependencies:** the project venv (FastAPI, uvicorn, `websockets` ≥ 12 — already installed) and Microsoft Edge
  (`BROWSER_HARNESS_EDGE` may point at `msedge.exe`). No CI pipeline exists; the command is CI-ready (headless, exit code,
  artifacts).
* **Runtime:** about 85 s (≈ 33 s builds the synthetic world, ≈ 50 s of flows).
* **Artifacts:** `browser_tests/artifacts/` (or `--out`): stable-named screenshots (`strategy_fit_rules_met.png`,
  `evidence_forward.png`, `ai_strategy_explanation.png`, `automation_on.png`, … and `*_1400.png`), `results.json`
  (every check, timings, errors) and `harness.log`. Only those files are replaced on the next run.

## What happens

1. A new temporary directory gets a scratch database (`STOCK_AGENT_ENV=browser_test`); the harness refuses the real or
   any existing database. `world.py` builds the deterministic world with the repository's test fixtures.
2. `app_server.py` starts the real FastAPI app in a thread on a free local port with a fixed clock (Fri Oct 9, 2026
   00:30 ET), synthetic market data for every live call (point-in-time snapshots still replay stored bars through the
   real code), a fake Claude provider for explanations, a fake read-only broker gateway, an in-memory watchlist and the
   Stage 3.7 scheduler parked ("Check automation now" is used instead of waiting).
3. A network guard allows only the harness's own two local ports. Any other connection (Anthropic, Alpaca, the Robinhood
   gateway on :8787, …), any other AI provider request or a `TradingClient` construction fails the run immediately.
4. `flows.py` drives headless Edge (`cdp.py`) at 1920×1080 and 1400×900: dashboard, Strategy Lab builder, stored
   backtest, forward journal, Strategy Fit (all four statuses, drawers, fast symbol switching), Evidence (forward cycles,
   MFE/MAE tracked and mixed legacy/tracked, continuity gap and blocked, empty forward, historical only), AI explanations
   (preview = 1 call, generated, cache = 0 calls, local, provider failure, prompt-injection name, view race), explanation
   history (OFF by default, enable, saved rows, filters, stored item, 0 calls), the Stage 4.0 Strategy Scanner (saved
   universe, watchlist, custom list, holdings unavailable, rules met / not met / incomplete / stale / outside universe,
   fixed grouping + alphabetical rows, filters, search, row details, Open Strategy Fit, strategy-switch race, no ranking
   language, 0 AI calls), automation (OFF, ON, eligible session captured, already recorded, continuity gap, no
   active journals) and — last, because it is the only flow that moves the clock (Oct 8 → Oct 14 sessions) — Stage 4.1
   saved scans (save with alerts OFF by default, turn on, duplicate refused, baseline, scheduled-vs-manual race, newly
   met, no longer met, incomplete transition, no change, open alert, Open Strategy Fit for the exact version, mark read,
   stored snapshot vs Refresh current scan, automation status lines, pause, archive, no polling, 0 AI / broker calls;
   `saved_scan.png`, `scanner_alert_new.png`, `scanner_alert_removed.png`, `alerts_center.png`,
   `saved_scan_paused.png`, `saved_scan_opened.png`, `automation_saved_scans.png` and 1400×900 versions), then the Stage
   4.2 Daily Brief over everything the earlier flows stored (latest session, Previous session through new / removed /
   incomplete alerts, baseline without change, several forward journals, a completed tracked cycle, a legacy cycle, MISSED
   and CONTINUITY BLOCKED sessions, an empty date, Refresh brief, the session-switch race, links to the saved scan /
   Strategy Fit / Forward Journal / Evidence, no polling, alert read state and stored tables unchanged, 0 AI / broker
   calls; `daily_brief.png`, `daily_brief_changes.png`, `daily_brief_forward.png`, `daily_brief_gap.png`,
   `daily_brief_empty.png`, `daily_brief_1400.png`), and finally the Stage 4.3 desktop delivery with a FAKE notifier
   (the real OS adapter is a blocked call that fails the run): OFF by default, enable = baseline, test notification,
   one delivery for a new stored session, same-session / restart dedup, no-activity skip, OS failure, history, no
   notification from opening / refreshing / navigating the brief, disable, 1400×900
   (`daily_brief_delivery_off.png`, `…_on.png`, `…_history.png`, `…_failed.png`, `daily_brief_delivery_1400.png`), and
   the Stage 4.4 click / identity flow with a fake identity (in memory), a fake browser opener and simulated
   NIN_BALLOONUSERCLICK messages (the real adapter start, a real browser open and the real registry are blocked calls):
   branding deferred while OFF, registered while ON, a delivered notification clicked -> the local Daily Brief opened
   and the deep link followed, test-notification click, three clicks = no history row, URL in the text never opened,
   invalid target refused, identity removed when OFF (`brief_delivery_identity_deferred.png`,
   `brief_delivery_identity.png`, `brief_delivery_clickable.png`, `brief_delivery_1400.png`), and last the Stage 4.5
   local paper portfolio on synthetic bars (no broker, no AI): account with no default cash, review + create orders,
   pending before the fill session completes, fill at the next open with slippage / commission (exact cash), cancel,
   insufficient cash at fill (gap up), missing next open (data wait), repeat / restart without duplicate fills, a second
   lot, a FIFO sell with exact realized / unrealized P&L and equity, oversell refused, 1400×900
   (`paper_portfolio_empty.png`, `paper_order_pending.png`, `paper_portfolio_position.png`, `paper_trade_history.png`,
   `paper_portfolio_1400.png`).
5. `checks.py` fails the run on any failed check, flow exception, JS error or unhandled rejection, console error, HTTP
   5xx or blocked call. Server, browser and guard are always stopped in `finally`.
