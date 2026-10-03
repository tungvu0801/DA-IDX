"""Stage 2.7F: Beginner command center + simplified Trader Review (quick trade check). Presentation over existing facts."""
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from e_fixtures import bars, bundle, market, no_research, research, tm, view
from insights import home
from insights.plain import freshness, order_by_timeframe, research_freshness, TOPICS
from insights.quick_check import (build_quick_check, candidate_summary, changes_since, compare_snapshot,
                                  group_candidate, ladder, reason_feedback, tiles)
from insights.trade_review import EntryState, review_process, simple_review, trading_patterns
from portfolio.models import to_jsonable
from test_market_insights import build as build_market
from test_stock_check import check

D = Decimal
NOW = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)
FRONT = Path(__file__).resolve().parents[1] / "frontend"


def metrics():
    return {s: tm(s, price=p, prev_close=pc, pct_change=round((p - pc) / pc * 100, 2))
            for s, p, pc in [("NVDA", 222.0, 220.0), ("SNDK", 45.0, 46.0), ("SHOP", 30.6, 30.0), ("MU", 12.0, 11.9)]}


def mblock(**kw):
    return home.market_block(to_jsonable(build_market(**kw)), NOW)


# ---- connection (connected / down / returned) ------------------------------------------------------------------

def test_connection_status_connected_and_down():
    assert home.connection_status(None, None) == {"connected": True, "status": "CONNECTED"}
    down = home.connection_status("GATEWAY_DOWN", "connection refused 127.0.0.1:8787")
    assert down["connected"] is False and down["title"] == "ROBINHOOD NOT CONNECTED"
    assert "not currently running" in down["message"] and "8787" not in down["message"]      # beginner wording
    assert any("rh_gateway serve" in s for s in down["how_to_fix"])
    assert down["technical"]["detail"].startswith("connection refused")                     # kept, shown collapsed
    assert "keeps working" in down["note"]
    assert home.connection_status("SOMETHING_NEW", "x")["how_to_fix"] == ["Wait a minute and press Refresh."]


# ---- session change, contributors, vs market ------------------------------------------------------------------

def test_session_change_uses_shares_held_before_session_and_excludes_intraday_buys():
    ch = home.session_change(view(), metrics(), tm("SPY", pct_change=0.5))
    assert ch["change"] == D("3.10") and ch["pct"] == D("0.62") and ch["base"] == D("497.90")
    assert ch["excluded"] == [{"symbol": "SHOP", "reason": "bought during this session"}]
    assert "not available" in ch["basis"]                                       # never presented as Robinhood's "Today"
    other_day = dict(metrics(), MU=tm("MU", as_of=__import__("pandas").Timestamp("2026-09-24T20:00Z")))
    assert {"symbol": "MU", "reason": "different trading session than SPY"} in \
        home.session_change(view(), other_day, tm("SPY"))["excluded"]


def test_contributors_sorted_and_signed():
    c = home.contributors(home.session_change(view(), metrics(), tm("SPY")))
    assert [r["symbol"] for r in c["positive"]] == ["NVDA", "MU"] and [r["symbol"] for r in c["negative"]] == ["SNDK"]
    assert c["positive"][0]["contribution_pp"] == D("0.80")


def test_portfolio_vs_spy_difference_is_python_calculated_and_exposure_only():
    ch = home.session_change(view(), metrics(), tm("SPY"))
    idx = [{"symbol": "SPY", "available": True, "pct_change": 0.53}, {"symbol": "QQQ", "available": True, "pct_change": 0.45}]
    v = home.vs_market(ch, idx, view(), [{"sector": "Semiconductors", "etf": "SOXX", "pct_change": 1.23}])
    assert v["diff_vs_spy_pp"] == D("0.62") - D("0.53") and v["diff_vs_qqq_pp"] == D("0.17")
    text = " ".join(v["why_it_may_differ"])
    assert "Semiconductors" in text and "SNDK alone is 45.00%" in text
    assert not re.search(r"\b(because|caused|due to)\b", text, re.I)           # exposure, never a cause
    assert home.vs_market({**ch, "available": False}, idx, view(), [])["available"] is False


