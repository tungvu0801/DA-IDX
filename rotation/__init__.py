"""
rotation/ — Stage 4.7 Deterministic Portfolio Rotation Engine (DESIGN_47_PORTFOLIO_ROTATION.md).

Phase 1 (this package today): the PURE deterministic core only — factor calculations (rotation/factors.py) and the
normalisation / composite / ranking / allocation / rank-buffer / rebalance / turnover rules (rotation/rules.py).
Everything here is Decimal arithmetic on values passed in: no clock, no randomness, no database, no network, no broker,
no Claude. The output of this package is a portfolio PROPOSAL vocabulary (ADD / INCREASE / DECREASE / EXIT / HOLD / NONE)
— never an order.
"""
