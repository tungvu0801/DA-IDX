"""
forward — Stage 3.3 FORWARD-TEST SIGNAL JOURNAL for SAVED Stage 3.1 strategy versions.

It answers one question: what did this exact, immutable strategy version say at each market close that was captured
AFTER that session happened? It is user-triggered only (no scheduler, no background evaluation), it never backfills a
missed session, it keeps only a light shadow state per symbol (no equity, cash, shares or orders), and it never calls
Claude or any broker.

  capture.py   what a capture reads: the Stage 3.2 bar cache + day_snapshot (technical), saved research snapshots,
               the Stage 2.6 event context — all existing code, no new feature maths
  journal.py   eligibility, creation (forward start = the day after creation), preflight, the per-symbol state machine,
               recording one completed session, and read-only views
  store.py     SQLite access (additive Stage 3.3 tables, database/forward_migrations.py)

See forward/METHOD.md for every convention.
"""