# ---- stock cards, research coverage, attention ------------------------------------------------------------------

def test_missing_and_stale_research_on_cards():
    rs = {"NVDA": {"available": True, "research_view": "BULLISH BIAS", "age_hours": 2.0},
          "SNDK": {"available": True, "research_view": "MIXED / WAIT", "age_hours": 60.0}}
    cards = {c["symbol"]: c for c in home.stock_cards(view(), metrics(), rs, {}, {"is_today": True})}
    assert cards["MU"]["research"]["freshness"] == "MISSING"
    assert cards["MU"]["sentence"] == "No fresh research saved — analyze MU."
    assert "research needed" in cards["MU"]["attention"]["reasons"]
    assert cards["SNDK"]["research"]["freshness"] == "STALE" and "analyze SNDK" in cards["SNDK"]["sentence"]
    assert cards["NVDA"]["research"]["freshness"] == "FRESH" and "Bullish Bias" in cards["NVDA"]["sentence"]
    assert "NVDA is up 0.91% today" in cards["NVDA"]["sentence"]
    assert list(cards) == ["SNDK", "SHOP", "NVDA", "MU"]                        # largest weight first


def test_research_coverage_counts_ai_calls_and_never_runs_them():
    cards = home.stock_cards(view(), metrics(), {}, {}, {"is_today": False})
    cov = home.research_coverage(cards, {"calls": 4, "daily_call_limit": 50, "calls_remaining_today": 46})
    assert cov["needs_research"] == ["SNDK", "SHOP", "NVDA", "MU"]
    assert cov["max_ai_calls"] == 4 * config.AI_CALLS_PER_RESEARCH_RUN == 12
    assert cov["calls_remaining_today"] == 46 and cov["hourly_limit"] == config.AI_MAX_CALLS_PER_HOUR
    assert "only when you click Analyze" in cov["note"]


def test_attention_levels():
    cards = {c["symbol"]: c for c in home.stock_cards(
        view(), dict(metrics(), NVDA=tm("NVDA", momentum_5d_pct=9.0, rsi=74.0)),
        {s: {"available": True, "research_view": "BULLISH BIAS", "age_hours": 1.0} for s in ("NVDA", "SNDK", "SHOP", "MU")},
        {"SNDK": {"position_severity": "HIGH"}}, {"is_today": True})}
    assert cards["SNDK"]["attention"]["level"] == "Higher attention"
    assert cards["NVDA"]["attention"]["level"] == "Watch" and "moved up quickly" in cards["NVDA"]["attention"]["reasons"]
    assert cards["NVDA"]["extended"] is True
    assert cards["SHOP"]["attention"]["level"] == "Normal"


# ---- market hero -------------------------------------------------------------------------------------------------

def test_market_block_verdicts_tiles_news_and_causation():
    news = [{"headline": f"H{i}", "source": "Benzinga", "created_at": "2026-09-27T20:00:00Z", "url": "u",
             "queried_symbol": "SPY"} for i in range(5)]
    m = mblock(news=news)
    assert m["available"] and set(m["verdicts"]) == {"market_trend", "trading_environment", "risk_level"}
    assert m["verdicts"]["market_trend"] in ("Improving", "Mixed", "Weakening")
    assert m["verdicts"]["trading_environment"] in ("More Stable", "Selective", "More Challenging")
    assert [t["symbol"] for t in m["tiles"]] == ["SPY", "QQQ", "SOXX"]
    assert len(m["news"]) == 3 and m["news"][0]["affected"] == "Broad market (SPY)"
    assert "does not prove causation" in m["causation_note"]
    assert "this app does not claim it caused" in m["news"][0]["why_it_may_matter"]
    assert m["big_picture"].startswith("The broad market trend is")
    assert m["freshness"]["label"] in ("FRESH", "AGING", "STALE")
    assert home.market_block({"available": False, "message": "x"}, NOW)["available"] is False


