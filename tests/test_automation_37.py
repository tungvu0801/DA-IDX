"""Stage 3.7: opt-in automatic after-close forward capture — the SAME preflight + record as the buttons, once per
session, completed sessions only (Stage 3.3 rule unchanged), no backfill, isolated per journal, one scheduler per process,
DST-correct New York schedule, clean start / stop. No network, no AI, no broker, no orders."""
import functools
import hashlib
import json
import logging
import re
import sqlite3
import tempfile
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bt_fixtures as X
import config
import fw_fixtures as FL
from backtest.bars import NY
from database.automation_migrations import run_automation_migrations
from forward import automation as A
from forward import journal as J
from forward.store import ForwardError
from strategy import spec as S

ROOT = Path(__file__).resolve().parents[1]
D = date.fromisoformat
QUIET = {"vol": 0.002}
UP3 = {"logic": "ALL", "conditions": [{"feature": "stock.change_1d_pct", "op": ">", "value": 3}]}
HOLD5 = {"logic": "ANY", "conditions": [], "invalidation": {"method": "PCT_BELOW_ENTRY", "pct": 40},
         "target": {"method": "PCT_ABOVE_ENTRY", "pct": 60}, "max_holding_days": 5}


def fspec(entry=None, symbols=("AMD",), name="Auto test", exit_=None):
    s = X.spec(symbols=symbols, entry=entry or UP3, exit_=exit_ or HOLD5,
               risk={"max_position_pct": 10, "max_open_positions": 50}, name=name)
    n, err = S.validate(s)
    assert not err, err
    return n


def et(d, hh=0, mm=15):
    return datetime.combine(D(d), time(hh, mm), tzinfo=NY).astimezone(timezone.utc)


def env(market=None, spec=None, created="2026-09-25"):
    lab = FL.FLab(market or FL.Market(("AMD", "SPY"), **QUIET))
    jid = lab.journal(spec or fspec(), FL.ny(D(created), 12))["journal_id"]
    store = A.AutomationStore(lab.path)
    kw = dict(fs=lab.fs, bs=lab.store, fetch_fn=lab.market.fetch, client=object(), research_db=lambda: lab.research,
              events_fn=lab.events.build, coverage_fn=lab.events.cov)
    return lab, jid, store, kw


def scheduler(lab, store, kw, clock):
    return A.Scheduler(store_fn=lambda: store, check_fn=functools.partial(A.run_check, **kw), clock=lambda: clock[0],
                       startup_delay_s=0)


def at(sch, lab, clock, t):
    clock[0] = t
    lab.market.now = t
    return sch.step()


def sessions(lab, jid):
    return [(s["session_date"], s["kind"]) for s in lab.fs.sessions(jid)]


def jump(m, sym, d, pct=5.0):
    pc = m.rows[sym][m.days[m.days.index(d) - 1]][5]
    m.set_bar(sym, d, pc, pc * (1 + pct / 100) * 1.001, pc * 0.999, pc * (1 + pct / 100))


# ---- 37 / 38 / 39 / 40 / 41: off by default, once per completed session, weekends and holidays -------------------------------

def test_37_disabled_makes_no_writes_and_manual_record_still_works():
    lab, jid, store, kw = env()
    clock = [et("2026-09-28", 12)]
    sch = scheduler(lab, store, kw, clock)
    assert store.settings()["enabled"] is False                                   # default OFF
    for d in ("2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"):
        assert at(sch, lab, clock, et(d)) is None
    assert sessions(lab, jid) == [] and sch.next_at is None and lab.market.calls == []
    with sqlite3.connect(lab.path) as c:                                          # reading the setting never migrates
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'app_settings'").fetchone() is None
    assert lab.record(jid, FL.at(D("2026-10-02")))["status"] == "RECORDED"


