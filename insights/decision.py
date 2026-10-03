"""
insights/decision.py — PORTFOLIO + WATCHLIST DECISION REPORT engine (Stage 2.7G). Pure and deterministic.

It combines facts the app ALREADY computes — market labels (insights.market), Stage 1 technicals
(analysis.indicators via insights.labels), saved/fresh Stage 2 research, Stage 2.6 event risk, the
Stage 2.7C Robinhood position, the unchanged Stage 2.7D policy flags and verified Trader Review
patterns — into ONE current ACTION STATE per stock. There are no scores, weights or odds:
every state is chosen by the first matching rule of an ordered, documented list, and conflicting
evidence is listed explicitly instead of being averaged away.

States are decision SUPPORT ("what deserves attention / what am I waiting for"). None of them is an
order or a buy/sell/hold instruction, and cost basis alone never produces an "add" suggestion (there is
no add state at all).

OWNED STOCK — first matching rule wins
  1 INSUFFICIENT DATA            no technical data or no price
  2 DATA STALE                   quote unreliable/unavailable, quote older than the latest market data by more
                                 than PORTFOLIO_QUOTE_STALE_SECONDS, or market data stale
  3 EVENT RISK — REVIEW          Stage 2.6 event risk HIGH
  4 SETUP WEAKENING              >= 2 of: downtrend, momentum deteriorating, price below recent support,
                                 CURRENT research bearish
  5 PROTECT GAINS / REVIEW RISK  open P&L > 0 AND (near resistance OR extended OR momentum deteriorating)
  6 REVIEW POSITION SIZE         Stage 2.7D position-concentration flag is HIGH
  7 RESEARCH NEEDED              research missing or older than BEGINNER_RESEARCH_STALE_HOURS
  8 MONITOR SUPPORT              open P&L < 0 AND price near support
  9 SETUP IMPROVING              uptrend + strong momentum + positive 5-day change, not extended/near resistance,
                                 while the (current) research is still MIXED — price action ahead of research
 10 WAIT FOR CONFIRMATION        near resistance, extended, mixed trend, mixed research, cautious market, or
                                 deteriorating momentum
 11 SUPPORTED — MONITOR          current bullish research + uptrend + market not cautious
 12 MONITOR                      otherwise

WATCHLIST STOCK — first matching rule wins
  1 INSUFFICIENT DATA            no technical data
  2 DATA STALE                   price older than the latest market data by more than the stale limit, or market
                                 data stale
  3 WAIT FOR EVENT               Stage 2.6 event risk HIGH
  4 HIGHER-RISK SETUP            current bearish research, downtrend in a cautious market, extended AND near
                                 resistance, or >= 2 weakening signals
  5 RESEARCH NEEDED              research missing or stale
  6 WAIT FOR MARKET CONFIRMATION market CAUTIOUS
  7 WAIT FOR PULLBACK            extended (risen quickly)
  8 WAIT FOR BREAKOUT CONFIRMATION near resistance
  9 WORTH FURTHER REVIEW         current bullish research + uptrend + momentum not weak
 10 MIXED — KEEP WATCHING        otherwise
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

import config
from analysis import signals
from analysis.evidence_scoring import sector_score
from insights.events import earnings_note, upcoming_events
from insights.labels import (BEARISH_VIEWS, BULLISH_VIEWS, MIXED_VIEW, UNAVAILABLE, context_label, is_extended,
                             momentum_label, price_location, research_lean, trend_label, volume_label)
from insights.plain import freshness, market_freshness, research_freshness

CENT = Decimal("0.01")

# ---- state vocabulary ------------------------------------------------------------------------------------------
INSUFFICIENT = "INSUFFICIENT DATA"
DATA_STALE = "DATA STALE"
EVENT_REVIEW = "EVENT RISK — REVIEW"
WEAKENING = "SETUP WEAKENING"
PROTECT = "PROTECT GAINS / REVIEW RISK"
SIZE = "REVIEW POSITION SIZE"
RESEARCH = "RESEARCH NEEDED"
MON_SUPPORT = "MONITOR SUPPORT"
IMPROVING = "SETUP IMPROVING"
WAIT_CONF = "WAIT FOR CONFIRMATION"
SUPPORTED = "SUPPORTED — MONITOR"
MONITOR = "MONITOR"
OWNED_STATES = [INSUFFICIENT, DATA_STALE, EVENT_REVIEW, WEAKENING, PROTECT, SIZE, RESEARCH, MON_SUPPORT, IMPROVING,
                WAIT_CONF, SUPPORTED, MONITOR]

W_EVENT = "WAIT FOR EVENT"
W_HIGHER = "HIGHER-RISK SETUP"
W_MARKET = "WAIT FOR MARKET CONFIRMATION"
W_PULLBACK = "WAIT FOR PULLBACK"
W_BREAKOUT = "WAIT FOR BREAKOUT CONFIRMATION"
W_REVIEW = "WORTH FURTHER REVIEW"
W_MIXED = "MIXED — KEEP WATCHING"
WATCH_STATES = [INSUFFICIENT, DATA_STALE, W_EVENT, W_HIGHER, RESEARCH, W_MARKET, W_PULLBACK, W_BREAKOUT, W_REVIEW,
                W_MIXED]

OWNED_GROUPS = {"NEEDS ATTENTION": [EVENT_REVIEW, WEAKENING, PROTECT, SIZE, DATA_STALE, INSUFFICIENT],
                "STABLE / MONITOR": [SUPPORTED, IMPROVING, MON_SUPPORT, WAIT_CONF, MONITOR],
                "RESEARCH NEEDED": [RESEARCH]}
WATCH_GROUPS = {"SETUPS WORTH REVIEWING": [W_REVIEW],
                "WAITING FOR BETTER CONDITIONS": [W_PULLBACK, W_BREAKOUT, W_MARKET, W_EVENT, W_MIXED],
                "HIGHER-RISK / UNCLEAR": [W_HIGHER, RESEARCH, DATA_STALE, INSUFFICIENT]}

# Beginner words for labels (display only).
MARKET_WORD = {"SUPPORTIVE": "Supportive", "MIXED": "Mixed", "CAUTIOUS": "Cautious"}
TREND_WORD = {"UPTREND": "Up", "DOWNTREND": "Down", "MIXED": "Mixed"}
MOMENTUM_WORD = {"STRONG": "Positive", "NORMAL": "Normal", "WEAK": "Weak"}
VOLUME_WORD = {"STRONG": "Strong", "NORMAL": "Normal", "WEAK": "Light"}
LOCATION_WORD = {"NEAR_SUPPORT": "Near support", "NEAR_RESISTANCE": "Near resistance", "MIDDLE_OF_RANGE": "Mid-range",
                 "NO_RESISTANCE_ABOVE": "Above recent resistance", "NO_SUPPORT_BELOW": "Below recent support"}
SECTOR_WORD = {"SUPPORTIVE": "Strong this session", "MIXED": "Mixed", "WEAK": "Weak this session"}
EVENT_WORD = {"HIGH": "High", "MEDIUM": "Medium", "LOW": "Low", "NONE": "Low"}
FIT_WORD = {"HIGH": "Higher attention", "MEDIUM": "Watch", None: "Normal"}


def _money(v) -> str:
    return "N/A" if v is None else f"${Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP):,}"


def _dec(v) -> Optional[Decimal]:
    return None if v is None else Decimal(str(v))


def _ts(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        t = v
    elif hasattr(v, "to_pydatetime"):
        t = v.to_pydatetime()
    else:
        try:
            t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _f(code: str, text: str) -> dict:
    return {"code": code, "text": text}


# ================================================================================================================
# EVIDENCE — one dict of labels per stock, built only from existing facts
# ================================================================================================================

def build_evidence(*, symbol: str, owned: Optional[bool], metrics: Any, position: Any, research: Any,
                   events_bundle: Any, market: Optional[dict], sector: Optional[str], sector_pct: Optional[float],
                   concern: Optional[dict], sector_severity: Optional[str], market_ref_time: Any,
                   portfolio_available: bool, now: datetime) -> dict:
    """`concern` is the Stage 2.7D per-position concern (owned only); `sector_severity` is the 2.7D sector flag
    for this stock's sector when Robinhood is connected (None = no flag). `market_ref_time` is the latest market-data
    trade time (SPY), used so a quote is only "stale" relative to the latest data that exists."""
    m = metrics
    if position is not None and position.last_price is not None:
        price, price_src, price_time = Decimal(str(position.last_price)), "Robinhood quote", position.price_timestamp
    elif m is not None and m.price is not None:
        price, price_src, price_time = Decimal(str(m.price)), "market data (Alpaca)", getattr(m, "as_of", None)
    else:
        price, price_src, price_time = None, None, None
    support = Decimal(str(round(m.support, 2))) if m is not None and m.support is not None else None
    resistance = Decimal(str(round(m.resistance, 2))) if m is not None and m.resistance is not None else None
    location = price_location(float(price) if price is not None else None,
                              float(support) if support is not None else None,
                              float(resistance) if resistance is not None else None)
    mom5 = m.momentum_5d_pct if m is not None else None
    momentum = momentum_label(m.momentum_score) if m is not None else UNAVAILABLE
    ev_level = getattr(events_bundle, "event_risk_level", None) or (getattr(position, "event_risk_level", None)
                                                                     if position is not None else None)
    ev_level = ev_level if ev_level in ("HIGH", "MEDIUM", "LOW", "NONE") else UNAVAILABLE
    upcoming = upcoming_events(events_bundle, now) if events_bundle is not None else []
    nxt = next((e for e in upcoming if e["severity"] in ("HIGH", "MEDIUM")), None)

    # research (existing Stage 2 output; never recomputed)
    avail = bool(research is not None and research.available)
    r_fresh = research_freshness(research.age_hours) if avail else "MISSING"
    current = avail and r_fresh in ("FRESH", "AGING")
    cat = (research.category_scores or {}).get("catalyst") if avail else None
    r = {"available": avail, "view": research.research_view if avail else None,
         "lean": research_lean(research.research_view) if current else UNAVAILABLE,
         "bullish_pct": research.bullish_pct if avail else None, "neutral_pct": research.neutral_pct if avail else None,
         "bearish_pct": research.bearish_pct if avail else None, "age_hours": research.age_hours if avail else None,
         "freshness": r_fresh, "current": current, "source": research.source if research is not None else None,
         "catalyst": UNAVAILABLE if cat is None else "POSITIVE" if cat > 0 else "NEGATIVE" if cat < 0 else "NEUTRAL",
         "category_scores": {k: (None if v is None else round(v, 2)) for k, v in (research.category_scores or {}).items()}
         if avail else {}}

    # quote age RELATIVE to the latest market data (a weekend close is not "stale" when nothing newer exists)
    ref, qt = _ts(market_ref_time), _ts(price_time)
    rel_age = None if qt is None else max(0.0, ((ref or now) - qt).total_seconds())
    q_label = freshness(rel_age, config.FRESHNESS_QUOTE_FRESH_SECONDS, config.PORTFOLIO_QUOTE_STALE_SECONDS)
    quote_quality = getattr(position, "quote_quality", None) if position is not None else ("OK" if price else None)
    if not market or not market.get("available"):
        m_label = "UNKNOWN"
    else:
        m_label = "STALE" if market.get("stale") else market_freshness(market.get("fetched_at"), now)["label"]

    pos = None
    if position is not None:
        pos = {"value": position.market_value, "avg_cost": position.avg_cost, "price": position.last_price,
               "open_pnl": position.unrealized_pnl, "open_pnl_pct": position.unrealized_pnl_pct,
               "weight": position.portfolio_weight, "cost_basis": position.cost_basis_total,
               "quote_quality": position.quote_quality}
    concern = concern or {}
    pos_sev = concern.get("position_severity") if owned else None
    sec_sev = concern.get("sector_severity") if owned else sector_severity
    worst = "HIGH" if "HIGH" in (pos_sev, sec_sev) else "MEDIUM" if "MEDIUM" in (pos_sev, sec_sev) else None
    env = market.get("environment") if market and market.get("available") else UNAVAILABLE
    return {
        "symbol": symbol, "owned": owned,
        "price": price, "price_source": price_src, "price_time": price_time, "support": support,
        "resistance": resistance, "location": location,
        "extended": bool(m is not None and is_extended(m.momentum_5d_pct, m.rsi)),
        "trend": trend_label(m.trend) if m is not None else UNAVAILABLE, "momentum": momentum, "mom5": mom5,
        "momentum_deteriorating": momentum == "WEAK" or (mom5 is not None and mom5 < 0),
        "volume": volume_label(m.relative_volume) if m is not None else UNAVAILABLE,
        "pct_today": m.pct_change if m is not None else None,
        "technicals": None if m is None else {
            "rsi": None if m.rsi is None else round(m.rsi, 1), "atr": None if m.atr is None else round(m.atr, 2),
            "ema_9": None if m.ema_fast is None else round(m.ema_fast, 2),
            "ema_20": None if m.ema_medium is None else round(m.ema_medium, 2),
            "ema_50": None if m.ema_slow is None else round(m.ema_slow, 2),
            "momentum_score": m.momentum_score, "momentum_5d_pct": None if mom5 is None else round(mom5, 2),
            "relative_volume": None if m.relative_volume is None else round(m.relative_volume, 2),
            "dist_from_support_pct": None if m.dist_from_support_pct is None else round(m.dist_from_support_pct, 2),
            "dist_from_resistance_pct": None if m.dist_from_resistance_pct is None
            else round(m.dist_from_resistance_pct, 2), "as_of": getattr(m, "as_of", None)},
        "market_env": env, "market_trend": (market or {}).get("trend") if env != UNAVAILABLE else None,
        "sector": sector or "UNCLASSIFIED",
        "sector_label": context_label(sector_score(_SectorPct(sector_pct))) if sector_pct is not None else UNAVAILABLE,
        "sector_pct": sector_pct,
        "event_risk": ev_level, "next_event": nxt, "earnings_note": earnings_note(events_bundle)
        if events_bundle is not None else None, "events_available": ev_level != UNAVAILABLE,
        "research": r, "position": pos,
        "portfolio": {"available": portfolio_available, "position_severity": pos_sev, "sector_severity": sec_sev,
                      "sector_weight": concern.get("sector_weight"),
                      "fit": FIT_WORD[worst] if portfolio_available else "Unavailable"},
        "quote": {"quality": quote_quality, "age_vs_latest_market_s": None if rel_age is None else int(rel_age),
                  "freshness": q_label, "time": price_time},
        "freshness": {"market": m_label, "quote": q_label, "research": r_fresh,
                      "events": "FRESH" if ev_level != UNAVAILABLE else "UNKNOWN"},
    }


class _SectorPct:
    """sector_score() only reads sector_pct_change; this avoids inventing the other SectorContext fields."""
    def __init__(self, pct):
        self.sector_pct_change = pct


# ================================================================================================================
# REASONS, CONFLICTS, WATCH LISTS
# ================================================================================================================

def reasons(E: dict) -> dict:
    sup, cau = [], []
    r, sym = E["research"], E["symbol"]
    if E["trend"] == "UPTREND":
        sup.append(_f("uptrend", "The stock's trend is up."))
    if E["momentum"] == "STRONG" and not E["momentum_deteriorating"]:
        sup.append(_f("momentum_positive", "Momentum is positive."))
    if r["current"] and r["view"] in BULLISH_VIEWS:
        sup.append(_f("research_bullish", f"Research is {r['view'].title()} ({r['age_hours']} hours old)."))
    if r["current"] and r["catalyst"] == "POSITIVE":
        sup.append(_f("catalyst_positive", "Recent news in the research leans positive."))
    if E["market_env"] == "SUPPORTIVE":
        sup.append(_f("market_supportive", "The overall market is supportive."))
    if E["sector_label"] == "SUPPORTIVE":
        sup.append(_f("sector_supportive", f"{E['sector']} stocks are strong this session."))
    if E["location"] == "NEAR_SUPPORT" and not E["extended"]:
        sup.append(_f("near_support", f"Price is near support ({_money(E['support'])})."))
    if E["event_risk"] in ("LOW", "NONE") and E["next_event"] is None:
        sup.append(_f("no_event", "No major event is close."))

    if E["trend"] == "DOWNTREND":
        cau.append(_f("downtrend", "The stock's trend is down."))
    if E["momentum_deteriorating"]:
        cau.append(_f("momentum_deteriorating", "Momentum is weakening."))
    if E["location"] == "NEAR_RESISTANCE":
        cau.append(_f("near_resistance", f"Price is near resistance ({_money(E['resistance'])})."))
    if E["location"] == "NO_SUPPORT_BELOW":
        cau.append(_f("below_support", "Price is below its recent support levels."))
    if E["extended"]:
        cau.append(_f("extended", "The stock has already risen quickly (extended)."))
    if r["current"] and r["view"] in BEARISH_VIEWS:
        cau.append(_f("research_bearish", f"Research is {r['view'].title()}."))
    if r["current"] and r["view"] == MIXED_VIEW:
        cau.append(_f("research_mixed", "Research is mixed."))
    if not r["available"]:
        cau.append(_f("research_missing", f"No research saved for {sym} yet."))
    elif not r["current"]:
        cau.append(_f("research_stale", f"The latest research is {r['age_hours']} hours old — too old to rely on."))
    if E["market_env"] == "CAUTIOUS":
        cau.append(_f("market_cautious", "The overall market is cautious."))
    if E["sector_label"] == "WEAK":
        cau.append(_f("sector_weak", f"{E['sector']} stocks are weak this session."))
    if E["event_risk"] == "HIGH":
        cau.append(_f("event_high", "A major event is very close (HIGH event risk)."))
    elif E["next_event"] is not None:
        n = E["next_event"]
        cau.append(_f("event_upcoming", f"{n['title']} on {n['date']}."))
    if not E["events_available"]:
        cau.append(_f("events_unavailable", "Event data is unavailable right now, so event risk is unknown."))
    P = E["portfolio"]
    if P["position_severity"] == "HIGH":
        cau.append(_f("position_large", f"The position is already large ({E['position']['weight']}% of your account)."))
    if P["sector_severity"] == "HIGH":
        what = f"{E['sector']} exposure" if E["sector"] != "UNCLASSIFIED" else "Unclassified-sector exposure"
        cau.append(_f("sector_concentrated", f"{what} is already high"
                      + (f" ({P['sector_weight']}%)." if P["sector_weight"] is not None else ".")))
    if E["quote"]["quality"] == "DEGRADED":
        cau.append(_f("quote_degraded", "The price quote is less reliable than usual."))
    return {"supports": sup, "cautions": cau}


def conflicts(E: dict) -> List[dict]:
    """Explicit disagreements between evidence layers — shown as-is, never averaged into one number."""
    r, P, pos = E["research"], E["portfolio"], E["position"]
    stock_supportive = (r["current"] and r["view"] in BULLISH_VIEWS and E["trend"] != "DOWNTREND") or \
        (E["trend"] == "UPTREND" and E["momentum"] == "STRONG")
    technical_supportive = E["trend"] == "UPTREND" and E["momentum"] != "WEAK"
    out = []

    def add(code, a, b, text):
        out.append({"code": code, "a": a, "b": b, "text": text})
    if stock_supportive and P["position_severity"] == "HIGH":
        add("stock_vs_position", "STOCK SUPPORTIVE", "PORTFOLIO FIT POOR",
            "The stock itself looks supportive, but this position is already a large part of your account.")
    if stock_supportive and P["sector_severity"] == "HIGH":
        add("stock_vs_sector", "STOCK ATTRACTIVE", "SECTOR CONCENTRATION ALREADY HIGH",
            f"The stock looks attractive on its own, but your {E['sector']} exposure is already high.")
    if technical_supportive and E["event_risk"] == "HIGH":
        add("technical_vs_event", "TECHNICAL SETUP SUPPORTIVE", "EVENT RISK HIGH",
            "The chart looks supportive, but a major event is very close and can move the price sharply.")
    if E["market_env"] == "CAUTIOUS" and E["trend"] == "UPTREND" and E["momentum"] == "STRONG":
        add("market_weak_stock_strong", "MARKET WEAK", "STOCK RELATIVE STRENGTH STRONG",
            "The overall market is cautious, but this stock is holding an uptrend with positive momentum.")
    if E["market_env"] == "SUPPORTIVE" and E["trend"] == "DOWNTREND":
        add("market_strong_stock_weak", "MARKET SUPPORTIVE", "STOCK TREND DOWN",
            "The overall market is supportive, but this stock is in a downtrend.")
    if r["current"] and r["view"] in BULLISH_VIEWS and E["extended"]:
        add("research_vs_extended", "RESEARCH POSITIVE", "PRICE EXTENDED",
            "The research is positive, but the price has already risen quickly.")
    if pos is not None and pos["open_pnl"] is not None and Decimal(str(pos["open_pnl"])) > 0 and \
            E["momentum_deteriorating"]:
        add("profitable_vs_momentum", "POSITION PROFITABLE", "MOMENTUM DETERIORATING",
            "The position is currently profitable, but momentum is weakening.")
    return out


def _improves_weakens(E: dict) -> tuple:
    imp, weak = [], []
    s, res, r = E["support"], E["resistance"], E["research"]
    if s is not None:
        imp.append(f"Price holds above support ({_money(s)}).")
        weak.append(f"Price breaks below support ({_money(s)}).")
    if res is not None and E["location"] != "NO_RESISTANCE_ABOVE":
        imp.append(f"Price moves above resistance ({_money(res)}) with stronger volume.")
    if E["momentum"] != "STRONG" or E["momentum_deteriorating"]:
        imp.append("Momentum turns positive.")
    if not E["momentum_deteriorating"]:
        weak.append("Momentum weakens.")
    if E["market_env"] in ("SUPPORTIVE",):
        imp.append("The market stays supportive.")
        weak.append("The market turns cautious.")
    elif E["market_env"] in ("MIXED", "CAUTIOUS"):
        imp.append("The overall market improves.")
    if not r["available"] or not r["current"]:
        imp.append("Fresh research confirms the picture.")
    elif r["view"] not in BULLISH_VIEWS:
        imp.append("The research view improves.")
    if r["current"] and r["view"] not in BEARISH_VIEWS:
        weak.append("The research view turns bearish.")
    if E["event_risk"] != "HIGH":
        weak.append("Event risk rises (a major event gets close).")
    elif E["next_event"] is not None:
        imp.append(f"{E['next_event']['title']} passes.")
    if E["portfolio"]["available"] and (E["portfolio"]["sector_severity"] == "HIGH" or
                                        E["portfolio"]["position_severity"] == "HIGH"):
        weak.append("Your concentration in this area keeps growing.")
    return imp[:4], weak[:4]


def watch_next(E: dict) -> dict:
    n = E["next_event"]
    return {"support": E["support"], "resistance": E["resistance"],
            "volume": VOLUME_WORD.get(E["volume"], "Unavailable"),
            "next_event": None if n is None else {"title": n["title"], "date": n["date"], "days_until": n["days_until"]}}


# ================================================================================================================
# LAYERS (Market → Stock → Portfolio, reused Stage 2.7F framework)
# ================================================================================================================

def layers(E: dict) -> dict:
    r = E["research"]
    market = {"SUPPORTIVE": "Supportive", "MIXED": "Mixed", "CAUTIOUS": "Caution"}.get(E["market_env"], "Unavailable")
    if E["trend"] == UNAVAILABLE and not r["available"]:
        stock = "Unavailable"
    elif (r["current"] and r["view"] in BEARISH_VIEWS) or E["trend"] == "DOWNTREND":
        stock = "Caution"
    elif E["trend"] == "UPTREND" and (r["current"] and r["view"] in BULLISH_VIEWS):
        stock = "Supportive"
    else:
        stock = "Mixed"
    P = E["portfolio"]
    if not P["available"]:
        portfolio = "Unavailable"                  # never treated as "safe"
    elif "HIGH" in (P["position_severity"], P["sector_severity"]):
        portfolio = "Caution"
    elif "MEDIUM" in (P["position_severity"], P["sector_severity"]):
        portfolio = "Watch"
    else:
        portfolio = "OK"
    if stock == "Unavailable":
        summary = "NOT ENOUGH DATA TO COMPARE"
    elif stock == "Supportive" and portfolio == "Caution":
        summary = "SUPPORTED, BUT PORTFOLIO RISK IS HIGH"
    elif stock == "Supportive" and market == "Caution":
        summary = "STOCK SUPPORTIVE, MARKET CAUTIOUS"
    elif stock == "Supportive" and market == "Supportive" and portfolio in ("OK", "Unavailable"):
        summary = "MARKET AND STOCK SUPPORTIVE" + ("" if portfolio == "OK" else " (PORTFOLIO FIT UNKNOWN)")
    elif stock == "Supportive":
        summary = "STOCK SUPPORTIVE, OTHER LAYERS MIXED"
    elif stock == "Caution" and portfolio == "Caution":
        summary = "STOCK AND PORTFOLIO BOTH NEED CARE"
    elif stock == "Caution":
        summary = "STOCK SETUP NEEDS CARE"
    else:
        summary = "MIXED EVIDENCE"
    return {"market": market, "stock": stock, "portfolio": portfolio, "summary": summary}


# ================================================================================================================
# STATE ENGINES
# ================================================================================================================

def _weakening_signals(E: dict) -> List[str]:
    r = E["research"]
    sig = []
    if E["trend"] == "DOWNTREND":
        sig.append("downtrend")
    if E["momentum_deteriorating"]:
        sig.append("momentum deteriorating")
    if E["location"] == "NO_SUPPORT_BELOW":
        sig.append("below recent support")
    if r["current"] and r["view"] in BEARISH_VIEWS:
        sig.append("bearish research")
    return sig


def _stale_critical(E: dict) -> Optional[str]:
    if E["quote"]["quality"] in ("UNRELIABLE", "UNAVAILABLE"):
        return "The price quote is unreliable or unavailable."
    if E["quote"]["freshness"] == "STALE":
        mins = (E["quote"]["age_vs_latest_market_s"] or 0) // 60
        return f"The price is {mins} minutes older than the latest market data."
    if E["freshness"]["market"] == "STALE":
        return "Market data is stale."
    return None


def owned_state(E: dict) -> dict:
    pos, r = E["position"], E["research"]
    pnl = _dec(pos["open_pnl"]) if pos else None
    weak = _weakening_signals(E)
    stale = _stale_critical(E)
    if E["technicals"] is None or E["price"] is None:
        state, rule = INSUFFICIENT, "No technical data or no price is available."
    elif stale:
        state, rule = DATA_STALE, stale
    elif E["event_risk"] == "HIGH":
        state, rule = EVENT_REVIEW, "Stage 2.6 event risk is HIGH."
    elif len(weak) >= 2:
        state, rule = WEAKENING, "Two or more weakening signals: " + ", ".join(weak) + "."
    elif pnl is not None and pnl > 0 and (E["location"] == "NEAR_RESISTANCE" or E["extended"]
                                          or E["momentum_deteriorating"]):
        state, rule = PROTECT, "Profitable position with a price-action risk sign."
    elif E["portfolio"]["position_severity"] == "HIGH":
        state, rule = SIZE, "Stage 2.7D position-concentration flag is HIGH."
    elif not r["current"]:
        state, rule = RESEARCH, "Research is missing or older than the stale limit."
    elif pnl is not None and pnl < 0 and E["location"] == "NEAR_SUPPORT":
        state, rule = MON_SUPPORT, "Below cost, price near support, setup not weakening."
    elif E["trend"] == "UPTREND" and E["momentum"] == "STRONG" and (E["mom5"] or 0) > 0 and not E["extended"] \
            and E["location"] != "NEAR_RESISTANCE" and r["view"] == MIXED_VIEW:
        state, rule = IMPROVING, "Uptrend with positive momentum while research is still mixed."
    elif E["location"] == "NEAR_RESISTANCE" or E["extended"] or E["trend"] == "MIXED" or r["view"] == MIXED_VIEW \
            or E["market_env"] == "CAUTIOUS" or E["momentum_deteriorating"]:
        state, rule = WAIT_CONF, "Evidence is mixed (level, extension, trend, research, market or momentum)."
    elif r["view"] in BULLISH_VIEWS and E["trend"] == "UPTREND" and E["market_env"] != "CAUTIOUS":
        state, rule = SUPPORTED, "Current bullish research, uptrend, market not cautious."
    else:
        state, rule = MONITOR, "No rule above matched."
    return _decision(E, state, rule, owned=True)


def watchlist_state(E: dict) -> dict:
    r = E["research"]
    weak = _weakening_signals(E)
    stale = None if E["quote"]["quality"] not in ("UNRELIABLE", "UNAVAILABLE") else "The price is unavailable."
    stale = stale or ("The price is older than the latest market data." if E["quote"]["freshness"] == "STALE" else
                      "Market data is stale." if E["freshness"]["market"] == "STALE" else None)
    if E["technicals"] is None or E["price"] is None:
        state, rule = INSUFFICIENT, "No technical data or no price is available."
    elif stale:
        state, rule = DATA_STALE, stale
    elif E["event_risk"] == "HIGH":
        state, rule = W_EVENT, "Stage 2.6 event risk is HIGH."
    elif (r["current"] and r["view"] in BEARISH_VIEWS) or (E["trend"] == "DOWNTREND" and E["market_env"] == "CAUTIOUS") \
            or (E["extended"] and E["location"] == "NEAR_RESISTANCE") or len(weak) >= 2:
        state, rule = W_HIGHER, "Bearish research, downtrend in a cautious market, extended at resistance, or 2+ " \
                                "weakening signals."
    elif not r["current"]:
        state, rule = RESEARCH, "Research is missing or older than the stale limit."
    elif E["market_env"] == "CAUTIOUS":
        state, rule = W_MARKET, "The overall market is cautious."
    elif E["extended"]:
        state, rule = W_PULLBACK, "The stock has risen quickly (extended)."
    elif E["location"] == "NEAR_RESISTANCE":
        state, rule = W_BREAKOUT, "Price is near resistance."
    elif r["view"] in BULLISH_VIEWS and E["trend"] == "UPTREND" and E["momentum"] != "WEAK":
        state, rule = W_REVIEW, "Current bullish research, uptrend, momentum not weak."
    else:
        state, rule = W_MIXED, "No rule above matched."
    return _decision(E, state, rule, owned=False)


NEXT_OWNED = {
    INSUFFICIENT: "There is not enough verified data to assess this position right now.",
    DATA_STALE: "Refresh the data before relying on this assessment.",
    EVENT_REVIEW: "A major event is very close — review how much event risk you are comfortable holding through it.",
    WEAKENING: "The setup is weakening — review what would change your view on this position.",
    PROTECT: "The position is profitable, but risk signs are showing — review your plan for protecting the gain. "
             "This is not an instruction to sell.",
    SIZE: "Review whether this position size still fits your plan, rather than automatically adding more.",
    RESEARCH: "Run fresh research before relying on this assessment.",
    MON_SUPPORT: "Watch whether support holds. Being below your cost is not by itself a reason to add.",
    IMPROVING: "Watch whether the improvement holds; the research may be lagging the price action.",
    WAIT_CONF: "Wait for confirmation before adding: see what would improve the setup.",
    SUPPORTED: "Monitor the position; the current evidence is supportive.",
    MONITOR: "Keep monitoring; nothing stands out as needing a decision today.",
}
NEXT_WATCH = {
    INSUFFICIENT: "There is not enough verified data to assess this stock right now.",
    DATA_STALE: "Refresh the data before relying on this assessment.",
    W_EVENT: "Wait until the major event has passed before judging the setup.",
    W_HIGHER: "The setup carries higher risk right now — keep watching rather than acting.",
    RESEARCH: "Run fresh research before relying on this assessment.",
    W_MARKET: "Wait for the overall market to improve.",
    W_PULLBACK: "Wait for a pullback toward support, or a pause, before judging an entry.",
    W_BREAKOUT: "Wait for a move above resistance with confirming volume.",
    W_REVIEW: "Worth reviewing further with fresh research — this is not a recommendation.",
    W_MIXED: "Keep watching; the evidence is mixed.",
}


def _waiting_for(E: dict, state: str) -> List[str]:
    s, res = E["support"], E["resistance"]
    out = []
    if state == W_PULLBACK and s is not None:
        out.append(f"A pullback toward support ({_money(s)}).")
    if state in (W_PULLBACK, W_BREAKOUT) and res is not None and E["location"] != "NO_RESISTANCE_ABOVE":
        out.append(f"A move above resistance ({_money(res)}) with confirming volume.")
    if state == W_MARKET:
        out.append("The overall market trend improving.")
    if state == W_EVENT and E["next_event"] is not None:
        out.append(f"{E['next_event']['title']} ({E['next_event']['date']}) to pass.")
    if state == RESEARCH:
        out.append("Fresh research for this stock.")
    if state == DATA_STALE:
        out.append("Up-to-date price data.")
    if state in (W_MIXED, W_HIGHER, W_REVIEW):
        if E["momentum"] != "STRONG":
            out.append("Momentum turning positive.")
        if E["trend"] != "UPTREND":
            out.append("The trend turning up.")
        if s is not None and state != W_REVIEW:
            out.append(f"Price holding above support ({_money(s)}).")
    return out[:3]


# Cautions that explain a state are listed first, so the 3 shown to a beginner are the relevant ones.
STATE_CAUTIONS = {
    SIZE: ["position_large", "sector_concentrated"],
    PROTECT: ["near_resistance", "extended", "momentum_deteriorating", "position_large", "sector_concentrated"],
    WEAKENING: ["downtrend", "momentum_deteriorating", "below_support", "research_bearish"],
    EVENT_REVIEW: ["event_high"], W_EVENT: ["event_high"],
    RESEARCH: ["research_missing", "research_stale"],
    WAIT_CONF: ["near_resistance", "extended", "research_mixed", "market_cautious", "momentum_deteriorating"],
    W_PULLBACK: ["extended"], W_BREAKOUT: ["near_resistance"], W_MARKET: ["market_cautious"],
    W_HIGHER: ["research_bearish", "downtrend", "extended", "near_resistance", "momentum_deteriorating"],
}


def _decision(E: dict, state: str, rule: str, *, owned: bool) -> dict:
    why = reasons(E)
    first = STATE_CAUTIONS.get(state, [])
    why["cautions"].sort(key=lambda f: first.index(f["code"]) if f["code"] in first else len(first))
    if state == DATA_STALE:
        why["cautions"].insert(0, _f("data_stale", rule))
    imp, weak = _improves_weakens(E)
    groups = OWNED_GROUPS if owned else WATCH_GROUPS
    group = next(g for g, states in groups.items() if state in states)
    lay = layers(E)
    nxt = NEXT_OWNED[state] if owned else NEXT_WATCH[state]
    if owned and lay["portfolio"] == "Caution" and state in (SUPPORTED, IMPROVING, WAIT_CONF, MONITOR, MON_SUPPORT):
        nxt += " Portfolio risk here is already high, so monitor rather than automatically adding."
    return {"symbol": E["symbol"], "state": state, "rule": rule, "group": group, "next": nxt,
            "waiting_for": [] if owned else _waiting_for(E, state),
            "layers": lay, "conflicts": conflicts(E),
            "supports": [f["text"] for f in why["supports"]], "cautions": [f["text"] for f in why["cautions"]],
            "support_codes": [f["code"] for f in why["supports"]], "caution_codes": [f["code"] for f in why["cautions"]],
            "watch_next": watch_next(E), "improves_if": imp, "weakens_if": weak}


# ================================================================================================================
# ATTENTION PRIORITY — severity/category ordering (no score, never profitability)
# ================================================================================================================
# (tier, category order) — lower sorts first. Ties: alphabetical by symbol. Documented & tested.
ATTENTION_ORDER = {
    EVENT_REVIEW: (1, 1), "market_event_24h": (1, 2), WEAKENING: (1, 3), SIZE: (1, 4), DATA_STALE: (1, 5),
    PROTECT: (2, 1), "sector_concentration": (2, 2), "conflict": (2, 3), MON_SUPPORT: (2, 4),
    RESEARCH: (3, 1), "research_coverage": (3, 2), W_REVIEW: (3, 3), "market_event_week": (3, 4),
}


def attention_items(owned: List[dict], watch: List[dict], market_block: dict, sector_flags: List[dict],
                    coverage: Optional[dict], limit: int = 5) -> List[dict]:
    items = []

    def add(key, symbol, text):
        tier, order = ATTENTION_ORDER[key]
        items.append({"tier": tier, "order": order, "key": key, "symbol": symbol, "text": text,
                      "level": {1: "HIGH", 2: "MEDIUM", 3: "LOW"}[tier]})
    for d in owned:
        if d["state"] in (EVENT_REVIEW, WEAKENING, SIZE, DATA_STALE, PROTECT, MON_SUPPORT):
            why = (d.get("cautions") or [d["next"]])[0]          # the fact behind the state, not a repeat of it
            add(d["state"], d["symbol"], f"{d['symbol']} — {d['state'].lower()}: {why}")
        else:
            # the sector-concentration conflict repeats the portfolio-level sector item, so prefer any other conflict
            other = [c for c in d["conflicts"] if not (sector_flags and c.get("code") == "stock_vs_sector")]
            if other:
                add("conflict", d["symbol"], f"{d['symbol']}: {other[0]['a']} but {other[0]['b']}.")
    for f in sector_flags:
        add("sector_concentration", f["subject"], f"{f['subject']} exposure is {f['value']}% of your account (HIGH "
                                                  "concentration flag).")
    ev = (market_block or {}).get("next_event")
    if ev and ev.get("hours_until") is not None:
        if ev["hours_until"] <= config.EVENT_HIGH_RISK_HOURS:
            add("market_event_24h", "MARKET", f"{ev['title']} is within {config.EVENT_HIGH_RISK_HOURS:g} hours.")
        elif ev.get("days_until") is not None and ev["days_until"] <= config.EVENT_LOW_RISK_DAYS:
            add("market_event_week", "MARKET", f"{ev['title']} is on {ev['date']}.")
    if coverage and coverage.get("need"):
        add("research_coverage", "RESEARCH", f"{len(coverage['need'])} of {coverage['total']} holdings need fresh "
                                             "research.")
    for d in watch:
        if d["state"] == W_REVIEW:
            add(W_REVIEW, d["symbol"], f"{d['symbol']} (watchlist): worth further review.")
    items.sort(key=lambda i: (i["tier"], i["order"], i["symbol"]))
    return items[:limit]


# ================================================================================================================
# WHAT CHANGED SINCE LAST CHECK (in-memory comparison; nothing persisted)
# ================================================================================================================
COMPARE_FIELDS = {"state": "State", "research_view": "Research", "location": "Price location", "trend": "Trend",
                  "event_risk": "Event risk", "momentum": "Momentum"}
MARKET_FIELDS = {"conditions": "Market", "risk_level": "Risk level", "market_trend": "Market trend"}


def compare_snapshot(owned: List[dict], watch: List[dict], evidence: Dict[str, dict], market_block: dict) -> dict:
    snap = {}
    for d in owned + watch:
        E = evidence[d["symbol"]]
        snap[d["symbol"]] = {"state": d["state"], "research_view": E["research"]["view"] if E["research"]["current"]
                             else None, "location": "EXTENDED" if E["extended"] else E["location"], "trend": E["trend"],
                             "event_risk": E["event_risk"], "momentum": E["momentum"]}
    if market_block.get("available"):
        snap["_market"] = {"conditions": market_block["conditions"], "risk_level": market_block["verdicts"]["risk_level"],
                           "market_trend": market_block["verdicts"]["market_trend"]}
    return snap


def _word(field: str, v):
    if v is None:
        return "None"
    if field == "location":
        return LOCATION_WORD.get(v, "Extended" if v == "EXTENDED" else str(v).title())
    if field in ("research_view", "trend", "momentum", "event_risk", "conditions"):
        return str(v).title()
    return str(v)


def changes_since(previous: Optional[dict], current: dict) -> List[dict]:
    if not previous:
        return []
    out = []
    for subject, cur in current.items():
        prev = previous.get(subject)
        if not isinstance(prev, dict):
            continue
        fields = MARKET_FIELDS if subject == "_market" else COMPARE_FIELDS
        for k, label in fields.items():
            if k in prev and prev.get(k) != cur.get(k):
                out.append({"subject": "MARKET" if subject == "_market" else subject, "field": label,
                            "before": _word(k, prev.get(k)), "after": _word(k, cur.get(k))})
    return out


# ================================================================================================================
# RESEARCH BATCH PLAN — respects the AI cache, hourly and daily limits (nothing runs here)
# ================================================================================================================

def research_plan(need: List[str], estimates: Dict[str, int], calls_last_hour: int, calls_today: int) -> dict:
    hourly_left = max(0, config.AI_MAX_CALLS_PER_HOUR - calls_last_hour)
    daily_left = max(0, config.AI_MAX_CALLS_PER_DAY - calls_today)
    allowed = min(hourly_left, daily_left)
    now_list, remaining, used = [], [], 0
    for sym in need:
        est = estimates.get(sym, config.AI_CALLS_PER_RESEARCH_RUN)
        if used + est <= allowed:
            now_list.append(sym)
            used += est
        else:
            remaining.append(sym)
    reason = None
    if remaining:
        reason = ("hourly" if hourly_left <= daily_left else "daily") + " AI call limit"
    return {"need": need, "estimates": estimates, "estimated_max_calls": sum(estimates.get(s, 0) for s in need),
            "analyze_now": now_list, "calls_for_now": used, "remaining": remaining, "limited_by": reason,
            "calls_last_hour": calls_last_hour, "hourly_limit": config.AI_MAX_CALLS_PER_HOUR, "hourly_left": hourly_left,
            "calls_today": calls_today, "daily_limit": config.AI_MAX_CALLS_PER_DAY, "daily_left": daily_left,
            "note": "Research runs only after you confirm. Cached AI results are reused and the hourly/daily limits "
                    "are enforced again on every call."}


# ================================================================================================================
# CONTEXT THAT NEVER CHANGES EVIDENCE: trading patterns and historical outcomes
# ================================================================================================================

def pattern_context(E: dict, patterns: Optional[dict]) -> Optional[dict]:
    """CHASING RISK note from VERIFIED Trader Review patterns, shown only for an extended stock. Behavioural history
    is context only — it is not predictive and never changes research evidence or the state."""
    if not E["extended"] or not patterns or not patterns.get("available"):
        return None
    ext = next((p for p in patterns.get("patterns", []) if p.get("kind") == "extended"), None)
    if not ext or not ext.get("count"):
        return None
    return {"title": "CHASING RISK",
            "text": (f"You have historically entered {ext['count']} of {ext['of']} reviewed positions after a fast rise. "
                     "This does not mean those trades were wrong, but it is a pattern worth checking before another "
                     "extended entry."),
            "note": "Behavioural history is context only; it does not predict anything and does not change the "
                    "evidence."}


def outcome_context(stats: Optional[Any], horizon: int) -> Optional[dict]:
    """Stage 2.5 outcomes for this stock's past saved research — HISTORICAL context, separate from current evidence.
    Rates are hidden below ANALYTICS_MIN_SAMPLE_SIZE_LOW so tiny samples never look like conclusions."""
    if stats is None or not stats.n:
        return None
    enough = stats.n >= config.ANALYTICS_MIN_SAMPLE_SIZE_LOW
    return {"horizon_trading_days": horizon, "n": stats.n, "enough_sample": enough,
            "positive_return_frequency_pct": round(stats.positive_return_frequency * 100, 1)
            if enough and stats.positive_return_frequency is not None else None,
            "median_return_pct": stats.median_return_pct if enough else None,
            "avg_mfe_pct": stats.avg_mfe_pct if enough else None, "avg_mae_pct": stats.avg_mae_pct if enough else None,
            "sample_warning": stats.sample_warning,
            "note": "Past outcomes of earlier saved research for this stock. Historical context only — it does not "
                    "change today's evidence."}