def test_risk_level_elevated_when_major_event_is_close():
    from e_fixtures import ev
    from insights.events import event_item
    from test_market_insights import NOW as MNOW
    m = mblock(events=[event_item(ev(hours=6.0, now=MNOW), MNOW)])
    assert m["verdicts"]["risk_level"] == "Elevated" and m["next_event"]["hours_until"] <= config.EVENT_HIGH_RISK_HOURS


def test_freshness_labels():
    assert freshness(10, 60, 120) == "FRESH" and freshness(90, 60, 120) == "AGING" and freshness(200, 60, 120) == "STALE"
    assert freshness(None, 60, 120) == "UNKNOWN"
    assert research_freshness(1) == "FRESH" and research_freshness(12) == "AGING" and research_freshness(30) == "STALE"


# ---- robinhood summary, daily feedback, pattern card --------------------------------------------------------------

def test_robinhood_summary_uses_reported_account_value():
    from portfolio.reconciliation import build_reconciliation
    v = view()
    rec = build_reconciliation(v, None, "UNAVAILABLE")
    ch = home.session_change(v, metrics(), tm("SPY"))
    s = home.robinhood_summary(v, rec, {"enabled": True, "flags": [{"severity": "HIGH"}]}, ch, {"label": "Today"}, NOW)
    assert s["account_value"] == rec["robinhood_portfolio_value"] and s["account_value_source"] == "Robinhood reported"
    assert s["largest_position"]["symbol"] == "SNDK" and s["largest_sector"]["sector"] == "Semiconductors"   # verified only
    assert s["attention"] == "HIGH" and s["session_change"] == D("3.10")


def test_daily_feedback_is_capped_and_deterministic():
    v = view()
    cards = home.stock_cards(v, metrics(), {}, {}, {"is_today": True})
    fb = home.daily_feedback(v, {"sectors": [{"sector": "Semiconductors", "etf": "SOXX", "pct_change": 1.2}],
                                 "events": [{"title": "Employment Situation", "date": "2026-10-02", "days_until": 4}]},
                             cards, home.session_change(v, metrics(), tm("SPY")), 10.0)
    assert 3 <= len(fb) <= 5 and fb == home.daily_feedback(v, {"sectors": [{"sector": "Semiconductors", "etf": "SOXX",
                                                                            "pct_change": 1.2}], "events": [
        {"title": "Employment Situation", "date": "2026-10-02", "days_until": 4}]}, cards,
        home.session_change(v, metrics(), tm("SPY")), 10.0)
    assert any("SNDK is your largest position" in x for x in fb)
    assert any("Employment Situation" in x for x in fb)


def test_pattern_card_only_when_verified():
    assert home.pattern_card(None) is None
    assert home.pattern_card({"available": False, "sample": 2}) is None
    card = home.pattern_card({"available": True, "patterns": [{"kind": "extended", "count": 26, "of": 49}]})
    assert card["text"] == "26 of 49 reviewed buys occurred after a fast rise."
    assert "does not mean those trades were automatically wrong" in card["explanation"]


def test_fit_together_three_layers():
    good = {"symbol": "NVDA", "trend": "Up", "weight": D("5.00"), "research": {"available": True, "view": "BULLISH BIAS",
            "freshness": "FRESH"}, "attention": {"level": "Normal", "reasons": []}}
    f = home.fit_together({"available": True, "conditions": "SUPPORTIVE"}, good)
    assert [c["layer"] for c in f["cards"]] == ["Market", "NVDA", "Your portfolio"] and "line up" in f["how_these_fit"]
    mixed = dict(good, attention={"level": "Higher attention", "reasons": ["large part of your account"]})
    f = home.fit_together({"available": True, "conditions": "SUPPORTIVE"}, mixed)
    assert f["how_these_fit"].startswith("The market and NVDA look supportive")          # ticker case kept
    assert "your portfolio calls for more care" in f["how_these_fit"]
    unknown = dict(good, trend="N/A", research={"available": False})
    assert "missing" in home.fit_together({"available": False}, unknown)["how_these_fit"]


# ---- quick trade check -----------------------------------------------------------------------------------------

def q(tf="days", reason=None, previous=None, **kw):
    return build_quick_check(check("MU", amount=500, **kw), timeframe=tf, reason=reason, previous=previous,
                             checked_at="2026-09-28T01:00:00+00:00")


