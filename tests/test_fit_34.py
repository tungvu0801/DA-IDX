"""Stage 3.4: deterministic Strategy Fit (saved versions only; latest completed close; group_met semantics; stored
evidence kept separate; read-only — no database writes, no orders, no broker, no AI, no network)."""
import copy
import hashlib
import json
import re
import sqlite3
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bt_fixtures as X
import config
import fw_fixtures as FL
from backtest import runs as R
from backtest.store import BacktestStore
from fit import current as FC
from fit import evidence as E
from fit import readonly as RO
from forward import capture as C
from forward import journal as J
from forward.store import ForwardStore
from strategy import spec as S
from strategy.evaluate import group_met
from strategy.store import StrategyStore

ROOT = Path(__file__).resolve().parents[1]
D = date.fromisoformat
T = D("2026-09-25")                       # a Friday; FL.at(T) = Saturday 12:00 UTC -> T is the latest completed session
QUIET = {"vol": 0.004}
TRUE = {"feature": "stock.close", "op": ">", "value": 0}
TRUE2 = {"feature": "stock.rsi_14", "op": "between", "value": [0, 100]}
FALSE = {"feature": "stock.close", "op": "<", "value": 0}
FALSE2 = {"feature": "stock.rsi_14", "op": ">", "value": 100}
UNAV = {"feature": "research.view", "op": "==", "value": "BULLISH BIAS"}      # no saved research -> UNAVAILABLE
TREND_UP = {"feature": "stock.trend", "op": "in", "value": ["UPTREND", "MIXED", "DOWNTREND"]}   # always true when computed
FROZEN_MODULES = "02d5c400eaebc53469e2873c65c7ca0ef837ec800d61f3c52d5c34505426e9da"   # Stage 3.1-3.3 files (re-pinned in Stage 3.6 without the 3 files it extends)


def G(logic, *conds):
    return {"logic": logic, "conditions": list(conds)}


def spec(entry, symbols=("MU",), name="Fit test", exit_=None):
    s = X.spec(symbols=symbols, entry=entry, exit_=exit_, name=name)
    n, err = S.validate(s)
    assert not err, err
    return n


def lab_with(market=None):
    return FL.FLab(market or FL.Market(("AMD", "MU", "NVDA", "SPY", "QQQ", "SOXX", "CLS"), **QUIET))


def fit(lab, sym, old=False, now=None, cache=None, **kw):
    now = now or FL.at(T)
    lab.market.now = now
    return FC.evaluate(sym, old, now=now, path=lab.path, fetch_fn=lab.market.fetch, client=object(),
                       research_db=kw.pop("research_db", lambda: lab.research), events_fn=lab.events.build,
                       coverage_fn=lab.events.cov, cache=cache if cache is not None else FC.BarCache(), **kw)


def by_name(r):
    return {s["strategy_name"]: s for s in r["strategies"]}


def stable(r):
    """A result without wall-clock / timing fields (for identity checks)."""
    r = copy.deepcopy(r)
    r.pop("timings", None)
    r.pop("evaluated_at", None)
    for s in r["strategies"]:
        if s.get("snapshot"):
            s["snapshot"].pop("captured_at", None)
    if r.get("environment"):
        r["environment"].pop("captured_at", None)
    if r.get("data_freshness"):
        r["data_freshness"]["market"].pop("sources", None)
        r["data_freshness"]["market"].pop("fetched", None)
    return r


def evaluation(r):
    """What the rules saw and decided (no ids, no timestamps) — comparable across databases."""
    keep = ("fit_status", "group_result", "logic", "determinate", "conditions_met", "conditions_not_met", "conditions_unavailable",
            "conditions_total", "trace", "unavailable", "context_timing", "status_text")
    return [{**{k: s[k] for k in keep}, "features": [{k: v for k, v in f.items() if k != "captured_at"}
                                                      for f in (s["snapshot"] or {}).get("features", [])]}
            for s in r["strategies"]]


def db_digest(path) -> str:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for name, sql in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name"):
            h.update(f"{name}|{sql}".encode())
            if sql and sql.upper().startswith("CREATE TABLE"):
                cols = ", ".join(f'"{r[1]}"' for r in conn.execute(f'PRAGMA table_info("{name}")'))
                for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY {cols}'):
                    h.update(repr(row).encode())
        return h.hexdigest()
    finally:
        conn.close()


def checkpoint(path):
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()


# ---- 58: EXACT EVALUATOR PARITY with the Stage 3.3 journal (same version, stock, session) ------------------------------

PARITY_ENTRY = G("ALL", {"feature": "stock.trend", "op": "!=", "value": "DOWNTREND"},
                 G("ANY", {"feature": "stock.rsi_14", "op": "<", "value": 55}, {"feature": "stock.price_location", "op": "in",
                                                                               "value": ["NEAR_SUPPORT", "MIDDLE_OF_RANGE"]}),
                 {"feature": "market.trend", "op": "!=", "value": "WEAKENING"},
                 {"feature": "research.view", "op": "in", "value": ["BULLISH BIAS", "MIXED / WAIT"]},
                 {"feature": "event.risk_level", "op": "!=", "value": "HIGH"})


def _parity_lab():
    lab = lab_with()
    lab.research.add("MU", FL.ny(T, 10))                    # saved BEFORE the close (AT_CLOSE)
    lab.research.add("AMD", FL.ny(T, 19), view="MIXED / WAIT")   # saved AFTER the close (POST_CLOSE)
    lab.events.earnings = True                               # complete calendars: event risk is available
    lab.events.level = {"MU": "LOW", "AMD": "HIGH"}
    return lab


