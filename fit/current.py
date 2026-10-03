"""
fit/current.py — ONE Strategy Fit evaluation: a chosen symbol against every saved strategy version, at the latest
COMPLETED daily close. Read-only and on demand; nothing is stored as evidence.

Flow (no new formulas anywhere):
  1. saved versions      non-archived strategies' current versions (older ones only when asked), each re-verified with
                         the Stage 3.3 eligibility checks (Stage 3.2 integrity + forward support). Errors are per version.
  2. universe            a version whose saved universe does not contain the symbol is OUTSIDE UNIVERSE — its rules are
                         not evaluated for that symbol at all.
  3. one dependency plan the entry groups of every evaluable version are merged into ONE Stage 3.2 `Needs` for this
                         symbol (the 80-stock breadth list only if some entry rule needs it; sector ETFs only if a sector
                         feature is used; research / events only if an entry rule uses them). Exit rules are shown,
                         never evaluated, so they add no dependency.
  4. bars                the Stage 3.2 immutable cache when it already covers the window (read-only), else an in-memory
                         copy fetched earlier in this process, else ONE batched read-only request through Stage 3.2's
                         backtest.bars.fetch_and_store with an in-memory stand-in for its dataset writer — so the same
                         complete-sessions-only rule applies and nothing is written.
  5. decision session    Stage 3.3's rule: the latest SPY session dated before today in New York. Today's unfinished
                         bar and intraday quotes are never used. A missing latest session is reported, never replaced by
                         an older one.
  6. ONE feature environment for (symbol, session): backtest.snapshots.day_snapshot (the Stage 3.2 / 3.3 point-in-time
                         snapshot), plus forward.capture.research_context (saved research only; 0 Claude calls) and
                         forward.capture.event_context (Stage 2.6 providers; incomplete data is never LOW).
  7. rules               strategy.evaluate.group_met through forward.journal._group_eval (the same trace, annotations and
                         unavailable-data logic Stage 3.3 stores). Condition counts are descriptive only.
  8. evidence            stored Stage 3.2 runs and Stage 3.3 journals of the exact version, kept separate.
"""
from __future__ import annotations

import copy
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import config
from backtest import bars as B
from backtest.replay import BarSeries
from backtest.snapshots import SPY, Needs
from backtest.store import BacktestError, bars_content_hash
from fit import evidence as E
from fit import readonly as RO
from forward import capture as C
from forward import journal as J
from strategy import features as F
from strategy import spec as S

ENGINE_VERSION = "3.4.0"

RULES_MET, RULES_NOT_MET, INCOMPLETE_DATA = "RULES_MET", "RULES_NOT_MET", "INCOMPLETE_DATA"
OUTSIDE_UNIVERSE, UNSUPPORTED, INTEGRITY_ERROR = "OUTSIDE_UNIVERSE", "UNSUPPORTED", "INTEGRITY_ERROR"
REGISTRY_MISMATCH, STALE_DATA, DATA_UNAVAILABLE = "REGISTRY_MISMATCH", "STALE_DATA", "DATA_UNAVAILABLE"
STATUS_LABEL = {RULES_MET: "RULES MET", RULES_NOT_MET: "RULES NOT MET", INCOMPLETE_DATA: "INCOMPLETE DATA",
                OUTSIDE_UNIVERSE: "OUTSIDE STRATEGY UNIVERSE", UNSUPPORTED: "UNSUPPORTED", INTEGRITY_ERROR: "INTEGRITY ERROR",
                REGISTRY_MISMATCH: "REGISTRY MISMATCH", STALE_DATA: "STALE DATA", DATA_UNAVAILABLE: "DATA UNAVAILABLE"}
EVALUATED = (RULES_MET, RULES_NOT_MET, INCOMPLETE_DATA)
STRICT, POST, INCOMPLETE_CTX = "STRICT_CLOSE_CONTEXT", "POST_CLOSE_CONTEXT", "INCOMPLETE_CONTEXT"
CONTEXT_TEXT = {STRICT: "Every input the rules used was known and fixed at the completed close.",
                POST: "Some forward-only inputs (saved research created after the close, research freshness, or event "
                      "calendars) were read after the close. This is not the same as the at-the-close assumption a "
                      "historical backtest makes.",
                INCOMPLETE_CTX: "One or more inputs the entry rules need are unavailable."}
NOTE = "This view compares current data with your saved rules. It does not rank strategies or predict future returns."
RULE_TEXT = "Rules are evaluated on the latest completed daily close. No order is placed."
SESSION_RULE = ("The decision session is the latest market session whose date has ended in New York (the Stage 3.3 "
                "completed-session rule). Today's unfinished daily bar and intraday quotes are never used.")
