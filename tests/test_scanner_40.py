"""Stage 4.0 — deterministic, read-only Strategy Scanner (fit/scanner.py): one exact saved version against one list,
Strategy Fit's own evaluation per stock, one shared dependency plan. No database write, no AI, no broker, no network."""
import copy
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import fw_fixtures as FL
from fit import current as FC
from fit import readonly as RO
from fit import scanner as SC
from forward import capture as C
from forward import journal as J
from strategy.evaluate import group_met
from test_fit_34 import (FALSE, FALSE2, PARITY_ENTRY, QUIET, TRUE, TRUE2, UNAV, G, T, _parity_lab, _raw_version, by_name,
                         checkpoint, db_digest, fit, lab_with, spec)

ROOT = Path(__file__).resolve().parents[1]


def vid(lab, name):
    return next(v["strategy_version_id"] for v in RO.saved_versions(Path(lab.path), include_old=True) if v["strategy_name"] == name)


def scan(lab, name, source="SAVED_UNIVERSE", symbols=None, now=None, cache=None, **kw):
    now = now or FL.at(T)
    lab.market.now = now
    return SC.scan(vid(lab, name), source, symbols, now=now, path=lab.path, fetch_fn=lab.market.fetch, client=object(),
                   research_db=kw.pop("research_db", lambda: lab.research), events_fn=lab.events.build,
                   coverage_fn=lab.events.cov, cache=cache if cache is not None else FC.BarCache(), **kw)


def rows(r):
    return {x["symbol"]: x for x in r["results"]}


PARITY_KEYS = ("fit_status", "status_text", "group_result", "logic", "determinate", "conditions_met", "conditions_not_met",
               "conditions_evaluable", "conditions_total", "conditions_unavailable", "trace", "unavailable", "context_timing",
               "warnings")


def features(s):
    return [{k: v for k, v in f.items() if k != "captured_at"} for f in (s.get("snapshot") or {}).get("features", [])]


# ---- 51: STRATEGY FIT PARITY — every scanned stock equals an individual Stage 3.4 evaluation ---------------------------------

def _parity_world():
    basket = tuple(config.FALLBACK_UNIVERSE)
    lab = _parity_lab()
    lab.market = FL.Market(tuple(dict.fromkeys(("AMD", "MU", "NVDA", "SPY", "QQQ", "SOXX", "CLS", "KO", "XLP") + basket)), **QUIET)
    lab.save(spec(PARITY_ENTRY, symbols=("AMD", "MU", "NVDA", "KO"), name="Parity"))
    # other versions make Strategy Fit's merged plan larger (breadth list, sector ETFs, QQQ) than the scanner's
    lab.save(spec(G("ALL", {"feature": "market.environment", "op": "!=", "value": "CAUTIOUS"}), symbols=("AMD", "MU", "KO"), name="Env"))
    lab.save(spec(G("ALL", {"feature": "sector.etf_trend", "op": "!=", "value": "DOWNTREND"},
                    {"feature": "market.qqq_trend", "op": "!=", "value": "DOWNTREND"}), symbols=("AMD", "KO", "CLS"), name="Sector"))
    return lab


def test_51_scanner_rows_equal_individual_strategy_fit():
    lab = _parity_world()
    for name in ("Parity", "Env", "Sector"):
        r = scan(lab, name, "CUSTOM", ["AMD", "MU", "NVDA", "KO", "CLS"])
        assert r["decision_session"] == T.isoformat()
        for sym, row in rows(r).items():
            s = by_name(fit(lab, sym))[name]
            assert row["fit_status"] == s["fit_status"], (name, sym)
            if s["fit_status"] == FC.OUTSIDE_UNIVERSE:
                assert row["trace"] is None and row["snapshot"] is None
                continue
            assert {k: row[k] for k in PARITY_KEYS} == {k: s[k] for k in PARITY_KEYS}, (name, sym)
            assert features(row) == features(s), (name, sym)            # feature values, availability, timing, source
    statuses = {x["fit_status"] for n in ("Parity", "Env", "Sector") for x in scan(lab, n)["results"]}
    assert {FC.RULES_MET, FC.RULES_NOT_MET, FC.INCOMPLETE_DATA} & statuses


