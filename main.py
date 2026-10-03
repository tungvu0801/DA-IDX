"""
main.py — Application entry point for the stock monitoring agent.

Usage:
    python main.py             Run a single market + watchlist scan and exit.
    python main.py --loop      Run continuously, rescanning on the intervals
                                configured in config.py (MARKET_SCAN_INTERVAL /
                                WATCHLIST_SCAN_INTERVAL), until Ctrl+C.

This agent is for market monitoring, analysis, and alerts only. It never
imports Alpaca's trading client and never places, modifies, or cancels
orders.
"""
import argparse
import logging
import sys
import time
from typing import Dict, Optional

import config
import scanner.market_scanner as market_scanner
import scanner.watchlist as watchlist_module
from alerts.alert_manager import AlertManager
from analysis.indicators import TickerMetrics

# Some terminals (notably the default Windows console codepage) can't encode
# the emoji used in the section headers below, which would otherwise crash
# every print. Force UTF-8 output so the report renders consistently
# everywhere; fall back silently if the stream doesn't support reconfiguring
# (e.g. output redirected to certain pipes).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SECTION_WIDTH = 60


# ---------------------------------------------------------------------------
# Formatting helpers — keep numeric formatting/None-handling in one place.
# ---------------------------------------------------------------------------
def _rule(char: str = "=") -> str:
    return char * SECTION_WIDTH


def print_header(title: str) -> None:
    print()
    print(_rule())
    print(title)
    print(_rule())


def fmt_price(value: Optional[float]) -> str:
    return f"${value:,.2f}" if value is not None else "N/A"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def fmt_rvol(value: Optional[float]) -> str:
    return f"{value:.2f}x" if value is not None else "N/A"


def fmt_score(value: Optional[int]) -> str:
    return f"{value}/100" if value is not None else "N/A"


def fmt_num(value: Optional[float], decimals: int = 2) -> str:
    return f"{value:.{decimals}f}" if value is not None else "N/A"


# ---------------------------------------------------------------------------
# Market overview rendering
# ---------------------------------------------------------------------------
def print_market_overview(overview: market_scanner.MarketOverview) -> None:
    print_header("MARKET OVERVIEW")

    if overview.used_fallback_universe:
        print(
            "(Alpaca's screener endpoint was unavailable — scanned a fixed, "
            "liquid fallback list of stocks instead of live market rankings.)"
        )
    print(
        f"Candidates scanned: {overview.scanned_symbol_count}   "
        f"Passed liquidity filter: {len(overview.all_metrics)}   "
        f"(min price ${config.MIN_PRICE:g}, min avg volume {config.MIN_AVG_VOLUME:,})"
    )

    print("\n\U0001F525 TOP GAINERS")
    if overview.top_gainers:
        for m in overview.top_gainers:
            print(f"  {m.symbol:<6} {fmt_pct(m.pct_change):>9}   RVOL {fmt_rvol(m.relative_volume):<8} {m.signal}")
    else:
        print("  (none)")

    print("\n\U0001F4C9 TOP LOSERS")
    if overview.top_losers:
        for m in overview.top_losers:
            print(f"  {m.symbol:<6} {fmt_pct(m.pct_change):>9}   RVOL {fmt_rvol(m.relative_volume):<8} {m.signal}")
    else:
        print("  (none)")

    print("\n\U0001F440 UNUSUAL VOLUME")
    if overview.unusual_volume:
        for m in overview.unusual_volume:
            print(f"  {m.symbol:<6} RVOL {fmt_rvol(m.relative_volume):<8} {fmt_pct(m.pct_change)}")
    else:
        print("  (none)")

    print("\n\U0001F680 MOMENTUM STOCKS")
    if overview.momentum_stocks:
        for m in overview.momentum_stocks:
            print(f"  {m.symbol:<6} {fmt_pct(m.pct_change):>9}   Momentum {fmt_score(m.momentum_score)}")
    else:
        print("  (none)")

    print("\n\U0001F9ED POSSIBLE BREAKOUTS")
    if overview.possible_breakouts:
        for m in overview.possible_breakouts:
            print(
                f"  {m.symbol:<6} dist-from-20d-high {fmt_num(m.dist_from_high_pct)}%   "
                f"Attention {fmt_score(m.attention_score)}"
            )
    else:
        print("  (none)")


