"""
rotation_walkforward/grid.py — bounded, explicit, deterministic candidate generation (DESIGN_49 §3).

Candidates are Stage 4.7 configurations validated by the EXISTING rotation.store.normalise_config. A one-level grid varies
the listed dimensions around the base configuration; a weight dimension sets one weight and rescales the other five
proportionally so the six still sum to exactly 1.000000. Invalid candidates are rejected WITH their reason, duplicates
(same config hash) are removed keeping the first, and the ordered list is cut at max_candidates. No random search.
"""
from __future__ import annotations

import itertools
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
from typing import Dict, List, Optional, Tuple

from rotation import rules as R
from rotation import store as S

SCALAR_DIMENSIONS = ("portfolio_size", "exit_rank", "cash_buffer_pct", "max_turnover_per_rotation", "rebalance_threshold")
WEIGHT_DIMENSIONS = tuple(f"weights.{k}" for k in R.WEIGHT_KEYS)
DIMENSIONS = SCALAR_DIMENSIONS + WEIGHT_DIMENSIONS
MAX_VALUES_PER_DIMENSION = 7
SIX = Decimal("0.000001")


class GridError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def rescale_weights(weights: Dict[str, str], key: str, value) -> Dict[str, str]:
    """Set `key` to `value` and rescale the other weights proportionally; the 6-dp residual goes to the largest other weight."""
    v = Decimal(str(value)).quantize(SIX, rounding=ROUND_HALF_EVEN)
    if not Decimal(0) <= v <= Decimal(1):
        raise GridError("INVALID_WEIGHT", f"{key} must be between 0 and 1")
    others = {k: Decimal(str(weights[k])) for k in R.WEIGHT_KEYS if k != key}
    total = sum(others.values(), Decimal(0))
    rest = Decimal(1) - v
    if total == 0:
        if rest != 0:
            raise GridError("INVALID_WEIGHT", f"{key} cannot be rescaled: every other weight is 0")
        out = {k: Decimal(0) for k in others}
    else:
        out = {k: (w / total * rest).quantize(SIX, rounding=ROUND_DOWN) for k, w in others.items()}
        residual = rest - sum(out.values(), Decimal(0))
        largest = max(out, key=lambda k: (out[k], k))
        out[largest] = (out[largest] + residual).quantize(SIX, rounding=ROUND_HALF_EVEN)
    out[key] = v
    return {k: format(out[k], "f") for k in R.WEIGHT_KEYS}


def apply(base: Dict, assignments: Dict[str, object]) -> Dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in base.items()}
    for dim, value in assignments.items():
        if dim.startswith("weights."):
            cfg["weights"] = rescale_weights(cfg["weights"], dim.split(".", 1)[1], value)
        elif dim in ("portfolio_size", "exit_rank"):
            cfg[dim] = int(value)
        else:
            cfg[dim] = str(value)
    return cfg


def label_for(assignments: Dict[str, object]) -> str:
    return "base" if not assignments else " ".join(f"{k}={v}" for k, v in sorted(assignments.items()))


def validate_candidate(label: str, config: Dict) -> Tuple[Optional[dict], Optional[dict]]:
    """(candidate row, None) or (None, rejection) — never a silent drop."""
    try:
        canon = S.normalise_config(config)
    except (S.StoreError, R.ConfigError) as exc:
        return None, {"label": label, "code": exc.code, "reason": exc.message}
    return {"label": label, "config": canon, "config_hash": S.config_hash(canon)}, None


def generate(base_config: Dict, explicit: Optional[List[Dict]], grid_spec: Optional[Dict], max_candidates: int) -> dict:
    """{'candidates': [...], 'rejected': [...], 'n_discarded': int, 'n_enumerated': int}. Order: the base first, explicit
    candidates in the given order, then the grid in a fixed order (dimensions sorted, values as given)."""
    rows: List[dict] = []
    rejected: List[dict] = []
    seen = set()

    def add(label, config):
        row, rej = validate_candidate(label, config)
        if rej:
            rejected.append(rej)
            return
        if row["config_hash"] in seen:
            return
        seen.add(row["config_hash"])
        rows.append(row)

    add("base", base_config)
    for i, c in enumerate(explicit or []):
        if not isinstance(c, dict):
            rejected.append({"label": f"explicit[{i}]", "code": "INVALID_CONFIG", "reason": "candidate must be an object"})
            continue
        add(str(c.get("label") or f"explicit[{i}]"), {k: v for k, v in c.items() if k != "label"})
    n_enumerated = 1 + len(explicit or [])
    if grid_spec:
        dims = grid_spec.get("dimensions") if isinstance(grid_spec, dict) else None
        if not isinstance(dims, dict) or not dims:
            raise GridError("INVALID_GRID", "grid.dimensions must be a non-empty object of {dimension: [values]}")
        unknown = sorted(set(dims) - set(DIMENSIONS))
        if unknown:
            raise GridError("INVALID_GRID", f"unknown grid dimensions {unknown}; allowed: {list(DIMENSIONS)}")
        names = sorted(dims)
        values = []
        for n in names:
            vs = dims[n]
            if not isinstance(vs, list) or not vs or len(vs) > MAX_VALUES_PER_DIMENSION:
                raise GridError("INVALID_GRID", f"grid dimension {n} needs 1-{MAX_VALUES_PER_DIMENSION} values")
            values.append([str(v) for v in vs])
        combos = list(itertools.product(*values))
        if len(combos) > 10_000:
            raise GridError("GRID_TOO_LARGE", f"{len(combos)} grid combinations exceed the 10,000 enumeration bound")
        n_enumerated += len(combos)
        for combo in combos:
            assignments = dict(zip(names, combo))
            try:
                add(label_for(assignments), apply(base_config, assignments))
            except GridError as exc:
                rejected.append({"label": label_for(assignments), "code": exc.code, "reason": exc.message})
    n_discarded = max(0, len(rows) - max_candidates)
    return {"candidates": rows[:max_candidates], "rejected": rejected, "n_discarded": n_discarded, "n_enumerated": n_enumerated}


def neighbourhood(base_config: Dict) -> List[Tuple[str, Dict]]:
    """The fixed one-at-a-time parameter neighbourhood used by the sensitivity report (DESIGN_49 §7)."""
    out: List[Tuple[str, Dict]] = []
    ps, er = int(base_config["portfolio_size"]), int(base_config["exit_rank"])
    buf, mt = Decimal(str(base_config["cash_buffer_pct"])), Decimal(str(base_config["max_turnover_per_rotation"]))
    for lab, asg in (("portfolio_size-1", {"portfolio_size": ps - 1}), ("portfolio_size+1", {"portfolio_size": ps + 1}),
                     ("exit_rank-1", {"exit_rank": er - 1}), ("exit_rank+1", {"exit_rank": er + 1}),
                     ("cash_buffer_pct-0.05", {"cash_buffer_pct": str(buf - Decimal("0.05"))}), ("cash_buffer_pct+0.05", {"cash_buffer_pct": str(buf + Decimal("0.05"))}),
                     ("max_turnover-0.10", {"max_turnover_per_rotation": str(mt - Decimal("0.10"))}), ("max_turnover+0.10", {"max_turnover_per_rotation": str(mt + Decimal("0.10"))})):
        out.append((lab, asg))
    for k in R.WEIGHT_KEYS:
        w = Decimal(str(base_config["weights"][k]))
        out.append((f"weights.{k}-0.05", {f"weights.{k}": str(w - Decimal("0.05"))}))
        out.append((f"weights.{k}+0.05", {f"weights.{k}": str(w + Decimal("0.05"))}))
    return out
