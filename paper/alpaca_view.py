"""
paper/alpaca_view.py — Stage 4.6A ALPACA PAPER · READ ONLY: connection status, the explicit refresh, the in-memory
snapshot of the last refresh, and the observational comparison with the LOCAL simulator (paper/reconcile.py).

  * Broker reads happen ONLY in refresh(), which only the explicit "Refresh Alpaca Paper" action calls — never at startup,
    never when a view opens, never on a timer. status() and view() make 0 broker calls.
  * One refresh = at most 4 requests (account, positions, recent orders, recent FILL activities), one attempt each.
    An authentication or connection failure stops the remaining reads (they would fail the same way).
  * Single flight: a refresh requested while another is running joins it instead of reading again.
  * Partial failure keeps every successful read (PARTIAL DATA).
  * The snapshot lives in memory only (lost on restart). Nothing is written: no database, no local-ledger change, no
    broker action. The Stage 4.5 local view is read (read-only) only to compare.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from paper import alpaca_readonly as R
from paper import portfolio as P
from paper import reconcile as RC

log = logging.getLogger(__name__)

LABEL = "ALPACA PAPER · READ ONLY · PAPER ACCOUNT · NO BROKER ACTIONS"
NOTE = ("A read-only view of your Alpaca PAPER trading account (simulated money at Alpaca, not a real portfolio). "
        "Stock Agent never submits, changes, cancels or closes anything there, and never copies it into the local simulator.")
STATES = {"NOT_CONFIGURED": "Not configured", "READY": "Ready", "REFRESHING": "Refreshing", "CONNECTED": "Connected",
          "AUTH_ERROR": "Auth error", "PAPER_API_UNAVAILABLE": "Paper API unavailable", "PARTIAL_DATA": "Partial data",
          "ERROR": "Error"}
MESSAGES = {"NOT_CONFIGURED": "Alpaca Paper is not configured. Add {} and {} to the local .env file, then restart the app.",
            "READY": "Configured. Press Refresh Alpaca Paper to read the paper account.",
            "REFRESHING": "Reading the Alpaca paper account…",
            "CONNECTED": "All four read-only requests succeeded.",
            "AUTH_ERROR": "Alpaca Paper authentication failed.",
            "PAPER_API_UNAVAILABLE": "The Alpaca paper API could not be reached. Nothing was changed; try Refresh later.",
            "PARTIAL_DATA": "Some reads failed; the sections that were read are shown.",
            "ERROR": "The Alpaca paper account could not be read."}
READS = ("account", "positions", "orders", "fills")
SECTION_TEXT = {"AUTH_FAILED": "Alpaca Paper authentication failed.",
                "CONNECT_FAILED": "The Alpaca paper API could not be reached.",
                "TIMEOUT": "The Alpaca paper {} read timed out.",
                "SERVER_ERROR": "The Alpaca paper API returned an error for {}.",
                "UNEXPECTED_RESPONSE": "The Alpaca paper {} answer could not be read.",
                "BLOCKED": "The {} read was refused by the read-only guard.",
                "SKIPPED": "Not requested after the earlier failure (no retry, no other endpoint).",
                "READER_UNAVAILABLE": "The read-only Alpaca paper reader could not be created."}
STOP_CODES = ("AUTH_FAILED", "CONNECT_FAILED")
JOIN_TIMEOUT_S = 4 * sum(R.TIMEOUT) + 5.0          # a joined refresh never waits longer than one full refresh can take

READER: Callable = R.open_reader                   # replaced by a fake reader in tests and the browser harness
_LOCK = threading.Lock()
_STATE = {"snapshot": None, "inflight": None}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _reset() -> None:
    """Tests only: forget the in-memory snapshot."""
    with _LOCK:
        _STATE.update(snapshot=None, inflight=None)


def _connection(state: str, cfg: dict) -> dict:
    msg = MESSAGES[state].format(*cfg["credential_names"]) if state == "NOT_CONFIGURED" else MESSAGES[state]
    return {"state": state, "label": STATES[state], "configured": cfg["configured"], "credential_names": cfg["credential_names"],
            "missing": cfg["missing"], "mode": "PAPER", "message": msg}


def status() -> dict:
    """Configuration and the last refresh's state — 0 broker calls."""
    cfg = R.connection_status()
    with _LOCK:
        snap, busy = _STATE["snapshot"], _STATE["inflight"] is not None
    state = "NOT_CONFIGURED" if not cfg["configured"] else "REFRESHING" if busy else snap["state"] if snap else "READY"
    return {"label": LABEL, "connection": _connection(state, cfg), "refreshed_at": snap["refreshed_at"] if snap and cfg["configured"] else None,
            "in_flight": busy}


