"""
rotation_walkforward/regimes.py — descriptive SPY regimes with no look-ahead (DESIGN_49 §9).

Label at session s uses SPY closes dated <= s only: TREND_UP when close > SMA200 else TREND_DOWN (UNKNOWN before 200
closes); HIGH_VOL when the trailing 20-session annualised realised volatility > 0.20 else LOW_VOL (UNKNOWN before 21
closes). A day's return is attributed to the regime known at the PREVIOUS session's close.
"""
from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from backtest.replay import BarSeries
from rotation_backtest import metrics as MX

SMA_SESSIONS, VOL_SESSIONS, VOL_THRESHOLD = 200, 20, 0.20
TREND_UP, TREND_DOWN, HIGH_VOL, LOW_VOL, UNKNOWN = "TREND_UP", "TREND_DOWN", "HIGH_VOL", "LOW_VOL", "UNKNOWN"
MIN_SESSIONS_FOR_SHARPE = 20


def labels(spy: BarSeries) -> Dict[str, dict]:
    """{session iso: {trend, vol}} for every SPY session, from bars dated <= that session only."""
    closes = [float(b.close) for b in spy.bars]
    out: Dict[str, dict] = {}
    run_sum = 0.0
    for i, (d, c) in enumerate(zip(spy.dates, closes)):
        run_sum += c
        if i >= SMA_SESSIONS:
            run_sum -= closes[i - SMA_SESSIONS]
        trend = UNKNOWN if i + 1 < SMA_SESSIONS else (TREND_UP if c > run_sum / SMA_SESSIONS else TREND_DOWN)
        if i >= VOL_SESSIONS:
            rets = [closes[j] / closes[j - 1] - 1.0 for j in range(i - VOL_SESSIONS + 1, i + 1)]
            m = sum(rets) / len(rets)
            sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
            vol = HIGH_VOL if sd > VOL_THRESHOLD else LOW_VOL
        else:
            vol = UNKNOWN
        out[d.isoformat()] = {"trend": trend, "vol": vol}
    return out


def _group_metrics(rows: List[dict], rets: List[float]) -> dict:
    eq = [Decimal(str(r["equity"])) for r in rows]
    n = len(rows)
    compounded = 1.0
    for r in rets:
        compounded *= 1.0 + r
    return {"sessions": n, "return": compounded - 1.0 if n else None, "sharpe": MX.sharpe(rets) if n >= MIN_SESSIONS_FOR_SHARPE else None,
            "max_drawdown": MX.max_drawdown(eq)["max_drawdown"] if eq else None,
            "mean_exposure": (sum(1.0 - float(r["cash_weight"]) for r in rows) / n) if n else None}


def breakdown(spy: BarSeries, stitched: Optional[List[dict]], rebalances: List[dict]) -> Optional[dict]:
    """OOS metrics by regime over the stitched curve; a day's return carries the regime of the previous session's close."""
    if not stitched:
        return None
    lab = labels(spy)
    eq = [Decimal(str(r["equity"])) for r in stitched]
    rets = MX.daily_returns(eq)
    sessions = [r["session_date"] for r in stitched]
    prev = {s: (lab.get(sessions[i - 1]) if i > 0 else None) for i, s in enumerate(sessions)}
    out: Dict[str, dict] = {}
    for axis in ("trend", "vol"):
        groups: Dict[str, dict] = {}
        for i, r in enumerate(stitched):
            if i == 0:
                continue
            key = (prev[r["session_date"]] or {}).get(axis, UNKNOWN)
            g = groups.setdefault(key, {"rows": [], "rets": [], "turnover": []})
            g["rows"].append(r)
            g["rets"].append(rets[i])
        for rb in rebalances:
            if not rb.get("executed") or rb.get("turnover") is None:
                continue
            key = (lab.get(rb["signal_session"]) or {}).get(axis, UNKNOWN)
            groups.setdefault(key, {"rows": [], "rets": [], "turnover": []})["turnover"].append(float(rb["turnover"]))
        out[axis] = {k: {**_group_metrics(g["rows"], g["rets"]), "mean_turnover": (sum(g["turnover"]) / len(g["turnover"])) if g["turnover"] else None}
                     for k, g in sorted(groups.items())}
    return {"method": f"TREND_UP: SPY close > SMA{SMA_SESSIONS}; HIGH_VOL: trailing {VOL_SESSIONS}-session annualised realised vol > {VOL_THRESHOLD}; "
                      "labels from bars <= each session; a day's return carries the previous close's label; descriptive only", **out}
