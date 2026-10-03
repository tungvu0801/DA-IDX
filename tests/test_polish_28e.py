"""Stage 2.8E: final desktop polish & consistency (presentation only — no backend or financial change).

Rendering tests run the REAL dashboard scripts in headless Edge (the 2.8D test page: local files only, fetch stubbed and
recorded), fed with REAL route payloads from the 2.7F fixture. Skipped without Edge; the static tests always run.
"""
import copy
import json
import re
from pathlib import Path

import pytest

from insights import decision as D
from test_command_center_27f import client  # noqa: F401
from test_workspace_28d import clicks, dash, home, offline, render  # noqa: F401  (browser page + helpers)

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
CC = (FRONT / "command_center.js").read_text(encoding="utf-8")
SR = (FRONT / "stock_result.js").read_text(encoding="utf-8")
RB = (FRONT / "research_batch.js").read_text(encoding="utf-8")
DR = (FRONT / "daily_review.js").read_text(encoding="utf-8")
TR = (FRONT / "trader_review.js").read_text(encoding="utf-8")
PB = (FRONT / "portfolio_beginner.js").read_text(encoding="utf-8")
MK = (FRONT / "market.js").read_text(encoding="utf-8")
CSS = (FRONT / "command_center.css").read_text(encoding="utf-8")


def run_batch(dash, client, d):
    """The real "Analyze my holdings" flow in the page: coverage + plan from the real routes, research stubbed."""
    rep = client.post("/api/insights/daily-review", json={}).json()
    cov = rep["portfolio"]["coverage"]
    plan = client.post("/api/insights/daily-review/research-plan", json={"symbols": cov["need"]}).json()
    render(dash, d)
    stubs = {f"/api/stocks/{s}/research": {"analysis_id": "feedc0de00"} for s in cov["need"]}
    dash.js(f"""Object.assign(window.__r, {json.dumps(stubs)}, {{ '/api/insights/daily-review': {json.dumps(rep)},
      '/api/insights/daily-review/research-plan': {json.dumps(plan)} }})""")
    dash.js("document.querySelector('#cc-analyze-all').click()")
    dash.wait("!!document.querySelector('#cc-analyze-host [data-yes], #cc-analyze-host [data-no]')")
    dash.js("(() => { const y = document.querySelector('#cc-analyze-host [data-yes]'); if (y) y.click(); })()")
    dash.wait("!!document.querySelector('.cc-batchbar')")
    return cov, plan


# ---- 1–2: completed research batch collapses; Details holds the existing numbers -------------------------------------

def test_completed_research_batch_collapses_to_one_line(client, dash):
    d = home(client)
    cov, plan = run_batch(dash, client, d)
    bar = dash.js("(() => { const b = document.querySelector('.cc-batchbar'); return { h: b.getBoundingClientRect().height, t: b.innerText }; })()")
    assert bar["h"] < 50 and "RESEARCH COVERAGE" in bar["t"]
    ran = [s for s in plan["analyze_now"]]
    still = [s for s in cov["need"] if s not in ran]
    assert f"{cov['total'] - len(still)} / {cov['total']} current" in bar["t"]
    assert (f"{len(still)} still need research" in bar["t"]) if still else ("Research up to date" in bar["t"])
    assert dash.js("document.getElementById('cc-batch-details').hidden") is True
    assert dash.js("!document.querySelector('.cc-mystocks .dr-batch')")                   # the old tall block is gone
    assert "batchResult.coverage = cov;" in CC and "setTimeout(" not in CC               # nothing continues on its own


def test_status_line_wording_for_up_to_date_and_no_capacity(dash, client):
    render(dash, home(client))
    cov = {"total": 8, "current": 5, "need": ["A", "B", "C"], "plan": {"hourly_limit": 10, "calls_last_hour": 10,
           "daily_limit": 30, "calls_today": 12, "analyze_now": [], "remaining": ["A", "B", "C"]}}
    html = lambda res: dash.js(f"(() => {{ const e = document.createElement('div'); e.innerHTML = ResearchBatch.statusHtml({json.dumps(res)}); return e.querySelector('.cc-batchbar').innerText; }})()")
    assert "8 / 8 current" in html({"updated": ["A", "B", "C"], "still_needed": [], "reason": None, "coverage": cov}) and \
        "Research up to date" in html({"updated": ["A", "B", "C"], "still_needed": [], "reason": None, "coverage": cov})
    t = html({"updated": [], "still_needed": ["A", "B", "C"], "reason": "Hourly AI limit", "coverage": cov})
    assert "5 / 8 current" in t and "3 waiting · Hourly AI limit reached" in t


def test_coverage_details_show_existing_numbers_without_a_request(client, dash):
    cov, plan = run_batch(dash, client, home(client))
    assert clicks(dash, "[data-toggle='cc-batch-details']") == []
    t = dash.js("document.getElementById('cc-batch-details').innerText")
    for label in ("BEFORE THIS RUN", "Hourly limit", "Used this hour", "Daily limit", "Used today", "CAN ANALYZE NOW",
                  "WAIT UNTIL LATER", "Research updated:", "Still needed:", "Nothing continues automatically."):
        assert label in t, label
    assert f"Hourly limit {plan['hourly_limit']}" in t and f"{cov['current']} / {cov['total']} current" in t