def test_58_trace_and_values_equal_the_stage_33_journal():
    sp = spec(PARITY_ENTRY, symbols=("AMD", "MU", "NVDA"), name="Parity")
    jlab = _parity_lab()
    jid = jlab.journal(sp, FL.ny(T - timedelta(days=1), 12))["journal_id"]
    assert jlab.record(jid, FL.at(T))["status"] == "RECORDED"
    obs = jlab.obs(jid, T)
    for mode in ("same_db_stage_32_cache", "independent_db_fetch"):
        flab = jlab if mode == "same_db_stage_32_cache" else _parity_lab()
        if flab is not jlab:
            flab.save(sp)
        for sym in ("AMD", "MU", "NVDA"):
            r = fit(flab, sym)
            s = by_name(r)["Parity"]
            o = obs[sym]
            assert r["decision_session"] == T.isoformat()
            assert s["trace"] == o["evaluation_trace"]["trace"], (mode, sym)
            assert (s["group_result"], s["logic"], s["determinate"]) == (o["evaluation_trace"]["result"], o["evaluation_trace"]["logic"],
                                                                         o["evaluation_trace"]["determinate"])
            assert (s["conditions_met"], s["conditions_total"]) == (o["rules_met"], o["rules_total"])
            jf = {f["feature_id"]: f for f in o["feature_snapshot"]["features"]}
            for f in s["snapshot"]["features"]:
                j = jf[f["feature_id"]]
                assert {k: f[k] for k in ("value", "availability", "reason", "timing", "source_timestamp", "unit")} == \
                       {k: j[k] for k in ("value", "availability", "reason", "timing", "source_timestamp", "unit")}, (mode, sym, f["feature_id"])
            decided = {"ENTER": "RULES_MET", "HOLD": "RULES_NOT_MET"}.get(o["decision"])
            if o["decision"] == "SKIP":
                decided = "INCOMPLETE_DATA"
            assert s["fit_status"] == decided, (mode, sym, o["decision"], s["fit_status"])
            src = r["data_freshness"]["market"]["sources"][sym]["source"]
            assert src == ("STAGE_3_2_CACHE" if mode == "same_db_stage_32_cache" else "FETCHED_NOW")
    # the POST_CLOSE research for AMD is labelled as such, never as if it existed at the close
    amd = by_name(fit(jlab, "AMD"))["Parity"]
    assert {f["feature_id"]: f["timing"] for f in amd["snapshot"]["features"]}["research.view"] == "POST_CLOSE"
    assert amd["context_timing"] == "POST_CLOSE_CONTEXT"


# ---- 59: the LATEST COMPLETED close — never today's unfinished bar -------------------------------------------------------

def test_59_uses_the_completed_close_not_the_unfinished_bar():
    lab = lab_with()
    lab.save(spec(G("ALL", {"feature": "stock.change_1d_pct", "op": ">", "value": -50}, TREND_UP), name="Close"))
    today = D("2026-09-28")                                   # Monday, market open: its bar exists at the provider
    during = FL.ny(today, 11, 15)
    after_close_same_day = FL.ny(today, 20, 0)
    a = fit(lab, "MU", now=during)
    assert a["decision_session"] == T.isoformat()             # Friday — Monday is not complete in New York yet
    lab.market.set_bar("MU", today, 1.0, 900.0, 0.5, 850.0, 9e9)       # radically mutate the unfinished bar
    lab.market.set_bar("SPY", today, 1.0, 900.0, 0.5, 850.0, 9e9)
    b = fit(lab, "MU", now=during)
    c = fit(lab, "MU", now=after_close_same_day)              # after 16:00 but the New York date has not ended
    assert c["decision_session"] == T.isoformat()
    for x in (b, c):
        assert [s["trace"] for s in x["strategies"]] == [s["trace"] for s in a["strategies"]]
        assert [s["snapshot"]["features"] for s in x["strategies"]] == [s["snapshot"]["features"] for s in a["strategies"]]
        assert [s["fit_status"] for s in x["strategies"]] == [s["fit_status"] for s in a["strategies"]]
    assert all(f["source_timestamp"] == C.utc_iso(C.close_utc(T)) for f in a["strategies"][0]["snapshot"]["features"])


# ---- 60: FUTURE LEAK — bars after T never change the evaluation at T ------------------------------------------------------

def test_60_mutating_every_bar_after_T_changes_nothing():
    lab = lab_with()
    lab.save(spec(PARITY_ENTRY, name="Leak"))
    lab.research.add("MU", FL.ny(T, 9))
    a = fit(lab, "MU")
    lab.market.mutate_after(T)
    b = fit(lab, "MU")
    assert stable(a)["strategies"] == stable(b)["strategies"] and a["decision_session"] == b["decision_session"]
    assert a["plan"]["replay"]["violations"] == 0 and b["plan"]["replay"]["violations"] == 0
    # a Stage 3.2 cached dataset that already holds LATER bars (fetched later) is replayed only through T
    c_lab = lab_with()
    c_lab.save(spec(PARITY_ENTRY, name="Leak"))
    c_lab.research.add("MU", FL.ny(T, 9))
    for s in ("MU", "SPY"):
        rows = [c_lab.market.rows[s][d] for d in sorted(c_lab.market.rows[s])]
        c_lab.cache(s, rows, requested_start="2025-01-01", requested_end="2026-12-31")
    c_lab.market.mutate_after(T)
    for s in ("MU", "SPY"):                                    # a second, mutated cache would be newest: prove it is used
        rows = [c_lab.market.rows[s][d] for d in sorted(c_lab.market.rows[s])]
        c_lab.cache(s, rows, requested_start="2025-01-01", requested_end="2026-12-31", fetched_at="2026-12-31T00:00:00+00:00")
    c = fit(c_lab, "MU")
    assert c["data_freshness"]["market"]["sources"]["MU"]["source"] == "STAGE_3_2_CACHE" and c_lab.market.calls == []
    assert evaluation(c) == evaluation(a)


# ---- 61: OUTSIDE UNIVERSE --------------------------------------------------------------------------------------------------

def test_61_outside_universe_is_not_evaluated_and_requests_nothing():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), symbols=("AMD", "MU", "CLS"), name="Semis"))
    r = fit(lab, "NVDA")
    s = by_name(r)["Semis"]
    assert s["fit_status"] == "OUTSIDE_UNIVERSE" and s["fit_label"] == "OUTSIDE STRATEGY UNIVERSE"
    assert all(s[k] is None for k in ("conditions_met", "conditions_total", "conditions_unavailable", "conditions_evaluable",
                                      "group_result", "trace", "snapshot", "historical_evidence", "forward_evidence"))
    assert r["status"] == "NOTHING_TO_EVALUATE" and r["decision_session"] is None and lab.market.calls == []
    assert r["counts"]["OUTSIDE_UNIVERSE"] == 1 and "not in this version's saved universe" in s["status_text"]
    lab.save(spec(G("ALL", TRUE), symbols=("NVDA",), name="Nvidia only"))
    r2 = fit(lab, "NVDA")
    assert {n: x["fit_status"] for n, x in by_name(r2).items()} == {"Semis": "OUTSIDE_UNIVERSE", "Nvidia only": "RULES_MET"}
    assert r2["plan"]["symbols"] == ["NVDA", "SPY"]


# ---- 62-65: UNAVAILABLE + NESTED semantics come from group_met, never from the counts --------------------------------------

