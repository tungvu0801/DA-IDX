"""
backtest/runs.py — eligibility, preflight, run configuration + hash, and the one-shot run job.

Only saved, immutable, BACKTEST_READY strategy versions run. Before every run (at preflight, again when the run is
created, and again inside the job) the version is re-verified: it exists, its table is trigger-protected, its stored
JSON re-hashes to spec_hash, rules_hash re-computes, it still validates unchanged, and its schema / registry version /
registry fingerprint / readiness match the running code. Nothing is downgraded or substituted.

Runs are explicit user actions. Each run is a one-shot background thread in this server process (status PENDING ->
RUNNING -> COMPLETED | FAILED, persisted), at most one at a time. There is no scheduler and nothing recurs.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import asdict
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import config
from backtest import bars as B
from backtest import metrics as M
from backtest.engine import ENGINE_VERSION, SELECTION_POLICIES, RunConfig, simulate
from backtest.replay import BarSeries
from backtest.snapshots import (BASKET_FEATURES, MIN_SESSIONS, SPY, Needs, compute_snapshots, default_workers,
                                needs_for)
from backtest.store import BacktestError, BacktestStore, canonical, sha256
from strategy import features as F
from strategy import spec as S

LOOKBACK = config.DAILY_BAR_LOOKBACK_DAYS
LIMITS = {"initial_equity": (1000.0, 100_000_000.0), "slippage_bps_per_side": (0.0, 500.0),
          "commission_per_order": (0.0, 1000.0), "max_years": 10, "min_sessions": 2, "earliest_start": "2000-01-01"}
DEFAULTS = {"initial_equity": 100000.0, "slippage_bps_per_side": 0.0, "commission_per_order": 0.0,
            "selection_policy": "ALPHABETICAL", "years": 2}
EXECUTION_MODEL = {"decision": "AT_CLOSE", "fill": "NEXT_SESSION_OPEN", "direction": "LONG_ONLY",
                   "sizing": "WHOLE_SHARES_CASH_ONLY_NO_LEVERAGE", "exit_commission_reserved": True,
                   "text": "Rules are checked at each session's close; entries and exits fill at the next session's "
                           "open. Cash only, long only, whole shares, no leverage or margin."}
ABSOLUTE_PRICE_FEATURES = {"stock.close", "stock.support", "stock.resistance", "stock.atr_14"}
VOLUME_FEATURES = {"stock.relative_volume", "stock.volume_level", "stock.volume_expansion", "stock.signal"}


def _w(code: str, text: str, **extra) -> dict:
    return {"code": code, "text": text, **extra}


# ---- 1. eligibility --------------------------------------------------------------------------------------------------

def eligibility(store: BacktestStore, strategy_id: str, version_number: int) -> Tuple[Optional[dict], Optional[dict], List[dict], List[dict]]:
    """(ref, spec, checks, errors). Errors use: NOT_FOUND, INTEGRITY_ERROR, SCHEMA_VERSION, REGISTRY_MISMATCH,
    FORWARD_TEST_ONLY, UNSUPPORTED."""
    checks, errors = [], []

    def check(code, ok, label, detail=None, err=None):
        checks.append({"code": code, "ok": bool(ok), "label": label, **({"detail": detail} if detail else {})})
        if not ok and err:
            errors.append({"code": err[0], "message": err[1]})
        return ok

    d, v, triggers = store.strategy_version_row(strategy_id, version_number)
    if not check("STRATEGY_FOUND", d is not None, "Strategy exists", err=("NOT_FOUND", "Strategy not found.")):
        return None, None, checks, errors
    if not check("VERSION_FOUND", v is not None, f"Version v{version_number} exists",
                 err=("NOT_FOUND", f"v{version_number} not found.")):
        return None, None, checks, errors
    need = {"strategy_versions_immutable_delete", "strategy_versions_immutable_update"}
    check("VERSION_IMMUTABLE", need <= set(triggers), "Version is immutable (database triggers present)",
          err=("INTEGRITY_ERROR", "The strategy_versions immutability triggers are missing."))
    try:
        spec = json.loads(v["spec_json"])
    except ValueError:
        spec = None
    ok_hash = spec is not None and S.canonical_json(spec) == v["spec_json"] and S.spec_hash(spec) == v["spec_hash"]
    if not check("SPEC_HASH_VERIFIED", ok_hash, "Stored spec re-hashes to its spec hash",
                 err=("INTEGRITY_ERROR", "The stored spec does not match its hash, so it is not trusted.")):
        return None, None, checks, errors
    check("RULES_HASH_VERIFIED", S.rules_hash(spec) == v["rules_hash"], "Rules hash re-computes",
          err=("INTEGRITY_ERROR", "The stored rules hash does not match the spec."))
    check("SCHEMA_VERSION", spec.get("schema_version") == S.SCHEMA_VERSION == v["schema_version"],
          f"Schema v{S.SCHEMA_VERSION}", err=("SCHEMA_VERSION", "Unsupported strategy schema version."))
    reg_ok = (spec.get("feature_registry_version") == v["feature_registry_version"] == F.REGISTRY_VERSION)
    check("REGISTRY_VERSION", reg_ok, f"Feature registry v{F.REGISTRY_VERSION}",
          err=("REGISTRY_MISMATCH", "The version was saved against a different feature registry version."))
    fp_ok = spec.get("feature_registry_fingerprint") == v["feature_registry_fingerprint"] == F.fingerprint()
    check("REGISTRY_FINGERPRINT", fp_ok, "Feature registry fingerprint matches",
          detail=f"saved {str(v['feature_registry_fingerprint'])[:12]}… · running {F.fingerprint()[:12]}…",
          err=("REGISTRY_MISMATCH", "Feature definitions or thresholds changed since this version was saved, so it "
               "cannot be evaluated unchanged."))
    if reg_ok and fp_ok and ok_hash:
        norm, verr = S.validate(spec)
        check("SPEC_STILL_VALID", not verr and S.canonical_json(norm) == v["spec_json"], "Spec validates unchanged",
              err=("INTEGRITY_ERROR", "The stored spec no longer validates unchanged."))
    rd = S.readiness(spec)["status"] if not errors else None
    if not errors:
        ready = v["readiness"] == S.READY and rd == S.READY
        code = S.READY if ready else (rd if rd != S.READY else v["readiness"])
        check("READINESS", ready, "BACKTEST READY",
              detail=None if ready else S.READINESS_LABEL.get(code, code),
              err=(code, "Only BACKTEST READY versions can be backtested. "
                   + ("This version uses data that cannot be rebuilt for past dates (FORWARD TEST ONLY)."
                      if code == S.FORWARD_ONLY else "This version uses live-account data no strategy may use yet.")))
    ref = {"strategy_id": d["strategy_id"], "name": spec.get("name"), "strategy_version_id": v["version_id"],
           "version_number": v["version_number"], "spec_hash": v["spec_hash"], "rules_hash": v["rules_hash"],
           "feature_registry_version": v["feature_registry_version"],
           "feature_registry_fingerprint": v["feature_registry_fingerprint"], "readiness": v["readiness"]}
    return ref, spec, checks, errors


# ---- 2. request ------------------------------------------------------------------------------------------------------

def parse_config(body: dict, now=None) -> Tuple[Optional[RunConfig], List[dict]]:
    errs = []
    try:
        start, end = date.fromisoformat(body["start_date"]), date.fromisoformat(body["end_date"])
    except (KeyError, TypeError, ValueError):
        return None, [{"code": "INVALID_DATE_RANGE", "message": "Dates must be YYYY-MM-DD."}]
    last = B.last_complete_session_date(now)
    if start >= end:
        errs.append({"code": "INVALID_DATE_RANGE", "message": "The start date must be before the end date."})
    if end > last:
        errs.append({"code": "INVALID_DATE_RANGE", "message": f"The end date must be on or before {last.isoformat()} "
                     "(the last session that has fully finished)."})
    if start < date.fromisoformat(LIMITS["earliest_start"]):
        errs.append({"code": "INVALID_DATE_RANGE", "message": f"The start date must be on or after {LIMITS['earliest_start']}."})
    if (end - start).days > 366 * LIMITS["max_years"]:
        errs.append({"code": "INVALID_DATE_RANGE", "message": f"At most {LIMITS['max_years']} years per run."})
    vals = {}
    for k in ("initial_equity", "slippage_bps_per_side", "commission_per_order"):
        lo, hi = LIMITS[k]
        v = body.get(k, DEFAULTS[k])
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= float(v) <= hi:
            errs.append({"code": "INVALID_CONFIG", "message": f"{k} must be a number from {lo:g} to {hi:g}."})
        else:
            vals[k] = float(v)
    pol = body.get("selection_policy", DEFAULTS["selection_policy"])
    if pol not in SELECTION_POLICIES:
        errs.append({"code": "INVALID_CONFIG", "message": f"selection_policy must be one of {', '.join(SELECTION_POLICIES)}."})
    if errs:
        return None, errs
    return RunConfig(start, end, vals["initial_equity"], vals["slippage_bps_per_side"], vals["commission_per_order"],
                     pol), []


def config_payload(ref: dict, cfg: RunConfig) -> dict:
    return {"engine_version": ENGINE_VERSION,
            "strategy": {k: ref[k] for k in ("strategy_id", "strategy_version_id", "version_number", "spec_hash",
                                             "rules_hash", "feature_registry_version", "feature_registry_fingerprint")},
            "period": {"start": cfg.start.isoformat(), "end": cfg.end.isoformat()},
            "capital": {"initial_equity": cfg.initial_equity},
            "costs": {"slippage_bps_per_side": cfg.slippage_bps_per_side, "commission_per_order": cfg.commission_per_order},
            "execution": {**{k: v for k, v in EXECUTION_MODEL.items() if k != "text"}, "selection_policy": cfg.selection_policy},
            "data": {"source": B.SOURCE, "feed": B.feed(), "adjustment": B.ADJUSTMENT, "timeframe": "1Day",
                     "window_calendar_days": LOOKBACK, "calendar": f"{SPY} sessions"},
            "benchmarks": ["SPY_BUY_AND_HOLD", "UNIVERSE_EQUAL_WEIGHT_BUY_AND_HOLD"]}


def config_hash(payload: dict) -> str:
    return sha256(canonical(payload))


# ---- 3. coverage + warnings ------------------------------------------------------------------------------------------

def _roles(needs: Needs) -> Dict[str, str]:
    r = {s: "BREADTH_LIST" for s in needs.basket}
    r.update({e: "SECTOR_ETF" for _, e in needs.sector_etfs})
    r.update({s: "MARKET_CONTEXT" for s in needs.index_symbols})
    r[SPY] = "MARKET_PROXY_AND_CALENDAR"
    r.update({s: "UNIVERSE" for s in needs.universe})
    return r


def need_start(cfg: RunConfig) -> date:
    return cfg.start - timedelta(days=LOOKBACK - 1)


def fetch_start(cfg: RunConfig) -> date:
    ns = need_start(cfg)
    return ns.replace(day=1)          # month start: nearby start dates can reuse the same cached download


def coverage(store: BacktestStore, needs: Needs, cfg: RunConfig, refresh: bool = False) -> dict:
    ns, fd = need_start(cfg).isoformat(), B.feed()
    roles = _roles(needs)
    rows, datasets, missing = [], {}, []
    sessions_by = {}
    found = {}
    for sym in needs.symbols():
        ds = None if refresh else store.covering_dataset(sym, fd, B.ADJUSTMENT, ns, cfg.end.isoformat())
        if ds is not None:
            found[sym] = ds
    dates_of = {}
    if found:
        with store._connect(readonly=True) as conn:
            for sym, ds in found.items():
                dates_of[sym] = [date.fromisoformat(r[0]) for r in conn.execute(
                    "SELECT session_date FROM historical_daily_bars WHERE dataset_id = ? ORDER BY session_date",
                    (ds["dataset_id"],))]
    for sym in needs.symbols():
        ds = found.get(sym)
        row = {"symbol": sym, "role": roles[sym]}
        if ds is None:
            rows.append({**row, "status": "NOT_CACHED"})
            missing.append(sym)
            continue
        datasets[sym] = ds
        dates = dates_of[sym]
        in_range = [d for d in dates if cfg.start <= d <= cfg.end]
        sessions_by[sym] = (dates, in_range)
        rows.append({**row, "status": "CACHED" if in_range else "NO_DATA_IN_RANGE", "dataset_id": ds["dataset_id"],
                     "content_hash": ds["content_hash"], "fetched_at": ds["fetched_at"],
                     "first_session": in_range[0].isoformat() if in_range else None,
                     "last_session": in_range[-1].isoformat() if in_range else None, "sessions": len(in_range),
                     "warmup_sessions": sum(1 for d in dates if need_start(cfg) <= d < cfg.start)})
    calendar = sessions_by.get(SPY, ([], []))[1]
    cal_set = set(calendar)
    # the market proxy IS the session calendar: 2+ consecutive weekdays without a bar is a data hole, not a holiday
    # (U.S. exchanges have had no multi-day closures since the data used here begins), so "next session" would be wrong
    holes = []
    for a, b in zip(calendar, calendar[1:]):
        wd = sum(1 for i in range(1, (b - a).days) if (a + timedelta(days=i)).weekday() < 5)
        if wd >= 2:
            holes.append({"after": a.isoformat(), "before": b.isoformat(), "weekdays_without_bar": wd})
    req_warm = max([MIN_SESSIONS[f] for f in (*needs.stock_features, *needs.market_features, *needs.sector_features)] or [2]) - 1
    for row in rows:
        if row["symbol"] in sessions_by and calendar:
            dates, in_range = sessions_by[row["symbol"]]
            have = set(in_range)
            span = [d for d in calendar if in_range and in_range[0] <= d <= in_range[-1]]
            gaps = [d for d in span if d not in have]
            row["missing_sessions"] = len(gaps)
            row["missing_examples"] = [d.isoformat() for d in gaps[:5]]
            row["extra_sessions"] = sum(1 for d in in_range if d not in cal_set)
            row["starts_late"] = bool(in_range) and in_range[0] > calendar[0]
            row["ends_early"] = bool(in_range) and in_range[-1] < calendar[-1]
            row["warmup_ok"] = bool(in_range) and not row["starts_late"] and row["warmup_sessions"] >= req_warm
    return {"rows": rows, "datasets": datasets, "missing": missing, "calendar": calendar, "calendar_gaps": holes,
            "required_warmup_sessions": req_warm, "need_start": ns}


def run_warnings(spec: dict, needs: Needs, cfg: RunConfig, cov: Optional[dict]) -> List[dict]:
    used = {fid for fid, _ in S.features_used(spec)}
    in_conditions = {c["feature"] for side in ("entry", "exit") for c in S._conditions_of(spec[side])}
    out = []
    if cfg.slippage_bps_per_side == 0 and cfg.commission_per_order == 0:
        out.append(_w("ZERO_COST_ASSUMPTIONS", "Slippage and commission are both zero — an optimistic, cost-free "
                      "assumption. Real fills include spreads and slippage."))
    elif cfg.slippage_bps_per_side == 0:
        out.append(_w("ZERO_SLIPPAGE_ASSUMPTION", "Slippage is zero — optimistic. Real fills at the open usually "
                      "differ from the printed open price."))
    if used & BASKET_FEATURES:
        out.append(_w("SURVIVORSHIP_BIAS_WARNING", "Breadth-based market features use today's fixed list of 80 large "
                      "U.S. stocks for every past date. Companies that left that group (or were not in it then) are not "
                      "represented, so past breadth is measured on a list chosen with hindsight."))
    if needs.sector_features:
        out.append(_w("STATIC_SECTOR_MAP_WARNING", "Sector features use the app's current, hand-curated sector map for "
                      "every past date; a stock's sector grouping then may have differed."))
    out.append(_w("UNIVERSE_HINDSIGHT_WARNING", "The strategy's symbols were chosen today. Stocks that were delisted, "
                  "merged or not on your radar then are not in this test (survivorship / selection bias)."))
    out.append(_w("ADJUSTED_PRICES_NOTE", "Bars are split- and dividend-adjusted (Alpaca adjustment=all, as the app "
                  "uses): past prices are restated in today's terms and dividends are reflected in the adjusted prices "
                  "rather than paid as cash. Share counts and dollar levels differ from what actually traded then."))
    if in_conditions & ABSOLUTE_PRICE_FEATURES:
        out.append(_w("ADJUSTED_PRICE_LEVEL_WARNING", "This strategy's conditions compare absolute price levels (close, "
                      "support, resistance or ATR in $) with fixed numbers. Adjusted history restates old prices using "
                      "later splits and dividends, so a fixed dollar threshold reflects information from after each "
                      "decision date."))
    if B.feed() == "iex":
        out.append(_w("IEX_FEED_NOTE", "Data comes from the IEX feed (the app's configured feed). IEX prints and volume "
                      "are a subset of all U.S. trading, so prices can differ slightly from consolidated data and "
                      "volume is IEX volume only" + (" — this affects the volume-based rules in this strategy."
                                                     if used & VOLUME_FEATURES else ".")))
    out.append(_w("DAILY_BAR_RESOLUTION", "Daily bars only: exits are decided at a close and filled at the next open. "
                  "There are no intraday stops, and overnight gaps can make fills far from the decision close."))
    out.append(_w("NO_HISTORICAL_EVENTS_OR_RESEARCH", "No news, research, earnings or macro-event data is used for past "
                  "dates (those cannot be rebuilt), so the test does not know about events that mattered then."))
    if cov:
        for r in cov["rows"]:
            if r["status"] == "NO_DATA_IN_RANGE":
                out.append(_w("MISSING_SYMBOL_DATA" if r["role"] == "UNIVERSE" else "CONTEXT_DATA_MISSING",
                              f"{r['symbol']} ({r['role'].replace('_', ' ').lower()}) has no bars in the test period, so "
                              + ("it is never evaluated." if r["role"] == "UNIVERSE" else "features built from it are "
                                 "unavailable."), symbol=r["symbol"]))
            elif r.get("starts_late"):
                out.append(_w("DATA_STARTS_LATE", f"{r['symbol']} data starts {r['first_session']}, after the test start; "
                              "earlier sessions cannot be evaluated.", symbol=r["symbol"]))
            elif r.get("warmup_ok") is False:
                out.append(_w("PARTIAL_WARMUP", f"{r['symbol']} has {r['warmup_sessions']} warm-up sessions before the "
                              f"start (needs {cov['required_warmup_sessions']}); early sessions may show "
                              "INSUFFICIENT_HISTORY for some features.", symbol=r["symbol"]))
            if r.get("missing_sessions"):
                out.append(_w("MISSING_SESSIONS", f"{r['symbol']} has no bar on {r['missing_sessions']} market "
                              f"session(s) inside its data range (e.g. {', '.join(r['missing_examples'])}). Those days "
                              "are not evaluated and do not count as holding days.", symbol=r["symbol"]))
    return out


# ---- 4. preflight ----------------------------------------------------------------------------------------------------

def preflight(store: BacktestStore, body: dict, refresh: bool = False, now=None) -> dict:
    ref, spec, checks, errors = eligibility(store, body.get("strategy_id", ""), body.get("version_number", 0))
    cfg, cerr = parse_config(body, now)
    checks.append({"code": "DATE_RANGE_AND_COSTS", "ok": not cerr, "label": "Date range and cost settings are valid"})
    errors += cerr
    out = {"status": "BLOCKED", "strategy": ref, "checks": checks, "errors": errors, "warnings": [], "coverage": None,
           "download": None, "config": None, "config_hash": None, "execution_model": EXECUTION_MODEL}
    if errors or cfg is None:
        return out
    needs = needs_for(spec)
    cov = coverage(store, needs, cfg, refresh=refresh)
    payload = config_payload(ref, cfg)
    out.update({"config": payload, "config_hash": config_hash(payload), "needs": needs.public(),
                "coverage": {k: cov[k] for k in ("rows", "required_warmup_sessions", "need_start", "calendar_gaps")} |
                            {"calendar_sessions": len(cov["calendar"]),
                             "calendar_first": cov["calendar"][0].isoformat() if cov["calendar"] else None,
                             "calendar_last": cov["calendar"][-1].isoformat() if cov["calendar"] else None},
                "warnings": run_warnings(spec, needs, cfg, cov)})
    if cov["missing"]:
        fs, fe = fetch_start(cfg), B.last_complete_session_date(now)
        out["download"] = {"symbols": cov["missing"], "count": len(cov["missing"]), "fetch_start": fs.isoformat(),
                           "fetch_end": fe.isoformat(), "estimated_bars": len(cov["missing"]) * B.estimate_sessions(fs, fe),
                           "request": "one batched Alpaca market-data request (daily bars; the client pages through "
                                      "results) — explicit, read-only, never a trading call",
                           "breadth_list": sum(1 for s in cov["missing"] if s in needs.basket)}
        out["status"] = "DATA_REQUIRED"
        checks.append({"code": "DATA_CACHED", "ok": False, "label": f"{len(cov['missing'])} symbol(s) need a download"})
        return out
    spy_row = next(r for r in cov["rows"] if r["symbol"] == SPY)
    if spy_row["status"] != "CACHED" or len(cov["calendar"]) < LIMITS["min_sessions"]:
        errors.append({"code": "DATA_UNAVAILABLE", "message": f"{SPY} (the market proxy and session calendar) has "
                       f"{len(cov['calendar'])} session(s) in this period — at least {LIMITS['min_sessions']} are needed."})
        checks.append({"code": "CALENDAR", "ok": False, "label": f"{SPY} session calendar"})
        return out
    if spy_row.get("starts_late"):
        errors.append({"code": "INSUFFICIENT_WARMUP", "message": f"{SPY} data starts {spy_row['first_session']}, after "
                       "the requested start; choose a later start date."})
    if cov["calendar_gaps"]:
        g = cov["calendar_gaps"][0]
        errors.append({"code": "DATA_UNAVAILABLE", "message": f"{SPY} (the session calendar) has no bars for "
                       f"{g['weekdays_without_bar']} weekdays between {g['after']} and {g['before']} — a data hole, not a "
                       f"market holiday. Choose a period after {cov['calendar_gaps'][-1]['before']}."})
    checks.append({"code": "CALENDAR", "ok": not cov["calendar_gaps"],
                   "label": f"{SPY} session calendar: {len(cov['calendar'])} sessions"
                            + (f", {len(cov['calendar_gaps'])} data hole(s)" if cov["calendar_gaps"] else ", no data holes")})
    for r in cov["rows"]:
        if r["role"] == "UNIVERSE":
            checks.append({"code": "COVERAGE", "ok": r["status"] == "CACHED", "label": f"{r['symbol']} coverage",
                           "detail": f"{r.get('first_session')} → {r.get('last_session')} · {r.get('sessions', 0)} sessions"})
    out["status"] = "BLOCKED" if errors else "READY"
    out["_datasets"] = cov["datasets"]
    return out


def download(store: BacktestStore, body: dict, refresh: bool = False, fetch_fn=None, client=None, now=None) -> dict:
    """Explicit, read-only market-data download for exactly the symbols the preflight lists. Returns the new preflight."""
    pf = preflight(store, body, refresh=refresh, now=now)
    if pf["status"] == "BLOCKED" and not pf.get("download"):
        raise BacktestError("PREFLIGHT_BLOCKED", "Fix the listed problems before downloading data.", pf["errors"], 422)
    if pf.get("download"):
        cfg, _ = parse_config(body, now)
        B.fetch_and_store(store, pf["download"]["symbols"], fetch_start(cfg), now=now, fetch_fn=fetch_fn, client=client)
    return preflight(store, body, now=now)


# ---- 5. run creation + job -------------------------------------------------------------------------------------------

_jobs: Dict[str, dict] = {}
_job_lock = threading.Lock()
RUN_INLINE = False          # tests run the job synchronously; the server uses one background thread per run


def progress(run_id: str) -> Optional[dict]:
    with _job_lock:
        p = _jobs.get(run_id)
        return dict(p) if p else None


def start_run(store: BacktestStore, body: dict, now=None) -> dict:
    pf = preflight(store, body, now=now)
    if pf["status"] != "READY":
        code = pf["errors"][0]["code"] if pf["errors"] else "DATA_REQUIRED"
        raise BacktestError(code, "The backtest cannot start: " + (pf["errors"][0]["message"] if pf["errors"] else
                            "market data must be downloaded first."), {k: pf[k] for k in ("checks", "errors", "download")},
                            422)
    with _job_lock:
        if any(j["state"] in ("PENDING", "RUNNING") for j in _jobs.values()):
            raise BacktestError("BACKTEST_BUSY", "Another backtest is running; wait for it to finish.", status=409)
        ds = pf.pop("_datasets")
        data = {"source": B.SOURCE, "feed": B.feed(), "adjustment": B.ADJUSTMENT, "timeframe": "1Day",
                "datasets": {s: {k: d[k] for k in ("dataset_id", "content_hash", "fetched_at", "first_session",
                                                   "last_session", "bar_count", "requested_start", "requested_end")}
                             for s, d in sorted(ds.items())}}
        data_hash = sha256(canonical({s: d["content_hash"] for s, d in data["datasets"].items()}))
        cfg_payload = pf["config"]
        run_id = store.create_run(pf["strategy"], canonical(cfg_payload), pf["config_hash"], canonical(data), data_hash,
                                  canonical({k: pf[k] for k in ("checks", "warnings", "coverage")}), ENGINE_VERSION,
                                  cfg_payload["period"]["start"], cfg_payload["period"]["end"])
        _jobs[run_id] = {"state": "PENDING", "phase": "QUEUED", "done": 0, "total": 0}
    if RUN_INLINE:
        _execute(store, run_id)
    else:
        threading.Thread(target=_execute, args=(store, run_id), name=f"backtest-{run_id[:8]}", daemon=True).start()
    return {"run_id": run_id, "status": "PENDING", "config_hash": pf["config_hash"]}


def _set(run_id: str, **kw) -> None:
    with _job_lock:
        if run_id in _jobs:
            _jobs[run_id].update(kw)


def _execute(store: BacktestStore, run_id: str) -> None:
    try:
        store.mark_running(run_id)
        _set(run_id, state="RUNNING", phase="VERIFYING")
        _run(store, run_id)
    except BacktestError as exc:
        store.fail_run(run_id, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - never leave a run RUNNING; no partial results are written
        tb = traceback.extract_tb(exc.__traceback__)[-1]
        store.fail_run(run_id, "INTERNAL_ERROR", f"{type(exc).__name__} at {tb.name}:{tb.lineno}: {str(exc)[:200]}")
    finally:
        with _job_lock:
            _jobs.pop(run_id, None)


def _run(store: BacktestStore, run_id: str) -> None:
    t0 = time.perf_counter()
    run = store.get_run(run_id)
    ref, spec, checks, errors = eligibility(store, run["strategy_id"], run["version_number"])
    if errors or ref["spec_hash"] != run["spec_hash"] or ref["strategy_version_id"] != run["strategy_version_id"]:
        code = errors[0]["code"] if errors else "STRATEGY_INTEGRITY_ERROR"
        raise BacktestError("REGISTRY_MISMATCH" if code == "REGISTRY_MISMATCH" else "STRATEGY_INTEGRITY_ERROR",
                            errors[0]["message"] if errors else "The strategy version changed after the run was created.")
    c = run["config"]
    cfg = RunConfig(date.fromisoformat(c["period"]["start"]), date.fromisoformat(c["period"]["end"]),
                    c["capital"]["initial_equity"], c["costs"]["slippage_bps_per_side"], c["costs"]["commission_per_order"],
                    c["execution"]["selection_policy"])
    if config_hash(c) != run["config_hash"]:
        raise BacktestError("STRATEGY_INTEGRITY_ERROR", "The stored run configuration does not match its hash.")
    needs = needs_for(spec)
    _set(run_id, phase="LOADING_DATA")
    series: Dict[str, BarSeries] = {}
    for sym, d in run["data"]["datasets"].items():
        series[sym] = BarSeries(sym, store.dataset_rows(d["dataset_id"], verify_hash=d["content_hash"]))
    if SPY not in series:
        raise BacktestError("DATA_UNAVAILABLE", f"{SPY} bars are missing.")
    calendar = [d for d in series[SPY].dates if cfg.start <= d <= cfg.end]
    if len(calendar) < LIMITS["min_sessions"]:
        raise BacktestError("DATA_UNAVAILABLE", "Not enough market sessions in the period.")
    t_load = time.perf_counter()

    _set(run_id, phase="COMPUTING_FEATURES", done=0, total=len(calendar))
    snaps, compute = compute_snapshots(str(store.db_path), {s: d["dataset_id"] for s, d in run["data"]["datasets"].items()},
                                       series, needs, calendar, progress=lambda i, n: _set(run_id, done=i, total=n))
    violations = sum(s["replay"]["violations"] for s in snaps.values())
    t_feat = time.perf_counter()

    _set(run_id, phase="SIMULATING")

    def snap(sym: str, T: date) -> Optional[dict]:
        day = snaps[T]
        s = day["symbols"].get(sym)
        return None if s is None else {**day["market"], **s["cells"]}

    prices = {s: series[s] for s in needs.universe if s in series}
    sim = simulate(spec, cfg, calendar, prices, snap)
    t_sim = time.perf_counter()

    spy_bh = M.buy_and_hold([SPY], series, calendar, cfg.initial_equity, cfg.slippage_bps_per_side, cfg.commission_per_order)
    uni_bh = M.buy_and_hold([s for s in needs.universe], series, calendar, cfg.initial_equity,
                            cfg.slippage_bps_per_side, cfg.commission_per_order)
    for i, e in enumerate(sim["equity"]):
        e["spy_benchmark_equity"], e["universe_benchmark_equity"] = spy_bh["series"][i], uni_bh["series"][i]
    metrics = M.core_metrics(cfg.initial_equity, sim["trades"], sim["equity"])
    counts = sim["counts"]
    audit = {"entry_evaluations": counts.get("entry_evaluations", 0), "signals_considered": counts.get("ENTRY_SIGNAL", 0),
             "signals_filled": counts.get("ENTRY_FILLED", 0),
             "skipped_max_positions": counts.get("ENTRY_SKIPPED:MAX_OPEN_POSITIONS", 0),
             "skipped_insufficient_cash": counts.get("ENTRY_SKIPPED:INSUFFICIENT_CASH", 0),
             "skipped": {k.split(":", 1)[1]: v for k, v in counts.items() if k.startswith("ENTRY_SKIPPED:")},
             "unfilled_entries": {k.split(":", 1)[1]: v for k, v in counts.items() if k.startswith("UNFILLED_ENTRY:")},
             "unfilled_exits": {k.split(":", 1)[1]: v for k, v in counts.items() if k.startswith("UNFILLED_EXIT:")},
             "exit_signals": counts.get("EXIT_SIGNAL", 0), "exits_filled": counts.get("EXIT_FILLED", 0),
             "evaluations_with_insufficient_history": counts.get("evaluations_with_insufficient_history", 0),
             "symbol_sessions_not_evaluated": counts.get("not_evaluated", 0)}
    warnings = run_warnings(spec, needs, cfg, None) + [w for w in run["preflight"]["warnings"]
                                                       if w["code"] in ("MISSING_SYMBOL_DATA", "CONTEXT_DATA_MISSING",
                                                                        "DATA_STARTS_LATE", "PARTIAL_WARMUP", "MISSING_SESSIONS")]
    if audit["evaluations_with_insufficient_history"]:
        warnings.append(_w("INSUFFICIENT_HISTORY_EVALUATIONS", f"{audit['evaluations_with_insufficient_history']} entry "
                           "evaluation(s) had at least one feature withheld for insufficient history; those conditions "
                           "counted as not met."))
    if metrics["open_positions_at_end"]:
        warnings.append(_w("OPEN_POSITIONS_AT_END", f"{metrics['open_positions_at_end']} position(s) were still open at "
                           "the end. They are marked to the last close in equity and excluded from closed-trade "
                           "statistics — they are not completed trades."))
    pit = {"strategy_hash_verified": True, "rules_hash_verified": True, "registry_fingerprint_verified": True,
           "readiness_backtest_ready": True, "historical_features_only": True,
           "data_content_hashes_verified": True, "bars_served_after_decision_day": violations,
           "latest_trade_override_used": False, "macro_events_and_news_used": False,
           "fills_after_signal_session_at_open": True, "window_calendar_days": LOOKBACK}
    pit_safe = violations == 0
    result = {
        "metrics": metrics, "sample": M.sample_size(metrics["closed_trades"]), "formulas": M.FORMULAS,
        "benchmarks": {"note": "BENCHMARK CONTEXT — buy-and-hold over the same period with the same data and cost "
                               "assumptions. Context only: a higher or lower return is not a verdict on the strategy.",
                       "spy": {k: v for k, v in spy_bh.items() if k != "series"} | {"symbol": SPY},
                       "universe_equal_weight": {k: v for k, v in uni_bh.items() if k != "series"} |
                                                {"symbols": list(needs.universe)}},
        "breakdowns": M.breakdowns(sim["trades"], bool(needs.basket)),
        "audit": audit, "warnings": warnings, "point_in_time_checks": pit,
        "execution_model": EXECUTION_MODEL, "selection_policy": {"code": cfg.selection_policy,
                                                                  "text": SELECTION_POLICIES[cfg.selection_policy]},
        "compute": compute | {"feature_sessions": len(calendar), "symbols_loaded": len(series)},
        "needs": needs.public(), "risk": spec["risk"],
        "timings": {"data_load_s": round(t_load - t0, 3), "features_s": round(t_feat - t_load, 3),
                    "simulation_s": round(t_sim - t_feat, 3), "metrics_s": round(time.perf_counter() - t_sim, 3)},
    }
    refs = {k: ref[k] for k in ("strategy_id", "strategy_version_id", "spec_hash", "rules_hash")}
    trades = []
    for t in sim["trades"]:
        row = {**refs, **t}
        for k in ("all_exit_reasons", "entry_feature_snapshot", "entry_evaluation_trace", "exit_feature_snapshot",
                  "exit_evaluation_trace"):
            row[k] = None if row[k] is None else canonical(row[k])
        trades.append(row)
    signals = [{**{k: e[k] for k in ("seq", "session_date", "symbol", "event_type", "reason_code", "trade_no")},
                "detail_json": canonical(e["detail"])} for e in sim["events"]]
    _set(run_id, phase="WRITING")
    store.complete_run(run_id, trades, signals, sim["equity"], result, pit_safe)


def public_config() -> dict:
    last = B.last_complete_session_date()
    while last.weekday() >= 5:                     # default to a weekday; the calendar decides the real sessions
        last -= timedelta(days=1)
    start = date(last.year - DEFAULTS["years"], last.month, min(last.day, 28))
    return {"defaults": {**DEFAULTS, "end_date": last.isoformat(), "start_date": start.isoformat()},
            "limits": LIMITS, "selection_policies": SELECTION_POLICIES, "execution_model": EXECUTION_MODEL,
            "cost_model": {"slippage": "entry fill = next open x (1 + bps/10000); exit fill = next open x (1 - bps/10000)",
                           "commission": "a fixed dollar amount per order (entry and exit are separate orders)",
                           "zero_cost_label": "Zero slippage and commission is an optimistic, cost-free assumption."},
            "data": {"source": B.SOURCE, "feed": B.feed(), "adjustment": B.ADJUSTMENT, "timeframe": "1Day",
                     "window_calendar_days": LOOKBACK, "calendar": f"{SPY} sessions",
                     "last_complete_session": last.isoformat()},
            "warmup_min_sessions": MIN_SESSIONS, "formulas": M.FORMULAS,
            "sample_bands": [{"from": lo, "to": hi, "code": code} for lo, hi, code, _ in M.SAMPLE_BANDS],
            "engine_version": ENGINE_VERSION, "workers": default_workers(),
            "note": "Historical behaviour under explicit assumptions — not a prediction, recommendation or ranking."}
