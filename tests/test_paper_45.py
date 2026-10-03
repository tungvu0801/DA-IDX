"""Stage 4.5 — the LOCAL deterministic paper portfolio (paper/): accounts, market orders filled at the next valid session
open from daily bars, FIFO lots, exact Decimal accounting, pending / cancel / reject, idempotent fills, immutable records.
Synthetic bars only; no broker, no AI, no network."""
import hashlib
import random
import re
import socket
import sqlite3
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fw_fixtures as FL
from backtest import bars as B
from fit import current as FC
from fit import readonly as RO
from paper import accounting as A
from paper import execution as X
from paper import portfolio as P
from paper.store import PaperStore

ROOT = Path(__file__).resolve().parents[1]
Dt = date.fromisoformat


def market(bars=None, syms=("AMD", "MU")):
    """bars: {session: {symbol: (open, close)}} set exactly; everything else is a quiet random walk."""
    m = FL.Market(tuple(syms) + ("SPY",), vol=0.002)
    for d, per in (bars or {}).items():
        for s, (o, c) in per.items():
            m.set_bar(s, Dt(d), o, max(o, c) * 1.001, min(o, c) * 0.999, c)
    return m, FL.FLab(m)


def kw(lab, s, **extra):
    """Clock: 12:00 UTC on the day after session `s` (08:00 New York) — `s` is then the latest completed session."""
    return dict(path=lab.path, now=extra.pop("now", FL.at(Dt(s))), fetch_fn=lab.market.fetch, client=object(),
                cache=FC.BarCache(), **extra)


def account(lab, cash="100000", slip="0", comm="0"):
    return X.create_account("Paper", cash, slip, comm, path=lab.path, now=FL.at(Dt("2026-09-24")))


def order(lab, s, sym, side, qty, **k):
    return X.create_order(sym, side, qty, path=lab.path, now=FL.at(Dt(s)), fetch_fn=lab.market.fetch, client=object(),
                          cache=FC.BarCache(), **k)


def process(lab, s):
    return X.process_pending(**kw(lab, s))


def view(lab, s):
    return P.view(**kw(lab, s))


def digest(path):
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    h = hashlib.sha256()
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        for r in c.execute(f'SELECT * FROM "{t}" ORDER BY 1'):
            h.update(repr(r).encode())
    c.close()
    return h.hexdigest()


# ---- 66: account ----------------------------------------------------------------------------------------------------------------

def test_66_account_creation_and_starting_cash_has_no_default():
    m, lab = market()
    assert view(lab, "2026-09-25")["account"] is None                         # setup first; nothing is assumed
    acct = account(lab, "100000")
    v = view(lab, "2026-09-25")
    assert acct["starting_cash"] == "100000.00" and acct["base_currency"] == "USD"
    assert v["summary"]["cash"] == v["summary"]["equity"] == "100000.00" and v["positions"] == [] and v["summary"]["market_value"] == "0.00"
    with pytest.raises(X.PaperError) as e:
        account(lab)
    assert e.value.code == "ACCOUNT_EXISTS" and e.value.status == 409
    for bad in ("999.99", "100000001", "-5", "1000.005", "abc"):
        m2, lab2 = market()
        with pytest.raises(X.PaperError):
            X.create_account("P", bad, "0", "0", path=lab2.path)
    with pytest.raises(X.PaperError):
        X.create_account("P", "5000", "500.01", "0", path=lab2.path)


# ---- 67 / 77: costs — slippage and commission, exactly ----------------------------------------------------------------------------