CASES = {  # name: (entry, fit status, met, not_met, unavailable, total)
    "62 ALL true true unavailable": (G("ALL", TRUE, TRUE2, UNAV), "INCOMPLETE_DATA", 2, 0, 1, 3),
    "63 ANY true unavailable": (G("ANY", TRUE, UNAV), "RULES_MET", 1, 0, 1, 2),
    "64 ANY false unavailable": (G("ANY", FALSE, UNAV), "INCOMPLETE_DATA", 0, 1, 1, 2),
    "ALL false unavailable": (G("ALL", FALSE, UNAV), "RULES_NOT_MET", 0, 1, 1, 2),
    "ANY 1 of 3": (G("ANY", FALSE, FALSE2, TRUE), "RULES_MET", 1, 2, 0, 3),
    "ALL 3 of 4": (G("ALL", TRUE, TRUE2, TREND_UP, FALSE), "RULES_NOT_MET", 3, 1, 0, 4),
    "65 nested ALL(T, ANY(F, T))": (G("ALL", TRUE, G("ANY", FALSE, TRUE2)), "RULES_MET", 2, 1, 0, 3),
    "65 nested ALL(T, ANY(F, U))": (G("ALL", TRUE, G("ANY", FALSE, UNAV)), "INCOMPLETE_DATA", 1, 1, 1, 3),
    "65 nested ANY(ALL(T, F), ALL(T, T))": (G("ANY", G("ALL", TRUE, FALSE), G("ALL", TRUE2, TREND_UP)), "RULES_MET", 3, 1, 0, 4),
    "65 nested ANY(ALL(T, U), F)": (G("ANY", G("ALL", TRUE, UNAV), FALSE), "INCOMPLETE_DATA", 1, 1, 1, 3),
    "65 nested ALL(ANY(F, F), T)": (G("ALL", G("ANY", FALSE, FALSE2), TRUE), "RULES_NOT_MET", 1, 2, 0, 3),
    "65 nested ANY(ALL(F, U), ALL(T, T))": (G("ANY", G("ALL", FALSE, UNAV), G("ALL", TRUE, TRUE2)), "RULES_MET", 2, 1, 1, 4),
}


def test_62_to_65_unavailable_and_nested_group_semantics():
    lab = lab_with()
    for name, (entry, *_rest) in CASES.items():
        lab.save(spec(entry, name=name))
    r = fit(lab, "MU")
    got = by_name(r)
    for name, (entry, status, met, not_met, unav, total) in CASES.items():
        s = got[name]
        assert (s["fit_status"], s["conditions_met"], s["conditions_not_met"], s["conditions_unavailable"], s["conditions_total"]) == \
               (status, met, not_met, unav, total), name
        assert s["conditions_evaluable"] == met + not_met
        values = {f["feature_id"]: f["value"] for f in s["snapshot"]["features"]}
        ok, _ = group_met(entry, values)                        # the overall result is exactly group_met's
        assert (s["group_result"] == "MET") is ok, name
        assert s["fit_status"] != "RULES_NOT_MET" or not any(u["reason"] == "RESEARCH_UNAVAILABLE" for u in s["unavailable"]) \
            or s["determinate"], name
    assert got["62 ALL true true unavailable"]["unavailable"][0]["reason"] == "RESEARCH_UNAVAILABLE"
    assert "not the same as the rules not matching" in got["62 ALL true true unavailable"]["status_text"]
    assert got["62 ALL true true unavailable"]["context_timing"] == "INCOMPLETE_CONTEXT"
    assert r["plan"]["snapshots_built"] == 1 and r["context_timing"] == "INCOMPLETE_CONTEXT"


# ---- 66: RESEARCH — saved snapshots only, read once, 0 Claude calls -----------------------------------------------------------

def test_66_research_saved_only_shared_and_timed(monkeypatch):
    import agents.technical_agent as ta
    calls = []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append(1))
    lab = lab_with()
    for i, view in enumerate(("BULLISH BIAS", "BEARISH BIAS", "MIXED / WAIT")):
        lab.save(spec(G("ALL", {"feature": "research.view", "op": "==", "value": view}), name=f"Research {i}"))
    reads = []
    orig = lab.research.list_snapshots
    lab.research.list_snapshots = lambda **kw: reads.append(kw) or orig(**kw)
    none = fit(lab, "MU")                                      # B: no saved research
    assert {s["fit_status"] for s in none["strategies"]} == {"INCOMPLETE_DATA"}
    assert {u["reason"] for s in none["strategies"] for u in s["unavailable"]} == {"RESEARCH_UNAVAILABLE"}
    assert any(w["code"] == "RESEARCH_UNAVAILABLE" for w in none["warnings"]) and len(reads) == 1   # read ONCE, not per version
    lab.research.add("MU", FL.ny(T, 10), view="BEARISH BIAS")  # A: saved before the close
    a = fit(lab, "MU")
    assert {n: s["fit_status"] for n, s in by_name(a).items()} == {"Research 0": "RULES_NOT_MET", "Research 1": "RULES_MET",
                                                                    "Research 2": "RULES_NOT_MET"}
    assert a["context_timing"] == "STRICT_CLOSE_CONTEXT" and a["data_freshness"]["research"]["existed_at_close"] is True
    lab.research.add("MU", FL.ny(T, 17), view="MIXED / WAIT", sid=2)     # newer snapshot, saved AFTER the close
    b = fit(lab, "MU")
    assert by_name(b)["Research 2"]["fit_status"] == "RULES_MET" and b["context_timing"] == "POST_CLOSE_CONTEXT"
    assert any(w["code"] == "RESEARCH_AFTER_CLOSE" for w in b["warnings"])
    assert calls == [] and len(reads) == 3


# ---- 67: EVENTS — Stage 3.3 semantics; incomplete never becomes LOW / FALSE / safe ------------------------------------------

def test_67_events_withheld_when_incomplete_and_resolved_once():
    lab = lab_with()
    lab.save(spec(G("ALL", {"feature": "event.risk_level", "op": "in", "value": ["LOW", "NONE"]}), name="Calm events"))
    lab.save(spec(G("ALL", {"feature": "event.risk_level", "op": "!=", "value": "HIGH"}), name="Not high"))
    lab.save(spec(G("ALL", {"feature": "market.major_event_within_24h", "op": "is_false"}), name="No macro"))
    lab.save(spec(G("ALL", TRUE), name="Price only"))
    lab.events.level = {"MU": "MEDIUM"}                        # earnings calendar unavailable (fixture default)
    r = fit(lab, "MU")
    got = by_name(r)
    for n in ("Calm events", "Not high"):
        assert got[n]["fit_status"] == "INCOMPLETE_DATA" and got[n]["unavailable"][0]["reason"] == "EVENT_DATA_INCOMPLETE"
        assert "never treated as low risk" in got[n]["unavailable"][0]["reason_text"]
    assert got["No macro"]["fit_status"] == "RULES_MET"       # FRED + FOMC complete and no macro event: a real FALSE
    assert got["Price only"]["fit_status"] == "RULES_MET"
    assert lab.events.calls == ["MU", "SPY"]                   # resolved ONCE per evaluation, not per version
    assert any(w["code"] == "EARNINGS_CALENDAR_UNAVAILABLE" for w in r["warnings"])
    lab.events.level = {"MU": "HIGH"}                          # HIGH is certain even with a calendar missing
    assert by_name(fit(lab, "MU"))["Not high"]["fit_status"] == "RULES_NOT_MET"
    lab.events.raise_for = {"MU"}
    lab.events.coverage = {**FL.COMPLETE, "fomc": False}
    got = by_name(fit(lab, "MU"))
    assert got["Not high"]["unavailable"][0]["reason"] == "EVENT_DATA_UNAVAILABLE"
    assert got["No macro"]["fit_status"] == "INCOMPLETE_DATA"  # macro calendar incomplete: FALSE cannot be confirmed
    only = lab_with()
    only.save(spec(G("ALL", TRUE), name="Price only"))
    fit(only, "MU")
    assert only.events.calls == []                             # no event rule -> no event call


