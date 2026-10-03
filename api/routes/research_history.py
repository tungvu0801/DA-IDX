"""
api/routes/research_history.py — Research snapshot persistence, outcome
tracking, and performance analytics (Stage 2.5).

POST   /api/research/snapshots           save exactly an already-generated analysis (no new Claude call)
GET    /api/research/snapshots           list saved snapshots (filters: symbol, research_view, date range, data_quality)
GET    /api/research/snapshots/{id}      one immutable stored snapshot + its outcomes — never mixed with live data
DELETE /api/research/snapshots/{id}      cleanup only; no edit endpoint exists anywhere (snapshots are immutable)
POST   /api/research/outcomes/update     run the outcome tracker for all eligible pending rows (never calls Claude)
POST   /api/research/outcomes/recompute  maintenance: re-derive all COMPLETED outcome rows (e.g. after a formula
                                          fix) from real bars, never touching research_snapshots (never calls Claude)
GET    /api/research/performance         deterministic aggregate analytics over completed outcomes
"""
import json
import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import config
from data.events.earnings import EARNINGS_UNAVAILABLE_REASON
from database.database import get_db
from models.schemas import (
    EventItemResponse,
    EventsResponse,
    GroupStatsResponse,
    OutcomeUpdateResponse,
    PerformanceResponse,
    ResearchOutcomeResponse,
    ResearchSnapshotResponse,
    RiskFlagResponse,
    SaveSnapshotRequest,
    SaveSnapshotResponse,
)
from services.analysis_cache import analysis_cache
from services.analytics import GroupStats, compute_performance_report
from services.outcome_tracker import recompute_completed_outcomes, update_pending_outcomes
from services.snapshot_builder import save_snapshot_from_bundle

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/research", tags=["research-history"])


_EVENT_RISK_FLAG_CODES = {
    "EVENT_RISK_HIGH", "EVENT_RISK_MEDIUM", "MACRO_EVENT_IMMINENT",
    "EARNINGS_EVENT_IMMINENT", "CORPORATE_ACTION_IMMINENT", "EARNINGS_DATA_UNAVAILABLE",
}


def _stored_event_to_response(raw: dict) -> Optional[EventItemResponse]:
    try:
        return EventItemResponse(
            event_id=raw["event_id"],
            event_type=raw["event_type"],
            category=raw["category"],
            symbol=raw.get("symbol"),
            title=raw["title"],
            event_date=raw["event_date"],
            event_time=raw.get("event_time"),
            timezone=raw.get("timezone"),
            event_datetime_utc=raw.get("event_datetime_utc"),
            time_precision=raw["time_precision"],
            source=raw["source"],
            source_url=raw.get("source_url"),
            source_provider=raw["source_provider"],
            status=raw["status"],
            importance=raw["importance"],
            confirmed=raw["confirmed"],
            retrieved_at=raw["retrieved_at"],
            raw_reference_id=raw.get("raw_reference_id"),
            metadata=raw.get("metadata") or {},
            actual=raw.get("actual"),
            consensus=raw.get("consensus"),
            previous=raw.get("previous"),
        )
    except (KeyError, TypeError):
        return None


def _events_response_from_snapshot(s, risk_flags: list) -> Optional[EventsResponse]:
    """
    Rebuilds an EventsResponse from a snapshot's FROZEN events_json (never
    re-fetched, never re-derived) -- None for a pre-Stage-2.6 snapshot,
    where events_json/event_risk_level/event_data_quality are all NULL.
    """
    if not s.events_json:
        return None
    try:
        raw_events = json.loads(s.events_json)
    except (json.JSONDecodeError, TypeError):
        return None

    events = [ev for ev in (_stored_event_to_response(e) for e in raw_events) if ev is not None]
    company_events = [e for e in events if e.category == "COMPANY"]
    macro_events = [e for e in events if e.category == "MACRO"]
    # EARNINGS_DATA_UNAVAILABLE is a data-quality caveat, never a scored risk
    # flag (bug fixed in Stage 2.6.1) -- current saves never emit it here,
    # but an old snapshot saved before the fix may still have it stored in
    # risk_flags_json (left untouched, per immutability), so it's excluded
    # from this reconstructed view either way, on both old and new rows.
    event_flags = [f for f in risk_flags if f.code in _EVENT_RISK_FLAG_CODES and f.code != "EARNINGS_DATA_UNAVAILABLE"]
    has_earnings_event = any(e.event_type == "EARNINGS" for e in events)

    return EventsResponse(
        company_events=company_events,
        macro_events=macro_events,
        nearest_event=None,  # a single "nearest" isn't meaningful for a frozen historical record
        event_risk_level=s.event_risk_level or "NONE",
        event_risk_flags=event_flags,
        data_quality=s.event_data_quality or "LOW",
        earnings_available=has_earnings_event,
        earnings_reason=None if has_earnings_event else EARNINGS_UNAVAILABLE_REASON,
    )


