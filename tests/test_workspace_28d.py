"""Stage 2.8D: desktop portfolio + watchlist workspace (presentation only; one additive, read-only home field).

Rendering tests run the REAL frontend/command_center.js (+ stock_result.js) in headless Microsoft Edge at 1920x1080,
fed with REAL /api/insights/home and /api/insights/new-money responses (TestClient + the 2.7F route fakes). The page
loads only local files and stubs fetch(), so every request the dashboard makes is recorded. Skipped without Edge.
"""
import copy
import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from test_command_center_27f import client  # noqa: F401  (2.7F route fixture: 4 holdings, watchlist NVDA + AMD)
from test_stock_view_28c import EDGE, Browser

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
CC = (FRONT / "command_center.js").read_text(encoding="utf-8")
CSS = (FRONT / "command_center.css").read_text(encoding="utf-8")

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="{FRONT}/style.css"><link rel="stylesheet" href="{FRONT}/command_center.css">
<link rel="stylesheet" href="{FRONT}/daily_review.css"></head>
<body><main style="padding:20px"><div id="dash-beginner"></div></main>
<script>
window.__f = []; window.__r = {}; window.__nav = [];
window.fetch = (u, o) => { window.__f.push(String(u)); const key = String(u).split('?')[0]; const body = window.__r[key];
  if (body === undefined) return Promise.reject(new Error('unexpected request ' + key));
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(JSON.stringify(body))) }); };
window.TraderReview = { quick: (s) => window.__nav.push('quick:' + s), openPosition: (s) => window.__nav.push('review:' + s),
  patterns: () => window.__nav.push('patterns') };
