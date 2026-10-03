"""Stage 4.6A — ALPACA PAPER · READ ONLY: the one TradingClient adapter (paper/alpaca_readonly.py), the explicit refresh
(paper/alpaca_view.py), the observational comparison with the Stage 4.5 local simulator (paper/reconcile.py) and the API.

Fakes only: a fake TradingClient (write methods raise), a fake network wire behind the REAL guarded transport, and a fake
reader. Fake credentials TEST_KEY_123 / TEST_SECRET_456. Sockets are blocked (conftest); no real Alpaca call is possible."""
import hashlib
import inspect
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient
from requests.adapters import BaseAdapter

import fw_fixtures as FL
import test_paper_45 as T45
from fit import current as FC
from fit import readonly as RO
from paper import alpaca_readonly as R
from paper import alpaca_view as AV
from paper import portfolio as P
from paper import reconcile as RC

ROOT = Path(__file__).resolve().parents[1]
Dt = date.fromisoformat
KEY, SECRET = "TEST_KEY_123", "TEST_SECRET_456"
FULL_ACCOUNT = "PA1234567890"
NEW_PRODUCT = ["paper/alpaca_readonly.py", "paper/alpaca_view.py", "paper/reconcile.py", "api/routes/alpaca_paper.py",
               "frontend/alpaca_paper.js"]
WRITE_METHODS = ("submit_order", "replace_order_by_id", "cancel_order_by_id", "cancel_orders", "close_position",
                 "close_all_positions", "exercise_options_position", "set_account_configurations", "create_watchlist",
                 "update_watchlist_by_id", "delete_watchlist_by_id", "add_asset_to_watchlist_by_id",
                 "remove_asset_from_watchlist_by_id")

# ---- broker data (the shapes Alpaca returns) ------------------------------------------------------------------------------------

ACCT = {"id": "11111111-2222-3333-4444-555555555555", "account_number": FULL_ACCOUNT, "status": "ACTIVE", "currency": "USD",
        "cash": "25000.25", "equity": "26600.25", "portfolio_value": "26600.25", "buying_power": "51600.5",
        "long_market_value": "1600", "short_market_value": "0", "trading_blocked": False, "account_blocked": False,
        "created_at": "2026-01-05T15:00:00Z"}


def pos(sym, qty, avg="150", price="160", side="long"):
    q = abs(float(qty))
    return {"asset_id": "a1111111-2222-3333-4444-555555555555", "symbol": sym, "exchange": "NASDAQ", "asset_class": "us_equity",
            "avg_entry_price": avg, "qty": str(qty), "side": side, "cost_basis": str(q * float(avg)),
            "market_value": str(q * float(price)), "current_price": price, "unrealized_pl": str(q * (float(price) - float(avg))),
            "unrealized_plpc": "0.0667"}


def order_json(sym="AMD", qty="10", oid="c1111111-2222-3333-4444-555555555555"):
    return {"id": oid, "client_order_id": "client-" + sym.lower(), "created_at": "2026-09-28T13:31:00Z",
            "updated_at": "2026-09-28T13:31:01Z", "submitted_at": "2026-09-28T13:31:00Z", "filled_at": "2026-09-28T13:31:01Z",
            "symbol": sym, "asset_class": "us_equity", "qty": qty, "filled_qty": qty, "filled_avg_price": "100",
            "order_class": "simple", "order_type": "market", "type": "market", "side": "buy", "time_in_force": "day",
            "status": "filled", "extended_hours": False}


def fill_json(sym="AMD", qty="10", oid="c1111111-2222-3333-4444-555555555555"):
    return {"id": "20260928133101000::d1", "activity_type": "FILL", "transaction_time": "2026-09-28T13:31:01Z", "type": "fill",
            "price": "100", "qty": qty, "side": "buy", "symbol": sym, "leaves_qty": "0", "order_id": oid, "cum_qty": qty,
            "order_status": "filled"}


BODIES = {"/v2/account": ACCT, "/v2/positions": [pos("MU", "5"), pos("AMD", "10")], "/v2/orders": [order_json()],
          "/v2/account/activities": [fill_json()]}


class FakeWire(BaseAdapter):
    """The network, faked BELOW the real guarded transport: records every request that would have been sent."""

    def __init__(self, bodies=None, status=None, raise_=None, echo_secret=False):
        super().__init__()
        self.bodies, self.status, self.raise_ = dict(bodies or BODIES), dict(status or {}), dict(raise_ or {})
        self.echo_secret, self.seen = echo_secret, []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        u = urlsplit(request.url)
        self.seen.append(SimpleNamespace(method=request.method, url=request.url, host=u.hostname, path=u.path,
                                         query=parse_qs(u.query), timeout=timeout, headers=dict(request.headers)))
        if u.path in self.raise_:
            raise self.raise_[u.path]
        r = requests.Response()
        r.status_code, r.url, r.request = self.status.get(u.path, 200), request.url, request
        body = self.bodies.get(u.path)
        if r.status_code >= 400 and self.echo_secret:          # an error body that repeats the credentials back
            body = {"code": 40110000, "message": f"request is not authorized {request.headers.get('APCA-API-KEY-ID')} "
                                                 f"{request.headers.get('APCA-API-SECRET-KEY')}"}
        r._content = json.dumps(body).encode()
        r.headers["Content-Type"] = "application/json"
        if 300 <= r.status_code < 400:
            r.headers["Location"] = "https://example.invalid/elsewhere"
        return r

    def close(self):
        pass


