"""
data/market_data.py — Low-level Alpaca market-data access: client
construction and batched historical bar retrieval.

This is the only place Alpaca's *market-data* clients get constructed.
Alpaca's *trading* client is never imported anywhere in this project, so
there is no code path capable of placing an order.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

import config

logger = logging.getLogger(__name__)


def get_data_client() -> StockHistoricalDataClient:
    """Build the Alpaca market-data client. Raises if credentials are missing."""
    if not config.has_credentials():
        raise RuntimeError(
            "Missing Alpaca API credentials. Copy .env.example to .env and "
            "fill in ALPACA_API_KEY / ALPACA_SECRET_KEY."
        )
    return StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)


def get_screener_client() -> ScreenerClient:
    """Build the Alpaca screener client. Raises if credentials are missing."""
    if not config.has_credentials():
        raise RuntimeError(
            "Missing Alpaca API credentials. Copy .env.example to .env and "
            "fill in ALPACA_API_KEY / ALPACA_SECRET_KEY."
        )
    return ScreenerClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY)


def fetch_daily_bars(
    client: StockHistoricalDataClient,
    symbols: List[str],
    lookback_days: int = config.DAILY_BAR_LOOKBACK_DAYS,
) -> Dict[str, pd.DataFrame]:
    """
    Batch-fetch daily OHLCV bars for many symbols in a single API request.

    Returns {symbol: DataFrame} sorted ascending by timestamp. Symbols with
    no data returned (invalid ticker, no history, delisted, etc.) are
    simply omitted — never fabricated.
    """
    if not symbols:
        return {}

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)

    try:
        feed = DataFeed(config.DATA_FEED)
    except ValueError:
        logger.warning("Unknown DATA_FEED '%s'; defaulting to IEX.", config.DATA_FEED)
        feed = DataFeed.IEX

    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        adjustment=Adjustment.ALL,
        feed=feed,
    )

    try:
        bar_set = client.get_stock_bars(request)
    except APIError as exc:
        logger.error("Failed to fetch daily bars for %d symbols: %s", len(symbols), exc)
        return {}
    except Exception as exc:  # noqa: BLE001 - one bad batch should not crash the scan
        logger.error("Unexpected error fetching daily bars: %s", exc)
        return {}

    result: Dict[str, pd.DataFrame] = {}
    for symbol, bar_list in (bar_set.data or {}).items():
        if not bar_list:
            continue
        frame = pd.DataFrame(
            {
                "timestamp": [b.timestamp for b in bar_list],
                "open": [b.open for b in bar_list],
                "high": [b.high for b in bar_list],
                "low": [b.low for b in bar_list],
                "close": [b.close for b in bar_list],
                "volume": [b.volume for b in bar_list],
            }
        )
        result[symbol] = frame.sort_values("timestamp").reset_index(drop=True)
    return result
