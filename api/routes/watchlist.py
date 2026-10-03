"""
api/routes/watchlist.py — Personal watchlist endpoints: view, add, remove.

Watchlist metrics are cached for config.WATCHLIST_SCAN_INTERVAL seconds,
same reasoning as the market overview cache. Adding/removing a symbol
invalidates the cache immediately so the change is reflected right away.
"""
import logging
from typing import List

from fastapi import APIRouter, HTTPException

import config
from api.cache import cache
from api.validation import validate_symbol
from models.schemas import TickerMetricsResponse, WatchlistAddRequest, WatchlistResponse
from scanner.market_scanner import get_data_client
from scanner.watchlist import add_to_watchlist, analyze_watchlist, load_watchlist, remove_from_watchlist

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


def _compute_watchlist():
    client = get_data_client()
    symbols = load_watchlist()
    return symbols, analyze_watchlist(client, symbols)


def get_cached_watchlist():
    """Shared by other route modules (alerts) that also need watchlist metrics."""
    return cache.get_or_set("watchlist_metrics", config.WATCHLIST_SCAN_INTERVAL, _compute_watchlist)


@router.get("", response_model=WatchlistResponse)
def get_watchlist() -> WatchlistResponse:
    try:
        symbols, metrics = get_cached_watchlist()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Watchlist fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="Market data temporarily unavailable.") from exc

    return WatchlistResponse(
        symbols=symbols,
        metrics={sym: TickerMetricsResponse.model_validate(m) for sym, m in metrics.items()},
    )


@router.post("", response_model=List[str])
def post_watchlist(body: WatchlistAddRequest) -> List[str]:
    symbols = add_to_watchlist(body.symbol)
    cache.invalidate("watchlist_metrics")
    return symbols


@router.delete("/{symbol}", response_model=List[str])
def delete_watchlist_symbol(symbol: str) -> List[str]:
    try:
        symbol = validate_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    symbols = remove_from_watchlist(symbol)
    cache.invalidate("watchlist_metrics")
    return symbols
