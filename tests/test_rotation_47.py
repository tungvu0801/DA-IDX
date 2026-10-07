"""Stage 4.7 Phase 1 — the PURE deterministic core (rotation/factors.py, rotation/rules.py). Fully offline: Decimal inputs
only, no clock, no database, no network, no broker (conftest blocks sockets and tripwires the Alpaca wires anyway)."""
import random
import re
from decimal import Decimal
from pathlib import Path

import pytest

from rotation import factors as F
from rotation import rules as R

ROOT = Path(__file__).resolve().parents[1]
D = Decimal


def closes(*values):
    return [D(str(v)) for v in values]


# ==================================================================================================================================
# A — factors (exact examples)
# ==================================================================================================================================

def test_1_momentum_exact_example():
    c = [D(90)] * 61
    c[0], c[40], c[60] = D(80), D(100), D(110)                        # T-60 = 80, T-20 = 100, T = 110
    assert F.simple_return(c, 20) == D("0.100000") and F.simple_return(c, 60) == D("0.375000")
    assert F.simple_return(c[:60], 60) is None                         # 61 closes needed
    assert F.simple_return([D(0)] + c[1:], 60) is None                 # a non-positive base close: no factor


def test_2_trend_exact_example():
    c = [D(100)] * 199 + [D(120)]
    assert F.sma(c, 50) == D("100.4") and F.trend(c, 50) == D("0.195219")      # 120 / 100.4 - 1
    assert F.sma(c, 200) == D("100.1") and F.trend(c, 200) == D("0.198801")    # 120 / 100.1 - 1
    assert F.trend(c[:199], 200) is None


def test_3_relative_strength_exact_example():
    assert F.relative_strength(D("0.10"), D("0.05")) == D("0.047619")          # 1.10 / 1.05 - 1
    assert F.relative_strength(D("0.10"), D("0")) == D("0.100000")
    assert F.relative_strength(None, D("0.05")) is None and F.relative_strength(D("0.1"), D("-1")) is None


def test_4_volatility_deterministic_example():
    alternating = closes(*([100, 110] * 10 + [100]))                   # 21 closes, 20 log returns of +/- ln(1.1), mean 0
    v = F.volatility(alternating)
    assert v == D("1.552308")                                           # sqrt(20 ln(1.1)^2 / 19) x sqrt(252) = 1.5523075…
    assert F.volatility(closes(*([100] * 21))) == D("0.000000")        # flat prices: zero volatility
    assert F.volatility(alternating[:20]) is None                      # 21 closes needed
    assert F.volatility(alternating) == F.volatility(list(alternating))  # pure: same input, same output


def test_5_drawdown():
    c = [D(100)] * 252
    c[100], c[-1] = D(200), D(150)
    assert F.drawdown(c) == D("-0.250000")
    assert F.drawdown([D(100)] * 251 + [D(120)]) == D("0.000000")      # at the rolling high
    assert F.drawdown(c[:251]) is None


def test_6_liquidity():
    c = [D(10)] * 20
    v = [D(1000)] * 19 + [D(3000)]
    assert F.liquidity(c, v) == D("11000.000000")                       # (19 x 10,000 + 30,000) / 20
    assert F.liquidity(c, v[:19]) is None and F.liquidity(c, v[:-1] + [D(-1)]) is None


def test_6b_compute_factors_and_completeness():
    c = [D(100)] * 251 + [D(120)]
    v = [D(1000)] * 252
    spy = [D(100)] * 251 + [D(110)]
    f = F.compute_factors(c, v, spy)
    assert set(f) == set(F.FACTOR_KEYS) and F.complete(f)
    assert f["ret20"] == D("0.200000") and f["relative_strength"] == F.relative_strength(f["ret60"], D("0.100000"))
    assert not F.complete(F.compute_factors(c[:100], v[:100], spy))      # too short for SMA200 / drawdown
    assert F.to_decimal(101.1) == D("101.1") and F.to_decimal(float("nan")) is None and F.to_decimal(None) is None


# ==================================================================================================================================
# B — percentile normalisation, composite, ranking
# ==================================================================================================================================

def test_7_percentile_n5():
    p = R.percentiles({"A": D(1), "B": D(2), "C": D(3), "D": D(4), "E": D(5)})
    assert p == {"A": D("0.000000"), "B": D("25.000000"), "C": D("50.000000"), "D": D("75.000000"), "E": D("100.000000")}