# ---- 3: owned watchlist strip -----------------------------------------------------------------------------------------

def test_owned_watchlist_names_are_one_inline_strip(client, dash):
    render(dash, home(client))
    s = dash.js("(() => { const w = document.querySelector('.cc-wowned'); return { h: w.getBoundingClientRect().height, in_head: !!w.closest('.cc-head'), n: w.querySelectorAll('[data-goto]').length, text: w.innerText }; })()")
    assert s["h"] < 40 and s["in_head"] and s["n"] >= 1 and "ALSO WATCHED" in s["text"]
    assert clicks(dash, ".cc-wowned [data-goto]") == []


# ---- 4, 8: semantic colours; price colour independent of the state colour ---------------------------------------------

def test_current_view_colours_follow_one_semantic_map(dash, client):
    render(dash, home(client))
    colors = dash.js("""(() => { const out = {}; for (const k of ['ok', 'warn', 'alert', 'bad', 'dim', 'info']) {
        const e = document.createElement('span'); e.className = 'cc-state cc-' + k; document.body.appendChild(e); out[k] = getComputedStyle(e).color; e.remove(); } return out; })()""")
    assert colors == {"ok": "rgb(63, 185, 80)", "warn": "rgb(210, 153, 34)", "alert": "rgb(227, 119, 46)",
                      "bad": "rgb(248, 81, 73)", "dim": "rgb(139, 148, 158)", "info": "rgb(88, 166, 255)"}
    for state, kind in ((D.WEAKENING, "bad"), (D.PROTECT, "bad"), (D.WAIT_CONF, "warn"), (D.SUPPORTED, "ok"),
                        (D.RESEARCH, "dim"), (D.W_HIGHER, "alert"), (D.MONITOR, "info")):
        assert dash.js(f"StockResult.stateKind({json.dumps(state)})") == kind
    # every screen draws states with the same pill; the Daily Review no longer uses its own outlined tag
    assert DR.count("stateTag(c.state)") == 2 and "tag(c.state, KIND" not in DR
    assert 'LEVEL = { HIGH: "bad", MEDIUM: "alert", LOW: "info" }' in DR
    assert '"BULLISH BIAS": "info"' in PB and 'HIGH: "alert"' in PB and 'WEAKENING: "bad"' in MK and 'Weakening: "bad"' in CC


def test_price_colour_is_independent_of_the_state_colour(client, dash):
    d = copy.deepcopy(home(client))
    c = d["my_stocks"][0]
    c["session_pct"] = 3.1
    d["decision_states"][c["symbol"]] = {"state": D.WEAKENING, "group": "NEEDS ATTENTION"}
    render(dash, d)
    card = f".cc-mystocks .cc-stock[data-sym=\"{c['symbol']}\"]"
    assert dash.js(f"document.querySelector('{card} .cc-stock-head .cc-mv').classList.contains('pct-up')")
    assert dash.js(f"document.querySelector('{card} .cc-state').classList.contains('cc-bad')")      # red state, green move


# ---- 5–7: freshness, "just now", wording --------------------------------------------------------------------------------

def test_freshness_badges_have_one_format(client, dash):
    render(dash, home(client))
    f = lambda label, h: dash.js(f"(() => {{ const e = document.createElement('div'); e.innerHTML = StockResult.freshTag({json.dumps(label)}, {json.dumps(h)}); return e.innerText; }})()")
    assert f("FRESH", 0.2) == "FRESH · 12m" and f("AGING", 9.4) == "AGING · 9h" and f("STALE", 31.6) == "STALE · 31h"
    assert f("MISSING", None) == "RESEARCH NEEDED" and f("UNKNOWN", None) == "UNKNOWN"
    assert f("AGING", 23.7) == "AGING · 23h"                           # rounded down: never looks past the stale limit
    assert "SR.freshTag(r.freshness, r.age_hours)" in TR and "hours old` : \"\"}` : \"No saved research" not in TR


def test_just_now_instead_of_zero_hours(client, dash):
    render(dash, home(client))
    assert dash.js("StockResult.age(0.0)") == "just now" and dash.js("StockResult.age(0.04)") == "just now"
    assert dash.js("StockResult.tidy('Research is Bullish Bias (0.0 hours old).')") == "Research is Bullish Bias (updated just now)."


def test_awkward_wording_is_tidied_without_changing_meaning(client, dash):
    render(dash, home(client))
    t = lambda s: dash.js(f"StockResult.tidy({json.dumps(s)})")
    assert t("Semiconductors stocks are weak this session.") == "Semiconductor stocks are weak this session."
    assert t("Already high semiconductors exposure") == "Already high semiconductor exposure"
    assert t("Semiconductors 73.42% → 74.90%") == "Semiconductors 73.42% → 74.90%"        # the sector name itself stays
    assert t("in 1 trading days") == "in 1 trading day" and t("in 11 days") == "in 11 days" and t("1.0 days") == "1 day"
    assert t("Old research (26.1 h) — refresh") == "Old research (26h) — refresh"
    for src in (DR, TR, PB, MK):
        assert "tidy(" in src                                           # every beginner screen uses the same pass


