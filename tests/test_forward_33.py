"""Stage 3.3: forward-test signal journal (saved versions only; manual captures; no backfill; no orders, no AI, no network)."""
import copy
import hashlib
import re
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bt_fixtures as X
import config
import fw_fixtures as FL
from backtest import runs as R
from backtest import snapshots as SN
from backtest.engine import RunConfig, simulate
from backtest.replay import BarSeries
from forward import capture as C
from forward import journal as J
from forward.store import ForwardError, ForwardStore
from strategy import features as F
from strategy import spec as S

ROOT = Path(__file__).resolve().parents[1]
D = date.fromisoformat
FROZEN_FINGERPRINT = "f394947b155dc9c002704531a9ca82ac3cfd37c3ab87dcadce4ccd8c15551e39"
QUIET = {"vol": 0.002}
UP3 = {"logic": "ALL", "conditions": [{"feature": "stock.change_1d_pct", "op": ">", "value": 3}]}
HOLD2 = {"logic": "ANY", "conditions": [], "invalidation": {"method": "PCT_BELOW_ENTRY", "pct": 20},
         "target": {"method": "PCT_ABOVE_ENTRY", "pct": 50}, "max_holding_days": 2}


def fspec(entry=None, exit_=None, symbols=("AMD", "MU", "NVDA"), name="Forward test"):
    s = X.spec(symbols=symbols, entry=entry or UP3, exit_=exit_ or HOLD2,
               risk={"max_position_pct": 10, "max_open_positions": 50}, name=name)
    n, err = S.validate(s)
    assert not err, err
    return n


def prev_session(m, d):
    return m.days[m.days.index(d) - 1]


def jump(m, sym, d, pct=5.0):
    """Make `sym` close `pct` % above the previous close on session d (so stock.change_1d_pct > 3 fires)."""
    pc = m.rows[sym][prev_session(m, d)][5]
    m.set_bar(sym, d, pc, pc * (1 + pct / 100) * 1.001, pc * 0.999, pc * (1 + pct / 100))


def raw_conn(lab):
    c = sqlite3.connect(lab.path)
    c.execute("PRAGMA foreign_keys = ON")
    return c


# ---- 54: FORWARD MEANS FORWARD — the creation day never counts, even if it has completed ------------------------------

def test_first_eligible_session_is_strictly_after_the_creation_date():
    lab = FL.FLab(FL.Market(**QUIET))
    j = lab.journal(fspec(), FL.ny(D("2026-09-29"), 11, 0))              # created BEFORE Sep 29's close
    jid = j["journal_id"]
    assert (j["created_session_date"], j["forward_start_date"], j["status"]) == ("2026-09-29", "2026-09-30", "ACTIVE")
    same_evening = FL.ny(D("2026-09-29"), 22, 0)
    assert lab.preflight(jid, same_evening)["status"] == "NO_ELIGIBLE_SESSION"
    next_morning = FL.at(D("2026-09-29"))                                  # Sep 29 is complete and its bars exist
    pf = lab.preflight(jid, next_morning)
    assert pf["status"] == "SESSION_NOT_COMPLETE" and "2026-09-30" in pf["errors"][0]["message"]
    for now in (same_evening, next_morning):
        with pytest.raises(ForwardError) as ei:
            lab.record(jid, now)
        assert ei.value.code in ("NO_ELIGIBLE_SESSION", "SESSION_NOT_COMPLETE") and ei.value.status == 409
    assert lab.fs.sessions(jid) == [] and lab.market.calls == []           # nothing downloaded, nothing stored
    r = lab.record(jid, FL.ny(D("2026-10-01"), 14, 0))                    # Oct 1 in progress -> Sep 30 is the latest close
    s = r["session"]["session"]
    assert r["status"] == "RECORDED" and s["session_date"] == "2026-09-30"
    assert [x["session_date"] for x in lab.fs.sessions(jid)] == ["2026-09-30"]           # Sep 29 never appears
    assert all(d["last_session"] <= "2026-09-30" for d in s["data"]["datasets"].values())  # today's bar never cached
    with raw_conn(lab) as c, pytest.raises(sqlite3.IntegrityError, match="forward start|no backfill"):
        c.execute("INSERT INTO forward_test_sessions (journal_id, session_date, kind, recorded_at, detected_with_session) "
                  "VALUES (?, '2026-09-29', 'MISSED', '2026-10-02T00:00:00+00:00', '2026-10-01')", (jid,))


# ---- 61 / 62: the full shadow-state cycle and next-open reference fills ----------------------------------------------

def _cycle_market():
    m = FL.Market(("AMD", "SPY"), **QUIET)
    jump(m, "AMD", D("2026-09-28"))
    m.set_bar("AMD", D("2026-09-29"), 120.0, 121.0, 119.0, 120.5)      # the signal is at Monday's close; Tuesday opens at 120
    m.set_bar("AMD", D("2026-09-30"), 120.5, 121.5, 120.0, 121.0)
    m.set_bar("AMD", D("2026-10-01"), 125.0, 126.0, 123.5, 124.0)
    return m


def test_state_machine_full_cycle_and_reference_fills_at_next_open():
    lab = FL.FLab(_cycle_market())
    jid = lab.journal(fspec(symbols=("AMD",)), FL.ny(D("2026-09-25"), 12))["journal_id"]
    seen = []
    for d in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"):
        o = lab.record(jid, FL.at(D(d)))["session"]["observations"][0]
        seen.append((d, o["state_before"], o["decision"], o["state_after"], o["reason_code"]))
    assert seen == [("2026-09-28", "FLAT", "ENTER", "ENTRY_PENDING", "ENTRY_RULES_MET"),
                    ("2026-09-29", "ENTRY_PENDING", "HOLD", "OPEN", "NO_EXIT_SIGNAL"),
                    ("2026-09-30", "OPEN", "EXIT", "EXIT_PENDING", "EXIT_RULES_MET"),
                    ("2026-10-01", "EXIT_PENDING", "HOLD", "FLAT", "NO_ENTRY")]
    signal_close = lab.market.rows["AMD"][D("2026-09-28")][5]
    fills = lab.fs.fills(jid)
    entry = next(f for f in fills if f["fill_type"] == "ENTRY")
    exit_ = next(f for f in fills if f["fill_type"] == "EXIT")
    assert (entry["status"], entry["signal_session_date"], entry["fill_session_date"], entry["reference_open_price"]) == \
        ("FILLED", "2026-09-28", "2026-09-29", 120.0)                          # Tuesday's OPEN, not Monday's close
    assert entry["reference_open_price"] != signal_close
    assert (exit_["signal_session_date"], exit_["fill_session_date"], exit_["reference_open_price"]) == ("2026-09-30", "2026-10-01", 125.0)
    assert exit_["reference_move_pct"] == pytest.approx((125.0 / 120.0 - 1) * 100) and exit_["delay_sessions"] == 0
    ex = lab.obs(jid, D("2026-09-30"))["AMD"]
    assert ex["exit_reasons"] == ["MAX_HOLDING"] and ex["lifecycle"]["holding_sessions"] == 2 and (ex["rules_met"], ex["rules_total"]) == (1, 3)
    assert lab.obs(jid, D("2026-09-28"))["AMD"]["lifecycle"]["entry_signal_close"] == signal_close
    summ = J.journal_view(lab.fs, jid)["summary"]
    assert (summ["enter_signals"], summ["exit_signals"], summ["completed_reference_cycles"], summ["continuity"]) == (1, 1, 1, "CONTINUOUS")
    with raw_conn(lab) as c, pytest.raises(sqlite3.IntegrityError):         # an illegal transition is rejected by the DB
        c.execute("INSERT INTO forward_test_sessions (journal_id, session_date, kind, recorded_at, market_close_snapshot_time, "
                  "context_timing, engine_version, spec_hash, data_json, data_hash, summary_json, warnings_json) VALUES "
                  "(?, '2026-10-02', 'CAPTURED', '2026-10-03T12:00:00+00:00', '2026-10-02T20:00:00+00:00', 'STRICT_FORWARD', "
                  "'x', ?, '{}', ?, '{}', '[]')", (jid, "a" * 64, "b" * 64))
        c.execute("INSERT INTO forward_test_observations (journal_id, session_date, symbol, state_before, decision, state_after, "
                  "reason_code, exit_reasons_json, unavailable_json, feature_snapshot_json, lifecycle_json, context_timing, "
                  "captured_at) VALUES (?, '2026-10-02', 'AMD', 'FLAT', 'HOLD', 'OPEN', 'X', '[]', '[]', '{}', '{}', "
                  "'STRICT_FORWARD', 'x')", (jid,))


