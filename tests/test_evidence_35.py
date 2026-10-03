"""Stage 3.5: historical vs forward EVIDENCE COMPARISON (stored evidence only; exact version isolation; completed
reference cycles from stored rows; descriptive differences; read-only — no writes, no market data, no AI, no broker)."""
import copy
import hashlib
import json
import re
import socket
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bt_fixtures as X
import config
import fw_fixtures as FL
from backtest import runs as R
from backtest.store import BacktestStore
from comparison import cycles as CY
from comparison import view as V
from fit import readonly as RO
from forward import journal as J
from forward.store import ForwardStore
from strategy import spec as S
from strategy.store import StrategyStore

ROOT = Path(__file__).resolve().parents[1]
D = date.fromisoformat
QUIET = {"vol": 0.002}
UP3 = {"logic": "ALL", "conditions": [{"feature": "stock.change_1d_pct", "op": ">", "value": 3}]}
NEVER = {"logic": "ALL", "conditions": [{"feature": "stock.close", "op": "<", "value": 0}]}
TREND = {"logic": "ALL", "conditions": [{"feature": "stock.trend", "op": "==", "value": "UPTREND"}]}
HOLD2 = {"logic": "ANY", "conditions": [], "invalidation": {"method": "PCT_BELOW_ENTRY", "pct": 20},
         "target": {"method": "PCT_ABOVE_ENTRY", "pct": 50}, "max_holding_days": 2}
SEPT = ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30",
        "2026-10-01", "2026-10-02")
FROZEN_MODULES = "02d5c400eaebc53469e2873c65c7ca0ef837ec800d61f3c52d5c34505426e9da"       # Stage 3.1-3.3 (Stage 3.6 re-pin, as in 3.4)
FROZEN_FIT_34 = "661153e75986c3f11a760bf0bd2d722b0772ff3e0b958adb068d51188235ff1c"   # Stage 3.4 Python + CSS (Stage 4.0 re-pin: fit/current.py shares its session / missing-data helpers with the scanner — pure refactor, Strategy Fit output byte-identical on 4 DBs)


def fspec(entry=None, exit_=None, symbols=("AMD", "MU", "NVDA"), name="Evidence test"):
    s = X.spec(symbols=symbols, entry=entry or UP3, exit_=exit_ or HOLD2,
               risk={"max_position_pct": 10, "max_open_positions": 50}, name=name)
    n, err = S.validate(s)
    assert not err, err
    return n


def jump(m, sym, d, pct=5.0):
    pc = m.rows[sym][m.days[m.days.index(d) - 1]][5]
    m.set_bar(sym, d, pc, pc * (1 + pct / 100) * 1.001, pc * 0.999, pc * (1 + pct / 100))


def vid_of(lab, sid, n=1):
    with sqlite3.connect(lab.path) as c:
        return c.execute("SELECT version_id FROM strategy_versions WHERE strategy_id = ? AND version_number = ?", (sid, n)).fetchone()[0]


def cached_lab(market=None):
    lab = FL.FLab(market or FL.Market(("AMD", "MU", "NVDA", "SPY"), start=date(2024, 11, 1), vol=0.004))
    for s in ("AMD", "MU", "NVDA", "SPY"):
        rows = [lab.market.rows[s][d] for d in sorted(lab.market.rows[s]) if d <= D("2026-09-25")]
        lab.cache(s, rows, requested_start="2024-11-01", requested_end="2026-09-25")
    return lab


def record_all(lab, jid, days):
    for d in days:
        lab.record(jid, FL.at(D(d)))


def raw(lab):
    c = sqlite3.connect(lab.path)
    c.execute("PRAGMA foreign_keys = ON")
    return c


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


@pytest.fixture(scope="module")
def rich():
    """One version with two stored runs and a 10-session journal; a second version with its own journal."""
    old = R.RUN_INLINE
    R.RUN_INLINE = True
    try:
        lab = cached_lab()
        sp = fspec(entry=TREND, name="Swing")
        sid = lab.save(sp)["strategy_id"]
        ra = R.start_run(lab.store, X.body(sid, start="2025-06-02", end="2026-09-25", slippage_bps_per_side=3.0), now=X.NOW)["run_id"]
        rb = R.start_run(lab.store, X.body(sid, start="2026-01-05", end="2026-09-25", slippage_bps_per_side=5.0,
                                           commission_per_order=1.0), now=X.NOW)["run_id"]
        jid = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(D("2026-09-18"), 12))["journal_id"]
        record_all(lab, jid, SEPT)
        lab.strategies.add_version(sid, fspec(entry=UP3, name="Swing"))
        j2 = J.create_journal(lab.fs, lab.store, sid, 2, now=FL.ny(D("2026-09-18"), 12))["journal_id"]
        record_all(lab, j2, SEPT[:3])
        other = lab.save(fspec(entry=TREND, name="Swing"))["strategy_id"]          # same name, different strategy
        ro = R.start_run(lab.store, X.body(other, start="2026-01-05", end="2026-09-25"), now=X.NOW)["run_id"]
    finally:
        R.RUN_INLINE = old
    lab.ids = {"sid": sid, "v1": vid_of(lab, sid, 1), "v2": vid_of(lab, sid, 2), "ra": ra, "rb": rb, "j1": jid, "j2": j2,
               "other": vid_of(lab, other, 1), "ro": ro}
    return lab


def view(lab, vid, run=None, journal=None):
    return V.view(vid, run, journal, path=lab.path)


# ---- 58: EXACT VERSION ISOLATION --------------------------------------------------------------------------------------------