# ---- 9–10: lower workspace balance, repeated-risk grouping ----------------------------------------------------------------

def test_lower_workspace_columns_are_balanced(client, dash):
    render(dash, home(client))
    cols = dash.js("[...document.querySelectorAll('.cc-lower-grid > *')].map((c) => { const r = c.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.height)]; })")
    assert len(cols) == 3 and len({t for t, _ in cols}) == 1
    hs = [h for _, h in cols]
    assert max(hs) < 2.2 * min(hs)                                      # no tall column beside a near-empty one
    assert dash.js("getComputedStyle(document.querySelector('.cc-lower-grid')).alignItems") == "start"   # natural heights


def test_holdings_with_the_same_state_share_one_risk_row(client, dash):
    d = copy.deepcopy(home(client))
    syms = [c["symbol"] for c in d["my_stocks"]]
    d["decision_states"] = {s: {"state": D.RESEARCH, "group": "STABLE / MONITOR"} for s in syms}
    for s in syms[:2]:
        d["decision_states"][s] = {"state": D.PROTECT, "group": "NEEDS ATTENTION"}
    d["decision_states"][syms[2]] = {"state": D.WEAKENING, "group": "NEEDS ATTENTION"}
    render(dash, d)
    rows = dash.js("[...document.querySelectorAll('.cc-lower-grid > .cc-lcol:first-child .cc-lrow:first-of-type .cc-lline')].map((r) => [r.querySelector('.cc-state').innerText, [...r.querySelectorAll('[data-goto]')].map((b) => b.dataset.goto)])")
    assert sorted(rows) == sorted([[D.PROTECT, sorted(syms[:2])], [D.WEAKENING, [syms[2]]]])       # grouped, never merged


# ---- 11–15: offline, requests, AI, execution, no financial change ---------------------------------------------------------

def test_offline_stays_compact_and_steps_stay_collapsed(client, dash, monkeypatch):
    render(dash, offline(client, monkeypatch))
    assert dash.js("document.querySelector('.cc-rh').getBoundingClientRect().height") < 80
    assert dash.js("document.getElementById('cc-rh-fix').hidden") is True
    assert '<details class="cc-more"><summary>How to reconnect</summary>' in DR                 # Daily Review too


def test_polish_adds_no_request(client, dash):
    cov, _ = run_batch(dash, client, home(client))
    n = dash.js("window.__f.length")
    for sel in ("[data-toggle='cc-batch-details']", "[data-toggle='cc-events-more']", "[data-toggle='cc-pattern-more']",
                "[data-toggle='cc-attn-why']", "[data-toggle='cc-vs-details']", ".cc-wowned [data-goto]"):
        if not dash.js(f"!!document.querySelector({json.dumps(sel)})"):   # e.g. Events "Details" exists only with events
            continue
        assert clicks(dash, sel) == [], sel
    assert dash.js(f"window.__f.length") == n
    render(dash, home(client))
    assert dash.js("window.__f") == ["/api/insights/home"]                                     # page load: one request


def test_no_claude_calls_and_no_execution_controls(client, dash):
    render(dash, home(client))
    client.post("/api/insights/new-money", json={"amount_usd": 500})
    client.post("/api/insights/daily-review", json={})
    assert client.ai_calls == []
    buttons = dash.js("[...document.querySelectorAll('button')].map((b) => b.innerText.trim())")
    assert not [b for b in buttons if re.search(r"\b(buy|sell|trade|order|execute|place)\b", b, re.I)]
    for src in (CC, SR, RB, DR, TR, PB, MK):
        assert not re.search(r"buy now|sell now|place_?order|submit_?order|setTimeout\(|setInterval\(", src, re.I)


def test_wording_tidy_never_changes_numbers_or_states(client, dash):
    """Financial outputs are untouched: tidy() may only change the allowed words — every number, $ value and state
    in the real decision payload survives it unchanged."""
    render(dash, home(client))
    rep = client.post("/api/insights/daily-review", json={}).json()
    texts = []
    for g in rep["portfolio"]["groups"].values():
        for c in g:
            x = c["details"]
            texts += [c["next"], c["state"], *x["all_supports"], *x["all_cautions"], *x["improves_if"], *x["weakens_if"],
                      *[k["text"] for k in x["conflicts"]]]
    out = dash.js(f"{json.dumps(texts)}.map((t) => StockResult.tidy(t))")
    for before, after in zip(texts, out):
        ages = r"\(?\d+(?:\.\d+)? hours old\)?|\(\d+(?:\.\d+)? h\)|\(?\d+[mh] old\)?|\(\d+[mh]\)|\(?updated just now\)?"
        nums = lambda s: re.findall(r"\$?[\d,]+\.?\d*%?", re.sub(ages, "", s))           # ages are the one allowed rewording
        assert nums(before) == nums(after), (before, after)
        if before in D.OWNED_STATES + D.WATCH_STATES:
            assert after == before
