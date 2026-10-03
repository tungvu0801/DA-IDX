"""
portfolio/policy.py — Deterministic portfolio ATTENTION policy engine (Stage 2.7D).

Python alone decides whether a rule triggers; Claude never does. A triggered rule
means "this deserves attention" — it is never an instruction to buy, sell, reduce
or rebalance, and it never feeds back into research evidence, Research View,
Stage 2.5 snapshots/outcomes or Stage 2.6 event scoring. Nothing is persisted.

Thresholds come ONLY from configuration (config.PORTFOLIO_POLICY_*), as
"INFO,MEDIUM,HIGH" bands; an empty band is disabled and a malformed band is
reported and skipped (never guessed). Rules can be switched off by id.

Metric definitions (all percentages 0–100 of the CALCULATED total =
valued positions + cash + other reported assets, from cent-rounded values):
  position_weight            per position
  sector_weight              per verified sector (UNCLASSIFIED is excluded here and
                             reported separately as sector-coverage)
  cash_pct                   cash share
  high_event_exposure_pct    share in positions whose Stage 2.6 event_risk_level is HIGH
  event_unknown_weight_pct   share in positions with no event data
  unclassified_weight_pct    share in positions with no verified sector
  abs_valuation_gap_pct      |Robinhood equity value - calculated position value| / equity value
  quote_issue_count          positions with DEGRADED / UNRELIABLE / UNAVAILABLE quotes
  stale_quote_count          positions with STALE quotes
  unreliable_quote_value_pct share of value (incl. reference values of unvalued positions)
                             whose quote is UNRELIABLE / UNAVAILABLE
  missing_basis_count        positions with no cost basis
  missing_basis_value_pct    share of value (same denominator) missing cost basis
A metric that cannot be computed (e.g. zero portfolio value) is None and its rules
report triggered=None ("not evaluated"), never a guessed result.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import config

SEVERITIES = ["INFO", "LOW", "MEDIUM", "HIGH"]
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
CATEGORIES = ["POSITION_CONCENTRATION", "SECTOR_CONCENTRATION", "LOW_CASH", "EVENT_EXPOSURE", "QUOTE_QUALITY",
              "VALUATION_QUALITY", "MISSING_BASIS", "SECTOR_COVERAGE", "SCENARIO_CONCENTRATION"]
_OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt}
_CENT = Decimal("0.01")
_HUNDRED = Decimal("100")
UNCLASSIFIED = "UNCLASSIFIED"
SEPARATION_NOTE = "PORTFOLIO POLICY does not modify RESEARCH EVIDENCE."
BAND_SEVERITIES = ("INFO", "MEDIUM", "HIGH")


@dataclass(frozen=True)
class PortfolioPolicyRule:
    id: str
    name: str
    category: str
    metric: str
    scope: str                   # position | sector | portfolio
    operator: str
    threshold: Decimal
    severity: str
    explanation_template: str
    enabled: bool = True
    unit: str = "%"
    metric_source: str = ""
    family: str = ""


@dataclass(frozen=True)
class Holding:
    symbol: str
    market_value: Optional[Decimal]
    reference_value: Optional[Decimal]
    sector: str
    event_level: str
    quote_quality: str
    basis_available: bool
    hypothetical: bool = False


@dataclass(frozen=True)
class PolicyInputs:
    holdings: Tuple[Holding, ...]
    cash: Optional[Decimal]
    other_assets: Decimal
    abs_valuation_gap_pct: Optional[Decimal]


# ---- rule construction (config only) -------------------------------------------------------------------

_T_CONC = {"INFO": "Notable concentration.", "MEDIUM": "Elevated concentration — worth reviewing.",
           "HIGH": "High concentration — review before adding more exposure."}

FAMILIES = [
    # family, name, category, metric, scope, operator, config attr, template, metric source
    ("position_concentration", "Position concentration", "POSITION_CONCENTRATION", "position_weight", "position", ">=",
     "PORTFOLIO_POLICY_POSITION_WEIGHT_PCT",
     "{subject} is {value}% of the portfolio (configured {severity} attention level: {op} {threshold}%). {tail}",
     "market value / calculated total value"),
    ("sector_concentration", "Sector concentration", "SECTOR_CONCENTRATION", "sector_weight", "sector", ">=",
     "PORTFOLIO_POLICY_SECTOR_WEIGHT_PCT",
     "{subject} holdings are {value}% of the portfolio (configured {severity} attention level: {op} {threshold}%). "
     "{tail}", "sum of market values in a verified sector / calculated total value"),
    ("low_cash", "Low cash", "LOW_CASH", "cash_pct", "portfolio", "<", "PORTFOLIO_POLICY_LOW_CASH_PCT",
     "Cash is {value}% of the portfolio, below the configured {severity} attention level ({op} {threshold}%). "
     "Little cash is available without adding new money.", "Robinhood cash / calculated total value"),
    ("event_exposure", "HIGH event-risk exposure", "EVENT_EXPOSURE", "high_event_exposure_pct", "portfolio", ">=",
     "PORTFOLIO_POLICY_HIGH_EVENT_EXPOSURE_PCT",
     "{value}% of the portfolio is in positions with a known HIGH event risk (configured {severity} attention "
     "level: {op} {threshold}%). A scheduled event is not bullish or bearish by itself.",
     "Stage 2.6 event_risk_level per symbol (read-only)"),
    ("valuation_gap", "Valuation gap", "VALUATION_QUALITY", "abs_valuation_gap_pct", "portfolio", ">=",
     "PORTFOLIO_POLICY_VALUATION_GAP_PCT",
     "Robinhood's reported equity value and the calculated position value differ by {value}% (configured "
     "{severity} attention level: {op} {threshold}%).", "|Robinhood equity_value - calculated| / equity_value"),
    ("unreliable_quote_value", "Unreliable quote value", "QUOTE_QUALITY", "unreliable_quote_value_pct", "portfolio",
     ">=", "PORTFOLIO_POLICY_UNRELIABLE_QUOTE_VALUE_PCT",
     "{value}% of portfolio value has an unreliable or unavailable quote and is not valued live (configured "
     "{severity} attention level: {op} {threshold}%).", "reference values of positions with unusable quotes"),
    ("missing_basis_value", "Value missing cost basis", "MISSING_BASIS", "missing_basis_value_pct", "portfolio", ">=",
     "PORTFOLIO_POLICY_MISSING_BASIS_VALUE_PCT",
     "{value}% of portfolio value has no cost basis, so its P&L is unavailable (configured {severity} attention "
     "level: {op} {threshold}%).", "value of positions without average cost"),
]

FIXED_RULES = [
    # id, name, category, metric, scope, op, threshold, template, unit, source
    ("quote_quality.any_issue", "Any quote-quality issue", "QUOTE_QUALITY", "quote_issue_count", "portfolio", ">=",
     "1", "{value} position(s) have a degraded, unreliable or unavailable quote.", "positions",
     "quote assessment per position"),
    ("quote_quality.any_stale", "Any stale quote", "QUOTE_QUALITY", "stale_quote_count", "portfolio", ">=", "1",
     "{value} position(s) have a quote older than the configured freshness limit.", "positions",
     "quote age vs PORTFOLIO_QUOTE_STALE_SECONDS"),
    ("missing_basis.any", "Any position missing cost basis", "MISSING_BASIS", "missing_basis_count", "portfolio",
     ">=", "1", "{value} position(s) have no cost basis available.", "positions", "average cost from Robinhood"),
    ("sector_coverage.unclassified", "Unclassified sector exposure", "SECTOR_COVERAGE", "unclassified_weight_pct",
     "portfolio", ">", "0",
     "{value}% of the portfolio has no verified sector classification, so sector weights may understate actual "
     "sector concentration.", "%", "verified sector sources (portfolio/sectors.py)"),
    ("event_exposure.unknown_coverage", "Missing event data", "EVENT_EXPOSURE", "event_unknown_weight_pct",
     "portfolio", ">", "0", "{value}% of the portfolio has no event data available.", "%",
     "Stage 2.6 event context availability"),
]


def parse_bands(raw: str) -> Tuple[List[Optional[Decimal]], List[str]]:
    parts = [p.strip() for p in (raw or "").split(",")]
    parts += [""] * (3 - len(parts))
    out, errors = [], []
    if len(parts) > 3:
        errors.append(f"expected at most 3 bands (INFO,MEDIUM,HIGH), got {len(parts)}")
    for p in parts[:3]:
        if p == "":
            out.append(None)
            continue
        try:
            d = Decimal(p)
            if not d.is_finite() or d < 0:
                raise InvalidOperation
            out.append(d)
        except InvalidOperation:
            out.append(None)
            errors.append(f"invalid band value '{p}' ignored")
    return out, errors


def build_rules(cfg: Any = config) -> Tuple[List[PortfolioPolicyRule], List[str]]:
    disabled = {r.strip() for r in (getattr(cfg, "PORTFOLIO_POLICY_DISABLED_RULES", "") or "").split(",") if r.strip()}
    rules: List[PortfolioPolicyRule] = []
    errors: List[str] = []
    for family, name, category, metric, scope, op, attr, template, source in FAMILIES:
        bands, errs = parse_bands(getattr(cfg, attr, ""))
        errors += [f"{attr}: {e}" for e in errs]
        for severity, threshold in zip(BAND_SEVERITIES, bands):
            if threshold is None:
                continue
            rid = f"{family}.{severity.lower()}"
            text = template.replace("{tail}", _T_CONC.get(severity, "")) if "{tail}" in template else template
            rules.append(PortfolioPolicyRule(rid, f"{name} ({severity})", category, metric, scope, op, threshold,
                                             severity, text, rid not in disabled, "%", source, family))
    for rid, name, category, metric, scope, op, threshold, template, unit, source in FIXED_RULES:
        rules.append(PortfolioPolicyRule(rid, name, category, metric, scope, op, Decimal(threshold), "INFO", template,
                                         rid not in disabled, unit, source, rid))
    unknown = disabled - {r.id for r in rules}
    if unknown:
        errors.append(f"PORTFOLIO_POLICY_DISABLED_RULES: unknown rule id(s) {sorted(unknown)}")
    return rules, errors


# ---- inputs & metrics (pure) ------------------------------------------------------------------------------

def inputs_from_view(view) -> PolicyInputs:
    s = view.snapshot
    others = [s.options_value, s.crypto_value, s.futures_value, s.event_contracts_value, s.mutual_funds_value,
              s.fixed_income_value]
    gap = abs(s.valuation_gap_pct) if s.valuation_gap_pct is not None else None
    return PolicyInputs(
        holdings=tuple(Holding(p.symbol, p.market_value, p.reference_value, p.sector or UNCLASSIFIED,
                               p.event_risk_level or "UNKNOWN", p.quote_quality, p.basis_available)
                       for p in view.positions),
        cash=s.cash, other_assets=sum((v for v in others if v is not None), Decimal("0")),
        abs_valuation_gap_pct=gap)


def _pct(num: Optional[Decimal], den: Optional[Decimal]) -> Optional[Decimal]:
    if num is None or den is None or den == 0:
        return None
    r = (num / den * _HUNDRED).quantize(_CENT, rounding=ROUND_HALF_UP)
    return abs(r) if r == 0 else r


def compute_metrics(inp: PolicyInputs) -> dict:
    valued = [h for h in inp.holdings if h.market_value is not None]
    total = None if inp.cash is None else sum((h.market_value for h in valued), Decimal("0")) + inp.cash + inp.other_assets
    if total is not None and total <= 0:
        total = None  # zero/negative total: no percentage can be computed honestly
    unvalued = [h for h in inp.holdings if h.market_value is None]
    unvalued_refs_known = all(h.reference_value is not None for h in unvalued)
    dq_total = (total + sum((h.reference_value for h in unvalued), Decimal("0"))) \
        if total is not None and unvalued_refs_known else None

    def value_of(h: Holding) -> Optional[Decimal]:
        return h.market_value if h.market_value is not None else h.reference_value

    def share(pred) -> Optional[Decimal]:
        if dq_total is None:
            return None
        return _pct(sum((value_of(h) for h in inp.holdings if pred(h)), Decimal("0")), dq_total)

    sectors: Dict[str, Decimal] = {}
    for h in valued:
        if h.sector != UNCLASSIFIED:
            sectors[h.sector] = sectors.get(h.sector, Decimal("0")) + h.market_value

    def weight_where(pred) -> Optional[Decimal]:
        return _pct(sum((h.market_value for h in valued if pred(h)), Decimal("0")), total)

    return {
        "total_value": total,
        "position": {h.symbol: {"position_weight": _pct(h.market_value, total)} for h in inp.holdings},
        "sector": {name: {"sector_weight": _pct(v, total)} for name, v in sectors.items()},
        "portfolio": {"portfolio": {
            "cash_pct": _pct(inp.cash, total),
            "high_event_exposure_pct": weight_where(lambda h: h.event_level == "HIGH"),
            "event_unknown_weight_pct": weight_where(lambda h: h.event_level == "UNKNOWN"),
            "unclassified_weight_pct": weight_where(lambda h: h.sector == UNCLASSIFIED),
            "abs_valuation_gap_pct": inp.abs_valuation_gap_pct,
            "quote_issue_count": Decimal(sum(1 for h in inp.holdings
                                             if h.quote_quality in ("DEGRADED", "UNRELIABLE", "UNAVAILABLE"))),
            "stale_quote_count": Decimal(sum(1 for h in inp.holdings if h.quote_quality == "STALE")),
            "unreliable_quote_value_pct": share(lambda h: h.quote_quality in ("UNRELIABLE", "UNAVAILABLE")),
            "missing_basis_count": Decimal(sum(1 for h in inp.holdings if not h.basis_available)),
            "missing_basis_value_pct": share(lambda h: not h.basis_available),
        }},
    }


def hypothetical_inputs(inp: PolicyInputs, symbol: str, amount: Decimal, funding: str, sector: Optional[str],
                        event_level: Optional[str]) -> PolicyInputs:
    """Return NEW inputs with $amount added to `symbol`'s market value. The original is never modified."""
    holdings = list(inp.holdings)
    idx = next((i for i, h in enumerate(holdings) if h.symbol == symbol), None)
    if idx is not None:
        h = holdings[idx]
        if h.market_value is None:
            raise ValueError(f"{symbol} has no reliable market value")
        holdings[idx] = replace(h, market_value=h.market_value + amount,
                                reference_value=(h.reference_value or h.market_value) + amount)
    else:
        holdings.append(Holding(symbol, amount, amount, sector or UNCLASSIFIED, event_level or "UNKNOWN", "OK", True,
                                hypothetical=True))
    cash = inp.cash if funding == "new_money" or inp.cash is None else inp.cash - amount
    return replace(inp, holdings=tuple(holdings), cash=cash)


