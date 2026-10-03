"""
strategy/features.py — the ONE deterministic feature registry for Stage 3 strategies.

Every strategy condition references a feature_id defined here. A feature never has its own maths: each one names
the EXISTING calculation it reuses (analysis.indicators.compute_metrics, insights.labels, insights.market,
analysis.sector_context / evidence_scoring, insights.research, services.event_context), and the extractors below
only call those functions or read the fields they already produce.

Historical support is declared explicitly and conservatively:
  * STOCK / MARKET / SECTOR features are derived only from daily bars, and analysis.indicators never reads past the
    last bar it is given — so they can be rebuilt at a past date T from bars through T.
  * RESEARCH (Claude-assisted research), EVENT (provider calendars) and PORTFOLIO (live Robinhood state) cannot be
    proven to be what was known at a past date, so they are NOT historical. Portfolio features are not forward-
    capable either: live account state must not drive a strategy (a future paper portfolio will define them).

Feature meaning depends on a few configurable thresholds (e.g. the "extended" momentum %). Each feature records the
parameter values it uses; the registry FINGERPRINT (SHA-256 over every definition and parameter) is pinned inside
every saved strategy version, so a later threshold change is detected instead of silently changing a strategy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from analysis import signals

REGISTRY_VERSION = 1

NUMBER, ENUM, BOOLEAN = "NUMBER", "ENUM", "BOOLEAN"
NUMBER_OPS = (">", ">=", "<", "<=", "==", "between")
ENUM_OPS = ("==", "!=", "in", "not_in")
BOOLEAN_OPS = ("is_true", "is_false")
OPS_BY_TYPE = {NUMBER: NUMBER_OPS, ENUM: ENUM_OPS, BOOLEAN: BOOLEAN_OPS}
SCOPES = ("STOCK", "MARKET", "SECTOR", "RESEARCH", "EVENT", "PORTFOLIO")

PIT_BARS = ("Computed from daily bars through the close of the evaluation day only (analysis.indicators never reads "
            "past the last bar it is given). Price is that day's close — never a later or intraday trade.")
PIT_MARKET = ("Computed from SPY / QQQ / SOXX and the fixed breadth-list daily bars through the evaluation day. The "
              "macro-event, news and scanner inputs of insights.market do not affect this field.")
PIT_SECTOR = ("Computed from the sector ETF's (and market proxy's) daily bars through the evaluation day. The "
              "stock-to-sector grouping is the app's current static sector map.")
PIT_RESEARCH = ("Saved research is produced with Claude at the time it is run and cannot be reconstructed for a past "
                "date. Forward use needs research that existed at the signal time.")
PIT_EVENT = ("Event calendars are fetched as they are today; the providers cannot prove what was known at a past "
             "date, including later revisions.")
PIT_PORTFOLIO = ("Live Robinhood account state. It must not drive a historical or forward strategy; a future paper "
                 "portfolio will define its own state.")


@dataclass(frozen=True)
class Feature:
    feature_id: str
    beginner_name: str
    description: str
    scope: str
    data_type: str
    unit: Optional[str]
    source: str
    historical_support: bool
    forward_support: bool
    point_in_time: str
    notes: str = ""
    values: Tuple[Tuple[str, str], ...] = ()          # ENUM: (value, beginner label)
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    parameters: Tuple[Tuple[str, Any], ...] = ()       # config values that define this feature
    true_text: str = ""                                # BOOLEAN phrasing
    false_text: str = ""

    @property
    def allowed_operators(self) -> Tuple[str, ...]:
        return OPS_BY_TYPE[self.data_type]

    def public(self) -> dict:
        d = asdict(self)
        d["allowed_operators"] = list(self.allowed_operators)
        d["values"] = [{"value": v, "label": lab} for v, lab in self.values]
        d["parameters"] = {k: v for k, v in self.parameters}
        return d

    def value_label(self, v) -> str:
        return dict(self.values).get(v, str(v))


def _f(feature_id, name, description, scope, data_type, unit, source, historical, forward, pit, **kw) -> Feature:
    return Feature(feature_id, name, description, scope, data_type, unit, source, historical, forward, pit, **kw)


TREND_VALUES = (("UPTREND", "Up"), ("DOWNTREND", "Down"), ("MIXED", "Mixed"))
SIGNAL_VALUES = tuple((s, s.title()) for s in ("NORMAL", "LARGE GAIN", "LARGE DROP", "OVERBOUGHT", "OVERSOLD",
                                                 "UNUSUAL VOLUME", "BREAKOUT WATCH", "PULLBACK", "STRONG MOMENTUM"))
IND = "analysis.indicators.compute_metrics"
EMA = (("ema_fast", config.EMA_FAST), ("ema_medium", config.EMA_MEDIUM), ("ema_slow", config.EMA_SLOW))

FEATURES: Tuple[Feature, ...] = (
    # ---- STOCK — daily-bar technicals (historical + forward) -----------------------------------------------------
    _f("stock.trend", "Stock trend", "Up when price > EMA9 > EMA20 > EMA50, down when the reverse, otherwise mixed.",
       "STOCK", ENUM, None, f"{IND} → determine_trend, via insights.labels.trend_label", True, True, PIT_BARS,
       values=TREND_VALUES, parameters=EMA),
    _f("stock.momentum", "Momentum", "Today's move blended with RSI (momentum score) as Positive / Normal / Weak.",
       "STOCK", ENUM, None, f"{IND} → scoring.momentum_score, via insights.labels.momentum_label", True, True, PIT_BARS,
       values=(("STRONG", "Positive"), ("NORMAL", "Normal"), ("WEAK", "Weak")),
       parameters=(("strong_score", config.BEGINNER_MOMENTUM_STRONG_SCORE), ("weak_score", config.BEGINNER_MOMENTUM_WEAK_SCORE),
                   ("rsi_period", config.RSI_PERIOD))),
    _f("stock.momentum_score", "Momentum score", "0–100 blend of today's move and RSI; 50 is neutral.", "STOCK", NUMBER,
       "0-100", f"{IND} → scoring.momentum_score", True, True, PIT_BARS, minimum=0, maximum=100,
       parameters=(("rsi_period", config.RSI_PERIOD),)),
    _f("stock.change_1d_pct", "Daily change", "Close versus the previous close.", "STOCK", NUMBER, "%",
       f"{IND} → pct_change", True, True, PIT_BARS, minimum=-100, maximum=1000),
    _f("stock.gap_pct", "Opening gap", "Open versus the previous close.", "STOCK", NUMBER, "%", f"{IND} → gap_pct",
       True, True, PIT_BARS, minimum=-100, maximum=1000),
    _f("stock.momentum_5d_pct", "5-day change", "Close versus the close 5 sessions earlier.", "STOCK", NUMBER, "%",
       f"{IND} → momentum_5d_pct", True, True, PIT_BARS, minimum=-100, maximum=1000,
       parameters=(("days", config.MOMENTUM_5D_DAYS),)),
    _f("stock.momentum_10d_pct", "10-day change", "Close versus the close 10 sessions earlier.", "STOCK", NUMBER, "%",
       f"{IND} → momentum_10d_pct", True, True, PIT_BARS, minimum=-100, maximum=1000,
       parameters=(("days", config.MOMENTUM_10D_DAYS),)),
    _f("stock.rsi_14", "RSI", "Relative Strength Index (Wilder smoothing).", "STOCK", NUMBER, "0-100", f"{IND} → rsi",
       True, True, PIT_BARS, minimum=0, maximum=100, parameters=(("period", config.RSI_PERIOD),)),
    _f("stock.relative_volume", "Relative volume", "Today's volume divided by the average of the previous sessions.",
       "STOCK", NUMBER, "x", f"{IND} → relative_volume", True, True, PIT_BARS, minimum=0, maximum=100,
       parameters=(("average_days", config.VOLUME_AVG_LOOKBACK_DAYS),)),
    _f("stock.volume_level", "Volume", "Relative volume as Strong / Normal / Light.", "STOCK", ENUM, None,
       f"{IND} → relative_volume, via insights.labels.volume_label", True, True, PIT_BARS,
       values=(("STRONG", "Strong"), ("NORMAL", "Normal"), ("WEAK", "Light")),
       parameters=(("strong_rvol", signals.UNUSUAL_VOLUME_RVOL), ("light_below_rvol", config.RISK_LOW_RVOL_THRESHOLD),
                   ("average_days", config.VOLUME_AVG_LOOKBACK_DAYS))),
    _f("stock.volume_expansion", "Volume expansion", "Short-window average volume over long-window average volume.",
       "STOCK", NUMBER, "x", f"{IND} → volume_expansion", True, True, PIT_BARS, minimum=0, maximum=100,
       parameters=(("short_days", config.VOLUME_EXPANSION_SHORT_DAYS), ("long_days", config.VOLUME_EXPANSION_LONG_DAYS))),
    _f("stock.atr_14", "ATR", "Average True Range in dollars.", "STOCK", NUMBER, "$", f"{IND} → atr", True, True, PIT_BARS,
       minimum=0, maximum=1_000_000, parameters=(("period", config.ATR_PERIOD),)),
    _f("stock.volatility_20d_pct", "Volatility", "Annualised volatility of daily returns over 20 sessions.", "STOCK",
       NUMBER, "%", f"{IND} → historical_volatility_pct", True, True, PIT_BARS, minimum=0, maximum=1000,
       parameters=(("days", config.VOLATILITY_LOOKBACK_DAYS),)),
    _f("stock.volatility_expansion", "Volatility expansion", "5-session volatility over 20-session volatility.",
       "STOCK", NUMBER, "x", f"{IND} → volatility_expansion", True, True, PIT_BARS, minimum=0, maximum=100,
       parameters=(("short_days", config.VOLATILITY_SHORT_LOOKBACK_DAYS), ("long_days", config.VOLATILITY_LOOKBACK_DAYS))),
    _f("stock.price_location", "Price location", "Where the close sits between recent support and resistance.",
       "STOCK", ENUM, None, f"{IND} → levels.find_support_resistance, via insights.labels.price_location", True, True,
       PIT_BARS, values=(("NEAR_SUPPORT", "Near support"), ("MIDDLE_OF_RANGE", "Mid-range"),
                         ("NEAR_RESISTANCE", "Near resistance"), ("NO_RESISTANCE_ABOVE", "Above recent resistance"),
                         ("NO_SUPPORT_BELOW", "Below recent support")),
       parameters=(("near_level_pct", config.APPROACHING_LEVEL_PCT),)),
    _f("stock.dist_from_support_pct", "Distance from support", "Close versus the nearest support below it.", "STOCK",
       NUMBER, "%", f"{IND} → dist_from_support_pct", True, True, PIT_BARS, minimum=-100, maximum=1000),
    _f("stock.dist_from_resistance_pct", "Distance from resistance", "Close versus the nearest resistance above it "
       "(negative when below).", "STOCK", NUMBER, "%", f"{IND} → dist_from_resistance_pct", True, True, PIT_BARS,
       minimum=-100, maximum=1000),
    _f("stock.dist_from_20d_high_pct", "Distance from 20-day high", "Close versus the 20-session high.", "STOCK",
       NUMBER, "%", f"{IND} → dist_from_high_pct", True, True, PIT_BARS, minimum=-100, maximum=1000,
       parameters=(("days", config.HIGH_LOW_LOOKBACK_DAYS),)),
    _f("stock.dist_from_20d_low_pct", "Distance from 20-day low", "Close versus the 20-session low.", "STOCK", NUMBER,
       "%", f"{IND} → dist_from_low_pct", True, True, PIT_BARS, minimum=-100, maximum=10000,
       parameters=(("days", config.HIGH_LOW_LOOKBACK_DAYS),)),
    _f("stock.support", "Support level", "Nearest recent swing-low level below the close.", "STOCK", NUMBER, "$",
       f"{IND} → levels.find_support_resistance", True, True, PIT_BARS, minimum=0, maximum=1_000_000,
       notes="An absolute price; usually used through distance features or the entry-support exit method."),
    _f("stock.resistance", "Resistance level", "Nearest recent swing-high level above the close.", "STOCK", NUMBER, "$",
       f"{IND} → levels.find_support_resistance", True, True, PIT_BARS, minimum=0, maximum=1_000_000,
       notes="An absolute price; usually used through distance features or the entry-resistance exit method."),
    _f("stock.close", "Close price", "The session's closing price.", "STOCK", NUMBER, "$", f"{IND} → price (daily close)",
       True, True, PIT_BARS, minimum=0, maximum=1_000_000),
    _f("stock.extended", "Extended", "Risen quickly: 5-day change or RSI at the app's 'extended' limits.", "STOCK",
       BOOLEAN, None, f"{IND}, via insights.labels.is_extended", True, True, PIT_BARS,
       parameters=(("momentum_5d_pct", config.RISK_EXTENDED_MOMENTUM_PCT), ("rsi", signals.RSI_OVERBOUGHT)),
       true_text="the stock is extended (has risen quickly)", false_text="the stock is not extended"),
    _f("stock.signal", "Scanner signal", "The app's rule-based daily activity label (e.g. Breakout watch, Pullback).",
       "STOCK", ENUM, None, f"{IND} → signals.generate_signal", True, True, PIT_BARS, values=SIGNAL_VALUES,
       parameters=(("large_move_pct", signals.LARGE_MOVE_PCT), ("rsi_overbought", signals.RSI_OVERBOUGHT),
                   ("rsi_oversold", signals.RSI_OVERSOLD), ("unusual_rvol", signals.UNUSUAL_VOLUME_RVOL),
                   ("breakout_proximity_pct", signals.BREAKOUT_PROXIMITY_PCT), ("pullback_pct", signals.PULLBACK_PCT),
                   ("strong_momentum_pct", signals.STRONG_MOMENTUM_PCT))),
    # ---- MARKET — index + breadth labels (historical + forward) -----------------------------------------------------
    _f("market.trend", "Market trend", "SPY trend with its 5-day change: Improving / Mixed / Weakening.", "MARKET",
       ENUM, None, "insights.market.build_market_insights → trend", True, True, PIT_MARKET,
       values=(("IMPROVING", "Improving"), ("MIXED", "Mixed"), ("WEAKENING", "Weakening"))),
    _f("market.environment", "Trading environment", "Market trend, breadth and volatility together.", "MARKET", ENUM,
       None, "insights.market.build_market_insights → environment", True, True, PIT_MARKET,
       values=(("SUPPORTIVE", "Supportive"), ("MIXED", "Mixed"), ("CAUTIOUS", "Cautious")),
       parameters=(("vol_low_pct", config.MARKET_VOL_LOW_PCT), ("vol_elevated_pct", config.MARKET_VOL_ELEVATED_PCT),
                   ("breadth_strong_pct", config.MARKET_BREADTH_STRONG_PCT), ("breadth_weak_pct", config.MARKET_BREADTH_WEAK_PCT))),
    _f("market.volatility", "Market volatility", "SPY annualised 20-day volatility and its expansion.", "MARKET", ENUM,
       None, "insights.market.build_market_insights → volatility", True, True, PIT_MARKET,
       values=(("LOW", "Low"), ("NORMAL", "Normal"), ("ELEVATED", "Elevated")),
       parameters=(("low_pct", config.MARKET_VOL_LOW_PCT), ("elevated_pct", config.MARKET_VOL_ELEVATED_PCT),
                   ("expansion_ratio", config.VOLATILITY_EXPANSION_RATIO_THRESHOLD))),
    _f("market.breadth", "Breadth", "How many of the app's fixed list of 80 large stocks rose and sit above EMA20.",
       "MARKET", ENUM, None, "insights.market.breadth_summary → label", True, True, PIT_MARKET,
       values=(("STRONG", "Strong"), ("MIXED", "Mixed"), ("WEAK", "Weak")),
       parameters=(("strong_pct", config.MARKET_BREADTH_STRONG_PCT), ("weak_pct", config.MARKET_BREADTH_WEAK_PCT)),
       notes="Uses today's fixed breadth list (survivorship caveat); unavailable on days with too few bars."),
    _f("market.breadth_advancing_pct", "Breadth % rising", "Share of the fixed breadth list that rose.", "MARKET",
       NUMBER, "%", "insights.market.breadth_summary → advancing_pct", True, True, PIT_MARKET, minimum=0, maximum=100),
    _f("market.risk_appetite", "Risk appetite", "SPY vs QQQ moves with breadth: Risk-on / Mixed / Risk-off.", "MARKET",
       ENUM, None, "insights.market.build_market_insights → risk_appetite", True, True, PIT_MARKET,
       values=(("RISK-ON", "Risk-on"), ("MIXED", "Mixed"), ("RISK-OFF", "Risk-off"))),
    _f("market.spy_trend", "SPY trend", "S&P 500 ETF trend (EMA stacking).", "MARKET", ENUM, None,
       "insights.market.index_summary(SPY) → trend", True, True, PIT_MARKET, values=TREND_VALUES, parameters=EMA),
    _f("market.qqq_trend", "QQQ trend", "Nasdaq-100 ETF trend (EMA stacking).", "MARKET", ENUM, None,
       "insights.market.index_summary(QQQ) → trend", True, True, PIT_MARKET, values=TREND_VALUES, parameters=EMA),
    _f("market.soxx_trend", "SOXX trend", "Semiconductor ETF trend (EMA stacking).", "MARKET", ENUM, None,
       "insights.market.index_summary(SOXX) → trend", True, True, PIT_MARKET, values=TREND_VALUES, parameters=EMA),
    _f("market.major_event_within_24h", "Major macro event within 24h", "A verified macro event (FOMC, CPI, jobs "
       "report …) is less than a day away.", "MARKET", BOOLEAN, None, "insights.market.build_market_insights → events",
       False, True, PIT_EVENT, parameters=(("hours", config.EVENT_HIGH_RISK_HOURS),),
       true_text="a major macro event is within 24 hours", false_text="no major macro event is within 24 hours"),
    # ---- SECTOR — sector ETF context (historical + forward) ---------------------------------------------------------
    _f("sector.context", "Sector today", "The stock's sector ETF move as Strong / Mixed / Weak this session.", "SECTOR",
       ENUM, None, "analysis.sector_context.compute_sector_context → evidence_scoring.sector_score → "
       "insights.labels.context_label", True, True, PIT_SECTOR,
       values=(("SUPPORTIVE", "Strong this session"), ("MIXED", "Mixed"), ("WEAK", "Weak this session")),
       parameters=(("sector_norm_pct", config.SECTOR_NORM_PCT), ("lean_threshold", config.RESEARCH_VIEW_LEAN_THRESHOLD))),
    _f("sector.stock_vs_sector_pct", "Stock vs sector", "Stock's daily change minus its sector ETF's.", "SECTOR",
       NUMBER, "%", "analysis.sector_context.compute_sector_context → stock_vs_sector_pct", True, True, PIT_SECTOR,
       minimum=-1000, maximum=1000),
    _f("sector.etf_trend", "Sector ETF trend", "Trend of the stock's sector ETF (EMA stacking).", "SECTOR", ENUM, None,
       f"{IND}(sector ETF) → determine_trend, via insights.labels.trend_label", True, True, PIT_SECTOR,
       values=TREND_VALUES, parameters=EMA),
    # ---- RESEARCH — Claude-assisted research (forward only) --------------------------------------------------------
    _f("research.view", "Research view", "The saved Research View for the stock.", "RESEARCH", ENUM, None,
       "insights.research.lookup_research → research_view", False, True, PIT_RESEARCH,
       values=(("STRONG BULLISH BIAS", "Strong bullish bias"), ("BULLISH BIAS", "Bullish bias"),
               ("MIXED / WAIT", "Mixed / wait"), ("BEARISH BIAS", "Bearish bias"),
               ("STRONG BEARISH BIAS", "Strong bearish bias"))),
    _f("research.freshness", "Research freshness", "How recent the saved research is.", "RESEARCH", ENUM, None,
       "insights.plain.research_freshness(lookup_research → age_hours)", False, True, PIT_RESEARCH,
       values=(("FRESH", "Fresh"), ("AGING", "Aging"), ("STALE", "Stale"), ("MISSING", "Missing")),
       parameters=(("fresh_hours", config.FRESHNESS_RESEARCH_FRESH_HOURS), ("stale_hours", config.BEGINNER_RESEARCH_STALE_HOURS))),
    _f("research.bullish_pct", "Research bullish share", "Bullish share of the research evidence mix (not a "
       "probability).", "RESEARCH", NUMBER, "%", "insights.research.lookup_research → bullish_pct", False, True,
       PIT_RESEARCH, minimum=0, maximum=100),
    _f("research.catalyst", "Catalyst sentiment", "Direction of the research's catalyst evidence.", "RESEARCH", ENUM,
       None, "insights.research.lookup_research → category_scores['catalyst'] sign", False, True, PIT_RESEARCH,
       values=(("POSITIVE", "Positive"), ("NEUTRAL", "Neutral"), ("NEGATIVE", "Negative"))),
    # ---- EVENT — Stage 2.6 event intelligence (forward only) --------------------------------------------------------
    _f("event.risk_level", "Event risk", "Stage 2.6 event risk for the stock (earnings, macro …).", "EVENT", ENUM, None,
       "services.event_context.build_event_context → event_risk_level", False, True, PIT_EVENT,
       values=(("HIGH", "High"), ("MEDIUM", "Medium"), ("LOW", "Low"), ("NONE", "None"))),
    # ---- PORTFOLIO — live account state (not usable by strategies yet) ---------------------------------------------
    _f("portfolio.position_weight_pct", "Position weight", "The stock's share of the live Robinhood account.",
       "PORTFOLIO", NUMBER, "%", "portfolio.analytics.build_view → portfolio_weight", False, False, PIT_PORTFOLIO,
       minimum=0, maximum=100),
    _f("portfolio.sector_weight_pct", "Sector weight", "The stock's sector share of the live Robinhood account.",
       "PORTFOLIO", NUMBER, "%", "portfolio.analytics.build_view → sector_exposure", False, False, PIT_PORTFOLIO,
       minimum=0, maximum=100),
)

BY_ID: Dict[str, Feature] = {f.feature_id: f for f in FEATURES}
assert len(BY_ID) == len(FEATURES), "duplicate feature_id"


def get(feature_id: str) -> Optional[Feature]:
    return BY_ID.get(feature_id)


def fingerprint() -> str:
    """SHA-256 over every feature definition and its CURRENT parameter values (stable key order)."""
    payload = json.dumps({"registry_version": REGISTRY_VERSION, "features": [f.public() for f in FEATURES]},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def public_registry() -> dict:
    return {"registry_version": REGISTRY_VERSION, "registry_fingerprint": fingerprint(), "scopes": list(SCOPES),
            "operators": {k: list(v) for k, v in OPS_BY_TYPE.items()}, "features": [f.public() for f in FEATURES]}


# ================================================================================================================
# EXTRACTORS — feature values from objects the app ALREADY computes (no new maths). Used by future evaluators.
# ================================================================================================================

def stock_values(m: Any) -> Dict[str, Any]:
    """From an analysis.indicators.TickerMetrics computed on bars through the evaluation day (no latest trade)."""
    from insights.labels import is_extended, momentum_label, price_location, trend_label, volume_label
    if m is None:
        return {}
    return {
        "stock.trend": trend_label(m.trend), "stock.momentum": momentum_label(m.momentum_score),
        "stock.momentum_score": m.momentum_score, "stock.change_1d_pct": m.pct_change, "stock.gap_pct": m.gap_pct,
        "stock.momentum_5d_pct": m.momentum_5d_pct, "stock.momentum_10d_pct": m.momentum_10d_pct, "stock.rsi_14": m.rsi,
        "stock.relative_volume": m.relative_volume, "stock.volume_level": volume_label(m.relative_volume),
        "stock.volume_expansion": m.volume_expansion, "stock.atr_14": m.atr, "stock.volatility_20d_pct": m.volatility_pct,
        "stock.volatility_expansion": m.volatility_expansion,
        "stock.price_location": price_location(m.price, m.support, m.resistance),
        "stock.dist_from_support_pct": m.dist_from_support_pct, "stock.dist_from_resistance_pct": m.dist_from_resistance_pct,
        "stock.dist_from_20d_high_pct": m.dist_from_high_pct, "stock.dist_from_20d_low_pct": m.dist_from_low_pct,
        "stock.support": m.support, "stock.resistance": m.resistance, "stock.close": m.price,
        "stock.extended": is_extended(m.momentum_5d_pct, m.rsi), "stock.signal": m.signal,
    }


def market_values(market: Optional[dict]) -> Dict[str, Any]:
    """From insights.market.build_market_insights output built from bars through the evaluation day."""
    if not market or not market.get("available"):
        return {}
    idx = {i["symbol"]: i for i in market.get("indices", []) if i.get("available")}
    breadth = market.get("breadth") or {}
    events = market.get("events") or []
    return {
        "market.trend": market.get("trend"), "market.environment": market.get("environment"),
        "market.volatility": market.get("volatility"), "market.breadth": market.get("breadth_label"),
        "market.breadth_advancing_pct": breadth.get("advancing_pct"), "market.risk_appetite": market.get("risk_appetite"),
        "market.spy_trend": (idx.get(config.MARKET_PROXY_SYMBOL) or {}).get("trend"),
        "market.qqq_trend": (idx.get(config.MARKET_TECH_PROXY_SYMBOL) or {}).get("trend"),
        "market.soxx_trend": (idx.get("SOXX") or {}).get("trend"),
        "market.major_event_within_24h": any(e.get("hours_until") is not None and e["hours_until"] <= config.EVENT_HIGH_RISK_HOURS
                                             for e in events),
    }


def sector_values(ctx: Any, etf_metrics: Any = None) -> Dict[str, Any]:
    """From analysis.sector_context.SectorContext (+ the sector ETF's TickerMetrics) for the evaluation day."""
    from analysis.evidence_scoring import sector_score
    from insights.labels import context_label, trend_label
    if ctx is None:
        return {}
    return {"sector.context": context_label(sector_score(ctx)), "sector.stock_vs_sector_pct": ctx.stock_vs_sector_pct,
            "sector.etf_trend": trend_label(etf_metrics.trend) if etf_metrics is not None else None}


def research_values(r: Any) -> Dict[str, Any]:
    """From insights.research.ResearchFacts that existed at the signal time (forward use only)."""
    from insights.plain import research_freshness
    if r is None or not r.available:
        return {"research.freshness": "MISSING"}
    cat = (r.category_scores or {}).get("catalyst")
    return {"research.view": r.research_view, "research.freshness": research_freshness(r.age_hours),
            "research.bullish_pct": r.bullish_pct,
            "research.catalyst": None if cat is None else "POSITIVE" if cat > 0 else "NEGATIVE" if cat < 0 else "NEUTRAL"}


def event_values(bundle: Any) -> Dict[str, Any]:
    """From services.event_context.EventsBundle fetched at the signal time (forward use only)."""
    level = getattr(bundle, "event_risk_level", None) if bundle is not None else None
    return {"event.risk_level": level if level in ("HIGH", "MEDIUM", "LOW", "NONE") else None}


EXTRACTORS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "STOCK": stock_values, "MARKET": market_values, "SECTOR": sector_values, "RESEARCH": research_values,
    "EVENT": event_values,   # PORTFOLIO: intentionally none — not a strategy input until a paper portfolio exists
}
