"""
agents/beginner_agent.py — Combines the deterministic evidence engine,
Research View label, risk flags, catalyst analysis, and sector context
into one beginner-friendly research bundle (BeginnerResearch), and asks
Claude for the five beginner-mode sections (what's happening / why /
what looks good / be careful / what to watch).

Claude never invents a percentage, price level, catalyst, or verdict here
— it only writes plain-language sentences describing data it's handed.
The evidence percentages and Research View label are computed BEFORE
Claude is called and handed to it as facts to explain, not decisions for
it to make.
"""
import json
import logging
from dataclasses import dataclass
from typing import List, Optional

import config
from agents.catalyst_agent import NO_CATALYST_MESSAGE, CatalystAnalysis, get_catalyst_analysis
from agents.gating import UNAVAILABLE_MESSAGE, run_gated_agent, strip_markdown_fence
from agents.risk_agent import get_risk_analysis
from agents.technical_agent import get_provider
from analysis.evidence_scoring import (
    EvidenceResult,
    catalyst_score,
    compute_evidence,
    market_score,
    sector_score,
    technical_score,
)
from analysis.indicators import TickerMetrics
from analysis.research_view import research_view_label
from analysis.risk_flags import RiskFlag, compute_risk_flags, risk_category_score
from analysis.sector_context import SectorContext, compute_sector_context
from scanner.market_scanner import get_data_client
from scanner.watchlist import analyze_watchlist
from services.event_context import EventsBundle, build_event_context

logger = logging.getLogger(__name__)

_FALLBACK_NARRATIVE = {
    "whats_happening": "A plain-language summary isn't available right now — see Advanced Details for the numbers.",
    "why": "AI explanation unavailable.",
    "whats_good": [],
    "be_careful": [],
    "what_to_watch": "Check the Advanced Details section for the underlying data.",
}

_SYSTEM_PROMPT = """You are a beginner-friendly stock research assistant for a first-time trader.

You will be given a fully computed research bundle for one stock — its evidence percentages,
Research View label, technical metrics, risk flags (already explained), catalyst news (already
classified), and sector/market context. Everything in this bundle was computed or verified by
the application. Do not invent a price, percentage, catalyst, or event that isn't in the
bundle, and do not add outside knowledge of the company.

Using ONLY this data, write five short sections in plain, simple language a first-time trader
can understand — avoid technical jargon:

1. "whats_happening" — 1-2 sentences describing today's price/volume action in plain terms.
2. "why" — 1-2 sentences on the most likely explanation, drawing from the catalyst/sector/
technical data given. If nothing clearly explains the move, say so honestly instead of guessing.
3. "whats_good" — a list of 2-4 short bullet points: positives actually supported by the data.
4. "be_careful" — a list of 2-4 short bullet points: risks/things that could go wrong, drawn
from the risk flags given.
5. "what_to_watch" — 1 sentence suggesting what to watch next (e.g. a specific price level from
the data), without recommending a trade.

Do not recommend buying, selling, holding, or trimming anywhere in your response. Do not state
a probability of future price movement or a confidence level — the evidence percentages
describe current evidence only, not a forecast.

Respond with ONLY a JSON object with exactly these keys: whats_happening, why, whats_good
(array of strings), be_careful (array of strings), what_to_watch. No prose before or after.

If the bundle includes an "events" section, follow these rules exactly when mentioning it:
- Only discuss events actually listed in the bundle — never mention an earnings date, economic
  release, or corporate action that isn't there, and never fill a gap from your own knowledge.
- Never invent or infer a missing event date or time. If earnings data is marked unavailable,
  say so plainly instead of guessing when earnings might be.
- Never call a scheduled macro event (CPI, jobs report, FOMC decision) bullish or bearish by
  default — its market impact depends on the data itself, which isn't known in advance.
- Keep "an event is scheduled," "what actually happens when it's released," and "how the market
  might react" as three separate ideas — do not collapse them into one prediction."""


