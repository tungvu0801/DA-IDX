"""Stage 4.8 — Historical Rotation Backtest (rotation_backtest/, database/portfolio_backtest_migrations.py,
api/routes/portfolio_backtest.py, frontend/portfolio_backtest.js). Fully offline: synthetic linear price series (exact
arithmetic), the Stage 4.7 engine unchanged, a scratch lab for the API; conftest blocks sockets and tripwires the Alpaca
wires; every provider / model path is tripwired in the API fixture. Nothing here can reach a broker."""
import ast
import math
import re
import sqlite3
from datetime import date, timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_EVEN
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bt_fixtures as X
import fw_fixtures as FL
from backtest.replay import BarSeries
from fit import current as FC
from fit import readonly as RO
from rotation import engine as E
from rotation import handoff as H
from rotation import store as S
from rotation import universe as U
from rotation_backtest import calendar as CAL
from rotation_backtest import config as C
from rotation_backtest import metrics as MX
from rotation_backtest import runner as RUN
from rotation_backtest import simulator as SIM
from rotation_backtest.store import PortfolioBacktestStore

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
Dt = date.fromisoformat
NOW = FL.at(Dt("2026-04-01"))                                      # "today" = 2026-04-02 New York → last complete session 2026-04-01
DAYS = X.sessions(date(2024, 6, 3), date(2026, 3, 31))
CONFIG = {"weights": {"momentum": "0.30", "trend": "0.25", "relative_strength": "0.25", "volatility": "0.10", "drawdown": "0", "liquidity": "0.10"},
          "portfolio_size": 2, "exit_rank": 3, "cash_buffer_pct": "0.05", "rebalance_threshold": "0.01", "max_turnover_per_rotation": "1.00",
          "max_position_weight": "0.50", "min_position_weight": "0.10", "min_price": "5.00", "min_avg_dollar_volume": "1000.00", "min_history_sessions": 252}
BODY = {"start_date": "2026-01-02", "end_date": "2026-03-31", "rebalance_frequency": "MONTHLY", "initial_cash": "100000.00",
        "transaction_cost_bps": "5", "slippage_bps": "5"}
LINEAR = {"AAA": lambda i: 100 + 0.1 * i, "BBB": lambda i: 50 + 0.02 * i, "CCC": lambda i: 80 - 0.03 * i, "SPY": lambda i: 400 + 0.05 * i}
GOLDEN_RESULT_HASH = "60693facab4e4ec49aaf6b10625a8d267a008a02219184246496dac3fadf8ec3"
GOLDEN_FINAL_EQUITY = "102929.52"


# ==================================================================================================================================
# synthetic data
# ==================================================================================================================================

def rows_for(sym, f, days=DAYS):
    """Exact, hand-computable bars: close = f(i); open = close - 0.05 (SPY: - 0.02); volume 1,000,000."""
    out = []
    for i, d in enumerate(days):
        c = round(f(i), 4)
        o = round(f(i) - (0.02 if sym == "SPY" else 0.05), 4)
        out.append((d.isoformat(), X.ts(d), o, round(c + 0.1, 4), round(o - 0.1, 4), c, 1_000_000.0))
    return out


def series(funcs=LINEAR, days=DAYS):
    return {s: BarSeries(s, rows_for(s, f, days)) for s, f in funcs.items()}


def cfg_row(**over):
    canon = S.normalise_config({**CONFIG, **over})
    return {"config_id": "0" * 32, "config_hash": S.config_hash(canon), "config": canon}


def universe(*syms):
    return U.resolve_universe("CUSTOM", None, list(syms or ("AAA", "BBB", "CCC")))


def definition(cfg=None, uni=None, **over):
    return C.normalise({**BODY, **over}, cfg or cfg_row(), uni or universe())


def simulate(ser=None, cfg=None, uni=None, **over):
    cfg = cfg or cfg_row()
    uni = uni or universe()
    return SIM.simulate(cfg, uni, ser if ser is not None else series(), definition(cfg, uni, **over), now=NOW)


def q2(d):
    return D(d).quantize(D("0.01"), rounding=ROUND_HALF_EVEN)


def q4(d):
    return D(d).quantize(D("0.0001"), rounding=ROUND_HALF_EVEN)


# ==================================================================================================================================
# GOLDEN end-to-end replay (every number recomputed here from the fixture arithmetic, then the pinned hash)
# ==================================================================================================================================

