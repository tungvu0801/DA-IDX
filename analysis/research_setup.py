"""
analysis/research_setup.py — Deterministic research-setup arithmetic
(entry zone, invalidation, targets, risk/reward), populating the schema
models.schemas.ResearchSetupResponse prepared in Milestone 1.

Every number here comes straight from analysis.indicators.TickerMetrics
(support/resistance/ATR/price) via plain arithmetic. Claude may explain
these numbers in words; it never invents or adjusts a price level. If a
required input (e.g. no support level found) is missing, the corresponding
output is left as None rather than guessed.
"""
from dataclasses import dataclass, field
from typing import List, Optional

import config
from analysis.indicators import TickerMetrics


@dataclass
class ResearchSetup:
    symbol: str
    time_horizon: str
    entry_low: Optional[float]
    entry_high: Optional[float]
    invalidation_level: Optional[float]
    support: List[float] = field(default_factory=list)
    resistance: List[float] = field(default_factory=list)
    possible_targets: List[float] = field(default_factory=list)
    risk_per_share: Optional[float] = None
    reward_per_share: Optional[float] = None
    risk_reward_ratio: Optional[float] = None


def compute_research_setup(m: TickerMetrics, time_horizon: str = config.RESEARCH_SETUP_DEFAULT_HORIZON) -> ResearchSetup:
    """
    Entry zone: between support and current price (or a small band around
    price if no support was found — never invents a support level).
    Invalidation: just below support, by RESEARCH_SETUP_INVALIDATION_BUFFER_PCT.
    Targets: resistance, plus an ATR-projected secondary target beyond it
    when ATR is available.
    """
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    invalidation: Optional[float] = None
    support_list: List[float] = []
    resistance_list: List[float] = []
    targets: List[float] = []

    if m.support is not None:
        support_list = [round(m.support, 2)]
        entry_low = round(m.support, 2)
        entry_high = round(m.price, 2)
        invalidation = round(m.support * (1 - config.RESEARCH_SETUP_INVALIDATION_BUFFER_PCT / 100.0), 2)

    if m.resistance is not None:
        resistance_list = [round(m.resistance, 2)]
        targets.append(round(m.resistance, 2))
        if m.atr is not None:
            targets.append(round(m.resistance + m.atr, 2))

    risk_per_share = None
    reward_per_share = None
    risk_reward_ratio = None
    if invalidation is not None and entry_high is not None:
        risk_per_share = round(entry_high - invalidation, 2)
        if targets and risk_per_share and risk_per_share > 0:
            reward_per_share = round(targets[0] - entry_high, 2)
            if reward_per_share > 0:
                risk_reward_ratio = round(reward_per_share / risk_per_share, 2)

    return ResearchSetup(
        symbol=m.symbol,
        time_horizon=time_horizon,
        entry_low=entry_low,
        entry_high=entry_high,
        invalidation_level=invalidation,
        support=support_list,
        resistance=resistance_list,
        possible_targets=targets,
        risk_per_share=risk_per_share,
        reward_per_share=reward_per_share,
        risk_reward_ratio=risk_reward_ratio,
    )