def test_38_enabled_captures_the_completed_session_once():
    lab, jid, store, kw = env()
    store.save(enabled=True)
    clock = [et("2026-09-28", 18)]
    sch = scheduler(lab, store, kw, clock)
    r = at(sch, lab, clock, et("2026-09-28", 18))                 # Monday 6 PM: Monday is NOT complete (Stage 3.3 rule)
    assert r["result"] == "NO_NEW_SESSION" and sessions(lab, jid) == []
    assert sch.next_at == et("2026-09-29")                        # next check: 00:15 ET Tuesday
    r = at(sch, lab, clock, et("2026-09-29"))
    assert (r["result"], r["latest_session"], r["counts"]["captured"]) == ("CAPTURED", "2026-09-28", 1)
    assert sessions(lab, jid) == [("2026-09-28", "CAPTURED")]
    assert store.settings()["last_check"]["result"] == "CAPTURED"


def test_39_weekend_checks_capture_nothing_new():
    lab, jid, store, kw = env()
    store.save(enabled=True)
    clock = [et("2026-09-29")]
    sch = scheduler(lab, store, kw, clock)
    out = {d: at(sch, lab, clock, et(d)) for d in ("2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03",
                                                   "2026-10-04", "2026-10-05")}
    assert [out[d]["result"] for d in out] == ["CAPTURED"] * 5 + ["ALREADY_RECORDED", "ALREADY_RECORDED"]   # Sun, Mon
    assert [s for s, _ in sessions(lab, jid)] == ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]


def test_40_holiday_has_no_new_session():
    lab, jid, store, kw = env(created="2026-09-02")               # Labor Day, Mon 2026-09-07, has no session
    store.save(enabled=True)
    clock = [et("2026-09-04")]
    sch = scheduler(lab, store, kw, clock)
    for d in ("2026-09-04", "2026-09-05"):
        assert at(sch, lab, clock, et(d))["result"] == "CAPTURED"
    for d in ("2026-09-06", "2026-09-07", "2026-09-08"):         # Sun, Mon (holiday), Tue morning: still Friday's close
        assert at(sch, lab, clock, et(d))["result"] == "ALREADY_RECORDED"
    assert [s for s, _ in sessions(lab, jid)] == ["2026-09-03", "2026-09-04"]
    assert at(sch, lab, clock, et("2026-09-09"))["latest_session"] == "2026-09-08"


def test_41_three_checks_one_capture():
    lab, jid, store, kw = env()
    res = [A.run_check(now=et("2026-09-29", 0, m), store=store, **kw)["result"] for m in (15, 30, 59)]
    assert res == ["CAPTURED", "ALREADY_RECORDED", "ALREADY_RECORDED"]
    assert sessions(lab, jid) == [("2026-09-28", "CAPTURED")] and len(lab.fs.observations(jid)) == 1


# ---- 42 / 43 / 44 / 45: offline gaps (no backfill), open-cycle gaps, MFE / MAE parity, research unavailable ------------------

def test_42_sessions_missed_while_offline_are_never_reconstructed():
    lab, jid, store, kw = env()
    store.save(enabled=True)
    clock = [et("2026-09-29")]
    assert at(scheduler(lab, store, kw, clock), lab, clock, et("2026-09-29"))["result"] == "CAPTURED"   # Monday's close
    # the server is off at 00:15 Wednesday; restarted Thursday 09:00 -> a NEW scheduler (process restart)
    sch = scheduler(lab, store, kw, clock)
    r = at(sch, lab, clock, et("2026-10-01", 9, 0))
    assert r["result"] == "CAPTURED" and r["latest_session"] == "2026-09-30"
    assert r["journals"][0]["missed_recorded"] == ["2026-09-29"]
    assert sessions(lab, jid) == [("2026-09-28", "CAPTURED"), ("2026-09-29", "MISSED"), ("2026-09-30", "CAPTURED")]
    assert lab.fs.observations(jid, "2026-09-29") == []                                      # Tuesday never rebuilt
    assert sch.next_at == et("2026-10-02")