# ---- 68: SHARED features — one environment; identical values across versions -----------------------------------------------

def test_68_three_versions_share_one_feature_environment(monkeypatch):
    lab = lab_with()
    lab.save(spec(G("ALL", {"feature": "stock.trend", "op": "==", "value": "UPTREND"}), name="A"))
    lab.save(spec(G("ANY", {"feature": "stock.trend", "op": "!=", "value": "DOWNTREND"}, FALSE), name="B"))
    lab.save(spec(G("ALL", {"feature": "stock.trend", "op": "in", "value": ["UPTREND", "MIXED"]}, TRUE2), name="C"))
    built = []
    real = C.technical_snapshot
    monkeypatch.setattr(C, "technical_snapshot", lambda *a: built.append(a[2]) or real(*a))
    r = fit(lab, "MU")
    cells = [json.dumps(next(f for f in s["snapshot"]["features"] if f["feature_id"] == "stock.trend"), sort_keys=True)
             for s in r["strategies"]]
    assert len(set(cells)) == 1 and len(cells) == 3            # byte-identical value for the same feature and session
    actual = {json.dumps(t["actual"]) for s in r["strategies"] for t in J._leaves(s["trace"]) if t["feature"] == "stock.trend"}
    assert len(actual) == 1
    assert built == [T] and len(lab.market.calls) == 1         # ONE snapshot and ONE market-data request for all three


# ---- 69 / 70: EVIDENCE — exact version joins; runs never combined ----------------------------------------------------------

def _cached_lab():
    lab = lab_with(FL.Market(("AMD", "MU", "NVDA", "SPY"), start=date(2024, 11, 1), **QUIET))
    for s in ("AMD", "MU", "NVDA", "SPY"):
        rows = [lab.market.rows[s][d] for d in sorted(lab.market.rows[s]) if d <= T]
        lab.cache(s, rows, requested_start="2024-11-01", requested_end="2026-09-25")
    return lab


def test_69_evidence_is_joined_by_exact_version(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = _cached_lab()
    v1 = spec(G("ALL", {"feature": "stock.trend", "op": "==", "value": "UPTREND"}), symbols=("AMD", "MU"), name="Iso")
    sid = lab.save(v1)["strategy_id"]
    lab.strategies.add_version(sid, spec(G("ALL", TRUE), symbols=("AMD", "MU"), name="Iso"))
    other = lab.save(spec(G("ALL", TRUE), symbols=("MU",), name="Other"))["strategy_id"]
    r1 = R.start_run(lab.store, X.body(sid, 1, start="2026-03-02", end="2026-09-25"), now=X.NOW)["run_id"]
    r3 = R.start_run(lab.store, X.body(other, 1, start="2026-03-02", end="2026-09-25"), now=X.NOW)["run_id"]
    jid = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(T - timedelta(days=1), 12))["journal_id"]
    lab.record(jid, FL.at(T))
    r = fit(lab, "MU", old=True)
    v = {(s["strategy_name"], s["version"]): s for s in r["strategies"]}
    assert [x["run_id"] for x in v[("Iso", 1)]["historical_evidence"]["runs"]] == [r1]
    assert v[("Iso", 1)]["forward_evidence"]["journal"]["journal_id"] == jid
    assert v[("Iso", 1)]["forward_evidence"]["symbol"]["latest_session"] == T.isoformat()
    assert v[("Iso", 2)]["historical_evidence"]["count"] == 0 and v[("Iso", 2)]["forward_evidence"]["journal"] is None
    assert [x["run_id"] for x in v[("Other", 1)]["historical_evidence"]["runs"]] == [r3]
    assert v[("Other", 1)]["forward_evidence"]["journals"] == 0
    assert v[("Iso", 1)]["strategy_version_id"] != v[("Iso", 2)]["strategy_version_id"]
    assert v[("Iso", 1)]["historical_evidence"]["runs"][0]["spec_hash_matches"] is True


