"""
forward/capture.py — what one forward capture READS. No new maths: every value comes from code that already exists.

  bars       the Stage 3.2 immutable bar cache (historical_bar_datasets / historical_daily_bars). A capture reuses a
             cached dataset whose requested range already covers what it needs, otherwise ONE batched read-only
             download through backtest.bars.fetch_and_store -> data.market_data.fetch_daily_bars (Alpaca MARKET DATA,
             same feed / adjustment as the app). Only complete sessions are ever cached (dated before today in New York).
  technical  backtest.snapshots.day_snapshot on those bars — the SAME point-in-time replay Stage 3.2 uses (bars through
             the close of T only, live-engine calculations, Stage 3.1 extractors, the same warm-up rules). Stage 3.2's
             dependency rules decide which context symbols load (the 80-stock breadth list only when a rule needs it).
  research   the latest SAVED research snapshot (Stage 2.5 table, read only) through insights.research.from_snapshot
             and strategy.features.research_values. Nothing is generated: no Analyze run, no Claude call. No snapshot
             -> the research features are UNAVAILABLE (RESEARCH_UNAVAILABLE).
  events     services.event_context.build_event_context (the Stage 2.6 providers) and strategy.features.event_values /
             market_values. The providers turn failures into empty lists, so the capture also reads their success
             cache to know whether each calendar really loaded; missing event data is never read as low risk.

Timing: technical values are fixed at the close of T. Research and event values are read when the capture runs, which
is always after the close (only completed sessions are recorded) — they are labelled POST_CLOSE unless provably
fixed at the close (a research snapshot saved before the close; research freshness is always measured at capture).
"""
from __future__ import annotations

import copy
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import config
from backtest import bars as B
from backtest.replay import BarSeries, session_close
from backtest.snapshots import SPY, Needs, day_snapshot, needs_for
from backtest.store import BacktestStore
from strategy import features as F
from strategy import spec as S

LOOKBACK = config.DAILY_BAR_LOOKBACK_DAYS
FETCH_MARGIN_DAYS = 10            # the latest session can be a few days before "yesterday" (weekends, holidays)
AT_CLOSE, POST_CLOSE = "AT_CLOSE", "POST_CLOSE"


def utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def close_utc(T: date) -> datetime:
    """The decision time: 16:00 New York on session T, in UTC (early closes are still 'the close of T')."""
    return session_close(T).astimezone(timezone.utc)


def next_open_estimate(T: date) -> datetime:
    """09:30 New York on the first weekday after T (holidays unknown in advance, so this errs early)."""
    d = T + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime.combine(d, time(9, 30), tzinfo=B.NY).astimezone(timezone.utc)


# ---- what the strategy needs ------------------------------------------------------------------------------------------

def _price_group(g: dict) -> dict:
    out = []
    for c in g.get("conditions", []):
        if "logic" in c:
            out.append(_price_group(c))
        elif F.get(c["feature"]) is not None and F.get(c["feature"]).historical_support:
            out.append(c)
    return {**g, "conditions": out}


def price_spec(spec: dict) -> dict:
    """A copy of the spec keeping only daily-bar conditions — used ONLY to derive which bars to load (with Stage 3.2's
    own dependency rules). It is never evaluated: decisions always use the full, stored spec."""
    s = copy.deepcopy(spec)
    s["entry"] = _price_group(s["entry"])
    s["exit"] = {**_price_group(s["exit"]), **{k: s["exit"].get(k) for k in ("invalidation", "target", "max_holding_days")}}
    return s


def price_needs(spec: dict) -> Needs:
    return needs_for(price_spec(spec))


def forward_only_features(spec: dict) -> Dict[str, List[str]]:
    used = [fid for fid, _ in S.features_used(spec)]
    fo = [f for f in used if not F.get(f).historical_support and F.get(f).forward_support]
    return {"research": [f for f in fo if F.get(f).scope == "RESEARCH"],
            "event_stock": [f for f in fo if F.get(f).scope == "EVENT"],
            "event_market": [f for f in fo if F.get(f).scope == "MARKET"]}


# ---- bars (Stage 3.2 cache) --------------------------------------------------------------------------------------------

def need_range(last_complete: date, earliest_needed: Optional[date]) -> Tuple[date, date]:
    """Bars from (a full indicator window before the latest possible session, or earlier if an open shadow state or
    the last capture needs it) through the last complete session."""
    start = last_complete - timedelta(days=LOOKBACK - 1 + FETCH_MARGIN_DAYS)
    if earliest_needed is not None:
        start = min(start, earliest_needed)
    return start, last_complete


def find_datasets(store: BacktestStore, symbols, start: date, end: date) -> Tuple[Dict[str, dict], List[str]]:
    found, missing = {}, []
    for sym in sorted(set(symbols)):
        ds = store.covering_dataset(sym, B.feed(), B.ADJUSTMENT, start.isoformat(), end.isoformat())
        if ds is None:
            missing.append(sym)
        else:
            found[sym] = ds
    return found, missing