def test_58_exact_version_joins_and_mismatch_is_rejected(rich):
    ids = rich.ids
    v = view(rich, ids["v1"], ids["ra"], ids["j1"])
    assert v["historical"]["status"] == "AVAILABLE" and v["forward"]["status"] == "AVAILABLE"
    assert v["identity"]["strategy_version_id"] == ids["v1"] and v["historical"]["run"]["run_id"] == ids["ra"]
    assert v["forward"]["journal"]["journal_id"] == ids["j1"]
    assert all(c["ok"] for c in v["historical"]["checks"]) and all(c["ok"] for c in v["forward"]["checks"])
    for run, journal in ((ids["ra"], ids["j2"]), (ids["ro"], ids["j1"]), (None, ids["j2"])):
        with pytest.raises(V.CompareError) as e:
            view(rich, ids["v1"], run, journal)
        assert e.value.code == "EVIDENCE_VERSION_MISMATCH" and e.value.status == 409
    v2 = view(rich, ids["v2"])                                   # v2: its own journal, no runs (never v1's)
    assert v2["historical"]["status"] == "NO_BACKTEST" and v2["forward"]["journal"]["journal_id"] == ids["j2"]
    assert [r["run_id"] for r in v2["selection"]["runs"]] == []
    vo = view(rich, ids["other"])                                # same NAME, different strategy: its own evidence only
    assert vo["historical"]["run"]["run_id"] == ids["ro"] and vo["forward"]["status"] == "NO_JOURNAL"
    with pytest.raises(V.CompareError) as e:
        view(rich, "f" * 32)
    assert e.value.code == "NOT_FOUND" and e.value.status == 404
    with pytest.raises(V.CompareError) as e:
        view(rich, ids["v1"], "e" * 32)
    assert e.value.code == "NOT_FOUND"


def test_hash_disagreement_is_evidence_integrity_error_and_sides_are_isolated(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = cached_lab()
    sid = lab.save(fspec(entry=TREND, name="Tamper"))["strategy_id"]
    run = R.start_run(lab.store, X.body(sid, start="2026-03-02", end="2026-09-25"), now=X.NOW)["run_id"]
    jid = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(D("2026-09-18"), 12))["journal_id"]
    record_all(lab, jid, SEPT[:4])
    vid = vid_of(lab, sid)
    with raw(lab) as c:                                          # a forged run hash (triggers dropped only for the test)
        c.execute("DROP TRIGGER backtest_runs_fixed_fields")
        c.execute("DROP TRIGGER backtest_runs_final")
        c.execute("UPDATE backtest_runs SET rules_hash = ? WHERE run_id = ?", ("a" * 64, run))
    v = view(lab, vid)
    assert v["historical"]["status"] == "EVIDENCE_INTEGRITY_ERROR" and "metrics" not in v["historical"]
    assert [c["code"] for c in v["historical"]["checks"] if not c["ok"]] == ["RUN_RULES_HASH"]
    assert v["forward"]["status"] == "AVAILABLE" and v["comparison"]["available"] is False          # forward still shown
    assert all(r["difference"] is None for r in v["comparison"]["compatible_metrics"])
    with raw(lab) as c:
        c.execute("DROP TRIGGER forward_test_journals_fixed_fields")
        c.execute("UPDATE forward_test_journals SET spec_hash = ? WHERE journal_id = ?", ("b" * 64, jid))
    v = view(lab, vid)
    assert v["forward"]["status"] == "EVIDENCE_INTEGRITY_ERROR" and "cycles" not in v["forward"]
    assert v["historical"]["status"] == "EVIDENCE_INTEGRITY_ERROR"


def test_forged_forward_move_or_duplicate_rows_are_integrity_errors():
    m = FL.Market(("AMD", "SPY"), **QUIET)
    jump(m, "AMD", D("2026-09-28"))
    lab = FL.FLab(m)
    jid = lab.journal(fspec(symbols=("AMD",)), FL.ny(D("2026-09-25"), 12))["journal_id"]
    record_all(lab, jid, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))
    obs, fills = lab.fs.observations(jid, compact=True), lab.fs.fills(jid)
    assert [c["status"] for c in CY.derive(obs, fills, [])] == ["COMPLETED"]
    with pytest.raises(CY.CycleIntegrityError, match="Two ENTER"):
        CY.derive(obs + [o for o in obs if o["decision"] == "ENTER"], fills, [])
    with pytest.raises(CY.CycleIntegrityError, match="Two reference EXIT"):
        CY.derive(obs, fills + [f for f in fills if f["fill_type"] == "EXIT"], [])
    with pytest.raises(CY.CycleIntegrityError, match="without its ENTER"):
        CY.derive([o for o in obs if o["decision"] != "ENTER"], fills, [])
    forged = [{**f, "reference_move_pct": f["reference_move_pct"] + 1.0} if f["fill_type"] == "EXIT" else f for f in fills]
    with pytest.raises(CY.CycleIntegrityError, match="stored reference move differs"):
        CY.derive(obs, forged, [])
    with raw(lab) as c:
        c.execute("DROP TRIGGER forward_test_reference_fills_no_update")
        c.execute("UPDATE forward_test_reference_fills SET reference_move_pct = 99 WHERE journal_id = ? AND fill_type = 'EXIT'", (jid,))
    v = V.view(vid_of(lab, lab.fs.journal(jid)["strategy_id"]), path=lab.path)
    assert v["forward"]["status"] == "EVIDENCE_INTEGRITY_ERROR"
    assert any(c["code"] == "REFERENCE_CYCLES" and not c["ok"] for c in v["forward"]["checks"])


