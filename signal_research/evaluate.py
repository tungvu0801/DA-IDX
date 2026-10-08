"""
signal_research/evaluate.py — per-variant evidence, factor verdicts, research flags and improvement criteria (DESIGN_52 §6–§8).

Reuses the Stage 5.1 helpers (stitched curves, curve metrics, benchmark candidates, attribution, regime rows, dx_v1 flags
and scorecard) through the ResearchEvaluator. Ablations are bounded (top-2 contributors, top sector) and documented.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from rotation import universe as U
from rotation_diagnostics import attribution as AT
from rotation_diagnostics import benchmarks as BM
from rotation_diagnostics import engine as DE
from rotation_diagnostics import flags as FL
from rotation_walkforward import regimes as RG
from signal_research import CRITERIA_VERSION, FACTOR_VERDICT_VERSION, FLAGS_VERSION
from signal_research import sector as SC
from signal_research import variant_engine as VE
from signal_research import variants as V

N_LOO, N_LSO = 2, 1
TOL = {"factor_cagr": 0.01, "factor_sharpe": 0.05, "factor_max_dd": 0.01, "factor_median_excess": 0.005, "factor_pct": 0.0,
       "criteria_window_pct": 0.60, "criteria_sharpe": 0.10, "criteria_drawdown": 0.02, "criteria_top3": 0.50, "criteria_regime": 0.02, "criteria_churn": 0.05,
       "concentration_reduced": 0.10, "cost_improved": 0.01, "drawdown_improved": 0.02, "return_sacrificed": 0.02, "neutrality_share": 0.10, "neutrality_sharpe": 0.05,
       "overlay_dd": 0.02, "overlay_sharpe": 0.05, "overlay_trend_down": 0.01, "overdefensive_cagr": 0.03}
FACTOR_RULE = (f"{FACTOR_VERDICT_VERSION}: remove-X vs baseline on the same TEST windows over five comparisons — stitched CAGR (tol {TOL['factor_cagr']}), median OOS "
               f"Sharpe ({TOL['factor_sharpe']}), stitched max drawdown ({TOL['factor_max_dd']}), median excess vs EW ({TOL['factor_median_excess']}), % windows beating EW; "
               "HELPFUL = removal worse on >= 3 and better on <= 1; HARMFUL = better on >= 3 and worse on <= 1; else NEUTRAL")
CRITERIA_RULE = (f"{CRITERIA_VERSION}: median excess vs EW >= 0 · windows beating EW >= {TOL['criteria_window_pct']:.0%} · median OOS Sharpe >= EW's median OOS Sharpe − "
                 f"{TOL['criteria_sharpe']} · stitched max drawdown >= EW's − {TOL['criteria_drawdown']} · excess vs EW at 10/10 bps > 0 · top-3 share <= {TOL['criteria_top3']} · "
                 f"no SECTOR_DEPENDENCE · no DOMINANT_CONTRIBUTOR · TREND_DOWN and HIGH_VOL excess vs EW >= baseline's − {TOL['criteria_regime']} · churn <= baseline's + "
                 f"{TOL['criteria_churn']}; all must hold; never relaxed")
ABLATION_RULE = f"leave-one-out for the {N_LOO} largest positive contributors and leave-sector-out for the {N_LSO} largest sector (same windows, same costs, no re-selection)"
DESCRIPTIONS = {
    "EW_STILL_DOMINANT": "EW_REBALANCED stitched total return and Sharpe both >= the variant's", "SPY_STILL_DOMINANT": "SPY stitched total return and Sharpe both >= the variant's",
    "CONCENTRATION_REDUCED": f"top-3 share or top-sector share <= baseline − {TOL['concentration_reduced']}", "COST_ROBUSTNESS_IMPROVED": f"excess vs EW at 20/20 bps >= baseline + {TOL['cost_improved']}",
    "DRAWDOWN_IMPROVED": f"stitched max drawdown >= baseline + {TOL['drawdown_improved']}", "RETURN_SACRIFICED_FOR_RISK": f"DRAWDOWN_IMPROVED and stitched CAGR <= baseline − {TOL['return_sacrificed']}",
    "SECTOR_NEUTRALITY_HELPFUL": f"sector-neutral ranking: top-sector share <= baseline − {TOL['neutrality_share']} and median OOS Sharpe >= baseline − {TOL['neutrality_sharpe']}",
    "SECTOR_CAP_BINDING": "the sector cap changed at least one selection or made at least one rebalance infeasible",
    "REGIME_OVERLAY_HELPFUL": f"vs the overlay-free counterpart: max drawdown >= +{TOL['overlay_dd']}, Sharpe >= −{TOL['overlay_sharpe']}, TREND_DOWN excess vs EW >= −{TOL['overlay_trend_down']}",
    "REGIME_OVERLAY_OVERDEFENSIVE": f"vs the counterpart: CAGR <= −{TOL['overdefensive_cagr']} without a {TOL['overlay_dd']} drawdown gain",
    "FACTOR_HELPFUL": "the removed factor's verdict is HELPFUL (removing it hurts)", "FACTOR_HARMFUL": "the removed factor's verdict is HARMFUL (removing it helps)",
    "IMPROVEMENT_CRITERIA_MET": "every sc_v1 criterion holds", "NO_SIGNAL_IMPROVEMENT": "run level: no variant meets the sc_v1 criteria"}


def describe() -> dict:
    return {"version": FLAGS_VERSION, "criteria_version": CRITERIA_VERSION, "factor_verdict_version": FACTOR_VERDICT_VERSION, "tolerances": TOL, "flags": DESCRIPTIONS,
            "factor_rule": FACTOR_RULE, "criteria_rule": CRITERIA_RULE, "ablation_rule": ABLATION_RULE, "note": "research flags and predeclared criteria, not trading recommendations"}


# ---- benchmarks ---------------------------------------------------------------------------------------------------------------------------

def benchmarks(ev: VE.ResearchEvaluator, base_config: dict, n_universe: int, windows: List[dict], defn: dict, costs: List[Tuple[str, str]], run_cost: str, run_slip: str,
               initial: Decimal) -> dict:
    """The Stage 5.1 benchmark rows / curves / per-window results at every cost point plus SPY once."""
    rows, curves, results, oos_by = [], {}, {}, {}
    bcands = {k: BM.benchmark_candidate(base_config, n_universe, k) for k in (BM.EW_REBALANCED, BM.BUY_HOLD)}
    for cost, slip in costs:
        for k, bc in bcands.items():
            oos, res, st = DE._stitched(ev, bc, windows, defn, cost, slip, initial)
            curves[(k, cost, slip)], results[(k, cost, slip)], oos_by[(k, cost, slip)] = st, res, oos
            rows.append({"benchmark": k, "transaction_cost_bps": cost, "slippage_bps": slip, "definition": {k2: v for k2, v in bc.items() if k2 != "config"} | {"config": bc["config"]},
                         "metrics": DE._curve_metrics(st or [], initial),
                         "windows": [{"window_index": o["window_index"], "status": o["test_status"], "total_return": o["test_metrics"].get("total_return"), "sharpe": o["test_metrics"].get("sharpe"),
                                      "cagr": o["test_metrics"].get("cagr"), "max_drawdown": o["test_metrics"].get("max_drawdown"), "mean_turnover": o["test_metrics"].get("mean_turnover")} for o in oos]})
    spy_rows = BM.spy_rows(curves[(BM.EW_REBALANCED, run_cost, run_slip)] or [], initial)
    rows.append({"benchmark": BM.SPY, "transaction_cost_bps": "0.0000", "slippage_bps": "0.0000", "definition": {"kind": BM.SPY, "convention": BM.CONVENTIONS[BM.SPY]},
                 "metrics": DE._curve_metrics(spy_rows, initial), "windows": [{"window_index": w["window_index"]} for w in windows]})
    curves[(BM.SPY, "0.0000", "0.0000")] = spy_rows
    ew_oos = oos_by[(BM.EW_REBALANCED, run_cost, run_slip)]
    ew_sharpes = sorted(o["test_metrics"]["sharpe"] for o in ew_oos if o["test_status"] == "COMPLETED" and o["test_metrics"].get("sharpe") is not None)
    return {"rows": rows, "curves": curves, "results": results, "oos": oos_by, "spy_metrics": rows[-1]["metrics"], "spy_curve": spy_rows,
            "ew_median_sharpe": ew_sharpes[len(ew_sharpes) // 2] if ew_sharpes else None}


def _metric(rows: List[dict], benchmark: str, cost: str) -> dict:
    return next(b["metrics"] for b in rows if b["benchmark"] == benchmark and b["transaction_cost_bps"] == cost)


def _median(xs: List[float]) -> Optional[float]:
    ys = sorted(x for x in xs if x is not None)
    return ys[len(ys) // 2] if ys else None


# ---- one variant -------------------------------------------------------------------------------------------------------------------------

def variant_summary(ev: VE.ResearchEvaluator, variant: dict, windows: List[dict], defn: dict, costs: List[Tuple[str, str]], run_cost: str, run_slip: str, initial: Decimal,
                    bench: dict, series: dict, sector_map: Dict[str, str], universe: U.ResolvedUniverse, path, now) -> Tuple[dict, int]:
    """(summary, extra evaluations). The Stage 5.1 per-configuration summary with per-window OOS statistics, bounded ablations and
    cap / overlay observations; Stage 5.1 flags and scorecard are attached by the caller."""
    cand = {"config_hash": variant["config_hash"], "config": variant["config"], "label": variant["label"], "research": variant["research"]}
    oos, results, st = DE._stitched(ev, cand, windows, defn, run_cost, run_slip, initial)
    if st is None:
        return {"status": "INCOMPLETE", "detail": "a TEST replay failed; no stitched curve", "oos": oos}, 0
    spy = series[DE.SIM.BENCHMARK]
    strat = DE._curve_metrics(st, initial)
    ew_m, bh_m, spy_m = _metric(bench["rows"], BM.EW_REBALANCED, run_cost), _metric(bench["rows"], BM.BUY_HOLD, run_cost), bench["spy_metrics"]
    tm = [o["test_metrics"] for o in oos]
    per_window = {"median_cagr": _median([m.get("cagr") for m in tm]), "median_sharpe": _median([m.get("sharpe") for m in tm]), "median_sortino": _median([m.get("sortino") for m in tm]),
                  "worst_max_drawdown": min((m.get("max_drawdown") for m in tm if m.get("max_drawdown") is not None), default=None),
                  "pct_positive": (sum(1 for m in tm if (m.get("total_return") or 0) > 0) / len(tm)) if tm else None, "n_windows": len(tm)}
    attr = AT.attribute(results, series, sector_map, initial)
    attr["reconciled"] = AT.reconcile(attr, results, initial)
    cost_rows = []
    for cost, slip in costs:
        _, _, stc = DE._stitched(ev, cand, windows, defn, cost, slip, initial)
        mc = DE._curve_metrics(stc or [], initial)
        ewc, bhc = _metric(bench["rows"], BM.EW_REBALANCED, cost), _metric(bench["rows"], BM.BUY_HOLD, cost)
        ex = lambda a, b: (a - b) if a is not None and b is not None else None  # noqa: E731
        cost_rows.append({"transaction_cost_bps": cost, "slippage_bps": slip, "strategy": mc, "EW_REBALANCED": ewc, "BUY_HOLD": bhc, "excess_vs_spy": ex(mc["total_return"], spy_m["total_return"]),
                          "excess_vs_ew": ex(mc["total_return"], ewc["total_return"]), "excess_vs_bh": ex(mc["total_return"], bhc["total_return"]),
                          "sharpe_vs_ew": ex(mc["sharpe"], ewc["sharpe"]), "sharpe_vs_spy": ex(mc["sharpe"], spy_m["sharpe"])})
    cost_rows.sort(key=lambda r: Decimal(r["transaction_cost_bps"]))
    at = lambda bps, key: next((r[key] for r in cost_rows if Decimal(r["transaction_cost_bps"]) == Decimal(bps)), None)  # noqa: E731
    cost = {"rows": cost_rows, "first_nonpositive_vs_ew": next((r["transaction_cost_bps"] for r in cost_rows if r["excess_vs_ew"] is not None and r["excess_vs_ew"] <= 0), None),
            "first_nonpositive_vs_spy": next((r["transaction_cost_bps"] for r in cost_rows if r["excess_vs_spy"] is not None and r["excess_vs_spy"] <= 0), None),
            "excess_vs_ew_at_10": at(10, "excess_vs_ew"), "excess_vs_ew_at_20": at(20, "excess_vs_ew"), "excess_vs_spy_at_10": at(10, "excess_vs_spy"), "excess_vs_spy_at_20": at(20, "excess_vs_spy")}
    # bounded ablations
    full = {"cagr": strat["cagr"], "sharpe": strat["sharpe"], "max_drawdown": strat["max_drawdown"],
            "excess_return": (strat["total_return"] - spy_m["total_return"]) if strat["total_return"] is not None and spy_m["total_return"] is not None else None}
    extra = 0

    def ablate(symbols_left: List[str]):
        nonlocal extra
        if not symbols_left:
            return "EMPTY_UNIVERSE", {}
        uni2 = U.resolve_universe("CUSTOM", None, symbols_left, path=path)
        ev2 = VE.ResearchEvaluator(uni2, {s: series[s] for s in list(symbols_left) + [DE.SIM.BENCHMARK] if s in series}, now, sector_map)
        _, _, st2 = DE._stitched(ev2, cand, windows, defn, run_cost, run_slip, initial)
        extra += ev2.evaluations
        if st2 is None:
            return "FAILED", {}
        m2 = DE._curve_metrics(st2, initial)
        red = {"cagr": m2["cagr"], "sharpe": m2["sharpe"], "max_drawdown": m2["max_drawdown"],
               "excess_return": (m2["total_return"] - spy_m["total_return"]) if m2["total_return"] is not None and spy_m["total_return"] is not None else None}
        return "COMPLETED", {"reduced": red, "deltas": {k: ((red[k] - full[k]) if red[k] is not None and full[k] is not None else None) for k in full}, "flag": FL.ablation_flag(full, red)}
    top_syms = [x["symbol"] for x in attr["symbols"] if Decimal(x["pnl"]) > 0][:N_LOO]
    loo = []
    for s in top_syms:
        status, r = ablate([x for x in universe.symbols if x != s])
        loo.append({"symbol": s, "status": status, "deltas": r.get("deltas", {}), "reduced": r.get("reduced", {}), "dominant": bool(r.get("flag"))})
    lso = []
    for sec in [x["sector"] for x in attr["sectors"] if Decimal(x["pnl"]) > 0][:N_LSO]:
        status, r = ablate([x for x in universe.symbols if SC.sector_of(x, sector_map) != sec])
        lso.append({"sector": sec, "status": status, "deltas": r.get("deltas", {}), "reduced": r.get("reduced", {}), "dependent": bool(r.get("flag"))})
    # windows
    ew_res, bh_res = bench["results"][(BM.EW_REBALANCED, run_cost, run_slip)], bench["results"][(BM.BUY_HOLD, run_cost, run_slip)]
    lab = RG.labels(spy)
    wrows = []
    for i, (w, o) in enumerate(zip(windows, oos)):
        m = o["test_metrics"]
        ewr, bhr = ew_res[i], bh_res[i]
        ew_ret = float(ewr.equity[-1]["equity"] / initial - 1) if ewr.status == "COMPLETED" and ewr.equity else None
        bh_ret = float(bhr.equity[-1]["equity"] / initial - 1) if bhr.status == "COMPLETED" and bhr.equity else None
        sess = [e["session_date"] for e in o["equity"]]
        r_ = m.get("total_return")
        ex = lambda a, b: (a - b) if a is not None and b is not None else None  # noqa: E731
        wrows.append({"window_index": w["window_index"], "test_first_session": w["test_first_session"], "test_last_session": w["test_last_session"], "status": o["test_status"],
                      "strategy_return": r_, "spy_return": m.get("benchmark_total_return"), "ew_return": ew_ret, "bh_return": bh_ret, "excess_vs_spy": ex(r_, m.get("benchmark_total_return")),
                      "excess_vs_ew": ex(r_, ew_ret), "excess_vs_bh": ex(r_, bh_ret), "strategy_max_drawdown": m.get("max_drawdown"), "sharpe": m.get("sharpe"), "mean_turnover": m.get("mean_turnover"),
                      "dominant_contributors": attr["windows"][i].get("top") if i < len(attr["windows"]) else None,
                      "regime_mix": {"trend_up_share": (sum(1 for s in sess if (lab.get(s) or {}).get("trend") == RG.TREND_UP) / len(sess)) if sess else None,
                                     "high_vol_share": (sum(1 for s in sess if (lab.get(s) or {}).get("vol") == RG.HIGH_VOL) / len(sess)) if sess else None}})
    done = [r for r in wrows if r["status"] == "COMPLETED"]
    pct = lambda key: (lambda xs: (sum(1 for x in xs if x > 0) / len(xs)) if xs else None)([r[key] for r in done if r[key] is not None])  # noqa: E731
    wsum = {"pct_beating_SPY": pct("excess_vs_spy"), "pct_beating_EW_REBALANCED": pct("excess_vs_ew"), "pct_beating_BUY_HOLD": pct("excess_vs_bh"),
            "median_excess_vs_spy": _median([r["excess_vs_spy"] for r in done]), "median_excess_vs_ew": _median([r["excess_vs_ew"] for r in done]), "median_excess_vs_bh": _median([r["excess_vs_bh"] for r in done]),
            "worst_relative_window_vs_ew": min(((r["excess_vs_ew"], r["window_index"]) for r in done if r["excess_vs_ew"] is not None), default=(None, None))}
    all_rebs = [rb for res in results if res.status == "COMPLETED" for rb in res.rebalances]
    regimes = DE._regime_rows(spy, st, {"spy": bench["spy_curve"], "ew": bench["curves"][(BM.EW_REBALANCED, run_cost, run_slip)] or [], "bh": bench["curves"][(BM.BUY_HOLD, run_cost, run_slip)] or []}, all_rebs)
    # cap / overlay observations from the deterministic replay rows
    cap_changed = sum(1 for rb in all_rebs if any(t.get("reason") == SC.TOP_N_SECTOR_CAP for t in rb["proposal"]["targets"]) or any(i.get("reason") == SC.EXIT_SECTOR_CAP for i in rb["proposal"]["items"]))
    cap_infeasible = sum(1 for rb in all_rebs if rb.get("engine_status") == SC.SECTOR_CAP_INFEASIBLE)
    mean_exposure = (sum(1.0 - float(r["cash_weight"]) for r in st) / len(st)) if st else None
    summary = {"status": "COMPLETED", "stitched": {"strategy": strat, "EW_REBALANCED": ew_m, "BUY_HOLD": bh_m, "SPY": spy_m}, "per_window": per_window, "attribution": attr, "cost": cost,
               "leave_one_out": loo, "leave_sector_out": lso, "windows": wrows, "windows_summary": wsum, "regimes": regimes, "selection": {"ratio": None, "note": "churn-based; see criteria"},
               "drawdown": {k: {"max_drawdown": m["max_drawdown"], "max_drawdown_sessions": m["max_drawdown_sessions"], "worst_month": m["worst_month"]}
                            for k, m in (("strategy", strat), ("EW_REBALANCED", ew_m), ("BUY_HOLD", bh_m), ("SPY", spy_m))},
               "construction": {"rebalances": len(all_rebs), "executed": sum(1 for rb in all_rebs if rb.get("executed")), "cap_changed_selection": cap_changed, "cap_infeasible": cap_infeasible,
                                "mean_exposure": mean_exposure, "n_sessions": len(st)}}
    return summary, extra


# ---- second pass: verdicts, flags, criteria --------------------------------------------------------------------------------------------------

def _g(s: dict, *keys, default=None):
    cur = s
    for k in keys:
        if cur is None or not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def factor_verdict(removed: dict, base: dict) -> dict:
    """fv_v1: compare the remove-X summary with the baseline summary (both COMPLETED)."""
    comps = []

    def cmp(name, a, b, tol, higher_better=True):
        if a is None or b is None:
            comps.append({"metric": name, "variant": a, "baseline": b, "result": "NA"})
            return
        d = (a - b) if higher_better else (b - a)
        comps.append({"metric": name, "variant": a, "baseline": b, "result": "WORSE" if d < -tol else ("BETTER" if d > tol else "SAME")})
    cmp("stitched_cagr", _g(removed, "stitched", "strategy", "cagr"), _g(base, "stitched", "strategy", "cagr"), TOL["factor_cagr"])
    cmp("median_oos_sharpe", _g(removed, "per_window", "median_sharpe"), _g(base, "per_window", "median_sharpe"), TOL["factor_sharpe"])
    cmp("stitched_max_drawdown", _g(removed, "stitched", "strategy", "max_drawdown"), _g(base, "stitched", "strategy", "max_drawdown"), TOL["factor_max_dd"])
    cmp("median_excess_vs_ew", _g(removed, "windows_summary", "median_excess_vs_ew"), _g(base, "windows_summary", "median_excess_vs_ew"), TOL["factor_median_excess"])
    cmp("pct_beating_ew", _g(removed, "windows_summary", "pct_beating_EW_REBALANCED"), _g(base, "windows_summary", "pct_beating_EW_REBALANCED"), TOL["factor_pct"])
    worse, better = sum(1 for c in comps if c["result"] == "WORSE"), sum(1 for c in comps if c["result"] == "BETTER")
    verdict = "FACTOR_HELPFUL" if worse >= 3 and better <= 1 else ("FACTOR_HARMFUL" if better >= 3 and worse <= 1 else "FACTOR_NEUTRAL")
    return {"verdict": verdict, "worse": worse, "better": better, "comparisons": comps, "rule": FACTOR_RULE}


def criteria(s: dict, base: dict, ew_median_sharpe: Optional[float], flags51: List[str]) -> dict:
    """sc_v1 — every check must hold."""
    checks = []

    def ck(name, ok, value):
        checks.append({"check": name, "pass": bool(ok), "value": value})
    me = _g(s, "windows_summary", "median_excess_vs_ew")
    ck("median excess vs EW >= 0", me is not None and me >= 0, me)
    pb = _g(s, "windows_summary", "pct_beating_EW_REBALANCED")
    ck(f"windows beating EW >= {TOL['criteria_window_pct']:.0%}", pb is not None and pb >= TOL["criteria_window_pct"], pb)
    ms = _g(s, "per_window", "median_sharpe")
    ck(f"median OOS Sharpe >= EW median − {TOL['criteria_sharpe']}", ms is not None and ew_median_sharpe is not None and ms >= ew_median_sharpe - TOL["criteria_sharpe"], {"variant": ms, "ew": ew_median_sharpe})
    dd, dde = _g(s, "stitched", "strategy", "max_drawdown"), _g(s, "stitched", "EW_REBALANCED", "max_drawdown")
    ck(f"max drawdown >= EW − {TOL['criteria_drawdown']}", dd is not None and dde is not None and dd >= dde - TOL["criteria_drawdown"], {"variant": dd, "ew": dde})
    e10 = _g(s, "cost", "excess_vs_ew_at_10")
    ck("excess vs EW at 10/10 bps > 0", e10 is not None and e10 > 0, e10)
    t3 = _g(s, "attribution", "concentration", "top3")
    ck(f"top-3 share <= {TOL['criteria_top3']}", t3 is not None and t3 <= TOL["criteria_top3"], t3)
    ck("no SECTOR_DEPENDENCE", "SECTOR_DEPENDENCE" not in flags51, [x["sector"] for x in s.get("leave_sector_out", []) if x.get("dependent")])
    ck("no DOMINANT_CONTRIBUTOR", "DOMINANT_CONTRIBUTOR" not in flags51, [x["symbol"] for x in s.get("leave_one_out", []) if x.get("dominant")])
    for reg in (RG.TREND_DOWN, RG.HIGH_VOL):
        v, b = _g(s, "regimes", "rows", reg, "excess_vs_ew"), _g(base, "regimes", "rows", reg, "excess_vs_ew")
        ck(f"{reg} excess vs EW >= baseline − {TOL['criteria_regime']}", (v is None and b is None) or (v is not None and b is not None and v >= b - TOL["criteria_regime"]), {"variant": v, "baseline": b})
    ch, chb = _g(s, "attribution", "holdings", "churn_mean"), _g(base, "attribution", "holdings", "churn_mean")
    ck(f"churn <= baseline + {TOL['criteria_churn']}", (ch is None and chb is None) or (ch is not None and chb is not None and ch <= chb + TOL["criteria_churn"]), {"variant": ch, "baseline": chb})
    return {"version": CRITERIA_VERSION, "met": all(c["pass"] for c in checks), "passed": sum(1 for c in checks if c["pass"]), "n": len(checks), "checks": checks, "rule": CRITERIA_RULE}


def research_flags(v: dict, s: dict, base: dict, counterpart: Optional[dict], factor: Optional[dict], crit: dict) -> List[str]:
    out = []
    st, ew, spy = _g(s, "stitched", "strategy", default={}), _g(s, "stitched", "EW_REBALANCED", default={}), _g(s, "stitched", "SPY", default={})
    bst = _g(base, "stitched", "strategy", default={})

    def ge(a, b, tol=0.0):
        return a is not None and b is not None and a >= b + tol
    if ge(ew.get("total_return"), st.get("total_return")) and ge(ew.get("sharpe"), st.get("sharpe")):
        out.append("EW_STILL_DOMINANT")
    if ge(spy.get("total_return"), st.get("total_return")) and ge(spy.get("sharpe"), st.get("sharpe")):
        out.append("SPY_STILL_DOMINANT")
    t3, bt3 = _g(s, "attribution", "concentration", "top3"), _g(base, "attribution", "concentration", "top3")
    ts, bts = _g(s, "attribution", "sector_concentration", "top_sector_share"), _g(base, "attribution", "sector_concentration", "top_sector_share")
    if (t3 is not None and bt3 is not None and t3 <= bt3 - TOL["concentration_reduced"]) or (ts is not None and bts is not None and ts <= bts - TOL["concentration_reduced"]):
        out.append("CONCENTRATION_REDUCED")
    if ge(_g(s, "cost", "excess_vs_ew_at_20"), _g(base, "cost", "excess_vs_ew_at_20"), TOL["cost_improved"]):
        out.append("COST_ROBUSTNESS_IMPROVED")
    dd_imp = ge(st.get("max_drawdown"), bst.get("max_drawdown"), TOL["drawdown_improved"])
    if dd_imp:
        out.append("DRAWDOWN_IMPROVED")
        if st.get("cagr") is not None and bst.get("cagr") is not None and st["cagr"] <= bst["cagr"] - TOL["return_sacrificed"]:
            out.append("RETURN_SACRIFICED_FOR_RISK")
    r = v["research"]
    if r["ranking"] == V.SECTOR_NEUTRAL_RANK and ts is not None and bts is not None and ts <= bts - TOL["neutrality_share"] \
            and ge(_g(s, "per_window", "median_sharpe"), _g(base, "per_window", "median_sharpe"), -TOL["neutrality_sharpe"]):
        out.append("SECTOR_NEUTRALITY_HELPFUL")
    if r["max_sector_weight"] is not None and (_g(s, "construction", "cap_changed_selection", default=0) > 0 or _g(s, "construction", "cap_infeasible", default=0) > 0):
        out.append("SECTOR_CAP_BINDING")
    if r["regime_overlay"] != V.NO_OVERLAY and counterpart is not None:
        cst = _g(counterpart, "stitched", "strategy", default={})
        td_v, td_c = _g(s, "regimes", "rows", RG.TREND_DOWN, "excess_vs_ew"), _g(counterpart, "regimes", "rows", RG.TREND_DOWN, "excess_vs_ew")
        if ge(st.get("max_drawdown"), cst.get("max_drawdown"), TOL["overlay_dd"]) and ge(st.get("sharpe"), cst.get("sharpe"), -TOL["overlay_sharpe"]) and (td_v is None or td_c is None or td_v >= td_c - TOL["overlay_trend_down"]):
            out.append("REGIME_OVERLAY_HELPFUL")
        if st.get("cagr") is not None and cst.get("cagr") is not None and st["cagr"] <= cst["cagr"] - TOL["overdefensive_cagr"] and not ge(st.get("max_drawdown"), cst.get("max_drawdown"), TOL["overlay_dd"]):
            out.append("REGIME_OVERLAY_OVERDEFENSIVE")
    if factor and factor["verdict"] in ("FACTOR_HELPFUL", "FACTOR_HARMFUL"):
        out.append(factor["verdict"])
    if crit["met"]:
        out.append("IMPROVEMENT_CRITERIA_MET")
    return out


def selection_verdict(s: dict, base: dict) -> dict:
    ch, chb = _g(s, "attribution", "holdings", "churn_mean"), _g(base, "attribution", "holdings", "churn_mean")
    if ch is None or chb is None:
        verdict = "WARN"
    else:
        verdict = "PASS" if ch <= chb else ("WARN" if ch <= chb + TOL["criteria_churn"] else "FAIL")
    return {"check": "selection stability", "verdict": verdict, "value": {"churn": ch, "baseline_churn": chb, "basis": "holdings churn vs the baseline variant"}}
