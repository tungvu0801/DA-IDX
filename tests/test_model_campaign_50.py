"""Stage 5.0 — Model Evaluation Campaign / Leaderboard (rotation_campaign/, database/model_campaign_migrations.py,
api/routes/model_campaign.py, frontend/portfolio_campaign.js). Fully offline: a synthetic six-candidate campaign whose
eligibility and ranking follow from designed price properties (the CAGR leader crashes, the Sharpe leader is cost-fragile,
a whipsaw configuration is parameter-fragile, a decliner fails, two steady names qualify); the Stage 4.8 / 4.9 machinery is
reused unchanged; conftest blocks sockets and tripwires the Alpaca wires; every provider / model path is tripwired."""
import ast
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fw_fixtures as FL
from fit import current as FC
from fit import readonly as RO
from rotation import handoff as H
from rotation import store as S
from rotation import universe as U
from rotation_campaign import FINALIST_LABEL
from rotation_campaign import config as C
from rotation_campaign import engine as CE
from rotation_campaign import leaderboard as LB
from rotation_campaign.store import ModelCampaignStore
from rotation_walkforward import grid as G

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
Dt = date.fromisoformat
NOW = FL.at(Dt("2026-04-01"))
Q = 13
# daily increments per quarter (2023Q1 … 2026Q1) plus an alternating ±zig: RRR / TTT steady with noise (robust), HHH strong with one
# crash quarter (CAGR leader, deep drawdown), SSS tiny perfectly smooth drift (Sharpe leader, cost-fragile, never beats SPY),
# XXX decliner; AAA / BBB alternate leadership monthly (a buffered one-name configuration on them is parameter-fragile)
SLOPES = {"RRR": [0.15] * Q, "TTT": [0.12] * Q, "HHH": [0.6] * 11 + [-3.5, 0.6], "SSS": [0.008] * Q, "XXX": [-0.1] * Q, "SPY": [0.1] * Q}
ZIG = {"RRR": 0.6, "TTT": 0.5, "HHH": 2.4, "SSS": 0.0, "XXX": 0.4, "SPY": 0.0}
START = {"RRR": 100.0, "TTT": 100.0, "HHH": 100.0, "SSS": 100.0, "XXX": 300.0, "AAA": 100.0, "BBB": 100.0, "SPY": 400.0}
SYMS = ("RRR", "TTT", "HHH", "SSS", "XXX", "AAA", "BBB", "SPY")
ALL = ["RRR", "TTT", "HHH", "SSS", "XXX", "AAA", "BBB"]
W = {"momentum": "0.30", "trend": "0.25", "relative_strength": "0.25", "volatility": "0.10", "drawdown": "0", "liquidity": "0.10"}
CORE = {"weights": W, "portfolio_size": 1, "exit_rank": 1, "cash_buffer_pct": "0.05", "rebalance_threshold": "0.01", "max_turnover_per_rotation": "1.00",
        "max_position_weight": "0.95", "min_position_weight": "0.10", "min_price": "5.00", "min_avg_dollar_volume": "1000.00", "min_history_sessions": 252}
only = lambda s: [x for x in ALL if x != s]   # noqa: E731
BASE = {**CORE, "excluded_symbols": only("RRR")}
CANDS = [{"label": "steady-2", **CORE, "excluded_symbols": only("TTT")}, {"label": "high-cagr", **CORE, "excluded_symbols": only("HHH")},
         {"label": "sharpe", **CORE, "excluded_symbols": only("SSS")}, {"label": "whipsaw", **CORE, "exit_rank": 2, "excluded_symbols": ["RRR", "TTT", "HHH", "SSS", "XXX"]},
         {"label": "decliner", **CORE, "excluded_symbols": only("XXX")}]
BODY = {"start_date": "2024-07-01", "end_date": "2026-03-31", "train_months": 6, "test_months": 3, "step_months": 3, "rebalance_frequency": "MONTHLY",
        "selection_metric": "SHARPE", "candidates": CANDS, "max_candidates": 20, "finalist_count": 3, "initial_cash": "100000.00", "transaction_cost_bps": "5", "slippage_bps": "5"}
