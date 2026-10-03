"""
insights/daily_review.py — TODAY'S PORTFOLIO + WATCHLIST REVIEW (Stage 2.7G): assembles the report.

`assemble()` is pure: every input (market, Robinhood view, metrics, events, research, policy, patterns,
outcomes) is fetched ONCE by the caller and shared across all stocks — no per-stock market scans. The state
of each stock comes from insights.decision; this module only groups, orders (alphabetically — never by
expected return) and formats. Helper functions at the bottom read the AI cache / usage records and the
Stage 2.5 outcome table (SELECT only) without changing either.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import config
from insights import decision as D
from insights.plain import portfolio_freshness

RESEARCH_ANALYSIS_TYPES = ("catalyst", "risk", "beginner_narrative")   # the Claude calls of one research run
OUTCOME_HORIZON = max(config.TRADING_DAY_HORIZONS)
DISCLAIMER = ("Decision support from current evidence only — not buy/sell instructions, not predictions and not "
              "a ranking by expected return. No order can be created from this page.")


def _research_word(r: dict) -> str:
    if not r["available"]:
        return "Research needed"
    if not r["current"]:
        return f"Old research ({r['age_hours']} h) — refresh"
    return r["view"].title()


def evidence_words(E: dict) -> dict:
    loc = D.LOCATION_WORD.get(E["location"], "Unavailable")
    return {"market": D.MARKET_WORD.get(E["market_env"], "Unavailable"),
            "stock_trend": D.TREND_WORD.get(E["trend"], "Unavailable"),
            "momentum": D.MOMENTUM_WORD.get(E["momentum"], "Unavailable"),
            "research": _research_word(E["research"]),
            "price_location": loc + (" · extended" if E["extended"] else ""),
            "sector": "Unclassified" if E["sector"] == "UNCLASSIFIED" else
            f"{E['sector']}: {D.SECTOR_WORD.get(E['sector_label'], 'Unavailable')}",
            "event_risk": D.EVENT_WORD.get(E["event_risk"], "Unavailable"),
            "portfolio_fit": E["portfolio"]["fit"]}


def card(E: dict, d: dict, *, pattern: Optional[dict], history: Optional[dict]) -> dict:
    """Compact beginner card + everything else under details (advanced)."""
    return {
        "symbol": d["symbol"], "owned": E["owned"], "state": d["state"], "summary": d["layers"]["summary"],
        "group": d["group"], "next": d["next"], "waiting_for": d["waiting_for"],
        "why": {"supports": d["supports"][:2], "cautions": d["cautions"][:3]},
        "position": E["position"], "price": E["price"], "pct_today": None if E["pct_today"] is None
        else round(E["pct_today"], 2),
        "watch_next": d["watch_next"],
        "details": {
            "evidence": evidence_words(E), "layers": d["layers"], "conflicts": d["conflicts"],
            "all_supports": d["supports"], "all_cautions": d["cautions"],
            "improves_if": d["improves_if"], "weakens_if": d["weakens_if"], "rule": d["rule"],
            "freshness": E["freshness"], "quote": E["quote"],
            "research": {k: E["research"][k] for k in ("view", "bullish_pct", "neutral_pct", "bearish_pct", "age_hours",
                                                       "freshness", "source", "catalyst", "category_scores")},
            "technicals": E["technicals"], "support": E["support"], "resistance": E["resistance"],
            "policy": {k: E["portfolio"][k] for k in ("position_severity", "sector_severity", "sector_weight")},
            "sector": E["sector"], "sector_pct_today": E["sector_pct"],
            "events": {"event_risk": E["event_risk"], "next_event": E["next_event"],
                       "earnings_note": E["earnings_note"], "available": E["events_available"]},
            "pattern_context": pattern, "historical_outcomes": history,
        },
    }


def _grouped(cards: List[dict], groups: Dict[str, list]) -> Dict[str, List[dict]]:
    out = {g: [] for g in groups}
    for c in cards:
        out[c["group"]].append(c)
    for g in out.values():
        g.sort(key=lambda c: c["symbol"])          # alphabetical inside groups — never a ranking
    return out


def assemble(*, now: datetime, market: dict, market_block: dict, connection: dict, view: Any,
             watchlist_symbols: List[str], metrics: Dict[str, Any], events: Dict[str, Any], research: Dict[str, Any],
             sector_of: Callable[[str], Optional[str]], sector_pct: Dict[str, float], concerns: Dict[str, dict],
             sector_flags: Dict[str, dict], patterns: Optional[dict], outcomes: Dict[str, Any],
             market_ref_time: Any, previous: Optional[dict], research_estimates: Dict[str, int],
             usage_counts: dict, events_loading: bool = False) -> dict:
    connected = view is not None
    positions = {p.symbol: p for p in view.positions} if connected else {}
    evidence, owned_d, watch_d = {}, [], []

    def ev(sym, owned):
        sec = positions[sym].sector if sym in positions else (sector_of(sym) or "UNCLASSIFIED")
        flag = sector_flags.get(sec)
        return D.build_evidence(
            symbol=sym, owned=owned, metrics=metrics.get(sym), position=positions.get(sym), research=research.get(sym),
            events_bundle=events.get(sym), market=market, sector=sec, sector_pct=sector_pct.get(sec),
            concern=concerns.get(sym), sector_severity=flag["severity"] if flag else None,
            market_ref_time=market_ref_time, portfolio_available=connected, now=now)

    for sym in sorted(positions):
        evidence[sym] = ev(sym, True)
        owned_d.append(D.owned_state(evidence[sym]))
    watch_only = [s for s in watchlist_symbols if s not in positions]
    for sym in watch_only:
        evidence[sym] = ev(sym, None if not connected else False)
        watch_d.append(D.watchlist_state(evidence[sym]))

    def history(sym):
        return D.outcome_context(outcomes.get(sym), OUTCOME_HORIZON)
    owned_cards = [card(evidence[d["symbol"]], d, pattern=D.pattern_context(evidence[d["symbol"]], patterns),
                        history=history(d["symbol"])) for d in owned_d]
    watch_cards = [card(evidence[d["symbol"]], d, pattern=D.pattern_context(evidence[d["symbol"]], patterns),
                        history=history(d["symbol"])) for d in watch_d]

    # research coverage: largest positions first (attention need, never expected return)
    by_weight = sorted(positions.values(), key=lambda p: p.portfolio_weight or Decimal("-1"), reverse=True)
    need = [p.symbol for p in by_weight if not evidence[p.symbol]["research"]["current"]]
    coverage = None
    if connected:
        plan = D.research_plan(need, {s: research_estimates.get(s, config.AI_CALLS_PER_RESEARCH_RUN) for s in need},
                               usage_counts["last_hour"], usage_counts["today"])
        coverage = {"total": len(positions), "current": len(positions) - len(need), "need": need, "plan": plan}

    high_sector = [{"subject": s, "value": f["value"]} for s, f in sorted(sector_flags.items())
                   if f["severity"] == "HIGH"] if connected else []
    snapshot = D.compare_snapshot(owned_d, watch_d, evidence, market_block)
    also_owned = [s for s in watchlist_symbols if s in positions]
    return {
        "now": now.isoformat(timespec="seconds"),
        "market": {k: market_block.get(k) for k in ("available", "conditions", "verdicts", "next_event", "freshness",
                                                     "big_picture", "message")},
        "robinhood": connection,
        "portfolio": {"available": connected, "groups": _grouped(owned_cards, D.OWNED_GROUPS) if connected else None,
                      "count": len(owned_cards), "coverage": coverage,
                      "freshness": portfolio_freshness(view.freshness["positions"].get("fetched_at"), now)
                      if connected else None},
        "watchlist": {"groups": _grouped(watch_cards, D.WATCH_GROUPS), "count": len(watch_cards),
                      "also_owned": also_owned, "ownership_known": connected,
                      "note": None if connected else "Robinhood is not connected, so ownership is unknown and the "
                                                     "portfolio layer is unavailable (not assumed safe)."},
        "attention": D.attention_items(owned_d, watch_d, market_block, high_sector, coverage),
        "changes": D.changes_since(previous, snapshot),
        "snapshot": snapshot,
        "pattern_available": bool(patterns and patterns.get("available")),
        "events_note": ("Event calendars are still loading, so event risk is shown as unknown for now. Press Refresh "
                        "in a moment.") if events_loading else None,
        "disclaimer": DISCLAIMER,
    }


# ---- helpers that READ existing state (nothing is changed) ----------------------------------------------------

def ai_usage_counts(tracker, category: str = "research") -> dict:
    """Same counting rule as UsageTracker.can_call (successful calls only), read without modifying the tracker.
    Stage 3.9: ONE budget — research by default, so Strategy / Evidence explanations never reduce research capacity."""
    from agents.usage_tracker import category_of
    now = datetime.now(timezone.utc)
    with tracker._lock:
        records = [r for r in tracker._records if category_of(r.request_type) == category]
    return {"last_hour": sum(1 for r in records if r.success and now - r.timestamp < timedelta(hours=1)),
            "today": sum(1 for r in records if r.success and r.timestamp.date() == now.date())}


def research_call_estimates(symbols: List[str], metrics: Dict[str, Any], cache) -> Dict[str, int]:
    """Max Claude calls one research run could make per stock: analyses still fresh in the AI cache are reused."""
    out = {}
    for s in symbols:
        m = metrics.get(s)
        if m is None or m.price is None:
            out[s] = len(RESEARCH_ANALYSIS_TYPES)
            continue
        out[s] = sum(1 for t in RESEARCH_ANALYSIS_TYPES
                     if cache.get_fresh(s, t, float(m.price), int(m.attention_score or 0)) is None)
    return out


def outcome_stats(symbols: List[str], db_getter: Callable, horizon: int = OUTCOME_HORIZON) -> Dict[str, Any]:
    """Stage 2.5 completed outcomes for each stock's saved research (SELECT only), aggregated with the existing
    services.analytics rules (including its small-sample warnings)."""
    from services.analytics import _aggregate
    db = db_getter()
    out = {}
    for s in symbols:
        try:
            rows = [o for snap in db.list_snapshots(symbol=s, limit=200) for o in db.get_outcomes_for_snapshot(snap.id)
                    if o.status == "COMPLETED" and o.horizon_trading_days == horizon]
        except Exception:  # noqa: BLE001 - historical context is optional
            continue
        if rows:
            out[s] = _aggregate(s, rows)
    return out
