"""
scanner/watchlist.py — Load and analyze the user's personal watchlist.

Tickers are read fresh from watchlist.txt every time `load_watchlist()` is
called, so editing that file (adding/removing symbols) takes effect on the
next run without any code changes.

Watchlist analysis reuses the same batched-fetch + indicator engine as the
market scanner (data.market_data.fetch_daily_bars / analysis.indicators.compute_metrics)
so there is a single source of truth for how metrics are calculated.
"""
import logging
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

import config
from analysis.indicators import TickerMetrics, compute_metrics
from data.market_data import fetch_daily_bars

logger = logging.getLogger(__name__)


def load_watchlist(path: Path = config.WATCHLIST_FILE) -> List[str]:
    """
    Read ticker symbols from watchlist.txt (one per line). Blank lines and
    lines starting with '#' are ignored so the file can contain comments.
    """
    if not path.exists():
        logger.warning("Watchlist file not found at %s; using an empty watchlist.", path)
        return []

    symbols: List[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip().upper()
        if not line or line.startswith("#"):
            continue
        symbols.append(line)
    # De-duplicate while preserving the order the user listed them in.
    seen = set()
    ordered_unique = []
    for sym in symbols:
        if sym not in seen:
            seen.add(sym)
            ordered_unique.append(sym)
    return ordered_unique


def add_to_watchlist(symbol: str, path: Path = config.WATCHLIST_FILE) -> List[str]:
    """Append `symbol` to watchlist.txt if not already present. Returns the updated list."""
    symbol = symbol.strip().upper()
    current = load_watchlist(path)
    if symbol and symbol not in current:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{symbol}\n")
        current.append(symbol)
    return current


def remove_from_watchlist(symbol: str, path: Path = config.WATCHLIST_FILE) -> List[str]:
    """Remove `symbol` from watchlist.txt (comments/other lines untouched). Returns the updated list."""
    symbol = symbol.strip().upper()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if line.strip().upper() != symbol]
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return load_watchlist(path)


def _fetch_latest_trades(
    client: StockHistoricalDataClient, symbols: List[str]
) -> Dict[str, Tuple[float, pd.Timestamp]]:
    """
    Best-effort fetch of the latest trade (price + timestamp) for each
    symbol, so the watchlist can show a fresher price than yesterday's
    close when the market is open. If this call fails (market closed, feed
    restriction, permission issue) we simply fall back to daily-bar closes
    elsewhere — this is never allowed to crash the watchlist scan.
    """
    if not symbols:
        return {}
    try:
        request = StockLatestTradeRequest(symbol_or_symbols=symbols)
        trades = client.get_stock_latest_trade(request)
    except APIError as exc:
        logger.info("Latest-trade lookup unavailable (%s); using daily closes instead.", exc)
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.info("Unexpected error fetching latest trades (%s); using daily closes instead.", exc)
        return {}

    return {symbol: (trade.price, trade.timestamp) for symbol, trade in trades.items()}


def analyze_watchlist(client: StockHistoricalDataClient, symbols: List[str]) -> Dict[str, TickerMetrics]:
    """
    Fetch bars + latest trades for the watchlist and compute full metrics
    for every symbol. A failure on one ticker (bad symbol, no data, etc.)
    is logged and skipped rather than aborting the whole watchlist.
    """
    if not symbols:
        return {}

    bars_by_symbol = fetch_daily_bars(client, symbols)
    latest_trades = _fetch_latest_trades(client, symbols)

    results: Dict[str, TickerMetrics] = {}
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol)
        if bars is None or bars.empty:
            logger.warning("No market data available for watchlist symbol '%s' — skipping.", symbol)
            continue
        try:
            latest_price, latest_trade_time = latest_trades.get(symbol, (None, None))
            metrics = compute_metrics(
                symbol,
                bars,
                latest_trade_price=latest_price,
                latest_trade_time=latest_trade_time,
            )
        except Exception as exc:  # noqa: BLE001 - one bad symbol should not crash the run
            logger.warning("Failed to compute metrics for watchlist symbol '%s': %s", symbol, exc)
            continue
        if metrics is not None:
            results[symbol] = metrics
        else:
            logger.warning("Insufficient data to analyze watchlist symbol '%s' — skipping.", symbol)

    return results
