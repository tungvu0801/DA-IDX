// alpaca_orders.js — Stage 4.6B "Alpaca Paper — Manual Orders" (the third view of the Paper Portfolio workspace).
//
// PAPER ACCOUNT only. You preview a MARKET / DAY order for whole shares; the server validates it with fixed rules and
// stores an immutable preview. Only a click on "Confirm Paper Order: …" can send it — one order to your Alpaca PAPER
// account. There is no form element (Enter never confirms), no timer, no polling, no AI, and no cancel / replace / close
// control. The browser never supplies a price, an order type, a client order id or an account.
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  const hmNY = (v) => (v ? `${new Date(v).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  const day = (iso) => (iso ? new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }) : "—");
  const usd = (s) => (s == null ? "—" : `$${Number(s).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
  const px = (s) => (s == null ? "—" : Number(s).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 }));
  const H = { "Content-Type": "application/json", "X-Stock-Agent-Intent": "paper-order" };
  const BASE = "/api/alpaca-paper-orders";
  const BANNER = "PAPER ACCOUNT — simulated trading only · orders go to your Alpaca PAPER account";
  const GUARD = "Stock Agent cash guard: previous close + 5% must fit in your Alpaca paper cash — a conservative estimate, not the fill price; Alpaca decides buying power";
  const CLOSED = "Market closed — confirmation is available during regular hours";
  const NEAR = "Confirmation is disabled within 5 minutes of the market close";
  const NO_SHORT = "turn it on in your Alpaca paper dashboard; Stock Agent never changes it";
  const KIND = { PREVIEWED: "info", SUBMITTED: "info", BROKER_ACCEPTED: "info", PARTIALLY_FILLED: "warn", FILLED: "ok", CANCELED: "dim",
    BROKER_REJECTED: "alert", CONFIRM_REJECTED: "dim", SUBMIT_NOT_SENT: "warn", RECONCILIATION_REQUIRED: "alert", SUBMISSION_PENDING: "warn",
    CONFIRMING: "warn", ABANDONED: "dim", EXPIRED: "dim", SUPERSEDED: "dim" };

  let el = null, settings = null, intents = [], current = null, busy = "", notice = "", seq = 0, requests = 0, relinkOpen = false, lastAction = null;
  let form = { symbol: "", side: "BUY", quantity: "" };

  const api = (method, url, body) => {
    requests++;
    return fetch(url, method === "GET" ? {} : { method, headers: H, body: JSON.stringify(body || {}) })
      .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }))
      .catch(() => ({ status: 0, body: { message: "The server did not answer. Nothing was sent." } }));
  };

  async function load() {
    const my = ++seq;
    const s = await api("GET", `${BASE}/settings`);
    const l = await api("GET", BASE);
    if (my !== seq) return;
    if (s.status === 200) settings = s.body;
    if (l.status === 200) intents = l.body.intents || [];
    if (current) current = intents.find((i) => i.intent_id === current.intent_id) || current;
    draw();
  }
  async function act(kind, url, body, after) {
    if (busy) return null;                                     // one action at a time: a double click sends one request
    busy = kind; notice = ""; draw();
    const r = await api("POST", url, body);
    busy = ""; seq++;
    lastAction = { kind, status: r.status, code: r.body.status || null };
    if (r.status === 200) after(r.body);
    else {
      notice = r.body.message || "The request could not be completed. Nothing was sent.";
      if (r.body.intent) current = r.body.intent;
    }
    await load();
    return r;
  }
  function readForm() {
    const q = (k) => el.querySelector(`[data-apo-f="${k}"]`);
    if (!q("symbol")) return;
    form = { symbol: q("symbol").value.trim().toUpperCase(), side: q("side").value, quantity: q("quantity").value.trim() };
  }
  function onClick(e) {
    const b = e.target.closest("button[data-apo]");
    if (!b || b.disabled || busy) return;
    const a = b.dataset.apo;
    if (a === "link") act("link", `${BASE}/settings/link-account`, { confirm: true }, (x) => { settings = x; });
    else if (a === "relink-open") { relinkOpen = true; draw(); }
    else if (a === "relink-cancel") { relinkOpen = false; draw(); }
    else if (a === "relink") act("relink", `${BASE}/settings/relink-account`, { confirm: true, current_account_masked: settings.account_masked }, (x) => { settings = x; relinkOpen = false; current = null; });
    else if (a === "enable") act("enable", `${BASE}/settings/enable`, { confirm: true }, (x) => { settings = x; });
    else if (a === "disable") act("disable", `${BASE}/settings/disable`, {}, (x) => { settings = x; current = null; });
    else if (a === "preview") {
      readForm();
      if (!/^[A-Z]{1,5}(\.[A-Z]{1,2})?$/.test(form.symbol)) { notice = "Enter a valid US stock ticker."; draw(); return; }
      if (!/^[1-9][0-9]{0,4}$/.test(form.quantity) || Number(form.quantity) > 10000) { notice = "Shares must be a whole number from 1 to 10,000."; draw(); return; }
      act("preview", `${BASE}/preview`, { symbol: form.symbol, side: form.side, quantity: Number(form.quantity) }, (x) => { current = x.intent; });
    } else if (a === "discard") { current = null; notice = ""; draw(); }
    else if (a === "confirm") {
      if (!current || !confirmReady(current)) return;
      if (Date.now() >= Date.parse(current.expires_at)) { notice = "This preview expired (120 s) — preview again."; draw(); return; }
      act("confirm", `${BASE}/confirm`, { preview_id: current.intent_id, preview_hash: current.preview_hash, confirm: true }, (x) => { current = x.intent; });
    } else if (a === "status") act("status", `${BASE}/status`, {}, () => {});
    else if (a === "retry" || a === "abandon") {
      const it = intents.find((i) => String(i.intent_id) === b.dataset.id) || current;
      act(a, `${BASE}/${encodeURIComponent(b.dataset.id)}/${a}`, { preview_hash: it.preview_hash, confirm: true }, (x) => { current = x.intent; });
    }
  }
  function confirmReady(i) {
    return i.state === "PREVIEWED" && i.confirmable && i.market && i.market.is_open && !i.market.near_close;
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------------
  function strip(s) {
    if (!s) return "";
    if (!s.configured) {
      return `<section class="cc-card apo-settings"><div class="cc-small"><b>Alpaca Paper is not configured.</b> Add ${esc(s.credential_names[0])} and ${esc(s.credential_names[1])} to the local .env file and restart the app.</div></section>`;
    }
    const d = busy ? " disabled" : "";
    const ns = s.last_no_shorting === true ? `${tag("ON ✓", "ok")}` : s.last_no_shorting === false ? `${tag("OFF", "alert")} <span class="cc-dimtext">${esc(NO_SHORT)}</span>` : `<span class="cc-dimtext">not checked yet</span>`;
    const link = !s.linked ? `<button type="button" class="cc-btn cc-mini cc-primary" data-apo="link"${d}>Link this paper account</button>`
      : relinkOpen ? `<span class="cc-small">Replace the linked account ${esc(s.account_masked)} with the account now configured in .env?</span>
          <button type="button" class="cc-btn cc-mini" data-apo="relink"${d}>Relink</button><button type="button" class="cc-btn cc-mini cc-link" data-apo="relink-cancel"${d}>Keep ${esc(s.account_masked)}</button>`
        : `<button type="button" class="cc-btn cc-mini cc-link" data-apo="relink-open"${d}>Relink…</button>`;
    return `<section class="cc-card apo-settings"><div class="apo-row cc-small">
        <span><span class="cc-label">MANUAL PAPER ORDERS</span> ${s.enabled ? tag("ON", "ok") : tag("OFF", "dim")}
          ${s.linked ? (s.enabled ? `<button type="button" class="cc-btn cc-mini" data-apo="disable"${d}>Turn off</button>` : `<button type="button" class="cc-btn cc-mini" data-apo="enable"${d}>Turn on</button>`) : ""}</span>
        <span><span class="cc-label">LINKED ACCOUNT</span> ${s.linked ? `<b>${esc(s.account_masked)}</b>` : "none"} ${link}</span>
        <span><span class="cc-label">ALPACA LONG-ONLY (NO SHORTING)</span> ${ns}</span></div>
      <div class="cc-small cc-dimtext">Off by default. Linking and turning on only read your paper account; they never change anything at Alpaca.</div></section>`;
  }
  function orderForm(s) {
    if (!s || !s.configured || !s.linked || !s.enabled) {
      return `<section class="cc-card apo-form"><p class="cc-small cc-dimtext">${!s || !s.configured ? "Configure Alpaca Paper first." : !s.linked ? "Link your Alpaca paper account to use manual paper orders." : "Manual paper orders are OFF. Turn them on to preview an order."}</p></section>`;
    }
    const d = busy ? " disabled" : "";
    return `<section class="cc-card apo-form"><div class="cc-head"><h2>NEW MANUAL PAPER ORDER <span class="cc-small cc-dimtext">market order · DAY · whole shares · long only</span></h2></div>
      <div class="ppf-fields">
        <label class="sf-field"><span class="cc-label">SYMBOL</span><input data-apo-f="symbol" maxlength="8" value="${esc(form.symbol)}" autocomplete="off" spellcheck="false"></label>
        <label class="sf-field"><span class="cc-label">SIDE</span><select data-apo-f="side"><option value="BUY"${form.side === "BUY" ? " selected" : ""}>BUY</option><option value="SELL"${form.side === "SELL" ? " selected" : ""}>SELL</option></select></label>
        <label class="sf-field"><span class="cc-label">SHARES</span><input data-apo-f="quantity" inputmode="numeric" value="${esc(form.quantity)}" placeholder="1 – 10,000"></label>
        <button type="button" class="cc-btn cc-mini" data-apo="preview"${d}>${busy === "preview" ? "Checking…" : "Preview Paper Order"}</button></div>
      <div class="cc-small cc-dimtext">A preview never sends anything. You choose the symbol, side and shares — Stock Agent never sizes an order.</div></section>`;
  }
  function rules(i) {
    return `<ul class="apo-rules cc-small">${(i.rules || []).map((r) => `<li data-apo-rule="${esc(r.rule)}">${r.ok ? tag("✓", "ok") : tag(r.info ? "info" : "✗", r.info ? "dim" : "alert")} <b>${esc(r.rule)}</b> ${r.ok ? "" : esc(r.text)}</li>`).join("")}</ul>`;
  }
  function previewCard(i) {
    if (!i) return "";
    const m = i.market || {};
    const ready = confirmReady(i);
    const why = i.state !== "PREVIEWED" ? "" : !i.confirmable ? "This preview did not pass every rule (see ✗ below), so it cannot be confirmed."
      : !m.is_open ? CLOSED : m.near_close ? NEAR : "";
    const label = `Confirm Paper Order: ${i.side} ${i.qty} ${i.symbol}`;
    const ref = i.reference || {};
    const money = i.side === "BUY" ? `<div><span class="cc-label">CASH GUARD</span> ${esc(GUARD)} · needs <b>${esc(usd(i.cash_guard && i.cash_guard.required))}</b> · paper cash ${esc(usd(i.cash_guard && i.cash_guard.cash))}</div>`
      : `<div><span class="cc-label">POSITION</span> Alpaca paper ${esc(i.sell && i.sell.alpaca_qty != null ? i.sell.alpaca_qty : "none")} shares · available to sell ${esc(i.sell && i.sell.qty_available != null ? i.sell.qty_available : "0")} · local simulator ${esc(i.sell && i.sell.local_simulator_shares != null ? i.sell.local_simulator_shares : "—")} (information only)</div>`;
    return `<section class="cc-card apo-preview" data-apo-preview="${esc(i.intent_id)}" data-apo-state="${esc(i.state)}">
      <div class="cc-head"><h2>${i.state === "PREVIEWED" ? "REVIEW PAPER ORDER" : "PAPER ORDER"} <span class="cc-small cc-dimtext">${esc(BANNER)} ${esc(i.account_masked)}</span></h2>${tag(i.state_label, KIND[i.state] || "dim")}</div>
      <div class="apo-order"><b>${esc(i.side)} ${esc(i.qty)} ${esc(i.symbol)}</b> · ${esc(i.order_type)} · ${esc(i.time_in_force)}</div>
      <div class="apo-facts cc-small">
        <div><span class="cc-label">REFERENCE</span> ${ref.price ? `${esc(px(ref.price))} — ${esc(ref.note)} <span class="cc-dimtext">(${esc(ref.source || "")})</span>` : `unavailable — ${esc(ref.reason || "no previous close")}`}</div>
        <div><span class="cc-label">ESTIMATED NOTIONAL</span> ${esc(usd(i.estimated_notional))} <span class="cc-dimtext">estimate only</span></div>
        ${money}
        <div><span class="cc-label">MARKET</span> ${m.is_open ? `OPEN · confirm available until ${esc(hmNY(m.confirm_available_until))}` : `CLOSED · next open ${esc(whenNY(m.next_open))}`}</div>
        <div><span class="cc-label">CLIENT ORDER ID</span> <code>${esc(i.client_order_id)}</code>${i.state === "PREVIEWED" ? ` · expires ${esc(whenNY(i.expires_at))}` : ""}</div>
        ${i.alpaca_order_ref ? `<div><span class="cc-label">ALPACA ORDER</span> <code>${esc(i.alpaca_order_ref)}</code> · ${esc(i.broker_status || "")}${i.filled_qty && i.filled_qty !== "0" ? ` · filled ${esc(i.filled_qty)} @ ${esc(px(i.filled_avg_price))}` : ""}</div>` : ""}
        ${i.error_text ? `<div class="apo-err">${esc(i.error_text)}</div>` : ""}</div>
      ${i.state === "PREVIEWED" ? rules(i) : ""}
      <div class="ppf-actions">
        ${i.state === "PREVIEWED" ? `<button type="button" class="cc-btn cc-mini cc-primary apo-confirm" data-apo="confirm"${ready && !busy ? "" : " disabled"}>${busy === "confirm" ? "Sending…" : esc(label)}</button>
          <button type="button" class="cc-btn cc-mini cc-link" data-apo="discard"${busy ? " disabled" : ""}>Discard preview</button>
          ${why ? `<span class="cc-small apo-why">${esc(why)}</span>` : ""}` : actions(i)}</div></section>`;
  }
  function actions(i) {
    const d = busy ? " disabled" : "";
    return `<button type="button" class="cc-btn cc-mini" data-apo="status"${d}>Check order status</button>
      ${i.can_retry ? `<button type="button" class="cc-btn cc-mini" data-apo="retry" data-id="${esc(i.intent_id)}"${d}>Retry submission (same client order id)</button>` : ""}
      ${i.can_abandon ? `<button type="button" class="cc-btn cc-mini cc-link" data-apo="abandon" data-id="${esc(i.intent_id)}"${d}>Abandon</button>` : ""}`;
  }
  function table() {
    const rows = intents.filter((i) => i.state !== "PREVIEWED").map((i) => `<tr data-apo-row="${esc(i.intent_id)}" data-apo-state="${esc(i.state)}">
        <td class="cc-small">${esc(whenNY(i.confirmed_at || i.previewed_at))}</td><td class="ppf-sym">${esc(i.symbol)}</td><td>${esc(i.side)}</td><td>${esc(i.qty)}</td>
        <td>${tag(i.state_label, KIND[i.state] || "dim")}</td><td class="cc-small">${esc(i.broker_status || "—")}</td>
        <td class="cc-small">${i.filled_qty && i.filled_qty !== "0" ? `${esc(i.filled_qty)} @ ${esc(px(i.filled_avg_price))}` : "—"}</td>
        <td class="cc-small apx-ref" title="${esc(i.client_order_id)}">${esc(i.client_order_id.slice(0, 14))}…</td><td class="cc-small apx-ref">${esc(i.alpaca_order_ref || "—")}</td>
        <td class="ppf-act">${i.can_retry ? `<button type="button" class="cc-btn cc-mini" data-apo="retry" data-id="${esc(i.intent_id)}"${busy ? " disabled" : ""}>Retry submission (same client order id)</button>` : ""}${i.can_abandon ? `<button type="button" class="cc-btn cc-mini cc-link" data-apo="abandon" data-id="${esc(i.intent_id)}"${busy ? " disabled" : ""}>Abandon</button>` : ""}</td></tr>`).join("");
    return `<section class="cc-card apo-orders"><div class="cc-head"><h2>MANUAL PAPER ORDERS <span class="cc-small cc-dimtext">newest first · exact Alpaca order links only</span></h2>
        <button type="button" class="cc-btn cc-mini" data-apo="status"${busy ? " disabled" : ""}>${busy === "status" ? "Checking…" : "Check order status"}</button></div>
      ${rows ? `<div class="sf-tablewrap"><table class="sf-table ppf-table"><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Shares</th><th>State</th><th>Alpaca status</th><th>Filled</th><th>Client order id</th><th>Alpaca order</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="cc-small cc-dimtext">No manual paper orders yet.</p>`}</section>`;
  }
  function draw() {
    if (!el) return;
    const s = settings;
    const acct = s && s.linked ? s.account_masked : "(not linked yet)";
    el.innerHTML = `<div class="apo">
      <div class="apo-banner" role="note">${esc(BANNER)} ${esc(acct)}</div>
      ${strip(s)}${notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}
      ${orderForm(s)}${previewCard(current)}${table()}
      <p class="cc-small cc-dimtext ppf-foot">${esc(s ? s.note : "")} The reference price is the previous close — not a quote, not the fill price. Orders are linked to Alpaca only by their exact ids.</p></div>`;
  }
  function show(container) {
    if (el !== container) { el = container; el.addEventListener("click", onClick); }
    draw();
    load();                                                   // settings + the stored orders: 0 broker requests
  }

  window.AlpacaOrders = { show, get state() {
    return { busy, notice, requests, linked: settings ? settings.linked : null, enabled: settings ? settings.enabled : null,
      configured: settings ? settings.configured : null, current: current ? { id: current.intent_id, state: current.state, confirmable: current.confirmable,
        ready: confirmReady(current), error: current.error_code, coid: current.client_order_id } : null,
      intents: intents.map((i) => [i.symbol, i.state]), lastAction }; } };
})();