# ---------------------------------------------------------------------------
# Watchlist rendering
# ---------------------------------------------------------------------------
def print_watchlist_detail(metrics_by_symbol: Dict[str, TickerMetrics]) -> None:
    print_header("MY WATCHLIST")

    if not metrics_by_symbol:
        print("No watchlist data available. Add tickers (one per line) to watchlist.txt.")
        return

    for symbol, m in metrics_by_symbol.items():
        source_label = "latest trade" if m.price_source == "latest_trade" else "prior daily close"
        print(f"\n{symbol}")
        print(f"  Price:             {fmt_price(m.price)}   ({source_label}, as of {m.as_of})")
        print(f"  Change:            {fmt_pct(m.pct_change)}")
        print(f"  Volume:            {m.volume:,.0f}")
        print(f"  Relative Volume:   {fmt_rvol(m.relative_volume)}")
        print(f"  RSI:               {fmt_num(m.rsi, 1)}")
        print(
            f"  EMA 9/20/50:       {fmt_num(m.ema_fast)} / {fmt_num(m.ema_medium)} / {fmt_num(m.ema_slow)}"
        )
        print(f"  Trend:             {m.trend}")
        print(f"  Dist. from 20d hi: {fmt_num(m.dist_from_high_pct)}%")
        print(f"  Volatility (ann.): {fmt_num(m.volatility_pct)}%")
        print(f"  Signal:            {m.signal}")
        print(
            f"  Attention Score:   {fmt_score(m.attention_score)}  "
            "(measures unusual activity only — not a buy/sell rating)"
        )


# ---------------------------------------------------------------------------
# Scan orchestration
# ---------------------------------------------------------------------------
def run_market_scan(alert_manager: AlertManager) -> None:
    try:
        overview = market_scanner.scan_market()
        print_market_overview(overview)
        alert_manager.dispatch(overview.all_metrics.values())
    except RuntimeError as exc:
        # Missing credentials, etc. — a clear message, not a stack trace.
        print(f"\nMarket scan skipped: {exc}")
    except Exception as exc:  # noqa: BLE001 - keep the agent alive on unexpected failures
        logger.error("Market scan failed unexpectedly: %s", exc)
        print("\nMarket scan failed unexpectedly. See logs for details.")


def run_watchlist_scan(alert_manager: AlertManager) -> None:
    try:
        client = market_scanner.get_data_client()
        symbols = watchlist_module.load_watchlist()
        wl_metrics = watchlist_module.analyze_watchlist(client, symbols)
        print_watchlist_detail(wl_metrics)
        alert_manager.dispatch(wl_metrics.values())
    except RuntimeError as exc:
        print(f"\nWatchlist scan skipped: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.error("Watchlist scan failed unexpectedly: %s", exc)
        print("\nWatchlist scan failed unexpectedly. See logs for details.")


def run_once() -> None:
    alert_manager = AlertManager()
    print_header("STOCK MONITORING AGENT")
    print("Analysis & alerts only — this agent never places trades.")

    run_market_scan(alert_manager)
    run_watchlist_scan(alert_manager)

    print()
    print(_rule())


def run_loop() -> None:
    alert_manager = AlertManager()
    last_market_scan = 0.0
    last_watchlist_scan = 0.0

    print_header("STOCK MONITORING AGENT (continuous mode)")
    print("Analysis & alerts only — this agent never places trades.")
    print(
        f"Market scan every {config.MARKET_SCAN_INTERVAL}s, "
        f"watchlist scan every {config.WATCHLIST_SCAN_INTERVAL}s. Press Ctrl+C to stop."
    )

    try:
        while True:
            now = time.time()

            if now - last_market_scan >= config.MARKET_SCAN_INTERVAL:
                run_market_scan(alert_manager)
                last_market_scan = now

            if now - last_watchlist_scan >= config.WATCHLIST_SCAN_INTERVAL:
                run_watchlist_scan(alert_manager)
                last_watchlist_scan = now

            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping stock monitoring agent.")


def run_sync_robinhood() -> None:
    """
    One-off, foreground sync of current Robinhood holdings into
    watchlist.txt. Deliberately NOT available in --loop mode: a first-time
    or expired Robinhood login can require approving a push notification or
    typing a 2FA code, which must never silently block a background process.
    """
    import robinhood_sync

    if not config.has_robinhood_credentials():
        print("Missing Robinhood credentials.")
        print("Add ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD to your .env file, then re-run.")
        sys.exit(1)

    print("Syncing Robinhood holdings into watchlist.txt ...")
    print("(You may be prompted to approve this login via the Robinhood app or a verification code.)")
    symbols = robinhood_sync.sync_watchlist_file()
    if symbols:
        print(f"Synced {len(symbols)} holding(s): {', '.join(symbols)}")
        print(f"Updated {config.WATCHLIST_FILE}")
    else:
        print("Sync did not complete — check the warnings above (login failure or no holdings found).")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Stock market monitoring agent — whole-market scanner + personal "
            "watchlist + descriptive alerts. Analysis only; never places trades."
        )
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously using the intervals configured in config.py instead of a single pass.",
    )
    parser.add_argument(
        "--sync-robinhood",
        action="store_true",
        help=(
            "One-off: pull your current Robinhood holdings into watchlist.txt, then exit. "
            "Read-only; never places trades. See robinhood_sync.py for details/caveats."
        ),
    )
    args = parser.parse_args()

    if args.sync_robinhood:
        run_sync_robinhood()
        return

    if not config.has_credentials():
        print("Missing Alpaca API credentials.")
        print("Copy .env.example to .env and fill in ALPACA_API_KEY / ALPACA_SECRET_KEY, then re-run.")
        sys.exit(1)

    if args.loop:
        run_loop()
    else:
        run_once()


if __name__ == "__main__":
    main()