GOLDEN = {"order": ["base", "steady-2", "sharpe", "high-cagr", "decliner", "whipsaw"], "eligible": {"base": True, "steady-2": True, "sharpe": False, "high-cagr": False, "decliner": False, "whipsaw": False},
          "exclusions": {"sharpe": ["min_benchmark_beating_pct", "max_cost_sensitivity"], "high-cagr": ["max_drawdown_floor"], "decliner": ["min_positive_window_pct", "min_benchmark_beating_pct"],
                         "whipsaw": ["min_positive_window_pct", "min_benchmark_beating_pct", "max_fragility"]},
          "finalists": ["base", "steady-2"], "result_hash": "62356305e5b530717865caa1303c4fdf82b00e23bdd8a917ffa62b3af2eb1b34", "score_base": 0.994504, "score_steady": 0.994023}


def quarter(d: date) -> int:
    return (d.year - 2023) * 4 + (d.month - 1) // 3


def month_index(d: date) -> int:
    return (d.year - 2023) * 12 + d.month - 1


def market():
    m = FL.Market(SYMS, start=date(2023, 1, 3), end=date(2026, 3, 31), vol=0.0)
    for s in START:
        p = START[s]
        for i, d in enumerate(m.days):
            if s in SLOPES:
                p += SLOPES[s][quarter(d)] + (ZIG[s] if i % 2 == 0 else -ZIG[s])
            elif s == "AAA":
                p += 0.5 if month_index(d) % 2 == 0 else -0.4
            else:
                p += 0.5 if month_index(d) % 2 == 1 else -0.4
            p = round(p, 4)
            o = round(p - 0.05, 4)
            m.set_bar(s, d, o, round(p + 0.1, 4), round(o - 0.1, 4), p, 1_000_000.0)
    return m


def make_lab():
    m = market()
    lb = FL.FLab(m)
    lb.market = m
    return lb


def rotation_config(lab, **over):
    return S.RotationStore(Path(lab.path)).create_config("camp", {**BASE, **over}, NOW)


def run(lab, **over):
    cfg = over.pop("cfg", None) or rotation_config(lab)
    body = {"config_id": cfg["config_id"], "config_hash": cfg["config_hash"], "universe": {"source": "CUSTOM", "symbols": ALL}, **BODY, **over}
    return CE.run_campaign(ModelCampaignStore(Path(lab.path)), body, now=NOW, path=Path(lab.path), cache=FC.BarCache(), fetch_fn=lab.market.fetch, client=object())


@pytest.fixture(scope="module")
def golden():
    """ONE golden campaign per module (≈ 1 minute): every test below reads it; the lab stays open for storage checks."""
    lab = make_lab()
    out = run(lab)
    return {"lab": lab, "out": out}


def by_label(out):
    return {r["label"]: r for r in out["leaderboard"]}


# ==================================================================================================================================
# GOLDEN campaign: eligibility, ranking and finalists follow from the designed price properties; hash pinned
# ==================================================================================================================================