def _read_all() -> dict:
    """The refresh itself: up to four reads, one attempt each, in a fixed order."""
    t_all = time.perf_counter()
    sections = {n: {"status": "NOT_REQUESTED", "code": None, "http_status": None, "ms": None, "count": None} for n in READS}
    data = {n: None for n in READS}
    try:
        reader = READER()
    except Exception as exc:  # noqa: BLE001 - the reader could not be built (e.g. an unexpected SDK layout): nothing was sent
        log.error("alpaca paper reader unavailable (%s)", type(exc).__name__)
        reader = None
        for n in READS:
            sections[n].update(status="FAILED", code="READER_UNAVAILABLE")
    if reader is not None:
        calls = {"account": reader.get_account, "positions": reader.get_positions,
                 "orders": lambda: reader.get_recent_orders(R.ORDER_LIMIT), "fills": lambda: reader.get_recent_fills(R.FILL_LIMIT)}
        stop = None
        for n in READS:
            if stop:
                sections[n].update(status="SKIPPED", code="SKIPPED")
                continue
            t0 = time.perf_counter()
            try:
                data[n] = calls[n]()
                sections[n].update(status="OK", count=None if n == "account" else len(data[n]))
            except R.BrokerReadError as exc:
                sections[n].update(status="FAILED", code=exc.code, http_status=exc.http_status)
                log.warning("alpaca paper %s read failed: %s", n, exc.code)
                if exc.code in STOP_CODES:
                    stop = exc.code
            except Exception as exc:  # noqa: BLE001 - a reader bug is reported, never raised to the page
                sections[n].update(status="FAILED", code="UNEXPECTED_RESPONSE")
                log.error("alpaca paper %s read: unexpected error (%s)", n, type(exc).__name__)
            sections[n]["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    ok = [n for n in READS if sections[n]["status"] == "OK"]
    codes = {sections[n]["code"] for n in READS if sections[n]["status"] != "OK"}
    if len(ok) == len(READS):
        state = "CONNECTED"
    elif ok:
        state = "PARTIAL_DATA"
    elif "AUTH_FAILED" in codes:
        state = "AUTH_ERROR"
    elif codes and codes <= {"CONNECT_FAILED", "TIMEOUT", "SERVER_ERROR", "SKIPPED"}:
        state = "PAPER_API_UNAVAILABLE"
    else:
        state = "ERROR"
    return {"state": state, "refreshed_at": _utcnow().isoformat(timespec="seconds"), "sections": sections, "data": data,
            "broker_total_ms": round((time.perf_counter() - t_all) * 1000, 1)}


def refresh() -> dict:
    """The ONLY broker-reading entry point (explicit user action). Returns the full payload."""
    cfg = R.connection_status()
    if not cfg["configured"]:
        return _payload(None, cfg)                            # 0 network calls, no reader, nothing remembered
    with _LOCK:
        ev = _STATE["inflight"]
        owner = ev is None
        if owner:
            ev = _STATE["inflight"] = threading.Event()
    if not owner:                                             # a refresh is already running: join it, read nothing
        ev.wait(JOIN_TIMEOUT_S)
        return view(joined=True)
    try:
        snap = _read_all()
        with _LOCK:
            _STATE["snapshot"] = snap
    finally:
        with _LOCK:
            _STATE["inflight"] = None
        ev.set()
    return _payload(snap, cfg)


def view(*, joined: bool = False) -> dict:
    """The last refresh (from memory) compared with the CURRENT local simulator — 0 broker calls."""
    cfg = R.connection_status()
    with _LOCK:
        snap, busy = _STATE["snapshot"], _STATE["inflight"] is not None
    return _payload(snap if cfg["configured"] else None, cfg, in_flight=busy, joined=joined)


def _warnings(snap: dict) -> list:
    out = []
    for n in READS:
        s = snap["sections"][n]
        if s["status"] in ("FAILED", "SKIPPED"):
            text = SECTION_TEXT.get(s["code"], SECTION_TEXT["UNEXPECTED_RESPONSE"]).format(n)
            out.append({"section": n, "code": s["code"], "http_status": s["http_status"], "text": text})
    return out


def _payload(snap: Optional[dict], cfg: dict, *, in_flight: bool = False, joined: bool = False) -> dict:
    if not cfg["configured"]:
        state = "NOT_CONFIGURED"
    elif in_flight:
        state = "REFRESHING"
    else:
        state = snap["state"] if snap else "READY"
    base = {"label": LABEL, "note": NOTE, "connection": _connection(state, cfg), "in_flight": in_flight, "joined": joined,
            "refreshed_at": snap["refreshed_at"] if snap else None, "last_result": snap["state"] if snap else None,
            "account": None, "positions": [], "orders": [], "fills": [], "sections": snap["sections"] if snap else None,
            "reconciliation": None, "warnings": [], "timings": {},
            "source": {"broker": f"Alpaca paper trading API ({R.PAPER_HOST}) · read only",
                       "local": "Stage 4.5 local simulator on this computer", "limits": {"orders": R.ORDER_LIMIT, "fills": R.FILL_LIMIT},
                       "requests_per_refresh": len(READS)}}
    if not snap:
        return base
    d = snap["data"]
    t0 = time.perf_counter()
    try:
        local = P.view()                                      # Stage 4.5 view — read-only
    except Exception as exc:  # noqa: BLE001 - the broker data is still shown; no comparison without the local side
        log.error("local paper view unavailable for the comparison (%s)", type(exc).__name__)
        local = None
    local_ms = round((time.perf_counter() - t0) * 1000, 1)
    t1 = time.perf_counter()
    rec = RC.reconcile(local, d, alpaca_as_of=snap["refreshed_at"]) if local is not None else None
    rows = {r["symbol"]: r for r in rec["positions"]["rows"]} if rec and rec["positions"]["available"] else {}
    positions = [{**p, "local_shares": (rows.get(p["symbol"]) or {}).get("local_shares"),
                  "status": (rows.get(p["symbol"]) or {}).get("status")} for p in (d["positions"] or [])]
    orders = [{**o, "link": RC.NOT_LINKED} for o in (d["orders"] or [])]
    fills = [{**f, "link": RC.NOT_LINKED} for f in (d["fills"] or [])]
    rec_ms = round((time.perf_counter() - t1) * 1000, 1)
    warnings = _warnings(snap)
    if local is None:
        warnings.append({"section": "local", "code": "LOCAL_UNAVAILABLE", "http_status": None,
                         "text": "The local simulator could not be read, so no comparison is shown."})
    timings = {f"{n}_ms": snap["sections"][n]["ms"] for n in READS}
    timings.update(broker_total_ms=snap["broker_total_ms"], local_ms=local_ms, reconcile_ms=rec_ms)
    return {**base, "account": d["account"], "positions": positions, "orders": orders, "fills": fills,
            "reconciliation": rec, "warnings": warnings, "timings": timings,
            "local_simulator": {"exists": bool(local and local.get("account")),
                                "mark_session": ((local or {}).get("summary") or {}).get("mark_session")}}
