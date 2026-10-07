"""Stage 1–2.6 regression + security isolation for Stage 2.7C.

- The research bundle (evidence, Research View, risk flags, events, narrative) is byte-identical with
  portfolio awareness OFF vs ON, even after exercising every portfolio endpoint.
- Portfolio endpoints write nothing to the database (schema + full table contents unchanged).
- No Stage 1–2.6 module imports the portfolio package.
- Production stock-agent code contains no MCP client, no broker tool names, no mutation tool names.
"""
import copy
import hashlib
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import config
from agents import beginner_agent as ba
from agents.catalyst_agent import CatalystAnalysis, CatalystItem
from analysis.indicators import TickerMetrics
from analysis.sector_context import SectorContext
from api.routes import portfolio as routes
from api.routes import research as research_routes
from conftest import TEST_DB
from pf_fixtures import FakeGatewayHttp, fake_provider
from services.event_context import EventsBundle

ROOT = Path(__file__).resolve().parents[1]


def metrics() -> TickerMetrics:
    return TickerMetrics(
        symbol="NVDA", price=225.08, price_source="latest_trade", as_of=pd.Timestamp("2026-09-25T19:59:59Z"),
        prev_close=222.5, pct_change=1.16, gap_pct=0.4, volume=180_000_000.0, avg_volume=150_000_000.0,
        relative_volume=1.2, volume_expansion=1.1, rsi=58.2, ema_fast=223.0, ema_medium=220.0, ema_slow=210.0,
        trend="UPTREND", atr=6.1, high_20d=233.0, low_20d=205.0, dist_from_high_pct=-3.4, dist_from_low_pct=9.8,
        support=215.0, resistance=233.0, dist_from_support_pct=4.7, dist_from_resistance_pct=-3.4,
        momentum_5d_pct=2.3, momentum_10d_pct=4.1, volatility_pct=41.0, volatility_expansion=1.05,
        momentum_score=62, relative_volume_score=55, trend_strength_score=70, volatility_score=40, attention_score=64,
        signal="WATCH")


@pytest.fixture
def research_stubs(monkeypatch):
    """Deterministic inputs for the real evidence/research code path (no Alpaca, no news, no Claude)."""
    monkeypatch.setattr(ba, "get_data_client", lambda: object())
    monkeypatch.setattr(ba, "analyze_watchlist", lambda client, syms: {"NVDA": metrics()})
    monkeypatch.setattr(ba, "compute_sector_context", lambda s, pct, c: SectorContext(
        "Semiconductors", "SOXX", 0.8, 1.0, 0.36, "SPY", 0.4))
    monkeypatch.setattr(ba, "get_catalyst_analysis", lambda *a, **k: CatalystAnalysis("NVDA", [CatalystItem(
        "Headline", "src", "2026-09-25", "https://example.invalid", "summary", True, "POSITIVE", "reason")], ""))
    monkeypatch.setattr(ba, "build_event_context", lambda s: EventsBundle(event_risk_level="LOW", data_quality="MEDIUM"))
    monkeypatch.setattr(ba, "get_risk_analysis", lambda *a, **k: "risk text")
    monkeypatch.setattr(ba, "_generate_narrative", lambda *a, **k: ba.BeginnerNarrative("h", "w", ["g"], ["c"], "t"))
    monkeypatch.setattr(research_routes.analysis_cache, "store", lambda *a, **k: "fixed-analysis-id")


@pytest.fixture
def client():
    from api.server import app
    return TestClient(app)


def db_digest() -> str:
    conn = sqlite3.connect(str(TEST_DB))
    try:
        h = hashlib.sha256()
        for (name, sql) in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name"):
            h.update(f"{name}|{sql}".encode())
            if sql and sql.upper().startswith("CREATE TABLE"):
                cols = ", ".join(f'"{r[1]}"' for r in conn.execute(f'PRAGMA table_info("{name}")'))   # WITHOUT ROWID too
                for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY {cols}'):
                    h.update(repr(row).encode())
        return h.hexdigest()
    finally:
        conn.close()


def exercise_portfolio(client):
    for path in ("/api/portfolio", "/api/portfolio/tax-lots/NVDA", "/api/portfolio/realized-pnl",
                 "/api/portfolio/orders", "/api/portfolio/status"):
        client.get(path)
    client.post("/api/portfolio/scenario", json={"type": "hypothetical_add", "symbol": "NVDA", "amount_usd": 100})
    client.post("/api/portfolio/explain", json={})


