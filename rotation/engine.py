"""
rotation/engine.py — Stage 4.7 deterministic ORCHESTRATION (DESIGN_47_PORTFOLIO_ROTATION §5–§14).

  compute_rotation(config_row, universe, series, snapshot, T, ...)   pure: eligibility E1–E10, Phase 1 factors / scores /
                                                                      ranking / rank buffer / equal weights / actions /
                                                                      turnover, reference valuation, hashes → RotationResult
  run_rotation(store, config_id, universe_spec, portfolio_source, snapshot, ...)
                                                                      resolve the universe, check the already-loaded
                                                                      snapshot (fail closed), load completed-session bars
                                                                      through the existing read-only bar layer (≤ 1 batched
                                                                      market-data request), compute, persist atomically

The engine knows nothing of HTTP, the frontend, Claude or a broker. It never fetches a quote, never refreshes a snapshot
and never constructs an order: its output is a PROPOSAL. Valuation uses reference_equity = snapshot cash + Σ quantity ×
completed-session close at T — broker-reported equity, market values and buying power live in source_meta only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_EVEN
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from backtest import bars as B
from backtest.replay import BarSeries
from backtest.store import bars_content_hash
from fit import current as FC
from fit import readonly as RO
from paper import alpaca_order_rules as RU                      # MAX_QTY / SYMBOL_RE (pure constants of the frozen module)
from paper.store import PaperStore
from rotation import factors as F
from rotation import rules as R
from rotation import snapshots as SN
from rotation import universe as U
from rotation.store import RotationStore, canonical_json, dec_str, proposal_hash, sha256_hex

BENCHMARK = FC.SPY
HISTORY_CALENDAR_DAYS = 420                                      # enough calendar days for 252 completed sessions
VALID, NO_ELIGIBLE, INSUFFICIENT, TURNOVER_EXCEEDED, DATA_STALE, INPUT_ERROR = (
    "VALID", "NO_ELIGIBLE_CANDIDATES", "INSUFFICIENT_CANDIDATES", "TURNOVER_LIMIT_EXCEEDED", "DATA_STALE", "INPUT_ERROR")
MONEY, PRICE = Decimal("0.01"), Decimal("0.0001")
SOURCE_MISMATCH_NOTE = ("Proposal based on Robinhood holdings — the Alpaca paper account's own positions and rules apply at "
                        "Preview; nothing here is a Robinhood action.")


class EngineError(ValueError):
    """A pre-run input problem (nothing is persisted)."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class RotationResult:
    run: dict
    candidates: List[dict] = field(default_factory=list)
    targets: List[dict] = field(default_factory=list)
    items: List[dict] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.run["status"]


# ---- helpers ------------------------------------------------------------------------------------------------------------------------

def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _q(d: Decimal, places: Decimal) -> Decimal:
    return d.quantize(places, rounding=ROUND_HALF_EVEN)


def _closes_to(ser: Optional[BarSeries], T: date):
    """(closes, volumes) as Decimals for every bar dated <= T, or (None, None) when the symbol has no bar dated T."""
    if ser is None or not len(ser) or not ser.has(T):
        return None, None
    i = ser.index[T]
    return [F.to_decimal(b.close) for b in ser.bars[:i + 1]], [F.to_decimal(b.volume) for b in ser.bars[:i + 1]]


def _bars_hash(ser: Optional[BarSeries], T: date) -> Optional[str]:
    if ser is None or not len(ser):
        return None
    rows = [(d.isoformat(), str(b.timestamp), b.open, b.high, b.low, b.close, b.volume) for d, b in zip(ser.dates, ser.bars) if d <= T]
    return bars_content_hash(rows)


def input_hash(config_hash: str, universe_hash: str, T: Optional[date], bar_hashes: Dict[str, Optional[str]], snapshot: Optional[SN.PortfolioSnapshot]) -> str:
    """Deterministic inputs only: config, universe, session, SPY, the exact bars, and the normalised cash + quantities."""
    return sha256_hex(canonical_json({"config_hash": config_hash, "universe_hash": universe_hash, "data_session": T.isoformat() if T else None,
                                      "benchmark": BENCHMARK, "bars": dict(sorted(bar_hashes.items())),
                                      "snapshot": snapshot.deterministic_inputs() if snapshot else None}))


