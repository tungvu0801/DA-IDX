"""
comparison/cycles.py — forward REFERENCE CYCLES derived only from immutable Stage 3.3 rows.

Stage 3.3 stores no "cycle" row. A completed reference cycle is reassembled from exactly four stored records of one
(journal, symbol, cycle_no):

    ENTER observation  ->  reference ENTRY fill (FILLED)  ->  EXIT observation  ->  reference EXIT fill (FILLED)

Nothing is inferred: a missing piece leaves the cycle OPEN / INCOMPLETE (or CONTINUITY BLOCKED) and it is excluded
from every completed-cycle statistic. No market data is read, no fill is invented and no old decision is recomputed.

reference_move_pct = (reference exit open / reference entry open - 1) x 100 — long only, no slippage, no commission,
no sizing. Both opens are on ONE price basis: the exit capture's data (Stage 3.3 re-reads the entry session's open from
the same split / dividend adjusted dataset as the exit open and stores it in the exit fill's price_basis). The derived
value must equal the move Stage 3.3 stored; a difference is an integrity error, never silently replaced.
"""
from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Tuple

from forward import journal as J

COMPLETED = "COMPLETED"
OPEN_CYCLE = "OPEN_REFERENCE_CYCLE"
BLOCKED_CYCLE = "INCOMPLETE_CONTINUITY_BLOCKED"
ENTRY_UNFILLED = "ENTRY_UNFILLED"
STATUS_TEXT = {
    COMPLETED: "Completed reference cycle (reference entry and reference exit both stored).",
    OPEN_CYCLE: "OPEN / INCOMPLETE REFERENCE CYCLE — no reference exit is stored yet; excluded from completed-cycle "
                "statistics and never force-closed.",
    BLOCKED_CYCLE: "INCOMPLETE — a session was missed while this cycle was pending or open, so its lifecycle cannot be "
                   "known (CONTINUITY BLOCKED). Excluded from completed-cycle statistics; nothing is inferred.",
    ENTRY_UNFILLED: "The reference entry could not fill (no bar at the next session). Not a cycle.",
}
FORMULA = ("reference move % = (reference exit open / reference entry open - 1) x 100 — both opens on the exit "
           "capture's price basis; no slippage, no commission, no sizing")
TOLERANCE = 1e-9


class CycleIntegrityError(Exception):
    """The stored rows do not assemble into consistent cycles (duplicates, orphans or a stored move that differs from
    its own prices)."""


def reference_move_pct(exit_open: float, entry_open: float) -> float:
    return (exit_open / entry_open - 1.0) * 100.0


def _entry_basis(exit_fill: dict) -> Tuple[float, str]:
    """The entry open on the exit fill's price basis (what Stage 3.3 used), and which basis that is."""
    pb = exit_fill.get("price_basis") or {}
    if pb.get("basis") == "THIS_CAPTURE" and pb.get("entry_open_this_capture"):
        return pb["entry_open_this_capture"], "THIS_CAPTURE"
    return exit_fill["reference_entry_price"], "AS_CAPTURED"


def derive(obs: List[dict], fills: List[dict], blocked_symbols: List[str]) -> List[dict]:
    """Every reference cycle in one journal, ordered by symbol then cycle number. `obs` are compact observations
    (with lifecycle and exit reasons); `fills` are the journal's reference fills."""
    enters: Dict[Tuple[str, int], dict] = {}
    exits: Dict[Tuple[str, int], dict] = {}
    for o in obs:
        if o["decision"] not in ("ENTER", "EXIT"):
            continue
        key = (o["symbol"], int((o.get("lifecycle") or {}).get("cycle_no") or 0))
        if key[1] < 1:
            raise CycleIntegrityError(f"{o['decision']} observation {o['symbol']} {o['session_date']} has no cycle number.")
        book = enters if o["decision"] == "ENTER" else exits
        if key in book:
            raise CycleIntegrityError(f"Two {o['decision']} observations for {key[0]} cycle {key[1]}.")
        book[key] = o
    entry_fills: Dict[Tuple[str, int], dict] = {}
    exit_fills: Dict[Tuple[str, int], dict] = {}
    for f in fills:
        key = (f["symbol"], int(f["cycle_no"]))
        book = entry_fills if f["fill_type"] == "ENTRY" else exit_fills
        if key in book:
            raise CycleIntegrityError(f"Two reference {f['fill_type']} fills for {key[0]} cycle {key[1]}.")
        book[key] = f
    for key in set(entry_fills) | set(exits) | set(exit_fills):
        if key not in enters:
            raise CycleIntegrityError(f"{key[0]} cycle {key[1]} has stored fills or an exit without its ENTER observation.")
    blocked = set(blocked_symbols)
    out = []
    for key in sorted(enters):
        sym, n = key
        e, ef, x, xf = enters[key], entry_fills.get(key), exits.get(key), exit_fills.get(key)
        row = {"symbol": sym, "cycle_no": n, "entry_signal_session": e["session_date"],
               "reference_entry_session": None, "reference_entry_open": None, "exit_signal_session": None,
               "reference_exit_session": None, "reference_exit_open": None, "entry_open_on_exit_basis": None,
               "price_basis": None, "reference_move_pct": None, "stored_reference_move_pct": None,
               "holding_sessions": None, "exit_reasons": [], "exit_fill_delay_sessions": None, "unfilled_reason": None}
        if ef is not None:
            if ef["signal_session_date"] != e["session_date"]:
                raise CycleIntegrityError(f"{sym} cycle {n}: the entry fill points at another signal session.")
            if ef["status"] == "UNFILLED":
                out.append({**row, "status": ENTRY_UNFILLED, "unfilled_reason": ef["reason_code"]})
                continue
            row.update({"reference_entry_session": ef["fill_session_date"], "reference_entry_open": ef["reference_open_price"]})
        if x is not None:
            if ef is None or ef["status"] != "FILLED" or x["session_date"] < ef["fill_session_date"]:
                raise CycleIntegrityError(f"{sym} cycle {n}: an EXIT observation precedes its reference entry.")
            row.update({"exit_signal_session": x["session_date"], "exit_reasons": list(x.get("exit_reasons") or []),
                        "holding_sessions": (x.get("lifecycle") or {}).get("holding_sessions")})
        if xf is not None:
            if x is None or xf["signal_session_date"] != x["session_date"] or xf["status"] != "FILLED":
                raise CycleIntegrityError(f"{sym} cycle {n}: the reference exit does not match its EXIT observation.")
            if xf["reference_entry_price"] != ef["reference_open_price"]:
                raise CycleIntegrityError(f"{sym} cycle {n}: the exit fill records a different reference entry price.")
            basis_open, basis = _entry_basis(xf)
            move = reference_move_pct(xf["reference_open_price"], basis_open)
            stored = xf["reference_move_pct"]
            if stored is None or abs(move - stored) > TOLERANCE:
                raise CycleIntegrityError(f"{sym} cycle {n}: the stored reference move differs from its stored prices.")
            row.update({"reference_exit_session": xf["fill_session_date"], "reference_exit_open": xf["reference_open_price"],
                        "entry_open_on_exit_basis": basis_open, "price_basis": basis, "reference_move_pct": move,
                        "stored_reference_move_pct": stored, "exit_fill_delay_sessions": xf["delay_sessions"]})
            out.append({**row, "status": COMPLETED})
            continue
        out.append({**row, "status": BLOCKED_CYCLE if sym in blocked else OPEN_CYCLE})
    return out


