"""Stage 4.1 — saved Strategy Scanner configurations and in-app RULES MET change alerts (fit/saved_scans.py). The ONE
Stage 4.0 scanner, one immutable snapshot per scan + session, one event only when RULES MET changed. No AI, no broker,
no network; the Stage 3.7 scheduler runs the checks as an isolated after-close extension."""
import json
import re
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import fw_fixtures as FL
from fit import current as FC
from fit import readonly as RO
from fit import saved_scans as SS
from fit import scanner as SC
from forward import automation as A
from test_fit_34 import QUIET, TRUE, G, checkpoint, db_digest, lab_with, spec

ROOT = Path(__file__).resolve().parents[1]
D = date.fromisoformat
S1, S2, S3, S4 = D("2026-09-25"), D("2026-09-28"), D("2026-09-29"), D("2026-09-30")     # consecutive sessions
UP = {"feature": "stock.change_1d_pct", "op": ">", "value": 2}


def jump(m, sym, d, pct):
    pc = m.rows[sym][m.days[m.days.index(d) - 1]][5]
    c = pc * (1 + pct / 100)
    m.set_bar(sym, d, pc, max(pc, c) * 1.001, min(pc, c) * 0.999, c)


def world(moves, syms=("AMD", "MU", "CLS"), entry=None, universe=None, name="Up"):
    """moves: {session: {symbol: pct}}; every other listed symbol is flat that session (RULES MET = moved > 2 %)."""
    lab = lab_with(FL.Market(tuple(dict.fromkeys(("AMD", "MU", "CLS", "NVDA", "KO", "SPY", "QQQ", "SOXX") + tuple(syms))), **QUIET))
    for d in (S1, S2, S3, S4):
        for s in syms:
            jump(lab.market, s, d, (moves.get(d) or {}).get(s, 0.0))
    lab.save(spec(entry or G("ALL", UP), symbols=universe or syms, name=name))
    return lab


def vid(lab, name="Up"):
    return next(v["strategy_version_id"] for v in RO.saved_versions(Path(lab.path), include_old=True) if v["strategy_name"] == name)


def kw(lab, **extra):
    return {"path": lab.path, "fetch_fn": lab.market.fetch, "client": object(), "research_db": extra.pop("research_db", lambda: lab.research),
            "events_fn": lab.events.build, "coverage_fn": lab.events.cov, "cache": FC.BarCache(), **extra}


def save(lab, source="SAVED_UNIVERSE", symbols=None, alerts=True, name=None, v=None):
    return SS.create(v or vid(lab), source, name, alerts, symbols, path=lab.path)["saved_scan_id"]


def check(lab, sid, session, trigger="MANUAL_CHECK", **extra):
    lab.market.now = FL.at(session)
    k = kw(lab, **extra)
    path = k.pop("path")
    return SS.check(sid, trigger, FL.at(session), path=path, **k)


def events(lab, sid=None):
    return SS.SavedScanStore(lab.path).events(sid)


def snaps(lab, sid):
    return SS.SavedScanStore(lab.path).snapshots(sid, 100)


# ---- 48-53: RULES MET change semantics ----------------------------------------------------------------------------------------

def test_48_first_check_is_a_baseline_without_alert():
    lab = world({S1: {"MU": 3}})
    sid = save(lab)
    r = check(lab, sid, S1)
    assert r["result"] == "BASELINE" and r["snapshot"]["rules_met"] == ["MU"] and r["snapshot"]["is_baseline"] is True
    assert r["alert"] is None and events(lab) == [] and len(snaps(lab, sid)) == 1


def test_49_newly_met():
    lab = world({S1: {"MU": 3}, S2: {"MU": 3, "AMD": 3}})
    sid = save(lab)
    check(lab, sid, S1)
    r = check(lab, sid, S2)
    assert r["result"] == "ALERT_CREATED"
    e = events(lab, sid)
    assert len(e) == 1 and [x["symbol"] for x in e[0]["newly_rules_met"]] == ["AMD"] and e[0]["no_longer_rules_met"] == []
    assert (e[0]["previous_session"], e[0]["decision_session"]) == ("2026-09-25", "2026-09-28")
    assert SS.alert_texts(e[0], "Up v1") == ["AMD newly meets the saved entry rules for Up v1 (RULES MET)."]


