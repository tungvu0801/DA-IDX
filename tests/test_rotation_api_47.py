"""Stage 4.7 Phase 4 — the FastAPI route (api/routes/portfolio_rotation.py) and the Portfolio Rotation workspace
(frontend/portfolio_rotation.js). Fully offline: synthetic bars, the Stage 2.7C fake gateway behind the real provider path,
fake Stage 4.6A view payloads, a scratch Stage 4.5 lab; conftest blocks sockets and tripwires the Alpaca wires."""
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fw_fixtures as FL
import pf_fixtures as PF
import test_paper_45 as T45
import test_rotation_engine_47 as P3
from api.routes import portfolio_rotation as PR
from fit import current as FC
from fit import readonly as RO
from paper import alpaca_view as APV
from rotation import snapshots as SN

ROOT = Path(__file__).resolve().parents[1]
Dt = date.fromisoformat
BASE = "/api/portfolio-rotation"


@pytest.fixture
def lab():
    m = FL.Market(P3.SYMS + ("SPY",), start=date(2025, 6, 2), vol=0.01)
    for s, close in (("AMD", 100), ("MU", 50), ("KO", 60), ("CLS", 40), ("NVDA", 150), ("SNDK", 500), ("SPY", 500)):
        m.set_bar(s, Dt(P3.SESSION), close, close * 1.01, close * 0.99, close, 2_000_000)
    lb = FL.FLab(m)
    lb.market = m
    return lb


