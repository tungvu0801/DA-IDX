"""
portfolio/quotes.py — Deterministic quote selection and sanity assessment.

Selection rule (Robinhood's own documented guidance): of the regular-session
last trade and the extended/overnight last trade, use whichever has the more
recent VENUE timestamp. A price without a venue timestamp is never selected,
because its freshness cannot be verified. The selected price's venue timestamp
is the only timestamp reported — nothing is invented.

Quality (worst issue wins):
  OK          usable, no issues
  STALE       usable; selected trade older than PORTFOLIO_QUOTE_STALE_SECONDS
  DEGRADED    usable but flagged: crossed bid/ask, extended-hours-only price, regular
              price missing, or a candidate price missing its timestamp
  UNRELIABLE  NOT used for valuation: instrument state not active, or has_traded == false
  UNAVAILABLE NOT used: no quote, or no price with a verifiable timestamp
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from portfolio.models import Quote, QuoteAssessment

QUALITY_RANK = {"OK": 0, "STALE": 1, "DEGRADED": 2, "UNRELIABLE": 3, "UNAVAILABLE": 4}
ISSUE_QUALITY = {
    "STALE_QUOTE": "STALE",
    "CROSSED_QUOTE": "DEGRADED",
    "EXTENDED_HOURS_ONLY": "DEGRADED",
    "MISSING_LAST_TRADE": "DEGRADED",
    "MISSING_TIMESTAMP": "DEGRADED",
    "STATE_NOT_ACTIVE": "UNRELIABLE",
    "NOT_TRADED": "UNRELIABLE",
    "NO_QUOTE": "UNAVAILABLE",
    "NO_TIMESTAMPED_PRICE": "UNAVAILABLE",
}


def assess_quote(quote: Optional[Quote], now: datetime, stale_after_seconds: int) -> QuoteAssessment:
    if quote is None:
        return QuoteAssessment("UNAVAILABLE", False, None, None, None, None, False, ["NO_QUOTE"])

    issues: list[str] = []
    if quote.state is not None and quote.state != "active":
        issues.append("STATE_NOT_ACTIVE")
    if quote.has_traded is False:
        issues.append("NOT_TRADED")

    crossed = (quote.bid_price is not None and quote.ask_price is not None and quote.bid_price > 0
               and quote.ask_price > 0 and quote.bid_price > quote.ask_price)
    if crossed:
        issues.append("CROSSED_QUOTE")

    candidates = []
    for source, price, when in (("REGULAR_LAST", quote.last_trade_price, quote.last_trade_time),
                                ("EXTENDED_LAST", quote.last_non_reg_trade_price, quote.last_non_reg_trade_time)):
        if price is None or price <= 0:
            if source == "REGULAR_LAST":
                issues.append("MISSING_LAST_TRADE")
            continue
        if when is None:
            issues.append("MISSING_TIMESTAMP")
            continue
        candidates.append((when, source, price))

    if not candidates:
        issues.append("NO_TIMESTAMPED_PRICE")
        return QuoteAssessment(_quality(issues), False, None, None, None, None, crossed, _dedupe(issues))

    when, source, price = max(candidates, key=lambda c: (c[0], c[1] == "REGULAR_LAST"))
    if source == "EXTENDED_LAST" and not any(c[1] == "REGULAR_LAST" for c in candidates):
        issues.append("EXTENDED_HOURS_ONLY")
    age = int((now - when).total_seconds())
    if age > stale_after_seconds:
        issues.append("STALE_QUOTE")

    quality = _quality(issues)
    usable = QUALITY_RANK[quality] <= QUALITY_RANK["DEGRADED"]
    return QuoteAssessment(quality, usable, price if usable else None, when, source, max(age, 0), crossed,
                           _dedupe(issues))


def _quality(issues: list[str]) -> str:
    worst = "OK"
    for issue in issues:
        q = ISSUE_QUALITY[issue]
        if QUALITY_RANK[q] > QUALITY_RANK[worst]:
            worst = q
    return worst


def _dedupe(issues: list[str]) -> list[str]:
    return list(dict.fromkeys(issues))


def reference_close(quote: Optional[Quote]) -> Optional[Decimal]:
    """Day-change reference: adjusted previous close (Robinhood's documented basis for daily change)."""
    return None if quote is None else quote.adjusted_previous_close
