"""
api/routes/daily_brief.py — Stage 4.2 READ-ONLY DAILY BRIEF (brief/daily.py).

GET /api/daily-brief                 the brief of the latest session represented in stored data
GET /api/daily-brief?session=YYYY-MM-DD   that session's stored brief (an empty brief when nothing was stored for it)

Read only: no POST, no write, no read-state change, no market data, no Claude, no broker. Unknown parameters are refused.
"""
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from brief import daily as DB

router = APIRouter(prefix="/api/daily-brief", tags=["daily-brief"])


@router.get("")
def get_brief(request: Request, session: Optional[str] = Query(default=None, max_length=10)) -> JSONResponse:
    extra = sorted(set(request.query_params) - {"session"})
    if extra:
        return JSONResponse({"status": "UNKNOWN_PARAMETER", "message": f"Unknown parameter: {', '.join(extra)}. "
                             "Only session=YYYY-MM-DD is accepted."}, status_code=422)
    try:
        return JSONResponse(DB.build(session))
    except DB.BriefError as exc:
        return JSONResponse({"status": exc.code, "message": exc.message}, status_code=exc.status)
