"""
agents/tools.py — Application "tools" exposed to Claude via the AI chat
tool-use loop (agents/chat_agent.py), and reused directly by the beginner
research-view builder (agents/beginner_agent.py).

Each function wraps a REAL, already-implemented data source (scanner/,
data/, analysis/, agents/) — never duplicates the underlying logic — or is
explicitly marked unavailable via ToolResult(available=False, ...) when no
real data source exists yet. Nothing here ever fabricates data to make a
tool look functional.

Every tool call here is treated as USER-REQUESTED (a person asked a
question, or clicked something, that led here), so the Attention Score
threshold that gates *automatic* analysis is bypassed — but the AI cache
and hourly/daily call limits (agents.gating / agents.usage_tracker) still
apply exactly as they do everywhere else.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import config
from agents.catalyst_agent import get_catalyst_analysis
from agents.risk_agent import get_risk_analysis
from analysis.risk_flags import compute_risk_flags
from analysis.sector_context import compute_sector_context
from data.events.corporate import get_company_events as fetch_corporate_events
from data.events.earnings import get_earnings_provider
from data.events.fomc import get_fomc_events
from data.events.macro import get_macro_events as fetch_macro_events
from data.news import get_recent_news
from scanner.market_scanner import get_data_client, scan_market
from scanner.watchlist import analyze_watchlist, load_watchlist
from services.event_context import build_event_context


@dataclass
class ToolResult:
    """Uniform result shape for every tool below."""

    available: bool
    data: Any = None
    reason: Optional[str] = None  # set when available=False


def get_market_overview() -> ToolResult:
    """Whole-market scan results (top gainers/losers/unusual volume/etc)."""
    try:
        overview = scan_market()
    except RuntimeError as exc:
        return ToolResult(available=False, reason=str(exc))
    return ToolResult(available=True, data=overview)


def scan_market_tool() -> ToolResult:
    """Alias of get_market_overview, kept as its own named tool for the planned tool surface."""
    return get_market_overview()


def get_stock_metrics(symbol: str) -> ToolResult:
    """Full computed metrics (TickerMetrics) for one symbol."""
    symbol = symbol.strip().upper()
    try:
        client = get_data_client()
    except RuntimeError as exc:
        return ToolResult(available=False, reason=str(exc))
    metrics = analyze_watchlist(client, [symbol])
    m = metrics.get(symbol)
    if m is None:
        return ToolResult(available=False, reason=f"No data found for '{symbol}'.")
    return ToolResult(available=True, data=m)


def get_watchlist() -> ToolResult:
    """Current watchlist symbols + their computed metrics."""
    symbols = load_watchlist()
    try:
        client = get_data_client()
    except RuntimeError as exc:
        return ToolResult(available=False, reason=str(exc))
    metrics = analyze_watchlist(client, symbols)
    return ToolResult(available=True, data={"symbols": symbols, "metrics": metrics})


def get_news(symbol: str) -> ToolResult:
    """Recent news for a symbol, from Alpaca's real news feed."""
    return ToolResult(available=True, data=get_recent_news(symbol))


def get_earnings(symbol: str) -> ToolResult:
    """Real interface (Stage 2.6) — no verified earnings-calendar provider exists yet, so this
    always reports unavailable with a clear reason. Never scraped, never inferred, never guessed."""
    symbol = symbol.strip().upper()
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=config.EVENT_LOOKBACK_DAYS)).date()
    end = (now + timedelta(days=config.EVENT_LOOKAHEAD_DAYS)).date()
    result = get_earnings_provider().get_upcoming_earnings(symbol, start, end)
    return ToolResult(available=result.available, data=result.events, reason=result.reason)


def get_events() -> ToolResult:
    """Real (Stage 2.6) — upcoming/recent macro events (FOMC always; CPI/PPI/Employment Situation/PCE
    only if FRED_API_KEY is configured). See get_macro_events() below for the richer version."""
    return get_macro_events()


def get_sector_context(symbol: str) -> ToolResult:
    """Real — sector/market performance context (analysis/sector_context.py). Unavailable if the symbol isn't in the curated sector map."""
    metrics_result = get_stock_metrics(symbol)
    if not metrics_result.available:
        return metrics_result
    try:
        client = get_data_client()
    except RuntimeError as exc:
        return ToolResult(available=False, reason=str(exc))
    context = compute_sector_context(symbol, metrics_result.data.pct_change, client)
    if context is None:
        return ToolResult(available=False, reason=f"'{symbol.upper()}' is not in the curated sector map.")
    return ToolResult(available=True, data=context)


def run_technical_analysis(symbol: str) -> ToolResult:
    """Real — runs the Technical Agent, user-requested (bypasses the automatic Attention Score threshold)."""
    from agents.technical_agent import get_technical_analysis  # local import avoids a cycle at module load

    metrics_result = get_stock_metrics(symbol)
    if not metrics_result.available:
        return metrics_result
    return ToolResult(available=True, data=get_technical_analysis(metrics_result.data, user_requested=True))


def run_catalyst_analysis(symbol: str) -> ToolResult:
    """Real — runs the Catalyst Agent, user-requested (bypasses the automatic Attention Score threshold)."""
    metrics_result = get_stock_metrics(symbol)
    if not metrics_result.available:
        return metrics_result
    m = metrics_result.data
    analysis = get_catalyst_analysis(m.symbol, m.attention_score, m.price, m.signal, user_requested=True)
    return ToolResult(available=True, data=analysis)