def _config_decimals(cfg: dict) -> dict:
    c = cfg["config"]
    return {"weights": {k: Decimal(v) for k, v in c["weights"].items()}, "portfolio_size": int(c["portfolio_size"]),
            "exit_rank": int(c["exit_rank"]), "cash_buffer_pct": Decimal(c["cash_buffer_pct"]),
            "rebalance_threshold": Decimal(c["rebalance_threshold"]), "max_turnover": Decimal(c["max_turnover_per_rotation"]),
            "min_price": Decimal(c["min_price"]), "min_adv": Decimal(c["min_avg_dollar_volume"]),
            "min_history": int(c["min_history_sessions"]), "max_snapshot_age_min": int(c["max_snapshot_age_min"]),
            "excluded": set(c["excluded_symbols"])}


def _run_base(cfg: dict, universe: U.ResolvedUniverse, portfolio_source: str, snapshot: Optional[SN.PortfolioSnapshot], now: datetime,
              T: Optional[date], market_data_requests: int) -> dict:
    return {"config_id": cfg["config_id"], "config_hash": cfg["config_hash"], "run_at": _iso(now), "benchmark": BENCHMARK,
            "data_session": T.isoformat() if T else None, "universe_source": universe.source, "universe_ref": universe.ref,
            "universe_json": list(universe.symbols), "universe_hash": universe.universe_hash, "portfolio_source": portfolio_source,
            "portfolio_snapshot_at": snapshot.snapshot_at if snapshot else None, "snapshot_status": snapshot.status if snapshot else None,
            "snapshot_cash": snapshot.cash if snapshot else None,
            "positions_json": snapshot.deterministic_inputs()["positions"] if snapshot else None,
            "source_meta_json": snapshot.source_meta if snapshot else None,
            "source_mismatch_note": SOURCE_MISMATCH_NOTE if portfolio_source == SN.ROBINHOOD_READ_ONLY else None,
            "reference_equity": None, "current_cash_weight": None, "target_cash_weight": None, "status_detail": None,
            "n_universe": len(universe.symbols), "n_eligible": 0, "n_selected": 0, "turnover": None,
            "market_data_requests": market_data_requests, "completed_at": _iso(now)}


def failed_result(cfg: dict, universe: U.ResolvedUniverse, portfolio_source: str, snapshot: Optional[SN.PortfolioSnapshot], now: datetime,
                  status: str, detail: str, *, T: Optional[date] = None, bar_hashes: Optional[Dict[str, Optional[str]]] = None,
                  market_data_requests: int = 0) -> RotationResult:
    """A persisted runtime failure (DATA_STALE / INPUT_ERROR before any candidate exists)."""
    run = _run_base(cfg, universe, portfolio_source, snapshot, now, T, market_data_requests)
    run.update(status=status, status_detail=detail[:300], input_hash=input_hash(cfg["config_hash"], universe.universe_hash, T, bar_hashes or {}, snapshot),
               proposal_hash=proposal_hash(status, None, None, None, [], []))
    return RotationResult(run=run)


# ---- the deterministic computation --------------------------------------------------------------------------------------------------

