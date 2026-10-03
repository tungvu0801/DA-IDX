"""
data/events/earnings.py — Earnings-date event interface. NO verified data
source is currently wired up.

Confirmed this stage: there is no earnings-calendar-capable source
anywhere in this project's stack (Alpaca's News/Screener/Corporate-Actions
APIs don't provide earnings dates, and no third-party earnings API is
configured). Per explicit instruction, this module NEVER scrapes, never
infers a date from news or price action, and never lets Claude fill the
gap from its own knowledge -- it always reports unavailable, with a clear
reason, so a real provider can be wired in behind this exact interface in
a later stage without touching any caller.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from data.events.models import NormalizedEvent

EARNINGS_UNAVAILABLE_REASON = "Earnings-calendar data is not currently available from a verified provider."


@dataclass
class EarningsQueryResult:
    available: bool
    events: List[NormalizedEvent] = field(default_factory=list)  # always [] while unavailable
    reason: Optional[str] = None


class EarningsEventProvider:
    """Interface a future real provider implements. Today, the only
    implementation (below) always reports unavailable."""

    def get_upcoming_earnings(self, symbol: str, start_date: date, end_date: date) -> EarningsQueryResult:
        raise NotImplementedError

    def get_recent_earnings(self, symbol: str, start_date: date, end_date: date) -> EarningsQueryResult:
        raise NotImplementedError


class UnavailableEarningsProvider(EarningsEventProvider):
    def get_upcoming_earnings(self, symbol: str, start_date: date, end_date: date) -> EarningsQueryResult:
        return EarningsQueryResult(available=False, events=[], reason=EARNINGS_UNAVAILABLE_REASON)

    def get_recent_earnings(self, symbol: str, start_date: date, end_date: date) -> EarningsQueryResult:
        return EarningsQueryResult(available=False, events=[], reason=EARNINGS_UNAVAILABLE_REASON)


_provider = UnavailableEarningsProvider()


def get_earnings_provider() -> EarningsEventProvider:
    return _provider