def test_golden_synthetic_end_to_end():
    ser = series()
    res = simulate(ser)
    assert res.status == "COMPLETED", (res.failure_code, res.failure_detail)
    sessions = [d for d in ser["SPY"].dates if Dt("2026-01-02") <= d <= Dt("2026-03-31")]
    assert res.sessions == sessions and len(sessions) == 61
    # schedule: the first session of each month → three signal sessions
    assert [b["signal_session"] for b in res.rebalances] == ["2026-01-02", "2026-02-02", "2026-03-02"]
    # ranks: AAA (steepest rise) 1, BBB 2, CCC (declining) 3 → portfolio_size 2 selects AAA + BBB
    r1 = res.rebalances[0]
    assert r1["engine_status"] == "VALID" and r1["executed"] == 1 and r1["execution_session"] == "2026-01-05"
    assert [(t["symbol"], t["rank"]) for t in r1["proposal"]["targets"]] == [("AAA", 1), ("BBB", 2)]
    T = Dt("2026-01-02")
    iT = DAYS.index(T)
    nxt = DAYS[iT + 1]
    assert nxt == Dt("2026-01-05") and nxt == CAL.next_session(sessions, T)
    equity_ref, w = D("100000.00"), D("0.475")                      # equal weight (1 - 0.05) / 2, residual 0 with both slots filled
    slip, cost = D("0.0005"), D("0.0005")
    expect, cash = [], D("100000.00")
    for sym in ("AAA", "BBB"):                                     # buys fill in rank order
        close_T = D(str(round(LINEAR[sym](iT), 4)))
        qty = int((q2(w * equity_ref) / close_T).to_integral_value(rounding=ROUND_FLOOR))          # whole shares, floored
        open_next = D(str(round(LINEAR[sym](iT + 1) - 0.05, 4)))
        fill = q4(open_next * (1 + slip))                                                          # slippage in the price
        notional = q2(fill * qty)
        fee = q2(notional * cost)                                                                  # cost debited separately
        cash -= notional + fee
        expect.append((sym, qty, open_next, fill, notional, fee, cash))
    assert expect[0][1] == 339 and expect[1][1] == 819
    assert [(t["symbol"], t["filled_qty"], t["open_price"], t["fill_price"], t["notional"], t["cost"], t["cash_after"]) for t in res.trades] == expect
    assert res.trades[0]["side"] == "BUY" and all(t["execution_session"] == "2026-01-05" and t["signal_session"] == "2026-01-02" for t in res.trades)
    # daily marking at completed closes (cash + qty × close); benchmark = initial × SPY close / first close
    def eq(i):
        return q2(cash + 339 * D(str(round(LINEAR["AAA"](i), 4))) + 819 * D(str(round(LINEAR["BBB"](i), 4))))
    assert res.equity[0]["equity"] == D("100000.00") and res.equity[0]["benchmark_index"] == D("100000.00")
    assert res.equity[1]["session_date"] == "2026-01-05" and res.equity[1]["equity"] == eq(iT + 1) == D("99963.00")
    assert res.equity[2]["equity"] == eq(iT + 2) == D("100013.28")
    spy0, spy1 = D(str(round(LINEAR["SPY"](iT), 4))), D(str(round(LINEAR["SPY"](iT + 1), 4)))
    assert res.equity[1]["benchmark_index"] == q2(D("100000.00") * spy1 / spy0)
    assert res.equity[-1]["equity"] == eq(DAYS.index(Dt("2026-03-31"))) == D(GOLDEN_FINAL_EQUITY)
    # later rebalances: HOLD within the 1 % threshold → no orders, no fills, nothing happens at the close of a signal day
    assert [(b["executed"], b["skip_reason"]) for b in res.rebalances[1:]] == [(0, SIM.NO_ORDERS)] * 2
    assert res.total_costs == D("47.45") and res.final_cash == cash == D("5051.28") and res.final_holdings == {"AAA": 339, "BBB": 819}
    assert res.result_hash() == GOLDEN_RESULT_HASH                                                 # pinned for future refactors
    m = MX.compute(definition(), res.equity, res.trades, res.rebalances, res.completed_positions, res.total_costs)
    assert m["total_return"] == pytest.approx(float(D(GOLDEN_FINAL_EQUITY) / D("100000") - 1))
    assert m["n_rebalances"] == 3 and m["n_rebalances_executed"] == 1 and m["n_trades"] == 2 and m["mean_turnover"] == pytest.approx(0.95)
    assert m["benchmark_total_return"] == pytest.approx(float(D(str(round(LINEAR["SPY"](DAYS.index(Dt("2026-03-31"))), 4))) / spy0 - 1), abs=1e-6)   # index is money (2 dp)


# ==================================================================================================================================
# 1–5: no future leakage · factor window · next-open execution · no same-close · benchmark alignment
# ==================================================================================================================================

def test_1_2_no_future_leakage_and_factor_window_ends_at_signal_date():
    ser = series()
    base = simulate(ser)
    T = Dt("2026-01-02")
    # every bar dated after T is heavily mutated: the decision at T (input hash + proposal) must be identical
    mutated = {}
    for s, x in ser.items():
        rows = [(d.isoformat(), str(b.timestamp), b.open, b.high, b.low, b.close, b.volume) if d <= T else
                (d.isoformat(), str(b.timestamp), b.open * 3, b.high * 9, b.low * 0.2, b.close * 0.1, b.volume * 50) for d, b in zip(x.dates, x.bars)]
        mutated[s] = BarSeries(s, rows)
    alt = simulate(mutated)
    assert alt.rebalances[0]["input_hash"] == base.rebalances[0]["input_hash"]
    assert alt.rebalances[0]["proposal_hash"] == base.rebalances[0]["proposal_hash"]
    assert alt.rebalances[0]["proposal"] == base.rebalances[0]["proposal"]
    # the engine only ever sees bars <= T
    t = SIM.truncate(ser["AAA"], T)
    assert t.dates[-1] == T and len(t) == DAYS.index(T) + 1 and all(d <= T for d in t.dates) and t.bar(T) is ser["AAA"].bar(T)
    assert SIM.truncate(ser["AAA"], Dt("2024-01-01")) is None
    cut = SIM.truncate_all(ser, T)
    assert all(x.dates[-1] <= T for x in cut.values())
    closes, _ = E._closes_to(cut["AAA"], T)
    assert len(closes) == DAYS.index(T) + 1 and closes[-1] == D(str(round(LINEAR["AAA"](DAYS.index(T)), 4)))
    # the same decision from the truncated and the full series (the engine slices to T itself)
    snap = SIM.SN.normalise(SIM.SN.LOCAL_SIMULATOR, None, "100000.00", [], {}, SIM.SN.OK)
    full = E.compute_rotation(cfg_row(), universe(), ser, snap, T, now=NOW, conflicts=set())
    part = E.compute_rotation(cfg_row(), universe(), cut, snap, T, now=NOW, conflicts=set())
    assert full.run["input_hash"] == part.run["input_hash"] and full.run["proposal_hash"] == part.run["proposal_hash"]


