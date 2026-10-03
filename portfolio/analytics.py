"""
portfolio/analytics.py — Deterministic portfolio calculations (Stage 2.7C). Pure Python + Decimal.

Units: money in the account currency (rounded to cents for output); every *_pct /
*weight* field is a PERCENT (0–100) rounded to 2 decimals. Totals are computed
from unrounded values.

Conventions (documented, not hidden):
  - Weights and cash % use the CALCULATED total (valued positions + cash + other
    Robinhood-reported asset values) so they are internally consistent. Robinhood's
    reported total/equity value is kept separately; the difference is valuation_gap.
  - A position whose quote is UNRELIABLE/UNAVAILABLE gets no market value and no
    weight; valuation_complete becomes False. It is never valued at zero.
  - A missing average cost means basis unavailable: no cost basis, no P&L for that
    position, and unrealized_pnl_complete becomes False. It is never treated as $0.
  - Day change = (selected price - adjusted previous close) x (quantity - intraday
    quantity); shares bought in the current session are excluded (day_change_partial).
This module never reads or writes the database and never touches Stage 2 evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional

from portfolio.models import (PortfolioSnapshot, Position, Quote, RawPosition, RealizedPnlSummary, TaxLot, dec,
                              day)
from portfolio.quotes import QUALITY_RANK, assess_quote, reference_close

CENT = Decimal("0.01")
HUNDRED = Decimal("100")
EVENT_LEVELS = ["HIGH", "MEDIUM", "LOW", "NONE", "UNKNOWN"]


def _no_negative_zero(d: Decimal) -> Decimal:
    return abs(d) if d == 0 else d


def money(v: Optional[Decimal]) -> Optional[Decimal]:
    return None if v is None else _no_negative_zero(v.quantize(CENT, rounding=ROUND_HALF_UP))


def pct(numer: Optional[Decimal], denom: Optional[Decimal]) -> Optional[Decimal]:
    if numer is None or denom is None or denom == 0:
        return None
    return _no_negative_zero((numer / denom * HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP))


@dataclass
class PortfolioView:
    status: str                                   # OK | STALE
    snapshot: PortfolioSnapshot
    positions: List[Position]
    sector_exposure: List[dict]
    event_exposure: List[dict]
    quote_quality_summary: dict
    basis_summary: dict
    freshness: dict
    messages: List[str] = field(default_factory=list)


def build_position(raw: RawPosition, quote: Optional[Quote], now: datetime, stale_after_s: int) -> Position:
    qa = assess_quote(quote, now, stale_after_s)
    qty = raw.quantity
    mv = qty * qa.price if (qa.usable and qty is not None and qa.price is not None) else None
    basis = qty * raw.avg_cost if (qty is not None and raw.avg_cost is not None) else None
    unreal = mv - basis if (mv is not None and basis is not None) else None
    ref = reference_close(quote)
    day_change, partial = None, False
    if qa.usable and qa.price is not None and ref is not None and qty is not None:
        intraday = raw.intraday_quantity or Decimal("0")
        partial = intraday > 0
        day_change = (qa.price - ref) * (qty - intraday)
    if mv is not None:
        ref, ref_src = mv, "MARKET"
    elif qty is not None and quote is not None and (quote.adjusted_previous_close or quote.previous_close):
        ref, ref_src = qty * (quote.adjusted_previous_close or quote.previous_close), "PREVIOUS_CLOSE"
    elif basis is not None:
        ref, ref_src = basis, "COST_BASIS"
    else:
        ref, ref_src = None, None
    return Position(
        symbol=raw.symbol, quantity=qty, intraday_quantity=raw.intraday_quantity,
        sellable_quantity=raw.sellable_quantity, held_quantity=raw.held_quantity, side=raw.side,
        avg_cost=raw.avg_cost, cost_basis_total=money(basis), last_price=qa.price, price_timestamp=qa.price_timestamp,
        price_source=qa.price_source, market_value=mv, unrealized_pnl=money(unreal),
        unrealized_pnl_pct=pct(unreal, basis), day_change=money(day_change), day_change_partial=partial,
        portfolio_weight=None, quote_crossed=qa.crossed, quote_quality=qa.quality, quote_issues=qa.issues,
        quote_age_seconds=qa.age_seconds, basis_available=raw.avg_cost is not None,
        reference_value=money(ref), reference_value_source=ref_src,
    )


def _event_summary(bundle: Any) -> tuple[str, Optional[dict], Optional[str]]:
    if bundle is None:
        return "UNKNOWN", None, None
    nearest = None
    ev = getattr(bundle, "nearest_event", None)
    prox = getattr(bundle, "nearest_event_proximity", None)
    if ev is not None:
        nearest = {
            "title": ev.title,
            "event_type": getattr(ev.event_type, "value", str(ev.event_type)),
            "event_date": ev.event_date.isoformat() if ev.event_date else None,
            "time_precision": getattr(ev.time_precision, "value", None),
            "source": ev.source,
            "hours_until": round(prox.hours_until, 1) if prox is not None and prox.hours_until is not None else None,
        }
    return bundle.event_risk_level or "UNKNOWN", nearest, getattr(bundle, "data_quality", None)


def build_view(portfolio_data: dict, portfolio_meta: dict, positions_data: dict, positions_meta: dict,
               realized: Optional[RealizedPnlSummary], *, now: datetime, stale_after_s: int,
               gap_material_pct: float, sector_fn: Callable[[str], Optional[str]],
               event_fn: Optional[Callable[[str], Any]] = None,
               classify_fn: Optional[Callable[[str], Any]] = None) -> PortfolioView:
    snap = PortfolioSnapshot.from_gateway(portfolio_data, portfolio_meta.get("fetched_at"))
    quotes = {s: Quote.from_gateway(q) for s, q in (positions_data.get("quotes") or {}).items()}
    raws = [RawPosition.from_gateway(p) for p in positions_data.get("positions", [])]
    positions = [build_position(r, quotes.get(r.symbol), now, stale_after_s) for r in raws]

    valued = [p for p in positions if p.market_value is not None]
    snap.valuation_complete = len(valued) == len(positions)
    pmv = sum((p.market_value for p in valued), Decimal("0"))
    snap.positions_market_value = money(pmv)

    others = [snap.options_value, snap.crypto_value, snap.futures_value, snap.event_contracts_value,
              snap.mutual_funds_value, snap.fixed_income_value]
    other_total = sum((v for v in others if v is not None), Decimal("0"))
    calc_total = pmv + snap.cash + other_total if snap.cash is not None else None
    snap.calculated_total_value = money(calc_total)
    snap.cash_pct = pct(snap.cash, calc_total)
    for p in positions:
        p.portfolio_weight = pct(p.market_value, calc_total)
        p.market_value = money(p.market_value)

    if snap.valuation_complete and snap.equity_value is not None:
        gap = snap.equity_value - pmv
        snap.valuation_gap = money(gap)
        snap.valuation_gap_pct = pct(gap, snap.equity_value)
        snap.valuation_gap_material = (snap.valuation_gap_pct is not None
                                       and abs(snap.valuation_gap_pct) >= Decimal(str(gap_material_pct)))

    with_basis = [p for p in positions if p.cost_basis_total is not None]
    snap.total_cost_basis = money(sum((p.cost_basis_total for p in with_basis), Decimal("0"))) if with_basis else None
    both = [p for p in positions if p.unrealized_pnl is not None and p.cost_basis_total is not None]
    snap.unrealized_pnl_complete = len(both) == len(positions)
    if both:
        tot_unreal = sum((p.unrealized_pnl for p in both), Decimal("0"))
        snap.total_unrealized_pnl = money(tot_unreal)
        snap.total_unrealized_pnl_pct = pct(tot_unreal, sum((p.cost_basis_total for p in both), Decimal("0")))
    if realized is not None:
        snap.realized_pnl_window = money(realized.total_returns)
        snap.realized_pnl_span = realized.span

    # sectors (existing curated data/sector_map.py only; unmapped -> UNCLASSIFIED, never guessed)
    sectors: Dict[str, dict] = {}
    for p in positions:
        if classify_fn is not None:
            c = classify_fn(p.symbol)
            p.sector, p.sector_source = c.sector, c.source
        else:
            p.sector = sector_fn(p.symbol) or "UNCLASSIFIED"
        s = sectors.setdefault(p.sector, {"sector": p.sector, "market_value": Decimal("0"), "symbols": [],
                                          "unvalued_symbols": []})
        s["symbols"].append(p.symbol)
        if p.market_value is None:
            s["unvalued_symbols"].append(p.symbol)
        else:
            s["market_value"] += p.market_value
    sector_exposure = sorted(({**s, "market_value": money(s["market_value"]),
                               "weight_pct": pct(s["market_value"], calc_total)} for s in sectors.values()),
                             key=lambda s: s["market_value"] or Decimal("0"), reverse=True)

    # Stage 2.6 event context joined by symbol (read-only; event scoring untouched)
    levels: Dict[str, dict] = {lvl: {"level": lvl, "market_value": Decimal("0"), "symbols": []} for lvl in EVENT_LEVELS}
    for p in positions:
        bundle = None
        if event_fn is not None:
            try:
                bundle = event_fn(p.symbol)
            except Exception:  # noqa: BLE001 - an event failure marks UNKNOWN, never breaks the view
                bundle = None
        p.event_risk_level, p.nearest_event, _ = _event_summary(bundle)
        if p.event_risk_level not in levels:
            p.event_risk_level = "UNKNOWN"
        bucket = levels[p.event_risk_level]
        bucket["symbols"].append(p.symbol)
        if p.market_value is not None:
            bucket["market_value"] += p.market_value
    event_exposure = [{**b, "market_value": money(b["market_value"]), "weight_pct": pct(b["market_value"], calc_total)}
                      for b in levels.values() if b["symbols"]]

    counts = {q: 0 for q in QUALITY_RANK}
    for p in positions:
        counts[p.quote_quality] += 1
    quote_summary = {
        "counts": counts,
        "worst": max((p.quote_quality for p in positions), key=lambda q: QUALITY_RANK[q], default="OK"),
        "issues": [{"symbol": p.symbol, "quality": p.quote_quality, "issues": p.quote_issues,
                    "age_seconds": p.quote_age_seconds} for p in positions if p.quote_issues],
        "stale_after_seconds": stale_after_s,
    }
    basis_summary = {
        "available": sum(1 for p in positions if p.basis_available),
        "missing": sum(1 for p in positions if not p.basis_available),
        "missing_symbols": [p.symbol for p in positions if not p.basis_available],
    }
    status = "STALE" if "STALE" in (portfolio_meta.get("status"), positions_meta.get("status")) else "OK"
    messages = [m for m in (portfolio_meta.get("message"), positions_meta.get("message")) if m]
    if not snap.valuation_complete:
        messages.append("Some positions could not be valued (quote unreliable or unavailable); totals, weights and "
                        "the valuation gap exclude them.")
    if not snap.unrealized_pnl_complete:
        messages.append("Cost basis is unavailable for some positions; total unrealized P&L excludes them.")
    freshness = {
        "portfolio": {k: portfolio_meta.get(k) for k in ("status", "fetched_at", "cache_age_s")},
        "positions": {k: positions_meta.get(k) for k in ("status", "fetched_at", "cache_age_s")},
        "quotes_component": (positions_meta.get("components") or {}).get("quotes"),
        "provider_as_of": "UNAVAILABLE",
        "computed_at": now.isoformat(timespec="seconds"),
    }
    positions.sort(key=lambda p: p.market_value if p.market_value is not None else Decimal("-1"), reverse=True)
    return PortfolioView(status, snap, positions, sector_exposure, event_exposure, quote_summary, basis_summary,
                         freshness, messages)


def long_term_date(open_date: date) -> date:
    """US rule of thumb: long-term after holding MORE than one year -> first long-term day is anniversary + 1."""
    try:
        anniversary = open_date.replace(year=open_date.year + 1)
    except ValueError:  # Feb 29
        anniversary = open_date.replace(year=open_date.year + 1, day=28)
    return anniversary + timedelta(days=1)


def build_tax_lots(lots: List[dict], quote: Optional[Quote], *, today: date, now: datetime,
                   stale_after_s: int) -> tuple[List[TaxLot], dict]:
    qa = assess_quote(quote, now, stale_after_s)
    out: List[TaxLot] = []
    for d in lots:
        qty = dec(d.get("quantity"))
        cps, basis = dec(d.get("cost_per_share")), dec(d.get("cost_basis"))
        opened = day(d.get("open_date"))
        mv = qty * qa.price if (qa.usable and qty is not None and qa.price is not None) else None
        pending = cps is None or basis is None
        term = d.get("term")
        out.append(TaxLot(
            lot_id=d.get("lot_id"), symbol=d.get("symbol") or "", open_type=d.get("open_type"),
            order_id=d.get("order_id"), quantity=qty, quantity_available=dec(d.get("quantity_available")),
            selectable=d.get("selectable"), cost_per_share=cps, cost_basis=basis, open_date=opened, term=term,
            basis_pending=pending,
            days_held=(today - opened).days if opened else None,
            days_to_long_term=(max(0, (long_term_date(opened) - today).days) if opened and term == "st"
                               else (0 if term == "lt" else None)),
            market_value=money(mv),
            unrealized_pnl=money(mv - basis) if (mv is not None and not pending) else None,
        ))
    quote_info = {"quality": qa.quality, "issues": qa.issues, "price": qa.price,
                  "price_timestamp": qa.price_timestamp, "price_source": qa.price_source}
    return out, quote_info