# ---- 65: record twice -> ALREADY_RECORDED, nothing new --------------------------------------------------------------------

def test_recording_the_same_session_twice_returns_the_stored_observation():
    lab = FL.FLab(_cycle_market())
    jid = lab.journal(fspec(symbols=("AMD",)), FL.ny(D("2026-09-25"), 12))["journal_id"]
    first = lab.record(jid, FL.at(D("2026-09-28")))
    rows, calls = lab.fs.raw_rows(jid), len(lab.market.calls)
    lab.research.add("AMD", FL.ny(D("2026-09-28"), 18))                   # newer context appears — must not matter
    again = lab.record(jid, FL.at(D("2026-09-28")) + timedelta(hours=5))
    assert again["status"] == "ALREADY_RECORDED" and again["session"]["session"] == first["session"]["session"]
    assert again["session"]["observations"] == first["session"]["observations"]
    assert lab.fs.raw_rows(jid) == rows and len(lab.market.calls) == calls        # no recomputation, no download
    assert lab.preflight(jid, FL.at(D("2026-09-28")))["status"] == "ALREADY_RECORDED"


# ---- 63: entry-time support / resistance are frozen (unit, explicit cells) -----------------------------------------------

DS = {"dataset_id": "d" * 32, "content_hash": "c" * 64}


def _cells(close, support=None, resistance=None, rsi=10.0):
    return {"stock.rsi_14": {"v": rsi, "a": "AVAILABLE"}, "stock.close": {"v": close, "a": "AVAILABLE"},
            "stock.support": {"v": support, "a": "AVAILABLE" if support is not None else "UNAVAILABLE"},
            "stock.resistance": {"v": resistance, "a": "AVAILABLE" if resistance is not None else "UNAVAILABLE"},
            "market.trend": {"v": "MIXED", "a": "AVAILABLE"}}


def _run_steps(spec, bars, cells, sym="AMD"):
    days = sorted(bars)
    ser = BarSeries(sym, X.rows_from(bars))
    prev, out = None, []
    for i, T in enumerate(days):
        ctx = J._Ctx(spec, T, [], {sym: ser}, {sym: DS}, [d.isoformat() for d in days[:i + 1]], None)
        obs, fills = J.step(ctx, sym, prev, cells.get(T))
        out.append((obs, fills))
        prev = obs
    return out


def test_entry_support_and_resistance_are_frozen_at_the_entry_signal():
    d1, d2, d3, d4 = D("2026-09-28"), D("2026-09-29"), D("2026-09-30"), D("2026-10-01")
    low_rsi = {"logic": "ALL", "conditions": [{"feature": "stock.rsi_14", "op": "<", "value": 20}]}
    sup = fspec(entry=low_rsi, exit_={"logic": "ANY", "conditions": [], "invalidation": {"method": "CLOSE_BELOW_ENTRY_SUPPORT"},
                                      "target": None, "max_holding_days": None}, symbols=("AMD",))
    bars = {d1: (100, 101, 99, 100), d2: (100, 101, 99, 100), d3: (96, 97, 94, 95), d4: (90, 90, 88, 89)}
    cells = {d1: _cells(100, 90, 130), d2: _cells(100, 110, 130), d3: _cells(95, 110, 130), d4: _cells(89, 110, 130)}
    out = _run_steps(sup, bars, cells)
    assert [o["decision"] for o, _ in out] == ["ENTER", "HOLD", "HOLD", "EXIT"]
    assert out[2][0]["lifecycle"]["levels_used"]["invalidation_level"] == 90                 # not the later 110
    assert out[3][0]["exit_reasons"] == ["INVALIDATION"] and out[1][1][0]["reference_open_price"] == 100
    res = fspec(entry=low_rsi, exit_={"logic": "ANY", "conditions": [], "invalidation": None,
                                      "target": {"method": "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE"}, "max_holding_days": None},
                symbols=("AMD",))
    bars = {d1: (100, 101, 99, 100), d2: (100, 101, 99, 100), d3: (105, 112, 104, 111)}
    cells = {d1: _cells(100, 90, 110), d2: _cells(100, 90, 130), d3: _cells(111, 90, 130)}
    out = _run_steps(res, bars, cells)
    assert [o["decision"] for o, _ in out] == ["ENTER", "HOLD", "EXIT"] and out[2][0]["exit_reasons"] == ["TARGET"]
    assert out[2][0]["evaluation_trace"]["target"]["level"] == 110                              # not the later 130
    miss = _run_steps(sup, {d1: (100, 101, 99, 100)}, {d1: _cells(100, None, 130)})           # required level missing
    assert (miss[0][0]["decision"], miss[0][0]["reason_code"], miss[0][0]["state_after"]) == \
        ("SKIP", "REQUIRED_ENTRY_SUPPORT_UNAVAILABLE", "FLAT")


def test_unavailable_data_is_skip_never_a_false_hold_and_never_an_entry():
    t = lambda r: {"feature": "x", "result": r}                                  # noqa: E731
    assert J._determinate("ALL", [t("MET"), t("UNAVAILABLE")]) is None             # could still be met -> SKIP
    assert J._determinate("ALL", [t("NOT_MET"), t("UNAVAILABLE")]) is False        # certainly not met -> HOLD
    assert J._determinate("ANY", [t("MET"), t("UNAVAILABLE")]) is True             # already met -> ENTER
    assert J._determinate("ANY", [t("NOT_MET"), t("UNAVAILABLE")]) is None
    assert J._determinate("ALL", [{"group": "ANY", "children": [t("NOT_MET"), t("UNAVAILABLE")]}, t("MET")]) is None
    d1 = D("2026-09-28")
    mix = fspec(entry={"logic": "ALL", "conditions": [{"feature": "stock.rsi_14", "op": "<", "value": 20},
                                                      {"feature": "research.view", "op": "==", "value": "BULLISH BIAS"}]},
                symbols=("AMD",))
    cells = {**_cells(100, 90, 130), "research.view": {"v": None, "a": "UNAVAILABLE", "reason": "RESEARCH_UNAVAILABLE"}}
    o = _run_steps(mix, {d1: (100, 101, 99, 100)}, {d1: cells})[0][0]
    assert (o["decision"], o["reason_code"], o["state_after"], o["rules_met"], o["rules_total"]) == \
        ("SKIP", "REQUIRED_DATA_UNAVAILABLE", "FLAT", 1, 2)
    o = _run_steps(mix, {d1: (100, 101, 99, 100)}, {d1: {**cells, "stock.rsi_14": {"v": 50.0, "a": "AVAILABLE"}}})[0][0]
    assert (o["decision"], o["reason_code"]) == ("HOLD", "NO_ENTRY")                # decided without the missing value


# ---- 64: holding counts captured market sessions; weekends and holidays are not sessions ---------------------------------

