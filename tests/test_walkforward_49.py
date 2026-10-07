"""Stage 4.9 — Walk-Forward + Robustness (rotation_walkforward/, database/portfolio_walkforward_migrations.py,
api/routes/portfolio_walkforward.py, frontend/portfolio_walkforward.js). Fully offline: synthetic piecewise-linear prices
whose train winners and test outcomes are predictable by hand, the Stage 4.8 replay and Stage 4.7 engine unchanged, a
scratch lab for the API; conftest blocks sockets and tripwires the Alpaca wires; every provider / model path is tripwired."""
import ast
import bisect
import math
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bt_fixtures as X
import fw_fixtures as FL
from backtest.replay import BarSeries
from fit import current as FC
from fit import readonly as RO
from rotation import handoff as H
from rotation import store as S
from rotation import universe as U
from rotation_walkforward import config as C
from rotation_walkforward import engine as WF
from rotation_walkforward import grid as G
from rotation_walkforward import regimes as RG
from rotation_walkforward import robustness as RB
from rotation_walkforward import selection as SEL
from rotation_walkforward import windows as W
from rotation_walkforward.store import PortfolioWalkForwardStore

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
Dt = date.fromisoformat
NOW = FL.at(Dt("2026-04-01"))
DAYS = X.sessions(date(2023, 1, 3), date(2026, 3, 31))
# daily price increments per calendar quarter (2023Q1 = 0 … 2026Q1 = 12): leadership rotates so different candidates win
# different TRAIN windows and the TEST outcomes are known in advance (see test_golden_walk_forward)
SLOPES = {"AAA": [0.05] * 6 + [0.05, 0.05, 0.05, 0.4, 0.4, 0.05, 0.02], "BBB": [0.05] * 6 + [0.4, 0.4, 0.4, -0.3, -0.3, -0.1, -0.3],
          "CCC": [0.05] * 6 + [0.05, 0.05, -0.3, 0.05, 0.7, -0.6, 0.05], "SPY": [0.1] * 13}
START = {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0, "SPY": 400.0}
BASE = {"weights": {"momentum": "0.30", "trend": "0.25", "relative_strength": "0.25", "volatility": "0.10", "drawdown": "0", "liquidity": "0.10"},
        "portfolio_size": 1, "exit_rank": 1, "cash_buffer_pct": "0.05", "rebalance_threshold": "0.01", "max_turnover_per_rotation": "1.00",
        "max_position_weight": "0.95", "min_position_weight": "0.10", "min_price": "5.00", "min_avg_dollar_volume": "1000.00", "min_history_sessions": 252,
        "excluded_symbols": ["BBB", "CCC"]}                    # the base holds AAA only; the explicit candidates hold BBB / CCC only
CANDS = [{"label": "only-BBB", **{k: v for k, v in BASE.items() if k != "excluded_symbols"}, "excluded_symbols": ["AAA", "CCC"]},
         {"label": "only-CCC", **{k: v for k, v in BASE.items() if k != "excluded_symbols"}, "excluded_symbols": ["AAA", "BBB"]}]
BODY = {"start_date": "2024-07-01", "end_date": "2026-03-31", "train_months": 6, "test_months": 3, "step_months": 3, "rebalance_frequency": "MONTHLY",
        "selection_metric": "CAGR", "candidates": CANDS, "max_candidates": 20, "initial_cash": "100000.00", "transaction_cost_bps": "5", "slippage_bps": "5"}
GOLDEN = {"selected": ["only-BBB", "only-BBB", "base", "only-CCC", "base"], "positive_window_pct": 0.6, "benchmark_beating_pct": 0.4,
          "n_distinct_selected": 3, "n_selection_changes": 3, "median_oos_cagr": 0.023078860934213186, "median_oos_sharpe": 13.846693092061257,
          "worst_window_return": -0.2297612, "stitched_total_return": -0.0807873, "score": 0.639075,
          "result_hash": "af77fe28b9beb06d04dd2af80e229d5695e7d9dfd51c6783981ce0a6f2fc2308"}


def quarter(d: date) -> int:
    return (d.year - 2023) * 4 + (d.month - 1) // 3


def prices(slopes=SLOPES, days=DAYS):
    out = {}
    for s, inc in slopes.items():
        px, series = START[s], {}
        for d in days:
            px = round(px + inc[quarter(d)], 4)
            series[d] = px
        out[s] = series
    return out


def market(slopes=SLOPES):
    m = FL.Market(tuple(slopes), start=date(2023, 1, 3), end=date(2026, 3, 31), vol=0.0)
    px = prices(slopes, m.days)
    for s in slopes:
        for d in m.days:
            c = px[s][d]
            o = round(c - 0.05, 4)
            m.set_bar(s, d, o, round(c + 0.1, 4), round(o - 0.1, 4), c, 1_000_000.0)
    m.prices = px
    return m


def series_from(m):
    return {s: BarSeries(s, [m.rows[s][d] for d in sorted(m.rows[s])]) for s in m.rows}


def growth(px, sym, start: str, end: str) -> float:
    """price(last session <= end) / price(first session AFTER the first session >= start) — the hand rule for a hold."""
    i0 = bisect.bisect_left(DAYS, Dt(start))
    i1 = bisect.bisect_right(DAYS, Dt(end)) - 1
    return px[sym][DAYS[i1]] / px[sym][DAYS[i0 + 1]] - 1


@pytest.fixture
def lab():
    m = market()
    lb = FL.FLab(m)
    lb.market = m
    return lb


def rotation_config(lab, **over):
    return S.RotationStore(Path(lab.path)).create_config("wf", {**BASE, **over}, NOW)


def run(lab, **over):
    cfg = over.pop("cfg", None) or rotation_config(lab)
    body = {"config_id": cfg["config_id"], "config_hash": cfg["config_hash"], "universe": {"source": "CUSTOM", "symbols": ["AAA", "BBB", "CCC"]}, **BODY, **over}
    return WF.run_walkforward(PortfolioWalkForwardStore(Path(lab.path)), body, now=NOW, path=Path(lab.path), cache=FC.BarCache(), fetch_fn=lab.market.fetch, client=object())