def test_8_percentile_ties_use_average_rank():
    p = R.percentiles({"A": D(1), "B": D(2), "C": D(2), "D": D(3)})     # ranks 1, 2.5, 2.5, 4
    assert p == {"A": D("0.000000"), "B": D("50.000000"), "C": D("50.000000"), "D": D("100.000000")}
    assert set(R.percentiles({"X": D(7), "Y": D(7)}).values()) == {D("50.000000")}   # all tied: all average


def test_9_percentile_single_symbol_is_50():
    assert R.percentiles({"ONLY": D("123.4")}) == {"ONLY": D("50.000000")} and R.percentiles({}) == {}


def test_10_volatility_score_is_inverted():
    raw = {s: {k: D(1) for k in F.FACTOR_KEYS} for s in ("LOW", "MID", "HIGH")}
    raw["LOW"]["volatility"], raw["MID"]["volatility"], raw["HIGH"]["volatility"] = D("0.10"), D("0.20"), D("0.30")
    sc = R.factor_scores(raw)
    assert (sc["LOW"]["volatility"], sc["MID"]["volatility"], sc["HIGH"]["volatility"]) == (D("100.000000"), D("50.000000"), D("0.000000"))
    assert sc["LOW"]["momentum"] == D("50.000000")                     # all other factors tied -> average


def test_11_composite_exact_result():
    w = R.validate_weights(R.DEFAULT_WEIGHTS)
    scores = {"momentum": D(100), "trend": D(50), "relative_strength": D(0), "volatility": D(80), "drawdown": D(999), "liquidity": D(10)}
    assert R.composite(scores, w) == D("51.500000")                    # 30 + 12.5 + 0 + 8 + 0 + 1 (drawdown weight 0)
    assert R.composite(scores, R.validate_weights({**R.DEFAULT_WEIGHTS, "drawdown": "0.10", "liquidity": "0"})) == D("150.400000")


def test_11b_weights_validation():
    assert R.validate_weights(R.DEFAULT_WEIGHTS)["drawdown"] == D("0.000000")
    for bad in ({**R.DEFAULT_WEIGHTS, "momentum": "0.31"}, {**R.DEFAULT_WEIGHTS, "momentum": "-0.1", "trend": "0.65"},
                {k: v for k, v in R.DEFAULT_WEIGHTS.items() if k != "drawdown"}, {**R.DEFAULT_WEIGHTS, "extra": "0"},
                {**R.DEFAULT_WEIGHTS, "momentum": "0.3000001", "trend": "0.2499999"}):
        with pytest.raises(R.ConfigError) as e:
            R.validate_weights(bad)
        assert e.value.code == "INVALID_WEIGHTS"
    with pytest.raises(R.ConfigError):
        R.validate_weights({**R.DEFAULT_WEIGHTS, "momentum": 0.30})     # a float is refused: strings / Decimals only


def entries(**kw):
    return {s: {"composite": D(c), "relative_strength": D(rs), "liquidity": D(lq)} for s, (c, rs, lq) in kw.items()}


def test_12_ranking_tie_breakers():
    e = entries(AAA=(50, 10, 10), BBB=(60, 10, 10), CCC=(60, 20, 10), DDD=(60, 20, 30), EEE=(60, 20, 30))
    assert R.rank_symbols(e) == ["DDD", "EEE", "CCC", "BBB", "AAA"]       # composite, then RS, then liquidity, then ticker
    assert R.ranks(["DDD", "EEE"]) == {"DDD": 1, "EEE": 2}


def test_13_shuffled_input_order_gives_identical_ranking_and_scores():
    rng = random.Random(7)
    raw = {f"S{i:02d}": {k: D(str(rng.randint(-50, 50) / 7)) for k in F.FACTOR_KEYS} for i in range(30)}
    raw["S03"]["volatility"] = raw["S17"]["volatility"]                 # a tie
    w = R.validate_weights(R.DEFAULT_WEIGHTS)

    def pipeline(r):
        sc = R.factor_scores(r)
        ent = {s: {**sc[s], "composite": R.composite(sc[s], w)} for s in sc}
        return R.rank_symbols(ent), ent
    base = pipeline(raw)
    for _ in range(5):
        items = list(raw.items())
        rng.shuffle(items)
        assert pipeline(dict(items)) == base


# ==================================================================================================================================
# C — static equal weight
# ==================================================================================================================================