def test_67_buy_at_next_open_with_slippage_and_commission_exact():
    m, lab = market({"2026-09-28": {"AMD": (100, 101)}})
    account(lab, "100000", "3", "1")
    o = order(lab, "2026-09-25", "AMD", "BUY", 10)
    assert (o["decision_session"], o["earliest_fill_session"], o["status"]) == ("2026-09-25", "2026-09-28", "PENDING")
    r = process(lab, "2026-09-28")
    (f,) = PaperStore(lab.path).fills(PaperStore(lab.path).account()["account_id"])
    assert r["filled"] == 1 and (f["fill_session"], f["base_price"], f["slippage_bps"], f["effective_price"]) == ("2026-09-28", "100.0000", "3.00", "100.0300")
    assert (f["notional"], f["commission"], f["cash_delta"], f["cash_after"]) == ("1000.30", "1.00", "-1001.30", "98998.70")
    assert (f["price_source"], f["adjustment"]) == ("FETCHED_NOW", B.ADJUSTMENT)
    (p,) = view(lab, "2026-09-28")["positions"]
    assert (p["shares"], p["cost_basis"], p["average_cost"]) == (10, "1001.30", "100.1300")
    assert p["lots"] == [{**p["lots"][0], "entry_session": "2026-09-28", "quantity": 10, "remaining": 10, "cost_per_share": "100.1300"}]


def test_77_costs_flow_consistently_into_cash_basis_and_realized_pnl():
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}, "2026-09-29": {"AMD": (120, 120)}})
    account(lab, "50000", "10", "2.50")
    order(lab, "2026-09-25", "AMD", "BUY", 7)
    process(lab, "2026-09-28")
    order(lab, "2026-09-28", "AMD", "SELL", 7)
    process(lab, "2026-09-29")
    buy, sell = PaperStore(lab.path).fills(PaperStore(lab.path).account()["account_id"])
    assert buy["effective_price"] == "100.1000" and buy["notional"] == "700.70" and buy["cash_delta"] == "-703.20"
    assert sell["effective_price"] == "119.8800" and sell["notional"] == "839.16" and sell["cash_delta"] == "836.66"
    assert sell["cost_basis_removed"] == "703.20" and sell["realized_pnl"] == "133.46"          # 836.66 - 703.20
    s = view(lab, "2026-09-29")["summary"]
    assert s["cash"] == "50133.46" == str(Decimal("50000") - Decimal("703.20") + Decimal("836.66")) and s["realized_pnl"] == "133.46"
    # the Stage 3.2 cost formula: entry = open x (1 + bps/10000), exit = open x (1 - bps/10000)
    assert abs(float(buy["effective_price"]) - 100 * (1 + 10 / 10000)) < 5e-5 and abs(float(sell["effective_price"]) - 120 * (1 - 10 / 10000)) < 5e-5


# ---- 68: FIFO -------------------------------------------------------------------------------------------------------------------------

def test_68_sell_closes_the_oldest_lots_first():
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}, "2026-09-29": {"AMD": (110, 110)}, "2026-09-30": {"AMD": (120, 125)}})
    account(lab)
    order(lab, "2026-09-25", "AMD", "BUY", 10)
    process(lab, "2026-09-28")
    order(lab, "2026-09-28", "AMD", "BUY", 10)
    process(lab, "2026-09-29")
    order(lab, "2026-09-29", "AMD", "SELL", 15)
    process(lab, "2026-09-30")
    acct = PaperStore(lab.path).account()
    fills, lots, closures = PaperStore(lab.path).ledger(acct["account_id"])
    by_lot = {lot["entry_session"]: lot["lot_id"] for lot in lots}
    assert [(c["lot_id"], c["quantity"], c["cost_basis"], c["proceeds"], c["realized_pnl"]) for c in closures] == [
        (by_lot["2026-09-28"], 10, "1000.00", "1200.00", "200.00"), (by_lot["2026-09-29"], 5, "550.00", "600.00", "50.00")]
    assert fills[-1]["realized_pnl"] == "250.00" and fills[-1]["cost_basis_removed"] == "1550.00"
    (p,) = view(lab, "2026-09-30")["positions"]
    assert (p["shares"], p["cost_basis"], [(x["entry_session"], x["remaining"]) for x in p["lots"]]) == (5, "550.00", [("2026-09-29", 5)])
    assert (p["mark"], p["market_value"], p["unrealized_pnl"], p["realized_pnl"]) == ("125.0000", "625.00", "75.00", "250.00")