def test_43_open_cycle_during_an_offline_session_blocks_exactly_as_before():
    m = FL.Market(("AMD", "SPY"), **QUIET)
    jump(m, "AMD", D("2026-09-28"))
    lab, jid, store, kw = env(market=m)
    for d in ("2026-09-29", "2026-09-30"):                    # ENTER Monday, reference entry Tuesday -> OPEN
        assert A.run_check(now=et(d), store=store, **kw)["result"] == "CAPTURED"
    assert lab.obs(jid, D("2026-09-29"))["AMD"]["state_after"] == "OPEN"
    r = A.run_check(now=et("2026-10-02"), store=store, **kw)     # Thursday 00:15 was missed (server off)
    assert r["result"] == "CAPTURED" and r["journals"][0]["missed_recorded"] == ["2026-09-30"]
    o = lab.obs(jid, D("2026-10-01"))["AMD"]
    assert (o["state_before"], o["state_after"], o["reason_code"]) == ("OPEN", "CONTINUITY_BLOCKED", "FORWARD_CONTINUITY_GAP")
    assert lab.fs.journal(jid)["status"] == "CONTINUITY_BLOCKED"
    assert [x["observation_type"] for x in lab.fs.excursions(jid)] == ["ENTRY_SESSION", "CONTINUITY_GAP"]
    r = A.run_check(now=et("2026-10-03"), store=store, **kw)     # a blocked journal is still captured normally
    assert r["result"] == "CAPTURED" and r["journals"][0]["journal_status"] == "CONTINUITY_BLOCKED"


def _normalized_evidence(lab, jid):
    rows = {"sessions": lab.fs.sessions(jid), "observations": lab.fs.observations(jid), "fills": lab.fs.fills(jid),
            "excursions": lab.fs.excursions(jid), "tracking": lab.fs.excursion_tracking(jid)}
    text = re.sub(r"\b[0-9a-f]{32}\b", "<ID>", json.dumps(rows, sort_keys=True, default=str))      # journal / dataset ids
    text = re.sub(r'"fetched_at": "[^"]*"', '"fetched_at": "<download wall clock>"', text)          # download time
    return re.sub(r'"timings": \{[^}]*\}', '"timings": "<how long the capture took>"', text)       # durations only


def test_44_automatic_capture_equals_manual_capture_including_mfe_mae():
    def market():
        m = FL.Market(("AMD", "MU", "SPY"), **QUIET)
        jump(m, "AMD", D("2026-09-28"))
        return m
    days = ("2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03")
    man, jm, _, km = env(market=market(), spec=fspec(symbols=("AMD", "MU")))
    for d in days:
        man.market.now = et(d)
        assert J.record(man.fs, man.store, jm, now=et(d), **{k: v for k, v in km.items() if k not in ("fs", "bs")})["status"] == "RECORDED"
    auto, ja, sa, ka = env(market=market(), spec=fspec(symbols=("AMD", "MU")))
    for d in days:
        auto.market.now = et(d)
        assert A.run_check(now=et(d), store=sa, **ka)["result"] == "CAPTURED"
    assert _normalized_evidence(man, jm) == _normalized_evidence(auto, ja)
    ex = auto.fs.excursions(ja)
    assert [x["observation_type"] for x in ex] == ["ENTRY_SESSION", "HELD_SESSION", "HELD_SESSION", "HELD_SESSION"]


def test_45_research_rule_without_saved_research_skips_and_makes_no_ai_call(monkeypatch):
    import agents.technical_agent as ta
    from portfolio import explain as ex
    calls = []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append(1))
    monkeypatch.setattr(ex, "get_provider", lambda: calls.append(1))
    sp = fspec(entry={"logic": "ALL", "conditions": [{"feature": "research.view", "op": "==", "value": "BULLISH BIAS"}]})
    lab, jid, store, kw = env(spec=sp)
    assert A.run_check(now=et("2026-09-29"), store=store, **kw)["result"] == "CAPTURED"
    o = lab.obs(jid, D("2026-09-28"))["AMD"]
    assert (o["decision"], o["reason_code"]) == ("SKIP", "REQUIRED_DATA_UNAVAILABLE") and o["research"]["source"] == "UNAVAILABLE"
    assert calls == [] and lab.research.snaps == {}


