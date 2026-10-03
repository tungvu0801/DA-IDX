"""
agents/usage_tracker.py — Records every AI-agent request (tokens/cost) and
enforces the configured per-hour/per-day call limits, so a bug or a busy
market day can't run up an unbounded Claude bill.

In-memory only for this milestone (resets on process restart) — enough for
an "AI usage today" dashboard panel on a single-process local server. Move
to a small SQLite table later if usage needs to persist across restarts.

Pricing math lives in one place here (estimate_cost_usd), reading from
config.AI_MODEL_PRICING, so a price change never requires touching any
calling code.

Stage 3.9: two independent budgets, decided by request_type (category_of):
EXPLANATION = Stage 3.8 strategy / evidence explanations; RESEARCH = every
other AI request (the pre-3.8 budget, unchanged). A call in one category never
counts against the other.
"""
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

import config

logger = logging.getLogger(__name__)

RESEARCH, EXPLANATION = "research", "explanation"
CATEGORIES = (RESEARCH, EXPLANATION)
_EXPLANATION_TYPES = ("strategy_explanation", "evidence_explanation")


def category_of(request_type: Optional[str]) -> str:
    """The budget a request type belongs to."""
    return EXPLANATION if str(request_type or "").startswith(_EXPLANATION_TYPES) else RESEARCH


def limits(category: str) -> Tuple[int, int]:
    """(hourly, daily) for one budget — read from config on every call, never copied."""
    if category == EXPLANATION:
        return config.AI_EXPLANATION_HOURLY_LIMIT, config.AI_EXPLANATION_DAILY_LIMIT
    return config.AI_MAX_CALLS_PER_HOUR, config.AI_MAX_CALLS_PER_DAY


@dataclass
class UsageRecord:
    timestamp: datetime
    symbol: str
    request_type: str  # e.g. "technical_analysis"
    model: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    success: bool = True
    error: Optional[str] = None


def estimate_cost_usd(
    model: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cache_creation_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
) -> Optional[float]:
    """
    Cost estimate from config.AI_MODEL_PRICING. Returns None if the model
    isn't in the pricing table — never guesses a price for an unknown model.
    """
    pricing = config.AI_MODEL_PRICING.get(model)
    if pricing is None:
        return None

    cost = 0.0
    if input_tokens:
        cost += input_tokens / 1_000_000 * pricing["input"]
    if output_tokens:
        cost += output_tokens / 1_000_000 * pricing["output"]
    if cache_creation_tokens:
        cost += cache_creation_tokens / 1_000_000 * pricing.get("cache_write", pricing["input"])
    if cache_read_tokens:
        cost += cache_read_tokens / 1_000_000 * pricing.get("cache_read", pricing["input"])
    return round(cost, 6)


class UsageTracker:
    """Thread-safe-enough in-memory log of AI requests, plus rate limiting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: List[UsageRecord] = []

    def counts(self, category: str = RESEARCH) -> dict:
        """Successful calls of ONE budget in the last hour and today (the counting rule can_call enforces)."""
        now = datetime.now(timezone.utc)
        with self._lock:
            mine = [r for r in self._records if r.success and category_of(r.request_type) == category]
        return {"last_hour": sum(1 for r in mine if now - r.timestamp < timedelta(hours=1)),
                "today": sum(1 for r in mine if r.timestamp.date() == now.date())}

    def can_call(self, category: str = RESEARCH) -> Tuple[bool, Optional[str]]:
        """
        Check the configured per-hour/per-day call limits of ONE budget BEFORE
        calling Claude. Only successful past calls count against the limit — a
        failed request shouldn't itself consume quota.
        """
        used = self.counts(category)
        hourly, daily = limits(category)
        what = "AI explanation" if category == EXPLANATION else "AI"
        if used["last_hour"] >= hourly:
            return False, f"Hourly {what} call limit reached ({hourly}/hour)."
        if used["today"] >= daily:
            return False, f"Daily {what} call limit reached ({daily}/day)."
        return True, None

    def record(self, entry: UsageRecord) -> None:
        with self._lock:
            self._records.append(entry)

    def summary_today(self, category: str = RESEARCH) -> dict:
        today: date = datetime.now(timezone.utc).date()
        _, daily_limit = limits(category)
        with self._lock:
            todays_records = [r for r in self._records if r.timestamp.date() == today and category_of(r.request_type) == category]

        calls = len(todays_records)
        input_tokens = sum(r.input_tokens or 0 for r in todays_records)
        output_tokens = sum(r.output_tokens or 0 for r in todays_records)
        cached_tokens = sum(r.cache_read_tokens or 0 for r in todays_records)
        cost = sum(r.estimated_cost_usd or 0.0 for r in todays_records)

        return {
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "estimated_cost_usd": round(cost, 4),
            "daily_call_limit": daily_limit,
            "calls_remaining_today": max(0, daily_limit - calls),
        }

    def budget(self, category: str) -> dict:
        """One budget for display: successful calls against its own limits (what can_call enforces)."""
        used = self.counts(category)
        hourly, daily = limits(category)
        ok, reason = self.can_call(category)
        s = self.summary_today(category)
        return {"category": category, "used_last_hour": used["last_hour"], "hourly_limit": hourly,
                "used_today": used["today"], "daily_limit": daily, "remaining_today": max(0, daily - used["today"]),
                "limit_reached": not ok, "reason": reason, "input_tokens": s["input_tokens"],
                "output_tokens": s["output_tokens"], "estimated_cost_usd": s["estimated_cost_usd"]}

    def budgets(self) -> dict:
        return {c: self.budget(c) for c in CATEGORIES}


usage_tracker = UsageTracker()
