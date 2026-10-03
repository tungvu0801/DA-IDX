"""Stage 3.2: point-in-time historical backtester (saved BACKTEST READY versions only; no orders, no AI, no network)."""
import copy
import hashlib
import json
import math
import re
import shutil
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import bt_fixtures as X
import config
from backtest import bars as B
from backtest import metrics as M
from backtest import runs as R
from backtest import snapshots as SN
from backtest.engine import RunConfig, _assert_execution_model, select_order, simulate
from backtest.replay import BarSeries, ReplayClient
from backtest.store import BacktestError, BacktestStore
from strategy import features as F
from strategy import spec as S
from strategy.evaluate import group_met

ROOT = Path(__file__).resolve().parents[1]
FROZEN_FINGERPRINT = "f394947b155dc9c002704531a9ca82ac3cfd37c3ab87dcadce4ccd8c15551e39"
D = date.fromisoformat
SHORT = {"start": "2026-03-02", "end": "2026-05-29"}          # ~62 sessions: keeps full-pipeline tests quick


# ---- engine harness: explicit bars + explicit feature cells ----------------------------------------------------------

def cells(rsi=50.0, support=None, resistance=None, close=None, trend="MIXED", a="AVAILABLE"):
    c = {"stock.rsi_14": {"v": rsi, "a": a if rsi is not None else "UNAVAILABLE"},
         "stock.support": {"v": support, "a": "AVAILABLE" if support is not None else "UNAVAILABLE"},
         "stock.resistance": {"v": resistance, "a": "AVAILABLE" if resistance is not None else "UNAVAILABLE"},
         "stock.close": {"v": close, "a": "AVAILABLE"}, "market.trend": {"v": trend, "a": "AVAILABLE"}}
    return c


def norm(**kw):
    s = X.spec(**kw)
    n, err = S.validate(s)
    assert not err, err
    return n


ENTRY_LOW_RSI = {"logic": "ALL", "conditions": [{"feature": "stock.rsi_14", "op": "<", "value": 20}]}


def engine(bars, snaps, *, symbols=None, exit_=None, risk=None, equity=10000.0, slip=0.0, comm=0.0, calendar=None):
    symbols = symbols or sorted(bars)
    prices = {s: BarSeries(s, X.rows_from(b)) for s, b in bars.items()}
    cal = calendar or sorted(set().union(*[set(b) for b in bars.values()]))
    sp = norm(symbols=symbols, entry=ENTRY_LOW_RSI,
              exit_=exit_ or {"logic": "ANY", "conditions": [{"feature": "stock.rsi_14", "op": ">", "value": 90}],
                              "invalidation": None, "target": None, "max_holding_days": None},
              risk=risk or {"max_position_pct": 50, "max_open_positions": 5})

    def snap(sym, T):
        if not prices[sym].has(T):
            return None
        c = snaps.get((sym, T)) or snaps.get(sym) or cells()
        return copy.deepcopy(c)
    cfg = RunConfig(cal[0], cal[-1], equity, slip, comm)
    return simulate(sp, cfg, cal, prices, snap), prices


def flat(days, price=100.0):
    return {d: (price, price + 1, price - 1, price) for d in days}


def ev(res, etype, sym=None):
    return [e for e in res["events"] if e["event_type"] == etype and (sym is None or e["symbol"] == sym)]


# ---- 61: FUTURE LEAK — mutating every bar after T never changes the snapshot or the signal at T ----------------------

def _all_needs(universe):
    hist = [f for f in F.FEATURES if f.historical_support]
    return SN.Needs(tuple(universe), tuple(sorted(f.feature_id for f in hist if f.scope == "STOCK")),
                    tuple(sorted(f.feature_id for f in hist if f.scope == "MARKET")),
                    tuple(sorted(f.feature_id for f in hist if f.scope == "SECTOR")),
                    (SN.QQQ, SN.SOXX, SN.SPY), tuple(config.FALLBACK_UNIVERSE), tuple((s, SN.SOXX) for s in universe))


def _series(days, mutate_after=None):
    syms = sorted({*config.FALLBACK_UNIVERSE, SN.SPY, SN.QQQ, SN.SOXX, "AMD", "NVDA"})
    out = {}
    for s in syms:
        rows = X.walk(s, days)
        if mutate_after is not None:
            rows = [r if D(r[0]) <= mutate_after else (r[0], r[1], r[2] * 3.0, r[3] * 9.0, r[4] * 0.2, r[5] * 0.1, r[6] * 50)
                    for r in rows]
        out[s] = BarSeries(s, rows)
    return out


def test_future_bars_cannot_change_the_snapshot_at_T():
    days = X.sessions(D("2025-06-02"), D("2026-03-31"))
    T = D("2026-01-15")
    needs = _all_needs(("AMD", "NVDA"))
    before = SN.day_snapshot(_series(days), needs, T)
    after = SN.day_snapshot(_series(days, mutate_after=T), needs, T)
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)          # byte-identical
    # the snapshot is real: stock, market (breadth list) and sector features were all computed from bars
    amd = before["symbols"]["AMD"]["cells"]
    assert amd["stock.rsi_14"]["a"] == "AVAILABLE" and amd["stock.trend"]["v"] in ("UPTREND", "DOWNTREND", "MIXED")
    assert amd["sector.context"]["a"] == "AVAILABLE" and amd["sector.etf_trend"]["a"] == "AVAILABLE"
    assert before["market"]["market.breadth"]["a"] == "AVAILABLE" and before["market"]["market.environment"]["v"]
    assert before["breadth_members"] >= 70 and before["replay"]["violations"] == 0
    assert before["symbols"]["AMD"]["as_of"].startswith("2026-01-15")


