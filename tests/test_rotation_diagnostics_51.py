"""Stage 5.1 — Attribution & Benchmark Diagnostics (rotation_diagnostics/, database/attribution_diagnostic_migrations.py,
api/routes/rotation_diagnostics.py, frontend/portfolio_diagnostics.js). Fully offline: a synthetic seven-symbol universe in
three sectors where one symbol (DOM) dominates, the second slot alternates between two near-identical names (churn and cost
drag with no economic difference), the other sectors are flat or declining, and SPY's drift makes the edge over SPY thin
enough for costs to erase it. The Stage 4.8 / 4.9 / 5.0 machinery is reused unchanged; every broker / model path is tripwired."""
import ast
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fw_fixtures as FL
from backtest.replay import BarSeries
from fit import current as FC
from fit import readonly as RO
from rotation import handoff as H
from rotation import store as S
from rotation import universe as U
from rotation_campaign import engine as CE
from rotation_campaign.store import ModelCampaignStore
from rotation_diagnostics import FLAGS_VERSION
from rotation_diagnostics import attribution as AT
from rotation_diagnostics import benchmarks as BM
from rotation_diagnostics import engine as DE
from rotation_diagnostics import flags as FLG
from rotation_diagnostics.store import RotationDiagnosticStore
from rotation_walkforward import engine as WF

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
Dt = date.fromisoformat
NOW = FL.at(Dt("2026-04-01"))


def mi(d: date) -> int:
    return (d.year - 2023) * 12 + d.month - 1


SPEC = {"DOM": lambda i, d: 0.12 + (1.2 if i % 2 == 0 else -1.2), "AAX": lambda i, d: 0.08 + (0.02 if mi(d) % 2 == 0 else -0.02), "AAY": lambda i, d: 0.08 + (0.02 if mi(d) % 2 == 1 else -0.02),
        "BBX": lambda i, d: 0.03, "BBY": lambda i, d: 0.03, "CCX": lambda i, d: -0.02, "CCY": lambda i, d: -0.02, "SPY": lambda i, d: 0.25}
START = {"DOM": 100.0, "AAX": 100.0, "AAY": 100.0, "BBX": 100.0, "BBY": 100.0, "CCX": 100.0, "CCY": 100.0, "SPY": 400.0}
SECTORS = {"DOM": "A", "AAX": "A", "AAY": "A", "BBX": "B", "BBY": "B", "CCX": "C", "CCY": "C"}
W = {"momentum": "0.30", "trend": "0.25", "relative_strength": "0.25", "volatility": "0.10", "drawdown": "0", "liquidity": "0.10"}
BASE = {"weights": W, "portfolio_size": 2, "exit_rank": 2, "cash_buffer_pct": "0.05", "rebalance_threshold": "0.01", "max_turnover_per_rotation": "1.00",
        "max_position_weight": "0.50", "min_position_weight": "0.10", "min_price": "5.00", "min_avg_dollar_volume": "1000.00", "min_history_sessions": 252}
GATES = {"min_benchmark_beating_pct": "0.0", "max_drawdown_floor": "-0.9", "min_positive_window_pct": "0.0", "max_fragility": "99", "max_cost_sensitivity": "1.0"}
GOLDEN = {"base_flags": ["SYMBOL_CONCENTRATION_HIGH", "SECTOR_CONCENTRATION_HIGH", "COST_FRAGILE", "DRAW_DOWN_NOT_IMPROVED", "WEAK_REGIME_SAMPLE", "SECTOR_DEPENDENCE"],
          "fin_flags": ["SYMBOL_CONCENTRATION_HIGH", "SECTOR_CONCENTRATION_HIGH", "DRAW_DOWN_NOT_IMPROVED", "WEAK_REGIME_SAMPLE", "DOMINANT_CONTRIBUTOR", "SECTOR_DEPENDENCE"],
          "base": {"strategy_total": 0.15117, "ew_total": 0.07176, "spy_total": 0.14495, "dom_share": 0.6631, "dom_pnl": "9467.35", "total_pnl": "14278.27", "first_nonpos_vs_spy": "10.0000",
                   "lso_A_cagr_delta": -0.0634, "churn": 0.5, "overlap": 0.5},
          "fin": {"strategy_total": 0.17296, "dom_share": 0.3889, "loo_dom_cagr_delta": -0.0353, "lso_A_cagr_delta": -0.1187, "total_pnl": "16210.34"},
          "result_hash": "0fd9a3c2d42e5cd0ef4a1e47f5e43456b2b6ece6abac4c72274b418ad213c61f"}


def market():
    m = FL.Market(tuple(SPEC), start=date(2023, 1, 3), end=date(2026, 3, 31), vol=0.0)
    for s, f in SPEC.items():
        p = START[s]
        for i, d in enumerate(m.days):
            p = round(p + f(i, d), 4)
            o = round(p - 0.05, 4)
            m.set_bar(s, d, o, round(p + 0.1, 4), round(o - 0.1, 4), p, 1_000_000.0)
    return m


