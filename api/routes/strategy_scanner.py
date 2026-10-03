"""
api/routes/strategy_scanner.py — Stage 4.0 deterministic, READ-ONLY Strategy Scanner (fit/scanner.py).

GET  /api/strategy-scanner/config   saved versions, server-side watchlist, holdings availability, limits, labels
POST /api/strategy-scanner/scan     ONE exact saved version against ONE list:
                                    {"strategy_version_id": "...", "source": "SAVED_UNIVERSE" | "WATCHLIST" | "HOLDINGS" | "CUSTOM",
                                     "symbols": [...] (CUSTOM only)}

Strict bodies (unknown fields are rejected). The server resolves SAVED_UNIVERSE / WATCHLIST / HOLDINGS itself and
re-validates a CUSTOM list; more than the limit is refused with TOO_MANY_SYMBOLS (never truncated). No database write,
no order, no Claude call, no schedule — a scan runs only when the user asks.
"""
import json
import time
from typing import List, Literal, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from fit import scanner as SC

router = APIRouter(prefix="/api/strategy-scanner", tags=["strategy-scanner"])


class ScanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_version_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    source: Literal["SAVED_UNIVERSE", "WATCHLIST", "HOLDINGS", "CUSTOM"]
    symbols: Optional[List[str]] = Field(default=None, max_length=1000)


@router.get("/config")
def get_config() -> JSONResponse:
    return JSONResponse(SC.public_config())


@router.post("/scan")
def post_scan(body: ScanBody) -> Response:
    if body.symbols and any(len(s) > 200 for s in body.symbols):
        return JSONResponse({"status": "INVALID_SYMBOL", "message": "A symbol entry is too long."}, status_code=422)
    try:
        out = SC.scan(body.strategy_version_id, body.source, body.symbols)
    except SC.ScanError as exc:
        return JSONResponse({"status": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status)
    t0 = time.perf_counter()
    json.dumps(out)                                        # measured once, then sent with the measurement included
    out["timings"]["serialization_s"] = round(time.perf_counter() - t0, 4)
    return Response(content=json.dumps(out), media_type="application/json")
