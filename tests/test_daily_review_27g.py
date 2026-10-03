"""Stage 2.7G: portfolio + watchlist decision report — deterministic states, conflicts, context and safety."""
import copy
import inspect
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from e_fixtures import bundle, ev, market, no_research, research, tm, view
from insights import decision as D
from insights.daily_review import assemble, research_call_estimates
from insights.decision import build_evidence, owned_state, watchlist_state
from portfolio.models import to_jsonable

Dec = Decimal
NOW = datetime(2026, 9, 28, 0, 1, tzinfo=timezone.utc)       # market fixture fetched 2026-09-28T00:00
REF = "2026-09-27T23:59:00+00:00"                            # latest market-data trade time (SPY)
ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {"available": True, "patterns": [{"kind": "extended", "label": "After a fast rise", "count": 26, "of": 49}]}


def pos(pnl="10", pnl_pct="5.00", weight="10.00", price=12.0, quality="OK", ts="2026-09-27T23:58:00+00:00",
        event=None):
    return SimpleNamespace(last_price=Dec(str(price)), price_timestamp=ts, event_risk_level=event,
                           market_value=Dec("100.00"), avg_cost=Dec("11.00"),
                           unrealized_pnl=None if pnl is None else Dec(pnl),
                           unrealized_pnl_pct=None if pnl_pct is None else Dec(pnl_pct), portfolio_weight=Dec(weight),
                           cost_basis_total=Dec("90.00"), quote_quality=quality)


def E(owned=True, m="default", p="default", r=None, events="default", mk=None, concern=None, sector_sev=None,
      connected=True, ref=REF):
    return build_evidence(
        symbol="MU", owned=owned, metrics=tm("MU") if m == "default" else m,
        position=(pos() if p == "default" else p) if owned else None, research=r if r is not None else research(),
        events_bundle=bundle("LOW") if events == "default" else events, market=mk or market("SUPPORTIVE"),
        sector="Semiconductors", sector_pct=0.5, concern=concern, sector_severity=sector_sev, market_ref_time=ref,
        portfolio_available=connected, now=NOW)


# ---- owned-stock states ----------------------------------------------------------------------------------------

def test_supportive_setup():
    d = owned_state(E())
    assert d["state"] == D.SUPPORTED and d["group"] == "STABLE / MONITOR"
    assert d["layers"] == {"market": "Supportive", "stock": "Supportive", "portfolio": "OK",
                           "summary": "MARKET AND STOCK SUPPORTIVE"}


def test_owned_profitable_near_resistance_is_protect_gains_not_sell():
    d = owned_state(E(p=pos(price=13.3)))                                  # 1.5% below resistance 13.5
    assert d["state"] == D.PROTECT and d["group"] == "NEEDS ATTENTION"
    assert "not an instruction to sell" in d["next"]
    assert d["cautions"][0].startswith("Price is near resistance")


def test_profitable_but_weakening_momentum():
    d = owned_state(E(m=tm("MU", momentum_score=35, momentum_5d_pct=-1.0)))
    assert d["state"] == D.PROTECT
    assert "profitable_vs_momentum" in {c["code"] for c in d["conflicts"]}


def test_owned_losing_position_near_support_never_suggests_adding():
    d = owned_state(E(p=pos(pnl="-10", pnl_pct="-8.00", price=10.6)))
    assert d["state"] == D.MON_SUPPORT
    assert "not by itself a reason to add" in d["next"]


def test_losing_but_setup_intact_is_not_weakening_and_no_add_state_exists():
    d = owned_state(E(p=pos(pnl="-10", pnl_pct="-8.00")))
    assert d["state"] == D.SUPPORTED
    assert not any("ADD" in s for s in D.OWNED_STATES + D.WATCH_STATES)
    assert not re.search(r"\badd (more|to)\b|average down", json.dumps(to_jsonable(d)), re.I)


def test_large_concentrated_position():
    d = owned_state(E(concern={"position_severity": "HIGH", "sector_severity": None, "position_weight": "30"}))
    assert d["state"] == D.SIZE and d["cautions"][0].startswith("The position is already large")
    assert d["layers"]["summary"] == "SUPPORTED, BUT PORTFOLIO RISK IS HIGH"
    assert "stock_vs_position" in {c["code"] for c in d["conflicts"]}


