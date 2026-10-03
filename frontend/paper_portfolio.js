// paper_portfolio.js — Stage 4.5 PAPER PORTFOLIO (a Strategy Lab workspace). PAPER · SIMULATED · NO REAL ORDERS.
//
// A local simulation: virtual USD cash, whole shares, long only, market orders filled at the next session's open from
// daily bars (paper/). Every order is an explicit two-step user action (Review, then Create paper order); pending orders
// fill only when you press "Process pending paper orders". Nothing is sent to a broker, nothing is combined with a real
// account, no AI is used, and the browser never supplies a price, a session or a date. No timers, no polling.
// Stage 4.6A: a view switch — "Local Simulator" (this file, the default) | "Alpaca Paper — Read Only" (alpaca_paper.js,
// its own container; it reads nothing until selected). The two are never merged.
// Stage 4.6B: a third view "Alpaca Paper — Manual Orders" (alpaca_orders.js, its own container; it reads nothing until
// selected and sends an order only on an explicit Confirm click there).
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("ppf-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const day = (iso) => (iso ? new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }) : "—");
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  // display only — every amount arrives as an exact decimal string computed on the server
  const usd = (s) => (s == null ? "—" : `${String(s).startsWith("-") ? "−" : ""}$${Number(String(s).replace("-", "")).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
  const pnl = (s) => (s == null ? "—" : `${String(s).startsWith("-") ? "" : Number(s) > 0 ? "+" : ""}${usd(s)}`);
  const px = (s) => (s == null ? "—" : Number(s).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 }));
  const pnlKind = (s) => (s == null || Number(s) === 0 ? "dim" : Number(s) > 0 ? "ok" : "alert");
  const SYMBOL_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;
  const WAIT = { NOT_YET_AVAILABLE: "waiting for the fill session to complete", NEXT_OPEN_UNAVAILABLE: "no open price for the fill session yet (data wait)",
    DATA_WAIT_CALENDAR: "market calendar not confirmed yet (data wait)" };

  let data = null, busy = "", notice = "", seq = 0, requests = 0, shell = false, form = null, lastProcess = null, view = "local";
  const openLots = new Set();

  const api = (method, url, body) => {
    requests++;
    return fetch(url, method === "GET" ? {} : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }))
      .catch(() => ({ status: 0, body: { message: "The server did not answer. Nothing changed." } }));
  };

  async function load() {
    const my = ++seq;
    busy = busy || "load"; draw();
    const r = await api("GET", "/api/paper-portfolio");
    if (my !== seq) return;
    if (busy === "load") busy = "";
    if (r.status === 200) { data = r.body; notice = ""; } else notice = r.body.message || "The paper portfolio could not be read.";
    draw();
  }
  async function act(kind, url, body, after) {
    busy = kind; notice = ""; draw();
    const r = await api("POST", url, body);
    busy = "";
    seq++;                                                      // an action's answer replaces any older read
    if (r.status >= 200 && r.status < 300) { after(r.body); } else { notice = r.body.message || "The request could not be completed."; }
    draw();
    return r;
  }

  // ---- actions -------------------------------------------------------------------------------------------------------
  function readForm() {
    const q = (k) => root.querySelector(`[data-ppf-f="${k}"]`);
    if (!form || !q("symbol")) return;
    form.symbol = q("symbol").value.trim().toUpperCase();
    form.side = q("side").value;
    form.quantity = q("quantity").value.trim();
  }
  function orderBody() {
    return { symbol: form.symbol, side: form.side, quantity: Number(form.quantity), origin: "MANUAL" };
  }
  function validForm() {
    if (!SYMBOL_RE.test(form.symbol)) return "Enter a valid US stock ticker.";
    if (!/^[1-9][0-9]{0,6}$/.test(form.quantity) || Number(form.quantity) > 1000000) return "Shares must be a whole number from 1 to 1,000,000.";
    return "";
  }
  function setView(v) {
    view = v;
    root.querySelectorAll("[data-ppf-view]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.ppfView === v)));
    root.querySelector('[data-ppfp="body"]').hidden = v !== "local";
    const ax = root.querySelector('[data-ppfp="alpaca"]');
    ax.hidden = v !== "alpaca";
    if (v === "alpaca") window.AlpacaPaper.show(ax);
    const ox = root.querySelector('[data-ppfp="orders"]');
    ox.hidden = v !== "orders";
    if (v === "orders") window.AlpacaOrders.show(ox);
  }
  function onClick(e) {
    const b = e.target.closest("button");
    if (!b || b.disabled) return;
    if (b.dataset.ppfView) { setView(b.dataset.ppfView); return; }
    const a = b.dataset.ppf;
    if (a === "create-account") {
      const v = (k) => root.querySelector(`[data-ppf-a="${k}"]`).value.trim();
      if (!/^\d{4,9}(\.\d{1,2})?$/.test(v("cash"))) { notice = "Enter the starting virtual cash (at least 1,000, at most 100,000,000)."; draw(); return; }
      act("account", "/api/paper-portfolio/account", { name: v("name") || null, starting_cash: v("cash"), slippage_bps: v("slip") || "0",
        commission_per_order: v("comm") || "0" }, (x) => { data = x; });
    } else if (a === "new-order") { form = { symbol: "", side: "BUY", quantity: "", preview: null }; draw(); }
    else if (a === "sell") { form = { symbol: b.dataset.symbol, side: "SELL", quantity: "", preview: null, max: Number(b.dataset.max) }; draw(); }
    else if (a === "cancel-form") { form = null; draw(); }
    else if (a === "review") {
      readForm();
      const bad = validForm();
      if (bad) { notice = bad; draw(); return; }
      act("preview", "/api/paper-orders/preview", orderBody(), (x) => { form.preview = x.preview; });
    } else if (a === "edit") { readForm(); form.preview = null; draw(); }
    else if (a === "create-order") {
      act("order", "/api/paper-orders", orderBody(), (x) => { data = x.portfolio; form = null; notice = ""; });
    } else if (a === "process") {
      act("process", "/api/paper-orders/process", {}, (x) => { data = x.portfolio; lastProcess = x.process; });
    } else if (a === "cancel-order") {
      act("cancel", `/api/paper-orders/${encodeURIComponent(b.dataset.id)}/cancel`, {}, (x) => { data = x.portfolio; });
    } else if (a === "lots") { const s = b.dataset.symbol; (openLots.has(s) ? openLots.delete(s) : openLots.add(s)); draw(); }
    else if (a === "refresh") { lastProcess = null; load(); }
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------------
  const stat = (label, v, k) => `<div class="ppf-stat"><span class="cc-label">${esc(label)}</span><div class="ppf-num${k ? ` ppf-${esc(k)}` : ""}">${v}</div></div>`;
  function setup(d) {
    const s = d.setup;
    return `<section class="cc-card ppf-setup"><div class="cc-head"><h2>CREATE YOUR PAPER ACCOUNT</h2></div>
      <p class="cc-small">One local paper account with virtual US dollars. ${esc(s.starting_cash_note)}</p>
      <div class="ppf-fields">
        <label class="sf-field"><span class="cc-label">ACCOUNT NAME</span><input data-ppf-a="name" maxlength="60" placeholder="Paper account"></label>
        <label class="sf-field"><span class="cc-label">STARTING VIRTUAL CASH (USD)</span><input data-ppf-a="cash" inputmode="decimal" placeholder="required" aria-required="true"></label>
        <label class="sf-field"><span class="cc-label">SLIPPAGE (BPS PER SIDE)</span><input data-ppf-a="slip" inputmode="decimal" value="${esc(s.defaults.slippage_bps)}"></label>
        <label class="sf-field"><span class="cc-label">COMMISSION ($ PER ORDER)</span><input data-ppf-a="comm" inputmode="decimal" value="${esc(s.defaults.commission_per_order)}"></label></div>
      <p class="cc-small cc-dimtext">Slippage and commission default to the backtester's values (0). ${esc(s.zero_cost_text)} Starting cash, slippage and commission are fixed once the account has its first fill.</p>
      <button type="button" class="cc-btn cc-primary" data-ppf="create-account"${busy ? " disabled" : ""}>${busy === "account" ? "Creating…" : "Create paper account"}</button></section>`;
  }
  function orderForm(d) {
    if (!form) return "";
    const p = form.preview;
    const dis = busy ? " disabled" : "";
    if (p) {
      return `<section class="cc-card ppf-form"><div class="cc-head"><h2>CONFIRM PAPER ORDER <span class="cc-small cc-dimtext">simulated — no real order</span></h2></div>
        <div class="ppf-confirm"><b>${esc(p.symbol)} · ${esc(p.side)} · ${esc(p.quantity)} share${p.quantity === 1 ? "" : "s"}</b> · market order</div>
        <ul class="cc-small ppf-terms"><li>Timing: next session open — the first session opening after you create the order (${esc(day(p.earliest_fill_session))} or the next trading session).</li>
          <li>Estimated using the ${esc(day(p.decision_session))} close with your slippage: ${esc(px(p.estimate_price))}/share · ${p.side === "BUY" ? "estimated cost" : "estimated proceeds"} ${esc(usd(p.estimate_amount))}. The fill uses the next session's open.</li>
          <li>${p.side === "BUY" ? "At the fill, cash is checked again at the actual open plus costs (cash only — no margin)." : "Shares are checked again at the fill (long only — no short)."}</li></ul>
        <div class="ppf-actions"><button type="button" class="cc-btn cc-mini" data-ppf="edit"${dis}>Change</button>
          <button type="button" class="cc-btn cc-mini" data-ppf="cancel-form"${dis}>Cancel</button>
          <button type="button" class="cc-btn cc-mini cc-primary" data-ppf="create-order"${dis}>${busy === "order" ? "Creating…" : "Create paper order"}</button></div></section>`;
    }
    return `<section class="cc-card ppf-form"><div class="cc-head"><h2>NEW PAPER ORDER <span class="cc-small cc-dimtext">market order · next session open · whole shares</span></h2></div>
      <div class="ppf-fields">
        <label class="sf-field"><span class="cc-label">SYMBOL</span><input data-ppf-f="symbol" maxlength="10" value="${esc(form.symbol)}" autocomplete="off" spellcheck="false"${form.side === "SELL" && form.max ? " readonly" : ""}></label>
        <label class="sf-field"><span class="cc-label">SIDE</span><select data-ppf-f="side"><option value="BUY"${form.side === "BUY" ? " selected" : ""}>BUY</option><option value="SELL"${form.side === "SELL" ? " selected" : ""}>SELL</option></select></label>
        <label class="sf-field"><span class="cc-label">SHARES</span><input data-ppf-f="quantity" inputmode="numeric" value="${esc(form.quantity)}" placeholder="${form.max ? `1 – ${esc(form.max)}` : "whole shares"}"></label></div>
      <p class="cc-small cc-dimtext">You choose the symbol, side and shares — the app never sizes an order. Long only: a sell needs paper shares you hold.</p>
      <div class="ppf-actions"><button type="button" class="cc-btn cc-mini" data-ppf="cancel-form"${dis}>Cancel</button>
        <button type="button" class="cc-btn cc-mini cc-primary" data-ppf="review"${dis}>${busy === "preview" ? "Checking…" : "Review paper order"}</button></div></section>`;
  }
  function positions(d) {
    const rows = d.positions.map((p) => `<tr data-ppf-pos="${esc(p.symbol)}"><td class="ppf-sym">${esc(p.symbol)}</td><td>${esc(p.shares)}</td><td>${esc(px(p.average_cost))}</td>
        <td>${esc(px(p.mark))}</td><td>${esc(usd(p.market_value))}</td><td>${tag(pnl(p.unrealized_pnl), pnlKind(p.unrealized_pnl))}</td><td>${tag(pnl(p.realized_pnl), pnlKind(p.realized_pnl))}</td>
        <td class="ppf-act">${p.shares ? `<button type="button" class="cc-btn cc-mini" data-ppf="sell" data-symbol="${esc(p.symbol)}" data-max="${esc(p.shares)}"${busy ? " disabled" : ""}>Sell paper shares</button>` : ""}
          ${p.lots.length ? `<button type="button" class="cc-btn cc-mini cc-link" data-ppf="lots" data-symbol="${esc(p.symbol)}">${openLots.has(p.symbol) ? "Hide lots" : "FIFO lots"}</button>` : ""}</td></tr>
      ${openLots.has(p.symbol) ? `<tr class="ppf-lots"><td colspan="8"><div class="cc-small"><span class="cc-label">FIFO LOTS</span> first entry ${esc(day(p.first_entry_session))} · latest transaction ${esc(day(p.latest_transaction_session))}</div>
        <table class="sf-table ppf-table"><thead><tr><th>Entry session</th><th>Shares bought</th><th>Shares remaining</th><th>Cost / share</th><th>Remaining cost basis</th></tr></thead><tbody>
        ${p.lots.map((l) => `<tr><td>${esc(day(l.entry_session))}</td><td>${esc(l.quantity)}</td><td>${esc(l.remaining)}</td><td>${esc(px(l.cost_per_share))}</td><td>${esc(usd(l.remaining_basis))}</td></tr>`).join("")}</tbody></table></td></tr>` : ""}`).join("");
    return `<section class="cc-card ppf-positions"><div class="cc-head"><h2>PAPER POSITIONS <span class="cc-small cc-dimtext">alphabetical · ${esc(d.mark_text)}</span></h2></div>
      ${rows ? `<div class="sf-tablewrap"><table class="sf-table ppf-table"><thead><tr><th>Symbol</th><th>Shares</th><th>Average cost</th><th>Mark</th><th>Market value</th><th>Unrealized P&amp;L</th><th>Realized P&amp;L</th><th>Action</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="cc-small cc-dimtext">No paper positions yet.</p>`}
      <div class="cc-small cc-dimtext">${esc(d.fifo_text)} Average cost and cost basis include slippage and commission.</div></section>`;
  }
  function pending(d) {
    const rows = d.pending_orders.map((o) => `<tr data-ppf-order="${esc(o.order_id)}"><td class="ppf-sym">${esc(o.symbol)}</td><td>${tag(o.side, o.side === "BUY" ? "info" : "dim")}</td><td>${esc(o.quantity)}</td>
        <td class="cc-small">${esc(whenNY(o.submitted_at))}<div class="cc-dimtext">decision: ${esc(day(o.decision_session))} close</div></td>
        <td class="cc-small">next session open (${esc(day(o.earliest_fill_session))} or later)</td>
        <td>${tag("PENDING", "warn")}${o.wait_reason ? ` <span class="cc-small cc-dimtext">${esc(WAIT[o.wait_reason] || o.wait_reason)}</span>` : ""}</td>
        <td class="ppf-act"><button type="button" class="cc-btn cc-mini" data-ppf="cancel-order" data-id="${esc(o.order_id)}"${busy ? " disabled" : ""}>Cancel</button></td></tr>`).join("");
    const closed = d.closed_orders.length ? `<details class="ppf-more"><summary class="cc-small">Cancelled / rejected paper orders (${esc(d.closed_orders.length)})</summary><ul class="cc-small ppf-list">${
      d.closed_orders.map((o) => `<li>${tag(o.status, o.status === "REJECTED" ? "alert" : "dim")} ${esc(o.symbol)} ${esc(o.side)} ${esc(o.quantity)} · ${esc(whenNY(o.submitted_at))}${o.reject_reason ? ` — ${esc(o.reject_reason)}` : ""}</li>`).join("")}</ul></details>` : "";
    return `<section class="cc-card ppf-pending"><div class="cc-head"><h2>PENDING PAPER ORDERS</h2></div>
      ${rows ? `<div class="sf-tablewrap"><table class="sf-table ppf-table"><thead><tr><th>Symbol</th><th>Side</th><th>Shares</th><th>Submitted</th><th>Target</th><th>Status</th><th>Action</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="cc-small cc-dimtext">No pending paper orders.</p>`}${closed}</section>`;
  }
  function history(d) {
    const rows = d.fills.map((f) => `<tr><td>${esc(day(f.fill_session))}</td><td class="ppf-sym">${esc(f.symbol)}</td><td>${tag(f.side, f.side === "BUY" ? "info" : "dim")}</td><td>${esc(f.quantity)}</td>
        <td>${esc(px(f.base_price))}</td><td>${esc(f.slippage_bps)} bps</td><td>${esc(px(f.effective_price))}</td><td>${esc(usd(f.commission))}</td>
        <td>${f.realized_pnl != null ? tag(pnl(f.realized_pnl), pnlKind(f.realized_pnl)) : "—"}</td><td class="cc-small">manual</td></tr>`).join("");
    return `<section class="cc-card ppf-history"><div class="cc-head"><h2>PAPER TRADE HISTORY <span class="cc-small cc-dimtext">immutable simulated fills · newest first</span></h2></div>
      ${rows ? `<div class="sf-tablewrap"><table class="sf-table ppf-table"><thead><tr><th>Session</th><th>Symbol</th><th>Side</th><th>Shares</th><th>Open (base)</th><th>Slippage</th><th>Fill</th><th>Commission</th><th>Realized P&amp;L</th><th>Origin</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="cc-small cc-dimtext">No paper fills yet.</p>`}
      <div class="cc-small cc-dimtext">Each fill is the stock's daily open of its fill session (adjusted daily bars) with your slippage and commission; fills are never edited.</div></section>`;
  }
  function processLine() {
    if (!lastProcess) return "";
    const p = lastProcess;
    return `<div class="ppf-process cc-small"><b>Processed ${esc(p.orders_considered)} pending order${p.orders_considered === 1 ? "" : "s"}:</b> ${esc(p.filled)} filled · ${esc(p.still_pending)} still pending · ${esc(p.rejected)} rejected${p.errors ? ` · ${esc(p.errors)} error(s)` : ""}${
      p.orders.filter((o) => o.status === "REJECTED").map((o) => `<div class="cc-dimtext">${esc(o.symbol)}: ${esc(o.text || o.reason)}</div>`).join("")}</div>`;
  }
  function draw() {
    if (!shell) {
      shell = true;
      const views = window.AlpacaPaper ? `<div class="ppf-views" role="group" aria-label="Paper portfolio views">
        <button type="button" class="cc-btn cc-mini" data-ppf-view="local" aria-pressed="true">Local Simulator</button>
        <button type="button" class="cc-btn cc-mini" data-ppf-view="alpaca" aria-pressed="false">Alpaca Paper — Read Only</button>${
          window.AlpacaOrders ? `<button type="button" class="cc-btn cc-mini" data-ppf-view="orders" aria-pressed="false">Alpaca Paper — Manual Orders</button>` : ""}</div>` : "";
      root.innerHTML = `<div class="ppf">${views}<div data-ppfp="body"></div><div data-ppfp="alpaca" hidden></div><div data-ppfp="orders" hidden></div></div>`;
      root.addEventListener("click", onClick);
    }
    const el = root.querySelector('[data-ppfp="body"]');
    const d = data;
    const strip = `<span class="ppf-strip">PAPER · SIMULATED · NO REAL ORDERS</span>`;
    if (!d) { el.innerHTML = `<section class="cc-card"><div class="cc-head"><h2>PAPER PORTFOLIO</h2>${strip}</div><p class="cc-small cc-dimtext">${esc(notice || "Loading the paper portfolio…")}</p></section>`; return; }
    const note = notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : "";
    if (!d.account) { el.innerHTML = `<section class="cc-card ppf-head"><div class="cc-head"><h2>PAPER PORTFOLIO <span class="cc-small cc-dimtext">local simulation</span></h2>${strip}</div><p class="cc-small">${esc(d.note)}</p>${note}</section>${setup(d)}`; return; }
    const a = d.account, s = d.summary, dis = busy ? " disabled" : "";
    el.innerHTML = `<section class="cc-card ppf-head">
        <div class="cc-head"><h2>PAPER PORTFOLIO <span class="cc-small cc-dimtext">${esc(a.name)} · virtual ${esc(a.base_currency)}</span></h2>${strip}</div>
        <div class="ppf-stats">${stat("PAPER EQUITY", esc(usd(s.equity)))}${stat("CASH", esc(usd(s.cash)))}${stat("MARKET VALUE", esc(usd(s.market_value)))}
          ${stat("REALIZED P&L", esc(pnl(s.realized_pnl)), pnlKind(s.realized_pnl))}${stat("UNREALIZED P&L", esc(pnl(s.unrealized_pnl)), pnlKind(s.unrealized_pnl))}</div>
        <div class="cc-small">${s.mark_session ? `Marked at the ${esc(day(s.mark_session))} close.` : s.open_positions ? `<b>Positions are unmarked:</b> ${esc(s.mark_note || "no completed close is available")}.` : "No open positions to mark."}
          ${s.unmarked_symbols.length ? ` <b>No close for ${esc(s.unmarked_symbols.join(", "))}</b> — market value and equity are incomplete (never guessed).` : ""}
          <span class="cc-dimtext">Starting virtual cash ${esc(usd(a.starting_cash))} · slippage ${esc(a.slippage_bps)} bps per side · commission ${esc(usd(a.commission_per_order))} per order${a.zero_cost ? " (an optimistic, cost-free assumption)" : ""}. Not your real net worth.</span></div>
        <div class="ppf-actions"><button type="button" class="cc-btn cc-mini cc-primary" data-ppf="new-order"${dis || (form ? " disabled" : "")}>New paper order</button>
          <button type="button" class="cc-btn cc-mini" data-ppf="process"${dis}>${busy === "process" ? "Processing…" : "Process pending paper orders"}</button>
          <button type="button" class="cc-btn cc-mini cc-link" data-ppf="refresh"${dis}>Refresh</button>
          <span class="cc-small cc-dimtext">Pending orders fill only when you process them, once their fill session has completed.</span></div>
        ${processLine()}${note}</section>
      ${orderForm(d)}${positions(d)}${pending(d)}${history(d)}
      <p class="cc-small cc-dimtext ppf-foot">${esc(d.note)} No AI, no broker, no automatic orders — a RULES MET result, a forward-journal decision or a strategy alert never creates a paper order.</p>`;
  }
  function show() {
    draw(); load();
    if (view === "alpaca") window.AlpacaPaper.show(root.querySelector('[data-ppfp="alpaca"]'));
    if (view === "orders") window.AlpacaOrders.show(root.querySelector('[data-ppfp="orders"]'));
  }

  window.StrategyFit.addWorkspace({ id: "paper", label: "Paper Portfolio", cls: "ppf-mode", show });
  window.PaperPortfolio = { get state() { return { account: data && data.account ? data.account.account_id : null, busy, notice, requests, view,
    positions: data && data.positions ? data.positions.length : 0, pending: data && data.pending_orders ? data.pending_orders.length : 0,
    fills: data && data.fills ? data.fills.length : 0, form: form ? (form.preview ? "confirm" : "edit") : null,
    equity: data && data.summary ? data.summary.equity : null, cash: data && data.summary ? data.summary.cash : null,
    lastProcess: lastProcess && { filled: lastProcess.filled, rejected: lastProcess.rejected, pending: lastProcess.still_pending } }; } };
})();
