// daily_review.js — Stage 2.7G TODAY'S PORTFOLIO + WATCHLIST REVIEW (read-only decision support).
//
// Every state, reason, conflict and number comes from POST /api/insights/daily-review (deterministic Python).
// Nothing here calls Claude on load: research runs only after an explicit, limit-checked confirmation, and the AI
// explanation only after its button is clicked. "What changed" compares with the previous check kept in memory;
// feedback stays in memory too and is never sent anywhere. There are no buy/sell/order controls.
(function () {
  const root = document.getElementById("dr-body");
  if (!root) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const n = (v) => (v == null || v === "" ? null : Number(v));
  const money = (v) => (n(v) == null ? "N/A" : n(v).toLocaleString("en-US", { style: "currency", currency: "USD" }));
  const signed = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : "−"}${money(Math.abs(n(v)))}`);
  const spct = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : "−"}${Math.abs(n(v)).toFixed(2)}%`);
  const pct = (v) => (n(v) == null ? "N/A" : `${n(v).toFixed(2)}%`);
  const move = (v) => (n(v) == null ? "" : n(v) > 0 ? "pct-up" : n(v) < 0 ? "pct-down" : "");
  const time = (v) => (v ? new Date(v).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "N/A");
  const cap = (s) => (s ? String(s).charAt(0) + String(s).slice(1).toLowerCase() : "");
  const tag = (text, kind) => `<span class="cc-tag cc-${esc(kind)}">${esc(text)}</span>`;
  const FRESH = { FRESH: "ok", AGING: "warn", STALE: "dim", MISSING: "dim", UNKNOWN: "dim" };   // 2.8C: shared semantics
  const fresh = (label, name) => `<span class="cc-fresh cc-${FRESH[label] || "dim"}">${esc(name)} · ${esc(label || "UNKNOWN")}</span>`;
  // 2.8E: the same state pill, wording tidy and ages as the dashboard / Analyze panel (stock_result.js, loaded first)
  const SR = window.StockResult;
  const stateTag = (s) => (SR ? SR.stateTag(s) : tag(s, KIND[s] || "info"));
  const T = (t) => (SR ? SR.tidy(t) : String(t == null ? "" : t));
  const ageTxt = (h) => (SR ? SR.age(h) : `${h} h`);
  const KIND = {
    "EVENT RISK — REVIEW": "alert", "SETUP WEAKENING": "alert", "PROTECT GAINS / REVIEW RISK": "alert", "REVIEW POSITION SIZE": "alert",
    "DATA STALE": "alert", "INSUFFICIENT DATA": "dim", "HIGHER-RISK SETUP": "alert", "WAIT FOR EVENT": "alert",
    "RESEARCH NEEDED": "warn", "WAIT FOR CONFIRMATION": "warn", "MONITOR SUPPORT": "warn", "WAIT FOR PULLBACK": "warn",
    "WAIT FOR BREAKOUT CONFIRMATION": "warn", "WAIT FOR MARKET CONFIRMATION": "warn", "MIXED — KEEP WATCHING": "warn",
    "SUPPORTED — MONITOR": "ok", "SETUP IMPROVING": "ok", "MONITOR": "info", "WORTH FURTHER REVIEW": "ok",
  };
  // 2.8C: one semantic colour per state on every screen (map lives in stock_result.js, loaded first)
  if (window.StockResult) Object.keys(KIND).forEach((s) => { KIND[s] = window.StockResult.stateKind(s); });
  const LEVEL = { HIGH: "bad", MEDIUM: "alert", LOW: "info" };          // 2.8E palette: red · orange · blue
  let last = null, previous = null, sentPrevious = null, advanced = false, busy = false;
  let ops = null, batchResult = null;          // 2.7G.1: operational status + last research-batch result (memory only)

  async function getJSON(url, options) { const r = await fetch(url, options); return r.json(); }
  const post = (url, body) => getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const ids = () => (window.ResearchIds ? window.ResearchIds.all() : {});

  // ---- header / attention / changes ------------------------------------------------------------------------------
  function header(d) {
    const m = d.market, v = m.verdicts || {};
    const ev = m.next_event;
    return `<section class="cc-card dr-top"><div class="cc-head"><h2>TODAY'S PORTFOLIO + WATCHLIST REVIEW</h2>
        <div class="dr-tools"><div class="mode-toggle dr-mode"><button data-mode="beginner" class="${advanced ? "" : "active"}">Beginner</button><button data-mode="advanced" class="${advanced ? "active" : ""}">Advanced</button></div>
        <button class="cc-btn" data-dr-refresh>Refresh</button></div></div>
      ${m.available ? `<div class="dr-strip">
        <div><div class="cc-label">Market trend</div><div class="cc-vval">${esc(v.market_trend)}</div></div>
        <div><div class="cc-label">Trading environment</div><div class="cc-vval">${esc(v.trading_environment)}</div></div>
        <div><div class="cc-label">Risk level</div><div class="cc-vval">${esc(v.risk_level)}</div></div>
        <div><div class="cc-label">Next major event</div><div class="cc-vval">${ev ? `${esc(ev.title)}` : "None soon"}</div>${ev ? `<div class="cc-small">${esc(ev.date)}</div>` : ""}</div></div>`
        : `<div class="cc-banner cc-warn">${esc(m.message || "Market data unavailable.")}</div>`}
      ${d.events_note ? `<div class="cc-banner cc-warn">${esc(d.events_note)}</div>` : ""}
      ${systemStatus(d)}
      <div class="cc-small cc-dimtext">Checked ${esc(time(d.now))} · ${fresh(m.freshness && m.freshness.label, "Market")} ${d.portfolio.freshness ? fresh(d.portfolio.freshness.label, "Portfolio") : ""}</div></section>`;
  }

  // ---- SYSTEM STATUS (2.7G.1): only states the data can verify; nothing is shown as healthy by default ----------
  // 2.8E palette: grey = unknown / unavailable / stale, blue = in progress (informational)
  const MACRO = { READY: ["Ready", "ok"], LOADING: ["Loading", "info"], UNAVAILABLE: ["Unavailable", "dim"], NOT_LOADED: ["Not loaded yet", "dim"] };
  const MARKET = { FRESH: ["Fresh", "ok"], AGING: ["Aging", "warn"], STALE: ["Stale", "dim"] };
  function systemStatus(d) {
    const row = (k, [text, kind], title) => `<div class="dr-status-row"><span>${esc(k)}</span>${tag(text, kind)}${title ? `<span class="cc-small cc-dimtext">${esc(title)}</span>` : ""}</div>`;
    const cov = d.portfolio.coverage;
    const macro = ops && ops.macro_events ? (MACRO[ops.macro_events.state] || ["Unknown", "dim"]) : ["Unknown", "dim"];
    const market = !d.market.available ? ["Unavailable", "dim"] : (MARKET[(d.market.freshness || {}).label] || ["Unknown", "dim"]);
    return `<div class="dr-status"><div class="cc-label">SYSTEM STATUS</div>
      ${row("Robinhood", d.robinhood.connected ? ["Connected", "ok"] : ["Not connected", "warn"])}
      ${row("Market data", market)}
      ${row("Macro events", macro, ops && ops.macro_events && ops.macro_events.state !== "READY" ? ops.macro_events.detail : "")}
      ${row("Research", cov ? [`${cov.current} / ${cov.total} current`, cov.need.length ? "warn" : "ok"] : ["Unknown — Robinhood not connected", "dim"])}
      ${row("AI usage", ops && ops.ai ? [`${ops.ai.calls_today} / ${ops.ai.daily_limit} today`, "info"] : ["Unknown", "dim"],
            ops && ops.ai ? `${ops.ai.calls_last_hour} / ${ops.ai.hourly_limit} this hour` : "")}</div>`;
  }

  function changes(d) {
    if (!d.changes.length) return "";
    return `<section class="cc-card dr-changes"><h2>WHAT CHANGED SINCE LAST CHECK</h2><ul class="cc-list">
      ${d.changes.map((c) => `<li><strong>${esc(c.subject)}</strong> ${esc(c.field)}: ${esc(c.before)} → <strong>${esc(c.after)}</strong></li>`).join("")}</ul></section>`;
  }

  function attention(d) {
    if (!d.attention.length) return "";
    return `<section class="cc-card cc-focus"><h2>TOP THINGS TO REVIEW TODAY</h2><ol class="dr-attn">
      ${d.attention.map((a) => `<li>${tag(a.level, LEVEL[a.level])} ${esc(T(a.text))}</li>`).join("")}</ol>
      <p class="cc-note">Ordered by how much attention each item needs (severity), never by expected return.</p></section>`;
  }

  // ---- cards -----------------------------------------------------------------------------------------------------
  function why(c) {
    return `<ul class="cc-list dr-why">${c.why.supports.map((x) => `<li class="cc-li-ok">${esc(T(x))}</li>`).join("")}${c.why.cautions.map((x) => `<li class="cc-li-warn">${esc(T(x))}</li>`).join("")}</ul>`;
  }

  function watchNext(w) {
    const bits = [];
    if (w.support != null) bits.push(`Support ${money(w.support)}`);
    if (w.resistance != null) bits.push(`Resistance ${money(w.resistance)}`);
    bits.push(`Volume ${esc(w.volume)}`);
    if (w.next_event) bits.push(`${esc(w.next_event.title)} ${esc(w.next_event.date)}`);
    return `<div class="dr-watch"><span class="cc-label">WATCH NEXT</span> ${bits.map((b) => `<span>${b}</span>`).join("")}</div>`;
  }

  function evidenceTable(e) {
    const rows = [["Market", e.market], ["Stock trend", e.stock_trend], ["Momentum", e.momentum], ["Research", T(e.research)],
      ["Price location", e.price_location], ["Sector", e.sector], ["Event risk", e.event_risk], ["Portfolio fit", e.portfolio_fit]];
    return `<table class="dr-kv">${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}</table>`;
  }

  function details(c) {
    const x = c.details, p = c.position, t = x.technicals || {}, r = x.research;
    const list = (title, items, cls) => (items.length ? `<div class="dr-sub"><div class="cc-label">${esc(title)}</div><ul class="cc-list">${items.map((i) => `<li class="${cls}">${esc(T(i))}</li>`).join("")}</ul></div>` : "");
    return `<details class="dr-details" ${advanced ? "open" : ""}><summary>Details</summary>
      ${p ? `<table class="dr-kv"><tr><th>Value</th><td>${money(p.value)}</td></tr><tr><th>Average cost</th><td>${money(p.avg_cost)}</td></tr>
        <tr><th>Current price</th><td>${money(p.price)}</td></tr><tr><th>Open P&amp;L</th><td class="${move(p.open_pnl)}">${signed(p.open_pnl)} (${spct(p.open_pnl_pct)})</td></tr>
        <tr><th>Portfolio weight</th><td>${pct(p.weight)}</td></tr></table>` : ""}
      <div class="dr-sub"><div class="cc-label">CURRENT EVIDENCE</div>${evidenceTable(x.evidence)}</div>
      <div class="dr-layers"><span>Market <b>${esc(x.layers.market)}</b></span>→<span>Stock <b>${esc(x.layers.stock)}</b></span>→<span>Portfolio <b>${esc(x.layers.portfolio)}</b></span></div>
      ${x.conflicts.length ? `<div class="dr-sub"><div class="cc-label">CONFLICTING EVIDENCE</div>${x.conflicts.map((k) => `<div class="dr-conflict"><b>${esc(k.a)}</b> but <b>${esc(k.b)}</b><div class="cc-small">${esc(T(k.text))}</div></div>`).join("")}</div>` : ""}
      <div class="dr-two">${list("SETUP IMPROVES IF", x.improves_if, "cc-li-ok")}${list("SETUP WEAKENS IF", x.weakens_if, "dr-li-x")}</div>
      ${x.pattern_context ? `<div class="dr-pattern"><div class="cc-label">${esc(x.pattern_context.title)}</div><p>${esc(T(x.pattern_context.text))}</p><div class="cc-small cc-dimtext">${esc(x.pattern_context.note)}</div></div>` : ""}
      ${x.historical_outcomes ? `<div class="dr-sub dr-history"><div class="cc-label">HISTORICAL OUTCOME CONTEXT (not current evidence)</div>
        <div class="cc-small">${esc(x.historical_outcomes.n)} completed ${esc(x.historical_outcomes.horizon_trading_days)}-day outcomes of earlier saved research.
        ${x.historical_outcomes.enough_sample ? `Positive in ${esc(x.historical_outcomes.positive_return_frequency_pct)}% · median ${spct(x.historical_outcomes.median_return_pct)}.` : ""}
        ${esc(x.historical_outcomes.sample_warning || "")}</div></div>` : ""}
      <div class="dr-sub cc-small">${fresh(x.freshness.market, "Market")} ${fresh(x.freshness.quote, "Quote")} ${fresh(x.freshness.research, "Research")} ${fresh(x.freshness.events, "Events")}</div>
      ${advanced ? `<div class="dr-sub"><div class="cc-label">ADVANCED</div><table class="dr-kv">
        <tr><th>RSI</th><td>${esc(t.rsi)}</td></tr><tr><th>ATR</th><td>${esc(t.atr)}</td></tr>
        <tr><th>EMA 9 / 20 / 50</th><td>${esc(t.ema_9)} / ${esc(t.ema_20)} / ${esc(t.ema_50)}</td></tr>
        <tr><th>Momentum score</th><td>${esc(t.momentum_score)} (5-day ${spct(t.momentum_5d_pct)})</td></tr>
        <tr><th>Relative volume</th><td>${esc(t.relative_volume)}x</td></tr>
        <tr><th>Support / resistance</th><td>${money(x.support)} (${spct(t.dist_from_support_pct)}) / ${money(x.resistance)} (${spct(t.dist_from_resistance_pct)})</td></tr>
        <tr><th>Evidence</th><td>${r.view ? `Bullish ${esc(r.bullish_pct)}% · Neutral ${esc(r.neutral_pct)}% · Bearish ${esc(r.bearish_pct)}% (${esc(r.source)}, ${esc(ageTxt(r.age_hours))})` : "No research"}</td></tr>
        <tr><th>Policy flags</th><td>Position ${esc(x.policy.position_severity || "none")} · Sector ${esc(x.policy.sector_severity || "none")}${x.policy.sector_weight != null ? ` (${pct(x.policy.sector_weight)})` : ""}</td></tr>
        <tr><th>Events</th><td>${esc(x.events.event_risk)}${x.events.next_event ? ` · ${esc(x.events.next_event.title)} ${esc(x.events.next_event.date)}` : ""}${x.events.earnings_note ? ` · ${esc(x.events.earnings_note)}` : ""}</td></tr>
        ${x.historical_outcomes && x.historical_outcomes.enough_sample ? `<tr><th>Avg MFE / MAE</th><td>${spct(x.historical_outcomes.avg_mfe_pct)} / ${spct(x.historical_outcomes.avg_mae_pct)}</td></tr>` : ""}
        <tr><th>Rule applied</th><td>${esc(x.rule)}</td></tr></table></div>` : ""}
      ${feedbackBox(c.symbol)}
    </details>`;
  }

  function ownedCard(c) {
    const p = c.position || {};
    return `<div class="dr-card" data-sym="${esc(c.symbol)}">
      <div class="dr-head"><strong class="cc-sym">${esc(c.symbol)}</strong><span class="${move(p.open_pnl_pct)} dr-pnl">${spct(p.open_pnl_pct)}</span><span class="cc-small cc-dimtext">${pct(p.weight)} of account</span></div>
      <div class="dr-state">${stateTag(c.state)}</div>
      <div class="dr-summary">${esc(c.summary)}</div>
      <p class="dr-next">${esc(c.next)}</p>
      ${why(c)}${watchNext(c.watch_next)}${details(c)}</div>`;
  }

  function watchCard(c) {
    const own = c.owned === false ? tag("Not owned", "dim") : tag("Ownership unknown", "dim");
    return `<div class="dr-card" data-sym="${esc(c.symbol)}">
      <div class="dr-head"><strong class="cc-sym">${esc(c.symbol)}</strong><span>${money(c.price)}</span><span class="${move(c.pct_today)}">${spct(c.pct_today)}</span>${own}</div>
      <div class="dr-state">${stateTag(c.state)}</div>
      ${why(c)}
      ${c.waiting_for.length ? `<div class="dr-sub"><div class="cc-label">WHAT I'M WAITING FOR</div><ul class="cc-list">${c.waiting_for.map((w) => `<li>• ${esc(T(w))}</li>`).join("")}</ul></div>` : `<p class="dr-next">${esc(c.next)}</p>`}
      ${details(c)}</div>`;
  }

  function groups(obj, render) {
    return Object.entries(obj).map(([g, cards]) => `<div class="dr-group"><h3>${esc(g)} <span class="cc-small cc-dimtext">(${cards.length})</span></h3>
      ${cards.length ? `<div class="dr-cards">${cards.map(render).join("")}</div>` : `<p class="cc-small cc-dimtext">None.</p>`}</div>`).join("");
  }

  function portfolio(d) {
    const P = d.portfolio, rh = d.robinhood;
    if (!P.available) {
      return `<section class="cc-card"><h2>MY PORTFOLIO</h2><div class="cc-down"><h3>${esc(rh.title || "ROBINHOOD NOT CONNECTED")}</h3><p>${esc(rh.message || "")}</p>
        ${rh.how_to_fix ? `<details class="cc-more"><summary>How to reconnect</summary><div class="cc-label">HOW TO FIX</div><ol class="cc-steps">${rh.how_to_fix.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></details>` : ""}
        <p class="cc-note">The watchlist below still works; ownership and portfolio fit are unknown until Robinhood reconnects.</p></div></section>`;
    }
    const cov = P.coverage, plan = cov.plan;
    const list = (a) => (a.length ? a.map(esc).join(", ") : "none");
    return `<section class="cc-card"><h2>MY PORTFOLIO</h2>
      <div class="dr-coverage"><div class="dr-cov-grid">
        <div><div class="cc-label">CURRENT RESEARCH COVERAGE</div>
          <div class="cc-vval">${esc(cov.current)} / ${esc(cov.total)} holdings current</div>
          <div class="cc-small">${cov.need.length ? `${esc(cov.need.length)} need research` : "All holdings have current research"}</div></div>
        <div><div class="cc-label">AI CAPACITY</div>
          <div class="cc-small">Hourly limit ${esc(plan.hourly_limit)} · Used this hour ${esc(plan.calls_last_hour)}</div>
          <div class="cc-small">Daily limit ${esc(plan.daily_limit)} · Used today ${esc(plan.calls_today)}</div></div>
        ${cov.need.length ? `<div><div class="cc-label">CAN ANALYZE NOW</div><div class="cc-small">${list(plan.analyze_now)}${plan.analyze_now.length ? ` (up to ${esc(plan.calls_for_now)} Claude calls)` : ""}</div></div>
        <div><div class="cc-label">WAIT UNTIL LATER</div><div class="cc-small">${list(plan.remaining)}${plan.remaining.length ? ` (${esc(plan.limited_by)})` : ""}</div></div>` : ""}</div>
        ${batchResult ? `<div class="dr-batch"><div><strong>Research updated:</strong> ${list(batchResult.updated)}</div>
          ${batchResult.stopped ? `<div class="cc-small">Stopped at ${esc(batchResult.stopped)}: ${esc(batchResult.error)}</div>` : ""}
          <div><strong>Still needed:</strong> ${list(cov.need)}</div><div class="cc-small cc-dimtext">Nothing continues automatically.</div></div>` : ""}
        ${cov.need.length ? `<button class="cc-btn cc-primary" id="dr-analyze">Analyze missing research</button>` : ""}</div>
      <div id="dr-analyze-host"></div>
      ${groups(P.groups, ownedCard)}</section>`;
  }

  function watchlist(d) {
    const W = d.watchlist;
    return `<section class="cc-card"><h2>MY WATCHLIST</h2>
      ${W.note ? `<div class="cc-banner cc-warn">${esc(W.note)}</div>` : ""}
      ${W.also_owned.length ? `<p class="cc-small">Also on your watchlist and owned (see My Portfolio): ${W.also_owned.map(esc).join(", ")}.</p>` : ""}
      ${W.count ? groups(W.groups, watchCard) : `<p class="cc-small cc-dimtext">No other watchlist stocks to review.</p>`}</section>`;
  }

  // ---- feedback (kept in this tab's memory only; never sent, never changes any analysis) ----------------------
  const feedback = new Map();
  function feedbackBox(sym) {
    const f = feedback.get(sym) || {};
    const btn = (group, val) => `<button class="cc-btn cc-mini ${f[group] === val ? "dr-picked" : ""}" data-fb="${esc(sym)}|${group}|${val}">${val}</button>`;
    return `<div class="dr-feedback"><div class="cc-small">Was this useful? ${btn("useful", "Yes")} ${btn("useful", "No")}</div>
      <div class="cc-small">What did you do? ${["Waited", "Added", "Reduced", "Closed", "No action"].map((v) => btn("action", v)).join(" ")}</div>
      <div class="cc-small cc-dimtext">Kept in this browser tab only. It is not saved and never changes the analysis.</div></div>`;
  }
  function wireFeedback(scope) {
    scope.querySelectorAll("[data-fb]").forEach((b) => b.addEventListener("click", () => {
      const [sym, group, val] = b.dataset.fb.split("|");
      const f = Object.assign({}, feedback.get(sym) || {}, { [group]: val });
      feedback.set(sym, f);
      b.parentElement.querySelectorAll("[data-fb]").forEach((x) => x.classList.toggle("dr-picked", x === b));
    }));
  }
  // ---- end feedback

  // ---- research batch (explicit confirmation; limits re-checked at click time) --------------------------------
  async function analyzeMissing() {
    if (busy || !last) return;
    const host = root.querySelector("#dr-analyze-host");
    const plan = await post("/api/insights/daily-review/research-plan", { symbols: last.portfolio.coverage.need });  // limits re-checked now
    const n = (a) => `${a.length} stock${a.length === 1 ? "" : "s"}`;
    host.innerHTML = `<div class="cc-confirm dr-confirm"><div class="cc-label">ANALYZE MISSING RESEARCH</div>
      <p>${esc(plan.need.length)} holding${plan.need.length === 1 ? "" : "s"} need current research.</p>
      <table class="dr-kv"><tr><th>Can analyze now</th><td>${plan.analyze_now.length ? `${n(plan.analyze_now)} (${plan.analyze_now.map(esc).join(", ")})` : "None right now"}</td></tr>
        <tr><th>Estimated maximum</th><td>${esc(plan.calls_for_now)} Claude calls (cached results count as zero)</td></tr>
        ${plan.remaining.length ? `<tr><th>Remaining</th><td>${n(plan.remaining)} (${plan.remaining.map(esc).join(", ")})</td></tr>
        <tr><th>Reason</th><td>${esc({ "hourly AI call limit": "Hourly AI limit", "daily AI call limit": "Daily AI limit" }[plan.limited_by] || plan.limited_by || "")}</td></tr>` : ""}
        <tr><th>AI capacity</th><td>Hour ${esc(plan.calls_last_hour)} / ${esc(plan.hourly_limit)} · Today ${esc(plan.calls_today)} / ${esc(plan.daily_limit)}</td></tr></table>
      ${plan.analyze_now.length ? `<button class="cc-btn cc-primary" data-yes>Analyze available stocks</button> ` : ""}<button class="cc-btn" data-no>Cancel</button></div>`;
    host.querySelector("[data-no]").addEventListener("click", () => { host.innerHTML = ""; });
    const yes = host.querySelector("[data-yes]");
    if (!yes) return;
    yes.addEventListener("click", async () => {
      busy = true;
      const updated = [];
      let stopped = null, error = null;
      for (const [i, sym] of plan.analyze_now.entries()) {       // only the stocks that fit the limits right now
        host.innerHTML = `<p class="hint">Researching ${esc(sym)} (${i + 1} of ${plan.analyze_now.length})…</p>`;
        try { await window.ResearchIds.analyze(sym); updated.push(sym); }
        catch (e) { stopped = sym; error = e.message; break; }
      }
      busy = false;
      batchResult = { updated, stopped, error };                   // shown with the refreshed "still needed" list
      load();
    });
  }

  async function explain() {
    const out = root.querySelector("#dr-explain-out");
    out.textContent = "Asking the AI to explain today's review…";
    const d = await post("/api/insights/daily-review/explain", { analysis_ids: ids(), previous: sentPrevious });
    if (d.status !== "OK") { out.innerHTML = `<div class="cc-banner cc-warn">${esc(d.message || d.status)}</div>`; return; }
    const e = d.explanation;
    const sec = (t, a) => (a && a.length ? `<h4>${esc(t)}</h4><ul>${a.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");
    out.innerHTML = `<div class="ai-box">${sec("What changed", e.what_changed)}${sec("Stable / supportive", e.stable_supportive)}${sec("Needs attention", e.needs_attention)}
      ${sec("Watchlist setups", e.watchlist_setups)}${sec("Important events", e.important_events)}${sec("What to monitor next", e.monitor_next)}
      <p class="hint">AI explanation of the calculated review; every number was checked and the states above were decided by the app. Not advice.</p></div>`;
  }

  function render(d) {
    root.innerHTML = `<div class="cc dr">${header(d)}${changes(d)}${attention(d)}${portfolio(d)}${watchlist(d)}
      <section class="cc-card"><h2>EXPLAIN TODAY'S PORTFOLIO</h2><button class="cc-btn" id="dr-explain">Explain today's portfolio (AI)</button><div id="dr-explain-out"></div>
      <p class="cc-note">${esc(d.disclaimer)}</p></section></div>`;
    root.querySelectorAll("[data-dr-refresh]").forEach((b) => b.addEventListener("click", () => load(true)));
    root.querySelectorAll(".dr-mode button").forEach((b) => b.addEventListener("click", () => { advanced = b.dataset.mode === "advanced"; render(last); }));
    const a = root.querySelector("#dr-analyze"); if (a) a.addEventListener("click", analyzeMissing);
    root.querySelector("#dr-explain").addEventListener("click", explain);
    wireFeedback(root);
  }

  async function load(refresh) {
    if (!last) root.innerHTML = `<p class="hint">Building today's review…</p>`;
    try {
      if (refresh) await getJSON("/api/insights/market?refresh=true").catch(() => null);
      sentPrevious = previous;
      const [d, o] = await Promise.all([post("/api/insights/daily-review", { analysis_ids: ids(), previous }),
        getJSON("/api/ops/status").catch(() => null)]);
      ops = o;
      if (!d || !d.market) throw new Error((d && d.detail && JSON.stringify(d.detail)) || "unexpected response");
      last = d;
      previous = d.snapshot;               // memory only — compared on the next check
      render(d);
    } catch (e) {
      root.innerHTML = `<div class="cc-banner cc-warn">Today's review could not load (${esc(e.message)}). If the server was started before this update, restart it.</div>`;
    }
  }

  const tabBtn = document.querySelector('.tab-btn[data-tab="daily"]');
  if (tabBtn) tabBtn.addEventListener("click", () => { if (!last) load(); });
  window.DailyReview = { load, get last() { return last; } };
})();
