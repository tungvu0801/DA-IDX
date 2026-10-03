"""
api/routes/market.py — Market-wide scanner endpoints.

Results are cached in memory for config.MARKET_SCAN_INTERVAL seconds so
repeated page loads/polling don't re-hit Alpaca on every request.
"""
import logging
from typing import List

from fastapi import APIRouter, HTTPException

import config
from api.cache import cache
from models.schemas import MarketOverviewResponse, TickerMetricsResponse
from scanner.market_scanner import scan_market

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["market"])


def get_cached_overview():
    """Shared by other route modules (stocks, alerts) that also need the overview."""
    return cache.get_or_set("market_overview", config.MARKET_SCAN_INTERVAL, scan_market)


@router.get("/market/overview", response_model=MarketOverviewResponse)
def get_overview() -> MarketOverviewResponse:
    try:
        overview = get_cached_overview()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a scan failure must surface as a clean HTTP error
        logger.error("Market overview failed: %s", exc)
        raise HTTPException(status_code=502, detail="Market data temporarily unavailable.") from exc

    return MarketOverviewResponse(
        top_gainers=[TickerMetricsResponse.model_validate(m) for m in overview.top_gainers],
        top_losers=[TickerMetricsResponse.model_validate(m) for m in overview.top_losers],
        unusual_volume=[TickerMetricsResponse.model_validate(m) for m in overview.unusual_volume],
        momentum_stocks=[TickerMetricsResponse.model_validate(m) for m in overview.momentum_stocks],
        possible_breakouts=[TickerMetricsResponse.model_validate(m) for m in overview.possible_breakouts],
        approaching_support=[TickerMetricsResponse.model_validate(m) for m in overview.approaching_support],
        approaching_resistance=[
            TickerMetricsResponse.model_validate(m) for m in overview.approaching_resistance
        ],
        volatility_expansion=[
            TickerMetricsResponse.model_validate(m) for m in overview.volatility_expansion
        ],
        used_fallback_universe=overview.used_fallback_universe,
        scanned_symbol_count=overview.scanned_symbol_count,
        liquid_symbol_count=len(overview.all_metrics),
    )


@router.get("/market/movers", response_model=List[TickerMetricsResponse])
def get_movers() -> List[TickerMetricsResponse]:
    try:
        overview = get_cached_overview()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Movers fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="Market data temporarily unavailable.") from exc

    combined = overview.top_gainers + overview.top_losers
    return [TickerMetricsResponse.model_validate(m) for m in combined]


@router.get("/scanner", response_model=MarketOverviewResponse)
def get_scanner() -> MarketOverviewResponse:
    """Alias for /market/overview — matches the originally-specified /api/scanner path."""
    return get_overview()