def test_golden_campaign_highest_cagr_does_not_win(golden):
    out = golden["out"]
    r, rows = out["run"], by_label(out)
    assert r["status"] == "COMPLETED" and r["n_candidates"] == 6 and r["n_windows"] == 5 and r["n_rejected"] == 0 and r["n_eligible"] == 2 and r["n_finalists"] == 2
    assert [x["label"] for x in out["leaderboard"]] == GOLDEN["order"] and [x["rank"] for x in out["leaderboard"]] == [1, 2, 3, 4, 5, 6]
    assert {k: v["eligible"] for k, v in rows.items()} == GOLDEN["eligible"]
    for lab_, gates in GOLDEN["exclusions"].items():
        assert [e["gate"] for e in rows[lab_]["exclusions"]] == gates and all(e["limit"] is not None for e in rows[lab_]["exclusions"])
    assert rows["base"]["exclusions"] == [] and rows["steady-2"]["exclusions"] == []
    # designed properties
    assert rows["high-cagr"]["median_oos_cagr"] == max(x["median_oos_cagr"] for x in out["leaderboard"])                # the CAGR leader …
    assert rows["high-cagr"]["worst_oos_max_drawdown"] < float(C.GATE_DEFAULTS["max_drawdown_floor"]) and rows["high-cagr"]["rank"] == 4     # … is excluded, not #1
    assert rows["sharpe"]["median_oos_sharpe"] == max(x["median_oos_sharpe"] for x in out["leaderboard"])               # the Sharpe leader …
    assert rows["sharpe"]["cost_sensitivity"] > float(C.GATE_DEFAULTS["max_cost_sensitivity"]) and rows["sharpe"]["benchmark_beating_pct"] == 0.0   # … is cost-fragile and never beats SPY
    assert rows["whipsaw"]["fragility_ratio"] > float(C.GATE_DEFAULTS["max_fragility"]) and all(x["fragility_ratio"] < 0.01 for k, x in rows.items() if k != "whipsaw")
    assert rows["decliner"]["positive_window_pct"] == 0.0 and rows["decliner"]["median_oos_cagr"] < 0
    assert rows["base"]["positive_window_pct"] == 1.0 and rows["base"]["benchmark_beating_pct"] == 1.0 and rows["base"]["worst_oos_max_drawdown"] > -0.01
    assert rows["base"]["robustness_score"] > rows["steady-2"]["robustness_score"] > rows["sharpe"]["robustness_score"]
    assert rows["base"]["robustness_score"] == pytest.approx(GOLDEN["score_base"], abs=1e-6) and rows["steady-2"]["robustness_score"] == pytest.approx(GOLDEN["score_steady"], abs=1e-6)
    assert [f["label"] for f in out["finalists"]] == GOLDEN["finalists"] and [f["finalist_rank"] for f in out["finalists"]] == [1, 2]
    assert all(f["role"] == FINALIST_LABEL == "paper-forward-test candidate" for f in out["finalists"])
    assert r["result_hash"] == GOLDEN["result_hash"]
    # full-history CAGR is informational only: the best full-history CAGR belongs to the base, but the order is not sorted by it
    full = [x["full_history_cagr"] for x in out["leaderboard"]]
    assert full != sorted(full, reverse=True) or rows["high-cagr"]["full_history_cagr"] < rows["sharpe"]["full_history_cagr"]
    assert "survivorship" in r["universe_note"].lower()


def test_16_17_baseline_comparison_and_model_card_completeness(golden):
    out = golden["out"]
    cmp = out["comparison"]
    assert cmp["baseline"]["label"] == "base" and [f["label"] for f in cmp["finalists"]] == ["base", "steady-2"]
    deltas = {d["label"]: d["delta"] for d in cmp["baseline_vs_finalists"]}
    assert deltas["base"]["median_oos_cagr"] == 0 and deltas["steady-2"]["median_oos_cagr"] < 0 and "robustness_score" in deltas["steady-2"]
    assert set(cmp["oos_distribution"]) == {f["config_hash"] for f in out["finalists"]} and all(len(v) == 5 for v in cmp["oos_distribution"].values())
    assert set(cmp["cost_sensitivity"]) == set(cmp["parameter_sensitivity"]) == set(cmp["regimes"]) == {f["config_hash"] for f in out["finalists"]}
    rows = by_label(out)
    assert cmp["pareto"]["return_leader"] == rows["high-cagr"]["config_hash"] and cmp["pareto"]["drawdown_leader"] == rows["sharpe"]["config_hash"]
    assert cmp["pareto"]["lowest_turnover"] == rows["base"]["config_hash"] and "note" in cmp["pareto"] and "survivorship" in cmp["universe_note"].lower()
    cards = {c["label"]: c for c in out["cards"]}
    assert set(cards) == {"base", "steady-2"}
    card = cards["base"]
    for key in ("role", "config_hash", "factor_weights", "portfolio_rules", "historical_interval", "universe", "oos_metrics", "robustness", "weak_regimes", "cost_sensitivity",
                "fragility", "known_limitations", "why_it_qualified", "invalidation_during_paper_forward_test"):
        assert key in card and card[key] not in (None, [], {}), key
    assert card["role"] == FINALIST_LABEL and card["leaderboard_rank"] == 1 and card["factor_weights"]["momentum"] == "0.300000"
    assert card["portfolio_rules"]["excluded_symbols"] == sorted(only("RRR")) and card["historical_interval"]["n_windows"] == 5 and len(card["historical_interval"]["test_windows"]) == 5
    assert "survivorship" in card["universe"]["limitation"].lower() and [w["axis"] for w in card["weak_regimes"]] == ["trend", "vol"]
    assert {g["gate"] for g in card["why_it_qualified"]} == {"min_windows", "max_drawdown_floor", "min_positive_window_pct", "min_benchmark_beating_pct", "max_turnover", "max_fragility", "max_cost_sensitivity"}
    assert len(card["invalidation_during_paper_forward_test"]) >= 5
    text = str(card).lower()
    for banned in ("buy ", "sell ", "order", "approved for live", "deploy"):
        assert banned not in text, banned


