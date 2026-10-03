"""
analysis/event_risk.py — Deterministic event proximity + severity, and the
risk flags they translate into. Pure Python, no I/O, no Claude.

Given events already fetched by data/events/*.py (real dates from real
sources, or simply absent if unavailable), this module ONLY does
arithmetic and threshold comparisons -- exactly like analysis/risk_flags.py
does for technical risk. Claude explains what these functions decide; it
never assigns a severity or window itself.
"""
from dataclasses import dataclass
from datetime import datetime, time, timezone
from enum import Enum
from typing import List, Optional

import config
from analysis.risk_flags import RiskFlag
from data.events.models import EventImportance, EventType, NormalizedEvent, TimePrecision


class EventWindow(str, Enum):
    NOW = "NOW"
    WITHIN_24H = "WITHIN_24H"
    WITHIN_3D = "WITHIN_3D"
    WITHIN_7D = "WITHIN_7D"
    LATER = "LATER"
    PAST = "PAST"


class EventSeverity(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


@dataclass
class EventProximity:
    event: NormalizedEvent
    days_until: float
    hours_until: float
    event_window: EventWindow
    severity: EventSeverity


def _event_datetime_utc(event: NormalizedEvent) -> datetime:
    """The instant used for proximity math. Exact when the source gave a
    real time; otherwise UTC midnight of the known date -- never a
    synthesized time-of-day."""
    if event.time_precision == TimePrecision.EXACT and event.event_datetime_utc is not None:
        return event.event_datetime_utc
    return datetime.combine(event.event_date, time.min, tzinfo=timezone.utc)


def compute_event_proximity(now_utc: datetime, event: NormalizedEvent) -> EventProximity:
    """
    Always compares against a caller-supplied UTC `now_utc`, so results are
    identical regardless of the server's local timezone (never
    datetime.now() with an implicit local zone).
    """
    delta = _event_datetime_utc(event) - now_utc
    hours_until = delta.total_seconds() / 3600.0
    days_until = hours_until / 24.0

    if hours_until < 0:
        window = EventWindow.PAST
    elif hours_until <= 1:
        window = EventWindow.NOW
    elif hours_until <= config.EVENT_HIGH_RISK_HOURS:
        window = EventWindow.WITHIN_24H
    elif days_until <= config.EVENT_MEDIUM_RISK_DAYS:
        window = EventWindow.WITHIN_3D
    elif days_until <= config.EVENT_LOW_RISK_DAYS:
        window = EventWindow.WITHIN_7D
    else:
        window = EventWindow.LATER

    severity = classify_event_severity(event, window)
    return EventProximity(event=event, days_until=days_until, hours_until=hours_until, event_window=window, severity=severity)


def classify_event_severity(event: NormalizedEvent, window: EventWindow) -> EventSeverity:
    """
    Deterministic severity, driven entirely by config thresholds and the
    event's own `importance` (set by its provider from real source
    context, e.g. FOMC/CPI/jobs = HIGH, a routine dividend = LOW).
    """
    if window == EventWindow.PAST or window == EventWindow.LATER:
        return EventSeverity.NONE

    if event.importance == EventImportance.HIGH:
        if window in (EventWindow.NOW, EventWindow.WITHIN_24H):
            return EventSeverity.HIGH
        if window == EventWindow.WITHIN_3D:
            return EventSeverity.MEDIUM
        return EventSeverity.LOW  # WITHIN_7D

    if event.importance == EventImportance.MEDIUM:
        if window in (EventWindow.NOW, EventWindow.WITHIN_24H, EventWindow.WITHIN_3D):
            return EventSeverity.MEDIUM
        return EventSeverity.LOW

    # LOW-importance events never escalate past LOW, regardless of proximity.
    return EventSeverity.LOW


def overall_event_risk_level(proximities: List[EventProximity]) -> EventSeverity:
    """The single worst (highest) severity across all nearby events -- the headline 'Event Risk: X' badge."""
    order = [EventSeverity.HIGH, EventSeverity.MEDIUM, EventSeverity.LOW, EventSeverity.NONE]
    best = EventSeverity.NONE
    for p in proximities:
        if order.index(p.severity) < order.index(best):
            best = p.severity
    return best


_FLAG_CODE_BY_TYPE_GROUP = {
    "macro": "MACRO_EVENT_IMMINENT",
    "earnings": "EARNINGS_EVENT_IMMINENT",
    "corporate": "CORPORATE_ACTION_IMMINENT",
}

_MACRO_TYPES = {
    EventType.CPI, EventType.PPI, EventType.EMPLOYMENT_SITUATION, EventType.JOLTS, EventType.PCE,
    EventType.FOMC_MEETING, EventType.FOMC_DECISION, EventType.FOMC_MINUTES, EventType.OTHER_MAJOR_MACRO,
}
_CORPORATE_TYPES = {
    EventType.DIVIDEND, EventType.STOCK_SPLIT, EventType.REVERSE_SPLIT,
    EventType.MERGER, EventType.SPINOFF, EventType.OTHER_CORPORATE_ACTION,
}


def _type_group(event_type: EventType) -> str:
    if event_type == EventType.EARNINGS:
        return "earnings"
    if event_type in _MACRO_TYPES:
        return "macro"
    return "corporate"


def build_event_risk_flags(proximities: List[EventProximity]) -> List[RiskFlag]:
    """
    Turns proximity+severity results into RiskFlag entries using the exact
    same RiskFlag shape/severity vocabulary analysis/risk_flags.py already
    uses, so risk_category_score() needs zero changes to reflect these.
    Only ever emits a flag when backed by a real event with real proximity.

    Missing earnings-calendar coverage is deliberately NEVER represented
    here: "we cannot evaluate this risk" is not the same claim as "this is
    additional risk evidence," and scoring it would silently penalize the
    Risk category for every symbol merely because no earnings provider is
    configured. That caveat is surfaced separately, as plain data-quality
    context (EventsBundle.earnings_available/earnings_reason and the
    data_caveats passed to the Risk Agent) -- never as a scored flag here.
    """
    flags: List[RiskFlag] = []

    overall = overall_event_risk_level(proximities)
    if overall == EventSeverity.HIGH:
        flags.append(RiskFlag("EVENT_RISK_HIGH", "HIGH", "A high-importance event is scheduled within 24 hours."))
    elif overall == EventSeverity.MEDIUM:
        flags.append(RiskFlag("EVENT_RISK_MEDIUM", "MEDIUM", "An important event is scheduled in the next few days."))

    seen_groups = set()
    for p in proximities:
        if p.severity in (EventSeverity.NONE,):
            continue
        group = _type_group(p.event.event_type)
        if group in seen_groups:
            continue
        code = _FLAG_CODE_BY_TYPE_GROUP[group]
        severity = "HIGH" if p.severity == EventSeverity.HIGH else ("MEDIUM" if p.severity == EventSeverity.MEDIUM else "LOW")
        flags.append(RiskFlag(code, severity, f"{p.event.title} is scheduled on {p.event.event_date.isoformat()}."))
        seen_groups.add(group)

    return flags