def test_untrusted_version_refuses_the_whole_view():
    lab = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    import uuid
    sp = fspec(symbols=("AMD",), name="Forged")
    sid, vid = uuid.uuid4().hex, uuid.uuid4().hex
    with sqlite3.connect(lab.path) as c:
        c.execute("INSERT INTO strategy_definitions (strategy_id, name, created_at) VALUES (?,?,?)", (sid, "Forged", "2026-09-01"))
        c.execute("INSERT INTO strategy_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (vid, sid, 1, 1, 1, sp["feature_registry_fingerprint"], S.canonical_json(sp), "c" * 64, S.rules_hash(sp),
                   "BACKTEST_READY", "2026-09-01", None))
    with pytest.raises(V.CompareError) as e:
        V.view(vid, path=lab.path)
    assert e.value.code == "EVIDENCE_INTEGRITY_ERROR" and e.value.status == 409


# ---- 59: MULTIPLE HISTORICAL RUNS — the selected one, never combined or chosen by return ------------------------------------

def test_59_selected_run_is_shown_never_aggregated_or_picked_by_return(rich):
    ids = rich.ids
    a, b = rich.store.get_run(ids["ra"])["result"]["metrics"], rich.store.get_run(ids["rb"])["result"]["metrics"]
    va = view(rich, ids["v1"], ids["ra"])
    assert va["historical"]["selection"] == "USER_SELECTED" and va["historical"]["run"]["run_id"] == ids["ra"]
    for k in V.METRIC_KEYS:
        assert va["historical"]["metrics"][k] == a[k], k
    assert va["historical"]["assumptions"]["slippage_bps_per_side"] == 3.0
    d = view(rich, ids["v1"])                                      # default: the MOST RECENT completed run (rb)
    assert d["historical"]["selection"] == "MOST_RECENT_COMPLETED" and d["historical"]["run"]["run_id"] == ids["rb"]
    assert d["historical"]["metrics"]["total_return_pct"] == b["total_return_pct"]
    assert d["historical"]["assumptions"]["commission_per_order"] == 1.0
    assert [r["run_id"] for r in d["selection"]["runs"]] == [ids["rb"], ids["ra"]]           # most recent first
    assert set(d["historical"]["metrics"]) == set(V.METRIC_KEYS)                           # one run's own values only
    assert "best_run" not in json.dumps(d) and "aggregate" not in json.dumps(d["historical"]["metrics"])


# ---- 7 / 8 / 9 / 60 / 61 / 62: no journal, no backtest, empty journal, zero / one / open cycle --------------------------------

def test_no_journal_and_no_backtest_states(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = cached_lab()
    sid = lab.save(fspec(entry=TREND, name="Only hist"))["strategy_id"]
    R.start_run(lab.store, X.body(sid, start="2026-03-02", end="2026-09-25"), now=X.NOW)
    v = view(lab, vid_of(lab, sid))
    assert v["forward"]["status"] == "NO_JOURNAL" and v["forward"]["message"] == "No forward journal yet."
    assert "captured_sessions" not in v["forward"] and "completed_cycles" not in v["forward"]          # no invented zeros
    assert v["historical"]["status"] == "AVAILABLE" and v["comparison"]["available"] is False
    fo = lab.save(fspec(entry={"logic": "ALL", "conditions": [{"feature": "research.view", "op": "==", "value": "BULLISH BIAS"}]},
                        name="Forward only"))["strategy_id"]
    jid = J.create_journal(lab.fs, lab.store, fo, 1, now=FL.ny(D("2026-09-18"), 12))["journal_id"]
    record_all(lab, jid, SEPT[:2])
    v = view(lab, vid_of(lab, fo))
    h = v["historical"]
    assert h["status"] == "UNAVAILABLE_FOR_VERSION" and h["message"] == "Unavailable for this strategy version."
    assert "FORWARD TEST ONLY" in h["reason"] and v["identity"]["readiness"] == "FORWARD_TEST_ONLY"
    assert v["forward"]["status"] == "AVAILABLE" and v["forward"]["captured_sessions"] == 2


def test_empty_journal_is_valid_and_shows_no_performance():
    lab = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    jid = lab.journal(fspec(symbols=("AMD",), name="Empty"), FL.ny(D("2026-09-25"), 12))["journal_id"]
    v = V.view(vid_of(lab, lab.fs.journal(jid)["strategy_id"]), path=lab.path)
    f = v["forward"]
    assert f["status"] == "AVAILABLE" and f["captured_sessions"] == 0 and f["completed_cycles"] == 0
    assert f["reference_cycle_metrics"] is None and f["sample"]["code"] == "NO_COMPLETED_FORWARD_CYCLES"
    assert f["continuity"] == "NOT_STARTED" and f["timeline"] == []


def test_60_zero_forward_cycles_has_no_average_and_no_zero_percent():
    lab = FL.FLab(FL.Market(("AMD", "MU", "SPY"), **QUIET))
    jid = lab.journal(fspec(entry=NEVER, symbols=("AMD", "MU"), name="Never"), FL.ny(D("2026-09-18"), 12))["journal_id"]
    record_all(lab, jid, SEPT)
    v = V.view(vid_of(lab, lab.fs.journal(jid)["strategy_id"]), path=lab.path)
    f = v["forward"]
    assert f["captured_sessions"] == 10 and f["completed_cycles"] == 0 and f["reference_cycle_metrics"] is None
    assert f["sample"]["code"] == "NO_COMPLETED_FORWARD_CYCLES"
    for r in v["comparison"]["compatible_metrics"]:
        if r["key"] in ("average_return", "median_return", "positive_outcomes", "average_holding", "median_holding"):
            assert r["forward"] is None and r["forward_display"] is None and r["difference"] is None, r["key"]
    for s in v["symbols"]:
        assert s["forward"]["completed_cycles"] == 0 and s["forward"]["average_reference_move_pct"] is None
        assert s["forward"]["observations"] == 10 and s["forward"]["latest_state"] == "FLAT"   # 27: shown, no return forced


def _one_cycle_market():
    m = FL.Market(("AMD", "SPY"), **QUIET)
    jump(m, "AMD", D("2026-09-28"))                                    # ENTER at Monday's close
    m.set_bar("AMD", D("2026-09-29"), 100.0, 101.0, 99.0, 100.5)       # reference entry = Tuesday's open = 100
    m.set_bar("AMD", D("2026-09-30"), 100.5, 101.5, 100.0, 101.0)      # holding 2 -> EXIT at Wednesday's close
    m.set_bar("AMD", D("2026-10-01"), 105.0, 106.0, 101.0, 101.5)      # reference exit = Thursday's open = 105
    return m


def test_61_one_forward_cycle_exact_arithmetic():
    lab = FL.FLab(_one_cycle_market())
    jid = lab.journal(fspec(symbols=("AMD",), name="One"), FL.ny(D("2026-09-25"), 12))["journal_id"]
    record_all(lab, jid, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))
    f = V.view(vid_of(lab, lab.fs.journal(jid)["strategy_id"]), path=lab.path)["forward"]
    (c,) = f["cycles"]
    assert (c["status"], c["reference_entry_open"], c["reference_exit_open"]) == ("COMPLETED", 100.0, 105.0)
    assert c["reference_move_pct"] == (105.0 / 100.0 - 1) * 100 == c["stored_reference_move_pct"]
    assert V.r2(c["reference_move_pct"]) == 5.0 and c["holding_sessions"] == 2 and c["exit_reasons"] == ["MAX_HOLDING"]
    assert (c["entry_signal_session"], c["reference_entry_session"], c["exit_signal_session"], c["reference_exit_session"]) == \
        ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01")
    m = f["reference_cycle_metrics"]
    assert m["completed_cycles"] == 1 and m["positive_cycles"] == 1 and m["positive_cycle_rate_pct"] == 100.0
    assert m["average_reference_move_pct"] == m["median_reference_move_pct"] == c["reference_move_pct"]
    assert f["sample"]["code"] == "VERY_SMALL_FORWARD_SAMPLE" and f["sample"]["text"].startswith("VERY SMALL FORWARD SAMPLE — 1 ")
    assert f["exit_reasons"]["groups"] == [{"group": "MAX_HOLDING", "completed_cycles": 1}]
    assert "pnl" not in json.dumps(f).lower() and "equity" not in json.dumps({k: f[k] for k in ("cycles", "reference_cycle_metrics")})


