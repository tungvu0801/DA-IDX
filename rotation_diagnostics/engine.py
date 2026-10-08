"""
rotation_diagnostics/engine.py — the diagnostics orchestration (DESIGN_51 §1–§8).

run_diagnostics(store, body) — body: {campaign_id, config_hashes? (default: the campaign base + its finalists), sector_map?, cost_points?}
  → load the immutable Stage 5.0 campaign (definition, finalists, stability) → rebuild its windows → one bar load
  → benchmarks (EW_REBALANCED, BUY_HOLD as Stage 4.8 replays; SPY from the benchmark index) at every cost point
  → per configuration: TEST replays, stitched curves, attribution, leave-one-out, leave-sector-out, cost grid, drawdown
    comparison, window table, regime-conditioned excess, flags, scorecard → persist.
Nothing is tuned, promoted, deployed or traded.
"""
from __future__ import annotations

import math
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
from rotation.store import canonical_json, sha256_hex
from rotation_backtest import metrics as MX
from rotation_backtest import runner as BR
from rotation_backtest import simulator as SIM
from rotation_campaign import config as CC
from rotation_campaign.store import ModelCampaignStore
from rotation_walkforward import engine as WF
from rotation_walkforward import regimes as RG
from rotation_walkforward import robustness as RB
from rotation_walkforward import windows as W

from rotation_diagnostics import ENGINE_VERSION, FLAGS_VERSION
from rotation_diagnostics import attribution as AT
from rotation_diagnostics import benchmarks as BM
from rotation_diagnostics import flags as FL
from rotation_diagnostics.store import RotationDiagnosticStore

COST_POINTS = (("0", "0"), ("5", "5"), ("10", "10"), ("20", "20"))
UNIVERSE_NOTE = CC.UNIVERSE_NOTE
DEFAULT_SECTORS = {"TECH/SOFTWARE": ["MSFT", "AAPL", "ORCL", "CRM", "ADBE", "INTU"], "SEMIS": ["NVDA", "AMD", "AVGO", "MU", "QCOM", "TXN", "INTC"],
                   "COMM/INTERNET": ["GOOGL", "META", "NFLX"], "CONSUMER": ["AMZN", "TSLA", "HD", "LOW", "NKE", "SBUX", "MCD", "COST"],
                   "HEALTHCARE": ["LLY", "JNJ", "MRK", "PFE", "UNH", "ABT"], "FINANCIALS": ["JPM", "BAC", "GS", "MS", "V", "MA"],
                   "INDUSTRIALS": ["CAT", "HON", "GE", "UPS", "DE"], "ENERGY": ["XOM", "CVX", "COP"]}
DEFAULT_SECTOR_MAP = {s: sec for sec, names in DEFAULT_SECTORS.items() for s in names}
CONVENTIONS = {"benchmarks": BM.CONVENTIONS, "attribution": "symbol P&L = sells − buys − costs + final quantity × last close per window; shares of total positive P&L; "
               "sector weights from proposal target weights; overlap |prev ∩ cur| / |prev|; churn (added + removed) / (2·N)",
               "ablation": "the same configuration replayed on the same TEST windows with the universe minus one symbol / sector, same costs, no re-selection",
               "cost": "0/0, 5/5, 10/10, 20/20 bps for the strategy and both universe benchmarks (SPY no cost); excess at the same cost point; no extrapolation",
               "drawdown": "max drawdown, drawdown duration (sessions to a new peak or the end) and worst calendar month on the stitched OOS curves",
               "regimes": "Stage 4.9 labels (SPY close vs SMA200; 20-session annualised vol vs 0.20); a day's return carries the previous close's label",
               "flags": FL.describe(), "stage_4_8": MX.CONVENTIONS, "stage_4_9": RB.CONVENTIONS}


class DiagnosticError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _q(v: Decimal) -> str:
    return format(Decimal(str(v)).quantize(Decimal("0.0001")), "f")


def _norm_costs(points) -> List[Tuple[str, str]]:
    out = []
    for c, s in (points or COST_POINTS):
        out.append((_q(Decimal(str(c))), _q(Decimal(str(s)))))
    return out


