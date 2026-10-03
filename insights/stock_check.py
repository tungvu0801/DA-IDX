"""
insights/stock_check.py — "What if I add money?" beginner decision SUPPORT (Stage 2.7E). Pure function.

Three separate layers, never collapsed into one recommendation:
  MARKET     insights.market (live, deterministic labels)
  STOCK      EXISTING Stage 2 research (fresh bundle or saved snapshot, with its age) + Stage 1 technicals
  PORTFOLIO  the Stage 2.7C/D scenario engine + policy rules (unchanged arithmetic)

Two deterministic displays, both built only from those facts (no second scoring model, no weights,
no probabilities, no forecasts, no order language):
  FIT      FAVORABLE CONDITIONS | MIXED CONDITIONS | CAUTION CONDITIONS
             CAUTION   if any blocking factor: bearish Research View, HIGH event risk, unreliable quote,
                       research unavailable, or an infeasible (insufficient-cash) cash-funded scenario
             FAVORABLE if the Research View is bullish, bullish evidence exceeds bearish, and there is no
                       caution factor at all
             MIXED     otherwise
  TIMING   MORE SUPPORTIVE CONDITIONS | MIXED — WAIT FOR CONFIRMATION | HIGHER-RISK CONDITIONS
             HIGHER-RISK if HIGH event risk, bearish Research View, unreliable quote, downtrend in a CAUTIOUS
                         market, or an extended stock sitting near resistance
             MORE SUPPORTIVE if bullish research, uptrend, and no stock-timing or portfolio-concentration
                         patience factor
             MIXED — WAIT FOR CONFIRMATION otherwise (with a concrete "what to watch" list)
These are descriptions of current conditions. They do not identify a best entry, predict prices, or tell
the user to trade; the user makes the decision.
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, List, Optional

import config
from analysis import signals
from analysis.evidence_scoring import sector_score
from insights.events import EARNINGS_UNAVAILABLE, EVENT_TIMING_TEXT, earnings_note, upcoming_events
from insights.labels import (BEARISH_VIEWS, BULLISH_VIEWS, MIXED_VIEW, PRICE_LOCATION_TEXT, RESISTANCE_TEXT,
                             SUPPORT_TEXT, UNAVAILABLE, context_label, is_extended, momentum_label, price_location,
                             research_lean, trend_label, volume_label)
from insights.research import ResearchFacts, research_summary
from portfolio.scenarios import ScenarioError, _amount, hypothetical_add

CENT = Decimal("0.01")
CLOSING_TEXT = ("These conditions help you compare the opportunity with your portfolio risk. They do not identify a "
                "guaranteed best entry, and you make the decision.")
LESSON = "A stock can look attractive on its own while still increasing the risk of your overall portfolio."
STAGED_TEXT = ("Splitting an amount into several hypothetical additions reduces the importance of choosing one exact "
               "entry price, but it can also mean paying higher prices if the stock continues rising.")


def _money(v) -> str:
    return "N/A" if v is None else f"${Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP):,}"


def _pct(v) -> str:
    return "N/A" if v is None else f"{Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)}%"


def _factor(code: str, layer: str, text: str) -> dict:
    return {"code": code, "layer": layer, "text": text}


def _concentration(scenario: dict, symbol: str, sector: str) -> dict:
    """Read the unchanged Stage 2.7D policy impact for this symbol's position and sector."""
    impact = scenario.get("policy_impact") or {}
    relevant = []
    for bucket in ("newly_triggered", "escalated", "de_escalated", "resolved", "unchanged"):
        for item in impact.get(bucket, []):
            if (item["family"] == "position_concentration" and item["subject"] == symbol) or \
                    (item["family"] == "sector_concentration" and item["subject"] == sector):
                relevant.append({**item, "bucket": bucket})
    creates = [i for i in relevant if i["bucket"] in ("newly_triggered", "escalated") and i["after_severity"] == "HIGH"]
    worsens = [i for i in relevant if i["bucket"] == "unchanged" and i["after_severity"] == "HIGH"
               and i["after_value"] is not None and i["before_value"] is not None
               and i["after_value"] > i["before_value"]]
    already = [i for i in relevant if i["before_severity"] == "HIGH"]
    return {"items": relevant, "creates_high": creates, "worsens_high": worsens, "already_high": already}


