"""
services/snapshot_builder.py — Flattens an already-generated
(BeginnerResearch, ResearchSetup) pair into an immutable ResearchSnapshot
row and persists it.

This module never calls Claude and never fetches market data — everything
it needs is already computed and handed to it by the caller. It is the one
place `agents.beginner_agent.BeginnerResearch` gets translated into the
database row shape, so that translation only exists once.
"""
import json
from datetime import datetime, timezone
from typing import Optional, Tuple

import config
from agents.beginner_agent import BeginnerResearch
from analysis.evidence_scoring import EvidenceResult
from analysis.research_setup import ResearchSetup
from database.database import get_db
from database.fingerprint import compute_fingerprint
from database.models import ResearchSnapshot


def _score_for(evidence: EvidenceResult, category: str) -> Optional[float]:
    for b in evidence.breakdown:
        if b.category == category:
            return b.score
    return None


def _target(setup: Optional[ResearchSetup], index: int) -> Optional[float]:
    if setup is None or len(setup.possible_targets) <= index:
        return None
    return setup.possible_targets[index]


def _serialize_event(e) -> dict:
    return {
        "event_id": e.event_id,
        "event_type": e.event_type.value,
        "category": e.category.value,
        "symbol": e.symbol,
        "title": e.title,
        "event_date": e.event_date.isoformat(),
        "event_time": e.event_time.isoformat() if e.event_time is not None else None,
        "timezone": e.timezone,
        "event_datetime_utc": e.event_datetime_utc.isoformat() if e.event_datetime_utc is not None else None,
        "time_precision": e.time_precision.value,
        "source": e.source,
        "source_url": e.source_url,
        "source_provider": e.source_provider,
        "status": e.status.value,
        "importance": e.importance.value,
        "confirmed": e.confirmed,
        "retrieved_at": e.retrieved_at.isoformat(),
        "raw_reference_id": e.raw_reference_id,
        "metadata": e.metadata,
        "actual": e.actual,
        "consensus": e.consensus,
        "previous": e.previous,
    }


def _event_fingerprint_identifier(e) -> str:
    """Stable identity for fingerprinting -- deliberately excludes retrieved_at/metadata (volatile)."""
    return f"{e.event_type.value}|{e.event_date.isoformat()}|{e.symbol or ''}|{e.source_provider}|{e.raw_reference_id or ''}"


