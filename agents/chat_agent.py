"""
agents/chat_agent.py — Real AI chat with tool use.

Claude answers by calling application tools (agents/tools.py) for any
current market fact rather than relying on its own training knowledge,
which may be stale or simply wrong for live prices/news. Every tool call
made this way is USER-REQUESTED (the user asked the question), so it
bypasses the automatic Attention Score threshold — but the underlying
per-symbol AI cache and the shared hourly/daily call limits
(agents.usage_tracker) still apply exactly as everywhere else, since the
tool implementations in agents/tools.py already route through
agents.gating.run_gated_agent for technical/catalyst/risk analysis.

The chat call itself (the reasoning/tool-orchestration turns, on top of
whatever the tools themselves used) also counts against the shared usage
budget, tracked under request_type="chat".
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

import config
from agents.gating import UNAVAILABLE_MESSAGE
from agents.technical_agent import get_provider
from agents.tools import TOOL_REGISTRY, ToolResult
from agents.usage_tracker import UsageRecord, estimate_cost_usd, usage_tracker

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a beginner-friendly stock research chat assistant.

You have tools that fetch REAL, current data from this application (Alpaca market data plus
the app's own deterministic quant engine, Catalyst Agent, and Risk Agent). ALWAYS use a tool
for any current market fact — price, volume, indicators, signals, news, sector performance —
rather than your own training knowledge, which may be stale or simply wrong for live data. If
a tool reports it's unavailable, say so honestly; never guess a current price, news item, or
event.

Keep answers short and beginner-friendly. When discussing a specific stock, prefer this shape:
CURRENT EVIDENCE (bullish/neutral/bearish, from a tool — never invent these numbers yourself),
RESEARCH VIEW label, a couple of "what looks good" points, a couple of "be careful" points, and
one line on what to watch (a support/resistance level from the data). Avoid long technical
paragraphs and jargon.

Never recommend buying, selling, holding, or trimming. Never state a probability or confidence
level for future price movement — evidence percentages describe current evidence only, not a
forecast. If the user asks you to place a trade or execute an order, explain that this
assistant is analysis-only and never places trades.

When discussing events (earnings, CPI/PPI/jobs reports, FOMC meetings/decisions, corporate
actions): only discuss events actually returned by a tool call — never invent or infer a missing
event date or time, and never fill an earnings-date gap from your own training knowledge. If a
tool reports earnings data as unavailable, say so plainly. Never call a macro event (CPI, jobs
report, an FOMC decision) bullish or bearish by default — its market impact depends on the
reported data itself, which isn't known in advance. Keep "an event is scheduled," "what the
released data actually says," and "how the market might react" as three separate, explicit
ideas — never collapse them into a single prediction."""

# A focused subset of agents.tools.TOOL_REGISTRY exposed to the chat model —
# scan_market is a duplicate of get_market_overview and is omitted here to
# avoid confusing the model with two names for the same thing.
TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "get_market_overview",
        "description": "Current whole-market scan: top gainers, losers, unusual volume, momentum stocks, possible breakouts.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_stock_metrics",
        "description": "Full current computed metrics for one stock symbol: price, % change, RSI, EMA9/20/50, ATR, support/resistance, relative volume, signal, Attention Score.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "get_watchlist",
        "description": "The user's personal watchlist symbols and their current metrics.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_news",
        "description": "Recent real news articles for a stock symbol (title, source, timestamp, summary, url).",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "get_earnings",
        "description": "Upcoming earnings date for a symbol, if a data source is available. Currently always reports unavailable.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "get_events",
        "description": "Upcoming macro/economic events, if a data source is available. Currently always reports unavailable.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_sector_context",
        "description": "How a stock is performing today relative to its sector ETF, its peers, and the overall market (SPY).",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "run_technical_analysis",
        "description": "AI technical-analysis explanation (trend/momentum/volume/levels/conflicts) for one stock. Never a buy/sell verdict.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "run_catalyst_analysis",
        "description": "Real current news for a stock, classified by relevance and sentiment (POSITIVE/NEUTRAL/NEGATIVE/UNCERTAIN). Honestly reports 'no clear catalyst' if none found.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "run_risk_analysis",
        "description": "Deterministically-computed risk flags (high volatility, extended move, near resistance, weak volume confirmation, etc.) for a stock, with a plain-language explanation.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "save_research_snapshot",
        "description": "Save the current research analysis for a stock as a permanent record, so its price outcome can be measured later (1/3/5 trading days). Use only when the user explicitly asks to save, record, or track an analysis.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "get_upcoming_events",
        "description": "Real upcoming/recent company and macro events relevant to a stock (corporate actions, earnings status, CPI/PPI/jobs/PCE, FOMC meetings/decisions/minutes), plus the deterministic event-risk level.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "get_macro_events",
        "description": "Real upcoming/recent macro-economic events (CPI, PPI, Employment Situation, PCE, FOMC meetings/decisions/minutes), not specific to any one stock. CPI/PPI/Employment Situation/PCE require a FRED API key to be configured; reports unavailable honestly if not.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_company_events",
        "description": "Real corporate actions (splits, dividends, mergers, spin-offs) for a stock, plus whether earnings-date data is available (it currently is not, from any verified provider).",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
    {
        "name": "get_event_risk",
        "description": "The deterministic event-risk summary for a stock: nearest upcoming event, overall risk level (HIGH/MEDIUM/LOW/NONE), and the specific risk flags behind it. Severity is computed by Python, never by you.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
    },
]


