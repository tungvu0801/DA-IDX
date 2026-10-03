"""
paper/alpaca_orders.py — Stage 4.6B service: manual Alpaca PAPER orders (DESIGN_46B_FINAL).

  settings    settings_view · link_account · relink_account · enable · disable     (no broker writes; ≤ 2 GET each)
  orders      preview (≤ 5 paper GET + ≤ 1 market-data request, 0 POST) → explicit confirm (≤ 4 GET + exactly 1 POST +
              ≤ 1 exact lookup) · retry (explicit; same client_order_id; exact lookup first) · abandon (explicit; exact
              lookup first) · check_status (exact lookups only) · list_intents (0 requests)

The ONLY path to the broker write (paper/alpaca_order_writer.py) is `_post_once`, called only by `confirm` and `retry`,
after the intent was committed as SUBMISSION_PENDING (write-ahead) and every gate (V1–V17) passed on fresh reads.
No automatic POST retry. Exact-ID reconciliation only (client_order_id / Alpaca order id; never symbol / qty / time /
price matching). Deterministic Python rules only — no Claude. No background job, startup hook or timer. The Stage 4.5
simulator is only read (local shares, for information) and never written; Stage 4.6A is not used or changed.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from backtest import bars as B
from fit import current as FC
from fit import readonly as RO
from paper import accounting as A
from paper import alpaca_order_reads as RD
from paper import alpaca_order_rules as RU
from paper import alpaca_order_store as S
from paper import alpaca_order_writer as W
from paper.store import PaperStore

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
LABEL = "PAPER ACCOUNT — simulated trading only"
NOTE = ("Orders go to your Alpaca PAPER account (simulated money at Alpaca). Stock Agent sends one order only when you "
        "click Confirm, never on its own, never with AI, and never cancels, replaces or closes anything.")

_LOCK = threading.RLock()                 # one order operation at a time in this process
_INFLIGHT: set = set()                    # intents with a confirm / retry running in this process (stale rules skip them)
_LAST_STATUS: list = [None]               # time of the last status check (spacing)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _reset() -> None:
    """Tests only: forget in-process state."""
    _INFLIGHT.clear()
    _LAST_STATUS[0] = None


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class OrderError(Exception):
    def __init__(self, code: str, message: Optional[str] = None, status: int = 409, **extra):
        super().__init__(code)
        self.code, self.message, self.status, self.extra = code, message or RU.RULE_TEXT.get(code, code), status, extra


def _store() -> S.OrderStore:
    return S.OrderStore(RO.db_path())


# ==================================================================================================================================
# settings and account binding (no broker writes)
# ==================================================================================================================================

def settings_view() -> dict:
    cfg = RD.configured()
    st = _store()
    s = st.settings() or {}
    return {"label": LABEL, "note": NOTE, "configured": cfg["configured"], "missing": cfg["missing"],
            "credential_names": cfg["credential_names"], "tables": st.exists(), "linked": bool(s.get("bound_account_fp")),
            "account_masked": s.get("bound_account_masked"), "bound_at": s.get("bound_at"), "enabled": bool(s.get("enabled")),
            "last_no_shorting": None if s.get("last_no_shorting") is None else bool(s["last_no_shorting"]),
            "last_config_check_at": s.get("last_config_check_at"),
            "limits": {"max_shares": RU.MAX_QTY, "max_notional": str(RU.MAX_NOTIONAL), "daily_confirms": RU.DAILY_LIMIT,
                       "buy_buffer_pct": 5, "preview_ttl_s": RU.PREVIEW_TTL_S, "near_close_min": RU.NEAR_CLOSE_S // 60}}


def _require_configured() -> None:
    if not RD.configured()["configured"]:
        raise OrderError("NOT_CONFIGURED")


def _linked(st: S.OrderStore) -> dict:
    s = st.settings()
    if not s or not s.get("bound_account_fp"):
        raise OrderError("NOT_LINKED")
    return s


def _reader(budget: int) -> RD.PaperOrderReader:
    try:
        return RD.PaperOrderReader(budget)
    except RD.NotConfigured:
        raise OrderError("NOT_CONFIGURED") from None


def _read(fn):
    try:
        return fn()
    except RD.ReadError as exc:
        log.warning("alpaca paper order read failed: %s", exc.code)
        raise OrderError("BROKER_READ_FAILED", f"The Alpaca paper account could not be read ({exc.code}). Nothing was sent.",
                         503, read_code=exc.code, http_status=exc.http_status) from None


def link_account() -> dict:
    with _LOCK:
        _require_configured()
        st = _store()
        s = st.settings()
        if s and s.get("bound_account_fp"):
            raise OrderError("ALREADY_LINKED", "A paper account is already linked. Use Relink to change it.")
        rd = _reader(2)
        acct, conf = _read(rd.account), _read(rd.configurations)
        now = _now()
        with st.write() as c:                                                 # the first write creates the tables
            st.save_settings(c, now, enabled=0, bound_account_fp=acct["fingerprint"], bound_account_masked=acct["masked"],
                             bound_at=_iso(now), last_no_shorting=int(conf["no_shorting"]), last_config_check_at=_iso(now))
            st.event(c, None, "LINKED", now)
            st.event(c, None, "CONFIG_CHECK", now, code="NO_SHORTING_ON" if conf["no_shorting"] else "NO_SHORTING_OFF")
        log.info("alpaca paper orders: account %s linked", acct["masked"])
        return settings_view()


def relink_account(current_account_masked: str) -> dict:
    with _LOCK:
        _require_configured()
        st = _store()
        s = _linked(st)
        if current_account_masked != s["bound_account_masked"]:
            raise OrderError("RELINK_MISMATCH", "The linked account named in the request is not the linked account.")
        _apply_stale(st)
        if st.in_states(RU.UNRESOLVED):
            raise OrderError("UNRESOLVED_INTENTS", "Resolve every order with an unresolved outcome before relinking.")
        rd = _reader(2)
        acct, conf = _read(rd.account), _read(rd.configurations)
        if acct["fingerprint"] == s["bound_account_fp"]:
            raise OrderError("SAME_ACCOUNT", "The configured paper account is already the linked account.")
        now = _now()
        with st.write() as c:
            st.supersede_previews(c, now)
            st.save_settings(c, now, enabled=0, bound_account_fp=acct["fingerprint"], bound_account_masked=acct["masked"],
                             bound_at=_iso(now), last_no_shorting=int(conf["no_shorting"]), last_config_check_at=_iso(now))
            st.event(c, None, "RELINKED", now)
            st.event(c, None, "CONFIG_CHECK", now, code="NO_SHORTING_ON" if conf["no_shorting"] else "NO_SHORTING_OFF")
        log.info("alpaca paper orders: relinked to %s (manual orders OFF)", acct["masked"])
        return settings_view()


def enable() -> dict:
    with _LOCK:
        _require_configured()
        st = _store()
        s = _linked(st)
        rd = _reader(2)
        acct, conf = _read(rd.account), _read(rd.configurations)
        now = _now()
        with st.write() as c:
            st.save_settings(c, now, last_no_shorting=int(conf["no_shorting"]), last_config_check_at=_iso(now))
            st.event(c, None, "CONFIG_CHECK", now, code="NO_SHORTING_ON" if conf["no_shorting"] else "NO_SHORTING_OFF")
        v8 = RU.v8_account(acct, s["bound_account_fp"])
        if not v8["ok"]:
            raise OrderError(v8["code"])
        if not RU.v15_no_shorting(conf)["ok"]:
            raise OrderError("NO_SHORTING_REQUIRED")
        with st.write() as c:
            st.save_settings(c, now, enabled=1)
            st.event(c, None, "ENABLED", now)
        return settings_view()


def disable() -> dict:
    with _LOCK:
        st = _store()
        if not st.exists():                                                   # nothing linked: nothing to write, no tables
            return settings_view()
        now = _now()
        with st.write() as c:
            st.supersede_previews(c, now)
            st.save_settings(c, now, enabled=0)
            st.event(c, None, "DISABLED", now)
        return settings_view()


# ==================================================================================================================================
# helpers: stale rules, reference price, views
# ==================================================================================================================================

def _apply_stale(st: S.OrderStore) -> None:
    """DESIGN_46B_FINAL §4: interrupted CONFIRMING / SUBMISSION_PENDING (no in-process owner) — DB only, no request."""
    if not st.exists():
        return
    now = _now()
    for r in st.in_states({RU.CONFIRMING, RU.SUBMISSION_PENDING}):
        if r["intent_id"] in _INFLIGHT:
            continue
        if r["state"] == RU.CONFIRMING and (now - RU.parse_time(r["confirmed_at"])).total_seconds() > RU.STALE_CONFIRMING_S:
            with st.write() as c:
                st.transition(c, r["intent_id"], {RU.CONFIRMING}, RU.CONFIRM_REJECTED, now, kind="CONFIRM_REJECTED",
                              error_code="INTERRUPTED", error_text=RU.OUTCOME_TEXT["INTERRUPTED"])
        elif r["state"] == RU.SUBMISSION_PENDING and \
                (now - RU.parse_time(r["last_submit_at"])).total_seconds() > RU.STALE_SUBMITTING_S:
            with st.write() as c:
                st.transition(c, r["intent_id"], {RU.SUBMISSION_PENDING}, RU.RR, now,
                              error_code="INTERRUPTED_SUBMISSION", error_text=RU.OUTCOME_TEXT["INTERRUPTED_SUBMISSION"])


def _reference(symbol: str) -> dict:
    """DESIGN_46B_FINAL §6.3: the latest completed daily close from the existing Stage 3.2/3.4 bar layer (local cache →
    memory → one batched market-data request, never stored). Informational only."""
    now = FC._utc()
    last_complete = B.last_complete_session_date(now)
    out = {"price": None, "session": None, "source": None, "feed": None, "market_data_requests": 0, "reason": None}
    try:
        out["feed"] = B.feed()
        series, prov, fetched = FC.load_bars(RO.ReadOnlyBacktestStore(RO.db_path()), sorted({symbol, FC.SPY}),
                                             last_complete - timedelta(days=45), last_complete, now, FC.BAR_CACHE)
    except Exception as exc:  # noqa: BLE001 - no reference price (the BUY cash guard then refuses); type name only
        log.warning("reference price unavailable for %s (%s)", symbol, type(exc).__name__)
        return {**out, "market_data_requests": None, "reason": "Market data unavailable."}
    out["market_data_requests"] = fetched.get("requests", 0)
    sess = FC.resolve_session(series, last_complete)
    T, ser = sess.get("T"), series.get(symbol)
    if not T:
        return {**out, "reason": sess.get("error") or "The latest completed session could not be confirmed."}
    if ser is None or not ser.has(T):
        return {**out, "session": T.isoformat(), "reason": f"No daily bar for {symbol} at the {T.isoformat()} close."}
    return {**out, "price": A.s(A.price(ser.bar(T).close)), "session": T.isoformat(),
            "source": (prov.get(symbol) or {}).get("source")}


def _local_shares(symbol: str) -> Optional[int]:
    """Stage 4.5 simulator shares — read only, for information (the simulator is independent)."""
    try:
        ps = PaperStore(RO.db_path())
        acct = ps.account()
        if not acct:
            return None
        fills, lots, closures = ps.ledger(acct["account_id"])
        return sum(x["remaining"] for x in A.open_lots(lots, closures, symbol))
    except Exception:  # noqa: BLE001
        return None


def _market(clock: dict) -> dict:
    s = RU.seconds_to_close(clock)
    until = None
    try:
        until = _iso(RU.parse_time(clock["next_close"]) - timedelta(seconds=RU.NEAR_CLOSE_S))
    except (KeyError, TypeError, ValueError):
        pass
    return {"timestamp": clock.get("timestamp"), "is_open": clock.get("is_open") is True, "next_open": clock.get("next_open"),
            "next_close": clock.get("next_close"), "seconds_to_close": s, "near_close": s is None or s <= RU.NEAR_CLOSE_S,
            "confirm_available_until": until}


def _view(row: dict, now: Optional[datetime] = None) -> dict:
    p = json.loads(row["preview_json"])
    now = now or _now()
    state, side, qty, sym = row["state"], row["side"].upper(), row["qty"], row["symbol"]
    expired = state == RU.PREVIEWED and now >= RU.parse_time(row["expires_at"])
    ref = p["reference"]
    return {"intent_id": row["intent_id"], "client_order_id": row["client_order_id"], "symbol": sym, "side": side, "qty": qty,
            "order_type": "MARKET", "time_in_force": "DAY", "state": state, "state_label": RU.LABELS.get(state, state),
            "confirmable": state == RU.PREVIEWED and bool(p.get("confirmable")) and not expired, "expired": expired,
            "preview_hash": row["preview_hash"], "previewed_at": row["previewed_at"], "expires_at": row["expires_at"],
            "label": LABEL, "account_masked": row["account_masked"],
            "reference": {**ref, "note": RU.REFERENCE_NOTE.format(ref["session"]) if ref.get("price") else
                          "no previous close available — not a quote, not the fill price"},
            "estimated_notional": p.get("estimated_notional"), "notional_note": "estimate only",
            "cash_guard": ({"required": p.get("cash_guard_required"), "cash": p.get("cash"), "buffer_pct": 5,
                            "text": RU.CASH_GUARD_TEXT} if side == "BUY" else None),
            "sell": ({"alpaca_qty": (p.get("position") or {}).get("qty"), "qty_available": (p.get("position") or {}).get("qty_available"),
                      "local_simulator_shares": p.get("local_simulator_shares")} if side == "SELL" else None),
            "market": p.get("market"), "rules": p.get("rules"), "confirm_label": f"Confirm Paper Order: {side} {qty} {sym}",
            "confirmed_at": row["confirmed_at"], "alpaca_order_id": row["alpaca_order_id"],
            "alpaca_order_ref": row["alpaca_order_id"][:8] if row["alpaca_order_id"] else None,
            "broker_status": row["broker_status"], "broker_status_at": row["broker_status_at"], "filled_qty": row["filled_qty"],
            "filled_avg_price": row["filled_avg_price"], "error_code": row["error_code"], "error_text": row["error_text"],
            "http_status": row["http_status"], "submit_attempts": row["submit_attempts"],
            "can_retry": state in (RU.SUBMIT_NOT_SENT, RU.RR) and row["submit_attempts"] < RU.MAX_ATTEMPTS,
            "can_abandon": state in (RU.SUBMIT_NOT_SENT, RU.RR), "unresolved": state in RU.UNRESOLVED,
            "terminal": state in RU.TERMINAL}


def list_intents(limit: int = 100) -> dict:
    st = _store()
    now = _now()
    return {"settings": settings_view(), "intents": [_view(r, now) for r in st.intents(limit)]}


def _confirmed_today(st: S.OrderStore, now: datetime) -> int:
    today = now.astimezone(NY).date()
    return sum(1 for t in st.submitted_since(_iso(now - timedelta(days=2))) if RU.parse_time(t).astimezone(NY).date() == today)


def _other_buy_guard(others) -> Decimal:
    total = Decimal(0)
    for u in others:
        if u["side"] == "buy" and u["reference_price"]:
            total += RU.guard_amount(u["qty"], RU.dec(u["reference_price"]))
    return total


# ==================================================================================================================================
# preview
# ==================================================================================================================================

def preview(symbol, side, quantity) -> dict:
    with _LOCK:
        sym = symbol.strip().upper() if isinstance(symbol, str) else symbol
        bad = RU.body_errors(sym, side, quantity)
        if bad:
            raise OrderError(bad, status=422)
        _require_configured()
        st = _store()
        s = _linked(st)
        if not s.get("enabled"):
            raise OrderError("NOT_ENABLED")
        _apply_stale(st)
        now = _now()
        unresolved = st.in_states(RU.UNRESOLVED)
        if any(u["symbol"] == sym for u in unresolved):
            raise OrderError("UNRESOLVED_IN_SYMBOL")
        confirmed_today = _confirmed_today(st, now)
        if confirmed_today >= RU.DAILY_LIMIT:
            raise OrderError("DAILY_LIMIT")
        rd = _reader(5)
        acct, conf = _read(rd.account), _read(rd.configurations)
        asset, clock = _read(lambda: rd.asset(sym)), _read(rd.clock)
        position = _read(lambda: rd.position(sym)) if side == "SELL" else None
        ref = _reference(sym)
        reference = RU.dec(ref["price"]) if ref["price"] else None
        qty = quantity
        notional = (Decimal(qty) * reference).quantize(RU.MONEY) if reference is not None else None
        cash = RU.dec(acct["cash"]) if acct.get("cash") is not None else None
        rules = [RU.result("V1", True), RU.result("V2", True), RU.v3_asset(asset), RU.result("V4", True),
                 RU.result("V5", True), RU.result("V6", True), RU.result("V7", True), RU.v8_account(acct, s["bound_account_fp"])]
        if side == "SELL":
            rules.append(RU.v9_sell(position, qty, sum(u["qty"] for u in unresolved if u["side"] == "sell" and u["symbol"] == sym)))
        else:
            rules.append(RU.v10_buy(reference, qty, cash, _other_buy_guard(unresolved)))
        rules += [RU.v11_unresolved(0), RU.v12a_daily(confirmed_today), RU.v12b_notional(notional), RU.v15_no_shorting(conf),
                  RU.v16_open(clock, info=True), RU.v17_near_close(clock, info=True)]
        coid = RU.new_client_order_id()
        payload = RU.canonical_payload(coid, sym, side.lower(), qty)
        expires = now + timedelta(seconds=RU.PREVIEW_TTL_S)
        facts = {"version": 1, "client_order_id": coid, "payload_sha256": RU.sha256_hex(payload), "symbol": sym,
                 "side": side.lower(), "qty": qty, "order_type": "market", "time_in_force": "day",
                 "account_masked": acct["masked"], "account_fp": acct["fingerprint"], "previewed_at": _iso(now),
                 "expires_at": _iso(expires), "reference": ref,
                 "estimated_notional": RU.money(notional) if notional is not None else None,
                 "cash": RU.money(cash) if cash is not None else None,
                 "cash_guard_required": RU.money(RU.guard_amount(qty, reference)) if side == "BUY" and reference is not None else None,
                 "position": position, "local_simulator_shares": _local_shares(sym) if side == "SELL" else None,
                 "market": _market(clock), "rules": rules, "confirmable": not RU.blocking(rules)}
        row = {"client_order_id": coid, "symbol": sym, "side": side.lower(), "qty": qty, "order_type": "market",
               "time_in_force": "day", "payload_json": payload.decode("utf-8"), "payload_sha256": RU.sha256_hex(payload),
               "reference_price": ref["price"], "reference_session": ref["session"], "reference_source": ref["source"],
               "estimated_notional": facts["estimated_notional"], "preview_json": RU.canonical_json(facts),
               "preview_hash": RU.preview_hash(facts), "account_fp": acct["fingerprint"], "account_masked": acct["masked"],
               "previewed_at": _iso(now), "expires_at": _iso(expires)}
        with st.write() as c:
            iid = st.insert_intent(c, row, now)
        log.info("alpaca paper order preview #%s %s %s %s %s (confirmable=%s)", iid, row["client_order_id"], side, qty, sym,
                 facts["confirmable"])
        return {"intent": _view(st.intent(iid), now)}


# ==================================================================================================================================
# confirm / retry / abandon — the only paths that can reach the writer are confirm and retry, via _post_once
# ==================================================================================================================================

def _fresh_gates(st: S.OrderStore, row: dict, s: dict, include_daily: bool) -> list:
    """V8, V9 | V10, V11, V12a (confirm only), V12b, V15, V16, V17 on fresh reads (≤ 4 GET)."""
    rd = _reader(4)
    acct, conf, clock = _read(rd.account), _read(rd.configurations), _read(rd.clock)
    position = _read(lambda: rd.position(row["symbol"])) if row["side"] == "sell" else None
    others = [u for u in st.in_states(RU.UNRESOLVED) if u["intent_id"] != row["intent_id"]]
    gates = [RU.v8_account(acct, s["bound_account_fp"]), RU.v15_no_shorting(conf), RU.v16_open(clock), RU.v17_near_close(clock)]
    if row["side"] == "sell":
        gates.append(RU.v9_sell(position, row["qty"], sum(u["qty"] for u in others if u["side"] == "sell" and u["symbol"] == row["symbol"])))
    else:
        gates.append(RU.v10_buy(RU.dec(row["reference_price"]), row["qty"], RU.dec(acct.get("cash")), _other_buy_guard(others)))
    gates.append(RU.v11_unresolved(sum(1 for u in others if u["symbol"] == row["symbol"])))
    if include_daily:
        gates.append(RU.v12a_daily(_confirmed_today(st, _now())))
    gates.append(RU.v12b_notional(RU.dec(row["estimated_notional"])))
    return gates


def _post_once(intent_id: int, row: dict) -> dict:
    """The single broker write of one attempt — inside the explicit confirm / retry scope."""
    token = W._SCOPE.set(intent_id)
    try:
        return W.submit(row["payload_json"].encode("utf-8"), row["client_order_id"])
    except W.WriteRefused:
        return {"kind": "not_sent", "reason": "LOCAL_REFUSAL"}
    except Exception as exc:  # noqa: BLE001 - the writer failed before any request was built: nothing was sent
        log.error("alpaca paper order #%s: writer failed before sending (%s)", intent_id, type(exc).__name__)
        return {"kind": "not_sent", "reason": "LOCAL_REFUSAL"}
    finally:
        W._SCOPE.reset(token)


def _lookup(row: dict, rd: Optional[RD.PaperOrderReader] = None) -> tuple:
    """Exact lookup by Alpaca order id (once known) or client_order_id. ("found", order) | ("not_found", None) | ("error", code)."""
    try:
        rd = rd or _reader(1)
        o = rd.order_by_id(row["alpaca_order_id"]) if row["alpaca_order_id"] else rd.order_by_client_id(row["client_order_id"])
    except RD.ReadError as exc:
        return ("error", exc.code)
    except OrderError as exc:
        return ("error", exc.code)
    return ("found", o) if o else ("not_found", None)


def _not_found_fields(row: dict, now: datetime) -> dict:
    return {"lookups_not_found": row["lookups_not_found"] + 1, "first_not_found_at": row["first_not_found_at"] or _iso(now),
            "last_lookup_at": _iso(now)}


def _not_found_rule_met(fields: dict, now: datetime) -> bool:
    return fields["lookups_not_found"] >= 2 and \
        (now - RU.parse_time(fields["first_not_found_at"])).total_seconds() >= RU.NOT_FOUND_SPAN_S


def _link(st: S.OrderStore, row: dict, order: dict, now: datetime, kind: Optional[str] = None, http_status=None) -> dict:
    """An exactly matching Alpaca order: record its id (once) and map its status (§8)."""
    state, code = RU.map_status(order["status"])
    fields = {"broker_status": order["status"], "broker_status_at": _iso(now), "filled_qty": order["filled_qty"],
              "filled_avg_price": order["filled_avg_price"], "error_code": code, "error_text": RU.OUTCOME_TEXT.get(code) if code else None,
              "last_lookup_at": _iso(now)}
    if http_status is not None:
        fields["http_status"] = http_status
    if row["alpaca_order_id"] is None:
        fields["alpaca_order_id"] = order["id"]
    with st.write() as c:
        st.transition(c, row["intent_id"], {row["state"]}, state, now, kind=kind, **fields)
    return st.intent(row["intent_id"])


def _mismatch(st: S.OrderStore, row: dict, now: datetime, kind: Optional[str] = None, **extra) -> dict:
    with st.write() as c:
        st.transition(c, row["intent_id"], {row["state"]}, RU.RR, now, kind=kind, error_code="MISMATCH",
                      error_text=RU.OUTCOME_TEXT["MISMATCH"], last_lookup_at=_iso(now), **extra)
    return st.intent(row["intent_id"])


def _settle(st: S.OrderStore, intent_id: int, res: dict) -> tuple:
    """Apply the POST outcome (§7 O1–O8): at most ONE automatic, read-only exact lookup. Returns (row, outcome)."""
    row = st.intent(intent_id)
    cls = RU.classify_post(res, row)
    now = _now()
    outcome = {"class": cls["klass"], "code": cls["code"], "http_status": cls["http_status"], "lookup": cls["lookup"]}
    base = {"http_status": cls["http_status"], "broker_error_code": cls["broker_error_code"]}
    log.info("alpaca paper order #%s %s: POST outcome %s (HTTP %s)", intent_id, row["client_order_id"], cls["klass"],
             cls["http_status"])
    if cls["klass"] == "O1":
        return _link(st, row, cls["order"], now, kind="SUBMIT_RESULT", http_status=200), outcome
    if cls["klass"] == "O3":
        with st.write() as c:
            st.transition(c, intent_id, {RU.SUBMISSION_PENDING}, RU.SUBMIT_NOT_SENT, now, kind="SUBMIT_RESULT",
                          error_code=cls["code"], error_text=RU.OUTCOME_TEXT.get(cls["code"], RU.OUTCOME_TEXT["NOT_SENT"]), **base)
        return st.intent(intent_id), outcome
    if cls["lookup"] == "later":
        _sleep(RU.AUTO_LOOKUP_DELAY_S)
    found, obj = _lookup(row)
    now = _now()
    with st.write() as c:
        st.event(c, intent_id, "LOOKUP", now, code=found.upper() if found != "error" else f"ERROR_{obj}")
    outcome["lookup_result"] = found
    if found == "found":
        if RU.consistent(obj, row):
            return _link(st, row, obj, now, kind="SUBMIT_RESULT", http_status=cls["http_status"]), outcome
        return _mismatch(st, row, now, kind="SUBMIT_RESULT", **base), outcome
    if cls["klass"] in ("O5", "O6") and found == "not_found":
        with st.write() as c:
            st.transition(c, intent_id, {RU.SUBMISSION_PENDING}, RU.BROKER_REJECTED, now, kind="SUBMIT_RESULT",
                          error_code=cls["code"], error_text=RU.CATEGORY_TEXT[cls["code"]], last_lookup_at=_iso(now), **base)
        return st.intent(intent_id), outcome
    extra = _not_found_fields(row, now) if found == "not_found" else {"last_lookup_at": _iso(now)}
    with st.write() as c:
        st.transition(c, intent_id, {RU.SUBMISSION_PENDING}, RU.RR, now, kind="SUBMIT_RESULT", error_code=cls["code"],
                      error_text=RU.uncertain_text(cls["code"])[:200], **base, **extra)
    return st.intent(intent_id), outcome


def _gate_failure(gates: list) -> Optional[dict]:
    failing = RU.blocking(gates)
    return failing[0] if failing else None


def confirm(preview_id: int, preview_hash: str) -> dict:
    with _LOCK:
        st = _store()
        _apply_stale(st)
        row = st.intent(preview_id)
        if row is None:
            raise OrderError("NOT_FOUND", "No such paper order preview.", 404)
        if row["preview_hash"] != preview_hash:
            raise OrderError("PREVIEW_MISMATCH", "This is not the exact stored preview. Preview again.")
        now = _now()
        if row["state"] != RU.PREVIEWED:
            if row["state"] == RU.SUPERSEDED:
                raise OrderError("PREVIEW_SUPERSEDED", "A newer preview replaced this one. Confirm the newest preview.")
            if row["state"] == RU.EXPIRED:
                raise OrderError("PREVIEW_EXPIRED", "This preview expired (120 s). Preview again.")
            if row["state"] == RU.CONFIRM_REJECTED:
                raise OrderError("CONFIRM_REJECTED", row["error_text"], reason=row["error_code"], intent=_view(row, now))
            return {"intent": _view(row, now), "already_confirmed": True, "outcome": None}      # 0 requests
        if now >= RU.parse_time(row["expires_at"]):
            with st.write() as c:
                st.transition(c, preview_id, {RU.PREVIEWED}, RU.EXPIRED, now, kind="EXPIRED")
            raise OrderError("PREVIEW_EXPIRED", "This preview expired (120 s). Preview again.")
        if not json.loads(row["preview_json"]).get("confirmable"):
            raise OrderError("PREVIEW_NOT_CONFIRMABLE", "This preview did not pass every rule, so it cannot be confirmed.")
        _require_configured()
        s = _linked(st)
        if not s.get("enabled"):
            raise OrderError("NOT_ENABLED")
        with st.write() as c:
            won = st.transition(c, preview_id, {RU.PREVIEWED}, RU.CONFIRMING, now, kind="CONFIRM_STARTED", confirmed_at=_iso(now))
        if not won:
            return {"intent": _view(st.intent(preview_id)), "already_confirmed": True, "outcome": None}
        _INFLIGHT.add(preview_id)
        try:
            try:
                fail = _gate_failure(_fresh_gates(st, row, s, include_daily=True))
            except OrderError as exc:
                fail = {"code": "BROKER_READ_FAILED", "text": exc.message}
            if fail:
                with st.write() as c:
                    st.transition(c, preview_id, {RU.CONFIRMING}, RU.CONFIRM_REJECTED, _now(), kind="CONFIRM_REJECTED",
                                  error_code=fail["code"], error_text=(fail["text"] or fail["code"])[:200])
                raise OrderError("CONFIRM_REJECTED", fail["text"], reason=fail["code"], intent=_view(st.intent(preview_id)))
            t = _now()
            with st.write() as c:                                             # write-ahead: committed BEFORE the POST
                st.transition(c, preview_id, {RU.CONFIRMING}, RU.SUBMISSION_PENDING, t, kind="SUBMIT_STARTED",
                              submit_attempts=row["submit_attempts"] + 1, last_submit_at=_iso(t), lookups_not_found=0,
                              first_not_found_at=None)
            res = _post_once(preview_id, row)
            final, outcome = _settle(st, preview_id, res)
            return {"intent": _view(final), "already_confirmed": False, "outcome": outcome}
        finally:
            _INFLIGHT.discard(preview_id)


def retry(intent_id: int, preview_hash: str) -> dict:
    with _LOCK:
        st = _store()
        _apply_stale(st)
        row = st.intent(intent_id)
        if row is None:
            raise OrderError("NOT_FOUND", "No such paper order.", 404)
        if row["preview_hash"] != preview_hash:
            raise OrderError("PREVIEW_MISMATCH", "This is not the exact stored order.")
        if row["state"] not in (RU.SUBMIT_NOT_SENT, RU.RR):
            raise OrderError("NOT_RETRYABLE", "Only an order that was not sent or needs reconciliation can be retried.")
        _require_configured()
        s = _linked(st)
        if not s.get("enabled"):
            raise OrderError("NOT_ENABLED")
        if row["submit_attempts"] >= RU.MAX_ATTEMPTS:
            raise OrderError("ATTEMPTS_EXHAUSTED", "This order already had 3 submission attempts.")
        _INFLIGHT.add(intent_id)
        try:
            fail = _gate_failure(_fresh_gates(st, row, s, include_daily=False))
            if fail:
                raise OrderError("RETRY_REJECTED", fail["text"], reason=fail["code"], intent=_view(row))
            found, obj = _lookup(row)                                         # exact lookup immediately before any POST
            now = _now()
            if found == "error":
                with st.write() as c:
                    st.event(c, intent_id, "LOOKUP", now, code=f"ERROR_{obj}")
                raise OrderError("LOOKUP_FAILED", "The exact order lookup failed; nothing was sent.", 503)
            if found == "found":
                with st.write() as c:
                    st.event(c, intent_id, "LOOKUP", now, code="FOUND")
                if RU.consistent(obj, row):
                    return {"intent": _view(_link(st, row, obj, now)), "linked_without_post": True, "outcome": None}
                return {"intent": _view(_mismatch(st, row, now)), "linked_without_post": False, "outcome": None}
            fields = _not_found_fields(row, now)
            if row["state"] == RU.RR and not _not_found_rule_met(fields, now):
                with st.write() as c:
                    st.transition(c, intent_id, {RU.RR}, RU.RR, now, kind="LOOKUP", **fields)
                raise OrderError("NOT_FOUND_RULE", "Retry needs at least 2 exact lookups finding nothing over at least 30 "
                                 "seconds. Check the order status again later.")
            with st.write() as c:
                st.transition(c, intent_id, {row["state"]}, RU.SUBMISSION_PENDING, now, kind="RETRY",
                              submit_attempts=row["submit_attempts"] + 1, last_submit_at=_iso(now), lookups_not_found=0,
                              first_not_found_at=None, last_lookup_at=_iso(now), error_code=None, error_text=None,
                              http_status=None, broker_error_code=None)
            res = _post_once(intent_id, row)
            final, outcome = _settle(st, intent_id, res)
            return {"intent": _view(final), "linked_without_post": False, "outcome": outcome}
        finally:
            _INFLIGHT.discard(intent_id)


def abandon(intent_id: int, preview_hash: str) -> dict:
    with _LOCK:
        st = _store()
        _apply_stale(st)
        row = st.intent(intent_id)
        if row is None:
            raise OrderError("NOT_FOUND", "No such paper order.", 404)
        if row["preview_hash"] != preview_hash:
            raise OrderError("PREVIEW_MISMATCH", "This is not the exact stored order.")
        if row["state"] not in (RU.SUBMIT_NOT_SENT, RU.RR):
            raise OrderError("NOT_ABANDONABLE", "Only an order that was not sent or needs reconciliation can be abandoned.")
        _require_configured()
        found, obj = _lookup(row)                                             # never abandon an order that exists
        now = _now()
        if found == "error":
            with st.write() as c:
                st.event(c, intent_id, "LOOKUP", now, code=f"ERROR_{obj}")
            raise OrderError("LOOKUP_FAILED", "The exact order lookup failed; nothing was changed.", 503)
        if found == "found":
            with st.write() as c:
                st.event(c, intent_id, "LOOKUP", now, code="FOUND")
            return {"intent": _view(_link(st, row, obj, now) if RU.consistent(obj, row) else _mismatch(st, row, now))}
        fields = _not_found_fields(row, now)
        if row["state"] == RU.RR and not _not_found_rule_met(fields, now):
            with st.write() as c:
                st.transition(c, intent_id, {RU.RR}, RU.RR, now, kind="LOOKUP", **fields)
            raise OrderError("NOT_FOUND_RULE", "Abandon needs at least 2 exact lookups finding nothing over at least 30 seconds.")
        with st.write() as c:
            st.transition(c, intent_id, {row["state"]}, RU.ABANDONED, now, kind="ABANDONED", **fields)
        return {"intent": _view(st.intent(intent_id))}


# ==================================================================================================================================
# status (exact lookups only)
# ==================================================================================================================================

def check_status() -> dict:
    with _LOCK:
        now = _now()
        last = _LAST_STATUS[0]
        if last is not None and (now - last).total_seconds() < RU.STATUS_SPACING_S:
            raise OrderError("STATUS_TOO_SOON", "Wait a few seconds between status checks.", 429)
        _LAST_STATUS[0] = now
        st = _store()
        if not st.exists():
            return {"checked": 0, "results": [], "intents": []}
        _require_configured()
        _apply_stale(st)
        rows = st.in_states(RU.LIVE | {RU.RR, RU.SUBMIT_NOT_SENT})[:RU.STATUS_MAX]
        results = []
        rd = _reader(RU.STATUS_MAX) if rows else None
        for row in rows:
            found, obj = _lookup(row, rd)
            t = _now()
            with st.write() as c:
                st.event(c, row["intent_id"], "LOOKUP", t, code=found.upper() if found != "error" else f"ERROR_{obj}")
            if found == "found":
                final = _link(st, row, obj, t) if RU.consistent(obj, row) else _mismatch(st, row, t)
            elif found == "not_found" and row["state"] in (RU.RR, RU.SUBMIT_NOT_SENT):
                with st.write() as c:
                    st.transition(c, row["intent_id"], {row["state"]}, row["state"], t, kind=False, **_not_found_fields(row, t))
                final = st.intent(row["intent_id"])
            else:
                final = row
            results.append({"intent_id": row["intent_id"], "result": found, "state": final["state"]})
        return {"checked": len(rows), "results": results, "intents": [_view(r) for r in st.intents(100)]}
