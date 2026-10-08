"""
rotation_diagnostics/benchmarks.py — the universe benchmarks as Stage 4.8 replays of DERIVED Stage 4.7 configurations
(DESIGN_51 §2): identical fills, slippage, costs, whole shares, cash buffer and eligibility rules as the tested strategy.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List

from rotation import store as S
from rotation.store import canonical_json, sha256_hex

EW_REBALANCED, BUY_HOLD, SPY = "EW_REBALANCED", "BUY_HOLD", "SPY"
BENCHMARKS = (SPY, EW_REBALANCED, BUY_HOLD)
CONVENTIONS = {
    EW_REBALANCED: "portfolio_size = exit_rank = universe size: every eligible symbol at equal weight (1 - cash_buffer)/N; the base configuration's "
                   "rebalance_threshold keeps weights near equal at every rebalance (same frequency, next-open fills, slippage and cost as the strategy)",
    BUY_HOLD: "as EW_REBALANCED but rebalance_threshold = 1.000000: after the initial equal-weight deployment nothing is increased or decreased; a symbol "
              "that becomes ineligible is sold at the next rebalance and one that becomes eligible later is added at the equal target (existing rules, no hindsight)",
    SPY: "Stage 4.8 benchmark index: SPY buy-and-hold from the first session of each window, chain-linked across windows, no cost",
}


def benchmark_config(base_config: dict, n_symbols: int, kind: str) -> dict:
    """The derived configuration (canonical Stage 4.7 form) for EW_REBALANCED or BUY_HOLD over a universe of n_symbols."""
    if kind not in (EW_REBALANCED, BUY_HOLD):
        raise ValueError(kind)
    cfg = {"weights": dict(base_config["weights"]), "portfolio_size": int(n_symbols), "exit_rank": int(n_symbols), "cash_buffer_pct": base_config["cash_buffer_pct"],
           "rebalance_threshold": base_config["rebalance_threshold"] if kind == EW_REBALANCED else "1.000000", "max_turnover_per_rotation": "1.000000",
           "max_position_weight": "1.000000", "min_position_weight": "0.000000", "min_price": base_config["min_price"],
           "min_avg_dollar_volume": base_config["min_avg_dollar_volume"], "min_history_sessions": base_config["min_history_sessions"],
           "max_snapshot_age_min": base_config.get("max_snapshot_age_min", 30), "excluded_symbols": []}
    return S.normalise_config(cfg)


def benchmark_candidate(base_config: dict, n_symbols: int, kind: str) -> dict:
    canon = benchmark_config(base_config, n_symbols, kind)
    return {"label": kind, "config": canon, "config_hash": S.config_hash(canon), "kind": kind, "convention": CONVENTIONS[kind]}


def definition_hash(cand: dict) -> str:
    return sha256_hex(canonical_json({"kind": cand["kind"], "config_hash": cand["config_hash"], "convention": cand["convention"]}))


def spy_rows(stitched: List[dict], initial_cash: Decimal) -> List[dict]:
    """The SPY curve of a stitched OOS series as equity rows (benchmark_index → equity) for the shared metric helpers."""
    return [{"session_date": r["session_date"], "equity": Decimal(str(r["benchmark_index"])), "benchmark_index": Decimal(str(r["benchmark_index"])),
             "cash_weight": Decimal(0), "n_positions": 1, "window_index": r.get("window_index")} for r in stitched]
