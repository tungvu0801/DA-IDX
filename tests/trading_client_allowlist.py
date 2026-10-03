"""Stage 4.6A: the ONE place Alpaca's TradingClient may appear in production code — the read-only PAPER adapter.

The earlier "no TradingClient / no alpaca.trading" invariants stay as they were (each scans its own stage's files); this
module states the global rule and pins the single exception. In test infrastructure the SDK class is only ever faked or made
to fail on construction (the Stage 4.5 API fixture, the browser harness guard); only the dedicated Stage 4.6A test file
constructs a real one — with fake credentials, behind the guarded transport and a fake wire, never reaching the network."""
TRADING_CLIENT_ADAPTER = "paper/alpaca_readonly.py"
TRADING_CLIENT_TEST_FILES = ("tests/test_alpaca_paper_46.py",)


def trading_client_allowed(rel: str) -> bool:
    """rel: a path relative to stock-agent/, with forward slashes."""
    return rel == TRADING_CLIENT_ADAPTER