def test_8_finalist_count_enforced_and_7_no_finalist_outcome(golden):
    lab = golden["lab"]
    cfg = rotation_config(lab)
    one = run(lab, cfg=cfg, finalist_count=1)
    assert one["run"]["status"] == "COMPLETED" and [f["label"] for f in one["finalists"]] == ["base"] and one["run"]["n_eligible"] == 2
    none = run(lab, cfg=cfg, gates={"min_benchmark_beating_pct": "1.0", "max_drawdown_floor": "-0.001"})       # stricter gates nobody passes
    assert none["run"]["status"] == "NO_FINALIST" and none["finalists"] == [] and none["cards"] == [] and none["run"]["n_finalists"] == 0
    assert len(none["leaderboard"]) == 6 and all(not r["eligible"] for r in none["leaderboard"])                   # full evidence remains visible
    st = ModelCampaignStore(Path(lab.path))
    stored = st.campaign(none["run"]["campaign_id"])
    assert stored["status"] == "NO_FINALIST" and st.finalists(none["run"]["campaign_id"]) == [] and len(st.leaderboard(none["run"]["campaign_id"])) == 6


# ==================================================================================================================================
# 1–3, 20–22: config validation · candidate cap · Stage 4.9 grid reuse · stable hashes · malformed metrics
# ==================================================================================================================================

def test_1_2_3_config_validation_candidate_cap_and_grid_reuse(golden):
    lab = golden["lab"]
    cfg = rotation_config(lab)
    for over, code in (({"finalist_count": 0}, "OUT_OF_RANGE"), ({"finalist_count": 11}, "OUT_OF_RANGE"), ({"max_candidates": 0}, "OUT_OF_RANGE"),
                       ({"gates": {"bogus": 1}}, "INVALID_GATES"), ({"gates": {"max_drawdown_floor": "0.5"}}, "OUT_OF_RANGE"), ({"end_date": "2024-01-01"}, "INVALID_DATE_RANGE"),
                       ({"selection_metric": "LUCK"}, "INVALID_SELECTION_METRIC"), ({"end_date": "2026-04-03"}, "INVALID_DATE_RANGE"), ({"grid": {"dimensions": {"nope": [1]}}}, "INVALID_GRID")):
        with pytest.raises(C.CampaignConfigError) as e:
            run(lab, cfg=cfg, **over)
        assert e.value.code == code, over
    assert C.normalise_gates(None) == {"min_windows": 3, "max_failed_windows": 0, "max_drawdown_floor": "-0.3500", "min_positive_window_pct": "0.5000",
                                       "min_benchmark_beating_pct": "0.4000", "max_turnover": "0.9000", "max_fragility": "0.5000", "max_cost_sensitivity": "0.7500"}
    gen = G.generate(BASE, None, {"dimensions": {"cash_buffer_pct": ["0.05", "0.10", "0.15", "0.20"]}}, 2)                 # the Stage 4.9 grid, capped
    assert len(gen["candidates"]) == 2 and gen["n_discarded"] == 2
    capped = run(lab, cfg=cfg, candidates=None, grid={"dimensions": {"cash_buffer_pct": ["0.05", "0.10", "0.15", "0.20"]}}, max_candidates=2, finalist_count=1)
    assert capped["run"]["n_candidates"] == 2 and capped["run"]["n_discarded"] == 2 and capped["definition"]["grid_spec"] == {"dimensions": {"cash_buffer_pct": ["0.05", "0.10", "0.15", "0.20"]}}
    assert [x["label"] for x in capped["leaderboard"]] == ["base", "cash_buffer_pct=0.10"] or {x["label"] for x in capped["leaderboard"]} == {"base", "cash_buffer_pct=0.10"}
    assert C.LIMITS["max_candidates_cap"] == 100 and C.DEFAULTS["max_candidates"] == 50 and C.DEFAULTS["finalist_count"] == 3