def download(store: BacktestStore, symbols: List[str], start: date, now: datetime, fetch_fn=None, client=None) -> Dict[str, dict]:
    """ONE batched, read-only market-data request (Stage 3.2 path). New immutable datasets; nothing is overwritten."""
    return B.fetch_and_store(store, symbols, start, now=now, fetch_fn=fetch_fn, client=client)


def load_series(store: BacktestStore, datasets: Dict[str, dict]) -> Dict[str, BarSeries]:
    return {s: BarSeries(s, store.dataset_rows(d["dataset_id"], verify_hash=d["content_hash"])) for s, d in datasets.items()}


def probe_sessions(last_complete: date, now: datetime, fetch_fn=None, client=None, lookback_days: int = 21) -> List[date]:
    """Preflight only: SPY session dates from one small read-only request that is NOT stored."""
    from data.market_data import fetch_daily_bars, get_data_client
    fetch_fn = fetch_fn or fetch_daily_bars
    client = client or get_data_client()
    frames = fetch_fn(client, [SPY], lookback_days=lookback_days)
    df = frames.get(SPY) if frames else None
    if df is None:
        return []
    return sorted({d for d in (B.session_date_of(t) for t in df["timestamp"]) if d <= last_complete})


def calendar_holes(dates: List[date]) -> List[dict]:
    """Stage 3.2's rule: 2+ consecutive weekdays without a market-proxy bar is a data hole, not a holiday."""
    out = []
    for a, b in zip(dates, dates[1:]):
        wd = sum(1 for i in range(1, (b - a).days) if (a + timedelta(days=i)).weekday() < 5)
        if wd >= 2:
            out.append({"after": a.isoformat(), "before": b.isoformat(), "weekdays_without_bar": wd})
    return out


def technical_snapshot(series: Dict[str, BarSeries], needs: Needs, T: date) -> dict:
    return day_snapshot(series, needs, T)


# ---- research (saved snapshots only) ----------------------------------------------------------------------------------

def _as_utc(dt) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def research_context(symbols: List[str], fids: List[str], captured_at: datetime, T: date,
                     db_getter: Optional[Callable] = None) -> Dict[str, Tuple[dict, dict]]:
    """{symbol: (cells, provenance)} from the latest SAVED research snapshot at capture time. Never generates research."""
    from insights.research import from_snapshot
    if db_getter is None:
        from database.database import get_db as db_getter
    close = close_utc(T)
    nxt = next_open_estimate(T)
    out = {}
    try:
        db = db_getter()
    except Exception:  # noqa: BLE001 - research is optional context; a failure makes it UNAVAILABLE, never guessed
        db = None
    for sym in symbols:
        snap, error = None, None
        if db is not None:
            try:
                rows = db.list_snapshots(symbol=sym, limit=1)
                snap = rows[0] if rows else None
            except Exception as exc:  # noqa: BLE001
                error = type(exc).__name__
        facts = from_snapshot(sym, snap, captured_at) if snap is not None else None
        vals = F.research_values(facts)
        created = _as_utc(getattr(snap, "created_at", None))
        strict = created is not None and created <= close
        cells = {}
        for fid in fids:
            v = vals.get(fid)
            if fid == "research.freshness":
                cells[fid] = {"v": v, "a": "AVAILABLE", "ts": utc_iso(captured_at), "timing": POST_CLOSE,
                              "note": "measured at capture time"}
            elif v is None:
                cells[fid] = {"v": None, "a": "UNAVAILABLE", "reason": "RESEARCH_UNAVAILABLE"}
            else:
                cells[fid] = {"v": v, "a": "AVAILABLE", "ts": utc_iso(created), "timing": AT_CLOSE if strict else POST_CLOSE}
        prov = {"source": "SAVED_SNAPSHOT" if snap is not None else "UNAVAILABLE",
                "reason": None if snap is not None else ("RESEARCH_LOOKUP_FAILED" if error else "RESEARCH_UNAVAILABLE"),
                "message": None if snap is not None else "Research required — no current saved research snapshot was "
                           "available. Nothing was generated; run Analyze and save a snapshot to use research later.",
                "error": error}
        if snap is not None:
            cat = (facts.category_scores or {}).get("catalyst")
            prov.update({"snapshot_id": getattr(snap, "id", None), "fingerprint": getattr(snap, "fingerprint", None),
                         "created_at": utc_iso(created), "market_timestamp": getattr(snap, "market_timestamp", None),
                         "price_timestamp": utc_iso(_as_utc(snap.price_timestamp)) if getattr(snap, "price_timestamp", None) else None,
                         "research_schema_version": getattr(snap, "research_schema_version", None),
                         "age_hours_at_capture": facts.age_hours, "stale": facts.stale, "data_quality": facts.data_quality,
                         "research_view": facts.research_view, "bullish_pct": facts.bullish_pct, "catalyst_score": cat,
                         "existed_at_close": strict, "created_after_next_open_estimate": created > nxt,
                         "values": {k: vals.get(k) for k in fids}})
        out[sym] = (cells, prov)
    return out


