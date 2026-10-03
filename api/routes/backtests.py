"""
api/routes/backtests.py — Stage 3.2 HISTORICAL BACKTEST API (research and validation only).

GET  /api/backtests/config                         defaults, limits, cost + execution model, formulas (no I/O)
POST /api/backtests/preflight                      eligibility, date / cost checks, cached-data coverage, download plan
POST /api/backtests/data                           explicit, read-only Alpaca MARKET DATA download of the listed bars
POST /api/backtests                                start a run (202) — one explicit, one-shot background job
GET  /api/backtests?strategy_id=&version_number=   run history, newest first
GET  /api/backtests/{run_id}                       the STORED run: config, status / progress, result, equity curve
GET  /api/backtests/{run_id}/ledger                simulated round trips (closed + open at end) with snapshots + traces
GET  /api/backtests/{run_id}/signals               audit events: signals, fills, skips, unfilled entries / exits

Stored runs are never recomputed to be viewed. Only saved BACKTEST READY strategy versions can run. No endpoint here
talks to a broker, paper trades, optimises or searches parameters, ranks strategies, schedules anything or calls Claude.
"""
import re
from typing import Literal, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backtest import runs
from backtest.store import BacktestError, get_backtest_store

router = APIRouter(prefix="/api/backtests", tags=["backtests"])
_ID = re.compile(r"^[0-9a-f]{32}$")
NOTE = ("Historical behaviour of one saved strategy version under the stated assumptions — not a prediction, a "
        "recommendation or a ranking.")


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    version_number: int = Field(ge=1)
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    initial_equity: float = Field(default=runs.DEFAULTS["initial_equity"], ge=runs.LIMITS["initial_equity"][0],
                                  le=runs.LIMITS["initial_equity"][1])
    slippage_bps_per_side: float = Field(default=0.0, ge=0.0, le=runs.LIMITS["slippage_bps_per_side"][1])
    commission_per_order: float = Field(default=0.0, ge=0.0, le=runs.LIMITS["commission_per_order"][1])
    selection_policy: Literal["ALPHABETICAL"] = "ALPHABETICAL"


class DataBody(RunBody):
    refresh: bool = False


def _err(exc: BacktestError) -> JSONResponse:
    return JSONResponse({"status": exc.code, "message": exc.message, "errors": exc.detail or []}, status_code=exc.status)


def _public(pf: dict) -> dict:
    return {k: v for k, v in pf.items() if not k.startswith("_")} | {"note": NOTE}


def _missing() -> JSONResponse:
    return JSONResponse({"status": "NOT_FOUND", "message": "Backtest run not found."}, status_code=404)


@router.get("/config")
def get_config() -> JSONResponse:
    return JSONResponse(runs.public_config())


@router.post("/preflight")
def post_preflight(body: RunBody) -> JSONResponse:
    return JSONResponse(_public(runs.preflight(get_backtest_store(), body.model_dump())))


@router.post("/data")
def post_data(body: DataBody) -> JSONResponse:
    b = body.model_dump()
    refresh = b.pop("refresh")
    try:
        return JSONResponse(_public(runs.download(get_backtest_store(), b, refresh=refresh)))
    except BacktestError as exc:
        return _err(exc)


@router.post("", status_code=202)
def post_run(body: RunBody) -> JSONResponse:
    try:
        return JSONResponse(runs.start_run(get_backtest_store(), body.model_dump()) | {"note": NOTE}, status_code=202)
    except BacktestError as exc:
        return _err(exc)


@router.get("")
def list_runs(strategy_id: Optional[str] = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
              version_number: Optional[int] = Query(default=None, ge=1)) -> JSONResponse:
    return JSONResponse({"runs": get_backtest_store().list_runs(strategy_id, version_number), "note": NOTE})


@router.get("/{run_id}")
def get_run(run_id: str) -> JSONResponse:
    if not _ID.match(run_id):
        return _missing()
    store = get_backtest_store()
    try:
        run = store.get_run(run_id)
    except BacktestError as exc:
        return _err(exc)
    run["progress"] = runs.progress(run_id) if run["status"] in ("PENDING", "RUNNING") else None
    run["equity"] = store.equity(run_id) if run["status"] == "COMPLETED" else []
    return JSONResponse(run | {"note": NOTE})


@router.get("/{run_id}/ledger")
def get_ledger(run_id: str) -> JSONResponse:
    if not _ID.match(run_id):
        return _missing()
    store = get_backtest_store()
    try:
        run = store.get_run(run_id)
    except BacktestError as exc:
        return _err(exc)
    return JSONResponse({"run_id": run_id, "status": run["status"], "trades": store.trades(run_id),
                         "note": "Simulated round trips from a historical backtest — no order was ever sent anywhere."})


@router.get("/{run_id}/signals")
def get_signals(run_id: str) -> JSONResponse:
    if not _ID.match(run_id):
        return _missing()
    store = get_backtest_store()
    try:
        store.get_run(run_id)
    except BacktestError as exc:
        return _err(exc)
    return JSONResponse({"run_id": run_id, "events": store.signals(run_id)})