def test_fifo_allocation_is_exact_in_cents_across_partial_sells():
    lots = [{"lot_id": "a", "symbol": "X", "entry_session": "2026-09-28", "quantity": 3, "total_cost": "100.01", "seq": 1}]
    closures = []
    for k, qty in enumerate((1, 1, 1)):
        fifo = A.open_lots(lots, closures, "X")
        cl = A.fifo_close(fifo, qty, A.money("40.00"))
        closures += [{**c, "symbol": "X", "cost_basis": A.s(c["cost_basis"]), "realized_pnl": A.s(c["realized_pnl"])} for c in cl]
    assert [c["cost_basis"] for c in closures] == ["33.34", "33.34", "33.33"]      # the last closure takes what remains
    assert sum(Decimal(c["cost_basis"]) for c in closures) == Decimal("100.01") and A.open_lots(lots, closures, "X") == []


# ---- 69 / 70: cash-only, long-only --------------------------------------------------------------------------------------------------

def test_69_a_gap_up_that_cash_cannot_cover_is_rejected_at_fill():
    m, lab = market({"2026-09-25": {"AMD": (99, 100)}, "2026-09-28": {"AMD": (110, 111)}})
    account(lab, "10000")
    o = order(lab, "2026-09-25", "AMD", "BUY", 95)                          # estimated 9,500 at the 100 close: accepted
    assert o["estimate_amount"] == "9500.00"
    r = process(lab, "2026-09-28")                                           # opens at 110: 10,450 > 10,000
    assert r["rejected"] == 1 and r["orders"][0]["reason"] == "INSUFFICIENT_CASH_AT_FILL"
    st = PaperStore(lab.path)
    assert st.order(o["order_id"])["status"] == "REJECTED" and st.fills(st.account()["account_id"]) == []
    assert st.ledger(st.account()["account_id"])[1] == [] and view(lab, "2026-09-28")["summary"]["cash"] == "10000.00"
    with pytest.raises(X.PaperError) as e:
        order(lab, "2026-09-28", "AMD", "BUY", 1000)                           # estimate already above cash: refused
    assert e.value.code == "INSUFFICIENT_CASH"


def test_70_no_oversell_and_no_short():
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}})
    account(lab)
    order(lab, "2026-09-25", "AMD", "BUY", 10)
    process(lab, "2026-09-28")
    for qty in (20, 11):
        with pytest.raises(X.PaperError) as e:
            order(lab, "2026-09-28", "AMD", "SELL", qty)
        assert e.value.code == "INSUFFICIENT_SHARES" and e.value.extra["owned"] == 10
    order(lab, "2026-09-28", "AMD", "SELL", 6)
    with pytest.raises(X.PaperError) as e:                                    # pending sells count
        order(lab, "2026-09-28", "AMD", "SELL", 5)
    assert e.value.extra["pending_sell"] == 6
    with pytest.raises(X.PaperError) as e:
        order(lab, "2026-09-28", "MU", "SELL", 1)                              # nothing held: no short
    assert e.value.code == "INSUFFICIENT_SHARES"
    for bad in (0, -3, 2.5, "10", True):
        with pytest.raises(X.PaperError):
            order(lab, "2026-09-28", "AMD", "BUY", bad)


# ---- 71 / 72 / 73: at most once, restart, cancel -------------------------------------------------------------------------------------

def test_71_duplicate_and_concurrent_processing_fill_once():
    m, lab = market({"2026-09-28": {"AMD": (100, 100), "MU": (50, 50)}})
    account(lab)
    order(lab, "2026-09-25", "AMD", "BUY", 10)
    order(lab, "2026-09-25", "MU", "BUY", 5)
    out, barrier = [], threading.Barrier(3)

    def go():
        barrier.wait()
        out.append(process(lab, "2026-09-28")["filled"])
    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert sum(out) == 2 and process(lab, "2026-09-28")["filled"] == 0
    st = PaperStore(lab.path)
    assert len(st.fills(st.account()["account_id"])) == 2


