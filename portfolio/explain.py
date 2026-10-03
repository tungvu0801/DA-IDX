"""
portfolio/explain.py — Claude explanations of already-computed facts (Stage 2.7C/D/E).

Claude receives ONLY normalized facts and deterministic analytics (numbers pre-formatted by Python). It
explains; it never calculates, recommends, sizes, forecasts, or prepares an order. Deterministic guards then
check the text; any failure WITHHOLDS the explanation (fail closed):

  grounding   every number must equal a number in the facts — exactly, rounded or truncated to 0/1/2
              decimals, or its absolute value. Differences, conversions and ratios are NOT accepted unless
              Python precomputed them into the facts (see _derived()).
  magnitude   abbreviated amounts ("$8.2k") and multiples ("2.5x", "3 times") are rejected — they are new
              arithmetic even when the digits happen to match an unrelated fact.
  language    buy/sell/trim/reduce/rebalance/order/share-count wording is rejected.
  forecast    predictions, probabilities, targets and guarantees are rejected.
  causal      (market explanations) "because / due to / driven by / investors fear ..." is rejected outside
              items that cite a supplied, sourced news headline.

Research context is read-only (latest SAVED snapshot, SELECT only). Evidence, Research View, snapshots,
outcomes and event scoring are never modified.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Callable, List, Optional

from agents.gating import UNAVAILABLE_MESSAGE, run_gated_agent, strip_markdown_fence
from agents.technical_agent import get_provider
from portfolio.analytics import PortfolioView
from portfolio.models import to_jsonable

logger = logging.getLogger(__name__)

_COMMON_RULES = """- Use ONLY numbers that appear in the facts, copied exactly as written (you may drop a trailing ".00"
  or round to fewer decimals). Never calculate, subtract, add, convert units, or write ratios/multiples.
  When you describe a change, use the precomputed values in "derived" (for example
  position_weight_change_pp) instead of doing the subtraction yourself. Never abbreviate amounts ("$8.2k").
- Never recommend buying, selling, holding, trimming, reducing, rebalancing or any position size. Never give a
  buy/sell/hold percentage, a target weight, a share count or a reduction amount. Never describe placing or
  preparing an order. Do not use the words "sell", "buy", "trim", "reduce", "rebalance" or "liquidate".
- Never forecast prices or the market, never give a probability or chance, never promise an outcome.
- A gain or loss is not evidence that a stock will go up or down. Scheduled events are not bullish or bearish."""

SYSTEM_PROMPT = f"""You explain a person's EXISTING stock portfolio to a beginner in plain, friendly language.

You are given a JSON object of facts. Every number was computed by the application from the person's
read-only brokerage data and its configured rules. Follow these rules exactly:

{_COMMON_RULES}
- The "portfolio_policy" flags and any "adding_money_check" states (fit, timing) were decided by the
  application's deterministic rules. Report them exactly; never change, add or remove a flag or state, and
  never choose or invent a threshold.
