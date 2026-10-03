"""
config.py — All user-editable settings for the stock monitoring agent.

Nothing in this file is secret. API credentials are NEVER stored here —
they are loaded from environment variables (via a local .env file) so
they never end up in source control or terminal output.
"""
from pathlib import Path
from typing import List

from dotenv import load_dotenv
import os

# Load variables from a local .env file (if present) into the environment.
load_dotenv()

PROJECT_ROOT: Path = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Alpaca API credentials — set these in a .env file next to this project
# (copy .env.example to .env and fill in your keys). Never hard-code keys.
# --------------------------------------------------------------------------
ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")


def has_credentials() -> bool:
    """Whether Alpaca API credentials have been supplied via the environment."""
    return bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)


# --------------------------------------------------------------------------
# Robinhood credentials (OPTIONAL) — only needed if you use
# `python main.py --sync-robinhood` to pull your current Robinhood holdings
# into watchlist.txt. See robinhood_sync.py for important caveats: this
# uses an unofficial, reverse-engineered client library, not an official
# Robinhood API. Leave these blank if you don't want to use that feature.
# --------------------------------------------------------------------------
ROBINHOOD_USERNAME: str = os.getenv("ROBINHOOD_USERNAME", "")
ROBINHOOD_PASSWORD: str = os.getenv("ROBINHOOD_PASSWORD", "")

# Where the Robinhood login session is cached on disk so you don't have to
# re-approve 2FA on every sync. Kept inside the project but git-ignored.
ROBINHOOD_SESSION_DIR: Path = PROJECT_ROOT / ".robinhood_session"


def has_robinhood_credentials() -> bool:
    """Whether Robinhood credentials have been supplied via the environment."""
    return bool(ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD)


# --------------------------------------------------------------------------
# LLM provider (OPTIONAL) — used by agents/technical_agent.py to explain a
# stock's technical picture in plain English. If unset, AI-dependent
# endpoints return a clear "unavailable" message instead of failing; every
# other feature (scanner, watchlist, dashboard) works with no LLM key at all.
# --------------------------------------------------------------------------
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

# Only needed if your key is an organization-level key that isn't scoped to
# a specific workspace (Anthropic will return a 400 asking for this header
# in that case). Leave blank if your key works without it.
ANTHROPIC_WORKSPACE_ID: str = os.getenv("ANTHROPIC_WORKSPACE_ID", "")


def has_llm_credentials() -> bool:
    """Whether an LLM provider key has been supplied via the environment."""
    return bool(ANTHROPIC_API_KEY)


# --------------------------------------------------------------------------
# AI-agent trigger/cost controls. The quant engine (Alpaca + Python) always
# runs on every scan; Claude is only ever called on top of it, for a bounded
# number of already-interesting stocks — never for the whole market, and
# never more than these limits allow. All of these are read from the
# environment so they're changeable without touching code.
# --------------------------------------------------------------------------
# Only stocks whose Attention Score crosses this threshold are eligible for
# AI analysis at all — below it, get_technical_analysis() never calls Claude.
AI_ANALYSIS_ATTENTION_THRESHOLD: int = int(os.getenv("AI_ANALYSIS_ATTENTION_THRESHOLD", "75"))

# How long a cached AI analysis is considered fresh before it's eligible for
# re-analysis (see AI_REANALYZE_* below for the other staleness triggers).
AI_ANALYSIS_CACHE_MINUTES: int = int(os.getenv("AI_ANALYSIS_CACHE_MINUTES", "30"))

# Hard ceilings on Claude calls, independent of caching/threshold — a safety
# net so a bug or a chaotic market day can't run up an unbounded AI bill.
# Two independent budgets (Stage 3.9) — one never reduces the other:
#   RESEARCH     every AI feature that existed before Stage 3.8 (Research Analyze / batch, chat, portfolio and insight
#                explanations). Env AI_RESEARCH_HOURLY_LIMIT / AI_RESEARCH_DAILY_LIMIT; the older names
#                AI_MAX_CALLS_PER_HOUR / AI_MAX_CALLS_PER_DAY still work. The code reads AI_MAX_CALLS_PER_*.
#   EXPLANATION  Stage 3.8 "Explain this setup" / "Explain this evidence".
AI_MAX_CALLS_PER_HOUR: int = int(os.getenv("AI_RESEARCH_HOURLY_LIMIT", os.getenv("AI_MAX_CALLS_PER_HOUR", "10")))
AI_MAX_CALLS_PER_DAY: int = int(os.getenv("AI_RESEARCH_DAILY_LIMIT", os.getenv("AI_MAX_CALLS_PER_DAY", "30")))
AI_EXPLANATION_HOURLY_LIMIT: int = int(os.getenv("AI_EXPLANATION_HOURLY_LIMIT", "10"))
AI_EXPLANATION_DAILY_LIMIT: int = int(os.getenv("AI_EXPLANATION_DAILY_LIMIT", "30"))

