"""
rotation/rules.py — Stage 4.7 PURE deterministic rules (DESIGN_47_PORTFOLIO_ROTATION §7–§11): percentile
normalisation, composite score, ranking, static equal-weight allocation, the rank buffer, rebalance action
classification and cash-aware turnover. Decimal only; every input is passed in; no clock, no randomness, no I/O.

Vocabulary produced here (ADD / INCREASE / DECREASE / EXIT / HOLD / NONE) describes a portfolio PROPOSAL — it is never an
order and nothing here can reach a broker.
"""
from __future__ import annotations

from decimal import Context, Decimal, InvalidOperation, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from rotation.factors import PLACES

CTX = Context(prec=34, rounding=ROUND_HALF_EVEN)
ONE, ZERO, HUNDRED, FIFTY = Decimal(1), Decimal(0), Decimal(100), Decimal("50.000000")
WEIGHT_KEYS = ("momentum", "trend", "relative_strength", "volatility", "drawdown", "liquidity")
DEFAULT_WEIGHTS = {"momentum": "0.30", "trend": "0.25", "relative_strength": "0.25", "volatility": "0.10",
                   "drawdown": "0", "liquidity": "0.10"}
ADD, INCREASE, DECREASE, EXIT, HOLD, NONE = "ADD", "INCREASE", "DECREASE", "EXIT", "HOLD", "NONE"
ACTIONS = (ADD, INCREASE, DECREASE, EXIT, HOLD, NONE)
TOP_N, RETAINED_RANK_BUFFER = "TOP_N", "RETAINED_RANK_BUFFER"
EXIT_RANK_ABOVE, EXIT_INELIGIBLE, EXIT_OVERFLOW = "RANK_ABOVE_EXIT_RANK", "INELIGIBLE", "RANK_BUFFER_OVERFLOW"
BELOW_ONE_SHARE = "BELOW_ONE_SHARE"


class ConfigError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def q(d: Decimal) -> Decimal:
    return d.quantize(PLACES, rounding=ROUND_HALF_EVEN)


def dec(v, what: str = "value") -> Decimal:
    """Exact Decimal of a string / int / Decimal; floats are refused (no float ingestion into the deterministic core)."""
    if isinstance(v, bool) or isinstance(v, float):
        raise ConfigError("INVALID_CONFIG", f"{what} must be given as a decimal string or integer, not {type(v).__name__}")
    try:
        d = Decimal(str(v)) if not isinstance(v, Decimal) else v
    except (InvalidOperation, ValueError):
        raise ConfigError("INVALID_CONFIG", f"{what} is not a number") from None
    if not d.is_finite():
        raise ConfigError("INVALID_CONFIG", f"{what} is not finite")
    return d


# ---- percentile normalisation (§7) ---------------------------------------------------------------------------------------------------

def percentiles(values: Mapping[str, Decimal]) -> Dict[str, Decimal]:
    """(average_rank - 1) / (n - 1) * 100 with rank 1 = worst (lowest) and rank n = best (highest); average ranks for
    ties; n = 1 -> 50. Higher raw value = better; the caller inverts afterwards where lower is better."""
    symbols = sorted(values)
    n = len(symbols)
    if n == 0:
        return {}
    if n == 1:
        return {symbols[0]: FIFTY}
    order = sorted(symbols, key=lambda s: (values[s], s))          # ascending raw value: worst first
    ranks: Dict[str, Decimal] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (Decimal(i + 1) + Decimal(j + 1)) / 2                 # average of the tied positions (1-based)
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    with localcontext(CTX):
        return {s: q((ranks[s] - 1) / (n - 1) * HUNDRED) for s in symbols}


def invert(score: Decimal) -> Decimal:
    return q(HUNDRED - score)


def factor_scores(raw: Mapping[str, Mapping[str, Decimal]]) -> Dict[str, Dict[str, Decimal]]:
    """Per-symbol normalised scores (0-100) for the six weighted factors from the eight raw factors of every ELIGIBLE
    symbol (all raw values must be present). Momentum / Trend = mean of their two sub-percentiles; volatility inverted."""
    def pct(key: str) -> Dict[str, Decimal]:
        return percentiles({s: raw[s][key] for s in raw})
    p = {k: pct(k) for k in ("ret20", "ret60", "trend50", "trend200", "relative_strength", "volatility", "drawdown", "liquidity")}
    out: Dict[str, Dict[str, Decimal]] = {}
    with localcontext(CTX):
        for s in sorted(raw):
            out[s] = {"momentum": q((p["ret20"][s] + p["ret60"][s]) / 2), "trend": q((p["trend50"][s] + p["trend200"][s]) / 2),
                      "relative_strength": p["relative_strength"][s], "volatility": invert(p["volatility"][s]),
                      "drawdown": p["drawdown"][s], "liquidity": p["liquidity"][s]}
    return out


# ---- composite and ranking (§7) --------------------------------------------------------------------------------------------------------

