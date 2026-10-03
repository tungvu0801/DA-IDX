"""
strategy/spec.py — the Stage 3 STRATEGY SPEC (schema v1): validation, normalisation, canonical JSON + SHA-256,
backtest readiness, plain-language summary and version comparison. Pure and deterministic; no I/O, no Claude.

A spec is DATA only — registered feature ids, whitelisted operators and literal values. Nothing in a spec is ever
executed. Unknown fields are rejected (so nothing can be smuggled in), and every error is returned as a structured
{code, path, message[, feature]} item instead of an exception.

Schema v1 (top level — every key required unless noted):
  schema_version            1
  feature_registry_version  pinned by the server (strategy.features.REGISTRY_VERSION)
  feature_registry_fingerprint  pinned by the server (SHA-256 of every feature definition + parameter values)
  name                      1–80 characters
  description               0–500 characters (optional, default "")
  direction                 "LONG_ONLY"            (the only direction in Stage 3.1)
  timeframe                 "1D"                   (the only timeframe in Stage 3.1)
  execution                 {"decision": "AT_CLOSE", "fill": "NEXT_SESSION_OPEN"}   (fixed in v1, but hashed)
  universe                  {"type": "EXPLICIT_SYMBOLS", "symbols": [...], "origin": MANUAL|WATCHLIST_COPY|HOLDINGS_COPY}
  entry                     GROUP  — {"logic": "ALL"|"ANY", "conditions": [CONDITION | GROUP, ...]}
  exit                      GROUP + optional "invalidation", "target", "max_holding_days" (at least one exit rule)
  risk                      {"max_position_pct": 0.5–100, "max_open_positions": 1–50}
CONDITION: {"feature": id, "op": operator, "value": literal}   (no "value" for is_true / is_false)
Nesting: a group may contain groups, at most MAX_DEPTH levels deep; at most MAX_CONDITIONS conditions per side.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from strategy import features as F

SCHEMA_VERSION = 1
DIRECTIONS = ("LONG_ONLY",)
TIMEFRAMES = ("1D",)
EXECUTION = {"decision": "AT_CLOSE", "fill": "NEXT_SESSION_OPEN"}
UNIVERSE_TYPES = ("EXPLICIT_SYMBOLS",)
UNIVERSE_ORIGINS = ("MANUAL", "WATCHLIST_COPY", "HOLDINGS_COPY")
LOGICS = ("ALL", "ANY")
MAX_DEPTH = 2
MAX_CONDITIONS = 12
MAX_SYMBOLS = 100
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
INVALIDATION_METHODS = {"CLOSE_BELOW_ENTRY_SUPPORT": None, "PCT_BELOW_ENTRY": (0.5, 50.0)}
TARGET_METHODS = {"CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE": None, "PCT_ABOVE_ENTRY": (0.5, 200.0)}
EXIT_METHOD_FEATURES = {"CLOSE_BELOW_ENTRY_SUPPORT": "stock.support", "PCT_BELOW_ENTRY": "stock.close",
                        "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE": "stock.resistance", "PCT_ABOVE_ENTRY": "stock.close"}
MAX_HOLDING_DAYS = (1, 252)
RISK_LIMITS = {"max_position_pct": (0.5, 100.0), "max_open_positions": (1, 50)}
READY, FORWARD_ONLY, UNSUPPORTED = "BACKTEST_READY", "FORWARD_TEST_ONLY", "UNSUPPORTED"
READINESS_LABEL = {READY: "BACKTEST READY", FORWARD_ONLY: "FORWARD TEST ONLY", UNSUPPORTED: "UNSUPPORTED"}
TOP_KEYS = {"schema_version", "feature_registry_version", "feature_registry_fingerprint", "name", "description",
            "direction", "timeframe", "execution", "universe", "entry", "exit", "risk"}
RULE_KEYS = ("schema_version", "feature_registry_version", "feature_registry_fingerprint", "direction", "timeframe",
             "execution", "universe", "entry", "exit", "risk")          # behaviour only (no name / description)


class _Errors:
    def __init__(self):
        self.items: List[dict] = []

    def add(self, code: str, path: str, message: str, feature: Optional[str] = None):
        e = {"code": code, "path": path, "message": message}
        if feature:
            e["feature"] = feature
        self.items.append(e)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _num(v):
    """Canonical number: integral floats become ints (30.0 and 30 are the same rule)."""
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _text(v, path, err, *, required, max_len) -> str:
    if v is None and not required:
        return ""
    if not isinstance(v, str):
        err.add("INVALID_TEXT", path, "Must be text.")
        return ""
    t = " ".join(v.split())
    if required and not t:
        err.add("REQUIRED", path, "Required.")
    if len(t) > max_len:
        err.add("TOO_LONG", path, f"At most {max_len} characters.")
    if any(ord(c) < 32 for c in t):
        err.add("INVALID_TEXT", path, "Control characters are not allowed.")
    return t


# ---- conditions -----------------------------------------------------------------------------------------------

def _condition(c, path, err) -> Optional[dict]:
    if not isinstance(c, dict):
        err.add("INVALID_CONDITION", path, "A condition must be an object.")
        return None
    extra = set(c) - {"feature", "op", "value"}
    if extra:
        err.add("UNKNOWN_FIELD", path, f"Unknown field(s): {', '.join(sorted(extra))}.")
    fid, op = c.get("feature"), c.get("op")
    feat = F.get(fid) if isinstance(fid, str) else None
    if feat is None:
        err.add("UNKNOWN_FEATURE", f"{path}.feature", f'Unknown feature "{fid}".', fid if isinstance(fid, str) else None)
        return None
    if op not in feat.allowed_operators:
        kind = {"NUMBER": "numeric", "ENUM": "choice", "BOOLEAN": "yes/no"}[feat.data_type]
        err.add("INVALID_OPERATOR", f"{path}.op", f'Operator "{op}" is not valid for a {kind} feature '
                f'(allowed: {", ".join(feat.allowed_operators)}).', fid)
        return None
    out = {"feature": fid, "op": op}
    v = c.get("value")
    if feat.data_type == "BOOLEAN":
        if "value" in c:
            err.add("INVALID_VALUE", f"{path}.value", f'"{op}" takes no value.', fid)
        return out
    if feat.data_type == "NUMBER":
        def bounded(x, p):
            if not _is_number(x):
                err.add("INVALID_VALUE", p, "Must be a number.", fid)
                return None
            if (feat.minimum is not None and x < feat.minimum) or (feat.maximum is not None and x > feat.maximum):
                err.add("OUT_OF_BOUNDS", p, f"Must be between {_num(feat.minimum)} and {_num(feat.maximum)}"
                        f"{' ' + feat.unit if feat.unit else ''}.", fid)
                return None
            return _num(x)
        if op == "between":
            if not (isinstance(v, list) and len(v) == 2):
                err.add("INVALID_VALUE", f"{path}.value", '"between" needs [low, high].', fid)
                return None
            lo, hi = bounded(v[0], f"{path}.value[0]"), bounded(v[1], f"{path}.value[1]")
            if lo is None or hi is None:
                return None
            if lo > hi:
                err.add("INVALID_VALUE", f"{path}.value", "Low must not be above high.", fid)
                return None
            out["value"] = [lo, hi]
        else:
            x = bounded(v, f"{path}.value")
            if x is None:
                return None
            out["value"] = x
        return out
    allowed = [val for val, _ in feat.values]                     # ENUM
    if op in ("in", "not_in"):
        if not (isinstance(v, list) and v):
            err.add("INVALID_VALUE", f"{path}.value", f'"{op}" needs a non-empty list of choices.', fid)
            return None
        bad = [x for x in v if x not in allowed]
        if bad:
            err.add("INVALID_ENUM", f"{path}.value", f"Not a valid choice: {', '.join(map(str, bad))} "
                    f"(allowed: {', '.join(allowed)}).", fid)
            return None
        out["value"] = sorted(set(v), key=allowed.index)         # canonical: registry order, no duplicates
        return out
    if v not in allowed:
        err.add("INVALID_ENUM", f"{path}.value", f'"{v}" is not a valid choice (allowed: {", ".join(allowed)}).', fid)
        return None
    out["value"] = v
    return out


def _group(g, path, err, depth, count) -> Optional[dict]:
    if not isinstance(g, dict):
        err.add("INVALID_GROUP", path, "Must be a group: {logic, conditions}.")
        return None
    extra = set(g) - {"logic", "conditions"} - ({"invalidation", "target", "max_holding_days"} if path == "exit" else set())
    if extra:
        err.add("UNKNOWN_FIELD", path, f"Unknown field(s): {', '.join(sorted(extra))}.")
    logic = g.get("logic")
    if logic not in LOGICS:
        err.add("INVALID_LOGIC", f"{path}.logic", 'Logic must be "ALL" or "ANY".')
    conds = g.get("conditions")
    if not isinstance(conds, list):
        err.add("INVALID_GROUP", f"{path}.conditions", "Conditions must be a list.")
        conds = []
    out = []
    for i, c in enumerate(conds):
        p = f"{path}.conditions[{i}]"
        if isinstance(c, dict) and "logic" in c:
            if depth >= MAX_DEPTH:
                err.add("NESTING_TOO_DEEP", p, f"Groups can be nested at most {MAX_DEPTH} levels deep.")
                continue
            sub = _group(c, p, err, depth + 1, count)
            if sub is not None:
                if not sub["conditions"]:
                    err.add("EMPTY_GROUP", p, "A nested group needs at least one condition.")
                out.append(sub)
        else:
            count[0] += 1
            n = _condition(c, p, err)
            if n is not None:
                out.append(n)
    return {"logic": logic, "conditions": out}


def _conditions_of(group: dict) -> List[dict]:
    out = []
    for c in (group or {}).get("conditions", []):
        out.extend(_conditions_of(c) if "logic" in c else [c])
    return out


# ---- top level ------------------------------------------------------------------------------------------------

def validate(spec: Any) -> Tuple[Optional[dict], List[dict]]:
    """Returns (normalised spec, errors). The spec is only usable when errors is empty."""
    err = _Errors()
    if not isinstance(spec, dict):
        err.add("INVALID_SPEC", "", "A strategy spec must be an object.")
        return None, err.items
    extra = set(spec) - TOP_KEYS
    if extra:
        err.add("UNKNOWN_FIELD", "", f"Unknown field(s): {', '.join(sorted(extra))}.")
    if spec.get("schema_version") != SCHEMA_VERSION:
        err.add("SCHEMA_VERSION", "schema_version", f"Unsupported schema version (expected {SCHEMA_VERSION}).")
    rv, rf = spec.get("feature_registry_version", F.REGISTRY_VERSION), spec.get("feature_registry_fingerprint", F.fingerprint())
    if rv != F.REGISTRY_VERSION or rf != F.fingerprint():
        err.add("REGISTRY_MISMATCH", "feature_registry_fingerprint", "This spec was defined against different feature "
                "definitions or thresholds than the current registry; it cannot be saved as a new version unchanged.")
    out: Dict[str, Any] = {"schema_version": SCHEMA_VERSION, "feature_registry_version": F.REGISTRY_VERSION,
                           "feature_registry_fingerprint": F.fingerprint()}
    out["name"] = _text(spec.get("name"), "name", err, required=True, max_len=80)
    out["description"] = _text(spec.get("description"), "description", err, required=False, max_len=500)
    if spec.get("direction") not in DIRECTIONS:
        err.add("INVALID_DIRECTION", "direction", f'Only {", ".join(DIRECTIONS)} is supported in this stage.')
    out["direction"] = spec.get("direction")
    if spec.get("timeframe") not in TIMEFRAMES:
        err.add("INVALID_TIMEFRAME", "timeframe", f'Only the daily timeframe ({", ".join(TIMEFRAMES)}) is supported.')
    out["timeframe"] = spec.get("timeframe")
    ex = spec.get("execution", EXECUTION)
    if ex != EXECUTION:
        err.add("INVALID_EXECUTION", "execution", "Decisions are made at the daily close and act at the next "
                "session's open in this schema version.")
    out["execution"] = dict(EXECUTION)

    u = spec.get("universe")
    syms: List[str] = []
    origin = "MANUAL"
    if not isinstance(u, dict) or u.get("type") not in UNIVERSE_TYPES:
        err.add("INVALID_UNIVERSE", "universe", "The universe must be an explicit list of symbols.")
    else:
        extra = set(u) - {"type", "symbols", "origin"}
        if extra:
            err.add("UNKNOWN_FIELD", "universe", f"Unknown field(s): {', '.join(sorted(extra))}.")
        origin = u.get("origin", "MANUAL")
        if origin not in UNIVERSE_ORIGINS:
            err.add("INVALID_UNIVERSE", "universe.origin", f"Origin must be one of {', '.join(UNIVERSE_ORIGINS)}.")
        raw = u.get("symbols")
        if not isinstance(raw, list) or not raw:
            err.add("EMPTY_UNIVERSE", "universe.symbols", "Add at least one symbol.")
            raw = []
        if len(raw) > MAX_SYMBOLS:
            err.add("TOO_MANY_SYMBOLS", "universe.symbols", f"At most {MAX_SYMBOLS} symbols.")
        seen = set()
        for i, s in enumerate(raw):
            n = s.strip().upper() if isinstance(s, str) else None
            if not n or not SYMBOL_RE.match(n):
                err.add("INVALID_SYMBOL", f"universe.symbols[{i}]", f'"{s}" is not a valid symbol.')
                continue
            if n in seen:
                err.add("DUPLICATE_SYMBOL", f"universe.symbols[{i}]", f"{n} is listed more than once.")
                continue
            seen.add(n)
            syms.append(n)
    out["universe"] = {"type": "EXPLICIT_SYMBOLS", "symbols": syms, "origin": origin}

    count = [0]
    entry = _group(spec.get("entry"), "entry", err, 1, count)
    if entry is not None and not _conditions_of(entry) and not any(e["path"].startswith("entry.conditions") for e in err.items):
        err.add("EMPTY_ENTRY", "entry.conditions", "Add at least one entry condition.")
    if count[0] > MAX_CONDITIONS:
        err.add("TOO_MANY_CONDITIONS", "entry.conditions", f"At most {MAX_CONDITIONS} entry conditions.")
    out["entry"] = entry

    x = spec.get("exit")
    count = [0]
    exit_g = _group(x, "exit", err, 1, count)
    if count[0] > MAX_CONDITIONS:
        err.add("TOO_MANY_CONDITIONS", "exit.conditions", f"At most {MAX_CONDITIONS} exit conditions.")
    if isinstance(x, dict) and exit_g is not None:
        for key, methods in (("invalidation", INVALIDATION_METHODS), ("target", TARGET_METHODS)):
            r = x.get(key)
            if r is None:
                exit_g[key] = None
                continue
            m = r.get("method") if isinstance(r, dict) else None
            if m not in methods or set(r) - {"method", "pct"}:
                err.add("INVALID_EXIT_METHOD", f"exit.{key}", f"Choose one of: {', '.join(methods)}.")
                exit_g[key] = None
                continue
            bounds = methods[m]
            if bounds is None:
                if "pct" in r:
                    err.add("INVALID_EXIT_METHOD", f"exit.{key}.pct", f"{m} takes no percentage.")
                exit_g[key] = {"method": m}
            else:
                p = r.get("pct")
                if not _is_number(p) or not bounds[0] <= p <= bounds[1]:
                    err.add("OUT_OF_BOUNDS", f"exit.{key}.pct", f"Must be a number from {_num(bounds[0])} to {_num(bounds[1])} %.")
                    exit_g[key] = None
                else:
                    exit_g[key] = {"method": m, "pct": _num(p)}
        h = x.get("max_holding_days")
        if h is None:
            exit_g["max_holding_days"] = None
        elif not (isinstance(h, int) and not isinstance(h, bool)) or not MAX_HOLDING_DAYS[0] <= h <= MAX_HOLDING_DAYS[1]:
            err.add("OUT_OF_BOUNDS", "exit.max_holding_days", f"A whole number of trading days from "
                    f"{MAX_HOLDING_DAYS[0]} to {MAX_HOLDING_DAYS[1]}.")
            exit_g["max_holding_days"] = None
        else:
            exit_g["max_holding_days"] = h
        if not (_conditions_of(exit_g) or exit_g["invalidation"] or exit_g["target"] or exit_g["max_holding_days"]) \
                and not any(e["path"].startswith("exit") for e in err.items):
            err.add("EMPTY_EXIT", "exit", "Add at least one exit rule (a condition, invalidation, target or maximum "
                    "holding period).")
    out["exit"] = exit_g

    r = spec.get("risk")
    risk = {}
    if not isinstance(r, dict):
        err.add("INVALID_RISK", "risk", "Risk limits are required.")
    else:
        extra = set(r) - set(RISK_LIMITS)
        if extra:
            err.add("UNKNOWN_FIELD", "risk", f"Unknown field(s): {', '.join(sorted(extra))}.")
        for k, (lo, hi) in RISK_LIMITS.items():
            v = r.get(k)
            whole = k == "max_open_positions"
            if not _is_number(v) or (whole and not isinstance(v, int)) or not lo <= v <= hi:
                err.add("OUT_OF_BOUNDS", f"risk.{k}", f"Must be {'a whole number' if whole else 'a number'} from "
                        f"{_num(lo)} to {_num(hi)}.")
            else:
                risk[k] = _num(v)
    out["risk"] = risk
    return (out if not err.items else None), err.items


# ---- canonical JSON + hashes ------------------------------------------------------------------------------------

def canonical_json(spec: dict) -> str:
    """Stable key order, compact separators, UTF-8, no timestamps (none exist in a spec)."""
    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def spec_hash(spec: dict) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def rules_hash(spec: dict) -> str:
    """Behaviour only: identical rules share it even if the name / description / origin differ."""
    core = {k: spec[k] for k in RULE_KEYS}
    core["universe"] = {"type": spec["universe"]["type"], "symbols": spec["universe"]["symbols"]}
    return hashlib.sha256(canonical_json(core).encode("utf-8")).hexdigest()


# ---- readiness ----------------------------------------------------------------------------------------------------

def features_used(spec: dict) -> List[Tuple[str, str]]:
    """(feature_id, where) for every condition and exit method, in spec order, without duplicates."""
    seen, out = set(), []
    for side in ("entry", "exit"):
        for c in _conditions_of(spec[side]):
            if c["feature"] not in seen:
                seen.add(c["feature"])
                out.append((c["feature"], side))
    for key in ("invalidation", "target"):
        r = spec["exit"].get(key)
        if r and EXIT_METHOD_FEATURES[r["method"]] not in seen:
            seen.add(EXIT_METHOD_FEATURES[r["method"]])
            out.append((EXIT_METHOD_FEATURES[r["method"]], f"exit.{key}"))
    return out


def readiness(spec: dict) -> dict:
    """Data availability only — NOT a quality score. Deterministic from the registry."""
    reasons = []
    for fid, where in features_used(spec):
        f = F.get(fid)
        state = "HISTORICAL" if f.historical_support and f.forward_support else \
            "FORWARD_ONLY" if f.forward_support else "UNSUPPORTED"
        reasons.append({"feature": fid, "name": f.beginner_name, "where": where, "state": state,
                        "why": "Can be rebuilt at a past date from daily bars." if state == "HISTORICAL" else f.point_in_time})
    if spec["exit"].get("max_holding_days"):
        reasons.append({"feature": None, "name": "Maximum holding period", "where": "exit.max_holding_days",
                        "state": "HISTORICAL", "why": "A count of trading days."})
    states = {r["state"] for r in reasons}
    status = UNSUPPORTED if "UNSUPPORTED" in states else FORWARD_ONLY if "FORWARD_ONLY" in states else READY
    explain = {READY: "Every rule can be rebuilt from information that existed at each past decision time.",
               FORWARD_ONLY: "Some rules use data that cannot be rebuilt for past dates, so the strategy can only be "
                             "tested going forward.",
               UNSUPPORTED: "Some rules use live account data that no strategy can use yet."}[status]
    return {"status": status, "label": READINESS_LABEL[status], "explanation": explain, "reasons": reasons}


# ---- plain-language summary ---------------------------------------------------------------------------------------

OP_TEXT = {">": "is above", ">=": "is at least", "<": "is below", "<=": "is at most", "==": "is", "!=": "is not",
           "in": "is one of", "not_in": "is none of", "between": "is between"}


def _fmt_num(v, unit) -> str:
    s = f"{v:,}" if isinstance(v, int) else f"{v:,.4g}"
    return f"${s}" if unit == "$" else f"{s}%" if unit == "%" else f"{s}x" if unit == "x" else s


def condition_text(c: dict) -> str:
    f = F.get(c["feature"])
    if f is None:
        return c.get("feature", "?")
    if f.data_type == "BOOLEAN":
        return f.true_text if c["op"] == "is_true" else f.false_text
    name = f.beginner_name[0].lower() + f.beginner_name[1:] if not f.beginner_name[:3].isupper() else f.beginner_name
    if f.data_type == "NUMBER":
        v = c["value"]
        val = f"{_fmt_num(v[0], f.unit)} and {_fmt_num(v[1], f.unit)}" if c["op"] == "between" else _fmt_num(v, f.unit)
        return f"{name} {OP_TEXT[c['op']]} {val}"
    vals = c["value"] if isinstance(c["value"], list) else [c["value"]]
    labels = [f.value_label(v) for v in vals]
    joined = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " or " + labels[-1]
    return f"{name} {OP_TEXT[c['op']]} {joined}"


def _group_text(g: dict) -> str:
    parts = [f"({_group_text(c)})" if "logic" in c else condition_text(c) for c in g["conditions"]]
    word = " and " if g["logic"] == "ALL" else " or "
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + word + parts[-1] if parts else ""


def exit_rule_texts(x: dict) -> List[str]:
    out = [f"({_group_text(c)})" if "logic" in c else condition_text(c) for c in x.get("conditions", [])]
    inv, tgt, h = x.get("invalidation"), x.get("target"), x.get("max_holding_days")
    if inv:
        out.append("the close falls below the support level that existed at entry" if inv["method"] == "CLOSE_BELOW_ENTRY_SUPPORT"
                   else f"the close is {_fmt_num(inv['pct'], '%')} or more below the entry price")
    if tgt:
        out.append("the close reaches the resistance level that existed at entry" if tgt["method"] == "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE"
                   else f"the close is {_fmt_num(tgt['pct'], '%')} or more above the entry price")
    if h:
        out.append(f"after {h} trading day{'s' if h != 1 else ''}")
    return out


def summary(spec: dict) -> str:
    e, x, r = spec["entry"], spec["exit"], spec["risk"]
    n = len(e["conditions"])
    how = "all" if e["logic"] == "ALL" else "any"
    lead = f"Enter when {how} of these {n} conditions are true: " if n > 1 else "Enter when "
    # exit fires when the condition group is met OR any of invalidation / target / maximum holding period
    methods = exit_rule_texts({**x, "conditions": []})
    group = _group_text(x) if x.get("conditions") else ""
    if group and x["logic"] == "ALL" and len(x["conditions"]) > 1:
        group = f"all of these are true: {group}"
    exit_parts = ([group] if group else []) + methods
    syms = spec["universe"]["symbols"]
    return " ".join([
        f"Long-only, daily. Universe: {', '.join(syms[:12])}{f' and {len(syms) - 12} more' if len(syms) > 12 else ''}.",
        lead + _group_text(e) + ".",
        "Exit when " + "; or ".join(exit_parts) + ".",
        f"Risk limits: at most {_fmt_num(r['max_position_pct'], '%')} of strategy equity per position and "
        f"{r['max_open_positions']} open position{'s' if r['max_open_positions'] != 1 else ''}.",
        "Decisions use the daily close; any action would be at the next session's open.",
    ])


# ---- version comparison -------------------------------------------------------------------------------------------

def compare(a: dict, b: dict) -> List[dict]:
    """Deterministic, rule-level differences between two normalised specs (no performance comparison)."""
    ch = []

    def add(section, old, new):
        ch.append({"section": section, "old": old, "new": new})
    for k in ("name", "description"):
        if a[k] != b[k]:
            add(k.upper(), a[k], b[k])
    sa, sb = a["universe"]["symbols"], b["universe"]["symbols"]
    if sa != sb:
        add("UNIVERSE", f"removed: {', '.join(s for s in sa if s not in sb) or 'none'}",
            f"added: {', '.join(s for s in sb if s not in sa) or 'none'}")
    for side in ("entry", "exit"):
        ga, gb = a[side], b[side]
        if ga["logic"] != gb["logic"] or ga["conditions"] != gb["conditions"]:
            add(f"{side.upper()} CONDITIONS", f"{ga['logic']}: {_group_text(ga) or 'none'}", f"{gb['logic']}: {_group_text(gb) or 'none'}")
    for k in ("invalidation", "target", "max_holding_days"):
        if a["exit"].get(k) != b["exit"].get(k):
            add(f"EXIT {k.replace('_', ' ').upper()}", a["exit"].get(k), b["exit"].get(k))
    if a["risk"] != b["risk"]:
        add("RISK", a["risk"], b["risk"])
    ra, rb = readiness(a)["label"], readiness(b)["label"]
    if ra != rb:
        add("READINESS", ra, rb)
    if a["feature_registry_fingerprint"] != b["feature_registry_fingerprint"]:
        add("FEATURE REGISTRY", a["feature_registry_fingerprint"][:12], b["feature_registry_fingerprint"][:12])
    return ch


def report(spec: Any) -> dict:
    """Everything the builder shows after "Validate" — no side effects."""
    norm, errors = validate(copy.deepcopy(spec))
    if errors:
        return {"valid": False, "errors": errors}
    return {"valid": True, "errors": [], "spec": norm, "canonical_json": canonical_json(norm),
            "spec_hash": spec_hash(norm), "rules_hash": rules_hash(norm), "readiness": readiness(norm),
            "summary": summary(norm), "entry_count": len(_conditions_of(norm["entry"])),
            "exit_count": len(exit_rule_texts(norm["exit"]))}
