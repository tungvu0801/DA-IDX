"""
portfolio/models.py — Provider-agnostic, read-only portfolio models (Stage 2.7C).

Every monetary/quantity value is a Decimal parsed from the gateway's Decimal
strings. A value the provider did not supply stays None — it is NEVER turned
into zero (a missing cost basis is "basis unavailable", not "$0 basis").
Nothing here can express an order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, List, Optional

PROVIDER_AS_OF_UNAVAILABLE = "UNAVAILABLE"  # Robinhood supplies no as-of time for portfolio/positions


def dec(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def ts(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 provider timestamp (nanosecond precision tolerated). None if absent/invalid."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    # Python accepts at most 6 fractional digits; Robinhood sends up to 9.
    if "." in s:
        head, _, tail = s.partition(".")
        frac = "".join(ch for ch in tail if ch.isdigit())
        rest = tail[len(frac):]
        s = f"{head}.{frac[:6]}{rest}"
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


@dataclass(frozen=True)
class AccountSummary:
    alias: str
    masked_id: str
    account_type: Optional[str]
    brokerage_account_type: Optional[str]
    state: Optional[str]
    unsettled_funds: Optional[Decimal]
    agent_tradable: Optional[bool]
    is_default: Optional[bool]

    @classmethod
    def from_gateway(cls, d: dict) -> "AccountSummary":
        return cls(alias=d["alias"], masked_id=d["masked_id"], account_type=d.get("account_type"),
                   brokerage_account_type=d.get("brokerage_account_type"), state=d.get("state"),
                   unsettled_funds=dec(d.get("unsettled_funds")), agent_tradable=d.get("agent_tradable"),
                   is_default=d.get("is_default"))


@dataclass(frozen=True)
class Quote:
    symbol: str
    last_trade_price: Optional[Decimal]
    last_trade_time: Optional[datetime]
    last_non_reg_trade_price: Optional[Decimal]
    last_non_reg_trade_time: Optional[datetime]
    adjusted_previous_close: Optional[Decimal]
    previous_close: Optional[Decimal]
    bid_price: Optional[Decimal]
    ask_price: Optional[Decimal]
    has_traded: Optional[bool]
    state: Optional[str]
    official_close_price: Optional[Decimal]
    official_close_date: Optional[str]

    @classmethod
    def from_gateway(cls, d: dict) -> "Quote":
        close = d.get("official_close") or {}
        return cls(symbol=d.get("symbol") or "", last_trade_price=dec(d.get("last_trade_price")),
                   last_trade_time=ts(d.get("venue_last_trade_time")),
                   last_non_reg_trade_price=dec(d.get("last_non_reg_trade_price")),
                   last_non_reg_trade_time=ts(d.get("venue_last_non_reg_trade_time")),
                   adjusted_previous_close=dec(d.get("adjusted_previous_close")),
                   previous_close=dec(d.get("previous_close")), bid_price=dec(d.get("bid_price")),
                   ask_price=dec(d.get("ask_price")), has_traded=d.get("has_traded"), state=d.get("state"),
                   official_close_price=dec(close.get("price")), official_close_date=close.get("date"))


@dataclass
class QuoteAssessment:
    quality: str                        # OK | STALE | DEGRADED | UNRELIABLE | UNAVAILABLE
    usable: bool                        # price may be used for valuation (OK/STALE/DEGRADED)
    price: Optional[Decimal]
    price_timestamp: Optional[datetime]  # the venue timestamp of the selected trade, never invented
    price_source: Optional[str]          # REGULAR_LAST | EXTENDED_LAST
    age_seconds: Optional[int]
    crossed: bool
    issues: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RawPosition:
    symbol: str
    quantity: Optional[Decimal]
    intraday_quantity: Optional[Decimal]
    sellable_quantity: Optional[Decimal]
    held_quantity: Optional[Decimal]
    side: Optional[str]
    avg_cost: Optional[Decimal]

    @classmethod
    def from_gateway(cls, d: dict) -> "RawPosition":
        held_parts = [dec(d.get(k)) for k in ("shares_held_for_sells", "shares_held_for_stock_grants",
                                              "shares_held_for_options_events", "shares_held_for_asset_transfer",
                                              "shares_pending_from_options_events")]
        held = None if any(p is None for p in held_parts) else sum(held_parts, Decimal("0"))
        return cls(symbol=d.get("symbol") or "", quantity=dec(d.get("quantity")),
                   intraday_quantity=dec(d.get("intraday_quantity")),
                   sellable_quantity=dec(d.get("shares_available_for_sells")), held_quantity=held,
                   side=d.get("type"), avg_cost=dec(d.get("average_buy_price")))


@dataclass
class Position:
    symbol: str
    quantity: Optional[Decimal]
    intraday_quantity: Optional[Decimal]
    sellable_quantity: Optional[Decimal]
    held_quantity: Optional[Decimal]
    side: Optional[str]
    avg_cost: Optional[Decimal]
    cost_basis_total: Optional[Decimal]
    last_price: Optional[Decimal]
    price_timestamp: Optional[datetime]
    price_source: Optional[str]
    market_value: Optional[Decimal]
    unrealized_pnl: Optional[Decimal]
    unrealized_pnl_pct: Optional[Decimal]
    day_change: Optional[Decimal]
    day_change_partial: bool             # True when shares bought in the current session are excluded
    portfolio_weight: Optional[Decimal]
    quote_crossed: bool
    quote_quality: str
    quote_issues: List[str]
    quote_age_seconds: Optional[int]
    basis_available: bool
    sector: str = "UNCLASSIFIED"
    event_risk_level: str = "UNKNOWN"
    nearest_event: Optional[dict] = None
    # Stage 2.7D: reference value used ONLY to weight data-quality policy metrics when no live market
    # value exists (MARKET = market value; PREVIOUS_CLOSE = qty x adjusted previous close; COST_BASIS).
    reference_value: Optional[Decimal] = None
    reference_value_source: Optional[str] = None
    sector_source: Optional[str] = None


@dataclass
class PortfolioSnapshot:
    account_alias: str
    account_masked_id: str
    total_value: Optional[Decimal]          # Robinhood-reported
    equity_value: Optional[Decimal]         # Robinhood-reported
    cash: Optional[Decimal]
    buying_power: Optional[Decimal]
    unleveraged_buying_power: Optional[Decimal]
    options_value: Optional[Decimal]
    crypto_value: Optional[Decimal]
    futures_value: Optional[Decimal]
    event_contracts_value: Optional[Decimal]
    mutual_funds_value: Optional[Decimal]
    fixed_income_value: Optional[Decimal]
    currency: Optional[str]
    fetched_at: Optional[str]               # local gateway fetch time
    provider_as_of: str = PROVIDER_AS_OF_UNAVAILABLE
    positions_market_value: Optional[Decimal] = None   # calculated from positions x quotes
    calculated_total_value: Optional[Decimal] = None   # calculated positions + cash + other reported assets
    valuation_gap: Optional[Decimal] = None            # Robinhood equity_value - calculated positions value
    valuation_gap_pct: Optional[Decimal] = None
    valuation_gap_material: bool = False
    valuation_complete: bool = True
    total_cost_basis: Optional[Decimal] = None
    total_unrealized_pnl: Optional[Decimal] = None
    total_unrealized_pnl_pct: Optional[Decimal] = None
    unrealized_pnl_complete: bool = True
    cash_pct: Optional[Decimal] = None
    realized_pnl_window: Optional[Decimal] = None
    realized_pnl_span: Optional[str] = None

    @classmethod
    def from_gateway(cls, d: dict, fetched_at: Optional[str]) -> "PortfolioSnapshot":
        acct = d.get("account") or {}
        return cls(account_alias=acct.get("alias", ""), account_masked_id=acct.get("masked_id", ""),
                   total_value=dec(d.get("total_value")), equity_value=dec(d.get("equity_value")),
                   cash=dec(d.get("cash")), buying_power=dec(d.get("buying_power")),
                   unleveraged_buying_power=dec(d.get("unleveraged_buying_power")),
                   options_value=dec(d.get("options_value")), crypto_value=dec(d.get("crypto_value")),
                   futures_value=dec(d.get("futures_value")),
                   event_contracts_value=dec(d.get("event_contracts_value")),
                   mutual_funds_value=dec(d.get("mutual_funds_value")),
                   fixed_income_value=dec(d.get("fixed_income_value")), currency=d.get("currency"),
                   fetched_at=fetched_at)


@dataclass
class TaxLot:
    lot_id: Optional[str]
    symbol: str
    open_type: Optional[str]
    order_id: Optional[str]
    quantity: Optional[Decimal]
    quantity_available: Optional[Decimal]
    selectable: Optional[bool]
    cost_per_share: Optional[Decimal]
    cost_basis: Optional[Decimal]
    open_date: Optional[date]
    term: Optional[str]
    basis_pending: bool
    days_held: Optional[int]
    days_to_long_term: Optional[int]
    market_value: Optional[Decimal]
    unrealized_pnl: Optional[Decimal]


@dataclass(frozen=True)
class OrderSummary:
    order_id: Optional[str]
    symbol: Optional[str]
    side: Optional[str]
    order_type: Optional[str]
    trigger: Optional[str]
    state: Optional[str]
    quantity: Optional[Decimal]
    cumulative_quantity: Optional[Decimal]
    limit_price: Optional[Decimal]
    stop_price: Optional[Decimal]
    average_price: Optional[Decimal]
    fees: Optional[Decimal]
    dollar_amount: Optional[Decimal]
    time_in_force: Optional[str]
    market_hours: Optional[str]
    placed_agent: Optional[str]
    created_at: Optional[datetime]
    last_transaction_at: Optional[datetime]

    @classmethod
    def from_gateway(cls, d: dict) -> "OrderSummary":
        return cls(order_id=d.get("order_id"), symbol=d.get("symbol"), side=d.get("side"),
                   order_type=d.get("order_type"), trigger=d.get("trigger"), state=d.get("state"),
                   quantity=dec(d.get("quantity")), cumulative_quantity=dec(d.get("cumulative_quantity")),
                   limit_price=dec(d.get("limit_price")), stop_price=dec(d.get("stop_price")),
                   average_price=dec(d.get("average_price")), fees=dec(d.get("fees")),
                   dollar_amount=dec(d.get("dollar_amount")), time_in_force=d.get("time_in_force"),
                   market_hours=d.get("market_hours"), placed_agent=d.get("placed_agent"),
                   created_at=ts(d.get("created_at")), last_transaction_at=ts(d.get("last_transaction_at")))


@dataclass(frozen=True)
class PnlTrade:
    timestamp: Optional[datetime]
    symbol: Optional[str]
    side: Optional[str]
    quantity: Optional[Decimal]
    price: Optional[Decimal]
    realized_gain: Optional[Decimal]

    @classmethod
    def from_gateway(cls, d: dict) -> "PnlTrade":
        return cls(timestamp=ts(d.get("timestamp")), symbol=d.get("symbol"), side=d.get("side"),
                   quantity=dec(d.get("quantity")), price=dec(d.get("price")),
                   realized_gain=dec(d.get("realized_gain")))


@dataclass
class RealizedPnlSummary:
    span: Optional[str]
    display_currency: Optional[str]
    total_returns: Optional[Decimal]
    total_rate_of_return: Optional[Decimal]      # fraction, e.g. 0.0199 = 1.99%
    buckets_with_trades: int
    closing_trades: int
    buckets: List[dict] = field(default_factory=list)   # null gains stay null ("n/a"), never 0
    trades: List[PnlTrade] = field(default_factory=list)

    @classmethod
    def from_gateway(cls, d: dict, trades: Optional[List[dict]] = None) -> "RealizedPnlSummary":
        buckets = [{"start_time": b.get("start_time"), "end_time": b.get("end_time"),
                    "realized_gain": dec(b.get("realized_gain")),
                    "rate_of_realized_gain": dec(b.get("rate_of_realized_gain")),
                    "number_of_trades": b.get("number_of_trades")} for b in d.get("buckets", [])]
        active = [b for b in buckets if (b["number_of_trades"] or 0) > 0]
        return cls(span=d.get("span") or d.get("window"), display_currency=d.get("display_currency"),
                   total_returns=dec(d.get("total_returns")), total_rate_of_return=dec(d.get("total_rate_of_return")),
                   buckets_with_trades=len(active), closing_trades=sum(b["number_of_trades"] or 0 for b in buckets),
                   buckets=active, trades=[PnlTrade.from_gateway(t) for t in (trades or [])])


def to_jsonable(obj: Any) -> Any:
    """Decimal -> str (exact), datetime/date -> ISO string, dataclasses -> dicts."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