def test_14_equal_weight_exact_sum():
    cfg = R.validate_portfolio_config(10, 15, "0.05", "0.05", "0.20", "0.01", "0.50")
    assert cfg["equal_weight"] == D("0.095000")
    weights, cash = R.allocate([f"S{i}" for i in range(10)], 10, cfg["cash_buffer_pct"])
    assert sum(weights.values()) == D("0.950000") and cash == D("0.050000") and set(weights.values()) == {D("0.095000")}


def test_15_invalid_min_max_equal_weight_configurations():
    for size, lo, hi, code in ((10, "0.10", "0.20", "INVALID_WEIGHT_BOUNDS"),     # equal 0.095 < min
                               (10, "0.01", "0.09", "INVALID_WEIGHT_BOUNDS"),     # equal 0.095 > max
                               (10, "0.20", "0.10", "INVALID_WEIGHT_BOUNDS"),     # min > max
                               (0, "0", "1", "INVALID_PORTFOLIO_SIZE")):
        with pytest.raises(R.ConfigError) as e:
            R.validate_portfolio_config(size, 15, "0.05", lo, hi, "0.01", "0.5")
        assert e.value.code == code
    for kw, code in (({"exit_rank": 9}, "INVALID_EXIT_RANK"), ({"cash_buffer_pct": "1"}, "INVALID_CASH_BUFFER"),
                     ({"rebalance_threshold": "1.5"}, "INVALID_THRESHOLD"), ({"max_turnover_per_rotation": "-0.1"}, "INVALID_TURNOVER_LIMIT"),
                     ({"cash_buffer_pct": 0.05}, "INVALID_CONFIG")):
        args = {"portfolio_size": 10, "exit_rank": 15, "cash_buffer_pct": "0.05", "min_position_weight": "0.05",
                "max_position_weight": "0.20", "rebalance_threshold": "0.01", "max_turnover_per_rotation": "0.5", **kw}
        with pytest.raises(R.ConfigError) as e:
            R.validate_portfolio_config(**args)
        assert e.value.code == code


def test_16_quantisation_residual_goes_to_rank_1():
    weights, cash = R.allocate(["TOP", "B", "C"], 3, D("0.05"))        # 0.95 / 3 = 0.316666 (rounded down) x 3 = 0.949998
    assert weights == {"TOP": D("0.316668"), "B": D("0.316666"), "C": D("0.316666")}
    assert sum(weights.values()) == D("0.950000") and cash == D("0.050000")
    partial, cash2 = R.allocate(["TOP", "B"], 3, D("0.05"))             # unfilled slot: no residual, its weight stays cash
    assert partial == {"TOP": D("0.316666"), "B": D("0.316666")} and cash2 == D("0.366668")


# ==================================================================================================================================
# D — rank buffer, actions, turnover
# ==================================================================================================================================

def test_17_rank_buffer_size_10_exit_15():
    ranked = [f"R{i:02d}" for i in range(1, 31)]                        # R01 best ... R30
    holdings = {"R11", "R15", "R16", "GONE", "R02"}                     # GONE is ineligible (not ranked)
    out = R.select(ranked, holdings, portfolio_size=10, exit_rank=15)
    assert "R11" in out["selected"] and "R15" in out["selected"]        # rank 11 and 15 retained
    assert out["exits"] == {"R16": "RANK_ABOVE_EXIT_RANK", "GONE": "INELIGIBLE"}   # rank 16 exits
    assert out["selected"] == ["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08", "R11", "R15"]
    assert out["retained"] == ["R02", "R11", "R15"]
    assert out["added"] == ["R01", "R03", "R04", "R05", "R06", "R07", "R08"]       # 7 free slots, best non-holdings
    assert out["reasons"]["R11"] == "RETAINED_RANK_BUFFER" and out["reasons"]["R15"] == "RETAINED_RANK_BUFFER"
    assert out["reasons"]["R02"] == "TOP_N" and out["reasons"]["R01"] == "TOP_N"


def test_17b_rank_buffer_overflow_is_deterministic():
    ranked = [f"R{i:02d}" for i in range(1, 21)]
    holdings = {f"R{i:02d}" for i in range(1, 13)}                      # 12 holdings all within exit_rank 15, size 10
    out = R.select(ranked, holdings, 10, 15)
    assert out["selected"] == [f"R{i:02d}" for i in range(1, 11)] and out["added"] == []
    assert out["exits"] == {"R11": "RANK_BUFFER_OVERFLOW", "R12": "RANK_BUFFER_OVERFLOW"}


