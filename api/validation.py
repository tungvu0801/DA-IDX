"""api/validation.py — Shared request validation helpers for API routes."""
import re

_SYMBOL_PATTERN = re.compile(r"^[A-Za-z.\-]{1,10}$")


def validate_symbol(symbol: str) -> str:
    """Normalize + validate a ticker symbol from a path/body parameter.

    Raises ValueError (routes should turn this into a 400) if the symbol
    doesn't look like a plausible ticker — this never contacts Alpaca to
    check it actually exists, it only rejects obviously malformed input.
    """
    symbol = symbol.strip().upper()
    if not _SYMBOL_PATTERN.match(symbol):
        raise ValueError(f"'{symbol}' is not a valid ticker symbol.")
    return symbol
