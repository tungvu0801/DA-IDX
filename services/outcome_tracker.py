"""
services/outcome_tracker.py — Measures what actually happened after a
saved research snapshot, using only real historical daily bars. NEVER
calls Claude — every number here is deterministic Python/pandas arithmetic.
Python determines outcomes; Claude is not required and not used.

Lookahead-bias discipline: a snapshot's own stored fields (RESEARCH DATA)
are never modified by this module. Only research_outcomes rows (OUTCOME
DATA) are written, using bars strictly AFTER the snapshot's
market_timestamp date (see _bars_after) — the snapshot's own frozen
`price` is always the reference point, never re-derived from later bars.

"N trading days after" = the Nth bar in the ascending list of bars whose
date is strictly greater than market_timestamp (index N-1, zero-indexed) —
so index 0 is "1 trading day after," never "0 trading days after." This
counts real elapsed trading sessions, so weekends/holidays/early closes
are handled automatically: they simply never produced a bar to count.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

import config
from data.market_data import fetch_daily_bars
from database.database import get_db
from database.models import ResearchOutcome, ResearchSnapshot
from scanner.market_scanner import get_data_client

logger = logging.getLogger(__name__)


@dataclass
class UpdateSummary:
    snapshots_checked: int = 0
    outcomes_updated: int = 0
    still_pending: int = 0
    errors: List[str] = field(default_factory=list)


def _bars_after(bars_df: pd.DataFrame, market_timestamp: str) -> pd.DataFrame:
    """Bars strictly after the snapshot's day-0 date, ascending, 0-indexed."""
    cutoff_date = pd.Timestamp(market_timestamp).date()
    mask = bars_df["timestamp"].dt.date > cutoff_date
    return bars_df[mask].sort_values("timestamp").reset_index(drop=True)


def _pct(current: float, reference: float) -> Optional[float]:
    if not reference:
        return None
    return (current - reference) / reference * 100.0


def _first_hit_index(bars: pd.DataFrame, condition: Callable[[pd.Series], bool]) -> Optional[int]:
    """Positional (0-based) index of the first row in `bars` where `condition(row)` is True, or None."""
    for i in range(len(bars)):
        if condition(bars.iloc[i]):
            return i
    return None


