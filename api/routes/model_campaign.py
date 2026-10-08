"""
api/routes/model_campaign.py — Stage 5.0 MODEL EVALUATION CAMPAIGN / LEADERBOARD (rotation_campaign/). RESEARCH ONLY.

GET  /api/model-campaign/config                 defaults, gates, ranking, limits, rotation configs (0 requests)
POST /api/model-campaign/run                    {config_id, config_hash, universe, start_date, end_date, train/test/step months, frequency,
                                                 selection_metric, candidates?, grid?, max_candidates, finalist_count, gates?, costs ...}:
                                                 the campaign — 0 broker / gateway / model requests, <= 1 market-data request; persisted
GET  /api/model-campaign/runs · /runs/{id} · /runs/{id}/leaderboard · /runs/{id}/finalists · /runs/{id}/cards · /runs/{id}/comparison

Finalists are "paper-forward-test candidate" research records: nothing here activates, deploys, schedules or previews
anything, carries a draft for any form, imports a broker module, or has a path containing "order", "trade", "backtest",
"paper", "execute", "broker" or "deploy". Every result carries the static-universe limitation. Strict bodies.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from fit import readonly as RO
from rotation import rules as R
from rotation import store as S
from rotation import universe as U
from rotation_campaign import FINALIST_LABEL
from rotation_campaign import config as C
from rotation_campaign import engine as CE
from rotation_campaign.store import ModelCampaignStore
from rotation_walkforward import grid as G

router = APIRouter(prefix="/api/model-campaign", tags=["model-campaign"])
LABEL = "MODEL EVALUATION CAMPAIGN — RESEARCH ONLY — NOTHING IS TRADED, ACTIVATED OR PROMOTED"
NOTE = ("A bounded set of deterministic rotation configurations is compared on hold-out TEST windows (Stage 4.8 replay through "
        "the Stage 4.9 machinery), filtered by transparent eligibility gates and ranked by a deterministic order that never uses "
        f"full-history CAGR. The top eligible rows are '{FINALIST_LABEL}' research records. Static universe — not a forecast, not "
        "an order path, and no configuration is switched on anywhere.")
_ID, _HASH = r"^[0-9a-f]{32}$", r"^[0-9a-f]{64}$"
NOW_FN = None
FETCH: dict = {"fetch_fn": None, "client": None, "cache": None}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UniverseBody(Strict):
    source: Literal["WATCHLIST", "SAVED_SCAN", "SAVED_UNIVERSE", "CUSTOM"]
    ref: Optional[str] = Field(default=None, max_length=64)
    symbols: Optional[List[str]] = Field(default=None, max_length=1000)


class GridBody(Strict):
    dimensions: Dict[str, List[str]] = Field(max_length=10)


class GatesBody(Strict):
    min_windows: Optional[StrictInt] = None
    max_failed_windows: Optional[StrictInt] = None
    max_drawdown_floor: Optional[str] = Field(default=None, max_length=12)
    min_positive_window_pct: Optional[str] = Field(default=None, max_length=12)
    min_benchmark_beating_pct: Optional[str] = Field(default=None, max_length=12)
    max_turnover: Optional[str] = Field(default=None, max_length=12)
    max_fragility: Optional[str] = Field(default=None, max_length=12)
    max_cost_sensitivity: Optional[str] = Field(default=None, max_length=12)


class RunBody(Strict):
    config_id: str = Field(pattern=_ID)
    config_hash: str = Field(pattern=_HASH)
    universe: UniverseBody
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)
    train_months: StrictInt = C.DEFAULTS["train_months"]
    test_months: StrictInt = C.DEFAULTS["test_months"]
    step_months: Optional[StrictInt] = None
    rebalance_frequency: Literal["WEEKLY", "MONTHLY"] = "MONTHLY"
    selection_metric: Literal["SHARPE", "SORTINO", "CAGR", "DD_CONSTRAINED_SHARPE", "COMPOSITE"] = "SHARPE"
    candidates: Optional[List[Dict]] = Field(default=None, max_length=100)
    grid: Optional[GridBody] = None
    max_candidates: StrictInt = C.DEFAULTS["max_candidates"]
    finalist_count: StrictInt = C.DEFAULTS["finalist_count"]
    gates: Optional[GatesBody] = None
    initial_cash: str = Field(default=C.DEFAULTS["initial_cash"], max_length=20)
    transaction_cost_bps: str = Field(default=C.DEFAULTS["transaction_cost_bps"], max_length=12)
    slippage_bps: str = Field(default=C.DEFAULTS["slippage_bps"], max_length=12)
    min_train_sessions: StrictInt = C.DEFAULTS["min_train_sessions"]
    min_test_sessions: StrictInt = C.DEFAULTS["min_test_sessions"]
    max_drawdown_limit: str = Field(default=C.DEFAULTS["max_drawdown_limit"], max_length=12)


def _err(code: str, message: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"status": code, "message": message}, status_code=status)


def _now() -> datetime:
    return (NOW_FN or (lambda: datetime.now(timezone.utc)))()


def _store() -> ModelCampaignStore:
    return ModelCampaignStore(RO.db_path())


def _run_out(run: dict) -> dict:
    keys = ("campaign_id", "status", "failure_code", "failure_detail", "campaign_hash", "base_config_hash", "universe_hash", "engine_version", "walkforward_version",
            "backtest_version", "robustness_version", "run_at", "completed_at", "n_candidates", "n_rejected", "n_discarded", "n_windows", "n_eligible", "n_finalists",
            "n_evaluations", "n_cache_hits", "runtime_s", "data_hash", "walkforward_result_hash", "result_hash", "market_data_requests", "universe_note")
    return {**{k: run.get(k) for k in keys}, "label": LABEL, "finalist_label": FINALIST_LABEL}


@router.get("/config")
def get_config() -> JSONResponse:
    return JSONResponse({"label": LABEL, "note": NOTE, "benchmark": C.BC.BENCHMARK, "defaults": dict(C.DEFAULTS), "gate_defaults": dict(C.GATE_DEFAULTS),
                         "limits": dict(C.LIMITS), "ranking": list(C.RANKING), "fragility_rule": C.FRAGILITY_RULE, "finalist_label": FINALIST_LABEL,
                         "grid_dimensions": list(G.DIMENSIONS), "frequencies": list(C.WC.FREQUENCIES), "selection_metrics": list(C.WC.SELECTION_METRICS),
                         "universe_sources": list(U.SOURCES), "universe_note": C.UNIVERSE_NOTE, "rotation_configs": S.RotationStore(RO.db_path()).configs(),
                         "max_symbols": U.MAX_SYMBOLS})


@router.post("/run")
def post_run(body: RunBody) -> JSONResponse:
    data = body.model_dump()
    if data.get("grid") is not None:
        data["grid"] = {"dimensions": data["grid"]["dimensions"]}
    if data.get("gates") is not None:
        data["gates"] = {k: v for k, v in data["gates"].items() if v is not None}
    try:
        out = CE.run_campaign(_store(), data, now=_now(), path=RO.db_path(), **FETCH)
    except C.CampaignConfigError as exc:
        return _err(exc.code, exc.message, exc.status)
    except U.UniverseError as exc:
        return _err(exc.code, exc.message, exc.status)
    except (S.StoreError, R.ConfigError) as exc:
        return _err(exc.code, exc.message, 422)
    return JSONResponse({"run": _run_out(out["run"]), "definition": {k: v for k, v in out["definition"].items() if k != "candidates"}, "rejected": out["rejected"],
                         "windows": out["windows"], "leaderboard": out["leaderboard"], "finalists": out["finalists"], "cards": out["cards"],
                         "comparison": out["comparison"], "stability": out["stability"]})


@router.get("/runs")
def get_runs(limit: int = 50) -> JSONResponse:
    return JSONResponse({"runs": [_run_out(r) for r in _store().campaigns(max(1, min(int(limit), 200)))]})


def _run_or_404(cid: str):
    st = _store()
    run = st.campaign(cid) if len(cid) == 32 else None
    return st, run


@router.get("/runs/{cid}")
def get_run(cid: str) -> JSONResponse:
    st, run = _run_or_404(cid)
    if run is None:
        return _err("NOT_FOUND", "No such campaign.", 404)
    m = st.metrics(cid)
    return JSONResponse({"run": _run_out(run), "definition": {k: v for k, v in run["definition"].items() if k != "candidates"}, "n_candidates": len(run["definition"]["candidates"]),
                         "stability": m["stability"] if m else None, "conventions": m["conventions"] if m else CE.CONVENTIONS})


@router.get("/runs/{cid}/leaderboard")
def get_leaderboard(cid: str) -> JSONResponse:
    st, run = _run_or_404(cid)
    if run is None:
        return _err("NOT_FOUND", "No such campaign.", 404)
    return JSONResponse({"leaderboard": st.leaderboard(cid), "ranking": list(C.RANKING), "gates": run["definition"]["gates"], "universe_note": C.UNIVERSE_NOTE})


@router.get("/runs/{cid}/finalists")
def get_finalists(cid: str) -> JSONResponse:
    st, run = _run_or_404(cid)
    if run is None:
        return _err("NOT_FOUND", "No such campaign.", 404)
    return JSONResponse({"finalists": st.finalists(cid), "status": run["status"], "finalist_label": FINALIST_LABEL, "universe_note": C.UNIVERSE_NOTE})


@router.get("/runs/{cid}/cards")
def get_cards(cid: str) -> JSONResponse:
    st, run = _run_or_404(cid)
    if run is None:
        return _err("NOT_FOUND", "No such campaign.", 404)
    return JSONResponse({"cards": st.cards(cid), "universe_note": C.UNIVERSE_NOTE})


@router.get("/runs/{cid}/comparison")
def get_comparison(cid: str) -> JSONResponse:
    st, run = _run_or_404(cid)
    if run is None:
        return _err("NOT_FOUND", "No such campaign.", 404)
    m = st.metrics(cid)
    return JSONResponse({"comparison": m["comparison"] if m else None, "candidates": [{"config_hash": c["config_hash"], "label": c["label"], "config": c["config"],
                                                                                     "evidence": c["evidence"]} for c in st.candidates(cid)], "universe_note": C.UNIVERSE_NOTE})