def test_20_21_stable_campaign_hash_and_identical_result_hash(golden):
    lab = golden["lab"]
    cfg = rotation_config(lab)
    a = run(lab, cfg=cfg, finalist_count=1)
    b = run(lab, cfg=cfg, finalist_count=1)
    assert a["run"]["campaign_hash"] == b["run"]["campaign_hash"] and a["run"]["result_hash"] == b["run"]["result_hash"] and a["run"]["data_hash"] == b["run"]["data_hash"]
    assert a["run"]["campaign_hash"] != golden["out"]["run"]["campaign_hash"]                                      # finalist_count is part of the definition
    assert a["run"]["result_hash"] != golden["out"]["run"]["result_hash"] and a["run"]["walkforward_result_hash"] == golden["out"]["run"]["walkforward_result_hash"]
    assert a["run"]["n_cache_hits"] >= 0 and a["run"]["n_evaluations"] == golden["out"]["run"]["n_evaluations"]


def test_22_malformed_metrics_fail_closed_in_gates_and_ranking():
    gates = C.normalise_gates(None)
    ev = {"aggregate": {"n_windows": 5, "n_completed_tests": 5, "worst_oos_max_drawdown": None, "positive_window_pct": None, "benchmark_beating_pct": None, "mean_oos_turnover": None},
          "cost_sensitivity": None, "parameter": None, "fragility_ratio": None}
    fails = {e["gate"] for e in LB.evaluate_gates(ev, gates)}
    assert fails == {"max_drawdown_floor", "min_positive_window_pct", "min_benchmark_beating_pct", "max_cost_sensitivity"}     # missing evidence never passes a gate
    assert LB.fragility_ratio(None) is None and LB.fragility_ratio({"base": {"metrics": {"sharpe": None}}}) is None
    assert LB.fragility_ratio({"base": {"metrics": {"sharpe": 2.0}}, "neighbours": []}) == 0.0
    assert LB.fragility_ratio({"base": {"metrics": {"sharpe": 2.0}}, "neighbours": [{"status": "COMPLETED", "metrics": {"sharpe": 0.5}}, {"status": "INVALID"}]}) == pytest.approx(0.75)
    assert LB.fragility_ratio({"base": {"metrics": {"sharpe": 0.2}}, "neighbours": [{"status": "COMPLETED", "metrics": {"sharpe": -0.3}}]}) == pytest.approx(0.5)     # floor 1.0 on |base|


# ==================================================================================================================================
# 4–6, 9–15, 23–24: deterministic leaderboard, gate pass / exclusions, tie-breakers, insufficient windows, failed Stage 4.9 run
# ==================================================================================================================================

def _ev(label, *, score=0.5, sharpe=1.0, dd=-0.10, cs=0.1, frag=0.0, to=0.2, cagr=0.1, pos=0.8, beat=0.6, windows=5, completed=5, h=None, fragile=False):
    return {"label": label, "config": {**CORE, "excluded_symbols": []}, "config_hash": (h or label[0]) * 64,
            "aggregate": {"n_windows": windows, "n_completed_tests": completed, "median_oos_cagr": cagr, "median_oos_sharpe": sharpe, "median_oos_sortino": sharpe, "worst_window_return": -0.05,
                          "worst_oos_max_drawdown": dd, "positive_window_pct": pos, "benchmark_beating_pct": beat, "median_oos_excess_return": 0.01, "mean_oos_turnover": to,
                          "oos_return_std": 0.05, "stitched_oos": {"cagr": cagr, "total_return": cagr}},
            "cost_sensitivity": cs, "fragility_ratio": frag, "parameter": {"fragile": fragile}, "robustness": {"score": score}, "full_history": {"cagr": cagr * 3}, "oos": [], "cost_rows": [], "regimes": None}


