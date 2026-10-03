// forward_automation.js — Stage 3.7 AUTOMATIC CAPTURE (opt-in) section of the Forward Journal panel.
//
// One global setting: when ON, the local Stock Agent server records each eligible completed close for every active
// forward journal, using the same "Record latest completed close" workflow as the button. It never places an order.
// Server calls: the status when the Forward Journal panel opens, and only explicit actions (turn on / off, save the
// check time, refresh the status, check now). No timers and no polling — the status updates when you ask for it.
// Stage 4.1: the same scheduler also checks saved scans that have alerts on; those results are listed separately
// (saved scan checks · alerts created), never merged into the forward-capture counts.
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  const day = (d) => (d ? new Date(`${d}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) : "");
  const words = (s) => String(s || "").replace(/_/g, " ");
  const RESULT = { CAPTURED: ["Captured", "ok"], NO_NEW_SESSION: ["No new session", "dim"], ALREADY_RECORDED: ["Already recorded", "dim"],
    NO_ACTIVE_JOURNALS: ["No active forward journals", "dim"], PARTIAL_FAILURE: ["Partial — some journals had an error", "warn"],
    ERROR: ["Error", "bad"], LEASE_HELD: ["Skipped — another check was running", "dim"], CHECK_IN_PROGRESS: ["A check is already running", "dim"],
    FORWARD_CAPTURE_OFF: ["Forward capture off", "dim"] };
  const SAVED = { ALERTS_CREATED: "", CHECKED: "", PARTIAL_FAILURE: "partial — some saved scans had an error",
    ERROR: "error", NO_ENABLED_SCANS: "no saved scans with alerts on", LEASE_HELD: "skipped — another check was running" };
  const TRIGGER = { SCHEDULED: "scheduled check", RETRY: "retry", MANUAL_CHECK: "check now" };

  let status = null, busy = "", notice = "", el = null, lastCheck = null;

  const api = (method, url, body) => fetch(url, method === "GET" ? {} : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
    .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }));

  function resultLine(c) {
    if (!c) return `<span class="cc-dimtext">No automatic check yet.</span>`;
    const r = RESULT[c.result] || [words(c.result), "dim"];
    const n = (c.counts || {}).captured || 0;
    return `${tag(c.result === "CAPTURED" ? `Captured ${n} journal${n === 1 ? "" : "s"}` : r[0], r[1])}${c.latest_session ? ` <span class="cc-small">session ${esc(day(c.latest_session))}</span>` : ""}`;
  }
  function journals(c) {
    const js = (c && c.journals) || [];
    if (!js.length) return "";
    return `<details class="fa-details"><summary class="cc-small">Per journal (${esc(js.length)})</summary><ul class="fa-list">${js.map((j) => {
      const r = RESULT[j.result] || [words(j.result), "dim"];
      return `<li class="cc-small"><b>${esc(j.strategy || "Journal")}</b> v${esc(j.version)} ${tag(r[0], r[1])}${j.session ? ` ${esc(day(j.session))}` : ""}${j.code && j.result === "ERROR" ? ` <span class="cc-dimtext">${esc(words(j.code))}</span>` : ""}${(j.missed_recorded || []).length ? ` <span class="cc-dimtext">· ${esc(j.missed_recorded.length)} missed session(s) recorded as MISSED</span>` : ""}</li>`;
    }).join("")}</ul></details>`;
  }
  function savedLines(s, c) {                   // Stage 4.1: saved-scan checks, reported on their own lines
    const st = (s.extensions || {}).saved_scans;
    const x = c && c.extensions && c.extensions.saved_scans;
    if (!st || (!st.alerts_on && !x)) return "";
    const n = (x && x.counts) || {};
    return `<div class="fa-saved cc-small"><div><span class="cc-label">SAVED SCAN CHECKS</span> ${esc(st.alerts_on)} with alerts on${st.paused ? ` · ${esc(st.paused)} with alerts off (not checked automatically)` : ""}${
      s.enabled ? "" : ` · <b>forward capture is off; the scheduler runs for saved scans only</b> (next check ${esc(whenNY(s.next_check_at))})`}</div>
      ${x ? `<div>Last check: ${esc(n.checked || 0)} saved scan${n.checked === 1 ? "" : "s"} checked${SAVED[x.result] === "" ? "" : ` (${esc(SAVED[x.result] || words(x.result).toLowerCase())})`} · <span class="cc-label">ALERTS CREATED</span> ${esc(n.alerts_created || 0)}${n.baselines ? ` · ${esc(n.baselines)} baseline(s)` : ""}${n.errors ? ` · ${esc(n.errors)} error(s)` : ""}</div>` : ""}
      <div class="cc-dimtext">Unread strategy alerts: ${esc(st.unread_alerts)} — shown in Strategy Lab → Scanner.</div></div>`;
  }
  function draw() {
    if (!el) return;
    if (!status) { el.innerHTML = `<div class="fa cc-small cc-dimtext">${notice ? esc(notice) : "Loading automatic capture…"}</div>`; return; }
    const s = status, on = s.enabled;
    const c = lastCheck || s.last_check;
    el.innerHTML = `<div class="fa${on ? " fa-on" : ""}">
      <div class="fa-head"><span class="cc-label">AUTOMATIC CAPTURE</span>${tag(on ? "ON" : "OFF", on ? "ok" : "dim")}
        <button type="button" class="cc-btn cc-mini${on ? "" : " cc-primary"}" data-fa="toggle"${busy ? " disabled" : ""}>${busy === "toggle" ? "Saving…" : on ? "Turn off" : "Turn on automatic capture"}</button></div>
      <div class="cc-small">${on ? `Automatically records each eligible completed close while this Stock Agent server is running. ${esc(s.scope)}`
        : "Off. Turn it on to record each eligible completed close for your active forward journals, using the same Record latest completed close workflow."}</div>
      <div class="fa-server cc-small"><b>Server must be running.</b> ${esc(s.server_note)}</div>
      ${on ? `<div class="fa-grid cc-small">
          <div><span class="cc-label">NEXT CHECK</span><div>${esc(whenNY(s.next_check_at))}</div></div>
          <div><span class="cc-label">LAST CHECK</span><div>${c ? `${esc(whenNY(c.at))} <span class="cc-dimtext">(${esc(TRIGGER[c.trigger] || words(c.trigger).toLowerCase())})</span>` : "—"}</div></div>
          <div><span class="cc-label">LAST RESULT</span><div>${resultLine(c)}</div></div>
          <div><span class="cc-label">CHECK TIME (NEW YORK)</span><div class="fa-time"><input type="time" data-fa="time" value="${esc(s.capture_time_et)}" aria-label="Check time, New York">
            <button type="button" class="cc-btn cc-mini" data-fa="savetime"${busy ? " disabled" : ""}>Save</button></div></div></div>
        ${journals(c)}
        <div class="cc-small cc-dimtext">${esc(s.completed_session_rule)} ${s.retry_pending ? "A retry of the same check is scheduled (every 15 minutes, at most 4 times)." : ""}</div>
        <div class="fa-actions"><button type="button" class="cc-btn cc-mini" data-fa="refresh"${busy ? " disabled" : ""}>Refresh status</button>
          <button type="button" class="cc-btn cc-mini" data-fa="check"${busy ? " disabled" : ""}>${busy === "check" ? "Checking…" : "Check automation now"}</button>
          <span class="cc-small cc-dimtext">Check now records only the latest completed close (never a missed earlier one).</span></div>` : ""}
      ${savedLines(s, c)}
      ${notice ? `<div class="cc-banner cc-warn">${esc(notice)}</div>` : ""}
      <div class="cc-small cc-dimtext">${esc(s.note)} ${esc(s.worker_note)}</div></div>`;
    el.querySelectorAll("[data-fa]").forEach((b) => { if (b.tagName === "BUTTON") b.addEventListener("click", () => act(b.dataset.fa)); });
  }
  async function load() {
    const r = await api("GET", "/api/forward-automation");
    if (r.status === 200) { status = r.body; notice = ""; } else { notice = "Automatic capture status is unavailable (restart the server if it predates this update)."; }
    draw();
  }
  async function act(a) {
    if (a === "refresh") { busy = "refresh"; draw(); await load(); busy = ""; draw(); return; }
    if (a === "toggle" || a === "savetime") {
      const time = el.querySelector('[data-fa="time"]');
      const body = { enabled: a === "toggle" ? !status.enabled : true };
      if (a === "savetime" && time && /^\d{2}:\d{2}$/.test(time.value)) body.capture_time_et = time.value;
      busy = "toggle"; notice = ""; draw();
      const r = await api("POST", "/api/forward-automation", body);
      busy = "";
      if (r.status === 200) { status = r.body; lastCheck = null; } else notice = (r.body && r.body.message) || "The setting could not be saved.";
      draw();
      return;
    }
    if (a === "check") {
      busy = "check"; notice = ""; draw();
      const r = await api("POST", "/api/forward-automation/check");
      busy = "";
      if (r.status === 200) { status = r.body.automation; lastCheck = r.body.check; }
      else notice = (r.body && r.body.message) || "The check could not run.";
      draw();
    }
  }
  // Forward Journal panel calls this when it draws its shell (one status request per panel opening)
  function mount(target) {
    el = target;
    if (!el) return;
    draw();
    load();
  }
  window.ForwardAutomation = { mount, get state() { return { status, busy, notice }; } };
})();