# ---- 52 / 53 / 54 / 55 / 56 / 57: statuses -------------------------------------------------------------------------------------

def test_52_rules_met_with_exact_values():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE, TRUE2), symbols=("AMD", "MU"), name="Met"))
    r = scan(lab, "Met")
    amd = rows(r)["AMD"]
    assert amd["fit_status"] == "RULES_MET" and amd["fit_label"] == "RULES MET" and amd["group"] == "RULES_MET"
    assert (amd["conditions_met"], amd["conditions_total"], amd["conditions_unavailable"]) == (2, 2, 0)
    s = by_name(fit(lab, "AMD"))["Met"]
    assert [t["actual"] for t in J._leaves(s["trace"])] == [t["actual"] for t in J._leaves(amd["trace"])] and amd["trace"] == s["trace"]
    assert amd["status_text"] == "The entry rules are met: all 2 conditions are met."


def test_53_rules_not_met_is_a_count_never_a_percentage():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE, TRUE2, {"feature": "stock.trend", "op": "in", "value": ["UPTREND", "MIXED", "DOWNTREND"]}, FALSE),
                  symbols=("MU",), name="Three of four"))
    mu = rows(scan(lab, "Three of four"))["MU"]
    assert mu["fit_status"] == "RULES_NOT_MET" and (mu["conditions_met"], mu["conditions_total"]) == (3, 4)
    assert mu["main_unmet"][0]["text"] == "close price is below $0" and "%" not in json.dumps({k: mu[k] for k in ("status_text", "main_unmet")})


def test_54_unknown_input_is_incomplete_not_false():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE, UNAV), symbols=("MU", "AMD"), name="Needs research"))
    lab.research.add("AMD", FL.ny(T, 10), view="BULLISH BIAS")
    got = rows(scan(lab, "Needs research"))
    assert got["MU"]["fit_status"] == "INCOMPLETE_DATA" and got["MU"]["determinate"] is False
    assert got["MU"]["main_unavailable"][0]["reason"].startswith("No saved research snapshot")
    assert got["AMD"]["fit_status"] == "RULES_MET"


def test_55_outside_universe_is_never_evaluated_or_fetched():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), symbols=("AMD", "MU", "CLS"), name="Three"))
    r = scan(lab, "Three", "CUSTOM", ["AMD", "MU", "CLS", "NVDA"])
    nv = rows(r)["NVDA"]
    assert nv["fit_status"] == "OUTSIDE_UNIVERSE" and nv["fit_label"] == "OUTSIDE UNIVERSE" and nv["trace"] is None
    assert "not in Three v1's saved universe" in nv["status_text"]
    assert all("NVDA" not in call for call in lab.market.calls) and r["plan"]["outside_universe_skipped"] == 1
    lab.market.calls.clear()
    none = scan(lab, "Three", "CUSTOM", ["NVDA", "KO"])
    assert none["status"] == "NOTHING_TO_EVALUATE" and lab.market.calls == [] and none["decision_session"] is None


def test_56_stale_symbol_is_stale_and_never_falls_back():
    lab = lab_with()
    del lab.market.rows["CLS"][T]                              # CLS has no bar for the decision session
    lab.save(spec(G("ALL", TRUE), symbols=("AMD", "MU", "CLS"), name="Stale"))
    got = rows(scan(lab, "Stale"))
    assert got["CLS"]["fit_status"] == "STALE_DATA" and got["CLS"]["trace"] is None and got["CLS"]["group"] == "DATA_ISSUES"
    assert "not evaluated on an older session" in got["CLS"]["status_text"]
    assert got["AMD"]["fit_status"] == got["MU"]["fit_status"] == "RULES_MET"
    s = by_name(fit(lab, "CLS"))["Stale"]
    assert s["fit_status"] == "STALE_DATA" and s["status_text"] == got["CLS"]["status_text"]