def test_4_5_6_9_10_deterministic_leaderboard_gates_and_no_cagr_ranking():
    gates = C.normalise_gates(None)
    evidence = {e["config_hash"]: e for e in (_ev("a", score=0.6, cagr=0.05), _ev("b", score=0.9, cagr=0.90, dd=-0.50), _ev("c", score=0.7, cagr=0.30),
                                               _ev("d", score=0.95, cagr=0.10, windows=2, completed=2), _ev("e", score=0.8, cagr=0.20, fragile=True))}
    rows = LB.build_rows(evidence, gates, {"c" * 64: 3})
    assert [r["label"] for r in rows] == ["c", "a", "d", "b", "e"]                                 # eligible first by score; then ineligible by score
    assert [r["eligible"] for r in rows] == [True, True, False, False, False]
    assert [e["gate"] for e in rows[2]["exclusions"]] == ["min_windows"] and [e["gate"] for e in rows[3]["exclusions"]] == ["max_drawdown_floor"]
    assert [e["gate"] for e in rows[4]["exclusions"]] == ["max_fragility"] and rows[4]["exclusions"][0]["fragile_flag"] is True
    assert rows[0]["times_selected"] == 3 and rows[1]["times_selected"] == 0
    assert max(evidence.values(), key=lambda e: e["aggregate"]["median_oos_cagr"])["label"] == "b" and rows[0]["label"] != "b"     # highest CAGR never wins by CAGR
    assert LB.build_rows(dict(reversed(list(evidence.items()))), gates, {}) == [dict(r, times_selected=0) for r in rows]      # input order irrelevant
    fins = LB.finalists(rows, 3)
    assert [f["label"] for f in fins] == ["c", "a"] and all(f["role"] == FINALIST_LABEL for f in fins) and LB.finalists(rows, 1)[0]["label"] == "c"


def test_11_12_13_14_15_tie_breakers_in_order():
    gates = C.normalise_gates(None)
    same = dict(score=0.8, sharpe=1.0, dd=-0.10, cs=0.1, frag=0.0, to=0.2)
    def rows_of(*evs):
        return [r["label"] for r in LB.build_rows({e["config_hash"]: e for e in evs}, gates, {})]
    assert rows_of(_ev("a", **{**same, "sharpe": 1.0}), _ev("b", **{**same, "sharpe": 1.5})) == ["b", "a"]                        # Sharpe after score
    assert rows_of(_ev("a", **{**same, "dd": -0.20}), _ev("b", **{**same, "dd": -0.05})) == ["b", "a"]                          # 11: less severe drawdown
    assert rows_of(_ev("a", **{**same, "cs": 0.3}), _ev("b", **{**same, "cs": 0.1})) == ["b", "a"]                              # 12: lower cost sensitivity
    assert rows_of(_ev("a", **{**same, "frag": 0.4}), _ev("b", **{**same, "frag": 0.1})) == ["b", "a"]                          # 13: lower fragility
    assert rows_of(_ev("a", **{**same, "to": 0.5}), _ev("b", **{**same, "to": 0.2})) == ["b", "a"]                              # 14: lower turnover
    assert rows_of(_ev("z", **same, h="f"), _ev("y", **same, h="0")) == ["y", "z"]                                              # 15: config hash
    assert rows_of(_ev("a", **{**same, "score": 0.5}), _ev("b", **{**same, "score": 0.6, "sharpe": 0.1})) == ["b", "a"]        # score before Sharpe


def test_23_24_insufficient_windows_excludes_and_failed_stage_49_replay_is_handled(golden):
    lab = golden["lab"]
    cfg = rotation_config(lab)
    few = run(lab, cfg=cfg, gates={"min_windows": 6})
    assert few["run"]["status"] == "NO_FINALIST" and all([e["gate"] for e in r["exclusions"]][0] == "min_windows" for r in few["leaderboard"] if r["label"] in ("base", "steady-2"))
    gap = market()
    gap.drop("SPY", Dt("2025-02-10"))
    lab2 = FL.FLab(gap)
    lab2.market = gap
    out = run(lab2)
    assert out["run"]["status"] == "FAILED" and out["run"]["failure_code"] == "MISSING_BENCHMARK" and out["leaderboard"] == [] and out["run"]["result_hash"] is None
    assert ModelCampaignStore(Path(lab2.path)).campaign(out["run"]["campaign_id"])["status"] == "FAILED"
    short = run(lab, cfg=cfg, min_train_sessions=500)
    assert short["run"]["status"] == "FAILED" and short["run"]["failure_code"] == "INSUFFICIENT_TRAIN_DATA"


# ==================================================================================================================================
# 18–19, 25–34: limitation · storage · isolation · research-only API and UI
# ==================================================================================================================================

