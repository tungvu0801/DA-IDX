"""
api/routes/explanation_history.py — Stage 3.9 OPTIONAL AI explanation history (local, append-only, OFF by default).

GET  /api/explanation-history/settings    is history saving on?
POST /api/explanation-history/settings    {"enabled": true|false} — the only way history starts or stops saving
GET  /api/explanation-history             newest first; kind=all|fit|evidence, origin=all|local|claude, optional
                                          strategy_version_id / symbol, limit <= 100, before=<cursor from "next">
GET  /api/explanation-history/{id}        one stored explanation exactly as saved

Reading history never calls Claude, never regenerates text and never touches the AI cache. Saving happens only in
api/routes/ai_explain.py after Stage 3.8 accepted an explanation. There is no update or delete endpoint.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ai_explain import history as H

router = APIRouter(prefix="/api/explanation-history", tags=["explanation-history"])
NOTE = ("Optional, local, append-only record of explanations the app showed you and the deterministic view each one "
        "explained. It is not evidence: nothing in Strategy Fit, backtests, the forward journal or Evidence reads it.")


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.get("/settings")
def get_settings() -> JSONResponse:
    return JSONResponse({"enabled": H.enabled(), "note": NOTE})


@router.post("/settings")
def post_settings(body: SettingsBody) -> JSONResponse:
    return JSONResponse({"enabled": H.set_enabled(body.enabled), "note": NOTE})


@router.get("")
def get_history(kind: Literal["all", "fit", "evidence"] = "all", origin: Literal["all", "local", "claude"] = "all",
                strategy_version_id: Optional[str] = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
                symbol: Optional[str] = Query(default=None, pattern=r"^[A-Za-z][A-Za-z0-9.\-]{0,11}$"),
                limit: int = Query(default=50, ge=1, le=H.PAGE_MAX),
                before: Optional[str] = Query(default=None, max_length=80, pattern=r"^[0-9T:+\-.]{10,40}\|[0-9a-f]{32}$")
                ) -> JSONResponse:
    out = H.list_recent(kind, origin, strategy_version_id, symbol, limit, before)
    return JSONResponse({**out, "enabled": H.enabled()})


@router.get("/{history_id}")
def get_item(history_id: str) -> JSONResponse:
    if len(history_id) != 32 or any(c not in "0123456789abcdef" for c in history_id):
        return JSONResponse({"status": "NOT_FOUND", "message": "No saved explanation with that id."}, status_code=404)
    item = H.get(history_id)
    if item is None:
        return JSONResponse({"status": "NOT_FOUND", "message": "No saved explanation with that id."}, status_code=404)
    return JSONResponse(item)
