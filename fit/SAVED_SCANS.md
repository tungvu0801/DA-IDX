# Stage 4.1 — Saved scans + RULES MET change alerts

Strategy Lab → **Scanner** → **SAVED SCANS** / **STRATEGY ALERTS**: *did the set of stocks that meet a saved strategy
version's entry rules change after the latest completed session?* Local, opt-in, in-app only, deterministic. Alerts are
informational rule-state changes: not trade signals, advice, rankings, predictions or orders.

## Saved scan

One exact immutable `strategy_version_id` + one list source:

| Source | Resolved at each check | Notes |
|---|---|---|
| `SAVED_UNIVERSE` | the version's saved universe | fixed with the version |
| `WATCHLIST` | the app watchlist at check time | membership can change; changes are recorded, never alerted |
| `CUSTOM` | the stored list | upper-cased, de-duplicated, sorted, ≤ 100 symbols, fixed at save |
| `HOLDINGS` | — | **deferred**: scheduled checks must never depend on the broker connection |

The identity (version, source, custom list) never changes (database triggers); a different choice is a new saved scan.
Saving the same active identity twice is refused (`ALREADY_SAVED`). Administrative fields only: name, the alert switch,
archive. **Alerts are OFF by default** — "Notify me when RULES MET changes" turns them on.

* **Alerts off / paused** — no scheduled check; **Check now** still runs (same path, same snapshot + event rules).
* **Archived** — no check of any kind; snapshots and alert history are kept; archiving is final. Nothing is deleted.

## Check

`fit.saved_scans.check` runs the ONE Stage 4.0 scanner (`fit.scanner.scan`) for the saved configuration at the latest
completed session and stores one immutable compact **snapshot** per saved scan and decision session: resolved symbols,
each symbol's status, the group lists, condition counts, list changes, spec / rules hashes, the feature registry
fingerprint and a snapshot fingerprint (sha256 of version, hashes, registry, symbols, session, statuses).

* The **first** snapshot is the **baseline**: no alert.
* The same session again → `ALREADY_CHECKED` (no-op). An older session after a newer one → `NEWER_SNAPSHOT_EXISTS`.
* No session could be resolved → `NO_SESSION` (nothing stored; retried by the scheduler only for data unavailability).
* Otherwise the RULES MET sets are compared over the symbols present in **both** snapshots:
  `newly_rules_met` / `no_longer_rules_met` (each with its previous and latest status). If either is non-empty, ONE
  immutable **alert event** is stored with an alert fingerprint (saved scan, both snapshot fingerprints, session, the
  two lists). Condition-count changes alone never alert. Symbols that joined or left the list are recorded on the
  snapshot as `list_changes` (with the joining symbol's status) and never create a rule alert.

Duplicates are impossible: an in-process lock per saved scan, one SQLite write transaction (`BEGIN IMMEDIATE`) and
UNIQUE (scan, session) on snapshots and events, UNIQUE snapshot / fingerprint on events, plus no-replace / no-delete
triggers. A manual check racing the scheduler, or two schedulers, produce one snapshot and at most one event.

## Wording

* `AMD newly meets the saved entry rules for Pullback v1 (RULES MET).`
* `MU no longer meets the saved entry rules for Pullback v1 (now RULES NOT MET).`
* `MU is no longer in RULES MET for Pullback v1; the latest scan has incomplete data, so the rule result could not be
  decided.` — INCOMPLETE DATA is never described as the rules becoming false. STALE DATA and DATA UNAVAILABLE have their
  own sentences.

"all" is not used ("meets **all** saved entry rules") because entry rules can combine ANY / nested groups.

## Scheduling

The Stage 3.7 scheduler (same thread, check time, lease table and retries) runs registered after-close extensions
(`forward.automation.register_extension`). The saved-scan extension checks every non-archived scan with alerts on, after
the forward capture (which runs only when automatic capture is on). The scheduler runs when forward capture is on OR a
saved scan has alerts on. Each saved scan is isolated (one failure never stops another; a forward-capture failure never
blocks saved scans); identical configurations in one check reuse one scanner result. A separate lease
(`saved_scan_checks`) keeps two scheduler processes apart. The status lists forward captures, saved-scan checks and
alerts created separately. Nothing is backfilled: a server that was off simply compares the next checked session with
the last stored one.

## API

```
GET  /api/saved-scans                   POST /api/saved-scans   {strategy_version_id, source, name?, alerts_enabled?, symbols? (CUSTOM)}
GET  /api/saved-scans/{id}              POST /api/saved-scans/{id}/check | /settings {name?, alerts_enabled?} | /archive
GET  /api/strategy-alerts?unread_only&limit&before     POST /api/strategy-alerts/{id}/read | /read-all
```

Strict bodies (`extra="forbid"`): no strategy JSON, SQL, prompt or expression; no delete endpoint. 0 Claude, 0 broker,
0 order calls. Explanations stay in Strategy Fit ("Open Strategy Fit" → "Explain this setup", Stage 3.8 budgets).

## Tables (additive; `PRAGMA user_version` untouched)

`saved_strategy_scans`, `saved_scan_snapshots`, `saved_scan_alert_events` — see `database/saved_scan_migrations.py`.

## Known limitations

* Checks run only while the local Stock Agent server runs and the computer is awake; no cloud, email, push, SMS or
  webhook notification.
* No missed-alert backfill; latest completed daily close only (daily-bar limitations remain).
* The saved strategy universe remains authoritative; a watchlist-based saved scan's membership can change.
* Event / earnings-calendar limitations remain (unavailable event data makes a result INCOMPLETE, never false).
* No ranking or recommendation; alerts describe rule-state changes, not predicted outcomes.
* Saved HOLDINGS scans are deferred.
