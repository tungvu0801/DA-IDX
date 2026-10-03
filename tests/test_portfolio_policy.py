"""Stage 2.7D portfolio attention policy: rules, boundaries, metrics, cards, scenarios (pure; no network)."""
import copy
from decimal import Decimal
from types import SimpleNamespace

import pytest

import config
from pf_fixtures import NOW, PORTFOLIO, POSITIONS, REALIZED
from portfolio.analytics import build_view
from portfolio.models import RealizedPnlSummary, to_jsonable
from portfolio.policy import (Holding, PolicyInputs, build_cards, build_rules, compute_metrics, effective_flags,
                              evaluate, parse_bands, policy_report)
from portfolio.scenarios import ScenarioError, run_scenario
from portfolio.sectors import CuratedSectorMapSource, SectorResolver

D = Decimal
RESOLVER = SectorResolver()


def cfg(**overrides):
    base = {k: getattr(config, k) for k in dir(config) if k.startswith("PORTFOLIO_POLICY_")}
    base.update(overrides)
    return SimpleNamespace(**base)


RULES, ERRORS = build_rules(cfg())


def meta():
    return {"status": "OK", "fetched_at": "2026-09-28T00:04:59+00:00", "cache_age_s": 1, "message": None}


def fixture_view(positions=POSITIONS, portfolio=PORTFOLIO, events=None):
    return build_view(copy.deepcopy(portfolio), meta(), copy.deepcopy(positions), meta(),
                      RealizedPnlSummary.from_gateway(REALIZED), now=NOW, stale_after_s=900, gap_material_pct=0.5,
                      sector_fn=RESOLVER.sector_for, event_fn=events, classify_fn=RESOLVER.classify)


def inputs(*holdings, cash="0", gap=None):
    return PolicyInputs(tuple(holdings), None if cash is None else D(cash), D("0"),
                        None if gap is None else D(gap))


def H(sym, mv, sector="Semiconductors", event="NONE", quality="OK", basis=True, ref=None):
    mv = None if mv is None else D(mv)
    return Holding(sym, mv, D(ref) if ref is not None else mv, sector, event, quality, basis)


def flags_for(inp, rules=RULES):
    return {(f["family"], f["subject"]): f["severity"] for f in effective_flags(evaluate(rules, compute_metrics(inp)))}


# ---- configuration --------------------------------------------------------------------------------------

def test_default_bands_match_documented_proposal():
    by_id = {r.id: (r.operator, r.threshold) for r in RULES}
    assert by_id["position_concentration.info"] == (">=", D("10"))
    assert by_id["position_concentration.medium"] == (">=", D("20"))
    assert by_id["position_concentration.high"] == (">=", D("30"))
    assert by_id["sector_concentration.high"] == (">=", D("60"))
    assert by_id["low_cash.info"] == ("<", D("10")) and by_id["low_cash.high"] == ("<", D("2"))
    assert by_id["event_exposure.medium"] == (">=", D("25"))
    assert by_id["valuation_gap.info"] == (">=", D("0.25")) and by_id["valuation_gap.high"] == (">=", D("1.00"))
    assert "unreliable_quote_value.info" not in by_id and by_id["unreliable_quote_value.medium"] == (">=", D("10"))
    assert by_id["missing_basis_value.high"] == (">=", D("25"))
    assert ERRORS == []


def test_bands_are_configuration_not_code():
    rules, _ = build_rules(cfg(PORTFOLIO_POLICY_POSITION_WEIGHT_PCT="5,15,50"))
    assert [r.threshold for r in rules if r.family == "position_concentration"] == [D("5"), D("15"), D("50")]


def test_band_parsing_empty_disables_and_invalid_is_reported():
    assert parse_bands("10,,30") == ([D("10"), None, D("30")], [])
    bands, errs = parse_bands("10,abc,-5")
    assert bands == [D("10"), None, None] and len(errs) == 2
    rules, errors = build_rules(cfg(PORTFOLIO_POLICY_SECTOR_WEIGHT_PCT="x,40,60"))
    assert "sector_concentration.info" not in {r.id for r in rules} and errors