def _is_historical_validation(s) -> bool:
    """
    Presentation-only signal that this snapshot's market data predates when
    it was saved by more than a normal weekend/holiday gap (e.g. a
    deliberately backdated validation record). Computed at read-time from
    two already-stored, immutable fields -- never persisted, never affects
    research_view/evidence/scoring/fingerprint.
    """
    try:
        market_date = date.fromisoformat(s.market_timestamp)
    except (TypeError, ValueError):
        return False
    return (s.created_at.date() - market_date).days >= config.HISTORICAL_VALIDATION_GAP_DAYS


def _outcome_to_response(o) -> ResearchOutcomeResponse:
    return ResearchOutcomeResponse(
        horizon_trading_days=o.horizon_trading_days,
        status=o.status,
        price_at_horizon=o.price_at_horizon,
        return_pct=o.return_pct,
        highest_price=o.highest_price,
        lowest_price=o.lowest_price,
        max_favorable_excursion_pct=o.max_favorable_excursion_pct,
        max_adverse_excursion_pct=o.max_adverse_excursion_pct,
        did_hit_support=o.did_hit_support,
        did_break_support=o.did_break_support,
        did_hit_resistance=o.did_hit_resistance,
        did_break_resistance=o.did_break_resistance,
        did_hit_invalidation=o.did_hit_invalidation,
        did_hit_target_1=o.did_hit_target_1,
        did_hit_target_2=o.did_hit_target_2,
        setup_entry_triggered=o.setup_entry_triggered,
        invalidation_after_entry=o.invalidation_after_entry,
        target_1_after_entry=o.target_1_after_entry,
        target_2_after_entry=o.target_2_after_entry,
        sequencing_ambiguous=o.sequencing_ambiguous,
        error_message=o.error_message,
    )


def _snapshot_to_response(s, outcomes) -> ResearchSnapshotResponse:
    try:
        catalysts = json.loads(s.catalysts_json) if s.catalysts_json else []
    except (json.JSONDecodeError, TypeError):
        catalysts = []
    try:
        risk_flags = json.loads(s.risk_flags_json) if s.risk_flags_json else []
    except (json.JSONDecodeError, TypeError):
        risk_flags = []
    try:
        narrative = json.loads(s.narrative_json) if s.narrative_json else None
    except (json.JSONDecodeError, TypeError):
        narrative = None

    risk_flags_for_events = [RiskFlagResponse(code=f["code"], severity=f["severity"], description=f["description"]) for f in risk_flags]

    return ResearchSnapshotResponse(
        id=s.id,
        symbol=s.symbol,
        created_at=s.created_at,
        market_timestamp=s.market_timestamp,
        price=s.price,
        price_source=s.price_source,
        price_timestamp=s.price_timestamp,
        daily_change_pct=s.daily_change_pct,
        volume=s.volume,
        relative_volume=s.relative_volume,
        rsi=s.rsi,
        ema_9=s.ema_9,
        ema_20=s.ema_20,
        ema_50=s.ema_50,
        atr=s.atr,
        volatility_pct=s.volatility_pct,
        momentum_5d_pct=s.momentum_5d_pct,
        momentum_10d_pct=s.momentum_10d_pct,
        support=s.support,
        resistance=s.resistance,
        attention_score=s.attention_score,
        scanner_signal=s.scanner_signal,
        evidence_technical=s.evidence_technical,
        evidence_catalyst=s.evidence_catalyst,
        evidence_risk=s.evidence_risk,
        evidence_market=s.evidence_market,
        evidence_sector=s.evidence_sector,
        bullish_pct=s.bullish_pct,
        neutral_pct=s.neutral_pct,
        bearish_pct=s.bearish_pct,
        research_view=s.research_view,
        data_quality_level=s.data_quality_level,
        catalysts=catalysts,
        risk_flags=risk_flags,
        sector_name=s.sector_name,
        sector_etf=s.sector_etf,
        sector_pct_change=s.sector_pct_change,
        market_pct_change=s.market_pct_change,
        setup_entry_low=s.setup_entry_low,
        setup_entry_high=s.setup_entry_high,
        setup_invalidation=s.setup_invalidation,
        setup_target_1=s.setup_target_1,
        setup_target_2=s.setup_target_2,
        setup_risk_reward_ratio=s.setup_risk_reward_ratio,
        setup_time_horizon=s.setup_time_horizon,
        narrative=narrative,
        engine_version=s.engine_version,
        evidence_version=s.evidence_version,
        prompt_version=s.prompt_version,
        llm_provider=s.llm_provider,
        llm_model=s.llm_model,
        research_schema_version=s.research_schema_version,
        historical_validation=_is_historical_validation(s),
        events=_events_response_from_snapshot(s, risk_flags_for_events),
        outcomes=[_outcome_to_response(o) for o in outcomes],
    )


