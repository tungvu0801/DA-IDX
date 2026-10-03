"""Stage 4.2 — the read-only Daily Brief (brief/daily.py): stored saved-scan alerts, forward captures, completed reference
cycles, evidence status and data / continuity issues of one session. Stored data only: no market data, no AI, no broker,
no write, no read-state change, no new financial logic."""
import hashlib
import json
import re
import socket
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fw_fixtures as FL
from brief import daily as DB
from comparison import view as V
from fit import readonly as RO
from fit import saved_scans as SS
from fit import scanner as SC
from forward import excursions as XC
from forward import journal as J
from test_evidence_35 import fspec, rich  # noqa: F401  (module fixture: two stored runs + journals)
from test_excursions_36 import LegacyLab, _two_cycle_market, run
from test_fit_34 import G
from test_saved_scans_41 import S1, S2, S3, S4, UP, check, save, world

ROOT = Path(__file__).resolve().parents[1]
D = date.fromisoformat
EVT = {"feature": "event.risk_level", "op": "!=", "value": "HIGH"}
FL_QUIET = {"vol": 0.002}
BANNED = re.compile(r"(?i)\b(recommend\w*|buy|sell|best|top|top pick|opportunit\w*|strongest|weakest|confidence|probability|"
                    r"avoid|entry now|likely|expected return|score)\b")


def system(moves, entry=None, journal=True, name="Up"):
    """A saved strategy with a saved scan (alerts on) and, optionally, its forward journal (created before S1)."""
    lab = world(moves, entry=entry, name=name)
    sid = next(v["strategy_id"] for v in RO.saved_versions(Path(lab.path)) if v["strategy_name"] == name)
    jid = J.create_journal(lab.fs, lab.store, sid, 1, now=FL.ny(D("2026-09-24"), 12))["journal_id"] if journal else None
    return lab, sid, jid, save(lab)


def brief(lab, session=None):
    return DB.build(None if session is None else str(session), path=lab.path)


def digest(path):
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    h = hashlib.sha256()
    for name, sql in c.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall():
        h.update(f"{name}|{sql}".encode())
        if sql and sql.upper().startswith("CREATE TABLE"):
            for row in c.execute(f'SELECT * FROM "{name}" ORDER BY 1'):
                h.update(repr(row).encode())
    c.close()
    return h.hexdigest(), hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stable(b):
    return {k: v for k, v in b.items() if k not in ("generated_at", "timings")}


# ---- 49: empty ---------------------------------------------------------------------------------------------------------------

def test_49_empty_database_gives_a_valid_empty_brief_and_creates_nothing(tmp_path):
    plain = tmp_path / "plain.db"
    sqlite3.connect(str(plain)).close()
    before = digest(plain)
    b = DB.build(path=plain)
    assert b["brief_session"] is None and b["empty"] is True and b["changes"] == b["forward_activity"] == []
    assert b["summary_text"][0].startswith("No stored strategy activity yet") and b["data_issues"] == []
    assert b["navigation"] == {"previous": None, "next": None, "latest": None, "count": 0, "recent": []}
    e = DB.build("2026-09-30", path=plain)
    assert e["brief_session"] == "2026-09-30" and e["summary_text"] == [DB.EMPTY_TEXT] and e["empty_text"] == DB.EMPTY_TEXT
    assert digest(plain) == before                                          # no table, no migration
    missing = tmp_path / "never.db"
    assert DB.build(path=missing)["brief_session"] is None and not missing.exists()



# ---- 50 / 51: stored saved-scan alerts, exactly; a baseline is never a change ---------------------------------------------------