# ---- evaluation ---------------------------------------------------------------------------------------------

def _fmt(v: Optional[Decimal], unit: str) -> str:
    if v is None:
        return "N/A"
    return str(v.quantize(Decimal(1))) if unit == "positions" else str(v)


def evaluate(rules: List[PortfolioPolicyRule], metrics: dict) -> List[dict]:
    results = []
    for r in rules:
        if not r.enabled:
            continue
        for subject, row in metrics[r.scope].items():
            value = row.get(r.metric)
            triggered = None if value is None else bool(_OPS[r.operator](value, r.threshold))
            results.append({
                "rule_id": r.id, "family": r.family, "name": r.name, "category": r.category, "severity": r.severity,
                "scope": r.scope, "subject": subject, "metric": r.metric, "value": value, "operator": r.operator,
                "threshold": r.threshold, "unit": r.unit, "triggered": triggered, "metric_source": r.metric_source,
                "explanation": r.explanation_template.format(
                    subject=subject, value=_fmt(value, r.unit), threshold=_fmt(r.threshold, r.unit),
                    severity=r.severity, op=r.operator) if triggered else None,
            })
    return results


def effective_flags(results: List[dict]) -> List[dict]:
    """Overlapping bands: keep the highest triggered severity per (family, subject)."""
    best: Dict[Tuple[str, str], dict] = {}
    for r in results:
        if not r["triggered"]:
            continue
        key = (r["family"], r["subject"])
        if key not in best or SEVERITY_RANK[r["severity"]] > SEVERITY_RANK[best[key]["severity"]]:
            best[key] = r
    return sorted(best.values(), key=lambda r: (-SEVERITY_RANK[r["severity"]], CATEGORIES.index(r["category"]),
                                                -(r["value"] or 0)))