def test_62_open_cycle_is_counted_but_excluded_from_averages():
    lab = FL.FLab(_one_cycle_market())
    jid = lab.journal(fspec(symbols=("AMD",), name="Open"), FL.ny(D("2026-09-25"), 12))["journal_id"]
    record_all(lab, jid, ("2026-09-28", "2026-09-29"))                  # entry filled Tuesday, no exit yet
    f = V.view(vid_of(lab, lab.fs.journal(jid)["strategy_id"]), path=lab.path)["forward"]
    assert f["open_cycles"] == 1 and f["completed_cycles"] == 0 and f["reference_cycle_metrics"] is None
    (c,) = f["cycles"]
    assert c["status"] == "OPEN_REFERENCE_CYCLE" and c["reference_entry_open"] == 100.0 and c["reference_move_pct"] is None
    assert f["symbol_breakdown"]["AMD"]["open_cycle"]["status"] == "OPEN_REFERENCE_CYCLE"
    assert f["states"]["open_shadow_states"] == 1


# ---- 63 / 64 / 50: gaps, blocks, archived and blocked journals ----------------------------------------------------------------

def test_63_gapped_journal_is_shown_as_stored_nothing_reconstructed():
    lab = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    jid = lab.journal(fspec(entry=NEVER, symbols=("AMD",), name="Gap"), FL.ny(D("2026-09-25"), 12))["journal_id"]
    record_all(lab, jid, ("2026-09-28", "2026-09-30"))                  # Tuesday Sep 29 missed
    before = lab.fs.raw_rows(jid)
    f = V.view(vid_of(lab, lab.fs.journal(jid)["strategy_id"]), path=lab.path)["forward"]
    assert (f["continuity"], f["captured_sessions"], f["missed_sessions"]) == ("GAPPED", 2, 1)
    assert [(s["session_date"], s["kind"]) for s in f["timeline"]] == [("2026-09-28", "CAPTURED"), ("2026-09-29", "MISSED"),
                                                                        ("2026-09-30", "CAPTURED")]
    assert f["timeline"][1]["groups"] == {} and f["timeline"][1]["fills"] == []
    assert lab.fs.raw_rows(jid) == before and f["continuity_complete"] is False


def test_64_continuity_blocked_symbol_is_shown_without_inferring_its_lifecycle():
    m = FL.Market(("AMD", "MU", "SPY"), **QUIET)
    jump(m, "AMD", D("2026-09-28"))
    lab = FL.FLab(m)
    jid = lab.journal(fspec(symbols=("AMD", "MU"), name="Block"), FL.ny(D("2026-09-25"), 12))["journal_id"]
    record_all(lab, jid, ("2026-09-28", "2026-09-30", "2026-10-01"))    # Sep 29 missed while AMD was ENTRY_PENDING
    v = V.view(vid_of(lab, lab.fs.journal(jid)["strategy_id"]), path=lab.path)
    f = v["forward"]
    assert f["journal_status"] == "CONTINUITY_BLOCKED" and f["continuity"] == "CONTINUITY_BLOCKED"
    assert f["states"]["blocked_symbols"] == ["AMD"] and f["blocked_cycles"] == 1 and f["completed_cycles"] == 0
    (c,) = [c for c in f["cycles"] if c["symbol"] == "AMD"]
    assert c["status"] == "INCOMPLETE_CONTINUITY_BLOCKED" and c["reference_entry_open"] is None and c["reference_exit_open"] is None
    assert f["symbol_breakdown"]["AMD"]["blocked"] is True and f["symbol_breakdown"]["MU"]["blocked"] is False
    assert any(n["code"] == "FORWARD_CONTINUITY_BLOCKED" for n in v["quality_notes"])