class FakeTradingClient:
    """Records construction and every read; ANY other method (write, generic get/post, ...) raises immediately."""
    instances: list = []

    def __init__(self, *args, **kwargs):
        from alpaca.trading.models import Order, Position, TradeAccount
        self._models = (TradeAccount, Position, Order)
        self.init, self.calls, self.hits, self.filter = (args, kwargs), [], [], None
        self._session, self._retry = requests.Session(), 3
        FakeTradingClient.instances.append(self)

    def get_account(self):
        self.calls.append("get_account")
        return self._models[0](**ACCT)

    def get_all_positions(self):
        self.calls.append("get_all_positions")
        return [self._models[1](**p) for p in BODIES["/v2/positions"]]

    def get_orders(self, filter=None):  # noqa: A002 - the SDK's parameter name
        self.calls.append("get_orders")
        self.filter = filter
        return [self._models[2](**o) for o in BODIES["/v2/orders"]]

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def trap(*a, **k):
            self.hits.append(name)
            raise AssertionError(f"forbidden TradingClient method touched: {name}")
        return trap


def normalized(account=None, positions=None, orders=None, fills=None):
    return {"account": account if account is not None else {**R._norm_account(SimpleNamespace(**{**ACCT, "status": "ACTIVE"}))},
            "positions": positions if positions is not None else [],
            "orders": orders if orders is not None else [], "fills": fills if fills is not None else []}


class FakeReader:
    """A fake of the adapter's reader: the four reads, optional per-read failure / delay; anything else raises."""

    def __init__(self, data=None, fail=None, delay=0.0, gate=None):
        self.data, self.fail, self.delay, self.gate, self.calls = data or normalized(), dict(fail or {}), delay, gate, []

    def _r(self, name):
        self.calls.append(name)
        if self.gate is not None:
            self.gate.wait(10)
        if self.delay:
            time.sleep(self.delay)
        if name in self.fail:
            raise R.BrokerReadError(*self.fail[name]) if isinstance(self.fail[name], tuple) else R.BrokerReadError(self.fail[name])
        return self.data[name]

    def get_account(self):
        return self._r("account")

    def get_positions(self):
        return self._r("positions")

    def get_recent_orders(self, limit):
        assert limit == R.ORDER_LIMIT
        return self._r("orders")

    def get_recent_fills(self, limit):
        assert limit == R.FILL_LIMIT
        return self._r("fills")

    def __getattr__(self, name):
        raise AssertionError(f"not an approved read: {name}")


@pytest.fixture(autouse=True)
def fresh_state():
    AV._reset()
    FakeTradingClient.instances = []
    yield
    AV._reset()


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv(R.KEY_ENV, KEY)
    monkeypatch.setenv(R.SECRET_ENV, SECRET)


@pytest.fixture
def wire(monkeypatch, creds):
    w = FakeWire()
    monkeypatch.setattr(R, "WIRE", lambda: w)
    return w


@pytest.fixture
def lab(monkeypatch):
    """A local simulator with AMD 10 and CLS 4 (filled Sep 28 at the open) marked at the Sep 28 close."""
    m, lb = T45.market({"2026-09-28": {"AMD": (100, 101), "CLS": (50, 52)}}, syms=("AMD", "MU", "CLS"))
    T45.account(lb, cash="100000")
    T45.order(lb, "2026-09-25", "AMD", "BUY", 10)
    T45.order(lb, "2026-09-25", "CLS", "BUY", 4)
    T45.process(lb, "2026-09-28")
    monkeypatch.setattr(RO, "db_path", lambda: Path(lb.path))
    monkeypatch.setattr("data.market_data.fetch_daily_bars", m.fetch)
    monkeypatch.setattr("data.market_data.get_data_client", lambda: object())
    monkeypatch.setattr(FC, "_utc", lambda now=None: now or FL.at(Dt("2026-09-28")))
    monkeypatch.setattr(FC, "BAR_CACHE", FC.BarCache())
    return lb


@pytest.fixture
def api(monkeypatch, lab):
    """The real app; Claude (both paths), the Robinhood gateway and the real TradingClient all fail fast."""
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
    monkeypatch.setattr(R, "TradingClient", fail("real alpaca TradingClient"))
    c = TestClient(app)
    c.lab, c.hits = lab, hits
    c.budgets = lambda: (usage_tracker.budget(RESEARCH)["used_today"], usage_tracker.budget(EXPLANATION)["used_today"],
                         len(usage_tracker._records))
    return c


def use_reader(monkeypatch, reader):
    made = []
    monkeypatch.setattr(AV, "READER", lambda: made.append(1) or reader)
    return made


def paper_tables(path) -> dict:
    with sqlite3.connect(str(path)) as c:
        return {t: hashlib.sha256(repr(c.execute(f'SELECT rowid, * FROM "{t}" ORDER BY rowid').fetchall()).encode()).hexdigest()
                for t in ("paper_accounts", "paper_orders", "paper_fills", "paper_lots", "paper_lot_closures")}


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ==================================================================================================================================
# credentials, paper-only client, TradingClient isolation
# ==================================================================================================================================

