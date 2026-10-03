"""
insights/daily_explain.py — OPTIONAL Claude explanation of today's review (Stage 2.7G). Button-only.

Claude receives only the structured facts below (states, reasons, conflicts, events, changes) and runs
through the existing portfolio.explain pipeline: gating + AI cache + hourly/daily limits + the same
grounding guards (every number must appear in the facts; no buy/sell/sizing language, forecasts,
odds or abbreviated maths). The states were chosen by Python and cannot be changed by Claude.
"""
from __future__ import annotations

from typing import Callable, Optional

from agents.technical_agent import get_provider
from portfolio import explain as ex
from portfolio.models import to_jsonable

DAILY_SYSTEM_PROMPT = f"""You explain a beginner's DAILY PORTFOLIO + WATCHLIST REVIEW in plain, friendly language.

You are given a JSON object of facts computed by the application from read-only brokerage data, market data,
saved research and scheduled events. Follow these rules exactly:

{ex._COMMON_RULES}
- Every "state" (for example "REVIEW POSITION SIZE" or "WAIT FOR PULLBACK") and every conflict was decided by
  the application's deterministic rules. Report them exactly; never change, rank, add or remove a state, and never
  pick a "best" stock.
- Keep CURRENT EVIDENCE separate from history. Behavioural patterns and past outcomes are context only.
- Anything marked unavailable, missing or stale must be described that way.
- Be concise: short sentences a beginner understands.

Respond with ONLY a JSON object with exactly these keys:
  "what_changed": array of 0-3 short statements (empty if nothing changed),
  "stable_supportive": array of 0-3 short statements,
  "needs_attention": array of 1-4 short statements,
  "watchlist_setups": array of 0-3 short statements,
  "important_events": array of 0-2 short statements,
  "monitor_next": array of 1-3 short statements (conditions to watch, never instructions).
No prose before or after the JSON."""
DAILY_KEYS = ("what_changed", "stable_supportive", "needs_attention", "watchlist_setups", "important_events",
              "monitor_next")


def build_daily_facts(report: dict) -> dict:
    def owned(c):
        pos = c.get("position") or {}
        return {"symbol": c["symbol"], "state": c["state"], "summary": c["summary"],
                "weight_pct": pos.get("weight"), "open_pnl_pct": pos.get("open_pnl_pct"),
                "supports": c["why"]["supports"], "cautions": c["why"]["cautions"],
                "conflicts": [x["text"] for x in c["details"]["conflicts"]],
                "evidence": c["details"]["evidence"]}

    def watch(c):
        return {"symbol": c["symbol"], "state": c["state"], "waiting_for": c["waiting_for"],
                "supports": c["why"]["supports"], "cautions": c["why"]["cautions"],
                "ownership": {True: "owned", False: "not owned", None: "unknown (Robinhood not connected)"}[c["owned"]]}
    m = report["market"]
    groups = report["portfolio"]["groups"] or {}
    cov = report["portfolio"].get("coverage")
    return to_jsonable({
        "market": {"available": m.get("available"), "conditions": m.get("conditions"), "verdicts": m.get("verdicts"),
                   "next_event": {k: (m.get("next_event") or {}).get(k) for k in ("title", "date", "days_until")}
                   if m.get("next_event") else None},
        "robinhood_connected": report["robinhood"].get("connected"),
        "portfolio": {g: [owned(c) for c in cards] for g, cards in groups.items()},
        "research_coverage": None if not cov else {"holdings": cov["total"], "with_current_research": cov["current"],
                                                   "need_research": cov["need"]},
        "watchlist": {g: [watch(c) for c in cards] for g, cards in report["watchlist"]["groups"].items()},
        "watchlist_also_owned": report["watchlist"]["also_owned"],
        "top_things_to_review": [a["text"] for a in report["attention"]],
        "changes_since_last_check": report["changes"],
    })


def explain_daily(report: dict, provider_fn: Optional[Callable] = None) -> dict:
    return ex._run("DAILY", 0.0, "daily_review_explanation", DAILY_SYSTEM_PROMPT, build_daily_facts(report),
                   provider_fn or get_provider, DAILY_KEYS)