# ==================================================================================================================================
# GOLDEN walk-forward: selections predicted from the slope table, test outcomes known, aggregates and hash pinned
# ==================================================================================================================================

def test_golden_walk_forward(lab):
    out = run(lab)
    r = out["run"]
    assert r["status"] == "COMPLETED", (r["failure_code"], r["failure_detail"])
    assert r["n_windows"] == 5 and r["n_candidates"] == 3 and r["n_rejected"] == 0 and r["overlapping_tests"] is False
    assert [c["label"] for c in out["definition"]["candidates"]] == ["base", "only-BBB", "only-CCC"]
    px = lab.market.prices
    holder = {"base": "AAA", "only-BBB": "BBB", "only-CCC": "CCC"}
    for w, s, o in zip(out["windows"], out["selections"], out["oos"]):
        predicted = max(holder, key=lambda lab_: growth(px, holder[lab_], w["train_start"], w["train_end"]))   # highest TRAIN growth wins (CAGR rule)
        assert s["label"] == predicted and s["window_index"] == w["window_index"] and s["selection_metric"] == "CAGR"
        assert s["train_metric_value"] > 0 and Dt(s["frozen_at"][:10]) == NOW.date()
        held = holder[s["label"]]
        test_growth = growth(px, held, w["test_start"], w["test_end"])
        spy_growth = growth(px, "SPY", w["test_start"], w["test_end"])
        tm = o["test_metrics"]
        assert o["test_status"] == "COMPLETED" and o["config_hash"] == s["config_hash"]
        assert (tm["total_return"] > 0) == (test_growth > 0.002)                                              # sign known from the held symbol
        assert (tm["total_return"] > tm["benchmark_total_return"]) == (test_growth * 0.95 > spy_growth + 0.002)
        assert abs(tm["benchmark_total_return"] - spy_growth) < 0.004
    assert [s["label"] for s in out["selections"]] == GOLDEN["selected"]
    m = out["metrics"]
    assert m["positive_window_pct"] == GOLDEN["positive_window_pct"] and m["benchmark_beating_pct"] == GOLDEN["benchmark_beating_pct"]
    assert m["n_distinct_selected"] == GOLDEN["n_distinct_selected"] and m["n_selection_changes"] == GOLDEN["n_selection_changes"]
    assert m["median_oos_cagr"] == pytest.approx(GOLDEN["median_oos_cagr"]) and m["median_oos_sharpe"] == pytest.approx(GOLDEN["median_oos_sharpe"])
    assert m["worst_window_return"] == pytest.approx(GOLDEN["worst_window_return"]) and m["stitched_oos"]["total_return"] == pytest.approx(GOLDEN["stitched_total_return"])
    assert m["stitched_oos"]["n_sessions"] == sum(w["n_test_sessions"] for w in out["windows"]) == 312
    assert out["robustness"]["score"] == pytest.approx(GOLDEN["score"]) and out["robustness"]["version"] == "rs_v1"
    assert r["result_hash"] == GOLDEN["result_hash"]                                                          # pinned: fails if leakage or selection changes
    n_completed = sum(1 for n in out["parameter_sensitivity"]["neighbours"] if n["status"] == "COMPLETED")
    assert r["n_evaluations"] == 5 * 3 + 5 + 4 * 5 + 1 + n_completed and r["n_cache_hits"] == 5           # train 15 + test 5 + 4 extra cost pairs + base + replayed neighbours; 5/5 pair cached
    assert "survivorship" in r["universe_note"].lower()


# ==================================================================================================================================
# 1–7, 12–13: boundaries · no TEST data in TRAIN selection · frozen before TEST · non-overlap · determinism · hashes
# ==================================================================================================================================

def test_1_6_7_13_window_boundaries_non_overlap_and_window_hashes(lab):
    sessions = [d for d in DAYS]
    ws = W.build_windows(sessions, Dt("2024-07-01"), Dt("2026-03-31"), 6, 3, 3, 10, 5)
    assert [(w["train_start"], w["train_end"], w["test_start"], w["test_end"]) for w in ws][:2] == [
        ("2024-07-01", "2024-12-31", "2025-01-01", "2025-03-31"), ("2024-10-01", "2025-03-31", "2025-04-01", "2025-06-30")]
    assert len(ws) == 5 and ws[-1]["test_end"] == "2026-03-31"
    for w in ws:
        assert Dt(w["train_last_session"]) < Dt(w["test_first_session"]) and w["train_first_session"] >= w["train_start"] and w["test_last_session"] <= w["test_end"]
    assert not W.tests_overlap(ws) and [w["test_start"] for w in ws][1:] == [str(Dt(w["test_end"]) + __import__("datetime").timedelta(days=1)) for w in ws][:-1]
    assert W.build_windows(sessions, Dt("2024-07-01"), Dt("2026-03-31"), 6, 3, 3, 10, 5) == ws                  # deterministic
    assert len({w["window_hash"] for w in ws}) == 5 and all(len(w["window_hash"]) == 64 for w in ws)
    over = W.build_windows(sessions, Dt("2024-07-01"), Dt("2026-03-31"), 6, 6, 3, 10, 5)                      # step < test → overlapping tests
    assert W.tests_overlap(over) and len(over) == 4
    assert W.add_months(Dt("2024-01-31"), 1) == Dt("2024-02-29") and W.add_months(Dt("2024-11-30"), 3) == Dt("2025-02-28")
    with pytest.raises(W.WindowError) as e:
        W.build_windows(sessions, Dt("2026-01-01"), Dt("2026-03-31"), 6, 3, 3, 10, 5)
    assert e.value.code == "NO_WINDOWS"