def test_quick_check_shape_is_short():
    r = q()
    assert r["title"] == "MU — $500 TRADE CHECK"
    assert [t["title"] for t in r["tiles"]] == ["Market", "MU trend", "Entry", "Momentum", "Event risk", "Your portfolio"]
    for k in ("what_looks_good", "what_makes_me_cautious", "what_i_would_watch"):
        assert len(r[k]) <= 3
    assert "BUY" not in json.dumps(to_jsonable({k: v for k, v in r.items() if k != "details"})).upper().split('"')


def test_near_support_near_resistance_and_extended():
    near_s = q(m=tm("MU", price=10.6, support=10.5, resistance=13.5, dist_from_support_pct=0.95))
    assert near_s["ladder"]["location"] in ("Near support", "Mid-range")
    ext = q(m=tm("MU", momentum_5d_pct=9.0, rsi=74.0))
    entry = next(t for t in ext["tiles"] if t["title"] == "Entry")
    assert entry["value"] == "Extended (after a fast rise)" and entry["level"] == "bad"
    assert ext["chasing_risk"].startswith("CHASING RISK")
    assert q()["chasing_risk"] is None                                            # only when extended
    assert any("pulls back toward support" in w for w in check("MU", m=tm("MU", momentum_5d_pct=9.0, rsi=74.0))
               ["timing"]["watch_for"])


def test_watch_items_use_plain_words():
    from insights.plain import plain_watch
    assert plain_watch("Momentum improves (momentum score above 50 and a positive 5-day change).") == \
        "Momentum turns positive."
    assert plain_watch("Price breaks above resistance ($13.50) with volume above 2x.") == \
        "Price breaks above resistance ($13.50) with strong trading volume."
    assert plain_watch("The Research View improves (currently MIXED / WAIT).") == "The research view improves (now Mixed / Wait)."
    assert plain_watch("Price holds above support ($10.50).") == "Price holds above support ($10.50)."
    for tf in ("today", "days", "weeks", "long_term"):
        assert not any(re.search(r"score above|\d+(\.\d+)?x\b", w) for w in q(tf=tf, m=tm("MU", momentum_score=45))
                       ["what_i_would_watch"])


def test_ladder_positions():
    c = check("MU")
    c["price_area"].update(support=D("10"), resistance=D("14"), current_price=D("13"), location="MIDDLE_OF_RANGE")
    assert ladder(c)["position"] == 0.75 and ladder(c)["location"] == "Mid-range"
    c["price_area"].update(resistance=None, location="NO_RESISTANCE_ABOVE")
    assert ladder(c)["position"] == 1.0 and ladder(c)["location"] == "Above recent resistance"
    c["price_area"].update(current_price=None)
    assert ladder(c)["position"] is None


def test_event_warning_tile():
    from e_fixtures import ev
    r = q(events=bundle("HIGH", macro=[ev(hours=6.0)]))
    tile = next(t for t in r["tiles"] if t["title"] == "Event risk")
    assert tile["value"] == "High" and tile["level"] == "bad"


def test_stale_quote_is_surfaced_in_plain_words():
    r = q(tf="today")
    assert any("price quote is getting old" in x for x in r["what_makes_me_cautious"])     # MU quote is DEGRADED


def test_timeframe_changes_priority_only():
    today, long_term = q(tf="today"), q(tf="long_term")
    assert today["tiles"] == long_term["tiles"] and today["current_conditions"] == long_term["current_conditions"]
    lt_codes = [f for f in order_by_timeframe([{"code": "momentum_positive"}, {"code": "concentration_high"},
                                               {"code": "near_support"}], "long_term")]
    assert [f["code"] for f in lt_codes] == ["concentration_high"]                  # long term drops entry noise
    td = order_by_timeframe([{"code": "concentration_high"}, {"code": "near_support"}], "today")
    assert td[0]["code"] == "near_support"
    assert set(TOPICS.values()) >= {"price", "trend", "portfolio", "events"}