def _group_to_response(g: GroupStats) -> GroupStatsResponse:
    return GroupStatsResponse(
        label=g.label,
        n=g.n,
        avg_return_pct=g.avg_return_pct,
        median_return_pct=g.median_return_pct,
        positive_return_frequency=g.positive_return_frequency,
        avg_mfe_pct=g.avg_mfe_pct,
        avg_mae_pct=g.avg_mae_pct,
        target_1_hit_rate=g.target_1_hit_rate,
        invalidation_hit_rate=g.invalidation_hit_rate,
        sample_warning=g.sample_warning,
    )


@router.post("/snapshots", response_model=SaveSnapshotResponse)
def post_snapshot(body: SaveSnapshotRequest) -> SaveSnapshotResponse:
    cached = analysis_cache.get(body.analysis_id)
    if cached is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "This analysis has expired or wasn't found — reload the research view "
                "(GET /api/stocks/{symbol}/research) and save again."
            ),
        )
    if cached.symbol.strip().upper() != body.symbol.strip().upper():
        raise HTTPException(status_code=400, detail="analysis_id does not match the given symbol.")

    try:
        snapshot_id, created = save_snapshot_from_bundle(cached.symbol, cached.bundle, cached.setup)
    except Exception as exc:  # noqa: BLE001 - a persistence failure must be a clear error, not a 500
        logger.error("Failed to save snapshot for %s: %s", cached.symbol, exc)
        raise HTTPException(status_code=503, detail=f"Could not save snapshot: {exc}") from exc

    return SaveSnapshotResponse(
        id=snapshot_id,
        created=created,
        message="Snapshot saved." if created else "An identical snapshot was already saved.",
    )


@router.get("/snapshots", response_model=list[ResearchSnapshotResponse])
def list_snapshots(
    symbol: Optional[str] = Query(None),
    research_view: Optional[str] = Query(None),
    data_quality: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
) -> list:
    db = get_db()
    snapshots = db.list_snapshots(
        symbol=symbol,
        research_view=research_view,
        data_quality=data_quality,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    results = []
    for s in snapshots:
        outcomes = db.get_outcomes_for_snapshot(s.id)
        results.append(_snapshot_to_response(s, outcomes))
    return results


@router.get("/snapshots/{snapshot_id}", response_model=ResearchSnapshotResponse)
def get_snapshot(snapshot_id: int) -> ResearchSnapshotResponse:
    db = get_db()
    snapshot = db.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"No snapshot found with id {snapshot_id}.")
    outcomes = db.get_outcomes_for_snapshot(snapshot_id)
    return _snapshot_to_response(snapshot, outcomes)


@router.delete("/snapshots/{snapshot_id}")
def delete_snapshot(snapshot_id: int) -> dict:
    db = get_db()
    deleted = db.delete_snapshot(snapshot_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No snapshot found with id {snapshot_id}.")
    return {"deleted": True, "id": snapshot_id}


@router.post("/outcomes/update", response_model=OutcomeUpdateResponse)
def post_outcomes_update() -> OutcomeUpdateResponse:
    """Runs the outcome tracker now for every eligible pending horizon. Never calls Claude."""
    summary = update_pending_outcomes()
    return OutcomeUpdateResponse(
        snapshots_checked=summary.snapshots_checked,
        outcomes_updated=summary.outcomes_updated,
        still_pending=summary.still_pending,
        errors=summary.errors,
    )


@router.post("/outcomes/recompute", response_model=OutcomeUpdateResponse)
def post_outcomes_recompute() -> OutcomeUpdateResponse:
    """
    Maintenance endpoint: re-derives every COMPLETED outcome's stored
    fields (e.g. after a formula fix like the MFE/MAE clamp) from real
    historical bars. Never calls Claude; never modifies research_snapshots.
    """
    summary = recompute_completed_outcomes()
    return OutcomeUpdateResponse(
        snapshots_checked=summary.snapshots_checked,
        outcomes_updated=summary.outcomes_updated,
        still_pending=summary.still_pending,
        errors=summary.errors,
    )


@router.get("/performance", response_model=PerformanceResponse)
def get_performance() -> PerformanceResponse:
    report = compute_performance_report()
    return PerformanceResponse(
        total_snapshots=report.total_snapshots,
        completed_snapshots=report.completed_snapshots,
        pending_snapshots=report.pending_snapshots,
        data_issues=report.data_issues,
        overall_by_horizon={h: _group_to_response(g) for h, g in report.overall_by_horizon.items()},
        evidence_buckets_by_horizon={
            h: [_group_to_response(g) for g in groups] for h, groups in report.evidence_buckets_by_horizon.items()
        },
        research_view_by_horizon={
            h: [_group_to_response(g) for g in groups] for h, groups in report.research_view_by_horizon.items()
        },
        catalyst_presence_by_horizon={
            h: [_group_to_response(g) for g in groups] for h, groups in report.catalyst_presence_by_horizon.items()
        },
        catalyst_sentiment_by_horizon={
            h: [_group_to_response(g) for g in groups] for h, groups in report.catalyst_sentiment_by_horizon.items()
        },
        risk_flag_presence_by_horizon={
            h: [_group_to_response(g) for g in groups] for h, groups in report.risk_flag_presence_by_horizon.items()
        },
    )