CARDS = [
    ("position_concentration", "Position Concentration", ["POSITION_CONCENTRATION"], "position_concentration"),
    ("sector_concentration", "Sector Concentration", ["SECTOR_CONCENTRATION"], "sector_concentration"),
    ("cash_level", "Cash Level", ["LOW_CASH"], "low_cash"),
    ("event_exposure", "Event Exposure", ["EVENT_EXPOSURE"], "event_exposure"),
    ("data_quality", "Data Quality", ["QUOTE_QUALITY", "VALUATION_QUALITY", "MISSING_BASIS", "SECTOR_COVERAGE"], None),
]

_OK_TEXT = {
    "position_concentration": "No position is at or above the configured attention levels.",
    "sector_concentration": "No verified sector is at or above the configured attention levels.",
    "cash_level": "Cash is not below the configured attention levels.",
    "event_exposure": "No meaningful share of the portfolio is in positions with known HIGH event risk.",
    "data_quality": "No quote, valuation, cost-basis or sector-coverage issue triggered a rule.",
}


def build_cards(rules: List[PortfolioPolicyRule], results: List[dict], metrics: dict) -> List[dict]:
    flags = effective_flags(results)
    cards = []
    for card_id, title, cats, primary_family in CARDS:
        card_results = [r for r in results if r["category"] in cats]
        card_flags = [f for f in flags if f["category"] in cats]
        evaluated = [r for r in card_results if r["triggered"] is not None]
        if card_flags:
            status = card_flags[0]["severity"]
        elif evaluated:
            status = "OK"
        else:
            status = "UNAVAILABLE"
        thresholds = [{"rule_id": r.id, "severity": r.severity, "operator": r.operator, "threshold": r.threshold,
                       "enabled": r.enabled} for r in rules if r.family == primary_family] if primary_family else []
        if card_id == "position_concentration":
            pos = [(s, m["position_weight"]) for s, m in metrics["position"].items() if m["position_weight"] is not None]
            top = max(pos, key=lambda x: x[1], default=(None, None))
        elif card_id == "sector_concentration":
            sec = [(s, m["sector_weight"]) for s, m in metrics["sector"].items() if m["sector_weight"] is not None]
            top = max(sec, key=lambda x: x[1], default=(None, None))
        elif card_id == "cash_level":
            top = ("cash", metrics["portfolio"]["portfolio"]["cash_pct"])
        elif card_id == "event_exposure":
            top = ("HIGH event risk", metrics["portfolio"]["portfolio"]["high_event_exposure_pct"])
        else:
            top = (None, None)
        by_severity: Dict[str, int] = {}
        for f in card_flags:
            by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
        cards.append({
            "card": card_id, "title": title, "status": status,
            "metric_subject": top[0], "metric_value": top[1],
            "thresholds": thresholds,
            "flags": card_flags,
            "flag_counts": by_severity,
            "explanation": card_flags[0]["explanation"] if card_flags else (
                _OK_TEXT[card_id] if status == "OK" else "Not enough data to evaluate this policy."),
        })
    return cards


