"""Stage 5.2 — Deterministic Signal Research (signal_research/, database/signal_research_migrations.py, api/routes/signal_research.py,
frontend/portfolio_signal_research.js). Fully offline. Synthetic thirteen-name universe in four sectors: a tech basket with strong
momentum (TAA–TAD), two "bounce" names (TBX, TBY: sawtooth, worst trend / volatility / liquidity), mild health, flat energy, mild
financials, and a SPY crash with alternating moves in 2025-Q2 (TREND_DOWN + HIGH_VOL). The Stage 4.7 / 4.8 / 4.9 / 5.0 / 5.1
machinery is reused unchanged; the only hook is the optional research proposal function of the Stage 4.8 simulator."""
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
from rotation import engine as E
from rotation import handoff as H
from rotation import rules as R
from rotation import snapshots as SN
from rotation import store as S
from rotation import universe as U
from rotation_backtest import simulator as SIM
from rotation_campaign import engine as CE
from rotation_campaign.store import ModelCampaignStore
from rotation_walkforward import engine as WF
from signal_research import CRITERIA_VERSION, FLAGS_VERSION, MAX_VARIANTS, RULES_VERSION
from signal_research import engine as SE
from signal_research import evaluate as EV
from signal_research import regime as RGO
from signal_research import sector as SC
from signal_research import variant_engine as VE
from signal_research import variants as V
from signal_research.store import SignalResearchStore

ROOT = Path(__file__).resolve().parents[1]
D = Decimal
NOW = FL.at(date(2026, 4, 1))
CRASH = (date(2025, 4, 1), date(2025, 6, 30))
SECTORS = {"TAA": "TECH", "TAB": "TECH", "TAC": "TECH", "TAD": "TECH", "TBX": "TECH", "TBY": "TECH", "HAA": "HEALTH", "HAB": "HEALTH", "HAC": "HEALTH", "EAA": "ENERGY", "EAB": "ENERGY",
           "FAA": "FIN", "FAB": "FIN"}


def _crash(d):
    return CRASH[0] <= d <= CRASH[1]


def _tech(up):
    return lambda i, d: ((-2.0 if i % 2 == 0 else 1.0) if _crash(d) else up)


def _mild(up, dn):
    return lambda i, d: ((-dn if i % 2 == 0 else dn / 2) if _crash(d) else up)


def _bounce(i, d):
    return (2.4 if i % 2 == 0 else 0.0) if (i % 100) < 40 else (-1.8 if i % 2 == 0 else 0.0)


SPEC = {"TAA": _tech(0.35), "TAB": _tech(0.30), "TAC": _tech(0.25), "TAD": _tech(0.20), "TBX": _bounce, "TBY": lambda i, d: _bounce(i + 50, d),
        "HAA": _mild(0.10, 0.3), "HAB": _mild(0.09, 0.3), "HAC": _mild(0.08, 0.3), "EAA": _mild(0.05, 0.0), "EAB": _mild(0.04, 0.0), "FAA": _mild(0.08, 0.4), "FAB": _mild(0.07, 0.4),
        "SPY": lambda i, d: ((-10.0 if i % 2 == 0 else 8.0) if _crash(d) else 0.25)}
START = {s: 100.0 for s in SPEC}
START.update(SPY=400.0, TBX=300.0, TBY=300.0)
VOLUME = {s: 1_000_000.0 for s in SPEC}
VOLUME.update(TBX=300_000.0, TBY=300_000.0)
W = {"momentum": "0.30", "trend": "0.25", "relative_strength": "0.25", "volatility": "0.10", "drawdown": "0", "liquidity": "0.10"}
BASE = {"weights": W, "portfolio_size": 4, "exit_rank": 5, "cash_buffer_pct": "0.05", "rebalance_threshold": "0.01", "max_turnover_per_rotation": "1.00", "max_position_weight": "0.50",
        "min_position_weight": "0.10", "min_price": "5.00", "min_avg_dollar_volume": "1000.00", "min_history_sessions": 252}
GATES = {"min_benchmark_beating_pct": "0.0", "max_drawdown_floor": "-0.9", "min_positive_window_pct": "0.0", "max_fragility": "99", "max_cost_sensitivity": "1.0"}
CAPS = ["0.250000", "0.500000"]
GOLDEN = {"n_variants": 19, "n_evaluations": 705, "result_hash": "f75a775317d2a6a3c90933bf0a351333218d304bd764f2dea24a0587dabbb8f9",
          "ablation_order_by_cagr": ["no_relative_strength", "baseline", "no_trend", "no_momentum", "no_liquidity", "no_volatility"],
          "factors": {"momentum": "FACTOR_HELPFUL", "trend": "FACTOR_NEUTRAL", "relative_strength": "FACTOR_NEUTRAL", "volatility": "FACTOR_HELPFUL", "liquidity": "FACTOR_HELPFUL"},
          "bench": {"EW_total": 0.0826, "BH_total": 0.0822, "SPY_total": 0.0177, "EW_dd": -0.0507, "SPY_dd": -0.1255},
          "baseline": {"total": 0.1079, "cagr": 0.0866, "dd": -0.0668, "top_sector_share": 0.84, "max_sector_weight": 0.95, "pct_beating_ew": 0.6, "criteria_passed": 9},
          "sector_neutral": {"total": 0.0873, "cagr": 0.0702, "dd": -0.0325, "top_sector_share": 0.34, "max_sector_weight": 0.24, "pct_beating_ew": 0.8, "criteria_passed": 9},
          "no_volatility": {"total": 0.0680, "criteria_passed": 4}, "momentum_only": {"total": 0.1386}, "simple_risk_off": {"total": 0.1040, "dd": -0.0440, "exposure": 0.67},
          "binary_trend_filter": {"dd": -0.0516, "exposure": 0.71}, "sector_cap_0.25": {"max_sector_weight": 0.24, "cap_changed": 15}, "sector_cap_0.50": {"max_sector_weight": 0.47, "cap_changed": 10, "criteria_passed": 10},
          "sn_ro": {"dd": -0.0239, "sharpe": 5.36}}


def market():
    m = FL.Market(tuple(SPEC), start=date(2023, 1, 3), end=date(2026, 3, 31), vol=0.0)
    for s, f in SPEC.items():
        p = START[s]
        for i, d in enumerate(m.days):
            p = round(max(p + f(i, d), 5.0), 4)
            o = round(p - 0.05, 4)
            m.set_bar(s, d, o, round(p + 0.1, 4), round(o - 0.1, 4), p, VOLUME[s])
    return m


def make_lab():
    m = market()
    lb = FL.FLab(m)
    lb.market = m
    return lb


