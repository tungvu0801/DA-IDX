"""
api/routes/forward_automation.py — Stage 3.7 OPT-IN automatic after-close forward capture (settings + status).

GET  /api/forward-automation          whether automatic capture is on, the check time (New York), next / last check
POST /api/forward-automation          {"enabled": true, "capture_time_et": "00:15"} — turn it on / off (default OFF)
POST /api/forward-automation/check    "Check automation now" (only while it is on): the same check, immediately

Automatic capture runs the existing forward-journal "Record latest completed close" workflow for active journals. It
never places, previews or simulates an order, never calls Claude, never backfills a missed session, and runs only
while this server is running. A separate prefix keeps the Stage 3.3 /api/forward-tests endpoint set unchanged.
"""
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from forward import automation as A

router = APIRouter(prefix="/api/forward-automation", tags=["forward-automation"])


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    capture_time_et: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("")
def get_status() -> JSONResponse:
    return JSONResponse(A.get_scheduler().status())


@router.post("")
def post_settings(body: SettingsBody) -> JSONResponse:
    return JSONResponse(A.configure(body.enabled, body.capture_time_et))


@router.post("/check")
def post_check(body: Optional[EmptyBody] = None) -> JSONResponse:
    sch = A.get_scheduler()
    if not sch.store_fn().settings()["enabled"]:
        return JSONResponse({"status": "AUTOMATION_OFF", "message": "Automatic capture is off. Turn it on first, or use "
                             "Record latest completed close in a journal."}, status_code=409)
    return JSONResponse({"check": sch.check_now(), "automation": sch.status()})