def test_holding_sessions_skip_weekend_and_holiday():
    m = FL.Market(("AMD", "SPY"), **QUIET)                                   # Labor Day 2026-09-07 is not a session
    jump(m, "AMD", D("2026-09-03"))
    lab = FL.FLab(m)
    jid = lab.journal(fspec(symbols=("AMD",)), FL.ny(D("2026-09-02"), 12))["journal_id"]
    assert lab.record(jid, FL.at(D("2026-09-03")))["session"]["observations"][0]["decision"] == "ENTER"
    assert lab.record(jid, FL.at(D("2026-09-04")))["session"]["observations"][0]["lifecycle"]["holding_sessions"] == 1
    assert lab.record(jid, FL.at(D("2026-09-07")))["status"] == "ALREADY_RECORDED"      # holiday: no new session
    o = lab.record(jid, FL.at(D("2026-09-08")))["session"]["observations"][0]
    assert o["lifecycle"]["holding_sessions"] == 2 and o["decision"] == "EXIT" and o["exit_reasons"] == ["MAX_HOLDING"]
    assert [s["kind"] for s in lab.fs.sessions(jid)] == ["CAPTURED"] * 3                 # no MISSED weekend / holiday


def test_unfilled_entry_and_open_state_without_a_bar():
    m = FL.Market(("AMD", "MU", "SPY"), **QUIET)
    jump(m, "AMD", D("2026-09-28"))
    jump(m, "MU", D("2026-09-28"))
    m.drop("AMD", D("2026-09-29"))                                          # no AMD bar on the next session
    m.drop("MU", D("2026-09-30"))                                           # MU fills, then has no bar one day
    lab = FL.FLab(m)
    jid = lab.journal(fspec(symbols=("AMD", "MU")), FL.ny(D("2026-09-25"), 12))["journal_id"]
    lab.record(jid, FL.at(D("2026-09-28")))
    o = {x["symbol"]: x for x in lab.record(jid, FL.at(D("2026-09-29")))["session"]["observations"]}
    assert (o["AMD"]["decision"], o["AMD"]["reason_code"], o["AMD"]["state_after"]) == ("SKIP", "NO_BAR_FOR_SESSION", "FLAT")
    unf = next(f for f in lab.fs.fills(jid) if f["symbol"] == "AMD")
    assert (unf["status"], unf["reason_code"], unf["fill_session_date"], unf["reference_open_price"]) == \
        ("UNFILLED", "NEXT_SESSION_BAR_MISSING", None, None)
    assert o["MU"]["state_after"] == "OPEN" and o["MU"]["lifecycle"]["holding_sessions"] == 1
    mu = {x["symbol"]: x for x in lab.record(jid, FL.at(D("2026-09-30")))["session"]["observations"]}["MU"]
    assert (mu["decision"], mu["reason_code"], mu["state_after"], mu["lifecycle"]["holding_sessions"]) == \
        ("SKIP", "NO_BAR_FOR_SESSION", "OPEN", 1)                              # not counted, not evaluated


# ---- 60 / 18 / 19: continuity — missed while FLAT gaps; missed while pending / open blocks ----------------------------------

def test_missed_session_while_flat_marks_gapped_and_keeps_observing():
    lab = FL.FLab(FL.Market(**QUIET))
    jid = lab.journal(fspec(), FL.ny(D("2026-09-25"), 12))["journal_id"]
    lab.record(jid, FL.at(D("2026-09-28")))                                 # Monday
    r = lab.record(jid, FL.at(D("2026-09-30")))                             # Tuesday was never captured
    assert r["session"]["session"]["session_date"] == "2026-09-30"
    assert [(s["session_date"], s["kind"]) for s in lab.fs.sessions(jid)] == [
        ("2026-09-28", "CAPTURED"), ("2026-09-29", "MISSED"), ("2026-09-30", "CAPTURED")]
    assert all(o["decision"] == "HOLD" and o["state_after"] == "FLAT" for o in r["session"]["observations"])
    v = J.journal_view(lab.fs, jid)
    assert v["summary"]["continuity"] == "GAPPED" and v["journal"]["status"] == "ACTIVE"
    assert any(w["code"] == "MISSED_FORWARD_SESSION" for w in r["session"]["session"]["warnings"])
    assert lab.fs.observations(jid, "2026-09-29") == []                     # nothing reconstructed for the missed day


def test_missed_session_while_pending_or_open_blocks_the_symbol():
    m = FL.Market(**QUIET)
    jump(m, "AMD", D("2026-09-28"))                                         # AMD: ENTER Mon, fills Tue -> OPEN
    jump(m, "MU", D("2026-09-29"))                                          # MU: ENTER Tue -> ENTRY_PENDING
    lab = FL.FLab(m)
    jid = lab.journal(fspec(), FL.ny(D("2026-09-25"), 12))["journal_id"]
    lab.record(jid, FL.at(D("2026-09-28")))
    lab.record(jid, FL.at(D("2026-09-29")))
    r = lab.record(jid, FL.at(D("2026-10-01")))                             # Wednesday Sep 30 missed
    o = {x["symbol"]: x for x in r["session"]["observations"]}
    for sym, before in (("AMD", "OPEN"), ("MU", "ENTRY_PENDING")):
        assert (o[sym]["state_before"], o[sym]["decision"], o[sym]["state_after"], o[sym]["reason_code"]) == \
            (before, "SKIP", "CONTINUITY_BLOCKED", "FORWARD_CONTINUITY_GAP"), sym
        assert o[sym]["evaluation_trace"] is None and o[sym]["lifecycle"]["blocked"]["missed_sessions"] == ["2026-09-30"]
    assert o["NVDA"]["state_after"] == "FLAT" and o["NVDA"]["evaluation_trace"] is not None      # FLAT keeps going
    assert [f for f in lab.fs.fills(jid) if f["resolved_in_session"] == "2026-10-01"] == []       # no reference fill claimed
    v = J.journal_view(lab.fs, jid)
    assert v["journal"]["status"] == "CONTINUITY_BLOCKED" and v["summary"]["blocked_symbols"] == ["AMD", "MU"]
    assert any(w["code"] == "FORWARD_CONTINUITY_GAP" for w in r["session"]["session"]["warnings"])
    o2 = {x["symbol"]: x for x in lab.record(jid, FL.at(D("2026-10-02")))["session"]["observations"]}
    assert o2["AMD"]["state_after"] == "CONTINUITY_BLOCKED" and o2["NVDA"]["reason_code"] in ("NO_ENTRY", "ENTRY_RULES_MET")
    pf = lab.preflight(jid, FL.at(D("2026-10-05")))
    assert pf["continuity"]["already_blocked"] == ["AMD", "MU"]


# ---- 33 / 35: database-enforced immutability and no backfill ---------------------------------------------------------------

def test_evidence_is_append_only_and_cannot_be_backfilled():
    lab = FL.FLab(_cycle_market())
    jid = lab.journal(fspec(symbols=("AMD",)), FL.ny(D("2026-09-25"), 12))["journal_id"]
    for d in ("2026-09-28", "2026-09-29", "2026-09-30"):
        lab.record(jid, FL.at(D(d)))
    with raw_conn(lab) as c:
        for sql in ("UPDATE forward_test_sessions SET context_timing = 'STRICT_FORWARD'",
                    "DELETE FROM forward_test_sessions", "UPDATE forward_test_observations SET decision = 'HOLD'",
                    "DELETE FROM forward_test_observations", "UPDATE forward_test_reference_fills SET reference_open_price = 1",
                    "DELETE FROM forward_test_reference_fills", "DELETE FROM forward_test_journals",
                    "UPDATE forward_test_journals SET spec_hash = 'x'", "UPDATE forward_test_journals SET forward_start_date = '2020-01-01'"):
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(sql)
        with pytest.raises(sqlite3.IntegrityError, match="no backfill"):     # a session before the latest capture
            c.execute("INSERT INTO forward_test_sessions (journal_id, session_date, kind, recorded_at, detected_with_session) "
                      "VALUES (?, '2026-09-27', 'MISSED', 'x', '2026-10-01')", (jid,))
        with pytest.raises(sqlite3.IntegrityError, match="latest captured session"):   # adding to an older session
            c.execute("INSERT INTO forward_test_observations (journal_id, session_date, symbol, state_before, decision, "
                      "state_after, reason_code, exit_reasons_json, unavailable_json, feature_snapshot_json, lifecycle_json, "
                      "context_timing, captured_at) VALUES (?, '2026-09-28', 'ZZZ', 'FLAT', 'HOLD', 'FLAT', 'NO_ENTRY', '[]', "
                      "'[]', '{}', '{}', 'STRICT_FORWARD', 'x')", (jid,))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE forward_test_journals SET status = 'NONSENSE'")


