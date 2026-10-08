"""
signal_research/engine.py — ONE bounded research run on a stored Stage 5.0 campaign (DESIGN_52 §5–§9).

run_research(): campaign → its universe, interval, window geometry, costs and base configuration → ≤ 1 batched market-data
read → the fixed variant set (≤ 20) → Stage 5.1 benchmarks → every variant's evidence through the ResearchEvaluator → factor
verdicts, research flags, predeclared criteria, family comparisons → persist (append-only). Nothing is activated or deployed.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backtest.replay import BarSeries
from fit import current as FC
from fit import readonly as RO
from rotation import universe as U
from rotation.store import canonical_json, sha256_hex
from rotation_backtest import metrics as MX
from rotation_backtest import runner as BR
from rotation_backtest import simulator as SIM
from rotation_campaign.store import ModelCampaignStore
from rotation_diagnostics import benchmarks as BM
from rotation_diagnostics import engine as DE
from rotation_diagnostics import flags as FL
from rotation_diagnostics.store import RotationDiagnosticStore
from rotation_walkforward import regimes as RG
from rotation_walkforward import windows as W
from signal_research import CRITERIA_VERSION, ENGINE_VERSION, FACTOR_VERDICT_VERSION, FLAGS_VERSION, MAX_VARIANTS, RULES_VERSION
from signal_research import evaluate as EV
from signal_research import sector as SC
from signal_research import variant_engine as VE
from signal_research import variants as V
from signal_research.store import SignalResearchStore

COST_POINTS = DE.COST_POINTS
DEFAULT_SECTOR_MAP = DE.DEFAULT_SECTOR_MAP
UNIVERSE_NOTE = DE.UNIVERSE_NOTE
CONVENTIONS = {"reuse": "Stage 4.7 engine for factors / eligibility / scores; Stage 4.8 replay (one optional proposal hook); Stage 4.9 windows, stitching, regimes; "
                        "Stage 5.1 benchmarks, attribution, cost grid, dx_v1 flags and scorecard",
               "ablation": "remove-one: the factor weight set to 0 and the others rescaled proportionally (6 dp, ROUND_DOWN, residual to the largest); subsets: kept keys rescaled the same way",
               "sector_neutral": "within-sector percentile (n − position) / (n − 1) × 100 by the production order (one name = 50); global order percentile desc, composite desc, liquidity desc, ticker asc",
               "sector_cap": "floor(max_sector_weight / equal_weight) names per sector; retention and fill skip full sectors; no redistribution; SECTOR_CAP_INFEASIBLE when slots stay empty",
               "regime_overlay": "Stage 4.9 labels from SPY bars <= T; equal weights × schedule exposure (6 dp), remainder cash; UNKNOWN → 1.0; no leverage, no shorting",
               "variant_set": f"fixed, ordered, at most {MAX_VARIANTS}; no search; adaptive 'best ablation' combinations are not run",
               "factor_verdict": EV.FACTOR_RULE, "criteria": EV.CRITERIA_RULE, "bounded_ablation": EV.ABLATION_RULE,
               "selection_stability": "holdings churn vs the baseline variant (the Stage 5.0 TRAIN-selection notion is not re-run)",
               "benchmarks": BM.CONVENTIONS, "stage_5_1_flags": FL.describe(), "research_flags": EV.describe(), "stage_4_8": MX.CONVENTIONS}


class ResearchError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _q(v) -> str:
    return format(Decimal(str(v)).quantize(Decimal("0.0001")), "f")


def _norm_costs(points) -> List[Tuple[str, str]]:
    return [(_q(c), _q(s)) for c, s in (points or COST_POINTS)]


def _delta(a: dict, b: dict) -> dict:
    keys = (("cagr", ("stitched", "strategy", "cagr")), ("total_return", ("stitched", "strategy", "total_return")), ("sharpe", ("stitched", "strategy", "sharpe")),
            ("max_drawdown", ("stitched", "strategy", "max_drawdown")), ("median_sharpe", ("per_window", "median_sharpe")), ("median_excess_vs_ew", ("windows_summary", "median_excess_vs_ew")),
            ("pct_beating_ew", ("windows_summary", "pct_beating_EW_REBALANCED")), ("top3_share", ("attribution", "concentration", "top3")),
            ("top_sector_share", ("attribution", "sector_concentration", "top_sector_share")), ("churn", ("attribution", "holdings", "churn_mean")),
            ("excess_vs_ew_at_20", ("cost", "excess_vs_ew_at_20")), ("trend_down_excess_vs_ew", ("regimes", "rows", RG.TREND_DOWN, "excess_vs_ew")),
            ("high_vol_excess_vs_ew", ("regimes", "rows", RG.HIGH_VOL, "excess_vs_ew")))
    out = {}
    for name, path in keys:
        x, y = EV._g(a, *path), EV._g(b, *path)
        out[name] = (x - y) if x is not None and y is not None else None
    return out


def run_research(store: SignalResearchStore, body: dict, *, now: Optional[datetime] = None, path: Optional[Path] = None, cache: Optional[FC.BarCache] = None, fetch_fn=None, client=None,
                 persist: bool = True) -> dict:
    t0 = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    path = Path(path) if path else store.path
    cid = str(body.get("campaign_id") or "")
    camp = ModelCampaignStore(path).campaign(cid) if len(cid) == 32 else None
    if camp is None:
        raise ResearchError("INVALID_CAMPAIGN", "Unknown campaign.", 404)
    if camp["status"] == "FAILED":
        raise ResearchError("CAMPAIGN_FAILED", "The referenced campaign failed; nothing to research.")
    defn = camp["definition"]
    try:
        sector_map = SC.normalise_map(body.get("sector_map") or DEFAULT_SECTOR_MAP)
    except SC.SectorRuleError as exc:
        raise ResearchError(exc.code, exc.message)
    families = body.get("families")
    if families is not None and (not families or any(f not in V.FAMILIES for f in families)):
        raise ResearchError("INVALID_FAMILIES", f"families must be a non-empty subset of {list(V.FAMILIES)}")
    caps = tuple(body.get("caps") or V.SECTOR_CAPS)
    try:
        costs = _norm_costs(body.get("cost_points"))
    except Exception:  # noqa: BLE001
        raise ResearchError("INVALID_COST_POINTS", "cost points must be decimal pairs")
    run_cost, run_slip = _q(defn["transaction_cost_bps"]), _q(defn["slippage_bps"])
    if (run_cost, run_slip) not in costs:
        costs.append((run_cost, run_slip))
    try:
        variants = V.build_variants(defn["base_config"], sector_map, families, caps)
    except (V.VariantError, SC.SectorRuleError) as exc:
        raise ResearchError(exc.code, exc.message)
    try:
        universe = U.resolve_universe("CUSTOM", None, defn["universe_symbols"], path=path)
    except Exception as exc:  # noqa: BLE001
        raise ResearchError("INVALID_UNIVERSE", f"The campaign universe could not be rebuilt ({type(exc).__name__}).")
    if universe.universe_hash != defn["universe_hash"]:
        raise ResearchError("UNIVERSE_MISMATCH", "The campaign universe could not be rebuilt identically.")
    diag_ids = [r["diag_id"] for r in RotationDiagnosticStore(path).read("SELECT diag_id FROM rotation_diagnostic_runs WHERE campaign_id = ? ORDER BY run_at, rowid", (cid,))]
    start, end = date.fromisoformat(defn["start_date"]), date.fromisoformat(defn["end_date"])
    initial = Decimal(defn["initial_cash"])
    definition = {"campaign_id": cid, "campaign_hash": camp["campaign_hash"], "diagnostic_run_ids": diag_ids, "sector_map": sector_map, "sector_map_hash": SC.sector_map_hash(sector_map),
                  "families": sorted(set(families)) if families else list(V.FAMILIES), "caps": list(caps), "cost_points": [list(c) for c in costs], "run_costs": [run_cost, run_slip],
                  "start_date": defn["start_date"], "end_date": defn["end_date"], "train_months": defn["train_months"], "test_months": defn["test_months"], "step_months": defn["step_months"],
                  "rebalance_frequency": defn["rebalance_frequency"], "initial_cash": defn["initial_cash"], "universe_hash": defn["universe_hash"], "universe_symbols": list(universe.symbols),
                  "base_config": defn["base_config"], "base_config_hash": defn["base_config_hash"],
                  "variants": [{k: v[k] for k in ("label", "family", "config_hash", "research")} | {"weights": v["config"]["weights"]} for v in variants],
                  "benchmarks": {k: BM.CONVENTIONS[k] for k in BM.BENCHMARKS}, "engine_version": ENGINE_VERSION, "rules_version": RULES_VERSION, "flags_version": FLAGS_VERSION,
                  "criteria_version": CRITERIA_VERSION, "factor_verdict_version": FACTOR_VERDICT_VERSION, "overlapping_tests": defn.get("overlapping_tests", False)}
    run_hash = sha256_hex(canonical_json(definition))
    failure = None
    series: Dict[str, BarSeries] = {}
    fetched = {"requests": 0}
    try:
        series, _prov, fetched = FC.load_bars(RO.ReadOnlyBacktestStore(path), sorted(set(universe.symbols) | {SIM.BENCHMARK}), start - timedelta(days=BR.HISTORY_CALENDAR_DAYS), end, now,
                                              cache if cache is not None else FC.BAR_CACHE, fetch_fn=fetch_fn, client=client)
    except Exception as exc:  # noqa: BLE001
        failure = ("DATA_UNAVAILABLE", f"Market data unavailable ({type(exc).__name__}).")
    windows: List[dict] = []
    if failure is None:
        try:
            sessions = SIM.check_benchmark(series, universe.symbols, start, end)
            windows = W.build_windows(sessions, start, end, defn["train_months"], defn["test_months"], defn["step_months"], defn["min_train_sessions"], defn["min_test_sessions"])
        except SIM.SimulationFailure as exc:
            failure = (exc.code, exc.detail)
        except W.WindowError as exc:
            failure = (exc.code, exc.detail)
    ev = VE.ResearchEvaluator(universe, series, now, sector_map)
    extra = 0
    bench: dict = {"rows": []}
    rows: List[dict] = []
    factors: List[dict] = []
    sector_tests: List[dict] = []
    regime_tests: List[dict] = []
    combinations: List[dict] = []
    comparisons: List[dict] = []
    run_flags: List[str] = []
    if failure is None:
        wdefn = {**defn, "rebalance_frequency": defn["rebalance_frequency"], "initial_cash": defn["initial_cash"]}
        bench = EV.benchmarks(ev, defn["base_config"], len(universe.symbols), windows, wdefn, costs, run_cost, run_slip, initial)
        summaries: Dict[str, dict] = {}
        for pos, v in enumerate(variants):
            s, x = EV.variant_summary(ev, v, windows, wdefn, costs, run_cost, run_slip, initial, bench, series, sector_map, universe, path, now)
            extra += x
            summaries[v["config_hash"]] = s
        base_v = variants[0]
        base_s = summaries[base_v["config_hash"]]
        by_label = {v["label"]: v for v in variants}
        for pos, v in enumerate(variants):
            s = summaries[v["config_hash"]]
            if s.get("status") != "COMPLETED":
                rows.append({"config_hash": v["config_hash"], "label": v["label"], "family": v["family"], "position": pos, "config": v["config"], "research": v["research"], "flags": ["NO_STITCHED_CURVE"],
                             "research_flags": [], "scorecard": [], "criteria": {"version": CRITERIA_VERSION, "met": False, "passed": 0, "n": 0, "checks": [], "rule": EV.CRITERIA_RULE},
                             "oos": {}, "summary": s})
                continue
            flags51 = FL.compute_flags(s)
            card = FL.scorecard(s, flags51)
            card = [EV.selection_verdict(s, base_s) if r["check"] == "selection stability" else r for r in card]
            factor = None
            if v["family"] == "ablation":
                factor = EV.factor_verdict(s, base_s) if base_s.get("status") == "COMPLETED" else None
                if factor:
                    factors.append({"factor": v["label"][3:], "variant_hash": v["config_hash"], "label": v["label"], **factor})
            cp_label = V.counterpart_label(v)
            counterpart = summaries.get(by_label[cp_label]["config_hash"]) if cp_label and cp_label in by_label else None
            crit = EV.criteria(s, base_s, bench["ew_median_sharpe"], flags51)
            rflags = EV.research_flags(v, s, base_s, counterpart, factor, crit)
            rows.append({"config_hash": v["config_hash"], "label": v["label"], "family": v["family"], "position": pos, "config": v["config"], "research": v["research"], "flags": flags51,
                         "research_flags": rflags, "scorecard": card, "criteria": crit, "oos": s["per_window"], "summary": s})
            r = v["research"]
            if v["family"] in ("sector", "combined") and (r["ranking"] == V.SECTOR_NEUTRAL_RANK or r["max_sector_weight"] is not None):
                secs = s["attribution"]["sectors"]
                sector_tests.append({"config_hash": v["config_hash"], "label": v["label"], "ranking": r["ranking"], "max_sector_weight": r["max_sector_weight"],
                                     "top_sector": secs[0]["sector"] if secs else None, "top_sector_share": s["attribution"]["sector_concentration"]["top_sector_share"],
                                     "max_sector_weight_observed": s["attribution"]["sector_concentration"]["max_sector_weight"], "top3_share": s["attribution"]["concentration"]["top3"],
                                     "cap_changed_selection": s["construction"]["cap_changed_selection"], "cap_infeasible": s["construction"]["cap_infeasible"], "delta": _delta(s, base_s)})
            if r["regime_overlay"] != V.NO_OVERLAY:
                cp_hash = by_label[cp_label]["config_hash"] if cp_label in by_label else None
                regime_tests.append({"config_hash": v["config_hash"], "label": v["label"], "regime_overlay": r["regime_overlay"], "counterpart_hash": cp_hash, "counterpart_label": cp_label,
                                     "mean_exposure": s["construction"]["mean_exposure"], "exposure_by_regime": {k: x["mean_exposure"] for k, x in s["regimes"]["rows"].items()},
                                     "delta": _delta(s, counterpart) if counterpart else {}})
            if v["family"] == "combined":
                comps = [r["ranking"]] + ([f"SECTOR_CAP {r['max_sector_weight']}"] if r["max_sector_weight"] else []) + [r["regime_overlay"]]
                combinations.append({"config_hash": v["config_hash"], "label": v["label"], "components": comps, "criteria_met": crit["met"], "delta": _delta(s, base_s)})
            for row in s["cost"]["rows"]:
                for bname in (BM.EW_REBALANCED, BM.BUY_HOLD):
                    at_run = row["transaction_cost_bps"] == run_cost
                    comparisons.append({"config_hash": v["config_hash"], "benchmark": bname, "transaction_cost_bps": row["transaction_cost_bps"], "slippage_bps": row["slippage_bps"],
                                        "strategy_total_return": row["strategy"]["total_return"], "benchmark_total_return": row[bname]["total_return"],
                                        "excess": row["excess_vs_ew"] if bname == BM.EW_REBALANCED else row["excess_vs_bh"], "strategy_sharpe": row["strategy"]["sharpe"], "benchmark_sharpe": row[bname]["sharpe"],
                                        "pct_windows_beating": s["windows_summary"][f"pct_beating_{bname}"] if at_run else None,
                                        "median_excess": s["windows_summary"]["median_excess_vs_ew" if bname == BM.EW_REBALANCED else "median_excess_vs_bh"] if at_run else None})
            comparisons.append({"config_hash": v["config_hash"], "benchmark": BM.SPY, "transaction_cost_bps": run_cost, "slippage_bps": run_slip, "strategy_total_return": s["stitched"]["strategy"]["total_return"],
                                "benchmark_total_return": s["stitched"]["SPY"]["total_return"], "excess": s["cost"]["rows"] and next((x["excess_vs_spy"] for x in s["cost"]["rows"] if x["transaction_cost_bps"] == run_cost), None),
                                "strategy_sharpe": s["stitched"]["strategy"]["sharpe"], "benchmark_sharpe": s["stitched"]["SPY"]["sharpe"], "pct_windows_beating": s["windows_summary"]["pct_beating_SPY"],
                                "median_excess": s["windows_summary"]["median_excess_vs_spy"]})
        if not any(r["criteria"]["met"] for r in rows):
            run_flags.append("NO_SIGNAL_IMPROVEMENT")
    hashes = BR.bar_hashes(series, end)
    result_hash = None if failure else sha256_hex(canonical_json([[r["config_hash"], r["flags"], r["research_flags"], r["criteria"]["passed"], [[c["check"], c["verdict"]] for c in r["scorecard"]],
                                                                   (r["summary"].get("stitched") or {}).get("strategy", {}).get("total_return")] for r in rows] + [[f["factor"], f["verdict"]] for f in factors]))
    run = {"run_hash": run_hash, "campaign_id": cid, "campaign_hash": camp["campaign_hash"], "universe_hash": defn["universe_hash"], "sector_map_hash": definition["sector_map_hash"],
           "diagnostic_run_ids": diag_ids, "engine_version": ENGINE_VERSION, "rules_version": RULES_VERSION, "flags_version": FLAGS_VERSION, "criteria_version": CRITERIA_VERSION,
           "status": "FAILED" if failure else "COMPLETED", "failure_code": failure[0] if failure else None, "failure_detail": failure[1][:300] if failure else None,
           "run_at": now.isoformat(timespec="seconds"), "completed_at": now.isoformat(timespec="seconds"), "start_date": defn["start_date"], "end_date": defn["end_date"],
           "n_windows": len(windows), "n_variants": len(variants), "n_evaluations": ev.evaluations + extra, "n_cache_hits": ev.hits, "runtime_s": f"{time.perf_counter() - t0:.3f}",
           "data_hash": sha256_hex(canonical_json(hashes)), "result_hash": result_hash, "market_data_requests": int(fetched.get("requests", 0)), "definition_json": definition,
           "run_flags": run_flags, "benchmarks": [{k: b[k] for k in ("benchmark", "transaction_cost_bps", "slippage_bps", "metrics")} for b in bench["rows"]], "universe_note": UNIVERSE_NOTE}
    out = {"definition": definition, "run": run, "windows": windows, "benchmarks": bench["rows"], "variants": rows, "factors": factors, "sector_tests": sector_tests, "regime_tests": regime_tests,
           "combinations": combinations, "benchmark_comparisons": comparisons, "conventions": CONVENTIONS}
    if persist:
        run["run_id"] = store.insert_run(run, rows, factors, sector_tests, regime_tests, combinations, comparisons)
    return out
