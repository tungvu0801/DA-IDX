"""
rotation_backtest/metrics.py — portfolio metrics with documented, deterministic conventions (DESIGN_48 §8).

Inputs are the simulator's equity rows (one per session, Decimal money) and its trade / rebalance / completed-position
records. Statistics use float arithmetic on daily simple returns; money stays Decimal. Nothing here is a statistical-
significance claim and nothing rates a strategy.
"""
from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

SESSIONS_PER_YEAR = 252
RISK_FREE = 0.0
ROLLING_3M_SESSIONS = 63
CONVENTIONS = {
    "returns": "daily simple returns of the equity curve (the first session's return is 0); sessions per year = 252; risk-free rate = 0",
    "total_return": "ending equity / initial cash - 1",
    "cagr": "(ending equity / initial cash) ^ (252 / n) - 1 with n = sessions after the first; null when n = 0",
    "annualized_volatility": "sample standard deviation of daily returns x sqrt(252); null with fewer than 2 returns",
    "sharpe": "mean(daily returns) / sample stdev x sqrt(252); null when the stdev is 0",
    "sortino": "mean(daily returns) / sqrt(mean(min(r, 0)^2)) x sqrt(252); null when there is no negative return",
    "max_drawdown": "most negative equity / running peak - 1 over session closes (peak starts at the first session)",
    "max_drawdown_sessions": "longest number of sessions from a peak until the equity next reaches a new peak (or the end)",
    "benchmark": "SPY buy-and-hold index: initial cash x close(s) / close(first session); same sessions as the portfolio",
    "excess_return": "total_return - benchmark_total_return; annualized_excess = cagr - benchmark_cagr (a simple difference, not a regression alpha)",
    "turnover": "mean Stage 4.7 weight turnover per executed rebalance; traded_notional_ratio = total traded notional / mean equity",
    "win_rate": "completed positions (quantity back to 0) with realised P&L > 0 / completed positions; average-cost basis including buy costs",
    "average_holding_sessions": "mean sessions from the opening fill (inclusive) to the closing fill (exclusive) of completed positions",
    "months": "calendar-month returns from the last session close of each month (the first month uses the first session as its base)",
    "worst_rolling_3m": "minimum return over any 63-session window; null with fewer than 64 sessions",
    "cash_weight": "cash / equity at each session close (mean, min, max)",
}


def _f(d) -> float:
    return float(d) if d is not None else float("nan")


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _stdev(xs: List[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def daily_returns(equity: List[Decimal]) -> List[float]:
    out = [0.0]
    for a, b in zip(equity, equity[1:]):
        out.append(float(b / a - 1) if a > 0 else 0.0)
    return out


def total_return(equity: List[Decimal], initial: Decimal) -> Optional[float]:
    return float(equity[-1] / initial - 1) if equity and initial > 0 else None


def cagr(equity: List[Decimal], initial: Decimal) -> Optional[float]:
    n = len(equity) - 1
    if n <= 0 or initial <= 0 or equity[-1] <= 0:
        return None
    return float(equity[-1] / initial) ** (SESSIONS_PER_YEAR / n) - 1


def annualized_volatility(returns: List[float]) -> Optional[float]:
    sd = _stdev(returns)
    return sd * math.sqrt(SESSIONS_PER_YEAR) if sd is not None else None


def sharpe(returns: List[float]) -> Optional[float]:
    sd = _stdev(returns)
    if not sd:
        return None
    return (_mean(returns) - RISK_FREE / SESSIONS_PER_YEAR) / sd * math.sqrt(SESSIONS_PER_YEAR)


def sortino(returns: List[float]) -> Optional[float]:
    if not returns:
        return None
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns))
    if downside == 0:
        return None
    return (_mean(returns) - RISK_FREE / SESSIONS_PER_YEAR) / downside * math.sqrt(SESSIONS_PER_YEAR)


