"""
scanner/market_scanner.py — Whole-market screening.

Strategy for scanning the "whole market" without downloading tick data for
every U.S.-listed stock:

  1. Ask Alpaca's screener endpoints for the day's top gainers, top losers,
     and most-active-by-volume symbols. Alpaca computes these market-wide
     rankings on their end — we only get back a bounded list of symbols
     (a few dozen), never the whole market's raw data.
  2. Fetch daily bars for just that bounded candidate list (plus, if the
     screener is unavailable, a small fixed fallback universe) in a single
     batched request (data.market_data.fetch_daily_bars).
  3. Run the shared indicator engine (analysis.indicators.compute_metrics)
     on each symbol and apply the liquidity filters (MIN_PRICE / MIN_AVG_VOLUME).

This module intentionally never requests tick-level data and never issues
one HTTP request per symbol — bar data is always fetched in one batched
call per scan. It only ever uses Alpaca's *market data* client (via
data.market_data), never the trading client, so this agent has no code
path capable of placing an order.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List

from alpaca.common.exceptions import APIError
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import MarketMoversRequest, MostActivesRequest

import config
from analysis.indicators import TickerMetrics, compute_metrics
from data.market_data import fetch_daily_bars, get_data_client, get_screener_client

logger = logging.getLogger(__name__)

# Re-exported so existing callers (main.py, api routes) can keep using
# market_scanner.get_data_client()/fetch_daily_bars() without knowing they
# actually live in the data layer now.
__all__ = [
    "MarketOverview",
    "get_data_client",
    "get_screener_client",
    "fetch_daily_bars",
    "get_candidate_symbols",
    "analyze_symbols",
    "scan_market",
]


@dataclass
class MarketOverview:
    """Categorized results from a whole-market scan."""

    top_gainers: List[TickerMetrics] = field(default_factory=list)
    top_losers: List[TickerMetrics] = field(default_factory=list)
    unusual_volume: List[TickerMetrics] = field(default_factory=list)
    momentum_stocks: List[TickerMetrics] = field(default_factory=list)
    possible_breakouts: List[TickerMetrics] = field(default_factory=list)
    approaching_support: List[TickerMetrics] = field(default_factory=list)
    approaching_resistance: List[TickerMetrics] = field(default_factory=list)
    volatility_expansion: List[TickerMetrics] = field(default_factory=list)
    all_metrics: Dict[str, TickerMetrics] = field(default_factory=dict)
    used_fallback_universe: bool = False
    scanned_symbol_count: int = 0


def get_candidate_symbols(top_n: int = config.SCREENER_TOP_N) -> tuple[List[str], bool]:
    """
    Return a bounded list of "already interesting" symbols from Alpaca's
    screener (top gainers + top losers + most active by volume).

    Returns (symbols, used_fallback). If the screener endpoint is
    unavailable for any reason (data plan limits, outage, bad response),
    falls back to config.FALLBACK_UNIVERSE so the scan can still proceed.
    """
    try:
        screener = get_screener_client()
        movers = screener.get_market_movers(MarketMoversRequest(top=top_n))
        actives = screener.get_most_actives(MostActivesRequest(top=top_n))

        symbols = set()
        symbols.update(m.symbol for m in movers.gainers)
        symbols.update(m.symbol for m in movers.losers)
        symbols.update(a.symbol for a in actives.most_actives)

        if symbols:
            return sorted(symbols), False
        logger.warning("Screener returned no symbols; using fallback universe.")
    except APIError as exc:
        logger.warning("Alpaca screener endpoint unavailable (%s); using fallback universe.", exc)
    except Exception as exc:  # noqa: BLE001 - any screener failure should degrade, not crash
        logger.warning("Unexpected screener error (%s); using fallback universe.", exc)

    return list(config.FALLBACK_UNIVERSE), True


def analyze_symbols(
    client: StockHistoricalDataClient,
    symbols: List[str],
) -> Dict[str, TickerMetrics]:
    """
    Fetch bars for `symbols` and compute indicators/scores/signal for each.
    A failure on any single symbol is logged and skipped — it never aborts
    the rest of the batch.
    """
    bars_by_symbol = fetch_daily_bars(client, symbols)
    metrics: Dict[str, TickerMetrics] = {}
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol)
        if bars is None or bars.empty:
            logger.debug("No bar data for %s; skipping.", symbol)
            continue
        try:
            result = compute_metrics(symbol, bars)
        except Exception as exc:  # noqa: BLE001 - never let one bad symbol kill the scan
            logger.warning("Failed to compute metrics for %s: %s", symbol, exc)
            continue
        if result is not None:
            metrics[symbol] = result
    return metrics


def _passes_liquidity_filter(m: TickerMetrics) -> bool:
    if m.price < config.MIN_PRICE:
        return False
    if m.avg_volume is not None and m.avg_volume < config.MIN_AVG_VOLUME:
        return False
    return True


def scan_market(top_n: int = config.SCREENER_TOP_N) -> MarketOverview:
    """
    Run a full whole-market scan: get candidate symbols, analyze them, apply
    liquidity filters, and bucket the results into the categories shown on
    the market overview screen.
    """
    client = get_data_client()
    symbols, used_fallback = get_candidate_symbols(top_n)
    metrics = analyze_symbols(client, symbols)

    liquid = {sym: m for sym, m in metrics.items() if _passes_liquidity_filter(m)}

    top_gainers = sorted(liquid.values(), key=lambda m: m.pct_change, reverse=True)[: config.TOP_RESULTS]
    top_losers = sorted(liquid.values(), key=lambda m: m.pct_change)[: config.TOP_RESULTS]

    unusual_volume = sorted(
        (m for m in liquid.values() if m.relative_volume is not None and m.relative_volume >= 2.0),
        key=lambda m: m.relative_volume,
        reverse=True,
    )[: config.TOP_RESULTS]

    momentum_stocks = sorted(
        (m for m in liquid.values() if m.signal == "STRONG MOMENTUM"),
        key=lambda m: m.momentum_score,
        reverse=True,
    )[: config.TOP_RESULTS]

    possible_breakouts = sorted(
        (m for m in liquid.values() if m.signal == "BREAKOUT WATCH"),
        key=lambda m: m.attention_score,
        reverse=True,
    )[: config.TOP_RESULTS]

    approaching_support = sorted(
        (
            m
            for m in liquid.values()
            if m.dist_from_support_pct is not None
            and 0 <= m.dist_from_support_pct <= config.APPROACHING_LEVEL_PCT
        ),
        key=lambda m: m.dist_from_support_pct,
    )[: config.TOP_RESULTS]

    approaching_resistance = sorted(
        (
            m
            for m in liquid.values()
            if m.dist_from_resistance_pct is not None
            and -config.APPROACHING_LEVEL_PCT <= m.dist_from_resistance_pct <= 0
        ),
        key=lambda m: m.dist_from_resistance_pct,
        reverse=True,
    )[: config.TOP_RESULTS]

    volatility_expansion = sorted(
        (
            m
            for m in liquid.values()
            if m.volatility_expansion is not None
            and m.volatility_expansion >= config.VOLATILITY_EXPANSION_RATIO_THRESHOLD
        ),
        key=lambda m: m.volatility_expansion,
        reverse=True,
    )[: config.TOP_RESULTS]

    return MarketOverview(
        top_gainers=top_gainers,
        top_losers=top_losers,
        unusual_volume=unusual_volume,
        momentum_stocks=momentum_stocks,
        possible_breakouts=possible_breakouts,
        approaching_support=approaching_support,
        approaching_resistance=approaching_resistance,
        volatility_expansion=volatility_expansion,
        all_metrics=metrics,
        used_fallback_universe=used_fallback,
        scanned_symbol_count=len(symbols),
    )
