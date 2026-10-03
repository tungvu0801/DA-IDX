"""
strategy/evaluate.py — the ONE meaning of every strategy operator (pure; Stage 3.1 defines it, later stages call it).

Stage 3.2 and any forward tester must evaluate a validated spec through these functions against a feature
SNAPSHOT built with strategy.features extractors from data available at the decision time — never re-implement the
rules. This module fetches nothing, simulates nothing and knows nothing about positions or orders.

Semantics (documented and tested):
  * a missing value (None / "UNAVAILABLE") never satisfies a condition — the condition is "not met", and the trace
    says the value was unavailable (so a data gap can never trigger an entry);
  * "between" is inclusive on both ends; "==" on numbers uses a 1e-9 absolute tolerance;
  * "in" / "not_in" compare exact registered choices; is_true / is_false only accept real booleans;
  * ALL = every child met; ANY = at least one child met (children are evaluated in spec order, all of them, so the
    trace is complete).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from strategy import features as F

MISSING = (None, "UNAVAILABLE")


def _missing(v) -> bool:
    return v in MISSING or (isinstance(v, float) and math.isnan(v))


def condition_met(cond: dict, snapshot: Dict[str, Any]) -> Tuple[bool, dict]:
    f = F.get(cond["feature"])
    v = snapshot.get(cond["feature"])
    trace = {"feature": cond["feature"], "op": cond["op"], "expected": cond.get("value"), "actual": v}
    if f is None:
        return False, {**trace, "result": "UNKNOWN_FEATURE"}
    if _missing(v):
        return False, {**trace, "result": "UNAVAILABLE"}
    op, want = cond["op"], cond.get("value")
    if f.data_type == "BOOLEAN":
        ok = isinstance(v, bool) and (v if op == "is_true" else not v)
    elif f.data_type == "NUMBER":
        x = float(v)
        ok = {">": lambda: x > want, ">=": lambda: x >= want, "<": lambda: x < want, "<=": lambda: x <= want,
              "==": lambda: abs(x - want) <= 1e-9, "between": lambda: want[0] <= x <= want[1]}[op]()
    else:
        ok = {"==": lambda: v == want, "!=": lambda: v != want, "in": lambda: v in want,
              "not_in": lambda: v not in want}[op]()
    return bool(ok), {**trace, "result": "MET" if ok else "NOT_MET"}


def group_met(group: dict, snapshot: Dict[str, Any]) -> Tuple[bool, List[dict]]:
    results, traces = [], []
    for c in group["conditions"]:
        if "logic" in c:
            ok, sub = group_met(c, snapshot)
            traces.append({"group": c["logic"], "result": "MET" if ok else "NOT_MET", "children": sub})
        else:
            ok, t = condition_met(c, snapshot)
            traces.append(t)
        results.append(ok)
    met = all(results) if group["logic"] == "ALL" else any(results)
    return bool(results) and met, traces