def test_50_no_longer_met():
    lab = world({S1: {"MU": 3, "AMD": 3}, S2: {"AMD": 3}})
    sid = save(lab)
    check(lab, sid, S1)
    check(lab, sid, S2)
    (e,) = events(lab, sid)
    assert e["newly_rules_met"] == [] and e["no_longer_rules_met"] == [{"symbol": "MU", "previous_status": "RULES_MET",
                                                                       "status": "RULES_NOT_MET"}]
    assert SS.alert_texts(e, "Up v1") == ["MU no longer meets the saved entry rules for Up v1 (now RULES NOT MET)."]


def test_51_both_directions_in_one_event():
    lab = world({S1: {"MU": 3, "CLS": 3}, S2: {"AMD": 3, "CLS": 3}})
    sid = save(lab)
    check(lab, sid, S1)
    check(lab, sid, S2)
    (e,) = events(lab, sid)
    assert [x["symbol"] for x in e["newly_rules_met"]] == ["AMD"] and [x["symbol"] for x in e["no_longer_rules_met"]] == ["MU"]


def test_52_no_change_no_event():
    lab = world({S1: {"AMD": 3, "MU": 3}, S2: {"AMD": 4, "MU": 5}})
    sid = save(lab)
    check(lab, sid, S1)
    r = check(lab, sid, S2)
    assert r["result"] == "NO_CHANGE" and events(lab) == [] and len(snaps(lab, sid)) == 2


def test_53_condition_count_changes_alone_never_alert():
    entry = G("ALL", UP, {"feature": "stock.change_1d_pct", "op": ">", "value": 5})       # AMD: 1/2 then 0/2 — never met
    lab = world({S1: {"AMD": 3}, S2: {}}, entry=entry)
    sid = save(lab)
    a, b = check(lab, sid, S1), check(lab, sid, S2)
    assert a["snapshot"]["conditions"]["AMD"] == [1, 2] and b["snapshot"]["conditions"]["AMD"] == [0, 2]
    assert b["result"] == "NO_CHANGE" and events(lab) == []


# ---- 54 + stale: leaving RULES MET because the result became unknowable --------------------------------------------------------

def test_54_incomplete_transition_keeps_the_status():
    entry = G("ALL", UP, {"feature": "research.view", "op": "==", "value": "BULLISH BIAS"})
    lab = world({S1: {"MU": 3}, S2: {"MU": 3}}, entry=entry)
    lab.research.add("MU", FL.ny(S1 - timedelta(days=3), 10))
    sid = save(lab)
    assert check(lab, sid, S1)["snapshot"]["rules_met"] == ["MU"]
    empty = FL.ResearchDB()                                   # the saved research is no longer readable
    check(lab, sid, S2, research_db=lambda: empty)
    (e,) = events(lab, sid)
    assert e["no_longer_rules_met"] == [{"symbol": "MU", "previous_status": "RULES_MET", "status": "INCOMPLETE_DATA"}]
    (t,) = SS.alert_texts(e, "Up v1")
    assert t.startswith("MU is no longer in RULES MET for Up v1; the latest scan has incomplete data") and "fail" not in t


def test_stale_transition_says_stale_not_false():
    lab = world({S1: {"MU": 3}})
    lab.market.drop("MU", S2)
    sid = save(lab)
    check(lab, sid, S1)
    check(lab, sid, S2)
    (e,) = events(lab, sid)
    assert e["no_longer_rules_met"][0]["status"] == "STALE_DATA"
    assert "stale data" in SS.alert_texts(e, "Up v1")[0] and "no longer meets" not in SS.alert_texts(e, "Up v1")[0]


# ---- 55 / 56: list membership changes are not rule alerts --------------------------------------------------------------------

