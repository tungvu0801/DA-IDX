"""Stage 2.7E TRADER REVIEW: process vs outcome, no hindsight leakage, no invented history, no scores/orders."""
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from e_fixtures import bars, snapshot, tm
from insights.trade_review import (EntryState, attach_research, outcomes_after, reconstruct, review_lot,
                                   review_process, snapshot_at_entry, trading_patterns)
from portfolio.models import to_jsonable

D = Decimal
PLAN_FULL = {"reason": "trend", "holding_period": "weeks", "invalidation": "close below support", "amount": 500}


def st(**kw):
    base = dict(source="RECONSTRUCTED", as_of="2026-09-10", entry_price=D("100"), trend="UPTREND", momentum="NORMAL",
                momentum_5d_pct=1.0, rsi=55.0, volume="NORMAL", support=98.0, resistance=110.0,
                location="NEAR_SUPPORT", research_view="BULLISH BIAS", research_source="SAVED_SNAPSHOT_AT_ENTRY",
                event_risk="LOW", event_source="SAVED_SNAPSHOT_AT_ENTRY", market="IMPROVING",
                market_source="RECONSTRUCTED")
    base.update(kw)
    return EntryState(**base)


# ---- decision process vs outcome -----------------------------------------------------------------------------

def test_losing_trade_can_have_a_well_supported_process():
    r = review_process(st(), plan=PLAN_FULL)
    assert r["process_state"] == "WELL-SUPPORTED PROCESS"
    assert {"research_bullish", "near_support_entry", "no_high_event", "plan_complete"} <= {f["code"] for f in r["supporting"]}


def test_profitable_trade_can_have_a_poor_process():
    # the review function takes no P&L at all: an extended entry near resistance is higher-risk regardless of outcome
    r = review_process(st(location="NEAR_RESISTANCE", extended=True, momentum_5d_pct=12.0, rsi=76.0))
    assert r["process_state"] == "HIGHER-RISK PROCESS"
    assert {"near_resistance_entry", "extended_entry"} <= set(r["major_risk_factors"])
    import inspect
    assert not {"pnl", "profit", "outcome", "return_pct"} & set(inspect.signature(review_process).parameters)


@pytest.mark.parametrize("kw,code", [({"location": "NEAR_SUPPORT"}, "near_support_entry"),
                                     ({"location": "NEAR_RESISTANCE"}, "near_resistance_entry"),
                                     ({"location": "MIDDLE_OF_RANGE", "extended": True}, "extended_entry")])
def test_entry_types(kw, code):
    r = review_process(st(**kw))
    assert code in {f["code"] for f in r["supporting"] + r["risks"]}


def test_high_event_risk_is_higher_risk_process():
    assert review_process(st(event_risk="HIGH"))["process_state"] == "HIGHER-RISK PROCESS"


def test_concentrated_vs_small_diversified_position():
    conc = review_process(st(location="MIDDLE_OF_RANGE"), portfolio_fit={"creates_or_worsens_high": True,
                                                                          "text": "Semis would rise to 74%."})
    assert conc["process_state"] == "MIXED PROCESS" and "concentration" in conc["major_risk_factors"]
    both = review_process(st(location="NEAR_RESISTANCE"), portfolio_fit={"creates_or_worsens_high": True})
    assert both["process_state"] == "HIGHER-RISK PROCESS"
    small = review_process(st(), plan=PLAN_FULL, portfolio_fit={"small_position": True, "creates_or_worsens_high": False})
    assert "small_position" in {f["code"] for f in small["supporting"]}
    assert small["process_state"] == "WELL-SUPPORTED PROCESS"


def test_missing_entry_information_is_insufficient():
    r = review_process(EntryState("UNAVAILABLE", None, None))
    assert r["process_state"] == "INSUFFICIENT INFORMATION" and r["available_areas"] < 3
    assert r["areas"]["plan"]["status"] == "UNAVAILABLE" and r["areas"]["portfolio"]["status"] == "UNAVAILABLE"


def test_missing_plan_items_are_not_invented():
    r = review_process(st(), plan={"reason": None, "holding_period": None, "invalidation": None, "amount": 500})
    assert r["areas"]["plan"]["items"] == {"reason": False, "holding_period": False, "invalidation": False, "amount": True}
    assert {"no_invalidation", "no_holding_period"} <= {f["code"] for f in r["risks"]}
    assert r["process_state"] != "WELL-SUPPORTED PROCESS"


