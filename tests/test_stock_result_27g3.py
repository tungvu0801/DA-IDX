"""Stage 2.7G.3: ANALYZE results visible and actionable (presentation + integration; no new financial logic)."""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from e_fixtures import bundle, tm
from insights import decision as D

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
FRESH_ID = "feedc0de00"          # fresh research for MU (small holding: purely "research needed" before)


def fake_cached(symbol, gated=False):
    now = datetime.now(timezone.utc)
    b = SimpleNamespace(
        research_view="BULLISH BIAS", metrics=tm(symbol),
        evidence=SimpleNamespace(bullish_pct=60, neutral_pct=30, bearish_pct=10,
                                 breakdown=[SimpleNamespace(category="technical", score=0.4, weight=0.3)]),
        data_quality=SimpleNamespace(level="HIGH", explanation="All sources available."),
        catalyst=SimpleNamespace(items=[SimpleNamespace(title="Chip demand", source="benzinga", published_at="2026-09-27",
                                                        sentiment="POSITIVE", reason="demand")], message=""),
        risk_flags=[SimpleNamespace(code="EXTENDED", severity="MEDIUM", description="Price is extended.")],
        risk_explanation="AI analysis unavailable right now — Hourly AI call limit reached." if gated else "Risk is moderate.",
        narrative=SimpleNamespace(whats_happening="Plain summary.", why="Because.", whats_good=["x"], be_careful=["y"]),
        sector=None, events=None)
    return SimpleNamespace(symbol=symbol, bundle=b, setup=None, generated_at=now)


def snapshot(symbol, hours_old):
    return SimpleNamespace(
        id=1, symbol=symbol, created_at=datetime.now(timezone.utc) - timedelta(hours=hours_old), research_view="MIXED / WAIT",
        bullish_pct=40, neutral_pct=40, bearish_pct=20, evidence_technical=0.1, evidence_catalyst=None, evidence_risk=-0.1,
        evidence_market=0.2, evidence_sector=0.1, data_quality_level="MEDIUM", ema_9=11.8, ema_20=11.5, ema_50=11.0,
        rsi=55.0, momentum_5d_pct=2.0, momentum_10d_pct=3.0, volume=1e6, relative_volume=1.2, support=10.5,
        resistance=13.5, atr=0.4, volatility_pct=30.0, catalysts_json="[]", risk_flags_json="[]", sector_name=None,
        sector_etf=None, sector_pct_change=None, market_pct_change=None)


@pytest.fixture
def client(monkeypatch):
    import agents.technical_agent as ta
    import insights.daily_explain as de
    from api.routes import daily_review as dr
    from api.routes import portfolio as pr
    from api.routes import watchlist as wl
    from api.server import app
    from database.database import get_db
    from insights import service
    from pf_fixtures import FakeGatewayHttp, fake_provider
    from portfolio import explain as ex
    from services.analysis_cache import analysis_cache
    from test_market_insights import inputs
    calls = []
    for mod in (ta, ex, de):
        monkeypatch.setattr(mod, "get_provider", lambda: calls.append(1))
    http = FakeGatewayHttp()
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    monkeypatch.setattr(pr, "event_lookup", lambda s: bundle("LOW"))
    # market data stamped with the request clock: a fixed timestamp turns "stale" (30 min) as real time passes
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: inputs(fetched=now))
    monkeypatch.setattr(service, "symbols_metrics_fn", lambda syms: {s: tm(s) for s in syms})
    monkeypatch.setattr(service, "cached_patterns", lambda compute: None)
    monkeypatch.setattr(wl, "get_cached_watchlist", lambda: (["NVDA", "AMD"], {}))
    monkeypatch.setattr(dr, "_outcomes", lambda syms: {})
    snaps = {}
    monkeypatch.setattr(get_db(), "list_snapshots", lambda symbol=None, limit=100, **k: snaps.get(symbol, []))
    store = {FRESH_ID: fake_cached("MU"), "feedc0de01": fake_cached("AMD"), "feedc0de02": fake_cached("SHOP", gated=True)}
    monkeypatch.setattr(analysis_cache, "get", lambda aid: store.get(aid))
    service._cache.clear()
    dr._events_inflight.clear()
    c = TestClient(app)
    c.calls, c.http, c.snaps = calls, http, snaps
    return c


def decide(client, symbol, analysis_id=None):
    return client.post("/api/insights/stock-decision", json={"symbol": symbol, "analysis_id": analysis_id}).json()


