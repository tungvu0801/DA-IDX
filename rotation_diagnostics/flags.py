"""
rotation_diagnostics/flags.py — documented, versioned research flags and the PASS / WARN / FAIL scorecard (DESIGN_51 §7).
Thresholds are simple constants; these are research flags, not trading recommendations.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from rotation_diagnostics import FLAGS_VERSION

THRESHOLDS = {"dominant_cagr_drop": 0.25, "top3_share_high": 0.50, "sector_share_high": 0.50, "sector_weight_high": 0.50, "window_share_high": 0.50,
              "selection_unstable_ratio": 0.60, "regime_min_sessions": 60, "regime_min_share": 0.10, "value_added_min_window_pct": 0.60,
              "drawdown_improvement_points": 0.02, "scorecard_top3_warn": 0.40, "scorecard_top3_fail": 0.60, "scorecard_sector_warn": 0.40,
              "scorecard_sector_fail": 0.60, "selection_warn_ratio": 0.40}
DESCRIPTIONS = {
    "BENCHMARK_UNDERPERFORM": "stitched OOS return below the equal-weight universe benchmark at the run's costs",
    "ROTATION_VALUE_ADDED": "beats SPY, EW and buy-and-hold stitched, beats EW in >= 60 % of windows, excess vs EW still > 0 at 10/10 bps, no dominant contributor, no sector dependence, top-3 share <= 50 %",
    "UNIVERSE_ALPHA_DOMINANT": "EW beats SPY and the EW − SPY excess exceeds the strategy − EW excess: the universe explains more than the rotation",
    "SYMBOL_CONCENTRATION_HIGH": "top-3 symbols > 50 % of positive P&L", "SECTOR_CONCENTRATION_HIGH": "top sector > 50 % of positive P&L or max sector weight > 50 %",
    "COST_FRAGILE": "excess vs EW or vs SPY non-positive at or before 10/10 bps", "DRAW_DOWN_NOT_IMPROVED": "strategy max drawdown not better than EW's",
    "WINDOW_CONCENTRATION_HIGH": "best window > 50 % of total log return", "SELECTION_UNSTABLE": "campaign distinct TRAIN selections / windows > 0.60",
    "WEAK_REGIME_SAMPLE": "a regime has < 60 sessions or < 10 % of OOS sessions", "DOMINANT_CONTRIBUTOR": "removing one symbol cuts stitched CAGR by >= 25 % of |CAGR| or flips excess vs SPY to <= 0",
    "SECTOR_DEPENDENCE": "removing one sector cuts stitched CAGR by >= 25 % of |CAGR| or flips excess vs SPY to <= 0",
}


def _ge(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and a > b


def ablation_flag(full: dict, loo: dict) -> bool:
    """DOMINANT_CONTRIBUTOR / SECTOR_DEPENDENCE rule on stitched metrics (full vs reduced universe)."""
    c0, c1 = full.get("cagr"), loo.get("cagr")
    e0, e1 = full.get("excess_return"), loo.get("excess_return")
    if c0 is None or c1 is None:
        return False
    drop = (c0 - c1) >= THRESHOLDS["dominant_cagr_drop"] * abs(c0) if c0 != 0 else c1 < 0
    flip = e0 is not None and e1 is not None and e0 > 0 >= e1
    return bool(drop or flip)


def compute_flags(summary: dict) -> List[str]:
    """summary: the per-configuration diagnostic summary built by the engine."""
    s = summary
    st, ew, bh, spy = s["stitched"]["strategy"], s["stitched"]["EW_REBALANCED"], s["stitched"]["BUY_HOLD"], s["stitched"]["SPY"]
    cost = s["cost"]
    flags = []
    if not _ge(st.get("total_return"), ew.get("total_return")):
        flags.append("BENCHMARK_UNDERPERFORM")
    ew_vs_spy = (ew.get("total_return") or 0) - (spy.get("total_return") or 0)
    st_vs_ew = (st.get("total_return") or 0) - (ew.get("total_return") or 0)
    if _ge(ew.get("total_return"), spy.get("total_return")) and ew_vs_spy > st_vs_ew:
        flags.append("UNIVERSE_ALPHA_DOMINANT")
    if (s["attribution"]["concentration"].get("top3") or 0) > THRESHOLDS["top3_share_high"]:
        flags.append("SYMBOL_CONCENTRATION_HIGH")
    sc = s["attribution"]["sector_concentration"]
    if (sc.get("top_sector_share") or 0) > THRESHOLDS["sector_share_high"] or (sc.get("max_sector_weight") or 0) > THRESHOLDS["sector_weight_high"]:
        flags.append("SECTOR_CONCENTRATION_HIGH")
    firsts = [cost.get("first_nonpositive_vs_ew"), cost.get("first_nonpositive_vs_spy")]
    if any(f is not None and float(f) <= 10.0 for f in firsts):
        flags.append("COST_FRAGILE")
    if st.get("max_drawdown") is not None and ew.get("max_drawdown") is not None and st["max_drawdown"] <= ew["max_drawdown"]:
        flags.append("DRAW_DOWN_NOT_IMPROVED")
    if (s["attribution"]["window_concentration"].get("best_window_share_of_log_return") or 0) > THRESHOLDS["window_share_high"]:
        flags.append("WINDOW_CONCENTRATION_HIGH")
    if s.get("selection", {}).get("ratio") is not None and s["selection"]["ratio"] > THRESHOLDS["selection_unstable_ratio"]:
        flags.append("SELECTION_UNSTABLE")
    if s["regimes"].get("weak"):
        flags.append("WEAK_REGIME_SAMPLE")
    if any(r["dominant"] for r in s["leave_one_out"]):
        flags.append("DOMINANT_CONTRIBUTOR")
    if any(r["dependent"] for r in s["leave_sector_out"]):
        flags.append("SECTOR_DEPENDENCE")
    wins = s["windows_summary"]
    value_added = (_ge(st.get("total_return"), spy.get("total_return")) and _ge(st.get("total_return"), ew.get("total_return")) and _ge(st.get("total_return"), bh.get("total_return"))
                   and (wins.get("pct_beating_EW_REBALANCED") or 0) >= THRESHOLDS["value_added_min_window_pct"]
                   and (cost.get("excess_vs_ew_at_10") or 0) > 0 and "DOMINANT_CONTRIBUTOR" not in flags and "SECTOR_DEPENDENCE" not in flags
                   and "SYMBOL_CONCENTRATION_HIGH" not in flags)
    if value_added:
        flags.append("ROTATION_VALUE_ADDED")
    return flags


def _verdict(cond_pass: bool, cond_warn: bool) -> str:
    return "PASS" if cond_pass else ("WARN" if cond_warn else "FAIL")


def scorecard(summary: dict, flags: List[str]) -> List[dict]:
    s = summary
    st, ew, bh, spy = s["stitched"]["strategy"], s["stitched"]["EW_REBALANCED"], s["stitched"]["BUY_HOLD"], s["stitched"]["SPY"]
    wins, cost = s["windows_summary"], s["cost"]
    rows = []

    def beats(name, bench):
        exc = (st.get("total_return") or 0) - (bench.get("total_return") or 0)
        pct = wins.get(f"pct_beating_{name}") or 0
        rows.append({"check": f"beats {name}", "verdict": _verdict(exc > 0 and pct >= 0.5, exc > 0), "value": {"stitched_excess": exc, "pct_windows": pct}})
    beats("SPY", spy)
    beats("EW_REBALANCED", ew)
    beats("BUY_HOLD", bh)
    dd_s, dd_e = st.get("max_drawdown"), ew.get("max_drawdown")
    imp = (dd_s - dd_e) if dd_s is not None and dd_e is not None else None
    rows.append({"check": "improves drawdown vs EW", "verdict": _verdict(imp is not None and imp >= THRESHOLDS["drawdown_improvement_points"], imp is not None and imp >= 0),
                 "value": {"strategy_max_dd": dd_s, "ew_max_dd": dd_e}})
    rows.append({"check": "survives 10 bps vs EW", "verdict": _verdict((cost.get("excess_vs_ew_at_10") or 0) > 0, False), "value": cost.get("excess_vs_ew_at_10")})
    rows.append({"check": "survives 20 bps vs EW", "verdict": _verdict((cost.get("excess_vs_ew_at_20") or 0) > 0, False), "value": cost.get("excess_vs_ew_at_20")})
    t3 = s["attribution"]["concentration"].get("top3")
    rows.append({"check": "top-3 contribution share", "verdict": _verdict(t3 is not None and t3 < THRESHOLDS["scorecard_top3_warn"], t3 is not None and t3 <= THRESHOLDS["scorecard_top3_fail"]), "value": t3})
    ss = s["attribution"]["sector_concentration"].get("top_sector_share")
    rows.append({"check": "sector concentration", "verdict": _verdict(ss is not None and ss < THRESHOLDS["scorecard_sector_warn"], ss is not None and ss <= THRESHOLDS["scorecard_sector_fail"]), "value": ss})
    n_dom = sum(1 for r in s["leave_one_out"] if r["dominant"])
    rows.append({"check": "leave-one-out stability", "verdict": _verdict(n_dom == 0, n_dom == 1), "value": {"dominant_symbols": [r["symbol"] for r in s["leave_one_out"] if r["dominant"]]}})
    n_dep = sum(1 for r in s["leave_sector_out"] if r["dependent"])
    rows.append({"check": "leave-sector-out stability", "verdict": _verdict(n_dep == 0, n_dep == 1), "value": {"dependent_sectors": [r["sector"] for r in s["leave_sector_out"] if r["dependent"]]}})
    weak = s["regimes"].get("weak") or []
    minimum = min((v["sessions"] for v in s["regimes"]["rows"].values()), default=0)
    rows.append({"check": "regime evidence quality", "verdict": _verdict(not weak, minimum >= 20), "value": {"weak_regimes": weak}})
    ratio = s.get("selection", {}).get("ratio")
    rows.append({"check": "selection stability", "verdict": _verdict(ratio is not None and ratio <= THRESHOLDS["selection_warn_ratio"], ratio is not None and ratio <= THRESHOLDS["selection_unstable_ratio"]), "value": ratio})
    return rows


def describe() -> dict:
    return {"version": FLAGS_VERSION, "thresholds": THRESHOLDS, "flags": DESCRIPTIONS, "note": "research flags and a PASS / WARN / FAIL scorecard, not trading recommendations"}
