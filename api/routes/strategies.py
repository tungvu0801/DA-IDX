"""
api/routes/strategies.py — Stage 3.1 STRATEGY LAB API (definitions only).

GET  /api/strategies/features                       feature registry + operators + limits + the example (no I/O)
POST /api/strategies/validate                       validation + canonical JSON + hashes + readiness + summary
GET  /api/strategies                                saved strategies (latest version of each)
POST /api/strategies                                save a NEW strategy as v1
GET  /api/strategies/{strategy_id}                  one strategy with its full, ordered version history
POST /api/strategies/{strategy_id}/versions         save an edited spec as v(n+1) — never overwrites a version
GET  /api/strategies/{strategy_id}/versions/{n}     one immutable version
GET  /api/strategies/{strategy_id}/compare?a=&b=    rule-level differences between two versions
POST /api/strategies/{strategy_id}/archive          archive / restore (metadata only; versions untouched)
GET  /api/strategies/shortcuts/watchlist            symbols in watchlist.txt (local file read only)
GET  /api/strategies/shortcuts/holdings             symbols currently held (one read-only gateway /positions read)

Nothing here backtests, paper trades, creates orders, calls Claude, scans the market or fetches events. The two
shortcuts only COPY symbols into the builder; a saved version stores its explicit list, never "my watchlist later".
"""
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from strategy import features as F
from strategy import spec as S
from strategy.examples import EXAMPLE_NOTE, pullback_example
from strategy.store import StrategyError, get_store

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
_ID = re.compile(r"^[0-9a-f]{32}$")
STATUS = {"INVALID_SPEC": 422, "NOT_FOUND": 404, "VERSION_CONFLICT": 409, "NO_CHANGE": 409}
NO_PERFORMANCE = ("Rules only — this summary describes what the strategy does, not how it performed. Historical "
                  "backtests are separate, labelled runs of a saved version; nothing is paper traded.")


class SpecBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: Dict[str, Any]
    notes: Optional[str] = Field(default=None, max_length=500)


class VersionBody(SpecBody):
    based_on_version: Optional[int] = Field(default=None, ge=1)


class ArchiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool


def _err(exc: StrategyError) -> JSONResponse:
    return JSONResponse({"status": exc.code, "message": exc.message, "errors": exc.detail or []},
                        status_code=STATUS.get(exc.code, 400))


def _bad_id(strategy_id: str) -> Optional[JSONResponse]:
    return None if _ID.match(strategy_id) else JSONResponse({"status": "NOT_FOUND", "message": "Strategy not found."},
                                                            status_code=404)


@router.get("/features")
def get_features() -> JSONResponse:
    return JSONResponse({**F.public_registry(), "schema_version": S.SCHEMA_VERSION,
                         "directions": list(S.DIRECTIONS), "timeframes": list(S.TIMEFRAMES), "execution": S.EXECUTION,
                         "exit_methods": {"invalidation": list(S.INVALIDATION_METHODS), "target": list(S.TARGET_METHODS)},
                         "limits": {"max_conditions": S.MAX_CONDITIONS, "max_depth": S.MAX_DEPTH,
                                    "max_symbols": S.MAX_SYMBOLS, "max_holding_days": list(S.MAX_HOLDING_DAYS),
                                    "risk": {k: list(v) for k, v in S.RISK_LIMITS.items()},
                                    "invalidation_pct": list(S.INVALIDATION_METHODS["PCT_BELOW_ENTRY"]),
                                    "target_pct": list(S.TARGET_METHODS["PCT_ABOVE_ENTRY"])},
                         "example": {"note": EXAMPLE_NOTE, "spec": pullback_example()}, "note": NO_PERFORMANCE})


@router.post("/validate")
def post_validate(body: SpecBody) -> JSONResponse:
    return JSONResponse({**S.report(body.spec), "note": NO_PERFORMANCE})