def test_65_credentials_only_from_the_paper_names_no_market_data_fallback(monkeypatch):
    import config
    monkeypatch.setenv("ALPACA_API_KEY", "MARKET_DATA_KEY_SENTINEL")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "MARKET_DATA_SECRET_SENTINEL")
    monkeypatch.setattr(config, "ALPACA_API_KEY", "MARKET_DATA_KEY_SENTINEL")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "MARKET_DATA_SECRET_SENTINEL")
    monkeypatch.setattr(R, "TradingClient", FakeTradingClient)
    assert R.connection_status() == {"configured": False, "missing": [R.KEY_ENV, R.SECRET_ENV],
                                     "credential_names": ["ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY"], "mode": "PAPER"}
    assert R.open_reader() is None and AV.refresh()["connection"]["state"] == "NOT_CONFIGURED"
    monkeypatch.setenv(R.KEY_ENV, KEY)                                           # one of the two is not enough
    assert R.connection_status()["missing"] == [R.SECRET_ENV] and R.open_reader() is None
    assert AV.refresh()["connection"]["state"] == "NOT_CONFIGURED" and FakeTradingClient.instances == []
    src = (ROOT / "paper" / "alpaca_readonly.py").read_text(encoding="utf-8")
    assert re.findall(r"os\.environ\.get\((\w+)\)", src) == ["n", "KEY_ENV", "SECRET_ENV"]   # the loop over the two names
    assert "getenv" not in src and "config.ALPACA" not in src and "has_credentials" not in src
    for f in NEW_PRODUCT:
        s = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"ALPACA_API_KEY|ALPACA_SECRET_KEY|config\.ALPACA|has_credentials", s), f
        if f != "paper/alpaca_readonly.py":
            assert not re.search(r"os\.environ|getenv|ALPACA_PAPER_", s), f                  # only the adapter reads them


def test_5_62_client_is_hard_wired_paper_true_no_override(wire, monkeypatch):
    monkeypatch.setattr(R, "TradingClient", FakeTradingClient)
    AV.refresh()
    (fc,) = FakeTradingClient.instances
    assert fc.init == ((KEY, SECRET), {"paper": True}) and fc._retry == 0
    assert fc._session.get_adapter("https://paper-api.alpaca.markets/v2/account").__class__.__name__ == "_PaperReadTransport"
    import ast
    src = (ROOT / "paper" / "alpaca_readonly.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "TradingClient"]
    assert len(calls) == 1                                                            # the one construction, exactly:
    (call,) = calls
    assert [a.id for a in call.args] == ["key", "secret"]
    assert [(k.arg, getattr(k.value, "value", None)) for k in call.keywords] == [("paper", True)]
    assert "paper=False" not in src and "url_override" not in src and not re.search(r"paper\s*=\s*(?!True)", src)
    for name, fn in inspect.getmembers(R, inspect.isfunction):
        assert "paper" not in inspect.signature(fn).parameters, name
    for name, fn in inspect.getmembers(R.PaperReader, inspect.isfunction):
        assert "paper" not in inspect.signature(fn).parameters and "url" not in inspect.signature(fn).parameters, name
    # test-only look at the SDK: paper=True is what selects the paper host (production never reads this private field)
    from alpaca.common.enums import BaseURL
    from alpaca.trading.client import TradingClient
    assert TradingClient(KEY, SECRET, paper=True)._base_url == BaseURL.TRADING_PAPER == "https://paper-api.alpaca.markets"


def test_59_60_trading_client_only_in_the_one_adapter_everywhere():
    from trading_client_allowlist import TRADING_CLIENT_ADAPTER, TRADING_CLIENT_TEST_FILES, trading_client_allowed
    assert TRADING_CLIENT_ADAPTER == "paper/alpaca_readonly.py"
    real_in_tests = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("*.py")
                           if re.search(r"(?<![\w.])TradingClient\(", p.read_text(encoding="utf-8")))
    assert tuple(real_in_tests) == TRADING_CLIENT_TEST_FILES                           # only this file builds a real one
    importers, constructors = [], []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT).as_posix()
        if p.suffix not in (".py", ".js", ".html") or rel.startswith((".venv/", "tests/", "browser_tests/")) or "__pycache__" in rel:
            continue
        t = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"^\s*(from\s+alpaca\.trading|import\s+alpaca\.trading)", t, re.M):
            importers.append(rel)
        if re.search(r"TradingClient\s*\(", t):
            constructors.append(rel)
        assert not re.search(r"TradingStream|alpaca\.trading\.stream|url_override|TRADING_LIVE|(?<![\w.-])api\.alpaca\.markets", t), rel
    assert importers == constructors == ["paper/alpaca_readonly.py"] and all(trading_client_allowed(r) for r in importers)
    assert not trading_client_allowed("paper/alpaca_view.py") and not trading_client_allowed("paper/execution.py")
    # test infrastructure: only this file (and the harness's guard that makes construction a violation) mention it
    users = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "browser_tests").glob("*.py")
                   if re.search(r"^\s*from\s+alpaca\.trading", p.read_text(encoding="utf-8"), re.M))
    assert users == ["browser_tests/app_server.py"]
    harness = (ROOT / "browser_tests" / "app_server.py").read_text(encoding="utf-8")
    assert 'TradingClient.__init__ = lambda *a, **k: guard.hit("alpaca TradingClient constructed")' in harness


def test_10_12_named_reads_only_no_generic_broker_method():
    assert sorted(n for n in dir(R.PaperReader) if not n.startswith("_")) == ["get_account", "get_positions",
                                                                              "get_recent_fills", "get_recent_orders"]
    public = sorted(n for n, v in vars(R).items() if callable(v) and not n.startswith("_") and not n.isupper()
                    and getattr(v, "__module__", "") == R.__name__)
    assert public == ["BrokerReadError", "PaperReader", "ReadOnlyViolation", "connection_status", "open_reader"]
    src = (ROOT / "paper" / "alpaca_readonly.py").read_text(encoding="utf-8")
    assert not re.search(r"def (call|request|raw_client|get_client|client)\b|getattr\(self\._client|__getattr__", src)
    assert R.READ_PATHS == {"/v2/account": "account", "/v2/positions": "positions", "/v2/orders": "orders",
                            "/v2/account/activities": "fills"}