def staged_plans(amount: Decimal, price: Optional[Decimal], support: Optional[Decimal],
                 resistance: Optional[Decimal], weight_after_all: Any, first_part_weight: dict) -> List[dict]:
    """Educational arithmetic only. No schedule, no order, no share count."""
    plans = []
    for label, parts in (("Add all now", 1), ("Half now, half later", 2), ("5 equal additions", 5)):
        each = (amount / parts).quantize(CENT, rounding=ROUND_HALF_UP)
        amounts = [each] * (parts - 1) + [amount - each * (parts - 1)]
        plan = {"plan": label, "additions": parts, "amounts": amounts, "first_addition": amounts[0],
                "weight_after_first_pct": first_part_weight.get(parts),
                "weight_after_all_pct": weight_after_all,
                "weight_note": "Weight after all additions assumes today's prices; real prices will differ."}
        if parts > 1 and price and price > 0:
            illus = {}
            for name, level in (("if later additions happened at today's support", support),
                                ("if later additions happened at today's resistance", resistance)):
                if level and level > 0:
                    # equal-dollar additions -> average price is the harmonic mean of the prices paid
                    avg = Decimal(parts) / (Decimal(1) / price + Decimal(parts - 1) / level)
                    illus[name] = avg.quantize(CENT, rounding=ROUND_HALF_UP)
            plan["illustrative_average_price"] = illus
            plan["illustration_note"] = ("Illustration only, using today's verified levels. It is not a forecast; the "
                                         "price may never reach these levels.")
        plans.append(plan)
    return plans