# ---- 46 / retry / no journals / archived ------------------------------------------------------------------------------------

def test_46_one_failing_journal_never_stops_the_others_and_is_retried(monkeypatch):
    lab, ja, store, kw = env()
    jb = lab.journal(fspec(name="B"), FL.ny(D("2026-09-25"), 13))["journal_id"]
    jc = lab.journal(fspec(name="C"), FL.ny(D("2026-09-25"), 14))["journal_id"]
    real = J.record
    down = {"on": True}

    def flaky(fs, bs, jid, **k):
        if jid == jb and down["on"]:
            raise ForwardError("DATA_UNAVAILABLE", "provider timeout", status=503)
        return real(fs, bs, jid, **k)
    monkeypatch.setattr(J, "record", flaky)
    store.save(enabled=True)
    clock = [et("2026-09-29")]
    sch = scheduler(lab, store, kw, clock)
    r = at(sch, lab, clock, et("2026-09-29"))
    assert r["result"] == "PARTIAL_FAILURE" and r["counts"] == {"captured": 2, "already_recorded": 0, "no_new_session": 0, "errors": 1}
    assert {x["journal"]: x["result"] for x in r["journals"]}[jb[:8]] == "ERROR" and r["retryable"]
    assert sch.next_at == et("2026-09-29") + timedelta(minutes=15) and sch.retries == 1
    down["on"] = False                                                                 # the provider recovers
    r2 = at(sch, lab, clock, et("2026-09-29", 0, 30))
    assert r2["trigger"] == "RETRY" and r2["counts"]["captured"] == 1 and r2["counts"]["already_recorded"] == 2
    assert sch.retries == 0 and sch.next_at == et("2026-09-30")
    for j in (ja, jb, jc):
        assert sessions(lab, j) == [("2026-09-28", "CAPTURED")]


def test_retries_are_bounded_then_wait_for_the_next_check():
    lab, jid, store, kw = env()
    lab.market.fail = True                                                             # no market data at all
    store.save(enabled=True)
    clock = [et("2026-09-29")]
    sch = scheduler(lab, store, kw, clock)
    t = et("2026-09-29")
    seen = []
    for _ in range(A.MAX_RETRIES + 1):
        r = at(sch, lab, clock, t)
        seen.append((r["trigger"], r["result"]))
        t = sch.next_at
    assert seen[0] == ("SCHEDULED", "ERROR") and all(x == ("RETRY", "ERROR") for x in seen[1:])
    assert sch.next_at == et("2026-09-30") and sch.retries == 0 and sessions(lab, jid) == []
    assert at(sch, lab, clock, et("2026-09-29", 3, 0)) is None                         # no tight loop in between


def test_no_active_journals_does_no_market_data_work():
    lab = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    store = A.AutomationStore(lab.path)
    r = A.run_check(now=et("2026-09-29"), store=store, fs=lab.fs, bs=lab.store, fetch_fn=lab.market.fetch, client=object())
    assert r["result"] == "NO_ACTIVE_JOURNALS" and lab.market.calls == []


def test_archived_journals_are_never_captured():
    lab, jid, store, kw = env()
    J.archive_journal(lab.fs, jid, now=FL.ny(D("2026-09-26"), 12))
    r = A.run_check(now=et("2026-09-29"), store=store, **kw)
    assert r["result"] == "NO_ACTIVE_JOURNALS" and sessions(lab, jid) == [] and lab.market.calls == []


# ---- 35 / 36: races -----------------------------------------------------------------------------------------------------------

