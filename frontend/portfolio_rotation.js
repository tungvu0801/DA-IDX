// portfolio_rotation.js — Stage 4.7 PORTFOLIO ROTATION (a Strategy Lab workspace). PROPOSAL ONLY — NO ORDERS ARE SENT.
//
// Load a portfolio snapshot explicitly (Robinhood read only, the Alpaca paper view, or the local simulator), pick an
// immutable configuration version and an explicit universe, press Run Rotation, and read the deterministic proposal:
// current vs target weights, actions and factor details.
// Phase 5 handoff (CLOSED source rule): only an ALPACA PAPER run's eligible items offer "Prepare Paper Order", which
// fills the EXISTING Stage 4.6B "Alpaca Paper — Manual Orders" form (symbol, side, whole shares — the server-provided
// draft, nothing recomputed here) and nothing more: the user still clicks Preview and then Confirm there. Robinhood and
// local-simulator runs are DISPLAY ONLY — no draft, no prefill, no write path. No timers, no polling, nothing on page
// load, no request to any Alpaca endpoint from this file; every value is escaped.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("prt-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const pct = (w) => (w == null ? "—" : `${(Number(w) * 100).toFixed(2)}%`);
  const usd = (s) => (s == null ? "—" : `$${Number(s).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
  const num = (s, d = 4) => (s == null ? "—" : Number(s).toFixed(d));
  const whenNY = (v) => (v ? `${new Date(v).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" })} ET` : "—");
  const BASE = "/api/portfolio-rotation";
  const ALPACA = "ALPACA_PAPER_VIEW", ROBINHOOD = "ROBINHOOD_READ_ONLY", LOCAL = "LOCAL_SIMULATOR";
  const PREFILL = "ALPACA_PAPER_PREFILL";
  const BANNER = "PORTFOLIO ROTATION — PROPOSAL ONLY — NO ORDERS ARE SENT";
  const RH_TITLE = "ROBINHOOD PORTFOLIO — READ ONLY";
  const RH_NOTE = "This proposal is based on your Robinhood holdings. No Robinhood orders will be sent.";
  const RH_EXPL = "Robinhood portfolio is read-only. No quantity is transferred to Alpaca.";
  const AL_TITLE = "ALPACA PAPER PORTFOLIO";
  const AL_NOTE = "Prepare Paper Order fills the existing Alpaca Paper — Manual Orders form; you review it there, then Preview and Confirm. Nothing is sent by this page.";
  const LS_TITLE = "LOCAL SIMULATOR";
  const LS_NOTE = "Proposal only — the local simulator is never traded from here.";
  const LS_EXPL = "Local simulator results cannot create a broker draft.";
  const PREPARE = "Prepare Paper Order", RH_CTRL = "Paper handoff unavailable", LS_CTRL = "Display only";
  const LOAD = { [ROBINHOOD]: "Load Robinhood Snapshot", [ALPACA]: "Read Alpaca Paper snapshot", [LOCAL]: "Read Local Simulator" };
  const SRC_LABEL = { [ALPACA]: "ALPACA PAPER", [ROBINHOOD]: "ROBINHOOD — READ ONLY", [LOCAL]: "LOCAL SIMULATOR" };
  const KIND = { VALID: "ok", NO_ELIGIBLE_CANDIDATES: "warn", INSUFFICIENT_CANDIDATES: "warn", TURNOVER_LIMIT_EXCEEDED: "alert", DATA_STALE: "alert", INPUT_ERROR: "alert" };
  const AKIND = { ADD: "info", INCREASE: "info", HOLD: "dim", DECREASE: "warn", EXIT: "alert", NONE: "dim" };
  const FACTORS = [["momentum", "Momentum", ["ret20", "ret60"]], ["trend", "Trend", ["trend50", "trend200"]], ["relative_strength", "Relative Strength", ["relative_strength"]],
    ["volatility", "Volatility", ["volatility"]], ["drawdown", "Drawdown", ["drawdown"]], ["liquidity", "Liquidity", ["liquidity"]]];

  let cfg = null, configs = [], snapshots = {}, source = ROBINHOOD, configId = "", universe = "WATCHLIST", ref = "", custom = "";
  let run = null, candidates = [], targets = [], items = [], detail = "", busy = "", notice = "", requests = 0, seq = 0, showForm = false;
  let lastHandoff = null, observer = null;
  let form = { name: "", momentum: "0.30", trend: "0.25", relative_strength: "0.25", volatility: "0.10", drawdown: "0", liquidity: "0.10", portfolio_size: "10",
    exit_rank: "15", cash_buffer_pct: "0.05", rebalance_threshold: "0.01", max_turnover_per_rotation: "0.50", max_position_weight: "0.20",
    min_position_weight: "0.05", min_price: "5.00", min_avg_dollar_volume: "5000000.00", excluded_symbols: "" };

  const api = (method, url, body) => {
    requests++;
    return fetch(url, method === "GET" ? {} : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }))
      .catch(() => ({ status: 0, body: { message: "The server did not answer." } }));
  };

  async function load() {
    const my = ++seq;
    const c = await api("GET", `${BASE}/config`);
    const l = await api("GET", `${BASE}/configs`);
    if (my !== seq) return;
    if (c.status === 200) { cfg = c.body; snapshots = cfg.snapshots || {}; }
    if (l.status === 200) { configs = l.body.configs || []; if (!configId && configs.length) configId = configs[configs.length - 1].config_id; }
    draw();
  }
  async function act(kind, url, body, after) {
    if (busy) return null;                                     // one action at a time: a double click sends one request
    busy = kind; notice = ""; draw();
    const r = await api("POST", url, body);
    busy = "";
    if (r.status === 200 || r.status === 201) after(r.body);
    else notice = `${r.body.status ? `${r.body.status}: ` : ""}${r.body.message || "The request could not be completed."}`;
    draw();
    return r;
  }
  const snap = () => snapshots[source] || null;
  const config = () => configs.find((x) => x.config_id === configId) || null;
  const maxAge = () => { const c = config(); const v = c && c.config ? Number(c.config.max_snapshot_age_min) : NaN; return Number.isFinite(v) ? v : (snap() && snap().fresh_for_min) || 30; };
  const fresh = (s) => !!(s && s.status === "OK" && s.age_min != null && Number(s.age_min) <= maxAge());
  const customSymbols = () => [...new Set(String(custom || "").split(/[\s,;]+/).filter(Boolean).map((s) => s.trim().toUpperCase()))].sort();
  function universeCount() {
    if (!cfg) return null;
    if (universe === "CUSTOM") return customSymbols().length;
    if (universe === "WATCHLIST") return cfg.watchlist_count;
    const list = universe === "SAVED_SCAN" ? cfg.saved_scans : cfg.saved_versions;
    const pick = (list || []).find((x) => (x.saved_scan_id || x.strategy_version_id) === ref);
    return pick ? pick.n_symbols : null;
  }
  const runReady = () => !!(fresh(snap()) && config() && (universe !== "CUSTOM" || customSymbols().length) && (!["SAVED_SCAN", "SAVED_UNIVERSE"].includes(universe) || ref));
  const clearRun = () => { run = null; candidates = []; targets = []; items = []; detail = ""; };

  function readForm() {
    root.querySelectorAll("[data-prt-f]").forEach((el) => { form[el.dataset.prtF] = el.value; });
    const u = root.querySelector('[data-prt="universe"]'); if (u) universe = u.value;
    const rf = root.querySelector('[data-prt="ref"]'); if (rf) ref = rf.value;
    const cu = root.querySelector('[data-prt="custom"]'); if (cu) custom = cu.value;
    const cs = root.querySelector('[data-prt="config"]'); if (cs) configId = cs.value;
  }
  function onClick(e) {
    const b = e.target.closest("[data-prt-act]");
    if (!b || b.disabled || busy) return;
    readForm();
    const a = b.dataset.prtAct;
    if (a === "source") { source = b.dataset.src; clearRun(); notice = ""; draw(); }    // a new source: any earlier proposal (and its handoff) is gone
    else if (a === "snapshot") act("snapshot", `${BASE}/snapshot`, { source }, (x) => { snapshots[source] = x.snapshot; });
    else if (a === "run") {
      if (!runReady()) return;
      const body = { config_id: configId, config_hash: config().config_hash, portfolio_source: source,
        universe: { source: universe, ref: ["SAVED_SCAN", "SAVED_UNIVERSE"].includes(universe) ? ref : null, symbols: universe === "CUSTOM" ? customSymbols() : null } };
      clearRun();
      act("run", `${BASE}/run`, body, (x) => { run = x.run; }).then((r) => { if (r && r.status === 200) loadRun(); });
    } else if (a === "detail") { detail = detail === b.dataset.sym ? "" : b.dataset.sym; draw(); }
    else if (a === "handoff") handoff(b.dataset.sym);
    else if (a === "form") { showForm = !showForm; draw(); }
    else if (a === "save-config") {
      const w = (k) => form[k];
      const body = { name: form.name, weights: { momentum: w("momentum"), trend: w("trend"), relative_strength: w("relative_strength"), volatility: w("volatility"),
        drawdown: w("drawdown"), liquidity: w("liquidity") }, portfolio_size: Number(form.portfolio_size), exit_rank: Number(form.exit_rank),
        cash_buffer_pct: form.cash_buffer_pct, rebalance_threshold: form.rebalance_threshold, max_turnover_per_rotation: form.max_turnover_per_rotation,
        max_position_weight: form.max_position_weight, min_position_weight: form.min_position_weight, min_price: form.min_price,
        min_avg_dollar_volume: form.min_avg_dollar_volume, excluded_symbols: String(form.excluded_symbols || "").split(/[\s,;]+/).filter(Boolean) };
      act("config", `${BASE}/configs`, body, (x) => { configId = x.config_id; showForm = false; }).then(() => load());
    }
  }
  async function loadRun() {
    if (!run) return;
    const my = ++seq;
    const [c, t, r] = await Promise.all([api("GET", `${BASE}/runs/${encodeURIComponent(run.run_id)}/candidates`),
      api("GET", `${BASE}/runs/${encodeURIComponent(run.run_id)}/targets`), api("GET", `${BASE}/runs/${encodeURIComponent(run.run_id)}/rebalance`)]);
    if (my !== seq) return;
    candidates = c.status === 200 ? c.body.candidates : []; targets = t.status === 200 ? t.body.targets : []; items = r.status === 200 ? r.body.items : [];
    draw();
  }

  // ---- Phase 5 handoff: the server's draft, the existing Stage 4.6B form, nothing else ------------------------------------------
  function eligibleDraft(sym) {
    // every check is against the SERVER's run and item data; no DOM attribute can grant eligibility
    if (!run || run.portfolio_source !== ALPACA || run.handoff_mode !== PREFILL || run.status !== "VALID") return null;
    const it = items.find((i) => i.symbol === sym);
    const h = it && it.handoff;
    if (!h || h.mode !== PREFILL || h.eligible !== true || !h.draft) return null;
    const d = h.draft;
    if (d.symbol !== it.symbol || !["BUY", "SELL"].includes(d.side) || !Number.isInteger(d.quantity) || d.quantity < 1) return null;
    if ((d.side === "BUY") !== ["ADD", "INCREASE"].includes(it.action) || (d.side === "SELL") !== ["DECREASE", "EXIT"].includes(it.action)) return null;
    return { symbol: d.symbol, side: d.side, quantity: d.quantity };
  }
  function handoff(sym) {
    const d = eligibleDraft(sym);
    if (!d) {
      const label = run ? SRC_LABEL[run.portfolio_source] || run.portfolio_source : "no run";
      notice = run && run.portfolio_source === ROBINHOOD ? `${RH_CTRL}: ${RH_EXPL}` : run && run.portfolio_source === LOCAL ? `${LS_CTRL}: ${LS_EXPL}`
        : `No paper handoff for ${sym} (${label}).`;
      draw();
      return;
    }
    const ppf = document.getElementById("ppf-body");
    const ok = window.StrategyFit && window.StrategyFit.setView && window.AlpacaOrders && ppf;
    if (!ok) { notice = "The Alpaca Paper — Manual Orders view is not available on this page."; draw(); return; }
    if (observer) { observer.disconnect(); observer = null; }
    const r0 = Number(window.AlpacaOrders.state.requests) || 0;
    lastHandoff = { symbol: d.symbol, side: d.side, quantity: d.quantity, placed: false, at: null };
    window.StrategyFit.setView("paper");                       // the Paper Portfolio workspace ...
    const view = ppf.querySelector('[data-ppf-view="orders"]');
    if (!view) { notice = "The Alpaca Paper — Manual Orders view is not available."; draw(); return; }
    view.click();                                              // ... and its Manual Orders view (0 broker requests to open)
    const pane = ppf.querySelector('[data-ppfp="orders"]');
    const fill = () => {
      if (!pane) return false;
      const q = (k) => pane.querySelector(`[data-apo-f="${k}"]`);
      if (!q("symbol") || !q("side") || !q("quantity")) return false;      // the form exists only when linked + turned on
      q("symbol").value = d.symbol; q("side").value = d.side; q("quantity").value = String(d.quantity);
      lastHandoff.placed = true; lastHandoff.at = new Date().toISOString();
      return true;
    };
    const settled = () => Number(window.AlpacaOrders.state.requests) >= r0 + 2;   // its own settings + list reload finished
    if (settled() && fill()) return;
    observer = new MutationObserver(() => { if (settled() && fill()) { observer.disconnect(); observer = null; } });
    observer.observe(pane || ppf, { childList: true, subtree: true });
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------------
  function sourceCard() {
    const s = snap();
    const d = busy ? " disabled" : "";
    const pills = (cfg ? cfg.portfolio_sources : []).map((p) => `<button type="button" class="cc-btn cc-mini${p.id === source ? " cc-primary" : ""}" data-prt-act="source" data-src="${esc(p.id)}" aria-pressed="${p.id === source}"${d}>${esc(p.label)}</button>`).join("");
    const state = !s ? `<span class="cc-dimtext">No snapshot loaded yet.</span>`
      : `${fresh(s) ? tag("Fresh", "ok") : tag("Stale", "alert")} snapshot ${esc(whenNY(s.snapshot_at))}${s.age_min != null ? ` (${esc(s.age_min)} min)` : ""} · cash <b>${esc(usd(s.cash))}</b> · ${esc(s.n_positions)} position${s.n_positions === 1 ? "" : "s"}${s.status === "STALE" ? ` · ${tag("source reports STALE", "alert")}` : ""}`;
    const held = s ? s.positions.filter((p) => (p.flags || []).includes("RH_SHARES_HELD")).map((p) => p.symbol) : [];
    return `<section class="cc-card prt-source"><div class="cc-head"><h2>PORTFOLIO SOURCE</h2>${tag(SRC_LABEL[source] || source, source === ROBINHOOD ? "alert" : "info")}</div>
      <div class="prt-pills">${pills}</div>
      <div class="prt-row cc-small"><button type="button" class="cc-btn cc-mini" data-prt-act="snapshot"${d}>${busy === "snapshot" ? "Loading…" : esc(LOAD[source] || "Load snapshot")}</button> ${state}</div>
      ${held.length ? `<div class="cc-small cc-dimtext">Shares held / not sellable at Robinhood (information only): ${esc(held.join(", "))}</div>` : ""}
      ${s && s.info && s.info.account_alias ? `<div class="cc-small cc-dimtext">Account alias ${esc(s.info.account_alias)} · gateway ${esc(JSON.stringify(s.info.gateway_status || {}))}</div>` : ""}
      ${s && s.info && s.info.account_number_masked ? `<div class="cc-small cc-dimtext">Alpaca paper account ${esc(s.info.account_number_masked)}</div>` : ""}</section>`;
  }
  function controls() {
    const d = busy ? " disabled" : "";
    const opts = configs.map((c) => `<option value="${esc(c.config_id)}"${c.config_id === configId ? " selected" : ""}>${esc(c.name)} v${esc(c.version)} · ${esc(c.config_hash.slice(0, 8))}</option>`).join("");
    const refs = universe === "SAVED_SCAN" ? (cfg ? cfg.saved_scans : []).map((x) => `<option value="${esc(x.saved_scan_id)}"${x.saved_scan_id === ref ? " selected" : ""}>${esc(x.name)} · ${esc(x.latest_session || "no check yet")} · ${esc(x.n_symbols == null ? "?" : x.n_symbols)} symbols</option>`).join("")
      : universe === "SAVED_UNIVERSE" ? (cfg ? cfg.saved_versions : []).map((x) => `<option value="${esc(x.strategy_version_id)}"${x.strategy_version_id === ref ? " selected" : ""}>${esc(x.strategy_name)} v${esc(x.version_number)} · ${esc(x.n_symbols == null ? "?" : x.n_symbols)} symbols</option>`).join("") : "";
    const n = universeCount();
    const count = n == null ? "symbol count unknown until the run" : `${n} symbol${n === 1 ? "" : "s"}${cfg && n > cfg.max_symbols ? ` — above the limit of ${cfg.max_symbols}` : ""}`;
    const c = config();
    const summary = c ? `size ${esc(c.config.portfolio_size)} · exit rank ${esc(c.config.exit_rank)} · cash buffer ${esc(pct(c.config.cash_buffer_pct))} · threshold ${esc(pct(c.config.rebalance_threshold))} · max turnover ${esc(pct(c.config.max_turnover_per_rotation))} · snapshot max age ${esc(c.config.max_snapshot_age_min)} min · weights ${esc(Object.entries(c.config.weights).filter(([, v]) => Number(v) > 0).map(([k, v]) => `${k} ${Number(v) * 100}%`).join(", "))}` : "no configuration yet — create one below";
    return `<section class="cc-card prt-controls"><div class="cc-head"><h2>RUN</h2></div>
      <div class="ppf-fields">
        <label class="sf-field"><span class="cc-label">CONFIGURATION</span><select data-prt="config"${d}>${opts || '<option value="">(none)</option>'}</select></label>
        <label class="sf-field"><span class="cc-label">UNIVERSE</span><select data-prt="universe"${d}>${(cfg ? cfg.universe_sources : ["WATCHLIST", "SAVED_SCAN", "SAVED_UNIVERSE", "CUSTOM"]).map((u) => `<option value="${esc(u)}"${u === universe ? " selected" : ""}>${esc(u)}</option>`).join("")}</select></label>
        ${refs !== "" || ["SAVED_SCAN", "SAVED_UNIVERSE"].includes(universe) ? `<label class="sf-field"><span class="cc-label">REFERENCE</span><select data-prt="ref"${d}>${refs || '<option value="">(none available)</option>'}</select></label>` : ""}
        ${universe === "CUSTOM" ? `<label class="sf-field prt-custom"><span class="cc-label">TICKERS</span><input data-prt="custom" value="${esc(custom)}" placeholder="AMD, MU, KO" autocomplete="off" spellcheck="false"></label>` : ""}
        <button type="button" class="cc-btn cc-mini cc-primary" data-prt-act="run"${runReady() && !busy ? "" : " disabled"}>${busy === "run" ? "Running…" : "Run Rotation"}</button></div>
      <div class="cc-small cc-dimtext">${esc(count)} · benchmark SPY · ${esc(summary)}</div>
      ${!fresh(snap()) ? `<div class="cc-small prt-why">Load a fresh snapshot for this source first — a run never refreshes one on its own.</div>` : ""}
      <div class="prt-row"><button type="button" class="cc-btn cc-mini cc-link" data-prt-act="form"${d}>${showForm ? "Hide configuration form" : "New configuration version…"}</button></div>
      ${showForm ? configForm() : ""}</section>`;
  }
  function configForm() {
    const f = (k, label, w = "") => `<label class="sf-field${w}"><span class="cc-label">${esc(label)}</span><input data-prt-f="${esc(k)}" value="${esc(form[k])}" autocomplete="off" spellcheck="false"></label>`;
    return `<div class="prt-form ppf-fields">${f("name", "NAME")}${f("momentum", "MOMENTUM")}${f("trend", "TREND")}${f("relative_strength", "REL. STRENGTH")}${f("volatility", "VOLATILITY")}${f("drawdown", "DRAWDOWN")}${f("liquidity", "LIQUIDITY")}
      ${f("portfolio_size", "PORTFOLIO SIZE")}${f("exit_rank", "EXIT RANK")}${f("cash_buffer_pct", "CASH BUFFER")}${f("rebalance_threshold", "REBALANCE THRESHOLD")}${f("max_turnover_per_rotation", "MAX TURNOVER")}
      ${f("max_position_weight", "MAX POSITION WT")}${f("min_position_weight", "MIN POSITION WT")}${f("min_price", "MIN PRICE")}${f("min_avg_dollar_volume", "MIN AVG $ VOLUME")}${f("excluded_symbols", "EXCLUDED SYMBOLS", " prt-custom")}
      <button type="button" class="cc-btn cc-mini" data-prt-act="save-config"${busy ? " disabled" : ""}>Save immutable configuration version</button>
      <div class="cc-small cc-dimtext">Weights are decimal strings summing to exactly 1.000000; benchmark is fixed to SPY; a saved version never changes.</div></div>`;
  }
  function sourceNotice() {
    if (source === ROBINHOOD) return `<div class="prt-notice prt-rh"><b>${esc(RH_TITLE)}</b> — ${esc(RH_NOTE)} ${esc(RH_EXPL)}</div>`;
    if (source === ALPACA) return `<div class="prt-notice prt-al"><b>${esc(AL_TITLE)}</b> — ${esc(AL_NOTE)}</div>`;
    return `<div class="prt-notice"><b>${esc(LS_TITLE)}</b> — ${esc(LS_NOTE)} ${esc(LS_EXPL)}</div>`;
  }
  function summary() {
    if (!run) return "";
    const r = run;
    const kv = (k, v) => `<div><span class="cc-label">${esc(k)}</span> ${v}</div>`;
    return `<section class="cc-card prt-summary" data-prt-run="${esc(r.run_id)}" data-prt-status="${esc(r.status)}" data-prt-source="${esc(r.portfolio_source)}"><div class="cc-head"><h2>RESULT</h2>${tag(r.source_label || SRC_LABEL[r.portfolio_source] || r.portfolio_source, r.portfolio_source === ROBINHOOD ? "alert" : "info")} ${tag(r.status, KIND[r.status] || "dim")}</div>
      ${r.status_detail ? `<div class="cc-small prt-why">${esc(r.status_detail)}</div>` : ""}
      ${r.source_mismatch_note ? `<div class="cc-small cc-dimtext">${esc(r.source_mismatch_note)}</div>` : ""}
      <div class="prt-facts cc-small">${kv("SOURCE", esc(r.source_label || r.portfolio_source))}${kv("DECISION SESSION", esc(r.data_session || "—"))}${kv("BENCHMARK", esc(r.benchmark))}${kv("REFERENCE EQUITY", esc(usd(r.reference_equity)))}
        ${kv("CURRENT CASH", esc(pct(r.current_cash_weight)))}${kv("TARGET CASH", esc(pct(r.target_cash_weight)))}${kv("TURNOVER", esc(pct(r.turnover)))}
        ${kv("ELIGIBLE", `${esc(r.n_eligible)} of ${esc(r.n_universe)}`)}${kv("SELECTED", esc(r.n_selected))}${kv("MARKET DATA REQUESTS", esc(r.market_data_requests))}
        ${kv("INPUT HASH", `<code>${esc(String(r.input_hash || "").slice(0, 16))}…</code>`)}${kv("PROPOSAL HASH", `<code>${esc(String(r.proposal_hash || "").slice(0, 16))}…</code>`)}</div></section>`;
  }
  function handoffCell(it) {
    if (!it) return "";
    const h = it.handoff || {};
    const src = run ? run.portfolio_source : null;
    if (src === ROBINHOOD) return `<button type="button" class="cc-btn cc-mini" disabled title="${esc(RH_EXPL)}">${esc(RH_CTRL)}</button>`;
    if (src === LOCAL) return `<button type="button" class="cc-btn cc-mini" disabled title="${esc(LS_EXPL)}">${esc(LS_CTRL)}</button>`;
    const d = eligibleDraft(it.symbol);
    if (d) return `<button type="button" class="cc-btn cc-mini cc-primary prt-handoff" data-prt-act="handoff" data-sym="${esc(it.symbol)}" data-side="${esc(d.side)}" data-action="${esc(it.action)}" data-quantity="${esc(d.quantity)}"${busy ? " disabled" : ""}>${esc(PREPARE)}: ${esc(d.side)} ${esc(d.quantity)} ${esc(it.symbol)}</button>`;
    return `<span class="cc-small cc-dimtext">${esc(h.reason_text || "no handoff")}</span>`;
  }
  function table() {
    if (!run) return "";
    const byItem = Object.fromEntries(items.map((i) => [i.symbol, i]));
    const byTarget = Object.fromEntries(targets.map((t) => [t.symbol, t]));
    const symbols = [...new Set([...candidates.map((c) => c.symbol), ...items.map((i) => i.symbol)])];
    const rankOf = (s) => { const c = candidates.find((x) => x.symbol === s); return c && c.rank != null ? c.rank : null; };
    symbols.sort((a, b) => ((rankOf(a) ?? 1e9) - (rankOf(b) ?? 1e9)) || a.localeCompare(b));
    const rows = symbols.map((s) => {
      const c = candidates.find((x) => x.symbol === s) || {}; const i = byItem[s] || null; const t = byTarget[s] || {};
      const action = i ? i.action : "NONE";
      const reason = (i && i.reason) || (c.reasons && c.reasons.length ? c.reasons.join(", ") : (t.reason || ""));
      const delta = i ? (["ADD", "INCREASE"].includes(action) ? `+${i.est_qty_diff}` : ["DECREASE", "EXIT"].includes(action) ? `−${i.est_qty_diff}` : "0") : "";
      const shares = i ? `${esc(num(i.current_qty, 0))} → ${esc(t.est_target_qty == null ? (action === "EXIT" ? 0 : "—") : t.est_target_qty)} (${esc(delta)})` : "—";
      return `<tr data-prt-row="${esc(s)}" data-prt-action="${esc(action)}" class="${c.eligible === 0 ? "prt-ineligible" : ""}"><td>${esc(c.rank == null ? "—" : c.rank)}</td><td class="ppf-sym"><button type="button" class="cc-btn cc-mini cc-link" data-prt-act="detail" data-sym="${esc(s)}">${esc(s)}</button></td>
        <td>${esc(c.composite == null ? "—" : num(c.composite, 2))}</td><td>${esc(pct(i ? i.current_weight : "0"))}</td><td>${esc(pct(i ? i.target_weight : (t.target_weight ?? "0")))}</td><td class="cc-small">${shares}</td>
        <td>${tag(action, AKIND[action] || "dim")}</td><td class="cc-small">${esc(reason)}${(c.flags || []).length ? ` · ${esc((c.flags || []).join(", "))}` : ""}</td><td>${handoffCell(i)}</td></tr>`;
    }).join("");
    const src = run.portfolio_source;
    const foot = src === ALPACA ? AL_NOTE : src === ROBINHOOD ? `${RH_CTRL}. ${RH_EXPL}` : `${LS_CTRL}. ${LS_EXPL}`;
    return `<section class="cc-card prt-table" data-prt-handoff-mode="${esc(run.handoff_mode || "")}"><div class="cc-head"><h2>CURRENT vs TARGET</h2>${tag(run.source_label || SRC_LABEL[src] || src, src === ROBINHOOD ? "alert" : "info")}<span class="cc-small cc-dimtext">proposal actions only — nothing is sent</span></div>
      ${rows ? `<div class="sf-tablewrap"><table class="sf-table ppf-table"><thead><tr><th>Rank</th><th>Ticker</th><th>Composite</th><th>Current Wt</th><th>Target Wt</th><th>Shares (Δ)</th><th>Action</th><th>Reason</th><th>Paper handoff</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="cc-small cc-dimtext">No candidates were evaluated for this run.</p>`}
      <div class="cc-small prt-handoff-note">${esc(foot)}</div>
      ${lastHandoff ? `<div class="cc-small prt-last">${lastHandoff.placed ? `Draft placed in Alpaca Paper — Manual Orders: ${esc(lastHandoff.side)} ${esc(lastHandoff.quantity)} ${esc(lastHandoff.symbol)}. Review it there, then Preview and Confirm.` : `Preparing ${esc(lastHandoff.side)} ${esc(lastHandoff.quantity)} ${esc(lastHandoff.symbol)} — the Manual Orders form must be linked and turned on.`}</div>` : ""}
      ${detailCard()}</section>`;
  }
  function detailCard() {
    const c = candidates.find((x) => x.symbol === detail);
    if (!c) return "";
    const raw = c.raw || {}, sc = c.scores || {};
    const rows = FACTORS.map(([k, label, rawKeys]) => `<tr><td>${esc(label)}</td><td class="cc-small">${esc(rawKeys.map((rk) => `${rk} ${raw[rk] == null ? "—" : num(raw[rk], 6)}`).join(" · "))}</td><td>${esc(sc[k] == null ? "—" : num(sc[k], 2))}</td></tr>`).join("");
    return `<div class="prt-detail" data-prt-detail="${esc(c.symbol)}"><div class="cc-head"><h3>${esc(c.symbol)} · FACTOR DETAIL</h3>${c.eligible ? tag(`rank ${c.rank}`, "info") : tag("ineligible", "dim")}</div>
      <table class="sf-table"><thead><tr><th>Factor</th><th>Raw</th><th>Score (0–100)</th></tr></thead><tbody>${rows}
        <tr><td><b>Composite</b></td><td></td><td><b>${esc(c.composite == null ? "—" : num(c.composite, 2))}</b></td></tr></tbody></table>
      ${c.reasons && c.reasons.length ? `<div class="cc-small prt-why">Not eligible: ${esc(c.reasons.join(", "))}</div>` : ""}
      <div class="cc-small cc-dimtext">reference price ${esc(c.reference_price == null ? "—" : num(c.reference_price, 2))} · avg $ volume ${esc(c.avg_dollar_volume == null ? "—" : usd(c.avg_dollar_volume))} — deterministic rules, no AI</div></div>`;
  }
  function draw() {
    root.innerHTML = `<div class="prt">
      <div class="prt-banner" role="note">${esc(BANNER)}</div>
      ${sourceNotice()}${notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}
      ${sourceCard()}${controls()}${summary()}${table()}
      <p class="cc-small cc-dimtext ppf-foot">${esc(cfg ? cfg.note : "")}</p></div>`;
  }
  function show() {
    if (!root.dataset.prtBound) { root.dataset.prtBound = "1"; root.addEventListener("click", onClick); root.addEventListener("change", () => { readForm(); draw(); }); }
    draw();
    load();                                                    // configuration + versions: 0 broker requests, nothing runs
  }

  window.StrategyFit.addWorkspace({ id: "rotation", label: "Portfolio Rotation", cls: "prt-mode", show });
  window.PortfolioRotation = { get state() {
    return { source, configId, universe, ref, busy, notice, requests, snapshot: snap() ? { fresh: fresh(snap()), n: snap().n_positions } : null,
      run: run ? { id: run.run_id, status: run.status, source: run.portfolio_source, handoff_mode: run.handoff_mode } : null, rows: candidates.length, items: items.length,
      eligible: items.filter((i) => eligibleDraft(i.symbol)).map((i) => i.symbol), detail, ready: runReady(), lastHandoff }; } };
})();
