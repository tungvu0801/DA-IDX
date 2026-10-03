"""Stage 2.7G.2: the 2.7F "Analyze my holdings" action uses the 2.7G limit-aware planner (UX/planning only)."""
import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from e_fixtures import bundle, tm
from insights import decision as D

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
NEED = ["SNDK", "AVGO", "NVDA", "MRVL", "CLS", "SHOP"]


# ---- the unchanged 2.7G planner, in every capacity situation ------------------------------------------------------

def test_hourly_capacity_available():
    p = D.research_plan(NEED[:3], {s: 3 for s in NEED[:3]}, calls_last_hour=0, calls_today=0)
    assert p["analyze_now"] == NEED[:3] and p["remaining"] == [] and p["calls_for_now"] == 9 and p["limited_by"] is None


def test_hourly_capacity_partially_available():
    p = D.research_plan(NEED, {s: 3 for s in NEED}, calls_last_hour=0, calls_today=0)
    assert p["analyze_now"] == ["SNDK", "AVGO", "NVDA"] and p["remaining"] == ["MRVL", "CLS", "SHOP"]
    assert p["calls_for_now"] == 9 and p["limited_by"] == "hourly AI call limit"


def test_no_hourly_capacity_runs_nothing():
    p = D.research_plan(NEED, {s: 3 for s in NEED}, calls_last_hour=config.AI_MAX_CALLS_PER_HOUR, calls_today=0)
    assert p["analyze_now"] == [] and p["calls_for_now"] == 0 and p["remaining"] == NEED
    assert p["limited_by"] == "hourly AI call limit"


def test_daily_limit_reached_runs_nothing():
    p = D.research_plan(NEED, {s: 3 for s in NEED}, calls_last_hour=0, calls_today=config.AI_MAX_CALLS_PER_DAY)
    assert p["analyze_now"] == [] and p["limited_by"] == "daily AI call limit"


def test_cache_hits_consume_zero_new_calls():
    p = D.research_plan(NEED, {s: 0 for s in NEED}, calls_last_hour=config.AI_MAX_CALLS_PER_HOUR, calls_today=0)
    assert p["analyze_now"] == NEED and p["calls_for_now"] == 0 and p["estimated_max_calls"] == 0


# ---- routes the button uses (coverage from the 2.7G report, limits re-checked by the 2.7G planner route) ----------

@pytest.fixture
def client(monkeypatch):
    import agents.technical_agent as ta
    import insights.daily_explain as de
    from api.routes import daily_review as dr
    from api.routes import portfolio as pr
    from api.routes import watchlist as wl
    from api.server import app
    from insights import service
    from pf_fixtures import FakeGatewayHttp, fake_provider
    from portfolio import explain as ex
    from test_market_insights import inputs
    calls = []
    for mod in (ta, ex, de):
        monkeypatch.setattr(mod, "get_provider", lambda: calls.append(1))
    http = FakeGatewayHttp()
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    monkeypatch.setattr(pr, "event_lookup", lambda s: bundle("LOW"))
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: inputs())
    monkeypatch.setattr(service, "symbols_metrics_fn", lambda syms: {s: tm(s) for s in syms})
    monkeypatch.setattr(service, "cached_patterns", lambda compute: None)
    monkeypatch.setattr(wl, "get_cached_watchlist", lambda: (["NVDA", "AMD"], {}))
    monkeypatch.setattr(dr, "_outcomes", lambda syms: {})
    service._cache.clear()
    c = TestClient(app)
    c.calls, c.http = calls, http
    return c


def test_coverage_contract_used_by_the_button(client):
    cov = client.post("/api/insights/daily-review", json={}).json()["portfolio"]["coverage"]
    assert {"current", "total", "need", "plan"} <= set(cov)
    assert {"analyze_now", "remaining", "calls_for_now", "limited_by", "hourly_limit", "calls_last_hour",
            "daily_limit", "calls_today"} <= set(cov["plan"])
    assert client.calls == []


def test_limits_rechecked_by_the_planner_route(client, monkeypatch):
    import insights.daily_review as dmod
    monkeypatch.setattr(dmod, "ai_usage_counts", lambda t: {"last_hour": config.AI_MAX_CALLS_PER_HOUR, "today": 0})
    p = client.post("/api/insights/daily-review/research-plan", json={"symbols": ["MU", "NVDA"]}).json()
    assert p["analyze_now"] == [] and p["limited_by"] == "hourly AI call limit"
    monkeypatch.setattr(dmod, "ai_usage_counts", lambda t: {"last_hour": 0, "today": config.AI_MAX_CALLS_PER_DAY})
    p = client.post("/api/insights/daily-review/research-plan", json={"symbols": ["MU", "NVDA"]}).json()
    assert p["analyze_now"] == [] and p["limited_by"] == "daily AI call limit"
    assert client.calls == []