def test_61_no_write_method_or_write_verb_in_stage_46_product_code():
    for f in NEW_PRODUCT:
        s = (ROOT / f).read_text(encoding="utf-8")
        for m in WRITE_METHODS:
            assert m not in s, (f, m)
        assert not re.search(r"\.(post|put|patch|delete)\(|requests\.(post|put|patch|delete|request)\(|method=\"(POST|PUT|PATCH|DELETE)",
                             s.replace('api("POST", "/api/alpaca-paper/refresh")', "").replace('@router.post("/refresh")', "")), f
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|new Function|pickle|"
                             r"importlib|powershell|TradingStream|websocket|setInterval|setTimeout|threading\.Thread|Scheduler|"
                             r"anthropic|get_provider|from agents|import agents|rh_gateway|robinhood|"
                             r"register_extension|CREATE TABLE|INSERT INTO|UPDATE paper|DELETE FROM", s, re.I), f
        assert not re.search(r"^\s*(from portfolio\b|import portfolio\b)", s, re.M), f       # the brokerage provider package


# ==================================================================================================================================
# the real SDK through the guarded transport (fake wire) — hosts, methods, budget, timeouts, retries, redirects
# ==================================================================================================================================

def test_68_real_sdk_reads_are_four_gets_on_the_paper_host_with_finite_timeouts(wire):
    p = AV.refresh()
    assert p["connection"]["state"] == "CONNECTED" and [s["status"] for s in p["sections"].values()] == ["OK"] * 4
    assert [(r.method, r.host, r.path) for r in wire.seen] == [("GET", "paper-api.alpaca.markets", x) for x in
                                                               ("/v2/account", "/v2/positions", "/v2/orders", "/v2/account/activities")]
    assert all(r.timeout == R.TIMEOUT == (5.0, 10.0) and r.headers["APCA-API-KEY-ID"] == KEY for r in wire.seen)
    assert wire.seen[2].query == {"status": ["all"], "limit": ["100"]}
    assert wire.seen[3].query == {"activity_types": ["FILL"], "direction": ["desc"], "page_size": ["100"]}
    assert [x["symbol"] for x in p["positions"]] == ["AMD", "MU"]                      # alphabetical
    assert p["orders"][0]["id_short"] == "c1111111" and p["fills"][0]["order_ref"] == "c1111111"


def test_68_80_fake_trading_client_only_approved_reads_write_methods_untouched(wire, monkeypatch):
    monkeypatch.setattr(R, "TradingClient", FakeTradingClient)
    for _ in range(3):
        assert AV.refresh()["connection"]["state"] == "CONNECTED"
    assert len(FakeTradingClient.instances) == 3                                       # one reader (budget) per refresh
    for fc in FakeTradingClient.instances:
        assert fc.calls == ["get_account", "get_all_positions", "get_orders"] and fc.hits == []
        assert fc.filter.limit == 100 and fc.filter.status.value == "all"
    assert [r.path for r in wire.seen] == ["/v2/account/activities"] * 3              # the fake client made no HTTP itself
    with pytest.raises(AssertionError, match="forbidden TradingClient method touched: submit_order"):
        FakeTradingClient().submit_order(None)                                        # the trap works


def test_transport_refuses_every_non_read_before_sending(wire):
    t = R._PaperReadTransport(wire)
    s = requests.Session()
    s.mount("https://", t)
    s.mount("http://", t)
    base = "https://paper-api.alpaca.markets"
    for method, url in (("POST", base + "/v2/orders"), ("DELETE", base + "/v2/orders"), ("PATCH", base + "/v2/account/configurations"),
                        ("PUT", base + "/v2/watchlists/x"), ("DELETE", base + "/v2/positions"),
                        ("GET", "https://api.alpaca.markets/v2/account"), ("GET", "http://paper-api.alpaca.markets/v2/account"),
                        ("GET", "https://paper-api.alpaca.markets:8443/v2/account"), ("GET", base + "/v2/orders/abc"),
                        ("GET", base + "/v2/positions/AMD"), ("GET", base + "/v2/account/configurations"),
                        ("GET", "https://data.alpaca.markets/v2/stocks/bars")):
        with pytest.raises(R.ReadOnlyViolation):
            s.request(method, url)
    assert wire.seen == []
    s.get(base + "/v2/account")
    with pytest.raises(R.ReadOnlyViolation, match="at most once"):
        s.get(base + "/v2/account")
    assert len(wire.seen) == 1


def test_80_write_calls_on_the_real_hardened_client_never_leave_the_process(wire):
    from alpaca.trading.requests import MarketOrderRequest
    rd = R.open_reader()
    client = rd._client                                                               # test-only reach into the reader
    for call in (lambda: client.submit_order(MarketOrderRequest(symbol="AMD", qty=1, side="buy", time_in_force="day")),
                 lambda: client.cancel_orders(), lambda: client.close_all_positions(), lambda: client.close_position("AMD"),
                 lambda: client.cancel_order_by_id("c1111111-2222-3333-4444-555555555555")):
        with pytest.raises(R.ReadOnlyViolation):
            call()
    assert wire.seen == []


def test_91_no_sdk_retries_no_redirects_at_most_four_requests(wire):
    wire.status.update({"/v2/account": 429, "/v2/account/activities": 302})
    p = AV.refresh()
    assert [r.path for r in wire.seen] == ["/v2/account", "/v2/positions", "/v2/orders", "/v2/account/activities"]   # 1 each
    assert p["sections"]["account"]["code"] == "SERVER_ERROR" and p["sections"]["account"]["http_status"] == 429
    assert p["sections"]["fills"]["code"] == "UNEXPECTED_RESPONSE" and p["sections"]["fills"]["http_status"] == 302
    assert p["connection"]["state"] == "PARTIAL_DATA" and not any("example.invalid" in r.url for r in wire.seen)
    wire.status.clear()
    wire.seen.clear()
    wire.status["/v2/positions"] = 500
    assert AV.refresh()["connection"]["state"] == "PARTIAL_DATA" and len(wire.seen) == 4