def test_2_3_5_test_data_never_influences_train_selection(lab):
    out = run(lab)
    ser = series_from(lab.market)
    uni = U.resolve_universe("CUSTOM", None, ["AAA", "BBB", "CCC"])
    defn = out["definition"]
    for w, s in zip(out["windows"], out["selections"]):
        # (a) the same TRAIN ranking from a world that ends at the train boundary: no bar after train_end exists at all
        cut = {k: v for k, v in ((k, __import__("rotation_backtest.simulator", fromlist=["truncate"]).truncate(v, Dt(w["train_end"]))) for k, v in ser.items()) if v is not None}
        ev = WF.Evaluator(uni, cut, NOW)
        trained = []
        for cand in defn["candidates"]:
            res, m = ev.evaluate(cand, w["train_start"], w["train_end"], "MONTHLY", "100000.00", "5.0000", "5.0000")
            trained.append({"label": cand["label"], "config_hash": cand["config_hash"], "config": cand["config"], "train_status": res.status, "train_metrics": WF._slim_metrics(m)})
        assert SEL.rank(trained, "CAGR", -0.25)[0]["config_hash"] == s["config_hash"]
        # (b) the stored candidate rows of the window carry TRAIN metrics only (sessions inside the train window)
        rows = [c for c in out["candidates"] if c["window_index"] == w["window_index"]]
        assert all(c["train_metrics"]["first_session"] >= w["train_start"] and c["train_metrics"]["last_session"] <= w["train_end"] for c in rows)
        assert rows[0]["train_rank"] == 1 and rows[0]["config_hash"] == s["config_hash"] and s["tie_break"]["metric"] == "CAGR"
    # (c) mutating every bar after the LAST train boundary changes only the last TEST result, never a selection
    mutated = market()
    for sym in mutated.rows:
        for d in list(mutated.rows[sym]):
            if d > Dt("2025-12-31"):
                r = mutated.rows[sym][d]
                mutated.rows[sym][d] = (r[0], r[1], r[2] * 0.5, r[3] * 0.5, r[4] * 0.5, r[5] * 0.5, r[6])
    lab2 = FL.FLab(mutated)
    lab2.market = mutated
    alt = run(lab2)
    assert [s["config_hash"] for s in alt["selections"]] == [s["config_hash"] for s in out["selections"]]
    assert [o["test_metrics"]["total_return"] for o in alt["oos"][:4]] == [o["test_metrics"]["total_return"] for o in out["oos"][:4]]
    assert alt["oos"][4]["test_metrics"]["total_return"] != out["oos"][4]["test_metrics"]["total_return"]
    assert alt["run"]["result_hash"] != out["run"]["result_hash"]


def test_4_selection_tie_breakers_are_deterministic():
    mk = lambda h, sharpe, dd, to, cagr=0.1: {"label": h, "config_hash": h * 64, "config": {}, "train_status": "COMPLETED",   # noqa: E731
                                              "train_metrics": {"sharpe": sharpe, "max_drawdown": dd, "mean_turnover": to, "cagr": cagr}}
    rows = [mk("a", 1.0, -0.20, 0.5), mk("b", 1.0, -0.10, 0.5), mk("c", 1.0, -0.10, 0.2), mk("d", 1.0, -0.10, 0.2), mk("e", 2.0, -0.50, 0.9), mk("f", None, 0.0, 0.0)]
    ranked = SEL.rank(rows, "SHARPE", -0.25)
    assert [r["label"] for r in ranked] == ["e", "c", "d", "b", "a", "f"]                                  # metric, then drawdown, turnover, hash
    assert [r["train_rank"] for r in ranked] == [1, 2, 3, 4, 5, 6]
    assert [r["label"] for r in SEL.rank(rows, "DD_CONSTRAINED_SHARPE", -0.25)][0] == "c"                 # e breaches the drawdown limit → last of the defined
    assert SEL.rank(rows, "DD_CONSTRAINED_SHARPE", -0.25)[-2]["label"] == "e"
    assert [r["label"] for r in SEL.rank(rows, "CAGR", -0.25)][:1] == ["c"] or SEL.rank(rows, "CAGR", -0.25)[0]["train_metric_value"] == 0.1
    failed = [{**mk("g", 5.0, 0.0, 0.0), "train_status": "FAILED", "train_metrics": {"failure_code": "MISSING_OPEN"}}] + rows[:1]
    assert SEL.rank(failed, "SHARPE", -0.25)[0]["label"] == "a"                                           # a failed train replay never wins
    assert SEL.composite_train_score({"sharpe": 2.0, "max_drawdown": 0.0, "mean_turnover": 0.0}) == pytest.approx(1.0)
    assert SEL.composite_train_score({"sharpe": None}) is None and SEL.rank(rows, "COMPOSITE", -0.25)[0]["label"] in ("c", "d")
    with pytest.raises(ValueError):
        SEL.metric_value("MAGIC", {"sharpe": 1.0}, -0.25)


def test_12_identical_runs_produce_identical_hashes(lab):
    cfg = rotation_config(lab)
    a, b = run(lab, cfg=cfg), run(lab, cfg=cfg)
    assert a["run"]["result_hash"] == b["run"]["result_hash"] == GOLDEN["result_hash"]
    assert a["run"]["wf_config_hash"] == b["run"]["wf_config_hash"] and a["run"]["data_hash"] == b["run"]["data_hash"]
    assert [w["window_hash"] for w in a["windows"]] == [w["window_hash"] for w in b["windows"]]
    assert run(lab, cfg=cfg, slippage_bps="6")["run"]["wf_config_hash"] != a["run"]["wf_config_hash"]
    st = PortfolioWalkForwardStore(Path(lab.path))
    assert len(st.runs()) == 3 and len(st.read("SELECT * FROM portfolio_walkforward_configs")) == 2                # same definition → one config row


# ==================================================================================================================================
# 8–11, 37: candidate grid
# ==================================================================================================================================