def test_a_failed_write_leaves_nothing_behind(monkeypatch):
    lab = FL.FLab(FL.Market(**QUIET))
    jid = lab.journal(fspec(), FL.ny(D("2026-09-25"), 12))["journal_id"]
    real = J.step

    def broken(ctx, sym, prev, cells):
        obs, f = real(ctx, sym, prev, cells)
        return ({**obs, "state_after": "OPEN"} if sym == "NVDA" else obs), f          # FLAT -> OPEN is illegal
    monkeypatch.setattr(J, "step", broken)
    with pytest.raises(ForwardError) as ei:
        lab.record(jid, FL.at(D("2026-09-28")))
    assert ei.value.code == "DATA_INTEGRITY_ERROR"
    assert lab.fs.raw_rows(jid) == {"forward_test_sessions": [], "forward_test_observations": [], "forward_test_reference_fills": []}


# ---- 55 / 57 / 58: a stored observation never changes when research, events or later bars change ------------------------

def test_stored_observation_is_immutable_when_research_events_and_bars_change():
    m = FL.Market(**QUIET)
    lab = FL.FLab(m)
    lab.events.earnings = True                                              # complete event data -> levels available
    lab.events.level = {"AMD": "LOW", "MU": "MEDIUM", "NVDA": "HIGH"}
    T = D("2026-09-28")
    for sym in ("AMD", "MU", "NVDA"):
        lab.research.add(sym, FL.ny(T, 10), view="BULLISH BIAS", sid=1)
    sp = fspec(entry={"logic": "ALL", "conditions": [
        {"feature": "research.view", "op": "in", "value": ["BULLISH BIAS", "STRONG BULLISH BIAS"]},
        {"feature": "event.risk_level", "op": "!=", "value": "HIGH"}, {"feature": "stock.close", "op": ">", "value": 1}]})
    jid = lab.journal(sp, FL.ny(D("2026-09-25"), 12))["journal_id"]
    r = lab.record(jid, FL.at(T))
    o = {x["symbol"]: x for x in r["session"]["observations"]}
    assert (o["AMD"]["decision"], o["MU"]["decision"], o["NVDA"]["decision"]) == ("ENTER", "ENTER", "HOLD")
    assert o["AMD"]["research"]["snapshot_id"] == 1 and o["AMD"]["research"]["existed_at_close"] is True
    assert r["session"]["session"]["context_timing"] == "POST_CLOSE_FORWARD_CONTEXT"          # events are read after the close
    rows = lab.fs.raw_rows(jid)
    view = J.session_view(lab.fs, jid, T.isoformat())
    # everything changes afterwards: research replaced, event risk changed, later bars restated
    for sym in ("AMD", "MU", "NVDA"):
        lab.research.add(sym, FL.ny(T, 20), view="STRONG BEARISH BIAS", sid=2)
    lab.events.level = {"AMD": "HIGH", "MU": "HIGH", "NVDA": "LOW"}
    m.mutate_after(T)
    assert lab.record(jid, FL.at(T))["status"] == "ALREADY_RECORDED"
    assert lab.fs.raw_rows(jid) == rows and J.session_view(lab.fs, jid, T.isoformat()) == view        # byte-identical
    nxt = {x["symbol"]: x for x in lab.record(jid, FL.at(D("2026-09-29")))["session"]["observations"]}
    assert nxt["NVDA"]["research"]["snapshot_id"] == 2 and nxt["NVDA"]["decision"] == "HOLD"   # new context, new session only
    assert lab.fs.raw_rows(jid)["forward_test_observations"][:3] == rows["forward_test_observations"]


# ---- 56: no future bar can change a capture; the technical snapshot IS the Stage 3.2 snapshot ---------------------------

def test_future_bars_cannot_change_the_capture_and_match_stage32():
    T = D("2026-09-30")
    labs = []
    for mutate in (False, True):
        m = FL.Market()
        if mutate:
            m.mutate_after(T)                                                # includes "today's" partial bar (Oct 1)
        lab = FL.FLab(m)
        jid = lab.journal(fspec(entry={"logic": "ALL", "conditions": [{"feature": "stock.rsi_14", "op": "<", "value": 55},
                                                                    {"feature": "stock.trend", "op": "!=", "value": "DOWNTREND"}]}),
                          FL.ny(D("2026-09-28"), 12))["journal_id"]
        labs.append(lab.record(jid, FL.ny(D("2026-10-01"), 15))["session"]["observations"])
    strip = lambda obs: [{k: o[k] for k in ("symbol", "decision", "state_after", "reason_code", "rules_met", "evaluation_trace",  # noqa: E731
                                            "unavailable", "lifecycle")} | {"features": o["feature_snapshot"]["features"]} for o in obs]
    assert strip(labs[0]) == strip(labs[1])
    m = FL.Market()
    series = {s: BarSeries(s, [m.rows[s][d] for d in sorted(m.rows[s]) if d <= T]) for s in m.rows}
    ref = SN.day_snapshot(series, C.price_needs(fspec(entry={"logic": "ALL", "conditions": [
        {"feature": "stock.rsi_14", "op": "<", "value": 55}, {"feature": "stock.trend", "op": "!=", "value": "DOWNTREND"}]})), T)
    for o in labs[0]:
        stored = {f["feature_id"]: (f["value"], f["availability"]) for f in o["feature_snapshot"]["features"]}
        for fid, cell in {**ref["market"], **ref["symbols"][o["symbol"]]["cells"]}.items():
            assert stored[fid] == (cell["v"], cell["a"]), (o["symbol"], fid)
    assert ref["replay"]["violations"] == 0


# ---- 59: no automatic AI — research-only rules without research are SKIP ------------------------------------------------

def test_research_rule_without_saved_research_is_skip_and_makes_no_ai_call(monkeypatch):
    import agents.technical_agent as ta
    from portfolio import explain as ex
    calls = []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append("technical"))
    monkeypatch.setattr(ex, "get_provider", lambda: calls.append("explain"))
    lab = FL.FLab(FL.Market(**QUIET))
    sp = fspec(entry={"logic": "ALL", "conditions": [{"feature": "research.catalyst", "op": "==", "value": "POSITIVE"}]})
    jid = lab.journal(sp, FL.ny(D("2026-09-25"), 12))["journal_id"]
    pf = lab.preflight(jid, FL.at(D("2026-09-28")))
    assert pf["status"] == "CAN_RECORD_WITH_MISSING_INPUT"
    assert any(c["code"] == "RESEARCH" and c["ok"] is False and "Research required" in c["label"] for c in pf["checks"])
    r = lab.record(jid, FL.at(D("2026-09-28")))
    for o in r["session"]["observations"]:
        assert (o["decision"], o["reason_code"]) == ("SKIP", "REQUIRED_DATA_UNAVAILABLE")
        assert o["research"]["source"] == "UNAVAILABLE" and "Research required" in o["research"]["message"]
        assert o["unavailable"] == [{"feature": "research.catalyst", "availability": "UNAVAILABLE", "reason": "RESEARCH_UNAVAILABLE"}]
    assert any(w["code"] == "RESEARCH_UNAVAILABLE" for w in r["session"]["session"]["warnings"])
    assert calls == []