def _together(*fns):
    out = [None] * len(fns)
    barrier = threading.Barrier(len(fns))

    def run(i, f):
        barrier.wait()
        try:
            out[i] = f()
        except Exception as exc:  # noqa: BLE001
            out[i] = exc
    ts = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    return out


def test_35_manual_and_automatic_record_at_the_same_time_capture_once():
    lab, jid, store, kw = env(market=FL.Market(("AMD", "MU", "NVDA", "SPY"), **QUIET), spec=fspec(symbols=("AMD", "MU", "NVDA")))
    rk = {k: v for k, v in kw.items() if k not in ("fs", "bs")}
    lab.market.now = et("2026-09-29")
    manual, auto = _together(lambda: J.record(lab.fs, lab.store, jid, now=et("2026-09-29"), **rk),
                             lambda: A.run_check(now=et("2026-09-29"), store=store, **kw))
    outcomes = sorted([manual["status"], "RECORDED" if auto["counts"]["captured"] else "ALREADY_RECORDED"])
    assert outcomes == ["ALREADY_RECORDED", "RECORDED"]
    assert sessions(lab, jid) == [("2026-09-28", "CAPTURED")] and len(lab.fs.observations(jid)) == 3


def test_35b_without_the_process_lock_the_database_still_allows_one_capture():
    lab, jid, store, kw = env(market=FL.Market(("AMD", "MU", "SPY"), **QUIET), spec=fspec(symbols=("AMD", "MU")))
    rk = {k: v for k, v in kw.items() if k not in ("fs", "bs")}
    lab.market.now = et("2026-09-29")
    now = et("2026-09-29")
    a, b = _together(lambda: J._record(lab.fs, lab.store, jid, now, rk["fetch_fn"], rk["client"], rk["research_db"],
                                       rk["events_fn"], rk["coverage_fn"]),
                     lambda: J._record(lab.fs, lab.store, jid, now, rk["fetch_fn"], rk["client"], rk["research_db"],
                                       rk["events_fn"], rk["coverage_fn"]))       # two processes: no shared lock
    got = sorted(x["status"] if isinstance(x, dict) else type(x).__name__ for x in (a, b))
    assert got == ["ALREADY_RECORDED", "RECORDED"]
    assert sessions(lab, jid) == [("2026-09-28", "CAPTURED")] and len(lab.fs.observations(jid)) == 2


def test_36_two_scheduler_loops_capture_once():
    lab, jid, store, kw = env()
    store.save(enabled=True)
    clock = [et("2026-09-29")]
    lab.market.now = clock[0]
    s1, s2 = scheduler(lab, store, kw, clock), scheduler(lab, store, kw, clock)
    r1, r2 = _together(s1.step, s2.step)
    results = sorted(r["result"] for r in (r1, r2))
    assert results in (["CAPTURED", "LEASE_HELD"], ["ALREADY_RECORDED", "CAPTURED"]) and sessions(lab, jid) == [("2026-09-28", "CAPTURED")]
    assert store.acquire("someone-else", et("2026-09-29", 1)) is True               # lease released after the checks
    r3 = A.run_check(now=et("2026-09-29", 1, 5), store=store, owner="me", **kw)
    assert r3["result"] == "LEASE_HELD"                                              # another live loop holds it
    assert store.acquire("me", et("2026-09-29", 2)) is True                          # an expired lease can be taken over


# ---- schedule: completed-session contract, DST, startup catch-up ------------------------------------------------------------

def test_default_time_follows_the_frozen_completed_session_rule():
    from backtest.bars import last_complete_session_date
    assert A.DEFAULT_TIME_ET == "00:15"
    assert last_complete_session_date(et("2026-09-28", 16, 15)) == D("2026-09-27")        # 4:15 PM Monday: not Monday
    assert last_complete_session_date(et("2026-09-28", 23, 59)) == D("2026-09-27")
    assert last_complete_session_date(et("2026-09-29", 0, 0)) == D("2026-09-28")          # from midnight: Monday
    assert last_complete_session_date(et("2026-09-29")) == D("2026-09-28")