# ==================================================================================================================================
# statuses: not configured, auth, partial, timeout, unavailable
# ==================================================================================================================================

def test_75_missing_credentials_not_configured_and_zero_network(api):
    for r in (api.get("/api/alpaca-paper/status"), api.get("/api/alpaca-paper/view"), api.post("/api/alpaca-paper/refresh")):
        assert r.status_code == 200 and r.json()["connection"]["state"] == "NOT_CONFIGURED"
        assert r.json()["connection"]["configured"] is False and "ALPACA_PAPER_API_KEY" in r.json()["connection"]["message"]
    assert api.hits == []                                                             # no TradingClient, no wire (conftest)
    assert api.get("/api/paper-portfolio").json()["summary"]["cash"]                 # the local simulator keeps working


def test_76_auth_error_no_fallback_no_live_attempt_remaining_reads_skipped(wire, monkeypatch, caplog):
    monkeypatch.setenv("ALPACA_API_KEY", "MARKET_DATA_KEY_SENTINEL")
    wire.status["/v2/account"] = 401
    wire.echo_secret = True
    with caplog.at_level(logging.DEBUG):
        p = AV.refresh()
    assert p["connection"]["state"] == "AUTH_ERROR" and p["connection"]["message"] == "Alpaca Paper authentication failed."
    assert [r.path for r in wire.seen] == ["/v2/account"]                             # stopped: no retry, no other endpoint
    assert {n: s["code"] for n, s in p["sections"].items()} == {"account": "AUTH_FAILED", "positions": "SKIPPED",
                                                                "orders": "SKIPPED", "fills": "SKIPPED"}
    assert all(r.host == R.PAPER_HOST and r.headers["APCA-API-KEY-ID"] == KEY for r in wire.seen)
    text = json.dumps(p) + caplog.text
    assert KEY not in text and SECRET not in text and "MARKET_DATA_KEY_SENTINEL" not in text and "not authorized" not in text
    assert AV.status()["connection"]["state"] == "AUTH_ERROR"


def test_77_partial_data_keeps_every_successful_read(monkeypatch, creds, lab):
    data = normalized(positions=[R._norm_position(SimpleNamespace(**pos("AMD", "10")))], fills=[R._norm_fill(fill_json())])
    rd = FakeReader(data, fail={"orders": ("SERVER_ERROR", 503)})
    use_reader(monkeypatch, rd)
    p = AV.refresh()
    assert p["connection"]["state"] == "PARTIAL_DATA" and rd.calls == ["account", "positions", "orders", "fills"]
    assert p["account"]["cash"] == "25000.25" and [x["symbol"] for x in p["positions"]] == ["AMD"] and len(p["fills"]) == 1
    assert p["orders"] == [] and p["sections"]["orders"] == {"status": "FAILED", "code": "SERVER_ERROR", "http_status": 503,
                                                             "ms": p["sections"]["orders"]["ms"], "count": None}
    assert [w["section"] for w in p["warnings"]] == ["orders"] and p["reconciliation"]["links"]["orders"]["alpaca_count"] is None
    assert p["reconciliation"]["positions"]["available"] is True                     # positions WERE read: compared


def test_78_timeouts_are_clean_statuses_and_connection_failures_stop(wire):
    wire.raise_["/v2/orders"] = requests.exceptions.ReadTimeout("read timed out")
    p = AV.refresh()
    assert p["connection"]["state"] == "PARTIAL_DATA" and p["sections"]["orders"]["code"] == "TIMEOUT" and len(wire.seen) == 4
    assert any(w["code"] == "TIMEOUT" and "timed out" in w["text"] for w in p["warnings"])
    wire.seen.clear()
    wire.raise_ = {"/v2/account": requests.exceptions.ConnectTimeout("connect timed out")}
    p = AV.refresh()
    assert p["connection"]["state"] == "PAPER_API_UNAVAILABLE" and [r.path for r in wire.seen] == ["/v2/account"]
    wire.seen.clear()
    wire.raise_ = {"/v2/account": requests.exceptions.ConnectionError("dns")}
    assert AV.refresh()["connection"]["state"] == "PAPER_API_UNAVAILABLE" and len(wire.seen) == 1


def test_78_app_stays_responsive_while_a_read_is_stalled(api, monkeypatch, creds):
    gate = threading.Event()
    use_reader(monkeypatch, FakeReader(gate=gate))
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", api.post("/api/alpaca-paper/refresh")))
    th.start()
    time.sleep(0.3)
    t0 = time.perf_counter()
    st = api.get("/api/alpaca-paper/status").json()
    assert st["connection"]["state"] == "REFRESHING" and st["in_flight"] is True and time.perf_counter() - t0 < 2
    assert api.get("/api/paper-portfolio").status_code == 200                        # the rest of the app answers
    gate.set()
    th.join(10)
    assert out["r"].json()["connection"]["state"] == "CONNECTED"


def test_reader_that_cannot_be_built_is_an_error_not_a_crash(monkeypatch, creds, lab):
    def broken():
        raise R.ReadOnlyViolation("unexpected alpaca-py client layout")
    monkeypatch.setattr(AV, "READER", broken)
    p = AV.refresh()
    assert p["connection"]["state"] == "ERROR" and {s["code"] for s in p["sections"].values()} == {"READER_UNAVAILABLE"}


def test_84_double_refresh_is_one_in_flight_read(monkeypatch, creds, lab):
    rd = FakeReader(delay=0.3)
    made = use_reader(monkeypatch, rd)
    res = []
    ths = [threading.Thread(target=lambda: res.append(AV.refresh())) for _ in range(2)]
    for t in ths:
        t.start()
        time.sleep(0.05)
    for t in ths:
        t.join(10)
    assert len(made) == 1 and rd.calls == ["account", "positions", "orders", "fills"]  # 4 reads, not 8
    assert sorted(r["joined"] for r in res) == [False, True] and res[0]["refreshed_at"] == res[1]["refreshed_at"]


