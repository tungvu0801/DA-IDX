"""
api/routes/signal_research.py — Stage 5.2 DETERMINISTIC SIGNAL RESEARCH (signal_research/). RESEARCH ONLY.

GET  /api/signal-research/config                    conventions, rules, flags, criteria, default sector map, completed campaigns (0 requests)
POST /api/signal-research/run                       {campaign_id, sector_map?, families?, caps?, cost_points?}: ONE bounded research run on a stored
                                                    campaign — 0 broker / gateway / model requests, <= 1 market-data request; persisted
GET  /api/signal-research/runs · /runs/{id} · /runs/{id}/ablation · /runs/{id}/sector · /runs/{id}/regime · /runs/{id}/combinations
     · /runs/{id}/benchmarks · /runs/{id}/scorecard

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
from signal_research import CRITERIA_VERSION, FLAGS_VERSION, MAX_VARIANTS, RULES_VERSION
from signal_research import engine as SE
from signal_research import evaluate as EV
from signal_research import variants as V
from signal_research.store import SignalResearchStore

router = APIRouter(prefix="/api/signal-research", tags=["signal-research"])
LABEL = "SIGNAL RESEARCH — RESEARCH ONLY — NOTHING IS TRADED, ACTIVATED OR PROMOTED"
NOTE = ("Deterministic research variants of the stored signal — factor ablation, sector-neutral ranking, sector caps and a regime exposure overlay — replayed on a "
        "stored campaign's windows against SPY, equal-weight and buy-and-hold with predeclared criteria. Static universe — not a forecast, not an order path.")
_ID = r"^[0-9a-f]{32}$"
NOW_FN = None
FETCH: dict = {"fetch_fn": None, "client": None, "cache": None}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunBody(Strict):
    campaign_id: str = Field(pattern=_ID)
    sector_map: Optional[Dict[str, str]] = Field(default=None)
    families: Optional[List[str]] = Field(default=None, max_length=6)
    caps: Optional[List[str]] = Field(default=None, max_length=3)
    cost_points: Optional[List[List[str]]] = Field(default=None, max_length=8)


def _err(code: str, message: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"status": code, "message": message}, status_code=status)


def _now() -> datetime:
    return (NOW_FN or (lambda: datetime.now(timezone.utc)))()


def _store() -> SignalResearchStore:
    return SignalResearchStore(RO.db_path())


def _run_out(run: dict) -> dict:
    keys = ("run_id", "status", "failure_code", "failure_detail", "run_hash", "campaign_id", "campaign_hash", "universe_hash", "sector_map_hash", "diagnostic_run_ids", "engine_version",
            "rules_version", "flags_version", "criteria_version", "run_at", "completed_at", "start_date", "end_date", "n_windows", "n_variants", "n_evaluations", "n_cache_hits", "runtime_s",
            "data_hash", "result_hash", "market_data_requests", "run_flags", "universe_note")
    return {**{k: run.get(k) for k in keys}, "label": LABEL}


def _variant_out(v: dict) -> dict:
    return {k: v.get(k) for k in ("config_hash", "label", "family", "position", "research", "flags", "research_flags", "criteria", "oos")} | {"weights": (v.get("config") or {}).get("weights")}


@router.get("/config")
def get_config() -> JSONResponse:
    camps = [{k: c.get(k) for k in ("campaign_id", "status", "campaign_hash", "n_candidates", "n_finalists", "run_at")} for c in ModelCampaignStore(RO.db_path()).campaigns(20)
             if c.get("status") != "FAILED"]
    return JSONResponse({"label": LABEL, "note": NOTE, "rules_version": RULES_VERSION, "flags_version": FLAGS_VERSION, "criteria_version": CRITERIA_VERSION, "max_variants": MAX_VARIANTS,
                         "families": list(V.FAMILIES), "rankings": list(V.RANKINGS), "overlays": list(V.OVERLAYS), "schedules": V.SCHEDULES, "sector_caps": list(V.SECTOR_CAPS),
                         "research": EV.describe(), "cost_points": [list(c) for c in SE.COST_POINTS], "default_sector_map": SE.DEFAULT_SECTOR_MAP, "conventions": SE.CONVENTIONS,
                         "campaigns": camps, "universe_note": SE.UNIVERSE_NOTE})


@router.post("/run")
def post_run(body: RunBody) -> JSONResponse:
    try:
        out = SE.run_research(_store(), body.model_dump(), now=_now(), path=RO.db_path(), **FETCH)
    except SE.ResearchError as exc:
        return _err(exc.code, exc.message, exc.status)
    return JSONResponse({"run": _run_out(out["run"]), "definition": {k: v for k, v in out["definition"].items() if k != "sector_map"}, "windows": out["windows"],
                         "variants": [_variant_out(v) | {"scorecard": v["scorecard"], "stitched": (v["summary"].get("stitched") if v["summary"].get("status") == "COMPLETED" else None)} for v in out["variants"]],
                         "factors": out["factors"], "sector_tests": out["sector_tests"], "regime_tests": out["regime_tests"], "combinations": out["combinations"],
                         "benchmarks": [{k: v for k, v in b.items() if k != "windows"} for b in out["benchmarks"]]})


@router.get("/runs")
def get_runs(limit: int = 50) -> JSONResponse:
    return JSONResponse({"runs": [_run_out(r) for r in _store().runs(max(1, min(int(limit), 200)))]})


def _run_or_404(rid: str):
    st = _store()
    run = st.run(rid) if len(rid) == 32 else None
    return st, run


@router.get("/runs/{rid}")
def get_run(rid: str) -> JSONResponse:
    st, run = _run_or_404(rid)
    if run is None:
        return _err("NOT_FOUND", "No such research run.", 404)
    return JSONResponse({"run": _run_out(run), "definition": {k: v for k, v in run["definition"].items() if k != "sector_map"}, "variants": [_variant_out(v) for v in st.variants(rid)],
                         "benchmarks": run["benchmarks"], "conventions": SE.CONVENTIONS})


@router.get("/runs/{rid}/ablation")
def get_ablation(rid: str) -> JSONResponse:
    st, run = _run_or_404(rid)
    if run is None:
        return _err("NOT_FOUND", "No such research run.", 404)
    vs = [v for v in st.variants(rid, with_summary=True) if v["family"] in ("baseline", "ablation", "control")]
    return JSONResponse({"factors": st.factors(rid), "rule": EV.FACTOR_RULE,
                         "variants": [_variant_out(v) | {"stitched": v["summary"].get("stitched"), "windows_summary": v["summary"].get("windows_summary"), "cost": v["summary"].get("cost"),
                                                         "concentration": (v["summary"].get("attribution") or {}).get("concentration"),
                                                         "sector_concentration": (v["summary"].get("attribution") or {}).get("sector_concentration"),
                                                         "holdings": (v["summary"].get("attribution") or {}).get("holdings"), "regimes": v["summary"].get("regimes")} for v in vs]})


@router.get("/runs/{rid}/sector")
def get_sector(rid: str) -> JSONResponse:
    st, run = _run_or_404(rid)
    if run is None:
        return _err("NOT_FOUND", "No such research run.", 404)
    return JSONResponse({"sector_tests": st.sector_tests(rid), "sector_map_hash": run["sector_map_hash"], "rule": SE.CONVENTIONS["sector_neutral"] + " · " + SE.CONVENTIONS["sector_cap"]})


@router.get("/runs/{rid}/regime")
def get_regime(rid: str) -> JSONResponse:
    st, run = _run_or_404(rid)
    if run is None:
        return _err("NOT_FOUND", "No such research run.", 404)
    return JSONResponse({"regime_tests": st.regime_tests(rid), "schedules": V.SCHEDULES, "rule": SE.CONVENTIONS["regime_overlay"]})


@router.get("/runs/{rid}/combinations")
def get_combinations(rid: str) -> JSONResponse:
    st, run = _run_or_404(rid)
    if run is None:
        return _err("NOT_FOUND", "No such research run.", 404)
    return JSONResponse({"combinations": st.combinations(rid), "rule": SE.CONVENTIONS["variant_set"]})


@router.get("/runs/{rid}/benchmarks")
def get_benchmarks(rid: str) -> JSONResponse:
    st, run = _run_or_404(rid)
    if run is None:
        return _err("NOT_FOUND", "No such research run.", 404)
    return JSONResponse({"benchmarks": run["benchmarks"], "comparisons": st.comparisons(rid), "universe_note": SE.UNIVERSE_NOTE})


@router.get("/runs/{rid}/scorecard")
def get_scorecard(rid: str) -> JSONResponse:
    st, run = _run_or_404(rid)
    if run is None:
        return _err("NOT_FOUND", "No such research run.", 404)
    return JSONResponse({"scorecards": st.scorecards(rid), "run_flags": run["run_flags"], "research": EV.describe(), "universe_note": SE.UNIVERSE_NOTE})
