"""
portfolio/scenarios.py — PURE arithmetic "what if" simulations (Stage 2.7C).

These functions take an already-computed PortfolioView and numbers, and return
numbers. They never place, prepare, construct or submit an order, never call
the gateway or Robinhood, and never modify any state. A hypothetical purchase
is described only as "$X added to <symbol>'s market value" — no share count,
order type, limit price or any other executable detail is produced.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from typing import Optional

from portfolio.analytics import EVENT_LEVELS, PortfolioView, money, pct
from portfolio.policy import hypothetical_inputs, inputs_from_view, scenario_impact

MAX_SCENARIO_AMOUNT = Decimal("100000000")
DISCLAIMER = ("Hypothetical arithmetic only. No order was created, prepared or sent, and nothing in your "
              "Robinhood account changed.")
FUNDING_MODES = {"cash", "new_money"}


class ScenarioError(ValueError):
    pass


def _amount(value) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ScenarioError("amount_usd must be a number") from None
    if not amount.is_finite() or amount <= 0 or amount > MAX_SCENARIO_AMOUNT:
        raise ScenarioError("amount_usd must be greater than 0 and at most 100,000,000")
    return amount


def _totals(view: PortfolioView) -> tuple[Decimal, Decimal]:
    s = view.snapshot
    if s.calculated_total_value is None or s.cash is None:
        raise ScenarioError("portfolio totals are unavailable, so no scenario can be computed")
    return s.calculated_total_value, s.cash


def hypothetical_add(view: PortfolioView, symbol: str, amount_usd, funding: str = "cash",
                     sector: Optional[str] = None, rules: Optional[list] = None,
                     event_level: Optional[str] = None) -> dict:
    """Weight/sector/cash arithmetic after adding $amount of market value to `symbol`.

    funding="cash": the amount comes out of cash (total unchanged; cash may go negative -> flagged).
    funding="new_money": the amount is new money (total grows by the amount; cash unchanged).
    """
    if funding not in FUNDING_MODES:
        raise ScenarioError("funding must be 'cash' or 'new_money'")
    amount = _amount(amount_usd)
    symbol = symbol.strip().upper()
    total, cash = _totals(view)
    held = next((p for p in view.positions if p.symbol == symbol), None)
    if held is not None and held.market_value is None:
        raise ScenarioError(f"{symbol} is held but has no reliable market value, so its weight can't be computed")
    sector_name = held.sector if held is not None else (sector or "UNCLASSIFIED")
    mv_before = held.market_value if held is not None else Decimal("0")
    sector_row = next((s for s in view.sector_exposure if s["sector"] == sector_name), None)
    sector_before = sector_row["market_value"] if sector_row and sector_row["market_value"] is not None \
        else Decimal("0")

    total_after = total if funding == "cash" else total + amount
    cash_after = cash - amount if funding == "cash" else cash
    result = {
        "scenario": "hypothetical_add",
        "inputs": {"symbol": symbol, "amount_usd": money(amount), "funding": funding},
        "held_now": held is not None,
        "sector": sector_name,
        "position_weight_before_pct": pct(mv_before, total),
        "position_weight_after_pct": pct(mv_before + amount, total_after),
        "position_value_before": money(mv_before),
        "position_value_after": money(mv_before + amount),
        "sector_weight_before_pct": pct(sector_before, total),
        "sector_weight_after_pct": pct(sector_before + amount, total_after),
        "cash_before": money(cash),
        "cash_after": money(cash_after),
        "cash_pct_before": pct(cash, total),
        "cash_pct_after": pct(cash_after, total_after),
        "total_value_before": money(total),
        "total_value_after": money(total_after),
        "insufficient_cash": funding == "cash" and cash_after < 0,
        "feasible": not (funding == "cash" and cash_after < 0),
        "available_cash": money(cash),
        "additional_cash_needed": money(amount - cash) if (funding == "cash" and cash_after < 0) else None,
        "infeasible_label": "INFEASIBLE SCENARIO" if (funding == "cash" and cash_after < 0) else None,
        "valuation_complete": view.snapshot.valuation_complete,
        "funding_explanation": ("Funded from existing cash: cash falls by the amount and the portfolio total is "
                                "unchanged." if funding == "cash" else
                                "Funded with new money: cash is unchanged and the portfolio total grows by the "
                                "amount, so every other weight shrinks slightly."),
        "sector_classification_uncertain": sector_name == "UNCLASSIFIED",
        "disclaimer": DISCLAIMER,
    }
    if sector_name == "UNCLASSIFIED":
        result["sector_note"] = (f"{symbol} has no verified sector classification, so its sector impact cannot be "
                                 "assessed and sector weights may understate actual concentration.")
    if rules is not None:
        before = inputs_from_view(view)
        level = held.event_risk_level if held is not None else (event_level or "UNKNOWN")
        after = hypothetical_inputs(before, symbol, amount, funding, sector_name, level)
        result["policy_impact"] = scenario_impact(rules, before, after)
    return result


def event_risk_exposure(view: PortfolioView, level: str = "HIGH") -> dict:
    """Fraction of the calculated portfolio value in positions at exactly `level` Stage 2.6 event risk."""
    level = level.upper()
    if level not in EVENT_LEVELS:
        raise ScenarioError(f"level must be one of {', '.join(EVENT_LEVELS)}")
    total, _ = _totals(view)
    row = next((e for e in view.event_exposure if e["level"] == level), None)
    mv = row["market_value"] if row else Decimal("0")
    return {
        "scenario": "event_risk_exposure",
        "inputs": {"level": level},
        "market_value": money(mv),
        "weight_pct": pct(mv, total),
        "symbols": row["symbols"] if row else [],
        "unknown_event_data_symbols": next((e["symbols"] for e in view.event_exposure if e["level"] == "UNKNOWN"), []),
        "disclaimer": DISCLAIMER,
    }


def hypothetical_purchase_cash_pct(view: PortfolioView, amount_usd, rules: Optional[list] = None) -> dict:
    """Cash % after a hypothetical $amount purchase of anything, funded from cash."""
    amount = _amount(amount_usd)
    total, cash = _totals(view)
    result = {
        "scenario": "cash_after_purchase",
        "inputs": {"amount_usd": money(amount)},
        "cash_before": money(cash),
        "cash_after": money(cash - amount),
        "cash_pct_before": pct(cash, total),
        "cash_pct_after": pct(cash - amount, total),
        "insufficient_cash": cash - amount < 0,
        "feasible": not (cash - amount < 0),
        "available_cash": money(cash),
        "additional_cash_needed": money(amount - cash) if cash - amount < 0 else None,
        "infeasible_label": "INFEASIBLE SCENARIO" if cash - amount < 0 else None,
        "disclaimer": DISCLAIMER,
    }
    if rules is not None:
        before = inputs_from_view(view)
        after = hypothetical_inputs(before, "(unspecified purchase)", amount, "cash", None, None)
        result["policy_impact"] = scenario_impact(rules, before, after, categories=["LOW_CASH"])
    return result


def run_scenario(view: PortfolioView, request: dict, sector_fn, rules: Optional[list] = None,
                 event_level_fn=None) -> dict:
    """Dispatch a validated scenario request. The view is deep-copied so it can never be mutated."""
    view = copy.deepcopy(view)
    kind = request.get("type")
    if kind == "hypothetical_add":
        symbol = str(request.get("symbol") or "").strip().upper()
        if not symbol or not symbol.replace(".", "").replace("-", "").isalnum() or len(symbol) > 10:
            raise ScenarioError("symbol is required (e.g. MU)")
        held = any(p.symbol == symbol for p in view.positions)
        level = event_level_fn(symbol) if (event_level_fn is not None and not held) else None
        return hypothetical_add(view, symbol, request.get("amount_usd"), request.get("funding", "cash"),
                                sector=sector_fn(symbol), rules=rules, event_level=level)
    if kind == "event_risk_exposure":
        return event_risk_exposure(view, str(request.get("level", "HIGH")))
    if kind == "cash_after_purchase":
        return hypothetical_purchase_cash_pct(view, request.get("amount_usd"), rules=rules)
    raise ScenarioError("type must be one of: hypothetical_add, event_risk_exposure, cash_after_purchase")