def policy_report(view, rules: List[PortfolioPolicyRule], rule_errors: List[str]) -> dict:
    metrics = compute_metrics(inputs_from_view(view))
    results = evaluate(rules, metrics)
    return {
        "enabled": True,
        "separation_note": SEPARATION_NOTE,
        "note": "Attention flags only — not buy/sell/hold advice and not position-size targets. Thresholds are "
                "configurable (config.PORTFOLIO_POLICY_*).",
        "cards": build_cards(rules, results, metrics),
        "flags": effective_flags(results),
        "results": results,
        "rules": [rule_to_dict(r) for r in rules],
        "rule_errors": rule_errors,
        "metrics": {k: v for k, v in metrics.items() if k != "total_value"},
        "calculated_total_value": metrics["total_value"],
    }


def rule_to_dict(r: PortfolioPolicyRule) -> dict:
    return {"id": r.id, "name": r.name, "category": r.category, "metric": r.metric, "scope": r.scope,
            "operator": r.operator, "threshold": r.threshold, "severity": r.severity, "enabled": r.enabled,
            "unit": r.unit, "metric_source": r.metric_source, "explanation_template": r.explanation_template}


def scenario_impact(rules: List[PortfolioPolicyRule], before: PolicyInputs, after: PolicyInputs,
                    categories: Optional[List[str]] = None) -> dict:
    """Re-evaluate the SAME rules on the hypothetical state and diff the effective flags."""
    mb, ma = compute_metrics(before), compute_metrics(after)
    fb = {(f["family"], f["subject"]): f for f in effective_flags(evaluate(rules, mb))}
    fa = {(f["family"], f["subject"]): f for f in effective_flags(evaluate(rules, ma))}
    changes = {"newly_triggered": [], "escalated": [], "de_escalated": [], "resolved": [], "unchanged": []}
    for key in sorted(set(fb) | set(fa)):
        b, a = fb.get(key), fa.get(key)
        ref = a or b
        if categories and ref["category"] not in categories:
            continue
        item = {"family": key[0], "subject": key[1], "category": ref["category"], "scenario_category":
                "SCENARIO_CONCENTRATION", "before_severity": b["severity"] if b else None,
                "after_severity": a["severity"] if a else None, "before_value": b["value"] if b else None,
                "after_value": a["value"] if a else None, "explanation_after": a["explanation"] if a else None}
        if b is None:
            changes["newly_triggered"].append(item)
        elif a is None:
            changes["resolved"].append(item)
        elif SEVERITY_RANK[a["severity"]] > SEVERITY_RANK[b["severity"]]:
            changes["escalated"].append(item)
        elif SEVERITY_RANK[a["severity"]] < SEVERITY_RANK[b["severity"]]:
            changes["de_escalated"].append(item)
        else:
            changes["unchanged"].append(item)
    # values for items that did not trigger on one side
    for bucket in changes.values():
        for item in bucket:
            scope = "position" if item["family"] == "position_concentration" else \
                "sector" if item["family"] == "sector_concentration" else "portfolio"
            metric = next((r.metric for r in rules if r.family == item["family"]), None)
            if metric:
                if item["before_value"] is None:
                    item["before_value"] = mb[scope].get(item["subject"], {}).get(metric)
                if item["after_value"] is None:
                    item["after_value"] = ma[scope].get(item["subject"], {}).get(metric)
    return {**changes, "separation_note": SEPARATION_NOTE,
            "note": "Same configured rules re-evaluated on a hypothetical state. Arithmetic only; no order exists."}