def test_cached_analyses_estimate_zero_through_the_route(client, monkeypatch):
    from agents.ai_cache import ai_cache
    monkeypatch.setattr(ai_cache, "get_fresh", lambda *a, **k: SimpleNamespace())
    p = client.post("/api/insights/daily-review/research-plan", json={"symbols": ["MU", "NVDA"]}).json()
    assert p["estimated_max_calls"] == 0 and p["analyze_now"] == ["MU", "NVDA"]


def test_page_loads_make_no_claude_calls_and_no_database_changes(client):
    from database.database import get_db
    db = Path(get_db().db_path) if hasattr(get_db(), "db_path") else None
    before = hashlib.sha256(db.read_bytes()).hexdigest() if db and db.exists() else None
    client.post("/api/insights/home", json={})
    client.post("/api/insights/daily-review", json={})
    client.post("/api/insights/daily-review/research-plan", json={"symbols": NEED})
    assert client.calls == []
    if before:
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before


# ---- the button's flow (static): confirmation first, re-check before running, never automatic -----------------------

def test_analyze_my_holdings_requires_confirmation_and_rechecks_limits():
    cc = (FRONT / "command_center.js").read_text(encoding="utf-8")
    fn = cc[cc.index("async function analyzeAll"):cc.index("async function loadScanner")]
    before_yes, after_yes = fn.split('yes.addEventListener("click"', 1)
    assert "ResearchBatch.coverage(" in before_yes and "ResearchBatch.confirmHtml(cov)" in before_yes
    assert "ResearchBatch.run(" not in before_yes and "ResearchIds.analyze" not in before_yes      # nothing runs before OK
    assert "window.ResearchBatch.run(cov.need" in after_yes
    assert "needs_research" not in fn and "max_ai_calls" not in fn         # old unlimited loop is gone

    rb = (FRONT / "research_batch.js").read_text(encoding="utf-8")
    run = rb[rb.index("async function run"):rb.index("function resultHtml")]
    assert run.index("/api/insights/daily-review/research-plan") < run.index("ResearchIds.analyze")   # re-check first
    assert run.index("if (!plan.analyze_now.length)") < run.index("for (const")                        # 0 calls if none
    assert "for (const [i, sym] of plan.analyze_now.entries())" in run                              # only what fits now
    assert rb.count("await window.ResearchIds.analyze(") == 1 and "await window.ResearchIds.analyze(" in run


def test_no_automatic_continuation_or_background_queue():
    for name in ("research_batch.js", "command_center.js"):
        src = (FRONT / name).read_text(encoding="utf-8")
        assert not re.search(r"setTimeout\(|setInterval\(|requestIdleCallback|new Worker|navigator\.sendBeacon", src), name
    rb = (FRONT / "research_batch.js").read_text(encoding="utf-8")
    assert "Nothing continues automatically." in rb


def test_confirmation_wording_matches_2_7g_and_reuses_its_design():
    rb = (FRONT / "research_batch.js").read_text(encoding="utf-8")
    for text in ("RESEARCH COVERAGE", "holdings current", "Need research:", "AI CAPACITY", "Hourly limit",
                 "Used this hour", "Daily limit", "Used today", "CAN ANALYZE NOW", "WAIT UNTIL LATER",
                 "Estimated maximum new Claude calls", "Analyze available stocks", "Cancel", "No AI capacity right now",
                 "no AI calls are needed", "Research updated:", "Still needed:", "Reason:", "Hourly AI limit",
                 "Daily AI limit"):
        assert text in rb, text
    for cls in ("cc-confirm dr-confirm", "dr-cov-grid", "dr-kv", "dr-batch"):                # existing 2.7G classes
        assert cls in rb
    assert not (FRONT / "research_batch.css").exists()
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    assert html.index("research_batch.js") < html.index("command_center.js")


def test_no_execution_or_broker_mutation_in_new_code():
    for f in ("frontend/research_batch.js", "frontend/command_center.js"):
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"TradingClient|place_?order|submit_?order|cancel_?order|webhook|/orders|buy now|sell now",
                             text, re.I), f
    from api.server import app
    from paper_endpoints import paper_exempt
    for path, ops in app.openapi()["paths"].items():
        if paper_exempt(path, ops):  # Stage 4.5: the exact local paper paths only
            continue
        assert not re.search(r"execute|broker|place|cancel|submit", path), path
        if "order" in path:
            assert set(ops) == {"get"}, path
