# Stage 3.7 opt-in automatic after-close capture — method

Automatic capture performs the **existing** Stage 3.3 / 3.6 workflow — the same `forward.journal.preflight` and
`forward.journal.record` calls the **Check** and **Record latest completed close** buttons make — for every active forward
journal, once per check. It automates capture only: no trading, order, paper order, strategy change, Strategy Fit run,
AI call, alert or scan. `forward/METHOD.md` and `forward/EXCURSIONS.md` are unchanged; this file documents Stage 3.7.

## Opt-in

Off by default. It is one global setting (`/api/forward-automation`, stored in the additive `app_settings` table) that
the user turns on in the Forward Journal panel. It covers every ACTIVE or CONTINUITY_BLOCKED journal; archived journals
are never captured. Reading the setting never creates a table, so a database where it was never turned on is unchanged.

## When it checks (the completed-session contract, unchanged)

Stage 3.3 records a session only once it is **complete**, and `backtest.bars.last_complete_session_date(now)` defines
complete as *every date before today in New York*. Monday's close therefore becomes recordable at 00:00 ET Tuesday; a
check at 4:15 PM ET Monday could only see Friday. Stage 3.7 does not change that rule. The default check time is
**00:15 ET** — the first safe moment after the session's date ends — and the user may choose another New York time; the
existing rule still decides what is complete.

Times are New York wall-clock times (`zoneinfo`, so daylight-saving changes are handled; never a fixed UTC offset). A
time that does not exist on a spring-forward night (e.g. 02:30) resolves to the same instant (03:30 EDT). Weekends,
holidays and other days without a new session need no calendar here: the check finds the session already recorded.

## What one check does

1. Takes a short lease (`app_leases`) so two scheduler loops never check at the same time.
2. Lists ACTIVE / CONTINUITY_BLOCKED journals (none → "No active forward journals", no market-data work).
3. Per journal: `preflight` (reads the bar cache; if it does not cover the latest completed date, one small SPY request
   that is not stored) → if READY / CAN_RECORD_WITH_MISSING_INPUT, `record`. Results: CAPTURED, ALREADY_RECORDED,
   NO_NEW_SESSION or ERROR (with its code). One journal's error never stops the others.
4. Saves a short summary (time, trigger, counts, per-journal result codes) as the last check.

Sessions nobody captured (server off, computer asleep) are stored as MISSED by that ordinary capture and never
reconstructed; an open cycle during a missed session is CONTINUITY BLOCKED and its MFE / MAE tracking becomes
incomplete — exactly as with a manual capture. There is no catch-up loop.

## Scheduler

One daemon thread per server process, started by the FastAPI lifespan only when the setting is on, and stopped on
shutdown. It sleeps until the next check (waking at most every 15 minutes to re-read the wall clock, which also covers a
computer that slept) and never touches market data while waiting. On start it checks once if the latest scheduled slot
was not checked yet (for example the server was off at 00:15); otherwise it waits. A retryable failure
(`DATA_UNAVAILABLE`, an unexpected error) retries the SAME check every 15 minutes, at most 4 times, then waits for the
next scheduled check. Errors are caught and reported; they never stop the server. **Check automation now** runs the same
check immediately (only while automation is on; the completed-session rule still applies — it is not a backfill).

## After-close extensions (Stage 4.1)

The same scheduler also runs registered after-close extensions (`register_extension`) — today only the saved Strategy
Scanner checks (`fit/SAVED_SCANS.md`). `automation.py` never imports them. The thread runs when automatic capture is on
OR an extension wants scheduled checks (a saved scan with alerts on). Each check: the forward capture first (only when
automatic capture is on; otherwise the summary says `FORWARD_CAPTURE_OFF`), then each extension, isolated, under its own
lease name and in its own `extensions` section of the summary — never merged into the capture counts. With no extension
wanted the summary is exactly the Stage 3.7 one.

Stage 4.3 adds an `after_cycle` phase: such an extension (Daily Brief desktop delivery) runs after the cycle's summary
is stored — forward capture, every regular extension, then the saved last-check — so it reads the completed cycle; its
result is added to that summary and saved again. Enabled desktop delivery alone keeps the thread running (the check then
only reads stored data). With no after-cycle extension wanted, a check is unchanged.

## Once per session

The journal's lock (`forward.journal.record`), ALREADY_RECORDED and the database's unique session / observation keys
remain authoritative: a manual Record and an automatic check at the same moment produce one capture; the other gets
ALREADY_RECORDED. The lease keeps two scheduler loops apart. Automatic capture expects a single server process.

## Known limitations

* Runs only while the local Stock Agent server is running and the computer is awake — no cloud or OS background service.
* No missed-session backfill; a provider failure that outlasts the retries leaves a gap.
* Manual and automatic capture share every data limitation (IEX daily bars, no verified earnings calendar, saved research
  only — nothing is generated).
* Forward capture is research evidence, not broker trading.
