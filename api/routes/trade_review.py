"""
api/routes/trade_review.py — TRADER REVIEW (Stage 2.7E). Educational decision review; never orders.

POST /api/trade-review/quick                  QUICK TRADE CHECK (beginner): stock + amount + timeframe, all else automatic
POST /api/trade-review/before                 review a HYPOTHETICAL trade with current verified information
GET  /api/trade-review/position/{symbol}      review a held Robinhood position (at entry vs now, outcomes as hindsight)
GET  /api/trade-review/patterns               deterministic patterns across recent verified buy entries
POST /api/trade-review/explain                Claude explanation of a review (grounding/language/forecast-checked)
GET  /api/trade-review/checklist              the disciplined-trader checklist (nothing is saved)

Reads only: rh_gateway (allowlisted reads), Alpaca daily bars, Stage 2.5 snapshots (SELECT), Stage 2.6 events.
Nothing is written anywhere.
"""
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Literal, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import config
from api.routes import portfolio as pr
from database.database import get_db
from insights import service
from insights.labels import PRICE_LOCATION_TEXT
from insights.research import lookup_research, research_summary
from insights.stock_check import build_add_money_check
from insights.trade_review import (CHECKLIST, HISTORY_LOOKBACK_DAYS, HOLDING_PERIODS, LESSON_TEXT, LESSONS,
                                   attach_research, review_lot, review_process, simple_review, technical_state,
                                   trading_patterns)
from portfolio import explain as explain_mod
from portfolio.models import to_jsonable
from portfolio.policy import build_rules, parse_bands
from portfolio.provider import PortfolioUnavailable
from portfolio.scenarios import ScenarioError
from services.analysis_cache import analysis_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trade-review", tags=["trade-review"])
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
DISCLAIMER = ("Educational decision review. It judges the PROCESS with the information available at the time — not "
              "whether the trade made money — and it is not a buy/sell recommendation. No order is created.")


def _respond(payload: dict) -> JSONResponse:
    return JSONResponse(to_jsonable(payload))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _position_info_band() -> Decimal:
    bands, _ = parse_bands(config.PORTFOLIO_POLICY_POSITION_WEIGHT_PCT)
    return next((b for b in bands if b is not None), Decimal("10"))


def _current_state(symbol: str, view, entry_price: Optional[Decimal] = None, analysis_id: Optional[str] = None):
    research = lookup_research(symbol, _now(), analysis_id=analysis_id, cache_get=analysis_cache.get, db_getter=get_db)
    metrics, sector_ctx = service.stock_data_fn(symbol)
    try:
        events = pr.event_lookup(symbol)
    except Exception:  # noqa: BLE001
        events = None
    market = service.market_insights(view)
    held = next((p for p in view.positions if p.symbol == symbol), None)
    price = entry_price if entry_price is not None else (held.last_price if held and held.last_price else None)
    st = technical_state(metrics, price, "CURRENT", getattr(metrics, "as_of", None).isoformat()
                         if metrics is not None and getattr(metrics, "as_of", None) is not None else None)
    attach_research(st, current=research)
    st.event_risk = getattr(events, "event_risk_level", None) or "UNAVAILABLE"
    st.event_source = "Stage 2.6 (current)" if events is not None else "UNAVAILABLE"
    if market.get("available"):
        st.market, st.market_source = market["environment"], "current market overview"
    return st, research, metrics, sector_ctx, events, market


class BeforeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=10)
    amount_usd: float
    entry_price: Optional[float] = Field(default=None, gt=0)
    holding_period: Optional[Literal["days", "weeks", "months", "long_term"]] = None
    reason: Optional[str] = Field(default=None, max_length=500)
    invalidation: Optional[str] = Field(default=None, max_length=500)
    funding: Literal["new_money", "cash"] = "new_money"
    analysis_id: Optional[str] = Field(default=None, max_length=64, pattern=r"^[0-9a-f]{8,64}$")


