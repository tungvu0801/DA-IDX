"""Stage 4.6B test infrastructure: an in-memory FAKE Alpaca paper broker that sits BELOW the real 4.6B transports (it is a
requests adapter). It answers the exact GETs of paper/alpaca_order_reads.py and the POST /v2/orders of
paper/alpaca_order_writer.py, enforces client_order_id uniqueness like Alpaca, and records every request. Nothing here
opens a socket. Used by tests/test_alpaca_orders_46b.py and the browser harness."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import requests
from requests.adapters import BaseAdapter
from urllib3.exceptions import MaxRetryError, NewConnectionError

ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ACCOUNT_ID = "99999999-8888-7777-6666-555555555555"


def iso(dt: datetime) -> str:
    return dt.isoformat()


class FakeAlpaca(BaseAdapter):
    """POST modes (consumed one per POST; default "accept"):
      accept[:status]      store the order, 200 with the order (status default "accepted")
      timeout_after_record store the order, then raise ReadTimeout (the broker got it; the answer was lost)
      timeout_before       raise ReadTimeout without storing
      connect_timeout      raise ConnectTimeout (nothing sent)
      dns                  raise ConnectionError(NewConnectionError) (nothing sent)
      reset_after_record   store, then raise ConnectionError (connection reset after sending)
      http:<status>[:json] return that status; body json given or a generic error; stores nothing
      http_after_record:<status>  store, then return that status
      garbage200 / garbage200_after_record   200 with an unparseable body
      inconsistent200      200 with an order whose qty differs (not stored)
    Duplicate client_order_id → 422 {"code": 40010001, "message": "client_order_id must be unique"} (like Alpaca)."""

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.account = {"id": ACCOUNT_ID, "account_number": "PA3FAKE7890", "status": "ACTIVE", "currency": "USD",
                        "cash": "25000.00", "buying_power": "50000.00", "trading_blocked": False, "account_blocked": False,
                        "trade_suspended_by_user": False, "equity": "25000.00"}
        self.config = {"no_shorting": True, "dtbp_check": "entry", "pdt_check": "entry", "suspend_trade": False,
                       "trade_confirm_email": "all", "fractional_trading": True, "max_margin_multiplier": "4"}
        self.assets = {s: {"id": str(uuid.uuid4()), "class": "us_equity", "exchange": "NASDAQ", "symbol": s, "status": "active",
                           "tradable": True, "shortable": True, "fractionable": True}
                       for s in ("AMD", "MU", "CLS", "KO", "SPY", "BRK.B")}
        self.positions = {}                                    # symbol -> {qty, qty_available}
        self.orders = {}                                       # alpaca id -> order json
        self.now = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)   # 10:00 New York
        self.is_open = True
        self.next_close = datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc)
        self.next_open = datetime(2026, 9, 30, 13, 30, tzinfo=timezone.utc)
        self.post_modes: list = []
        self.lookup_lag = 0                                     # lookups answering 404 although the order exists
        self.lookup_fail = 0                                    # lookups raising a connection error
        self.post_delay = 0.0
        self.on_post = None                                     # callback(request) at send time
        self.requests: list = []                                # (method, path, query)
        self.posts: list = []                                   # raw POST bodies (bytes) that reached the broker
        self.headers_seen: list = []

    # ---- helpers ------------------------------------------------------------------------------------------------------
    def set_position(self, sym, qty, available=None):
        self.positions[sym] = {"qty": str(qty), "qty_available": str(qty if available is None else available)}

    def order_by_coid(self, coid):
        return next((o for o in self.orders.values() if o["client_order_id"] == coid), None)

    def _resp(self, request, status, body):
        r = requests.Response()
        r.status_code, r.url, r.request = status, request.url, request
        r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        r.headers["Content-Type"] = "application/json"
        return r

    def _order_json(self, payload, status="accepted"):
        oid = str(uuid.uuid4())
        t = iso(self.now)
        return {"id": oid, "client_order_id": payload["client_order_id"], "created_at": t, "updated_at": t, "submitted_at": t,
                "filled_at": None, "symbol": payload["symbol"], "asset_class": "us_equity", "qty": payload["qty"],
                "filled_qty": "0", "filled_avg_price": None, "order_class": "", "order_type": payload["type"],
                "type": payload["type"], "side": payload["side"], "time_in_force": payload["time_in_force"],
                "status": status, "extended_hours": False, "notional": None, "limit_price": None, "stop_price": None}

    # ---- the wire ------------------------------------------------------------------------------------------------------
    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        u = urlsplit(request.url)
        path, q = unquote(u.path), parse_qs(u.query)
        with self.lock:
            self.requests.append((request.method, path, u.query))
            self.headers_seen.append(dict(request.headers))
        if request.method == "POST":
            return self._post(request)
        if request.method != "GET":
            return self._resp(request, 405, {"code": 40500000, "message": "method not allowed"})
        if path == "/v2/account":
            return self._resp(request, 200, self.account)
        if path == "/v2/account/configurations":
            return self._resp(request, 200, self.config)
        if path == "/v2/clock":
            return self._resp(request, 200, {"timestamp": iso(self.now), "is_open": self.is_open,
                                             "next_open": iso(self.next_open), "next_close": iso(self.next_close)})
        if path.startswith("/v2/assets/"):
            a = self.assets.get(path.rsplit("/", 1)[1])
            return self._resp(request, 200, a) if a else self._resp(request, 404, {"code": 40410000, "message": "asset not found"})
        if path.startswith("/v2/positions/"):
            sym = path.rsplit("/", 1)[1]
            p = self.positions.get(sym)
            if not p:
                return self._resp(request, 404, {"code": 40410000, "message": "position does not exist"})
            return self._resp(request, 200, {"asset_id": "x", "symbol": sym, "exchange": "NASDAQ", "asset_class": "us_equity",
                                             "qty": p["qty"], "qty_available": p["qty_available"], "side": "long",
                                             "avg_entry_price": "100", "market_value": "1000", "cost_basis": "1000"})
        if path == "/v2/orders:by_client_order_id" or path.startswith("/v2/orders/"):
            if self.lookup_fail:
                self.lookup_fail -= 1
                raise requests.exceptions.ConnectionError("lookup failed (fake)")
            o = (self.order_by_coid((q.get("client_order_id") or [""])[0]) if path.endswith(":by_client_order_id")
                 else self.orders.get(path.rsplit("/", 1)[1]))
            if o is None or self.lookup_lag > 0:
                self.lookup_lag = max(0, self.lookup_lag - 1)
                return self._resp(request, 404, {"code": 40410000, "message": "order not found"})
            return self._resp(request, 200, o)
        return self._resp(request, 404, {"code": 40410000, "message": "not found"})

    def _post(self, request):
        if self.on_post:
            self.on_post(request)
        if self.post_delay:
            time.sleep(self.post_delay)
        mode = self.post_modes.pop(0) if self.post_modes else "accept"
        if mode == "connect_timeout":
            raise requests.exceptions.ConnectTimeout("connect timed out (fake)")
        if mode == "dns":
            raise requests.exceptions.ConnectionError(MaxRetryError(None, request.url, NewConnectionError(None, "name resolution failed")))
        with self.lock:
            self.posts.append(request.body)
        payload = json.loads(request.body)
        if self.order_by_coid(payload["client_order_id"]) is not None:
            return self._resp(request, 422, {"code": 40010001, "message": "client_order_id must be unique"})
        kind, _, arg = mode.partition(":")
        if kind == "accept":
            o = self._order_json(payload, arg or "accepted")
            self.orders[o["id"]] = o
            return self._resp(request, 200, o)
        if kind in ("timeout_after_record", "reset_after_record", "garbage200_after_record", "http_after_record"):
            o = self._order_json(payload)
            self.orders[o["id"]] = o
            if kind == "timeout_after_record":
                raise requests.exceptions.ReadTimeout("read timed out (fake)")
            if kind == "reset_after_record":
                raise requests.exceptions.ConnectionError("connection reset by peer (fake)")
            if kind == "garbage200_after_record":
                return self._resp(request, 200, b"<html>gateway</html>")
            return self._resp(request, int(arg), {"code": 50000000, "message": "internal error"})
        if kind == "timeout_before":
            raise requests.exceptions.ReadTimeout("read timed out (fake)")
        if kind == "garbage200":
            return self._resp(request, 200, b"not json")
        if kind == "inconsistent200":
            return self._resp(request, 200, {**self._order_json(payload), "qty": str(int(payload["qty"]) + 1)})
        if kind == "http":
            status, _, body = arg.partition(":")
            return self._resp(request, int(status), body.encode() if body else {"code": 40000000, "message": "error"})
        raise AssertionError(f"unknown fake POST mode {mode}")

    def close(self):
        pass

    # ---- test conveniences --------------------------------------------------------------------------------------------
    def gets(self):
        return [p for m, p, _ in self.requests if m == "GET"]

    def set_status(self, coid, status, filled_qty=None, filled_avg_price=None):
        o = self.order_by_coid(coid)
        o["status"] = status
        if filled_qty is not None:
            o["filled_qty"], o["filled_avg_price"] = str(filled_qty), filled_avg_price
        return o

    def minutes_to_close(self, minutes: float):
        self.next_close = self.now + timedelta(minutes=minutes)