def test_schedule_is_new_york_wall_clock_across_dst():
    t = et("2026-10-30", 12)
    seen = []
    for _ in range(5):                                           # across the Nov 1 2026 fall-back
        t = A.next_scheduled(t, "00:15")
        seen.append(t)
    assert all((x.astimezone(NY).hour, x.astimezone(NY).minute) == (0, 15) for x in seen)
    assert {x.utcoffset() for x in seen} == {timedelta(0)} and {x.hour for x in seen} == {4, 5}      # EDT then EST
    t = et("2026-03-06", 12)
    spring = []
    for _ in range(4):                                           # across the Mar 8 2026 spring-forward
        t = A.next_scheduled(t, "00:15")
        spring.append(t)
    assert [x.astimezone(NY).date().isoformat() for x in spring] == ["2026-03-07", "2026-03-08", "2026-03-09", "2026-03-10"]
    assert all(x.astimezone(NY).hour == 0 for x in spring)
    gap = A.next_scheduled(et("2026-03-07", 12), "02:30")        # 02:30 does not exist on Mar 8 -> the same instant, 03:30 EDT
    assert gap.astimezone(NY).isoformat().startswith("2026-03-08T03:30")
    assert A.previous_scheduled(et("2026-09-29", 9, 0), "00:15") == et("2026-09-29")


def test_startup_checks_the_latest_session_once_if_its_slot_was_missed():
    lab, jid, store, kw = env()
    store.save(enabled=True)
    clock = [et("2026-09-29", 9, 0)]
    sch = scheduler(lab, store, kw, clock)
    assert sch._plan(clock[0], store.settings()) == clock[0]              # never checked -> check once now
    store.save(last_check={"at": et("2026-09-29", 0, 20).isoformat()})
    assert sch._plan(clock[0], store.settings()) == et("2026-09-30")      # today's slot was checked -> wait
    store.save(last_check={"at": et("2026-09-28", 0, 20).isoformat()})
    assert sch._plan(clock[0], store.settings()) == clock[0]              # server was off at 00:15 -> one check now


# ---- 47 / 48: restart, one scheduler, lifespan start / stop --------------------------------------------------------------------

def test_47_restart_keeps_the_setting_and_starts_exactly_one_scheduler():
    lab, jid, store, kw = env()
    A.AutomationStore(lab.path).save(enabled=True, capture_time_et="00:20")
    clock = [et("2026-09-29", 9, 0)]
    s1 = A.Scheduler(store_fn=lambda: A.AutomationStore(lab.path), check_fn=lambda **k: {"result": "NO_NEW_SESSION"},
                     clock=lambda: clock[0], startup_delay_s=3600)
    assert s1.start() is True and s1.start() is False and s1.running()             # one thread per scheduler
    assert s1.stop(5) and not s1.running()
    s2 = A.Scheduler(store_fn=lambda: A.AutomationStore(lab.path), clock=lambda: clock[0], startup_delay_s=3600)   # "restart"
    st = s2.status()
    assert st["enabled"] and st["capture_time_et"] == "00:20" and st["next_check_at"]
    assert A.get_scheduler() is A.get_scheduler()                                   # one per process


def test_48_lifespan_starts_only_when_enabled_and_stops_cleanly(monkeypatch):
    from api.server import app
    lab = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    store = A.AutomationStore(lab.path)
    for enabled in (False, True):
        store.save(enabled=enabled)
        sch = A.Scheduler(store_fn=lambda: store, check_fn=lambda **k: {"result": "NO_ACTIVE_JOURNALS"}, startup_delay_s=3600)
        monkeypatch.setattr(A, "_scheduler", sch)
        monkeypatch.setattr(config, "EVENT_WARMUP_ON_STARTUP", False)
        with TestClient(app):
            assert sch.running() is enabled
        assert not sch.running()
    assert not any(t.name == "forward-auto-capture" and t.is_alive() for t in threading.enumerate())