# ==================================================================================================================================
# reconciliation semantics
# ==================================================================================================================================

def local_pos(sym, shares, avg="100.0000", mark="101.0000"):
    return {"symbol": sym, "shares": shares, "average_cost": avg, "mark": mark, "market_value": "1.00", "unrealized_pnl": "0.10"}


def broker_pos(sym, qty, **k):
    return R._norm_position(SimpleNamespace(**pos(sym, qty, **k)))


def test_69_match_local_only_alpaca_only_alphabetical():
    r = RC.positions([local_pos("AMD", 10), local_pos("CLS", 4)], [broker_pos("MU", "5"), broker_pos("AMD", "10")])
    assert [(x["symbol"], x["status"]) for x in r["rows"]] == [("AMD", "MATCH"), ("CLS", "LOCAL_ONLY"), ("MU", "ALPACA_ONLY")]
    assert [(x["local_shares"], x["alpaca_shares"], x["share_delta"]) for x in r["rows"]] == [("10", "10", "0"), ("4", None, "-4"),
                                                                                              (None, "5", "5")]
    assert r["summary"] == {"local_positions": 2, "alpaca_positions": 2, "quantity_matches": 1, "quantity_differences": 0,
                            "local_only": 1, "alpaca_only": 1}


def test_70_different_quantity_delta_is_alpaca_minus_local_no_recommendation():
    r = RC.positions([local_pos("AMD", 10)], [broker_pos("AMD", "8")])
    (row,) = r["rows"]
    assert row["status"] == "DIFFERENT" and row["share_delta"] == "-2" and "Alpaca paper shares − local simulated shares" in r["delta_convention"]
    frac = RC.positions([local_pos("AMD", 10)], [broker_pos("AMD", "10.5")])["rows"][0]
    assert frac["status"] == "DIFFERENT" and frac["share_delta"] == "0.5"
    short = RC.positions([], [broker_pos("TSLA", "-3", side="short")])["rows"][0]
    assert short["alpaca_shares"] == "-3" and short["status"] == "ALPACA_ONLY"
    text = json.dumps(r).lower()
    assert not re.search(r"\b(good|bad|better|worse|should|recommend\w*|fix|sync|buy|sell|score|confidence)\b", text), text


def test_positions_are_never_categorised_when_the_broker_read_failed():
    r = RC.positions([local_pos("AMD", 10)], None)
    assert r["available"] is False and r["rows"] == [] and r["summary"] is None     # never "LOCAL_ONLY" for an unread account
    closed = RC.positions([{**local_pos("AMD", 0), "realized_pnl": "5.00"}], [])       # a closed local position is not held
    assert closed["rows"] == [] and closed["summary"]["local_positions"] == 0


def test_71_independent_balances_shown_separately_not_an_error(monkeypatch, creds, lab):
    use_reader(monkeypatch, FakeReader())
    p = AV.refresh()
    a = p["reconciliation"]["account"]
    assert p["connection"]["state"] == "CONNECTED" and a["relationship"] == "INDEPENDENT"
    assert a["local"]["starting_cash"] == "100000.00" and a["local"]["cash"] == P.view()["summary"]["cash"] and a["alpaca"]["cash"] == "25000.25"
    assert "independent paper accounts" in a["note"] and "not an error" in a["note"]
    assert a["differences"]["cash"] == str((Decimal("25000.25") - Decimal(a["local"]["cash"])).quantize(Decimal("0.01")))
    assert not re.search(r"(?i)\b(error|mismatch|wrong|incorrect)\b", json.dumps(a).replace("not an error", ""))
    assert p["warnings"] == []


def test_72_price_timing_not_directly_comparable_no_pnl_verdict(monkeypatch, creds, lab):
    use_reader(monkeypatch, FakeReader(normalized(positions=[broker_pos("AMD", "10", avg="100", price="187.5")])))
    p = AV.refresh()
    (amd,) = [x for x in p["reconciliation"]["positions"]["rows"] if x["symbol"] == "AMD"]
    assert amd["status"] == "MATCH" and amd["value_comparison"] == "NOT_DIRECTLY_COMPARABLE"
    assert amd["local_mark"] == "101.0000" and amd["alpaca_current_price"] == "187.5"
    vals = p["reconciliation"]["positions"]["values"]
    assert vals["status"] == "NOT_DIRECTLY_COMPARABLE" and vals["local_basis"] == {"kind": "COMPLETED_DAILY_CLOSE", "session": "2026-09-28"}
    assert vals["alpaca_basis"] == {"kind": "BROKER_REFRESH", "as_of": p["refreshed_at"]}
    assert not [k for row in p["reconciliation"]["positions"]["rows"] for k in row if re.search(r"(pnl|value|price).*(delta|diff)|(delta|diff).*(pnl|value|price)", k)]