def test_future_bars_cannot_change_signals_up_to_T(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = X.Lab()
    days = X.sessions(D("2025-01-02"), D("2026-09-25"))
    T = D("2026-03-16")
    base = {s: X.walk(s, days) for s in ("AMD", "MU", "NVDA", "SPY")}
    sp = X.spec(entry={"logic": "ALL", "conditions": [{"feature": "stock.trend", "op": "==", "value": "UPTREND"}]})
    sid = lab.save(sp)["strategy_id"]

    def run_with(rows_by_sym):
        lab2 = X.Lab()
        s2 = lab2.save(sp)["strategy_id"]
        for s, rows in rows_by_sym.items():
            lab2.cache(s, rows)
        rid = R.start_run(lab2.store, X.body(s2, start="2026-01-05", end="2026-05-29"), now=X.NOW)["run_id"]
        return lab2.store.signals(rid)
    a = run_with(base)
    mutated = {s: [r if D(r[0]) <= T else (r[0], r[1], r[2] * 0.5, r[3] * 4, r[4] * 0.1, r[5] * 3.0, r[6]) for r in rows]
               for s, rows in base.items()}
    b = run_with(mutated)
    upto = lambda evs: [(e["session_date"], e["symbol"], e["event_type"], e["reason_code"], e["detail"]) for e in evs  # noqa: E731
                        if e["session_date"] <= T.isoformat()]
    assert upto(a) and upto(a) == upto(b)
    assert sid


def test_replay_serves_nothing_after_T_and_only_fresh_windows():
    days = X.sessions(D("2025-01-02"), D("2025-12-31"))
    s = {"AMD": BarSeries("AMD", X.walk("AMD", days)), "MU": BarSeries("MU", X.walk("MU", days[:-10]))}
    T = days[-5]
    from data.market_data import fetch_daily_bars
    frames = fetch_daily_bars(ReplayClient(s, T), ["AMD", "MU", "ZZZ"])      # the EXISTING function, unchanged
    assert set(frames) == {"AMD"}                           # MU has no bar dated T -> not presented as today
    assert frames["AMD"]["timestamp"].max().tz_convert(B.NY).date() == T
    first = frames["AMD"]["timestamp"].min().tz_convert(B.NY).date()
    assert first >= T - timedelta(days=config.DAILY_BAR_LOOKBACK_DAYS - 1)    # the live app's calendar window
    from types import SimpleNamespace
    from alpaca.data.enums import Adjustment
    from alpaca.data.timeframe import TimeFrame
    rc = ReplayClient(s, T)
    with pytest.raises(ValueError):
        rc.get_stock_bars(SimpleNamespace(timeframe=TimeFrame.Hour, adjustment=Adjustment.ALL, symbol_or_symbols="AMD"))
    with pytest.raises(ValueError):
        rc.get_stock_bars(SimpleNamespace(timeframe=TimeFrame.Day, adjustment=Adjustment.RAW, symbol_or_symbols="AMD"))


# ---- 62 / 14: next-open fills, never the signal close ---------------------------------------------------------------

def test_entry_and_exit_fill_at_next_open_not_signal_close():
    days = X.sessions(D("2026-03-02"), D("2026-03-13"))
    b = flat(days)
    b[days[2]] = (100, 101, 99, 100)      # signal close T = $100
    b[days[3]] = (120, 125, 118, 121)     # next open $120
    b[days[6]] = (130, 131, 129, 130)     # exit signal close $130
    b[days[7]] = (150, 151, 149, 150)     # next open $150
    snaps = {("AMD", days[2]): cells(rsi=10), ("AMD", days[6]): cells(rsi=95)}
    res, prices = engine({"AMD": b}, snaps)
    t = res["trades"][0]
    assert (t["entry_signal_date"], t["entry_fill_date"]) == (days[2].isoformat(), days[3].isoformat())
    assert t["entry_fill_price"] == 120 and t["entry_open_price"] == 120          # not the $100 signal close
    assert (t["exit_signal_date"], t["exit_fill_date"]) == (days[6].isoformat(), days[7].isoformat())
    assert t["exit_fill_price"] == 150                                            # not the $130 signal close
    assert all(tr["entry_fill_date"] > tr["entry_signal_date"] for tr in res["trades"])


def test_same_close_execution_is_rejected_by_the_guard_and_the_database():
    days = X.sessions(D("2026-03-02"), D("2026-03-06"))
    prices = {"AMD": BarSeries("AMD", X.rows_from(flat(days)))}
    bad = {"trade_no": 1, "symbol": "AMD", "entry_signal_date": days[1].isoformat(), "entry_fill_date": days[1].isoformat(),
           "entry_open_price": 100.0, "exit_fill_date": None}
    with pytest.raises(AssertionError):
        _assert_execution_model([bad], prices)
    bad2 = {**bad, "entry_fill_date": days[2].isoformat(), "entry_open_price": 100.0 + 1e-6 + 0.5}
    with pytest.raises(AssertionError):                         # a fill price that is not the fill day's open
        _assert_execution_model([bad2], prices)
    sql = (ROOT / "database" / "backtest_migrations.py").read_text(encoding="utf-8")
    assert "CHECK (entry_fill_date > entry_signal_date)" in sql and "CHECK (exit_fill_date IS NULL OR exit_fill_date > exit_signal_date)" in sql


# ---- 63 / 64: entry support / resistance frozen for the life of the trade -------------------------------------------

def test_support_frozen_from_entry_snapshot():
    days = X.sessions(D("2026-03-02"), D("2026-03-20"))
    b = flat(days)
    for d in days[4:9]:
        b[d] = (100, 101, 99, 100)        # closes at $100: below later support ($110), above entry support ($90)
    b[days[10]] = (95, 96, 88, 89)        # close $89 < $90 -> invalidation
    snaps = {"AMD": cells(rsi=50, support=110.0, resistance=200.0), ("AMD", days[2]): cells(rsi=10, support=90.0, resistance=200.0)}
    res, _ = engine({"AMD": b}, snaps, exit_={"logic": "ANY", "conditions": [], "invalidation": {"method": "CLOSE_BELOW_ENTRY_SUPPORT"},
                                               "target": None, "max_holding_days": None})
    t = res["trades"][0]
    assert t["entry_support"] == 90.0 and t["invalidation_level"] == 90.0
    assert t["exit_signal_date"] == days[10].isoformat() and t["all_exit_reasons"] == ["INVALIDATION"]


def test_resistance_frozen_from_entry_snapshot():
    days = X.sessions(D("2026-03-02"), D("2026-03-20"))
    b = flat(days)
    for d in days[4:9]:
        b[d] = (120, 121, 119, 120)       # above later resistance ($110), below entry resistance ($130)
    b[days[10]] = (129, 132, 128, 131)
    snaps = {"AMD": cells(rsi=50, support=50.0, resistance=110.0), ("AMD", days[2]): cells(rsi=10, support=50.0, resistance=130.0)}
    res, _ = engine({"AMD": b}, snaps, exit_={"logic": "ANY", "conditions": [], "invalidation": None,
                                               "target": {"method": "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE"}, "max_holding_days": None})
    t = res["trades"][0]
    assert t["target_level"] == 130.0 and t["exit_signal_date"] == days[10].isoformat() and t["all_exit_reasons"] == ["TARGET"]


def test_required_entry_levels_missing_skip_the_entry():
    days = X.sessions(D("2026-03-02"), D("2026-03-10"))
    snaps = {("AMD", days[1]): cells(rsi=10, support=None, resistance=130.0), ("MU", days[1]): cells(rsi=10, support=90.0, resistance=None)}
    res, _ = engine({"AMD": flat(days), "MU": flat(days)}, snaps,
                    exit_={"logic": "ANY", "conditions": [], "invalidation": {"method": "CLOSE_BELOW_ENTRY_SUPPORT"},
                           "target": {"method": "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE"}, "max_holding_days": None})
    assert res["trades"] == []
    assert [e["reason_code"] for e in ev(res, "ENTRY_SKIPPED")] == ["REQUIRED_ENTRY_SUPPORT_UNAVAILABLE",
                                                                  "REQUIRED_ENTRY_RESISTANCE_UNAVAILABLE"]


def test_percent_levels_use_the_actual_fill_after_slippage():
    days = X.sessions(D("2026-03-02"), D("2026-03-20"))
    b = flat(days)
    b[days[3]] = (100, 101, 99, 100)
    snaps = {("AMD", days[2]): cells(rsi=10)}
    res, _ = engine({"AMD": b}, snaps, slip=50, exit_={"logic": "ANY", "conditions": [],
                                                      "invalidation": {"method": "PCT_BELOW_ENTRY", "pct": 3},
                                                      "target": {"method": "PCT_ABOVE_ENTRY", "pct": 5}, "max_holding_days": None})
    t = res["trades"][0] if res["trades"] else None
    fill = 100 * 1.005
    assert t["entry_fill_price"] == pytest.approx(fill)
    assert t["invalidation_level"] == pytest.approx(fill * 0.97) and t["target_level"] == pytest.approx(fill * 1.05)


# ---- 65: MFE / MAE — entry day's range counts, the exit day's range after the open does not -------------------------

def test_mfe_mae_semantics_and_gap_exit():
    days = X.sessions(D("2026-03-02"), D("2026-03-13"))
    b = flat(days)
    b[days[1]] = (100, 100, 100, 100)                 # signal
    b[days[2]] = (100, 108, 97, 104)                  # entry at open 100: day's high 108 / low 97 count
    b[days[3]] = (104, 112, 99, 105)                  # exit signal at this close
    b[days[4]] = (95, 130, 90, 128)                   # gap-down exit at open 95: 130 / 90 must NOT count
    snaps = {("AMD", days[1]): cells(rsi=10), ("AMD", days[3]): cells(rsi=95)}
    res, _ = engine({"AMD": b}, snaps)
    t = res["trades"][0]
    assert t["exit_fill_price"] == 95
    assert t["mfe_pct"] == pytest.approx(12.0) and t["mae_pct"] == pytest.approx(-5.0)
    assert t["mfe_pct"] >= 0 >= t["mae_pct"]


def test_mfe_mae_clamped_when_price_never_moves_favourably():
    days = X.sessions(D("2026-03-02"), D("2026-03-13"))
    b = flat(days)
    b[days[2]] = (100, 100, 95, 96)
    b[days[3]] = (96, 99, 94, 95)
    b[days[4]] = (95, 96, 94, 95)
    snaps = {("AMD", days[1]): cells(rsi=10), ("AMD", days[3]): cells(rsi=95)}
    t = engine({"AMD": b}, snaps)[0]["trades"][0]
    assert t["mfe_pct"] == 0.0 and t["mae_pct"] == pytest.approx(-6.0)


# ---- 66: max holding counts trading sessions (weekends, holidays, missing bars) --------------------------------------

def test_max_holding_counts_sessions_not_calendar_days():
    # Thu 2026-04-02 signal · Fri 04-03 = market holiday in the fixtures · entry Mon 04-06 (day 1) · Tue 04-07 (day 2)
    # Wed 04-08 the SYMBOL has no bar (missing session: not counted) · Thu 04-09 day 3 -> exit signal · Fri 04-10 fill
    cal = X.sessions(D("2026-03-30"), D("2026-04-17"))
    assert D("2026-04-03") not in cal
    b = flat(cal)
    del b[D("2026-04-08")]
    snaps = {("AMD", D("2026-04-02")): cells(rsi=10)}
    res, _ = engine({"AMD": b}, snaps, calendar=cal, exit_={"logic": "ANY", "conditions": [], "invalidation": None,
                                                             "target": None, "max_holding_days": 3})
    t = res["trades"][0]
    assert t["entry_fill_date"] == "2026-04-06" and t["holding_days"] == 3
    assert t["exit_signal_date"] == "2026-04-09" and t["exit_fill_date"] == "2026-04-10"
    assert t["all_exit_reasons"] == ["MAX_HOLDING"]


def test_max_holding_one_day_exits_after_the_entry_day():
    cal = X.sessions(D("2026-03-02"), D("2026-03-10"))
    res, _ = engine({"AMD": flat(cal)}, {("AMD", cal[0]): cells(rsi=10)},
                    exit_={"logic": "ANY", "conditions": [], "invalidation": None, "target": None, "max_holding_days": 1})
    t = res["trades"][0]
    assert (t["entry_fill_date"], t["exit_signal_date"], t["exit_fill_date"]) == (cal[1].isoformat(), cal[1].isoformat(), cal[2].isoformat())
    assert t["holding_days"] == 1


# ---- 25: exit and re-entry never share a close --------------------------------------------------------------------------

def test_no_exit_and_reentry_on_the_same_close():
    cal = X.sessions(D("2026-03-02"), D("2026-03-13"))
    # entry signal day 0; exit condition AND entry condition both true on day 3
    snaps = {("AMD", cal[0]): cells(rsi=10), ("AMD", cal[3]): cells(rsi=10), ("AMD", cal[4]): cells(rsi=10)}
    res, _ = engine({"AMD": flat(cal)}, snaps, exit_={"logic": "ANY", "conditions": [{"feature": "stock.rsi_14", "op": "<", "value": 15}],
                                                       "invalidation": None, "target": None, "max_holding_days": None})
    t1, t2 = res["trades"][0], res["trades"][1]
    assert t1["exit_signal_date"] == cal[3].isoformat() and t1["exit_fill_date"] == cal[4].isoformat()
    assert [e["session_date"] for e in ev(res, "ENTRY_SIGNAL")] == [cal[0].isoformat(), cal[4].isoformat()]  # not cal[3]
    assert t2["entry_signal_date"] == cal[4].isoformat() and t2["entry_fill_date"] == cal[5].isoformat()


# ---- 13: unfilled entries ----------------------------------------------------------------------------------------------

def test_unfilled_entries_are_recorded_never_fabricated():
    cal = X.sessions(D("2026-03-02"), D("2026-03-06"))
    b = flat(cal)
    del b[cal[2]]                                     # MU has no bar on the session after its signal
    snaps = {("AMD", cal[-1]): cells(rsi=10), ("MU", cal[1]): cells(rsi=10)}
    res, _ = engine({"AMD": flat(cal), "MU": b}, snaps, calendar=cal)
    un = {e["symbol"]: e["reason_code"] for e in ev(res, "UNFILLED_ENTRY")}
    assert un == {"AMD": "NO_NEXT_SESSION_IN_RUN", "MU": "NEXT_SESSION_BAR_MISSING"} and res["trades"] == []


# ---- 67: multi-symbol contention, sizing, cash, no leverage -------------------------------------------------------------

def test_alphabetical_contention_and_max_open_positions():
    cal = X.sessions(D("2026-03-02"), D("2026-03-13"))
    syms = ["NVDA", "AMD", "MU", "INTC"]
    snaps = {(s, cal[1]): cells(rsi=10) for s in syms}
    res, _ = engine({s: flat(cal) for s in syms}, snaps, symbols=syms, risk={"max_position_pct": 20, "max_open_positions": 2})
    assert [e["symbol"] for e in ev(res, "ENTRY_SIGNAL")] == ["AMD", "INTC", "MU", "NVDA"]
    assert [e["symbol"] for e in ev(res, "ENTRY_FILLED")] == ["AMD", "INTC"]
    assert {e["symbol"]: e["reason_code"] for e in ev(res, "ENTRY_SKIPPED")} == {"MU": "MAX_OPEN_POSITIONS", "NVDA": "MAX_OPEN_POSITIONS"}
    assert select_order(["b", "a"], "ALPHABETICAL") == ["a", "b"]
    with pytest.raises(ValueError):
        select_order(["a"], "STRONGEST")


def test_sizing_cash_cap_no_leverage_no_negative_cash():
    cal = X.sessions(D("2026-03-02"), D("2026-03-13"))
    syms = ["AMD", "MU", "NVDA"]
    snaps = {(s, cal[1]): cells(rsi=10) for s in syms}
    res, _ = engine({s: flat(cal) for s in syms}, snaps, symbols=syms, risk={"max_position_pct": 45, "max_open_positions": 3})
    f = {e["symbol"]: e["detail"] for e in ev(res, "ENTRY_FILLED")}
    assert f["AMD"]["shares"] == 45 and f["MU"]["shares"] == 45         # 45% of $10,000 at $100
    assert f["NVDA"]["shares"] == 10 and f["NVDA"]["target_value"] == 4500   # capped by the $1,000 of cash left
    assert all(e["cash"] >= 0 for e in res["equity"])
    assert all(e["market_value"] <= e["equity"] + 1e-9 for e in res["equity"])      # no leverage


def test_insufficient_cash_and_target_below_one_share():
    cal = X.sessions(D("2026-03-02"), D("2026-03-13"))
    snaps = {("AMD", cal[1]): cells(rsi=10), ("MU", cal[1]): cells(rsi=10)}
    b = {d: (20000.0, 20001.0, 19999.0, 20000.0) for d in cal}
    res, _ = engine({"AMD": b, "MU": {d: (3000.0, 3001.0, 2999.0, 3000.0) for d in cal}}, snaps, symbols=["AMD", "MU"],
                    risk={"max_position_pct": 10, "max_open_positions": 5})
    assert {e["symbol"]: e["reason_code"] for e in ev(res, "ENTRY_SKIPPED")} == {"AMD": "INSUFFICIENT_CASH",
                                                                                "MU": "TARGET_BELOW_ONE_SHARE"}


# ---- 68: exact cost arithmetic -------------------------------------------------------------------------------------------

def test_exact_slippage_commission_cash_pnl_and_equity():
    cal = X.sessions(D("2026-03-02"), D("2026-03-13"))
    b = flat(cal)
    b[cal[2]] = (100, 101, 99, 100)
    b[cal[5]] = (110, 111, 109, 110)
    for d in cal[6:]:
        b[d] = (110, 111, 109, 110)
    snaps = {("AMD", cal[1]): cells(rsi=10), ("AMD", cal[4]): cells(rsi=95)}
    res, _ = engine({"AMD": b}, snaps, slip=10, comm=1.0, risk={"max_position_pct": 50, "max_open_positions": 1})
    t = res["trades"][0]
    assert t["entry_fill_price"] == pytest.approx(100.1) and t["shares"] == 49
    assert t["entry_value"] == pytest.approx(4904.9)
    assert t["exit_fill_price"] == pytest.approx(109.89) and t["exit_value"] == pytest.approx(5384.61)
    assert t["commission"] == pytest.approx(2.0) and t["slippage_impact"] == pytest.approx(4.9 + 5.39)
    assert t["pnl_dollars"] == pytest.approx(5384.61 - 1 - 4904.9 - 1)
    assert t["return_pct"] == pytest.approx((5384.61 - 1 - 4904.9 - 1) / 4905.9 * 100)
    assert res["ending_cash"] == pytest.approx(10000 - 4904.9 - 1 + 5384.61 - 1)
    assert res["equity"][-1]["equity"] == pytest.approx(10477.71)


# ---- 69: open at the end — marked, not liquidated, not a closed trade -----------------------------------------------------

def test_open_position_at_end_is_marked_not_closed():
    cal = X.sessions(D("2026-03-02"), D("2026-03-13"))
    b = flat(cal)
    b[cal[-1]] = (100, 106, 99, 105)
    res, _ = engine({"AMD": b}, {("AMD", cal[1]): cells(rsi=10)})
    [t] = res["trades"]
    assert t["status"] == "OPEN_AT_END" and t["exit_fill_date"] is None and t["pnl_dollars"] is None
    assert t["mark_price"] == 105 and t["mark_date"] == cal[-1].isoformat()
    m = M.core_metrics(10000, res["trades"], res["equity"])
    assert m["closed_trades"] == 0 and m["open_positions_at_end"] == 1 and m["winning_trades"] == 0
    assert m["ending_equity"] == pytest.approx(res["ending_cash"] + t["shares"] * 105)
    assert ev(res, "EXIT_FILLED") == []


def test_exit_signal_on_last_session_is_unfilled_and_stays_open():
    cal = X.sessions(D("2026-03-02"), D("2026-03-06"))
    res, _ = engine({"AMD": flat(cal)}, {("AMD", cal[0]): cells(rsi=10), ("AMD", cal[-1]): cells(rsi=95)})
    [t] = res["trades"]
    assert t["status"] == "OPEN_AT_END" and t["exit_signal_date"] == cal[-1].isoformat()
    assert [e["reason_code"] for e in ev(res, "UNFILLED_EXIT")] == ["NO_NEXT_SESSION_IN_RUN"]


# ---- 70 / 35 / 36 / 37: metrics formulas ------------------------------------------------------------------------------

def _t(ret, pnl, days=3, mfe=2.0, mae=-1.0, sym="AMD", exit_="2026-03-10", reasons=("CONDITION",), trend="MIXED"):
    return {"status": "CLOSED", "return_pct": ret, "pnl_dollars": pnl, "holding_days": days, "mfe_pct": mfe, "mae_pct": mae,
            "symbol": sym, "exit_fill_date": exit_, "trade_no": 1, "entry_fill_date": "2026-03-02", "commission": 0.0,
            "slippage_impact": 0.0, "unrealized_pnl": None, "all_exit_reasons": list(reasons), "market_trend_at_entry": trend,
            "market_environment_at_entry": None}


def _eq(values, positions=None):
    out, peak, prev = [], values[0], None
    for i, v in enumerate(values):
        peak = max(peak, v)
        out.append({"equity": v, "drawdown": min(0.0, v / peak - 1), "open_positions": (positions or [0] * len(values))[i]})
    return out


def test_metrics_mixed_trades_exact():
    trades = [_t(10.0, 1000), _t(-5.0, -500), _t(4.0, 400), _t(0.0, 0.0)]
    m = M.core_metrics(10000, trades, _eq([10000, 11000, 9900, 12000], positions=[0, 1, 2, 1]))
    assert m["total_return_pct"] == pytest.approx(20.0) and m["max_drawdown_pct"] == pytest.approx(-10.0)
    assert (m["winning_trades"], m["losing_trades"], m["breakeven_trades"]) == (2, 1, 1)
    assert m["win_rate_pct"] == 50.0 and m["average_trade_return_pct"] == pytest.approx(2.25)
    assert m["median_trade_return_pct"] == pytest.approx(2.0)
    assert m["average_winner_pct"] == pytest.approx(7.0) and m["average_loser_pct"] == pytest.approx(-5.0)
    assert m["gross_profit"] == 1400 and m["gross_loss"] == -500 and m["profit_factor"]["value"] == pytest.approx(2.8)
    assert m["expectancy_decomposition_pct"] == pytest.approx(m["expectancy_pct"])        # win%·avgW + loss%·avgL
    assert m["time_in_market_pct"] == 75.0 and m["max_simultaneous_positions"] == 2 and m["average_simultaneous_positions"] == 1.0
    assert m["best_trade"]["return_pct"] == 10.0 and m["worst_trade"]["return_pct"] == -5.0
    assert m["annualized_return_pct"] is None and "shorter than one trading year" in m["annualized_note"]


def test_metrics_edge_cases_zero_all_winners_all_losers_one_trade():
    z = M.core_metrics(10000, [], _eq([10000, 10000]))
    assert z["closed_trades"] == 0 and z["win_rate_pct"] is None and z["profit_factor"] == {"value": None, "text": "N/A — no closed trades"}
    w = M.core_metrics(10000, [_t(5, 500), _t(3, 300)], _eq([10000, 10800]))
    assert w["profit_factor"] == {"value": None, "text": "N/A — no losing trades"} and w["win_rate_pct"] == 100.0
    lo = M.core_metrics(10000, [_t(-5, -500), _t(-3, -300)], _eq([10000, 9200]))
    assert lo["profit_factor"]["value"] == 0.0 and lo["win_rate_pct"] == 0.0 and lo["average_winner_pct"] is None
    one = M.core_metrics(10000, [_t(2.5, 250, days=4)], _eq([10000, 10250]))
    assert one["average_trade_return_pct"] == one["median_trade_return_pct"] == 2.5 and one["median_holding_days"] == 4


def test_annualized_only_with_a_trading_year():
    eq = _eq([10000 * (1.001 ** i) for i in range(252)])
    m = M.core_metrics(10000, [], eq)
    assert m["annualized_return_pct"] == pytest.approx(((eq[-1]["equity"] / 10000) ** (252 / 252) - 1) * 100)
    assert M.core_metrics(10000, [], eq[:251])["annualized_return_pct"] is None


def test_sample_size_bands():
    assert [M.sample_size(n)["code"] for n in (0, 1, 9, 10, 29, 30, 200)] == [
        "NO_TRADES", "VERY_SMALL_SAMPLE", "VERY_SMALL_SAMPLE", "SMALL_SAMPLE", "SMALL_SAMPLE", "SAMPLE_SIZE", "SAMPLE_SIZE"]
    assert "not a quality rating" in M.sample_size(5)["note"] and "no statistical proof" in M.sample_size(40)["text"]


def test_breakdowns_group_and_multiple_reasons():
    tr = [_t(5, 500, sym="MU", reasons=("TARGET",), trend="IMPROVING"), _t(-2, -200, sym="AMD", reasons=("INVALIDATION", "MAX_HOLDING")),
          _t(1, 100, sym="AMD", reasons=("CONDITION",), trend="IMPROVING")]
    b = M.breakdowns(tr, environment_available=False)
    assert [g["group"] for g in b["exit_reason"]] == ["CONDITION", "MULTIPLE", "TARGET"]
    assert {g["group"]: g["closed_trades"] for g in b["symbol"]} == {"AMD": 2, "MU": 1}
    assert {g["group"]: g["closed_trades"] for g in b["market_trend_at_entry"]} == {"IMPROVING": 2, "MIXED": 1}
    assert b["exit_reason_appearances"]["MAX_HOLDING"] == 1 and b["market_environment_at_entry"] is None


def test_benchmark_buy_and_hold_arithmetic_and_leftover_cash():
    cal = X.sessions(D("2026-03-02"), D("2026-03-06"))
    spy = {d: (400.0, 401.0, 399.0, 400.0 + i) for i, d in enumerate(cal)}
    prices = {"SPY": BarSeries("SPY", X.rows_from(spy))}
    bh = M.buy_and_hold(["SPY"], prices, cal, 10000.0, 10.0, 1.0)
    fill = 400 * 1.001
    shares = math.floor((10000 - 1) / fill)
    assert bh["holdings"]["SPY"]["shares"] == shares
    assert bh["uninvested_cash"] == pytest.approx(10000 - shares * fill - 1)
    assert bh["series"][-1] == pytest.approx(bh["uninvested_cash"] + shares * 404.0)
    ew = M.buy_and_hold(["SPY", "ZZZ"], prices, cal, 10000.0, 0.0, 0.0)
    assert ew["excluded"] == [{"symbol": "ZZZ", "reason": "NO_BAR_AT_FIRST_SESSION"}] and ew["holdings"]["SPY"]["shares"] == 12


# ---- 8 / 9: warm-up and INSUFFICIENT_HISTORY ---------------------------------------------------------------------------

def test_insufficient_history_withholds_features_and_blocks_conditions():
    days = X.sessions(D("2026-01-02"), D("2026-03-31"))
    series = {"NEWCO": BarSeries("NEWCO", X.walk("NEWCO", days[-30:])), "SPY": BarSeries("SPY", X.walk("SPY", days))}
    needs = SN.Needs(("NEWCO",), ("stock.close", "stock.resistance", "stock.rsi_14", "stock.support", "stock.trend"),
                     ("market.trend",), (), ("SPY",), (), ())
    snap = SN.day_snapshot(series, needs, days[-1])
    c = snap["symbols"]["NEWCO"]["cells"]
    assert c["stock.trend"] == {"v": None, "a": "INSUFFICIENT_HISTORY"}          # 30 sessions < EMA50: never "MIXED"
    assert c["stock.support"]["a"] == "INSUFFICIENT_HISTORY" and c["stock.rsi_14"]["a"] == "AVAILABLE"
    ok, trace = group_met({"logic": "ALL", "conditions": [{"feature": "stock.trend", "op": "==", "value": "MIXED"}]},
                          {k: v["v"] for k, v in c.items()})
    assert ok is False and trace[0]["result"] == "UNAVAILABLE"


def test_warmup_bars_never_create_trades_before_the_start(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = X.Lab()
    days = X.sessions(D("2024-10-01"), D("2026-09-25"))
    for s in ("AMD", "MU", "NVDA", "SPY"):
        lab.cache(s, X.walk(s, days))
    sid = lab.save(X.spec())["strategy_id"]
    rid = R.start_run(lab.store, X.body(sid, start="2026-01-05", end="2026-05-29"), now=X.NOW)["run_id"]
    run = lab.store.get_run(rid)
    assert run["status"] == "COMPLETED"
    assert all(e["session_date"] >= "2026-01-05" for e in lab.store.signals(rid))
    assert lab.store.equity(rid)[0]["session_date"] == "2026-01-05"
    assert run["result"]["audit"]["evaluations_with_insufficient_history"] == 0


# ---- 3: eligibility — only BACKTEST_READY, verified versions ------------------------------------------------------------

def _raw_version(lab, spec, fingerprint=None, spec_hash=None, readiness="BACKTEST_READY"):
    """Insert a version directly (as an old registry or a tampered row would look) — triggers allow INSERT only."""
    import uuid
    sp = copy.deepcopy(spec)
    if fingerprint:
        sp["feature_registry_fingerprint"] = fingerprint
    cj = S.canonical_json(sp)
    sid = uuid.uuid4().hex
    with sqlite3.connect(lab.path) as c:
        c.execute("INSERT INTO strategy_definitions (strategy_id, name, created_at) VALUES (?,?,?)", (sid, sp["name"], "2026-09-01"))
        c.execute("INSERT INTO strategy_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (uuid.uuid4().hex, sid, 1, 1, 1, sp["feature_registry_fingerprint"], cj, spec_hash or S.spec_hash(sp),
                   S.rules_hash(sp), readiness, "2026-09-01", None))
    return sid


def test_eligibility_rejections_are_structured():
    lab = X.Lab()
    good = norm()
    ok_id = lab.save(good)["strategy_id"]
    fwd = X.spec(entry={"logic": "ALL", "conditions": [{"feature": "research.catalyst", "op": "==", "value": "POSITIVE"}]})
    fwd_id = lab.save(fwd)["strategy_id"]
    uns = X.spec(entry={"logic": "ALL", "conditions": [{"feature": "portfolio.position_weight_pct", "op": "<", "value": 5}]})
    uns_id = lab.save(uns)["strategy_id"]
    mism = _raw_version(lab, good, fingerprint="0" * 64)
    tamper = _raw_version(lab, good, spec_hash="f" * 64)
    codes = {}
    for name, sid in (("ok", ok_id), ("fwd", fwd_id), ("uns", uns_id), ("mism", mism), ("tamper", tamper)):
        _, _, checks, errors = R.eligibility(lab.store, sid, 1)
        codes[name] = [e["code"] for e in errors]
    assert codes == {"ok": [], "fwd": ["FORWARD_TEST_ONLY"], "uns": ["UNSUPPORTED"], "mism": ["REGISTRY_MISMATCH"],
                     "tamper": ["INTEGRITY_ERROR"]}
    assert R.eligibility(lab.store, ok_id, 9)[3][0]["code"] == "NOT_FOUND"
    for sid in (fwd_id, uns_id, mism, tamper):
        with pytest.raises(BacktestError) as ei:
            R.start_run(lab.store, X.body(sid), now=X.NOW)
        assert ei.value.status == 422
    pf = R.preflight(lab.store, X.body(ok_id), now=X.NOW)
    assert pf["status"] == "DATA_REQUIRED" and {c["code"] for c in pf["checks"] if c["ok"]} >= {
        "STRATEGY_FOUND", "VERSION_FOUND", "VERSION_IMMUTABLE", "SPEC_HASH_VERIFIED", "RULES_HASH_VERIFIED",
        "REGISTRY_VERSION", "REGISTRY_FINGERPRINT", "SPEC_STILL_VALID", "READINESS", "DATE_RANGE_AND_COSTS"}


def test_request_validation_codes():
    lab = X.Lab()
    sid = lab.save(norm())["strategy_id"]
    bad = [X.body(sid, start="2026-05-01", end="2026-04-01"), X.body(sid, end="2026-09-28"),
           X.body(sid, start="1999-01-04"), X.body(sid, start="2012-01-03")]
    for b in bad:
        assert {e["code"] for e in R.preflight(lab.store, b, now=X.NOW)["errors"]} == {"INVALID_DATE_RANGE"}, b
    assert R.preflight(lab.store, X.body(sid, slippage_bps_per_side=900), now=X.NOW)["errors"][0]["code"] == "INVALID_CONFIG"


# ---- data cache ----------------------------------------------------------------------------------------------------------

def _frames(rows_by_sym):
    return {s: pd.DataFrame({"timestamp": pd.to_datetime([r[1] for r in rows], utc=True), "open": [r[2] for r in rows],
                             "high": [r[3] for r in rows], "low": [r[4] for r in rows], "close": [r[5] for r in rows],
                             "volume": [r[6] for r in rows]}) for s, rows in rows_by_sym.items()}


def test_download_uses_existing_fetch_function_and_stores_immutable_datasets():
    lab = X.Lab()
    days = X.sessions(D("2025-01-02"), D("2026-09-28"))          # includes "today" (2026-09-28) — must not be cached
    calls = []

    def fake_fetch(client, symbols, lookback_days):
        calls.append((tuple(symbols), lookback_days))
        return _frames({s: X.walk(s, days) for s in symbols if s != "NODATA"})
    out = B.fetch_and_store(lab.store, ["AMD", "SPY", "NODATA"], D("2025-02-01"), now=X.NOW, fetch_fn=fake_fetch, client=object())
    assert len(calls) == 1 and calls[0][0] == ("AMD", "NODATA", "SPY")          # one batched request
    assert out["AMD"]["last_session"] == "2026-09-25" and out["AMD"]["requested_end"] == "2026-09-27"
    assert out["AMD"]["first_session"] >= "2025-02-01" and out["NODATA"]["bar_count"] == 0
    assert out["AMD"]["feed"] == B.feed() and out["AMD"]["adjustment"] == "all"
    rows = lab.store.dataset_rows(out["AMD"]["dataset_id"], verify_hash=out["AMD"]["content_hash"])
    assert len(rows) == out["AMD"]["bar_count"]
    with sqlite3.connect(lab.path) as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE historical_daily_bars SET close = 1 WHERE dataset_id = ?", (out["AMD"]["dataset_id"],))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM historical_bar_datasets WHERE dataset_id = ?", (out["AMD"]["dataset_id"],))
    again = B.fetch_and_store(lab.store, ["AMD"], D("2025-02-01"), now=X.NOW, fetch_fn=fake_fetch, client=object())
    assert again["AMD"]["dataset_id"] != out["AMD"]["dataset_id"] and lab.store.dataset(out["AMD"]["dataset_id"])   # refresh = new
    with pytest.raises(BacktestError) as ei:
        B.fetch_and_store(lab.store, ["AMD"], D("2025-02-01"), now=X.NOW, fetch_fn=lambda *a, **k: {}, client=object())
    assert ei.value.code == "FETCH_FAILED"