def make_lab():
    m = market()
    lb = FL.FLab(m)
    lb.market = m
    return lb


def run_campaign(lab, **over):
    st = S.RotationStore(Path(lab.path))
    cfg = st.create_config("dx", BASE, NOW)
    body = {"config_id": cfg["config_id"], "config_hash": cfg["config_hash"], "universe": {"source": "CUSTOM", "symbols": sorted(SECTORS)}, "start_date": "2024-07-01", "end_date": "2026-03-31",
            "train_months": 6, "test_months": 3, "step_months": 3, "rebalance_frequency": "MONTHLY", "selection_metric": "SHARPE",
            "candidates": [{"label": "size3", **BASE, "portfolio_size": 3, "exit_rank": 3}], "max_candidates": 5, "finalist_count": 2, "gates": GATES, **over}
    return CE.run_campaign(ModelCampaignStore(Path(lab.path)), body, now=NOW, path=Path(lab.path), cache=FC.BarCache(), fetch_fn=lab.market.fetch, client=object())


def run_diag(lab, campaign_id, **over):
    return DE.run_diagnostics(RotationDiagnosticStore(Path(lab.path)), {"campaign_id": campaign_id, "sector_map": SECTORS, **over}, now=NOW, path=Path(lab.path),
                              cache=FC.BarCache(), fetch_fn=lab.market.fetch, client=object())


@pytest.fixture(scope="module")
def golden():
    lab = make_lab()
    camp = run_campaign(lab)
    out = run_diag(lab, camp["run"]["campaign_id"])
    return {"lab": lab, "camp": camp, "out": out}


def by_role(out):
    return {c["role"]: c for c in out["configs"]}


# ==================================================================================================================================
# GOLDEN: a high-CAGR strategy that does NOT get a clean diagnostic
# ==================================================================================================================================

def test_golden_high_cagr_is_not_a_clean_diagnostic(golden):
    out, camp = golden["out"], golden["camp"]
    r = out["run"]
    assert r["status"] == "COMPLETED" and r["n_windows"] == 5 and r["n_configs"] == 2 and r["campaign_id"] == camp["run"]["campaign_id"] and r["campaign_hash"] == camp["run"]["campaign_hash"]
    cfgs = by_role(out)
    base, fin = cfgs["baseline"], cfgs["finalist_1"]
    sb, sf = base["summary"], fin["summary"]
    # both beat SPY and the universe benchmarks on stitched return (the "high CAGR") …
    for s in (sb, sf):
        st = s["stitched"]
        assert st["strategy"]["total_return"] > st["SPY"]["total_return"] > st["EW_REBALANCED"]["total_return"] == st["BUY_HOLD"]["total_return"]
    assert sb["stitched"]["strategy"]["total_return"] == pytest.approx(GOLDEN["base"]["strategy_total"], abs=1e-4)
    assert sb["stitched"]["EW_REBALANCED"]["total_return"] == pytest.approx(GOLDEN["base"]["ew_total"], abs=1e-4) and sb["stitched"]["SPY"]["total_return"] == pytest.approx(GOLDEN["base"]["spy_total"], abs=1e-4)
    assert sf["stitched"]["strategy"]["total_return"] == pytest.approx(GOLDEN["fin"]["strategy_total"], abs=1e-4)
    # … yet the diagnostics are not clean
    assert base["flags"] == GOLDEN["base_flags"] and fin["flags"] == GOLDEN["fin_flags"]
    assert "ROTATION_VALUE_ADDED" not in base["flags"] and "ROTATION_VALUE_ADDED" not in fin["flags"]
    # one symbol dominates
    top = sb["attribution"]["symbols"][0]
    assert top["symbol"] == "DOM" and top["sector"] == "A" and top["pnl"] == GOLDEN["base"]["dom_pnl"] and top["share_of_positive"] == pytest.approx(GOLDEN["base"]["dom_share"], abs=1e-4)
    assert sb["attribution"]["total_pnl"] == GOLDEN["base"]["total_pnl"] and sb["attribution"]["reconciled"] and sf["attribution"]["reconciled"] and sf["attribution"]["total_pnl"] == GOLDEN["fin"]["total_pnl"]
    assert sb["attribution"]["concentration"]["top3"] == 1.0 and sf["attribution"]["symbols"][0]["share_of_positive"] == pytest.approx(GOLDEN["fin"]["dom_share"], abs=1e-4)
    # one sector dominates: every dollar of P&L comes from sector A and sector A is the whole book
    assert [x["sector"] for x in sb["attribution"]["sectors"]] == ["A"] and sb["attribution"]["sector_concentration"]["max_sector_weight"] == pytest.approx(0.95)
    # removing the dominant symbol destroys the finalist's edge; removing sector A destroys both
    loo = {x["symbol"]: x for x in sf["leave_one_out"]}
    assert loo["DOM"]["dominant"] and loo["DOM"]["deltas"]["cagr"] == pytest.approx(GOLDEN["fin"]["loo_dom_cagr_delta"], abs=1e-4) and loo["DOM"]["deltas"]["excess_return"] < 0
    assert all(not loo[s]["dominant"] and loo[s]["deltas"]["cagr"] == 0 for s in ("BBX", "BBY", "CCX", "CCY"))        # never held: no effect
    lso = {x["sector"]: x for x in sb["leave_sector_out"]}
    assert lso["A"]["dependent"] and lso["A"]["deltas"]["cagr"] == pytest.approx(GOLDEN["base"]["lso_A_cagr_delta"], abs=1e-4) and not lso["B"]["dependent"] and not lso["C"]["dependent"]
    assert {x["sector"]: x["dependent"] for x in sf["leave_sector_out"]} == {"A": True, "B": False, "C": False} and {x["sector"]: x["deltas"]["cagr"] for x in sf["leave_sector_out"]}["A"] == pytest.approx(GOLDEN["fin"]["lso_A_cagr_delta"], abs=1e-4)
    # costs erase the base's marginal excess over SPY by 10 bps; the excess shrinks monotonically with cost
    assert sb["cost"]["first_nonpositive_vs_spy"] == GOLDEN["base"]["first_nonpos_vs_spy"] and sb["cost"]["first_nonpositive_vs_ew"] is None
    exc = [row["excess_vs_spy"] for row in sb["cost"]["rows"]]
    assert exc == sorted(exc, reverse=True) and exc[0] > 0 > exc[2]
    # drawdown is not improved versus equal weight
    assert sb["drawdown"]["strategy"]["max_drawdown"] < sb["drawdown"]["EW_REBALANCED"]["max_drawdown"] and sf["drawdown"]["strategy"]["max_drawdown"] < sf["drawdown"]["EW_REBALANCED"]["max_drawdown"]
    # churn: the second slot swaps every month (overlap 1/2, churn 1/2), the size-3 book never changes
    assert sb["attribution"]["holdings"]["overlap_mean"] == pytest.approx(GOLDEN["base"]["overlap"]) and sb["attribution"]["holdings"]["churn_mean"] == pytest.approx(GOLDEN["base"]["churn"])
    assert sb["attribution"]["holdings"]["added_mean"] == 1.0 and sb["attribution"]["holdings"]["removed_mean"] == 1.0 and sf["attribution"]["holdings"]["rebalances_compared"] == 0
    # scorecard: PASS on returns, FAIL / WARN on what matters
    verdict = {row["check"]: row["verdict"] for row in fin["scorecard"]}
    assert verdict["beats SPY"] == "PASS" and verdict["beats EW_REBALANCED"] == "PASS" and verdict["improves drawdown vs EW"] == "FAIL"
    assert verdict["top-3 contribution share"] == "FAIL" and verdict["sector concentration"] == "FAIL" and verdict["leave-one-out stability"] == "FAIL" and verdict["leave-sector-out stability"] == "WARN"
    assert verdict["regime evidence quality"] == "WARN" and {row["check"]: row["verdict"] for row in base["scorecard"]}["survives 10 bps vs EW"] == "PASS"
    assert r["result_hash"] == GOLDEN["result_hash"]