def test_reason_chip_feedback():
    c = check("MU")
    assert reason_feedback("pullback", c, []) == \
        "You selected Pullback, but the current price is not near the calculated support area."
    c2 = check("MU", m=tm("MU", price=10.6, dist_from_support_pct=0.9))
    c2["price_area"]["location"] = "NEAR_SUPPORT"
    assert "is near the calculated support area" in reason_feedback("pullback", c2, [])
    assert "no verified catalyst" in reason_feedback("news", c, [])
    assert "1 recent verified headline" in reason_feedback("news", c, [{"title": "t"}])
    assert reason_feedback(None, c, []) is None


def test_changes_since_last_check_shows_only_changed_fields():
    first = q()
    assert first["changes_since_last_check"] == []
    prev = dict(first["compare_snapshot"], price="11.00", momentum="WEAK")
    again = q(previous=prev)
    assert {c["field"] for c in again["changes_since_last_check"]} == {"Price", "Momentum"}
    assert changes_since(first["compare_snapshot"], first["compare_snapshot"]) == []


def test_same_fact_not_repeated():
    r = q(m=tm("MU", momentum_5d_pct=9.0, rsi=74.0), mk=market("SUPPORTIVE"))
    conc = [x for x in r["what_makes_me_cautious"] if "exposure" in x or "part of your account" in x]
    assert len(conc) <= 1


def test_new_money_grouping_is_not_a_ranking():
    c = check("MU", m=tm("MU", momentum_5d_pct=9.0, rsi=74.0))
    s = candidate_summary(c, True, "FRESH")
    assert s["group"] in ("CONDITIONS WORTH RESEARCHING", "MIXED — NEED MORE CONFIRMATION", "HIGHER-RISK CONDITIONS")
    assert s["price_location"] == "EXTENDED" and s["owned"] is True
    assert not re.search(r"\b(best|top pick|rank|score)\b", json.dumps(to_jsonable(s)), re.I)
    down = check("MU", m=tm("MU", trend="Bearish", ema_fast=10.0, ema_medium=11.0, ema_slow=12.0))
    assert group_candidate(down) in ("MIXED — NEED MORE CONFIRMATION", "HIGHER-RISK CONDITIONS")


# ---- after I traded / patterns ------------------------------------------------------------------------------------

def test_simple_review_sections_and_hindsight_is_separate():
    st = EntryState("RECONSTRUCTED", "2026-09-01", D("100"), trend="UPTREND", location="NEAR_RESISTANCE",
                    extended=True, market="IMPROVING")
    lot = {"open_date": "2026-09-01", "entry_price": D("100"), "entry_state": st, "review": review_process(st),
           "outcomes": [{"horizon_trading_days": 5, "status": "COMPLETED", "return_pct": 3.2, "mfe_pct": 5.1,
                         "mae_pct": -1.2}, {"horizon_trading_days": 20, "status": "PENDING"}]}
    s = simple_review(lot)
    assert s["how_it_started"][0] == "Bought on 2026-09-01 at $100.00."
    assert "The stock had already moved up quickly (after a fast rise)." in s["how_it_started"]
    assert any("fast rise" in x for x in s["what_to_learn"])
    assert s["what_happened"][0].startswith("5 trading days later: +3.20%") and "not reached yet" in s["what_happened"][1]
    one = simple_review(dict(lot, outcomes=[{"horizon_trading_days": 1, "status": "PENDING"}]))
    assert one["what_happened"] == ["1 trading day later: not reached yet."]
    assert "hindsight" in s["hindsight_label"]
    assert not any("%" in x for x in s["how_it_started"] + s["what_was_done_well"] + s["what_increased_risk"])


def test_pattern_counters_include_zero_counts_and_main_pattern():
    def e(**kw):
        return {"entry_state": EntryState("RECONSTRUCTED", None, D("1"), **kw), "outcomes": []}
    entries = [e(extended=True, location="MIDDLE_OF_RANGE") for _ in range(4)] + [e(location="NEAR_SUPPORT")]
    p = trading_patterns(entries)
    counters = {c["kind"]: (c["count"], c["of"]) for c in p["counters"]}
    assert counters == {"extended": (4, 5), "near_resistance": (0, 5), "near_support": (1, 5), "conflict": (0, 5)}
    assert p["main_pattern"]["text"] == "You frequently entered after a fast upward move."
    assert "not a judgement" in p["main_pattern_note"]


