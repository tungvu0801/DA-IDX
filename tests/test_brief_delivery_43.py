"""Stage 4.3 — opt-in local Windows desktop delivery of the Stage 4.2 Daily Brief (notifications/delivery.py). A fake OS
adapter everywhere (conftest also fails any real adapter call); no market data, no AI, no broker; at most once per
session; no backfill; the brief itself is never changed."""
import copy
import json
import re
import socket
import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fw_fixtures as FL
from brief import daily as DB
from fit import readonly as RO
from fit import saved_scans as SS
from fit import scanner as SC
from forward import automation as A
from forward import journal as J
from notifications import delivery as D
from notifications import windows as W
from notifications.windows import send as REAL_SEND        # the real adapter (conftest replaces W.send at test time)
from test_daily_brief_42 import EVT, stable, system
from test_evidence_35 import fspec
from test_fit_34 import G
from test_saved_scans_41 import S1, S2, S3, S4, UP, check, kw

ROOT = Path(__file__).resolve().parents[1]
BANNED = re.compile(r"(?i)\b(buy|sell|trade now|enter|exit now|best|top|recommend\w*|high confidence|confidence|opportunit\w*|"
                    r"probability|signal)\b")


class Fake:
    def __init__(self):
        self.calls, self.result, self.raises = [], {"status": "DELIVERED", "error_code": None}, None

    def send(self, title, body):
        self.calls.append((title, body))
        if self.raises:
            raise self.raises
        return dict(self.result)


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(D, "ADAPTER", f)
    monkeypatch.setattr(A, "reconcile", lambda: {})                 # never start the process-wide scheduler thread here
    monkeypatch.setattr(D, "_last_test", [0.0])
    return f


def lab_at(moves=None, journal=True, **kw_):
    lab, sid, jid, ss = system(moves or {S1: {"MU": 5}, S2: {"AMD": 5}, S3: {"CLS": 5}, S4: {"AMD": 5}}, journal=journal, **kw_)
    return lab, sid, jid, ss


def store(lab, ss, jid, s):
    check(lab, ss, s)
    if jid:
        lab.record(jid, FL.at(s))


def deliver(lab, s, trigger="SCHEDULED", owner="t"):
    lab.market.now = FL.at(s)
    return D.deliver(FL.at(s), trigger, owner, path=lab.path)


def rows(lab, kind="SESSION"):
    return [r for r in D.DeliveryStore(lab.path).history(100) if r["kind"] == kind][::-1]


def enable(lab, s, **k):
    return D.configure(True, path=lab.path, now=FL.at(s), **k)


# ---- defaults, enable, baseline (44) ----------------------------------------------------------------------------------------

def test_off_by_default_and_nothing_happens_while_off(fake):
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    st = D.status(path=lab.path)
    assert st["enabled"] is False and st["notify_only_if_activity"] is True and st["history"] == [] and st["last_delivery"] is None
    assert deliver(lab, S1)["result"] == "DISABLED" and fake.calls == [] and D.EXTENSION.__class__(lambda: Path(lab.path)).wanted() is False


def test_44_first_enable_baselines_history_and_sends_nothing(fake):
    lab, _, jid, ss = lab_at()
    for s in (S1, S2, S3):
        store(lab, ss, jid, s)
    st = enable(lab, S3)
    assert st["enabled"] is True and [(r["brief_session"], r["status"], r["trigger"]) for r in rows(lab)] == [("2026-09-29", "BASELINE", "ENABLE")]
    for _ in range(3):
        assert deliver(lab, S3)["result"] == "NO_NEW_SESSION"
    assert fake.calls == []                                                   # no burst of old briefs
    D.configure(False, path=lab.path)
    store(lab, ss, jid, S4)                                                   # stored while OFF
    enable(lab, S4)                                                           # re-enable: a new baseline, no replay
    assert [r["status"] for r in rows(lab)] == ["BASELINE", "BASELINE"] and deliver(lab, S4)["result"] == "NO_NEW_SESSION"
    assert fake.calls == []


# ---- 45 / 46 / 53: one delivery per new session; same session / restart never resend -------------------------------------------

