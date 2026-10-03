"""
agents/technical_agent.py — Turns a stock's structured technical metrics
into a plain-English explanation.

Architecture reminder (see project docs): Alpaca + Python own every
factual number (price, volume, RSI, EMA, ATR, support/resistance,
Attention Score, the rule-based signal). Claude never calculates any of
that and is never allowed to substitute its own knowledge for it — it only
interprets the structured data it's handed, which is why the prompt below
serializes that data as JSON rather than prose, and explicitly instructs
the model not to invent values.

This agent explains the technical picture only: structure, volume, levels,
volatility, and conflicts between signals. It does NOT issue or imply a
buy/sell/hold/trim verdict — combining this with catalyst and risk context
into an actionable conclusion is future work, once those agents exist to
responsibly inform it.

Call gating (attention threshold -> cache -> rate limits -> call) lives in
agents/gating.py and is shared with the Catalyst and Risk agents — see that
module for the exact order and configuration.

If no LLM provider is configured (no ANTHROPIC_API_KEY in .env), or a call
is skipped/blocked/fails, this always returns a clear, non-crashing message
— the rest of the app (scanner, watchlist, dashboard) keeps working either way.
"""
import json
import logging
from typing import Any, Callable, Dict, List, Optional

import config
from agents.gating import UNAVAILABLE_MESSAGE, run_gated_agent
from agents.orchestrator import LLMProvider, LLMResponse
from analysis.indicators import TickerMetrics

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a technical-analysis assistant for a personal stock research dashboard.

You will be given structured, machine-computed market data as JSON. Every value in it was \
computed by the application (Alpaca market data + a Python quant engine) — treat it as the \
complete and only source of truth. Do not use your own knowledge of this stock's current \
price, volume, news, earnings dates, or recent events, and do not silently replace or \
"correct" any value in the JSON with a number from your own knowledge. If a field is null, \
say that data wasn't available for it — never estimate or guess a replacement value.

Structure your response in exactly these five labeled sections:

TECHNICAL STRUCTURE — trend, momentum, EMA structure (9/20/50 alignment).
VOLUME — relative volume, volume expansion, whether volume confirms the price move.
LEVELS — nearest support, nearest resistance, and breakout/breakdown areas only if the \
data actually supports them (don't invent a level that isn't in the data).
VOLATILITY — ATR, historical volatility, whether the move looks unusually extended.
CONFLICTS — bullish vs. bearish signals, momentum vs. overextension, trend vs. resistance, \
volume confirmation or lack of it. If there are no real conflicts, say so briefly.