def _evaluate_horizon(snapshot: ResearchSnapshot, bars_after: pd.DataFrame, horizon: int) -> Optional[ResearchOutcome]:
    """Returns None (leave PENDING) if not enough trading sessions have elapsed yet for this horizon."""
    if len(bars_after) < horizon:
        return None

    window = bars_after.iloc[:horizon].reset_index(drop=True)
    horizon_bar = window.iloc[-1]

    price = snapshot.price
    price_at_horizon = float(horizon_bar["close"])
    highest = float(window["high"].max())
    lowest = float(window["low"].min())

    # Long-style convention: MFE describes the best excursion IN THE FAVORABLE
    # (up) direction only, MAE the worst excursion IN THE ADVERSE (down)
    # direction only. If price never rose above the snapshot price, the best
    # it did is 0.00 (not negative); symmetrically MAE never reads positive.
    # return_pct is unaffected -- it's the actual signed outcome, not an excursion.
    raw_mfe = _pct(highest, price)
    raw_mae = _pct(lowest, price)

    outcome = ResearchOutcome(
        snapshot_id=snapshot.id,
        horizon_trading_days=horizon,
        status="COMPLETED",
        price_at_horizon=price_at_horizon,
        return_pct=_pct(price_at_horizon, price),
        highest_price=highest,
        lowest_price=lowest,
        max_favorable_excursion_pct=None if raw_mfe is None else max(0.0, raw_mfe),
        max_adverse_excursion_pct=None if raw_mae is None else min(0.0, raw_mae),
        outcome_schema_version=config.OUTCOME_SCHEMA_VERSION,
    )

    # Hit = intrabar touch (order-independent). Break = a *closing* price beyond the level.
    if snapshot.support is not None:
        outcome.did_hit_support = bool((window["low"] <= snapshot.support).any())
        outcome.did_break_support = bool((window["close"] < snapshot.support).any())
    if snapshot.resistance is not None:
        outcome.did_hit_resistance = bool((window["high"] >= snapshot.resistance).any())
        outcome.did_break_resistance = bool((window["close"] > snapshot.resistance).any())

    if snapshot.setup_invalidation is not None:
        outcome.did_hit_invalidation = bool((window["low"] <= snapshot.setup_invalidation).any())
    if snapshot.setup_target_1 is not None:
        outcome.did_hit_target_1 = bool((window["high"] >= snapshot.setup_target_1).any())
    if snapshot.setup_target_2 is not None:
        outcome.did_hit_target_2 = bool((window["high"] >= snapshot.setup_target_2).any())

    has_setup = snapshot.setup_entry_low is not None and snapshot.setup_entry_high is not None
    if has_setup:
        entry_low, entry_high = snapshot.setup_entry_low, snapshot.setup_entry_high
        entry_idx = _first_hit_index(window, lambda row: row["low"] <= entry_high and row["high"] >= entry_low)
        outcome.setup_entry_triggered = entry_idx is not None

        if entry_idx is not None:
            event_indexes: Dict[str, Optional[int]] = {}
            if snapshot.setup_invalidation is not None:
                event_indexes["invalidation"] = _first_hit_index(
                    window, lambda row: row["low"] <= snapshot.setup_invalidation
                )
            if snapshot.setup_target_1 is not None:
                event_indexes["target_1"] = _first_hit_index(
                    window, lambda row: row["high"] >= snapshot.setup_target_1
                )
            if snapshot.setup_target_2 is not None:
                event_indexes["target_2"] = _first_hit_index(
                    window, lambda row: row["high"] >= snapshot.setup_target_2
                )

            ambiguous = False
            for name, idx in event_indexes.items():
                if idx is None:
                    after = None  # event never happened within this window
                elif idx > entry_idx:
                    after = True
                elif idx < entry_idx:
                    after = False
                else:
                    after = None  # same bar as entry -- order within that session is unknowable from daily OHLC
                    ambiguous = True

                if name == "invalidation":
                    outcome.invalidation_after_entry = after
                elif name == "target_1":
                    outcome.target_1_after_entry = after
                elif name == "target_2":
                    outcome.target_2_after_entry = after

            # Two events colliding on the same bar as EACH OTHER (not just vs. entry)
            # are equally unresolvable ("did it hit target or invalidation first?").
            hit_only = [i for i in event_indexes.values() if i is not None]
            if len(hit_only) != len(set(hit_only)):
                ambiguous = True

            outcome.sequencing_ambiguous = ambiguous
        # else: entry never triggered -- *_after_entry fields stay their None default (not applicable)

    return outcome


def update_pending_outcomes() -> UpdateSummary:
    """
    Find snapshots with pending horizons, fetch real bars per symbol, and
    resolve whichever horizons now have enough elapsed trading sessions.

    A transient fetch failure (network/API error) leaves existing rows
    completely untouched — it's only reported in `summary.errors`, so a
    retry later can still succeed. A confirmed empty result for a symbol
    (the request succeeded but returned no bars at all) is a real, likely-
    recurring data problem (delisted, bad symbol) and is stored as
    status='ERROR' with a message, rather than left silently PENDING forever.
    """
    summary = UpdateSummary()
    db = get_db()
    pending = db.get_pending_outcomes_with_snapshots()
    if not pending:
        return summary

    by_snapshot: Dict[int, Tuple[ResearchSnapshot, List[ResearchOutcome]]] = {}
    for outcome, snapshot in pending:
        by_snapshot.setdefault(snapshot.id, (snapshot, []))[1].append(outcome)

    try:
        client = get_data_client()
    except RuntimeError as exc:
        summary.errors.append(f"Alpaca not configured: {exc}")
        summary.still_pending = len(pending)
        return summary

    now = datetime.now(timezone.utc)
    for snapshot_id, (snapshot, outcomes) in by_snapshot.items():
        summary.snapshots_checked += 1
        try:
            market_date = pd.Timestamp(snapshot.market_timestamp).date()
            lookback_days = max((now.date() - market_date).days + config.OUTCOME_FETCH_BUFFER_DAYS, 1)
            bars_by_symbol = fetch_daily_bars(client, [snapshot.symbol], lookback_days=lookback_days)
        except Exception as exc:  # noqa: BLE001 - a transient failure must never corrupt stored rows
            logger.warning("Outcome fetch failed for snapshot %s (%s): %s", snapshot_id, snapshot.symbol, exc)
            summary.errors.append(f"{snapshot.symbol} (snapshot {snapshot_id}): {exc}")
            summary.still_pending += len(outcomes)
            continue

        bars_df = bars_by_symbol.get(snapshot.symbol)
        if bars_df is None or bars_df.empty:
            for outcome in outcomes:
                outcome.status = "ERROR"
                outcome.error_message = f"No bar data returned for {snapshot.symbol}."
                outcome.outcome_schema_version = config.OUTCOME_SCHEMA_VERSION
                db.update_outcome(outcome)
                summary.errors.append(
                    f"{snapshot.symbol} (snapshot {snapshot_id}, {outcome.horizon_trading_days}d): no data"
                )
            continue

        bars_after = _bars_after(bars_df, snapshot.market_timestamp)
        for outcome in outcomes:
            resolved = _evaluate_horizon(snapshot, bars_after, outcome.horizon_trading_days)
            if resolved is None:
                summary.still_pending += 1
                continue
            resolved.id = outcome.id
            db.update_outcome(resolved)
            summary.outcomes_updated += 1

    return summary