def test_scheduler_errors_never_escape():
    clock = [et("2026-09-29")]
    lab = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    store = A.AutomationStore(lab.path)
    store.save(enabled=True)

    def boom(**k):
        raise RuntimeError("unexpected")
    sch = A.Scheduler(store_fn=lambda: store, check_fn=boom, clock=lambda: clock[0], startup_delay_s=0)
    r = sch.step()
    assert r["result"] == "ERROR" and r["error"] == "RuntimeError" and sch.next_at == clock[0] + timedelta(minutes=15)


# ---- migration, settings, API, UI, logging, security --------------------------------------------------------------------------

def test_49_migration_on_a_copy_of_the_real_database_is_additive_and_idempotent():
    real = ROOT / "data" / "stock_agent.db"
    if not real.exists():
        pytest.skip("no real database")
    tmp = Path(tempfile.mkdtemp(prefix="au37mig-")) / "copy.db"
    src, dst = sqlite3.connect(f"file:{real.as_posix()}?mode=ro", uri=True), sqlite3.connect(str(tmp))
    src.backup(dst)
    src.close()
    dst.close()
    conn = sqlite3.connect(str(tmp))
    before = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))

    def digest():
        h = hashlib.sha256()
        for t in before:
            cols = ", ".join(f'"{r[1]}"' for r in conn.execute(f'PRAGMA table_info("{t}")'))
            for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY {cols}'):
                h.update(repr(row).encode())
        return h.hexdigest()
    d0, uv0 = digest(), conn.execute("PRAGMA user_version").fetchone()[0]
    run_automation_migrations(conn)
    s1 = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
    run_automation_migrations(conn)
    assert sorted(conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()) == s1
    after = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"))
    assert set(after) - set(before) == {"app_settings", "app_leases"} and digest() == d0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == uv0
    with pytest.raises(sqlite3.IntegrityError):                      # only the approved setting keys can be stored
        conn.execute("INSERT INTO app_settings (key, value, updated_at) VALUES ('strategy_decision', 'ENTER', 'x')")
    conn.close()


def test_settings_persist_and_validate():
    lab = FL.FLab(FL.Market(("AMD", "SPY"), **QUIET))
    s = A.AutomationStore(lab.path)
    assert s.settings() == {"enabled": False, "capture_time_et": "00:15", "last_check": None}
    s.save(enabled=True, capture_time_et="00:45")
    assert A.AutomationStore(lab.path).settings()["enabled"] and A.AutomationStore(lab.path).settings()["capture_time_et"] == "00:45"
    with pytest.raises(ValueError):
        s.save(capture_time_et="25:00")


@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from api.routes import portfolio as pr
    from api.server import app
    from pf_fixtures import FakeGatewayHttp, fake_provider
    calls = []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append(1))
    http = FakeGatewayHttp()
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    lab, jid, store, kw = env()
    clock = [et("2026-09-29")]
    lab.market.now = clock[0]
    sch = A.Scheduler(store_fn=lambda: store, check_fn=functools.partial(A.run_check, **kw), clock=lambda: clock[0],
                      startup_delay_s=3600)
    monkeypatch.setattr(A, "_scheduler", sch)
    c = TestClient(app)
    c.lab, c.jid, c.store, c.sch, c.calls, c.http = lab, jid, store, sch, calls, http
    yield c
    sch.stop(5)


