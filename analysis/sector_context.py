"""
analysis/sector_context.py — Computes a stock's performance relative to its
sector and the overall market, using real, live-fetched Alpaca bars for the
sector ETF, a handful of peers, and the broad-market proxy (config.MARKET_PROXY_SYMBOL).

Only the sector/peer *grouping* (data/sector_map.py) is static; every
number here — the sector ETF's % change, peer average % change, the market
proxy's % change — is computed the same way as everything else in
analysis/indicators.py, via one batched data.market_data.fetch_daily_bars
call and the existing compute_metrics() function. Nothing here duplicates
that math or fabricates a return.
"""
from dataclasses import dataclass
from typing import Optional

from alpaca.data.historical.stock import StockHistoricalDataClient

import config
from analysis.indicators import compute_metrics
from data.market_data import fetch_daily_bars
from data.sector_map import SECTOR_MAP, get_sector_for_symbol


@dataclass
class SectorContext:
    sector_name: str
    sector_etf: str
    sector_pct_change: Optional[float]
    peer_avg_pct_change: Optional[float]
    stock_vs_sector_pct: Optional[float]  # stock's own % change minus the sector ETF's
    market_proxy_symbol: str
    market_pct_change: Optional[float]


def compute_sector_context(
    symbol: str, stock_pct_change: float, client: StockHistoricalDataClient
) -> Optional[SectorContext]:
    """
    Return sector/market context for `symbol`, or None if it isn't in the
    curated sector map or the required bars couldn't be fetched — callers
    must treat None as "sector context unavailable," never fabricate one.
    """
    sector_name = get_sector_for_symbol(symbol)
    if sector_name is None:
        return None

    sector_info = SECTOR_MAP[sector_name]
    etf = sector_info["etf"]
    peers = [p for p in sector_info["peers"] if p != symbol.strip().upper()]
    market_symbol = config.MARKET_PROXY_SYMBOL

    symbols_to_fetch = sorted(set([etf, market_symbol] + peers))
    bars_by_symbol = fetch_daily_bars(client, symbols_to_fetch)

    etf_bars = bars_by_symbol.get(etf)
    etf_metrics = compute_metrics(etf, etf_bars) if etf_bars is not None else None
    sector_pct_change = etf_metrics.pct_change if etf_metrics is not None else None

    market_bars = bars_by_symbol.get(market_symbol)
    market_metrics = compute_metrics(market_symbol, market_bars) if market_bars is not None else None
    market_pct_change = market_metrics.pct_change if market_metrics is not None else None

    peer_changes = []
    for peer in peers:
        peer_bars = bars_by_symbol.get(peer)
        if peer_bars is None:
            continue
        peer_metrics = compute_metrics(peer, peer_bars)
        if peer_metrics is not None:
            peer_changes.append(peer_metrics.pct_change)
    peer_avg_pct_change = sum(peer_changes) / len(peer_changes) if peer_changes else None

    stock_vs_sector = (
        stock_pct_change - sector_pct_change if sector_pct_change is not None else None
    )

    return SectorContext(
        sector_name=sector_name,
        sector_etf=etf,
        sector_pct_change=sector_pct_change,
        peer_avg_pct_change=peer_avg_pct_change,
        stock_vs_sector_pct=stock_vs_sector,
        market_proxy_symbol=market_symbol,
        market_pct_change=market_pct_change,
    )