@pytest.fixture
def api(monkeypatch, lab):
    """The real app against the scratch lab: fixed clock, synthetic market data, no snapshot loaded, every broker path faked."""
    import agents.technical_agent as ta
    from ai_explain import service as AX
    from api.routes import portfolio as pr
    from api.server import app
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    monkeypatch.setattr(PR, "NOW_FN", lambda: P3.NOW)
    monkeypatch.setattr(PR, "FETCH", {"fetch_fn": lab.market.fetch, "client": object(), "cache": FC.BarCache()})
    monkeypatch.setattr(PR, "VIEW_FN", lambda: {"refreshed_at": None, "account": None, "positions": []})     # no 4.6A refresh yet
    PR._SNAPSHOTS.clear()
    hits = []

    def fail(what):
        def _f(*a, **k):
            hits.append(what)
            raise RuntimeError(what)
        return _f
    monkeypatch.setattr(ta, "get_provider", fail("claude research"))
    monkeypatch.setattr(AX, "get_provider", fail("claude explanation"))
    provider, http = P3.gateway(fetched_at=(P3.NOW - timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(pr, "provider_factory", lambda: provider)                                   # the approved extension point
    c = TestClient(app)
    c.hits, c.lab, c.provider, c.http = hits, lab, provider, http
    yield c
    PR._SNAPSHOTS.clear()


def cfg(api, **over):
    body = {"name": "api", "weights": dict(P3.CONFIG["weights"]), **{k: v for k, v in P3.CONFIG.items() if k != "weights"}, **over}
    r = api.post(f"{BASE}/configs", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def run(api, c, source="ROBINHOOD_READ_ONLY", universe=None):
    return api.post(f"{BASE}/run", json={"config_id": c["config_id"], "config_hash": c["config_hash"], "portfolio_source": source,
                                         "universe": universe or {"source": "CUSTOM", "symbols": list(P3.SYMS)}})


# ==================================================================================================================================
# API — configuration
# ==================================================================================================================================

def test_1_2_get_config_and_configs_make_no_requests(api):
    r = api.get(f"{BASE}/config")
    assert r.status_code == 200
    b = r.json()
    assert b["benchmark"] == "SPY" and b["max_symbols"] == 100 and b["label"].startswith("PORTFOLIO ROTATION")
    assert [s["id"] for s in b["portfolio_sources"]] == ["ALPACA_PAPER_VIEW", "ROBINHOOD_READ_ONLY", "LOCAL_SIMULATOR"]
    assert {s["id"]: s["handoff"] for s in b["portfolio_sources"]} == b["handoff_modes"] == {"ALPACA_PAPER_VIEW": "ALPACA_PAPER_PREFILL",
                                                                                              "ROBINHOOD_READ_ONLY": "DISPLAY_ONLY", "LOCAL_SIMULATOR": "DISPLAY_ONLY"}
    assert b["universe_sources"] == ["WATCHLIST", "SAVED_SCAN", "SAVED_UNIVERSE", "CUSTOM"]
    assert b["snapshots"] == {"ALPACA_PAPER_VIEW": None, "ROBINHOOD_READ_ONLY": None, "LOCAL_SIMULATOR": None} and "saved_scans" in b
    assert api.get(f"{BASE}/configs").json() == {"configs": []}
    assert api.provider.calls == [] and api.lab.market.calls == [] and api.hits == []           # 0 gateway, 0 market data, 0 AI


def test_3_4_post_config_valid_and_invalid(api):
    c = cfg(api)
    assert c["created"] is True and c["version"] == 1 and c["benchmark"] == "SPY" and re.fullmatch(r"[0-9a-f]{64}", c["config_hash"])
    assert api.get(f"{BASE}/configs").json()["configs"][0]["config_id"] == c["config_id"]
    again = api.post(f"{BASE}/configs", json={"name": "api", "weights": dict(P3.CONFIG["weights"]), **{k: v for k, v in P3.CONFIG.items() if k != "weights"}})
    assert again.status_code == 200 and again.json()["created"] is False                         # identical content: not re-created
    for bad, code in (({"portfolio_size": 10, "exit_rank": 15, "min_position_weight": "0.10"}, "INVALID_WEIGHT_BOUNDS"),
                      ({"weights": {**P3.CONFIG["weights"], "momentum": "0.31"}}, "INVALID_WEIGHTS"),
                      ({"exit_rank": 1}, "INVALID_EXIT_RANK"), ({"excluded_symbols": ["bad!"]}, "INVALID_CONFIG")):
        body = {"name": "x", "weights": dict(P3.CONFIG["weights"]), **{k: v for k, v in P3.CONFIG.items() if k != "weights"}, **bad}
        r = api.post(f"{BASE}/configs", json=body)
        assert r.status_code == 422 and r.json()["status"] == code, bad
    assert len(api.get(f"{BASE}/configs").json()["configs"]) == 1                                 # rejected configs are never stored


# ==================================================================================================================================
# API — snapshots
# ==================================================================================================================================

def test_5_load_robinhood_snapshot_through_the_approved_path(api):
    r = api.post(f"{BASE}/snapshot", json={"source": "ROBINHOOD_READ_ONLY"})
    assert r.status_code == 200, r.text
    s = r.json()["snapshot"]
    assert s["source"] == "ROBINHOOD_READ_ONLY" and s["fresh"] is True and s["age_min"] == 5 and s["cash"] == "1000.00" and s["n_positions"] == 2
    assert s["positions"] == [{"symbol": "AMD", "quantity": "10", "flags": []}, {"symbol": "MU", "quantity": "2.5", "flags": []}]
    assert s["handoff"] == "DISPLAY_ONLY" and s["info"]["account_alias"] == "holdings" and "equity_value" in s["info"]
    assert api.provider.calls == ["get_portfolio", "get_positions"] and [x["path"] for x in api.http.requests] == ["/portfolio", "/positions"]
    assert api.get(f"{BASE}/config").json()["snapshots"]["ROBINHOOD_READ_ONLY"]["n_positions"] == 2       # held in memory
    text = r.text
    assert "x" * 43 not in text and "secret" not in text.lower() and "token" not in text.lower()          # nothing secret returned


def test_6_robinhood_unavailable(api, monkeypatch):
    from api.routes import portfolio as pr
    from portfolio.provider import UnavailablePortfolioProvider
    monkeypatch.setattr(pr, "provider_factory", lambda: UnavailablePortfolioProvider("DISABLED"))
    r = api.post(f"{BASE}/snapshot", json={"source": "ROBINHOOD_READ_ONLY"})
    assert r.status_code == 422 and r.json()["status"] == "PORTFOLIO_UNAVAILABLE"
    assert api.get(f"{BASE}/config").json()["snapshots"]["ROBINHOOD_READ_ONLY"] is None


def test_7_stale_snapshot_is_reported_and_blocks_the_run(api, monkeypatch):
    from api.routes import portfolio as pr
    provider, _ = P3.gateway(fetched_at=(P3.NOW - timedelta(minutes=45)).isoformat())
    monkeypatch.setattr(pr, "provider_factory", lambda: provider)
    s = api.post(f"{BASE}/snapshot", json={"source": "ROBINHOOD_READ_ONLY"}).json()["snapshot"]
    assert s["fresh"] is False and s["age_min"] == 45
    c = cfg(api)
    r = run(api, c)
    assert r.status_code == 200 and r.json()["run"]["status"] == "DATA_STALE" and api.lab.market.calls == []   # persisted, fail closed
    provider2, _ = P3.gateway(status="STALE", fetched_at=(P3.NOW - timedelta(minutes=1)).isoformat())
    monkeypatch.setattr(pr, "provider_factory", lambda: provider2)
    s2 = api.post(f"{BASE}/snapshot", json={"source": "ROBINHOOD_READ_ONLY"}).json()["snapshot"]
    assert s2["status"] == "STALE" and s2["fresh"] is False


def test_8_alpaca_snapshot_is_read_only(api, monkeypatch):
    r = api.post(f"{BASE}/snapshot", json={"source": "ALPACA_PAPER_VIEW"})
    assert r.status_code == 422 and r.json()["status"] == "INPUT_ERROR"                               # no 4.6A refresh yet
    monkeypatch.setattr(PR, "VIEW_FN", lambda: P3.alpaca_view(cash="2500.00", refreshed_at=(P3.NOW - timedelta(minutes=2)).isoformat()))
    r = api.post(f"{BASE}/snapshot", json={"source": "ALPACA_PAPER_VIEW"})
    assert r.status_code == 200
    s = r.json()["snapshot"]
    assert s["source"] == "ALPACA_PAPER_VIEW" and s["fresh"] and s["cash"] == "2500.00" and s["positions"] == [{"symbol": "AMD", "quantity": "10", "flags": []}]
    assert s["info"]["account_number_masked"] == "••••7890" and s["info"]["equity"] == "999999.00"       # informational only
    assert api.provider.calls == [] and api.http.requests == []                                          # not the Robinhood path


def test_8b_alpaca_default_view_is_the_frozen_4_6A_view_with_zero_requests(api, monkeypatch):
    monkeypatch.setattr(PR, "VIEW_FN", None)
    calls = []
    monkeypatch.setattr(APV, "view", lambda: calls.append(1) or {"refreshed_at": None, "account": None, "positions": []})
    r = api.post(f"{BASE}/snapshot", json={"source": "ALPACA_PAPER_VIEW"})
    assert r.status_code == 422 and calls == [1]                                                          # APV.view() consulted, 0 broker calls


def test_9_local_simulator_snapshot(api):
    T45.account(api.lab, cash="50000")
    T45.order(api.lab, "2026-09-24", "AMD", "BUY", 10)
    T45.process(api.lab, "2026-09-25")                                                           # the Stage 4.5 fill uses the synthetic market
    n0 = len(api.lab.market.calls)
    r = api.post(f"{BASE}/snapshot", json={"source": "LOCAL_SIMULATOR"})
    assert r.status_code == 200
    s = r.json()["snapshot"]
    assert s["source"] == "LOCAL_SIMULATOR" and s["fresh"] and s["positions"] == [{"symbol": "AMD", "quantity": "10", "flags": []}]
    assert api.provider.calls == [] and len(api.lab.market.calls) == n0                            # the snapshot itself: 0 network


# ==================================================================================================================================
# API — runs
# ==================================================================================================================================

def test_10_run_with_missing_snapshot_fails_closed(api):
    c = cfg(api)
    r = run(api, c)
    assert r.status_code == 200 and r.json()["run"]["status"] == "INPUT_ERROR" and "snapshot" in r.json()["run"]["status_detail"].lower()
    assert api.provider.calls == [] and api.lab.market.calls == []


def test_11_12_valid_run_makes_zero_provider_calls(api):
    api.post(f"{BASE}/snapshot", json={"source": "ROBINHOOD_READ_ONLY"})
    api.provider.calls.clear(); api.http.requests.clear()
    c = cfg(api)
    r = run(api, c)
    assert r.status_code == 200, r.text
    out = r.json()["run"]
    assert out["status"] == "VALID" and out["data_session"] == P3.SESSION and out["benchmark"] == "SPY" and out["handoff_mode"] == "DISPLAY_ONLY"
    assert out["reference_equity"] == "2125.00" and out["current_cash_weight"] and out["target_cash_weight"] == "0.050000"
    assert out["n_universe"] == 6 and out["n_eligible"] == 6 and out["n_selected"] == 3 and out["turnover"] and out["market_data_requests"] == 1
    assert re.fullmatch(r"[0-9a-f]{64}", out["input_hash"]) and re.fullmatch(r"[0-9a-f]{64}", out["proposal_hash"]) and re.fullmatch(r"[0-9a-f]{32}", out["run_id"])
    assert out["source_mismatch_note"] and "Robinhood" in out["source_mismatch_note"]
    assert api.provider.calls == [] and api.http.requests == [] and len(api.lab.market.calls) == 1   # the run: 0 gateway calls, 1 market-data fetch
    bad = api.post(f"{BASE}/run", json={"config_id": c["config_id"], "config_hash": "0" * 64, "portfolio_source": "ROBINHOOD_READ_ONLY",
                                        "universe": {"source": "CUSTOM", "symbols": ["AMD"]}})
    assert bad.status_code == 409 and bad.json()["status"] == "CONFIG_HASH_MISMATCH"
    assert run(api, c, universe={"source": "CUSTOM", "symbols": ["bad!"]}).status_code == 422
    assert run(api, c, universe={"source": "SAVED_SCAN", "ref": "0" * 32}).status_code == 404
    assert len(api.get(f"{BASE}/runs").json()["runs"]) == 1                                           # pre-run errors are not persisted


def test_13_17_runs_detail_candidates_targets_rebalance(api):
    api.post(f"{BASE}/snapshot", json={"source": "ROBINHOOD_READ_ONLY"})
    c = cfg(api)
    rid = run(api, c).json()["run"]["run_id"]
    runs = api.get(f"{BASE}/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["run_id"] == rid and runs[0]["handoff_mode"] == "DISPLAY_ONLY"
    d = api.get(f"{BASE}/runs/{rid}").json()
    assert d["run"]["run_id"] == rid and d["integrity"]["ok"] is True and d["universe"] == sorted(P3.SYMS) and d["positions"][0]["symbol"] == "AMD"
    cands = api.get(f"{BASE}/runs/{rid}/candidates").json()["candidates"]
    assert len(cands) == 6 and [x["rank"] for x in cands] == [1, 2, 3, 4, 5, 6] and isinstance(cands[0]["raw"], dict) and isinstance(cands[0]["scores"], dict)
    assert set(cands[0]["scores"]) == {"momentum", "trend", "relative_strength", "volatility", "drawdown", "liquidity"} and cands[0]["reasons"] == []
    t = api.get(f"{BASE}/runs/{rid}/targets").json()["targets"]
    assert len(t) == 3 and t[0]["rank"] == 1 and isinstance(t[0]["flags"], list)
    rb = api.get(f"{BASE}/runs/{rid}/rebalance").json()
    assert rb["handoff_mode"] == "DISPLAY_ONLY" and rb["source_mismatch_note"] and all(i["action"] in ("ADD", "INCREASE", "DECREASE", "EXIT", "HOLD", "NONE") for i in rb["items"])
    assert all(i["handoff"]["mode"] == "DISPLAY_ONLY" and i["handoff"]["eligible"] is False and i["handoff"]["draft"] is None for i in rb["items"])
    assert {i["symbol"] for i in rb["items"]} >= {"AMD", "MU"}
    for u in (f"{BASE}/runs/{'0' * 32}", f"{BASE}/runs/{'0' * 32}/candidates", f"{BASE}/runs/{'0' * 32}/targets", f"{BASE}/runs/{'0' * 32}/rebalance"):
        assert api.get(u).status_code == 404


# ==================================================================================================================================
# security
# ==================================================================================================================================

def test_18_19_exact_endpoints_no_broker_write_path(api):
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith(BASE)}
    assert mine == {f"{BASE}/config": {"get"}, f"{BASE}/configs": {"get", "post"}, f"{BASE}/snapshot": {"post"}, f"{BASE}/run": {"post"},
                    f"{BASE}/runs": {"get"}, f"{BASE}/runs/{{run_id}}": {"get"}, f"{BASE}/runs/{{run_id}}/candidates": {"get"},
                    f"{BASE}/runs/{{run_id}}/targets": {"get"}, f"{BASE}/runs/{{run_id}}/rebalance": {"get"}}
    for p in mine:
        assert not re.search(r"(?i)order|trade|execute|submit|place|cancel|replace|broker|paper|prefill|handoff", p), p
    for m in ("put", "patch", "delete"):
        assert getattr(api, m)(f"{BASE}/run").status_code == 405


def test_20_21_static_route_boundaries():
    src = (ROOT / "api" / "routes" / "portfolio_rotation.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"alpaca_orders\b|alpaca_order_writer|alpaca_order_reads|alpaca_order_store|alpaca_order_rules|alpaca_view|alpaca_readonly|"
                         r"^\s*(from|import)\s+(requests|rh_gateway|portfolio\b|portfolio\.provider|alpaca|anthropic|agents)", code, re.M)
    assert not re.search(r"get_orders|get_quotes|get_tax_lots|get_realized|get_pnl|place_?order|submit_?order|cancel|/v2/|TradingClient|"
                         r"os\.environ|getenv|\.env\b|eval\(|exec\(|__import__|importlib|setInterval|setTimeout|Scheduler|threading\.Thread", code)
    assert "provider_factory" not in code and "load_robinhood_snapshot(PROVIDER_FN)" in code      # only through the Phase 3 adapter
    assert code.count("JSONResponse(") >= 10 and 'extra="forbid"' in code


def test_22_strict_request_bodies(api):
    good = {"name": "s", "weights": dict(P3.CONFIG["weights"]), **{k: v for k, v in P3.CONFIG.items() if k != "weights"}}
    assert api.post(f"{BASE}/configs", json={**good, "benchmark": "QQQ"}).status_code == 422          # unknown / fixed field
    assert api.post(f"{BASE}/configs", json={**good, "portfolio_size": "10"}).status_code == 422       # strict int
    assert api.post(f"{BASE}/configs", json={**good, "cash_buffer_pct": 0.05}).status_code == 422      # decimal strings only
    assert api.post(f"{BASE}/configs", json={**good, "weights": {**good["weights"], "momentum": 0.3}}).status_code == 422
    assert api.post(f"{BASE}/snapshot", json={"source": "MANUAL"}).status_code == 422
    assert api.post(f"{BASE}/snapshot", json={"source": "ROBINHOOD_READ_ONLY", "refresh": True}).status_code == 422
    c = cfg(api)
    base = {"config_id": c["config_id"], "config_hash": c["config_hash"], "portfolio_source": "ROBINHOOD_READ_ONLY", "universe": {"source": "WATCHLIST"}}
    for extra in ({"order": True}, {"quantity": 5}, {"universe": {"source": "WATCHLIST", "extra": 1}}, {"portfolio_source": "ROBINHOOD"}):
        assert api.post(f"{BASE}/run", json={**base, **extra}).status_code == 422, extra
    assert api.provider.calls == [] and api.lab.market.calls == []


# ==================================================================================================================================
# UI (static) and wiring
# ==================================================================================================================================

JS = (ROOT / "frontend" / "portfolio_rotation.js").read_text(encoding="utf-8")
CODE = "\n".join(line for line in JS.splitlines() if not line.lstrip().startswith("//"))
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def test_23_34_workspace_is_lazy_escaped_timer_free_and_display_only():
    assert 'addWorkspace({ id: "rotation", label: "Portfolio Rotation", cls: "prt-mode", show })' in JS                # 23 lazy via show()
    assert 'document.getElementById("prt-body")' in JS and 'id="prt-body"' in HTML and "portfolio_rotation.css" in HTML and "portfolio_rotation.js" in HTML
    assert HTML.index("alpaca_orders.js") < HTML.index("portfolio_rotation.js")
    assert "Load Robinhood Snapshot" in JS and "ROBINHOOD PORTFOLIO — READ ONLY" in JS                                   # 24 / 25
    assert "This proposal is based on your Robinhood holdings. No Robinhood orders will be sent." in JS
    assert "ALPACA PAPER PORTFOLIO" in JS and "Prepare Paper Order" in JS                                                 # Phase 5: Alpaca only
    assert not re.search(r"/api/alpaca-paper|alpaca-paper-orders|__apoPost|data-apo=\"preview\"|data-apo=\"confirm\"|AlpacaOrders\.show", CODE)   # never the 4.6B API or its buttons
    assert not re.search(r"setTimeout|setInterval|requestAnimationFrame|eval\(|new Function|location\.|document\.cookie", CODE)       # 34 no timers
    assert "PROPOSAL ONLY — NO ORDERS ARE SENT" in JS and not re.search(r"\b(buy now|sell now|order now|place|execute|recommend\w*)\b", CODE, re.I)
    assert "if (busy) return null;" in JS and 'if (!b || b.disabled || busy) return;' in JS                               # 33 busy guard
    assert 'data-prt-act="run"${runReady() && !busy ? "" : " disabled"}' in JS and "fresh(snap())" in JS                 # 32 stale disables Run
    assert "const esc = (v) =>" in JS and JS.count("esc(") > 60 and 'innerHTML = `<div class="prt">' in JS                # 31 escaped rendering
    assert "<th>Rank</th><th>Ticker</th><th>Composite</th><th>Current Wt</th><th>Target Wt</th><th>Shares (Δ)</th><th>Action</th><th>Reason</th><th>Paper handoff</th>" in JS   # 29
    assert "FACTOR DETAIL" in JS and all(k in JS for k in ("Momentum", "Trend", "Relative Strength", "Volatility", "Drawdown", "Liquidity"))   # 30
    assert JS.count("fetch(") == 1 and "load();" in JS.split("function show()")[1]                                           # 35: show → 2 GETs only
    assert "RH_SHARES_HELD" in JS and "information only" in JS
    for name in ("portfolio_rotation.js", "portfolio_rotation.css"):
        assert HTML.count(name) == 1
    css = (ROOT / "frontend" / "portfolio_rotation.css").read_text(encoding="utf-8")
    assert "#tab-strategy:not(.prt-mode) #prt-body { display: none; }" in css and "#tab-strategy.prt-mode #ppf-body" in css


def test_35_38_pane_requests_are_zero_network_and_fake_broker_sees_nothing(api):
    """The pane's own requests (GET /config, GET /configs) and a Robinhood snapshot + run through the API: 0 broker requests."""
    for u in (f"{BASE}/config", f"{BASE}/configs"):
        assert api.get(u).status_code == 200
    assert api.provider.calls == [] and api.lab.market.calls == [] and api.hits == []                      # 35
    api.post(f"{BASE}/snapshot", json={"source": "ROBINHOOD_READ_ONLY"})
    assert api.provider.calls == ["get_portfolio", "get_positions"] and [x["path"] for x in api.http.requests] == ["/portfolio", "/positions"]   # 36
    api.provider.calls.clear()
    c = cfg(api)
    assert run(api, c).json()["run"]["status"] == "VALID"
    assert api.provider.calls == [] and len(api.lab.market.calls) == 1                                    # 37 / 38
    import conftest
    assert conftest._ALPACA_ORDER_WIRE_HITS == [] and api.hits == []


def test_44_wiring_touches_only_server_and_index():
    server = (ROOT / "api" / "server.py").read_text(encoding="utf-8")
    assert server.count("portfolio_rotation") == 2 and "app.include_router(portfolio_rotation.router)" in server
    life = server.split("async def _lifespan")[1].split("app = FastAPI(")[0]
    assert "rotation" not in life                                                                           # nothing at startup