# ---- 25: events — missing data is never LOW; provider status is recorded -----------------------------------------------

def test_event_values_are_withheld_when_calendars_are_incomplete():
    ev = FL.Events()
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    ev.level = {"AMD": "LOW", "MU": "HIGH", "NVDA": "NONE"}
    per, market = C.event_context(["AMD", "MU", "NVDA"], ["event.risk_level"], [], now, ev.build, ev.cov)
    assert per["AMD"][0]["event.risk_level"] == {"v": None, "a": "UNAVAILABLE", "reason": "EVENT_DATA_INCOMPLETE"}
    assert per["NVDA"][0]["event.risk_level"]["a"] == "UNAVAILABLE"                    # "NONE" is not trusted either
    assert per["MU"][0]["event.risk_level"]["v"] == "HIGH"                             # HIGH is certain even if partial
    assert per["AMD"][1]["status"] == "PARTIAL" and per["AMD"][1]["providers"]["earnings"]["available"] is False
    ev.earnings = True
    per, _ = C.event_context(["AMD"], ["event.risk_level"], [], now, ev.build, ev.cov)
    assert per["AMD"][0]["event.risk_level"]["v"] == "LOW" and per["AMD"][1]["status"] == "COMPLETE"
    ev.coverage = {**FL.COMPLETE, "fred": {**FL.COMPLETE["fred"], "CPI": False}}
    per, market = C.event_context(["AMD"], ["event.risk_level"], ["market.major_event_within_24h"], now, ev.build, ev.cov)
    assert per["AMD"][0]["event.risk_level"]["a"] == "UNAVAILABLE"
    assert market[0]["market.major_event_within_24h"]["a"] == "UNAVAILABLE"            # "no event" cannot be confirmed
    from e_fixtures import ev as macro
    ev.macro = [macro(hours=10.0, now=now)]
    _, market = C.event_context(["AMD"], [], ["market.major_event_within_24h"], now, ev.build, ev.cov)
    assert market[0]["market.major_event_within_24h"]["v"] is True and market[1]["events"][0]["hours_until"] == pytest.approx(10.0)
    ev.raise_for = {"AMD"}
    per, _ = C.event_context(["AMD"], ["event.risk_level"], [], now, ev.build, ev.cov)
    assert per["AMD"][0]["event.risk_level"]["reason"] == "EVENT_DATA_UNAVAILABLE" and per["AMD"][1]["status"] == "UNAVAILABLE"


def test_provider_coverage_reads_the_stage26_success_cache():
    from data.events.cache import event_cache
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=config.EVENT_LOOKBACK_DAYS)).date().isoformat()
    end = (now + timedelta(days=config.EVENT_LOOKAHEAD_DAYS)).date().isoformat()
    for rid in config.FRED_RELEASE_IDS.values():
        event_cache.set(f"fred:{rid}:{start}:{end}", [], 5)
    event_cache.set("fomc:calendar", [], 5)
    keys = [f"fred:{rid}:{start}:{end}" for rid in config.FRED_RELEASE_IDS.values()] + ["fomc:calendar", f"corporate:ZZZT:{start}:{end}"]
    try:
        cov = C.provider_coverage("zzzt")
        assert all(cov["fred"].values()) and cov["fomc"] and cov["corporate"] is False
        event_cache.set(f"corporate:ZZZT:{start}:{end}", [], 5)
        assert C.provider_coverage("zzzt")["corporate"] is True
    finally:
        for k in keys:                                                       # leave the process-wide cache as found
            event_cache._store.pop(k, None)


# ---- 26: context timing ----------------------------------------------------------------------------------------------------

def test_context_timing_strict_vs_post_close():
    T = D("2026-09-28")
    lab = FL.FLab(FL.Market(**QUIET))
    price_only = lab.journal(fspec(name="price"), FL.ny(D("2026-09-25"), 12))["journal_id"]
    assert lab.record(price_only, FL.at(T))["session"]["session"]["context_timing"] == "STRICT_FORWARD"
    lab2 = FL.FLab(FL.Market(**QUIET))
    lab2.research.add("AMD", FL.ny(T, 11))                                   # saved before the close
    lab2.research.add("MU", FL.ny(T, 19))                                    # saved after the close
    rv = lab2.journal(fspec(entry={"logic": "ALL", "conditions": [{"feature": "research.view", "op": "==", "value": "BULLISH BIAS"}]}),
                      FL.ny(D("2026-09-25"), 12))["journal_id"]
    s = lab2.record(rv, FL.at(T))["session"]
    o = {x["symbol"]: x for x in s["observations"]}
    assert o["AMD"]["context_timing"] == "STRICT_FORWARD" and o["MU"]["context_timing"] == "POST_CLOSE_FORWARD_CONTEXT"
    assert s["session"]["context_timing"] == "POST_CLOSE_FORWARD_CONTEXT" and s["session"]["forward_context_captured_at"]
    assert {x["feature_id"]: x["timing"] for x in o["AMD"]["feature_snapshot"]["features"]}["research.view"] == "AT_CLOSE"
    cells, prov = C.research_context(["AMD"], ["research.freshness"], FL.at(T), T, lambda: lab2.research)["AMD"]
    assert cells["research.freshness"]["timing"] == "POST_CLOSE"             # freshness is measured at capture time
    late = FL.FLab(FL.Market(**QUIET))
    late.research.add("AMD", FL.ny(T, 11))
    lj = late.journal(fspec(entry={"logic": "ALL", "conditions": [{"feature": "research.freshness", "op": "==", "value": "FRESH"}]}),
                      FL.ny(D("2026-09-25"), 12))["journal_id"]
    codes = {w["code"] for w in late.record(lj, FL.ny(D("2026-09-29"), 13))["session"]["session"]["warnings"]}
    assert {"POST_CLOSE_FORWARD_CONTEXT", "CAPTURED_AFTER_NEXT_OPEN"} <= codes


# ---- 3 / 4 / 53: eligibility and re-verification ---------------------------------------------------------------------------

def _raw_version(lab, spec, fingerprint=None, spec_hash=None, readiness="BACKTEST_READY"):
    import uuid
    sp = copy.deepcopy(spec)
    if fingerprint:
        sp["feature_registry_fingerprint"] = fingerprint
    sid = uuid.uuid4().hex
    with sqlite3.connect(lab.path) as c:
        c.execute("INSERT INTO strategy_definitions (strategy_id, name, created_at) VALUES (?,?,?)", (sid, sp["name"], "2026-09-01"))
        c.execute("INSERT INTO strategy_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (uuid.uuid4().hex, sid, 1, 1, 1, sp["feature_registry_fingerprint"], S.canonical_json(sp),
                   spec_hash or S.spec_hash(sp), S.rules_hash(sp), readiness, "2026-09-01", None))
    return sid


