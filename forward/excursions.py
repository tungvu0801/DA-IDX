"""
forward/excursions.py — Stage 3.6 FORWARD MFE / MAE of reference cycles, observed going forward only.

While a forward reference cycle is open, each captured session adds ONE immutable excursion row (table
forward_test_excursions, database/excursion_migrations.py) with the same conventions as Stage 3.2's simulated trades
(backtest/engine.py: max / min start at the entry open, the entry session's full high / low, full ranges of later held
sessions, only the OPEN of the exit session; MFE = max(0, ...), MAE = min(0, ...)):

    ENTRY_SESSION  the reference-entry session: its open is the reference entry; its full high / low count
    HELD_SESSION   each later captured session while the cycle is open (including the exit-signal session): full high / low
    EXIT_OPEN      the reference-exit session: ONLY its open counts (the cycle ended at that open; its later range never)
    CONTINUITY_GAP a session was missed while the cycle was open (Stage 3.3 blocks the symbol): tracking is INCOMPLETE

    session high % = (high / entry open - 1) x 100      session low % = (low / entry open - 1) x 100
    exit open %    = (exit open / entry open - 1) x 100 (both the session's high % and low %)
    cumulative MFE = max(0, every session high %)      cumulative MAE = min(0, every session low %)

Price basis (Stage 3.3, reused, never re-invented): each capture reads split / dividend adjusted bars, so every
percentage uses the entry session's open from the SAME dataset as the session it is compared with (the exit row uses
exactly the basis Stage 3.3 stored with the reference exit). A split during an open cycle therefore cannot create a
fake excursion.

No backfill: only cycles whose reference entry is captured after tracking started for the journal are tracked, and a
row is only ever written for the session being captured, from bars through that session. A captured session without a
bar for the symbol adds nothing (Stage 3.2 counts no such day either) and is recorded as EXCURSION_DATA_UNAVAILABLE; a
missing price basis ends tracking as INCOMPLETE_DATA_UNAVAILABLE. No position size, dollars, costs or orders.
"""
from __future__ import annotations

import statistics
from datetime import date
from typing import Dict, List, Optional, Tuple

ENGINE_VERSION = "3.6.0"
FLAT, ENTRY_PENDING, OPEN, EXIT_PENDING, BLOCKED = "FLAT", "ENTRY_PENDING", "OPEN", "EXIT_PENDING", "CONTINUITY_BLOCKED"
TRACKING, COMPLETE = "TRACKING", "COMPLETE"
GAP, DATA_UNAVAILABLE = "INCOMPLETE_DUE_TO_CONTINUITY_GAP", "INCOMPLETE_DATA_UNAVAILABLE"
LEGACY = "LEGACY_NOT_TRACKED"
NOT_ENTERED = "NOT_ENTERED"
STATUS_TEXT = {
    TRACKING: "COMPLETE SO FAR — every session since the reference entry was observed; not final while the cycle is open.",
    COMPLETE: "Tracked from the reference entry through the reference exit open.",
    GAP: "MFE / MAE tracking incomplete due to a continuity gap — the missed session is never reconstructed, so no final "
         "MFE / MAE exists for this cycle.",
    DATA_UNAVAILABLE: "MFE / MAE tracking incomplete — a price needed on a consistent price basis was unavailable.",
    LEGACY: "Not tracked for this legacy forward cycle (it started before Stage 3.6 MFE / MAE tracking).",
}
FORMULAS = {
    "session_high_pct": "(session high / reference entry open - 1) x 100, both from the same capture's dataset",
    "session_low_pct": "(session low / reference entry open - 1) x 100, both from the same capture's dataset",
    "exit_open_pct": "(reference exit open / reference entry open - 1) x 100 — the exit session's later range is not used",
    "mfe_pct": "max(0, every session high % from the reference entry session through the exit-signal session, and the "
               "exit open %)",
    "mae_pct": "min(0, every session low % over the same sessions, and the exit open %)",
}


def _d(s: Optional[str]) -> Optional[date]:
    return date.fromisoformat(s) if s else None


def _pct(price: float, basis: float) -> float:
    return (price / basis - 1.0) * 100.0


def last_by_cycle(rows: List[dict]) -> Dict[Tuple[str, int], dict]:
    """The latest stored excursion row of every tracked cycle (rows ordered by session, then insertion order)."""
    order = {"ENTRY_SESSION": 0, "HELD_SESSION": 1, "CONTINUITY_GAP": 2, "EXIT_OPEN": 3}
    out: Dict[Tuple[str, int], dict] = {}
    for r in sorted(rows, key=lambda r: (r["session_date"], order[r["observation_type"]])):
        out[(r["symbol"], r["cycle_no"])] = r
    return out