def test_api_settings_status_and_check_now(api):
    s = api.get("/api/forward-automation").json()
    assert s["enabled"] is False and s["next_check_at"] is None and "Server" not in s["note"] and s["default_time_et"] == "00:15"
    assert "while this Stock Agent server is running" in s["server_note"] and s["scheduler_running"] is False
    assert api.post("/api/forward-automation/check").status_code == 409                     # off: nothing to check
    r = api.post("/api/forward-automation", json={"enabled": True, "capture_time_et": "00:30"}).json()
    assert r["enabled"] and r["capture_time_et"] == "00:30" and r["scheduler_running"] and api.store.settings()["enabled"]
    c = api.post("/api/forward-automation/check").json()
    assert c["check"]["result"] == "CAPTURED" and c["check"]["trigger"] == "MANUAL_CHECK"
    assert sessions(api.lab, api.jid) == [("2026-09-28", "CAPTURED")]
    assert api.post("/api/forward-automation/check").json()["check"]["result"] == "ALREADY_RECORDED"
    assert api.post("/api/forward-automation", json={"enabled": True, "backfill": True}).status_code == 422
    assert api.post("/api/forward-automation", json={"enabled": True, "capture_time_et": "4:15 PM"}).status_code == 422
    off = api.post("/api/forward-automation", json={"enabled": False}).json()
    assert off["enabled"] is False and off["scheduler_running"] is False
    assert api.calls == [] and api.http.requests == []
    from api.server import app
    fw = {p for p in app.openapi()["paths"] if p.startswith("/api/forward-tests")}
    assert not any("automation" in p for p in fw)                                             # Stage 3.3 set unchanged


def test_logs_are_concise_and_carry_no_secrets(caplog):
    lab, jid, store, kw = env()
    with caplog.at_level(logging.INFO, logger="forward.automation"):
        A.run_check(now=et("2026-09-29"), store=store, **kw)
        A.run_check(now=et("2026-09-29", 0, 30), store=store, **kw)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "AUTO_CAPTURE check started" in text and "AUTO_CAPTURE captured 1 journal(s)" in text
    assert f"AUTO_CAPTURE journal {jid[:8]} already recorded" in text and jid not in text
    for secret in (config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, getattr(config, "ANTHROPIC_API_KEY", "")):
        assert not secret or secret not in text


NEW_FILES = ["forward/automation.py", "api/routes/forward_automation.py", "database/automation_migrations.py",
             "frontend/forward_automation.js"]


def test_56_no_trading_ai_code_execution_or_heavy_scheduler():
    for f in NEW_FILES:
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|new Function|"
                             r"\bpickle\b|marshal|importlib", text), f
        assert not re.search(r"TradingClient|alpaca\.trading|place_?order|submit_?order|cancel_?order|/orders|webhook|"
                             r"anthropic|get_provider\(|from agents|import agents|from portfolio|import portfolio|rh_gateway|"
                             r"robinhood|setInterval|setTimeout|apscheduler|BackgroundScheduler|add_job|crontab|"
                             r"threading\.Timer|analyze|fit\.current|evidence_comparison", text, re.I), f
    auto = (ROOT / "forward" / "automation.py").read_text(encoding="utf-8")
    assert "J.preflight(" in auto and "J.record(" in auto and "day_snapshot" not in auto and "group_met" not in auto


def test_58_62_ui_is_opt_in_explicit_and_uses_capture_language():
    js = (ROOT / "frontend" / "forward_automation.js").read_text(encoding="utf-8")
    fj = (ROOT / "frontend" / "forward_journal.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    for s in ("AUTOMATIC CAPTURE", "Automatically records each eligible completed close while this Stock Agent server is running",
              "Server must be running", "NEXT CHECK", "LAST CHECK", "LAST RESULT", "Check automation now", "Refresh status",
              "No new session", "Already recorded", "Turn on automatic capture"):
        assert s in js, s
    assert "setInterval" not in js and "setTimeout" not in js
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert not re.search(r"(?i)auto[- ]?trad|\bbot\b|execution|buy automatically|sell automatically|\bbuy\b|\bsell\b", code)
    assert "window.ForwardAutomation.mount(" in fj and html.index("forward_journal.js") < html.index("forward_automation.js")
    assert "No orders are ever placed." in A.NOTE and "computer is awake" in A.SERVER_NOTE