def _summarize_event(e) -> Dict[str, Any]:
    return {
        "event_type": e.event_type.value,
        "category": e.category.value,
        "symbol": e.symbol,
        "title": e.title,
        "event_date": e.event_date.isoformat(),
        "time_precision": e.time_precision.value,
        "status": e.status.value,
        "importance": e.importance.value,
        "confirmed": e.confirmed,
        "source": e.source,
        "source_url": e.source_url,
    }


def _summarize_events_bundle(bundle) -> Dict[str, Any]:
    return {
        "event_risk_level": bundle.event_risk_level,
        "nearest_event": _summarize_event(bundle.nearest_event) if bundle.nearest_event is not None else None,
        "company_events": [_summarize_event(e) for e in bundle.company_events],
        "macro_events": [_summarize_event(e) for e in bundle.macro_events],
        "risk_flags": [{"code": f.code, "severity": f.severity, "description": f.description} for f in bundle.event_risk_flags],
        "data_quality": bundle.data_quality,
        "earnings_available": bundle.earnings_available,
        "earnings_reason": bundle.earnings_reason,
    }


def _summarize_metrics(m) -> Dict[str, Any]:
    return {
        "symbol": m.symbol,
        "price": m.price,
        "pct_change": round(m.pct_change, 2),
        "volume": m.volume,
        "relative_volume": m.relative_volume,
        "rsi": m.rsi,
        "ema9": m.ema_fast,
        "ema20": m.ema_medium,
        "ema50": m.ema_slow,
        "atr": m.atr,
        "support": m.support,
        "resistance": m.resistance,
        "trend": m.trend,
        "signal": m.signal,
        "attention_score": m.attention_score,
    }


def _serialize_tool_result(tool_name: str, result: ToolResult) -> Dict[str, Any]:
    """Turn a ToolResult (which may wrap a dataclass) into a compact JSON-safe dict for Claude."""
    if not result.available and result.data is None:
        return {"available": False, "reason": result.reason}

    data = result.data
    if tool_name in ("get_market_overview",):
        overview = data
        summarize_list = lambda items: [_summarize_metrics(m) for m in items[:8]]  # noqa: E731
        return {
            "available": True,
            "top_gainers": summarize_list(overview.top_gainers),
            "top_losers": summarize_list(overview.top_losers),
            "unusual_volume": summarize_list(overview.unusual_volume),
            "momentum_stocks": summarize_list(overview.momentum_stocks),
            "possible_breakouts": summarize_list(overview.possible_breakouts),
        }
    if tool_name == "get_stock_metrics":
        return {"available": True, **_summarize_metrics(data)}
    if tool_name == "get_watchlist":
        return {
            "available": True,
            "symbols": data["symbols"],
            "metrics": {sym: _summarize_metrics(m) for sym, m in data["metrics"].items()},
        }
    if tool_name == "get_news":
        return {
            "available": True,
            "articles": [
                {
                    "title": a.get("headline"),
                    "source": a.get("source"),
                    "published_at": str(a.get("created_at")) if a.get("created_at") else None,
                    "summary": a.get("summary"),
                    "url": a.get("url"),
                }
                for a in (data or [])[:10]
            ],
        }
    if tool_name in ("get_earnings", "get_events"):
        return {"available": result.available, "reason": result.reason, "data": data}
    if tool_name == "get_sector_context":
        ctx = data
        return {
            "available": True,
            "sector_name": ctx.sector_name,
            "sector_etf": ctx.sector_etf,
            "sector_pct_change": ctx.sector_pct_change,
            "stock_vs_sector_pct": ctx.stock_vs_sector_pct,
            "market_pct_change": ctx.market_pct_change,
        }
    if tool_name == "run_technical_analysis":
        return {"available": True, "analysis": data}
    if tool_name == "run_catalyst_analysis":
        analysis = data
        return {
            "available": True,
            "message": analysis.message or None,
            "items": [
                {"title": c.title, "sentiment": c.sentiment, "reason": c.reason, "source": c.source, "url": c.url}
                for c in analysis.items
            ],
        }
    if tool_name == "run_risk_analysis":
        return {
            "available": True,
            "flags": [
                {"code": f.code, "severity": f.severity, "description": f.description} for f in data["flags"]
            ],
            "explanation": data["explanation"],
        }
    if tool_name == "save_research_snapshot":
        return {"available": True, **data}
    if tool_name in ("get_upcoming_events", "get_event_risk"):
        return {"available": True, **_summarize_events_bundle(data)}
    if tool_name == "get_macro_events":
        return {
            "available": True,
            "fred_configured": data["fred_configured"],
            "events": [_summarize_event(e) for e in data["events"]],
        }
    if tool_name == "get_company_events":
        return {
            "available": True,
            "corporate_events": [_summarize_event(e) for e in data["corporate_events"]],
            "earnings_available": data["earnings_available"],
            "earnings_reason": data["earnings_reason"],
        }
    if tool_name == "get_earnings":
        return {"available": result.available, "reason": result.reason, "events": [_summarize_event(e) for e in (data or [])]}
    return {"available": bool(result.available), "reason": result.reason}