def _before(req: BeforeRequest) -> dict:
    symbol = req.symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        raise ScenarioError("Invalid symbol.")
    view = pr.load_view(pr.provider_factory())
    rules = build_rules()[0] if pr.policy_enabled() else None
    entry = Decimal(str(req.entry_price)) if req.entry_price else None
    st, research, metrics, sector_ctx, events, market = _current_state(symbol, view, entry, req.analysis_id)
    check = build_add_money_check(symbol=symbol, amount_usd=req.amount_usd, funding=req.funding, view=view,
                                  rules=rules, research=research, metrics=metrics, sector_ctx=sector_ctx,
                                  events_bundle=events, market=market, sector_fn=pr.sector_resolver.sector_for,
                                  now=_now())
    conc = [f for f in check["fit"]["caution"] if f["code"] == "concentration_high"]
    weight_after = check["layers"]["portfolio"]["position_weight_after_pct"]
    fit = {"creates_or_worsens_high": bool(conc), "text": conc[0]["text"] if conc else None,
           "small_position": weight_after is not None and Decimal(str(weight_after)) < _position_info_band(),
           "weight_after_pct": weight_after, "sector": check["layers"]["portfolio"]["sector"],
           "sector_weight_after_pct": check["layers"]["portfolio"]["sector_weight_after_pct"]}
    plan = {"reason": (req.reason or "").strip() or None, "holding_period": req.holding_period,
            "invalidation": (req.invalidation or "").strip() or None, "amount": req.amount_usd}
    review = review_process(st, plan=plan, portfolio_fit=fit)
    return {"status": view.status, "mode": "BEFORE", "symbol": symbol, "review": review, "entry_state": st,
            "plan": {**plan, "holding_period_text": HOLDING_PERIODS.get(req.holding_period)},
            "context": {"market": check["layers"]["market"], "research": research_summary(research),
                        "price_area": check["price_area"], "events": check["events"],
                        "portfolio": check["layers"]["portfolio"], "feasibility": check["feasibility"]},
            "lessons": LESSONS, "lesson_text": LESSON_TEXT, "disclaimer": DISCLAIMER}


@router.post("/before")
def post_before(req: BeforeRequest) -> JSONResponse:
    try:
        return _respond(_before(req))
    except PortfolioUnavailable as exc:
        return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": exc.reason, "message": exc.message})
    except ScenarioError as exc:
        return _respond({"status": "SCENARIO_INVALID", "message": str(exc)})


def _position(symbol: str) -> dict:
    provider = pr.provider_factory()
    view = pr.load_view(provider)
    held = next((p for p in view.positions if p.symbol == symbol), None)
    if held is None:
        return {"status": "NOT_HELD", "symbol": symbol, "message": f"{symbol} is not a current position."}
    lots_resp = provider.get_tax_lots(symbol)
    lots = (lots_resp.data or {}).get("tax_lots", [])
    bars = service.history_fn([symbol, config.MARKET_PROXY_SYMBOL], HISTORY_LOOKBACK_DAYS)
    try:
        snapshots = get_db().list_snapshots(symbol=symbol, limit=200)
    except Exception:  # noqa: BLE001
        snapshots = []
    reviews = [review_lot(symbol, lot, bars.get(symbol), bars.get(config.MARKET_PROXY_SYMBOL), snapshots)
               for lot in sorted(lots, key=lambda l: l.get("open_date") or "")]
    for r in reviews:
        r["simple"] = simple_review(r)
    now_state, research, *_ = _current_state(symbol, view)
    first = next((r for r in reviews if r.get("entry_state") is not None), None)

    def pair(label, at_entry, now, source_entry):
        if label == "Price location":   # plain words, never raw codes
            at_entry, now = PRICE_LOCATION_TEXT.get(at_entry, at_entry), PRICE_LOCATION_TEXT.get(now, now)
            at_entry = None if at_entry == PRICE_LOCATION_TEXT["UNAVAILABLE"] else at_entry
        return {"item": label, "at_entry": at_entry if at_entry not in (None, "UNAVAILABLE") else None,
                "now": now, "at_entry_source": source_entry if at_entry not in (None, "UNAVAILABLE") else
                "not saved / unavailable"}
    es = first["entry_state"] if first else None
    changes = [
        pair("Research view", es.research_view if es else None, now_state.research_view,
             es.research_source if es else None),
        pair("Trend", es.trend if es else None, now_state.trend, "reconstructed from prices before entry"),
        pair("Price location", es.location if es else None, now_state.location, "reconstructed from prices before entry"),
        pair("Event risk", es.event_risk if es else None, now_state.event_risk, es.event_source if es else None),
        pair("Portfolio weight", None, held.portfolio_weight, None),
        pair(f"{held.sector} exposure", None, next((x["weight_pct"] for x in view.sector_exposure
                                                   if x["sector"] == held.sector), None), None),
    ]
    return {"status": view.status, "mode": "AFTER", "symbol": symbol,
            "position": {"quantity": held.quantity, "avg_cost": held.avg_cost, "cost_basis": held.cost_basis_total,
                         "price_now": held.last_price, "value_now": held.market_value,
                         "unrealized_pnl": held.unrealized_pnl, "unrealized_pnl_pct": held.unrealized_pnl_pct,
                         "weight_now_pct": held.portfolio_weight, "sector": held.sector,
                         "price_time": held.price_timestamp, "quote_quality": held.quote_quality},
            "lots": reviews, "first_entry_date": first["open_date"] if first else None,
            "changes_since_entry": changes, "now_state": now_state,
            "research_now": research_summary(research),
            "hindsight_note": ("Outcomes after each entry are shown separately as HINDSIGHT. The process review uses "
                               "only information available before the entry."),
            "lessons": LESSONS, "lesson_text": LESSON_TEXT, "disclaimer": DISCLAIMER}


