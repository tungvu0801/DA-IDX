"""
analysis/position_sizing.py — Deterministic position-sizing calculator
(the "Risk Manager" prepared in Milestone 1). Pure Python arithmetic —
Claude never decides a position size anywhere in this app.

No brokerage-equity data source exists anywhere in this project (no
Robinhood/Alpaca position sync), so `portfolio_value` must be supplied
explicitly by the caller — it is never guessed or auto-detected.

Sell/hold/reduce-position percentages for an EXISTING holding are
deliberately NOT computed here: that requires real portfolio/position data
(current shares held, cost basis, concentration, etc.) that this app
doesn't have. Use `position_adjustment_unavailable_message()` for that case.
"""
import math
from dataclasses import dataclass
from typing import Optional

import config

POSITION_ADJUSTMENT_UNAVAILABLE_MESSAGE = (
    "Position adjustment percentage unavailable — portfolio/risk data required."
)


@dataclass
class PositionSizing:
    portfolio_value: float
    entry_price: float
    invalidation_price: float
    max_risk_pct: float
    risk_per_share: Optional[float]
    max_risk_based_shares: Optional[int]
    max_allocation_based_shares: Optional[int]
    recommended_shares: Optional[int]
    position_value: Optional[float]
    position_pct_of_portfolio: Optional[float]
    max_loss_if_invalidated: Optional[float]


def compute_position_sizing(
    portfolio_value: float,
    entry_price: float,
    invalidation_price: float,
    max_risk_pct: float = config.MAX_RISK_PER_TRADE_PERCENT,
    max_position_pct: float = config.MAX_POSITION_PERCENT,
) -> PositionSizing:
    """
    risk_per_share = |entry - invalidation|
    max_risk_based_shares = floor(portfolio_value * max_risk_pct/100 / risk_per_share)
    max_allocation_based_shares = floor(portfolio_value * max_position_pct/100 / entry_price)
    recommended_shares = the smaller of the two (never violates either ceiling)
    """
    risk_per_share = abs(entry_price - invalidation_price)
    if risk_per_share <= 0 or portfolio_value <= 0 or entry_price <= 0:
        return PositionSizing(
            portfolio_value=portfolio_value,
            entry_price=entry_price,
            invalidation_price=invalidation_price,
            max_risk_pct=max_risk_pct,
            risk_per_share=risk_per_share if risk_per_share > 0 else None,
            max_risk_based_shares=None,
            max_allocation_based_shares=None,
            recommended_shares=None,
            position_value=None,
            position_pct_of_portfolio=None,
            max_loss_if_invalidated=None,
        )

    max_risk_based_shares = math.floor(portfolio_value * max_risk_pct / 100.0 / risk_per_share)
    max_allocation_based_shares = math.floor(portfolio_value * max_position_pct / 100.0 / entry_price)
    recommended_shares = max(0, min(max_risk_based_shares, max_allocation_based_shares))

    position_value = round(recommended_shares * entry_price, 2)
    position_pct = round(position_value / portfolio_value * 100.0, 2) if portfolio_value else None
    max_loss = round(recommended_shares * risk_per_share, 2)

    return PositionSizing(
        portfolio_value=portfolio_value,
        entry_price=entry_price,
        invalidation_price=invalidation_price,
        max_risk_pct=max_risk_pct,
        risk_per_share=round(risk_per_share, 2),
        max_risk_based_shares=max_risk_based_shares,
        max_allocation_based_shares=max_allocation_based_shares,
        recommended_shares=recommended_shares,
        position_value=position_value,
        position_pct_of_portfolio=position_pct,
        max_loss_if_invalidated=max_loss,
    )


def position_adjustment_unavailable_message() -> str:
    return POSITION_ADJUSTMENT_UNAVAILABLE_MESSAGE