def test_18_19_33_34_limitation_storage_and_research_only_records(golden):
    out, lab = golden["out"], golden["lab"]
    cid = out["run"]["campaign_id"]
    st = ModelCampaignStore(Path(lab.path))
    camp = st.campaign(cid)
    assert camp["status"] == "COMPLETED" and "survivorship" in camp["universe_note"].lower() and camp["definition"]["finalist_label"] == FINALIST_LABEL
    board = st.leaderboard(cid)
    assert [r["label"] for r in board] == GOLDEN["order"] and all("median_oos_cagr" in r and "exclusions" in r for r in board)      # 33: full evidence visible
    assert len(st.candidates(cid)) == 6 and all("aggregate" in c["evidence"] and "cost_rows" in c["evidence"] and "parameter" in c["evidence"] for c in st.candidates(cid))
    fins = st.finalists(cid)
    assert [f["label"] for f in fins] == ["base", "steady-2"] and all(f["role"] == FINALIST_LABEL for f in fins)                      # 34: research records only
    assert {c["label"] for c in st.cards(cid)} == {"base", "steady-2"} and st.metrics(cid)["comparison"]["baseline"]["label"] == "base"
    assert "campaign_ranking" in st.metrics(cid)["conventions"] and "steady_state_turnover" in st.metrics(cid)["conventions"]
    with sqlite3.connect(str(lab.path)) as c:
        for sql in ("UPDATE model_campaigns SET status = 'FAILED'", "DELETE FROM model_campaign_finalists", "UPDATE model_campaign_leaderboard SET rank = 99",
                    "DELETE FROM model_campaign_model_cards", "UPDATE model_campaign_candidates SET label = 'x'", "DELETE FROM model_campaign_metrics"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)
        with pytest.raises(sqlite3.DatabaseError):                                                                                  # a finalist can only carry the research role
            c.execute("INSERT INTO model_campaign_finalists (campaign_id, finalist_rank, config_hash, label, role, robustness_score) VALUES (?, 9, ?, 'x', 'approved for live', 1.0)", (cid, "9" * 64))