def validate_weights(weights: Mapping[str, object]) -> Dict[str, Decimal]:
    """Exactly the six keys, each >= 0 (6 dp), summing to exactly 1.000000."""
    if set(weights) != set(WEIGHT_KEYS):
        raise ConfigError("INVALID_WEIGHTS", f"weights must have exactly the keys {list(WEIGHT_KEYS)}")
    out = {}
    for k in WEIGHT_KEYS:
        d = dec(weights[k], f"weight {k}")
        if d < 0:
            raise ConfigError("INVALID_WEIGHTS", f"weight {k} must be >= 0")
        if d != q(d):
            raise ConfigError("INVALID_WEIGHTS", f"weight {k} has more than 6 decimal places")
        out[k] = q(d)
    if sum(out.values(), ZERO) != Decimal("1.000000"):
        raise ConfigError("INVALID_WEIGHTS", "weights must sum to exactly 1.000000")
    return out


def composite(scores: Mapping[str, Decimal], weights: Mapping[str, Decimal]) -> Decimal:
    with localcontext(CTX):
        return q(sum((weights[k] * scores[k] for k in WEIGHT_KEYS), ZERO))


def rank_symbols(entries: Mapping[str, Mapping[str, Decimal]]) -> List[str]:
    """Best first: composite desc, relative-strength score desc, liquidity score desc, ticker asc."""
    return sorted(entries, key=lambda s: (-entries[s]["composite"], -entries[s]["relative_strength"], -entries[s]["liquidity"], s))


def ranks(ordered: Sequence[str]) -> Dict[str, int]:
    return {s: i + 1 for i, s in enumerate(ordered)}


# ---- portfolio configuration and static equal weight (§8-§9) ---------------------------------------------------------------------------

def equal_weight(portfolio_size: int, cash_buffer_pct: Decimal) -> Decimal:
    with localcontext(CTX):
        return ((ONE - cash_buffer_pct) / portfolio_size).quantize(PLACES, rounding=ROUND_DOWN)


def validate_portfolio_config(portfolio_size, exit_rank, cash_buffer_pct, min_position_weight, max_position_weight,
                              rebalance_threshold, max_turnover_per_rotation) -> Dict[str, object]:
    """The static allocation rule: equal_weight = (1 - cash_buffer_pct) / portfolio_size must satisfy
    min_position_weight <= equal_weight <= max_position_weight; no dynamic clamping exists."""
    if isinstance(portfolio_size, bool) or not isinstance(portfolio_size, int) or portfolio_size < 1:
        raise ConfigError("INVALID_PORTFOLIO_SIZE", "portfolio_size must be an integer >= 1")
    if isinstance(exit_rank, bool) or not isinstance(exit_rank, int) or exit_rank < portfolio_size:
        raise ConfigError("INVALID_EXIT_RANK", "exit_rank must be an integer >= portfolio_size")
    buffer = dec(cash_buffer_pct, "cash_buffer_pct")
    if not ZERO <= buffer < ONE:
        raise ConfigError("INVALID_CASH_BUFFER", "cash_buffer_pct must be >= 0 and < 1")
    lo, hi = dec(min_position_weight, "min_position_weight"), dec(max_position_weight, "max_position_weight")
    if not ZERO <= lo <= hi <= ONE:
        raise ConfigError("INVALID_WEIGHT_BOUNDS", "0 <= min_position_weight <= max_position_weight <= 1 is required")
    ew = equal_weight(portfolio_size, buffer)
    if not lo <= ew <= hi:
        raise ConfigError("INVALID_WEIGHT_BOUNDS", f"equal weight {ew} is outside [{lo}, {hi}]")
    thr = dec(rebalance_threshold, "rebalance_threshold")
    if not ZERO <= thr <= ONE:
        raise ConfigError("INVALID_THRESHOLD", "rebalance_threshold must be between 0 and 1")
    mt = dec(max_turnover_per_rotation, "max_turnover_per_rotation")
    if not ZERO <= mt <= ONE:
        raise ConfigError("INVALID_TURNOVER_LIMIT", "max_turnover_per_rotation must be between 0 and 1")
    return {"portfolio_size": portfolio_size, "exit_rank": exit_rank, "cash_buffer_pct": buffer, "min_position_weight": lo,
            "max_position_weight": hi, "equal_weight": ew, "rebalance_threshold": thr, "max_turnover_per_rotation": mt}


def allocate(selected_best_first: Sequence[str], portfolio_size: int, cash_buffer_pct: Decimal) -> Tuple[Dict[str, Decimal], Decimal]:
    """Static EQUAL_WEIGHT: every selected name gets equal_weight; with all slots filled the quantisation residual goes
    to rank 1 so the security weights sum to exactly 1 - cash_buffer_pct. Returns (weights, target_cash_weight)."""
    ew = equal_weight(portfolio_size, cash_buffer_pct)
    weights = {s: ew for s in selected_best_first}
    with localcontext(CTX):
        invested = ONE - cash_buffer_pct
        if len(selected_best_first) == portfolio_size and selected_best_first:
            weights[selected_best_first[0]] = q(ew + (invested - ew * portfolio_size))
        return weights, q(ONE - sum(weights.values(), ZERO))


