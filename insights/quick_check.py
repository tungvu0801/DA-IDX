"""
insights/quick_check.py — QUICK TRADE CHECK (Stage 2.7F): a short, beginner summary of the existing
add-money check (insights/stock_check.py). Presentation only — no new scoring, no forecasts.

Inputs the beginner gives: stock, amount, timeframe, optional reason chip / note. Everything else is
computed by the existing deterministic systems. The timeframe changes only which (already computed)
facts are shown first; the reason chip only adds a teaching note comparing the stated reason to the facts.
"""
from __future__ import annotations

from typing import Any, List, Optional

from insights.labels import BULLISH_VIEWS, BEARISH_VIEWS
from insights.plain import (TIMEFRAME_FOCUS, TIMEFRAME_LABELS, TIMEFRAMES, order_by_timeframe, plain_factor,
                            plain_watch, research_freshness, watch_topic)

REASON_CHIPS = {"pullback": "Pullback", "breakout": "Breakout", "news": "News", "earnings": "Earnings",
                "long_term": "Long-term idea", "momentum": "Momentum", "researching": "Just researching"}
LOCATION_TEXT = {"NEAR_SUPPORT": "Near support", "MIDDLE_OF_RANGE": "Mid-range", "NEAR_RESISTANCE": "Near resistance",
                 "NO_RESISTANCE_ABOVE": "Above recent resistance", "NO_SUPPORT_BELOW": "Below recent support",
                 "UNAVAILABLE": "Unavailable"}
CHASING_TEXT = ("CHASING RISK — This stock has already moved quickly. Consider what confirmation or pullback condition "
                "would make the entry easier to evaluate.")
GOOD, MID, BAD, NEUTRAL = "good", "mid", "bad", "neutral"
# Codes that describe the same fact for a beginner; only the first (most specific) is shown.
SAME_FACT = [("concentration_high", "portfolio_high_exposure", "addition_worsens_concentration"),
             ("extended_near_resistance", "extended", "near_resistance"), ("no_high_event", "no_near_event"),
             ("trend_improving", "uptrend"), ("research_bullish", "bullish_exceeds_bearish")]


def dedupe(items: List[dict]) -> List[dict]:
    seen, out = set(), []
    for f in items:
        group = next((g for g in SAME_FACT if f["code"] in g), (f["code"],))
        if not seen.intersection(group):
            out.append(f)
        seen.add(f["code"])
    return out


def _tile(title: str, value: str, level: str) -> dict:
    return {"title": title, "value": value, "level": level}


def tiles(check: dict) -> List[dict]:
    L, T, P = check["layers"], check["stock_trend"], check["layers"]["portfolio"]
    env = L["market"]["environment"]
    market = _tile("Market", {"SUPPORTIVE": "Supportive", "MIXED": "Mixed", "CAUTIOUS": "Cautious"}.get(env, "Unavailable"),
                   {"SUPPORTIVE": GOOD, "MIXED": MID, "CAUTIOUS": BAD}.get(env, NEUTRAL))
    trend = _tile(f"{check['symbol']} trend", {"UPTREND": "Uptrend", "MIXED": "Mixed", "DOWNTREND": "Downtrend"}.get(
        T["trend"], "Unavailable"), {"UPTREND": GOOD, "MIXED": MID, "DOWNTREND": BAD}.get(T["trend"], NEUTRAL))
    extended = any(f["code"] in ("extended", "extended_near_resistance") for f in check["timing"]["patience"])
    loc = check["price_area"]["location"]
    if extended:
        entry = _tile("Entry", "Extended (after a fast rise)", BAD)
    else:
        entry = _tile("Entry", LOCATION_TEXT.get(loc, loc), {"NEAR_SUPPORT": GOOD, "MIDDLE_OF_RANGE": MID,
                                                             "NEAR_RESISTANCE": MID, "NO_RESISTANCE_ABOVE": MID,
                                                             "NO_SUPPORT_BELOW": BAD}.get(loc, NEUTRAL))
    mom = _tile("Momentum", {"STRONG": "Positive", "NORMAL": "Normal", "WEAK": "Weak"}.get(T["momentum"], "Unavailable"),
                {"STRONG": GOOD, "NORMAL": MID, "WEAK": BAD}.get(T["momentum"], NEUTRAL))
    ev = check["events"]["event_risk"]
    event = _tile("Event risk", {"LOW": "Low", "NONE": "Low", "MEDIUM": "Medium", "HIGH": "High"}.get(ev, "Unavailable"),
                  {"LOW": GOOD, "NONE": GOOD, "MEDIUM": MID, "HIGH": BAD}.get(ev, NEUTRAL))
    setup = P["setup"]
    sector = P["sector"]
    if not check["feasibility"]["feasible"]:
        port = _tile("Your portfolio", "Not enough cash for this amount", BAD)
    elif setup in ("HIGH CONCENTRATION WOULD INCREASE", "ALREADY CONCENTRATED"):
        port = _tile("Your portfolio", f"Already high {sector.lower() if sector != 'UNCLASSIFIED' else check['symbol']} "
                                       "exposure", BAD)
    else:
        port = _tile("Your portfolio", "No concentration concern", GOOD)
    return [market, trend, entry, mom, event, port]


