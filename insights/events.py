"""
insights/events.py — Beginner presentation of Stage 2.6 verified events (read-only).

Only events returned by the Stage 2.6 providers are shown, with their verified dates.
Nothing predicts an event's outcome or the market's reaction.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from analysis.event_risk import compute_event_proximity

EARNINGS_UNAVAILABLE = "Earnings date unavailable from the current verified source."
EVENT_TIMING_TEXT = ("Prices can move sharply around major events. Waiting until after an event reduces event "
                     "uncertainty, but the price may also move before or immediately after it.")

WHY_IT_MATTERS = {
    "EMPLOYMENT_SITUATION": "Jobs data can affect expectations for interest rates, which can influence the broader "
                            "stock market.",
    "CPI": "Inflation data can affect expectations for interest rates, which can influence stock prices.",
    "PPI": "Producer-price data is an early inflation signal and can affect interest-rate expectations.",
    "PCE": "PCE is the Federal Reserve's preferred inflation measure and can affect interest-rate expectations.",
    "JOLTS": "Job-openings data adds detail on the labor market, which can affect interest-rate expectations.",
    "FOMC_MEETING": "Federal Reserve meetings can change interest-rate policy, which affects the whole market.",
    "FOMC_DECISION": "The Federal Reserve's interest-rate decision can move the whole market.",
    "FOMC_MINUTES": "Meeting minutes can change expectations about future interest-rate decisions.",
    "EARNINGS": "Company earnings reports often cause large moves in that stock.",
    "DIVIDEND": "A dividend date changes the share price mechanically on the ex-dividend date.",
    "STOCK_SPLIT": "A stock split changes the share count and price, not the value of your holding.",
    "REVERSE_SPLIT": "A reverse split changes the share count and price, not the value of your holding.",
}
MACRO_TYPES = {"CPI", "PPI", "EMPLOYMENT_SITUATION", "JOLTS", "PCE", "FOMC_MEETING", "FOMC_DECISION",
               "FOMC_MINUTES", "OTHER_MAJOR_MACRO"}


def event_item(ev: Any, now: datetime) -> dict:
    prox = compute_event_proximity(now, ev)
    etype = getattr(ev.event_type, "value", str(ev.event_type))
    return {
        "title": ev.title,
        "event_type": etype,
        "macro": etype in MACRO_TYPES,
        "date": ev.event_date.isoformat() if ev.event_date else None,
        "time_precision": getattr(ev.time_precision, "value", None),
        "days_until": round(prox.days_until, 1),
        "hours_until": round(prox.hours_until, 1),
        "window": prox.event_window.value,
        "severity": prox.severity.value,
        "importance": getattr(ev.importance, "value", None),
        "source": ev.source,
        "source_url": ev.source_url,
        "confirmed": ev.confirmed,
        "why_it_matters": WHY_IT_MATTERS.get(etype, "Major scheduled events can increase price movement."),
    }


def upcoming_events(bundle: Any, now: datetime, macro_only: bool = False, limit: int = 8) -> List[dict]:
    if bundle is None:
        return []
    events = list(getattr(bundle, "macro_events", []) or [])
    if not macro_only:
        events = list(getattr(bundle, "company_events", []) or []) + events
    items = [event_item(e, now) for e in events]
    items = [i for i in items if i["window"] != "PAST"]
    items.sort(key=lambda i: i["hours_until"])
    seen, out = set(), []
    for i in items:
        key = (i["event_type"], i["date"])
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out[:limit]


def earnings_note(bundle: Any) -> Optional[str]:
    if bundle is None:
        return EARNINGS_UNAVAILABLE
    return None if getattr(bundle, "earnings_available", False) else EARNINGS_UNAVAILABLE