def test_57_nested_groups_follow_group_met_not_counts():
    entry = G("ANY", FALSE, FALSE2, G("ALL", TRUE, TRUE2))
    lab = lab_with()
    lab.save(spec(entry, symbols=("MU",), name="Nested"))
    mu = rows(scan(lab, "Nested"))["MU"]
    assert mu["fit_status"] == "RULES_MET" and (mu["conditions_met"], mu["conditions_total"]) == (2, 4)
    assert group_met(entry, {"stock.close": 1.0, "stock.rsi_14": 50.0})[0] is True and mu["group_result"] == "MET"
    lab2 = lab_with()
    lab2.save(spec(G("ALL", TRUE, TRUE2, G("ANY", FALSE, FALSE2)), symbols=("MU",), name="Mostly"))
    m2 = rows(scan(lab2, "Mostly"))["MU"]
    assert m2["fit_status"] == "RULES_NOT_MET" and (m2["conditions_met"], m2["conditions_total"]) == (2, 4)


def test_one_decision_session_for_every_evaluated_row():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), symbols=("AMD", "MU", "NVDA", "CLS"), name="Sync"))
    r = scan(lab, "Sync")
    sessions = {x["snapshot"]["session"] for x in r["results"] if x["snapshot"]}
    assert sessions == {T.isoformat()} == {r["decision_session"]}


# ---- 58 / 59: dependency planning — breadth once, or not at all ---------------------------------------------------------------

def test_58_breadth_is_loaded_and_calculated_once_for_20_symbols(monkeypatch):
    basket = tuple(config.FALLBACK_UNIVERSE)
    syms = basket[:20]
    lab = lab_with(FL.Market(tuple(dict.fromkeys(("SPY", "QQQ", "SOXX") + basket)), **QUIET))
    lab.save(spec(G("ALL", {"feature": "market.environment", "op": "!=", "value": "CAUTIOUS"}, TRUE), symbols=syms, name="Env"))
    import scanner.market_scanner as ms
    real, calls = ms.analyze_symbols, []
    monkeypatch.setattr(ms, "analyze_symbols", lambda c, s: calls.append(tuple(s)) or real(c, s))
    r = scan(lab, "Env")
    assert r["plan"]["breadth_list"] is True and r["plan"]["snapshots_built"] == 1 and r["plan"]["breadth_members"] == len(basket)
    assert len(lab.market.calls) == 1                                   # ONE batched market-data request
    assert sum(1 for c in calls if set(c) == set(basket)) == 1          # the breadth list analysed ONCE, not per symbol
    assert r["evaluated_count"] == 20 and any(w["code"] == "BREADTH_LIST_FIXED" for w in r["warnings"])


def test_59_no_breadth_list_when_no_rule_needs_it():
    basket = set(config.FALLBACK_UNIVERSE)
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE, {"feature": "market.trend", "op": "!=", "value": "WEAKENING"}), symbols=("AMD", "MU"), name="Plain"))
    r = scan(lab, "Plain")
    assert r["plan"]["breadth_list"] is False and r["plan"]["breadth_members"] is None
    assert not (set(lab.market.calls[0]) & (basket - {"AMD", "MU", "NVDA", "CLS", "SPY", "QQQ", "SOXX"}))


def test_sector_etfs_are_deduplicated_and_per_symbol():
    lab = lab_with(FL.Market(("AMD", "MU", "NVDA", "KO", "PEP", "CLS", "SPY", "QQQ", "SOXX", "XLP"), **QUIET))
    lab.save(spec(G("ALL", {"feature": "sector.etf_trend", "op": "in", "value": ["UPTREND", "MIXED", "DOWNTREND"]}),
                  symbols=("AMD", "MU", "NVDA", "KO", "PEP", "CLS"), name="Sectors"))
    r = scan(lab, "Sectors")
    assert r["plan"]["sector_etfs"] == ["SOXX", "XLP"] and len(lab.market.calls) == 1
    got = rows(r)
    assert got["CLS"]["fit_status"] == "INCOMPLETE_DATA" and got["CLS"]["unavailable"][0]["reason"] == "NO_SECTOR_MAPPING"
    assert {got[s]["fit_status"] for s in ("AMD", "MU", "NVDA", "KO", "PEP")} == {"RULES_MET"}


# ---- 60 / 61: research and events ---------------------------------------------------------------------------------------------