def test_3_4_next_open_execution_and_never_the_signal_close():
    res = simulate()
    for t in res.trades:
        sig, ex = Dt(t["signal_session"]), Dt(t["execution_session"])
        assert ex > sig and ex == DAYS[DAYS.index(sig) + 1]                                        # the very next session
        i = DAYS.index(ex)
        assert t["open_price"] == D(str(round(LINEAR[t["symbol"]](i) - 0.05, 4)))                   # T+1 OPEN
        assert t["fill_price"] == q4(t["open_price"] * D("1.0005"))
        assert t["fill_price"] != D(str(round(LINEAR[t["symbol"]](i - 1), 4)))                      # not T's close
        assert t["fill_price"] != D(str(round(LINEAR[t["symbol"]](i), 4)))                          # not T+1's close either
    for b in res.rebalances:
        assert b["execution_session"] is None or Dt(b["execution_session"]) > Dt(b["signal_session"])


def test_5_benchmark_alignment():
    ser = series()
    res = simulate(ser)
    assert [e["session_date"] for e in res.equity] == [d.isoformat() for d in ser["SPY"].dates if Dt("2026-01-02") <= d <= Dt("2026-03-31")]
    spy0 = D(str(round(LINEAR["SPY"](DAYS.index(Dt("2026-01-02"))), 4)))
    for e in res.equity:
        i = DAYS.index(Dt(e["session_date"]))
        assert e["benchmark_index"] == q2(D("100000.00") * D(str(round(LINEAR["SPY"](i), 4))) / spy0)
    m = MX.compute(definition(), res.equity, res.trades, res.rebalances, res.completed_positions, res.total_costs)
    assert m["benchmark"] == "SPY" and m["excess_return"] == pytest.approx(m["total_return"] - m["benchmark_total_return"])
    assert m["annualized_excess"] == pytest.approx(m["cagr"] - m["benchmark_cagr"])


# ==================================================================================================================================
# 6–7: scheduling
# ==================================================================================================================================

def test_6_7_weekly_and_monthly_schedules():
    sessions = [d for d in DAYS if Dt("2026-01-01") <= d <= Dt("2026-03-31")]
    monthly = CAL.schedule(sessions, "MONTHLY")
    assert monthly == ["2026-01-02", "2026-02-02", "2026-03-02"] or [d.isoformat() for d in monthly] == ["2026-01-02", "2026-02-02", "2026-03-02"]
    weekly = CAL.schedule(sessions, "WEEKLY")
    expected, seen = [], set()
    for d in sessions:                                             # independent rule: first session of each ISO (year, week)
        k = d.isocalendar()[:2]
        if k not in seen:
            seen.add(k)
            expected.append(d)
    assert weekly == expected and weekly[0] == Dt("2026-01-02") and all(a.isocalendar()[:2] != b.isocalendar()[:2] for a, b in zip(weekly, weekly[1:]))
    mlk = Dt("2026-01-19")                                          # a Monday holiday: that week starts on Tuesday
    if mlk not in sessions:
        assert Dt("2026-01-20") in weekly
    with pytest.raises(ValueError):
        CAL.schedule(sessions, "DAILY")
    assert CAL.next_session(sessions, sessions[-1]) is None and CAL.next_session(sessions, sessions[0]) == sessions[1]
    res = simulate(rebalance_frequency="WEEKLY")
    assert [b["signal_session"] for b in res.rebalances] == [d.isoformat() for d in CAL.schedule(res.sessions, "WEEKLY")]


# ==================================================================================================================================
# 8–13: costs · slippage · BUY / SELL cash accounting · whole shares · cash buffer
# ==================================================================================================================================

def test_8_9_10_12_13_buy_side_costs_slippage_whole_shares_and_cash_buffer():
    res = simulate(transaction_cost_bps="25", slippage_bps="10")
    for t in res.trades:
        assert t["fill_price"] == q4(t["open_price"] * D("1.0010")) and t["cost"] == q2(t["notional"] * D("0.0025"))
        assert t["notional"] == q2(t["fill_price"] * t["filled_qty"]) and t["filled_qty"] == t["requested_qty"]
    cash = D("100000.00") - sum((t["notional"] + t["cost"] for t in res.trades), D(0))
    assert res.trades[-1]["cash_after"] == cash == res.final_cash and cash > 0                      # residual cash preserved
    assert res.total_costs == sum((t["cost"] for t in res.trades), D(0))
    e1 = res.equity[1]
    assert e1["cash_weight"] == (e1["cash"] / e1["equity"]).quantize(D("0.000001"), rounding=ROUND_HALF_EVEN) and e1["cash_weight"] >= D("0.045")
    assert all(float(e["cash_weight"]) >= 0.045 for e in res.equity[1:])                            # the 5 % buffer stays in cash


def _collapse_after(T: date, *, sym="AAA"):
    """AAA rises until T, then collapses below the 5.00 minimum price → ineligible at the next signal → EXIT."""
    iT = DAYS.index(T)
    f = dict(LINEAR)
    f[sym] = lambda i: (100 + 0.1 * i) if i <= iT + 1 else 4.0
    return series(f)