# ---- routes ------------------------------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    import agents.technical_agent as ta
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
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    monkeypatch.setattr(pr, "event_lookup", lambda s: SimpleNamespace(event_risk_level="LOW", nearest_event=None,
                        nearest_event_proximity=None, data_quality="MEDIUM", company_events=[], macro_events=[],
                        earnings_available=False))
    refreshed = []
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: (refreshed.append(now), inputs())[1])
    monkeypatch.setattr(service, "symbols_metrics_fn", lambda syms: {s: m for s, m in metrics().items() if s in syms})
    monkeypatch.setattr(service, "stock_data_fn", lambda s: (tm(s, price=110.0, support=105.0, resistance=130.0), None))
    monkeypatch.setattr(service, "cached_patterns", lambda compute: {
        "available": True, "patterns": [{"kind": "extended", "count": 26, "of": 49}]})
    monkeypatch.setattr(wl, "get_cached_watchlist", lambda: (["NVDA", "AMD"], {"NVDA": tm("NVDA"), "AMD": tm("AMD")}))
    service._cache.clear()
    c = TestClient(app)
    c.http, c.ai_calls, c.refreshed = http, ai_calls, refreshed
    return c


def test_home_route_connected_separates_holdings_and_watchlist(client):
    d = client.post("/api/insights/home", json={}).json()
    assert d["robinhood"]["connected"] is True and d["market"]["available"] is True
    assert [c["symbol"] for c in d["my_stocks"]] == ["SNDK", "SHOP", "NVDA", "MU"]
    assert d["watchlist"] == [
        {"symbol": "NVDA", "owned": True, "price": 12.0, "session_pct": 0.8, "signal": "WATCH"},
        {"symbol": "AMD", "owned": False, "price": 12.0, "session_pct": 0.8, "signal": "WATCH"}]
    assert d["pattern"]["text"].startswith("26 of 49")
    assert d["research_coverage"]["max_ai_calls"] == len(d["research_coverage"]["needs_research"]) * 3
    assert len(d["layers"]) == 4 and d["vs_market"]["available"] is True
    assert client.ai_calls == []                                                  # no AI on page load


def test_home_route_gateway_down_keeps_market_working(client, monkeypatch):
    import requests
    from api.routes import portfolio as pr
    from pf_fixtures import fake_provider

    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("refused")
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(Down()))
    d = client.post("/api/insights/home", json={}).json()
    assert d["robinhood"]["connected"] is False and d["robinhood"]["technical"]["reason"] == "GATEWAY_DOWN"
    assert d["market"]["available"] is True and "my_stocks" not in d
    assert all(w["owned"] is None for w in d["watchlist"])                        # unknown, never guessed
    pf = client.get("/api/portfolio").json()
    assert pf["status"] == "PORTFOLIO_UNAVAILABLE" and pf["connection"]["title"] == "ROBINHOOD NOT CONNECTED"


def test_home_route_rejects_bad_research_ids(client):
    assert client.post("/api/insights/home", json={"analysis_ids": {"MU": "not-hex!"}}).status_code == 422
    assert client.post("/api/insights/home", json={"place_order": True}).status_code == 422


def test_new_money_route_groups_alphabetically(client):
    d = client.post("/api/insights/new-money", json={"amount_usd": 500}).json()
    assert set(d["groups"]) == {"CONDITIONS WORTH RESEARCHING", "MIXED — NEED MORE CONFIRMATION", "HIGHER-RISK CONDITIONS"}
    allsyms = [c["symbol"] for g in d["groups"].values() for c in g]
    assert sorted(allsyms) == ["AMD", "MU", "NVDA", "SHOP", "SNDK"]
    for g in d["groups"].values():
        assert [c["symbol"] for c in g] == sorted(c["symbol"] for c in g)
    owned = {c["symbol"]: c["owned"] for g in d["groups"].values() for c in g}
    assert owned["AMD"] is False and owned["NVDA"] is True
    assert "not a recommendation or ranking" in d["note"] and client.ai_calls == []


