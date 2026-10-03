"""
backtest/metrics.py — deterministic result metrics, benchmarks and breakdowns (formulas in FORMULAS, shown in the UI).

Descriptive only: nothing here rates a strategy, and sample-size labels describe how many closed trades exist — they
are not quality ratings. Closed-trade statistics exclude positions still open at the end; portfolio statistics
(equity, total return, drawdown) include them at their last close.
"""
from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Dict, List, Optional

from backtest.replay import BarSeries

ANNUALIZE_MIN_SESSIONS = 252
SAMPLE_BANDS = (
    (0, 0, "NO_TRADES", "NO TRADES — the strategy produced no closed trades in this period."),
    (1, 9, "VERY_SMALL_SAMPLE", "VERY SMALL SAMPLE — {n} closed trade{s}. Results can change a lot with a few trades."),
    (10, 29, "SMALL_SAMPLE", "SMALL SAMPLE — {n} closed trades. Treat averages and rates with caution."),
    (30, None, "SAMPLE_SIZE", "{n} closed trades. A larger sample, but still no statistical proof of future behaviour."),
)
FORMULAS = {
    "total_return_pct": "(ending equity / initial equity - 1) x 100; ending equity includes open positions at the last close",
    "win_rate_pct": "winning closed trades / closed trades x 100 (winning = P&L > 0; losing = P&L < 0; P&L = 0 is neither)",
    "average_trade_return_pct": "mean of closed-trade return % (this is the expectancy used here)",
    "expectancy_pct": "average closed-trade return % = win rate x average winner + loss rate x average loser "
                      "(break-even trades add 0)",
    "median_trade_return_pct": "median of closed-trade return %",
    "average_winner_pct": "mean return % of winning closed trades",
    "average_loser_pct": "mean return % of losing closed trades",
    "gross_profit": "sum of P&L of winning closed trades ($)",
    "gross_loss": "sum of P&L of losing closed trades ($, zero or negative)",
    "profit_factor": "gross profit / |gross loss|; N/A when there are no losing trades; 0 when there are losses but "
                     "no winners",
    "trade_return_pct": "P&L / (entry value + entry commission) x 100, where P&L = exit value - exit commission - entry "
                        "value - entry commission",
    "max_drawdown_pct": "most negative (equity / running peak equity - 1) x 100 over daily closes (peak starts at the "
                        "initial equity)",
    "holding_days": "the symbol's sessions from the entry fill day (day 1) through the exit signal day",
    "mfe_pct": "max(0, (highest price while held - entry fill) / entry fill x 100); held = entry day's full range, full "
               "ranges of later days, and only the OPEN of the exit day",
    "mae_pct": "min(0, (lowest price while held - entry fill) / entry fill x 100), same days as MFE",
    "time_in_market_pct": "sessions ending with at least one open position / sessions x 100",
    "simultaneous_positions": "open positions at each session close (maximum and mean)",
    "annualized_return_pct": "((ending equity / initial equity) ^ (252 / sessions) - 1) x 100, shown only with at "
                             "least 252 sessions",
    "benchmark": "buy at the first session's open with the same slippage and commission, whole shares, hold to the "
                 "end, mark at each close; uninvested remainder stays as cash at 0%",
}