def test_11_sell_cash_accounting_sells_first_and_completed_positions():
    T1 = Dt("2026-01-02")
    res = simulate(_collapse_after(T1))
    assert res.status == "COMPLETED", (res.failure_code, res.failure_detail)
    r2 = res.rebalances[1]
    assert r2["signal_session"] == "2026-02-02" and r2["executed"] == 1
    items = {i["symbol"]: i for i in r2["proposal"]["items"]}
    assert items["AAA"]["action"] == "EXIT" and items["AAA"]["est_qty_diff"] == 339
    fills = [t for t in res.trades if t["seq"] == 2]
    assert fills[0]["symbol"] == "AAA" and fills[0]["side"] == "SELL"                               # sells first
    sides = [t["side"] for t in fills]
    assert fills[0]["order_index"] == 0 and sides == sorted(sides, key=lambda x: x != "SELL")      # every SELL before any BUY
    sell = fills[0]
    i = DAYS.index(Dt(sell["execution_session"]))
    assert sell["open_price"] == D("3.9500") and sell["fill_price"] == q4(D("3.95") * D("0.9995")) and sell["filled_qty"] == 339
    before = [t for t in res.trades if t["seq"] == 1][-1]["cash_after"]
    assert sell["notional"] == q2(sell["fill_price"] * 339) and sell["cost"] == q2(sell["notional"] * D("0.0005"))
    assert sell["cash_after"] == before + sell["notional"] - sell["cost"] and sell["qty_after"] == 0
    assert sell["realised_pnl"] < 0 and sell["realised_pnl"] == q2(sell["notional"] - sell["cost"] - (D("47432.85") + D("23.72")))
    assert res.completed_positions == [{"symbol": "AAA", "opened_session": "2026-01-05", "closed_session": sell["execution_session"],
                                        "holding_sessions": i - DAYS.index(Dt("2026-01-05")), "realised_pnl": sell["realised_pnl"], "win": False}]
    m = MX.compute(definition(), res.equity, res.trades, res.rebalances, res.completed_positions, res.total_costs)
    assert m["n_completed_positions"] == 1 and m["win_rate"] == 0.0 and m["average_holding_sessions"] == float(i - DAYS.index(Dt("2026-01-05")))
    assert "AAA" not in res.final_holdings


# ==================================================================================================================================
# 14–17: rank buffer · turnover limit · no shorting · no leverage
# ==================================================================================================================================

def _flip_after(T: date):
    """AAA leads until T then goes flat; BBB accelerates after T → BBB rank 1, AAA rank 2 at the next signal."""
    iT = DAYS.index(T)
    f = dict(LINEAR)
    f["AAA"] = lambda i: (100 + 0.1 * i) if i <= iT else 100 + 0.1 * iT
    f["BBB"] = lambda i: (50 + 0.02 * i) if i <= iT else 50 + 0.02 * iT + 0.5 * (i - iT)
    return series(f)


def test_14_rank_buffer_through_historical_replay():
    T1 = Dt("2026-01-02")
    ser = _flip_after(T1)
    retained = simulate(ser, cfg_row(portfolio_size=1, exit_rank=2, max_position_weight="0.95"))
    r1, r2 = retained.rebalances[0], retained.rebalances[1]
    assert [(t["symbol"], t["rank"], t["reason"]) for t in r1["proposal"]["targets"]] == [("AAA", 1, "TOP_N")]
    assert [(t["symbol"], t["rank"], t["reason"]) for t in r2["proposal"]["targets"]] == [("AAA", 2, "RETAINED_RANK_BUFFER")]
    assert r2["executed"] == 0 and r2["skip_reason"] == SIM.NO_ORDERS and "AAA" in retained.final_holdings
    strict = simulate(ser, cfg_row(portfolio_size=1, exit_rank=1, max_position_weight="0.95"))
    r2s = strict.rebalances[1]
    assert [(t["symbol"], t["rank"]) for t in r2s["proposal"]["targets"]] == [("BBB", 1)]
    items = {i["symbol"]: i for i in r2s["proposal"]["items"]}
    assert items["AAA"]["action"] == "EXIT" and items["BBB"]["action"] == "ADD"
    seq2 = [t for t in strict.trades if t["seq"] == 2]
    assert [(t["symbol"], t["side"]) for t in seq2] == [("AAA", "SELL"), ("BBB", "BUY")] and seq2[0]["qty_after"] == 0
    net = sum((t["filled_qty"] if t["side"] == "BUY" else -t["filled_qty"]) for t in strict.trades if t["symbol"] == "BBB")
    assert strict.final_holdings == {"BBB": net} and net > 0


def test_15_turnover_limit_blocks_the_whole_rebalance():
    res = simulate(cfg=cfg_row(max_turnover_per_rotation="0.10"))
    assert res.status == "COMPLETED" and res.trades == [] and res.final_holdings == {}
    assert all(b["engine_status"] == "TURNOVER_LIMIT_EXCEEDED" and b["executed"] == 0 and b["skip_reason"] == SIM.ENGINE_STATUS for b in res.rebalances)
    assert all(e["equity"] == D("100000.00") for e in res.equity)


def test_16_17_no_shorting_no_leverage():
    for res in (simulate(), simulate(_collapse_after(Dt("2026-01-02"))), simulate(_flip_after(Dt("2026-01-02")), cfg_row(portfolio_size=1, exit_rank=1, max_position_weight="0.95"))):
        assert res.status == "COMPLETED"
        held = {}
        for t in res.trades:
            if t["side"] == "SELL":
                assert t["filled_qty"] <= held.get(t["symbol"], 0)                                   # never more than held
                held[t["symbol"]] = held.get(t["symbol"], 0) - t["filled_qty"]
            else:
                held[t["symbol"]] = held.get(t["symbol"], 0) + t["filled_qty"]
            assert t["qty_after"] == held[t["symbol"]] >= 0 and t["cash_after"] >= 0
        assert all(v >= 0 for v in res.final_holdings.values())
        assert all(e["cash"] >= 0 and e["positions_value"] <= e["equity"] for e in res.equity)       # no borrowed cash
    # a BUY the cash cannot fund is cut to the affordable whole shares (never negative cash)
    costly = simulate(slippage_bps="500", transaction_cost_bps="500")                            # 5 % + 5 %: the second buy no longer fits
    assert costly.status == "COMPLETED" and all(t["cash_after"] >= 0 for t in costly.trades)
    limited = [t for t in costly.trades if t["note"] == SIM.CASH_LIMITED]
    assert limited and all(0 < t["filled_qty"] < t["requested_qty"] for t in limited)
    assert all(t["notional"] == q2(t["fill_price"] * t["filled_qty"]) for t in costly.trades)


