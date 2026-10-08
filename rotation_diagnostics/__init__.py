"""
rotation_diagnostics — Stage 5.1 ATTRIBUTION & BENCHMARK DIAGNOSTICS (DESIGN_51_ATTRIBUTION.md). Diagnostics only.

Equal-weight and buy-and-hold universe benchmarks (Stage 4.8 replays of derived configurations, so the same next-open fills,
costs and eligibility), symbol / sector attribution, leave-one-out and leave-sector-out ablations, cost-adjusted excess,
drawdown comparison, window and regime diagnostics, documented research flags and a PASS / WARN / FAIL scorecard for
configurations already evaluated by a Stage 5.0 campaign. Nothing here changes a signal, tunes a weight, promotes,
deploys, trades, imports a broker module, reads Robinhood or calls a model.
"""
ENGINE_VERSION = "5.1.0"
FLAGS_VERSION = "dx_v1"
