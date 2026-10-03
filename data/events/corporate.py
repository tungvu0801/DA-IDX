"""
data/events/corporate.py — REAL corporate-action events (splits, dividends,
mergers, spin-offs, etc.) via Alpaca's market-data Corporate Actions API.

Uses alpaca.data.historical.corporate_actions.CorporateActionsClient, which
hits the same market-data base URL (and the same ALPACA_API_KEY/SECRET_KEY
credentials) already used everywhere else in this project -- NOT
alpaca.trading.client.TradingClient, which is never imported anywhere here.
Reference: https://docs.alpaca.markets/reference/corporateactions-1

Alpaca documents that corporate-action creation/availability can lag the
official record -- that limitation is surfaced honestly via
metadata["provider_note"] on every event this module returns, never hidden.
"""
import logging
from datetime import date, datetime, timezone
from typing import List, Optional
from uuid import uuid4

from alpaca.common.exceptions import APIError
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest

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

PROVIDER_NOTE = (
    "Alpaca corporate-action data can lag the official record; treat as best-available, not guaranteed-complete."
)

_TYPE_MAP = {
    "forward_splits": EventType.STOCK_SPLIT,
    "reverse_splits": EventType.REVERSE_SPLIT,
    "unit_splits": EventType.OTHER_CORPORATE_ACTION,
    "cash_dividends": EventType.DIVIDEND,
    "stock_dividends": EventType.DIVIDEND,
    "spin_offs": EventType.SPINOFF,
    "cash_mergers": EventType.MERGER,
    "stock_mergers": EventType.MERGER,
    "stock_and_cash_mergers": EventType.MERGER,
    "redemptions": EventType.OTHER_CORPORATE_ACTION,
    "name_changes": EventType.OTHER_CORPORATE_ACTION,
    "worthless_removals": EventType.OTHER_CORPORATE_ACTION,
    "rights_distributions": EventType.OTHER_CORPORATE_ACTION,
}

_HIGH_SIGNAL_TYPES = {EventType.STOCK_SPLIT, EventType.REVERSE_SPLIT, EventType.MERGER, EventType.SPINOFF}


def _get_client() -> CorporateActionsClient:
    if not config.has_credentials():
        raise RuntimeError("Missing Alpaca API credentials.")
    return CorporateActionsClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)


def _event_date_for(item) -> Optional[date]:
    for attr in ("ex_date", "process_date", "effective_date", "payable_date", "record_date"):
        value = getattr(item, attr, None)
        if value is not None:
            return value
    return None


def _title_for(action_type: str, item, symbol: str) -> str:
    if action_type in ("forward_splits", "reverse_splits", "unit_splits"):
        old_rate = getattr(item, "old_rate", None)
        new_rate = getattr(item, "new_rate", None)
        kind = "reverse split" if action_type == "reverse_splits" else "split"
        return f"{symbol} {new_rate:g}:{old_rate:g} {kind}" if old_rate and new_rate else f"{symbol} {kind}"
    if action_type in ("cash_dividends", "stock_dividends"):
        rate = getattr(item, "rate", None)
        if action_type == "cash_dividends":
            return f"{symbol} cash dividend" + (f" (${rate:g}/share)" if rate is not None else "")
        return f"{symbol} stock dividend"
    if action_type == "spin_offs":
        new_symbol = getattr(item, "new_symbol", None)
        return f"{symbol} spin-off" + (f" into {new_symbol}" if new_symbol else "")
    if action_type in ("cash_mergers", "stock_mergers", "stock_and_cash_mergers"):
        acquirer = getattr(item, "acquirer_symbol", None)
        return f"{symbol} merger" + (f" (acquirer: {acquirer})" if acquirer else "")
    return f"{symbol} corporate action ({action_type})"


def get_company_events(symbol: str, start: date, end: date) -> List[NormalizedEvent]:
    """Real corporate actions for `symbol` between `start`/`end` (inclusive). [] on any failure -- never fabricated."""
    symbol = symbol.strip().upper()
    cache_key = f"corporate:{symbol}:{start.isoformat()}:{end.isoformat()}"
    cached = event_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        client = _get_client()
    except RuntimeError as exc:
        logger.info("Corporate actions unavailable for %s: %s", symbol, exc)
        return []

    try:
        request = CorporateActionsRequest(symbols=[symbol], start=start, end=end)
        result = client.get_corporate_actions(request)
    except APIError as exc:
        logger.warning("Corporate actions fetch failed for %s: %s", symbol, exc)
        return []
    except Exception as exc:  # noqa: BLE001 - a provider failure must not crash the caller
        logger.warning("Unexpected error fetching corporate actions for %s: %s", symbol, exc)
        return []

    now = datetime.now(timezone.utc)
    events: List[NormalizedEvent] = []
    for action_type, items in (result.data or {}).items():
        event_type = _TYPE_MAP.get(action_type, EventType.OTHER_CORPORATE_ACTION)
        for item in items:
            event_date = _event_date_for(item)
            if event_date is None:
                continue
            events.append(
                NormalizedEvent(
                    event_id=f"alpaca-corp-{getattr(item, 'id', uuid4().hex)}",
                    event_type=event_type,
                    category=EventCategory.COMPANY,
                    symbol=symbol,
                    title=_title_for(action_type, item, symbol),
                    event_date=event_date,
                    event_time=None,
                    timezone=None,
                    event_datetime_utc=None,
                    time_precision=TimePrecision.DATE_ONLY,
                    source="Alpaca Corporate Actions API",
                    source_url="https://docs.alpaca.markets/reference/corporateactions-1",
                    source_provider="ALPACA",
                    status=EventStatus.RELEASED if event_date <= now.date() else EventStatus.UPCOMING,
                    importance=EventImportance.MEDIUM if event_type in _HIGH_SIGNAL_TYPES else EventImportance.LOW,
                    confirmed=True,
                    retrieved_at=now,
                    raw_reference_id=str(getattr(item, "id", "")) or None,
                    metadata={"action_type": action_type, "provider_note": PROVIDER_NOTE},
                )
            )

    event_cache.set(cache_key, events, config.CORPORATE_ACTION_CACHE_TTL_MINUTES)
    return events