# ==================================================================================================================================
# 18–19, 34: fail closed on missing / malformed data
# ==================================================================================================================================

def test_18_missing_open_hard_fail():
    ser = series()
    rows = [(d.isoformat(), str(b.timestamp), b.open, b.high, b.low, b.close, b.volume) for d, b in zip(ser["AAA"].dates, ser["AAA"].bars) if d != Dt("2026-01-05")]
    ser["AAA"] = BarSeries("AAA", rows)
    res = simulate(ser)
    assert res.status == "FAILED" and res.failure_code == "MISSING_OPEN" and "AAA" in res.failure_detail and "2026-01-05" in res.failure_detail
    assert res.result_hash() is None


def test_19_missing_benchmark_hard_fail():
    ser = series()
    rows = [(d.isoformat(), str(b.timestamp), b.open, b.high, b.low, b.close, b.volume) for d, b in zip(ser["SPY"].dates, ser["SPY"].bars) if d != Dt("2026-02-10")]
    ser["SPY"] = BarSeries("SPY", rows)
    res = simulate(ser)
    assert res.status == "FAILED" and res.failure_code == "MISSING_BENCHMARK" and "2026-02-10" in res.failure_detail
    res2 = simulate({k: v for k, v in series().items() if k != "SPY"})
    assert res2.status == "FAILED" and res2.failure_code == "MISSING_BENCHMARK"
    assert simulate(start_date="2026-04-06", end_date="2026-04-10").failure_code == "NO_SESSIONS"


def test_34_malformed_or_incomplete_history_fails_closed():
    ser = series()
    rows = [(d.isoformat(), str(b.timestamp), b.open, b.high, b.low, b.close, b.volume) for d, b in zip(ser["BBB"].dates, ser["BBB"].bars) if d != Dt("2026-02-17")]
    ser["BBB"] = BarSeries("BBB", rows)                            # a held symbol without a close on a marking day
    res = simulate(ser)
    assert res.status == "FAILED" and res.failure_code == "MISSING_CLOSE" and "BBB" in res.failure_detail
    bad = series()
    rows = [(d.isoformat(), str(b.timestamp), (0.0 if d == Dt("2026-01-05") else b.open), b.high, b.low, b.close, b.volume) for d, b in zip(bad["AAA"].dates, bad["AAA"].bars)]
    bad["AAA"] = BarSeries("AAA", rows)                            # a non-positive open is not a price
    assert simulate(bad).failure_code == "MISSING_OPEN"
    assert simulate({"AAA": series()["AAA"], "SPY": series()["SPY"]}, uni=universe("AAA", "BBB", "CCC")).status == "COMPLETED"   # absent symbols: ineligible, not fatal
    with pytest.raises(C.BacktestConfigError) as e:
        definition(end_date="2025-12-31")
    assert e.value.code == "INVALID_DATE_RANGE"
    for field, value, code in (("transaction_cost_bps", "-1", "OUT_OF_RANGE"), ("slippage_bps", "abc", "INVALID_DECIMAL"), ("initial_cash", "1", "OUT_OF_RANGE"),
                               ("rebalance_frequency", "DAILY", "INVALID_FREQUENCY"), ("start_date", "2026/01/02", "INVALID_DATE")):
        with pytest.raises(C.BacktestConfigError) as e:
            definition(**{field: value})
        assert e.value.code == code


# ==================================================================================================================================
# 20–22: determinism · stable hashes · append-only storage
# ==================================================================================================================================

def test_20_21_deterministic_runs_and_stable_hashes():
    a, b = simulate(), simulate()
    assert a.result_hash() == b.result_hash() == GOLDEN_RESULT_HASH and a.equity == b.equity and a.trades == b.trades
    d1, d2 = definition(), definition()
    assert C.definition_hash(d1) == C.definition_hash(d2) and re.match(r"^[0-9a-f]{64}$", C.definition_hash(d1))
    assert C.definition_hash(definition(slippage_bps="6")) != C.definition_hash(d1)
    assert C.definition_hash(definition(cfg_row(exit_rank=2))) != C.definition_hash(d1)              # the rotation version is part of the definition
    assert d1["execution_price"] == "NEXT_OPEN" and d1["benchmark"] == "SPY" and d1["whole_shares"] is True and d1["cash_interest"] == "0"
    assert simulate(slippage_bps="6").result_hash() != a.result_hash()