# ---- history: snapshots, reconstruction, outcomes ---------------------------------------------------------------

def test_missing_historical_research_snapshot_is_unavailable():
    s = attach_research(st(research_view=None, research_source="UNAVAILABLE"), snapshot=None)
    assert s.research_view is None and any("No research was saved" in n for n in s.notes)


def test_snapshot_at_entry_never_uses_later_or_too_old_snapshots():
    entry = date(2026, 9, 10)
    before = snapshot(datetime(2026, 9, 9, 15, tzinfo=timezone.utc), sid=1)
    after = snapshot(datetime(2026, 9, 11, 15, tzinfo=timezone.utc), "BEARISH BIAS", sid=2)
    old = snapshot(datetime(2026, 8, 1, 15, tzinfo=timezone.utc), sid=3)
    assert snapshot_at_entry([before, after, old], entry) is before
    assert snapshot_at_entry([after, old], entry) is None


def test_no_hindsight_leakage_in_reconstruction():
    b = bars(end=date(2026, 9, 25))
    entry = date(2026, 9, 10)
    s1 = reconstruct("MU", b, entry, D("150"))
    future = b.copy()
    mask = future["timestamp"].dt.date >= entry
    future.loc[mask, ["open", "high", "low", "close"]] *= 3            # rewrite everything from the entry onward
    s2 = reconstruct("MU", future, entry, D("150"))
    assert to_jsonable(s1) == to_jsonable(s2)
    assert s1.as_of < entry.isoformat() and s1.source == "RECONSTRUCTED"


def test_stage_2_5_outcome_evaluation_is_reused():
    b = bars(end=date(2026, 9, 25))
    entry = date(2026, 9, 10)
    price = D("150")
    s = reconstruct("MU", b, entry, price)
    out = {o["horizon_trading_days"]: o for o in outcomes_after(b, entry, price, s)}
    after = b[b["timestamp"].dt.date > entry].reset_index(drop=True)
    exp3 = (float(after["close"].iloc[2]) - 150) / 150 * 100
    assert out[3]["status"] == "COMPLETED" and out[3]["return_pct"] == round(exp3, 2)
    assert out[5]["mfe_pct"] == round(max(0.0, (float(after["high"].iloc[:5].max()) - 150) / 150 * 100), 2)
    assert out[5]["mae_pct"] == round(min(0.0, (float(after["low"].iloc[:5].min()) - 150) / 150 * 100), 2)
    pend = outcomes_after(b, date(2026, 9, 24), price, s)
    assert [o["status"] for o in pend] == ["COMPLETED", "PENDING", "PENDING"]


def test_review_lot_separates_process_from_hindsight():
    b, spy = bars(end=date(2026, 9, 25)), bars(start=400, end=date(2026, 9, 25))
    lot = {"lot_id": "x", "open_date": "2026-09-10", "cost_per_share": "150.00", "open_type": "buy", "quantity": "1"}
    snap = snapshot(datetime(2026, 9, 9, tzinfo=timezone.utc), event_level="MEDIUM")
    r = review_lot("MU", lot, b, spy, [snap])
    assert r["entry_state"].research_source == "SAVED_SNAPSHOT_AT_ENTRY" and r["entry_state"].event_risk == "MEDIUM"
    assert r["outcomes"] and "does not by itself mean" in r["hindsight_note"]
    missing = review_lot("MU", {"lot_id": "y", "open_date": None, "cost_per_share": None}, b, spy, [])
    assert missing["review"]["process_state"] == "INSUFFICIENT INFORMATION"


def test_trading_patterns_need_enough_data_and_only_count_facts():
    b, spy = bars(end=date(2026, 9, 25)), bars(start=400, end=date(2026, 9, 25))
    few = [review_lot("MU", {"open_date": d, "cost_per_share": "150"}, b, spy, []) for d in ("2026-09-10", "2026-09-11")]
    assert trading_patterns(few)["available"] is False
    many = [review_lot("MU", {"open_date": f"2026-09-{d:02d}", "cost_per_share": "150"}, b, spy, [])
            for d in (8, 9, 10, 11, 14, 15)]
    p = trading_patterns(many)
    assert p["available"] and p["sample"] == 6
    for item in p["patterns"]:
        assert f"of the last {p['sample']}" in item["text"] or "entries with a completed" in item["text"]
    text = json.dumps(to_jsonable(p)).lower()
    assert "emotional" not in text and "you are" not in text


