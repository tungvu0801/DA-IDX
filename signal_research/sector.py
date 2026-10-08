"""
signal_research/sector.py — static research sector map, sector-neutral ranking and sector-capped construction (DESIGN_52 §3).

Everything is deterministic and point-in-time trivial: the map is static (no future membership), the ranking uses only the
production scores of the decision session, and the cap never redistributes weight — it either selects differently or fails.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_EVEN
from typing import Dict, Iterable, List, Mapping, Sequence, Set

from rotation import rules as R
from rotation_diagnostics.attribution import UNMAPPED, sector_map_hash as _smh, sector_of as _sector_of

SIX = Decimal("0.000001")
EXIT_SECTOR_CAP, TOP_N_SECTOR_CAP, SECTOR_CAP_INFEASIBLE = "EXIT_SECTOR_CAP", "TOP_N_SECTOR_CAP", "SECTOR_CAP_INFEASIBLE"


class SectorRuleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def normalise_map(sector_map: Mapping[str, str]) -> Dict[str, str]:
    out = {}
    for k, v in (sector_map or {}).items():
        s, sec = str(k).upper().strip(), str(v).strip()
        if not s or not sec or len(sec) > 40:
            raise SectorRuleError("INVALID_SECTOR_MAP", f"invalid sector map entry {k!r}: {v!r}")
        out[s] = sec
    return dict(sorted(out.items()))


def sector_map_hash(sector_map: Mapping[str, str]) -> str:
    return _smh(normalise_map(sector_map))


def sector_of(symbol: str, sector_map: Mapping[str, str]) -> str:
    return _sector_of(symbol, sector_map)


# ---- sector-neutral ranking ---------------------------------------------------------------------------------------------------------------

def within_sector_percentiles(entries: Mapping[str, Mapping[str, Decimal]], sector_map: Mapping[str, str]) -> Dict[str, Decimal]:
    """Per symbol: (n − position) / (n − 1) × 100 inside its sector, position 1 = best by the production order; one name → 50."""
    groups: Dict[str, List[str]] = {}
    for s in entries:
        groups.setdefault(sector_of(s, sector_map), []).append(s)
    out: Dict[str, Decimal] = {}
    for sec, names in groups.items():
        ordered = R.rank_symbols({s: entries[s] for s in names})
        n = len(ordered)
        for pos, s in enumerate(ordered, start=1):
            out[s] = Decimal(50) if n == 1 else (Decimal(n - pos) / Decimal(n - 1) * 100).quantize(SIX, rounding=ROUND_HALF_EVEN)
    return out


def sector_neutral_rank(entries: Mapping[str, Mapping[str, Decimal]], sector_map: Mapping[str, str]) -> List[str]:
    """Best first: within-sector percentile desc, production composite desc, liquidity score desc, ticker asc."""
    pct = within_sector_percentiles(entries, sector_map)
    return sorted(entries, key=lambda s: (-pct[s], -entries[s]["composite"], -entries[s]["liquidity"], s))


# ---- sector cap ------------------------------------------------------------------------------------------------------------------------------

def cap_count(max_sector_weight: Decimal, equal_weight: Decimal) -> int:
    """Names allowed per sector: floor(max_sector_weight / equal_weight)."""
    if equal_weight <= 0:
        return 0
    return int((max_sector_weight / equal_weight).to_integral_value(rounding=ROUND_FLOOR))


def validate_cap(max_sector_weight: Decimal, equal_weight: Decimal, portfolio_size: int, sector_map: Mapping[str, str]) -> int:
    n = cap_count(max_sector_weight, equal_weight)
    if n < 1:
        raise SectorRuleError("SECTOR_CAP_BELOW_EQUAL_WEIGHT", f"max_sector_weight {max_sector_weight} is below the equal weight {equal_weight}: no name fits")
    sectors = len(set(normalise_map(sector_map).values()))
    if sectors * n < portfolio_size:
        raise SectorRuleError("SECTOR_CAP_INFEASIBLE", f"{sectors} sector(s) × {n} name(s) cannot fill {portfolio_size} slots")
    return n


def select_capped(ranked_best_first: Sequence[str], holdings: Iterable[str], portfolio_size: int, exit_rank: int, sector_map: Mapping[str, str], per_sector: int) -> Dict[str, object]:
    """The Stage 4.7 rank-buffer selection with at most `per_sector` names per sector. Holdings in rank order are retained while
    rank ≤ exit_rank, a slot is free and the sector is not full (else EXIT_SECTOR_CAP); free slots are filled by the best
    non-holdings whose sector is not full. `capped` lists the names skipped because of the cap; `infeasible` is True when the
    cap left slots empty that the uncapped selection would have filled."""
    rank_of = R.ranks(ranked_best_first)
    held: Set[str] = set(holdings)
    count: Dict[str, int] = {}
    exits: Dict[str, str] = {}
    keep: List[str] = []
    capped: List[str] = []
    for h in sorted(held, key=lambda s: (rank_of.get(s, 10 ** 9), s)):
        r = rank_of.get(h)
        sec = sector_of(h, sector_map)
        if r is None:
            exits[h] = R.EXIT_INELIGIBLE
        elif r > exit_rank:
            exits[h] = R.EXIT_RANK_ABOVE
        elif len(keep) >= portfolio_size:
            exits[h] = R.EXIT_OVERFLOW
        elif count.get(sec, 0) >= per_sector:
            exits[h] = EXIT_SECTOR_CAP
            capped.append(h)
        else:
            keep.append(h)
            count[sec] = count.get(sec, 0) + 1
    adds: List[str] = []
    for s in ranked_best_first:
        if len(keep) + len(adds) >= portfolio_size:
            break
        if s in held:
            continue
        sec = sector_of(s, sector_map)
        if count.get(sec, 0) >= per_sector:
            capped.append(s)
            continue
        adds.append(s)
        count[sec] = count.get(sec, 0) + 1
    selected = sorted(keep + adds, key=lambda s: rank_of[s])
    uncapped = R.select(ranked_best_first, holdings, portfolio_size, exit_rank)
    infeasible = len(selected) < min(portfolio_size, len(uncapped["selected"]))
    reasons = {}
    for s in selected:
        if s in held and rank_of[s] > portfolio_size:
            reasons[s] = R.RETAINED_RANK_BUFFER
        elif s not in held and any(rank_of[c] < rank_of[s] for c in capped):
            reasons[s] = TOP_N_SECTOR_CAP
        else:
            reasons[s] = R.TOP_N
    return {"selected": selected, "retained": sorted(keep, key=lambda s: rank_of[s]), "added": adds, "exits": exits, "reasons": reasons, "rank_of": rank_of,
            "capped": sorted(capped, key=lambda s: rank_of[s]), "infeasible": infeasible, "sector_counts": dict(sorted(count.items()))}


def sector_weights(weights: Mapping[str, Decimal], sector_map: Mapping[str, str]) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for s, w in weights.items():
        sec = sector_of(s, sector_map)
        out[sec] = out.get(sec, Decimal(0)) + w
    return dict(sorted(out.items()))


__all__ = ["UNMAPPED", "normalise_map", "sector_map_hash", "sector_of", "within_sector_percentiles", "sector_neutral_rank", "cap_count", "validate_cap", "select_capped",
           "sector_weights", "EXIT_SECTOR_CAP", "TOP_N_SECTOR_CAP", "SECTOR_CAP_INFEASIBLE", "SectorRuleError"]