def test_eligibility_allows_ready_and_forward_only_and_rejects_the_rest():
    lab = FL.FLab()
    ok = lab.save(fspec())["strategy_id"]
    fwd = lab.save(fspec(entry={"logic": "ALL", "conditions": [{"feature": "research.view", "op": "==", "value": "BULLISH BIAS"}]}))["strategy_id"]
    uns = lab.save(X.spec(entry={"logic": "ALL", "conditions": [{"feature": "portfolio.position_weight_pct", "op": "<", "value": 5}]}))["strategy_id"]
    mism = _raw_version(lab, fspec(), fingerprint="0" * 64)
    tamper = _raw_version(lab, fspec(), spec_hash="f" * 64)
    codes = {n: [e["code"] for e in J.eligibility(lab.store, s, 1)[3]]
             for n, s in (("ok", ok), ("fwd", fwd), ("uns", uns), ("mism", mism), ("tamper", tamper))}
    assert codes == {"ok": [], "fwd": [], "uns": ["FORWARD_UNSUPPORTED"], "mism": ["REGISTRY_MISMATCH"],
                     "tamper": ["STRATEGY_INTEGRITY_ERROR"]}
    assert J.eligibility(lab.store, ok, 7)[3][0]["code"] == "NOT_FOUND"
    ref = J.eligibility(lab.store, fwd, 1)[0]
    assert ref["readiness"] == "FORWARD_TEST_ONLY" and [f["feature"] for f in ref["forward_only_features"]] == ["research.view"]
    for sid, code in ((uns, "FORWARD_UNSUPPORTED"), (mism, "REGISTRY_MISMATCH"), (tamper, "STRATEGY_INTEGRITY_ERROR")):
        with pytest.raises(ForwardError) as ei:
            J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(D("2026-09-25"), 12))
        assert ei.value.code == code and ei.value.status == 422
    lv = J.list_view(lab.fs, lab.store, fwd, 1)["eligibility"]
    assert lv["eligible"] and lv["historical_backtest_available"] is False
    assert J.list_view(lab.fs, lab.store, ok, 1)["eligibility"]["historical_backtest_available"] is True


def test_every_capture_reverifies_the_strategy_version():
    lab = FL.FLab(FL.Market(**QUIET))
    jid = lab.journal(fspec(), FL.ny(D("2026-09-25"), 12))["journal_id"]
    with sqlite3.connect(lab.path) as c:                                     # someone removes the guard and edits the spec
        c.execute("DROP TRIGGER strategy_versions_immutable_update")
        c.execute("UPDATE strategy_versions SET spec_json = replace(spec_json, '\"value\":3', '\"value\":2')")
    with pytest.raises(ForwardError) as ei:
        lab.record(jid, FL.at(D("2026-09-28")))
    assert ei.value.code == "STRATEGY_INTEGRITY_ERROR" and lab.fs.sessions(jid) == []
    assert lab.preflight(jid, FL.at(D("2026-09-28")))["status"] == "BLOCKED"


# ---- 46 / 47: one primary journal per version; archive stops captures, deletes nothing -----------------------------------

def test_one_active_journal_per_version_and_archive_keeps_evidence():
    lab = FL.FLab(FL.Market(**QUIET))
    sid = lab.save(fspec())["strategy_id"]
    j = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(D("2026-09-25"), 12))
    with pytest.raises(ForwardError) as ei:
        J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(D("2026-09-26"), 12))
    assert ei.value.code == "JOURNAL_EXISTS" and ei.value.detail["journal_id"] == j["journal_id"]
    lab.record(j["journal_id"], FL.at(D("2026-09-28")))
    rows = lab.fs.raw_rows(j["journal_id"])
    arch = J.archive_journal(lab.fs, j["journal_id"], now=FL.ny(D("2026-09-29"), 12))
    assert arch["status"] == "ARCHIVED" and arch["archived_at"]
    with pytest.raises(ForwardError) as ei:
        lab.record(j["journal_id"], FL.at(D("2026-09-29")))
    assert ei.value.code == "JOURNAL_ARCHIVED" and ei.value.status == 409
    assert lab.preflight(j["journal_id"], FL.at(D("2026-09-29")))["status"] == "JOURNAL_ARCHIVED"
    with pytest.raises(ForwardError):
        J.archive_journal(lab.fs, j["journal_id"])
    assert lab.fs.raw_rows(j["journal_id"]) == rows and J.journal_view(lab.fs, j["journal_id"])["summary"]["sessions_captured"] == 1
    with raw_conn(lab) as c, pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE forward_test_journals SET status = 'ACTIVE', archived_at = NULL WHERE journal_id = ?", (j["journal_id"],))
    j2 = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(D("2026-09-29"), 12))          # a fresh journal
    assert j2["journal_id"] != j["journal_id"] and j2["forward_start_date"] == "2026-09-30"
    assert [x["status"] for x in lab.fs.list_journals(sid, 1)] == ["ACTIVE", "ARCHIVED"]


# ---- the same next-open / exit semantics as the Stage 3.2 engine --------------------------------------------------------------

def test_lifecycle_matches_the_stage32_engine_on_the_same_bars():
    syms = ("AMD", "NVDA")
    m = FL.Market((*syms, "SPY"))
    sp = fspec(entry={"logic": "ALL", "conditions": [{"feature": "stock.rsi_14", "op": "<", "value": 52}]},
               exit_={"logic": "ANY", "conditions": [{"feature": "stock.rsi_14", "op": ">", "value": 62}],
                      "invalidation": {"method": "PCT_BELOW_ENTRY", "pct": 2}, "target": {"method": "PCT_ABOVE_ENTRY", "pct": 3},
                      "max_holding_days": 4}, symbols=syms)
    cal = [d for d in m.days if D("2026-08-03") <= d <= D("2026-09-11")]
    lab = FL.FLab(m)
    jid = lab.journal(sp, FL.ny(D("2026-07-31"), 12))["journal_id"]
    for T in cal:
        lab.record(jid, FL.at(T))
    series = {s: BarSeries(s, [m.rows[s][d] for d in sorted(m.rows[s]) if d <= cal[-1]]) for s in m.rows}
    needs = C.price_needs(sp)
    snaps = {T: SN.day_snapshot(series, needs, T) for T in cal}

    def snap(sym, T):
        s = snaps[T]["symbols"].get(sym)
        return None if s is None else {**snaps[T]["market"], **s["cells"]}
    sim = simulate(sp, RunConfig(cal[0], cal[-1], 1e9, 0.0, 0.0), cal, {s: series[s] for s in syms}, snap)
    bt = [(t["symbol"], t["entry_signal_date"], t["entry_fill_date"], t["entry_open_price"], t["exit_signal_date"],
           t["all_exit_reasons"], t["exit_fill_date"], t["exit_open_price"], t["holding_days"]) for t in sim["trades"]]
    obs = lab.fs.observations(jid)
    fills = lab.fs.fills(jid)
    fw = []
    for f in [x for x in fills if x["fill_type"] == "ENTRY" and x["status"] == "FILLED"]:
        ex = next((x for x in fills if x["fill_type"] == "EXIT" and x["symbol"] == f["symbol"] and x["cycle_no"] == f["cycle_no"]), None)
        exo = next((o for o in obs if o["symbol"] == f["symbol"] and o["decision"] == "EXIT"
                    and o["lifecycle"]["cycle_no"] == f["cycle_no"]), None)
        last = [o for o in obs if o["symbol"] == f["symbol"] and o["lifecycle"]["cycle_no"] == f["cycle_no"]
                and o["state_after"] in ("OPEN", "EXIT_PENDING")]
        fw.append((f["symbol"], f["signal_session_date"], f["fill_session_date"], f["reference_open_price"],
                   exo["session_date"] if exo else None, exo["exit_reasons"] if exo else [],
                   ex["fill_session_date"] if ex else None, ex["reference_open_price"] if ex else None,
                   (exo or last[-1])["lifecycle"]["holding_sessions"]))
    key = lambda r: (r[1], r[0])                                                # noqa: E731
    assert len(bt) >= 6 and sorted(bt, key=key) == sorted(fw, key=key)
    signals = sorted((e["symbol"], e["session_date"]) for e in sim["events"] if e["event_type"] == "ENTRY_SIGNAL")
    assert signals == sorted((o["symbol"], o["session_date"]) for o in obs if o["decision"] == "ENTER")


# ---- adjusted prices: a split after entry restates earlier bars; levels are rebased, never misread ------------------------

