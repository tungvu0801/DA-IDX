"""
api/routes/research_workflow.py — the research workflow (research_flow/): shortlist-before-LLM, cached, budgeted,
observable. Research only — nothing here can place, preview or prepare an order.

POST /api/research-workflow/shortlist   {run_id, symbols?, settings?}            the deterministic shortlist + research states (0 model calls)
POST /api/research-workflow/research    {run_id, symbols?, refresh?, settings?}  research the shortlist: cache first, then <= max_llm_calls_per_run
                                                                                 gated Claude calls (user-triggered only; never on load, never scheduled)
GET  /api/research-workflow/results/{run_id}                                     stored results and batch reports of a run (0 model calls)

Strict bodies. Responses carry no credential and no recommendation field; a result is the validated structured note or
a status explaining why it is unavailable (cache stale, budget skipped, provider failed, malformed reply withheld).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from fit import readonly as RO
from research_flow import contracts as C
from research_flow import orchestrator as O
from research_flow.store import ResearchStore

router = APIRouter(prefix="/api/research-workflow", tags=["research-workflow"])
_ID = r"^[0-9a-f]{32}$"
_SYM = r"^[A-Za-z][A-Za-z0-9.\-]{0,11}$"
NOW_FN = None                                                # tests: a fixed clock
PROVIDER_FN = None                                           # tests: a fake provider (default: the shared get_provider, resolved per call)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsBody(Strict):
    top_n: Optional[StrictInt] = None
    max_symbols_per_research_run: Optional[StrictInt] = None
    max_llm_calls_per_run: Optional[StrictInt] = None
    max_refresh_calls_per_symbol_per_window: Optional[StrictInt] = None
    held_rank_change_min: Optional[StrictInt] = None
    mover_rank_change_min: Optional[StrictInt] = None


class ShortlistBody(Strict):
    run_id: str = Field(pattern=_ID)
    symbols: List[str] = Field(default_factory=list, max_length=50)
    settings: Optional[SettingsBody] = None


class ResearchBody(ShortlistBody):
    refresh: List[str] = Field(default_factory=list, max_length=50)


def _err(exc: O.WorkflowError) -> JSONResponse:
    return JSONResponse({"status": exc.code, "message": exc.message}, status_code=exc.status)


def _settings(body) -> dict:
    return {k: v for k, v in (body.settings.model_dump() if body.settings else {}).items() if v is not None}


def _now():
    return NOW_FN() if NOW_FN else None


@router.post("/shortlist")
def post_shortlist(body: ShortlistBody) -> JSONResponse:
    try:
        out = O.status_for_run(body.run_id, user_symbols=body.symbols, config_overrides=_settings(body), path=RO.db_path(), now=_now())
    except O.WorkflowError as exc:
        return _err(exc)
    return JSONResponse({**out, "states": list(O.STATES), "result_fields": C.result_fields(), "claude_calls": 0})


@router.post("/research")
def post_research(body: ResearchBody) -> JSONResponse:
    try:
        report = O.research_run(body.run_id, user_symbols=body.symbols, refresh=body.refresh, config_overrides=_settings(body),
                                provider_fn=PROVIDER_FN, path=RO.db_path(), now=_now())
        status = O.status_for_run(body.run_id, user_symbols=body.symbols, config_overrides=_settings(body), path=RO.db_path(), now=_now())
    except O.WorkflowError as exc:
        return _err(exc)
    return JSONResponse({"report": report, **status, "claude_calls": report["llm_calls"]})


@router.get("/results/{run_id}")
def get_results(run_id: str) -> JSONResponse:
    if len(run_id) != 32:
        return JSONResponse({"status": "NOT_FOUND", "message": "No such rotation run."}, status_code=404)
    store = ResearchStore(RO.db_path())
    return JSONResponse({"run_id": run_id, "results": store.results_for_run(run_id), "batches": store.batches(run_id), "claude_calls": 0})
