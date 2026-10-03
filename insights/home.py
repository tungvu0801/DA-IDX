"""
insights/home.py — Beginner COMMAND CENTER aggregation (Stage 2.7F). Pure presentation over existing facts.

No new scoring. Every label/number comes from: the market overview (insights/market.py), the Robinhood
portfolio view + policy (Stage 2.7C/D), saved/fresh Stage 2 research, Stage 1 market metrics, and the
Trader Review pattern counts. Anything unavailable is shown as unavailable.

Session change ("today"): Robinhood's read-only tools expose no "Today" figure. The session change is
therefore CALCULATED on the same basis as SPY/QQQ — the latest market-data session (Alpaca), using your
Robinhood share counts for shares held BEFORE that session:
    change_$  = shares_held_before_session x (session price - previous close)
    change_%  = sum(change_$) / sum(shares_held_before_session x previous close)
    contribution_pp = change_$ / that same base x 100
Shares bought during the session are excluded (their entry price for the session is not a previous close).
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

import config
from analysis import signals
from insights.labels import BULLISH_VIEWS, BEARISH_VIEWS, is_extended, price_location, trend_label
from insights.plain import market_freshness, portfolio_freshness, quote_freshness, research_freshness

CENT = Decimal("0.01")
CAUSATION_NOTE = "Price data tells us WHAT happened. News can help explain WHY, but it does not prove causation."
SEVERITY_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 1}

CONNECTION_HELP = {
    "GATEWAY_DOWN": ("Your Stock Agent is running, but the local read-only Robinhood connection is not currently running.",
                     ["Open PowerShell.", 'Go to the rh_gateway folder:  cd "C:\\Data Analyst Py\\DA IDX\\rh_gateway"',
                      "Start the read-only gateway:  .venv\\Scripts\\python -m rh_gateway serve  (keep that window open)",
                      "Come back here and press Refresh."]),
    "GATEWAY_SECRET_UNAVAILABLE": ("The read-only Robinhood connection has not been started on this computer yet.",
                                   ["Open PowerShell.", 'Go to the rh_gateway folder:  cd "C:\\Data Analyst Py\\DA IDX\\rh_gateway"',
                                    "Start the read-only gateway:  .venv\\Scripts\\python -m rh_gateway serve",
                                    "Come back here and press Refresh."]),
    "REAUTH_REQUIRED": ("Robinhood needs you to sign in again before your holdings can be shown.",
                        ["Open PowerShell in the rh_gateway folder.",
                         "Run:  .venv\\Scripts\\python -m rh_gateway login  and approve in your browser.",
                         "Then start the gateway:  .venv\\Scripts\\python -m rh_gateway serve",
                         "Come back here and press Refresh."]),
    "DISABLED": ("The Robinhood connection is turned off in the Stock Agent settings.",
                 ["Open stock-agent\\.env and set  PORTFOLIO_AWARENESS_ENABLED=true", "Restart the Stock Agent.",
                  "Start the read-only gateway (see the rh_gateway README)."]),
    "GATEWAY_REJECTED": ("The Stock Agent could not verify the local Robinhood connection.",
                         ["Stop the gateway window (Ctrl+C) and start it again:  .venv\\Scripts\\python -m rh_gateway serve",
                          "Come back here and press Refresh."]),
}
DEFAULT_HELP = ("Robinhood did not respond just now.", ["Wait a minute and press Refresh."])


def _money(v) -> Optional[Decimal]:
    return None if v is None else Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)


def _pp(v) -> Optional[Decimal]:
    return None if v is None else Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)


def connection_status(reason: Optional[str], message: Optional[str]) -> dict:
    if reason is None:
        return {"connected": True, "status": "CONNECTED"}
    text, steps = CONNECTION_HELP.get(reason, DEFAULT_HELP)
    return {"connected": False, "status": "NOT CONNECTED", "title": "ROBINHOOD NOT CONNECTED", "message": text,
            "how_to_fix": steps, "technical": {"reason": reason, "detail": message},
            "note": "Market information keeps working while Robinhood is offline."}


def session_label(as_of: Optional[datetime], now: datetime) -> dict:
    if as_of is None:
        return {"label": "Latest session", "date": None, "is_today": False}
    d = as_of.date() if hasattr(as_of, "date") else as_of
    is_today = d == now.date()
    return {"label": "Today" if is_today else f"Last session ({d.strftime('%a %b')} {d.day})", "date": d.isoformat(),
            "is_today": is_today}


def session_change(view, metrics: Dict[str, Any], spy_metrics) -> dict:
    spy_date = spy_metrics.as_of.date() if spy_metrics is not None and getattr(spy_metrics, "as_of", None) is not None \
        else None
    rows, excluded, base, total = [], [], Decimal("0"), Decimal("0")
    for p in view.positions:
        m = metrics.get(p.symbol)
        if m is None or p.quantity is None or m.prev_close in (None, 0):
            excluded.append({"symbol": p.symbol, "reason": "no market data for this session"})
            continue
        if spy_date is not None and getattr(m, "as_of", None) is not None and m.as_of.date() != spy_date:
            excluded.append({"symbol": p.symbol, "reason": "different trading session than SPY"})
            continue
        held_before = p.quantity - (p.intraday_quantity or Decimal("0"))
        if held_before <= 0:
            excluded.append({"symbol": p.symbol, "reason": "bought during this session"})
            continue
        prev_close, price = Decimal(str(m.prev_close)), Decimal(str(m.price))
        change = held_before * (price - prev_close)
        prev_value = held_before * prev_close
        base += prev_value
        total += change
        rows.append({"symbol": p.symbol, "session_pct": _pp(m.pct_change), "change": change, "prev_value": prev_value,
                     "partial": p.intraday_quantity is not None and p.intraday_quantity > 0})
    for r in rows:
        r["contribution_pp"] = _pp(r["change"] / base * 100) if base else None
        r["change"] = _money(r["change"])
        r["prev_value"] = _money(r["prev_value"])
    pct = _pp(total / base * 100) if base else None
    return {"available": bool(rows), "change": _money(total) if rows else None, "pct": pct, "base": _money(base),
            "rows": rows, "excluded": excluded,
            "basis": ("Calculated with the same market data and session as SPY/QQQ, for shares you held before this "
                      "session. Robinhood's own 'Today' figure is not available through the read-only connection.")}


def contributors(change: dict, limit: int = 3) -> dict:
    rows = [r for r in change["rows"] if r["change"] is not None]
    pos = sorted([r for r in rows if r["change"] > 0], key=lambda r: r["change"], reverse=True)[:limit]
    neg = sorted([r for r in rows if r["change"] < 0], key=lambda r: r["change"])[:limit]
    return {"positive": pos, "negative": neg,
            "note": "Contribution = your shares held before the session × that stock's price change (Python-calculated)."}


def vs_market(change: dict, indices: List[dict], view, sectors: List[dict]) -> dict:
    idx = {i["symbol"]: i for i in indices if i.get("available")}
    spy = idx.get(config.MARKET_PROXY_SYMBOL)
    qqq = idx.get(config.MARKET_TECH_PROXY_SYMBOL)
    out = {"available": change["available"] and spy is not None, "portfolio_pct": change["pct"],
           "spy_pct": spy.get("pct_change") if spy else None, "qqq_pct": qqq.get("pct_change") if qqq else None}
    if out["available"]:
        out["diff_vs_spy_pp"] = _pp(Decimal(str(change["pct"])) - Decimal(str(spy["pct_change"])))
        out["diff_vs_qqq_pp"] = _pp(Decimal(str(change["pct"])) - Decimal(str(qqq["pct_change"]))) if qqq else None
    reasons = []
    classified = [x for x in view.sector_exposure if x["sector"] != "UNCLASSIFIED" and x["weight_pct"] is not None]
    if classified:
        top = max(classified, key=lambda x: x["weight_pct"])
        etf = next((s for s in sectors if s["sector"] == top["sector"]), None)
        move = f" This session {etf['etf']} moved {float(etf['pct_change']):+.2f}%." if etf else ""
        reasons.append(f"{top['weight_pct']}% of your portfolio is in {top['sector']} stocks, while SPY is a broad index "
                       f"of many sectors, so {top['sector']} moves can have a larger effect on your account.{move}")
    largest = max((p for p in view.positions if p.portfolio_weight is not None), key=lambda p: p.portfolio_weight,
                  default=None)
    if largest is not None:
        reasons.append(f"{largest.symbol} alone is {largest.portfolio_weight}% of your portfolio, so its moves weigh "
                       "heavily on the total.")
    out["why_it_may_differ"] = reasons
    out["note"] = "Differences are Python-calculated from the figures shown; they describe exposure, not a cause."
    return out


def _attention(p, concern: dict, research: dict, m) -> dict:
    reasons_high, reasons_watch = [], []
    if concern.get("position_severity") == "HIGH":
        reasons_high.append("large part of your account")
    if concern.get("sector_severity") == "HIGH":
        reasons_high.append(f"{concern.get('sector')} exposure is high")
    if p.event_risk_level == "HIGH":
        reasons_high.append("major event very close")
    if p.quote_quality in ("UNRELIABLE", "UNAVAILABLE"):
        reasons_high.append("price quote unreliable")
    if concern.get("position_severity") == "MEDIUM" or concern.get("sector_severity") == "MEDIUM":
        reasons_watch.append("fairly large position")
    if p.quote_quality in ("STALE", "DEGRADED"):
        reasons_watch.append("price quote is getting old")
    rf = research_freshness(research.get("age_hours")) if research.get("available") else "MISSING"
    if rf in ("STALE", "MISSING"):
        reasons_watch.append("research needed" if rf == "MISSING" else "research is stale")
    if p.event_risk_level == "MEDIUM":
        reasons_watch.append("event coming up")
    if m is not None and is_extended(m.momentum_5d_pct, m.rsi):
        reasons_watch.append("moved up quickly")
    level = "Higher attention" if reasons_high else "Watch" if reasons_watch else "Normal"
    return {"level": level, "reasons": reasons_high + reasons_watch}


def stock_cards(view, metrics: Dict[str, Any], research_map: dict, concerns: dict, session: dict) -> List[dict]:
    cards = []
    for p in sorted(view.positions, key=lambda x: x.portfolio_weight or Decimal("-1"), reverse=True):
        m = metrics.get(p.symbol)
        r = research_map.get(p.symbol, {"available": False})
        rf = research_freshness(r.get("age_hours")) if r.get("available") else "MISSING"
        trend = {"UPTREND": "Up", "DOWNTREND": "Down", "MIXED": "Mixed"}.get(trend_label(m.trend) if m else "", "N/A")
        pct = _pp(m.pct_change) if m is not None else None
        move = "flat" if pct is None or pct == 0 else ("up" if pct > 0 else "down")
        when = "today" if session.get("is_today") else "in the last session"
        move_txt = f"{p.symbol}'s move {when} is unavailable" if pct is None else (
            f"{p.symbol} was flat {when}" if pct == 0 else f"{p.symbol} is {move} {abs(pct)}% {when}")
        if r.get("available") and rf in ("FRESH", "AGING"):
            sentence = (f"{move_txt}; its latest saved research is {r['research_view'].title()} "
                        f"({r['age_hours']} hours old).")
        elif r.get("available"):
            sentence = f"{p.symbol}'s saved research is {r['age_hours']} hours old — analyze {p.symbol} for a fresh view."
        else:
            sentence = f"No fresh research saved — analyze {p.symbol}."
        loc = price_location(m.price, m.support, m.resistance) if m is not None else "UNAVAILABLE"
        cards.append({
            "symbol": p.symbol, "session_pct": pct, "position_value": p.market_value, "result": p.unrealized_pnl,
            "result_pct": p.unrealized_pnl_pct, "weight": p.portfolio_weight, "sector": p.sector,
            "research": {"available": r.get("available", False), "view": r.get("research_view"),
                         "bullish_pct": r.get("bullish_pct"), "neutral_pct": r.get("neutral_pct"),
                         "bearish_pct": r.get("bearish_pct"), "age_hours": r.get("age_hours"), "freshness": rf,
                         "source": r.get("source")},
            "trend": trend, "price_location": loc,
            "extended": bool(m is not None and is_extended(m.momentum_5d_pct, m.rsi)),
            "attention": _attention(p, concerns.get(p.symbol, {}), r, m),
            "quote": {"quality": p.quote_quality, "freshness": quote_freshness(p.quote_age_seconds),
                      "updated": p.price_timestamp},
            "sentence": sentence,
        })
    return cards


def big_picture(market: dict) -> str:
    trend = {"IMPROVING": "The broad market trend is improving", "WEAKENING": "The broad market trend is weakening"}.get(
        market.get("trend"), "The broad market trend is mixed")
    tech = next((i for i in market.get("indices", []) if i.get("symbol") == config.MARKET_TECH_PROXY_SYMBOL
                 and i.get("available")), None)
    parts = [trend]
    if tech and tech["trend"] == "UPTREND" and (tech.get("pct_change") or 0) > 0:
        parts.append(" and technology is participating")
    elif tech and tech["trend"] == "DOWNTREND":
        parts.append(" while technology is lagging")
    b = market.get("breadth_label")
    if b == "MIXED":
        parts.append(", but breadth is mixed. That means the indexes can look stronger than many individual stocks "
                     "underneath them.")
    elif b == "WEAK":
        parts.append(", but most stocks are not participating.")
    elif b == "STRONG":
        parts.append(", and most stocks are participating.")
    else:
        parts.append(".")
    text = "".join(parts)
    if market.get("volatility") == "ELEVATED":
        text += " Price swings are larger than normal."
    return text


def market_block(market: dict, now: datetime) -> dict:
    if not market or not market.get("available"):
        return {"available": False, "message": (market or {}).get("message") or "Current market trend unavailable",
                "freshness": {"label": "UNKNOWN"}}
    idx = {i["symbol"]: i for i in market["indices"]}
    risk = {"LOW": "Lower", "NORMAL": "Normal", "ELEVATED": "Elevated"}.get(market["volatility"], "Unknown")
    events = market.get("events", [])
    if any(e["hours_until"] <= config.EVENT_HIGH_RISK_HOURS for e in events):
        risk = "Elevated"
    b = market["breadth"]

    def tile(sym, title):
        i = idx.get(sym) or {}
        return {"title": title, "symbol": sym, "available": bool(i.get("available")), "pct": i.get("pct_change"),
                "pct_5d": i.get("momentum_5d_pct"), "trend": i.get("trend")}
    news = []
    for n in market.get("news", [])[:3]:
        affected = {"SPY": "Broad market (SPY)", "QQQ": "Technology / growth (QQQ)"}.get(n.get("queried_symbol"),
                                                                                         n.get("queried_symbol"))
        news.append({"headline": n.get("headline"), "source": n.get("source"), "time": n.get("created_at"),
                     "url": n.get("url"), "affected": affected,
                     "why_it_may_matter": f"Returned by the news source for {affected}. Headlines like this can affect "
                                          "overall market sentiment; this app does not claim it caused a price move."})
    return {
        "available": True,
        "conditions": market["environment"],
        "verdicts": {"market_trend": market["trend"].title(), "trading_environment": {
            "MORE STABLE": "More Stable", "MIXED / SELECTIVE": "Selective", "MORE CHALLENGING": "More Challenging"}.get(
            market["difficulty"], market["difficulty"]), "risk_level": risk},
        "tiles": [tile(config.MARKET_PROXY_SYMBOL, "S&P 500 (SPY)"), tile(config.MARKET_TECH_PROXY_SYMBOL, "Tech (QQQ)"),
                  tile("SOXX", "Semis (SOXX)")],
        "breadth": {"label": market["breadth_label"], "advancing_pct": b.get("advancing_pct"),
                    "above_ema20_pct": b.get("above_ema20_pct"), "scope": b.get("scope")},
        "volatility": market["volatility"],
        "next_event": events[0] if events else None,
        "events": events[:3],
        "big_picture": big_picture(market),
        "data_says": {"positive": market["drivers"]["positive"], "caution": market["drivers"]["caution"]},
        "news": news,
        "causation_note": CAUSATION_NOTE,
        "what_to_watch": market.get("what_to_watch", []),
        "freshness": market_freshness(market.get("fetched_at"), now),
        "stale_message": market.get("stale_message"),
        "data_as_of": market.get("data_as_of"),
    }


def robinhood_summary(view, reconciliation: dict, policy: dict, change: dict, session: dict, now: datetime) -> dict:
    positions = [p for p in view.positions if p.portfolio_weight is not None]
    largest = max(positions, key=lambda p: p.portfolio_weight, default=None)
    classified = [x for x in view.sector_exposure if x["sector"] != "UNCLASSIFIED" and x["weight_pct"] is not None]
    top_sector = max(classified, key=lambda x: x["weight_pct"], default=None)
    flags = policy.get("flags", []) if policy.get("enabled") else []
    worst = max((SEVERITY_ORDER.get(f["severity"], 0) for f in flags if f["severity"] in ("HIGH", "MEDIUM")), default=0)
    return {
        "account_value": reconciliation["robinhood_portfolio_value"], "account_value_source": "Robinhood reported",
        "session_change": change["change"], "session_pct": change["pct"], "session": session,
        "session_basis": change["basis"],
        "open_pnl": reconciliation["unrealized_pnl"], "open_pnl_pct": reconciliation["unrealized_pnl_pct"],
        "cash": reconciliation["cash"],
        "largest_position": {"symbol": largest.symbol, "weight": largest.portfolio_weight} if largest else None,
        "largest_sector": {"sector": top_sector["sector"], "weight": top_sector["weight_pct"]} if top_sector else None,
        "attention": {3: "HIGH", 2: "MEDIUM"}.get(worst, "NORMAL"),
        "freshness": portfolio_freshness(view.freshness["positions"].get("fetched_at"), now),
        "account": reconciliation["account"],
    }


def daily_feedback(view, market: dict, cards: List[dict], change: dict, sector_info_band: float) -> List[str]:
    out = []
    classified = [x for x in view.sector_exposure if x["sector"] != "UNCLASSIFIED" and x["weight_pct"] is not None]
    top = max(classified, key=lambda x: x["weight_pct"], default=None)
    if top and float(top["weight_pct"]) >= sector_info_band:
        etf = next((s for s in (market.get("sectors") or []) if s["sector"] == top["sector"]), None)
        tail = f" ({etf['etf']} {float(etf['pct_change']):+.2f}% this session)" if etf else ""
        out.append(f"{top['sector']} exposure is high ({top['weight_pct']}%), so semiconductor-sector movement is "
                   f"particularly important for you{tail}." if top["sector"] == "Semiconductors" else
                   f"{top['sector']} exposure is high ({top['weight_pct']}%), so that sector's movement is particularly "
                   f"important for you{tail}.")
    if cards:
        big = cards[0]
        out.append(f"{big['symbol']} is your largest position ({big['weight']}%), so it has the largest potential effect "
                   "on your account.")
    nxt = next((e for e in (market.get("events") or []) if e["days_until"] <= config.EVENT_LOW_RISK_DAYS), None)
    if nxt:
        out.append(f"{nxt['title']} is on {nxt['date']} ({nxt['days_until']} days away).")
    stale = [c["symbol"] for c in cards if c["quote"]["freshness"] == "STALE" or c["research"]["freshness"] in
             ("STALE", "MISSING")]
    if stale:
        out.append(f"{', '.join(stale[:4])}{' and others' if len(stale) > 4 else ''}: quote or research is missing or "
                   "old — refresh before relying on it.")
    hot = [c["symbol"] for c in cards if c["price_location"] == "NEAR_RESISTANCE" or c["extended"]]
    if hot:
        out.append(f"{', '.join(hot[:3])} {'is' if len(hot) == 1 else 'are'} near resistance or moved up quickly, so "
                   "confirmation may matter more than chasing the move.")
    neg = contributors(change)["negative"] if change.get("available") else []
    if neg and len(out) < 5:
        out.append(f"{neg[0]['symbol']} was the biggest drag on your account this session "
                   f"(−${abs(neg[0]['change']):,.2f}).")
    return out[:5]


def pattern_card(patterns: Optional[dict]) -> Optional[dict]:
    if not patterns or not patterns.get("available"):
        return None
    ext = next((p for p in patterns["patterns"] if p.get("kind") == "extended"), None)
    if not ext:
        return None
    return {"count": ext["count"], "of": ext["of"],
            "text": f"{ext['count']} of {ext['of']} reviewed buys occurred after a fast rise.",
            "explanation": ("This does not mean those trades were automatically wrong. It means you frequently entered "
                            "after substantial upward momentum.") if ext["count"] / ext["of"] >= 0.4 else
            "This does not mean those trades were wrong; it is one pattern in your verified entries.",
            "source": "Verified Robinhood buy orders + price history before each entry (Trader Review)."}


def research_coverage(cards: List[dict], usage: dict) -> dict:
    need = [c["symbol"] for c in cards if c["research"]["freshness"] != "FRESH"]
    return {"needs_research": need, "ai_calls_per_stock": config.AI_CALLS_PER_RESEARCH_RUN,
            "max_ai_calls": len(need) * config.AI_CALLS_PER_RESEARCH_RUN,
            "calls_today": usage.get("calls"), "daily_limit": usage.get("daily_call_limit"),
            "calls_remaining_today": usage.get("calls_remaining_today"), "hourly_limit": config.AI_MAX_CALLS_PER_HOUR,
            "note": "Research runs only when you click Analyze. Nothing is spent on page load."}


def fit_together(market: dict, card: dict) -> dict:
    """MARKET → STOCK → PORTFOLIO for one holding, from labels already computed. Deterministic wording only."""
    env = market.get("conditions") if market.get("available") else None
    m_level = {"SUPPORTIVE": "good", "MIXED": "mid", "CAUTIOUS": "bad"}.get(env, "unknown")
    r = card["research"]
    view = r.get("view") if r.get("available") and r.get("freshness") in ("FRESH", "AGING") else None
    if card["trend"] == "Up" and (view is None or view in BULLISH_VIEWS):
        s_level = "good"
    elif card["trend"] == "Down" or (view in BEARISH_VIEWS):
        s_level = "bad"
    elif card["trend"] == "N/A" and view is None:
        s_level = "unknown"
    else:
        s_level = "mid"
    att = card["attention"]["level"]
    p_level = {"Normal": "good", "Watch": "mid", "Higher attention": "bad"}[att]
    sym = card["symbol"]
    cards = [
        {"layer": "Market", "value": (env or "Unavailable").title(), "level": m_level,
         "text": "Overall market conditions." if env else "Market data unavailable."},
        {"layer": sym, "value": f"Trend {card['trend']}" + (f" · {view.title()}" if view else " · Research needed"),
         "level": s_level, "text": "The stock's own trend and its latest research."},
        {"layer": "Your portfolio", "value": att, "level": p_level,
         "text": f"{sym} is {card['weight']}% of your account" + (f"; {', '.join(card['attention']['reasons'])}."
                                                                  if card["attention"]["reasons"] else ".")},
    ]
    names = {"Market": "the market", sym: sym, "Your portfolio": "your portfolio"}
    good = [names[c["layer"]] for c in cards if c["level"] == "good"]
    bad = [names[c["layer"]] for c in cards if c["level"] in ("bad", "mid")]
    if any(c["level"] == "unknown" for c in cards):
        text = "Some information is missing, so the three layers can't be compared fully yet."
    elif not bad:
        text = f"The market, {sym} and your portfolio currently line up. Lined-up conditions still don't make a result certain."
    elif not good:
        text = f"The market, {sym} and your portfolio all call for caution right now."
    else:
        lead = _join(good)
        text = (f"{lead[0].upper() + lead[1:]} {'looks' if len(good) == 1 else 'look'} supportive, while {_join(bad)} "
                f"{'calls' if len(bad) == 1 else 'call'} for more care. When layers disagree, more confirmation is "
                "usually worth waiting for.")
    return {"symbol": sym, "cards": cards, "how_these_fit": text}


def _join(items: List[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