def test_disabled_rule_is_not_evaluated_and_listed_inactive():
    rules, errors = build_rules(cfg(PORTFOLIO_POLICY_DISABLED_RULES="position_concentration.high,nope.rule"))
    high = next(r for r in rules if r.id == "position_concentration.high")
    assert high.enabled is False and any("nope.rule" in e for e in errors)
    f = flags_for(inputs(H("A", "35"), cash="65"), rules)
    assert f[("position_concentration", "A")] == "MEDIUM"  # HIGH band off -> next highest


def test_templates_never_use_trading_instructions():
    import re
    for r in RULES:
        assert not re.search(r"(?i)\b(sell|buy|reduce|trim|rebalanc\w*|buy less)\b", r.explanation_template), r.id


# ---- boundaries (>= and <) --------------------------------------------------------------------------------

@pytest.mark.parametrize("mv,cash,expected", [("19.99", "80.01", "INFO"), ("20", "80", "MEDIUM"),
                                              ("20.01", "79.99", "MEDIUM"), ("9.99", "90.01", None),
                                              ("10", "90", "INFO"), ("30", "70", "HIGH")])
def test_position_weight_boundaries(mv, cash, expected):
    f = flags_for(inputs(H("A", mv, sector="UNCLASSIFIED"), cash=cash))
    assert f.get(("position_concentration", "A")) == expected


@pytest.mark.parametrize("cash,expected", [("2.01", "MEDIUM"), ("2", "MEDIUM"), ("1.99", "HIGH"), ("5", "INFO"),
                                           ("4.99", "MEDIUM"), ("10", None), ("9.99", "INFO"), ("0", "HIGH")])
def test_low_cash_boundaries_strictly_below(cash, expected):
    other = D("100") - D(cash)
    f = flags_for(inputs(H("A", str(other), sector="UNCLASSIFIED"), cash=cash))
    assert f.get(("low_cash", "portfolio")) == expected


def test_sector_weight_boundary_and_unclassified_excluded():
    f = flags_for(inputs(H("A", "40"), H("B", "20", sector="Software"), H("U", "35", sector="UNCLASSIFIED"),
                         cash="5"))
    assert f[("sector_concentration", "Semiconductors")] == "MEDIUM"          # exactly 40 -> MEDIUM
    assert ("sector_concentration", "UNCLASSIFIED") not in f                   # never scored as a sector
    assert f[("sector_coverage.unclassified", "portfolio")] == "INFO"


def test_valuation_gap_boundaries_with_exact_decimals():
    for gap, expected in (("0.24", None), ("0.25", "INFO"), ("0.49", "INFO"), ("0.50", "MEDIUM"), ("1.00", "HIGH")):
        assert flags_for(inputs(H("A", "90", sector="UNCLASSIFIED"), cash="10", gap=gap)).get(
            ("valuation_gap", "portfolio")) == expected


def test_event_exposure_bands():
    f = flags_for(inputs(H("A", "25", event="HIGH", sector="UNCLASSIFIED"), H("B", "70", sector="UNCLASSIFIED"),
                         cash="5"))
    assert f[("event_exposure", "portfolio")] == "MEDIUM"
    f2 = flags_for(inputs(H("A", "50", event="UNKNOWN", sector="UNCLASSIFIED"), cash="50"))
    assert ("event_exposure", "portfolio") not in f2 and f2[("event_exposure.unknown_coverage", "portfolio")] == "INFO"


# ---- overlap, missing metrics, zero values ------------------------------------------------------------------

def test_overlapping_bands_report_highest_severity_but_keep_all_results():
    inp = inputs(H("A", "45", sector="UNCLASSIFIED"), cash="55")
    results = [r for r in evaluate(RULES, compute_metrics(inp)) if r["family"] == "position_concentration"]
    assert [r["triggered"] for r in results] == [True, True, True]
    assert flags_for(inp)[("position_concentration", "A")] == "HIGH"


def test_zero_portfolio_value_evaluates_nothing():
    m = compute_metrics(inputs(H("A", "0", sector="UNCLASSIFIED"), cash="0"))
    assert m["total_value"] is None and m["portfolio"]["portfolio"]["cash_pct"] is None
    results = evaluate(RULES, m)
    assert all(r["triggered"] is None for r in results if r["unit"] == "%")
    cards = {c["card"]: c["status"] for c in build_cards(RULES, results, m)}
    assert cards["cash_level"] == "UNAVAILABLE" and cards["position_concentration"] == "UNAVAILABLE"


