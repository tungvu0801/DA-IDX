"""
api/routes/alpaca_paper_orders.py — Stage 4.6B manual Alpaca PAPER orders (paper/alpaca_orders.py; DESIGN_46B_FINAL §6.1).

GET  /api/alpaca-paper-orders/settings                 0 broker requests
POST /api/alpaca-paper-orders/settings/link-account    {confirm: true}                              2 GET, 0 POST
POST /api/alpaca-paper-orders/settings/relink-account  {confirm: true, current_account_masked}      2 GET, 0 POST
POST /api/alpaca-paper-orders/settings/enable          {confirm: true}                              2 GET, 0 POST
POST /api/alpaca-paper-orders/settings/disable         {}                                           0
GET  /api/alpaca-paper-orders                          the orders (0 requests)
POST /api/alpaca-paper-orders/preview                  {symbol, side, quantity}                     ≤ 5 GET + ≤ 1 market-data, 0 POST
POST /api/alpaca-paper-orders/confirm                  {preview_id, preview_hash, confirm: true}    ≤ 4 GET + 1 POST + ≤ 1 lookup
POST /api/alpaca-paper-orders/{intent_id}/retry        {preview_hash, confirm: true}                1 lookup + ≤ 4 GET + ≤ 1 POST + ≤ 1 lookup
POST /api/alpaca-paper-orders/{intent_id}/abandon      {preview_hash, confirm: true}                1 account GET + 1 lookup
POST /api/alpaca-paper-orders/status                   {}                                           1 account GET + ≤ 20 lookups

Every POST: `Content-Type: application/json`, header `X-Stock-Agent-Intent: paper-order`, Host = 127.0.0.1:PORT or
localhost:PORT of this server, Origin (when present) the same, loopback client only, no query parameters, strict bodies
(unknown fields → 422). Confirm / retry / abandon carry NO order fields — the server uses only the stored preview.
Never returned: credentials, headers, full account numbers, raw broker text.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from paper import alpaca_orders as O

router = APIRouter(prefix="/api/alpaca-paper-orders", tags=["alpaca-paper-orders"])
INTENT_HEADER = ("x-stock-agent-intent", "paper-order")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_HASH = r"^[0-9a-f]{64}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyBody(Strict):
    pass


class ConfirmFlag(Strict):
    confirm: Literal[True]


class RelinkBody(Strict):
    confirm: Literal[True]
    current_account_masked: str = Field(min_length=1, max_length=12)


class PreviewBody(Strict):
    symbol: str = Field(min_length=1, max_length=10)
    side: Literal["BUY", "SELL"]
    quantity: StrictInt


class ConfirmBody(Strict):
    preview_id: StrictInt
    preview_hash: str = Field(pattern=_HASH)
    confirm: Literal[True]


class IntentActionBody(Strict):
    preview_hash: str = Field(pattern=_HASH)
    confirm: Literal[True]


def _refuse(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"status": code, "message": message}, status_code=status)


def _gate(request: Request, post: bool = True) -> Optional[JSONResponse]:
    """DESIGN_46B_FINAL §9 HTTP gates (DNS-rebinding and cross-site protection for the first broker-state endpoints)."""
    if request.query_params:
        return _refuse("INVALID_REQUEST", "This endpoint takes no query parameters.", 422)
    client = request.client.host if request.client else ""
    server = request.scope.get("server") or (None, None)
    port = server[1]
    if client not in _LOOPBACK:
        return _refuse("FORBIDDEN", "Paper order endpoints accept requests from this computer only.", 403)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if request.headers.get("host") not in allowed_hosts:
        return _refuse("FORBIDDEN", "Unexpected Host header.", 403)
    origin = request.headers.get("origin")
    if origin is not None and origin not in {f"http://{h}" for h in allowed_hosts}:
        return _refuse("FORBIDDEN", "Unexpected Origin.", 403)
    if post:
        if (request.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
            return _refuse("INVALID_REQUEST", "JSON body required.", 422)
        if request.headers.get(INTENT_HEADER[0]) != INTENT_HEADER[1]:
            return _refuse("FORBIDDEN", "Missing X-Stock-Agent-Intent header.", 403)
    return None


def _run(request: Request, fn, *args, post: bool = True) -> JSONResponse:
    refused = _gate(request, post)
    if refused is not None:
        return refused
    try:
        return JSONResponse(fn(*args))
    except O.OrderError as exc:
        return JSONResponse({"status": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status)


@router.get("/settings")
def get_settings(request: Request) -> JSONResponse:
    return _run(request, O.settings_view, post=False)


@router.post("/settings/link-account")
def post_link(request: Request, body: ConfirmFlag) -> JSONResponse:
    return _run(request, O.link_account)


@router.post("/settings/relink-account")
def post_relink(request: Request, body: RelinkBody) -> JSONResponse:
    return _run(request, O.relink_account, body.current_account_masked)


@router.post("/settings/enable")
def post_enable(request: Request, body: ConfirmFlag) -> JSONResponse:
    return _run(request, O.enable)


@router.post("/settings/disable")
def post_disable(request: Request, body: EmptyBody) -> JSONResponse:
    return _run(request, O.disable)


@router.get("")
def get_list(request: Request) -> JSONResponse:
    return _run(request, O.list_intents, post=False)


@router.post("/preview")
def post_preview(request: Request, body: PreviewBody) -> JSONResponse:
    return _run(request, O.preview, body.symbol, body.side, body.quantity)


@router.post("/confirm")
def post_confirm(request: Request, body: ConfirmBody) -> JSONResponse:
    return _run(request, lambda: O.confirm(body.preview_id, body.preview_hash))


@router.post("/{intent_id}/retry")
def post_retry(request: Request, intent_id: int, body: IntentActionBody) -> JSONResponse:
    return _run(request, lambda: O.retry(intent_id, body.preview_hash))


@router.post("/{intent_id}/abandon")
def post_abandon(request: Request, intent_id: int, body: IntentActionBody) -> JSONResponse:
    return _run(request, lambda: O.abandon(intent_id, body.preview_hash))


@router.post("/status")
def post_status(request: Request, body: EmptyBody) -> JSONResponse:
    return _run(request, O.check_status)
