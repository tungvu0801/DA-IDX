"""
api/routes/strategy_fit.py — Stage 3.4 STRATEGY FIT API (read-only, on demand).

GET  /api/strategy-fit/config      symbol sources (saved strategy universes, watchlist file) and labels — local reads only
POST /api/strategy-fit/evaluate    {"symbol": "MU", "include_old_versions": false} -> current rule fit of every saved
                                   version for that symbol at the latest completed daily close, with stored evidence

Strategy Fit compares current data with saved rules. It writes nothing (no evidence table, no cached dataset), never
calls Claude, never reads a broker account, never places or previews an order, and does not rank, score or recommend
strategies. Results are sorted by strategy name and version only.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ai_explain.service import FIT_RECORD
from fit import current as FC

router = APIRouter(prefix="/api/strategy-fit", tags=["strategy-fit"])


class EvaluateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=12)
    include_old_versions: bool = False


@router.get("/config")
def get_config() -> JSONResponse:
    return JSONResponse(FC.public_config())


@router.post("/evaluate")
def post_evaluate(body: EvaluateBody) -> JSONResponse:
    try:
        result = FC.evaluate(body.symbol, body.include_old_versions)
    except FC.FitError as exc:
        return JSONResponse({"status": exc.code, "message": exc.message}, status_code=exc.status)
    FIT_RECORD.remember(result)          # Stage 3.8: "Explain this setup" reads this exact result (memory only; no recompute)
    return JSONResponse(result)