def compute_rotation(cfg: dict, universe: U.ResolvedUniverse, series: Dict[str, BarSeries], snapshot: SN.PortfolioSnapshot, T: date, *,
                     now: datetime, conflicts: Optional[Set[str]] = None, market_data_requests: int = 0) -> RotationResult:
    """Pure: the same (config, universe, bars, snapshot cash + quantities, T) always gives the same result and hashes."""
    p = _config_decimals(cfg)
    conflicts = set(conflicts or ())
    holdings = snapshot.quantities()
    symbols = sorted(set(universe.symbols) | set(holdings))
    bar_hashes = {s: _bars_hash(series.get(s), T) for s in symbols + [BENCHMARK]}
    base = _run_base(cfg, universe, snapshot.source, snapshot, now, T, market_data_requests)
    base["input_hash"] = input_hash(cfg["config_hash"], universe.universe_hash, T, bar_hashes, snapshot)

    def fail(status: str, detail: str) -> RotationResult:
        run = {**base, "status": status, "status_detail": detail[:300], "proposal_hash": proposal_hash(status, None, None, None, [], [])}
        return RotationResult(run=run)

    spy_closes, _ = _closes_to(series.get(BENCHMARK), T)
    if spy_closes is None:
        return fail(INPUT_ERROR, f"No {BENCHMARK} bar for the decision session {T.isoformat()}.")

    # reference prices (completed-session close at T) and the deterministic valuation — a held symbol must be priced
    ref_price: Dict[str, Decimal] = {}
    for s in symbols:
        closes, _ = _closes_to(series.get(s), T)
        if closes and closes[-1] is not None and closes[-1] > 0:
            ref_price[s] = _q(closes[-1], PRICE)
    unpriced = sorted(s for s in holdings if s not in ref_price)
    if unpriced:
        return fail(INPUT_ERROR, f"No reference price at {T.isoformat()} for held symbol(s): {', '.join(unpriced[:5])}.")
    try:
        val = R.current_weights(snapshot.cash, holdings, ref_price)
    except ValueError as exc:
        return fail(INPUT_ERROR, str(exc))
    equity, current_w, current_cash_w = val["reference_equity"], val["weights"], val["cash_weight"]

    # eligibility E1–E10 and raw factors
    candidates: Dict[str, dict] = {}
    raw_ok: Dict[str, dict] = {}
    for s in universe.symbols:
        reasons: List[str] = []
        ser = series.get(s)
        closes, volumes = _closes_to(ser, T)
        if not RU.SYMBOL_RE.match(s):
            reasons.append("INVALID_SYMBOL")                                                   # E1
        if closes is None:
            reasons.append("DATA_STALE" if ser is not None and len(ser) else "NO_REFERENCE_PRICE")    # E6 / E3
            raw = None
        else:
            if len(closes) < p["min_history"]:
                reasons.append("INSUFFICIENT_HISTORY")                                         # E2
            price = ref_price.get(s)
            if price is None:
                reasons.append("NO_REFERENCE_PRICE")                                           # E3
            elif price < p["min_price"]:
                reasons.append("BELOW_MIN_PRICE")                                              # E4
            raw = F.compute_factors(closes, volumes, spy_closes)
            if raw["liquidity"] is not None and raw["liquidity"] < p["min_adv"]:
                reasons.append("LOW_LIQUIDITY")                                                # E5
            if not F.complete(raw):
                reasons.append("FACTOR_INPUT_MISSING")                                         # E7
        if s in p["excluded"]:
            reasons.append("EXCLUDED")                                                         # E8
        if s in conflicts:
            reasons.append("CONFLICTING_STATE")                                                # E9
        if universe.rules_met(s) is False:
            reasons.append("SCANNER_NOT_MET")                                                  # E10
        eligible = not reasons
        candidates[s] = {"symbol": s, "eligible": eligible, "reasons": reasons,
                         "raw": {k: dec_str(v) for k, v in (raw or {}).items()}, "scores": None, "composite": None, "rank": None,
                         "reference_price": ref_price.get(s), "avg_dollar_volume": (raw or {}).get("liquidity"), "flags": snapshot.flags(s)}
        if eligible:
            raw_ok[s] = raw

    def finish(status: str, detail: Optional[str], targets: List[dict], items: List[dict], turnover, target_cash_w, n_selected: int) -> RotationResult:
        run = {**base, "status": status, "status_detail": detail, "reference_equity": equity, "current_cash_weight": current_cash_w,
               "target_cash_weight": target_cash_w, "n_eligible": len(raw_ok), "n_selected": n_selected, "turnover": turnover,
               "proposal_hash": proposal_hash(status, turnover, current_cash_w, target_cash_w, targets, items)}
        return RotationResult(run=run, candidates=[candidates[s] for s in universe.symbols], targets=targets, items=items)

    if not raw_ok:
        return finish(NO_ELIGIBLE, "No universe symbol passed every eligibility rule.", [], [], None, None, 0)

    # scores, composite, ranking
    scores = R.factor_scores(raw_ok)
    entries = {s: {**scores[s], "composite": R.composite(scores[s], p["weights"])} for s in scores}
    ranked = R.rank_symbols(entries)
    rank_of = R.ranks(ranked)
    for s in ranked:
        candidates[s].update(scores={k: dec_str(v) for k, v in scores[s].items()}, composite=entries[s]["composite"], rank=rank_of[s])

    # rank buffer, static equal weights, actions, turnover
    sel = R.select(ranked, holdings, p["portfolio_size"], p["exit_rank"])
    weights, target_cash_w = R.allocate(sel["selected"], p["portfolio_size"], p["cash_buffer_pct"])
    targets = []
    for s in sel["selected"]:
        notional = _q(weights[s] * equity, MONEY)
        est = int((notional / ref_price[s]).to_integral_value(rounding=ROUND_FLOOR))
        flags = list(candidates[s]["flags"])
        if est > RU.MAX_QTY:
            est, flags = RU.MAX_QTY, flags + ["CAPPED_AT_MAX_QTY"]
        targets.append({"symbol": s, "rank": rank_of[s], "target_weight": weights[s], "reference_price": ref_price[s],
                        "target_notional": notional, "est_target_qty": est, "reason": sel["reasons"][s], "flags": flags})
    items = []
    for s in sorted(set(sel["selected"]) | set(holdings)):
        held = s in holdings
        cw, tw = current_w.get(s, Decimal(0)), weights.get(s, Decimal(0))
        diff = R.q(tw - cw)
        qty = holdings.get(s, Decimal(0))
        est = int(qty.to_integral_value(rounding=ROUND_FLOOR)) if (held and tw == 0) else R.est_qty_diff(diff, equity, ref_price[s])
        action, reason = R.classify(held, cw, tw, p["rebalance_threshold"], est)
        if action == R.EXIT:
            reason = sel["exits"].get(s, reason)
        items.append({"symbol": s, "current_qty": qty, "current_weight": cw, "target_weight": tw, "weight_diff": diff, "est_qty_diff": est,
                      "side_hint": R.side_hint(action), "action": action, "reason": reason, "handoff_allowed": 0})
    turnover = R.turnover(current_w, weights, current_cash_w, target_cash_w)
    if turnover > p["max_turnover"]:
        status, detail = TURNOVER_EXCEEDED, f"Turnover {turnover} exceeds the limit {p['max_turnover']}."
    elif len(sel["selected"]) < p["portfolio_size"]:
        status, detail = INSUFFICIENT, f"{len(sel['selected'])} of {p['portfolio_size']} slots could be filled."
    else:
        status, detail = VALID, None
    for it in items:
        it["handoff_allowed"] = 1 if (status == VALID and it["action"] in (R.ADD, R.INCREASE, R.DECREASE, R.EXIT) and it["est_qty_diff"] > 0) else 0
    return finish(status, detail, targets, items, turnover, target_cash_w, len(sel["selected"]))