def test_missing_cash_is_not_evaluated():
    m = compute_metrics(inputs(H("A", "50"), cash=None))
    assert m["total_value"] is None
    assert all(r["triggered"] is None for r in evaluate(RULES, m) if r["family"] == "low_cash")


def test_missing_quote_value_uses_reference_value_for_data_quality_only():
    inp = inputs(H("A", "80", sector="UNCLASSIFIED"), H("B", None, quality="UNRELIABLE", ref="20"), cash="0.00001")
    m = compute_metrics(inp)
    assert m["position"]["B"]["position_weight"] is None                     # never valued for weights
    assert m["portfolio"]["portfolio"]["unreliable_quote_value_pct"] == D("20.00")
    f = flags_for(inp)
    assert f[("unreliable_quote_value", "portfolio")] == "MEDIUM" and f[("quote_quality.any_issue", "portfolio")] == "INFO"


def test_unknown_reference_value_makes_value_share_unavailable():
    inp = inputs(H("A", "80"), H("B", None, quality="UNAVAILABLE", ref=None), cash="20")
    assert compute_metrics(inp)["portfolio"]["portfolio"]["unreliable_quote_value_pct"] is None
    assert flags_for(inp)[("quote_quality.any_issue", "portfolio")] == "INFO"


def test_missing_cost_basis_count_and_value_bands():
    f = flags_for(inputs(H("A", "70", sector="UNCLASSIFIED"), H("B", "30", basis=False, sector="UNCLASSIFIED"),
                         cash="0"))
    assert f[("missing_basis.any", "portfolio")] == "INFO" and f[("missing_basis_value", "portfolio")] == "HIGH"


@pytest.mark.parametrize("quality,family", [("STALE", "quote_quality.any_stale"), ("DEGRADED", "quote_quality.any_issue"),
                                            ("UNRELIABLE", "quote_quality.any_issue")])
def test_stale_degraded_unreliable_quotes(quality, family):
    mv = None if quality == "UNRELIABLE" else "50"
    f = flags_for(inputs(H("A", mv, quality=quality, ref="50", sector="UNCLASSIFIED"), H("B", "50",
                                                                                         sector="UNCLASSIFIED"),
                         cash="50"))
    assert f[(family, "portfolio")] == "INFO"


# ---- report on the Stage 2.7C fixture portfolio ------------------------------------------------------------------

def test_fixture_portfolio_report():
    rep = policy_report(fixture_view(), RULES, [])
    flags = {(f["family"], f["subject"]): f["severity"] for f in rep["flags"]}
    assert flags[("position_concentration", "SNDK")] == "HIGH" and flags[("position_concentration", "SHOP")] == "HIGH"
    assert flags[("position_concentration", "NVDA")] == "MEDIUM" and ("position_concentration", "MU") not in flags
    assert flags[("low_cash", "portfolio")] == "HIGH" and flags[("valuation_gap", "portfolio")] == "MEDIUM"
    assert ("sector_concentration", "Semiconductors") not in flags          # 23.40% < 25%
    assert flags[("sector_coverage.unclassified", "portfolio")] == "INFO"
    assert flags[("quote_quality.any_issue", "portfolio")] == "INFO"          # MU crossed quote
    assert flags[("missing_basis.any", "portfolio")] == "INFO"
    cards = {c["card"]: c for c in rep["cards"]}
    assert cards["position_concentration"]["status"] == "HIGH"
    assert cards["position_concentration"]["metric_subject"] == "SNDK"
    assert cards["position_concentration"]["metric_value"] == D("45.00")
    assert cards["sector_concentration"]["status"] == "OK" and cards["cash_level"]["status"] == "HIGH"
    assert cards["data_quality"]["status"] == "MEDIUM"
    assert "review before adding more exposure" in cards["position_concentration"]["explanation"]
    assert rep["separation_note"] == "PORTFOLIO POLICY does not modify RESEARCH EVIDENCE."
    assert to_jsonable(rep)  # serializable