def run_campaign(lab):
    st = S.RotationStore(Path(lab.path))
    cfg = st.create_config("sr", BASE, NOW)
    body = {"config_id": cfg["config_id"], "config_hash": cfg["config_hash"], "universe": {"source": "CUSTOM", "symbols": sorted(SECTORS)}, "start_date": "2024-07-01", "end_date": "2026-03-31",
            "train_months": 6, "test_months": 3, "step_months": 3, "rebalance_frequency": "MONTHLY", "selection_metric": "SHARPE",
            "candidates": [{"label": "size3", **BASE, "portfolio_size": 3, "exit_rank": 4}], "max_candidates": 5, "finalist_count": 1, "gates": GATES}
    return CE.run_campaign(ModelCampaignStore(Path(lab.path)), body, now=NOW, path=Path(lab.path), cache=FC.BarCache(), fetch_fn=lab.market.fetch, client=object())


def run_research(lab, campaign_id, **over):
    return SE.run_research(SignalResearchStore(Path(lab.path)), {"campaign_id": campaign_id, "sector_map": SECTORS, "caps": CAPS, **over}, now=NOW, path=Path(lab.path),
                           cache=FC.BarCache(), fetch_fn=lab.market.fetch, client=object())


def base_config():
    return {**BASE, "benchmark": "SPY", "excluded_symbols": [], "max_snapshot_age_min": 30}


def series_of(m):
    return {s: BarSeries(s, [m.rows[s][d] for d in sorted(m.rows[s])]) for s in m.rows}


@pytest.fixture(scope="module")
def golden():
    lab = make_lab()
    camp = run_campaign(lab)
    out = run_research(lab, camp["run"]["campaign_id"])
    return {"lab": lab, "camp": camp, "out": out, "by": {v["label"]: v for v in out["variants"]}}


# ==================================================================================================================================
# GOLDEN: higher CAGR alone does NOT define the winner
# ==================================================================================================================================

def test_golden_higher_cagr_does_not_define_the_winner(golden):
    out, by = golden["out"], golden["by"]
    r = out["run"]
    assert r["status"] == "COMPLETED" and r["n_windows"] == 5 and r["n_variants"] == GOLDEN["n_variants"] == len(out["variants"]) <= MAX_VARIANTS and r["n_evaluations"] == GOLDEN["n_evaluations"]
    assert r["market_data_requests"] == 1 and r["campaign_id"] == golden["camp"]["run"]["campaign_id"] and r["rules_version"] == RULES_VERSION == "sr_v1" and r["flags_version"] == FLAGS_VERSION == "rf_v1"
    assert r["run_flags"] == ["NO_SIGNAL_IMPROVEMENT"] and not any(v["criteria"]["met"] for v in out["variants"])
    assert [v["label"] for v in out["variants"]][:6] == ["baseline", "no_momentum", "no_trend", "no_relative_strength", "no_volatility", "no_liquidity"]
    assert [v["label"] for v in out["variants"]][12:] == ["sector_neutral", "sector_cap_0.25", "sector_cap_0.50", "simple_risk_off", "binary_trend_filter", "sector_neutral+simple_risk_off", "sector_cap_0.30+simple_risk_off"]
    b = {x["benchmark"]: x for x in out["benchmarks"] if x["transaction_cost_bps"] in ("5.0000", "0.0000") and (x["benchmark"] == "SPY" or x["transaction_cost_bps"] == "5.0000")}
    assert b["EW_REBALANCED"]["metrics"]["total_return"] == pytest.approx(GOLDEN["bench"]["EW_total"], abs=1e-4) and b["BUY_HOLD"]["metrics"]["total_return"] == pytest.approx(GOLDEN["bench"]["BH_total"], abs=1e-4)
    assert b["SPY"]["metrics"]["total_return"] == pytest.approx(GOLDEN["bench"]["SPY_total"], abs=1e-4) and b["EW_REBALANCED"]["metrics"]["max_drawdown"] == pytest.approx(GOLDEN["bench"]["EW_dd"], abs=1e-4)
    base, sn = by["baseline"], by["sector_neutral"]
    bs, ss = base["summary"], sn["summary"]
    # the tech basket "looks strong": highest CAGR of the serious variants and it beats every benchmark …
    assert bs["stitched"]["strategy"]["total_return"] == pytest.approx(GOLDEN["baseline"]["total"], abs=1e-4) and bs["stitched"]["strategy"]["cagr"] == pytest.approx(GOLDEN["baseline"]["cagr"], abs=1e-4)
    assert bs["stitched"]["strategy"]["total_return"] > bs["stitched"]["EW_REBALANCED"]["total_return"] > bs["stitched"]["SPY"]["total_return"]
    assert bs["stitched"]["strategy"]["cagr"] > ss["stitched"]["strategy"]["cagr"] == pytest.approx(GOLDEN["sector_neutral"]["cagr"], abs=1e-4)
    # … but it is a single-sector book with a deeper drawdown and it fails the predeclared criteria
    assert bs["attribution"]["sector_concentration"]["top_sector_share"] == pytest.approx(GOLDEN["baseline"]["top_sector_share"], abs=0.006) and bs["attribution"]["sector_concentration"]["max_sector_weight"] == pytest.approx(0.95)
    assert bs["stitched"]["strategy"]["max_drawdown"] == pytest.approx(GOLDEN["baseline"]["dd"], abs=1e-4) and base["criteria"]["passed"] == GOLDEN["baseline"]["criteria_passed"] and not base["criteria"]["met"]
    assert base["research_flags"] == [] and "SECTOR_CONCENTRATION_HIGH" in base["flags"]
    # sector-neutral ranking: lower CAGR, better drawdown and risk-adjusted profile, concentration cut — the research flags say so
    assert ss["stitched"]["strategy"]["max_drawdown"] == pytest.approx(GOLDEN["sector_neutral"]["dd"], abs=1e-4) and ss["stitched"]["strategy"]["sharpe"] > bs["stitched"]["strategy"]["sharpe"]
    assert ss["attribution"]["sector_concentration"]["top_sector_share"] == pytest.approx(GOLDEN["sector_neutral"]["top_sector_share"], abs=0.006) and ss["attribution"]["sector_concentration"]["max_sector_weight"] == pytest.approx(0.24, abs=0.006)
    assert ss["windows_summary"]["pct_beating_EW_REBALANCED"] == pytest.approx(GOLDEN["sector_neutral"]["pct_beating_ew"]) and sn["criteria"]["passed"] == GOLDEN["sector_neutral"]["criteria_passed"]
    assert sn["research_flags"] == ["CONCENTRATION_REDUCED", "DRAWDOWN_IMPROVED", "SECTOR_NEUTRALITY_HELPFUL"]
    # the highest raw CAGR is a single-factor control with 93 % of positive P&L in one sector — not a winner
    mo = by["momentum_only"]
    assert mo["summary"]["stitched"]["strategy"]["total_return"] == pytest.approx(GOLDEN["momentum_only"]["total"], abs=1e-4) and mo["family"] == "control" and not mo["criteria"]["met"]
    assert mo["summary"]["attribution"]["sector_concentration"]["top_sector_share"] > 0.9
    # one ablation clearly worsens performance
    nv = by["no_volatility"]
    assert nv["summary"]["stitched"]["strategy"]["total_return"] == pytest.approx(GOLDEN["no_volatility"]["total"], abs=1e-4) and nv["criteria"]["passed"] == GOLDEN["no_volatility"]["criteria_passed"]
    assert "FACTOR_HELPFUL" in nv["research_flags"] and "EW_STILL_DOMINANT" in nv["research_flags"]
    # factor-ablation ordering and verdicts
    abl = sorted([v for v in out["variants"] if v["family"] in ("baseline", "ablation")], key=lambda v: -v["summary"]["stitched"]["strategy"]["cagr"])
    assert [v["label"] for v in abl] == GOLDEN["ablation_order_by_cagr"]
    assert {f["factor"]: f["verdict"] for f in out["factors"]} == GOLDEN["factors"]
    # regime overlay: exposure scaled down in the crash, drawdown reduced
    ro, bt = by["simple_risk_off"], by["binary_trend_filter"]
    assert ro["summary"]["construction"]["mean_exposure"] == pytest.approx(GOLDEN["simple_risk_off"]["exposure"], abs=0.006) and bt["summary"]["construction"]["mean_exposure"] == pytest.approx(GOLDEN["binary_trend_filter"]["exposure"], abs=0.006)
    assert ro["summary"]["stitched"]["strategy"]["max_drawdown"] == pytest.approx(GOLDEN["simple_risk_off"]["dd"], abs=1e-4) and ro["summary"]["stitched"]["strategy"]["max_drawdown"] > bs["stitched"]["strategy"]["max_drawdown"]
    assert "DRAWDOWN_IMPROVED" in ro["research_flags"] and "REGIME_OVERLAY_HELPFUL" in ro["research_flags"] and "REGIME_OVERLAY_OVERDEFENSIVE" not in ro["research_flags"]
    rt = {g["label"]: g for g in out["regime_tests"]}
    assert rt["simple_risk_off"]["counterpart_label"] == "baseline" and rt["simple_risk_off"]["exposure_by_regime"]["TREND_DOWN"] < 0.6 < 0.85 < rt["simple_risk_off"]["exposure_by_regime"]["TREND_UP"]
    assert rt["sector_neutral+simple_risk_off"]["counterpart_label"] == "sector_neutral" and rt["sector_cap_0.30+simple_risk_off"]["counterpart_label"] == "sector_cap_0.30"
    # sector caps bind and are respected
    st_ = {g["label"]: g for g in out["sector_tests"]}
    assert st_["sector_cap_0.25"]["max_sector_weight_observed"] == pytest.approx(0.2375, abs=1e-4) and st_["sector_cap_0.25"]["cap_changed_selection"] == GOLDEN["sector_cap_0.25"]["cap_changed"]
    assert st_["sector_cap_0.50"]["max_sector_weight_observed"] == pytest.approx(0.475, abs=1e-4) and st_["sector_cap_0.50"]["cap_changed_selection"] == GOLDEN["sector_cap_0.50"]["cap_changed"]
    assert all(g["cap_infeasible"] == 0 for g in out["sector_tests"]) and "SECTOR_CAP_BINDING" in by["sector_cap_0.25"]["research_flags"] and by["sector_cap_0.50"]["criteria"]["passed"] == GOLDEN["sector_cap_0.50"]["criteria_passed"]
    # combined variants: best risk profile of all, still not a winner (return sacrificed, criteria not met)
    snro = by["sector_neutral+simple_risk_off"]
    assert snro["summary"]["stitched"]["strategy"]["max_drawdown"] == pytest.approx(GOLDEN["sn_ro"]["dd"], abs=1e-4) and snro["summary"]["stitched"]["strategy"]["sharpe"] == pytest.approx(GOLDEN["sn_ro"]["sharpe"], abs=0.006)
    assert "RETURN_SACRIFICED_FOR_RISK" in snro["research_flags"] and not snro["criteria"]["met"] and [c["label"] for c in out["combinations"]] == ["sector_neutral+simple_risk_off", "sector_cap_0.30+simple_risk_off"]
    # scorecards: the Stage 5.1 card is attached to every variant with the churn-based selection row
    for v in out["variants"]:
        assert len(v["scorecard"]) == 12 and v["scorecard"][-1]["check"] == "selection stability" and v["scorecard"][-1]["value"]["basis"].startswith("holdings churn")
        assert v["criteria"]["version"] == CRITERIA_VERSION and v["criteria"]["n"] == 11 and len(v["config"]["weights"]) == 6
    assert base["scorecard"][-1]["verdict"] == "PASS" and {c["check"]: c["verdict"] for c in sn["scorecard"]}["sector concentration"] == "PASS"
    assert r["result_hash"] == GOLDEN["result_hash"]