@router.get("/position/{symbol}")
def get_position_review(symbol: str) -> JSONResponse:
    symbol = symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        return _respond({"status": "SCENARIO_INVALID", "message": "Invalid symbol."})
    try:
        return _respond(_position(symbol))
    except PortfolioUnavailable as exc:
        return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": exc.reason, "message": exc.message})


def _patterns(days: int = 90) -> dict:
    provider = pr.provider_factory()
    since = (date.today() - timedelta(days=days)).isoformat()
    orders = (provider.get_orders(since).data or {}).get("orders", [])
    buys = [o for o in orders if o.get("side") == "buy" and o.get("state") == "filled" and o.get("average_price")
            and o.get("created_at") and o.get("symbol")]
    symbols = sorted({o["symbol"] for o in buys})
    bars = service.history_fn(symbols + [config.MARKET_PROXY_SYMBOL], HISTORY_LOOKBACK_DAYS) if buys else {}
    entries = []
    for o in buys:
        lot = {"lot_id": o.get("order_id"), "open_date": o["created_at"][:10], "cost_per_share": o["average_price"],
               "open_type": "buy order", "quantity": o.get("cumulative_quantity")}
        entries.append({"symbol": o["symbol"], **review_lot(o["symbol"], lot, bars.get(o["symbol"]),
                                                            bars.get(config.MARKET_PROXY_SYMBOL), [])})
    result = trading_patterns(entries)
    return {"status": "OK", "since": since, "buy_entries": len(buys), **result,
            "source": "Verified filled buy orders from Robinhood (read-only) + historical daily prices before each "
                      "entry."}


@router.get("/patterns")
def get_patterns() -> JSONResponse:
    try:
        return _respond(_patterns())
    except PortfolioUnavailable as exc:
        return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": exc.reason, "message": exc.message})


@router.get("/checklist")
def get_checklist() -> JSONResponse:
    return _respond({"status": "OK", "checklist": CHECKLIST, "saved": False, "lessons": LESSONS,
                     "note": "Your answers stay in this browser tab only; nothing is saved."})


class ExplainReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    before: Optional[BeforeRequest] = None
    position: Optional[str] = Field(default=None, max_length=10)


