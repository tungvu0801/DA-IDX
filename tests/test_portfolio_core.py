"""Stage 2.7C: models, quote sanity, analytics, tax lots, risk facts, scenarios (pure, no network)."""
import copy
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from data.sector_map import get_sector_for_symbol
from pf_fixtures import NOW, PORTFOLIO, POSITIONS, REALIZED, TAX_LOTS, quote
from portfolio.analytics import build_tax_lots, build_view, long_term_date
from portfolio.models import Quote, RealizedPnlSummary, dec, to_jsonable, ts
from portfolio.quotes import assess_quote
from portfolio.risk_facts import build_risk_facts
from portfolio.scenarios import ScenarioError, run_scenario

D = Decimal


def meta(status="OK"):
    return {"status": status, "fetched_at": "2026-09-28T00:04:59+00:00", "cache_age_s": 1, "message": None}


def view(positions=POSITIONS, portfolio=PORTFOLIO, event_fn=None, stale=900, gap=0.5):
    return build_view(copy.deepcopy(portfolio), meta(), copy.deepcopy(positions), meta(),
                      RealizedPnlSummary.from_gateway(REALIZED), now=NOW, stale_after_s=stale,
                      gap_material_pct=gap, sector_fn=get_sector_for_symbol, event_fn=event_fn)


def by_symbol(v):
    return {p.symbol: p for p in v.positions}


# ---- Decimal parsing -----------------------------------------------------------------------------------

def test_decimal_parsing_is_exact_and_never_zero_fills():
    assert dec("8365.5631868669") == D("8365.5631868669")
    assert dec(None) is None and dec("") is None and dec("abc") is None and dec("NaN") is None and dec(True) is None
    assert ts("2026-09-25T19:59:59.998759419Z").microsecond == 998759
    assert ts(None) is None and ts("not a time") is None


def test_market_value_uses_exact_decimal_arithmetic():
    pos = {**POSITIONS, "positions": [dict(POSITIONS["positions"][0], quantity="3.981584")]}
    v = view(positions=pos)
    assert by_symbol(v)["NVDA"].market_value == (D("3.981584") * D("111.00")).quantize(D("0.01"))


# ---- quote sanity -----------------------------------------------------------------------------------------

def q(**kw):
    return Quote.from_gateway(quote("AAA", kw.pop("price", "10.00"), **kw))


def test_latest_timestamp_wins_extended_newer():
    a = assess_quote(q(reg_price="9.00"), NOW, 900)
    assert (a.quality, a.price_source, a.price) == ("OK", "EXTENDED_LAST", D("10.00"))
    assert a.price_timestamp == ts("2026-09-28T00:00:00.123456789Z")


def test_latest_timestamp_wins_regular_newer():
    a = assess_quote(q(reg_price="9.00", reg_time="2026-09-28T00:04:00Z"), NOW, 900)
    assert (a.price_source, a.price) == ("REGULAR_LAST", D("9.00"))


def test_crossed_quote_is_flagged_degraded_not_hidden():
    a = assess_quote(q(reg_price="9.9", bid="10.10", ask="10.00"), NOW, 900)
    assert a.crossed and a.quality == "DEGRADED" and a.usable and "CROSSED_QUOTE" in a.issues


@pytest.mark.parametrize("kw,issue", [({"has_traded": False}, "NOT_TRADED"), ({"state": "halted"}, "STATE_NOT_ACTIVE")])
def test_unreliable_quotes_are_not_used(kw, issue):
    a = assess_quote(q(reg_price="9.9", **kw), NOW, 900)
    assert a.quality == "UNRELIABLE" and not a.usable and a.price is None and issue in a.issues


def test_missing_regular_last_trade_extended_only():
    a = assess_quote(q(reg_price=None), NOW, 900)
    assert a.quality == "DEGRADED" and a.usable
    assert {"MISSING_LAST_TRADE", "EXTENDED_HOURS_ONLY"} <= set(a.issues)


def test_price_without_timestamp_is_never_selected():
    a = assess_quote(q(reg_price="9.00", reg_time=None, ext_time=None), NOW, 900)
    assert a.quality == "UNAVAILABLE" and a.price is None and "MISSING_TIMESTAMP" in a.issues
    b = assess_quote(q(reg_price="9.00", reg_time=None), NOW, 900)
    assert b.quality == "DEGRADED" and b.price_source == "EXTENDED_LAST"


def test_stale_quote_is_labelled():
    a = assess_quote(q(reg_price="9.00"), NOW + timedelta(hours=2), 900)
    assert a.quality == "STALE" and a.usable and a.age_seconds > 900 and "STALE_QUOTE" in a.issues


def test_no_quote_is_unavailable():
    a = assess_quote(None, NOW, 900)
    assert a.quality == "UNAVAILABLE" and not a.usable


# ---- analytics ---------------------------------------------------------------------------------------------