def _curve_metrics(rows: List[dict], initial: Decimal) -> dict:
    eq = [Decimal(str(r["equity"])) for r in rows]
    if not eq:
        return {"total_return": None, "cagr": None, "sharpe": None, "sortino": None, "max_drawdown": None, "max_drawdown_sessions": None, "worst_month": None, "n_sessions": 0}
    rets = MX.daily_returns(eq)
    months = MX.monthly_returns([r["session_date"] for r in rows], eq)
    worst = min(months, key=lambda m: m["return"] if m["return"] is not None else math.inf) if months else None
    return {"total_return": MX.total_return(eq, initial), "cagr": MX.cagr(eq, initial), "sharpe": MX.sharpe(rets), "sortino": MX.sortino(rets),
            "annualized_volatility": MX.annualized_volatility(rets), **MX.max_drawdown(eq), "worst_month": worst, "n_sessions": len(eq)}


def _stitched(ev: WF.Evaluator, cand: dict, windows: List[dict], defn: dict, cost: str, slip: str, initial: Decimal):
    """(oos rows with equity, results per window, stitched rows or None)."""
    oos, results = [], []
    for w in windows:
        res, m = ev.evaluate(cand, w["test_start"], w["test_end"], defn["rebalance_frequency"], defn["initial_cash"], cost, slip)
        results.append(res)
        oos.append({"window_index": w["window_index"], "config_hash": cand["config_hash"], "test_status": res.status, "test_metrics": WF._slim_metrics(m) or {"failure_code": res.failure_code},
                    "equity": [{"session_date": e["session_date"], "equity": e["equity"], "benchmark_index": e["benchmark_index"], "cash_weight": e["cash_weight"], "n_positions": e["n_positions"]} for e in res.equity]})
    sel = [{"config_hash": cand["config_hash"], "config": cand["config"]}] * len(windows)
    stitched = None if defn.get("overlapping_tests") else RB.stitch(windows, oos, initial)
    return oos, results, stitched


def _regime_rows(spy: BarSeries, strat: List[dict], bench: Dict[str, List[dict]], rebalances: List[dict]) -> dict:
    lab = RG.labels(spy)
    sessions = [r["session_date"] for r in strat]
    n = len(sessions)
    s_rets = MX.daily_returns([Decimal(str(r["equity"])) for r in strat])
    b_rets = {k: MX.daily_returns([Decimal(str(r["equity"])) for r in v]) if v else [] for k, v in bench.items()}
    rows: Dict[str, dict] = {}
    for i in range(1, n):
        prevlab = lab.get(sessions[i - 1]) or {}
        for axis in ("trend", "vol"):
            key = prevlab.get(axis, RG.UNKNOWN)
            g = rows.setdefault(key, {"axis": axis, "sessions": 0, "strategy": 1.0, "exposure": 0.0, "turnover": [], **{f"bench_{k}": 1.0 for k in bench}})
            g["sessions"] += 1
            g["strategy"] *= 1.0 + s_rets[i]
            g["exposure"] += 1.0 - float(strat[i]["cash_weight"])
            for k in bench:
                if i < len(b_rets[k]):
                    g[f"bench_{k}"] *= 1.0 + b_rets[k][i]
    for rb in rebalances:
        if rb.get("executed") and rb.get("turnover") is not None:
            for axis in ("trend", "vol"):
                key = (lab.get(rb["signal_session"]) or {}).get(axis, RG.UNKNOWN)
                if key in rows:
                    rows[key]["turnover"].append(float(rb["turnover"]))
    out = {}
    for key, g in rows.items():
        s_ret = g["strategy"] - 1.0
        out[key] = {"axis": g["axis"], "sessions": g["sessions"], "share": g["sessions"] / max(n - 1, 1), "strategy_return": s_ret,
                    **{f"{k}_return": g[f"bench_{k}"] - 1.0 for k in bench}, **{f"excess_vs_{k}": s_ret - (g[f"bench_{k}"] - 1.0) for k in bench},
                    "mean_exposure": g["exposure"] / g["sessions"], "mean_turnover": (sum(g["turnover"]) / len(g["turnover"])) if g["turnover"] else None}
    weak = [k for k, v in out.items() if v["sessions"] < FL.THRESHOLDS["regime_min_sessions"] or v["share"] < FL.THRESHOLDS["regime_min_share"]]
    for k in (RG.TREND_UP, RG.TREND_DOWN, RG.HIGH_VOL, RG.LOW_VOL):
        if k not in out:
            weak.append(k)
    return {"rows": out, "weak": sorted(set(weak))}