@router.post("/explain")
def post_explain(req: ExplainReviewRequest) -> JSONResponse:
    try:
        if req.before is not None:
            review = _before(req.before)
        elif req.position:
            review = _position(req.position.strip().upper())
        else:
            return _respond({"status": "SCENARIO_INVALID", "message": "Provide 'before' or 'position'."})
    except PortfolioUnavailable as exc:
        return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": exc.reason, "message": exc.message})
    except ScenarioError as exc:
        return _respond({"status": "SCENARIO_INVALID", "message": str(exc)})
    if review.get("status") not in ("OK", "STALE"):
        return _respond(review)
    return _respond({"review_status": review["status"], **explain_mod.explain_trade_review(review)})


class QuickRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=10)
    amount_usd: float = Field(gt=0, le=100_000_000)
    timeframe: Literal["today", "days", "weeks", "long_term"] = "days"
    reason: Optional[Literal["pullback", "breakout", "news", "earnings", "long_term", "momentum", "researching"]] = None
    note: Optional[str] = Field(default=None, max_length=200)
    analysis_id: Optional[str] = Field(default=None, max_length=64, pattern=r"^[0-9a-f]{8,64}$")
    refresh: bool = False
    previous: Optional[Dict[str, Optional[str]]] = None


def _catalysts(symbol: str, analysis_id: Optional[str]) -> list:
    """Verified catalysts from the fresh research bundle, else the latest saved snapshot. Never invented."""
    import json as _json
    cached = analysis_cache.get(analysis_id) if analysis_id else None
    if cached is not None and cached.symbol.upper() == symbol:
        return [{"title": c.title, "source": c.source, "published_at": c.published_at, "sentiment": c.sentiment}
                for c in cached.bundle.catalyst.items]
    try:
        rows = get_db().list_snapshots(symbol=symbol, limit=1)
        return [{"title": c.get("title"), "source": c.get("source"), "published_at": c.get("published_at"),
                 "sentiment": c.get("sentiment")} for c in _json.loads(rows[0].catalysts_json or "[]")] if rows else []
    except Exception:  # noqa: BLE001
        return []


@router.post("/quick")
def post_quick(req: QuickRequest) -> JSONResponse:
    from insights.quick_check import build_quick_check
    symbol = req.symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        return _respond({"status": "SCENARIO_INVALID", "message": "Invalid symbol."})
    if req.previous is not None and (len(req.previous) > 12 or any(len(str(v or "")) > 64 for v in req.previous.values())):
        return _respond({"status": "SCENARIO_INVALID", "message": "Invalid previous check."})
    try:
        if req.refresh:
            service.get_market_inputs(refresh=True)       # market data only — never an AI call
        view = pr.load_view(pr.provider_factory())
        rules = build_rules()[0] if pr.policy_enabled() else None
        now = _now()
        research = lookup_research(symbol, now, analysis_id=req.analysis_id, cache_get=analysis_cache.get,
                                   db_getter=get_db)
        cached = analysis_cache.get(req.analysis_id) if req.analysis_id else None
        if cached is not None and cached.symbol.upper() == symbol and not req.refresh:
            metrics, sector_ctx, events = cached.bundle.metrics, cached.bundle.sector, cached.bundle.events
        else:
            metrics, sector_ctx = service.stock_data_fn(symbol)
            try:
                events = pr.event_lookup(symbol)
            except Exception:  # noqa: BLE001
                events = None
        market = service.market_insights(view)
        check = build_add_money_check(symbol=symbol, amount_usd=req.amount_usd, funding="new_money", view=view,
                                      rules=rules, research=research, metrics=metrics, sector_ctx=sector_ctx,
                                      events_bundle=events, market=market, sector_fn=pr.sector_resolver.sector_for,
                                      now=now)
    except PortfolioUnavailable as exc:
        return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": exc.reason, "message": exc.message})
    except ScenarioError as exc:
        return _respond({"status": "SCENARIO_INVALID", "message": str(exc)})
    quick = build_quick_check(check, timeframe=req.timeframe, reason=req.reason, note=req.note,
                              catalysts=_catalysts(symbol, req.analysis_id), previous=req.previous,
                              checked_at=now.isoformat(timespec="seconds"))
    return _respond({"status": view.status, "quick": quick, "market_fetched_at": market.get("fetched_at"),
                     "stock_data_as_of": getattr(metrics, "as_of", None) if metrics is not None else None})