try { sessionStorage.clear(); } catch (e) {}
</script><script src="{FRONT}/research_batch.js"></script><script src="{FRONT}/stock_result.js"></script>
<script src="{FRONT}/command_center.js"></script></body></html>"""

CARDS = r"""(() => { const root = document.getElementById('dash-beginner'), R = (e) => e.getBoundingClientRect();
  const rows = (sel) => { const m = new Map(); [...root.querySelectorAll(sel)].filter((e) => e.offsetParent !== null)
    .forEach((e) => { const t = Math.round(R(e).top); m.set(t, (m.get(t) || 0) + 1); }); return [...m.values()]; };
  return { rows: rows('.cc-mystocks .cc-stocks > .cc-stock'), wrows: rows('.cc-wgrid > *'),
    states: Object.fromEntries([...root.querySelectorAll('.cc-mystocks .cc-stock')].map((c) => [c.dataset.sym, c.querySelector('.cc-state-line').innerText.replace('CURRENT VIEW', '').trim()])),
    att: [...root.querySelectorAll('.cc-mystocks .cc-att')].map((a) => ({ text: a.innerText, font: parseFloat(getComputedStyle(a).fontSize),
      color: getComputedStyle(a).color, in_state: !!a.closest('.cc-state-line') })),
    state_font: [...root.querySelectorAll('.cc-mystocks .cc-state')].map((e) => parseFloat(getComputedStyle(e).fontSize)),
    dim_color: getComputedStyle(document.documentElement).getPropertyValue('--text-dim').trim(),
    sections: [...root.querySelectorAll(':scope > .cc > section')].map((s) => (s.querySelector('h2') || {}).innerText.split('\n')[0]),
    overflow: document.documentElement.scrollWidth > innerWidth + 1, requests: window.__f.slice() }; })()"""


@pytest.fixture(scope="module")
def dash():
    """Module scope: created before the per-test no-network guard; it only talks to the local Edge debug port."""
    if EDGE is None:
        pytest.skip("Microsoft Edge is not installed; rendering tests need a real browser")
    from websockets.sync.client import connect
    tmp = Path(tempfile.mkdtemp(prefix="ws28d-"))
    page = tmp / "dash.html"
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


def home(client):
    d = client.post("/api/insights/home", json={}).json()
    assert d["robinhood"]["connected"] is True and d["decision_states"]
    return d


def render(b, payload, new_money=None):
    b.send("Page.navigate", {"url": b.url})
    b.wait("document.readyState === 'complete' && !!window.CommandCenter")
    b.js(f"window.__r['/api/insights/home'] = {json.dumps(payload)}; window.__r['/api/insights/new-money'] = {json.dumps(new_money)};")
    b.js("CommandCenter.render(document.getElementById('dash-beginner'), false)")
    b.wait("!!document.querySelector('#dash-beginner .cc-hero')")
    return b.js(CARDS)


def clicks(b, selector):
    n = b.js("window.__f.length")
    b.js(f"document.querySelector({json.dumps(selector)}).click()")
    return b.js(f"window.__f.slice({n})")


def with_holdings(d, n):
    """n holdings for the grid test: the real cards cloned under new symbols (presentation input only)."""
    d = copy.deepcopy(d)
    base = d["my_stocks"]
    cards, states = [], {}
    for i in range(n):
        c = copy.deepcopy(base[i % len(base)])
        c["symbol"] = f"H{i}"
        cards.append(c)
        states[c["symbol"]] = d["decision_states"][base[i % len(base)]["symbol"]]
    d["my_stocks"], d["decision_states"] = cards, states
    return d


# ---- 1–2: portfolio vs market -----------------------------------------------------------------------------------------

def test_portfolio_vs_market_is_a_compact_strip(client, dash):
    d = home(client)
    f = render(dash, d)
    v = d["vs_market"]
    strip = dash.js("document.querySelector('.cc-mystocks .cc-pt').innerText")
    label = "PORTFOLIO TODAY" if d["session"]["is_today"] else "PORTFOLIO · LAST SESSION"   # 2.8E: session wording
    assert label in strip and "vs SPY" in strip and "vs QQQ" in strip
    assert f"{abs(float(v['portfolio_pct'])):.2f}%" in strip and f"{abs(float(v['diff_vs_spy_pp'])):.2f} pts" in strip
    assert dash.js("document.getElementById('cc-vs-details').hidden") is True
    # the detail comes AFTER the cards, so it never interrupts the flow into My Stocks
    assert dash.js("!!(document.querySelector('.cc-mystocks .cc-stocks').compareDocumentPosition(document.getElementById('cc-vs-details')) & Node.DOCUMENT_POSITION_FOLLOWING)")
    assert f["sections"][:4] == ["TODAY'S MARKET", "MY ROBINHOOD", "WHAT NEEDS ATTENTION", "MY STOCKS TODAY"]


def test_why_is_it_different_opens_the_existing_detail_without_a_request(client, dash):
    d = home(client)
    render(dash, d)
    assert clicks(dash, "[data-toggle='cc-vs-details']") == []
    detail = dash.js("document.getElementById('cc-vs-details').innerText")
    assert "HELPED MOST" in detail and "HURT MOST" in detail
    for line in d["vs_market"]["why_it_may_differ"]:
        assert line in detail
    assert dash.js("document.querySelector('[data-toggle=\"cc-vs-details\"]').innerText") == "Why is it different?"


# ---- 3–5: holdings grid, card state, secondary attention -----------------------------------------------------------------

def test_holdings_fill_balanced_desktop_rows(client, dash):
    d = home(client)
    for n, rows in ((8, [4, 4]), (7, [4, 3]), (6, [3, 3]), (4, [4]), (3, [3])):
        assert render(dash, with_holdings(d, n))["rows"] == rows, n
    f = render(dash, with_holdings(d, 8))
    assert not f["overflow"]
    bottom = dash.js("Math.max(...[...document.querySelectorAll('.cc-mystocks .cc-stock')].map((c) => c.getBoundingClientRect().bottom))")
    assert bottom < 1080 + 700                                                          # two compact rows, not a long list


def test_cards_show_the_deterministic_state_or_say_it_is_unavailable(client, dash):
    d = home(client)
    f = render(dash, d)
    assert f["states"] == {s: v["state"] for s, v in d["decision_states"].items()}
    missing = copy.deepcopy(d)
    missing["decision_states"] = None
    f = render(dash, missing)
    assert set(f["states"].values()) == {"Current view unavailable"}                  # never the attention badge instead


def test_attention_badge_is_secondary(client, dash):
    f = render(dash, home(client))
    assert f["att"], "fixture has holdings with a non-normal attention level"
    assert all(a["font"] <= 10.5 and not a["in_state"] for a in f["att"])
    assert all(a["font"] < s for a in f["att"] for s in f["state_font"])
    assert re.search(r"\.cc-att \{[^}]*color: var\(--cc-dim\)", CSS)                   # grey text, small coloured dot


# ---- 6–8: watchlist ------------------------------------------------------------------------------------------------------

def test_watchlist_separates_unowned_candidates_from_owned_names(client, dash):
    d = home(client)
    owned = [w["symbol"] for w in d["watchlist"] if w["owned"] is True]
    unowned = [w["symbol"] for w in d["watchlist"] if w["owned"] is False]
    assert owned and unowned
    render(dash, d)
    assert dash.js("[...document.querySelectorAll('.cc-watch-item')].map((e) => e.dataset.sym)") == unowned
    assert dash.js("[...document.querySelectorAll('.cc-wowned [data-goto]')].map((e) => e.dataset.goto)") == owned
    assert "WATCHLIST — NOT OWNED" in dash.js("document.querySelector('.cc-watchlist h2').innerText")


def test_unowned_watchlist_uses_a_compact_grid(client, dash):
    d = home(client)
    render(dash, d)
    g = dash.js("""(() => { const g = document.querySelector('.cc-wgrid'), c = g.querySelector('.cc-wcard');
      return { display: getComputedStyle(g).display, grid_w: g.getBoundingClientRect().width, card_w: c.getBoundingClientRect().width,
               card_h: c.getBoundingClientRect().height, text: c.innerText }; })()""")
    assert g["display"] == "grid" and g["card_w"] < g["grid_w"] / 2 and g["card_h"] < 160      # not a full-width row
    ctx = d["watchlist_context"][next(w["symbol"] for w in d["watchlist"] if w["owned"] is False)]
    assert "Not owned" in g["text"] and "CURRENT VIEW" in g["text"] and "Check setup" in g["text"]
    assert f"Trend" in g["text"] and ctx["trend"] in g["text"]


def test_owned_watchlist_names_are_not_repeated_as_cards(client, dash):
    render(dash, home(client))
    for s in dash.js("[...document.querySelectorAll('.cc-mystocks .cc-stock')].map((c) => c.dataset.sym)"):
        assert dash.js(f"document.querySelectorAll('.cc-watch-item[data-sym=\"{s}\"]').length") == 0
    chip = dash.js("document.querySelector('.cc-wowned [data-goto]').dataset.goto")
    assert clicks(dash, f".cc-wowned [data-goto='{chip}']") == []                       # jump to the card, no request
    assert dash.js(f"document.querySelector('.cc-mystocks .cc-stock[data-sym=\"{chip}\"]').classList.contains('cc-flash')")


# ---- 9–11: opportunities, lower workspace, pattern ---------------------------------------------------------------------

def test_opportunities_show_existing_groups_compactly(client, dash):
    nm = client.post("/api/insights/new-money", json={"amount_usd": 500}).json()
    render(dash, home(client), new_money=nm)
    assert clicks(dash, "#cc-nm-run") == ["/api/insights/new-money"]                    # the one existing request
    dash.wait("!!document.querySelector('#cc-nm-out .cc-cand')")
    shown = dash.js("[...document.querySelectorAll('#cc-nm-out .cc-cand')].map((c) => c.querySelector('.cc-sym').innerText)")
    full = [(g, [c["symbol"] for c in lst]) for g, lst in nm["groups"].items() if lst]
    assert shown == [s for _, lst in full for s in lst]                                  # order as returned (alphabetical)
    if len(full) == 1:
        assert f"All {len(full[0][1])} names are in this group." in dash.js("document.querySelector('.cc-ngroup-line').innerText")
    else:
        assert dash.js("getComputedStyle(document.querySelector('.cc-ngroups')).gridTemplateColumns.split(' ').length") == len(full)
    assert dash.js("Math.max(...[...document.querySelectorAll('#cc-nm-out .cc-cand')].map((c) => c.getBoundingClientRect().height))") < 120


def test_risks_events_watch_next_sit_side_by_side(client, dash):
    d = home(client)
    render(dash, d)
    # 2.8E layout: RISKS | EVENTS over WATCH NEXT | TRADING PATTERN, natural heights, same top row
    cols = dash.js("[...document.querySelectorAll('.cc-lower-grid > *')].map((c) => [[...c.querySelectorAll('h3')].map((h) => h.innerText).join('+'), Math.round(c.getBoundingClientRect().top)])")
    assert [c[0] for c in cols] == ["RISKS", "EVENTS+WATCH NEXT", "TRADING PATTERN"] and len({c[1] for c in cols}) == 1
    watch = dash.js("document.querySelectorAll('.cc-lower-grid .cc-wchip').length")
    assert watch <= 4
    attn = [s for s, v in d["decision_states"].items() if v["group"] == "NEEDS ATTENTION"]
    risk = dash.js("document.querySelector('.cc-lower-grid > .cc-lcol').innerText")
    assert all(s in risk for s in attn) and ("NEEDS ATTENTION" in risk)


def test_trading_pattern_is_a_compact_context_card(client, dash):
    d = home(client)
    render(dash, d)
    card = dash.js("document.querySelector('.cc-patterncard').innerText")
    assert f"{d['pattern']['count']} / {d['pattern']['of']}" in card and "Context only" in card and "not a prediction" in card
    assert dash.js("document.getElementById('cc-pattern-more').hidden") is True        # long text behind "More"
    assert clicks(dash, "#cc-pattern-review") == [] and dash.js("window.__nav") == ["patterns"]
    assert dash.js("document.querySelector('.cc-patterncard').getBoundingClientRect().height") < 260


# ---- 12–13: Robinhood offline ----------------------------------------------------------------------------------------

def offline(client, monkeypatch):
    import requests
    from api.routes import portfolio as pr
    from pf_fixtures import fake_provider

    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("refused")
    monkeypatch.setattr(pr, "provider_factory", lambda: fake_provider(Down()))
    d = client.post("/api/insights/home", json={}).json()
    assert d["robinhood"]["connected"] is False
    return d


def test_robinhood_offline_is_one_compact_line(client, dash, monkeypatch):
    render(dash, offline(client, monkeypatch))
    h = dash.js("document.querySelector('.cc-rh').getBoundingClientRect().height")
    assert h < 80 and dash.js("document.getElementById('cc-rh-fix').hidden") is True
    assert clicks(dash, "[data-toggle='cc-rh-fix']") == []
    assert "HOW TO FIX" in dash.js("document.getElementById('cc-rh-fix').innerText")    # steps still available


def test_offline_ownership_is_unknown_never_guessed(client, dash, monkeypatch):
    d = offline(client, monkeypatch)
    render(dash, d)
    items = dash.js("[...document.querySelectorAll('.cc-watch-item')].map((e) => e.innerText)")
    assert len(items) == len(d["watchlist"]) and all("Ownership unknown" in t for t in items)
    assert not any("Not owned" in t or "You own this" in t for t in items)
    assert dash.js("document.querySelectorAll('.cc-wowned').length") == 0
    assert "holding risks are unknown" in dash.js("document.querySelector('.cc-lower-grid').innerText")


# ---- 14–17: requests, AI, execution, backend ---------------------------------------------------------------------------

def test_page_load_and_workspace_interactions_make_no_extra_requests(client, dash):
    f = render(dash, home(client))
    assert f["requests"] == ["/api/insights/home"]                                       # one request for the whole page
    for sel in ("[data-toggle='cc-vs-details']", "[data-toggle='cc-layers-panel']", "[data-toggle='cc-attn-why']",
                "[data-toggle='cc-news-panel']", "[data-toggle='cc-pattern-more']", ".cc-wowned [data-goto]"):
        assert clicks(dash, sel) == [], sel
    assert dash.js("document.getElementById('cc-news-panel').hidden") is False           # news: only behind its button


def test_home_adds_no_scan_fetch_or_ai_call(client, monkeypatch):
    from api.routes import portfolio as pr
    from insights import service
    from test_market_insights import inputs
    events, scans, metric_calls = [], [], []
    orig = service.symbols_metrics_fn
    monkeypatch.setattr(pr, "event_lookup", lambda s: (events.append(s), __import__("e_fixtures").bundle("LOW"))[1])
    monkeypatch.setattr(service, "market_inputs_fn", lambda now: (scans.append(now), inputs(fetched=now))[1])
    monkeypatch.setattr(service, "symbols_metrics_fn", lambda syms: (metric_calls.append(tuple(syms)), orig(syms))[1])
    service._cache.clear()
    before = len(client.http.requests)
    d = client.post("/api/insights/home", json={}).json()
    held = sorted(c["symbol"] for c in d["my_stocks"])
    assert sorted(events) == held and len(scans) == 1                                    # one event read per holding
    assert len(metric_calls) == 1 and sorted(metric_calls[0]) == held                    # watchlist uses the cached metrics
    paths = [r["path"].split("?")[0] for r in client.http.requests[before:]]
    assert paths.count("/positions") == 1 and paths.count("/portfolio") == 1
    assert client.ai_calls == []


def test_no_execution_controls(client, dash):
    render(dash, home(client))
    buttons = dash.js("[...document.querySelectorAll('button')].map((b) => b.innerText.trim())")
    assert not [t for t in buttons if re.search(r"\b(buy|sell|trade|order|execute|place)\b", t, re.I)]
    assert not re.search(r"buy now|sell now|place_?order|submit_?order|/orders|setTimeout\(|setInterval\(", CC, re.I)


def test_backend_change_is_additive_and_read_only(client):
    d = home(client)
    assert all(set(w) == {"symbol", "owned", "price", "session_pct", "signal"} for w in d["watchlist"])   # unchanged
    held = {c["symbol"] for c in d["my_stocks"]}
    ctx = d["watchlist_context"]
    assert set(ctx) == {w["symbol"] for w in d["watchlist"] if w["symbol"] not in held}
    for v in ctx.values():
        assert set(v) == {"research", "trend"} and set(v["research"]) == {"available", "view", "age_hours", "freshness"}
        assert v["trend"] in ("Up", "Down", "Mixed", "N/A")
    src = (ROOT / "api" / "routes" / "insights.py").read_text(encoding="utf-8")
    block = src[src.index('out["watchlist_context"] = {}'):src.index("except Exception:  # noqa: BLE001 - watchlist")]
    assert "_research_lookups(" in block and "research_freshness(" in block and "trend_label(" in block
    assert not re.search(r"event_lookup|symbols_metrics_fn|market_insights|provider|score|assemble\(", block)
