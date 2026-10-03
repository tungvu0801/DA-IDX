"""Stage 2.7G.1: operational polish — non-blocking event warm-up, 2.7F event budget, ops status, research batch UX.
No financial logic is tested as changed here; the point is that it is NOT changed and nothing blocks or spends."""
import hashlib
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from e_fixtures import bundle, tm
from insights import warmup

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_warmup(monkeypatch):
    from data.events.cache import event_cache
    warmup.reset_for_tests()
    event_cache._store.clear()
    monkeypatch.setattr(config, "has_fred_credentials", lambda: True)
    yield
    warmup.reset_for_tests()
    event_cache._store.clear()


def fill_fred_cache():
    from datetime import datetime, timezone
    from data.events.cache import event_cache
    start, end = warmup.event_window(datetime.now(timezone.utc))
    for rid in config.FRED_RELEASE_IDS.values():
        event_cache.set(f"fred:{rid}:{start.isoformat()}:{end.isoformat()}", [], 60)


def wait_until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ---- warm-up ----------------------------------------------------------------------------------------------------

def test_warmup_is_non_blocking_and_reports_loading_then_ready():
    release = threading.Event()

    def slow_fetch(symbol):
        assert symbol == config.MARKET_PROXY_SYMBOL                      # same call the market scan makes
        release.wait(5)
        fill_fred_cache()
    t = time.monotonic()
    assert warmup.start_event_warmup(slow_fetch) is True
    assert time.monotonic() - t < 0.2                                     # returned immediately
    assert warmup.macro_status()["state"] == "LOADING"
    assert warmup.start_event_warmup(slow_fetch) is False                 # never two warm-ups at once
    release.set()
    assert wait_until(lambda: warmup.macro_status()["state"] == "READY")


def test_warmup_failure_and_partial_results_are_unavailable_never_ready():
    def boom(symbol):
        raise RuntimeError("FRED down")
    warmup.start_event_warmup(boom)
    assert wait_until(lambda: warmup.macro_status()["state"] == "UNAVAILABLE")
    assert "RuntimeError" in warmup.macro_status()["detail"]
    assert warmup.start_event_warmup(boom) is False                       # retry is rate-limited…
    warmup.reset_for_tests()
    warmup.start_event_warmup(lambda s: None)                             # returns, but FRED cached nothing
    assert wait_until(lambda: warmup.macro_status()["state"] == "UNAVAILABLE")
    assert "0 of 4" in warmup.macro_status()["detail"]


def test_warmup_timeout_is_reported_unavailable_without_breaking(monkeypatch):
    monkeypatch.setattr(config, "EVENT_WARMUP_TIMEOUT_SECONDS", 0.05)
    release = threading.Event()
    warmup.start_event_warmup(lambda s: release.wait(5))
    time.sleep(0.1)
    st = warmup.macro_status()
    assert st["state"] == "UNAVAILABLE" and "has not answered" in st["detail"]
    release.set()


def test_fred_not_configured(monkeypatch):
    monkeypatch.setattr(config, "has_fred_credentials", lambda: False)
    assert warmup.start_event_warmup(lambda s: None) is False
    assert warmup.macro_status() == {"state": "UNAVAILABLE", "label": "Unavailable", "detail": "FRED is not configured."}


def test_warmup_reads_the_same_stage_2_6_cache_keys(monkeypatch):
    """The real Stage 2.6 build_event_context fills exactly the keys warm-up status checks (no second FRED client)."""
    from data.events import macro
    from services.event_context import build_event_context

    def fake_get(url, params=None, timeout=None):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"release_dates": [{"date": "2026-10-02"}]})
    monkeypatch.setattr(macro.requests, "get", fake_get)
    assert warmup.macro_cache_coverage()[0] == 0
    build_event_context(config.MARKET_PROXY_SYMBOL)
    cached, total = warmup.macro_cache_coverage()
    assert cached == total == len(config.FRED_RELEASE_IDS)
    assert warmup.macro_status()["state"] == "READY"
    src = (ROOT / "insights" / "warmup.py").read_text(encoding="utf-8")
    assert "api.stlouisfed.org" not in src and not re.search(r"^\s*import requests|^\s*from requests", src, re.M)


# ---- startup (lifespan) -------------------------------------------------------------------------------------------

def _recorders(monkeypatch):
    import agents.technical_agent as ta
    import insights.daily_explain as de
    from portfolio import explain as ex
    calls = []
    for mod in (ta, ex, de):
        monkeypatch.setattr(mod, "get_provider", lambda: calls.append(1))
    return calls


