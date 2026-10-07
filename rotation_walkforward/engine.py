"""
rotation_walkforward/engine.py — the walk-forward orchestration (DESIGN_49 §1–§9).

  evaluate()        ONE Stage 4.8 replay (simulate + metrics) of a candidate on a date range; memoised per run by
                    (config_hash, start, end, frequency, cost, slippage) — the bars never change inside a run
  run_windows()     for each window: TRAIN replays of every candidate → TRAIN-only ranking → ONE frozen selection →
                    TEST replay of that selection only. Test metrics are produced AFTER the selection is frozen and are
                    never passed to the ranking.
  run_walkforward() resolve → candidates → windows → run_windows → aggregate OOS → cost matrix → parameter neighbourhood →
                    regimes → robustness score → persist atomically
Research only: no broker, no snapshot refresh, no model call, no configuration is activated anywhere.
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
from rotation_backtest import config as BC
from rotation_backtest import metrics as MX
from rotation_backtest import runner as BR
from rotation_backtest import simulator as SIM

from rotation_walkforward import ENGINE_VERSION, ROBUSTNESS_VERSION
from rotation_walkforward import config as C
from rotation_walkforward import grid as G
from rotation_walkforward import regimes as RG
from rotation_walkforward import robustness as RB
from rotation_walkforward import selection as SEL
from rotation_walkforward import windows as W
from rotation_walkforward.store import PortfolioWalkForwardStore

HISTORY_CALENDAR_DAYS = BR.HISTORY_CALENDAR_DAYS
FRAGILITY_DROP = 0.5


class Evaluator:
    """Memoised Stage 4.8 replays over one immutable bar set."""

    def __init__(self, universe: U.ResolvedUniverse, series: Dict[str, BarSeries], now: datetime):
        self.universe, self.series, self.now = universe, series, now
        self.cache: Dict[tuple, Tuple[SIM.SimulationResult, Optional[dict]]] = {}
        self.evaluations, self.hits = 0, 0

    def evaluate(self, cand: dict, start: str, end: str, frequency: str, initial_cash: str, cost_bps: str, slip_bps: str):
        key = (cand["config_hash"], start, end, frequency, initial_cash, cost_bps, slip_bps)
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        cfg_row = {"config_id": cand["config_hash"][:32], "config_hash": cand["config_hash"], "config": cand["config"]}
        defn = {"config_id": cfg_row["config_id"], "config_hash": cand["config_hash"], "start_date": start, "end_date": end, "rebalance_frequency": frequency,
                "initial_cash": initial_cash, "transaction_cost_bps": cost_bps, "slippage_bps": slip_bps, "benchmark": BC.BENCHMARK}
        res = SIM.simulate(cfg_row, self.universe, self.series, defn, now=self.now)
        metrics = MX.compute(defn, res.equity, res.trades, res.rebalances, res.completed_positions, res.total_costs) if res.status == "COMPLETED" else None
        self.evaluations += 1
        self.cache[key] = (res, metrics)
        return res, metrics


def _slim_metrics(m: Optional[dict]) -> Optional[dict]:
    if m is None:
        return None
    keep = ("n_sessions", "first_session", "last_session", "total_return", "cagr", "annualized_volatility", "sharpe", "sortino", "max_drawdown",
            "max_drawdown_sessions", "benchmark_total_return", "benchmark_cagr", "excess_return", "annualized_excess", "n_rebalances",
            "n_rebalances_executed", "n_trades", "mean_turnover", "total_transaction_costs", "win_rate", "n_completed_positions", "final_equity")
    return {k: m.get(k) for k in keep}


def run_windows(ev: Evaluator, defn: dict, windows: List[dict], now: datetime) -> Tuple[List[dict], List[dict], List[dict], List[dict]]:
    """→ (candidate rows per window, selections, oos results, test rebalances). TRAIN ranking sees TRAIN metrics only; the
    TEST replay of a window runs only after its selection is frozen."""
    cands_out, selections, oos, test_rebs = [], [], [], []
    freq, cash, cost, slip = defn["rebalance_frequency"], defn["initial_cash"], defn["transaction_cost_bps"], defn["slippage_bps"]
    dd_limit = float(defn["max_drawdown_limit"])
    for w in windows:
        trained = []
        for cand in defn["candidates"]:
            res, m = ev.evaluate(cand, w["train_start"], w["train_end"], freq, cash, cost, slip)
            trained.append({"label": cand["label"], "config_hash": cand["config_hash"], "config": cand["config"], "train_status": res.status,
                            "train_metrics": _slim_metrics(m) or {"failure_code": res.failure_code, "failure_detail": res.failure_detail}})
        ranked = SEL.rank(trained, defn["selection_metric"], dd_limit)
        chosen = ranked[0]
        frozen_at = now.isoformat(timespec="seconds")
        selections.append({"window_index": w["window_index"], "config_hash": chosen["config_hash"], "label": chosen["label"],
                           "selection_metric": defn["selection_metric"], "train_metric_value": chosen["train_metric_value"],
                           "tie_break": SEL.tie_break_record(chosen, defn["selection_metric"]), "config": chosen["config"], "frozen_at": frozen_at})
        for r in ranked:
            cands_out.append({"window_index": w["window_index"], "config_hash": r["config_hash"], "label": r["label"], "train_rank": r["train_rank"],
                              "train_status": r["train_status"], "train_metric_value": r["train_metric_value"], "train_metrics": r["train_metrics"]})
        # only now: the frozen configuration on unseen TEST data
        res, m = ev.evaluate({"config_hash": chosen["config_hash"], "config": chosen["config"]}, w["test_start"], w["test_end"], freq, cash, cost, slip)
        oos.append({"window_index": w["window_index"], "config_hash": chosen["config_hash"], "test_status": res.status,
                    "test_metrics": _slim_metrics(m) or {"failure_code": res.failure_code, "failure_detail": res.failure_detail},
                    "equity": [{"session_date": e["session_date"], "equity": e["equity"], "benchmark_index": e["benchmark_index"], "cash_weight": e["cash_weight"],
                                "n_positions": e["n_positions"]} for e in res.equity], "result_hash": res.result_hash()})
        test_rebs.extend(res.rebalances)
    return cands_out, selections, oos, test_rebs


def cost_matrix(ev: Evaluator, defn: dict, windows: List[dict], selections: List[dict]) -> List[dict]:
    """The frozen per-window selections replayed on their TEST windows under each cost pair (no re-selection)."""
    rows = []
    for cost, slip in defn["cost_matrix"]:
        oos = []
        for w, s in zip(windows, selections):
            res, m = ev.evaluate({"config_hash": s["config_hash"], "config": s["config"]}, w["test_start"], w["test_end"], defn["rebalance_frequency"],
                                 defn["initial_cash"], cost, slip)
            oos.append({"window_index": w["window_index"], "config_hash": s["config_hash"], "test_status": res.status, "test_metrics": _slim_metrics(m) or {},
                        "equity": [{"session_date": e["session_date"], "equity": e["equity"], "benchmark_index": e["benchmark_index"], "cash_weight": e["cash_weight"],
                                    "n_positions": e["n_positions"]} for e in res.equity]})
        agg = RB.aggregate(windows, selections, oos, Decimal(defn["initial_cash"]), defn["overlapping_tests"])
        st = agg.get("stitched_oos") or {}
        rows.append({"transaction_cost_bps": cost, "slippage_bps": slip, "median_oos_cagr": agg["median_oos_cagr"], "median_oos_sharpe": agg["median_oos_sharpe"],
                     "stitched_cagr": st.get("cagr"), "stitched_total_return": st.get("total_return"), "positive_window_pct": agg["positive_window_pct"]})
    first_nonpos = next((r for r in rows if r["stitched_total_return"] is not None and r["stitched_total_return"] <= 0), None)
    for r in rows:
        r["break_even_hint"] = (first_nonpos is not None and r is first_nonpos)
    return rows


def parameter_sensitivity(ev: Evaluator, defn: dict) -> dict:
    """Base vs one-at-a-time neighbours over the full run range (descriptive; nothing is rejected or deployed)."""
    base_row, rej = G.validate_candidate("base", defn["base_config"])
    start, end = defn["start_date"], defn["end_date"]
    freq, cash, cost, slip = defn["rebalance_frequency"], defn["initial_cash"], defn["transaction_cost_bps"], defn["slippage_bps"]
    _, bm = ev.evaluate(base_row, start, end, freq, cash, cost, slip)
    base_m = _slim_metrics(bm)
    rows = []
    for label, asg in G.neighbourhood(defn["base_config"]):
        try:
            cfg = G.apply(defn["base_config"], asg)
        except G.GridError as exc:
            rows.append({"label": label, "status": "INVALID", "reason": exc.message})
            continue
        row, rej = G.validate_candidate(label, cfg)
        if rej:
            rows.append({"label": label, "status": "INVALID", "reason": rej["reason"]})
            continue
        res, m = ev.evaluate(row, start, end, freq, cash, cost, slip)
        sm = _slim_metrics(m)
        rows.append({"label": label, "status": res.status, "config_hash": row["config_hash"], "metrics": sm,
                     "delta": ({k: (sm[k] - base_m[k]) if sm and base_m and sm.get(k) is not None and base_m.get(k) is not None else None
                                for k in ("sharpe", "cagr", "max_drawdown", "total_return")} if sm and base_m else None)})
    valid = [r for r in rows if r.get("status") == "COMPLETED" and r.get("metrics") and r["metrics"].get("sharpe") is not None]
    ordered = sorted([("base", (base_m or {}).get("sharpe"))] + [(r["label"], r["metrics"]["sharpe"]) for r in valid], key=lambda x: (-(x[1] if x[1] is not None else -1e9), x[0]))
    base_rank = next((i + 1 for i, (lab, _) in enumerate(ordered) if lab == "base"), None)
    for r in valid:
        r["rank"] = next(i + 1 for i, (lab, _) in enumerate(ordered) if lab == r["label"])
        r["rank_delta"] = r["rank"] - base_rank if base_rank else None
    base_sharpe = (base_m or {}).get("sharpe")
    deteriorations = [max(0.0, base_sharpe - r["metrics"]["sharpe"]) for r in valid] if base_sharpe is not None else []
    med_det = (sorted(deteriorations)[len(deteriorations) // 2] if deteriorations else None)
    med_neighbor = (sorted(r["metrics"]["sharpe"] for r in valid)[len(valid) // 2] if valid else None)
    fragile = bool(base_sharpe is not None and med_det is not None and base_sharpe > 0 and (med_det > FRAGILITY_DROP * base_sharpe or (med_neighbor is not None and med_neighbor <= 0)))
    return {"method": "one dimension at a time around the base configuration (portfolio_size ±1, exit_rank ±1, cash_buffer ±0.05, max_turnover ±0.10, each "
                      "weight ±0.05 with proportional rescaling), each replayed over the full run range with the run's costs; fragile when the median Sharpe "
                      f"deterioration exceeds {int(FRAGILITY_DROP * 100)} % of the base Sharpe or the median neighbour Sharpe is <= 0; descriptive only",
            "base": {"config_hash": base_row["config_hash"], "metrics": base_m, "rank_among_neighbours": base_rank, "n_valid_neighbours": len(valid)},
            "neighbours": rows, "median_sharpe_deterioration": med_det, "median_neighbour_sharpe": med_neighbor, "fragile": fragile}


def bar_hashes(series: Dict[str, BarSeries], end: date) -> Dict[str, Optional[str]]:
    return BR.bar_hashes(series, end)


def _result_hash(selections: List[dict], oos: List[dict], agg: dict, score: dict) -> str:
    body = {"selections": [[s["window_index"], s["config_hash"], s["train_metric_value"]] for s in selections],
            "oos": [[o["window_index"], o["test_status"], o["result_hash"], (o["test_metrics"] or {}).get("total_return")] for o in oos],
            "aggregate": {k: agg[k] for k in ("median_oos_cagr", "median_oos_sharpe", "positive_window_pct", "benchmark_beating_pct", "worst_window_return",
                                              "n_distinct_selected")}, "score": score["score"]}
    return sha256_hex(canonical_json(body))


def run_walkforward(store: PortfolioWalkForwardStore, body: dict, *, now: Optional[datetime] = None, path: Optional[Path] = None,
                    cache: Optional[FC.BarCache] = None, fetch_fn=None, client=None, watchlist_fn=None, persist: bool = True) -> dict:
    """Validate → candidates → bars → windows → train / select / test → aggregate → sensitivity → regimes → score → persist."""
    t0 = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    path = Path(path) if path else store.path
    rot = RotationStore(path)
    base = rot.config(str(body.get("config_id") or ""))
    if base is None:
        raise C.WalkForwardConfigError("INVALID_CONFIG", "Unknown rotation configuration.", 404)
    if body.get("config_hash") and body["config_hash"] != base["config_hash"]:
        raise C.WalkForwardConfigError("CONFIG_HASH_MISMATCH", "The configuration hash does not match the stored version.", 409)
    uspec = body.get("universe") or {}
    universe = U.resolve_universe(uspec.get("source"), uspec.get("ref"), uspec.get("symbols"), path=path, watchlist_fn=watchlist_fn)
    max_cand = int(body.get("max_candidates") or C.DEFAULTS["max_candidates"])
    try:
        gen = G.generate(base["config"], body.get("candidates"), body.get("grid"), max(1, min(max_cand, C.LIMITS["max_candidates_cap"])))
    except G.GridError as exc:
        raise C.WalkForwardConfigError(exc.code, exc.message) from None
    canon = C.normalise(body, base, universe, gen["candidates"], body.get("grid"))
    last_complete = B.last_complete_session_date(now)
    end = date.fromisoformat(canon["end_date"])
    if end > last_complete:
        raise C.WalkForwardConfigError("INVALID_DATE_RANGE", f"end_date must be a completed session (on or before {last_complete.isoformat()}).")
    start = date.fromisoformat(canon["start_date"])
    symbols = sorted(set(universe.symbols) | {SIM.BENCHMARK})
    failure: Optional[Tuple[str, str]] = None
    series: Dict[str, BarSeries] = {}
    fetched = {"requests": 0}
    try:
        series, _prov, fetched = FC.load_bars(RO.ReadOnlyBacktestStore(path), symbols, start - timedelta(days=HISTORY_CALENDAR_DAYS), end, now,
                                              cache if cache is not None else FC.BAR_CACHE, fetch_fn=fetch_fn, client=client)
    except Exception as exc:  # noqa: BLE001 - market data unavailable: a persisted FAILED run, type name only
        failure = ("DATA_UNAVAILABLE", f"Market data unavailable ({type(exc).__name__}).")
    windows: List[dict] = []
    cands: List[dict] = []
    selections: List[dict] = []
    oos: List[dict] = []
    sens: List[dict] = []
    agg = score = param = regimes = None
    costs: List[dict] = []
    ev = Evaluator(universe, series, now)
    if failure is None:
        try:
            sessions = SIM.check_benchmark(series, universe.symbols, start, end)
            windows = W.build_windows(sessions, start, end, canon["train_months"], canon["test_months"], canon["step_months"], canon["min_train_sessions"],
                                      canon["min_test_sessions"])
        except SIM.SimulationFailure as exc:
            failure = (exc.code, exc.detail)
        except W.WindowError as exc:
            failure = (exc.code, exc.detail)
    if failure is None:
        cands, selections, oos, test_rebs = run_windows(ev, canon, windows, now)
        agg = RB.aggregate(windows, selections, oos, Decimal(canon["initial_cash"]), canon["overlapping_tests"])
        costs = cost_matrix(ev, canon, windows, selections)
        param = parameter_sensitivity(ev, canon)
        regimes = RG.breakdown(series[SIM.BENCHMARK], agg.get("stitched_equity"), test_rebs)
        score = RB.robustness_score(agg, costs)
        sens = [{"kind": "COST", "key": f"{r['transaction_cost_bps']}/{r['slippage_bps']}", "payload": r} for r in costs]
        sens += [{"kind": "PARAMETER", "key": "neighbourhood", "payload": param}]
        if regimes:
            sens += [{"kind": "REGIME", "key": "spy", "payload": regimes}]
    hashes = bar_hashes(series, end)
    runtime = time.perf_counter() - t0
    run = {"wf_config_hash": C.definition_hash(canon), "base_config_hash": base["config_hash"], "universe_hash": universe.universe_hash,
           "engine_version": ENGINE_VERSION, "backtest_version": BACKTEST_VERSION, "robustness_version": ROBUSTNESS_VERSION,
           "status": "FAILED" if failure else "COMPLETED", "failure_code": failure[0] if failure else None, "failure_detail": failure[1][:300] if failure else None,
           "run_at": now.isoformat(timespec="seconds"), "completed_at": now.isoformat(timespec="seconds"), "n_windows": len(windows),
           "n_candidates": len(canon["candidates"]), "n_rejected": len(gen["rejected"]), "n_discarded": gen["n_discarded"], "n_evaluations": ev.evaluations,
           "n_cache_hits": ev.hits, "overlapping_tests": canon["overlapping_tests"], "runtime_s": f"{runtime:.3f}",
           "data_hash": sha256_hex(canonical_json(hashes)), "bars_json": hashes,
           "result_hash": _result_hash(selections, oos, agg, score) if failure is None else None, "market_data_requests": int(fetched.get("requests", 0)),
           "universe_note": C.UNIVERSE_NOTE}
    metrics = None
    if failure is None:
        metrics = {k: v for k, v in agg.items() if k != "stitched_equity"}
        metrics["stitched_equity"] = agg["stitched_equity"]
    out = {"definition": C.public(canon), "rejected": gen["rejected"], "run": run, "windows": windows, "candidates": cands, "selections": selections,
           "oos": oos, "metrics": metrics, "robustness": score, "cost_sensitivity": costs, "parameter_sensitivity": param, "regimes": regimes,
           "conventions": {**RB.CONVENTIONS, "stage_4_8": MX.CONVENTIONS}}
    if persist:
        cfg_row = store.ensure_config(canon, now)
        run["wf_config_id"] = cfg_row["wf_config_id"]
        run["run_id"] = store.insert_run(run, windows, cands, selections, oos, sens, metrics, score, out["conventions"])
    return out