def test_55_56_watchlist_changes_are_recorded_separately_never_as_rule_alerts():
    lab = world({S1: {"MU": 3}, S2: {"MU": 3, "CLS": 4}, S3: {"MU": 3, "CLS": 4}})
    watch = [["AMD", "MU"]]
    sid = save(lab, "WATCHLIST")
    check(lab, sid, S1, watchlist_fn=lambda: watch[0])
    watch[0] = ["AMD", "MU", "NVDA"]                                   # NVDA is outside the universe
    r2 = check(lab, sid, S2, watchlist_fn=lambda: watch[0])
    assert r2["snapshot"]["list_changes"] == {"added": [{"symbol": "NVDA", "status": "OUTSIDE_UNIVERSE"}], "removed": []}
    assert r2["result"] == "NO_CHANGE" and events(lab) == []
    watch[0] = ["AMD", "CLS", "MU", "NVDA"]                            # CLS joins the list already meeting the rules
    r3 = check(lab, sid, S3, watchlist_fn=lambda: watch[0])
    assert r3["snapshot"]["resolved_symbols"] == ["AMD", "CLS", "MU", "NVDA"] and r3["snapshot"]["rules_met"] == ["CLS", "MU"]
    assert r3["snapshot"]["list_changes"]["added"] == [{"symbol": "CLS", "status": "RULES_MET"}]
    assert r3["result"] == "NO_CHANGE" and events(lab) == []           # a list change, not a rule-state change


# ---- 57 / 58 / 59: dedup, no backfill, races ---------------------------------------------------------------------------------

def test_57_same_session_twice_is_a_no_op():
    lab = world({S1: {"MU": 3}, S2: {"AMD": 3}})
    sid = save(lab)
    check(lab, sid, S1)
    first = check(lab, sid, S2)
    again = check(lab, sid, S2, trigger="SCHEDULED")
    assert first["result"] == "ALERT_CREATED" and again["result"] == "ALREADY_CHECKED" and again["alert"] is None
    assert len(snaps(lab, sid)) == 2 and len(events(lab)) == 1


def test_58_offline_sessions_are_never_reconstructed():
    lab = world({S1: {"MU": 3}, S2: {"AMD": 3}, S3: {"CLS": 3}, S4: {"AMD": 3}})
    sid = save(lab)
    check(lab, sid, S1)                                                # then the server was off for S2 and S3
    r = check(lab, sid, S4)
    assert [s["decision_session"] for s in snaps(lab, sid)] == ["2026-09-30", "2026-09-25"]
    (e,) = events(lab, sid)                                            # ONE comparison: S4 against S1, nothing in between
    assert (e["previous_session"], e["decision_session"]) == ("2026-09-25", "2026-09-30") and r["result"] == "ALERT_CREATED"
    assert check(lab, sid, S3)["result"] == "NEWER_SNAPSHOT_EXISTS" and len(snaps(lab, sid)) == 2


