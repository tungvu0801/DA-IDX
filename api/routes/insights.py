"""
api/routes/insights.py — Beginner market context (Stage 2.7E). Read-only; never a prediction or an order.

GET  /api/insights/market?refresh=true|false   "Current market — what's going on?" (deterministic labels)
POST /api/insights/market/explain              Claude explanation of those facts (grounding/causal/forecast-checked)
POST /api/insights/home                        Beginner command center (market + Robinhood + my stocks + watchlist)
POST /api/insights/new-money                   "I have new money": group holdings + watchlist by current conditions
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, Literal, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

import config
from insights import service
from portfolio import explain as explain_mod
from portfolio.models import to_jsonable

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/insights", tags=["insights"])


def _portfolio_view_or_none():
    """Portfolio exposure is optional context for the market card; any failure just omits it."""
    if not config.PORTFOLIO_AWARENESS_ENABLED:
        return None
    try:
        from api.routes import portfolio as portfolio_routes
        return portfolio_routes.load_view(portfolio_routes.provider_factory(), with_events=False)
    except Exception:  # noqa: BLE001
        return None


@router.get("/market")
def get_market(refresh: bool = False) -> JSONResponse:
    return JSONResponse(to_jsonable(service.market_insights(_portfolio_view_or_none(), refresh=refresh)))


@router.post("/market/explain")
def post_market_explain() -> JSONResponse:
    insights = service.market_insights(_portfolio_view_or_none())
    if not insights.get("available"):
        return JSONResponse(to_jsonable({"status": "MARKET_DATA_UNAVAILABLE", "message": insights.get("message"),
                                         "explanation": None}))
    if insights.get("stale"):
        return JSONResponse(to_jsonable({"status": "STALE", "message": insights.get("stale_message"),
                                         "explanation": None}))
    return JSONResponse(to_jsonable({"market_fetched_at": insights.get("fetched_at"),
                                     **explain_mod.explain_market(insights)}))


_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_ANALYSIS_ID = re.compile(r"^[0-9a-f]{8,64}$")


class ResearchIds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_ids: Optional[Dict[str, str]] = None

    @field_validator("analysis_ids")
    @classmethod
    def _ids(cls, v):
        if v is None:
            return v
        if len(v) > 40 or any(not _SYMBOL.match(k) or not _ANALYSIS_ID.match(x) for k, x in v.items()):
            raise ValueError("invalid analysis_ids")
        return v


def _research_lookups(symbols, ids, now):
    from database.database import get_db
    from insights.research import lookup_research
    from services.analysis_cache import analysis_cache
    ids = ids or {}
    return {s: lookup_research(s, now, analysis_id=ids.get(s), cache_get=analysis_cache.get, db_getter=get_db)
            for s in symbols}


def _research_map(symbols, ids, now):
    from insights.research import research_summary
    return {s: research_summary(r) for s, r in _research_lookups(symbols, ids, now).items()}


def _decision_states(*, now, market, market_block, conn, view, metrics, research, concerns, policy, market_ref_time,
                     events_loading):
    """Stage 2.8C: the unchanged Stage 2.7G state of each holding, from inputs this request ALREADY loaded — no
    extra market scan, Robinhood fetch, event fetch or Claude call. A state reads only the Stage 2.6 event RISK
    LEVEL, which every position already carries (build_evidence falls back to it), so no event bundles are needed.
    Only the state and its group are returned; the full decision stays with Analyze / the Daily Review."""
    from api.routes import portfolio as pr
    from insights.daily_review import assemble
    sector_flags = {f["subject"]: f for f in (policy.get("flags", []) if policy.get("enabled") else [])
                    if f["family"] == "sector_concentration"}
    sector_pct = {x["sector"]: x["pct_change"] for x in market.get("sectors", [])} if market.get("available") else {}
    report = assemble(
        now=now, market=market, market_block=market_block, connection=conn, view=view, watchlist_symbols=[],
        metrics=metrics, events={}, research=research, sector_of=pr.sector_resolver.sector_for, sector_pct=sector_pct,
        concerns=concerns, sector_flags=sector_flags, patterns=None, outcomes={}, market_ref_time=market_ref_time,
        previous=None, research_estimates={}, usage_counts={"last_hour": 0, "today": 0}, events_loading=events_loading)
    return {c["symbol"]: {"state": c["state"], "group": c["group"]}
            for cards in (report["portfolio"]["groups"] or {}).values() for c in cards}


def _load_portfolio():
    """(provider, view, connection_status). Robinhood being offline never breaks the market side."""
    from api.routes import portfolio as pr
    from insights.home import connection_status
    from portfolio.provider import PortfolioUnavailable
    if not config.PORTFOLIO_AWARENESS_ENABLED:
        return None, None, connection_status("DISABLED", "Portfolio awareness is turned off.")
    provider = pr.provider_factory()
    try:
        return provider, pr.load_view(provider), connection_status(None, None)
    except PortfolioUnavailable as exc:
        return provider, None, connection_status(exc.reason, exc.message)


@router.post("/home")
def post_home(req: ResearchIds) -> JSONResponse:
    from agents.usage_tracker import usage_tracker
    from api.routes import portfolio as pr
    from insights import home
    from portfolio.policy import parse_bands
    from api.routes.daily_review import _load_portfolio_budgeted  # Stage 2.7G.1: bounded wait on event calendars
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=1) as pool:     # market scan and Robinhood load run side by side
        warm = pool.submit(service.get_market_inputs)
        provider, view, conn, events_slow = _load_portfolio_budgeted()
        inputs = warm.result()
    market = service.market_insights(view)
    spy_m = inputs.indices.get(config.MARKET_PROXY_SYMBOL) if inputs else None
    session = home.session_label(getattr(spy_m, "as_of", None), now)
    out = {"now": now.isoformat(timespec="seconds"), "market": home.market_block(market, now), "robinhood": conn,
           "session": session}
    if events_slow and view is not None:   # positions loaded without events: event risk is UNKNOWN, never "low"
        out["events_note"] = ("Event calendars are still loading, so event risk for your holdings is unknown for now. "
                              "Press Refresh in a moment.")
    held = set()
    if view is not None:
        from insights.research import research_summary
        policy = pr._policy_block(view)
        lookups = _research_lookups([p.symbol for p in view.positions], req.analysis_ids, now)
        research, concerns = {s: research_summary(r) for s, r in lookups.items()}, \
            pr._research_and_concerns(view, policy)[1]
        reconciliation = pr._reconciliation(provider, view)
        metrics = service.symbols_metrics_fn([p.symbol for p in view.positions])
        change = home.session_change(view, metrics, spy_m)
        cards = home.stock_cards(view, metrics, research, concerns, session)
        band = float(next((b for b in parse_bands(config.PORTFOLIO_POLICY_SECTOR_WEIGHT_PCT)[0] if b is not None), 25))
        held = {p.symbol for p in view.positions}
        out.update({
            "robinhood": {**conn, "summary": home.robinhood_summary(view, reconciliation, policy, change, session, now)},
            "my_stocks": cards,
            "vs_market": home.vs_market(change, market.get("indices", []) if market.get("available") else [], view,
                                        market.get("sectors", []) if market.get("available") else []),
            "contributors": home.contributors(change) if change["available"] else None,
            "session_change": {k: change[k] for k in ("available", "change", "pct", "base", "excluded", "basis")},
            "feedback": home.daily_feedback(view, market if market.get("available") else {}, cards, change, band),
            "research_coverage": home.research_coverage(cards, usage_tracker.summary_today()),
        })
        out["layers"] = [home.fit_together(out["market"], c) for c in cards]
        try:
            out["decision_states"] = _decision_states(
                now=now, market=market, market_block=out["market"], conn=conn, view=view, metrics=metrics,
                research=lookups, concerns=concerns, policy=policy, market_ref_time=getattr(spy_m, "as_of", None),
                events_loading=events_slow)
        except Exception:  # noqa: BLE001 - the cards fall back to their attention badge
            logger.exception("Decision states unavailable for the command center")
            out["decision_states"] = None
        from api.routes import trade_review as tr
        out["pattern"] = home.pattern_card(service.cached_patterns(tr._patterns))
    try:
        from api.routes.watchlist import get_cached_watchlist
        wl_symbols, wl_metrics = get_cached_watchlist()
        # owned is None (unknown) while Robinhood is not connected — never guessed either way
        out["watchlist"] = [{"symbol": s, "owned": (s in held) if view is not None else None,
                             "price": getattr(wl_metrics.get(s), "price", None),
                             "session_pct": getattr(wl_metrics.get(s), "pct_change", None),
                             "signal": getattr(wl_metrics.get(s), "signal", None)} for s in wl_symbols]
        # Stage 2.8D: for names NOT already in My Stocks, the saved research (local read, same lookup as the cards)
        # and the cards' existing trend word from the cached watchlist metrics. No calculation, no external call.
        from insights.labels import trend_label
        from insights.plain import research_freshness
        from insights.research import research_summary
        others = [s for s in wl_symbols if s not in held]
        out["watchlist_context"] = {}
        for s, r in _research_lookups(others, req.analysis_ids, now).items():
            rs, m = research_summary(r), wl_metrics.get(s)
            out["watchlist_context"][s] = {
                "research": {"available": rs["available"], "view": rs["research_view"], "age_hours": rs["age_hours"],
                             "freshness": research_freshness(rs["age_hours"]) if rs["available"] else "MISSING"},
                "trend": {"UPTREND": "Up", "DOWNTREND": "Down", "MIXED": "Mixed"}.get(
                    trend_label(m.trend) if m is not None else "", "N/A")}
    except Exception:  # noqa: BLE001 - watchlist is optional context
        out["watchlist"] = []
    return JSONResponse(to_jsonable(out))


class NewMoneyRequest(ResearchIds):
    amount_usd: float = Field(gt=0, le=100_000_000)
    funding: Literal["new_money", "cash"] = "new_money"


@router.post("/new-money")
def post_new_money(req: NewMoneyRequest) -> JSONResponse:
    from analysis.sector_context import SectorContext
    from api.routes import portfolio as pr
    from insights.plain import research_freshness
    from insights.quick_check import candidate_summary
    from insights.research import lookup_research
    from insights.stock_check import build_add_money_check
    from portfolio.policy import build_rules
    from portfolio.scenarios import ScenarioError
    now = datetime.now(timezone.utc)
    provider, view, conn = _load_portfolio()
    if view is None:
        return JSONResponse(to_jsonable({"status": "PORTFOLIO_UNAVAILABLE", "robinhood": conn,
                                         "message": "Connect Robinhood to see how new money would affect your portfolio."}))
    held = [p.symbol for p in view.positions]
    try:
        from api.routes.watchlist import get_cached_watchlist
        watch = [s for s in get_cached_watchlist()[0] if s not in held]
    except Exception:  # noqa: BLE001
        watch = []
    candidates = (held + watch)[:15]
    metrics = service.symbols_metrics_fn(candidates)
    market = service.market_insights(view)
    sectors = {x["sector"]: x for x in market.get("sectors", [])} if market.get("available") else {}
    spy_pct = next((i.get("pct_change") for i in market.get("indices", []) if i.get("symbol") ==
                    config.MARKET_PROXY_SYMBOL), None) if market.get("available") else None
    rules = build_rules()[0] if pr.policy_enabled() else None
    ids = req.analysis_ids or {}

    def events_for(sym):
        try:
            return sym, pr.event_lookup(sym)
        except Exception:  # noqa: BLE001
            return sym, None
    with ThreadPoolExecutor(max_workers=min(8, len(candidates) or 1)) as pool:
        events = dict(pool.map(events_for, candidates))
    groups = {"CONDITIONS WORTH RESEARCHING": [], "MIXED — NEED MORE CONFIRMATION": [], "HIGHER-RISK CONDITIONS": []}
    skipped = []
    from database.database import get_db
    from services.analysis_cache import analysis_cache
    for sym in candidates:
        sector_name = pr.sector_resolver.sector_for(sym)
        row = sectors.get(sector_name) if sector_name else None
        ctx = SectorContext(sector_name, row["etf"], row["pct_change"], None, None, config.MARKET_PROXY_SYMBOL,
                            spy_pct) if row else None
        research = lookup_research(sym, now, analysis_id=ids.get(sym), cache_get=analysis_cache.get, db_getter=get_db)
        try:
            check = build_add_money_check(symbol=sym, amount_usd=req.amount_usd, funding=req.funding, view=view,
                                          rules=rules, research=research, metrics=metrics.get(sym), sector_ctx=ctx,
                                          events_bundle=events.get(sym), market=market,
                                          sector_fn=pr.sector_resolver.sector_for, now=now)
        except ScenarioError as exc:
            skipped.append({"symbol": sym, "reason": str(exc)})
            continue
        summary = candidate_summary(check, sym in held, research_freshness(research.age_hours)
                                    if research.available else "MISSING")
        groups[summary["group"]].append(summary)
    for g in groups.values():
        g.sort(key=lambda c: c["symbol"])          # alphabetical: NOT a ranking
    return JSONResponse(to_jsonable({
        "status": view.status, "amount_usd": req.amount_usd, "funding": req.funding, "groups": groups,
        "skipped": skipped, "evaluated": len(candidates),
        "note": ("Where to research further — not a recommendation or ranking. Stocks are listed alphabetically inside "
                 "each group. Each group describes current conditions only."),
        "market_fetched_at": market.get("fetched_at")}))
