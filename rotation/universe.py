"""
rotation/universe.py — Stage 4.7 explicit UNIVERSE resolution (DESIGN_47_PORTFOLIO_ROTATION §6 / §13).

A rotation run never scans arbitrary symbols. The universe is one of:
  WATCHLIST       the current watchlist (scanner.watchlist.load_watchlist); unreadable entries are ignored with a warning
  SAVED_SCAN      the IMMUTABLE latest snapshot of a saved strategy scan (fit.saved_scans) — its resolved symbols and each
                  symbol's stored scanner status (eligibility rule E10); the scanner is never re-run here
  SAVED_UNIVERSE  the saved universe of one exact strategy version (its stored spec)
  CUSTOM          an explicit list, validated here (invalid symbols are refused, never dropped)
Symbols are upper-cased, de-duplicated and sorted with the Stage 4.0 helper; more than fit.scanner.MAX_SYMBOLS is refused
(TOO_MANY_SYMBOLS), never truncated; nothing is added silently. `universe_hash` depends on (source, ref, sorted symbols)
only, so an equivalent list in any order hashes identically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from fit import current as FC
from fit import readonly as RO
from fit import scanner as SC
from rotation.store import canonical_json, sha256_hex

WATCHLIST, SAVED_SCAN, SAVED_UNIVERSE, CUSTOM = "WATCHLIST", "SAVED_SCAN", "SAVED_UNIVERSE", "CUSTOM"
SOURCES = (WATCHLIST, SAVED_SCAN, SAVED_UNIVERSE, CUSTOM)
MAX_SYMBOLS = SC.MAX_SYMBOLS


class UniverseError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class ResolvedUniverse:
    source: str
    ref: Optional[str]
    symbols: Tuple[str, ...]                                     # sorted, unique, validated
    universe_hash: str
    status_map: Dict[str, str]                                    # SAVED_SCAN only: symbol -> stored scanner status
    scan_session: Optional[str]                                   # SAVED_SCAN only: the snapshot's decision session
    warnings: Tuple[dict, ...]

    def rules_met(self, symbol: str) -> Optional[bool]:
        """SAVED_SCAN: whether the stored status is RULES MET; None for other sources (E10 does not apply)."""
        return None if self.source != SAVED_SCAN else self.status_map.get(symbol) == FC.RULES_MET


def universe_hash(source: str, ref: Optional[str], symbols: Sequence[str]) -> str:
    return sha256_hex(canonical_json({"source": source, "ref": ref, "symbols": sorted(set(symbols))}))


def _watchlist() -> List[str]:
    from scanner.watchlist import load_watchlist
    return list(load_watchlist())


def _version_universe(strategy_version_id: str, path: Path) -> List[str]:
    try:
        with RO.connect(path) as conn:
            row = conn.execute("SELECT spec_json FROM strategy_versions WHERE version_id = ?", (strategy_version_id,)).fetchone()
    except Exception:  # noqa: BLE001 - no strategy tables: nothing is saved
        row = None
    if row is None:
        raise UniverseError("UNKNOWN_REF", "That saved strategy version does not exist.", 404)
    try:
        return list(json.loads(row["spec_json"])["universe"]["symbols"])
    except (ValueError, KeyError, TypeError):
        raise UniverseError("UNKNOWN_REF", "That saved strategy version has no readable universe.", 409) from None


def resolve_universe(source: str, ref: Optional[str] = None, symbols: Optional[Sequence[str]] = None, *, path: Optional[Path] = None,
                     watchlist_fn: Optional[Callable[[], List[str]]] = None) -> ResolvedUniverse:
    if source not in SOURCES:
        raise UniverseError("INVALID_SOURCE", f"Unknown universe source {source!r}.")
    if source != CUSTOM and symbols:
        raise UniverseError("SYMBOLS_NOT_ACCEPTED", "Symbols are only accepted for a CUSTOM universe; the server resolves the others.")
    if source in (WATCHLIST, CUSTOM) and ref is not None:
        raise UniverseError("REF_NOT_ACCEPTED", f"A {source} universe takes no reference.")
    if source in (SAVED_SCAN, SAVED_UNIVERSE) and not ref:
        raise UniverseError("REF_REQUIRED", f"A {source} universe needs its reference id.")
    path = Path(path) if path else RO.db_path()
    warnings: List[dict] = []
    status_map: Dict[str, str] = {}
    scan_session = None
    if source == WATCHLIST:
        try:
            raw = (watchlist_fn or _watchlist)()
        except Exception:  # noqa: BLE001 - an unreadable watchlist is an empty universe, never a crash
            raw = []
        syms, bad = SC.normalise_symbols(raw, split=False)
        if bad:
            warnings.append({"code": "WATCHLIST_ENTRIES_IGNORED", "count": len(bad)})
    elif source == CUSTOM:
        syms, bad = SC.normalise_symbols(symbols or [], split=True)
        if bad:
            raise UniverseError("INVALID_SYMBOLS", f"Invalid symbols: {', '.join(str(b) for b in bad[:5])}.")
    elif source == SAVED_UNIVERSE:
        syms, bad = SC.normalise_symbols(_version_universe(ref, path), split=False)
        if bad:
            warnings.append({"code": "UNIVERSE_ENTRIES_IGNORED", "count": len(bad)})
    else:
        from fit import saved_scans as SS
        store = SS.SavedScanStore(path)
        if store.scan(ref) is None:
            raise UniverseError("UNKNOWN_REF", "That saved scan does not exist.", 404)
        snap = store.latest(ref)
        if snap is None:
            raise UniverseError("NO_SCAN_SNAPSHOT", "That saved scan has no stored check yet; run its check first.", 409)
        syms, bad = SC.normalise_symbols(snap["resolved_symbols"], split=False)
        status_map = {s: snap["status_map"].get(s) for s in syms}
        scan_session = snap.get("decision_session")
    if not syms:
        raise UniverseError("EMPTY_UNIVERSE", "The universe resolved to no symbols.")
    if len(syms) > MAX_SYMBOLS:
        raise UniverseError("TOO_MANY_SYMBOLS", f"{len(syms)} symbols; the limit is {MAX_SYMBOLS}. Nothing was truncated.")
    return ResolvedUniverse(source=source, ref=ref, symbols=tuple(syms), universe_hash=universe_hash(source, ref, syms),
                            status_map=status_map, scan_session=scan_session, warnings=tuple(warnings))
