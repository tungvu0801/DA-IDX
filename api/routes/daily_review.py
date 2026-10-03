"""
api/routes/daily_review.py — TODAY'S PORTFOLIO + WATCHLIST REVIEW (Stage 2.7G). Read-only decision support.

POST /api/insights/daily-review                 the report (deterministic; never calls Claude)
POST /api/insights/daily-review/research-plan   which missing-research holdings fit the AI limits right now
POST /api/insights/daily-review/explain         OPTIONAL Claude explanation (explicit button only)

Every shared input (market scan, metrics batch, events, patterns, outcomes) is fetched once per request and
reused for all stocks. Robinhood access goes through the unchanged read-only gateway provider.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

import config
from insights import service
from portfolio.models import to_jsonable

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/insights/daily-review", tags=["daily-review"])

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_ID = re.compile(r"^[0-9a-f]{8,64}$")
_SUBJECT = re.compile(r"^(_market|[A-Z][A-Z0-9.\-]{0,9})$")


class DailyReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_ids: Optional[Dict[str, str]] = None
    previous: Optional[Dict[str, Dict[str, Optional[str]]]] = None   # last check, kept in the browser only

    @field_validator("analysis_ids")
    @classmethod
    def _ids(cls, v):
        if v is not None and (len(v) > 60 or any(not _SYMBOL.match(k) or not _ID.match(x) for k, x in v.items())):
            raise ValueError("invalid analysis_ids")
        return v

    @field_validator("previous")
    @classmethod
    def _prev(cls, v):
        if v is None:
            return v
        if len(v) > 80 or any(not _SUBJECT.match(k) or len(f) > 10 or any(len(str(x or "")) > 64 for x in f.values())
                              for k, f in v.items()):
            raise ValueError("invalid previous check")
        return v


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: List[str] = Field(max_length=40)

    @field_validator("symbols")
    @classmethod
    def _syms(cls, v):
        if any(not _SYMBOL.match(s) for s in v):
            raise ValueError("invalid symbol")
        return v


_cache: dict = {}
OUTCOME_CACHE_SECONDS = 600     # Stage 2.5 outcomes change at most once a day


def _outcomes(symbols):
    from database.database import get_db
    from insights.daily_review import outcome_stats
    key = tuple(sorted(symbols))
    hit = _cache.get(key)
    if hit is not None and time.time() - hit[0] <= OUTCOME_CACHE_SECONDS:
        return hit[1]
    result = outcome_stats(symbols, get_db)
    _cache[key] = (time.time(), result)
    return result


# Stage 2.6 event calendars are cached for hours, but a COLD macro (FRED) fetch can take ~30 s. The event fetch runs
# in the background from the start of the request; if it is not ready within the budget, the report is built with
# event data marked "still loading" (unknown — never treated as low risk) and the fetch keeps warming the caches.
EVENTS_BUDGET_SECONDS = 15.0
PORTFOLIO_EVENTS_BUDGET_SECONDS = 10.0   # the portfolio load includes held-stock events (2.7C load_view)
_events_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="daily-review-events")
_events_inflight: dict = {}


def _events_future(symbols, fetch):
    key = tuple(symbols)
    fut = _events_inflight.get(key)
    if fut is None or fut.done():
        fut = _events_pool.submit(fetch, list(symbols))
        _events_inflight[key] = fut
    return fut


def _load_portfolio_budgeted():
    """The normal read-only portfolio load (with Stage 2.6 events). If slow event calendars make it exceed the budget,
    load the same positions WITHOUT events instead; the 2.7D policy then reports event data as missing (its existing
    'Missing event data' rule) rather than guessing. The slow load keeps running and warms the event caches."""
    from api.routes import insights as ins
    from api.routes import portfolio as pr
    from insights.home import connection_status
    from portfolio.provider import PortfolioUnavailable
    fut = _events_pool.submit(ins._load_portfolio)
    try:
        return (*fut.result(timeout=PORTFOLIO_EVENTS_BUDGET_SECONDS), False)
    except FuturesTimeout:
        provider = pr.provider_factory()
        try:
            return provider, pr.load_view(provider, with_events=False), connection_status(None, None), True
        except PortfolioUnavailable as exc:
            return provider, None, connection_status(exc.reason, exc.message), True


def _watchlist_symbols():
    try:
        from api.routes.watchlist import get_cached_watchlist
        return list(get_cached_watchlist()[0])
    except Exception:  # noqa: BLE001 - watchlist is optional
        return []


def build(req: DailyReviewRequest) -> dict:
    from agents.ai_cache import ai_cache
    from agents.usage_tracker import usage_tracker
    from api.routes import portfolio as pr
    from api.routes import trade_review as tr
    from database.database import get_db
    from insights import home
    from insights.daily_review import ai_usage_counts, assemble, research_call_estimates
    from insights.research import lookup_research
    from services.analysis_cache import analysis_cache

    now = datetime.now(timezone.utc)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:            # market scan, watchlist and Robinhood load side by side
        warm = pool.submit(service.get_market_inputs)
        wl = pool.submit(_watchlist_symbols)
        provider, view, conn, portfolio_events_slow = _load_portfolio_budgeted()
        watchlist_symbols = wl.result()
        held = [p.symbol for p in view.positions] if view is not None else []
        symbols = sorted(set(held) | set(watchlist_symbols))
        events_future = _events_future(symbols, pr._events_for)    # provider-level TTL caches, one shared fetch
        inputs = warm.result()
    market = service.market_insights(view)
    market_block = home.market_block(market, now)
    metrics = service.symbols_metrics_fn(symbols)                # ONE batch for every stock
    try:
        events = events_future.result(timeout=max(0.5, EVENTS_BUDGET_SECONDS - (time.monotonic() - started)))
        events_loading = False
    except FuturesTimeout:
        events, events_loading = {}, True
    events_loading = events_loading or portfolio_events_slow
    ids = req.analysis_ids or {}
    research = {s: lookup_research(s, now, analysis_id=ids.get(s), cache_get=analysis_cache.get, db_getter=get_db)
                for s in symbols}
    concerns, sector_flags, patterns = {}, {}, None
    if view is not None:
        policy = pr._policy_block(view)
        concerns = pr._research_and_concerns(view, policy)[1]
        sector_flags = {f["subject"]: f for f in (policy.get("flags", []) if policy.get("enabled") else [])
                        if f["family"] == "sector_concentration"}
        patterns = service.cached_patterns(tr._patterns)
    sector_pct = {x["sector"]: x["pct_change"] for x in market.get("sectors", [])} if market.get("available") else {}
    spy = inputs.indices.get(config.MARKET_PROXY_SYMBOL) if inputs else None
    need = [s for s in held if not research[s].available or research[s].stale]
    return assemble(
        now=now, market=market, market_block=market_block, connection=conn, view=view,
        watchlist_symbols=watchlist_symbols, metrics=metrics, events=events, research=research,
        sector_of=pr.sector_resolver.sector_for, sector_pct=sector_pct, concerns=concerns, sector_flags=sector_flags,
        patterns=patterns, outcomes=_outcomes(symbols), market_ref_time=getattr(spy, "as_of", None),
        previous=req.previous, research_estimates=research_call_estimates(need, metrics, ai_cache),
        usage_counts=ai_usage_counts(usage_tracker), events_loading=events_loading)


@router.post("")
def post_daily_review(req: DailyReviewRequest) -> JSONResponse:
    return JSONResponse(to_jsonable(build(req)))


@router.post("/research-plan")
def post_research_plan(req: PlanRequest) -> JSONResponse:
    """Re-checked at click time: which of these stocks fit the hourly/daily AI limits right now. Runs nothing."""
    from agents.ai_cache import ai_cache
    from agents.usage_tracker import usage_tracker
    from insights.daily_review import ai_usage_counts, research_call_estimates
    from insights.decision import research_plan
    metrics = service.symbols_metrics_fn(req.symbols) if req.symbols else {}
    counts = ai_usage_counts(usage_tracker)
    return JSONResponse(to_jsonable(research_plan(req.symbols, research_call_estimates(req.symbols, metrics, ai_cache),
                                                  counts["last_hour"], counts["today"])))


@router.post("/explain")
def post_explain(req: DailyReviewRequest) -> JSONResponse:
    from insights.daily_explain import explain_daily
    return JSONResponse(to_jsonable(explain_daily(build(req))))