# ==================================================================================================================================
# 1–8: ablation weights
# ==================================================================================================================================

def test_1_2_3_4_5_6_7_8_ablation_rescaling_and_controls(golden):
    by = golden["by"]
    for v in golden["out"]["variants"]:
        w = {k: D(x) for k, x in v["config"]["weights"].items()}
        assert sum(w.values()) == D("1.000000") and all(x == x.quantize(D("0.000001")) and x >= 0 for x in w.values()) and w["drawdown"] == 0         # 1, 2
    # exact remove-one weights (Stage 4.9 rescale: ROUND_DOWN, residual to the largest remaining weight)
    assert by["no_momentum"]["config"]["weights"] == {"momentum": "0.000000", "trend": "0.357144", "relative_strength": "0.357142", "volatility": "0.142857", "drawdown": "0.000000", "liquidity": "0.142857"}
    assert by["no_trend"]["config"]["weights"]["trend"] == "0.000000" and D(by["no_trend"]["config"]["weights"]["momentum"]) == D("0.400001")                                       # 4
    assert by["no_relative_strength"]["config"]["weights"]["relative_strength"] == "0.000000" and D(by["no_relative_strength"]["config"]["weights"]["momentum"]) == D("0.400001")   # 5
    assert by["no_volatility"]["config"]["weights"]["volatility"] == "0.000000" and D(by["no_volatility"]["config"]["weights"]["momentum"]) == D("0.333335")                        # 6
    assert by["no_liquidity"]["config"]["weights"]["liquidity"] == "0.000000" and D(by["no_liquidity"]["config"]["weights"]["trend"]) == D("0.277777")                              # 7
    assert by["momentum_only"]["config"]["weights"]["momentum"] == "1.000000" and by["trend_only"]["config"]["weights"]["trend"] == "1.000000"                                        # 8
    assert by["momentum+trend"]["config"]["weights"] == {"momentum": "0.545455", "trend": "0.454545", "relative_strength": "0.000000", "volatility": "0.000000", "drawdown": "0.000000", "liquidity": "0.000000"}
    assert V.keep_weights(W, ("momentum", "trend")) == V.keep_weights(dict(reversed(list(W.items()))), ("trend", "momentum"))                                                            # deterministic
    assert V.remove_factor(W, "momentum") == V.remove_factor(W, "momentum")
    with pytest.raises(V.VariantError):
        V.remove_factor(W, "drawdown")
    with pytest.raises(V.VariantError):
        V.keep_weights(W, ("momentum", "momentum"))
    assert by["no_momentum"]["family"] == "ablation" and by["momentum_only"]["family"] == "control" and by["baseline"]["config"]["weights"] == V.normalised_weights(W)


