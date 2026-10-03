"""
models/schemas.py — Pydantic response/request models for the FastAPI
backend. These are the API's public contract; they never expose API keys
or other secrets, and every field mirrors what analysis.indicators.TickerMetrics
already computes (or the true absence of a value, as None — nothing here
fabricates a metric that couldn't be computed).
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class TickerMetricsResponse(BaseModel):
    """Mirrors analysis.indicators.TickerMetrics for API responses."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    price: float
    price_source: str
    as_of: datetime

    prev_close: float
    pct_change: float
    gap_pct: Optional[float] = None

    volume: float
    avg_volume: Optional[float] = None
    relative_volume: Optional[float] = None
    volume_expansion: Optional[float] = None

    rsi: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_medium: Optional[float] = None
    ema_slow: Optional[float] = None
    trend: str

    atr: Optional[float] = None

    high_20d: Optional[float] = None
    low_20d: Optional[float] = None
    dist_from_high_pct: Optional[float] = None
    dist_from_low_pct: Optional[float] = None

    support: Optional[float] = None
    resistance: Optional[float] = None
    dist_from_support_pct: Optional[float] = None
    dist_from_resistance_pct: Optional[float] = None

    momentum_5d_pct: Optional[float] = None
    momentum_10d_pct: Optional[float] = None

    volatility_pct: Optional[float] = None
    volatility_expansion: Optional[float] = None

    momentum_score: int
    relative_volume_score: int
    trend_strength_score: int
    volatility_score: int
    attention_score: int

    signal: str


class MarketOverviewResponse(BaseModel):
    """Mirrors scanner.market_scanner.MarketOverview for API responses."""

    top_gainers: List[TickerMetricsResponse] = Field(default_factory=list)
    top_losers: List[TickerMetricsResponse] = Field(default_factory=list)
    unusual_volume: List[TickerMetricsResponse] = Field(default_factory=list)
    momentum_stocks: List[TickerMetricsResponse] = Field(default_factory=list)
    possible_breakouts: List[TickerMetricsResponse] = Field(default_factory=list)
    approaching_support: List[TickerMetricsResponse] = Field(default_factory=list)
    approaching_resistance: List[TickerMetricsResponse] = Field(default_factory=list)
    volatility_expansion: List[TickerMetricsResponse] = Field(default_factory=list)
    used_fallback_universe: bool
    scanned_symbol_count: int
    liquid_symbol_count: int


class WatchlistResponse(BaseModel):
    symbols: List[str]
    metrics: Dict[str, TickerMetricsResponse]


class WatchlistAddRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10, pattern=r"^[A-Za-z.\-]+$")


class AlternativeCandidate(BaseModel):
    """A market-scanner candidate shown as a "consider looking at" alternative."""

    symbol: str
    signal: str
    attention_score: int
    pct_change: float


class StockDetailResponse(BaseModel):
    metrics: TickerMetricsResponse
    alternatives: List[AlternativeCandidate] = Field(default_factory=list)


class TechnicalAnalysisResponse(BaseModel):
    symbol: str
    analysis: str
    ai_available: bool


class AlertResponse(BaseModel):
    symbol: str
    signal: str
    pct_change: float
    price: float
    attention_score: int
    message: str


class HealthResponse(BaseModel):
    status: str
    alpaca_configured: bool
    llm_configured: bool


class AIUsageResponse(BaseModel):
    """Backend-tracked AI usage for today (agents/usage_tracker.py)."""

    calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    estimated_cost_usd: float
    daily_call_limit: int
    calls_remaining_today: int
    # Stage 3.9: the top-level fields above are the RESEARCH budget (unchanged meaning for existing callers);
    # budgets holds both independent budgets: {"research": {...}, "explanation": {...}}.
    budgets: Optional[Dict[str, Any]] = None


class ResearchSetupResponse(BaseModel):
    """
    Populated by analysis/research_setup.py — every price level here is
    plain arithmetic from TickerMetrics.support/resistance/atr/price.
    Claude may explain these numbers; it never invents or adjusts one.
    """

    symbol: str
    time_horizon: str
    technical_summary: Optional[str] = None
    catalyst_summary: Optional[str] = None
    risk_summary: Optional[str] = None
    entry_zone: Dict[str, Optional[float]] = Field(default_factory=dict)
    invalidation_level: Optional[float] = None
    support: List[float] = Field(default_factory=list)
    resistance: List[float] = Field(default_factory=list)
    possible_targets: List[float] = Field(default_factory=list)
    risk_factors: List[str] = Field(default_factory=list)
    risk_reward_ratio: Optional[float] = None
    data_timestamp: datetime