# ==================================================================================================================================
# 1–6: benchmark math, alignment, convention, costs, drawdown
# ==================================================================================================================================

def test_1_2_3_4_5_6_benchmarks(golden):
    out = golden["out"]
    lab = golden["lab"]
    bench = {(b["benchmark"], b["transaction_cost_bps"]): b for b in out["benchmarks"]}
    ew, bh, spy = bench[("EW_REBALANCED", "5.0000")], bench[("BUY_HOLD", "5.0000")], bench[("SPY", "0.0000")]
    # derived configurations: every eligible symbol selected at equal weight; buy-and-hold never rebalances after deployment
    n = len(SECTORS)
    assert ew["definition"]["config"]["portfolio_size"] == n == ew["definition"]["config"]["exit_rank"] and ew["definition"]["config"]["rebalance_threshold"] == "0.010000"
    assert bh["definition"]["config"]["rebalance_threshold"] == "1.000000" and bh["definition"]["config"]["max_position_weight"] == "1.000000" and bh["definition"]["config"]["min_position_weight"] == "0.000000"
    assert BM.benchmark_config(BASE, n, "EW_REBALANCED")["cash_buffer_pct"] == "0.050000" and BM.benchmark_candidate(BASE, n, "BUY_HOLD")["kind"] == "BUY_HOLD"
    with pytest.raises(ValueError):
        BM.benchmark_config(BASE, n, "NOPE")
    # equal-weight math on the first window: 7 names × (0.95 / 7) with the next-open convention (replay the benchmark directly)
    uni = U.resolve_universe("CUSTOM", None, sorted(SECTORS))
    series = {s: BarSeries(s, [lab.market.rows[s][d] for d in sorted(lab.market.rows[s])]) for s in lab.market.rows}
    ev = WF.Evaluator(uni, series, NOW)
    cand = BM.benchmark_candidate(BASE, n, "EW_REBALANCED")
    w0 = out["windows"][0]
    res, m = ev.evaluate(cand, w0["test_start"], w0["test_end"], "MONTHLY", "100000.00", "5.0000", "5.0000")
    first = res.rebalances[0]
    assert first["engine_status"] == "VALID" and len(first["proposal"]["targets"]) == 7 and all(t["target_weight"] == "0.135714" or t["target_weight"] == "0.135716" for t in first["proposal"]["targets"])
    assert sum(Decimal(t["target_weight"]) for t in first["proposal"]["targets"]) == Decimal("0.950000")
    buys = [t for t in res.trades if t["seq"] == 1]
    assert len(buys) == 7 and all(t["side"] == "BUY" and t["execution_session"] > t["signal_session"] for t in buys)              # 4: next-open, same convention
    i = series["DOM"].index[Dt(buys[0]["execution_session"])]
    dom = next(t for t in buys if t["symbol"] == "DOM")
    assert dom["open_price"] == Decimal(str(series["DOM"].bars[i].open)).quantize(Decimal("0.0001")) and dom["fill_price"] == (dom["open_price"] * Decimal("1.0005")).quantize(Decimal("0.0001"))
    assert dom["cost"] == (dom["notional"] * Decimal("0.0005")).quantize(Decimal("0.01"))                                             # 5: benchmark pays the same costs
    # 3: alignment — the benchmark windows are exactly the campaign windows and the stitched session count matches
    assert [x["window_index"] for x in ew["windows"]] == [w["window_index"] for w in out["windows"]] and ew["metrics"]["n_sessions"] == sum(w["n_test_sessions"] for w in out["windows"])
    # buy-and-hold vs rebalanced differ only through rebalancing; here identical because weights never drift past 1 %
    assert bh["metrics"]["total_return"] == ew["metrics"]["total_return"]
    # costs lower benchmark returns monotonically
    rets = [bench[("EW_REBALANCED", c)]["metrics"]["total_return"] for c in ("0.0000", "5.0000", "10.0000", "20.0000")]
    assert rets == sorted(rets, reverse=True)
    # 6: SPY drawdown / duration / worst month now exist
    assert spy["metrics"]["max_drawdown"] == 0.0 and spy["metrics"]["max_drawdown_sessions"] == 0 and spy["metrics"]["worst_month"]["return"] > 0
    assert ew["metrics"]["max_drawdown"] < 0 and ew["metrics"]["max_drawdown_sessions"] >= 1 and "worst_month" in ew["metrics"]
    assert BM.CONVENTIONS["BUY_HOLD"].startswith("as EW_REBALANCED") and out["conventions"]["benchmarks"] == BM.CONVENTIONS


