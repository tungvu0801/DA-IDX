"""
insights/warmup.py — Non-blocking warm-up of the Stage 2.6 event calendars (Stage 2.7G.1, operational only).

The market scan (insights.market.fetch_market_inputs) and every per-stock event lookup call
services.event_context.build_event_context, whose FRED macro-release fetch can take ~30 s when cold
(4 sequential requests, 15 s timeout each) and is then cached for MACRO_CACHE_TTL_MINUTES. This module
makes that SAME call — same function, same symbol as the market scan (SPY), same date window, same
data.events cache — in a daemon thread when the server starts. Nothing here re-implements FRED, changes
event scoring, or makes a Claude call.

Status is reported honestly:
  READY        every FRED release for the current event window is in the Stage 2.6 cache
  LOADING      a warm-up is running (and has not exceeded EVENT_WARMUP_TIMEOUT_SECONDS)
  UNAVAILABLE  FRED is not configured, the last attempt failed, or the warm-up is taking too long
  NOT_LOADED   nothing cached yet for today's window and no attempt is running (a later request loads it)
Loading/unavailable event data is never presented as "low event risk" anywhere.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import config

logger = logging.getLogger(__name__)
WARM_SYMBOL = config.MARKET_PROXY_SYMBOL          # the market scan warms/reads SPY's event context

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_state = {"started_monotonic": None, "finished_monotonic": None, "ok": None, "error": None, "attempts": 0,
          "duration_s": None}


def event_window(now: datetime):
    """Same window as services.event_context.build_event_context (Stage 2.6), so cache keys match exactly."""
    return ((now - timedelta(days=config.EVENT_LOOKBACK_DAYS)).date(),
            (now + timedelta(days=config.EVENT_LOOKAHEAD_DAYS)).date())


def macro_cache_coverage(now: Optional[datetime] = None) -> tuple:
    """(cached releases, total releases) for the current window, read from the existing Stage 2.6 cache."""
    from data.events.cache import event_cache
    start, end = event_window(now or datetime.now(timezone.utc))
    ids = list(config.FRED_RELEASE_IDS.values())
    cached = sum(1 for rid in ids if event_cache.get(f"fred:{rid}:{start.isoformat()}:{end.isoformat()}") is not None)
    return cached, len(ids)


def _default_fetch(symbol: str):
    from services.event_context import build_event_context
    return build_event_context(symbol)


def _run(fetch: Callable[[str], object]) -> None:
    t = time.monotonic()
    ok, err = False, None
    try:
        fetch(WARM_SYMBOL)
        cached, total = macro_cache_coverage()
        ok = cached == total
        if not ok:
            err = f"FRED returned {cached} of {total} release calendars"
    except Exception as exc:  # noqa: BLE001 - a warm-up failure must never affect the server
        err = type(exc).__name__
        logger.warning("Event calendar warm-up failed: %s", exc)
    with _lock:
        _state.update(finished_monotonic=time.monotonic(), ok=ok, error=err, duration_s=round(time.monotonic() - t, 2))


def start_event_warmup(fetch: Optional[Callable[[str], object]] = None, *, force: bool = False) -> bool:
    """Start one background warm-up (returns immediately). False if one is running, FRED is not configured, the
    cache is already complete, or the last attempt was less than EVENT_WARMUP_RETRY_SECONDS ago."""
    global _thread
    if not config.has_fred_credentials():
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        last = _state["started_monotonic"]
        if not force and last is not None and time.monotonic() - last < config.EVENT_WARMUP_RETRY_SECONDS:
            return False
        cached, total = macro_cache_coverage()
        if not force and cached == total:
            return False
        _state.update(started_monotonic=time.monotonic(), finished_monotonic=None, ok=None, error=None,
                      attempts=_state["attempts"] + 1, duration_s=None)
        _thread = threading.Thread(target=_run, args=(fetch or _default_fetch,), name="event-calendar-warmup",
                                   daemon=True)
        _thread.start()
    return True


def macro_status() -> dict:
    """Operational status of the macro-event calendar for display. Never reports READY without cached data."""
    if not config.has_fred_credentials():
        return {"state": "UNAVAILABLE", "label": "Unavailable", "detail": "FRED is not configured."}
    cached, total = macro_cache_coverage()
    with _lock:
        running = _thread is not None and _thread.is_alive()
        started, st = _state["started_monotonic"], dict(_state)
    if cached == total:
        return {"state": "READY", "label": "Ready", "detail": f"{total} release calendars cached.",
                "warmup_seconds": st["duration_s"]}
    if running:
        elapsed = time.monotonic() - started
        if elapsed > config.EVENT_WARMUP_TIMEOUT_SECONDS:
            return {"state": "UNAVAILABLE", "label": "Unavailable",
                    "detail": f"FRED has not answered after {int(elapsed)} s; still trying in the background."}
        return {"state": "LOADING", "label": "Loading", "detail": "Macro calendar is loading in the background."}
    if st["ok"] is False:
        return {"state": "UNAVAILABLE", "label": "Unavailable",
                "detail": f"Last load failed ({st['error']}); it retries automatically."}
    return {"state": "NOT_LOADED", "label": "Not loaded yet", "detail": "Loads on the next market refresh."}


def reset_for_tests() -> None:
    global _thread
    with _lock:
        _thread = None
        _state.update(started_monotonic=None, finished_monotonic=None, ok=None, error=None, attempts=0,
                      duration_s=None)
