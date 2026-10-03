"""
insights/market.py — "Current market — what's going on?" (Stage 2.7E). Market CONTEXT, not a prediction.

Every fact comes from data the project already verifies:
  indexes      SPY (config.MARKET_PROXY_SYMBOL), QQQ (config.MARKET_TECH_PROXY_SYMBOL, technology/growth proxy),
               SOXX (the Semiconductors ETF already in data/sector_map.py) — Alpaca daily bars via the Stage 1
               indicator pipeline (scanner.market_scanner.analyze_symbols)
  breadth      the app's fixed 80-stock large-cap list (config.FALLBACK_UNIVERSE) — explicitly NOT the whole
               U.S. market; scanner counts come from today's screener movers and are labelled as such
  sectors      the sector ETFs in data/sector_map.py, scored with the EXISTING Stage 2 sector_score()
  events       Stage 2.6 verified macro events (FOMC, CPI, PPI, jobs report, PCE)
  news         Alpaca news (data/news.py) — shown as sourced headlines, separate from market data

Labels are deterministic (cutoffs documented in config.MARKET_* and insights/labels.py). Nothing here
forecasts prices, assigns probabilities, or explains a move with an unsourced cause.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import config
from analysis import signals
from analysis.evidence_scoring import market_score, sector_score
from analysis.sector_context import SectorContext
from data.sector_map import SECTOR_MAP
from insights.labels import UNAVAILABLE, context_label, momentum_label, trend_label, volume_label

MIN_BREADTH_SAMPLE = 20
BREADTH_SCOPE = ("the app's fixed list of 80 large U.S. stocks (not the entire U.S. market)")


@dataclass
class MarketInputs:
    indices: Dict[str, object]                 # symbol -> TickerMetrics | None
    basket: List[object]                       # TickerMetrics for the breadth list
    basket_requested: int
    scanner_counts: Optional[dict]
    sector_etf_changes: Dict[str, Optional[float]]  # sector name -> ETF daily % change
    macro_events: List[dict]
    news: List[dict]
    fetched_at: datetime
    errors: List[str] = field(default_factory=list)


def _r(v, n=2):
    return None if v is None else round(float(v), n)


def index_summary(symbol: str, role: str, m) -> dict:
    if m is None:
        return {"symbol": symbol, "role": role, "available": False}
    return {
        "symbol": symbol, "role": role, "available": True, "price": _r(m.price), "pct_change": _r(m.pct_change),
        "trend": trend_label(m.trend), "momentum_5d_pct": _r(m.momentum_5d_pct),
        "momentum_10d_pct": _r(m.momentum_10d_pct), "momentum": momentum_label(m.momentum_score),
        "dist_from_20d_high_pct": _r(m.dist_from_high_pct), "dist_from_20d_low_pct": _r(m.dist_from_low_pct),
        "relative_volume": _r(m.relative_volume), "volume": volume_label(m.relative_volume),
        "volatility_pct": _r(m.volatility_pct, 1), "volatility_expansion": _r(m.volatility_expansion),
        "rsi": _r(m.rsi, 1), "as_of": m.as_of.isoformat() if getattr(m, "as_of", None) is not None else None,
    }


def breadth_summary(basket: List[object], requested: int, scanner_counts: Optional[dict]) -> dict:
    n = len(basket)
    out = {"scope": BREADTH_SCOPE, "sample_size": n, "requested": requested, "scanner": scanner_counts}
    if n < MIN_BREADTH_SAMPLE:
        return {**out, "available": False, "label": UNAVAILABLE}
    adv = sum(1 for m in basket if (m.pct_change or 0) > 0)
    dec = sum(1 for m in basket if (m.pct_change or 0) < 0)
    e20 = [m for m in basket if m.ema_medium is not None]
    e50 = [m for m in basket if m.ema_slow is not None]
    above20 = round(100 * sum(1 for m in e20 if m.price > m.ema_medium) / len(e20), 1) if e20 else None
    above50 = round(100 * sum(1 for m in e50 if m.price > m.ema_slow) / len(e50), 1) if e50 else None
    large = [m for m in basket if m.pct_change is not None and abs(m.pct_change) >= signals.LARGE_MOVE_PCT]
    adv_pct = round(100 * adv / n, 1)
    if adv_pct >= config.MARKET_BREADTH_STRONG_PCT and (above20 or 0) >= config.MARKET_BREADTH_STRONG_PCT:
        label = "STRONG"
    elif adv_pct <= config.MARKET_BREADTH_WEAK_PCT and above20 is not None and above20 <= config.MARKET_BREADTH_WEAK_PCT:
        label = "WEAK"
    else:
        label = "MIXED"
    return {**out, "available": True, "label": label, "advancing_pct": adv_pct,
            "declining_pct": round(100 * dec / n, 1), "above_ema20_pct": above20, "above_ema50_pct": above50,
            "strong_momentum_count": sum(1 for m in basket if (m.pct_change or 0) >= signals.STRONG_MOMENTUM_PCT),
            "large_gain_count": sum(1 for m in large if m.pct_change > 0),
            "large_drop_count": sum(1 for m in large if m.pct_change < 0),
            "large_mover_share_pct": round(100 * len(large) / n, 1)}


def _trend_state(spy: dict) -> str:
    if not spy.get("available"):
        return UNAVAILABLE
    mom5 = spy.get("momentum_5d_pct")
    if spy["trend"] == "UPTREND" and mom5 is not None and mom5 > 0:
        return "IMPROVING"
    if spy["trend"] == "DOWNTREND" and mom5 is not None and mom5 < 0:
        return "WEAKENING"
    return "MIXED"


def _volatility_state(spy: dict) -> str:
    vol, exp = spy.get("volatility_pct"), spy.get("volatility_expansion")
    if vol is None:
        return UNAVAILABLE
    if vol >= config.MARKET_VOL_ELEVATED_PCT or (exp is not None and exp >= config.VOLATILITY_EXPANSION_RATIO_THRESHOLD):
        return "ELEVATED"
    if vol <= config.MARKET_VOL_LOW_PCT and (exp is None or exp < 1.0):
        return "LOW"
    return "NORMAL"


def _risk_appetite(spy: dict, tech: dict, breadth: dict) -> str:
    if not (spy.get("available") and tech.get("available")) or not breadth.get("available"):
        return UNAVAILABLE
    s, t, adv = spy["pct_change"], tech["pct_change"], breadth["advancing_pct"]
    if s > 0 and t >= s and adv >= 50:
        return "RISK-ON"
    if s < 0 and t <= s and adv < 50:
        return "RISK-OFF"
    return "MIXED"


def _fmt_pct(v) -> str:
    return "N/A" if v is None else f"{v:+.2f}%"


def build_market_insights(inp: MarketInputs, now: datetime, portfolio_exposure: Optional[dict] = None) -> dict:
    spy_sym, tech_sym = config.MARKET_PROXY_SYMBOL, config.MARKET_TECH_PROXY_SYMBOL
    semi_etf = SECTOR_MAP["Semiconductors"]["etf"]
    spy = index_summary(spy_sym, "Broad market (S&P 500 ETF)", inp.indices.get(spy_sym))
    tech = index_summary(tech_sym, "Technology / growth proxy (Nasdaq-100 ETF)", inp.indices.get(tech_sym))
    semi = index_summary(semi_etf, "Semiconductors (sector ETF from the app's sector map)", inp.indices.get(semi_etf))
    breadth = breadth_summary(inp.basket, inp.basket_requested, inp.scanner_counts)

    if not spy.get("available"):
        return {"available": False, "status": "MARKET_DATA_UNAVAILABLE",
                "message": "Current market trend unavailable (broad-market data could not be fetched).",
                "fetched_at": inp.fetched_at.isoformat(timespec="seconds"), "errors": inp.errors,
                "events": inp.macro_events, "news": inp.news}

    trend = _trend_state(spy)
    volatility = _volatility_state(spy)
    appetite = _risk_appetite(spy, tech, breadth)
    today_score = market_score(SectorContext("Market", spy_sym, None, None, None, spy_sym, spy["pct_change"]))
    today_label = context_label(today_score)
    b = breadth["label"]
    if trend == "IMPROVING" and b != "WEAK" and volatility != "ELEVATED":
        environment = "SUPPORTIVE"
    elif trend == "WEAKENING" or (volatility == "ELEVATED" and b == "WEAK"):
        environment = "CAUTIOUS"
    else:
        environment = "MIXED"

    conflicts = []
    if tech.get("available") and {spy["trend"], tech["trend"]} == {"UPTREND", "DOWNTREND"}:
        conflicts.append(f"{spy_sym} is in a {spy['trend'].lower()} while {tech_sym} is in a {tech['trend'].lower()}.")
    if trend == "IMPROVING" and b == "WEAK":
        conflicts.append("The broad index is improving but most stocks in the breadth list are not.")
    if trend == "WEAKENING" and b == "STRONG":
        conflicts.append("The broad index is weakening but most stocks in the breadth list are rising.")

    events = inp.macro_events
    within_24h = [e for e in events if e["hours_until"] <= config.EVENT_HIGH_RISK_HOURS]
    within_7d = [e for e in events if e["days_until"] <= config.EVENT_LOW_RISK_DAYS]

    challenges = []
    if volatility == "ELEVATED":
        challenges.append("Volatility is elevated.")
    if b == "WEAK":
        challenges.append("Breadth is weak.")
    if conflicts:
        challenges.append("Market signals disagree.")
    if within_24h:
        challenges.append(f"A major event ({within_24h[0]['title']}) is within 24 hours.")
    if breadth.get("available") and breadth["large_mover_share_pct"] >= config.MARKET_LARGE_MOVER_SHARE_PCT:
        challenges.append(f"{breadth['large_mover_share_pct']}% of the breadth list moved {signals.LARGE_MOVE_PCT:g}% "
                          "or more today.")
    if not challenges and trend != "WEAKENING":
        difficulty = "MORE STABLE"
    elif len(challenges) >= 2:
        difficulty = "MORE CHALLENGING"
    else:
        difficulty = "MIXED / SELECTIVE"

    drivers_pos, drivers_neg = [], []
    if spy["pct_change"] > 0 and (spy.get("momentum_5d_pct") or 0) > 0:
        drivers_pos.append(f"Positive momentum: {spy_sym} is {_fmt_pct(spy['pct_change'])} today and "
                           f"{_fmt_pct(spy['momentum_5d_pct'])} over 5 days.")
    if (spy.get("momentum_5d_pct") or 0) < 0:
        drivers_neg.append(f"Weakening momentum: {spy_sym} is {_fmt_pct(spy['momentum_5d_pct'])} over 5 days.")
    if b == "STRONG":
        drivers_pos.append(f"Broad participation: {breadth['advancing_pct']}% of the breadth list rose today.")
    if b == "WEAK":
        drivers_neg.append(f"Weak breadth: only {breadth['advancing_pct']}% of the breadth list rose today.")
    for summary in (tech, semi):
        if summary.get("available") and summary["trend"] == "UPTREND" and (summary.get("momentum_5d_pct") or 0) > 0:
            drivers_pos.append(f"Strong {summary['role'].split(' (')[0].lower()} trend: {summary['symbol']} is in an "
                               f"uptrend ({_fmt_pct(summary['momentum_5d_pct'])} over 5 days).")
        if summary.get("available") and summary["trend"] == "DOWNTREND":
            drivers_neg.append(f"{summary['role'].split(' (')[0]} weakness: {summary['symbol']} is in a downtrend "
                               f"({_fmt_pct(summary['pct_change'])} today).")
    if volatility == "ELEVATED":
        drivers_neg.append(f"Elevated volatility: {spy_sym} 20-day volatility is {spy['volatility_pct']}% "
                           f"(annualized).")
    if within_7d:
        drivers_neg.append(f"Major macro event approaching: {within_7d[0]['title']} on {within_7d[0]['date']}.")
    for c in conflicts:
        drivers_neg.append(f"Conflicting signals: {c}")

    sectors = []
    for name, pct in inp.sector_etf_changes.items():
        if pct is None:
            continue
        score = sector_score(SectorContext(name, SECTOR_MAP[name]["etf"], pct, None, None, spy_sym, None))
        sectors.append({"sector": name, "etf": SECTOR_MAP[name]["etf"], "pct_change": _r(pct),
                        "label": context_label(score)})
    sectors.sort(key=lambda x: x["pct_change"], reverse=True)
    strong_areas = [s for s in sectors if s["label"] == "SUPPORTIVE"][:3]
    weak_areas = [s for s in sectors if s["label"] == "WEAK"][-3:]

    happening = [f"{spy_sym} (broad market) is {_fmt_pct(spy['pct_change'])} today and its trend is "
                 f"{spy['trend'].lower()} ({_fmt_pct(spy['momentum_5d_pct'])} over 5 days)."]
    if tech.get("available"):
        happening.append(f"{tech_sym} (technology/growth) is {_fmt_pct(tech['pct_change'])} today, trend "
                         f"{tech['trend'].lower()}.")
    if breadth.get("available"):
        happening.append(f"Across {BREADTH_SCOPE}, {breadth['advancing_pct']}% rose and "
                         f"{breadth['declining_pct']}% fell today.")

    watch = []
    if within_7d:
        watch.append(f"The {within_7d[0]['title']} on {within_7d[0]['date']}.")
    if b in ("WEAK", "MIXED") and breadth.get("available"):
        watch.append(f"Whether more stocks start rising (currently {breadth['advancing_pct']}% of the breadth list "
                     "advancing).")
    if volatility == "ELEVATED":
        watch.append(f"Whether volatility cools ({spy_sym} 20-day volatility {spy['volatility_pct']}%).")
    if trend == "WEAKENING":
        watch.append(f"Whether {spy_sym}'s 5-day momentum turns positive (currently {_fmt_pct(spy['momentum_5d_pct'])}).")
    elif trend == "IMPROVING":
        watch.append(f"Whether {spy_sym} holds its recent gains (5-day {_fmt_pct(spy['momentum_5d_pct'])}).")

    for_portfolio = []
    if portfolio_exposure:
        for s in portfolio_exposure.get("sectors", []):
            etf_row = next((x for x in sectors if x["sector"] == s["sector"]), None)
            move = f"; today its ETF {etf_row['etf']} is {_fmt_pct(etf_row['pct_change'])}" if etf_row else ""
            for_portfolio.append(
                f"{s['sector']} makes up {s['weight_pct']}% of your portfolio ({s['share_of_classified_pct']}% of the "
                f"holdings with a verified sector), so its movement matters more for you than for a broadly "
                f"diversified portfolio{move}.")
            if etf_row and len(watch) < 4:
                watch.append(f"{s['sector']} trend ({etf_row['etf']}), since it is {s['weight_pct']}% of your "
                             "portfolio.")
        if portfolio_exposure.get("unclassified_pct"):
            for_portfolio.append(f"{portfolio_exposure['unclassified_pct']}% of your portfolio has no verified sector, "
                                 "so its sector exposure cannot be assessed.")

    return {
        "available": True,
        "status": "OK",
        "environment": environment,
        "trend": trend,
        "volatility": volatility,
        "risk_appetite": appetite,
        "breadth_label": b,
        "market_today": today_label,
        "difficulty": difficulty,
        "difficulty_factors": challenges,
        "conflicting_signals": conflicts,
        "indices": [spy, tech, semi],
        "breadth": breadth,
        "drivers": {"positive": drivers_pos, "caution": drivers_neg,
                    "note": "OBSERVED market data only. Price movement alone does not prove why a move happened."},
        "strong_areas": strong_areas,
        "weak_areas": weak_areas,
        "sectors": sectors,
        "events": events,
        "news": inp.news,
        "what_is_happening": happening,
        "why_it_matters": ENVIRONMENT_TEXT[environment],
        "difficulty_text": DIFFICULTY_TEXT[difficulty],
        "what_to_watch": watch[:4],
        "for_your_portfolio": for_portfolio,
        "fetched_at": inp.fetched_at.isoformat(timespec="seconds"),
        "data_as_of": spy.get("as_of"),
        "errors": inp.errors,
        "disclaimer": "Market context, not a prediction. Nothing here says what the market will do next.",
    }


ENVIRONMENT_TEXT = {
    "SUPPORTIVE": "The broad market's trend is improving, breadth is not weak and volatility is not elevated.",
    "MIXED": "Market signals are mixed, so conditions differ a lot from stock to stock.",
    "CAUTIOUS": "The broad market's trend is weakening, or volatility is elevated while breadth is weak.",
}
DIFFICULTY_TEXT = {
    "MORE STABLE": "Price moves are relatively calm and signals mostly agree.",
    "MIXED / SELECTIVE": "Some signals are unsettled, so individual stocks can behave very differently.",
    "MORE CHALLENGING": "Price moves are larger than normal and/or market signals disagree. For a beginner, this means "
                        "entry timing and risk control deserve extra attention.",
}


def with_freshness(insights: dict, now: datetime) -> dict:
    fetched = insights.get("fetched_at")
    age_min = None
    if fetched:
        fetched_dt = datetime.fromisoformat(fetched)
        if fetched_dt.tzinfo is None:
            fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
        age_min = round((now - fetched_dt).total_seconds() / 60.0, 1)
    stale = age_min is None or age_min > config.MARKET_FEEDBACK_STALE_MINUTES
    return {**insights, "age_minutes": age_min, "stale": stale,
            "stale_message": "MARKET FEEDBACK STALE — REFRESH REQUIRED" if stale else None,
            "stale_after_minutes": config.MARKET_FEEDBACK_STALE_MINUTES}


def _sector_attention_level() -> float:
    """Reuse the configured sector-concentration INFO band (Stage 2.7D) to decide which sectors to mention."""
    from portfolio.policy import parse_bands
    bands, _ = parse_bands(config.PORTFOLIO_POLICY_SECTOR_WEIGHT_PCT)
    return float(next((b for b in bands if b is not None), 25))


def portfolio_exposure_from_view(view) -> Optional[dict]:
    if view is None:
        return None
    classified = [x for x in view.sector_exposure if x["sector"] != "UNCLASSIFIED" and x["weight_pct"] is not None]
    classified_total = sum(float(x["weight_pct"]) for x in classified)
    sectors = [{"sector": x["sector"], "weight_pct": float(x["weight_pct"]),
                "share_of_classified_pct": round(100 * float(x["weight_pct"]) / classified_total, 1)
                if classified_total else None}
               for x in classified if float(x["weight_pct"]) >= _sector_attention_level()]
    unclassified = next((float(x["weight_pct"]) for x in view.sector_exposure if x["sector"] == "UNCLASSIFIED"
                         and x["weight_pct"] is not None), 0.0)
    return {"sectors": sectors, "unclassified_pct": unclassified or None}


def fetch_market_inputs(now: datetime) -> MarketInputs:
    """Live, read-only data collection (Alpaca market data, Stage 2.6 events, Alpaca news)."""
    from scanner.market_scanner import analyze_symbols, get_data_client
    errors: List[str] = []
    client = get_data_client()
    spy_sym, tech_sym = config.MARKET_PROXY_SYMBOL, config.MARKET_TECH_PROXY_SYMBOL
    sector_etfs = {name: info["etf"] for name, info in SECTOR_MAP.items()}
    index_syms = sorted({spy_sym, tech_sym, *sector_etfs.values()})
    idx = analyze_symbols(client, index_syms)
    basket = list(analyze_symbols(client, list(config.FALLBACK_UNIVERSE)).values())
    scanner_counts = None
    try:
        from api.routes.market import get_cached_overview
        ov = get_cached_overview()
        scanner_counts = {"scope": "today's screener movers (top gainers, top losers, most active) — not a random "
                                   "sample of the market",
                          "scanned": ov.scanned_symbol_count, "possible_breakouts": len(ov.possible_breakouts),
                          "momentum_stocks": len(ov.momentum_stocks),
                          "large_drops": sum(1 for m in ov.all_metrics.values()
                                             if m.pct_change is not None and m.pct_change <= -signals.LARGE_MOVE_PCT)}
    except Exception as exc:  # noqa: BLE001 - scanner counts are optional
        errors.append(f"scanner counts unavailable ({type(exc).__name__})")
    macro = []
    try:
        from insights.events import upcoming_events
        from services.event_context import build_event_context
        macro = upcoming_events(build_event_context(spy_sym), now, macro_only=True)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"event data unavailable ({type(exc).__name__})")
    news = []
    try:
        from data.news import get_recent_news
        seen = set()
        for sym in (spy_sym, tech_sym):
            for item in get_recent_news(sym, limit=3):
                if item.get("headline") and item["headline"] not in seen:
                    seen.add(item["headline"])
                    news.append({**item, "queried_symbol": sym})
    except Exception as exc:  # noqa: BLE001
        errors.append(f"news unavailable ({type(exc).__name__})")
    return MarketInputs(indices={s: idx.get(s) for s in index_syms}, basket=basket,
                        basket_requested=len(config.FALLBACK_UNIVERSE), scanner_counts=scanner_counts,
                        sector_etf_changes={n: (idx[e].pct_change if idx.get(e) else None) for n, e in sector_etfs.items()},
                        macro_events=macro, news=news[:4], fetched_at=now, errors=errors)
