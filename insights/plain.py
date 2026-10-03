"""
insights/plain.py — Beginner wording + freshness labels (Stage 2.7F). Presentation only.

Maps deterministic factor CODES (produced by insights/stock_check.py) to short, plain sentences, and
tags each with a TOPIC so the quick trade check can change emphasis by timeframe. Nothing here decides
a state or changes a number; every number comes from the check it describes.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional

import config

FRESH, AGING, STALE, UNKNOWN = "FRESH", "AGING", "STALE", "UNKNOWN"


def freshness(age_seconds: Optional[float], fresh_s: float, stale_s: float) -> str:
    if age_seconds is None:
        return UNKNOWN
    if age_seconds <= fresh_s:
        return FRESH
    if age_seconds <= stale_s:
        return AGING
    return STALE


def age_seconds(ts: Optional[str], now: datetime) -> Optional[float]:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (now - t).total_seconds())


def market_freshness(fetched_at: Optional[str], now: datetime) -> dict:
    a = age_seconds(fetched_at, now)
    return {"label": freshness(a, config.FRESHNESS_MARKET_FRESH_MINUTES * 60, config.MARKET_FEEDBACK_STALE_MINUTES * 60),
            "updated": fetched_at, "age_seconds": None if a is None else int(a)}


def portfolio_freshness(fetched_at: Optional[str], now: datetime) -> dict:
    a = age_seconds(fetched_at, now)
    return {"label": freshness(a, config.FRESHNESS_PORTFOLIO_FRESH_SECONDS, config.FRESHNESS_PORTFOLIO_STALE_SECONDS),
            "updated": fetched_at, "age_seconds": None if a is None else int(a)}


def research_freshness(age_hours: Optional[float]) -> str:
    if age_hours is None:
        return UNKNOWN
    return freshness(age_hours * 3600, config.FRESHNESS_RESEARCH_FRESH_HOURS * 3600,
                     config.BEGINNER_RESEARCH_STALE_HOURS * 3600)


def quote_freshness(age_s: Optional[float]) -> str:
    return freshness(age_s, config.FRESHNESS_QUOTE_FRESH_SECONDS, config.PORTFOLIO_QUOTE_STALE_SECONDS)


TOPICS = {
    "near_support": "price", "near_resistance": "price", "extended": "price", "extended_near_resistance": "price",
    "volume_confirms": "volume",
    "momentum_positive": "momentum", "momentum_weakening": "momentum", "momentum_weak": "momentum",
    "trend_improving": "trend", "uptrend": "trend", "downtrend": "trend", "downtrend_in_cautious_market": "trend",
    "stronger_than_market": "trend", "weaker_than_market": "trend",
    "no_high_event": "events", "no_near_event": "events", "event_approaching": "events", "high_event_risk": "events",
    "catalyst_supportive": "catalysts",
    "market_supportive": "market", "market_weak": "market",
    "sector_supportive": "sector", "sector_weak": "sector", "sector_unclassified": "sector",
    "research_bullish": "research", "bullish_exceeds_bearish": "research", "research_mixed": "research",
    "research_bearish": "research", "bearish_material": "research", "research_unavailable": "research",
    "technical_supportive": "research",
    "concentration_high": "portfolio", "portfolio_high_exposure": "portfolio",
    "addition_worsens_concentration": "portfolio", "no_new_high_concentration": "portfolio",
    "insufficient_cash": "portfolio",
    "data_fresh": "data", "stale_data": "data", "quote_stale": "data", "research_stale": "data",
    "quote_unreliable": "data",
}

# Presentation priority only (Stage 2 evidence is unchanged). "long_term" drops intraday topics from the top lists.
TIMEFRAMES = {
    "today": ["price", "volume", "momentum", "events", "market", "trend", "research", "sector", "portfolio", "data",
              "catalysts"],
    "days": ["trend", "price", "catalysts", "events", "market", "momentum", "research", "sector", "portfolio", "volume",
             "data"],
    "weeks": ["trend", "sector", "research", "events", "portfolio", "market", "catalysts", "price", "momentum",
              "volume", "data"],
    "long_term": ["portfolio", "sector", "research", "trend", "events", "market", "catalysts", "data"],
}
TIMEFRAME_LABELS = {"today": "Today", "days": "Few days", "weeks": "Few weeks", "long_term": "Long term"}
TIMEFRAME_FOCUS = {
    "today": "For a same-day idea, price movement, volume, momentum, nearby levels and events matter most.",
    "days": "For a few days, trend, support/resistance, catalysts, events and the market matter most.",
    "weeks": "For a few weeks, the broader trend, sector, research view, events and your portfolio matter most.",
    "long_term": "For a long-term idea, portfolio concentration, sector exposure and the broader research matter "
                 "most; day-to-day entry noise matters less.",
}


def _money(v) -> str:
    return "N/A" if v is None else f"${float(v):,.2f}"


def plain_factor(f: dict, ctx: dict) -> str:
    """Short beginner sentence for one factor. Falls back to the factor's own deterministic text."""
    sym, sector = ctx.get("symbol", "This stock"), ctx.get("sector") or "this sector"
    sector_txt = "an unclassified sector" if sector == "UNCLASSIFIED" else sector
    code = f.get("code")
    table = {
        "research_bullish": f"{sym} research currently leans bullish.",
        "bullish_exceeds_bearish": "Bullish evidence outweighs bearish evidence.",
        "technical_supportive": "The chart evidence in the research is positive.",
        "catalyst_supportive": "Recent news in the research leans positive.",
        "research_mixed": "The research is mixed right now.",
        "research_bearish": "The research currently leans bearish.",
        "bearish_material": "There is meaningful bearish evidence.",
        "research_unavailable": f"There is no research for {sym} yet.",
        "research_stale": "The research is getting old — refresh it before relying on it.",
        "trend_improving": "The trend is up.",
        "uptrend": "The trend is up.",
        "downtrend": "The trend is down.",
        "momentum_positive": "Momentum is positive.",
        "momentum_weakening": "Momentum is fading.",
        "momentum_weak": "Momentum is weak.",
        "volume_confirms": "Trading volume supports the move.",
        "near_support": f"Price is near support ({_money(ctx.get('support'))}).",
        "near_resistance": f"Price is close to resistance ({_money(ctx.get('resistance'))}).",
        "extended": "The stock has already moved up quickly.",
        "extended_near_resistance": "It has moved up quickly and is near resistance.",
        "market_supportive": "The overall market backdrop is supportive.",
        "market_weak": "The overall market backdrop is weak.",
        "sector_supportive": f"{sector_txt} stocks are doing well this session.",
        "sector_weak": f"{sector_txt} stocks are weak this session.",
        "sector_unclassified": "We can't verify this stock's sector, so its sector effect is unknown.",
        "no_high_event": "No major event risk right now.",
        "no_near_event": "No major event risk right now.",
        "high_event_risk": "A major scheduled event is very close.",
        "no_new_high_concentration": "This amount doesn't make your account too concentrated.",
        "portfolio_high_exposure": f"Your {sector_txt} exposure is already high.",
        "addition_worsens_concentration": f"Adding would increase your {sector_txt} exposure further.",
        "insufficient_cash": "You don't have enough cash for this amount — use new money instead.",
        "data_fresh": "Price and research data are fresh.",
        "quote_stale": "The price quote is getting old — refresh it.",
        "stale_data": "Some data is getting old — refresh before relying on it.",
        "quote_unreliable": "The price quote is unreliable right now.",
        "stronger_than_market": "The stock looks stronger than the overall market right now.",
        "weaker_than_market": "The stock looks weaker than the overall market right now.",
    }
    if code == "concentration_high":
        if ctx.get("sector_before") is not None and ctx.get("sector_after") is not None and sector != "UNCLASSIFIED":
            return (f"Your {sector_txt} exposure is already high and would increase from {ctx['sector_before']}% to "
                    f"{ctx['sector_after']}%.")
        return (f"{sym} is already a fairly large part of your account ({ctx.get('weight_before')}% → "
                f"{ctx.get('weight_after')}%).")
    if code == "event_approaching":
        return f.get("text", "").replace("An important event is approaching: ", "").rstrip(".") + " is coming up."
    return table.get(code, f.get("text", ""))


