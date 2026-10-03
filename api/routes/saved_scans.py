"""
api/routes/saved_scans.py — Stage 4.1 SAVED STRATEGY SCANS + RULES-MET CHANGE ALERTS (fit/saved_scans.py). In-app only.

GET  /api/saved-scans                    saved scans (active + archived) with the latest stored snapshot, unread alerts
POST /api/saved-scans                    save one scan: {"strategy_version_id", "source": SAVED_UNIVERSE | WATCHLIST | CUSTOM,
                                         "name"?, "alerts_enabled"?, "symbols": [...] (CUSTOM only)}
GET  /api/saved-scans/{id}               definition, recent snapshots and alert events
POST /api/saved-scans/{id}/check         check now: the Stage 4.0 scanner + snapshot (+ an event if RULES MET changed)
POST /api/saved-scans/{id}/settings      {"name"?, "alerts_enabled"?} (administrative fields only)
POST /api/saved-scans/{id}/archive       stop checks; snapshots and alerts are kept (no delete exists)
GET  /api/strategy-alerts                newest first; unread_only, limit <= 100, before=<cursor>
POST /api/strategy-alerts/{id}/read      mark one alert read
POST /api/strategy-alerts/read-all       mark every alert read

Strict bodies. A saved scan always references an existing immutable strategy version; no strategy JSON, SQL, prompt or
expression is accepted. No Claude call, no broker call, no order.
"""
from typing import List, Literal, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from fit import saved_scans as SS

SS.register()                                             # the after-close extension of the Stage 3.7 scheduler
router = APIRouter(prefix="/api/saved-scans", tags=["saved-scans"])
alerts_router = APIRouter(prefix="/api/strategy-alerts", tags=["strategy-alerts"])
_ID = r"^[0-9a-f]{32}$"


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_version_id: str = Field(pattern=_ID)
    source: Literal["SAVED_UNIVERSE", "WATCHLIST", "CUSTOM", "HOLDINGS"]
    name: Optional[str] = Field(default=None, max_length=200)
    alerts_enabled: bool = False
    symbols: Optional[List[str]] = Field(default=None, max_length=1000)


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, max_length=200)
    alerts_enabled: Optional[bool] = None


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _err(exc: SS.SavedScanError) -> JSONResponse:
    return JSONResponse({"status": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status)


def _id_ok(i: str) -> bool:
    return len(i) == 32 and all(c in "0123456789abcdef" for c in i)


def _missing() -> JSONResponse:
    return JSONResponse({"status": "NOT_FOUND", "message": "No saved scan with that id."}, status_code=404)


@router.get("")
def list_saved() -> JSONResponse:
    return JSONResponse(SS.list_scans())


@router.post("")
def create_saved(body: CreateBody) -> JSONResponse:
    if body.symbols and any(len(s) > 200 for s in body.symbols):
        return JSONResponse({"status": "INVALID_SYMBOL", "message": "A symbol entry is too long."}, status_code=422)
    try:
        return JSONResponse(SS.create(body.strategy_version_id, body.source, body.name, body.alerts_enabled, body.symbols),
                            status_code=201)
    except SS.SavedScanError as exc:
        return _err(exc)


@router.get("/{saved_scan_id}")
def get_saved(saved_scan_id: str) -> JSONResponse:
    if not _id_ok(saved_scan_id):
        return _missing()
    try:
        return JSONResponse(SS.get(saved_scan_id))
    except SS.SavedScanError as exc:
        return _err(exc)


@router.post("/{saved_scan_id}/check")
def check_saved(saved_scan_id: str, body: Optional[EmptyBody] = None) -> JSONResponse:
    if not _id_ok(saved_scan_id):
        return _missing()
    try:
        return JSONResponse({"check": SS.check(saved_scan_id, "MANUAL_CHECK"), "saved_scan": SS.get(saved_scan_id)})
    except SS.SavedScanError as exc:
        return _err(exc)


@router.post("/{saved_scan_id}/settings")
def settings_saved(saved_scan_id: str, body: SettingsBody) -> JSONResponse:
    if not _id_ok(saved_scan_id):
        return _missing()
    try:
        return JSONResponse(SS.settings(saved_scan_id, body.name, body.alerts_enabled))
    except SS.SavedScanError as exc:
        return _err(exc)


@router.post("/{saved_scan_id}/archive")
def archive_saved(saved_scan_id: str, body: Optional[EmptyBody] = None) -> JSONResponse:
    if not _id_ok(saved_scan_id):
        return _missing()
    try:
        return JSONResponse(SS.archive(saved_scan_id))
    except SS.SavedScanError as exc:
        return _err(exc)


@alerts_router.get("")
def list_alerts(unread_only: bool = False, limit: int = Query(default=30, ge=1, le=100),
                before: Optional[str] = Query(default=None, max_length=60, pattern=r"^\d{4}-\d{2}-\d{2}\|[0-9a-f]{32}$")) -> JSONResponse:
    store = SS.SavedScanStore()
    rows = store.events(unread_only=unread_only, limit=limit + 1, before=before)
    versions = SS._versions(store.path)
    items = []
    for e in rows[:limit]:
        v = versions.get(e["strategy_version_id"]) or {}
        label = f"{v.get('strategy_name', e['scan_name'])} v{v.get('version_number', '?')}"
        items.append({**e, "label": label, "is_current_version": v.get("is_current"), "texts": SS.alert_texts(e, label)})
    nxt = f"{rows[limit - 1]['decision_session']}|{rows[limit - 1]['alert_id']}" if len(rows) > limit else None
    return JSONResponse({"alerts": items, "next": nxt, "unread": store.unread(), "note": SS.NOTE})


@alerts_router.post("/read-all")
def read_all(body: Optional[EmptyBody] = None) -> JSONResponse:
    n = SS.SavedScanStore().mark_read(None)
    return JSONResponse({"marked": n, "unread": SS.SavedScanStore().unread()})


@alerts_router.post("/{alert_id}/read")
def read_one(alert_id: str, body: Optional[EmptyBody] = None) -> JSONResponse:
    if not _id_ok(alert_id):
        return JSONResponse({"status": "NOT_FOUND", "message": "No alert with that id."}, status_code=404)
    n = SS.SavedScanStore().mark_read(alert_id)
    return JSONResponse({"marked": n, "unread": SS.SavedScanStore().unread()})