def test_73_74_similar_orders_and_fills_stay_not_linked(monkeypatch, creds, lab):
    o = R._norm_order(SimpleNamespace(**{**order_json("AMD", "10"), "id": "c1111111-2222", "type": None, "order_type": "market",
                                         "notional": None}))
    use_reader(monkeypatch, FakeReader(normalized(positions=[broker_pos("AMD", "10")], orders=[o], fills=[R._norm_fill(fill_json("AMD", "10"))])))
    p = AV.refresh()
    lk = p["reconciliation"]["links"]
    assert lk["orders"] == {"status": "NOT_LINKED", "linked": 0, "local_count": 2, "alpaca_count": 1}
    assert lk["fills"] == {"status": "NOT_LINKED", "linked": 0, "local_count": 2, "alpaca_count": 1}
    assert [x["link"] for x in p["orders"] + p["fills"]] == ["NOT_LINKED", "NOT_LINKED"]
    assert p["reconciliation"]["summary"]["linked_orders"] == p["reconciliation"]["summary"]["linked_fills"] == 0
    def keys(x):
        return ([k for k, v in x.items() for k in [k, *keys(v)]] if isinstance(x, dict) else
                [k for v in x for k in keys(v)] if isinstance(x, list) else [])
    assert not [k for k in keys(p) if re.search(r"(?i)pair|match_id|linked_(order|fill)_id|local_order_id|broker_order_id", k)]
    src = (ROOT / "paper" / "reconcile.py").read_text(encoding="utf-8")
    assert "NOT_LINKED" in src and not re.search(r"for .* in .*orders.*:\s*\n.*symbol.*==", src)   # no heuristic pairing loop


def test_35_summary_counts_deterministic(monkeypatch, creds, lab):
    use_reader(monkeypatch, FakeReader(normalized(positions=[broker_pos("AMD", "10"), broker_pos("MU", "5")])))
    p = AV.refresh()
    assert p["reconciliation"]["summary"] == {"local_positions": 2, "alpaca_positions": 2, "quantity_matches": 1,
                                              "quantity_differences": 0, "local_only": 1, "alpaca_only": 1,
                                              "linked_orders": 0, "linked_fills": 0}
    assert [(x["symbol"], x["local_shares"], x["status"]) for x in p["positions"]] == [("AMD", "10", "MATCH"), ("MU", None, "ALPACA_ONLY")]
    assert not re.search(r"(?i)score|confidence|percent_match|probability", json.dumps(p["reconciliation"]))


# ==================================================================================================================================
# no writes, no persistence, secrets, masking
# ==================================================================================================================================

def test_41_42_79_refresh_and_view_write_nothing(monkeypatch, creds, lab):
    use_reader(monkeypatch, FakeReader(normalized(positions=[broker_pos("AMD", "8")])))
    t0, d0 = paper_tables(lab.path), digest(lab.path)
    with sqlite3.connect(lab.path) as c:
        schema0 = sorted(c.execute("SELECT type, name, sql FROM sqlite_master"))
    local0 = json.dumps(P.view(), sort_keys=True)
    for _ in range(5):
        AV.refresh()
        AV.view()
        AV.status()
    assert paper_tables(lab.path) == t0 and digest(lab.path) == d0 and json.dumps(P.view(), sort_keys=True) == local0
    with sqlite3.connect(lab.path) as c:
        assert sorted(c.execute("SELECT type, name, sql FROM sqlite_master")) == schema0
    assert not list((ROOT / "database").glob("*alpaca*")) and not list((ROOT / "database").glob("*broker*"))


def test_66_fake_credentials_never_appear_anywhere(api, wire, caplog, monkeypatch):
    monkeypatch.setattr(R, "TradingClient", __import__("alpaca.trading.client", fromlist=["TradingClient"]).TradingClient)
    with caplog.at_level(logging.DEBUG):
        outs = [api.post("/api/alpaca-paper/refresh").text, api.get("/api/alpaca-paper/view").text, api.get("/api/alpaca-paper/status").text]
        wire.status["/v2/account"], wire.echo_secret = 401, True
        outs.append(api.post("/api/alpaca-paper/refresh").text)
        err = R.BrokerReadError("AUTH_FAILED", 401)
        outs += [str(err), repr(err), repr(R.open_reader()), caplog.text]
    for url in ("/.env", "/../.env", "/%2e%2e/.env", "/frontend/../.env", "/..%2f.env", "/api/alpaca-paper/../../.env"):
        r = api.get(url)                                                              # the browser can never read .env
        assert r.status_code in (404, 405, 422) and "ALPACA" not in r.text, url
    html = api.get("/").text + (ROOT / "frontend" / "alpaca_paper.js").read_text(encoding="utf-8")
    assert any(r.headers.get("APCA-API-SECRET-KEY") == SECRET for r in wire.seen)   # it WAS used — only on the wire
    for text in outs + [html]:
        assert KEY not in text and SECRET not in text and "APCA-API" not in text


def test_67_account_number_is_masked_everywhere(monkeypatch, api, wire, caplog):
    monkeypatch.setattr(R, "TradingClient", __import__("alpaca.trading.client", fromlist=["TradingClient"]).TradingClient)
    with caplog.at_level(logging.DEBUG):
        body = api.post("/api/alpaca-paper/refresh").json()
        view = api.get("/api/alpaca-paper/view").text
    assert body["account"]["account_number_masked"] == "••••7890"
    assert FULL_ACCOUNT not in json.dumps(body) + view + caplog.text and ACCT["id"] not in json.dumps(body)
    assert R._mask_account_number("") is None and R._mask_account_number("1234") == "••••"


# ==================================================================================================================================
# API strictness, no action routes, no AI, no Robinhood, no automatic call
# ==================================================================================================================================