def test_preflight_coverage_download_plan_and_cache_reuse():
    lab = X.Lab()
    sid = lab.save(norm())["strategy_id"]
    days = X.sessions(D("2024-12-02"), D("2026-09-25"))
    fetched = []

    def fake_fetch(client, symbols, lookback_days):
        fetched.append(tuple(symbols))
        rows = {s: X.walk(s, days) for s in symbols}
        rows["MU"] = [r for r in rows.get("MU", []) if r[0] != "2026-02-10"]       # one missing session
        return _frames({k: v for k, v in rows.items() if k in symbols})
    pf = R.preflight(lab.store, X.body(sid), now=X.NOW)
    assert pf["status"] == "DATA_REQUIRED" and pf["download"]["symbols"] == ["AMD", "MU", "NVDA", "SPY"]
    assert pf["download"]["fetch_start"] == "2025-02-01" and pf["download"]["estimated_bars"] > 1000
    pf2 = R.download(lab.store, X.body(sid), fetch_fn=fake_fetch, client=object(), now=X.NOW)
    assert pf2["status"] == "READY" and len(fetched) == 1
    mu = next(r for r in pf2["coverage"]["rows"] if r["symbol"] == "MU")
    assert mu["missing_sessions"] == 1 and mu["missing_examples"] == ["2026-02-10"] and mu["warmup_ok"]
    assert any(w["code"] == "MISSING_SESSIONS" and w["symbol"] == "MU" for w in pf2["warnings"])
    R.download(lab.store, X.body(sid), fetch_fn=fake_fetch, client=object(), now=X.NOW)       # nothing missing
    assert len(fetched) == 1                                                                    # cache reused
    assert R.preflight(lab.store, X.body(sid, start="2025-09-02"), now=X.NOW)["status"] == "READY"