def rows_for(sym: str, T: date, prev_obs: Optional[dict], obs: dict, fills: List[dict], series, ds: Optional[dict],
             last: Dict[Tuple[str, int], dict], captured_at: str) -> List[dict]:
    """The excursion rows ONE captured session T adds for one symbol. Pure: reads only bars dated T and the cycle's
    entry session (both <= T) from this capture's dataset, the previous observation and the fills Stage 3.3 made."""
    rows: List[dict] = []
    t = T.isoformat()
    prev_state = prev_obs["state_after"] if prev_obs else FLAT
    plc = (prev_obs or {}).get("lifecycle") or {}
    bar = series.bar(T) if series is not None else None
    ref = {"dataset_id": ds["dataset_id"] if ds else None, "content_hash": ds["content_hash"] if ds else None}

    def row(kind, status, tracking, entry_session, entry_price, prev, reason=None, basis=None, basis_kind=None,
            o=None, h=None, low=None, hp=None, lp=None, data=True):
        mfe = prev["cumulative_mfe_pct"] if prev else 0.0
        mae = prev["cumulative_mae_pct"] if prev else 0.0
        if hp is not None:
            mfe, mae = max(mfe, hp, 0.0), min(mae, lp, 0.0)
        return {"symbol": sym, "cycle_no": cyc, "session_date": t, "observation_type": kind, "status": status,
                "reason_code": reason, "tracking_status": tracking, "entry_session_date": entry_session,
                "entry_reference_price": entry_price, "basis_entry_open": basis, "price_basis": basis_kind,
                "observed_open": o, "observed_high": h, "observed_low": low, "session_high_pct": hp, "session_low_pct": lp,
                "cumulative_mfe_pct": mfe, "cumulative_mae_pct": mae,
                "dataset_id": ref["dataset_id"] if data else None, "content_hash": ref["content_hash"] if data else None,
                "engine_version": ENGINE_VERSION, "captured_at": captured_at}

    # ---- the cycle that was open (or waiting for its exit) coming into T --------------------------------------------
    if prev_state in (OPEN, EXIT_PENDING):
        cyc = plc.get("cycle_no")
        prev = last.get((sym, cyc))
        if prev is not None and prev["tracking_status"] == TRACKING:          # tracked from its entry; legacy never
            es, ep = prev["entry_session_date"], prev["entry_reference_price"]
            exit_fill = next((f for f in fills if f["fill_type"] == "EXIT" and f["status"] == "FILLED"
                              and f["cycle_no"] == cyc), None)
            if obs["state_after"] == BLOCKED:
                rows.append(row("CONTINUITY_GAP", GAP, GAP, es, ep, prev, reason="FORWARD_CONTINUITY_GAP", data=False))
            elif exit_fill is not None:                                        # the cycle ends at the open of T
                pb = exit_fill.get("price_basis") or {}
                this = pb.get("basis") == "THIS_CAPTURE" and pb.get("entry_open_this_capture")
                basis = pb["entry_open_this_capture"] if this else exit_fill["reference_entry_price"]
                x = _pct(exit_fill["reference_open_price"], basis)
                rows.append(row("EXIT_OPEN", "OBSERVED", COMPLETE, es, ep, prev, basis=basis,
                                basis_kind="THIS_CAPTURE" if this else "AS_CAPTURED", o=exit_fill["reference_open_price"],
                                hp=x, lp=x))
            elif bar is None:                                                  # nothing traded / recorded: not a held day
                rows.append(row("HELD_SESSION", "EXCURSION_DATA_UNAVAILABLE", TRACKING, es, ep, prev,
                                reason="NO_BAR_FOR_SESSION", data=False))
            else:
                entry_now = series.bar(_d(plc.get("entry_fill_session") or es))
                if entry_now is None:                                          # no consistent price basis: stop, never guess
                    rows.append(row("HELD_SESSION", "EXCURSION_DATA_UNAVAILABLE", DATA_UNAVAILABLE, es, ep, prev,
                                    reason="PRICE_BASIS_UNAVAILABLE"))
                else:
                    b = entry_now.open
                    rows.append(row("HELD_SESSION", "OBSERVED", TRACKING, es, ep, prev, basis=b,
                                    basis_kind="THIS_CAPTURE", o=bar.open, h=bar.high, low=bar.low,
                                    hp=_pct(bar.high, b), lp=_pct(bar.low, b)))
    # ---- a new reference entry at the open of T: the entry session's full range counts ------------------------------
    entry_fill = next((f for f in fills if f["fill_type"] == "ENTRY" and f["status"] == "FILLED"), None)
    if entry_fill is not None and bar is not None:
        cyc = entry_fill["cycle_no"]
        b = entry_fill["reference_open_price"]
        rows.append(row("ENTRY_SESSION", "OBSERVED", TRACKING, t, b, None, basis=b, basis_kind="THIS_CAPTURE",
                        o=bar.open, h=bar.high, low=bar.low, hp=_pct(bar.high, b), lp=_pct(bar.low, b)))
    return rows


