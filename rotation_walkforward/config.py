"""
rotation_walkforward/config.py — the immutable WalkForwardConfig: validation, canonical form and hash (DESIGN_49 §2–§4, §8).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from database import portfolio_walkforward_migrations as M
from rotation import universe as U
from rotation.store import canonical_json, sha256_hex
from rotation_backtest import ENGINE_VERSION as BACKTEST_VERSION
from rotation_backtest import config as BC

from rotation_walkforward import ENGINE_VERSION, ROBUSTNESS_VERSION

FREQUENCIES = M.FREQUENCIES
SELECTION_METRICS = M.SELECTION_METRICS
DEFAULT_SELECTION = "SHARPE"
BENCHMARK = M.BENCHMARK
DEFAULTS = {"train_months": 24, "test_months": 6, "step_months": None, "rebalance_frequency": "MONTHLY", "selection_metric": DEFAULT_SELECTION,
            "initial_cash": "100000.00", "transaction_cost_bps": "5", "slippage_bps": "5", "min_train_sessions": 120, "min_test_sessions": 20,
            "max_candidates": 20, "max_drawdown_limit": "-0.25"}
LIMITS = {"max_candidates_cap": 100, "max_windows": 60, "max_months": 120}
COST_MATRIX = (("0", "0"), ("2.5", "2.5"), ("5", "5"), ("10", "10"), ("20", "20"))
UNIVERSE_NOTE = BC.UNIVERSE_NOTE


class WalkForwardConfigError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _int(v, what: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            v = int(str(v))
        except (TypeError, ValueError):
            raise WalkForwardConfigError("INVALID_INTEGER", f"{what} must be an integer") from None
    if not lo <= v <= hi:
        raise WalkForwardConfigError("OUT_OF_RANGE", f"{what} must be between {lo} and {hi}")
    return v


def _dec(v, what: str, lo: Decimal, hi: Decimal, places: str) -> str:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        raise WalkForwardConfigError("INVALID_DECIMAL", f"{what} must be a decimal number") from None
    if not d.is_finite() or d < lo or d > hi:
        raise WalkForwardConfigError("OUT_OF_RANGE", f"{what} must be between {lo} and {hi}")
    return format(d.quantize(Decimal(places)), "f")


def _date(v, what: str) -> date:
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        raise WalkForwardConfigError("INVALID_DATE", f"{what} must be an ISO date (YYYY-MM-DD)") from None


def normalise(body: Dict, base_cfg: Dict, universe: U.ResolvedUniverse, candidates: List[Dict], grid_spec: Optional[Dict]) -> Dict:
    """Validate and return the canonical, hashable walk-forward definition. `candidates` are the already generated,
    validated candidate rows ({label, config, config_hash}); `grid_spec` the explicit grid request (or None)."""
    if not isinstance(body, dict):
        raise WalkForwardConfigError("INVALID_BODY", "the walk-forward definition must be an object")
    start, end = _date(body.get("start_date"), "start_date"), _date(body.get("end_date"), "end_date")
    if end <= start:
        raise WalkForwardConfigError("INVALID_DATE_RANGE", "end_date must be after start_date")
    if (end - start).days > LIMITS["max_months"] * 31:
        raise WalkForwardConfigError("INVALID_DATE_RANGE", f"the range may not exceed {LIMITS['max_months']} months")
    train = _int(body.get("train_months", DEFAULTS["train_months"]), "train_months", 1, LIMITS["max_months"])
    test = _int(body.get("test_months", DEFAULTS["test_months"]), "test_months", 1, LIMITS["max_months"])
    step = _int(body.get("step_months") if body.get("step_months") is not None else test, "step_months", 1, LIMITS["max_months"])
    freq = str(body.get("rebalance_frequency") or DEFAULTS["rebalance_frequency"]).upper()
    if freq not in FREQUENCIES:
        raise WalkForwardConfigError("INVALID_FREQUENCY", f"rebalance_frequency must be one of {list(FREQUENCIES)}")
    metric = str(body.get("selection_metric") or DEFAULT_SELECTION).upper()
    if metric not in SELECTION_METRICS:
        raise WalkForwardConfigError("INVALID_SELECTION_METRIC", f"selection_metric must be one of {list(SELECTION_METRICS)}")
    cash = _dec(body.get("initial_cash", DEFAULTS["initial_cash"]), "initial_cash", BC.LIMITS["min_initial_cash"], BC.LIMITS["max_initial_cash"], "0.01")
    cost = _dec(body.get("transaction_cost_bps", DEFAULTS["transaction_cost_bps"]), "transaction_cost_bps", Decimal(0), BC.LIMITS["max_bps"], "0.0001")
    slip = _dec(body.get("slippage_bps", DEFAULTS["slippage_bps"]), "slippage_bps", Decimal(0), BC.LIMITS["max_bps"], "0.0001")
    min_train = _int(body.get("min_train_sessions", DEFAULTS["min_train_sessions"]), "min_train_sessions", 1, 5000)
    min_test = _int(body.get("min_test_sessions", DEFAULTS["min_test_sessions"]), "min_test_sessions", 1, 5000)
    max_cand = _int(body.get("max_candidates", DEFAULTS["max_candidates"]), "max_candidates", 1, LIMITS["max_candidates_cap"])
    dd_limit = _dec(body.get("max_drawdown_limit", DEFAULTS["max_drawdown_limit"]), "max_drawdown_limit", Decimal("-1"), Decimal("0"), "0.0001")
    if not candidates:
        raise WalkForwardConfigError("NO_CANDIDATES", "at least one valid candidate configuration is required")
    if not universe.symbols:
        raise WalkForwardConfigError("EMPTY_UNIVERSE", "the universe resolved to no symbols")
    return {"base_config_id": base_cfg["config_id"], "base_config_hash": base_cfg["config_hash"], "base_config": base_cfg["config"],
            "start_date": start.isoformat(), "end_date": end.isoformat(), "train_months": train, "test_months": test, "step_months": step,
            "overlapping_tests": step < test, "rebalance_frequency": freq, "selection_metric": metric, "max_drawdown_limit": dd_limit,
            "initial_cash": cash, "transaction_cost_bps": cost, "slippage_bps": slip, "benchmark": BENCHMARK, "execution_price": BC.EXECUTION_PRICE,
            "min_train_sessions": min_train, "min_test_sessions": min_test, "max_candidates": max_cand,
            "candidates": [{"label": c["label"], "config_hash": c["config_hash"], "config": c["config"]} for c in candidates],
            "grid_spec": grid_spec, "cost_matrix": [[_dec(a, "cost", Decimal(0), BC.LIMITS["max_bps"], "0.0001"), _dec(b, "slippage", Decimal(0), BC.LIMITS["max_bps"], "0.0001")] for a, b in COST_MATRIX],
            "universe_source": universe.source, "universe_ref": universe.ref, "universe_symbols": list(universe.symbols),
            "universe_hash": universe.universe_hash, "engine_version": ENGINE_VERSION, "backtest_version": BACKTEST_VERSION,
            "robustness_version": ROBUSTNESS_VERSION}


def definition_hash(canon: Dict) -> str:
    return sha256_hex(canonical_json(canon))


def public(canon: Dict) -> Dict:
    return {**canon, "wf_config_hash": definition_hash(canon), "universe_note": UNIVERSE_NOTE}