# A cached analysis is still invalidated early if price or Attention Score
# have moved this much, even if the cache TTL hasn't expired yet.
AI_REANALYZE_PRICE_CHANGE_PERCENT: float = float(os.getenv("AI_REANALYZE_PRICE_CHANGE_PERCENT", "2.0"))
AI_REANALYZE_ATTENTION_CHANGE: int = int(os.getenv("AI_REANALYZE_ATTENTION_CHANGE", "10"))

# --------------------------------------------------------------------------
# Claude model pricing, USD per 1,000,000 tokens. Isolated here so a price
# change never requires touching agents/usage_tracker.py or any calling
# code — just update these numbers. Cache write/read rates follow
# Anthropic's standard multipliers (1.25x / 0.1x of the input rate) where
# not separately published. Source: Anthropic pricing, retrieved 2026-06-24.
# A model not listed here simply won't get a cost estimate (never guessed).
# --------------------------------------------------------------------------
AI_MODEL_PRICING = {
    "claude-fable-5-1":  {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 1.00},
    "claude-fable-5":    {"input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 1.00},
    "claude-opus-5":     {"input": 5.00,  "output": 25.00, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-opus-4-8":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-opus-4-7":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-opus-4-6":   {"input": 5.00,  "output": 25.00, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-sonnet-5":   {"input": 2.00,  "output": 10.00, "cache_write": 2.50,  "cache_read": 0.20},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.00,  "output": 5.00,  "cache_write": 1.25,  "cache_read": 0.10},
}


# --------------------------------------------------------------------------
# Web dashboard (FastAPI + static frontend)
# --------------------------------------------------------------------------
FASTAPI_HOST: str = os.getenv("FASTAPI_HOST", "127.0.0.1")
FASTAPI_PORT: int = int(os.getenv("FASTAPI_PORT", "8000"))
WEB_UI_REFRESH_SECONDS: int = 30  # how often the browser redraws from cached data


# --------------------------------------------------------------------------
# Scan intervals (in seconds). Only used when main.py is run with --loop.
# --------------------------------------------------------------------------
MARKET_SCAN_INTERVAL: int = 900   # how often to refresh the whole-market overview
WATCHLIST_SCAN_INTERVAL: int = 60  # how often to refresh the watchlist detail view

# --------------------------------------------------------------------------
# Liquidity filters for the market-wide scanner. These keep illiquid /
# penny stocks out of the "notable stocks" results.
# --------------------------------------------------------------------------
MIN_PRICE: float = 5.0
MIN_AVG_VOLUME: int = 500_000

# --------------------------------------------------------------------------
# Output sizing
# --------------------------------------------------------------------------
TOP_RESULTS: int = 20

# --------------------------------------------------------------------------
# Indicator / lookback settings
# --------------------------------------------------------------------------
DAILY_BAR_LOOKBACK_DAYS: int = 120    # calendar days of daily bars to pull per symbol
RSI_PERIOD: int = 14
EMA_FAST: int = 9
EMA_MEDIUM: int = 20
EMA_SLOW: int = 50
HIGH_LOW_LOOKBACK_DAYS: int = 20      # window for the "20-day high/low" feature
VOLUME_AVG_LOOKBACK_DAYS: int = 20    # window used to compute average volume
VOLATILITY_LOOKBACK_DAYS: int = 20    # window used to compute historical volatility
VOLATILITY_SHORT_LOOKBACK_DAYS: int = 5  # shorter window, used only for the volatility-expansion ratio

ATR_PERIOD: int = 14                  # Average True Range smoothing period

SUPPORT_RESISTANCE_LOOKBACK_DAYS: int = 60  # bars considered when looking for swing highs/lows
SWING_WINDOW_DAYS: int = 3                  # a bar is a swing point if it's the extreme within +/- this many bars
APPROACHING_LEVEL_PCT: float = 2.0          # within this % of support/resistance counts as "approaching"

MOMENTUM_5D_DAYS: int = 5             # multi-day momentum windows
MOMENTUM_10D_DAYS: int = 10

VOLUME_EXPANSION_SHORT_DAYS: int = 5   # short/long average-volume ratio windows
VOLUME_EXPANSION_LONG_DAYS: int = 20
VOLATILITY_EXPANSION_RATIO_THRESHOLD: float = 1.3  # short/long volatility ratio considered "expanding"

# --------------------------------------------------------------------------
# Attention Score weights (must describe "how unusual/interesting," never a
# buy/sell weighting). Kept configurable per the original design goal.
# --------------------------------------------------------------------------
ATTENTION_WEIGHT_MOMENTUM: float = 0.35
ATTENTION_WEIGHT_RVOL: float = 0.30
ATTENTION_WEIGHT_TREND: float = 0.20
ATTENTION_WEIGHT_VOLATILITY: float = 0.15

# --------------------------------------------------------------------------
# Alpaca screener settings (whole-market scan). Alpaca's screener endpoints
# do the market-wide ranking on their side, so we only ever request a
# bounded list of "already interesting" symbols back — this keeps the
# scanner efficient and avoids pulling data for the entire market.
# --------------------------------------------------------------------------
SCREENER_TOP_N: int = 25              # symbols requested per screener call (gainers/losers/actives)

# Which Alpaca data feed to use. "iex" works on free/basic accounts.
# Accounts with a market data subscription can switch this to "sip".
DATA_FEED: str = os.getenv("ALPACA_DATA_FEED", "iex")

# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------
WATCHLIST_FILE: Path = PROJECT_ROOT / "watchlist.txt"

# --------------------------------------------------------------------------
# Fallback universe — used ONLY if Alpaca's screener endpoints are
# unavailable (e.g. older data plan, temporary outage). This is a small,
# fixed list of liquid large/mid-cap U.S. stocks so the market overview
# still produces useful output. It is intentionally bounded in size so a
# fallback scan stays fast, cheap, and within API rate limits.
# --------------------------------------------------------------------------
FALLBACK_UNIVERSE: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "AVGO", "TSLA", "BRK.B",
    "LLY", "JPM", "V", "XOM", "UNH", "MA", "COST", "HD", "PG", "JNJ",
    "NFLX", "BAC", "ABBV", "CRM", "ORCL", "MRK", "CVX", "KO", "AMD", "PEP",
    "ADBE", "WMT", "TMO", "LIN", "MCD", "CSCO", "ACN", "ABT", "DHR", "TXN",
    "GE", "PM", "IBM", "CAT", "VZ", "INTU", "QCOM", "NOW", "AMGN", "ISRG",
    "SPGI", "BKNG", "NEE", "AMAT", "PFE", "UNP", "RTX", "LOW", "HON", "SYK",
    "MU", "MRVL", "SNDK", "PANW", "LRCX", "KLAC", "ADI", "REGN", "VRTX", "PLD",
    "SCHW", "GS", "MS", "BLK", "C", "AXP", "T", "DIS", "UBER", "SBUX",
]