REASON_TEXT = {
    "RESEARCH_UNAVAILABLE": "No saved research snapshot for this stock. Nothing is generated here — run Analyze and save "
                            "research to use it.",
    "RESEARCH_LOOKUP_FAILED": "Saved research could not be read.",
    "EVENT_DATA_INCOMPLETE": "Some event calendars are unavailable (there is no verified earnings-calendar provider), so "
                             "an event risk below HIGH cannot be confirmed. Missing event data is never treated as low risk.",
    "EVENT_DATA_UNAVAILABLE": "Event data could not be loaded. Missing event data is never treated as low risk.",
    "INSUFFICIENT_HISTORY": "Not enough daily history at this session to compute this value.",
    "NO_SECTOR_MAPPING": "This stock is not in the app's static sector map.",
    "CONTEXT_DATA_MISSING": "The sector ETF has no bar for this session.",
    "UNAVAILABLE": "The value could not be computed from this session's data.",
    "NOT_COMPUTED": "The value was not computed.",
}
LIMITATIONS = [
    "Each strategy's universe is the explicit list the user saved; other symbols are never evaluated against it.",
    "Market breadth uses the app's current fixed list of 80 large stocks (survivorship caveat).",
    "Sector features use the app's static sector map.",
    "Event data has no verified earnings calendar, so a stock event risk below HIGH is unavailable.",
    "Saved research may have been created after the evaluated close; that timing is shown.",
    "Daily-close resolution only: intraday moves never change the fit.",
    "Strategy Fit describes current rule alignment only — not future performance.",
]


class FitError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def normalise_symbol(symbol) -> str:
    s = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not S.SYMBOL_RE.match(s):
        raise FitError("INVALID_SYMBOL", f'"{symbol}" is not a valid ticker symbol.', 422)
    return s


def _utc(now: Optional[datetime]) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


# ================================================================================================================
# BARS — Stage 3.2 cache (read-only) -> in-memory copy -> one batched read-only request (never stored)
# ================================================================================================================

class BarCache:
    """Short-lived in-memory store (like the app's other in-memory market caches): COMPLETED-session bars fetched by
    Strategy Fit, keyed by symbol and requested window, and parsed series of immutable Stage 3.2 datasets, keyed by dataset
    id + content hash. Never written to the database."""

    def __init__(self, ttl_s: float = 1800.0):
        self.ttl_s = ttl_s
        self._d: Dict[tuple, tuple] = {}
        self._lock = threading.Lock()

    def get(self, key: tuple):
        with self._lock:
            e = self._d.get(key)
            if e is None:
                return None
            if time.monotonic() - e[0] > self.ttl_s:
                del self._d[key]
                return None
            return e[1]

    def put(self, key: tuple, value) -> None:
        with self._lock:
            now = time.monotonic()
            for k in [k for k, e in self._d.items() if now - e[0] > self.ttl_s]:
                del self._d[k]
            self._d[key] = (now, value)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


BAR_CACHE = BarCache()


class _MemoryDatasets:
    """Stands in for BacktestStore.insert_dataset inside backtest.bars.fetch_and_store: Stage 3.2's exact download rules
    (one batched request, complete sessions only, one bar per session, "all empty" = failed request) with the bars kept
    in memory. Nothing is written."""

    def __init__(self):
        self.rows: Dict[str, list] = {}

    def insert_dataset(self, symbol, source, feed, adjustment, requested_start, requested_end, rows, fetched_at) -> dict:
        rows = sorted(rows, key=lambda r: r[0])
        self.rows[symbol] = rows
        return {"dataset_id": None, "symbol": symbol, "source": source, "feed": feed, "adjustment": adjustment,
                "requested_start": requested_start, "requested_end": requested_end,
                "first_session": rows[0][0] if rows else None, "last_session": rows[-1][0] if rows else None,
                "bar_count": len(rows), "content_hash": bars_content_hash(rows), "fetched_at": fetched_at}


def _prov(source: str, ds: dict) -> dict:
    return {"source": source, "dataset_id": ds.get("dataset_id"), "content_hash": ds.get("content_hash"),
            "fetched_at": ds.get("fetched_at"), "first_session": ds.get("first_session"),
            "last_session": ds.get("last_session"), "bar_count": ds.get("bar_count")}