# shortcuts are declared before /{strategy_id} so they are never mistaken for an id
@router.get("/shortcuts/watchlist")
def get_watchlist_symbols() -> JSONResponse:
    from scanner.watchlist import load_watchlist
    syms = [s for s in load_watchlist() if S.SYMBOL_RE.match(s)]
    return JSONResponse({"available": True, "symbols": syms, "origin": "WATCHLIST_COPY",
                         "note": "Copied now. A saved version keeps this exact list even if the watchlist changes."})


@router.get("/shortcuts/holdings")
def get_holding_symbols() -> JSONResponse:
    import config
    if not config.PORTFOLIO_AWARENESS_ENABLED:
        return JSONResponse({"available": False, "symbols": [], "message": "Portfolio awareness is turned off."})
    from api.routes import portfolio as pr
    from portfolio.provider import PortfolioUnavailable
    try:
        positions = pr.provider_factory().get_positions()            # read-only gateway GET /positions
    except PortfolioUnavailable as exc:
        return JSONResponse({"available": False, "symbols": [], "message": exc.message})
    syms = sorted({p.get("symbol") for p in (positions.data or {}).get("positions", []) if p.get("symbol")})
    return JSONResponse({"available": True, "symbols": [s for s in syms if S.SYMBOL_RE.match(s)], "origin": "HOLDINGS_COPY",
                         "note": "Copied now. A saved version keeps this exact list even if your holdings change."})


@router.get("")
def list_strategies(include_archived: bool = False) -> JSONResponse:
    return JSONResponse({"strategies": get_store().list(include_archived), "note": NO_PERFORMANCE})


@router.post("", status_code=201)
def create_strategy(body: SpecBody) -> JSONResponse:
    try:
        return JSONResponse(get_store().create(body.spec, body.notes), status_code=201)
    except StrategyError as exc:
        return _err(exc)


@router.get("/{strategy_id}")
def get_strategy(strategy_id: str) -> JSONResponse:
    if (bad := _bad_id(strategy_id)) is not None:
        return bad
    try:
        return JSONResponse({**get_store().get(strategy_id), "note": NO_PERFORMANCE})
    except StrategyError as exc:
        return _err(exc)


@router.post("/{strategy_id}/versions", status_code=201)
def create_version(strategy_id: str, body: VersionBody) -> JSONResponse:
    if (bad := _bad_id(strategy_id)) is not None:
        return bad
    try:
        return JSONResponse(get_store().add_version(strategy_id, body.spec, body.notes, body.based_on_version),
                            status_code=201)
    except StrategyError as exc:
        return _err(exc)


@router.get("/{strategy_id}/versions/{number}")
def get_version(strategy_id: str, number: int) -> JSONResponse:
    if (bad := _bad_id(strategy_id)) is not None:
        return bad
    try:
        return JSONResponse(get_store().version(strategy_id, number))
    except StrategyError as exc:
        return _err(exc)


@router.get("/{strategy_id}/compare")
def compare_versions(strategy_id: str, a: int = Query(ge=1), b: int = Query(ge=1)) -> JSONResponse:
    if (bad := _bad_id(strategy_id)) is not None:
        return bad
    try:
        va, vb = get_store().version(strategy_id, a), get_store().version(strategy_id, b)
    except StrategyError as exc:
        return _err(exc)
    if "INTEGRITY_ERROR" in (va["integrity"], vb["integrity"]):
        return JSONResponse({"status": "INTEGRITY_ERROR", "message": "A stored version failed its hash check."}, status_code=409)
    return JSONResponse({"a": a, "b": b, "changes": S.compare(va["spec"], vb["spec"]),
                         "same_rules": va["rules_hash"] == vb["rules_hash"], "note": "Rule differences only — no "
                         "performance comparison exists before backtesting."})


@router.post("/{strategy_id}/archive")
def archive_strategy(strategy_id: str, body: ArchiveBody) -> JSONResponse:
    if (bad := _bad_id(strategy_id)) is not None:
        return bad
    try:
        return JSONResponse(get_store().set_archived(strategy_id, body.archived))
    except StrategyError as exc:
        return _err(exc)