- Keep RESEARCH VIEW (saved research in "research_view_context", may be outdated — say so) separate from
  PORTFOLIO POLICY (attention flags about the person's holdings). Neither changes the other.
- Cost basis is what the CURRENT holdings cost; it is not the total money deposited. "Money added" is
  unavailable from the data source — say so if you mention it.

Respond with ONLY a JSON object with exactly these keys:
  "where_you_are_now": 1-3 sentences (account value, open-position cost, open-position gain/loss),
  "going_well": array of 0-3 short statements,
  "deserves_attention": array of 1-4 short statements about the portfolio policy flags,
  "research_says": array of 0-4 short statements about saved research only (say if none is available),
  "watch_next": array of 1-3 short statements about what to watch (no instructions),
  "adding_money": array of 0-3 short statements explaining the adding-money check if present, else empty,
  "caveats": array of 1-3 short data-quality caveats.
No prose before or after the JSON."""

MARKET_SYSTEM_PROMPT = f"""You explain the CURRENT U.S. stock-market context to a beginner in plain language.

You are given a JSON object of facts computed by the application from verified market data, a fixed
80-stock breadth list (NOT the whole market), verified scheduled events and sourced news headlines.
Follow these rules exactly:

{_COMMON_RULES}
- Do not say WHY the market moved unless a supplied news item says so. Keep three things separate:
    "observed": what the data shows (restate facts only),
    "sourced_news": what a supplied headline reports — start each item with its number, e.g. "[1] ...",
    "interpretation": at most 2 cautious sentences using "may" or "could"; no causes, no forecasts.
- The environment, trend, volatility, risk-appetite, breadth and difficulty labels were decided by the
  application. Report them exactly; never change them.

Respond with ONLY a JSON object with exactly these keys:
  "observed": array of 2-3 sentences,
  "sourced_news": array of 0-2 sentences (empty if no news is supplied),
  "interpretation": array of 0-2 sentences,
  "why_it_matters": 1-2 sentences,
  "what_to_watch": array of 1-3 short statements.
No prose before or after the JSON."""

_FORBIDDEN_LANGUAGE = [
    re.compile(p, re.I) for p in (
        r"\bsell(s|ing)?\b", r"\bbuys?\b", r"\btrim(s|med|ming)?\b", r"\breduc(e|es|ed|ing|tion)\b",
        r"\brebalanc\w*", r"\bliquidat\w*", r"\bplace (an? )?(buy |sell )?orders?\b", r"\border (to|for)\b",
        r"\b\d[\d,.]*\s+shares?\b", r"\btarget (weight|allocation|position)\b", r"\bexit (the|your|this) position\b",
        r"\bput all\b", r"\ball in\b",
    )
]
_FORECAST_LANGUAGE = [
    re.compile(p, re.I) for p in (
        r"\bwill (rise|fall|drop|go up|go down|rally|crash|recover|continue|rebound|climb|decline)\b",
        r"\b(is|are) (likely|expected|set|poised|bound) to\b", r"\bprobabilit\w*\b", r"\bchance of\b",
        r"\b\d[\d.,]*\s*% chance\b", r"\bprice target\b", r"\bguarantee\w*\b", r"\bperfect entry\b",
        r"\bbottom (is|has) (in|been)\b", r"\bdefinitely\b", r"\bcertain(ly)? to\b", r"\bforecast\w*\b",
    )
]
_CAUSAL_LANGUAGE = [
    re.compile(p, re.I) for p in (
        r"\bbecause\b", r"\bdue to\b", r"\bdriven by\b", r"\bcaused by\b", r"\bon (fears|hopes|worries)\b",
        r"\binvestors (fear|worry|are worried|are concerned|expect)\b", r"\bthanks to\b", r"\bsparked by\b",
    )
]
_MAGNITUDE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:[kKmMbB]\b|x\b|times\b|-fold\b)")
_NUM = re.compile(r"(?<![A-Za-z_])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")
_GENERIC_SMALL_INTS = {Decimal(i) for i in range(0, 11)}


def forbidden_language(text: str) -> List[str]:
    """Order / sizing / buy-sell wording. Any match withholds the AI explanation (fail closed)."""
    return sorted({m.group(0) for rx in _FORBIDDEN_LANGUAGE for m in rx.finditer(text)})


_NEGATED_GUARANTEE = re.compile(
    r"(?i)(\b(does not|do not|doesn't|don't|did not|didn't|is not|isn't|are not|aren't|not|no|never|without|cannot|"
    r"can't)\b|≠)\s+(a\s+|an\s+|any\s+|automatically\s+|necessarily\s+)*guarantee\w*")


def forecast_language(text: str) -> List[str]:
    # Negated statements ("a good decision does not guarantee profit", "≠ GUARANTEED PROFIT") are the required
    # beginner lesson, not a promise; they are removed before checking. Affirmative guarantees are still rejected.
    text = _NEGATED_GUARANTEE.sub(" ", text)
    return sorted({m.group(0) for rx in _FORECAST_LANGUAGE for m in rx.finditer(text)})


def causal_language(text: str) -> List[str]:
    return sorted({m.group(0) for rx in _CAUSAL_LANGUAGE for m in rx.finditer(text)})


def magnitude_terms(text: str) -> List[str]:
    return sorted({m.group(0).strip() for m in _MAGNITUDE.finditer(text)})


def _to_decimal(token: str) -> Optional[Decimal]:
    cleaned = token.replace("$", "").replace(",", "").replace("%", "").replace("+", "")
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def allowed_numbers(facts_text: str) -> set:
    allowed = set()
    for tok in _NUM.findall(facts_text):
        d = _to_decimal(tok)
        if d is None:
            continue
        for v in (d, abs(d)):
            allowed.add(v.normalize())
            for places in (0, 1, 2):
                q = Decimal(1).scaleb(-places)
                allowed.add(v.quantize(q, rounding=ROUND_HALF_UP).normalize())   # "about $8,210"
                allowed.add(v.quantize(q, rounding=ROUND_DOWN).normalize())      # "about $8,209" (truncated)
    return allowed


def ungrounded_numbers(text: str, facts_text: str) -> List[str]:
    allowed = allowed_numbers(facts_text)
    bad = []
    for tok in _NUM.findall(text):
        d = _to_decimal(tok)
        if d is None:
            continue
        if d.normalize() in allowed or abs(d).normalize() in allowed or abs(d) in _GENERIC_SMALL_INTS:
            continue
        bad.append(tok)
    return bad


def check_text(text: str, facts_text: str, *, market: bool = False, causal_exempt: str = "") -> dict:
    """All deterministic guards. Returns {} when the text passes, else the failing category -> tokens."""
    failures = {}
    for name, fn in (("forbidden_language", forbidden_language), ("forecast_language", forecast_language),
                     ("magnitude_terms", magnitude_terms)):
        hits = fn(text)
        if hits:
            failures[name] = hits
    if market:
        causal = causal_language(text.replace(causal_exempt, "")) if causal_exempt else causal_language(text)
        if causal:
            failures["unsupported_causal_claims"] = causal
    bad = ungrounded_numbers(text, facts_text)
    if bad:
        failures["ungrounded_numbers"] = bad
    return failures


def _fmt(v):
    return None if v is None else str(v)


def _d(v) -> Optional[Decimal]:
    try:
        return None if v is None else Decimal(str(v))
    except InvalidOperation:
        return None


def _diff(after, before) -> Optional[str]:
    a, b = _d(after), _d(before)
    if a is None or b is None:
        return None
    return str((a - b).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _derived(view: PortfolioView, scenario: Optional[dict]) -> dict:
    """Numbers a beginner explanation naturally needs, precomputed by Python so Claude never does arithmetic."""
    d = {"quote_age_minutes": {p.symbol: round(p.quote_age_seconds / 60) for p in view.positions
                               if p.quote_age_seconds is not None}}
    if scenario:
        d["scenario"] = {
            "position_weight_change_pp": _diff(scenario.get("position_weight_after_pct"),
                                               scenario.get("position_weight_before_pct")),
            "sector_weight_change_pp": _diff(scenario.get("sector_weight_after_pct"),
                                             scenario.get("sector_weight_before_pct")),
            "position_value_change": _diff(scenario.get("position_value_after"), scenario.get("position_value_before")),
            "cash_change": _diff(scenario.get("cash_after"), scenario.get("cash_before")),
            "cash_pct_change_pp": _diff(scenario.get("cash_pct_after"), scenario.get("cash_pct_before")),
            "total_value_change": _diff(scenario.get("total_value_after"), scenario.get("total_value_before")),
            "additional_cash_needed": _fmt(scenario.get("additional_cash_needed")),
        }
    return d


def _policy_facts(policy: Optional[dict]) -> dict:
    if not policy or not policy.get("enabled"):
        return {"enabled": False}
    return {
        "enabled": True,
        "separation_note": policy.get("separation_note"),
        "cards": [{"card": c["title"], "status": c["status"], "metric_subject": c["metric_subject"],
                   "metric_value": _fmt(c["metric_value"]),
                   "thresholds": [{"severity": t["severity"], "operator": t["operator"],
                                   "threshold": _fmt(t["threshold"])} for t in c["thresholds"] if t["enabled"]]}
                  for c in policy["cards"]],
        "flags": [{"rule_id": f["rule_id"], "category": f["category"], "severity": f["severity"],
                   "subject": f["subject"], "value": _fmt(f["value"]), "operator": f["operator"],
                   "threshold": _fmt(f["threshold"]), "explanation": f["explanation"]} for f in policy["flags"]],
    }


def _check_facts(check: Optional[dict]) -> Optional[dict]:
    if not check:
        return None
    return to_jsonable({
        "symbol": check["symbol"], "amount_usd": check["amount_usd"], "funding": check["funding"],
        "feasible": check["feasibility"]["feasible"],
        "additional_cash_needed": check["feasibility"]["additional_cash_needed"],
        "fit_state": check["fit"]["state"], "fit_supporting": [f["text"] for f in check["fit"]["supporting"]],
        "fit_caution": [f["text"] for f in check["fit"]["caution"]],
        "timing_status": check["timing"]["status"], "timing_supporting": [f["text"] for f in check["timing"]["supporting"]],
        "timing_patience": [f["text"] for f in check["timing"]["patience"]], "watch_for": check["timing"]["watch_for"],
        "price_location": check["price_area"]["location"], "support": check["price_area"]["support"],
        "resistance": check["price_area"]["resistance"], "current_price": check["price_area"]["current_price"],
        "market_environment": check["layers"]["market"]["environment"],
        "research_view": check["layers"]["stock"]["research"]["research_view"],
        "research_age_hours": check["layers"]["stock"]["research"]["age_hours"],
        "interpretation": check["layers"]["interpretation"],
    })


def build_facts(view: PortfolioView, risk: dict, research: dict, scenario: Optional[dict] = None,
                policy: Optional[dict] = None, reconciliation: Optional[dict] = None,
                check: Optional[dict] = None) -> dict:
    s = view.snapshot
    facts = {
        "account": {"alias": s.account_alias, "masked_id": s.account_masked_id},
        "values": {
            "robinhood_reported_total_value": _fmt(s.total_value),
            "robinhood_reported_equity_value": _fmt(s.equity_value),
            "calculated_position_value": _fmt(s.positions_market_value),
            "calculated_total_value": _fmt(s.calculated_total_value),
            "valuation_gap": _fmt(s.valuation_gap), "valuation_gap_pct": _fmt(s.valuation_gap_pct),
            "valuation_gap_material": s.valuation_gap_material,
            "cash": _fmt(s.cash), "cash_pct": _fmt(s.cash_pct), "buying_power": _fmt(s.buying_power),
            "open_position_cost_basis": _fmt(s.total_cost_basis), "total_unrealized_pnl": _fmt(s.total_unrealized_pnl),
            "total_unrealized_pnl_pct": _fmt(s.total_unrealized_pnl_pct),
            "realized_pnl": _fmt(s.realized_pnl_window), "realized_pnl_span": s.realized_pnl_span,
        },
        "positions": [{
            "symbol": p.symbol, "sector": p.sector, "market_value": _fmt(p.market_value),
            "weight_pct": _fmt(p.portfolio_weight), "cost_basis": _fmt(p.cost_basis_total),
            "unrealized_pnl": _fmt(p.unrealized_pnl), "unrealized_pnl_pct": _fmt(p.unrealized_pnl_pct),
            "quote_quality": p.quote_quality, "basis_available": p.basis_available,
            "event_risk_level": p.event_risk_level,
            "nearest_event": (p.nearest_event or {}).get("title"),
        } for p in view.positions],
        "research_view_context": {
            "label": "RESEARCH VIEW - latest saved research snapshots (may be outdated); unchanged by portfolio policy",
            "by_symbol": research or {},
        },
        "portfolio_policy": _policy_facts(policy),
        "sector_exposure": [{"sector": x["sector"], "weight_pct": _fmt(x["weight_pct"]), "symbols": x["symbols"]}
                            for x in view.sector_exposure],
        "event_exposure": [{"level": e["level"], "weight_pct": _fmt(e["weight_pct"]), "symbols": e["symbols"]}
                           for e in view.event_exposure],
        "data_quality": {"quote_quality_worst": view.quote_quality_summary["worst"],
                         "quote_issues": view.quote_quality_summary["issues"],
                         "positions_missing_basis": view.basis_summary["missing_symbols"],
                         "valuation_complete": s.valuation_complete, "messages": view.messages,
                         "risk_thresholds_note": risk.get("note")},
        "derived": _derived(view, scenario),
    }
    if reconciliation is not None:
        facts["reconciliation"] = {k: reconciliation[k] for k in (
            "robinhood_portfolio_value", "current_positions_value", "open_position_cost_basis", "unrealized_pnl",
            "unrealized_pnl_pct", "realized_pnl", "realized_pnl_span", "realized_pnl_status", "cash",
            "net_contributions_status", "net_contributions_message", "cost_basis_note")}
    if scenario is not None:
        facts["scenario_result"] = to_jsonable(scenario)
    if check is not None:
        facts["adding_money_check"] = _check_facts(check)
    return to_jsonable(facts)


def latest_saved_research(symbols: List[str], db_getter: Callable) -> dict:
    """SELECT-only lookup of the most recent saved research snapshot per symbol."""
    out = {}
    try:
        db = db_getter()
    except Exception:  # noqa: BLE001 - research context is optional
        return out
    now = datetime.now(timezone.utc)
    for sym in symbols:
        try:
            rows = db.list_snapshots(symbol=sym, limit=1)
        except Exception:  # noqa: BLE001
            continue
        if rows:
            r = rows[0]
            created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
            out[sym] = {"research_view": r.research_view, "bullish_pct": r.bullish_pct,
                        "neutral_pct": r.neutral_pct, "bearish_pct": r.bearish_pct,
                        "saved_at": created.date().isoformat(), "age_days": (now - created).days,
                        "age_hours": round((now - created).total_seconds() / 3600, 1)}
    return out


PORTFOLIO_KEYS = ("where_you_are_now", "going_well", "deserves_attention", "research_says", "watch_next",
                  "adding_money", "caveats")
MARKET_KEYS = ("observed", "sourced_news", "interpretation", "why_it_matters", "what_to_watch")


def _as_list(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


EXPLANATION_MAX_TOKENS = 2400   # multi-section beginner explanations; truncated JSON is withheld, never repaired


def _extract_json(text: str) -> str:
    """The outermost {...} object, tolerating stray prose or fences around it (content is never altered)."""
    text = strip_markdown_fence(text)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def _run(symbol: str, price: float, analysis_type: str, system_prompt: str, facts: dict, provider_fn: Callable,
         keys: tuple, market: bool = False) -> dict:
    facts_text = json.dumps(facts, sort_keys=True, default=str)
    digest = hashlib.sha256((analysis_type + facts_text).encode()).hexdigest()[:16]
    raw = run_gated_agent(
        symbol=symbol, price=price, attention_score=100, signal=symbol, analysis_type=f"{analysis_type}:{digest}",
        system_prompt=system_prompt, get_provider_fn=provider_fn,
        build_prompt_fn=lambda: "Facts:\n\n" + json.dumps(facts, indent=2), user_requested=True,
        max_tokens=EXPLANATION_MAX_TOKENS)
    base = {"facts": facts, "facts_digest": digest}
    if raw == UNAVAILABLE_MESSAGE or raw.startswith("AI analysis"):
        return {**base, "status": "AI_UNAVAILABLE", "message": raw, "explanation": None}
    text = raw.split("\n\n(Cached analysis")[0]
    try:
        parsed = json.loads(_extract_json(text))
        explanation = {k: _as_list(parsed.get(k)) for k in keys}
        if not any(explanation.values()):
            raise KeyError("empty")
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        # diagnostics without content: length, whether the text ends like a complete JSON object
        logger.warning("%s returned unusable JSON (%s; %d chars; ends_with_brace=%s).", analysis_type,
                       type(exc).__name__, len(text), text.rstrip().endswith(("}", "```")))
        return {**base, "status": "WITHHELD", "message": "The AI explanation was not in the expected format, so only "
                "the calculated facts are shown.", "explanation": None}
    all_text = " ".join(x for v in explanation.values() for x in v)
    exempt = " ".join(x for x in explanation.get("sourced_news", []) if re.match(r"^\s*\[\d+\]", x)) if market else ""
    failures = check_text(all_text, facts_text, market=market, causal_exempt=exempt)
    if failures:
        logger.warning("%s withheld: %s", analysis_type, ", ".join(failures))
        reason = {"forbidden_language": "used buy/sell/sizing or order language",
                  "forecast_language": "contained a forecast, probability or guarantee",
                  "magnitude_terms": "used abbreviated amounts or multiples (new arithmetic)",
                  "unsupported_causal_claims": "claimed a cause without a sourced news item",
                  "ungrounded_numbers": "contained numbers that are not in the calculated facts"}
        return {**base, "status": "WITHHELD", **failures,
                "message": "The AI explanation " + "; ".join(reason[k] for k in failures) + ", so it was withheld. The "
                           "deterministic numbers and labels above are unaffected.", "explanation": None}
    return {**base, "status": "OK", "explanation": explanation, "message": None}


def explain(view: PortfolioView, risk: dict, research: dict, scenario: Optional[dict] = None,
            provider_fn: Callable = get_provider, *, policy: Optional[dict] = None,
            reconciliation: Optional[dict] = None, check: Optional[dict] = None) -> dict:
    facts = build_facts(view, risk, research, scenario, policy, reconciliation, check)
    return _run("PORTFOLIO", float(view.snapshot.calculated_total_value or 0), "portfolio_explanation", SYSTEM_PROMPT,
                facts, provider_fn, PORTFOLIO_KEYS)


def build_market_facts(insights: dict) -> dict:
    keep = ("environment", "trend", "volatility", "risk_appetite", "breadth_label", "market_today", "difficulty",
            "difficulty_factors", "conflicting_signals", "indices", "breadth", "drivers", "strong_areas", "weak_areas",
            "what_is_happening", "what_to_watch", "for_your_portfolio", "fetched_at", "data_as_of")
    facts = {k: insights.get(k) for k in keep}
    facts["events"] = [{k: e.get(k) for k in ("title", "event_type", "date", "days_until", "why_it_matters")}
                       for e in insights.get("events", [])]
    facts["news"] = [{"number": i + 1, "headline": n.get("headline"), "source": n.get("source"),
                      "created_at": str(n.get("created_at")), "summary": (n.get("summary") or "")[:400]}
                     for i, n in enumerate(insights.get("news", []))]
    return to_jsonable(facts)


def explain_market(insights: dict, provider_fn: Callable = get_provider) -> dict:
    facts = build_market_facts(insights)
    spy = next((i for i in insights.get("indices", []) if i.get("available")), {})
    return _run("MARKET", float(spy.get("price") or 0), "market_explanation", MARKET_SYSTEM_PROMPT, facts, provider_fn,
                MARKET_KEYS, market=True)


TRADE_REVIEW_SYSTEM_PROMPT = f"""You explain a TRADE REVIEW to a beginner in plain language. It reviews the quality of a
trading DECISION PROCESS using the information available at the time. It is not a verdict on whether the trade
was right, and you are not a licensed or professional trader.

You are given a JSON object of facts computed by the application. Follow these rules exactly:

{_COMMON_RULES}
- The "process_state", area labels, supporting factors and risk factors were decided by the application's
  deterministic rules. Report them exactly; never change, add or remove them; never give a score or grade.
- Keep DECISION PROCESS separate from TRADE OUTCOME. Outcomes (returns, MFE, MAE, current P&L) are HINDSIGHT:
  never use them to call the decision good or bad. Say that a good process can still lose money and a poor
  process can still make money.
- Anything marked unavailable / not saved must be described as unavailable. Never guess historical facts.
- Never say "you should have bought/sold" or "you shouldn't have". Do not diagnose the person's psychology.

Respond with ONLY a JSON object with exactly these keys:
  "process_summary": 1-2 sentences restating the process state and why,
  "done_well": array of 0-3 short statements,
  "increased_risk": array of 0-3 short statements,
  "conflicting_evidence": array of 0-2 short statements,
  "what_to_learn": array of 1-2 short statements,
  "worth_monitoring": array of 1-3 short statements,
  "outcome_hindsight": array of 0-2 short statements that clearly label outcomes as hindsight.
No prose before or after the JSON."""
TRADE_REVIEW_KEYS = ("process_summary", "done_well", "increased_risk", "conflicting_evidence", "what_to_learn",
                     "worth_monitoring", "outcome_hindsight")


def build_trade_review_facts(review: dict) -> dict:
    def area_labels(r):
        return {k: {"label": v.get("label"), "status": v.get("status")} for k, v in r["areas"].items()}
    facts = {"mode": review["mode"], "symbol": review["symbol"], "lessons": review.get("lessons")}
    if review["mode"] == "BEFORE":
        r = review["review"]
        facts.update({"process_state": r["process_state"], "areas": area_labels(r),
                      "supporting": [f["text"] for f in r["supporting"]], "risks": [f["text"] for f in r["risks"]],
                      "watch": r["watch"], "plan": review.get("plan"),
                      "portfolio": {k: review["context"]["portfolio"].get(k) for k in (
                          "position_weight_before_pct", "position_weight_after_pct", "sector",
                          "sector_weight_before_pct", "sector_weight_after_pct")},
                      "derived": {"position_weight_change_pp": _diff(
                          review["context"]["portfolio"].get("position_weight_after_pct"),
                          review["context"]["portfolio"].get("position_weight_before_pct")),
                          "sector_weight_change_pp": _diff(
                          review["context"]["portfolio"].get("sector_weight_after_pct"),
                          review["context"]["portfolio"].get("sector_weight_before_pct"))}})
    else:
        facts.update({"position_now": review["position"], "changes_since_entry": review["changes_since_entry"],
                      "first_entry_date": review.get("first_entry_date"),
                      "entries": [{"open_date": l.get("open_date"), "entry_price": l.get("entry_price"),
                                   "process_state": l["review"]["process_state"], "areas": area_labels(l["review"]),
                                   "supporting": [f["text"] for f in l["review"]["supporting"]],
                                   "risks": [f["text"] for f in l["review"]["risks"]],
                                   "hindsight_outcomes": l.get("outcomes")}
                                  for l in review["lots"][-5:]]})
    return to_jsonable(facts)


def explain_trade_review(review: dict, provider_fn: Callable = get_provider) -> dict:
    facts = build_trade_review_facts(review)
    return _run(f"REVIEW-{review['symbol']}", 0.0, "trade_review_explanation", TRADE_REVIEW_SYSTEM_PROMPT, facts,
                provider_fn, TRADE_REVIEW_KEYS)