def test_quick_route_refresh_is_deterministic_and_tracks_changes(client):
    body = {"symbol": "MU", "amount_usd": 500, "timeframe": "today", "reason": "pullback"}
    first = client.post("/api/trade-review/quick", json=body).json()
    assert first["status"] in ("OK", "STALE") and first["quick"]["title"] == "MU — $500 TRADE CHECK"
    n = len(client.refreshed)
    prev = dict(first["quick"]["compare_snapshot"], momentum="WEAK")
    again = client.post("/api/trade-review/quick", json={**body, "refresh": True, "previous": prev}).json()
    assert len(client.refreshed) == n + 1                                         # market data re-read
    assert [c["field"] for c in again["quick"]["changes_since_last_check"]] == ["Momentum"]
    assert client.ai_calls == []                                                  # refresh never calls Claude
    assert client.post("/api/trade-review/quick", json={**body, "timeframe": "forever"}).status_code == 422
    assert client.post("/api/trade-review/quick", json={**body, "side": "buy"}).status_code == 422


def test_position_route_has_simple_sections(client):
    d = client.get("/api/trade-review/position/NVDA").json()
    s = d["lots"][0]["simple"]
    assert set(s) >= {"how_it_started", "what_was_done_well", "what_increased_risk", "what_to_learn", "what_happened"}


def test_only_allowlisted_gateway_reads(client):
    client.post("/api/insights/home", json={})
    client.post("/api/insights/new-money", json={"amount_usd": 100})
    client.post("/api/trade-review/quick", json={"symbol": "NVDA", "amount_usd": 100})
    paths = {r["path"].split("?")[0] for r in client.http.requests}
    assert paths <= {"/portfolio", "/positions", "/realized-pnl", "/orders", "/accounts", "/pnl-history"} | \
        {p for p in paths if p.startswith("/tax-lots/")}


# ---- frontend guards ---------------------------------------------------------------------------------------------

NEW_FILES = ["command_center.js", "trader_review.js", "command_center.css"]


def test_frontend_has_no_execution_or_prediction_language():
    for name in NEW_FILES + ["portfolio_beginner.js", "market.js"]:
        text = (FRONT / name).read_text(encoding="utf-8")
        assert not re.search(r"place_?order|submit order|buy now|sell now|\bprobabilit|best stock|top pick|"
                             r"will (rise|fall|go up|go down)", text, re.I), name
        assert "method: \"DELETE\"" not in text and "/orders\", { method" not in text


def test_advanced_checklist_collapsed_and_mobile_styles():
    tr = (FRONT / "trader_review.js").read_text(encoding="utf-8")
    assert '<details class="qc-advanced" id="qc-advanced">' in tr                  # no `open` attribute
    assert "Refresh check" in tr and "What if I add" in tr and "Show technical details" in tr
    css = (FRONT / "command_center.css").read_text(encoding="utf-8")
    assert "@media (min-width: 720px)" in css
    assert re.search(r"\.qc-tiles \{[^}]*repeat\(2", css) and re.search(r"\.cc-stocks \{[^}]*grid-template-columns: 1fr;", css)
    html = (FRONT / "index.html").read_text(encoding="utf-8")
    assert html.index("command_center.js") < html.index("market.js")


def test_command_center_analyze_requires_confirmation_and_is_click_only():
    cc = (FRONT / "command_center.js").read_text(encoding="utf-8")
    # 2.7G.3: the per-card confirmation (with the planner's real estimate) lives in stock_result.js
    sr = (FRONT / "stock_result.js").read_text(encoding="utf-8")
    assert "window.StockResult.analyze(sym, host" in cc and "This may use up to ${esc(est)} new AI analysis call" in sr
    # 2.7G.2: "Analyze my holdings" now uses the 2.7G limit-aware planner (see tests/test_batch_27g2.py)
    assert "window.ResearchBatch.confirmHtml(cov)" in cc and "window.ResearchBatch.run(cov.need" in cc
    render = cc[cc.index("async function render(root, refresh)"):]
    assert "/research" not in render                                              # page load never runs research
