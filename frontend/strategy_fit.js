// strategy_fit.js — Stage 3.4 STRATEGY FIT: a Strategy Lab workspace (Builder | Strategy Fit).
//
// For one chosen stock, compares CURRENT conditions (the latest completed daily close) with the EXACT saved rules of
// every saved strategy version: which entry rules are met, which are not, and which inputs are unavailable. Read-only:
// no orders, no broker, no Claude, no ranking and no composite number. Server calls: the config (symbol sources) when
// the view is first opened, and ONE explicit evaluation per chosen symbol, "Show older versions" change or "Refresh
// current fit". Filters, rule details and the stored evidence drawers use data already on the page (no request).
// Only Strategy Fit elements are redrawn — never the Builder, Backtest or Forward Journal panels. A newer selection
// aborts the previous request, and any late response is discarded (sequence check), so the view always shows the
// symbol that was chosen last. No timers and no polling.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const nav = document.getElementById("sl-subnav");
  const root = document.getElementById("sf-body");
  if (!tabEl || !nav || !root) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const money = (v) => (isNum(v) ? `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—");
  const pct = (v) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}%` : "—");
  const short = (h) => (h ? String(h).slice(0, 12) : "—");
  const day = (d) => (d ? new Date(`${d}T12:00:00Z`).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) : "—");
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  const words = (s) => String(s || "").replace(/_/g, " ");
  const val = (v) => (v == null ? "—" : isNum(v) ? v.toLocaleString(undefined, { maximumFractionDigits: 4 }) : Array.isArray(v) ? v.join(", ") : typeof v === "boolean" ? (v ? "Yes" : "No") : String(v));
  const fval = (v, unit, label) => (label != null ? label : !isNum(v) ? val(v) : unit === "%" ? pct(v) : unit === "$" ? money(v)
    : unit === "x" ? `${v.toFixed(2)}x` : unit === "0-100" ? v.toFixed(1) : val(v));
  const SYMBOL_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;

  const STATUS = { RULES_MET: ["RULES MET", "info"], RULES_NOT_MET: ["RULES NOT MET", "dim"], INCOMPLETE_DATA: ["INCOMPLETE DATA", "warn"],
    OUTSIDE_UNIVERSE: ["OUTSIDE UNIVERSE", "dim"], UNSUPPORTED: ["UNSUPPORTED", "dim"], INTEGRITY_ERROR: ["INTEGRITY ERROR", "bad"],
    REGISTRY_MISMATCH: ["REGISTRY MISMATCH", "bad"], STALE_DATA: ["STALE DATA", "alert"], DATA_UNAVAILABLE: ["DATA UNAVAILABLE", "alert"] };
  const CTX = { STRICT_CLOSE_CONTEXT: ["STRICT CLOSE", "ok"], POST_CLOSE_CONTEXT: ["POST CLOSE", "warn"], INCOMPLETE_CONTEXT: ["INCOMPLETE", "warn"] };
  const FILTERS = [["ALL", "All"], ["RULES_MET", "Rules met"], ["RULES_NOT_MET", "Rules not met"], ["INCOMPLETE_DATA", "Incomplete"],
    ["OUTSIDE_UNIVERSE", "Outside universe"], ["NOT_EVALUATED", "Not evaluated"]];
  const NOT_EVALUATED = new Set(["UNSUPPORTED", "INTEGRITY_ERROR", "REGISTRY_MISMATCH", "STALE_DATA", "DATA_UNAVAILABLE"]);
  const EVALUATED = new Set(["RULES_MET", "RULES_NOT_MET", "INCOMPLETE_DATA"]);
  const SOURCE = { STAGE_3_2_CACHE: "stored bar cache", MEMORY_CACHE: "fetched earlier (this session)", FETCHED_NOW: "fetched now" };
  const STATE_TEXT = { FLAT: "Flat", ENTRY_PENDING: "Entry pending", OPEN: "Open (shadow state)", EXIT_PENDING: "Exit pending", CONTINUITY_BLOCKED: "Continuity blocked" };
  const DECISION_TEXT = (o) => (o.state_after === "CONTINUITY_BLOCKED" ? "CONTINUITY BLOCKED" : o.decision === "HOLD"
    ? (o.state_after === "OPEN" ? "HOLD / NO EXIT SIGNAL" : "NO ENTRY") : o.decision);

  let view = "builder", cfg = null, cfgLoading = null, shellDrawn = false;
  let symbol = "", includeOld = false, filter = "ALL";
  let current = null, busy = false, stale = false, notice = "", seq = 0, ctrl = null, requests = 0;
  const results = new Map();          // "SYM|0/1" -> the last response for it (this page session only; never persisted)
  const openKeys = new Set();         // expanded drawers, kept across redraws of the cards

  // ---- sub-navigation: Builder | Strategy Fit (visibility only — nothing else is re-rendered) --------------------------
  const workspaces = [];                // Stage 3.5+: further read-only workspaces register here (e.g. Evidence)
  function drawNav() {
    const b = (id, label) => `<button type="button" role="tab" data-sfv="${esc(id)}" aria-selected="${view === id}" class="${view === id ? "active" : ""}">${esc(label)}</button>`;
    nav.innerHTML = `<div class="sf-subnav" role="tablist" aria-label="Strategy Lab workspace">
      ${b("builder", "Builder")}${b("fit", "Strategy Fit")}${workspaces.map((w) => b(w.id, w.label)).join("")}</div>`;
    nav.querySelectorAll("[data-sfv]").forEach((x) => x.addEventListener("click", () => setView(x.dataset.sfv)));
  }
  function setView(v) {
    if (view !== v) {
      view = v; tabEl.classList.toggle("sf-mode", v === "fit");
      workspaces.forEach((w) => tabEl.classList.toggle(w.cls, v === w.id));
      drawNav();
    }
    if (v === "fit") { ensureShell(); fillSymbols(); }
    const w = workspaces.find((x) => x.id === v);
    if (w) w.show();
  }
  // a workspace = { id, label, cls (class set on the Strategy Lab tab while it is shown), show() } — visibility only
  function addWorkspace(w) { if (w && !workspaces.some((x) => x.id === w.id)) { workspaces.push(w); drawNav(); } }

  // ---- shell + parts ----------------------------------------------------------------------------------------------------
  const part = (name) => root.querySelector(`[data-sfp="${name}"]`);
  function ensureShell() {
    if (shellDrawn) return;
    shellDrawn = true;
    root.innerHTML = `<div class="sf">
      <section class="cc-card sf-head">
        <div class="cc-head"><h2>STRATEGY FIT <span class="cc-small cc-dimtext">current rule fit · saved versions · read-only</span></h2>
          <span class="sf-strip">No broker · No orders · AI only on request</span></div>
        <div class="sf-controls">
          <label class="sf-field"><span class="cc-label">SYMBOL</span><select data-sf="symbol" aria-label="Symbol"><option value="">Choose a stock…</option></select></label>
          <label class="sf-field"><span class="cc-label">OR TYPE A TICKER</span><span class="sf-manual"><input data-sf="manual" maxlength="10" placeholder="e.g. AMD" autocomplete="off" spellcheck="false" aria-label="Ticker"><button type="button" class="cc-btn cc-mini" data-sf="check">Check</button></span></label>
          <label class="sf-old cc-small"><input type="checkbox" data-sf="old"> Show older versions</label>
          <button type="button" class="cc-btn cc-mini cc-primary" data-sf="refresh" disabled>Refresh current fit</button></div>
        <div class="sf-notes"><div class="cc-small">${esc("Rules are evaluated on the latest completed daily close. No order is placed.")}</div>
          <div class="cc-small cc-dimtext">This view compares current data with your saved rules. It does not rank strategies or predict future returns.</div></div></section>
      <div data-sfp="status" aria-live="polite"></div>
      <section class="cc-card sf-summary" data-sfp="summary"><p class="cc-small cc-dimtext">Choose a stock to compare it with every saved strategy version's entry rules. Nothing is evaluated until you choose.</p></section>
      <div data-sfp="filters"></div>
      <div class="sf-cards" data-sfp="cards"></div>
      <div data-sfp="env"></div></div>`;
    const sel = root.querySelector('[data-sf="symbol"]'), inp = root.querySelector('[data-sf="manual"]');
    sel.addEventListener("change", () => { if (sel.value) evaluate(sel.value); });
    root.querySelector('[data-sf="check"]').addEventListener("click", () => manual(inp));
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") manual(inp); });
    root.querySelector('[data-sf="old"]').addEventListener("change", (e) => { includeOld = e.target.checked; if (symbol) evaluate(symbol); });
    root.querySelector('[data-sf="refresh"]').addEventListener("click", () => { if (symbol) evaluate(symbol); });
    root.addEventListener("toggle", (e) => {                       // drawers: remember what is open (no request)
      const k = e.target && e.target.dataset && e.target.dataset.ok;
      if (!k) return;
      if (e.target.open) openKeys.add(k); else openKeys.delete(k);
    }, true);
    root.addEventListener("click", (e) => {
      const f = e.target.closest("[data-sff]");
      if (f) { filter = f.dataset.sff; drawFilters(); drawCards(); return; }
      const o = e.target.closest("[data-sf-open]");
      if (o) openIn(o.dataset.sfOpen, o.dataset.sid, Number(o.dataset.n));
    });
    loadConfig();
  }
  async function loadConfig() {
    if (cfg || cfgLoading) return cfgLoading;
    cfgLoading = fetch("/api/strategy-fit/config").then(async (r) => {
      cfg = r.status === 200 ? await r.json() : { symbols: { strategies: [], watchlist: [] }, saved_strategies: null };
      fillSymbols();
    }).catch(() => { cfg = { symbols: { strategies: [], watchlist: [] }, saved_strategies: null }; fillSymbols(); });
    return cfgLoading;
  }
  function dash() { return (window.CommandCenter && window.CommandCenter.last) || null; }
  function holdings() { const d = dash(); return d && Array.isArray(d.my_stocks) ? d.my_stocks.map((c) => c.symbol).filter((s) => SYMBOL_RE.test(s || "")) : []; }
  function quoteFor(sym) {                                        // shown for context only; never used by the rules
    const d = dash(), w = d && Array.isArray(d.watchlist) ? d.watchlist.find((x) => x.symbol === sym) : null;
    return w && isNum(w.price) ? { price: w.price, pct: w.session_pct } : null;
  }
  function fillSymbols() {
    const sel = root.querySelector('[data-sf="symbol"]');
    if (!sel) return;
    const s = (cfg && cfg.symbols) || { strategies: [], watchlist: [] };
    const groups = [["In saved strategies", s.strategies || []], ["Watchlist", s.watchlist || []], ["Holdings (from the dashboard)", holdings()]];
    const known = new Set(groups.flatMap((g) => g[1]));
    if (symbol && !known.has(symbol)) groups.push(["Typed", [symbol]]);
    sel.innerHTML = `<option value="">Choose a stock…</option>${groups.filter((g) => g[1].length).map(([label, syms]) =>
      `<optgroup label="${esc(label)}">${[...new Set(syms)].map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("")}</optgroup>`).join("")}`;
    sel.value = symbol || "";
  }
  function manual(inp) {
    const t = String(inp.value || "").trim().toUpperCase();
    if (!SYMBOL_RE.test(t)) { notice = t ? `"${t}" is not a valid ticker symbol.` : "Type a ticker symbol first."; drawStatus(); return; }
    inp.value = "";
    evaluate(t);
  }

  // ---- evaluation (one explicit request; stale responses are discarded) --------------------------------------------------
  async function evaluate(sym) {
    ensureShell();
    symbol = sym;
    fillSymbols();
    const key = `${sym}|${includeOld ? 1 : 0}`, my = ++seq;
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    busy = true; notice = "";
    const cached = results.get(key);
    if (cached) { current = cached; stale = true; drawResult(); }           // previous view, clearly labelled
    else if (current && current.symbol === sym) stale = true;               // same stock: keep it visible while refreshing
    else { current = null; stale = false; drawResult(); }
    drawStatus();
    root.querySelector('[data-sf="refresh"]').disabled = true;
    let r, body;
    try {
      requests += 1;
      r = await fetch("/api/strategy-fit/evaluate", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: sym, include_old_versions: includeOld }), signal: ctrl.signal });
      body = await r.json().catch(() => ({}));
    } catch (e) {
      if (my !== seq) return;                                               // aborted by a newer choice
      busy = false; notice = "Strategy Fit could not be evaluated (the server did not answer). Nothing changed.";
      drawStatus(); root.querySelector('[data-sf="refresh"]').disabled = !symbol; return;
    }
    if (my !== seq) return;                                                 // a newer choice owns the view
    busy = false; stale = false;
    root.querySelector('[data-sf="refresh"]').disabled = false;
    if (r.status !== 200) { notice = body.message || "Strategy Fit could not be evaluated."; drawStatus(); if (!current) drawResult(); return; }
    results.set(key, body); current = body;
    drawResult(); drawStatus();
  }

  // ---- drawing -----------------------------------------------------------------------------------------------------------
  function drawStatus() {
    const el = part("status");
    if (!el) return;
    const bits = [];
    if (notice) bits.push(`<div class="cc-banner cc-warn">${esc(notice)}</div>`);
    if (busy && stale && current) bits.push(`<div class="sf-refreshing"><b>Previous view — refreshing</b> <span class="cc-small cc-dimtext">${esc(current.symbol)} as evaluated ${esc(whenNY(current.evaluated_at))}; it is replaced when the new evaluation finishes.</span></div>`);
    else if (busy) bits.push(`<div class="sf-refreshing cc-small">Evaluating ${esc(symbol)} at the latest completed close…</div>`);
    el.innerHTML = bits.join("");
  }
  function drawResult() { drawSummary(); drawFilters(); drawCards(); drawEnv(); showFocus(); }

  function freshness(d) {
    const f = d.data_freshness;
    if (!f) return "";
    const m = f.market || {}, src = m.sources || {}, own = src[d.symbol];
    const other = Object.keys(src).filter((s) => s !== d.symbol);
    const kinds = [...new Set(Object.values(src).map((p) => SOURCE[p.source] || p.source))];
    const rs = f.research, ev = f.events;
    const research = !rs || !rs.used ? "not used by these rules"
      : rs.source !== "SAVED_SNAPSHOT" ? "no saved research (unavailable — nothing was generated)"
        : `saved ${whenNY(rs.created_at)} · ${rs.existed_at_close ? "before the close" : "after the close (POST CLOSE)"}`;
    const evWord = (x, what) => (x.status === "COMPLETE" ? `${what}: every calendar loaded` : x.status === "PARTIAL"
      ? `${what}: incomplete${what === "stock events" && x.providers && x.providers.earnings && !x.providers.earnings.available ? " (no verified earnings calendar)" : ""}`
      : `${what}: unavailable`);
    const evs = !ev || !ev.used ? "not used by these rules" : [ev.stock && evWord(ev.stock, "stock events"), ev.market && evWord(ev.market, "macro calendar")].filter(Boolean).join(" · ");
    return `<details class="sf-fresh" data-ok="fresh"${openKeys.has("fresh") ? " open" : ""}><summary><span class="cc-label">DATA FRESHNESS</span>
        <span class="cc-small">daily bars through ${esc(day(d.decision_session || m.symbol_last_bar))} · ${esc(kinds.join(", ") || "—")}</span></summary>
      <div class="sf-kv cc-small">
        <div><span class="cc-label">MARKET DATA</span>${esc(d.symbol)}: last bar ${esc(day(m.symbol_last_bar))}${own ? ` (${esc(SOURCE[own.source] || own.source)})` : ""} · ${esc(String(m.feed || "").toUpperCase())} feed, split/dividend adjusted${other.length ? ` · context: ${esc(other.length)} symbol${other.length === 1 ? "" : "s"}` : ""}${m.fetched && m.fetched.requests ? ` · 1 read-only request (${esc(m.fetched.bars)} bars)` : " · no new request"}</div>
        <div><span class="cc-label">RESEARCH</span>${esc(research)}</div>
        <div><span class="cc-label">EVENTS</span>${esc(evs)}</div>
        <div><span class="cc-label">RULE</span>${esc(d.session_rule || "")}</div></div></details>`;
  }
  function drawSummary() {
    const el = part("summary");
    if (!el) return;
    const d = current;
    if (!d) { el.innerHTML = `<p class="cc-small cc-dimtext">${symbol ? `Evaluating ${esc(symbol)}…` : "Choose a stock to compare it with every saved strategy version's entry rules. Nothing is evaluated until you choose."}</p>`; return; }
    const c = d.counts || {}, ctx = CTX[d.context_timing];
    const q = quoteFor(d.symbol);
    const line = [["Rules met", c.RULES_MET], ["Rules not met", c.RULES_NOT_MET], ["Incomplete", c.INCOMPLETE_DATA], ["Outside universe", c.OUTSIDE_UNIVERSE],
      ["Not evaluated", c.not_evaluated]].map(([t, n]) => `<span>${esc(t)} <b>${esc(n || 0)}</b></span>`).join("");
    el.innerHTML = `<div class="sf-top">
        <div><span class="cc-label">SYMBOL</span><div class="sf-big">${esc(d.symbol)}</div></div>
        <div><span class="cc-label">DECISION SESSION</span><div class="sf-val">${d.decision_session ? `${esc(day(d.decision_session))} close` : "—"}</div>
          ${d.decision_session ? "" : `<div class="cc-small cc-dimtext">${esc(d.status === "NOTHING_TO_EVALUATE" ? "Not needed — no saved version includes this stock" : words(d.status))}</div>`}</div>
        <div><span class="cc-label">EVALUATED</span><div class="sf-val">${esc(whenNY(d.evaluated_at))}</div></div>
        <div><span class="cc-label">CONTEXT</span><div class="sf-val">${ctx ? tag(ctx[0], ctx[1]) : `<span class="cc-dimtext">—</span>`}</div></div>
        <div><span class="cc-label">SAVED VERSIONS CHECKED</span><div class="sf-val">${esc(c.checked || 0)}</div></div></div>
      <div class="sf-counts">${line}</div>
      ${d.context_text ? `<div class="cc-small cc-dimtext">${esc(d.context_text)}</div>` : ""}
      ${d.message ? `<div class="cc-small">${esc(d.message)}</div>` : ""}
      ${q ? `<div class="sf-quote cc-small">Current quote (dashboard): <b>${esc(money(q.price))}</b>${isNum(q.pct) ? ` ${esc(pct(q.pct))}` : ""} — <b>NOT used in Strategy Fit</b></div>` : ""}
      ${(d.warnings || []).length ? `<ul class="sf-warns">${d.warnings.map((w) => `<li>${esc(w.text)}</li>`).join("")}</ul>` : ""}
      ${freshness(d)}`;
  }
  function drawFilters() {
    const el = part("filters");
    if (!el) return;
    if (!current || !(current.strategies || []).length) { el.innerHTML = ""; return; }
    const n = (k) => (current.strategies || []).filter((s) => match(s, k)).length;
    el.innerHTML = `<div class="sf-filters" role="group" aria-label="Filter">${FILTERS.map(([k, t]) =>
      `<button type="button" data-sff="${k}" class="${filter === k ? "active" : ""}" aria-pressed="${filter === k}">${esc(t)} <span class="sf-n">${n(k)}</span></button>`).join("")}
      <span class="cc-small cc-dimtext">Sorted by strategy name, then version.</span></div>`;
  }
  const match = (s, k) => k === "ALL" || (k === "NOT_EVALUATED" ? NOT_EVALUATED.has(s.fit_status) : s.fit_status === k);

  function mark(result) {
    return result === "MET" ? `<span class="sf-mark cc-ok" title="met">✓</span>` : result === "NOT_MET" ? `<span class="sf-mark cc-bad" title="not met">✕</span>`
      : `<span class="sf-mark cc-warn" title="unavailable">⚠</span>`;
  }
  function leaf(t) {
    const actual = t.result === "MET" || t.result === "NOT_MET" ? `actual: ${fval(t.actual, t.unit, t.actual_label)}` : `unavailable${t.reason ? ` · ${words(t.reason)}` : ""}`;
    return `<li>${mark(t.result)} ${esc(t.text || t.feature)} <span class="cc-dimtext">— ${esc(actual)}</span></li>`;
  }
  function conds(trace) {
    return (trace || []).map((t) => (t.children ? `<li><span class="sf-mark cc-dim">◦</span> <b>${esc(t.group)}</b> of ${esc(t.children.length)} → ${esc(words(t.result))}<ul>${conds(t.children)}</ul></li>` : leaf(t))).join("");
  }
  function traceRows(trace, depth) {
    return (trace || []).map((t) => (t.children
      ? `<tr class="sf-grp"><td colspan="4" style="padding-left:${6 + depth * 14}px"><b>${esc(t.group)}</b> group of ${esc(t.children.length)} → ${esc(words(t.result))}</td></tr>${traceRows(t.children, depth + 1)}`
      : `<tr><td style="padding-left:${6 + depth * 14}px">${mark(t.result)} ${esc(t.text || t.name || t.feature)}</td>
         <td>${esc(t.result === "MET" || t.result === "NOT_MET" ? fval(t.actual, t.unit, t.actual_label) : "—")}</td><td>${esc(words(t.result))}</td>
         <td>${esc(words(t.availability))}${t.reason ? `<div class="cc-dimtext">${esc(words(t.reason))}</div>` : ""}</td></tr>`)).join("");
  }
  function featureList(features) {                       // compact form for a card (the full table is the diagnostic drawer)
    return `<ul class="sf-flist">${(features || []).map((f) => `<li><b>${esc(f.name)}</b> ${esc(fval(f.value, f.unit, f.value_label))}
      <span class="cc-dimtext">· ${esc(words(f.availability))}${f.reason ? ` (${esc(words(f.reason))})` : ""} · ${esc(words(f.timing || "—"))}${f.source_timestamp ? ` ${esc(whenNY(f.source_timestamp))}` : ""}</span>
      <div class="cc-small cc-dimtext">${esc(f.feature_id)} · ${esc(f.source || "—")}</div></li>`).join("")}</ul>`;
  }
  function featureTable(features) {
    if (!features || !features.length) return "";
    return `<div class="sf-tablewrap"><table class="data-table sf-feat"><tr><th>Feature</th><th>Value</th><th>Availability</th><th>Source</th><th>Timing</th></tr>
      ${features.map((f) => `<tr><td><b>${esc(f.name)}</b><div class="cc-small cc-dimtext">${esc(f.feature_id)}</div></td><td>${esc(fval(f.value, f.unit, f.value_label))}</td>
        <td>${esc(words(f.availability))}${f.reason ? `<div class="cc-small cc-dimtext">${esc(words(f.reason))}</div>` : ""}</td><td class="cc-small">${esc(f.source || "—")}</td>
        <td class="cc-small">${esc(words(f.timing || "—"))}${f.source_timestamp ? `<div class="cc-dimtext">${esc(whenNY(f.source_timestamp))}</div>` : ""}</td></tr>`).join("")}</table></div>`;
  }
  const drawer = (key, head, body, cls) => `<details class="sf-drawer${cls ? ` ${cls}` : ""}" data-ok="${esc(key)}"${openKeys.has(key) ? " open" : ""}><summary>${head}</summary><div class="sf-drawer-body">${body}</div></details>`;

  function runLine(x, first) {
    const m = x.metrics || {};
    const done = x.status === "COMPLETED";
    return `<li class="sf-run"><div>${first ? `<b>Most recent stored run</b> · ` : ""}${esc(day(x.period.start))} → ${esc(day(x.period.end))} ${done ? "" : tag(words(x.status), "dim")}</div>
      <div class="cc-small">${done ? `${esc(m.closed_trades ?? "—")} closed trades · total return ${esc(pct(m.total_return_pct))} · max drawdown ${esc(pct(m.max_drawdown_pct))}` : esc(x.error_code ? words(x.error_code) : "no results")}</div>
      <div class="cc-small cc-dimtext">Initial ${esc(money(x.initial_equity))} · slippage ${esc(val(x.slippage_bps_per_side))} bps/side · commission ${esc(money(x.commission_per_order))} · data ${esc(short(x.data_hash))} · stored ${esc(whenNY(x.created_at))}${x.spec_hash_matches === false ? " · ⚠ spec hash differs" : ""}</div></li>`;
  }
  function historical(s) {
    const h = s.historical_evidence;
    if (!h) return "";
    const recent = (h.runs || []).find((x) => x.status === "COMPLETED");
    const none = s.readiness === "BACKTEST_READY" ? "No stored backtest" : "Unavailable for these rules (forward test only)";
    const head = `<span class="cc-label">HISTORICAL EVIDENCE</span> <span class="cc-small">${h.count ? `${esc(h.count)} stored backtest${h.count === 1 ? "" : "s"}${h.completed !== h.count ? ` (${esc(h.completed)} completed)` : ""}` : none}</span>`;
    const body = h.count ? `<ul class="sf-runs">${recent ? runLine(recent, true) : ""}${h.runs.filter((x) => x !== recent).map((x) => runLine(x, false)).join("")}</ul>
        <div class="cc-small cc-dimtext">${esc(h.note)}</div>`
      : `<div class="cc-small">${s.readiness === "BACKTEST_READY" ? "This version has no stored backtest." : "Historical backtests are unavailable for these rules (FORWARD TEST ONLY)."} No evidence is not the same as a poor fit.</div>`;
    const btn = s.readiness === "BACKTEST_READY" ? `<button type="button" class="cc-btn cc-mini" data-sf-open="backtest" data-sid="${esc(s.strategy_id)}" data-n="${esc(s.version)}">Open backtest history</button>` : "";
    return drawer(`${s.strategy_version_id}|hist`, head, body + btn, "sf-ev");
  }
  function forward(s, sym) {
    const f = s.forward_evidence;
    if (!f) return "";
    const j = f.journal, o = f.symbol;
    const head = `<span class="cc-label">FORWARD EVIDENCE</span> <span class="cc-small">${j ? `Journal ${esc(words(j.status).toLowerCase())} · ${esc(j.sessions_captured)} session${j.sessions_captured === 1 ? "" : "s"} captured` : "No forward journal"}</span>`;
    const body = j ? `<div class="cc-small">${tag(words(j.status), j.status === "ACTIVE" ? "ok" : j.status === "ARCHIVED" ? "dim" : "alert")} ${tag(words(j.continuity), j.continuity === "CONTINUOUS" ? "ok" : j.continuity === "GAPPED" ? "warn" : "dim")}
          ${esc(j.sessions_captured)} captured · ${esc(j.sessions_missed)} missed · latest ${esc(day(j.latest_captured_session))} · started ${esc(day(j.forward_start_date))}${f.journals > 1 ? ` · ${esc(f.journals)} journals (${esc(f.archived_journals)} archived)` : ""}</div>
        ${o ? `<div class="cc-small"><b>${esc(sym)}</b> · latest stored decision: <b>${esc(DECISION_TEXT(o))}</b> (${esc(day(o.latest_session))}) · shadow state ${esc(STATE_TEXT[o.state_after] || words(o.state_after))} · completed reference cycles ${esc(o.completed_reference_cycles)}</div>`
          : `<div class="cc-small cc-dimtext">${j.universe_includes_symbol ? "No stored observation for this stock yet." : "This journal's universe does not include this stock."}</div>`}
        <div class="cc-small cc-dimtext">${esc(f.note)}</div>`
      : `<div class="cc-small">This version has no forward journal. No evidence is not the same as a poor fit.</div>`;
    const btn = `<button type="button" class="cc-btn cc-mini" data-sf-open="forward" data-sid="${esc(s.strategy_id)}" data-n="${esc(s.version)}">${j ? "Open journal" : "Open forward journal"}</button>`;
    return drawer(`${s.strategy_version_id}|fwd`, head, body + btn, "sf-ev");
  }
  function journalState(s, sym) {
    const o = s.forward_evidence && s.forward_evidence.symbol;
    if (!o || !o.open_cycle) return "";
    const c = o.open_cycle;
    return `<div class="sf-jstate"><span class="cc-label">FORWARD JOURNAL STATE · ${esc(sym)}</span>
      <div class="cc-small">${tag(STATE_TEXT[o.state_after] || words(o.state_after), "info")} since ${esc(day(c.entry_signal_session))}${isNum(c.reference_entry_open) ? ` · reference entry ${esc(money(c.reference_entry_open))}` : ""}${isNum(c.holding_sessions) && c.holding_sessions ? ` · ${esc(c.holding_sessions)} session${c.holding_sessions === 1 ? "" : "s"} held` : ""}</div>
      <div class="cc-small cc-dimtext">Stored Stage 3.3 lifecycle — separate from the entry fit above. No order was placed.</div>
      <button type="button" class="cc-btn cc-mini" data-sf-open="forward" data-sid="${esc(s.strategy_id)}" data-n="${esc(s.version)}">Open journal</button></div>`;
  }
  function card(s, sym) {
    const st = STATUS[s.fit_status] || [words(s.fit_status), "dim"];
    const head = `<div class="sf-card-head"><b class="sf-name">${esc(s.strategy_name)}</b><span class="sf-ver">v${esc(s.version)}${s.is_current ? " · current" : " · older version"}</span>${tag(st[0], st[1])}</div>
      ${s.version_name && s.version_name !== s.strategy_name ? `<div class="cc-small cc-dimtext">Saved as “${esc(s.version_name)}”</div>` : ""}`;
    const kind = s.fit_status === "RULES_MET" ? "info" : s.fit_status === "INCOMPLETE_DATA" ? "warn" : st[1] === "bad" ? "bad" : st[1] === "alert" ? "alert" : "dim";
    if (s.fit_status === "OUTSIDE_UNIVERSE") {
      return `<article class="sf-card sf-k-dim sf-compact">${head}<div class="cc-small">${esc(sym)} is not in this version's saved universe${s.universe ? ` (${esc(s.universe.slice(0, 10).join(", "))}${s.universe.length > 10 ? ` +${s.universe.length - 10}` : ""})` : ""}. Its rules are not evaluated for ${esc(sym)}.</div>${axSlot(s)}</article>`;
    }
    if (!EVALUATED.has(s.fit_status) && !["STALE_DATA", "DATA_UNAVAILABLE"].includes(s.fit_status)) {
      const e = (s.eligibility && s.eligibility.errors) || [];
      return `<article class="sf-card sf-k-${kind} sf-compact">${head}<div class="cc-small">${esc(s.status_text)}</div>
        ${e.length > 1 ? `<ul class="sf-warns">${e.slice(1).map((x) => `<li>${esc(x.message)}</li>`).join("")}</ul>` : ""}
        <div class="cc-small cc-dimtext">This version is not evaluated. Nothing else is substituted for it.</div>${axSlot(s)}</article>`;
    }
    const counts = EVALUATED.has(s.fit_status) ? `<div class="sf-count"><b>${esc(s.conditions_met)} / ${esc(s.conditions_total)}</b> condition${s.conditions_total === 1 ? "" : "s"} met${s.conditions_unavailable ? ` · <b>${esc(s.conditions_unavailable)}</b> unavailable` : ""} <span class="cc-small cc-dimtext">(descriptive count)</span></div>
      <div class="cc-small">Overall entry rule (${esc(s.logic)}): <b>${esc(s.fit_status === "INCOMPLETE_DATA" ? "cannot be decided" : s.group_result === "MET" ? "met" : "not met")}</b> ${ctxTag(s.context_timing)}</div>` : "";
    const unav = (s.unavailable || []).length ? `<ul class="sf-warns">${s.unavailable.map((u) => `<li><b>${esc(u.name || u.feature)} unavailable</b> — ${esc(u.reason_text)}</li>`).join("")}</ul>` : "";
    const warns = (s.warnings || []).filter((w) => w.code !== "POST_CLOSE_CONTEXT").map((w) => `<li>${esc(w.text)}</li>`).join("");
    const details = EVALUATED.has(s.fit_status) ? drawer(`${s.strategy_version_id}|trace`, "Rule details", `<div class="cc-small">Entry rule: ${esc(s.entry_text || "")}</div>
        <div class="sf-tablewrap"><table class="data-table sf-trace"><tr><th>Condition</th><th>Actual</th><th>Result</th><th>Availability</th></tr>${traceRows(s.trace, 0)}</table></div>
        <div class="cc-label">CURRENT SNAPSHOT — features these rules use</div>${featureList(s.snapshot && s.snapshot.features)}
        <div class="cc-small cc-dimtext">Integrity verified (spec ${esc(short(s.spec_hash))} · rules ${esc(short(s.rules_hash))}). Evaluated with the saved rules' own logic; the count above does not decide the result.</div>`) : "";
    const exit = drawer(`${s.strategy_version_id}|exit`, "Exit plan", `<ul class="sf-plain">${(s.exit_plan || []).map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
      <div class="cc-small cc-dimtext">Shown for reference — exit rules are not evaluated for a stock that is only being examined.</div>`);
    const risk = s.risk ? `<div class="cc-small cc-dimtext">Risk definition (read-only, not part of the fit): max position ${esc(val(s.risk.max_position_pct))}% · max open positions ${esc(val(s.risk.max_open_positions))}</div>` : "";
    return `<article class="sf-card sf-k-${kind}">${head}${counts}
      ${EVALUATED.has(s.fit_status) ? `<ul class="sf-conds">${conds(s.trace)}</ul>` : ""}
      <p class="sf-sum">${esc(s.status_text)}</p>${unav}${warns ? `<ul class="sf-warns">${warns}</ul>` : ""}
      ${details}<div class="sf-evidence">${historical(s)}${forward(s, sym)}</div>${journalState(s, sym)}${exit}${risk}${axSlot(s)}</article>`;
  }
  // Stage 3.8: the explicit "Explain this setup" slot (ai_explain.js); it explains this exact result and never re-evaluates
  const axSlot = (s) => (window.AIExplain && current && current.evaluated_at ? window.AIExplain.fitSlot(s, current) : "");
  const ctxTag = (c) => (CTX[c] ? tag(CTX[c][0], CTX[c][1]) : "");
  function drawCards() {
    const el = part("cards");
    if (!el) return;
    if (!current) { el.innerHTML = ""; return; }
    const list = (current.strategies || []).filter((s) => match(s, filter));
    el.innerHTML = list.length ? list.map((s) => card(s, current.symbol)).join("")
      : `<p class="cc-small cc-dimtext sf-empty">${(current.strategies || []).length ? "No saved version matches this filter." : "No saved strategies yet — build and save one in the Builder."}</p>`;
  }
  function drawEnv() {
    const el = part("env");
    if (!el) return;
    const e = current && current.environment;
    el.innerHTML = e && e.features && e.features.length ? drawer("env", `<span class="cc-label">FULL DIAGNOSTIC SNAPSHOT</span> <span class="cc-small cc-dimtext">every value computed for ${esc(current.symbol)} at the ${esc(day(current.decision_session))} close (${esc(e.features.length)})</span>`,
      featureTable(e.features), "cc-card") : "";
  }

  // ---- navigation to the stored evidence (Builder view, the exact version, its panel) -----------------------------------
  function openIn(panel, sid, n) {
    setView("builder");
    if (window.StrategyLab && window.StrategyLab.openVersion) window.StrategyLab.openVersion(sid, n, panel);
  }
  // Dashboard / watchlist stock views: open Strategy Lab → Strategy Fit with the symbol chosen (one shared view)
  // Stage 4.0: the Scanner opens a stock here and may ask to show one exact version's card (focus = { versionId, old })
  let focus = null;
  function openFor(sym, want) {
    const t = String(sym || "").trim().toUpperCase();
    if (!SYMBOL_RE.test(t)) return;
    const btn = document.querySelector('.tab-btn[data-tab="strategy"]');
    if (btn && !tabEl.classList.contains("active")) btn.click();
    setView("fit");
    focus = want && want.versionId ? { sym: t, vid: want.versionId } : null;
    if (focus) {
      filter = "ALL";
      if (want.old && !includeOld) { includeOld = true; const cb = root.querySelector('[data-sf="old"]'); if (cb) cb.checked = true; }
    }
    evaluate(t);
    if (root.scrollIntoView) root.scrollIntoView({ block: "start" });
  }
  function showFocus() {
    if (!focus || !current || current.symbol !== focus.sym) return;
    const slot = root.querySelector(`[data-ax-slot^="fit|${focus.vid}|"]`);
    const card = slot && slot.closest(".sf-card");
    if (!busy) focus = null;                     // a remembered result is drawn first; keep the focus for the fresh one
    if (!card) return;
    root.querySelectorAll(".sf-card.sf-focus").forEach((c) => c.classList.remove("sf-focus"));
    card.classList.add("sf-focus");
    card.scrollIntoView({ block: "start" });
  }

  drawNav();
  window.StrategyFit = { openFor, setView, addWorkspace, get state() { return { view, symbol, includeOld, filter, busy, stale, notice, requests, cached: results.size, shown: current && current.symbol, open: [...openKeys] }; } };
})();
