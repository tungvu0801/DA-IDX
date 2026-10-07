"""
rotation_walkforward/selection.py — TRAIN-only candidate ranking with deterministic tie-breakers (DESIGN_49 §4).

Inputs are the Stage 4.8 metric dicts of the TRAIN replays. Nothing from a TEST replay is accepted here.
"""
from __future__ import annotations

from typing import Dict, List, Optional

TIE_BREAKERS = ("metric desc (None last)", "max_drawdown higher (less negative)", "mean_turnover lower", "config_hash asc")


def _clamp(x: Optional[float]) -> float:
    return 0.0 if x is None else max(0.0, min(1.0, x))


def composite_train_score(m: dict) -> Optional[float]:
    """0.5·clamp(Sharpe/2) + 0.25·clamp(1 + max_dd/0.5) + 0.25·clamp(1 − mean turnover); None when Sharpe is undefined."""
    if m.get("sharpe") is None:
        return None
    return 0.5 * _clamp(m["sharpe"] / 2.0) + 0.25 * _clamp(1.0 + (m.get("max_drawdown") or 0.0) / 0.5) + 0.25 * _clamp(1.0 - (m.get("mean_turnover") or 0.0))


def metric_value(metric: str, m: Optional[dict], max_drawdown_limit: float) -> Optional[float]:
    if not m:
        return None
    if metric == "SHARPE":
        return m.get("sharpe")
    if metric == "SORTINO":
        return m.get("sortino")
    if metric == "CAGR":
        return m.get("cagr")
    if metric == "DD_CONSTRAINED_SHARPE":
        s = m.get("sharpe")
        if s is None:
            return None
        dd = m.get("max_drawdown")
        return s if dd is None or dd >= max_drawdown_limit else s - 1000.0           # constrained candidates rank below every unconstrained one
    if metric == "COMPOSITE":
        return composite_train_score(m)
    raise ValueError(f"unknown selection metric {metric!r}")


def sort_key(row: dict):
    v = row.get("train_metric_value")
    dd = (row.get("train_metrics") or {}).get("max_drawdown")
    to = (row.get("train_metrics") or {}).get("mean_turnover")
    return (0 if v is not None else 1, -(v if v is not None else 0.0), -(dd if dd is not None else -9.0), to if to is not None else 9.0, row["config_hash"])


def rank(candidates: List[dict], metric: str, max_drawdown_limit: float) -> List[dict]:
    """Each candidate: {label, config_hash, config, train_status, train_metrics}. Returns the rows with train_metric_value and
    train_rank set, best first, deterministically."""
    rows = []
    for c in candidates:
        m = c.get("train_metrics") if c.get("train_status") == "COMPLETED" else None
        rows.append({**c, "train_metric_value": metric_value(metric, m, max_drawdown_limit)})
    rows.sort(key=sort_key)
    for i, r in enumerate(rows):
        r["train_rank"] = i + 1
    return rows


def tie_break_record(selected: dict, metric: str) -> dict:
    m = selected.get("train_metrics") or {}
    return {"metric": metric, "value": selected.get("train_metric_value"), "max_drawdown": m.get("max_drawdown"), "mean_turnover": m.get("mean_turnover"),
            "config_hash": selected["config_hash"], "order": list(TIE_BREAKERS)}
