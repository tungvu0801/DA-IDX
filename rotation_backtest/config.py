"""
rotation_backtest/config.py — the immutable BacktestConfig: validation, canonical form and hash (DESIGN_48 §4).

The definition binds an exact Stage 4.7 rotation configuration VERSION (config_id + config_hash), the date range, the
rebalance frequency, the initial cash, the cost assumptions, the fixed execution convention (NEXT_OPEN), the fixed
benchmark (SPY) and the RESOLVED universe (symbols + hash). Its sha256 identifies the definition; the same definition over
the same bars reproduces the same results.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from database import portfolio_backtest_migrations as M
from rotation import universe as U
from rotation.store import canonical_json, sha256_hex

from rotation_backtest import ENGINE_VERSION

FREQUENCIES = M.FREQUENCIES
BENCHMARK, EXECUTION_PRICE = M.BENCHMARK, M.EXECUTION_PRICE
DEFAULTS = {"rebalance_frequency": "MONTHLY", "initial_cash": "100000.00", "transaction_cost_bps": "5", "slippage_bps": "5"}
LIMITS = {"max_bps": Decimal("1000"), "max_years": 20, "min_initial_cash": Decimal("100"), "max_initial_cash": Decimal("1000000000")}
UNIVERSE_NOTE = ("Static universe supplied today: historical index membership is unknown, so these results are NOT free of "
                 "survivorship bias. Historical simulation, not a forecast.")


class BacktestConfigError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _date(v, what: str) -> date:
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        raise BacktestConfigError("INVALID_DATE", f"{what} must be an ISO date (YYYY-MM-DD)") from None


def _dec(v, what: str, lo: Decimal, hi: Decimal, places: str) -> str:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        raise BacktestConfigError("INVALID_DECIMAL", f"{what} must be a decimal number") from None
    if not d.is_finite() or d < lo or d > hi:
        raise BacktestConfigError("OUT_OF_RANGE", f"{what} must be between {lo} and {hi}")
    return format(d.quantize(Decimal(places)), "f")


def normalise(body: Dict, rotation_cfg: Dict, universe: U.ResolvedUniverse) -> Dict:
    """Validate and return the canonical, hashable backtest definition. `rotation_cfg` is the stored Stage 4.7 config row
    (config_id, config_hash, config); `universe` the resolved Stage 4.7 universe."""
    if not isinstance(body, dict):
        raise BacktestConfigError("INVALID_BODY", "the backtest definition must be an object")
    start, end = _date(body.get("start_date"), "start_date"), _date(body.get("end_date"), "end_date")
    if end < start:
        raise BacktestConfigError("INVALID_DATE_RANGE", "end_date must be on or after start_date")
    if (end - start).days > LIMITS["max_years"] * 366:
        raise BacktestConfigError("INVALID_DATE_RANGE", f"the range may not exceed {LIMITS['max_years']} years")
    freq = str(body.get("rebalance_frequency") or DEFAULTS["rebalance_frequency"]).upper()
    if freq not in FREQUENCIES:
        raise BacktestConfigError("INVALID_FREQUENCY", f"rebalance_frequency must be one of {list(FREQUENCIES)}")
    cash = _dec(body.get("initial_cash", DEFAULTS["initial_cash"]), "initial_cash", LIMITS["min_initial_cash"], LIMITS["max_initial_cash"], "0.01")
    cost = _dec(body.get("transaction_cost_bps", DEFAULTS["transaction_cost_bps"]), "transaction_cost_bps", Decimal(0), LIMITS["max_bps"], "0.0001")
    slip = _dec(body.get("slippage_bps", DEFAULTS["slippage_bps"]), "slippage_bps", Decimal(0), LIMITS["max_bps"], "0.0001")
    if not universe.symbols:
        raise BacktestConfigError("EMPTY_UNIVERSE", "the universe resolved to no symbols")
    return {"config_id": rotation_cfg["config_id"], "config_hash": rotation_cfg["config_hash"], "rotation_config": rotation_cfg["config"],
            "start_date": start.isoformat(), "end_date": end.isoformat(), "rebalance_frequency": freq, "initial_cash": cash,
            "transaction_cost_bps": cost, "slippage_bps": slip, "benchmark": BENCHMARK, "execution_price": EXECUTION_PRICE,
            "cash_interest": "0", "whole_shares": True, "universe_source": universe.source, "universe_ref": universe.ref,
            "universe_symbols": list(universe.symbols), "universe_hash": universe.universe_hash, "engine_version": ENGINE_VERSION}


def definition_hash(canon: Dict) -> str:
    return sha256_hex(canonical_json(canon))


def public(canon: Dict) -> Dict:
    return {**canon, "backtest_config_hash": definition_hash(canon), "universe_note": UNIVERSE_NOTE}
