"""Stage 2.7E: "What if I add money?" decision support — deterministic fit + timing (no prediction model)."""
import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from e_fixtures import RESOLVER, RULES, bundle, ev, market, no_research, research, sector_ctx, tm, view
from insights.events import EARNINGS_UNAVAILABLE
from insights.stock_check import build_add_money_check
from pf_fixtures import POSITIONS
from portfolio.models import to_jsonable
from portfolio.scenarios import ScenarioError

D = Decimal
NOW = datetime.now(timezone.utc)


def check(symbol="AMD", amount=10, funding="new_money", r=None, m=None, sec=0.5, events=None, mk=None, v=None):
    return build_add_money_check(symbol=symbol, amount_usd=amount, funding=funding, view=v or view(), rules=RULES,
                                 research=r if r is not None else research(), metrics=m if m is not None else tm(symbol),
                                 sector_ctx=sector_ctx(sec) if sec is not None else None,
                                 events_bundle=events if events is not None else bundle("LOW"),
                                 market=mk if mk is not None else market("MIXED"), sector_fn=RESOLVER.sector_for, now=NOW)


def codes(fs):
    return {f["code"] for f in fs}


# ---- fit state ---------------------------------------------------------------------------------------------

def test_favorable_conditions_when_everything_agrees():
    c = check()
    assert c["fit"]["state"] == "FAVORABLE CONDITIONS" and not c["fit"]["caution"]
    assert {"research_bullish", "bullish_exceeds_bearish", "sector_supportive", "no_high_event",
            "no_new_high_concentration", "data_fresh"} <= codes(c["fit"]["supporting"])
    assert c["timing"]["status"] == "MORE SUPPORTIVE CONDITIONS"


def test_bullish_research_with_concentrated_portfolio_is_mixed():
    c = check("MU", 500)          # MU 1.20% -> 34.13% creates a HIGH position flag
    assert c["fit"]["state"] == "MIXED CONDITIONS"
    assert "concentration_high" in codes(c["fit"]["caution"])
    assert "pointing in different directions" in c["fit"]["explanation"]
    assert c["timing"]["portfolio_setup"] == "HIGH CONCENTRATION WOULD INCREASE"
    assert c["fit"]["lesson"].startswith("A stock can look attractive on its own")
    assert "portfolio already has substantial" in c["layers"]["interpretation"]


def test_bullish_stock_weak_market():
    c = check(mk=market("CAUTIOUS"))
    assert c["fit"]["state"] == "MIXED CONDITIONS" and "market_weak" in codes(c["fit"]["caution"])
    assert c["timing"]["status"] == "MIXED — WAIT FOR CONFIRMATION"
    assert "The broad market trend improves." in c["timing"]["watch_for"]


def test_bearish_research_strong_market_is_caution():
    c = check(r=research("BEARISH BIAS", 20, 30, 50), mk=market("SUPPORTIVE"))
    assert c["fit"]["state"] == "CAUTION CONDITIONS" and "research_bearish" in c["fit"]["blocking"]
    assert c["timing"]["status"] == "HIGHER-RISK CONDITIONS"
    assert "market_supportive" in codes(c["fit"]["supporting"])     # both sides still shown


def test_high_event_risk():
    c = check(events=bundle("HIGH", macro=[ev(hours=6)]))
    assert c["fit"]["state"] == "CAUTION CONDITIONS" and "high_event_risk" in c["fit"]["blocking"]
    assert c["timing"]["status"] == "HIGHER-RISK CONDITIONS"
    assert c["events"]["important_event_ahead"]["title"] == "Employment Situation"


def test_stale_research_and_unavailable_research():
    c = check(r=research(age_hours=48))
    assert "research_stale" in codes(c["fit"]["caution"]) and c["fit"]["state"] == "MIXED CONDITIONS"
    assert "Fresh research is generated for this stock." in c["timing"]["watch_for"]
    u = check(r=no_research())
    assert u["fit"]["state"] == "CAUTION CONDITIONS" and "research_unavailable" in u["fit"]["blocking"]


def test_stale_quote_for_held_position():
    pos = copy.deepcopy(POSITIONS)
    for k in ("venue_last_trade_time", "venue_last_non_reg_trade_time"):
        pos["quotes"]["NVDA"][k] = "2026-09-25T10:00:00Z"
    c = check("NVDA", 10, v=view(positions=pos))
    assert "quote_stale" in codes(c["fit"]["caution"]) and "stale_data" in codes(c["timing"]["patience"])


def test_unclassified_sector():
    c = check("SHOP", 10)
    assert "sector_unclassified" in codes(c["fit"]["caution"])
    assert c["scenario"]["sector_classification_uncertain"] is True


def test_market_context_unavailable_never_guessed():
    c = check(mk={"available": False})
    assert c["layers"]["market"]["environment"] == "UNAVAILABLE"
    assert not ({"market_supportive", "market_weak"} & (codes(c["fit"]["supporting"]) | codes(c["fit"]["caution"])))


# ---- price area & timing ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("price,loc,text", [(10.6, "NEAR_SUPPORT", "Price is closer to support."),
                                            (13.3, "NEAR_RESISTANCE", "Price is closer to resistance."),
                                            (12.0, "MIDDLE_OF_RANGE", "Price is in the middle of its recent range.")])
def test_price_location(price, loc, text):
    c = check(m=tm("AMD", price=price))
    pa = c["price_area"]
    assert pa["location"] == loc and pa["location_text"] == text
    assert "does not guarantee" in pa["support_text"] and "does not guarantee" in pa["resistance_text"]
    assert pa["support"] == D("10.5") and pa["resistance"] == D("13.5")


