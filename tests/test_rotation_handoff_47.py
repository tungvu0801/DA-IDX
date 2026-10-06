"""Stage 4.7 Phase 5 — the CLOSED handoff rule: only an ALPACA_PAPER_VIEW run's eligible rebalance items carry a browser
prefill draft for the EXISTING frozen Stage 4.6B preview form; ROBINHOOD_READ_ONLY and LOCAL_SIMULATOR are display only.
Pure eligibility (rotation/handoff.py), the API payloads, the frozen 4.6B preview contract, static browser boundaries and
the protected files. Fully offline."""
import re
from datetime import timedelta
from pathlib import Path

import pytest

import test_rotation_api_47 as P4
import test_rotation_engine_47 as P3
from api.routes import portfolio_rotation as PR
from paper import alpaca_order_rules as RU
from rotation import handoff as H

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/portfolio-rotation"
lab = P4.lab
api = P4.api

RUN_OK = {"portfolio_source": "ALPACA_PAPER_VIEW", "status": "VALID"}
ITEM = {"symbol": "AAPL", "action": "INCREASE", "side_hint": "BUY", "est_qty_diff": 2, "handoff_allowed": 1, "current_qty": "5", "target_weight": "0.1"}


# ==================================================================================================================================
# pure eligibility
# ==================================================================================================================================

def test_source_eligibility_matrix():
    assert H.mode_for_source("ALPACA_PAPER_VIEW") == "ALPACA_PAPER_PREFILL"
    assert H.mode_for_source("ROBINHOOD_READ_ONLY") == H.mode_for_source("LOCAL_SIMULATOR") == H.mode_for_source(None) == "DISPLAY_ONLY"
    ok = H.evaluate(RUN_OK, ITEM)
    assert ok["eligible"] is True and ok["draft"] == {"symbol": "AAPL", "side": "BUY", "quantity": 2} and ok["mode"] == "ALPACA_PAPER_PREFILL"
    assert ok["source_label"] == "ALPACA PAPER" and ok["control_label"] == "Prepare Paper Order"
    for src, label, ctrl, text in (("ROBINHOOD_READ_ONLY", "ROBINHOOD — READ ONLY", "Paper handoff unavailable", "Robinhood portfolio is read-only. No quantity is transferred to Alpaca."),
                                   ("LOCAL_SIMULATOR", "LOCAL SIMULATOR", "Display only", "Local simulator results cannot create a broker draft.")):
        r = H.evaluate({**RUN_OK, "portfolio_source": src}, ITEM)                       # same deterministic item: never a draft
        assert r["eligible"] is False and r["draft"] is None and r["reason"] == "SOURCE_DISPLAY_ONLY" and r["mode"] == "DISPLAY_ONLY"
        assert r["source_label"] == label and r["control_label"] == ctrl and r["explanation"] == text
    bad = H.evaluate({**RUN_OK, "portfolio_source": "MANUAL"}, ITEM)
    assert bad["eligible"] is False and bad["reason"] == "INVALID_SOURCE" and bad["mode"] == "DISPLAY_ONLY"


def test_buy_sell_mapping_and_hold_none_blocking():
    for action, side in (("ADD", "BUY"), ("INCREASE", "BUY"), ("DECREASE", "SELL"), ("EXIT", "SELL")):
        r = H.evaluate(RUN_OK, {**ITEM, "action": action, "side_hint": side})
        assert r["eligible"] and r["draft"]["side"] == side, action
        assert H.side_for_action(action) == side
    for action in ("HOLD", "NONE", "SUBMIT", None, ""):
        r = H.evaluate(RUN_OK, {**ITEM, "action": action, "side_hint": None})
        assert r["eligible"] is False and r["reason"] == "NO_ACTION" and r["draft"] is None, action
        assert H.side_for_action(action) is None