def covering_datasets(bstore: RO.ReadOnlyBacktestStore, symbols, start: date, end: date) -> Dict[str, dict]:
    """backtest.store.BacktestStore.covering_dataset for many symbols in ONE read-only query — the same rule: per symbol,
    the most recently fetched dataset (same feed + adjustment) whose REQUESTED range covers the window; newest wins."""
    syms = sorted(set(symbols))
    if not syms:
        return {}
    try:
        with bstore._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM historical_bar_datasets WHERE symbol IN ({','.join('?' * len(syms))}) AND feed = ? AND "
                "adjustment = ? AND requested_start <= ? AND requested_end >= ? ORDER BY fetched_at DESC, rowid DESC",
                (*syms, B.feed(), B.ADJUSTMENT, start.isoformat(), end.isoformat())).fetchall()
    except sqlite3.OperationalError:                   # no Stage 3.2 cache tables in this database
        return {}
    out: Dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["symbol"], dict(r))
    return out


def dataset_rows_many(bstore: RO.ReadOnlyBacktestStore, datasets: Dict[str, dict]) -> Dict[str, list]:
    """Every dataset's bars in one read-only query, each verified against its stored content hash (Stage 3.2 rule)."""
    ids = sorted({d["dataset_id"] for d in datasets.values()})
    rows: Dict[str, list] = {i: [] for i in ids}
    if ids:
        with bstore._connect() as conn:
            for r in conn.execute(f"SELECT dataset_id, session_date, bar_timestamp, open, high, low, close, volume FROM "
                                  f"historical_daily_bars WHERE dataset_id IN ({','.join('?' * len(ids))}) "
                                  "ORDER BY dataset_id, session_date", ids):
                rows[r[0]].append(tuple(r)[1:])
    out = {}
    for sym, d in datasets.items():
        if bars_content_hash(rows[d["dataset_id"]]) != d["content_hash"]:
            raise BacktestError("DATA_INTEGRITY_ERROR", f"Cached bars for dataset {d['dataset_id'][:12]} no longer match "
                                "their content hash.", status=409)
        out[sym] = rows[d["dataset_id"]]
    return out


def load_bars(bstore: RO.ReadOnlyBacktestStore, symbols, start: date, end: date, now: datetime, cache: BarCache,
              fetch_fn=None, client=None) -> Tuple[Dict[str, BarSeries], Dict[str, dict], dict]:
    feed = B.feed()
    series: Dict[str, BarSeries] = {}
    prov: Dict[str, dict] = {}
    found = covering_datasets(bstore, symbols, start, end)
    # immutable datasets: their verified, parsed series are reused by (dataset id, content hash) for the cache lifetime
    unparsed = {s: d for s, d in found.items() if cache.get(("dataset", d["dataset_id"], d["content_hash"])) is None}
    fresh = dataset_rows_many(bstore, unparsed) if unparsed else {}
    for sym, d in found.items():
        key = ("dataset", d["dataset_id"], d["content_hash"])
        if sym in fresh:
            cache.put(key, BarSeries(sym, fresh[sym]))
        series[sym] = cache.get(key)
        prov[sym] = _prov("STAGE_3_2_CACHE", d)
    missing: List[str] = []
    for sym in sorted(set(symbols) - set(series)):
        hit = cache.get((sym, feed, start.isoformat(), end.isoformat()))
        if hit is not None:
            series[sym] = hit[1]
            prov[sym] = _prov("MEMORY_CACHE", hit[0])
        else:
            missing.append(sym)
    fetched = {"symbols": [], "request_symbols": [], "bars": 0, "requests": 0}
    if missing:
        # SPY rides along, so "no bars for any symbol" (a failed request) is distinguishable from "no bars for this ticker"
        req = sorted(set(missing) | {SPY})
        mem = _MemoryDatasets()
        out = B.fetch_and_store(mem, req, start, now=now, fetch_fn=fetch_fn, client=client)
        for sym, ds in out.items():
            ser = BarSeries(sym, mem.rows[sym])
            cache.put((sym, feed, start.isoformat(), end.isoformat()), (ds, ser))
            if sym not in series:
                series[sym] = ser
                prov[sym] = _prov("FETCHED_NOW", ds)
        fetched = {"symbols": missing, "request_symbols": req, "bars": sum(len(mem.rows[s]) for s in req), "requests": 1}
    return series, prov, fetched


# ================================================================================================================
# PLAN — one dependency plan across every evaluable version (entry rules only)
# ================================================================================================================

def entry_only(spec: dict, symbol: str) -> dict:
    """The spec reduced to what Strategy Fit evaluates: its entry group, for one symbol. Used ONLY to plan data
    dependencies with Stage 3.2 / 3.3's own rules; decisions always use the full stored spec's entry group."""
    s = copy.deepcopy(spec)
    s["universe"] = {**s["universe"], "symbols": [symbol]}
    s["exit"] = {"logic": "ANY", "conditions": [], "invalidation": None, "target": None, "max_holding_days": None}
    return s