_RUN_DECIMALS = ("snapshot_cash", "reference_equity", "current_cash_weight", "target_cash_weight", "turnover")
_ROW_DECIMALS = ("composite", "reference_price", "avg_dollar_volume", "target_weight", "target_notional", "current_qty",
                 "current_weight", "weight_diff")


def _strings(row: dict, keys) -> dict:
    return {k: (dec_str(v) if k in keys else v) for k, v in row.items()}


def public(res: RotationResult) -> dict:
    """The result with every Decimal in the canonical string form the store persists (what callers and the API see)."""
    return {"run": _strings(res.run, _RUN_DECIMALS), "candidates": [_strings(c, _ROW_DECIMALS) for c in res.candidates],
            "targets": [_strings(t, _ROW_DECIMALS) for t in res.targets], "items": [_strings(i, _ROW_DECIMALS) for i in res.items]}


# ---- E9 inputs (existing read-only stores) ----------------------------------------------------------------------------------------------

def default_conflicts(path: Path, portfolio_source: str) -> Set[str]:
    """Symbols with an unresolved Stage 4.6B intent, plus pending Stage 4.5 orders for the local simulator. Robinhood
    holdings contribute nothing here (no pending-order inference; the Robinhood order history is never read)."""
    out: Set[str] = set()
    from paper import alpaca_order_store as AOS                 # read-only use of the frozen Stage 4.6B store
    st = AOS.OrderStore(path)
    if st.exists():
        out |= {r["symbol"].upper() for r in st.in_states(RU.UNRESOLVED)}
    if portfolio_source == SN.LOCAL_SIMULATOR:
        ps = PaperStore(path)
        acct = ps.account()
        if acct:
            out |= {o["symbol"].upper() for o in ps.orders(acct["account_id"], status="PENDING")}
    return out


