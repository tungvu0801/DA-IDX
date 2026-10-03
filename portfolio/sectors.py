"""
portfolio/sectors.py — Verified sector-classification sources for portfolio exposure (Stage 2.7D).

Resolution order (first source that knows the symbol wins):
  1. existing verified project source: data/sector_map.py (curated, the same map Stage 2 uses)
  2. official/company source        — none wired yet
  3. market-data provider already available to the project — none exposes sectors today
     (Alpaca market data has no sector field; Robinhood's get_equity_fundamentals is not on the
     gateway's read allowlist and would need a separate reviewed stage)

A symbol no verified source classifies is UNCLASSIFIED. Nothing here guesses, infers from the
ticker/company name, or uses model knowledge. This module never changes Stage 2 sector scoring.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol

from data.sector_map import get_sector_for_symbol

UNCLASSIFIED = "UNCLASSIFIED"


@dataclass(frozen=True)
class SectorClassification:
    symbol: str
    sector: str                 # a verified sector name, or UNCLASSIFIED
    source: Optional[str]       # id of the source that classified it; None when UNCLASSIFIED
    verified: bool


class SectorSource(Protocol):
    source_id: str

    def lookup(self, symbol: str) -> Optional[str]: ...


class CuratedSectorMapSource:
    """data/sector_map.py — the project's existing, hand-curated sector grouping."""

    source_id = "project_curated_sector_map"

    def __init__(self, lookup_fn: Callable[[str], Optional[str]] = get_sector_for_symbol):
        self._lookup = lookup_fn

    def lookup(self, symbol: str) -> Optional[str]:
        return self._lookup(symbol)


class SectorResolver:
    def __init__(self, sources: Optional[List[SectorSource]] = None):
        self.sources: List[SectorSource] = list(sources) if sources is not None else [CuratedSectorMapSource()]

    def classify(self, symbol: str) -> SectorClassification:
        sym = symbol.strip().upper()
        for source in self.sources:
            sector = source.lookup(sym)
            if sector:
                return SectorClassification(sym, sector, source.source_id, True)
        return SectorClassification(sym, UNCLASSIFIED, None, False)

    def sector_for(self, symbol: str) -> Optional[str]:
        """Adapter for the Stage 2.7C sector_fn signature: None means UNCLASSIFIED."""
        c = self.classify(symbol)
        return c.sector if c.verified else None

    def describe(self) -> dict:
        return {"sources": [s.source_id for s in self.sources],
                "note": "Only verified sources are used; unmapped symbols stay UNCLASSIFIED (no guessing)."}


default_resolver = SectorResolver()