def entry_features(spec: dict) -> List[str]:
    seen, out = set(), []
    for c in S._conditions_of(spec["entry"]):
        if c["feature"] not in seen:
            seen.add(c["feature"])
            out.append(c["feature"])
    return out


def plan(symbol: str, specs: List[dict]) -> Tuple[Needs, Dict[str, List[str]]]:
    needs = [C.price_needs(entry_only(s, symbol)) for s in specs]
    fos = [C.forward_only_features(entry_only(s, symbol)) for s in specs]

    def union(attr):
        return tuple(sorted(set().union(*(getattr(n, attr) for n in needs))))
    merged = Needs((symbol,), union("stock_features"), union("market_features"), union("sector_features"),
                   union("index_symbols"), tuple(config.FALLBACK_UNIVERSE) if any(n.basket for n in needs) else (),
                   union("sector_etfs"))
    fo = {k: sorted(set().union(*(f[k] for f in fos))) for k in ("research", "event_stock", "event_market")}
    return merged, fo


# ================================================================================================================
# PER-VERSION RESULT
# ================================================================================================================

def _error_status(errors: List[dict]) -> str:
    codes = {e["code"] for e in errors}
    if codes & {"STRATEGY_INTEGRITY_ERROR", "NOT_FOUND"}:
        return INTEGRITY_ERROR
    if "REGISTRY_MISMATCH" in codes:
        return REGISTRY_MISMATCH
    return UNSUPPORTED


def _base(v: dict, ref: Optional[dict], spec: Optional[dict], checks: List[dict], errors: List[dict]) -> dict:
    return {"strategy_id": v["strategy_id"], "strategy_version_id": v["strategy_version_id"],
            "strategy_name": v["strategy_name"], "version_name": (spec or {}).get("name", v["version_name"]),
            "version": v["version_number"], "is_current": v["is_current"], "current_version": v["current_version"],
            "readiness": v["readiness"], "spec_hash": (ref or {}).get("spec_hash"), "rules_hash": (ref or {}).get("rules_hash"),
            "universe": list(spec["universe"]["symbols"]) if spec else None,
            "universe_origin": spec["universe"].get("origin") if spec else None,
            "eligibility": {"eligible": not errors, "errors": errors, "checks": checks,
                            "integrity_verified": bool(ref) and not any(e["code"] in ("STRATEGY_INTEGRITY_ERROR", "NOT_FOUND")
                                                                        for e in errors)},
            "fit_status": None, "fit_label": None, "status_text": None, "group_result": None, "logic": None,
            "determinate": None, "conditions_met": None, "conditions_not_met": None, "conditions_evaluable": None,
            "conditions_total": None, "conditions_unavailable": None, "trace": None, "unavailable": [],
            "required_features": entry_features(spec) if spec else [], "snapshot": None, "context_timing": None,
            "warnings": [], "exit_plan": S.exit_rule_texts(spec["exit"]) if spec else [],
            "risk": dict(spec["risk"]) if spec else None, "entry_text": S._group_text(spec["entry"]) if spec else None,
            "historical_evidence": None, "forward_evidence": None}


def _set_status(r: dict, status: str, text: str) -> dict:
    r.update(fit_status=status, fit_label=STATUS_LABEL[status], status_text=text)
    return r


def _summary_text(status: str, met: int, not_met: int, unavailable: int, total: int) -> str:
    def n(k, word):
        return f"{k} {word}{'' if k == 1 else 's'}"
    if status == RULES_MET:
        t = (f"The entry rules are met: all {n(total, 'condition')} are met." if met == total else
             f"The entry rules are met ({met} of {n(total, 'condition')} met; the rule's ANY / nested logic is satisfied).")
        if unavailable:
            t += f" {n(unavailable, 'condition')} could not be checked, but cannot change this result."
        return t
    if status == RULES_NOT_MET:
        t = f"{n(not_met, 'entry condition')} {'is' if not_met == 1 else 'are'} not currently met."
        if unavailable:
            t += f" {n(unavailable, 'condition')} could not be checked, but the rules are not met either way."
        return t
    return (f"{n(unavailable, 'required input')} {'is' if unavailable == 1 else 'are'} unavailable, so the entry rules "
            f"cannot be decided ({met} of {n(total, 'condition')} met). This is not the same as the rules not matching.")


