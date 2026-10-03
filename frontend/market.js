// market.js — Stage 2.7E beginner market context. The Stage 2.7F Beginner Dashboard is command_center.js.
//
// Every label and number comes from /api/insights/market (deterministic Python). The AI button only
// explains those facts; its output is grounding-checked server-side. Nothing here is a prediction or an
// instruction to trade. The existing Dashboard is untouched and stays available under "Advanced".
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const n = (v) => (v == null || v === "" ? null : Number(v));
  const pct = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : ""}${n(v).toFixed(2)}%`);
  const money = (v) => (n(v) == null ? "N/A" : n(v).toLocaleString("en-US", { style: "currency", currency: "USD" }));
  const when = (v) => (v ? new Date(v).toLocaleString() : "N/A");
  const badge = (label, cls) => `<span class="mk-badge mk-${esc(cls || label)}">${esc(label)}</span>`;
  const LABEL_CLASS = {
    SUPPORTIVE: "good", "MORE STABLE": "good", IMPROVING: "good", "RISK-ON": "good", LOW: "good", STRONG: "good",
    MIXED: "mid", "MIXED / SELECTIVE": "mid", NORMAL: "info",
    // 2.8E palette: caution = orange, weakening = red (same as the dashboard's market strip)
    CAUTIOUS: "alert", "MORE CHALLENGING": "alert", WEAKENING: "bad", "RISK-OFF": "alert", ELEVATED: "alert", WEAK: "alert",
  };
  const lab = (v) => badge(v || "N/A", LABEL_CLASS[v] || "mid");
  let last = null;

  async function getJSON(url, options) {
    const res = await fetch(url, options);
    return res.json();
  }

  async function load(refresh) {
    last = await getJSON(`/api/insights/market${refresh ? "?refresh=true" : ""}`);
    return last;
  }

  function staleBanner(m) {
    if (!m.available) return `<div class="pf-banner pf-warn">${esc(m.message || "Current market trend unavailable")}</div>`;
    return m.stale ? `<div class="pf-banner pf-bad"><strong>${esc(m.stale_message)}</strong> (last updated ${esc(when(m.fetched_at))})</div>` : "";
  }

  // Compact card used on the Portfolio tab and at the top of the beginner Dashboard.
  function card(m, opts = {}) {
    if (!m || !m.available) {
      return `<div class="section mk-card"><h2>${esc(opts.title || "Current market")}</h2>${staleBanner(m || {})}
        <p class="hint">Current market trend unavailable. Nothing is guessed.</p></div>`;
    }
    return `<div class="section mk-card"><h2>${esc(opts.title || "Current market")}
        <button class="mk-refresh" data-refresh="1">Refresh market</button></h2>
      ${staleBanner(m)}
      <div class="mk-grid">
        <div><div class="pf-label">Overall environment</div>${lab(m.environment)}</div>
        <div><div class="pf-label">Trend</div>${lab(m.trend)}</div>
        <div><div class="pf-label">Volatility</div>${lab(m.volatility)}</div>
        <div><div class="pf-label">Risk appetite</div>${lab(m.risk_appetite)}</div>
        <div><div class="pf-label">Beginner difficulty</div>${lab(m.difficulty)}</div>
        <div><div class="pf-label">Data freshness</div><div class="pf-small">Updated ${esc(when(m.fetched_at))}</div></div>
      </div>
      <p><strong>What is happening?</strong> ${m.what_is_happening.map(esc).join(" ")}</p>
      <p><strong>Why does it matter?</strong> ${esc(m.why_it_matters)} ${esc(m.difficulty_text)}</p>
      ${m.what_to_watch.length ? `<p><strong>What should I watch?</strong></p><ul>${m.what_to_watch.map((w) => `<li>${esc(window.StockResult ? window.StockResult.tidy(w) : w)}</li>`).join("")}</ul>` : ""}
      <p class="hint">${esc(m.disclaimer)}</p></div>`;
  }

  function drivers(m) {
    if (!m || !m.available) return "";
    const pos = m.drivers.positive.map((d) => `<li class="mk-pos">✓ ${esc(d)}</li>`).join("");
    const neg = m.drivers.caution.map((d) => `<li class="mk-neg">⚠ ${esc(d)}</li>`).join("");
    const areas = (list) => list.map((s) => `${esc(s.sector)} (${esc(s.etf)} ${pct(s.pct_change)})`).join(", ") || "none identified";
    const b = m.breadth;
    const breadth = b.available ? `<p class="pf-small"><strong>Breadth: ${esc(b.label)}</strong> — ${esc(b.advancing_pct)}% rising, ${esc(b.declining_pct)}% falling, ${esc(b.above_ema20_pct)}% above their 20-day average, ${esc(b.above_ema50_pct)}% above their 50-day average (${esc(b.sample_size)} stocks: ${esc(b.scope)}).${b.scanner ? ` Scanner (${esc(b.scanner.scope)}): ${esc(b.scanner.possible_breakouts)} possible breakouts, ${esc(b.scanner.large_drops)} large drops.` : ""}</p>` : `<p class="pf-small">Breadth unavailable.</p>`;
    const news = (m.news || []).length ? `<h3>News <span class="pf-muted pf-small">(sourced headlines — price moves alone don't prove why the market moved)</span></h3><ul>${m.news.map((x, i) => `<li>[${i + 1}] <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.headline)}</a> <span class="pf-muted pf-small">— ${esc(x.source)}, ${esc(when(x.created_at))}, via ${esc(x.queried_symbol)} news</span>${x.summary ? `<div class="pf-small">${esc(String(x.summary).slice(0, 220))}</div>` : ""}</li>`).join("")}</ul>` : `<p class="pf-small pf-muted">No sourced news items were returned.</p>`;
    return `<div class="section"><h2>What's driving it? <span class="pf-muted pf-small">(observed market data)</span></h2>
      <ul class="mk-list">${pos}${neg}</ul>${breadth}
      <p class="pf-small"><strong>Strong areas:</strong> ${areas(m.strong_areas)} · <strong>Weak areas:</strong> ${areas(m.weak_areas)}</p>
      ${news}</div>`;
  }

  function events(m) {
    if (!m) return "";
    const list = (m.events || []).slice(0, 6);
    return `<div class="section"><h2>Important events</h2>${list.length ? `<ul class="mk-list">${list.map((e) => `<li><strong>${esc(e.title)}</strong> — ${esc(e.date)} <span class="pf-muted pf-small">(${esc(e.days_until)} days, ${esc(e.source)})</span><div class="pf-small">${esc(e.why_it_matters)}</div></li>`).join("")}</ul>` : `<p class="pf-small">No verified upcoming macro events in the lookahead window.</p>`}
      <p class="hint">Verified dates only. The app does not predict any release or the market's reaction.</p></div>`;
  }

  async function explain(box) {
    box.textContent = "Asking the AI to explain the market facts…";
    const d = await getJSON("/api/insights/market/explain", { method: "POST" });
    if (d.status !== "OK") { box.innerHTML = `<div class="pf-banner pf-warn">${esc(d.message || d.status)}</div>`; return; }
    const e = d.explanation;
    const list = (a) => (a.length ? `<ul>${a.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "<p class='pf-muted'>None.</p>");
    box.innerHTML = `<div class="ai-box"><h3>Observed</h3>${list(e.observed)}<h3>Sourced news</h3>${list(e.sourced_news)}
      <h3>Interpretation <span class="pf-muted pf-small">(may, not will)</span></h3>${list(e.interpretation)}
      <p><strong>Why it matters:</strong> ${e.why_it_matters.map(esc).join(" ")}</p><h3>What to watch</h3>${list(e.what_to_watch)}
      <p class="hint">AI explanation of the facts above; every number was checked against them. Not a forecast or advice.</p></div>`;
  }

  // ---- beginner Dashboard (Stage 2.7F: the command center lives in command_center.js) -------------------------
  function renderDashboard(refresh) {
    if (window.CommandCenter) window.CommandCenter.render(document.getElementById("dash-beginner"), refresh);
  }

  function wire(root, onRefresh) {
    root.querySelectorAll("[data-refresh]").forEach((b) => b.addEventListener("click", onRefresh));
    const eb = root.querySelector("#mk-explain-btn");
    if (eb) eb.addEventListener("click", () => explain(root.querySelector("#mk-explain")));
  }

  // Dashboard mode toggle (Beginner = new view, Advanced = the original dashboard, unchanged)
  let mode = "beginner";
  try { mode = localStorage.getItem("dashMode") || "beginner"; } catch (e) { /* storage unavailable */ }
  function setMode(m) {
    mode = m;
    try { localStorage.setItem("dashMode", m); } catch (e) { /* ignore */ }
    document.querySelectorAll("#dash-mode-toggle button[data-mode]").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
    const beg = document.getElementById("dash-beginner"), adv = document.getElementById("dash-advanced");
    if (beg && adv) { beg.style.display = m === "beginner" ? "" : "none"; adv.style.display = m === "advanced" ? "" : "none"; }
    if (m === "beginner") renderDashboard(false);
  }
  document.querySelectorAll("#dash-mode-toggle button[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  if (document.getElementById("dash-beginner")) setMode(mode);

  window.MarketContext = { load, card, drivers, events, wire, explainInto: explain, get last() { return last; } };
})();
