"""
rotation/factors.py — Stage 4.7 PURE deterministic factor calculations (DESIGN_47_PORTFOLIO_ROTATION §5).

Inputs are ascending sequences of COMPLETED daily-session closes (and volumes) for ONE symbol; the LAST element is the
decision session T. Values are Decimals (the caller converts bar floats exactly once with `Decimal(str(x))` — see
`to_decimal`). Every factor is computed in a fixed Decimal context and quantised to 6 decimal places, so the same bars
always give the same strings. A factor whose inputs are missing, non-positive where a ratio needs them, or too short is
None (eligibility rule E7 later refuses such a symbol); nothing is ever guessed or filled in.

  ret20 / ret60       C_T / C_{T-k} - 1
  trend50 / trend200  C_T / SMA_n - 1
  relative_strength   (1 + ret60_symbol) / (1 + ret60_benchmark) - 1
  volatility          sample standard deviation of the latest 20 daily log returns (21 closes), annualised x sqrt(252)
  drawdown            C_T / max(C over the latest 252 sessions) - 1                     (<= 0)
  liquidity           mean(close x volume) over the latest 20 sessions
"""
from __future__ import annotations

from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Dict, Optional, Sequence

CTX = Context(prec=34, rounding=ROUND_HALF_EVEN)
PLACES = Decimal("0.000001")
RET_SHORT, RET_LONG = 20, 60
SMA_SHORT, SMA_LONG = 50, 200
VOL_SESSIONS = 20
DRAWDOWN_SESSIONS = 252
LIQUIDITY_SESSIONS = 20
ANNUALISATION_SESSIONS = 252
FACTOR_KEYS = ("ret20", "ret60", "trend50", "trend200", "relative_strength", "volatility", "drawdown", "liquidity")


def to_decimal(v) -> Optional[Decimal]:
    """The ONE conversion point for bar values: exact decimal of the value's shortest repr; None for missing / non-finite."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        d = v
    else:
        try:
            d = Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None
    return d if d.is_finite() else None


def q(d: Optional[Decimal]) -> Optional[Decimal]:
    return None if d is None else d.quantize(PLACES, rounding=ROUND_HALF_EVEN)


def _positive(values: Sequence[Decimal]) -> bool:
    return all(v is not None and v > 0 for v in values)


def simple_return(closes: Sequence[Decimal], sessions: int) -> Optional[Decimal]:
    """C_T / C_{T-sessions} - 1 (needs sessions + 1 closes, both positive)."""
    if len(closes) < sessions + 1:
        return None
    last, base = closes[-1], closes[-1 - sessions]
    if not _positive((last, base)):
        return None
    with localcontext(CTX):
        return q(last / base - 1)


def sma(closes: Sequence[Decimal], sessions: int) -> Optional[Decimal]:
    if len(closes) < sessions or not _positive(closes[-sessions:]):
        return None
    with localcontext(CTX):
        return sum(closes[-sessions:], Decimal(0)) / sessions


def trend(closes: Sequence[Decimal], sessions: int) -> Optional[Decimal]:
    """C_T / SMA_sessions - 1."""
    avg = sma(closes, sessions)
    if avg is None or avg <= 0 or not _positive(closes[-1:]):
        return None
    with localcontext(CTX):
        return q(closes[-1] / avg - 1)


def relative_strength(ret_symbol: Optional[Decimal], ret_benchmark: Optional[Decimal]) -> Optional[Decimal]:
    """(1 + ret_symbol) / (1 + ret_benchmark) - 1; None when either return is missing or the benchmark lost everything."""
    if ret_symbol is None or ret_benchmark is None or ret_benchmark <= -1:
        return None
    with localcontext(CTX):
        return q((1 + ret_symbol) / (1 + ret_benchmark) - 1)


def volatility(closes: Sequence[Decimal], sessions: int = VOL_SESSIONS) -> Optional[Decimal]:
    """Annualised sample standard deviation of the latest `sessions` daily log returns (needs sessions + 1 closes)."""
    if sessions < 2 or len(closes) < sessions + 1 or not _positive(closes[-(sessions + 1):]):
        return None
    with localcontext(CTX):
        window = closes[-(sessions + 1):]
        rets = [(window[i] / window[i - 1]).ln() for i in range(1, len(window))]
        mean = sum(rets, Decimal(0)) / len(rets)
        var = sum(((r - mean) ** 2 for r in rets), Decimal(0)) / (len(rets) - 1)
        return q(var.sqrt() * Decimal(ANNUALISATION_SESSIONS).sqrt())


def drawdown(closes: Sequence[Decimal], sessions: int = DRAWDOWN_SESSIONS) -> Optional[Decimal]:
    """C_T / max(C over the latest `sessions`) - 1 (<= 0)."""
    if len(closes) < sessions or not _positive(closes[-sessions:]):
        return None
    with localcontext(CTX):
        return q(closes[-1] / max(closes[-sessions:]) - 1)


def liquidity(closes: Sequence[Decimal], volumes: Sequence[Decimal], sessions: int = LIQUIDITY_SESSIONS) -> Optional[Decimal]:
    """Mean dollar volume (close x volume) over the latest `sessions`; volumes must be present and non-negative."""
    if len(closes) < sessions or len(volumes) < sessions or not _positive(closes[-sessions:]):
        return None
    vols = volumes[-sessions:]
    if any(v is None or v < 0 for v in vols):
        return None
    with localcontext(CTX):
        return q(sum((c * v for c, v in zip(closes[-sessions:], vols)), Decimal(0)) / sessions)


def compute_factors(closes: Sequence[Decimal], volumes: Sequence[Decimal],
                    benchmark_closes: Sequence[Decimal]) -> Dict[str, Optional[Decimal]]:
    """All raw factors of one symbol at T (the last close); each value is a 6-dp Decimal or None."""
    ret60 = simple_return(closes, RET_LONG)
    return {"ret20": simple_return(closes, RET_SHORT), "ret60": ret60,
            "trend50": trend(closes, SMA_SHORT), "trend200": trend(closes, SMA_LONG),
            "relative_strength": relative_strength(ret60, simple_return(benchmark_closes, RET_LONG)),
            "volatility": volatility(closes), "drawdown": drawdown(closes), "liquidity": liquidity(closes, volumes)}


def complete(factors: Dict[str, Optional[Decimal]]) -> bool:
    """True when every factor input is available (eligibility rule E7)."""
    return all(factors.get(k) is not None for k in FACTOR_KEYS)