def test_high_sector_concentration_is_a_conflict_not_averaged_away():
    d = owned_state(E(concern={"position_severity": None, "sector_severity": "HIGH", "sector_weight": "73.26"}))
    assert d["state"] == D.SUPPORTED                                      # stock state unchanged
    assert d["next"].endswith("monitor rather than automatically adding.")
    assert d["layers"]["summary"] == "SUPPORTED, BUT PORTFOLIO RISK IS HIGH"
    c = next(x for x in d["conflicts"] if x["code"] == "stock_vs_sector")
    assert (c["a"], c["b"]) == ("STOCK ATTRACTIVE", "SECTOR CONCENTRATION ALREADY HIGH")
    assert any("73.26%" in x for x in d["cautions"])


def test_weakening_setup():
    m = tm("MU", trend="Bearish", ema_fast=10.0, ema_medium=11.0, ema_slow=12.0, momentum_score=35, momentum_5d_pct=-3)
    assert owned_state(E(m=m))["state"] == D.WEAKENING
    assert watchlist_state(E(owned=False, m=m))["state"] == D.W_HIGHER


def test_high_event_risk():
    x = E(events=bundle("HIGH", macro=[ev(hours=6.0)]))
    d = owned_state(x)
    assert d["state"] == D.EVENT_REVIEW and "technical_vs_event" in {c["code"] for c in d["conflicts"]}
    assert watchlist_state(E(owned=False, events=bundle("HIGH"))) ["state"] == D.W_EVENT


def test_stale_quote_downgrades_to_data_stale():
    old = owned_state(E(p=pos(ts="2026-09-27T23:30:00+00:00")))          # 29 min older than the latest market data
    assert old["state"] == D.DATA_STALE and "older than the latest market data" in old["cautions"][0]
    assert owned_state(E(p=pos(quality="UNRELIABLE")))["state"] == D.DATA_STALE
    # a weekend close is NOT stale when nothing newer exists (reference = latest market data)
    assert owned_state(E(p=pos(ts="2026-09-25T20:00:00+00:00"), ref="2026-09-25T20:00:00+00:00"))["state"] != D.DATA_STALE


def test_stale_and_missing_research_never_presented_as_current():
    stale = E(r=research(age_hours=30.0))
    d = owned_state(stale)
    assert d["state"] == D.RESEARCH and stale["research"]["freshness"] == "STALE" and not stale["research"]["current"]
    assert not any("Research is Bullish" in x for x in d["supports"])
    missing = owned_state(E(r=no_research()))
    assert missing["state"] == D.RESEARCH and any("No research saved" in x for x in missing["cautions"])
    from insights.daily_review import evidence_words
    assert evidence_words(stale)["research"].startswith("Old research")


def test_missing_event_data_is_unknown_not_low():
    x = E(events=None)
    d = owned_state(x)
    assert x["event_risk"] == "UNAVAILABLE"
    assert "No major event is close." not in d["supports"]
    assert any("Event data is unavailable" in c for c in d["cautions"])


def test_market_supportive_vs_weak_and_stock_market_conflicts():
    weak = E(mk=market("CAUTIOUS"))
    assert owned_state(weak)["state"] == D.WAIT_CONF
    assert watchlist_state(E(owned=False, mk=market("CAUTIOUS")))["state"] == D.W_MARKET
    assert "market_weak_stock_strong" in {c["code"] for c in owned_state(weak)["conflicts"]}
    down = tm("MU", trend="Bearish", momentum_score=50, momentum_5d_pct=1.0)
    assert "market_strong_stock_weak" in {c["code"] for c in owned_state(E(m=down))["conflicts"]}


def test_insufficient_data():
    assert owned_state(E(m=None))["state"] == D.INSUFFICIENT
    assert watchlist_state(E(owned=False, m=None))["state"] == D.INSUFFICIENT


# ---- watchlist states ------------------------------------------------------------------------------------------

def test_watchlist_states_near_support_resistance_extended():
    near_s = watchlist_state(E(owned=False, m=tm("MU", price=10.6)))
    assert near_s["state"] == D.W_REVIEW and any("near support" in x for x in near_s["supports"])
    near_r = watchlist_state(E(owned=False, m=tm("MU", price=13.3)))
    assert near_r["state"] == D.W_BREAKOUT and any("above resistance" in w for w in near_r["waiting_for"])
    ext = watchlist_state(E(owned=False, m=tm("MU", momentum_5d_pct=12.0, rsi=74.0)))
    assert ext["state"] == D.W_PULLBACK and ext["waiting_for"][0].startswith("A pullback toward support")
    assert "WORTH FURTHER REVIEW" in D.WATCH_GROUPS["SETUPS WORTH REVIEWING"]


