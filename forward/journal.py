"""
forward/journal.py — Stage 3.3 FORWARD-TEST SIGNAL JOURNAL: eligibility, creation, preflight, recording and views.

Question answered: what did this exact saved strategy version say, session by session, using information captured
AFTER each market session actually happened? It is not a backtest, not a paper account and not automatic: every
capture is an explicit user action ("Record latest completed close"), nothing is scheduled, no order object exists.

Rules (see forward/METHOD.md):
  * only saved, immutable, integrity-checked BACKTEST_READY or FORWARD_TEST_ONLY versions whose every feature is
    forward-capable; re-verified before every evaluation;
  * forward means forward: the first eligible session is the first market session strictly AFTER the journal's
    creation date (New York), even if the journal was created before that day's close;
  * only COMPLETED sessions (Stage 3.2 rule: dated before today in New York) are recorded, and only the latest one;
  * no backfill: an eligible session nobody captured is stored as MISSED, never reconstructed later;
  * a light per-symbol SHADOW STATE (FLAT / ENTRY_PENDING / OPEN / EXIT_PENDING) keeps entry and exit rules in order —
    no equity, cash, shares, sizing or orders. A missed session while a symbol is not FLAT makes that symbol
    CONTINUITY_BLOCKED (no lifecycle claims after it); a missed session while FLAT only marks the journal GAPPED;
  * REFERENCE fills are the next session's actual open with Stage 3.2's next-open conventions — not orders, not
    broker fills, not a claim that the price was achievable.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from backtest import bars as B
from backtest import runs as R
from backtest.engine import EPS, EXIT_REASONS
from backtest.snapshots import ENTRY_LEVELS, SPY
from backtest.store import BacktestError, BacktestStore, canonical, sha256
from forward import capture as C
from forward import excursions as XC
from forward.store import ForwardError, ForwardStore
from strategy import features as F
from strategy import spec as S
from strategy.evaluate import group_met

ENGINE_VERSION = "3.3.0"
FLAT, ENTRY_PENDING, OPEN, EXIT_PENDING, BLOCKED = "FLAT", "ENTRY_PENDING", "OPEN", "EXIT_PENDING", "CONTINUITY_BLOCKED"
STRICT, POST = "STRICT_FORWARD", "POST_CLOSE_FORWARD_CONTEXT"
NOTE = ("Forward observations of one saved strategy version — what its rules said at each captured close. No order "
        "was placed, nothing is sized, and this is not a recommendation or a performance verdict.")
REFERENCE_FILL_TEXT = ("REFERENCE FILL = the actual opening price of the next market session, with the Stage 3.2 "
                       "next-open conventions. It is not an order, not a broker fill and not a claim that this price was "
                       "achievable.")
TIMING_TEXT = {STRICT: "Every input the rules used was fixed as of the session close (daily bars through the close, "
                       "and any research saved before the close).",
               POST: "Some forward-only inputs (research freshness, research saved after the close, or event calendars) "
                     "were read when this session was recorded, after the close. Still forward evidence, but not "
                     "identical to the at-the-close assumption Stage 3.2 uses."}
ERROR_STATUS = {"NOT_FOUND": 404, "STRATEGY_INTEGRITY_ERROR": 422, "REGISTRY_MISMATCH": 422, "FORWARD_UNSUPPORTED": 422,
                "JOURNAL_ARCHIVED": 409, "NO_ELIGIBLE_SESSION": 409, "SESSION_NOT_COMPLETE": 409, "JOURNAL_EXISTS": 409,
                "DATA_UNAVAILABLE": 503}


def _now(now: Optional[datetime]) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def ny_date(dt: datetime) -> date:
    return dt.astimezone(B.NY).date()


def _d(s: Optional[str]) -> Optional[date]:
    return date.fromisoformat(s) if s else None


def _raise(code: str, message: str, detail=None) -> None:
    raise ForwardError(code, message, detail, ERROR_STATUS.get(code, 400))


# ================================================================================================================
# 1. ELIGIBILITY — Stage 3.2's integrity checks, with the readiness gate widened to forward-capable versions
# ================================================================================================================

def eligibility(bstore: BacktestStore, strategy_id: str, version_number: int) -> Tuple[Optional[dict], Optional[dict], List[dict], List[dict]]:
    """(ref, spec, checks, errors). Error codes: NOT_FOUND, STRATEGY_INTEGRITY_ERROR, REGISTRY_MISMATCH,
    FORWARD_UNSUPPORTED. BACKTEST_READY and FORWARD_TEST_ONLY versions are both allowed."""
    ref, spec, checks, errors = R.eligibility(bstore, strategy_id, version_number)
    checks = [c for c in checks if c["code"] != "READINESS"]            # Stage 3.2's BACKTEST_READY-only gate
    errors = [e for e in errors if e["code"] not in (S.FORWARD_ONLY, S.UNSUPPORTED, S.READY)]
    code_map = {"INTEGRITY_ERROR": "STRATEGY_INTEGRITY_ERROR", "SCHEMA_VERSION": "STRATEGY_INTEGRITY_ERROR"}
    errors = [{**e, "code": code_map.get(e["code"], e["code"])} for e in errors]
    if ref is None or spec is None or errors:
        return ref, spec, checks, errors
    rd = S.readiness(spec)
    stored_ok = ref["readiness"] == rd["status"]
    checks.append({"code": "READINESS_CONSISTENT", "ok": stored_ok, "label": "Stored readiness matches the rules"})
    if not stored_ok:
        errors.append({"code": "STRATEGY_INTEGRITY_ERROR", "message": "The stored readiness does not match the rules."})
    bad = [fid for fid, _ in S.features_used(spec) if not F.get(fid).forward_support]
    checks.append({"code": "FORWARD_SUPPORT", "ok": not bad, "label": "Every rule feature can be captured going forward",
                   **({"detail": ", ".join(bad)} if bad else {})})
    if bad or rd["status"] == S.UNSUPPORTED:
        errors.append({"code": "FORWARD_UNSUPPORTED", "message": "This version uses live-account features "
                       f"({', '.join(bad) or 'unsupported'}) that no strategy may use yet, so it cannot be forward tested.",
                       "features": bad})
    ref = {**ref, "readiness_detail": rd,
           "forward_only_features": [{"feature": r["feature"], "name": r["name"], "why": r["why"]}
                                     for r in rd["reasons"] if r["state"] == "FORWARD_ONLY"]}
    return ref, spec, checks, errors


def _checked(bstore: BacktestStore, j: dict) -> Tuple[dict, dict, List[dict]]:
    ref, spec, checks, errors = eligibility(bstore, j["strategy_id"], j["version_number"])
    if not errors and (ref["strategy_version_id"] != j["strategy_version_id"] or ref["spec_hash"] != j["spec_hash"]
                       or ref["rules_hash"] != j["rules_hash"]):
        errors.append({"code": "STRATEGY_INTEGRITY_ERROR", "message": "The strategy version no longer matches the one "
                       "this journal was started for."})
    if errors:
        _raise(errors[0]["code"], errors[0]["message"], {"checks": checks, "errors": errors})
    return ref, spec, checks


# ================================================================================================================
# 2. CREATE — forward start = the day after creation (New York); nothing is evaluated
# ================================================================================================================

def journal_config(ref: dict, spec: dict) -> dict:
    needs = C.price_needs(spec)
    return {"engine_version": ENGINE_VERSION,
            "strategy": {k: ref[k] for k in ("strategy_id", "strategy_version_id", "version_number", "spec_hash",
                                             "rules_hash", "feature_registry_version", "feature_registry_fingerprint",
                                             "readiness")} | {"name": spec["name"]},
            "universe": list(spec["universe"]["symbols"]),
            "execution": {"decision": "AT_CLOSE", "reference_fill": "NEXT_SESSION_OPEN", "sizing": "NONE",
                          "risk_limits_applied": False, "text": REFERENCE_FILL_TEXT},
            "forward_start_rule": "first market session strictly after the journal's creation date (New York)",
            "completed_session_rule": "a session is recorded only after its date has ended in New York (Stage 3.2 rule)",
            "data": {"source": B.SOURCE, "feed": B.feed(), "adjustment": B.ADJUSTMENT, "timeframe": "1Day",
                     "window_calendar_days": C.LOOKBACK, "calendar": f"{SPY} sessions"},
            "needs": needs.public(), "forward_only": C.forward_only_features(spec),
            "research_source": "latest SAVED research snapshot at capture (never generated)",
            "event_source": "services.event_context.build_event_context at capture"}


def create_journal(fstore: ForwardStore, bstore: BacktestStore, strategy_id: str, version_number: int,
                   now: Optional[datetime] = None) -> dict:
    ref, spec, checks, errors = eligibility(bstore, strategy_id, version_number)
    if errors:
        _raise(errors[0]["code"], errors[0]["message"], {"checks": checks, "errors": errors})
    now = _now(now)
    created = ny_date(now)
    cfg = journal_config(ref, spec)
    return fstore.create_journal(ref, ENGINE_VERSION, canonical(cfg), sha256(canonical(cfg)), C.utc_iso(now),
                                 created.isoformat(), (created + timedelta(days=1)).isoformat())


def archive_journal(fstore: ForwardStore, journal_id: str, now: Optional[datetime] = None) -> dict:
    return fstore.archive(journal_id, C.utc_iso(_now(now)))


# ================================================================================================================
# 3. EVALUATION — strategy.evaluate.group_met is the only meaning of a condition
# ================================================================================================================

def _determinate(logic: str, trace: List[dict]) -> Optional[bool]:
    """Would the group's result stay the same whatever the unavailable values were? True / False, or None when an
    unavailable value could change it. Only combines group_met's own per-condition results (no rule is re-evaluated).
    A None never turns into an entry: group_met treats a missing value as "not met" (Stage 3.1)."""
    vals = [(_determinate(t["group"], t["children"]) if "children" in t else {"MET": True, "NOT_MET": False}.get(t["result"]))
            for t in trace]
    if logic == "ALL":
        return False if any(v is False for v in vals) else True if vals and all(v is True for v in vals) else None
    return True if any(v is True for v in vals) else False if all(v is False for v in vals) else None


def _leaves(trace: List[dict]) -> List[dict]:
    out = []
    for t in trace:
        out.extend(_leaves(t["children"]) if "children" in t else [t])
    return out


def _annotate(trace: List[dict], cells: dict) -> List[dict]:
    out = []
    for t in trace:
        if "children" in t:
            out.append({**t, "children": _annotate(t["children"], cells)})
        else:
            c = cells.get(t.get("feature")) or {}
            f = F.get(t.get("feature"))
            cond = {k: t[k] for k in ("feature", "op") if k in t} | ({"value": t["expected"]} if t.get("expected") is not None else {})
            out.append({**t, "availability": c.get("a", "NOT_COMPUTED"), **({"reason": c["reason"]} if c.get("reason") else {}),
                        "name": f.beginner_name if f else t.get("feature"), "text": S.condition_text(cond),
                        "unit": f.unit if f else None,
                        "actual_label": f.value_label(t["actual"]) if f and f.data_type == "ENUM" and t.get("actual") is not None else None})
    return out


def _values(cells: Optional[dict]) -> dict:
    return {} if not cells else {fid: c["v"] for fid, c in cells.items()}


def _group_eval(group: dict, cells: dict) -> dict:
    ok, trace = group_met(group, _values(cells))
    leaves = _leaves(trace)
    return {"logic": group["logic"], "result": "MET" if ok else "NOT_MET", "met": ok,
            "determinate": _determinate(group["logic"], trace) is not None,
            "trace": _annotate(trace, cells), "leaves_met": sum(1 for x in leaves if x["result"] == "MET"),
            "leaves_total": len(leaves)}


# ================================================================================================================
# 4. THE PER-SYMBOL STEP for one captured session T (mirrors the Stage 3.2 day order)
#    OPEN of T: a pending exit fills, then a pending entry fills (or is unfilled: no bar that session)
#    CLOSE of T: an open state counts T and checks exit rules; a flat symbol checks entry rules
# ================================================================================================================

EMPTY_CYCLE = {"entry_signal_session": None, "entry_signal_close": None, "entry_support": None, "entry_resistance": None,
               "entry_fill_session": None, "reference_entry_open": None, "holding_sessions": 0,
               "exit_signal_session": None, "exit_reasons": []}


def _fresh_lifecycle(prev: Optional[dict]) -> dict:
    base = {"cycle_no": 0, **EMPTY_CYCLE}
    if prev:
        base.update({k: prev.get(k, base[k]) for k in base})
        base["exit_reasons"] = list(prev.get("exit_reasons") or [])
    base.pop("levels_used", None)
    base.pop("blocked", None)
    if prev and prev.get("blocked"):
        base["blocked"] = prev["blocked"]
    return base


def _bar_json(series, T: date, ds: Optional[dict]) -> Optional[dict]:
    b = series.bar(T) if series is not None else None
    if b is None:
        return None
    return {"open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
            "dataset_id": ds["dataset_id"] if ds else None, "content_hash": ds["content_hash"] if ds else None}


def _exit_eval(spec: dict, lc: dict, cells: dict, series, T: date) -> dict:
    """Exit rules at the close of T for an open shadow state: condition group OR invalidation OR target OR max holding.
    Entry-time levels are those captured at the entry signal / reference fill, REBASED onto this capture's price basis
    (split / dividend adjustment restates earlier bars in every new download) — the same numbers Stage 3.2 would use on
    this dataset. Every true reason is kept."""
    x = spec["exit"]
    inv, tgt, hold = x.get("invalidation"), x.get("target"), x.get("max_holding_days")
    bar = series.bar(T)
    close = bar.close
    reasons, unknown = [], []
    tr = {"side": "EXIT", "session": T.isoformat(), "close": close, "holding_sessions": lc["holding_sessions"]}
    met = total = 0
    if x.get("conditions"):
        g = _group_eval(x, cells)
        tr["conditions"] = {k: g[k] for k in ("logic", "result", "determinate", "trace")}
        met, total = g["leaves_met"], g["leaves_total"]
        if g["met"]:
            reasons.append("CONDITION")
        elif not g["determinate"]:
            unknown.append("CONDITION")
    sig_bar = series.bar(_d(lc["entry_signal_session"]))
    fill_bar = series.bar(_d(lc["entry_fill_session"]))
    f_sig = sig_bar.close / lc["entry_signal_close"] if sig_bar is not None and lc["entry_signal_close"] else None
    fill_now = fill_bar.open if fill_bar is not None else None
    basis = {"signal_close_as_captured": lc["entry_signal_close"], "signal_close_this_capture": sig_bar.close if sig_bar else None,
             "reference_open_as_captured": lc["reference_entry_open"], "reference_open_this_capture": fill_now,
             "level_factor": f_sig, "entry_factor": (fill_now / lc["reference_entry_open"]) if fill_now else None}
    tr["price_basis"] = basis
    for key, rule in (("invalidation", inv), ("target", tgt)):
        if not rule:
            continue
        total += 1
        m = rule["method"]
        if m in ("CLOSE_BELOW_ENTRY_SUPPORT", "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE"):
            raw = lc["entry_support"] if m == "CLOSE_BELOW_ENTRY_SUPPORT" else lc["entry_resistance"]
            lvl = raw * f_sig if (raw is not None and f_sig is not None) else None
        else:
            raw = None
            sign = -1.0 if m == "PCT_BELOW_ENTRY" else 1.0
            lvl = fill_now * (1 + sign * rule["pct"] / 100.0) if fill_now is not None else None
        if lvl is None:
            tr[key] = {"method": m, "level": None, "close": close, "hit": None, "reason": "PRICE_BASIS_UNAVAILABLE"}
            unknown.append(key.upper())
            continue
        hit = (close < lvl - EPS if m == "CLOSE_BELOW_ENTRY_SUPPORT" else close <= lvl + EPS) if key == "invalidation" \
            else close >= lvl - EPS
        tr[key] = {"method": m, "level": lvl, "level_as_captured": raw, "close": close, "hit": hit}
        if hit:
            met += 1
            reasons.append("INVALIDATION" if key == "invalidation" else "TARGET")
    if hold:
        total += 1
        hit = lc["holding_sessions"] >= hold
        tr["max_holding"] = {"holding_sessions": lc["holding_sessions"], "max_holding_days": hold, "hit": hit}
        if hit:
            met += 1
            reasons.append("MAX_HOLDING")
    reasons = [r for r in EXIT_REASONS if r in reasons]            # contract order; every true reason kept
    tr["result"] = "EXIT" if reasons else "UNDETERMINED" if unknown else "NO_EXIT"
    return {"reasons": reasons, "unknown": unknown, "trace": tr, "met": met, "total": total,
            "levels": {"invalidation_level": (tr.get("invalidation") or {}).get("level"),
                       "target_level": (tr.get("target") or {}).get("level"), **basis}}


class _Ctx:
    def __init__(self, spec, T, missed, series, datasets, captured_sessions, now):
        self.spec, self.T, self.missed, self.series, self.datasets = spec, T, missed, series, datasets
        self.captured_sessions, self.now = captured_sessions, now


def step(ctx: _Ctx, sym: str, prev_obs: Optional[dict], cells: Optional[dict]) -> Tuple[dict, List[dict]]:
    """One symbol at one captured session. Returns (observation, reference fills). Pure (no I/O)."""
    spec, T = ctx.spec, ctx.T
    series, ds = ctx.series.get(sym), ctx.datasets.get(sym)
    state = prev_obs["state_after"] if prev_obs else FLAT
    lc = _fresh_lifecycle(prev_obs["lifecycle"] if prev_obs else None)
    bar = series.bar(T) if series is not None else None
    fills: List[dict] = []
    obs = {"symbol": sym, "state_before": state, "decision": "SKIP", "state_after": state, "reason_code": "",
           "evaluated_side": None, "rules_met": None, "rules_total": None, "exit_reasons": [], "evaluation_trace": None,
           "bar": _bar_json(series, T, ds)}

    def fill_row(kind, status, signal, **kw):
        base = {"symbol": sym, "cycle_no": lc["cycle_no"], "fill_type": kind, "status": status, "reason_code": None,
                "signal_session_date": signal, "fill_session_date": None, "reference_open_price": None,
                "delay_sessions": None, "reference_entry_price": None, "reference_move_pct": None, "price_basis": None,
                "dataset_id": ds["dataset_id"] if ds else None, "content_hash": ds["content_hash"] if ds else None}
        base.update(kw)
        if status == "UNFILLED":
            base.update({"dataset_id": None, "content_hash": None})
        return base

    # ---- continuity ----------------------------------------------------------------------------------------------
    if state == BLOCKED:
        return {**obs, "reason_code": "FORWARD_CONTINUITY_GAP", "lifecycle": lc}, fills
    if ctx.missed and state != FLAT:
        lc["blocked"] = {"since_session": T.isoformat(), "state_when_blocked": state,
                         "missed_sessions": [d.isoformat() for d in ctx.missed]}
        return {**obs, "state_after": BLOCKED, "reason_code": "FORWARD_CONTINUITY_GAP", "lifecycle": lc}, fills

    # ---- OPEN of T -----------------------------------------------------------------------------------------------
    if state == EXIT_PENDING and bar is not None:
        entry_now = series.bar(_d(lc["entry_fill_session"]))
        entry_basis = entry_now.open if entry_now is not None else lc["reference_entry_open"]
        sig = lc["exit_signal_session"]
        delay = sum(1 for d in ctx.captured_sessions if sig < d < T.isoformat())
        fills.append(fill_row("EXIT", "FILLED", sig, fill_session_date=T.isoformat(), reference_open_price=bar.open,
                              delay_sessions=delay, reference_entry_price=lc["reference_entry_open"],
                              reference_move_pct=(bar.open / entry_basis - 1.0) * 100.0,
                              price_basis={"entry_open_as_captured": lc["reference_entry_open"],
                                           "entry_open_this_capture": entry_now.open if entry_now else None,
                                           "basis": "THIS_CAPTURE" if entry_now is not None else "AS_CAPTURED"}))
        lc.update({**EMPTY_CYCLE, "exit_reasons": []})
        state = FLAT
    elif state == ENTRY_PENDING:
        sig = lc["entry_signal_session"]
        if bar is not None:
            fills.append(fill_row("ENTRY", "FILLED", sig, fill_session_date=T.isoformat(), reference_open_price=bar.open,
                                  delay_sessions=0))
            lc.update({"entry_fill_session": T.isoformat(), "reference_entry_open": bar.open, "holding_sessions": 0})
            state = OPEN
        else:
            fills.append(fill_row("ENTRY", "UNFILLED", sig, reason_code="NEXT_SESSION_BAR_MISSING"))
            lc.update({**EMPTY_CYCLE, "exit_reasons": []})
            state = FLAT

    # ---- CLOSE of T ----------------------------------------------------------------------------------------------
    if bar is None:
        return {**obs, "state_after": state, "reason_code": "NO_BAR_FOR_SESSION", "lifecycle": lc}, fills
    if state == OPEN:
        lc["holding_sessions"] += 1
        ev = _exit_eval(spec, lc, cells or {}, series, T)
        lc["levels_used"] = ev["levels"]
        base = {**obs, "evaluated_side": "EXIT", "rules_met": ev["met"], "rules_total": ev["total"],
                "evaluation_trace": ev["trace"]}
        if ev["reasons"]:
            lc.update({"exit_signal_session": T.isoformat(), "exit_reasons": ev["reasons"]})
            return {**base, "decision": "EXIT", "state_after": EXIT_PENDING, "reason_code": "EXIT_RULES_MET",
                    "exit_reasons": ev["reasons"], "lifecycle": lc}, fills
        if ev["unknown"]:
            return {**base, "decision": "SKIP", "state_after": OPEN, "reason_code": "REQUIRED_DATA_UNAVAILABLE",
                    "lifecycle": lc}, fills
        return {**base, "decision": "HOLD", "state_after": OPEN, "reason_code": "NO_EXIT_SIGNAL", "lifecycle": lc}, fills
    # FLAT (possibly just exited at this session's open, or an entry that could not fill)
    if cells is None:
        return {**obs, "state_after": FLAT, "reason_code": "NOT_EVALUATED", "lifecycle": lc}, fills
    g = _group_eval(spec["entry"], cells)
    base = {**obs, "evaluated_side": "ENTRY", "rules_met": g["leaves_met"], "rules_total": g["leaves_total"],
            "evaluation_trace": {"side": "ENTRY", "session": T.isoformat(), **{k: g[k] for k in ("logic", "result", "determinate", "trace")}}}
    if g["met"]:
        x = spec["exit"]
        inv, tgt = x.get("invalidation") or {}, x.get("target") or {}
        sup, res = (cells.get("stock.support") or {}).get("v"), (cells.get("stock.resistance") or {}).get("v")
        if inv.get("method") == "CLOSE_BELOW_ENTRY_SUPPORT" and sup is None:
            return {**base, "decision": "SKIP", "state_after": FLAT, "reason_code": "REQUIRED_ENTRY_SUPPORT_UNAVAILABLE", "lifecycle": lc}, fills
        if tgt.get("method") == "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE" and res is None:
            return {**base, "decision": "SKIP", "state_after": FLAT, "reason_code": "REQUIRED_ENTRY_RESISTANCE_UNAVAILABLE", "lifecycle": lc}, fills
        lc.update({**EMPTY_CYCLE, "cycle_no": lc["cycle_no"] + 1, "entry_signal_session": T.isoformat(),
                   "entry_signal_close": bar.close, "entry_support": sup, "entry_resistance": res, "exit_reasons": []})
        return {**base, "decision": "ENTER", "state_after": ENTRY_PENDING, "reason_code": "ENTRY_RULES_MET", "lifecycle": lc}, fills
    if not g["determinate"]:
        return {**base, "decision": "SKIP", "state_after": FLAT, "reason_code": "REQUIRED_DATA_UNAVAILABLE", "lifecycle": lc}, fills
    return {**base, "decision": "HOLD", "state_after": FLAT, "reason_code": "NO_ENTRY", "lifecycle": lc}, fills


# ================================================================================================================
# 5. SNAPSHOTS
# ================================================================================================================

def _snapshot(cells: Optional[dict], T: date, captured_at: str, context_at: Optional[str], reason: Optional[str],
              expected: List[str]) -> dict:
    close = C.utc_iso(C.close_utc(T))
    feats = []
    ids = sorted(set(cells or {}) | set(expected))
    for fid in ids:
        f = F.get(fid)
        c = (cells or {}).get(fid)
        if c is None:
            c = {"v": None, "a": "NOT_EVALUATED", "reason": reason or "NOT_COMPUTED"}
        feats.append({"feature_id": fid, "name": f.beginner_name if f else fid, "scope": f.scope if f else None,
                      "value": c["v"], "unit": f.unit if f else None,
                      "value_label": f.value_label(c["v"]) if f and f.data_type == "ENUM" and c["v"] is not None else None,
                      "availability": c["a"], "reason": c.get("reason"),
                      "source": f.source if f else None, "source_timestamp": c.get("ts", close if f and f.historical_support else None),
                      "timing": c.get("timing", C.AT_CLOSE if f and f.historical_support else None)})
    return {"session": T.isoformat(), "market_close_snapshot_time": close, "captured_at": captured_at,
            "forward_context_captured_at": context_at, "registry_version": F.REGISTRY_VERSION,
            "registry_fingerprint": F.fingerprint(), "features": feats}


def _timing(cells: Optional[dict], used: set) -> str:
    return POST if any((c or {}).get("timing") == C.POST_CLOSE for fid, c in (cells or {}).items() if fid in used) else STRICT


# ================================================================================================================
# 6. DATA for a capture (Stage 3.2 cache) + the session calendar
# ================================================================================================================

_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)


def _earliest_needed(j: dict, captured: List[str], latest: Dict[str, dict]) -> date:
    """The market calendar is needed from the last capture (to find missed sessions) and every open cycle's entry
    signal session (to rebase entry-time levels onto this capture's price basis)."""
    days = [_d(captured[-1])] if captured else [_d(j["forward_start_date"])]
    for o in latest.values():
        lc = o["lifecycle"] or {}
        if o["state_after"] in (ENTRY_PENDING, OPEN, EXIT_PENDING) and lc.get("entry_signal_session"):
            days.append(_d(lc["entry_signal_session"]))
    return min(days)


def _not_started(j: dict, now: datetime, last_complete: date) -> dict:
    fstart = _d(j["forward_start_date"])
    today = ny_date(now)
    in_progress = today >= fstart and today.weekday() < 5
    first_possible = max(fstart, today) if in_progress else fstart
    code = "SESSION_NOT_COMPLETE" if in_progress else "NO_ELIGIBLE_SESSION"
    msg = (f"The first eligible session ({first_possible.isoformat()}) has not finished yet; it can be recorded from "
           f"{(first_possible + timedelta(days=1)).isoformat()} (New York)." if in_progress else
           f"No eligible session has completed yet. The first eligible session is the first market session on or after "
           f"{fstart.isoformat()} (the day after this journal was created); the latest completed session is "
           f"{last_complete.isoformat()} or earlier.")
    return {"code": code, "message": msg}


def _datasets_public(ds: Dict[str, dict]) -> Dict[str, dict]:
    return {s: {k: d[k] for k in ("dataset_id", "content_hash", "fetched_at", "requested_start", "requested_end",
                                  "first_session", "last_session", "bar_count")} for s, d in sorted(ds.items())}


# ================================================================================================================
# 7. PREFLIGHT — checks only; writes nothing (at most one small read-only SPY request that is not stored)
# ================================================================================================================

def preflight(fstore: ForwardStore, bstore: BacktestStore, journal_id: str, now: Optional[datetime] = None,
              fetch_fn=None, client=None, research_db=None, probe: bool = True) -> dict:
    t0 = time.perf_counter()
    j = fstore.journal(journal_id)
    now = _now(now)
    out = {"journal_id": journal_id, "status": "BLOCKED", "checks": [], "errors": [], "warnings": [], "session": None,
           "continuity": None, "pending": [], "inputs": [], "download": None, "note": NOTE}
    if j["status"] == "ARCHIVED":
        out.update(status="JOURNAL_ARCHIVED", errors=[{"code": "JOURNAL_ARCHIVED", "message": "This journal is archived; "
                                                        "it records nothing more. Start a new journal to continue."}])
        return out
    ref, spec, checks, errors = eligibility(bstore, j["strategy_id"], j["version_number"])
    out["checks"] = checks
    if not errors and (ref["strategy_version_id"] != j["strategy_version_id"] or ref["spec_hash"] != j["spec_hash"]):
        errors.append({"code": "STRATEGY_INTEGRITY_ERROR", "message": "The strategy version no longer matches this journal."})
    if errors:
        out["errors"] = errors
        return out
    last_complete = B.last_complete_session_date(now)
    fstart = _d(j["forward_start_date"])
    sessions = fstore.sessions(journal_id)
    captured = [s["session_date"] for s in sessions if s["kind"] == "CAPTURED"]
    latest = fstore.latest_observations(journal_id)
    out["checks"].append({"code": "SESSION_COMPLETE_RULE", "ok": True, "label": f"Only completed sessions are recorded "
                          f"(dated before today in New York; latest possible: {last_complete.isoformat()})"})
    if last_complete < fstart:
        e = _not_started(j, now, last_complete)
        out.update(status=e["code"], errors=[e], session={"eligible_from": fstart.isoformat(), "latest_completed": None})
        return out
    needs = C.price_needs(spec)
    fo = C.forward_only_features(spec)
    start, end = C.need_range(last_complete, _earliest_needed(j, captured, latest))
    found, missing = C.find_datasets(bstore, needs.symbols(), start, end)
    cal, source = [], None
    if SPY in found:
        cal = [_d(r[0]) for r in bstore.dataset_rows(found[SPY]["dataset_id"])]
        source = "CACHE"
    elif probe:
        try:
            since = _d(captured[-1]) if captured else fstart
            cal = C.probe_sessions(last_complete, now, fetch_fn=fetch_fn, client=client,
                                   lookback_days=max(21, (now.date() - since).days + 7))
            source = "LIVE_PROBE (read-only, not stored)"
        except Exception as exc:  # noqa: BLE001 - no credentials / network: say so, never guess the session
            out.update(status="DATA_UNAVAILABLE", errors=[{"code": "DATA_UNAVAILABLE", "message": "The latest completed "
                       f"session could not be determined from market data ({type(exc).__name__})."}])
            return out
    cal = [d for d in cal if d <= last_complete]
    if not cal:
        out.update(status="DATA_UNAVAILABLE", errors=[{"code": "DATA_UNAVAILABLE", "message": "No market-proxy (SPY) "
                   "sessions are available to determine the latest completed session."}])
        return out
    T = cal[-1]
    out["session"] = {"latest_completed": T.isoformat(), "source": source, "eligible_from": fstart.isoformat(),
                      "market_close_snapshot_time": C.utc_iso(C.close_utc(T)), "last_captured": captured[-1] if captured else None}
    out["checks"].append({"code": "DAILY_BAR_COMPLETE", "ok": True, "label": f"Daily bar complete — session {T.isoformat()} "
                          "has finished; today's bar is never used"})
    if T < fstart:
        e = _not_started(j, now, last_complete)
        out.update(status=e["code"], errors=[e])
        return out
    if captured and T.isoformat() <= captured[-1]:
        out.update(status="ALREADY_RECORDED")
        out["checks"].append({"code": "ALREADY_RECORDED", "ok": True, "label": f"{T.isoformat()} is already recorded — "
                              "recording again returns the stored observation"})
        return out
    lo = _d(captured[-1]) if captured else fstart - timedelta(days=1)
    window = [d for d in cal if lo < d <= T and d >= fstart]
    missed = [d for d in window if d != T]
    if source == "CACHE":
        holes = C.calendar_holes([d for d in cal if d >= lo])
        if holes:
            out.update(status="DATA_UNAVAILABLE", errors=[{"code": "DATA_UNAVAILABLE", "message": "The market calendar "
                       f"has a data hole ({holes[0]['after']} → {holes[0]['before']}); sessions cannot be determined."}])
            return out
    blocking = sorted(s for s, o in latest.items() if missed and o["state_after"] in (ENTRY_PENDING, OPEN, EXIT_PENDING))
    out["continuity"] = {"status": "GAPPED" if (missed or any(s["kind"] == "MISSED" for s in sessions)) else
                         ("CONTINUOUS" if captured else "NOT_STARTED"),
                         "will_record_missed": [d.isoformat() for d in missed], "will_block": blocking,
                         "already_blocked": sorted(s for s, o in latest.items() if o["state_after"] == BLOCKED)}
    if missed:
        out["warnings"].append({"code": "MISSED_FORWARD_SESSION", "text": f"{len(missed)} eligible session(s) were not "
                                f"captured ({', '.join(d.isoformat() for d in missed[:5])}{'…' if len(missed) > 5 else ''}). "
                                "They will be stored as MISSED — never reconstructed — and the journal becomes GAPPED."})
    if blocking:
        out["warnings"].append({"code": "FORWARD_CONTINUITY_GAP", "text": f"{', '.join(blocking)} had an unresolved "
                                "shadow state during the missed session(s); their lifecycle cannot be known, so they "
                                "become CONTINUITY BLOCKED (no further lifecycle claims). End this journal and start a "
                                "new one to observe them again."})
    out["pending"] = [{"symbol": s, "state": o["state_after"], "signal_session": (o["lifecycle"] or {}).get(
        "exit_signal_session" if o["state_after"] == EXIT_PENDING else "entry_signal_session")}
        for s, o in sorted(latest.items()) if o["state_after"] in (ENTRY_PENDING, EXIT_PENDING)]
    # inputs
    universe = list(spec["universe"]["symbols"])
    if missing:
        est = len(missing) * B.estimate_sessions(start, end)
        out["download"] = {"symbols": missing, "count": len(missing), "fetch_start": start.isoformat(),
                           "fetch_end": end.isoformat(), "estimated_bars": est,
                           "breadth_list": sum(1 for s in missing if s in needs.basket),
                           "request": "one batched Alpaca market-data request at record time (read-only, stored as new "
                                      "immutable datasets)"}
        out["checks"].append({"code": "BARS", "ok": True, "label": f"Technical inputs: {len(missing)} symbol(s) will be "
                              f"downloaded when you record (~{est:,} bars)"})
    else:
        series = C.load_series(bstore, {s: d for s, d in found.items() if s in universe})
        no_bar = [s for s in universe if not series[s].has(T)]
        out["checks"].append({"code": "BARS", "ok": not no_bar, "label": "Technical inputs available from cached bars"
                              + (f" — no bar on {T.isoformat()} for {', '.join(no_bar)} (SKIP)" if no_bar else "")})
        for s in no_bar:
            out["inputs"].append({"symbol": s, "input": "DAILY_BAR", "status": "MISSING"})
    missing_input = False
    if fo["research"]:
        rc = C.research_context(universe, fo["research"], now, T, research_db)
        none = [s for s, (_, p) in rc.items() if p["source"] == "UNAVAILABLE"]
        missing_input |= bool(none)
        out["checks"].append({"code": "RESEARCH", "ok": not none, "label": "Research available (saved snapshots)" if not none
                              else f"Research required — no saved research for {', '.join(none)} (those rules may SKIP)"})
        for s, (_, p) in rc.items():
            out["inputs"].append({"symbol": s, "input": "RESEARCH", "status": "AVAILABLE" if p["source"] != "UNAVAILABLE"
                                  else "UNAVAILABLE", "created_at": p.get("created_at"), "existed_at_close": p.get("existed_at_close")})
    if fo["event_stock"] or fo["event_market"]:
        missing_input = True
        out["checks"].append({"code": "EVENTS", "ok": None, "label": "Event data is read from the Stage 2.6 providers at "
                              "record time (after the close). The earnings calendar has no verified provider, so a stock "
                              "event risk below HIGH will be UNAVAILABLE rather than assumed low."})
    if fo["research"] or fo["event_stock"] or fo["event_market"]:
        out["warnings"].append({"code": "POST_CLOSE_FORWARD_CONTEXT_POSSIBLE", "text": "This strategy uses forward-only "
                                "inputs read at record time; the session is labelled POST-CLOSE FORWARD CONTEXT unless every "
                                "input was fixed at the close."})
        if now > C.next_open_estimate(T):
            out["warnings"].append({"code": "CAPTURED_AFTER_NEXT_OPEN", "text": "The next session has probably opened "
                                    "already; forward-only context read now may include information from after the "
                                    "decision close."})
    missing_input |= any(i["status"] != "AVAILABLE" for i in out["inputs"])
    out["status"] = "CAN_RECORD_WITH_MISSING_INPUT" if missing_input else "READY"
    out["preflight_s"] = round(time.perf_counter() - t0, 3)
    return out


# ================================================================================================================
# 8. RECORD — explicit user action; one completed session; one transaction
# ================================================================================================================

def record(fstore: ForwardStore, bstore: BacktestStore, journal_id: str, now: Optional[datetime] = None, fetch_fn=None,
           client=None, research_db=None, events_fn=None, coverage_fn=None) -> dict:
    with _locks[journal_id]:
        return _record(fstore, bstore, journal_id, _now(now), fetch_fn, client, research_db, events_fn, coverage_fn)


def _record(fstore, bstore, journal_id, now, fetch_fn, client, research_db, events_fn, coverage_fn) -> dict:
    t0 = time.perf_counter()
    j = fstore.journal(journal_id)
    if j["status"] == "ARCHIVED":
        _raise("JOURNAL_ARCHIVED", "This journal is archived; it records nothing more.")
    ref, spec, _ = _checked(bstore, j)
    last_complete = B.last_complete_session_date(now)
    fstart = _d(j["forward_start_date"])
    if last_complete < fstart:
        e = _not_started(j, now, last_complete)
        _raise(e["code"], e["message"])
    sessions = fstore.sessions(journal_id)
    captured = [s["session_date"] for s in sessions if s["kind"] == "CAPTURED"]
    latest = fstore.latest_observations(journal_id)
    needs = C.price_needs(spec)
    fo = C.forward_only_features(spec)
    universe = list(spec["universe"]["symbols"])

    start, end = C.need_range(last_complete, _earliest_needed(j, captured, latest))
    found, missing = C.find_datasets(bstore, needs.symbols(), start, end)
    if SPY in found and captured:                   # already recorded? answer from the cache without downloading
        spy_dates = [r[0] for r in bstore.dataset_rows(found[SPY]["dataset_id"]) if r[0] <= last_complete.isoformat()]
        if spy_dates and spy_dates[-1] <= captured[-1]:
            return already_recorded(fstore, journal_id, captured[-1])
    downloaded = []
    if missing:
        try:
            found.update(C.download(bstore, missing, start, now, fetch_fn=fetch_fn, client=client))
        except BacktestError as exc:
            _raise("DATA_UNAVAILABLE", f"Market data could not be downloaded: {exc.message}", {"cause": exc.code})
        downloaded = missing
    t_data = time.perf_counter()
    series = C.load_series(bstore, found)
    spy = series.get(SPY)
    cal = [d for d in (spy.dates if spy else []) if d <= last_complete]
    if not cal:
        _raise("DATA_UNAVAILABLE", f"No {SPY} sessions are available, so the latest completed session is unknown.")
    T = cal[-1]
    if T < fstart:
        e = _not_started(j, now, last_complete)
        _raise(e["code"], e["message"])
    if captured and T.isoformat() <= captured[-1]:
        return already_recorded(fstore, journal_id, captured[-1])
    lo = _d(captured[-1]) if captured else fstart - timedelta(days=1)
    holes = C.calendar_holes([d for d in cal if d >= lo])
    if holes:
        _raise("DATA_UNAVAILABLE", f"The {SPY} session calendar has a data hole ({holes[0]['after']} → "
               f"{holes[0]['before']}), so missed sessions cannot be determined.")
    missed = [d for d in cal if lo < d < T and d >= fstart]

    # ---- inputs: technical (Stage 3.2 snapshot) + forward-only context -------------------------------------------------
    snap = C.technical_snapshot(series, needs, T)
    captured_at = C.utc_iso(now)
    research = C.research_context(universe, fo["research"], now, T, research_db) if fo["research"] else {}
    ev_stock, ev_market = (C.event_context(universe, fo["event_stock"], fo["event_market"], now, events_fn, coverage_fn)
                           if (fo["event_stock"] or fo["event_market"]) else ({}, None))
    context_at = captured_at if (fo["research"] or fo["event_stock"] or fo["event_market"]) else None
    t_feat = time.perf_counter()

    # ---- per-symbol steps ------------------------------------------------------------------------------------------------
    used = {fid for fid, _ in S.features_used(spec)}
    expected = sorted(used | set(ENTRY_LEVELS))
    ctx = _Ctx(spec, T, missed, series, found, captured + [T.isoformat()], now)
    observations, fills, newly_blocked = [], [], []
    # Stage 3.6: forward MFE / MAE of reference cycles, from this capture onward only (never backfilled)
    t_x = time.perf_counter()
    track, tracking, x_rows = fstore.excursion_context(journal_id)
    new_tracking = ({"activated_in_session": T.isoformat(), "activated_at": captured_at, "engine_version": XC.ENGINE_VERSION}
                    if track and tracking is None else None)
    last_x = XC.last_by_cycle(x_rows)
    excursions: List[dict] = []
    x_s = time.perf_counter() - t_x
    for sym in universe:
        s = snap["symbols"].get(sym)
        cells = None
        if s is not None:
            cells = {**snap["market"], **s["cells"]}
            if sym in research:
                cells.update(research[sym][0])
            if sym in ev_stock:
                cells.update(ev_stock[sym][0])
            if ev_market is not None:
                cells.update(ev_market[0])
        obs, f = step(ctx, sym, latest.get(sym), cells)
        if track:
            t_x = time.perf_counter()
            excursions.extend(XC.rows_for(sym, T, latest.get(sym), obs, f, series.get(sym), found.get(sym), last_x, captured_at))
            x_s += time.perf_counter() - t_x
        if obs["state_after"] == BLOCKED and obs["state_before"] != BLOCKED:
            newly_blocked.append(sym)
        reason = None if cells is not None else ("NO_BAR_FOR_SESSION" if obs["bar"] is None else "NOT_EVALUATED")
        obs["feature_snapshot"] = _snapshot(cells, T, captured_at, context_at, reason, expected)
        obs["unavailable"] = [{"feature": x["feature_id"], "availability": x["availability"], "reason": x["reason"]}
                              for x in obs["feature_snapshot"]["features"] if x["feature_id"] in used and x["availability"] != "AVAILABLE"]
        obs["context_timing"] = _timing(cells, used)
        obs["research"] = research[sym][1] if sym in research else None
        obs["event"] = ({"stock": ev_stock[sym][1] if sym in ev_stock else None, "market": ev_market[1] if ev_market else None}
                        if (sym in ev_stock or ev_market is not None) else None)
        observations.append(obs)
        fills.extend(f)
    timing = POST if any(o["context_timing"] == POST for o in observations) else STRICT
    warnings = _session_warnings(observations, missed, newly_blocked, timing, now, T, fo)
    by = defaultdict(list)
    for o in observations:
        by[_group(o)].append(o["symbol"])
    evaluated = sum(1 for o in observations if o["decision"] != "SKIP")
    t_eval = time.perf_counter()
    summary = {"symbols": len(universe), "evaluated": evaluated, "groups": dict(by),
               "coverage_text": f"{evaluated} / {len(universe)} symbols evaluated",
               "missed_detected": [d.isoformat() for d in missed], "newly_blocked": newly_blocked,
               "reference_fills": [{k: f[k] for k in ("symbol", "fill_type", "status", "fill_session_date",
                                                      "reference_open_price", "reference_move_pct", "reason_code")} for f in fills],
               "context_timing": timing, "downloaded": downloaded, "replay": snap["replay"],
               "timings": {"data_s": round(t_data - t0, 3), "features_s": round(t_feat - t_data, 3),
                           "evaluation_s": round(t_eval - t_feat, 3)}}
    data = {"source": B.SOURCE, "feed": B.feed(), "adjustment": B.ADJUSTMENT, "timeframe": "1Day",
            "window_calendar_days": C.LOOKBACK, "datasets": _datasets_public(found), "downloaded": downloaded}
    session_row = {"session_date": T.isoformat(), "recorded_at": captured_at,
                   "market_close_snapshot_time": C.utc_iso(C.close_utc(T)), "forward_context_captured_at": context_at,
                   "context_timing": timing, "engine_version": ENGINE_VERSION, "spec_hash": ref["spec_hash"],
                   "data": data, "data_hash": sha256(canonical({s: d["content_hash"] for s, d in data["datasets"].items()})),
                   "summary": summary, "warnings": warnings}
    new_status = "CONTINUITY_BLOCKED" if newly_blocked and j["status"] == "ACTIVE" else None
    try:
        write_s = fstore.write_capture(journal_id, [{"session_date": d.isoformat(), "recorded_at": captured_at,
                                                     "detected_with_session": T.isoformat()} for d in missed],
                                       session_row, observations, fills, new_status, excursions=excursions,
                                       tracking=new_tracking)
    except sqlite3.IntegrityError as exc:
        existing = fstore.session(journal_id, T.isoformat())
        if existing is not None and existing["kind"] == "CAPTURED":       # a concurrent capture won the race
            return already_recorded(fstore, journal_id, T.isoformat())
        raise ForwardError("DATA_INTEGRITY_ERROR", f"The capture was rejected by the database: {exc}", status=409) from exc
    out = {"status": "RECORDED", "session": session_view(fstore, journal_id, T.isoformat()),
           "journal": journal_view(fstore, journal_id, include_latest=False),
           "timings": {**summary["timings"], "excursions_s": round(x_s, 4), "excursion_rows": len(excursions),
                       "database_write_s": round(write_s, 3), "total_s": round(time.perf_counter() - t0, 3)}, "note": NOTE}
    return out


def already_recorded(fstore: ForwardStore, journal_id: str, session_date: str) -> dict:
    return {"status": "ALREADY_RECORDED", "message": f"The {session_date} close is already recorded; the stored observation "
            "is returned unchanged (it is never recalculated with newer research, news or bars).",
            "session": session_view(fstore, journal_id, session_date),
            "journal": journal_view(fstore, journal_id, include_latest=False), "note": NOTE}


def _group(o: dict) -> str:
    if o["decision"] == "ENTER":
        return "ENTER"
    if o["decision"] == "EXIT":
        return "EXIT"
    if o["state_after"] == BLOCKED:
        return "BLOCKED"
    if o["decision"] == "SKIP":
        return "SKIPPED"
    return "HOLD" if o["state_after"] in (OPEN,) else "NO_ENTRY"


def _session_warnings(obs: List[dict], missed: List[date], blocked: List[str], timing: str, now: datetime, T: date,
                      fo: dict) -> List[dict]:
    w = []
    if missed:
        w.append({"code": "MISSED_FORWARD_SESSION", "text": f"{len(missed)} eligible session(s) were not captured "
                  f"({', '.join(d.isoformat() for d in missed[:6])}{'…' if len(missed) > 6 else ''}). They are stored as "
                  "MISSED and were not reconstructed."})
    if blocked:
        w.append({"code": "FORWARD_CONTINUITY_GAP", "text": f"{', '.join(blocked)}: a session was missed while the shadow "
                  "state was pending or open, so the lifecycle after it cannot be known. These symbols are CONTINUITY "
                  "BLOCKED for the rest of this journal."})
    if timing == POST:
        w.append({"code": "POST_CLOSE_FORWARD_CONTEXT", "text": TIMING_TEXT[POST]})
        if now > C.next_open_estimate(T):
            w.append({"code": "CAPTURED_AFTER_NEXT_OPEN", "text": "Forward-only context was read after the next session "
                      "probably opened, so it may include information from after the decision close."})
    no_research = sorted(o["symbol"] for o in obs if o.get("research") and o["research"]["source"] == "UNAVAILABLE")
    if no_research:
        w.append({"code": "RESEARCH_UNAVAILABLE", "text": f"No saved research for {', '.join(no_research)}. Research "
                  "features were UNAVAILABLE (never generated automatically)."})
    ev_missing = sorted(o["symbol"] for o in obs if any(u["reason"] in ("EVENT_DATA_INCOMPLETE", "EVENT_DATA_UNAVAILABLE")
                                                        for u in o["unavailable"]))
    if ev_missing:
        w.append({"code": "EVENT_DATA_UNAVAILABLE", "text": f"Event features were withheld for {', '.join(ev_missing)} "
                  "because some event calendars were unavailable — missing event data is never treated as low risk."})
    nobar = sorted(o["symbol"] for o in obs if o["reason_code"] == "NO_BAR_FOR_SESSION")
    if nobar:
        w.append({"code": "NO_BAR_FOR_SESSION", "text": f"No daily bar on {T.isoformat()} for {', '.join(nobar)} — not "
                  "evaluated (SKIP)."})
    rebased = sorted(o["symbol"] for o in obs if abs(((o["lifecycle"].get("levels_used") or {}).get("level_factor") or 1.0) - 1.0) > 1e-9
                     or abs(((o["lifecycle"].get("levels_used") or {}).get("entry_factor") or 1.0) - 1.0) > 1e-9)
    if rebased:
        w.append({"code": "PRICE_BASIS_RESTATED", "text": f"{', '.join(rebased)}: split / dividend adjustment restated "
                  "earlier bars since entry; entry-time levels were rebased onto this capture's prices (stored values "
                  "are unchanged)."})
    if fo["event_stock"] and any(not ((((o.get("event") or {}).get("stock") or {}).get("providers") or {}).get("earnings")
                                      or {}).get("available", False) for o in obs):
        w.append({"code": "EARNINGS_CALENDAR_UNAVAILABLE", "text": "The earnings calendar has no verified provider, so "
                  "stock event risk is available only when it is already HIGH."})
    return w


# ================================================================================================================
# 9. VIEWS — stored rows only; opening a journal or a session never recomputes anything
# ================================================================================================================

def _summary(sessions: List[dict], obs: List[dict], fills: List[dict], latest: Dict[str, dict]) -> dict:
    cap = [s for s in sessions if s["kind"] == "CAPTURED"]
    mis = [s for s in sessions if s["kind"] == "MISSED"]
    moves = [f["reference_move_pct"] for f in fills if f["fill_type"] == "EXIT" and f["status"] == "FILLED"
             and f["reference_move_pct"] is not None]
    states = [o["state_after"] for o in latest.values()]
    return {"sessions_captured": len(cap), "sessions_missed": len(mis),
            "continuity": "NOT_STARTED" if not sessions else "GAPPED" if mis else "CONTINUOUS",
            "first_eligible_session": sessions[0]["session_date"] if sessions else None,
            "latest_captured_session": cap[-1]["session_date"] if cap else None,
            "enter_signals": sum(1 for o in obs if o["decision"] == "ENTER"),
            "exit_signals": sum(1 for o in obs if o["decision"] == "EXIT"),
            "hold_decisions": sum(1 for o in obs if o["decision"] == "HOLD"),
            "skip_decisions": sum(1 for o in obs if o["decision"] == "SKIP"),
            "open_shadow_states": states.count(OPEN), "pending_entries": states.count(ENTRY_PENDING),
            "pending_exits": states.count(EXIT_PENDING), "blocked_symbols": sorted(s for s, o in latest.items() if o["state_after"] == BLOCKED),
            "reference_entries_filled": sum(1 for f in fills if f["fill_type"] == "ENTRY" and f["status"] == "FILLED"),
            "reference_entries_unfilled": sum(1 for f in fills if f["fill_type"] == "ENTRY" and f["status"] == "UNFILLED"),
            "completed_reference_cycles": len(moves),
            "average_completed_reference_move_pct": (sum(moves) / len(moves)) if moves else None,
            "average_move_note": "Descriptive only: the average open-to-open move of completed reference cycles. No "
                                 "sizing, costs or slippage — not a return, not a verdict, and a small forward sample.",
            "context_timing_counts": {t: sum(1 for s in cap if s["context_timing"] == t) for t in (STRICT, POST)}}


def _journal_public(j: dict) -> dict:
    cfg = j["config"]
    return {k: j[k] for k in ("journal_id", "strategy_id", "strategy_version_id", "version_number", "spec_hash",
                              "rules_hash", "feature_registry_version", "feature_registry_fingerprint", "readiness",
                              "engine_version", "config_hash", "created_at", "created_session_date",
                              "forward_start_date", "status", "archived_at")} | {
        "name": cfg["strategy"]["name"], "universe": cfg["universe"], "execution": cfg["execution"],
        "forward_only": cfg["forward_only"], "data": cfg["data"]}


def journal_view(fstore: ForwardStore, journal_id: str, include_latest: bool = True) -> dict:
    j = fstore.journal(journal_id)
    sessions = fstore.sessions(journal_id)
    obs = fstore.observations(journal_id, compact=True)
    fills = fstore.fills(journal_id)
    latest = fstore.latest_observations(journal_id) if obs else {}
    by_session = defaultdict(list)
    for o in obs:
        by_session[o["session_date"]].append({k: o[k] for k in ("symbol", "decision", "state_before", "state_after",
                                                                  "reason_code", "rules_met", "rules_total", "evaluated_side")}
                                             | {"exit_reasons": o["exit_reasons"]})
    timeline = [{"session_date": s["session_date"], "kind": s["kind"], "recorded_at": s["recorded_at"],
                 "context_timing": s["context_timing"], "detected_with_session": s["detected_with_session"],
                 "observations": by_session.get(s["session_date"], []),
                 "fills": [{k: f[k] for k in ("symbol", "fill_type", "status", "reference_open_price", "reference_move_pct")}
                           for f in fills if f["resolved_in_session"] == s["session_date"]]}
                for s in reversed(sessions)]
    states = [{"symbol": s, "state": o["state_after"], "since_session": o["session_date"],
               "lifecycle": {k: v for k, v in (o["lifecycle"] or {}).items()}} for s, o in sorted(latest.items())]
    out = {"journal": _journal_public(j), "summary": _summary(sessions, obs, fills, latest), "states": states,
           "timeline": timeline, "reference_fill_text": REFERENCE_FILL_TEXT, "timing_text": TIMING_TEXT, "note": NOTE,
           "excursions": excursion_view(fstore, journal_id, fills)}
    if include_latest:
        cap = [s for s in sessions if s["kind"] == "CAPTURED"]
        out["latest_session"] = session_view(fstore, journal_id, cap[-1]["session_date"]) if cap else None
    return out


def excursion_view(fstore: ForwardStore, journal_id: str, fills: List[dict]) -> dict:
    """Stage 3.6 (additive): stored MFE / MAE of every reference cycle — current while open, final once the reference exit
    is stored, or LEGACY_NOT_TRACKED for cycles that started before tracking. Never recomputed from bars."""
    tracking = fstore.excursion_tracking(journal_id)
    cycles = XC.cycle_summaries(fstore.excursions(journal_id) if tracking else [], fills, tracking)
    return {"tracking": tracking, "cycles": cycles, "metrics": XC.completed_metrics(cycles), "status_text": XC.STATUS_TEXT,
            "formulas": XC.FORMULAS,
            "note": "MFE / MAE describe how far the price moved from the reference entry while a reference cycle was open "
                    "(daily highs and lows; the exit session's open only). Observed going forward; never backfilled."}


def session_view(fstore: ForwardStore, journal_id: str, session_date: str) -> dict:
    j = fstore.journal(journal_id)
    s = fstore.session(journal_id, session_date)
    if s is None:
        raise ForwardError("NOT_FOUND", "That session is not in this journal.", status=404)
    fills = fstore.fills(journal_id)
    obs = fstore.observations(journal_id, session_date) if s["kind"] == "CAPTURED" else []
    return {"journal": _journal_public(j), "session": s, "observations": obs,
            "fills_resolved": [f for f in fills if f["resolved_in_session"] == session_date],
            "fills_from_signals": [f for f in fills if f["signal_session_date"] == session_date],
            "reference_fill_text": REFERENCE_FILL_TEXT, "timing_text": TIMING_TEXT.get(s.get("context_timing") or "", None),
            "note": NOTE, "excursions": fstore.excursions(journal_id, session_date)}


def list_view(fstore: ForwardStore, bstore: BacktestStore, strategy_id: Optional[str], version_number: Optional[int]) -> dict:
    journals = []
    for j in fstore.list_journals(strategy_id, version_number):
        sessions = fstore.sessions(j["journal_id"])
        journals.append(_journal_public(j) | {"sessions_captured": sum(1 for s in sessions if s["kind"] == "CAPTURED"),
                                              "sessions_missed": sum(1 for s in sessions if s["kind"] == "MISSED"),
                                              "latest_captured_session": next((s["session_date"] for s in reversed(sessions)
                                                                               if s["kind"] == "CAPTURED"), None)})
    out = {"journals": journals, "note": NOTE}
    if strategy_id and version_number is not None:
        ref, spec, checks, errors = eligibility(bstore, strategy_id, version_number)
        out["eligibility"] = {"eligible": not errors, "checks": checks, "errors": errors,
                              "readiness": ref["readiness"] if ref else None,
                              "forward_only_features": (ref or {}).get("forward_only_features", []),
                              "historical_backtest_available": bool(ref) and not errors and ref["readiness"] == S.READY}
    return out
