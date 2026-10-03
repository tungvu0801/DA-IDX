"""
fit/scanner.py — Stage 4.0 deterministic, READ-ONLY STRATEGY SCANNER.

"For ONE exact saved strategy version, which stocks in a list I choose currently have its entry rules met, not met,
incomplete, stale, or outside the saved universe?" — Strategy Fit (fit/current.py) turned sideways: one version, many
stocks, the SAME building blocks and no second rules engine.

  1. version      the exact saved version, re-verified with the Stage 3.3 / 3.4 eligibility checks (integrity, registry,
                  schema, forward support). INTEGRITY ERROR / REGISTRY MISMATCH / UNSUPPORTED versions are rejected.
  2. list         SAVED_UNIVERSE / WATCHLIST / HOLDINGS are resolved on the server; CUSTOM is validated again here.
                  Upper-cased, trimmed, de-duplicated, alphabetical; more than MAX_SYMBOLS is refused (never truncated).
  3. universe     a symbol outside the version's saved universe is OUTSIDE UNIVERSE: no rule and no data work for it.
  4. one plan     the version's entry rules for every in-universe symbol, planned ONCE with Stage 3.2 / 3.3's own
                  dependency rules (breadth list only if a rule needs it; sector ETFs only if used, de-duplicated).
  5. bars         fit.current.load_bars — the same Stage 3.2 cache / memory / one batched read-only request as Strategy Fit.
  6. session      fit.current.resolve_session — ONE decision session for the whole scan (Stage 3.3 completed-session
                  rule). A symbol without a bar for that session is STALE DATA; an older session is never used.
  7. features     ONE shared snapshot (backtest.snapshots.day_snapshot) for all symbols at that session: market and
                  breadth features computed once; saved research (0 Claude calls) and event context read in one batch.
  8. rules        fit.current.evaluate_version per symbol — strategy.evaluate.group_met via the Stage 3.3 trace helper,
                  exactly Strategy Fit's per-version result. Counts are descriptive; the group result is authoritative.

Nothing is written, scheduled, ranked or advised. Results are grouped by status and alphabetical within a group.
"""
from __future__ import annotations

import dataclasses
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import config
from backtest import bars as B
from fit import current as FC
from fit import evidence as E
from fit import readonly as RO
from forward import capture as C
from forward import journal as J
from strategy import spec as S

ENGINE_VERSION = "4.0.0"
MAX_SYMBOLS = 100
SOURCES = {"SAVED_UNIVERSE": "Saved strategy universe", "WATCHLIST": "Current watchlist", "HOLDINGS": "Current holdings",
           "CUSTOM": "Custom list"}
# display groups in a FIXED order (never by performance); rows are alphabetical inside each group
GROUPS = [("RULES_MET", "Rules met", (FC.RULES_MET,)), ("RULES_NOT_MET", "Rules not met", (FC.RULES_NOT_MET,)),
          ("INCOMPLETE_DATA", "Incomplete data", (FC.INCOMPLETE_DATA,)),
          ("DATA_ISSUES", "Stale / data unavailable", (FC.STALE_DATA, FC.DATA_UNAVAILABLE)),
          ("OUTSIDE_UNIVERSE", "Outside universe", (FC.OUTSIDE_UNIVERSE,)),
          ("NOT_EVALUATED", "Error / unsupported", (FC.UNSUPPORTED, FC.INTEGRITY_ERROR, FC.REGISTRY_MISMATCH))]
GROUP_OF = {st: g for g, _, sts in GROUPS for st in sts}
STATUS_LABEL = {**FC.STATUS_LABEL, FC.OUTSIDE_UNIVERSE: "OUTSIDE UNIVERSE"}
NOTE = ("The scanner compares ONE saved strategy version's entry rules with the latest completed close of each stock in "
        "the list you chose. It does not rank stocks or strategies, give advice, predict returns or place orders.")