def test_archived_journals_stay_viewable_and_selection_is_active_first():
    lab = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    sp = fspec(entry=NEVER, symbols=("AMD",), name="Arch")
    first = lab.journal(sp, FL.ny(D("2026-09-18"), 12))["journal_id"]
    sid = lab.fs.journal(first)["strategy_id"]
    record_all(lab, first, SEPT[:2])
    J.archive_journal(lab.fs, first, now=FL.ny(D("2026-09-23"), 12))
    vid = vid_of(lab, sid)
    v = V.view(vid, path=lab.path)                                     # only archived -> most recent archived, labelled
    assert v["selection"]["journal_selection"] == "MOST_RECENT_ARCHIVED" and v["forward"]["journal_status"] == "ARCHIVED"
    second = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(D("2026-09-24"), 12))["journal_id"]
    record_all(lab, second, ("2026-09-25",))
    v = V.view(vid, path=lab.path)
    assert v["selection"]["journal_selection"] == "CURRENT_JOURNAL" and v["forward"]["journal"]["journal_id"] == second
    assert [j["journal_id"] for j in v["selection"]["journals"]] == [second, first]          # active first, then newest
    a = V.view(vid, None, first, path=lab.path)["forward"]
    assert a["journal_status"] == "ARCHIVED" and a["captured_sessions"] == 2                  # never combined with `second`


# ---- 65 / 69 / 70 / 71: no reconstruction, no market data, no AI, no broker -------------------------------------------------