# ==========================================================================
# STAGE 2 — Beginner research view, evidence engine, risk/position sizing.
# Everything above this line is unchanged from Milestone 1.
# ==========================================================================

# --------------------------------------------------------------------------
# Explanation mode. The UI defaults to plain-language Beginner mode; a
# future toggle can request ADVANCED per-call. Advanced technical fields
# are always computed and returned either way — this only controls which
# framing is primary in the UI/beginner narrative.
# --------------------------------------------------------------------------
EXPLANATION_MODE: str = os.getenv("EXPLANATION_MODE", "BEGINNER")  # BEGINNER | ADVANCED

# --------------------------------------------------------------------------
# Evidence-scoring weights (analysis/evidence_scoring.py). A category with
# no data is excluded and its weight redistributed among the rest — it is
# never defaulted to 0, which would silently count "no data" as evidence.
# --------------------------------------------------------------------------
EVIDENCE_WEIGHT_TECHNICAL: float = float(os.getenv("EVIDENCE_WEIGHT_TECHNICAL", "0.35"))
EVIDENCE_WEIGHT_CATALYST: float = float(os.getenv("EVIDENCE_WEIGHT_CATALYST", "0.25"))
EVIDENCE_WEIGHT_RISK: float = float(os.getenv("EVIDENCE_WEIGHT_RISK", "0.20"))
EVIDENCE_WEIGHT_MARKET: float = float(os.getenv("EVIDENCE_WEIGHT_MARKET", "0.10"))
EVIDENCE_WEIGHT_SECTOR: float = float(os.getenv("EVIDENCE_WEIGHT_SECTOR", "0.10"))

