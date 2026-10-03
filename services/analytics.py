"""
services/analytics.py — Deterministic performance analytics over saved,
COMPLETED outcomes. No Claude anywhere in this file; every number is a
plain aggregate over real, already-measured returns.

Per the review correction: catalyst/risk-flag grouping parses
catalysts_json/risk_flags_json with json.loads() and filters on actual
structured fields (e.g. catalyst["sentiment"] == "POSITIVE") — never a
SQL LIKE against the raw JSON text, which would be brittle and could
match unrelated substrings. Malformed historical JSON is skipped and
counted, never allowed to crash the aggregation.

These are historical observations of this agent's own saved research —
never a probability of future returns, and never described as such.
"""
import json
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import config
from database.database import get_db
from database.models import ResearchOutcome, ResearchSnapshot

EVIDENCE_BUCKETS = [(0, 39), (40, 49), (50, 59), (60, 69), (70, 79), (80, 89), (90, 100)]


@dataclass
class GroupStats:
    label: str
    n: int
    avg_return_pct: Optional[float]
    median_return_pct: Optional[float]
    positive_return_frequency: Optional[float]  # fraction of rows with return_pct > 0
    avg_mfe_pct: Optional[float]
    avg_mae_pct: Optional[float]
    target_1_hit_rate: Optional[float]
    invalidation_hit_rate: Optional[float]
    sample_warning: Optional[str]


@dataclass
class PerformanceReport:
    total_snapshots: int
    completed_snapshots: int
    pending_snapshots: int
    data_issues: int  # rows whose stored JSON couldn't be parsed -- skipped, not crashed on
    overall_by_horizon: Dict[int, GroupStats] = field(default_factory=dict)
    evidence_buckets_by_horizon: Dict[int, List[GroupStats]] = field(default_factory=dict)
    research_view_by_horizon: Dict[int, List[GroupStats]] = field(default_factory=dict)
    catalyst_presence_by_horizon: Dict[int, List[GroupStats]] = field(default_factory=dict)
    catalyst_sentiment_by_horizon: Dict[int, List[GroupStats]] = field(default_factory=dict)
    risk_flag_presence_by_horizon: Dict[int, List[GroupStats]] = field(default_factory=dict)


def _sample_warning(n: int) -> Optional[str]:
    if n < config.ANALYTICS_MIN_SAMPLE_SIZE_LOW:
        return "Very small sample — not enough observations for a reliable conclusion."
    if n < config.ANALYTICS_MIN_SAMPLE_SIZE_CAUTION:
        return "Small sample — interpret cautiously."
    return None


def _aggregate(label: str, rows: List[ResearchOutcome]) -> GroupStats:
    n = len(rows)
    if n == 0:
        return GroupStats(label, 0, None, None, None, None, None, None, None, _sample_warning(0))

    returns = [r.return_pct for r in rows if r.return_pct is not None]
    mfes = [r.max_favorable_excursion_pct for r in rows if r.max_favorable_excursion_pct is not None]
    maes = [r.max_adverse_excursion_pct for r in rows if r.max_adverse_excursion_pct is not None]
    target_hits = [r.did_hit_target_1 for r in rows if r.did_hit_target_1 is not None]
    invalidation_hits = [r.did_hit_invalidation for r in rows if r.did_hit_invalidation is not None]

    return GroupStats(
        label=label,
        n=n,
        avg_return_pct=round(sum(returns) / len(returns), 3) if returns else None,
        median_return_pct=round(statistics.median(returns), 3) if returns else None,
        positive_return_frequency=round(sum(1 for r in returns if r > 0) / len(returns), 3) if returns else None,
        avg_mfe_pct=round(sum(mfes) / len(mfes), 3) if mfes else None,
        avg_mae_pct=round(sum(maes) / len(maes), 3) if maes else None,
        target_1_hit_rate=round(sum(1 for h in target_hits if h) / len(target_hits), 3) if target_hits else None,
        invalidation_hit_rate=(
            round(sum(1 for h in invalidation_hits if h) / len(invalidation_hits), 3) if invalidation_hits else None
        ),
        sample_warning=_sample_warning(n),
    )