def test_startup_does_not_wait_for_fred_and_makes_no_claude_call(monkeypatch):
    from api.server import app
    calls = _recorders(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(warmup, "_default_fetch", lambda s: release.wait(5))
    t = time.monotonic()
    with TestClient(app) as c:
        assert time.monotonic() - t < 2
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/ops/status").json()["macro_events"]["state"] == "LOADING"
    release.set()
    assert calls == []


def test_startup_survives_warmup_errors(monkeypatch):
    from api.server import app

    def broken(*a, **k):
        raise RuntimeError("cannot start thread")
    monkeypatch.setattr(warmup, "start_event_warmup", broken)
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200


def test_startup_warmup_can_be_disabled(monkeypatch):
    from api.server import app
    monkeypatch.setattr(config, "EVENT_WARMUP_ON_STARTUP", False)
    started = []
    monkeypatch.setattr(warmup, "start_event_warmup", lambda *a, **k: started.append(1))
    with TestClient(app):
        pass
    assert started == []


# ---- 2.7F dashboard with slow event calendars -----------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from api.routes import daily_review as dr
    from api.routes import portfolio as pr
    from api.routes import watchlist as wl
    from api.server import app
    from insights import service
    from pf_fixtures import FakeGatewayHttp, fake_provider
    from test_market_insights import inputs
    calls = _recorders(monkeypatch)
    http = FakeGatewayHttp()
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    monkeypatch.setattr(pr, "event_lookup", lambda s: bundle("LOW"))
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: inputs())
    monkeypatch.setattr(service, "symbols_metrics_fn", lambda syms: {s: tm(s) for s in syms})
    monkeypatch.setattr(service, "cached_patterns", lambda compute: None)
    monkeypatch.setattr(wl, "get_cached_watchlist", lambda: (["NVDA", "AMD"], {}))
    monkeypatch.setattr(dr, "_outcomes", lambda syms: {})
    dr._events_inflight.clear()
    service._cache.clear()
    c = TestClient(app)
    c.calls, c.http = calls, http
    return c


def slow_events(monkeypatch):
    from api.routes import daily_review as dr
    from api.routes import portfolio as pr
    release = threading.Event()

    def slow(symbols):
        release.wait(5)
        return {s: bundle("LOW") for s in symbols}
    monkeypatch.setattr(pr, "_events_for", slow)
    monkeypatch.setattr(dr, "PORTFOLIO_EVENTS_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(dr, "EVENTS_BUDGET_SECONDS", 0.6)
    return release


def test_2_7f_dashboard_loads_while_event_calendars_are_slow(client, monkeypatch):
    release = slow_events(monkeypatch)
    t = time.monotonic()
    try:
        d = client.post("/api/insights/home", json={}).json()
    finally:
        release.set()
    assert time.monotonic() - t < 3
    assert d["robinhood"]["connected"] is True and len(d["my_stocks"]) == 4          # holdings still shown
    assert "still loading" in d["events_note"] and "unknown" in d["events_note"]
    for card in d["my_stocks"]:                                                       # nothing claims low/no event risk
        assert not any("event" in r for r in card["attention"]["reasons"])
    assert client.calls == []


def test_2_7f_normal_load_is_unchanged_when_events_are_fast(client):
    d = client.post("/api/insights/home", json={}).json()
    assert "events_note" not in d and len(d["my_stocks"]) == 4


def test_2_7g_event_loading_is_never_low_risk(client, monkeypatch):
    release = slow_events(monkeypatch)
    try:
        r = client.post("/api/insights/daily-review", json={}).json()
    finally:
        release.set()
    cards = [c for g in r["portfolio"]["groups"].values() for c in g] + \
            [c for g in r["watchlist"]["groups"].values() for c in g]
    assert cards and all(c["details"]["evidence"]["event_risk"] != "Low" for c in cards)
    assert all("No major event is close." not in c["details"]["all_supports"] for c in cards)


def test_robinhood_offline_then_reconnect(client, monkeypatch):
    import requests
    from api.routes import portfolio as pr
    from pf_fixtures import FakeGatewayHttp, fake_provider

    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("refused")
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(Down()))
    d = client.post("/api/insights/home", json={}).json()
    assert d["robinhood"]["connected"] is False and d["market"]["available"] is True
    r = client.post("/api/insights/daily-review", json={}).json()
    assert r["robinhood"]["connected"] is False and r["portfolio"]["available"] is False
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(FakeGatewayHttp()))
    d = client.post("/api/insights/home", json={}).json()
    assert d["robinhood"]["connected"] is True and len(d["my_stocks"]) == 4


# ---- ops status ------------------------------------------------------------------------------------------------------