def test_70_multiple_backtests_stay_separate(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = _cached_lab()
    sid = lab.save(spec(G("ALL", {"feature": "stock.trend", "op": "==", "value": "UPTREND"}), symbols=("AMD", "MU", "NVDA"),
                        name="Multi"))["strategy_id"]
    ra = R.start_run(lab.store, X.body(sid, start="2025-06-02", end="2026-09-25", slippage_bps_per_side=3.0), now=X.NOW)["run_id"]
    rb = R.start_run(lab.store, X.body(sid, start="2026-01-05", end="2026-09-25", slippage_bps_per_side=5.0), now=X.NOW)["run_id"]
    h = by_name(fit(lab, "MU"))["Multi"]["historical_evidence"]
    assert set(h) == {"available", "count", "completed", "runs", "most_recent_completed_run_id", "note"}   # nothing combined
    assert h["count"] == 2 and h["most_recent_completed_run_id"] == rb
    runs = {x["run_id"]: x for x in h["runs"]}
    for rid, bps, start in ((ra, 3.0, "2025-06-02"), (rb, 5.0, "2026-01-05")):
        stored = lab.store.get_run(rid)["result"]["metrics"]
        assert runs[rid]["slippage_bps_per_side"] == bps and runs[rid]["period"]["start"] == start
        assert runs[rid]["metrics"]["total_return_pct"] == stored["total_return_pct"]
        assert runs[rid]["metrics"]["closed_trades"] == stored["closed_trades"]


def test_backtest_and_journal_evidence_never_change_the_fit(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = _cached_lab()
    ready = G("ALL", {"feature": "stock.trend", "op": "!=", "value": "DOWNTREND"}, {"feature": "stock.rsi_14", "op": "<", "value": 80})
    sid = lab.save(spec(ready, symbols=("AMD", "MU"), name="Same"))["strategy_id"]
    before = stable(fit(lab, "MU"))
    assert R.start_run(lab.store, X.body(sid, start="2026-03-02", end="2026-09-25"), now=X.NOW)["status"] in ("PENDING", "RUNNING", "COMPLETED")
    jid = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(T - timedelta(days=1), 12))["journal_id"]
    lab.record(jid, FL.at(T))
    after = stable(fit(lab, "MU"))
    for k in ("fit_status", "trace", "snapshot", "conditions_met", "group_result", "context_timing", "status_text"):
        assert before["strategies"][0][k] == after["strategies"][0][k], k
    ev = after["strategies"][0]
    assert ev["historical_evidence"]["completed"] == 1 and ev["forward_evidence"]["journal"] is not None


def test_open_forward_shadow_state_is_reported_separately_from_entry_fit():
    m = FL.Market(("AMD", "MU", "NVDA", "SPY"), **QUIET)
    pc = m.rows["MU"][D("2026-09-24")][5]
    m.set_bar("MU", D("2026-09-25"), pc, pc * 1.06, pc * 0.99, pc * 1.05)    # +5 % on Friday -> ENTER at Friday's close
    lab = FL.FLab(m)
    sp = spec(G("ALL", {"feature": "stock.change_1d_pct", "op": ">", "value": 3}), symbols=("MU",), name="Jump",
              exit_={"logic": "ANY", "conditions": [], "invalidation": None, "target": None, "max_holding_days": 5})
    jid = lab.journal(sp, FL.ny(T - timedelta(days=1), 12))["journal_id"]
    lab.record(jid, FL.at(T))
    lab.record(jid, FL.at(D("2026-09-28")))                    # Monday: the reference entry fills -> OPEN
    r = fit(lab, "MU", now=FL.at(D("2026-09-28")))
    s = by_name(r)["Jump"]
    fe = s["forward_evidence"]["symbol"]
    assert fe["state_after"] == "OPEN" and fe["open_cycle"]["entry_fill_session"] == "2026-09-28"
    assert s["fit_status"] in ("RULES_MET", "RULES_NOT_MET")  # entry fit is computed normally, independent of the journal
    assert s["trace"][0]["feature"] == "stock.change_1d_pct"


# ---- 71: NO DATABASE WRITE -------------------------------------------------------------------------------------------------

def test_71_strategy_fit_writes_nothing(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = _cached_lab()
    sid = lab.save(spec(PARITY_ENTRY, symbols=("AMD", "MU"), name="Writes"))["strategy_id"]
    ready = lab.save(spec(G("ALL", TRUE2), symbols=("AMD", "MU"), name="Ready"))["strategy_id"]
    lab.save(spec(G("ALL", {"feature": "market.environment", "op": "!=", "value": "CAUTIOUS"}), symbols=("NVDA",), name="Env"))
    R.start_run(lab.store, X.body(ready, start="2026-03-02", end="2026-09-25"), now=X.NOW)
    jid = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(T - timedelta(days=1), 12))["journal_id"]
    lab.record(jid, FL.at(T))
    lab.research.add("MU", FL.ny(T, 10))
    checkpoint(lab.path)
    file0, dig0 = hashlib.sha256(Path(lab.path).read_bytes()).hexdigest(), db_digest(lab.path)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a writable store was opened"))  # noqa: E731
    monkeypatch.setattr(BacktestStore, "__init__", boom)
    monkeypatch.setattr(ForwardStore, "__init__", boom)
    monkeypatch.setattr(StrategyStore, "__init__", boom)
    cache = FC.BarCache()
    for sym, old in (("MU", False), ("MU", True), ("AMD", False), ("NVDA", False), ("XOM", False), ("MU", False)):
        fit(lab, sym, old=old, cache=cache)
    FC.public_config(lab.path)
    assert hashlib.sha256(Path(lab.path).read_bytes()).hexdigest() == file0 and db_digest(lab.path) == dig0
    with RO.ReadOnlyBacktestStore(lab.path)._connect() as conn, pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM backtest_runs")
    with RO.ReadOnlyForwardStore(lab.path)._connect() as conn, pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("UPDATE forward_test_journals SET status = 'ARCHIVED'")


def test_database_without_stage_32_33_tables_is_read_not_migrated():
    path = Path(tempfile.mkdtemp(prefix="fit34-")) / "only31.db"
    StrategyStore(path).create(spec(G("ALL", TRUE), name="Only 3.1"))
    before = db_digest(path)
    m = FL.Market(("MU", "SPY"), **QUIET)
    m.now = FL.at(T)
    r = FC.evaluate("MU", now=FL.at(T), path=path, fetch_fn=m.fetch, client=object(), cache=FC.BarCache())
    s = r["strategies"][0]
    assert s["fit_status"] == "RULES_MET" and s["historical_evidence"]["available"] is False
    assert s["forward_evidence"]["available"] is False and db_digest(path) == before
    tables = {r[0] for r in sqlite3.connect(str(path)).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not any(t.startswith(("backtest_", "historical_", "forward_test_")) for t in tables)


# ---- 74: BREADTH only when needed, and once ---------------------------------------------------------------------------------

def test_74_breadth_list_only_when_needed_and_once():
    basket = tuple(config.FALLBACK_UNIVERSE)
    lab = lab_with(FL.Market(("AMD", "MU", "NVDA", "SPY", "QQQ", "SOXX") + basket, **QUIET))
    lab.save(spec(G("ALL", TRUE), symbols=("MU", "AMD"), name="Plain"))
    lab.save(spec(G("ALL", {"feature": "market.trend", "op": "!=", "value": "WEAKENING"}), symbols=("MU", "AMD"), name="Trend"))
    cache = FC.BarCache()
    a = fit(lab, "MU", cache=cache)
    assert a["plan"]["breadth_list"] is False and not (set(lab.market.calls[0]) & (set(basket) - {"MU", "AMD", "NVDA"}))
    assert a["plan"]["breadth_members"] is None
    lab.save(spec(G("ALL", {"feature": "market.environment", "op": "!=", "value": "CAUTIOUS"}), symbols=("MU", "AMD"), name="Env"))
    lab.market.calls.clear()
    b = fit(lab, "MU", cache=cache)
    assert b["plan"]["breadth_list"] is True and b["plan"]["breadth_members"] == len(basket)
    assert len(lab.market.calls) == 1 and set(basket) - {"MU"} <= set(lab.market.calls[0])   # ONE batched request (MU cached)
    assert b["plan"]["snapshots_built"] == 1 and by_name(b)["Env"]["fit_status"] in FC.EVALUATED
    lab.market.calls.clear()
    assert "AMD" in basket
    c = fit(lab, "AMD", cache=cache)                           # another symbol: breadth list (incl. AMD) reused from memory
    assert lab.market.calls == [] and c["plan"]["breadth_list"] is True and c["plan"]["snapshots_built"] == 1


def test_data_sources_stage_32_cache_then_memory_then_one_request():
    lab = _cached_lab()
    lab.save(spec(G("ALL", TRUE), symbols=("MU", "AMD", "NVDA"), name="Src"))
    r = fit(lab, "MU")
    assert lab.market.calls == [] and r["data_freshness"]["market"]["sources"]["MU"]["source"] == "STAGE_3_2_CACHE"
    fresh = lab_with()
    fresh.save(spec(G("ALL", TRUE), symbols=("MU", "AMD"), name="Src"))
    cache = FC.BarCache()
    r1 = fit(fresh, "MU", cache=cache)
    r2 = fit(fresh, "MU", cache=cache)
    assert fresh.market.calls == [("MU", "SPY")] and r1["data_freshness"]["market"]["fetched"]["requests"] == 1
    assert r2["data_freshness"]["market"]["sources"]["MU"]["source"] == "MEMORY_CACHE"
    assert stable(r1)["strategies"] == stable(r2)["strategies"]


def test_bulk_cache_read_matches_stage_32_rule_verifies_hashes_and_reuses_series(monkeypatch):
    lab = _cached_lab()
    for s, fetched, req_end in (("MU", "2026-09-27T00:00:00+00:00", "2026-09-25"), ("MU", "2026-09-20T00:00:00+00:00", "2026-09-25"),
                                ("AMD", "2026-09-30T00:00:00+00:00", "2026-09-18"), ("NVDA", "2026-09-27T00:00:00+00:00", "2026-09-25")):
        rows = [lab.market.rows[s][d] for d in sorted(lab.market.rows[s]) if d <= D(req_end)]
        lab.cache(s, rows, requested_start="2025-01-01", requested_end=req_end, fetched_at=fetched)
    ro = RO.ReadOnlyBacktestStore(lab.path)
    start, end = C.need_range(T, None)
    bulk = FC.covering_datasets(ro, ["AMD", "MU", "NVDA", "SPY", "ZZZZ"], start, end)
    for sym in ("AMD", "MU", "NVDA", "SPY", "ZZZZ"):         # the same dataset Stage 3.2 itself would choose (newest wins)
        one = lab.store.covering_dataset(sym, FC.B.feed(), FC.B.ADJUSTMENT, start.isoformat(), end.isoformat())
        assert (bulk.get(sym) or {}).get("dataset_id") == (one or {}).get("dataset_id"), sym
    lab.save(spec(G("ALL", TRUE), symbols=("MU",), name="Cache"))
    reads = []
    real = FC.dataset_rows_many
    monkeypatch.setattr(FC, "dataset_rows_many", lambda b, d: reads.append(sorted(d)) or real(b, d))
    cache = FC.BarCache()
    a, b = fit(lab, "MU", cache=cache), fit(lab, "MU", cache=cache)
    assert reads == [["MU", "SPY"]] and evaluation(a) == evaluation(b)      # verified + parsed once, then reused
    with sqlite3.connect(lab.path) as c:                     # tamper with a cached bar behind the triggers' back
        c.execute("DROP TRIGGER IF EXISTS historical_daily_bars_no_update")
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'historical_daily_bars'").fetchall():
            c.execute(f'DROP TRIGGER "{name}"')
        ds = bulk["MU"]["dataset_id"]
        c.execute("UPDATE historical_daily_bars SET close = close * 2 WHERE dataset_id = ? AND session_date = ?", (ds, T.isoformat()))
    r = fit(lab, "MU")                                        # a fresh cache re-verifies: never evaluated on tampered bars
    assert r["status"] == "DATA_UNAVAILABLE" and "no longer match their content hash" in r["message"]
    assert by_name(r)["Cache"]["fit_status"] == "DATA_UNAVAILABLE" and lab.market.calls == []


# ---- STALE / UNAVAILABLE data: never an older session ---------------------------------------------------------------------

def test_stale_and_unavailable_data_are_reported_not_replaced():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), symbols=("MU", "AMD", "CLS", "ZZZZ"), name="Data"))
    lab.save(spec(G("ALL", TRUE), symbols=("NVDA",), name="Elsewhere"))
    lab.market.drop("MU", T)
    s = by_name(fit(lab, "MU"))["Data"]
    assert s["fit_status"] == "STALE_DATA" and "2026-09-24" in s["status_text"] and s["trace"] is None
    assert by_name(fit(lab, "ZZZZ"))["Data"]["fit_status"] == "DATA_UNAVAILABLE"          # no bars for that ticker
    gap = lab_with()
    gap.save(spec(G("ALL", TRUE), symbols=("AMD",), name="Gap"))
    gap.market.drop("SPY", T)
    g = fit(gap, "AMD")                                         # SPY lacks Friday but AMD has it: SPY's bar is missing
    assert g["status"] == "DATA_UNAVAILABLE" and g["decision_session"] is None
    assert by_name(g)["Gap"]["fit_status"] == "DATA_UNAVAILABLE"
    hole = lab_with()
    hole.save(spec(G("ALL", TRUE), symbols=("AMD",), name="Hole"))
    for d in (T, T - timedelta(days=1)):
        for sym in ("SPY", "AMD"):
            hole.market.drop(sym, d)
    assert fit(hole, "AMD")["status"] == "DATA_UNAVAILABLE"   # two weekdays without SPY: never Wednesday instead
    hol = lab_with()
    hol.save(spec(G("ALL", TRUE), symbols=("AMD",), name="Holiday"))
    for sym in hol.market.rows:
        hol.market.drop(sym, T)
    h = fit(hol, "AMD")                                         # nobody has Friday: a market holiday, stated explicitly
    assert h["decision_session"] == "2026-09-24" and any(w["code"] == "SESSION_ASSUMED_HOLIDAY" for w in h["warnings"])
    down = lab_with()
    down.save(spec(G("ALL", TRUE), symbols=("MU",), name="Down"))
    down.save(spec(G("ALL", TRUE), symbols=("AMD",), name="Other"))
    down.market.fail = True
    d = fit(down, "MU")
    assert d["status"] == "DATA_UNAVAILABLE" and by_name(d)["Down"]["fit_status"] == "DATA_UNAVAILABLE"
    assert by_name(d)["Other"]["fit_status"] == "OUTSIDE_UNIVERSE"