def test_50_alert_semantics_are_the_stored_stage_41_events():
    lab, _, _, ss = system({S1: {"MU": 3}, S2: {"AMD": 3, "MU": 3}}, entry=G("ALL", UP, EVT), journal=False)
    lab.events.earnings = True
    check(lab, ss, S1)
    lab.events.raise_for = {"MU"}
    check(lab, ss, S2)
    b = brief(lab)
    assert b["brief_session"] == "2026-09-28"
    (c,) = b["changes"]
    stored = SS.SavedScanStore(lab.path).events(ss)[0]
    assert c["texts"] == SS.alert_texts(stored, "Up v1") == [
        "AMD newly meets the saved entry rules for Up v1 (RULES MET).",
        "MU is no longer in RULES MET for Up v1; the latest scan has incomplete data, so the rule result could not be decided."]
    assert c["newly_rules_met"] == stored["newly_rules_met"] and c["no_longer_rules_met"][0]["status"] == "INCOMPLETE_DATA"
    assert c["alert_fingerprint"] == stored["alert_fingerprint"] and c["unread"] is True
    n = b["summary_counts"]
    assert (n["saved_scans_checked"], n["rule_change_events"], n["newly_rules_met_symbols"], n["no_longer_rules_met_symbols"]) == (1, 1, 1, 1)
    assert b["headline"][0] == "1 rule-state change"
    assert any(i["code"] == "SCAN_INCOMPLETE_DATA" and i["symbols"] == ["MU"] and i["level"] == "warn" for i in b["data_issues"])


def test_51_a_baseline_is_never_reported_as_a_change():
    lab, _, _, ss = system({S1: {"MU": 3, "AMD": 3}}, journal=False)
    check(lab, ss, S1)
    b = brief(lab)
    assert b["changes"] == [] and b["summary_counts"]["rule_change_events"] == 0
    (x,) = b["saved_scans_checked"]
    assert x["result"] == "BASELINE" and x["rules_met"] == ["AMD", "MU"] and x["is_baseline"] is True
    assert "newly" not in json.dumps(b["summary_text"]) and "baseline" in b["summary_text"][0]


# ---- 52 / 53: forward captures, MISSED, GAPPED, CONTINUITY_BLOCKED ------------------------------------------------------------

def test_52_forward_capture_rows_are_the_stored_observations():
    lab, sid, jid, ss = system({S1: {"MU": 5}, S2: {"AMD": 5}})
    for s in (S1, S2):
        check(lab, ss, s)
        lab.record(jid, FL.at(s))
    b = brief(lab, S2)
    (j,) = b["forward_activity"]
    assert (j["label"], j["capture"], j["journal_status"]) == ("Up v1", "CAPTURED", "ACTIVE")
    rows = {o["symbol"]: o for o in j["observations"]}
    stored = {o["symbol"]: o for o in lab.fs.observations(jid, "2026-09-28", compact=True)}
    for s, o in rows.items():
        assert (o["decision"], o["state_before"], o["state_after"], o["reason_code"]) == \
            (stored[s]["decision"], stored[s]["state_before"], stored[s]["state_after"], stored[s]["reason_code"])
    assert (rows["AMD"]["decision"], rows["AMD"]["state_after"]) == ("ENTER", "ENTRY_PENDING")
    assert rows["AMD"]["reference_fill_pending"] == "Reference entry: pending the next session's open."
    assert (rows["MU"]["decision"], rows["MU"]["state_before"], rows["MU"]["state_after"]) == ("HOLD", "ENTRY_PENDING", "OPEN")
    (f,) = rows["MU"]["fills_resolved"]
    fill = next(x for x in lab.fs.fills(jid) if x["symbol"] == "MU" and x["fill_type"] == "ENTRY")
    assert (f["status"], f["reference_open_price"], f["fill_session_date"]) == ("FILLED", fill["reference_open_price"], "2026-09-28")
    assert j["decision_counts"] == {"ENTER": 1, "EXIT": 0, "HOLD": 2, "SKIP": 0} and b["summary_counts"]["forward_captures"] == 1
    assert [o["symbol"] for o in j["observations"]] == sorted(rows)