# ================================================================================================================
# stored-evidence summaries (views): per reference cycle, never recomputed from bars
# ================================================================================================================

def cycle_summaries(rows: List[dict], fills: List[dict], tracking: Optional[dict]) -> List[dict]:
    """One entry per reference cycle whose reference entry FILLED, in symbol / cycle order: its excursion tracking status
    and the stored cumulative (current or final) MFE / MAE. Cycles filled before tracking started are LEGACY."""
    by: Dict[Tuple[str, int], List[dict]] = {}
    for r in rows:
        by.setdefault((r["symbol"], r["cycle_no"]), []).append(r)
    last = last_by_cycle(rows)
    exits = {(f["symbol"], f["cycle_no"]): f for f in fills if f["fill_type"] == "EXIT" and f["status"] == "FILLED"}
    out = []
    for f in sorted((f for f in fills if f["fill_type"] == "ENTRY" and f["status"] == "FILLED"),
                    key=lambda f: (f["symbol"], f["cycle_no"])):
        key = (f["symbol"], f["cycle_no"])
        x = exits.get(key) or {}
        base = {"symbol": key[0], "cycle_no": key[1], "reference_entry_session": f["fill_session_date"],
                "reference_entry_open": f["reference_open_price"], "completed": key in exits,
                "reference_exit_session": x.get("fill_session_date"), "reference_exit_open": x.get("reference_open_price"),
                "reference_move_pct": x.get("reference_move_pct")}
        lr = last.get(key)
        if lr is None:
            out.append({**base, "status": LEGACY, "status_text": STATUS_TEXT[LEGACY], "mfe_pct": None, "mae_pct": None,
                        "final": False, "sessions_observed": 0, "sessions_without_bar": 0, "last_session": None})
            continue
        rs = by[key]
        st = lr["tracking_status"]
        out.append({**base, "status": st, "status_text": STATUS_TEXT[st],
                    "mfe_pct": lr["cumulative_mfe_pct"], "mae_pct": lr["cumulative_mae_pct"], "final": st == COMPLETE,
                    "sessions_observed": sum(1 for r in rs if r["status"] == "OBSERVED"),
                    "sessions_without_bar": sum(1 for r in rs if r["reason_code"] == "NO_BAR_FOR_SESSION"),
                    "last_session": lr["session_date"], "tracked_since": tracking["activated_in_session"] if tracking else None})
    return out


def completed_metrics(summaries: List[dict]) -> dict:
    """Completed-cycle excursion statistics over TRACKED completed cycles only (legacy / incomplete never count as 0)."""
    done = [s for s in summaries if s["completed"]]
    tracked = [s for s in done if s["status"] == COMPLETE]
    mfe = [s["mfe_pct"] for s in tracked]
    mae = [s["mae_pct"] for s in tracked]
    return {"completed_cycles": len(done), "tracked_completed_cycles": len(tracked),
            "legacy_untracked_completed_cycles": sum(1 for s in done if s["status"] == LEGACY),
            "incomplete_completed_cycles": sum(1 for s in done if s["status"] not in (COMPLETE, LEGACY)),
            "open_tracked_cycles": sum(1 for s in summaries if not s["completed"] and s["status"] == TRACKING),
            "average_mfe_pct": (sum(mfe) / len(mfe)) if mfe else None,
            "average_mae_pct": (sum(mae) / len(mae)) if mae else None,
            "median_mfe_pct": statistics.median(mfe) if mfe else None,
            "median_mae_pct": statistics.median(mae) if mae else None,
            "formulas": FORMULAS}