# ==================================================================================================================================
# 9–13: sector map, sector-neutral ranking, caps
# ==================================================================================================================================

def test_9_10_11_12_13_sector_rules(golden):
    h = SC.sector_map_hash(SECTORS)
    assert len(h) == 64 and h == SC.sector_map_hash(dict(reversed(list(SECTORS.items())))) == SC.sector_map_hash({k.lower(): v for k, v in SECTORS.items()})                       # 9
    assert h != SC.sector_map_hash({**SECTORS, "TAA": "HEALTH"}) and golden["out"]["run"]["sector_map_hash"] == h and golden["by"]["baseline"]["research"]["sector_map_hash"] == h
    entries = {"TAA": {"composite": D(90), "relative_strength": D(90), "liquidity": D(50)}, "TAB": {"composite": D(80), "relative_strength": D(80), "liquidity": D(50)},
               "TAC": {"composite": D(70), "relative_strength": D(70), "liquidity": D(50)}, "HAA": {"composite": D(60), "relative_strength": D(60), "liquidity": D(50)},
               "HAB": {"composite": D(40), "relative_strength": D(40), "liquidity": D(50)}, "EAA": {"composite": D(30), "relative_strength": D(30), "liquidity": D(50)}}
    pct = SC.within_sector_percentiles(entries, SECTORS)
    assert pct == {"TAA": D(100), "TAB": D(50), "TAC": D(0), "HAA": D(100), "HAB": D(0), "EAA": D(50)}                                                                                # 10
    assert SC.sector_neutral_rank(entries, SECTORS) == ["TAA", "HAA", "TAB", "EAA", "TAC", "HAB"] and R.rank_symbols(entries) == ["TAA", "TAB", "TAC", "HAA", "HAB", "EAA"]            # 11
    ranked = SC.sector_neutral_rank(entries, SECTORS)
    assert SC.sector_neutral_rank(dict(reversed(list(entries.items()))), SECTORS) == ranked
    ew = R.equal_weight(4, D("0.05"))
    assert SC.cap_count(D("0.25"), ew) == 1 and SC.cap_count(D("0.50"), ew) == 2 and SC.validate_cap(D("0.25"), ew, 4, SECTORS) == 1                                                  # 12
    sel = SC.select_capped(R.rank_symbols(entries), [], 4, 5, SECTORS, 1)
    assert sel["selected"] == ["TAA", "HAA", "EAA"] and sel["capped"] == ["TAB", "TAC", "HAB"] and sel["infeasible"] is True and sel["reasons"]["HAA"] == SC.TOP_N_SECTOR_CAP           # 13
    sel2 = SC.select_capped(R.rank_symbols(entries), ["TAB", "TAC"], 4, 5, SECTORS, 2)
    assert sel2["selected"] == ["TAB", "TAC", "HAA", "HAB"] and sel2["capped"] == ["TAA"] and sel2["exits"] == {} and sel2["infeasible"] is False          # holdings retained first
    sel3 = SC.select_capped(R.rank_symbols(entries), ["TAB", "TAC", "HAB"], 4, 5, SECTORS, 1)
    assert sel3["selected"] == ["TAB", "HAB", "EAA"] and sel3["exits"]["TAC"] == SC.EXIT_SECTOR_CAP and sel3["infeasible"] is True
    with pytest.raises(SC.SectorRuleError) as e:
        SC.validate_cap(D("0.20"), ew, 4, SECTORS)
    assert e.value.code == "SECTOR_CAP_BELOW_EQUAL_WEIGHT"
    with pytest.raises(SC.SectorRuleError) as e:
        SC.validate_cap(D("0.25"), ew, 5, {"A": "X", "B": "X", "C": "Y"})
    assert e.value.code == "SECTOR_CAP_INFEASIBLE"
    with pytest.raises((V.VariantError, SC.SectorRuleError)):
        V.build_variants(base_config(), SECTORS, families=("sector",), caps=("0.200000",))
    # every replayed proposal of a capped variant respects the cap
    for label, cap in (("sector_cap_0.25", D("0.25")), ("sector_cap_0.50", D("0.50"))):
        s = golden["by"][label]["summary"]
        assert s["attribution"]["sector_concentration"]["max_sector_weight"] <= float(cap) and s["construction"]["cap_infeasible"] == 0


def test_13b_cap_infeasible_proposal_fails_closed():
    m = market()
    series = series_of(m)
    uni = U.resolve_universe("CUSTOM", None, sorted(SECTORS))
    two = {s: ("TECH" if s.startswith("T") else "OTHER") for s in SECTORS}            # two sectors, one name each → cannot fill four slots
    v = V.make_variant("cap", "sector", base_config(), V.rules(V.GLOBAL_RANK, "0.250000", V.NO_OVERLAY, SC.sector_map_hash(SECTORS)), SECTORS)
    cfg = {"config_id": v["config_hash"][:32], "config_hash": v["config_hash"], "config": v["config"]}
    T = date(2025, 2, 3)
    snap = SN.normalise(SN.LOCAL_SIMULATOR, None, D("100000.00"), [], {"historical_backtest": True}, SN.OK)
    rr = VE.compute_variant(cfg, uni, SIM.truncate_all(series, T), snap, T, now=NOW, conflicts=set(), research=v["research"], sector_map=two)
    assert rr.run["status"] == SC.SECTOR_CAP_INFEASIBLE and rr.run["n_selected"] == 2 and rr.run["status"] not in SIM.TRADABLE_STATUSES
    ok = VE.compute_variant(cfg, uni, SIM.truncate_all(series, T), snap, T, now=NOW, conflicts=set(), research=v["research"], sector_map=SECTORS)
    assert ok.run["status"] == E.VALID and len({SC.sector_of(t["symbol"], SECTORS) for t in ok.targets}) == 4 and all(t["target_weight"] <= D("0.25") for t in ok.targets)


# ==================================================================================================================================
# 14–18: regime overlay
# ==================================================================================================================================

