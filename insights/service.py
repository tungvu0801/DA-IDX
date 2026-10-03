"""
insights/service.py — Orchestration for Stage 2.7E (live data fetch + caching). Read-only everywhere.

The expensive market-data fetch is cached for config.MARKET_SCAN_INTERVAL seconds (the same interval the
Stage 1 market scanner uses); labels are rebuilt on every request from the cached inputs so freshness is
always evaluated against "now". `refresh=True` discards the cache first. Failures never fabricate data:
they return an explicit "unavailable" result.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

import config
from insights.market import (MarketInputs, build_market_insights, fetch_market_inputs, portfolio_exposure_from_view,
                             with_freshness)

logger = logging.getLogger(__name__)

_cache: dict = {}
market_inputs_fn: Callable[[datetime], MarketInputs] = fetch_market_inputs  # replaceable in tests


def get_market_inputs(refresh: bool = False) -> Optional[MarketInputs]:
    now = datetime.now(timezone.utc)
    entry = _cache.get("inputs")
    if refresh or entry is None or time.time() - entry[0] > config.MARKET_SCAN_INTERVAL:
        try:
            inputs = market_inputs_fn(now)
        except Exception as exc:  # noqa: BLE001 - market data unavailable must degrade, never crash
            logger.warning("Market inputs unavailable: %s", exc)
            _cache.pop("inputs", None)
            return None
        _cache["inputs"] = (time.time(), inputs)
        return inputs
    return entry[1]


def market_insights(view=None, refresh: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    inputs = get_market_inputs(refresh)
    if inputs is None:
        return {"available": False, "status": "MARKET_DATA_UNAVAILABLE", "stale": True,
                "message": "Current market trend unavailable (market data could not be fetched).",
                "stale_message": None, "fetched_at": None}
    return with_freshness(build_market_insights(inputs, now, portfolio_exposure_from_view(view)), now)


def live_stock_data(symbol: str) -> Tuple[Optional[object], Optional[object]]:
    """Current Stage 1 technicals + Stage 2 sector/market context for one symbol (Alpaca, read-only)."""
    from analysis.sector_context import compute_sector_context
    from scanner.market_scanner import get_data_client
    from scanner.watchlist import analyze_watchlist
    try:
        client = get_data_client()
    except Exception:  # noqa: BLE001
        return None, None
    try:
        metrics = analyze_watchlist(client, [symbol]).get(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Stock metrics unavailable for %s: %s", symbol, exc)
        return None, None
    sector = None
    if metrics is not None:
        try:
            sector = compute_sector_context(symbol, metrics.pct_change, client)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sector context unavailable for %s: %s", symbol, exc)
    return metrics, sector


stock_data_fn: Callable[[str], Tuple[Optional[object], Optional[object]]] = live_stock_data


def history_bars(symbols, lookback_days: int) -> dict:
    """Verified historical daily bars (Alpaca, read-only) for Trader Review reconstruction and outcomes."""
    from data.market_data import fetch_daily_bars
    from scanner.market_scanner import get_data_client
    try:
        return fetch_daily_bars(get_data_client(), sorted(set(symbols)), lookback_days=lookback_days)
    except Exception as exc:  # noqa: BLE001 - history unavailable -> reviews report UNAVAILABLE, never guess
        logger.warning("Historical bars unavailable: %s", exc)
        return {}


history_fn = history_bars


def symbols_metrics(symbols, ttl_seconds: int = 120) -> dict:
    """Current Stage 1 market metrics (Alpaca, read-only) for several symbols in one batch; short in-memory cache."""
    from scanner.market_scanner import get_data_client
    from scanner.watchlist import analyze_watchlist
    key = ("metrics", tuple(sorted(set(symbols))))
    entry = _cache.get(key)
    if entry is not None and time.time() - entry[0] <= ttl_seconds:
        return entry[1]
    try:
        result = analyze_watchlist(get_data_client(), sorted(set(symbols))) if symbols else {}
    except Exception as exc:  # noqa: BLE001 - unavailable metrics show as unavailable, never guessed
        logger.warning("Symbol metrics unavailable: %s", exc)
        return {}
    _cache[key] = (time.time(), result)
    return result


def cached_patterns(compute: Callable[[], dict], ttl_seconds: int = 1800) -> Optional[dict]:
    """Trader Review patterns are expensive (orders + price history); cache them for 30 minutes."""
    entry = _cache.get("patterns")
    if entry is not None and time.time() - entry[0] <= ttl_seconds:
        return entry[1]
    try:
        result = compute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Trading patterns unavailable: %s", exc)
        return None
    _cache["patterns"] = (time.time(), result)
    return result


symbols_metrics_fn = symbols_metrics
