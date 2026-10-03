"""Stage 2.8C: desktop stock analysis panel (presentation only) + the compact-card decision state (assembly only).

Rendering tests run the REAL frontend/stock_result.js in headless Microsoft Edge, fed with payloads from the REAL
/api/insights/stock-decision route (TestClient + the same fakes as the 2.7G.3 tests). The test page loads only local
files and stubs fetch(), so it never reaches any server and every request the panel makes is recorded. The rendering
tests are skipped when Edge is not installed; the route/static tests always run.
"""
import copy
import hashlib
import json
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from insights import decision as D
from test_stock_result_27g3 import FRESH_ID, client, decide, snapshot  # noqa: F401  (shared route fixture)

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
SR = (FRONT / "stock_result.js").read_text(encoding="utf-8")
CC = (FRONT / "command_center.js").read_text(encoding="utf-8")
DR = (FRONT / "daily_review.js").read_text(encoding="utf-8")
CSS = (FRONT / "command_center.css").read_text(encoding="utf-8")
EDGE = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if Path(p).exists()), None)

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{FRONT}/style.css"><link rel="stylesheet" href="{FRONT}/command_center.css">
<link rel="stylesheet" href="{FRONT}/daily_review.css"></head>
<body><main style="padding:20px"><div id="dash-beginner"><section class="cc-card"><div class="cc-stocks" id="grid"></div></section></div></main>
<script>
window.__f = []; window.__b = []; window.__r = {}; window.__usage = [];
window.fetch = (u, o) => { window.__f.push(String(u)); window.__b.push(o && o.body ? String(o.body) : null);
  const key = String(u).split('?')[0];
  const body = key === '/api/ai/usage' ? { calls: window.__usage.length ? window.__usage.shift() : 0 } : window.__r[key];
  if (body === undefined) return Promise.reject(new Error('unexpected request ' + key));
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(body))) }); };
window.ResearchIds = { ids: {}, mode: 'ok', next_id: null, get(s) { return this.ids[s] || null; }, set(s, id) { this.ids[s] = id; },
  all() { return this.ids; }, async analyzeFull(s) { window.__f.push('RESEARCH:' + s);
    if (this.mode === 'fail') throw new Error('research failed (test)');
    return { analysis_id: this.next_id, narrative: { whats_happening: 'ok' }, catalyst_note: '', risk_explanation: 'ok' }; } };
window.setup = (sym, watch) => { document.getElementById('grid').innerHTML = watch
  ? `<div class="cc-watch-row"><div class="cc-watch-item" data-sym="${sym}"><strong>${sym}</strong><button data-analyze-w="${sym}">Analyze</button></div><div class="cc-confirm-host" data-result-for="${sym}"></div></div>`
  : `<div class="cc-stock cc-stock-compact" data-sym="${sym}" title="${sym} summary"><div class="cc-stock-head"><strong>${sym}</strong></div><div class="cc-actions"><button data-analyze="${sym}">Analyze</button></div><div class="cc-confirm-host" data-result-for="${sym}"></div></div>`; };
