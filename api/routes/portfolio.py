"""
api/routes/portfolio.py — Read-only portfolio awareness endpoints (Stage 2.7C).

GET  /api/portfolio                     portfolio view: values, positions, sectors, events, quality, risk facts
GET  /api/portfolio/status              feature flag + gateway status (no Robinhood call)
GET  /api/portfolio/policy              configured attention-policy rules (config only, no data call)
GET  /api/portfolio/tax-lots/{symbol}   tax lots for one held symbol
GET  /api/portfolio/realized-pnl        realized P&L summary + closing trades
GET  /api/portfolio/orders              recent equity orders (read-only history)
POST /api/portfolio/scenario            pure arithmetic "what if" (never an order)
POST /api/portfolio/add-money-check     beginner "What if I add money?" decision support (Stage 2.7E)
POST /api/portfolio/explain             Claude explanation of the computed facts (grounding-checked)

Every response is 200 with a "status" field: OK | STALE | PORTFOLIO_UNAVAILABLE (+ reason).
Data comes only from the local rh_gateway; nothing is written to the database; research
evidence, Research View, snapshots, outcomes and event scoring are never touched.
Decimals are serialized as exact strings.
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import config
from database.database import get_db
from portfolio import explain as explain_mod
from portfolio.analytics import PortfolioView, build_tax_lots, build_view
from portfolio.models import OrderSummary, Quote, RealizedPnlSummary, to_jsonable
from portfolio.policy import SEPARATION_NOTE, build_rules, policy_report, rule_to_dict
from portfolio.provider import PortfolioProvider, PortfolioUnavailable, get_portfolio_provider
from portfolio.risk_facts import build_risk_facts
from portfolio.scenarios import ScenarioError, run_scenario
from portfolio.sectors import default_resolver
from services.analysis_cache import analysis_cache
from services.event_context import build_event_context
from insights import service as insights_service
from insights.research import lookup_research, research_summary
from insights.stock_check import build_add_money_check
from portfolio.reconciliation import build_reconciliation

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_PNL_SPANS = {"day", "week", "month", "3month", "year", "all"}
_HISTORY_SPANS = {"week", "month", "3month", "ytd", "all"}

# Indirection points so tests can substitute fakes without touching global state.
provider_factory = get_portfolio_provider
event_lookup = build_event_context
sector_resolver = default_resolver


def policy_enabled() -> bool:
    return config.PORTFOLIO_AWARENESS_ENABLED and config.PORTFOLIO_POLICY_ENABLED


def _event_level(symbol: str) -> Optional[str]:
    try:
        bundle = event_lookup(symbol)
    except Exception:  # noqa: BLE001 - event context is optional
        return None
    return getattr(bundle, "event_risk_level", None)


def _respond(payload: dict) -> JSONResponse:
    return JSONResponse(to_jsonable(payload))


def _unavailable(exc: PortfolioUnavailable) -> JSONResponse:
    from insights.home import connection_status   # beginner "how to fix" steps (Stage 2.7F)
    return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": exc.reason, "message": exc.message,
                     "connection": connection_status(exc.reason, exc.message)})


def _meta(result) -> dict:
    return {"status": result.status, "fetched_at": result.fetched_at, "cache_age_s": result.cache_age_s,
            "message": result.message, "components": result.components}


def _events_for(symbols: list) -> dict:
    def safe(sym):
        try:
            return sym, event_lookup(sym)
        except Exception as exc:  # noqa: BLE001 - event data is optional context
            logger.warning("Event context unavailable for %s: %s", sym, exc)
            return sym, None
    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        return dict(pool.map(safe, symbols))


def load_view(provider: PortfolioProvider, with_events: bool = True) -> PortfolioView:
    portfolio = provider.get_portfolio()
    positions = provider.get_positions()
    realized = None
    notes = []
    try:
        r = provider.get_realized_pnl(config.PORTFOLIO_REALIZED_PNL_SPAN)
        realized = RealizedPnlSummary.from_gateway(r.data or {})
    except PortfolioUnavailable as exc:
        notes.append(f"Realized P&L unavailable ({exc.reason}).")
    symbols = [p.get("symbol") for p in (positions.data or {}).get("positions", []) if p.get("symbol")]
    events = _events_for(symbols) if with_events else {}
    view = build_view(portfolio.data or {}, _meta(portfolio), positions.data or {}, _meta(positions), realized,
                      now=datetime.now(timezone.utc), stale_after_s=config.PORTFOLIO_QUOTE_STALE_SECONDS,
                      gap_material_pct=config.PORTFOLIO_VALUATION_GAP_MATERIAL_PCT,
                      sector_fn=sector_resolver.sector_for, event_fn=events.get if with_events else None,
                      classify_fn=sector_resolver.classify)
    view.messages.extend(notes)
    return view


def _policy_block(view: PortfolioView) -> dict:
    if not policy_enabled():
        return {"enabled": False, "separation_note": SEPARATION_NOTE,
                "message": "Portfolio attention policy is turned off (PORTFOLIO_POLICY_ENABLED=false)."}
    rules, errors = build_rules()
    return policy_report(view, rules, errors)


def _reconciliation(provider: Optional[PortfolioProvider], view: PortfolioView) -> dict:
    realized, status = None, None
    if provider is not None:
        try:
            r = provider.get_realized_pnl(config.PORTFOLIO_RECONCILIATION_REALIZED_SPAN)
            realized = RealizedPnlSummary.from_gateway(r.data or {})
        except PortfolioUnavailable as exc:
            status = f"UNAVAILABLE ({exc.reason})"
    return build_reconciliation(view, realized, status)


def _research_and_concerns(view: PortfolioView, policy: dict) -> tuple:
    now = datetime.now(timezone.utc)
    research = {p.symbol: research_summary(lookup_research(p.symbol, now, db_getter=get_db)) for p in view.positions}
    flags = policy.get("flags", []) if policy.get("enabled") else []
    concerns = {}
    for p in view.positions:
        pos = next((f for f in flags if f["family"] == "position_concentration" and f["subject"] == p.symbol), None)
        sec = next((f for f in flags if f["family"] == "sector_concentration" and f["subject"] == p.sector), None)
        concerns[p.symbol] = {"position_severity": pos["severity"] if pos else None,
                              "position_weight": pos["value"] if pos else p.portfolio_weight,
                              "sector": p.sector, "sector_severity": sec["severity"] if sec else None,
                              "sector_weight": sec["value"] if sec else None,
                              "sector_unclassified": p.sector == "UNCLASSIFIED"}
    return research, concerns


def _view_payload(view: PortfolioView, provider: Optional[PortfolioProvider] = None) -> dict:
    policy = _policy_block(view)
    research, concerns = _research_and_concerns(view, policy)
    return {
        "status": view.status,
        "reconciliation": _reconciliation(provider, view),
        "research_by_symbol": research,
        "concerns_by_symbol": concerns,
        "snapshot": view.snapshot,
        "positions": view.positions,
        "sector_exposure": view.sector_exposure,
        "event_exposure": view.event_exposure,
        "quote_quality_summary": view.quote_quality_summary,
        "basis_summary": view.basis_summary,
        "risk_facts": build_risk_facts(view, config.PORTFOLIO_RISK_RULES_JSON),
        "policy": policy,
        "sector_sources": sector_resolver.describe(),
        "freshness": view.freshness,
        "messages": view.messages,
        "valuation_gap_material_pct": config.PORTFOLIO_VALUATION_GAP_MATERIAL_PCT,
        "disclaimer": "Read-only view of your brokerage data. This app never places, prepares or cancels orders.",
    }


@router.get("")
def get_portfolio_view() -> JSONResponse:
    provider = provider_factory()
    try:
        return _respond(_view_payload(load_view(provider), provider))
    except PortfolioUnavailable as exc:
        return _unavailable(exc)


@router.get("/status")
def get_portfolio_status() -> JSONResponse:
    enabled = config.PORTFOLIO_AWARENESS_ENABLED
    if not enabled:
        return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": "DISABLED", "enabled": False,
                         "message": "Portfolio awareness is turned off (PORTFOLIO_AWARENESS_ENABLED=false)."})
    try:
        accounts = provider_factory().get_accounts()
        return _respond({"status": accounts.status, "enabled": True, "accounts": accounts.data,
                         "fetched_at": accounts.fetched_at})
    except PortfolioUnavailable as exc:
        return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": exc.reason, "enabled": True,
                         "message": exc.message})


@router.get("/policy")
def get_policy_rules() -> JSONResponse:
    rules, errors = build_rules()
    return _respond({"status": "OK", "enabled": policy_enabled(),
                     "awareness_enabled": config.PORTFOLIO_AWARENESS_ENABLED,
                     "policy_flag": config.PORTFOLIO_POLICY_ENABLED,
                     "active_rules": [rule_to_dict(r) for r in rules if r.enabled],
                     "inactive_rules": [rule_to_dict(r) for r in rules if not r.enabled],
                     "rule_errors": errors, "separation_note": SEPARATION_NOTE,
                     "note": "Attention flags only — not trading instructions. Edit thresholds via "
                             "PORTFOLIO_POLICY_* settings."})


@router.get("/tax-lots/{symbol}")
def get_tax_lots(symbol: str) -> JSONResponse:
    symbol = symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": "DATA_UNAVAILABLE", "message": "Invalid symbol."})
    try:
        r = provider_factory().get_tax_lots(symbol)
    except PortfolioUnavailable as exc:
        return _unavailable(exc)
    data = r.data or {}
    quote = Quote.from_gateway(data["quote"]) if data.get("quote") else None
    lots, quote_info = build_tax_lots(data.get("tax_lots", []), quote, today=date.today(),
                                      now=datetime.now(timezone.utc),
                                      stale_after_s=config.PORTFOLIO_QUOTE_STALE_SECONDS)
    return _respond({"status": r.status, "symbol": symbol, "tax_lots": lots, "quote": quote_info,
                     "truncated": r.truncated, "fetched_at": r.fetched_at, "message": r.message,
                     "note": "Holding-period figures are informational, not tax advice. Lots with missing basis "
                             "are shown as basis pending, never as $0."})


@router.get("/realized-pnl")
def get_realized_pnl(span: str = "3month") -> JSONResponse:
    if span not in _PNL_SPANS:
        return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": "DATA_UNAVAILABLE", "message": "Invalid span."})
    provider = provider_factory()
    try:
        summary = provider.get_realized_pnl(span)
        history_span = span if span in _HISTORY_SPANS else "3month"
        trades = provider.get_pnl_history(history_span)
    except PortfolioUnavailable as exc:
        return _unavailable(exc)
    result = RealizedPnlSummary.from_gateway(summary.data or {}, (trades.data or {}).get("trades", []))
    status = "STALE" if "STALE" in (summary.status, trades.status) else "OK"
    return _respond({"status": status, "realized_pnl": result, "trades_span": history_span,
                     "trades_truncated": trades.truncated, "fetched_at": summary.fetched_at,
                     "note": "Realized P&L only (closed positions); informational, not a tax document."})


@router.get("/orders")
def get_orders(since: Optional[str] = None) -> JSONResponse:
    if since is not None and not re.match(r"^\d{4}-\d{2}-\d{2}$", since):
        return _respond({"status": "PORTFOLIO_UNAVAILABLE", "reason": "DATA_UNAVAILABLE", "message": "Invalid date."})
    try:
        r = provider_factory().get_orders(since)
    except PortfolioUnavailable as exc:
        return _unavailable(exc)
    data = r.data or {}
    return _respond({"status": r.status, "since": data.get("since"),
                     "orders": [OrderSummary.from_gateway(o) for o in data.get("orders", [])],
                     "truncated": r.truncated, "fetched_at": r.fetched_at,
                     "note": "Read-only order history. This app cannot place, change or cancel orders."})


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hypothetical_add", "event_risk_exposure", "cash_after_purchase"]
    symbol: Optional[str] = Field(default=None, max_length=10)
    amount_usd: Optional[float] = None
    funding: Literal["cash", "new_money"] = "cash"
    level: Literal["HIGH", "MEDIUM", "LOW", "NONE", "UNKNOWN"] = "HIGH"


@router.post("/scenario")
def post_scenario(req: ScenarioRequest) -> JSONResponse:
    try:
        # Event context is needed for event-exposure scenarios AND for policy impact (event rules).
        view = load_view(provider_factory(), with_events=req.type == "event_risk_exposure" or policy_enabled())
    except PortfolioUnavailable as exc:
        return _unavailable(exc)
    rules = build_rules()[0] if policy_enabled() else None
    try:
        result = run_scenario(view, req.model_dump(), sector_resolver.sector_for, rules=rules,
                              event_level_fn=_event_level)
    except ScenarioError as exc:
        return _respond({"status": "SCENARIO_INVALID", "message": str(exc)})
    return _respond({"status": view.status, "result": result, "policy_enabled": rules is not None})


class AddMoneyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=10)
    amount_usd: float
    funding: Literal["new_money", "cash"] = "new_money"
    analysis_id: Optional[str] = Field(default=None, max_length=64, pattern=r"^[0-9a-f]{8,64}$")


def _build_check(req: AddMoneyRequest, view: PortfolioView, rules) -> dict:
    symbol = req.symbol.strip().upper()
    if not _SYMBOL.match(symbol):
        raise ScenarioError("Invalid symbol.")
    now = datetime.now(timezone.utc)
    research = lookup_research(symbol, now, analysis_id=req.analysis_id, cache_get=analysis_cache.get, db_getter=get_db)
    cached = analysis_cache.get(req.analysis_id) if req.analysis_id else None
    if cached is not None and cached.symbol.upper() == symbol:
        metrics, sector_ctx, events = cached.bundle.metrics, cached.bundle.sector, cached.bundle.events
    else:
        metrics, sector_ctx = insights_service.stock_data_fn(symbol)
        try:
            events = event_lookup(symbol)
        except Exception:  # noqa: BLE001 - event data is optional
            events = None
    market = insights_service.market_insights(view)
    return build_add_money_check(symbol=symbol, amount_usd=req.amount_usd, funding=req.funding, view=view,
                                 rules=rules, research=research, metrics=metrics, sector_ctx=sector_ctx,
                                 events_bundle=events, market=market, sector_fn=sector_resolver.sector_for, now=now)


@router.post("/add-money-check")
def post_add_money_check(req: AddMoneyRequest) -> JSONResponse:
    try:
        view = load_view(provider_factory())
    except PortfolioUnavailable as exc:
        return _unavailable(exc)
    rules = build_rules()[0] if policy_enabled() else None
    try:
        check = _build_check(req, view, rules)
    except ScenarioError as exc:
        return _respond({"status": "SCENARIO_INVALID", "message": str(exc)})
    return _respond({"status": view.status, "check": check})


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: Optional[ScenarioRequest] = None
    check: Optional[AddMoneyRequest] = None


@router.post("/explain")
def post_explain(req: ExplainRequest) -> JSONResponse:
    provider = provider_factory()
    try:
        view = load_view(provider)
    except PortfolioUnavailable as exc:
        return _unavailable(exc)
    rules = build_rules()[0] if policy_enabled() else None
    scenario = None
    check = None
    if req.check is not None:
        try:
            check = _build_check(req.check, view, rules)
        except ScenarioError as exc:
            return _respond({"status": "SCENARIO_INVALID", "message": str(exc)})
        scenario = check["scenario"]
    if req.scenario is not None and check is None:
        try:
            scenario = run_scenario(view, req.scenario.model_dump(), sector_resolver.sector_for, rules=rules,
                                    event_level_fn=_event_level)
        except ScenarioError as exc:
            return _respond({"status": "SCENARIO_INVALID", "message": str(exc)})
    risk = build_risk_facts(view, config.PORTFOLIO_RISK_RULES_JSON)
    policy = _policy_block(view)
    symbols = [p.symbol for p in view.positions]
    if req.scenario is not None and req.scenario.symbol:
        symbols.append(req.scenario.symbol.strip().upper())
    if req.check is not None:
        symbols.append(req.check.symbol.strip().upper())
    research = explain_mod.latest_saved_research(sorted(set(symbols)), get_db)
    return _respond({"portfolio_status": view.status,
                     **explain_mod.explain(view, risk, research, scenario, policy=policy,
                                           reconciliation=_reconciliation(provider, view), check=check)})
