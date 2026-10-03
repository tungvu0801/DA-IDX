"""
robinhood_sync.py — OPTIONAL, READ-ONLY sync of your Robinhood holdings into
watchlist.txt.

>>> READ THIS BEFORE USING <<<

Robinhood does not publish an official API for market data or account
access. This module uses `robin_stocks`, a well-known but UNOFFICIAL,
reverse-engineered client for Robinhood's private app API. Using it means:

  - Your Robinhood username/password are stored in your local .env file
    (never in code, never committed — see .gitignore), not a scoped API key.
  - It goes outside Robinhood's Terms of Service, which prohibit
    unauthorized/automated access. Some users have had accounts flagged or
    restricted for using tools like this — use at your own risk.
  - It can break without notice whenever Robinhood changes its private
    endpoints, since there's no stable, versioned contract to rely on.

To keep the blast radius as small as possible, this module:
  - Only ever calls read endpoints (login + fetch current holdings).
  - Never imports `robin_stocks.robinhood.orders` or any other module
    capable of placing, modifying, or cancelling a trade.
  - Is only ever triggered explicitly and in the foreground, via
    `python main.py --sync-robinhood`. It is never called automatically
    from the monitoring loop (--loop), because a first-time or expired
    login can require you to approve a push notification or type a 2FA
    code — something that must not silently block a background process.

First run will prompt you (in the terminal / Robinhood app) to approve the
login. After that, the session is cached in `config.ROBINHOOD_SESSION_DIR`
so future syncs usually don't require 2FA again until that session expires.
"""
import logging
from pathlib import Path
from typing import List, Optional

import config

logger = logging.getLogger(__name__)

# Markers that delimit the auto-managed block inside watchlist.txt. Anything
# outside this block (your own manually-added tickers/comments) is left
# untouched on every sync.
_START_MARKER = "# === ROBINHOOD HOLDINGS (auto-synced by robinhood_sync.py — do not edit below) ==="
_END_MARKER = "# === END ROBINHOOD HOLDINGS ==="

_robin_stocks_module = None  # lazily imported so the rest of the agent works without this optional dependency


def _client():
    """Lazily import robin_stocks so it's only required if this feature is used."""
    global _robin_stocks_module
    if _robin_stocks_module is None:
        try:
            import robin_stocks.robinhood as r
        except ImportError as exc:
            raise RuntimeError(
                "robin_stocks is not installed. Run: pip install robin_stocks"
            ) from exc
        _robin_stocks_module = r
    return _robin_stocks_module


def login() -> bool:
    """
    Log into Robinhood with a read-only intent (we never call any order
    endpoint). Returns True on success, False if credentials are missing or
    login failed. May prompt for a 2FA/verification code on the terminal,
    or wait for you to approve a push notification in the Robinhood app —
    this is expected on first login or once the cached session expires.
    """
    if not config.has_robinhood_credentials():
        logger.warning(
            "ROBINHOOD_USERNAME / ROBINHOOD_PASSWORD not set in .env; skipping Robinhood sync."
        )
        return False

    r = _client()
    config.ROBINHOOD_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    try:
        r.login(
            username=config.ROBINHOOD_USERNAME,
            password=config.ROBINHOOD_PASSWORD,
            store_session=True,
            pickle_path=str(config.ROBINHOOD_SESSION_DIR),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - never crash the agent over a login hiccup
        logger.error("Robinhood login failed: %s", exc)
        return False


def get_holding_symbols() -> List[str]:
    """
    Return the ticker symbols currently held in the Robinhood account.

    This calls robin_stocks' `build_holdings()`, which only reads position
    data (GET requests) — it cannot place, modify, or cancel an order.
    """
    r = _client()
    try:
        holdings = r.build_holdings()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to fetch Robinhood holdings: %s", exc)
        return []

    if not holdings:
        return []
    return sorted(holdings.keys())


def sync_watchlist_file(path: Path = config.WATCHLIST_FILE) -> Optional[List[str]]:
    """
    Fetch current Robinhood holdings and write them into a clearly-marked,
    auto-managed block in watchlist.txt, without touching any tickers or
    comments you've added manually elsewhere in the file.

    Returns the list of synced symbols, or None if the sync could not run
    (missing credentials, login failure, or no holdings returned).
    """
    if not login():
        return None

    symbols = get_holding_symbols()
    if not symbols:
        logger.warning("No Robinhood holdings found (or the fetch failed); watchlist.txt left unchanged.")
        return None

    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    if _START_MARKER in existing_lines and _END_MARKER in existing_lines:
        start_idx = existing_lines.index(_START_MARKER)
        end_idx = existing_lines.index(_END_MARKER)
        existing_lines = existing_lines[:start_idx] + existing_lines[end_idx + 1 :]

    while existing_lines and existing_lines[-1].strip() == "":
        existing_lines.pop()

    managed_block = [_START_MARKER, *symbols, _END_MARKER]
    new_lines = existing_lines + ([""] if existing_lines else []) + managed_block

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    logger.info("Synced %d Robinhood holding(s) into %s", len(symbols), path)
    return symbols
