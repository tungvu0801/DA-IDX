"""
api/routes/daily_brief_delivery.py — Stage 4.3 OPT-IN DAILY BRIEF DESKTOP DELIVERY (notifications/delivery.py).

GET  /api/brief-delivery            settings (OFF by default), last delivery, recent history (20)
GET  /api/brief-delivery/history    recent delivery decisions (limit <= 20)
POST /api/brief-delivery/settings   {"enabled"?: bool, "notify_only_if_activity"?: bool}
POST /api/brief-delivery/test       send the fixed server-side test notification (recorded as TEST)

Stage 4.4: every response carries "identity" (windows.capability(): click-to-open support, the fixed local Daily Brief
URL, branding state, app id, display name). Turning notifications ON registers the per-user notification identity
(notifications/identity.py); turning them OFF removes it. Clicking a notification only opens the local Daily Brief.

Strict bodies. No endpoint accepts a title, body or any notification text: real deliveries carry only the Stage 4.2
Daily Brief counts, and the test uses fixed server text. There is no "send the current brief now" endpoint — real
deliveries happen only in the Stage 3.7 scheduler's after-close check. No market data, no Claude, no broker.
"""
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from notifications import delivery as D
from notifications import identity as ID
from notifications import windows as W

D.register()                                              # after-close extension, ordered after the saved-scan checks
router = APIRouter(prefix="/api/brief-delivery", tags=["daily-brief-delivery"])


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: Optional[bool] = None
    notify_only_if_activity: Optional[bool] = None


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _with_identity(payload: dict) -> dict:
    return {**payload, "identity": W.capability()}


@router.get("")
def get_status() -> JSONResponse:
    return JSONResponse(_with_identity(D.status()))


@router.get("/history")
def get_history(limit: int = Query(default=20, ge=1, le=20)) -> JSONResponse:
    return JSONResponse({"deliveries": D.DeliveryStore().history(limit), "note": D.NOTE})


@router.post("/settings")
def post_settings(body: SettingsBody) -> JSONResponse:
    if body.enabled is None and body.notify_only_if_activity is None:
        return JSONResponse({"status": "NOTHING_TO_CHANGE", "message": "Send enabled and / or notify_only_if_activity."},
                            status_code=422)
    out = D.configure(body.enabled, body.notify_only_if_activity)
    if body.enabled is not None:                           # Stage 4.4: the identity exists only while notifications are ON
        ID.register() if body.enabled else ID.unregister()
        W.refresh_identity()
    return JSONResponse(_with_identity(out))


@router.post("/test")
def post_test(body: Optional[EmptyBody] = None) -> JSONResponse:
    try:
        return JSONResponse({"test": D.send_test(), "delivery": _with_identity(D.status())})
    except D.DeliveryError as exc:
        return JSONResponse({"status": exc.code, "message": exc.message}, status_code=exc.status)
