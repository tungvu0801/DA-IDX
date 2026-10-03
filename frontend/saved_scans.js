// saved_scans.js — Stage 4.1 SAVED SCANS + STRATEGY ALERTS (inside the Strategy Scanner workspace).
//
// A saved scan = one exact saved strategy version + one list (saved universe, watchlist or a custom list). Alerts are OFF
// until the user turns on "Notify me when RULES MET changes". The server stores one snapshot per completed session and an
// in-app alert only when the RULES MET set changed; this view shows what is stored and never decides a status itself.
// Requests happen only when the Scanner opens and on explicit actions (save, check now, open, pause, archive, mark read,
// refresh). No timers, no polling, no market-data requests from this view, no AI call.
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  const day = (iso) => (iso ? new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", timeZone: "UTC" }) : "—");
  const words = (s) => String(s || "").replace(/_/g, " ");
  const LABEL = { RULES_MET: "RULES MET", RULES_NOT_MET: "RULES NOT MET", INCOMPLETE_DATA: "INCOMPLETE DATA", STALE_DATA: "STALE DATA",
    DATA_UNAVAILABLE: "DATA UNAVAILABLE", OUTSIDE_UNIVERSE: "OUTSIDE UNIVERSE", UNSUPPORTED: "UNSUPPORTED",
    INTEGRITY_ERROR: "INTEGRITY ERROR", REGISTRY_MISMATCH: "REGISTRY MISMATCH" };
  const KIND = { RULES_MET: "info", RULES_NOT_MET: "dim", INCOMPLETE_DATA: "warn", STALE_DATA: "warn", DATA_UNAVAILABLE: "warn",
    OUTSIDE_UNIVERSE: "dim", UNSUPPORTED: "dim", INTEGRITY_ERROR: "bad", REGISTRY_MISMATCH: "bad" };
  const GROUPS = [["RULES_MET", "Rules met"], ["RULES_NOT_MET", "Rules not met"], ["INCOMPLETE_DATA", "Incomplete data"],
    ["DATA_ISSUES", "Stale / data unavailable"], ["OUTSIDE_UNIVERSE", "Outside universe"], ["NOT_EVALUATED", "Error / unsupported"]];
  const RESULT = { BASELINE: ["Baseline stored — no alert for a first snapshot", "dim"], ALERT_CREATED: ["RULES MET changed — alert stored", "info"],
    NO_CHANGE: ["No RULES MET change", "dim"], ALREADY_CHECKED: ["Already checked for this session", "dim"],
    NEWER_SNAPSHOT_EXISTS: ["A newer session is already stored", "dim"], NO_SESSION: ["No completed session available", "warn"],
    ERROR: ["The check could not run", "warn"] };
  const TRIGGER = { SCHEDULED: "scheduled check", RETRY: "retry", MANUAL_CHECK: "check now" };
  const EVALUATED = ["RULES_MET", "RULES_NOT_MET", "INCOMPLETE_DATA", "STALE_DATA"];

  let el = null, list = null, alerts = null, opened = null, openId = "", busy = "", notice = "", form = null, confirmId = "";
  let lastCheck = {}, requests = 0, loadSeq = 0;

  const api = (method, url, body) => {
    requests++;
    return fetch(url, method === "GET" ? {} : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }))
      .catch(() => ({ status: 0, body: { message: "The server did not answer. Nothing changed." } }));
  };
  const scanner = () => (window.StrategyScanner ? window.StrategyScanner.state : {});
  const labelOf = (s) => `${s.strategy_name} v${s.version_number == null ? "?" : s.version_number}`;
  const fit = (sym, versionId, isCurrent) => window.StrategyFit && window.StrategyFit.openFor(sym, { versionId, old: isCurrent === false });

  // ---- loading (the Scanner opening, or an explicit refresh / action) ---------------------------------------------------------
  async function loadAll() {
    const my = ++loadSeq;
    const [a, b] = await Promise.all([api("GET", "/api/saved-scans"), api("GET", "/api/strategy-alerts?limit=30")]);
    if (my !== loadSeq) return;
    if (a.status === 200) list = a.body; else notice = a.body.message || "Saved scans are unavailable (restart the server if it predates this update).";
    if (b.status === 200) alerts = b.body;
    draw();
  }
  async function loadOpened(id) {
    const r = await api("GET", `/api/saved-scans/${encodeURIComponent(id)}`);
    if (r.status === 200 && openId === id) opened = r.body;
    else if (r.status !== 200) notice = r.body.message || "That saved scan could not be loaded.";
  }

  // ---- actions -----------------------------------------------------------------------------------------------------------------
  function openForm() {
    const st = scanner(), v = st.version;
    if (!st.versionId || !v) { notice = "Choose a saved strategy version and a list in the scanner first."; form = null; draw(); return; }
    const custom = st.source === "CUSTOM" ? (st.custom || { symbols: [], invalid: [] }) : null;
    const src = { SAVED_UNIVERSE: "Saved universe", WATCHLIST: "Watchlist", CUSTOM: "Custom list", HOLDINGS: "Holdings" }[st.source] || st.source;
    form = { versionId: st.versionId, source: st.source, symbols: custom ? custom.symbols : null, invalid: custom ? custom.invalid : [],
      label: `${v.strategy_name} v${v.version_number} · ${src}`, name: `${v.strategy_name} v${v.version_number} · ${src}`, alerts: false };
    notice = ""; draw();
    const inp = el.querySelector('[data-ssf="name"]');
    if (inp) inp.focus();
  }
  async function save() {
    if (!form || busy) return;
    const nameEl = el.querySelector('[data-ssf="name"]'), alertEl = el.querySelector('[data-ssf="alerts"]');
    if (nameEl) form.name = nameEl.value;
    if (alertEl) form.alerts = alertEl.checked;
    const body = { strategy_version_id: form.versionId, source: form.source, name: form.name.trim() || form.label, alerts_enabled: form.alerts,
      ...(form.source === "CUSTOM" ? { symbols: form.symbols } : {}) };
    busy = "save"; notice = ""; draw();
    const r = await api("POST", "/api/saved-scans", body);
    busy = "";
    if (r.status === 201) {
      form = null; notice = "";
      lastCheck[r.body.saved_scan_id] = { result: "SAVED" };
      await loadAll(); return;
    }
    notice = r.body.message || "The scan could not be saved.";
    if (r.body.status === "ALREADY_SAVED") form = null;
    draw();
  }
  async function checkNow(id) {
    busy = `check:${id}`; notice = ""; draw();
    const r = await api("POST", `/api/saved-scans/${encodeURIComponent(id)}/check`);
    busy = "";
    if (r.status === 200) {
      lastCheck[id] = r.body.check;
      if (openId === id) opened = r.body.saved_scan;
      await loadAll(); return;
    }
    notice = r.body.message || "The check could not run.";
    draw();
  }
  async function setAlerts(id, on) {
    busy = `alerts:${id}`; notice = ""; draw();
    const r = await api("POST", `/api/saved-scans/${encodeURIComponent(id)}/settings`, { alerts_enabled: on });
    busy = "";
    if (r.status !== 200) { notice = r.body.message || "The setting could not be saved."; draw(); return; }
    if (openId === id) await loadOpened(id);
    await loadAll();
  }
  async function archive(id) {
    if (confirmId !== id) { confirmId = id; draw(); return; }
    confirmId = ""; busy = `archive:${id}`; notice = ""; draw();
    const r = await api("POST", `/api/saved-scans/${encodeURIComponent(id)}/archive`);
    busy = "";
    if (r.status !== 200) { notice = r.body.message || "The scan could not be archived."; draw(); return; }
    if (openId === id) await loadOpened(id);
    await loadAll();
  }
  async function openScan(id) {
    openId = id; opened = null; busy = `open:${id}`; notice = ""; draw();
    await loadOpened(id);
    busy = ""; draw();
    const card = el.querySelector(".ssv-open");
    if (card && card.scrollIntoView) card.scrollIntoView({ block: "nearest" });
  }
  function refreshCurrent() {                    // the live Stage 4.0 scan of the same configuration, shown below (not stored)
    if (!opened || !window.StrategyScanner) return;
    window.StrategyScanner.useConfig({ versionId: opened.strategy_version_id, source: opened.list_source, symbols: opened.custom_symbols, run: true });
  }
  async function markRead(id) {
    busy = `read:${id || "all"}`; draw();
    const r = await api("POST", id ? `/api/strategy-alerts/${encodeURIComponent(id)}/read` : "/api/strategy-alerts/read-all");
    busy = "";
    if (r.status !== 200) { notice = r.body.message || "The alert could not be marked read."; draw(); return; }
    if (openId) await loadOpened(openId);
    await loadAll();
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------------------
  function checkLine(id) {
    const c = lastCheck[id];
    if (!c) return "";
    if (c.result === "SAVED") return `<div class="cc-small cc-dimtext">Saved. The first check stores a baseline (no alert); press Check now or wait for the next check after the close.</div>`;
    const r = RESULT[c.result] || [words(c.result), "dim"];
    return `<div class="cc-small ssv-last">${tag(r[0], r[1])}${c.decision_session ? ` ${esc(day(c.decision_session))} close` : ""}${c.message ? ` <span class="cc-dimtext">${esc(c.message)}</span>` : ""}</div>`;
  }
  function savedRow(s) {
    const snap = s.latest_snapshot, id = s.saved_scan_id, on = s.alerts_enabled, arch = !!s.archived_at;
    const met = snap ? (snap.rules_met.length ? snap.rules_met.join(", ") : "none") : "—";
    const b = (a, t, extra) => `<button type="button" class="cc-btn cc-mini${extra || ""}" data-ss="${a}" data-id="${esc(id)}"${busy ? " disabled" : ""}>${t}</button>`;
    return `<li class="ssv-item${openId === id ? " ssv-sel" : ""}" data-ss-item="${esc(id)}">
      <div class="ssv-line"><b class="ssv-name">${esc(s.name)}</b>
        ${arch ? tag("ARCHIVED", "dim") : tag(on ? "Alerts: ON" : "Alerts: OFF", on ? "info" : "dim")}
        ${s.available ? "" : tag("version unavailable", "warn")}</div>
      <div class="cc-small cc-dimtext">${esc(labelOf(s))}${s.is_current === false ? " (older version)" : ""} · ${esc(s.source_label)}${s.custom_symbols ? `: ${esc(s.custom_symbols.slice(0, 8).join(", "))}${s.custom_symbols.length > 8 ? " …" : ""}` : ""}</div>
      <div class="ssv-facts cc-small"><span><span class="cc-label">LAST CHECKED</span> ${snap ? `${esc(day(snap.decision_session))} close` : "not yet"}</span>
        <span><span class="cc-label">RULES MET</span> ${esc(met)}</span></div>
      ${checkLine(id)}
      <div class="ssv-actions">${arch ? b("open", "Open") : `${b("check", busy === `check:${id}` ? "Checking…" : "Check now")}${b("open", "Open")}
        ${b(on ? "pause" : "resume", on ? "Pause alerts" : "Turn on alerts")}
        ${confirmId === id ? `${b("archive", "Confirm archive", " ssv-confirm")}${b("cancel", "Keep")}` : b("archive", "Archive", " cc-link")}`}</div>
      ${confirmId === id ? `<div class="cc-small cc-dimtext">Archiving stops all checks for this saved scan. Its snapshots and alerts are kept; it cannot be un-archived.</div>` : ""}</li>`;
  }
  function saveForm() {
    if (!form) return "";
    if (form.source === "HOLDINGS") return `<div class="ssv-form cc-small"><b>Holdings scans cannot be saved yet.</b> Scheduled checks must never depend on the broker connection, so saved HOLDINGS scans are deferred. Save the watchlist or a custom list instead.
      <div class="ssv-actions"><button type="button" class="cc-btn cc-mini" data-ss="cancel-form">Close</button></div></div>`;
    const bad = form.source === "CUSTOM" && (form.invalid.length || !form.symbols.length);
    return `<div class="ssv-form">
      <div class="cc-small">Saving <b>${esc(form.label)}</b>${form.symbols ? `: ${esc(form.symbols.join(", "))}` : ""}. The strategy version and the list stay fixed; a different choice is a new saved scan.</div>
      ${bad ? `<div class="cc-banner cc-warn cc-small">${form.invalid.length ? `Not a valid ticker: ${esc(form.invalid.slice(0, 5).join(", "))}.` : "Enter at least one ticker symbol."} Nothing can be saved.</div>` : ""}
      <label class="sf-field"><span class="cc-label">NAME</span><input data-ssf="name" maxlength="80" value="${esc(form.name)}" aria-label="Saved scan name" autocomplete="off"></label>
      <label class="ssv-check cc-small"><input type="checkbox" data-ssf="alerts"${form.alerts ? " checked" : ""}> Notify me when RULES MET changes <span class="cc-dimtext">(in this app only · checked after each completed close while the server runs)</span></label>
      <div class="ssv-actions"><button type="button" class="cc-btn cc-mini cc-primary" data-ss="save"${bad || busy ? " disabled" : ""}>${busy === "save" ? "Saving…" : "Save scan"}</button>
        <button type="button" class="cc-btn cc-mini" data-ss="cancel-form">Cancel</button></div></div>`;
  }
  function alertItem(a) {
    const syms = [...a.newly_rules_met, ...a.no_longer_rules_met].map((x) => x.symbol);
    const current = a.is_current_version;
    return `<li class="ssv-alert${a.read_at ? " ssv-read" : ""}" data-ss-alert="${esc(a.alert_id)}">
      <div class="ssv-line cc-small"><b>${esc(day(a.decision_session))} close</b> <span class="cc-dimtext">vs ${esc(day(a.previous_session))} · ${esc(a.scan_name)}</span>
        ${a.read_at ? "" : tag("NEW", "info")}</div>
      <ul class="ssv-texts">${a.texts.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
      <div class="ssv-actions"><button type="button" class="cc-btn cc-mini" data-ss="open" data-id="${esc(a.saved_scan_id)}"${busy ? " disabled" : ""}>Open scan</button>
        ${[...new Set(syms)].map((s) => `<button type="button" class="cc-btn cc-mini" data-ss-fit="${esc(s)}" data-version="${esc(a.strategy_version_id)}" data-current="${current === false ? "0" : "1"}">Open Strategy Fit · ${esc(s)}</button>`).join("")}
        ${a.read_at ? "" : `<button type="button" class="cc-btn cc-mini cc-link" data-ss="read" data-id="${esc(a.alert_id)}"${busy ? " disabled" : ""}>Mark read</button>`}</div></li>`;
  }
  function openedCard() {
    if (!openId) return "";
    if (!opened) return `<section class="cc-card ssv-open"><p class="cc-small cc-dimtext">${busy.startsWith("open:") ? "Loading the stored snapshot…" : "The saved scan could not be loaded."}</p></section>`;
    const o = opened, snap = o.latest_snapshot;
    const head = `<div class="cc-head"><h2>STORED SNAPSHOT <span class="cc-small cc-dimtext">${esc(o.name)}</span></h2>
      <div class="ssv-actions">${o.archived_at || !o.available ? "" : `<button type="button" class="cc-btn cc-mini cc-primary" data-ss="refresh-current">Refresh current scan</button>`}
        <button type="button" class="cc-btn cc-mini" data-ss="close">Close</button></div></div>`;
    if (!snap) return `<section class="cc-card ssv-open">${head}<p class="cc-small">No snapshot stored yet. ${o.archived_at ? "" : "Press <b>Check now</b> to store the baseline for the latest completed close."}</p></section>`;
    const sm = snap.status_map, cond = snap.conditions || {};
    const groups = GROUPS.map(([g, t]) => {
      const syms = (snap.groups[g] || []);
      if (!syms.length) return "";
      return `<tr class="scn-group"><th colspan="3">${esc(t)} <span class="sf-n">${esc(syms.length)}</span></th></tr>${syms.map((s) => {
        const c = cond[s];
        return `<tr><td class="scn-sym">${esc(s)}</td><td>${tag(LABEL[sm[s]] || words(sm[s]), KIND[sm[s]] || "dim")}${c ? ` <span class="cc-small cc-dimtext">${esc(c[0])} / ${esc(c[1])} conditions met</span>` : ""}</td>
          <td class="scn-act">${EVALUATED.includes(sm[s]) && o.available ? `<button type="button" class="cc-btn cc-mini" data-ss-fit="${esc(s)}" data-version="${esc(o.strategy_version_id)}" data-current="${o.is_current === false ? "0" : "1"}">Open Strategy Fit</button>` : ""}</td></tr>`;
      }).join("")}`;
    }).join("");
    const lc = snap.list_changes || { added: [], removed: [] };
    const changes = lc.added.length || lc.removed.length ? `<div class="cc-small">List changed since the previous snapshot (not a rule alert):
      ${lc.added.length ? `added ${esc(lc.added.map((x) => `${x.symbol} (${LABEL[x.status] || words(x.status)})`).join(", "))}` : ""}${lc.added.length && lc.removed.length ? "; " : ""}${lc.removed.length ? `removed ${esc(lc.removed.map((x) => x.symbol).join(", "))}` : ""}.</div>` : "";
    const history = (o.events || []).length ? `<details class="ssv-hist"><summary class="cc-small">Alert history (${esc(o.events.length)})</summary><ul class="ssv-texts cc-small">${o.events.map((e) =>
      `<li><b>${esc(day(e.decision_session))}</b> ${e.texts.map(esc).join(" ")}</li>`).join("")}</ul></details>` : "";
    const sessions = (o.snapshots || []).length > 1 ? `<div class="cc-small cc-dimtext">Stored sessions: ${o.snapshots.map((x) => `${esc(day(x.decision_session))}${x.is_baseline ? " (baseline)" : ""}`).join(" · ")}</div>` : "";
    return `<section class="cc-card ssv-open">${head}
      <div class="ssv-stored cc-small"><b>Stored snapshot — ${esc(day(snap.decision_session))} close.</b> Checked ${esc(whenNY(snap.evaluated_at))} (${esc(TRIGGER[snap.trigger] || words(snap.trigger))})${snap.is_baseline ? " · baseline" : ""}.
        This is the stored result, not a live scan. <span class="cc-dimtext">Refresh current scan runs the scanner now and shows the result below, without storing it.</span></div>
      <div class="cc-small cc-dimtext">${esc(labelOf(o))}${o.is_current === false ? " (older version — kept exactly)" : ""} · ${esc(o.source_label)} · ${esc(snap.resolved_symbols.length)} symbols · spec ${esc(String(snap.spec_hash).slice(0, 12))} · snapshot ${esc(String(snap.snapshot_fingerprint).slice(0, 12))}</div>
      ${changes}
      <div class="sf-tablewrap"><table class="sf-table scn-table ssv-table"><thead><tr><th>Symbol</th><th>Stored status</th><th>Action</th></tr></thead><tbody>${groups}</tbody></table></div>
      ${history}${sessions}</section>`;
  }
  function draw() {
    if (!el) return;
    if (!list) { el.innerHTML = `<div class="cc-small cc-dimtext ssv-loading">${notice ? esc(notice) : "Loading saved scans…"}</div>`; return; }
    const active = list.saved_scans.filter((s) => !s.archived_at), archived = list.saved_scans.filter((s) => s.archived_at);
    const unread = alerts ? alerts.unread : list.unread_alerts;
    const items = alerts ? alerts.alerts : [];
    el.innerHTML = `<div class="ssv">
      <div class="ssv-grid">
        <section class="cc-card ssv-saved">
          <div class="cc-head"><h2>SAVED SCANS <span class="cc-small cc-dimtext">one exact version · one list · alerts off until you turn them on</span></h2>
            <button type="button" class="cc-btn cc-mini" data-ss="save-open"${busy || form ? " disabled" : ""}>Save this scan</button></div>
          ${saveForm()}
          ${active.length ? `<ul class="ssv-list">${active.map(savedRow).join("")}</ul>` : `<p class="cc-small cc-dimtext">No saved scans yet. Choose a version and a list above, then press <b>Save this scan</b>.</p>`}
          ${archived.length ? `<details class="ssv-archived"><summary class="cc-small">Archived (${esc(archived.length)}) — history kept, no longer checked</summary><ul class="ssv-list">${archived.map(savedRow).join("")}</ul></details>` : ""}
          <div class="cc-small cc-dimtext">${esc(list.server_note)} Scans with alerts off (or paused) are not checked automatically; Check now still works.</div></section>
        <section class="cc-card ssv-alerts">
          <div class="cc-head"><h2>STRATEGY ALERTS <span class="cc-small cc-dimtext">Unread: <b data-ss-unread>${esc(unread)}</b></span></h2>
            <div class="ssv-actions"><button type="button" class="cc-btn cc-mini" data-ss="reload"${busy ? " disabled" : ""}>Refresh</button>
              ${unread ? `<button type="button" class="cc-btn cc-mini cc-link" data-ss="read-all"${busy ? " disabled" : ""}>Mark all read</button>` : ""}</div></div>
          ${items.length ? `<ul class="ssv-alist">${items.map(alertItem).join("")}</ul>` : `<p class="cc-small cc-dimtext">No alerts. An alert appears only when a saved scan with alerts on finds that its RULES MET set changed between two completed sessions.</p>`}
          <div class="cc-small cc-dimtext">${esc(list.note)}</div></section>
      </div>
      ${notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}
      ${openedCard()}</div>`;
  }
  function onClick(e) {
    const f = e.target.closest("[data-ss-fit]");
    if (f) { fit(f.dataset.ssFit, f.dataset.version, f.dataset.current === "1"); return; }
    const b = e.target.closest("[data-ss]");
    if (!b || b.disabled) return;
    const id = b.dataset.id, a = b.dataset.ss;
    if (a === "save-open") openForm();
    else if (a === "cancel-form") { form = null; draw(); }
    else if (a === "save") save();
    else if (a === "check") checkNow(id);
    else if (a === "open") openScan(id);
    else if (a === "pause") setAlerts(id, false);
    else if (a === "resume") setAlerts(id, true);
    else if (a === "archive") archive(id);
    else if (a === "cancel") { confirmId = ""; draw(); }
    else if (a === "read") markRead(id);
    else if (a === "read-all") markRead(null);
    else if (a === "reload") { notice = ""; loadAll(); }
    else if (a === "refresh-current") refreshCurrent();
    else if (a === "close") { openId = ""; opened = null; draw(); }
  }

  // the Scanner calls this each time its workspace is shown (the unread count refreshes on opening the Scanner)
  function mount(target) {
    if (!target) return;
    if (el !== target) { el = target; el.addEventListener("click", onClick); }
    draw();
    loadAll();
  }
  // Stage 4.2: the Daily Brief opens one stored saved scan here (the Scanner workspace is shown first, which mounts this view)
  window.SavedScans = { mount, open: (id) => openScan(id), get state() { return { saved: list ? list.saved_scans.length : null, unread: alerts ? alerts.unread : null,
    alerts: alerts ? alerts.alerts.length : null, openId, opened: !!opened, busy, notice, form: !!form, requests, lastCheck }; } };
})();
