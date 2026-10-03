"""
api/routes/ops_status.py — compact operational status (Stage 2.7G.1). Read-only; no external calls, no Claude.

GET /api/ops/status   macro-event calendar state (warm-up / cache), market-data freshness from the existing
                      in-memory cache, and AI usage vs the hourly/daily limits (same counting rule as the gating).

It never reports a healthy state it cannot verify: "Ready" needs cached data, market freshness needs a cached
scan, and Robinhood/research status are taken by the page from the report it already loaded (no extra
gateway call). If the macro calendar is not cached and no warm-up is running, a background warm-up is started
(rate-limited, 0 Claude calls).
"""
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config

router = APIRouter(prefix="/api/ops", tags=["ops"])


@router.get("/status")
def get_ops_status() -> JSONResponse:
    from agents.usage_tracker import usage_tracker
    from insights import service
    from insights.daily_review import ai_usage_counts
    from insights.plain import market_freshness
    from insights.warmup import macro_status, start_event_warmup
    now = datetime.now(timezone.utc)
    macro = macro_status()
    if macro["state"] == "NOT_LOADED" and config.EVENT_WARMUP_ON_STARTUP and start_event_warmup():
        macro = macro_status()
    entry = service._cache.get("inputs")             # read-only peek at the existing market-scan cache
    if entry is None:
        market = {"label": "NOT_LOADED", "text": "Not loaded yet"}
    else:
        f = market_freshness(entry[1].fetched_at.isoformat(), now)
        market = {"label": f["label"], "text": f["label"].title(), "updated": f["updated"]}
    counts = ai_usage_counts(usage_tracker)
    return JSONResponse({
        "macro_events": macro, "market_data": market,
        "ai": {"calls_last_hour": counts["last_hour"], "hourly_limit": config.AI_MAX_CALLS_PER_HOUR,
               "calls_today": counts["today"], "daily_limit": config.AI_MAX_CALLS_PER_DAY},
        "checked_at": now.isoformat(timespec="seconds"),
    })
