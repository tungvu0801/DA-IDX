"""
rotation_diagnostics/attribution.py — symbol / sector attribution, exposure, overlap and churn from the deterministic Stage 4.8
TEST replays (DESIGN_51 §3). Pure functions over SimulationResult objects and the bar series.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Dict, List, Optional

from backtest.replay import BarSeries
from rotation_backtest import simulator as SIM
from rotation.store import canonical_json, sha256_hex

UNMAPPED = "UNMAPPED"
MONEY = Decimal("0.01")


def sector_of(symbol: str, sector_map: Dict[str, str]) -> str:
    return sector_map.get(symbol, UNMAPPED)


def sector_map_hash(sector_map: Dict[str, str]) -> str:
    return sha256_hex(canonical_json({k: v for k, v in sorted(sector_map.items())}))


def window_symbol_pnl(res, series: Dict[str, BarSeries]) -> Dict[str, Decimal]:
    """P&L per symbol of ONE window: sells − buys − costs + final quantity × last close. Σ equals final equity − initial cash."""
    pnl: Dict[str, Decimal] = {}
    for t in res.trades:
        s = t["symbol"]
        if t["side"] == "BUY":
            pnl[s] = pnl.get(s, Decimal(0)) - t["notional"] - t["cost"]
        else:
            pnl[s] = pnl.get(s, Decimal(0)) + t["notional"] - t["cost"]
    if res.sessions:
        last = res.sessions[-1]
        for s, q in res.final_holdings.items():
            px = SIM.price_of(series.get(s), last, "close")
            if px is None:
                raise ValueError(f"no close for held symbol {s} at {last.isoformat()}")
            pnl[s] = pnl.get(s, Decimal(0)) + px * q
    return {s: v.quantize(MONEY, rounding=ROUND_HALF_EVEN) for s, v in pnl.items()}


def _shares(pnl: Dict[str, Decimal]):
    pos = sum((v for v in pnl.values() if v > 0), Decimal(0))
    ordered = sorted(pnl.items(), key=lambda kv: (-kv[1], kv[0]))
    share = {s: (float(v / pos) if pos > 0 and v > 0 else 0.0) for s, v in pnl.items()}
    top = lambda n: float(sum((v for _, v in ordered[:n] if v > 0), Decimal(0)) / pos) if pos > 0 else None  # noqa: E731
    return ordered, share, {"top1": top(1), "top3": top(3), "top5": top(5)}, pos


def attribute(results: List, series: Dict[str, BarSeries], sector_map: Dict[str, str], initial_cash: Decimal) -> dict:
    """Attribution across the TEST windows of one configuration (results = one SimulationResult per window)."""
    pnl: Dict[str, Decimal] = {}
    windows_held: Dict[str, int] = {}
    per_window: List[dict] = []
    overlaps: List[float] = []
    adds: List[int] = []
    removes: List[int] = []
    churns: List[float] = []
    sector_w_sum: Dict[str, float] = {}
    sector_w_max: Dict[str, float] = {}
    n_targets_rows = 0
    held_per_rebalance: List[int] = []
    for w, res in enumerate(results):
        if res.status != "COMPLETED":
            per_window.append({"window_index": w, "status": res.status, "pnl": None, "top": None})
            continue
        wp = window_symbol_pnl(res, series)
        for s, v in wp.items():
            pnl[s] = pnl.get(s, Decimal(0)) + v
        held_syms = set()
        prev = None
        for rb in res.rebalances:
            if not rb.get("executed"):
                continue
            tg = rb["proposal"]["targets"]
            cur = {t["symbol"] for t in tg}
            held_syms |= cur
            held_per_rebalance.append(len(cur))
            n_targets_rows += 1
            by_sector: Dict[str, float] = {}
            for t in tg:
                by_sector[sector_of(t["symbol"], sector_map)] = by_sector.get(sector_of(t["symbol"], sector_map), 0.0) + float(t["target_weight"])
            for sec, wgt in by_sector.items():
                sector_w_sum[sec] = sector_w_sum.get(sec, 0.0) + wgt
                sector_w_max[sec] = max(sector_w_max.get(sec, 0.0), wgt)
            if prev is not None and prev:
                overlaps.append(len(prev & cur) / len(prev))
                a, r = len(cur - prev), len(prev - cur)
                adds.append(a)
                removes.append(r)
                churns.append((a + r) / (2.0 * max(len(cur), 1)))
            prev = cur
        for s in held_syms:
            windows_held[s] = windows_held.get(s, 0) + 1
        ordered, _, top, _ = _shares(wp)
        total_w = sum(wp.values(), Decimal(0))
        per_window.append({"window_index": w, "status": "COMPLETED", "pnl": str(total_w), "return": float(total_w / initial_cash),
                           "top": [[s, str(v)] for s, v in ordered[:3]], "top1_share": top["top1"]})
    ordered, share, conc, pos = _shares(pnl)
    sectors: Dict[str, Decimal] = {}
    for s, v in pnl.items():
        sectors[sector_of(s, sector_map)] = sectors.get(sector_of(s, sector_map), Decimal(0)) + v
    s_ordered, s_share, s_conc, _ = _shares(sectors)
    rets = [pw["return"] for pw in per_window if pw.get("return") is not None]
    logs = [math.log1p(r) for r in rets if r > -1]
    total_log = sum(logs)
    best_share = (max(logs) / total_log) if logs and total_log > 0 else None
    avg_w = {sec: (sector_w_sum[sec] / n_targets_rows) for sec in sector_w_sum} if n_targets_rows else {}
    return {"symbols": [{"symbol": s, "sector": sector_of(s, sector_map), "pnl": str(v), "share_of_positive": share[s], "windows_held": windows_held.get(s, 0)} for s, v in ordered],
            "total_pnl": str(sum(pnl.values(), Decimal(0))), "total_positive_pnl": str(pos), "concentration": conc,
            "sectors": [{"sector": sec, "pnl": str(v), "share_of_positive": s_share[sec], "avg_weight": avg_w.get(sec), "max_weight": sector_w_max.get(sec)} for sec, v in s_ordered],
            "sector_concentration": {**s_conc, "max_sector_weight": max(sector_w_max.values()) if sector_w_max else None,
                                     "top_sector_share": s_conc["top1"]},
            "holdings": {"names_held_mean": (sum(held_per_rebalance) / len(held_per_rebalance)) if held_per_rebalance else None,
                         "overlap_mean": (sum(overlaps) / len(overlaps)) if overlaps else None, "added_mean": (sum(adds) / len(adds)) if adds else None,
                         "removed_mean": (sum(removes) / len(removes)) if removes else None, "churn_mean": (sum(churns) / len(churns)) if churns else None,
                         "rebalances_compared": len(overlaps)},
            "windows": per_window, "window_concentration": {"best_window_share_of_log_return": best_share, "n_positive_windows": sum(1 for r in rets if r > 0), "n_windows": len(rets)},
            "sector_map_hash": sector_map_hash(sector_map)}


def reconcile(attr: dict, results: List, initial_cash: Decimal) -> bool:
    """Σ symbol P&L == Σ sector P&L == Σ (final equity − initial cash) over the completed windows."""
    total = Decimal(attr["total_pnl"])
    sectors = sum((Decimal(s["pnl"]) for s in attr["sectors"]), Decimal(0))
    equity = sum(((res.equity[-1]["equity"] - initial_cash) for res in results if res.status == "COMPLETED" and res.equity), Decimal(0))
    return total == sectors and abs(total - equity) <= Decimal("0.05") * max(1, len(results))
