// brief_delivery.js — Stage 4.3 DESKTOP DELIVERY section of the Daily Brief (opt-in local Windows notifications).
//
// OFF by default. When on, the server's after-close check shows ONE Windows notification per new stored completed
// session with that session's Daily Brief counts. This view only shows the setting and the stored delivery history and
// sends explicit actions (enable / disable, the activity option, a fixed server-side test notification). It never sends
// notification text, never delivers a brief itself, and has no timers or polling.
// Stage 4.4: clicking a notification opens the local Daily Brief (http://127.0.0.1:8000/#daily-brief) — this file turns
// that fixed fragment into the existing navigation (Strategy Lab tab + the Daily Brief workspace); the card shows the
// click support and the notification identity ("Stock Agent" when registered, otherwise "Python · branding deferred").
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const day = (iso) => (iso ? new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", timeZone: "UTC" }) : "");
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "");
  const STATUS = { DELIVERED: ["Daily Brief delivered", "ok"], FAILED: ["Failed", "alert"], UNSUPPORTED_PLATFORM: ["Unsupported platform", "warn"],
    SKIPPED_NO_ACTIVITY: ["Skipped — no new stored activity", "dim"], BASELINE: ["Baseline — notifications start after this session", "dim"],
    PENDING: ["Interrupted — never re-sent", "warn"] };

  let el = null, status = null, busy = "", notice = "", requests = 0, lastTest = null;

  const api = (method, url, body) => {
    requests++;
    return fetch(url, method === "GET" ? {} : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }))
      .catch(() => ({ status: 0, body: { message: "The server did not answer. Nothing changed." } }));
  };
  const label = (h) => {
    const s = STATUS[h.status] || [String(h.status).replace(/_/g, " "), "dim"];
    return h.kind === "TEST" ? [`Test notification · ${h.status === "DELIVERED" ? "sent" : s[0].toLowerCase()}`, h.status === "DELIVERED" ? "info" : s[1]] : s;
  };
  function row(h) {
    const [t, k] = label(h);
    return `<li class="cc-small" data-bdl-row="${esc(h.status)}"><b>${esc(h.kind === "TEST" ? whenNY(h.created_at) : `${day(h.brief_session)} close`)}</b> ${tag(t, k)}${
      h.error_code ? ` <span class="cc-dimtext">${esc(h.error_code)}</span>` : ""}${h.kind === "SESSION" && h.status !== "BASELINE" ? ` <span class="cc-dimtext">${esc(whenNY(h.created_at))}</span>` : ""}</li>`;
  }
  function identity(s) {
    const i = s.identity;
    if (!i) return "";
    const click = i.click_supported ? `${tag("✓", "ok")} Clicking a notification opens this Daily Brief <span class="cc-dimtext">(${esc(i.activation_url)})</span>`
      : `<span class="cc-dimtext">${i.platform === "Windows" ? "Click-to-open is unavailable: the server address is not a local loopback address." : "Click-to-open needs Windows."}</span>`;
    const who = i.branding_active ? `<b>${esc(i.display_name)}</b> <span class="cc-dimtext">(${esc(i.app_id)}, registered for this Windows user)</span>`
      : i.platform === "Windows" ? `<b>Python</b> · <span class="cc-dimtext">branding deferred${s.enabled ? " (the Stock Agent identity could not be registered)" : " — turning notifications on registers the Stock Agent identity"}</span>`
        : `<span class="cc-dimtext">not available on ${esc(i.platform)}</span>`;
    return `<div class="bdl-ident cc-small"><div><span class="cc-label">OPEN DAILY BRIEF ON CLICK</span> ${click}</div>
      <div><span class="cc-label">NOTIFICATION IDENTITY</span> ${who}</div></div>`;
  }
  function draw() {
    if (!el) return;
    if (!status) { el.innerHTML = `<section class="cc-card bdl"><div class="cc-small cc-dimtext">${esc(notice || "Loading desktop delivery…")}</div></section>`; return; }
    const s = status, on = s.enabled, last = s.last_delivery;
    const d = busy ? " disabled" : "";
    el.innerHTML = `<section class="cc-card bdl${on ? " bdl-on" : ""}">
      <div class="cc-head"><h2>DESKTOP DELIVERY <span class="cc-small cc-dimtext">Windows notifications on this computer · opt-in</span></h2>
        <span class="bdl-state">Desktop notifications: ${tag(on ? "ON" : "OFF", on ? "ok" : "dim")}</span></div>
      ${on ? `<div class="cc-small">Next: <b>${esc(s.next)}</b> One notification with that session's Daily Brief counts${s.notify_only_if_activity ? " (only when it has stored activity)" : ""}.</div>`
        : `<div class="cc-small">Off. When on, this computer shows one notification after each new stored completed session, with the counts of that session's Daily Brief. Existing briefs are never sent.</div>`}
      ${s.platform_supported ? "" : `<div class="cc-banner cc-warn cc-small">This computer is not running Windows: deliveries are recorded as UNSUPPORTED PLATFORM.</div>`}
      <div class="bdl-actions">
        ${on ? `<button type="button" class="cc-btn cc-mini" data-bdl="test"${d}>${busy === "test" ? "Sending…" : "Send test notification"}</button>
          <label class="cc-small bdl-only"><input type="checkbox" data-bdl="only"${s.notify_only_if_activity ? " checked" : ""}${d}> Only when the brief has stored activity</label>
          <button type="button" class="cc-btn cc-mini cc-link" data-bdl="off"${d}>Disable</button>`
        : `<button type="button" class="cc-btn cc-mini cc-primary" data-bdl="on"${d}>${busy === "on" ? "Enabling…" : "Enable desktop notifications"}</button>`}</div>
      ${identity(s)}
      ${lastTest ? `<div class="cc-small bdl-test">${lastTest.result === "DELIVERED" ? "Test notification sent — check the Windows notification area." : `Test notification: ${esc(String(lastTest.result).replace(/_/g, " ").toLowerCase())}${lastTest.error_code ? ` (${esc(lastTest.error_code)})` : ""}.`}</div>` : ""}
      <div class="cc-small">Last delivery: ${last && last.status !== "BASELINE" ? `<b>${esc(day(last.brief_session))} close</b> · ${tag(label(last)[0], label(last)[1])}${last.error_code ? ` <span class="cc-dimtext">${esc(last.error_code)}</span>` : ""}`
        : `<span class="cc-dimtext">none yet${last ? ` (baseline: ${esc(day(last.brief_session))} close — notifications start after it)` : ""}</span>`}</div>
      ${s.history.length ? `<details class="bdl-hist"${s.history.length <= 6 ? " open" : ""}><summary class="cc-small">Recent deliveries (${esc(s.history.length)})</summary><ul class="bdl-list">${s.history.map(row).join("")}</ul></details>` : ""}
      ${notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}
      <div class="cc-small cc-dimtext">${esc(s.note)} ${esc(s.server_note)}</div></section>`;
  }
  async function load() {
    const r = await api("GET", "/api/brief-delivery");
    if (r.status === 200) { status = r.body; notice = ""; } else notice = r.body.message || "Desktop delivery status is unavailable (restart the server if it predates this update).";
    draw();
  }
  async function act(a, value) {
    if (a === "on" || a === "off" || a === "only") {
      busy = a; notice = ""; draw();
      const body = a === "only" ? { notify_only_if_activity: value } : { enabled: a === "on" };
      const r = await api("POST", "/api/brief-delivery/settings", body);
      busy = "";
      if (r.status === 200) { status = r.body; if (a === "off") lastTest = null; } else notice = r.body.message || "The setting could not be saved.";
      draw();
      return;
    }
    if (a === "test") {
      busy = "test"; notice = ""; draw();
      const r = await api("POST", "/api/brief-delivery/test");
      busy = "";
      if (r.status === 200) { lastTest = r.body.test; status = r.body.delivery; } else notice = r.body.message || "The test notification could not be sent.";
      draw();
    }
  }
  function onClick(e) {
    const b = e.target.closest("button[data-bdl]");
    if (b && !b.disabled) act(b.dataset.bdl);
  }
  function onChange(e) { if (e.target.matches('input[data-bdl="only"]')) act("only", e.target.checked); }
  // the Daily Brief calls this when its workspace opens (one status read; nothing is ever sent from here)
  function mount(target) {
    if (!target) return;
    if (el !== target) { el = target; el.addEventListener("click", onClick); el.addEventListener("change", onChange); }
    lastTest = null;                             // a re-opened card shows only the stored state
    draw();
    load();
  }
  window.BriefDelivery = { mount, get state() { return { enabled: status ? status.enabled : null, onlyActivity: status ? status.notify_only_if_activity : null,
    last: status && status.last_delivery ? status.last_delivery.status : null, history: status ? status.history.length : null, busy, notice, requests,
    lastTest: lastTest && lastTest.result, clickSupported: status && status.identity ? status.identity.click_supported : null,
    branding: status && status.identity ? status.identity.branding : null }; } };
  // Stage 4.4: the notification's fixed local target — open the Daily Brief with the app's existing navigation (after this card is ready,
  // so the opened Daily Brief shows its delivery card too)
  if (location.hash === "#daily-brief" && window.StrategyFit && window.StrategyFit.setView) {
    const btn = document.querySelector('.tab-btn[data-tab="strategy"]');
    if (btn) btn.click();
    window.StrategyFit.setView("brief");
    if (window.history && history.replaceState) history.replaceState(null, "", location.pathname + location.search);
  }
})();
