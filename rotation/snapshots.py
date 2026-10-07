"""
rotation/snapshots.py — Stage 4.7 current-portfolio SNAPSHOT adapters (DESIGN_47_PORTFOLIO_ROTATION §12).

Every source is normalised into ONE PortfolioSnapshot: source, snapshot_at, cash (Decimal), positions (symbol, Decimal
quantity — fractional preserved, duplicates summed, quantity <= 0 dropped, symbols upper-cased and validated) and
`source_meta`, which is INFORMATIONAL ONLY: it never reaches the engine's deterministic inputs (reference equity, weights,
turnover, input_hash, proposal_hash depend on `deterministic_inputs()` alone).

  ALPACA_PAPER_VIEW    the last Stage 4.6A refresh from memory (paper.alpaca_view.view(), read only) — 0 broker calls here
  ROBINHOOD_READ_ONLY  the existing read-only gateway path api.routes.portfolio.provider_factory(): an EXPLICIT loader that
                       calls exactly get_portfolio() and get_positions() once each — never the order history, quotes, tax
                       lots or any write — and a rotation run never calls it (the engine receives an already-loaded snapshot)
  LOCAL_SIMULATOR      the Stage 4.5 simulator ledger (paper.store / paper.accounting), 0 network

Failure vocabulary: SnapshotError("INPUT_ERROR") for a missing / incomplete / invalid snapshot,
SnapshotError("PORTFOLIO_UNAVAILABLE") when the Robinhood path is disabled or unavailable. `freshness()` turns a stale
or missing snapshot into the run statuses DATA_STALE / INPUT_ERROR (fail closed; nothing is refreshed silently).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from paper import accounting as A
from paper import alpaca_order_rules as RU                      # SYMBOL_RE only (pure constants of the frozen module)
from paper.store import PaperStore
from rotation.factors import to_decimal

ALPACA_PAPER_VIEW, ROBINHOOD_READ_ONLY, LOCAL_SIMULATOR = "ALPACA_PAPER_VIEW", "ROBINHOOD_READ_ONLY", "LOCAL_SIMULATOR"
SOURCES = (ALPACA_PAPER_VIEW, ROBINHOOD_READ_ONLY, LOCAL_SIMULATOR)
OK, STALE = "OK", "STALE"
RH_SHARES_HELD = "RH_SHARES_HELD"
_HELD_PARTS = ("shares_held_for_sells", "shares_held_for_stock_grants", "shares_held_for_options_events",
               "shares_held_for_asset_transfer", "shares_pending_from_options_events")


class SnapshotError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class PortfolioSnapshot:
    source: str
    snapshot_at: Optional[str]
    cash: Decimal
    positions: Tuple[Tuple[str, Decimal], ...]                  # (symbol, quantity), symbol-sorted, quantity > 0
    source_meta: dict = field(default_factory=dict)             # informational only — never a deterministic input
    status: Optional[str] = OK                                  # OK | STALE as reported by the source

    def quantities(self) -> Dict[str, Decimal]:
        return dict(self.positions)

    def symbols(self) -> List[str]:
        return [s for s, _ in self.positions]

    def deterministic_inputs(self) -> dict:
        """The ONLY part of a snapshot the engine and the hashes may depend on."""
        return {"cash": format(self.cash, "f"), "positions": [{"symbol": s, "quantity": format(q, "f")} for s, q in self.positions]}

    def flags(self, symbol: str) -> List[str]:
        """Display flags recorded in source_meta for a symbol (e.g. RH_SHARES_HELD) — never used in a calculation."""
        return list((self.source_meta.get("positions") or {}).get(symbol, {}).get("flags") or [])


def normalise(source: str, snapshot_at: Optional[str], cash, positions: Iterable[Tuple[str, object]], source_meta: Optional[dict],
              status: Optional[str] = OK) -> PortfolioSnapshot:
    if source not in SOURCES:
        raise SnapshotError("INPUT_ERROR", f"unknown portfolio source {source!r}")
    c = to_decimal(cash)
    if c is None or c < 0:
        raise SnapshotError("INPUT_ERROR", "the snapshot cash is missing, not a finite number or negative")
    c = c.quantize(Decimal("0.01"))                              # money: one canonical representation across sources
    merged: Dict[str, Decimal] = {}
    for raw_symbol, raw_qty in positions:
        sym = str(raw_symbol or "").strip().upper()
        if not RU.SYMBOL_RE.match(sym):
            raise SnapshotError("INPUT_ERROR", f"invalid position symbol {raw_symbol!r} in the portfolio snapshot")
        qty = to_decimal(raw_qty)
        if qty is None:
            raise SnapshotError("INPUT_ERROR", f"invalid quantity for {sym} in the portfolio snapshot")
        if qty <= 0:
            continue
        merged[sym] = (merged.get(sym, Decimal(0)) + qty).normalize()   # "10.000000" and "10" are the same quantity
    if status not in (OK, STALE, None):
        raise SnapshotError("INPUT_ERROR", f"unknown snapshot status {status!r}")
    return PortfolioSnapshot(source=source, snapshot_at=snapshot_at, cash=c, positions=tuple(sorted(merged.items())),
                             source_meta=dict(source_meta or {}), status=status)


# ---- ALPACA_PAPER_VIEW (Stage 4.6A, read only; 0 broker calls here) -------------------------------------------------------------

def from_alpaca_view(view: Optional[dict]) -> PortfolioSnapshot:
    """The last Stage 4.6A refresh as a snapshot. No refresh, no account → INPUT_ERROR; a non-long or negative position →
    INPUT_ERROR; failed account / positions sections → INPUT_ERROR (incomplete)."""
    if not view or not view.get("account") or not view.get("refreshed_at"):
        raise SnapshotError("INPUT_ERROR", "No Alpaca paper snapshot: refresh the Alpaca Paper — Read Only view first.")
    sections = view.get("sections") or {}
    for name in ("account", "positions"):
        if sections and (sections.get(name) or {}).get("status") not in (None, "OK"):
            raise SnapshotError("INPUT_ERROR", f"The last Alpaca paper refresh did not read the {name} section.")
    acct = view["account"]
    rows = []
    for p in view.get("positions") or []:
        side = str(p.get("side") or "long").lower()
        qty = to_decimal(p.get("qty"))
        if side != "long" or qty is None or qty < 0:
            raise SnapshotError("INPUT_ERROR", f"Alpaca paper position {p.get('symbol')} is not a long position.")
        rows.append((p.get("symbol"), qty))
    meta = {"account_number_masked": acct.get("account_number_masked"), "status": acct.get("status"),
            "equity": acct.get("equity"), "portfolio_value": acct.get("portfolio_value"), "buying_power": acct.get("buying_power"),
            "last_result": view.get("last_result"), "refreshed_at": view.get("refreshed_at"),
            "positions": {str(p.get("symbol") or "").upper(): {"market_value": p.get("market_value"), "flags": []}
                          for p in view.get("positions") or []}}
    return normalise(ALPACA_PAPER_VIEW, view.get("refreshed_at"), acct.get("cash"), rows, meta, OK)


def load_alpaca_snapshot(view_fn: Optional[Callable[[], dict]] = None) -> PortfolioSnapshot:
    if view_fn is None:
        from paper import alpaca_view as APV                    # frozen Stage 4.6A module, read only: view() makes 0 requests
        view_fn = APV.view
    return from_alpaca_view(view_fn())


# ---- ROBINHOOD_READ_ONLY (existing read-only gateway path; explicit loader) --------------------------------------------------------

def _portfolio_route():
    from api.routes import portfolio as pr                      # the approved extension point: provider_factory (+ the error type)
    return pr


def _earliest(*values: Optional[str]) -> Optional[str]:
    present = [v for v in values if v]
    return min(present) if present else None


def from_robinhood_results(portfolio_result, positions_result) -> PortfolioSnapshot:
    """Normalise the two gateway results. Either `truncated` → INPUT_ERROR; either STALE → snapshot status STALE."""
    for name, r in (("portfolio", portfolio_result), ("positions", positions_result)):
        if r is None or r.data is None:
            raise SnapshotError("INPUT_ERROR", f"The gateway returned no {name} data.")
        if getattr(r, "truncated", False):
            raise SnapshotError("INPUT_ERROR", f"The gateway {name} data is truncated; an incomplete portfolio is never rotated.")
    pdata, qdata = portfolio_result.data, positions_result.data
    rows, meta_positions = [], {}
    for p in qdata.get("positions") or []:
        sym = str(p.get("symbol") or "").upper()
        if str(p.get("type") or "long").lower() != "long":
            raise SnapshotError("INPUT_ERROR", f"Robinhood position {sym} is not a long position.")
        qty, sellable = to_decimal(p.get("quantity")), to_decimal(p.get("shares_available_for_sells"))
        parts = [to_decimal(p.get(k)) for k in _HELD_PARTS]
        held = None if any(x is None for x in parts) else sum(parts, Decimal(0))
        flags = [RH_SHARES_HELD] if ((held is not None and held > 0) or (qty is not None and sellable is not None and sellable < qty)) else []
        rows.append((sym, qty))
        meta_positions[sym] = {"average_buy_price": p.get("average_buy_price"), "sellable_quantity": None if sellable is None else format(sellable, "f"),
                               "held_quantity": None if held is None else format(held, "f"), "flags": flags}
    status = STALE if STALE in (portfolio_result.status, positions_result.status) else OK
    acct = pdata.get("account") or {}
    meta = {"gateway_status": {"portfolio": portfolio_result.status, "positions": positions_result.status},
            "fetched_at": {"portfolio": portfolio_result.fetched_at, "positions": positions_result.fetched_at},
            "cache_age_s": {"portfolio": portfolio_result.cache_age_s, "positions": positions_result.cache_age_s},
            "account_alias": acct.get("alias"), "account_masked_id": acct.get("masked_id"),
            "total_value": pdata.get("total_value"), "equity_value": pdata.get("equity_value"), "buying_power": pdata.get("buying_power"),
            "positions": meta_positions}
    return normalise(ROBINHOOD_READ_ONLY, _earliest(portfolio_result.fetched_at, positions_result.fetched_at), pdata.get("cash"), rows,
                     meta, status)


def load_robinhood_snapshot(provider_fn: Optional[Callable] = None) -> PortfolioSnapshot:
    """The EXPLICIT Robinhood snapshot load: exactly one get_portfolio() and one get_positions() through the existing
    read-only provider. A rotation run never calls this; it receives the result."""
    pr = _portfolio_route()
    provider = (provider_fn or pr.provider_factory)()
    try:
        portfolio_result = provider.get_portfolio()
        positions_result = provider.get_positions()
    except pr.PortfolioUnavailable as exc:
        raise SnapshotError("PORTFOLIO_UNAVAILABLE", exc.message) from None
    return from_robinhood_results(portfolio_result, positions_result)


# ---- LOCAL_SIMULATOR (Stage 4.5, read only; 0 network) ---------------------------------------------------------------------------------

def load_local_simulator_snapshot(path=None, now: Optional[datetime] = None) -> PortfolioSnapshot:
    ps = PaperStore(path)
    acct = ps.account()
    if not acct:
        raise SnapshotError("INPUT_ERROR", "There is no local paper account yet.")
    fills, lots, closures = ps.ledger(acct["account_id"])
    cash = A.cash(acct["starting_cash"], fills)
    qty: Dict[str, int] = {}
    for lot in A.open_lots(lots, closures):
        qty[lot["symbol"]] = qty.get(lot["symbol"], 0) + int(lot["remaining"])
    pending = sorted({o["symbol"] for o in ps.orders(acct["account_id"], status="PENDING")})
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    meta = {"account_id": acct["account_id"], "name": acct.get("name"), "pending_symbols": pending,
            "positions": {s: {"flags": []} for s in qty}}
    return normalise(LOCAL_SIMULATOR, at, cash, [(s, Decimal(q)) for s, q in qty.items()], meta, OK)


# ---- freshness (fail closed) -----------------------------------------------------------------------------------------------------------

def parse_time(v: str) -> datetime:
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def freshness(snapshot: Optional[PortfolioSnapshot], now: datetime, max_age_min: int) -> Optional[Tuple[str, str]]:
    """None when the snapshot may be used; otherwise (run_status, detail): a missing snapshot is INPUT_ERROR, a STALE or
    aged one is DATA_STALE. Nothing is refreshed here."""
    if snapshot is None:
        return "INPUT_ERROR", "No portfolio snapshot was loaded for this source."
    if snapshot.status == STALE:
        return "DATA_STALE", "The portfolio source reported its data as STALE."
    if not snapshot.snapshot_at:
        return "INPUT_ERROR", "The portfolio snapshot has no timestamp."
    try:
        age = (now - parse_time(snapshot.snapshot_at)).total_seconds()
    except ValueError:
        return "INPUT_ERROR", "The portfolio snapshot timestamp is unreadable."
    if age > max_age_min * 60:
        return "DATA_STALE", f"The portfolio snapshot is {int(age // 60)} minutes old (limit {max_age_min})."
    return None
