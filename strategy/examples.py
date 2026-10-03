"""
strategy/examples.py — ONE educational example for the builder. It is never saved automatically, never called
profitable or recommended, and carries no historical performance. The universe is deliberately empty: the user must
choose symbols before it can validate, and must press Save to keep it.
"""
from strategy import features as F
from strategy.spec import EXECUTION, SCHEMA_VERSION

EXAMPLE_NOTE = ("Example rules for learning the builder — not a recommendation, not tested, and no performance is "
                "claimed. Nothing is saved unless you press Save.")


def pullback_example() -> dict:
    return {
        "schema_version": SCHEMA_VERSION, "feature_registry_version": F.REGISTRY_VERSION,
        "feature_registry_fingerprint": F.fingerprint(),
        "name": "Pullback example (example rules)",
        "description": "Uptrend stocks that have pulled back toward support while the market is not cautious.",
        "direction": "LONG_ONLY", "timeframe": "1D", "execution": dict(EXECUTION),
        "universe": {"type": "EXPLICIT_SYMBOLS", "symbols": [], "origin": "MANUAL"},
        "entry": {"logic": "ALL", "conditions": [
            {"feature": "stock.trend", "op": "==", "value": "UPTREND"},
            {"feature": "stock.price_location", "op": "==", "value": "NEAR_SUPPORT"},
            {"feature": "stock.momentum", "op": "!=", "value": "WEAK"},
            {"feature": "market.environment", "op": "!=", "value": "CAUTIOUS"},
        ]},
        "exit": {"logic": "ANY", "conditions": [
            {"feature": "stock.price_location", "op": "==", "value": "NO_SUPPORT_BELOW"},
        ], "invalidation": {"method": "CLOSE_BELOW_ENTRY_SUPPORT"},
            "target": {"method": "CLOSE_AT_OR_ABOVE_ENTRY_RESISTANCE"}, "max_holding_days": 10},
        "risk": {"max_position_pct": 10, "max_open_positions": 5},
    }
