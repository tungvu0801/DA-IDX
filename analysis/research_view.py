"""
analysis/research_view.py — Deterministic Research View label, derived
purely from the Evidence Engine's net score (analysis/evidence_scoring.py).

This label is a research summary, NOT an instruction to execute a trade,
and Claude cannot override it — the label is computed here, before Claude
is ever called, and handed to Claude only to be explained.
"""
from typing import Optional

import config
from analysis.evidence_scoring import EvidenceResult

INSUFFICIENT_DATA = "INSUFFICIENT DATA"
STRONG_BULLISH = "STRONG BULLISH BIAS"
BULLISH = "BULLISH BIAS"
MIXED = "MIXED / WAIT"
BEARISH = "BEARISH BIAS"
STRONG_BEARISH = "STRONG BEARISH BIAS"


def research_view_label(evidence: EvidenceResult) -> str:
    """Map evidence.net -> one of the five labels above, or INSUFFICIENT_DATA."""
    return _label_for_net(evidence.net)


def _label_for_net(net: Optional[float]) -> str:
    if net is None:
        return INSUFFICIENT_DATA
    strong = config.RESEARCH_VIEW_STRONG_THRESHOLD
    lean = config.RESEARCH_VIEW_LEAN_THRESHOLD
    if net >= strong:
        return STRONG_BULLISH
    if net >= lean:
        return BULLISH
    if net <= -strong:
        return STRONG_BEARISH
    if net <= -lean:
        return BEARISH
    return MIXED
