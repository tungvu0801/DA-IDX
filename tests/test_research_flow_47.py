"""Research workflow (research_flow/ + api/routes/research_workflow.py): deterministic shortlist before any LLM call, a
structured fail-closed Claude contract, a local cache, budgets, failure fallbacks and observability. Fully offline: a fake
LLMProvider, the Phase 4 scratch lab / fake gateway, synthetic market data; conftest blocks sockets and tripwires the
Alpaca wires."""
import json
import os
import re
from datetime import timedelta
from pathlib import Path

import pytest

import test_rotation_api_47 as P4
import test_rotation_engine_47 as P3
from agents import gating
from agents import technical_agent as TA
from agents.ai_cache import AIAnalysisCache
from agents.orchestrator import LLMProvider, LLMResponse
from agents.usage_tracker import UsageTracker
from api.routes import research_workflow as RW
from research_flow import contracts as C
from research_flow import orchestrator as O
from research_flow import shortlist as SL
from research_flow.store import ResearchStore

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/research-workflow"
lab = P4.lab
api = P4.api

GOOD = {"summary": "A large semiconductor company whose fortunes track the data-center and gaming cycles.",
        "catalysts": ["New product cycles in accelerators", "Hyperscaler capital spending themes"],
        "risks": ["Cyclical demand swings", "Export-control exposure"],
        "earnings_context": "Reports on a regular quarterly cadence with heavy focus on data-center demand commentary.",
        "news_context": "Coverage centres on AI infrastructure demand and competitive positioning.",
        "confidence_note": "General knowledge of a widely covered company; recent developments may be missing."}


class FakeProvider(LLMProvider):
    def __init__(self, answers=None, fail=False):
        self.answers, self.fail, self.calls, self.prompts, self.model = answers, fail, 0, [], "fake-model"

    def analyze(self, prompt, system_prompt, max_tokens=1400):
        self.calls += 1
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("provider down")
        sym = json.loads(prompt.split("\n\n", 1)[1])["symbol"]
        text = self.answers(sym) if callable(self.answers) else (self.answers or json.dumps(GOOD))
        return LLMResponse(text=text, model=self.model, input_tokens=100, output_tokens=50)

    def chat(self, message, history=None):
        raise NotImplementedError

    def run_tool_loop(self, *a, **k):
        raise NotImplementedError


@pytest.fixture
def fake(monkeypatch):
    """A fresh gated pipeline per test (process cache + usage tracker) and the fake provider wired where Stage 3.8 resolves it."""
    cache, tracker = AIAnalysisCache(), UsageTracker()
    monkeypatch.setattr(gating, "ai_cache", cache)
    monkeypatch.setattr(gating, "usage_tracker", tracker)
    monkeypatch.setattr(O, "usage_tracker", tracker)
    fp = FakeProvider()
    monkeypatch.setattr(TA, "get_provider", lambda: fp)
    monkeypatch.setattr(RW, "NOW_FN", lambda: P3.NOW)
    monkeypatch.setattr(RW, "PROVIDER_FN", None)
    fp.cache, fp.tracker = cache, tracker
    return fp


def rotation_run(api):
    api.post("/api/portfolio-rotation/snapshot", json={"source": "ROBINHOOD_READ_ONLY"})
    c = P4.cfg(api)
    r = P4.run(api, c)
    assert r.status_code == 200 and r.json()["run"]["status"] == "VALID", r.text
    return r.json()["run"]


# ==================================================================================================================================
# deterministic shortlist (pure)
# ==================================================================================================================================

def cands(n=12):
    return [{"symbol": f"S{i:02d}", "eligible": True, "rank": i, "composite": str(100 - i)} for i in range(1, n + 1)] + \
           [{"symbol": "BAD", "eligible": False, "rank": None, "composite": None}]