LIMITATIONS = [
    "One exact saved strategy version at a time; its saved universe stays authoritative.",
    "It does not rank, advise or alert; results are grouped by status and alphabetical.",
    "Latest completed daily close only; daily-bar limitations remain.",
    "Market breadth uses the app's current fixed list of large stocks where a rule needs it (survivorship caveat).",
    "Sector features use the app's static sector map.",
    "There is no verified earnings calendar, so a stock event risk below HIGH is unavailable.",
    "Saved research may be unavailable or created after the close; that timing is shown.",
    "Scanner results are current derived views, not stored evidence.",
]
_SPLIT = re.compile(r"[\s,;]+")


class ScanError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


# ---- symbol lists ----------------------------------------------------------------------------------------------------------

def normalise_symbols(values, split: bool = True) -> Tuple[List[str], List[str]]:
    """(valid, invalid): upper-cased, trimmed, de-duplicated, alphabetical. Typed (CUSTOM) input is also split on commas /
    spaces; a server-side list (universe, watchlist, holdings) is one symbol per entry — never split into new tickers."""
    tokens = []
    for v in values or []:
        tokens += [t for t in _SPLIT.split(str(v).strip()) if t] if split else ([str(v).strip()] if str(v).strip() else [])
    valid, invalid = set(), []
    for t in tokens:
        s = t.strip().upper()
        (valid.add(s) if S.SYMBOL_RE.match(s) else invalid.append(t))
    return sorted(valid), invalid


def _holdings() -> dict:
    """The existing read-only holdings shortcut (Stage 3.1): one gateway GET /positions when portfolio awareness is on."""
    from api.routes.strategies import get_holding_symbols
    return json.loads(get_holding_symbols().body)


def _watchlist() -> List[str]:
    from scanner.watchlist import load_watchlist
    return list(load_watchlist())


def resolve_list(source: str, symbols: Optional[List[str]], spec: dict, watchlist_fn: Optional[Callable] = None,
                 holdings_fn: Optional[Callable] = None) -> Tuple[List[str], List[dict]]:
    """The authoritative symbol list (server side) and any list warnings. Raises ScanError, never truncates."""
    if source not in SOURCES:
        raise ScanError("INVALID_SOURCE", f"Unknown list source {source!r}.")
    if source != "CUSTOM" and symbols:
        raise ScanError("SYMBOLS_NOT_ACCEPTED", "Symbols are only accepted for a CUSTOM list; the server resolves the others.")
    warnings: List[dict] = []
    if source == "SAVED_UNIVERSE":
        syms, bad = normalise_symbols(spec["universe"]["symbols"], split=False)
    elif source == "WATCHLIST":
        try:
            raw = (watchlist_fn or _watchlist)()
        except Exception:  # noqa: BLE001 - an unreadable watchlist is an empty source, never a crash
            raw = []
        syms, bad = normalise_symbols(raw, split=False)
        if bad:
            warnings.append({"code": "WATCHLIST_ENTRIES_IGNORED", "text": f"{len(bad)} watchlist entr{'y is' if len(bad) == 1 else 'ies are'} "
                             f"not a valid ticker and {'was' if len(bad) == 1 else 'were'} not scanned: {', '.join(map(str, bad[:5]))}."})
        if not syms:
            raise ScanError("EMPTY_LIST", "The watchlist is empty (or unavailable), so there is nothing to scan.")
    elif source == "HOLDINGS":
        h = (holdings_fn or _holdings)()
        if not h.get("available"):
            raise ScanError("HOLDINGS_UNAVAILABLE", f"Holdings are unavailable: {h.get('message') or 'not connected'}. "
                            "The saved universe, the watchlist and a custom list still work.", 409)
        syms, bad = normalise_symbols(h.get("symbols") or [], split=False)
        if not syms:
            raise ScanError("EMPTY_LIST", "There are no holdings to scan.")
    else:
        syms, bad = normalise_symbols(symbols or [])
        if bad:
            raise ScanError("INVALID_SYMBOL", f"Not a valid ticker: {', '.join(map(str, bad[:10]))}. Nothing was scanned.",
                            invalid=bad[:50])
        if not syms:
            raise ScanError("EMPTY_LIST", "Enter at least one ticker symbol.")
    if len(syms) > MAX_SYMBOLS:
        raise ScanError("TOO_MANY_SYMBOLS", f"{len(syms)} symbols were requested; the limit is {MAX_SYMBOLS} per scan. "
                        "Nothing was scanned (the list is never shortened silently).", requested=len(syms), limit=MAX_SYMBOLS)
    return syms, warnings


