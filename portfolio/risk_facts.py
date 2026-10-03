"""
portfolio/risk_facts.py — Raw deterministic portfolio risk FACTS + a configuration-driven rule engine.

No concentration / cash / gain / loss thresholds are defined here. Rules come
only from config.PORTFOLIO_RISK_RULES_JSON, which is empty ("[]") until the
thresholds are reviewed; with no rules configured nothing is ever flagged.

Facts describe the portfolio. They are never converted into research evidence,
never change the Bullish/Neutral/Bearish percentages or the Research View, and
a gain or loss on its own is not treated as bullish or bearish.
"""
from __future__ import annotations

import json
import operator
from decimal import Decimal, InvalidOperation
from typing import Any, List, Optional

from portfolio.analytics import PortfolioView

_OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt, "==": operator.eq}
_SCOPES = {"position", "sector", "portfolio"}


def position_facts(view: PortfolioView) -> List[dict]:
    sector_weight = {s["sector"]: s["weight_pct"] for s in view.sector_exposure}
    return [{
        "symbol": p.symbol,
        "position_weight": p.portfolio_weight,
        "sector": p.sector,
        "sector_weight": sector_weight.get(p.sector),
        "unrealized_pnl_pct": p.unrealized_pnl_pct,
        "event_risk_level": p.event_risk_level,
        "quote_quality": p.quote_quality,
        "basis_available": p.basis_available,
    } for p in view.positions]


def portfolio_facts(view: PortfolioView) -> dict:
    s = view.snapshot
    weights = [p.portfolio_weight for p in view.positions if p.portfolio_weight is not None]
    sector_weights = [x["weight_pct"] for x in view.sector_exposure if x["weight_pct"] is not None]
    high = next((e for e in view.event_exposure if e["level"] == "HIGH"), None)
    unclassified = next((x for x in view.sector_exposure if x["sector"] == "UNCLASSIFIED"), None)
    return {
        "cash_pct": s.cash_pct,
        "valuation_gap_pct": s.valuation_gap_pct,
        "valuation_complete": s.valuation_complete,
        "position_count": len(view.positions),
        "largest_position_weight": max(weights) if weights else None,
        "largest_sector_weight": max(sector_weights) if sector_weights else None,
        "high_event_risk_weight": high["weight_pct"] if high else Decimal("0.00"),
        "unclassified_sector_weight": unclassified["weight_pct"] if unclassified else Decimal("0.00"),
        "total_unrealized_pnl_pct": s.total_unrealized_pnl_pct,
        "positions_missing_basis": view.basis_summary["missing"],
        "positions_with_quote_issues": len(view.quote_quality_summary["issues"]),
        "worst_quote_quality": view.quote_quality_summary["worst"],
    }


def sector_facts(view: PortfolioView) -> List[dict]:
    return [{"sector": x["sector"], "sector_weight": x["weight_pct"], "symbols": x["symbols"]}
            for x in view.sector_exposure]


def load_rules(raw_json: str) -> tuple[List[dict], List[str]]:
    """Parse configured rules; invalid rules are dropped and reported, never guessed."""
    errors: List[str] = []
    try:
        rules = json.loads(raw_json or "[]")
    except json.JSONDecodeError:
        return [], ["PORTFOLIO_RISK_RULES_JSON is not valid JSON; no rules applied."]
    if not isinstance(rules, list):
        return [], ["PORTFOLIO_RISK_RULES_JSON must be a JSON list; no rules applied."]
    valid = []
    for i, r in enumerate(rules):
        if not isinstance(r, dict) or r.get("scope") not in _SCOPES or r.get("op") not in _OPS \
                or not isinstance(r.get("fact"), str) or not r.get("id"):
            errors.append(f"rule #{i} is malformed and was ignored")
            continue
        try:
            threshold = Decimal(str(r["value"])) if not isinstance(r.get("value"), (bool, str)) else r["value"]
        except (InvalidOperation, KeyError):
            errors.append(f"rule '{r.get('id')}' has an invalid value and was ignored")
            continue
        valid.append({**r, "value": threshold})
    return valid, errors


def evaluate_rules(rules: List[dict], facts: dict) -> List[dict]:
    subjects = {
        "position": [(f["symbol"], f) for f in facts["positions"]],
        "sector": [(f["sector"], f) for f in facts["sectors"]],
        "portfolio": [("portfolio", facts["portfolio"])],
    }
    results = []
    for rule in rules:
        for subject, fact_row in subjects[rule["scope"]]:
            value: Any = fact_row.get(rule["fact"])
            triggered: Optional[bool]
            if value is None:
                triggered = None  # fact unavailable -> not evaluated, never assumed
            else:
                try:
                    triggered = bool(_OPS[rule["op"]](value, rule["value"]))
                except TypeError:
                    triggered = None
            results.append({"rule_id": rule["id"], "label": rule.get("label"), "scope": rule["scope"],
                            "subject": subject, "fact": rule["fact"], "value": value, "op": rule["op"],
                            "threshold": rule["value"], "triggered": triggered})
    return results


def build_risk_facts(view: PortfolioView, rules_json: str) -> dict:
    facts = {"positions": position_facts(view), "sectors": sector_facts(view), "portfolio": portfolio_facts(view)}
    rules, errors = load_rules(rules_json)
    return {
        **facts,
        "rules_configured": len(rules),
        "rule_results": evaluate_rules(rules, facts),
        "rule_errors": errors,
        "note": ("Raw facts only. Concentration, cash, gain and loss thresholds are not finalized and none are "
                 "applied unless explicitly configured. Portfolio facts never change research evidence."),
    }
