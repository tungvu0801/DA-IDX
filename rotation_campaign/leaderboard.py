"""
rotation_campaign/leaderboard.py — eligibility gates, deterministic ranking, finalists, comparisons, model cards (DESIGN_50 §2–§4).
Pure functions over per-candidate evidence dicts produced by rotation_campaign.engine.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from rotation_campaign import FINALIST_LABEL
from rotation_campaign import config as C

INF = float("inf")


def fragility_ratio(param: Optional[dict]) -> Optional[float]:
    """max(0, Sharpe_base − min Sharpe_neighbour) / max(|Sharpe_base|, 1.0) over valid neighbours; None without evidence."""
    if not param or not param.get("base") or (param["base"].get("metrics") or {}).get("sharpe") is None:
        return None
    base = param["base"]["metrics"]["sharpe"]
    vals = [n["metrics"]["sharpe"] for n in param.get("neighbours", []) if n.get("status") == "COMPLETED" and (n.get("metrics") or {}).get("sharpe") is not None]
    if not vals:
        return 0.0
    return max(0.0, base - min(vals)) / max(abs(base), 1.0)


def evaluate_gates(ev: dict, gates: dict) -> List[dict]:
    """Every failed gate as {gate, value, limit}; an empty list means eligible."""
    agg = ev["aggregate"]
    out: List[dict] = []
    completed = agg.get("n_completed_tests", 0)
    failed = agg.get("n_windows", 0) - completed
    if completed < gates["min_windows"]:
        out.append({"gate": "min_windows", "value": completed, "limit": gates["min_windows"]})
    if failed > gates["max_failed_windows"]:
        out.append({"gate": "max_failed_windows", "value": failed, "limit": gates["max_failed_windows"]})
    dd = agg.get("worst_oos_max_drawdown")
    if dd is None or dd < float(gates["max_drawdown_floor"]):
        out.append({"gate": "max_drawdown_floor", "value": dd, "limit": float(gates["max_drawdown_floor"])})
    pos = agg.get("positive_window_pct")
    if pos is None or pos < float(gates["min_positive_window_pct"]):
        out.append({"gate": "min_positive_window_pct", "value": pos, "limit": float(gates["min_positive_window_pct"])})
    beat = agg.get("benchmark_beating_pct")
    if beat is None or beat < float(gates["min_benchmark_beating_pct"]):
        out.append({"gate": "min_benchmark_beating_pct", "value": beat, "limit": float(gates["min_benchmark_beating_pct"])})
    to = agg.get("mean_oos_turnover")
    if to is not None and to > float(gates["max_turnover"]):
        out.append({"gate": "max_turnover", "value": to, "limit": float(gates["max_turnover"])})
    frag = ev.get("fragility_ratio")
    if (frag is not None and frag > float(gates["max_fragility"])) or (ev.get("parameter") or {}).get("fragile"):
        out.append({"gate": "max_fragility", "value": frag, "limit": float(gates["max_fragility"]), "fragile_flag": bool((ev.get("parameter") or {}).get("fragile"))})
    cs = ev.get("cost_sensitivity")
    if cs is None or cs > float(gates["max_cost_sensitivity"]):
        out.append({"gate": "max_cost_sensitivity", "value": cs, "limit": float(gates["max_cost_sensitivity"])})
    return out


def _key(row: dict):
    s = row.get("robustness_score")
    sh = row.get("median_oos_sharpe")
    dd = row.get("worst_oos_max_drawdown")
    cs = row.get("cost_sensitivity")
    fr = row.get("fragility_ratio")
    to = row.get("mean_oos_turnover")
    return (0 if row["eligible"] else 1, -(s if s is not None else -INF), -(sh if sh is not None else -INF), -(dd if dd is not None else -INF),
            cs if cs is not None else INF, fr if fr is not None else INF, to if to is not None else INF, row["config_hash"])


def summary_of(config: dict) -> dict:
    w = config["weights"]
    return {"portfolio_size": config["portfolio_size"], "exit_rank": config["exit_rank"], "cash_buffer_pct": config["cash_buffer_pct"],
            "max_turnover_per_rotation": config["max_turnover_per_rotation"], "rebalance_threshold": config["rebalance_threshold"],
            "weights": {k: w[k] for k in sorted(w)}, "excluded_symbols": list(config.get("excluded_symbols") or [])}


def build_rows(evidence: Dict[str, dict], gates: dict, selected_counts: Dict[str, int]) -> List[dict]:
    rows = []
    for h, ev in evidence.items():
        agg = ev["aggregate"]
        exclusions = evaluate_gates(ev, gates)
        rows.append({"config_hash": h, "label": ev["label"], "summary": summary_of(ev["config"]), "eligible": not exclusions, "exclusions": exclusions,
                     "n_windows": agg.get("n_windows"), "n_completed_tests": agg.get("n_completed_tests"), "median_oos_cagr": agg.get("median_oos_cagr"),
                     "median_oos_sharpe": agg.get("median_oos_sharpe"), "median_oos_sortino": agg.get("median_oos_sortino"),
                     "worst_window_return": agg.get("worst_window_return"), "worst_oos_max_drawdown": agg.get("worst_oos_max_drawdown"),
                     "positive_window_pct": agg.get("positive_window_pct"), "benchmark_beating_pct": agg.get("benchmark_beating_pct"),
                     "median_oos_excess_return": agg.get("median_oos_excess_return"), "mean_oos_turnover": agg.get("mean_oos_turnover"),
                     "oos_return_std": agg.get("oos_return_std"), "cost_sensitivity": ev.get("cost_sensitivity"), "fragility_ratio": ev.get("fragility_ratio"),
                     "fragile_flag": bool((ev.get("parameter") or {}).get("fragile")), "times_selected": selected_counts.get(h, 0),
                     "robustness_score": (ev.get("robustness") or {}).get("score"), "full_history_cagr": (ev.get("full_history") or {}).get("cagr"),
                     "stitched_oos_cagr": (agg.get("stitched_oos") or {}).get("cagr"), "stitched_oos_total_return": (agg.get("stitched_oos") or {}).get("total_return")})
    rows.sort(key=_key)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def finalists(rows: List[dict], count: int) -> List[dict]:
    out = [r for r in rows if r["eligible"]][:count]
    return [{"finalist_rank": i + 1, "config_hash": r["config_hash"], "label": r["label"], "role": FINALIST_LABEL, "robustness_score": r["robustness_score"] or 0.0}
            for i, r in enumerate(out)]


def pareto_flags(rows: List[dict]) -> dict:
    def lead(key, best):
        xs = [r for r in rows if r.get(key) is not None]
        return best(xs, key=lambda r: (r[key], r["config_hash"]))["config_hash"] if xs else None
    stable = [r for r in rows if r.get("fragility_ratio") is not None]
    return {"return_leader": lead("median_oos_cagr", max), "drawdown_leader": lead("worst_oos_max_drawdown", max), "lowest_turnover": lead("mean_oos_turnover", min),
            "most_stable": min(stable, key=lambda r: (r["fragility_ratio"], r.get("oos_return_std") if r.get("oos_return_std") is not None else INF, r["config_hash"]))["config_hash"] if stable else None,
            "note": "descriptive leaders across all candidates (eligible or not); the leaderboard order is the only ranking"}


def comparison(rows: List[dict], fins: List[dict], base_hash: str, evidence: Dict[str, dict]) -> dict:
    by = {r["config_hash"]: r for r in rows}
    keys = ("median_oos_cagr", "median_oos_sharpe", "median_oos_sortino", "worst_oos_max_drawdown", "worst_window_return", "positive_window_pct",
            "benchmark_beating_pct", "median_oos_excess_return", "mean_oos_turnover", "cost_sensitivity", "fragility_ratio", "robustness_score", "oos_return_std")
    base = by.get(base_hash)
    fin_rows = [by[f["config_hash"]] for f in fins]

    def pick(r):
        return {k: r.get(k) for k in keys} | {"config_hash": r["config_hash"], "label": r["label"], "rank": r["rank"], "eligible": r["eligible"]}
    return {"baseline": pick(base) if base else None, "finalists": [pick(r) for r in fin_rows],
            "baseline_vs_finalists": [{"config_hash": r["config_hash"], "label": r["label"],
                                       "delta": {k: (r[k] - base[k]) if base and r.get(k) is not None and base.get(k) is not None else None for k in keys}} for r in fin_rows] if base else [],
            "oos_distribution": {r["config_hash"]: [o["test_metrics"].get("total_return") for o in evidence[r["config_hash"]]["oos"]] for r in fin_rows + ([base] if base and base not in fin_rows else [])},
            "cost_sensitivity": {r["config_hash"]: evidence[r["config_hash"]]["cost_rows"] for r in fin_rows},
            "parameter_sensitivity": {r["config_hash"]: {"fragile": evidence[r["config_hash"]]["parameter"]["fragile"], "fragility_ratio": r["fragility_ratio"],
                                                         "median_sharpe_deterioration": evidence[r["config_hash"]]["parameter"]["median_sharpe_deterioration"]} for r in fin_rows},
            "regimes": {r["config_hash"]: evidence[r["config_hash"]].get("regimes") for r in fin_rows}, "pareto": pareto_flags(rows), "universe_note": C.UNIVERSE_NOTE}


def weak_regimes(regimes: Optional[dict]) -> List[dict]:
    out = []
    for axis in ("trend", "vol"):
        groups = (regimes or {}).get(axis) or {}
        valid = [(k, v) for k, v in groups.items() if v.get("return") is not None]
        if valid:
            k, v = min(valid, key=lambda kv: kv[1]["return"])
            out.append({"axis": axis, "regime": k, "return": v["return"], "sessions": v["sessions"], "max_drawdown": v.get("max_drawdown")})
    return out


def model_card(row: dict, ev: dict, defn: dict, windows: List[dict]) -> dict:
    cfg = ev["config"]
    gates = defn["gates"]
    agg = ev["aggregate"]
    return {"role": FINALIST_LABEL, "config_hash": row["config_hash"], "label": row["label"], "leaderboard_rank": row["rank"],
            "factor_weights": {k: cfg["weights"][k] for k in sorted(cfg["weights"])},
            "portfolio_rules": {k: cfg[k] for k in ("portfolio_size", "exit_rank", "cash_buffer_pct", "rebalance_threshold", "max_turnover_per_rotation",
                                                   "max_position_weight", "min_position_weight", "min_price", "min_avg_dollar_volume", "min_history_sessions", "excluded_symbols")},
            "historical_interval": {"start": defn["start_date"], "end": defn["end_date"], "train_months": defn["train_months"], "test_months": defn["test_months"],
                                    "step_months": defn["step_months"], "rebalance_frequency": defn["rebalance_frequency"], "n_windows": len(windows),
                                    "test_windows": [[w["test_first_session"], w["test_last_session"]] for w in windows]},
            "universe": {"source": defn["universe_source"], "symbols": defn["universe_symbols"], "hash": defn["universe_hash"], "limitation": C.UNIVERSE_NOTE},
            "oos_metrics": {k: agg.get(k) for k in ("n_completed_tests", "median_oos_cagr", "median_oos_sharpe", "median_oos_sortino", "median_oos_excess_return",
                                                    "worst_window_return", "worst_oos_max_drawdown", "mean_oos_max_drawdown", "positive_window_pct",
                                                    "benchmark_beating_pct", "oos_return_std", "mean_oos_turnover", "stitched_oos")},
            "robustness": ev.get("robustness"), "weak_regimes": weak_regimes(ev.get("regimes")), "cost_sensitivity": {"value": ev.get("cost_sensitivity"), "rows": ev.get("cost_rows")},
            "fragility": {"ratio": ev.get("fragility_ratio"), "fragile_flag": bool((ev.get("parameter") or {}).get("fragile")), "rule": C.FRAGILITY_RULE},
            "known_limitations": [C.UNIVERSE_NOTE, "Historical simulation with the Stage 4.8 conventions (next-open fills, modelled slippage and cost, whole shares); "
                                  "not a forecast.", "Robustness score rs_v1 is a deterministic ranking aid, not a probability.",
                                  "No trading signal is implied; this card describes a configuration, not an action."],
            "why_it_qualified": [{"gate": g, "limit": gates[g], "observed": obs} for g, obs in (
                ("min_windows", agg.get("n_completed_tests")), ("max_drawdown_floor", agg.get("worst_oos_max_drawdown")), ("min_positive_window_pct", agg.get("positive_window_pct")),
                ("min_benchmark_beating_pct", agg.get("benchmark_beating_pct")), ("max_turnover", agg.get("mean_oos_turnover")), ("max_fragility", ev.get("fragility_ratio")),
                ("max_cost_sensitivity", ev.get("cost_sensitivity")))],
            "invalidation_during_paper_forward_test": [
                f"a paper-forward drawdown worse than {gates['max_drawdown_floor']}", f"fewer than {gates['min_positive_window_pct']} of forward windows positive",
                f"fewer than {gates['min_benchmark_beating_pct']} of forward windows beating {defn['benchmark']}", f"realised turnover per rebalance above {gates['max_turnover']}",
                "realised slippage or cost materially above the modelled bps", "any need to change parameters to keep the result (fragility)",
                "a universe change that invalidates the static-universe assumption"]}