def _context(cells: dict, used: List[str]) -> str:
    if any((cells.get(f) or {}).get("a") != "AVAILABLE" for f in used):
        return INCOMPLETE_CTX
    if any((cells.get(f) or {}).get("timing") == C.POST_CLOSE for f in used):
        return POST
    return STRICT


def evaluate_version(r: dict, spec: dict, cells_all: dict, T: date, evaluated_at: str, context_at: Optional[str]) -> dict:
    """Entry rules of ONE version against the shared feature environment — strategy.evaluate.group_met via the Stage 3.3
    trace helper. Counts are descriptive; the group result is authoritative."""
    used = entry_features(spec)
    cells = {fid: cells_all[fid] for fid in used if fid in cells_all}
    g = J._group_eval(spec["entry"], cells)
    leaves = J._leaves(g["trace"])
    met = sum(1 for x in leaves if x["result"] == "MET")
    not_met = sum(1 for x in leaves if x["result"] == "NOT_MET")
    total = len(leaves)
    unavailable = total - met - not_met
    status = RULES_MET if g["met"] else INCOMPLETE_DATA if not g["determinate"] else RULES_NOT_MET
    unav = []
    for x in leaves:
        if x["result"] not in ("MET", "NOT_MET"):
            c = cells.get(x.get("feature")) or {}
            code = c.get("reason") or c.get("a") or "NOT_COMPUTED"
            unav.append({"feature": x.get("feature"), "name": x.get("name"), "text": x.get("text"),
                         "availability": x.get("availability"), "reason": code, "reason_text": REASON_TEXT.get(code, code)})
    warnings = []
    x = spec["exit"]
    inv, tgt = x.get("invalidation") or {}, x.get("target") or {}
    if g["met"]:
        for method, fid, what in (("CLOSE_BELOW_ENTRY_SUPPORT", "stock.support", "support"),
                                  ("CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE", "stock.resistance", "resistance")):
            if method in (inv.get("method"), tgt.get("method")) and (cells_all.get(fid) or {}).get("v") is None:
                warnings.append({"code": "EXIT_LEVEL_UNAVAILABLE", "text": f"The exit plan uses the {what} level at entry, "
                                 f"which is unavailable at this close (a Stage 3.3 forward journal would SKIP this entry)."})
    ctx = _context(cells, used)
    if ctx == POST:
        warnings.append({"code": "POST_CLOSE_CONTEXT", "text": CONTEXT_TEXT[POST]})
    r.update(group_result=g["result"], logic=g["logic"], determinate=g["determinate"], conditions_met=met,
             conditions_not_met=not_met, conditions_evaluable=met + not_met, conditions_total=total,
             conditions_unavailable=unavailable, trace=g["trace"], unavailable=unav, context_timing=ctx, warnings=warnings,
             snapshot=J._snapshot(cells, T, evaluated_at, context_at, None, used))
    return _set_status(r, status, _summary_text(status, met, not_met, unavailable, total))


# ================================================================================================================
# SHARED WITH THE STAGE 4.0 SCANNER (one completed-session rule, one missing-data classification)
# ================================================================================================================

def resolve_session(series: Dict[str, BarSeries], last_complete: date) -> dict:
    """The decision session T: the latest SPY session on or before the last completed date. A weekday without a SPY
    bar is a holiday unless another loaded symbol has a bar that day. Returns {"T", "warning"} or {"T": None, "error"} —
    an older session is never used instead."""
    spy = series.get(SPY)
    cal = [d for d in (spy.dates if spy is not None else []) if d <= last_complete]
    holes = C.calendar_holes([cal[-1], last_complete + timedelta(days=1)]) if cal else []
    # one weekday without a SPY bar is a holiday unless another symbol has a bar that day (then SPY's bar is missing)
    gap_day = next((d for d in (cal[-1] + timedelta(days=i) for i in range(1, (last_complete - cal[-1]).days + 1))
                    if d.weekday() < 5), None) if cal and not holes else None
    seen_on_gap = sorted(s for s, ser in series.items() if s != SPY and gap_day is not None and ser.has(gap_day))
    if not cal or holes or seen_on_gap:
        msg = (f"The latest completed session could not be confirmed: {SPY} has no bar for "
               f"{holes[0]['weekdays_without_bar']} weekday(s) after {holes[0]['after']}." if holes else
               f"The latest completed session could not be confirmed: {SPY} has no bar for {gap_day.isoformat()}, "
               f"but {', '.join(seen_on_gap[:3])} {'does' if len(seen_on_gap) == 1 else 'do'}." if seen_on_gap else
               f"No {SPY} sessions are available, so the latest completed session is unknown.")
        return {"T": None, "error": msg + " An older session is never used instead."}
    T = cal[-1]
    warning = None
    if gap_day is not None:
        warning = {"code": "SESSION_ASSUMED_HOLIDAY", "text": f"No market data exists for "
                   f"{gap_day.isoformat()} (a weekday), so it is treated as a market holiday and "
                   f"{T.isoformat()} is the latest completed session."}
    return {"T": T, "warning": warning}


