"""
data/news.py — Wrapper around Alpaca's news endpoint.

Not called by any endpoint yet in this milestone (the Technical Agent
doesn't use it) — provided now so a future Catalyst Agent has a real,
working data source to build on instead of a stub.
"""
import logging
from typing import Any, Dict, List

from alpaca.common.exceptions import APIError
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

import config

logger = logging.getLogger(__name__)


def get_recent_news(symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Return recent news items for `symbol` from Alpaca's news feed, each as
    {"headline", "source", "created_at", "url", "summary"}.

    Returns an empty list on any failure or if no credentials are
    configured. Callers must treat an empty list as "no news found" —
    never fabricate a catalyst that wasn't actually returned.
    """
    if not config.has_credentials():
        return []
    try:
        client = NewsClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)
        request = NewsRequest(symbols=symbol, limit=limit)
        news_set = client.get_news(request)
    except APIError as exc:
        logger.warning("Failed to fetch news for %s: %s", symbol, exc)
        return []
    except Exception as exc:  # noqa: BLE001 - a news lookup failure must not crash the caller
        logger.warning("Unexpected error fetching news for %s: %s", symbol, exc)
        return []

    articles = (news_set.data or {}).get("news", [])
    return [
        {
            "headline": article.headline,
            "source": article.source,
            "created_at": article.created_at,
            "url": article.url,
            "summary": article.summary,
        }
        for article in articles
    ]