def test_8_9_10_11_grid_generation_dedupe_rejection_and_cap():
    base = {k: v for k, v in BASE.items() if k != "excluded_symbols"}
    base["exit_rank"] = 3                                                                              # room for portfolio_size up to 3
    g = G.generate(base, None, {"dimensions": {"portfolio_size": [1, 2, 3], "cash_buffer_pct": ["0.05", "0.10"]}}, 20)
    labels = [c["label"] for c in g["candidates"]]
    assert labels[0] == "base" and "cash_buffer_pct=0.05 portfolio_size=1" not in labels                     # the base duplicate is removed
    assert labels == ["base", "cash_buffer_pct=0.05 portfolio_size=2", "cash_buffer_pct=0.05 portfolio_size=3", "cash_buffer_pct=0.10 portfolio_size=1",
                      "cash_buffer_pct=0.10 portfolio_size=2", "cash_buffer_pct=0.10 portfolio_size=3"]              # dimensions sorted, last one fastest
    assert g["rejected"] == [] and g["n_discarded"] == 0 and g["n_enumerated"] == 7
    assert G.generate(base, None, {"dimensions": {"portfolio_size": [1, 2, 3], "cash_buffer_pct": ["0.05", "0.10"]}}, 20)["candidates"] == g["candidates"]   # stable
    assert len({c["config_hash"] for c in g["candidates"]}) == len(g["candidates"])
    capped = G.generate(base, None, {"dimensions": {"portfolio_size": [1, 2, 3], "cash_buffer_pct": ["0.05", "0.10"]}}, 3)
    assert len(capped["candidates"]) == 3 and capped["n_discarded"] == 3 and [c["label"] for c in capped["candidates"]] == labels[:3]
    bad = G.generate(base, [{"label": "neg", **base, "cash_buffer_pct": "-0.1"}, {"label": "er", **base, "exit_rank": 0}, "nonsense"],
                     {"dimensions": {"exit_rank": [0, 1]}}, 20)
    assert [r["label"] for r in bad["rejected"]] == ["neg", "er", "explicit[2]", "exit_rank=0"] and all(r["reason"] for r in bad["rejected"])
    assert [c["label"] for c in bad["candidates"]] == ["base", "exit_rank=1"]                               # exit_rank=0 rejected with its reason, exit_rank=1 kept
    same = G.generate(base, [{"label": "dup", **base}], None, 20)
    assert [c["label"] for c in same["candidates"]] == ["base"] and same["rejected"] == []                   # an explicit duplicate of the base is removed
    w = G.rescale_weights(base["weights"], "momentum", "0.50")
    assert w["momentum"] == "0.500000" and sum(Decimal(v) for v in w.values()) == Decimal("1.000000")
    assert Decimal(w["trend"]) == pytest.approx(Decimal("0.178571"), abs=Decimal("0.000002")) and w["drawdown"] == "0.000000"
    gw = G.generate(base, None, {"dimensions": {"weights.momentum": ["0.30", "0.50", "1.00"]}}, 20)
    assert [c["label"] for c in gw["candidates"]] == ["base", "weights.momentum=0.50", "weights.momentum=1.00"]
    assert gw["candidates"][2]["config"]["weights"] == {"momentum": "1.000000", "trend": "0.000000", "relative_strength": "0.000000", "volatility": "0.000000", "drawdown": "0.000000", "liquidity": "0.000000"}
    for spec in ({"dimensions": {}}, {"dimensions": {"nope": [1]}}, {"dimensions": {"portfolio_size": list(range(1, 9))}}, {"dimensions": {"portfolio_size": "1"}}):
        with pytest.raises(G.GridError):
            G.generate(base, None, spec, 20)
    assert len(G.neighbourhood(base)) == 8 + 12 and G.label_for({}) == "base"


def test_37_38_39_40_malformed_config_and_insufficient_or_missing_data_fail_clearly(lab):
    cfg = rotation_config(lab)
    for over, code in (({"train_months": 0}, "OUT_OF_RANGE"), ({"selection_metric": "LUCK"}, "INVALID_SELECTION_METRIC"), ({"end_date": "2024-01-01"}, "INVALID_DATE_RANGE"),
                       ({"transaction_cost_bps": "x"}, "INVALID_DECIMAL"), ({"max_candidates": 0}, "OUT_OF_RANGE"), ({"rebalance_frequency": "DAILY"}, "INVALID_FREQUENCY"),
                       ({"end_date": "2026-04-03"}, "INVALID_DATE_RANGE"), ({"grid": {"dimensions": {"bogus": [1]}}}, "INVALID_GRID")):
        with pytest.raises(C.WalkForwardConfigError) as e:
            run(lab, cfg=cfg, **over)
        assert e.value.code == code, over
    rej = run(lab, cfg=cfg, candidates=[{"label": "bad", **BASE, "exit_rank": 0}])
    assert rej["run"]["status"] == "COMPLETED" and rej["rejected"] == [{"label": "bad", "code": "INVALID_EXIT_RANK", "reason": "exit_rank must be an integer >= portfolio_size"}]
    too_short = run(lab, cfg=cfg, min_train_sessions=500)
    assert too_short["run"]["status"] == "FAILED" and too_short["run"]["failure_code"] == "INSUFFICIENT_TRAIN_DATA" and "window 0" in too_short["run"]["failure_detail"]
    too_short_test = run(lab, cfg=cfg, min_test_sessions=200)
    assert too_short_test["run"]["failure_code"] == "INSUFFICIENT_TEST_DATA"
    none = run(lab, cfg=cfg, start_date="2026-01-01", end_date="2026-03-31")
    assert none["run"]["failure_code"] == "NO_WINDOWS"
    gap = market()
    gap.drop("SPY", Dt("2025-02-10"))
    lab2 = FL.FLab(gap)
    lab2.market = gap
    out = run(lab2)
    assert out["run"]["status"] == "FAILED" and out["run"]["failure_code"] == "MISSING_BENCHMARK" and out["metrics"] is None and out["run"]["result_hash"] is None
    assert PortfolioWalkForwardStore(Path(lab2.path)).run(out["run"]["run_id"])["status"] == "FAILED"


# ==================================================================================================================================
# 14–21: OOS metrics, medians, percentages, worst cases, stitching, no double counting
# ==================================================================================================================================

def _oos(tr, bench, dd, sharpe=1.0, cagr=0.1, to=0.3, equity=None, status="COMPLETED"):
    return {"test_status": status, "test_metrics": {"total_return": tr, "benchmark_total_return": bench, "max_drawdown": dd, "sharpe": sharpe, "sortino": sharpe,
                                                     "cagr": cagr, "excess_return": (tr - bench) if tr is not None else None, "mean_turnover": to,
                                                     "total_transaction_costs": "1.00"}, "equity": equity or []}