# ---- orchestration ------------------------------------------------------------------------------------------------------------------------

def run_rotation(store: RotationStore, config_id: str, universe_spec: dict, portfolio_source: str, snapshot: Optional[SN.PortfolioSnapshot], *,
                 now: Optional[datetime] = None, path: Optional[Path] = None, cache: Optional[FC.BarCache] = None, fetch_fn=None, client=None,
                 conflicts_fn: Optional[Callable[[Path, str], Set[str]]] = None, watchlist_fn=None, persist: bool = True) -> dict:
    """Resolve → check the loaded snapshot → bars (≤ 1 request) → compute → persist. Returns the stored run row (plus the
    result) or raises EngineError / UniverseError for a pre-run input problem (nothing persisted). The snapshot must have been
    loaded explicitly beforehand; this function never loads or refreshes one and never contacts a broker."""
    now = now or datetime.now(timezone.utc)
    path = Path(path) if path else store.path
    cfg = store.config(config_id)
    if cfg is None:
        raise EngineError("INVALID_CONFIG", "Unknown configuration.", 404)
    if portfolio_source not in SN.SOURCES:
        raise EngineError("INVALID_PORTFOLIO_SOURCE", f"Unknown portfolio source {portfolio_source!r}.")
    if snapshot is not None and snapshot.source != portfolio_source:
        raise EngineError("SNAPSHOT_SOURCE_MISMATCH", "The loaded snapshot belongs to another portfolio source.")
    universe = U.resolve_universe(universe_spec.get("source"), universe_spec.get("ref"), universe_spec.get("symbols"), path=path,
                                  watchlist_fn=watchlist_fn)
    p = _config_decimals(cfg)

    def persist_result(res: RotationResult) -> dict:
        if persist:
            res.run["run_id"] = store.insert_run(res.run, res.candidates, res.targets, res.items)
        return public(res)

    problem = SN.freshness(snapshot, now, p["max_snapshot_age_min"])
    if problem:
        return persist_result(failed_result(cfg, universe, portfolio_source, snapshot, now, problem[0], problem[1]))
    last_complete = B.last_complete_session_date(now)
    symbols = sorted(set(universe.symbols) | set(snapshot.symbols()) | {BENCHMARK})
    try:
        series, _prov, fetched = FC.load_bars(RO.ReadOnlyBacktestStore(path), symbols, last_complete - timedelta(days=HISTORY_CALENDAR_DAYS),
                                              last_complete, now, cache if cache is not None else FC.BAR_CACHE, fetch_fn=fetch_fn, client=client)
    except Exception as exc:  # noqa: BLE001 - market data unavailable: a persisted INPUT_ERROR, type name only
        return persist_result(failed_result(cfg, universe, portfolio_source, snapshot, now, INPUT_ERROR,
                                            f"Market data unavailable ({type(exc).__name__})."))
    requests = int(fetched.get("requests", 0))
    spy = series.get(BENCHMARK)
    if spy is None or not len(spy):
        return persist_result(failed_result(cfg, universe, portfolio_source, snapshot, now, INPUT_ERROR, f"No {BENCHMARK} bars are available.",
                                            market_data_requests=requests))
    sess = FC.resolve_session(series, last_complete)
    if not sess.get("T"):
        return persist_result(failed_result(cfg, universe, portfolio_source, snapshot, now, DATA_STALE, sess.get("error") or "No decision session.",
                                            market_data_requests=requests))
    conflicts = (conflicts_fn or default_conflicts)(path, portfolio_source)
    return persist_result(compute_rotation(cfg, universe, series, snapshot, sess["T"], now=now, conflicts=conflicts, market_data_requests=requests))
