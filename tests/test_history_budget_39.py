"""Stage 3.9 — optional append-only AI explanation history (Part A) and separate AI budgets (Part B).

Reuses the Stage 3.8 labs: a Strategy Fit lab served through the real API and an Evidence lab, both with a FAKE Claude
provider. No real Claude, broker or market-data call is possible here."""
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

import config
import fw_fixtures as FL
from agents import gating
from agents.ai_cache import ai_cache
from agents.usage_tracker import EXPLANATION, RESEARCH, UsageRecord, category_of, usage_tracker
from ai_explain import history as H
from ai_explain import service as AX
from comparison import view as V
from database.explanation_history_migrations import run_explanation_history_migrations
from fit import current as FC
from fit import readonly as RO
from test_explain_38 import D, Fake, _evidence_lab, api, ev_api, evaluate, fresh_ai, ident  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[1]


def rows(path):
    with sqlite3.connect(str(path)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'ai_explanation_history'").fetchone():
            return None
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM ai_explanation_history ORDER BY saved_at, history_id")]


def enable(client, on=True):
    r = client.post("/api/explanation-history/settings", json={"enabled": on})
    assert r.status_code == 200 and r.json()["enabled"] is on


# ================================================================================================================
# PART A — history
# ================================================================================================================

def test_60_history_off_by_default_writes_nothing(api):
    res = evaluate(api, "MU")
    body, _ = ident(res, "Pullback")
    assert api.get("/api/explanation-history/settings").json()["enabled"] is False
    r = api.post("/api/ai-explain/strategy-fit", json=body).json()
    assert r["status"] == "OK" and r["history"] == {"enabled": False, "saved": False, "history_id": None}
    assert rows(api.lab.path) is None                                   # not even the table: 0 history writes
    assert api.get("/api/explanation-history").json()["items"] == []


def test_61_history_on_saves_one_accepted_explanation(api):
    enable(api)
    res = evaluate(api, "MU")
    body, s = ident(res, "Pullback")
    r = api.post("/api/ai-explain/strategy-fit", json=body).json()
    assert r["status"] == "OK" and r["history"]["saved"] is True and r["claude_calls"] == 1
    saved = rows(api.lab.path)
    assert len(saved) == 1
    row = saved[0]
    assert (row["explanation_type"], row["strategy_version_id"], row["symbol"], row["decision_session"]) == \
        ("STRATEGY_FIT_EXPLANATION", s["strategy_version_id"], "MU", res["decision_session"])
    assert row["strategy_id"] == s["strategy_id"] and row["backtest_run_id"] is None and row["forward_journal_id"] is None
    assert (row["input_fingerprint"], row["prompt_version"], row["provider"], row["model"]) == \
        (r["input_fingerprint"], "strategy_explain_v1", "anthropic", config.ANTHROPIC_MODEL)
    assert (row["cache_hit"], row["claude_calls"], row["status"]) == (0, 1, "OK")
    item = api.get(f"/api/explanation-history/{r['history']['history_id']}").json()
    assert item["explanation"] == r["explanation"]                      # exactly what the user was shown
    g = item["grounding_summary"]
    assert g["fit_status"] == "RULES NOT MET" and g["conditions"]["met"] == "1 of 2" and g["label"] == "Pullback v1"
    assert [c["result"] for c in g["condition_trace"]] == ["MET", "NOT MET"]
    for secret in (config.ANTHROPIC_API_KEY or "unset-key", "Authorization", "system_prompt", "DATA, not instructions"):
        assert secret not in str(row)                                   # no key, headers or prompt text is stored


def test_62_cache_hit_adds_no_row_and_no_call(api):
    enable(api)
    res = evaluate(api, "MU")
    body, _ = ident(res, "Pullback")
    first = api.post("/api/ai-explain/strategy-fit", json=body).json()
    again = api.post("/api/ai-explain/strategy-fit", json=body).json()
    assert again["cache_hit"] is True and again["claude_calls"] == 0 and api.fake.calls == 1
    assert again["history"] == {"enabled": True, "saved": False, "history_id": first["history"]["history_id"]}
    assert len(rows(api.lab.path)) == 1


def test_63_new_fingerprint_is_a_new_row_and_the_old_one_is_untouched(api, monkeypatch):
    enable(api)
    res = evaluate(api, "MU")
    body, _ = ident(res, "Pullback")
    first = api.post("/api/ai-explain/strategy-fit", json=body).json()
    before = rows(api.lab.path)
    nxt = FL.at(D("2026-09-28"))                                        # the next session: a changed deterministic input
    monkeypatch.setattr(FC, "_utc", lambda now=None: nxt)
    monkeypatch.setattr(FC, "BAR_CACHE", FC.BarCache())
    api.lab.market.now = nxt
    res2 = evaluate(api, "MU")
    assert res2["decision_session"] != res["decision_session"]
    body2, _ = ident(res2, "Pullback")
    second = api.post("/api/ai-explain/strategy-fit", json=body2).json()
    assert second["input_fingerprint"] != first["input_fingerprint"] and second["history"]["saved"] is True
    after = {r["history_id"]: r for r in rows(api.lab.path)}
    assert len(after) == 2 and after[before[0]["history_id"]] == before[0]   # the older record is byte-for-byte unchanged


def test_64_local_explanation_is_saved_as_local(api):
    enable(api)
    res = evaluate(api, "MU")
    body, s = ident(res, "Cls only")
    assert s["fit_status"] == "OUTSIDE_UNIVERSE"
    pv = api.post("/api/ai-explain/strategy-fit/preview", json=body).json()
    assert pv["mode"] == "LOCAL" and pv["history"]["saved"] is True
    api.post("/api/ai-explain/strategy-fit/preview", json=body)         # shown again: same text, no duplicate
    api.post("/api/ai-explain/strategy-fit", json=body)
    saved = rows(api.lab.path)
    assert len(saved) == 1 and api.fake.calls == 0
    assert (saved[0]["provider"], saved[0]["model"], saved[0]["claude_calls"], saved[0]["status"], saved[0]["prompt_version"]) == \
        ("LOCAL", "NONE", 0, "LOCAL", "NONE")
    item = api.get(f"/api/explanation-history/{saved[0]['history_id']}").json()
    assert item["origin"] == "local" and item["explanation"] == pv["explanation"]


def test_65_failed_withheld_or_unavailable_answers_are_never_saved(api, monkeypatch):
    enable(api)
    res = evaluate(api, "MU")
    body, _ = ident(res, "Pullback")
    api.fake.fail = RuntimeError("provider down")
    assert api.post("/api/ai-explain/strategy-fit", json=body).json()["status"] == "UNAVAILABLE"
    api.fake.fail = None
    monkeypatch.setattr(ai_cache, "_store", {})
    api.fake.reply = '{"summary": "MU meets 1 of 2 saved entry conditions", "what_is_true_now": ["cl'   # cut off
    assert api.post("/api/ai-explain/strategy-fit", json=body).json()["status"] == "WITHHELD"
    monkeypatch.setattr(ai_cache, "_store", {})
    api.fake.reply = {"summary": "You should buy MU now.", "what_is_true_now": [], "what_is_not_met": [], "evidence_context": [],
                      "limitations": ["x"]}
    assert api.post("/api/ai-explain/strategy-fit", json=body).json()["status"] == "WITHHELD"
    assert rows(api.lab.path) == []


def test_66_history_rows_cannot_be_updated_deleted_or_replaced(api):
    enable(api)
    res = evaluate(api, "MU")
    body, _ = ident(res, "Pullback")
    hid = api.post("/api/ai-explain/strategy-fit", json=body).json()["history"]["history_id"]
    before = rows(api.lab.path)
    with sqlite3.connect(str(api.lab.path)) as c:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("UPDATE ai_explanation_history SET structured_explanation_json = '{}' WHERE history_id = ?", (hid,))
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("DELETE FROM ai_explanation_history WHERE history_id = ?", (hid,))
        cols = [r[1] for r in c.execute("PRAGMA table_info(ai_explanation_history)")]
        vals = [before[0][k] for k in cols]
        vals[cols.index("structured_explanation_json")] = '{"summary": "rewritten"}'
        c.execute(f"INSERT OR REPLACE INTO ai_explanation_history ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        c.commit()
    assert rows(api.lab.path) == before                                 # the replace was skipped by the database
    from api.server import app
    mine = {p: set(o) for p, o in app.openapi()["paths"].items() if p.startswith("/api/explanation-history")}
    assert mine == {"/api/explanation-history": {"get"}, "/api/explanation-history/settings": {"get", "post"},
                    "/api/explanation-history/{history_id}": {"get"}}          # no update / delete endpoint exists


def test_history_evidence_rows_use_exact_resolved_ids(monkeypatch, ev_api):
    lab, vid = _evidence_lab(monkeypatch)
    monkeypatch.setattr(RO, "db_path", lambda: Path(lab.path))
    enable(ev_api)
    v = V.view(vid, path=lab.path)
    r = ev_api.post("/api/ai-explain/evidence", json={"strategy_version_id": vid}).json()
    again = ev_api.post("/api/ai-explain/evidence", json={"strategy_version_id": vid, "backtest_run_id": v["selection"]["backtest_run_id"],
                                                          "forward_journal_id": v["selection"]["forward_journal_id"]}).json()
    assert r["status"] == "OK" and again["cache_hit"] is True and ev_api.fake.calls == 1
    saved = rows(lab.path)
    assert len(saved) == 1
    row = saved[0]
    assert (row["backtest_run_id"], row["forward_journal_id"], row["symbol"], row["decision_session"]) == \
        (v["selection"]["backtest_run_id"], v["selection"]["forward_journal_id"], None, None)
    g = H.get(row["history_id"], path=lab.path)["grounding_summary"]
    assert g["historical"]["closed_trades"] == 4 and g["forward"]["completed_reference_cycles"] == 1
    assert g["mfe_mae_tracking"] == "1 tracked of 1 completed cycles" and g["forward"]["continuity"] == "CONTINUOUS"


def test_history_reads_make_no_call_and_leave_the_cache_alone(api):
    enable(api)
    res = evaluate(api, "MU")
    for name in ("Pullback", "Research gate", "Cls only"):
        body, _ = ident(res, name)
        api.post("/api/ai-explain/strategy-fit/preview", json=body)
        api.post("/api/ai-explain/strategy-fit", json=body)
    calls, records, cache = api.fake.calls, list(usage_tracker._records), dict(ai_cache._store)
    listing = api.get("/api/explanation-history").json()
    assert [x["origin"] for x in listing["items"]].count("local") == 1 and len(listing["items"]) == 3
    for f, n in (("kind=fit", 3), ("kind=evidence", 0), ("origin=local", 1), ("origin=claude", 2)):
        assert len(api.get(f"/api/explanation-history?{f}").json()["items"]) == n, f
    for x in listing["items"]:
        api.get(f"/api/explanation-history/{x['history_id']}")
    cursor = api.get("/api/explanation-history?limit=2").json()["next"]
    assert cursor
    page2 = api.get("/api/explanation-history", params={"limit": 2, "before": cursor}).json()
    assert len(page2["items"]) == 1 and page2["next"] is None
    assert (api.fake.calls, usage_tracker._records, ai_cache._store) == (calls, records, cache)
    saved = [x["saved_at"] for x in listing["items"]]
    assert saved == sorted(saved, reverse=True)                          # newest first, never by performance
    for bad in ("/api/explanation-history?limit=500", "/api/explanation-history?kind=ranked", "/api/explanation-history/zz"):
        assert api.get(bad).status_code in (404, 422), bad
    assert api.post("/api/explanation-history/settings", json={"enabled": True, "prompt": "x"}).status_code == 422


def test_history_on_does_not_change_explanations_or_cache_hits(api):
    res = evaluate(api, "MU")
    body, _ = ident(res, "Pullback")
    off = api.post("/api/ai-explain/strategy-fit", json=body).json()
    enable(api)
    pv = api.post("/api/ai-explain/strategy-fit/preview", json=body).json()
    on = api.post("/api/ai-explain/strategy-fit", json=body).json()
    assert pv["cache_hit"] is True and on["cache_hit"] is True and api.fake.calls == 1
    strip = lambda d: {k: v for k, v in d.items() if k not in ("history", "cache_hit", "claude_calls", "generated_at")}  # noqa: E731
    assert strip(off) == strip(on)                                        # same explanation; only the history block differs
    assert on["history"]["saved"] is True                                  # shown (from cache) while ON: recorded once


def test_history_migration_is_additive_and_idempotent(tmp_path):
    lab = FL.FLab(FL.Market(("AMD", "SPY"), start=D("2026-06-01"), vol=0.004))
    with sqlite3.connect(lab.path) as c:
        before = {n: c.execute(f"SELECT count(*) FROM {n}").fetchone()[0]
                  for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        schema0 = sorted(c.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE '%ai_explanation%'"))
        for _ in range(3):
            run_explanation_history_migrations(c)
        after = {n: c.execute(f"SELECT count(*) FROM {n}").fetchone()[0]
                 for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        schema1 = sorted(c.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE '%ai_explanation%'"))
    assert set(after) - set(before) == {"ai_explanation_settings", "ai_explanation_history"}
    assert {k: after[k] for k in before} == before and schema0 == schema1
    with sqlite3.connect(lab.path) as c:
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO ai_explanation_settings VALUES ('forward_auto_capture_enabled', 'true', 'x')")


def test_history_is_never_read_by_financial_code():
    readers = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith((".venv", "tests/")) or "__pycache__" in rel:
            continue
        if re.search(r"ai_explanation_history|ai_explain\.history|ai_explain import history", p.read_text(encoding="utf-8", errors="ignore")):
            readers.append(rel)
    assert sorted(readers) == ["ai_explain/history.py", "api/routes/ai_explain.py", "api/routes/explanation_history.py",
                               "database/explanation_history_migrations.py"]


# ================================================================================================================
# PART B — separate budgets
# ================================================================================================================

def _research_call(provider, symbol="AMD"):
    return gating.run_gated_agent(symbol=symbol, price=100.0, attention_score=100, signal="x", analysis_type="technical_analysis",
                                  system_prompt="s", get_provider_fn=lambda: provider, build_prompt_fn=lambda: "p",
                                  user_requested=True, max_tokens=50)


def test_budget_categories():
    assert category_of("strategy_explanation:strategy_explain_v1:m:abc") == EXPLANATION
    assert category_of("evidence_explanation:evidence_explain_v2:m:abc") == EXPLANATION
    for t in ("technical_analysis", "chat", "research_view", "portfolio_explanation", None, ""):
        assert category_of(t) == RESEARCH, t


def test_67_explanation_call_uses_only_the_explanation_budget(api):
    res = evaluate(api, "MU")
    body, _ = ident(res, "Pullback")
    r0, x0 = usage_tracker.budget(RESEARCH), usage_tracker.budget(EXPLANATION)
    pv = api.post("/api/ai-explain/strategy-fit/preview", json=body).json()
    assert pv["budget"]["category"] == "explanation" and pv["budget"]["used_today"] == x0["used_today"]
    api.post("/api/ai-explain/strategy-fit", json=body)
    r1, x1 = usage_tracker.budget(RESEARCH), usage_tracker.budget(EXPLANATION)
    assert x1["used_today"] == x0["used_today"] + 1 and x1["used_last_hour"] == x0["used_last_hour"] + 1
    assert r1 == r0
    from insights.daily_review import ai_usage_counts
    assert ai_usage_counts(usage_tracker) == {"last_hour": 0, "today": 0}           # research capacity untouched
    u = api.get("/api/ai/usage").json()
    assert u["calls"] == 0 and u["budgets"]["explanation"]["used_today"] == 1 and u["budgets"]["research"]["used_today"] == 0


def test_68_research_call_uses_only_the_research_budget(fresh_ai):
    x0 = usage_tracker.budget(EXPLANATION)
    out = _research_call(Fake("fine"))
    assert "fine" in out
    assert usage_tracker.budget(RESEARCH)["used_today"] == 1 and usage_tracker.budget(EXPLANATION) == x0


def test_69_cache_hits_and_local_explanations_consume_no_budget(api):
    res = evaluate(api, "MU")
    body, _ = ident(res, "Pullback")
    api.post("/api/ai-explain/strategy-fit", json=body)
    snap = usage_tracker.budgets()
    api.post("/api/ai-explain/strategy-fit", json=body)                                 # cache hit
    local, _ = ident(res, "Cls only")
    api.post("/api/ai-explain/strategy-fit/preview", json=local)
    api.post("/api/ai-explain/strategy-fit", json=local)                                # local explanation
    assert usage_tracker.budgets() == snap and api.fake.calls == 1


def test_70_each_budget_runs_out_independently(api, monkeypatch):
    res = evaluate(api, "MU")
    body, _ = ident(res, "Pullback")
    monkeypatch.setattr(config, "AI_EXPLANATION_DAILY_LIMIT", 1)
    api.post("/api/ai-explain/strategy-fit", json=body)
    gate, _ = ident(res, "Research gate")
    pv = api.post("/api/ai-explain/strategy-fit/preview", json=gate).json()
    assert pv["available"] is False and pv["expected_claude_calls"] == 0
    assert pv["message"].startswith("AI explanation limit reached. The deterministic result is still available.")
    r = api.post("/api/ai-explain/strategy-fit", json=gate).json()
    assert r["status"] == "UNAVAILABLE" and r["claude_calls"] == 0 and r["message"].startswith("AI explanation limit reached.")
    assert evaluate(api, "MU")["strategies"]                                           # Strategy Fit itself still works
    assert usage_tracker.can_call() == (True, None)                                     # Research is unaffected ...
    assert "fine" in _research_call(Fake("fine"))
    monkeypatch.setattr(config, "AI_EXPLANATION_DAILY_LIMIT", 30)
    monkeypatch.setattr(config, "AI_MAX_CALLS_PER_DAY", 1)                              # ... and the other way round
    assert usage_tracker.can_call()[0] is False and "unavailable right now" in _research_call(Fake("again"), symbol="NVDA")
    assert api.post("/api/ai-explain/strategy-fit", json=gate).json()["status"] == "OK"


def test_budget_double_click_consumes_one_explanation_call(api):
    res = evaluate(api, "MU")
    body, _ = ident(res, "Pullback")
    api.fake.delay = 0.5
    barrier, out = threading.Barrier(2), []

    def go():
        barrier.wait()
        out.append(api.post("/api/ai-explain/strategy-fit", json=body).json())
    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert api.fake.calls == 1 and usage_tracker.budget(EXPLANATION)["used_today"] == 1


def test_research_limit_text_and_counting_rule_are_unchanged(fresh_ai, monkeypatch):
    monkeypatch.setattr(config, "AI_MAX_CALLS_PER_HOUR", 2)
    now = datetime.now(timezone.utc)
    for t in ("technical_analysis", "chat"):
        usage_tracker.record(UsageRecord(timestamp=now, symbol="X", request_type=t, model="m"))
    usage_tracker.record(UsageRecord(timestamp=now, symbol="X", request_type="chat", model="m", success=False))
    for _ in range(5):
        usage_tracker.record(UsageRecord(timestamp=now, symbol="X", request_type="strategy_explanation:v:m:f", model="m"))
    assert usage_tracker.can_call() == (False, "Hourly AI call limit reached (2/hour).")
    assert usage_tracker.summary_today()["calls"] == 3                               # research records only (incl. failed)
    assert usage_tracker.can_call(EXPLANATION) == (True, None)


def test_budget_ui_is_compact_and_separate():
    app = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "ai_explain.js").read_text(encoding="utf-8")
    assert "AI · Research ${r.used_today}/${r.daily_limit} · Explain ${x.used_today}/${x.daily_limit}" in app
    assert "Explanation budget: ${esc(p.budget.used_today)} / ${esc(p.budget.daily_limit)} used today" in js
    assert "(separate from Research)" in js and "Research" not in js.split("Explanation budget:")[0][-200:]
    cfg = (ROOT / "config.py").read_text(encoding="utf-8")
    for k in ("AI_RESEARCH_HOURLY_LIMIT", "AI_RESEARCH_DAILY_LIMIT", "AI_EXPLANATION_HOURLY_LIMIT", "AI_EXPLANATION_DAILY_LIMIT"):
        assert cfg.count(f'"{k}"') == 1, k                                          # each limit is read in ONE place


def test_history_ui_is_escaped_explicit_and_timer_free():
    js = (ROOT / "frontend" / "ai_history.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert html.index("ai_explain.js") < html.index("ai_history.js") and "ai_history.css" in html and 'id="axh-body"' in html
    assert 'addWorkspace({ id: "history", label: "AI history"' in js and "Enable history" in js and "History saving" in js
    assert not re.search(r"setTimeout|setInterval|eval\(|new Function|/api/ai-explain|method: \"DELETE\"|method: \"PUT\"", js)
    assert js.count("fetch(") == 1 and "/api/explanation-history" in js
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert not re.search(r"(?i)recommend|trade idea|best action|\bbuy\b|\bsell\b|\bscore\b|confidence|probabilit|\brank", code)
    raw = set(re.findall(r"\$\{[xdgcfh]\.[a-z_]+\}", code))          # stored data is interpolated through esc() ...
    assert raw == {"${h.closed_trades}", "${f.completed_reference_cycles}", "${x.continuity}"}  # ... passed on to esc()/kv()
    assert "innerHTML +=" not in code and code.count("root.innerHTML =") == 2
