"""
database/fingerprint.py — Deterministic snapshot fingerprint for
deduplication. Pure function, no I/O, no database access.

Deliberately EXCLUDES `created_at` — that's exactly the sub-second/
sub-minute noise a double-click or an accidental duplicate save must be
allowed to collapse into "the same snapshot." Price is rounded to
config.FINGERPRINT_PRICE_DECIMALS (2 by default) only to absorb
floating-point pipeline artifacts (e.g. 191.50000000001) — real equity
prices are already cent-quantized, so this never merges two genuinely
different prices. A snapshot taken hours later is naturally saved as
distinct the moment ANY substantive field differs (price having moved,
attention score, evidence split, a new/changed catalyst or risk flag, a
different research setup) — if literally nothing changed, treating it as
a duplicate is correct: nothing new was learned.
"""
import hashlib
import json
from typing import Any, Dict, List, Optional

import config


def compute_fingerprint(
    *,
    symbol: str,
    market_timestamp: str,
    price_source: str,
    price: float,
    attention_score: Optional[int],
    research_view: str,
    bullish_pct: int,
    neutral_pct: int,
    bearish_pct: int,
    catalyst_identifiers: List[str],
    risk_flag_codes: List[str],
    setup_levels: Optional[Dict[str, Optional[float]]],
    event_identifiers: Optional[List[str]] = None,
    event_risk_level: Optional[str] = None,
) -> str:
    """
    `catalyst_identifiers` should be e.g. f"{source}|{title}|{published_at}"
    per catalyst (caller builds these — this function just sorts/hashes).
    `setup_levels` is a dict of the setup's price levels (entry_low,
    entry_high, invalidation, target_1, target_2) or None if no setup was
    generated for this snapshot — presence/absence of a setup is itself
    part of the snapshot's identity.

    Stage 2.6: `event_identifiers` should be a STABLE identity per nearby
    event -- e.g. f"{event_type}|{event_date}|{symbol_or_blank}|
    {source_provider}|{raw_reference_id}" (caller builds these). Per review
    correction, this deliberately EXCLUDES volatile fields (retrieved_at,
    cache age, any transient provider-status text) -- including those would
    make two fingerprint runs of IDENTICAL underlying research produce
    different hashes merely because the event provider was re-queried,
    creating spurious duplicate snapshots.
    """
    decimals = config.FINGERPRINT_PRICE_DECIMALS

    canonical: Dict[str, Any] = {
        "symbol": symbol.strip().upper(),
        "market_timestamp": market_timestamp,
        "price_source": price_source,
        "price": round(price, decimals),
        "attention_score": attention_score,
        "research_view": research_view,
        "bullish_pct": bullish_pct,
        "neutral_pct": neutral_pct,
        "bearish_pct": bearish_pct,
        "catalysts": sorted(c.lower() for c in catalyst_identifiers),
        "risk_flags": sorted(set(risk_flag_codes)),
        "setup": (
            {k: (round(v, decimals) if v is not None else None) for k, v in sorted(setup_levels.items())}
            if setup_levels is not None
            else None
        ),
        "events": sorted(set(event_identifiers)) if event_identifiers is not None else None,
        "event_risk_level": event_risk_level,
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
