"""
rotation_walkforward/robustness.py — aggregate out-of-sample metrics, the stitched OOS curve and the robustness score
(DESIGN_49 §5–§6). Pure functions over per-window TEST metric dicts and equity rows.
"""
from __future__ import annotations

import math
import statistics
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Dict, List, Optional

from rotation_backtest import metrics as MX

from rotation_walkforward import ROBUSTNESS_VERSION

SCORE_WEIGHTS = {"s_sharpe": 0.30, "s_pos": 0.20, "s_beat": 0.15, "s_dd": 0.15, "s_turn": 0.10, "s_stab": 0.05, "s_cost": 0.05}
SCORE_FORMULA = ("rs_v1 = 0.30·clamp(median_oos_sharpe/2) + 0.20·positive_window_pct + 0.15·benchmark_beating_pct + "
                 "0.15·clamp(1 + worst_oos_max_drawdown/0.5) + 0.10·clamp(1 − mean_oos_turnover) + "
                 "0.05·(1 − (distinct_selected − 1)/max(windows − 1, 1)) + 0.05·clamp(1 − cost_sensitivity); "
                 "cost_sensitivity = clamp((CAGR_0bps − CAGR_20bps)/max(|CAGR_0bps|, 0.01)); every term clamped to [0, 1]; "
                 "a deterministic ranking aid, NOT a probability")
CONVENTIONS = {
    "oos_windows": "each TEST window is replayed from initial cash with its frozen configuration; medians are over windows with a COMPLETED test",
    "positive_window_pct": "windows with TEST total return > 0 / completed test windows",
    "benchmark_beating_pct": "windows with TEST total return > TEST benchmark total return / completed test windows",
    "worst_window_return": "minimum TEST total return across windows; oos_return_std = sample standard deviation of TEST total returns",
    "stitched": "non-overlapping test windows only: E(t) = E_prev_end × equity_w(t) / initial_cash (chain-linked); then the Stage 4.8 metric conventions",
    "parameter_drift": "per numeric field of the frozen configurations: number of changes between consecutive windows and the min / max value",
    "robustness_score": SCORE_FORMULA,
}


def _clamp(x: Optional[float]) -> float:
    return 0.0 if x is None or (isinstance(x, float) and math.isnan(x)) else max(0.0, min(1.0, x))


