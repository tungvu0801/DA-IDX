"""
data/events/fomc.py — REAL FOMC meeting/decision/minutes events, parsed
from the official Federal Reserve calendar page:
https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm

Verified (2026-09-27) that this page has no JSON/ICS feed -- it's a plain
HTML page organized by year, so it's parsed at runtime and cached
(config.FOMC_CACHE_TTL_MINUTES) rather than re-fetched on every request.

Honesty rules, verified against the live page structure:
  - FOMC_MEETING: the meeting date range is always known once a year's
    section is published. Some future dates are tentative (marked with an
    asterisk on the source page for "Summary of Economic Projections"
    meetings, which is unrelated to date confidence, but rows for the
    *next* calendar year are treated as `confirmed=False` since the Fed
    itself describes next-year dates as subject to change).
  - FOMC_DECISION: the Fed always announces its decision on the LAST day
    of a 2-day meeting (or the single day, for a notation-vote meeting) --
    that date is exactly as confirmed as the meeting date itself. The page
    never states an exact announcement TIME, so this is always
    time_precision=DATE_ONLY. Never hardcode "2:00 PM ET" here even though
    that's the well-known convention -- that belongs in prose explanation
    only (agents/beginner_agent.py's narrative), never in this data layer.
  - FOMC_MINUTES: created ONLY when the page's own "Minutes" block for that
    meeting states an explicit "(Released <Month> <Day>, <Year>)" -- i.e.
    only for meetings whose minutes have ALREADY been released. For a
    meeting whose minutes are still pending, this block is empty on the
    live page (verified for the most recent meeting as of this writing),
    and NO event is created -- never derived as "meeting date + 3 weeks."
"""
import logging
import re
from datetime import date, datetime, timezone
from typing import List, Optional
from uuid import uuid4

import requests
from bs4 import BeautifulSoup

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

FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_USER_AGENT = "stock-agent-research-tool/1.0 (personal research project)"
_REQUEST_TIMEOUT_SECONDS = 15

_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
_DATE_RANGE_RE = re.compile(r"(\d{1,2})(?:-(\d{1,2}))?")
_RELEASED_RE = re.compile(r"Released\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")
_YEAR_HEADING_RE = re.compile(r"(\d{4})\s+FOMC Meetings")