def test_1_2_shortlist_order_and_top_n():
    entries, skipped = SL.build_shortlist(cands(), [], None)
    assert [e.symbol for e in entries] == ["S01", "S02", "S03", "S04", "S05"] and all(e.reasons == ("TOP_N",) for e in entries)
    assert skipped == [] and [e.rank for e in entries] == [1, 2, 3, 4, 5]
    shuffled = list(reversed(cands()))
    assert [e.symbol for e in SL.build_shortlist(shuffled, [], None)[0]] == ["S01", "S02", "S03", "S04", "S05"]       # order-independent
    entries3, _ = SL.build_shortlist(cands(), [], None, config={"top_n": 3})
    assert [e.symbol for e in entries3] == ["S01", "S02", "S03"]
    with pytest.raises(ValueError):
        SL.build_shortlist(cands(), [], None, config={"top_n": 0})


def test_3_held_deterioration_movers_and_user_symbols_with_cap():
    items = [{"symbol": "S09", "current_qty": "3", "action": "HOLD"}, {"symbol": "S11", "current_qty": "0", "action": "NONE"}]
    prev = {"S09": 4, "S10": 2, "S07": 7, "S11": 9}                       # S09 held, worsened by 5 · S10 moved 8 · S11 moved 2 (not held)
    entries, skipped = SL.build_shortlist(cands(), items, prev, user_symbols=["s12", "ZZZ"], config={"max_symbols_per_research_run": 7})
    reasons = {e.symbol: e.reasons for e in entries}
    assert reasons["S09"] == ("HELD_DETERIORATION", "RANK_MOVER") and reasons["S10"] == ("RANK_MOVER",)
    assert "S11" not in reasons and "S07" not in reasons                  # below both thresholds, not held: not shortlisted
    assert [e.symbol for e in entries] == ["S01", "S02", "S03", "S04", "S05", "S09", "S10"]   # S12 (user) cut by the cap, lowest rank first
    assert skipped == [{"symbol": "ZZZ", "reason": "NOT_RANKED"}, {"symbol": "S12", "reason": "SHORTLIST_CAP"}]
    assert next(e for e in entries if e.symbol == "S09").rank_change == -5 and next(e for e in entries if e.symbol == "S09").position["held"] == "yes"


# ==================================================================================================================================
# contract
# ==================================================================================================================================

REQ = C.ResearchRequest(symbol="AMD", as_of="2026-09-28", run_id="a" * 32, deterministic_rank=1, deterministic_score="91.2", rank_change=2,
                        position_context={"held": "yes", "current_qty": "10", "proposal_action": "INCREASE", "target_weight": "0.316666"})


def test_9_16_17_malformed_or_forbidden_output_fails_closed_and_the_contract_has_no_order_fields():
    ok = C.parse_result(json.dumps(GOOD), REQ, P3.NOW, 24, "m")
    assert ok[0] == "COMPLETE" and ok[1]["symbol"] == "AMD" and ok[1]["stale_after"] == (P3.NOW + timedelta(hours=24)).isoformat(timespec="seconds")
    assert set(ok[1]) == set(C.result_fields()) and ok[1]["deterministic"]["rank"] == 1 and ok[1]["model_metadata"]["prompt_version"] == C.PROMPT_VERSION
    for raw, reason in (("not json at all", "NOT_JSON"), ('{"summary": "x", "catalysts": [', "CUT_OFF"), ("[1, 2]", "NOT_OBJECT"),
                        (json.dumps({**GOOD, "recommendation": "BUY"}), "FORBIDDEN_FIELD:recommendation"),
                        (json.dumps({**GOOD, "target_price": "100"}), "FORBIDDEN_FIELD:target_price"),
                        (json.dumps({**GOOD, "Side": "BUY"}), "FORBIDDEN_FIELD:Side"),
                        (json.dumps({**GOOD, "extra": "x"}), "UNEXPECTED_FIELDS"),
                        (json.dumps({k: v for k, v in GOOD.items() if k != "risks"}), "MISSING_FIELDS"),
                        (json.dumps({**GOOD, "catalysts": ["a"] * 6}), "BAD_SECTION:catalysts"),
                        (json.dumps({**GOOD, "summary": "x" * 601}), "BAD_SECTION:summary"),
                        (json.dumps({**GOOD, "summary": ""}), "BAD_SECTION:summary")):
        status, result, why = C.parse_result(raw, REQ, P3.NOW, 24, "m")
        assert (status, result) == ("WITHHELD", None) and why == reason, raw[:40]
    for text in ("You should buy more shares now.", "The stock will likely rise 20% next quarter.", "Probability of success is high.",
                 "Revenue grew 35% in 2025."):
        status, _, why = C.parse_result(json.dumps({**GOOD, "summary": text}), REQ, P3.NOW, 24, "m")
        assert status == "WITHHELD" and why.startswith("GUARD:"), text                               # Stage 3.8 deterministic guards reused
    assert not {"recommendation", "action", "side", "target_price", "position_size", "quantity", "order"} & set(C.result_fields())
    assert all(f in C.FORBIDDEN_FIELDS for f in ("recommendation", "buy", "sell", "target_price", "position_size", "quantity", "order"))
    assert "confidence_level" in C.FORBIDDEN_FIELDS and "confidence_note" in C.SECTIONS


