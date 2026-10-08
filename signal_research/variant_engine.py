"""
signal_research/variant_engine.py — the research proposal function and its Stage 4.9 evaluator (DESIGN_52 §1).

compute_variant() calls the production Stage 4.7 engine for eligibility, factors, scores and the composite, then applies the
research rules in order: ranking (GLOBAL_RANK / SECTOR_NEUTRAL_RANK) → selection (rank buffer, optionally sector-capped) →
static equal weights → regime exposure overlay → actions / turnover / status with the same rotation.rules primitives. With
GLOBAL_RANK, no cap and NO_OVERLAY the proposal is identical to the production one. handoff_allowed is always 0: nothing here
is ever handed to any order path.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR
from typing import Dict, List, Mapping, Optional, Set

from backtest.replay import BarSeries
from paper import alpaca_order_rules as RU                      # MAX_QTY (pure constant of the frozen module; same use as the Stage 4.7 engine)
from rotation import engine as E
from rotation import rules as R
from rotation import snapshots as SN
from rotation import universe as U
from rotation.store import canonical_json, proposal_hash, sha256_hex
from rotation_backtest import simulator as SIM
from rotation_walkforward import engine as WF
from signal_research import regime as RGO
from signal_research import sector as SC
from signal_research import variants as V


def rules_hash(research: Mapping) -> str:
    return sha256_hex(canonical_json(dict(research)))


def compute_variant(cfg: dict, universe: U.ResolvedUniverse, series: Dict[str, BarSeries], snapshot: SN.PortfolioSnapshot, T: date, *, now: datetime,
                    conflicts: Optional[Set[str]] = None, research: Mapping, sector_map: Mapping[str, str]) -> E.RotationResult:
    rr = E.compute_rotation(cfg, universe, series, snapshot, T, now=now, conflicts=conflicts)
    rh = rules_hash(research)
    rr.run["input_hash"] = sha256_hex(rr.run["input_hash"] + rh)
    rr.run["research_rules_hash"] = rh
    if rr.run["status"] in (E.INPUT_ERROR, E.NO_ELIGIBLE, E.DATA_STALE):
        return rr                                                                           # fail closed exactly as production
    p = E._config_decimals(cfg)
    holdings = snapshot.quantities()
    ref_price: Dict[str, Decimal] = {c["symbol"]: c["reference_price"] for c in rr.candidates if c.get("reference_price") is not None}
    for s in holdings:
        if s not in ref_price:
            closes, _ = E._closes_to(series.get(s), T)
            ref_price[s] = E._q(closes[-1], E.PRICE)                                        # production already priced every holding
    val = R.current_weights(snapshot.cash, holdings, ref_price)
    equity, current_w, current_cash_w = val["reference_equity"], val["weights"], val["cash_weight"]
    elig = {c["symbol"]: c for c in rr.candidates if c["eligible"]}
    entries = {s: {**{k: Decimal(v) for k, v in c["scores"].items()}, "composite": Decimal(c["composite"])} for s, c in elig.items()}
    # 1. ranking
    ranked = SC.sector_neutral_rank(entries, sector_map) if research["ranking"] == V.SECTOR_NEUTRAL_RANK else R.rank_symbols(entries)
    rank_of = R.ranks(ranked)
    candidates = []
    for c in rr.candidates:
        c2 = dict(c)
        c2["rank"] = rank_of.get(c["symbol"])
        candidates.append(c2)
    # 2. selection (rank buffer, optionally capped)
    cap = research.get("max_sector_weight")
    pc = R.validate_portfolio_config(cfg["config"]["portfolio_size"], cfg["config"]["exit_rank"], cfg["config"]["cash_buffer_pct"], cfg["config"]["min_position_weight"],
                                     cfg["config"]["max_position_weight"], cfg["config"]["rebalance_threshold"], cfg["config"]["max_turnover_per_rotation"])
    if cap is not None:
        per_sector = SC.cap_count(Decimal(cap), pc["equal_weight"])
        sel = SC.select_capped(ranked, holdings, p["portfolio_size"], p["exit_rank"], sector_map, per_sector)
    else:
        sel = {**R.select(ranked, holdings, p["portfolio_size"], p["exit_rank"]), "capped": [], "infeasible": False}
    # 3. static equal weights, 4. regime exposure overlay
    weights, _ = R.allocate(sel["selected"], p["portfolio_size"], p["cash_buffer_pct"])
    exposure, label = RGO.exposure_at(series.get(E.BENCHMARK), T, research["exposure_schedule"])
    weights, target_cash_w = RGO.scale_weights(weights, exposure)
    sec_w = SC.sector_weights(weights, sector_map)
    cap_exceeded = cap is not None and any(w > Decimal(cap) for w in sec_w.values())
    # 5. targets, items, turnover, status (production rules)
    targets = []
    for s in sel["selected"]:
        notional = E._q(weights[s] * equity, E.MONEY)
        est = int((notional / ref_price[s]).to_integral_value(rounding=ROUND_FLOOR))
        flags = list(elig[s]["flags"])
        if est > RU.MAX_QTY:
            est, flags = RU.MAX_QTY, flags + ["CAPPED_AT_MAX_QTY"]
        targets.append({"symbol": s, "rank": rank_of[s], "target_weight": weights[s], "reference_price": ref_price[s], "target_notional": notional, "est_target_qty": est,
                        "reason": sel["reasons"][s], "flags": flags})
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
        items.append({"symbol": s, "current_qty": qty, "current_weight": cw, "target_weight": tw, "weight_diff": diff, "est_qty_diff": est, "side_hint": R.side_hint(action),
                      "action": action, "reason": reason, "handoff_allowed": 0})
    turnover = R.turnover(current_w, weights, current_cash_w, target_cash_w)
    if sel["infeasible"] or cap_exceeded:
        status, detail = SC.SECTOR_CAP_INFEASIBLE, (f"The sector cap {cap} leaves {p['portfolio_size'] - len(sel['selected'])} slot(s) unfilled." if sel["infeasible"]
                                                   else f"A sector weight exceeds the cap {cap} after allocation.")
    elif turnover > p["max_turnover"]:
        status, detail = E.TURNOVER_EXCEEDED, f"Turnover {turnover} exceeds the limit {p['max_turnover']}."
    elif len(sel["selected"]) < p["portfolio_size"]:
        status, detail = E.INSUFFICIENT, f"{len(sel['selected'])} of {p['portfolio_size']} slots could be filled."
    else:
        status, detail = E.VALID, None
    run = {**rr.run, "status": status, "status_detail": detail, "reference_equity": equity, "current_cash_weight": current_cash_w, "target_cash_weight": target_cash_w,
           "n_eligible": len(elig), "n_selected": len(sel["selected"]), "turnover": turnover,
           "proposal_hash": proposal_hash(status, turnover, current_cash_w, target_cash_w, targets, items),
           "research": {"ranking": research["ranking"], "max_sector_weight": cap, "regime_overlay": research["regime_overlay"], "regime": label, "exposure": exposure,
                        "capped": list(sel.get("capped") or []), "sector_weights": {k: v for k, v in sec_w.items()}}}
    return E.RotationResult(run=run, candidates=candidates, targets=targets, items=items)


class ResearchEvaluator(WF.Evaluator):
    """The Stage 4.9 memoised evaluator; candidates carrying `research` rules replay through compute_variant, others (the
    benchmarks) through the production engine. The cache key is the candidate hash, which covers the rules."""

    def __init__(self, universe: U.ResolvedUniverse, series: Dict[str, BarSeries], now: datetime, sector_map: Mapping[str, str]):
        super().__init__(universe, series, now)
        self.sector_map = SC.normalise_map(sector_map)
        self.research_evaluations = 0

    def evaluate(self, cand: dict, start: str, end: str, frequency: str, initial_cash: str, cost_bps: str, slip_bps: str):
        research = cand.get("research")
        if not research:
            return super().evaluate(cand, start, end, frequency, initial_cash, cost_bps, slip_bps)
        key = (cand["config_hash"], start, end, frequency, initial_cash, cost_bps, slip_bps)
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        cfg_row = {"config_id": cand["config_hash"][:32], "config_hash": cand["config_hash"], "config": cand["config"]}
        defn = {"config_id": cfg_row["config_id"], "config_hash": cand["config_hash"], "start_date": start, "end_date": end, "rebalance_frequency": frequency,
                "initial_cash": initial_cash, "transaction_cost_bps": cost_bps, "slippage_bps": slip_bps, "benchmark": SIM.BENCHMARK}
        sector_map = self.sector_map

        def engine(cfg, universe, series, snapshot, T, *, now, conflicts=None, market_data_requests=0):
            return compute_variant(cfg, universe, series, snapshot, T, now=now, conflicts=conflicts, research=research, sector_map=sector_map)
        res = SIM.simulate(cfg_row, self.universe, self.series, defn, now=self.now, engine=engine)
        metrics = WF.MX.compute(defn, res.equity, res.trades, res.rebalances, res.completed_positions, res.total_costs) if res.status == "COMPLETED" else None
        self.evaluations += 1
        self.research_evaluations += 1
        self.cache[key] = (res, metrics)
        return res, metrics