def test_pattern_context_only_when_extended_and_never_changes_state():
    ext = E(owned=False, m=tm("MU", momentum_5d_pct=12.0, rsi=74.0))
    before = copy.deepcopy(watchlist_state(ext))
    ctx = D.pattern_context(ext, PATTERNS)
    assert ctx["title"] == "CHASING RISK" and "26 of 49" in ctx["text"] and "does not mean" in ctx["text"]
    assert watchlist_state(ext) == before
    assert D.pattern_context(E(owned=False), PATTERNS) is None                      # not extended -> not shown
    assert D.pattern_context(ext, {"available": False}) is None                     # unverified -> not shown


def test_historical_outcomes_are_context_and_hide_tiny_samples():
    from services.analytics import GroupStats
    small = GroupStats("MU", 4, 1.0, 0.8, 0.75, 2.0, -1.0, None, None, "Very small sample")
    ctx = D.outcome_context(small, 5)
    assert ctx["n"] == 4 and ctx["enough_sample"] is False and ctx["positive_return_frequency_pct"] is None
    big = GroupStats("MU", 12, 1.0, 0.8, 0.75, 2.0, -1.0, None, None, "Small sample — interpret cautiously.")
    assert D.outcome_context(big, 5)["positive_return_frequency_pct"] == 75.0


# ---- report assembly -------------------------------------------------------------------------------------------

def report(view_obj=view(), watch=("NVDA", "AMD"), outcomes=None, patterns=PATTERNS, previous=None):
    syms = ["SNDK", "SHOP", "NVDA", "MU", "AMD"]
    return assemble(
        now=NOW, market=market("SUPPORTIVE"), market_block={"available": True, "conditions": "SUPPORTIVE",
                                                            "verdicts": {"market_trend": "Improving",
                                                                         "risk_level": "Normal"},
                                                            "next_event": None, "freshness": {"label": "FRESH"}},
        connection={"connected": view_obj is not None}, view=view_obj, watchlist_symbols=list(watch),
        metrics={s: tm(s) for s in syms}, events={s: bundle("LOW") for s in syms},
        research={s: research() for s in syms}, sector_of=lambda s: "Semiconductors", sector_pct={"Semiconductors": 0.5},
        concerns={}, sector_flags={}, patterns=patterns, outcomes=outcomes or {}, market_ref_time=REF,
        previous=previous, research_estimates={}, usage_counts={"last_hour": 0, "today": 0})


def test_report_groups_alphabetical_and_ownership():
    r = report()
    owned = [c["symbol"] for g in r["portfolio"]["groups"].values() for c in g]
    assert sorted(owned) == ["MU", "NVDA", "SHOP", "SNDK"]
    for g in r["portfolio"]["groups"].values():
        assert [c["symbol"] for c in g] == sorted(c["symbol"] for c in g)
    wl = [c for g in r["watchlist"]["groups"].values() for c in g]
    assert [c["symbol"] for c in wl] == ["AMD"] and wl[0]["owned"] is False        # watchlist not owned
    assert r["watchlist"]["also_owned"] == ["NVDA"]                                 # watchlist owned -> portfolio card


def test_robinhood_offline_ownership_unknown_and_portfolio_not_assumed_safe():
    r = report(view_obj=None, watch=("NVDA", "AMD"))
    assert r["portfolio"]["available"] is False and r["portfolio"]["groups"] is None
    wl = {c["symbol"]: c for g in r["watchlist"]["groups"].values() for c in g}
    assert set(wl) == {"NVDA", "AMD"} and all(c["owned"] is None for c in wl.values())
    assert all(c["details"]["layers"]["portfolio"] == "Unavailable" for c in wl.values())
    assert all(c["details"]["evidence"]["portfolio_fit"] == "Unavailable" for c in wl.values())
    assert "not assumed safe" in r["watchlist"]["note"]