# ---- versions, sorting, error isolation -------------------------------------------------------------------------------------

def _raw_version(lab, sp, fingerprint=None, spec_hash=None, readiness="BACKTEST_READY"):
    sp = copy.deepcopy(sp)
    if fingerprint:
        sp["feature_registry_fingerprint"] = fingerprint
    sid = uuid.uuid4().hex
    with sqlite3.connect(lab.path) as c:
        c.execute("INSERT INTO strategy_definitions (strategy_id, name, created_at) VALUES (?,?,?)", (sid, sp["name"], "2026-09-01"))
        c.execute("INSERT INTO strategy_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (uuid.uuid4().hex, sid, 1, 1, 1, sp["feature_registry_fingerprint"], S.canonical_json(sp),
                   spec_hash or S.spec_hash(sp), S.rules_hash(sp), readiness, "2026-09-01", None))
    return sid


def test_error_isolation_and_no_substitution():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), name="Good"))
    lab.save(X.spec(symbols=("MU",), name="Portfolio", entry=G("ALL", {"feature": "portfolio.position_weight_pct", "op": "<", "value": 5})))
    _raw_version(lab, spec(G("ALL", TRUE), name="Mismatch"), fingerprint="0" * 64)
    _raw_version(lab, spec(G("ALL", TRUE), name="Tampered"), spec_hash="f" * 64)
    r = fit(lab, "MU")
    got = {n: s["fit_status"] for n, s in by_name(r).items()}
    assert got == {"Good": "RULES_MET", "Portfolio": "UNSUPPORTED", "Mismatch": "REGISTRY_MISMATCH", "Tampered": "INTEGRITY_ERROR"}
    assert r["counts"]["checked"] == 4 and r["counts"]["not_evaluated"] == 3
    assert all(s["trace"] is None for s in r["strategies"] if s["strategy_name"] != "Good")
    # for a symbol outside a hash-verified universe the answer is OUTSIDE UNIVERSE; an untrusted spec stays an error
    other = {n: s["fit_status"] for n, s in by_name(fit(lab, "NVDA")).items()}
    assert other == {"Good": "OUTSIDE_UNIVERSE", "Portfolio": "OUTSIDE_UNIVERSE", "Mismatch": "OUTSIDE_UNIVERSE",
                     "Tampered": "INTEGRITY_ERROR"}
    assert "also not evaluable: REGISTRY MISMATCH" in by_name(fit(lab, "NVDA"))["Mismatch"]["status_text"]