def test_60_saved_research_only_zero_claude_calls(monkeypatch):
    import agents.technical_agent as ta
    from ai_explain import service as AX
    calls = []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append(1))
    monkeypatch.setattr(AX, "get_provider", lambda: calls.append(1))
    syms = ("AMD", "MU", "NVDA", "CLS")
    lab = lab_with()
    lab.save(spec(G("ALL", {"feature": "research.view", "op": "==", "value": "BULLISH BIAS"}), symbols=syms, name="Research"))
    lab.research.add("AMD", FL.ny(T, 10), view="BULLISH BIAS")
    lab.research.add("MU", FL.ny(T, 19), view="BEARISH BIAS")
    reads = []
    orig = lab.research.list_snapshots
    lab.research.list_snapshots = lambda **kw: reads.append(kw) or orig(**kw)
    r = scan(lab, "Research")
    got = rows(r)
    assert got["AMD"]["fit_status"] == "RULES_MET" and got["MU"]["fit_status"] == "RULES_NOT_MET"
    assert got["MU"]["context_timing"] == "POST_CLOSE_CONTEXT" and any(w["code"] == "RESEARCH_AFTER_CLOSE" for w in got["MU"]["symbol_warnings"])
    for s in ("NVDA", "CLS"):
        assert got[s]["fit_status"] == "INCOMPLETE_DATA" and got[s]["unavailable"][0]["reason"] == "RESEARCH_UNAVAILABLE"
    assert calls == [] and r["data_freshness"]["research"]["symbols_with_saved_research"] == ["AMD", "MU"]
    assert len(reads) == 4 and all(x.get("limit") == 1 for x in reads)   # one latest-snapshot read per stock, none per rule


def test_61_events_keep_stage_34_semantics():
    lab = lab_with()
    lab.save(spec(G("ALL", {"feature": "event.risk_level", "op": "!=", "value": "HIGH"}), symbols=("AMD", "MU"), name="Not high"))
    lab.events.level = {"MU": "MEDIUM", "AMD": "HIGH"}                  # earnings calendar unavailable (fixture default)
    got = rows(scan(lab, "Not high"))
    assert got["MU"]["fit_status"] == "INCOMPLETE_DATA" and got["MU"]["unavailable"][0]["reason"] == "EVENT_DATA_INCOMPLETE"
    assert got["AMD"]["fit_status"] == "RULES_NOT_MET"                 # HIGH is certain even with a calendar missing
    assert sorted(lab.events.calls) == ["AMD", "MU"]                    # each stock once; no market-event rule, no market call
    macro = lab_with()
    macro.save(spec(G("ALL", {"feature": "market.major_event_within_24h", "op": "is_false"}), symbols=("AMD", "MU", "NVDA"), name="Macro"))
    scan(macro, "Macro")
    assert macro.events.calls == ["SPY"]                               # the market-level calendar resolved ONCE for the scan
    only = lab_with()
    only.save(spec(G("ALL", TRUE), symbols=("MU",), name="Price only"))
    scan(only, "Price only")
    assert only.events.calls == []


# ---- 62 / 63 / 64 / 65: list sources ----------------------------------------------------------------------------------------------

def test_62_watchlist_is_normalised_deduplicated_and_invalid_entries_reported():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), symbols=("AMD", "MU"), name="W"))
    r = scan(lab, "W", "WATCHLIST", watchlist_fn=lambda: ["amd", " MU", "AMD", "mu ", "bad symbol!", "NVDA"])
    assert r["requested_symbols"] == ["AMD", "MU", "NVDA"] and rows(r)["NVDA"]["fit_status"] == "OUTSIDE_UNIVERSE"
    assert r["warnings"][0]["code"] == "WATCHLIST_ENTRIES_IGNORED"
    with pytest.raises(SC.ScanError) as e:
        scan(lab, "W", "WATCHLIST", watchlist_fn=lambda: [])
    assert e.value.code == "EMPTY_LIST"