def _win(i, ts, te):
    return {"window_index": i, "test_start": ts, "test_end": te}


def test_14_15_16_17_18_19_aggregate_oos_metrics_known_cases():
    sel = [{"config_hash": "a" * 64, "config": {**BASE, "excluded_symbols": []}}, {"config_hash": "b" * 64, "config": {**BASE, "excluded_symbols": [], "portfolio_size": 2, "exit_rank": 2}},
           {"config_hash": "a" * 64, "config": {**BASE, "excluded_symbols": []}}]
    wins = [_win(0, "2025-01-01", "2025-03-31"), _win(1, "2025-04-01", "2025-06-30"), _win(2, "2025-07-01", "2025-09-30")]
    oos = [_oos(0.10, 0.05, -0.05, sharpe=1.0, cagr=0.40, to=0.2), _oos(-0.04, 0.02, -0.20, sharpe=-0.5, cagr=-0.15, to=0.4), _oos(0.02, 0.03, -0.10, sharpe=0.3, cagr=0.08, to=0.3)]
    agg = RB.aggregate(wins, sel, oos, D("100000"), overlapping=True)
    assert agg["median_oos_cagr"] == pytest.approx(0.08) and agg["median_oos_sharpe"] == pytest.approx(0.3) and agg["median_oos_excess_return"] == pytest.approx(-0.01)
    assert agg["worst_oos_max_drawdown"] == -0.20 and agg["mean_oos_max_drawdown"] == pytest.approx(-0.35 / 3)
    assert agg["positive_window_pct"] == pytest.approx(2 / 3) and agg["benchmark_beating_pct"] == pytest.approx(1 / 3)
    assert agg["worst_window_return"] == -0.04 and agg["oos_return_std"] == pytest.approx(__import__("statistics").stdev([0.10, -0.04, 0.02]))
    assert agg["mean_oos_turnover"] == pytest.approx(0.3) and agg["n_distinct_selected"] == 2 and agg["n_selection_changes"] == 2
    assert agg["parameter_drift"]["portfolio_size"] == {"changes": 2, "min": 1.0, "max": 2.0} and agg["stitched_oos"] is None and agg["stitched_equity"] is None
    assert agg["total_oos_costs"] == "3.00"
    partial = RB.aggregate(wins, sel, oos[:2] + [_oos(None, None, None, status="FAILED")], D("100000"), overlapping=False)
    assert partial["n_completed_tests"] == 2 and partial["positive_window_pct"] == 0.5 and partial["stitched_oos"] is None


def test_20_21_stitched_curve_only_for_non_overlapping_windows_without_double_counting():
    eq = lambda rows: [{"session_date": d, "equity": D(e), "benchmark_index": D(b), "cash_weight": D("0.05"), "n_positions": 1} for d, e, b in rows]   # noqa: E731
    w0 = eq([("2025-01-02", "100000.00", "100000.00"), ("2025-01-03", "110000.00", "101000.00")])
    w1 = eq([("2025-04-01", "100000.00", "100000.00"), ("2025-04-02", "90000.00", "99000.00")])
    wins = [_win(0, "2025-01-01", "2025-03-31"), _win(1, "2025-04-01", "2025-06-30")]
    oos = [_oos(0.10, 0.01, 0.0, equity=w0), _oos(-0.10, -0.01, -0.10, equity=w1)]
    st = RB.stitch(wins, oos, D("100000"))
    assert [(r["session_date"], str(r["equity"]), str(r["benchmark_index"]), r["window_index"]) for r in st] == [
        ("2025-01-02", "100000.00", "100000.00", 0), ("2025-01-03", "110000.00", "101000.00", 0), ("2025-04-01", "110000.00", "101000.00", 1), ("2025-04-02", "99000.00", "99990.00", 1)]
    assert len(st) == 4                                                                                      # every session exactly once
    agg = RB.aggregate(wins, [{"config_hash": "a" * 64, "config": {**BASE, "excluded_symbols": []}}] * 2, oos, D("100000"), overlapping=False)
    assert agg["stitched_oos"]["total_return"] == pytest.approx(-0.01) and agg["stitched_oos"]["n_sessions"] == 4
    overlapping = [_win(0, "2025-01-01", "2025-06-30"), _win(1, "2025-04-01", "2025-09-30")]
    w1o = eq([("2025-01-03", "100000.00", "100000.00"), ("2025-04-02", "90000.00", "99000.00")])          # starts inside the previous test window
    assert RB.stitch(overlapping, [_oos(0.10, 0.01, 0.0, equity=w0), _oos(-0.10, -0.01, -0.10, equity=w1o)], D("100000")) is None
    assert RB.aggregate(overlapping, [{"config_hash": "a" * 64, "config": {**BASE, "excluded_symbols": []}}] * 2, oos, D("100000"), overlapping=True)["stitched_oos"] is None
    assert RB.stitch(wins, [oos[0], _oos(None, None, None, status="FAILED")], D("100000")) is None


# ==================================================================================================================================
# 22–24: parameter and cost sensitivity known cases
# ==================================================================================================================================

