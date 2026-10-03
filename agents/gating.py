"""
agents/gating.py — Shared call-gating pipeline for every Claude-backed
agent in this app (Technical, Catalyst, Risk, and future ones). Extracted
from the original technical_agent.py so every agent follows one audited
pattern instead of several subtly-different copies.

Order of checks:
  1. Attention Score threshold — skipped only when `user_requested=True`
     (a user explicitly asked for this analysis, e.g. via chat or a
     dashboard click). Automatic/background analysis always respects it.
  2. AI analysis cache (agents.ai_cache), keyed by (symbol, analysis_type)
  3. Hourly/daily call limits (agents.usage_tracker) — apply regardless of
     whether the request was automatic or user-requested.
  4. The actual Claude call, with usage recording + cache storage.

Always returns a plain string; never raises. Callers only supply a
provider getter, a prompt builder, and identifying info — everything else
is identical across agents.
"""
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import config
from agents.ai_cache import CachedAnalysis, ai_cache
from agents.orchestrator import LLMProvider
from agents.usage_tracker import RESEARCH, UsageRecord, category_of, estimate_cost_usd, usage_tracker

logger = logging.getLogger(__name__)

UNAVAILABLE_MESSAGE = "AI analysis unavailable — no LLM provider configured."


def strip_markdown_fence(text: str) -> str:
    """Defensively strip ```json ... ``` fences some models add to JSON responses despite being told not to."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def run_gated_agent(
    *,
    symbol: str,
    price: float,
    attention_score: int,
    signal: str,
    analysis_type: str,
    system_prompt: str,
    get_provider_fn: Callable[[], Optional[LLMProvider]],
    build_prompt_fn: Callable[[], str],
    user_requested: bool = False,
    max_tokens: int = 1400,
) -> str:
    """
    Run the shared gate -> cache -> rate-limit -> call pipeline for one
    Claude-backed agent request. `build_prompt_fn` is only invoked if a
    real Claude call is actually going to happen (never built speculatively,
    so an expensive prompt-assembly step never runs needlessly).
    """
    if not user_requested and attention_score < config.AI_ANALYSIS_ATTENTION_THRESHOLD:
        return (
            f"AI analysis skipped — Attention Score ({attention_score}/100) is below the "
            f"configured threshold ({config.AI_ANALYSIS_ATTENTION_THRESHOLD}/100) used to limit "
            f"how often the AI agent runs automatically. Rule-based signal: {signal}."
        )

    provider = get_provider_fn()
    if provider is None:
        return UNAVAILABLE_MESSAGE

    cached = ai_cache.get_fresh(symbol, analysis_type, price, attention_score)
    if cached is not None:
        return cached.analysis + "\n\n(Cached analysis — nothing meaningful has changed since the last check.)"

    budget = category_of(analysis_type)                 # Stage 3.9: research and explanation budgets are independent
    can_call, limit_reason = usage_tracker.can_call() if budget == RESEARCH else usage_tracker.can_call(budget)
    if not can_call:
        return f"AI analysis unavailable right now — {limit_reason} Try again later."

    prompt = build_prompt_fn()
    try:
        response = provider.analyze(prompt, system_prompt, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001 - a provider failure must not crash the request
        logger.error("%s agent failed for %s: %s", analysis_type, symbol, exc)
        usage_tracker.record(
            UsageRecord(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                request_type=analysis_type,
                model=config.ANTHROPIC_MODEL,
                success=False,
                error=str(exc),
            )
        )
        return "AI analysis temporarily unavailable (provider error). The scanner and dashboard are unaffected."

    text = (response.text or "").strip()
    if not text:
        text = "AI analysis returned no content (unexpected response format from the provider)."

    cost = estimate_cost_usd(
        response.model,
        response.input_tokens,
        response.output_tokens,
        response.cache_creation_tokens,
        response.cache_read_tokens,
    )
    usage_tracker.record(
        UsageRecord(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol,
            request_type=analysis_type,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cache_creation_tokens=response.cache_creation_tokens,
            cache_read_tokens=response.cache_read_tokens,
            estimated_cost_usd=cost,
            success=True,
        )
    )
    ai_cache.store(
        CachedAnalysis(
            symbol=symbol,
            analysis_type=analysis_type,
            analyzed_at=datetime.now(timezone.utc),
            price_at_analysis=price,
            attention_score_at_analysis=attention_score,
            latest_news_id_at_analysis=None,
            analysis=text,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
    )
    return text
