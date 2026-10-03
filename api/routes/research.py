"""
api/routes/research.py — The beginner research view and research-setup /
position-sizing endpoints.

GET /api/stocks/{symbol}/research                     full beginner research bundle
GET /api/stocks/{symbol}/research-setup                entry/invalidation/targets/R:R (Python-only arithmetic)
GET /api/stocks/{symbol}/position-sizing?portfolio_value=...   deterministic position sizing
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import config
from agents.beginner_agent import build_beginner_research
from analysis.position_sizing import compute_position_sizing, position_adjustment_unavailable_message
from analysis.research_setup import compute_research_setup
from api.validation import validate_symbol
from models.schemas import (
    BeginnerNarrativeResponse,
    BeginnerResearchResponse,
    CatalystItemResponse,
    DataQualityResponse,
    EventItemResponse,
    EventsResponse,
    EvidenceCategoryResponse,
    EvidenceResponse,
    PositionSizingResponse,
    ResearchSetupResponse,
    RiskFlagResponse,
    SectorContextResponse,
)
from scanner.market_scanner import get_data_client
from scanner.watchlist import analyze_watchlist
from services.analysis_cache import analysis_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/stocks", tags=["research"])


def _event_to_response(e) -> EventItemResponse:
    return EventItemResponse(
        event_id=e.event_id,
        event_type=e.event_type.value,
        category=e.category.value,
        symbol=e.symbol,
        title=e.title,
        event_date=e.event_date.isoformat(),
        event_time=e.event_time.isoformat() if e.event_time is not None else None,
        timezone=e.timezone,
        event_datetime_utc=e.event_datetime_utc,
        time_precision=e.time_precision.value,
        source=e.source,
        source_url=e.source_url,
        source_provider=e.source_provider,
        status=e.status.value,
        importance=e.importance.value,
        confirmed=e.confirmed,
        retrieved_at=e.retrieved_at,
        raw_reference_id=e.raw_reference_id,
        metadata=e.metadata,
        actual=e.actual,
        consensus=e.consensus,
        previous=e.previous,
    )


def events_bundle_to_response(bundle) -> Optional[EventsResponse]:
    if bundle is None:
        return None
    return EventsResponse(
        company_events=[_event_to_response(e) for e in bundle.company_events],
        macro_events=[_event_to_response(e) for e in bundle.macro_events],
        nearest_event=_event_to_response(bundle.nearest_event) if bundle.nearest_event is not None else None,
        event_risk_level=bundle.event_risk_level,
        event_risk_flags=[
            RiskFlagResponse(code=f.code, severity=f.severity, description=f.description) for f in bundle.event_risk_flags
        ],
        data_quality=bundle.data_quality,
        earnings_available=bundle.earnings_available,
        earnings_reason=bundle.earnings_reason,
    )


@router.get("/{symbol}/research", response_model=BeginnerResearchResponse)
def get_research(symbol: str) -> BeginnerResearchResponse:
    try:
        symbol = validate_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        bundle = build_beginner_research(symbol, user_requested=True)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a research-bundle failure must surface as a clean HTTP error
        logger.error("Research bundle failed for %s: %s", symbol, exc)
        raise HTTPException(status_code=502, detail="Research data temporarily unavailable.") from exc

    if bundle is None:
        raise HTTPException(status_code=404, detail=f"No data found for symbol '{symbol}'.")

    # Compute the setup from the SAME TickerMetrics used for the narrative
    # (bundle.metrics), so the two are always numerically consistent, and
    # cache the pair together under one analysis_id. This is what lets
    # POST /api/research/snapshots save exactly what's shown here without
    # ever making a second Claude call.
    setup = compute_research_setup(bundle.metrics)
    analysis_id = analysis_cache.store(symbol, bundle, setup)

    return BeginnerResearchResponse(
        symbol=bundle.symbol,
        metrics=bundle.metrics,
        evidence=EvidenceResponse(
            bullish_pct=bundle.evidence.bullish_pct,
            neutral_pct=bundle.evidence.neutral_pct,
            bearish_pct=bundle.evidence.bearish_pct,
            insufficient_data=bundle.evidence.insufficient_data,
            available_categories=bundle.evidence.available_categories,
            unavailable_categories=bundle.evidence.unavailable_categories,
            breakdown=[
                EvidenceCategoryResponse(category=b.category, score=b.score, weight=b.weight)
                for b in bundle.evidence.breakdown
            ],
        ),
        research_view=bundle.research_view,
        sector=(
            SectorContextResponse(
                sector_name=bundle.sector.sector_name,
                sector_etf=bundle.sector.sector_etf,
                sector_pct_change=bundle.sector.sector_pct_change,
                peer_avg_pct_change=bundle.sector.peer_avg_pct_change,
                stock_vs_sector_pct=bundle.sector.stock_vs_sector_pct,
                market_proxy_symbol=bundle.sector.market_proxy_symbol,
                market_pct_change=bundle.sector.market_pct_change,
            )
            if bundle.sector is not None
            else None
        ),
        catalysts=[
            CatalystItemResponse(
                title=c.title,
                source=c.source,
                published_at=c.published_at,
                url=c.url,
                summary=c.summary,
                sentiment=c.sentiment,
                reason=c.reason,
            )
            for c in bundle.catalyst.items
        ],
        catalyst_note=bundle.catalyst.message or None,
        risk_flags=[RiskFlagResponse(code=f.code, severity=f.severity, description=f.description) for f in bundle.risk_flags],
        risk_explanation=bundle.risk_explanation,
        data_quality=DataQualityResponse(level=bundle.data_quality.level, explanation=bundle.data_quality.explanation),
        narrative=BeginnerNarrativeResponse(
            whats_happening=bundle.narrative.whats_happening,
            why=bundle.narrative.why,
            whats_good=bundle.narrative.whats_good,
            be_careful=bundle.narrative.be_careful,
            what_to_watch=bundle.narrative.what_to_watch,
        ),
        explanation_mode=config.EXPLANATION_MODE,
        events=events_bundle_to_response(bundle.events),
        analysis_id=analysis_id,
    )


@router.get("/{symbol}/research-setup", response_model=ResearchSetupResponse)
def get_research_setup(symbol: str) -> ResearchSetupResponse:
    try:
        symbol = validate_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        client = get_data_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    metrics = analyze_watchlist(client, [symbol])
    m = metrics.get(symbol)
    if m is None:
        raise HTTPException(status_code=404, detail=f"No data found for symbol '{symbol}'.")

    setup = compute_research_setup(m)
    return ResearchSetupResponse(
        symbol=setup.symbol,
        time_horizon=setup.time_horizon,
        entry_zone={"low": setup.entry_low, "high": setup.entry_high},
        invalidation_level=setup.invalidation_level,
        support=setup.support,
        resistance=setup.resistance,
        possible_targets=setup.possible_targets,
        risk_reward_ratio=setup.risk_reward_ratio,
        data_timestamp=datetime.now(timezone.utc),
    )


@router.get("/{symbol}/position-sizing", response_model=PositionSizingResponse)
def get_position_sizing(
    symbol: str,
    portfolio_value: float = Query(..., gt=0),
    entry_price: Optional[float] = Query(None),
    invalidation_price: Optional[float] = Query(None),
    max_risk_pct: Optional[float] = Query(None),
) -> PositionSizingResponse:
    try:
        symbol = validate_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        client = get_data_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    metrics = analyze_watchlist(client, [symbol])
    m = metrics.get(symbol)
    if m is None:
        raise HTTPException(status_code=404, detail=f"No data found for symbol '{symbol}'.")

    setup = compute_research_setup(m)
    resolved_entry = entry_price if entry_price is not None else (setup.entry_high or m.price)
    resolved_invalidation = invalidation_price if invalidation_price is not None else setup.invalidation_level

    if resolved_invalidation is None:
        raise HTTPException(
            status_code=422,
            detail="No invalidation level could be determined (no support found) — provide invalidation_price explicitly.",
        )

    resolved_max_risk = max_risk_pct if max_risk_pct is not None else config.MAX_RISK_PER_TRADE_PERCENT
    sizing = compute_position_sizing(portfolio_value, resolved_entry, resolved_invalidation, resolved_max_risk)

    if sizing.recommended_shares is not None:
        explanation = (
            f"With your configured {resolved_max_risk:g}% maximum risk per trade, this setup would risk no more "
            f"than ${sizing.max_loss_if_invalidated:,.2f} if the invalidation level (${resolved_invalidation:.2f}) is "
            f"reached, sizing to {sizing.recommended_shares} shares (${sizing.position_value:,.2f}, "
            f"{sizing.position_pct_of_portfolio:.1f}% of the portfolio value you provided)."
        )
    else:
        explanation = "Position sizing could not be computed — entry and invalidation price must differ."

    return PositionSizingResponse(
        portfolio_value=sizing.portfolio_value,
        entry_price=sizing.entry_price,
        invalidation_price=sizing.invalidation_price,
        max_risk_pct=sizing.max_risk_pct,
        risk_per_share=sizing.risk_per_share,
        max_risk_based_shares=sizing.max_risk_based_shares,
        max_allocation_based_shares=sizing.max_allocation_based_shares,
        recommended_shares=sizing.recommended_shares,
        position_value=sizing.position_value,
        position_pct_of_portfolio=sizing.position_pct_of_portfolio,
        max_loss_if_invalidated=sizing.max_loss_if_invalidated,
        explanation=explanation,
        position_adjustment_message=position_adjustment_unavailable_message(),
    )
