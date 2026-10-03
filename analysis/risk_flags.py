"""
analysis/risk_flags.py — Deterministic risk-flag detection.

Every flag is a plain threshold check against fields analysis.indicators
already computed (plus optional sector/market context) — nothing here is
decided by Claude. The Risk Agent (agents/risk_agent.py) only explains
flags that this module already found; it never invents one.

Earnings-date risk is intentionally NOT a flag here: no real earnings-
calendar data source exists yet (data/events/earnings.py is an interface
stub), and guessing an earnings date would violate the project's
no-fabrication rule. It's surfaced instead as an explicit data-quality
caveat elsewhere.

Stage 2.6: the optional `events` parameter carries pre-built RiskFlag
entries from analysis/event_risk.py (EVENT_RISK_HIGH/MEDIUM,
MACRO_EVENT_IMMINENT, EARNINGS_EVENT_IMMINENT, CORPORATE_ACTION_IMMINENT,
EARNINGS_DATA_UNAVAILABLE) -- computed entirely by that module from real
event dates, never by this one. Defaulting to None keeps every pre-Stage-
2.6 call site unchanged.
"""
from dataclasses import dataclass
from typing import List, Optional

import config
from analysis import signals
from analysis.indicators import TickerMetrics
from analysis.sector_context import SectorContext


@dataclass
class RiskFlag:
    code: str
    severity: str  # "HIGH" | "MEDIUM" | "LOW"
    description: str  # short, factual — not AI-generated


def compute_risk_flags(
    m: TickerMetrics, sector: Optional[SectorContext] = None, event_flags: Optional[List[RiskFlag]] = None
) -> List[RiskFlag]:
    flags: List[RiskFlag] = []

    if m.atr is not None and m.price:
        atr_pct = m.atr / m.price * 100.0
        if atr_pct >= config.RISK_ATR_PCT_THRESHOLD:
            flags.append(
                RiskFlag("HIGH_ATR", "MEDIUM", f"Average daily price swing (ATR) is {atr_pct:.1f}% of price.")
            )

    if m.volatility_pct is not None and m.volatility_pct >= config.RISK_VOLATILITY_THRESHOLD:
        flags.append(
            RiskFlag(
                "HIGH_VOLATILITY", "MEDIUM", f"Annualized volatility is {m.volatility_pct:.0f}%, well above typical."
            )
        )

    if (
        m.volatility_expansion is not None
        and m.volatility_expansion >= config.VOLATILITY_EXPANSION_RATIO_THRESHOLD
    ):
        flags.append(
            RiskFlag(
                "VOLATILITY_EXPANDING",
                "LOW",
                f"Short-term volatility is running {m.volatility_expansion:.1f}x the longer-term average.",
            )
        )

    if m.gap_pct is not None and abs(m.gap_pct) >= config.RISK_LARGE_GAP_PCT:
        direction = "up" if m.gap_pct > 0 else "down"
        flags.append(RiskFlag("LARGE_GAP", "MEDIUM", f"Price gapped {direction} {abs(m.gap_pct):.1f}% at the open."))

    if (
        m.dist_from_high_pct is not None
        and m.dist_from_high_pct >= -config.APPROACHING_LEVEL_PCT
        and m.momentum_5d_pct is not None
        and m.momentum_5d_pct >= config.RISK_EXTENDED_MOMENTUM_PCT
    ):
        flags.append(
            RiskFlag(
                "EXTENDED_MOVE_UP",
                "HIGH",
                f"Price is near its 20-day high after a {m.momentum_5d_pct:.1f}% run over 5 days — the move looks extended.",
            )
        )
    if (
        m.dist_from_low_pct is not None
        and m.dist_from_low_pct <= config.APPROACHING_LEVEL_PCT
        and m.momentum_5d_pct is not None
        and m.momentum_5d_pct <= -config.RISK_EXTENDED_MOMENTUM_PCT
    ):
        flags.append(
            RiskFlag(
                "EXTENDED_MOVE_DOWN",
                "HIGH",
                f"Price is near its 20-day low after a {m.momentum_5d_pct:.1f}% decline over 5 days — the move looks extended.",
            )
        )

    if (
        m.dist_from_resistance_pct is not None
        and -config.APPROACHING_LEVEL_PCT <= m.dist_from_resistance_pct <= 0
    ):
        flags.append(
            RiskFlag("NEAR_RESISTANCE", "LOW", f"Price is within {config.APPROACHING_LEVEL_PCT:.0f}% of resistance (${m.resistance:.2f}).")
        )
    if m.dist_from_support_pct is not None and 0 <= m.dist_from_support_pct <= config.APPROACHING_LEVEL_PCT:
        flags.append(
            RiskFlag("NEAR_SUPPORT", "LOW", f"Price is within {config.APPROACHING_LEVEL_PCT:.0f}% of support (${m.support:.2f}).")
        )

    if (
        m.relative_volume is not None
        and m.relative_volume < config.RISK_LOW_RVOL_THRESHOLD
        and abs(m.pct_change) >= signals.STRONG_MOMENTUM_PCT
    ):
        flags.append(
            RiskFlag(
                "WEAK_VOLUME_CONFIRMATION",
                "MEDIUM",
                f"A {m.pct_change:+.1f}% move is happening on below-average volume ({m.relative_volume:.2f}x).",
            )
        )

    if m.rsi is not None:
        if m.trend == "Bullish" and m.rsi >= signals.RSI_OVERBOUGHT:
            flags.append(
                RiskFlag(
                    "CONFLICTING_SIGNALS",
                    "MEDIUM",
                    f"Trend is bullish but RSI ({m.rsi:.0f}) is overbought — momentum may be due for a pause.",
                )
            )
        elif m.trend == "Bearish" and m.rsi <= signals.RSI_OVERSOLD:
            flags.append(
                RiskFlag(
                    "CONFLICTING_SIGNALS",
                    "MEDIUM",
                    f"Trend is bearish but RSI ({m.rsi:.0f}) is oversold — a bounce is possible against the trend.",
                )
            )

    if sector is not None:
        if sector.sector_pct_change is not None and sector.sector_pct_change <= config.RISK_SECTOR_MARKET_WEAKNESS_PCT:
            flags.append(
                RiskFlag(
                    "SECTOR_WEAKNESS",
                    "MEDIUM",
                    f"The {sector.sector_name} sector ({sector.sector_etf}) is down {abs(sector.sector_pct_change):.1f}% today.",
                )
            )
        if sector.market_pct_change is not None and sector.market_pct_change <= config.RISK_SECTOR_MARKET_WEAKNESS_PCT:
            flags.append(
                RiskFlag(
                    "MARKET_WEAKNESS",
                    "MEDIUM",
                    f"The broad market ({sector.market_proxy_symbol}) is down {abs(sector.market_pct_change):.1f}% today.",
                )
            )

    if event_flags:
        flags.extend(event_flags)

    return flags


def risk_category_score(flags: List[RiskFlag]) -> float:
    """
    Turn the flag list into the Risk evidence-category score: always <= 0
    (risk only ever pulls toward caution/bearish, never counts as bullish
    evidence), and always available (0.0 = no flags triggered) since "no
    risk data" would wrongly hide real risk rather than being neutral.
    """
    severity_weights = config.RISK_FLAG_SEVERITY
    total = sum(severity_weights.get(f.severity, 0.0) for f in flags)
    return -min(1.0, total)
