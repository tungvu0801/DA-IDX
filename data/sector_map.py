"""
data/sector_map.py — A small, manually curated mapping of sector name ->
{sector ETF proxy, representative peer tickers}.

There is no free, already-integrated sector-classification data source in
this app (Alpaca's market-data API doesn't include GICS sector data), so
this static, editable file stands in for one. It is intentionally simple
to edit by hand as sectors/peers drift over time — treat it as a curated
reference, not authoritative classification data.

IMPORTANT: only the *grouping* here is static. Every performance number
computed from it (sector ETF % change, peer average % change, etc.) always
comes from live Alpaca bars via analysis/sector_context.py — nothing here
fabricates a price or return.
"""
from typing import Dict, List, TypedDict


class SectorInfo(TypedDict):
    etf: str
    peers: List[str]


SECTOR_MAP: Dict[str, SectorInfo] = {
    "Semiconductors": {
        "etf": "SOXX",
        "peers": ["NVDA", "AMD", "INTC", "MU", "AVGO", "MRVL", "QCOM", "TXN"],
    },
    "Software": {
        "etf": "XLK",
        "peers": ["MSFT", "ORCL", "CRM", "ADBE", "NOW", "PANW", "INTU"],
    },
    "Financials": {
        "etf": "XLF",
        "peers": ["JPM", "BAC", "GS", "MS", "WFC", "C", "SCHW", "AXP"],
    },
    "Energy": {
        "etf": "XLE",
        "peers": ["XOM", "CVX", "COP", "SLB", "OXY", "EOG"],
    },
    "Healthcare": {
        "etf": "XLV",
        "peers": ["LLY", "UNH", "JNJ", "MRK", "ABBV", "PFE", "TMO", "ISRG"],
    },
    "Consumer Discretionary": {
        "etf": "XLY",
        "peers": ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG"],
    },
    "Consumer Staples": {
        "etf": "XLP",
        "peers": ["PG", "KO", "PEP", "WMT", "COST", "PM"],
    },
    "Industrials": {
        "etf": "XLI",
        "peers": ["CAT", "GE", "RTX", "UNP", "HON", "BA"],
    },
    "Utilities": {
        "etf": "XLU",
        "peers": ["NEE", "DUK", "SO", "D", "AEP"],
    },
    "Real Estate": {
        "etf": "XLRE",
        "peers": ["PLD", "AMT", "EQIX", "SPG"],
    },
    "Communication Services": {
        "etf": "XLC",
        "peers": ["GOOGL", "GOOG", "META", "NFLX", "DIS", "T", "VZ"],
    },
    "Materials": {
        "etf": "XLB",
        "peers": ["LIN", "SHW", "FCX", "NEM"],
    },
}

MARKET_PROXY = "SPY"  # kept here for convenience; config.MARKET_PROXY_SYMBOL is the source of truth


def _build_symbol_to_sector() -> Dict[str, str]:
    """Inverts SECTOR_MAP once at import time — never hand-maintained twice."""
    reverse: Dict[str, str] = {}
    for sector_name, info in SECTOR_MAP.items():
        for peer in info["peers"]:
            reverse.setdefault(peer, sector_name)
    return reverse


SYMBOL_TO_SECTOR: Dict[str, str] = _build_symbol_to_sector()


def get_sector_for_symbol(symbol: str) -> str | None:
    """Return the sector name for `symbol`, or None if it isn't in the curated map."""
    return SYMBOL_TO_SECTOR.get(symbol.strip().upper())
