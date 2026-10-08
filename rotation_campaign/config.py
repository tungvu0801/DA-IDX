"""
rotation_campaign/config.py — the immutable ModelCampaignConfig: validation, gates, canonical form and hash (DESIGN_50 §1–§2).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from rotation import universe as U
from rotation.store import canonical_json, sha256_hex
from rotation_backtest import ENGINE_VERSION as BACKTEST_VERSION
from rotation_backtest import config as BC
from rotation_walkforward import ENGINE_VERSION as WALKFORWARD_VERSION
from rotation_walkforward import ROBUSTNESS_VERSION
from rotation_walkforward import config as WC

from rotation_campaign import ENGINE_VERSION, FINALIST_LABEL

UNIVERSE_NOTE = BC.UNIVERSE_NOTE
DEFAULTS = {"train_months": 24, "test_months": 6, "step_months": None, "rebalance_frequency": "MONTHLY", "selection_metric": "SHARPE",
            "initial_cash": "100000.00", "transaction_cost_bps": "5", "slippage_bps": "5", "min_train_sessions": 120, "min_test_sessions": 20,
            "max_candidates": 50, "finalist_count": 3, "max_drawdown_limit": "-0.25"}
GATE_DEFAULTS = {"min_windows": 3, "max_failed_windows": 0, "max_drawdown_floor": "-0.35", "min_positive_window_pct": "0.50",
                 "min_benchmark_beating_pct": "0.40", "max_turnover": "0.90", "max_fragility": "0.50", "max_cost_sensitivity": "0.75"}
LIMITS = {"max_candidates_cap": 100, "max_finalists": 10, "max_months": 120}
RANKING = ("eligible first", "robustness score desc", "median OOS Sharpe desc", "worst OOS max drawdown less severe", "cost sensitivity asc",
           "fragility ratio asc", "mean OOS turnover asc", "config hash asc")
FRAGILITY_RULE = "fragility_ratio = max(0, Sharpe_base - min Sharpe_neighbour) / max(|Sharpe_base|, 1.0) over valid one-at-a-time neighbours (Stage 4.9 neighbourhood)"


class CampaignConfigError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _int(v, what, lo, hi) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            v = int(str(v))
        except (TypeError, ValueError):
            raise CampaignConfigError("INVALID_INTEGER", f"{what} must be an integer") from None
    if not lo <= v <= hi:
        raise CampaignConfigError("OUT_OF_RANGE", f"{what} must be between {lo} and {hi}")
    return v


def _dec(v, what, lo: Decimal, hi: Decimal, places: str) -> str:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        raise CampaignConfigError("INVALID_DECIMAL", f"{what} must be a decimal number") from None
    if not d.is_finite() or d < lo or d > hi:
        raise CampaignConfigError("OUT_OF_RANGE", f"{what} must be between {lo} and {hi}")
    return format(d.quantize(Decimal(places)), "f")


def _date(v, what) -> date:
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        raise CampaignConfigError("INVALID_DATE", f"{what} must be an ISO date (YYYY-MM-DD)") from None


def normalise_gates(g: Optional[Dict]) -> Dict:
    g = {**GATE_DEFAULTS, **(g or {})}
    unknown = sorted(set(g) - set(GATE_DEFAULTS))
    if unknown:
        raise CampaignConfigError("INVALID_GATES", f"unknown gates {unknown}")
    return {"min_windows": _int(g["min_windows"], "gates.min_windows", 1, 1000), "max_failed_windows": _int(g["max_failed_windows"], "gates.max_failed_windows", 0, 1000),
            "max_drawdown_floor": _dec(g["max_drawdown_floor"], "gates.max_drawdown_floor", Decimal("-1"), Decimal("0"), "0.0001"),
            "min_positive_window_pct": _dec(g["min_positive_window_pct"], "gates.min_positive_window_pct", Decimal(0), Decimal(1), "0.0001"),
            "min_benchmark_beating_pct": _dec(g["min_benchmark_beating_pct"], "gates.min_benchmark_beating_pct", Decimal(0), Decimal(1), "0.0001"),
            "max_turnover": _dec(g["max_turnover"], "gates.max_turnover", Decimal(0), Decimal(1), "0.0001"),
            "max_fragility": _dec(g["max_fragility"], "gates.max_fragility", Decimal(0), Decimal(100), "0.0001"),
            "max_cost_sensitivity": _dec(g["max_cost_sensitivity"], "gates.max_cost_sensitivity", Decimal(0), Decimal(1), "0.0001")}


def normalise(body: Dict, base_cfg: Dict, universe: U.ResolvedUniverse, candidates: List[Dict], grid_spec: Optional[Dict]) -> Dict:
    if not isinstance(body, dict):
        raise CampaignConfigError("INVALID_BODY", "the campaign definition must be an object")
    start, end = _date(body.get("start_date"), "start_date"), _date(body.get("end_date"), "end_date")
    if end <= start:
        raise CampaignConfigError("INVALID_DATE_RANGE", "end_date must be after start_date")
    if (end - start).days > LIMITS["max_months"] * 31:
        raise CampaignConfigError("INVALID_DATE_RANGE", f"the range may not exceed {LIMITS['max_months']} months")
    train = _int(body.get("train_months", DEFAULTS["train_months"]), "train_months", 1, LIMITS["max_months"])
    test = _int(body.get("test_months", DEFAULTS["test_months"]), "test_months", 1, LIMITS["max_months"])
    step = _int(body.get("step_months") if body.get("step_months") is not None else test, "step_months", 1, LIMITS["max_months"])
    freq = str(body.get("rebalance_frequency") or DEFAULTS["rebalance_frequency"]).upper()
    if freq not in WC.FREQUENCIES:
        raise CampaignConfigError("INVALID_FREQUENCY", f"rebalance_frequency must be one of {list(WC.FREQUENCIES)}")
    metric = str(body.get("selection_metric") or DEFAULTS["selection_metric"]).upper()
    if metric not in WC.SELECTION_METRICS:
        raise CampaignConfigError("INVALID_SELECTION_METRIC", f"selection_metric must be one of {list(WC.SELECTION_METRICS)}")
    cash = _dec(body.get("initial_cash", DEFAULTS["initial_cash"]), "initial_cash", BC.LIMITS["min_initial_cash"], BC.LIMITS["max_initial_cash"], "0.01")
    cost = _dec(body.get("transaction_cost_bps", DEFAULTS["transaction_cost_bps"]), "transaction_cost_bps", Decimal(0), BC.LIMITS["max_bps"], "0.0001")
    slip = _dec(body.get("slippage_bps", DEFAULTS["slippage_bps"]), "slippage_bps", Decimal(0), BC.LIMITS["max_bps"], "0.0001")
    min_train = _int(body.get("min_train_sessions", DEFAULTS["min_train_sessions"]), "min_train_sessions", 1, 5000)
    min_test = _int(body.get("min_test_sessions", DEFAULTS["min_test_sessions"]), "min_test_sessions", 1, 5000)
    max_cand = _int(body.get("max_candidates", DEFAULTS["max_candidates"]), "max_candidates", 1, LIMITS["max_candidates_cap"])
    finalists = _int(body.get("finalist_count", DEFAULTS["finalist_count"]), "finalist_count", 1, LIMITS["max_finalists"])
    dd_limit = _dec(body.get("max_drawdown_limit", DEFAULTS["max_drawdown_limit"]), "max_drawdown_limit", Decimal("-1"), Decimal("0"), "0.0001")
    gates = normalise_gates(body.get("gates"))
    if not candidates:
        raise CampaignConfigError("NO_CANDIDATES", "at least one valid candidate configuration is required")
    if not universe.symbols:
        raise CampaignConfigError("EMPTY_UNIVERSE", "the universe resolved to no symbols")
    return {"base_config_id": base_cfg["config_id"], "base_config_hash": base_cfg["config_hash"], "base_config": base_cfg["config"],
            "start_date": start.isoformat(), "end_date": end.isoformat(), "train_months": train, "test_months": test, "step_months": step,
            "overlapping_tests": step < test, "rebalance_frequency": freq, "selection_metric": metric, "max_drawdown_limit": dd_limit,
            "initial_cash": cash, "transaction_cost_bps": cost, "slippage_bps": slip, "benchmark": BC.BENCHMARK, "execution_price": BC.EXECUTION_PRICE,
            "min_train_sessions": min_train, "min_test_sessions": min_test, "max_candidates": max_cand, "finalist_count": finalists, "gates": gates,
            "candidates": [{"label": c["label"], "config_hash": c["config_hash"], "config": c["config"]} for c in candidates], "grid_spec": grid_spec,
            "cost_pairs": [["0.0000", "0.0000"], [cost, slip], ["20.0000", "20.0000"]], "ranking": list(RANKING), "fragility_rule": FRAGILITY_RULE,
            "finalist_label": FINALIST_LABEL, "universe_source": universe.source, "universe_ref": universe.ref, "universe_symbols": list(universe.symbols),
            "universe_hash": universe.universe_hash, "engine_version": ENGINE_VERSION, "walkforward_version": WALKFORWARD_VERSION,
            "backtest_version": BACKTEST_VERSION, "robustness_version": ROBUSTNESS_VERSION}


def definition_hash(canon: Dict) -> str:
    return sha256_hex(canonical_json(canon))


def public(canon: Dict) -> Dict:
    return {**canon, "campaign_hash": definition_hash(canon), "universe_note": UNIVERSE_NOTE}