def _median(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _std(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return statistics.stdev(xs) if len(xs) >= 2 else None


def stitch(windows: List[dict], oos: List[dict], initial_cash: Decimal) -> Optional[List[dict]]:
    """Chain-link the TEST equity rows of consecutive non-overlapping windows. None when any pair overlaps or a test failed."""
    if not oos or any(o.get("test_status") != "COMPLETED" for o in oos):
        return None
    out: List[dict] = []
    level, blevel = initial_cash, initial_cash                                           # portfolio and benchmark chain separately
    last_date = None
    for w, o in zip(windows, oos):
        rows = o["equity"]
        if not rows:
            return None
        if last_date is not None and rows[0]["session_date"] <= last_date:
            return None                                                                      # overlapping: never concatenated
        base = initial_cash                                                                  # each window starts from initial cash
        for r in rows:
            eq = (level * Decimal(str(r["equity"])) / base).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            bench = (blevel * Decimal(str(r["benchmark_index"])) / base).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
            out.append({"session_date": r["session_date"], "equity": eq, "benchmark_index": bench, "cash_weight": Decimal(str(r["cash_weight"])),
                        "n_positions": r["n_positions"], "window_index": w["window_index"]})
        level, blevel = out[-1]["equity"], out[-1]["benchmark_index"]
        last_date = out[-1]["session_date"]
    return out


def parameter_drift(selections: List[dict]) -> Dict[str, dict]:
    fields = ("portfolio_size", "exit_rank", "cash_buffer_pct", "max_turnover_per_rotation", "rebalance_threshold")
    out: Dict[str, dict] = {}
    cfgs = [s["config"] for s in selections]
    for f in fields:
        vals = [float(c[f]) for c in cfgs]
        out[f] = {"changes": sum(1 for a, b in zip(vals, vals[1:]) if a != b), "min": min(vals), "max": max(vals)} if vals else None
    for k in (cfgs[0]["weights"] if cfgs else {}):
        vals = [float(c["weights"][k]) for c in cfgs]
        out[f"weights.{k}"] = {"changes": sum(1 for a, b in zip(vals, vals[1:]) if a != b), "min": min(vals), "max": max(vals)}
    return out


def aggregate(windows: List[dict], selections: List[dict], oos: List[dict], initial_cash: Decimal, overlapping: bool) -> dict:
    done = [o for o in oos if o.get("test_status") == "COMPLETED"]
    tm = [o["test_metrics"] for o in done]
    rets = [m.get("total_return") for m in tm]
    beats = [m.get("total_return") is not None and m.get("benchmark_total_return") is not None and m["total_return"] > m["benchmark_total_return"] for m in tm]
    hashes = [s["config_hash"] for s in selections]
    stitched = None if overlapping else stitch(windows, oos, initial_cash)
    stitched_metrics = None
    if stitched:
        stitched_metrics = {
            "n_sessions": len(stitched), "first_session": stitched[0]["session_date"], "last_session": stitched[-1]["session_date"],
            "total_return": MX.total_return([r["equity"] for r in stitched], initial_cash), "cagr": MX.cagr([r["equity"] for r in stitched], initial_cash),
            "sharpe": MX.sharpe(MX.daily_returns([r["equity"] for r in stitched])), "sortino": MX.sortino(MX.daily_returns([r["equity"] for r in stitched])),
            "annualized_volatility": MX.annualized_volatility(MX.daily_returns([r["equity"] for r in stitched])),
            **MX.max_drawdown([r["equity"] for r in stitched]),
            "benchmark_total_return": MX.total_return([r["benchmark_index"] for r in stitched], initial_cash),
            "benchmark_cagr": MX.cagr([r["benchmark_index"] for r in stitched], initial_cash),
        }
        stitched_metrics["excess_return"] = (stitched_metrics["total_return"] - stitched_metrics["benchmark_total_return"]
                                             if stitched_metrics["total_return"] is not None and stitched_metrics["benchmark_total_return"] is not None else None)
    return {
        "n_windows": len(windows), "n_completed_tests": len(done), "overlapping_tests": overlapping,
        "median_oos_cagr": _median([m.get("cagr") for m in tm]), "median_oos_sharpe": _median([m.get("sharpe") for m in tm]),
        "median_oos_sortino": _median([m.get("sortino") for m in tm]), "median_oos_excess_return": _median([m.get("excess_return") for m in tm]),
        "worst_oos_max_drawdown": min([m.get("max_drawdown") for m in tm if m.get("max_drawdown") is not None], default=None),
        "mean_oos_max_drawdown": _mean([m.get("max_drawdown") for m in tm]),
        "positive_window_pct": (sum(1 for r in rets if r is not None and r > 0) / len(done)) if done else None,
        "benchmark_beating_pct": (sum(beats) / len(done)) if done else None,
        "worst_window_return": min([r for r in rets if r is not None], default=None), "oos_return_std": _std(rets),
        "mean_oos_turnover": _mean([m.get("mean_turnover") for m in tm]), "oos_turnover_std": _std([m.get("mean_turnover") for m in tm]),
        "total_oos_costs": str(sum((Decimal(m.get("total_transaction_costs") or "0") for m in tm), Decimal(0))),
        "n_distinct_selected": len(set(hashes)), "n_selection_changes": sum(1 for a, b in zip(hashes, hashes[1:]) if a != b),
        "selected_sequence": hashes, "parameter_drift": parameter_drift(selections),
        "stitched_oos": stitched_metrics, "stitched_equity": stitched,
    }


def cost_sensitivity_value(cost_rows: List[dict]) -> Optional[float]:
    """clamp((CAGR at 0/0 − CAGR at 20/20) / max(|CAGR at 0/0|, 0.01)) from the stitched OOS CAGR of the cost matrix."""
    by = {(Decimal(r["transaction_cost_bps"]), Decimal(r["slippage_bps"])): r for r in cost_rows}
    a, b = by.get((Decimal(0), Decimal(0))), by.get((Decimal(20), Decimal(20)))
    if not a or not b or a.get("stitched_cagr") is None or b.get("stitched_cagr") is None:
        return None
    return _clamp((a["stitched_cagr"] - b["stitched_cagr"]) / max(abs(a["stitched_cagr"]), 0.01))


def robustness_score(agg: dict, cost_rows: List[dict]) -> dict:
    cs = cost_sensitivity_value(cost_rows)
    n = agg["n_windows"]
    comps = {"s_sharpe": _clamp((agg["median_oos_sharpe"] or 0.0) / 2.0), "s_pos": _clamp(agg["positive_window_pct"]),
             "s_beat": _clamp(agg["benchmark_beating_pct"]), "s_dd": _clamp(1.0 + (agg["worst_oos_max_drawdown"] or 0.0) / 0.5),
             "s_turn": _clamp(1.0 - (agg["mean_oos_turnover"] or 0.0)),
             "s_stab": _clamp(1.0 - (agg["n_distinct_selected"] - 1) / max(n - 1, 1)) if n else 0.0,
             "s_cost": _clamp(1.0 - cs) if cs is not None else 0.0}
    score = sum(SCORE_WEIGHTS[k] * comps[k] for k in SCORE_WEIGHTS)
    return {"version": ROBUSTNESS_VERSION, "score": round(score, 6), "components": comps, "weights": SCORE_WEIGHTS, "cost_sensitivity": cs,
            "formula": SCORE_FORMULA, "note": "a deterministic ranking aid favouring consistency; not a probability, not a forecast"}