def test_22_23_parameter_sensitivity_and_fragility_known_case(lab):
    out = run(lab)
    p = out["parameter_sensitivity"]
    labels = [n["label"] for n in p["neighbours"]]
    assert len(labels) == 20 and labels[0] == "portfolio_size-1" and "weights.momentum+0.05" in labels
    invalid = {n["label"]: n["reason"] for n in p["neighbours"] if n["status"] == "INVALID"}
    assert "portfolio_size-1" in invalid and "exit_rank-1" in invalid and "cash_buffer_pct-0.05" in invalid and "max_turnover+0.10" in invalid and "weights.drawdown-0.05" in invalid
    assert "portfolio_size+1" in invalid                                                                   # size 2 > exit_rank 1 is rejected by the Stage 4.7 rule, with its reason
    completed = [n for n in p["neighbours"] if n["status"] == "COMPLETED"]
    valid = [n for n in completed if n["metrics"]["sharpe"] is not None]
    assert len(completed) == 20 - len(invalid) and len(valid) == p["base"]["n_valid_neighbours"] and all(n["delta"] is not None and n["rank"] >= 1 for n in valid)
    assert any(n["metrics"]["sharpe"] is None for n in completed)                                           # e.g. a turnover cap that blocks every rebalance: flat equity, Sharpe undefined
    assert p["fragile"] is False and p["base"]["rank_among_neighbours"] == 1 and p["median_sharpe_deterioration"] == 0.0   # AAA-only base: weights cannot change the held name
    assert all(n["rank_delta"] >= 0 for n in valid) and "descriptive" in p["method"]
    # the fragility rule itself on a synthetic neighbourhood (hand case): median deterioration 1.2 of base 2.0 (> 50 %) → fragile
    fake = WF.Evaluator(U.resolve_universe("CUSTOM", None, ["AAA"]), {}, NOW)
    fake.evaluate = lambda cand, *a, **k: (type("R", (), {"status": "COMPLETED"})(), {"sharpe": 2.0 if cand["label"] == "base" else 0.8, "cagr": 0.1, "max_drawdown": -0.1, "total_return": 0.1})   # noqa: E731
    defn = {"base_config": {**BASE, "excluded_symbols": []}, "start_date": "2025-01-01", "end_date": "2025-12-31", "rebalance_frequency": "MONTHLY", "initial_cash": "100000.00",
            "transaction_cost_bps": "5.0000", "slippage_bps": "5.0000"}
    frag = WF.parameter_sensitivity(fake, defn)
    assert frag["fragile"] is True and frag["median_sharpe_deterioration"] == pytest.approx(1.2) and frag["base"]["rank_among_neighbours"] == 1


def test_24_cost_sensitivity_known_case(lab):
    out = run(lab)
    rows = out["cost_sensitivity"]
    assert [(r["transaction_cost_bps"], r["slippage_bps"]) for r in rows] == [("0.0000", "0.0000"), ("2.5000", "2.5000"), ("5.0000", "5.0000"), ("10.0000", "10.0000"), ("20.0000", "20.0000")]
    cagrs = [r["stitched_cagr"] for r in rows]
    assert cagrs == sorted(cagrs, reverse=True) and all(a > b for a, b in zip(cagrs, cagrs[1:]))             # costs only ever hurt: strictly decreasing
    assert rows[2]["stitched_cagr"] == pytest.approx(out["metrics"]["stitched_oos"]["cagr"])                 # the 5/5 row IS the main run (cached)
    assert rows[2]["median_oos_cagr"] == pytest.approx(out["metrics"]["median_oos_cagr"])
    assert sum(1 for r in rows if r["break_even_hint"]) <= 1
    cs = RB.cost_sensitivity_value(rows)
    assert cs == pytest.approx(max(0.0, min(1.0, (cagrs[0] - cagrs[4]) / max(abs(cagrs[0]), 0.01))))
    assert RB.cost_sensitivity_value([{"transaction_cost_bps": "0.0000", "slippage_bps": "0.0000", "stitched_cagr": 0.10},
                                      {"transaction_cost_bps": "20.0000", "slippage_bps": "20.0000", "stitched_cagr": 0.06}]) == pytest.approx(0.4)
    assert RB.cost_sensitivity_value(rows[:2]) is None
    score = RB.robustness_score({"n_windows": 4, "median_oos_sharpe": 1.0, "positive_window_pct": 0.75, "benchmark_beating_pct": 0.5, "worst_oos_max_drawdown": -0.25,
                                 "mean_oos_turnover": 0.3, "n_distinct_selected": 1},
                                [{"transaction_cost_bps": "0.0000", "slippage_bps": "0.0000", "stitched_cagr": 0.10}, {"transaction_cost_bps": "20.0000", "slippage_bps": "20.0000", "stitched_cagr": 0.06}])
    assert score["components"] == {"s_sharpe": 0.5, "s_pos": 0.75, "s_beat": 0.5, "s_dd": pytest.approx(0.5), "s_turn": pytest.approx(0.7), "s_stab": 1.0, "s_cost": pytest.approx(0.6)}
    assert score["score"] == pytest.approx(0.30 * 0.5 + 0.20 * 0.75 + 0.15 * 0.5 + 0.15 * 0.5 + 0.10 * 0.7 + 0.05 * 1.0 + 0.05 * 0.6) and "probability" in score["note"]


# ==================================================================================================================================
# 25–26: regimes
# ==================================================================================================================================

def test_25_26_regime_labels_use_no_future_data_and_group_metrics():
    days = DAYS[:400]
    closes = [100.0 + 0.1 * i for i in range(300)] + [130.0 - 0.5 * (i - 299) for i in range(300, 400)]      # rising, then a sharp decline
    spy = BarSeries("SPY", [(d.isoformat(), X.ts(d), c - 0.02, c + 0.1, c - 0.1, c, 1e6) for d, c in zip(days, closes)])
    lab = RG.labels(spy)
    assert lab[days[198].isoformat()]["trend"] == "UNKNOWN" and lab[days[199].isoformat()]["trend"] == "TREND_UP"
    assert lab[days[19].isoformat()]["vol"] == "UNKNOWN" and lab[days[20].isoformat()]["vol"] == "LOW_VOL"
    # no look-ahead: the label of a session is unchanged when every later bar is altered
    cut = BarSeries("SPY", [(d.isoformat(), X.ts(d), c - 0.02, c + 0.1, c - 0.1, c, 1e6) for d, c in list(zip(days, closes))[:320]])
    assert {k: v for k, v in RG.labels(cut).items()} == {k: v for k, v in lab.items() if k <= days[319].isoformat()}
    first_down = next(d for d in days if lab[d.isoformat()]["trend"] == "TREND_DOWN")
    i = days.index(first_down)
    assert closes[i] <= sum(closes[i - 199:i + 1]) / 200 and closes[i - 1] > sum(closes[i - 200:i]) / 200
    # grouping: a day's return carries the PREVIOUS close's label; Sharpe only with >= 20 sessions
    stitched = [{"session_date": d.isoformat(), "equity": D(str(100000 + 10 * k)), "benchmark_index": D("100000"), "cash_weight": D("0.05"), "n_positions": 1}
                for k, d in enumerate(days[290:340])]
    rebs = [{"executed": 1, "turnover": "0.5", "signal_session": days[295].isoformat()}, {"executed": 1, "turnover": "0.1", "signal_session": days[335].isoformat()},
            {"executed": 0, "turnover": None, "signal_session": days[300].isoformat()}]
    br = RG.breakdown(spy, stitched, rebs)
    groups = br["trend"]
    assert set(groups) <= {"TREND_UP", "TREND_DOWN"} and sum(g["sessions"] for g in groups.values()) == 49       # 50 rows → 49 returns
    expected_up = sum(1 for k in range(1, 50) if lab[days[290 + k - 1].isoformat()]["trend"] == "TREND_UP")
    assert groups["TREND_UP"]["sessions"] == expected_up and groups["TREND_UP"]["mean_exposure"] == pytest.approx(0.95)
    assert groups["TREND_UP"]["mean_turnover"] == 0.5 and groups["TREND_DOWN"]["mean_turnover"] == 0.1
    assert (groups["TREND_UP"]["sharpe"] is None) == (groups["TREND_UP"]["sessions"] < 20) and "descriptive" in br["method"]
    assert RG.breakdown(spy, None, []) is None


