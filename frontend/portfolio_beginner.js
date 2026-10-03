// portfolio_beginner.js — Stage 2.7E beginner Portfolio page (the detailed page stays under "Advanced").
//
// Order: 1 My Robinhood account · 2 Current market · 3 What needs attention · 4 My positions ·
// 5 What if I add money? · 6 Market → Stock → Portfolio · 7 When might conditions be better? · 8 Explain (AI)
// Every number and label comes from the backend (deterministic Python). Nothing here can place an order.
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const n = (v) => (v == null || v === "" ? null : Number(v));
  const money = (v) => (n(v) == null ? "N/A" : n(v).toLocaleString("en-US", { style: "currency", currency: "USD" }));
  const signed = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : "−"}${money(Math.abs(n(v)))}`);
  const pct = (v) => (n(v) == null ? "N/A" : `${n(v).toFixed(2)}%`);
  const spct = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : ""}${n(v).toFixed(2)}%`);
  const cls = (v) => (n(v) == null ? "" : n(v) >= 0 ? "pct-up" : "pct-down");
  const qty = (v) => { const x = n(v); if (x == null) return "N/A"; return `${x.toLocaleString("en-US", { maximumFractionDigits: 6 })} ${x === 1 ? "share" : "shares"}`; };
  const when = (v) => (v ? new Date(v).toLocaleString() : "N/A");
  const badge = (t, c) => `<span class="mk-badge mk-${esc(c)}">${esc(t)}</span>`;
  const STATE_CLASS = {
    // 2.8E palette (same as the dashboard): caution / concentration = orange, research views = blue context
    "FAVORABLE CONDITIONS": "good", "MIXED CONDITIONS": "mid", "CAUTION CONDITIONS": "alert",
    "MORE SUPPORTIVE CONDITIONS": "good", "MIXED — WAIT FOR CONFIRMATION": "mid", "HIGHER-RISK CONDITIONS": "alert",
    HIGH: "alert", MEDIUM: "mid", INFO: "info", LOW: "info", OK: "good",
    "BULLISH BIAS": "info", "STRONG BULLISH BIAS": "info", "MIXED / WAIT": "info", "BEARISH BIAS": "info",
    "STRONG BEARISH BIAS": "info", SUPPORTIVE: "good", MIXED: "mid", CAUTIOUS: "alert",
  };
  const state = (t) => badge(t || "N/A", STATE_CLASS[t] || "mid");
  const LOCATION = { NEAR_SUPPORT: "Near support", NEAR_RESISTANCE: "Near resistance", MIDDLE_OF_RANGE: "Middle of range", NO_RESISTANCE_ABOVE: "Above recent highs (no resistance found)", NO_SUPPORT_BELOW: "Below recent lows (no support found)", UNAVAILABLE: "Unavailable" };
  const info = (text) => `<button class="pf-info" type="button" aria-label="What is this?">ⓘ</button><span class="pf-tipbox">${esc(text)}</span>`;
  let lastReq = null, lastCheck = null;

  async function getJSON(url, options) { const r = await fetch(url, options); return r.json(); }

  function account(d) {
    const r = d.reconciliation, g = r.glossary;
    const realized = r.realized_pnl_status === "VERIFIED"
      ? `<span class="${cls(r.realized_pnl)}">${signed(r.realized_pnl)}</span> <span class="pf-muted pf-small">(${esc(r.realized_pnl_span === "all" ? "all time" : r.realized_pnl_span)}, reported by Robinhood)</span>`
      : `<span class="pf-muted">Not available</span>`;
    const row = (label, value, tip) => `<div class="pb-acct-row"><span>${esc(label)} ${info(tip)}</span><span class="pb-acct-val">${value}</span></div>`;
    return `<div class="section pb-acct"><h2>My Robinhood account <span class="pf-muted pf-small">${esc(r.account.alias)} ${esc(r.account.masked_id)}</span></h2>
      ${row("Money added", `<span class="pf-muted">Not available</span>`, g.net_contributions)}
      ${row("Current account value", money(r.robinhood_portfolio_value), g.portfolio_value)}
      ${row("Open-position cost", money(r.open_position_cost_basis), g.cost_basis)}
      ${row("Open-position gain/loss", `<span class="${cls(r.unrealized_pnl)}">${signed(r.unrealized_pnl)} (${spct(r.unrealized_pnl_pct)})</span>${r.unrealized_pnl_complete ? "" : " <span class='pf-muted pf-small'>(incomplete)</span>"}`, g.unrealized_pnl)}
      ${row("Realized gain/loss", realized, g.realized_pnl)}
      ${row("Cash", money(r.cash), g.cash)}
      <p class="pb-note">${esc(r.cost_basis_note)}</p>
      <p class="pf-small pf-muted">${esc(r.net_contributions_message)}. Current positions value (calculated here): ${money(r.current_positions_value)}.</p></div>`;
  }

  function attention(d) {
    const p = d.policy;
    if (!p || !p.enabled) return `<div class="section"><h2>What needs attention</h2><p class="hint">${esc((p && p.message) || "Policy engine off.")}</p></div>`;
    const plain = { HIGH: "High — review before adding more exposure", MEDIUM: "Elevated — worth reviewing", INFO: "Noted", LOW: "Worth noting", OK: "No attention flag", UNAVAILABLE: "Not enough data" };
    return `<div class="section"><h2>What needs attention</h2><div class="pf-att-grid">${p.cards.map((c) => `
      <div class="pf-att pf-att-${esc(c.status)}"><div class="pf-att-head"><span class="pf-label">${esc(c.title)}</span>${state(c.status)}</div>
        <div class="pf-att-status">${esc(plain[c.status] || c.status)}</div><div class="pf-small">${esc(window.StockResult ? window.StockResult.tidy(c.explanation) : c.explanation)}</div></div>`).join("")}</div>
      <p class="hint">These are attention flags from configured rules, not instructions to buy or sell. ${esc(p.separation_note)}</p></div>`;
  }

  function positions(d) {
    const rows = d.positions.map((p) => {
      const r = d.research_by_symbol[p.symbol] || {}, c = d.concerns_by_symbol[p.symbol] || {};
      const research = r.available
        ? `${state(r.research_view)} <span class="pf-small">Bullish ${esc(r.bullish_pct)}% · Neutral ${esc(r.neutral_pct)}% · Bearish ${esc(r.bearish_pct)}%</span>${r.stale ? ` <span class="cc-fresh cc-dim">STALE · ${esc(window.StockResult ? window.StockResult.age(r.age_hours) : `${r.age_hours}h`)}</span>` : ` <span class="pf-muted pf-small">${esc(window.StockResult ? window.StockResult.age(r.age_hours) : `${r.age_hours}h`)} old</span>`}`
        : `<span class="pf-muted">No research yet</span>`;
      const concern = [c.position_severity ? `${state(c.position_severity)} position concentration` : "",
        c.sector_severity ? `${state(c.sector_severity)} ${esc(c.sector)} concentration` : "",
        c.sector_unclassified ? `<span class="pf-muted pf-small">Sector unclassified</span>` : ""].filter(Boolean).join("<br>") || `<span class="pf-muted">None</span>`;
      return `<div class="pb-pos"><div class="pb-pos-head"><strong>${esc(p.symbol)}</strong><span>${money(p.market_value)} · ${pct(p.portfolio_weight)} of portfolio</span></div>
        <div class="pb-pos-grid">
          <div><span class="pf-label">You own</span>${qty(p.quantity)}</div>
          <div><span class="pf-label">You paid (average)</span>${p.basis_available ? money(p.avg_cost) : "Unavailable"}</div>
          <div><span class="pf-label">Price now</span>${money(p.last_price)}</div>
          <div><span class="pf-label">You put into this current position</span>${p.basis_available ? money(p.cost_basis_total) : "Unavailable"}</div>
          <div><span class="pf-label">Worth now</span>${money(p.market_value)}</div>
          <div><span class="pf-label">Current open-position result</span><span class="${cls(p.unrealized_pnl)}">${signed(p.unrealized_pnl)} (${spct(p.unrealized_pnl_pct)})</span></div>
          <div class="pb-wide"><span class="pf-label">Current research</span>${research}</div>
          <div class="pb-wide"><span class="pf-label">Portfolio concern</span>${concern}</div>
        </div><button class="pb-review" data-sym="${esc(p.symbol)}">Review this position</button></div>`;
    }).join("");
    return `<div class="section"><h2>My positions</h2><div class="pb-pos-list">${rows}</div>
      <p class="hint">"You put into this current position" is the cost of the shares you still own — not all money you ever added.</p></div>`;
  }

  function addMoneyForm(d) {
    const cash = d.reconciliation.cash;
    const opts = d.positions.map((p) => `<option value="${esc(p.symbol)}">${esc(p.symbol)}</option>`).join("");
    return `<div class="section"><h2>What if I add money?</h2>
      <div class="pf-form"><label>Stock <select id="pb-sym">${opts}<option value="__other">Other…</option></select></label>
        <input id="pb-sym-other" placeholder="Symbol" maxlength="10" size="8" style="display:none" />
        <label>Amount $ <input id="pb-amt" type="number" min="1" step="1" value="500" size="8" /></label></div>
      <div class="pb-funding">
        <label><input type="radio" name="pb-fund" value="new_money" checked /> <strong>NEW MONEY</strong> — I am adding new money to Robinhood</label>
        <label><input type="radio" name="pb-fund" value="cash" /> <strong>USE CURRENT CASH</strong> — I am using cash already in Robinhood (available: ${money(cash)})</label>
      </div>
      <div class="pf-form"><button id="pb-calc">Calculate</button><button id="pb-refresh-research" title="Runs the existing Stage 2 research for this stock (uses the AI call limits)">Refresh research for this stock</button></div>
      <div id="pb-check"></div></div>`;
  }

  function factorList(list, mark) { return list.length ? `<ul class="mk-list">${list.map((f) => `<li class="${mark === "✓" ? "mk-pos" : "mk-neg"}">${mark} ${esc(f.text || f)}</li>`).join("")}</ul>` : `<p class="pf-muted pf-small">None identified.</p>`; }

  function renderCheck(res) {
    const box = document.getElementById("pb-check");
    if (res.status === "PORTFOLIO_UNAVAILABLE" || res.status === "SCENARIO_INVALID" || res.detail) {
      box.innerHTML = `<div class="pf-banner pf-warn">${esc(res.message || "Please check the inputs.")}</div>`; return;
    }
    const c = res.check; lastCheck = c;
    const L = c.layers, P = L.portfolio, R = L.stock.research, T = c.timing, A = c.price_area, F = c.feasibility;
    let h = "";
    if (!F.feasible) {
      h += `<div class="pb-insufficient"><h3>INSUFFICIENT CASH</h3>
        <div>Available cash: <strong>${money(F.available_cash)}</strong></div><div>Hypothetical amount: <strong>${money(F.amount)}</strong></div>
        <div>Additional cash needed: <strong>${money(F.additional_cash_needed)}</strong></div>
        <p>This cash-funded scenario is not possible with your current cash. ${esc(F.suggestion)}</p>
        <button id="pb-switch-new">Switch to NEW MONEY</button></div>`;
    }
    h += `<h3>What changes if you add ${money(c.amount_usd)} to ${esc(c.symbol)}?</h3><div class="pf-kpis">
      <div class="pf-kpi"><div class="pf-label">Current ${esc(c.symbol)} position</div><div class="pf-value">${money(P.position_value_before)}</div><div class="pf-sub">${pct(P.position_weight_before_pct)} of portfolio</div></div>
      <div class="pf-kpi"><div class="pf-label">After hypothetical ${money(c.amount_usd)}</div><div class="pf-value">${money(P.position_value_after)}</div><div class="pf-sub">${pct(P.position_weight_after_pct)} of portfolio</div></div>
      <div class="pf-kpi"><div class="pf-label">${esc(P.sector === "UNCLASSIFIED" ? "Unclassified-sector" : P.sector)} exposure</div><div class="pf-value">${pct(P.sector_weight_before_pct)} → ${pct(P.sector_weight_after_pct)}</div></div>
      <div class="pf-kpi"><div class="pf-label">Cash</div><div class="pf-value">${c.funding === "new_money" ? "Unchanged" : F.feasible ? `${money(P.cash_before)} → ${money(P.cash_after)}` : "Not enough"}</div><div class="pf-sub">${c.funding === "new_money" ? "because this scenario uses new money" : "funded from current cash"}</div></div></div>`;
    h += `<h3>Current research</h3>${R.available ? `<div>${state(R.research_view)} Evidence: Bullish ${esc(R.bullish_pct)}% · Neutral ${esc(R.neutral_pct)}% · Bearish ${esc(R.bearish_pct)}%</div>
      <div class="pf-small">${esc(R.source === "FRESH_RESEARCH" ? "Fresh research" : "Saved research")} from ${esc(when(R.as_of))} (${esc(window.StockResult ? window.StockResult.tidy(`${R.age_hours} hours old`) : `${R.age_hours} hours old`)})</div>
      ${R.stale ? `<div class="pf-banner pf-bad"><strong>RESEARCH IS STALE</strong> — it is older than ${esc(R.stale_after_hours)} hours. Use "Refresh research for this stock" before relying on it.</div>` : ""}`
      : `<div class="pf-banner pf-warn">No research is available for ${esc(c.symbol)} yet. Use "Refresh research for this stock".</div>`}`;
    // 6 MARKET → STOCK → PORTFOLIO
    h += `<div class="section"><h2>Market → Stock → Portfolio</h2><div class="pb-layers">
      <div><div class="pf-label">1 · Market</div>${state(L.market.environment)}<div class="pf-small">Trend ${esc(L.market.trend)} · Volatility ${esc(L.market.volatility)} · Breadth ${esc(L.market.breadth)}</div></div>
      <div><div class="pf-label">2 · Stock — ${esc(c.symbol)}</div>${state(R.research_view || "NO RESEARCH")}<div class="pf-small">Trend ${esc(c.stock_trend.trend)} · Momentum ${esc(c.stock_trend.momentum)}</div></div>
      <div><div class="pf-label">3 · Portfolio</div>${esc(P.setup)}<div class="pf-small">${esc(c.symbol)} ${pct(P.position_weight_before_pct)} → ${pct(P.position_weight_after_pct)}</div></div></div>
      <p><strong>Interpretation:</strong> ${esc(L.interpretation)}</p>
      <h3>Does adding now fit current conditions? ${state(c.fit.state)}</h3>
      <div class="pb-two"><div><h4>Why adding has support</h4>${factorList(c.fit.supporting, "✓")}</div>
      <div><h4>Why you may want to be cautious</h4>${factorList(c.fit.caution, "⚠")}</div></div>
      <p>${esc(c.fit.explanation)}</p><div class="pb-lesson">${esc(c.fit.lesson)}</div>
      <p class="hint">These labels describe the combination of current evidence and portfolio conditions. They are not predictions and not order instructions.</p></div>`;
    // 7 WHEN MIGHT CONDITIONS BE BETTER?
    const ev = c.events;
    h += `<div class="section"><h2>When might conditions be better?</h2><div>${state(T.status)} <span class="pf-small">Stock setup: ${esc(T.stock_setup)} · Portfolio setup: ${esc(T.portfolio_setup)}</span></div>
      <p>${esc(T.explanation)}</p>
      <div class="pf-kpis"><div class="pf-kpi"><div class="pf-label">Current price</div><div class="pf-value">${money(A.current_price)}</div><div class="pf-sub">${esc(A.price_source || "")} ${esc(when(A.price_time))}</div></div>
      <div class="pf-kpi"><div class="pf-label">Nearest support area</div><div class="pf-value">${money(A.support)}</div></div>
      <div class="pf-kpi"><div class="pf-label">Nearest resistance area</div><div class="pf-value">${money(A.resistance)}</div></div></div>
      <p><strong>${esc(A.location_text)}</strong></p><p class="pf-small">${esc(A.support_text)} ${esc(A.resistance_text)}</p>
      <div class="pb-two"><div><h4>Reasons conditions may support adding</h4>${factorList(T.supporting, "✓")}</div>
      <div><h4>Reasons to be patient</h4>${factorList(T.patience, "⚠")}</div></div>
      <h4>Watch for</h4>${T.watch_for.length ? `<ul class="mk-list">${T.watch_for.map((w) => `<li>□ ${esc(w)}</li>`).join("")}</ul>` : "<p class='pf-muted'>Nothing specific.</p>"}
      <h4>Event timing</h4>${ev.important_event_ahead ? `<div class="pf-banner pf-warn"><strong>IMPORTANT EVENT AHEAD</strong> — ${esc(ev.important_event_ahead.title)}, ${esc(ev.important_event_ahead.date)}</div>` : ""}
      ${ev.upcoming.length ? `<ul class="mk-list">${ev.upcoming.slice(0, 5).map((e) => `<li>${esc(e.title)} — ${esc(e.date)} <span class="pf-muted pf-small">(${esc(e.source)})</span></li>`).join("")}</ul>` : "<p class='pf-muted pf-small'>No verified upcoming events.</p>"}
      ${ev.earnings_note ? `<p class="pf-small">${esc(ev.earnings_note)}</p>` : ""}<p class="pf-small">${esc(ev.explanation)}</p>
      <h4>Add all at once vs hypothetical staged additions</h4><table class="data-table"><tr><th>Plan</th><th class="pf-num">Each addition</th><th class="pf-num">Weight after first</th><th class="pf-num">Weight after all*</th><th>Illustrative average price</th></tr>
      ${c.staged.plans.map((pl) => `<tr><td>${esc(pl.plan)}</td><td class="pf-num">${money(pl.first_addition)}${pl.additions > 1 ? ` × ${esc(pl.additions)}` : ""}</td><td class="pf-num">${pct(pl.weight_after_first_pct)}</td><td class="pf-num">${pct(pl.weight_after_all_pct)}</td>
        <td class="pf-small">${pl.illustrative_average_price ? Object.entries(pl.illustrative_average_price).map(([k, v]) => `${esc(k)}: ${money(v)}`).join("<br>") : "—"}</td></tr>`).join("")}</table>
      <p class="pf-small">* Assumes today's prices. ${esc(c.staged.explanation)} Illustrations use today's verified levels and are not forecasts. Nothing is scheduled or sent.</p>
      ${summaryCard(c)}
      <details class="pb-why"><summary>Why? (the actual metrics)</summary>${why(c)}</details></div>`;
    box.innerHTML = h;
    const sw = document.getElementById("pb-switch-new");
    if (sw) sw.addEventListener("click", () => { document.querySelector('input[name="pb-fund"][value="new_money"]').checked = true; runCheck(); });
  }

  function summaryCard(c) {
    const s = c.summary_card;
    return `<div class="pb-summary"><h3>${esc(s.title)}</h3><div class="pb-summary-grid">
      <div>Current price</div><div>${money(s.current_price)}</div>
      <div>Current research</div><div>${state(s.research_view)}${s.research_stale ? " <span class='cc-fresh cc-dim'>STALE</span>" : ""}</div>
      <div>Current stock trend</div><div>${esc(s.stock_trend)}</div><div>Current market</div><div>${esc(s.market)}</div>
      <div>Price location</div><div>${esc(LOCATION[s.price_location] || s.price_location)}</div><div>Event risk</div><div>${esc(s.event_risk)}</div>
      <div>Your current ${esc(c.symbol)} exposure</div><div>${pct(s.exposure_before_pct)}</div><div>After adding ${money(c.amount_usd)}</div><div>${pct(s.exposure_after_pct)}</div>
      <div>Your ${esc(s.sector === "UNCLASSIFIED" ? "unclassified-sector" : s.sector)} exposure</div><div>${pct(s.sector_before_pct)} → ${pct(s.sector_after_pct)}</div>
      <div>Timing conditions</div><div>${state(s.timing)}</div></div>
      <div class="pb-two"><div><h4>What supports adding</h4>${factorList(s.supports, "✓")}</div><div><h4>Why to be patient</h4>${factorList(s.patience, "⚠")}</div></div>
      <h4>What to watch</h4>${s.watch.length ? `<ul class="mk-list">${s.watch.map((w) => `<li>□ ${esc(w)}</li>`).join("")}</ul>` : "<p class='pf-muted'>Nothing specific.</p>"}
      <p class="pb-note">${esc(s.closing)}</p></div>`;
  }

  function why(c) {
    const t = c.stock_trend, w = t.why;
    const m = window.MarketContext && window.MarketContext.last;
    let h = `<table class="data-table"><tr><th>Label</th><th>Value</th><th>Based on</th></tr>
      <tr><td>Trend</td><td>${esc(t.trend)}</td><td class="pf-small">${w ? `EMA 9/20/50: ${esc(w.ema_9)} / ${esc(w.ema_20)} / ${esc(w.ema_50)}` : "unavailable"}</td></tr>
      <tr><td>Momentum</td><td>${esc(t.momentum)}</td><td class="pf-small">${w ? `momentum score ${esc(w.momentum_score)} (50 = neutral), 5-day ${spct(w.momentum_5d_pct)}, RSI ${esc(w.rsi)}` : "unavailable"}</td></tr>
      <tr><td>Price location</td><td>${esc(LOCATION[t.price_location] || t.price_location)}</td><td class="pf-small">${w ? `${spct(w.dist_from_support_pct)} from support, ${spct(w.dist_from_resistance_pct)} from resistance (near = within ${esc(w.near_level_rule_pct)}%)` : "unavailable"}</td></tr>
      <tr><td>Volume</td><td>${esc(t.volume)}</td><td class="pf-small">${w ? `relative volume ${esc(w.relative_volume)}x` : "unavailable"}</td></tr>
      <tr><td>Event risk</td><td>${esc(t.event_risk)}</td><td class="pf-small">Stage 2.6 event intelligence</td></tr>
      <tr><td>Research view</td><td>${esc(t.research_view)}</td><td class="pf-small">Stage 2 evidence engine (unchanged)</td></tr></table>
      ${w ? `<p class="pf-small">Stock data as of ${esc(when(w.as_of))}.</p>` : ""}`;
    if (m && m.available) {
      h += `<table class="data-table"><tr><th>Index</th><th class="pf-num">Today</th><th>Trend</th><th class="pf-num">5-day</th><th class="pf-num">20-day vol</th></tr>${m.indices.filter((i) => i.available).map((i) => `<tr><td>${esc(i.symbol)} <span class="pf-muted pf-small">${esc(i.role)}</span></td><td class="pf-num">${spct(i.pct_change)}</td><td>${esc(i.trend)}</td><td class="pf-num">${spct(i.momentum_5d_pct)}</td><td class="pf-num">${esc(i.volatility_pct)}%</td></tr>`).join("")}</table>`;
    }
    return h;
  }

  function currentReq() {
    const sel = document.getElementById("pb-sym").value;
    const sym = sel === "__other" ? document.getElementById("pb-sym-other").value.trim().toUpperCase() : sel;
    return { symbol: sym, amount_usd: Number(document.getElementById("pb-amt").value),
      funding: document.querySelector('input[name="pb-fund"]:checked').value };
  }

  async function runCheck(analysisId) {
    const req = currentReq();
    if (analysisId) req.analysis_id = analysisId;
    else if (lastReq && lastReq.analysis_id && lastReq.symbol === req.symbol) req.analysis_id = lastReq.analysis_id;
    lastReq = req;
    const box = document.getElementById("pb-check");
    box.innerHTML = "<p class='hint'>Checking current conditions…</p>";
    renderCheck(await getJSON("/api/portfolio/add-money-check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(req) }));
  }

  async function refreshResearch() {
    const req = currentReq();
    const box = document.getElementById("pb-check");
    box.innerHTML = `<p class='hint'>Running the Stage 2 research for ${esc(req.symbol)}… (this uses the AI call limits)</p>`;
    const r = await fetch(`/api/stocks/${encodeURIComponent(req.symbol)}/research`);
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.analysis_id) { box.innerHTML = `<div class="pf-banner pf-warn">Research could not be refreshed: ${esc(d.detail || r.status)}</div>`; return; }
    await runCheck(d.analysis_id);
  }

  function explainSection() {
    return `<div class="section"><h2>Explain my portfolio (AI)</h2><button id="pb-explain">Explain my portfolio (AI)</button><div id="pb-explain-out"></div>
      <p class="hint">The AI only explains the numbers and flags above. Every number is checked; anything unsupported is withheld.</p></div>`;
  }

  async function explain() {
    const out = document.getElementById("pb-explain-out");
    out.textContent = "Asking the AI to explain the calculated facts…";
    const body = lastCheck && lastReq ? { check: lastReq } : {};
    const d = await getJSON("/api/portfolio/explain", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (d.status === "PORTFOLIO_UNAVAILABLE") { out.textContent = `${d.reason}: ${d.message}`; return; }
    if (d.status !== "OK") { out.innerHTML = `<div class="pf-banner pf-warn">${esc(d.message)}</div>`; return; }
    const e = d.explanation;
    const sec = (title, items) => (items && items.length ? `<h3>${esc(title)}</h3><ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "");
    out.innerHTML = `<div class="ai-box">${sec("1 · Where you are now", e.where_you_are_now)}${sec("2 · What is going well", e.going_well)}
      ${sec("3 · What deserves attention (portfolio policy)", e.deserves_attention)}${sec("4 · What current research says (research view)", e.research_says)}
      ${sec("5 · What to watch next", e.watch_next)}${sec("Adding money", e.adding_money)}${e.caveats.length ? `<p class="hint">${e.caveats.map(esc).join(" · ")}</p>` : ""}
      <p class="hint">This does not mean any stock should automatically be bought or sold; it shows where current evidence and portfolio exposure agree or conflict.</p></div>`;
  }

  async function render(d, body) {
    body.innerHTML = `${account(d)}<div id="pb-market"><p class="hint">Loading current market…</p></div>${attention(d)}${positions(d)}${addMoneyForm(d)}${explainSection()}`;
    body.querySelectorAll(".pf-info").forEach((b) => b.addEventListener("click", () => b.nextElementSibling.classList.toggle("pf-tip-open")));
    body.querySelectorAll(".pb-review").forEach((b) => b.addEventListener("click", () => window.TraderReview && window.TraderReview.openPosition(b.dataset.sym)));
    const sel = document.getElementById("pb-sym");
    sel.addEventListener("change", () => { document.getElementById("pb-sym-other").style.display = sel.value === "__other" ? "" : "none"; });
    document.getElementById("pb-calc").addEventListener("click", () => runCheck());
    document.getElementById("pb-refresh-research").addEventListener("click", refreshResearch);
    document.getElementById("pb-explain").addEventListener("click", explain);
    const mbox = document.getElementById("pb-market");
    const showMarket = async (refresh) => {
      try {
        mbox.innerHTML = window.MarketContext.card(await window.MarketContext.load(refresh), { title: "Current market" });
        window.MarketContext.wire(mbox, () => showMarket(true));
      } catch (e) { mbox.innerHTML = `<div class="section"><h2>Current market</h2><p class="hint">Current market trend unavailable</p></div>`; }
    };
    if (pending) {                 // "What if I add $X?" from the quick trade check (Stage 2.7F)
      const { symbol, amount } = pending; pending = null;
      const has = [...sel.options].some((o) => o.value === symbol);
      sel.value = has ? symbol : "__other";
      const other = document.getElementById("pb-sym-other");
      other.style.display = has ? "none" : ""; if (!has) other.value = symbol;
      if (amount) document.getElementById("pb-amt").value = amount;
      document.getElementById("pb-sym").closest(".section").scrollIntoView({ behavior: "smooth" });
      runCheck(window.ResearchIds ? window.ResearchIds.get(symbol) : null);
    }
    await showMarket(false);
  }

  let pending = null;
  function openAddMoney(symbol, amount) {
    pending = { symbol, amount };
    if (window.PortfolioTab) window.PortfolioTab.beginner();
    const t = document.querySelector('.tab-btn[data-tab="portfolio"]');
    if (t) t.click();               // loads the portfolio, then render() applies the pending request
  }

  window.PortfolioBeginner = { render, openAddMoney };
})();