def test_63_holdings_unavailable_but_other_sources_work():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), symbols=("AMD", "MU"), name="H"))
    with pytest.raises(SC.ScanError) as e:
        scan(lab, "H", "HOLDINGS", holdings_fn=lambda: {"available": False, "symbols": [], "message": "gateway down"})
    assert e.value.code == "HOLDINGS_UNAVAILABLE" and "still work" in e.value.message and lab.market.calls == []
    assert scan(lab, "H")["evaluated_count"] == 2
    assert scan(lab, "H", "WATCHLIST", watchlist_fn=lambda: ["MU"])["evaluated_count"] == 1
    assert scan(lab, "H", "CUSTOM", ["amd"])["evaluated_count"] == 1
    held = scan(lab, "H", "HOLDINGS", holdings_fn=lambda: {"available": True, "symbols": ["mu", "KO"]})
    assert held["requested_symbols"] == ["KO", "MU"]


def test_64_custom_list_is_normalised_and_deduplicated():
    assert SC.normalise_symbols(["amd, MU", "AMD", "cls"]) == (["AMD", "CLS", "MU"], [])
    assert SC.normalise_symbols(["amd  mu;cls", "brk.b"]) == (["AMD", "BRK.B", "CLS", "MU"], [])
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), symbols=("AMD", "MU", "CLS"), name="C"))
    assert scan(lab, "C", "CUSTOM", ["amd, MU", "AMD", "cls"])["requested_symbols"] == ["AMD", "CLS", "MU"]
    lab.market.calls.clear()
    with pytest.raises(SC.ScanError) as e:
        scan(lab, "C", "CUSTOM", ["AMD", "not/a/ticker"])
    assert e.value.code == "INVALID_SYMBOL" and lab.market.calls == []


def test_65_more_than_the_limit_is_refused_never_truncated():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), symbols=("AMD",), name="Cap"))
    too_many = [f"T{i:03d}" for i in range(SC.MAX_SYMBOLS + 1)]
    with pytest.raises(SC.ScanError) as e:
        scan(lab, "Cap", "CUSTOM", too_many)
    assert e.value.code == "TOO_MANY_SYMBOLS" and e.value.extra == {"requested": 101, "limit": 100} and lab.market.calls == []
    assert scan(lab, "Cap", "CUSTOM", too_many[:100])["requested_count"] == 100


# ---- versions --------------------------------------------------------------------------------------------------------------------

def test_only_an_intact_saved_version_can_be_scanned():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), symbols=("AMD",), name="Ok"))
    with pytest.raises(SC.ScanError) as e:
        SC.scan(uuid.uuid4().hex, "SAVED_UNIVERSE", path=lab.path)
    assert e.value.code == "NOT_FOUND" and e.value.status == 404
    _raw_version(lab, spec(G("ALL", TRUE), symbols=("AMD",), name="Tampered"), spec_hash="f" * 64)
    _raw_version(lab, spec(G("ALL", TRUE), symbols=("AMD",), name="Mismatch"), fingerprint="0" * 64)
    lab.save(spec(G("ALL", {"feature": "portfolio.position_weight_pct", "op": "<", "value": 5}), symbols=("AMD",), name="Portfolio"))
    for name, code in (("Tampered", "INTEGRITY_ERROR"), ("Mismatch", "REGISTRY_MISMATCH"), ("Portfolio", "UNSUPPORTED")):
        with pytest.raises(SC.ScanError) as e:
            scan(lab, name)
        assert e.value.code == code and e.value.status == 409, name
    assert lab.market.calls == []
    cfg = SC.public_config(path=lab.path, watchlist_fn=lambda: [])
    assert {v["strategy_name"]: v["scannable"] for v in cfg["versions"]} == {"Ok": True, "Tampered": False, "Mismatch": False,
                                                                           "Portfolio": False}


def test_error_isolation_one_bad_symbol_does_not_fail_the_scan(monkeypatch):
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), symbols=("AMD", "MU", "NVDA"), name="Iso"))
    real = C.technical_snapshot

    def flaky(series, needs, T_):
        if "NVDA" in needs.universe:
            raise ValueError("broken bars")
        return real(series, needs, T_)
    monkeypatch.setattr(C, "technical_snapshot", flaky)
    r = scan(lab, "Iso")
    got = rows(r)
    assert got["NVDA"]["fit_status"] == "DATA_UNAVAILABLE" and "ValueError" in got["NVDA"]["status_text"]
    assert got["AMD"]["fit_status"] == got["MU"]["fit_status"] == "RULES_MET" and r["plan"]["per_symbol_fallback"] is True


