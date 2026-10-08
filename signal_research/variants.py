"""
signal_research/variants.py — explicit research variants of the Stage 4.7 configuration (DESIGN_52 §2, §3, §4, §5).

A variant = {label, family, config (a Stage 4.7 configuration with exact weights), research (sr_v1 rules), config_hash}.
Weights are rescaled deterministically (6 dp, ROUND_DOWN, residual to the largest kept weight) and always sum to exactly
1.000000. The set is fixed and ordered; there is no search. Hash = sha256(canonical {config, research}).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
from typing import Dict, List, Optional, Sequence

from rotation import rules as R
from rotation.store import canonical_json, sha256_hex
from rotation_walkforward import grid as G
from signal_research import MAX_VARIANTS, RULES_VERSION
from signal_research import sector as SC

SIX = Decimal("0.000001")
GLOBAL_RANK, SECTOR_NEUTRAL_RANK = "GLOBAL_RANK", "SECTOR_NEUTRAL_RANK"
NO_OVERLAY, SIMPLE_RISK_OFF, BINARY_TREND_FILTER = "NO_OVERLAY", "SIMPLE_RISK_OFF", "BINARY_TREND_FILTER"
RANKINGS, OVERLAYS = (GLOBAL_RANK, SECTOR_NEUTRAL_RANK), (NO_OVERLAY, SIMPLE_RISK_OFF, BINARY_TREND_FILTER)
FAMILIES = ("baseline", "ablation", "control", "sector", "regime", "combined")
ABLATABLE = ("momentum", "trend", "relative_strength", "volatility", "liquidity")
REGIME_KEYS = ("TREND_UP|LOW_VOL", "TREND_UP|HIGH_VOL", "TREND_DOWN|LOW_VOL", "TREND_DOWN|HIGH_VOL")
SCHEDULES = {NO_OVERLAY: {"TREND_UP|LOW_VOL": "1.000000", "TREND_UP|HIGH_VOL": "1.000000", "TREND_DOWN|LOW_VOL": "1.000000", "TREND_DOWN|HIGH_VOL": "1.000000"},
             SIMPLE_RISK_OFF: {"TREND_UP|LOW_VOL": "1.000000", "TREND_UP|HIGH_VOL": "0.750000", "TREND_DOWN|LOW_VOL": "0.500000", "TREND_DOWN|HIGH_VOL": "0.250000"},
             BINARY_TREND_FILTER: {"TREND_UP|LOW_VOL": "1.000000", "TREND_UP|HIGH_VOL": "1.000000", "TREND_DOWN|LOW_VOL": "0.500000", "TREND_DOWN|HIGH_VOL": "0.500000"}}
SECTOR_CAPS = ("0.250000", "0.300000", "0.350000")
SUBSETS = (("momentum",), ("trend",), ("relative_strength",), ("momentum", "trend"), ("momentum", "relative_strength"), ("trend", "relative_strength"))


class VariantError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---- weights -----------------------------------------------------------------------------------------------------------------------------

def _fmt(d: Decimal) -> str:
    return format(d.quantize(SIX, rounding=ROUND_HALF_EVEN), "f")


def remove_factor(weights: Dict[str, str], key: str) -> Dict[str, str]:
    """Set `key` to 0 and rescale the other weights proportionally (Stage 4.9 rescale: ROUND_DOWN, residual to the largest)."""
    if key not in ABLATABLE:
        raise VariantError("INVALID_FACTOR", f"{key} is not an ablatable factor")
    if Decimal(str(weights[key])) == 0:
        raise VariantError("FACTOR_ALREADY_ZERO", f"{key} already has weight 0")
    return G.rescale_weights({k: str(v) for k, v in weights.items()}, key, "0")


def keep_weights(weights: Dict[str, str], keep: Sequence[str]) -> Dict[str, str]:
    """Keep only `keep` (proportionally rescaled to 1.000000, ROUND_DOWN, residual to the largest kept weight); the rest 0."""
    keep = tuple(keep)
    if not keep or any(k not in ABLATABLE for k in keep) or len(set(keep)) != len(keep):
        raise VariantError("INVALID_SUBSET", f"invalid factor subset {list(keep)}")
    kept = {k: Decimal(str(weights[k])) for k in keep}
    total = sum(kept.values(), Decimal(0))
    if total <= 0:
        raise VariantError("INVALID_SUBSET", "the kept factors all have weight 0")
    out = {k: (w / total).quantize(SIX, rounding=ROUND_DOWN) for k, w in kept.items()}
    residual = Decimal(1) - sum(out.values(), Decimal(0))
    largest = max(out, key=lambda k: (out[k], k))
    out[largest] = (out[largest] + residual).quantize(SIX, rounding=ROUND_HALF_EVEN)
    full = {k: (out.get(k, Decimal(0))) for k in R.WEIGHT_KEYS}
    R.validate_weights({k: _fmt(v) for k, v in full.items()})
    return {k: _fmt(full[k]) for k in R.WEIGHT_KEYS}


def normalised_weights(weights: Dict[str, str]) -> Dict[str, str]:
    return {k: _fmt(v) for k, v in R.validate_weights(weights).items()}


# ---- rules and hashing -------------------------------------------------------------------------------------------------------------------

def rules(ranking: str = GLOBAL_RANK, max_sector_weight: Optional[str] = None, overlay: str = NO_OVERLAY, sector_map_hash: str = "", schedule: Optional[Dict[str, str]] = None) -> dict:
    if ranking not in RANKINGS:
        raise VariantError("INVALID_RANKING", f"unknown ranking rule {ranking!r}")
    if overlay not in OVERLAYS:
        raise VariantError("INVALID_OVERLAY", f"unknown regime overlay {overlay!r}")
    sched = dict(schedule or SCHEDULES[overlay])
    if set(sched) != set(REGIME_KEYS):
        raise VariantError("INVALID_SCHEDULE", f"the exposure schedule must have exactly the keys {list(REGIME_KEYS)}")
    out_sched = {}
    for k in REGIME_KEYS:
        v = Decimal(str(sched[k]))
        if not Decimal(0) <= v <= Decimal(1):
            raise VariantError("INVALID_SCHEDULE", f"exposure {k} must be between 0 and 1 (no leverage, no shorting)")
        out_sched[k] = _fmt(v)
    cap = None
    if max_sector_weight is not None:
        c = Decimal(str(max_sector_weight))
        if not Decimal(0) < c <= Decimal(1):
            raise VariantError("INVALID_SECTOR_CAP", "max_sector_weight must be in (0, 1]")
        cap = _fmt(c)
    if len(sector_map_hash) != 64:
        raise VariantError("INVALID_SECTOR_MAP", "a 64-hex sector map hash is required")
    return {"rules_version": RULES_VERSION, "ranking": ranking, "max_sector_weight": cap, "regime_overlay": overlay, "exposure_schedule": out_sched, "sector_map_hash": sector_map_hash}


def variant_hash(config: dict, research: dict) -> str:
    return sha256_hex(canonical_json({"config": config, "research": research}))


def make_variant(label: str, family: str, config: dict, research: dict, sector_map: Dict[str, str]) -> dict:
    if family not in FAMILIES:
        raise VariantError("INVALID_FAMILY", f"unknown family {family!r}")
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in config.items()}
    cfg["weights"] = normalised_weights(cfg["weights"])
    pc = R.validate_portfolio_config(cfg["portfolio_size"], cfg["exit_rank"], cfg["cash_buffer_pct"], cfg["min_position_weight"], cfg["max_position_weight"],
                                     cfg["rebalance_threshold"], cfg["max_turnover_per_rotation"])
    if research["max_sector_weight"] is not None:
        SC.validate_cap(Decimal(research["max_sector_weight"]), pc["equal_weight"], int(cfg["portfolio_size"]), sector_map)
    if research["sector_map_hash"] != SC.sector_map_hash(sector_map):
        raise VariantError("SECTOR_MAP_MISMATCH", "the rules reference a different sector map")
    return {"label": label, "family": family, "config": cfg, "research": research, "config_hash": variant_hash(cfg, research)}


def build_variants(base_config: dict, sector_map: Dict[str, str], families: Optional[Sequence[str]] = None, caps: Sequence[str] = SECTOR_CAPS) -> List[dict]:
    """The fixed, ordered research set (DESIGN_52 §5): at most MAX_VARIANTS; `families` restricts to a subset (baseline always included)."""
    fams = set(FAMILIES if families is None else families)
    smh = SC.sector_map_hash(sector_map)
    base_w = normalised_weights(base_config["weights"])
    base = {**base_config, "weights": base_w}
    out: List[dict] = [make_variant("baseline", "baseline", base, rules(sector_map_hash=smh), sector_map)]
    if "ablation" in fams:
        for k in ABLATABLE:
            if Decimal(base_w[k]) > 0:
                out.append(make_variant(f"no_{k}", "ablation", {**base, "weights": remove_factor(base_w, k)}, rules(sector_map_hash=smh), sector_map))
    if "control" in fams:
        for keep in SUBSETS:
            out.append(make_variant("+".join(keep) + ("_only" if len(keep) == 1 else ""), "control", {**base, "weights": keep_weights(base_w, keep)}, rules(sector_map_hash=smh), sector_map))
    if "sector" in fams:
        out.append(make_variant("sector_neutral", "sector", base, rules(SECTOR_NEUTRAL_RANK, None, NO_OVERLAY, smh), sector_map))
        for cap in caps:
            out.append(make_variant(f"sector_cap_{Decimal(cap).quantize(Decimal('0.01'))}", "sector", base, rules(GLOBAL_RANK, cap, NO_OVERLAY, smh), sector_map))
    if "regime" in fams:
        out.append(make_variant("simple_risk_off", "regime", base, rules(GLOBAL_RANK, None, SIMPLE_RISK_OFF, smh), sector_map))
        out.append(make_variant("binary_trend_filter", "regime", base, rules(GLOBAL_RANK, None, BINARY_TREND_FILTER, smh), sector_map))
    if "combined" in fams:
        out.append(make_variant("sector_neutral+simple_risk_off", "combined", base, rules(SECTOR_NEUTRAL_RANK, None, SIMPLE_RISK_OFF, smh), sector_map))
        out.append(make_variant("sector_cap_0.30+simple_risk_off", "combined", base, rules(GLOBAL_RANK, "0.300000", SIMPLE_RISK_OFF, smh), sector_map))
    seen = set()
    for v in out:
        if v["config_hash"] in seen:
            raise VariantError("DUPLICATE_VARIANT", f"duplicate variant {v['label']}")
        seen.add(v["config_hash"])
    if len(out) > MAX_VARIANTS:
        raise VariantError("TOO_MANY_VARIANTS", f"{len(out)} variants exceed the hard cap of {MAX_VARIANTS}")
    return out


def counterpart_label(v: dict) -> Optional[str]:
    """The overlay-free variant that an overlay variant is compared with (DESIGN_52 §8), by label."""
    r = v["research"]
    if r["regime_overlay"] == NO_OVERLAY:
        return None
    if r["ranking"] == SECTOR_NEUTRAL_RANK:
        return "sector_neutral"
    if r["max_sector_weight"] is not None:
        return f"sector_cap_{Decimal(r['max_sector_weight']).quantize(Decimal('0.01'))}"
    return "baseline"