def _fetch_raw_html() -> Optional[str]:
    try:
        resp = requests.get(FOMC_CALENDAR_URL, headers={"User-Agent": _USER_AGENT}, timeout=_REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("Failed to fetch FOMC calendar page: %s", exc)
        return None


def _resolve_range(year: int, month: int, start_day: int, end_day: int) -> Optional[tuple]:
    """Handles a meeting that crosses a month boundary (e.g. 'April 30-May 1' -> date div '30-1')."""
    try:
        start = date(year, month, start_day)
    except ValueError:
        return None
    if end_day >= start_day:
        try:
            end = date(year, month, end_day)
        except ValueError:
            return None
    else:
        next_month = month + 1
        next_year = year
        if next_month > 12:
            next_month = 1
            next_year += 1
        try:
            end = date(next_year, next_month, end_day)
        except ValueError:
            return None
    return start, end


def _parse_calendar_html(html: str) -> List[dict]:
    """Returns a list of {start_date, end_date, has_projections, confirmed, minutes_released} dicts."""
    soup = BeautifulSoup(html, "html.parser")
    meetings: List[dict] = []

    current_year_this_calendar_year = datetime.now(timezone.utc).year
    for heading in soup.find_all(string=_YEAR_HEADING_RE):
        year_match = _YEAR_HEADING_RE.search(str(heading))
        if not year_match:
            continue
        year = int(year_match.group(1))

        panel = heading.find_parent("div", class_="panel-heading")
        panel_container = panel.find_parent("div", class_="panel") if panel else None
        if panel_container is None:
            continue

        for row in panel_container.find_all("div", class_="fomc-meeting"):
            month_div = row.find(class_="fomc-meeting__month")
            date_div = row.find(class_="fomc-meeting__date")
            minutes_div = row.find(class_="fomc-meeting__minutes")
            if month_div is None or date_div is None:
                continue

            month_name = month_div.get_text(strip=True)
            month = _MONTHS.get(month_name)
            if month is None:
                continue

            raw_date_text = date_div.get_text(strip=True)
            has_projections = "*" in raw_date_text
            m = _DATE_RANGE_RE.match(raw_date_text)
            if not m:
                continue
            start_day = int(m.group(1))
            end_day = int(m.group(2)) if m.group(2) else start_day

            resolved = _resolve_range(year, month, start_day, end_day)
            if resolved is None:
                continue
            start_dt, end_dt = resolved

            minutes_released = None
            if minutes_div is not None:
                rm = _RELEASED_RE.search(minutes_div.get_text(" ", strip=True))
                if rm:
                    mon = _MONTHS.get(rm.group(1))
                    if mon:
                        try:
                            minutes_released = date(int(rm.group(3)), mon, int(rm.group(2)))
                        except ValueError:
                            minutes_released = None

            # The Fed presents next-calendar-year dates as tentative until confirmed closer to the year.
            confirmed = year <= current_year_this_calendar_year

            meetings.append(
                {
                    "year": year,
                    "start_date": start_dt,
                    "end_date": end_dt,
                    "raw_date_text": raw_date_text,
                    "has_projections": has_projections,
                    "confirmed": confirmed,
                    "minutes_released": minutes_released,
                }
            )

    return meetings


def _get_parsed_meetings() -> List[dict]:
    cached = event_cache.get("fomc:calendar")
    if cached is not None:
        return cached

    html = _fetch_raw_html()
    if html is None:
        return []

    try:
        meetings = _parse_calendar_html(html)
    except Exception as exc:  # noqa: BLE001 - a parsing failure must not crash the caller
        logger.warning("Failed to parse FOMC calendar page: %s", exc)
        return []

    event_cache.set("fomc:calendar", meetings, config.FOMC_CACHE_TTL_MINUTES)
    return meetings


def get_fomc_events(start: date, end: date) -> List[NormalizedEvent]:
    """Real FOMC_MEETING/FOMC_DECISION/FOMC_MINUTES events overlapping [start, end]. [] on any failure."""
    meetings = _get_parsed_meetings()
    if not meetings:
        return []

    now = datetime.now(timezone.utc)
    today = now.date()
    events: List[NormalizedEvent] = []

    for meeting in meetings:
        meeting_start, meeting_end = meeting["start_date"], meeting["end_date"]
        if meeting_end < start or meeting_start > end:
            continue

        ref = f"fomc-{meeting_start.isoformat()}"
        common = dict(
            symbol=None,
            category=EventCategory.MACRO,
            event_time=None,
            timezone=None,
            event_datetime_utc=None,
            time_precision=TimePrecision.DATE_ONLY,
            source="Federal Reserve FOMC calendar",
            source_url=FOMC_CALENDAR_URL,
            source_provider="FED",
            confirmed=meeting["confirmed"],
            retrieved_at=now,
        )

        events.append(
            NormalizedEvent(
                event_id=f"{ref}-meeting",
                event_type=EventType.FOMC_MEETING,
                title=f"FOMC Meeting ({meeting_start.strftime('%b %d')}-{meeting_end.strftime('%d, %Y')})"
                if meeting_start.month == meeting_end.month
                else f"FOMC Meeting ({meeting_start.strftime('%b %d')}-{meeting_end.strftime('%b %d, %Y')})",
                event_date=meeting_end,
                status=EventStatus.RELEASED if meeting_end <= today else EventStatus.UPCOMING,
                importance=EventImportance.HIGH,
                raw_reference_id=ref,
                metadata={
                    "meeting_start_date": meeting_start.isoformat(),
                    "meeting_end_date": meeting_end.isoformat(),
                    "has_projections": meeting["has_projections"],
                },
                **common,
            )
        )

        events.append(
            NormalizedEvent(
                event_id=f"{ref}-decision",
                event_type=EventType.FOMC_DECISION,
                title="FOMC Interest Rate Decision",
                event_date=meeting_end,
                status=EventStatus.RELEASED if meeting_end <= today else EventStatus.UPCOMING,
                importance=EventImportance.HIGH,
                raw_reference_id=ref,
                metadata={"meeting_start_date": meeting_start.isoformat(), "meeting_end_date": meeting_end.isoformat()},
                **common,
            )
        )

        minutes_released = meeting["minutes_released"]
        if minutes_released is not None and start <= minutes_released <= end:
            events.append(
                NormalizedEvent(
                    event_id=f"{ref}-minutes",
                    event_type=EventType.FOMC_MINUTES,
                    title="FOMC Meeting Minutes Released",
                    event_date=minutes_released,
                    status=EventStatus.RELEASED,
                    importance=EventImportance.MEDIUM,
                    raw_reference_id=ref,
                    metadata={"meeting_start_date": meeting_start.isoformat(), "meeting_end_date": meeting_end.isoformat()},
                    **common,
                )
            )
        # else: minutes not yet explicitly published for this meeting -- correctly omitted, never estimated.

    return events