def test_44_46_47_exact_routes_and_strict_requests(api):
    from api.server import app
    from paper_endpoints import ALPACA_PAPER_ENDPOINTS
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if ("alpaca" in p or "broker" in p) and not p.startswith("/api/alpaca-paper-orders")}
    assert mine == ALPACA_PAPER_ENDPOINTS == {"/api/alpaca-paper/status": {"get"}, "/api/alpaca-paper/refresh": {"post"},
                                              "/api/alpaca-paper/view": {"get"}}
    for p in mine:
        assert not re.search(r"(?i)submit|place|trade|cancel|replace|close|liquidate|exercise|order|sync|import", p), p
    for bad in ({"api_key": "x"}, {"secret_key": "x"}, {"paper": False}, {"base_url": "https://api.alpaca.markets"},
                {"endpoint": "/v2/orders"}, {"symbol": "AMD"}, {"method": "POST"}, {"path": "/v2/orders"}, {"live": True}):
        assert api.post("/api/alpaca-paper/refresh", json=bad).status_code == 422, bad
    assert api.post("/api/alpaca-paper/refresh", json=[1]).status_code == 422
    for url in ("/api/alpaca-paper/status?paper=false", "/api/alpaca-paper/view?base_url=x"):
        assert api.get(url).status_code == 422, url
    assert api.post("/api/alpaca-paper/refresh?paper=false").status_code == 422
    assert api.post("/api/alpaca-paper/refresh", json={}).status_code == 200 and api.post("/api/alpaca-paper/refresh").status_code == 200
    for m in ("put", "patch", "delete"):
        assert getattr(api, m)("/api/alpaca-paper/refresh").status_code == 405


def test_81_82_no_ai_no_robinhood_budgets_unchanged(api, monkeypatch, creds):
    use_reader(monkeypatch, FakeReader(normalized(positions=[broker_pos("AMD", "10")])))
    b0 = api.budgets()
    assert api.post("/api/alpaca-paper/refresh").json()["connection"]["state"] == "CONNECTED"
    api.get("/api/alpaca-paper/view")
    assert api.hits == [] and api.budgets() == b0


def test_83_no_broker_call_on_startup_open_or_view(monkeypatch, creds, lab):
    import config
    made = use_reader(monkeypatch, FakeReader())
    monkeypatch.setattr(config, "EVENT_WARMUP_ON_STARTUP", False)
    from api.server import app
    with TestClient(app) as c:                                                        # runs the real startup / shutdown
        for url in ("/api/alpaca-paper/status", "/api/alpaca-paper/view", "/api/paper-portfolio", "/api/paper-orders",
                    "/api/paper-fills", "/api/alpaca-paper/view"):
            assert c.get(url).status_code == 200, url
        assert made == [] and c.get("/api/alpaca-paper/status").json()["connection"]["state"] == "READY"
        c.post("/api/alpaca-paper/refresh")
        assert made == [1]
    for f in ("api/server.py", "forward/automation.py", "notifications/delivery.py", "brief/daily.py", "paper/portfolio.py",
              "paper/execution.py", "api/routes/paper.py", "frontend/paper_portfolio.js", "frontend/app.js"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"alpaca_view|alpaca_readonly|AV\.refresh|alpaca-paper/refresh", src), f
    js = (ROOT / "frontend" / "alpaca_paper.js").read_text(encoding="utf-8")
    assert js.count("/api/alpaca-paper/refresh") == 1 and js.count("fetch(") == 1 and "/api/alpaca-paper/view" in js
    refresh_body = js.split("async function refresh()")[1].split("function onClick")[0]
    assert '"/api/alpaca-paper/refresh"' in refresh_body and js.count("refresh();") == 1 and 'b.dataset.apx === "refresh"' in js


def test_85_ui_race_rules_and_no_action_controls():
    js = (ROOT / "frontend" / "alpaca_paper.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert "if (refreshing) return;" in js and "fresher(r.body)" in js and "const rank = (b) => [b.refreshed_at" in js
    assert 'el.innerHTML = `<div class="apx">' in js and "document.querySelector(" not in js      # draws only into its own pane
    assert re.findall(r'data-apx="(\w+)"', js) == ["refresh"]                                    # the only control
    words = re.sub(r"\.replace\(", "(", code)                                        # the string method is not a control
    assert not re.search(r"(?i)\b(execute|trade|send order|sync account|fix mismatch|use alpaca position|cancel|replace|liquidate)\b", words)
    assert not re.search(r"(?i)<button[^>]*>(Close|Cancel|Replace|Buy|Sell)", js)
    for s in ("READ ONLY · PAPER ACCOUNT · NO BROKER ACTIONS", "Refresh Alpaca Paper", "LOCAL SIMULATOR vs ALPACA PAPER", "NOT LINKED",
              "NOT DIRECTLY COMPARABLE", "INDEPENDENT", "BROKER DATA REFRESHED AT", "Local simulated shares", "Difference status"):
        assert s in js, s
    ppf = (ROOT / "frontend" / "paper_portfolio.js").read_text(encoding="utf-8")
    assert 'data-ppf-view="local" aria-pressed="true">Local Simulator' in ppf and 'data-ppf-view="alpaca"' in ppf
    assert "if (v === \"alpaca\") window.AlpacaPaper.show(ax);" in ppf
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert html.index("paper_portfolio.js") < html.index("alpaca_paper.js") and "alpaca_paper.css" in html


def test_97_stage_45_payload_unchanged_without_a_refresh(api):
    before = api.get("/api/paper-portfolio").json()
    api.get("/api/alpaca-paper/status")
    api.get("/api/alpaca-paper/view")
    assert api.get("/api/paper-portfolio").json() == before
    assert set(before) == {"label", "note", "fifo_text", "mark_text", "setup", "account", "summary", "positions", "pending_orders",
                           "closed_orders", "fills", "counts"}


def test_100_documentation():
    doc = (ROOT / "paper" / "ALPACA_PAPER.md").read_text(encoding="utf-8")
    for s in ("ALPACA_PAPER_API_KEY", "ALPACA_PAPER_SECRET_KEY", "paper=True", "GET /v2/account/activities", "paper-api.alpaca.markets",
              "NOT_LINKED", "NOT_DIRECTLY_COMPARABLE", "at most 4", "no fallback", "Refresh Alpaca Paper"):
        assert s in doc, s
    assert not re.search(r"PK[A-Z0-9]{10,}|AK[A-Z0-9]{10,}", doc)
