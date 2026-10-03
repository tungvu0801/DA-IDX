// strategy_scanner.js — Stage 4.0 STRATEGY SCANNER (a Strategy Lab workspace).
//
// ONE exact saved strategy version against ONE list the user chooses (saved universe, watchlist, holdings or a custom
// list), at the latest completed close. The server decides every status with Strategy Fit's own evaluation; this view
// only groups (fixed order, alphabetical within a group), filters and searches what came back — never ranks.
// A scan runs only when the user presses Scan / Refresh scan: one request, no timers, no polling, no AI call.
// Results are kept in page memory only ("Previous scan — refresh to update"); nothing is stored.
// Stage 4.1: saved scans + RULES MET change alerts live in saved_scans.js (its own requests); this view only exposes the
// current choice (state), lets it choose a saved configuration (useConfig) and mounts it under the controls.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("scn-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "");
  const day = (iso) => (iso ? new Date(`${iso}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }) : "—");
  const SYMBOL_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;
  const SOURCES = [["SAVED_UNIVERSE", "Saved universe"], ["WATCHLIST", "Watchlist"], ["HOLDINGS", "Holdings"], ["CUSTOM", "Custom list"]];
  const FILTERS = [["ALL", "All"], ["RULES_MET", "Rules met"], ["RULES_NOT_MET", "Rules not met"], ["INCOMPLETE_DATA", "Incomplete"],
    ["OUTSIDE_UNIVERSE", "Outside universe"], ["DATA_ISSUES", "Data issues"]];
  const KIND = { RULES_MET: "info", RULES_NOT_MET: "dim", INCOMPLETE_DATA: "warn", STALE_DATA: "alert", DATA_UNAVAILABLE: "alert",
    OUTSIDE_UNIVERSE: "dim", UNSUPPORTED: "dim", INTEGRITY_ERROR: "bad", REGISTRY_MISMATCH: "bad" };
  const CTX = { STRICT_CLOSE_CONTEXT: "STRICT CLOSE", POST_CLOSE_CONTEXT: "POST CLOSE", INCOMPLETE_CONTEXT: "INCOMPLETE" };
  const SOURCE = { STAGE_3_2_CACHE: "stored bar cache", MEMORY_CACHE: "fetched earlier (this session)", FETCHED_NOW: "fetched now" };
  let cfg = null, cfgBusy = false, strategyId = "", versionId = "", source = "SAVED_UNIVERSE", custom = "";
  let current = null, currentKey = "", loadedAt = null, fromCache = false, busy = false, notice = "", filter = "ALL", search = "";
  let seq = 0, ctrl = null, requests = 0, shellDrawn = false, pending = null;
  const cache = new Map();                       // key -> { body, loadedAt } (page memory only)
  const openRows = new Set();

  // ---- lists -----------------------------------------------------------------------------------------------------------------
  function parseCustom(text) {
    const valid = new Set(), invalid = [];
    String(text || "").split(/[\s,;]+/).filter(Boolean).forEach((t) => { const s = t.trim().toUpperCase(); (SYMBOL_RE.test(s) ? valid.add(s) : invalid.push(t)); });
    return { symbols: [...valid].sort(), invalid };
  }
  const versionsOf = (sid) => (cfg ? cfg.versions.filter((v) => v.strategy_id === sid).sort((a, b) => b.version_number - a.version_number) : []);
  const version = () => (cfg ? cfg.versions.find((v) => v.strategy_version_id === versionId) : null);
  function keyOf() {
    const c = source === "CUSTOM" ? parseCustom(custom).symbols.join(",") : "";
    return `${versionId}|${source}|${c}`;
  }
  function expectedCount() {
    const v = version();
    if (source === "SAVED_UNIVERSE") return v ? v.universe.length : null;
    if (source === "WATCHLIST") return cfg ? cfg.watchlist.symbols.length : null;
    if (source === "CUSTOM") return parseCustom(custom).symbols.length;
    return null;
  }

  // ---- shell ---------------------------------------------------------------------------------------------------------------
  function ensureShell() {
    if (shellDrawn) return;
    shellDrawn = true;
    root.innerHTML = `<div class="scn">
      <section class="cc-card scn-head">
        <div class="cc-head"><h2>STRATEGY SCANNER <span class="cc-small cc-dimtext">one saved version · the stocks you choose · latest completed close</span></h2>
          <span class="sf-strip">No ranking · No orders · No AI during scan</span></div>
        <div class="scn-controls">
          <label class="sf-field"><span class="cc-label">STRATEGY</span><select data-scn="strategy" aria-label="Strategy"></select></label>
          <label class="sf-field"><span class="cc-label">VERSION</span><select data-scn="version" aria-label="Version"></select></label>
          <label class="sf-field"><span class="cc-label">SCAN LIST</span><select data-scn="source" aria-label="Scan list"></select></label>
          <label class="sf-field scn-custom"><span class="cc-label">CUSTOM SYMBOLS</span><input data-scn="custom" placeholder="e.g. AMD, MU, CLS" autocomplete="off" spellcheck="false" aria-label="Custom symbols"></label>
          <div class="sf-field"><span class="cc-label">DECISION SESSION</span><div class="scn-session" data-scnp="session">—</div></div>
          <div class="scn-buttons"><button type="button" class="cc-btn cc-primary" data-scn="scan">Scan</button>
            <button type="button" class="cc-btn cc-mini" data-scn="refresh" disabled>Refresh scan</button></div></div>
        <div class="cc-small cc-dimtext" data-scnp="listinfo"></div>
        <div data-scnp="status"></div></section>
      <div data-scnp="saved"></div>
      <div data-scnp="result"></div></div>`;
    const q = (k) => root.querySelector(`[data-scn="${k}"]`);
    q("strategy").addEventListener("change", (e) => { strategyId = e.target.value; const vs = versionsOf(strategyId); versionId = vs.length ? vs[0].strategy_version_id : ""; fillVersions(); choose(); });
    q("version").addEventListener("change", (e) => { versionId = e.target.value; choose(); });
    q("source").addEventListener("change", (e) => { source = e.target.value; root.querySelector(".scn-custom").hidden = source !== "CUSTOM"; choose(); });
    q("custom").addEventListener("input", (e) => { custom = e.target.value; drawListInfo(); });
    q("custom").addEventListener("keydown", (e) => { if (e.key === "Enter") runScan(); });
    q("scan").addEventListener("click", () => runScan());
    q("refresh").addEventListener("click", () => runScan());
    root.addEventListener("click", (e) => {
      const f = e.target.closest("[data-scn-filter]");
      if (f) { filter = f.dataset.scnFilter; drawResult(); return; }
      const d = e.target.closest("[data-scn-details]");
      if (d) { const s = d.dataset.scnDetails; (openRows.has(s) ? openRows.delete(s) : openRows.add(s)); drawResult(); return; }
      const o = e.target.closest("[data-scn-fit]");
      if (o && current) window.StrategyFit.openFor(o.dataset.scnFit, { versionId: current.strategy.strategy_version_id, old: !current.strategy.is_current });
    });
    root.querySelector(".scn-custom").hidden = true;
  }
  function fillStrategies() {
    const sel = root.querySelector('[data-scn="strategy"]');
    const names = new Map();
    cfg.versions.forEach((v) => { if (!names.has(v.strategy_id)) names.set(v.strategy_id, v.strategy_name); });
    const list = [...names.entries()].sort((a, b) => a[1].localeCompare(b[1]));
    sel.innerHTML = `<option value="">Choose a strategy…</option>${list.map(([id, n]) => `<option value="${esc(id)}"${id === strategyId ? " selected" : ""}>${esc(n)}</option>`).join("")}`;
    fillVersions();
    const src = root.querySelector('[data-scn="source"]');
    src.innerHTML = SOURCES.map(([k, t]) => `<option value="${k}"${k === source ? " selected" : ""}>${esc(t)}</option>`).join("");
  }
  function fillVersions() {
    const sel = root.querySelector('[data-scn="version"]');
    const vs = versionsOf(strategyId);
    sel.disabled = !vs.length;
    sel.innerHTML = vs.map((v) => `<option value="${esc(v.strategy_version_id)}"${v.strategy_version_id === versionId ? " selected" : ""}${v.scannable ? "" : " disabled"}>v${esc(v.version_number)}${v.is_current ? " · current" : ""} · ${esc(v.readiness.replace(/_/g, " "))}${v.scannable ? "" : ` · ${esc(v.status)}`}</option>`).join("");
  }
  function drawListInfo() {
    const el = root.querySelector('[data-scnp="listinfo"]');
    if (!cfg) { el.textContent = ""; return; }
    const v = version();
    let t = "";
    if (source === "SAVED_UNIVERSE") t = v ? `Saved universe of v${v.version_number}: ${v.universe.length} symbol${v.universe.length === 1 ? "" : "s"} (${v.universe.slice(0, 12).join(", ")}${v.universe.length > 12 ? " …" : ""}).` : "";
    else if (source === "WATCHLIST") t = cfg.watchlist.symbols.length ? `Current watchlist: ${cfg.watchlist.symbols.length} symbols — resolved again on the server when you scan.` : "The watchlist is empty.";
    else if (source === "HOLDINGS") t = cfg.holdings.note;
    else { const p = parseCustom(custom); t = `${p.symbols.length} symbol${p.symbols.length === 1 ? "" : "s"}${p.invalid.length ? ` · not a ticker: ${p.invalid.slice(0, 5).join(", ")}` : ""}${p.symbols.length > cfg.max_symbols ? ` · more than the ${cfg.max_symbols}-symbol limit` : ""}. Upper-cased, de-duplicated and sorted; symbols outside the saved universe are shown as OUTSIDE UNIVERSE.`; }
    el.textContent = t;
  }
  function choose() {                            // a new choice never scans by itself; a remembered scan is shown, labelled
    ctrl && ctrl.abort(); seq++; busy = false; notice = ""; openRows.clear();
    const hit = versionId ? cache.get(keyOf()) : null;
    current = hit ? hit.body : null; loadedAt = hit ? hit.loadedAt : null; fromCache = !!hit; currentKey = hit ? keyOf() : "";
    drawListInfo(); drawStatus(); drawResult();
  }

  // ---- config + scan (explicit requests only) -------------------------------------------------------------------------------
  async function loadConfig() {
    if (cfg || cfgBusy) return;
    cfgBusy = true; drawStatus();
    try {
      requests++;
      const r = await fetch("/api/strategy-scanner/config");
      cfg = await r.json();
      const first = cfg.versions.find((v) => v.is_current && v.scannable) || cfg.versions[0];
      if (first && !strategyId) { strategyId = first.strategy_id; versionId = first.strategy_version_id; }
      fillStrategies(); drawListInfo();
    } catch (e) { notice = "The scanner could not load its settings (the server did not answer)."; }
    cfgBusy = false; drawStatus();
    if (cfg && pending) { const p = pending; pending = null; useConfig(p); }
  }
  function useConfig(p) {                        // a saved scan's exact configuration; scans only when p.run is set
    if (!cfg) { pending = p; return false; }
    const v = cfg.versions.find((x) => x.strategy_version_id === p.versionId);
    if (!v) { notice = "That saved strategy version is not available in the scanner."; drawStatus(); return false; }
    strategyId = v.strategy_id; versionId = v.strategy_version_id; source = p.source;
    custom = p.source === "CUSTOM" ? (p.symbols || []).join(", ") : "";
    fillStrategies();
    const inp = root.querySelector('[data-scn="custom"]');
    if (inp) inp.value = custom;
    root.querySelector(".scn-custom").hidden = source !== "CUSTOM";
    choose();
    if (p.run) runScan();
    return true;
  }
  async function runScan() {
    if (!versionId) return;
    if (source === "CUSTOM") {
      const p = parseCustom(custom);
      if (p.invalid.length) { notice = `Not a valid ticker: ${p.invalid.slice(0, 5).join(", ")}. Nothing was scanned.`; drawStatus(); return; }
      if (!p.symbols.length) { notice = "Enter at least one ticker symbol."; drawStatus(); return; }
    }
    const key = keyOf(), my = ++seq;
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    busy = true; notice = "";
    if (currentKey !== key) { current = null; openRows.clear(); }        // a different scan: never show the old one under it
    drawStatus(); drawResult();
    const body = { strategy_version_id: versionId, source, ...(source === "CUSTOM" ? { symbols: parseCustom(custom).symbols } : {}) };
    let r, b;
    try {
      requests++;
      r = await fetch("/api/strategy-scanner/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: ctrl.signal });
      b = await r.json().catch(() => ({}));
    } catch (e) {
      if (my !== seq) return;                                           // replaced by a newer choice
      busy = false; notice = "The scan could not run (the server did not answer). Nothing changed."; drawStatus(); drawResult(); return;
    }
    if (my !== seq) return;                                             // a newer choice owns the view
    busy = false;
    if (r.status !== 200) { notice = b.message || "The scan could not run."; drawStatus(); drawResult(); return; }
    loadedAt = new Date().toISOString(); fromCache = false; current = b; currentKey = key;
    cache.set(key, { body: b, loadedAt });
    drawStatus(); drawResult();
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------------------
  function drawStatus() {
    const el = root.querySelector('[data-scnp="status"]');
    const n = expectedCount();
    const bits = [];
    if (cfgBusy) bits.push(`<div class="cc-small cc-dimtext">Loading saved strategies…</div>`);
    if (busy) bits.push(`<div class="scn-busy"><b>${current ? "Refreshing…" : `Scanning ${n != null ? `${n} symbol${n === 1 ? "" : "s"}` : source === "HOLDINGS" ? "your holdings" : "the list"}…`}</b> <span class="cc-small cc-dimtext">one request · the previous result stays until the new one arrives</span></div>`);
    else if (current && fromCache) bits.push(`<div class="scn-stale cc-small"><b>Previous scan — refresh to update.</b> Loaded ${esc(whenNY(loadedAt))} for the ${esc(day(current.decision_session))} close.</div>`);
    if (notice) bits.push(`<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>`);
    el.innerHTML = bits.join("");
    const scanBtn = root.querySelector('[data-scn="scan"]'), ref = root.querySelector('[data-scn="refresh"]');
    if (scanBtn) scanBtn.disabled = !versionId || busy;
    if (ref) ref.disabled = !current || busy;
    const s = root.querySelector('[data-scnp="session"]');
    if (s) s.textContent = current && current.decision_session ? `${day(current.decision_session)} close` : "—";
  }
  const fmt = (t) => {                           // the same value formatting as Strategy Fit
    if (t.actual_label != null) return t.actual_label;
    const v = t.actual;
    if (v == null) return "—";
    if (typeof v === "boolean") return v ? "Yes" : "No";
    if (Array.isArray(v)) return v.join(", ");
    if (typeof v !== "number") return String(v);
    if (t.unit === "%") return `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}%`;
    if (t.unit === "$") return `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    if (t.unit === "x") return `${v.toFixed(2)}x`;
    if (t.unit === "0-100") return v.toFixed(1);
    return v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  };
  function leaves(trace) {
    const out = [];
    const walk = (n) => { (Array.isArray(n) ? n : [n]).forEach((x) => { if (!x) return; if (x.conditions) walk(x.conditions); else if (x.feature) out.push(x); }); };
    walk(trace);
    return out;
  }
  function mainText(r) {
    const plural = (k, w) => `${k} ${w}${k === 1 ? "" : "s"}`;
    if (r.fit_status === "RULES_MET") return r.conditions_met === r.conditions_total ? `All ${plural(r.conditions_total, "saved entry condition")} are met.` : r.status_text;
    if (r.fit_status === "RULES_NOT_MET") {
      const u = r.main_unmet[0];
      return `${plural(r.conditions_not_met, "condition")} ${r.conditions_not_met === 1 ? "is" : "are"} not currently met${u ? `: ${u.text} (actual: ${fmt(u)})` : ""}.`;
    }
    if (r.fit_status === "INCOMPLETE_DATA") {
      const u = r.main_unavailable[0];
      return `${plural(r.conditions_evaluable, "condition")} evaluable · ${r.conditions_unavailable} unavailable${u ? `: ${u.text} — ${u.reason}` : ""}`;
    }
    if (r.fit_status === "OUTSIDE_UNIVERSE") return `Not in ${current.strategy.name} v${current.strategy.version}'s saved universe — no rule evaluation.`;
    return r.status_text;
  }
  function detail(r) {
    const ls = leaves(r.trace);
    const rows = ls.map((t) => `<tr><td>${t.result === "MET" ? "✓" : t.result === "NOT_MET" ? "✕" : "?"} ${esc(t.text)}</td><td>${esc(fmt(t))}</td>
      <td>${esc(String(t.result).replace(/_/g, " "))}</td><td>${esc(String(t.availability || "").replace(/_/g, " "))}</td></tr>`).join("");
    const feats = ((r.snapshot || {}).features || []).map((f) => `<li><b>${esc(f.name)}</b> ${esc(fmt({ actual_label: f.value_label, actual: f.value, unit: f.unit }))} · ${esc(String(f.availability).replace(/_/g, " "))}${f.timing ? ` · ${esc(String(f.timing).replace(/_/g, " ").toLowerCase())}` : ""}${f.source ? ` · <span class="cc-dimtext">${esc(f.source)}</span>` : ""}</li>`).join("");
    const warns = [...(r.warnings || []), ...(r.symbol_warnings || [])].map((w) => `<li>${esc(w.text)}</li>`).join("");
    return `<tr class="scn-detail"><td colspan="6"><div class="scn-detailbox">
      ${rows ? `<div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Condition</th><th>Actual</th><th>Result</th><th>Availability</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      <div class="cc-small">${esc(r.status_text)}${r.determinate === false ? " Unavailable inputs are never treated as false." : ""}</div>
      ${feats ? `<div class="cc-small"><span class="cc-label">FEATURES THESE RULES USE</span><ul class="scn-feats">${feats}</ul></div>` : ""}
      <div class="cc-small cc-dimtext">Context: ${esc(CTX[r.context_timing] || "—")} · decision session ${esc(day(current.decision_session))} close${(r.data || {}).source ? ` · daily bars: ${esc(SOURCE[r.data.source] || r.data.source)}` : ""}</div>
      ${warns ? `<ul class="cc-small scn-warns">${warns}</ul>` : ""}</div></td></tr>`;
  }
  function row(r) {
    const evaluated = ["RULES_MET", "RULES_NOT_MET", "INCOMPLETE_DATA"].includes(r.fit_status);
    const counts = evaluated ? `${r.conditions_met} / ${r.conditions_total} conditions met${r.conditions_unavailable ? ` · ${r.conditions_unavailable} unavailable` : ""}` : "—";
    const canOpen = evaluated || r.fit_status === "STALE_DATA";
    return `<tr class="scn-row scn-k-${esc(KIND[r.fit_status] || "dim")}" data-scn-row="${esc(r.symbol)}">
      <td class="scn-sym">${esc(r.symbol)}</td><td>${tag(r.fit_label, KIND[r.fit_status] || "dim")}</td>
      <td class="cc-small">${esc(counts)}${evaluated ? ` <span class="cc-dimtext">(descriptive count)</span>` : ""}</td>
      <td class="cc-small scn-main">${esc(mainText(r))}</td><td class="cc-small">${esc(CTX[r.context_timing] || "—")}</td>
      <td class="scn-act">${canOpen ? `<button type="button" class="cc-btn cc-mini cc-link" data-scn-details="${esc(r.symbol)}" aria-expanded="${openRows.has(r.symbol)}">${openRows.has(r.symbol) ? "Hide details" : "Details"}</button>` : ""}
        ${evaluated ? `<button type="button" class="cc-btn cc-mini" data-scn-fit="${esc(r.symbol)}">Open Strategy Fit</button>` : ""}</td></tr>${openRows.has(r.symbol) && canOpen ? detail(r) : ""}`;
  }
  function matches(r) {
    if (search && !r.symbol.includes(search.toUpperCase())) return false;
    if (filter === "ALL") return true;
    if (filter === "DATA_ISSUES") return r.group === "DATA_ISSUES" || r.group === "NOT_EVALUATED";
    return r.group === filter;
  }
  function drawResult() {
    const el = root.querySelector('[data-scnp="result"]');
    if (!current) {
      el.innerHTML = busy ? "" : `<section class="cc-card"><p class="cc-small cc-dimtext">${cfg && !cfg.versions.length ? "No saved strategies yet — build and save one in the Builder." : "Choose a saved strategy version and a stock list, then press <b>Scan</b>. Nothing runs until you do."}</p></section>`;
      return;
    }
    const c = current, st = c.strategy, sc = c.status_counts;
    const n = (k) => sc[k] || 0;
    const stat = (label, v) => `<div><span class="cc-label">${esc(label)}</span><div class="scn-num">${esc(v)}</div></div>`;
    const summary = `<section class="cc-card scn-summary">
      <div class="cc-head"><h2>${esc(st.name.toUpperCase())} v${esc(st.version)} <span class="cc-small cc-dimtext">${esc(c.source_label)}${st.is_current ? " · current version" : " · older version"}</span></h2>
        <span class="cc-small cc-dimtext">Loaded ${esc(whenNY(loadedAt))}</span></div>
      <div class="scn-stats">${stat("Symbols requested", c.requested_count)}${stat("Evaluated", c.evaluated_count)}${stat("Rules met", n("RULES_MET"))}
        ${stat("Rules not met", n("RULES_NOT_MET"))}${stat("Incomplete", n("INCOMPLETE_DATA"))}${stat("Stale / unavailable", n("STALE_DATA") + n("DATA_UNAVAILABLE"))}
        ${stat("Outside universe", n("OUTSIDE_UNIVERSE"))}${stat("Context", CTX[c.context_timing] || "—")}</div>
      <div class="cc-small cc-dimtext">Entry rule: ${esc(st.entry_text)} · Stored backtests: ${esc(st.evidence.stored_backtests)} · Forward journal: ${esc(st.evidence.forward_journal ? String(st.evidence.forward_journal).toLowerCase().replace(/_/g, " ") : "none")}
        · integrity verified (spec ${esc(String(st.spec_hash).slice(0, 12))} · rules ${esc(String(st.rules_hash).slice(0, 12))})</div>
      ${c.message ? `<div class="cc-banner cc-warn cc-small">${esc(c.message)}</div>` : ""}
      ${(c.warnings || []).length ? `<ul class="cc-small scn-warns">${c.warnings.map((w) => `<li>${esc(w.text)}</li>`).join("")}</ul>` : ""}</section>`;
    const counts = { ALL: c.results.length, DATA_ISSUES: c.results.filter((r) => r.group === "DATA_ISSUES" || r.group === "NOT_EVALUATED").length };
    c.groups.forEach((g) => { if (counts[g.group] === undefined) counts[g.group] = g.count; });
    const chips = `<div class="scn-tools"><div class="sf-filters" role="group" aria-label="Filter">${FILTERS.map(([k, t]) =>
      `<button type="button" data-scn-filter="${k}" class="${filter === k ? "active" : ""}" aria-pressed="${filter === k}">${esc(t)} <span class="sf-n">${counts[k] || 0}</span></button>`).join("")}</div>
      <input class="scn-search" data-scn-search placeholder="Find a symbol" value="${esc(search)}" aria-label="Find a symbol" autocomplete="off" spellcheck="false"></div>`;
    const body = c.groups.map((g) => {
      const rs = c.results.filter((r) => r.group === g.group && matches(r));
      return rs.length ? `<tbody><tr class="scn-group"><th colspan="6">${esc(g.label)} <span class="sf-n">${esc(g.count)}</span></th></tr>${rs.map(row).join("")}</tbody>` : "";
    }).join("");
    el.innerHTML = `${summary}<section class="cc-card scn-results">${chips}
      ${body ? `<div class="sf-tablewrap"><table class="sf-table scn-table"><thead><tr><th>Symbol</th><th>Status</th><th>Conditions</th><th>Main unmet / unavailable condition</th><th>Context</th><th>Action</th></tr></thead>${body}</table></div>`
        : `<p class="cc-small cc-dimtext">No symbol matches this filter.</p>`}
      <p class="cc-small cc-dimtext">Grouped by status in a fixed order and alphabetical inside each group — never by count or performance. ${esc(c.note)}</p></section>`;
    const s = el.querySelector("[data-scn-search]");
    if (s) s.addEventListener("input", (e) => { search = e.target.value.trim(); const pos = e.target.selectionStart; drawResult(); const n2 = el.querySelector("[data-scn-search]"); if (n2) { n2.focus(); n2.setSelectionRange(pos, pos); } });
  }

  function show() {
    ensureShell(); drawStatus(); drawResult(); loadConfig();
    if (window.SavedScans) window.SavedScans.mount(root.querySelector('[data-scnp="saved"]'));
  }
  window.StrategyFit.addWorkspace({ id: "scanner", label: "Scanner", cls: "scn-mode", show });
  window.StrategyScanner = { useConfig, get state() { return { strategyId, versionId, source, busy, notice, filter, search, requests, cached: cache.size,
    fromCache, shown: current && current.strategy.strategy_version_id, results: current ? current.results.length : 0, open: [...openRows],
    custom: source === "CUSTOM" ? parseCustom(custom) : null, session: current ? current.decision_session : null,
    version: version() }; } };
})();
