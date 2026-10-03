// portfolio.js — Stage 2.7C read-only Portfolio tab.
//
// Renders numbers exactly as the backend computed them (Decimal strings); the
// browser only formats them for display. Nothing here can place, prepare or
// cancel an order — there is no endpoint for that anywhere in this app.
(function () {
  const body = document.getElementById("pf-body");
  if (!body) return;
  let mode = "beginner";
  try { mode = localStorage.getItem("pfMode") || "beginner"; } catch (e) { /* storage unavailable */ }
  let last = null;

  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const n = (v) => (v == null || v === "" ? null : Number(v));
  const money = (v) => (n(v) == null ? "N/A" : n(v).toLocaleString("en-US", { style: "currency", currency: "USD" }));
  const signedMoney = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : ""}${money(v)}`);
  const pctTxt = (v) => (n(v) == null ? "N/A" : `${n(v).toFixed(2)}%`);
  const signedPct = (v) => (n(v) == null ? "N/A" : `${n(v) >= 0 ? "+" : ""}${n(v).toFixed(2)}%`);
  const qty = (v) => (n(v) == null ? "N/A" : n(v).toLocaleString("en-US", { maximumFractionDigits: 6 }));
  const cls = (v) => (n(v) == null ? "" : n(v) >= 0 ? "pct-up" : "pct-down");
  const when = (v) => (v ? new Date(v).toLocaleString() : "N/A");
  const age = (s) => (s == null ? "N/A" : s < 120 ? `${s}s` : s < 7200 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`);
  const q = (quality) => `<span class="pf-q pf-q-${esc(quality)}">${esc(quality)}</span>`;
  const ev = (lvl) => `<span class="event-risk-badge event-risk-${esc(lvl)}">${esc(lvl)}</span>`;

  async function getJSON(path, options) {
    const res = await fetch(path, options);
    return res.json();
  }

  function setMode(m) {
    mode = m;
    try { localStorage.setItem("pfMode", m); } catch (e) { /* ignore */ }
    document.querySelectorAll("#pf-mode-toggle button[data-mode]").forEach((b) =>
      b.classList.toggle("active", b.dataset.mode === m));
    body.classList.toggle("pf-advanced", m === "advanced");
  }

  const UNAVAILABLE_HELP = {
    DISABLED: "Portfolio awareness is off. Set PORTFOLIO_AWARENESS_ENABLED=true in stock-agent/.env and start the gateway.",
    GATEWAY_DOWN: "The local Robinhood gateway isn't running. Start it with: python -m rh_gateway serve (in the rh_gateway folder).",
    GATEWAY_SECRET_UNAVAILABLE: "The gateway's local access key wasn't found. Start the gateway once so it can create it.",
    REAUTH_REQUIRED: "Robinhood authorization is needed. Run: python -m rh_gateway login (in the rh_gateway folder).",
    GATEWAY_REJECTED: "The gateway rejected this app's request. Restart the gateway, then retry.",
  };

  function renderUnavailable(d) {
    const c = d.connection;
    if (c && !c.connected) {   // Stage 2.7F beginner wording; technical detail stays collapsed
      body.innerHTML = `<div class="unavailable-panel cc-down"><h2>${esc(c.title)}</h2><p>${esc(c.message)}</p>
        <div class="cc-label">HOW TO FIX</div><ol class="cc-steps">${c.how_to_fix.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
        <p class="hint">${esc(c.note)}</p>
        <details class="cc-more"><summary>Technical details</summary><div class="pf-small">${esc(d.reason || "")} — ${esc(d.message || "")}</div></details></div>`;
      return;
    }
    body.innerHTML = `<div class="unavailable-panel"><h2>Portfolio unavailable</h2>
      <p><strong>${esc(d.reason || "")}</strong> — ${esc(d.message || "")}</p>
      <p class="hint">${esc(UNAVAILABLE_HELP[d.reason] || "Everything else in the dashboard keeps working normally.")}</p></div>`;
  }

  // ---- Stage 2.7D Portfolio Attention (deterministic policy flags; never trading instructions) ----
  const STATUS_TEXT = {
    HIGH: "High — review before adding more exposure",
    MEDIUM: "Elevated — worth reviewing",
    LOW: "Low — worth noting",
    INFO: "Noted",
    OK: "No attention flag",
    UNAVAILABLE: "Not enough data",
  };
  const opTxt = (o) => ({ ">=": "≥", "<=": "≤" }[o] || o);
  const sev = (x) => `<span class="pf-sev pf-sev-${esc(x)}">${esc(x)}</span>`;
  const unitVal = (v, unit) => (v == null ? "N/A" : unit === "positions" ? `${Number(v).toFixed(0)}` : `${esc(v)}%`);

  function cardMetric(c) {
    if (c.metric_value == null) return c.card === "data_quality" ? `${c.flags.length} flag(s)` : "N/A";
    if (c.card === "cash_level") return `${esc(c.metric_value)}% cash`;
    if (c.card === "event_exposure") return `${esc(c.metric_value)}% in HIGH event risk`;
    return `${esc(c.metric_value)}% ${esc(c.metric_subject)}`;
  }

  function renderAttention(policy) {
    if (!policy) return "";
    if (!policy.enabled) {
      return `<div class="section"><h2>Portfolio Attention</h2><p class="hint">${esc(policy.message || "Policy engine off.")}</p></div>`;
    }
    let h = `<div class="section"><h2>Portfolio Attention</h2>
      <p class="hint">${esc(policy.note)} <strong>${esc(policy.separation_note)}</strong></p><div class="pf-att-grid">`;
    policy.cards.forEach((c) => {
      const th = c.thresholds.filter((t) => t.enabled).map((t) => `${esc(t.severity)} ${esc(opTxt(t.operator))} ${esc(t.threshold)}%`).join(" · ");
      h += `<div class="pf-att pf-att-${esc(c.status)}">
        <div class="pf-att-head"><span class="pf-label">${esc(c.title)}</span>${sev(c.status)}</div>
        <div class="pf-value">${cardMetric(c)}</div>
        <div class="pf-sub">${th ? `Configured levels: ${th}` : c.card === "data_quality" ? "Quote, valuation, cost-basis and sector-coverage rules" : ""}</div>
        <div class="pf-att-status">${esc(STATUS_TEXT[c.status] || c.status)}</div>
        <div class="pf-small">${esc(window.StockResult ? window.StockResult.tidy(c.explanation) : c.explanation)}</div>
        ${c.flags.length > 1 ? `<div class="pf-small pf-muted">${c.flags.length} flags in this group (see Advanced).</div>` : ""}
      </div>`;
    });
    h += `</div>`;
    h += `<div class="pf-adv"><h3>PORTFOLIO POLICY does not modify RESEARCH EVIDENCE</h3>
      <p class="hint">Flags are decided by Python from configured thresholds. Research evidence, the Research View and event scoring are unchanged by anything here.</p>
      <h3>Triggered flags</h3><table class="data-table"><tr><th>Rule id</th><th>Category</th><th>Subject</th><th class="pf-num">Value</th><th>Threshold</th><th>Severity</th><th>Metric source</th></tr>
      ${policy.flags.map((f) => `<tr><td>${esc(f.rule_id)}</td><td>${esc(f.category)}</td><td>${esc(f.subject)}</td>
        <td class="pf-num">${unitVal(f.value, f.unit)}</td><td>${esc(opTxt(f.operator))} ${unitVal(f.threshold, f.unit)}</td><td>${sev(f.severity)}</td>
        <td class="pf-small">${esc(f.metric_source)}</td></tr>`).join("") || '<tr><td colspan="7">No flags triggered.</td></tr>'}
      </table>
      <h3>All configured rules</h3><table class="data-table"><tr><th>Rule id</th><th>Name</th><th>Metric</th><th>Rule</th><th>Severity</th><th>State</th></tr>
      ${policy.rules.map((r) => `<tr class="${r.enabled ? "" : "pf-muted"}"><td>${esc(r.id)}</td><td>${esc(r.name)}</td><td>${esc(r.metric)}</td>
        <td>${esc(opTxt(r.operator))} ${unitVal(r.threshold, r.unit)}</td><td>${sev(r.severity)}</td><td>${r.enabled ? "active" : "inactive"}</td></tr>`).join("")}
      </table>
      ${policy.rule_errors.length ? `<div class="pf-banner pf-warn">Configuration issues: ${esc(policy.rule_errors.join("; "))}</div>` : ""}
    </div></div>`;
    return h;
  }

  function renderImpact(impact) {
    if (!impact) return "";
    const rows = [];
    [["newly_triggered", "Newly flagged"], ["escalated", "Higher severity"], ["de_escalated", "Lower severity"],
     ["resolved", "No longer flagged"], ["unchanged", "Unchanged flags"]].forEach(([key, label]) => {
      impact[key].forEach((i) => rows.push(`<tr><td>${esc(label)}</td><td>${esc(i.family)}</td><td>${esc(i.subject)}</td>
        <td class="pf-num">${i.before_value == null ? "N/A" : esc(i.before_value)} → ${i.after_value == null ? "N/A" : esc(i.after_value)}</td>
        <td>${i.before_severity ? sev(i.before_severity) : "—"} → ${i.after_severity ? sev(i.after_severity) : "—"}</td></tr>`));
    });
    return `<h3>Policy impact (hypothetical)</h3><p class="hint">${esc(impact.note)} ${esc(impact.separation_note)}</p>
      <table class="data-table"><tr><th>Change</th><th>Policy</th><th>Subject</th><th class="pf-num">Before → after</th><th>Severity</th></tr>
      ${rows.join("") || '<tr><td colspan="5">No policy flags before or after.</td></tr>'}</table>`;
  }

  function kpi(label, value, sub) {
    return `<div class="pf-kpi"><div class="pf-label">${esc(label)}</div><div class="pf-value">${value}</div>
      ${sub ? `<div class="pf-sub">${sub}</div>` : ""}</div>`;
  }

  function render(d) {
    last = d;
    if (d.status === "PORTFOLIO_UNAVAILABLE") return renderUnavailable(d);
    if (mode === "beginner" && window.PortfolioBeginner) {
      body.classList.remove("pf-advanced");
      return window.PortfolioBeginner.render(d, body);
    }
    return renderAdvanced(d);
  }

  function renderAdvanced(d) {
    last = d;
    if (d.status === "PORTFOLIO_UNAVAILABLE") return renderUnavailable(d);
    const s = d.snapshot;
    const qs = d.quote_quality_summary;
    const totalDay = d.positions.reduce((a, p) => (p.day_change == null ? a : a + Number(p.day_change)), 0);
    let html = "";

    if (d.status === "STALE") {
      html += `<div class="pf-banner pf-warn">Robinhood could not be reached just now — showing the last successful data (fetched ${esc(when(s.fetched_at))}). Marked STALE.</div>`;
    }
    [...new Set(d.messages || [])].forEach((m) => { html += `<div class="pf-banner pf-info">${esc(m)}</div>`; });

    html += `<div class="pf-kpis">
      ${kpi("Portfolio value (Robinhood)", money(s.total_value), `Account ${esc(s.account_alias)} ${esc(s.account_masked_id)}`)}
      ${kpi("Cash", money(s.cash), `${pctTxt(s.cash_pct)} of calculated total`)}
      ${kpi("Current exposure (positions)", money(s.positions_market_value), `Day change ${signedMoney(totalDay)} (calculated)`)}
      ${kpi("Cost basis", money(s.total_cost_basis), d.basis_summary.missing ? `${d.basis_summary.missing} position(s) missing basis` : "All positions have basis")}
      ${kpi("Unrealized P&L", `<span class="${cls(s.total_unrealized_pnl)}">${signedMoney(s.total_unrealized_pnl)}</span>`, `${signedPct(s.total_unrealized_pnl_pct)} on cost${s.unrealized_pnl_complete ? "" : " (incomplete)"}`)}
      ${kpi("Data quality", q(qs.worst), `Fetched ${esc(when(d.freshness.positions.fetched_at))}`)}
    </div>`;

    html += renderAttention(d.policy);

    if (s.valuation_gap_material || mode === "advanced") {
      html += `<div class="section"><h2>${s.valuation_gap_material ? "⚠️ " : ""}Robinhood value vs calculated value</h2>
        <div class="pf-gap">
          ${kpi("ROBINHOOD REPORTED EQUITY VALUE", money(s.equity_value), "As reported by Robinhood (no as-of time provided)")}
          ${kpi("CALCULATED POSITION VALUE", money(s.positions_market_value), "Shares × selected quote, calculated here")}
        </div>
        <p class="hint">Difference: ${signedMoney(s.valuation_gap)} (${signedPct(s.valuation_gap_pct)}).
        ${s.valuation_gap_material ? "This gap is above the configured display threshold, so both values are shown. They can differ because Robinhood's price basis and timing aren't disclosed." : ""}
        ${s.valuation_complete ? "" : "Some positions could not be valued, so no gap is computed."}</p></div>`;
    }

    html += `<div class="section"><h2>Positions</h2><table class="data-table"><tr>
      <th>Symbol</th><th class="pf-num">Shares</th><th class="pf-num">Avg cost</th><th class="pf-num">Cost basis</th>
      <th class="pf-num">Price</th><th class="pf-num">Current value</th><th class="pf-num">Unrealized P&amp;L</th>
      <th class="pf-num">Weight</th><th>Sector</th><th>Event risk</th><th>Quote</th>
      <th class="pf-adv pf-num">Day change</th><th class="pf-adv">Price time / source</th><th class="pf-adv">Basis</th><th class="pf-adv">Lots</th></tr>`;
    d.positions.forEach((p) => {
      html += `<tr>
        <td><strong>${esc(p.symbol)}</strong></td>
        <td class="pf-num">${qty(p.quantity)}</td>
        <td class="pf-num">${p.basis_available ? money(p.avg_cost) : '<span class="pf-muted">unavailable</span>'}</td>
        <td class="pf-num">${p.basis_available ? money(p.cost_basis_total) : '<span class="pf-muted">unavailable</span>'}</td>
        <td class="pf-num">${money(p.last_price)}</td>
        <td class="pf-num">${money(p.market_value)}</td>
        <td class="pf-num ${cls(p.unrealized_pnl)}">${signedMoney(p.unrealized_pnl)}<br><span class="pf-small">${signedPct(p.unrealized_pnl_pct)}</span></td>
        <td class="pf-num">${pctTxt(p.portfolio_weight)}</td>
        <td>${esc(p.sector)}</td>
        <td>${ev(p.event_risk_level)}${p.nearest_event ? `<div class="pf-small pf-muted">${esc(p.nearest_event.title)} · ${esc(p.nearest_event.event_date)}</div>` : ""}</td>
        <td>${q(p.quote_quality)}${p.quote_issues.length ? `<div class="pf-small pf-muted">${esc(p.quote_issues.join(", "))}</div>` : ""}</td>
        <td class="pf-adv pf-num ${cls(p.day_change)}">${signedMoney(p.day_change)}${p.day_change_partial ? "*" : ""}</td>
        <td class="pf-adv pf-small">${esc(when(p.price_timestamp))}<br>${esc(p.price_source || "")} · age ${esc(age(p.quote_age_seconds))}</td>
        <td class="pf-adv pf-small">${p.basis_available ? "available" : "UNAVAILABLE"}</td>
        <td class="pf-adv"><button class="pf-lots-btn" data-symbol="${esc(p.symbol)}">Tax lots</button></td>
      </tr>`;
    });
    html += `</table><div id="pf-lots" class="pf-lots"></div>
      <p class="hint pf-adv">* Day change excludes shares bought in the current session. Weights use the calculated total (positions + cash + other reported assets).</p></div>`;

    html += `<div class="section-grid"><div class="section"><h2>Sector exposure</h2>`;
    d.sector_exposure.forEach((x) => {
      html += `<div class="pf-bar-row"><span>${esc(x.sector)} <span class="pf-muted pf-small">(${esc(x.symbols.join(", "))})</span></span>
        <div class="pf-bar"><span style="width:${Math.max(0, Math.min(100, n(x.weight_pct) || 0))}%"></span></div><span class="pf-num">${pctTxt(x.weight_pct)}</span></div>`;
    });
    if (d.sector_exposure.some((x) => x.sector === "UNCLASSIFIED")) {
      html += `<p class="hint">UNCLASSIFIED = not in the app's curated sector list; no sector is guessed.</p>`;
    }
    html += `</div><div class="section"><h2>Known event risk</h2>`;
    d.event_exposure.forEach((e) => {
      html += `<div class="pf-bar-row"><span>${ev(e.level)} <span class="pf-muted pf-small">${esc(e.symbols.join(", "))}</span></span>
        <div class="pf-bar"><span style="width:${Math.max(0, Math.min(100, n(e.weight_pct) || 0))}%"></span></div><span class="pf-num">${pctTxt(e.weight_pct)}</span></div>`;
    });
    html += `<p class="hint">From the Stage 2.6 event calendar (earnings data may be unavailable). A scheduled event is not bullish or bearish by itself.</p></div></div>`;

    html += `<div class="pf-adv"><div class="section"><h2>Advanced details</h2><div class="pf-kpis">
      ${kpi("Buying power", money(s.buying_power), `Unleveraged ${money(s.unleveraged_buying_power)}`)}
      ${kpi("Realized P&L", signedMoney(s.realized_pnl_window), `Span ${esc(s.realized_pnl_span || "N/A")}`)}
      ${kpi("Calculated total", money(s.calculated_total_value), `Robinhood total ${money(s.total_value)}`)}
      ${kpi("Quotes", `${qs.counts.OK} OK`, `${qs.counts.STALE} stale · ${qs.counts.DEGRADED} degraded · ${qs.counts.UNRELIABLE + qs.counts.UNAVAILABLE} unusable (stale after ${esc(qs.stale_after_seconds)}s)`)}
    </div>
    <p class="hint">Portfolio fetched ${esc(when(d.freshness.portfolio.fetched_at))} (cache age ${esc(age(d.freshness.portfolio.cache_age_s))}); positions fetched ${esc(when(d.freshness.positions.fetched_at))}. Robinhood provides no as-of time for these values.</p>
    <h2>Raw risk facts</h2><p class="hint">${esc(d.risk_facts.note)} Rules configured: ${esc(d.risk_facts.rules_configured)}.</p>
    <table class="data-table"><tr><th>Fact</th><th class="pf-num">Value</th></tr>
      ${Object.entries(d.risk_facts.portfolio).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="pf-num">${esc(v == null ? "N/A" : v)}</td></tr>`).join("")}
    </table>
    <h2>Realized P&amp;L</h2><div id="pf-realized"><button id="pf-realized-btn">Load realized P&amp;L</button></div></div></div>`;

    html += `<div class="section"><h2>What-if (arithmetic only)</h2>
      <div class="pf-form">
        <select id="pf-sc-type">
          <option value="hypothetical_add">Add $ to a stock</option>
          <option value="cash_after_purchase">Cash after a purchase</option>
          <option value="event_risk_exposure">Share in HIGH event risk</option>
        </select>
        <input id="pf-sc-symbol" placeholder="Symbol (e.g. MU)" maxlength="10" size="10" />
        <input id="pf-sc-amount" type="number" min="1" step="1" placeholder="Amount $" size="10" />
        <select id="pf-sc-funding"><option value="cash">from cash</option><option value="new_money">new money</option></select>
        <button id="pf-sc-run">Calculate</button>
        <button id="pf-explain-btn">Explain my portfolio (AI)</button>
      </div>
      <div id="pf-sc-result"></div><div id="pf-explain"></div>
      <p class="hint">Scenarios are simulations only — no order is created, prepared or sent, and nothing in your account changes.</p></div>`;

    body.innerHTML = html;
    setMode(mode);
    body.querySelectorAll(".pf-lots-btn").forEach((b) => b.addEventListener("click", () => loadLots(b.dataset.symbol)));
    const rb = document.getElementById("pf-realized-btn");
    if (rb) rb.addEventListener("click", loadRealized);
    document.getElementById("pf-sc-run").addEventListener("click", runScenario);
    document.getElementById("pf-explain-btn").addEventListener("click", explain);
  }

  async function loadLots(symbol) {
    const box = document.getElementById("pf-lots");
    box.textContent = `Loading tax lots for ${symbol}…`;
    const d = await getJSON(`/api/portfolio/tax-lots/${encodeURIComponent(symbol)}`);
    if (d.status === "PORTFOLIO_UNAVAILABLE") { box.textContent = `${d.reason}: ${d.message}`; return; }
    let h = `<h3>${esc(symbol)} tax lots</h3><p class="hint">${esc(d.note)} Quote ${q(d.quote.quality)}.</p>
      <table class="data-table"><tr><th>Opened</th><th>Type</th><th class="pf-num">Shares</th><th class="pf-num">Cost/share</th>
      <th class="pf-num">Basis</th><th class="pf-num">Value</th><th class="pf-num">Unrealized</th><th>Term</th><th class="pf-num">Days held</th><th class="pf-num">Days to long-term</th></tr>`;
    d.tax_lots.forEach((l) => {
      h += `<tr><td>${esc(l.open_date)}</td><td>${esc(l.open_type)}</td><td class="pf-num">${qty(l.quantity)}</td>
        <td class="pf-num">${l.basis_pending ? "pending" : money(l.cost_per_share)}</td><td class="pf-num">${l.basis_pending ? "pending" : money(l.cost_basis)}</td>
        <td class="pf-num">${money(l.market_value)}</td><td class="pf-num ${cls(l.unrealized_pnl)}">${signedMoney(l.unrealized_pnl)}</td>
        <td>${esc(l.term)}</td><td class="pf-num">${esc(l.days_held)}</td><td class="pf-num">${esc(l.days_to_long_term)}</td></tr>`;
    });
    box.innerHTML = h + "</table>";
  }

  async function loadRealized() {
    const box = document.getElementById("pf-realized");
    box.textContent = "Loading realized P&L…";
    const d = await getJSON("/api/portfolio/realized-pnl?span=3month");
    if (d.status === "PORTFOLIO_UNAVAILABLE") { box.textContent = `${d.reason}: ${d.message}`; return; }
    const r = d.realized_pnl;
    let h = `<p>Last ${esc(r.span)}: <strong class="${cls(r.total_returns)}">${signedMoney(r.total_returns)}</strong>
      (${signedPct(n(r.total_rate_of_return) == null ? null : n(r.total_rate_of_return) * 100)}) across ${esc(r.closing_trades)} closing trade(s). <span class="hint">${esc(d.note)}</span></p>
      <table class="data-table"><tr><th>When</th><th>Symbol</th><th>Side</th><th class="pf-num">Qty</th><th class="pf-num">Price</th><th class="pf-num">Realized</th></tr>`;
    r.trades.forEach((t) => {
      h += `<tr><td>${esc(when(t.timestamp))}</td><td>${esc(t.symbol)}</td><td>${esc(t.side)}</td><td class="pf-num">${qty(t.quantity)}</td>
        <td class="pf-num">${money(t.price)}</td><td class="pf-num ${cls(t.realized_gain)}">${signedMoney(t.realized_gain)}</td></tr>`;
    });
    box.innerHTML = h + "</table>";
  }

  function scenarioRequest() {
    const type = document.getElementById("pf-sc-type").value;
    const req = { type };
    if (type === "hypothetical_add") {
      req.symbol = document.getElementById("pf-sc-symbol").value.trim().toUpperCase();
      req.funding = document.getElementById("pf-sc-funding").value;
    }
    if (type !== "event_risk_exposure") req.amount_usd = Number(document.getElementById("pf-sc-amount").value);
    return req;
  }

  async function runScenario() {
    const box = document.getElementById("pf-sc-result");
    box.textContent = "Calculating…";
    const d = await getJSON("/api/portfolio/scenario", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(scenarioRequest()) });
    if (d.status === "PORTFOLIO_UNAVAILABLE" || d.status === "SCENARIO_INVALID" || d.detail) {
      box.textContent = d.message || (d.detail ? "Please check the scenario inputs." : d.reason); return;
    }
    const r = d.result;
    const skip = ["scenario", "inputs", "disclaimer", "policy_impact", "funding_explanation", "sector_note"];
    const rows = Object.entries(r).filter(([k]) => !skip.includes(k));
    box.innerHTML = `${r.funding_explanation ? `<p class="hint">${esc(r.funding_explanation)}</p>` : ""}
      ${r.sector_note ? `<div class="pf-banner pf-warn">${esc(r.sector_note)}</div>` : ""}
      <table class="data-table">${rows.map(([k, v]) => `<tr><td>${esc(k.replace(/_/g, " "))}</td><td class="pf-num">${esc(Array.isArray(v) ? v.join(", ") : v == null ? "N/A" : v)}</td></tr>`).join("")}</table>
      ${renderImpact(r.policy_impact)}
      <p class="hint">${esc(r.disclaimer)}</p>`;
  }

  async function explain() {
    const box = document.getElementById("pf-explain");
    box.textContent = "Asking the AI to explain the calculated facts…";
    const type = document.getElementById("pf-sc-type").value;
    const withScenario = document.getElementById("pf-sc-result").innerHTML.trim() !== "";
    const d = await getJSON("/api/portfolio/explain", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(withScenario && type ? { scenario: scenarioRequest() } : {}) });
    if (d.status === "PORTFOLIO_UNAVAILABLE") { box.textContent = `${d.reason}: ${d.message}`; return; }
    if (d.status !== "OK") { box.innerHTML = `<div class="pf-banner pf-warn">${esc(d.message)}</div>`; return; }
    const e = d.explanation;
    const list = (items) => `<ul>${items.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>`;
    box.innerHTML = `<div class="ai-box"><p>${esc(e.summary)}</p>
      <h3>Research view <span class="pf-muted pf-small">(saved research — may be outdated)</span></h3>${e.research_view.length ? list(e.research_view) : '<p class="pf-muted">No saved research mentioned.</p>'}
      <h3>Portfolio policy <span class="pf-muted pf-small">(attention flags from configured rules)</span></h3>${list(e.portfolio_policy)}
      ${e.scenario.length ? `<h3>Scenario</h3>${list(e.scenario)}` : ""}
      ${e.caveats.length ? `<p class="hint">${e.caveats.map(esc).join(" · ")}</p>` : ""}
      <p class="hint">AI explanation of numbers and flags calculated by the app; every number was checked against those facts. Research and portfolio policy are separate. Not advice.</p></div>`;
  }

  async function load() {
    body.textContent = "Loading portfolio…";
    try {
      const res = await fetch("/api/portfolio");
      const d = await res.json().catch(() => null);
      if (!res.ok || !d || (d.status !== "PORTFOLIO_UNAVAILABLE" && !(d.snapshot && Array.isArray(d.positions)))) {
        renderUnavailable({ reason: "SERVER_ERROR", message: `The dashboard server returned an unexpected response (HTTP ${res.status}). If it was started before portfolio support was added, restart it.` });
        return;
      }
      render(d);
    } catch (err) {
      body.innerHTML = `<div class="unavailable-panel"><h2>Portfolio unavailable</h2><p>${esc(err.message)}</p></div>`;
    }
  }

  document.querySelectorAll("#pf-mode-toggle button[data-mode]").forEach((b) =>
    b.addEventListener("click", () => { setMode(b.dataset.mode); if (last) render(last); }));
  document.getElementById("pf-refresh-btn").addEventListener("click", load);
  const tabBtn = document.querySelector('.tab-btn[data-tab="portfolio"]');
  if (tabBtn) tabBtn.addEventListener("click", load);
  setMode(mode);
  window.PortfolioTab = { beginner() { setMode("beginner"); } };   // used by "What if I add $X?" (Stage 2.7F)
})();