def ladder(check: dict) -> dict:
    pa = check["price_area"]
    s, p, r = pa["support"], pa["current_price"], pa["resistance"]
    pos = None
    if p is not None and s is not None and r is not None and r > s:
        pos = max(0.0, min(1.0, float((p - s) / (r - s))))
    elif p is not None and r is None and s is not None:
        pos = 1.0
    elif p is not None and s is None and r is not None:
        pos = 0.0
    return {"support": s, "price": p, "resistance": r, "position": None if pos is None else round(pos, 3),
            "location": LOCATION_TEXT.get(pa["location"], pa["location"]), "levels_as_of": pa.get("levels_as_of")}


def reason_feedback(reason: Optional[str], check: dict, catalysts: List[dict]) -> Optional[str]:
    if not reason:
        return None
    loc, T = check["price_area"]["location"], check["stock_trend"]
    extended = any(f["code"] == "extended" for f in check["timing"]["patience"])
    name = REASON_CHIPS.get(reason, reason)
    if reason == "pullback":
        if loc == "NEAR_SUPPORT" and not extended:
            return "You selected Pullback, and the current price is near the calculated support area."
        return "You selected Pullback, but the current price is not near the calculated support area."
    if reason == "breakout":
        if loc == "NO_RESISTANCE_ABOVE" and T["volume"] == "STRONG":
            return "You selected Breakout: price is above recent resistance and volume is strong."
        if loc in ("NEAR_RESISTANCE", "NO_RESISTANCE_ABOVE"):
            return (f"You selected Breakout: price is {LOCATION_TEXT[loc].lower()}, but volume is {T['volume'].lower()}; "
                    "breakouts are usually judged with stronger volume.")
        return "You selected Breakout, but price is not near or above the calculated resistance area."
    if reason == "momentum":
        return {"STRONG": "You selected Momentum, and momentum is currently positive.",
                "WEAK": "You selected Momentum, but momentum is currently weak."}.get(
            T["momentum"], "You selected Momentum; momentum is currently normal, not strong.")
    if reason == "news":
        if catalysts:
            return f"You selected News: the research found {len(catalysts)} recent verified headline(s) (see details)."
        return "You selected News, but no verified catalyst was found in the current research."
    if reason == "earnings":
        note = check["events"]["earnings_note"]
        return f"You selected Earnings. {note}" if note else "You selected Earnings; check the event list for the date."
    if reason == "long_term":
        return ("You selected Long-term idea: for this, how much of your account it becomes and your sector exposure "
                "matter more than today's price move.")
    return f"You selected {name}."