def save_snapshot_from_bundle(
    symbol: str, bundle: BeginnerResearch, setup: Optional[ResearchSetup]
) -> Tuple[int, bool]:
    """
    Persist `bundle`/`setup` verbatim as a new immutable snapshot (or
    return the id of an identical existing one — see database/fingerprint.py).
    On first creation, also inserts PENDING outcome rows for every
    configured horizon (config.TRADING_DAY_HORIZONS).

    Returns (snapshot_id, created) — created=False means a fingerprint-
    identical snapshot already existed and nothing new was inserted.
    """
    m = bundle.metrics
    market_timestamp = m.as_of.date().isoformat() if hasattr(m.as_of, "date") else str(m.as_of)[:10]

    catalyst_identifiers = [f"{c.source}|{c.title}|{c.published_at}" for c in bundle.catalyst.items]
    risk_flag_codes = [f.code for f in bundle.risk_flags]
    setup_levels = (
        {
            "entry_low": setup.entry_low,
            "entry_high": setup.entry_high,
            "invalidation": setup.invalidation_level,
            "target_1": _target(setup, 0),
            "target_2": _target(setup, 1),
        }
        if setup is not None
        else None
    )

    all_events = []
    if bundle.events is not None:
        all_events = list(bundle.events.company_events) + list(bundle.events.macro_events)
    event_identifiers = [_event_fingerprint_identifier(e) for e in all_events]
    event_risk_level = bundle.events.event_risk_level if bundle.events is not None else None

    fingerprint = compute_fingerprint(
        symbol=symbol,
        market_timestamp=market_timestamp,
        price_source=m.price_source,
        price=m.price,
        attention_score=m.attention_score,
        research_view=bundle.research_view,
        bullish_pct=bundle.evidence.bullish_pct,
        neutral_pct=bundle.evidence.neutral_pct,
        bearish_pct=bundle.evidence.bearish_pct,
        catalyst_identifiers=catalyst_identifiers,
        risk_flag_codes=risk_flag_codes,
        setup_levels=setup_levels,
        event_identifiers=event_identifiers,
        event_risk_level=event_risk_level,
    )

    catalysts_json = json.dumps(
        [
            {"title": c.title, "source": c.source, "url": c.url, "sentiment": c.sentiment, "published_at": c.published_at}
            for c in bundle.catalyst.items
        ]
    )
    risk_flags_json = json.dumps(
        [{"code": f.code, "severity": f.severity, "description": f.description} for f in bundle.risk_flags]
    )
    narrative_json = json.dumps(
        {
            "whats_happening": bundle.narrative.whats_happening,
            "why": bundle.narrative.why,
            "whats_good": bundle.narrative.whats_good,
            "be_careful": bundle.narrative.be_careful,
            "what_to_watch": bundle.narrative.what_to_watch,
        }
    )

    # Stage 2.6 — freezes EXACTLY the event context already computed above
    # (same `bundle.events` used for the fingerprint), never re-fetched.
    # None/blank for a pre-Stage-2.6 caller or when the event layer itself
    # failed -- never backfilled later.
    events_json = json.dumps([_serialize_event(e) for e in all_events]) if bundle.events is not None else None
    event_data_quality = bundle.events.data_quality if bundle.events is not None else None

    llm_provider = "anthropic" if config.has_llm_credentials() else None
    llm_model = config.ANTHROPIC_MODEL if config.has_llm_credentials() else None

    snapshot = ResearchSnapshot(
        symbol=symbol.strip().upper(),
        created_at=datetime.now(timezone.utc),
        market_timestamp=market_timestamp,
        price=m.price,
        price_source=m.price_source,
        price_timestamp=m.as_of,
        daily_change_pct=m.pct_change,
        volume=m.volume,
        relative_volume=m.relative_volume,
        rsi=m.rsi,
        ema_9=m.ema_fast,
        ema_20=m.ema_medium,
        ema_50=m.ema_slow,
        atr=m.atr,
        volatility_pct=m.volatility_pct,
        momentum_5d_pct=m.momentum_5d_pct,
        momentum_10d_pct=m.momentum_10d_pct,
        support=m.support,
        resistance=m.resistance,
        attention_score=m.attention_score,
        scanner_signal=m.signal,
        evidence_technical=_score_for(bundle.evidence, "technical"),
        evidence_catalyst=_score_for(bundle.evidence, "catalyst"),
        evidence_risk=_score_for(bundle.evidence, "risk"),
        evidence_market=_score_for(bundle.evidence, "market"),
        evidence_sector=_score_for(bundle.evidence, "sector"),
        bullish_pct=bundle.evidence.bullish_pct,
        neutral_pct=bundle.evidence.neutral_pct,
        bearish_pct=bundle.evidence.bearish_pct,
        research_view=bundle.research_view,
        data_quality_level=bundle.data_quality.level,
        catalysts_json=catalysts_json,
        risk_flags_json=risk_flags_json,
        sector_name=bundle.sector.sector_name if bundle.sector else None,
        sector_etf=bundle.sector.sector_etf if bundle.sector else None,
        sector_pct_change=bundle.sector.sector_pct_change if bundle.sector else None,
        market_pct_change=bundle.sector.market_pct_change if bundle.sector else None,
        setup_entry_low=setup.entry_low if setup else None,
        setup_entry_high=setup.entry_high if setup else None,
        setup_invalidation=setup.invalidation_level if setup else None,
        setup_target_1=_target(setup, 0),
        setup_target_2=_target(setup, 1),
        setup_risk_reward_ratio=setup.risk_reward_ratio if setup else None,
        setup_time_horizon=setup.time_horizon if setup else None,
        narrative_json=narrative_json,
        engine_version=config.ENGINE_VERSION,
        evidence_version=config.EVIDENCE_VERSION,
        prompt_version=config.PROMPT_VERSION,
        llm_provider=llm_provider,
        llm_model=llm_model,
        research_schema_version=config.RESEARCH_SCHEMA_VERSION,
        fingerprint=fingerprint,
        events_json=events_json,
        event_risk_level=event_risk_level,
        event_data_quality=event_data_quality,
        event_schema_version=config.EVENT_SCHEMA_VERSION if bundle.events is not None else None,
    )

    db = get_db()
    snapshot_id, created = db.save_snapshot(snapshot)
    if created:
        db.create_pending_outcomes(snapshot_id, config.TRADING_DAY_HORIZONS)
    return snapshot_id, created