def test_14_15_16_17_18_regime_overlay_point_in_time_no_leverage_no_shorting():
    m = market()
    spy = series_of(m)["SPY"]
    T = date(2025, 6, 2)
    lab = RGO.label_at(spy, T)
    assert lab == {"trend": "TREND_DOWN", "vol": "HIGH_VOL"}
    assert RGO.label_at(spy, date(2025, 2, 3)) == {"trend": "TREND_UP", "vol": "LOW_VOL"}
    m2 = market()
    m2.mutate_after(T)                                                                     # 14: bars after T are irrelevant
    assert RGO.label_at(series_of(m2)["SPY"], T) == lab and RGO.exposure_at(series_of(m2)["SPY"], T, V.SCHEDULES[V.SIMPLE_RISK_OFF])[0] == D("0.250000")
    assert RGO.exposure_at(spy, date(2025, 2, 3), V.SCHEDULES[V.SIMPLE_RISK_OFF])[0] == D("1.000000") and RGO.exposure_at(spy, T, V.SCHEDULES[V.BINARY_TREND_FILTER])[0] == D("0.500000")
    assert RGO.label_at(spy, date(2023, 3, 1))["trend"] == "UNKNOWN" and RGO.exposure_for({"trend": "UNKNOWN", "vol": "LOW_VOL"}, V.SCHEDULES[V.SIMPLE_RISK_OFF]) == D("1.000000")
    assert RGO.label_at(spy, date(2025, 5, 3)) == {"trend": "UNKNOWN", "vol": "UNKNOWN"}                                                                      # no bar (Saturday) → no label
    weights, _ = R.allocate(["TAA", "HAA", "EAA", "FAA"], 4, D("0.05"))
    scaled, cash = RGO.scale_weights(weights, D("0.25"))                                                                                                    # 15, 16
    assert set(scaled.values()) == {D("0.059375")} and cash == D("0.762500") and sum(scaled.values()) + cash == D("1.000000")
    full, cash1 = RGO.scale_weights(weights, D("1.000000"))
    assert full == weights and cash1 == D("0.050000")
    with pytest.raises(ValueError):
        RGO.scale_weights(weights, D("1.5"))                                                                                                                # 17: no leverage
    with pytest.raises(ValueError):
        RGO.exposure_for({"trend": "TREND_UP", "vol": "LOW_VOL"}, {"TREND_UP|LOW_VOL": "-0.1"})                                                             # 18: no shorting
    for bad in ({"TREND_UP|LOW_VOL": "1.2", "TREND_UP|HIGH_VOL": "1", "TREND_DOWN|LOW_VOL": "1", "TREND_DOWN|HIGH_VOL": "1"}, {"TREND_UP|LOW_VOL": "1"}):
        with pytest.raises(V.VariantError):
            V.rules(V.GLOBAL_RANK, None, V.SIMPLE_RISK_OFF, SC.sector_map_hash(SECTORS), bad)
    # the overlay is applied after ranking: same targets, scaled weights, more cash, identical ranking
    series = series_of(m)
    uni = U.resolve_universe("CUSTOM", None, sorted(SECTORS))
    smh = SC.sector_map_hash(SECTORS)
    plain = V.make_variant("p", "baseline", base_config(), V.rules(sector_map_hash=smh), SECTORS)
    over = V.make_variant("o", "regime", base_config(), V.rules(V.GLOBAL_RANK, None, V.SIMPLE_RISK_OFF, smh), SECTORS)
    snap = SN.normalise(SN.LOCAL_SIMULATOR, None, D("100000.00"), [], {"historical_backtest": True}, SN.OK)
    bars = SIM.truncate_all(series, T)
    a = VE.compute_variant({"config_id": "a" * 32, "config_hash": plain["config_hash"], "config": plain["config"]}, uni, bars, snap, T, now=NOW, conflicts=set(), research=plain["research"], sector_map=SECTORS)
    b = VE.compute_variant({"config_id": "b" * 32, "config_hash": over["config_hash"], "config": over["config"]}, uni, bars, snap, T, now=NOW, conflicts=set(), research=over["research"], sector_map=SECTORS)
    assert [t["symbol"] for t in a.targets] == [t["symbol"] for t in b.targets] and [t["rank"] for t in a.targets] == [t["rank"] for t in b.targets]
    assert all(tb["target_weight"] == R.q(ta["target_weight"] * D("0.25")) for ta, tb in zip(a.targets, b.targets)) and b.run["target_cash_weight"] > a.run["target_cash_weight"] == D("0.050000")
    assert b.run["research"]["exposure"] == D("0.250000") and b.run["research"]["regime"] == lab and all(i["handoff_allowed"] == 0 for i in a.items + b.items)


# ==================================================================================================================================
# 19–21: combined variants, the cap, identical hashes; the baseline variant reproduces production
# ==================================================================================================================================

def test_19_20_21_combined_deterministic_cap_and_hashes(golden):
    lab = golden["lab"]
    cid = golden["camp"]["run"]["campaign_id"]
    a = run_research(lab, cid, families=["combined"])
    b = run_research(lab, cid, families=["combined"])
    assert [v["label"] for v in a["variants"]] == ["baseline", "sector_neutral+simple_risk_off", "sector_cap_0.30+simple_risk_off"]
    assert a["run"]["result_hash"] == b["run"]["result_hash"] and a["run"]["run_hash"] == b["run"]["run_hash"] and [v["config_hash"] for v in a["variants"]] == [v["config_hash"] for v in b["variants"]]   # 19, 21
    assert a["run"]["run_id"] != b["run"]["run_id"] and a["variants"][1]["research"]["ranking"] == V.SECTOR_NEUTRAL_RANK and a["variants"][1]["research"]["regime_overlay"] == V.SIMPLE_RISK_OFF
    assert a["variants"][1]["config_hash"] == golden["by"]["sector_neutral+simple_risk_off"]["config_hash"] and a["variants"][1]["summary"]["stitched"]["strategy"]["total_return"] == golden["by"]["sector_neutral+simple_risk_off"]["summary"]["stitched"]["strategy"]["total_return"]
    assert MAX_VARIANTS == 20 and len(V.build_variants(base_config(), SECTORS)) == 20                                                                                                    # 20
    with pytest.raises(V.VariantError) as e:
        V.build_variants(base_config(), SECTORS, caps=("0.250000", "0.300000", "0.350000", "0.500000"))
    assert e.value.code == "TOO_MANY_VARIANTS"
    with pytest.raises(SE.ResearchError) as e:
        run_research(lab, cid, caps=["0.250000", "0.300000", "0.350000", "0.500000"])
    assert e.value.code == "TOO_MANY_VARIANTS"
    # the baseline variant through the research hook == the production Stage 4.8 replay (handoff_allowed is the only proposal difference)
    series = series_of(lab.market)
    uni = U.resolve_universe("CUSTOM", None, sorted(SECTORS))
    basev = golden["by"]["baseline"]
    rev = VE.ResearchEvaluator(uni, series, NOW, SECTORS)
    r1, m1 = rev.evaluate({"config_hash": basev["config_hash"], "config": basev["config"], "research": basev["research"]}, "2025-01-01", "2025-03-31", "MONTHLY", "100000.00", "5.0000", "5.0000")
    r0, m0 = WF.Evaluator(uni, series, NOW).evaluate({"config_hash": basev["config_hash"], "config": basev["config"]}, "2025-01-01", "2025-03-31", "MONTHLY", "100000.00", "5.0000", "5.0000")
    assert m1["total_return"] == m0["total_return"] and [t["symbol"] for rb in r1.rebalances for t in rb["proposal"]["targets"]] == [t["symbol"] for rb in r0.rebalances for t in rb["proposal"]["targets"]]
    assert [e["equity"] for e in r1.equity] == [e["equity"] for e in r0.equity] and rev.research_evaluations == 1


