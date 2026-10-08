"""
rotation_campaign — Stage 5.0 MODEL EVALUATION CAMPAIGN / LEADERBOARD (DESIGN_50_MODEL_CAMPAIGN.md). Research only.

Compares a bounded set of deterministic Stage 4.7 configurations with the Stage 4.8 replay and the Stage 4.9 walk-forward
machinery (candidate grid, windows, evaluator, aggregation, robustness score), applies transparent eligibility gates,
ranks a leaderboard and names "paper-forward-test candidates". Not a strategy engine, not ML, not search, not deployment:
nothing here trades, activates a configuration, imports a broker module, reads Robinhood or calls a model.
"""
ENGINE_VERSION = "5.0.0"
FINALIST_LABEL = "paper-forward-test candidate"