def test_65_69_70_no_mfe_reconstruction_no_market_data_no_ai(rich, monkeypatch):
    import agents.technical_agent as ta
    from portfolio import explain as ex
    calls = []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append("ai"))
    monkeypatch.setattr(ex, "get_provider", lambda: calls.append("ai"))
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network / market data"))    # noqa: E731
    monkeypatch.setattr("data.market_data.fetch_daily_bars", boom)
    monkeypatch.setattr("data.market_data.get_data_client", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    n = len(rich.market.calls)
    from forward import excursions as XC                     # Stage 3.6: this journal was recorded with MFE / MAE tracking
    j1 = rich.ids["j1"]
    stored = {(s["symbol"], s["cycle_no"]): s for s in XC.cycle_summaries(rich.fs.excursions(j1), rich.fs.fills(j1),
                                                                             rich.fs.excursion_tracking(j1))}
    for run, journal in ((None, None), (rich.ids["ra"], None), (rich.ids["rb"], rich.ids["j1"])):
        v = view(rich, rich.ids["v1"], run, journal)
        xm = v["forward"]["excursion_metrics"]
        assert v["forward"]["mfe_mae"]["tracked"] == (xm["tracked_completed_cycles"] > 0)
        rows = {r["key"]: r for r in v["comparison"]["compatible_metrics"]}
        assert rows["mfe"]["forward"] == (xm["average_mfe_pct"] if xm["tracked_completed_cycles"] else None)
        assert rows["mfe"]["historical"] is not None
        for c in v["forward"]["cycles"]:                      # forward MFE / MAE only ever come from STORED rows
            s = stored.get((c["symbol"], c["cycle_no"]))
            if s:
                assert (c["excursion"]["mfe_pct"], c["excursion"]["mae_pct"]) == (s["mfe_pct"], s["mae_pct"])
    V.public_config(rich.path)
    assert len(rich.market.calls) == n and calls == []


# ---- 66 / 67 / 22 / 23: differences, rounding, no false equivalence --------------------------------------------------------------

def test_66_difference_is_forward_minus_historical_in_percentage_points_with_half_up_rounding():
    assert V.difference(3.20, 5.81) == -2.61 and V._pp_text(V.difference(3.20, 5.81), "pp") == "−2.61 percentage points"
    assert V.difference(3.204, 5.806) == -2.61                         # from the displayed values 3.20 and 5.81
    assert V.r2(0.125) == 0.13 and V.r2(-0.125) == -0.13 and V.r2(2.675) == 2.68 and V.r2(1.005) == 1.01
    assert V.difference(5.81, 5.81) == 0.0 and V._pp_text(0.0, "pp") == "0.00 percentage points"
    assert V.difference(None, 5.81) is None and V.difference(3.2, None) is None
    hist = {"status": "AVAILABLE", "metrics": {"closed_trades": 7, "average_trade_return_pct": 5.81, "median_trade_return_pct": 5.72,
                                               "win_rate_pct": 100.0, "winning_trades": 7, "average_holding_days": 2.0,
                                               "median_holding_days": 2.0, "average_mfe_pct": 6.83, "average_mae_pct": -1.5,
                                               "open_positions_at_end": 0}}
    fwd = {"status": "AVAILABLE", "completed_cycles": 2, "open_cycles": 1, "continuity": "CONTINUOUS",
           "reference_cycle_metrics": {"average_reference_move_pct": 3.2, "median_reference_move_pct": 2.9,
                                       "positive_cycle_rate_pct": 50.0, "positive_cycles": 1, "completed_cycles": 2,
                                       "average_holding_sessions": 3.0, "median_holding_sessions": 3.0}}
    rows = {r["key"]: r for r in V.compare(hist, fwd)["compatible_metrics"]}
    assert rows["average_return"]["difference"] == -2.61 and rows["average_return"]["difference_unit"] == "pp"
    assert rows["median_return"]["difference"] == -2.82 and rows["positive_outcomes"]["difference"] == -50.0
    assert rows["positive_outcomes"]["forward_count"] == {"positive": 1, "of": 2}
    assert rows["average_holding"]["difference"] == 1.0 and rows["average_holding"]["difference_text"] == "+1.00 sessions"
    assert rows["sample"]["difference"] is None and rows["open_at_end"]["difference"] is None
    txt = V.compare(hist, fwd)["summary_text"]
    assert txt == ("Forward average reference move differs from the selected historical run's average trade return by "
                   "−2.61 percentage points.")
    assert not re.search(r"(?i)better|worse|improv|degrad|outperform|underperform|% worse|% better", json.dumps(V.compare(hist, fwd)))


def test_67_only_same_meaning_measures_are_paired(rich):
    c = view(rich, rich.ids["v1"])["comparison"]
    keys = [r["key"] for r in c["compatible_metrics"]]
    assert keys == ["sample", "average_return", "median_return", "positive_outcomes", "average_holding", "median_holding",
                    "mfe", "mae", "continuity", "open_at_end"]
    avg = c["compatible_metrics"][1]
    assert (avg["historical_measure"], avg["forward_measure"]) == ("average closed-trade return", "average reference move")
    for r in c["compatible_metrics"]:
        assert "total" not in (r["historical_measure"] or "") and "equity" not in (r["historical_measure"] or "")
    assert {x["key"] for x in c["not_compared"]} >= {"total_return_pct", "max_drawdown_pct", "profit_factor",
                                                     "annualized_return_pct", "time_in_market_pct", "costs", "entry_frequency"}
    js = (ROOT / "frontend" / "evidence.js").read_text(encoding="utf-8")
    compare_fn = js[js.index("function drawCompare"):js.index("// ---- historical card")]
    assert "total_return" not in compare_fn and "compatible_metrics" in compare_fn


# ---- 52 / 12 / 19 / 25-29: counts once, forward summary, timing, symbols, exit reasons, entry frequency ------------------------

def test_52_each_stored_row_counts_once_and_matches_the_journal_summary(rich):
    f = view(rich, rich.ids["v1"])["forward"]
    fills = rich.fs.fills(rich.ids["j1"])
    exits = [x for x in fills if x["fill_type"] == "EXIT" and x["status"] == "FILLED"]
    summ = J.journal_view(rich.fs, rich.ids["j1"])["summary"]
    assert f["completed_cycles"] == len(exits) == summ["completed_reference_cycles"]
    keys = [(c["symbol"], c["cycle_no"]) for c in f["cycles"]]
    assert len(keys) == len(set(keys))
    assert (f["decision_counts"]["ENTER"], f["decision_counts"]["EXIT"], f["decision_counts"]["HOLD"], f["decision_counts"]["SKIP"]) == \
        (summ["enter_signals"], summ["exit_signals"], summ["hold_decisions"], summ["skip_decisions"])
    assert f["captured_sessions"] == summ["sessions_captured"] == 10 and f["missed_sessions"] == 0
    if f["reference_cycle_metrics"]:
        moves = sorted(x["reference_move_pct"] for x in exits)
        assert sorted(f["reference_cycle_metrics"]["reference_moves_pct"]) == moves
        assert f["reference_cycle_metrics"]["average_reference_move_pct"] == pytest.approx(summ["average_completed_reference_move_pct"])
    assert sum(g["completed_cycles"] for g in f["exit_reasons"]["groups"]) == f["completed_cycles"]


def test_19_timing_breakdown_uses_stage33_labels_separately():
    lab = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    lab.research.add("AMD", FL.ny(D("2026-09-28"), 11))
    sp = fspec(entry={"logic": "ALL", "conditions": [{"feature": "research.freshness", "op": "==", "value": "FRESH"}]},
               symbols=("AMD",), name="Timing")
    jid = lab.journal(sp, FL.ny(D("2026-09-25"), 12))["journal_id"]
    lab.record(jid, FL.ny(D("2026-09-29"), 13))                       # after the next open -> POST + CAPTURED_AFTER_NEXT_OPEN
    lab.record(jid, FL.at(D("2026-09-29")))                            # before the next open -> POST only
    f = V.view(vid_of(lab, lab.fs.journal(jid)["strategy_id"]), path=lab.path)["forward"]
    t = f["timing_breakdown"]
    assert (t["strict_forward"], t["post_close_forward_context"], t["captured_after_next_open"]) == (0, 2, 1)
    assert set(t["labels"]) == {"STRICT_FORWARD", "POST_CLOSE_FORWARD_CONTEXT", "CAPTURED_AFTER_NEXT_OPEN"}
    assert [s["captured_after_next_open"] for s in f["timeline"]] == [True, False]
    price = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    pj = price.journal(fspec(entry=NEVER, symbols=("AMD",), name="Strict"), FL.ny(D("2026-09-25"), 12))["journal_id"]
    price.record(pj, FL.at(D("2026-09-28")))
    t2 = V.view(vid_of(price, price.fs.journal(pj)["strategy_id"]), path=price.path)["forward"]["timing_breakdown"]
    assert (t2["strict_forward"], t2["post_close_forward_context"], t2["captured_after_next_open"]) == (1, 0, 0)


def test_25_to_29_symbols_alphabetical_both_sides_separate_counts(rich):
    v = view(rich, rich.ids["v1"])
    syms = [s["symbol"] for s in v["symbols"]]
    assert syms == sorted(syms) == ["AMD", "MU", "NVDA"]
    h, f = v["historical"], v["forward"]
    for s in v["symbols"]:
        hs = h["symbol_breakdown"][s["symbol"]]
        stored = {g["group"]: g for g in rich.store.get_run(rich.ids["rb"])["result"]["breakdowns"]["symbol"]}
        assert hs["closed_trades"] == stored.get(s["symbol"], {}).get("closed_trades", 0)
        assert s["forward"]["observations"] == 10 and "completed_cycles" in s["forward"]
    assert h["exit_reasons"]["groups"] == [{"group": g["group"], "closed_trades": g["closed_trades"]}
                                           for g in rich.store.get_run(rich.ids["rb"])["result"]["breakdowns"]["exit_reason"]]
    assert h["exit_reasons"]["scope"] != f["exit_reasons"]["scope"]                              # never merged
    assert h["entry_frequency"]["entry_signals"] == rich.store.get_run(rich.ids["rb"])["result"]["audit"]["signals_considered"]
    assert f["entry_frequency"]["enter_decisions"] == f["decision_counts"]["ENTER"]
    assert "annualized" not in json.dumps({k: f[k] for k in f if k not in ("timing_breakdown", "assumptions", "note")})


def test_zero_historical_trade_symbol_is_still_listed(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = cached_lab()
    sid = lab.save(fspec(entry=NEVER, symbols=("AMD", "MU"), name="Zero"))["strategy_id"]
    R.start_run(lab.store, X.body(sid, start="2026-03-02", end="2026-09-25"), now=X.NOW)
    jid = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(D("2026-09-18"), 12))["journal_id"]
    record_all(lab, jid, SEPT[:5])
    v = view(lab, vid_of(lab, sid))
    for s in v["symbols"]:
        assert s["historical"]["closed_trades"] == 0 and s["historical"]["average_return_pct"] is None
        assert s["forward"]["observations"] == 5 and s["forward"]["completed_cycles"] == 0
    assert v["historical"]["sample"]["code"] == "NO_TRADES"


# ---- 68: READ ONLY ----------------------------------------------------------------------------------------------------------------

def test_68_evidence_comparison_writes_nothing(rich, monkeypatch):
    checkpoint(rich.path)
    file0, dig0 = hashlib.sha256(Path(rich.path).read_bytes()).hexdigest(), db_digest(rich.path)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("a writable store was opened"))  # noqa: E731
    monkeypatch.setattr(BacktestStore, "__init__", boom)
    monkeypatch.setattr(ForwardStore, "__init__", boom)
    monkeypatch.setattr(StrategyStore, "__init__", boom)
    ids = rich.ids
    for vid, run, journal in ((ids["v1"], None, None), (ids["v1"], ids["ra"], None), (ids["v1"], ids["rb"], ids["j1"]),
                              (ids["v2"], None, None), (ids["other"], None, None), (ids["v1"], None, None)):
        view(rich, vid, run, journal)
    V.public_config(rich.path)
    assert hashlib.sha256(Path(rich.path).read_bytes()).hexdigest() == file0 and db_digest(rich.path) == dig0