def missing_symbol_status(sym: str, ser: Optional[BarSeries], T: date) -> Tuple[str, str]:
    """Why a symbol has no feature row at T: no bars, no bar dated T (STALE — never an older session), or too little history."""
    if ser is None or not len(ser):
        return DATA_UNAVAILABLE, f"No daily bars were returned for {sym}."
    if not ser.has(T):
        last = [d for d in ser.dates if d <= T]
        return STALE_DATA, (f"{sym} has no daily bar for the decision session {T.isoformat()}"
                            f"{f' (its latest bar is {last[-1].isoformat()})' if last else ''}. The rules are not evaluated "
                            "on an older session.")
    return DATA_UNAVAILABLE, f"Not enough daily history for {sym} to evaluate its rules."


def symbol_warnings(sym: str, T: date, research, ev_stock: dict, event_stock_features: List[str]) -> List[dict]:
    """Per-stock context warnings (saved research timing / availability, earnings calendar)."""
    out = []
    if research is not None and research[1].get("source") == "SAVED_SNAPSHOT" and not research[1].get("existed_at_close"):
        out.append({"code": "RESEARCH_AFTER_CLOSE", "text": f"The saved research for {sym} was created after "
                    f"the {T.isoformat()} close; it is labelled POST-CLOSE, not as if it existed at the close."})
    if research is not None and research[1].get("source") != "SAVED_SNAPSHOT":
        out.append({"code": "RESEARCH_UNAVAILABLE", "text": REASON_TEXT["RESEARCH_UNAVAILABLE"]})
    if event_stock_features and sym in ev_stock:
        ea = ((ev_stock[sym][1].get("providers") or {}).get("earnings") or {}).get("available")
        if not ea:
            out.append({"code": "EARNINGS_CALENDAR_UNAVAILABLE", "text": "There is no verified earnings-calendar "
                        "provider, so a stock event risk below HIGH is unavailable (never assumed low)."})
    return out


# ================================================================================================================
# THE EVALUATION
# ================================================================================================================

def _sort_key(r: dict):
    return (str(r["strategy_name"]).casefold(), r["strategy_id"], r["version"])