def test_research_bundle_byte_identical_portfolio_off_vs_on(client, research_stubs, monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", False)
    off = client.get("/api/stocks/NVDA/research")
    assert off.status_code == 200
    exercise_portfolio(client)  # disabled path

    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(routes, "provider_factory", lambda: fake_provider(FakeGatewayHttp()))
    monkeypatch.setattr(routes, "event_lookup", lambda s: EventsBundle(event_risk_level="HIGH"))
    exercise_portfolio(client)  # enabled path, every endpoint
    on = client.get("/api/stocks/NVDA/research")

    assert on.content == off.content  # byte-identical: evidence, research view, flags, events, narrative
    ev = off.json()["evidence"]
    assert ev["bullish_pct"] + ev["neutral_pct"] + ev["bearish_pct"] == 100
    assert off.json()["research_view"]


def test_evidence_weights_and_thresholds_untouched_by_portfolio_config():
    assert (config.EVIDENCE_WEIGHT_TECHNICAL, config.EVIDENCE_WEIGHT_CATALYST, config.EVIDENCE_WEIGHT_RISK,
            config.EVIDENCE_WEIGHT_MARKET, config.EVIDENCE_WEIGHT_SECTOR) == (0.35, 0.25, 0.20, 0.10, 0.10)
    assert (config.RESEARCH_VIEW_STRONG_THRESHOLD, config.RESEARCH_VIEW_LEAN_THRESHOLD) == (0.50, 0.20)
    assert config.EVIDENCE_VERSION == "1.0.0" and config.ENGINE_VERSION == "1.0.0"


def test_portfolio_endpoints_write_nothing_to_database(client, monkeypatch):
    before = db_digest()
    for enabled in (False, True):
        monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", enabled)
        monkeypatch.setattr(routes, "provider_factory", lambda: fake_provider(FakeGatewayHttp()))
        monkeypatch.setattr(routes, "event_lookup", lambda s: EventsBundle())
        exercise_portfolio(client)
    snapshots_before = client.get("/api/research/snapshots")
    assert db_digest() == before
    assert client.get("/api/research/snapshots").content == snapshots_before.content


def test_event_bundles_are_not_mutated_by_portfolio_join():
    from datetime import datetime, timezone

    from portfolio.analytics import build_view
    from pf_fixtures import PORTFOLIO, POSITIONS
    bundle = EventsBundle(event_risk_level="MEDIUM", data_quality="HIGH")
    frozen = copy.deepcopy(bundle)
    build_view(copy.deepcopy(PORTFOLIO), {"status": "OK"}, copy.deepcopy(POSITIONS), {"status": "OK"}, None,
               now=datetime(2026, 9, 28, 0, 5, tzinfo=timezone.utc), stale_after_s=900, gap_material_pct=0.5,
               sector_fn=lambda s: None, event_fn=lambda s: bundle)
    assert bundle == frozen


# ---- static isolation / security scans -----------------------------------------------------------------------

def production_files():
    for p in ROOT.rglob("*"):
        # browser_tests/ is test infrastructure (Stage 3.9 browser harness: it swaps the broker for a fake), like tests/
        if any(part in {".venv", "__pycache__", "tests", "browser_tests", "data", ".pytest_cache"} for part in p.relative_to(ROOT).parts):
            continue
        if p.suffix in {".py", ".js", ".html"}:
            yield p


FORBIDDEN_TOKENS = ["place_equity_order", "review_equity_order", "cancel_equity_order", "place_option_order",
                    "place_crypto_order", "agent.robinhood.com", "get_equity_positions", "get_equity_tax_lots",
                    "get_equity_quotes", "get_equity_orders", "get_pnl_trade_history", "call_tool", "ClientSession"]


def test_no_mcp_client_or_broker_tool_names_in_stock_agent():
    offenders = []
    for p in production_files():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for tok in FORBIDDEN_TOKENS:
            if tok in text:
                offenders.append(f"{p.relative_to(ROOT)}: {tok}")
        if re.search(r"^\s*(import mcp\b|from mcp\b)", text, re.M):
            offenders.append(f"{p.relative_to(ROOT)}: imports mcp")
        if re.search(r"access_token|refresh_token|Bearer ", text) and p.parent.name == "portfolio":
            offenders.append(f"{p.relative_to(ROOT)}: OAuth material referenced")
    assert not offenders, offenders


def test_stage_1_to_2_6_modules_do_not_import_portfolio():
    allowed = {Path("api/routes/portfolio.py"), Path("api/routes/insights.py"), Path("api/routes/trade_review.py"),
               Path("api/routes/daily_review.py"),   # Stage 2.7G portfolio + watchlist report (read-only)
               Path("api/routes/stock_decision.py"),  # Stage 2.7G.3 one-stock decision after Analyze (read-only)
               Path("api/routes/strategies.py"),      # Stage 3.1 "Use my holdings" shortcut (one read-only /positions)
               Path("ai_explain/service.py"),         # Stage 3.8 reuses the pure text guard portfolio.explain.check_text
               Path("rotation/snapshots.py"),  # Stage 4.7 explicit read-only Robinhood snapshot: provider_factory, get_portfolio/get_positions only
               Path("api/server.py")}
    offenders = []
    for p in production_files():
        rel = p.relative_to(ROOT)
        if p.suffix != ".py" or rel.parts[0] in ("portfolio", "insights") or rel in allowed:
            continue
        if re.search(r"^\s*(from portfolio\b|import portfolio\b|from api\.routes import .*portfolio)",
                     p.read_text(encoding="utf-8", errors="ignore"), re.M):
            offenders.append(str(rel))
    assert not offenders, offenders


def test_stock_agent_has_no_order_endpoint():
    from api.server import app
    paths = app.openapi()["paths"]
    from paper_endpoints import paper_exempt
    for path, ops in paths.items():
        if paper_exempt(path, ops):  # Stage 4.5: the exact local paper paths only (simulated; no broker)
            continue
        assert not re.search(r"(?i)order", path) or set(ops) == {"get"}, path
        assert not re.search(r"(?i)(place|cancel|submit|execute|preview-order|review-order)", path), path
        assert not re.search(r"(?i)trade(?!-review)", path), path   # only the educational trade REVIEW exists


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Never reach a real LLM from these tests."""
    from portfolio import explain as ex
    monkeypatch.setattr(ex.explain, "__defaults__", (None, lambda: None))


def test_research_bundle_byte_identical_with_all_policies_on(client, research_stubs, monkeypatch):
    """Stage 2.7D: enabling every policy (even with extreme thresholds) never changes research output."""
    monkeypatch.setattr(config, "PORTFOLIO_AWARENESS_ENABLED", True)
    monkeypatch.setattr(config, "PORTFOLIO_POLICY_ENABLED", False)
    monkeypatch.setattr(routes, "provider_factory", lambda: fake_provider(FakeGatewayHttp()))
    monkeypatch.setattr(routes, "event_lookup", lambda s: EventsBundle(event_risk_level="HIGH"))
    off = client.get("/api/stocks/NVDA/research")
    exercise_portfolio(client)
    assert client.get("/api/portfolio").json()["policy"]["enabled"] is False

    monkeypatch.setattr(config, "PORTFOLIO_POLICY_ENABLED", True)
    for attr in ("PORTFOLIO_POLICY_POSITION_WEIGHT_PCT", "PORTFOLIO_POLICY_SECTOR_WEIGHT_PCT",
                 "PORTFOLIO_POLICY_HIGH_EVENT_EXPOSURE_PCT", "PORTFOLIO_POLICY_VALUATION_GAP_PCT",
                 "PORTFOLIO_POLICY_UNRELIABLE_QUOTE_VALUE_PCT", "PORTFOLIO_POLICY_MISSING_BASIS_VALUE_PCT"):
        monkeypatch.setattr(config, attr, "0,0,0")          # every band triggers
    monkeypatch.setattr(config, "PORTFOLIO_POLICY_LOW_CASH_PCT", "100,100,100")
    exercise_portfolio(client)
    policy = client.get("/api/portfolio").json()["policy"]
    assert policy["enabled"] is True and any(f["severity"] == "HIGH" for f in policy["flags"])
    on = client.get("/api/stocks/NVDA/research")

    assert on.content == off.content
    a, b = off.json(), on.json()
    assert (a["evidence"], a["research_view"], a["events"]["event_risk_level"]) == \
           (b["evidence"], b["research_view"], b["events"]["event_risk_level"])


def test_policy_modules_do_not_touch_research_or_database_code():
    import re as _re
    for name in ("policy.py", "sectors.py", "scenarios.py"):
        text = (ROOT / "portfolio" / name).read_text(encoding="utf-8")
        assert not _re.search(r"^\s*(from|import) (analysis|agents|database|services)\b", text, _re.M), name
