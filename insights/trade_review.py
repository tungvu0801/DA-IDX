"""
insights/trade_review.py — TRADER REVIEW: "was this trade well-supported?" (Stage 2.7E). Educational only.

Reviews the decision PROCESS with the information available at decision time, kept strictly separate
from the trade OUTCOME (hindsight). Profit or loss is never an input to the process state.

Process state (deterministic, never a score or grade):
  INSUFFICIENT INFORMATION  fewer than 3 of the 6 review areas have verified data
  HIGHER-RISK PROCESS       an extended entry near resistance, HIGH event risk, or 2+ major risk factors
                            (extended entry, near-resistance entry, HIGH event risk, bearish research,
                             HIGH concentration created/worsened, downtrend)
  WELL-SUPPORTED PROCESS    no major risk factor, bullish research, entry not near resistance, and (when a
                            plan was supplied) a holding period and an invalidation condition
  MIXED PROCESS             otherwise

At-entry information comes ONLY from:
  - saved Stage 2.5 research snapshots from before the entry (research view, evidence, saved event risk)
  - technicals RECONSTRUCTED from verified historical daily prices up to the trading day BEFORE the entry
    (the same Stage 1 indicator code; nothing after the entry is used -> no hindsight leakage)
Anything else (plan, portfolio weight at entry, event context at entry without a snapshot) is UNAVAILABLE.
Outcomes (1/3/5-day return, MFE, MAE) reuse the unchanged Stage 2.5 outcome evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import config
from insights.labels import (BEARISH_VIEWS, BULLISH_VIEWS, UNAVAILABLE, is_extended, momentum_label, price_location,
                             trend_label, volume_label)

PROCESS_STATES = ("WELL-SUPPORTED PROCESS", "MIXED PROCESS", "HIGHER-RISK PROCESS", "INSUFFICIENT INFORMATION")
LESSONS = ["GOOD DECISION ≠ GUARANTEED PROFIT", "BAD OUTCOME ≠ AUTOMATICALLY BAD DECISION",
           "GOOD OUTCOME ≠ AUTOMATICALLY GOOD DECISION"]
LESSON_TEXT = "The goal is to improve repeatable decision-making, not to judge a trade by one result."
HINDSIGHT_TEXT = ("What happened after the entry does not by itself mean the original decision was good or poor. The "
                  "process review evaluates the information available at entry.")
CHECKLIST = [
    "Why am I entering?", "What is the current trend?", "Where is support?", "Where is resistance?",
    "Am I chasing an extended move?", "Is volume supporting the move?", "Is a major event approaching?",
    "How much of my portfolio will this become?", "Am I already concentrated in this sector?",
    "What would invalidate my original idea?", "How long do I intend to hold?",
]
HOLDING_PERIODS = {"days": "a few days", "weeks": "a few weeks", "months": "a few months", "long_term": "long term"}
SNAPSHOT_MAX_AGE_DAYS = 3        # a saved snapshot counts as "at entry" only if saved within this many days before
MIN_PATTERN_SAMPLE = 5
HISTORY_LOOKBACK_DAYS = 240


@dataclass
class EntryState:
    source: str                                  # CURRENT | RECONSTRUCTED | UNAVAILABLE
    as_of: Optional[str]
    entry_price: Optional[Decimal]
    trend: str = UNAVAILABLE
    momentum: str = UNAVAILABLE
    momentum_5d_pct: Optional[float] = None
    rsi: Optional[float] = None
    volume: str = UNAVAILABLE
    relative_volume: Optional[float] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    location: str = UNAVAILABLE
    extended: bool = False
    breakout: bool = False
    pullback: bool = False
    research_view: Optional[str] = None
    research_source: str = UNAVAILABLE           # SAVED_SNAPSHOT_AT_ENTRY | CURRENT_RESEARCH | UNAVAILABLE
    research_saved_at: Optional[str] = None
    bullish_pct: Optional[int] = None
    bearish_pct: Optional[int] = None
    event_risk: str = UNAVAILABLE
    event_source: str = UNAVAILABLE
    market: str = UNAVAILABLE
    market_source: str = UNAVAILABLE
    notes: List[str] = field(default_factory=list)


def technical_state(metrics, entry_price: Optional[Decimal], source: str, as_of: Optional[str]) -> EntryState:
    if metrics is None:
        return EntryState(UNAVAILABLE, as_of, entry_price, notes=["Price history before this entry is unavailable."])
    price = float(entry_price) if entry_price is not None else float(metrics.price)
    st = EntryState(source, as_of, entry_price if entry_price is not None else Decimal(str(round(metrics.price, 2))))
    st.trend, st.momentum = trend_label(metrics.trend), momentum_label(metrics.momentum_score)
    st.momentum_5d_pct = None if metrics.momentum_5d_pct is None else round(metrics.momentum_5d_pct, 2)
    st.rsi = None if metrics.rsi is None else round(metrics.rsi, 1)
    st.volume, st.relative_volume = volume_label(metrics.relative_volume), (
        None if metrics.relative_volume is None else round(metrics.relative_volume, 2))
    st.support = None if metrics.support is None else round(metrics.support, 2)
    st.resistance = None if metrics.resistance is None else round(metrics.resistance, 2)
    st.location = price_location(price, st.support, st.resistance)
    st.extended = is_extended(metrics.momentum_5d_pct, metrics.rsi)
    st.breakout = metrics.high_20d is not None and price > metrics.high_20d
    st.pullback = st.trend == "UPTREND" and st.momentum_5d_pct is not None and st.momentum_5d_pct < 0
    return st


def attach_research(st: EntryState, snapshot=None, current=None) -> EntryState:
    """Saved snapshot from before the entry (AFTER mode) or current research (BEFORE mode). Never invented."""
    if snapshot is not None:
        st.research_view, st.research_source = snapshot.research_view, "SAVED_SNAPSHOT_AT_ENTRY"
        st.research_saved_at = snapshot.created_at.isoformat()
        st.bullish_pct, st.bearish_pct = snapshot.bullish_pct, snapshot.bearish_pct
        if getattr(snapshot, "event_risk_level", None):
            st.event_risk, st.event_source = snapshot.event_risk_level, "SAVED_SNAPSHOT_AT_ENTRY"
    elif current is not None and getattr(current, "available", False):
        st.research_view, st.research_source = current.research_view, "CURRENT_RESEARCH"
        st.research_saved_at = current.as_of.isoformat() if current.as_of else None
        st.bullish_pct, st.bearish_pct = current.bullish_pct, current.bearish_pct
    else:
        st.notes.append("No research was saved before this decision, so the research view at that time is unavailable.")
    return st


def _f(code: str, area: str, text: str) -> dict:
    return {"code": code, "area": area, "text": text}


def review_process(st: EntryState, *, plan: Optional[dict] = None, portfolio_fit: Optional[dict] = None) -> dict:
    """Six review areas -> supporting factors, risk factors, watch list and a deterministic process state."""
    sup, risk, watch, areas = [], [], [], {}
    major = []

    # 1 MARKET CONTEXT
    if st.market == UNAVAILABLE:
        areas["market"] = {"label": UNAVAILABLE, "status": UNAVAILABLE}
    else:
        good = st.market in ("SUPPORTIVE", "IMPROVING")
        bad = st.market in ("CAUTIOUS", "WEAKENING")
        areas["market"] = {"label": st.market, "status": "SUPPORTIVE" if good else "CAUTION" if bad else "NEUTRAL",
                           "source": st.market_source}
        if good:
            sup.append(_f("market_supportive", "MARKET", f"The broader market context was {st.market.lower()}."))
        if bad:
            risk.append(_f("market_weak", "MARKET", f"The broader market context was {st.market.lower()}."))
            watch.append("Whether the broader market strengthens.")

    # 2 STOCK SETUP
    setup_known = st.trend != UNAVAILABLE or st.research_view is not None
    if not setup_known:
        areas["stock"] = {"label": UNAVAILABLE, "status": UNAVAILABLE}
    else:
        areas["stock"] = {"label": st.research_view or UNAVAILABLE, "trend": st.trend, "momentum": st.momentum,
                          "volume": st.volume, "research_source": st.research_source, "status": "NEUTRAL"}
        if st.research_view in BULLISH_VIEWS:
            sup.append(_f("research_bullish", "STOCK", f"Research leaned bullish ({st.research_view.title()})."))
            areas["stock"]["status"] = "SUPPORTIVE"
        if st.research_view in BEARISH_VIEWS:
            risk.append(_f("research_bearish", "STOCK", f"Research leaned bearish ({st.research_view.title()})."))
            major.append("research_bearish")
            areas["stock"]["status"] = "CAUTION"
        if st.trend == "UPTREND":
            sup.append(_f("uptrend", "STOCK", "The technical trend was up."))
        if st.trend == "DOWNTREND":
            risk.append(_f("downtrend", "STOCK", "The technical trend was down."))
            major.append("downtrend")
        if st.momentum == "STRONG" or (st.momentum_5d_pct is not None and st.momentum_5d_pct > 0 and not st.extended):
            sup.append(_f("momentum_positive", "STOCK", "Momentum was positive."))
        if st.momentum == "WEAK":
            risk.append(_f("momentum_weak", "STOCK", "Momentum was weak."))
            watch.append("Whether momentum improves.")
        if st.volume in ("WEAK", "NORMAL"):
            watch.append("Whether volume confirms the move.")

    # 3 ENTRY QUALITY
    if st.location == UNAVAILABLE and not st.extended:
        areas["entry"] = {"label": UNAVAILABLE, "status": UNAVAILABLE}
    else:
        label = {"NEAR_SUPPORT": "Near support", "NEAR_RESISTANCE": "Near resistance",
                 "MIDDLE_OF_RANGE": "Middle of range", "NO_RESISTANCE_ABOVE": "Above recent highs",
                 "NO_SUPPORT_BELOW": "Below recent lows"}.get(st.location, UNAVAILABLE)
        if st.breakout:
            label = "Breakout above the 20-day high"
        elif st.pullback and st.location != "NEAR_RESISTANCE":
            label = f"Pullback within an uptrend ({label.lower()})"
        areas["entry"] = {"label": label, "status": "NEUTRAL", "extended": st.extended, "support": st.support,
                          "resistance": st.resistance, "entry_price": st.entry_price}
        if st.location == "NEAR_SUPPORT" and not st.extended:
            sup.append(_f("near_support_entry", "ENTRY", f"The entry was near support ({st.support})."))
            areas["entry"]["status"] = "SUPPORTIVE"
        if st.location == "NEAR_RESISTANCE":
            risk.append(_f("near_resistance_entry", "ENTRY", f"The entry was close to resistance ({st.resistance})."))
            major.append("near_resistance_entry")
            areas["entry"]["status"] = "CAUTION"
            watch.append("Whether resistance breaks with supporting volume.")
        if st.extended:
            risk.append(_f("extended_entry", "ENTRY", "The entry came after a fast rise (extended price)."))
            major.append("extended_entry")
            areas["entry"]["status"] = "CAUTION"
        if st.support is not None:
            watch.append(f"Whether price holds support ({st.support}).")

    # 4 EVENT RISK
    if st.event_risk == UNAVAILABLE:
        areas["events"] = {"label": UNAVAILABLE, "status": UNAVAILABLE}
    else:
        areas["events"] = {"label": st.event_risk, "source": st.event_source,
                           "status": "CAUTION" if st.event_risk == "HIGH" else "SUPPORTIVE"
                           if st.event_risk in ("LOW", "NONE") else "NEUTRAL"}
        if st.event_risk == "HIGH":
            risk.append(_f("high_event_risk", "EVENTS", "A HIGH-risk event was very close to the entry."))
            major.append("high_event_risk")
        elif st.event_risk in ("LOW", "NONE"):
            sup.append(_f("no_high_event", "EVENTS", "No known HIGH event risk was identified."))

    # 5 PORTFOLIO FIT
    if not portfolio_fit:
        areas["portfolio"] = {"label": UNAVAILABLE, "status": UNAVAILABLE,
                              "note": "Portfolio weight at the time of entry was not saved."}
    else:
        worsens = portfolio_fit.get("creates_or_worsens_high")
        small = portfolio_fit.get("small_position")
        areas["portfolio"] = {"label": "Caution" if worsens else "OK", "status": "CAUTION" if worsens else
                              "SUPPORTIVE" if small else "NEUTRAL", **{k: portfolio_fit.get(k) for k in (
                                  "weight_after_pct", "sector", "sector_weight_after_pct")}}
        if worsens:
            risk.append(_f("concentration", "PORTFOLIO", portfolio_fit.get("text") or
                           "The position increased an already HIGH concentration."))
            major.append("concentration")
            watch.append("Whether your portfolio concentration decreases.")
        if small:
            sup.append(_f("small_position", "PORTFOLIO", "The position size stayed small relative to the portfolio."))

    # 6 PLAN QUALITY (never invented)
    if plan is None:
        areas["plan"] = {"label": UNAVAILABLE, "status": UNAVAILABLE, "note": "No trade plan was recorded."}
        plan_complete = None
    else:
        have = {k: bool(plan.get(k)) for k in ("reason", "holding_period", "invalidation", "amount")}
        plan_complete = have["holding_period"] and have["invalidation"]
        areas["plan"] = {"label": f"{sum(have.values())} of 4 plan items provided", "items": have,
                         "status": "SUPPORTIVE" if all(have.values()) else "CAUTION" if not have["invalidation"]
                         else "NEUTRAL"}
        if all(have.values()):
            sup.append(_f("plan_complete", "PLAN", "The plan states a reason, holding period, amount and what would "
                                                   "invalidate the idea."))
        if not have["invalidation"]:
            risk.append(_f("no_invalidation", "PLAN", "No invalidation condition was defined."))
        if not have["holding_period"]:
            risk.append(_f("no_holding_period", "PLAN", "No intended holding period was defined."))

    available = sum(1 for a in areas.values() if a["status"] != UNAVAILABLE)
    if available < 3:
        state = "INSUFFICIENT INFORMATION"
    elif (st.extended and st.location == "NEAR_RESISTANCE") or "high_event_risk" in major or len(major) >= 2:
        state = "HIGHER-RISK PROCESS"
    elif not major and st.research_view in BULLISH_VIEWS and st.location != "NEAR_RESISTANCE" \
            and plan_complete is not False:
        state = "WELL-SUPPORTED PROCESS"
    else:
        state = "MIXED PROCESS"
    return {"process_state": state, "areas": areas, "supporting": sup, "risks": risk,
            "watch": list(dict.fromkeys(watch)), "available_areas": available, "major_risk_factors": major,
            "lessons": LESSONS, "lesson_text": LESSON_TEXT,
            "note": "This describes the decision PROCESS, not future performance, and is not a buy/sell verdict."}


# ---- AFTER I TRADED: reconstruction + outcomes ------------------------------------------------------------

def _bars_before(bars, entry_date: date):
    return bars[bars["timestamp"].dt.date < entry_date].reset_index(drop=True)


def reconstruct(symbol: str, bars, entry_date: date, entry_price: Optional[Decimal]) -> EntryState:
    """Technicals as of the trading day BEFORE the entry, from verified daily bars (no lookahead)."""
    from analysis.indicators import compute_metrics
    if bars is None or bars.empty:
        return EntryState(UNAVAILABLE, None, entry_price, notes=["Price history before this entry is unavailable."])
    before = _bars_before(bars, entry_date)
    try:
        metrics = compute_metrics(symbol, before) if len(before) else None
    except Exception:  # noqa: BLE001 - too little history -> unavailable, never guessed
        metrics = None
    as_of = before["timestamp"].iloc[-1].date().isoformat() if len(before) else None
    return technical_state(metrics, entry_price, "RECONSTRUCTED", as_of)


def market_at(bars, entry_date: date) -> str:
    from analysis.indicators import compute_metrics
    if bars is None or bars.empty:
        return UNAVAILABLE
    before = _bars_before(bars, entry_date)
    try:
        m = compute_metrics(config.MARKET_PROXY_SYMBOL, before) if len(before) else None
    except Exception:  # noqa: BLE001
        m = None
    if m is None:
        return UNAVAILABLE
    t = trend_label(m.trend)
    if t == "UPTREND" and (m.momentum_5d_pct or 0) > 0:
        return "IMPROVING"
    if t == "DOWNTREND" and (m.momentum_5d_pct or 0) < 0:
        return "WEAKENING"
    return "MIXED"


def outcomes_after(bars, entry_date: date, entry_price: Decimal, st: EntryState) -> List[dict]:
    """Stage 2.5 outcome evaluation, unchanged, applied to the entry (day 0 = entry date)."""
    from services.outcome_tracker import _bars_after, _evaluate_horizon
    if bars is None or bars.empty:
        return []
    after = _bars_after(bars, entry_date.isoformat())
    probe = SimpleNamespace(id=None, price=float(entry_price), support=st.support, resistance=st.resistance,
                            setup_entry_low=None, setup_entry_high=None, setup_invalidation=None,
                            setup_target_1=None, setup_target_2=None)
    out = []
    for h in config.TRADING_DAY_HORIZONS:
        o = _evaluate_horizon(probe, after, h)
        if o is None:
            out.append({"horizon_trading_days": h, "status": "PENDING"})
        else:
            out.append({"horizon_trading_days": h, "status": "COMPLETED",
                        "return_pct": None if o.return_pct is None else round(o.return_pct, 2),
                        "mfe_pct": None if o.max_favorable_excursion_pct is None else round(o.max_favorable_excursion_pct, 2),
                        "mae_pct": None if o.max_adverse_excursion_pct is None else round(o.max_adverse_excursion_pct, 2)})
    return out


def snapshot_at_entry(snapshots: List[Any], entry_date: date) -> Optional[Any]:
    """Latest saved snapshot on or before the entry date and at most SNAPSHOT_MAX_AGE_DAYS older."""
    best = None
    for s in snapshots:
        d = s.created_at.date()
        if d <= entry_date and (entry_date - d).days <= SNAPSHOT_MAX_AGE_DAYS:
            if best is None or s.created_at > best.created_at:
                best = s
    return best


def review_lot(symbol: str, lot: dict, bars, spy_bars, snapshots: List[Any]) -> dict:
    opened = date.fromisoformat(lot["open_date"]) if lot.get("open_date") else None
    price = Decimal(str(lot["cost_per_share"])) if lot.get("cost_per_share") else None
    base = {"lot_id": lot.get("lot_id"), "open_date": lot.get("open_date"), "open_type": lot.get("open_type"),
            "quantity": lot.get("quantity"), "entry_price": price, "cost_basis": lot.get("cost_basis")}
    if opened is None or price is None:
        return {**base, "review": review_process(EntryState(UNAVAILABLE, None, price)), "outcomes": [],
                "entry_state": None, "note": "Entry date or price is not available for this lot."}
    st = reconstruct(symbol, bars, opened, price)
    st = attach_research(st, snapshot=snapshot_at_entry(snapshots, opened))
    st.market, st.market_source = market_at(spy_bars, opened), "RECONSTRUCTED (SPY daily prices before entry)"
    return {**base, "entry_state": st, "review": review_process(st), "outcomes": outcomes_after(bars, opened, price, st),
            "hindsight_note": HINDSIGHT_TEXT}


MAIN_PATTERN_TEXT = {   # (frequent, most common) — descriptive only, never good/bad
    "extended": ("You frequently entered after a fast upward move.",
                 "Your most common entry pattern was entering after a fast upward move."),
    "near_resistance": ("You frequently entered near resistance.", "Your most common entry pattern was near resistance."),
    "near_support": ("You frequently entered near support.", "Your most common entry pattern was near support."),
    "conflict": ("You frequently entered while the stock and market trends disagreed.",
                 "Your most common entry pattern was entering while stock and market trends disagreed."),
}


def trading_patterns(entries: List[dict]) -> dict:
    """Deterministic counts over reviewed entries. No psychology, no judgement."""
    usable = [e for e in entries if e.get("entry_state") is not None and e["entry_state"].source == "RECONSTRUCTED"]
    n = len(usable)
    if n < MIN_PATTERN_SAMPLE:
        return {"available": False, "sample": n, "min_sample": MIN_PATTERN_SAMPLE,
                "message": f"Not enough reviewed entries yet ({n} of the {MIN_PATTERN_SAMPLE} needed)."}

    def count(pred):
        return sum(1 for e in usable if pred(e))

    patterns = []
    for kind, label, pred, text in (
        ("near_resistance", "Near resistance", lambda e: e["entry_state"].location == "NEAR_RESISTANCE",
         "occurred near resistance"),
        ("near_support", "Near support", lambda e: e["entry_state"].location == "NEAR_SUPPORT", "occurred near support"),
        ("extended", "After a fast rise", lambda e: e["entry_state"].extended, "came after a fast rise (extended price)"),
        ("conflict", "Stock/market disagreement", lambda e: {e["entry_state"].trend, {"IMPROVING": "UPTREND",
                                                                                   "WEAKENING": "DOWNTREND"}.get(
            e["entry_state"].market, "")} == {"UPTREND", "DOWNTREND"}, "happened while stock and market trends conflicted"),
        ("high_event", "Saved HIGH event risk", lambda e: e["entry_state"].event_risk == "HIGH",
         "happened with a saved HIGH event risk"),
    ):
        k = count(pred)
        if k:
            patterns.append({"kind": kind, "label": label, "count": k, "of": n,
                             "text": f"{k} of the last {n} reviewed entries {text}."})
    completed = [e for e in usable if any(o.get("status") == "COMPLETED" and o["horizon_trading_days"] == 5
                                          for o in e.get("outcomes", []))]
    if completed:
        fav = sum(1 for e in completed for o in e["outcomes"] if o["horizon_trading_days"] == 5
                  and o["status"] == "COMPLETED" and (o["mfe_pct"] or 0) >= abs(o["mae_pct"] or 0))
        patterns.append({"kind": "mfe", "label": "Moved in your favor first (hindsight)", "count": fav,
                         "of": len(completed),
                         "text": f"{fav} of {len(completed)} entries with a completed 5-day window moved further in your "
                                 "favor than against you at some point (MFE vs MAE). This is hindsight, not a verdict "
                                 "on the decisions."})
    entry_kinds = [p for p in patterns if p["kind"] in MAIN_PATTERN_TEXT]
    main = max(entry_kinds, key=lambda p: p["count"], default=None)
    main_pattern = None if main is None else {
        "kind": main["kind"], "count": main["count"], "of": main["of"],
        "text": MAIN_PATTERN_TEXT[main["kind"]][0 if main["count"] / main["of"] >= 0.4 else 1]}
    by_kind = {p["kind"]: p["count"] for p in patterns}
    counters = [{"kind": k, "label": label, "count": by_kind.get(k, 0), "of": n}
                for k, label in (("extended", "After a fast rise"), ("near_resistance", "Near resistance"),
                                 ("near_support", "Near support"), ("conflict", "Stock/market disagreement"))]
    return {"available": True, "sample": n, "patterns": patterns, "main_pattern": main_pattern, "counters": counters,
            "main_pattern_note": "A pattern to be aware of — not a judgement that it is good or bad.",
            "not_assessed": ["Sector concentration at entry (not saved)", "Event risk at entry unless research was "
                             "saved at the time"],
            "note": "Counts of verified facts only; this does not judge you or your decisions."}


# ---- Stage 2.7F: simplified "After I traded" (presentation only; same review, same outcomes) -----------------
LEARN_TEXT = {
    "extended_entry": "Entering after a fast rise makes the entry harder to evaluate; a pullback or a pause can give a "
                      "clearer entry to judge.",
    "near_resistance_entry": "Near resistance, a move above it with strong volume is the usual confirmation to look for "
                             "before entering.",
    "high_event_risk": "Big scheduled events can move the price sharply in either direction; knowing the date before "
                       "entering helps you size the risk.",
    "downtrend": "Entering while the stock's trend is down means the trade starts against the current direction.",
    "market_weak": "When the overall market is weak, individual stocks have less support.",
    "research_bearish": "Saved research leaned bearish at the time; checking why before entering is worth it.",
    "concentration": "Large positions make one stock's moves matter much more to the whole account.",
    "no_invalidation": "Writing down what would prove the idea wrong makes the next review easier.",
}
LOCATION_WORDS = {"NEAR_SUPPORT": "near support", "NEAR_RESISTANCE": "near resistance",
                  "MIDDLE_OF_RANGE": "in the middle of its recent range",
                  "NO_RESISTANCE_ABOVE": "above its recent highs", "NO_SUPPORT_BELOW": "below its recent lows"}


def _days(n: int) -> str:
    return f"{n} trading day" + ("" if n == 1 else "s")


def simple_review(lot: dict) -> dict:
    """HOW THE TRADE STARTED / WHAT WAS DONE WELL / WHAT INCREASED RISK / WHAT TO LEARN — judged with what was known
    at entry — and WHAT HAPPENED, which is hindsight and kept separate."""
    st, r = lot.get("entry_state"), lot["review"]
    started = []
    if lot.get("open_date") and lot.get("entry_price") is not None:
        started.append(f"Bought on {lot['open_date']} at ${float(lot['entry_price']):,.2f}.")
    if st is not None:
        if st.location in LOCATION_WORDS:
            started.append(f"The price was {LOCATION_WORDS[st.location]}.")
        if st.extended:
            started.append("The stock had already moved up quickly (after a fast rise).")
        if st.trend in ("UPTREND", "DOWNTREND", "MIXED"):
            started.append(f"The stock's trend was {st.trend.lower().replace('uptrend', 'up').replace('downtrend', 'down')}.")
        if st.market in ("IMPROVING", "WEAKENING", "MIXED"):
            started.append(f"The broad market trend was {st.market.lower()}.")
        started.append(f"Saved research at the time: {st.research_view.title()}." if st.research_view
                       else "No research was saved at that time.")
    else:
        started.append("The conditions at entry could not be reconstructed.")
    happened = []
    for o in lot.get("outcomes", []):
        if o.get("status") == "COMPLETED" and o.get("return_pct") is not None:
            happened.append(f"{_days(o['horizon_trading_days'])} later: {o['return_pct']:+.2f}% "
                            f"(best point {o['mfe_pct']:+.2f}%, worst point {o['mae_pct']:+.2f}%).")
        elif o.get("status") == "PENDING":
            happened.append(f"{_days(o['horizon_trading_days'])} later: not reached yet.")
    codes = [f["code"] for f in r["risks"]]
    learn = [LEARN_TEXT[c] for c in codes if c in LEARN_TEXT][:2] or [LESSON_TEXT]
    return {"process_state": r["process_state"], "how_it_started": started,
            "what_was_done_well": [f["text"] for f in r["supporting"]][:3],
            "what_increased_risk": [f["text"] for f in r["risks"]][:3],
            "what_to_learn": learn,
            "what_happened": happened,
            "hindsight_label": "WHAT HAPPENED (hindsight — this was not known at the time of the decision)"}
