"""
browser_tests/app_server.py — the REAL FastAPI app on a fresh scratch database, every external dependency replaced.

  * network guard      socket.create_connection / getaddrinfo allow ONLY this harness's own server and browser-control
                       ports on 127.0.0.1; anything else (Anthropic, Alpaca, Robinhood gateway on :8787, FRED, ...) is
                       recorded as a violation and fails the run
  * market data        tests/fw_fixtures.Market (fixed seed) behind data.market_data / the scanner
  * Claude             a deterministic fake for Stage 3.8 explanations only (grounded replies from the payload); any
                       other AI provider request is a violation
  * broker             tests/pf_fixtures fake gateway (read-only GETs, recorded)
  * Alpaca orders      Stage 4.6B: tests/alpaca_order_fakes.FakeAlpaca behind BOTH 4.6B wires (paper reads + the POST
                       writer) — an in-memory fake broker, never a socket; every request it receives is recorded
  * Alpaca paper       Stage 4.6A: a fake read-only paper account behind paper.alpaca_view.READER (fake credentials
                       TEST_KEY_123 / TEST_SECRET_456 only); the real TradingClient and the adapter's real network wire
                       are violations, and paper-api.alpaca.markets is blocked by the network guard like every host
  * clock              fixed: Fri Oct 9, 2026 00:30 ET for Strategy Fit, the forward journal and the Stage 3.7 scheduler
                       (whose background loop is parked; the harness uses "Check automation now")
  * database           STOCK_AGENT_ENV=browser_test and a temporary directory; refuses to run on the real database
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TESTS = ROOT / "tests"            # the repository's test fixtures (fw_fixtures, bt_fixtures, pf_fixtures, test_explain_38)
REAL_DB = ROOT / "data" / "stock_agent.db"
NOW: list = [None]                # the harness clock: fixed (world.CLOCK) except where the Stage 4.1 flow moves it forward


class Guard:
    """Records every blocked external call; a non-empty list fails the run immediately."""

    def __init__(self):
        self.violations, self.allowed_ports = [], set()
        self._cc, self._gai = socket.create_connection, socket.getaddrinfo

    def hit(self, what: str):
        self.violations.append(what)
        raise RuntimeError(f"browser harness blocked an external call: {what}")

    def install(self):
        def create_connection(address, *a, **k):
            host, port = address[0], int(address[1])
            if host in ("127.0.0.1", "localhost") and port in self.allowed_ports:
                return self._cc(address, *a, **k)
            self.hit(f"network connection to {host}:{port}")

        def getaddrinfo(host, port, *a, **k):
            p = int(port) if str(port or "").isdigit() else None
            if host in ("127.0.0.1", "localhost", None) and (p is None or p in self.allowed_ports or p == 0):
                return self._gai(host, port, *a, **k)
            self.hit(f"address lookup for {host}:{port}")
        socket.create_connection, socket.getaddrinfo = create_connection, getaddrinfo

    def uninstall(self):
        socket.create_connection, socket.getaddrinfo = self._cc, self._gai


def rebind(fakes: dict) -> None:
    """Replace functions everywhere they are referenced — also in modules that imported them by name."""
    by_id = {id(k): v for k, v in fakes.items()}
    for mod in list(sys.modules.values()):
        for name, val in list(getattr(mod, "__dict__", {}).items()):
            if id(val) in by_id and callable(val):
                setattr(mod, name, by_id[id(val)])


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeAI:
    """Controls for the fake Stage 3.8 provider: per-symbol delay, failure switch, fixed reply overrides, call log."""

    def __init__(self):
        self.delay, self.fail, self.calls, self.override = {}, False, [], {"fit": None, "evidence": None}
        self.scan_delay = 0.0                                  # Stage 4.0: a slow scan, for the strategy-switch race
        self.brief_delay = 0.0                                 # Stage 4.2: a slow brief read, for the session-switch race
        self.notify_calls, self.notify_fail = [], False         # Stage 4.3: the fake desktop notifier (never a real one)
        self.opened, self.registry = [], None                    # Stage 4.4: fake browser opener + in-memory identity
        self.alpaca = None                                       # Stage 4.6A: the fake read-only Alpaca paper account
        self.orders, self.orders_offset = None, [0.0]            # Stage 4.6B: the fake paper broker + order-clock offset (s)

    def reset(self):
        self.delay, self.fail, self.override = {}, False, {"fit": None, "evidence": None}


def prepare(tmp: Path, guard: Guard):
    """Set up the environment, build the world, patch the app. Returns (app, world, fake_ai, broker_http)."""
    db = (Path(tmp) / "stock_agent.db").resolve()           # refuse before anything else changes
    if db == REAL_DB.resolve() or db.exists():
        raise RuntimeError("the browser harness only runs on a NEW scratch database, never on an existing one")
    os.environ["STOCK_AGENT_ENV"] = "browser_test"
    os.environ["PORTFOLIO_AWARENESS_ENABLED"] = "false"
    os.environ["EVENT_WARMUP_ON_STARTUP"] = "false"
    for p in (str(TESTS), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    guard.install()
    import database.database as dbm
    dbm._db_instance = dbm.ResearchDatabase(db)
    assert Path(dbm.get_db().db_path).resolve() == db

    from browser_tests import world as W                  # noqa: E402 - after the database singleton is set
    wd = W.build(db)

    import config
    config.EVENT_WARMUP_ON_STARTUP = False
    config.PORTFOLIO_AWARENESS_ENABLED = False
    config.ANTHROPIC_API_KEY = "browser-harness-fake-key"            # makes explanations "configured"; never sent anywhere
    config.AI_EXPLANATION_HOURLY_LIMIT, config.AI_EXPLANATION_DAILY_LIMIT = 100, 300   # the harness makes ~15 fake calls

    import data.events.corporate as corp
    import data.market_data as md
    import data.news as news
    import scanner.market_scanner  # noqa: F401 - loaded so its by-name imports are rebound below
    import scanner.watchlist as wl
    import services.event_context as sec
    from ai_explain import service as AX
    from api.routes import portfolio as pr
    from api.server import app
    from fit import current as FC
    from forward import automation as A
    from forward import capture as C
    from forward import journal as J
    from pf_fixtures import FakeGatewayHttp, fake_provider
    import test_explain_38 as T38

    config.FALLBACK_UNIVERSE = list(W.UNIVERSE)            # the scanner's fixed universe: the world's symbols only

    def no_screener():
        raise RuntimeError("no screener in the browser harness")     # the scanner falls back to its fixed universe

    from backtest.replay import ReplayClient
    real_fetch = md.fetch_daily_bars

    def fetch(client, symbols, lookback_days=config.DAILY_BAR_LOOKBACK_DAYS):   # the real signature
        if isinstance(client, ReplayClient):               # point-in-time replay of stored bars (Stage 3.2-3.4 snapshots):
            return real_fetch(client, symbols, lookback_days)   # the real function, which reads only the replay — no network
        return wd.market.fetch(client, symbols, lookback_days)  # every live call: synthetic bars
    watch = ["AMD", "NVDA", "MU"]                          # in memory: the repository's watchlist.txt is never read or written

    def add(symbol, path=None):
        s = symbol.strip().upper()
        if s and s not in watch:
            watch.append(s)
        return list(watch)

    def remove(symbol, path=None):
        s = symbol.strip().upper()
        if s in watch:
            watch.remove(s)
        return list(watch)
    rebind({md.fetch_daily_bars: fetch, md.get_data_client: lambda: object(), md.get_screener_client: no_screener,
            news.get_recent_news: lambda *a, **k: [], corp.get_company_events: lambda *a, **k: [],
            wl.load_watchlist: lambda *a, **k: list(watch), wl.add_to_watchlist: add, wl.remove_from_watchlist: remove})
    sec.build_event_context = wd.events.build
    C.provider_coverage = wd.events.cov
    broker = FakeGatewayHttp()
    pr.provider_factory = lambda: fake_provider(broker)

    NOW[0] = W.CLOCK
    FC._utc = lambda now=None: (now or NOW[0]).astimezone(W.CLOCK.tzinfo)
    FC.BAR_CACHE = FC.BarCache()
    J._now = lambda now=None: (now or NOW[0]).astimezone(W.CLOCK.tzinfo)
    A._scheduler = A.Scheduler(clock=lambda: NOW[0], startup_delay_s=86400)    # background loop parked for the run

    from alpaca.trading.client import TradingClient       # alpaca-py loads this module itself; constructing one is a violation
    TradingClient.__init__ = lambda *a, **k: guard.hit("alpaca TradingClient constructed")
    for name, mod in list(sys.modules.items()):           # every AI path except Stage 3.8 explanations is a violation
        if name.startswith(("agents.", "insights.", "portfolio.")) and hasattr(mod, "get_provider"):
            setattr(mod, "get_provider", lambda _n=name: guard.hit(f"AI provider requested by {_n}"))
    ai = FakeAI()

    class HarnessFake(T38.Fake):
        def analyze(self, prompt, system_prompt, max_tokens=1400):
            p = T38.payload_of(prompt)
            kind = "fit" if "STRATEGY_FIT" in prompt else "evidence"
            sym = (p.get("grounded_in") or {}).get("symbol") or "EVIDENCE"
            ai.calls.append(sym)
            self.delay = ai.delay.get(sym, 0.0)
            self.fail = RuntimeError("provider down (harness)") if ai.fail else None
            self.reply = ai.override[kind] if ai.override[kind] is not None else default
            return super().analyze(prompt, system_prompt, max_tokens)

    def default(s, pr_):
        return T38.grounded_fit_reply(s, pr_) if "STRATEGY_FIT" in pr_ else T38.grounded_evidence_reply(s, pr_)
    fake = HarnessFake(default)
    AX.get_provider = lambda: fake
    from fit import scanner as SC
    real_scan = SC.scan

    def slow_scan(*a, **k):                                # the route calls SC.scan: the harness can make it slow
        if ai.scan_delay:
            time.sleep(ai.scan_delay)
        return real_scan(*a, **k)
    SC.scan = slow_scan
    from brief import daily as DBF
    real_brief = DBF.build

    def slow_brief(*a, **k):                               # the route calls DBF.build: the harness can make it slow
        if ai.brief_delay:
            time.sleep(ai.brief_delay)
        return real_brief(*a, **k)
    DBF.build = slow_brief
    from notifications import delivery as DLV
    from notifications import windows as NW
    NW.send = lambda *a, **k: guard.hit("real OS desktop notification adapter invoked")    # never a real notification

    class FakeNotifier:
        def send(self, title, body):
            ai.notify_calls.append((title, body))
            return {"status": "FAILED", "error_code": "HARNESS_OS_FAILURE"} if ai.notify_fail else {"status": "DELIVERED", "error_code": None}
    DLV.ADAPTER = FakeNotifier()
    import webbrowser
    from notifications import identity as NID
    webbrowser.open = lambda *a, **k: guard.hit("real browser opened by the harness")
    NW.start = lambda *a, **k: guard.hit("real OS desktop notification adapter started")

    class MemoryRegistry:                                  # the per-user identity, never the real registry
        value = None

        def get(self):
            return self.value

        def put(self):
            self.value = NID.DISPLAY_NAME

        def remove(self):
            self.value = None
    ai.registry = NID.BACKEND = MemoryRegistry()
    NW.OPENER = ai.opened.append                           # a notification click "opens" this URL (the flow follows it)
    from paper import alpaca_readonly as APR
    from paper import alpaca_view as APV
    for name in (APR.KEY_ENV, APR.SECRET_ENV):             # never the user's real paper keys: the flow sets fake ones
        os.environ.pop(name, None)
    APR.WIRE = lambda: guard.hit("real Alpaca paper network adapter requested")
    ai.alpaca = FakeAlpacaPaper()
    APV.READER = ai.alpaca.reader
    APV._utcnow = lambda: NOW[0]                           # "refreshed at" on the harness clock, like the local simulator
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from alpaca_order_fakes import FakeAlpaca              # Stage 4.6B: in-memory fake paper broker (reads + POST /v2/orders)
    from paper import alpaca_order_reads as APOR
    from paper import alpaca_order_writer as APOW
    from paper import alpaca_orders as APO
    ai.orders = FakeAlpaca()
    APOR.WIRE = APOW.WIRE = lambda: ai.orders
    APO._sleep = lambda seconds: None                      # the automatic 2 s lookup delay is not waited for
    APO._now = lambda: _dt.now(_tz.utc) + _td(seconds=ai.orders_offset[0])   # real time + a test offset (30 s rule)
    from api.routes import portfolio_rotation as PRT       # Stage 4.7: the harness clock; snapshots start empty (0 requests)
    PRT.NOW_FN = lambda: NOW[0]
    PRT._SNAPSHOTS.clear()
    return app, wd, ai, broker


class FakeAlpacaPaper:
    """Stage 4.6A: a fake Alpaca PAPER account with the adapter's four reads (built through the adapter's own
    normalisation from alpaca-py models). Controls: per-read failure, delay. Anything but the four reads is a violation."""
    ACCOUNT_NUMBER = "PA3HARNESS7890"

    def __init__(self):
        self.made, self.reads, self.hits, self.fail, self.delay = 0, [], [], {}, 0.0

    def data(self):
        from alpaca.trading.models import Order, Position, TradeAccount

        def pos(sym, qty, avg, price):
            q = float(qty)
            return Position(asset_id="a1111111-2222-3333-4444-555555555555", symbol=sym, exchange="NYSE", asset_class="us_equity",
                            avg_entry_price=avg, qty=qty, side="long", cost_basis=str(round(q * float(avg), 2)),
                            market_value=str(round(q * float(price), 2)), current_price=price,
                            unrealized_pl=str(round(q * (float(price) - float(avg)), 2)), unrealized_plpc="0.0123")

        def order(oid, sym, qty, when):
            return Order(id=oid, client_order_id=f"harness-{sym.lower()}-1", created_at=when, updated_at=when, submitted_at=when,
                         filled_at=when, symbol=sym, asset_class="us_equity", qty=qty, filled_qty=qty, filled_avg_price="100.25",
                         order_class="simple", order_type="market", type="market", side="buy", time_in_force="day", status="filled",
                         extended_hours=False)
        acct = TradeAccount(id="11111111-2222-3333-4444-555555555555", account_number=self.ACCOUNT_NUMBER, status="ACTIVE",
                            currency="USD", cash="25000.25", equity="26837.75", portfolio_value="26837.75", buying_power="51838",
                            long_market_value="1837.5", short_market_value="0", trading_blocked=False, account_blocked=False,
                            created_at="2026-01-05T15:00:00Z")
        return {"account": acct, "positions": [pos("MU", "5", "95.1", "100"), pos("AMD", "10", "150", "160"), pos("KO", "8", "60", "61.5")],
                "orders": [order("c2222222-2222-3333-4444-555555555555", "AMD", "10", "2026-10-26T13:31:00Z"),
                           order("c3333333-2222-3333-4444-555555555555", "KO", "8", "2026-10-22T13:31:00Z")],
                "fills": [{"activity_type": "FILL", "transaction_time": "2026-10-26T13:31:01Z", "type": "fill", "price": "100.25",
                           "qty": "10", "side": "buy", "symbol": "AMD", "order_id": "c2222222-2222-3333-4444-555555555555"},
                          {"activity_type": "FILL", "transaction_time": "2026-10-22T13:31:01Z", "type": "fill", "price": "60",
                           "qty": "8", "side": "buy", "symbol": "KO", "order_id": "c3333333-2222-3333-4444-555555555555"}]}

    def reader(self):
        from paper import alpaca_readonly as APR
        fake = self
        self.made += 1

        class Reader:
            def _r(self, name, fn):
                fake.reads.append(name)
                if fake.delay:
                    time.sleep(fake.delay)
                if name in fake.fail:
                    raise APR.BrokerReadError(*fake.fail[name])
                return fn(fake.data())

            def get_account(self):
                return self._r("account", lambda d: APR._norm_account(d["account"]))

            def get_positions(self):
                return self._r("positions", lambda d: sorted((APR._norm_position(p) for p in d["positions"]), key=lambda p: p["symbol"]))

            def get_recent_orders(self, limit):
                return self._r("orders", lambda d: [APR._norm_order(o) for o in d["orders"]][:limit])

            def get_recent_fills(self, limit):
                return self._r("fills", lambda d: [APR._norm_fill(f) for f in d["fills"]][:limit])

            def __getattr__(self, name):
                fake.hits.append(name)
                raise AssertionError(f"not an approved Alpaca paper read: {name}")
        return Reader()


class Server:
    """uvicorn in a daemon thread; stop() always returns within the timeout."""

    def __init__(self, app, port: int):
        import uvicorn
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, name="browser-harness-server", daemon=True)

    def start(self, timeout: float = 60.0):
        self.thread.start()
        end = time.time() + timeout
        while not self.server.started:
            if time.time() > end or not self.thread.is_alive():
                raise RuntimeError("the harness server did not start")
            time.sleep(0.05)

    def stop(self, timeout: float = 20.0):
        self.server.should_exit = True
        self.thread.join(timeout)
        return not self.thread.is_alive()