@dataclass
class DataQuality:
    level: str  # HIGH | MEDIUM | LOW
    explanation: str


@dataclass
class BeginnerNarrative:
    whats_happening: str
    why: str
    whats_good: List[str]
    be_careful: List[str]
    what_to_watch: str


@dataclass
class BeginnerResearch:
    symbol: str
    metrics: TickerMetrics
    evidence: EvidenceResult
    research_view: str
    sector: Optional[SectorContext]
    catalyst: CatalystAnalysis
    risk_flags: List[RiskFlag]
    risk_explanation: str
    data_quality: DataQuality
    narrative: BeginnerNarrative
    events: Optional[EventsBundle] = None


def _compute_data_quality(catalyst: CatalystAnalysis, sector: Optional[SectorContext]) -> DataQuality:
    catalyst_checked = catalyst.message in ("", None) or catalyst.message == NO_CATALYST_MESSAGE
    sector_available = sector is not None

    if catalyst_checked and sector_available:
        return DataQuality(
            "HIGH",
            "Current price, volume, technical data, sector information, and recent news were all available.",
        )
    if catalyst_checked or sector_available:
        missing = "sector/peer comparison" if catalyst_checked else "a current news check"
        return DataQuality("MEDIUM", f"Technical data is available, but {missing} could not be completed.")
    return DataQuality(
        "LOW", "No clear current catalyst or sector context was available, so this relies mainly on technical data."
    )


def _build_narrative_prompt(
    m: TickerMetrics,
    evidence: EvidenceResult,
    view_label: str,
    sector: Optional[SectorContext],
    catalyst: CatalystAnalysis,
    flags: List[RiskFlag],
    events: Optional[EventsBundle] = None,
) -> str:
    bundle = {
        "symbol": m.symbol,
        "price": m.price,
        "daily_change_percent": round(m.pct_change, 2),
        "signal": m.signal,
        "evidence": {"bullish_pct": evidence.bullish_pct, "neutral_pct": evidence.neutral_pct, "bearish_pct": evidence.bearish_pct},
        "research_view": view_label,
        "sector_context": (
            {
                "sector_name": sector.sector_name,
                "sector_pct_change": sector.sector_pct_change,
                "stock_vs_sector_pct": sector.stock_vs_sector_pct,
                "market_pct_change": sector.market_pct_change,
            }
            if sector is not None
            else None
        ),
        "catalysts": [
            {"title": c.title, "sentiment": c.sentiment, "reason": c.reason} for c in catalyst.items
        ],
        "catalyst_note": catalyst.message or None,
        "risk_flags": [{"code": f.code, "severity": f.severity, "description": f.description} for f in flags],
        "support": m.support,
        "resistance": m.resistance,
        "events": (
            {
                "event_risk_level": events.event_risk_level,
                "nearest_event": (
                    {
                        "title": events.nearest_event.title,
                        "event_type": events.nearest_event.event_type.value,
                        "event_date": events.nearest_event.event_date.isoformat(),
                        "time_precision": events.nearest_event.time_precision.value,
                        "source": events.nearest_event.source,
                    }
                    if events.nearest_event is not None
                    else None
                ),
                "earnings_available": events.earnings_available,
                "earnings_note": events.earnings_reason,
            }
            if events is not None
            else None
        ),
    }
    return "Research bundle:\n\n" + json.dumps(bundle, indent=2, default=str)


def _parse_narrative(raw_text: str) -> Optional[BeginnerNarrative]:
    try:
        parsed = json.loads(strip_markdown_fence(raw_text))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return BeginnerNarrative(
            whats_happening=str(parsed["whats_happening"]),
            why=str(parsed["why"]),
            whats_good=[str(x) for x in parsed.get("whats_good", [])],
            be_careful=[str(x) for x in parsed.get("be_careful", [])],
            what_to_watch=str(parsed["what_to_watch"]),
        )
    except (KeyError, TypeError):
        return None


