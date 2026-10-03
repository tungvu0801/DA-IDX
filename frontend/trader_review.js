// trader_review.js — TRADER REVIEW (Stage 2.7E, simplified in Stage 2.7F). Educational; never places or prepares orders.
//
// QUICK TRADE CHECK: the beginner gives a stock, an amount and a timeframe (optional reason chip); every check runs
// automatically on the existing deterministic systems. "Refresh check" re-reads market/quote/metrics/events/portfolio
// data only — it never starts AI research. Research is refreshed only by the explicit "Refresh research" button.
// The previous check (for "SINCE YOUR LAST CHECK") lives in memory only and is never saved.
(function () {
  const root = document.getElementById("tr-body");
  if (!root) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const n = (v) => (v == null || v === "" ? null : Number(v));
  const money = (v) => (n(v) == null ? "N/A" : n(v).toLocaleString("en-US", { style: "currency", currency: "USD" }));
  const spct = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : "−"}${Math.abs(n(v)).toFixed(2)}%`);
  const pct = (v) => (n(v) == null ? "N/A" : `${n(v).toFixed(2)}%`);
  const move = (v) => (n(v) == null ? "" : n(v) > 0 ? "pct-up" : n(v) < 0 ? "pct-down" : "");
  const time = (v) => (v ? new Date(v).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }) : "N/A");
  const when = (v) => (v ? new Date(v).toLocaleString() : "N/A");
  const cap = (s) => (s ? String(s).charAt(0) + String(s).slice(1).toLowerCase() : "");
  const tag = (text, kind) => `<span class="cc-tag cc-${esc(kind)}">${esc(text)}</span>`;
  const LEVEL = { good: "ok", mid: "warn", bad: "alert", neutral: "dim" };
  const COND = { "MORE SUPPORTIVE CONDITIONS": "ok", "MIXED — WAIT FOR CONFIRMATION": "warn", "HIGHER-RISK CONDITIONS": "alert" };
  const PROCESS = { "WELL-SUPPORTED PROCESS": "ok", "MIXED PROCESS": "warn", "HIGHER-RISK PROCESS": "alert", "INSUFFICIENT INFORMATION": "dim" };
  const FRESH = { FRESH: "ok", AGING: "warn", STALE: "dim", MISSING: "dim", UNKNOWN: "dim" };   // 2.8E shared semantics
  const SR = window.StockResult;
  const T = (t) => (SR ? SR.tidy(t) : String(t == null ? "" : t));                                  // wording tidy only
  const TIMEFRAMES = [["today", "Today"], ["days", "Few days"], ["weeks", "Few weeks"], ["long_term", "Long term"]];
  const CHIPS = [["pullback", "Pullback"], ["breakout", "Breakout"], ["news", "News"], ["earnings", "Earnings"], ["long_term", "Long-term idea"], ["momentum", "Momentum"], ["researching", "Just researching"]];
  const LOC = { NEAR_SUPPORT: "Near support", NEAR_RESISTANCE: "Near resistance", MIDDLE_OF_RANGE: "Mid-range", NO_RESISTANCE_ABOVE: "Above recent resistance", NO_SUPPORT_BELOW: "Below recent support", UNAVAILABLE: "Unavailable" };
  let checklist = [], state = { timeframe: "days", reason: null }, lastReq = null, lastQuick = null;
  const previous = {};            // symbol -> compare snapshot of the previous check (memory only)

  async function getJSON(url, options) { const r = await fetch(url, options); return r.json(); }
  const post = (url, body) => getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  const lessons = `<div class="tr-lessons tr-lessons-compact"><span>GOOD DECISION ≠ GUARANTEED PROFIT</span> · <span>BAD OUTCOME ≠ AUTOMATICALLY BAD DECISION</span>
    <div class="pf-small">This reviews decisions with the information available at the time. It is not advice and never creates an order.</div></div>`;

  // ---- QUICK TRADE CHECK -------------------------------------------------------------------------------------
  function quickForm(sym, amount) {
    return `<div class="section qc"><h2>QUICK TRADE CHECK</h2>
      <div class="qc-form">
        <label class="qc-field">Stock <input id="qc-sym" maxlength="10" placeholder="MU" value="${esc(sym || "")}" autocapitalize="characters" /></label>
        <label class="qc-field">Amount $ <input id="qc-amt" type="number" min="1" step="1" value="${esc(amount || 500)}" /></label>
      </div>
      <div class="qc-label">I'm thinking:</div>
      <div class="qc-seg" id="qc-tf">${TIMEFRAMES.map(([v, t]) => `<button type="button" data-tf="${v}" class="${state.timeframe === v ? "active" : ""}">${t}</button>`).join("")}</div>
      <div class="qc-label">Why are you interested? <span class="pf-muted pf-small">(optional)</span></div>
      <div class="qc-chips" id="qc-chips">${CHIPS.map(([v, t]) => `<button type="button" data-chip="${v}" class="${state.reason === v ? "active" : ""}">${t}</button>`).join("")}</div>
      <input id="qc-note" class="qc-note" maxlength="200" placeholder="Anything else? (optional, not saved)" />
      <div class="qc-go"><button class="cc-btn cc-primary" id="qc-run">Check</button></div>
      <div id="qc-out"></div>
      <details class="qc-advanced" id="qc-advanced"><summary>Advanced trade checklist</summary>${advancedForm()}</details></div>`;
  }

  function tiles(q) {
    return `<div class="qc-tiles">${q.tiles.map((t) => `<div class="qc-tile cc-${LEVEL[t.level] || "dim"}"><div class="qc-tile-title">${esc(t.title)}</div><div class="qc-tile-val">${esc(T(t.value))}</div></div>`).join("")}</div>`;
  }

  function ladder(l) {
    if (l.price == null) return "";
    const pos = l.position == null ? null : Math.round(l.position * 100);
    return `<div class="qc-ladder"><div class="qc-label">WHERE THE PRICE IS — ${esc(l.location)}</div>
      <div class="qc-track">${pos == null ? "" : `<span class="qc-marker qc-at-${pos > 75 ? "r" : pos < 25 ? "l" : "c"}" style="left:${pos}%"><span>Now ${money(l.price)}</span></span>`}</div>
      <div class="qc-ends"><span>Support ${l.support == null ? "none found" : money(l.support)}</span><span>Resistance ${l.resistance == null ? "none found" : money(l.resistance)}</span></div></div>`;
  }

  const list3 = (title, items, cls) => `<div class="qc-list ${cls}"><h4>${esc(title)}</h4>${items.length ? `<ul>${items.map((x) => `<li>${esc(T(x))}</li>`).join("")}</ul>` : "<p class='pf-small pf-muted'>Nothing notable.</p>"}</div>`;

  function renderQuick(d) {
    const out = document.getElementById("qc-out");
    if (d.status !== "OK" && d.status !== "STALE") {
      out.innerHTML = `<div class="cc-banner cc-warn">${esc(d.status === "PORTFOLIO_UNAVAILABLE" ? "Robinhood is not connected, so the portfolio part of this check can't run. Start the read-only gateway and try again." : (d.message || d.detail || "Please check the inputs."))}</div>`;
      return;
    }
    const q = d.quick; lastQuick = q;
    previous[q.symbol] = q.compare_snapshot;
    const r = q.research;
    const researchLine = r.freshness === "MISSING" ? "RESEARCH NEEDED" : `RESEARCH ${r.freshness}`;
    out.innerHTML = `<div class="qc-result">
      <div class="qc-head"><h3>${esc(q.title)}</h3><div class="pf-small">Checked: ${esc(time(q.checked_at))} <button class="cc-btn cc-mini" data-act="refresh">Refresh check</button></div></div>
      <div class="qc-cond"><span class="qc-label">CURRENT CONDITIONS</span> ${tag(q.current_conditions, COND[q.current_conditions] || "warn")}</div>
      <div class="pf-small pf-muted">${esc(q.timeframe_label)}: ${esc(q.timeframe_focus)}</div>
      ${q.changes_since_last_check.length ? `<div class="qc-since"><div class="qc-label">SINCE YOUR LAST CHECK</div><ul>${q.changes_since_last_check.map((c) => `<li>${esc(c.field)}: ${esc(c.field === "Price" ? money(c.before) : c.field === "Weight after adding" ? pct(c.before) : cap(c.before))} → <strong>${esc(c.field === "Price" ? money(c.after) : c.field === "Weight after adding" ? pct(c.after) : cap(c.after))}</strong></li>`).join("")}</ul></div>` : ""}
      ${q.chasing_risk ? `<div class="qc-chase">${esc(q.chasing_risk)}</div>` : ""}
      ${tiles(q)}
      ${ladder(q.ladder)}
      ${q.reason_feedback ? `<div class="qc-reason">${esc(q.reason_feedback)}</div>` : ""}
      <div class="qc-lists">${list3("WHAT LOOKS GOOD", q.what_looks_good, "qc-good")}${list3("WHAT MAKES ME CAUTIOUS", q.what_makes_me_cautious, "qc-careful")}${list3("WHAT I WOULD WATCH", q.what_i_would_watch, "qc-watch")}</div>
      <p class="qc-port">${esc(T(q.portfolio_line))}</p>
      <div class="qc-research"><span class="cc-label">RESEARCH</span> ${SR ? SR.freshTag(r.freshness, r.age_hours) : tag(researchLine, FRESH[r.freshness] || "dim")} ${r.view ? tag(cap(r.view), "info") : "No saved research for this stock."}
        ${r.needs_refresh ? `<button class="cc-btn cc-mini" data-act="research">Refresh research</button>` : ""}<div id="qc-confirm"></div></div>
      ${q.catalysts.length ? `<details class="qc-more"><summary>Recent verified headlines (${q.catalysts.length})</summary><ul>${q.catalysts.map((c) => `<li>${esc(c.title)} <span class="pf-muted pf-small">— ${esc(c.source)}, ${esc(when(c.published_at))}</span></li>`).join("")}</ul></details>` : ""}
      <div class="qc-actions"><button class="cc-btn" data-act="refresh">Refresh check</button>
        <button class="cc-btn" data-act="addmoney">What if I add ${money(q.amount_usd).replace(".00", "")}?</button>
        <button class="cc-btn" data-act="details">Show technical details</button>
        <button class="cc-btn" data-act="advanced">Advanced checklist</button></div>
      <div id="qc-details" hidden>${details(q.details, d)}</div>
      <p class="hint">${esc(q.disclaimer)}</p></div>`;
    out.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => act(b.dataset.act)));
  }

  function details(c, d) {
    const t = c.stock_trend, w = t.why || {};
    return `<table class="data-table"><tr><th>Item</th><th>Value</th><th>Based on</th></tr>
      <tr><td>Trend</td><td>${esc(t.trend)}</td><td class="pf-small">EMA 9/20/50: ${esc(w.ema_9)} / ${esc(w.ema_20)} / ${esc(w.ema_50)}</td></tr>
      <tr><td>Momentum</td><td>${esc(t.momentum)}</td><td class="pf-small">score ${esc(w.momentum_score)} (50 = neutral), 5-day ${spct(w.momentum_5d_pct)}, RSI ${esc(w.rsi)}</td></tr>
      <tr><td>Price location</td><td>${esc(LOC[c.price_area.location] || c.price_area.location)}</td><td class="pf-small">${spct(w.dist_from_support_pct)} from support, ${spct(w.dist_from_resistance_pct)} from resistance</td></tr>
      <tr><td>Volume</td><td>${esc(t.volume)}</td><td class="pf-small">relative volume ${esc(w.relative_volume)}x</td></tr>
      <tr><td>Event risk</td><td>${esc(c.events.event_risk)}</td><td class="pf-small">${esc(c.events.explanation || "")}</td></tr>
      <tr><td>Fit / timing</td><td>${esc(c.fit.state)} / ${esc(c.timing.status)}</td><td class="pf-small">Existing Stage 2.7E rules</td></tr></table>
      <p class="pf-small">Price ${money(c.price_area.current_price)} (${esc(c.price_area.price_source || "")}, ${esc(when(c.price_area.price_time))}). Market data fetched ${esc(when(d.market_fetched_at))}.</p>`;
  }

  function quickReq(extra) {
    const req = { symbol: document.getElementById("qc-sym").value.trim().toUpperCase(), amount_usd: Number(document.getElementById("qc-amt").value),
      timeframe: state.timeframe };
    if (state.reason) req.reason = state.reason;
    const note = document.getElementById("qc-note").value.trim(); if (note) req.note = note;
    const id = window.ResearchIds && window.ResearchIds.get(req.symbol); if (id) req.analysis_id = id;
    if (previous[req.symbol]) req.previous = previous[req.symbol];
    return Object.assign(req, extra || {});
  }

  async function runQuick(extra) {
    const req = quickReq(extra);
    if (!req.symbol) { document.getElementById("qc-out").innerHTML = "<div class='cc-banner cc-warn'>Enter a stock symbol.</div>"; return; }
    lastReq = req;
    document.getElementById("qc-out").innerHTML = `<p class="hint">Checking ${esc(req.symbol)} with live market, stock and portfolio data (no AI calls)…</p>`;
    renderQuick(await post("/api/trade-review/quick", req).catch(() => ({ status: "ERROR", message: "Could not reach the server." })));
  }

  async function act(a) {
    if (a === "refresh") return runQuick({ refresh: true });
    if (a === "details") { const el = document.getElementById("qc-details"); el.hidden = !el.hidden; return; }
    if (a === "advanced") { const el = document.getElementById("qc-advanced"); el.open = true; el.scrollIntoView({ behavior: "smooth" }); return; }
    if (a === "addmoney") { if (window.PortfolioBeginner) window.PortfolioBeginner.openAddMoney(lastQuick.symbol, n(lastQuick.amount_usd)); return; }
    if (a === "research") {
      const host = document.getElementById("qc-confirm"), sym = lastQuick.symbol;
      host.innerHTML = `<div class="cc-confirm"><p>Refresh research for ${esc(sym)}? This may make up to 3 AI analysis calls.</p>
        <button class="cc-btn cc-primary" data-yes>Continue</button> <button class="cc-btn" data-no>Cancel</button></div>`;
      host.querySelector("[data-no]").addEventListener("click", () => { host.innerHTML = ""; });
      host.querySelector("[data-yes]").addEventListener("click", async () => {
        host.innerHTML = `<p class="hint">Researching ${esc(sym)}…</p>`;
        try { await window.ResearchIds.analyze(sym); await runQuick(); }
        catch (e) { host.innerHTML = `<div class="cc-banner cc-warn">Research could not run: ${esc(e.message)}</div>`; }
      });
    }
  }

  function wireQuick() {
    root.querySelectorAll("#qc-tf button").forEach((b) => b.addEventListener("click", () => {
      state.timeframe = b.dataset.tf;
      root.querySelectorAll("#qc-tf button").forEach((x) => x.classList.toggle("active", x === b));
    }));
    root.querySelectorAll("#qc-chips button").forEach((b) => b.addEventListener("click", () => {
      state.reason = state.reason === b.dataset.chip ? null : b.dataset.chip;
      root.querySelectorAll("#qc-chips button").forEach((x) => x.classList.toggle("active", x.dataset.chip === state.reason));
    }));
    document.getElementById("qc-run").addEventListener("click", () => runQuick());
    document.getElementById("tr-run-before").addEventListener("click", runBefore);
  }

  // ---- Advanced checklist (the Stage 2.7E full review; collapsed by default) --------------------------------------
  function advancedForm() {
    return `<div class="pf-form"><label>Intended entry price (optional) <input id="tr-entry" type="number" min="0" step="0.01" size="8" placeholder="current" /></label>
        <label>Holding period <select id="tr-hold"><option value="">not decided</option><option value="days">a few days</option><option value="weeks">a few weeks</option><option value="months">a few months</option><option value="long_term">long term</option></select></label></div>
      <div class="pf-form tr-plan"><label>Why am I entering? <textarea id="tr-reason" rows="2" maxlength="500"></textarea></label>
        <label>What would invalidate my idea? <textarea id="tr-inval" rows="2" maxlength="500"></textarea></label></div>
      <div class="tr-checklist">${checklist.map((q, i) => `<label class="tr-q"><span>□ ${esc(q)}</span><input data-q="${i}" maxlength="200" /></label>`).join("")}</div>
      <div class="pf-form"><button class="cc-btn" id="tr-run-before">Run the full process review</button></div>
      <p class="pf-small pf-muted">Uses the stock and amount above. Your answers stay in this tab and are not saved.</p><div id="tr-before-out"></div>`;
  }

  function beforeReq() {
    const entry = n(document.getElementById("tr-entry").value);
    const req = { symbol: document.getElementById("qc-sym").value.trim().toUpperCase(), amount_usd: Number(document.getElementById("qc-amt").value) };
    if (entry) req.entry_price = entry;
    const hold = document.getElementById("tr-hold").value; if (hold) req.holding_period = hold;
    const reason = document.getElementById("tr-reason").value.trim(); if (reason) req.reason = reason;
    const inval = document.getElementById("tr-inval").value.trim(); if (inval) req.invalidation = inval;
    return req;
  }

  async function runBefore() {
    const out = document.getElementById("tr-before-out");
    out.innerHTML = "<p class='hint'>Reviewing with current verified information…</p>";
    const d = await post("/api/trade-review/before", beforeReq());
    if (d.status !== "OK" && d.status !== "STALE") { out.innerHTML = `<div class="cc-banner cc-warn">${esc(d.message || d.detail || d.status)}</div>`; return; }
    const r = d.review;
    out.innerHTML = `<h3>Process review ${tag(r.process_state, PROCESS[r.process_state] || "warn")}</h3>
      <div class="qc-lists">${list3("WHAT SUPPORTS THE IDEA", r.supporting.map((f) => f.text), "qc-good")}${list3("WHAT MAKES IT RISKIER", r.risks.map((f) => f.text), "qc-careful")}${list3("WHAT A DISCIPLINED PROCESS WOULD WATCH", r.watch, "qc-watch")}</div>
      <div class="pf-form"><button class="cc-btn" id="tr-explain-before">Explain this review (AI)</button></div><div id="tr-explain-out"></div><p class="hint">${esc(r.note)}</p>`;
    document.getElementById("tr-explain-before").addEventListener("click", () => explain({ before: beforeReq() }));
  }

  // ---- AFTER I TRADED (simplified) ---------------------------------------------------------------------------
  async function afterView(symbol) {
    const body = root.querySelector("#tr-mode-body");
    body.innerHTML = "<p class='hint'>Loading your positions…</p>";
    const pf = await getJSON("/api/portfolio").catch(() => null);
    const syms = pf && pf.positions ? pf.positions.map((p) => p.symbol) : [];
    body.innerHTML = `<div class="section"><h2>AFTER I TRADED</h2>
      ${syms.length ? `<div class="pf-form"><select id="tr-pos">${syms.map((s) => `<option ${s === symbol ? "selected" : ""}>${esc(s)}</option>`).join("")}</select><button class="cc-btn cc-primary" id="tr-run-after">Review this position</button></div>`
        : `<div class="cc-banner cc-warn">${esc((pf && pf.status === "PORTFOLIO_UNAVAILABLE") ? "Robinhood is not connected. Start the read-only gateway, then come back." : "No positions found.")}</div>`}
      <div id="tr-after-out"></div></div>`;
    const btn = document.getElementById("tr-run-after");
    if (btn) btn.addEventListener("click", () => runAfter(document.getElementById("tr-pos").value));
    if (symbol && syms.includes(symbol)) runAfter(symbol);
  }

  const sectionList = (title, items, cls) => `<div class="tr-sec ${cls}"><h4>${esc(title)}</h4>${items.length ? `<ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "<p class='pf-small pf-muted'>Nothing identified.</p>"}</div>`;

  async function runAfter(symbol) {
    const out = document.getElementById("tr-after-out");
    out.innerHTML = `<p class='hint'>Reviewing ${esc(symbol)} (verified entries, prices before each entry, saved research)…</p>`;
    const d = await getJSON(`/api/trade-review/position/${encodeURIComponent(symbol)}`);
    if (d.status !== "OK" && d.status !== "STALE") { out.innerHTML = `<div class="cc-banner cc-warn">${esc(d.message || d.status)}</div>`; return; }
    const p = d.position;
    const lots = d.lots.slice(-6).reverse();
    out.innerHTML = `<div class="tr-pos-sum"><div><span class="qc-label">Average entry</span> ${money(p.avg_cost)}</div><div><span class="qc-label">Price now</span> ${money(p.price_now)}</div>
        <div><span class="qc-label">Result so far (hindsight)</span> <span class="${move(p.unrealized_pnl)}">${money(p.unrealized_pnl)} (${spct(p.unrealized_pnl_pct)})</span></div></div>
      ${lots.map((l, i) => { const s = l.simple; return `<details class="tr-entry" ${i === 0 ? "open" : ""}><summary>${esc(l.open_date || "Unknown date")} · ${money(l.entry_price)} ${tag(s.process_state, PROCESS[s.process_state] || "warn")}</summary>
        ${sectionList("HOW THE TRADE STARTED", s.how_it_started, "")}
        <div class="tr-grid">${sectionList("WHAT WAS DONE WELL", s.what_was_done_well, "qc-good")}${sectionList("WHAT INCREASED RISK", s.what_increased_risk, "qc-careful")}</div>
        <div class="tr-learn"><div class="qc-label">WHAT TO LEARN</div>${s.what_to_learn.map((x) => `<p>${esc(x)}</p>`).join("")}</div>
        <div class="tr-hindsight"><div class="qc-label">${esc(s.hindsight_label)}</div>${s.what_happened.length ? `<ul>${s.what_happened.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "<p class='pf-small'>No completed windows yet.</p>"}</div></details>`; }).join("")}
      <details class="qc-more"><summary>What has changed since your first entry?</summary><table class="data-table"><tr><th>Item</th><th>At entry</th><th>Now</th></tr>
        ${d.changes_since_entry.map((c) => `<tr><td>${esc(c.item)}</td><td>${c.at_entry == null ? `<span class="pf-muted">${esc(c.at_entry_source)}</span>` : esc(LOC[c.at_entry] || c.at_entry)}</td><td>${esc(LOC[c.now] || c.now)}</td></tr>`).join("")}</table></details>
      <div class="pf-form"><button class="cc-btn" id="tr-explain-after">Explain this review (AI)</button></div><div id="tr-explain-out"></div><p class="hint">${esc(d.disclaimer)}</p>`;
    document.getElementById("tr-explain-after").addEventListener("click", () => explain({ position: d.symbol }));
  }

  // ---- MY TRADING PATTERNS (visual) --------------------------------------------------------------------------
  async function patternsView() {
    const body = root.querySelector("#tr-mode-body");
    body.innerHTML = "<p class='hint'>Looking at your recent verified buy entries…</p>";
    const d = await getJSON("/api/trade-review/patterns");
    if (d.status !== "OK") { body.innerHTML = `<div class="cc-banner cc-warn">${esc(d.status === "PORTFOLIO_UNAVAILABLE" ? "Robinhood is not connected. Start the read-only gateway, then come back." : (d.message || d.status))}</div>`; return; }
    if (!d.available) { body.innerHTML = `<div class="section"><h2>MY TRADING PATTERNS</h2><p>${esc(d.message)}</p></div>`; return; }
    const mfe = d.patterns.find((p) => p.kind === "mfe");
    body.innerHTML = `<div class="section"><h2>MY TRADING PATTERNS</h2><p class="pf-small">${esc(d.buy_entries)} verified buy entries since ${esc(d.since)} · ${esc(d.sample)} could be reviewed.</p>
      <div class="tp-counters">${d.counters.map((c) => `<div class="tp-counter"><div class="tp-num">${esc(c.count)}<span>/${esc(c.of)}</span></div>
        <div class="tp-bar"><span style="width:${c.of ? Math.round((100 * c.count) / c.of) : 0}%"></span></div><div class="tp-label">${esc(c.label)}</div></div>`).join("")}</div>
      ${d.main_pattern ? `<div class="tp-main"><div class="qc-label">MAIN PATTERN TO WATCH</div><p class="tp-main-text">${esc(d.main_pattern.text)}</p><p class="pf-small">${esc(d.main_pattern_note)}</p></div>` : ""}
      ${mfe ? `<p class="pf-small pf-muted">Hindsight: ${esc(mfe.text)}</p>` : ""}
      <p class="pf-small pf-muted">Not assessed: ${d.not_assessed.map(esc).join("; ")}. ${esc(d.source)}</p><p class="hint">${esc(d.note)}</p></div>`;
  }

  async function explain(body) {
    const out = document.getElementById("tr-explain-out");
    out.textContent = "Asking the AI to explain the review…";
    const d = await post("/api/trade-review/explain", body);
    if (d.status !== "OK") { out.innerHTML = `<div class="cc-banner cc-warn">${esc(d.message || d.status)}</div>`; return; }
    const e = d.explanation;
    const sec = (t, a) => (a && a.length ? `<h4>${esc(t)}</h4><ul>${a.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");
    out.innerHTML = `<div class="ai-box">${sec("Process", e.process_summary)}${sec("Done well", e.done_well)}${sec("Increased risk", e.increased_risk)}
      ${sec("Conflicting evidence", e.conflicting_evidence)}${sec("What to learn", e.what_to_learn)}${sec("Worth monitoring", e.worth_monitoring)}${sec("Outcome (hindsight)", e.outcome_hindsight)}
      <p class="hint">AI explanation of the deterministic review; numbers are checked. Not advice.</p></div>`;
  }

  function setMode(m, symbol, amount) {
    root.querySelectorAll("#tr-mode-toggle button").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
    const body = root.querySelector("#tr-mode-body");
    if (m === "quick") { body.innerHTML = quickForm(symbol, amount); wireQuick(); if (symbol) runQuick(); }
    else if (m === "after") afterView(symbol);
    else patternsView();
  }

  let initP = null;
  const ensure = () => initP || (initP = init());
  async function init() {
    const c = await getJSON("/api/trade-review/checklist").catch(() => ({ checklist: [] }));
    checklist = c.checklist || [];
    root.innerHTML = `${lessons}<div class="mode-toggle" id="tr-mode-toggle"><button data-mode="quick" class="active">Quick trade check</button>
      <button data-mode="after">After I traded</button><button data-mode="patterns">My trading patterns</button></div><div id="tr-mode-body"></div>`;
    root.querySelectorAll("#tr-mode-toggle button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
  }

  const tabBtn = document.querySelector('.tab-btn[data-tab="review"]');
  if (tabBtn) tabBtn.addEventListener("click", async () => { if (!initP) { await ensure(); setMode("quick"); } });

  async function open(mode, symbol, amount) {
    await ensure();                   // after this the tab click below never re-initialises
    if (tabBtn && !document.getElementById("tab-review").classList.contains("active")) tabBtn.click();
    setMode(mode, symbol, amount);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  window.TraderReview = {
    openPosition(symbol) { return open("after", symbol); },
    quick(symbol, amount) { return open("quick", symbol, amount); },
    patterns() { return open("patterns"); },                 // 2.8D: "Review pattern" (navigation only)
  };
})();