def test_current_versions_by_default_older_on_request_archived_never():
    lab = lab_with()
    sid = lab.save(spec(G("ALL", FALSE), name="Versions"))["strategy_id"]
    lab.strategies.add_version(sid, spec(G("ALL", TRUE), name="Versions"))
    arch = lab.save(spec(G("ALL", TRUE), name="Archived"))["strategy_id"]
    lab.strategies.set_archived(arch, True)
    cur = fit(lab, "MU")
    assert [(s["strategy_name"], s["version"], s["is_current"], s["fit_status"]) for s in cur["strategies"]] == \
           [("Versions", 2, True, "RULES_MET")]
    old = fit(lab, "MU", old=True)
    assert [(s["version"], s["is_current"], s["fit_status"]) for s in old["strategies"]] == \
           [(1, False, "RULES_NOT_MET"), (2, True, "RULES_MET")]


def test_sorted_by_name_then_version_never_by_fit():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), name="Zeta"))                # RULES MET
    lab.save(spec(G("ALL", FALSE), name="alpha"))              # RULES NOT MET
    lab.save(spec(G("ALL", UNAV), name="Beta"))                # INCOMPLETE
    sid = lab.save(spec(G("ALL", FALSE), name="Gamma"))["strategy_id"]
    lab.strategies.add_version(sid, spec(G("ALL", TRUE), name="Gamma"))
    r = fit(lab, "MU", old=True)
    assert [(s["strategy_name"], s["version"]) for s in r["strategies"]] == [("alpha", 1), ("Beta", 1), ("Gamma", 1),
                                                                             ("Gamma", 2), ("Zeta", 1)]
    assert not re.search(r"rank|score|percent|probab|confidence|best|recommend", json.dumps(sorted(r["strategies"][0])), re.I)


def test_portfolio_state_never_influences_the_fit(monkeypatch):
    lab = lab_with()
    lab.save(spec(PARITY_ENTRY, name="P"))
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", False)
    a = stable(fit(lab, "MU"))
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    b = stable(fit(lab, "MU"))
    assert a["strategies"] == b["strategies"]


# ---- API: exact endpoints, strict bodies, 0 Claude, 0 broker ------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from api.routes import portfolio as pr
    from api.server import app
    from pf_fixtures import FakeGatewayHttp, fake_provider
    from portfolio import explain as ex
    calls = []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append(1))
    monkeypatch.setattr(ex, "get_provider", lambda: calls.append(1))
    http = FakeGatewayHttp()
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    lab = lab_with()
    lab.research.add("MU", FL.ny(T, 10))
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr("data.market_data.fetch_daily_bars", lab.market.fetch)
    monkeypatch.setattr("data.market_data.get_data_client", lambda: object())
    monkeypatch.setattr("scanner.watchlist.load_watchlist", lambda *a, **k: ["NVDA", "AMD", "bad symbol!"])
    monkeypatch.setattr("database.database.get_db", lambda: lab.research)
    monkeypatch.setattr("services.event_context.build_event_context", lab.events.build)
    monkeypatch.setattr(C, "provider_coverage", lab.events.cov)
    monkeypatch.setattr(FC, "_utc", lambda now=None: FL.at(T))
    monkeypatch.setattr(FC, "BAR_CACHE", FC.BarCache())
    lab.market.now = FL.at(T)
    c = TestClient(app)
    c.lab, c.calls, c.http = lab, calls, http
    return c


def test_api_round_trip_no_ai_no_broker(api):
    lab = api.lab
    lab.save(spec(PARITY_ENTRY, symbols=("AMD", "MU"), name="Api"))
    lab.save(spec(G("ALL", TRUE), symbols=("CLS",), name="Cls only"))
    snaps0 = {s: len(v) for s, v in lab.research.snaps.items()}
    cfg = api.get("/api/strategy-fit/config").json()
    assert cfg["symbols"] == {"strategies": ["AMD", "CLS", "MU"], "watchlist": ["NVDA", "AMD"]} and lab.market.calls == []
    assert cfg["saved_strategies"] == 2 and "does not rank strategies" in cfg["note"]
    r = api.post("/api/strategy-fit/evaluate", json={"symbol": " mu ", "include_old_versions": False})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "MU" and body["decision_session"] == T.isoformat() and body["counts"]["checked"] == 2
    assert {s["strategy_name"]: s["fit_status"] for s in body["strategies"]}["Cls only"] == "OUTSIDE_UNIVERSE"
    assert api.post("/api/strategy-fit/evaluate", json={"symbol": "MU", "auto_refresh": True}).status_code == 422
    bad = api.post("/api/strategy-fit/evaluate", json={"symbol": "../x"})
    assert bad.status_code == 422 and bad.json()["status"] == "INVALID_SYMBOL"
    assert api.post("/api/strategy-fit/evaluate", json={"symbol": "A" * 13}).status_code == 422
    assert api.calls == [] and api.http.requests == []        # no Claude, no Robinhood gateway
    assert {s: len(v) for s, v in lab.research.snaps.items()} == snaps0     # no research was created