def test_research_needed_then_fresh_research_visible_with_deterministic_state(client):
    before = decide(client, "MU")
    assert before["research"]["available"] is False and before["research"]["freshness"] == "MISSING"
    assert before["decision"]["state"] == D.RESEARCH                                   # research-needed card
    after = decide(client, "MU", FRESH_ID)
    assert after["research"]["source"] == "FRESH_RESEARCH" and after["research"]["freshness"] == "FRESH"
    assert after["research"]["research_view"] == "BULLISH BIAS"
    assert after["decision"]["state"] in D.OWNED_STATES and after["decision"]["state"] != D.RESEARCH
    assert after["full_analysis"]["source"] == "FRESH_RESEARCH"
    assert client.calls == []                                                           # no Claude call


def test_stale_research_replaced_after_successful_refresh(client):
    client.snaps["MU"] = [snapshot("MU", 40)]
    stale = decide(client, "MU")
    assert stale["research"]["freshness"] == "STALE" and stale["decision"]["state"] == D.RESEARCH
    assert stale["full_analysis"]["source"] == "SAVED_SNAPSHOT"
    fresh = decide(client, "MU", FRESH_ID)
    assert fresh["research"]["freshness"] == "FRESH" and fresh["decision"]["state"] != D.RESEARCH


def test_owned_uses_owned_states_and_watchlist_uses_watchlist_states(client):
    owned = decide(client, "MU")
    assert owned["owned"] is True and owned["decision"]["owned"] is True and owned["decision"]["state"] in D.OWNED_STATES
    wl = decide(client, "AMD", "feedc0de01")
    assert wl["owned"] is False and wl["decision"]["owned"] is False and wl["decision"]["state"] in D.WATCH_STATES
    assert wl["decision"]["group"] in D.WATCH_GROUPS


def test_offline_watchlist_state_and_unknown_ownership(client, monkeypatch):
    import requests
    from api.routes import portfolio as pr
    from pf_fixtures import fake_provider

    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("refused")
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(Down()))
    d = decide(client, "MU")
    assert d["owned"] is None and d["decision"]["owned"] is None and d["decision"]["state"] in D.WATCH_STATES
    assert d["decision"]["details"]["layers"]["portfolio"] == "Unavailable"


def test_same_decision_as_the_daily_review_for_the_same_inputs(client):
    rep = client.post("/api/insights/daily-review", json={}).json()
    mu = next(c for g in rep["portfolio"]["groups"].values() for c in g if c["symbol"] == "MU")
    assert decide(client, "MU")["decision"] == mu


def test_conflicts_levels_and_missing_values(client, monkeypatch):
    from insights import service
    from test_market_insights import inputs
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: inputs(spy_trend="Bearish", spy_mom5=-3.0, spy_pct=-1.0, fetched=now))
    service._cache.clear()
    d = decide(client, "MU")
    codes = {c["code"] for c in d["decision"]["details"]["conflicts"]}
    assert "market_weak_stock_strong" in codes                                     # market ↔ stock conflict shown
    assert d["decision"]["watch_next"]["support"] == "10.5" and d["decision"]["watch_next"]["resistance"] == "13.5"
    monkeypatch.setattr(service, "symbols_metrics_fn", lambda syms: {})
    m = decide(client, "MU")
    assert m["decision"]["state"] == D.INSUFFICIENT                                # nothing invented
    assert m["decision"]["watch_next"]["support"] is None and m["full_analysis"] is None


def test_full_analysis_uses_existing_outputs_and_hides_gated_ai_text(client):
    f = decide(client, "MU", FRESH_ID)["full_analysis"]
    assert f["evidence"]["bullish_pct"] == 60 and f["catalysts"]["items"][0]["title"] == "Chip demand"
    assert f["levels"]["support"] == 10.5 and f["risk"]["explanation_ai"] == "Risk is moderate."
    g = decide(client, "SHOP", "feedc0de02")["full_analysis"]
    assert g["risk"]["explanation_ai"] is None                                      # gated text is not shown as analysis
    assert client.calls == []


def test_route_makes_no_claude_call_and_no_database_change(client):
    from database.database import get_db
    db = Path(get_db().db_path) if hasattr(get_db(), "db_path") else None
    before = hashlib.sha256(db.read_bytes()).hexdigest() if db and db.exists() else None
    for sym, aid in (("MU", FRESH_ID), ("SHOP", None), ("AMD", "feedc0de01")):
        decide(client, sym, aid)
    assert client.calls == []
    if before:
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert client.post("/api/insights/stock-decision", json={"symbol": "MU", "side": "buy"}).status_code == 422