def test_database_without_stage_32_33_tables_is_read_not_migrated():
    import tempfile
    path = Path(tempfile.mkdtemp(prefix="ev35-")) / "only31.db"
    StrategyStore(path).create(fspec(entry=NEVER, name="Only 3.1"))
    with sqlite3.connect(path) as c:
        vid = c.execute("SELECT version_id FROM strategy_versions").fetchone()[0]
    before = db_digest(path)
    v = V.view(vid, path=path)
    assert v["historical"]["status"] == "NO_BACKTEST" and v["forward"]["status"] == "NO_JOURNAL"
    assert V.public_config(path)["versions"][0]["runs"] == 0 and db_digest(path) == before


# ---- API ---------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def api(rich, monkeypatch):
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
    monkeypatch.setattr(RO, "db_path", lambda: Path(rich.path))
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("market data"))     # noqa: E731
    monkeypatch.setattr("data.market_data.fetch_daily_bars", boom)
    c = TestClient(app)
    c.lab, c.calls, c.http = rich, calls, http
    return c


def test_api_round_trip_no_ai_no_broker_no_market_data(api):
    ids = api.lab.ids
    cfg = api.get("/api/evidence-comparison/config").json()
    names = [(v["strategy_name"], v["version_number"]) for v in cfg["versions"]]
    assert ("Swing", 2) in names and ("Swing", 1) in names
    one = next(v for v in cfg["versions"] if v["strategy_version_id"] == ids["v1"])
    assert (one["runs"], one["completed_runs"], one["journals"], one["open_journals"]) == (2, 2, 1, 1)
    r = api.post("/api/evidence-comparison/view", json={"strategy_version_id": ids["v1"]})
    assert r.status_code == 200 and r.json()["historical"]["run"]["run_id"] == ids["rb"]
    r = api.post("/api/evidence-comparison/view", json={"strategy_version_id": ids["v1"], "backtest_run_id": ids["ra"],
                                                        "forward_journal_id": ids["j1"]})
    assert r.status_code == 200 and r.json()["historical"]["run"]["run_id"] == ids["ra"]
    bad = api.post("/api/evidence-comparison/view", json={"strategy_version_id": ids["v1"], "forward_journal_id": ids["j2"]})
    assert bad.status_code == 409 and bad.json()["status"] == "EVIDENCE_VERSION_MISMATCH"
    assert api.post("/api/evidence-comparison/view", json={"strategy_version_id": "0" * 32}).status_code == 404
    assert api.post("/api/evidence-comparison/view", json={"strategy_version_id": ids["v1"], "combine": True}).status_code == 422
    assert api.post("/api/evidence-comparison/view", json={"strategy_version_id": "../x"}).status_code == 422
    assert api.calls == [] and api.http.requests == []