def test_22_storage_is_additive_append_only_and_idempotent(tmp_path):
    from database import portfolio_backtest_migrations as M
    db = tmp_path / "bt48.db"
    with sqlite3.connect(str(db)) as c:
        c.execute("PRAGMA user_version = 7")
        for _ in range(3):
            M.run_portfolio_backtest_migrations(c)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 7
        names = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(M.TABLES) <= names and not any("alpaca" in n or "paper_" in n for n in names)
        sql = " ".join(s for (s,) in c.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        assert "DROP" not in sql.upper() and "ALTER" not in sql.upper()
    st = PortfolioBacktestStore(db)
    assert st.exists() and st.runs() == []
    canon = definition()
    row = st.ensure_config(canon, NOW)
    assert row["created"] is True and st.ensure_config(canon, NOW)["created"] is False and len(st.configs()) == 1
    res = simulate()
    run = {"backtest_config_id": row["backtest_config_id"], "backtest_config_hash": row["backtest_config_hash"], "config_id": canon["config_id"],
           "config_hash": canon["config_hash"], "universe_hash": canon["universe_hash"], "status": "COMPLETED", "run_at": NOW.isoformat(),
           "completed_at": NOW.isoformat(), "first_session": "2026-01-02", "last_session": "2026-03-31", "n_sessions": len(res.equity),
           "n_rebalances": 3, "n_rebalances_executed": 1, "n_trades": 2, "initial_cash": "100000.00", "final_equity": res.equity[-1]["equity"],
           "final_cash": res.final_cash, "total_costs": res.total_costs, "data_hash": "a" * 64, "bars_json": {"AAA": "b" * 64}, "result_hash": res.result_hash(),
           "market_data_requests": 0, "universe_note": C.UNIVERSE_NOTE}
    rid = st.insert_run(run, res.rebalances, res.trades, res.equity, {"total_return": 0.1})
    assert st.run(rid)["status"] == "COMPLETED" and len(st.equity(rid)) == 61 and len(st.trades(rid)) == 2 and len(st.rebalances(rid)) == 3
    assert st.metrics(rid)["metrics"] == {"total_return": 0.1} and st.metrics(rid)["conventions"] == MX.CONVENTIONS
    assert st.rebalances(rid)[0]["proposal"]["targets"][0]["symbol"] == "AAA"
    with sqlite3.connect(str(db)) as c:
        for sql in ("UPDATE portfolio_backtest_runs SET status = 'FAILED'", "DELETE FROM portfolio_backtest_runs", "DELETE FROM portfolio_backtest_trades",
                    "UPDATE portfolio_backtest_equity SET equity = '1'", "DELETE FROM portfolio_backtest_configs", "UPDATE portfolio_backtest_metrics SET metrics_json = '{}'"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)
        with pytest.raises(sqlite3.DatabaseError):                                                    # a FAILED run must carry its reason
            c.execute("INSERT INTO portfolio_backtest_runs (run_id, backtest_config_id, backtest_config_hash, config_id, config_hash, universe_hash, "
                      "engine_version, status, run_at, completed_at, n_sessions, n_rebalances, n_rebalances_executed, n_trades, initial_cash, data_hash, "
                      "bars_json, market_data_requests, universe_note) VALUES ('f'*32, ?, ?, ?, ?, ?, '4.8.0', 'FAILED', 'a', 'b', 0, 0, 0, 0, '1', ?, '{}', 0, 'n')",
                      (row["backtest_config_id"], row["backtest_config_hash"], canon["config_id"], canon["config_hash"], canon["universe_hash"], "a" * 64))


# ==================================================================================================================================
# 23–26: metrics with known synthetic series
# ==================================================================================================================================

def test_23_24_25_26_metrics_known_cases():
    eq = [D("100"), D("102"), D("100.98"), D("101.9898")]           # returns 0, +2 %, −1 %, +1 %
    rets = MX.daily_returns(eq)
    assert rets == pytest.approx([0.0, 0.02, -0.01, 0.01])
    assert MX.total_return(eq, D("100")) == pytest.approx(0.019898)
    assert MX.cagr(eq, D("100")) == pytest.approx(1.019898 ** (252 / 3) - 1)                         # 3 sessions after the first
    sd = math.sqrt(((-0.005) ** 2 + 0.015 ** 2 + (-0.015) ** 2 + 0.005 ** 2) / 3)
    assert MX.annualized_volatility(rets) == pytest.approx(sd * math.sqrt(252))
    assert MX.sharpe(rets) == pytest.approx(0.005 / sd * math.sqrt(252))
    assert MX.sortino(rets) == pytest.approx(0.005 / 0.005 * math.sqrt(252))                         # downside = sqrt(0.0001 / 4)
    dd = MX.max_drawdown(eq)
    assert dd["max_drawdown"] == pytest.approx(100.98 / 102 - 1) and dd["max_drawdown_sessions"] == 2
    assert MX.max_drawdown([D("100"), D("90"), D("95"), D("101"), D("80")]) == {"max_drawdown": pytest.approx(80 / 101 - 1), "max_drawdown_sessions": 2}
    assert MX.sharpe([0.0, 0.0]) is None and MX.sortino([0.0, 0.01]) is None and MX.cagr([D("100")], D("100")) is None
    assert MX.annualized_volatility([0.1]) is None
    months = MX.monthly_returns(["2026-01-05", "2026-01-30", "2026-02-02", "2026-02-27"], [D("100"), D("110"), D("99"), D("108.9")])
    assert months == [{"month": "2026-01", "return": pytest.approx(0.10)}, {"month": "2026-02", "return": pytest.approx(0.099 / 1.10 - 0.1 + 0.1 - 0.01)}] or \
        [m["month"] for m in months] == ["2026-01", "2026-02"] and months[1]["return"] == pytest.approx(108.9 / 110 - 1)
    assert MX.worst_rolling([D(100 + i) for i in range(64)], 63) == pytest.approx(163 / 100 - 1) and MX.worst_rolling([D(1)] * 63) is None
    assert set(MX.CONVENTIONS) >= {"returns", "cagr", "sharpe", "sortino", "max_drawdown", "benchmark", "turnover", "win_rate"}


# ==================================================================================================================================
# 27, 31–33: isolation — no broker import, research-only API, no handoff from history, limitation documented
# ==================================================================================================================================

def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_27_no_broker_robinhood_or_model_module_import():
    files = list((ROOT / "rotation_backtest").glob("*.py")) + [ROOT / "api" / "routes" / "portfolio_backtest.py", ROOT / "database" / "portfolio_backtest_migrations.py"]
    banned = ("paper", "portfolio", "agents", "ai_explain", "anthropic", "alpaca", "robinhood", "rh_gateway", "requests", "httpx", "research_flow", "notifications", "forward.automation")
    for f in files:
        mods = _imports(f)
        hits = [m for m in mods if m == "paper" or m.startswith("paper.") or any(m == b or m.startswith(b + ".") for b in banned)]
        assert not hits, (f.name, hits)
    assert "from rotation import engine as E" in (ROOT / "rotation_backtest" / "simulator.py").read_text(encoding="utf-8")   # the ONE strategy engine is reused
    src = (ROOT / "rotation_backtest" / "simulator.py").read_text(encoding="utf-8")
    assert "compute_rotation(" in src and "def rank" not in src and "percentile" not in src          # no second ranking / allocation code
    assert src.count("E.compute_rotation(") == 1


def test_31_32_api_is_research_only_and_history_cannot_hand_off():
    from api.routes import portfolio_backtest as PB
    paths = [(sorted(r.methods)[0], r.path) for r in PB.router.routes]
    assert all("order" not in p.lower() and "preview" not in p.lower() and "confirm" not in p.lower() and "handoff" not in p.lower() for _, p in paths)
    assert [p for m, p in paths if m == "POST"] == ["/api/rotation-replay/run"]
    route_src = (ROOT / "api" / "routes" / "portfolio_backtest.py").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "portfolio_backtest.js").read_text(encoding="utf-8")
    for banned in ("handoff", "Prepare Paper", "AlpacaOrders", "alpaca-paper", "data-apo-", "prefill", "MutationObserver", "setView("):
        assert banned not in route_src and banned not in js, banned
    assert js.count("fetch(") == 1 and "/api/rotation-replay" in js and "/api/alpaca" not in js and "backtest" not in js.split("const BASE")[1].split(";")[0]
    # a historical run row is never an eligible Stage 4.7 handoff source
    res = simulate()
    fake_run = {"status": "VALID", "portfolio_source": None, **{k: v for k, v in res.rebalances[0].items() if k != "proposal"}}
    item = {"symbol": "AAA", "action": "ADD", "handoff_allowed": 1, "est_qty_diff": 339}
    out = H.evaluate(fake_run, item)
    assert out["eligible"] is False and out["mode"] == H.MODE_DISPLAY and out["draft"] is None and out["reason"] == "INVALID_SOURCE"
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="pbt-body"' in html and 'src="portfolio_backtest.js"' in html and 'href="portfolio_backtest.css"' in html