def test_45_46_53_new_session_once_same_session_and_restart_never_resend(fake):
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    enable(lab, S1)
    store(lab, ss, jid, S2)
    r = deliver(lab, S2)
    assert r["result"] == "DELIVERED" and r["brief_session"] == "2026-09-28" and len(fake.calls) == 1
    msg = D.compose(DB.build("2026-09-28", path=lab.path))
    (title, body), = fake.calls
    assert (title, body) == (msg["title"], msg["body"]) and r["fingerprint"] == msg["fingerprint"]
    for _ in range(3):
        assert deliver(lab, S2)["result"] == "NO_NEW_SESSION"             # a second check, or a restart
    assert len(fake.calls) == 1
    last = rows(lab)[-1]
    assert (last["status"], last["brief_session"], last["summary_fingerprint"], last["body"]) == ("DELIVERED", "2026-09-28", msg["fingerprint"], body)
    assert last["delivered_at"] and last["error_code"] is None


def test_53_an_interrupted_pending_claim_is_never_resent(fake):
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    enable(lab, S1)
    store(lab, ss, jid, S2)
    D.DeliveryStore(lab.path).insert(D._row("SESSION", "PENDING", "SCHEDULED", FL.at(S2), "2026-09-28",
                                            D.compose(DB.build("2026-09-28", path=lab.path))))       # crashed after the claim
    assert deliver(lab, S2)["result"] == "NO_NEW_SESSION" and fake.calls == []
    assert D.status(path=lab.path)["last_delivery"]["status"] == "PENDING"


def test_no_backfill_only_the_latest_session_is_considered(fake):
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    enable(lab, S1)
    for s in (S2, S3, S4):                                                   # the server was off for three sessions
        store(lab, ss, jid, s)
    assert deliver(lab, S4)["brief_session"] == "2026-09-30" and len(fake.calls) == 1
    assert [r["brief_session"] for r in rows(lab)] == ["2026-09-25", "2026-09-30"]


# ---- 47 / 48 / 49: activity, exact counts, change preview ------------------------------------------------------------------------

def test_47_no_activity_is_skipped_and_recorded(fake):
    lab, _, _, ss = lab_at({S1: {"MU": 3}, S2: {"MU": 3}, S3: {"MU": 3}}, journal=False)
    check(lab, ss, S1)
    enable(lab, S1)
    check(lab, ss, S2)                                                        # a check with no RULES MET change
    assert deliver(lab, S2)["result"] == "SKIPPED_NO_ACTIVITY" and fake.calls == []
    assert rows(lab)[-1]["status"] == "SKIPPED_NO_ACTIVITY" and rows(lab)[-1]["summary_fingerprint"]
    D.configure(notify_only_if_activity=False, path=lab.path)
    check(lab, ss, S3)
    assert deliver(lab, S3)["result"] == "DELIVERED" and len(fake.calls) == 1
    assert "0 rule-state changes · 0 forward captures · 0 completed cycles" in fake.calls[0][1]


def _brief(changes=(), rc=0, fc=0, cc=0, di=0, missed=0):
    return {"brief_session": "2026-09-30", "changes": list(changes),
            "summary_counts": {"rule_change_events": rc, "forward_captures": fc, "completed_reference_cycles": cc, "data_issues": di,
                               "forward_sessions_missed": missed}}


def test_48_body_uses_exactly_the_stage_42_counts():
    b = _brief([{"label": "Pullback v1", "newly_rules_met": [{"symbol": "AMD"}], "no_longer_rules_met": []}], rc=1, fc=2, di=1)
    m = D.compose(b)
    assert m["title"] == "Stock Agent · Daily Strategy Brief" and m["activity"] is True
    assert m["body"] == ("Sep 30 close\n1 rule-state change · 2 forward captures · 0 completed cycles\n1 data / continuity issue\n"
                         "AMD newly meets the saved entry rules (Pullback v1).\nOpen Daily Brief for details.")
    assert m["fingerprint"] == D.sha({"brief_session": "2026-09-30", "channel": "WINDOWS_DESKTOP", "title": m["title"], "body": m["body"]})
    assert D.compose(copy.deepcopy(b)) == m                                  # deterministic; no timestamp inside