def test_71b_the_database_refuses_a_second_fill_even_without_the_engine_lock():
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}})
    account(lab)
    o = order(lab, "2026-09-25", "AMD", "BUY", 10)
    process(lab, "2026-09-28")
    st = PaperStore(lab.path)
    (f,) = st.fills(st.account()["account_id"])
    with sqlite3.connect(lab.path) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("INSERT INTO paper_fills SELECT ?, order_id, account_id, symbol, side, quantity, fill_session, base_price, "
                      "slippage_bps, effective_price, notional, commission, cash_delta, cash_after, cost_basis_removed, "
                      "realized_pnl, price_source, adjustment, feed, dataset_id, content_hash, filled_at, engine_version "
                      "FROM paper_fills", ("f" * 32,))
    assert st.order(o["order_id"])["status"] == "FILLED"


def test_72_restart_keeps_pending_and_fills_once(monkeypatch):
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}})
    account(lab)
    o = order(lab, "2026-09-25", "AMD", "BUY", 10)
    assert process(lab, "2026-09-25")["still_pending"] == 1                  # not completed yet: never filled early
    monkeypatch.setattr(FC, "BAR_CACHE", FC.BarCache())                      # a restarted server: nothing in memory
    monkeypatch.setattr(X, "_lock", threading.Lock())
    assert PaperStore(lab.path).order(o["order_id"])["status"] == "PENDING"
    assert process(lab, "2026-09-28")["filled"] == 1 and process(lab, "2026-09-29")["filled"] == 0


def test_73_cancel_only_pending_and_a_cancelled_order_never_fills():
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}})
    account(lab)
    o = order(lab, "2026-09-25", "AMD", "BUY", 10)
    c = X.cancel_order(o["order_id"], path=lab.path)
    assert c["status"] == "CANCELLED" and c["changed"] is True
    assert X.cancel_order(o["order_id"], path=lab.path)["changed"] is False     # idempotent
    assert process(lab, "2026-09-28")["orders_considered"] == 0
    st = PaperStore(lab.path)
    assert st.fills(st.account()["account_id"]) == []
    o2 = order(lab, "2026-09-28", "AMD", "BUY", 1)
    m.set_bar("AMD", Dt("2026-09-29"), 100, 101, 99, 100)
    process(lab, "2026-09-29")
    with pytest.raises(X.PaperError) as e:
        X.cancel_order(o2["order_id"], path=lab.path)
    assert e.value.code == "ALREADY_FILLED" and e.value.status == 409


# ---- 74: missing data; timing (no look-ahead) ------------------------------------------------------------------------------------------

def test_74_missing_next_open_keeps_the_order_pending():
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}})
    account(lab)
    o = order(lab, "2026-09-25", "AMD", "BUY", 10)
    m.drop("AMD", Dt("2026-09-28"))                                            # no bar for the fill session
    r = process(lab, "2026-09-28")
    assert r["filled"] == 0 and r["orders"][0]["reason"] == "NEXT_OPEN_UNAVAILABLE"
    row = PaperStore(lab.path).order(o["order_id"])
    assert (row["status"], row["wait_reason"]) == ("PENDING", "NEXT_OPEN_UNAVAILABLE")
    st = PaperStore(lab.path)
    assert st.fills(st.account()["account_id"]) == []                        # never an invented price


def test_timing_never_fills_at_an_open_that_was_known_when_ordering():
    m, lab = market()
    account(lab)
    during = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)               # Tue 11:00 New York: Tue's open is known
    o = X.create_order("AMD", "BUY", 1, path=lab.path, now=during, fetch_fn=m.fetch, client=object(), cache=FC.BarCache())
    assert (o["decision_session"], o["earliest_fill_session"]) == ("2026-09-28", "2026-09-30")
    pre = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)                  # Tue 08:00 New York: before the open
    o2 = X.create_order("AMD", "BUY", 1, path=lab.path, now=pre, fetch_fn=m.fetch, client=object(), cache=FC.BarCache())
    assert (o2["decision_session"], o2["earliest_fill_session"]) == ("2026-09-28", "2026-09-29")
    weekend = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)              # Saturday
    o3 = X.create_order("AMD", "BUY", 1, path=lab.path, now=weekend, fetch_fn=m.fetch, client=object(), cache=FC.BarCache())
    assert (o3["decision_session"], o3["earliest_fill_session"]) == ("2026-10-02", "2026-10-05")