def test_fail_closed_on_missing_or_inconsistent_data():
    assert H.evaluate(None, ITEM)["reason"] == "MISSING_DATA" and H.evaluate(RUN_OK, None)["reason"] == "MISSING_DATA"
    assert H.evaluate({**RUN_OK, "status": "INSUFFICIENT_CANDIDATES"}, ITEM)["reason"] == "RUN_NOT_VALID"
    assert H.evaluate({**RUN_OK, "status": "TURNOVER_LIMIT_EXCEEDED"}, ITEM)["reason"] == "RUN_NOT_VALID"
    assert H.evaluate(RUN_OK, {**ITEM, "handoff_allowed": 0})["reason"] == "NOT_ALLOWED"
    assert H.evaluate(RUN_OK, {**ITEM, "est_qty_diff": 0})["reason"] == "NO_WHOLE_SHARES"
    assert H.evaluate(RUN_OK, {**ITEM, "est_qty_diff": "2"})["reason"] == "MISSING_DATA"            # a string is never coerced
    assert H.evaluate(RUN_OK, {**ITEM, "est_qty_diff": 2.0})["reason"] == "MISSING_DATA"
    assert H.evaluate(RUN_OK, {**ITEM, "est_qty_diff": True})["reason"] == "MISSING_DATA"
    assert H.evaluate(RUN_OK, {**ITEM, "est_qty_diff": RU.MAX_QTY + 1})["reason"] == "ABOVE_46B_MAX_QTY"
    assert H.evaluate(RUN_OK, {**ITEM, "est_qty_diff": RU.MAX_QTY})["eligible"] is True
    assert H.evaluate(RUN_OK, {**ITEM, "symbol": "aapl"})["reason"] == "INVALID_SYMBOL"
    assert H.evaluate(RUN_OK, {**ITEM, "symbol": ""})["reason"] == "INVALID_SYMBOL"
    assert H.evaluate(RUN_OK, {**ITEM, "side_hint": "SELL"})["reason"] == "SIDE_MISMATCH"             # INCREASE with a SELL hint
    assert H.evaluate(RUN_OK, {**ITEM, "side_hint": None})["eligible"] is True
    for r in (H.evaluate(RUN_OK, {**ITEM, "est_qty_diff": 0}), H.evaluate({**RUN_OK, "portfolio_source": "ROBINHOOD_READ_ONLY"}, ITEM)):
        assert r["draft"] is None and r["reason_text"]


def test_draft_matches_the_frozen_stage_46b_preview_contract():
    from api.routes.alpaca_paper_orders import PreviewBody                                     # the frozen contract, read only
    d = H.evaluate(RUN_OK, ITEM)["draft"]
    body = PreviewBody(**d)
    assert (body.symbol, body.side, body.quantity) == ("AAPL", "BUY", 2)
    assert RU.body_errors(body.symbol, body.side, body.quantity) is None                          # V3 / V4 / V5 of the frozen rules
    with pytest.raises(Exception):
        PreviewBody(symbol="AAPL", side="BUY", quantity=2.5)                                      # fractional shares are not a 4.6B shape
    frac = H.evaluate(RUN_OK, {**ITEM, "current_qty": "2.5", "est_qty_diff": 2})                  # rotation already floors to whole shares
    assert frac["draft"]["quantity"] == 2
    assert set(d) == {"symbol", "side", "quantity"}                                               # nothing else is handed over


# ==================================================================================================================================
# API payloads
# ==================================================================================================================================

def _alpaca_run(api, monkeypatch, positions=None):
    monkeypatch.setattr(PR, "VIEW_FN", lambda: P3.alpaca_view(cash="7875.00", refreshed_at=(P3.NOW - timedelta(minutes=2)).isoformat(),
                                                             positions=positions or [{"symbol": "AMD", "qty": "10", "side": "long"}, {"symbol": "MU", "qty": "2", "side": "long"}]))
    assert api.post(f"{BASE}/snapshot", json={"source": "ALPACA_PAPER_VIEW"}).status_code == 200
    c = P4.cfg(api, exit_rank=3)                                                                 # a tight buffer: the lower-ranked holding exits
    r = P4.run(api, c, source="ALPACA_PAPER_VIEW")
    assert r.status_code == 200, r.text
    return r.json()["run"]


def test_alpaca_run_carries_server_drafts_only_for_actionable_items(api, monkeypatch):
    run = _alpaca_run(api, monkeypatch)
    assert run["status"] == "VALID" and run["handoff_mode"] == "ALPACA_PAPER_PREFILL" and run["source_label"] == "ALPACA PAPER"
    rb = api.get(f"{BASE}/runs/{run['run_id']}/rebalance").json()
    assert rb["handoff_mode"] == "ALPACA_PAPER_PREFILL" and rb["control_label"] == "Prepare Paper Order" and rb["run_status"] == "VALID"
    items = rb["items"]
    assert items and all(set(i["handoff"]) >= {"mode", "eligible", "reason", "draft"} for i in items)
    for i in items:
        h = i["handoff"]
        if i["action"] in ("ADD", "INCREASE", "DECREASE", "EXIT") and i["est_qty_diff"] >= 1 and i["handoff_allowed"]:
            assert h["eligible"] is True and h["draft"] == {"symbol": i["symbol"], "side": "BUY" if i["action"] in ("ADD", "INCREASE") else "SELL",
                                                            "quantity": i["est_qty_diff"]}, i
        else:
            assert h["eligible"] is False and h["draft"] is None, i
    sides = {i["handoff"]["draft"]["side"] for i in items if i["handoff"]["eligible"]}
    assert sides == {"BUY", "SELL"}                                                              # MU exits (SELL), new names enter (BUY)
    assert api.get(f"{BASE}/runs").json()["runs"][0]["handoff_mode"] == "ALPACA_PAPER_PREFILL"
    assert api.get(f"{BASE}/config").json()["handoff_modes"] == {"ALPACA_PAPER_VIEW": "ALPACA_PAPER_PREFILL", "ROBINHOOD_READ_ONLY": "DISPLAY_ONLY",
                                                                 "LOCAL_SIMULATOR": "DISPLAY_ONLY"}


