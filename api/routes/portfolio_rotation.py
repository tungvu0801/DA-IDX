"""
api/routes/portfolio_rotation.py — Stage 4.7 Portfolio Rotation (rotation/engine.py, rotation/store.py). PROPOSALS ONLY.

GET  /api/portfolio-rotation/config                    defaults, limits, sources, selector lists, loaded snapshots (0 requests)
GET  /api/portfolio-rotation/configs                   immutable configuration versions
POST /api/portfolio-rotation/configs                   create one immutable configuration version (rejected input is not stored)
POST /api/portfolio-rotation/snapshot                  {source}: EXPLICIT portfolio snapshot load, held in process memory
                                                       ROBINHOOD_READ_ONLY: get_portfolio + get_positions through the existing
                                                       read-only provider (2 loopback gateway GETs) · ALPACA_PAPER_VIEW: the last
                                                       Stage 4.6A refresh (0 Alpaca calls) · LOCAL_SIMULATOR: local DB (0 network)
POST /api/portfolio-rotation/run                       {config_id, config_hash, universe, portfolio_source}: the deterministic
                                                       rotation on the already-loaded snapshot — 0 broker / gateway requests,
                                                       <= 1 batched market-data request; persisted atomically
GET  /api/portfolio-rotation/runs · /runs/{id} · /runs/{id}/candidates · /runs/{id}/targets · /runs/{id}/rebalance

Handoff (Phase 5, rotation/handoff.py): only an ALPACA_PAPER_VIEW run's eligible items carry a browser prefill draft
(symbol, side, whole shares) for the EXISTING frozen Stage 4.6B preview form; ROBINHOOD_READ_ONLY and LOCAL_SIMULATOR
runs are DISPLAY ONLY. There is no order, no broker write, no alternate preview / confirm path and no Stage 4.6B service
import here. Strict bodies (unknown fields → 422). Nothing secret is returned: account identifiers appear only masked or
as the gateway alias.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from fit import readonly as RO
from rotation import engine as E
from rotation import handoff as H
from rotation import rules as R
from rotation import snapshots as SN
from rotation import store as S
from rotation import universe as U

router = APIRouter(prefix="/api/portfolio-rotation", tags=["portfolio-rotation"])
LABEL = "PORTFOLIO ROTATION — PROPOSAL ONLY — NO ORDERS ARE SENT"
NOTE = ("A deterministic ranking of an explicit universe into an equal-weight target portfolio and a rebalance proposal. "
        "Nothing here places or previews an order; an Alpaca Paper run's eligible items can only prefill the existing "
        "Alpaca Paper — Manual Orders form, where you still Preview and Confirm.")
HANDOFF = H.MODE_DISPLAY
SOURCE_LABELS = {SN.ROBINHOOD_READ_ONLY: "Robinhood Read Only", SN.ALPACA_PAPER_VIEW: "Alpaca Paper", SN.LOCAL_SIMULATOR: "Local Simulator"}
FRESH_FOR_MIN = 30                                           # the display freshness; a run applies its config's max_snapshot_age_min
_ID, _HASH = r"^[0-9a-f]{32}$", r"^[0-9a-f]{64}$"
_LOCK = threading.Lock()
_SNAPSHOTS: Dict[str, SN.PortfolioSnapshot] = {}             # source -> the last EXPLICITLY loaded snapshot (process memory)
VIEW_FN = None                                               # tests: a fake Stage 4.6A view (default: the adapter's own frozen view())
PROVIDER_FN = None                                           # tests: a fake read-only provider (default: the adapter's approved path)
NOW_FN = None                                                # tests: a fixed clock
FETCH: dict = {"fetch_fn": None, "client": None, "cache": None}   # tests: synthetic market data


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeightsBody(Strict):
    momentum: str = Field(max_length=12)
    trend: str = Field(max_length=12)
    relative_strength: str = Field(max_length=12)
    volatility: str = Field(max_length=12)
    drawdown: str = Field(default="0", max_length=12)
    liquidity: str = Field(max_length=12)


class ConfigBody(Strict):
    name: str = Field(min_length=1, max_length=80)
    weights: WeightsBody
    portfolio_size: StrictInt
    exit_rank: StrictInt
    cash_buffer_pct: str = Field(max_length=12)
    rebalance_threshold: str = Field(max_length=12)
    max_turnover_per_rotation: str = Field(max_length=12)
    max_position_weight: str = Field(max_length=12)
    min_position_weight: str = Field(max_length=12)
    min_price: str = Field(default="5.00", max_length=16)
    min_avg_dollar_volume: str = Field(default="5000000.00", max_length=20)
    min_history_sessions: StrictInt = 252
    max_snapshot_age_min: StrictInt = 30
    excluded_symbols: List[str] = Field(default_factory=list, max_length=200)


class SnapshotBody(Strict):
    source: Literal["ROBINHOOD_READ_ONLY", "ALPACA_PAPER_VIEW", "LOCAL_SIMULATOR"]


class UniverseBody(Strict):
    source: Literal["WATCHLIST", "SAVED_SCAN", "SAVED_UNIVERSE", "CUSTOM"]
    ref: Optional[str] = Field(default=None, max_length=64)
    symbols: Optional[List[str]] = Field(default=None, max_length=1000)


class RunBody(Strict):
    config_id: str = Field(pattern=_ID)
    config_hash: str = Field(pattern=_HASH)
    universe: UniverseBody
    portfolio_source: Literal["ROBINHOOD_READ_ONLY", "ALPACA_PAPER_VIEW", "LOCAL_SIMULATOR"]


def _err(code: str, message: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"status": code, "message": message}, status_code=status)


def _now() -> datetime:
    return (NOW_FN or (lambda: datetime.now(timezone.utc)))()


def _store() -> S.RotationStore:
    return S.RotationStore(RO.db_path())


def _age_min(snapshot: SN.PortfolioSnapshot, now: datetime) -> Optional[int]:
    try:
        return int((now - SN.parse_time(snapshot.snapshot_at)).total_seconds() // 60) if snapshot.snapshot_at else None
    except ValueError:
        return None


def _snapshot_out(snapshot: SN.PortfolioSnapshot, now: datetime) -> dict:
    """The normalised snapshot plus informational metadata (masked / alias identifiers only)."""
    meta = snapshot.source_meta or {}
    age = _age_min(snapshot, now)
    info = {k: meta.get(k) for k in ("account_alias", "account_masked_id", "account_number_masked", "gateway_status", "cache_age_s",
                                     "equity_value", "total_value", "buying_power", "equity", "portfolio_value", "last_result", "name",
                                     "pending_symbols") if meta.get(k) is not None}
    return {"source": snapshot.source, "source_label": SOURCE_LABELS[snapshot.source], "snapshot_at": snapshot.snapshot_at,
            "status": snapshot.status, "age_min": age, "fresh": snapshot.status == SN.OK and age is not None and age <= FRESH_FOR_MIN,
            "fresh_for_min": FRESH_FOR_MIN, "cash": format(snapshot.cash, "f"), "n_positions": len(snapshot.positions),
            "positions": [{"symbol": s, "quantity": format(q, "f"), "flags": snapshot.flags(s)} for s, q in snapshot.positions],
            "info": info, "handoff": H.mode_for_source(snapshot.source)}


def _loaded(now: datetime) -> dict:
    with _LOCK:
        snaps = dict(_SNAPSHOTS)
    return {src: (_snapshot_out(snaps[src], now) if src in snaps else None) for src in SN.SOURCES}


def _selectors(path) -> dict:
    """Lists for the universe pickers (0 requests): saved scans with their latest resolved symbols, saved versions with
    their universe sizes, the watchlist size."""
    out = {"saved_scans": [], "saved_versions": [], "watchlist_count": None}
    try:
        from fit import saved_scans as SS
        st = SS.SavedScanStore(path)
        for sc in st.scans(include_archived=False):
            latest = st.latest(sc["saved_scan_id"])
            out["saved_scans"].append({"saved_scan_id": sc["saved_scan_id"], "name": sc["name"], "list_source": sc["list_source"],
                                       "latest_session": latest["decision_session"] if latest else None,
                                       "n_symbols": len(latest["resolved_symbols"]) if latest else None})
    except Exception:  # noqa: BLE001 - no saved-scan tables yet
        pass
    try:
        with RO.connect(path) as conn:
            sizes = {r["version_id"]: r["spec_json"] for r in conn.execute("SELECT version_id, spec_json FROM strategy_versions")}
        for v in RO.saved_versions(path, include_old=True):
            try:
                n = len(json.loads(sizes.get(v["strategy_version_id"]) or "{}")["universe"]["symbols"])
            except (ValueError, KeyError, TypeError):
                n = None
            out["saved_versions"].append({"strategy_version_id": v["strategy_version_id"], "strategy_name": v["strategy_name"],
                                          "version_number": v["version_number"], "is_current": v["is_current"], "n_symbols": n})
    except Exception:  # noqa: BLE001 - no strategy tables yet
        pass
    try:
        from scanner.watchlist import load_watchlist
        out["watchlist_count"] = len(list(load_watchlist()))
    except Exception:  # noqa: BLE001
        out["watchlist_count"] = None
    return out


def _run_out(run: dict) -> dict:
    keys = ("run_id", "status", "status_detail", "data_session", "benchmark", "portfolio_source", "universe_source", "universe_ref",
            "universe_hash", "config_id", "config_hash", "input_hash", "proposal_hash", "reference_equity", "snapshot_cash",
            "current_cash_weight", "target_cash_weight", "turnover", "n_universe", "n_eligible", "n_selected", "market_data_requests",
            "run_at", "completed_at", "portfolio_snapshot_at", "snapshot_status", "source_mismatch_note")
    src = run.get("portfolio_source")
    return {**{k: run.get(k) for k in keys}, "handoff_mode": H.mode_for_source(src), "source_label": H.SOURCE_LABELS.get(src),
            "handoff_explanation": H.EXPLANATIONS.get(src)}


def _decode(row: dict, keys) -> dict:
    out = dict(row)
    for k in keys:
        if k in out:
            out[k[:-5]] = json.loads(out.pop(k)) if out[k] is not None else None
    return out


# ---- configuration ---------------------------------------------------------------------------------------------------------------

@router.get("/config")
def get_config() -> JSONResponse:
    now = _now()
    return JSONResponse({"label": LABEL, "note": NOTE, "benchmark": "SPY", "max_symbols": U.MAX_SYMBOLS,
                         "handoff_modes": {s: H.mode_for_source(s) for s in SN.SOURCES},
                         "portfolio_sources": [{"id": s, "label": SOURCE_LABELS[s], "source_label": H.SOURCE_LABELS[s],
                                                "handoff": H.mode_for_source(s), "control_label": H.CONTROL_LABELS[s],
                                                "explanation": H.EXPLANATIONS[s]} for s in SN.SOURCES],
                         "universe_sources": list(U.SOURCES), "defaults": {**S.CONFIG_DEFAULTS, "weights": R.DEFAULT_WEIGHTS},
                         "weight_keys": list(R.WEIGHT_KEYS), "actions": list(R.ACTIONS), "fresh_for_min": FRESH_FOR_MIN,
                         "snapshots": _loaded(now), **_selectors(RO.db_path())})


@router.get("/configs")
def get_configs() -> JSONResponse:
    return JSONResponse({"configs": _store().configs()})


@router.post("/configs", status_code=201)
def post_config(body: ConfigBody) -> JSONResponse:
    data = body.model_dump()
    name = data.pop("name")
    try:
        row = _store().create_config(name, data, _now())
    except (S.StoreError, R.ConfigError) as exc:
        return _err(exc.code, exc.message, 422)
    return JSONResponse(row, status_code=201 if row.get("created") else 200)


# ---- snapshots (explicit; held in memory) ------------------------------------------------------------------------------------------

@router.post("/snapshot")
def post_snapshot(body: SnapshotBody) -> JSONResponse:
    now = _now()
    try:
        if body.source == SN.ROBINHOOD_READ_ONLY:
            snapshot = SN.load_robinhood_snapshot(PROVIDER_FN)
        elif body.source == SN.ALPACA_PAPER_VIEW:
            snapshot = SN.load_alpaca_snapshot(VIEW_FN)
        else:
            snapshot = SN.load_local_simulator_snapshot(RO.db_path(), now)
    except SN.SnapshotError as exc:
        return _err(exc.code, exc.message, 422)
    with _LOCK:
        _SNAPSHOTS[body.source] = snapshot
    return JSONResponse({"snapshot": _snapshot_out(snapshot, now)})


# ---- runs ------------------------------------------------------------------------------------------------------------------------------

@router.post("/run")
def post_run(body: RunBody) -> JSONResponse:
    now = _now()
    store = _store()
    cfg = store.config(body.config_id)
    if cfg is None:
        return _err("INVALID_CONFIG", "Unknown configuration.", 404)
    if cfg["config_hash"] != body.config_hash:
        return _err("CONFIG_HASH_MISMATCH", "The configuration hash does not match the stored version.", 409)
    with _LOCK:
        snapshot = _SNAPSHOTS.get(body.portfolio_source)
    try:
        out = E.run_rotation(store, body.config_id, body.universe.model_dump(), body.portfolio_source, snapshot, now=now,
                             path=RO.db_path(), **FETCH)
    except U.UniverseError as exc:
        return _err(exc.code, exc.message, exc.status)
    except E.EngineError as exc:
        return _err(exc.code, exc.message, exc.status)
    except (S.StoreError, R.ConfigError) as exc:
        return _err(exc.code, exc.message, 422)
    return JSONResponse({"run": _run_out(out["run"])})


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
        return _err("NOT_FOUND", "No such rotation run.", 404)
    return JSONResponse({"run": _run_out(run), "integrity": st.verify_run(run_id), "universe": json.loads(run["universe_json"]),
                         "positions": json.loads(run["positions_json"]) if run["positions_json"] else None})


@router.get("/runs/{run_id}/candidates")
def get_candidates(run_id: str) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such rotation run.", 404)
    return JSONResponse({"candidates": [_decode(c, ("reasons_json", "raw_json", "scores_json", "flags_json")) for c in st.candidates(run_id)]})


@router.get("/runs/{run_id}/targets")
def get_targets(run_id: str) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such rotation run.", 404)
    return JSONResponse({"targets": [_decode(t, ("flags_json",)) for t in st.targets(run_id)]})


@router.get("/runs/{run_id}/rebalance")
def get_rebalance(run_id: str) -> JSONResponse:
    st, run = _run_or_404(run_id)
    if run is None:
        return _err("NOT_FOUND", "No such rotation run.", 404)
    src = run["portfolio_source"]
    return JSONResponse({"items": [{**i, "handoff": H.evaluate(run, i)} for i in st.items(run_id)],
                         "handoff_mode": H.mode_for_source(src), "source_label": H.SOURCE_LABELS.get(src),
                         "control_label": H.CONTROL_LABELS.get(src), "explanation": H.EXPLANATIONS.get(src),
                         "run_status": run["status"], "source_mismatch_note": run["source_mismatch_note"]})