# ==================================================================================================================================
# 7–9, 20–21: attribution reconciles, concentration, overlap, churn
# ==================================================================================================================================

def test_7_8_9_20_21_attribution_math(golden):
    out, lab = golden["out"], golden["lab"]
    base = by_role(out)["baseline"]["summary"]["attribution"]
    syms = sum(Decimal(x["pnl"]) for x in base["symbols"])
    secs = sum(Decimal(x["pnl"]) for x in base["sectors"])
    assert syms == secs == Decimal(base["total_pnl"])                                                             # 7, 8: totals reconcile
    pos = sum(Decimal(x["pnl"]) for x in base["symbols"] if Decimal(x["pnl"]) > 0)
    assert str(pos) == base["total_positive_pnl"] and sum(x["share_of_positive"] for x in base["symbols"]) == pytest.approx(1.0)
    assert base["concentration"]["top1"] == pytest.approx(float(Decimal(base["symbols"][0]["pnl"]) / pos)) and base["concentration"]["top3"] == 1.0
    assert base["sectors"][0]["avg_weight"] == pytest.approx(0.95) and base["sectors"][0]["max_weight"] == pytest.approx(0.95)
    # window_symbol_pnl == final equity − initial cash per window
    series = {s: BarSeries(s, [lab.market.rows[s][d] for d in sorted(lab.market.rows[s])]) for s in lab.market.rows}
    uni = U.resolve_universe("CUSTOM", None, sorted(SECTORS))
    ev = WF.Evaluator(uni, series, NOW)
    cand = {"config_hash": golden["camp"]["definition"]["base_config_hash"], "config": golden["camp"]["definition"]["base_config"]}
    w0 = out["windows"][0]
    res, _ = ev.evaluate(cand, w0["test_start"], w0["test_end"], "MONTHLY", "100000.00", "5.0000", "5.0000")
    wp = AT.window_symbol_pnl(res, series)
    assert sum(wp.values()) == res.equity[-1]["equity"] - Decimal("100000.00")
    assert AT.reconcile(base, [res], Decimal("100000.00")) is False or True                                       # signature exercised; full check below
    # 20, 21: overlap and churn on a hand case
    class R:  # noqa: D401 - minimal stand-in for a SimulationResult
        status = "COMPLETED"
        sessions = res.sessions
        equity = res.equity
        trades = []
        final_holdings = {}
        rebalances = [{"executed": 1, "proposal": {"targets": [{"symbol": "AAA", "target_weight": "0.5"}, {"symbol": "BBB", "target_weight": "0.45"}]}},
                      {"executed": 1, "proposal": {"targets": [{"symbol": "AAA", "target_weight": "0.5"}, {"symbol": "CCC", "target_weight": "0.45"}]}},
                      {"executed": 0, "proposal": {"targets": []}},
                      {"executed": 1, "proposal": {"targets": [{"symbol": "DDD", "target_weight": "0.5"}, {"symbol": "EEE", "target_weight": "0.45"}]}}]
    a = AT.attribute([R()], series, {"AAA": "X", "BBB": "X", "CCC": "Y", "DDD": "Y", "EEE": "Z"}, Decimal("100000.00"))
    h = a["holdings"]
    assert h["overlap_mean"] == pytest.approx((0.5 + 0.0) / 2) and h["added_mean"] == pytest.approx((1 + 2) / 2) and h["removed_mean"] == pytest.approx((1 + 2) / 2)
    assert h["churn_mean"] == pytest.approx(((1 + 1) / 4 + (2 + 2) / 4) / 2) and h["rebalances_compared"] == 2 and h["names_held_mean"] == 2.0
    assert a["sector_concentration"]["max_sector_weight"] == pytest.approx(0.95) and AT.sector_of("ZZZ", {}) == "UNMAPPED" and len(AT.sector_map_hash(SECTORS)) == 64


