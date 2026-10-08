"""
api/routes/rotation_diagnostics.py — Stage 5.1 ATTRIBUTION & BENCHMARK DIAGNOSTICS (rotation_diagnostics/). RESEARCH ONLY.

GET  /api/rotation-diagnostics/config                  conventions, flag thresholds, default sector map, completed campaigns (0 requests)
POST /api/rotation-diagnostics/run                     {campaign_id, config_hashes?, sector_map?, cost_points?}: the diagnostics of a stored
                                                       campaign — 0 broker / gateway / model requests, <= 1 market-data request; persisted
GET  /api/rotation-diagnostics/runs · /runs/{id} · /runs/{id}/benchmarks · /runs/{id}/attribution · /runs/{id}/ablation · /runs/{id}/windows
     · /runs/{id}/regimes · /runs/{id}/scorecard

Nothing here previews, prepares or sends an order, activates or promotes a configuration, or carries a draft for any form;
no broker import; no path contains "order", "trade", "backtest", "paper", "execute", "broker" or "deploy". Strict bodies.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from fit import readonly as RO
from rotation_campaign.store import ModelCampaignStore
from rotation_diagnostics import FLAGS_VERSION
from rotation_diagnostics import engine as DE
from rotation_diagnostics import flags as FL
from rotation_diagnostics.store import RotationDiagnosticStore

router = APIRouter(prefix="/api/rotation-diagnostics", tags=["rotation-diagnostics"])
LABEL = "ATTRIBUTION & BENCHMARK DIAGNOSTICS — RESEARCH ONLY — NOTHING IS TRADED, ACTIVATED OR PROMOTED"
NOTE = ("For configurations already evaluated by a stored campaign: equal-weight and buy-and-hold universe benchmarks under the same replay "
        "conventions, symbol and sector attribution, leave-one-out and leave-sector-out ablations, cost-adjusted excess, drawdown comparison, window "
        "and regime diagnostics, documented research flags and a PASS / WARN / FAIL scorecard. Static universe — not a forecast, not an order path.")
_ID, _HASH = r"^[0-9a-f]{32}$", r"^[0-9a-f]{64}$"
NOW_FN = None
FETCH: dict = {"fetch_fn": None, "client": None, "cache": None}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunBody(Strict):
    campaign_id: str = Field(pattern=_ID)
    config_hashes: Optional[List[str]] = Field(default=None, max_length=10)
    sector_map: Optional[Dict[str, str]] = Field(default=None)
    cost_points: Optional[List[List[str]]] = Field(default=None, max_length=8)


def _err(code: str, message: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"status": code, "message": message}, status_code=status)


def _now() -> datetime:
    return (NOW_FN or (lambda: datetime.now(timezone.utc)))()


def _store() -> RotationDiagnosticStore:
    return RotationDiagnosticStore(RO.db_path())


def _run_out(run: dict) -> dict:
    keys = ("diag_id", "status", "failure_code", "failure_detail", "diag_hash", "campaign_id", "campaign_hash", "universe_hash", "sector_map_hash", "engine_version", "flags_version",
            "run_at", "completed_at", "start_date", "end_date", "n_windows", "n_configs", "n_evaluations", "n_cache_hits", "runtime_s", "data_hash", "result_hash",
            "market_data_requests", "universe_note")
    return {**{k: run.get(k) for k in keys}, "label": LABEL}


@router.get("/config")
def get_config() -> JSONResponse:
    camps = [{k: c.get(k) for k in ("campaign_id", "status", "campaign_hash", "n_candidates", "n_finalists", "run_at")} for c in ModelCampaignStore(RO.db_path()).campaigns(20)
             if c.get("status") != "FAILED"]
    return JSONResponse({"label": LABEL, "note": NOTE, "flags": FL.describe(), "flags_version": FLAGS_VERSION, "cost_points": [list(c) for c in DE.COST_POINTS],
                         "default_sector_map": DE.DEFAULT_SECTOR_MAP, "benchmarks": DE.BM.CONVENTIONS, "campaigns": camps, "universe_note": DE.UNIVERSE_NOTE})


@router.post("/run")
def post_run(body: RunBody) -> JSONResponse:
    try:
        out = DE.run_diagnostics(_store(), body.model_dump(), now=_now(), path=RO.db_path(), **FETCH)
    except DE.DiagnosticError as exc:
        return _err(exc.code, exc.message, exc.status)
    cfgs = [{"config_hash": c["config_hash"], "label": c["label"], "role": c["role"], "flags": c["flags"], "scorecard": c["scorecard"],
             "stitched": (c["summary"].get("stitched") if c["summary"].get("status") == "COMPLETED" else None)} for c in out["configs"]]
    return JSONResponse({"run": _run_out(out["run"]), "definition": out["definition"], "windows": out["windows"], "configs": cfgs,
                         "benchmarks": [{k: v for k, v in b.items() if k != "windows"} for b in out["benchmarks"]]})


@router.get("/runs")
def get_runs(limit: int = 50) -> JSONResponse:
    return JSONResponse({"runs": [_run_out(r) for r in _store().runs(max(1, min(int(limit), 200)))]})


def _run_or_404(did: str):
    st = _store()
    run = st.run(did) if len(did) == 32 else None
    return st, run


@router.get("/runs/{did}")
def get_run(did: str) -> JSONResponse:
    st, run = _run_or_404(did)
    if run is None:
        return _err("NOT_FOUND", "No such diagnostic run.", 404)
    cards = st.scorecards(did)
    return JSONResponse({"run": _run_out(run), "definition": run["definition"], "configs": [{k: c[k] for k in ("config_hash", "label", "role", "flags", "scorecard")} for c in cards],
                         "conventions": DE.CONVENTIONS})


@router.get("/runs/{did}/benchmarks")
def get_benchmarks(did: str) -> JSONResponse:
    st, run = _run_or_404(did)
    if run is None:
        return _err("NOT_FOUND", "No such diagnostic run.", 404)
    cards = st.scorecards(did)
    return JSONResponse({"benchmarks": st.benchmarks(did), "strategies": [{"config_hash": c["config_hash"], "label": c["label"], "role": c["role"], "stitched": c["summary"].get("stitched"),
                                                                           "drawdown": c["summary"].get("drawdown"), "cost": c["summary"].get("cost")} for c in cards],
                         "universe_note": DE.UNIVERSE_NOTE})


@router.get("/runs/{did}/attribution")
def get_attribution(did: str) -> JSONResponse:
    st, run = _run_or_404(did)
    if run is None:
        return _err("NOT_FOUND", "No such diagnostic run.", 404)
    return JSONResponse({"symbols": st.symbols(did), "sectors": st.sectors(did),
                         "holdings": {c["config_hash"]: {"label": c["label"], **(c["summary"].get("attribution") or {}).get("holdings", {}),
                                                         "concentration": (c["summary"].get("attribution") or {}).get("concentration"),
                                                         "sector_concentration": (c["summary"].get("attribution") or {}).get("sector_concentration"),
                                                         "window_concentration": (c["summary"].get("attribution") or {}).get("window_concentration")} for c in st.scorecards(did)},
                         "sector_map_hash": run["sector_map_hash"]})


@router.get("/runs/{did}/ablation")
def get_ablation(did: str) -> JSONResponse:
    st, run = _run_or_404(did)
    if run is None:
        return _err("NOT_FOUND", "No such diagnostic run.", 404)
    return JSONResponse({"leave_one_out": st.leave_one_out(did), "leave_sector_out": st.leave_sector_out(did), "rule": FL.DESCRIPTIONS["DOMINANT_CONTRIBUTOR"]})


@router.get("/runs/{did}/windows")
def get_windows(did: str) -> JSONResponse:
    st, run = _run_or_404(did)
    if run is None:
        return _err("NOT_FOUND", "No such diagnostic run.", 404)
    return JSONResponse({"windows": st.windows(did), "summary": {c["config_hash"]: c["summary"].get("windows_summary") for c in st.scorecards(did)}})


@router.get("/runs/{did}/regimes")
def get_regimes(did: str) -> JSONResponse:
    st, run = _run_or_404(did)
    if run is None:
        return _err("NOT_FOUND", "No such diagnostic run.", 404)
    return JSONResponse({"regimes": {c["config_hash"]: {"label": c["label"], **(c["summary"].get("regimes") or {})} for c in st.scorecards(did)}})


@router.get("/runs/{did}/scorecard")
def get_scorecard(did: str) -> JSONResponse:
    st, run = _run_or_404(did)
    if run is None:
        return _err("NOT_FOUND", "No such diagnostic run.", 404)
    return JSONResponse({"scorecards": [{k: c[k] for k in ("config_hash", "label", "role", "flags", "scorecard")} for c in st.scorecards(did)], "flags": FL.describe(),
                         "universe_note": DE.UNIVERSE_NOTE})