def test_a_market_holiday_fills_at_the_next_real_session():
    m, lab = market({"2026-10-06": {"AMD": (100, 100)}})
    for s in ("AMD", "MU", "SPY"):
        m.drop(s, Dt("2026-10-05"))                                             # Monday: no session anywhere (holiday)
    account(lab)
    o = order(lab, "2026-10-02", "AMD", "BUY", 1)
    assert o["earliest_fill_session"] == "2026-10-05"
    r = process(lab, "2026-10-06")
    assert r["filled"] == 1 and r["orders"][0]["fill_session"] == "2026-10-06"


# ---- 75 / 76: marks and equity ---------------------------------------------------------------------------------------------------------

def test_75_76_marked_at_the_latest_completed_close_and_equity_adds_up():
    m, lab = market({"2026-09-28": {"AMD": (100, 100), "MU": (40, 40)}, "2026-09-29": {"AMD": (101, 125), "MU": (41, 38.5)}})
    account(lab, "20000", "3", "1")
    order(lab, "2026-09-25", "AMD", "BUY", 10)
    order(lab, "2026-09-25", "MU", "BUY", 33)
    process(lab, "2026-09-28")
    v = view(lab, "2026-09-29")
    s, pos = v["summary"], {p["symbol"]: p for p in v["positions"]}
    assert s["mark_session"] == "2026-09-29" and pos["AMD"]["mark"] == "125.0000" and pos["MU"]["mark"] == "38.5000"
    assert pos["AMD"]["market_value"] == "1250.00" and pos["AMD"]["unrealized_pnl"] == str(Decimal("1250.00") - Decimal(pos["AMD"]["cost_basis"]))
    assert Decimal(s["equity"]) == Decimal(s["cash"]) + Decimal(s["market_value"])
    assert Decimal(s["market_value"]) == Decimal(pos["AMD"]["market_value"]) + Decimal(pos["MU"]["market_value"])
    assert Decimal(s["unrealized_pnl"]) == Decimal(s["market_value"]) - Decimal(s["cost_basis"])
    m.drop("MU", Dt("2026-09-30"))                                           # no close for MU at the next mark session
    m.set_bar("AMD", Dt("2026-09-30"), 125, 126, 124, 125)
    v = view(lab, "2026-09-30")
    assert v["summary"]["unmarked_symbols"] == ["MU"] and v["summary"]["equity"] is None     # never guessed


def test_randomized_ledgers_keep_every_identity_exact():
    rnd = random.Random(45)
    sessions = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06"]
    bars = {s: {sym: (round(rnd.uniform(20, 200), 2), round(rnd.uniform(20, 200), 2)) for sym in ("AMD", "MU")} for s in sessions}
    m, lab = market(bars)
    account(lab, "250000", "7.5", "0.99")
    prev = "2026-09-25"
    for s in sessions:
        for sym in ("AMD", "MU"):
            held = sum(p["shares"] for p in view(lab, prev)["positions"] if p["symbol"] == sym)
            if held and rnd.random() < 0.5:
                order(lab, prev, sym, "SELL", rnd.randint(1, held))
            else:
                order(lab, prev, sym, "BUY", rnd.randint(1, 40))
        process(lab, s)
        prev = s
    st = PaperStore(lab.path)
    acct = st.account()
    fills, lots, closures = st.ledger(acct["account_id"])
    assert A.cash(acct["starting_cash"], fills) == Decimal(fills[-1]["cash_after"])
    for lot in lots:
        mine = [c for c in closures if c["lot_id"] == lot["lot_id"]]
        if sum(c["quantity"] for c in mine) == lot["quantity"]:
            assert sum(Decimal(c["cost_basis"]) for c in mine) == Decimal(lot["total_cost"])
    for f in fills:
        if f["side"] == "SELL":
            mine = [c for c in closures if c["sell_fill_id"] == f["fill_id"]]
            assert sum(Decimal(c["proceeds"]) for c in mine) == Decimal(f["cash_delta"])
            assert sum(Decimal(c["realized_pnl"]) for c in mine) == Decimal(f["realized_pnl"])
    s = view(lab, sessions[-1])["summary"]
    assert Decimal(s["equity"]) == Decimal(s["cash"]) + Decimal(s["market_value"]) and not s["cash"].startswith("-")