def test_split_after_entry_rebases_levels_and_reference_move():
    m = FL.Market(("AMD", "SPY"), **QUIET)
    jump(m, "AMD", D("2026-09-28"))
    m.split = ("AMD", D("2026-09-30"), 2.0)                                  # 2-for-1 effective at the Sep 30 open
    lab = FL.FLab(m)
    sp = fspec(symbols=("AMD",), exit_={"logic": "ANY", "conditions": [], "invalidation": {"method": "PCT_BELOW_ENTRY", "pct": 5},
                                        "target": {"method": "PCT_ABOVE_ENTRY", "pct": 50}, "max_holding_days": 3})
    jid = lab.journal(sp, FL.ny(D("2026-09-25"), 12))["journal_id"]
    lab.record(jid, FL.at(D("2026-09-28")))
    lab.record(jid, FL.at(D("2026-09-29")))                                 # reference entry at the pre-split open
    entry = lab.fs.fills(jid)[0]
    o = lab.record(jid, FL.at(D("2026-09-30")))["session"]
    ob = o["observations"][0]
    assert ob["decision"] == "HOLD" and ob["exit_reasons"] == []              # a halved price is NOT a 50 % loss
    lv = ob["lifecycle"]["levels_used"]
    assert lv["entry_factor"] == pytest.approx(0.5) and lv["invalidation_level"] == pytest.approx(entry["reference_open_price"] * 0.5 * 0.95)
    assert any(w["code"] == "PRICE_BASIS_RESTATED" for w in o["session"]["warnings"])
    lab.record(jid, FL.at(D("2026-10-01")))                                  # max holding -> exit signal
    lab.record(jid, FL.at(D("2026-10-02")))
    ex = next(f for f in lab.fs.fills(jid) if f["fill_type"] == "EXIT")
    raw = m.rows["AMD"]
    assert ex["reference_move_pct"] == pytest.approx((raw[D("2026-10-02")][2] / raw[D("2026-09-29")][2] - 1) * 100)
    assert entry["reference_open_price"] == pytest.approx(raw[D("2026-09-29")][2] * 2)          # stored as captured
    assert abs(ex["reference_move_pct"]) < 10 and ex["price_basis"]["basis"] == "THIS_CAPTURE"


# ---- 51: preflight writes nothing --------------------------------------------------------------------------------------------

def test_preflight_writes_nothing_and_only_probes_the_calendar():
    lab = FL.FLab(FL.Market(**QUIET))
    jid = lab.journal(fspec(), FL.ny(D("2026-09-25"), 12))["journal_id"]
    n0 = lab.store.dataset_count()
    pf = lab.preflight(jid, FL.at(D("2026-09-28")))
    assert pf["status"] == "READY" and pf["session"]["latest_completed"] == "2026-09-28"
    assert pf["session"]["source"].startswith("LIVE_PROBE") and pf["download"]["symbols"] == ["AMD", "MU", "NVDA", "SPY"]
    assert lab.market.calls == [("SPY",)] and lab.store.dataset_count() == n0 and lab.fs.sessions(jid) == []
    lab.record(jid, FL.at(D("2026-09-28")))
    pf2 = lab.preflight(jid, FL.at(D("2026-09-30")))                          # Sep 29 will be MISSED
    assert pf2["continuity"]["will_record_missed"] == ["2026-09-29"] and pf2["continuity"]["status"] == "GAPPED"
    assert any(w["code"] == "MISSED_FORWARD_SESSION" for w in pf2["warnings"]) and len(lab.fs.sessions(jid)) == 1


# ---- 30: whole-market work only when a rule needs it ------------------------------------------------------------------------

def test_breadth_list_is_loaded_only_when_needed_and_needs_match_stage32():
    plain = fspec()
    assert C.price_needs(plain).basket == () and C.price_needs(plain) == SN.needs_for(plain)
    breadth = fspec(entry={"logic": "ALL", "conditions": [{"feature": "market.environment", "op": "!=", "value": "CAUTIOUS"}]})
    assert C.price_needs(breadth).basket == tuple(config.FALLBACK_UNIVERSE) and C.price_needs(breadth) == SN.needs_for(breadth)
    fwd = fspec(entry={"logic": "ALL", "conditions": [{"feature": "research.view", "op": "==", "value": "BULLISH BIAS"},
                                                      {"feature": "market.major_event_within_24h", "op": "is_false"}]})
    n = C.price_needs(fwd)
    assert n.basket == () and "research.view" not in n.stock_features and set(SN.ENTRY_LEVELS) <= set(n.stock_features)
    assert C.forward_only_features(fwd) == {"research": ["research.view"], "event_stock": [],
                                            "event_market": ["market.major_event_within_24h"]}


def test_stored_views_never_recompute(monkeypatch):
    lab = FL.FLab(_cycle_market())
    jid = lab.journal(fspec(symbols=("AMD",)), FL.ny(D("2026-09-25"), 12))["journal_id"]
    lab.record(jid, FL.at(D("2026-09-28")))
    before = (J.journal_view(lab.fs, jid), J.session_view(lab.fs, jid, "2026-09-28"))

    def boom(*a, **k):
        raise AssertionError("recomputed")
    for target in ("technical_snapshot", "research_context", "event_context", "download", "load_series"):
        monkeypatch.setattr(C, target, boom)
    monkeypatch.setattr(SN, "day_snapshot", boom)
    assert (J.journal_view(lab.fs, jid), J.session_view(lab.fs, jid, "2026-09-28")) == before


# ---- 66: additive migration on a copy of the real database and on a Stage 3.2 database -----------------------------------------

def _digest(conn, tables):
    out = {}
    for t in tables:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        h = hashlib.sha256((conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()[0] or "").encode())
        for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY {", ".join(chr(34) + c + chr(34) for c in cols)}'):
            h.update(repr(row).encode())
        out[t] = h.hexdigest()
    return out


def _migrate_and_compare(path):
    from database.forward_migrations import run_forward_migrations
    conn = sqlite3.connect(str(path))
    before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    d0, uv0 = _digest(conn, sorted(before)), conn.execute("PRAGMA user_version").fetchone()[0]
    trig0 = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    run_forward_migrations(conn)
    run_forward_migrations(conn)                                             # idempotent
    after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"forward_test_journals", "forward_test_sessions", "forward_test_observations", "forward_test_reference_fills"} <= after
    assert _digest(conn, sorted(before)) == d0 and conn.execute("PRAGMA user_version").fetchone()[0] == uv0
    trig1 = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    assert all(trig1[k] == v for k, v in trig0.items())
    conn.close()
    return before, after


def test_migration_on_a_copy_of_the_real_database_is_additive_and_idempotent():
    real = ROOT / "data" / "stock_agent.db"
    if not real.exists():
        pytest.skip("no real database")
    tmp = Path(tempfile.mkdtemp(prefix="fw33mig-")) / "copy.db"
    src, dst = sqlite3.connect(f"file:{real.as_posix()}?mode=ro", uri=True), sqlite3.connect(str(tmp))
    src.backup(dst)
    src.close()
    dst.close()
    before, after = _migrate_and_compare(tmp)
    assert {"research_snapshots", "research_outcomes"} <= before