# ---- events (Stage 2.6 providers) -------------------------------------------------------------------------------------

def provider_coverage(symbol: str) -> dict:
    """Did each Stage 2.6 calendar really load? The providers cache ONLY successful fetches (data/events/cache.py), so a
    present cache entry for the window build_event_context just used means success. Read-only."""
    from data.events.cache import event_cache
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=config.EVENT_LOOKBACK_DAYS)).date().isoformat()
    end = (now + timedelta(days=config.EVENT_LOOKAHEAD_DAYS)).date().isoformat()
    return {"fred": {name: event_cache.get(f"fred:{rid}:{start}:{end}") is not None for name, rid in config.FRED_RELEASE_IDS.items()},
            "fomc": event_cache.get("fomc:calendar") is not None,
            "corporate": event_cache.get(f"corporate:{symbol.strip().upper()}:{start}:{end}") is not None}


def _macro_complete(cov: dict) -> bool:
    return bool(cov.get("fomc")) and bool(cov.get("fred")) and all(cov["fred"].values())


def event_context(symbols: List[str], stock_fids: List[str], market_fids: List[str], captured_at: datetime,
                  build_fn: Optional[Callable] = None, coverage_fn: Optional[Callable] = None
                  ) -> Tuple[Dict[str, Tuple[dict, dict]], Optional[Tuple[dict, dict]]]:
    """Per-symbol event.risk_level and the market-level macro-event flag, with provider status. Values are read NOW
    (after the close) — always POST_CLOSE context. Incomplete data never produces a low / 'no event' value."""
    from insights.events import event_item, upcoming_events
    if build_fn is None:
        from services.event_context import build_event_context as build_fn
    coverage_fn = coverage_fn or provider_coverage
    ts = utc_iso(captured_at)
    per = {}
    for sym in (symbols if stock_fids else []):
        try:
            b = build_fn(sym)
            cov = coverage_fn(sym)
        except Exception as exc:  # noqa: BLE001 - a provider failure makes the value UNAVAILABLE, never LOW
            per[sym] = ({fid: {"v": None, "a": "UNAVAILABLE", "reason": "EVENT_DATA_UNAVAILABLE"} for fid in stock_fids},
                        {"status": "UNAVAILABLE", "error": type(exc).__name__, "captured_at": ts})
            continue
        level = F.event_values(b).get("event.risk_level")
        earnings = bool(getattr(b, "earnings_available", False))
        complete = _macro_complete(cov) and bool(cov.get("corporate")) and earnings
        if level is None:
            cell = {"v": None, "a": "UNAVAILABLE", "reason": "EVENT_DATA_UNAVAILABLE"}
        elif complete or level == "HIGH":          # missing calendars can only ADD events, so HIGH is already certain
            cell = {"v": level, "a": "AVAILABLE", "ts": ts, "timing": POST_CLOSE}
        else:
            cell = {"v": None, "a": "UNAVAILABLE", "reason": "EVENT_DATA_INCOMPLETE"}
        nearest = getattr(b, "nearest_event", None)
        per[sym] = ({fid: dict(cell) for fid in stock_fids}, {
            "status": "COMPLETE" if complete else "PARTIAL", "source": "services.event_context.build_event_context",
            "providers": {**cov, "earnings": {"available": earnings, "reason": getattr(b, "earnings_reason", None)}},
            "data_quality": getattr(b, "data_quality", None), "computed_level": level,
            "nearest_event": event_item(nearest, captured_at) if nearest is not None else None,
            "captured_at": ts, "reference_time": ts,
            "note": None if complete else "Some event calendars were not available (earnings has no verified provider), "
                    "so an event risk below HIGH cannot be confirmed and is withheld."})
    market = None
    if market_fids:
        try:
            b = build_fn(SPY)
            cov = coverage_fn(SPY)
            items = upcoming_events(b, captured_at, macro_only=True)
            val = F.market_values({"available": True, "events": items}).get("market.major_event_within_24h")
            ok = _macro_complete(cov)
            cell = ({"v": val, "a": "AVAILABLE", "ts": ts, "timing": POST_CLOSE} if (val is True or (val is False and ok))
                    else {"v": None, "a": "UNAVAILABLE", "reason": "EVENT_DATA_INCOMPLETE"})
            market = ({fid: dict(cell) for fid in market_fids}, {
                "status": "COMPLETE" if ok else "PARTIAL", "source": "services.event_context.build_event_context(SPY) → "
                "insights.events.upcoming_events (macro only)", "providers": {"fred": cov.get("fred"), "fomc": cov.get("fomc")},
                "events": [{k: e.get(k) for k in ("title", "event_type", "date", "hours_until", "source")} for e in items[:6]],
                "captured_at": ts, "reference_time": ts})
        except Exception as exc:  # noqa: BLE001
            market = ({fid: {"v": None, "a": "UNAVAILABLE", "reason": "EVENT_DATA_UNAVAILABLE"} for fid in market_fids},
                      {"status": "UNAVAILABLE", "error": type(exc).__name__, "captured_at": ts})
    return per, market