def run_risk_analysis(symbol: str) -> ToolResult:
    """Real — computes risk flags (Python) and explains them via the Risk Agent, user-requested."""
    metrics_result = get_stock_metrics(symbol)
    if not metrics_result.available:
        return metrics_result
    m = metrics_result.data

    sector_result = get_sector_context(symbol)
    sector = sector_result.data if sector_result.available else None

    try:
        events = build_event_context(m.symbol)
    except Exception:  # noqa: BLE001 - the event layer must never break this tool
        events = None
    flags = compute_risk_flags(m, sector, event_flags=events.event_risk_flags if events is not None else None)

    caveat = (
        events.earnings_reason
        if events is not None and not events.earnings_available and events.earnings_reason
        else "Earnings-calendar data is not currently available from a verified provider."
    )
    explanation = get_risk_analysis(
        m.symbol, m.attention_score, m.price, m.signal, flags, data_caveats=[caveat], user_requested=True
    )
    return ToolResult(available=True, data={"flags": flags, "explanation": explanation})


def get_upcoming_events(symbol: str) -> ToolResult:
    """Real — company (corporate actions + earnings-status) and macro events relevant to `symbol`."""
    symbol = symbol.strip().upper()
    events = build_event_context(symbol)
    return ToolResult(available=True, data=events)


def get_macro_events() -> ToolResult:
    """Real — upcoming/recent macro events (CPI/PPI/Employment Situation/PCE/FOMC), symbol-independent."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=config.EVENT_LOOKBACK_DAYS)).date()
    end = (now + timedelta(days=config.EVENT_LOOKAHEAD_DAYS)).date()
    fred_events = fetch_macro_events(start, end)
    fomc_events = get_fomc_events(start, end)
    return ToolResult(
        available=True,
        data={
            "events": fred_events + fomc_events,
            "fred_configured": config.has_fred_credentials(),
        },
    )


def get_company_events(symbol: str) -> ToolResult:
    """Real — corporate actions (splits/dividends/mergers/spin-offs) + earnings status for `symbol`."""
    symbol = symbol.strip().upper()
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=config.EVENT_LOOKBACK_DAYS)).date()
    end = (now + timedelta(days=config.EVENT_LOOKAHEAD_DAYS)).date()
    corporate = fetch_corporate_events(symbol, start, end)
    earnings_result = get_earnings_provider().get_upcoming_earnings(symbol, start, end)
    return ToolResult(
        available=True,
        data={
            "corporate_events": corporate,
            "earnings_available": earnings_result.available,
            "earnings_reason": earnings_result.reason,
        },
    )


def get_event_risk(symbol: str) -> ToolResult:
    """Real — the deterministic event-risk summary (nearest event, risk level, risk flags) for `symbol`."""
    symbol = symbol.strip().upper()
    events = build_event_context(symbol)
    return ToolResult(available=True, data=events)


def save_research_snapshot(symbol: str) -> ToolResult:
    """
    Generates a fresh beginner research bundle for `symbol` and immediately
    saves it as an immutable snapshot (Stage 2.5). Unlike the dashboard's
    "Save Research Snapshot" button — which saves an analysis the user is
    already looking at, with zero new Claude calls — there is no prior
    displayed analysis in a chat context to preserve, so generating one
    now and saving it in the same step is the correct, intentional
    behavior here (the user explicitly asked the assistant to save one).
    """
    from agents.beginner_agent import build_beginner_research
    from analysis.research_setup import compute_research_setup
    from services.snapshot_builder import save_snapshot_from_bundle

    symbol = symbol.strip().upper()
    try:
        bundle = build_beginner_research(symbol, user_requested=True)
    except RuntimeError as exc:
        return ToolResult(available=False, reason=str(exc))
    if bundle is None:
        return ToolResult(available=False, reason=f"No data found for '{symbol}'.")

    setup = compute_research_setup(bundle.metrics)
    snapshot_id, created = save_snapshot_from_bundle(symbol, bundle, setup)
    return ToolResult(
        available=True,
        data={
            "snapshot_id": snapshot_id,
            "created": created,
            "message": "Snapshot saved." if created else "An identical snapshot was already saved.",
        },
    )


# Registry so the chat tool-use loop has one place to enumerate what's
# available, without re-deriving it from this module's contents.
TOOL_REGISTRY: Dict[str, Callable[..., ToolResult]] = {
    "get_market_overview": get_market_overview,
    "scan_market": scan_market_tool,
    "get_stock_metrics": get_stock_metrics,
    "get_watchlist": get_watchlist,
    "get_news": get_news,
    "get_earnings": get_earnings,
    "get_events": get_events,
    "get_sector_context": get_sector_context,
    "run_technical_analysis": run_technical_analysis,
    "run_catalyst_analysis": run_catalyst_analysis,
    "run_risk_analysis": run_risk_analysis,
    "save_research_snapshot": save_research_snapshot,
    "get_upcoming_events": get_upcoming_events,
    "get_macro_events": get_macro_events,
    "get_company_events": get_company_events,
    "get_event_risk": get_event_risk,
}