def evaluate(symbol: str, include_old_versions: bool = False, now: Optional[datetime] = None, *,
             path: Optional[Path] = None, fetch_fn=None, client=None, research_db: Optional[Callable] = None,
             events_fn: Optional[Callable] = None, coverage_fn: Optional[Callable] = None,
             cache: Optional[BarCache] = None) -> dict:
    t0 = time.perf_counter()
    sym = normalise_symbol(symbol)
    now = _utc(now)
    cache = BAR_CACHE if cache is None else cache
    path = Path(path) if path else RO.db_path()
    bro, fro = RO.ReadOnlyBacktestStore(path), RO.ReadOnlyForwardStore(path)
    evaluated_at = C.utc_iso(now)

    # 1-2. saved versions, integrity, universe --------------------------------------------------------------------------
    versions = RO.saved_versions(path, include_old_versions)
    results, evaluable = [], []
    for v in versions:
        ref, spec, checks, errors = J.eligibility(bro, v["strategy_id"], v["version_number"])
        if not errors and ref["strategy_version_id"] != v["strategy_version_id"]:
            errors = [{"code": "STRATEGY_INTEGRITY_ERROR", "message": "The stored version id does not match."}]
        r = _base(v, ref, spec, checks, errors)
        results.append(r)
        # a hash-verified spec's universe is trustworthy even when the version cannot be evaluated (registry mismatch,
        # unsupported features); an integrity failure means nothing in the spec is trusted, universe included
        trusted = spec is not None and not any(e["code"] in ("STRATEGY_INTEGRITY_ERROR", "NOT_FOUND") for e in errors)
        if trusted and sym not in spec["universe"]["symbols"]:
            _set_status(r, OUTSIDE_UNIVERSE, f"{sym} is not in this version's saved universe, so its rules are not "
                        f"evaluated for {sym}." + (f" (This version is also not evaluable: "
                                                   f"{STATUS_LABEL[_error_status(errors)]}.)" if errors else ""))
        elif errors:
            _set_status(r, _error_status(errors), errors[0]["message"])
        else:
            evaluable.append((r, spec))
    t_elig = time.perf_counter()
    out = {"engine_version": ENGINE_VERSION, "status": "EVALUATED", "symbol": sym, "include_old_versions": include_old_versions,
           "decision_session": None, "decision_session_close": None, "evaluated_at": evaluated_at,
           "context_timing": None, "context_text": None, "data_freshness": None, "plan": None, "environment": None,
           "counts": None, "strategies": results, "warnings": [], "message": None, "note": NOTE, "rule_text": RULE_TEXT,
           "session_rule": SESSION_RULE, "timings": None}

    def finish(t_data=None, t_feat=None, t_eval=None):
        t_ev0 = time.perf_counter()
        for r, spec in evaluable:                      # stored evidence of the EXACT version (never recomputed)
            r["historical_evidence"] = E.historical(bro, r["strategy_version_id"], r["spec_hash"])
            r["forward_evidence"] = E.forward(fro, r["strategy_version_id"], sym)
        t_end = time.perf_counter()
        results.sort(key=_sort_key)
        counts = {k: sum(1 for r in results if r["fit_status"] == k) for k in STATUS_LABEL}
        out["counts"] = {"checked": len(results), **counts,
                         "not_evaluated": sum(counts[k] for k in (UNSUPPORTED, INTEGRITY_ERROR, REGISTRY_MISMATCH,
                                                                  STALE_DATA, DATA_UNAVAILABLE))}
        ev = [r["context_timing"] for r in results if r["fit_status"] in EVALUATED]
        out["context_timing"] = (INCOMPLETE_CTX if INCOMPLETE_CTX in ev else POST if POST in ev else STRICT) if ev else None
        out["context_text"] = CONTEXT_TEXT.get(out["context_timing"])
        rnd = lambda a, b: round(b - a, 3) if a is not None and b is not None else None  # noqa: E731
        out["timings"] = {"eligibility_s": rnd(t0, t_elig), "market_data_s": rnd(t_elig, t_data), "features_s": rnd(t_data, t_feat),
                          "evaluation_s": rnd(t_feat, t_eval), "evidence_s": rnd(t_ev0, t_end), "total_s": rnd(t0, t_end)}
        return out

    if not evaluable:
        out["status"] = "NO_SAVED_STRATEGIES" if not versions else "NOTHING_TO_EVALUATE"
        out["message"] = ("No saved strategies yet." if not versions else
                          f"No saved version includes {sym} in its universe (or every one that does is not evaluable). "
                          "No market data was requested.")
        return finish()

    # 3. one dependency plan ------------------------------------------------------------------------------------------
    needs, fo = plan(sym, [spec for _, spec in evaluable])
    last_complete = B.last_complete_session_date(now)
    start, end = C.need_range(last_complete, None)
    out["plan"] = {"symbols": list(needs.symbols()), "context_symbols": list(needs.context_symbols()),
                   "index_symbols": list(needs.index_symbols), "breadth_list": bool(needs.basket),
                   "breadth_list_size": len(needs.basket), "sector_etfs": sorted({e for _, e in needs.sector_etfs}),
                   "stock_features": list(needs.stock_features), "market_features": list(needs.market_features),
                   "sector_features": list(needs.sector_features), "research_features": fo["research"],
                   "event_features": fo["event_stock"] + fo["event_market"], "versions_evaluated": len(evaluable),
                   "window": {"start": start.isoformat(), "end": end.isoformat()}}

    def unavailable_all(status: str, message: str, code: str):
        out.update(status=code, message=message)
        out["warnings"].append({"code": code, "text": message})
        for r, _ in evaluable:
            _set_status(r, status, message)

    # 4-5. bars + the decision session -------------------------------------------------------------------------------
    try:
        series, prov, fetched = load_bars(bro, needs.symbols(), start, end, now, cache, fetch_fn, client)
    except BacktestError as exc:
        unavailable_all(DATA_UNAVAILABLE, f"Market data is unavailable: {exc.message}", DATA_UNAVAILABLE)
        return finish(time.perf_counter())
    except Exception as exc:  # noqa: BLE001 - network / provider failure: report it, never guess a session
        unavailable_all(DATA_UNAVAILABLE, f"Market data is unavailable ({type(exc).__name__}).", DATA_UNAVAILABLE)
        return finish(time.perf_counter())
    t_data = time.perf_counter()
    out["data_freshness"] = {"market": {"feed": B.feed(), "adjustment": B.ADJUSTMENT, "timeframe": "1Day",
                                        "last_complete_date": last_complete.isoformat(), "sources": prov,
                                        "fetched": fetched, "window_calendar_days": C.LOOKBACK},
                             "research": None, "events": None}
    sess = resolve_session(series, last_complete)
    if sess["T"] is None:
        unavailable_all(DATA_UNAVAILABLE, sess["error"], DATA_UNAVAILABLE)
        return finish(t_data)
    T = sess["T"]
    if sess["warning"]:
        out["warnings"].append(sess["warning"])
    out["decision_session"] = T.isoformat()
    out["decision_session_close"] = C.utc_iso(C.close_utc(T))
    ser = series.get(sym)
    out["data_freshness"]["market"]["symbol_last_bar"] = ser.dates[-1].isoformat() if ser is not None and len(ser) else None

    # 6. ONE feature environment for (symbol, T) ---------------------------------------------------------------------
    snap = C.technical_snapshot(series, needs, T)
    s = snap["symbols"].get(sym)
    if s is None:
        status, message = missing_symbol_status(sym, ser, T)
        unavailable_all(status, message, status)
        return finish(t_data, time.perf_counter())
    cells_all = {**snap["market"], **s["cells"]}
    research = C.research_context([sym], fo["research"], now, T, research_db)[sym] if fo["research"] else None
    ev_stock, ev_market = (C.event_context([sym], fo["event_stock"], fo["event_market"], now, events_fn, coverage_fn)
                           if (fo["event_stock"] or fo["event_market"]) else ({}, None))
    if research is not None:
        cells_all.update(research[0])
    if sym in ev_stock:
        cells_all.update(ev_stock[sym][0])
    if ev_market is not None:
        cells_all.update(ev_market[0])
    context_at = evaluated_at if (fo["research"] or fo["event_stock"] or fo["event_market"]) else None
    out["data_freshness"]["research"] = ({"used": True, "features": fo["research"], **research[1]} if research is not None
                                         else {"used": False})
    out["data_freshness"]["events"] = ({"used": True, "stock": ev_stock[sym][1] if sym in ev_stock else None,
                                        "market": ev_market[1] if ev_market is not None else None}
                                       if (fo["event_stock"] or fo["event_market"]) else {"used": False})
    out["plan"].update(snapshots_built=1, replay=snap["replay"], breadth_members=snap["breadth_members"],
                       spy_sessions_in_window=snap["spy_sessions"])
    out["environment"] = J._snapshot(cells_all, T, evaluated_at, context_at, None, [])
    t_feat = time.perf_counter()

    # 7. rules — every evaluable version against the SAME environment -------------------------------------------------
    for r, spec in evaluable:
        evaluate_version(r, spec, cells_all, T, evaluated_at, context_at)
    t_eval = time.perf_counter()

    if needs.basket:
        out["warnings"].append({"code": "BREADTH_LIST_FIXED", "text": "Breadth-based market features use the app's "
                                "current fixed list of 80 large stocks (survivorship caveat)."})
    if needs.sector_features:
        out["warnings"].append({"code": "SECTOR_MAP_STATIC", "text": "Sector features use the app's static sector map."})
    out["warnings"].extend(symbol_warnings(sym, T, research, ev_stock, fo["event_stock"]))
    return finish(t_data, t_feat, t_eval)