# ---- the exact version -------------------------------------------------------------------------------------------------------

def find_version(strategy_version_id: str, path: Path) -> dict:
    v = next((x for x in RO.saved_versions(path, include_old=True) if x["strategy_version_id"] == strategy_version_id), None)
    if v is None:
        raise ScanError("NOT_FOUND", "That saved strategy version does not exist (or its strategy is archived).", 404)
    return v


def checked_version(v: dict, bro) -> Tuple[dict, dict, List[dict]]:
    ref, spec, checks, errors = J.eligibility(bro, v["strategy_id"], v["version_number"])
    if not errors and ref["strategy_version_id"] != v["strategy_version_id"]:
        errors = [{"code": "STRATEGY_INTEGRITY_ERROR", "message": "The stored version id does not match."}]
    if errors:
        status = FC._error_status(errors)
        raise ScanError(status, f"{FC.STATUS_LABEL[status]}: {errors[0]['message']} This version cannot be scanned.", 409)
    return ref, spec, checks


def _entry_only_many(spec: dict, symbols: List[str]) -> dict:
    """fit.current.entry_only for a list of symbols: the entry group only, used ONLY to plan data dependencies."""
    s = FC.entry_only(spec, symbols[0])
    s["universe"] = {**s["universe"], "symbols": list(symbols)}
    return s


# ---- one row -------------------------------------------------------------------------------------------------------------------

_ROW_DROP = ("universe", "universe_origin", "exit_plan", "risk", "entry_text", "historical_evidence", "forward_evidence",
             "eligibility", "spec_hash", "rules_hash", "strategy_name", "version_name", "version", "is_current",
             "current_version", "readiness", "strategy_id", "strategy_version_id", "required_features")


def _row(sym: str, base: dict) -> dict:
    r = {k: v for k, v in base.items() if k not in _ROW_DROP}
    return {"symbol": sym, **r}


def _finish_row(r: dict) -> dict:
    leaves = J._leaves(r["trace"]) if r.get("trace") else []
    r["group"] = GROUP_OF[r["fit_status"]]
    r["fit_label"] = STATUS_LABEL[r["fit_status"]]
    r["main_unmet"] = [{"text": x.get("text"), "feature": x.get("feature"), "actual": x.get("actual"),
                        "actual_label": x.get("actual_label"), "unit": x.get("unit")}
                       for x in leaves if x["result"] == "NOT_MET"][:3]
    r["main_unavailable"] = [{"text": u.get("text"), "feature": u.get("feature"), "reason": u.get("reason_text")}
                             for u in r.get("unavailable") or []][:3]
    return r


# ---- the scan ----------------------------------------------------------------------------------------------------------------