class PositionSizingRequest(BaseModel):
    portfolio_value: float = Field(..., gt=0)
    entry_price: Optional[float] = None  # defaults to the research-setup entry if omitted
    invalidation_price: Optional[float] = None  # defaults to the research-setup invalidation if omitted
    max_risk_pct: Optional[float] = None  # defaults to config.MAX_RISK_PER_TRADE_PERCENT if omitted


class PositionSizingResponse(BaseModel):
    portfolio_value: float
    entry_price: float
    invalidation_price: float
    max_risk_pct: float
    risk_per_share: Optional[float] = None
    max_risk_based_shares: Optional[int] = None
    max_allocation_based_shares: Optional[int] = None
    recommended_shares: Optional[int] = None
    position_value: Optional[float] = None
    position_pct_of_portfolio: Optional[float] = None
    max_loss_if_invalidated: Optional[float] = None
    explanation: str
    position_adjustment_message: str  # always the fixed "unavailable" message — see analysis/position_sizing.py


class CatalystItemResponse(BaseModel):
    title: str
    source: str
    published_at: Optional[str] = None
    url: str
    summary: str
    sentiment: str  # POSITIVE | NEUTRAL | NEGATIVE | UNCERTAIN
    reason: str


class RiskFlagResponse(BaseModel):
    code: str
    severity: str  # HIGH | MEDIUM | LOW
    description: str


class EvidenceCategoryResponse(BaseModel):
    category: str
    score: Optional[float] = None
    weight: float


class EvidenceResponse(BaseModel):
    bullish_pct: int
    neutral_pct: int
    bearish_pct: int
    insufficient_data: bool
    available_categories: List[str]
    unavailable_categories: List[str]
    breakdown: List[EvidenceCategoryResponse]
    disclaimer: str = (
        "These percentages summarize current evidence. They are not probabilities of future returns."
    )


class SectorContextResponse(BaseModel):
    sector_name: str
    sector_etf: str
    sector_pct_change: Optional[float] = None
    peer_avg_pct_change: Optional[float] = None
    stock_vs_sector_pct: Optional[float] = None
    market_proxy_symbol: str
    market_pct_change: Optional[float] = None


class BeginnerNarrativeResponse(BaseModel):
    whats_happening: str
    why: str
    whats_good: List[str]
    be_careful: List[str]
    what_to_watch: str


class DataQualityResponse(BaseModel):
    level: str  # HIGH | MEDIUM | LOW
    explanation: str


class EventItemResponse(BaseModel):
    """
    Mirrors data.events.models.NormalizedEvent. Every field here traces to
    a real source (see EventsResponse.data_quality and each event's own
    `source`/`source_url`/`confirmed`) -- Claude only ever explains these,
    never invents one.
    """

    event_id: str
    event_type: str
    category: str  # COMPANY | MACRO
    symbol: Optional[str] = None
    title: str
    event_date: str  # ISO date
    event_time: Optional[str] = None
    timezone: Optional[str] = None
    event_datetime_utc: Optional[datetime] = None
    time_precision: str  # EXACT | DATE_ONLY
    source: str
    source_url: Optional[str] = None
    source_provider: str
    status: str  # UPCOMING | RELEASED | CANCELLED | UNKNOWN
    importance: str  # HIGH | MEDIUM | LOW
    confirmed: bool
    retrieved_at: datetime
    raw_reference_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    actual: Optional[str] = None
    consensus: Optional[str] = None
    previous: Optional[str] = None


class EventsResponse(BaseModel):
    """
    Stage 2.6 — real event context for a symbol. event_risk_level is
    computed entirely in Python (analysis/event_risk.py) from real event
    dates; Claude never assigns it.
    """

    company_events: List[EventItemResponse] = Field(default_factory=list)
    macro_events: List[EventItemResponse] = Field(default_factory=list)
    nearest_event: Optional[EventItemResponse] = None
    event_risk_level: str = "NONE"  # HIGH | MEDIUM | LOW | NONE
    event_risk_flags: List[RiskFlagResponse] = Field(default_factory=list)
    data_quality: str  # HIGH | MEDIUM | LOW
    earnings_available: bool
    earnings_reason: Optional[str] = None


class BeginnerResearchResponse(BaseModel):
    """The full beginner research bundle — GET /api/stocks/{symbol}/research."""

    symbol: str
    metrics: TickerMetricsResponse
    evidence: EvidenceResponse
    research_view: str
    research_view_disclaimer: str = (
        "This is a research summary, not an instruction to buy, sell, or hold."
    )
    sector: Optional[SectorContextResponse] = None
    catalysts: List[CatalystItemResponse] = Field(default_factory=list)
    catalyst_note: Optional[str] = None
    risk_flags: List[RiskFlagResponse] = Field(default_factory=list)
    risk_explanation: str
    data_quality: DataQualityResponse
    narrative: BeginnerNarrativeResponse
    explanation_mode: str = "BEGINNER"
    events: Optional[EventsResponse] = None
    analysis_id: str  # pass this to POST /api/research/snapshots to save EXACTLY this analysis, no new Claude call


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)


