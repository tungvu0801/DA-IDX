"""
rotation_backtest/simulator.py — the deterministic historical replay (DESIGN_48 §2, §6, §7). Pure: no I/O, no clock, no
randomness, no network. Every rebalance decision comes from the EXISTING rotation.engine.compute_rotation; this module
only feeds it point-in-time data and the simulated portfolio, then executes and accounts.

Order of events on each session s of the range (benchmark calendar):
  OPEN of s   pending orders from the previous signal session fill at s's open: SELLs first (alphabetical), then BUYs in
              rank order; BUY fill = open × (1 + slippage), SELL fill = open × (1 − slippage); cost = |notional| ×
              transaction_cost_bps / 10⁴ debited separately; a BUY is cut to the whole shares the cash allows.
  CLOSE of s  holdings are marked at s's close → one equity observation (benchmark index alongside).
              If s is a schedule date: the engine decides on bars ≤ s and the portfolio as of s's close; its orders become
              pending for the next session's open. The last session cannot have a next session → NO_NEXT_SESSION.
Fail closed: a held symbol without a close on a marking day (MISSING_CLOSE) or a pending symbol without a bar on the
execution day (MISSING_OPEN), a benchmark gap (MISSING_BENCHMARK) or an empty calendar (NO_SESSIONS) stops the run with a
FAILED result; prices are never interpolated, carried forward or replaced.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_EVEN
from typing import Dict, List, Optional

from backtest.replay import BarSeries
from rotation import engine as E
from rotation import factors as F
from rotation import rules as R
from rotation import snapshots as SN
from rotation import universe as U
from rotation.store import canonical_json, dec_str, sha256_hex

from rotation_backtest import calendar as CAL

BENCHMARK = E.BENCHMARK
TRADABLE_STATUSES = (E.VALID, E.INSUFFICIENT)
BUY_ACTIONS, SELL_ACTIONS = (R.ADD, R.INCREASE), (R.DECREASE, R.EXIT)
MONEY, PRICE, WEIGHT = Decimal("0.01"), Decimal("0.0001"), Decimal("0.000001")
BPS = Decimal(10000)
NO_NEXT_SESSION, ENGINE_STATUS, NO_ORDERS = "NO_NEXT_SESSION", "ENGINE_STATUS", "NO_ORDERS"
CASH_LIMITED, NO_CASH, NOT_HELD = "CASH_LIMITED", "NO_CASH", "NOT_HELD"


class SimulationFailure(Exception):
    """A fail-closed stop: the run is recorded as FAILED with this code and detail."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


@dataclass
class SimulationResult:
    status: str                                         # COMPLETED | FAILED
    failure_code: Optional[str] = None
    failure_detail: Optional[str] = None
    sessions: List[date] = field(default_factory=list)
    rebalances: List[dict] = field(default_factory=list)
    trades: List[dict] = field(default_factory=list)
    equity: List[dict] = field(default_factory=list)
    final_cash: Optional[Decimal] = None
    final_holdings: Dict[str, int] = field(default_factory=dict)
    completed_positions: List[dict] = field(default_factory=list)
    total_costs: Decimal = Decimal(0)

    def result_hash(self) -> Optional[str]:
        if self.status != "COMPLETED":
            return None
        body = {"equity": [[e["session_date"], dec_str(e["equity"]), dec_str(e["cash"])] for e in self.equity],
                "trades": [[t["seq"], t["order_index"], t["symbol"], t["side"], t["filled_qty"], dec_str(t["fill_price"]), dec_str(t["cost"])]
                           for t in self.trades],
                "rebalances": [[r["seq"], r["signal_session"], r["engine_status"], r["executed"], r["proposal_hash"]] for r in self.rebalances]}
        return sha256_hex(canonical_json(body))


# ---- helpers ------------------------------------------------------------------------------------------------------------------------

def _q(d: Decimal, places: Decimal) -> Decimal:
    return d.quantize(places, rounding=ROUND_HALF_EVEN)