# ---- UI flow (static): one research call at most; rendering/full/collapse never request -----------------------------

def _fn(src, start, end):
    return src[src.index(start):src.index(end)]


def test_analyze_flow_checks_limits_confirms_and_measures_calls():
    sr = (FRONT / "stock_result.js").read_text(encoding="utf-8")
    a = _fn(sr, "async function analyze(", "function reopen(")
    assert a.index("/api/insights/daily-review/research-plan") < a.index("ResearchIds.analyzeFull")   # capacity first
    assert a.index("if (!plan.analyze_now.length)") < a.index("data-yes")                            # none -> no call
    assert a.index('querySelector("[data-yes]").addEventListener') < a.index("ResearchIds.analyzeFull")  # confirmed
    assert a.index("const before = await aiCallsToday()") < a.index("ResearchIds.analyzeFull") < a.index("const after = await aiCallsToday()")
    assert a.index("if (gated) return failure(") < a.index("window.ResearchIds.set(")                # gated never stored
    assert sr.count("ResearchIds.analyzeFull(") == 1 and "ResearchIds.set(" not in _fn(sr, "async function failure(", "async function analyze(")
    for text in ("✓ ANALYSIS UPDATED", "✓ CURRENT ANALYSIS LOADED", "ANALYSIS COULD NOT BE COMPLETED", "EXISTING SAVED RESEARCH",
                 "CURRENT VIEW", "NEXT THING TO CONSIDER", "WHAT I'M WAITING FOR", "SETUP IMPROVES IF",   # 2.8C labels
                 "SETUP WEAKENS IF", "Full analysis", "Quick check", "Collapse", "CONFLICTING EVIDENCE"):
        assert text in sr, text


def test_rendering_full_analysis_and_collapse_make_no_requests():
    sr = (FRONT / "stock_result.js").read_text(encoding="utf-8")
    for start, end in (("function collapse(", "function researchLine("), ("function decisionHtml(", "function fullHtml("),
                       ("function fullHtml(", "function panelHtml("), ("function panelHtml(", "function show("),
                       ("function show(", "async function failure("), ("function reopen(", "window.StockResult")):
        body = _fn(sr, start, end)
        assert "fetch(" not in body and "post(" not in body and "getJSON(" not in body and "analyze" not in body.replace("analysis", ""), start


def test_card_integration_no_full_reload_and_obvious_analyze():
    cc = (FRONT / "command_center.js").read_text(encoding="utf-8")
    one = _fn(cc, "function analyzeOne(", "async function analyzeAll(")
    assert "window.StockResult.analyze(sym, host" in one and "render(root" not in one           # no dashboard reload
    assert '.cc-research")' in one and "cc-analyze-needed" in one                               # research line updated in place
    # 2.8A/B compact card: the summary sentence moved to the card's hover text; the one caution line is refreshed
    assert "card.title =" in one and '.cc-why")' in one and "research (needed|is stale)" in one  # stale wording replaced
    assert "cc-analyze-needed" in _fn(cc, "function stockCard(", "function myStocks(")
    assert 'data-analyze-w="' in cc and 'data-result-for="' in cc                               # watchlist Analyze + result host
    full = _fn(cc, "async analyzeFull(", "window.ResearchIds = ResearchIds")
    assert "this.set(" not in full                                                                # id stored only after checks
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    assert html.index("stock_result.js") < html.index("command_center.js")


def test_no_timers_execution_or_broker_mutation():
    for f in ("frontend/stock_result.js", "frontend/command_center.js", "insights/stock_result.py",
              "api/routes/stock_decision.py"):
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"setTimeout\(|setInterval\(|TradingClient|place_?order|submit_?order|cancel_?order|webhook|"
                             r"buy now|sell now", text, re.I), f
    from api.server import app
    paths = app.openapi()["paths"]
    assert set(paths["/api/insights/stock-decision"]) == {"post"}
    from paper_endpoints import paper_exempt
    for path, ops in paths.items():
        if paper_exempt(path, ops):  # Stage 4.5: the exact local paper paths only
            continue
        assert not re.search(r"execute|broker|place|cancel|submit", path), path
        if "order" in path:
            assert set(ops) == {"get"}, path
