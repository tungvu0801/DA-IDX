"""
data/events/ — Real event-data providers (Stage 2.6).

Each module here wraps exactly one REAL, already-verified source and
normalizes its output into data.events.models.NormalizedEvent — never a
provider-specific payload leaking into the rest of the app:

  corporate.py  REAL   Alpaca Corporate Actions API (splits/dividends/mergers/spin-offs)
  fomc.py       REAL   Official Federal Reserve FOMC calendar page (meetings/decisions/
                       minutes -- minutes ONLY when the Fed has explicitly published a
                       release date, never estimated as "3 weeks after the meeting")
  macro.py      REAL, key-gated   FRED release/dates API (CPI/PPI/Employment Situation/PCE/JOLTS)
  earnings.py   INTERFACE ONLY    no verified earnings-calendar source exists in this
                                  stack; always reports available=False, never fabricated

Nothing here ever fabricates a date, time, or event. See each module's
docstring for its exact source and honesty rules.
"""
