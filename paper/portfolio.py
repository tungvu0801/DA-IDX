"""
paper/portfolio.py — Stage 4.5 the PAPER PORTFOLIO view: account, cash, positions (FIFO lots), marks, equity, pending
orders and the immutable trade history. SIMULATED — never combined with the real brokerage portfolio.

Marks are the latest COMPLETED daily close (the same completed-session rule and read-only bar source as Strategy Fit);
a position without a close for that session is shown unmarked (equity is then incomplete, never guessed). No intraday
quote, no broker, no AI. Reading the view writes nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from backtest import bars as B
from fit import readonly as RO
from paper import accounting as A
from paper import execution as X
from paper.store import PaperStore

LABEL = "PAPER · SIMULATED · NO REAL ORDERS"
NOTE = ("A local simulation: virtual USD cash, whole shares, long only, market orders filled at the next session's open "
        "from daily bars. No order is ever sent to a broker, and nothing here is combined with a real account.")
FIFO_TEXT = "Sells close the oldest open lots first (FIFO)."
MARK_TEXT = "Positions are marked at the latest completed daily close — not an intraday quote."


def _money(x) -> Optional[str]:
    return A.s(x) if x is not None else None


def setup_info() -> dict:
    return {"limits": {k: [A.s(v[0]), A.s(v[1])] if k != "quantity" else list(v) for k, v in X.LIMITS.items()},
            "defaults": X.DEFAULTS, "zero_cost_text": X.ZERO_COST_TEXT,
            "starting_cash_note": "Enter the virtual starting cash yourself — there is no default. It is fixed after the first fill."}


def view(*, path: Optional[Path] = None, now: Optional[datetime] = None, fetch_fn=None, client=None, cache=None,
         history: int = 200) -> dict:
    path = Path(path) if path else RO.db_path()
    store = PaperStore(path)
    acct = store.account()
    base = {"label": LABEL, "note": NOTE, "fifo_text": FIFO_TEXT, "mark_text": MARK_TEXT, "setup": setup_info()}
    if acct is None:
        return {**base, "account": None}
    fills, lots, closures = store.ledger(acct["account_id"])
    held = sorted({p["symbol"] for p in A.positions(lots, closures, {}) if p["shares"]})
    marks, mark_session, mark_note = {}, None, None
    if held:
        now = X._now(now)
        last_complete = B.last_complete_session_date(now)
        try:
            series, _ = X._bars(held, last_complete - timedelta(days=X.CALENDAR_DAYS), last_complete, now, path, fetch_fn, client, cache)
            cal = X._calendar(series, last_complete)
        except Exception as exc:  # noqa: BLE001 - no data: positions are shown unmarked
            series, cal = {}, {"T": None, "error": f"Market data unavailable ({type(exc).__name__})."}
        T = cal.get("T")
        mark_session = T.isoformat() if T else None
        mark_note = None if T else cal.get("error")
        for sym in held:
            ser = series.get(sym)
            marks[sym] = A.price(ser.bar(T).close) if (T and ser is not None and ser.has(T)) else None
    pos = A.positions(lots, closures, marks)
    summ = A.summary(acct["starting_cash"], fills, pos)
    orders = store.orders(acct["account_id"], limit=1000)
    frozen = bool(fills)
    return {**base,
            "account": {**{k: acct[k] for k in ("account_id", "name", "base_currency", "starting_cash", "slippage_bps",
                                                "commission_per_order", "created_at")},
                        "settings_frozen": frozen, "zero_cost": A.D(acct["slippage_bps"]) == 0 and A.D(acct["commission_per_order"]) == 0},
            "summary": {**{k: _money(summ[k]) for k in ("cash", "market_value", "equity", "realized_pnl", "unrealized_pnl",
                                                         "cost_basis")},
                        "starting_cash": acct["starting_cash"], "open_positions": summ["open_positions"],
                        "unmarked_symbols": summ["unmarked_symbols"], "mark_session": mark_session, "mark_note": mark_note},
            "positions": [{**{k: p[k] for k in ("symbol", "shares", "first_entry_session")},
                           **{k: _money(p[k]) for k in ("cost_basis", "average_cost", "mark", "market_value", "unrealized_pnl",
                                                        "realized_pnl")},
                           "latest_transaction_session": max((f["fill_session"] for f in fills if f["symbol"] == p["symbol"]), default=None),
                           "lots": [{**{k: lot[k] for k in ("lot_id", "entry_session", "quantity", "remaining")},
                                     "cost_per_share": A.s(lot["cost_per_share"]), "remaining_basis": A.s(lot["remaining_basis"])}
                                    for lot in p["lots"]]}
                          for p in pos if p["shares"] or p["realized_pnl"] != A.ZERO],
            "pending_orders": [o for o in orders if o["status"] == "PENDING"],
            "closed_orders": [o for o in orders if o["status"] in ("CANCELLED", "REJECTED")][:50],
            "fills": [{k: v for k, v in f.items() if k != "seq"} for f in reversed(fills)][:history],
            "counts": {"fills": len(fills), "orders": len(orders), "pending": sum(1 for o in orders if o["status"] == "PENDING")}}
