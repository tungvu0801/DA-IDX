"""
data/events/models.py — Normalized event model shared by every provider in
data/events/. Every field mirrors what a REAL source stated; nothing here
is ever fabricated, inferred, or derived from a general rule of thumb
(e.g. "minutes are usually released 3 weeks after the meeting" is never
used to manufacture an event_date -- see data/events/fomc.py).
"""
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime
from datetime import time as time_type
from enum import Enum
from typing import Any, Dict, Optional


class EventCategory(str, Enum):
    COMPANY = "COMPANY"
    MACRO = "MACRO"


class EventType(str, Enum):
    # COMPANY
    EARNINGS = "EARNINGS"
    DIVIDEND = "DIVIDEND"
    STOCK_SPLIT = "STOCK_SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    MERGER = "MERGER"
    SPINOFF = "SPINOFF"
    OTHER_CORPORATE_ACTION = "OTHER_CORPORATE_ACTION"
    # MACRO
    CPI = "CPI"
    PPI = "PPI"
    EMPLOYMENT_SITUATION = "EMPLOYMENT_SITUATION"
    JOLTS = "JOLTS"
    PCE = "PCE"
    FOMC_MEETING = "FOMC_MEETING"
    FOMC_DECISION = "FOMC_DECISION"
    FOMC_MINUTES = "FOMC_MINUTES"
    OTHER_MAJOR_MACRO = "OTHER_MAJOR_MACRO"


class EventStatus(str, Enum):
    UPCOMING = "UPCOMING"
    RELEASED = "RELEASED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class EventImportance(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class TimePrecision(str, Enum):
    EXACT = "EXACT"       # source stated a specific time
    DATE_ONLY = "DATE_ONLY"  # only a date is known -- never synthesize a time


@dataclass
class NormalizedEvent:
    event_id: str
    event_type: EventType
    category: EventCategory
    symbol: Optional[str]  # None for macro events
    title: str

    event_date: date_type
    event_time: Optional[time_type]        # None unless the source stated an exact time
    timezone: Optional[str]                # source's own zone, e.g. "America/New_York"
    event_datetime_utc: Optional[datetime]  # None when time_precision == DATE_ONLY
    time_precision: TimePrecision

    source: str              # human-readable source name, e.g. "Federal Reserve FOMC calendar"
    source_url: Optional[str]
    source_provider: str     # short code: "ALPACA" | "FRED" | "FED" (used in fingerprinting)

    status: EventStatus
    importance: EventImportance
    confirmed: bool          # False for a tentative/starred meeting date, etc.

    retrieved_at: datetime
    raw_reference_id: Optional[str] = None  # provider's own id for this record, if any
    metadata: Dict[str, Any] = field(default_factory=dict)

    actual: Optional[str] = None
    consensus: Optional[str] = None
    previous: Optional[str] = None