def test_migration_keeps_stage32_backtests_byte_identical(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = X.Lab()
    for s in ("AMD", "MU", "NVDA", "SPY"):
        lab.cache(s, X.walk(s, X.sessions(D("2025-12-01"), D("2026-09-25"))))
    sid = lab.save(fspec())["strategy_id"]
    rid = R.start_run(lab.store, X.body(sid, start="2026-04-01", end="2026-09-25"), now=X.NOW)["run_id"]
    run = lab.store.get_run(rid)
    assert run["status"] == "COMPLETED"
    before, _ = _migrate_and_compare(lab.path)
    assert {"backtest_runs", "backtest_trades", "backtest_signals", "backtest_equity", "historical_daily_bars"} <= before
    assert lab.store.get_run(rid) == run


def test_frozen_modules_do_not_know_about_the_journal():
    for f in ("database/migrations.py", "database/strategy_migrations.py", "database/backtest_migrations.py",
              "backtest/engine.py", "backtest/snapshots.py", "backtest/runs.py", "backtest/store.py", "strategy/features.py",
              "strategy/spec.py", "strategy/evaluate.py"):
        assert "forward_test" not in (ROOT / f).read_text(encoding="utf-8"), f
    assert F.fingerprint() == FROZEN_FINGERPRINT


# ---- 68: security ----------------------------------------------------------------------------------------------------------

FW_FILES = ["forward/__init__.py", "forward/capture.py", "forward/journal.py", "forward/store.py",
            "database/forward_migrations.py", "api/routes/forward_tests.py", "frontend/forward_journal.js"]


def test_no_code_execution_broker_ai_or_scheduler_in_journal_code():
    for f in FW_FILES:
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|new Function|"
                             r"\bpickle\b|marshal|importlib", text), f
        assert not re.search(r"TradingClient|alpaca\.trading|place_?order|submit_?order|cancel_?order|/orders|webhook|"
                             r"anthropic|get_provider\(|from agents|import agents|from portfolio|import portfolio|rh_gateway|"
                             r"robinhood|analysis_cache|setInterval|setTimeout|apscheduler|BackgroundScheduler|add_job|"
                             r"crontab|threading\.Timer", text, re.I), f


def test_forward_endpoints_are_exactly_the_journal_set():
    from api.server import app
    paths = app.openapi()["paths"]
    mine = {p: set(ops) for p, ops in paths.items() if p.startswith("/api/forward-tests")}
    assert mine == {"/api/forward-tests": {"get", "post"}, "/api/forward-tests/{journal_id}": {"get"},
                    "/api/forward-tests/{journal_id}/preflight": {"post"}, "/api/forward-tests/{journal_id}/record": {"post"},
                    "/api/forward-tests/{journal_id}/sessions": {"get"},
                    "/api/forward-tests/{journal_id}/sessions/{session_date}": {"get"},
                    "/api/forward-tests/{journal_id}/archive": {"post"}}
    for p in mine:
        assert not re.search(r"(?i)order|trade|paper|execute|broker|optimi|scan|rank|schedule|backfill", p), p


def test_strategy_text_is_inert_in_the_journal():
    lab = FL.FLab(FL.Market(**QUIET))
    jid = lab.journal(fspec(name="$(rm -rf /); __import__('os').system('x') <script>alert(1)</script>"),
                      FL.ny(D("2026-09-25"), 12))["journal_id"]
    assert lab.record(jid, FL.at(D("2026-09-28")))["status"] == "RECORDED"
    assert J.journal_view(lab.fs, jid)["journal"]["name"].startswith("$(rm")
    js = (ROOT / "frontend" / "forward_journal.js").read_text(encoding="utf-8")
    assert "esc(j.name)" in js and "esc(name)" in js and "eval(" not in js


# ---- API round trip: explicit actions only, 0 Claude, 0 broker ---------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from api.routes import forward_tests as routes
    from api.routes import portfolio as pr
    from api.server import app
    from pf_fixtures import FakeGatewayHttp, fake_provider
    calls = []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append(1))
    http = FakeGatewayHttp()
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    lab = FL.FLab(_cycle_market())
    monkeypatch.setattr(routes, "get_forward_store", lambda: lab.fs)
    monkeypatch.setattr(routes, "get_backtest_store", lambda: lab.store)
    monkeypatch.setattr("data.market_data.fetch_daily_bars", lab.market.fetch)
    monkeypatch.setattr("data.market_data.get_data_client", lambda: object())
    clock = [FL.ny(D("2026-09-25"), 12)]
    monkeypatch.setattr(J, "_now", lambda now=None: (now or clock[0]).astimezone(timezone.utc))
    c = TestClient(app)
    c.lab, c.calls, c.http, c.clock = lab, calls, http, clock
    return c


def test_api_round_trip(api):
    lab = api.lab
    sid = lab.save(fspec(symbols=("AMD",)))["strategy_id"]
    lv = api.get(f"/api/forward-tests?strategy_id={sid}&version_number=1").json()
    assert lv["journals"] == [] and lv["eligibility"]["eligible"] and lab.market.calls == []
    r = api.post("/api/forward-tests", json={"strategy_id": sid, "version_number": 1})
    assert r.status_code == 201 and lab.market.calls == []                  # starting evaluates nothing
    jid = r.json()["journal"]["journal_id"]
    assert api.post("/api/forward-tests", json={"strategy_id": sid, "version_number": 1}).status_code == 409
    assert api.post(f"/api/forward-tests/{jid}/record").status_code == 409  # no eligible session yet
    api.clock[0] = FL.at(D("2026-09-28"))
    pf = api.post(f"/api/forward-tests/{jid}/preflight").json()
    assert pf["status"] == "READY" and len(lab.fs.sessions(jid)) == 0
    rec = api.post(f"/api/forward-tests/{jid}/record").json()
    assert rec["status"] == "RECORDED" and rec["session"]["observations"][0]["decision"] == "ENTER"
    assert api.post(f"/api/forward-tests/{jid}/record").json()["status"] == "ALREADY_RECORDED"
    api.clock[0] = FL.at(D("2026-09-29"))
    api.post(f"/api/forward-tests/{jid}/record")
    v = api.get(f"/api/forward-tests/{jid}").json()
    assert v["summary"]["sessions_captured"] == 2 and v["latest_session"]["session"]["session_date"] == "2026-09-29"
    assert [t["session_date"] for t in api.get(f"/api/forward-tests/{jid}/sessions").json()["timeline"]] == ["2026-09-29", "2026-09-28"]
    s = api.get(f"/api/forward-tests/{jid}/sessions/2026-09-28").json()
    assert s["observations"][0]["feature_snapshot"]["features"] and s["fills_from_signals"][0]["reference_open_price"] == 120.0
    assert api.get(f"/api/forward-tests/{jid}/sessions/2026-09-27").status_code == 404
    assert api.get(f"/api/forward-tests/{jid}/sessions/../x").status_code == 404
    assert api.get("/api/forward-tests/" + "z" * 32).status_code == 404
    assert api.post(f"/api/forward-tests/{jid}/record", json={"backfill": "2026-09-01"}).status_code == 422   # strict body
    assert api.post("/api/forward-tests", json={"strategy_id": sid, "version_number": 1, "auto": True}).status_code == 422
    assert api.post(f"/api/forward-tests/{jid}/archive").json()["journal"]["status"] == "ARCHIVED"
    assert api.post(f"/api/forward-tests/{jid}/record").json()["status"] == "JOURNAL_ARCHIVED"
    assert api.calls == [] and api.http.requests == []                       # no Claude, no gateway


def test_ui_hooks_and_neutral_language():
    js = (ROOT / "frontend" / "forward_journal.js").read_text(encoding="utf-8")
    sl = (ROOT / "frontend" / "strategy_lab.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\bbuy\b|\bsell\b|trade now|recommended|best strategy|profit opportunity|profitable|winning strategy|"
                         r"losing strategy|probabilit|confidence|score", js, re.I)
    assert "No order was placed" in js and "No broker · No orders · Manual capture only" in js
    assert "Past sessions cannot be added later" in js and "REFERENCE FILL" in js and "STRICT FORWARD" in js
    assert "POST-CLOSE FORWARD CONTEXT" in js and "MISSED SESSION" in js and "(descriptive)" in js
    assert 'id="fj-body"' in html and html.index("backtest_lab.js") < html.index("forward_journal.js") and "forward_journal.css" in html
    assert 'data-top="forward"' in sl and "window.ForwardJournal.select(btRef())" in sl and "window.BacktestLab.select(btRef())" in sl
    assert "setInterval" not in js and "setTimeout" not in js                # manual only: no polling, no timers
