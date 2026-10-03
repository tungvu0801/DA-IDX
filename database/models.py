"""
database/models.py — Plain dataclasses mirroring the SQLite schema
(database/migrations.py). These are the shapes every repository method in
database/database.py takes and returns — never a raw sqlite3.Row, so
callers outside this package never need to know it's SQLite underneath.

Row identity: `id` is None for a not-yet-persisted ResearchSnapshot (before
INSERT); every read from the database has it populated.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class ResearchSnapshot:
    symbol: str
    created_at: datetime
    market_timestamp: str  # ISO8601 DATE ("day 0" for trading-day counting) — see note in outcome_tracker.py
    price: float
    price_source: str  # "latest_trade" | "daily_close"
    price_timestamp: datetime  # full-precision — may be intraday

    daily_change_pct: Optional[float]
    volume: Optional[float]
    relative_volume: Optional[float]

    rsi: Optional[float]
    ema_9: Optional[float]
    ema_20: Optional[float]
    ema_50: Optional[float]
    atr: Optional[float]
    volatility_pct: Optional[float]
    momentum_5d_pct: Optional[float]
    momentum_10d_pct: Optional[float]

    support: Optional[float]
    resistance: Optional[float]

    attention_score: Optional[int]
    scanner_signal: str

    evidence_technical: Optional[float]
    evidence_catalyst: Optional[float]
    evidence_risk: Optional[float]
    evidence_market: Optional[float]
    evidence_sector: Optional[float]

    bullish_pct: int
    neutral_pct: int
    bearish_pct: int
    research_view: str
    data_quality_level: str

    catalysts_json: str  # JSON list[{title, source, url, sentiment, published_at}]
    risk_flags_json: str  # JSON list[{code, severity, description}]

    sector_name: Optional[str]
    sector_etf: Optional[str]
    sector_pct_change: Optional[float]
    market_pct_change: Optional[float]

    setup_entry_low: Optional[float]
    setup_entry_high: Optional[float]
    setup_invalidation: Optional[float]
    setup_target_1: Optional[float]
    setup_target_2: Optional[float]
    setup_risk_reward_ratio: Optional[float]
    setup_time_horizon: Optional[str]

    narrative_json: Optional[str]  # JSON {whats_happening, why, whats_good, be_careful, what_to_watch}

    engine_version: str
    evidence_version: str
    prompt_version: Optional[str]
    llm_provider: Optional[str]
    llm_model: Optional[str]
    research_schema_version: str

    fingerprint: str

    # Stage 2.6 — additive, nullable. NULL on every snapshot saved before
    # this stage (and forever after, for those rows -- never backfilled;
    # see database/migrations.py). Populated at save time from whatever
    # services/event_context.build_event_context() returned AT THAT MOMENT.
    events_json: Optional[str] = None
    event_risk_level: Optional[str] = None
    event_data_quality: Optional[str] = None
    event_schema_version: Optional[str] = None

    id: Optional[int] = None


@dataclass
class ResearchOutcome:
    snapshot_id: int
    horizon_trading_days: int
    status: str = "PENDING"  # PENDING | COMPLETED | ERROR

    price_at_horizon: Optional[float] = None
    return_pct: Optional[float] = None
    highest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    max_favorable_excursion_pct: Optional[float] = None
    max_adverse_excursion_pct: Optional[float] = None

    did_hit_support: Optional[bool] = None
    did_break_support: Optional[bool] = None
    did_hit_resistance: Optional[bool] = None
    did_break_resistance: Optional[bool] = None
    did_hit_invalidation: Optional[bool] = None
    did_hit_target_1: Optional[bool] = None
    did_hit_target_2: Optional[bool] = None

    setup_entry_triggered: Optional[bool] = None
    invalidation_after_entry: Optional[bool] = None
    target_1_after_entry: Optional[bool] = None
    target_2_after_entry: Optional[bool] = None
    sequencing_ambiguous: bool = False

    outcome_schema_version: str = "1.0.0"
    error_message: Optional[str] = None
    updated_at: Optional[datetime] = None
    id: Optional[int] = None


@dataclass
class SnapshotWithOutcomes:
    """Convenience bundle returned by read endpoints — a snapshot plus all of its outcome rows."""

    snapshot: ResearchSnapshot
    outcomes: List[ResearchOutcome] = field(default_factory=list)
