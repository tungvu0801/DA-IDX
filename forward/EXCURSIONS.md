# Stage 3.6 forward MFE / MAE — method

Stage 3.6 answers one question for forward REFERENCE cycles (Stage 3.3): **while a cycle was open, how far did the price
move in the favourable and adverse directions from the reference entry?** It is observed going forward, one captured
session at a time, with the existing **Record latest completed close** action — there is no new button, no scheduler,
no backfill, no sizing, no dollars, no costs and no order. `forward/METHOD.md` (Stage 3.3) and `backtest/METHOD.md`
(Stage 3.2) are unchanged; Stage 3.2's MFE / MAE formulas are not touched.

## Semantics (Stage 3.2 conventions, long only)

`backtest/engine.py` starts a position's max / min at the entry open, adds the entry day's full high / low, the full
ranges of later held days, and only the OPEN of the exit day; MFE = max(0, …), MAE = min(0, …). The forward journal uses
the same rules with the reference entry open (no slippage):

| row (`observation_type`) | captured session | prices used |
|---|---|---|
| `ENTRY_SESSION` | the session whose open is the reference entry | its full high and low |
| `HELD_SESSION` | every later captured session while the cycle is open, including the exit-signal session | its full high and low |
| `EXIT_OPEN` | the session whose open is the reference exit | its OPEN only — the later range is never read |
| `CONTINUITY_GAP` | the capture that found a missed session while the cycle was open | none (tracking incomplete) |

* session high % = (high / reference entry open − 1) × 100; session low % = (low / reference entry open − 1) × 100
* exit open % = (reference exit open / reference entry open − 1) × 100 — it is exactly the stored reference move, and it
  can extend MFE or MAE (a gap at the exit open counts, as in Stage 3.2)
* cumulative MFE = max(0, every session high %, the exit open %); cumulative MAE = min(0, every session low %, the exit
  open %). The database refuses MFE < 0 or MAE > 0.

## Price basis (Stage 3.3's, reused)

Every capture reads split / dividend adjusted bars, so an earlier session's prices can be restated by a later capture.
Each percentage therefore uses the entry session's open from the **same dataset** as the prices it is compared with
(`basis_entry_open`); the exit row uses exactly the basis Stage 3.3 stored with the reference exit (`price_basis`). A
split during an open cycle changes nothing (tested against the same journal without a split).

## No backfill (a contract the database enforces)

* The first capture made with Stage 3.6 stores the journal's tracking start (`forward_test_excursion_tracking`).
* A cycle is tracked only if its reference entry is captured on or after that start: the `ENTRY_SESSION` row needs the
  FILLED reference entry of that exact session. Every later row needs the cycle's `ENTRY_SESSION` row, so a cycle that
  was already open (or finished) before Stage 3.6 never gains a row — it stays `LEGACY_NOT_TRACKED`.
* A row can only be added for the session being captured (the latest captured session), from bars through that session;
  bars after it are never read. Rows are never updated or deleted; one row per (journal, symbol, cycle, session, type);
  recording a session twice returns `ALREADY_RECORDED` and adds nothing.

## Cycle tracking status

| status | meaning | in completed-cycle averages |
|---|---|---|
| `TRACKING` | open; every session since the entry observed — shown as **COMPLETE SO FAR**, not final | no |
| `COMPLETE` | tracked from the entry through the exit open — final MFE / MAE | yes |
| `INCOMPLETE_DUE_TO_CONTINUITY_GAP` | a session was missed while open (Stage 3.3 blocks the symbol); never reconstructed | no |
| `INCOMPLETE_DATA_UNAVAILABLE` | the entry-session open was missing from a capture's data (no consistent basis) | no |
| `LEGACY_NOT_TRACKED` | the reference entry happened before tracking started | no (never counted as 0) |

A captured session without a bar for the symbol is stored as `EXCURSION_DATA_UNAVAILABLE / NO_BAR_FOR_SESSION` and adds
nothing — Stage 3.2 counts no such day either, so tracking stays complete.

## Where it shows

* Forward Journal: a REFERENCE CYCLES · MFE / MAE table (entry, exit, reference move, MFE, MAE, tracking), the current
  MFE / MAE of an open cycle ("FORWARD JOURNAL STATE … Tracking: COMPLETE SO FAR"), the final values at the reference
  exit, and "Not tracked for this legacy forward cycle" for legacy cycles.
* Evidence (Stage 3.5): for a journal with Stage 3.6 evidence, the MFE / MAE sample ("3 tracked cycles of 7 completed
  cycles"), averages over tracked completed cycles only, and side-by-side rows with the historical average MFE / MAE and
  a forward − historical difference in percentage points. A journal without Stage 3.6 evidence is shown exactly as in
  Stage 3.5 ("Not tracked in Stage 3.3").

## Known limitations

* Only cycles entered after Stage 3.6 tracking started have MFE / MAE; legacy cycles stay untracked.
* A missed capture while a cycle is open ends its tracking (incomplete) — the same gap blocks the Stage 3.3 lifecycle.
* Daily bars only: there is no intraday path order (whether the high or the low came first is unknown).
* Reference fills are printed opens, not executions; there is no position size, dollar P&L or cost model.
* Historical (Stage 3.2) MFE / MAE is measured from the entry fill after slippage; forward from the reference open.