# ==================================================================================================================================
# 22–28: benchmarks, costs, concentration, drawdown, regimes
# ==================================================================================================================================

def test_22_23_24_25_26_27_28_benchmarks_costs_concentration_drawdown_regimes(golden):
    out, by = golden["out"], golden["by"]
    base = by["baseline"]
    cmp = [c for c in out["benchmark_comparisons"] if c["config_hash"] == base["config_hash"]]
    assert {c["benchmark"] for c in cmp} == {"EW_REBALANCED", "BUY_HOLD", "SPY"} and sum(1 for c in cmp if c["benchmark"] == "SPY") == 1 and sum(1 for c in cmp if c["benchmark"] == "EW_REBALANCED") == 4   # 22–24
    for c in cmp:
        assert c["excess"] == pytest.approx(c["strategy_total_return"] - c["benchmark_total_return"])
    ew5 = next(c for c in cmp if c["benchmark"] == "EW_REBALANCED" and c["transaction_cost_bps"] == "5.0000")
    assert ew5["pct_windows_beating"] == pytest.approx(0.6) and ew5["median_excess"] == base["summary"]["windows_summary"]["median_excess_vs_ew"]
    bdef = next(b for b in out["benchmarks"] if b["benchmark"] == "EW_REBALANCED")["definition"]["config"]
    assert bdef["portfolio_size"] == len(SECTORS) == bdef["exit_rank"] and next(b for b in out["benchmarks"] if b["benchmark"] == "BUY_HOLD")["definition"]["config"]["rebalance_threshold"] == "1.000000"
    rows = base["summary"]["cost"]["rows"]
    assert [r["transaction_cost_bps"] for r in rows] == ["0.0000", "5.0000", "10.0000", "20.0000"]                                                                                   # 25
    cagrs = [r["strategy"]["cagr"] for r in rows]
    assert cagrs == sorted(cagrs, reverse=True) and rows[1]["excess_vs_ew"] == pytest.approx(base["summary"]["stitched"]["strategy"]["total_return"] - base["summary"]["stitched"]["EW_REBALANCED"]["total_return"])
    conc_b, conc_s = base["summary"]["attribution"]["concentration"], by["sector_neutral"]["summary"]["attribution"]["concentration"]                                              # 26
    assert conc_b["top1"] <= conc_b["top3"] <= conc_b["top5"] <= 1.0 and by["sector_neutral"]["summary"]["attribution"]["sector_concentration"]["top_sector_share"] < base["summary"]["attribution"]["sector_concentration"]["top_sector_share"] - 0.4
    assert "CONCENTRATION_REDUCED" in by["sector_neutral"]["research_flags"] and "CONCENTRATION_REDUCED" not in base["research_flags"]
    dd = base["summary"]["drawdown"]                                                                                                                                                 # 27
    assert set(dd) == {"strategy", "EW_REBALANCED", "BUY_HOLD", "SPY"} and dd["SPY"]["max_drawdown"] == pytest.approx(GOLDEN["bench"]["SPY_dd"], abs=1e-4) and dd["strategy"]["max_drawdown"] < dd["EW_REBALANCED"]["max_drawdown"]
    assert "DRAWDOWN_IMPROVED" in by["sector_neutral"]["research_flags"] and "DRAW_DOWN_NOT_IMPROVED" in base["flags"]
    rg = base["summary"]["regimes"]["rows"]                                                                                                                                         # 28
    assert set(rg) == {"TREND_UP", "TREND_DOWN", "HIGH_VOL", "LOW_VOL"} and base["summary"]["regimes"]["weak"] == [] and rg["TREND_DOWN"]["sessions"] >= 60
    for k, v in rg.items():
        assert v["excess_vs_ew"] == pytest.approx(v["strategy_return"] - v["ew_return"]) and 0 < v["mean_exposure"] <= 1
    ro = by["simple_risk_off"]["summary"]["regimes"]["rows"]
    assert ro["TREND_DOWN"]["mean_exposure"] < rg["TREND_DOWN"]["mean_exposure"] - 0.3 and ro["HIGH_VOL"]["excess_vs_ew"] > rg["HIGH_VOL"]["excess_vs_ew"]


# ==================================================================================================================================
# 29–30: criteria and NO_SIGNAL_IMPROVEMENT
# ==================================================================================================================================

def _summary(**over):
    s = {"stitched": {"strategy": {"total_return": 0.20, "cagr": 0.12, "sharpe": 1.5, "max_drawdown": -0.10}, "EW_REBALANCED": {"total_return": 0.10, "cagr": 0.07, "sharpe": 1.2, "max_drawdown": -0.12},
                      "BUY_HOLD": {"total_return": 0.09, "cagr": 0.06, "sharpe": 1.1, "max_drawdown": -0.12}, "SPY": {"total_return": 0.08, "cagr": 0.05, "sharpe": 1.0, "max_drawdown": -0.13}},
         "per_window": {"median_sharpe": 1.4}, "windows_summary": {"median_excess_vs_ew": 0.01, "pct_beating_EW_REBALANCED": 0.8, "pct_beating_SPY": 0.8, "pct_beating_BUY_HOLD": 0.8},
         "cost": {"excess_vs_ew_at_10": 0.05, "excess_vs_ew_at_20": 0.03, "first_nonpositive_vs_ew": None, "first_nonpositive_vs_spy": None},
         "attribution": {"concentration": {"top3": 0.40}, "sector_concentration": {"top_sector_share": 0.35, "max_sector_weight": 0.30}, "holdings": {"churn_mean": 0.30},
                         "window_concentration": {"best_window_share_of_log_return": 0.3}},
         "leave_one_out": [{"symbol": "X", "dominant": False}], "leave_sector_out": [{"sector": "S", "dependent": False}],
         "regimes": {"rows": {"TREND_DOWN": {"excess_vs_ew": 0.01, "sessions": 100}, "HIGH_VOL": {"excess_vs_ew": 0.0, "sessions": 90}}, "weak": []}, "construction": {"cap_changed_selection": 0, "cap_infeasible": 0}}
    for k, v in over.items():
        cur = s
        *path, last = k.split(".")
        for p in path:
            cur = cur[p]
        cur[last] = v
    return s