def test_33_static_universe_limitation_is_documented():
    note = C.UNIVERSE_NOTE.lower()
    assert "survivorship" in note and "static universe" in note and "not" in note
    assert C.public(definition())["universe_note"] == C.UNIVERSE_NOTE
    design = (ROOT / "DESIGN_48_HISTORICAL_BACKTEST.md").read_text(encoding="utf-8").lower()
    assert "survivorship" in design and "next" in design and "open" in design and "no future leakage" in design


# ==================================================================================================================================
# API (scratch lab, fixed clock, synthetic bars, every broker / model path tripwired): 28–31 and the UI contract
# ==================================================================================================================================

SYMS = ("AAA", "BBB", "CCC")
BASE = "/api/rotation-replay"


@pytest.fixture
def lab():
    m = FL.Market(SYMS + ("SPY",), start=date(2024, 6, 3), end=date(2026, 3, 31), vol=0.0)
    for s, f in LINEAR.items():
        for i, d in enumerate(m.days):
            c = round(f(i), 4)
            o = round(f(i) - (0.02 if s == "SPY" else 0.05), 4)
            m.set_bar(s, d, o, round(c + 0.1, 4), round(o - 0.1, 4), c, 1_000_000.0)
    lb = FL.FLab(m)
    lb.market = m
    return lb


@pytest.fixture
def api(monkeypatch, lab):
    import agents.technical_agent as ta
    from ai_explain import service as AX
    from api.routes import portfolio as pr
    from api.routes import portfolio_backtest as PB
    from api.server import app
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr(PB, "NOW_FN", lambda: NOW)
    monkeypatch.setattr(PB, "FETCH", {"fetch_fn": lab.market.fetch, "client": object(), "cache": FC.BarCache()})
    hits = []

    def fail(what):
        def _f(*a, **k):
            hits.append(what)
            raise RuntimeError(what)
        return _f
    monkeypatch.setattr(ta, "get_provider", fail("claude research"))
    monkeypatch.setattr(AX, "get_provider", fail("claude explanation"))
    monkeypatch.setattr(pr, "provider_factory", fail("robinhood provider"))
    c = TestClient(app)
    c.hits, c.lab = hits, lab
    return c


