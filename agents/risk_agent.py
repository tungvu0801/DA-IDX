"""
agents/risk_agent.py — Real Risk Agent.

Explains the deterministic risk flags already computed by
analysis/risk_flags.py in plain, beginner-friendly language. Claude does
NOT decide which risks apply — that's pure Python, computed before Claude
is ever called — it only translates already-triggered flags into
everyday language (e.g. "ATR expansion near resistance creates asymmetric
downside" becomes "the stock is moving more than usual and is already
close to an area where sellers showed up before").

Uses the same shared gating pipeline as the other agents.
"""
import json
import logging
from typing import List, Optional

from agents.gating import run_gated_agent
from agents.technical_agent import get_provider
from analysis.risk_flags import RiskFlag

logger = logging.getLogger(__name__)

NO_RISK_MESSAGE = "No notable risk flags identified from current data."

_SYSTEM_PROMPT = """You are a risk-explanation assistant for a beginner-friendly stock research dashboard.

You will be given a list of risk flags a Python program already detected for one stock,
plus any data-quality caveats (things that couldn't be checked). Do NOT add a risk that
isn't in the list, and do NOT remove one that is — your only job is to explain what the
given flags mean in plain, everyday language a first-time trader can understand.

Avoid jargon. For example, instead of "ATR expansion with proximity to resistance creates
asymmetric short-term downside," write something like "The stock is moving more than usual
and is already close to an area where sellers previously appeared, so buying after a large
jump could be riskier."

Write 1-2 short sentences per flag (combine closely related flags into one thought if that
reads more naturally), then end with one sentence covering any data-quality caveats. Do not
recommend buying, selling, holding, or trimming — only explain the risk."""


def _build_prompt(symbol: str, flags: List[RiskFlag], data_caveats: List[str]) -> str:
    payload = {
        "symbol": symbol,
        "flags": [{"code": f.code, "severity": f.severity, "description": f.description} for f in flags],
        "data_quality_caveats": data_caveats,
    }
    return "Explain these risk flags in plain language:\n\n" + json.dumps(payload, indent=2)


def get_risk_analysis(
    symbol: str,
    attention_score: int,
    price: float,
    signal: str,
    flags: List[RiskFlag],
    data_caveats: Optional[List[str]] = None,
    user_requested: bool = False,
) -> str:
    """
    Explain `flags` (already computed by analysis.risk_flags) in plain
    language, gated the same way as the other agents. Returns
    NO_RISK_MESSAGE immediately (no Claude call) if there are no flags and
    no caveats to explain.
    """
    data_caveats = data_caveats or []
    if not flags and not data_caveats:
        return NO_RISK_MESSAGE

    return run_gated_agent(
        symbol=symbol,
        price=price,
        attention_score=attention_score,
        signal=signal,
        analysis_type="risk",
        system_prompt=_SYSTEM_PROMPT,
        get_provider_fn=get_provider,
        build_prompt_fn=lambda: _build_prompt(symbol, flags, data_caveats),
        user_requested=user_requested,
    )