def test_extended_near_resistance_is_higher_risk_and_watch_lists_confirmation():
    c = check(m=tm("AMD", price=13.3, momentum_5d_pct=12.0, rsi=75.0))
    assert c["timing"]["status"] == "HIGHER-RISK CONDITIONS"
    assert {"near_resistance", "extended"} <= codes(c["timing"]["patience"])
    assert any("breaks above resistance ($13.50)" in w for w in c["timing"]["watch_for"])


def test_confirmation_conditions_only_relevant_ones():
    c = check(m=tm("AMD", momentum_score=35, momentum_5d_pct=-1.0, relative_volume=0.8, trend="Neutral"),
              mk=market("MIXED"))
    w = " ".join(c["timing"]["watch_for"])
    assert "Momentum improves" in w and "Volume strengthens" in w and "broad market trend improves" in w
    assert "event" not in w.lower()          # no event in this case -> not listed
    assert c["timing"]["status"] == "MIXED — WAIT FOR CONFIRMATION"


def test_unavailable_event_data_says_earnings_unavailable():
    c = check(events=None)
    assert c["events"]["earnings_note"] == EARNINGS_UNAVAILABLE


# ---- scenarios -----------------------------------------------------------------------------------------------------

def test_new_money_vs_current_cash_and_insufficient_cash():
    nm = check("MU", 500, "new_money")
    assert nm["feasibility"]["feasible"] and nm["layers"]["portfolio"]["cash_after"] == D("10.00")
    cash = check("MU", 500, "cash")
    f = cash["feasibility"]
    assert not f["feasible"] and f["label"] == "INSUFFICIENT CASH"
    assert f["additional_cash_needed"] == D("490.00") and "NEW MONEY" in f["suggestion"]
    assert "insufficient_cash" in cash["fit"]["blocking"] and cash["fit"]["state"] == "CAUTION CONDITIONS"
    assert cash["scenario"]["infeasible_label"] == "INFEASIBLE SCENARIO" and cash["scenario"]["cash_after"] == D("-490.00")
    small = check("MU", 5, "cash")
    assert small["feasibility"]["feasible"] and small["layers"]["portfolio"]["cash_after"] == D("5.00")


def test_staged_addition_arithmetic():
    c = check(m=tm("AMD", price=12.0), amount=500)
    plans = {p["plan"]: p for p in c["staged"]["plans"]}
    assert plans["Add all now"]["amounts"] == [D("500")]
    assert plans["Half now, half later"]["amounts"] == [D("250.00"), D("250.00")]
    assert plans["5 equal additions"]["amounts"] == [D("100.00")] * 5
    for p in plans.values():
        assert sum(p["amounts"]) == D("500")
    avg = plans["Half now, half later"]["illustrative_average_price"]
    assert avg["if later additions happened at today's support"] == D("11.20")   # 2 / (1/12 + 1/10.5)
    assert plans["Add all now"]["weight_after_first_pct"] == plans["Add all now"]["weight_after_all_pct"]
    assert plans["5 equal additions"]["weight_after_first_pct"] < plans["5 equal additions"]["weight_after_all_pct"]
    assert "can also mean paying higher prices" in c["staged"]["explanation"]


def test_invalid_scenarios():
    with pytest.raises(ScenarioError):
        check(amount=0)
    with pytest.raises(ScenarioError):
        check(funding="margin")


# ---- isolation: no second prediction model, no probabilities, no orders -----------------------------------------

def test_no_second_prediction_model_or_probability_or_order_language():
    src = Path(__file__).resolve().parents[1].joinpath("insights", "stock_check.py").read_text(encoding="utf-8")
    for forbidden in ("compute_evidence(", "technical_score(", "catalyst_score(", "research_view_label("):
        assert forbidden not in src
    for c in (check(), check("MU", 500), check(mk=market("CAUTIOUS")), check(events=bundle("HIGH", macro=[ev(hours=6)]))):
        generated = {k: v for k, v in to_jsonable(c).items() if k != "disclaimer"}   # disclaimer negates these words
        text = json.dumps(generated).lower()
        for bad in ("probab", "% chance", "will rise", "will fall", "guarantee", "buy now", "sell now", "perfect entry",
                    "put all", "you should"):
            assert bad not in text.replace("does not guarantee", "").replace("guaranteed best entry", ""), bad
        assert not (set(c) & {"order", "order_type", "limit_price", "quantity", "shares"})


def test_research_values_are_copied_not_recomputed():
    r = research("BULLISH BIAS", 51, 42, 7)
    c = check(r=r)
    rs = c["layers"]["stock"]["research"]
    assert (rs["research_view"], rs["bullish_pct"], rs["neutral_pct"], rs["bearish_pct"]) == ("BULLISH BIAS", 51, 42, 7)


@pytest.mark.parametrize("kw,loc", [({"price": 15.0, "resistance": None}, "NO_RESISTANCE_ABOVE"),
                                    ({"price": 9.0, "support": None}, "NO_SUPPORT_BELOW")])
def test_missing_level_is_reported_not_invented(kw, loc):
    c = check(m=tm("AMD", **kw))
    assert c["price_area"]["location"] == loc and "no nearby" in c["price_area"]["location_text"]


def test_no_contradictory_concentration_factors():
    c = check("MU", 500)
    assert not ("no_new_high_concentration" in codes(c["fit"]["supporting"]) and
                "concentration_high" in codes(c["fit"]["caution"]))


def test_research_unavailable_explanation_points_to_refresh():
    c = check(r=no_research())
    assert "Refresh research" in c["fit"]["explanation"]