def rotation_cfg(api, **over):
    body = {"name": "bt48", "weights": dict(CONFIG["weights"]), **{k: v for k, v in CONFIG.items() if k != "weights"}, **over}
    r = api.post("/api/portfolio-rotation/configs", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def run_api(api, c, **over):
    return api.post(f"{BASE}/run", json={"config_id": c["config_id"], "config_hash": c["config_hash"], "universe": {"source": "CUSTOM", "symbols": list(SYMS)}, **BODY, **over})


def test_api_config_run_persist_and_reads(api):
    r = api.get(f"{BASE}/config")
    assert r.status_code == 200
    b = r.json()
    assert b["benchmark"] == "SPY" and b["execution_price"] == "NEXT_OPEN" and b["frequencies"] == ["WEEKLY", "MONTHLY"] and "survivorship" in b["universe_note"].lower()
    assert b["rotation_configs"] == [] and b["label"].startswith("HISTORICAL ROTATION BACKTEST")
    c = rotation_cfg(api)
    api.lab.market.calls.clear()
    r = run_api(api, c)
    assert r.status_code == 200, r.text
    out = r.json()
    run = out["run"]
    assert run["status"] == "COMPLETED" and run["n_sessions"] == 61 and run["n_rebalances"] == 3 and run["n_trades"] == 2
    assert run["result_hash"] == GOLDEN_RESULT_HASH and run["final_equity"] == GOLDEN_FINAL_EQUITY and run["market_data_requests"] == 1
    assert len(api.lab.market.calls) == 1 and set(api.lab.market.calls[0]) == set(SYMS) | {"SPY"}       # ONE batched market-data request
    assert api.hits == []                                                                             # 28–30: no Robinhood, no Alpaca, no Claude
    assert out["definition"]["backtest_config_hash"] == run["backtest_config_hash"] and out["metrics"]["n_trades"] == 2
    assert "handoff" not in str(out).lower() and "survivorship" in run["universe_note"].lower()
    rid = run["run_id"]
    assert api.get(f"{BASE}/runs").json()["runs"][0]["run_id"] == rid
    d = api.get(f"{BASE}/runs/{rid}").json()
    assert d["run"]["status"] == "COMPLETED" and d["definition"]["start_date"] == "2026-01-02" and d["universe"] == list(SYMS) and set(d["bars"]) == set(SYMS) | {"SPY"}
    assert d["metrics"]["total_return"] == pytest.approx(float(D(GOLDEN_FINAL_EQUITY) / D("100000") - 1)) and "sharpe" in d["conventions"]
    e = api.get(f"{BASE}/runs/{rid}/equity").json()
    assert len(e["equity"]) == 61 and e["equity"][0]["equity"] == "100000.00" and e["equity"][-1]["equity"] == GOLDEN_FINAL_EQUITY and e["benchmark"] == "SPY"
    t = api.get(f"{BASE}/runs/{rid}/fills").json()
    assert [(x["symbol"], x["side"], x["filled_qty"], x["fill_price"]) for x in t["trades"]] == [("AAA", "BUY", 339, "139.9199"), ("BBB", "BUY", 819, "57.9590")]
    rb = api.get(f"{BASE}/runs/{rid}/rebalances").json()["rebalances"]
    assert [(x["seq"], x["executed"], x["skip_reason"]) for x in rb] == [(1, 1, None), (2, 0, "NO_ORDERS"), (3, 0, "NO_ORDERS")]
    assert rb[0]["proposal"]["targets"][0] == {"symbol": "AAA", "rank": 1, "target_weight": "0.475000", "est_target_qty": 339, "reason": "TOP_N"}
    # the second identical run reuses the definition row and reproduces the hash (cache: 0 new market-data requests)
    r2 = run_api(api, c)
    assert r2.json()["run"]["result_hash"] == GOLDEN_RESULT_HASH and r2.json()["run"]["market_data_requests"] == 0
    assert len(api.get(f"{BASE}/runs").json()["runs"]) == 2 and len(PortfolioBacktestStore(Path(api.lab.path)).configs()) == 1
    assert api.get(f"{BASE}/config").json()["rotation_configs"][0]["config_id"] == c["config_id"]


def test_api_validation_and_failures(api):
    c = rotation_cfg(api)
    assert run_api(api, c, end_date="2026-04-03").status_code == 422                                  # beyond the last complete session
    assert run_api(api, c, end_date="2026-04-03").json()["status"] == "INVALID_DATE_RANGE"
    assert run_api(api, c, rebalance_frequency="DAILY").status_code == 422
    assert run_api(api, c, transaction_cost_bps="-5").json()["status"] == "OUT_OF_RANGE"
    bad = api.post(f"{BASE}/run", json={"config_id": "1" * 32, "config_hash": c["config_hash"], "universe": {"source": "CUSTOM", "symbols": ["AAA"]}, **BODY})
    assert bad.status_code == 404 and bad.json()["status"] == "INVALID_CONFIG"
    mism = api.post(f"{BASE}/run", json={"config_id": c["config_id"], "config_hash": "0" * 64, "universe": {"source": "CUSTOM", "symbols": ["AAA"]}, **BODY})
    assert mism.status_code == 409
    extra = api.post(f"{BASE}/run", json={"config_id": c["config_id"], "config_hash": c["config_hash"], "universe": {"source": "CUSTOM", "symbols": ["AAA"]}, **BODY, "side": "BUY"})
    assert extra.status_code == 422                                                                   # strict body: no order fields exist
    assert api.get(f"{BASE}/runs/{'z' * 32}").status_code == 404 and api.get(f"{BASE}/runs/short/equity").status_code == 404
    # a benchmark gap inside the range is a persisted FAILED run, never an interpolated price
    api.lab.market.drop("SPY", Dt("2026-02-10"))
    from api.routes import portfolio_backtest as PB
    PB.FETCH["cache"] = FC.BarCache()
    r = run_api(api, c, start_date="2026-02-02")
    assert r.status_code == 200 and r.json()["run"]["status"] == "FAILED" and r.json()["run"]["failure_code"] == "MISSING_BENCHMARK"
    assert r.json()["metrics"] is None and api.get(f"{BASE}/runs/{r.json()['run']['run_id']}/equity").json()["equity"] == []
    assert api.hits == []


def test_api_weekly_run_and_runtime(api):
    import time
    c = rotation_cfg(api)
    t0 = time.perf_counter()
    r = run_api(api, c, rebalance_frequency="WEEKLY", start_date="2025-06-02")
    assert r.status_code == 200 and r.json()["run"]["status"] == "COMPLETED"
    assert r.json()["run"]["n_rebalances"] == len(CAL.schedule([d for d in DAYS if Dt("2025-06-02") <= d <= Dt("2026-03-31")], "WEEKLY"))
    assert time.perf_counter() - t0 < 30                                                              # correctness first; still well within budget