def _evidence_bucket_label(bullish_pct: int) -> str:
    for low, high in EVIDENCE_BUCKETS:
        if low <= bullish_pct <= high:
            return f"{low}-{high}"
    return "unknown"


def _safe_json_list(raw: Optional[str], data_issues: List[int], row_id: int) -> List[Dict[str, Any]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        data_issues.append(row_id)
        return []


def compute_performance_report() -> PerformanceReport:
    db = get_db()
    counts = db.count_snapshots()
    completed_pairs: List[Tuple[ResearchOutcome, ResearchSnapshot]] = db.get_completed_outcomes_with_snapshots()

    data_issue_ids: List[int] = []
    by_horizon: Dict[int, List[Tuple[ResearchOutcome, ResearchSnapshot]]] = {}
    for outcome, snapshot in completed_pairs:
        by_horizon.setdefault(outcome.horizon_trading_days, []).append((outcome, snapshot))

    report = PerformanceReport(
        total_snapshots=counts["total"],
        completed_snapshots=counts["completed"],
        pending_snapshots=counts["pending"],
        data_issues=0,
    )

    for horizon, pairs in by_horizon.items():
        outcomes_only = [o for o, _ in pairs]
        report.overall_by_horizon[horizon] = _aggregate(f"{horizon}D overall", outcomes_only)

        # Evidence-bucket breakdown
        buckets: Dict[str, List[ResearchOutcome]] = {}
        for outcome, snapshot in pairs:
            label = _evidence_bucket_label(snapshot.bullish_pct)
            buckets.setdefault(label, []).append(outcome)
        report.evidence_buckets_by_horizon[horizon] = [
            _aggregate(f"Bullish {label}%", rows) for label, rows in sorted(buckets.items())
        ]

        # Research View breakdown
        views: Dict[str, List[ResearchOutcome]] = {}
        for outcome, snapshot in pairs:
            views.setdefault(snapshot.research_view, []).append(outcome)
        report.research_view_by_horizon[horizon] = [_aggregate(label, rows) for label, rows in views.items()]

        # Catalyst presence / sentiment
        with_catalyst: List[ResearchOutcome] = []
        without_catalyst: List[ResearchOutcome] = []
        by_sentiment: Dict[str, List[ResearchOutcome]] = {}
        for outcome, snapshot in pairs:
            items = _safe_json_list(snapshot.catalysts_json, data_issue_ids, snapshot.id)
            if items:
                with_catalyst.append(outcome)
                sentiments = {item.get("sentiment") for item in items if isinstance(item, dict)}
                for sentiment in sentiments:
                    if sentiment:
                        by_sentiment.setdefault(sentiment, []).append(outcome)
            else:
                without_catalyst.append(outcome)
        report.catalyst_presence_by_horizon[horizon] = [
            _aggregate("Catalyst present", with_catalyst),
            _aggregate("No catalyst", without_catalyst),
        ]
        report.catalyst_sentiment_by_horizon[horizon] = [
            _aggregate(f"Catalyst: {sentiment}", rows) for sentiment, rows in sorted(by_sentiment.items())
        ]

        # Risk-flag presence / by code
        with_flag: Dict[str, List[ResearchOutcome]] = {}
        no_flags: List[ResearchOutcome] = []
        for outcome, snapshot in pairs:
            flags = _safe_json_list(snapshot.risk_flags_json, data_issue_ids, snapshot.id)
            if not flags:
                no_flags.append(outcome)
            for flag in flags:
                if isinstance(flag, dict) and flag.get("code"):
                    with_flag.setdefault(flag["code"], []).append(outcome)
        flag_groups = [_aggregate(f"Flag: {code}", rows) for code, rows in sorted(with_flag.items())]
        flag_groups.append(_aggregate("No risk flags", no_flags))
        report.risk_flag_presence_by_horizon[horizon] = flag_groups

    report.data_issues = len(set(data_issue_ids))
    return report