def test_outcomes_and_patterns_do_not_alter_states():
    from services.analytics import GroupStats
    base = report(outcomes={}, patterns=None)
    ctx = report(outcomes={s: GroupStats(s, 40, -2.0, -1.5, 0.1, 1.0, -5.0, None, None, None)
                           for s in ["SNDK", "SHOP", "NVDA", "MU", "AMD"]}, patterns=PATTERNS)

    def states(r):
        return {c["symbol"]: (c["state"], c["why"]) for g in r["portfolio"]["groups"].values() for c in g}
    assert states(base) == states(ctx)


def test_changes_since_last_check_only_meaningful_fields():
    first = report()
    assert first["changes"] == []
    prev = copy.deepcopy(first["snapshot"])
    prev["MU"]["research_view"] = "MIXED / WAIT"
    prev["_market"]["conditions"] = "MIXED"
    again = report(previous=prev)
    assert {(c["subject"], c["field"], c["before"], c["after"]) for c in again["changes"]} == {
        ("MU", "Research", "Mixed / Wait", "Bullish Bias"), ("MARKET", "Market", "Mixed", "Supportive")}


def test_attention_priority_is_severity_ordered_not_profitability():
    owned = [{"symbol": "ZZZ", "state": D.RESEARCH, "next": "", "conflicts": []},
             {"symbol": "BBB", "state": D.PROTECT, "next": "x", "conflicts": []},
             {"symbol": "AAA", "state": D.EVENT_REVIEW, "next": "x", "conflicts": []},
             {"symbol": "CCC", "state": D.SUPPORTED, "next": "x",
              "conflicts": [{"a": "STOCK ATTRACTIVE", "b": "SECTOR CONCENTRATION ALREADY HIGH"}]}]
    items = D.attention_items(owned, [], {}, [{"subject": "Semiconductors", "value": "73.26"}],
                              {"need": ["ZZZ"], "total": 4})
    assert [(i["key"], i["symbol"]) for i in items] == [
        (D.EVENT_REVIEW, "AAA"), (D.PROTECT, "BBB"), ("sector_concentration", "Semiconductors"), ("conflict", "CCC"),
        ("research_coverage", "RESEARCH")]
    assert all(set(i) == {"tier", "order", "key", "symbol", "text", "level"} for i in items)
    # the per-stock sector conflict is not repeated when the portfolio-level sector item is present
    dup = [{"symbol": "MU", "state": D.SUPPORTED, "next": "x",
            "conflicts": [{"code": "stock_vs_sector", "a": "STOCK ATTRACTIVE", "b": "SECTOR CONCENTRATION ALREADY HIGH"}]}]
    keys = [i["key"] for i in D.attention_items(dup, [], {}, [{"subject": "Semiconductors", "value": "73.26"}], None)]
    assert keys == ["sector_concentration"]
    assert "pnl" not in inspect.getsource(D.attention_items)


# ---- research batch --------------------------------------------------------------------------------------------

def test_research_batch_respects_hourly_and_daily_limits():
    need = ["AMD", "SNDK", "AVGO", "NVDA"]
    est = {s: 3 for s in need}
    p = D.research_plan(need, est, calls_last_hour=config.AI_MAX_CALLS_PER_HOUR - 5, calls_today=0)
    assert p["analyze_now"] == ["AMD"] and p["remaining"] == ["SNDK", "AVGO", "NVDA"] and p["limited_by"] == "hourly AI call limit"
    p = D.research_plan(need, est, calls_last_hour=0, calls_today=config.AI_MAX_CALLS_PER_DAY - 6)
    assert p["analyze_now"] == ["AMD", "SNDK"] and p["limited_by"] == "daily AI call limit"
    p = D.research_plan(need, {**est, "AMD": 0}, calls_last_hour=0, calls_today=0)   # cached analyses cost nothing
    assert p["estimated_max_calls"] == 9 and p["analyze_now"] == need and p["remaining"] == []


def test_research_call_estimates_reuse_ai_cache():
    class Cache:
        def get_fresh(self, symbol, t, price, att):
            return object() if (symbol, t) in {("MU", "catalyst"), ("MU", "risk")} else None
    assert research_call_estimates(["MU", "NVDA", "X"], {"MU": tm("MU"), "NVDA": tm("NVDA")}, Cache()) == {
        "MU": 1, "NVDA": 3, "X": 3}


# ---- routes (fake gateway; no network; Claude never called on load) --------------------------------------------