def truncate(ser: Optional[BarSeries], T: date) -> Optional[BarSeries]:
    """The same series with ONLY bars dated <= T (point-in-time view; None when nothing is dated <= T)."""
    if ser is None:
        return None
    i = ser.last_on_or_before(T)
    if i is None:
        return None
    t = BarSeries.__new__(BarSeries)
    t.symbol, t.dates, t.bars = ser.symbol, ser.dates[:i + 1], ser.bars[:i + 1]
    t.index = {d: k for k, d in enumerate(t.dates)}
    return t


def truncate_all(series: Dict[str, BarSeries], T: date) -> Dict[str, BarSeries]:
    out = {}
    for s, ser in series.items():
        t = truncate(ser, T)
        if t is not None:
            out[s] = t
    return out


def price_of(ser: Optional[BarSeries], d: date, field_name: str) -> Optional[Decimal]:
    bar = ser.bar(d) if ser is not None else None
    if bar is None:
        return None
    v = F.to_decimal(getattr(bar, field_name))
    return _q(v, PRICE) if v is not None and v > 0 else None


def check_benchmark(series: Dict[str, BarSeries], universe_symbols, start: date, end: date) -> List[date]:
    """The benchmark calendar; raises MISSING_BENCHMARK when a universe symbol has a session the benchmark lacks."""
    spy = series.get(BENCHMARK)
    if spy is None or not len(spy):
        raise SimulationFailure("MISSING_BENCHMARK", f"No {BENCHMARK} bars are available for the range.")
    sessions = CAL.sessions_in_range(spy, start, end)
    if not sessions:
        raise SimulationFailure("NO_SESSIONS", f"No {BENCHMARK} session between {start.isoformat()} and {end.isoformat()}.")
    have = set(sessions)
    for s in sorted(universe_symbols):
        ser = series.get(s)
        if ser is None:
            continue
        gap = [d for d in ser.dates if start <= d <= end and d not in have]
        if gap:
            raise SimulationFailure("MISSING_BENCHMARK", f"{BENCHMARK} has no bar on {gap[0].isoformat()} although {s} does.")
    return sessions


# ---- the replay ----------------------------------------------------------------------------------------------------------------------

def simulate(rotation_cfg: dict, universe: U.ResolvedUniverse, series: Dict[str, BarSeries], definition: dict, *, now: datetime, engine=None) -> SimulationResult:
    """Replay `definition` (the canonical BacktestConfig) with the Stage 4.7 engine. Returns a COMPLETED or FAILED result.
    `engine` (research only, Stage 5.2): an alternative proposal function with the engine's signature; None = the production engine."""
    try:
        return _simulate(rotation_cfg, universe, series, definition, now, engine)
    except SimulationFailure as exc:
        return SimulationResult(status="FAILED", failure_code=exc.code, failure_detail=exc.detail[:300])