def _median(xs: List[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def metrics(cycles: List[dict]) -> Optional[dict]:
    """Descriptive statistics of COMPLETED cycles only (None when there are none — never a 0 % placeholder)."""
    done = [c for c in cycles if c["status"] == COMPLETED]
    if not done:
        return None
    moves = [c["reference_move_pct"] for c in done]
    holds = [c["holding_sessions"] for c in done]
    holds_ok = all(isinstance(h, int) and h >= 1 for h in holds)
    n = len(done)
    pos = sum(1 for m in moves if m > 0)
    neg = sum(1 for m in moves if m < 0)
    return {"completed_cycles": n, "reference_moves_pct": moves,
            "average_reference_move_pct": sum(moves) / n, "median_reference_move_pct": _median(moves),
            "positive_cycles": pos, "negative_cycles": neg, "flat_cycles": n - pos - neg,
            "positive_cycle_rate_pct": pos / n * 100.0,
            "average_holding_sessions": (sum(holds) / n) if holds_ok else None,
            "median_holding_sessions": _median(holds) if holds_ok else None,
            "holding_note": None if holds_ok else "Unavailable — a stored cycle lacks its holding-session count.",
            "formula": FORMULA}


SAMPLE_BANDS = (
    (0, 0, "NO_COMPLETED_FORWARD_CYCLES", "NO COMPLETED FORWARD CYCLES"),
    (1, 9, "VERY_SMALL_FORWARD_SAMPLE", "VERY SMALL FORWARD SAMPLE — {n} completed reference cycle{s}."),
    (10, 29, "SMALL_FORWARD_SAMPLE", "SMALL FORWARD SAMPLE — {n} completed reference cycles."),
    (30, None, "FORWARD_SAMPLE_SIZE", "{n} completed reference cycles — a count, not statistical proof of anything."),
)


def sample(n: int) -> dict:
    for lo, hi, code, text in SAMPLE_BANDS:
        if n >= lo and (hi is None or n <= hi):
            return {"completed_cycles": n, "code": code, "text": text.format(n=n, s="" if n == 1 else "s"),
                    "note": "Sample-size labels describe how many completed cycles exist; they are not a quality rating."}
    raise ValueError(n)


def exit_reason_groups(cycles: List[dict]) -> List[dict]:
    """Completed cycles grouped by their stored exit reasons (MULTIPLE when more than one was true — the Stage 3.2
    grouping), alphabetically."""
    groups: Dict[str, int] = {}
    for c in cycles:
        if c["status"] != COMPLETED:
            continue
        r = c["exit_reasons"]
        g = "MULTIPLE" if len(r) > 1 else (r[0] if r else "UNKNOWN")
        groups[g] = groups.get(g, 0) + 1
    return [{"group": g, "completed_cycles": n} for g, n in sorted(groups.items())]


def exit_reason_appearances(cycles: List[dict]) -> Dict[str, int]:
    done = [c for c in cycles if c["status"] == COMPLETED]
    return {r: sum(1 for c in done if r in c["exit_reasons"]) for r in ("CONDITION", "INVALIDATION", "TARGET", "MAX_HOLDING")}


def latest_by_symbol(obs: List[dict]) -> Dict[str, dict]:
    """The most recent stored observation per symbol (same result as ForwardStore.latest_observations, from the
    compact rows already read)."""
    out: Dict[str, dict] = {}
    for o in obs:                                   # observations arrive ordered by session_date, symbol
        cur = out.get(o["symbol"])
        if cur is None or o["session_date"] >= cur["session_date"]:
            out[o["symbol"]] = o
    return out


STATE_OPEN = (J.ENTRY_PENDING, J.OPEN, J.EXIT_PENDING)