def test_evidence_endpoints_are_exactly_the_read_only_pair():
    from api.server import app
    paths = app.openapi()["paths"]
    mine = {p: set(ops) for p, ops in paths.items() if p.startswith("/api/evidence-comparison")}
    assert mine == {"/api/evidence-comparison/config": {"get"}, "/api/evidence-comparison/view": {"post"}}
    for p in mine:
        assert not re.search(r"(?i)order|trade|paper|execute|broker|optimi|scan|rank|schedule|recommend|save|store|score", p), p


# ---- 77: security; frozen Stage 3.1-3.4; UI hooks + neutral language ----------------------------------------------------------------

NEW_FILES = ["comparison/__init__.py", "comparison/cycles.py", "comparison/view.py", "api/routes/evidence_comparison.py",
             "frontend/evidence.js"]


def test_77_no_code_execution_broker_ai_scheduler_or_sql_writes():
    for f in NEW_FILES:
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|new Function|"
                             r"\bpickle\b|marshal|importlib", text), f
        assert not re.search(r"TradingClient|alpaca\.trading|place_?order|submit_?order|cancel_?order|/orders|webhook|"
                             r"anthropic|get_provider\(|from agents|import agents|from portfolio|import portfolio|rh_gateway|"
                             r"robinhood|analysis_cache|setInterval|setTimeout|apscheduler|BackgroundScheduler|add_job|"
                             r"crontab|threading\.Timer|shortcuts/holdings|/api/portfolio|fetch_daily_bars|market_data|"
                             r"get_data_client|fetch_and_store|build_event_context|research_context", text, re.I), f
        if f.endswith(".py"):
            assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\s+(INTO|TABLE|FROM|INDEX|TRIGGER|\w+\s+SET)",
                                 text), f
            assert "_connect(readonly=False" not in text and "sqlite3.connect(" not in text, f


def test_frozen_stage_31_to_34_unchanged():
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
    fit = ["fit/__init__.py", "fit/current.py", "fit/evidence.py", "fit/readonly.py", "fit/METHOD.md",
           "frontend/strategy_fit.css", "frontend/stock_result.js"]      # the route is covered by tests/test_explain_38.py
    h = hashlib.sha256()
    for f in fit:
        h.update(f.encode() + b"\0" + hashlib.sha256((ROOT / f).read_bytes()).digest())
    assert h.hexdigest() == FROZEN_FIT_34
    assert not list((ROOT / "database").glob("*compar*")) and not list((ROOT / "database").glob("*evidence*"))


def test_strategy_text_is_inert_in_evidence():
    lab = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    name = "$(rm -rf /); __import__('os') <script>alert(1)</script>"
    jid = lab.journal(fspec(entry=NEVER, symbols=("AMD",), name=name), FL.ny(D("2026-09-25"), 12))["journal_id"]
    v = V.view(vid_of(lab, lab.fs.journal(jid)["strategy_id"]), path=lab.path)
    assert v["identity"]["strategy_name"] == name
    js = (ROOT / "frontend" / "evidence.js").read_text(encoding="utf-8")
    assert "esc(id.strategy_name)" in js and "esc(v.strategy_name)" in js and "innerHTML = `" in js


def test_ui_race_guard_partial_redraw_and_neutral_language():
    js = (ROOT / "frontend" / "evidence.js").read_text(encoding="utf-8")
    sf = (ROOT / "frontend" / "strategy_fit.js").read_text(encoding="utf-8")
    sl = (ROOT / "frontend" / "strategy_lab.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "new AbortController()" in js and "ctrl.abort()" in js and js.count("if (my !== seq) return;") == 2
    assert "setInterval" not in js and "setTimeout" not in js
    assert js.count("rb.disabled = busy || !versionId") == 1 and js.count('[data-ev="refresh"]').__gt__(0)   # one place decides
    assert not re.search(r"sl-body|bt-body|fj-body|sf-body|location\.reload|StrategyLab\.load\(", js)
    assert 'addWorkspace({ id: "evidence", label: "Evidence", cls: "ev-mode", show })' in js
    assert "Refresh evidence" in js and "Stored evidence as loaded" in js and "No forward journal yet." in js
    assert "No completed forward cycles yet." in js and "Most recent stored run" in js and "Open historical equity curve" in js
    assert "Open Forward Journal" in js and "Current Strategy Fit" in js and "HISTORICAL TIMELINE" in js and "FORWARD TIMELINE" in js
    assert "Positive completed cycles" in js and "Positive-cycle rate" in js and "reference move" in js
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    low = re.sub(r"does not combine them into one score|no combined score|not a rating", "", code)
    assert not re.search(r"\bbuy\b|\bsell\b|recommend|\bbest\b|\bworst\b|\bscore\b|confidence|probabilit|leaderboard|\brank|"
                         r"\bgrade|\bbetter\b|\bworse\b|improved|degraded|outperform|underperform|is working|stopped working|"
                         r"confirms|disproves|good strategy|bad strategy|is good|is bad|reliable|verdict|profit\b(?! factor)|realized gain|broker p&l|"
                         r"trade profit|equity curve \+", low, re.I)
    fwd = js[js.index("function fwdCard"):js.index("function drawSides")]
    assert not re.search(r"\btrades?\b|win rate|P&L|\bprofit", fwd, re.I)                   # forward = reference cycles
    assert 'classList.toggle("sf-mode"' in sf and "addWorkspace" in sf and "workspaces.forEach" in sf
    assert "openVersion(id, number, panel, runId)" in sl and 'tabBtn.addEventListener("click", () => { if (!loaded) load(); })' in sl
    assert 'id="ev-body"' in html and "evidence.css" in html and html.index("strategy_fit.js") < html.index("evidence.js")