# ---- rank buffer (§8) ------------------------------------------------------------------------------------------------------------------

def select(ranked_best_first: Sequence[str], holdings: Iterable[str], portfolio_size: int, exit_rank: int) -> Dict[str, object]:
    """Holdings with rank <= exit_rank are retained (in rank order, up to portfolio_size slots); rank > exit_rank or not
    ranked (ineligible) exit; free slots are filled by the highest-ranked non-holdings. Deterministic."""
    rank_of = ranks(ranked_best_first)
    held: Set[str] = set(holdings)
    exits: Dict[str, str] = {}
    keep: List[str] = []
    for h in sorted(held, key=lambda s: (rank_of.get(s, 10 ** 9), s)):
        r = rank_of.get(h)
        if r is None:
            exits[h] = EXIT_INELIGIBLE
        elif r > exit_rank:
            exits[h] = EXIT_RANK_ABOVE
        elif len(keep) >= portfolio_size:
            exits[h] = EXIT_OVERFLOW
        else:
            keep.append(h)
    slots = portfolio_size - len(keep)
    adds = [s for s in ranked_best_first if s not in held][:max(slots, 0)]
    selected = sorted(keep + adds, key=lambda s: rank_of[s])
    reasons = {s: (RETAINED_RANK_BUFFER if s in held and rank_of[s] > portfolio_size else TOP_N) for s in selected}
    return {"selected": selected, "retained": sorted(keep, key=lambda s: rank_of[s]), "added": adds, "exits": exits,
            "reasons": reasons, "rank_of": rank_of}


# ---- reference valuation, actions, turnover (§10-§11) ----------------------------------------------------------------------------------

def current_weights(cash: Decimal, positions: Mapping[str, Decimal], reference_prices: Mapping[str, Decimal]) -> Dict[str, object]:
    """reference_equity = cash + sum(qty x reference price at T); weights per symbol and the cash weight. Raises ValueError
    for a held symbol without a positive reference price or a non-positive equity (the engine maps these to INPUT_ERROR)."""
    if cash is None or cash < 0:
        raise ValueError("cash must be a non-negative Decimal")
    with localcontext(CTX):
        equity = cash
        for sym in sorted(positions):
            qty = positions[sym]
            if qty <= 0:
                continue
            price = reference_prices.get(sym)
            if price is None or price <= 0:
                raise ValueError(f"no reference price for held symbol {sym}")
            equity += qty * price
        if equity <= 0:
            raise ValueError("reference equity must be positive")
        weights = {sym: q(positions[sym] * reference_prices[sym] / equity) for sym in sorted(positions) if positions[sym] > 0}
        return {"reference_equity": equity.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN), "weights": weights,
                "cash_weight": q(cash / equity)}


def est_qty_diff(weight_diff: Decimal, reference_equity: Decimal, reference_price: Decimal) -> int:
    """Whole shares of |weight_diff| x equity at the reference price (floored); 0 when the price is unusable."""
    if reference_price is None or reference_price <= 0:
        return 0
    with localcontext(CTX):
        return int((abs(weight_diff) * reference_equity / reference_price).to_integral_value(rounding=ROUND_FLOOR))


def classify(held: bool, current_weight: Decimal, target_weight: Decimal, rebalance_threshold: Decimal,
             est_qty: int) -> Tuple[str, Optional[str]]:
    """ADD / INCREASE / DECREASE / EXIT / HOLD / NONE from deterministic weights; est_qty = 0 -> HOLD / BELOW_ONE_SHARE."""
    diff = target_weight - current_weight
    if target_weight == 0:
        return (EXIT, "TARGET_ZERO") if held else (NONE, None)
    if not held:
        return (ADD, None) if est_qty > 0 else (HOLD, BELOW_ONE_SHARE)
    if abs(diff) < rebalance_threshold:
        return HOLD, "WITHIN_THRESHOLD"
    if est_qty == 0:
        return HOLD, BELOW_ONE_SHARE
    return (INCREASE, None) if diff > 0 else (DECREASE, None)


def side_hint(action: str) -> Optional[str]:
    return "BUY" if action in (ADD, INCREASE) else "SELL" if action in (DECREASE, EXIT) else None


def turnover(current: Mapping[str, Decimal], target: Mapping[str, Decimal], current_cash_weight: Decimal,
             target_cash_weight: Decimal) -> Decimal:
    """(sum over securities |target - current| + |target cash - current cash|) / 2."""
    with localcontext(CTX):
        total = sum((abs(target.get(s, ZERO) - current.get(s, ZERO)) for s in set(current) | set(target)), ZERO)
        return q((total + abs(target_cash_weight - current_cash_weight)) / 2)
