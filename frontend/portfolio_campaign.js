// portfolio_campaign.js — Stage 5.0 MODEL EVALUATION CAMPAIGN (a Strategy Lab workspace). RESEARCH ONLY — NOTHING IS TRADED, ACTIVATED OR PROMOTED.
//
// Pick a base rotation configuration, the universe, the range, walk-forward settings, a bounded candidate grid, max
// candidates and finalist count; press Run; read eligibility, the leaderboard with every candidate's evidence and exclusion
// reasons, the "paper-forward-test candidate" finalists, their comparison and model cards. No broker button, no activation
// button, nothing is written into any form. No timers, no polling, nothing on page load; every value is escaped.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("pmc-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const pct = (x, d = 2) => (x == null ? "—" : `${(Number(x) * 100).toFixed(d)}%`);
  const num = (x, d = 2) => (x == null ? "—" : Number(x).toFixed(d));
  const BASE = "/api/model-campaign";
  const BANNER = "MODEL EVALUATION CAMPAIGN — RESEARCH ONLY — NOTHING IS TRADED, ACTIVATED OR PROMOTED";
  const KIND = { COMPLETED: "ok", NO_FINALIST: "warn", FAILED: "alert" };

  let cfg = null, configs = [], runs = [], busy = "", notice = "", requests = 0, seq = 0;
  let form = { config_id: "", universe: "CUSTOM", ref: "", symbols: "", start_date: "", end_date: "", train_months: "24", test_months: "6", step_months: "",
    rebalance_frequency: "MONTHLY", selection_metric: "SHARPE", max_candidates: "20", finalist_count: "3", grid_dimension: "", grid_values: "",
    initial_cash: "100000.00", transaction_cost_bps: "5", slippage_bps: "5" };
  let detail = null, board = [], fins = [], cards = [], comparison = null, view = "leaderboard", card = "";

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

  function readForm() { root.querySelectorAll("[data-pmc-f]").forEach((el) => { form[el.dataset.pmcF] = el.value; }); }
  const chosen = () => configs.find((c) => c.config_id === form.config_id);
  const symbolsList = () => [...new Set(form.symbols.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean))];
  const gridValues = () => form.grid_values.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  const ready = () => !!(chosen() && form.start_date && form.end_date && (form.universe !== "CUSTOM" || symbolsList().length) &&
    (!["SAVED_SCAN", "SAVED_UNIVERSE"].includes(form.universe) || form.ref));

  async function runCampaign() {
    const c = chosen();
    if (!c || busy) return;
    busy = "run"; notice = ""; draw();
    const universe = { source: form.universe, ref: ["SAVED_SCAN", "SAVED_UNIVERSE"].includes(form.universe) ? form.ref : null,
      symbols: form.universe === "CUSTOM" ? symbolsList() : null };
    const body = { config_id: c.config_id, config_hash: c.config_hash, universe, start_date: form.start_date, end_date: form.end_date,
      train_months: Number(form.train_months), test_months: Number(form.test_months), rebalance_frequency: form.rebalance_frequency,
      selection_metric: form.selection_metric, max_candidates: Number(form.max_candidates), finalist_count: Number(form.finalist_count),
      initial_cash: form.initial_cash, transaction_cost_bps: form.transaction_cost_bps, slippage_bps: form.slippage_bps };
    if (form.step_months) body.step_months = Number(form.step_months);
    if (form.grid_dimension && gridValues().length) body.grid = { dimensions: { [form.grid_dimension]: gridValues() } };
    const r = await api("POST", `${BASE}/run`, body);
    busy = "";
    if (r.status !== 200) { notice = `${r.body.status || r.status}: ${r.body.message || "The campaign could not run."}`; draw(); return; }
    await open(r.body.run.campaign_id);
    const l = await api("GET", `${BASE}/runs?limit=20`);
    if (l.status === 200) runs = l.body.runs || [];
    draw();
  }

  async function open(id) {
    busy = "open"; draw();
    const [d, b, f, k, m] = await Promise.all([api("GET", `${BASE}/runs/${id}`), api("GET", `${BASE}/runs/${id}/leaderboard`), api("GET", `${BASE}/runs/${id}/finalists`),
      api("GET", `${BASE}/runs/${id}/cards`), api("GET", `${BASE}/runs/${id}/comparison`)]);
    busy = "";
    if (d.status !== 200) { notice = "The campaign could not be loaded."; draw(); return; }
    detail = d.body; board = b.status === 200 ? b.body.leaderboard : []; fins = f.status === 200 ? f.body.finalists : []; cards = k.status === 200 ? k.body.cards : [];
    comparison = m.status === 200 ? m.body.comparison : null; card = "";
    draw();
  }

  function onClick(ev) {
    const b = ev.target.closest("[data-pmc-act]");
    if (!b || b.disabled) return;
    const a = b.dataset.pmcAct;
    if (a === "run") { readForm(); runCampaign(); }
    else if (a === "open") open(b.dataset.id);
    else if (a === "view") { view = b.dataset.view; draw(); }
    else if (a === "card") { card = card === b.dataset.hash ? "" : b.dataset.hash; draw(); }
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------------
  function controls() {
    const d = busy ? " disabled" : "";
    const opts = configs.map((c) => `<option value="${esc(c.config_id)}"${c.config_id === form.config_id ? " selected" : ""}>${esc(c.name)} v${esc(c.version)} · size ${esc(c.config.portfolio_size)} · exit ${esc(c.config.exit_rank)}</option>`).join("");
    const sel = (k, values, allowEmpty) => `<select data-pmc-f="${k}"${d}>${allowEmpty ? `<option value=""${form[k] ? "" : " selected"}>—</option>` : ""}${values.map((v) => `<option value="${esc(v)}"${form[k] === v ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
    const inp = (k, type = "text", extra = "") => `<input data-pmc-f="${k}" type="${type}" value="${esc(form[k])}" ${extra}${d}>`;
    return `<section class="cc-card"><div class="cc-head"><h2>MODEL EVALUATION CAMPAIGN</h2><span class="cc-small cc-dimtext">hold-out evidence per candidate · transparent gates · deterministic leaderboard · research only</span></div>
      <div class="pmc-grid">
        <label><span class="cc-label">BASE ROTATION CONFIG</span><select data-pmc-f="config_id"${d}>${opts || '<option value="">No saved configuration — create one in Portfolio Rotation</option>'}</select></label>
        <label><span class="cc-label">UNIVERSE</span>${sel("universe", cfg ? cfg.universe_sources : ["CUSTOM"])}</label>
        ${["SAVED_SCAN", "SAVED_UNIVERSE"].includes(form.universe) ? `<label><span class="cc-label">REFERENCE ID</span>${inp("ref", "text", 'maxlength="64"')}</label>` : ""}
        ${form.universe === "CUSTOM" ? `<label class="pmc-wide"><span class="cc-label">SYMBOLS</span>${inp("symbols", "text", 'placeholder="AMD, MU, KO …"')}</label>` : ""}
        <label><span class="cc-label">START</span>${inp("start_date", "date")}</label><label><span class="cc-label">END</span>${inp("end_date", "date")}</label>
        <label><span class="cc-label">TRAIN MONTHS</span>${inp("train_months", "number", 'min="1" max="120"')}</label>
        <label><span class="cc-label">TEST MONTHS</span>${inp("test_months", "number", 'min="1" max="120"')}</label>
        <label><span class="cc-label">STEP MONTHS (blank = test)</span>${inp("step_months", "number", 'min="1" max="120"')}</label>
        <label><span class="cc-label">REBALANCE</span>${sel("rebalance_frequency", cfg ? cfg.frequencies : ["WEEKLY", "MONTHLY"])}</label>
        <label><span class="cc-label">STABILITY SELECTION METRIC</span>${sel("selection_metric", cfg ? cfg.selection_metrics : ["SHARPE"])}</label>
        <label><span class="cc-label">MAX CANDIDATES</span>${inp("max_candidates", "number", 'min="1" max="100"')}</label>
        <label><span class="cc-label">FINALISTS</span>${inp("finalist_count", "number", 'min="1" max="10"')}</label>
        <label><span class="cc-label">GRID DIMENSION</span>${sel("grid_dimension", cfg ? cfg.grid_dimensions : [], true)}</label>
        <label><span class="cc-label">GRID VALUES</span>${inp("grid_values", "text", 'placeholder="e.g. 5, 8, 10"')}</label>
        <label><span class="cc-label">INITIAL CASH</span>${inp("initial_cash", "text", 'maxlength="20"')}</label>
        <label><span class="cc-label">COST (bps)</span>${inp("transaction_cost_bps", "text", 'maxlength="12"')}</label>
        <label><span class="cc-label">SLIPPAGE (bps)</span>${inp("slippage_bps", "text", 'maxlength="12"')}</label>
      </div>
      <div class="pmc-row"><button type="button" class="cc-btn cc-primary" data-pmc-act="run"${!ready() || busy ? " disabled" : ""}>${busy === "run" ? "Running…" : "Run Campaign"}</button>
        <span class="cc-small cc-dimtext">${esc(cfg ? cfg.universe_note : "")}</span></div>
      ${cfg ? `<div class="cc-small cc-dimtext">Gates: ${esc(Object.entries(cfg.gate_defaults).map(([k, v]) => `${k} ${v}`).join(" · "))} · Ranking: ${esc((cfg.ranking || []).join(" → "))}</div>` : ""}</section>`;
  }

  function cell(label, value) { return `<div><span class="cc-label">${esc(label)}</span><b>${value}</b></div>`; }

  function resultCard() {
    if (!detail) return `<section class="cc-card"><p class="cc-small cc-dimtext">Run a campaign or open a stored one to see its leaderboard.</p></section>`;
    const r = detail.run;
    const head = `<div class="cc-head"><h2>CAMPAIGN ${esc((r.campaign_id || "").slice(0, 8))}</h2>${tag(r.status, KIND[r.status] || "dim")}
      <span class="cc-small cc-dimtext">${esc(r.n_candidates)} candidates (${esc(r.n_rejected)} rejected, ${esc(r.n_discarded)} over cap) · ${esc(r.n_windows)} windows · ${esc(r.n_eligible)} eligible · ${esc(r.n_finalists)} ${esc(r.finalist_label)}s · ${esc(r.n_evaluations)} replays (${esc(r.n_cache_hits)} cache hits) · ${esc(r.runtime_s)} s</span></div>`;
    if (r.status === "FAILED") return `<section class="cc-card">${head}<div class="cc-small prt-why">Failed closed: ${esc(r.failure_code)} — ${esc(r.failure_detail)}</div></section>`;
    const views = ["leaderboard", "finalists", "comparison", "cards", "stability"].map((v) => `<button type="button" class="cc-btn cc-mini${view === v ? " cc-primary" : ""}" data-pmc-act="view" data-view="${v}">${esc(v)}</button>`).join(" ");
    let body = "";
    if (view === "leaderboard") {
      body = `<div class="sf-tablewrap pmc-scroll"><table class="sf-table"><thead><tr><th>#</th><th>Candidate</th><th>Eligible</th><th>Score</th><th>Med OOS CAGR</th><th>Med Sharpe</th><th>Med Sortino</th><th>Worst ret</th><th>Worst DD</th><th>Pos %</th><th>Beat SPY %</th><th>Med excess</th><th>Turnover</th><th>Cost sens.</th><th>Fragility</th><th>Selected</th><th>Full-hist CAGR (info)</th><th>Exclusions</th></tr></thead><tbody>
        ${board.map((b) => `<tr class="${b.eligible ? "" : "prt-ineligible"}"><td>${esc(b.rank)}</td><td>${esc(b.label)} <code>${esc(b.config_hash.slice(0, 8))}</code></td><td>${b.eligible ? tag("eligible", "ok") : tag("excluded", "dim")}</td><td>${num(b.robustness_score, 3)}</td><td>${pct(b.median_oos_cagr)}</td><td>${num(b.median_oos_sharpe)}</td><td>${num(b.median_oos_sortino)}</td><td>${pct(b.worst_window_return)}</td><td>${pct(b.worst_oos_max_drawdown)}</td><td>${pct(b.positive_window_pct, 0)}</td><td>${pct(b.benchmark_beating_pct, 0)}</td><td>${pct(b.median_oos_excess_return)}</td><td>${pct(b.mean_oos_turnover)}</td><td>${num(b.cost_sensitivity)}</td><td>${num(b.fragility_ratio)}${b.fragile_flag ? " ⚠" : ""}</td><td>${esc(b.times_selected)}</td><td>${pct(b.full_history_cagr)}</td><td class="cc-small">${esc((b.exclusions || []).map((e) => `${e.gate} (${e.value == null ? "n/a" : Number(e.value).toFixed(3)} vs ${e.limit})`).join("; "))}</td></tr>`).join("")}</tbody></table></div>
        <p class="cc-small cc-dimtext">Every candidate keeps its full evidence here whether eligible or not. Full-history CAGR is informational and never enters the order.</p>`;
    } else if (view === "finalists") {
      body = fins.length ? `<div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>#</th><th>Candidate</th><th>Role</th><th>Score</th><th></th></tr></thead><tbody>
        ${fins.map((f) => `<tr><td>${esc(f.finalist_rank)}</td><td>${esc(f.label)} <code>${esc(f.config_hash.slice(0, 8))}</code></td><td>${esc(f.role)}</td><td>${num(f.robustness_score, 3)}</td><td><button type="button" class="cc-btn cc-mini" data-pmc-act="card" data-hash="${esc(f.config_hash)}">Model card</button></td></tr>`).join("")}</tbody></table></div>
        <p class="cc-small cc-dimtext">Research records only: nothing here is activated, scheduled or sent anywhere.</p>${cardView()}`
        : `<p class="cc-small">${tag("NO_FINALIST", "warn")} No candidate passed every eligibility gate. That is a valid outcome; the leaderboard still shows every candidate and its exclusion reasons.</p>`;
    } else if (view === "comparison") {
      const c = comparison || {};
      const rowsC = [c.baseline, ...(c.finalists || [])].filter(Boolean);
      body = `<div class="sf-tablewrap pmc-scroll"><table class="sf-table"><thead><tr><th>Config</th><th>Rank</th><th>Score</th><th>Med OOS CAGR</th><th>Med Sharpe</th><th>Worst DD</th><th>Worst ret</th><th>Pos %</th><th>Beat SPY %</th><th>Turnover</th><th>Cost sens.</th><th>Fragility</th><th>OOS std</th></tr></thead><tbody>
        ${rowsC.map((x) => `<tr><td>${esc(x.label)} <code>${esc(x.config_hash.slice(0, 8))}</code>${c.baseline && x.config_hash === c.baseline.config_hash ? " (baseline)" : ""}</td><td>${esc(x.rank)}</td><td>${num(x.robustness_score, 3)}</td><td>${pct(x.median_oos_cagr)}</td><td>${num(x.median_oos_sharpe)}</td><td>${pct(x.worst_oos_max_drawdown)}</td><td>${pct(x.worst_window_return)}</td><td>${pct(x.positive_window_pct, 0)}</td><td>${pct(x.benchmark_beating_pct, 0)}</td><td>${pct(x.mean_oos_turnover)}</td><td>${num(x.cost_sensitivity)}</td><td>${num(x.fragility_ratio)}</td><td>${pct(x.oos_return_std)}</td></tr>`).join("")}</tbody></table></div>
        <div class="cc-small cc-dimtext">Pareto flags: ${esc(Object.entries((c.pareto || {})).filter(([k]) => k !== "note").map(([k, v]) => `${k} ${v ? v.slice(0, 8) : "—"}`).join(" · "))}</div>
        <div class="cc-small cc-dimtext">OOS window returns: ${esc(Object.entries(c.oos_distribution || {}).map(([h, xs]) => `${h.slice(0, 8)}: ${xs.map((x) => (x == null ? "—" : (x * 100).toFixed(1) + "%")).join(", ")}`).join(" · "))}</div>`;
    } else if (view === "cards") {
      body = cards.length ? cards.map((k) => cardHtml(k)).join("") : `<p class="cc-small cc-dimtext">No model card (no finalist).</p>`;
    } else {
      const s = detail.stability || {};
      body = `<div class="pmc-facts">${cell("Distinct configs frozen by the walk-forward", esc(s.n_distinct_selected))}${cell("Selection changes", esc(s.n_selection_changes))}${cell("Selection metric (TRAIN only)", esc(s.selection_metric))}</div>
        <div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Window</th><th>Frozen candidate (TRAIN only)</th><th>Train metric</th></tr></thead><tbody>
        ${(s.selections || []).map((x) => `<tr><td>${esc(x.window_index)}</td><td>${esc(x.label)} <code>${esc(x.config_hash.slice(0, 8))}</code></td><td>${num(x.train_metric_value, 3)}</td></tr>`).join("")}</tbody></table></div>`;
    }
    return `<section class="cc-card">${head}<div class="pmc-row">${views}</div>${body}</section>`;
  }

  function cardView() { const k = cards.find((x) => x.config_hash === card); return k ? cardHtml(k) : ""; }
  function cardHtml(k) {
    const o = k.oos_metrics || {}, rb = k.robustness || {};
    return `<div class="prt-detail"><div class="cc-head"><h3>${esc(k.label)} · MODEL CARD</h3>${tag(k.role, "info")}<code>${esc(k.config_hash.slice(0, 12))}</code></div>
      <div class="pmc-facts">${cell("Leaderboard rank", esc(k.leaderboard_rank))}${cell("Robustness score", num(rb.score, 3))}${cell("Median OOS CAGR", pct(o.median_oos_cagr))}${cell("Median OOS Sharpe", num(o.median_oos_sharpe))}${cell("Worst OOS DD", pct(o.worst_oos_max_drawdown))}${cell("Positive / beating", `${pct(o.positive_window_pct, 0)} / ${pct(o.benchmark_beating_pct, 0)}`)}${cell("Cost sensitivity", num((k.cost_sensitivity || {}).value))}${cell("Fragility", num((k.fragility || {}).ratio))}</div>
      <div class="cc-small"><b>Weights:</b> ${esc(Object.entries(k.factor_weights || {}).map(([a, b]) => `${a} ${b}`).join(" · "))}</div>
      <div class="cc-small"><b>Rules:</b> ${esc(Object.entries(k.portfolio_rules || {}).map(([a, b]) => `${a} ${Array.isArray(b) ? b.join("/") || "—" : b}`).join(" · "))}</div>
      <div class="cc-small"><b>Interval:</b> ${esc(k.historical_interval.start)} → ${esc(k.historical_interval.end)} · ${esc(k.historical_interval.n_windows)} windows · ${esc(k.historical_interval.rebalance_frequency)}</div>
      <div class="cc-small"><b>Weak regimes:</b> ${esc((k.weak_regimes || []).map((w) => `${w.axis}: ${w.regime} ${(w.return * 100).toFixed(2)}%`).join(" · ") || "—")}</div>
      <div class="cc-small"><b>Why it qualified:</b> ${esc((k.why_it_qualified || []).map((g) => `${g.gate} ${g.observed == null ? "n/a" : Number(g.observed).toFixed(3)} vs ${g.limit}`).join(" · "))}</div>
      <div class="cc-small"><b>Would invalidate it:</b><ul class="cc-small">${(k.invalidation_during_paper_forward_test || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
      <div class="cc-small cc-dimtext"><b>Limitations:</b><ul class="cc-small">${(k.known_limitations || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div></div>`;
  }

  function runsCard() {
    if (!runs.length) return "";
    return `<section class="cc-card"><div class="cc-head"><h2>STORED CAMPAIGNS</h2><span class="cc-small cc-dimtext">immutable · newest first</span></div>
      <div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Campaign</th><th>Status</th><th>Candidates</th><th>Eligible</th><th>Finalists</th><th></th></tr></thead><tbody>
      ${runs.map((r) => `<tr><td><code>${esc(r.campaign_id.slice(0, 8))}</code></td><td>${tag(r.status, KIND[r.status] || "dim")}</td><td>${esc(r.n_candidates)}</td><td>${esc(r.n_eligible)}</td><td>${esc(r.n_finalists)}</td>
        <td><button type="button" class="cc-btn cc-mini" data-pmc-act="open" data-id="${esc(r.campaign_id)}"${busy ? " disabled" : ""}>Open</button></td></tr>`).join("")}</tbody></table></div></section>`;
  }

  function draw() {
    root.innerHTML = `<div class="pmc"><div class="prt-banner" role="note">${esc(BANNER)}</div>
      ${notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}${controls()}${resultCard()}${runsCard()}
      <p class="cc-small cc-dimtext">${esc(cfg ? cfg.note : "")}</p></div>`;
  }
  function show() {
    if (!root.dataset.pmcBound) { root.dataset.pmcBound = "1"; root.addEventListener("click", onClick); root.addEventListener("change", () => { readForm(); draw(); }); }
    draw();
    load();
  }

  window.StrategyFit.addWorkspace({ id: "campaign50", label: "Model Campaign", cls: "pmc-mode", show });
  window.ModelCampaign = { get state() { return { busy, notice, requests, configs: configs.length, runs: runs.length, run: detail ? { id: detail.run.campaign_id, status: detail.run.status } : null, view, ready: ready() }; } };
})();