def build_beginner_research(symbol: str, user_requested: bool = True) -> Optional[BeginnerResearch]:
    """
    Assemble the full beginner research bundle for `symbol`. Returns None
    if basic market data for the symbol couldn't be fetched at all (the
    caller should treat that as a 404 — there's nothing to build a bundle
    around). Every AI-dependent piece degrades to a clear message rather
    than failing the whole bundle.
    """
    symbol = symbol.strip().upper()
    try:
        client = get_data_client()
    except RuntimeError:
        return None

    metrics = analyze_watchlist(client, [symbol])
    m = metrics.get(symbol)
    if m is None:
        return None

    sector = compute_sector_context(symbol, m.pct_change, client)
    catalyst = get_catalyst_analysis(symbol, m.attention_score, m.price, m.signal, user_requested=user_requested)

    try:
        events = build_event_context(symbol)
    except Exception as exc:  # noqa: BLE001 - the event layer must never break the whole research bundle
        logger.warning("Event context failed for %s: %s", symbol, exc)
        events = None

    flags = compute_risk_flags(m, sector, event_flags=events.event_risk_flags if events is not None else None)
    earnings_caveat = (
        events.earnings_reason
        if events is not None and not events.earnings_available and events.earnings_reason
        else "Earnings-calendar data is not currently available from a verified provider."
    )
    risk_explanation = get_risk_analysis(
        symbol, m.attention_score, m.price, m.signal, flags, data_caveats=[earnings_caveat], user_requested=user_requested
    )

    sentiments = [c.sentiment for c in catalyst.items]
    scores = {
        "technical": technical_score(m),
        "catalyst": catalyst_score(sentiments),
        "risk": risk_category_score(flags),
        "market": market_score(sector),
        "sector": sector_score(sector),
    }
    evidence = compute_evidence(scores)
    view_label = research_view_label(evidence)
    data_quality = _compute_data_quality(catalyst, sector)

    narrative = _generate_narrative(m, evidence, view_label, sector, catalyst, flags, events, user_requested)

    return BeginnerResearch(
        symbol=symbol,
        metrics=m,
        evidence=evidence,
        research_view=view_label,
        sector=sector,
        catalyst=catalyst,
        risk_flags=flags,
        risk_explanation=risk_explanation,
        data_quality=data_quality,
        narrative=narrative,
        events=events,
    )


def _generate_narrative(
    m: TickerMetrics,
    evidence: EvidenceResult,
    view_label: str,
    sector: Optional[SectorContext],
    catalyst: CatalystAnalysis,
    flags: List[RiskFlag],
    events: Optional[EventsBundle],
    user_requested: bool,
) -> BeginnerNarrative:
    raw = run_gated_agent(
        symbol=m.symbol,
        price=m.price,
        attention_score=m.attention_score,
        signal=m.signal,
        analysis_type="beginner_narrative",
        system_prompt=_SYSTEM_PROMPT,
        get_provider_fn=get_provider,
        build_prompt_fn=lambda: _build_narrative_prompt(m, evidence, view_label, sector, catalyst, flags, events),
        user_requested=user_requested,
        max_tokens=1200,
    )

    if raw == UNAVAILABLE_MESSAGE or raw.startswith("AI analysis"):
        return BeginnerNarrative(
            whats_happening=raw,
            why=_FALLBACK_NARRATIVE["why"],
            whats_good=[],
            be_careful=[],
            what_to_watch=_FALLBACK_NARRATIVE["what_to_watch"],
        )

    json_text = raw.split("\n\n(Cached analysis")[0]
    parsed = _parse_narrative(json_text)
    if parsed is None:
        logger.warning("Beginner narrative returned malformed JSON for %s.", m.symbol)
        return BeginnerNarrative(**_FALLBACK_NARRATIVE)
    return parsed