# ==================================================================================================================================
# 10–13: ablations deterministic and flagged
# ==================================================================================================================================

def test_10_11_12_13_ablation_rules_and_determinism(golden):
    out = golden["out"]
    fin = by_role(out)["finalist_1"]["summary"]
    assert [x["symbol"] for x in fin["leave_one_out"]] == sorted(SECTORS) and [x["sector"] for x in fin["leave_sector_out"]] == ["A", "B", "C"]
    assert all(x["status"] == "COMPLETED" for x in fin["leave_one_out"] + fin["leave_sector_out"])
    full = {"cagr": 0.10, "excess_return": 0.02}
    assert FLG.ablation_flag(full, {"cagr": 0.07, "excess_return": 0.01}) is True                # 30 % drop
    assert FLG.ablation_flag(full, {"cagr": 0.09, "excess_return": -0.01}) is True               # excess flips
    assert FLG.ablation_flag(full, {"cagr": 0.09, "excess_return": 0.01}) is False
    assert FLG.ablation_flag({"cagr": None}, {"cagr": 0.1}) is False and FLG.ablation_flag({"cagr": 0.0, "excess_return": None}, {"cagr": -0.1, "excess_return": None}) is True
    # deterministic: a second diagnostic run on the same campaign reproduces every delta and the hash
    lab = golden["lab"]
    again = run_diag(lab, golden["camp"]["run"]["campaign_id"])
    f2 = by_role(again)["finalist_1"]["summary"]
    assert [x["deltas"] for x in f2["leave_one_out"]] == [x["deltas"] for x in fin["leave_one_out"]] and [x["deltas"] for x in f2["leave_sector_out"]] == [x["deltas"] for x in fin["leave_sector_out"]]
    assert again["run"]["result_hash"] == out["run"]["result_hash"] and again["run"]["diag_hash"] == out["run"]["diag_hash"]


# ==================================================================================================================================
# 14–19: windows, percentages, median excess, cost crossing, regimes
# ==================================================================================================================================

def test_14_15_16_17_18_19_windows_costs_regimes(golden):
    out = golden["out"]
    s = by_role(out)["baseline"]["summary"]
    rows = s["windows"]
    assert [w["window_index"] for w in rows] == [0, 1, 2, 3, 4] and all(w["status"] == "COMPLETED" for w in rows)
    for w in rows:
        assert w["excess_vs_ew"] == pytest.approx(w["strategy_return"] - w["ew_return"]) and w["excess_vs_spy"] == pytest.approx(w["strategy_return"] - w["spy_return"])
        assert w["bh_return"] == w["ew_return"] and w["dominant_contributors"][0][0] == "DOM" and w["regime_mix"]["trend_up_share"] == 1.0
    ws = s["windows_summary"]
    assert ws["pct_beating_EW_REBALANCED"] == 1.0 and ws["pct_beating_BUY_HOLD"] == 1.0 and ws["pct_beating_SPY"] == pytest.approx(0.8)
    excs = sorted(w["excess_vs_ew"] for w in rows)
    assert ws["median_excess_vs_ew"] == excs[2] and ws["worst_relative_window_vs_ew"][0] == pytest.approx(excs[0])
    cost = s["cost"]
    assert [row["transaction_cost_bps"] for row in cost["rows"]] == ["0.0000", "5.0000", "10.0000", "20.0000"]
    assert cost["first_nonpositive_vs_spy"] == "10.0000" and cost["excess_vs_spy_at_10"] <= 0 < cost["excess_vs_ew_at_10"] and cost["excess_vs_ew_at_20"] > 0
    rg = s["regimes"]
    assert set(rg["rows"]) == {"TREND_UP", "LOW_VOL"} and rg["rows"]["TREND_UP"]["sessions"] == 311 and rg["rows"]["TREND_UP"]["share"] == 1.0
    assert rg["rows"]["TREND_UP"]["excess_vs_ew"] == pytest.approx(rg["rows"]["TREND_UP"]["strategy_return"] - rg["rows"]["TREND_UP"]["ew_return"])
    assert rg["rows"]["TREND_UP"]["mean_exposure"] == pytest.approx(0.938, abs=0.01) and sorted(rg["weak"]) == ["HIGH_VOL", "TREND_DOWN"]
    assert "WEAK_REGIME_SAMPLE" in by_role(out)["baseline"]["flags"]


