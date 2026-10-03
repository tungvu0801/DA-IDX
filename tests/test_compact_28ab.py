"""Stage 2.8A/B: desktop compactness of the Beginner dashboard (presentation only — no financial logic)."""
import re
from pathlib import Path

from e_fixtures import tm, view
from insights import home

FRONT = Path(__file__).resolve().parents[1] / "frontend"
CC = (FRONT / "command_center.js").read_text(encoding="utf-8")
CSS = (FRONT / "command_center.css").read_text(encoding="utf-8")
SR = (FRONT / "stock_result.js").read_text(encoding="utf-8")


def fn(name, nxt):
    return CC[CC.index(name):CC.index(nxt)]


def test_market_hero_is_a_compact_strip_with_news_and_prose_behind_toggles():
    h = fn("function hero(m)", "// ---- MY ROBINHOOD")
    assert "cc-mstrip" in h and "cc-bigpicture" not in CC and "cc-verdicts" not in h
    assert 'class="cc-panel" id="cc-news-panel" hidden' in h and 'class="cc-panel" id="cc-mkt-details" hidden' in h
    assert h.index("cc-oneline") < h.index('id="cc-news-panel"')            # only the one-line summary is visible
    assert "firstSentence(m.big_picture)" in h and 'toggle("cc-news-panel", `News (${m.news.length})`)' in h
    assert "m.news.map(" in h[h.index('id="cc-news-panel"'):]              # news still available, not deleted


def test_robinhood_is_one_seven_metric_strip():
    r = fn("function robinhood(d)", "// ---- WHAT NEEDS ATTENTION")
    assert "cc-mstrip cc-mstrip-7" in r and r.count("${cell(") == 7
    for label in ("Account value", "Open P&L", "Cash", "Largest", "Sector", "Attention"):
        assert f'cell("{label}"' in r


def test_attention_chips_reuse_the_deterministic_observations():
    a = fn("function attention(d)", "// ---- MY STOCKS TODAY")
    assert "d.feedback.slice(0, 5).map((t) => chipFor(t, d))" in a and 'id="cc-attn-why" hidden' in a
    assert "cc-focus-list" in a                                              # full sentences still behind "Why?"
    patterns = [re.compile(p) for p in re.findall(r"\(x = /(.+?)/\.exec\(text\)\)", fn("function chipFor(", "function attention("))]
    patterns.append(re.compile(r"quote or research is missing or old"))
    v = view()
    metrics = {s: tm(s, price=p, prev_close=pc, pct_change=round((p - pc) / pc * 100, 2), momentum_5d_pct=12.0, rsi=74.0)
               for s, p, pc in [("NVDA", 222.0, 220.0), ("SNDK", 45.0, 46.0), ("SHOP", 30.6, 30.0), ("MU", 12.0, 11.9)]}
    cards = home.stock_cards(v, metrics, {}, {}, {"is_today": True})
    change = home.session_change(v, metrics, tm("SPY", pct_change=0.5))
    fb = home.daily_feedback(v, {"sectors": [{"sector": "Semiconductors", "etf": "SOXX", "pct_change": 1.2}],
                                 "events": [{"title": "Employment Situation (Jobs Report)", "date": "2026-10-02",
                                             "days_until": 3.4}]}, cards, change, 10.0)
    assert len(fb) == 5
    for sentence in fb:                                                     # every real observation gets a proper chip
        assert any(p.search(sentence) for p in patterns), sentence


def test_my_stocks_starts_with_cards_and_compact_card_content():
    m = fn("function myStocks(d)", "function layers(d)")
    assert m.index("vsLine(d)") < m.index("cc-stocks") and 'id="cc-vs-details" hidden' in m
    card = fn("function stockCard(c", "function myStocks(d)")
    assert "cc-sentence" not in card and 'title="${esc(c.sentence)}"' in card    # summary kept as hover text
    assert "cc-mini3" in card and card.count("cc-line") >= 2                     # 2.8D: metrics in one row of cells
    assert "data-analyze" in card and "data-quick" in card and "cc-quiet" in card      # 2.8E: Review is secondary
    assert "cc-why" in card and "/ exposure is high$/" in card              # one stock-specific caution first


def test_toggles_open_panels_without_requests_except_the_existing_market_loader():
    w = fn("function wire(root)", "async function render(")
    t = w[w.index('querySelectorAll("[data-toggle]")'):w.index('const sc = root.querySelector("#cc-scanner")')]
    assert "panel.hidden = !panel.hidden" in t and "aria-expanded" in t
    assert "fetch(" not in t and "post(" not in t and t.count("MarketContext.load(false)") == 1   # cached market only


def test_page_load_requests_and_ai_are_unchanged():
    r = CC[CC.index("async function render(root, refresh)"):]
    assert r.count('post("/api/insights/home"') == 1 and "/research" not in r and "analyze" not in r.lower().replace("stockresult", "")
    assert "Loading your command center" in r                              # loading state kept


def test_expanded_analysis_uses_three_desktop_columns():
    d = SR[SR.index("function decisionHtml(dec)"):SR.index("function fullHtml(dec")]      # 2.8C panel grid
    assert '<div class="cc-result-grid sa-grid">${leftCol(c, x, dec.owned)}${centerCol(dec, c, x)}${rightCol(dec, c, x)}</div>' in d
    assert re.search(r"\.cc-result-grid\.sa-grid \{ grid-template-columns: repeat\(3, minmax\(0, 1fr\)\);", CSS)


def test_desktop_grid_rules_and_mobile_base_preserved():
    desk = CSS[CSS.index("@media (min-width: 1200px)"):]
    for rule in (".cc-mstrip { grid-template-columns: repeat(9", ".cc-mstrip-7 { grid-template-columns: repeat(7",
                 ".cc-chips { grid-template-columns: repeat(5"):
        assert rule in desk, rule
    assert re.search(r"\.cc-stocks \{[^}]*grid-template-columns: 1fr;", CSS)   # small-screen base rule untouched


def test_offline_and_missing_data_paths_stay_compact_and_honest():
    nc = fn("function notConnected(r)", "function robinhood(d)")
    assert "cc-down-compact" in nc and "cc-steps" in nc and "Technical details" in nc
    assert "if (!m.available)" in fn("function hero(m)", "// ---- MY ROBINHOOD") and '"N/A"' in fn("function robinhood(d)", "// ---- WHAT NEEDS ATTENTION")
    assert 'tag("Ownership unknown", "dim")' in CC and 'title="Robinhood is not connected"' in CC


def test_no_execution_controls_or_timers():
    for src in (CC, SR):
        assert not re.search(r"buy now|sell now|place_?order|submit_?order|setTimeout\(|setInterval\(", src, re.I)