def test_53_missed_gapped_and_continuity_blocked_are_visible_where_stored():
    lab, sid, jid, ss = system({S1: {"MU": 5}, S2: {"AMD": 5}})
    for s in (S1, S2, S4):                                                   # S3 (Sep 29) is never captured
        check(lab, ss, s)
        lab.record(jid, FL.at(s))
    missed = brief(lab, S3)
    (j,) = missed["forward_activity"]
    assert j["capture"] == "MISSED" and j["detected_with_session"] == "2026-09-30" and j["observations"] == []
    assert any(i["code"] == "SESSION_MISSED" and i["level"] == "alert" for i in missed["data_issues"])
    assert missed["summary_counts"]["forward_sessions_missed"] == 1
    after = brief(lab, S4)
    (j,) = after["forward_activity"]
    assert j["missed_recorded"] == ["2026-09-29"] and after["summary_counts"]["missed_sessions_recorded"] == 1
    codes = {i["code"] for i in after["data_issues"]}
    assert {"MISSED_FORWARD_SESSION", "FORWARD_CONTINUITY_GAP", "JOURNAL_CONTINUITY_BLOCKED"} <= codes
    stored = {w["code"]: w["text"] for w in lab.fs.session(jid, "2026-09-30")["warnings"]}
    assert all(i["text"] == f"Up v1: {stored[i['code']]}" for i in after["data_issues"] if i["code"] in stored)   # verbatim
    assert {o["state_after"] for o in j["observations"] if o["symbol"] in ("AMD", "MU")} == {"CONTINUITY_BLOCKED"}
    earlier = brief(lab, S2)                        # the journal's later state never leaks into an older session's issues
    assert not any(i["code"].startswith("JOURNAL_") for i in earlier["data_issues"])


# ---- 54 / 55: completed cycles — stored values, MFE / MAE as stored, legacy wording kept ----------------------------------------

def test_54_completed_cycle_values_are_the_stored_evidence_values():
    from test_evidence_35 import _one_cycle_market
    lab = FL.FLab(_one_cycle_market())
    jid = lab.journal(fspec(symbols=("AMD",), name="One"), FL.ny(D("2026-09-25"), 12))["journal_id"]
    for d in ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"):
        lab.record(jid, FL.at(D(d)))
    b = brief(lab, "2026-10-01")
    (c,) = b["completed_cycles"]
    vid = lab.fs.journal(jid)["strategy_version_id"]
    (ev,) = V.view(vid, path=lab.path)["forward"]["cycles"]
    for k in ("entry_signal_session", "reference_entry_session", "reference_entry_open", "exit_signal_session",
              "reference_exit_session", "reference_exit_open", "reference_move_pct", "holding_sessions", "exit_reasons"):
        assert c[k] == ev[k], k
    assert (c["reference_entry_open"], c["reference_exit_open"], c["reference_move_pct"]) == (100.0, 105.0, (105.0 / 100.0 - 1) * 100)
    assert c["excursion_status"] == "COMPLETE" and (c["mfe_pct"], c["mae_pct"]) == (ev["excursion"]["mfe_pct"], ev["excursion"]["mae_pct"])
    assert b["summary_counts"]["completed_reference_cycles"] == 1 and brief(lab, "2026-09-30")["completed_cycles"] == []


