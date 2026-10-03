// alpaca_paper.js — Stage 4.6A ALPACA PAPER · READ ONLY (the second view of the Paper Portfolio workspace).
//
// Shows your Alpaca PAPER account — cash, equity, positions, recent orders, recent fills — next to (never merged with) the
// local simulator, and describes the differences. Broker data is read ONLY when you press "Refresh Alpaca Paper" (at most
// four read-only requests on the server); opening this view reads the last refresh from the server's memory (no broker
// call). There is no order, cancel, replace or close control here; nothing is synced, imported or linked; no timers, no
// polling, no AI. Values arrive as exact decimal strings; the account number arrives already masked.
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const day = (iso) => (iso ? new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }) : "—");
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  const usd = (s) => (s == null ? "—" : `${String(s).startsWith("-") ? "−" : ""}$${Number(String(s).replace("-", "")).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
  const pnl = (s) => (s == null ? "—" : `${!String(s).startsWith("-") && Number(s) > 0 ? "+" : ""}${usd(s)}`);
  const px = (s) => (s == null ? "—" : Number(s).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 }));
  const pct = (s) => (s == null ? "" : `${Number(s) > 0 ? "+" : ""}${(Number(s) * 100).toFixed(2)}%`);
  const kind = (s) => (s == null || Number(s) === 0 ? "dim" : Number(s) > 0 ? "ok" : "alert");
  const sh = (s) => (s == null ? "—" : String(s).replace("-", "−"));
  const CONN = { NOT_CONFIGURED: "dim", READY: "info", REFRESHING: "info", CONNECTED: "ok", AUTH_ERROR: "alert",
    PAPER_API_UNAVAILABLE: "warn", PARTIAL_DATA: "warn", ERROR: "alert" };
  const POS = { MATCH: ["MATCH", "ok"], DIFFERENT: ["DIFFERENT", "warn"], LOCAL_ONLY: ["LOCAL ONLY", "info"], ALPACA_ONLY: ["ALPACA ONLY", "info"] };

  let el = null, data = null, seq = 0, refreshing = false, notice = "", requests = 0, renderMs = null;

  const api = (method, url) => {
    requests++;
    return fetch(url, method === "GET" ? {} : { method, headers: { "Content-Type": "application/json" }, body: "{}" })
      .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }))
      .catch(() => ({ status: 0, body: { message: "The server did not answer. Nothing changed." } }));
  };
  // a late answer never replaces a fresher one: order by (refresh time, finished) — never by arrival
  const rank = (b) => [b.refreshed_at || "", b.in_flight ? 0 : 1];
  const fresher = (b) => { if (!data) return true; const a = rank(b), c = rank(data); return a[0] > c[0] || (a[0] === c[0] && a[1] >= c[1]); };

  async function load() {
    const my = ++seq;
    const r = await api("GET", "/api/alpaca-paper/view");
    if (r.status === 200 && fresher(r.body)) { data = r.body; notice = ""; }
    else if (r.status !== 200 && my === seq) notice = r.body.message || "The Alpaca paper view could not be read.";
    draw();
  }
  async function refresh() {
    if (refreshing) return;                                   // one refresh at a time (the server also joins duplicates)
    refreshing = true; notice = ""; ++seq; draw();
    const r = await api("POST", "/api/alpaca-paper/refresh");
    refreshing = false;
    if (r.status === 200 && fresher(r.body)) data = r.body;
    else if (r.status !== 200) notice = r.body.message || "The refresh could not be completed. Nothing changed.";
    draw();
  }
  function onClick(e) {
    const b = e.target.closest("button[data-apx]");
    if (!b || b.disabled) return;
    if (b.dataset.apx === "refresh") refresh();
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------------
  const stat = (label, v, k) => `<div class="ppf-stat"><span class="cc-label">${esc(label)}</span><div class="ppf-num${k ? ` ppf-${esc(k)}` : ""}">${v}</div></div>`;
  function head(d) {
    const st = refreshing ? "REFRESHING" : d ? d.connection.state : null;
    const c = d ? d.connection : null;
    const warn = d && d.warnings && d.warnings.length ? `<ul class="cc-small apx-warn">${d.warnings.map((w) => `<li data-apx-warn="${esc(w.code)}">${tag(w.section.toUpperCase(), "warn")} ${esc(w.text)}${w.http_status ? ` <span class="cc-dimtext">(HTTP ${esc(w.http_status)})</span>` : ""}</li>`).join("")}</ul>` : "";
    const msg = refreshing ? "Reading the Alpaca paper account…" : c ? c.message : notice || "Loading…";
    const can = c && c.configured && !refreshing;           // during another refresh, a click joins it on the server
    return `<section class="cc-card apx-head" data-apx-state="${esc(st || "")}">
      <div class="cc-head"><h2>ALPACA PAPER <span class="cc-small cc-dimtext">your Alpaca paper account</span></h2><span class="apx-strip">READ ONLY · PAPER ACCOUNT · NO BROKER ACTIONS</span></div>
      <div class="apx-conn cc-small"><span><span class="cc-label">CONNECTION</span> ${c ? (c.configured ? "Configured" : "Not configured") : "—"} ${st ? tag(st.replace(/_/g, " "), CONN[st] || "dim") : ""}</span>
        <span><span class="cc-label">BROKER DATA REFRESHED AT</span> ${d && d.refreshed_at ? esc(whenNY(d.refreshed_at)) : "not refreshed yet"}</span></div>
      <div class="cc-small apx-msg">${esc(msg)}</div>${warn}${notice && c ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}
      <div class="ppf-actions"><button type="button" class="cc-btn cc-mini cc-primary" data-apx="refresh"${can ? "" : " disabled"}>${refreshing ? "Refreshing…" : "Refresh Alpaca Paper"}</button>
        <span class="cc-small cc-dimtext">Reads only: the account, positions, up to ${esc(d ? d.source.limits.orders : 100)} recent orders and ${esc(d ? d.source.limits.fills : 100)} recent fills — ${esc(d ? d.source.requests_per_refresh : 4)} requests. Nothing at Alpaca or in the local simulator is changed.</span></div></section>`;
  }
  function notConfigured(d) {
    const [k, s] = d.connection.credential_names;
    return `<section class="cc-card apx-setup"><div class="cc-head"><h2>CONNECT YOUR ALPACA PAPER ACCOUNT <span class="cc-small cc-dimtext">optional · read only</span></h2></div>
      <p class="cc-small">Add these two lines to the local <code>.env</code> file next to the app (paper keys from your Alpaca paper dashboard), then restart the app:</p>
      <pre class="apx-env">${esc(k)}=&lt;your paper key id&gt;\n${esc(s)}=&lt;your paper secret key&gt;</pre>
      <p class="cc-small cc-dimtext">Only these two names are used. The market-data keys are never used for this, and the keys are never shown, sent to the browser or stored by Stock Agent. Never paste a secret into a chat or a web page.</p></section>`;
  }
  function account(d) {
    const a = d.account;
    if (!a) return d.sections && d.sections.account.status !== "OK" ? `<section class="cc-card apx-account"><div class="cc-head"><h2>ALPACA PAPER ACCOUNT</h2></div><p class="cc-small cc-dimtext">Not available in this refresh.</p></section>` : "";
    return `<section class="cc-card apx-account"><div class="cc-head"><h2>ALPACA PAPER ACCOUNT <span class="cc-small cc-dimtext">${esc(a.account_number_masked || "")} · ${esc(a.currency || "")} · simulated money at Alpaca — not your real portfolio</span></h2></div>
      <div class="ppf-stats">${stat("ALPACA PAPER CASH", esc(usd(a.cash)))}${stat("ALPACA PAPER EQUITY", esc(usd(a.equity)))}${stat("PORTFOLIO VALUE", esc(usd(a.portfolio_value)))}
        ${stat("BUYING POWER", esc(usd(a.buying_power)))}${stat("ACCOUNT STATUS", esc(a.status || "—"))}</div>
      <div class="cc-small cc-dimtext">Values as reported by Alpaca at the refresh time.${a.trading_blocked || a.account_blocked ? ` <b>Alpaca reports this paper account as ${a.account_blocked ? "blocked" : "blocked for new orders"}.</b>` : ""} Created ${esc(a.created_at ? day(a.created_at.slice(0, 10)) : "—")}.</div></section>`;
  }
  function positions(d) {
    if (d.sections && d.sections.positions.status !== "OK") return `<section class="cc-card apx-positions"><div class="cc-head"><h2>ALPACA PAPER POSITIONS</h2></div><p class="cc-small cc-dimtext">Not available in this refresh.</p></section>`;
    const rows = d.positions.map((p) => { const s = POS[p.status] || [p.status || "—", "dim"];
      return `<tr data-apx-pos="${esc(p.symbol)}"><td class="ppf-sym">${esc(p.symbol)}</td><td>${esc(sh(p.qty))}</td><td>${esc(px(p.avg_entry_price))}</td><td>${esc(usd(p.market_value))}</td>
        <td>${tag(`${pnl(p.unrealized_pl)} ${pct(p.unrealized_plpc)}`.trim(), kind(p.unrealized_pl))}</td><td>${esc(p.local_shares == null ? "—" : sh(p.local_shares))}</td><td>${tag(s[0], s[1])}</td></tr>`; }).join("");
    return `<section class="cc-card apx-positions"><div class="cc-head"><h2>ALPACA PAPER POSITIONS <span class="cc-small cc-dimtext">alphabetical · Alpaca values at the refresh time</span></h2></div>
      ${rows ? `<div class="sf-tablewrap"><table class="sf-table ppf-table"><thead><tr><th>Symbol</th><th>Shares</th><th>Avg entry</th><th>Market value</th><th>Unrealized P&amp;L</th><th>Local simulated shares</th><th>Difference status</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="cc-small cc-dimtext">No open positions in the Alpaca paper account.</p>`}</section>`;
  }
  function reconciliation(d) {
    const r = d.reconciliation;
    if (!r) return `<section class="cc-card apx-recon"><div class="cc-head"><h2>LOCAL SIMULATOR vs ALPACA PAPER</h2></div><p class="cc-small cc-dimtext">No comparison is available for this refresh.</p></section>`;
    const p = r.positions, a = r.account, l = r.links, s = r.summary;
    const sum = s ? `<div class="apx-sum cc-small" data-apx-summary>Local positions: <b>${esc(s.local_positions)}</b> · Alpaca positions: <b>${esc(s.alpaca_positions)}</b> · Quantity matches: <b>${esc(s.quantity_matches)}</b> · Quantity differences: <b>${esc(s.quantity_differences)}</b> · Local-only: <b>${esc(s.local_only)}</b> · Alpaca-only: <b>${esc(s.alpaca_only)}</b> · Linked orders: <b>0</b> · Linked fills: <b>0</b></div>` : "";
    const rows = p.available ? p.rows.map((x) => { const t = POS[x.status];
      return `<tr data-apx-rec="${esc(x.symbol)}" data-apx-status="${esc(x.status)}"><td class="ppf-sym">${esc(x.symbol)}</td><td>${esc(sh(x.local_shares))}</td><td>${esc(sh(x.alpaca_shares))}</td><td>${esc(x.share_delta === "0" ? "0" : sh(x.share_delta.startsWith("-") ? x.share_delta : `+${x.share_delta}`))}</td>
        <td>${tag(t[0], t[1])}</td><td>${esc(px(x.local_average_cost))}</td><td>${esc(px(x.alpaca_avg_entry_price))}</td>
        <td class="cc-small">${x.value_comparison ? `${tag("NOT DIRECTLY COMPARABLE", "dim")}<div class="cc-dimtext">local ${esc(usd(x.local_market_value))} · Alpaca ${esc(usd(x.alpaca_market_value))}</div>` : "—"}</td></tr>`; }).join("") : "";
    const loc = a.local, alp = a.alpaca;
    const bal = `<div class="apx-bal"><div><span class="cc-label">LOCAL SIMULATOR</span>${loc.exists ? `<div class="cc-small">Simulated cash <b>${esc(usd(loc.cash))}</b> · simulated equity <b>${esc(usd(loc.equity))}</b>${loc.mark_session ? ` <span class="cc-dimtext">(marked at the ${esc(day(loc.mark_session))} close)</span>` : ""}</div><div class="cc-small cc-dimtext">Starting virtual cash ${esc(usd(loc.starting_cash))}</div>` : `<div class="cc-small cc-dimtext">No local paper account.</div>`}</div>
      <div><span class="cc-label">ALPACA PAPER</span>${alp ? `<div class="cc-small">Paper cash <b>${esc(usd(alp.cash))}</b> · paper equity <b>${esc(usd(alp.equity))}</b> <span class="cc-dimtext">(at the refresh)</span></div>` : `<div class="cc-small cc-dimtext">Not available in this refresh.</div>`}</div></div>
      ${a.available && a.differences && a.differences.cash != null ? `<div class="cc-small cc-dimtext">Difference, descriptive only (Alpaca − local): cash ${esc(pnl(a.differences.cash))}${a.differences.equity != null ? ` · equity ${esc(pnl(a.differences.equity))}` : ""}</div>` : ""}`;
    return `<section class="cc-card apx-recon"><div class="cc-head"><h2>LOCAL SIMULATOR vs ALPACA PAPER <span class="cc-small cc-dimtext">observational · nothing is synced or linked</span></h2></div>
      ${sum}
      <h3 class="apx-h3">POSITIONS</h3>
      ${p.available ? (rows ? `<div class="sf-tablewrap"><table class="sf-table ppf-table"><thead><tr><th>Symbol</th><th>Local shares</th><th>Alpaca shares</th><th>Share delta</th><th>Status</th><th>Local average cost</th><th>Alpaca avg entry</th><th>Market value</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="cc-small cc-dimtext">Neither account has an open position.</p>`)
        + `<div class="cc-small cc-dimtext">${esc(p.delta_convention)} ${esc(p.cost_note)} ${esc(p.values.note)}${p.values.local_basis.session ? ` Local: the ${esc(day(p.values.local_basis.session))} close.` : ""} Alpaca: ${esc(whenNY(p.values.alpaca_basis.as_of))}.</div>`
        : `<p class="cc-small cc-dimtext">${esc(p.reason)}</p>`}
      <h3 class="apx-h3">BALANCES ${tag("INDEPENDENT", "dim")}</h3>${bal}<div class="cc-small cc-dimtext">${esc(a.note)}</div>
      <h3 class="apx-h3">ORDERS ${tag("NOT LINKED", "dim")} · FILLS ${tag("NOT LINKED", "dim")}</h3>
      <div class="cc-small">Local simulator: ${esc(l.orders.local_count)} orders, ${esc(l.fills.local_count)} fills · Alpaca paper (recent): ${l.orders.alpaca_count == null ? "orders not read" : `${esc(l.orders.alpaca_count)} orders`}, ${l.fills.alpaca_count == null ? "fills not read" : `${esc(l.fills.alpaca_count)} fills`} · linked: 0</div>
      <div class="cc-small cc-dimtext">${esc(l.note)}</div></section>`;
  }
  function orders(d) {
    if (d.sections && d.sections.orders.status !== "OK") return `<section class="cc-card apx-orders"><div class="cc-head"><h2>RECENT ALPACA PAPER ORDERS</h2></div><p class="cc-small cc-dimtext">Not available in this refresh.</p></section>`;
    const rows = d.orders.map((o) => `<tr><td class="cc-small">${esc(whenNY(o.submitted_at))}</td><td class="ppf-sym">${esc(o.symbol)}</td><td>${esc(String(o.side || "").toUpperCase())}</td>
        <td>${o.qty != null ? esc(o.qty) : o.notional != null ? `${esc(usd(o.notional))} notional` : "—"}</td><td>${esc(o.type || "—")}</td><td>${esc(o.time_in_force || "—")}</td><td>${tag(String(o.status || "—").replace(/_/g, " "), "dim")}</td>
        <td>${esc(o.filled_qty == null ? "—" : o.filled_qty)}</td><td>${esc(px(o.filled_avg_price))}</td><td class="cc-small">${esc(whenNY(o.filled_at))}</td>
        <td class="cc-small apx-ref" title="Alpaca order id (shortened)">${esc(o.id_short)}</td><td class="cc-small apx-ref" title="${esc(o.client_order_id)}">${esc(String(o.client_order_id || "—").slice(0, 12))}</td><td>${tag("NOT LINKED", "dim")}</td></tr>`).join("");
    return `<section class="cc-card apx-orders"><div class="cc-head"><h2>RECENT ALPACA PAPER ORDERS <span class="cc-small cc-dimtext">read only · newest first · up to ${esc(d.source.limits.orders)}</span></h2></div>
      ${rows ? `<div class="sf-tablewrap"><table class="sf-table ppf-table"><thead><tr><th>Submitted</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Type</th><th>TIF</th><th>Status</th><th>Filled qty</th><th>Avg fill price</th><th>Filled at</th><th>Order ref</th><th>Client order id</th><th>Link</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="cc-small cc-dimtext">No recent orders in the Alpaca paper account.</p>`}</section>`;
  }
  function fills(d) {
    if (d.sections && d.sections.fills.status !== "OK") return `<section class="cc-card apx-fills"><div class="cc-head"><h2>RECENT ALPACA PAPER FILLS</h2></div><p class="cc-small cc-dimtext">Not available in this refresh.</p></section>`;
    const rows = d.fills.map((f) => `<tr><td class="cc-small">${esc(whenNY(f.time))}</td><td class="ppf-sym">${esc(f.symbol)}</td><td>${esc(String(f.side || "").toUpperCase())}</td><td>${esc(f.qty)}</td>
        <td>${esc(px(f.price))}</td><td class="cc-small apx-ref">${esc(f.order_ref || "—")}</td><td>${tag("NOT LINKED", "dim")}</td></tr>`).join("");
    return `<section class="cc-card apx-fills"><div class="cc-head"><h2>RECENT ALPACA PAPER FILLS <span class="cc-small cc-dimtext">read only · newest first · up to ${esc(d.source.limits.fills)}</span></h2></div>
      ${rows ? `<div class="sf-tablewrap"><table class="sf-table ppf-table"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Order ref</th><th>Link</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="cc-small cc-dimtext">No recent fills in the Alpaca paper account.</p>`}</section>`;
  }
  function draw() {
    if (!el) return;
    const t0 = performance.now();
    const d = data;
    let body = "";
    if (d && !d.connection.configured) body = notConfigured(d);
    else if (d && d.refreshed_at) body = account(d) + positions(d) + reconciliation(d) + orders(d) + fills(d);
    el.innerHTML = `<div class="apx">${head(d)}${body}
      <p class="cc-small cc-dimtext ppf-foot">${esc(d ? d.note : "")} No AI. The local simulator and the Alpaca paper account stay independent; their totals are never combined.</p></div>`;
    renderMs = Math.round((performance.now() - t0) * 10) / 10;
  }
  function show(container) {
    if (el !== container) { el = container; el.addEventListener("click", onClick); }
    draw();
    load();                                                   // the last refresh from server memory — no broker call
  }

  window.AlpacaPaper = { show, get state() {
    const d = data;
    return { state: refreshing ? "REFRESHING" : d ? d.connection.state : null, refreshing, requests, notice, renderMs,
      configured: d ? d.connection.configured : null, refreshedAt: d ? d.refreshed_at : null,
      positions: d ? d.positions.length : 0, orders: d ? d.orders.length : 0, fills: d ? d.fills.length : 0,
      statuses: d && d.reconciliation && d.reconciliation.positions.available ? Object.fromEntries(d.reconciliation.positions.rows.map((r) => [r.symbol, r.status])) : {},
      summary: d && d.reconciliation ? d.reconciliation.summary : null, warnings: d ? d.warnings.map((w) => `${w.section}:${w.code}`) : [] }; } };
})();
