"""Stage 2.7E test fixtures (synthetic, deterministic; no network)."""
import copy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from analysis.indicators import TickerMetrics
from data.events.models import (EventCategory, EventImportance, EventStatus, EventType, NormalizedEvent,
                                TimePrecision)
from pf_fixtures import NOW, PORTFOLIO, POSITIONS, REALIZED
from portfolio.analytics import build_view
from portfolio.models import RealizedPnlSummary
from portfolio.policy import build_rules
from portfolio.sectors import SectorResolver
from services.event_context import EventsBundle

RESOLVER = SectorResolver()
RULES = build_rules()[0]


def tm(symbol="MU", **kw) -> TickerMetrics:
    base = dict(symbol=symbol, price=12.0, price_source="latest_trade", as_of=pd.Timestamp("2026-09-27T23:59:00Z"),
                prev_close=11.9, pct_change=0.8, gap_pct=0.1, volume=1e6, avg_volume=9e5, relative_volume=1.2,
                volume_expansion=1.0, rsi=55.0, ema_fast=11.8, ema_medium=11.5, ema_slow=11.0, trend="Bullish",
                atr=0.4, high_20d=13.0, low_20d=10.0, dist_from_high_pct=-7.7, dist_from_low_pct=20.0,
                support=10.5, resistance=13.5, dist_from_support_pct=14.3, dist_from_resistance_pct=-11.1,
                momentum_5d_pct=2.0, momentum_10d_pct=3.0, volatility_pct=30.0, volatility_expansion=1.0,
                momentum_score=62, relative_volume_score=30, trend_strength_score=50, volatility_score=30,
                attention_score=50, signal="WATCH")
    base.update(kw)
    return TickerMetrics(**base)


def view(positions=POSITIONS, events=None):
    return build_view(copy.deepcopy(PORTFOLIO), {"status": "OK", "fetched_at": "2026-09-28T00:04:59+00:00"},
                      copy.deepcopy(positions), {"status": "OK"}, RealizedPnlSummary.from_gateway(REALIZED), now=NOW,
                      stale_after_s=900, gap_material_pct=0.5, sector_fn=RESOLVER.sector_for, event_fn=events,
                      classify_fn=RESOLVER.classify)


def ev(title="Employment Situation", etype=EventType.EMPLOYMENT_SITUATION, hours=120.0,
       importance=EventImportance.HIGH, now=None) -> NormalizedEvent:
    now = now or datetime.now(timezone.utc)
    when = now + timedelta(hours=hours)
    return NormalizedEvent(event_id=f"{etype.value}-{hours}", event_type=etype,
                           category=EventCategory.MACRO if etype.value not in ("EARNINGS", "DIVIDEND") else
                           EventCategory.COMPANY, symbol=None, title=title, event_date=when.date(),
                           event_time=when.time(), timezone="UTC", event_datetime_utc=when,
                           time_precision=TimePrecision.EXACT, source="Test source", source_url=None,
                           source_provider="FRED", status=EventStatus.UPCOMING, importance=importance, confirmed=True,
                           retrieved_at=now)


def bundle(level="LOW", macro=None, company=None, earnings_available=False) -> EventsBundle:
    return EventsBundle(company_events=company or [], macro_events=macro or [], event_risk_level=level,
                        data_quality="MEDIUM", earnings_available=earnings_available,
                        earnings_reason=None if earnings_available else "Earnings provider unavailable.")


def research(view_label="BULLISH BIAS", bull=52, neu=34, bear=14, age_hours=2.0, cats=None):
    from insights.research import ResearchFacts
    now = datetime.now(timezone.utc)
    return ResearchFacts("MU", "SAVED_SNAPSHOT", view_label, bull, neu, bear,
                         cats or {"technical": 0.4, "catalyst": 0.2, "risk": -0.1, "market": 0.1, "sector": 0.2},
                         "HIGH", now - timedelta(hours=age_hours), age_hours, stale=age_hours > 24)


def no_research():
    from insights.research import ResearchFacts
    return ResearchFacts("MU", "UNAVAILABLE")


def market(environment="MIXED", **kw):
    base = {"available": True, "environment": environment, "trend": "MIXED", "volatility": "NORMAL",
            "breadth_label": "MIXED", "market_today": "MIXED", "fetched_at": "2026-09-28T00:00:00+00:00"}
    base.update(kw)
    return base


def sector_ctx(pct):
    from analysis.sector_context import SectorContext
    return SectorContext("Semiconductors", "SOXX", pct, None, None, "SPY", 0.1)


def bars(n=140, start=100.0, step=0.3, end=date(2026, 9, 25), amp=6.0) -> pd.DataFrame:
    days = pd.bdate_range(end=end, periods=n)
    close = np.array([start + step * i + amp * np.sin(i / 4) for i in range(n)])
    return pd.DataFrame({"timestamp": pd.to_datetime(days).tz_localize("UTC"), "open": close - 0.2,
                         "high": close + 1.0, "low": close - 1.0, "close": close, "volume": np.full(n, 1_000_000.0)})


def snapshot(created, view_label="BULLISH BIAS", bull=55, bear=10, event_level="LOW", sid=1):
    return SimpleNamespace(id=sid, created_at=created, research_view=view_label, bullish_pct=bull, neutral_pct=100 - bull - bear,
                           bearish_pct=bear, evidence_technical=0.3, evidence_catalyst=0.1, evidence_risk=0.0,
                           evidence_market=0.1, evidence_sector=0.1, data_quality_level="HIGH",
                           event_risk_level=event_level)