def order_by_timeframe(items: Iterable[dict], timeframe: str, limit: int = 3) -> List[dict]:
    prio = TIMEFRAMES.get(timeframe, TIMEFRAMES["days"])
    usable = [i for i in items if TOPICS.get(i.get("code"), "data") in prio]
    return sorted(usable, key=lambda i: prio.index(TOPICS.get(i.get("code"), "data")))[:limit]


_WATCH_PLAIN = [
    (re.compile(r"^Momentum improves \(.*\)\.$"), "Momentum turns positive."),
    (re.compile(r"^Volume strengthens \(.*\)\.$"), "More shares trade on up days (stronger volume)."),
    (re.compile(r"with volume above [\d.]+x\.$"), "with strong trading volume."),
    (re.compile(r"^The Research View improves \(currently (.*)\)\.$"),
     lambda m: f"The research view improves (now {m.group(1).title()})."),
    (re.compile(r"^Your portfolio concentration in (.*) decreases\.$"), r"Your \1 exposure gets smaller."),
]


def plain_watch(text: str) -> str:
    """Same watch condition in beginner words (thresholds stay in the technical details)."""
    for pattern, repl in _WATCH_PLAIN:
        text = pattern.sub(repl, text)
    return text


def watch_topic(text: str) -> str:
    t = text.lower()
    for key, topic in (("support", "price"), ("resistance", "price"), ("momentum", "momentum"), ("volume", "volume"),
                       ("broad market", "market"), ("sector", "sector"), ("passes", "events"), ("research", "research"),
                       ("concentration", "portfolio"), ("pulls back", "price")):
        if key in t:
            return topic
    return "data"
