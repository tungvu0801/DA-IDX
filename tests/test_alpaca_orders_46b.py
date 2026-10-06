"""Stage 4.6B — Alpaca PAPER manual orders (DESIGN_46B_FINAL). Fakes only: tests/alpaca_order_fakes.FakeAlpaca sits BELOW
the real 4.6B transports (read adapter + writer); the market-data layer uses the synthetic market; sockets are blocked and
the real wires are tripwired (conftest). Fake credentials TEST_KEY_123 / TEST_SECRET_456."""
import ast
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

import fw_fixtures as FL
import test_paper_45 as T45
from alpaca_order_fakes import ACCOUNT_ID, OTHER_ACCOUNT_ID, FakeAlpaca
from fit import current as FC
from fit import readonly as RO
from paper import alpaca_order_reads as RD
from paper import alpaca_order_rules as RU
from paper import alpaca_order_store as S
from paper import alpaca_order_writer as W
from paper import alpaca_orders as O

ROOT = Path(__file__).resolve().parents[1]
Dt = date.fromisoformat
KEY, SECRET = "TEST_KEY_123", "TEST_SECRET_456"
HDR = {"X-Stock-Agent-Intent": "paper-order"}
NEW_PRODUCT = ["paper/alpaca_order_rules.py", "paper/alpaca_order_store.py", "paper/alpaca_order_reads.py",
               "paper/alpaca_order_writer.py", "paper/alpaca_orders.py", "database/alpaca_order_migrations.py",
               "api/routes/alpaca_paper_orders.py", "frontend/alpaca_orders.js"]
TABLES = ("alpaca_paper_order_settings", "alpaca_paper_order_intents", "alpaca_paper_order_events")


# ---- fixtures ------------------------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    O._reset()
    sleeps = []
    monkeypatch.setattr(O, "_sleep", sleeps.append)
    yield sleeps
    O._reset()


@pytest.fixture
def clock(monkeypatch):
    clk = [datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)]          # Tue 10:00 New York
    monkeypatch.setattr(O, "_now", lambda: clk[0])
    return clk


@pytest.fixture
def lab(monkeypatch):
    """A local DB (Stage 4.5 simulator with AMD 10) + synthetic market: reference close of AMD on Sep 28 = 101."""
    m, lb = T45.market({"2026-09-28": {"AMD": (100, 101), "CLS": (50, 52), "MU": (20, 20), "KO": (60, 61)}},
                       syms=("AMD", "MU", "CLS", "KO"))
    T45.account(lb, cash="100000")
    T45.order(lb, "2026-09-25", "AMD", "BUY", 10)
    T45.process(lb, "2026-09-28")
    monkeypatch.setattr(RO, "db_path", lambda: Path(lb.path))
    calls = []

    def fetch(client, symbols, lookback_days):
        calls.append(tuple(symbols))
        return m.fetch(client, symbols, lookback_days)
    monkeypatch.setattr("data.market_data.fetch_daily_bars", fetch)
    monkeypatch.setattr("data.market_data.get_data_client", lambda: object())
    monkeypatch.setattr(FC, "_utc", lambda now=None: now or FL.at(Dt("2026-09-28")))
    monkeypatch.setattr(FC, "BAR_CACHE", FC.BarCache())
    lb.md_calls, lb.market = calls, m
    return lb


@pytest.fixture
def broker(monkeypatch, clock, lab):
    b = FakeAlpaca()
    b.now = clock[0]
    b.next_close = b.now + timedelta(hours=6)
    b.set_position("AMD", 10)
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", KEY)
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", SECRET)
    monkeypatch.setattr(RD, "WIRE", lambda: b)
    monkeypatch.setattr(W, "WIRE", lambda: b)
    b.lab = lab
    return b


def ready(b):
    O.link_account()
    O.enable()
    b.requests.clear()
    return b


def preview(sym="AMD", side="BUY", qty=10):
    return O.preview(sym, side, qty)["intent"]


def confirm(i):
    return O.confirm(i["intent_id"], i["preview_hash"])


def err(fn, *a):
    with pytest.raises(O.OrderError) as e:
        fn(*a)
    return e.value


def db_rows(path, table):
    with sqlite3.connect(str(path)) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