def test_19b_fresh_db_migration(tmp_path):
    from database import model_campaign_migrations as M
    db = tmp_path / "mc50.db"
    with sqlite3.connect(str(db)) as c:
        c.execute("PRAGMA user_version = 11")
        for _ in range(3):
            M.run_model_campaign_migrations(c)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 11
        names = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(M.TABLES) <= names and len(M.TABLES) == 6 and not any("alpaca" in n or "paper_" in n for n in names)
        sql = " ".join(s for (s,) in c.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        assert "DROP" not in sql.upper() and "ALTER" not in sql.upper() and FINALIST_LABEL in sql
    assert ModelCampaignStore(db).exists() and ModelCampaignStore(db).campaigns() == []


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_25_26_27_28_29_30_isolation_no_deploy_no_handoff():
    files = list((ROOT / "rotation_campaign").glob("*.py")) + [ROOT / "api" / "routes" / "model_campaign.py", ROOT / "database" / "model_campaign_migrations.py"]
    banned = ("paper", "portfolio", "agents", "ai_explain", "anthropic", "alpaca", "robinhood", "rh_gateway", "requests", "httpx", "research_flow", "notifications",
              "forward", "random", "sklearn", "torch", "numpy", "scipy", "schedule", "threading")
    for f in files:
        hits = [m for m in _imports(f) if any(m == b or m.startswith(b + ".") for b in banned)]
        assert not hits, (f.name, hits)
    src = "".join(f.read_text(encoding="utf-8") for f in files) + (ROOT / "frontend" / "portfolio_campaign.js").read_text(encoding="utf-8")
    for word in ("handoff", "Prepare Paper", "AlpacaOrders", "/api/alpaca", "data-apo-", "MutationObserver", "activate_config", "set_active", "deploy(", "promote(",
                 "approved for live", "trade this", "Scheduler(", "start_if_enabled"):
        assert word not in src, word
    assert "rotation_walkforward" in "".join(_imports(ROOT / "rotation_campaign" / "engine.py")) or any(m.startswith("rotation_walkforward") for m in _imports(ROOT / "rotation_campaign" / "engine.py"))
    assert "def generate" not in (ROOT / "rotation_campaign" / "engine.py").read_text(encoding="utf-8")                        # the Stage 4.9 grid is reused, not rebuilt
    out = H.evaluate({"status": "VALID", "portfolio_source": None}, {"symbol": "RRR", "action": "ADD", "handoff_allowed": 1, "est_qty_diff": 1})
    assert out["eligible"] is False and out["draft"] is None and out["reason"] == "INVALID_SOURCE"
    design = (ROOT / "DESIGN_50_MODEL_CAMPAIGN.md").read_text(encoding="utf-8").lower()
    assert "survivorship" in design and "no_finalist" in design and "paper-forward-test candidate" in design and "not ml" in design
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="pmc-body"' in html and 'src="portfolio_campaign.js"' in html and 'href="portfolio_campaign.css"' in html


API = "/api/model-campaign"


@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from ai_explain import service as AX
    from api.routes import model_campaign as MC
    from api.routes import portfolio as pr
    from api.server import app
    lab = make_lab()
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr(MC, "NOW_FN", lambda: NOW)
    monkeypatch.setattr(MC, "FETCH", {"fetch_fn": lab.market.fetch, "client": object(), "cache": FC.BarCache()})
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


def test_31_32_api_research_only_and_ui_has_no_execution_controls(api):
    from api.routes import model_campaign as MC
    paths = [(sorted(r.methods)[0], r.path) for r in MC.router.routes]
    assert all(not any(w in p.lower() for w in ("order", "trade", "backtest", "paper", "execute", "broker", "handoff", "deploy", "activate")) for _, p in paths)
    assert [p for m, p in paths if m == "POST"] == [f"{API}/run"]
    b = api.get(f"{API}/config").json()
    assert b["finalist_label"] == FINALIST_LABEL and b["ranking"][0] == "eligible first" and "survivorship" in b["universe_note"].lower() and b["gate_defaults"]["min_windows"] == 3
    cfg = rotation_config(api.lab)
    api.lab.market.calls.clear()
    body = {"config_id": cfg["config_id"], "config_hash": cfg["config_hash"], "universe": {"source": "CUSTOM", "symbols": ALL}, **BODY,
            "candidates": CANDS[:1] + CANDS[4:], "finalist_count": 1}                                                             # base + steady-2 + decliner
    r = api.post(f"{API}/run", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["run"]["status"] == "COMPLETED" and out["run"]["n_candidates"] == 3 and out["run"]["market_data_requests"] == 1 and len(api.lab.market.calls) == 1
    assert api.hits == []                                                                                                            # 26–28: no Robinhood, Alpaca or Claude
    assert [f["label"] for f in out["finalists"]] == ["base"] and out["finalists"][0]["role"] == FINALIST_LABEL and len(out["leaderboard"]) == 3
    assert "handoff" not in str(out).lower() and "approved for live" not in str(out).lower()
    cid = out["run"]["campaign_id"]
    assert api.get(f"{API}/runs").json()["runs"][0]["campaign_id"] == cid
    d = api.get(f"{API}/runs/{cid}").json()
    assert d["run"]["status"] == "COMPLETED" and d["n_candidates"] == 3 and d["stability"]["selection_metric"] == "SHARPE"
    lb = api.get(f"{API}/runs/{cid}/leaderboard").json()
    assert [x["label"] for x in lb["leaderboard"]][0] == "base" and lb["gates"]["min_windows"] == 3 and "survivorship" in lb["universe_note"].lower()
    assert api.get(f"{API}/runs/{cid}/finalists").json()["finalists"][0]["label"] == "base"
    assert api.get(f"{API}/runs/{cid}/cards").json()["cards"][0]["role"] == FINALIST_LABEL
    cmpr = api.get(f"{API}/runs/{cid}/comparison").json()
    assert cmpr["comparison"]["baseline"]["label"] == "base" and len(cmpr["candidates"]) == 3
    assert api.get(f"{API}/runs/{'z' * 32}").status_code == 404 and api.get(f"{API}/runs/short/cards").status_code == 404
    assert api.post(f"{API}/run", json={**body, "side": "BUY"}).status_code == 422                                                  # strict body: no order fields exist
    assert api.post(f"{API}/run", json={**body, "finalist_count": 0}).status_code == 422
    assert api.post(f"{API}/run", json={**body, "config_hash": "0" * 64}).status_code == 409
    js = (ROOT / "frontend" / "portfolio_campaign.js").read_text(encoding="utf-8")
    assert js.count("fetch(") == 1 and API in js and "/api/alpaca" not in js
    for word in ("Trade this", "Deploy", "Activate", "Prepare Paper", "Preview", "Confirm Paper"):
        assert word not in js, word
    assert "paper-forward-test candidate" in js.lower() or "finalist_label" in js


def test_performance_note(golden):
    r = golden["out"]["run"]
    assert r["n_evaluations"] > 100 and r["n_cache_hits"] > 0 and float(r["runtime_s"]) < 300
