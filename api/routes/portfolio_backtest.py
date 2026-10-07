"""
api/routes/portfolio_backtest.py — Stage 4.8 HISTORICAL ROTATION BACKTEST (rotation_backtest/). RESEARCH ONLY.

GET  /api/rotation-replay/config                 defaults, limits, frequencies, rotation configuration versions (0 requests)
POST /api/rotation-replay/run                    {config_id, config_hash, universe, start_date, end_date, rebalance_frequency,
                                                     initial_cash, transaction_cost_bps, slippage_bps}: the deterministic replay —
                                                     0 broker / gateway / model requests, <= 1 batched market-data request; persisted
GET  /api/rotation-replay/runs · /runs/{id} · /runs/{id}/equity · /runs/{id}/fills · /runs/{id}/rebalances
(the simulated fills live under /fills: the Stage 2 isolation rule keeps every 'trade' / 'order' path out of this app)

Nothing here previews, prepares or sends an order; responses carry no draft for any form, there is no broker import and
no path contains "order". The prefix is /api/rotation-replay: the Stage 3.1 route rule reserves the word "backtest" in a
path for the Stage 3.2 /api/backtests endpoints, so the historical replay lives under its own name. Results are a historical SIMULATION of the Stage 4.7 model over a static universe (survivorship bias noted).
Strict bodies (unknown fields → 422).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from fit import readonly as RO
from rotation import rules as R
from rotation import store as S
from rotation.store import dec_str
from rotation import universe as U
from rotation_backtest import config as C
from rotation_backtest import metrics as MX
from rotation_backtest import runner as RUN
from rotation_backtest.store import PortfolioBacktestStore

router = APIRouter(prefix="/api/rotation-replay", tags=["portfolio-backtest"])
LABEL = "HISTORICAL ROTATION BACKTEST — RESEARCH ONLY — NOTHING IS TRADED"
NOTE = ("A deterministic replay of a saved Portfolio Rotation configuration over historical daily bars: signals at a completed "
        "close, simulated fills at the next session's open with explicit slippage and cost, whole shares, no leverage, no "
        "shorting. A historical simulation of a static universe — not a forecast and not an order path.")
_ID, _HASH = r"^[0-9a-f]{32}$", r"^[0-9a-f]{64}$"
NOW_FN = None                                                # tests: a fixed clock
FETCH: dict = {"fetch_fn": None, "client": None, "cache": None}   # tests: synthetic market data


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UniverseBody(Strict):
    source: Literal["WATCHLIST", "SAVED_SCAN", "SAVED_UNIVERSE", "CUSTOM"]
    ref: Optional[str] = Field(default=None, max_length=64)
    symbols: Optional[List[str]] = Field(default=None, max_length=1000)


class RunBody(Strict):
    config_id: str = Field(pattern=_ID)
    config_hash: str = Field(pattern=_HASH)
    universe: UniverseBody
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)
    rebalance_frequency: Literal["WEEKLY", "MONTHLY"] = "MONTHLY"
    initial_cash: str = Field(default=C.DEFAULTS["initial_cash"], max_length=20)
    transaction_cost_bps: str = Field(default=C.DEFAULTS["transaction_cost_bps"], max_length=12)
    slippage_bps: str = Field(default=C.DEFAULTS["slippage_bps"], max_length=12)


def _err(code: str, message: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"status": code, "message": message}, status_code=status)


def _now() -> datetime:
    return (NOW_FN or (lambda: datetime.now(timezone.utc)))()


def _store() -> PortfolioBacktestStore:
    return PortfolioBacktestStore(RO.db_path())


def _run_out(run: dict) -> dict:
    out = {k: run.get(k) for k in ("run_id", "status", "failure_code", "failure_detail", "backtest_config_id", "backtest_config_hash", "config_id",
                                   "config_hash", "universe_hash", "engine_version", "run_at", "completed_at", "first_session", "last_session",
                                   "n_sessions", "n_rebalances", "n_rebalances_executed", "n_trades", "initial_cash", "final_equity", "final_cash",
                                   "total_costs", "data_hash", "result_hash", "market_data_requests", "universe_note")}
    for k in ("initial_cash", "final_equity", "final_cash", "total_costs"):
        out[k] = dec_str(out.get(k))
    out["label"] = LABEL
    return out


@router.get("/config")
def get_config() -> JSONResponse:
    return JSONResponse({"label": LABEL, "note": NOTE, "benchmark": C.BENCHMARK, "execution_price": C.EXECUTION_PRICE,
                         "frequencies": list(C.FREQUENCIES), "defaults": dict(C.DEFAULTS),
                         "limits": {k: (str(v) if not isinstance(v, int) else v) for k, v in C.LIMITS.items()}, "universe_sources": list(U.SOURCES),
                         "universe_note": C.UNIVERSE_NOTE, "conventions": MX.CONVENTIONS, "rotation_configs": S.RotationStore(RO.db_path()).configs(),
                         "max_symbols": U.MAX_SYMBOLS})


@router.post("/run")
def post_run(body: RunBody) -> JSONResponse:
    try:
        out = RUN.run_backtest(_store(), body.model_dump(), now=_now(), path=RO.db_path(), **FETCH)
    except C.BacktestConfigError as exc:
        return _err(exc.code, exc.message, exc.status)
    except U.UniverseError as exc:
        return _err(exc.code, exc.message, exc.status)
    except (S.StoreError, R.ConfigError) as exc:
        return _err(exc.code, exc.message, 422)
    return JSONResponse({"run": _run_out(out["run"]), "definition": out["definition"], "metrics": out["metrics"], "conventions": out["conventions"],
                         "final_holdings": out["final_holdings"], "n_equity": len(out["equity"])})


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
        return _err("NOT_FOUND", "No such backtest run.", 404)
    cfg = st.config_by_hash(run["backtest_config_hash"])
    m = st.metrics(run_id)
    return JSONResponse({"run": _run_out(run), "definition": (cfg or {}).get("definition"), "universe": (cfg or {}).get("universe"),
                         "metrics": m["metrics"] if m else None, "conventions": m["conventions"] if m else MX.CONVENTIONS,
                         "bars": __import__("json").loads(run["bars_json"])})


@router.get("/runs/{run_id}/equity")
def get_equity(run_id: str) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such backtest run.", 404)
    return JSONResponse({"equity": st.equity(run_id), "benchmark": C.BENCHMARK, "initial_cash": run["initial_cash"]})


@router.get("/runs/{run_id}/fills")
def get_trades(run_id: str) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such backtest run.", 404)
    return JSONResponse({"trades": st.trades(run_id), "note": "simulated fills of a historical replay — nothing was or can be sent to a broker"})


@router.get("/runs/{run_id}/rebalances")
def get_rebalances(run_id: str) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such backtest run.", 404)
    return JSONResponse({"rebalances": st.rebalances(run_id)})