def test_non_valid_alpaca_run_has_no_drafts(api, monkeypatch):
    monkeypatch.setattr(PR, "VIEW_FN", lambda: P3.alpaca_view(cash="7875.00", refreshed_at=(P3.NOW - timedelta(minutes=2)).isoformat()))
    api.post(f"{BASE}/snapshot", json={"source": "ALPACA_PAPER_VIEW"})
    c = P4.cfg(api, max_turnover_per_rotation="0.10")                                           # all cash → turnover limit exceeded
    run = P4.run(api, c, source="ALPACA_PAPER_VIEW").json()["run"]
    assert run["status"] == "TURNOVER_LIMIT_EXCEEDED" and run["handoff_mode"] == "ALPACA_PAPER_PREFILL"
    items = api.get(f"{BASE}/runs/{run['run_id']}/rebalance").json()["items"]
    assert items and all(i["handoff"]["eligible"] is False and i["handoff"]["reason"] in ("RUN_NOT_VALID", "NO_ACTION") for i in items)


def test_robinhood_and_local_runs_are_display_only_with_identical_items(api, monkeypatch):
    import test_paper_45 as T45
    api.post(f"{BASE}/snapshot", json={"source": "ROBINHOOD_READ_ONLY"})
    c = P4.cfg(api)
    rh = P4.run(api, c, source="ROBINHOOD_READ_ONLY").json()["run"]
    assert rh["status"] == "VALID" and rh["handoff_mode"] == "DISPLAY_ONLY" and rh["source_label"] == "ROBINHOOD — READ ONLY"
    rb = api.get(f"{BASE}/runs/{rh['run_id']}/rebalance").json()
    assert rb["handoff_mode"] == "DISPLAY_ONLY" and rb["control_label"] == "Paper handoff unavailable"
    assert rb["explanation"] == "Robinhood portfolio is read-only. No quantity is transferred to Alpaca."
    assert rb["items"] and all(i["handoff"] == {**i["handoff"], "eligible": False, "draft": None, "reason": "SOURCE_DISPLAY_ONLY", "mode": "DISPLAY_ONLY"} for i in rb["items"])
    assert any(i["action"] in ("ADD", "INCREASE", "DECREASE", "EXIT") and i["est_qty_diff"] >= 1 for i in rb["items"])   # actionable, yet no draft
    T45.account(api.lab, cash="50000")
    api.post(f"{BASE}/snapshot", json={"source": "LOCAL_SIMULATOR"})
    ls = P4.run(api, c, source="LOCAL_SIMULATOR").json()["run"]
    rb2 = api.get(f"{BASE}/runs/{ls['run_id']}/rebalance").json()
    assert ls["handoff_mode"] == "DISPLAY_ONLY" and rb2["control_label"] == "Display only" and rb2["explanation"] == "Local simulator results cannot create a broker draft."
    assert all(i["handoff"]["eligible"] is False and i["handoff"]["draft"] is None for i in rb2["items"])
    assert api.provider.calls == ["get_portfolio", "get_positions"]                              # the explicit Robinhood load only


def test_no_new_endpoint_and_no_broker_path(api):
    from api.server import app
    mine = sorted(p for p in app.openapi()["paths"] if p.startswith(BASE))
    assert mine == [f"{BASE}/config", f"{BASE}/configs", f"{BASE}/run", f"{BASE}/runs", f"{BASE}/runs/{{run_id}}", f"{BASE}/runs/{{run_id}}/candidates",
                    f"{BASE}/runs/{{run_id}}/rebalance", f"{BASE}/runs/{{run_id}}/targets", f"{BASE}/snapshot"]
    for p in mine:
        assert not re.search(r"(?i)order|trade|execute|submit|place|cancel|replace|broker|paper|prefill|handoff|preview|confirm", p), p


# ==================================================================================================================================
# static boundaries: the browser handoff and the protected files
# ==================================================================================================================================