def test_grouping_is_fixed_and_alphabetical_never_by_count():
    lab = lab_with()
    del lab.market.rows["CLS"][T]
    lab.save(spec(G("ANY", {"feature": "stock.rsi_14", "op": "<", "value": 50}, UNAV), symbols=("NVDA", "AMD", "MU", "CLS"), name="Mix"))
    r = scan(lab, "Mix", "CUSTOM", ["NVDA", "MU", "KO", "AMD", "CLS"])
    assert [x["symbol"] for x in r["results"]] == ["AMD", "CLS", "KO", "MU", "NVDA"]
    assert [g["group"] for g in r["groups"]] == ["RULES_MET", "RULES_NOT_MET", "INCOMPLETE_DATA", "DATA_ISSUES",
                                                 "OUTSIDE_UNIVERSE", "NOT_EVALUATED"]
    for g in r["groups"]:
        assert g["symbols"] == sorted(g["symbols"]) and g["count"] == len(g["symbols"])
    assert sum(g["count"] for g in r["groups"]) == 5 and r["groups"][4]["symbols"] == ["KO"] and r["groups"][3]["symbols"] == ["CLS"]


# ---- 66 / 67 / 68: read-only, no AI, no broker; API --------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from ai_explain import service as AX
    from api.routes import portfolio as pr
    from api.server import app
    from pf_fixtures import FakeGatewayHttp, fake_provider
    ai = []
    monkeypatch.setattr(ta, "get_provider", lambda: ai.append(1))
    monkeypatch.setattr(AX, "get_provider", lambda: ai.append(1))
    http = FakeGatewayHttp()
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    lab = lab_with()
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr("data.market_data.fetch_daily_bars", lab.market.fetch)
    monkeypatch.setattr("data.market_data.get_data_client", lambda: object())
    monkeypatch.setattr("scanner.watchlist.load_watchlist", lambda *a, **k: ["nvda", "AMD", "amd", "bad symbol!"])
    monkeypatch.setattr("database.database.get_db", lambda: lab.research)
    monkeypatch.setattr("services.event_context.build_event_context", lab.events.build)
    monkeypatch.setattr(C, "provider_coverage", lab.events.cov)
    monkeypatch.setattr(FC, "_utc", lambda now=None: FL.at(T))
    monkeypatch.setattr(FC, "BAR_CACHE", FC.BarCache())
    lab.market.now = FL.at(T)
    lab.save(spec(G("ALL", TRUE, TRUE2), symbols=("AMD", "MU"), name="Api best top score"))
    c = TestClient(app)
    c.lab, c.ai, c.http = lab, ai, http
    return c


def test_66_67_68_api_round_trip_writes_nothing_calls_no_ai_and_needs_no_broker(api, monkeypatch):
    lab = api.lab
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", False)                      # no broker for the first part
    checkpoint(lab.path)
    before = db_digest(lab.path)
    cfg = api.get("/api/strategy-scanner/config").json()
    v = next(x for x in cfg["versions"] if x["strategy_name"] == "Api best top score")
    assert v["scannable"] is True and v["universe"] == ["AMD", "MU"] and cfg["max_symbols"] == 100
    assert cfg["watchlist"] == {"symbols": ["AMD", "NVDA"], "ignored": 1} and cfg["holdings"]["enabled"] is False
    for body in ({"source": "SAVED_UNIVERSE"}, {"source": "WATCHLIST"}, {"source": "CUSTOM", "symbols": ["mu", "CLS"]}):
        for _ in range(3):
            r = api.post("/api/strategy-scanner/scan", json={"strategy_version_id": v["strategy_version_id"], **body})
            assert r.status_code == 200, r.text
    b = r.json()
    assert b["requested_symbols"] == ["CLS", "MU"] and rows(b)["MU"]["fit_status"] == "RULES_MET" and "serialization_s" in b["timings"]
    h = api.post("/api/strategy-scanner/scan", json={"strategy_version_id": v["strategy_version_id"], "source": "HOLDINGS"})
    assert h.status_code == 409 and h.json()["status"] == "HOLDINGS_UNAVAILABLE"            # portfolio awareness is off
    checkpoint(lab.path)
    assert db_digest(lab.path) == before and api.ai == [] and api.http.requests == []
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)                       # holdings discovery only
    hb = api.post("/api/strategy-scanner/scan", json={"strategy_version_id": v["strategy_version_id"], "source": "HOLDINGS"})
    assert hb.status_code == 200 and [q[0] if isinstance(q, tuple) else q for q in api.http.requests]
    assert all("positions" in str(q) for q in api.http.requests)                          # one read-only GET /positions
    assert db_digest(lab.path) == before


