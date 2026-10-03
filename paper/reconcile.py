"""
paper/reconcile.py — Stage 4.6A observational comparison: LOCAL SIMULATOR (Stage 4.5) vs ALPACA PAPER (read only).

Pure functions over two independent read-only views. The output DESCRIBES differences; it never fixes, syncs, imports or
links anything, and it has no score, verdict or confidence.

  positions   union of symbols, alphabetical: MATCH / DIFFERENT / LOCAL_ONLY / ALPACA_ONLY by share quantity only.
              share_delta = Alpaca paper shares − local simulated shares (negative: Alpaca holds fewer).
              Average costs come from each account's own fills and are shown side by side, never compared.
  values      local marks are the latest COMPLETED daily close; Alpaca's current price / market value / unrealized P&L
              are as of the broker refresh. Different timestamps: NOT_DIRECTLY_COMPARABLE, no P&L comparison at all.
  balances    INDEPENDENT accounts (different starting balances and histories); a difference is not an error.
  orders      NOT_LINKED — a local simulator order carries no Alpaca order id, and orders are never paired by symbol,
  fills       quantity, time or price (a coincidence is not the same user intent). Exact linkage is a later stage.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

MATCH, DIFFERENT, LOCAL_ONLY, ALPACA_ONLY = "MATCH", "DIFFERENT", "LOCAL_ONLY", "ALPACA_ONLY"
NOT_LINKED = "NOT_LINKED"
NOT_DIRECTLY_COMPARABLE = "NOT_DIRECTLY_COMPARABLE"
INDEPENDENT = "INDEPENDENT"
DELTA_CONVENTION = "Share delta = Alpaca paper shares − local simulated shares (negative: Alpaca paper holds fewer)."
BALANCE_NOTE = ("These are independent paper accounts and may have different starting balances and transaction histories. "
                "A difference is expected and is not an error.")
DIFF_NOTE = "Differences are descriptive only (Alpaca paper − local simulated)."
TIMING_NOTE = ("Local values use the latest completed daily close; Alpaca paper values are as of the broker refresh. "
               "They are different timestamps, so market value and unrealized P&L are not directly comparable.")
COST_NOTE = "Average costs come from each account's own fills (the local one includes simulated slippage and commission)."
LINK_NOTE = ("Local simulator orders and fills carry no Alpaca order id, so they are shown separately. Stage 4.6A never pairs "
             "them by symbol, quantity, time or price.")
UNAVAILABLE = "Alpaca paper {} could not be read in this refresh — no comparison is shown for it."


def _d(x) -> Optional[Decimal]:
    return None if x is None else Decimal(str(x))


def _s(x: Optional[Decimal]) -> Optional[str]:
    """A share quantity as plain text: whole shares without decimals ("10", "-2"), fractions as given ("8.5")."""
    if x is None:
        return None
    return str(int(x)) if x == x.to_integral_value() else format(x.normalize(), "f")


def _money(x: Optional[Decimal]) -> Optional[str]:
    return None if x is None else str(x.quantize(Decimal("0.01")))


def positions(local_positions, alpaca_positions, *, local_mark_session=None, alpaca_as_of=None) -> dict:
    """local_positions: the Stage 4.5 view's positions (only open ones count); alpaca_positions: normalized, or None when
    the broker read failed (then nothing is categorised — an unread account is never reported as LOCAL_ONLY)."""
    if alpaca_positions is None:
        return {"available": False, "reason": UNAVAILABLE.format("positions"), "rows": [], "summary": None}
    local = {p["symbol"]: p for p in (local_positions or []) if p.get("shares")}
    broker = {p["symbol"]: p for p in alpaca_positions}
    rows = []
    for sym in sorted(set(local) | set(broker)):
        lp, bp = local.get(sym), broker.get(sym)
        lq, bq = (_d(lp["shares"]) if lp else None), (_d(bp["qty"]) if bp else None)
        status = (ALPACA_ONLY if lp is None else LOCAL_ONLY if bp is None else MATCH if lq == bq else DIFFERENT)
        rows.append({"symbol": sym, "status": status,
                     "local_shares": _s(lq), "alpaca_shares": _s(bq),
                     "share_delta": _s((bq or Decimal(0)) - (lq or Decimal(0))),
                     "local_average_cost": lp.get("average_cost") if lp else None,
                     "alpaca_avg_entry_price": bp.get("avg_entry_price") if bp else None,
                     "local_mark": lp.get("mark") if lp else None, "local_market_value": lp.get("market_value") if lp else None,
                     "local_unrealized_pnl": lp.get("unrealized_pnl") if lp else None,
                     "alpaca_current_price": bp.get("current_price") if bp else None,
                     "alpaca_market_value": bp.get("market_value") if bp else None,
                     "alpaca_unrealized_pl": bp.get("unrealized_pl") if bp else None,
                     "value_comparison": NOT_DIRECTLY_COMPARABLE if (lp and bp) else None})
    count = lambda k: sum(1 for r in rows if r["status"] == k)  # noqa: E731
    return {"available": True, "rows": rows, "delta_convention": DELTA_CONVENTION, "cost_note": COST_NOTE,
            "values": {"status": NOT_DIRECTLY_COMPARABLE, "note": TIMING_NOTE,
                       "local_basis": {"kind": "COMPLETED_DAILY_CLOSE", "session": local_mark_session},
                       "alpaca_basis": {"kind": "BROKER_REFRESH", "as_of": alpaca_as_of}},
            "summary": {"local_positions": len(local), "alpaca_positions": len(broker), "quantity_matches": count(MATCH),
                        "quantity_differences": count(DIFFERENT), "local_only": count(LOCAL_ONLY), "alpaca_only": count(ALPACA_ONLY)}}


def account(local_view: dict, alpaca_account: Optional[dict]) -> dict:
    acct = (local_view or {}).get("account")
    summ = (local_view or {}).get("summary") or {}
    local = ({"exists": True, "name": acct.get("name"), "starting_cash": acct.get("starting_cash"), "cash": summ.get("cash"),
              "equity": summ.get("equity"), "mark_session": summ.get("mark_session"),
              "equity_complete": summ.get("equity") is not None and not summ.get("unmarked_symbols")}
             if acct else {"exists": False})
    if alpaca_account is None:
        return {"relationship": INDEPENDENT, "note": BALANCE_NOTE, "local": local, "alpaca": None,
                "available": False, "reason": UNAVAILABLE.format("account")}
    alpaca = {k: alpaca_account.get(k) for k in ("cash", "equity", "portfolio_value", "buying_power", "status", "currency")}
    diff = lambda a, b: _money(_d(a) - _d(b)) if (a is not None and b is not None) else None  # noqa: E731
    return {"relationship": INDEPENDENT, "note": BALANCE_NOTE, "local": local, "alpaca": alpaca, "available": True,
            "differences": {"cash": diff(alpaca["cash"], local.get("cash")), "equity": diff(alpaca["equity"], local.get("equity")),
                            "note": DIFF_NOTE}}


def links(local_view: dict, alpaca_orders, alpaca_fills) -> dict:
    counts = (local_view or {}).get("counts") or {}

    def one(local_n, broker):
        return {"status": NOT_LINKED, "linked": 0, "local_count": local_n,
                "alpaca_count": None if broker is None else len(broker)}
    return {"orders": one(counts.get("orders", 0), alpaca_orders), "fills": one(counts.get("fills", 0), alpaca_fills),
            "note": LINK_NOTE}


def reconcile(local_view: dict, broker: dict, *, alpaca_as_of: Optional[str] = None) -> dict:
    """broker: {"account", "positions", "orders", "fills"} normalized, each None when its read failed."""
    pos = positions((local_view or {}).get("positions"), broker.get("positions"),
                    local_mark_session=((local_view or {}).get("summary") or {}).get("mark_session"), alpaca_as_of=alpaca_as_of)
    lk = links(local_view, broker.get("orders"), broker.get("fills"))
    summary = dict(pos["summary"]) if pos["summary"] else None
    if summary is not None:
        summary.update(linked_orders=0, linked_fills=0)
    return {"account": account(local_view, broker.get("account")), "positions": pos, "links": lk, "summary": summary}