@pytest.fixture
def client(monkeypatch):
    import agents.technical_agent as ta
    from api.routes import daily_review as dr
    from api.routes import portfolio as pr
    from api.routes import watchlist as wl
    from api.server import app
    from insights import service
    from pf_fixtures import FakeGatewayHttp, fake_provider
    from portfolio import explain as ex
    from test_market_insights import inputs
    http = FakeGatewayHttp()
    ai_calls = []
    for mod in (ta, ex):
        monkeypatch.setattr(mod, "get_provider", lambda: ai_calls.append(1))
    import insights.daily_explain as de
    monkeypatch.setattr(de, "get_provider", lambda: ai_calls.append(1))
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    monkeypatch.setattr(pr, "event_lookup", lambda s: bundle("LOW"))
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: inputs())
    monkeypatch.setattr(service, "symbols_metrics_fn", lambda syms: {s: tm(s) for s in syms})
    monkeypatch.setattr(service, "cached_patterns", lambda compute: PATTERNS)
    monkeypatch.setattr(wl, "get_cached_watchlist", lambda: (["NVDA", "AMD"], {}))
    monkeypatch.setattr(dr, "_outcomes", lambda syms: {})
    service._cache.clear()
    c = TestClient(app)
    c.http, c.ai_calls = http, ai_calls
    return c


def test_report_route_connected_and_no_ai_on_load(client):
    r = client.post("/api/insights/daily-review", json={}).json()
    assert r["portfolio"]["available"] is True and r["portfolio"]["count"] == 4
    assert r["watchlist"]["also_owned"] == ["NVDA"]
    assert r["portfolio"]["coverage"]["plan"]["hourly_limit"] == config.AI_MAX_CALLS_PER_HOUR
    assert client.ai_calls == []
    paths = {x["path"].split("?")[0] for x in client.http.requests}
    assert paths <= {"/portfolio", "/positions", "/realized-pnl", "/orders", "/accounts", "/pnl-history"}
    assert all(set(x["headers"]) == {"X-RH-Gateway-Secret"} for x in client.http.requests)


def test_slow_event_calendars_do_not_block_the_report(client, monkeypatch):
    import threading
    from api.routes import daily_review as dr
    from api.routes import portfolio as pr
    release = threading.Event()

    def slow(symbols):
        release.wait(5)
        return {s: bundle("LOW") for s in symbols}
    monkeypatch.setattr(pr, "_events_for", slow)
    monkeypatch.setattr(dr, "EVENTS_BUDGET_SECONDS", 0.6)
    monkeypatch.setattr(dr, "PORTFOLIO_EVENTS_BUDGET_SECONDS", 0.3)
    dr._events_inflight.clear()
    import time
    t0 = time.monotonic()
    try:
        r = client.post("/api/insights/daily-review", json={}).json()
    finally:
        release.set()
    assert time.monotonic() - t0 < 3                                                  # not blocked by slow calendars
    assert r["portfolio"]["available"] is True and r["portfolio"]["count"] == 4       # positions still shown
    owned = [c for g in r["portfolio"]["groups"].values() for c in g]
    assert all(c["details"]["evidence"]["event_risk"] == "Unavailable" for c in owned)
    assert "still loading" in r["events_note"]
    wl = [c for g in r["watchlist"]["groups"].values() for c in g]                # no Robinhood event level either
    assert wl and all(c["details"]["events"]["available"] is False for c in wl)
    assert all(c["details"]["evidence"]["event_risk"] == "Unavailable" for c in wl)          # unknown, never "Low"
    assert all(c["details"]["freshness"]["events"] == "UNKNOWN" for c in wl)
    assert all(any("Event data is unavailable" in x for x in c["details"]["all_cautions"]) for c in wl)


def test_report_route_offline(client, monkeypatch):
    import requests
    from api.routes import portfolio as pr
    from pf_fixtures import fake_provider

    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("refused")
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(Down()))
    r = client.post("/api/insights/daily-review", json={}).json()
    assert r["robinhood"]["connected"] is False and r["portfolio"]["available"] is False
    wl = [c for g in r["watchlist"]["groups"].values() for c in g]
    assert {c["symbol"] for c in wl} == {"NVDA", "AMD"} and all(c["owned"] is None for c in wl)


def test_feedback_is_not_accepted_by_the_backend_and_ui_never_sends_it(client):
    assert client.post("/api/insights/daily-review", json={"feedback": {"MU": "Added"}}).status_code == 422
    js = (ROOT / "frontend" / "daily_review.js").read_text(encoding="utf-8")
    fb = js[js.index("// ---- feedback"):js.index("// ---- end feedback")]
    assert "fetch(" not in fb and "post(" not in fb and "sessionStorage" not in fb and "localStorage" not in fb


