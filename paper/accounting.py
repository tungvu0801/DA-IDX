"""
paper/accounting.py — Stage 4.5 paper-portfolio ACCOUNTING: pure, deterministic, Decimal-only (no database, no web).

Precision (the repository's Stage 3.2 engine uses floats; a ledger needs exact cents, so the paper ledger uses Decimal):
  * prices (open, effective fill, marks, average cost)   4 decimal places, ROUND_HALF_UP
  * money (notional, commission, cash, cost basis, P&L)  2 decimal places (cents), ROUND_HALF_UP
  * slippage in basis points                             2 decimal places
Bars arrive as floats; they are converted through their shortest decimal repr, then rounded to 4 dp.

Costs — the Stage 3.2 execution contract, per fill:
  BUY  effective = open x (1 + bps / 10000); cash -= notional + commission; lot cost = notional + commission
  SELL effective = open x (1 - bps / 10000); cash += notional - commission (net proceeds)
FIFO: a SELL closes the oldest open lots first (entry session, then fill order). A lot's cost basis is allocated in
cents; the closure that empties a lot takes exactly what remains, so a lot's closures always add up to its total cost.
The sell's net proceeds are allocated across its closures the same way (the last closure takes the remainder).
Realized P&L = allocated proceeds - allocated cost basis. Unrealized P&L = shares x mark - remaining cost basis.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional

PRICE, CENT, BPS = Decimal("0.0001"), Decimal("0.01"), Decimal("0.01")
ZERO = Decimal("0.00")


def D(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    return Decimal(str(x))


def money(x) -> Decimal:
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def price(x) -> Decimal:
    return D(x).quantize(PRICE, rounding=ROUND_HALF_UP)


def bps(x) -> Decimal:
    return D(x).quantize(BPS, rounding=ROUND_HALF_UP)


def s(x: Optional[Decimal]) -> Optional[str]:
    """The canonical string stored and returned (never a float)."""
    return None if x is None else format(x, "f")


def effective_price(base, slippage_bps, side: str) -> Decimal:
    f = D(slippage_bps) / Decimal(10000)
    return price(price(base) * (Decimal(1) + f if side == "BUY" else Decimal(1) - f))


def buy_cost(eff: Decimal, qty: int, commission) -> dict:
    notional = money(eff * qty)
    comm = money(commission)
    return {"notional": notional, "commission": comm, "total": money(notional + comm)}


def sell_proceeds(eff: Decimal, qty: int, commission) -> dict:
    notional = money(eff * qty)
    comm = money(commission)
    return {"notional": notional, "commission": comm, "net": money(notional - comm)}


def open_lots(lots: List[dict], closures: List[dict], symbol: Optional[str] = None) -> List[dict]:
    """Lots with remaining shares, FIFO order (entry session, then creation order), each with its remaining basis."""
    closed: Dict[str, dict] = {}
    for c in closures:
        x = closed.setdefault(c["lot_id"], {"qty": 0, "basis": ZERO})
        x["qty"] += int(c["quantity"])
        x["basis"] = money(x["basis"] + D(c["cost_basis"]))
    out = []
    for lot in sorted(lots, key=lambda r: (r["entry_session"], r.get("seq", 0), r["lot_id"])):
        if symbol is not None and lot["symbol"] != symbol:
            continue
        c = closed.get(lot["lot_id"], {"qty": 0, "basis": ZERO})
        remaining = int(lot["quantity"]) - c["qty"]
        if remaining > 0:
            out.append({**lot, "remaining": remaining, "remaining_basis": money(D(lot["total_cost"]) - c["basis"]),
                        "closed_basis": c["basis"]})
    return out


def fifo_close(open_fifo: List[dict], qty: int, net_proceeds: Decimal) -> List[dict]:
    """Closures for selling `qty` shares against FIFO-ordered open lots (raises if not enough shares)."""
    if qty <= 0 or sum(lot["remaining"] for lot in open_fifo) < qty:
        raise ValueError("not enough paper shares for this sell")
    left, out = qty, []
    for lot in open_fifo:
        if left == 0:
            break
        take = min(left, lot["remaining"])
        basis = lot["remaining_basis"] if take == lot["remaining"] else money(D(lot["total_cost"]) * take / int(lot["quantity"]))
        out.append({"lot_id": lot["lot_id"], "entry_session": lot["entry_session"], "quantity": take, "cost_basis": basis})
        left -= take
    allocated = ZERO
    for i, c in enumerate(out):
        share = money(net_proceeds) - allocated if i == len(out) - 1 else money(net_proceeds * c["quantity"] / qty)
        allocated = money(allocated + share)
        c["proceeds"] = share
        c["realized_pnl"] = money(share - c["cost_basis"])
    return out


def cash(starting_cash, fills: List[dict]) -> Decimal:
    return money(D(starting_cash) + sum((D(f["cash_delta"]) for f in fills), ZERO))


def positions(lots: List[dict], closures: List[dict], marks: Dict[str, Optional[Decimal]]) -> List[dict]:
    """Per symbol (alphabetical): shares, remaining cost basis, average cost, mark, market value, unrealized and realized
    P&L, first entry and latest transaction sessions, FIFO lots. marks[symbol] = latest completed close or None."""
    symbols = sorted({x["symbol"] for x in lots})
    realized: Dict[str, Decimal] = {}
    for c in closures:
        realized[c["symbol"]] = money(realized.get(c["symbol"], ZERO) + D(c["realized_pnl"]))
    out = []
    for sym in symbols:
        fifo = open_lots(lots, closures, sym)
        shares = sum(lot["remaining"] for lot in fifo)
        basis = money(sum((lot["remaining_basis"] for lot in fifo), ZERO))
        mark = marks.get(sym)
        mv = money(mark * shares) if (mark is not None and shares) else (ZERO if not shares else None)
        mine = [x for x in lots if x["symbol"] == sym]
        out.append({"symbol": sym, "shares": shares, "cost_basis": basis,
                    "average_cost": price(basis / shares) if shares else None, "mark": mark, "market_value": mv,
                    "unrealized_pnl": money(mv - basis) if (mv is not None and shares) else (ZERO if not shares else None),
                    "realized_pnl": realized.get(sym, ZERO), "first_entry_session": min(x["entry_session"] for x in mine),
                    "lots": [{"lot_id": lot["lot_id"], "entry_session": lot["entry_session"], "quantity": int(lot["quantity"]),
                              "remaining": lot["remaining"], "cost_per_share": price(D(lot["total_cost"]) / int(lot["quantity"])),
                              "remaining_basis": lot["remaining_basis"]} for lot in fifo]})
    return out


def summary(starting_cash, fills: List[dict], pos: List[dict]) -> dict:
    c = cash(starting_cash, fills)
    held = [p for p in pos if p["shares"]]
    unmarked = [p["symbol"] for p in held if p["market_value"] is None]
    mv = None if unmarked else money(sum((p["market_value"] for p in held), ZERO))
    realized = money(sum((p["realized_pnl"] for p in pos), ZERO))
    unrealized = None if unmarked else money(sum((p["unrealized_pnl"] for p in held), ZERO))
    return {"cash": c, "market_value": mv, "equity": money(c + mv) if mv is not None else None, "realized_pnl": realized,
            "unrealized_pnl": unrealized, "unmarked_symbols": unmarked, "open_positions": len(held),
            "cost_basis": money(sum((p["cost_basis"] for p in held), ZERO))}