# Research View label thresholds, applied to net = (bullish_pct - bearish_pct) / 100.
RESEARCH_VIEW_STRONG_THRESHOLD: float = float(os.getenv("RESEARCH_VIEW_STRONG_THRESHOLD", "0.50"))
RESEARCH_VIEW_LEAN_THRESHOLD: float = float(os.getenv("RESEARCH_VIEW_LEAN_THRESHOLD", "0.20"))

# --------------------------------------------------------------------------
# Market/Sector context normalization. A proxy's daily % change is divided
# by these values and clamped to [-1, 1] to become a -1..+1 evidence score
# (i.e. a move of this size or larger counts as "maximally" bullish/bearish
# for that category).
# --------------------------------------------------------------------------
MARKET_NORM_PCT: float = float(os.getenv("MARKET_NORM_PCT", "1.5"))
SECTOR_NORM_PCT: float = float(os.getenv("SECTOR_NORM_PCT", "2.0"))
MARKET_PROXY_SYMBOL: str = os.getenv("MARKET_PROXY_SYMBOL", "SPY")

# --------------------------------------------------------------------------
# Risk Agent flag thresholds (analysis/risk_flags.py) and severities used to
# turn triggered flags into the Risk evidence-category score.
# --------------------------------------------------------------------------
RISK_FLAG_SEVERITY = {"HIGH": 0.45, "MEDIUM": 0.25, "LOW": 0.10}
RISK_ATR_PCT_THRESHOLD: float = float(os.getenv("RISK_ATR_PCT_THRESHOLD", "4.0"))       # ATR as % of price
RISK_VOLATILITY_THRESHOLD: float = float(os.getenv("RISK_VOLATILITY_THRESHOLD", "60.0"))  # annualized %
RISK_LARGE_GAP_PCT: float = float(os.getenv("RISK_LARGE_GAP_PCT", "4.0"))
RISK_EXTENDED_MOMENTUM_PCT: float = float(os.getenv("RISK_EXTENDED_MOMENTUM_PCT", "10.0"))  # 5-day momentum
RISK_LOW_RVOL_THRESHOLD: float = float(os.getenv("RISK_LOW_RVOL_THRESHOLD", "1.0"))
RISK_SECTOR_MARKET_WEAKNESS_PCT: float = float(os.getenv("RISK_SECTOR_MARKET_WEAKNESS_PCT", "-1.0"))

# --------------------------------------------------------------------------
# Research Setup calculator (analysis/research_setup.py) — all arithmetic,
# no AI involved. `buffer` shrinks the invalidation level below support.
# --------------------------------------------------------------------------
RESEARCH_SETUP_INVALIDATION_BUFFER_PCT: float = float(os.getenv("RESEARCH_SETUP_INVALIDATION_BUFFER_PCT", "1.5"))
RESEARCH_SETUP_DEFAULT_HORIZON: str = os.getenv("RESEARCH_SETUP_DEFAULT_HORIZON", "2-5 days")

# --------------------------------------------------------------------------
# Deterministic Risk Manager / position sizing (analysis/position_sizing.py).
# Claude never picks a position size — this is pure Python arithmetic.
# Portfolio value has no real data source in this app yet, so it must be
# supplied explicitly per request; these are only the configurable ceilings.
# --------------------------------------------------------------------------
MAX_POSITION_PERCENT: float = float(os.getenv("MAX_POSITION_PERCENT", "10.0"))
MAX_RISK_PER_TRADE_PERCENT: float = float(os.getenv("MAX_RISK_PER_TRADE_PERCENT", "1.0"))
MAX_DAILY_LOSS_PERCENT: float = float(os.getenv("MAX_DAILY_LOSS_PERCENT", "3.0"))  # informational only — not enforced (no position tracking exists)
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "10"))               # informational only — not enforced (no position tracking exists)