def test_handoff_module_is_pure_and_bounded():
    src = (ROOT / "rotation" / "handoff.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"^\s*(from|import)\s+(requests|socket|urllib|http|json|sqlite3|os|sys|alpaca\b|anthropic|agents|portfolio|api|fit|"
                         r"backtest|database|paper\.alpaca_orders|paper\.alpaca_order_writer|paper\.alpaca_order_reads|paper\.alpaca_order_store)\b", code, re.M)
    assert "from paper import alpaca_order_rules as RU" in src and src.count("RU.") == 3         # MAX_QTY (text + check) + SYMBOL_RE
    assert not re.search(r"alpaca_orders\b|alpaca_order_writer|alpaca_order_reads|/v2/|TradingClient|submit|place_?order|requests\.", code)


def test_browser_handoff_reuses_the_frozen_46b_form_and_nothing_else():
    js = (ROOT / "frontend" / "portfolio_rotation.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    # the only Stage 4.6B touch points: the three form inputs (write), the view button (navigation) and the public state (read)
    assert sorted(set(re.findall(r"data-apo-f=\"\$\{k\}\"|data-apo-f=\"[a-z]+\"", code))) == ['data-apo-f="${k}"']
    assert code.count("window.AlpacaOrders.state.requests") == 2 and "window.StrategyFit.setView(\"paper\")" in code
    assert code.count(".click()") == 1 and 'querySelector(\'[data-ppf-view="orders"]\')' in code       # navigation only
    assert 'data-apo="preview"' not in code and 'data-apo="confirm"' not in code and "AlpacaOrders.show" not in code
    assert "/api/alpaca-paper" not in code and "__apoPost" not in code and code.count("fetch(") == 1          # never the 4.6B API
    assert not re.search(r"setTimeout|setInterval|requestAnimationFrame|eval\(|new Function|location\.", code)
    # eligibility is decided from the SERVER's run + item data, never from DOM attributes
    assert 'run.portfolio_source !== ALPACA || run.handoff_mode !== PREFILL || run.status !== "VALID"' in code
    assert "h.eligible !== true || !h.draft" in code and "d.symbol !== it.symbol" in code and 'Number.isInteger(d.quantity)' in code
    assert "a === \"source\") { source = b.dataset.src; clearRun();" in code                       # a new source drops any earlier proposal
    assert "const eligibleDraft = " not in code and "function eligibleDraft(sym)" in code
    assert "b.dataset.quantity" not in code.split("function handoff(sym)")[1].split("function sourceCard")[0]   # the click never reads qty from the DOM
    # the user-visible contract
    for s in ("Prepare Paper Order", "Paper handoff unavailable", "Display only", "Robinhood portfolio is read-only. No quantity is transferred to Alpaca.",
              "Local simulator results cannot create a broker draft.", "ALPACA PAPER", "ROBINHOOD — READ ONLY", "LOCAL SIMULATOR",
              "you review it there, then Preview and Confirm", "PROPOSAL ONLY — NO ORDERS ARE SENT"):
        assert s in js, s
    assert "Paper handoff available after Phase 5" not in js
    assert "MutationObserver" in code and "observer.disconnect()" in code


def test_protected_stage_46a_46b_files_and_stage_45_are_untouched():
    import hashlib
    frozen = {"paper/alpaca_orders.py": "60f4af04", "paper/alpaca_order_rules.py": "ea6b4436", "paper/alpaca_order_reads.py": "8f1efe37",
              "paper/alpaca_order_writer.py": "f6cfe85b", "paper/alpaca_order_store.py": "5ad100be", "database/alpaca_order_migrations.py": "2f66ad55",
              "api/routes/alpaca_paper_orders.py": "36c000f9", "frontend/alpaca_orders.js": "f79bdde5", "frontend/alpaca_orders.css": "1140d946",
              "paper/ALPACA_ORDERS.md": "155d9a91", "tests/alpaca_order_fakes.py": "4aad94b5", "tests/test_alpaca_orders_46b.py": "aa08b62d",
              "tests/paper_endpoints.py": "3197167c", "paper/alpaca_readonly.py": "aa81bb31", "paper/alpaca_view.py": "bcb63760",
              "paper/reconcile.py": "7db057d5", "paper/ALPACA_PAPER.md": "037341b8", "api/routes/alpaca_paper.py": "d6be4b99",
              "frontend/alpaca_paper.js": "805ebf49", "frontend/alpaca_paper.css": "1b4b0122", "tests/trading_client_allowlist.py": "0fed7abe",
              "tests/test_alpaca_paper_46.py": "764c55c1"}
    import subprocess  # noqa: S404 - git read of committed blobs (LF as stored), test only
    for rel, prefix in frozen.items():
        blob = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True).stdout
        assert hashlib.sha256(blob).hexdigest().startswith(prefix), rel                           # the Stage 4.6B freeze manifest
    for rel in ("paper/execution.py", "paper/portfolio.py", "paper/accounting.py", "paper/store.py", "api/routes/paper.py",
                "database/paper_migrations.py", "frontend/paper_portfolio.js"):
        status = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=ROOT, capture_output=True, text=True).stdout
        assert status.strip() == "", rel                                                          # Stage 4.5 untouched in the working tree