def tables_in(path):
    with sqlite3.connect(str(path)) as c:
        return {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def api(monkeypatch, broker):
    import agents.technical_agent as ta
    from agents.usage_tracker import EXPLANATION, RESEARCH, usage_tracker
    from ai_explain import service as AX
    from api.routes import portfolio as pr
    from api.server import app
    hits = []

    def fail(what):
        def _f(*a, **k):
            hits.append(what)
            raise RuntimeError(what)
        return _f
    monkeypatch.setattr(ta, "get_provider", fail("claude research"))
    monkeypatch.setattr(AX, "get_provider", fail("claude explanation"))
    monkeypatch.setattr(pr, "provider_factory", fail("robinhood gateway"))
    c = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    c.hits, c.broker = hits, broker
    c.budgets = lambda: (usage_tracker.budget(RESEARCH)["used_today"], usage_tracker.budget(EXPLANATION)["used_today"])
    c.p = lambda url, body=None, **k: c.post("/api/alpaca-paper-orders" + url, json={} if body is None else body,
                                             headers={**HDR, **k.pop("headers", {})}, **k)
    return c


# ==================================================================================================================================
# A · B — zero broker writes outside confirm; confirm = exactly one POST of the stored payload
# ==================================================================================================================================

def test_A_zero_writes_startup_page_load_settings_preview_list_status_and_4_6A(monkeypatch, api):
    import config
    from paper import alpaca_view as AV
    monkeypatch.setattr(config, "EVENT_WARMUP_ON_STARTUP", False)
    from api.server import app
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as c:   # real lifespan
        assert c.get("/").status_code == 200
        assert api.broker.requests == []                                                     # startup / page: 0 reads too
        for url in ("/api/alpaca-paper-orders/settings", "/api/alpaca-paper-orders"):
            assert c.get(url).status_code == 200
        assert api.broker.requests == []                                                     # opening the pane: 0 requests
    assert api.p("/settings/link-account", {"confirm": True}).status_code == 200
    assert api.p("/settings/enable", {"confirm": True}).status_code == 200
    r = api.p("/preview", {"symbol": "AMD", "side": "BUY", "quantity": 1})
    assert r.status_code == 200 and r.json()["intent"]["state"] == "PREVIEWED"
    assert api.get("/api/alpaca-paper-orders").status_code == 200 and api.p("/status").status_code == 200
    assert api.p("/settings/disable").status_code == 200
    monkeypatch.setattr(AV, "READER", lambda: None)
    assert api.post("/api/alpaca-paper/refresh").status_code == 200                           # 4.6A refresh
    assert api.broker.posts == [] and [m for m, _, _ in api.broker.requests if m != "GET"] == []
    assert api.hits == []


def test_B_confirm_is_exactly_one_post_of_the_stored_payload_after_the_write_ahead_commit(broker):
    ready(broker)
    i = preview("AMD", "BUY", 3)
    row = S.OrderStore().intent(i["intent_id"])
    assert [p for m, p, _ in broker.requests] == ["/v2/account", "/v2/account/configurations", "/v2/assets/AMD", "/v2/clock"]
    broker.requests.clear()                                                                 # preview: 4 GETs (BUY), 0 POST
    seen = []
    broker.on_post = lambda req: seen.append((S.OrderStore().intent(i["intent_id"])["state"], req.body, dict(req.headers)))
    out = confirm(i)
    assert len(broker.posts) == 1 and seen[0][0] == "SUBMISSION_PENDING"                     # committed BEFORE the POST
    assert seen[0][1] == row["payload_json"].encode() == broker.posts[0]
    assert json.loads(broker.posts[0]) == {"client_order_id": i["client_order_id"], "qty": "3", "side": "buy", "symbol": "AMD",
                                           "time_in_force": "day", "type": "market"}
    assert seen[0][2]["Content-Type"] == "application/json" and seen[0][2]["APCA-API-KEY-ID"] == KEY
    assert out["intent"]["state"] == "SUBMITTED" and out["intent"]["alpaca_order_id"]
    assert [p for m, p, _ in broker.requests] == ["/v2/account", "/v2/account/configurations", "/v2/clock", "/v2/orders"]


# ==================================================================================================================================
# C — outcome classes O1–O9
# ==================================================================================================================================

OUTCOMES = [
    # mode, expected state, expected code, lookups, delay
    ("accept", "SUBMITTED", None, 0, None),
    ("accept:new", "BROKER_ACCEPTED", None, 0, None),
    ("accept:filled", "FILLED", None, 0, None),
    ("garbage200_after_record", "SUBMITTED", None, 1, 2.0),
    ("garbage200", "RECONCILIATION_REQUIRED", "UNREADABLE_200", 1, 2.0),
    ("inconsistent200", "RECONCILIATION_REQUIRED", "INCONSISTENT_200", 1, 2.0),
    ("connect_timeout", "SUBMIT_NOT_SENT", "NOT_SENT", 0, None),
    ("dns", "SUBMIT_NOT_SENT", "NOT_SENT", 0, None),
    ('http:422:{"code": 40010001, "message": "client_order_id must be unique"}', "RECONCILIATION_REQUIRED", "DUPLICATE_NOT_FOUND", 1, None),
    ('http:422:{"code": 40010000, "message": "qty must be > 0"}', "BROKER_REJECTED", "INVALID_QUANTITY", 1, None),
    ('http:422:{"code": 42210000, "message": "asset AMD is not tradable"}', "BROKER_REJECTED", "ASSET_NOT_TRADABLE", 1, None),
    ('http:422:{"code": 40310000, "message": "potential wash trade detected. use complex orders"}', "BROKER_REJECTED", "WASH_TRADE", 1, None),
    ('http:422:{"code": 40310100, "message": "trade denied due to pattern day trading protection"}', "BROKER_REJECTED", "PDT_PROTECTION", 1, None),
    ('http:422:{"code": 40310000, "message": "account is not allowed to short"}', "BROKER_REJECTED", "SHORT_NOT_ALLOWED", 1, None),
    ('http:422:{"code": 40310000, "message": "insufficient qty available for order"}', "BROKER_REJECTED", "INSUFFICIENT_QTY", 1, None),
    ('http:422:{"code": 40010000, "message": "market is closed"}', "BROKER_REJECTED", "MARKET_CLOSED", 1, None),
    ('http:422:{"code": 40010000, "message": "something new"}', "RECONCILIATION_REQUIRED", "UNCERTAIN_HTTP_422", 1, 2.0),
    ("http:422:<html>", "RECONCILIATION_REQUIRED", "UNCERTAIN_HTTP_422", 1, 2.0),
    ('http:403:{"code": 40310000, "message": "insufficient buying power"}', "BROKER_REJECTED", "INSUFFICIENT_BUYING_POWER", 1, None),
    ('http:403:{"code": 40310000, "message": "account is restricted"}', "BROKER_REJECTED", "ACCOUNT_RESTRICTED", 1, None),
    ('http:403:{"code": 40310000, "message": "forbidden"}', "RECONCILIATION_REQUIRED", "UNCERTAIN_HTTP_403", 1, 2.0),
    ("http:429", "RECONCILIATION_REQUIRED", "UNCERTAIN_HTTP_429", 1, 2.0),
    ("http:500", "RECONCILIATION_REQUIRED", "UNCERTAIN_HTTP_500", 1, 2.0),
    ("http:503", "RECONCILIATION_REQUIRED", "UNCERTAIN_HTTP_503", 1, 2.0),
    ("http:400", "RECONCILIATION_REQUIRED", "UNCERTAIN_HTTP_400", 1, 2.0),
    ("http:401", "RECONCILIATION_REQUIRED", "UNCERTAIN_HTTP_401", 1, 2.0),
    ("http:404", "RECONCILIATION_REQUIRED", "UNCERTAIN_HTTP_404", 1, 2.0),
    ("http_after_record:502", "SUBMITTED", None, 1, 2.0),
    ("timeout_after_record", "SUBMITTED", None, 1, 2.0),
    ("timeout_before", "RECONCILIATION_REQUIRED", "UNCERTAIN_TIMEOUT", 1, 2.0),
    ("reset_after_record", "SUBMITTED", None, 1, 2.0),
]


@pytest.mark.parametrize("mode,state,code,lookups,delay", OUTCOMES, ids=[o[0][:40] for o in OUTCOMES])
def test_C_outcome_classification(broker, fresh, mode, state, code, lookups, delay):
    ready(broker)
    i = preview()
    broker.post_modes = [mode]
    it = confirm(i)["intent"]
    assert it["state"] == state, (mode, it)
    assert (it["error_code"] or None) == code or state not in ("RECONCILIATION_REQUIRED", "SUBMIT_NOT_SENT", "BROKER_REJECTED")
    if state == "BROKER_REJECTED":
        assert it["error_code"] == code and it["error_text"] == RU.CATEGORY_TEXT[code]
    lookup_calls = [p for m, p, _ in broker.requests if p.startswith("/v2/orders:")]
    assert len(lookup_calls) == lookups and len([m for m, *_ in broker.requests if m == "POST"]) == 1
    assert fresh == ([delay] if delay else [])                                              # 2 s only for the delayed classes


def later(clock, broker, seconds=31):
    clock[0] += timedelta(seconds=seconds)
    broker.now = clock[0]


def test_C_422_duplicate_found_consistent_links_and_404_is_never_rejected(broker, clock):
    ready(broker)
    i = preview()
    broker.post_modes, broker.lookup_lag = ["timeout_after_record"], 1                     # Alpaca HAS it; the lookup lags
    it = confirm(i)["intent"]
    assert it["state"] == "RECONCILIATION_REQUIRED" and it["error_code"] == "UNCERTAIN_TIMEOUT"
    assert S.OrderStore().intent(i["intent_id"])["lookups_not_found"] == 1
    later(clock, broker)
    broker.lookup_lag = 1                                                                   # the pre-retry lookup lags too
    it = O.retry(i["intent_id"], i["preview_hash"])["intent"]                              # POST → 422 duplicate → O4 → found
    assert it["state"] == "SUBMITTED" and it["alpaca_order_id"] == broker.order_by_coid(i["client_order_id"])["id"]
    assert len(broker.posts) == 2 and len(broker.orders) == 1                               # still ONE order at the broker
    k = preview("KO", "BUY", 1)
    broker.post_modes, broker.lookup_lag = ["timeout_after_record"], 1
    confirm(k)
    later(clock, broker)
    broker.lookup_lag = 2                                                                   # 422 duplicate, lookup still 404
    it = O.retry(k["intent_id"], k["preview_hash"])["intent"]
    assert it["state"] == "RECONCILIATION_REQUIRED" and it["error_code"] == "DUPLICATE_NOT_FOUND"   # never BROKER_REJECTED


def test_C_422_duplicate_found_inconsistent_is_mismatch_never_linked(broker, clock):
    ready(broker)
    i = preview()
    broker.post_modes, broker.lookup_lag = ["timeout_after_record"], 1
    confirm(i)
    broker.order_by_coid(i["client_order_id"])["qty"] = "999"                              # an order that does not match
    later(clock, broker)
    broker.lookup_lag = 1
    it = O.retry(i["intent_id"], i["preview_hash"])["intent"]
    assert it["state"] == "RECONCILIATION_REQUIRED" and it["error_code"] == "MISMATCH" and it["alpaca_order_id"] is None


def test_C_lookup_failure_leaves_reconciliation_required(broker):
    ready(broker)
    i = preview()
    broker.post_modes, broker.lookup_fail = ["http:500"], 1
    it = confirm(i)["intent"]
    assert it["state"] == "RECONCILIATION_REQUIRED" and it["error_code"] == "UNCERTAIN_HTTP_500"


def test_C_stale_states_after_a_crash(broker, clock):
    ready(broker)
    i = preview()
    st = S.OrderStore()
    with st.write() as c:                                                                   # a crash right after CONFIRMING
        st.transition(c, i["intent_id"], {"PREVIEWED"}, "CONFIRMING", clock[0], confirmed_at=clock[0].isoformat())
    j_id = i["intent_id"]
    clock[0] += timedelta(seconds=61)
    O.check_status()
    assert st.intent(j_id)["state"] == "CONFIRM_REJECTED" and st.intent(j_id)["error_code"] == "INTERRUPTED"
    k = preview("KO", "BUY", 1)
    with st.write() as c:                                                                   # a crash between POST and its answer
        st.transition(c, k["intent_id"], {"PREVIEWED"}, "CONFIRMING", clock[0], confirmed_at=clock[0].isoformat())
        st.transition(c, k["intent_id"], {"CONFIRMING"}, "SUBMISSION_PENDING", clock[0], submit_attempts=1,
                      last_submit_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=46)
    O._LAST_STATUS[0] = None
    O.check_status()
    assert st.intent(k["intent_id"])["state"] == "RECONCILIATION_REQUIRED"
    assert st.intent(k["intent_id"])["error_code"] in ("INTERRUPTED_SUBMISSION", "UNCERTAIN_TIMEOUT") and broker.posts == []


# ==================================================================================================================================
# D — idempotency, retry, abandon, client_order_id
# ==================================================================================================================================

def test_D_double_click_and_concurrent_confirms_make_one_post(broker):
    ready(broker)
    i = preview()
    broker.post_delay = 0.3
    res = []
    ths = [threading.Thread(target=lambda: res.append(confirm(i))) for _ in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(20)
    assert len(broker.posts) == 1 and sum(1 for r in res if r["already_confirmed"]) == 2
    assert {r["intent"]["intent_id"] for r in res} == {i["intent_id"]}


def _lost_cas(monkeypatch, interloper):
    """Simulate another process moving the intent right before the write-ahead CAS into SUBMISSION_PENDING: the real
    transition then returns False. Records every CAS result and refuses any call of _post_once."""
    real, results, posts = S.OrderStore.transition, [], []

    def hijacked(self, conn, intent_id, expected, new_state, now, kind=None, **fields):
        if new_state == RU.SUBMISSION_PENDING:
            interloper(real, self, conn, intent_id, now)
            results.append(real(self, conn, intent_id, expected, new_state, now, kind=kind, **fields))
            return results[-1]
        return real(self, conn, intent_id, expected, new_state, now, kind=kind, **fields)
    monkeypatch.setattr(S.OrderStore, "transition", hijacked)
    monkeypatch.setattr(O, "_post_once", lambda *a, **k: posts.append(a) or pytest.fail("_post_once called after a lost CAS"))
    return results, posts


def test_D_confirm_fails_closed_when_the_submission_cas_is_lost(broker, monkeypatch):
    ready(broker)
    i = preview()
    results, posts = _lost_cas(monkeypatch, lambda real, st, c, iid, now: real(
        st, c, iid, {RU.CONFIRMING}, RU.CONFIRM_REJECTED, now, kind="CONFIRM_REJECTED", error_code="INTERRUPTED",
        error_text=RU.OUTCOME_TEXT["INTERRUPTED"]))
    e = err(confirm, i)
    assert results == [False] and posts == [] and broker.posts == []                       # CAS lost → no _post_once, 0 POSTs
    assert e.code == "CONFIRM_REJECTED" and e.status == 409 and e.extra["reason"] == "INTERRUPTED"
    row = S.OrderStore().intent(i["intent_id"])
    assert row["state"] == "CONFIRM_REJECTED" and row["submit_attempts"] == 0 and row["client_order_id"] == i["client_order_id"]
    assert [m for m, *_ in broker.requests if m == "POST"] == [] and i["intent_id"] not in O._INFLIGHT


def test_D_retry_fails_closed_when_the_submission_cas_is_lost(broker, monkeypatch):
    ready(broker)
    i = preview()
    broker.post_modes = ["connect_timeout"]
    assert confirm(i)["intent"]["state"] == "SUBMIT_NOT_SENT" and broker.posts == []
    results, posts = _lost_cas(monkeypatch, lambda real, st, c, iid, now: real(
        st, c, iid, {RU.SUBMIT_NOT_SENT}, RU.ABANDONED, now, kind="ABANDONED"))
    e = err(O.retry, i["intent_id"], i["preview_hash"])
    assert results == [False] and posts == [] and broker.posts == []
    assert e.code == "NOT_RETRYABLE" and e.status == 409 and e.extra["intent"]["state"] == "ABANDONED"
    row = S.OrderStore().intent(i["intent_id"])
    assert row["state"] == "ABANDONED" and row["submit_attempts"] == 1 and row["client_order_id"] == i["client_order_id"]
    assert i["intent_id"] not in O._INFLIGHT


def test_D_retry_from_not_sent_looks_up_first_then_posts_the_same_payload(broker):
    ready(broker)
    i = preview()
    broker.post_modes = ["connect_timeout"]
    assert confirm(i)["intent"]["state"] == "SUBMIT_NOT_SENT" and broker.posts == []
    broker.requests.clear()
    out = O.retry(i["intent_id"], i["preview_hash"])
    paths = [p for _, p, _ in broker.requests]
    assert paths.index("/v2/orders:by_client_order_id") < paths.index("/v2/orders")          # lookup precedes the POST
    assert out["intent"]["state"] == "SUBMITTED" and out["intent"]["client_order_id"] == i["client_order_id"]
    assert json.loads(broker.posts[0])["client_order_id"] == i["client_order_id"] and out["intent"]["submit_attempts"] == 2


def test_D_retry_from_reconciliation_required_needs_the_not_found_rule(broker, clock):
    ready(broker)
    i = preview()
    broker.post_modes = ["timeout_before"]
    assert confirm(i)["intent"]["state"] == "RECONCILIATION_REQUIRED"                      # automatic lookup: 404 (1)
    e = err(O.retry, i["intent_id"], i["preview_hash"])                                     # 2nd 404 but span 0 s
    assert e.code == "NOT_FOUND_RULE" and len(broker.posts) == 1
    clock[0] += timedelta(seconds=31)
    broker.now = clock[0]
    out = O.retry(i["intent_id"], i["preview_hash"])
    assert out["intent"]["state"] == "SUBMITTED" and len(broker.posts) == 2 and broker.posts[0] == broker.posts[1]


def test_D_retry_links_instead_of_posting_when_the_lookup_finds_the_order(broker, clock):
    ready(broker)
    i = preview()
    broker.post_modes, broker.lookup_lag = ["timeout_after_record"], 1
    assert confirm(i)["intent"]["state"] == "RECONCILIATION_REQUIRED"
    clock[0] += timedelta(seconds=31)
    broker.now = clock[0]
    out = O.retry(i["intent_id"], i["preview_hash"])
    assert out["linked_without_post"] is True and out["intent"]["state"] == "SUBMITTED" and len(broker.posts) == 1


def test_D_attempts_are_capped_at_three(broker):
    ready(broker)
    i = preview()
    broker.post_modes = ["connect_timeout"] * 3
    confirm(i)
    O.retry(i["intent_id"], i["preview_hash"])
    it = O.retry(i["intent_id"], i["preview_hash"])["intent"]
    assert it["state"] == "SUBMIT_NOT_SENT" and it["submit_attempts"] == 3 and it["can_retry"] is False
    assert err(O.retry, i["intent_id"], i["preview_hash"]).code == "ATTEMPTS_EXHAUSTED"


def test_D_abandon_requires_a_fresh_404_and_links_a_found_order(broker, clock):
    ready(broker)
    i = preview()
    broker.post_modes = ["connect_timeout"]
    confirm(i)
    assert O.abandon(i["intent_id"], i["preview_hash"])["intent"]["state"] == "ABANDONED"
    k = preview("KO", "BUY", 1)
    broker.post_modes, broker.lookup_lag = ["timeout_after_record"], 1
    assert confirm(k)["intent"]["state"] == "RECONCILIATION_REQUIRED"
    out = O.abandon(k["intent_id"], k["preview_hash"])                                        # the order exists: linked
    assert out["intent"]["state"] == "SUBMITTED" and out["intent"]["alpaca_order_id"]
    m = preview("MU", "BUY", 1)
    broker.post_modes = ["timeout_before"]
    confirm(m)
    assert err(O.abandon, m["intent_id"], m["preview_hash"]).code == "NOT_FOUND_RULE"
    clock[0] += timedelta(seconds=31)
    assert O.abandon(m["intent_id"], m["preview_hash"])["intent"]["state"] == "ABANDONED"


def test_D_client_order_id_is_uuid_based_created_at_preview_and_never_regenerated(broker):
    ready(broker)
    i = preview()
    assert re.fullmatch(r"sa46b-[0-9a-f]{32}", i["client_order_id"])
    row = S.OrderStore().intent(i["intent_id"])
    assert row["client_order_id"] == i["client_order_id"] and json.loads(row["payload_json"])["client_order_id"] == i["client_order_id"]
    broker.post_modes = ["connect_timeout", "connect_timeout", "accept"]
    confirm(i)
    O.retry(i["intent_id"], i["preview_hash"])
    O.retry(i["intent_id"], i["preview_hash"])
    assert {json.loads(b)["client_order_id"] for b in broker.posts} == {i["client_order_id"]}
    assert len({preview()["client_order_id"] for _ in range(5)}) == 5                        # every new preview: a new uuid


# ==================================================================================================================================
# E — validation V1–V17
# ==================================================================================================================================

def rules(it):
    return {r["rule"]: (r["ok"], r["code"]) for r in it["rules"]}


def test_E_body_rules_are_refusals_with_no_row_and_no_request(broker):
    ready(broker)
    for sym, side, qty, code in (("amd!", "BUY", 1, "INVALID_SYMBOL"), ("TOOLONG", "BUY", 1, "INVALID_SYMBOL"),
                                 ("AMD", "SHORT", 1, "INVALID_SIDE"), ("AMD", "BUY", 0, "INVALID_QUANTITY"),
                                 ("AMD", "BUY", -1, "INVALID_QUANTITY"), ("AMD", "BUY", 10_001, "INVALID_QUANTITY"),
                                 ("AMD", "BUY", 1.5, "INVALID_QUANTITY"), ("AMD", "BUY", "10", "INVALID_QUANTITY"),
                                 ("AMD", "BUY", True, "INVALID_QUANTITY")):
        assert err(O.preview, sym, side, qty).code == code, (sym, side, qty)
    assert broker.requests == [] and S.OrderStore().intents() == []
    assert rules(preview("AMD", "BUY", 10_000))["V5"] == (True, None)
    assert rules(preview("BRK.B", "BUY", 1))["V3"][0] is True


def test_E_asset_rules(broker):
    ready(broker)
    broker.assets["OTCX"] = {**broker.assets["AMD"], "symbol": "OTCX", "exchange": "OTC"}
    broker.assets["BTCX"] = {**broker.assets["AMD"], "symbol": "BTCX", "class": "crypto"}
    broker.assets["OLD"] = {**broker.assets["AMD"], "symbol": "OLD", "status": "inactive"}
    broker.assets["NOPE"] = {**broker.assets["AMD"], "symbol": "NOPE", "tradable": False}
    for s in ("OTCX", "BTCX", "OLD", "NOPE", "ZZZZ"):
        it = preview(s, "SELL", 1)
        assert rules(it)["V3"] == (False, "ASSET_NOT_SUPPORTED") and it["confirmable"] is False, s


def test_E_sell_rules_use_qty_available_and_need_a_long_position(broker):
    ready(broker)
    broker.set_position("AMD", 10, available=6)
    assert rules(preview("AMD", "SELL", 6))["V9"] == (True, None)
    assert rules(preview("AMD", "SELL", 7))["V9"] == (False, "INSUFFICIENT_QTY_AVAILABLE")
    assert rules(preview("CLS", "SELL", 1))["V9"] == (False, "NO_POSITION")
    it = preview("AMD", "SELL", 5)
    assert it["sell"] == {"alpaca_qty": "10", "qty_available": "6", "local_simulator_shares": 10}


def test_E_buy_cash_guard_boundary_and_reference(broker):
    ready(broker)
    # reference = 101 (Sep 28 close); guard = qty x 101 x 1.05
    broker.account["cash"] = str(Decimal(101) * Decimal("1.05") * 10)                        # exactly equal: allowed
    it = preview("AMD", "BUY", 10)
    assert rules(it)["V10"] == (True, None) and it["reference"]["price"] == "101.0000" and it["reference"]["session"] == "2026-09-28"
    assert it["cash_guard"]["required"] == "1060.50" and it["estimated_notional"] == "1010.00"
    broker.account["cash"] = str(Decimal(101) * Decimal("1.05") * 10 - Decimal("0.01"))
    assert rules(preview("AMD", "BUY", 10))["V10"] == (False, "CASH_GUARD")


def test_E_reference_unavailable_refuses_buy_allows_sell(broker):
    ready(broker)
    broker.lab.market.drop("KO", Dt("2026-09-28"))
    FC.BAR_CACHE.clear()
    broker.set_position("KO", 5)
    b = preview("KO", "BUY", 1)
    assert rules(b)["V10"] == (False, "REFERENCE_PRICE_UNAVAILABLE") and b["confirmable"] is False
    s = preview("KO", "SELL", 1)
    assert s["confirmable"] is True and s["reference"]["price"] is None and s["estimated_notional"] is None


def test_E_notional_cap(broker):
    ready(broker)
    broker.account["cash"] = "1000000"
    broker.lab.market.set_bar("KO", Dt("2026-09-28"), 10, 10, 10, 10)
    FC.BAR_CACHE.clear()
    assert rules(preview("KO", "BUY", 10_000))["V12b"] == (True, None)                     # 100,000.00
    broker.lab.market.set_bar("KO", Dt("2026-09-28"), 10, 10.0001, 10, 10.0001)
    FC.BAR_CACHE.clear()
    assert rules(preview("KO", "BUY", 10_000))["V12b"] == (False, "NOTIONAL_LIMIT")         # 100,001.00


def test_E_daily_limit_twenty_per_new_york_day(broker, clock):
    ready(broker)
    broker.account["cash"] = "10000000"
    for _ in range(20):
        confirm(preview("KO", "BUY", 1))
    assert err(O.preview, "KO", "BUY", 1).code == "DAILY_LIMIT"
    clock[0] += timedelta(days=1)
    broker.now = clock[0]
    broker.next_close = clock[0] + timedelta(hours=6)
    assert preview("KO", "BUY", 1)["state"] == "PREVIEWED"


def test_E_account_rules_not_configured_not_enabled_and_blocked(broker, monkeypatch):
    assert err(O.preview, "AMD", "BUY", 1).code == "NOT_LINKED"
    O.link_account()
    assert err(O.preview, "AMD", "BUY", 1).code == "NOT_ENABLED"
    O.enable()
    broker.account["trading_blocked"] = True
    assert rules(preview())["V8"] == (False, "ACCOUNT_NOT_ACTIVE")
    monkeypatch.delenv("ALPACA_PAPER_SECRET_KEY")
    assert err(O.preview, "AMD", "BUY", 1).code == "NOT_CONFIGURED"


def test_E_expired_superseded_wrong_hash_and_unresolved_symbol(broker, clock):
    ready(broker)
    a = preview()
    b = preview()
    assert err(confirm, a).code == "PREVIEW_SUPERSEDED"
    assert err(O.confirm, b["intent_id"], "0" * 64).code == "PREVIEW_MISMATCH"
    clock[0] += timedelta(seconds=121)
    assert err(confirm, b).code == "PREVIEW_EXPIRED" and S.OrderStore().intent(b["intent_id"])["state"] == "EXPIRED"
    c = preview()
    broker.post_modes = ["connect_timeout"]
    confirm(c)                                                                              # SUBMIT_NOT_SENT: unresolved
    assert err(O.preview, "AMD", "SELL", 1).code == "UNRESOLVED_IN_SYMBOL"
    assert preview("KO", "BUY", 1)["state"] == "PREVIEWED"                                  # other symbols unaffected
    assert broker.posts == []


def test_E_failing_preview_is_stored_but_cannot_be_confirmed(broker):
    ready(broker)
    it = preview("CLS", "SELL", 1)
    assert it["state"] == "PREVIEWED" and it["confirmable"] is False
    assert err(confirm, it).code == "PREVIEW_NOT_CONFIRMABLE" and broker.posts == []


# ==================================================================================================================================
# F — market hours and the 5-minute near-close cutoff
# ==================================================================================================================================

def test_F_market_closed_preview_allowed_confirm_and_retry_rejected(broker):
    ready(broker)
    broker.is_open = False
    it = preview()
    assert it["state"] == "PREVIEWED" and it["confirmable"] is True and it["market"]["is_open"] is False
    e = err(confirm, it)
    assert e.code == "CONFIRM_REJECTED" and e.extra["reason"] == "MARKET_CLOSED" and broker.posts == []
    assert S.OrderStore().intent(it["intent_id"])["state"] == "CONFIRM_REJECTED"
    broker.is_open = True
    k = preview("KO", "BUY", 1)
    broker.post_modes = ["connect_timeout"]
    confirm(k)
    broker.is_open = False
    e = err(O.retry, k["intent_id"], k["preview_hash"])
    assert e.code == "RETRY_REJECTED" and e.extra["reason"] == "MARKET_CLOSED" and broker.posts == []


def test_F_near_close_cutoff_301_allowed_300_rejected(broker):
    ready(broker)
    broker.next_close = broker.now + timedelta(seconds=300)
    it = preview()
    assert it["state"] == "PREVIEWED" and it["market"]["near_close"] is True               # preview still allowed
    e = err(confirm, it)
    assert e.extra["reason"] == "NEAR_CLOSE" and broker.posts == []
    broker.next_close = broker.now + timedelta(seconds=301)
    assert confirm(preview())["intent"]["state"] == "SUBMITTED"


def test_F_cutoff_uses_the_alpaca_clock_timestamp_not_the_local_clock(broker, clock):
    ready(broker)
    clock[0] = broker.now + timedelta(hours=9)                                              # local clock: after the close
    assert confirm(preview())["intent"]["state"] == "SUBMITTED"                             # Alpaca: open, 6 h to close
    clock[0] = broker.now - timedelta(hours=9)                                              # local clock: before the open
    broker.next_close = broker.now + timedelta(seconds=200)
    assert err(confirm, preview()).extra["reason"] == "NEAR_CLOSE"


# ==================================================================================================================================
# G — settings, account binding, no_shorting (read only)
# ==================================================================================================================================

def test_G_defaults_link_creates_tables_reads_never_do(broker):
    path = Path(broker.lab.path)
    assert not set(TABLES) & tables_in(path)
    s = O.settings_view()
    assert (s["linked"], s["enabled"], s["tables"]) == (False, False, False) and O.list_intents()["intents"] == []
    O.disable()
    assert not set(TABLES) & tables_in(path) and broker.requests == []                     # nothing to disable, nothing created
    s = O.link_account()
    assert set(TABLES) <= tables_in(path) and s["linked"] is True and s["enabled"] is False
    assert s["account_masked"] == "••••7890" and [p for _, p, _ in broker.requests] == ["/v2/account", "/v2/account/configurations"]
    assert err(O.link_account).code == "ALREADY_LINKED"


def test_G_enable_requires_link_and_no_shorting_and_no_shorting_blocks_everything(broker):
    assert err(O.enable).code == "NOT_LINKED"
    O.link_account()
    broker.config["no_shorting"] = False
    assert err(O.enable).code == "NO_SHORTING_REQUIRED" and O.settings_view()["enabled"] is False
    broker.config["no_shorting"] = True
    O.enable()
    it = preview()
    broker.config["no_shorting"] = False
    assert rules(preview())["V15"] == (False, "NO_SHORTING_REQUIRED")
    assert err(confirm, it).code in ("PREVIEW_SUPERSEDED",)
    broker.config["no_shorting"] = True
    it = preview()
    broker.config["no_shorting"] = False
    assert err(confirm, it).extra["reason"] == "NO_SHORTING_REQUIRED"
    broker.config["no_shorting"] = True
    k = preview("KO", "BUY", 1)
    broker.post_modes = ["connect_timeout"]
    confirm(k)
    broker.config["no_shorting"] = False
    assert err(O.retry, k["intent_id"], k["preview_hash"]).extra["reason"] == "NO_SHORTING_REQUIRED"
    assert broker.posts == [] and all(m in ("GET", "POST") for m, *_ in broker.requests)   # never a PATCH


def test_G_account_change_needs_explicit_relink(broker):
    ready(broker)
    first = preview()
    broker.account.update(id=OTHER_ACCOUNT_ID, account_number="PA3OTHER1234")
    assert rules(preview())["V8"] == (False, "ACCOUNT_NOT_LINKED")
    assert err(O.enable).code == "ACCOUNT_NOT_LINKED"
    assert err(O.relink_account, "••••0000").code == "RELINK_MISMATCH"
    broker.account.update(id=ACCOUNT_ID, account_number="PA3FAKE7890")
    k = preview("KO", "BUY", 1)
    broker.post_modes = ["connect_timeout"]
    confirm(k)                                                                              # an unresolved intent
    broker.account.update(id=OTHER_ACCOUNT_ID, account_number="PA3OTHER1234")
    assert err(O.relink_account, "••••7890").code == "UNRESOLVED_INTENTS"
    broker.account.update(id=ACCOUNT_ID, account_number="PA3FAKE7890")
    O.abandon(k["intent_id"], k["preview_hash"])
    assert err(O.relink_account, "••••7890").code == "SAME_ACCOUNT"
    p = preview("MU", "BUY", 1)
    broker.account.update(id=OTHER_ACCOUNT_ID, account_number="PA3OTHER1234")
    s = O.relink_account("••••7890")
    assert s["account_masked"] == "••••1234" and s["enabled"] is False
    assert S.OrderStore().intent(p["intent_id"])["state"] == "SUPERSEDED" and S.OrderStore().intent(first["intent_id"])["state"] == "SUPERSEDED"
    assert broker.posts == []


def test_G_disable_supersedes_previews_and_blocks_retry_but_not_status(broker):
    ready(broker)
    k = preview("KO", "BUY", 1)
    broker.post_modes = ["connect_timeout"]
    confirm(k)
    p = preview()
    O.disable()
    assert S.OrderStore().intent(p["intent_id"])["state"] == "SUPERSEDED"
    assert err(O.retry, k["intent_id"], k["preview_hash"]).code == "NOT_ENABLED"
    assert O.check_status()["checked"] >= 1 and broker.posts == []


def order_tables(path):
    return [db_rows(path, t) for t in TABLES]


def test_G_status_with_another_paper_account_is_refused_before_any_lookup_or_state_change(broker, clock):
    ready(broker)
    i = preview()
    broker.post_modes = ["timeout_before"]
    confirm(i)                                                                              # RECONCILIATION_REQUIRED
    k = preview("KO", "BUY", 1)
    confirm(k)                                                                              # SUBMITTED (live)
    st = S.OrderStore()
    m = preview("MU", "BUY", 1)
    with st.write() as c:                                                                   # an interrupted submission ...
        st.transition(c, m["intent_id"], {"PREVIEWED"}, "CONFIRMING", clock[0], confirmed_at=clock[0].isoformat())
        st.transition(c, m["intent_id"], {"CONFIRMING"}, "SUBMISSION_PENDING", clock[0], submit_attempts=1,
                      last_submit_at=clock[0].isoformat())
    clock[0] += timedelta(seconds=46)                                                       # ... now stale
    broker.account.update(id=OTHER_ACCOUNT_ID, account_number="PA3OTHER1234")              # .env switched to another account
    path = Path(broker.lab.path)
    before = order_tables(path)
    broker.requests.clear()
    assert err(O.check_status).code == "ACCOUNT_NOT_LINKED"
    assert [p for _, p, _ in broker.requests] == ["/v2/account"]                           # 1 account GET, 0 order lookups
    assert order_tables(path) == before and st.intent(m["intent_id"])["state"] == "SUBMISSION_PENDING"   # 0 state changes
    broker.account.update(id=ACCOUNT_ID, account_number="PA3FAKE7890")                    # the linked account again
    clock[0] += timedelta(seconds=4)
    broker.requests.clear()
    out = O.check_status()
    paths = [p for _, p, _ in broker.requests]
    assert paths[0] == "/v2/account" and paths.count("/v2/account") == 1                   # ONE account GET for the action
    assert all(p.startswith("/v2/orders") for p in paths[1:]) and out["checked"] == len(paths) - 1 == 3
    assert st.intent(m["intent_id"])["state"] == "RECONCILIATION_REQUIRED"                 # existing behaviour unchanged
    assert st.intent(k["intent_id"])["state"] == "SUBMITTED" and broker.posts and len(broker.posts) == 2


def test_G_abandon_with_another_paper_account_is_refused_before_the_lookup(broker):
    ready(broker)
    i = preview()
    broker.post_modes = ["connect_timeout"]
    confirm(i)                                                                              # SUBMIT_NOT_SENT
    broker.account.update(id=OTHER_ACCOUNT_ID, account_number="PA3OTHER1234")
    path = Path(broker.lab.path)
    before = order_tables(path)
    broker.requests.clear()
    assert err(O.abandon, i["intent_id"], i["preview_hash"]).code == "ACCOUNT_NOT_LINKED"
    assert [p for _, p, _ in broker.requests] == ["/v2/account"]                           # 1 account GET, 0 order lookups
    assert order_tables(path) == before and S.OrderStore().intent(i["intent_id"])["state"] == "SUBMIT_NOT_SENT"
    broker.account.update(id=ACCOUNT_ID, account_number="PA3FAKE7890")
    broker.requests.clear()
    assert O.abandon(i["intent_id"], i["preview_hash"])["intent"]["state"] == "ABANDONED"  # existing behaviour unchanged
    assert [p for _, p, _ in broker.requests] == ["/v2/account", "/v2/orders:by_client_order_id"]
    assert broker.posts == []


# ==================================================================================================================================
# H — reference price source and credential separation
# ==================================================================================================================================

def test_H_reference_provenance_budget_and_no_market_data_at_confirm(broker):
    ready(broker)
    it = preview()
    assert it["reference"]["source"] == "FETCHED_NOW" and it["reference"]["market_data_requests"] == 1
    assert len(broker.lab.md_calls) == 1 and set(broker.lab.md_calls[0]) == {"AMD", "SPY"}
    it2 = preview()
    assert it2["reference"]["source"] == "MEMORY_CACHE" and len(broker.lab.md_calls) == 1
    confirm(it2)
    assert len(broker.lab.md_calls) == 1                                                    # 0 market-data at confirm
    assert "not a quote, not the fill price" in it2["reference"]["note"]


def test_H_paper_credentials_only_on_the_paper_wire_market_data_pair_never(broker, monkeypatch):
    import config
    monkeypatch.setenv("ALPACA_API_KEY", "MARKET_DATA_KEY_SENTINEL")
    monkeypatch.setattr(config, "ALPACA_API_KEY", "MARKET_DATA_KEY_SENTINEL")
    ready(broker)
    confirm(preview())
    for h in broker.headers_seen:
        assert h.get("APCA-API-KEY-ID") == KEY and "MARKET_DATA_KEY_SENTINEL" not in json.dumps(h)
    monkeypatch.delenv("ALPACA_PAPER_API_KEY")
    assert err(O.preview, "AMD", "BUY", 1).code == "NOT_CONFIGURED"
    for f in NEW_PRODUCT:
        assert not re.search(r"ALPACA_API_KEY|ALPACA_SECRET_KEY|config\.ALPACA|has_credentials", (ROOT / f).read_text(encoding="utf-8")), f


# ==================================================================================================================================
# I — wording; J — status map
# ==================================================================================================================================

GUARD = ("Stock Agent cash guard: previous close + 5% must fit in your Alpaca paper cash — a conservative estimate, not the "
         "fill price; Alpaca decides buying power")


def test_I_cash_guard_and_market_wording(broker):
    js = (ROOT / "frontend" / "alpaca_orders.js").read_text(encoding="utf-8")
    doc = (ROOT / "paper" / "ALPACA_ORDERS.md").read_text(encoding="utf-8")
    for s in (GUARD, "PAPER ACCOUNT — simulated trading only · orders go to your Alpaca PAPER account",
              "not a quote, not the fill price", "estimate only", "Confirmation is disabled within 5 minutes of the market close",
              "Market closed — confirmation is available during regular hours", "Preview Paper Order", "Confirm Paper Order: ",
              "Retry submission (same client order id)", "Check order status", "Discard preview",
              "turn it on in your Alpaca paper dashboard; Stock Agent never changes it"):
        assert s in js, s
    for s in ("5%", "cash guard", "not the fill price", "Alpaca decides buying power", "ALPACA_PAPER_API_KEY", "no_shorting"):
        assert s in doc, s
    ready(broker)
    it = preview()
    assert it["cash_guard"]["text"] == GUARD
    texts = json.dumps(it) + js + doc + json.dumps(RU.CATEGORY_TEXT) + json.dumps(RU.RULE_TEXT)
    assert not re.search(r"(?i)guarantee", texts.replace("NOT a guarantee", "").replace("not a guarantee", ""))


def test_J_status_map_covers_every_alpaca_status():
    expect = {"pending_new": "SUBMITTED", "accepted": "SUBMITTED", "accepted_for_bidding": "SUBMITTED", "pending_review": "SUBMITTED",
              "new": "BROKER_ACCEPTED", "held": "BROKER_ACCEPTED", "calculated": "BROKER_ACCEPTED", "done_for_day": "BROKER_ACCEPTED",
              "pending_cancel": "BROKER_ACCEPTED", "suspended": "BROKER_ACCEPTED", "stopped": "BROKER_ACCEPTED",
              "partially_filled": "PARTIALLY_FILLED", "filled": "FILLED", "canceled": "CANCELED", "expired": "CANCELED",
              "rejected": "BROKER_REJECTED", "replaced": "RECONCILIATION_REQUIRED", "pending_replace": "RECONCILIATION_REQUIRED"}
    from alpaca.trading.enums import OrderStatus                                             # the installed SDK's 18 values
    assert {s.value for s in OrderStatus} == set(expect)
    for k, v in expect.items():
        assert RU.map_status(k)[0] == v, k
    assert RU.map_status("something_new") == ("RECONCILIATION_REQUIRED", "UNKNOWN_STATUS")
    assert RU.map_status("replaced") == ("RECONCILIATION_REQUIRED", "BROKER_REPLACED")


def test_J_status_check_follows_the_broker_exactly(broker, clock):
    ready(broker)
    i = preview()
    confirm(i)
    broker.set_status(i["client_order_id"], "partially_filled", 4, "100.10")
    O.check_status()
    it = O.list_intents()["intents"][0]
    assert (it["state"], it["filled_qty"], it["filled_avg_price"]) == ("PARTIALLY_FILLED", "4", "100.10")
    broker.set_status(i["client_order_id"], "filled", 10, "100.20")
    clock[0] += timedelta(seconds=4)
    O.check_status()
    assert O.list_intents()["intents"][0]["state"] == "FILLED"
    assert err(O.check_status).code == "STATUS_TOO_SOON"
    clock[0] += timedelta(seconds=4)
    assert O.check_status()["checked"] == 0 and broker.posts == [broker.posts[0]]           # terminal: never looked up


# ==================================================================================================================================
# K — transports
# ==================================================================================================================================

def test_K_writer_transport_refuses_everything_but_one_exact_post():
    fake = FakeAlpaca()
    body = RU.canonical_payload("sa46b-" + "a" * 32, "AMD", "buy", 1)
    t = W._WriteTransport(fake, body)
    s = requests.Session()
    s.mount("https://", t)
    s.mount("http://", t)
    base = "https://paper-api.alpaca.markets"
    for method, url, data in (("GET", base + "/v2/orders", None), ("DELETE", base + "/v2/orders", None),
                              ("PATCH", base + "/v2/account/configurations", body), ("POST", "https://api.alpaca.markets/v2/orders", body),
                              ("POST", "http://paper-api.alpaca.markets/v2/orders", body), ("POST", base + "/v2/orders/x", body),
                              ("POST", base + "/v2/positions", body), ("POST", base + "/v2/orders?x=1", body),
                              ("POST", base + "/v2/orders", body + b" "), ("POST", "https://paper-api.alpaca.markets:444/v2/orders", body)):
        with pytest.raises(W.WriteRefused):
            s.request(method, url, data=data)
    assert fake.requests == []
    s.post(base + "/v2/orders", data=body)
    with pytest.raises(W.WriteRefused, match="one request"):
        s.post(base + "/v2/orders", data=body)
    assert len(fake.posts) == 1


def test_K_writer_refuses_outside_scope_and_passes_finite_timeouts(broker):
    body = RU.canonical_payload("sa46b-" + "b" * 32, "AMD", "buy", 1)
    with pytest.raises(W.WriteRefused, match="scope"):
        W.submit(body, "sa46b-" + "b" * 32)
    assert broker.requests == []
    seen = {}
    real_send = broker.send

    def spy(request, **kw):
        seen["timeout"] = kw.get("timeout")
        return real_send(request, **kw)
    broker.send = spy
    tok = W._SCOPE.set(1)
    try:
        res = W.submit(body, "sa46b-" + "b" * 32)
    finally:
        W._SCOPE.reset(tok)
    assert res["kind"] == "response" and res["status"] == 200 and seen["timeout"] == W.TIMEOUT == (5.0, 15.0)
    tok = W._SCOPE.set(1)
    try:
        with pytest.raises(W.WriteRefused):
            W.submit(body, "sa46b-" + "c" * 32)                                              # coid ≠ payload coid
    finally:
        W._SCOPE.reset(tok)


def test_K_read_transport_allow_list_budget_and_no_redirects(broker):
    rd = RD.PaperOrderReader(budget=3)
    rd.account()
    rd.clock()
    assert rd.position("CLS") is None
    with pytest.raises(RD.ReadError) as e:
        rd.configurations()
    assert e.value.code == "BUDGET"
    n0 = len(broker.requests)
    t = RD._ReadTransport(broker, budget=10)
    s = requests.Session()
    s.mount("https://", t)
    s.mount("http://", t)
    base = "https://paper-api.alpaca.markets"
    for method, url in (("POST", base + "/v2/orders"), ("PATCH", base + "/v2/account/configurations"), ("GET", base + "/v2/orders"),
                        ("GET", base + "/v2/positions"), ("GET", base + "/v2/account/activities"), ("GET", "https://api.alpaca.markets/v2/account"),
                        ("GET", base + "/v2/orders:by_client_order_id?client_order_id=x"), ("GET", base + "/v2/orders/not-a-uuid"),
                        ("GET", base + "/v2/assets/amd!"), ("GET", "http://paper-api.alpaca.markets/v2/account")):
        with pytest.raises(RD.ReadRefused):
            s.request(method, url)
    assert len(broker.requests) == n0                                                       # nothing reached the broker
    n = len(broker.requests)
    broker.orders["00000000-0000-4000-8000-000000000000"] = {"x": 1}
    real_send = broker.send
    broker.send = lambda request, **kw: broker._resp(request, 302, {"location": "elsewhere"})
    with pytest.raises(RD.ReadError) as e:
        RD.PaperOrderReader(budget=1).account()
    assert e.value.code == "UNEXPECTED_RESPONSE" and e.value.http_status == 302
    broker.send = real_send
    assert len(broker.requests) == n


# ==================================================================================================================================
# L — security, isolation, static boundaries
# ==================================================================================================================================

def test_L_secrets_account_number_and_raw_broker_text_never_surface(api, caplog):
    with caplog.at_level(logging.DEBUG):
        api.p("/settings/link-account", {"confirm": True})
        api.p("/settings/enable", {"confirm": True})
        pv = api.p("/preview", {"symbol": "AMD", "side": "BUY", "quantity": 1}).json()["intent"]
        api.broker.post_modes = [f'http:422:{{"code": 40010000, "message": "bad key {KEY} secret {SECRET} account PA3FAKE7890"}}']
        out = api.p("/confirm", {"preview_id": pv["intent_id"], "preview_hash": pv["preview_hash"], "confirm": True})
        texts = [out.text, api.get("/api/alpaca-paper-orders").text, api.get("/api/alpaca-paper-orders/settings").text, caplog.text]
    path = Path(api.broker.lab.path)
    texts += [json.dumps(db_rows(path, t)) for t in TABLES]
    for t in texts:
        for s in (KEY, SECRET, "PA3FAKE7890", ACCOUNT_ID, "APCA-API", "bad key"):
            assert s not in t, s
    assert any(r["account_fp"] == hashlib.sha256(ACCOUNT_ID.encode()).hexdigest() for r in db_rows(path, TABLES[1]))


def test_L_http_gates(api):
    good = {"symbol": "AMD", "side": "BUY", "quantity": 1}
    api.p("/settings/link-account", {"confirm": True})
    api.p("/settings/enable", {"confirm": True})
    assert api.post("/api/alpaca-paper-orders/preview", json=good).status_code == 403               # no custom header
    assert api.p("/preview", good, headers={"host": "evil.example:8000"}).status_code == 403          # DNS rebinding
    assert api.p("/preview", good, headers={"origin": "http://evil.example"}).status_code == 403
    assert api.p("/preview", good, headers={"origin": "http://127.0.0.1:8000"}).status_code == 200
    assert api.p("/preview", good, headers={"origin": "http://localhost:8000", "host": "localhost:8000"}).status_code == 200
    far = TestClient(api.app, base_url="http://127.0.0.1:8000", client=("192.168.1.50", 50000))
    assert far.post("/api/alpaca-paper-orders/preview", json=good, headers=HDR).status_code == 403
    assert api.post("/api/alpaca-paper-orders/preview", content=json.dumps(good), headers={**HDR, "Content-Type": "text/plain"}).status_code == 422
    assert api.p("/preview?x=1", good).status_code == 422
    for extra in ({"type": "limit"}, {"time_in_force": "gtc"}, {"limit_price": 1}, {"client_order_id": "x"}, {"account": "x"},
                  {"url": "x"}, {"paper": False}):
        assert api.p("/preview", {**good, **extra}).status_code == 422, extra
    pv = api.p("/preview", good).json()["intent"]
    for extra in ({"symbol": "AMD"}, {"quantity": 5}, {"side": "SELL"}):
        assert api.p("/confirm", {"preview_id": pv["intent_id"], "preview_hash": pv["preview_hash"], "confirm": True, **extra}).status_code == 422
    assert api.p("/confirm", {"preview_id": pv["intent_id"], "preview_hash": pv["preview_hash"], "confirm": False}).status_code == 422
    for m in ("put", "patch", "delete"):
        assert getattr(api, m)("/api/alpaca-paper-orders/confirm").status_code == 405
    assert api.broker.posts == []


def test_L_exact_routes_and_frozen_carve_outs():
    from api.server import app
    from paper_endpoints import ALPACA_PAPER_ENDPOINTS, ALPACA_PAPER_ORDER_ENDPOINTS
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/alpaca-paper-orders")}
    assert mine == ALPACA_PAPER_ORDER_ENDPOINTS
    assert {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/alpaca-paper/")} == ALPACA_PAPER_ENDPOINTS
    for p in mine:
        assert not re.search(r"(?i)cancel|replace|close|liquidate|exercise|patch|configur", p), p
    t46a = (ROOT / "tests" / "test_alpaca_paper_46.py").read_text(encoding="utf-8")
    assert t46a.count('not p.startswith("/api/alpaca-paper-orders")') == 1                   # the one approved line


def test_L_writer_import_and_call_graph():
    importers, submit_callers, service_importers = [], [], []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith((".venv/", "tests/", "browser_tests/")) or "__pycache__" in rel:
            continue
        src = p.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "paper" and any(a.name == "alpaca_order_writer" for a in n.names):
                importers.append(rel)
            if isinstance(n, (ast.Import, ast.ImportFrom)) and "alpaca_order_writer" in ast.dump(n) and rel not in importers:
                importers.append(rel)
            if isinstance(n, ast.ImportFrom) and n.module == "paper" and any(a.name == "alpaca_orders" for a in n.names):
                service_importers.append(rel)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "submit" and \
                    isinstance(n.func.value, ast.Name) and n.func.value.id == "W":
                submit_callers.append(rel)
    assert sorted(set(importers)) == ["paper/alpaca_orders.py"]
    assert sorted(set(service_importers)) == ["api/routes/alpaca_paper_orders.py"]
    assert submit_callers == ["paper/alpaca_orders.py"]
    svc = (ROOT / "paper" / "alpaca_orders.py").read_text(encoding="utf-8")
    fn = {n.name: n for n in ast.walk(ast.parse(svc)) if isinstance(n, ast.FunctionDef)}
    callers = [name for name, f in fn.items() if "W.submit(" in ast.unparse(f)]
    assert callers == ["_post_once"]
    post_once_users = sorted(name for name, f in fn.items() if "_post_once(" in ast.unparse(f) and name != "_post_once")
    assert post_once_users == ["confirm", "retry"]
    routes = (ROOT / "api" / "routes" / "alpaca_paper_orders.py").read_text(encoding="utf-8")
    assert routes.count("O.confirm(") == 1 and routes.count("O.retry(") == 1


def test_L_static_isolation_no_sdk_no_ai_no_automation_no_other_verbs():
    for f in NEW_PRODUCT:
        s = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+alpaca\b|TradingClient|alpaca\.trading", s, re.M), f
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|new Function|pickle|"
                             r"importlib|powershell|threading\.Thread|setInterval|setTimeout|Scheduler|register_extension|"
                             r"anthropic|get_provider|from agents|import agents|rh_gateway|robinhood|TradingStream|websocket|"
                             r"url_override|(?<![\w.-])api\.alpaca\.markets", s, re.I), f
        assert not re.search(r"""["'](PATCH|PUT|DELETE)["']|\.(patch|put|delete)\(""", s), f
    for f in [x for x in NEW_PRODUCT if x != "paper/alpaca_order_writer.py"]:
        s = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"""["']/v2/orders["']|requests\.post\(|\.post\(\s*ORDERS_URL|method\s*==\s*["']POST["']\s*and""", s), f
    wr = (ROOT / "paper" / "alpaca_order_writer.py").read_text(encoding="utf-8")
    assert wr.count('ORDERS_URL = f"https://{PAPER_HOST}/v2/orders"') == 1
    for root in ("forward", "fit", "brief", "notifications", "backtest", "strategy", "scanner", "comparison", "ai_explain", "agents",
                 "insights", "portfolio", "data", "services", "alerts", "analysis"):
        for p in (ROOT / root).rglob("*.py"):
            assert not re.search(r"alpaca_order|alpaca_orders|alpaca-paper-orders", p.read_text(encoding="utf-8", errors="ignore")), p
    server = (ROOT / "api" / "server.py").read_text(encoding="utf-8")
    life = server.split("async def _lifespan")[1].split("app = FastAPI(")[0]
    assert "alpaca" not in life


def test_L_real_wires_are_tripwired_in_every_test():
    """conftest: without the fake broker, both 4.6B wires are blocked (a real request would fail the test)."""
    with pytest.raises(AssertionError, match="real Alpaca"):
        RD.WIRE()
    with pytest.raises(AssertionError, match="real Alpaca"):
        W.WIRE()
    import conftest
    conftest._ALPACA_ORDER_WIRE_HITS.clear()                                               # this test's deliberate hits


# ==================================================================================================================================
# M — database: additive migration, triggers, 4.5 tables untouched
# ==================================================================================================================================

def test_M_migration_additive_idempotent_and_triggers(broker):
    from database.alpaca_order_migrations import run_alpaca_order_migrations
    path = Path(broker.lab.path)
    with sqlite3.connect(str(path)) as c:
        uv = c.execute("PRAGMA user_version").fetchone()[0]
        t0 = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows0 = {t: c.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in t0}
        for _ in range(3):
            run_alpaca_order_migrations(c)
        t1 = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert t1 - t0 == set(TABLES) and c.execute("PRAGMA user_version").fetchone()[0] == uv
        assert {t: c.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in t0} == rows0
    ready(broker)
    i = preview()
    confirm(i)
    with sqlite3.connect(str(path)) as c:
        for sql in ("DELETE FROM alpaca_paper_order_intents", "DELETE FROM alpaca_paper_order_events",
                    "DELETE FROM alpaca_paper_order_settings", "UPDATE alpaca_paper_order_events SET code = 'x'",
                    "UPDATE alpaca_paper_order_intents SET qty = 99", "UPDATE alpaca_paper_order_intents SET client_order_id = 'sa46b-x'",
                    "UPDATE alpaca_paper_order_intents SET payload_json = '{}'", "UPDATE alpaca_paper_order_intents SET preview_hash = 'x'",
                    "UPDATE alpaca_paper_order_intents SET alpaca_order_id = '00000000-0000-4000-8000-000000000000'"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)
        c.execute("UPDATE alpaca_paper_order_intents SET broker_status = 'filled'")        # a non-terminal row may progress
    broker.set_status(i["client_order_id"], "filled", 10, "101")
    O.check_status()
    with sqlite3.connect(str(path)) as c:
        with pytest.raises(sqlite3.DatabaseError, match="final"):
            c.execute("UPDATE alpaca_paper_order_intents SET broker_status = 'x'")           # FILLED is terminal


def test_M_migration_file_and_exact_schema_are_pinned(tmp_path):
    """The ONE approved Alpaca migration file (the frozen 4.6A test exempts exactly this name) and its exact schema."""
    from database.alpaca_order_migrations import run_alpaca_order_migrations
    assert sorted(p.name for p in (ROOT / "database").glob("*alpaca*")) == ["alpaca_order_migrations.py"]
    assert not list((ROOT / "database").glob("*broker*"))
    db = tmp_path / "pin.db"
    with sqlite3.connect(str(db)) as c:
        run_alpaca_order_migrations(c)
        objs = sorted(c.execute("SELECT type, name FROM sqlite_master"))
    assert [n for t, n in objs if t == "table"] == sorted(TABLES)                            # exactly three tables, nothing else
    assert objs == sorted([("table", t) for t in TABLES] + [
        ("index", "idx_apo_events_intent"), ("index", "idx_apo_intents_state"), ("index", "idx_apo_intents_symbol"),
        ("index", "sqlite_autoindex_alpaca_paper_order_intents_1"), ("index", "sqlite_autoindex_alpaca_paper_order_intents_2"),
        ("index", "sqlite_autoindex_alpaca_paper_order_intents_3"),
        ("trigger", "apo_events_no_delete"), ("trigger", "apo_events_no_update"), ("trigger", "apo_intents_immutable"),
        ("trigger", "apo_intents_no_delete"), ("trigger", "apo_intents_order_id_once"), ("trigger", "apo_intents_terminal_final"),
        ("trigger", "apo_settings_no_delete")])
    t46a = (ROOT / "tests" / "test_alpaca_paper_46.py").read_text(encoding="utf-8")
    assert t46a.count('if p.name != "alpaca_order_migrations.py"]') == 1                     # the second approved 4.6A line


def test_M_stage_4_5_tables_and_views_untouched(broker):
    path = Path(broker.lab.path)

    def paper_hash():
        with sqlite3.connect(str(path)) as c:
            return {t: c.execute(f'SELECT rowid, * FROM "{t}" ORDER BY rowid').fetchall()
                    for t in ("paper_accounts", "paper_orders", "paper_fills", "paper_lots", "paper_lot_closures")}
    from paper import portfolio as P
    h0, v0 = paper_hash(), json.dumps(P.view(), sort_keys=True)
    ready(broker)
    for mode in ("accept", "timeout_after_record", "connect_timeout"):
        broker.post_modes = [mode]
        confirm(preview("KO", "BUY", 1)) if mode != "connect_timeout" else confirm(preview("MU", "BUY", 1))
    assert paper_hash() == h0 and json.dumps(P.view(), sort_keys=True) == v0


# ==================================================================================================================================
# no AI / no Robinhood through the API; full HTTP happy path
# ==================================================================================================================================

def test_api_happy_path_no_ai_no_robinhood(api):
    b0 = api.budgets()
    assert api.p("/settings/link-account", {"confirm": True}).json()["linked"] is True
    assert api.p("/settings/enable", {"confirm": True}).json()["enabled"] is True
    pv = api.p("/preview", {"symbol": "AMD", "side": "BUY", "quantity": 2}).json()["intent"]
    assert pv["confirm_label"] == "Confirm Paper Order: BUY 2 AMD" and pv["confirmable"] is True
    r = api.p("/confirm", {"preview_id": pv["intent_id"], "preview_hash": pv["preview_hash"], "confirm": True})
    assert r.status_code == 200 and r.json()["intent"]["state"] == "SUBMITTED" and len(api.broker.posts) == 1
    again = api.p("/confirm", {"preview_id": pv["intent_id"], "preview_hash": pv["preview_hash"], "confirm": True})
    assert again.status_code == 200 and again.json()["already_confirmed"] is True and len(api.broker.posts) == 1
    assert api.p("/status").status_code == 200 and api.hits == [] and api.budgets() == b0
    assert api.p("/status").status_code == 429                                                # spacing
