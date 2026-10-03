"""
api/routes/alpaca_paper.py — Stage 4.6A ALPACA PAPER · READ ONLY (paper/alpaca_view.py). No broker action exists here.

GET  /api/alpaca-paper/status    configured / not configured (names only), last refresh state — 0 broker calls
POST /api/alpaca-paper/refresh   the explicit "Refresh Alpaca Paper" action: at most 4 read-only paper requests, then the
                                 payload (account, positions, recent orders, recent fills, comparison with the local
                                 simulator, warnings, timings, source)
GET  /api/alpaca-paper/view      the last refresh from memory compared with the current local simulator — 0 broker calls

Strict: the refresh body must be empty ({} or none) and no query parameter is accepted, so the browser can never supply a
credential, a mode, a URL, a path, a method or a symbol expression. Never returns a secret or a full account number.
"""
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from paper import alpaca_view as AV

router = APIRouter(prefix="/api/alpaca-paper", tags=["alpaca-paper"])


class RefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _no_params(request: Request) -> Optional[JSONResponse]:
    if request.query_params:
        return JSONResponse({"status": "INVALID_REQUEST", "message": "This endpoint takes no parameters."}, status_code=422)
    return None


@router.get("/status")
def get_status(request: Request) -> JSONResponse:
    return _no_params(request) or JSONResponse(AV.status())


@router.post("/refresh")
def post_refresh(request: Request, body: Optional[RefreshBody] = None) -> JSONResponse:
    return _no_params(request) or JSONResponse(AV.refresh())


@router.get("/view")
def get_view(request: Request) -> JSONResponse:
    return _no_params(request) or JSONResponse(AV.view())