def scan(strategy_version_id: str, source: str, symbols: Optional[List[str]] = None, now: Optional[datetime] = None, *,
         path: Optional[Path] = None, fetch_fn=None, client=None, research_db: Optional[Callable] = None,
         events_fn: Optional[Callable] = None, coverage_fn: Optional[Callable] = None, cache: Optional[FC.BarCache] = None,
         watchlist_fn: Optional[Callable] = None, holdings_fn: Optional[Callable] = None) -> dict:
    t0 = time.perf_counter()
    now = FC._utc(now)
    cache = FC.BAR_CACHE if cache is None else cache
    path = Path(path) if path else RO.db_path()
    bro, fro = RO.ReadOnlyBacktestStore(path), RO.ReadOnlyForwardStore(path)
    evaluated_at = C.utc_iso(now)

    # 1-3. version, list, universe ------------------------------------------------------------------------------------------
    v = find_version(strategy_version_id, path)
    ref, spec, checks = checked_version(v, bro)
    syms, warnings = resolve_list(source, symbols, spec, watchlist_fn, holdings_fn)
    t_list = time.perf_counter()
    universe = set(spec["universe"]["symbols"])
    base = FC._base(v, ref, spec, checks, [])
    name = f"{v['strategy_name']} v{v['version_number']}"
    rows: Dict[str, dict] = {}
    inside = [s for s in syms if s in universe]
    for sym in syms:
        rows[sym] = _row(sym, base)
        if sym not in universe:
            FC._set_status(rows[sym], FC.OUTSIDE_UNIVERSE, f"{sym} is not in {name}'s saved universe. No rule evaluation "
                           "was performed and no data was requested for it.")
    hist = E.historical(bro, v["strategy_version_id"], ref["spec_hash"])
    fwd = E.forward(fro, v["strategy_version_id"], sorted(universe)[0])
    out = {
        "engine_version": ENGINE_VERSION, "status": "SCANNED", "message": None,
        "strategy": {"strategy_id": v["strategy_id"], "strategy_version_id": v["strategy_version_id"], "name": v["strategy_name"],
                     "version_name": base["version_name"], "version": v["version_number"], "is_current": v["is_current"],
                     "current_version": v["current_version"], "readiness": v["readiness"], "spec_hash": ref["spec_hash"],
                     "rules_hash": ref["rules_hash"], "schema_version": spec.get("schema_version"),
                     "feature_registry_version": spec.get("feature_registry_version"),
                     "feature_registry_fingerprint": spec.get("feature_registry_fingerprint"),
                     "integrity_verified": True, "checks": checks, "universe": sorted(universe),
                     "entry_text": base["entry_text"], "logic": spec["entry"]["logic"], "required_features": base["required_features"],
                     "evidence": {"stored_backtests": hist["count"], "completed_backtests": hist["completed"],
                                  "forward_journal": (fwd.get("journal") or {}).get("status") if fwd.get("available") else None,
                                  "forward_journals": fwd.get("journals", 0)}},
        "source": source, "source_label": SOURCES[source], "requested_symbols": syms, "requested_count": len(syms),
        "evaluated_count": 0, "decision_session": None, "decision_session_close": None, "evaluated_at": evaluated_at,
        "context_timing": None, "data_freshness": None, "plan": None, "status_counts": None, "groups": None, "results": None,
        "warnings": warnings, "note": NOTE, "rule_text": FC.RULE_TEXT, "session_rule": FC.SESSION_RULE, "limitations": LIMITATIONS,
        "timings": None}

    def finish(t_data=None, t_feat=None, t_ctx=None, t_eval=None):
        results = [_finish_row(rows[s]) for s in syms]          # syms is alphabetical
        out["results"] = results
        out["status_counts"] = {k: sum(1 for r in results if r["fit_status"] == k) for k in STATUS_LABEL}
        out["groups"] = [{"group": g, "label": label, "count": sum(1 for r in results if r["group"] == g),
                          "symbols": [r["symbol"] for r in results if r["group"] == g]} for g, label, _ in GROUPS]
        out["evaluated_count"] = sum(1 for r in results if r["fit_status"] in FC.EVALUATED)
        ev = [r["context_timing"] for r in results if r["fit_status"] in FC.EVALUATED]
        out["context_timing"] = (FC.INCOMPLETE_CTX if FC.INCOMPLETE_CTX in ev else FC.POST if FC.POST in ev else FC.STRICT) if ev else None
        out["context_text"] = FC.CONTEXT_TEXT.get(out["context_timing"])
        t_end = time.perf_counter()
        rnd = lambda a, b: round(b - a, 4) if a is not None and b is not None else None  # noqa: E731
        out["timings"] = {"version_and_list_s": rnd(t0, t_list), "market_data_s": rnd(t_list, t_data),
                          "shared_features_s": rnd(t_data, t_feat), "research_events_s": rnd(t_feat, t_ctx),
                          "rule_evaluation_s": rnd(t_ctx, t_eval), "total_s": rnd(t0, t_end)}
        return out

    if not inside:
        out.update(status="NOTHING_TO_EVALUATE", message="No symbol in this list is in the version's saved universe, so no "
                   "rules were evaluated and no market data was requested.")
        return finish()

    # 4. ONE dependency plan for this version across every in-universe symbol --------------------------------------------------
    planning = _entry_only_many(spec, inside)
    needs = C.price_needs(planning)
    fo = C.forward_only_features(planning)
    last_complete = B.last_complete_session_date(now)
    start, end = C.need_range(last_complete, None)
    out["plan"] = {"symbols": list(needs.universe), "context_symbols": list(needs.context_symbols()),
                   "index_symbols": list(needs.index_symbols), "breadth_list": bool(needs.basket),
                   "breadth_list_size": len(needs.basket), "sector_etfs": sorted({e for _, e in needs.sector_etfs}),
                   "stock_features": list(needs.stock_features), "market_features": list(needs.market_features),
                   "sector_features": list(needs.sector_features), "research_features": fo["research"],
                   "event_features": fo["event_stock"] + fo["event_market"], "outside_universe_skipped": len(syms) - len(inside),
                   "window": {"start": start.isoformat(), "end": end.isoformat()}, "snapshots_built": 0}

    def unavailable_all(message: str, code: str = FC.DATA_UNAVAILABLE):
        out.update(status=code, message=message)
        out["warnings"].append({"code": code, "text": message})
        for s in inside:
            FC._set_status(rows[s], FC.DATA_UNAVAILABLE, message)

    # 5-6. bars + ONE decision session --------------------------------------------------------------------------------------------
    try:
        series, prov, fetched = FC.load_bars(bro, needs.symbols(), start, end, now, cache, fetch_fn, client)
    except Exception as exc:  # noqa: BLE001 - network / provider / cache-integrity failure: report it, never guess
        unavailable_all(f"Market data is unavailable ({getattr(exc, 'message', None) or type(exc).__name__}).")
        return finish(time.perf_counter())
    t_data = time.perf_counter()
    out["data_freshness"] = {"market": {"feed": B.feed(), "adjustment": B.ADJUSTMENT, "timeframe": "1Day",
                                        "last_complete_date": last_complete.isoformat(), "fetched": fetched,
                                        "window_calendar_days": C.LOOKBACK}, "research": None, "events": None}
    sess = FC.resolve_session(series, last_complete)
    if sess["T"] is None:
        unavailable_all(sess["error"])
        return finish(t_data)
    T = sess["T"]
    if sess["warning"]:
        out["warnings"].append(sess["warning"])
    out["decision_session"] = T.isoformat()
    out["decision_session_close"] = C.utc_iso(C.close_utc(T))

    # 7. ONE shared snapshot (per-symbol fallback isolates a failing symbol) ---------------------------------------------------------
    snaps, failed = {}, {}
    try:
        snap = C.technical_snapshot(series, needs, T)
        market, snaps = snap["market"], snap["symbols"]
        out["plan"].update(snapshots_built=1, breadth_members=snap["breadth_members"], replay=snap["replay"])
    except Exception:  # noqa: BLE001 - one bad symbol must not fail the scan: rebuild symbol by symbol
        market, built = None, 0
        for s in inside:
            one = dataclasses.replace(needs, universe=(s,), sector_etfs=tuple(x for x in needs.sector_etfs if x[0] == s))
            try:
                sn = C.technical_snapshot(series, one, T)
                built += 1
                market = sn["market"] if market is None else market
                if s in sn["symbols"]:
                    snaps[s] = sn["symbols"][s]
            except Exception as exc:  # noqa: BLE001
                failed[s] = type(exc).__name__
        market = market or {}
        out["plan"].update(snapshots_built=built, per_symbol_fallback=True)
    ready = [s for s in inside if s in snaps]
    for s in inside:
        if s in failed:
            FC._set_status(rows[s], FC.DATA_UNAVAILABLE, f"Features could not be computed for {s} ({failed[s]}).")
        elif s not in snaps:
            status, message = FC.missing_symbol_status(s, series.get(s), T)
            FC._set_status(rows[s], status, message)
        rows[s]["data"] = {"last_bar": (series[s].dates[-1].isoformat() if s in series and len(series[s]) else None),
                           "source": (prov.get(s) or {}).get("source")}
    t_feat = time.perf_counter()

    # saved research (0 Claude calls) and event context, once for the whole list ---------------------------------------------------
    research = C.research_context(ready, fo["research"], now, T, research_db) if fo["research"] and ready else {}
    ev_stock, ev_market = (C.event_context(ready, fo["event_stock"], fo["event_market"], now, events_fn, coverage_fn)
                           if ready and (fo["event_stock"] or fo["event_market"]) else ({}, None))
    context_at = evaluated_at if (fo["research"] or fo["event_stock"] or fo["event_market"]) else None
    out["data_freshness"]["research"] = ({"used": True, "features": fo["research"], "symbols_with_saved_research":
                                          sorted(s for s, x in research.items() if x[1].get("source") == "SAVED_SNAPSHOT")}
                                         if fo["research"] else {"used": False})
    out["data_freshness"]["events"] = ({"used": True, "market": ev_market[1] if ev_market is not None else None}
                                       if (fo["event_stock"] or fo["event_market"]) else {"used": False})
    t_ctx = time.perf_counter()

    # 8. rules — Strategy Fit's per-version evaluation, one symbol at a time --------------------------------------------------------
    for s in ready:
        cells_all = {**market, **snaps[s]["cells"]}
        if s in research:
            cells_all.update(research[s][0])
        if s in ev_stock:
            cells_all.update(ev_stock[s][0])
        if ev_market is not None:
            cells_all.update(ev_market[0])
        FC.evaluate_version(rows[s], spec, cells_all, T, evaluated_at, context_at)
        rows[s]["symbol_warnings"] = FC.symbol_warnings(s, T, research.get(s), ev_stock, fo["event_stock"])
    t_eval = time.perf_counter()
    if needs.basket:
        out["warnings"].append({"code": "BREADTH_LIST_FIXED", "text": "Breadth-based market features use the app's current "
                                "fixed list of large stocks (survivorship caveat); it was calculated once for this scan."})
    if needs.sector_features:
        out["warnings"].append({"code": "SECTOR_MAP_STATIC", "text": "Sector features use the app's static sector map."})
    return finish(t_data, t_feat, t_ctx, t_eval)


