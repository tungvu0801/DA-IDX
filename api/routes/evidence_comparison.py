"""
api/routes/evidence_comparison.py — Stage 3.5 EVIDENCE COMPARISON API (read-only, stored evidence only).

GET  /api/evidence-comparison/config   saved versions (with how much stored evidence each has) and labels — local reads
POST /api/evidence-comparison/view     {"strategy_version_id": "...", "backtest_run_id": null, "forward_journal_id": null}
                                       -> ONE version's selected stored backtest run and selected stored forward journal,
                                          side by side, with descriptive differences for compatible measures only

Both routes only read stored rows. Nothing is written or persisted, no backtest is re-run, no forward session is
captured or reconstructed, no market data / research / event / broker / Claude call is made, and nothing is scored,
ranked or recommended. A run or journal of another strategy version is refused (EVIDENCE_VERSION_MISMATCH).
"""
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from comparison import view as V

router = APIRouter(prefix="/api/evidence-comparison", tags=["evidence-comparison"])
_ID = r"^[0-9a-f]{32}$"


class ViewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_version_id: str = Field(pattern=_ID)
    backtest_run_id: Optional[str] = Field(default=None, pattern=_ID)
    forward_journal_id: Optional[str] = Field(default=None, pattern=_ID)


@router.get("/config")
def get_config() -> JSONResponse:
    return JSONResponse(V.public_config())


@router.post("/view")
def post_view(body: ViewBody) -> JSONResponse:
    try:
        return JSONResponse(V.view(body.strategy_version_id, body.backtest_run_id, body.forward_journal_id))
    except V.CompareError as exc:
        return JSONResponse({"status": exc.code, "message": exc.message}, status_code=exc.status)