def sample_size(n: int) -> dict:
    for lo, hi, code, text in SAMPLE_BANDS:
        if n >= lo and (hi is None or n <= hi):
            return {"closed_trades": n, "code": code, "text": text.format(n=n, s="" if n == 1 else "s"),
                    "note": "Sample-size labels describe how many trades exist; they are not a quality rating."}
    raise ValueError(n)


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _median(xs: List[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def _profit_factor(gross_profit: float, gross_loss: float, n_win: int, n_loss: int, n_closed: int) -> dict:
    if n_closed == 0:
        return {"value": None, "text": "N/A — no closed trades"}
    if n_loss == 0:
        return {"value": None, "text": "N/A — no losing trades"}
    if n_win == 0:
        return {"value": 0.0, "text": "0 — no winning trades"}
    return {"value": gross_profit / abs(gross_loss), "text": None}


def core_metrics(initial_equity: float, trades: List[dict], equity: List[dict]) -> dict:
    closed = [t for t in trades if t["status"] == "CLOSED"]
    still_open = [t for t in trades if t["status"] == "OPEN_AT_END"]
    ending = equity[-1]["equity"] if equity else initial_equity
    rets = [t["return_pct"] for t in closed]
    winners = [t for t in closed if t["pnl_dollars"] > 0]
    losers = [t for t in closed if t["pnl_dollars"] < 0]
    gp = sum(t["pnl_dollars"] for t in winners)
    gl = sum(t["pnl_dollars"] for t in losers)
    n = len(closed)
    win_rate = len(winners) / n * 100.0 if n else None
    loss_rate = len(losers) / n * 100.0 if n else None
    avg_w, avg_l = _mean([t["return_pct"] for t in winners]), _mean([t["return_pct"] for t in losers])
    expectancy = _mean(rets)
    decomposition = None if not n else (win_rate / 100.0) * (avg_w or 0.0) + (loss_rate / 100.0) * (avg_l or 0.0)
    sessions = len(equity)
    counts = [e["open_positions"] for e in equity]
    order = lambda t: (t["return_pct"], t["exit_fill_date"], t["symbol"])  # noqa: E731 - deterministic tie-break
    best = max(closed, key=order) if closed else None
    worst = min(closed, key=lambda t: (t["return_pct"], t["exit_fill_date"], t["symbol"])) if closed else None
    pick = lambda t: None if t is None else {k: t[k] for k in ("trade_no", "symbol", "entry_fill_date",  # noqa: E731
                                                               "exit_fill_date", "return_pct", "pnl_dollars")}
    annual = None
    if sessions >= ANNUALIZE_MIN_SESSIONS and initial_equity > 0 and ending > 0:
        annual = ((ending / initial_equity) ** (ANNUALIZE_MIN_SESSIONS / sessions) - 1.0) * 100.0
    return {
        "initial_equity": initial_equity, "ending_equity": ending,
        "total_return_pct": (ending / initial_equity - 1.0) * 100.0,
        "annualized_return_pct": annual,
        "annualized_note": None if annual is not None else "Not shown — backtest shorter than one trading year "
                                                             f"({sessions} of {ANNUALIZE_MIN_SESSIONS} sessions).",
        "sessions": sessions,
        "closed_trades": n, "open_positions_at_end": len(still_open),
        "winning_trades": len(winners), "losing_trades": len(losers), "breakeven_trades": n - len(winners) - len(losers),
        "win_rate_pct": win_rate, "loss_rate_pct": loss_rate,
        "average_trade_return_pct": expectancy, "expectancy_pct": expectancy,
        "expectancy_decomposition_pct": decomposition,
        "median_trade_return_pct": _median(rets),
        "average_winner_pct": avg_w, "average_loser_pct": avg_l,
        "gross_profit": gp, "gross_loss": gl,
        "profit_factor": _profit_factor(gp, gl, len(winners), len(losers), n),
        "realized_pnl": sum(t["pnl_dollars"] for t in closed),
        "unrealized_pnl": sum(t["unrealized_pnl"] for t in still_open),
        "max_drawdown_pct": min((e["drawdown"] for e in equity), default=0.0) * 100.0,
        "average_holding_days": _mean([t["holding_days"] for t in closed]),
        "median_holding_days": _median([t["holding_days"] for t in closed]),
        "average_mfe_pct": _mean([t["mfe_pct"] for t in closed]),
        "average_mae_pct": _mean([t["mae_pct"] for t in closed]),
        "best_trade": pick(best), "worst_trade": pick(worst),
        "time_in_market_pct": (sum(1 for c in counts if c > 0) / sessions * 100.0) if sessions else 0.0,
        "max_simultaneous_positions": max(counts, default=0),
        "average_simultaneous_positions": _mean(counts) or 0.0,
        "total_commission": sum(t["commission"] for t in trades),
        "total_slippage_impact": sum(t["slippage_impact"] for t in trades),
    }


def buy_and_hold(symbols: List[str], prices: Dict[str, BarSeries], calendar: List[date], initial_equity: float,
                 slippage_bps: float, commission: float) -> dict:
    """Equal capital per listed symbol, bought at the first session's open, never rebalanced or sold."""
    slip = slippage_bps / 10000.0
    first = calendar[0]
    alloc = initial_equity / len(symbols) if symbols else 0.0
    cash, held, excluded = initial_equity, {}, []
    for sym in sorted(symbols):
        bar = prices[sym].bar(first) if sym in prices else None
        if bar is None:
            excluded.append({"symbol": sym, "reason": "NO_BAR_AT_FIRST_SESSION"})
            continue
        fill = bar.open * (1 + slip)
        shares = math.floor((alloc - commission) / fill + 1e-9) if alloc > commission else 0
        while shares > 0 and shares * fill + commission > alloc + 1e-7:
            shares -= 1
        if shares < 1:
            excluded.append({"symbol": sym, "reason": "ALLOCATION_BELOW_ONE_SHARE"})
            continue
        cash -= shares * fill + commission
        held[sym] = {"shares": shares, "fill_price": fill}
    series, peak, dd = [], initial_equity, 0.0
    for T in calendar:
        v = cash
        for sym, h in held.items():
            i = prices[sym].last_on_or_before(T)
            v += h["shares"] * prices[sym].bars[i].close
        peak = max(peak, v)
        dd = min(dd, v / peak - 1.0)
        series.append(v)
    ending = series[-1] if series else initial_equity
    sessions = len(series)
    annual = ((ending / initial_equity) ** (ANNUALIZE_MIN_SESSIONS / sessions) - 1.0) * 100.0 \
        if sessions >= ANNUALIZE_MIN_SESSIONS and ending > 0 else None
    return {"series": series, "ending_equity": ending, "total_return_pct": (ending / initial_equity - 1.0) * 100.0,
            "max_drawdown_pct": dd * 100.0, "annualized_return_pct": annual, "uninvested_cash": cash,
            "holdings": held, "excluded": excluded, "first_session": first.isoformat()}


def _group(trades: List[dict]) -> dict:
    n = len(trades)
    return {"closed_trades": n, "win_rate_pct": (sum(1 for t in trades if t["pnl_dollars"] > 0) / n * 100.0) if n else None,
            "average_return_pct": _mean([t["return_pct"] for t in trades]),
            "realized_pnl": sum(t["pnl_dollars"] for t in trades),
            "average_holding_days": _mean([t["holding_days"] for t in trades]),
            "average_mfe_pct": _mean([t["mfe_pct"] for t in trades]),
            "average_mae_pct": _mean([t["mae_pct"] for t in trades]),
            "sample": sample_size(n)["code"]}


def _by(trades: List[dict], key) -> List[dict]:
    groups: Dict[str, List[dict]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return [{"group": g, **_group(ts)} for g, ts in sorted(groups.items())]


def breakdowns(trades: List[dict], environment_available: bool) -> dict:
    closed = [t for t in trades if t["status"] == "CLOSED"]
    appearances = {r: sum(1 for t in closed if r in t["all_exit_reasons"]) for r in
                   ("CONDITION", "INVALIDATION", "TARGET", "MAX_HOLDING")}
    return {
        "market_trend_at_entry": _by(closed, lambda t: t["market_trend_at_entry"] or "UNAVAILABLE"),
        "market_environment_at_entry": _by(closed, lambda t: t["market_environment_at_entry"] or "UNAVAILABLE")
        if environment_available else None,
        "symbol": _by(closed, lambda t: t["symbol"]),
        "exit_reason": _by(closed, lambda t: "MULTIPLE" if len(t["all_exit_reasons"]) > 1 else t["all_exit_reasons"][0]),
        "exit_reason_appearances": appearances,
        "note": "Descriptive groupings of closed trades. Small groups say little; nothing here ranks symbols or states.",
    }