def test_api_bodies_are_strict_and_errors_are_clear(api):
    vid_ = vid(api.lab, "Api best top score")
    post = lambda b: api.post("/api/strategy-scanner/scan", json=b)  # noqa: E731
    assert post({"strategy_version_id": vid_, "source": "SAVED_UNIVERSE", "rank_by": "conditions"}).status_code == 422
    assert post({"strategy_version_id": vid_, "source": "EVERYTHING"}).status_code == 422
    assert post({"strategy_version_id": "xyz", "source": "SAVED_UNIVERSE"}).status_code == 422
    r = post({"strategy_version_id": vid_, "source": "WATCHLIST", "symbols": ["AMD"]})
    assert r.status_code == 422 and r.json()["status"] == "SYMBOLS_NOT_ACCEPTED"
    r = post({"strategy_version_id": vid_, "source": "CUSTOM", "symbols": [f"S{i}" for i in range(101)]})
    assert r.status_code == 422 and r.json()["status"] == "TOO_MANY_SYMBOLS" and api.lab.market.calls == []
    assert post({"strategy_version_id": uuid.uuid4().hex, "source": "SAVED_UNIVERSE"}).status_code == 404
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/strategy-scanner")}
    assert mine == {"/api/strategy-scanner/config": {"get"}, "/api/strategy-scanner/scan": {"post"}}


def test_69_no_ranking_or_advice_language_except_inside_user_text(api):
    vid_ = vid(api.lab, "Api best top score")
    body = api.post("/api/strategy-scanner/scan", json={"strategy_version_id": vid_, "source": "SAVED_UNIVERSE"}).json()
    text = json.dumps(body).replace("Api best top score", "")
    bad = re.compile(r"(?i)\b(best|top|strongest|recommend\w*|score|probability|confidence|buy|sell|trade now|entry signal)\b")
    assert not bad.findall(text), bad.findall(text)
    for f in ("fit/scanner.py", "api/routes/strategy_scanner.py", "frontend/strategy_scanner.js"):          # CSS has no words
        src = (ROOT / f).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith(("//", "#", "*", '"""')))
        assert not bad.findall(code), (f, bad.findall(code))


def test_82_scanner_code_has_no_orders_ai_scheduler_execution_or_sql_writes():
    for f in ("fit/scanner.py", "api/routes/strategy_scanner.py", "frontend/strategy_scanner.js"):
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|new Function|pickle|importlib", text), f
        assert not re.search(r"TradingClient|alpaca\.trading|place_?order|submit_?order|cancel_?order|/orders|anthropic|"
                             r"get_provider|from agents|import agents|from portfolio|import portfolio|rh_gateway|setInterval|"
                             r"setTimeout|apscheduler|BackgroundScheduler|add_job|threading\.Timer|/api/ai-explain", text, re.I), f
        if f.endswith(".py"):
            assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\s+(INTO|TABLE|FROM|INDEX|TRIGGER|\w+\s+SET)", text), f


def test_scanner_is_a_strategy_lab_workspace_with_race_guards():
    js = (ROOT / "frontend" / "strategy_scanner.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'addWorkspace({ id: "scanner", label: "Scanner"' in js
    assert html.index("evidence.js") < html.index("strategy_scanner.js") < html.index("ai_history.js")
    assert "new AbortController()" in js and "my !== seq" in js and "/api/strategy-scanner/scan" in js
    assert "Previous scan — refresh to update" in js and "No ranking · No orders · No AI during scan" in js
    assert "Current quote — not used in scanner rule evaluation" in js or "quote" not in js.lower()
    assert "StrategyFit.openFor(" in js and js.count("fetch(") == 2
