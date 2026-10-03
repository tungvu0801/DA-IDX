"""
api/routes/paper.py — Stage 4.5 LOCAL PAPER PORTFOLIO (paper/). SIMULATED — no broker, no real order, no AI.

GET  /api/paper-portfolio                    account, cash, equity, positions (FIFO lots, latest-close marks), pending
                                             orders, trade history — or the setup fields when no account exists yet
POST /api/paper-portfolio/account            {"name"?, "starting_cash", "slippage_bps", "commission_per_order"}
POST /api/paper-portfolio/account/settings   {"name"?, and before the first fill: "starting_cash"?, "slippage_bps"?,
                                             "commission_per_order"?}
GET  /api/paper-orders?status=               paper orders, newest first
POST /api/paper-orders                       {"symbol", "side": BUY|SELL, "quantity": whole shares, "origin": "MANUAL",
                                             "strategy_version_id"? (provenance only, validated)}
POST /api/paper-orders/preview               the same body: validation + server-defined sessions and estimate, NOT stored
POST /api/paper-orders/{id}/cancel           only a PENDING order (FILLED -> 409 ALREADY_FILLED; CANCELLED -> no-op)
POST /api/paper-orders/process               fill pending orders whose next-session open is final (explicit action)
GET  /api/paper-fills                        the immutable paper fills

Strict bodies: no fill price, session, date, cash amount or order time is ever accepted from the browser — the server
derives them. No real broker endpoint exists here.
"""
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from paper import execution as X
from paper import portfolio as P
from paper.store import PaperStore

portfolio_router = APIRouter(prefix="/api/paper-portfolio", tags=["paper-portfolio"])
orders_router = APIRouter(prefix="/api/paper-orders", tags=["paper-portfolio"])
fills_router = APIRouter(prefix="/api/paper-fills", tags=["paper-portfolio"])
_ID = r"^[0-9a-f]{32}$"
Money = Field(max_digits=12, decimal_places=2)


class AccountBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, max_length=60)
    starting_cash: Decimal = Money
    slippage_bps: Decimal = Field(max_digits=6, decimal_places=2)
    commission_per_order: Decimal = Money


class AccountSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, max_length=60)
    starting_cash: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)
    slippage_bps: Optional[Decimal] = Field(default=None, max_digits=6, decimal_places=2)
    commission_per_order: Optional[Decimal] = Field(default=None, max_digits=12, decimal_places=2)


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=10)
    side: Literal["BUY", "SELL"]
    quantity: StrictInt = Field(ge=1, le=1_000_000)
    origin: Literal["MANUAL"] = "MANUAL"
    strategy_version_id: Optional[str] = Field(default=None, pattern=_ID)


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _err(exc: X.PaperError) -> JSONResponse:
    return JSONResponse({"status": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status)


def _ok(payload, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


@portfolio_router.get("")
def get_portfolio() -> JSONResponse:
    return _ok(P.view())


@portfolio_router.post("/account")
def post_account(body: AccountBody) -> JSONResponse:
    try:
        X.create_account(body.name, body.starting_cash, body.slippage_bps, body.commission_per_order)
        return _ok(P.view(), 201)
    except X.PaperError as exc:
        return _err(exc)


@portfolio_router.post("/account/settings")
def post_account_settings(body: AccountSettingsBody) -> JSONResponse:
    try:
        X.update_account(body.name, body.starting_cash, body.slippage_bps, body.commission_per_order)
        return _ok(P.view())
    except X.PaperError as exc:
        return _err(exc)


@orders_router.get("")
def get_orders(status: Optional[Literal["PENDING", "FILLED", "CANCELLED", "REJECTED"]] = None,
               limit: int = Query(default=200, ge=1, le=1000)) -> JSONResponse:
    store = PaperStore()
    acct = store.account()
    return _ok({"orders": store.orders(acct["account_id"], status, limit) if acct else [], "label": P.LABEL})


@orders_router.post("")
def post_order(body: OrderBody) -> JSONResponse:
    try:
        order = X.create_order(body.symbol, body.side, body.quantity, body.origin, body.strategy_version_id)
        return _ok({"order": order, "portfolio": P.view()}, 201)
    except X.PaperError as exc:
        return _err(exc)


@orders_router.post("/preview")
def post_preview(body: OrderBody) -> JSONResponse:
    try:
        return _ok({"preview": X.preview_order(body.symbol, body.side, body.quantity, body.origin, body.strategy_version_id)})
    except X.PaperError as exc:
        return _err(exc)


@orders_router.post("/process")
def post_process(body: Optional[EmptyBody] = None) -> JSONResponse:
    return _ok({"process": X.process_pending(), "portfolio": P.view()})


@orders_router.post("/{order_id}/cancel")
def post_cancel(order_id: str, body: Optional[EmptyBody] = None) -> JSONResponse:
    if len(order_id) != 32 or any(c not in "0123456789abcdef" for c in order_id):
        return JSONResponse({"status": "NOT_FOUND", "message": "No paper order with that id."}, status_code=404)
    try:
        return _ok({"order": X.cancel_order(order_id), "portfolio": P.view()})
    except X.PaperError as exc:
        return _err(exc)


@fills_router.get("")
def get_fills(limit: int = Query(default=500, ge=1, le=5000)) -> JSONResponse:
    store = PaperStore()
    acct = store.account()
    rows = [{k: v for k, v in f.items() if k != "seq"} for f in reversed(store.fills(acct["account_id"]))][:limit] if acct else []
    return _ok({"fills": rows, "label": P.LABEL})