def test_18_rebalance_threshold():
    thr = D("0.01")
    assert R.classify(True, D("0.094"), D("0.100"), thr, est_qty=6) == ("HOLD", "WITHIN_THRESHOLD")      # 0.6% drift
    assert R.classify(True, D("0.075"), D("0.100"), thr, est_qty=25) == ("INCREASE", None)             # 2.5% drift
    assert R.classify(True, D("0.120"), D("0.100"), thr, est_qty=20) == ("DECREASE", None)
    assert R.classify(True, D("0.120"), D("0"), thr, est_qty=120) == ("EXIT", "TARGET_ZERO")
    assert R.classify(False, D("0"), D("0.100"), thr, est_qty=10) == ("ADD", None)
    assert R.classify(False, D("0"), D("0"), thr, est_qty=0) == ("NONE", None)
    assert R.side_hint("ADD") == R.side_hint("INCREASE") == "BUY" and R.side_hint("EXIT") == R.side_hint("DECREASE") == "SELL"
    assert R.side_hint("HOLD") is None and R.side_hint("NONE") is None


def test_19_below_one_share():
    assert R.est_qty_diff(D("0.025"), D("10000.00"), D("300")) == 0                                    # $250 < one share
    assert R.est_qty_diff(D("0.025"), D("100000.00"), D("300")) == 8                                   # floor(2500 / 300)
    assert R.classify(True, D("0.075"), D("0.100"), D("0.01"), est_qty=0) == ("HOLD", "BELOW_ONE_SHARE")
    assert R.classify(False, D("0"), D("0.100"), D("0.01"), est_qty=0) == ("HOLD", "BELOW_ONE_SHARE")


def test_19b_reference_valuation_ignores_everything_but_cash_qty_and_reference_price():
    out = R.current_weights(D("1000.00"), {"AAA": D("10"), "BBB": D("2.5")}, {"AAA": D("100"), "BBB": D("400")})
    assert out["reference_equity"] == D("3000.00")
    assert out["weights"] == {"AAA": D("0.333333"), "BBB": D("0.333333")} and out["cash_weight"] == D("0.333333")
    with pytest.raises(ValueError):
        R.current_weights(D("1000"), {"AAA": D("10")}, {})               # a held symbol without a reference price
    with pytest.raises(ValueError):
        R.current_weights(D("0"), {}, {})                                # no equity at all


def test_20_cash_aware_turnover():
    cur = {"AAA": D("0.50"), "BBB": D("0.30")}                           # cash 0.20
    tgt = {"AAA": D("0.40"), "CCC": D("0.40")}                           # cash 0.20
    assert R.turnover(cur, tgt, D("0.20"), D("0.20")) == D("0.400000")   # (0.1 + 0.3 + 0.4 + 0) / 2
    assert R.turnover(cur, {"AAA": D("0.50"), "BBB": D("0.30")}, D("0.20"), D("0.20")) == D("0.000000")
    assert R.turnover({}, {}, D("1"), D("1")) == D("0.000000")
    assert R.turnover({"AAA": D("0.5")}, {"AAA": D("0.5")}, D("0.5"), D("0.3")) == D("0.100000")   # cash-only change counts


def test_21_all_cash_to_95_percent_invested_is_95_percent_turnover():
    weights, cash = R.allocate([f"S{i}" for i in range(10)], 10, D("0.05"))
    assert R.turnover({}, weights, D("1"), cash) == D("0.950000")


# ==================================================================================================================================
# E — purity
# ==================================================================================================================================

def test_22_pure_core_has_no_random_clock_network_or_io_dependency():
    for f in ("rotation/__init__.py", "rotation/factors.py", "rotation/rules.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"^\s*(from|import)\s+(random|datetime|time|requests|socket|urllib|http|json|sqlite3|os|sys|"
                             r"threading|subprocess|alpaca|anthropic|agents|portfolio|paper|rh_gateway|fit|backtest|api|"
                             r"database)\b", code, re.M), f
        assert not re.search(r"random\.|datetime|time\.|requests|socket|urllib|open\(|os\.environ|getenv|\.env|/v2/|"
                             r"TradingClient|place_?order|submit_?order|cancel|anthropic|get_provider|eval\(|exec\(|"
                             r"__import__|importlib|float\(", code), f
    assert set(F.FACTOR_KEYS) == {"ret20", "ret60", "trend50", "trend200", "relative_strength", "volatility", "drawdown", "liquidity"}
    assert R.ACTIONS == ("ADD", "INCREASE", "DECREASE", "EXIT", "HOLD", "NONE")