# --------------------------------------------------------------------------
# AI chat (agents/chat_agent.py) — shares the same usage_tracker budget as
# the other agents; this only bounds the tool-use loop itself.
# --------------------------------------------------------------------------
CHAT_MAX_TOOL_ITERATIONS: int = int(os.getenv("CHAT_MAX_TOOL_ITERATIONS", "5"))


# ==========================================================================
# STAGE 2.5 — Research history, outcome tracking, and validation.
# This is an OBSERVATION layer only: it stores immutable snapshots of what
# the research engine produced and later measures real price outcomes
# against them. Nothing here changes any scoring formula, weight,
# threshold, or prompt above this line.
# ==========================================================================

# --------------------------------------------------------------------------
# Local SQLite database (database/database.py). Never stores API keys or
# secrets — only research snapshots and their later-measured outcomes.
# --------------------------------------------------------------------------
RESEARCH_DB_PATH: Path = PROJECT_ROOT / "data" / "stock_agent.db"

# --------------------------------------------------------------------------
# Versioning stamped onto every saved snapshot/outcome row. Bump these by
# hand whenever the corresponding logic changes — historical rows keep
# their original stamp forever; nothing here is ever rewritten in place.
# --------------------------------------------------------------------------
ENGINE_VERSION: str = "1.0.0"            # overall research-engine version (analysis/ + agents/)
EVIDENCE_VERSION: str = "1.0.0"          # analysis/evidence_scoring.py formula/weights version
PROMPT_VERSION: str = "1.0.0"            # agent system-prompt version
RESEARCH_SCHEMA_VERSION: str = "1.0.0"   # research_snapshots table shape
OUTCOME_SCHEMA_VERSION: str = "1.0.0"    # research_outcomes table shape

# --------------------------------------------------------------------------
# Outcome tracking (services/outcome_tracker.py)
# --------------------------------------------------------------------------
TRADING_DAY_HORIZONS: List[int] = [1, 3, 5]
OUTCOME_FETCH_BUFFER_DAYS: int = int(os.getenv("OUTCOME_FETCH_BUFFER_DAYS", "5"))  # extra calendar days fetched past the snapshot date

# --------------------------------------------------------------------------
# Snapshot deduplication (database/fingerprint.py)
# --------------------------------------------------------------------------
FINGERPRINT_PRICE_DECIMALS: int = int(os.getenv("FINGERPRINT_PRICE_DECIMALS", "2"))

# --------------------------------------------------------------------------
# How long a generated (but not yet saved) research bundle stays available
# to be saved verbatim via POST /api/research/snapshots (services/analysis_cache.py).
# --------------------------------------------------------------------------
ANALYSIS_CACHE_TTL_MINUTES: int = int(os.getenv("ANALYSIS_CACHE_TTL_MINUTES", "60"))

# --------------------------------------------------------------------------
# Performance analytics small-sample warnings (services/analytics.py)
# --------------------------------------------------------------------------
ANALYTICS_MIN_SAMPLE_SIZE_LOW: int = int(os.getenv("ANALYTICS_MIN_SAMPLE_SIZE_LOW", "10"))       # below this: "very small sample"
ANALYTICS_MIN_SAMPLE_SIZE_CAUTION: int = int(os.getenv("ANALYTICS_MIN_SAMPLE_SIZE_CAUTION", "30"))  # below this: "small sample"

# --------------------------------------------------------------------------
# Historical-validation badge (api/routes/research_history.py, presentation
# only). A snapshot whose market_timestamp predates its created_at by at
# least this many calendar days is flagged HISTORICAL VALIDATION in the UI --
# large enough to never fire on a normal weekend/holiday gap between the
# last trading session and an ordinary same-week save.
# --------------------------------------------------------------------------
HISTORICAL_VALIDATION_GAP_DAYS: int = int(os.getenv("HISTORICAL_VALIDATION_GAP_DAYS", "5"))

