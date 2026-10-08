"""
api/routes/portfolio_walkforward.py — Stage 4.9 WALK-FORWARD + ROBUSTNESS (rotation_walkforward/). RESEARCH ONLY.

GET  /api/rotation-walkforward/config              defaults, limits, selection metrics, grid dimensions, rotation configs (0 requests)
POST /api/rotation-walkforward/run                 {config_id, config_hash, universe, start_date, end_date, train_months, test_months,
                                                    step_months?, rebalance_frequency, selection_metric, candidates?, grid?, max_candidates,
                                                    initial_cash, transaction_cost_bps, slippage_bps, ...}: train-only selection, test-only
                                                    evaluation per window — 0 broker / gateway / model requests, <= 1 market-data request
GET  /api/rotation-walkforward/runs · /runs/{id} · /runs/{id}/windows · /runs/{id}/oos · /runs/{id}/sensitivity · /runs/{id}/regimes

Nothing here previews, prepares or sends an order, activates or deploys a configuration, or carries a draft for any
form; there is no broker import and no path contains "order", "trade", "backtest", "paper", "execute" or "broker"
(prior-stage route rules). Every result carries the static-universe limitation. Strict bodies (unknown fields → 422).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from fit import readonly as RO
from rotation import rules as R
from rotation import store as S
from rotation import universe as U
from rotation_walkforward import config as C
from rotation_walkforward import engine as WF
from rotation_walkforward import grid as G
from rotation_walkforward import robustness as RB
from rotation_walkforward.store import PortfolioWalkForwardStore

router = APIRouter(prefix="/api/rotation-walkforward", tags=["rotation-walkforward"])
LABEL = "WALK-FORWARD ROBUSTNESS — RESEARCH ONLY — NOTHING IS TRADED OR DEPLOYED"
NOTE = ("Train-only candidate selection, then evaluation of the one frozen configuration on the unseen test window, advanced "
        "window by window. Aggregate out-of-sample metrics, a documented robustness score, cost and parameter sensitivity and "
        "descriptive SPY regimes. A historical simulation over a static universe — not a forecast, not an order path, and no "
        "configuration is switched on anywhere.")
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


def _store() -> PortfolioWalkForwardStore:
    return PortfolioWalkForwardStore(RO.db_path())


def _run_out(run: dict) -> dict:
    keys = ("run_id", "status", "failure_code", "failure_detail", "wf_config_id", "wf_config_hash", "base_config_hash", "universe_hash", "engine_version",
            "backtest_version", "robustness_version", "run_at", "completed_at", "n_windows", "n_candidates", "n_rejected", "n_discarded", "n_evaluations",
            "n_cache_hits", "overlapping_tests", "runtime_s", "data_hash", "result_hash", "market_data_requests", "universe_note")
    out = {k: run.get(k) for k in keys}
    out["overlapping_tests"] = bool(out.get("overlapping_tests"))
    out["label"] = LABEL
    return out


@router.get("/config")
def get_config() -> JSONResponse:
    return JSONResponse({"label": LABEL, "note": NOTE, "benchmark": C.BENCHMARK, "frequencies": list(C.FREQUENCIES), "selection_metrics": list(C.SELECTION_METRICS),
                         "default_selection_metric": C.DEFAULT_SELECTION, "defaults": {k: v for k, v in C.DEFAULTS.items()},
                         "limits": {k: v for k, v in C.LIMITS.items()}, "grid_dimensions": list(G.DIMENSIONS), "cost_matrix": [list(p) for p in C.COST_MATRIX],
                         "universe_sources": list(U.SOURCES), "universe_note": C.UNIVERSE_NOTE, "robustness_formula": RB.SCORE_FORMULA,
                         "conventions": RB.CONVENTIONS, "rotation_configs": S.RotationStore(RO.db_path()).configs(), "max_symbols": U.MAX_SYMBOLS})


@router.post("/run")
def post_run(body: RunBody) -> JSONResponse:
    data = body.model_dump()
    if data.get("grid") is not None:
        data["grid"] = {"dimensions": data["grid"]["dimensions"]}
    try:
        out = WF.run_walkforward(_store(), data, now=_now(), path=RO.db_path(), **FETCH)
    except C.WalkForwardConfigError as exc:
        return _err(exc.code, exc.message, exc.status)
    except U.UniverseError as exc:
        return _err(exc.code, exc.message, exc.status)
    except (S.StoreError, R.ConfigError) as exc:
        return _err(exc.code, exc.message, 422)
    metrics = out["metrics"]
    slim = {k: v for k, v in (metrics or {}).items() if k != "stitched_equity"} if metrics else None
    return JSONResponse({"run": _run_out(out["run"]), "definition": {k: v for k, v in out["definition"].items() if k != "candidates"},
                         "n_candidates": len(out["definition"]["candidates"]), "rejected": out["rejected"], "windows": out["windows"],
                         "selections": [{k: v for k, v in s.items() if k != "config"} for s in out["selections"]],
                         "oos": [{k: v for k, v in o.items() if k != "equity"} for o in out["oos"]], "metrics": slim, "robustness": out["robustness"],
                         "cost_sensitivity": out["cost_sensitivity"], "parameter_sensitivity": out["parameter_sensitivity"], "regimes": out["regimes"]})


@router.get("/runs")
def get_runs(limit: int = 50) -> JSONResponse:
    return JSONResponse({"runs": [_run_out(r) for r in _store().runs(max(1, min(int(limit), 200)))]})


def _run_or_404(run_id: str):
    st = _store()
    run = st.run(run_id) if len(run_id) == 32 else None
    return st, run


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such walk-forward run.", 404)
    cfg = st.config_by_hash(run["wf_config_hash"])
    m = st.metrics(run_id)
    return JSONResponse({"run": _run_out(run), "definition": {k: v for k, v in ((cfg or {}).get("definition") or {}).items() if k != "candidates"},
                         "candidates": (cfg or {}).get("candidates"), "universe": (cfg or {}).get("universe"),
                         "metrics": ({k: v for k, v in m["metrics"].items() if k != "stitched_equity"} if m else None),
                         "stitched_equity": (m["metrics"].get("stitched_equity") if m else None), "robustness": m["robustness"] if m else None,
                         "conventions": m["conventions"] if m else RB.CONVENTIONS, "selections": st.selections(run_id), "bars": json.loads(run["bars_json"])})


@router.get("/runs/{run_id}/windows")
def get_windows(run_id: str) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such walk-forward run.", 404)
    return JSONResponse({"windows": st.windows(run_id), "candidates": st.candidates(run_id), "selections": st.selections(run_id)})


@router.get("/runs/{run_id}/oos")
def get_oos(run_id: str, equity: bool = False) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such walk-forward run.", 404)
    return JSONResponse({"oos": st.oos(run_id, with_equity=bool(equity)), "universe_note": C.UNIVERSE_NOTE})


@router.get("/runs/{run_id}/sensitivity")
def get_sensitivity(run_id: str) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such walk-forward run.", 404)
    rows = st.sensitivity(run_id)
    return JSONResponse({"cost": [r["payload"] for r in rows if r["kind"] == "COST"],
                         "parameter": next((r["payload"] for r in rows if r["kind"] == "PARAMETER"), None), "universe_note": C.UNIVERSE_NOTE})


@router.get("/runs/{run_id}/regimes")
def get_regimes(run_id: str) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such walk-forward run.", 404)
    rows = st.sensitivity(run_id, "REGIME")
    return JSONResponse({"regimes": rows[0]["payload"] if rows else None, "universe_note": C.UNIVERSE_NOTE})
