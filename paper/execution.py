"""
paper/execution.py — Stage 4.5 paper ORDERS and the deterministic FILL ENGINE (no broker, no real order, no AI).

Timing — the Stage 3.2 contract (decision at a completed close, fill at the next session's open):
  * decision_session  = the latest COMPLETED session when the order is submitted (backtest.bars.last_complete_session_date
                        + the SPY market calendar via fit.current.resolve_session — the same rule as Strategy Fit / 3.3).
  * earliest_fill_session = the first weekday whose 09:30 New York open is AFTER the submission time, so an order placed
                        during a session can never fill at that session's already-known open. Holidays are unknown in
                        advance: the fill session is the first SPY session on or after that date.
  * A pending order fills only once its fill session has COMPLETED under the same rule (its daily bar is final). Until
    then it stays PENDING ("NOT_YET_AVAILABLE"); if that session's open is missing for the stock it stays PENDING
    ("NEXT_OPEN_UNAVAILABLE") — a price is never invented and a quote is never used.

Fill: base = the stock's daily open of the fill session (the Stage 3.2 daily-bar source / cache, read-only through
fit.current.load_bars, adjustment "all"), effective = base x (1 +/- slippage bps / 10000), whole shares, fixed commission
per order (paper/accounting.py). Cash-only and long-only: a BUY whose actual cost exceeds cash, or a SELL beyond the
shares held, is REJECTED at fill (no fill, no lot, cash never negative). At each open SELLs are processed before BUYs
(Stage 3.2 order), then by submission time.

At most once: each fill is ONE transaction (BEGIN IMMEDIATE) that re-reads the order as PENDING, writes the fill, its lot
or FIFO lot closures and the order's FILLED status; UNIQUE(order_id) on fills and the database triggers make a second fill
impossible — a restart, a second click or a second server never duplicates anything.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from backtest import bars as B
from backtest.snapshots import SPY
from fit import current as FC
from fit import readonly as RO
from paper import accounting as A
from paper.store import PaperStore

ENGINE_VERSION = "4.5.0"
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
LIMITS = {"starting_cash": (A.D("1000"), A.D("100000000")), "slippage_bps": (A.D("0"), A.D("500")),
          "commission_per_order": (A.D("0"), A.D("1000")), "quantity": (1, 1_000_000)}        # Stage 3.2's cost limits
DEFAULTS = {"slippage_bps": "0", "commission_per_order": "0"}                                   # Stage 3.2's cost defaults
ZERO_COST_TEXT = "Zero slippage and commission is an optimistic, cost-free assumption."          # Stage 3.2's label
CALENDAR_DAYS = 45
_lock = threading.Lock()


class PaperError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


def _now(now: Optional[datetime] = None) -> datetime:
    return FC._utc(now)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def earliest_fill_session(decision_session: date, submitted_at: datetime) -> date:
    """The first weekday after the decision session whose 09:30 New York open is after the submission time."""
    d = decision_session + timedelta(days=1)
    while d.weekday() >= 5 or datetime.combine(d, time(9, 30), tzinfo=B.NY) <= submitted_at:
        d += timedelta(days=1)
    return d


def _bars(symbols, start: date, last_complete: date, now: datetime, path: Path, fetch_fn=None, client=None, cache=None):
    """Daily bars through the last completed session — the existing read-only Stage 3.2 source / cache (no DB write)."""
    series, prov, _ = FC.load_bars(RO.ReadOnlyBacktestStore(path), sorted(set(symbols) | {SPY}), start, last_complete, now,
                                   cache if cache is not None else FC.BAR_CACHE, fetch_fn=fetch_fn, client=client)
    return series, prov


def _calendar(series, last_complete: date) -> dict:
    sess = FC.resolve_session(series, last_complete)
    spy = series.get(SPY)
    cal = [d for d in (spy.dates if spy is not None else []) if sess.get("T") and d <= sess["T"]]
    return {**sess, "calendar": cal}


# ================================================================================================================
# account
# ================================================================================================================

def _dec(v, key: str, places) -> "A.Decimal":
    try:
        x = A.D(v)
    except Exception:  # noqa: BLE001
        raise PaperError("INVALID_VALUE", f"{key} must be a number.") from None
    lo, hi = LIMITS[key]
    if not x.is_finite() or x < lo or x > hi or x != places(x):
        raise PaperError("INVALID_VALUE", f"{key} must be between {lo} and {hi} with at most "
                         f"{'2 decimals' if places is A.money else '2 decimals of a basis point'}.")
    return places(x)


def create_account(name: str, starting_cash, slippage_bps, commission_per_order, *, path: Optional[Path] = None,
                   now: Optional[datetime] = None) -> dict:
    store = PaperStore(path)
    if store.account() is not None:
        raise PaperError("ACCOUNT_EXISTS", "A paper account already exists. Stage 4.5 has one local paper account.", 409)
    nm = " ".join(str(name or "Paper account").split())[:60] or "Paper account"
    row = {"account_id": uuid.uuid4().hex, "name": nm, "base_currency": "USD",
           "starting_cash": A.s(_dec(starting_cash, "starting_cash", A.money)),
           "slippage_bps": A.s(_dec(slippage_bps, "slippage_bps", A.bps)),
           "commission_per_order": A.s(_dec(commission_per_order, "commission_per_order", A.money)),
           "status": "ACTIVE", "created_at": _iso(_now(now)), "updated_at": _iso(_now(now))}
    with store.connect() as conn:
        store.insert(conn, "paper_accounts", row)
        conn.commit()
    return row


def update_account(name: Optional[str] = None, starting_cash=None, slippage_bps=None, commission_per_order=None, *,
                   path: Optional[Path] = None, now: Optional[datetime] = None) -> dict:
    store = PaperStore(path)
    acct = store.account()
    if acct is None:
        raise PaperError("NO_ACCOUNT", "Create the paper account first.", 404)
    vals = {}
    if name is not None:
        vals["name"] = " ".join(str(name).split())[:60] or acct["name"]
    money = {k: v for k, v in (("starting_cash", starting_cash), ("slippage_bps", slippage_bps),
                               ("commission_per_order", commission_per_order)) if v is not None}
    if money and store.fills(acct["account_id"], limit=1):
        raise PaperError("ACCOUNT_FROZEN", "Starting cash, slippage and commission are fixed once the paper account has a "
                         "fill. Only the name can change.", 409)
    for k, v in money.items():
        vals[k] = A.s(_dec(v, k, A.bps if k == "slippage_bps" else A.money))
    if vals:
        vals["updated_at"] = _iso(_now(now))
        with store.connect() as conn:
            conn.execute(f"UPDATE paper_accounts SET {', '.join(f'{k} = ?' for k in vals)} WHERE account_id = ?",
                         (*vals.values(), acct["account_id"]))
            conn.commit()
    return store.account()


# ================================================================================================================
# orders
# ================================================================================================================

def preview_order(symbol: str, side: str, quantity: int, origin: str = "MANUAL", strategy_version_id: Optional[str] = None,
                  **kw) -> dict:
    """The same validation and server-defined sessions / estimate as create_order — nothing is written."""
    return create_order(symbol, side, quantity, origin, strategy_version_id, store_it=False, **kw)


def create_order(symbol: str, side: str, quantity: int, origin: str = "MANUAL", strategy_version_id: Optional[str] = None, *,
                 path: Optional[Path] = None, now: Optional[datetime] = None, fetch_fn=None, client=None, cache=None,
                 store_it: bool = True) -> dict:
    """Validate and store ONE pending paper order (explicit user action). Server-defined session and estimate; no fill."""
    path = Path(path) if path else RO.db_path()
    store = PaperStore(path)
    acct = store.account()
    if acct is None:
        raise PaperError("NO_ACCOUNT", "Create the paper account first.", 404)
    sym = str(symbol or "").strip().upper()
    if not SYMBOL_RE.match(sym):
        raise PaperError("INVALID_SYMBOL", "Not a valid US stock ticker.")
    if side not in ("BUY", "SELL"):
        raise PaperError("INVALID_SIDE", "Side must be BUY or SELL (long only).")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not LIMITS["quantity"][0] <= quantity <= LIMITS["quantity"][1]:
        raise PaperError("INVALID_QUANTITY", "Quantity must be a whole number of shares between 1 and 1,000,000.")
    if origin != "MANUAL":
        raise PaperError("INVALID_ORIGIN", "Stage 4.5 paper orders are manual.")
    if strategy_version_id is not None and strategy_version_id not in {v["strategy_version_id"] for v in RO.saved_versions(path, include_old=True)}:
        raise PaperError("UNKNOWN_STRATEGY_VERSION", "That saved strategy version does not exist.", 404)
    now = _now(now)
    last_complete = B.last_complete_session_date(now)
    series, _ = _bars([sym], last_complete - timedelta(days=CALENDAR_DAYS), last_complete, now, path, fetch_fn, client, cache)
    cal = _calendar(series, last_complete)
    if not cal.get("T"):
        raise PaperError("SESSION_UNKNOWN", cal.get("error") or "The latest completed session is unknown.", 409)
    T = cal["T"]
    ser = series.get(sym)
    if ser is None or not ser.has(T):
        raise PaperError("NO_DATA_FOR_SYMBOL", f"No daily bar for {sym} at the {T.isoformat()} close — it cannot be paper "
                         "traded (unknown or unsupported symbol, or no current data).")
    est = A.effective_price(ser.bar(T).close, acct["slippage_bps"], side)
    fills, lots, closures = store.ledger(acct["account_id"])
    pending = store.orders(acct["account_id"], "PENDING")
    if side == "BUY":
        amount = A.buy_cost(est, quantity, acct["commission_per_order"])["total"]
        reserved = A.money(sum((A.D(o["estimate_amount"]) for o in pending if o["side"] == "BUY"), A.ZERO))
        available = A.money(A.cash(acct["starting_cash"], fills) - reserved)
        if amount > available:
            raise PaperError("INSUFFICIENT_CASH", f"Estimated cost {A.s(amount)} exceeds the paper cash available for new "
                             f"orders ({A.s(available)}, after pending buys).", estimate=A.s(amount), available=A.s(available))
    else:
        owned = sum(x["remaining"] for x in A.open_lots(lots, closures, sym))
        pend = sum(o["quantity"] for o in pending if o["side"] == "SELL" and o["symbol"] == sym)
        if quantity > owned - pend:
            raise PaperError("INSUFFICIENT_SHARES", f"You hold {owned} paper share(s) of {sym}"
                             f"{f' and {pend} are already in pending sells' if pend else ''} — long only, no short.",
                             owned=owned, pending_sell=pend)
        amount = A.sell_proceeds(est, quantity, acct["commission_per_order"])["net"]
        if amount < 0:
            amount = A.ZERO
    row = {"order_id": uuid.uuid4().hex, "account_id": acct["account_id"], "symbol": sym, "side": side, "quantity": quantity,
           "order_type": "MARKET", "timing": "NEXT_SESSION_OPEN", "origin": origin, "strategy_version_id": strategy_version_id,
           "decision_session": T.isoformat(), "earliest_fill_session": earliest_fill_session(T, now).isoformat(),
           "submitted_at": _iso(now), "estimate_price": A.s(est), "estimate_amount": A.s(amount), "status": "PENDING",
           "wait_reason": None, "checked_at": None, "reject_reason": None, "resolved_at": None, "fill_session": None,
           "fill_id": None}
    if not store_it:
        return {**row, "order_id": None, "status": "PREVIEW"}
    with store.connect() as conn:
        store.insert(conn, "paper_orders", row)
        conn.commit()
    return row


def cancel_order(order_id: str, *, path: Optional[Path] = None, now: Optional[datetime] = None) -> dict:
    store = PaperStore(path)
    with _lock, store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        o = conn.execute("SELECT * FROM paper_orders WHERE order_id = ?", (order_id,)).fetchone()
        if o is None:
            conn.rollback()
            raise PaperError("NOT_FOUND", "No paper order with that id.", 404)
        if o["status"] == "FILLED":
            conn.rollback()
            raise PaperError("ALREADY_FILLED", "A filled paper order cannot be cancelled or edited.", 409)
        if o["status"] == "REJECTED":
            conn.rollback()
            raise PaperError("ALREADY_REJECTED", "This paper order was rejected; there is nothing to cancel.", 409)
        if o["status"] == "CANCELLED":
            conn.rollback()
            return {**dict(o), "changed": False}
        conn.execute("UPDATE paper_orders SET status = 'CANCELLED', resolved_at = ? WHERE order_id = ? AND status = 'PENDING'",
                     (_iso(_now(now)), order_id))
        conn.commit()
    return {**store.order(order_id), "changed": True}


def process_pending(*, path: Optional[Path] = None, now: Optional[datetime] = None, fetch_fn=None, client=None,
                    cache=None) -> dict:
    """Fill every pending order whose next-session open is now final (explicit user action). Never twice."""
    path = Path(path) if path else RO.db_path()
    store = PaperStore(path)
    acct = store.account()
    out = {"orders_considered": 0, "filled": 0, "still_pending": 0, "rejected": 0, "errors": 0, "orders": [],
           "decision_session": None}
    if acct is None:
        return {**out, "status": "NO_ACCOUNT"}
    with _lock:
        pending = sorted(store.orders(acct["account_id"], "PENDING", limit=100000), key=lambda o: (o["submitted_at"], o["order_id"]))
        out["orders_considered"] = len(pending)
        if not pending:
            return {**out, "status": "NOTHING_PENDING"}
        now = _now(now)
        last_complete = B.last_complete_session_date(now)
        start = min(date.fromisoformat(o["decision_session"]) for o in pending) - timedelta(days=10)
        start = min(start, last_complete - timedelta(days=CALENDAR_DAYS))
        try:
            series, prov = _bars([o["symbol"] for o in pending], start, last_complete, now, path, fetch_fn, client, cache)
            cal = _calendar(series, last_complete)
        except Exception as exc:  # noqa: BLE001 - no data now: everything simply stays pending
            series, prov, cal = {}, {}, {"T": None, "error": f"Market data unavailable ({type(exc).__name__}).", "calendar": []}
        out["decision_session"] = cal["T"].isoformat() if cal.get("T") else None
        plan = []
        for o in pending:
            if not cal.get("T"):
                plan.append((o, None, "DATA_WAIT_CALENDAR", cal.get("error")))
                continue
            earliest, decided = date.fromisoformat(o["earliest_fill_session"]), date.fromisoformat(o["decision_session"])
            N = next((d for d in cal["calendar"] if d >= earliest and d > decided), None)
            if N is None:
                plan.append((o, None, "NOT_YET_AVAILABLE", f"The fill session (the first session opening after "
                             f"{o['submitted_at']}) has not completed yet."))
                continue
            ser = series.get(o["symbol"])
            if ser is None or not ser.has(N):
                plan.append((o, None, "NEXT_OPEN_UNAVAILABLE", f"No {N.isoformat()} open for {o['symbol']} in the daily bars; "
                             "the order stays pending (a price is never invented)."))
                continue
            plan.append((o, (N, ser.bar(N).open), None, None))
        order_key = {"SELL": 0, "BUY": 1}
        ready = sorted([p for p in plan if p[1] is not None], key=lambda p: (p[1][0], order_key[p[0]["side"]],
                                                                           p[0]["submitted_at"], p[0]["order_id"]))
        for o, _, code, text in [p for p in plan if p[1] is None]:
            _wait(store, o["order_id"], code, now)
            out["still_pending"] += 1
            out["orders"].append({"order_id": o["order_id"], "symbol": o["symbol"], "side": o["side"], "status": "PENDING",
                                  "fill_id": None, "reason": code, "text": text})
        for o, (N, open_px), _, _ in ready:
            try:
                r = _fill(store, acct, o, N, open_px, prov.get(o["symbol"]) or {}, now)
            except Exception as exc:  # noqa: BLE001 - one order's failure never affects the others
                r = {"status": "PENDING", "fill_id": None, "reason": f"ERROR_{type(exc).__name__}"}
                out["errors"] += 1
            key = {"FILLED": "filled", "REJECTED": "rejected"}.get(r["status"])
            if key:
                out[key] += 1
            elif r["status"] == "PENDING" and not r["reason"].startswith("ERROR"):
                out["still_pending"] += 1
            out["orders"].append({"order_id": o["order_id"], "symbol": o["symbol"], "side": o["side"], **r})
        out["status"] = "PROCESSED"
        return out


def _wait(store: PaperStore, order_id: str, code: str, now: datetime) -> None:
    with store.connect() as conn:
        conn.execute("UPDATE paper_orders SET wait_reason = ?, checked_at = ? WHERE order_id = ? AND status = 'PENDING'",
                     (code, _iso(now), order_id))
        conn.commit()


def _fill(store: PaperStore, acct: dict, o: dict, N: date, open_px: float, prov: dict, now: datetime) -> dict:
    """ONE order, ONE transaction: re-check PENDING, then fill (fill + lot / FIFO closures + FILLED) or reject."""
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("SELECT status FROM paper_orders WHERE order_id = ?", (o["order_id"],)).fetchone()
        if cur is None or cur["status"] != "PENDING":
            conn.rollback()
            return {"status": cur["status"] if cur else "MISSING", "fill_id": None, "reason": "ALREADY_RESOLVED"}
        acct = dict(conn.execute("SELECT * FROM paper_accounts WHERE account_id = ?", (acct["account_id"],)).fetchone())
        fills, lots, closures = store.ledger(acct["account_id"], conn)
        cash = A.cash(acct["starting_cash"], fills)
        base = A.price(open_px)
        eff = A.effective_price(base, acct["slippage_bps"], o["side"])
        stamp = _iso(now)
        fid = uuid.uuid4().hex
        common = {"fill_id": fid, "order_id": o["order_id"], "account_id": acct["account_id"], "symbol": o["symbol"],
                  "side": o["side"], "quantity": o["quantity"], "fill_session": N.isoformat(), "base_price": A.s(base),
                  "slippage_bps": acct["slippage_bps"], "effective_price": A.s(eff), "price_source": prov.get("source") or "FETCHED_NOW",
                  "adjustment": B.ADJUSTMENT, "feed": B.feed(), "dataset_id": prov.get("dataset_id"),
                  "content_hash": prov.get("content_hash"), "filled_at": stamp, "engine_version": ENGINE_VERSION}
        reject = None
        if o["side"] == "BUY":
            c = A.buy_cost(eff, o["quantity"], acct["commission_per_order"])
            if c["total"] > cash:
                reject = (f"INSUFFICIENT_CASH_AT_FILL: cost {A.s(c['total'])} at the {N.isoformat()} open exceeds paper cash "
                          f"{A.s(cash)} (no margin).")
            else:
                after = A.money(cash - c["total"])
                store.insert(conn, "paper_fills", {**common, "notional": A.s(c["notional"]), "commission": A.s(c["commission"]),
                                                   "cash_delta": A.s(-c["total"]), "cash_after": A.s(after),
                                                   "cost_basis_removed": None, "realized_pnl": None})
                store.insert(conn, "paper_lots", {"lot_id": uuid.uuid4().hex, "account_id": acct["account_id"], "symbol": o["symbol"],
                                                  "open_fill_id": fid, "entry_session": N.isoformat(), "quantity": o["quantity"],
                                                  "total_cost": A.s(c["total"]), "created_at": stamp})
        else:
            fifo = A.open_lots(lots, closures, o["symbol"])
            owned = sum(x["remaining"] for x in fifo)
            p = A.sell_proceeds(eff, o["quantity"], acct["commission_per_order"])
            if owned < o["quantity"]:
                reject = f"INSUFFICIENT_SHARES_AT_FILL: {owned} paper share(s) held (long only, no short)."
            elif cash + p["net"] < 0:
                reject = f"INSUFFICIENT_CASH_AT_FILL: the commission exceeds the sale proceeds and paper cash {A.s(cash)}."
            else:
                cl = A.fifo_close(fifo, o["quantity"], p["net"])
                basis = A.money(sum((x["cost_basis"] for x in cl), A.ZERO))
                store.insert(conn, "paper_fills", {**common, "notional": A.s(p["notional"]), "commission": A.s(p["commission"]),
                                                   "cash_delta": A.s(p["net"]), "cash_after": A.s(A.money(cash + p["net"])),
                                                   "cost_basis_removed": A.s(basis), "realized_pnl": A.s(A.money(p["net"] - basis))})
                for x in cl:
                    store.insert(conn, "paper_lot_closures", {"closure_id": uuid.uuid4().hex, "account_id": acct["account_id"],
                                                              "lot_id": x["lot_id"], "sell_fill_id": fid, "symbol": o["symbol"],
                                                              "quantity": x["quantity"], "cost_basis": A.s(x["cost_basis"]),
                                                              "proceeds": A.s(x["proceeds"]), "realized_pnl": A.s(x["realized_pnl"])})
        if reject:
            conn.execute("UPDATE paper_orders SET status = 'REJECTED', reject_reason = ?, resolved_at = ?, checked_at = ? "
                         "WHERE order_id = ? AND status = 'PENDING'", (reject, stamp, stamp, o["order_id"]))
            conn.commit()
            return {"status": "REJECTED", "fill_id": None, "reason": reject.split(":")[0], "text": reject}
        conn.execute("UPDATE paper_orders SET status = 'FILLED', fill_id = ?, fill_session = ?, resolved_at = ?, checked_at = ?, "
                     "wait_reason = NULL WHERE order_id = ? AND status = 'PENDING'", (fid, N.isoformat(), stamp, stamp, o["order_id"]))
        conn.commit()
    return {"status": "FILLED", "fill_id": fid, "reason": None, "fill_session": N.isoformat(), "effective_price": A.s(eff)}