def test_sector_resolver_is_verified_only():
    r = SectorResolver([CuratedSectorMapSource()])
    for sym in ("SNDK", "SHOP", "CLS"):
        c = r.classify(sym)
        assert c.sector == "UNCLASSIFIED" and c.source is None and c.verified is False
    assert r.classify("nvda").sector == "Semiconductors" and r.classify("NVDA").source == "project_curated_sector_map"
    v = fixture_view()
    assert {p.symbol: p.sector_source for p in v.positions}["SNDK"] is None


# ---- scenario policy impact -----------------------------------------------------------------------------------

FORBIDDEN_KEYS = {"order", "order_type", "limit_price", "shares", "quantity", "side", "time_in_force", "account_number"}


def scenario(req, view=None):
    return run_scenario(view or fixture_view(), req, RESOLVER.sector_for, rules=RULES, event_level_fn=lambda s: "LOW")


def _impact(result, family, subject):
    for bucket, items in result["policy_impact"].items():
        if isinstance(items, list):
            for i in items:
                if i["family"] == family and i["subject"] == subject:
                    return bucket, i
    return None, None


def test_cash_funded_add_worsens_concentration():
    r = scenario({"type": "hypothetical_add", "symbol": "MU", "amount_usd": 500, "funding": "cash"})
    bucket, item = _impact(r, "position_concentration", "MU")
    assert bucket == "newly_triggered" and item["after_severity"] == "HIGH" and item["after_value"] == D("51.20")
    bucket, item = _impact(r, "sector_concentration", "Semiconductors")
    assert bucket == "newly_triggered" and item["after_value"] == D("73.40") and item["after_severity"] == "HIGH"
    assert r["cash_after"] == D("-490.00") and "existing cash" in r["funding_explanation"]
    assert not (FORBIDDEN_KEYS & set(r))


def test_new_money_add_improves_other_concentration():
    r = scenario({"type": "hypothetical_add", "symbol": "NVDA", "amount_usd": 1000, "funding": "new_money"})
    bucket, item = _impact(r, "position_concentration", "SNDK")
    assert bucket == "de_escalated" and (item["before_severity"], item["after_severity"]) == ("HIGH", "MEDIUM")
    assert item["before_value"] == D("45.00") and item["after_value"] == D("22.50")
    bucket, _ = _impact(r, "low_cash", "portfolio")
    assert bucket == "unchanged" and "new money" in r["funding_explanation"]
    assert r["cash_before"] == r["cash_after"] == D("10.00")


def test_unclassified_symbol_flags_sector_uncertainty():
    r = scenario({"type": "hypothetical_add", "symbol": "SHOP", "amount_usd": 100, "funding": "new_money"})
    assert r["sector_classification_uncertain"] is True and "no verified sector" in r["sector_note"]
    r2 = scenario({"type": "hypothetical_add", "symbol": "ZZZZ", "amount_usd": 100, "funding": "new_money"})
    assert r2["held_now"] is False and r2["sector_classification_uncertain"] is True


def test_cash_after_purchase_policy_impact_is_low_cash_only():
    r = scenario({"type": "cash_after_purchase", "amount_usd": 5})
    families = {i["family"] for b in r["policy_impact"].values() if isinstance(b, list) for i in b}
    assert families == {"low_cash"}


def test_scenario_does_not_mutate_view_or_call_anything(monkeypatch):
    v = fixture_view()
    before = to_jsonable(v)
    import requests

    def boom(*a, **k):
        raise AssertionError("scenario must not make HTTP calls")
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests, "post", boom)
    scenario({"type": "hypothetical_add", "symbol": "MU", "amount_usd": 500, "funding": "cash"}, view=v)
    assert to_jsonable(v) == before


def test_scenario_on_unvalued_holding_is_rejected():
    pos = copy.deepcopy(POSITIONS)
    pos["quotes"]["MU"]["has_traded"] = False
    with pytest.raises(ScenarioError):
        scenario({"type": "hypothetical_add", "symbol": "MU", "amount_usd": 10}, view=fixture_view(positions=pos))