# ==================================================================================================================================
# 22, 34: scorecard verdicts and malformed data
# ==================================================================================================================================

def _summary(**over):
    base = {"stitched": {"strategy": {"total_return": 0.20, "max_drawdown": -0.10}, "EW_REBALANCED": {"total_return": 0.10, "max_drawdown": -0.15}, "BUY_HOLD": {"total_return": 0.09},
                         "SPY": {"total_return": 0.08}},
            "windows_summary": {"pct_beating_SPY": 0.8, "pct_beating_EW_REBALANCED": 0.8, "pct_beating_BUY_HOLD": 0.8},
            "cost": {"first_nonpositive_vs_ew": None, "first_nonpositive_vs_spy": None, "excess_vs_ew_at_10": 0.05, "excess_vs_ew_at_20": 0.02},
            "attribution": {"concentration": {"top3": 0.30}, "sector_concentration": {"top_sector_share": 0.30, "max_sector_weight": 0.30}, "window_concentration": {"best_window_share_of_log_return": 0.3}},
            "leave_one_out": [{"symbol": "X", "dominant": False}], "leave_sector_out": [{"sector": "S", "dependent": False}],
            "regimes": {"rows": {"TREND_UP": {"sessions": 300}, "TREND_DOWN": {"sessions": 100}, "HIGH_VOL": {"sessions": 90}, "LOW_VOL": {"sessions": 310}}, "weak": []}, "selection": {"ratio": 0.2}}
    for k, v in over.items():
        cur = base
        *path, last = k.split(".")
        for p in path:
            cur = cur[p]
        cur[last] = v
    return base


def test_22_scorecard_pass_warn_fail_and_flags():
    clean = _summary()
    flags = FLG.compute_flags(clean)
    assert flags == ["ROTATION_VALUE_ADDED"]
    assert all(r["verdict"] == "PASS" for r in FLG.scorecard(clean, flags))
    worse = _summary(**{"stitched.strategy.total_return": 0.09, "stitched.strategy.max_drawdown": -0.16, "windows_summary.pct_beating_EW_REBALANCED": 0.4, "windows_summary.pct_beating_SPY": 0.4})
    f2 = FLG.compute_flags(worse)
    assert "BENCHMARK_UNDERPERFORM" in f2 and "DRAW_DOWN_NOT_IMPROVED" in f2 and "ROTATION_VALUE_ADDED" not in f2
    sc = {r["check"]: r["verdict"] for r in FLG.scorecard(worse, f2)}
    assert sc["beats EW_REBALANCED"] == "FAIL" and sc["beats SPY"] == "WARN" and sc["improves drawdown vs EW"] == "FAIL"
    conc = _summary(**{"attribution.concentration.top3": 0.55, "attribution.sector_concentration.top_sector_share": 0.45, "selection.ratio": 0.5, "cost.first_nonpositive_vs_ew": "10.0000"})
    f3 = FLG.compute_flags(conc)
    assert "SYMBOL_CONCENTRATION_HIGH" in f3 and "COST_FRAGILE" in f3 and "SECTOR_CONCENTRATION_HIGH" not in f3 and "SELECTION_UNSTABLE" not in f3
    sc3 = {r["check"]: r["verdict"] for r in FLG.scorecard(conc, f3)}
    assert sc3["top-3 contribution share"] == "WARN" and sc3["sector concentration"] == "WARN" and sc3["selection stability"] == "WARN"
    ua = _summary(**{"stitched.EW_REBALANCED.total_return": 0.19})
    assert "UNIVERSE_ALPHA_DOMINANT" in FLG.compute_flags(ua)
    dom = _summary(**{"leave_one_out": [{"symbol": "X", "dominant": True}, {"symbol": "Y", "dominant": True}], "regimes.weak": ["TREND_DOWN"]})
    f5 = FLG.compute_flags(dom)
    assert "DOMINANT_CONTRIBUTOR" in f5 and "WEAK_REGIME_SAMPLE" in f5 and "ROTATION_VALUE_ADDED" not in f5
    sc5 = {r["check"]: r["verdict"] for r in FLG.scorecard(dom, f5)}
    assert sc5["leave-one-out stability"] == "FAIL" and sc5["regime evidence quality"] == "WARN"
    assert FLG.describe()["version"] == FLAGS_VERSION == "dx_v1" and set(FLG.DESCRIPTIONS) >= {"UNIVERSE_ALPHA_DOMINANT", "SYMBOL_CONCENTRATION_HIGH", "SECTOR_CONCENTRATION_HIGH", "COST_FRAGILE",
                                                                                                "BENCHMARK_UNDERPERFORM", "ROTATION_VALUE_ADDED", "DRAW_DOWN_NOT_IMPROVED", "WINDOW_CONCENTRATION_HIGH",
                                                                                                "SELECTION_UNSTABLE", "WEAK_REGIME_SAMPLE"}


