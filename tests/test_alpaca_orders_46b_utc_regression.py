"""
Stage 4.6B regression: the Preview path must call the REAL fit.current._utc with its required argument.

Production failure (2026-10-07, real Alpaca PAPER validation, attempt 5): `paper/alpaca_orders._reference()` called
`FC._utc()` with no argument; `fit/current._utc(now: Optional[datetime])` requires one, so every Preview crashed with a
TypeError (HTTP 500) before a client_order_id or intent existed. The approved fix is `FC._utc(None)`.

Why the existing suites never caught it: the `lab` fixture of tests/test_alpaca_orders_46b.py (line 75) and the browser
harness (browser_tests/app_server.py) replace FC._utc with a lambda whose argument has a default, so a zero-argument call
was valid under test and invalid in production. This module keeps every other 4.6B fake (fake broker, fake market data,
no network) but puts the REAL `_utc` back before Preview runs, so the test fails on `FC._utc()` and passes on `FC._utc(None)`.
"""
import inspect
import re
from pathlib import Path

import pytest

import test_alpaca_orders_46b as T46B
from test_alpaca_orders_46b import broker, clock, fresh, lab  # noqa: F401 - the Stage 4.6B fixtures (fakes only, no network)
from fit import current as FC
from paper import alpaca_orders as O

ROOT = Path(__file__).resolve().parents[1]
REAL_UTC = FC.__dict__["_utc"]                 # captured at import time, before any fixture replaces the module attribute


def test_real_utc_requires_one_argument_and_the_old_zero_argument_call_fails():
    params = list(inspect.signature(REAL_UTC).parameters.values())
    assert [p.name for p in params] == ["now"] and params[0].default is inspect.Parameter.empty   # no default: the real contract
    with pytest.raises(TypeError):
        REAL_UTC()                                                                                 # exactly the production bug
    assert REAL_UTC(None).tzinfo is not None                                                       # the approved call works


def test_preview_runs_with_the_real_fit_current_utc_signature(broker, monkeypatch):
    """The real `_utc` (one required argument) is in place while Preview runs: on the old `FC._utc()` this raises TypeError."""
    T46B.ready(broker)
    monkeypatch.setattr(FC, "_utc", REAL_UTC)                      # undo the lab stub for THIS test only
    assert FC._utc is REAL_UTC and inspect.signature(FC._utc).parameters["now"].default is inspect.Parameter.empty
    it = O.preview("KO", "BUY", 1)["intent"]                        # BUY: the reference-price step (where the crash was) always runs
    assert it["state"] == "PREVIEWED" and it["symbol"] == "KO" and it["side"] == "BUY" and it["qty"] == 1
    assert re.match(r"^sa46b-[0-9a-f]{32}$", it["client_order_id"]) and it["order_type"] == "MARKET" and it["time_in_force"] == "DAY"
    assert {r["rule"] for r in it["rules"]} >= {"V8", "V10", "V15", "V16", "V17"}          # the deterministic rules were evaluated
    assert broker.posts == [] and all(m == "GET" for m, _, _ in broker.requests)             # preview never writes to the broker


def test_production_module_never_calls_utc_without_its_argument():
    src = (ROOT / "paper" / "alpaca_orders.py").read_text(encoding="utf-8")
    assert "FC._utc()" not in src and src.count("FC._utc(None)") == 1