def test_29_30_criteria_and_no_signal_improvement(golden):
    base = _summary()
    good = _summary()
    c = EV.criteria(good, base, 1.3, [])
    assert c["met"] and c["passed"] == c["n"] == 11 and c["version"] == "sc_v1"                                                                                                       # 29
    v = {"research": V.rules(sector_map_hash="0" * 64)}
    assert EV.research_flags(v, good, base, None, None, c) == ["IMPROVEMENT_CRITERIA_MET"]
    bad = _summary(**{"windows_summary.median_excess_vs_ew": -0.01, "attribution.concentration.top3": 0.55, "cost.excess_vs_ew_at_10": 0.0, "attribution.holdings.churn_mean": 0.34})
    c2 = EV.criteria(bad, base, 1.3, ["DOMINANT_CONTRIBUTOR"])
    assert not c2["met"] and c2["passed"] == 7 and [x["check"] for x in c2["checks"] if not x["pass"]] == ["median excess vs EW >= 0", "excess vs EW at 10/10 bps > 0", "top-3 share <= 0.5", "no DOMINANT_CONTRIBUTOR"]
    worse = _summary(**{"stitched.strategy.total_return": 0.09, "stitched.strategy.cagr": 0.08, "stitched.strategy.sharpe": 1.0, "per_window.median_sharpe": 1.1, "windows_summary.median_excess_vs_ew": -0.02})
    assert not EV.criteria(worse, base, 1.3, [])["met"] and EV.research_flags(v, worse, base, None, None, EV.criteria(worse, base, 1.3, [])) == ["EW_STILL_DOMINANT"]
    assert EV.factor_verdict(worse, base)["verdict"] == "FACTOR_HELPFUL" and EV.factor_verdict(base, base)["verdict"] == "FACTOR_NEUTRAL"
    assert EV.factor_verdict(_summary(**{"stitched.strategy.cagr": 0.15, "per_window.median_sharpe": 1.6, "windows_summary.median_excess_vs_ew": 0.03}), base)["verdict"] == "FACTOR_HARMFUL"
    assert golden["out"]["run"]["run_flags"] == ["NO_SIGNAL_IMPROVEMENT"] and "NO_SIGNAL_IMPROVEMENT" in EV.DESCRIPTIONS and EV.describe()["version"] == "rf_v1"                    # 30: a valid outcome
    assert golden["out"]["run"]["status"] == "COMPLETED" and golden["out"]["run"]["result_hash"]


# ==================================================================================================================================
# 31–32: storage, malformed input
# ==================================================================================================================================