# ---- 78: immutability -------------------------------------------------------------------------------------------------------------------

def test_78_fills_lots_and_resolved_orders_are_immutable():
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}, "2026-09-29": {"AMD": (110, 110)}})
    account(lab)
    o = order(lab, "2026-09-25", "AMD", "BUY", 10)
    process(lab, "2026-09-28")
    order(lab, "2026-09-28", "AMD", "SELL", 4)
    process(lab, "2026-09-29")
    with sqlite3.connect(lab.path) as c:
        for bad in ("UPDATE paper_fills SET effective_price = '1.0000'", "UPDATE paper_fills SET quantity = 1",
                    "DELETE FROM paper_fills", "UPDATE paper_lots SET quantity = 99", "DELETE FROM paper_lots",
                    "UPDATE paper_lot_closures SET quantity = 1", "DELETE FROM paper_lot_closures", "DELETE FROM paper_orders",
                    "DELETE FROM paper_accounts", f"UPDATE paper_orders SET status = 'CANCELLED' WHERE order_id = '{o['order_id']}'",
                    "UPDATE paper_orders SET quantity = 5", "UPDATE paper_accounts SET starting_cash = '1.00'"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(bad)
        row = c.execute("SELECT * FROM paper_fills LIMIT 1").fetchone()
        cols = [r[1] for r in c.execute("PRAGMA table_info(paper_fills)")]
        with pytest.raises(sqlite3.DatabaseError, match="never replaced|at most once"):
            c.execute(f"INSERT OR REPLACE INTO paper_fills ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", row)
        lot = c.execute("SELECT lot_id, account_id FROM paper_lots").fetchone()
        sell = c.execute("SELECT fill_id FROM paper_fills WHERE side = 'SELL'").fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError, match="beyond its size"):
            c.execute("INSERT INTO paper_lot_closures VALUES (?, ?, ?, ?, 'AMD', 7, '1.00', '1.00', '0.00')",
                      ("c" * 32, lot[1], lot[0], sell))
        c.execute("UPDATE paper_accounts SET name = 'Renamed'")                  # administrative: allowed
    with pytest.raises(X.PaperError) as e:
        X.update_account(starting_cash="5000", path=lab.path)
    assert e.value.code == "ACCOUNT_FROZEN"


# ---- API, 79 / 80: no broker, no AI; strict bodies --------------------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from agents.usage_tracker import EXPLANATION, RESEARCH, usage_tracker
    from ai_explain import service as AX
    from alpaca.trading.client import TradingClient
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
    monkeypatch.setattr(TradingClient, "__init__", fail("alpaca TradingClient"))
    monkeypatch.setattr(socket, "create_connection", fail("network"))
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}, "2026-09-29": {"AMD": (120, 121)}})
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr("data.market_data.fetch_daily_bars", m.fetch)
    monkeypatch.setattr("data.market_data.get_data_client", lambda: object())
    clock = [FL.at(Dt("2026-09-25"))]
    monkeypatch.setattr(FC, "_utc", lambda now=None: now or clock[0])
    monkeypatch.setattr(FC, "BAR_CACHE", FC.BarCache())
    c = TestClient(app)
    c.lab, c.hits, c.clock = lab, hits, clock
    c.budgets = lambda: (usage_tracker.budget(RESEARCH)["used_today"], usage_tracker.budget(EXPLANATION)["used_today"], len(usage_tracker._records))
    return c