</script><script src="{FRONT}/stock_result.js"></script></body></html>"""

FACTS = r"""(() => { const p = document.querySelector('.sa'), q = (s) => p.querySelector(s), t = (s) => (q(s) || {}).innerText || null;
  const lis = (s) => [...p.querySelectorAll(s)].map((e) => e.innerText.trim());
  return {
    status: t('.sa-status'), state: t('.sa-state'), state_font: parseFloat(getComputedStyle(q('.sa-state')).fontSize),
    state_kind: [...q('.sa-view').classList].find((c) => c.startsWith('sa-k-')), view_box: t('.sa-view'),
    research_head: t('.sa-head .sa-hr'), research_tag_class: (q('.sa-head .sa-hr .cc-tag') || {}).className || null,
    position_head: p.querySelectorAll('.sa-head .sa-hr')[1].innerText,
    layers: lis('.sa-layer .sa-lv'), matters: t('.sa-matters'),
    supports: lis('.sa-grid > .sa-col:first-child .cc-li-ok'), cautions: lis('.sa-grid > .sa-col:first-child .cc-li-warn'),
    next: t('.sa-next p'), location: t('.sa-bhead .cc-tag'), scale: !!q('.sa-map'), marker_left: q('.sa-marker') ? q('.sa-marker').style.left : null,
    map_text: q('.sa-map') ? q('.sa-map').innerText.replace(/\n/g, ' ') : null,
    cells: q('.sa-cells3') ? q('.sa-cells3').innerText.replace(/\n/g, ' ') : null, nomap: t('.sa-nomap'),
    strip: Object.fromEntries([...p.querySelectorAll('.sa-strip .sa-cell')].map((c) => [c.querySelector('.cc-label').innerText, c.querySelector('.sa-cv').innerText.trim()])),
    event: t('.sa-evline'), iw_first_label: t('.sa-grid .sa-iw > div:first-child .cc-label'),
    iw_first: lis('.sa-grid .sa-iw > div:first-child li'), weakens: lis('.sa-grid .sa-iw > div:last-child li'),
    conflicts: lis('.sa-conflict'), has_conflict_section: !!q('.sa-conflicts'),
    portfolio: t('.sa-grid > .sa-col:last-child > .sa-block'), text: p.innerText,
    buttons: [...p.querySelectorAll('button')].map((b) => b.innerText.trim()),
    primary: [...p.querySelectorAll('.sa-actions .cc-primary')].map((b) => b.innerText.trim()),
    details_hidden: q('[data-sa-panel="more"]').hidden, full_hidden: q('.cc-full-host').hidden,
    card_body_hidden: getComputedStyle(document.querySelector('.cc-stock-head, .cc-watch-item')).display === 'none',
    head_position: getComputedStyle(q('.sa-head')).position,
    cols_top: [...p.querySelectorAll('.sa-grid > .sa-col')].map((c) => Math.round(c.getBoundingClientRect().top)),
    requests: window.__f.slice() }; })()"""


# ---- headless browser (local file page; fetch stubbed) ---------------------------------------------------------------

class Browser:
    def __init__(self, ws, url):
        self.ws, self.url, self.n = ws, url, 0

    def send(self, method, params=None):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv(timeout=60))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError(d.get("exception", {}).get("description") or d.get("text"))
        return r["result"].get("value")

    def wait(self, expr, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if self.js(expr):
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out: {expr}")


@pytest.fixture(scope="module")
def browser():
    """Module scope: created before the per-test no-network guard; it only talks to the local Edge debug port."""
    if EDGE is None:
        pytest.skip("Microsoft Edge is not installed; rendering tests need a real browser")
    from websockets.sync.client import connect
    tmp = Path(tempfile.mkdtemp(prefix="sa28c-"))
    page = tmp / "panel.html"
    page.write_text(PAGE.replace("{FRONT}", FRONT.as_uri()), encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu", f"--remote-debugging-port={port}",
                             f"--user-data-dir={tmp / 'profile'}", "--no-first-run", "--no-default-browser-check",
                             "--disable-extensions", "--allow-file-access-from-files", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(150):
            try:
                target = next(t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2))
                              if t["type"] == "page")
                break
            except Exception:  # noqa: BLE001 - browser still starting
                time.sleep(0.1)
        if target is None:
            pytest.skip("Edge did not start")
        with connect(target["webSocketDebuggerUrl"], max_size=2 ** 24) as ws:
            b = Browser(ws, page.as_uri())
            b.send("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False})
            yield b
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def open_panel(b, dec, *, watch=False, calls=(5, 5), research_id="feedc0de00", mode="ok", previous_id=None):
    """Runs the real Analyze flow (capacity check -> confirm -> research -> decision) against stubbed responses."""
    sym = dec["symbol"]
    b.send("Page.navigate", {"url": b.url})
    b.wait("document.readyState === 'complete' && !!window.StockResult")
    plan = {"analyze_now": [sym], "calls_for_now": 3, "calls_last_hour": 0, "hourly_limit": 10, "calls_today": 5,
            "daily_limit": 30, "limited_by": None}
    j = json.dumps
    b.js(f"""(() => {{ setup({j(sym)}, {j(watch)});
      window.__r['/api/insights/daily-review/research-plan'] = {j(plan)};
      window.__r['/api/insights/stock-decision'] = {j(dec)};
      window.__usage = {j(list(calls))}; ResearchIds.mode = {j(mode)}; ResearchIds.next_id = {j(research_id)};
      if ({j(previous_id)}) ResearchIds.ids[{j(sym)}] = {j(previous_id)};
      StockResult.analyze({j(sym)}, document.querySelector('[data-result-for="{sym}"]'), {{}}); }})()""")
    b.wait("!!document.querySelector('[data-yes]')")
    b.js("document.querySelector('[data-yes]').click()")
    b.wait("!!document.querySelector('.sa')")
    return b.js(FACTS)


def clicks(b, selector):
    n = b.js("window.__f.length")
    b.js(f"document.querySelector({json.dumps(selector)}).click()")
    return b.js(f"window.__f.slice({n})")


def with_levels(dec, support="keep", resistance="keep", price="keep"):
    d = copy.deepcopy(dec)
    if support != "keep":
        d["decision"]["watch_next"]["support"] = support
    if resistance != "keep":
        d["decision"]["watch_next"]["resistance"] = resistance
    if price != "keep":
        d["decision"]["price"] = price
    return d


# ---- 1–3: current view primary, research separate, market -> stock -> portfolio ----------------------------------------

def test_current_view_is_the_primary_element(client, browser):
    dec = decide(client, "MU", FRESH_ID)
    f = open_panel(browser, dec)
    c = dec["decision"]
    assert f["state"] == c["state"] and f["state_font"] >= 20                        # the largest text in the panel
    assert f["state_kind"] == "sa-k-" + {"ok": "ok", "info": "info", "warn": "warn", "alert": "alert", "bad": "bad", "dim": "dim"}[
        browser.js(f"StockResult.stateKind({json.dumps(c['state'])})")]
    assert "CURRENT VIEW" in f["view_box"] and f["head_position"] == "sticky"      # persistent header keeps the identity
    assert "MU" in browser.js("document.querySelector('.sa-head .sa-sym').innerText")


def test_research_view_stays_separate_from_the_current_view(client, browser):
    dec = decide(client, "MU", FRESH_ID)
    f = open_panel(browser, dec)
    assert "RESEARCH VIEW" in f["research_head"] and "Bullish bias" in f["research_head"]
    assert "Separate from the current view" in f["research_head"] and "cc-info" in f["research_tag_class"]
    assert "Bullish" not in f["view_box"] and dec["decision"]["state"] not in f["research_head"]


def test_market_stock_portfolio_layers_and_why_it_matters(client, browser):
    dec = decide(client, "SNDK")
    f = open_panel(browser, dec)
    lay = dec["decision"]["details"]["layers"]
    assert f["layers"] == [lay["market"], lay["stock"], lay["portfolio"]]
    assert lay["summary"].lower() in f["matters"].lower()                          # existing deterministic wording


# ---- 4–5: capped why lists, existing next step -----------------------------------------------------------------------

def test_why_lists_are_capped_and_the_rest_is_behind_show_details(client, browser):
    dec = decide(client, "MU", FRESH_ID)
    x = dec["decision"]["details"]
    assert len(x["all_supports"]) > 3
    f = open_panel(browser, dec)
    assert len(f["supports"]) == 3 and len(f["cautions"]) <= 3 and f["details_hidden"] is True
    assert clicks(browser, "[data-sa-toggle='more']") == []                        # no request
    more = browser.js("document.querySelector('[data-sa-panel=\"more\"]').innerText")
    for t in x["all_supports"][3:]:                                                  # 2.8E: shown with wording tidy
        assert browser.js(f"StockResult.tidy({json.dumps(re.sub(r'[(].*[)]', '', t).strip(' .'))})") in more


def test_next_step_is_the_existing_decision_wording(client, browser):
    for dec, watch in ((decide(client, "MU", FRESH_ID), False), (decide(client, "AMD", "feedc0de01"), True)):
        f = open_panel(browser, dec, watch=watch)
        assert f["next"] == dec["decision"]["next"]
        assert dec["decision"]["next"] in (D.NEXT_OWNED[dec["decision"]["state"]] if dec["owned"] else
                                           D.NEXT_WATCH[dec["decision"]["state"]]) or \
            dec["decision"]["next"].startswith(D.NEXT_OWNED.get(dec["decision"]["state"], "~"))


# ---- 6–9: price position ---------------------------------------------------------------------------------------------

def test_price_map_uses_existing_support_current_resistance(client, browser):
    dec = decide(client, "MU", FRESH_ID)
    c = dec["decision"]
    f = open_panel(browser, dec)
    s, r, p = float(c["watch_next"]["support"]), float(c["watch_next"]["resistance"]), float(c["price"])
    assert f["scale"] and abs(float(f["marker_left"].rstrip("%")) - (p - s) / (r - s) * 100) < 0.1   # geometry only
    assert "$10.50" in f["map_text"] and "$13.50" in f["map_text"] and "NOW $12.00" in f["map_text"]
    assert f["location"] == c["details"]["evidence"]["price_location"]            # existing label, no new threshold


def test_missing_support_shows_labels_not_a_scale(client, browser):
    f = open_panel(browser, with_levels(decide(client, "MU", FRESH_ID), support=None))
    assert not f["scale"] and "SUPPORT Unavailable" in f["cells"] and "support" in f["nomap"]


def test_missing_resistance_shows_labels_not_a_scale(client, browser):
    f = open_panel(browser, with_levels(decide(client, "MU", FRESH_ID), resistance=None))
    assert not f["scale"] and "RESISTANCE Unavailable" in f["cells"] and "resistance" in f["nomap"]


def test_invalid_geometry_falls_back_to_labels(client, browser):
    dec = decide(client, "MU", FRESH_ID)
    for bad in (with_levels(dec, support="12.50"), with_levels(dec, support="14.00", resistance="13.00")):
        f = open_panel(browser, bad)
        assert not f["scale"] and "outside the recent support" in f["nomap"] and "$12.00" in f["cells"]


# ---- 10–12: momentum, volume, event risk ------------------------------------------------------------------------------

def test_momentum_and_volume_come_from_existing_labels(client, browser):
    dec = decide(client, "MU", FRESH_ID)
    f = open_panel(browser, dec)
    assert f["strip"]["MOMENTUM"] == dec["decision"]["details"]["evidence"]["momentum"]
    assert f["strip"]["VOLUME"] == dec["decision"]["watch_next"]["volume"]
    assert f["strip"]["TREND"] == dec["decision"]["details"]["evidence"]["stock_trend"]


def test_event_risk_loading_and_unavailable_are_never_low(client, browser, monkeypatch):
    from api.routes import daily_review as dr
    from api.routes import portfolio as pr
    dec = decide(client, "MU", FRESH_ID)
    f = open_panel(browser, dec)
    assert f["strip"]["EVENT RISK"] == dec["decision"]["details"]["evidence"]["event_risk"]
    loading = dict(copy.deepcopy(dec), events_note="Event calendars are still loading.")
    f = open_panel(browser, loading)
    assert f["strip"]["EVENT RISK"] == "Loading" and "Events loading" in f["event"]

    def down(sym):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(pr, "event_lookup", down)
    dr._events_inflight.clear()
    unavailable = decide(client, "MU", FRESH_ID)
    assert unavailable["decision"]["details"]["events"]["available"] is False
    f = open_panel(browser, unavailable)
    assert f["strip"]["EVENT RISK"] == "Unavailable" and "Event data unavailable" in f["event"] and "Low" not in f["strip"].values()


# ---- 13–14: portfolio impact (owned) / portfolio fit (not owned) -------------------------------------------------------

def test_owned_portfolio_impact_shows_concentration_once(client, browser):
    dec = decide(client, "SNDK")
    w = f"{float(dec['decision']['position']['weight']):.2f}%"
    f = open_panel(browser, dec)
    assert "YOUR POSITION" in f["portfolio"] and w in f["portfolio"] and "Higher attention" in f["portfolio"]
    assert f["text"].count(w) == 1                                                  # not repeated in why/conflicts
    assert "The position is already large." in f["cautions"]                        # reason kept, number shown once
    assert "Open P&L" in f["position_head"] and "Review" in f["buttons"]


def test_unowned_and_offline_show_no_fake_position(client, browser, monkeypatch):
    dec = decide(client, "AMD", "feedc0de01")
    f = open_panel(browser, dec, watch=True)
    assert dec["owned"] is False and "PORTFOLIO FIT" in f["portfolio"] and "Not currently owned" in f["portfolio"]
    assert "YOUR POSITION" not in f["text"] and "Open P&L" not in f["text"] and "Review" not in f["buttons"]
    assert f["iw_first_label"] == ("WHAT I'M WAITING FOR" if dec["decision"]["waiting_for"] else "SETUP IMPROVES IF")
    import requests
    from api.routes import portfolio as pr
    from pf_fixtures import fake_provider

    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("refused")
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(Down()))
    off = decide(client, "MU", FRESH_ID)
    f = open_panel(browser, off)
    assert off["owned"] is None and "Ownership unknown" in f["portfolio"] and "not assumed safe" in f["portfolio"]
    assert f["layers"][2] == "Unavailable"


# ---- 15–17: improves / weakens, conflicts ------------------------------------------------------------------------------

def test_improves_and_weakens_use_the_existing_lists(client, browser):
    dec = decide(client, "MU", FRESH_ID)
    x = dec["decision"]["details"]
    f = open_panel(browser, dec)
    assert f["iw_first_label"] == "SETUP IMPROVES IF" and f["iw_first"] == x["improves_if"][:3]
    assert f["weakens"] == x["weakens_if"][:3]


def test_conflicts_are_listed_not_averaged(client, browser):
    dec = decide(client, "SNDK")
    k = dec["decision"]["details"]["conflicts"]
    assert k
    f = open_panel(browser, dec)
    assert len(f["conflicts"]) == len(k) and all(any(c["text"] in t for t in f["conflicts"]) for c in k)


def test_no_conflicts_means_no_empty_section(client, browser):
    dec = decide(client, "MU", FRESH_ID)
    assert not dec["decision"]["details"]["conflicts"]
    f = open_panel(browser, dec)
    assert f["has_conflict_section"] is False and "CONFLICTING EVIDENCE" not in f["text"]


# ---- 18–19: freshness, evidence wording ---------------------------------------------------------------------------------

def test_research_freshness_is_obvious(client, browser):
    f = open_panel(browser, decide(client, "MU", FRESH_ID))
    assert "FRESH · just now" in f["research_head"] and f["primary"] == ["Quick check"]
    client.snaps["MU"] = [snapshot("MU", 40)]
    f = open_panel(browser, decide(client, "MU"))
    assert "STALE · 40h" in f["research_head"] and f["primary"] == ["Analyze"]      # Analyze leads when research is old
    f = open_panel(browser, decide(client, "NVDA"))
    assert "RESEARCH NEEDED" in f["research_head"] and f["primary"] == ["Analyze"]
    assert "(0.0 hours old)" not in f["text"]


def test_evidence_percentages_are_not_called_probabilities(client, browser):
    f = open_panel(browser, decide(client, "MU", FRESH_ID))
    assert "not a probability" in f["text"] and "Bullish 60%" in f["text"]
    for src in (SR, f["text"]):
        assert not re.search(r"% chance|chance of|probability of (rising|falling|gain)|likely to (rise|fall)", src, re.I)


# ---- 20–23: requests and AI calls --------------------------------------------------------------------------------------

def test_full_analysis_and_its_tabs_make_no_requests(client, browser):
    f = open_panel(browser, decide(client, "MU", FRESH_ID))
    assert f["full_hidden"] is True
    assert clicks(browser, "[data-full]") == []
    for tab in ("technical", "catalysts", "risks", "market", "portfolio", "evidence", "ai"):
        assert clicks(browser, f"[data-tab='{tab}']") == []
        assert browser.js("document.querySelectorAll('.sa-tabbody').length") == 1   # one section at a time
    assert clicks(browser, "[data-tab='technical']") == []
    tech = browser.js("document.querySelector('.sa-tabbody').innerText")
    assert "RSI" in tech and "EMA 9 / 20 / 50" in tech                               # raw indicators only live here
    assert "RSI" not in browser.js("document.querySelector('.sa-grid').innerText")


def test_cached_result_reports_zero_new_ai_calls(client, browser):
    dec = decide(client, "MU", FRESH_ID)
    f = open_panel(browser, dec, calls=(5, 5))
    assert f["status"].startswith("✓ CURRENT ANALYSIS LOADED · 0 new AI calls")
    assert f["requests"] == ["/api/insights/daily-review/research-plan", "/api/ai/usage", "RESEARCH:MU", "/api/ai/usage",
                             "/api/insights/stock-decision"]                          # exactly one research run
    f = open_panel(browser, dec, calls=(5, 8))
    assert f["status"].startswith("✓ ANALYSIS UPDATED · 3 new AI calls")


def test_collapse_makes_no_request_and_restores_the_card(client, browser):
    f = open_panel(browser, decide(client, "MU", FRESH_ID))
    assert f["card_body_hidden"] is True                                            # identity lives in the panel header
    assert browser.js("document.querySelector('.cc-stock').getAttribute('title')") in (None, "")
    assert clicks(browser, "[data-collapse]") == []
    assert browser.js("!document.querySelector('.sa') && !document.querySelector('.cc-stock').classList.contains('cc-expanded')")
    assert browser.js("document.querySelector('.cc-stock').getAttribute('title')") == "MU summary"


def test_failure_keeps_the_previous_valid_research_visible(client, browser):
    dec = decide(client, "MU", FRESH_ID)
    f = open_panel(browser, dec, mode="fail", previous_id=FRESH_ID)
    assert f["status"].startswith("ANALYSIS COULD NOT BE COMPLETED") and "research failed (test)" in f["status"]
    assert "EXISTING SAVED RESEARCH" in f["research_head"] and "Bullish bias" in f["research_head"]
    bodies = browser.js("window.__b")
    idx = f["requests"].index("/api/insights/stock-decision") - 1                  # RESEARCH stub is not a fetch
    assert json.loads(bodies[idx])["analysis_id"] == FRESH_ID
    assert browser.js("ResearchIds.ids.MU") == FRESH_ID                             # previous id not replaced


# ---- 24–26: safety ---------------------------------------------------------------------------------------------------

def test_no_execution_controls(client, browser):
    f = open_panel(browser, decide(client, "SNDK"))
    assert not [b for b in f["buttons"] if re.search(r"\b(buy|sell|trade|order|execute|place)\b", b, re.I)]
    for src in (SR, CC):
        assert not re.search(r"buy now|sell now|place_?order|submit_?order|setTimeout\(|setInterval\(", src, re.I)


def test_no_broker_mutations(client):
    before = len(client.http.requests)
    client.post("/api/insights/home", json={})
    decide(client, "MU", FRESH_ID)
    calls = client.http.requests[before:]
    assert calls and all(r.get("method", "GET") == "GET" for r in calls)
    assert {r["path"].split("?")[0] for r in calls} <= {"/portfolio", "/positions", "/realized-pnl"} | \
        {r["path"].split("?")[0] for r in calls if r["path"].startswith("/tax-lots/")}
    paths = __import__("api.server", fromlist=["app"]).app.openapi()["paths"]
    from paper_endpoints import paper_exempt
    for path, ops in paths.items():
        if paper_exempt(path, ops):  # Stage 4.5: the exact local paper paths only
            continue
        assert not re.search(r"execute|broker|place|cancel|submit", path), path


def test_no_database_changes(client):
    from conftest import TEST_DB

    def digest():
        conn = sqlite3.connect(str(TEST_DB))
        try:
            h = hashlib.sha256()
            for name, sql in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name"):
                h.update(f"{name}|{sql}".encode())
                if sql and sql.upper().startswith("CREATE TABLE"):
                    cols = ", ".join(f'"{r[1]}"' for r in conn.execute(f'PRAGMA table_info("{name}")'))   # WITHOUT ROWID too
                    for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY {cols}'):
                        h.update(repr(row).encode())
            return h.hexdigest()
        finally:
            conn.close()
    before = digest()
    client.post("/api/insights/home", json={})
    for sym, aid in (("MU", FRESH_ID), ("SNDK", None), ("AMD", "feedc0de01")):
        decide(client, sym, aid)
    assert digest() == before and client.calls == []


# ---- compact-card decision state (assembly only) -------------------------------------------------------------------

def test_home_card_states_equal_the_daily_review_states(client):
    home = client.post("/api/insights/home", json={}).json()
    rep = client.post("/api/insights/daily-review", json={}).json()
    review = {c["symbol"]: (c["state"], c["group"]) for g in rep["portfolio"]["groups"].values() for c in g}
    states = home["decision_states"]
    assert set(states) == {c["symbol"] for c in home["my_stocks"]}
    assert {s: (v["state"], v["group"]) for s, v in states.items()} == {s: review[s] for s in states}
    assert set(next(iter(states.values()))) == {"state", "group"}                   # only the state, not a new report
    assert client.calls == []


def test_card_states_need_no_extra_fetch_scan_or_event_call(client, monkeypatch):
    from api.routes import portfolio as pr
    from insights import service
    from test_market_insights import inputs
    events, scans = [], []
    monkeypatch.setattr(pr, "event_lookup", lambda s: (events.append(s), __import__("e_fixtures").bundle("LOW"))[1])
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: (scans.append(now), inputs(fetched=now))[1])
    service._cache.clear()
    before = len(client.http.requests)
    home = client.post("/api/insights/home", json={}).json()
    held = [c["symbol"] for c in home["my_stocks"]]
    assert home["decision_states"] and sorted(events) == sorted(held)               # one event lookup per holding, as before
    assert len(scans) == 1                                                           # one market scan
    paths = [r["path"].split("?")[0] for r in client.http.requests[before:]]
    assert paths.count("/positions") == 1 and paths.count("/portfolio") == 1        # one Robinhood read


def test_card_renders_the_state_and_never_substitutes_the_attention_badge():
    card = CC[CC.index("function stockCard(c, st)"):CC.index("function myStocks(d)")]
    # 2.8D: no state -> "Current view unavailable"; the attention level is only a small secondary badge
    assert "SR.stateTag(st.state)" in card and "Current view unavailable" in card
    assert "tag(c.attention.level" not in card and 'class="cc-att' in card
    assert "stockCard(c, (d.decision_states || {})[c.symbol])" in CC
    one = CC[CC.index("function analyzeOne("):CC.index("async function analyzeAll(")]
    assert "onResearch: (s, r, dec)" in one and "stateTag(dec.state)" in one        # refreshed in place after Analyze


def test_one_semantic_colour_map_across_screens():
    assert "--cc-ok: var(--green)" in CSS and "--cc-info: var(--accent)" in CSS and "--cc-bad: var(--red)" in CSS
    kinds = dict(re.findall(r'"([A-Z][A-Z /—-]+)": "(ok|info|warn|alert|bad|dim)"', SR[SR.index("const STATE_KIND"):SR.index("const FRESH")]))
    assert set(kinds) == set(D.OWNED_STATES) | set(D.WATCH_STATES)                  # every state has one colour
    assert kinds[D.WEAKENING] == "bad" and kinds[D.SUPPORTED] == "ok" and kinds[D.WAIT_CONF] == "warn" and \
        kinds[D.W_HIGHER] == "alert" and kinds[D.DATA_STALE] == "dim" and kinds[D.MONITOR] == "info"
    assert "window.StockResult.stateKind(s)" in DR                                    # Daily Review uses the same map
    assert 'STALE: "dim"' in CC and 'STALE: "dim"' in DR and 'STALE: "dim"' in SR


def test_three_columns_side_by_side_at_desktop_width(client, browser):
    f = open_panel(browser, decide(client, "SNDK"))
    assert len(f["cols_top"]) == 3 and len(set(f["cols_top"])) == 1
    assert browser.js("document.documentElement.scrollWidth <= innerWidth")
