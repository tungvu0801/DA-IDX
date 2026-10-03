"""
data/events/macro.py — REAL macro-release events (CPI, PPI, Employment
Situation, PCE) via the FRED (Federal Reserve Bank of St. Louis)
release-dates API: https://api.stlouisfed.org/fred/release/dates

Verified (2026-09-27): a genuine REST/JSON endpoint requiring a free
`api_key`. Source priority per project policy: the official publishing
agency (BLS for CPI/PPI/Employment Situation/JOLTS, BEA for PCE) outranks
FRED, which is a structured official AGGREGATOR of those same release
calendars, not the original publisher. This module wires FRED as the one
live feed for this stage; a direct BLS/BEA feed is a documented future
addition (see config.FRED_RELEASE_IDS' comment) -- when one is added, the
two must be deduplicated to a single event per release, preferring the
original publisher, keyed on (event_type, event_date, source_provider).

Two honesty rules, both from explicit review corrections:
  1. Every forward-looking query passes include_release_dates_with_no_data
     =true -- FRED's documented default (false) can silently exclude
     future release dates that don't have data yet, which would make this
     calendar quietly miss upcoming CPI/PPI/jobs/PCE releases.
  2. FRED supplies a DATE ONLY. This module never attaches a conventional
     release time (e.g. "8:30 AM ET") merely because that's the release's
     usual time -- time_precision is always DATE_ONLY here.

Without config.FRED_API_KEY configured (the default in this project),
get_macro_events() returns [] and callers see this as UNAVAILABLE via
services/event_context.py's data-quality reporting -- never guessed.
"""
import logging
from datetime import date, datetime, timezone
from typing import List, Optional

import requests

import config
from data.events.cache import event_cache
from data.events.models import (
    EventCategory,
    EventImportance,
    EventStatus,
    EventType,
    NormalizedEvent,
    TimePrecision,
)

logger = logging.getLogger(__name__)

FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
_REQUEST_TIMEOUT_SECONDS = 15

_TITLES = {
    "CPI": "Consumer Price Index (CPI)",
    "PPI": "Producer Price Index (PPI)",
    "EMPLOYMENT_SITUATION": "Employment Situation (Jobs Report)",
    "PCE": "Personal Income and Outlays (PCE)",
}


def _fetch_release_dates(release_id: int, start: date, end: date) -> Optional[List[date]]:
    """Real FRED fetch for one release_id. None on any failure/misconfiguration -- never fabricated."""
    if not config.has_fred_credentials():
        return None

    cache_key = f"fred:{release_id}:{start.isoformat()}:{end.isoformat()}"
    cached = event_cache.get(cache_key)
    if cached is not None:
        return cached

    params = {
        "release_id": release_id,
        "api_key": config.FRED_API_KEY,
        "file_type": "json",
        "realtime_start": start.isoformat(),
        "realtime_end": end.isoformat(),
        # Correction from review: without this, FRED's documented default
        # (false) can exclude future dates that don't have data yet --
        # exactly the upcoming releases this calendar most needs.
        "include_release_dates_with_no_data": "true",
    }
    try:
        resp = requests.get(FRED_RELEASE_DATES_URL, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        logger.warning("FRED release_dates fetch failed for release_id=%s: %s", release_id, exc)
        return None
    except ValueError as exc:  # malformed JSON
        logger.warning("FRED release_dates returned malformed JSON for release_id=%s: %s", release_id, exc)
        return None

    dates: List[date] = []
    for row in payload.get("release_dates", []):
        raw = row.get("date")
        if not raw:
            continue
        try:
            dates.append(date.fromisoformat(raw))
        except ValueError:
            continue

    event_cache.set(cache_key, dates, config.MACRO_CACHE_TTL_MINUTES)
    return dates


def get_macro_events(start: date, end: date) -> List[NormalizedEvent]:
    """
    Real CPI/PPI/Employment Situation/PCE events overlapping [start, end].
    [] if no FRED_API_KEY is configured, or on any fetch failure -- never
    guessed. JOLTS is intentionally not queried (see config.FRED_RELEASE_IDS).
    """
    if not config.has_fred_credentials():
        return []

    now = datetime.now(timezone.utc)
    today = now.date()
    events: List[NormalizedEvent] = []

    for event_type_name, release_id in config.FRED_RELEASE_IDS.items():
        dates = _fetch_release_dates(release_id, start, end)
        if not dates:
            continue
        event_type = EventType[event_type_name]
        for release_date in dates:
            if release_date < start or release_date > end:
                continue
            events.append(
                NormalizedEvent(
                    event_id=f"fred-{release_id}-{release_date.isoformat()}",
                    event_type=event_type,
                    category=EventCategory.MACRO,
                    symbol=None,
                    title=_TITLES.get(event_type_name, event_type_name),
                    event_date=release_date,
                    event_time=None,
                    timezone=None,
                    event_datetime_utc=None,
                    time_precision=TimePrecision.DATE_ONLY,
                    source="FRED (release calendar; aggregates the official BLS/BEA release schedule)",
                    source_url=f"https://fred.stlouisfed.org/release?rid={release_id}",
                    source_provider="FRED",
                    status=EventStatus.RELEASED if release_date <= today else EventStatus.UPCOMING,
                    importance=EventImportance.HIGH,
                    confirmed=True,
                    retrieved_at=now,
                    raw_reference_id=f"fred-release-{release_id}",
                    metadata={"fred_release_id": release_id},
                )
            )

    return events
