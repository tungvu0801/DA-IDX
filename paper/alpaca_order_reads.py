"""
paper/alpaca_order_reads.py — Stage 4.6B PAPER-READ adapter for manual paper orders. GET only.

The only requests this module can send (each ONE attempt, finite timeout, no retries, no redirects, a per-operation request
budget) — to https://paper-api.alpaca.markets only, authenticated with ALPACA_PAPER_API_KEY / ALPACA_PAPER_SECRET_KEY only:

    GET /v2/account                                       (status, cash, blocks; the id only as a sha256 fingerprint)
    GET /v2/account/configurations                        (no_shorting — READ ONLY; never changed by Stock Agent)
    GET /v2/assets/{SYMBOL}
    GET /v2/clock
    GET /v2/positions/{SYMBOL}                            (404 = no position)
    GET /v2/orders:by_client_order_id?client_order_id=…   (exact lookup of a stored sa46b-… id)
    GET /v2/orders/{uuid}                                 (exact lookup of a stored Alpaca order id)

Anything else — another verb, host, scheme, port, path, query, or a request beyond the budget — is refused BEFORE it is
sent. No alpaca-py (its REST client has no timeout and retries automatically). No credential, header, full account number
or raw broker text ever leaves this module.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import HTTPAdapter

import config  # noqa: F401 - loads the local .env into the environment (only the two names below are read here)
from paper import alpaca_order_rules as RU

log = logging.getLogger(__name__)

KEY_ENV = "ALPACA_PAPER_API_KEY"
SECRET_ENV = "ALPACA_PAPER_SECRET_KEY"
PAPER_HOST = "paper-api.alpaca.markets"
BASE = f"https://{PAPER_HOST}"
TIMEOUT = (5.0, 10.0)                           # (connect, read) seconds
_EXACT = {"/v2/account", "/v2/account/configurations", "/v2/clock"}
_SYM = r"[A-Z]{1,5}(?:\.[A-Z]{1,2})?"
_ASSET = re.compile(rf"^/v2/assets/{_SYM}$")
_POSITION = re.compile(rf"^/v2/positions/{_SYM}$")
_ORDER_ID = re.compile(r"^/v2/orders/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_BY_COID = "/v2/orders:by_client_order_id"


class ReadRefused(RuntimeError):
    """A request outside the exact paper GET allow-list (or beyond the budget) — refused before anything was sent."""


class ReadError(RuntimeError):
    """One read failed. `code` is fixed; no response text, URL or credential is ever carried."""

    def __init__(self, code: str, http_status: Optional[int] = None):
        super().__init__(code if http_status is None else f"{code} (HTTP {http_status})")
        self.code, self.http_status = code, http_status


class NotConfigured(RuntimeError):
    pass


def _wire() -> HTTPAdapter:
    return HTTPAdapter(max_retries=0)


WIRE = _wire                                    # the real network adapter factory (tests / the harness replace it)


class _ReadTransport(HTTPAdapter):
    def __init__(self, wire, budget: int):
        super().__init__(max_retries=0)
        self._wire, self._left = wire, int(budget)

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        u = urlsplit(request.url)
        if request.method != "GET":
            raise ReadRefused("only GET requests are allowed")
        if u.scheme != "https" or u.hostname != PAPER_HOST or u.port not in (None, 443):
            raise ReadRefused("only the Alpaca paper trading host is allowed")
        path = u.path
        if path == _BY_COID:
            q = parse_qs(u.query, keep_blank_values=True)
            if set(q) != {"client_order_id"} or len(q["client_order_id"]) != 1 or not RU.COID_RE.match(q["client_order_id"][0]):
                raise ReadRefused("only an exact lookup of a Stock Agent client order id is allowed")
        elif u.query or not (path in _EXACT or _ASSET.match(path) or _POSITION.match(path) or _ORDER_ID.match(path)):
            raise ReadRefused("not one of the exact read paths")
        if self._left <= 0:
            raise ReadRefused("request budget of this operation exhausted")
        self._left -= 1
        return self._wire.send(request, stream=stream, timeout=TIMEOUT, verify=True, cert=cert, proxies=proxies)

    def close(self):
        self._wire.close()


def configured() -> dict:
    missing = [n for n in (KEY_ENV, SECRET_ENV) if not os.environ.get(n)]
    return {"configured": not missing, "missing": missing, "credential_names": [KEY_ENV, SECRET_ENV]}


class PaperOrderReader:
    """Exact paper GETs for ONE operation (its own request budget). Returns normalized dicts; raises ReadError."""
    __slots__ = ("_http", "_headers")

    def __init__(self, budget: int):
        key, secret = os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)
        if not key or not secret:
            raise NotConfigured("paper credentials are not configured")
        http = requests.Session()
        t = _ReadTransport(WIRE(), budget)
        http.mount("https://", t)
        http.mount("http://", t)
        self._http = http
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}

    def __repr__(self) -> str:
        return "<Alpaca paper order reader (GET only)>"

    def _get(self, path: str, params=None, allow_404: bool = False):
        try:
            r = self._http.get(BASE + path, params=params, headers=self._headers, allow_redirects=False, timeout=TIMEOUT)
        except ReadRefused as exc:
            raise ReadError("BUDGET" if "budget" in str(exc) else "BLOCKED") from None
        except requests.exceptions.ConnectTimeout:
            raise ReadError("CONNECT_FAILED") from None
        except requests.exceptions.Timeout:
            raise ReadError("TIMEOUT") from None
        except requests.exceptions.ConnectionError:
            raise ReadError("CONNECT_FAILED") from None
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code != 200:
            code = ("AUTH_FAILED" if r.status_code in (401, 403) else
                    "SERVER_ERROR" if r.status_code == 429 or r.status_code >= 500 else "UNEXPECTED_RESPONSE")
            raise ReadError(code, r.status_code)
        try:
            data = r.json()
        except ValueError:
            raise ReadError("UNEXPECTED_RESPONSE", r.status_code) from None
        if not isinstance(data, dict):
            raise ReadError("UNEXPECTED_RESPONSE", r.status_code)
        return data

    def _norm(self, fn, data):
        try:
            return fn(data)
        except (KeyError, TypeError, ValueError):
            raise ReadError("UNEXPECTED_RESPONSE") from None

    def account(self) -> dict:
        return self._norm(lambda a: {
            "fingerprint": RU.fingerprint(a["id"]), "masked": RU.mask(a.get("account_number")), "status": a.get("status"),
            "currency": a.get("currency"), "cash": str(RU.dec(a.get("cash"))) if a.get("cash") is not None else None,
            "trading_blocked": bool(a.get("trading_blocked")), "account_blocked": bool(a.get("account_blocked")),
            "trade_suspended_by_user": bool(a.get("trade_suspended_by_user"))}, self._get("/v2/account"))

    def configurations(self) -> dict:
        return self._norm(lambda c: {"no_shorting": c.get("no_shorting") is True}, self._get("/v2/account/configurations"))

    def asset(self, symbol: str) -> Optional[dict]:
        a = self._get(f"/v2/assets/{symbol}", allow_404=True)
        return None if a is None else self._norm(lambda x: {
            "symbol": x.get("symbol"), "class": x.get("class") or x.get("asset_class"), "exchange": x.get("exchange"),
            "status": x.get("status"), "tradable": x.get("tradable") is True}, a)

    def clock(self) -> dict:
        return self._norm(lambda c: {"timestamp": str(c["timestamp"]), "is_open": c.get("is_open") is True,
                                     "next_open": str(c.get("next_open")), "next_close": str(c["next_close"])},
                          self._get("/v2/clock"))

    def position(self, symbol: str) -> Optional[dict]:
        p = self._get(f"/v2/positions/{symbol}", allow_404=True)
        return None if p is None else self._norm(lambda x: {
            "symbol": x.get("symbol"), "side": str(x.get("side") or "long").lower(), "qty": RU.qty_str(RU.dec(x.get("qty"))),
            "qty_available": RU.qty_str(RU.dec(x.get("qty_available") if x.get("qty_available") is not None else x.get("qty")))}, p)

    def order_by_client_id(self, client_order_id: str) -> Optional[dict]:
        return self._order(self._get(_BY_COID, params={"client_order_id": client_order_id}, allow_404=True))

    def order_by_id(self, order_id: str) -> Optional[dict]:
        return self._order(self._get(f"/v2/orders/{order_id}", allow_404=True))

    @staticmethod
    def _order(o) -> Optional[dict]:
        if o is None:
            return None
        parsed = RU.parse_order(RU.canonical_json(o))
        if parsed is None:
            raise ReadError("UNEXPECTED_RESPONSE")
        return parsed