def _execute_tool(tool_name: str, tool_input: dict) -> Dict[str, Any]:
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return {"available": False, "reason": f"Unknown tool '{tool_name}'."}
    try:
        result = fn(**tool_input) if tool_input else fn()
    except Exception as exc:  # noqa: BLE001 - a broken tool must not crash the chat turn
        logger.error("Chat tool '%s' failed: %s", tool_name, exc)
        return {"available": False, "reason": f"Tool execution failed: {exc}"}
    return _serialize_tool_result(tool_name, result)


@dataclass
class ChatToolCall:
    tool: str
    input: dict
    available: bool


@dataclass
class ChatResult:
    answer: str
    tool_calls: List[ChatToolCall] = field(default_factory=list)
    ai_available: bool = True


def run_chat(user_message: str) -> ChatResult:
    """
    Answer `user_message` using Claude + the application's real tools.
    Always returns a ChatResult; never raises. Shares the same hourly/daily
    usage budget as every other agent (agents.usage_tracker) — a busy chat
    session and a busy technical-analysis session draw from the same pool.
    """
    provider = get_provider()
    if provider is None:
        return ChatResult(answer=UNAVAILABLE_MESSAGE, ai_available=False)

    can_call, limit_reason = usage_tracker.can_call()
    if not can_call:
        return ChatResult(answer=f"Chat is temporarily unavailable — {limit_reason} Try again later.", ai_available=True)

    tool_calls_made: List[ChatToolCall] = []

    def _tracked_execute(tool_name: str, tool_input: dict) -> Dict[str, Any]:
        result_dict = _execute_tool(tool_name, tool_input)
        tool_calls_made.append(
            ChatToolCall(tool=tool_name, input=tool_input, available=bool(result_dict.get("available", True)))
        )
        return result_dict

    try:
        response = provider.run_tool_loop(
            user_message=user_message,
            system_prompt=_SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            execute_tool=_tracked_execute,
            max_iterations=config.CHAT_MAX_TOOL_ITERATIONS,
        )
    except Exception as exc:  # noqa: BLE001 - a provider failure must not crash the chat endpoint
        logger.error("Chat tool-use loop failed: %s", exc)
        usage_tracker.record(
            UsageRecord(
                timestamp=datetime.now(timezone.utc),
                symbol="CHAT",
                request_type="chat",
                model=config.ANTHROPIC_MODEL,
                success=False,
                error=str(exc),
            )
        )
        return ChatResult(
            answer="Chat is temporarily unavailable (provider error). The scanner and dashboard are unaffected.",
            tool_calls=tool_calls_made,
            ai_available=True,
        )

    cost = estimate_cost_usd(
        response.model, response.input_tokens, response.output_tokens, response.cache_creation_tokens, response.cache_read_tokens
    )
    usage_tracker.record(
        UsageRecord(
            timestamp=datetime.now(timezone.utc),
            symbol="CHAT",
            request_type="chat",
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_creation_tokens=response.cache_creation_tokens,
            cache_read_tokens=response.cache_read_tokens,
            estimated_cost_usd=cost,
            success=True,
        )
    )

    text = (response.text or "").strip() or "I couldn't generate a response — please try rephrasing your question."
    return ChatResult(answer=text, tool_calls=tool_calls_made, ai_available=True)