def public_config(path: Optional[Path] = None) -> dict:
    """What the Strategy Fit view needs before any evaluation: symbol sources and labels. Local reads only."""
    from scanner.watchlist import load_watchlist
    path = Path(path) if path else RO.db_path()
    versions = RO.saved_versions(path, include_old=False)
    try:
        watch = [s for s in load_watchlist() if S.SYMBOL_RE.match(s)]
    except Exception:  # noqa: BLE001 - a missing watchlist file only removes one symbol source
        watch = []
    return {"engine_version": ENGINE_VERSION, "symbols": {"strategies": RO.strategy_symbols(path), "watchlist": watch},
            "saved_strategies": len(versions), "statuses": STATUS_LABEL,
            "filters": {"ALL": "All", RULES_MET: "Rules met", RULES_NOT_MET: "Rules not met", INCOMPLETE_DATA: "Incomplete",
                        OUTSIDE_UNIVERSE: "Outside universe", "NOT_EVALUATED": "Not evaluated"},
            "context_labels": {STRICT: "STRICT CLOSE", POST: "POST CLOSE", INCOMPLETE_CTX: "INCOMPLETE"},
            "context_text": CONTEXT_TEXT, "note": NOTE, "rule_text": RULE_TEXT, "session_rule": SESSION_RULE,
            "reason_text": REASON_TEXT, "limitations": LIMITATIONS, "registry_fingerprint": F.fingerprint()}
