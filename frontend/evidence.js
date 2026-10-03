// evidence.js — Stage 3.5 EVIDENCE COMPARISON: a Strategy Lab workspace (Builder | Strategy Fit | Evidence).
//
// For ONE exact saved strategy version, shows its STORED historical evidence (one selected Stage 3.2 backtest run) and
// its STORED forward evidence (one selected Stage 3.3 journal) side by side. The two are never combined: no combined
// score, no ranking, no recommendation, no verdict. Only measures with the same meaning are placed in one table, with a
// simple arithmetic difference (percentage points for % measures). Read-only: no backtest is re-run, no forward session
// is captured, no market data, broker or Claude call. Server calls: the config (saved versions) when the view is first
// opened, and ONE /view request per chosen version / run / journal or explicit "Refresh evidence". An evidence view that
// was already opened is shown again from memory (labelled with when it was loaded). Drawers, symbol details and
// timelines use data already on the page. Only Evidence elements are redrawn — never the Builder, Backtest, Forward
// Journal or Strategy Fit panels. A newer choice aborts the previous request and a late response is discarded (sequence
// check). No timers and no polling.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("ev-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const sgn = (v) => (v > 0 ? "+" : v < 0 ? "−" : "");
  const pct = (v, d = 2) => (isNum(v) ? `${sgn(v)}${Math.abs(v).toFixed(d)}%` : "—");
  const num = (v, d = 2) => (isNum(v) ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : "—");
  const money = (v) => (isNum(v) ? `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—");
  const money0 = (v) => (isNum(v) ? `${v < 0 ? "−" : ""}$${Math.abs(Math.round(v)).toLocaleString()}` : "—");
  const short = (h) => (h ? String(h).slice(0, 12) : "—");
  const words = (s) => String(s || "").replace(/_/g, " ");
  const day = (d) => (d ? new Date(`${d}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) : "—");
  const dayShort = (d) => (d ? new Date(`${d}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric" }) : "—");
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  const JSTATUS = { ACTIVE: "ok", CONTINUITY_BLOCKED: "alert", ARCHIVED: "dim" };
  const CONT = { CONTINUOUS: "ok", GAPPED: "warn", CONTINUITY_BLOCKED: "alert", NOT_STARTED: "dim" };
  const SAMPLE_TAG = { NO_TRADES: "dim", VERY_SMALL_SAMPLE: "warn", SMALL_SAMPLE: "warn", SAMPLE_SIZE: "dim",
    NO_COMPLETED_FORWARD_CYCLES: "dim", VERY_SMALL_FORWARD_SAMPLE: "warn", SMALL_FORWARD_SAMPLE: "warn", FORWARD_SAMPLE_SIZE: "dim" };
  const REASON = { CONDITION: "Exit condition", INVALIDATION: "Invalidation", TARGET: "Target", MAX_HOLDING: "Max holding", MULTIPLE: "Multiple reasons", UNKNOWN: "Unknown" };
  const CYCLE = { COMPLETED: ["COMPLETED", "ok"], OPEN_REFERENCE_CYCLE: ["OPEN / INCOMPLETE", "info"], INCOMPLETE_CONTINUITY_BLOCKED: ["INCOMPLETE · BLOCKED", "alert"], ENTRY_UNFILLED: ["ENTRY UNFILLED", "dim"] };
  const GROUP = { ENTER: "ENTER", EXIT: "EXIT", HOLD: "HOLD / NO EXIT SIGNAL", NO_ENTRY: "NO ENTRY", SKIPPED: "SKIP", BLOCKED: "CONTINUITY BLOCKED" };
  const STATE = { FLAT: "Flat", ENTRY_PENDING: "Entry pending", OPEN: "Open (shadow state)", EXIT_PENDING: "Exit pending", CONTINUITY_BLOCKED: "Continuity blocked" };
  const SIDE = { historical: "HISTORICAL", forward: "FORWARD", both: "BOTH" };
  const HIST_ROWS = 40;
  // Stage 3.6: forward MFE / MAE of one reference cycle (stored; never recomputed). Only journals with Stage 3.6 evidence.
  const excCell = (e) => (!e ? "" : e.status === "COMPLETE" ? `${pct(e.mfe_pct)} / ${pct(e.mae_pct)}`
    : e.status === "TRACKING" ? `${pct(e.mfe_pct)} / ${pct(e.mae_pct)} so far` : e.status === "LEGACY_NOT_TRACKED" ? "not tracked (legacy cycle)"
      : e.status === "INCOMPLETE_DUE_TO_CONTINUITY_GAP" ? "incomplete (continuity gap)" : e.status === "INCOMPLETE_DATA_UNAVAILABLE" ? "incomplete (data unavailable)" : "—");

  let cfg = null, cfgLoading = null, shellDrawn = false;
  let strategyId = "", versionId = "", current = null, busy = false, notice = "", seq = 0, ctrl = null, requests = 0;
  let fromCache = false, revisit = false, showAllTrades = false;
  const cache = new Map();          // "vid|run|journal" -> { body, loadedAt } (this page session only; never persisted)
  const openKeys = new Set();       // expanded drawers, kept across redraws

  // ---- shell + parts (drawn once) ----------------------------------------------------------------------------------------
  const part = (name) => root.querySelector(`[data-evp="${name}"]`);
  function ensureShell() {
    if (shellDrawn) return;
    shellDrawn = true;
    root.innerHTML = `<div class="ev">
      <section class="cc-card ev-head">
        <div class="cc-head"><h2>EVIDENCE COMPARISON <span class="cc-small cc-dimtext">stored evidence · one exact version · read-only</span></h2>
          <span class="sf-strip">Stored evidence · No orders · AI only on request</span></div>
        <div class="ev-controls">
          <label class="ev-field"><span class="cc-label">STRATEGY</span><select data-ev="strategy" aria-label="Strategy"><option value="">Loading…</option></select></label>
          <label class="ev-field"><span class="cc-label">VERSION</span><select data-ev="version" aria-label="Version" disabled></select></label>
          <label class="ev-field"><span class="cc-label">HISTORICAL RUN</span><select data-ev="run" aria-label="Historical run" disabled></select></label>
          <label class="ev-field"><span class="cc-label">FORWARD JOURNAL</span><select data-ev="journal" aria-label="Forward journal" disabled></select></label>
          <button type="button" class="cc-btn cc-mini cc-primary" data-ev="refresh" disabled>Refresh evidence</button></div>
        <div class="ev-notes"><div class="cc-small">Historical and forward evidence use different observation periods. This view is descriptive and does not combine them into one score.</div>
          <div class="cc-small cc-dimtext">Only saved, immutable versions. Every value is read from stored evidence of that exact version — nothing is re-run, captured or fetched.</div></div></section>
      <div data-evp="status" aria-live="polite"></div>
      <section class="cc-card ev-summary" data-evp="summary"><p class="cc-small cc-dimtext">Choose a saved strategy version to see its stored historical and forward evidence side by side.</p></section>
      <section class="cc-card ev-compare" data-evp="compare" hidden></section>
      <div class="ev-sides" data-evp="sides"></div>
      <section class="cc-card" data-evp="symbols" hidden></section>
      <section class="cc-card" data-evp="notes" hidden></section></div>`;
    const q = (k) => root.querySelector(`[data-ev="${k}"]`);
    q("strategy").addEventListener("change", (e) => chooseStrategy(e.target.value));
    q("version").addEventListener("change", (e) => { if (e.target.value) load(e.target.value, null, null, false); });
    q("run").addEventListener("change", (e) => { if (current) load(versionId, e.target.value || null, sel().journal, false); });
    q("journal").addEventListener("change", (e) => { if (current) load(versionId, sel().run, e.target.value || null, false); });
    q("refresh").addEventListener("click", () => { if (versionId) load(versionId, sel().run, sel().journal, true); });
    root.addEventListener("toggle", (e) => {                   // drawers: remember what is open (no request)
      const k = e.target && e.target.dataset && e.target.dataset.ok;
      if (!k) return;
      if (e.target.open) openKeys.add(k); else openKeys.delete(k);
    }, true);
    root.addEventListener("click", (e) => {
      const o = e.target.closest("[data-ev-open]");
      if (o) { openIn(o.dataset.evOpen, o.dataset.run || ""); return; }
      const f = e.target.closest("[data-ev-fit]");
      if (f && window.StrategyFit) { window.StrategyFit.openFor(f.dataset.evFit); return; }
      if (e.target.closest("[data-ev-alltrades]")) { showAllTrades = !showAllTrades; drawSides(); }
    });
  }
  const sel = () => ({ run: current && current.selection ? current.selection.backtest_run_id : null,
    journal: current && current.selection ? current.selection.forward_journal_id : null });

  async function loadConfig() {
    if (cfg || cfgLoading) return cfgLoading;
    cfgLoading = fetch("/api/evidence-comparison/config").then(async (r) => {
      cfg = r.status === 200 ? await r.json() : { versions: [], failed: true };
    }).catch(() => { cfg = { versions: [], failed: true }; }).then(() => { fillStrategies(); fillVersions(); });
    return cfgLoading;
  }
  function strategies() {
    const seen = new Map();
    ((cfg && cfg.versions) || []).forEach((v) => { if (!seen.has(v.strategy_id)) seen.set(v.strategy_id, v); });
    return [...seen.values()];                               // the server's order: strategy name, then id
  }
  function fillStrategies() {
    const s = root.querySelector('[data-ev="strategy"]');
    if (!s) return;
    const list = strategies();
    s.innerHTML = cfg && cfg.failed ? `<option value="">Evidence could not load — restart the server if it predates this update</option>`
      : list.length ? `<option value="">Choose a strategy…</option>${list.map((v) => `<option value="${esc(v.strategy_id)}">${esc(v.strategy_name)}</option>`).join("")}`
        : `<option value="">No saved strategies yet</option>`;
    s.value = strategyId;
  }
  function fillVersions() {
    const s = root.querySelector('[data-ev="version"]');
    if (!s) return;
    const vs = ((cfg && cfg.versions) || []).filter((v) => v.strategy_id === strategyId);
    s.disabled = !vs.length;
    s.innerHTML = vs.map((v) => `<option value="${esc(v.strategy_version_id)}">v${esc(v.version_number)}${v.is_current ? " · current" : " · older"} · ${esc(v.readiness_label)} · ${esc(plural(v.completed_runs, "run"))} · ${esc(plural(v.journals, "journal"))}</option>`).join("");
    s.value = versionId;
  }
  function fillSelectors() {
    const r = root.querySelector('[data-ev="run"]'), j = root.querySelector('[data-ev="journal"]');
    const d = current;
    if (!r || !j) return;
    const runs = (d && d.selection && d.selection.runs) || [], journals = (d && d.selection && d.selection.journals) || [];
    const s = sel(), recent = runs.find((x) => x.status === "COMPLETED");
    r.disabled = !runs.length;
    r.innerHTML = runs.length ? runs.map((x) => `<option value="${esc(x.run_id)}"${x.status === "COMPLETED" ? "" : " disabled"}>#${esc(x.run_id.slice(0, 6))} · ${esc(x.period.start.slice(0, 7))} → ${esc(x.period.end.slice(0, 7))} · ${esc(num(x.slippage_bps_per_side))} bps · ${esc(money(x.commission_per_order))}/order${x === recent ? " · most recent" : ""}${x.status === "COMPLETED" ? "" : ` · ${esc(x.status)}`}</option>`).join("")
      : `<option value="">No stored backtest</option>`;
    r.value = s.run || "";
    j.disabled = !journals.length;
    j.innerHTML = journals.length ? journals.map((x) => `<option value="${esc(x.journal_id)}">${esc(words(x.status))} · started ${esc(day(x.forward_start_date))} · ${esc(plural(x.sessions_captured, "session"))}</option>`).join("")
      : `<option value="">No forward journal</option>`;
    j.value = s.journal || "";
  }
  function chooseStrategy(id) {
    strategyId = id;
    const vs = ((cfg && cfg.versions) || []).filter((v) => v.strategy_id === id);
    const cur = vs.find((v) => v.is_current) || vs[0];
    versionId = cur ? cur.strategy_version_id : "";
    fillVersions();
    if (versionId) load(versionId, null, null, false);
  }

  // ---- loading (one request per new selection; stale responses are discarded) --------------------------------------------
  async function load(vid, run, journal, force) {
    ensureShell();
    versionId = vid;
    const key = `${vid}|${run || ""}|${journal || ""}`, my = ++seq;
    if (ctrl) { ctrl.abort(); ctrl = null; }
    notice = ""; revisit = false;
    const hit = !force && cache.get(key);
    if (hit) {                                               // already opened: shown again from memory, labelled
      current = hit.body; current._loadedAt = hit.loadedAt; busy = false; fromCache = true;
      drawAll(); return;
    }
    busy = true;
    if (!current || current.identity.strategy_version_id !== vid) { current = null; drawAll(); } else drawStatus();
    ctrl = new AbortController();
    let r, body;
    try {
      requests += 1;
      r = await fetch("/api/evidence-comparison/view", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ strategy_version_id: vid, backtest_run_id: run || null, forward_journal_id: journal || null }), signal: ctrl.signal });
      body = await r.json().catch(() => ({}));
    } catch (e) {
      if (my !== seq) return;                                // aborted by a newer choice
      busy = false; notice = "Evidence could not be loaded (the server did not answer). Nothing changed."; drawStatus(); return;
    }
    if (my !== seq) return;                                  // a newer choice owns the view
    busy = false;
    if (r.status !== 200) { notice = `${words(body.status || "ERROR")} — ${body.message || "Evidence could not be loaded."}`; fillSelectors(); drawStatus(); if (!current) drawAll(); return; }
    const loadedAt = new Date().toISOString();
    body._loadedAt = loadedAt;
    cache.set(key, { body, loadedAt });
    cache.set(`${vid}|${body.selection.backtest_run_id || ""}|${body.selection.forward_journal_id || ""}`, { body, loadedAt });
    current = body; fromCache = false; showAllTrades = false;
    drawAll();
  }

  // ---- drawing -------------------------------------------------------------------------------------------------------------
  function drawAll() { fillSelectors(); drawStatus(); drawSummary(); drawCompare(); drawSides(); drawSymbols(); drawNotes(); }
  function drawStatus() {
    const el = part("status");
    if (!el) return;
    const rb = root.querySelector('[data-ev="refresh"]');           // one place decides: every path ends in drawStatus()
    if (rb) rb.disabled = busy || !versionId;
    const bits = [];
    if (notice) bits.push(`<div class="cc-banner cc-warn">${esc(notice)}</div>`);
    if (busy) bits.push(`<div class="sf-refreshing cc-small">${current ? "<b>Previous view — refreshing</b> · " : ""}Reading stored evidence…</div>`);
    else if (current && (fromCache || revisit)) bits.push(`<div class="ev-stale cc-small"><b>Stored evidence as loaded ${esc(whenNY(current._loadedAt))}.</b> A forward journal may have gained captures (or a backtest may have finished) since. <button type="button" class="cc-btn cc-mini" data-ev-refresh-inline>Refresh evidence</button></div>`);
    el.innerHTML = bits.join("");
    const b = el.querySelector("[data-ev-refresh-inline]");
    if (b) b.addEventListener("click", () => load(versionId, sel().run, sel().journal, true));
  }
  const drawer = (key, head, body, cls) => `<details class="sf-drawer${cls ? ` ${cls}` : ""}" data-ok="${esc(key)}"${openKeys.has(key) ? " open" : ""}><summary>${head}</summary><div class="sf-drawer-body">${body}</div></details>`;
  const sampleTag = (s) => (!s || !s.code ? "" : tag(s.code === "SAMPLE_SIZE" ? `${s.closed_trades} CLOSED TRADES`
    : s.code === "FORWARD_SAMPLE_SIZE" ? `${s.completed_cycles} COMPLETED CYCLES` : words(s.code), SAMPLE_TAG[s.code] || "dim"));
  const sampleText = (s) => {                                 // the stored label text without repeating the tag
    const x = String((s && s.text) || "").replace(/^[A-Z][A-Z ]+ — /, "");
    return !x || x === words(s.code) ? "" : x.charAt(0).toUpperCase() + x.slice(1);
  };

  function drawSummary() {
    const el = part("summary");
    if (!el) return;
    const d = current;
    if (!d) { el.innerHTML = `<p class="cc-small cc-dimtext">${busy ? "Reading stored evidence…" : "Choose a saved strategy version to see its stored historical and forward evidence side by side."}</p>`; return; }
    const id = d.identity, h = d.historical, f = d.forward, hd = d.header;
    const hBlock = h.status === "AVAILABLE"
      ? `<div class="ev-val">${esc(day(hd.historical_period.start))} → ${esc(day(hd.historical_period.end))}</div>
         <div class="cc-small">${esc(plural(h.metrics.closed_trades, "closed trade"))} ${h.sample.code === "SAMPLE_SIZE" ? "" : sampleTag(h.sample)}</div>
         <div class="cc-small cc-dimtext">Run #${esc(h.run.run_id.slice(0, 6))}${h.selection === "MOST_RECENT_COMPLETED" ? " · Most recent stored run" : " · selected run"}</div>`
      : `<div class="ev-val cc-dimtext">${esc(h.message || words(h.status))}</div>`;
    const fBlock = f.status === "AVAILABLE"
      ? `<div class="ev-val">Started ${esc(day(f.journal.forward_start_date))}</div>
         <div class="cc-small">${esc(plural(f.captured_sessions, "captured session"))} · ${esc(plural(f.completed_cycles, "completed reference cycle"))} ${f.sample.code === "FORWARD_SAMPLE_SIZE" ? "" : sampleTag(f.sample)}</div>
         <div class="cc-small cc-dimtext">Journal ${tag(words(f.journal_status), JSTATUS[f.journal_status] || "dim")}${f.selection === "MOST_RECENT_ARCHIVED" ? " · only archived journals exist — choose one above" : ""}</div>`
      : `<div class="ev-val cc-dimtext">${esc(f.message || words(f.status))}</div>`;
    el.innerHTML = `<div class="ev-title"><b class="ev-name">EVIDENCE — ${esc(id.strategy_name)}</b> <span class="sf-ver">v${esc(id.version_number)}${id.is_current ? " · current version" : ` · older version (current v${esc(id.current_version)})`}</span> ${tag(id.readiness_label, "dim")}</div>
      <div class="ev-top">
        <div><span class="cc-label">HISTORICAL</span>${hBlock}</div>
        <div><span class="cc-label">FORWARD</span>${fBlock}</div>
        <div><span class="cc-label">CONTINUITY</span><div class="ev-val">${f.status === "AVAILABLE" ? tag(words(f.continuity), CONT[f.continuity] || "dim") : "—"}</div>
          ${f.status === "AVAILABLE" ? `<div class="cc-small">${esc(f.captured_sessions)} captured · ${esc(f.missed_sessions)} missed</div>` : ""}</div>
        <div><span class="cc-label">IDENTITY</span><div class="cc-small">Integrity verified · spec <code>${esc(short(id.spec_hash))}</code> · rules <code>${esc(short(id.rules_hash))}</code></div>
          <div class="cc-small cc-dimtext">Joined by the exact strategy_version_id</div></div></div>
      ${d.comparison.summary_text ? `<div class="ev-diffline">${esc(d.comparison.summary_text)}</div>` : ""}
      ${window.AIExplain ? window.AIExplain.evidenceSlot(d) : ""}
      <div class="cc-small cc-dimtext">${esc(d.note)}</div>`;
  }

  // comparison table: same-meaning measures only
  function cellVal(r, side) {
    const v = r[`${side}_display`], t = r[`${side}_text`];
    if (r.key === "positive_outcomes") {
      const c = r[`${side}_count`];
      if (!c) return `<span class="cc-dimtext">—</span>`;
      return `${esc(c.positive)} / ${esc(c.of)}${isNum(v) ? ` <span class="cc-dimtext">(${esc(v.toFixed(2))}%)</span>` : ""}`;
    }
    if (t && (r.unit === "count" || v == null)) return esc(t);           // counts and fixed texts ("Not tracked", "N/A")
    if (v == null) return `<span class="cc-dimtext">—</span>`;
    if (r.unit === "%") return esc(pct(v));
    if (r.unit === "sessions") return esc(`${v.toFixed(2)} sessions`);
    if (r.unit === "text") return tag(words(v), CONT[v] || "dim");
    return esc(v);
  }
  function drawCompare() {
    const el = part("compare");
    if (!el) return;
    const d = current;
    if (!d) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    const c = d.comparison, f = d.forward;
    const noF = f.status === "AVAILABLE" && !f.completed_cycles;
    const rows = c.compatible_metrics.map((r) => `<tr title="${esc(r.semantic_note || "")}">
        <td><b>${esc(r.label)}</b>${r.semantic_note ? `<div class="cc-small cc-dimtext">${esc(r.semantic_note)}</div>` : ""}</td>
        <td>${cellVal(r, "historical")}${r.historical_measure && r.unit !== "text" ? `<div class="cc-small cc-dimtext">${esc(r.historical_measure)}</div>` : ""}</td>
        <td>${r.forward == null && noF && r.unit !== "count" && r.key !== "mfe" && r.key !== "mae" && r.key !== "continuity" ? `<span class="cc-dimtext">No completed forward cycles yet</span>` : cellVal(r, "forward")}${r.forward_measure && r.unit !== "text" && !((r.key === "mfe" || r.key === "mae") && !r.forward_count) ? `<div class="cc-small cc-dimtext">${esc(r.forward_measure)}</div>` : ""}</td>
        <td>${r.difference_text ? `<b>${esc(r.difference_text)}</b>` : `<span class="cc-dimtext">—</span>`}</td></tr>`).join("");
    el.innerHTML = `<div class="cc-head"><h2>SIDE BY SIDE <span class="cc-small cc-dimtext">compatible measures only · no combined score</span></h2></div>
      <div class="sf-tablewrap ev-tablewrap"><table class="data-table ev-table"><tr><th>Measure</th><th>Historical</th><th>Forward</th><th>Difference</th></tr>${rows}</table></div>
      <div class="cc-small cc-dimtext">${esc(c.difference_rule)}. Hover a row for what each side measures.</div>
      ${drawer("notcompared", `<span class="cc-label">NOT COMPARED</span> <span class="cc-small cc-dimtext">measures that exist on one side only (${esc(c.not_compared.length)})</span>`,
        `<ul class="sf-plain">${c.not_compared.map((x) => `<li><b>${esc(x.label)}</b> — ${esc(x.why)}</li>`).join("")}</ul>
         <ul class="sf-plain">${c.semantic_notes.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`)}`;
  }

  // ---- historical card --------------------------------------------------------------------------------------------------------
  function metric(label, value, title) {
    return `<div class="ev-metric"${title ? ` title="${esc(title)}"` : ""}><span class="cc-label">${esc(label)}</span><div class="ev-mval">${value}</div></div>`;
  }
  function checksList(checks) {
    return `<ul class="sf-plain">${(checks || []).map((c) => `<li>${c.ok ? "✓" : "✕"} ${esc(c.label)}</li>`).join("")}</ul>`;
  }
  function histCard(d) {
    const h = d.historical, id = d.identity;
    const head = (extra) => `<div class="ev-card-head"><span class="cc-label ev-side">HISTORICAL EVIDENCE</span>${extra || ""}</div>`;
    if (h.status === "EVIDENCE_INTEGRITY_ERROR" || h.status === "READ_ERROR") {
      return `<article class="ev-card ev-hist sf-k-bad">${head(tag(words(h.status), "bad"))}<div class="cc-small">${esc(h.message)}</div>${h.checks ? checksList(h.checks) : ""}
        <div class="cc-small cc-dimtext">Forward evidence is shown independently.</div></article>`;
    }
    if (h.status !== "AVAILABLE") {
      const btn = id.readiness === "BACKTEST_READY" ? `<button type="button" class="cc-btn cc-mini" data-ev-open="backtest">Open backtest panel</button>` : "";
      return `<article class="ev-card ev-hist">${head()}<div class="ev-val">${esc(h.message)}</div>
        ${h.reason ? `<div class="cc-small"><b>Reason:</b> ${esc(h.reason)}</div><div class="cc-small cc-dimtext">${esc(h.readiness_explanation || "")}</div>` : ""}
        ${id.forward_only_features && id.forward_only_features.length && h.status === "UNAVAILABLE_FOR_VERSION" ? `<ul class="sf-plain">${id.forward_only_features.map((x) => `<li><b>${esc(x.name)}</b> — ${esc(x.why)}</li>`).join("")}</ul>` : ""}
        <div class="cc-small cc-dimtext">Forward evidence remains viewable. No evidence is not a result.</div>${btn}</article>`;
    }
    const m = h.metrics, a = h.assumptions, f = h.formulas || {};
    const pf = m.profit_factor || {};
    const grid = [
      metric("Closed trades", esc(m.closed_trades)), metric("Open at end", esc(m.open_positions_at_end)),
      metric("Total return", esc(pct(m.total_return_pct)), f.total_return_pct), metric("Max drawdown", esc(pct(m.max_drawdown_pct)), f.max_drawdown_pct),
      metric("Win rate", esc(pct(m.win_rate_pct)), f.win_rate_pct), metric("Avg trade return", esc(pct(m.average_trade_return_pct)), f.average_trade_return_pct),
      metric("Median trade return", esc(pct(m.median_trade_return_pct)), f.median_trade_return_pct), metric("Avg winner", esc(pct(m.average_winner_pct)), f.average_winner_pct),
      metric("Avg loser", esc(pct(m.average_loser_pct)), f.average_loser_pct), metric("Profit factor", esc(pf.text || num(pf.value, 2)), f.profit_factor),
      metric("Avg holding", esc(isNum(m.average_holding_days) ? `${num(m.average_holding_days)} days` : "—"), f.holding_days),
      metric("Median holding", esc(isNum(m.median_holding_days) ? `${num(m.median_holding_days)} days` : "—"), f.holding_days),
      metric("Avg MFE", esc(pct(m.average_mfe_pct)), f.mfe_pct), metric("Avg MAE", esc(pct(m.average_mae_pct)), f.mae_pct),
      metric("Time in market", esc(pct(m.time_in_market_pct)), f.time_in_market_pct),
      metric("Costs", esc(`${money(m.total_commission)} commission · ${money(m.total_slippage_impact)} slippage`)),
      metric("Annualized return", esc(isNum(m.annualized_return_pct) ? pct(m.annualized_return_pct) : "Not shown"), m.annualized_note || f.annualized_return_pct),
    ].join("");
    const assumptions = `<div class="sf-kv cc-small">
        <div><span class="cc-label">PERIOD</span>${esc(day(a.period && a.period.start))} → ${esc(day(a.period && a.period.end))} · ${esc(a.sessions)} sessions</div>
        <div><span class="cc-label">INITIAL EQUITY</span>${esc(money0(a.initial_equity))}</div>
        <div><span class="cc-label">SLIPPAGE / COMMISSION</span>${esc(num(a.slippage_bps_per_side))} bps per side · ${esc(money(a.commission_per_order))} per order${a.zero_cost ? ` ${tag("COST-FREE (OPTIMISTIC)", "warn")}` : ""}</div>
        <div><span class="cc-label">EXECUTION MODEL</span>${esc(a.execution_text || "")}</div>
        <div><span class="cc-label">DATA</span>${esc((a.data && a.data.source) || "")} · feed ${esc((a.data && a.data.feed) || "")} · adjustment ${esc((a.data && a.data.adjustment) || "")} · ${esc((a.data && a.data.timeframe) || "")}</div>
        <div><span class="cc-label">SELECTION POLICY</span>${esc((a.selection_policy && a.selection_policy.code) || "")} — ${esc((a.selection_policy && a.selection_policy.text) || "")}</div>
        <div><span class="cc-label">RISK LIMITS</span>max position ${esc(a.risk ? a.risk.max_position_pct : "—")}% · max open positions ${esc(a.risk ? a.risk.max_open_positions : "—")}</div>
        <div><span class="cc-label">RUN</span>#${esc(h.run.run_id.slice(0, 6))} · config ${esc(short(h.run.config_hash))} · data ${esc(short(h.run.data_hash))} · ${h.run.point_in_time_safe ? "point-in-time safe" : "point-in-time check failed"}</div></div>`;
    const ex = h.exit_reasons;
    const exits = `<table class="data-table ev-mini"><tr><th>Exit reason</th><th>Closed trades</th></tr>${ex.groups.length ? ex.groups.map((g) => `<tr><td>${esc(REASON[g.group] || words(g.group))}</td><td>${esc(g.closed_trades)}</td></tr>`).join("") : `<tr><td colspan="2" class="cc-dimtext">No closed trades</td></tr>`}</table>
      <div class="cc-small cc-dimtext">Scope: ${esc(ex.scope)}. Reason appearances (a trade can have several): ${Object.entries(ex.appearances || {}).map(([k, v]) => `${esc(REASON[k] || k)} ${esc(v)}`).join(" · ")}</div>`;
    const ef = h.entry_frequency;
    const freq = `<div class="cc-small">${esc(num(ef.entry_signals, 0))} entry signals · ${esc(num(ef.entries_filled, 0))} filled · ${esc(num(ef.entry_evaluations, 0))} entry evaluations · ${esc(num(ef.skipped_max_open_positions, 0))} skipped (max open positions) · ${esc(num(ef.skipped_insufficient_cash, 0))} skipped (cash)</div>
      <div class="cc-small cc-dimtext">${esc(ef.text)}</div>`;
    const trades = h.trades || [];
    const shown = showAllTrades ? trades : trades.slice(0, HIST_ROWS);
    const lane = `<div class="sf-tablewrap"><table class="data-table ev-mini"><tr><th>Entry fill</th><th>Exit fill</th><th>Symbol</th><th>Trade return</th><th>Holding</th><th>Exit reason</th></tr>
      ${shown.map((t) => `<tr><td>${esc(day(t.entry_fill_date))}</td><td>${t.status === "CLOSED" ? esc(day(t.exit_fill_date)) : `<span class="cc-dimtext">open at end</span>`}</td><td>${esc(t.symbol)}</td>
        <td>${t.status === "CLOSED" ? esc(pct(t.return_pct)) : "—"}</td><td>${esc(t.holding_days)} d</td><td>${esc(t.status === "CLOSED" ? (t.exit_reasons.length > 1 ? REASON.MULTIPLE : REASON[t.primary_exit_reason] || words(t.primary_exit_reason)) : "—")}</td></tr>`).join("")}</table></div>
      ${trades.length > HIST_ROWS ? `<button type="button" class="cc-btn cc-mini" data-ev-alltrades>${showAllTrades ? `Show the first ${HIST_ROWS}` : `Show all ${esc(trades.length)} trades`}</button>` : ""}`;
    const warns = (h.warnings || []).map((w) => `<li>${esc(w.text)}</li>`).join("");
    const k = `h|${h.run.run_id}`;
    return `<article class="ev-card ev-hist">${head(`${tag(`RUN #${h.run.run_id.slice(0, 6)}`, "dim")} ${h.selection === "MOST_RECENT_COMPLETED" ? `<span class="cc-small">Most recent stored run</span>` : `<span class="cc-small">Selected run</span>`}`)}
      <div class="cc-small">${esc(day(a.period.start))} → ${esc(day(a.period.end))} · simulated backtest · ${sampleTag(h.sample)}</div>
      ${sampleText(h.sample) ? `<div class="cc-small cc-dimtext">${esc(sampleText(h.sample))}</div>` : ""}
      <div class="ev-grid">${grid}</div>
      <button type="button" class="cc-btn cc-mini" data-ev-open="backtest" data-run="${esc(h.run.run_id)}">Open historical equity curve</button>
      ${drawer(`${k}|assume`, `<span class="cc-label">ASSUMPTIONS</span> <span class="cc-small cc-dimtext">capital, costs, execution, data</span>`, assumptions)}
      ${drawer(`${k}|exits`, `<span class="cc-label">EXIT REASONS</span> <span class="cc-small cc-dimtext">stored counts</span>`, exits)}
      ${drawer(`${k}|freq`, `<span class="cc-label">ENTRY FREQUENCY</span> <span class="cc-small cc-dimtext">from the stored audit</span>`, freq)}
      ${drawer(`${k}|warn`, `<span class="cc-label">WARNINGS</span> <span class="cc-small cc-dimtext">stored with the run (${esc((h.warnings || []).length)})</span>`, `<ul class="sf-warns">${warns}</ul>`)}
      ${drawer(`${k}|lane`, `<span class="cc-label">HISTORICAL TIMELINE</span> <span class="cc-small cc-dimtext">backtest trades of this run (${esc(trades.length)})</span>`, lane)}
      <div class="cc-small cc-dimtext">${esc(h.note)}</div></article>`;
  }

  // ---- forward card ---------------------------------------------------------------------------------------------------------
  function fwdCard(d) {
    const f = d.forward;
    const head = (extra) => `<div class="ev-card-head"><span class="cc-label ev-side">FORWARD EVIDENCE</span>${extra || ""}</div>`;
    if (f.status === "EVIDENCE_INTEGRITY_ERROR" || f.status === "READ_ERROR") {
      return `<article class="ev-card ev-fwd sf-k-bad">${head(tag(words(f.status), "bad"))}<div class="cc-small">${esc(f.message)}</div>${f.checks ? checksList(f.checks) : ""}
        <div class="cc-small cc-dimtext">Historical evidence is shown independently.</div></article>`;
    }
    if (f.status === "NO_JOURNAL") {
      return `<article class="ev-card ev-fwd">${head()}<div class="ev-val">No forward journal yet.</div>
        <div class="cc-small cc-dimtext">Nothing is shown as zero — there is no forward sample. Historical evidence remains viewable.</div>
        <button type="button" class="cc-btn cc-mini" data-ev-open="forward">Open Forward Journal</button></article>`;
    }
    const mt = f.reference_cycle_metrics, st = f.states, dc = f.decision_counts, tb = f.timing_breakdown;
    const counts = [
      metric("Captured sessions", esc(f.captured_sessions)), metric("Missed sessions", esc(f.missed_sessions)),
      metric("ENTER decisions", esc(dc.ENTER)), metric("EXIT decisions", esc(dc.EXIT)), metric("HOLD decisions", esc(dc.HOLD)), metric("SKIP decisions", esc(dc.SKIP)),
      metric("Open shadow states", esc(st.open_shadow_states)), metric("Pending entries / exits", esc(`${st.pending_entries} / ${st.pending_exits}`)),
      metric("Completed reference cycles", esc(f.completed_cycles)), metric("Open reference cycles", esc(f.open_cycles)),
      f.blocked_cycles ? metric("Incomplete · blocked cycles", esc(f.blocked_cycles), "A session was missed while these cycles were pending or open; excluded, nothing inferred") : "",
      f.unfilled_entries ? metric("Unfilled reference entries", esc(f.unfilled_entries), "No bar at the next session; not a cycle") : "",
    ].join("");
    const cyc = mt ? [
      metric("Average reference move", esc(pct(mt.average_reference_move_pct)), mt.formula), metric("Median reference move", esc(pct(mt.median_reference_move_pct)), mt.formula),
      metric("Positive completed cycles", esc(`${mt.positive_cycles} positive / ${mt.completed_cycles} completed`)),
      metric("Positive-cycle rate", esc(`${mt.positive_cycle_rate_pct.toFixed(2)}% (n = ${mt.completed_cycles})`), "Positive cycles / completed cycles — a count-based description, shown with its sample size"),
      metric("Negative completed cycles", esc(mt.negative_cycles)),
      metric("Average holding", esc(isNum(mt.average_holding_sessions) ? `${num(mt.average_holding_sessions)} sessions` : "Unavailable")),
      metric("Median holding", esc(isNum(mt.median_holding_sessions) ? `${num(mt.median_holding_sessions)} sessions` : "Unavailable")),
      ...(f.excursion_metrics ? [
        metric("MFE / MAE sample", esc(`${f.excursion_metrics.tracked_completed_cycles} tracked of ${f.excursion_metrics.completed_cycles} completed`), "Only cycles whose reference entry was captured after MFE / MAE tracking started; untracked cycles are never counted as 0"),
        metric("Average MFE", esc(f.excursion_metrics.tracked_completed_cycles ? pct(f.excursion_metrics.average_mfe_pct) : "No tracked cycle yet"), f.excursion_metrics.formulas.mfe_pct),
        metric("Average MAE", esc(f.excursion_metrics.tracked_completed_cycles ? pct(f.excursion_metrics.average_mae_pct) : "No tracked cycle yet"), f.excursion_metrics.formulas.mae_pct),
      ] : [metric("Forward MFE / MAE", esc(f.mfe_mae.text))]),
    ].join("") : "";
    const empty = !mt ? `<div class="ev-empty"><b>${f.captured_sessions ? "Forward journal started" : "Forward journal started — no session captured yet"}</b><div class="cc-small">No completed forward cycles yet.${f.open_cycles ? ` ${esc(plural(f.open_cycles, "open reference cycle"))} (not counted until its reference exit is stored).` : ""}${f.blocked_cycles ? ` ${esc(plural(f.blocked_cycles, "incomplete cycle"))} (continuity blocked) excluded.` : ""}</div>
        <div class="cc-small cc-dimtext">No averages or rates are shown without a completed cycle. Forward MFE / MAE: ${esc(f.mfe_mae.text)}.</div></div>` : "";
    const gap = f.continuity === "GAPPED" ? `<div class="cc-banner cc-warn">GAPPED — ${esc(plural(f.missed_sessions, "eligible session"))} not captured. The forward evidence is incomplete; missed sessions were not reconstructed.</div>`
      : f.continuity === "CONTINUITY_BLOCKED" ? `<div class="cc-banner cc-warn">CONTINUITY BLOCKED — ${esc(st.blocked_symbols.join(", "))}: a session was missed while a cycle was pending or open. Those lifecycles are unknown and excluded; nothing is inferred.</div>` : "";
    const timing = `<div class="ev-timing cc-small"><span>${esc(tb.labels.STRICT_FORWARD)}: <b>${esc(tb.strict_forward)}</b></span><span>${esc(tb.labels.POST_CLOSE_FORWARD_CONTEXT)}: <b>${esc(tb.post_close_forward_context)}</b></span><span>${esc(tb.labels.CAPTURED_AFTER_NEXT_OPEN)}: <b>${esc(tb.captured_after_next_open)}</b></span></div>`;
    const hasX = f.cycles.some((c) => c.excursion);
    const cycTable = f.cycles.length ? `<div class="sf-tablewrap"><table class="data-table ev-mini"><tr><th>Symbol</th><th>Cycle</th><th>Status</th><th>Reference entry</th><th>Reference exit</th><th>Reference move</th><th>Holding</th>${hasX ? "<th>MFE / MAE</th>" : ""}<th>Exit reasons</th></tr>
      ${f.cycles.map((c) => `<tr><td>${esc(c.symbol)}</td><td>${esc(c.cycle_no)}</td><td>${tag(CYCLE[c.status][0], CYCLE[c.status][1])}</td>
        <td>${c.reference_entry_session ? `${esc(dayShort(c.reference_entry_session))} · ${esc(money(c.reference_entry_open))}` : `<span class="cc-dimtext">${esc(c.unfilled_reason ? words(c.unfilled_reason) : `signal ${dayShort(c.entry_signal_session)}`)}</span>`}</td>
        <td>${c.reference_exit_session ? `${esc(dayShort(c.reference_exit_session))} · ${esc(money(c.reference_exit_open))}` : `<span class="cc-dimtext">${c.exit_signal_session ? `exit signal ${esc(dayShort(c.exit_signal_session))}` : "—"}</span>`}</td>
        <td>${c.status === "COMPLETED" ? esc(pct(c.reference_move_pct)) : "—"}</td><td>${isNum(c.holding_sessions) && c.status === "COMPLETED" ? esc(`${c.holding_sessions} sessions`) : "—"}</td>
        ${hasX ? `<td>${esc(excCell(c.excursion))}</td>` : ""}<td>${esc((c.exit_reasons || []).map((r) => REASON[r] || r).join(", ") || "—")}</td></tr>`).join("")}</table></div>
      <div class="cc-small cc-dimtext">${esc(d.forward.reference_cycle_metrics ? d.forward.reference_cycle_metrics.formula : "reference move % = (reference exit open / reference entry open − 1) × 100 — no slippage, commission or sizing")}. A reference cycle involves no broker execution.</div>`
      : `<div class="cc-small cc-dimtext">No ENTER decision stored yet, so there are no reference cycles.</div>`;
    const ex = f.exit_reasons;
    const exits = `<table class="data-table ev-mini"><tr><th>Exit reason</th><th>Completed cycles</th></tr>${ex.groups.length ? ex.groups.map((g) => `<tr><td>${esc(REASON[g.group] || words(g.group))}</td><td>${esc(g.completed_cycles)}</td></tr>`).join("") : `<tr><td colspan="2" class="cc-dimtext">No completed cycles</td></tr>`}</table>
      <div class="cc-small cc-dimtext">Scope: ${esc(ex.scope)}.${ex.pending_exit_signals ? ` ${esc(plural(ex.pending_exit_signals, "exit signal"))} awaiting the reference exit.` : ""}</div>`;
    const ef = f.entry_frequency;
    const freq = `<div class="cc-small">${esc(ef.enter_decisions)} ENTER decisions · ${esc(ef.entry_evaluations)} entry decisions made while flat · ${esc(ef.captured_sessions)} captured sessions</div>
      <div class="cc-small">SKIP reasons: ${Object.entries(dc.skip_reasons || {}).map(([k, v]) => `${esc(words(k).toLowerCase())} ${esc(v)}`).join(" · ") || "none"}</div>
      <div class="cc-small cc-dimtext">${esc(ef.text)}</div>`;
    const assume = `<ul class="sf-plain">${f.assumptions.items.map((x) => `<li>${esc(x.text)}</li>`).join("")}</ul>
      <div class="cc-small">Journal ${tag(words(f.journal_status), JSTATUS[f.journal_status] || "dim")} · continuity ${tag(words(f.continuity), CONT[f.continuity] || "dim")} · data ${esc((f.assumptions.data && f.assumptions.data.source) || "")} · feed ${esc((f.assumptions.data && f.assumptions.data.feed) || "")} · adjustment ${esc((f.assumptions.data && f.assumptions.data.adjustment) || "")}</div>`;
    const warns = f.warnings.length ? `<ul class="sf-warns">${f.warnings.map((w) => `<li><b>${esc(words(w.code))}</b> · ${esc(plural(w.sessions, "session"))} — ${esc(w.text)}</li>`).join("")}</ul>` : `<div class="cc-small cc-dimtext">No session warnings were stored.</div>`;
    const lane = `<ol class="ev-lane">${f.timeline.map((s) => {
      const g = Object.entries(s.groups).filter(([k]) => k === "HOLD" || k === "NO_ENTRY").map(([k, n]) => `${GROUP[k]} ×${n}`).join(" · ");
      const notable = s.notable.map((o) => `<span class="ev-dec ev-dec-${esc(o.group.toLowerCase())}">${esc(o.symbol)} ${esc(GROUP[o.group] || o.decision)}${o.group === "SKIPPED" ? ` (${esc(words(o.reason_code).toLowerCase())})` : ""}${o.exit_reasons.length ? ` · ${esc(o.exit_reasons.map((r) => REASON[r] || r).join(", "))}` : ""}</span>`).join("");
      const fills = s.fills.map((x) => `<span class="ev-fill">${esc(x.symbol)} reference ${esc(x.fill_type.toLowerCase())}${x.status === "FILLED" ? ` ${esc(money(x.reference_open_price))}${isNum(x.reference_move_pct) ? ` · move ${esc(pct(x.reference_move_pct))}` : ""}` : ` unfilled (${esc(words(x.reason_code).toLowerCase())})`}</span>`).join("");
      return `<li class="${s.kind === "MISSED" ? "ev-missed" : ""}"><span class="ev-date">${esc(dayShort(s.session_date))}</span>
        ${s.kind === "MISSED" ? `<span class="cc-dimtext">MISSED — not captured, never reconstructed</span>` : `${fills}${notable}${g ? `<span class="cc-dimtext">${esc(g)}</span>` : ""}${s.context_timing === "POST_CLOSE_FORWARD_CONTEXT" ? ` ${tag("POST CLOSE", "warn")}` : ""}${s.captured_after_next_open ? ` ${tag("AFTER NEXT OPEN", "warn")}` : ""}`}</li>`;
    }).join("") || `<li class="cc-dimtext">No sessions captured yet.</li>`}</ol>`;
    const k = `f|${f.journal.journal_id}`;
    return `<article class="ev-card ev-fwd">${head(`<span class="cc-small cc-dimtext">journal</span> ${tag(words(f.journal_status), JSTATUS[f.journal_status] || "dim")} <span class="cc-small cc-dimtext">continuity</span> ${tag(words(f.continuity), CONT[f.continuity] || "dim")}`)}
      <div class="cc-small">Started ${esc(day(f.journal.forward_start_date))}${f.latest_captured_session ? ` · latest captured ${esc(day(f.latest_captured_session))}` : ""} · captured observations · ${sampleTag(f.sample)}</div>
      ${sampleText(f.sample) ? `<div class="cc-small cc-dimtext">${esc(sampleText(f.sample))}</div>` : ""}
      ${gap}<div class="ev-grid">${counts}</div>${cyc ? `<div class="ev-grid ev-grid-cyc">${cyc}</div>` : empty}
      ${f.excursion_metrics && f.excursion_metrics.legacy_note ? `<div class="cc-small cc-dimtext">${esc(f.excursion_metrics.legacy_note)} MFE / MAE averages use tracked cycles only.</div>` : ""}${timing}
      <button type="button" class="cc-btn cc-mini" data-ev-open="forward">Open journal</button>
      ${drawer(`${k}|cycles`, `<span class="cc-label">REFERENCE CYCLES</span> <span class="cc-small cc-dimtext">from stored ENTER / fill / EXIT rows (${esc(f.cycles.length)})</span>`, cycTable)}
      ${drawer(`${k}|assume`, `<span class="cc-label">ASSUMPTIONS</span> <span class="cc-small cc-dimtext">no equity, sizing, costs or broker fills</span>`, assume)}
      ${drawer(`${k}|exits`, `<span class="cc-label">EXIT REASONS</span> <span class="cc-small cc-dimtext">stored EXIT decisions</span>`, exits)}
      ${drawer(`${k}|freq`, `<span class="cc-label">ENTRY FREQUENCY</span> <span class="cc-small cc-dimtext">stored decisions</span>`, freq)}
      ${drawer(`${k}|warn`, `<span class="cc-label">WARNINGS</span> <span class="cc-small cc-dimtext">stored with the sessions (${esc(f.warnings.length)})</span>`, warns)}
      ${drawer(`${k}|lane`, `<span class="cc-label">FORWARD TIMELINE</span> <span class="cc-small cc-dimtext">captured decisions and reference fills (${esc(f.timeline.length)} sessions)</span>`, lane)}
      <div class="cc-small cc-dimtext">${esc(f.note)}</div></article>`;
  }
  function drawSides() {
    const el = part("sides");
    if (!el) return;
    el.innerHTML = current ? `${histCard(current)}${fwdCard(current)}` : "";
  }

  // ---- per-symbol (alphabetical; never sorted by any result) --------------------------------------------------------------------
  function drawSymbols() {
    const el = part("symbols");
    if (!el) return;
    const d = current;
    if (!d || !d.symbols.length) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    const hOk = d.historical.status === "AVAILABLE", fOk = d.forward.status === "AVAILABLE";
    const cards = d.symbols.map((s) => {
      const h = s.historical, f = s.forward;
      const hl = !hOk ? `<span class="cc-dimtext">no historical evidence selected</span>` : !h ? `<span class="cc-dimtext">not in this run</span>`
        : `${esc(plural(h.closed_trades, "closed trade"))}${h.closed_trades ? ` · avg return ${esc(pct(h.average_return_pct))} · avg holding ${esc(num(h.average_holding_days))} days · ${esc(h.positive_trades)} positive` : ""}${h.open_at_end ? ` · ${esc(h.open_at_end)} open at end` : ""}`;
      const fl = !fOk ? `<span class="cc-dimtext">no forward journal selected</span>` : !f ? `<span class="cc-dimtext">not observed</span>`
        : `${esc(plural(f.observations, "captured session"))} · ${esc(plural(f.completed_cycles, "completed reference cycle"))}${f.completed_cycles ? ` · reference move${f.completed_cycles > 1 ? "s" : ""} ${esc(f.reference_moves_pct.map((x) => pct(x)).join(", "))}${f.completed_cycles > 1 ? ` (avg ${esc(pct(f.average_reference_move_pct))})` : ""}` : ""} · latest state ${esc(STATE[f.latest_state] || words(f.latest_state) || "—")}${f.open_cycle ? ` · ${esc(CYCLE[f.open_cycle.status][0].toLowerCase())} cycle` : ""}${f.excursion && f.excursion.tracked_completed_cycles ? ` · avg MFE ${esc(pct(f.excursion.average_mfe_pct))} / MAE ${esc(pct(f.excursion.average_mae_pct))} (${esc(f.excursion.tracked_completed_cycles)} tracked)` : ""}`;
      return `<article class="ev-sym${f && f.blocked ? " ev-sym-blocked" : ""}"><div class="ev-sym-head"><b>${esc(s.symbol)}</b>${f && f.blocked ? tag("CONTINUITY BLOCKED", "alert") : ""}${s.in_universe ? "" : tag("not in the version's universe", "dim")}
          <button type="button" class="cc-btn cc-mini ev-fitlink" data-ev-fit="${esc(s.symbol)}" title="Opens Strategy Fit (the current state) for this stock — not part of this evidence">Current Strategy Fit</button></div>
        <div class="cc-small"><span class="cc-label">HISTORICAL</span> ${hl}</div>
        <div class="cc-small"><span class="cc-label">FORWARD</span> ${fl}</div></article>`;
    }).join("");
    el.innerHTML = `<div class="cc-head"><h2>BY SYMBOL <span class="cc-small cc-dimtext">alphabetical · symbols in either evidence source</span></h2></div>
      <div class="ev-syms">${cards}</div>
      <div class="cc-small cc-dimtext">A symbol with no trades or no completed cycles is shown as it is — that is evidence too. Current Strategy Fit is a separate, current state and is never part of these numbers.</div>`;
  }
  function drawNotes() {
    const el = part("notes");
    if (!el) return;
    const d = current;
    if (!d || !d.quality_notes.length) { el.hidden = true; el.innerHTML = ""; return; }
    el.hidden = false;
    el.innerHTML = drawer("quality", `<span class="cc-label">EVIDENCE NOTES</span> <span class="cc-small cc-dimtext">limitations to keep in mind (${esc(d.quality_notes.length)}) — not a rating</span>`,
      `<ul class="sf-warns">${d.quality_notes.map((n) => `<li><b>${esc(SIDE[n.side] || "")}</b> · ${esc(n.text)}</li>`).join("")}</ul>`);
  }

  // ---- navigation to the stored panels (Builder view, the exact version) ------------------------------------------------------------
  function openIn(panel, runId) {
    const id = current && current.identity;
    if (!id) return;
    window.StrategyFit.setView("builder");
    if (window.StrategyLab && window.StrategyLab.openVersion) window.StrategyLab.openVersion(id.strategy_id, id.version_number, panel, /^[0-9a-f]{32}$/.test(runId) ? runId : undefined);
  }
  function show() {
    ensureShell();
    if (current) { revisit = true; drawStatus(); }        // back on the view: stored data may have changed meanwhile
    loadConfig();
  }
  // other views can open a version's evidence directly
  function openVersion(vid) {
    const btn = document.querySelector('.tab-btn[data-tab="strategy"]');
    if (btn && !tabEl.classList.contains("active")) btn.click();
    window.StrategyFit.setView("evidence");
    loadConfig().then(() => {
      const v = ((cfg && cfg.versions) || []).find((x) => x.strategy_version_id === vid);
      if (v) { strategyId = v.strategy_id; fillStrategies(); }
      versionId = vid; fillVersions();
      load(vid, null, null, false);
    });
  }

  window.StrategyFit.addWorkspace({ id: "evidence", label: "Evidence", cls: "ev-mode", show });
  window.EvidenceComparison = { openVersion, get state() { return { strategyId, versionId, busy, notice, requests, cached: cache.size, shown: current && current.identity.strategy_version_id, run: sel().run, journal: sel().journal, fromCache, open: [...openKeys] }; } };
})();