def compare_snapshot(check: dict) -> dict:
    return {"price": None if check["price_area"]["current_price"] is None else str(check["price_area"]["current_price"]),
            "market": check["layers"]["market"]["environment"], "trend": check["stock_trend"]["trend"],
            "price_location": check["price_area"]["location"], "momentum": check["stock_trend"]["momentum"],
            "volume": check["stock_trend"]["volume"], "timing": check["timing"]["status"],
            "research_view": check["layers"]["stock"]["research"]["research_view"],
            "event_risk": check["events"]["event_risk"],
            "weight_after": str(check["layers"]["portfolio"]["position_weight_after_pct"])}


LABELS = {"price": "Price", "market": "Market", "trend": "Trend", "price_location": "Price location",
          "momentum": "Momentum", "volume": "Volume", "timing": "Timing", "research_view": "Research view",
          "event_risk": "Event risk", "weight_after": "Weight after adding"}


def changes_since(previous: Optional[dict], current: dict) -> List[dict]:
    if not previous:
        return []
    out = []
    for k, label in LABELS.items():
        before, after = previous.get(k), current.get(k)
        if before != after and not (before is None and after is None):
            if k == "price_location":
                before, after = LOCATION_TEXT.get(before, before), LOCATION_TEXT.get(after, after)
            out.append({"field": label, "before": before, "after": after})
    return out


def build_quick_check(check: dict, *, timeframe: str = "days", reason: Optional[str] = None, note: Optional[str] = None,
                      catalysts: Optional[List[dict]] = None, previous: Optional[dict] = None,
                      checked_at: Optional[str] = None) -> dict:
    timeframe = timeframe if timeframe in TIMEFRAMES else "days"
    catalysts = catalysts or []
    P = check["layers"]["portfolio"]
    ctx = {"symbol": check["symbol"], "sector": P["sector"], "support": check["price_area"]["support"],
           "resistance": check["price_area"]["resistance"], "sector_before": P["sector_weight_before_pct"],
           "sector_after": P["sector_weight_after_pct"], "weight_before": P["position_weight_before_pct"],
           "weight_after": P["position_weight_after_pct"]}
    supports = dedupe(check["fit"]["supporting"] + check["timing"]["supporting"])
    cautions = dedupe(check["fit"]["caution"] + check["timing"]["patience"])
    env, trend = check["layers"]["market"]["environment"], check["stock_trend"]["trend"]
    if trend == "UPTREND" and env == "CAUTIOUS":
        supports.append({"code": "stronger_than_market", "text": ""})
    if trend == "DOWNTREND" and env == "SUPPORTIVE":
        cautions.append({"code": "weaker_than_market", "text": ""})
    good = [plain_factor(f, ctx) for f in order_by_timeframe(supports, timeframe)]
    careful = [plain_factor(f, ctx) for f in order_by_timeframe(cautions, timeframe)]
    prio = TIMEFRAMES[timeframe]
    watch = [plain_watch(w) for w in sorted([w for w in check["timing"]["watch_for"] if watch_topic(w) in prio],
                                            key=lambda w: prio.index(watch_topic(w)))[:3]]
    r = check["layers"]["stock"]["research"]
    rf = research_freshness(r.get("age_hours")) if r.get("available") else "MISSING"
    extended = any(f["code"] in ("extended", "extended_near_resistance") for f in check["timing"]["patience"])
    snap = compare_snapshot(check)
    return {
        "title": f"{check['symbol']} — ${float(check['amount_usd']):,.0f} TRADE CHECK",
        "symbol": check["symbol"], "amount_usd": check["amount_usd"], "timeframe": timeframe,
        "timeframe_label": TIMEFRAME_LABELS[timeframe], "timeframe_focus": TIMEFRAME_FOCUS[timeframe],
        "checked_at": checked_at, "current_conditions": check["timing"]["status"],
        "tiles": tiles(check), "what_looks_good": good, "what_makes_me_cautious": careful, "what_i_would_watch": watch,
        "ladder": ladder(check),
        "research": {"view": r.get("research_view"), "freshness": rf, "age_hours": r.get("age_hours"),
                     "bullish_pct": r.get("bullish_pct"), "neutral_pct": r.get("neutral_pct"),
                     "bearish_pct": r.get("bearish_pct"), "source": r.get("source"),
                     "needs_refresh": rf in ("AGING", "STALE", "MISSING")},
        "catalysts": catalysts[:3],
        "reason": reason, "reason_label": REASON_CHIPS.get(reason) if reason else None, "note": note,
        "reason_feedback": reason_feedback(reason, check, catalysts),
        "chasing_risk": CHASING_TEXT if extended else None,
        "compare_snapshot": snap, "changes_since_last_check": changes_since(previous, snap),
        "portfolio_line": (f"Adding ${float(check['amount_usd']):,.0f} changes {check['symbol']} from "
                           f"{P['position_weight_before_pct']}% → {P['position_weight_after_pct']}% of your portfolio"
                           + (f"; {P['sector']} {P['sector_weight_before_pct']}% → {P['sector_weight_after_pct']}%."
                              if P["sector"] != "UNCLASSIFIED" else ".")),
        "details": check,
        "disclaimer": "A description of current conditions, not a buy/sell instruction or a prediction. No order is created.",
    }


