"""
api/routes/stocks.py — Per-symbol detail, AI technical analysis, and news.

GET /api/stocks/{symbol}           metrics + "consider looking at" alternatives
GET /api/stocks/{symbol}/analysis  AI Technical Agent explanation (or "unavailable")
GET /api/stocks/{symbol}/news      recent news from Alpaca's news feed
"""
import logging
from typing import List, Optional, Tuple

from fastapi import APIRouter, HTTPException

import config
from agents.technical_agent import get_technical_analysis
from analysis.indicators import TickerMetrics
from api.routes.market import get_cached_overview
from api.validation import validate_symbol
from data.news import get_recent_news
from models.schemas import (
    AlternativeCandidate,
    StockDetailResponse,
    TechnicalAnalysisResponse,
    TickerMetricsResponse,
)
from scanner.market_scanner import MarketOverview, get_data_client
from scanner.watchlist import analyze_watchlist

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stocks", tags=["stocks"])


def _get_symbol_metrics(symbol: str) -> Tuple[Optional[TickerMetrics], Optional[MarketOverview]]:
    """
    Look up one symbol's metrics: reuse the cached market overview if it's
    already covered there, otherwise fetch it directly (e.g. a watchlist-only
    symbol that never showed up in the screener's candidate list).
    """
    overview: Optional[MarketOverview] = None
    try:
        overview = get_cached_overview()
    except RuntimeError:
        pass  # missing credentials handled by the direct-fetch path below

    if overview is not None and symbol in overview.all_metrics:
        return overview.all_metrics[symbol], overview

    client = get_data_client()
    metrics = analyze_watchlist(client, [symbol])
    return metrics.get(symbol), overview


@router.get("/{symbol}", response_model=StockDetailResponse)
def get_stock_detail(symbol: str) -> StockDetailResponse:
    try:
        symbol = validate_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        m, overview = _get_symbol_metrics(symbol)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Stock detail failed for %s: %s", symbol, exc)
        raise HTTPException(status_code=502, detail="Market data temporarily unavailable.") from exc

    if m is None:
        raise HTTPException(status_code=404, detail=f"No data found for symbol '{symbol}'.")

    alternatives: List[AlternativeCandidate] = []
    if overview is not None:
        seen = set()
        for c in overview.momentum_stocks + overview.possible_breakouts:
            if c.symbol == m.symbol or c.symbol in seen:
                continue
            seen.add(c.symbol)
            alternatives.append(
                AlternativeCandidate(
                    symbol=c.symbol,
                    signal=c.signal,
                    attention_score=c.attention_score,
                    pct_change=c.pct_change,
                )
            )

    return StockDetailResponse(
        metrics=TickerMetricsResponse.model_validate(m),
        alternatives=alternatives[: config.TOP_RESULTS],
    )


@router.get("/{symbol}/analysis", response_model=TechnicalAnalysisResponse)
def get_stock_analysis(symbol: str) -> TechnicalAnalysisResponse:
    try:
        symbol = validate_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        m, _ = _get_symbol_metrics(symbol)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Stock analysis failed for %s: %s", symbol, exc)
        raise HTTPException(status_code=502, detail="Market data temporarily unavailable.") from exc

    if m is None:
        raise HTTPException(status_code=404, detail=f"No data found for symbol '{symbol}'.")

    return TechnicalAnalysisResponse(
        symbol=m.symbol,
        analysis=get_technical_analysis(m),
        ai_available=config.has_llm_credentials(),
    )


@router.get("/{symbol}/news")
def get_stock_news(symbol: str) -> list:
    try:
        symbol = validate_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return get_recent_news(symbol)
