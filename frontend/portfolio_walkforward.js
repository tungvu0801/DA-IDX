// portfolio_walkforward.js — Stage 4.9 WALK-FORWARD + ROBUSTNESS (a Strategy Lab workspace). RESEARCH ONLY — NOTHING IS TRADED OR DEPLOYED.
//
// Pick a base rotation configuration, the universe, the date range, train / test / step months, the frequency, a bounded
// candidate grid, the selection metric and costs; press Run; read window-by-window TRAIN selections, out-of-sample TEST
// metrics, aggregate robustness, selected-config changes, cost and parameter sensitivity and the regime breakdown.
// No broker button, no promotion button, nothing is written into any form. No timers, no polling, nothing on page load.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("pwf-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const pct = (x, d = 2) => (x == null ? "—" : `${(Number(x) * 100).toFixed(d)}%`);
  const num = (x, d = 2) => (x == null ? "—" : Number(x).toFixed(d));
  const BASE = "/api/rotation-walkforward";
  const BANNER = "WALK-FORWARD ROBUSTNESS — RESEARCH ONLY — NOTHING IS TRADED OR DEPLOYED";
  const KIND = { COMPLETED: "ok", FAILED: "alert" };

  let cfg = null, configs = [], runs = [], busy = "", notice = "", requests = 0, seq = 0;
  let form = { config_id: "", universe: "CUSTOM", ref: "", symbols: "", start_date: "", end_date: "", train_months: "24", test_months: "6", step_months: "",
    rebalance_frequency: "MONTHLY", selection_metric: "SHARPE", max_candidates: "20", initial_cash: "100000.00", transaction_cost_bps: "5", slippage_bps: "5",
    grid_dimension: "", grid_values: "" };
  let detail = null, windows = [], candidates = [], selections = [], oos = [], sens = null, regimes = null, view = "summary";

  const api = (method, url, body) => {
    requests++;
    return fetch(url, method === "GET" ? {} : { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }))
      .catch(() => ({ status: 0, body: { message: "The server did not answer." } }));
  };

  async function load() {
    const my = ++seq;
    const [c, r] = await Promise.all([api("GET", `${BASE}/config`), api("GET", `${BASE}/runs?limit=20`)]);
    if (my !== seq) return;
    if (c.status === 200) { cfg = c.body; configs = cfg.rotation_configs || []; if (!form.config_id && configs.length) form.config_id = configs[0].config_id; }
    if (r.status === 200) runs = r.body.runs || [];
    draw();
  }

  function readForm() { root.querySelectorAll("[data-pwf-f]").forEach((el) => { form[el.dataset.pwfF] = el.value; }); }
  const chosen = () => configs.find((c) => c.config_id === form.config_id);
  const symbolsList = () => [...new Set(form.symbols.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean))];
  const gridValues = () => form.grid_values.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  const ready = () => !!(chosen() && form.start_date && form.end_date && (form.universe !== "CUSTOM" || symbolsList().length) &&
    (!["SAVED_SCAN", "SAVED_UNIVERSE"].includes(form.universe) || form.ref));

  async function runWalkForward() {
    const c = chosen();
    if (!c || busy) return;
    busy = "run"; notice = ""; draw();
    const universe = { source: form.universe, ref: ["SAVED_SCAN", "SAVED_UNIVERSE"].includes(form.universe) ? form.ref : null,
      symbols: form.universe === "CUSTOM" ? symbolsList() : null };
    const body = { config_id: c.config_id, config_hash: c.config_hash, universe, start_date: form.start_date, end_date: form.end_date,
      train_months: Number(form.train_months), test_months: Number(form.test_months), rebalance_frequency: form.rebalance_frequency,
      selection_metric: form.selection_metric, max_candidates: Number(form.max_candidates), initial_cash: form.initial_cash,
      transaction_cost_bps: form.transaction_cost_bps, slippage_bps: form.slippage_bps };
    if (form.step_months) body.step_months = Number(form.step_months);
    if (form.grid_dimension && gridValues().length) body.grid = { dimensions: { [form.grid_dimension]: gridValues() } };
    const r = await api("POST", `${BASE}/run`, body);
    busy = "";
    if (r.status !== 200) { notice = `${r.body.status || r.status}: ${r.body.message || "The evaluation could not run."}`; draw(); return; }
    await open(r.body.run.run_id);
    const l = await api("GET", `${BASE}/runs?limit=20`);
    if (l.status === 200) runs = l.body.runs || [];
    draw();
  }

  async function open(id) {
    busy = "open"; draw();
    const [d, w, o, s, g] = await Promise.all([api("GET", `${BASE}/runs/${id}`), api("GET", `${BASE}/runs/${id}/windows`), api("GET", `${BASE}/runs/${id}/oos`),
      api("GET", `${BASE}/runs/${id}/sensitivity`), api("GET", `${BASE}/runs/${id}/regimes`)]);
    busy = "";
    if (d.status !== 200) { notice = "The run could not be loaded."; draw(); return; }
    detail = d.body; windows = w.status === 200 ? w.body.windows : []; candidates = w.status === 200 ? w.body.candidates : [];
    selections = w.status === 200 ? w.body.selections : []; oos = o.status === 200 ? o.body.oos : []; sens = s.status === 200 ? s.body : null;
    regimes = g.status === 200 ? g.body.regimes : null;
    draw();
  }

  function onClick(ev) {
    const b = ev.target.closest("[data-pwf-act]");
    if (!b || b.disabled) return;
    const a = b.dataset.pwfAct;
    if (a === "run") { readForm(); runWalkForward(); }
    else if (a === "open") open(b.dataset.id);
    else if (a === "view") { view = b.dataset.view; draw(); }
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------------
  function controls() {
    const d = busy ? " disabled" : "";
    const opts = configs.map((c) => `<option value="${esc(c.config_id)}"${c.config_id === form.config_id ? " selected" : ""}>${esc(c.name)} v${esc(c.version)} · size ${esc(c.config.portfolio_size)} · exit ${esc(c.config.exit_rank)}</option>`).join("");
    const sel = (k, values, allowEmpty) => `<select data-pwf-f="${k}"${d}>${allowEmpty ? `<option value=""${form[k] ? "" : " selected"}>—</option>` : ""}${values.map((v) => `<option value="${esc(v)}"${form[k] === v ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
    const inp = (k, type = "text", extra = "") => `<input data-pwf-f="${k}" type="${type}" value="${esc(form[k])}" ${extra}${d}>`;
    return `<section class="cc-card"><div class="cc-head"><h2>WALK-FORWARD ROBUSTNESS</h2><span class="cc-small cc-dimtext">train-only selection · test-only evaluation · Stage 4.8 replay · research only</span></div>
      <div class="pwf-grid">
        <label><span class="cc-label">BASE ROTATION CONFIG</span><select data-pwf-f="config_id"${d}>${opts || '<option value="">No saved configuration — create one in Portfolio Rotation</option>'}</select></label>
        <label><span class="cc-label">UNIVERSE</span>${sel("universe", cfg ? cfg.universe_sources : ["CUSTOM"])}</label>
        ${["SAVED_SCAN", "SAVED_UNIVERSE"].includes(form.universe) ? `<label><span class="cc-label">REFERENCE ID</span>${inp("ref", "text", 'maxlength="64"')}</label>` : ""}
        ${form.universe === "CUSTOM" ? `<label class="pwf-wide"><span class="cc-label">SYMBOLS</span>${inp("symbols", "text", 'placeholder="AMD, MU, KO …"')}</label>` : ""}
        <label><span class="cc-label">START</span>${inp("start_date", "date")}</label><label><span class="cc-label">END</span>${inp("end_date", "date")}</label>
        <label><span class="cc-label">TRAIN MONTHS</span>${inp("train_months", "number", 'min="1" max="120"')}</label>
        <label><span class="cc-label">TEST MONTHS</span>${inp("test_months", "number", 'min="1" max="120"')}</label>
        <label><span class="cc-label">STEP MONTHS (blank = test)</span>${inp("step_months", "number", 'min="1" max="120"')}</label>
        <label><span class="cc-label">REBALANCE</span>${sel("rebalance_frequency", cfg ? cfg.frequencies : ["WEEKLY", "MONTHLY"])}</label>
        <label><span class="cc-label">SELECTION METRIC</span>${sel("selection_metric", cfg ? cfg.selection_metrics : ["SHARPE"])}</label>
        <label><span class="cc-label">MAX CANDIDATES</span>${inp("max_candidates", "number", 'min="1" max="100"')}</label>
        <label><span class="cc-label">GRID DIMENSION</span>${sel("grid_dimension", cfg ? cfg.grid_dimensions : [], true)}</label>
        <label><span class="cc-label">GRID VALUES</span>${inp("grid_values", "text", 'placeholder="e.g. 5, 8, 10"')}</label>
        <label><span class="cc-label">INITIAL CASH</span>${inp("initial_cash", "text", 'maxlength="20"')}</label>
        <label><span class="cc-label">COST (bps)</span>${inp("transaction_cost_bps", "text", 'maxlength="12"')}</label>
        <label><span class="cc-label">SLIPPAGE (bps)</span>${inp("slippage_bps", "text", 'maxlength="12"')}</label>
      </div>
      <div class="pwf-row"><button type="button" class="cc-btn cc-primary" data-pwf-act="run"${!ready() || busy ? " disabled" : ""}>${busy === "run" ? "Running…" : "Run Walk-Forward"}</button>
        <span class="cc-small cc-dimtext">${esc(cfg ? cfg.universe_note : "")}</span></div></section>`;
  }

  function cell(label, value) { return `<div><span class="cc-label">${esc(label)}</span><b>${value}</b></div>`; }

  function resultCard() {
    if (!detail) return `<section class="cc-card"><p class="cc-small cc-dimtext">Run an evaluation or open a stored run to see its results.</p></section>`;
    const r = detail.run, m = detail.metrics || {}, rb = detail.robustness || {};
    const head = `<div class="cc-head"><h2>RUN ${esc((r.run_id || "").slice(0, 8))}</h2>${tag(r.status, KIND[r.status] || "dim")}
      <span class="cc-small cc-dimtext">${esc(r.n_windows)} windows · ${esc(r.n_candidates)} candidates (${esc(r.n_rejected)} rejected, ${esc(r.n_discarded)} over cap) · ${esc(r.n_evaluations)} replays (${esc(r.n_cache_hits)} cache hits) · ${esc(r.runtime_s)} s${r.overlapping_tests ? " · OVERLAPPING TEST WINDOWS (no stitched curve)" : ""}</span></div>`;
    if (r.status !== "COMPLETED") return `<section class="cc-card">${head}<div class="cc-small prt-why">Failed closed: ${esc(r.failure_code)} — ${esc(r.failure_detail)}</div></section>`;
    const views = ["summary", "windows", "oos", "costs", "parameters", "regimes"].map((v) => `<button type="button" class="cc-btn cc-mini${view === v ? " cc-primary" : ""}" data-pwf-act="view" data-view="${v}">${esc(v)}</button>`).join(" ");
    let body = "";
    if (view === "summary") {
      const st = m.stitched_oos || {};
      body = `<div class="pwf-facts">
        ${cell("Robustness score (rs_v1, not a probability)", num(rb.score, 3))}${cell("Median OOS CAGR", pct(m.median_oos_cagr))}${cell("Median OOS Sharpe", num(m.median_oos_sharpe))}${cell("Median OOS Sortino", num(m.median_oos_sortino))}
        ${cell("Positive windows", pct(m.positive_window_pct, 0))}${cell("Beat SPY windows", pct(m.benchmark_beating_pct, 0))}${cell("Median OOS excess", pct(m.median_oos_excess_return))}
        ${cell("Worst OOS drawdown", pct(m.worst_oos_max_drawdown))}${cell("Mean OOS drawdown", pct(m.mean_oos_max_drawdown))}${cell("Worst window return", pct(m.worst_window_return))}${cell("OOS return std", pct(m.oos_return_std))}
        ${cell("Mean OOS turnover", pct(m.mean_oos_turnover))}${cell("Distinct configs selected", esc(m.n_distinct_selected))}${cell("Selection changes", esc(m.n_selection_changes))}
        ${cell("Stitched OOS return", pct(st.total_return))}${cell("Stitched OOS CAGR", pct(st.cagr))}${cell("Stitched OOS Sharpe", num(st.sharpe))}${cell("Stitched OOS max DD", pct(st.max_drawdown))}${cell("Stitched SPY return", pct(st.benchmark_total_return))}</div>
        <div class="cc-small cc-dimtext">Score components: ${esc(Object.entries(rb.components || {}).map(([k, v]) => `${k} ${Number(v).toFixed(2)}`).join(" · "))}</div>
        <div class="cc-small cc-dimtext">${esc(rb.formula || "")}</div>
        <div class="cc-small cc-dimtext">${esc(r.universe_note)} · hashes: definition ${esc((r.wf_config_hash || "").slice(0, 12))}, data ${esc((r.data_hash || "").slice(0, 12))}, result ${esc((r.result_hash || "").slice(0, 12))}</div>`;
    } else if (view === "windows") {
      const selByW = Object.fromEntries(selections.map((s) => [s.window_index, s]));
      body = `<div class="sf-tablewrap pwf-scroll"><table class="sf-table"><thead><tr><th>#</th><th>Train</th><th>Test</th><th>Sessions</th><th>Selected (TRAIN only)</th><th>Metric</th><th>Value</th></tr></thead><tbody>
        ${windows.map((w) => { const s = selByW[w.window_index] || {}; return `<tr><td>${esc(w.window_index)}</td><td>${esc(w.train_first_session)} → ${esc(w.train_last_session)}</td><td>${esc(w.test_first_session)} → ${esc(w.test_last_session)}</td><td>${esc(w.n_train_sessions)} / ${esc(w.n_test_sessions)}</td><td>${esc(s.label)} <code>${esc((s.config_hash || "").slice(0, 8))}</code></td><td>${esc(s.selection_metric)}</td><td>${num(s.train_metric_value, 3)}</td></tr>`; }).join("")}</tbody></table></div>
        <h3 class="cc-small">TRAIN ranking per window</h3><div class="sf-tablewrap pwf-scroll"><table class="sf-table"><thead><tr><th>Window</th><th>Rank</th><th>Candidate</th><th>Status</th><th>Metric</th><th>Train CAGR</th><th>Train Sharpe</th><th>Train max DD</th><th>Turnover</th></tr></thead><tbody>
        ${candidates.map((c) => `<tr><td>${esc(c.window_index)}</td><td>${esc(c.train_rank)}</td><td>${esc(c.label)}</td><td>${esc(c.train_status)}</td><td>${num(c.train_metric_value, 3)}</td><td>${pct(c.train_metrics.cagr)}</td><td>${num(c.train_metrics.sharpe)}</td><td>${pct(c.train_metrics.max_drawdown)}</td><td>${pct(c.train_metrics.mean_turnover)}</td></tr>`).join("")}</tbody></table></div>`;
    } else if (view === "oos") {
      body = `<div class="sf-tablewrap pwf-scroll"><table class="sf-table"><thead><tr><th>Window</th><th>Config</th><th>Status</th><th>Return</th><th>SPY</th><th>Excess</th><th>CAGR</th><th>Sharpe</th><th>Max DD</th><th>Turnover</th><th>Costs</th></tr></thead><tbody>
        ${oos.map((o) => { const t = o.test_metrics || {}; return `<tr><td>${esc(o.window_index)}</td><td><code>${esc((o.config_hash || "").slice(0, 8))}</code></td><td>${esc(o.test_status)}</td><td>${pct(t.total_return)}</td><td>${pct(t.benchmark_total_return)}</td><td>${pct(t.excess_return)}</td><td>${pct(t.cagr)}</td><td>${num(t.sharpe)}</td><td>${pct(t.max_drawdown)}</td><td>${pct(t.mean_turnover)}</td><td>${esc(t.total_transaction_costs)}</td></tr>`; }).join("")}</tbody></table></div>
        <p class="cc-small cc-dimtext">Each TEST window is replayed from initial cash with the configuration frozen on its TRAIN window; TEST results never influence any selection.</p>`;
    } else if (view === "costs") {
      const rows = (sens && sens.cost) || [];
      body = `<div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Cost bps</th><th>Slippage bps</th><th>Median OOS CAGR</th><th>Median OOS Sharpe</th><th>Stitched OOS CAGR</th><th>Stitched OOS return</th><th>Positive windows</th><th></th></tr></thead><tbody>
        ${rows.map((c) => `<tr><td>${esc(c.transaction_cost_bps)}</td><td>${esc(c.slippage_bps)}</td><td>${pct(c.median_oos_cagr)}</td><td>${num(c.median_oos_sharpe)}</td><td>${pct(c.stitched_cagr)}</td><td>${pct(c.stitched_total_return)}</td><td>${pct(c.positive_window_pct, 0)}</td><td>${c.break_even_hint ? tag("first non-positive", "warn") : ""}</td></tr>`).join("")}</tbody></table></div>
        <p class="cc-small cc-dimtext">The frozen per-window selections replayed under each cost pair; no re-selection, no extrapolation beyond the tested points.</p>`;
    } else if (view === "parameters") {
      const p = (sens && sens.parameter) || {};
      const base = p.base || {};
      body = `<div class="pwf-facts">${cell("Fragile", p.fragile ? "YES" : "no")}${cell("Base rank among neighbours", esc(base.rank_among_neighbours))}${cell("Valid neighbours", esc(base.n_valid_neighbours))}${cell("Median Sharpe deterioration", num(p.median_sharpe_deterioration))}${cell("Base Sharpe (full range)", num((base.metrics || {}).sharpe))}</div>
        <div class="sf-tablewrap pwf-scroll"><table class="sf-table"><thead><tr><th>Neighbour</th><th>Status</th><th>Sharpe</th><th>Δ Sharpe</th><th>CAGR</th><th>Δ CAGR</th><th>Max DD</th><th>Rank</th><th>Δ rank</th><th>Reason</th></tr></thead><tbody>
        ${(p.neighbours || []).map((n) => { const mm = n.metrics || {}, dl = n.delta || {}; return `<tr><td>${esc(n.label)}</td><td>${esc(n.status)}</td><td>${num(mm.sharpe)}</td><td>${num(dl.sharpe)}</td><td>${pct(mm.cagr)}</td><td>${pct(dl.cagr)}</td><td>${pct(mm.max_drawdown)}</td><td>${esc(n.rank == null ? "—" : n.rank)}</td><td>${esc(n.rank_delta == null ? "—" : n.rank_delta)}</td><td class="cc-small">${esc(n.reason || "")}</td></tr>`; }).join("")}</tbody></table></div>
        <p class="cc-small cc-dimtext">${esc(p.method || "")}</p>`;
    } else {
      const rg = regimes || {};
      const table = (axis) => `<h3 class="cc-small">${esc(axis)}</h3><div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Regime</th><th>Sessions</th><th>Return</th><th>Sharpe</th><th>Max DD</th><th>Exposure</th><th>Turnover</th></tr></thead><tbody>
        ${Object.entries(rg[axis] || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v.sessions)}</td><td>${pct(v.return)}</td><td>${num(v.sharpe)}</td><td>${pct(v.max_drawdown)}</td><td>${pct(v.mean_exposure)}</td><td>${pct(v.mean_turnover)}</td></tr>`).join("")}</tbody></table></div>`;
      body = regimes ? `${table("trend")}${table("vol")}<p class="cc-small cc-dimtext">${esc(rg.method || "")}</p>` : `<p class="cc-small cc-dimtext">No regime breakdown (no stitched out-of-sample curve).</p>`;
    }
    return `<section class="cc-card">${head}<div class="pwf-row">${views}</div>${body}</section>`;
  }

  function runsCard() {
    if (!runs.length) return "";
    return `<section class="cc-card"><div class="cc-head"><h2>STORED RUNS</h2><span class="cc-small cc-dimtext">immutable · newest first</span></div>
      <div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Run</th><th>Status</th><th>Windows</th><th>Candidates</th><th>Replays</th><th></th></tr></thead><tbody>
      ${runs.map((r) => `<tr><td><code>${esc(r.run_id.slice(0, 8))}</code></td><td>${tag(r.status, KIND[r.status] || "dim")}</td><td>${esc(r.n_windows)}</td><td>${esc(r.n_candidates)}</td><td>${esc(r.n_evaluations)}</td>
        <td><button type="button" class="cc-btn cc-mini" data-pwf-act="open" data-id="${esc(r.run_id)}"${busy ? " disabled" : ""}>Open</button></td></tr>`).join("")}</tbody></table></div></section>`;
  }

  function draw() {
    root.innerHTML = `<div class="pwf"><div class="prt-banner" role="note">${esc(BANNER)}</div>
      ${notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}${controls()}${resultCard()}${runsCard()}
      <p class="cc-small cc-dimtext">${esc(cfg ? cfg.note : "")}</p></div>`;
  }
  function show() {
    if (!root.dataset.pwfBound) { root.dataset.pwfBound = "1"; root.addEventListener("click", onClick); root.addEventListener("change", () => { readForm(); draw(); }); }
    draw();
    load();
  }

  window.StrategyFit.addWorkspace({ id: "walkforward49", label: "Walk-Forward", cls: "pwf-mode", show });
  window.PortfolioWalkForward = { get state() { return { busy, notice, requests, configs: configs.length, runs: runs.length, run: detail ? { id: detail.run.run_id, status: detail.run.status } : null, view, ready: ready() }; } };
})();