def build_add_money_check(*, symbol: str, amount_usd: Any, funding: str, view, rules: Optional[list],
                          research: ResearchFacts, metrics: Optional[Any], sector_ctx: Optional[Any],
                          events_bundle: Optional[Any], market: Optional[dict], sector_fn, now: datetime) -> dict:
    symbol = symbol.strip().upper()
    amount = _amount(amount_usd)
    if funding not in ("new_money", "cash"):
        raise ScenarioError("funding must be 'new_money' or 'cash'")
    held = next((p for p in view.positions if p.symbol == symbol), None)
    event_level = getattr(events_bundle, "event_risk_level", None) or (held.event_risk_level if held else None)
    scenario = hypothetical_add(view, symbol, amount, funding, sector=sector_fn(symbol), rules=rules,
                                event_level=event_level)
    sector = scenario["sector"]

    # ---- feasibility (cash-funded) --------------------------------------------------------------
    cash = view.snapshot.cash
    feasible = not scenario["insufficient_cash"]
    feasibility = {"feasible": feasible, "funding": funding, "available_cash": cash, "amount": amount,
                   "additional_cash_needed": (amount - cash) if (not feasible and cash is not None) else None,
                   "label": "INSUFFICIENT CASH" if not feasible else "OK",
                   "suggestion": "Switch the scenario to NEW MONEY to see the effect of adding new money instead."
                   if not feasible else None}

    # ---- price & stock technicals (existing Stage 1 metrics) ------------------------------------
    if held is not None and held.last_price is not None:
        price, price_src, price_time, quote_quality = held.last_price, "Robinhood quote", held.price_timestamp, \
            held.quote_quality
    elif metrics is not None:
        price, price_src, price_time, quote_quality = Decimal(str(metrics.price)), "market data (Alpaca)", \
            getattr(metrics, "as_of", None), "OK"
    else:
        price, price_src, price_time, quote_quality = None, None, None, UNAVAILABLE
    m = metrics
    support = Decimal(str(round(m.support, 2))) if m is not None and m.support is not None else None
    resistance = Decimal(str(round(m.resistance, 2))) if m is not None and m.resistance is not None else None
    location = price_location(float(price) if price is not None else None,
                              float(support) if support is not None else None,
                              float(resistance) if resistance is not None else None)
    trend = trend_label(m.trend) if m is not None else UNAVAILABLE
    momentum = momentum_label(m.momentum_score) if m is not None else UNAVAILABLE
    volume = volume_label(m.relative_volume) if m is not None else UNAVAILABLE
    extended = is_extended(m.momentum_5d_pct, m.rsi) if m is not None else False
    mom5 = m.momentum_5d_pct if m is not None else None
    sector_pct = getattr(sector_ctx, "sector_pct_change", None) if sector_ctx is not None else None
    sector_label = context_label(sector_score(sector_ctx)) if sector_ctx is not None else UNAVAILABLE
    market_env = market.get("environment") if market and market.get("available") else UNAVAILABLE

    # ---- events (Stage 2.6) -----------------------------------------------------------------------
    upcoming = upcoming_events(events_bundle, now)
    important = next((e for e in upcoming if e["severity"] in ("HIGH", "MEDIUM")
                      and e["days_until"] <= config.EVENT_LOW_RISK_DAYS), None)
    level = event_level or UNAVAILABLE

    # ---- research (existing Stage 2 output, never recomputed) --------------------------------------
    rs = research_summary(research)
    view_label = research.research_view if research.available else None
    bullish_gt_bearish = research.available and (research.bullish_pct or 0) > (research.bearish_pct or 0)
    conc = _concentration(scenario, symbol, sector)

    # ---- FIT factors ---------------------------------------------------------------------------------
    support_f, caution_f, blocking = [], [], []
    if view_label in BULLISH_VIEWS:
        support_f.append(_factor("research_bullish", "STOCK", f"{symbol} research currently has a {view_label.title()}."))
    if bullish_gt_bearish:
        support_f.append(_factor("bullish_exceeds_bearish", "STOCK",
                                 f"Bullish evidence ({research.bullish_pct}%) is stronger than bearish evidence "
                                 f"({research.bearish_pct}%)."))
    cats = research.category_scores if research.available else {}
    for cat, text_pos in (("technical", "Technical structure is supportive in the research."),
                          ("catalyst", "Catalyst evidence is positive in the research.")):
        if cats.get(cat) is not None and cats[cat] > 0:
            support_f.append(_factor(f"{cat}_supportive", "STOCK", text_pos))
    if market_env == "SUPPORTIVE":
        support_f.append(_factor("market_supportive", "MARKET", "The broader market context is supportive."))
    if sector_label == "SUPPORTIVE":
        support_f.append(_factor("sector_supportive", "MARKET",
                                 f"The {sector} sector ETF is {sector_pct:+.2f}% today (supportive)."))
    if level != "HIGH" and level != UNAVAILABLE:
        support_f.append(_factor("no_high_event", "STOCK", "No known HIGH event risk is currently identified."))
    if rules is not None and not conc["creates_high"] and not conc["worsens_high"]:
        support_f.append(_factor("no_new_high_concentration", "PORTFOLIO",
                                 "This addition does not create a new HIGH concentration flag."))
    if quote_quality == "OK" and research.available and not research.stale:
        support_f.append(_factor("data_fresh", "DATA", "Quote and research data are fresh."))

    if view_label == MIXED_VIEW:
        caution_f.append(_factor("research_mixed", "STOCK", f"{symbol} research is currently MIXED / WAIT."))
    if view_label in BEARISH_VIEWS:
        blocking.append(_factor("research_bearish", "STOCK", f"{symbol} research currently has a {view_label.title()}."))
    if research.available and (research.bearish_pct or 0) >= config.BEGINNER_BEARISH_MATERIAL_PCT:
        caution_f.append(_factor("bearish_material", "STOCK",
                                 f"Bearish evidence is material ({research.bearish_pct}% of the research evidence)."))
    if market_env == "CAUTIOUS":
        caution_f.append(_factor("market_weak", "MARKET", "The broader market context is cautious."))
    if sector_label == "WEAK":
        caution_f.append(_factor("sector_weak", "MARKET", f"The {sector} sector ETF is {sector_pct:+.2f}% today (weak)."))
    if level == "HIGH":
        blocking.append(_factor("high_event_risk", "STOCK", "There is HIGH event risk (a major event is very close)."))
    for i in conc["creates_high"] + conc["worsens_high"]:
        what = f"{symbol}'s weight" if i["family"] == "position_concentration" else f"{sector} exposure"
        verb = "would reach" if i in conc["creates_high"] else "is already HIGH and would rise"
        caution_f.append(_factor("concentration_high", "PORTFOLIO",
                                 f"{what} {verb} from {_pct(i['before_value'])} to {_pct(i['after_value'])} "
                                 "(HIGH concentration level)."))
    if quote_quality in ("STALE", "DEGRADED"):
        caution_f.append(_factor("quote_stale", "DATA", f"The {symbol} quote is {quote_quality.lower()}."))
    if quote_quality in ("UNRELIABLE",) or (held is not None and held.last_price is None):
        blocking.append(_factor("quote_unreliable", "DATA", f"The {symbol} quote is unreliable or unavailable."))
    if not research.available:
        blocking.append(_factor("research_unavailable", "STOCK", f"No research is available for {symbol} yet."))
    elif research.stale:
        caution_f.append(_factor("research_stale", "DATA",
                                 f"The research is {research.age_hours} hours old (older than "
                                 f"{config.BEGINNER_RESEARCH_STALE_HOURS:g} hours) — refresh it before relying on it."))
    if sector == "UNCLASSIFIED":
        caution_f.append(_factor("sector_unclassified", "PORTFOLIO",
                                 f"{symbol} has no verified sector, so its sector impact cannot be assessed."))
    if not feasible:
        blocking.append(_factor("insufficient_cash", "PORTFOLIO",
                                f"Not enough cash: {_money(cash)} available for a {_money(amount)} amount."))

    if blocking:
        fit_state = "CAUTION CONDITIONS"
    elif view_label in BULLISH_VIEWS and bullish_gt_bearish and not caution_f:
        fit_state = "FAVORABLE CONDITIONS"
    else:
        fit_state = "MIXED CONDITIONS"

    # ---- TIMING factors ------------------------------------------------------------------------------
    t_sup, t_pat, t_block = [], [], []
    if location == "NEAR_SUPPORT" and not extended:
        t_sup.append(_factor("near_support", "STOCK", f"Price is near verified support ({_money(support)}) rather "
                                                       "than extended."))
    if trend == "UPTREND" and (mom5 or 0) > 0:
        t_sup.append(_factor("trend_improving", "STOCK", f"The technical trend is up (5-day change {mom5:+.2f}%)."))
    if momentum == "STRONG":
        t_sup.append(_factor("momentum_positive", "STOCK", f"Momentum is positive (momentum score {m.momentum_score})."))
    if m is not None and m.relative_volume is not None and m.relative_volume >= config.RISK_LOW_RVOL_THRESHOLD \
            and (m.pct_change or 0) > 0:
        t_sup.append(_factor("volume_confirms", "STOCK",
                             f"Volume confirms today's move (relative volume {m.relative_volume:.2f}x)."))
    if bullish_gt_bearish:
        t_sup.append(_factor("bullish_exceeds_bearish", "STOCK", "Bullish evidence exceeds bearish evidence."))
    if market_env == "SUPPORTIVE":
        t_sup.append(_factor("market_supportive", "MARKET", "Broader market context is supportive."))
    if sector_label == "SUPPORTIVE":
        t_sup.append(_factor("sector_supportive", "MARKET", "Sector context is supportive."))
    if level in ("LOW", "NONE") and important is None:
        t_sup.append(_factor("no_near_event", "STOCK", "No HIGH near-term event risk."))
    if quote_quality == "OK" and research.available and not research.stale:
        t_sup.append(_factor("data_fresh", "DATA", "Quote and research data are fresh."))

    if location == "NEAR_RESISTANCE":
        t_pat.append(_factor("near_resistance", "STOCK", f"Price is near resistance ({_money(resistance)})."))
    if extended:
        bits = []
        if mom5 is not None:
            bits.append(f"5-day change {mom5:+.2f}%")
        if m.rsi is not None:
            bits.append(f"RSI {m.rsi:.1f}")
        t_pat.append(_factor("extended", "STOCK", f"The stock has risen quickly / is extended ({', '.join(bits)})."))
    if momentum == "WEAK" or (mom5 is not None and mom5 < 0):
        t_pat.append(_factor("momentum_weakening", "STOCK", "Momentum is weakening."))
    if market_env == "CAUTIOUS":
        t_pat.append(_factor("market_weak", "MARKET", "The broader market trend is cautious."))
    if sector_label == "WEAK":
        t_pat.append(_factor("sector_weak", "MARKET", "The sector trend is weak today."))
    if important is not None and level != "HIGH":
        t_pat.append(_factor("event_approaching", "STOCK", f"An important event is approaching: {important['title']} "
                                                           f"on {important['date']}."))
    if level == "HIGH":
        t_block.append(_factor("high_event_risk", "STOCK", "HIGH event risk."))
    if quote_quality in ("STALE", "DEGRADED") or (research.available and research.stale):
        t_pat.append(_factor("stale_data", "DATA", "Quote or research data is stale."))
    if conc["already_high"]:
        t_pat.append(_factor("portfolio_high_exposure", "PORTFOLIO",
                             f"Your portfolio already has high exposure here ({sector if sector != 'UNCLASSIFIED' else symbol})."))
    if conc["creates_high"] or conc["worsens_high"]:
        t_pat.append(_factor("addition_worsens_concentration", "PORTFOLIO",
                             "This hypothetical addition creates or worsens a HIGH concentration."))
    if view_label in BEARISH_VIEWS:
        t_block.append(_factor("research_bearish", "STOCK", "Research View is bearish."))
    if quote_quality == "UNRELIABLE":
        t_block.append(_factor("quote_unreliable", "DATA", "The quote is unreliable."))
    if trend == "DOWNTREND" and market_env == "CAUTIOUS":
        t_block.append(_factor("downtrend_in_cautious_market", "STOCK", "The stock is in a downtrend while the market is "
                                                                        "cautious."))
    if extended and location == "NEAR_RESISTANCE":
        t_block.append(_factor("extended_near_resistance", "STOCK", "The stock is extended and near resistance."))

    stock_patience = [f for f in t_pat if f["layer"] != "PORTFOLIO"]
    portfolio_patience = [f for f in t_pat if f["layer"] == "PORTFOLIO"]
    if t_block:
        timing = "HIGHER-RISK CONDITIONS"
    elif view_label in BULLISH_VIEWS and trend == "UPTREND" and not t_pat:
        timing = "MORE SUPPORTIVE CONDITIONS"
    else:
        timing = "MIXED — WAIT FOR CONFIRMATION"
    stock_setup = ("HIGHER-RISK" if [f for f in t_block if f["layer"] != "PORTFOLIO"] else
                   "SUPPORTIVE" if view_label in BULLISH_VIEWS and trend == "UPTREND" and not stock_patience else "MIXED")
    portfolio_setup = ("HIGH CONCENTRATION WOULD INCREASE" if (conc["creates_high"] or conc["worsens_high"]) else
                       "ALREADY CONCENTRATED" if conc["already_high"] else "NO CONCENTRATION CONCERN")

    # ---- what to watch (only conditions relevant to this stock) ------------------------------------
    watch = []
    if support is not None and location in ("NEAR_SUPPORT", "MIDDLE_OF_RANGE", "NO_RESISTANCE_ABOVE"):
        watch.append(f"Price holds above support ({_money(support)}).")
    if support is not None and (extended or location == "NEAR_RESISTANCE"):
        watch.append(f"Price pulls back toward support ({_money(support)}).")
    if momentum in ("WEAK", "NORMAL") or (mom5 is not None and mom5 < 0):
        watch.append("Momentum improves (momentum score above 50 and a positive 5-day change).")
    if volume in ("WEAK", "NORMAL"):
        watch.append(f"Volume strengthens (relative volume above {config.RISK_LOW_RVOL_THRESHOLD:g}x on up days).")
    if resistance is not None and location in ("NEAR_RESISTANCE", "MIDDLE_OF_RANGE", "NO_SUPPORT_BELOW"):
        watch.append(f"Price breaks above resistance ({_money(resistance)}) with volume above "
                     f"{signals.UNUSUAL_VOLUME_RVOL:g}x.")
    if market_env in ("CAUTIOUS", "MIXED"):
        watch.append("The broad market trend improves.")
    if sector_label in ("WEAK", "MIXED"):
        watch.append(f"The {sector} sector trend improves.")
    if important is not None:
        watch.append(f"The {important['title']} on {important['date']} passes.")
    if view_label not in BULLISH_VIEWS and research.available:
        watch.append(f"The Research View improves (currently {view_label}).")
    if research.available and research.stale or not research.available:
        watch.append("Fresh research is generated for this stock.")
    if conc["already_high"] or conc["creates_high"] or conc["worsens_high"]:
        target = sector if any(i["family"] == "sector_concentration" for i in conc["items"]) else symbol
        watch.append(f"Your portfolio concentration in {target} decreases.")

    # ---- staged additions (new-money arithmetic; educational only) -----------------------------------
    first_part_weight = {}
    for parts in (1, 2, 5):
        first = (amount / parts).quantize(CENT, rounding=ROUND_HALF_UP)
        part_scn = hypothetical_add(view, symbol, first, funding, sector=sector_fn(symbol))
        first_part_weight[parts] = part_scn["position_weight_after_pct"]
    staged = staged_plans(amount, price, support, resistance, scenario["position_weight_after_pct"], first_part_weight)

    # ---- plain-language explanations (deterministic templates) --------------------------------------
    lean = research_lean(view_label)
    stock_phrase = {"POSITIVE": "leans positive", "NEGATIVE": "leans negative", "MIXED": "is mixed"}.get(
        lean, "is not available yet")
    market_phrase = {"SUPPORTIVE": "supportive", "MIXED": "mixed", "CAUTIOUS": "cautious"}.get(market_env,
                                                                                            "unavailable")
    if conc["creates_high"] or conc["worsens_high"] or conc["already_high"]:
        port_phrase = (f"your portfolio already has substantial {sector} exposure" if sector != "UNCLASSIFIED"
                       else f"your portfolio is already concentrated in {symbol}")
    else:
        port_phrase = "this addition does not raise a HIGH concentration flag in your portfolio"
    interpretation = (f"The individual stock evidence currently {stock_phrase}, while the broader market is "
                      f"{market_phrase} and {port_phrase}.")
    if fit_state == "MIXED CONDITIONS" and lean == "POSITIVE" and (conc["creates_high"] or conc["worsens_high"]):
        fit_text = ("The stock research currently leans positive, but your portfolio already has a lot of "
                    f"{sector if sector != 'UNCLASSIFIED' else symbol} exposure. Adding money would increase that "
                    "concentration further. The stock evidence and your portfolio risk are therefore pointing in "
                    "different directions.")
    elif fit_state == "FAVORABLE CONDITIONS":
        fit_text = ("The current research and your portfolio conditions do not conflict for this addition. That does "
                    "not make the outcome certain.")
    elif fit_state == "CAUTION CONDITIONS" and [f["code"] for f in blocking] == ["research_unavailable"]:
        fit_text = (f"There is no research for {symbol} yet, so the stock side cannot be evaluated. Use \"Refresh "
                    "research for this stock\" to generate it; the portfolio facts above still apply.")
    elif fit_state == "CAUTION CONDITIONS":
        fit_text = "At least one important caution applies right now (listed above), so extra care is warranted."
    else:
        fit_text = "Some facts support adding and others argue for patience; they are listed above so you can weigh them."
    timing_text = {
        ("SUPPORTIVE", "NO CONCENTRATION CONCERN"): "The stock's setup looks supportive and the addition does not "
                                                    "raise a concentration concern.",
        ("SUPPORTIVE", "HIGH CONCENTRATION WOULD INCREASE"): "The stock's setup is improving, but adding it would "
                                                             "increase an already concentrated part of your portfolio.",
        ("SUPPORTIVE", "ALREADY CONCENTRATED"): "The stock's setup is improving, but this part of your portfolio is "
                                                "already concentrated.",
    }.get((stock_setup, portfolio_setup),
          "The stock's setup is not clearly supportive yet; the watch list shows what could improve it." if
          stock_setup == "MIXED" else "The stock's setup currently carries higher risk (see the reasons listed).")

    events_block = {"event_risk": level, "upcoming": upcoming, "important_event_ahead": important,
                    "earnings_note": earnings_note(events_bundle), "explanation": EVENT_TIMING_TEXT}
    price_area = {"current_price": price, "price_source": price_src, "price_time": price_time,
                  "quote_quality": quote_quality, "support": support, "resistance": resistance,
                  "atr": round(m.atr, 2) if m is not None and m.atr is not None else None,
                  "location": location, "location_text": PRICE_LOCATION_TEXT[location],
                  "support_text": SUPPORT_TEXT, "resistance_text": RESISTANCE_TEXT,
                  "levels_as_of": getattr(m, "as_of", None) if m is not None else None}
    stock_trend = {"trend": trend, "momentum": momentum, "price_location": location, "volume": volume,
                   "event_risk": level, "research_view": view_label or "UNAVAILABLE",
                   "why": None if m is None else {
                       "trend_basis": "EMA stacking (9/20/50-day)", "ema_9": round(m.ema_fast, 2) if m.ema_fast else None,
                       "ema_20": round(m.ema_medium, 2) if m.ema_medium else None,
                       "ema_50": round(m.ema_slow, 2) if m.ema_slow else None,
                       "momentum_score": m.momentum_score, "momentum_5d_pct": round(m.momentum_5d_pct, 2)
                       if m.momentum_5d_pct is not None else None,
                       "rsi": round(m.rsi, 1) if m.rsi is not None else None,
                       "relative_volume": round(m.relative_volume, 2) if m.relative_volume is not None else None,
                       "dist_from_support_pct": round(m.dist_from_support_pct, 2)
                       if m.dist_from_support_pct is not None else None,
                       "dist_from_resistance_pct": round(m.dist_from_resistance_pct, 2)
                       if m.dist_from_resistance_pct is not None else None,
                       "daily_change_pct": round(m.pct_change, 2), "as_of": getattr(m, "as_of", None),
                       "near_level_rule_pct": config.APPROACHING_LEVEL_PCT}}
    layers = {
        "market": {"environment": market_env,
                   "trend": market.get("trend") if market else None,
                   "volatility": market.get("volatility") if market else None,
                   "breadth": market.get("breadth_label") if market else None,
                   "market_today": market.get("market_today") if market else None,
                   "sector": sector, "sector_today": sector_label, "sector_pct_change": sector_pct,
                   "fetched_at": market.get("fetched_at") if market else None},
        "stock": {"symbol": symbol, "research": rs, "trend": trend, "momentum": momentum},
        "portfolio": {"position_weight_before_pct": scenario["position_weight_before_pct"],
                      "position_weight_after_pct": scenario["position_weight_after_pct"],
                      "position_value_before": scenario["position_value_before"],
                      "position_value_after": scenario["position_value_after"],
                      "sector": sector, "sector_weight_before_pct": scenario["sector_weight_before_pct"],
                      "sector_weight_after_pct": scenario["sector_weight_after_pct"],
                      "cash_before": scenario["cash_before"], "cash_after": scenario["cash_after"],
                      "concentration": conc["items"], "setup": portfolio_setup},
        "interpretation": interpretation,
    }
    summary_card = {
        "title": f"{symbol} — ADDING-MONEY CHECK", "current_price": price, "research_view": view_label or "UNAVAILABLE",
        "research_stale": research.stale if research.available else True, "stock_trend": trend,
        "market": market_env, "price_location": location, "event_risk": level,
        "exposure_before_pct": scenario["position_weight_before_pct"],
        "exposure_after_pct": scenario["position_weight_after_pct"], "sector": sector,
        "sector_before_pct": scenario["sector_weight_before_pct"],
        "sector_after_pct": scenario["sector_weight_after_pct"], "timing": timing,
        "supports": [f["text"] for f in t_sup], "patience": [f["text"] for f in t_pat + t_block],
        "watch": watch, "closing": CLOSING_TEXT,
    }
    return {
        "status": "OK", "symbol": symbol, "amount_usd": amount, "funding": funding, "feasibility": feasibility,
        "layers": layers,
        "fit": {"state": fit_state, "supporting": support_f, "caution": caution_f + blocking,
                "blocking": [f["code"] for f in blocking], "explanation": fit_text, "lesson": LESSON},
        "timing": {"status": timing, "stock_setup": stock_setup, "portfolio_setup": portfolio_setup,
                   "supporting": t_sup, "patience": t_pat + t_block, "watch_for": watch, "explanation": timing_text},
        "price_area": price_area, "stock_trend": stock_trend, "events": events_block,
        "staged": {"plans": staged, "explanation": STAGED_TEXT},
        "summary_card": summary_card, "scenario": scenario,
        "disclaimer": ("Decision support only. Nothing here is a buy/sell/hold instruction, a price forecast or a "
                       "probability, and no order was created, prepared or sent."),
    }
