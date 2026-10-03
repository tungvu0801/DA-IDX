"""Stage 2.7E regression: new endpoints never change research output, Stage 2.6 events, policy arithmetic or the DB."""
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from e_fixtures import bars, tm
from pf_fixtures import FakeGatewayHttp, fake_provider
from test_regression_isolation import db_digest, research_stubs  # noqa: F401  (fixture reuse)


@pytest.fixture
def client(monkeypatch):
    from api.routes import portfolio as pr
    from api.server import app
    from insights import service
    from insights.market import MarketInputs
    from portfolio import explain as ex
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(FakeGatewayHttp()))
    monkeypatch.setattr(pr, "event_lookup", lambda s: SimpleNamespace(event_risk_level="LOW", nearest_event=None,
                        nearest_event_proximity=None, data_quality="MEDIUM", company_events=[], macro_events=[],
                        earnings_available=False))
    monkeypatch.setattr(service, "stock_data_fn", lambda s: (tm(s), None))
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: MarketInputs(
        {"SPY": tm("SPY", price=500.0), "QQQ": tm("QQQ", price=400.0), "SOXX": tm("SOXX", price=200.0)},
        [], 80, None, {"Semiconductors": 1.0}, [], [], now))
    monkeypatch.setattr(service, "history_fn", lambda syms, lookback: {s: bars(end=date(2026, 9, 25)) for s in syms})
    monkeypatch.setattr(ex.explain, "__kwdefaults__", {**(ex.explain.__kwdefaults__ or {})})
    monkeypatch.setattr(ex.explain, "__defaults__", (None, lambda: None))
    monkeypatch.setattr(ex.explain_market, "__defaults__", (lambda: None,))
    monkeypatch.setattr(ex.explain_trade_review, "__defaults__", (lambda: None,))
    service._cache.clear()
    return TestClient(app)


def exercise_27e(client):
    client.get("/api/portfolio")
    client.post("/api/portfolio/add-money-check", json={"symbol": "MU", "amount_usd": 500})
    client.post("/api/portfolio/add-money-check", json={"symbol": "MU", "amount_usd": 500, "funding": "cash"})
    client.post("/api/portfolio/explain", json={"check": {"symbol": "MU", "amount_usd": 500}})
    client.get("/api/insights/market")
    client.get("/api/insights/market?refresh=true")
    client.post("/api/insights/market/explain")
    client.post("/api/trade-review/before", json={"symbol": "MU", "amount_usd": 100})
    client.get("/api/trade-review/position/NVDA")
    client.get("/api/trade-review/patterns")
    client.post("/api/trade-review/explain", json={"position": "NVDA"})


def test_research_byte_identical_and_db_untouched_with_all_27e_features(client, research_stubs, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", False)
    before_db = db_digest()
    off = client.get("/api/stocks/NVDA/research")
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(config, "PORTFOLIO_POLICY_ENABLED", True)
    exercise_27e(client)
    on = client.get("/api/stocks/NVDA/research")
    assert on.content == off.content                       # evidence, Research View, events: byte-identical
    assert on.json()["events"]["event_risk_level"] == off.json()["events"]["event_risk_level"]
    assert db_digest() == before_db                          # no DB writes, no migration


def test_add_money_route_and_market_route(client, monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    c = client.post("/api/portfolio/add-money-check", json={"symbol": "MU", "amount_usd": 500}).json()["check"]
    assert c["funding"] == "new_money" and c["fit"]["state"] in ("FAVORABLE CONDITIONS", "MIXED CONDITIONS",
                                                                 "CAUTION CONDITIONS")
    assert client.post("/api/portfolio/add-money-check", json={"symbol": "MU", "amount_usd": 5,
                                                               "order_type": "market"}).status_code == 422
    m = client.get("/api/insights/market").json()
    assert m["available"] is True and m["stale"] is False and m["environment"] in ("SUPPORTIVE", "MIXED", "CAUTIOUS")


def test_policy_arithmetic_unchanged_by_27e():
    """Same inputs as Stage 2.7D fixture -> identical policy numbers (2.7E only added fields, not arithmetic)."""
    from e_fixtures import RULES, view
    from portfolio.policy import policy_report
    rep = policy_report(view(), RULES, [])
    flags = {(f["family"], f["subject"]): (f["severity"], str(f["value"])) for f in rep["flags"]}
    assert flags[("position_concentration", "SNDK")] == ("HIGH", "45.00")
    assert flags[("low_cash", "portfolio")] == ("HIGH", "1.00")
    assert flags[("valuation_gap", "portfolio")] == ("MEDIUM", "0.50")