def _simulate(cfg: dict, universe: U.ResolvedUniverse, series: Dict[str, BarSeries], defn: dict, now: datetime, engine=None) -> SimulationResult:
    start, end = date.fromisoformat(defn["start_date"]), date.fromisoformat(defn["end_date"])
    cost_rate = Decimal(defn["transaction_cost_bps"]) / BPS
    slip = Decimal(defn["slippage_bps"]) / BPS
    sessions = check_benchmark(series, universe.symbols, start, end)
    sched = set(CAL.schedule(sessions, defn["rebalance_frequency"]))
    spy = series[BENCHMARK]
    base_close = price_of(spy, sessions[0], "close")
    cash = Decimal(defn["initial_cash"])
    holdings: Dict[str, int] = {}
    basis: Dict[str, Decimal] = {}                    # total cost basis (notional + buy costs) of the open position
    opened: Dict[str, int] = {}                       # session index when the position was opened
    res = SimulationResult(status="COMPLETED", sessions=list(sessions))
    pending: List[dict] = []
    seq = 0

    for idx, s in enumerate(sessions):
        # 1. OPEN: fill the orders decided at the previous signal session
        if pending:
            reb = res.rebalances[-1]
            fills, traded, costs = 0, Decimal(0), Decimal(0)
            for k, o in enumerate(pending):
                px_open = price_of(series.get(o["symbol"]), s, "open")
                if px_open is None:
                    raise SimulationFailure("MISSING_OPEN", f"No {o['symbol']} bar at the execution session {s.isoformat()} "
                                                            f"(signal {o['signal_session']}).")
                side = o["side"]
                fill = _q(px_open * ((1 + slip) if side == "BUY" else (1 - slip)), PRICE)
                qty, note, pnl = o["qty"], None, None
                if side == "BUY":
                    affordable = int((cash / (fill * (1 + cost_rate))).to_integral_value(rounding=ROUND_FLOOR)) if fill > 0 else 0
                    if affordable < qty:
                        qty, note = max(affordable, 0), (CASH_LIMITED if affordable > 0 else NO_CASH)
                    notional = _q(fill * qty, MONEY)
                    cost = _q(notional * cost_rate, MONEY)
                    cash -= notional + cost
                    if qty > 0:
                        if holdings.get(o["symbol"], 0) == 0:
                            opened[o["symbol"]] = idx
                        holdings[o["symbol"]] = holdings.get(o["symbol"], 0) + qty
                        basis[o["symbol"]] = basis.get(o["symbol"], Decimal(0)) + notional + cost
                else:
                    held = holdings.get(o["symbol"], 0)
                    if held <= 0:
                        qty, note = 0, NOT_HELD
                    elif qty > held:
                        qty = held                                                # never short
                    notional = _q(fill * qty, MONEY)
                    cost = _q(notional * cost_rate, MONEY)
                    if qty > 0:
                        avg = basis[o["symbol"]] / held
                        pnl = _q(notional - cost - avg * qty, MONEY)
                        basis[o["symbol"]] = _q(basis[o["symbol"]] - avg * qty, MONEY)
                        holdings[o["symbol"]] = held - qty
                        cash += notional - cost
                        if holdings[o["symbol"]] == 0:
                            opened_idx = opened.pop(o["symbol"])
                            res.completed_positions.append({"symbol": o["symbol"], "opened_session": sessions[opened_idx].isoformat(),
                                                            "closed_session": s.isoformat(), "holding_sessions": idx - opened_idx,
                                                            "realised_pnl": pnl, "win": pnl > 0})
                            holdings.pop(o["symbol"], None)
                            basis.pop(o["symbol"], None)
                if cash < 0:                                                      # accounting invariant, never expected
                    raise SimulationFailure("NEGATIVE_CASH", f"Cash went negative at {s.isoformat()}.")
                res.trades.append({"seq": reb["seq"], "order_index": k, "signal_session": o["signal_session"], "execution_session": s.isoformat(),
                                   "symbol": o["symbol"], "side": side, "action": o["action"], "rank": o.get("rank"), "requested_qty": o["qty"],
                                   "filled_qty": qty, "open_price": px_open, "fill_price": fill, "notional": notional, "cost": cost,
                                   "cash_after": _q(cash, MONEY), "qty_after": holdings.get(o["symbol"], 0), "realised_pnl": pnl, "note": note})
                fills += 1 if qty > 0 else 0
                traded += notional
                costs += cost
            reb.update(execution_session=s.isoformat(), executed=1, n_fills=fills, traded_notional=_q(traded, MONEY), total_cost=_q(costs, MONEY))
            res.total_costs += costs
            pending = []

        # 2. CLOSE: mark every holding at s's completed close
        value = Decimal(0)
        for sym in sorted(holdings):
            px = price_of(series.get(sym), s, "close")
            if px is None:
                raise SimulationFailure("MISSING_CLOSE", f"No {sym} close at {s.isoformat()} while {holdings[sym]} shares are held.")
            value += px * holdings[sym]
        equity = _q(cash + value, MONEY)
        bench = _q(Decimal(defn["initial_cash"]) * price_of(spy, s, "close") / base_close, MONEY)
        res.equity.append({"session_date": s.isoformat(), "cash": _q(cash, MONEY), "positions_value": _q(value, MONEY), "equity": equity,
                           "benchmark_index": bench, "n_positions": len(holdings), "cash_weight": _q(cash / equity, WEIGHT) if equity > 0 else Decimal(0)})

        # 3. SIGNAL (schedule dates): the Stage 4.7 engine on bars <= s and the simulated portfolio as of s's close
        if s in sched:
            seq += 1
            snapshot = SN.normalise(SN.LOCAL_SIMULATOR, None, _q(cash, MONEY), [(sym, q) for sym, q in holdings.items()],
                                    {"historical_backtest": True}, SN.OK)
            bars_to_s = truncate_all(series, s)
            rr = E.compute_rotation(cfg, universe, bars_to_s, snapshot, s, now=now, conflicts=set()) if engine is None else engine(cfg, universe, bars_to_s, snapshot, s, now=now, conflicts=set())
            run = rr.run
            row = {"seq": seq, "signal_session": s.isoformat(), "execution_session": None, "engine_status": run["status"],
                   "status_detail": run.get("status_detail"), "executed": 0, "skip_reason": None, "reference_equity": run.get("reference_equity"),
                   "cash_before": _q(cash, MONEY), "turnover": run.get("turnover"), "n_eligible": run.get("n_eligible", 0),
                   "n_selected": run.get("n_selected", 0), "n_orders": 0, "n_fills": 0, "traded_notional": Decimal(0), "total_cost": Decimal(0),
                   "input_hash": run["input_hash"], "proposal_hash": run["proposal_hash"],
                   "proposal": {"targets": [{"symbol": t["symbol"], "rank": t["rank"], "target_weight": dec_str(t["target_weight"]),
                                             "est_target_qty": t["est_target_qty"], "reason": t["reason"]} for t in rr.targets],
                                "items": [{"symbol": i["symbol"], "current_qty": dec_str(i["current_qty"]), "current_weight": dec_str(i["current_weight"]),
                                           "target_weight": dec_str(i["target_weight"]), "est_qty_diff": i["est_qty_diff"], "action": i["action"],
                                           "reason": i["reason"]} for i in rr.items]}}
            nxt = CAL.next_session(sessions, s)
            if run["status"] not in TRADABLE_STATUSES:
                row["skip_reason"] = ENGINE_STATUS
            elif nxt is None:
                row["skip_reason"] = NO_NEXT_SESSION
            else:
                rank_of = {t["symbol"]: t["rank"] for t in rr.targets}
                sells = sorted((i for i in rr.items if i["action"] in SELL_ACTIONS), key=lambda i: i["symbol"])
                buys = sorted((i for i in rr.items if i["action"] in BUY_ACTIONS), key=lambda i: (rank_of.get(i["symbol"], 10 ** 9), i["symbol"]))
                for i in sells:
                    qty = holdings.get(i["symbol"], 0) if i["action"] == R.EXIT else int(i["est_qty_diff"])
                    if qty > 0:
                        pending.append({"symbol": i["symbol"], "side": "SELL", "action": i["action"], "qty": qty, "rank": rank_of.get(i["symbol"]),
                                        "signal_session": s.isoformat()})
                for i in buys:
                    qty = int(i["est_qty_diff"])
                    if qty > 0:
                        pending.append({"symbol": i["symbol"], "side": "BUY", "action": i["action"], "qty": qty, "rank": rank_of.get(i["symbol"]),
                                        "signal_session": s.isoformat()})
                row["n_orders"] = len(pending)
                if not pending:
                    row["skip_reason"] = NO_ORDERS
            res.rebalances.append(row)

    res.final_cash, res.final_holdings = _q(cash, MONEY), dict(holdings)
    res.total_costs = _q(res.total_costs, MONEY)
    return res