def test_59_manual_and_automatic_race_store_one_snapshot_and_one_event():
    lab = world({S1: {"MU": 3}, S2: {"AMD": 3}})
    sid = save(lab)
    check(lab, sid, S1)
    lab.market.now = FL.at(S2)
    out, barrier = [], threading.Barrier(2)

    def go(trigger):
        barrier.wait()
        k = kw(lab)
        path = k.pop("path")
        out.append(SS.check(sid, trigger, FL.at(S2), path=path, **k)["result"])
    ts = [threading.Thread(target=go, args=(t,)) for t in ("MANUAL_CHECK", "SCHEDULED")]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert sorted(out) == ["ALERT_CREATED", "ALREADY_CHECKED"] and len(snaps(lab, sid)) == 2 and len(events(lab)) == 1
    # two PROCESSES (no shared in-process lock): the SQLite write transaction + UNIQUE decide
    lab2 = world({S1: {"MU": 3}, S2: {"AMD": 3}})
    sid2 = save(lab2)
    check(lab2, sid2, S1)
    lab2.market.now = FL.at(S2)
    k = kw(lab2)
    k.pop("path")
    res = SC.scan(vid(lab2), "SAVED_UNIVERSE", None, now=FL.at(S2), path=lab2.path, **k)
    scan = SS.SavedScanStore(lab2.path).scan(sid2)
    got, b2 = [], threading.Barrier(2)

    def raw():
        b2.wait()
        got.append(SS.SavedScanStore(lab2.path).record(scan, res, "SCHEDULED")["result"])
    ts = [threading.Thread(target=raw) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert sorted(got) == ["ALERT_CREATED", "ALREADY_CHECKED"] and len(events(lab2)) == 1


# ---- 60 + scheduler integration --------------------------------------------------------------------------------------------------

@pytest.fixture
def ext(monkeypatch):
    """The registered extension, pointed at a lab (as the scheduler would use the app database)."""
    SS.register()
    state = {}
    monkeypatch.setattr(SS.EXTENSION, "path_fn", lambda: Path(state["lab"].path))
    monkeypatch.setattr(SS.EXTENSION, "scan_kw_fn", lambda: {k: v for k, v in kw(state["lab"]).items() if k != "path"})
    monkeypatch.setattr(RO, "db_path", lambda: Path(state["lab"].path))
    return state


def scheduler(lab, session):
    clock = FL.at(session)
    lab.market.now = clock
    return A.Scheduler(store_fn=lambda: A.AutomationStore(Path(lab.path)), clock=lambda: clock, startup_delay_s=3600)


def test_60_multi_scan_isolation_in_one_scheduler_check(ext, monkeypatch):
    lab = world({S1: {"MU": 3}, S2: {"AMD": 3}})
    lab.save(spec(G("ALL", UP), symbols=("AMD", "MU"), name="Second"))
    lab.save(spec(G("ALL", UP), symbols=("CLS",), name="Broken"))
    ext["lab"] = lab
    a, b, c = save(lab), save(lab, v=vid(lab, "Second")), save(lab, v=vid(lab, "Broken"))
    real = SC.scan

    def flaky(v_, *a_, **k_):
        if v_ == vid(lab, "Broken"):
            raise RuntimeError("provider down for this one")
        return real(v_, *a_, **k_)
    monkeypatch.setattr(SC, "scan", flaky)
    s1 = scheduler(lab, S1).step()
    x = s1["extensions"]["saved_scans"]
    assert x["counts"]["baselines"] == 2 and x["counts"]["errors"] == 1 and x["result"] == "PARTIAL_FAILURE" and s1["retryable"]
    assert s1["result"] == "FORWARD_CAPTURE_OFF" and s1["journals"] == []           # forward capture is off: untouched
    assert {r["name"]: r["result"] for r in x["scans"]}["Broken v1 · Saved universe"] == "ERROR"
    s2 = scheduler(lab, S2).step()["extensions"]["saved_scans"]
    assert s2["counts"]["alerts_created"] == 2 and len(events(lab)) == 2 and snaps(lab, c) == []


def test_scheduler_runs_only_when_forward_capture_or_an_alerting_scan_needs_it(ext):
    lab = world({S1: {"MU": 3}})
    ext["lab"] = lab
    sch = scheduler(lab, S1)
    assert sch.step() is None and A.extensions_wanted() is False               # nothing opted in: no check at all
    sid = save(lab, alerts=False)
    assert sch.step() is None                                                  # a saved scan with alerts OFF: still nothing
    SS.settings(sid, alerts_enabled=True, path=lab.path)
    assert A.extensions_wanted() is True and sch.step()["extensions"]["saved_scans"]["counts"]["baselines"] == 1
    st = sch.status()
    assert st["enabled"] is False and st["next_check_at"] and st["extensions"]["saved_scans"]["alerts_on"] == 1


def test_paused_and_archived_scans(ext):
    lab = world({S1: {"MU": 3}, S2: {"AMD": 3}, S3: {"MU": 3}})
    ext["lab"] = lab
    sid = save(lab)
    scheduler(lab, S1).step()
    SS.settings(sid, alerts_enabled=False, path=lab.path)                      # pause: no scheduled evaluation
    assert scheduler(lab, S2).step() is None and len(snaps(lab, sid)) == 1
    assert check(lab, sid, S2)["result"] == "ALERT_CREATED"                    # a manual check still works (same path)
    SS.archive(sid, path=lab.path)
    with pytest.raises(SS.SavedScanError) as e:
        check(lab, sid, S3)
    assert e.value.code == "ARCHIVED" and e.value.status == 409
    assert len(snaps(lab, sid)) == 2 and len(events(lab)) == 1                 # history is kept
    with pytest.raises(SS.SavedScanError):
        SS.settings(sid, alerts_enabled=True, path=lab.path)


def test_two_schedulers_never_duplicate(ext):
    lab = world({S1: {"MU": 3}, S2: {"AMD": 3}})
    ext["lab"] = lab
    sid = save(lab)
    scheduler(lab, S1).step()
    one, two = scheduler(lab, S2), scheduler(lab, S2)
    out, barrier = [], threading.Barrier(2)

    def go(s):
        barrier.wait()
        out.append(s.step()["extensions"]["saved_scans"]["result"])
    ts = [threading.Thread(target=go, args=(s,)) for s in (one, two)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert len(snaps(lab, sid)) == 2 and len(events(lab)) == 1
    assert set(out) <= {"ALERTS_CREATED", "LEASE_HELD", "CHECKED"}


def test_64_stage_37_summary_is_unchanged_when_no_scan_alerts(ext):
    lab = world({S1: {}})
    ext["lab"] = lab
    A.AutomationStore(Path(lab.path)).save(enabled=True)
    s = scheduler(lab, S1).step()
    assert "extensions" not in s and s["result"] == "NO_ACTIVE_JOURNALS"      # byte-for-byte the Stage 3.7 summary


# ---- 61: migration ---------------------------------------------------------------------------------------------------------------

def test_61_migration_is_additive_idempotent_and_enforces_immutability():
    lab = world({S1: {"MU": 3}, S2: {"AMD": 3}})
    with sqlite3.connect(lab.path) as c:
        uv = c.execute("PRAGMA user_version").fetchone()[0]
        before = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    sid = save(lab)
    check(lab, sid, S1)
    check(lab, sid, S2)
    from database.saved_scan_migrations import run_saved_scan_migrations
    with sqlite3.connect(lab.path) as c:
        for _ in range(3):
            run_saved_scan_migrations(c)
        after = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert after - before == {"saved_strategy_scans", "saved_scan_snapshots", "saved_scan_alert_events"}
        assert c.execute("PRAGMA user_version").fetchone()[0] == uv
        for bad in ("UPDATE saved_strategy_scans SET strategy_version_id = 'x' || substr(strategy_version_id, 2)",
                    "UPDATE saved_strategy_scans SET custom_symbols_json = '[\"AMD\"]'",
                    "DELETE FROM saved_strategy_scans", "UPDATE saved_scan_snapshots SET status_map_json = '{}'",
                    "DELETE FROM saved_scan_snapshots", "UPDATE saved_scan_alert_events SET newly_rules_met_json = '[]'",
                    "DELETE FROM saved_scan_alert_events"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(bad)
        row = c.execute("SELECT * FROM saved_scan_snapshots LIMIT 1").fetchone()
        cols = [r[1] for r in c.execute("PRAGMA table_info(saved_scan_snapshots)")]
        with pytest.raises(sqlite3.DatabaseError, match="never replaced"):
            c.execute(f"INSERT OR REPLACE INTO saved_scan_snapshots ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", row)
        c.execute("UPDATE saved_scan_alert_events SET read_at = '2026-10-01T00:00:00+00:00'")      # read state: allowed
        c.execute("UPDATE saved_strategy_scans SET name = 'Renamed', alerts_enabled = 0")         # admin fields: allowed


def test_62_only_saved_scan_tables_are_written():
    lab = world({S1: {"MU": 3}, S2: {"AMD": 3}})
    sid = save(lab)
    checkpoint(lab.path)

    def digest_without_saved():
        conn = sqlite3.connect(lab.path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        out = {n: conn.execute(f'SELECT count(*), total(length(quote("{n}"))) FROM "{n}"').fetchone()
               for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'saved_%'")}
        dump = "\n".join(conn.iterdump())
        conn.close()
        return out, "\n".join(line for line in dump.splitlines() if "saved_s" not in line)
    before = digest_without_saved()
    check(lab, sid, S1)
    check(lab, sid, S2)
    SS.SavedScanStore(lab.path).mark_read(None)
    assert digest_without_saved() == before


# ---- 63: Stage 4.0 parity -------------------------------------------------------------------------------------------------------

def test_63_snapshot_is_the_stage_40_scanner_result():
    lab = world({S1: {"MU": 3, "AMD": 1}})
    sid = save(lab, "CUSTOM", ["mu", "AMD", "cls", "NVDA", "amd"])
    r = check(lab, sid, S1)
    k = kw(lab)
    k.pop("path")
    direct = SC.scan(vid(lab), "CUSTOM", ["AMD", "CLS", "MU", "NVDA"], now=FL.at(S1), path=lab.path, **k)
    assert r["snapshot"]["status_map"] == {x["symbol"]: x["fit_status"] for x in direct["results"]}
    assert r["snapshot"]["resolved_symbols"] == direct["requested_symbols"] == ["AMD", "CLS", "MU", "NVDA"]
    assert r["snapshot"]["groups"] == {g["group"]: g["symbols"] for g in direct["groups"]}
    assert r["snapshot"]["spec_hash"] == direct["strategy"]["spec_hash"] and r["snapshot"]["decision_session"] == "2026-09-25"


# ---- API, AI, broker, language, security --------------------------------------------------------------------------------------

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
    lab = world({S1: {"MU": 3}, S2: {"AMD": 3}})
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr("data.market_data.fetch_daily_bars", lab.market.fetch)
    monkeypatch.setattr("data.market_data.get_data_client", lambda: object())
    monkeypatch.setattr("scanner.watchlist.load_watchlist", lambda *a, **k: ["AMD", "MU"])
    monkeypatch.setattr("database.database.get_db", lambda: lab.research)
    clock = [FL.at(S1)]
    monkeypatch.setattr(FC, "_utc", lambda now=None: clock[0])
    monkeypatch.setattr(FC, "BAR_CACHE", FC.BarCache())
    monkeypatch.setattr(A, "reconcile", lambda: {})                            # no real scheduler thread in tests
    lab.market.now = clock[0]
    c = TestClient(app)
    c.lab, c.ai, c.http, c.clock = lab, ai, http, clock
    return c


def test_api_round_trip(api):
    v = vid(api.lab)
    r = api.post("/api/saved-scans", json={"strategy_version_id": v, "source": "WATCHLIST", "name": "Pullback watchlist",
                                           "alerts_enabled": True})
    assert r.status_code == 201 and r.json()["alerts_enabled"] is True and r.json()["latest_snapshot"] is None
    sid = r.json()["saved_scan_id"]
    dup = api.post("/api/saved-scans", json={"strategy_version_id": v, "source": "WATCHLIST"})
    assert dup.status_code == 409 and dup.json()["status"] == "ALREADY_SAVED" and dup.json()["saved_scan_id"] == sid
    c1 = api.post(f"/api/saved-scans/{sid}/check", json={}).json()
    assert c1["check"]["result"] == "BASELINE" and c1["saved_scan"]["latest_snapshot"]["rules_met"] == ["MU"]
    api.clock[0] = FL.at(S2)
    api.lab.market.now = api.clock[0]
    c2 = api.post(f"/api/saved-scans/{sid}/check").json()
    assert c2["check"]["result"] == "ALERT_CREATED"
    assert c2["check"]["alert"]["texts"] == ["AMD newly meets the saved entry rules for Up v1 (RULES MET).",
                                             "MU no longer meets the saved entry rules for Up v1 (now RULES NOT MET)."]
    al = api.get("/api/strategy-alerts").json()
    assert al["unread"] == 1 and len(al["alerts"]) == 1 and al["alerts"][0]["label"] == "Up v1"
    aid = al["alerts"][0]["alert_id"]
    assert api.post(f"/api/strategy-alerts/{aid}/read", json={}).json() == {"marked": 1, "unread": 0}
    assert api.get("/api/strategy-alerts?unread_only=true").json()["alerts"] == []
    lst = api.get("/api/saved-scans").json()
    assert lst["saved_scans"][0]["latest_snapshot"]["decision_session"] == "2026-09-28" and lst["unread_alerts"] == 0
    assert api.post(f"/api/saved-scans/{sid}/settings", json={"alerts_enabled": False}).json()["alerts_enabled"] is False
    assert api.post(f"/api/saved-scans/{sid}/archive", json={}).json()["archived_at"]
    assert api.post(f"/api/saved-scans/{sid}/check").status_code == 409
    assert api.ai == [] and api.http.requests == []


def test_api_bodies_are_strict_and_holdings_is_deferred(api):
    v = vid(api.lab)
    post = lambda b: api.post("/api/saved-scans", json=b)  # noqa: E731
    assert post({"strategy_version_id": v, "source": "WATCHLIST", "spec": {"entry": {}}}).status_code == 422
    assert post({"strategy_version_id": v, "source": "WATCHLIST", "sql": "DROP TABLE x"}).status_code == 422
    h = post({"strategy_version_id": v, "source": "HOLDINGS"})
    assert h.status_code == 422 and h.json()["status"] == "HOLDINGS_DEFERRED" and api.http.requests == []
    assert post({"strategy_version_id": v, "source": "CUSTOM", "symbols": ["AMD", "bad/x"]}).json()["status"] == "INVALID_SYMBOL"
    assert post({"strategy_version_id": v, "source": "WATCHLIST", "symbols": ["AMD"]}).json()["status"] == "SYMBOLS_NOT_ACCEPTED"
    assert post({"strategy_version_id": v, "source": "CUSTOM", "symbols": [f"S{i}" for i in range(101)]}).json()["status"] == "TOO_MANY_SYMBOLS"
    ok = post({"strategy_version_id": v, "source": "CUSTOM", "symbols": ["cls, amd", "AMD"]})
    assert ok.status_code == 201 and ok.json()["custom_symbols"] == ["AMD", "CLS"]
    sid = ok.json()["saved_scan_id"]
    assert api.post(f"/api/saved-scans/{sid}/settings", json={"strategy_version_id": v}).status_code == 422   # identity is fixed
    assert api.post(f"/api/saved-scans/{sid}/settings", json={"list_source": "WATCHLIST"}).status_code == 422
    assert api.post("/api/saved-scans", json={"strategy_version_id": "0" * 32, "source": "WATCHLIST"}).status_code == 404
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith(("/api/saved-scans", "/api/strategy-alerts"))}
    assert mine == {"/api/saved-scans": {"get", "post"}, "/api/saved-scans/{saved_scan_id}": {"get"},
                    "/api/saved-scans/{saved_scan_id}/check": {"post"}, "/api/saved-scans/{saved_scan_id}/settings": {"post"},
                    "/api/saved-scans/{saved_scan_id}/archive": {"post"}, "/api/strategy-alerts": {"get"},
                    "/api/strategy-alerts/read-all": {"post"}, "/api/strategy-alerts/{alert_id}/read": {"post"}}


def test_saved_scans_keep_their_exact_version():
    lab = world({S1: {"MU": 3}})
    sid = save(lab)
    v1 = vid(lab)
    from strategy.store import StrategyStore
    import bt_fixtures as X
    sid_strategy = RO.saved_versions(Path(lab.path))[0]["strategy_id"]
    StrategyStore(lab.path).add_version(sid_strategy, X.spec(symbols=("AMD", "MU", "CLS"), name="Up", entry=G("ALL", TRUE)))
    r = check(lab, sid, S1)
    assert r["snapshot"]["strategy_version_id"] == v1 and r["snapshot"]["rules_met"] == ["MU"]      # never upgraded to v2
    info = SS.get(sid, path=lab.path)
    assert info["strategy_version_id"] == v1 and info["is_current"] is False


def test_language_security_and_no_second_evaluator():
    bad = re.compile(r"(?i)\b(buy|sell|trade now|entry opportunity|best setup|high[- ]conviction|recommend\w*|score|probability|confidence)\b")
    for f in ("fit/saved_scans.py", "api/routes/saved_scans.py", "frontend/saved_scans.js"):
        src = (ROOT / f).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith(("#", "//", "*")))
        assert not bad.findall(code.replace("not trade signals", "")), (f, bad.findall(code))
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|new Function|pickle|importlib", src), f
        assert not re.search(r"TradingClient|alpaca\.trading|place_?order|submit_?order|/orders|anthropic|get_provider|"
                             r"from agents|import agents|from portfolio|import portfolio|rh_gateway|setInterval|setTimeout|"
                             r"apscheduler|BackgroundScheduler|threading\.Timer|smtp|webhook|twilio|discord|slack", src, re.I), f
    svc = (ROOT / "fit" / "saved_scans.py").read_text(encoding="utf-8")
    assert "SC.scan(" in svc and "group_met" not in svc and "evaluate_version" not in svc and "day_snapshot" not in svc
    auto = (ROOT / "forward" / "automation.py").read_text(encoding="utf-8")
    imports = [line for line in auto.splitlines() if line.lstrip().startswith(("import ", "from "))]
    assert not [line for line in imports if "fit" in line or "scan" in line]                            # isolation:
    assert "saved_scans" not in auto and "SC." not in auto and "SS." not in auto                        # hook only
