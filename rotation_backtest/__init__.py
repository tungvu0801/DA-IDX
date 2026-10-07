"""
rotation_backtest — Stage 4.8 HISTORICAL ROTATION BACKTEST (DESIGN_48_HISTORICAL_BACKTEST.md). Research only.

A deterministic replay of the Stage 4.7 Portfolio Rotation model over historical daily bars: the SAME
rotation.engine.compute_rotation produces every decision; this package adds only the session calendar, point-in-time
truncation, next-open execution with explicit slippage and cost, cash / holdings accounting, daily marking, metrics,
append-only persistence and a research-only API. Nothing here trades, builds an order, imports a broker module, reads
Robinhood or calls a model.
"""
ENGINE_VERSION = "4.8.0"