def test_79_80_api_workflow_needs_no_broker_and_no_ai(api):
    b0 = api.budgets()
    assert api.get("/api/paper-portfolio").json()["account"] is None
    assert api.post("/api/paper-portfolio/account", json={"slippage_bps": 0, "commission_per_order": 0}).status_code == 422   # cash required
    r = api.post("/api/paper-portfolio/account", json={"name": "Mine", "starting_cash": "100000", "slippage_bps": 3, "commission_per_order": 1})
    assert r.status_code == 201 and r.json()["summary"]["equity"] == "100000.00" and r.json()["label"] == "PAPER · SIMULATED · NO REAL ORDERS"
    o = api.post("/api/paper-orders", json={"symbol": "amd", "side": "BUY", "quantity": 10, "origin": "MANUAL"})
    assert o.status_code == 201 and o.json()["order"]["status"] == "PENDING" and o.json()["order"]["symbol"] == "AMD"
    api.clock[0] = FL.at(Dt("2026-09-28"))
    p = api.post("/api/paper-orders/process", json={}).json()
    assert p["process"]["filled"] == 1 and p["portfolio"]["positions"][0]["shares"] == 10
    assert api.post(f"/api/paper-orders/{o.json()['order']['order_id']}/cancel").status_code == 409
    s = api.post("/api/paper-orders", json={"symbol": "AMD", "side": "SELL", "quantity": 4})
    api.clock[0] = FL.at(Dt("2026-09-29"))
    api.post("/api/paper-orders/process")
    f = api.get("/api/paper-fills").json()["fills"]
    assert [x["side"] for x in f] == ["SELL", "BUY"] and f[0]["realized_pnl"] and s.status_code == 201
    assert api.hits == [] and api.budgets() == b0


def test_api_refuses_prices_sessions_fractions_and_unknown_fields(api):
    api.post("/api/paper-portfolio/account", json={"starting_cash": 50000, "slippage_bps": 0, "commission_per_order": 0})
    base = {"symbol": "AMD", "side": "BUY", "quantity": 1}
    for extra in ({"fill_price": 1}, {"decision_session": "2020-01-02"}, {"submitted_at": "2020-01-02T00:00:00Z"},
                  {"target_fill_session": "2030-01-01"}, {"limit_price": 90}, {"order_type": "LIMIT"}, {"account_id": "x"}):
        assert api.post("/api/paper-orders", json={**base, **extra}).status_code == 422, extra
    for bad in ({**base, "quantity": 1.5}, {**base, "quantity": "10"}, {**base, "quantity": 0}, {**base, "side": "SHORT"},
                {**base, "origin": "FORWARD_JOURNAL"}, {**base, "symbol": "BAD SYMBOL"}, {**base, "strategy_version_id": "x"}):
        assert api.post("/api/paper-orders", json=bad).status_code == 422, bad
    assert api.post("/api/paper-orders", json={**base, "strategy_version_id": "0" * 32}).json()["status"] == "UNKNOWN_STRATEGY_VERSION"
    assert api.post("/api/paper-orders", json={**base, "symbol": "ZZZZ"}).json()["status"] == "NO_DATA_FOR_SYMBOL"
    assert api.post("/api/paper-portfolio/account/settings", json={"starting_cash": 1}).status_code == 422       # below the limit
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if "paper" in p}
    from paper_endpoints import ALPACA_PAPER_ENDPOINTS, ALPACA_PAPER_ORDER_ENDPOINTS, PAPER_ENDPOINTS
    # the exact carve-out the earlier "no order endpoint" tests allow (Stage 4.6A adds its three read-only Alpaca paper paths;
    # Stage 4.6B its exact manual paper order paths)
    assert mine == {**PAPER_ENDPOINTS, **ALPACA_PAPER_ENDPOINTS, **ALPACA_PAPER_ORDER_ENDPOINTS}


