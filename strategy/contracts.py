"""
strategy/contracts.py — record shapes later stages must use (DEFINITIONS ONLY; see CONTRACTS.md).

Nothing here runs a backtest, schedules anything or talks to a broker. These frozen dataclasses exist so Stage 3.2
and a future paper bot reference a strategy by (strategy_id, strategy_version_id, strategy_spec_hash) and record the
exact feature snapshot behind every decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class StrategyRef:
    strategy_id: str
    strategy_version_id: str
    version_number: int
    strategy_spec_hash: str
    feature_registry_fingerprint: str


@dataclass(frozen=True)
class BacktestEvaluationInput:
    """One historical decision point for Stage 3.2: information available at the close of `evaluation_date` only."""
    strategy: StrategyRef
    spec: Dict[str, Any]                  # the stored canonical spec, re-hashed before use
    symbol: str
    evaluation_date: str                  # the session T (ISO date); bars through its close, nothing later
    feature_snapshot: Dict[str, Any]      # built with strategy.features extractors from bars through T
    registry_version: int


@dataclass(frozen=True)
class PaperSignalRecord:
    strategy: StrategyRef
    symbol: str
    signal_timestamp: str                 # the close of T that produced the decision
    feature_snapshot: Dict[str, Any]
    decision: str                         # ENTER | EXIT | HOLD | SKIP
    evaluation_trace: List[Dict[str, Any]]


@dataclass(frozen=True)
class PaperTradeRecord:
    signal: PaperSignalRecord
    paper_order_id: Optional[str]
    paper_fill_price: Optional[float]
    paper_fill_time: Optional[str]
    paper_fill_quantity: Optional[float]
    exit_reason: Optional[str]            # CONDITION | INVALIDATION | TARGET | MAX_HOLDING
    outcome: Optional[Dict[str, Any]]     # filled only after the exit
