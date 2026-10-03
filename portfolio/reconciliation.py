"""
portfolio/reconciliation.py — "Your Robinhood account" reconciliation (Stage 2.7E).

Keeps distinct concepts distinct:
  ROBINHOOD PORTFOLIO VALUE         Robinhood-reported total value (get_portfolio.total_value)
  CURRENT POSITIONS VALUE           calculated: shares x selected quote (Stage 2.7C)
  CURRENT OPEN-POSITION COST BASIS  cost of the positions you hold NOW (average cost x shares)
  UNREALIZED P&L                    current positions value - open-position cost basis
  REALIZED P&L                      Robinhood-reported gains/losses on CLOSED positions (get_realized_pnl)
  NET CONTRIBUTIONS                 deposits - withdrawals: UNAVAILABLE — none of the allowlisted read tools
                                    returns deposit/withdrawal history, so it is never estimated
  TOTAL INVESTMENT RESULT           UNAVAILABLE for the same reason (never derived from cost basis)

Open-position cost basis is NOT "total money deposited" and is never labelled as such.
"""
from __future__ import annotations

from typing import Optional

from portfolio.analytics import PortfolioView
from portfolio.models import RealizedPnlSummary

CONTRIBUTIONS_UNAVAILABLE = "Total money added: unavailable from the current verified data source"
COST_BASIS_NOTE = ("Cost basis is what your CURRENT holdings cost. It is not necessarily the total amount you have "
                   "ever deposited.")

GLOSSARY = {
    "portfolio_value": "Portfolio value is what Robinhood says your whole account is worth right now: your stocks "
                       "plus cash and anything else in the account.",
    "positions_value": "Current positions value is your shares multiplied by their latest price, calculated by this "
                       "app. It can differ slightly from Robinhood's figure because of price timing.",
    "cost_basis": "Cost basis is what you paid for the shares you still own (your average price times your shares). "
                  "It does not include money from shares you already sold, and it is not your total deposits.",
    "unrealized_pnl": "Unrealized gain/loss is how much your current holdings are up or down versus what you paid. "
                      "It only becomes 'real' if you sell.",
    "realized_pnl": "Realized gain/loss is the profit or loss you already locked in by selling shares, as reported "
                    "by Robinhood.",
    "cash": "Cash is money in your account that is not invested in any stock.",
    "portfolio_weight": "Portfolio weight is how much of your whole portfolio a stock makes up. A high weight means "
                        "that one stock moves your account a lot.",
    "net_contributions": "Money added means deposits minus withdrawals. The current verified data source does not "
                         "provide deposit or withdrawal history, so this app cannot show it.",
}


def build_reconciliation(view: PortfolioView, realized: Optional[RealizedPnlSummary],
                         realized_status: Optional[str] = None) -> dict:
    s = view.snapshot
    realized_ok = realized is not None and realized.total_returns is not None
    return {
        "robinhood_portfolio_value": s.total_value,
        "current_positions_value": s.positions_market_value,
        "current_positions_value_complete": s.valuation_complete,
        "open_position_cost_basis": s.total_cost_basis,
        "unrealized_pnl": s.total_unrealized_pnl,
        "unrealized_pnl_pct": s.total_unrealized_pnl_pct,
        "unrealized_pnl_complete": s.unrealized_pnl_complete,
        "realized_pnl": realized.total_returns if realized_ok else None,
        "realized_pnl_span": (realized.span if realized_ok else None),
        "realized_pnl_status": "VERIFIED" if realized_ok else (realized_status or "UNAVAILABLE"),
        "realized_pnl_source": "Robinhood realized profit & loss (closed positions)" if realized_ok else None,
        "cash": s.cash,
        "net_contributions": None,
        "net_contributions_status": "UNAVAILABLE",
        "net_contributions_message": CONTRIBUTIONS_UNAVAILABLE,
        "total_investment_result": None,
        "total_investment_result_status": "UNAVAILABLE",
        "total_investment_result_message": ("Your overall investment result needs your deposit and withdrawal "
                                            "history, which the current verified data source does not provide."),
        "cost_basis_note": COST_BASIS_NOTE,
        "glossary": GLOSSARY,
        "account": {"alias": s.account_alias, "masked_id": s.account_masked_id},
    }