# ==========================================================================
# STAGE 2.6 — Real event intelligence (earnings/macro calendar/corporate
# actions). Observation layer only: event risk feeds the EXISTING Risk
# evidence category through new deterministic risk-flag codes (see
# analysis/risk_flags.py) -- no new evidence category, no weight/threshold
# change above this line. Every date/time here comes from a real, named
# source (see data/events/*.py module docstrings) or is honestly reported
# unavailable -- nothing is ever fabricated or inferred.
# ==========================================================================
EVENT_SCHEMA_VERSION: str = "1.0.0"

# How far back/ahead of "now" the event layer looks when assembling a
# symbol's event context.
EVENT_LOOKAHEAD_DAYS: int = int(os.getenv("EVENT_LOOKAHEAD_DAYS", "14"))
EVENT_LOOKBACK_DAYS: int = int(os.getenv("EVENT_LOOKBACK_DAYS", "7"))

# Deterministic event-risk severity thresholds (analysis/event_risk.py).
# Claude never assigns severity -- these are the sole authority.
EVENT_HIGH_RISK_HOURS: float = float(os.getenv("EVENT_HIGH_RISK_HOURS", "24"))
EVENT_MEDIUM_RISK_DAYS: float = float(os.getenv("EVENT_MEDIUM_RISK_DAYS", "3"))
EVENT_LOW_RISK_DAYS: float = float(os.getenv("EVENT_LOW_RISK_DAYS", "7"))

# Event-provider cache TTLs (data/events/cache.py) -- these calendars change
# slowly, so a request never needs to re-fetch them from scratch.
MACRO_CACHE_TTL_MINUTES: int = int(os.getenv("MACRO_CACHE_TTL_MINUTES", "720"))            # 12h
CORPORATE_ACTION_CACHE_TTL_MINUTES: int = int(os.getenv("CORPORATE_ACTION_CACHE_TTL_MINUTES", "180"))  # 3h
FOMC_CACHE_TTL_MINUTES: int = int(os.getenv("FOMC_CACHE_TTL_MINUTES", "720"))               # 12h
EARNINGS_CACHE_TTL_MINUTES: int = int(os.getenv("EARNINGS_CACHE_TTL_MINUTES", "60"))        # unused while unavailable

# FRED (St. Louis Fed) release-dates API — OPTIONAL. Without a key, the
# macro provider (data/events/macro.py) honestly reports UNAVAILABLE rather
# than guessing. Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html
FRED_API_KEY: str = os.getenv("FRED_API_KEY", "")


def has_fred_credentials() -> bool:
    """Whether a FRED API key has been supplied via the environment."""
    return bool(FRED_API_KEY)


# FRED release_id per macro series -- confirmed against FRED's own
# /fred/releases catalog, not guessed. JOLTS has no stable, cleanly
# resolvable release_id in this catalog as of this writing, so it is
# intentionally omitted (data/events/macro.py reports it UNAVAILABLE
# without blocking the other series).
FRED_RELEASE_IDS = {
    "CPI": 10,                     # Consumer Price Index
    "PPI": 46,                     # Producer Price Index
    "EMPLOYMENT_SITUATION": 50,    # Employment Situation (the "jobs report")
    "PCE": 21,                     # Personal Income and Outlays (includes the PCE price index)
}


# --------------------------------------------------------------------------
# Stage 2.7C — read-only portfolio awareness (OPTIONAL, OFF by default).
# The stock-agent never talks to Robinhood: it reads normalized JSON from the
# separate local rh_gateway process (../rh_gateway), which alone holds the
# Robinhood OAuth credential and full account numbers. When disabled, or when
# the gateway is unreachable, every Stage 1–2.6 feature behaves exactly as
# before and portfolio endpoints report PORTFOLIO_UNAVAILABLE.
# Portfolio data never changes evidence scores, Research View, snapshots,
# outcomes or event scoring, and is never written to the database.
# --------------------------------------------------------------------------
PORTFOLIO_AWARENESS_ENABLED: bool = os.getenv("PORTFOLIO_AWARENESS_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on")
RH_GATEWAY_URL: str = os.getenv("RH_GATEWAY_URL", "http://127.0.0.1:8787")  # must be loopback
RH_GATEWAY_TIMEOUT_SECONDS: float = float(os.getenv("RH_GATEWAY_TIMEOUT_SECONDS", "30"))
PORTFOLIO_ACCOUNT_ALIAS: str = os.getenv("PORTFOLIO_ACCOUNT_ALIAS", "holdings")  # alias only, never a number