def test_55_legacy_cycles_keep_their_wording_and_no_mfe_is_inferred():
    lab = LegacyLab(_two_cycle_market())
    jid = run(lab, ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"))
    only_legacy = brief(lab, "2026-10-01")["completed_cycles"][0]           # a journal from before Stage 3.6 (no tracking)
    assert (only_legacy["excursion_status"], only_legacy["excursion_text"]) == ("NOT_TRACKED_STAGE_3_3", "Not tracked in Stage 3.3")
    lab.upgrade()
    for d in ("2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"):
        lab.record(jid, FL.at(D(d)))
    legacy = brief(lab, "2026-10-01")["completed_cycles"][0]
    assert legacy["excursion_status"] == XC.LEGACY and legacy["excursion_text"] == XC.STATUS_TEXT[XC.LEGACY]
    assert legacy["mfe_pct"] is None and legacy["mae_pct"] is None
    tracked = brief(lab, "2026-10-08")["completed_cycles"][0]
    assert tracked["excursion_status"] == "COMPLETE" and tracked["mfe_pct"] == pytest.approx(7.5) and tracked["mae_pct"] == pytest.approx(-2.0)
    ev = brief(lab, "2026-10-08")["evidence_status"][0]["forward"]
    assert ev["mfe_mae_text"] == "MFE / MAE sample: 1 tracked cycle of 2 completed cycles"


# ---- 56 / 57: evidence labels exactly as stored; independent strategies, no ranking ---------------------------------------------

def test_56_evidence_status_is_the_evidence_view_labels(rich):
    b = brief(rich, "2026-10-02")
    by = {e["strategy_version_id"]: e for e in b["evidence_status"]}
    e = by[rich.ids["v1"]]
    v = V.view(rich.ids["v1"], None, rich.ids["j1"], path=rich.path)
    h, f = v["historical"], v["forward"]
    assert (e["historical"]["closed_trades"], e["historical"]["sample_code"], e["historical"]["sample_text"]) == \
        (h["metrics"]["closed_trades"], h["sample"]["code"], h["sample"]["text"])
    assert (e["forward"]["completed_cycles"], e["forward"]["sample_code"], e["forward"]["continuity"], e["forward"]["journal_status"]) == \
        (f["completed_cycles"], f["sample"]["code"], f["continuity"], f["journal_status"])
    assert e["historical"]["sample_code"] in ("NO_TRADES", "VERY_SMALL_SAMPLE", "SMALL_SAMPLE", "SAMPLE_SIZE")
    text = json.dumps(b["summary_text"] + b["headline"] + [DB.EVIDENCE_NOTE, DB.NOTE])
    assert not re.search(r"(?i)works|profitable|reliable|proven|confiden", text)


def test_57_multiple_strategies_are_independent_and_alphabetical():
    lab = FL.FLab(FL.Market(("AMD", "MU", "SPY"), **FL_QUIET))
    for name in ("Zeta", "Alpha", "Mid"):
        jid = lab.journal(fspec(symbols=("AMD", "MU"), name=name), FL.ny(D("2026-09-24"), 12))["journal_id"]
        lab.record(jid, FL.at(S1))
    b = brief(lab, S1)
    assert [j["strategy_name"] for j in b["forward_activity"]] == ["Alpha", "Mid", "Zeta"]
    assert [e["strategy_name"] for e in b["evidence_status"]] == ["Alpha", "Mid", "Zeta"]
    assert b["summary_counts"]["forward_captures"] == 3 and b["summary_counts"]["strategies_represented"] == 3
    keys = json.dumps(b)
    assert not re.search(r'"(score|rank|ranking|priority|importance)[a-z_]*"\s*:', keys)


# ---- 58 / 59 / 60: older sessions, read state, no writes ---------------------------------------------------------------------------

def test_58_older_session_shows_only_that_sessions_stored_state(monkeypatch):
    lab, sid, jid, ss = system({S1: {"MU": 5}, S2: {"AMD": 5}})
    for s in (S1, S2):
        check(lab, ss, s)
        lab.record(jid, FL.at(s))
    monkeypatch.setattr(SC, "scan", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no scan from the brief")))
    old = brief(lab, S1)
    assert old["brief_session"] == "2026-09-25" and old["navigation"] == {**old["navigation"], "previous": None, "next": "2026-09-28",
                                                                         "latest": "2026-09-28"}
    assert any(w["code"] == "OLDER_SESSION" for w in old["warnings"])
    (x,) = old["saved_scans_checked"]
    assert x["result"] == "BASELINE" and x["rules_met"] == ["MU"]           # the stored S1 snapshot, not a current scan
    (j,) = old["forward_activity"]
    assert {o["symbol"]: o["decision"] for o in j["observations"]} == {"AMD": "HOLD", "CLS": "HOLD", "MU": "ENTER"}
    assert old["changes"] == [] and old["completed_cycles"] == []
    gap = brief(lab, "2026-09-27")                                           # a date nothing was stored for (Sunday)
    assert gap["empty"] and gap["summary_text"] == [DB.EMPTY_TEXT]
    assert (gap["navigation"]["previous"], gap["navigation"]["next"], gap["navigation"]["stored"]) == ("2026-09-25", "2026-09-28", False)


def test_59_opening_the_brief_never_marks_alerts_read():
    lab, _, _, ss = system({S1: {"MU": 3}, S2: {"AMD": 3}}, journal=False)
    check(lab, ss, S1)
    check(lab, ss, S2)
    store = SS.SavedScanStore(lab.path)
    before = [(e["alert_id"], e["read_at"]) for e in store.events()]
    for _ in range(3):
        brief(lab)
        brief(lab, S1)
    assert [(e["alert_id"], e["read_at"]) for e in store.events()] == before and store.unread() == 1


def test_60_no_write_byte_for_byte():
    lab, sid, jid, ss = system({S1: {"MU": 5}, S2: {"AMD": 5}})
    for s in (S1, S2, S4):
        check(lab, ss, s)
        lab.record(jid, FL.at(s))
    before = digest(lab.path)
    for s in (None, S1, S2, S3, S4, "2026-09-27", None):
        brief(lab, s)
    assert digest(lab.path) == before                                        # table contents AND file bytes


# ---- 48 / 61 / 62 / 63: no external call, no AI, no broker, no market data ------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from agents.usage_tracker import EXPLANATION, RESEARCH, usage_tracker
    from ai_explain import service as AX
    from api.routes import portfolio as pr
    from api.server import app
    hits = []

    def fail(what):
        def _f(*a, **k):
            hits.append(what)
            raise RuntimeError(f"{what} must not be called by the Daily Brief")
        return _f
    lab, sid, jid, ss = system({S1: {"MU": 5}, S2: {"AMD": 5}})          # stored first, through the real Stage 3.3 / 4.1 paths
    for s in (S1, S2):
        check(lab, ss, s)
        lab.record(jid, FL.at(s))
    monkeypatch.setattr(ta, "get_provider", fail("claude (research)"))
    monkeypatch.setattr(AX, "get_provider", fail("claude (explanation)"))
    monkeypatch.setattr(pr, "provider_factory", fail("broker gateway"))
    monkeypatch.setattr("data.market_data.fetch_daily_bars", fail("market data"))
    monkeypatch.setattr("data.market_data.get_data_client", fail("market data client"))
    monkeypatch.setattr(SC, "scan", fail("scanner"))
    monkeypatch.setattr(socket, "create_connection", fail("network"))
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    c = TestClient(app)
    c.lab, c.hits, c.budgets = lab, hits, lambda: (usage_tracker.budget(RESEARCH)["used_today"], usage_tracker.budget(EXPLANATION)["used_today"],
                                                    len(usage_tracker._records))
    c.sid, c.jid, c.ss = sid, jid, ss
    return c


def test_48_61_62_63_api_works_with_every_external_dependency_failing(api):
    b0 = api.budgets()
    before = digest(api.lab.path)
    r = api.get("/api/daily-brief")
    assert r.status_code == 200 and r.json()["brief_session"] == "2026-09-28" and r.json()["changes"]
    for s in ("2026-09-25", "2026-09-26", "2026-09-28"):
        assert api.get(f"/api/daily-brief?session={s}").status_code == 200
    assert api.hits == [] and api.budgets() == b0 and digest(api.lab.path) == before



def test_api_is_get_only_and_strict(api):
    assert api.get("/api/daily-brief?session=2026-13-01").json()["status"] == "INVALID_SESSION"
    assert api.get("/api/daily-brief?session=yesterday").status_code == 422
    assert api.get("/api/daily-brief?symbol=AMD").json()["status"] == "UNKNOWN_PARAMETER"
    assert api.get("/api/daily-brief?session=2026-09-28&refresh=1").status_code == 422
    assert api.post("/api/daily-brief", json={}).status_code == 405
    from api.server import app
    assert {p: sorted(o) for p, o in app.openapi()["paths"].items() if "daily-brief" in p} == {"/api/daily-brief": ["get"]}


# ---- determinism, navigation, wording, security ----------------------------------------------------------------------------------

def test_the_brief_is_deterministic_and_navigates_stored_sessions_only():
    lab, sid, jid, ss = system({S1: {"MU": 5}, S2: {"AMD": 5}})
    for s in (S1, S2, S4):
        check(lab, ss, s)
        lab.record(jid, FL.at(s))
    assert stable(brief(lab, S2)) == stable(brief(lab, S2))
    latest = brief(lab)
    assert latest["brief_session"] == "2026-09-30" and latest["navigation"]["recent"] == ["2026-09-30", "2026-09-29", "2026-09-28", "2026-09-25"]
    seen, s = [], latest["brief_session"]
    while s:
        seen.append(s)
        s = brief(lab, s)["navigation"]["previous"]
    assert seen == ["2026-09-30", "2026-09-29", "2026-09-28", "2026-09-25"]   # the MISSED session is a stored session too
    assert latest["source_freshness"]["latest_forward_capture_session"] == "2026-09-30" and latest["stored_label"] == "Stored after the 2026-09-30 close"


def test_64_wording_templates_and_ui_text_have_no_advice_language():
    lab, sid, jid, ss = system({S1: {"MU": 5}, S2: {"AMD": 5}})
    for s in (S1, S2, S4):
        check(lab, ss, s)
        lab.record(jid, FL.at(s))
    for s in (S1, S2, S3, S4, "2026-09-27"):
        b = brief(lab, s)
        generated = b["summary_text"] + b["headline"] + [i["text"] for i in b["data_issues"] + b["notes"] if i["kind"] != "FORWARD"]
        assert not BANNED.findall(" ".join(generated)), BANNED.findall(" ".join(generated))
    for text in (DB.NOTE, DB.EMPTY_TEXT, DB.EVIDENCE_NOTE, DB.LINK_NOTE, *DB.PENDING_TEXT.values(), *DB.UNDECIDED_TEXT.values()):
        assert not BANNED.findall(text), text
    js = (ROOT / "frontend" / "daily_brief.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert not BANNED.findall(code), BANNED.findall(code)
    assert "setInterval" not in js and "setTimeout" not in js and js.count("fetch(") == 1
    assert "new AbortController()" in js and "my !== seq" in js and 'addWorkspace({ id: "brief", label: "Daily Brief"' in js
    for s in ("Stored data only · No AI · No new market-data calls · No orders", "Refresh brief", "WHAT CHANGED", "FORWARD JOURNALS",
              "EVIDENCE STATUS", "DATA / CONTINUITY", "Previous session", "Next session"):
        assert s in js, s


def test_security_read_only_sources_no_scheduler_no_ai_no_broker():
    py = (ROOT / "brief" / "daily.py").read_text(encoding="utf-8") + (ROOT / "api" / "routes" / "daily_brief.py").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "daily_brief.js").read_text(encoding="utf-8")
    for src in (py, js):
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|new Function|pickle|importlib", src)
        assert not re.search(r"TradingClient|alpaca|place_?order|submit_?order|/orders|anthropic|get_provider|from agents|"
                             r"import agents|from portfolio|import portfolio|rh_gateway|robinhood|setInterval|threading|"
                             r"apscheduler|forward\.automation|forward import automation|Scheduler|webhook|smtp", src, re.I)
    assert not re.search(r"(?i)\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\b\s+(INTO|TABLE|FROM|INDEX|TRIGGER|\w+\s+SET)", py)
    for store in ("ForwardStore(", "BacktestStore(", "SavedScanStore(", "AutomationStore(", "ResearchDatabase(", "sqlite3.connect("):
        assert store not in py, store                                       # only RO.connect / RO read-only stores
    assert "RO.connect" in py and "V.view(" in py and "SS.alert_texts(" in py
    assert "fetch_daily_bars" not in py and "SC.scan(" not in py and "evaluate_version" not in py
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert '<div id="dbf-body"></div>' in html and html.index("saved_scans.js") < html.index("daily_brief.js") < html.index("ai_history.js")


def test_evidence_summary_cache_is_exact_and_invalidated_by_any_new_stored_evidence():
    lab, sid, jid, ss = system({S1: {"MU": 5}, S2: {"AMD": 5}})
    for s in (S1, S2):
        check(lab, ss, s)
        lab.record(jid, FL.at(s))
    first = brief(lab, S2)
    again = brief(lab, S2)
    assert first["timings"]["evidence_cache_hits"] == 0 and again["timings"]["evidence_cache_hits"] == again["timings"]["evidence_views"] == 1
    DB._EVIDENCE_CACHE.clear()
    assert stable(again) == stable(first) == stable(brief(lab, S2))          # cached == freshly built
    lab.record(jid, FL.at(S4))                                               # a new capture (+ a MISSED session) changes the stamp
    after = brief(lab, S2)
    assert after["timings"]["evidence_cache_hits"] == 0
    assert after["evidence_status"][0]["forward"]["captured_sessions"] == first["evidence_status"][0]["forward"]["captured_sessions"] + 1
    assert brief(lab, S2)["timings"]["evidence_cache_hits"] == 1
    from strategy.store import StrategyStore
    StrategyStore(lab.path).add_version(sid, fspec(symbols=("AMD", "MU", "CLS"), name="Up"))   # v1 is no longer current
    assert brief(lab, S2)["timings"]["evidence_cache_hits"] == 0
    J.archive_journal(lab.fs, jid, now=FL.at(S4))
    archived = brief(lab, S2)
    assert archived["timings"]["evidence_cache_hits"] == 0 and archived["evidence_status"][0]["forward"]["journal_status"] == "ARCHIVED"