class ChatToolCallResponse(BaseModel):
    tool: str
    input: Dict
    available: bool


class ChatResponse(BaseModel):
    answer: str
    tool_calls: List[ChatToolCallResponse] = Field(default_factory=list)
    ai_available: bool


# ==========================================================================
# STAGE 2.5 — Research history, outcome tracking, and performance analytics.
# ==========================================================================
class SaveSnapshotRequest(BaseModel):
    """POST /api/research/snapshots — saves EXACTLY the analysis behind `analysis_id`, no new Claude call."""

    analysis_id: str
    symbol: str


class SaveSnapshotResponse(BaseModel):
    id: int
    created: bool  # False means a fingerprint-identical snapshot already existed
    message: str


class ResearchOutcomeResponse(BaseModel):
    horizon_trading_days: int
    status: str  # PENDING | COMPLETED | ERROR
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
    error_message: Optional[str] = None


class ResearchSnapshotResponse(BaseModel):
    """A fully immutable stored snapshot — "what the agent knew and concluded at this moment." Never mixed with live data."""

    id: int
    symbol: str
    created_at: datetime
    market_timestamp: str
    price: float
    price_source: str
    price_timestamp: datetime
    daily_change_pct: Optional[float] = None
    volume: Optional[float] = None
    relative_volume: Optional[float] = None
    rsi: Optional[float] = None
    ema_9: Optional[float] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    atr: Optional[float] = None
    volatility_pct: Optional[float] = None
    momentum_5d_pct: Optional[float] = None
    momentum_10d_pct: Optional[float] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    attention_score: Optional[int] = None
    scanner_signal: str
    evidence_technical: Optional[float] = None
    evidence_catalyst: Optional[float] = None
    evidence_risk: Optional[float] = None
    evidence_market: Optional[float] = None
    evidence_sector: Optional[float] = None
    bullish_pct: int
    neutral_pct: int
    bearish_pct: int
    research_view: str
    data_quality_level: str
    catalysts: List[Dict] = Field(default_factory=list)
    risk_flags: List[Dict] = Field(default_factory=list)
    sector_name: Optional[str] = None
    sector_etf: Optional[str] = None
    sector_pct_change: Optional[float] = None
    market_pct_change: Optional[float] = None
    setup_entry_low: Optional[float] = None
    setup_entry_high: Optional[float] = None
    setup_invalidation: Optional[float] = None
    setup_target_1: Optional[float] = None
    setup_target_2: Optional[float] = None
    setup_risk_reward_ratio: Optional[float] = None
    setup_time_horizon: Optional[str] = None
    narrative: Optional[Dict] = None
    engine_version: str
    evidence_version: str
    prompt_version: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    research_schema_version: str
    historical_validation: bool = False
    events: Optional[EventsResponse] = None  # None for a pre-Stage-2.6 snapshot -- never backfilled
    outcomes: List[ResearchOutcomeResponse] = Field(default_factory=list)


class OutcomeUpdateResponse(BaseModel):
    snapshots_checked: int
    outcomes_updated: int
    still_pending: int
    errors: List[str] = Field(default_factory=list)


class GroupStatsResponse(BaseModel):
    label: str
    n: int
    avg_return_pct: Optional[float] = None
    median_return_pct: Optional[float] = None
    positive_return_frequency: Optional[float] = None
    avg_mfe_pct: Optional[float] = None
    avg_mae_pct: Optional[float] = None
    target_1_hit_rate: Optional[float] = None
    invalidation_hit_rate: Optional[float] = None
    sample_warning: Optional[str] = None


class PerformanceResponse(BaseModel):
    total_snapshots: int
    completed_snapshots: int
    pending_snapshots: int
    data_issues: int
    overall_by_horizon: Dict[int, GroupStatsResponse]
    evidence_buckets_by_horizon: Dict[int, List[GroupStatsResponse]]
    research_view_by_horizon: Dict[int, List[GroupStatsResponse]]
    catalyst_presence_by_horizon: Dict[int, List[GroupStatsResponse]]
    catalyst_sentiment_by_horizon: Dict[int, List[GroupStatsResponse]]
    risk_flag_presence_by_horizon: Dict[int, List[GroupStatsResponse]]
    disclaimer: str = (
        "These are historical observations of this agent's own saved research, not a probability of "
        "future returns and not investment advice. Small-sample groups are explicitly flagged above."
    )