def test_research_plan_route(client):
    p = client.post("/api/insights/daily-review/research-plan", json={"symbols": ["MU", "NVDA"]}).json()
    assert set(p) >= {"analyze_now", "remaining", "hourly_left", "daily_left", "estimated_max_calls"}
    assert client.post("/api/insights/daily-review/research-plan", json={"symbols": ["bad!"]}).status_code == 422
    assert client.ai_calls == []


def test_ai_explanation_grounded(client, monkeypatch):
    from agents import gating
    import insights.daily_explain as de
    monkeypatch.setattr(gating.ai_cache, "get_fresh", lambda *a, **k: None)
    monkeypatch.setattr(gating.ai_cache, "store", lambda *a, **k: None)
    monkeypatch.setattr(gating.usage_tracker, "can_call", lambda: (True, None))
    monkeypatch.setattr(gating.usage_tracker, "record", lambda *a, **k: None)

    class FakeLLM:
        def __init__(self, payload):
            self.payload = payload

        def analyze(self, prompt, system_prompt, max_tokens=1400):
            return SimpleNamespace(text=json.dumps(self.payload), model="fake", input_tokens=0, output_tokens=0,
                                   cache_creation_tokens=0, cache_read_tokens=0)
    good = {"what_changed": [], "stable_supportive": ["MU looks supported."], "needs_attention": ["Research is needed."],
            "watchlist_setups": [], "important_events": [], "monitor_next": ["Watch support."]}
    monkeypatch.setattr(de, "get_provider", lambda: FakeLLM(good))
    ok = client.post("/api/insights/daily-review/explain", json={}).json()
    assert ok["status"] == "OK" and ok["explanation"]["stable_supportive"] == ["MU looks supported."]
    bad = dict(good, needs_attention=["MU has a 83% chance to rise to $999."])
    monkeypatch.setattr(de, "get_provider", lambda: FakeLLM(bad))
    w = client.post("/api/insights/daily-review/explain", json={}).json()
    assert w["status"] == "WITHHELD" and w["explanation"] is None
    sell = dict(good, needs_attention=["You should sell AMD."])
    monkeypatch.setattr(de, "get_provider", lambda: FakeLLM(sell))
    assert client.post("/api/insights/daily-review/explain", json={}).json()["status"] == "WITHHELD"


# ---- safety ----------------------------------------------------------------------------------------------------

NEW_FILES = ["insights/decision.py", "insights/daily_review.py", "insights/daily_explain.py",
             "api/routes/daily_review.py", "frontend/daily_review.js", "frontend/daily_review.css"]


def test_no_execution_or_broker_mutation_in_new_code():
    for f in NEW_FILES:
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"TradingClient|place_?order|submit_?order|cancel_?order|OrderRequest|webhook|trigger\.trade|"
                             r"alpaca\.trading|/orders\"?,\s*\{\s*method", text, re.I), f
        assert not re.search(r"\bbuy now\b|\bsell now\b|\bbest stock\b|\btop pick\b|\bprobabilit", text, re.I), f


def test_no_execution_endpoints_in_app():
    from api.server import app
    paths = app.openapi()["paths"]      # app.routes holds unexpanded _IncludedRouter objects in this FastAPI version
    assert {p for p in paths if p.startswith("/api/insights/daily-review")} == {
        "/api/insights/daily-review", "/api/insights/daily-review/research-plan", "/api/insights/daily-review/explain"}
    from paper_endpoints import paper_exempt
    for path, ops in paths.items():
        if paper_exempt(path, ops):  # Stage 4.5: the exact local paper paths only
            continue
        assert not re.search(r"trade/execute|execute|broker|place|cancel|submit", path.replace("trade-review", "")), path
        if "order" in path:
            assert set(ops) == {"get"}, path                  # read-only order history (2.7C) only
        if path.startswith("/api/insights/daily-review"):
            assert set(ops) == {"post"}, path


def test_no_share_quantity_or_sizing_recommendations():
    out = json.dumps(to_jsonable(report()))
    assert not re.search(r"\b\d+(\.\d+)?\s+shares\b|target weight|sell \d|trim \d", out, re.I)