# ==================================================================================================================================
# 27–36: limitation · isolation · no deployment · no handoff · storage · API
# ==================================================================================================================================

def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_27_28_29_30_31_32_33_limitation_isolation_no_deploy_no_handoff():
    files = list((ROOT / "rotation_walkforward").glob("*.py")) + [ROOT / "api" / "routes" / "portfolio_walkforward.py", ROOT / "database" / "portfolio_walkforward_migrations.py"]
    banned = ("paper", "portfolio", "agents", "ai_explain", "anthropic", "alpaca", "robinhood", "rh_gateway", "requests", "httpx", "research_flow", "notifications",
              "forward.automation", "random", "sklearn", "torch", "numpy", "scipy")
    for f in files:
        hits = [m for m in _imports(f) if any(m == b or m.startswith(b + ".") for b in banned)]
        assert not hits, (f.name, hits)
    src = "".join(f.read_text(encoding="utf-8") for f in files) + (ROOT / "frontend" / "portfolio_walkforward.js").read_text(encoding="utf-8")
    for word in ("handoff", "Prepare Paper", "AlpacaOrders", "/api/alpaca", "data-apo-", "MutationObserver", "activate_config", "set_active", "deploy(", "promote("):
        assert word not in src, word
    assert "survivorship" in C.UNIVERSE_NOTE.lower() and C.UNIVERSE_NOTE == __import__("rotation_backtest.config", fromlist=["UNIVERSE_NOTE"]).UNIVERSE_NOTE
    design = (ROOT / "DESIGN_49_WALKFORWARD.md").read_text(encoding="utf-8").lower()
    assert "survivorship" in design and "train" in design and "not a probability" in design
    assert "not a probability" in RB.SCORE_FORMULA.lower() and RB.SCORE_FORMULA.startswith("rs_v1")
    # a walk-forward run row can never be an eligible Stage 4.7 handoff source
    out = H.evaluate({"status": "VALID", "portfolio_source": None, "run_id": "x" * 32}, {"symbol": "AAA", "action": "ADD", "handoff_allowed": 1, "est_qty_diff": 10})
    assert out["eligible"] is False and out["draft"] is None and out["reason"] == "INVALID_SOURCE"
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="pwf-body"' in html and 'src="portfolio_walkforward.js"' in html and 'href="portfolio_walkforward.css"' in html


