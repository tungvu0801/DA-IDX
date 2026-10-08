// portfolio_backtest.js — Stage 4.8 HISTORICAL ROTATION BACKTEST (a Strategy Lab workspace). RESEARCH ONLY — NOTHING IS TRADED.
//
// Pick a saved Portfolio Rotation configuration version, a universe, a date range, a rebalance frequency and the cost
// assumptions, press Run Backtest, and read the deterministic replay: summary metrics (documented conventions), the
// equity series, the rebalance history and the simulated fills. No broker button, nothing is written into any form, no link to any order form.
// No timers, no polling, nothing on page load; every value is escaped.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("pbt-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const pct = (x, d = 2) => (x == null ? "—" : `${(Number(x) * 100).toFixed(d)}%`);
  const num = (x, d = 2) => (x == null ? "—" : Number(x).toFixed(d));
  const usd = (s) => (s == null ? "—" : `$${Number(s).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
  const BASE = "/api/rotation-replay";
  const BANNER = "HISTORICAL ROTATION BACKTEST — RESEARCH ONLY — NOTHING IS TRADED";
  const KIND = { COMPLETED: "ok", FAILED: "alert" };

  let cfg = null, configs = [], runs = [], busy = "", notice = "", requests = 0, seq = 0;
  let form = { config_id: "", universe: "CUSTOM", ref: "", symbols: "", start_date: "", end_date: "", rebalance_frequency: "MONTHLY",
    initial_cash: "100000.00", transaction_cost_bps: "5", slippage_bps: "5" };
  let result = null, detail = null, equity = [], trades = [], rebalances = [], view = "metrics";

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

  function readForm() { root.querySelectorAll("[data-pbt-f]").forEach((el) => { form[el.dataset.pbtF] = el.value; }); }
  const chosen = () => configs.find((c) => c.config_id === form.config_id);
  const symbolsList = () => [...new Set(form.symbols.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean))];
  const ready = () => !!(chosen() && form.start_date && form.end_date && (form.universe !== "CUSTOM" || symbolsList().length) &&
    (!["SAVED_SCAN", "SAVED_UNIVERSE"].includes(form.universe) || form.ref));

  async function runBacktest() {
    const c = chosen();
    if (!c || busy) return;
    busy = "run"; notice = ""; draw();
    const universe = { source: form.universe, ref: ["SAVED_SCAN", "SAVED_UNIVERSE"].includes(form.universe) ? form.ref : null,
      symbols: form.universe === "CUSTOM" ? symbolsList() : null };
    const r = await api("POST", `${BASE}/run`, { config_id: c.config_id, config_hash: c.config_hash, universe, start_date: form.start_date,
      end_date: form.end_date, rebalance_frequency: form.rebalance_frequency, initial_cash: form.initial_cash,
      transaction_cost_bps: form.transaction_cost_bps, slippage_bps: form.slippage_bps });
    busy = "";
    if (r.status !== 200) { notice = `${r.body.status || r.status}: ${r.body.message || "The backtest could not run."}`; draw(); return; }
    result = r.body;
    await open(result.run.run_id);
    const l = await api("GET", `${BASE}/runs?limit=20`);
    if (l.status === 200) runs = l.body.runs || [];
    draw();
  }

  async function open(id) {
    busy = "open"; draw();
    const [d, e, t, b] = await Promise.all([api("GET", `${BASE}/runs/${id}`), api("GET", `${BASE}/runs/${id}/equity`),
      api("GET", `${BASE}/runs/${id}/fills`), api("GET", `${BASE}/runs/${id}/rebalances`)]);
    busy = "";
    if (d.status !== 200) { notice = "The run could not be loaded."; draw(); return; }
    detail = d.body; equity = e.status === 200 ? e.body.equity : []; trades = t.status === 200 ? t.body.trades : [];
    rebalances = b.status === 200 ? b.body.rebalances : [];
    draw();
  }

  function onClick(ev) {
    const b = ev.target.closest("[data-pbt-act]");
    if (!b || b.disabled) return;
    const a = b.dataset.pbtAct;
    if (a === "run") { readForm(); runBacktest(); }
    else if (a === "open") open(b.dataset.id);
    else if (a === "view") { view = b.dataset.view; draw(); }
  }

  // ---- rendering ---------------------------------------------------------------------------------------------------------
  function controls() {
    const d = busy ? " disabled" : "";
    const opts = configs.map((c) => `<option value="${esc(c.config_id)}"${c.config_id === form.config_id ? " selected" : ""}>${esc(c.name)} v${esc(c.version)} · size ${esc(c.config.portfolio_size)} · exit ${esc(c.config.exit_rank)}</option>`).join("");
    const sel = (k, values) => `<select data-pbt-f="${k}"${d}>${values.map((v) => `<option value="${esc(v)}"${form[k] === v ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
    const inp = (k, type = "text", extra = "") => `<input data-pbt-f="${k}" type="${type}" value="${esc(form[k])}" ${extra}${d}>`;
    return `<section class="cc-card pbt-controls"><div class="cc-head"><h2>HISTORICAL BACKTEST</h2><span class="cc-small cc-dimtext">Stage 4.7 model · signal at a completed close · fill at the next open · whole shares · research only</span></div>
      <div class="pbt-grid">
        <label><span class="cc-label">ROTATION CONFIG</span><select data-pbt-f="config_id"${d}>${opts || '<option value="">No saved configuration — create one in Portfolio Rotation</option>'}</select></label>
        <label><span class="cc-label">UNIVERSE</span>${sel("universe", cfg ? cfg.universe_sources : ["CUSTOM"])}</label>
        ${["SAVED_SCAN", "SAVED_UNIVERSE"].includes(form.universe) ? `<label><span class="cc-label">REFERENCE ID</span>${inp("ref", "text", 'maxlength="64" placeholder="saved scan / version id"')}</label>` : ""}
        ${form.universe === "CUSTOM" ? `<label class="pbt-wide"><span class="cc-label">SYMBOLS</span>${inp("symbols", "text", 'placeholder="AMD, MU, KO …"')}</label>` : ""}
        <label><span class="cc-label">START</span>${inp("start_date", "date")}</label>
        <label><span class="cc-label">END</span>${inp("end_date", "date")}</label>
        <label><span class="cc-label">REBALANCE</span>${sel("rebalance_frequency", cfg ? cfg.frequencies : ["WEEKLY", "MONTHLY"])}</label>
        <label><span class="cc-label">INITIAL CASH</span>${inp("initial_cash", "text", 'maxlength="20"')}</label>
        <label><span class="cc-label">COST (bps)</span>${inp("transaction_cost_bps", "text", 'maxlength="12"')}</label>
        <label><span class="cc-label">SLIPPAGE (bps)</span>${inp("slippage_bps", "text", 'maxlength="12"')}</label>
      </div>
      <div class="pbt-row"><button type="button" class="cc-btn cc-primary" data-pbt-act="run"${!ready() || busy ? " disabled" : ""}>${busy === "run" ? "Running…" : "Run Backtest"}</button>
        <span class="cc-small cc-dimtext">${esc(cfg ? cfg.universe_note : "")}</span></div></section>`;
  }

  function metricsCard() {
    if (!detail) return `<section class="cc-card"><p class="cc-small cc-dimtext">Run a backtest or open a stored run to see its results.</p></section>`;
    const r = detail.run, m = detail.metrics || {};
    const cell = (label, value) => `<div><span class="cc-label">${esc(label)}</span><b>${value}</b></div>`;
    const head = `<div class="cc-head"><h2>RUN ${esc((r.run_id || "").slice(0, 8))}</h2>${tag(r.status, KIND[r.status] || "dim")}
      <span class="cc-small cc-dimtext">${esc(r.first_session || "")} → ${esc(r.last_session || "")} · ${esc(r.n_sessions)} sessions · ${esc(r.n_rebalances_executed)}/${esc(r.n_rebalances)} rebalances executed · ${esc(r.n_trades)} fills</span></div>`;
    if (r.status !== "COMPLETED") return `<section class="cc-card">${head}<div class="cc-small prt-why">Failed closed: ${esc(r.failure_code)} — ${esc(r.failure_detail)}</div></section>`;
    const views = ["metrics", "equity", "rebalances", "trades"].map((v) => `<button type="button" class="cc-btn cc-mini${view === v ? " cc-primary" : ""}" data-pbt-act="view" data-view="${v}">${esc(v)}</button>`).join(" ");
    let body = "";
    if (view === "metrics") {
      body = `<div class="pbt-facts">
        ${cell("Total return", pct(m.total_return))}${cell("CAGR", pct(m.cagr))}${cell("Benchmark total", pct(m.benchmark_total_return))}${cell("Benchmark CAGR", pct(m.benchmark_cagr))}
        ${cell("Excess return", pct(m.excess_return))}${cell("Annualized excess (simple)", pct(m.annualized_excess))}${cell("Volatility (ann.)", pct(m.annualized_volatility))}
        ${cell("Sharpe (rf 0)", num(m.sharpe))}${cell("Sortino (rf 0)", num(m.sortino))}${cell("Max drawdown", pct(m.max_drawdown))}${cell("Max DD sessions", esc(m.max_drawdown_sessions))}
        ${cell("Mean turnover / rebalance", pct(m.mean_turnover))}${cell("Traded notional", usd(m.traded_notional))}${cell("Transaction costs", usd(m.total_transaction_costs))}
        ${cell("Completed positions", esc(m.n_completed_positions))}${cell("Win rate", pct(m.win_rate))}${cell("Avg holding (sessions)", num(m.average_holding_sessions, 1))}
        ${cell("Best month", m.best_month ? `${esc(m.best_month.month)} ${pct(m.best_month.return)}` : "—")}${cell("Worst month", m.worst_month ? `${esc(m.worst_month.month)} ${pct(m.worst_month.return)}` : "—")}
        ${cell("Worst rolling 3m", pct(m.worst_rolling_3m))}${cell("Cash weight mean / min / max", `${pct(m.cash_weight_mean)} / ${pct(m.cash_weight_min)} / ${pct(m.cash_weight_max)}`)}
        ${cell("Final equity", usd(m.final_equity))}${cell("Initial cash", usd(m.initial_cash))}</div>
        <div class="cc-small cc-dimtext">${esc(r.universe_note)} · hashes: definition ${esc((r.backtest_config_hash || "").slice(0, 12))}, data ${esc((r.data_hash || "").slice(0, 12))}, result ${esc((r.result_hash || "").slice(0, 12))}</div>`;
    } else if (view === "equity") {
      body = `<div class="sf-tablewrap pbt-scroll"><table class="sf-table"><thead><tr><th>Session</th><th>Equity</th><th>Cash</th><th>Positions</th><th>Benchmark</th><th>#</th><th>Cash %</th></tr></thead><tbody>
        ${equity.map((e) => `<tr><td>${esc(e.session_date)}</td><td>${usd(e.equity)}</td><td>${usd(e.cash)}</td><td>${usd(e.positions_value)}</td><td>${usd(e.benchmark_index)}</td><td>${esc(e.n_positions)}</td><td>${pct(e.cash_weight)}</td></tr>`).join("")}</tbody></table></div>`;
    } else if (view === "rebalances") {
      body = `<div class="sf-tablewrap pbt-scroll"><table class="sf-table"><thead><tr><th>#</th><th>Signal</th><th>Executed</th><th>Engine status</th><th>Eligible</th><th>Selected</th><th>Turnover</th><th>Orders</th><th>Fills</th><th>Traded</th><th>Cost</th></tr></thead><tbody>
        ${rebalances.map((b) => `<tr><td>${esc(b.seq)}</td><td>${esc(b.signal_session)}</td><td>${esc(b.execution_session || b.skip_reason || "—")}</td><td>${esc(b.engine_status)}</td><td>${esc(b.n_eligible)}</td><td>${esc(b.n_selected)}</td><td>${pct(b.turnover)}</td><td>${esc(b.n_orders)}</td><td>${esc(b.n_fills)}</td><td>${usd(b.traded_notional)}</td><td>${usd(b.total_cost)}</td></tr>`).join("")}</tbody></table></div>`;
    } else {
      body = `<div class="sf-tablewrap pbt-scroll"><table class="sf-table"><thead><tr><th>#</th><th>Signal</th><th>Fill session</th><th>Symbol</th><th>Side</th><th>Action</th><th>Req</th><th>Filled</th><th>Open</th><th>Fill</th><th>Notional</th><th>Cost</th><th>P&amp;L</th><th>Note</th></tr></thead><tbody>
        ${trades.map((t) => `<tr><td>${esc(t.seq)}</td><td>${esc(t.signal_session)}</td><td>${esc(t.execution_session)}</td><td>${esc(t.symbol)}</td><td>${esc(t.side)}</td><td>${esc(t.action)}</td><td>${esc(t.requested_qty)}</td><td>${esc(t.filled_qty)}</td><td>${num(t.open_price, 4)}</td><td>${num(t.fill_price, 4)}</td><td>${usd(t.notional)}</td><td>${usd(t.cost)}</td><td>${t.realised_pnl == null ? "—" : usd(t.realised_pnl)}</td><td>${esc(t.note || "")}</td></tr>`).join("")}</tbody></table></div>
        <p class="cc-small cc-dimtext">Simulated fills of a historical replay — nothing here was or can be sent to a broker.</p>`;
    }
    return `<section class="cc-card">${head}<div class="pbt-row">${views}</div>${body}</section>`;
  }

  function runsCard() {
    if (!runs.length) return "";
    return `<section class="cc-card"><div class="cc-head"><h2>STORED RUNS</h2><span class="cc-small cc-dimtext">immutable · newest first</span></div>
      <div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Run</th><th>Status</th><th>Range</th><th>Rebalances</th><th>Fills</th><th>Final equity</th><th></th></tr></thead><tbody>
      ${runs.map((r) => `<tr><td><code>${esc(r.run_id.slice(0, 8))}</code></td><td>${tag(r.status, KIND[r.status] || "dim")}</td><td>${esc(r.first_session || "")} → ${esc(r.last_session || "")}</td><td>${esc(r.n_rebalances_executed)}/${esc(r.n_rebalances)}</td><td>${esc(r.n_trades)}</td><td>${usd(r.final_equity)}</td>
        <td><button type="button" class="cc-btn cc-mini" data-pbt-act="open" data-id="${esc(r.run_id)}"${busy ? " disabled" : ""}>Open</button></td></tr>`).join("")}</tbody></table></div></section>`;
  }

  function draw() {
    root.innerHTML = `<div class="pbt"><div class="prt-banner" role="note">${esc(BANNER)}</div>
      ${notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}${controls()}${metricsCard()}${runsCard()}
      <p class="cc-small cc-dimtext">${esc(cfg ? cfg.note : "")}</p></div>`;
  }
  function show() {
    if (!root.dataset.pbtBound) { root.dataset.pbtBound = "1"; root.addEventListener("click", onClick); root.addEventListener("change", () => { readForm(); draw(); }); }
    draw();
    load();
  }

  window.StrategyFit.addWorkspace({ id: "backtest48", label: "Rotation Backtest", cls: "pbt-mode", show });
  window.PortfolioBacktest = { get state() { return { busy, notice, requests, configs: configs.length, runs: runs.length, run: detail ? { id: detail.run.run_id, status: detail.run.status } : null, view, ready: ready() }; } };
})();