def test_values_weights_pnl_and_gap():
    v = view()
    s, p = v.snapshot, by_symbol(v)
    assert s.positions_market_value == D("990.00") and s.calculated_total_value == D("1000.00")
    assert s.cash_pct == D("1.00")
    assert [p[x].portfolio_weight for x in ("NVDA", "SNDK", "SHOP", "MU")] == [D("22.20"), D("45.00"), D("30.60"), D("1.20")]
    assert sum(x.portfolio_weight for x in v.positions) + s.cash_pct == D("100.00")
    assert (p["NVDA"].cost_basis_total, p["NVDA"].unrealized_pnl, p["NVDA"].unrealized_pnl_pct) == (D("200.00"), D("22.00"), D("11.00"))
    assert (p["SNDK"].unrealized_pnl, p["SNDK"].unrealized_pnl_pct) == (D("-50.00"), D("-10.00"))
    assert s.valuation_gap == D("5.00") and s.valuation_gap_pct == D("0.50") and s.valuation_gap_material
    assert s.provider_as_of == "UNAVAILABLE" and s.fetched_at == "2026-09-28T00:04:59+00:00"
    assert s.realized_pnl_window == D("5.69")
    assert v.positions[0].symbol == "SNDK"  # sorted by value


def test_missing_basis_is_unavailable_never_zero():
    v = view()
    mu, s = by_symbol(v)["MU"], v.snapshot
    assert mu.avg_cost is None and mu.cost_basis_total is None and mu.unrealized_pnl is None
    assert mu.basis_available is False and mu.market_value == D("12.00")
    assert s.total_cost_basis == D("1000.00") and s.total_unrealized_pnl == D("-22.00")
    assert s.total_unrealized_pnl_pct == D("-2.20") and s.unrealized_pnl_complete is False
    assert v.basis_summary == {"available": 3, "missing": 1, "missing_symbols": ["MU"]}


def test_unreliable_quote_leaves_position_unvalued_and_gap_undefined():
    pos = copy.deepcopy(POSITIONS)
    pos["quotes"]["SNDK"]["has_traded"] = False
    v = view(positions=pos)
    sndk, s = by_symbol(v)["SNDK"], v.snapshot
    assert sndk.market_value is None and sndk.portfolio_weight is None and sndk.unrealized_pnl is None
    assert s.valuation_complete is False and s.valuation_gap is None and s.positions_market_value == D("540.00")
    assert any("could not be valued" in m for m in v.messages)


def test_day_change_excludes_intraday_shares():
    p = by_symbol(view())
    assert p["SHOP"].day_change == D("0.00") and p["SHOP"].day_change_partial is True
    assert p["NVDA"].day_change == D("22.00") and p["NVDA"].day_change_partial is False


def test_sectors_use_existing_map_and_unclassified_is_explicit():
    v = view()
    sectors = {s["sector"]: s for s in v.sector_exposure}
    assert set(sectors) == {"Semiconductors", "UNCLASSIFIED"}
    assert sectors["UNCLASSIFIED"]["symbols"] == ["SNDK", "SHOP"] and sectors["UNCLASSIFIED"]["weight_pct"] == D("75.60")
    assert sectors["Semiconductors"]["weight_pct"] == D("23.40")
    for sym in ("SNDK", "SHOP", "CLS"):
        assert get_sector_for_symbol(sym) is None  # gap reported, not filled from memory


def test_event_join_and_unknown_on_failure():
    def events(sym):
        if sym == "SHOP":
            raise RuntimeError("provider down")
        level = {"NVDA": "HIGH", "SNDK": "MEDIUM", "MU": "NONE"}[sym]
        return SimpleNamespace(event_risk_level=level, nearest_event=None, nearest_event_proximity=None,
                               data_quality="MEDIUM")
    v = view(event_fn=events)
    levels = {e["level"]: e for e in v.event_exposure}
    assert levels["HIGH"]["symbols"] == ["NVDA"] and levels["HIGH"]["weight_pct"] == D("22.20")
    assert levels["UNKNOWN"]["symbols"] == ["SHOP"]
    assert by_symbol(v)["SHOP"].event_risk_level == "UNKNOWN"


def test_quote_quality_summary():
    s = view().quote_quality_summary
    assert s["worst"] == "DEGRADED" and s["counts"]["DEGRADED"] == 1 and s["counts"]["OK"] == 3
    assert s["issues"][0]["symbol"] == "MU"


def test_json_serialization_keeps_decimals_exact():
    out = to_jsonable(view().snapshot)
    assert out["positions_market_value"] == "990.00" and out["valuation_gap_pct"] == "0.50"


# ---- tax lots ------------------------------------------------------------------------------------------------

def test_tax_lots_basis_pending_and_holding_period():
    lots, qi = build_tax_lots(TAX_LOTS["tax_lots"], Quote.from_gateway(TAX_LOTS["quote"]), today=date(2026, 9, 27),
                              now=NOW, stale_after_s=900)
    a, b = lots
    assert a.basis_pending is False and a.market_value == D("111.00") and a.unrealized_pnl == D("21.00")
    assert a.days_held == 26 and a.days_to_long_term == (date(2027, 9, 2) - date(2026, 9, 27)).days
    assert b.basis_pending is True and b.cost_basis is None and b.unrealized_pnl is None and b.market_value == D("111.00")
    assert b.days_to_long_term == 0
    assert qi["quality"] == "OK"
    assert long_term_date(date(2024, 2, 29)) == date(2025, 3, 1)


