// daily_brief.js — Stage 4.2 DAILY BRIEF (a Strategy Lab workspace). READ-ONLY.
//
// "What changed in my strategy system at one completed session?" — built on the server from STORED data only (saved-scan
// RULES MET change alerts, forward-journal captures, completed reference cycles, evidence status, data / continuity
// issues). This view only draws what came back. One GET per load: when the workspace opens, on Refresh brief and on
// session navigation. No timers, no polling, no market data, no AI call, and opening it never marks an alert read.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("dbf-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const words = (s) => String(s == null ? "" : s).replace(/_/g, " ");
  const day = (iso) => (iso ? new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }) : "—");
  const short = (iso) => (iso ? new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" }) : "—");
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  const price = (v) => (v == null ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const pct = (v) => (v == null ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}%`);
  const n = (k, one, many) => `${k} ${k === 1 ? one : (many || `${one}s`)}`;
  const STATUS = { RULES_MET: ["RULES MET", "ok"], RULES_NOT_MET: ["RULES NOT MET", "dim"], INCOMPLETE_DATA: ["INCOMPLETE DATA", "warn"],
    STALE_DATA: ["STALE DATA", "alert"], DATA_UNAVAILABLE: ["DATA UNAVAILABLE", "alert"], OUTSIDE_UNIVERSE: ["OUTSIDE UNIVERSE", "dim"],
    UNSUPPORTED: ["UNSUPPORTED", "dim"], INTEGRITY_ERROR: ["INTEGRITY ERROR", "bad"], REGISTRY_MISMATCH: ["REGISTRY MISMATCH", "bad"] };
  const DECISION = { ENTER: "info", EXIT: "info", HOLD: "dim", SKIP: "dim" };
  const CONT = { CONTINUOUS: "ok", GAPPED: "alert", CONTINUITY_BLOCKED: "alert", NOT_STARTED: "dim" };
  const LEVEL = { warn: "DATA", alert: "CONTINUITY", bad: "ERROR", info: "NOTE" };
  const TIMING = { STRICT_FORWARD: "strict forward", POST_CLOSE_FORWARD_CONTEXT: "post-close forward context" };
  const SCAN_RESULT = { BASELINE: ["BASELINE", "dim", "first snapshot — a baseline, no change is reported"],
    NO_CHANGE: ["NO CHANGE", "dim", "no RULES MET change"], RULES_MET_CHANGED: ["RULES MET CHANGED", "info", "see the change above"] };

  let brief = null, want = null, busy = false, notice = "", seq = 0, ctrl = null, requests = 0, renderMs = null, renders = 0, shell = false;

  // ---- loading (the workspace opening, Refresh brief, session navigation) ------------------------------------------------------
  async function load(session) {
    want = session || null;
    const my = ++seq;
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    busy = true; notice = ""; drawStatus();
    let r, b;
    try {
      requests++;
      r = await fetch(`/api/daily-brief${want ? `?session=${encodeURIComponent(want)}` : ""}`, { signal: ctrl.signal });
      b = await r.json().catch(() => ({}));
    } catch (e) {
      if (my !== seq) return;                                          // replaced by a newer selection
      busy = false; notice = "The brief could not be read (the server did not answer). Nothing changed."; drawStatus(); return;
    }
    if (my !== seq) return;                                            // a newer selection owns the view
    busy = false;
    if (r.status !== 200) { notice = b.message || "The brief could not be read."; drawStatus(); return; }
    brief = b;
    render();
  }

  // ---- links (current views of the exact version / journal / saved scan) ---------------------------------------------------------
  function onClick(e) {
    const b = e.target.closest("button");
    if (!b || b.disabled) return;
    const d = b.dataset;
    if (d.dbf === "prev" && brief) load(brief.navigation.previous);
    else if (d.dbf === "next" && brief) load(brief.navigation.next);
    else if (d.dbf === "latest") load(null);
    else if (d.dbf === "refresh") load(want);
    else if (d.dbfScan) { window.StrategyFit.setView("scanner"); if (window.SavedScans && window.SavedScans.open) window.SavedScans.open(d.dbfScan); }
    else if (d.dbfFit) window.StrategyFit.openFor(d.dbfFit, { versionId: d.version, old: d.current === "0" });
    else if (d.dbfJournal) { window.StrategyFit.setView("builder"); if (window.StrategyLab) window.StrategyLab.openVersion(d.dbfJournal, Number(d.number), "forward"); }
    else if (d.dbfEvidence && window.EvidenceComparison) window.EvidenceComparison.openVersion(d.dbfEvidence);
  }
  const fitBtn = (sym, vid, cur) => `<button type="button" class="cc-btn cc-mini" data-dbf-fit="${esc(sym)}" data-version="${esc(vid)}" data-current="${cur === false ? "0" : "1"}">Open Strategy Fit · ${esc(sym)}</button>`;
  const scanBtn = (id) => (id ? `<button type="button" class="cc-btn cc-mini" data-dbf-scan="${esc(id)}">Open saved scan</button>` : "");
  const journalBtn = (x) => (x.strategy_id ? `<button type="button" class="cc-btn cc-mini" data-dbf-journal="${esc(x.strategy_id)}" data-number="${esc(x.version_number)}">Open Forward Journal</button>` : "");
  const evBtn = (vid) => (vid ? `<button type="button" class="cc-btn cc-mini" data-dbf-evidence="${esc(vid)}">Open Evidence</button>` : "");

  // ---- rendering ---------------------------------------------------------------------------------------------------------------
  function ensureShell() {
    if (shell) return;
    shell = true;
    root.innerHTML = `<div class="dbf"><div data-dbfp="status" aria-live="polite"></div><div data-dbfp="body"></div><div data-dbfp="delivery"></div></div>`;
    root.addEventListener("click", onClick);
    root.addEventListener("change", (e) => { if (e.target.matches('[data-dbf="session"]')) load(e.target.value); });
  }
  function drawStatus() {
    const el = root.querySelector('[data-dbfp="status"]');
    if (!el) return;
    el.innerHTML = `${busy ? `<div class="dbf-busy cc-small"><b>Reading the stored brief${want ? ` for ${esc(day(want))}` : ""}…</b> <span class="cc-dimtext">stored data only · the previous brief stays until the new one arrives</span></div>` : ""}${
      notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}`;
    root.querySelectorAll("[data-dbf]").forEach((b) => { if (b.tagName === "BUTTON" && b.dataset.dbf !== "refresh") b.disabled = busy || b.hasAttribute("data-off"); });
  }
  function head(b) {
    const nav = b.navigation, c = b.summary_counts, s = b.system_status;
    const opts = [...new Set([...(b.brief_session && !nav.stored ? [b.brief_session] : []), ...nav.recent])].sort().reverse();
    const off = (x) => (x ? "" : " data-off disabled");
    const chips = (b.headline || []).map((h, i) => `<span class="dbf-chip${i === 3 && c.data_issues ? " dbf-chip-issue" : ""}">${esc(h)}</span>`).join("");
    const auto = s.last_automatic_check;
    return `<section class="cc-card dbf-head">
      <div class="cc-head"><h2>DAILY STRATEGY BRIEF <span class="cc-small cc-dimtext">what the app stored for one completed session</span></h2>
        <span class="sf-strip">Stored data only · No AI · No new market-data calls · No orders</span></div>
      <div class="dbf-bar">
        <div class="dbf-when"><div class="dbf-session">${b.brief_session ? `${esc(day(b.brief_session))} close` : "No stored session yet"}</div>
          <div class="cc-small cc-dimtext">${b.brief_session ? `Stored after the ${esc(day(b.brief_session))} close · ` : ""}generated ${esc(whenNY(b.generated_at))}</div></div>
        <div class="dbf-nav">
          <button type="button" class="cc-btn cc-mini" data-dbf="prev"${off(nav.previous)} title="${esc(nav.previous ? day(nav.previous) : "")}">‹ Previous session</button>
          <select data-dbf="session" aria-label="Stored session"${opts.length ? "" : " disabled"}>${opts.map((o) => `<option value="${esc(o)}"${o === b.brief_session ? " selected" : ""}>${esc(short(o))}${nav.recent.includes(o) ? "" : " (nothing stored)"}</option>`).join("")}</select>
          <button type="button" class="cc-btn cc-mini" data-dbf="next"${off(nav.next)} title="${esc(nav.next ? day(nav.next) : "")}">Next session ›</button>
          <button type="button" class="cc-btn cc-mini" data-dbf="latest"${off(nav.latest && nav.latest !== b.brief_session)}>Latest</button>
          <button type="button" class="cc-btn cc-mini cc-primary" data-dbf="refresh">Refresh brief</button></div></div>
      <div class="dbf-chips">${chips}</div>
      <ul class="dbf-summary">${(b.summary_text || []).map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
      ${(b.warnings || []).filter((w) => w.code === "OLDER_SESSION").map((w) => `<div class="dbf-older cc-small">${esc(w.text)}</div>`).join("")}
      <div class="dbf-system cc-small cc-dimtext">Current settings (not part of this stored brief): automatic capture <b>${esc(s.automatic_capture)}</b> ·
        saved scans with alerts on <b>${esc(s.saved_scans_alerts_on)}</b> of ${esc(s.saved_scans_active)} · unread strategy alerts <b>${esc(s.unread_rule_change_events_total)}</b>${
        auto ? ` · last automatic check ${esc(whenNY(auto.at))} (${esc(words(auto.result).toLowerCase())}${auto.saved_scans_result ? `; saved scans ${esc(words(auto.saved_scans_result).toLowerCase())}` : ""})` : ""}</div></section>`;
  }
  function changes(b) {
    const c = b.summary_counts;
    const items = b.changes.map((x) => {
      const syms = [...x.newly_rules_met.map((y) => y.symbol), ...x.no_longer_rules_met.map((y) => y.symbol)];
      return `<div class="dbf-item" data-dbf-change="${esc(x.alert_id)}">
        <div class="dbf-line"><b>${esc(x.label)}</b> <span class="cc-small cc-dimtext">${esc(x.source_label)}${x.scan_name && x.scan_name !== `${x.label} · ${x.source_label}` ? ` · “${esc(x.scan_name)}”` : ""} · compared with the ${esc(day(x.previous_session))} close</span>${x.unread ? ` ${tag("UNREAD", "info")}` : ""}</div>
        <ul class="dbf-texts">${x.texts.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
        <div class="dbf-syms">${x.newly_rules_met.map((y) => tag(`${y.symbol} · NEWLY RULES MET`, "ok")).join(" ")} ${x.no_longer_rules_met.map((y) => {
          const s = STATUS[y.status] || [words(y.status), "dim"];
          return tag(`${y.symbol} · NO LONGER RULES MET · now ${s[0]}`, s[1]);
        }).join(" ")}</div>
        <div class="dbf-actions">${scanBtn(x.saved_scan_id)}${syms.map((s) => fitBtn(s, x.strategy_version_id, x.is_current_version)).join("")}</div></div>`;
    }).join("");
    const quiet = b.saved_scans_checked.filter((x) => x.result !== "RULES_MET_CHANGED");
    const checked = quiet.length ? `<details class="dbf-more"><summary class="cc-small">Saved scans checked without a RULES MET change (${esc(quiet.length)})</summary>
      <ul class="dbf-list cc-small">${quiet.map((x) => { const r = SCAN_RESULT[x.result] || [words(x.result), "dim", ""];
        return `<li>${tag(r[0], r[1])} <b>${esc(x.scan_name)}</b> <span class="cc-dimtext">${esc(x.label)} · ${esc(x.source_label)} · ${esc(r[2])}</span> · RULES MET: ${esc(x.rules_met.length ? x.rules_met.join(", ") : "none")} ${scanBtn(x.saved_scan_id)}</li>`; }).join("")}</ul></details>` : "";
    return `<section class="cc-card dbf-changes">
      <div class="cc-head"><h2>WHAT CHANGED <span class="cc-small cc-dimtext">stored saved-scan RULES MET change alerts</span></h2></div>
      <div class="dbf-counts cc-small">Saved scans checked: <b>${esc(c.saved_scans_checked)}</b> · RULES MET change events: <b>${esc(c.rule_change_events)}</b> ·
        Newly RULES MET symbols: <b>${esc(c.newly_rules_met_symbols)}</b> · No-longer RULES MET symbols: <b>${esc(c.no_longer_rules_met_symbols)}</b></div>
      ${items || `<p class="cc-small cc-dimtext">No RULES MET change was stored for this session.</p>`}${checked}
      <div class="cc-small cc-dimtext">Counts only — the same stored alert events as Strategy Lab → Scanner. Opening the brief never marks an alert read.</div></section>`;
  }
  function fillText(f) {
    if (f.fill_type === "ENTRY") return f.status === "FILLED" ? `Reference entry filled at this session's open: ${price(f.reference_open_price)}` : `Reference entry not filled (${words(f.reason_code)})`;
    return f.status === "FILLED" ? `Reference exit filled at this session's open: ${price(f.reference_open_price)} · reference move ${pct(f.reference_move_pct)}` : `Reference exit not filled (${words(f.reason_code)})`;
  }
  function journal(j) {
    const dc = j.decision_counts;
    const rows = j.observations.map((o) => `<tr><td class="dbf-sym">${esc(o.symbol)}</td>
        <td>${tag(o.decision, DECISION[o.decision] || "dim")}${o.decision === "SKIP" ? ` <span class="cc-small cc-dimtext">${esc(words(o.reason_code))}</span>` : ""}</td>
        <td class="cc-small">${esc(words(o.state_before))} → ${esc(words(o.state_after))}${o.rules_total ? ` <span class="cc-dimtext">(${esc(o.rules_met)} / ${esc(o.rules_total)} ${esc((o.evaluated_side || "").toLowerCase())} rules met)</span>` : ""}</td>
        <td class="cc-small">${[...o.fills_resolved.map(fillText), ...(o.reference_fill_pending ? [o.reference_fill_pending] : [])].map(esc).join("<br>") || "—"}</td></tr>`).join("");
    const busyJ = dc.ENTER || dc.EXIT || j.fills_resolved || j.capture === "MISSED" || j.missed_recorded.length;
    return `<details class="dbf-item dbf-journal"${busyJ ? " open" : ""} data-dbf-journal-item="${esc(j.journal_id)}">
      <summary class="dbf-line"><b>${esc(j.label)}</b> ${tag(j.capture, j.capture === "CAPTURED" ? "ok" : "alert")}
        <span class="cc-small cc-dimtext">${j.capture === "CAPTURED" ? `ENTER ${esc(dc.ENTER)} · EXIT ${esc(dc.EXIT)} · HOLD ${esc(dc.HOLD)} · SKIP ${esc(dc.SKIP)}${j.context_timing ? ` · ${esc(TIMING[j.context_timing] || words(j.context_timing))}` : ""} · ` : ""}journal now ${esc(words(j.journal_status).toLowerCase())}</span></summary>
      ${j.capture === "MISSED" ? `<div class="cc-small">This session was not captured — stored as <b>MISSED</b> when the ${esc(day(j.detected_with_session))} session was captured. It is never reconstructed.</div>` : ""}
      ${j.missed_recorded.length ? `<div class="cc-small">Recorded as <b>MISSED</b> during this capture: ${esc(j.missed_recorded.map(short).join(", "))}.</div>` : ""}
      ${rows ? `<div class="sf-tablewrap"><table class="sf-table dbf-table"><thead><tr><th>Symbol</th><th>Decision</th><th>State before → after</th><th>Reference fill</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      <div class="dbf-actions">${journalBtn(j)}${evBtn(j.strategy_version_id)}</div></details>`;
  }
  function cycles(b) {
    if (!b.completed_cycles.length) return "";
    const rows = b.completed_cycles.map((c) => {
      const tracked = c.mfe_pct != null && c.mae_pct != null;
      return `<tr><td><b>${esc(c.label)}</b></td><td class="dbf-sym">${esc(c.symbol)} <span class="cc-small cc-dimtext">#${esc(c.cycle_no)}</span></td>
        <td class="cc-small">${esc(short(c.entry_signal_session))} → ${esc(price(c.reference_entry_open))} <span class="cc-dimtext">(${esc(short(c.reference_entry_session))} open)</span></td>
        <td class="cc-small">${esc(short(c.exit_signal_session))} → ${esc(price(c.reference_exit_open))} <span class="cc-dimtext">(${esc(short(c.reference_exit_session))} open)</span></td>
        <td>${esc(pct(c.reference_move_pct))}</td><td>${c.holding_sessions == null ? "—" : esc(n(c.holding_sessions, "session"))}</td>
        <td class="cc-small">${tracked ? `MFE ${esc(pct(c.mfe_pct))} · MAE ${esc(pct(c.mae_pct))}` : `<span class="cc-dimtext">${esc(c.excursion_text)}</span>`}</td></tr>`;
    }).join("");
    return `<div class="dbf-sub"><span class="cc-label">COMPLETED REFERENCE CYCLES (${esc(b.completed_cycles.length)})</span>
      <span class="cc-small cc-dimtext">the reference exit was stored at this session's open · stored values, not recalculated</span></div>
      <div class="sf-tablewrap"><table class="sf-table dbf-table dbf-cycles"><thead><tr><th>Strategy</th><th>Symbol</th><th>Entry signal → reference open</th><th>Exit signal → reference open</th><th>Reference move</th><th>Holding</th><th>MFE / MAE</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  function forward(b) {
    const c = b.summary_counts, d = c.decisions;
    return `<section class="cc-card dbf-forward">
      <div class="cc-head"><h2>FORWARD JOURNALS <span class="cc-small cc-dimtext">stored captures of this session</span></h2></div>
      <div class="dbf-counts cc-small">Captured: <b>${esc(c.forward_captures)}</b> · Not captured (MISSED): <b>${esc(c.forward_sessions_missed)}</b> ·
        ENTER ${esc(d.ENTER)} · EXIT ${esc(d.EXIT)} · HOLD ${esc(d.HOLD)} · SKIP ${esc(d.SKIP)} · Reference fills resolved: ${esc(c.reference_fills_resolved)}</div>
      ${b.forward_activity.length ? b.forward_activity.map(journal).join("") : `<p class="cc-small cc-dimtext">No forward journal stored a session for this date.</p>`}
      ${cycles(b)}</section>`;
  }
  function evidence(b) {
    const items = b.evidence_status.map((e) => {
      const h = e.historical || {}, f = e.forward || {};
      const hist = h.status === "AVAILABLE" ? `${esc(n(h.closed_trades || 0, "closed trade"))} ${tag(words(h.sample_code), "dim")}` : `<span class="cc-dimtext">${esc(h.message || words(h.status) || "—")}</span>`;
      const fwd = f.status === "AVAILABLE" ? `${esc(n(f.completed_cycles || 0, "completed reference cycle"))} ${tag(words(f.sample_code), "dim")} <span class="cc-dimtext">${esc(f.mfe_mae_text || "")}</span>` : `<span class="cc-dimtext">${esc(f.message || words(f.status) || "—")}</span>`;
      const cont = f.status === "AVAILABLE" ? `${tag(words(f.continuity), CONT[f.continuity] || "dim")} <span class="cc-small cc-dimtext">${esc(f.captured_sessions)} captured · ${esc(f.missed_sessions)} missed · journal ${esc(words(f.journal_status).toLowerCase())}</span>` : "—";
      return `<div class="dbf-item" data-dbf-ev="${esc(e.strategy_version_id)}"><div class="dbf-line"><b>${esc(e.label)}</b>${e.status !== "AVAILABLE" ? ` ${tag("READ ERROR", "bad")}` : ""}</div>
        ${e.status === "AVAILABLE" ? `<div class="dbf-ev cc-small"><div><span class="cc-label">HISTORICAL</span> ${hist}</div><div><span class="cc-label">FORWARD</span> ${fwd}</div>
          <div><span class="cc-label">CONTINUITY</span> ${cont}</div></div>` : `<div class="cc-small">${esc(e.message)}</div>`}
        <div class="dbf-actions">${evBtn(e.strategy_version_id)}</div></div>`;
    }).join("");
    return `<section class="cc-card dbf-evidence">
      <div class="cc-head"><h2>EVIDENCE STATUS <span class="cc-small cc-dimtext">versions in this brief · as stored now</span></h2></div>
      ${items || `<p class="cc-small cc-dimtext">No strategy version is represented in this session's stored activity.</p>`}
      <div class="cc-small cc-dimtext">${esc(b.evidence_note)} Sample labels count what exists; they are not a quality rating.</div></section>`;
  }
  function issues(b) {
    const act = (i) => (i.kind === "SAVED_SCAN" ? scanBtn(i.saved_scan_id) : i.kind === "FORWARD" ? journalBtn(i) : i.kind === "EVIDENCE" && i.code !== "NO_FORWARD_JOURNAL" ? evBtn(i.strategy_version_id) : "");
    const li = (i) => `<li class="dbf-issue dbf-l-${esc(i.level)}">${tag(LEVEL[i.level] || "NOTE", i.level === "info" ? "dim" : i.level)} <span class="dbf-code cc-small cc-dimtext">${esc(words(i.code))}</span>
      <div>${esc(i.text)}</div>${act(i) ? `<div class="dbf-actions">${act(i)}</div>` : ""}</li>`;
    return `<section class="cc-card dbf-issues">
      <div class="cc-head"><h2>DATA / CONTINUITY <span class="cc-small cc-dimtext">system and evidence issues — not investment risks</span></h2></div>
      ${b.data_issues.length ? `<ul class="dbf-list">${b.data_issues.map(li).join("")}</ul>` : `<p class="cc-small cc-dimtext">No data or continuity issue was stored for this session.</p>`}
      ${b.notes.length ? `<details class="dbf-more"><summary class="cc-small">Stored notes (${esc(b.notes.length)})</summary><ul class="dbf-list">${b.notes.map(li).join("")}</ul></details>` : ""}</section>`;
  }
  function render() {
    const t0 = performance.now();
    ensureShell();
    const el = root.querySelector('[data-dbfp="body"]');
    const b = brief;
    el.innerHTML = !b ? "" : `${head(b)}${b.empty ? `<section class="cc-card dbf-empty"><p><b>${esc(b.empty_text || "No new stored strategy activity for this session.")}</b></p>
        <p class="cc-small cc-dimtext">Nothing is fetched to fill it: the brief only shows what saved-scan checks and forward-journal captures stored.${b.navigation.previous ? " Use Previous session for the last stored one." : ""}</p></section>`
      : `<div class="dbf-grid">${changes(b)}${forward(b)}${evidence(b)}${issues(b)}</div>`}
      <p class="cc-small cc-dimtext dbf-foot">${esc(b.note)} ${esc(b.link_note)}</p>`;
    drawStatus();
    renderMs = Math.round((performance.now() - t0) * 10) / 10;
    renders++;
  }
  function show() {                                                        // opening the workspace re-reads stored data
    ensureShell(); drawStatus(); load(want);
    if (window.BriefDelivery) window.BriefDelivery.mount(root.querySelector('[data-dbfp="delivery"]'));   // Stage 4.3 (opt-in)
  }

  window.StrategyFit.addWorkspace({ id: "brief", label: "Daily Brief", cls: "dbf-mode", show });
  window.DailyBrief = { open: load, renderPayload: (p) => { brief = p; render(); return renderMs; },
    get state() { return { session: brief && brief.brief_session, want, busy, notice, requests, renderMs, renders, empty: brief ? brief.empty : null,
      counts: brief && brief.summary_counts }; } };
})();
