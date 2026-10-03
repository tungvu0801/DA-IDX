"""
backtest — Stage 3.2 point-in-time historical backtester for SAVED Stage 3.1 strategy versions.

It answers one question: what would this exact, immutable strategy version have done historically under explicit
assumptions? It does not optimise, rank, recommend, paper trade or place orders, and it never calls Claude.

  bars.py       historical daily-bar cache (immutable datasets) filled only by an explicit download through the
                EXISTING data.market_data.fetch_daily_bars (Alpaca MARKET DATA, same feed / adjustment as the app)
  replay.py     a point-in-time stand-in for the Alpaca data client: serves bars through the close of T only
  snapshots.py  feature snapshots at T built by the EXISTING engine (analyze_symbols / compute_metrics /
                build_market_insights / compute_sector_context) and the Stage 3.1 registry extractors
  engine.py     the deterministic cash-only, long-only simulation (close-of-T decisions, next-open fills)
  metrics.py    result metrics, benchmarks and breakdowns with documented formulas
  runs.py       eligibility, preflight, run configuration hash and the one-shot run job
  store.py      SQLite access (additive Stage 3.2 tables, database/backtest_migrations.py)

See backtest/METHOD.md for every convention.
"""