def recompute_completed_outcomes() -> UpdateSummary:
    """
    Maintenance-only: re-derives every COMPLETED outcome row from real
    historical bars using the CURRENT _evaluate_horizon() logic (e.g. after
    a formula fix such as the MFE/MAE clamp). Reusing _evaluate_horizon()
    wholesale -- rather than patching individual fields in place -- means
    every derived field on a recomputed row stays mutually consistent, with
    no risk of a partial update leaving stale fields behind.

    Distinct from update_pending_outcomes(), which only ever resolves
    PENDING rows and is left untouched by this function. research_snapshots
    rows are never read for writing and never modified -- only
    research_outcomes rows change. Never calls Claude.
    """
    summary = UpdateSummary()
    db = get_db()
    completed = db.get_completed_outcomes_with_snapshots()
    if not completed:
        return summary

    by_snapshot: Dict[int, Tuple[ResearchSnapshot, List[ResearchOutcome]]] = {}
    for outcome, snapshot in completed:
        by_snapshot.setdefault(snapshot.id, (snapshot, []))[1].append(outcome)

    try:
        client = get_data_client()
    except RuntimeError as exc:
        summary.errors.append(f"Alpaca not configured: {exc}")
        return summary

    now = datetime.now(timezone.utc)
    for snapshot_id, (snapshot, outcomes) in by_snapshot.items():
        summary.snapshots_checked += 1
        try:
            market_date = pd.Timestamp(snapshot.market_timestamp).date()
            lookback_days = max((now.date() - market_date).days + config.OUTCOME_FETCH_BUFFER_DAYS, 1)
            bars_by_symbol = fetch_daily_bars(client, [snapshot.symbol], lookback_days=lookback_days)
        except Exception as exc:  # noqa: BLE001 - a transient failure must never corrupt stored rows
            logger.warning("Recompute fetch failed for snapshot %s (%s): %s", snapshot_id, snapshot.symbol, exc)
            summary.errors.append(f"{snapshot.symbol} (snapshot {snapshot_id}): {exc}")
            continue

        bars_df = bars_by_symbol.get(snapshot.symbol)
        if bars_df is None or bars_df.empty:
            summary.errors.append(f"{snapshot.symbol} (snapshot {snapshot_id}): no data available for recompute")
            continue

        bars_after = _bars_after(bars_df, snapshot.market_timestamp)
        for outcome in outcomes:
            resolved = _evaluate_horizon(snapshot, bars_after, outcome.horizon_trading_days)
            if resolved is None:
                summary.errors.append(
                    f"{snapshot.symbol} (snapshot {snapshot_id}, {outcome.horizon_trading_days}d): "
                    "not enough bars to recompute an already-COMPLETED outcome"
                )
                continue
            resolved.id = outcome.id
            db.update_outcome(resolved)
            summary.outcomes_updated += 1

    return summary