# ---- what the Scanner view needs before a scan -------------------------------------------------------------------------------

def public_config(path: Optional[Path] = None, watchlist_fn: Optional[Callable] = None) -> dict:
    """Saved versions (identity + universe), the server-side watchlist, holdings availability, limits and labels.
    Local reads only: the broker gateway is contacted only when a HOLDINGS scan is requested."""
    path = Path(path) if path else RO.db_path()
    bro = RO.ReadOnlyBacktestStore(path)
    versions = []
    for v in RO.saved_versions(path, include_old=True):
        ref, spec, _, errors = J.eligibility(bro, v["strategy_id"], v["version_number"])
        ok = not errors and ref is not None and ref["strategy_version_id"] == v["strategy_version_id"]
        versions.append({**v, "scannable": ok, "status": None if ok else FC.STATUS_LABEL[FC._error_status(errors or [{"code": "STRATEGY_INTEGRITY_ERROR"}])],
                         "reason": None if ok else (errors[0]["message"] if errors else "The stored version id does not match."),
                         "universe": sorted(spec["universe"]["symbols"]) if (spec and ok) else []})
    try:
        watch, bad = normalise_symbols((watchlist_fn or _watchlist)(), split=False)
    except Exception:  # noqa: BLE001
        watch, bad = [], []
    return {"engine_version": ENGINE_VERSION, "versions": versions, "sources": SOURCES, "max_symbols": MAX_SYMBOLS,
            "watchlist": {"symbols": watch, "ignored": len(bad)},
            "holdings": {"enabled": bool(config.PORTFOLIO_AWARENESS_ENABLED),
                         "note": ("Resolved from your read-only holdings when you scan." if config.PORTFOLIO_AWARENESS_ENABLED
                                  else "Portfolio awareness is turned off, so holdings are unavailable.")},
            "statuses": STATUS_LABEL, "groups": [{"group": g, "label": label} for g, label, _ in GROUPS],
            "context_labels": {FC.STRICT: "STRICT CLOSE", FC.POST: "POST CLOSE", FC.INCOMPLETE_CTX: "INCOMPLETE"},
            "note": NOTE, "session_rule": FC.SESSION_RULE, "limitations": LIMITATIONS}