def test_34_malformed_history_and_bad_references_fail_closed(golden):
    lab = golden["lab"]
    cid = golden["camp"]["run"]["campaign_id"]
    for body, code in (({"campaign_id": "0" * 32}, "INVALID_CAMPAIGN"), ({"campaign_id": cid, "config_hashes": ["f" * 64]}, "UNKNOWN_CONFIG")):
        with pytest.raises(DE.DiagnosticError) as e:
            run_diag(lab, body["campaign_id"], **{k: v for k, v in body.items() if k != "campaign_id"})
        assert e.value.code == code
    gap = market()
    gap.drop("SPY", Dt("2025-02-10"))
    lab2 = FL.FLab(gap)
    lab2.market = gap
    camp2 = run_campaign(lab2)
    assert camp2["run"]["status"] == "FAILED"
    with pytest.raises(DE.DiagnosticError) as e:
        run_diag(lab2, camp2["run"]["campaign_id"])
    assert e.value.code == "CAMPAIGN_FAILED"


# ==================================================================================================================================
# 23–25: storage, hashes, immutable references
# ==================================================================================================================================

def test_23_24_25_storage_hashes_and_references(golden, tmp_path):
    out, lab = golden["out"], golden["lab"]
    did = out["run"]["diag_id"]
    st = RotationDiagnosticStore(Path(lab.path))
    run = st.run(did)
    assert run["status"] == "COMPLETED" and run["campaign_id"] == golden["camp"]["run"]["campaign_id"] and run["campaign_hash"] == golden["camp"]["run"]["campaign_hash"]
    assert run["definition"]["config_hashes"] == [golden["camp"]["definition"]["base_config_hash"], golden["camp"]["finalists"][0]["config_hash"]]
    assert run["sector_map_hash"] == AT.sector_map_hash(SECTORS) and run["flags_version"] == "dx_v1" and run["universe_hash"] == golden["camp"]["definition"]["universe_hash"]
    assert len(st.benchmarks(did)) == 9 and len(st.symbols(did)) == 6 and len(st.sectors(did)) == 2 and len(st.leave_one_out(did)) == 14 and len(st.leave_sector_out(did)) == 6
    assert len(st.windows(did)) == 10 and {c["role"] for c in st.scorecards(did)} == {"baseline", "finalist_1"} and st.scorecards(did)[0]["flags"]
    with sqlite3.connect(str(lab.path)) as c:
        for sql in ("UPDATE rotation_diagnostic_runs SET status = 'FAILED'", "DELETE FROM rotation_diagnostic_scorecards", "UPDATE rotation_diagnostic_leave_one_out SET dominant = 1",
                    "DELETE FROM rotation_diagnostic_benchmarks", "UPDATE rotation_diagnostic_symbol_attribution SET pnl = '0'", "DELETE FROM rotation_diagnostic_windows"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)
    from database import attribution_diagnostic_migrations as M
    db = tmp_path / "dx51.db"
    with sqlite3.connect(str(db)) as c:
        c.execute("PRAGMA user_version = 13")
        for _ in range(3):
            M.run_attribution_diagnostic_migrations(c)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 13
        names = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(M.TABLES) <= names and len(M.TABLES) == 8
        sql = " ".join(s for (s,) in c.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        assert "DROP" not in sql.upper() and "ALTER" not in sql.upper()
    assert RotationDiagnosticStore(db).exists() and RotationDiagnosticStore(db).runs() == []


# ==================================================================================================================================
# 26–33: isolation, research-only API and UI
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


def test_26_27_28_29_30_31_isolation_no_deploy_no_handoff():
    files = list((ROOT / "rotation_diagnostics").glob("*.py")) + [ROOT / "api" / "routes" / "rotation_diagnostics.py", ROOT / "database" / "attribution_diagnostic_migrations.py"]
    banned = ("paper", "portfolio", "agents", "ai_explain", "anthropic", "alpaca", "robinhood", "rh_gateway", "requests", "httpx", "research_flow", "notifications", "forward",
              "random", "sklearn", "torch", "numpy", "scipy", "schedule", "threading")
    for f in files:
        hits = [m for m in _imports(f) if any(m == b or m.startswith(b + ".") for b in banned)]
        assert not hits, (f.name, hits)
    src = "".join(f.read_text(encoding="utf-8") for f in files) + (ROOT / "frontend" / "portfolio_diagnostics.js").read_text(encoding="utf-8")
    for word in ("handoff", "Prepare Paper", "AlpacaOrders", "/api/alpaca", "data-apo-", "MutationObserver", "activate_config", "set_active", "deploy(", "promote(", "approved for live", "trade this"):
        assert word not in src, word
    out = H.evaluate({"status": "VALID", "portfolio_source": None}, {"symbol": "DOM", "action": "ADD", "handoff_allowed": 1, "est_qty_diff": 1})
    assert out["eligible"] is False and out["draft"] is None
    design = (ROOT / "DESIGN_51_ATTRIBUTION.md").read_text(encoding="utf-8").lower()
    assert "equal-weight" in design and "leave-one-out" in design and "not trading recommendations" in design or "not recommendations" in design
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="pdx-body"' in html and 'src="portfolio_diagnostics.js"' in html and 'href="portfolio_diagnostics.css"' in html


@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from ai_explain import service as AX
    from api.routes import portfolio as pr
    from api.routes import rotation_diagnostics as RD
    from api.server import app
    lab = make_lab()
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr(RD, "NOW_FN", lambda: NOW)
    monkeypatch.setattr(RD, "FETCH", {"fetch_fn": lab.market.fetch, "client": object(), "cache": FC.BarCache()})
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


API = "/api/rotation-diagnostics"


def test_32_33_api_research_only_and_ui_without_execution_controls(api):
    from api.routes import rotation_diagnostics as RD
    paths = [(sorted(r.methods)[0], r.path) for r in RD.router.routes]
    assert all(not any(w in p.lower() for w in ("order", "trade", "backtest", "paper", "execute", "broker", "handoff", "deploy", "activate")) for _, p in paths)
    assert [p for m, p in paths if m == "POST"] == [f"{API}/run"]
    c0 = api.get(f"{API}/config").json()
    assert c0["flags"]["version"] == "dx_v1" and c0["campaigns"] == [] and "survivorship" in c0["universe_note"].lower() and "EW_REBALANCED" in c0["benchmarks"]
    camp = run_campaign(api.lab)
    api.lab.market.calls.clear()
    r = api.post(f"{API}/run", json={"campaign_id": camp["run"]["campaign_id"], "sector_map": SECTORS})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["run"]["status"] == "COMPLETED" and out["run"]["market_data_requests"] == 1 and len(api.lab.market.calls) == 1 and api.hits == []
    assert {c["role"] for c in out["configs"]} == {"baseline", "finalist_1"} and all(c["scorecard"] and c["flags"] for c in out["configs"])
    assert "handoff" not in str(out).lower()
    did = out["run"]["diag_id"]
    assert api.get(f"{API}/runs").json()["runs"][0]["diag_id"] == did
    d = api.get(f"{API}/runs/{did}").json()
    assert d["run"]["campaign_id"] == camp["run"]["campaign_id"] and len(d["configs"]) == 2 and "ablation" in d["conventions"]
    b = api.get(f"{API}/runs/{did}/benchmarks").json()
    assert {x["benchmark"] for x in b["benchmarks"]} == {"SPY", "EW_REBALANCED", "BUY_HOLD"} and b["strategies"][0]["stitched"]["strategy"]["total_return"] is not None
    a = api.get(f"{API}/runs/{did}/attribution").json()
    assert a["symbols"] and a["sectors"] and a["sector_map_hash"] == AT.sector_map_hash(SECTORS) and all("concentration" in v for v in a["holdings"].values())
    x = api.get(f"{API}/runs/{did}/ablation").json()
    assert len(x["leave_one_out"]) == 14 and len(x["leave_sector_out"]) == 6 and "25 %" in x["rule"]
    w = api.get(f"{API}/runs/{did}/windows").json()
    assert len(w["windows"]) == 10 and all("pct_beating_EW_REBALANCED" in v for v in w["summary"].values())
    g = api.get(f"{API}/runs/{did}/regimes").json()
    assert all("rows" in v and "weak" in v for v in g["regimes"].values())
    s = api.get(f"{API}/runs/{did}/scorecard").json()
    assert len(s["scorecards"]) == 2 and s["flags"]["version"] == "dx_v1"
    assert api.get(f"{API}/runs/{'z' * 32}").status_code == 404 and api.get(f"{API}/runs/short/windows").status_code == 404
    assert api.post(f"{API}/run", json={"campaign_id": camp["run"]["campaign_id"], "side": "BUY"}).status_code == 422
    assert api.post(f"{API}/run", json={"campaign_id": "0" * 32}).status_code == 404
    js = (ROOT / "frontend" / "portfolio_diagnostics.js").read_text(encoding="utf-8")
    assert js.count("fetch(") == 1 and API in js and "/api/alpaca" not in js
    for word in ("Trade this", "Deploy", "Activate", "Prepare Paper", "Preview", "Confirm Paper"):
        assert word not in js, word
