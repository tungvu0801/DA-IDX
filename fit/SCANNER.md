# Stage 4.0 — Strategy Scanner

Strategy Lab → **Scanner**: *for ONE exact saved strategy version, which stocks in a list I choose currently have its
entry rules met, not met, incomplete, stale, or outside the saved universe?* Read-only, user-triggered, deterministic.
Not a ranking, recommendation, prediction, alert, screener or trading feature. (Stage 4.1 adds opt-in saved scans with
in-app RULES MET change alerts on top of this scanner — see `SAVED_SCANS.md`; a live scan itself still stores nothing.)

## How it works (`fit/scanner.py`, no second rules engine)

1. **Version** — the exact saved version, re-verified with the Stage 3.3 / 3.4 eligibility checks (spec / rules hash,
   schema, feature-registry version + fingerprint, forward support). INTEGRITY ERROR, REGISTRY MISMATCH and UNSUPPORTED
   versions are refused. BACKTEST_READY and FORWARD_TEST_ONLY versions can be scanned; backtests are not required.
2. **List** — Saved universe / Watchlist / Holdings are resolved on the server (holdings through the existing read-only
   Stage 3.1 shortcut; unavailable holdings never block the other sources). A custom list is upper-cased, trimmed,
   de-duplicated and validated again on the server. More than **100** symbols → `TOO_MANY_SYMBOLS` (never truncated).
3. **Universe** — a symbol outside the version's saved universe is `OUTSIDE UNIVERSE`: no rule and no data work for it.
4. **One plan, one load** — the version's entry rules are planned once for every in-universe symbol with Stage 3.2 /
   3.3's own dependency rules; bars come from Strategy Fit's `load_bars` (Stage 3.2 cache → memory → one batched
   read-only request). The breadth list is loaded only if a rule needs it; sector ETFs are de-duplicated.
5. **One session** — Strategy Fit's completed-session rule (`fit.current.resolve_session`) gives one decision session
   for the whole scan; a symbol without a bar for it is `STALE DATA` (never an older session).
6. **One snapshot** — market and breadth features are computed once; saved research (0 Claude calls) and event context
   are read once for the list.
7. **Rules** — Strategy Fit's own `evaluate_version` per symbol (`strategy.evaluate.group_met` via the Stage 3.3 trace):
   identical status, trace, counts and feature values to an individual Strategy Fit evaluation (tested). Counts are
   descriptive ("3 / 4 conditions met"), never a percentage or score.

Results are grouped in a fixed order (rules met, not met, incomplete, stale / unavailable, outside universe, error) and
alphabetical within each group. Nothing is stored: 0 database writes, no scheduler, no alert.

## API

* `GET /api/strategy-scanner/config` — versions (with scannability and universe), watchlist, holdings availability,
  limits, labels. The broker gateway is contacted only by a HOLDINGS scan.
* `POST /api/strategy-scanner/scan` — `{"strategy_version_id", "source": SAVED_UNIVERSE | WATCHLIST | HOLDINGS | CUSTOM,
  "symbols": [...] (CUSTOM only)}`; strict body. Errors: `NOT_FOUND` 404, `INTEGRITY_ERROR` / `REGISTRY_MISMATCH` /
  `UNSUPPORTED` / `HOLDINGS_UNAVAILABLE` 409, `TOO_MANY_SYMBOLS` / `INVALID_SYMBOL` / `EMPTY_LIST` 422.

## UI

Choose a strategy, version and list → **Scan** → grouped table (symbol, status, conditions, main unmet / unavailable
condition, context, actions). Filters and search are client-side (0 requests); **Details** shows the rule trace and
feature sources; **Open Strategy Fit** opens that stock with the exact version's card in focus — the Stage 3.8
"Explain this setup" is available there (the scanner itself never calls Claude). A remembered scan is labelled
"Previous scan — refresh to update"; **Refresh scan** re-runs it.

## Limitations

One version at a time; the saved universe stays authoritative; latest completed daily close only (daily-bar limits);
breadth uses the app's fixed list; the sector map is static; there is no verified earnings calendar; saved research may
be missing or created after the close; results are current derived views, not stored evidence.