def test_34_35_storage_append_only_and_fresh_db_migration(tmp_path):
    from database import portfolio_walkforward_migrations as M
    db = tmp_path / "wf49.db"
    with sqlite3.connect(str(db)) as c:
        c.execute("PRAGMA user_version = 9")
        for _ in range(3):
            M.run_portfolio_walkforward_migrations(c)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 9
        names = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(M.TABLES) <= names and len(M.TABLES) == 8 and not any("alpaca" in n or "paper_" in n for n in names)
        sql = " ".join(s for (s,) in c.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        assert "DROP" not in sql.upper() and "ALTER" not in sql.upper()
    st = PortfolioWalkForwardStore(db)
    assert st.exists() and st.runs() == [] and st.run("0" * 32) is None
    with sqlite3.connect(str(db)) as c:
        with pytest.raises(sqlite3.DatabaseError):                                                        # a window whose test precedes its train is impossible
            c.execute("INSERT INTO portfolio_walkforward_windows VALUES ('0'*32, 0, 'h', '2025-01-01', '2025-06-30', '2025-01-01', '2025-03-31', '2025-01-02', '2025-06-30', '2025-01-02', '2025-03-31', 10, 5)")


def test_34b_rows_are_immutable_after_a_real_run(lab):
    out = run(lab)
    rid = out["run"]["run_id"]
    st = PortfolioWalkForwardStore(Path(lab.path))
    assert st.run(rid)["status"] == "COMPLETED" and len(st.windows(rid)) == 5 and len(st.candidates(rid)) == 15 and len(st.selections(rid)) == 5
    assert len(st.oos(rid)) == 5 and st.oos(rid, with_equity=True)[0]["equity"][0]["equity"] == "100000.00"
    kinds = {r["kind"] for r in st.sensitivity(rid)}
    assert kinds == {"COST", "PARAMETER", "REGIME"} and len(st.sensitivity(rid, "COST")) == 5
    m = st.metrics(rid)
    assert m["robustness"]["score"] == pytest.approx(GOLDEN["score"]) and m["metrics"]["n_distinct_selected"] == 3 and "stitched" in m["conventions"]
    with sqlite3.connect(str(lab.path)) as c:
        for sql in ("UPDATE portfolio_walkforward_runs SET status = 'FAILED'", "DELETE FROM portfolio_walkforward_selections", "UPDATE portfolio_walkforward_oos_results SET test_status = 'X'",
                    "DELETE FROM portfolio_walkforward_metrics", "UPDATE portfolio_walkforward_candidates SET train_rank = 99", "DELETE FROM portfolio_walkforward_configs"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)


# ==================================================================================================================================
# API (scratch lab, fixed clock, synthetic bars, every broker / model path tripwired): 36 + the UI contract
# ==================================================================================================================================

API = "/api/rotation-walkforward"


@pytest.fixture
def api(monkeypatch, lab):
    import agents.technical_agent as ta
    from ai_explain import service as AX
    from api.routes import portfolio as pr
    from api.routes import portfolio_walkforward as PW
    from api.server import app
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr(PW, "NOW_FN", lambda: NOW)
    monkeypatch.setattr(PW, "FETCH", {"fetch_fn": lab.market.fetch, "client": object(), "cache": FC.BarCache()})
    hits = []

    def fail(what):
        def _f(*a, **k):
            hits.append(what)
            raise RuntimeError(what)
        return _f
    monkeypatch.setattr(ta, "get_provider", fail("claude research"))
    monkeypatch.setattr(AX, "get_provider", fail("claude explanation"))
    monkeypatch.setattr(pr, "provider_factory", fail("robinhood provider"))
    c = TestClient(app)
    c.hits, c.lab = hits, lab
    return c


def test_36_api_is_research_only_runs_persists_and_reads(api):
    from api.routes import portfolio_walkforward as PW
    paths = [(sorted(r.methods)[0], r.path) for r in PW.router.routes]
    assert all(not any(w in p.lower() for w in ("order", "trade", "backtest", "paper", "execute", "broker", "handoff", "deploy")) for _, p in paths)
    assert [p for m, p in paths if m == "POST"] == [f"{API}/run"]
    r = api.get(f"{API}/config")
    assert r.status_code == 200
    b = r.json()
    assert b["default_selection_metric"] == "SHARPE" and b["selection_metrics"] == ["SHARPE", "SORTINO", "CAGR", "DD_CONSTRAINED_SHARPE", "COMPOSITE"]
    assert "survivorship" in b["universe_note"].lower() and b["cost_matrix"][-1] == ["20", "20"] and "weights.momentum" in b["grid_dimensions"]
    cfg = rotation_config(api.lab)
    api.lab.market.calls.clear()
    body = {"config_id": cfg["config_id"], "config_hash": cfg["config_hash"], "universe": {"source": "CUSTOM", "symbols": ["AAA", "BBB", "CCC"]}, **BODY}
    r = api.post(f"{API}/run", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    run_ = out["run"]
    assert run_["status"] == "COMPLETED" and run_["result_hash"] == GOLDEN["result_hash"] and run_["n_windows"] == 5 and run_["market_data_requests"] == 1
    assert len(api.lab.market.calls) == 1 and api.hits == []                                             # one market-data request; no Robinhood / Alpaca / Claude
    assert [s["label"] for s in out["selections"]] == GOLDEN["selected"] and out["robustness"]["score"] == pytest.approx(GOLDEN["score"])
    assert "survivorship" in run_["universe_note"].lower() and "handoff" not in str(out).lower() and "config" not in str(out["selections"][0]).lower().replace("config_hash", "")
    rid = run_["run_id"]
    assert api.get(f"{API}/runs").json()["runs"][0]["run_id"] == rid
    d = api.get(f"{API}/runs/{rid}").json()
    assert d["run"]["status"] == "COMPLETED" and len(d["candidates"]) == 3 and d["universe"] == ["AAA", "BBB", "CCC"] and len(d["stitched_equity"]) == 312
    assert d["metrics"]["positive_window_pct"] == 0.6 and d["robustness"]["version"] == "rs_v1" and len(d["selections"]) == 5
    w = api.get(f"{API}/runs/{rid}/windows").json()
    assert len(w["windows"]) == 5 and len(w["candidates"]) == 15 and [s["label"] for s in w["selections"]] == GOLDEN["selected"]
    o = api.get(f"{API}/runs/{rid}/oos").json()
    assert len(o["oos"]) == 5 and "equity" not in o["oos"][0] and o["oos"][0]["test_metrics"]["total_return"] > 0
    assert len(api.get(f"{API}/runs/{rid}/oos?equity=true").json()["oos"][0]["equity"]) == w["windows"][0]["n_test_sessions"]
    s = api.get(f"{API}/runs/{rid}/sensitivity").json()
    assert len(s["cost"]) == 5 and s["parameter"]["fragile"] is False
    g = api.get(f"{API}/runs/{rid}/regimes").json()
    assert set(g["regimes"]) >= {"trend", "vol", "method"}
    assert api.get(f"{API}/runs/{'z' * 32}").status_code == 404 and api.get(f"{API}/runs/short/oos").status_code == 404
    bad = api.post(f"{API}/run", json={**body, "side": "BUY"})
    assert bad.status_code == 422                                                                            # strict body: no order fields exist
    assert api.post(f"{API}/run", json={**body, "train_months": 0}).status_code == 422
    assert api.post(f"{API}/run", json={**body, "config_hash": "0" * 64}).status_code == 409
    grid = api.post(f"{API}/run", json={**body, "candidates": None, "grid": {"dimensions": {"cash_buffer_pct": ["0.05", "0.10", "0.20"]}}, "max_candidates": 2})
    assert grid.status_code == 200 and grid.json()["n_candidates"] == 2 and grid.json()["run"]["n_discarded"] == 1
    js = (ROOT / "frontend" / "portfolio_walkforward.js").read_text(encoding="utf-8")
    assert js.count("fetch(") == 1 and API in js and "/api/alpaca" not in js and "deploy" not in js.lower().replace("deployed", "")


def test_runtime_note(lab):
    import time
    t0 = time.perf_counter()
    out = run(lab)
    dt = time.perf_counter() - t0
    n_completed = sum(1 for n in out["parameter_sensitivity"]["neighbours"] if n["status"] == "COMPLETED")
    assert out["run"]["status"] == "COMPLETED" and out["run"]["n_evaluations"] == 41 + n_completed and out["run"]["n_cache_hits"] == 5 and dt < 60