# A quote older than this is labelled STALE (still shown, never silently treated as live).
PORTFOLIO_QUOTE_STALE_SECONDS: int = int(os.getenv("PORTFOLIO_QUOTE_STALE_SECONDS", "900"))
# Display rule only: above this |gap| the UI shows Robinhood-reported equity value and the
# calculated position value side by side with an explicit warning.
PORTFOLIO_VALUATION_GAP_MATERIAL_PCT: float = float(os.getenv("PORTFOLIO_VALUATION_GAP_MATERIAL_PCT", "0.5"))
PORTFOLIO_REALIZED_PNL_SPAN: str = os.getenv("PORTFOLIO_REALIZED_PNL_SPAN", "3month")

# Portfolio risk RULES are configuration-only and intentionally EMPTY until thresholds are reviewed.
# Format (JSON list): [{"id": "...", "scope": "position|sector|portfolio", "fact": "...",
#                       "op": ">=|>|<=|<|==", "value": 0.0, "label": "..."}]
PORTFOLIO_RISK_RULES_JSON: str = os.getenv("PORTFOLIO_RISK_RULES_JSON", "[]")


# --------------------------------------------------------------------------
# Stage 2.7D — portfolio ATTENTION policy (deterministic; not trading advice).
# Bands are "INFO,MEDIUM,HIGH" thresholds in PERCENT of the calculated
# portfolio value (leave a band empty, e.g. "10,,30", to disable it).
# A triggered rule means "this deserves attention", never "buy" or "sell".
# Policy results never change research evidence, Research View, snapshots,
# outcomes or event scoring, and are never written to the database.
# Effective only when PORTFOLIO_AWARENESS_ENABLED is also true.
# --------------------------------------------------------------------------
PORTFOLIO_POLICY_ENABLED: bool = os.getenv("PORTFOLIO_POLICY_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
PORTFOLIO_POLICY_POSITION_WEIGHT_PCT: str = os.getenv("PORTFOLIO_POLICY_POSITION_WEIGHT_PCT", "10,20,30")        # >=
PORTFOLIO_POLICY_SECTOR_WEIGHT_PCT: str = os.getenv("PORTFOLIO_POLICY_SECTOR_WEIGHT_PCT", "25,40,60")            # >=
PORTFOLIO_POLICY_LOW_CASH_PCT: str = os.getenv("PORTFOLIO_POLICY_LOW_CASH_PCT", "10,5,2")                        # <
PORTFOLIO_POLICY_HIGH_EVENT_EXPOSURE_PCT: str = os.getenv("PORTFOLIO_POLICY_HIGH_EVENT_EXPOSURE_PCT", "10,25,50")  # >=
PORTFOLIO_POLICY_VALUATION_GAP_PCT: str = os.getenv("PORTFOLIO_POLICY_VALUATION_GAP_PCT", "0.25,0.50,1.00")      # |gap| >=
# Quote / basis data-quality: INFO = any affected position (count >= 1); MEDIUM,HIGH = share of portfolio value.
PORTFOLIO_POLICY_UNRELIABLE_QUOTE_VALUE_PCT: str = os.getenv("PORTFOLIO_POLICY_UNRELIABLE_QUOTE_VALUE_PCT", ",10,25")
PORTFOLIO_POLICY_MISSING_BASIS_VALUE_PCT: str = os.getenv("PORTFOLIO_POLICY_MISSING_BASIS_VALUE_PCT", ",10,25")
# Comma-separated rule ids to switch off (see GET /api/portfolio/policy for the ids).
PORTFOLIO_POLICY_DISABLED_RULES: str = os.getenv("PORTFOLIO_POLICY_DISABLED_RULES", "")


# --------------------------------------------------------------------------
# Stage 2.7E — beginner decision SUPPORT and market context (display labels only).
# These cutoffs only choose plain-language LABELS (e.g. "Strong"/"Weak") from
# facts the app already computes. They never change evidence scores, Research
# View, event scoring or policy arithmetic, and nothing here is an instruction
# to trade. Existing constants are reused wherever one exists (see insights/labels.py).
# --------------------------------------------------------------------------
BEGINNER_RESEARCH_STALE_HOURS: float = float(os.getenv("BEGINNER_RESEARCH_STALE_HOURS", "24"))
BEGINNER_MOMENTUM_STRONG_SCORE: int = int(os.getenv("BEGINNER_MOMENTUM_STRONG_SCORE", "60"))  # momentum_score 0-100, 50 = neutral
BEGINNER_MOMENTUM_WEAK_SCORE: int = int(os.getenv("BEGINNER_MOMENTUM_WEAK_SCORE", "40"))
BEGINNER_BEARISH_MATERIAL_PCT: int = int(os.getenv("BEGINNER_BEARISH_MATERIAL_PCT", "30"))  # bearish evidence share
MARKET_TECH_PROXY_SYMBOL: str = os.getenv("MARKET_TECH_PROXY_SYMBOL", "QQQ")
MARKET_VOL_LOW_PCT: float = float(os.getenv("MARKET_VOL_LOW_PCT", "12"))        # SPY annualized 20-day volatility
MARKET_VOL_ELEVATED_PCT: float = float(os.getenv("MARKET_VOL_ELEVATED_PCT", "25"))
MARKET_BREADTH_STRONG_PCT: float = float(os.getenv("MARKET_BREADTH_STRONG_PCT", "60"))
MARKET_BREADTH_WEAK_PCT: float = float(os.getenv("MARKET_BREADTH_WEAK_PCT", "40"))
MARKET_LARGE_MOVER_SHARE_PCT: float = float(os.getenv("MARKET_LARGE_MOVER_SHARE_PCT", "10"))
MARKET_FEEDBACK_STALE_MINUTES: float = float(os.getenv("MARKET_FEEDBACK_STALE_MINUTES", "30"))
PORTFOLIO_RECONCILIATION_REALIZED_SPAN: str = os.getenv("PORTFOLIO_RECONCILIATION_REALIZED_SPAN", "all")


# --------------------------------------------------------------------------
# Stage 2.7F — Beginner command center: data FRESHNESS labels (FRESH / AGING / STALE).
# Display only. Each "stale" boundary reuses the existing stale setting for that data.
# --------------------------------------------------------------------------
FRESHNESS_MARKET_FRESH_MINUTES: float = float(os.getenv("FRESHNESS_MARKET_FRESH_MINUTES", "15"))   # stale: MARKET_FEEDBACK_STALE_MINUTES
FRESHNESS_PORTFOLIO_FRESH_SECONDS: float = float(os.getenv("FRESHNESS_PORTFOLIO_FRESH_SECONDS", "120"))
FRESHNESS_PORTFOLIO_STALE_SECONDS: float = float(os.getenv("FRESHNESS_PORTFOLIO_STALE_SECONDS", "900"))
FRESHNESS_RESEARCH_FRESH_HOURS: float = float(os.getenv("FRESHNESS_RESEARCH_FRESH_HOURS", "6"))    # stale: BEGINNER_RESEARCH_STALE_HOURS
FRESHNESS_QUOTE_FRESH_SECONDS: float = float(os.getenv("FRESHNESS_QUOTE_FRESH_SECONDS", "300"))    # stale: PORTFOLIO_QUOTE_STALE_SECONDS
AI_CALLS_PER_RESEARCH_RUN: int = 3   # catalyst + risk + narrative (agents/beginner_agent.py), used for the cost warning

# ------------------------------------------------------------------------------------------------------------------
# Stage 2.7G.1 — operational polish (no financial logic). Warm the Stage 2.6 event calendars (FRED macro releases,
# FOMC) in the background when the server starts, so the first dashboard load does not wait on a cold FRED fetch.
# Startup never waits for it, failures never block startup, and it makes 0 Claude calls.
EVENT_WARMUP_ON_STARTUP: bool = os.getenv("EVENT_WARMUP_ON_STARTUP", "true").strip().lower() in ("1", "true", "yes", "on")
EVENT_WARMUP_TIMEOUT_SECONDS: float = float(os.getenv("EVENT_WARMUP_TIMEOUT_SECONDS", "90"))   # still running after this -> reported unavailable (slow)
EVENT_WARMUP_RETRY_SECONDS: float = float(os.getenv("EVENT_WARMUP_RETRY_SECONDS", "300"))      # minimum gap between warm-up attempts