def test_calendar_data_hole_blocks_the_run():
    lab = X.Lab()
    sid = lab.save(norm())["strategy_id"]
    days = X.sessions(D("2024-12-02"), D("2026-09-25"))
    holed = [d for d in days if not D("2026-03-09") <= d <= D("2026-03-13")]        # a whole week without SPY bars
    for s in ("AMD", "MU", "NVDA"):
        lab.cache(s, X.walk(s, days))
    lab.cache("SPY", X.walk("SPY", holed))
    pf = R.preflight(lab.store, X.body(sid), now=X.NOW)
    assert pf["status"] == "BLOCKED" and pf["errors"][0]["code"] == "DATA_UNAVAILABLE"
    assert pf["coverage"]["calendar_gaps"] == [{"after": "2026-03-06", "before": "2026-03-16", "weekdays_without_bar": 5}]
    ok = R.preflight(lab.store, X.body(sid, start="2026-03-17"), now=X.NOW)
    assert ok["status"] == "READY" and ok["coverage"]["calendar_gaps"] == []                 # single holidays are fine


def test_missing_universe_symbol_is_reported_not_ignored(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = X.Lab()
    sid = lab.save(norm(symbols=("AMD", "GONE")))["strategy_id"]
    days = X.sessions(D("2024-12-02"), D("2026-09-25"))
    for s in ("AMD", "SPY"):
        lab.cache(s, X.walk(s, days))
    lab.cache("GONE", [])
    pf = R.preflight(lab.store, X.body(sid), now=X.NOW)
    assert pf["status"] == "READY"
    assert any(w["code"] == "MISSING_SYMBOL_DATA" and w["symbol"] == "GONE" for w in pf["warnings"])
    run = lab.store.get_run(R.start_run(lab.store, X.body(sid, **SHORT), now=X.NOW)["run_id"])
    assert any(w["code"] == "MISSING_SYMBOL_DATA" for w in run["result"]["warnings"])
    assert run["result"]["benchmarks"]["universe_equal_weight"]["excluded"][0]["symbol"] == "GONE"


def test_warnings_for_biases_and_costs():
    sp = norm(entry={"logic": "ALL", "conditions": [{"feature": "market.breadth", "op": "==", "value": "STRONG"},
                                                    {"feature": "sector.context", "op": "==", "value": "SUPPORTIVE"},
                                                    {"feature": "stock.close", "op": ">", "value": 50}]})
    needs = SN.needs_for(sp)
    assert needs.basket == tuple(config.FALLBACK_UNIVERSE) and dict(needs.sector_etfs)["NVDA"] == "SOXX"
    w = {x["code"] for x in R.run_warnings(sp, needs, RunConfig(D("2025-01-02"), D("2026-01-02"), 1e5, 0, 0), None)}
    assert {"SURVIVORSHIP_BIAS_WARNING", "STATIC_SECTOR_MAP_WARNING", "ZERO_COST_ASSUMPTIONS", "ADJUSTED_PRICE_LEVEL_WARNING",
            "UNIVERSE_HINDSIGHT_WARNING", "DAILY_BAR_RESOLUTION"} <= w
    plain = norm()
    w2 = {x["code"] for x in R.run_warnings(plain, SN.needs_for(plain), RunConfig(D("2025-01-02"), D("2026-01-02"), 1e5, 5, 0), None)}
    assert not w2 & {"SURVIVORSHIP_BIAS_WARNING", "STATIC_SECTOR_MAP_WARNING", "ZERO_COST_ASSUMPTIONS", "ADJUSTED_PRICE_LEVEL_WARNING"}
    assert SN.needs_for(plain).basket == () and SN.needs_for(plain).index_symbols == ("SPY",)


# ---- runs: immutability, reproducibility, failure handling, determinism --------------------------------------------------

@pytest.fixture
def ready_lab(monkeypatch):
    monkeypatch.setattr(R, "RUN_INLINE", True)
    lab = X.Lab()
    days = X.sessions(D("2024-12-02"), D("2026-09-25"))
    for s in ("AMD", "MU", "NVDA", "SPY"):
        lab.cache(s, X.walk(s, days))
    lab.sid = lab.save(norm())["strategy_id"]
    return lab


def test_completed_run_is_immutable_and_reruns_are_new_identical_runs(ready_lab):
    lab = ready_lab
    a = R.start_run(lab.store, X.body(lab.sid, **SHORT), now=X.NOW)
    b = R.start_run(lab.store, X.body(lab.sid, **SHORT), now=X.NOW)
    ra, rb = lab.store.get_run(a["run_id"]), lab.store.get_run(b["run_id"])
    assert a["run_id"] != b["run_id"] and ra["config_hash"] == rb["config_hash"] and ra["data_hash"] == rb["data_hash"]
    strip = lambda r: {k: v for k, v in r["result"].items() if k not in ("timings",)}  # noqa: E731
    assert strip(ra) == strip(rb) and ra["point_in_time_safe"] is True
    ta = [{k: v for k, v in t.items() if k != "run_id"} for t in lab.store.trades(a["run_id"])]
    tb = [{k: v for k, v in t.items() if k != "run_id"} for t in lab.store.trades(b["run_id"])]
    assert ta == tb and lab.store.equity(a["run_id"]) == lab.store.equity(b["run_id"])
    c = R.start_run(lab.store, X.body(lab.sid, slippage_bps_per_side=5, **SHORT), now=X.NOW)
    assert lab.store.get_run(c["run_id"])["config_hash"] != ra["config_hash"]
    with sqlite3.connect(lab.path) as conn:
        for sql in ("UPDATE backtest_runs SET result_json = '{}' WHERE run_id = ?", "DELETE FROM backtest_runs WHERE run_id = ?",
                    "UPDATE backtest_runs SET status = 'RUNNING' WHERE run_id = ?", "DELETE FROM backtest_trades WHERE run_id = ?",
                    "UPDATE backtest_equity SET equity = 1 WHERE run_id = ?", "DELETE FROM backtest_signals WHERE run_id = ?"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, (a["run_id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO backtest_signals VALUES (?, 99999, '2026-01-05', 'AMD', 'ENTRY_SIGNAL', NULL, NULL, '{}')",
                         (a["run_id"],))


def test_viewing_a_stored_run_never_recomputes(ready_lab, monkeypatch):
    lab = ready_lab
    rid = R.start_run(lab.store, X.body(lab.sid, **SHORT), now=X.NOW)["run_id"]
    import backtest.engine as E
    monkeypatch.setattr(R, "simulate", lambda *a, **k: (_ for _ in ()).throw(AssertionError("recomputed")))
    monkeypatch.setattr(E, "simulate", lambda *a, **k: (_ for _ in ()).throw(AssertionError("recomputed")))
    monkeypatch.setattr(SN, "day_snapshot", lambda *a, **k: (_ for _ in ()).throw(AssertionError("recomputed")))
    run = lab.store.get_run(rid)
    assert run["status"] == "COMPLETED" and lab.store.trades(rid) is not None and lab.store.equity(rid)


def test_failed_run_keeps_code_and_no_results(ready_lab, monkeypatch):
    lab = ready_lab
    monkeypatch.setattr(R, "simulate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rid = R.start_run(lab.store, X.body(lab.sid, **SHORT), now=X.NOW)["run_id"]
    run = lab.store.get_run(rid)
    assert run["status"] == "FAILED" and run["error_code"] == "INTERNAL_ERROR" and "boom" in run["error_message"]
    assert run["result"] is None and run["point_in_time_safe"] is None
    assert lab.store.trades(rid) == [] and lab.store.equity(rid) == [] and lab.store.signals(rid) == []


def test_data_tampering_fails_the_run(ready_lab, monkeypatch):
    lab = ready_lab
    real = lab.store.dataset_rows
    monkeypatch.setattr(lab.store, "dataset_rows", lambda ds, verify_hash=None: real(ds, verify_hash="0" * 64))
    rid = R.start_run(lab.store, X.body(lab.sid, **SHORT), now=X.NOW)["run_id"]
    assert lab.store.get_run(rid)["error_code"] == "DATA_INTEGRITY_ERROR"


def test_interrupted_runs_are_failed_on_next_start(ready_lab):
    lab = ready_lab
    pf = R.preflight(lab.store, X.body(lab.sid), now=X.NOW)
    ref = pf["strategy"]
    rid = lab.store.create_run(ref, "{}", "a" * 64, "{}", "b" * 64, "{}", "3.2.0", "2026-01-05", "2026-09-25")
    lab.store.mark_running(rid)
    BacktestStore(lab.path)                                # a new server process opens the database
    run = lab.store.get_run(rid)
    assert run["status"] == "FAILED" and run["error_code"] == "INTERRUPTED" and run["result"] is None


def test_process_pool_and_in_process_snapshots_are_identical(ready_lab, monkeypatch):
    lab = ready_lab
    pf = R.preflight(lab.store, X.body(lab.sid, start="2026-06-01"), now=X.NOW)
    ds = {s: d["dataset_id"] for s, d in pf["_datasets"].items()}
    series = {s: BarSeries(s, lab.store.dataset_rows(i)) for s, i in ds.items()}
    days = [d for d in series["SPY"].dates if d >= D("2026-06-01")][:40]
    needs = SN.needs_for(norm())
    a, ma = SN.compute_snapshots(str(lab.path), ds, series, needs, days, workers=1)
    monkeypatch.setattr(SN, "POOL_MIN_UNITS", 1)
    b, mb = SN.compute_snapshots(str(lab.path), ds, series, needs, days, workers=2)
    assert ma["mode"] == "in_process" and mb["mode"] == "process_pool"
    assert json.dumps({k.isoformat(): v for k, v in a.items()}, sort_keys=True) == \
        json.dumps({k.isoformat(): v for k, v in b.items()}, sort_keys=True)


def test_busy_guard_allows_one_run_at_a_time(ready_lab):
    lab = ready_lab
    with R._job_lock:
        R._jobs["x" * 32] = {"state": "RUNNING"}
    try:
        with pytest.raises(BacktestError) as ei:
            R.start_run(lab.store, X.body(lab.sid, **SHORT), now=X.NOW)
        assert ei.value.code == "BACKTEST_BUSY" and ei.value.status == 409
    finally:
        R._jobs.pop("x" * 32, None)


# ---- 72 / 60: migration on a COPY of the real database ------------------------------------------------------------------

def _digest(conn, tables):
    out = {}
    for t in tables:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()
        h = hashlib.sha256((sql[0] if sql else "").encode())
        cols = ", ".join(f'"{r[1]}"' for r in conn.execute(f'PRAGMA table_info("{t}")'))   # also WITHOUT ROWID tables
        for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY {cols}'):
            h.update(repr(row).encode())
        out[t] = h.hexdigest()
    return out


def test_migration_on_copy_of_real_database_is_additive_and_idempotent():
    real = ROOT / "data" / "stock_agent.db"
    if not real.exists():
        pytest.skip("no real database")
    tmp = Path(tempfile.mkdtemp(prefix="bt32mig-")) / "copy.db"
    src = sqlite3.connect(f"file:{real.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(tmp))
    src.backup(dst)
    src.close()
    dst.close()
    conn = sqlite3.connect(str(tmp))
    before_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    old = sorted(before_tables)
    d0, uv0 = _digest(conn, old), conn.execute("PRAGMA user_version").fetchone()[0]
    trig0 = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    from database.backtest_migrations import run_backtest_migrations
    run_backtest_migrations(conn)
    run_backtest_migrations(conn)                         # idempotent
    after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    # the real database may already hold the Stage 3.2 tables (created on first use); then this proves idempotence on them
    assert after >= {"historical_bar_datasets", "historical_daily_bars", "backtest_runs", "backtest_trades",
                     "backtest_signals", "backtest_equity"}
    assert _digest(conn, old) == d0                       # research_snapshots / outcomes / strategy tables byte-identical
    assert conn.execute("PRAGMA user_version").fetchone()[0] == uv0
    trig1 = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    assert all(trig1[k] == v for k, v in trig0.items())   # Stage 3.1 immutability triggers untouched
    assert {"backtest_runs_final", "backtest_runs_no_delete", "backtest_trades_only_while_running"} <= set(trig1)
    conn.close()


def test_existing_migration_modules_are_unchanged_by_stage_32():
    assert "backtest" not in (ROOT / "database" / "migrations.py").read_text(encoding="utf-8").lower()
    s31 = (ROOT / "database" / "strategy_migrations.py").read_text(encoding="utf-8")
    assert not re.search(r"backtest_(runs|trades|signals|equity)|historical_(bar|daily)", s31)   # 3.2 tables live elsewhere
    assert F.fingerprint() == FROZEN_FINGERPRINT                 # Stage 3.1 registry (and so every saved hash) unchanged


# ---- 73: security -------------------------------------------------------------------------------------------------------

BT_FILES = ["backtest/__init__.py", "backtest/bars.py", "backtest/replay.py", "backtest/snapshots.py", "backtest/engine.py",
            "backtest/metrics.py", "backtest/runs.py", "backtest/store.py", "database/backtest_migrations.py",
            "api/routes/backtests.py", "frontend/backtest_lab.js"]


def test_no_code_execution_broker_or_ai_in_backtest_code():
    for f in BT_FILES:
        text = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"\beval\(|\bexec\(|(?<!re\.)compile\(|__import__|subprocess|os\.system|os\.popen|new Function|"
                             r"\bpickle\b|marshal|importlib", text), f
        assert not re.search(r"TradingClient|alpaca\.trading|place_?order|submit_?order|cancel_?order|/orders|webhook|"
                             r"anthropic|get_provider\(|from agents|import agents|from portfolio|import portfolio|rh_gateway|"
                             r"robinhood", text, re.I), f


def test_backtest_endpoints_are_exactly_the_research_set():
    from api.server import app
    paths = app.openapi()["paths"]
    mine = {p: set(ops) for p, ops in paths.items() if p.startswith("/api/backtests")}
    assert mine == {"/api/backtests/config": {"get"}, "/api/backtests/preflight": {"post"}, "/api/backtests/data": {"post"},
                    "/api/backtests": {"get", "post"}, "/api/backtests/{run_id}": {"get"},
                    "/api/backtests/{run_id}/ledger": {"get"}, "/api/backtests/{run_id}/signals": {"get"}}
    for p in mine:
        assert not re.search(r"(?i)order|trade|paper|execute|optimi|scan|rank|schedule", p), p


def test_strategy_fields_cannot_execute_anything_in_a_backtest(ready_lab):
    lab = ready_lab
    evil = norm(name="$(rm -rf /); __import__('os').system('x')")
    sid = lab.save({**evil, "description": "<script>alert(1)</script>"})["strategy_id"]
    rid = R.start_run(lab.store, X.body(sid, **SHORT), now=X.NOW)["run_id"]
    assert lab.store.get_run(rid)["status"] == "COMPLETED"
    js = (ROOT / "frontend" / "backtest_lab.js").read_text(encoding="utf-8")
    assert "esc(sel.name)" in js and "innerHTML = `" in js and "eval(" not in js


# ---- API round trip: 0 Claude calls, 0 broker calls, explicit data download only -------------------------------------------

@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from api.routes import portfolio as pr
    from api.server import app
    from backtest import store as bs
    from pf_fixtures import FakeGatewayHttp, fake_provider
    calls = []
    monkeypatch.setattr(ta, "get_provider", lambda: calls.append(1))
    http = FakeGatewayHttp()
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(http))
    lab = X.Lab()
    monkeypatch.setattr(bs, "get_backtest_store", lambda: lab.store)
    import api.routes.backtests as routes
    monkeypatch.setattr(routes, "get_backtest_store", lambda: lab.store)
    days = X.sessions(D("2024-12-02"), D("2026-09-25"))
    fetched = []

    def fake_fetch(client, symbols, lookback_days):
        fetched.append(tuple(symbols))
        return _frames({s: X.walk(s, days) for s in symbols})
    monkeypatch.setattr("data.market_data.fetch_daily_bars", fake_fetch)
    monkeypatch.setattr("data.market_data.get_data_client", lambda: object())
    monkeypatch.setattr(R, "RUN_INLINE", True)
    c = TestClient(app)
    c.lab, c.calls, c.http, c.fetched = lab, calls, http, fetched
    return c


def test_api_round_trip(api):
    lab = api.lab
    sid = lab.save(norm())["strategy_id"]
    fwd = lab.save(X.spec(entry={"logic": "ALL", "conditions": [{"feature": "research.view", "op": "==", "value": "BULLISH BIAS"}]}))["strategy_id"]
    cfgr = api.get("/api/backtests/config").json()
    assert cfgr["defaults"]["slippage_bps_per_side"] == 0 and cfgr["selection_policies"] and "zero_cost_label" in cfgr["cost_model"]
    b = X.body(sid, **SHORT)
    pf = api.post("/api/backtests/preflight", json=b).json()
    assert pf["status"] == "DATA_REQUIRED" and "_datasets" not in pf and api.fetched == []     # preflight never downloads
    assert api.post("/api/backtests", json=b).status_code == 422                                  # no data yet
    pf2 = api.post("/api/backtests/data", json=b).json()
    assert pf2["status"] == "READY" and len(api.fetched) == 1
    r = api.post("/api/backtests", json=b)
    assert r.status_code == 202
    rid = r.json()["run_id"]
    run = api.get(f"/api/backtests/{rid}").json()
    assert run["status"] == "COMPLETED" and run["point_in_time_safe"] is True and len(run["equity"]) == run["result"]["metrics"]["sessions"]
    led = api.get(f"/api/backtests/{rid}/ledger").json()["trades"]
    sig = api.get(f"/api/backtests/{rid}/signals").json()["events"]
    assert len([t for t in led if t["status"] == "CLOSED"]) == run["result"]["metrics"]["closed_trades"]
    assert sum(1 for e in sig if e["event_type"] == "ENTRY_FILLED") == run["result"]["audit"]["signals_filled"]
    assert api.get(f"/api/backtests?strategy_id={sid}&version_number=1").json()["runs"][0]["run_id"] == rid
    assert api.post("/api/backtests", json={**b, "strategy_id": fwd}).json()["status"] == "FORWARD_TEST_ONLY"
    assert api.post("/api/backtests", json={**b, "optimize": True}).status_code == 422             # strict body
    assert api.post("/api/backtests", json={**b, "selection_policy": "BEST"}).status_code == 422
    assert api.get("/api/backtests/../../etc").status_code in (404, 405)
    assert api.get("/api/backtests/" + "z" * 32).status_code == 404
    assert api.calls == [] and api.http.requests == []                                           # no Claude, no gateway


def test_ui_hooks_and_no_verdict_language():
    js = (ROOT / "frontend" / "backtest_lab.js").read_text(encoding="utf-8")
    sl = (ROOT / "frontend" / "strategy_lab.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"good strategy|bad strategy|best strategy|profitable strategy|use this|outperform|beat the|"
                         r"probabilit|expected return|winning strategy", js, re.I)
    assert "not recommended assumptions" in js
    assert 'id="bt-body"' in html and html.index("strategy_lab.js") < html.index("backtest_lab.js") and "backtest_lab.css" in html
    assert 'data-top="backtest"' in sl and "window.BacktestLab.select(btRef())" in sl
    assert "POINT-IN-TIME SAFE" in js and "run.point_in_time_safe" in js                           # badge only when true
    assert "optimistic, cost-free" in js and "BENCHMARK CONTEXT" in js and "OPEN POSITIONS AT END" in js
    assert re.search(r"setTimeout\(poll, \d+\)", js) and "setInterval" not in js                    # polls one run, then stops