def test_31_32_storage_append_only_and_malformed_fails_closed(golden, tmp_path):
    lab, out = golden["lab"], golden["out"]
    rid = out["run"]["run_id"]
    st = SignalResearchStore(Path(lab.path))
    run = st.run(rid)
    assert run["status"] == "COMPLETED" and run["campaign_id"] == golden["camp"]["run"]["campaign_id"] and run["campaign_hash"] == golden["camp"]["run"]["campaign_hash"] and run["n_variants"] == 19
    assert run["definition"]["base_config_hash"] == golden["camp"]["definition"]["base_config_hash"] and run["definition"]["variants"][0]["weights"] == V.normalised_weights(W) and run["diagnostic_run_ids"] == []
    assert run["benchmarks"][0]["metrics"]["total_return"] is not None and run["run_flags"] == ["NO_SIGNAL_IMPROVEMENT"] and run["sector_map_hash"] == SC.sector_map_hash(SECTORS)
    assert len(st.variants(rid)) == 19 and len(st.scorecards(rid)) == 19 and len(st.factors(rid)) == 5 and len(st.sector_tests(rid)) == 5 and len(st.regime_tests(rid)) == 4
    assert len(st.combinations(rid)) == 2 and len(st.comparisons(rid)) == 19 * 9 and st.variants(rid, with_summary=True)[0]["summary"]["stitched"]["strategy"]["total_return"] == pytest.approx(GOLDEN["baseline"]["total"], abs=1e-4)
    assert [v["label"] for v in st.variants(rid)][:3] == ["baseline", "no_momentum", "no_trend"] and st.runs()[0]["run_id"] in {rid} | {r["run_id"] for r in st.runs()}
    with sqlite3.connect(str(lab.path)) as c:
        for sql in ("UPDATE signal_research_runs SET status = 'FAILED'", "DELETE FROM signal_research_variants", "UPDATE signal_research_factor_ablation SET verdict = 'FACTOR_HARMFUL'",
                    "DELETE FROM signal_research_scorecards", "UPDATE signal_research_benchmark_comparisons SET excess = 0", "DELETE FROM signal_research_regime_tests",
                    "DELETE FROM signal_research_sector_tests", "UPDATE signal_research_combinations SET criteria_met = 1"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)
    from database import signal_research_migrations as M
    db = tmp_path / "sr52.db"
    with sqlite3.connect(str(db)) as c:
        c.execute("PRAGMA user_version = 13")
        for _ in range(3):
            M.run_signal_research_migrations(c)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 13
        names = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(M.TABLES) <= names and len(M.TABLES) == 8
        sql = " ".join(s for (s,) in c.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        assert "DROP" not in sql.upper() and "ALTER" not in sql.upper()
    assert SignalResearchStore(db).exists() and SignalResearchStore(db).runs() == []
    cid = golden["camp"]["run"]["campaign_id"]                                                                                                                                       # 32
    for over, code in (({"campaign_id": "0" * 32}, "INVALID_CAMPAIGN"), ({"families": ["nope"]}, "INVALID_FAMILIES"), ({"families": []}, "INVALID_FAMILIES"),
                       ({"caps": ["0.200000"]}, "SECTOR_CAP_BELOW_EQUAL_WEIGHT"), ({"sector_map": {"TAA": ""}}, "INVALID_SECTOR_MAP"), ({"cost_points": [["x", "1"]]}, "INVALID_COST_POINTS")):
        with pytest.raises(SE.ResearchError) as e:
            run_research(lab, over.pop("campaign_id", cid), **over)
        assert e.value.code == code
    with pytest.raises(V.VariantError):
        V.rules("RANDOM_RANK", None, V.NO_OVERLAY, "0" * 64)
    with pytest.raises(V.VariantError):
        V.rules(V.GLOBAL_RANK, "1.5", V.NO_OVERLAY, "0" * 64)
    with pytest.raises((V.VariantError, R.ConfigError)):
        V.make_variant("x", "baseline", {**base_config(), "weights": {**W, "momentum": "0.31"}}, V.rules(sector_map_hash=SC.sector_map_hash(SECTORS)), SECTORS)
    gap = market()
    gap.drop("SPY", date(2025, 2, 10))
    lab2 = FL.FLab(gap)
    lab2.market = gap
    camp2 = run_campaign(lab2)
    assert camp2["run"]["status"] == "FAILED"
    with pytest.raises(SE.ResearchError) as e:
        run_research(lab2, camp2["run"]["campaign_id"])
    assert e.value.code == "CAMPAIGN_FAILED"


# ==================================================================================================================================
# 33–40: isolation, research-only API and UI
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


def test_33_34_35_36_37_38_isolation_no_deploy_no_handoff():
    files = list((ROOT / "signal_research").glob("*.py")) + [ROOT / "api" / "routes" / "signal_research.py", ROOT / "database" / "signal_research_migrations.py"]
    banned = ("paper.alpaca_orders", "paper.alpaca_paper", "paper.store", "portfolio", "agents", "ai_explain", "anthropic", "alpaca", "robinhood", "rh_gateway", "requests", "httpx", "research_flow",
              "notifications", "forward", "random", "sklearn", "torch", "numpy", "scipy", "pandas", "schedule", "threading", "subprocess")
    for f in files:
        hits = [m for m in _imports(f) if any(m == b or m.startswith(b + ".") for b in banned)]
        assert not hits, (f.name, hits)
    ve = (ROOT / "signal_research" / "variant_engine.py").read_text(encoding="utf-8")
    assert ve.count("from paper import alpaca_order_rules as RU") == 1 and ve.count("RU.MAX_QTY") == 2 and ve.count("RU.") == 2                       # the same pure constant the Stage 4.7 engine uses
    src = "".join(f.read_text(encoding="utf-8") for f in files) + (ROOT / "frontend" / "portfolio_signal_research.js").read_text(encoding="utf-8")
    for word in ("Prepare Paper", "AlpacaOrders", "/api/alpaca", "data-apo-", "MutationObserver", "activate_config", "set_active", "deploy(", "promote(", "approved for live", "trade this",
                 "submit_order", "TradingClient", "/v2/orders", "get_provider", "provider_factory"):
        assert word not in src, word
    assert '"handoff_allowed": 0' in ve and "handoff_allowed\": 1" not in ve
    out = H.evaluate({"status": "VALID", "portfolio_source": None}, {"symbol": "TAA", "action": "ADD", "handoff_allowed": 1, "est_qty_diff": 1})
    assert out["eligible"] is False and out["draft"] is None
    sim = (ROOT / "rotation_backtest" / "simulator.py").read_text(encoding="utf-8")
    assert sim.count("E.compute_rotation(") == 1 and "engine=None" in sim and "def rank" not in sim and "percentile" not in sim          # the ONE hook; production path untouched
    design = (ROOT / "DESIGN_52_SIGNAL_RESEARCH.md").read_text(encoding="utf-8").lower()
    assert "no ml" in design and "no_signal_improvement" in design and "never relaxed" in design and "no leverage" in design
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="psr-body"' in html and 'src="portfolio_signal_research.js"' in html and 'href="portfolio_signal_research.css"' in html


@pytest.fixture
def api(monkeypatch):
    import agents.technical_agent as ta
    from ai_explain import service as AX
    from api.routes import portfolio as pr
    from api.routes import signal_research as SR
    from api.server import app
    lab = make_lab()
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr(SR, "NOW_FN", lambda: NOW)
    monkeypatch.setattr(SR, "FETCH", {"fetch_fn": lab.market.fetch, "client": object(), "cache": FC.BarCache()})
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


API = "/api/signal-research"


def test_39_40_api_research_only_and_ui_without_execution_controls(api):
    from api.routes import signal_research as SR
    paths = [(sorted(r.methods)[0], r.path) for r in SR.router.routes]
    assert all(not any(w in p.lower() for w in ("order", "trade", "backtest", "paper", "execute", "broker", "handoff", "deploy", "activate", "promote")) for _, p in paths)
    assert [p for m, p in paths if m == "POST"] == [f"{API}/run"]
    c0 = api.get(f"{API}/config").json()
    assert c0["rules_version"] == "sr_v1" and c0["max_variants"] == 20 and c0["campaigns"] == [] and "survivorship" in c0["universe_note"].lower() and c0["schedules"]["SIMPLE_RISK_OFF"]["TREND_DOWN|HIGH_VOL"] == "0.250000"
    camp = run_campaign(api.lab)
    api.lab.market.calls.clear()
    r = api.post(f"{API}/run", json={"campaign_id": camp["run"]["campaign_id"], "sector_map": SECTORS, "families": ["sector"], "caps": ["0.250000"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["run"]["status"] == "COMPLETED" and out["run"]["market_data_requests"] == 1 and len(api.lab.market.calls) == 1 and api.hits == []
    assert [v["label"] for v in out["variants"]] == ["baseline", "sector_neutral", "sector_cap_0.25"] and all(v["scorecard"] and v["criteria"] for v in out["variants"])
    assert "handoff" not in str(out).lower() and "sector_map" not in out["definition"] and out["run"]["run_flags"] == ["NO_SIGNAL_IMPROVEMENT"]
    rid = out["run"]["run_id"]
    assert api.get(f"{API}/runs").json()["runs"][0]["run_id"] == rid
    d = api.get(f"{API}/runs/{rid}").json()
    assert d["run"]["campaign_id"] == camp["run"]["campaign_id"] and len(d["variants"]) == 3 and d["benchmarks"] and "sector_neutral" in d["conventions"]
    a = api.get(f"{API}/runs/{rid}/ablation").json()
    assert a["factors"] == [] and [v["label"] for v in a["variants"]] == ["baseline"] and a["variants"][0]["stitched"]["strategy"]["total_return"] is not None
    s = api.get(f"{API}/runs/{rid}/sector").json()
    assert len(s["sector_tests"]) == 2 and s["sector_map_hash"] == SC.sector_map_hash(SECTORS)
    assert api.get(f"{API}/runs/{rid}/regime").json()["regime_tests"] == [] and api.get(f"{API}/runs/{rid}/combinations").json()["combinations"] == []
    b = api.get(f"{API}/runs/{rid}/benchmarks").json()
    assert {x["benchmark"] for x in b["benchmarks"]} == {"SPY", "EW_REBALANCED", "BUY_HOLD"} and len(b["comparisons"]) == 27
    sc = api.get(f"{API}/runs/{rid}/scorecard").json()
    assert len(sc["scorecards"]) == 3 and sc["run_flags"] == ["NO_SIGNAL_IMPROVEMENT"] and sc["research"]["version"] == "rf_v1"
    assert api.get(f"{API}/runs/{'z' * 32}").status_code == 404 and api.get(f"{API}/runs/short/sector").status_code == 404
    assert api.post(f"{API}/run", json={"campaign_id": camp["run"]["campaign_id"], "side": "BUY"}).status_code == 422
    assert api.post(f"{API}/run", json={"campaign_id": "0" * 32}).status_code == 404
    assert api.post(f"{API}/run", json={"campaign_id": camp["run"]["campaign_id"], "families": ["nope"]}).status_code == 422
    js = (ROOT / "frontend" / "portfolio_signal_research.js").read_text(encoding="utf-8")
    assert js.count("fetch(") == 1 and API in js and "/api/alpaca" not in js
    for word in ("Trade this", "Deploy", "Activate", "Prepare Paper", "Preview", "Confirm Paper", "Promote"):
        assert word not in js, word
