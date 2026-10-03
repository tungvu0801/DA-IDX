# Stage 4.2 — Daily Brief (read-only)

Strategy Lab → **Daily Brief**: *what changed in my strategy system at one completed session?* A deterministic summary of
what earlier stages already **stored**. Stored data only: no market data, no Claude, no broker, no orders, no writes.
Not a recommendation, ranking, signal, prediction, AI opinion or new scan.

## Session

Default: the latest session represented in stored data — any `decision_session` of a saved-scan snapshot or alert event,
or any `session_date` of a forward-journal session (CAPTURED or MISSED). `?session=YYYY-MM-DD` shows that session's
stored brief; a date with nothing stored returns a valid empty brief ("No new stored strategy activity for this session."),
never a 404. Previous / Next move between stored sessions only. Sessions are never combined.

## Sections (fixed order; strategy name A–Z, then symbol A–Z; no importance score)

| Section | Source (stored) | Notes |
|---|---|---|
| WHAT CHANGED | `saved_scan_alert_events` + `saved_scan_snapshots` of the session | Stage 4.1 decoders and `alert_texts` — the exact sentences; a baseline is never a change; counts only |
| FORWARD JOURNALS | `forward_test_sessions` (session_date or `detected_with_session`), `forward_test_observations`, `forward_test_reference_fills` resolved at the session | stored decision, state before → after, reason code, fills resolved at this open, "pending the next session's open" for ENTER / EXIT; MISSED sessions and the sessions a capture recorded as MISSED |
| Completed reference cycles | exit fills resolved at the session, values from `comparison.view.view` (Stage 3.5 cycles + Stage 3.6 excursions) | MFE / MAE as stored; `LEGACY_NOT_TRACKED` and "Not tracked in Stage 3.3" wording kept; nothing inferred |
| EVIDENCE STATUS | `comparison.view.view` of each version represented in the session's activity | as stored **now** (labelled): closed trades + Stage 3.2 sample label, completed cycles + forward sample label, MFE / MAE sample text, continuity; no new comparison — link to Evidence |
| DATA / CONTINUITY | stored capture warnings (verbatim), MISSED sessions, stored scan statuses that were not decided (INCOMPLETE / STALE / unavailable / integrity), evidence read errors, journal continuity at the journal's latest captured session, the stored last automatic check when it concerns the session | system / evidence issues, never investment risks; informational notes (e.g. "No forward journal exists …", post-close context) are listed separately and not counted |

Header: session, "Stored after the … close", generated time, four counts (rule-state changes, forward captures, completed
reference cycles, data issues) and deterministic template sentences. The current settings line (automatic capture ON /
OFF, saved scans with alerts on, unread alerts, last automatic check) is labelled as current, not part of the stored brief.

## Guarantees

* Read-only: every connection is `mode=ro` (`fit.readonly`); a missing table means "nothing stored"; a missing database
  file is never created. 0 tables, 0 migrations, 0 writes; opening the brief never changes an alert's `read_at`.
* No new financial logic: statuses, decisions, states, cycles, moves, MFE / MAE, sample labels and continuity are read
  from the stages that produced them.
* No scheduler task, no polling (the UI reads on open, Refresh brief and navigation), no AI endpoint.
* Query shape: one query per source table for the session (snapshots, events, sessions, observations, fills, settings)
  plus one Evidence view per represented strategy version — the Evidence layer reads one exact version at a time, so
  that part is N+1 by design, bounded by the session's activity (never every strategy in the database).
* Evidence summaries are kept in a small in-memory cache (256 entries, process memory only) keyed by a stamp read in the
  same pass: the version's runs and their status, its journals with status, session count and last `recorded_at`, fill /
  excursion counts and tracking start, and whether it is still the current version. Evidence rows are append-only
  (database triggers), so any new capture, run, archive or version changes the stamp and the view is rebuilt; Refresh
  brief and session navigation over unchanged evidence reuse it. Nothing is persisted.

## API

`GET /api/daily-brief[?session=YYYY-MM-DD]` → `brief_session`, `generated_at`, `navigation`, `summary_counts`,
`headline`, `summary_text`, `changes[]`, `saved_scans_checked[]`, `forward_activity[]`, `completed_cycles[]`,
`evidence_status[]`, `data_issues[]`, `notes[]`, `system_status`, `source_freshness`, `warnings[]`, `timings`.
GET only; unknown parameters and malformed sessions are refused (422).

## Stage 4.3

`notifications/delivery.py` reads this brief (unchanged) to compose the opt-in local desktop notification of a new
stored session — see `notifications/METHOD.md`. The brief API itself is unchanged and still GET-only.

## Known limitations

Only what earlier stages stored (a server that was off stores nothing to summarise); latest completed daily close only;
Evidence status and journal status reflect what is stored now, not a reconstruction of that date; the last automatic
check is the only stored automation summary (earlier checks are not kept); saved HOLDINGS scans remain deferred.