def test_18_request_payload_carries_no_secret(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SENTINEL_ANTHROPIC_KEY_VALUE")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "SENTINEL_PAPER_SECRET_VALUE")
    payload = json.dumps(REQ.payload())
    assert "SENTINEL" not in payload and "key" not in payload.lower() and "secret" not in payload.lower() and "token" not in payload.lower()
    assert set(REQ.payload()) == {"symbol", "as_of", "run_id", "deterministic_rank", "deterministic_score", "rank_change", "position_context",
                                  "known_event_context", "requested_sections", "request_type", "prompt_version"}
    assert REQ.payload()["known_event_context"] is None                                                   # never invented
    h1, h2 = REQ.context_hash(), C.ResearchRequest(**{**REQ.__dict__, "run_id": "b" * 32}).context_hash()
    assert h1 == h2 and len(h1) == 64                                                                      # the run id does not break caching


# ==================================================================================================================================
# orchestration through the API
# ==================================================================================================================================

def test_4_5_claude_only_on_the_shortlist_and_cache_hits_avoid_calls(api, fake):
    run = rotation_run(api)
    s = api.post(f"{BASE}/shortlist", json={"run_id": run["run_id"]})
    assert s.status_code == 200
    body = s.json()
    assert body["claude_calls"] == 0 and fake.calls == 0 and body["scanned"] == 6 and body["eligible"] == 6
    assert [x["symbol"] for x in body["shortlist"]] == [x["symbol"] for x in body["symbols"]] and len(body["shortlist"]) == 5
    assert all(x["state"] == "NOT_REQUESTED" for x in body["symbols"]) and body["states"] == list(O.STATES)
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"]})
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    assert rep["llm_calls"] == 5 == fake.calls and sorted(rep["researched"]) == sorted(rep["shortlisted"])
    prompted = sorted(json.loads(p.split("\n\n", 1)[1])["symbol"] for p in fake.prompts)
    assert prompted == sorted(rep["shortlisted"]) and len(prompted) == 5                                   # 4: never the 6th symbol
    assert all(x["state"] == "COMPLETE" and x["cache"] == "FRESH" and x["result"]["summary"] for x in r.json()["symbols"])
    assert "5 shortlisted, 5 researched, 0 from cache, 0 skipped, 0 failed" == rep["summary"]
    r2 = api.post(f"{BASE}/research", json={"run_id": run["run_id"]})
    rep2 = r2.json()["report"]
    assert rep2["llm_calls"] == 0 and fake.calls == 5 and sorted(rep2["cache_hits"]) == sorted(rep["shortlisted"])   # 5: cache hit, 0 calls
    assert all(x["state"] == "COMPLETE" and x["cache"] == "HIT" and x["cache_age_min"] == 0 for x in r2.json()["symbols"])
    assert all(s["state"] == "CACHE_HIT" for s in rep2["symbols"])
    res = api.get(f"{BASE}/results/{run['run_id']}").json()
    assert len(res["results"]) == 5 and len(res["batches"]) == 2 and res["batches"][0]["report"]["cache_hits"]
    assert all(x["claude_calls"] == 1 and x["status"] == "COMPLETE" for x in res["results"])


def test_6_7_stale_cache_is_labelled_and_refresh_calls_again_within_its_limit(api, fake, monkeypatch):
    run = rotation_run(api)
    api.post(f"{BASE}/research", json={"run_id": run["run_id"], "settings": {"top_n": 1}})
    assert fake.calls == 1
    sym = api.post(f"{BASE}/shortlist", json={"run_id": run["run_id"], "settings": {"top_n": 1}}).json()["symbols"][0]["symbol"]
    later = P3.NOW + timedelta(hours=25)
    monkeypatch.setattr(RW, "NOW_FN", lambda: later)
    s = api.post(f"{BASE}/shortlist", json={"run_id": run["run_id"], "settings": {"top_n": 1}}).json()["symbols"][0]
    assert s["state"] == "STALE" and s["cache"] == "STALE" and s["cache_age_min"] == 25 * 60 and s["result"]["summary"]   # 6: stale, still shown
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"], "settings": {"top_n": 1}}).json()
    assert r["report"]["llm_calls"] == 1 and fake.calls == 2 and r["symbols"][0]["state"] == "COMPLETE" and r["symbols"][0]["cache"] == "FRESH"
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"], "settings": {"top_n": 1}, "refresh": [sym]}).json()
    assert r["report"]["llm_calls"] == 1 and fake.calls == 3                                                 # 7: refresh bypasses a fresh cache
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"], "settings": {"top_n": 1}, "refresh": [sym]}).json()
    assert r["report"]["llm_calls"] == 1 and fake.calls == 4
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"], "settings": {"top_n": 1}, "refresh": [sym]}).json()
    assert r["report"]["llm_calls"] == 0 and fake.calls == 4 and r["report"]["skipped"] == [{"symbol": sym, "reason": "REFRESH_LIMIT"}]
    assert r["symbols"][0]["state"] == "COMPLETE" and r["symbols"][0]["cache"] == "HIT"                        # the limit keeps the latest note


def test_8_14_claude_failure_leaves_scanner_rotation_and_proposal_untouched(api, fake):
    run = rotation_run(api)
    before = api.get(f"/api/portfolio-rotation/runs/{run['run_id']}").json()
    fake.fail = True
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"]})
    assert r.status_code == 200
    rep = r.json()["report"]
    assert len(rep["failed"]) == 5 and all(f["reason"] == "PROVIDER_ERROR" for f in rep["failed"]) and rep["researched"] == []
    assert all(x["state"] == "FAILED" for x in r.json()["symbols"]) and rep["llm_calls"] == 5                 # one attempt each, no retry loop
    after = api.get(f"/api/portfolio-rotation/runs/{run['run_id']}").json()
    assert after["run"]["proposal_hash"] == before["run"]["proposal_hash"] == run["proposal_hash"] and after["integrity"]["ok"]   # 14
    assert api.get(f"/api/portfolio-rotation/runs/{run['run_id']}/rebalance").json()["items"]
    fake.fail, fake.answers = False, "not json"
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"], "settings": {"top_n": 1}}).json()
    assert r["report"]["failed"][0]["reason"] == "NOT_JSON" and r["symbols"][0]["state"] == "FAILED"            # 9: malformed → withheld
    stored = ResearchStore(Path(api.lab.path)).results_for_run(run["run_id"])
    assert all(x["status"] in ("FAILED", "WITHHELD") and x["result"] is None for x in stored)


def test_8b_no_provider_configured_marks_everything_unavailable_with_zero_calls(api, fake, monkeypatch):
    run = rotation_run(api)
    monkeypatch.setattr(TA, "get_provider", lambda: None)
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"]}).json()
    assert r["report"]["llm_calls"] == 0 and r["report"]["stop_reason"] == "NO_PROVIDER" and fake.calls == 0
    assert [x["state"] for x in r["symbols"]] == ["FAILED"] + ["SKIPPED"] * 4


def test_10_budget_limit_skips_excess_symbols_and_reports_them(api, fake):
    run = rotation_run(api)
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"], "settings": {"max_llm_calls_per_run": 3}}).json()
    rep = r["report"]
    assert rep["llm_calls"] == 3 == fake.calls and len(rep["researched"]) == 3 and [s["reason"] for s in rep["skipped"]] == ["BUDGET", "BUDGET"]
    assert rep["summary"] == "5 shortlisted, 3 researched, 0 from cache, 2 skipped due to budget, 0 failed"
    states = [(x["state"], x["cache"], x.get("reason")) for x in r["symbols"]]
    assert states == [("COMPLETE", "FRESH", None)] * 3 + [("SKIPPED", None, "BUDGET")] * 2                    # ranks 1-3 first, deterministic
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"], "settings": {"max_llm_calls_per_run": 1}}).json()
    assert r["report"]["llm_calls"] == 1 and fake.calls == 4 and r["report"]["skipped"] == [{"symbol": r["symbols"][4]["symbol"], "reason": "BUDGET"}]
    assert [(x["state"], x["cache"]) for x in r["symbols"]] == [("COMPLETE", "HIT")] * 3 + [("COMPLETE", "FRESH"), ("SKIPPED", None)]


def test_10b_hourly_rate_limit_stops_further_calls(api, fake, monkeypatch):
    import config
    run = rotation_run(api)
    monkeypatch.setattr(config, "AI_MAX_CALLS_PER_HOUR", 2)
    r = api.post(f"{BASE}/research", json={"run_id": run["run_id"]}).json()
    assert r["report"]["llm_calls"] == 2 and fake.calls == 2 and [s["reason"] for s in r["report"]["skipped"]] == ["RATE_LIMIT"] * 3


def test_15_strict_bodies_and_status_mapping(api, fake):
    run = rotation_run(api)
    assert api.post(f"{BASE}/shortlist", json={"run_id": run["run_id"], "order": True}).status_code == 422
    assert api.post(f"{BASE}/research", json={"run_id": run["run_id"], "settings": {"top_n": "5"}}).status_code == 422
    assert api.post(f"{BASE}/shortlist", json={"run_id": "0" * 32}).status_code == 404
    assert api.post(f"{BASE}/shortlist", json={"run_id": run["run_id"], "settings": {"top_n": 0}}).status_code == 422
    assert api.get(f"{BASE}/results/{run['run_id']}").json() == {"run_id": run["run_id"], "results": [], "batches": [], "claude_calls": 0}
    assert set(O.STATES) == {"NOT_REQUESTED", "QUEUED", "CACHE_HIT", "RUNNING", "COMPLETE", "STALE", "FAILED", "SKIPPED"}
    assert fake.calls == 0


# ==================================================================================================================================
# safety: static boundaries, append-only persistence, protected files
# ==================================================================================================================================

def test_11_12_13_no_broker_module_import_no_order_path_no_robinhood_write():
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith(BASE)}
    assert mine == {f"{BASE}/shortlist": {"post"}, f"{BASE}/research": {"post"}, f"{BASE}/results/{{run_id}}": {"get"}}
    for p in mine:
        assert not re.search(r"(?i)order|trade|execute|submit|place|cancel|replace|broker|paper|prefill|handoff|preview|confirm", p), p
    files = ["research_flow/__init__.py", "research_flow/contracts.py", "research_flow/shortlist.py", "research_flow/store.py",
             "research_flow/orchestrator.py", "api/routes/research_workflow.py", "database/research_cache_migrations.py"]
    for f in files:
        src = (ROOT / f).read_text(encoding="utf-8")
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"^\s*(from|import)\s+(requests|socket|urllib|http|alpaca\b|rh_gateway|portfolio\b|portfolio\.|paper\b|paper\.|"
                             r"api\.routes\.portfolio\b|subprocess|threading\.Timer|apscheduler)", code, re.M), f
        assert not re.search(r"alpaca_order|alpaca_paper|alpaca_view|alpaca_readonly|TradingClient|/v2/|place_?order|submit_?order|provider_factory|"
                             r"get_positions|get_portfolio|get_orders|robinhood|os\.environ|getenv|\.env\b|eval\(|exec\(|__import__|importlib|"
                             r"setInterval|setTimeout|Scheduler|register\(", code), f
    orch = (ROOT / "research_flow" / "orchestrator.py").read_text(encoding="utf-8")
    assert orch.count("run_gated_agent(") == 1 and "one_request(" in orch and "max_tokens=MAX_TOKENS" in orch        # the Stage 3.8 pipeline, once
    body = orch.split("def research_run")[1]
    assert "while True" not in body and not re.search(r"for\s+\w+\s+in\s+range\(", body) and "max_retries" not in orch   # one attempt per symbol
    js = (ROOT / "frontend" / "portfolio_rotation.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert "/api/research-workflow" in js and js.count("fetch(") == 1 and not re.search(r"recommend|\bbuy now\b|\bsell now\b", code, re.I)
    assert "research notes, not advice" in js and "Build shortlist (0 AI calls)" in js and "Research shortlist" in js


def test_cache_tables_are_additive_and_append_only(tmp_path):
    import sqlite3
    from database import research_cache_migrations as M
    db = tmp_path / "r.db"
    with sqlite3.connect(str(db)) as c:
        c.execute("PRAGMA user_version = 5")
        for _ in range(3):
            M.run_research_cache_migrations(c)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 5
        names = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(M.TABLES) <= names and not any("alpaca" in n or "paper" in n for n in names)
    st = ResearchStore(db)
    st.insert_result(cache_key="a" * 64, symbol="AMD", request_type=C.REQUEST_TYPE, bucket="2026-09-28", context_hash="b" * 64, run_id="c" * 32,
                     status="COMPLETE", reason=None, request={"symbol": "AMD"}, result={"summary": "x"}, model="m", prompt_version=C.PROMPT_VERSION,
                     claude_calls=1, refresh=False, created_at=P3.NOW, stale_after="2026-09-30T12:00:00+00:00")
    assert st.latest("a" * 64)["result"] == {"summary": "x"} and st.latest("f" * 64) is None
    st.insert_batch(run_id="c" * 32, created_at=P3.NOW, report={"summary": "x"})       # a row to protect: row triggers fire per row
    with sqlite3.connect(str(db)) as c:
        for sql in ("UPDATE research_results SET status = 'FAILED'", "DELETE FROM research_results", "DELETE FROM research_batches"):
            with pytest.raises(sqlite3.DatabaseError):
                c.execute(sql)
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("INSERT INTO research_results (cache_key, symbol, request_type, bucket, context_hash, status, request_json, prompt_version, "
                      "claude_calls, refresh, created_at, stale_after) VALUES ('x', 'AMD', 't', '2026-09-28', 'y', 'DONE', '{}', 'v', 0, 0, 'a', 'b')")


def test_protected_files_and_frozen_paths_untouched():
    import hashlib
    import subprocess  # noqa: S404 - git read of committed blobs, test only
    frozen = {"paper/alpaca_orders.py": "391cd2f2", "paper/alpaca_order_rules.py": "ea6b4436", "paper/alpaca_order_reads.py": "8f1efe37",
              "paper/alpaca_order_writer.py": "f6cfe85b", "paper/alpaca_order_store.py": "5ad100be", "database/alpaca_order_migrations.py": "2f66ad55",
              "api/routes/alpaca_paper_orders.py": "36c000f9", "frontend/alpaca_orders.js": "f79bdde5", "paper/alpaca_readonly.py": "aa81bb31",
              "paper/alpaca_view.py": "bcb63760", "api/routes/alpaca_paper.py": "d6be4b99", "rotation/handoff.py": None}
    for rel, prefix in frozen.items():
        blob = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True).stdout
        if prefix:
            assert hashlib.sha256(blob).hexdigest().startswith(prefix), rel
        assert (ROOT / rel).read_bytes().replace(b"\r\n", b"\n") == blob.replace(b"\r\n", b"\n"), rel        # working tree == HEAD