# ---- 81: migration; isolation; no auto-trading; security ------------------------------------------------------------------------------

def test_81_migration_is_additive_idempotent_and_preserves_everything():
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}})
    lab.journal({**__import__("test_evidence_35").fspec(symbols=("AMD",))}, FL.ny(Dt("2026-09-24"), 12))
    from database.paper_migrations import run_paper_migrations
    with sqlite3.connect(lab.path) as c:
        uv = c.execute("PRAGMA user_version").fetchone()[0]
        t0 = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        schema0 = sorted(c.execute("SELECT type, name, sql FROM sqlite_master"))
        rows0 = {t: c.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in t0}
        for _ in range(3):
            run_paper_migrations(c)
        t1 = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert t1 - t0 == {"paper_accounts", "paper_orders", "paper_fills", "paper_lots", "paper_lot_closures"}
        assert [x for x in sorted(c.execute("SELECT type, name, sql FROM sqlite_master")) if x[1] in {y[1] for y in schema0}] == schema0
        assert {t: c.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall() for t in t0} == rows0
        assert c.execute("PRAGMA user_version").fetchone()[0] == uv


def test_reading_the_portfolio_writes_nothing_and_needs_no_tables(tmp_path):
    plain = tmp_path / "plain.db"
    sqlite3.connect(str(plain)).close()
    assert P.view(path=plain)["account"] is None and digest(plain) == digest(plain)
    with sqlite3.connect(str(plain)) as c:
        assert c.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    m, lab = market({"2026-09-28": {"AMD": (100, 100)}})
    account(lab)
    order(lab, "2026-09-25", "AMD", "BUY", 3)
    process(lab, "2026-09-28")
    before = digest(lab.path)
    for _ in range(3):
        view(lab, "2026-09-28")
    assert digest(lab.path) == before


def test_no_automatic_trading_and_isolated_from_the_real_portfolio():
    for f in ("forward/automation.py", "forward/journal.py", "fit/saved_scans.py", "fit/scanner.py", "fit/current.py",
              "notifications/delivery.py", "brief/daily.py", "api/routes/portfolio.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from paper|import paper)|paper\.execution|paper_orders|create_order\(", src, re.M), f
    from forward import automation as AU
    assert all("paper" not in e.name for e in AU._extensions)
    for f in ("paper/execution.py", "paper/portfolio.py", "paper/accounting.py", "paper/store.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"from portfolio|import portfolio|rh_gateway|robinhood", src, re.I), f


def test_security_no_broker_no_ai_no_code_execution():
    files = ["paper/execution.py", "paper/portfolio.py", "paper/accounting.py", "paper/store.py", "api/routes/paper.py",
             "database/paper_migrations.py", "frontend/paper_portfolio.js"]
    for f in files:
        src = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|new Function|"
                             r"pickle|importlib|powershell", src, re.I), f
        assert not re.search(r"TradingClient|alpaca\.trading|trading_client|submit_order|place_order|replace_order|cancel_order_by|"
                             r"/v2/orders|anthropic|get_provider|from agents|import agents|rh_gateway|robinhood|webhook|smtp|"
                             r"setInterval|setTimeout|threading\.Thread|Scheduler", src, re.I), f
    ex = (ROOT / "paper" / "execution.py").read_text(encoding="utf-8")
    assert "FC.load_bars(" in ex and "fetch_daily_bars" not in ex
    assert not re.search(r"(?i)latest_quote|LatestQuote|get_stock_latest|StockSnapshot|latest_trade|midpoint|bid_price|ask_price", ex)
    js = (ROOT / "frontend" / "paper_portfolio.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert not re.search(r"(?i)recommended (shares|size)|ideal size|safe size|suggested size|you should|best", code)
    assert "PAPER" in js and "SIMULATED" in js and "NO REAL ORDERS" in js and "fill_price" not in js
