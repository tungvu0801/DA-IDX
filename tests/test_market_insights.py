"""Stage 2.7E: current-market context (deterministic labels) + market explanation guards."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from e_fixtures import ev, tm
from insights.market import MarketInputs, build_market_insights, portfolio_exposure_from_view, with_freshness
from portfolio import explain as ex

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)


def basket(adv_pct=70, above20_pct=70, n=80, large=0):
    out = []
    for i in range(n):
        up = i < n * adv_pct / 100
        above = i < n * above20_pct / 100
        pct = (6.0 if i < large else 1.0) if up else -1.0
        out.append(SimpleNamespace(pct_change=pct, price=100.0, ema_medium=99.0 if above else 101.0,
                                   ema_slow=98.0 if above else 102.0))
    return out


def inputs(spy_trend="Bullish", spy_mom5=2.0, spy_pct=0.5, vol=15.0, vol_exp=1.0, qqq_trend="Bullish", qqq_pct=0.7,
           soxx_trend="Bullish", soxx_pct=1.5, breadth=None, events=None, news=None, spy=True, fetched=NOW):
    idx = {"SPY": tm("SPY", trend=spy_trend, momentum_5d_pct=spy_mom5, pct_change=spy_pct, volatility_pct=vol,
                     volatility_expansion=vol_exp, price=500.0) if spy else None,
           "QQQ": tm("QQQ", trend=qqq_trend, pct_change=qqq_pct, momentum_5d_pct=1.0, price=400.0),
           "SOXX": tm("SOXX", trend=soxx_trend, pct_change=soxx_pct, momentum_5d_pct=2.0 if soxx_trend == "Bullish"
                      else -2.0, price=200.0)}
    return MarketInputs(indices=idx, basket=breadth if breadth is not None else basket(), basket_requested=80,
                        scanner_counts={"scope": "movers", "possible_breakouts": 3, "large_drops": 2},
                        sector_etf_changes={"Semiconductors": soxx_pct, "Energy": -1.5, "Software": 0.1},
                        macro_events=events or [], news=news or [], fetched_at=fetched)


def build(**kw):
    exposure = kw.pop("exposure", None)
    return build_market_insights(inputs(**kw), NOW, exposure)


def test_supportive_market():
    m = build()
    assert (m["environment"], m["trend"], m["volatility"], m["breadth_label"]) == ("SUPPORTIVE", "IMPROVING", "NORMAL", "STRONG")
    assert m["risk_appetite"] == "RISK-ON" and m["difficulty"] == "MORE STABLE"
    assert any("Positive momentum" in d for d in m["drivers"]["positive"])


def test_mixed_market():
    m = build(spy_trend="Neutral", breadth=basket(50, 50))
    assert m["environment"] == "MIXED" and m["trend"] == "MIXED" and m["breadth_label"] == "MIXED"


def test_cautious_market():
    m = build(spy_trend="Bearish", spy_mom5=-2.0, spy_pct=-1.0, qqq_pct=-1.5, breadth=basket(30, 30))
    assert m["environment"] == "CAUTIOUS" and m["trend"] == "WEAKENING" and m["risk_appetite"] == "RISK-OFF"
    assert any("Weak breadth" in d for d in m["drivers"]["caution"])


def test_strong_index_but_weak_scanner_breadth_is_conflict_not_supportive():
    m = build(breadth=basket(30, 30))
    assert m["environment"] == "MIXED" and m["conflicting_signals"]
    assert "not the entire U.S. market" in m["breadth"]["scope"]


def test_weak_index_but_improving_breadth():
    m = build(spy_trend="Bearish", spy_mom5=-1.0, breadth=basket(75, 75))
    assert m["environment"] == "CAUTIOUS" and any("breadth list are rising" in c for c in m["conflicting_signals"])


def test_elevated_volatility():
    m = build(vol=31.0, breadth=basket(30, 30))
    assert m["volatility"] == "ELEVATED" and m["difficulty"] == "MORE CHALLENGING"
    assert "Volatility is elevated." in m["difficulty_factors"]


def test_major_event_within_24h_and_within_7d_and_none():
    from insights.events import event_item
    e24 = event_item(ev(hours=10, now=NOW), NOW)
    e7 = event_item(ev(hours=100, now=NOW), NOW)
    m24 = build(events=[e24])
    assert any("within 24 hours" in f for f in m24["difficulty_factors"])
    m7 = build(events=[e7])
    assert not any("within 24 hours" in f for f in m7["difficulty_factors"])
    assert any("Major macro event approaching" in d for d in m7["drivers"]["caution"])
    assert m7["events"][0]["why_it_matters"].startswith("Jobs data")
    m0 = build(events=[])
    assert m0["events"] == [] and not any("event" in d.lower() for d in m0["drivers"]["caution"])


def test_stale_and_missing_market_data():
    fresh = with_freshness(build(), NOW + timedelta(minutes=5))
    assert fresh["stale"] is False
    stale = with_freshness(build(), NOW + timedelta(hours=2))
    assert stale["stale"] is True and stale["stale_message"] == "MARKET FEEDBACK STALE — REFRESH REQUIRED"
    missing = build(spy=False)
    assert missing["available"] is False and "Current market trend unavailable" in missing["message"]


def test_portfolio_heavily_exposed_to_strong_and_weak_sector():
    exposure = {"sectors": [{"sector": "Semiconductors", "weight_pct": 73.26, "share_of_classified_pct": 100.0}],
                "unclassified_pct": 26.73}
    strong = build(exposure=exposure, soxx_pct=1.5)
    assert strong["strong_areas"][0]["sector"] == "Semiconductors"
    assert any("73.26%" in t and "+1.50%" in t for t in strong["for_your_portfolio"])
    weak = build(exposure=exposure, soxx_pct=-1.5, soxx_trend="Bearish")
    assert any(a["sector"] == "Semiconductors" for a in weak["weak_areas"])
    assert any("-1.50%" in t for t in weak["for_your_portfolio"])


def test_exposure_from_view_uses_configured_band():
    from e_fixtures import view
    exp = portfolio_exposure_from_view(view())
    assert exp["unclassified_pct"] == 75.6 and exp["sectors"] == []     # Semis 23.40% < 25% INFO band


def test_news_is_separate_from_market_data():
    news = [{"headline": "Chip stocks rally", "source": "Benzinga", "created_at": "2026-09-28", "url": "u",
             "summary": "s", "queried_symbol": "SPY"}]
    m = build(news=news)
    assert m["news"] == news and "does not prove why" in m["drivers"]["note"]
    assert build()["news"] == []


# ---- market explanation guards -------------------------------------------------------------------------------

class FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def analyze(self, prompt, system_prompt, max_tokens=1400):
        return SimpleNamespace(text=json.dumps(self.payload), model="fake", input_tokens=0, output_tokens=0,
                               cache_creation_tokens=0, cache_read_tokens=0)


@pytest.fixture(autouse=True)
def isolated_ai(monkeypatch):
    from agents import gating
    monkeypatch.setattr(gating.ai_cache, "get_fresh", lambda *a, **k: None)
    monkeypatch.setattr(gating.ai_cache, "store", lambda *a, **k: None)
    monkeypatch.setattr(gating.usage_tracker, "can_call", lambda: (True, None))
    monkeypatch.setattr(gating.usage_tracker, "record", lambda *a, **k: None)


def run_explain(payload, news=None):
    m = build(news=news)
    return ex.explain_market(m, provider_fn=lambda: FakeLLM(payload))


BASE = {"observed": ["SPY is +0.50% today."], "sourced_news": [], "interpretation": [], "why_it_matters": ["x"],
        "what_to_watch": ["Breadth."]}


def test_grounded_market_explanation_ok_and_sourced_catalyst_allowed():
    news = [{"headline": "Chip stocks rally", "source": "Benzinga", "created_at": "2026-09-28", "url": "u",
             "summary": "s", "queried_symbol": "SPY"}]
    out = run_explain({**BASE, "sourced_news": ["[1] Benzinga reports chip stocks rallied because of strong demand."]},
                      news)
    assert out["status"] == "OK"


@pytest.mark.parametrize("field,text,key", [
    ("observed", "Stocks are falling because investors fear a recession.", "unsupported_causal_claims"),
    ("interpretation", "The market is up due to AI optimism.", "unsupported_causal_claims"),
    ("interpretation", "The market will rise next week.", "forecast_language"),
    ("what_to_watch", "There is a 70% chance of a rally.", "forecast_language"),
    ("what_to_watch", "Buy semiconductor stocks now.", "forbidden_language"),
    ("observed", "SPY is +0.73% today.", "ungrounded_numbers"),
])
def test_market_explanation_withholds_unsupported_claims(field, text, key):
    out = run_explain({**BASE, field: [text]})
    assert out["status"] == "WITHHELD" and key in out
