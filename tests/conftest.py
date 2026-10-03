"""Test setup: isolated DB copy, no network. Must run before the app is imported."""
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import database.database as dbm  # noqa: E402

# Point the process-wide DB singleton at a COPY of the real database (read via the sqlite backup API,
# opened read-only), so tests can prove the portfolio code writes nothing without touching real data.
TMP_DIR = Path(tempfile.mkdtemp(prefix="stock-agent-tests-"))
TEST_DB = TMP_DIR / "stock_agent.db"
_real = ROOT / "data" / "stock_agent.db"
if _real.exists():
    src = sqlite3.connect(f"file:{_real.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(TEST_DB))
    src.backup(dst)
    src.close()
    dst.close()
dbm._db_instance = dbm.ResearchDatabase(TEST_DB)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def blocked(*a, **k):
        raise AssertionError("network access attempted during tests")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture(autouse=True)
def no_os_notifications(monkeypatch):
    """Stage 4.3: no test ever shows a real desktop notification (tests use a fake adapter)."""
    from notifications import windows
    hits = []
    monkeypatch.setattr(windows, "send", lambda *a, **k: hits.append(a) or {"status": "FAILED", "error_code": "BLOCKED_IN_TESTS"})
    yield
    assert not hits, "a real OS notification adapter call was attempted during tests"


@pytest.fixture(autouse=True)
def no_registry_no_browser(monkeypatch):
    """Stage 4.4: no test writes the real registry or opens a real browser (an in-memory identity + a recording opener)."""
    from notifications import identity, windows

    class MemoryRegistry:
        value = None

        def get(self):
            return self.value

        def put(self):
            self.value = identity.DISPLAY_NAME

        def remove(self):
            self.value = None
    monkeypatch.setattr(identity, "BACKEND", MemoryRegistry())
    monkeypatch.setattr(windows, "OPENER", lambda url: windows._stats.__setitem__("test_opened_url", url))


@pytest.fixture(autouse=True)
def no_alpaca_paper_broker(monkeypatch):
    """Stage 4.6A: no test ever sees real Alpaca PAPER credentials (both names are removed from the environment) or reaches
    the paper trading API (the adapter's real network wire is replaced by one that fails the test). Tests use fakes."""
    from paper import alpaca_readonly as R
    for name in (R.KEY_ENV, R.SECRET_ENV):
        monkeypatch.delenv(name, raising=False)

    def real_wire_blocked():
        raise AssertionError("a real Alpaca paper network adapter was requested during tests")
    monkeypatch.setattr(R, "WIRE", real_wire_blocked)


_ALPACA_ORDER_WIRE_HITS: list = []


@pytest.fixture(autouse=True)
def no_alpaca_paper_orders(monkeypatch):
    """Stage 4.6B: in EVERY test the real network wires of the paper order adapters (reads and the POST writer) are
    tripwires: requesting one fails the test (recorded here and asserted at teardown, so it cannot be swallowed).
    Only tests that install the fake broker (tests/alpaca_order_fakes.py) reach a wire at all."""
    from paper import alpaca_order_reads, alpaca_order_writer

    def tripwire(which):
        def _f():
            _ALPACA_ORDER_WIRE_HITS.append(which)
            raise AssertionError(f"a real Alpaca paper {which} network adapter was requested during tests")
        return _f
    monkeypatch.setattr(alpaca_order_reads, "WIRE", tripwire("read"))
    monkeypatch.setattr(alpaca_order_writer, "WIRE", tripwire("order POST"))
    yield
    hits, _ALPACA_ORDER_WIRE_HITS[:] = list(_ALPACA_ORDER_WIRE_HITS), []
    assert not hits, f"real Alpaca paper wire requested during a test: {hits}"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(TMP_DIR, ignore_errors=True)