# ---- risk facts -----------------------------------------------------------------------------------------------

def test_risk_facts_raw_with_no_default_thresholds():
    r = build_risk_facts(view(), "[]")
    assert r["rules_configured"] == 0 and r["rule_results"] == []
    pf = r["portfolio"]
    assert pf["cash_pct"] == D("1.00") and pf["largest_position_weight"] == D("45.00")
    assert pf["largest_sector_weight"] == D("75.60") and pf["valuation_gap_pct"] == D("0.50")
    f = {x["symbol"]: x for x in r["positions"]}
    assert f["MU"]["basis_available"] is False and f["MU"]["unrealized_pnl_pct"] is None
    assert f["NVDA"]["sector_weight"] == D("23.40")


def test_configured_rule_evaluates_and_unavailable_fact_is_not_assumed():
    rules = ('[{"id":"t1","scope":"position","fact":"position_weight","op":">=","value":40},'
             '{"id":"t2","scope":"position","fact":"unrealized_pnl_pct","op":"<","value":0},'
             '{"id":"bad","scope":"galaxy","fact":"x","op":">","value":1}]')
    r = build_risk_facts(view(), rules)
    t1 = {x["subject"]: x["triggered"] for x in r["rule_results"] if x["rule_id"] == "t1"}
    t2 = {x["subject"]: x["triggered"] for x in r["rule_results"] if x["rule_id"] == "t2"}
    assert t1 == {"SNDK": True, "SHOP": False, "NVDA": False, "MU": False}
    assert t2["MU"] is None and t2["SNDK"] is True
    assert r["rules_configured"] == 2 and r["rule_errors"]


def test_invalid_rules_json_applies_nothing():
    r = build_risk_facts(view(), "{not json")
    assert r["rules_configured"] == 0 and r["rule_errors"]


# ---- scenarios -------------------------------------------------------------------------------------------------

FORBIDDEN_KEYS = {"order", "order_type", "limit_price", "shares", "quantity", "side", "time_in_force", "account_number"}


def test_add_500_to_mu_from_cash():
    v = view()
    before = copy.deepcopy(v)
    r = run_scenario(v, {"type": "hypothetical_add", "symbol": "MU", "amount_usd": 500, "funding": "cash"},
                     get_sector_for_symbol)
    assert r["position_weight_before_pct"] == D("1.20") and r["position_weight_after_pct"] == D("51.20")
    assert r["sector_weight_before_pct"] == D("23.40") and r["sector_weight_after_pct"] == D("73.40")
    assert r["cash_after"] == D("-490.00") and r["insufficient_cash"] is True
    assert r["total_value_after"] == D("1000.00")
    assert not (FORBIDDEN_KEYS & set(r)) and "No order was created" in r["disclaimer"]
    assert to_jsonable(v) == to_jsonable(before)  # the view is never mutated


def test_add_new_money_to_unheld_symbol():
    r = run_scenario(view(), {"type": "hypothetical_add", "symbol": "NVDA", "amount_usd": "1000",
                              "funding": "new_money"}, get_sector_for_symbol)
    assert r["total_value_after"] == D("2000.00") and r["position_weight_after_pct"] == D("61.10")
    assert r["sector_weight_after_pct"] == D("61.70")
    r2 = run_scenario(view(), {"type": "hypothetical_add", "symbol": "AMD", "amount_usd": 100, "funding": "new_money"},
                      get_sector_for_symbol)
    assert r2["held_now"] is False and r2["sector"] == "Semiconductors"


def test_high_event_risk_fraction_and_cash_after_purchase():
    v = view(event_fn=lambda s: SimpleNamespace(event_risk_level="HIGH" if s in ("NVDA", "MU") else "LOW",
                                                nearest_event=None, nearest_event_proximity=None, data_quality="HIGH"))
    r = run_scenario(v, {"type": "event_risk_exposure", "level": "HIGH"}, get_sector_for_symbol)
    assert r["weight_pct"] == D("23.40") and r["symbols"] == ["NVDA", "MU"]
    c = run_scenario(v, {"type": "cash_after_purchase", "amount_usd": 5}, get_sector_for_symbol)
    assert c["cash_pct_before"] == D("1.00") and c["cash_pct_after"] == D("0.50") and c["insufficient_cash"] is False


@pytest.mark.parametrize("req", [{"type": "hypothetical_add", "symbol": "MU", "amount_usd": 0},
                                 {"type": "hypothetical_add", "symbol": "MU", "amount_usd": -5},
                                 {"type": "hypothetical_add", "symbol": "", "amount_usd": 5},
                                 {"type": "hypothetical_add", "symbol": "MU", "amount_usd": 5, "funding": "margin"},
                                 {"type": "place_order", "symbol": "MU", "amount_usd": 5},
                                 {"type": "event_risk_exposure", "level": "EXTREME"}])
def test_invalid_scenarios_rejected(req):
    with pytest.raises(ScenarioError):
        run_scenario(view(), req, get_sector_for_symbol)
