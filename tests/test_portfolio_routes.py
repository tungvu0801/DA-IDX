"""FastAPI portfolio routes with a fake gateway and fake LLM. No network, no DB writes."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from api.routes import portfolio as routes
from pf_fixtures import FakeGatewayHttp, fake_provider

ENDPOINTS = [("get", "/api/portfolio", None), ("get", "/api/portfolio/tax-lots/NVDA", None),
             ("get", "/api/portfolio/realized-pnl", None), ("get", "/api/portfolio/orders", None),
             ("post", "/api/portfolio/scenario", {"type": "cash_after_purchase", "amount_usd": 5}),
             ("post", "/api/portfolio/explain", {})]


@pytest.fixture
def client():
    from api.server import app
    return TestClient(app)


@pytest.fixture
def enabled(monkeypatch):
    http = FakeGatewayHttp()
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(routes, "provider_factory", lambda: fake_provider(http))
    monkeypatch.setattr(routes, "event_lookup", lambda s: SimpleNamespace(
        event_risk_level="HIGH" if s == "NVDA" else "NONE", nearest_event=None, nearest_event_proximity=None,
        data_quality="MEDIUM"))
    return http


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_disabled_reports_unavailable_everywhere(client, monkeypatch, method, path, body):
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", False)
    r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
    assert r.status_code == 200
    assert r.json()["status"] == "PORTFOLIO_UNAVAILABLE" and r.json()["reason"] == "DISABLED"


def test_gateway_down_reports_unavailable(client, monkeypatch):
    import requests

    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError()
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(routes, "provider_factory", lambda: fake_provider(Down()))
    body = client.get("/api/portfolio").json()
    assert body["status"] == "PORTFOLIO_UNAVAILABLE" and body["reason"] == "GATEWAY_DOWN"


def test_portfolio_view(client, enabled):
    body = client.get("/api/portfolio").json()
    assert body["status"] == "OK"
    s = body["snapshot"]
    assert s["positions_market_value"] == "990.00" and s["valuation_gap"] == "5.00"
    assert s["account_masked_id"] == "••••3450" and s["provider_as_of"] == "UNAVAILABLE"
    syms = {p["symbol"]: p for p in body["positions"]}
    assert syms["MU"]["cost_basis_total"] is None and syms["MU"]["basis_available"] is False
    assert syms["SNDK"]["sector"] == "UNCLASSIFIED" and syms["NVDA"]["event_risk_level"] == "HIGH"
    assert body["risk_facts"]["rules_configured"] == 0
    assert isinstance(s["cash"], str)  # Decimal strings, never floats
    assert [r["path"] for r in enabled.requests] == ["/portfolio", "/positions", "/realized-pnl", "/realized-pnl"]
    assert enabled.requests[-1]["params"]["span"] == "all"          # reconciliation: realized P&L, all time
    rec = body["reconciliation"]
    assert rec["open_position_cost_basis"] == "1000.00" and rec["robinhood_portfolio_value"] == "1005.00"
    assert rec["net_contributions"] is None and rec["net_contributions_status"] == "UNAVAILABLE"


def test_tax_lots_realized_orders(client, enabled):
    lots = client.get("/api/portfolio/tax-lots/nvda").json()
    assert lots["status"] == "OK" and lots["tax_lots"][1]["basis_pending"] is True
    pnl = client.get("/api/portfolio/realized-pnl?span=3month").json()
    assert pnl["realized_pnl"]["total_returns"] == "5.69" and pnl["realized_pnl"]["trades"][0]["symbol"] == "LLY"
    orders = client.get("/api/portfolio/orders?since=2026-09-01").json()
    assert orders["orders"][0]["state"] == "filled"
    assert client.get("/api/portfolio/tax-lots/n;rm").json()["status"] == "PORTFOLIO_UNAVAILABLE"
    assert client.get("/api/portfolio/realized-pnl?span=forever").json()["status"] == "PORTFOLIO_UNAVAILABLE"


def test_scenario_endpoint(client, enabled):
    r = client.post("/api/portfolio/scenario", json={"type": "hypothetical_add", "symbol": "MU", "amount_usd": 500})
    res = r.json()["result"]
    assert res["position_weight_after_pct"] == "51.20" and "No order" in res["disclaimer"]
    bad = client.post("/api/portfolio/scenario", json={"type": "hypothetical_add", "symbol": "MU", "amount_usd": -1})
    assert bad.json()["status"] == "SCENARIO_INVALID"
    extra = client.post("/api/portfolio/scenario", json={"type": "hypothetical_add", "symbol": "MU",
                                                         "amount_usd": 5, "place_order": True})
    assert extra.status_code == 422
    assert client.post("/api/portfolio/scenario", json={"type": "place_order"}).status_code == 422


class FakeLLM:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    def analyze(self, prompt, system_prompt, max_tokens=1400):
        self.prompts.append((prompt, system_prompt))
        return SimpleNamespace(text=self.text, model="fake", input_tokens=1, output_tokens=1,
                               cache_creation_tokens=0, cache_read_tokens=0)


def _explain(client, monkeypatch, text, body=None):
    from portfolio import explain as ex
    llm = FakeLLM(text)
    monkeypatch.setattr(ex, "get_provider", lambda: llm)
    monkeypatch.setattr(ex.explain, "__defaults__", (None, lambda: llm))
    return client.post("/api/portfolio/explain", json=body or {}).json(), llm


def _answer(**sections):
    base = {"where_you_are_now": ["Your account is worth $1,005.00."], "going_well": [], "deserves_attention": [],
            "research_says": [], "watch_next": ["Watch the configured flags."], "adding_money": [], "caveats": []}
    base.update(sections)
    return json.dumps(base)


def test_explain_grounded_ok(client, enabled, monkeypatch):
    text = _answer(where_you_are_now=["Your calculated position value is $990.00 and cash is 1.00% of the total."],
                   deserves_attention=["SNDK is 45.00% of the portfolio, at or above the configured HIGH level of 30%.",
                                       "Cash is 1.00%, below the configured HIGH level of 2%."],
                   research_says=["No saved research is available for these holdings."],
                   caveats=["MU has no cost basis available."])
    out, llm = _explain(client, monkeypatch, text)
    assert out["status"] == "OK" and out["explanation"]["deserves_attention"][0].startswith("SNDK")
    prompt, system = llm.prompts[0]
    assert "Never recommend buying" in system and "RESEARCH VIEW" in system and "PORTFOLIO POLICY" in system
    assert "990.00" in prompt and "3450" in prompt and "account_number" not in prompt
    facts = out["facts"]
    assert facts["portfolio_policy"]["enabled"] is True and facts["portfolio_policy"]["flags"]
    assert "research_view_context" in facts and "saved_research" not in json.dumps(facts["positions"])
    assert facts["reconciliation"]["net_contributions_status"] == "UNAVAILABLE"


def test_explain_with_invented_number_is_withheld(client, enabled, monkeypatch):
    text = _answer(deserves_attention=["SNDK would be at 27.5% after changes.", "A 17.5% level applies."])
    out, _ = _explain(client, monkeypatch, text)
    assert out["status"] == "WITHHELD" and out["explanation"] is None
    assert "17.5%" in out["ungrounded_numbers"]


@pytest.mark.parametrize("phrase", ["You should trim SNDK.", "Consider selling some AMD.", "Buy 10 shares of MU.",
                                    "Reduce your semiconductor exposure.", "Rebalance toward cash.",
                                    "Place an order for NVDA.", "Put all $500 in now."])
def test_explain_with_order_or_sizing_language_is_withheld(client, enabled, monkeypatch, phrase):
    out, _ = _explain(client, monkeypatch, _answer(watch_next=[phrase]))
    assert out["status"] == "WITHHELD" and out["forbidden_language"]


@pytest.mark.parametrize("phrase,key", [("MU will rise after the jobs report.", "forecast_language"),
                                        ("There is a 90% chance it goes up.", "forecast_language"),
                                        ("This is the perfect entry.", "forecast_language"),
                                        ("Your account is about $1.0k.", "magnitude_terms"),
                                        ("SNDK is 2 times the size of MU.", "magnitude_terms")])
def test_explain_forecasts_and_new_arithmetic_withheld(client, enabled, monkeypatch, phrase, key):
    out, _ = _explain(client, monkeypatch, _answer(going_well=[phrase]))
    assert out["status"] == "WITHHELD" and key in out


def test_explain_without_llm(client, enabled, monkeypatch):
    from portfolio import explain as ex
    monkeypatch.setattr(ex.explain, "__defaults__", (None, lambda: None))
    out = client.post("/api/portfolio/explain", json={}).json()
    assert out["status"] == "AI_UNAVAILABLE" and out["facts"]["values"]["calculated_position_value"] == "990.00"


@pytest.fixture(autouse=True)
def isolated_ai_cache(monkeypatch):
    """The AI cache/usage tracker are process-global; keep each test independent."""
    from agents import gating
    monkeypatch.setattr(gating.ai_cache, "get_fresh", lambda *a, **k: None)
    monkeypatch.setattr(gating.ai_cache, "store", lambda *a, **k: None)
    monkeypatch.setattr(gating.usage_tracker, "can_call", lambda: (True, None))
    monkeypatch.setattr(gating.usage_tracker, "record", lambda *a, **k: None)


def test_scenario_policy_impact_uses_event_context(client, enabled):
    """Regression (found in browser validation): scenarios must load Stage 2.6 event context for policy rules."""
    monkeypatch_levels = client.post("/api/portfolio/scenario", json={"type": "hypothetical_add", "symbol": "MU",
                                                                      "amount_usd": 100, "funding": "new_money"})
    impact = monkeypatch_levels.json()["result"]["policy_impact"]
    families = {i["family"] for b in impact.values() if isinstance(b, list) for i in b}
    assert "event_exposure.unknown_coverage" not in families
    assert "event_exposure" in families  # NVDA is HIGH event risk in the fixture (22.20% -> INFO band)
