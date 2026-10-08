"""
signal_research/regime.py — point-in-time regime exposure overlay (DESIGN_52 §4).

The label at the decision session T comes from `rotation_walkforward.regimes.labels` on SPY bars dated <= T only (the series
is truncated here even if the caller already did). Exposure = schedule[trend|vol] in [0, 1]; UNKNOWN labels → 1.000000.
Applied after ranking and allocation: every selected weight × exposure (6 dp), the remainder is cash. No leverage, no shorting.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Dict, Mapping, Optional, Tuple

from backtest.replay import BarSeries
from rotation import rules as R
from rotation_backtest import simulator as SIM
from rotation_walkforward import regimes as RG

ONE = Decimal("1.000000")
SIX = Decimal("0.000001")


def label_at(spy: Optional[BarSeries], T: date) -> Dict[str, str]:
    """{trend, vol} at T from bars <= T (UNKNOWN when SPY has no bar at T or too few closes)."""
    ser = SIM.truncate(spy, T)
    if ser is None or not len(ser) or ser.dates[-1] != T:
        return {"trend": RG.UNKNOWN, "vol": RG.UNKNOWN}
    lab = RG.labels(ser)
    return dict(lab.get(T.isoformat()) or {"trend": RG.UNKNOWN, "vol": RG.UNKNOWN})


def exposure_for(label: Mapping[str, str], schedule: Mapping[str, str]) -> Decimal:
    key = f"{label.get('trend')}|{label.get('vol')}"
    if key not in schedule:
        return ONE                                              # UNKNOWN trend or vol: no information, no timing
    v = Decimal(str(schedule[key])).quantize(SIX, rounding=ROUND_HALF_EVEN)
    if not Decimal(0) <= v <= ONE:
        raise ValueError(f"exposure {key}={v} outside [0, 1]")
    return v


def exposure_at(spy: Optional[BarSeries], T: date, schedule: Mapping[str, str]) -> Tuple[Decimal, Dict[str, str]]:
    lab = label_at(spy, T)
    return exposure_for(lab, schedule), lab


def scale_weights(weights: Mapping[str, Decimal], exposure: Decimal) -> Tuple[Dict[str, Decimal], Decimal]:
    """(scaled weights, target cash weight): each weight × exposure quantised to 6 dp; cash = 1 − Σ. Equal weights stay equal."""
    scaled = {s: R.q(w * exposure) for s, w in weights.items()}
    total = sum(scaled.values(), Decimal(0))
    if total > ONE or any(w < 0 for w in scaled.values()):
        raise ValueError("scaled weights violate the no-leverage / no-shorting invariant")
    return scaled, R.q(ONE - total)