def group_candidate(check: dict) -> str:
    blocking = set(check["fit"]["blocking"]) - {"research_unavailable", "insufficient_cash"}
    if check["timing"]["status"] == "HIGHER-RISK CONDITIONS" or blocking:
        return "HIGHER-RISK CONDITIONS"
    if check["timing"]["status"] == "MORE SUPPORTIVE CONDITIONS" or check["fit"]["state"] == "FAVORABLE CONDITIONS":
        return "CONDITIONS WORTH RESEARCHING"
    return "MIXED — NEED MORE CONFIRMATION"


def candidate_summary(check: dict, owned: bool, research_freshness_label: str) -> dict:
    P, T = check["layers"]["portfolio"], check["stock_trend"]
    ctx = {"symbol": check["symbol"], "sector": P["sector"], "support": check["price_area"]["support"],
           "resistance": check["price_area"]["resistance"], "sector_before": P["sector_weight_before_pct"],
           "sector_after": P["sector_weight_after_pct"], "weight_before": P["position_weight_before_pct"],
           "weight_after": P["position_weight_after_pct"]}
    extended = any(f["code"] in ("extended", "extended_near_resistance") for f in check["timing"]["patience"])
    sup = [plain_factor(f, ctx) for f in dedupe(check["fit"]["supporting"] + check["timing"]["supporting"])]
    pat = [plain_factor(f, ctx) for f in dedupe(check["fit"]["caution"] + check["timing"]["patience"])]
    return {
        "symbol": check["symbol"], "owned": owned, "group": group_candidate(check),
        "research_view": check["layers"]["stock"]["research"]["research_view"] or "RESEARCH NEEDED",
        "research_freshness": research_freshness_label,
        "research_needed": not check["layers"]["stock"]["research"]["available"],
        "trend": T["trend"], "price_location": "EXTENDED" if extended else check["price_area"]["location"],
        "market": check["layers"]["market"]["environment"], "sector": P["sector"],
        "sector_today": check["layers"]["market"]["sector_today"], "event_risk": check["events"]["event_risk"],
        "weight_before": P["position_weight_before_pct"], "weight_after": P["position_weight_after_pct"],
        "sector_before": P["sector_weight_before_pct"], "sector_after": P["sector_weight_after_pct"],
        "supports": list(dict.fromkeys(sup))[:3], "patience": list(dict.fromkeys(pat))[:3],
        "timing": check["timing"]["status"],
    }
