"""
api/routes/stock_decision.py — one stock's current decision + full analysis (Stage 2.7G.3). Read-only.

POST /api/insights/stock-decision   {symbol, analysis_id?}

Used right after a card's Analyze finishes (and to re-open a result). It rebuilds the Stage 2.7G decision
for THIS stock only, through the unchanged insights.daily_review.assemble() path with the same inputs the
Daily Review uses (shared caches: market scan, Robinhood view, events, patterns, outcomes) — so the state is
identical to the Daily Review's for the same data. Owned stocks get owned states; others get watchlist states.
It never calls Claude: research was already produced by the existing research endpoint, and "full analysis"
is read from the analysis cache (this session) or the latest saved snapshot.
"""
import re
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import config
from insights import service
from portfolio.models import to_jsonable

router = APIRouter(prefix="/api/insights", tags=["stock-decision"])
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class StockDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=10)
    analysis_id: Optional[str] = Field(default=None, max_length=64, pattern=r"^[0-9a-f]{8,64}$")


def _find(report: dict, symbol: str) -> Optional[dict]:
    for section in ("portfolio", "watchlist"):
        for cards in (report[section].get("groups") or {}).values():
            for c in cards:
                if c["symbol"] == symbol:
                    return c
    return None


@router.post("/stock-decision")
def post_stock_decision(req: StockDecisionRequest) -> JSONResponse:
    from api.routes import daily_review as dr
    from api.routes import portfolio as pr
    from api.routes import trade_review as tr
    from database.database import get_db
    from insights import home
    from insights.daily_review import assemble
    from insights.plain import research_freshness
    from insights.research import lookup_research, research_summary
    from insights.stock_result import from_bundle, from_snapshot
    from services.analysis_cache import analysis_cache

    symbol = req.symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        return JSONResponse({"status": "INVALID", "message": "Invalid symbol."}, status_code=422)
    now = datetime.now(timezone.utc)
    provider, view, conn, events_slow = dr._load_portfolio_budgeted()
    market = service.market_insights(view)                        # cached market scan (no re-scan when warm)
    market_block = home.market_block(market, now)
    held = {p.symbol for p in view.positions} if view is not None else set()
    owned = symbol in held
    metrics = service.symbols_metrics_fn([symbol])
    try:
        events = dr._events_future([symbol], pr._events_for).result(timeout=dr.EVENTS_BUDGET_SECONDS)
    except FuturesTimeout:
        events, events_slow = {}, True
    research = lookup_research(symbol, now, analysis_id=req.analysis_id, cache_get=analysis_cache.get, db_getter=get_db)
    concerns, sector_flags, patterns = {}, {}, None
    if view is not None:                                           # same inputs as the Daily Review report
        policy = pr._policy_block(view)
        concerns = pr._research_and_concerns(view, policy)[1]
        sector_flags = {f["subject"]: f for f in (policy.get("flags", []) if policy.get("enabled") else [])
                        if f["family"] == "sector_concentration"}
        patterns = service.cached_patterns(tr._patterns)
    sector_pct = {x["sector"]: x["pct_change"] for x in market.get("sectors", [])} if market.get("available") else {}
    inputs = service.get_market_inputs()
    spy = inputs.indices.get(config.MARKET_PROXY_SYMBOL) if inputs else None
    report = assemble(
        now=now, market=market, market_block=market_block, connection=conn, view=view,
        watchlist_symbols=[] if owned else [symbol], metrics=metrics, events=events, research={symbol: research},
        sector_of=pr.sector_resolver.sector_for, sector_pct=sector_pct, concerns=concerns, sector_flags=sector_flags,
        patterns=patterns, outcomes=dr._outcomes([symbol]), market_ref_time=getattr(spy, "as_of", None),
        previous=None, research_estimates={}, usage_counts={"last_hour": 0, "today": 0}, events_loading=events_slow)
    card = _find(report, symbol)

    full = None
    cached = analysis_cache.get(req.analysis_id) if req.analysis_id else None
    if cached is not None and cached.symbol.upper() == symbol:
        full = from_bundle(cached)
    else:
        try:
            rows = get_db().list_snapshots(symbol=symbol, limit=1)
            full = from_snapshot(rows[0]) if rows else None
        except Exception:  # noqa: BLE001 - full analysis is optional context
            full = None
    rs = research_summary(research)
    rs["freshness"] = research_freshness(research.age_hours) if research.available else "MISSING"
    return JSONResponse(to_jsonable({
        "status": "OK", "symbol": symbol, "owned": owned if view is not None else None,
        "decision": card, "research": rs, "full_analysis": full,
        "market": {k: market_block.get(k) for k in ("available", "conditions", "verdicts", "next_event")},
        "robinhood": {"connected": view is not None}, "events_note": report.get("events_note"),
        "note": "Current state from the app's deterministic rules; research and the state are shown separately. "
                "Not a buy/sell instruction.",
    }))
