"""
strategy — Stage 3.1 STRATEGY LAB: explicit, declarative, versioned strategy definitions.

    features.py   the ONE feature registry every condition must reference (reuses existing calculations)
    spec.py       the strategy schema: validation, normalisation, canonical JSON + SHA-256, readiness, summary
    evaluate.py   the single meaning of every operator (pure; no data fetching, no simulation)
    examples.py   one clearly labelled example (never saved automatically)
    store.py      immutable, versioned storage in the Stage 3 SQLite tables
    CONTRACTS.md  what Stage 3.2 (backtests) and a future paper bot must consume and record

Nothing in this package executes user-supplied code, calls Claude, reads Robinhood, places orders or runs a
backtest. Strategies are data: registered features, whitelisted operators, literal values.
"""
