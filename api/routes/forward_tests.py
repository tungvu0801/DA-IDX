"""
api/routes/forward_tests.py — Stage 3.3 FORWARD-TEST SIGNAL JOURNAL API (user-triggered observation only).

GET  /api/forward-tests?strategy_id=&version_number=     journals (+ forward eligibility of that version)
POST /api/forward-tests                                  start a journal for a saved version (evaluates nothing)
GET  /api/forward-tests/{journal_id}                     the STORED journal: status, continuity, summary, shadow
                                                         states, latest session, timeline
POST /api/forward-tests/{journal_id}/preflight           checks only — writes nothing
POST /api/forward-tests/{journal_id}/record              record the latest COMPLETED close (explicit action; one
                                                         session; ALREADY_RECORDED returns the stored record)
GET  /api/forward-tests/{journal_id}/sessions            captured + missed sessions (compact timeline)
GET  /api/forward-tests/{journal_id}/sessions/{date}     one stored session: snapshots, traces, reference fills
POST /api/forward-tests/{journal_id}/archive             stop future captures (nothing is deleted)

No route places, previews or simulates an order, sizes anything, calls Claude, runs on a schedule, or backfills a past
forward session. Stored sessions are never recomputed to be viewed.
"""
import re
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backtest.store import get_backtest_store
from forward import journal as J
from forward.store import ForwardError, get_forward_store

router = APIRouter(prefix="/api/forward-tests", tags=["forward-tests"])
_ID = re.compile(r"^[0-9a-f]{32}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    version_number: int = Field(ge=1)


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _err(exc: ForwardError) -> JSONResponse:
    return JSONResponse({"status": exc.code, "message": exc.message, "detail": exc.detail}, status_code=exc.status)


def _missing() -> JSONResponse:
    return JSONResponse({"status": "NOT_FOUND", "message": "Forward journal not found."}, status_code=404)


def _stores():
    return get_forward_store(), get_backtest_store()


@router.get("")
def list_journals(strategy_id: Optional[str] = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
                  version_number: Optional[int] = Query(default=None, ge=1)) -> JSONResponse:
    fs, bs = _stores()
    return JSONResponse(J.list_view(fs, bs, strategy_id, version_number))


@router.post("", status_code=201)
def create_journal(body: CreateBody) -> JSONResponse:
    fs, bs = _stores()
    try:
        j = J.create_journal(fs, bs, body.strategy_id, body.version_number)
    except ForwardError as exc:
        return _err(exc)
    return JSONResponse(J.journal_view(fs, j["journal_id"]), status_code=201)


@router.get("/{journal_id}")
def get_journal(journal_id: str) -> JSONResponse:
    if not _ID.match(journal_id):
        return _missing()
    try:
        return JSONResponse(J.journal_view(get_forward_store(), journal_id))
    except ForwardError as exc:
        return _err(exc)


@router.post("/{journal_id}/preflight")
def post_preflight(journal_id: str, body: Optional[EmptyBody] = None) -> JSONResponse:
    if not _ID.match(journal_id):
        return _missing()
    fs, bs = _stores()
    try:
        return JSONResponse(J.preflight(fs, bs, journal_id))
    except ForwardError as exc:
        return _err(exc)


@router.post("/{journal_id}/record")
def post_record(journal_id: str, body: Optional[EmptyBody] = None) -> JSONResponse:
    if not _ID.match(journal_id):
        return _missing()
    fs, bs = _stores()
    try:
        return JSONResponse(J.record(fs, bs, journal_id))
    except ForwardError as exc:
        return _err(exc)


@router.get("/{journal_id}/sessions")
def get_sessions(journal_id: str) -> JSONResponse:
    if not _ID.match(journal_id):
        return _missing()
    try:
        v = J.journal_view(get_forward_store(), journal_id, include_latest=False)
    except ForwardError as exc:
        return _err(exc)
    return JSONResponse({"journal_id": journal_id, "timeline": v["timeline"], "summary": v["summary"], "note": v["note"]})


@router.get("/{journal_id}/sessions/{session_date}")
def get_session(journal_id: str, session_date: str) -> JSONResponse:
    if not _ID.match(journal_id) or not _DATE.match(session_date):
        return _missing()
    try:
        return JSONResponse(J.session_view(get_forward_store(), journal_id, session_date))
    except ForwardError as exc:
        return _err(exc)


@router.post("/{journal_id}/archive")
def post_archive(journal_id: str, body: Optional[EmptyBody] = None) -> JSONResponse:
    if not _ID.match(journal_id):
        return _missing()
    fs = get_forward_store()
    try:
        J.archive_journal(fs, journal_id)
        return JSONResponse(J.journal_view(fs, journal_id))
    except ForwardError as exc:
        return _err(exc)
