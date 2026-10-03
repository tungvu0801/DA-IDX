"""
backtest/engine.py — the deterministic Stage 3.2 simulation (pure: no I/O, no clock, no randomness).

Contract (strategy/CONTRACTS.md, unchanged): decisions use the close of session T; any entry or exit fills at the
NEXT session's open. Each session T runs in this order:

  OPEN of T   1. pending EXITS fill at T's open (exit first, so freed cash is available);
              2. pending ENTRIES (signalled at the previous close) fill at T's open, in selection-policy order.
  CLOSE of T  3. open positions record T's high / low (excursions) and count T as a holding day;
              4. EXIT rules are checked for every open position without a pending exit;
              5. ENTRY rules are checked for every universe symbol with no position (open or pending);
              6. positions are marked to T's close -> one equity observation.

Conventions (all documented in backtest/METHOD.md and tested):
  * a signal at the close of T never fills at T's close; entries need the symbol's bar on the very next market
    session (else UNFILLED_ENTRY / NEXT_SESSION_BAR_MISSING); a signal on the last session is UNFILLED_ENTRY /
    NO_NEXT_SESSION_IN_RUN. An exit fills at the symbol's next available open (delay recorded) or stays open.
  * cash-only, long-only, whole shares, no leverage: target = equity at that open x max_position_pct, capped by cash
    after the entry commission and one reserved exit commission per open position, so cash can never go negative.
  * contention: when more symbols signal than free slots, the CENTRAL selection policy orders them (ALPHABETICAL is
    the only one — a neutral, documented rule, not a ranking). Positions awaiting an exit still hold their slot.
  * entry-based exit levels are frozen at the entry signal: support / resistance from the entry snapshot, percentage
    levels from the actual entry fill price (after slippage). If a required level is missing, the entry is skipped.
  * holding days count the symbol's own sessions; the entry fill day is day 1; reaching max_holding_days at a close
    signals the exit there (fill next open).
  * the same symbol cannot exit and re-enter on one close: re-entry is evaluated from the close after the exit fill.
  * positions still open at the end are NOT liquidated: they are marked to the last close and reported separately.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional

from backtest.replay import BarSeries, session_close
from strategy import features as F
from strategy.evaluate import group_met

ENGINE_VERSION = "3.2.0"
SELECTION_POLICIES = {
    "ALPHABETICAL": "When more symbols signal than open slots, symbols are taken in alphabetical order. This is a "
                    "neutral, documented tie-break — not a ranking and not a claim that earlier letters are better.",
}
EXIT_REASONS = ("CONDITION", "INVALIDATION", "TARGET", "MAX_HOLDING")   # Stage 3.1 contract order = display priority
EPS = 1e-9


@dataclass(frozen=True)
class RunConfig:
    start: date
    end: date
    initial_equity: float
    slippage_bps_per_side: float
    commission_per_order: float
    selection_policy: str = "ALPHABETICAL"


def select_order(symbols, policy: str) -> List[str]:
    """The ONE place entry contention is ordered (future stages may add other explicit policies here)."""
    if policy == "ALPHABETICAL":
        return sorted(symbols)
    raise ValueError(f"unknown selection policy {policy}")


def _values(cells: Optional[dict]) -> dict:
    return {} if not cells else {fid: c["v"] for fid, c in cells.items()}


def _annotate(trace: List[dict], cells: dict) -> List[dict]:
    out = []
    for t in trace:
        if "children" in t:
            out.append({**t, "children": _annotate(t["children"], cells)})
        else:
            cell = cells.get(t.get("feature")) or {}
            out.append({**t, "availability": cell.get("a", "NOT_COMPUTED")})
    return out


def snapshot_record(cells: dict, T: date) -> dict:
    feats = []
    for fid in sorted(cells):
        f = F.get(fid)
        feats.append({"feature_id": fid, "name": f.beginner_name if f else fid, "value": cells[fid]["v"],
                      "availability": cells[fid]["a"], "source": f.source if f else None})
    return {"session": T.isoformat(), "evaluated_at": session_close(T).isoformat(), "features": feats}


@dataclass
class Position:
    symbol: str
    trade_no: int
    entry_signal_date: date
    entry_fill_date: date
    entry_open: float
    entry_fill: float
    shares: int
    entry_value: float
    entry_commission: float
    entry_slippage: float
    entry_support: Optional[float]
    entry_resistance: Optional[float]
    invalidation_level: Optional[float]
    target_level: Optional[float]
    entry_snapshot: dict
    entry_trace: dict
    market_trend: Optional[str]
    market_environment: Optional[str]
    max_price: float
    min_price: float
    holding_days: int = 0
    exit_signal_date: Optional[date] = None
    exit_reasons: List[str] = field(default_factory=list)
    exit_snapshot: Optional[dict] = None
    exit_trace: Optional[dict] = None


def _exc(p: Position):
    mfe = max(0.0, (p.max_price - p.entry_fill) / p.entry_fill * 100.0)
    mae = min(0.0, (p.min_price - p.entry_fill) / p.entry_fill * 100.0)
    return mfe, mae


def simulate(spec: dict, cfg: RunConfig, calendar: List[date], prices: Dict[str, BarSeries],
             snap: Callable[[str, date], Optional[dict]]) -> dict:
    """calendar: market sessions in [start, end] ascending. prices: bars per universe symbol. snap(sym, T): merged
    feature cells {feature_id: {"v", "a"}} for a symbol evaluated at the close of T, or None (not evaluated)."""
    risk, x = spec["risk"], spec["exit"]
    inv, tgt, hold = x.get("invalidation"), x.get("target"), x.get("max_holding_days")
    universe = list(spec["universe"]["symbols"])
    slip, comm = cfg.slippage_bps_per_side / 10000.0, float(cfg.commission_per_order)
    cash = float(cfg.initial_equity)
    positions: Dict[str, Position] = {}
    pending: Dict[str, dict] = {}
    last_close: Dict[str, float] = {}
    events: List[dict] = []
    trades: List[dict] = []
    equity_rows: List[dict] = []
    counts: Counter = Counter()
    peak, prev_eq = cash, cash
    next_trade = [1]
    cal_index = {d: i for i, d in enumerate(calendar)}

    def emit(T, sym, etype, reason=None, trade_no=None, **detail):
        events.append({"seq": len(events) + 1, "session_date": T.isoformat(), "symbol": sym, "event_type": etype,
                       "reason_code": reason, "trade_no": trade_no, "detail": detail})
        counts[etype] += 1
        if reason:
            counts[f"{etype}:{reason}"] += 1

    def close_trade(p: Position, T: date, bar) -> None:
        nonlocal cash
        exit_fill = bar.open * (1 - slip)
        p.max_price, p.min_price = max(p.max_price, bar.open), min(p.min_price, bar.open)   # only the exit open counts
        exit_value = p.shares * exit_fill
        cash += exit_value - comm
        pnl = exit_value - comm - p.entry_value - p.entry_commission
        mfe, mae = _exc(p)
        primary = next(r for r in EXIT_REASONS if r in p.exit_reasons)
        trades.append(_trade_row(p, "CLOSED", mfe, mae) | {
            "exit_signal_date": p.exit_signal_date.isoformat(), "exit_fill_date": T.isoformat(),
            "exit_open_price": bar.open, "exit_fill_price": exit_fill, "exit_value": exit_value, "exit_commission": comm,
            "commission": p.entry_commission + comm, "slippage_impact": p.entry_slippage + p.shares * (bar.open - exit_fill),
            "pnl_dollars": pnl, "return_pct": pnl / (p.entry_value + p.entry_commission) * 100.0,
            "primary_exit_reason": primary, "all_exit_reasons": list(p.exit_reasons),
            "exit_fill_delay_sessions": cal_index[T] - cal_index[p.exit_signal_date] - 1,
            "exit_feature_snapshot": p.exit_snapshot, "exit_evaluation_trace": p.exit_trace})
        emit(T, p.symbol, "EXIT_FILLED", primary, p.trade_no, open=bar.open, fill_price=exit_fill, shares=p.shares,
             proceeds=exit_value - comm, pnl_dollars=pnl)

    for k, T in enumerate(calendar):
        nxt = calendar[k + 1] if k + 1 < len(calendar) else None
        # ---------------- OPEN of T ----------------
        for sym in sorted(positions):
            p = positions[sym]
            if p.exit_signal_date is not None and prices[sym].has(T):
                close_trade(p, T, prices[sym].bar(T))
                del positions[sym]
        if pending:
            eq_open = cash + sum(p.shares * (prices[s].bar(T).open if prices[s].has(T) else last_close[s])
                                 for s, p in positions.items())
            for sym in select_order(pending, cfg.selection_policy):
                sig = pending.pop(sym)
                bar = prices[sym].bar(T)
                if bar is None:
                    emit(T, sym, "UNFILLED_ENTRY", "NEXT_SESSION_BAR_MISSING", signal_date=sig["T"].isoformat())
                    continue
                fill = bar.open * (1 + slip)
                target_value = eq_open * risk["max_position_pct"] / 100.0
                available = cash - comm - comm * (len(positions) + 1)      # entry commission + reserved exit commissions
                budget = min(target_value, available)
                shares = math.floor(budget / fill + EPS) if budget > 0 else 0
                while shares > 0 and shares * fill > budget + 1e-7:
                    shares -= 1
                if shares < 1:
                    reason = "INSUFFICIENT_CASH" if available < fill else "TARGET_BELOW_ONE_SHARE"
                    emit(T, sym, "ENTRY_SKIPPED", reason, signal_date=sig["T"].isoformat(), fill_price=fill,
                         target_value=target_value, available_cash=max(available, 0.0))
                    continue
                value = shares * fill
                cash -= value + comm
                sup, res = sig["support"], sig["resistance"]
                inv_level = None if not inv else sup if inv["method"] == "CLOSE_BELOW_ENTRY_SUPPORT" else fill * (1 - inv["pct"] / 100.0)
                tgt_level = None if not tgt else res if tgt["method"] == "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE" else fill * (1 + tgt["pct"] / 100.0)
                no = next_trade[0]
                next_trade[0] += 1
                positions[sym] = Position(sym, no, sig["T"], T, bar.open, fill, shares, value, comm, shares * (fill - bar.open),
                                          sup, res, inv_level, tgt_level, sig["snapshot"], sig["trace"], sig["market_trend"],
                                          sig["market_environment"], max_price=bar.open, min_price=bar.open)
                emit(T, sym, "ENTRY_FILLED", None, no, signal_date=sig["T"].isoformat(), open=bar.open, fill_price=fill,
                     shares=shares, cost=value + comm, equity_at_open=eq_open, target_value=target_value)
        # ---------------- CLOSE of T ----------------
        for sym, p in positions.items():
            bar = prices[sym].bar(T)
            if bar is not None:
                p.max_price, p.min_price = max(p.max_price, bar.high), min(p.min_price, bar.low)
                p.holding_days += 1
                last_close[sym] = bar.close
        for sym in sorted(positions):
            p = positions[sym]
            bar = prices[sym].bar(T)
            if p.exit_signal_date is not None or bar is None:
                continue
            cells = snap(sym, T) or {}
            close, reasons = bar.close, []
            tr = {"session": T.isoformat(), "close": close, "holding_days": p.holding_days}
            if x.get("conditions"):
                ok, trace = group_met(x, _values(cells))
                tr["conditions"] = {"logic": x["logic"], "result": "MET" if ok else "NOT_MET", "trace": _annotate(trace, cells)}
                if ok:
                    reasons.append("CONDITION")
            if inv:
                lvl = p.invalidation_level
                hit = close < lvl - EPS if inv["method"] == "CLOSE_BELOW_ENTRY_SUPPORT" else close <= lvl + EPS
                tr["invalidation"] = {"method": inv["method"], "level": lvl, "close": close, "hit": hit}
                if hit:
                    reasons.append("INVALIDATION")
            if tgt:
                lvl = p.target_level
                hit = close >= lvl - EPS
                tr["target"] = {"method": tgt["method"], "level": lvl, "close": close, "hit": hit}
                if hit:
                    reasons.append("TARGET")
            if hold:
                hit = p.holding_days >= hold
                tr["max_holding"] = {"holding_days": p.holding_days, "max_holding_days": hold, "hit": hit}
                if hit:
                    reasons.append("MAX_HOLDING")
            if reasons:
                p.exit_signal_date, p.exit_reasons, p.exit_trace = T, reasons, tr
                p.exit_snapshot = snapshot_record(cells, T) if cells else None
                emit(T, sym, "EXIT_SIGNAL", next(r for r in EXIT_REASONS if r in reasons), p.trade_no, reasons=reasons,
                     trace=tr)
        free = risk["max_open_positions"] - len(positions)
        candidates = {}
        for sym in universe:
            if sym in positions or sym not in prices or not prices[sym].has(T):
                continue
            cells = snap(sym, T)
            if cells is None:
                counts["not_evaluated"] += 1
                continue
            ok, trace = group_met(spec["entry"], _values(cells))
            counts["entry_evaluations"] += 1
            if any(c["a"] == "INSUFFICIENT_HISTORY" for c in cells.values()):
                counts["evaluations_with_insufficient_history"] += 1
            if ok:
                candidates[sym] = (cells, trace)
        for sym in select_order(candidates, cfg.selection_policy):
            cells, trace = candidates[sym]
            rec, tr = snapshot_record(cells, T), {"logic": spec["entry"]["logic"], "result": "MET",
                                                  "trace": _annotate(trace, cells)}
            emit(T, sym, "ENTRY_SIGNAL", None, None, snapshot=rec, trace=tr)
            sup, res = cells.get("stock.support", {}).get("v"), cells.get("stock.resistance", {}).get("v")
            if inv and inv["method"] == "CLOSE_BELOW_ENTRY_SUPPORT" and sup is None:
                emit(T, sym, "ENTRY_SKIPPED", "REQUIRED_ENTRY_SUPPORT_UNAVAILABLE",
                     availability=cells.get("stock.support", {}).get("a"))
            elif tgt and tgt["method"] == "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE" and res is None:
                emit(T, sym, "ENTRY_SKIPPED", "REQUIRED_ENTRY_RESISTANCE_UNAVAILABLE",
                     availability=cells.get("stock.resistance", {}).get("a"))
            elif free <= 0:
                emit(T, sym, "ENTRY_SKIPPED", "MAX_OPEN_POSITIONS", open_positions=len(positions),
                     max_open_positions=risk["max_open_positions"])
            elif nxt is None:
                emit(T, sym, "UNFILLED_ENTRY", "NO_NEXT_SESSION_IN_RUN")
            else:
                pending[sym] = {"T": T, "snapshot": rec, "trace": tr, "support": sup, "resistance": res,
                                "market_trend": cells.get("market.trend", {}).get("v"),
                                "market_environment": cells.get("market.environment", {}).get("v")}
                free -= 1
        mv = sum(p.shares * last_close[s] for s, p in positions.items())
        eq = cash + mv
        peak = max(peak, eq)
        equity_rows.append({"session_date": T.isoformat(), "cash": cash, "market_value": mv, "equity": eq,
                            "open_positions": len(positions), "daily_return": eq / prev_eq - 1.0,
                            "drawdown": min(0.0, eq / peak - 1.0), "spy_benchmark_equity": None,
                            "universe_benchmark_equity": None})
        prev_eq = eq

    last = calendar[-1]
    for sym in sorted(positions):
        p = positions[sym]
        if p.exit_signal_date is not None:
            emit(last, sym, "UNFILLED_EXIT", "NO_NEXT_SESSION_IN_RUN" if p.exit_signal_date == last else "NO_LATER_BAR_IN_RUN",
                 p.trade_no, signal_date=p.exit_signal_date.isoformat(), reasons=p.exit_reasons)
        i = prices[sym].last_on_or_before(last)
        mark, mark_date = prices[sym].bars[i].close, prices[sym].dates[i]
        mfe, mae = _exc(p)
        trades.append(_trade_row(p, "OPEN_AT_END", mfe, mae) | {
            "exit_signal_date": p.exit_signal_date.isoformat() if p.exit_signal_date else None, "exit_fill_date": None,
            "exit_open_price": None, "exit_fill_price": None, "exit_value": None, "exit_commission": None,
            "commission": p.entry_commission, "slippage_impact": p.entry_slippage, "pnl_dollars": None, "return_pct": None,
            "mark_date": mark_date.isoformat(), "mark_price": mark,
            "unrealized_pnl": p.shares * mark - p.entry_value - p.entry_commission,
            "primary_exit_reason": next((r for r in EXIT_REASONS if r in p.exit_reasons), None),
            "all_exit_reasons": list(p.exit_reasons), "exit_fill_delay_sessions": None,
            "exit_feature_snapshot": p.exit_snapshot, "exit_evaluation_trace": p.exit_trace})
    trades.sort(key=lambda t: t["trade_no"])
    _assert_execution_model(trades, prices)
    return {"trades": trades, "events": events, "equity": equity_rows, "counts": dict(counts),
            "ending_cash": cash}


def _trade_row(p: Position, status: str, mfe: float, mae: float) -> dict:
    return {"trade_no": p.trade_no, "status": status, "symbol": p.symbol,
            "entry_signal_date": p.entry_signal_date.isoformat(), "entry_fill_date": p.entry_fill_date.isoformat(),
            "entry_open_price": p.entry_open, "entry_fill_price": p.entry_fill, "shares": p.shares,
            "entry_value": p.entry_value, "entry_commission": p.entry_commission, "mark_date": None, "mark_price": None,
            "unrealized_pnl": None, "holding_days": p.holding_days, "mfe_pct": mfe, "mae_pct": mae,
            "entry_support": p.entry_support, "entry_resistance": p.entry_resistance,
            "invalidation_level": p.invalidation_level, "target_level": p.target_level,
            "market_trend_at_entry": p.market_trend, "market_environment_at_entry": p.market_environment,
            "entry_feature_snapshot": p.entry_snapshot, "entry_evaluation_trace": p.entry_trace}


def _assert_execution_model(trades: List[dict], prices: Dict[str, BarSeries]) -> None:
    """Hard guard: no same-close execution, fills only at a real open of a later session."""
    for t in trades:
        s = prices[t["symbol"]]
        if not t["entry_fill_date"] > t["entry_signal_date"]:
            raise AssertionError(f"trade {t['trade_no']}: entry filled on its signal session")
        if abs(s.bar(date.fromisoformat(t["entry_fill_date"])).open - t["entry_open_price"]) > 1e-12:
            raise AssertionError(f"trade {t['trade_no']}: entry is not at the fill session's open")
        if t["exit_fill_date"] is not None:
            if not t["exit_fill_date"] > t["exit_signal_date"]:
                raise AssertionError(f"trade {t['trade_no']}: exit filled on its signal session")
            if abs(s.bar(date.fromisoformat(t["exit_fill_date"])).open - t["exit_open_price"]) > 1e-12:
                raise AssertionError(f"trade {t['trade_no']}: exit is not at the fill session's open")