Do not recommend buying, selling, holding, or trimming. Do not predict future price \
direction. Do not state a confidence level, probability, or price target. Only describe \
what the data shows."""


class AnthropicProvider(LLMProvider):
    """Reference LLMProvider implementation backed by the Anthropic API."""

    def __init__(self, api_key: str, model: str, workspace_id: str = ""):
        from anthropic import Anthropic  # local import: optional dependency

        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self._client = Anthropic(api_key=api_key, default_headers=headers)
        self._model = model

    def analyze(self, prompt: str, system_prompt: str, max_tokens: int = 1400) -> LLMResponse:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if hasattr(block, "text"))
        usage = response.usage
        return LLMResponse(
            text=text,
            model=response.model,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", None),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
        )

    def chat(self, message: str, history: Optional[List[dict]] = None) -> LLMResponse:
        raise NotImplementedError("Plain (non-tool) chat is not used by anything yet; see run_tool_loop.")

    def run_tool_loop(
        self,
        user_message: str,
        system_prompt: str,
        tools: List[Dict[str, Any]],
        execute_tool: Callable[[str, dict], Dict[str, Any]],
        max_iterations: int = 5,
    ) -> LLMResponse:
        messages: List[dict] = [{"role": "user", "content": user_message}]
        total_input = total_output = total_cache_creation = total_cache_read = 0
        model_used = self._model

        for _ in range(max_iterations):
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1500,
                system=system_prompt,
                messages=messages,
                tools=tools,
            )
            model_used = response.model
            usage = response.usage
            total_input += getattr(usage, "input_tokens", None) or 0
            total_output += getattr(usage, "output_tokens", None) or 0
            total_cache_creation += getattr(usage, "cache_creation_input_tokens", None) or 0
            total_cache_read += getattr(usage, "cache_read_input_tokens", None) or 0

            if response.stop_reason != "tool_use":
                text = "".join(block.text for block in response.content if hasattr(block, "text"))
                return LLMResponse(
                    text=text,
                    model=model_used,
                    input_tokens=total_input,
                    output_tokens=total_output,
                    cache_creation_tokens=total_cache_creation,
                    cache_read_tokens=total_cache_read,
                )

            messages.append({"role": "assistant", "content": response.content})
            tool_result_blocks = []
            for block in response.content:
                if getattr(block, "type", None) == "tool_use":
                    try:
                        result = execute_tool(block.name, block.input or {})
                        content_str = json.dumps(result, default=str)
                    except Exception as exc:  # noqa: BLE001 - a bad tool call must not abort the whole loop
                        content_str = json.dumps({"available": False, "reason": f"Tool error: {exc}"})
                    tool_result_blocks.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": content_str}
                    )
            messages.append({"role": "user", "content": tool_result_blocks})

        return LLMResponse(
            text=(
                "I wasn't able to finish answering that within the allowed number of tool calls "
                f"({max_iterations}) — try a more specific question."
            ),
            model=model_used,
            input_tokens=total_input,
            output_tokens=total_output,
            cache_creation_tokens=total_cache_creation,
            cache_read_tokens=total_cache_read,
        )


def get_provider() -> Optional[LLMProvider]:
    """Return the configured LLMProvider, or None if no key is set / init fails."""
    if not config.has_llm_credentials():
        return None
    try:
        return AnthropicProvider(
            config.ANTHROPIC_API_KEY,
            model=config.ANTHROPIC_MODEL,
            workspace_id=config.ANTHROPIC_WORKSPACE_ID,
        )
    except Exception as exc:  # noqa: BLE001 - never let a bad provider crash the app
        logger.error("Failed to initialize LLM provider: %s", exc)
        return None


def _build_structured_input(m: TickerMetrics) -> dict:
    """
    Build the exact structured payload handed to Claude — JSON, not prose,
    so every factual value is unambiguous and traceable back to
    analysis.indicators.TickerMetrics. Nothing here is computed by Claude.
    """
    return {
        "symbol": m.symbol,
        "timestamp": m.as_of.isoformat() if hasattr(m.as_of, "isoformat") else str(m.as_of),
        "price_source": m.price_source,
        "market_data": {
            "price": m.price,
            "daily_change_percent": round(m.pct_change, 4),
            "volume": m.volume,
            "avg_volume": m.avg_volume,
            "relative_volume": m.relative_volume,
        },
        "technical": {
            "rsi": m.rsi,
            "ema9": m.ema_fast,
            "ema20": m.ema_medium,
            "ema50": m.ema_slow,
            "atr": m.atr,
            "momentum_5d": m.momentum_5d_pct,
            "momentum_10d": m.momentum_10d_pct,
            "support": [m.support] if m.support is not None else [],
            "resistance": [m.resistance] if m.resistance is not None else [],
            "high_20d": m.high_20d,
            "low_20d": m.low_20d,
            "volatility": m.volatility_pct,
            "volatility_expansion": m.volatility_expansion,
            "volume_expansion": m.volume_expansion,
        },
        "scanner": {
            "signal": m.signal,
            "attention_score": m.attention_score,
        },
    }


def _build_prompt(m: TickerMetrics) -> str:
    structured = _build_structured_input(m)
    return (
        "Analyze the following stock using ONLY the structured data below. "
        "Do not use outside knowledge of this stock.\n\n"
        + json.dumps(structured, indent=2, default=str)
    )


def get_technical_analysis(m: TickerMetrics, user_requested: bool = False) -> str:
    """
    Return a plain-English technical explanation for `m` via the shared
    gating pipeline (agents.gating.run_gated_agent): Attention Score
    threshold (skipped if `user_requested`), analysis cache, then hourly/
    daily call limits. Always returns a clear string; never raises, and
    never issues a buy/sell verdict.
    """
    return run_gated_agent(
        symbol=m.symbol,
        price=m.price,
        attention_score=m.attention_score,
        signal=m.signal,
        analysis_type="technical",
        system_prompt=_SYSTEM_PROMPT,
        get_provider_fn=get_provider,
        build_prompt_fn=lambda: _build_prompt(m),
        user_requested=user_requested,
    )