def test_no_scores_grades_probabilities_or_orders_in_reviews():
    for r in (review_process(st(), plan=PLAN_FULL), review_process(st(location="NEAR_RESISTANCE", extended=True))):
        text = json.dumps(to_jsonable(r))
        for bad in ("/100", "A+", "score", "probab", "% chance", "should have bought", "should have sold", "Buy now",
                    "Sell now"):
            assert bad not in text, bad
        assert not ({"order", "order_type", "quantity", "limit_price"} & set(r))


# ---- routes (fake gateway; no network; only allowlisted GET reads; nothing written) -----------------------------

@pytest.fixture
def client(monkeypatch):
    from api.routes import portfolio as pr
    from api.server import app
    from insights import service
    from insights.market import MarketInputs
    from pf_fixtures import FakeGatewayHttp, fake_provider
    http = FakeGatewayHttp()
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    monkeypatch.setattr(pr, "event_lookup", lambda s: SimpleNamespace(event_risk_level="LOW", nearest_event=None,
                        nearest_event_proximity=None, data_quality="MEDIUM", company_events=[], macro_events=[],
                        earnings_available=False))
    monkeypatch.setattr(service, "stock_data_fn", lambda s: (tm(s, price=110.0, support=105.0, resistance=130.0), None))
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: MarketInputs({}, [], 80, None, {}, [], [], now))
    monkeypatch.setattr(service, "history_fn", lambda syms, lookback: {s: bars(end=date(2026, 9, 25)) for s in syms})
    service._cache.clear()
    c = TestClient(app)
    c.http = http
    return c


def test_before_route(client):
    body = client.post("/api/trade-review/before", json={"symbol": "NVDA", "amount_usd": 100, "entry_price": 225,
                                                         "holding_period": "weeks", "invalidation": "below support"}).json()
    assert body["mode"] == "BEFORE" and body["review"]["process_state"] in (
        "WELL-SUPPORTED PROCESS", "MIXED PROCESS", "HIGHER-RISK PROCESS", "INSUFFICIENT INFORMATION")
    assert body["review"]["areas"]["plan"]["items"]["reason"] is False           # not invented
    assert client.post("/api/trade-review/before", json={"symbol": "NVDA", "amount_usd": 1, "place_order": True}).status_code == 422


def test_position_route_marks_unsaved_history_unavailable(client):
    body = client.get("/api/trade-review/position/NVDA").json()
    assert body["mode"] == "AFTER" and body["lots"]
    changes = {c["item"]: c for c in body["changes_since_entry"]}
    assert changes["Portfolio weight"]["at_entry"] is None
    assert changes["Portfolio weight"]["at_entry_source"] == "not saved / unavailable"
    assert changes["Research view"]["at_entry"] is None or changes["Research view"]["at_entry_source"] == "SAVED_SNAPSHOT_AT_ENTRY"
    assert client.get("/api/trade-review/position/ZZZZ").json()["status"] == "NOT_HELD"


def test_patterns_route_and_only_allowlisted_reads(client):
    body = client.get("/api/trade-review/patterns").json()
    assert body["status"] == "OK" and body["available"] is False     # 1 buy order in the fixture < 5
    paths = {r["path"] for r in client.http.requests}
    assert paths <= {"/portfolio", "/positions", "/realized-pnl", "/tax-lots/NVDA", "/orders", "/accounts",
                     "/pnl-history"}
    for r in client.http.requests:
        assert set(r["headers"]) == {"X-RH-Gateway-Secret"}


def test_checklist_is_not_saved(client):
    body = client.get("/api/trade-review/checklist").json()
    assert body["saved"] is False and len(body["checklist"]) == 11


def test_position_review_has_no_partial_now_process_label(client):
    body = client.get("/api/trade-review/position/NVDA").json()
    assert "now_review" not in body and "now_process_state" not in json.dumps(body)
    loc = next(c for c in body["changes_since_entry"] if c["item"] == "Price location")
    assert loc["now"] is None or not str(loc["now"]).isupper()        # plain words, not codes
