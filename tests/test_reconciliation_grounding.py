"""Stage 2.7E: Robinhood reconciliation + AI grounding bug reproduction/fix."""
import json
from decimal import Decimal

import pytest

from e_fixtures import RULES, view
from portfolio.explain import (allowed_numbers, build_facts, check_text, magnitude_terms, ungrounded_numbers)
from portfolio.models import RealizedPnlSummary
from portfolio.reconciliation import CONTRIBUTIONS_UNAVAILABLE, COST_BASIS_NOTE, build_reconciliation
from portfolio.risk_facts import build_risk_facts
from portfolio.scenarios import hypothetical_add

D = Decimal


# ---- reconciliation -------------------------------------------------------------------------------------

def test_reconciliation_keeps_concepts_separate():
    v = view()
    rec = build_reconciliation(v, RealizedPnlSummary.from_gateway({"span": "all", "total_returns": "50.12"}))
    assert rec["robinhood_portfolio_value"] == D("1005.00")        # Robinhood-reported
    assert rec["current_positions_value"] == D("990.00")           # calculated
    assert rec["open_position_cost_basis"] == D("1000.00")         # cost of CURRENT holdings only
    assert rec["unrealized_pnl"] == D("-22.00") and rec["unrealized_pnl_complete"] is False
    assert rec["realized_pnl"] == D("50.12") and rec["realized_pnl_status"] == "VERIFIED"
    assert rec["cash"] == D("10.00")


def test_cost_basis_is_never_labelled_as_money_added():
    rec = build_reconciliation(view(), None)
    assert rec["net_contributions"] is None and rec["net_contributions_status"] == "UNAVAILABLE"
    assert rec["net_contributions_message"] == CONTRIBUTIONS_UNAVAILABLE
    assert rec["total_investment_result"] is None and rec["total_investment_result_status"] == "UNAVAILABLE"
    assert rec["cost_basis_note"] == COST_BASIS_NOTE and "not necessarily the total amount" in COST_BASIS_NOTE
    assert "deposit" not in rec["glossary"]["cost_basis"].split("not your total deposits")[0].lower()


def test_missing_realized_is_unavailable_not_zero():
    rec = build_reconciliation(view(), None, "UNAVAILABLE (UPSTREAM_ERROR)")
    assert rec["realized_pnl"] is None and rec["realized_pnl_status"] == "UNAVAILABLE (UPSTREAM_ERROR)"


def test_realized_and_unrealized_are_distinct():
    rec = build_reconciliation(view(), RealizedPnlSummary.from_gateway({"span": "all", "total_returns": "5.69"}))
    assert rec["realized_pnl"] != rec["unrealized_pnl"]
    for k in ("unrealized_pnl", "realized_pnl", "cost_basis", "portfolio_value", "cash", "portfolio_weight"):
        assert rec["glossary"].get(k) if k != "cost_basis" else rec["glossary"]["cost_basis"]


# ---- grounding bug: reproduction + fix ----------------------------------------------------------------------

def _facts(scenario=None, derived=True):
    from portfolio.policy import policy_report
    v = view()
    facts = build_facts(v, build_risk_facts(v, "[]"), {}, scenario, policy_report(v, RULES, []))
    if not derived:
        facts.pop("derived")
    return json.dumps(facts, sort_keys=True, default=str)


def _scenario():
    return hypothetical_add(view(), "MU", 500, "new_money", sector="Semiconductors", rules=RULES)


def test_reproduction_change_in_percentage_points_was_blocked_without_derived_facts():
    """Root cause: Claude states the CHANGE (after - before) which was not in the facts."""
    s = _scenario()
    assert (s["position_weight_before_pct"], s["position_weight_after_pct"]) == (D("1.20"), D("34.13"))
    text = "MU's weight would rise by 32.93 percentage points."
    assert ungrounded_numbers(text, _facts(s, derived=False)) == ["32.93"]     # the old failure
    assert ungrounded_numbers(text, _facts(s)) == []                           # fixed: Python precomputed it


def test_derived_facts_are_python_computed():
    facts = json.loads(_facts(_scenario()))
    d = facts["derived"]["scenario"]
    assert d["position_weight_change_pp"] == "32.93" and d["sector_weight_change_pp"] == "25.53"
    assert d["position_value_change"] == "500.00" and d["cash_change"] == "0.00"


@pytest.mark.parametrize("text", ["Your account is worth about $8,209.", "about $8,210", "$8,209.50", "8209.5",
                                  "-$8,209.50", "−8,209.50"])
def test_legitimate_rounding_truncation_and_currency_formats(text):
    assert ungrounded_numbers(text, '{"v": "8209.50"}') == []


@pytest.mark.parametrize("text,bad", [("about $8,211", ["$8,211"]), ("45.5% of the portfolio", ["45.5%"]),
                                      ("a 17.5% jump", ["17.5%"])])
def test_invented_numbers_remain_blocked(text, bad):
    facts = _facts(_scenario())
    assert ungrounded_numbers(text, facts if "8,211" not in text else '{"v": "8209.50"}') == bad


def test_percentages_thresholds_negatives_and_before_after_are_grounded():
    facts = _facts(_scenario())
    text = ("SNDK is 45.00% of the portfolio (HIGH level 30%). Unrealized result -$22.00 (-2.20%). "
            "MU would go from 1.20% to 34.13% and Semiconductors from 23.40% to 48.93%.")
    assert ungrounded_numbers(text, facts) == []


@pytest.mark.parametrize("text", ["Your account is about $1.0k.", "SNDK is 2 times MU.", "a 3-fold increase", "8.2K"])
def test_abbreviations_and_multiples_are_blocked(text):
    assert magnitude_terms(text)
    assert check_text(text, _facts(_scenario()))


def test_allowed_set_includes_abs_and_rounding_variants():
    a = allowed_numbers('{"x": "-12.345"}')
    for v in ("12.345", "12.35", "12.34", "12.3", "12"):
        assert D(v).normalize() in a


@pytest.mark.parametrize("text,blocked", [
    ("A good process does not guarantee profit.", False),
    ("GOOD DECISION ≠ GUARANTEED PROFIT", False),
    ("There is no guarantee the price holds support.", False),
    ("Support isn't a guarantee.", False),
    ("This entry is guaranteed to work.", True),
    ("We guarantee a rebound.", True),
    ("It will rise after earnings.", True),
])
def test_negated_guarantees_allowed_affirmative_blocked(text, blocked):
    from portfolio.explain import forecast_language
    assert bool(forecast_language(text)) is blocked


def test_explanation_token_cap_is_applied_and_json_extraction_is_tolerant():
    import inspect
    from portfolio import explain as ex
    assert "max_tokens=EXPLANATION_MAX_TOKENS" in inspect.getsource(ex._run) and ex.EXPLANATION_MAX_TOKENS >= 2000
    assert ex._extract_json('Here you go:\n```json\n{"a": 1}\n```\nThanks') == '{"a": 1}'
