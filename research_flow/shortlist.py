"""
research_flow/shortlist.py — deterministic SHORTLIST-BEFORE-LLM selection from a Stage 4.7 rotation run. Pure.

Claude never sees the whole universe. From the run's ranked, eligible candidates the shortlist is:
  TOP_N               the top `top_n` ranks (default 5)
  HELD_DETERIORATION  a currently held name whose rank worsened by >= `held_rank_change_min` since the previous run
  RANK_MOVER          any name whose rank moved by >= `mover_rank_change_min` since the previous run
  USER_SELECTED       symbols the user explicitly asked for (only if the run ranked them)
Entries are ordered by rank, then symbol; more than `max_symbols_per_research_run` is cut deterministically (lowest
ranks first) and every cut or unranked symbol is reported as skipped with a machine-readable reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DEFAULTS = {"top_n": 5, "max_symbols_per_research_run": 8, "held_rank_change_min": 3, "mover_rank_change_min": 5}
TOP_N, HELD_DETERIORATION, RANK_MOVER, USER_SELECTED = "TOP_N", "HELD_DETERIORATION", "RANK_MOVER", "USER_SELECTED"


@dataclass(frozen=True)
class ShortlistEntry:
    symbol: str
    rank: int
    score: Optional[str]
    rank_change: Optional[int]                   # previous rank − current rank (positive = improved)
    reasons: Tuple[str, ...]
    position: Dict[str, Optional[str]] = field(default_factory=dict)


def settings(overrides: Optional[Mapping] = None) -> dict:
    cfg = {**DEFAULTS, **{k: v for k, v in (overrides or {}).items() if k in DEFAULTS}}
    for k, v in cfg.items():
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(f"{k} must be an integer >= 1")
    return cfg


def build_shortlist(candidates: Sequence[Mapping], items: Sequence[Mapping], previous_ranks: Optional[Mapping[str, int]],
                    user_symbols: Iterable[str] = (), config: Optional[Mapping] = None) -> Tuple[List[ShortlistEntry], List[dict]]:
    cfg = settings(config)
    ranked = sorted((c for c in candidates if c.get("eligible") and c.get("rank") is not None), key=lambda c: (int(c["rank"]), c["symbol"]))
    by_item = {i["symbol"]: i for i in items}
    held = {s for s, i in by_item.items() if i.get("current_qty") not in (None, "0", 0)}
    users = sorted({str(s).strip().upper() for s in user_symbols if str(s).strip()})
    reasons: Dict[str, List[str]] = {}
    change: Dict[str, Optional[int]] = {}
    for c in ranked:
        s, r = c["symbol"], int(c["rank"])
        prev = (previous_ranks or {}).get(s)
        change[s] = (int(prev) - r) if prev is not None else None
        why = []
        if r <= cfg["top_n"]:
            why.append(TOP_N)
        if s in held and change[s] is not None and -change[s] >= cfg["held_rank_change_min"]:
            why.append(HELD_DETERIORATION)
        if change[s] is not None and abs(change[s]) >= cfg["mover_rank_change_min"] and RANK_MOVER not in why:
            why.append(RANK_MOVER)
        if s in users:
            why.append(USER_SELECTED)
        if why:
            reasons[s] = why
    skipped = [{"symbol": s, "reason": "NOT_RANKED"} for s in users if s not in {c["symbol"] for c in ranked}]
    entries = [ShortlistEntry(symbol=c["symbol"], rank=int(c["rank"]), score=c.get("composite"), rank_change=change[c["symbol"]],
                              reasons=tuple(reasons[c["symbol"]]),
                              position={"held": "yes" if c["symbol"] in held else "no",
                                        "current_qty": None if c["symbol"] not in by_item else str(by_item[c["symbol"]].get("current_qty")),
                                        "proposal_action": None if c["symbol"] not in by_item else by_item[c["symbol"]].get("action")})
               for c in ranked if c["symbol"] in reasons]
    cap = cfg["max_symbols_per_research_run"]
    skipped += [{"symbol": e.symbol, "reason": "SHORTLIST_CAP"} for e in entries[cap:]]
    return entries[:cap], skipped
