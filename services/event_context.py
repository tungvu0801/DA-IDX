"""
services/event_context.py — Orchestrates the REAL event providers
(data/events/*.py) plus the deterministic proximity/severity engine
(analysis/event_risk.py) into one EventsBundle per symbol.

This is Stage 2.6's single new integration point: it fetches real events,
computes proximity/severity/risk-flags in pure Python, and returns a
bundle for agents/beginner_agent.py to attach to the research bundle and
for services/snapshot_builder.py to freeze at save time. It never calls
Claude and never invents a date -- every event either came from a real,
named source or simply isn't in the list.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import config
from analysis.event_risk import (
    EventProximity,
    build_event_risk_flags,
    compute_event_proximity,
    overall_event_risk_level,
)
from analysis.risk_flags import RiskFlag
from data.events.corporate import get_company_events as fetch_corporate_events
from data.events.earnings import get_earnings_provider
from data.events.fomc import get_fomc_events
from data.events.macro import get_macro_events as fetch_macro_events
from data.events.models import NormalizedEvent

logger = logging.getLogger(__name__)


@dataclass
class EventsBundle:
    company_events: List[NormalizedEvent] = field(default_factory=list)
    macro_events: List[NormalizedEvent] = field(default_factory=list)
    nearest_event: Optional[NormalizedEvent] = None
    nearest_event_proximity: Optional[EventProximity] = None
    event_risk_level: str = "NONE"  # HIGH | MEDIUM | LOW | NONE
    event_risk_flags: List[RiskFlag] = field(default_factory=list)
    data_quality: str = "LOW"  # HIGH | MEDIUM | LOW
    earnings_available: bool = False
    earnings_reason: Optional[str] = None


def _data_quality() -> str:
    """
    Coarse, configuration-driven signal (mirrors agents/beginner_agent.py's
    own _compute_data_quality pattern) -- not a per-request success probe,
    since every provider already degrades to [] silently on a transient
    failure rather than raising.
    """
    if not config.has_credentials():
        return "LOW"  # Alpaca (corporate actions) itself isn't configured
    return "HIGH" if config.has_fred_credentials() else "MEDIUM"


def build_event_context(symbol: str) -> EventsBundle:
    symbol = symbol.strip().upper()
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=config.EVENT_LOOKBACK_DAYS)).date()
    end = (now + timedelta(days=config.EVENT_LOOKAHEAD_DAYS)).date()

    try:
        corporate_events = fetch_corporate_events(symbol, start, end)
    except Exception as exc:  # noqa: BLE001 - a provider failure must never break the research bundle
        logger.warning("Corporate events fetch failed for %s: %s", symbol, exc)
        corporate_events = []

    try:
        macro_events = fetch_macro_events(start, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Macro (FRED) events fetch failed: %s", exc)
        macro_events = []

    try:
        fomc_events = get_fomc_events(start, end)
    except Exception as exc:  # noqa: BLE001
        logger.warning("FOMC events fetch failed: %s", exc)
        fomc_events = []

    earnings_provider = get_earnings_provider()
    earnings_result = earnings_provider.get_upcoming_earnings(symbol, start, end)

    company_events = corporate_events + list(earnings_result.events)
    macro_events_all = macro_events + fomc_events

    all_events = company_events + macro_events_all
    proximities = [compute_event_proximity(now, e) for e in all_events]
    upcoming = [p for p in proximities if p.event_window.value != "PAST"]

    nearest_proximity: Optional[EventProximity] = None
    nearest_event: Optional[NormalizedEvent] = None
    if upcoming:
        nearest_proximity = min(upcoming, key=lambda p: p.hours_until)
        nearest_event = nearest_proximity.event

    risk_flags = build_event_risk_flags(upcoming)
    risk_level = overall_event_risk_level(upcoming).value

    return EventsBundle(
        company_events=company_events,
        macro_events=macro_events_all,
        nearest_event=nearest_event,
        nearest_event_proximity=nearest_proximity,
        event_risk_level=risk_level,
        event_risk_flags=risk_flags,
        data_quality=_data_quality(),
        earnings_available=earnings_result.available,
        earnings_reason=earnings_result.reason,
    )