def test_49_at_most_two_change_lines_then_a_count():
    b = _brief([{"label": "Alpha v1", "newly_rules_met": [{"symbol": "AMD"}, {"symbol": "MU"}], "no_longer_rules_met": []},
                {"label": "Beta v2", "newly_rules_met": [], "no_longer_rules_met": [{"symbol": "CLS", "status": "INCOMPLETE_DATA"}]}], rc=2)
    m = D.compose(b)
    lines = m["body"].split("\n")
    assert lines[2:] == ["AMD newly meets the saved entry rules (Alpha v1).", "MU newly meets the saved entry rules (Alpha v1).",
                         "+1 more change", "Open Daily Brief for details."]
    assert m["preview_lines"] == 2 and m["changes_total"] == 3
    one = D.compose(_brief([{"label": "Beta v2", "newly_rules_met": [], "no_longer_rules_met": [{"symbol": "CLS", "status": "INCOMPLETE_DATA"}]}], rc=1))
    assert "CLS is no longer in RULES MET (Beta v2); now INCOMPLETE DATA." in one["body"]
    long = D.compose(_brief([{"label": "L" * 70 + " v1", "newly_rules_met": [{"symbol": s} for s in ("AAAA", "BBBB", "CCCC")],
                              "no_longer_rules_met": []}], rc=1, fc=12, cc=3, di=9))
    assert len(long["body"]) <= W.BODY_MAX and long["body"].endswith("Open Daily Brief for details.") and "more change" in long["body"]
    assert all(len(line) <= D.PREVIEW_LINE_MAX for line in long["body"].split("\n"))


def test_activity_is_the_stage_42_counts_only():
    assert D.activity({"rule_change_events": 0, "forward_captures": 0, "completed_reference_cycles": 0, "data_issues": 0,
                       "forward_sessions_missed": 0, "saved_scans_checked": 5}) is False
    for k in ("rule_change_events", "forward_captures", "forward_sessions_missed", "completed_reference_cycles", "data_issues"):
        assert D.activity({k: 1}) is True, k


# ---- 50 / 51: partial failure, OS failure, platform ----------------------------------------------------------------------------

@pytest.fixture
def exts(monkeypatch):
    state = {}
    SS.register()
    D.register()
    monkeypatch.setattr(SS.EXTENSION, "path_fn", lambda: Path(state["lab"].path))
    monkeypatch.setattr(SS.EXTENSION, "scan_kw_fn", lambda: {k: v for k, v in kw(state["lab"]).items() if k != "path"})
    monkeypatch.setattr(D.EXTENSION, "path_fn", lambda: Path(state["lab"].path))
    monkeypatch.setattr(RO, "db_path", lambda: Path(state["lab"].path))
    return state


def scheduler(lab, s, capture=False):
    """A Stage 3.7 scheduler over the lab (capture=True: its forward capture uses the lab's journal store and market)."""
    import functools
    lab.market.now = FL.at(s)
    extra = {"check_fn": functools.partial(A.run_check, fs=lab.fs, bs=lab.store, fetch_fn=lab.market.fetch, client=object(),
                                           research_db=lambda: lab.research, events_fn=lab.events.build,
                                           coverage_fn=lab.events.cov)} if capture else {}
    return A.Scheduler(store_fn=lambda: A.AutomationStore(Path(lab.path)), clock=lambda: FL.at(s), startup_delay_s=3600, **extra)


def test_50_partial_failure_is_delivered_with_its_stored_data_issue(exts, fake, monkeypatch):
    lab, _, _, ss = lab_at({S1: {"MU": 3}, S2: {"AMD": 3}}, journal=False)
    lab.save(fspec(symbols=("CLS",), entry=G("ALL", UP), name="Broken"))
    exts["lab"] = lab
    broken = SS.create(next(v["strategy_version_id"] for v in RO.saved_versions(Path(lab.path)) if v["strategy_name"] == "Broken"),
                       "SAVED_UNIVERSE", None, True, None, path=lab.path)["saved_scan_id"]
    scheduler(lab, S1).step()                                                 # baselines; delivery is still OFF
    enable(lab, S1)
    real = SC.scan
    monkeypatch.setattr(SC, "scan", lambda v, *a, **k: (_ for _ in ()).throw(RuntimeError("down")) if v == SS.SavedScanStore(lab.path).scan(broken)["strategy_version_id"] else real(v, *a, **k))
    summary = scheduler(lab, S2).step()
    x = summary["extensions"]
    assert x["saved_scans"]["result"] == "PARTIAL_FAILURE" and x["daily_brief_delivery"]["result"] == "DELIVERED"
    assert list(x) == ["saved_scans", "daily_brief_delivery"]                # delivery after the checks
    (title, body), = fake.calls
    assert "1 rule-state change" in body and "1 data / continuity issue" in body
    stored = A.AutomationStore(Path(lab.path)).settings()["last_check"]["extensions"]
    assert stored["daily_brief_delivery"]["result"] == "DELIVERED" and stored["saved_scans"]["result"] == "PARTIAL_FAILURE"