def run_diagnostics(store: RotationDiagnosticStore, body: dict, *, now: Optional[datetime] = None, path: Optional[Path] = None, cache: Optional[FC.BarCache] = None,
                    fetch_fn=None, client=None, persist: bool = True) -> dict:
    t0 = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    path = Path(path) if path else store.path
    cid = str(body.get("campaign_id") or "")
    camp = ModelCampaignStore(path).campaign(cid) if len(cid) == 32 else None
    if camp is None:
        raise DiagnosticError("INVALID_CAMPAIGN", "Unknown campaign.", 404)
    if camp["status"] == "FAILED":
        raise DiagnosticError("CAMPAIGN_FAILED", "The referenced campaign failed; nothing to diagnose.")
    defn = camp["definition"]
    cstore = ModelCampaignStore(path)
    finals = cstore.finalists(cid)
    stability = (cstore.metrics(cid) or {}).get("stability") or {}
    by_hash = {c["config_hash"]: c for c in defn["candidates"]}
    wanted = body.get("config_hashes") or [defn["base_config_hash"]] + [f["config_hash"] for f in finals]
    seen, configs = set(), []
    for h in wanted:
        if h in seen:
            continue
        seen.add(h)
        if h not in by_hash:
            raise DiagnosticError("UNKNOWN_CONFIG", f"config {h[:12]} is not a candidate of the campaign.")
        role = "baseline" if h == defn["base_config_hash"] else next((f"finalist_{f['finalist_rank']}" for f in finals if f["config_hash"] == h), "candidate")
        configs.append({**by_hash[h], "role": role})
    sector_map = {str(k).upper(): str(v) for k, v in (body.get("sector_map") or DEFAULT_SECTOR_MAP).items()}
    costs = _norm_costs(body.get("cost_points"))
    run_cost, run_slip = _q(Decimal(defn["transaction_cost_bps"])), _q(Decimal(defn["slippage_bps"]))
    if (run_cost, run_slip) not in costs:
        costs.append((run_cost, run_slip))
    universe = U.resolve_universe("CUSTOM", None, defn["universe_symbols"], path=path)
    if universe.universe_hash != defn["universe_hash"]:
        raise DiagnosticError("UNIVERSE_MISMATCH", "The campaign universe could not be rebuilt identically.")
    start, end = date.fromisoformat(defn["start_date"]), date.fromisoformat(defn["end_date"])
    initial = Decimal(defn["initial_cash"])
    definition = {"campaign_id": cid, "campaign_hash": camp["campaign_hash"], "config_hashes": [c["config_hash"] for c in configs], "roles": {c["config_hash"]: c["role"] for c in configs},
                  "sector_map": sector_map, "sector_map_hash": AT.sector_map_hash(sector_map), "cost_points": [list(c) for c in costs], "start_date": defn["start_date"],
                  "end_date": defn["end_date"], "train_months": defn["train_months"], "test_months": defn["test_months"], "step_months": defn["step_months"],
                  "rebalance_frequency": defn["rebalance_frequency"], "initial_cash": defn["initial_cash"], "run_costs": [run_cost, run_slip], "universe_hash": defn["universe_hash"],
                  "universe_symbols": list(universe.symbols), "benchmarks": {k: BM.CONVENTIONS[k] for k in BM.BENCHMARKS}, "engine_version": ENGINE_VERSION, "flags_version": FLAGS_VERSION,
                  "overlapping_tests": defn.get("overlapping_tests", False)}
    diag_hash = sha256_hex(canonical_json(definition))
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
    ev = WF.Evaluator(universe, series, now)
    evaluations_extra = 0
    bench_rows: List[dict] = []
    per_config: List[dict] = []
    bench_curves: Dict[Tuple[str, str, str], Optional[List[dict]]] = {}
    bench_results: Dict[Tuple[str, str, str], List] = {}
    if failure is None:
        spy = series[SIM.BENCHMARK]
        n = len(universe.symbols)
        bcands = {k: BM.benchmark_candidate(defn["base_config"], n, k) for k in (BM.EW_REBALANCED, BM.BUY_HOLD)}
        for cost, slip in costs:
            for k, bc in bcands.items():
                oos, results, st = _stitched(ev, bc, windows, defn, cost, slip, initial)
                bench_curves[(k, cost, slip)] = st
                bench_results[(k, cost, slip)] = results
                m = _curve_metrics(st or [], initial)
                bench_rows.append({"benchmark": k, "transaction_cost_bps": cost, "slippage_bps": slip, "definition": {k2: v for k2, v in bc.items() if k2 != "config"} | {"config": bc["config"]},
                                   "metrics": m, "windows": [{"window_index": o["window_index"], "status": o["test_status"], "total_return": o["test_metrics"].get("total_return"),
                                                              "max_drawdown": o["test_metrics"].get("max_drawdown"), "mean_turnover": o["test_metrics"].get("mean_turnover")} for o in oos]})
        spy_rows = BM.spy_rows(bench_curves[(BM.EW_REBALANCED, run_cost, run_slip)] or [], initial)          # SPY carries no cost: ONE row
        bench_rows.append({"benchmark": BM.SPY, "transaction_cost_bps": "0.0000", "slippage_bps": "0.0000", "definition": {"kind": BM.SPY, "convention": BM.CONVENTIONS[BM.SPY]},
                           "metrics": _curve_metrics(spy_rows, initial), "windows": [{"window_index": w["window_index"]} for w in windows]})
        bench_curves[(BM.SPY, "0.0000", "0.0000")] = spy_rows
        spy_metrics = next(b["metrics"] for b in bench_rows if b["benchmark"] == BM.SPY)
        spy_curve = bench_curves[(BM.SPY, "0.0000", "0.0000")]
        for cfg in configs:
            cand = {"config_hash": cfg["config_hash"], "config": cfg["config"], "label": cfg["label"]}
            oos, results, st = _stitched(ev, cand, windows, defn, run_cost, run_slip, initial)
            if st is None:
                per_config.append({"config_hash": cfg["config_hash"], "label": cfg["label"], "role": cfg["role"], "flags": ["NO_STITCHED_CURVE"], "scorecard": [],
                                   "summary": {"status": "INCOMPLETE", "detail": "a TEST replay failed or the tests overlap; no stitched curve"}})
                continue
            strat_metrics = _curve_metrics(st, initial)
            ew_curve, bh_curve = bench_curves[(BM.EW_REBALANCED, run_cost, run_slip)], bench_curves[(BM.BUY_HOLD, run_cost, run_slip)]
            ew_metrics = next(b["metrics"] for b in bench_rows if b["benchmark"] == BM.EW_REBALANCED and b["transaction_cost_bps"] == run_cost)
            bh_metrics = next(b["metrics"] for b in bench_rows if b["benchmark"] == BM.BUY_HOLD and b["transaction_cost_bps"] == run_cost)
            # attribution
            attr = AT.attribute(results, series, sector_map, initial)
            attr["reconciled"] = AT.reconcile(attr, results, initial)
            # cost grid
            cost_rows = []
            for cost, slip in costs:
                _, _, stc = _stitched(ev, cand, windows, defn, cost, slip, initial)
                mc = _curve_metrics(stc or [], initial)
                ewc = next(b["metrics"] for b in bench_rows if b["benchmark"] == BM.EW_REBALANCED and b["transaction_cost_bps"] == cost)
                bhc = next(b["metrics"] for b in bench_rows if b["benchmark"] == BM.BUY_HOLD and b["transaction_cost_bps"] == cost)
                cost_rows.append({"transaction_cost_bps": cost, "slippage_bps": slip, "strategy": mc, "EW_REBALANCED": ewc, "BUY_HOLD": bhc,
                                  "excess_vs_spy": (mc["total_return"] - spy_metrics["total_return"]) if mc["total_return"] is not None and spy_metrics["total_return"] is not None else None,
                                  "excess_vs_ew": (mc["total_return"] - ewc["total_return"]) if mc["total_return"] is not None and ewc["total_return"] is not None else None,
                                  "excess_vs_bh": (mc["total_return"] - bhc["total_return"]) if mc["total_return"] is not None and bhc["total_return"] is not None else None})
            cost_rows.sort(key=lambda r: Decimal(r["transaction_cost_bps"]))
            first_ew = next((r["transaction_cost_bps"] for r in cost_rows if r["excess_vs_ew"] is not None and r["excess_vs_ew"] <= 0), None)
            first_spy = next((r["transaction_cost_bps"] for r in cost_rows if r["excess_vs_spy"] is not None and r["excess_vs_spy"] <= 0), None)
            at = lambda bps, key: next((r[key] for r in cost_rows if Decimal(r["transaction_cost_bps"]) == Decimal(bps)), None)  # noqa: E731
            cost = {"rows": cost_rows, "first_nonpositive_vs_ew": first_ew, "first_nonpositive_vs_spy": first_spy, "excess_vs_ew_at_10": at(10, "excess_vs_ew"),
                    "excess_vs_ew_at_20": at(20, "excess_vs_ew"), "excess_vs_spy_at_10": at(10, "excess_vs_spy"), "excess_vs_spy_at_20": at(20, "excess_vs_spy")}
            # leave-one-out / leave-sector-out (same windows, same costs, no re-selection)
            full = {"cagr": strat_metrics["cagr"], "sharpe": strat_metrics["sharpe"], "max_drawdown": strat_metrics["max_drawdown"],
                    "excess_return": (strat_metrics["total_return"] - spy_metrics["total_return"]) if strat_metrics["total_return"] is not None and spy_metrics["total_return"] is not None else None}

            def ablate(symbols_left: List[str]) -> Tuple[str, dict]:
                nonlocal evaluations_extra
                if not symbols_left:
                    return "EMPTY_UNIVERSE", {}
                uni2 = U.resolve_universe("CUSTOM", None, symbols_left, path=path)
                ev2 = WF.Evaluator(uni2, {s: series[s] for s in list(symbols_left) + [SIM.BENCHMARK] if s in series}, now)
                _, _, st2 = _stitched(ev2, cand, windows, defn, run_cost, run_slip, initial)
                evaluations_extra += ev2.evaluations
                if st2 is None:
                    return "FAILED", {}
                m2 = _curve_metrics(st2, initial)
                red = {"cagr": m2["cagr"], "sharpe": m2["sharpe"], "max_drawdown": m2["max_drawdown"],
                       "excess_return": (m2["total_return"] - spy_metrics["total_return"]) if m2["total_return"] is not None and spy_metrics["total_return"] is not None else None}
                deltas = {k: ((red[k] - full[k]) if red[k] is not None and full[k] is not None else None) for k in full}
                return "COMPLETED", {"reduced": red, "deltas": deltas, "flag": FL.ablation_flag(full, red)}
            loo = []
            for s in universe.symbols:
                status, r = ablate([x for x in universe.symbols if x != s])
                loo.append({"symbol": s, "status": status, "deltas": r.get("deltas", {}), "reduced": r.get("reduced", {}), "dominant": bool(r.get("flag"))})
            lso = []
            sectors_present = sorted({AT.sector_of(s, sector_map) for s in universe.symbols})
            for sec in sectors_present:
                status, r = ablate([x for x in universe.symbols if AT.sector_of(x, sector_map) != sec])
                lso.append({"sector": sec, "status": status, "deltas": r.get("deltas", {}), "reduced": r.get("reduced", {}), "dependent": bool(r.get("flag"))})
            # windows
            ew_oos = {o["window_index"]: o for o in [{"window_index": w["window_index"], "r": bench_results[(BM.EW_REBALANCED, run_cost, run_slip)][i]} for i, w in enumerate(windows)]}
            bh_oos = {w["window_index"]: bench_results[(BM.BUY_HOLD, run_cost, run_slip)][i] for i, w in enumerate(windows)}
            lab = RG.labels(spy)
            wrows = []
            for i, (w, o) in enumerate(zip(windows, oos)):
                tm = o["test_metrics"]
                ewr = ew_oos[w["window_index"]]["r"]
                bhr = bh_oos[w["window_index"]]
                ew_ret = float(ewr.equity[-1]["equity"] / initial - 1) if ewr.status == "COMPLETED" and ewr.equity else None
                bh_ret = float(bhr.equity[-1]["equity"] / initial - 1) if bhr.status == "COMPLETED" and bhr.equity else None
                sess = [e["session_date"] for e in o["equity"]]
                trend_up = sum(1 for s in sess if (lab.get(s) or {}).get("trend") == RG.TREND_UP)
                high_vol = sum(1 for s in sess if (lab.get(s) or {}).get("vol") == RG.HIGH_VOL)
                top = attr["windows"][i].get("top") if i < len(attr["windows"]) else None
                r_ = tm.get("total_return")
                wrows.append({"window_index": w["window_index"], "test_first_session": w["test_first_session"], "test_last_session": w["test_last_session"], "status": o["test_status"],
                              "strategy_return": r_, "spy_return": tm.get("benchmark_total_return"), "ew_return": ew_ret, "bh_return": bh_ret,
                              "excess_vs_spy": (r_ - tm["benchmark_total_return"]) if r_ is not None and tm.get("benchmark_total_return") is not None else None,
                              "excess_vs_ew": (r_ - ew_ret) if r_ is not None and ew_ret is not None else None, "excess_vs_bh": (r_ - bh_ret) if r_ is not None and bh_ret is not None else None,
                              "strategy_max_drawdown": tm.get("max_drawdown"), "mean_turnover": tm.get("mean_turnover"), "dominant_contributors": top,
                              "regime_mix": {"trend_up_share": trend_up / len(sess) if sess else None, "high_vol_share": high_vol / len(sess) if sess else None}})
            done = [r for r in wrows if r["status"] == "COMPLETED"]

            def pct(key):
                xs = [r[key] for r in done if r[key] is not None]
                return (sum(1 for x in xs if x > 0) / len(xs)) if xs else None

            def med(key):
                xs = sorted(r[key] for r in done if r[key] is not None)
                return xs[len(xs) // 2] if xs else None
            wsum = {"pct_beating_SPY": pct("excess_vs_spy"), "pct_beating_EW_REBALANCED": pct("excess_vs_ew"), "pct_beating_BUY_HOLD": pct("excess_vs_bh"),
                    "median_excess_vs_spy": med("excess_vs_spy"), "median_excess_vs_ew": med("excess_vs_ew"), "median_excess_vs_bh": med("excess_vs_bh"),
                    "worst_relative_window_vs_ew": min(((r["excess_vs_ew"], r["window_index"]) for r in done if r["excess_vs_ew"] is not None), default=(None, None))}
            # regimes
            all_rebs = [rb for res in results if res.status == "COMPLETED" for rb in res.rebalances]
            regimes = _regime_rows(spy, st, {"spy": spy_curve, "ew": ew_curve or [], "bh": bh_curve or []}, all_rebs)
            sel_ratio = (stability.get("n_distinct_selected") / len(windows)) if stability.get("n_distinct_selected") and windows else None
            summary = {"status": "COMPLETED", "stitched": {"strategy": strat_metrics, "EW_REBALANCED": ew_metrics, "BUY_HOLD": bh_metrics, "SPY": spy_metrics},
                       "attribution": attr, "cost": cost, "leave_one_out": loo, "leave_sector_out": lso, "windows": wrows, "windows_summary": wsum, "regimes": regimes,
                       "selection": {"ratio": sel_ratio, "n_distinct_selected": stability.get("n_distinct_selected"), "n_windows": len(windows),
                                     "times_selected": (stability.get("times_selected") or {}).get(cfg["config_hash"], 0)},
                       "drawdown": {k: {"max_drawdown": m["max_drawdown"], "max_drawdown_sessions": m["max_drawdown_sessions"], "worst_month": m["worst_month"]}
                                    for k, m in (("strategy", strat_metrics), ("EW_REBALANCED", ew_metrics), ("BUY_HOLD", bh_metrics), ("SPY", spy_metrics))}}
            flags_ = FL.compute_flags(summary)
            per_config.append({"config_hash": cfg["config_hash"], "label": cfg["label"], "role": cfg["role"], "flags": flags_, "scorecard": FL.scorecard(summary, flags_), "summary": summary})
    hashes = BR.bar_hashes(series, end)
    result_hash = None if failure else sha256_hex(canonical_json([[p["config_hash"], p["flags"], [[r["check"], r["verdict"]] for r in p["scorecard"]],
                                                                   (p["summary"].get("stitched") or {}).get("strategy", {}).get("total_return")] for p in per_config]))
    run = {"diag_hash": diag_hash, "campaign_id": cid, "campaign_hash": camp["campaign_hash"], "universe_hash": defn["universe_hash"], "sector_map_hash": definition["sector_map_hash"],
           "engine_version": ENGINE_VERSION, "flags_version": FLAGS_VERSION, "status": "FAILED" if failure else "COMPLETED", "failure_code": failure[0] if failure else None,
           "failure_detail": failure[1][:300] if failure else None, "run_at": now.isoformat(timespec="seconds"), "completed_at": now.isoformat(timespec="seconds"),
           "start_date": defn["start_date"], "end_date": defn["end_date"], "n_windows": len(windows), "n_configs": len(configs), "n_evaluations": ev.evaluations + evaluations_extra,
           "n_cache_hits": ev.hits, "runtime_s": f"{time.perf_counter() - t0:.3f}", "data_hash": sha256_hex(canonical_json(hashes)), "result_hash": result_hash,
           "market_data_requests": int(fetched.get("requests", 0)), "definition_json": definition, "universe_note": UNIVERSE_NOTE}
    out = {"definition": definition, "run": run, "windows": windows, "benchmarks": bench_rows, "configs": per_config, "conventions": CONVENTIONS}
    if persist:
        run["diag_id"] = store.insert_run(run, bench_rows, per_config)
    return out