def test_strategy_fit_endpoints_are_exactly_the_read_only_pair():
    from api.server import app
    paths = app.openapi()["paths"]
    mine = {p: set(ops) for p, ops in paths.items() if p.startswith("/api/strategy-fit")}
    assert mine == {"/api/strategy-fit/config": {"get"}, "/api/strategy-fit/evaluate": {"post"}}
    for p in mine:
        assert not re.search(r"(?i)order|trade|paper|execute|broker|optimi|scan|rank|schedule|recommend|save|store", p), p


# ---- security, frozen stages, UI hooks + neutral language -----------------------------------------------------------------------

FIT_FILES = ["fit/__init__.py", "fit/current.py", "fit/evidence.py", "fit/readonly.py", "api/routes/strategy_fit.py",
             "frontend/strategy_fit.js"]


def test_no_code_execution_broker_ai_scheduler_or_sql_writes_in_fit_code():
    for f in FIT_FILES:
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|new Function|"
                             r"\bpickle\b|marshal|importlib", text), f
        assert not re.search(r"TradingClient|alpaca\.trading|place_?order|submit_?order|cancel_?order|/orders|webhook|"
                             r"anthropic|get_provider\(|from agents|import agents|from portfolio|import portfolio|rh_gateway|"
                             r"robinhood|analysis_cache|setInterval|setTimeout|apscheduler|BackgroundScheduler|add_job|"
                             r"crontab|threading\.Timer|shortcuts/holdings|/api/portfolio", text, re.I), f
        if f.endswith(".py"):
            assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\s+(INTO|TABLE|FROM|INDEX|TRIGGER|\w+\s+SET)",
                                 text), f
            assert "fetch_and_store(mem" in text or f != "fit/current.py"     # downloads go to memory, never to a dataset


def test_frozen_stage_31_to_33_modules_unchanged():
    frozen = ["strategy/__init__.py", "strategy/contracts.py", "strategy/evaluate.py", "strategy/examples.py",
              "strategy/features.py", "strategy/spec.py", "strategy/store.py", "strategy/CONTRACTS.md", "backtest/__init__.py",
              "backtest/bars.py", "backtest/engine.py", "backtest/metrics.py", "backtest/replay.py", "backtest/runs.py",
              "backtest/snapshots.py", "backtest/store.py", "backtest/METHOD.md", "forward/__init__.py", "forward/capture.py",
              "forward/METHOD.md", "database/migrations.py",
              "database/strategy_migrations.py", "database/backtest_migrations.py", "database/forward_migrations.py",
              "api/routes/strategies.py", "api/routes/backtests.py", "api/routes/forward_tests.py",
              "frontend/backtest_lab.js"]    # Stage 3.6 extends forward/journal.py, forward/store.py, forward_journal.js
    h = hashlib.sha256()
    for f in frozen:
        h.update(f.encode() + b"\0" + hashlib.sha256((ROOT / f).read_bytes()).digest())
    assert h.hexdigest() == FROZEN_MODULES
    assert not list((ROOT / "database").glob("*fit*")) and "strategy_fit" not in (ROOT / "database" / "migrations.py").read_text(encoding="utf-8")


def test_strategy_text_is_inert():
    lab = lab_with()
    lab.save(spec(G("ALL", TRUE), name="$(rm -rf /); __import__('os') <script>alert(1)</script>"))
    r = fit(lab, "MU")
    assert r["strategies"][0]["strategy_name"].startswith("$(rm") and r["strategies"][0]["fit_status"] == "RULES_MET"
    js = (ROOT / "frontend" / "strategy_fit.js").read_text(encoding="utf-8")
    assert "esc(s.strategy_name)" in js and "esc(t.text || t.feature)" in js and "innerHTML = `" in js


def test_75_76_ui_race_guard_partial_redraw_and_neutral_language():
    js = (ROOT / "frontend" / "strategy_fit.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    sl = (ROOT / "frontend" / "strategy_lab.js").read_text(encoding="utf-8")
    sr = (ROOT / "frontend" / "stock_result.js").read_text(encoding="utf-8")
    assert "new AbortController()" in js and "ctrl.abort()" in js and js.count("if (my !== seq) return;") == 2
    assert "setInterval" not in js and "setTimeout" not in js                       # no polling, no timers
    assert not re.search(r"sl-body|bt-body|fj-body|location\.reload|StrategyLab\.load\(", js)   # never redraws other panels
    assert 'classList.toggle("sf-mode"' in js and "Previous view — refreshing" in js
    assert "Rules are evaluated on the latest completed daily close. No order is placed." in js
    assert "This view compares current data with your saved rules. It does not rank strategies or predict future returns." in js
    assert "NOT used in Strategy Fit" in js and "Show older versions" in js and "Refresh current fit" in js
    assert "HISTORICAL EVIDENCE" in js and "FORWARD EVIDENCE" in js and "FORWARD JOURNAL STATE" in js
    assert "Most recent stored run" in js and "No evidence is not the same as a poor fit." in js
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))   # UI code, not comments
    low = re.sub(r"It does not rank strategies or predict future returns\.", "", code)
    assert not re.search(r"\bbuy\b|\bsell\b|recommend|best (fit|strategy|match|run)|top (strategy|match)|good strategy|"
                         r"bad strategy|highest fit|\bscore\b|confidence|probabilit|leaderboard|\brank|\bgrade|fit %|"
                         r"use this strategy|stars?\b", low, re.I)
    assert not re.search(r"conditions_met\s*/\s*[^)]*\*\s*100|\*\s*100", js)        # a count is never turned into a percentage
    assert 'id="sf-body"' in html and 'id="sl-subnav"' in html and "strategy_fit.css" in html
    assert html.index("forward_journal.js") < html.index("strategy_fit.js")
    assert "openVersion" in sl and 'tabBtn.addEventListener("click", () => { if (!loaded) load(); })' in sl
    assert 'data-fit-r="${esc(sym)}"' in sr and "window.StrategyFit.openFor(sym)" in sr