def test_ops_status_never_shows_unverified_health(client, monkeypatch):
    monkeypatch.setattr(warmup, "_default_fetch", lambda s: None)       # a warm-up that caches nothing
    s = client.get("/api/ops/status").json()
    assert s["market_data"]["label"] == "NOT_LOADED"                    # no scan cached yet -> not "fresh"
    assert s["macro_events"]["state"] in ("LOADING", "UNAVAILABLE", "NOT_LOADED")
    assert s["ai"] == {"calls_last_hour": s["ai"]["calls_last_hour"], "hourly_limit": config.AI_MAX_CALLS_PER_HOUR,
                       "calls_today": s["ai"]["calls_today"], "daily_limit": config.AI_MAX_CALLS_PER_DAY}
    client.post("/api/insights/home", json={})
    fill_fred_cache()
    s = client.get("/api/ops/status").json()
    assert s["macro_events"]["state"] == "READY" and s["market_data"]["label"] in ("FRESH", "AGING", "STALE")
    assert client.calls == []


def test_page_loads_make_no_claude_calls_and_no_database_changes(client):
    from database.database import get_db
    db = Path(get_db().db_path) if hasattr(get_db(), "db_path") else None
    before = hashlib.sha256(db.read_bytes()).hexdigest() if db and db.exists() else None
    for _ in range(2):
        client.post("/api/insights/home", json={})
        client.post("/api/insights/daily-review", json={})
        client.get("/api/ops/status")
    assert client.calls == []
    if before:
        assert hashlib.sha256(db.read_bytes()).hexdigest() == before


# ---- research batch UX (limits are enforced by the unchanged 2.7G plan; UI never auto-runs) -------------------------

def test_research_batch_requires_confirmation_and_never_runs_on_a_timer():
    js = (ROOT / "frontend" / "daily_review.js").read_text(encoding="utf-8")
    body = js[js.index("async function analyzeMissing"):js.index("async function explain")]
    confirm_handler = body[body.index('yes.addEventListener("click"'):]
    assert "ResearchIds.analyze" in confirm_handler and "ResearchIds.analyze" not in body[:body.index('yes.addEventListener')]
    assert "for (const [i, sym] of plan.analyze_now.entries())" in confirm_handler   # only what fits the limits
    assert "Analyze available stocks" in body and "Cancel" in body and '"Hourly AI limit"' in body
    assert "Research updated:" in js and "Still needed:" in js and "Nothing continues automatically." in js
    for name in ("daily_review.js", "command_center.js"):
        src = (ROOT / "frontend" / name).read_text(encoding="utf-8")
        assert "setInterval(" not in src and "setTimeout(" not in src, name


def test_research_batch_limits_and_cache_hits():
    from insights import decision as D
    from insights.daily_review import research_call_estimates
    need = ["SNDK", "AVGO", "NVDA", "MRVL", "CLS", "SHOP"]
    hourly = D.research_plan(need, {s: 3 for s in need}, calls_last_hour=0, calls_today=0)
    assert hourly["analyze_now"] == ["SNDK", "AVGO", "NVDA"] and hourly["limited_by"] == "hourly AI call limit"
    daily = D.research_plan(need, {s: 3 for s in need}, calls_last_hour=0, calls_today=config.AI_MAX_CALLS_PER_DAY - 3)
    assert daily["analyze_now"] == ["SNDK"] and daily["limited_by"] == "daily AI call limit"

    class Cache:
        def get_fresh(self, symbol, t, price, att):
            return object() if symbol == "SNDK" else None               # SNDK fully cached -> 0 new calls
    est = research_call_estimates(need, {s: tm(s) for s in need}, Cache())
    assert est["SNDK"] == 0
    assert D.research_plan(need, est, 0, 0)["analyze_now"] == ["SNDK", "AVGO", "NVDA", "MRVL"]


# ---- safety -----------------------------------------------------------------------------------------------------------

NEW_OR_CHANGED = ["insights/warmup.py", "api/routes/ops_status.py", "api/server.py", "api/routes/insights.py",
                  "frontend/daily_review.js", "frontend/command_center.js"]


def test_no_broker_mutation_or_execution_added():
    for f in NEW_OR_CHANGED:
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"TradingClient|place_?order|submit_?order|cancel_?order|webhook|alpaca\.trading", text, re.I), f
    from api.server import app
    paths = app.openapi()["paths"]      # app.routes holds unexpanded _IncludedRouter objects in this FastAPI version
    assert set(paths["/api/ops/status"]) == {"get"}
    from paper_endpoints import paper_exempt
    for path, ops in paths.items():     # read-only order HISTORY (2.7C) is GET; nothing can place one
        if paper_exempt(path, ops):  # Stage 4.5: the exact local paper paths only
            continue
        assert not re.search(r"execute|broker|place|cancel|submit", path), path
        if re.search(r"order", path):
            assert set(ops) == {"get"}, path