def max_drawdown(equity: List[Decimal]) -> Dict[str, Optional[float]]:
    """{'max_drawdown': most negative drawdown (<= 0), 'max_drawdown_sessions': longest peak-to-recovery span}."""
    if not equity:
        return {"max_drawdown": None, "max_drawdown_sessions": None}
    peak, worst, longest, since_peak = equity[0], 0.0, 0, 0
    for e in equity:
        if e >= peak:
            peak, since_peak = e, 0
        else:
            since_peak += 1
            worst = min(worst, float(e / peak - 1))
        longest = max(longest, since_peak)
    return {"max_drawdown": worst, "max_drawdown_sessions": longest}


def monthly_returns(sessions: List[str], equity: List[Decimal]) -> List[dict]:
    """[{month, return}] from each month's last close; the first month's base is the first session's equity."""
    if not equity:
        return []
    out, last_by_month = [], {}
    for s, e in zip(sessions, equity):
        last_by_month[s[:7]] = e
    base = equity[0]
    for month in sorted(last_by_month):
        e = last_by_month[month]
        out.append({"month": month, "return": float(e / base - 1) if base > 0 else None})
        base = e
    return out


def worst_rolling(equity: List[Decimal], window: int = ROLLING_3M_SESSIONS) -> Optional[float]:
    if len(equity) <= window:
        return None
    return min(float(equity[i + window] / equity[i] - 1) for i in range(len(equity) - window) if equity[i] > 0)


def compute(definition: dict, equity_rows: List[dict], trades: List[dict], rebalances: List[dict], completed: List[dict], total_costs: Decimal) -> dict:
    initial = Decimal(definition["initial_cash"])
    sessions = [r["session_date"] for r in equity_rows]
    eq = [Decimal(r["equity"]) for r in equity_rows]
    bench = [Decimal(r["benchmark_index"]) for r in equity_rows]
    cash_w = [float(r["cash_weight"]) for r in equity_rows]
    rets = daily_returns(eq)
    dd = max_drawdown(eq)
    months = monthly_returns(sessions, eq)
    tr, btr = total_return(eq, initial), total_return(bench, initial)
    cg, bcg = cagr(eq, initial), cagr(bench, initial)
    executed = [r for r in rebalances if r.get("executed")]
    turnovers = [float(r["turnover"]) for r in executed if r.get("turnover") is not None]
    traded = sum((Decimal(r["traded_notional"]) for r in executed), Decimal(0))
    mean_equity = (sum(eq, Decimal(0)) / len(eq)) if eq else None
    wins = [p for p in completed if p["win"]]
    best = max(months, key=lambda m: m["return"] if m["return"] is not None else -math.inf) if months else None
    worst = min(months, key=lambda m: m["return"] if m["return"] is not None else math.inf) if months else None
    return {
        "n_sessions": len(eq), "first_session": sessions[0] if sessions else None, "last_session": sessions[-1] if sessions else None,
        "initial_cash": str(initial), "final_equity": str(eq[-1]) if eq else None,
        "total_return": tr, "cagr": cg, "annualized_volatility": annualized_volatility(rets), "sharpe": sharpe(rets), "sortino": sortino(rets),
        **dd,
        "benchmark": definition["benchmark"], "benchmark_total_return": btr, "benchmark_cagr": bcg,
        "excess_return": (tr - btr) if tr is not None and btr is not None else None,
        "annualized_excess": (cg - bcg) if cg is not None and bcg is not None else None,
        "n_rebalances": len(rebalances), "n_rebalances_executed": len(executed), "n_trades": len(trades),
        "n_fills": sum(1 for t in trades if t["filled_qty"] > 0),
        "mean_turnover": _mean(turnovers), "traded_notional": str(traded.quantize(Decimal("0.01"))),
        "traded_notional_ratio": float(traded / mean_equity) if mean_equity else None,
        "total_transaction_costs": str(Decimal(total_costs).quantize(Decimal("0.01"))),
        "n_completed_positions": len(completed), "win_rate": (len(wins) / len(completed)) if completed else None,
        "average_holding_sessions": _mean([float(p["holding_sessions"]) for p in completed]),
        "best_month": best, "worst_month": worst, "worst_rolling_3m": worst_rolling(eq),
        "cash_weight_mean": _mean(cash_w), "cash_weight_min": min(cash_w) if cash_w else None, "cash_weight_max": max(cash_w) if cash_w else None,
    }
