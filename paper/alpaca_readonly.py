"""
paper/alpaca_readonly.py — Stage 4.6A the ONE Alpaca PAPER trading adapter. STRICTLY READ-ONLY.

This is the only production module that imports Alpaca's TradingClient. It exposes four named reads and nothing else:

    PaperReader.get_account()              GET /v2/account
    PaperReader.get_positions()            GET /v2/positions
    PaperReader.get_recent_orders(limit)   GET /v2/orders            (one bounded page, newest first)
    PaperReader.get_recent_fills(limit)    GET /v2/account/activities (activity_types=FILL, one bounded page)

plus open_reader() (a reader, or None when the paper credentials are not configured) and connection_status()
(configuration only — no network). There is no generic call / request / raw-client accessor.

Paper-only by construction:
  * credentials come ONLY from ALPACA_PAPER_API_KEY / ALPACA_PAPER_SECRET_KEY — never the market-data pair, no fallback
  * the client is always TradingClient(key, secret, paper=True); no parameter, setting or environment variable can change
    it, and no base-URL override is used. Safety never depends on what a key looks like.
  * every HTTP request of a reader (the SDK's three reads and the activities read) passes through ONE transport that
    allows only GET, only https://paper-api.alpaca.markets, only the four read paths, at most once each, with a finite
    timeout. Anything else is refused BEFORE it is sent. The SDK's automatic retries are switched off, so one read is
    exactly one request (a refresh is at most 4 requests). Redirects are never followed.
Nothing is written anywhere: no database, no file. Secrets never appear in a return value, an exception or a log line.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import List, Optional
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

import config  # noqa: F401 - loads the local .env into the environment (only the two names below are read here)
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

log = logging.getLogger(__name__)

KEY_ENV = "ALPACA_PAPER_API_KEY"
SECRET_ENV = "ALPACA_PAPER_SECRET_KEY"
PAPER_HOST = "paper-api.alpaca.markets"
ACTIVITIES_URL = f"https://{PAPER_HOST}/v2/account/activities"
READ_PATHS = {"/v2/account": "account", "/v2/positions": "positions", "/v2/orders": "orders",
              "/v2/account/activities": "fills"}
ORDER_LIMIT = 100                      # one page of recent orders (never a history crawl)
FILL_LIMIT = 100                       # one page of recent FILL activities (Alpaca's page_size maximum)
TIMEOUT = (5.0, 10.0)                  # (connect, read) seconds for every request


class ReadOnlyViolation(RuntimeError):
    """A request outside the four approved paper reads — refused before anything was sent."""


class BrokerReadError(RuntimeError):
    """One read failed. `code` is a fixed identifier; no response text, URL or credential is ever carried."""
    CODES = ("AUTH_FAILED", "CONNECT_FAILED", "TIMEOUT", "SERVER_ERROR", "UNEXPECTED_RESPONSE", "BLOCKED")

    def __init__(self, code: str, http_status: Optional[int] = None):
        super().__init__(code if http_status is None else f"{code} (HTTP {http_status})")
        self.code, self.http_status = code, http_status


def _wire() -> HTTPAdapter:
    return HTTPAdapter(max_retries=0)


WIRE = _wire                           # the real network adapter factory (tests and the browser harness replace it)


class _PaperReadTransport(HTTPAdapter):
    """Every request of one reader passes here. Only one GET per approved read path on the paper host is ever sent."""

    def __init__(self, wire):
        super().__init__(max_retries=0)
        self._wire = wire
        self._sent: set = set()

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        parts = urlsplit(request.url)
        if request.method != "GET":
            raise ReadOnlyViolation("only GET requests are allowed")
        if parts.scheme != "https" or parts.hostname != PAPER_HOST or parts.port not in (None, 443):
            raise ReadOnlyViolation("only the Alpaca paper trading host is allowed")
        if parts.path not in READ_PATHS:
            raise ReadOnlyViolation("not one of the approved read paths")
        if parts.path in self._sent:
            raise ReadOnlyViolation("each read path is requested at most once per refresh")
        self._sent.add(parts.path)
        return self._wire.send(request, stream=stream, timeout=TIMEOUT, verify=True, cert=cert, proxies=proxies)

    def close(self):
        self._wire.close()


def connection_status() -> dict:
    """Whether both paper credentials are configured — names only, never values; no network."""
    missing = [n for n in (KEY_ENV, SECRET_ENV) if not os.environ.get(n)]
    return {"configured": not missing, "missing": missing, "credential_names": [KEY_ENV, SECRET_ENV], "mode": "PAPER"}


def open_reader() -> Optional["PaperReader"]:
    """A new reader for one refresh (its own request budget), or None when either paper credential is missing."""
    key, secret = os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)
    if not key or not secret:
        return None
    return PaperReader(key, secret)


def _mask_account_number(value) -> Optional[str]:
    s = str(value or "")
    return None if not s else "••••" + (s[-4:] if len(s) > 4 else "")


def _val(v):
    return getattr(v, "value", v)                          # SDK enums -> their plain string value


def _num(v) -> Optional[str]:
    if v is None or v == "":
        return None
    d = Decimal(str(_val(v)))
    if not d.is_finite():
        raise ValueError("not a finite number")
    return str(d)


def _time(v) -> Optional[str]:
    if v is None or v == "":
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _short(v) -> Optional[str]:
    return str(v)[:8] if v else None


def _norm_account(a) -> dict:
    return {"status": _val(a.status), "currency": a.currency, "cash": _num(a.cash), "equity": _num(a.equity),
            "portfolio_value": _num(a.portfolio_value), "buying_power": _num(a.buying_power),
            "long_market_value": _num(a.long_market_value), "short_market_value": _num(a.short_market_value),
            "trading_blocked": bool(a.trading_blocked), "account_blocked": bool(a.account_blocked),
            "created_at": _time(a.created_at), "account_number_masked": _mask_account_number(a.account_number)}


def _norm_position(p) -> dict:
    side = str(_val(p.side) or "long").lower()
    qty = abs(Decimal(_num(p.qty)))
    return {"symbol": str(p.symbol).upper(), "qty": str(-qty if side == "short" else qty), "side": side,
            "avg_entry_price": _num(p.avg_entry_price), "cost_basis": _num(p.cost_basis), "market_value": _num(p.market_value),
            "current_price": _num(p.current_price), "unrealized_pl": _num(p.unrealized_pl),
            "unrealized_plpc": _num(p.unrealized_plpc)}


def _norm_order(o) -> dict:
    return {"id_short": _short(o.id), "client_order_id": o.client_order_id, "symbol": o.symbol, "side": _val(o.side),
            "qty": _num(o.qty), "notional": _num(o.notional), "type": _val(o.type or o.order_type),
            "time_in_force": _val(o.time_in_force), "status": _val(o.status), "submitted_at": _time(o.submitted_at),
            "filled_at": _time(o.filled_at), "filled_qty": _num(o.filled_qty), "filled_avg_price": _num(o.filled_avg_price)}


def _norm_fill(a: dict) -> dict:
    if not isinstance(a, dict) or a.get("activity_type") != "FILL":
        raise ValueError("not a FILL activity")
    return {"time": _time(a.get("transaction_time")), "symbol": a.get("symbol"), "side": a.get("side"),
            "qty": _num(a.get("qty")), "price": _num(a.get("price")), "fill_type": a.get("type"),
            "order_ref": _short(a.get("order_id"))}


class PaperReader:
    """Four named reads of ONE Alpaca paper account for ONE refresh. Holds no public client accessor."""
    __slots__ = ("_client", "_http", "_headers")

    def __init__(self, key: str, secret: str):
        transport = _PaperReadTransport(WIRE())
        client = TradingClient(key, secret, paper=True)
        session = getattr(client, "_session", None)
        if not isinstance(session, requests.Session) or not isinstance(getattr(client, "_retry", None), int):
            raise ReadOnlyViolation("unexpected alpaca-py client layout; refusing to read without the guarded transport")
        client._retry = 0                                  # no automatic SDK retries: one read = one request
        session.mount("https://", transport)
        session.mount("http://", transport)
        http = requests.Session()
        http.mount("https://", transport)
        http.mount("http://", transport)
        self._client, self._http = client, http
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}

    def __repr__(self) -> str:
        return "<Alpaca paper reader (read-only)>"

    @staticmethod
    def _guard(name: str, fn):
        try:
            return fn()
        except BrokerReadError:
            raise
        except ReadOnlyViolation:
            log.error("alpaca paper %s read refused by the read-only transport", name)
            raise BrokerReadError("BLOCKED") from None
        except APIError as exc:
            status = _status_of(exc)
            raise BrokerReadError(_http_code(status), status) from None
        except requests.exceptions.ConnectTimeout:
            raise BrokerReadError("CONNECT_FAILED") from None
        except requests.exceptions.Timeout:
            raise BrokerReadError("TIMEOUT") from None
        except requests.exceptions.ConnectionError:
            raise BrokerReadError("CONNECT_FAILED") from None
        except Exception as exc:  # noqa: BLE001 - an unreadable answer; the type name only (never its text)
            log.warning("alpaca paper %s read: unexpected response (%s)", name, type(exc).__name__)
            raise BrokerReadError("UNEXPECTED_RESPONSE") from None

    def get_account(self) -> dict:
        return self._guard("account", lambda: _norm_account(self._client.get_account()))

    def get_positions(self) -> List[dict]:
        return self._guard("positions", lambda: sorted((_norm_position(p) for p in self._client.get_all_positions()),
                                                       key=lambda p: p["symbol"]))

    def get_recent_orders(self, limit: int = ORDER_LIMIT) -> List[dict]:
        n = max(1, min(int(limit), ORDER_LIMIT))
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=n)
        return self._guard("orders", lambda: [_norm_order(o) for o in self._client.get_orders(filter=req)][:n])

    def get_recent_fills(self, limit: int = FILL_LIMIT) -> List[dict]:
        n = max(1, min(int(limit), FILL_LIMIT))

        def read():
            r = self._http.get(ACTIVITIES_URL, params={"activity_types": "FILL", "direction": "desc", "page_size": n},
                               headers=self._headers, allow_redirects=False, timeout=TIMEOUT)
            if r.status_code != 200:
                raise BrokerReadError(_http_code(r.status_code), r.status_code)
            rows = r.json()
            if not isinstance(rows, list):
                raise ValueError("activities answer is not a list")
            return [_norm_fill(a) for a in rows[:n]]
        return self._guard("fills", read)


def _status_of(exc: APIError) -> Optional[int]:
    try:
        return int(exc.status_code)
    except Exception:  # noqa: BLE001
        return None


def _http_code(status: Optional[int]) -> str:
    if status in (401, 403):
        return "AUTH_FAILED"
    if status is not None and (status == 429 or status >= 500):
        return "SERVER_ERROR"
    return "UNEXPECTED_RESPONSE"
