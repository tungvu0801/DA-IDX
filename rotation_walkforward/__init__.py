"""
rotation_walkforward — Stage 4.9 WALK-FORWARD + ROBUSTNESS evaluation (DESIGN_49_WALKFORWARD.md). Research only.

Train-only candidate selection, test-only evaluation of ONE frozen configuration per window, aggregate out-of-sample
metrics, a documented robustness score, parameter and cost sensitivity, descriptive SPY regimes — all on top of the
Stage 4.8 replay (which itself reuses the Stage 4.7 engine). Nothing here trades, deploys, switches a configuration,
imports a broker module, reads Robinhood or calls a model.
"""
ENGINE_VERSION = "4.9.0"
ROBUSTNESS_VERSION = "rs_v1"
