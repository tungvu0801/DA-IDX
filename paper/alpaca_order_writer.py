"""
paper/alpaca_order_writer.py — Stage 4.6B THE ONLY BROKER WRITE in Stock Agent: one POST /v2/orders to the Alpaca PAPER
host per attempt.

  submit(payload_bytes, client_order_id) -> result
      result = {"kind": "response", "status", "body", "latency_ms"}      the broker answered (any status)
             | {"kind": "not_sent", "reason"}                            nothing left the machine (DNS / refused / connect
                                                                         timeout / local refusal)
             | {"kind": "uncertain", "reason"}                           the request may have reached Alpaca (read timeout,
                                                                         broken or reset connection, TLS error, …)
Guarantees: host https://paper-api.alpaca.markets only (constant; no environment switch, no override), path exactly
/v2/orders, body bytes == the stored canonical payload (whose client_order_id must match), ONE request per call (the
transport refuses a second), finite timeouts, no automatic retry (urllib3 max_retries 0; no SDK), no redirects, credentials
ALPACA_PAPER_* only and never returned or logged. It refuses to run unless the service set the confirm / retry scope.
It is imported only by paper/alpaca_orders.py.
"""
from __future__ import annotations

import contextvars
import json
import os
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import NewConnectionError

from paper import alpaca_order_rules as RU

KEY_ENV = "ALPACA_PAPER_API_KEY"
SECRET_ENV = "ALPACA_PAPER_SECRET_KEY"
PAPER_HOST = "paper-api.alpaca.markets"
ORDERS_URL = f"https://{PAPER_HOST}/v2/orders"
TIMEOUT = (5.0, 15.0)                               # (connect, read) seconds
MAX_BODY = 65536
_SCOPE: contextvars.ContextVar = contextvars.ContextVar("alpaca_paper_order_scope", default=None)


class WriteRefused(RuntimeError):
    """Refused before anything was sent."""


def _wire() -> HTTPAdapter:
    return HTTPAdapter(max_retries=0)


WIRE = _wire                                        # the real network adapter factory (tests / the harness replace it)


class _WriteTransport(HTTPAdapter):
    """Exactly one POST of exactly `expected_body` to exactly the paper /v2/orders URL; everything else is refused."""

    def __init__(self, wire, expected_body: bytes):
        super().__init__(max_retries=0)
        self._wire, self._body, self._used = wire, expected_body, False

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        u = urlsplit(request.url)
        if request.method != "POST":
            raise WriteRefused("only POST is allowed")
        if u.scheme != "https" or u.hostname != PAPER_HOST or u.port not in (None, 443) or u.path != "/v2/orders" or u.query:
            raise WriteRefused("only the Alpaca paper /v2/orders URL is allowed")
        if request.body != self._body:
            raise WriteRefused("the body is not the stored payload")
        if self._used:
            raise WriteRefused("one request per attempt")
        self._used = True
        return self._wire.send(request, stream=stream, timeout=TIMEOUT, verify=True, cert=cert, proxies=proxies)

    def close(self):
        self._wire.close()


def _not_sent_connection_error(exc: requests.exceptions.ConnectionError) -> bool:
    """A connection that was never established (DNS failure, refused) — nothing was sent. Anything else is uncertain."""
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    return isinstance(reason, NewConnectionError)


def submit(payload_bytes: bytes, client_order_id: str) -> dict:
    if _SCOPE.get() is None:
        raise WriteRefused("outside the explicit confirm / retry scope")
    if not isinstance(payload_bytes, bytes) or not RU.COID_RE.match(client_order_id or ""):
        raise WriteRefused("invalid payload or client order id")
    try:
        body = json.loads(payload_bytes)
    except ValueError:
        raise WriteRefused("the payload is not JSON") from None
    if body.get("client_order_id") != client_order_id or payload_bytes != RU.canonical_payload(
            client_order_id, body.get("symbol", ""), body.get("side", ""), int(body.get("qty", 0))):
        raise WriteRefused("the payload is not the canonical payload of this client order id")
    key, secret = os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)
    if not key or not secret:
        return {"kind": "not_sent", "reason": "NOT_SENT"}
    transport = _WriteTransport(WIRE(), payload_bytes)
    http = requests.Session()
    http.mount("https://", transport)
    http.mount("http://", transport)
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Content-Type": "application/json",
               "Accept": "application/json"}
    t0 = time.perf_counter()
    try:
        r = http.post(ORDERS_URL, data=payload_bytes, headers=headers, allow_redirects=False, timeout=TIMEOUT)
    except WriteRefused:
        return {"kind": "not_sent", "reason": "LOCAL_REFUSAL"}
    except requests.exceptions.ConnectTimeout:
        return {"kind": "not_sent", "reason": "NOT_SENT"}
    except requests.exceptions.SSLError:
        return {"kind": "uncertain", "reason": "CONNECTION"}
    except requests.exceptions.Timeout:
        return {"kind": "uncertain", "reason": "TIMEOUT"}
    except requests.exceptions.ConnectionError as exc:
        return {"kind": "not_sent", "reason": "NOT_SENT"} if _not_sent_connection_error(exc) else \
            {"kind": "uncertain", "reason": "CONNECTION"}
    except Exception:  # noqa: BLE001 - anything unexpected after the request may have left: uncertain (never raw text)
        return {"kind": "uncertain", "reason": "CONNECTION"}
    finally:
        http.close()
    return {"kind": "response", "status": r.status_code, "body": (r.content or b"")[:MAX_BODY],
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