def test_51_os_failure_is_recorded_and_isolated(exts, fake):
    lab, _, jid, ss = lab_at()
    exts["lab"] = lab
    store(lab, ss, jid, S1)
    enable(lab, S1)
    A.AutomationStore(Path(lab.path)).save(enabled=True)
    fake.raises = OSError("notification area unavailable")
    lab.market.now = FL.at(S2)
    check(lab, ss, S2)
    summary = scheduler(lab, S2, capture=True).step()
    d = summary["extensions"]["daily_brief_delivery"]
    assert d["result"] == "FAILED" and d["error_code"] == "OSError" and d["retryable"] is False
    assert summary["result"] == "CAPTURED" and summary["counts"]["captured"] == 1          # forward capture unaffected
    assert rows(lab)[-1]["status"] == "FAILED" and rows(lab)[-1]["error_code"] == "OSError"
    assert not summary["retryable"] and len(fake.calls) == 1                           # no retry storm
    assert deliver(lab, S2)["result"] == "NO_NEW_SESSION" and len(fake.calls) == 1


def test_unsupported_platform_is_a_status_not_a_crash(fake, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert REAL_SEND("t", "b") == {"status": "UNSUPPORTED_PLATFORM", "error_code": "NOT_WINDOWS"}
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    enable(lab, S1)
    store(lab, ss, jid, S2)
    fake.result = {"status": "UNSUPPORTED_PLATFORM", "error_code": "NOT_WINDOWS"}
    assert deliver(lab, S2)["result"] == "UNSUPPORTED_PLATFORM" and rows(lab)[-1]["status"] == "UNSUPPORTED_PLATFORM"
    assert D.status(path=lab.path)["platform_supported"] is False


# ---- 52: two schedulers / two servers ------------------------------------------------------------------------------------------

def test_52_two_schedulers_deliver_once(exts, fake):
    lab, _, jid, ss = lab_at()
    exts["lab"] = lab
    store(lab, ss, jid, S1)
    enable(lab, S1)
    store(lab, ss, jid, S2)
    out, barrier = [], threading.Barrier(3)

    def go(owner):
        barrier.wait()
        out.append(D.deliver(FL.at(S2), "SCHEDULED", owner, path=lab.path)["result"])
    ts = [threading.Thread(target=go, args=(f"server-{i}",)) for i in range(3)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert out.count("DELIVERED") == 1 and set(out) <= {"DELIVERED", "LEASE_HELD", "NO_NEW_SESSION", "ALREADY_HANDLED"}
    assert len(fake.calls) == 1 and len([r for r in rows(lab) if r["brief_session"] == "2026-09-28"]) == 1


# ---- 54 / 55: test notification; opening / refreshing the brief sends nothing ------------------------------------------------------

@pytest.fixture
def api(monkeypatch, fake):
    import agents.technical_agent as ta
    from agents.usage_tracker import EXPLANATION, RESEARCH, usage_tracker
    from ai_explain import service as AX
    from api.routes import portfolio as pr
    from api.server import app
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    store(lab, ss, jid, S2)
    hits = []

    def fail(what):
        def _f(*a, **k):
            hits.append(what)
            raise RuntimeError(what)
        return _f
    for target, what in ((ta, "claude research"), (AX, "claude explanation")):
        monkeypatch.setattr(target, "get_provider", fail(what))
    monkeypatch.setattr(pr, "provider_factory", fail("broker"))
    monkeypatch.setattr("data.market_data.fetch_daily_bars", fail("market"))
    monkeypatch.setattr("data.market_data.get_data_client", fail("market client"))
    monkeypatch.setattr(SC, "scan", fail("scanner"))
    monkeypatch.setattr(socket, "create_connection", fail("network"))
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    c = TestClient(app)
    c.lab, c.hits = lab, hits
    c.budgets = lambda: (usage_tracker.budget(RESEARCH)["used_today"], usage_tracker.budget(EXPLANATION)["used_today"], len(usage_tracker._records))
    return c


def test_54_test_notification_is_fixed_server_text_and_marked_test(api, fake):
    b0 = api.budgets()
    r = api.post("/api/brief-delivery/test", json={})
    assert r.status_code == 200 and r.json()["test"]["result"] == "DELIVERED"
    assert fake.calls == [(D.TEST_TITLE, D.TEST_BODY)] and "TEST" in D.TEST_BODY
    (t,) = D.DeliveryStore(api.lab.path).history()
    assert (t["kind"], t["brief_session"], t["status"], t["trigger"]) == ("TEST", None, "DELIVERED", "TEST")
    assert api.post("/api/brief-delivery/test", json={}).status_code == 429       # no rapid repeats
    assert api.post("/api/brief-delivery/test", json={"title": "BUY NOW", "body": "x"}).status_code == 422
    assert api.hits == [] and api.budgets() == b0 and len(fake.calls) == 1


def test_55_opening_refreshing_and_navigating_the_brief_sends_nothing(api, fake):
    api.post("/api/brief-delivery/settings", json={"enabled": True})
    before = D.DeliveryStore(api.lab.path).history()
    for path in ("/api/daily-brief", "/api/daily-brief?session=2026-09-25", "/api/daily-brief", "/api/brief-delivery",
                 "/api/brief-delivery/history"):
        assert api.get(path).status_code == 200
    assert fake.calls == [] and D.DeliveryStore(api.lab.path).history() == before and api.hits == []


def test_api_is_strict_and_accepts_no_notification_text(api):
    assert api.get("/api/brief-delivery").json()["enabled"] is False
    assert api.post("/api/brief-delivery/settings", json={}).json()["status"] == "NOTHING_TO_CHANGE"
    for bad in ({"enabled": True, "title": "x"}, {"body": "x"}, {"enabled": "yes please"}, {"channel": "SMTP"}):
        assert api.post("/api/brief-delivery/settings", json=bad).status_code == 422, bad
    on = api.post("/api/brief-delivery/settings", json={"enabled": True}).json()
    assert on["enabled"] is True and on["last_delivery"]["status"] == "BASELINE" and on["next"] == D.NEXT_TEXT
    assert api.get("/api/brief-delivery/history?limit=21").status_code == 422
    from api.server import app
    spec = app.openapi()
    mine = {p: sorted(o) for p, o in spec["paths"].items() if p.startswith("/api/brief-delivery")}
    assert mine == {"/api/brief-delivery": ["get"], "/api/brief-delivery/history": ["get"], "/api/brief-delivery/settings": ["post"],
                    "/api/brief-delivery/test": ["post"]}
    sch = spec["components"]["schemas"]

    def body_schema(path):
        s = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        ref = s.get("$ref") or next(x["$ref"] for x in s.get("anyOf", []) if "$ref" in x)
        return sch[ref.rsplit("/", 1)[-1]]
    settings, empty = body_schema("/api/brief-delivery/settings"), body_schema("/api/brief-delivery/test")
    assert set(settings["properties"]) == {"enabled", "notify_only_if_activity"} and settings["additionalProperties"] is False
    assert empty.get("properties", {}) == {} and empty["additionalProperties"] is False


# ---- 56 / 57 / 58: no market, no AI, no broker during a real delivery --------------------------------------------------------------

def test_56_57_58_delivery_needs_no_market_ai_or_broker(api, fake):
    b0 = api.budgets()
    st = D.DeliveryStore(api.lab.path)
    st.save_settings(enabled=True)                                  # enabled while Sep 25 was the latest stored session
    st.insert(D._row("SESSION", "BASELINE", "ENABLE", FL.at(S1), "2026-09-25"))
    r = D.deliver(FL.at(S2), "SCHEDULED", "o", path=api.lab.path)    # every market / AI / broker path fails fast
    assert r["result"] == "DELIVERED" and r["brief_session"] == "2026-09-28" and len(fake.calls) == 1
    assert api.hits == [] and api.budgets() == b0


# ---- 59: Stage 4.2 parity; migration; immutability; language; security --------------------------------------------------------------

def test_59_the_brief_is_unchanged_by_delivery(fake):
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    enable(lab, S1)
    store(lab, ss, jid, S2)
    before = stable(DB.build("2026-09-28", path=lab.path))
    b = DB.build("2026-09-28", path=lab.path)
    snapshot = copy.deepcopy(b)
    D.compose(b)
    assert b == snapshot                                                     # the formatter never mutates the brief
    deliver(lab, S2)
    assert stable(DB.build("2026-09-28", path=lab.path)) == before


def test_migration_is_additive_idempotent_and_rows_are_immutable(fake):
    lab, _, jid, ss = lab_at()
    store(lab, ss, jid, S1)
    A.AutomationStore(Path(lab.path)).save(enabled=False)                   # Stage 3.7 settings + lease tables exist
    from database.delivery_migrations import run_delivery_migrations
    with sqlite3.connect(lab.path) as c:
        uv = c.execute("PRAGMA user_version").fetchone()[0]
        tables0 = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        schema0 = sorted(c.execute("SELECT type, name, sql FROM sqlite_master"))
        old = {t: c.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables0}
        for _ in range(3):
            run_delivery_migrations(c)
        tables1 = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert tables1 - tables0 == {"daily_brief_delivery_settings", "daily_brief_deliveries"}
        assert c.execute("PRAGMA user_version").fetchone()[0] == uv
        assert [x for x in sorted(c.execute("SELECT type, name, sql FROM sqlite_master")) if x[1] in {y[1] for y in schema0}] == schema0
        assert {t: c.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in tables0} == old
    enable(lab, S1)
    store(lab, ss, jid, S2)
    deliver(lab, S2)
    with sqlite3.connect(lab.path) as c:
        for bad in ("UPDATE daily_brief_deliveries SET status = 'FAILED'", "UPDATE daily_brief_deliveries SET body = 'x'",
                    "DELETE FROM daily_brief_deliveries", "DELETE FROM daily_brief_delivery_settings",
                    "INSERT INTO daily_brief_delivery_settings VALUES ('smtp_password', 'true', 'x')",
                    "INSERT INTO daily_brief_deliveries (delivery_id, kind, brief_session, channel, status, trigger, created_at) "
                    "VALUES ('" + "a" * 32 + "', 'SESSION', '2026-09-28', 'WINDOWS_DESKTOP', 'SKIPPED_NO_ACTIVITY', 'SCHEDULED', 'x')",
                    "INSERT INTO daily_brief_deliveries (delivery_id, kind, brief_session, channel, status, trigger, created_at) "
                    "VALUES ('" + "b" * 32 + "', 'SESSION', '2026-10-01', 'EMAIL', 'SKIPPED_NO_ACTIVITY', 'SCHEDULED', 'x')"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(bad)
        row = c.execute("SELECT * FROM daily_brief_deliveries WHERE status = 'DELIVERED'").fetchone()
        cols = [r[1] for r in c.execute("PRAGMA table_info(daily_brief_deliveries)")]
        with pytest.raises(sqlite3.DatabaseError, match="at most once"):
            c.execute(f"INSERT OR REPLACE INTO daily_brief_deliveries ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", row)


def test_pending_becomes_final_exactly_once():
    lab, _, _, _ = lab_at(journal=False)
    s = D.DeliveryStore(lab.path)
    r = D._row("SESSION", "PENDING", "SCHEDULED", FL.at(S1), "2026-09-25", {"title": "t", "body": "b", "fingerprint": "f" * 64})
    s.insert(r)
    with sqlite3.connect(lab.path) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("UPDATE daily_brief_deliveries SET status = 'SKIPPED_NO_ACTIVITY'")
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("UPDATE daily_brief_deliveries SET status = 'FAILED', body = 'changed'")
    s.finalize(r["delivery_id"], "FAILED", "OSError")
    s.finalize(r["delivery_id"], "DELIVERED", None)                          # no second transition (WHERE status = PENDING)
    (h,) = s.history()
    assert (h["status"], h["error_code"], h["delivered_at"]) == ("FAILED", "OSError", None)


def test_notification_language_and_windows_limits():
    texts = [D.TITLE, D.FOOTER, D.TEST_TITLE, D.TEST_BODY, D.NOTE, D.SERVER_NOTE, D.NEXT_TEXT]
    b = _brief([{"label": "Up v1", "newly_rules_met": [{"symbol": "AMD"}], "no_longer_rules_met": [{"symbol": "MU", "status": "RULES_NOT_MET"}]}],
               rc=1, fc=3, cc=1, di=2)
    texts += [D.compose(b)["body"]]
    for t in texts:
        assert not BANNED.findall(t), (t, BANNED.findall(t))
    assert len(D.TITLE) <= W.TITLE_MAX and len(D.TEST_TITLE) <= W.TITLE_MAX and len(D.TEST_BODY) <= W.BODY_MAX
    js = (ROOT / "frontend" / "brief_delivery.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert not re.search(r"(?i)trade alert|buy alert|signal notification|opportunity alert|\bbuy\b|\bsell\b", code)
    for s in ("Desktop notifications", "Daily Brief delivered", "No new stored activity", "Test notification", "Send test notification"):
        assert s.lower() in js.lower(), s
    assert "setInterval" not in js and "setTimeout" not in js and "title:" not in js and "body: JSON.stringify(body" in js


def test_security_no_process_no_network_no_trading_no_ai():
    files = ["notifications/windows.py", "notifications/delivery.py", "api/routes/daily_brief_delivery.py",
             "database/delivery_migrations.py", "frontend/brief_delivery.js"]
    for f in files:
        src = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|ShellExecute|"
                             r"CreateProcess|WinExec|powershell|new Function|pickle|importlib", src, re.I), f
        assert not re.search(r"TradingClient|alpaca|place_?order|submit_?order|/orders|anthropic|get_provider|from agents|"
                             r"import agents|from portfolio|import portfolio|rh_gateway|robinhood|smtp|twilio|slack|discord|"
                             r"webhook|requests\.|urllib|http\.client|socket|setInterval|Timer\(|apscheduler",
                             src, re.I), f
        # Stage 4.4: exactly ONE thread may exist — the desktop adapter's message loop (windows.py); nothing else starts one
        assert src.count("threading.Thread(") == (1 if f == "notifications/windows.py" else 0), f
    win = (ROOT / "notifications" / "windows.py").read_text(encoding="utf-8")
    assert "ctypes" in win and "Shell_NotifyIconW" in win and "import ctypes" in win
    assert "fetch_daily_bars" not in (ROOT / "notifications" / "delivery.py").read_text(encoding="utf-8")
    dl = (ROOT / "notifications" / "delivery.py").read_text(encoding="utf-8")
    assert "DB.build(" in dl and "SC.scan(" not in dl and "evaluate_version" not in dl and 'phase = "after_cycle"' in dl


@pytest.mark.skipif(sys.platform != "win32" or sys.maxsize <= 2 ** 32, reason="Windows 64-bit structure layout")
def test_windows_adapter_structure_matches_the_win32_layout():
    import ctypes
    api = W._load()                                                         # builds bindings only: no window, no icon
    assert ctypes.sizeof(api["NID"]) == 976 and W._icons == []


def test_scheduler_is_unchanged_when_delivery_is_off_and_runs_for_delivery_alone(exts, fake):
    lab, _, jid, ss = lab_at(journal=True)
    exts["lab"] = lab
    store(lab, ss, jid, S1)
    SS.settings(ss, alerts_enabled=False, path=lab.path)
    sch = scheduler(lab, S2)
    assert A.extensions_wanted() is False and sch.step() is None             # nothing opted in: no check at all
    enable(lab, S1)
    assert A.extensions_wanted() is True                                     # delivery alone keeps the scheduler running
    lab.record(jid, FL.at(S2))                                               # a new stored session (manual capture)
    s = scheduler(lab, S2).step()
    assert s["result"] == "FORWARD_CAPTURE_OFF" and list(s["extensions"]) == ["daily_brief_delivery"]
    assert s["extensions"]["daily_brief_delivery"]["result"] == "DELIVERED" and len(fake.calls) == 1
