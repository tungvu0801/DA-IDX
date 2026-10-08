"""
rotation_campaign/engine.py — the campaign orchestration (DESIGN_50 §1).

  candidate_evidence()  one candidate's hold-out evidence: the candidate replayed on EVERY TEST window through the Stage 4.9
                        Evaluator (Stage 4.8 replay), Stage 4.9 aggregation, cost sensitivity (0/0 vs 20/20 bps on the same
                        TEST windows), the Stage 4.9 parameter neighbourhood (fragile flag + fragility ratio), the rs_v1 score,
                        the full-range replay (information only) and regimes of the stitched OOS curve
  run_campaign()        resolve → candidates (Stage 4.9 grid) → bars → windows → evidence per candidate → ONE Stage 4.9 walk-forward
                        across the candidate set (selection stability) → gates → leaderboard → finalists → comparisons → model cards
                        → persist
The candidate set is fixed before any replay; TEST data never alters it. Nothing is activated or deployed.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from backtest import bars as B
from backtest.replay import BarSeries
from fit import current as FC
from fit import readonly as RO
from rotation import universe as U
from rotation.store import RotationStore, canonical_json, sha256_hex
from rotation_backtest import ENGINE_VERSION as BACKTEST_VERSION
from rotation_backtest import metrics as MX
from rotation_backtest import runner as BR
from rotation_backtest import simulator as SIM
from rotation_walkforward import ENGINE_VERSION as WALKFORWARD_VERSION
from rotation_walkforward import ROBUSTNESS_VERSION
from rotation_walkforward import engine as WF
from rotation_walkforward import grid as G
from rotation_walkforward import regimes as RG
from rotation_walkforward import robustness as RB
from rotation_walkforward import windows as W

from rotation_campaign import ENGINE_VERSION
from rotation_campaign import config as C
from rotation_campaign import leaderboard as LB
from rotation_campaign.store import ModelCampaignStore

HISTORY_CALENDAR_DAYS = BR.HISTORY_CALENDAR_DAYS
CONVENTIONS = {**RB.CONVENTIONS, "stage_4_8": MX.CONVENTIONS, "campaign_ranking": list(C.RANKING), "fragility": C.FRAGILITY_RULE,
               "finalists": f"top finalist_count eligible leaderboard rows, stored as '{C.FINALIST_LABEL}' research records; nothing is activated",
               "hold_out_evidence": "every candidate replayed from initial cash on every TEST window; TRAIN windows are used only by the selection-stability walk-forward",
               "steady_state_turnover": "mean_oos_turnover excludes each TEST window's first executed rebalance (the deployment from initial cash); the raw Stage 4.9 figure is kept as mean_oos_turnover_raw"}


def _equity_rows(res) -> List[dict]:
    return [{"session_date": e["session_date"], "equity": e["equity"], "benchmark_index": e["benchmark_index"], "cash_weight": e["cash_weight"], "n_positions": e["n_positions"]}
            for e in res.equity]


def _oos_for(ev: WF.Evaluator, cand: dict, windows: List[dict], defn: dict, cost: str, slip: str) -> Tuple[List[dict], List[dict], List[float]]:
    """(oos rows, all test rebalances, per-window steady-state turnover). Steady-state turnover excludes each TEST window's first
    executed rebalance — the deployment from initial cash, an artefact of replaying every window from cash — so the gate and the
    score measure how much the configuration trades once invested."""
    oos, rebs, steady = [], [], []
    for w in windows:
        res, m = ev.evaluate(cand, w["test_start"], w["test_end"], defn["rebalance_frequency"], defn["initial_cash"], cost, slip)
        oos.append({"window_index": w["window_index"], "config_hash": cand["config_hash"], "test_status": res.status,
                    "test_metrics": WF._slim_metrics(m) or {"failure_code": res.failure_code, "failure_detail": res.failure_detail},
                    "equity": _equity_rows(res), "result_hash": res.result_hash()})
        rebs.extend(res.rebalances)
        executed = [float(r["turnover"]) for r in res.rebalances if r.get("executed") and r.get("turnover") is not None]
        steady.append(sum(executed[1:]) / len(executed[1:]) if len(executed) > 1 else 0.0)
    return oos, rebs, steady


def candidate_evidence(ev: WF.Evaluator, cand: dict, windows: List[dict], defn: dict, spy: BarSeries) -> dict:
    cash = Decimal(defn["initial_cash"])
    sel = [{"config_hash": cand["config_hash"], "config": cand["config"]}] * len(windows)
    oos, rebs, steady = _oos_for(ev, cand, windows, defn, defn["transaction_cost_bps"], defn["slippage_bps"])
    agg = RB.aggregate(windows, sel, oos, cash, defn["overlapping_tests"])
    agg["mean_oos_turnover_raw"] = agg["mean_oos_turnover"]
    agg["mean_oos_turnover"] = (sum(steady) / len(steady)) if steady else None            # steady-state (ex initial deployment) — gate + score input
    cost_rows = []
    for c, s in defn["cost_pairs"]:
        o2, _, _ = _oos_for(ev, cand, windows, defn, c, s)
        a2 = RB.aggregate(windows, sel, o2, cash, defn["overlapping_tests"])
        st = a2.get("stitched_oos") or {}
        cost_rows.append({"transaction_cost_bps": c, "slippage_bps": s, "median_oos_cagr": a2["median_oos_cagr"], "median_oos_sharpe": a2["median_oos_sharpe"],
                          "stitched_cagr": st.get("cagr"), "stitched_total_return": st.get("total_return"), "positive_window_pct": a2["positive_window_pct"]})
    cs = RB.cost_sensitivity_value(cost_rows)
    param = WF.parameter_sensitivity(ev, {**defn, "base_config": cand["config"]})
    score = RB.robustness_score(agg, cost_rows)
    full_res, full_m = ev.evaluate(cand, defn["start_date"], defn["end_date"], defn["rebalance_frequency"], defn["initial_cash"], defn["transaction_cost_bps"], defn["slippage_bps"])
    regimes = RG.breakdown(spy, agg.get("stitched_equity"), rebs)
    return {"label": cand["label"], "config_hash": cand["config_hash"], "config": cand["config"], "oos": [{k: v for k, v in o.items() if k != "equity"} for o in oos],
            "aggregate": {k: v for k, v in agg.items() if k != "stitched_equity"}, "cost_rows": cost_rows, "cost_sensitivity": cs, "parameter": param,
            "fragility_ratio": LB.fragility_ratio(param), "robustness": score, "full_history": WF._slim_metrics(full_m) if full_res.status == "COMPLETED" else None,
            "regimes": regimes}


def _result_hash(rows: List[dict], fins: List[dict]) -> str:
    body = {"rows": [[r["rank"], r["config_hash"], r["eligible"], r["robustness_score"], r["median_oos_sharpe"], r["worst_oos_max_drawdown"], r["cost_sensitivity"], r["fragility_ratio"]]
                     for r in rows], "finalists": [[f["finalist_rank"], f["config_hash"]] for f in fins]}
    return sha256_hex(canonical_json(body))


def run_campaign(store: ModelCampaignStore, body: dict, *, now: Optional[datetime] = None, path: Optional[Path] = None, cache: Optional[FC.BarCache] = None,
                 fetch_fn=None, client=None, watchlist_fn=None, persist: bool = True) -> dict:
    t0 = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    path = Path(path) if path else store.path
    rot = RotationStore(path)
    base = rot.config(str(body.get("config_id") or ""))
    if base is None:
        raise C.CampaignConfigError("INVALID_CONFIG", "Unknown rotation configuration.", 404)
    if body.get("config_hash") and body["config_hash"] != base["config_hash"]:
        raise C.CampaignConfigError("CONFIG_HASH_MISMATCH", "The configuration hash does not match the stored version.", 409)
    uspec = body.get("universe") or {}
    universe = U.resolve_universe(uspec.get("source"), uspec.get("ref"), uspec.get("symbols"), path=path, watchlist_fn=watchlist_fn)
    max_cand = int(body.get("max_candidates") or C.DEFAULTS["max_candidates"])
    try:
        gen = G.generate(base["config"], body.get("candidates"), body.get("grid"), max(1, min(max_cand, C.LIMITS["max_candidates_cap"])))
    except G.GridError as exc:
        raise C.CampaignConfigError(exc.code, exc.message) from None
    canon = C.normalise(body, base, universe, gen["candidates"], body.get("grid"))
    last_complete = B.last_complete_session_date(now)
    end = date.fromisoformat(canon["end_date"])
    if end > last_complete:
        raise C.CampaignConfigError("INVALID_DATE_RANGE", f"end_date must be a completed session (on or before {last_complete.isoformat()}).")
    start = date.fromisoformat(canon["start_date"])
    symbols = sorted(set(universe.symbols) | {SIM.BENCHMARK})
    failure: Optional[Tuple[str, str]] = None
    series: Dict[str, BarSeries] = {}
    fetched = {"requests": 0}
    try:
        series, _prov, fetched = FC.load_bars(RO.ReadOnlyBacktestStore(path), symbols, start - timedelta(days=HISTORY_CALENDAR_DAYS), end, now,
                                              cache if cache is not None else FC.BAR_CACHE, fetch_fn=fetch_fn, client=client)
    except Exception as exc:  # noqa: BLE001 - market data unavailable: a persisted FAILED campaign, type name only
        failure = ("DATA_UNAVAILABLE", f"Market data unavailable ({type(exc).__name__}).")
    windows: List[dict] = []
    ev = WF.Evaluator(universe, series, now)
    evidence: Dict[str, dict] = {}
    rows: List[dict] = []
    fins: List[dict] = []
    cards: List[dict] = []
    comparison = stability = None
    wf_hash = None
    if failure is None:
        try:
            sessions = SIM.check_benchmark(series, universe.symbols, start, end)
            windows = W.build_windows(sessions, start, end, canon["train_months"], canon["test_months"], canon["step_months"], canon["min_train_sessions"], canon["min_test_sessions"])
        except SIM.SimulationFailure as exc:
            failure = (exc.code, exc.detail)
        except W.WindowError as exc:
            failure = (exc.code, exc.detail)
    if failure is None:
        spy = series[SIM.BENCHMARK]
        for cand in canon["candidates"]:
            evidence[cand["config_hash"]] = candidate_evidence(ev, cand, windows, canon, spy)
        wf_cands, wf_sel, wf_oos, _ = WF.run_windows(ev, canon, windows, now)                 # TRAIN-only selection across the whole set
        wf_agg = RB.aggregate(windows, wf_sel, wf_oos, Decimal(canon["initial_cash"]), canon["overlapping_tests"])
        counts: Dict[str, int] = {}
        for s in wf_sel:
            counts[s["config_hash"]] = counts.get(s["config_hash"], 0) + 1
        stability = {"selections": [{"window_index": s["window_index"], "config_hash": s["config_hash"], "label": s["label"], "train_metric_value": s["train_metric_value"]} for s in wf_sel],
                     "times_selected": counts, "n_distinct_selected": wf_agg["n_distinct_selected"], "n_selection_changes": wf_agg["n_selection_changes"],
                     "walkforward_oos": {k: v for k, v in wf_agg.items() if k not in ("stitched_equity",)}, "selection_metric": canon["selection_metric"]}
        wf_hash = sha256_hex(canonical_json([[s["window_index"], s["config_hash"]] for s in wf_sel]))
        rows = LB.build_rows(evidence, canon["gates"], counts)
        fins = LB.finalists(rows, canon["finalist_count"])
        comparison = LB.comparison(rows, fins, base["config_hash"], evidence)
        cards = [LB.model_card(next(r for r in rows if r["config_hash"] == f["config_hash"]), evidence[f["config_hash"]], canon, windows) for f in fins]
    hashes = BR.bar_hashes(series, end)
    runtime = time.perf_counter() - t0
    status = "FAILED" if failure else ("COMPLETED" if fins else "NO_FINALIST")
    run = {"campaign_hash": C.definition_hash(canon), "base_config_hash": base["config_hash"], "universe_hash": universe.universe_hash, "engine_version": ENGINE_VERSION,
           "walkforward_version": WALKFORWARD_VERSION, "backtest_version": BACKTEST_VERSION, "robustness_version": ROBUSTNESS_VERSION, "status": status,
           "failure_code": failure[0] if failure else None, "failure_detail": failure[1][:300] if failure else None, "run_at": now.isoformat(timespec="seconds"),
           "completed_at": now.isoformat(timespec="seconds"), "n_candidates": len(canon["candidates"]), "n_rejected": len(gen["rejected"]), "n_discarded": gen["n_discarded"],
           "n_windows": len(windows), "n_eligible": sum(1 for r in rows if r["eligible"]), "n_finalists": len(fins), "n_evaluations": ev.evaluations, "n_cache_hits": ev.hits,
           "runtime_s": f"{runtime:.3f}", "data_hash": sha256_hex(canonical_json(hashes)), "walkforward_result_hash": wf_hash,
           "result_hash": _result_hash(rows, fins) if failure is None else None, "market_data_requests": int(fetched.get("requests", 0)),
           "definition_json": canon, "universe_note": C.UNIVERSE_NOTE}
    out = {"definition": C.public(canon), "rejected": gen["rejected"], "run": run, "windows": windows, "leaderboard": rows, "finalists": fins, "cards": cards,
           "comparison": comparison, "stability": stability, "evidence": evidence, "conventions": CONVENTIONS}
    if persist:
        run["campaign_id"] = store.insert_campaign(run, [{"config_hash": h, "label": e["label"], "config": e["config"], "evidence": {k: v for k, v in e.items() if k not in ("config", "label")}}
                                                         for h, e in evidence.items()], rows, fins, cards, comparison, stability, CONVENTIONS)
    return out
