// portfolio_diagnostics.js — Stage 5.1 ATTRIBUTION & BENCHMARK DIAGNOSTICS (a Strategy Lab workspace). RESEARCH ONLY — NOTHING IS TRADED, ACTIVATED OR PROMOTED.
//
// Pick a stored campaign, run the diagnostics, read the benchmark comparison, symbol and sector attribution, leave-one-out and
// leave-sector-out ablations, OOS windows, regime-conditioned excess and the PASS / WARN / FAIL scorecard. No broker button, no
// promotion button, nothing is written into any form. No timers, no polling, nothing on page load; every value is escaped.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("pdx-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const pct = (x, d = 2) => (x == null ? "—" : `${(Number(x) * 100).toFixed(d)}%`);
  const num = (x, d = 2) => (x == null ? "—" : Number(x).toFixed(d));
  const BASE = "/api/rotation-diagnostics";
  const BANNER = "ATTRIBUTION & BENCHMARK DIAGNOSTICS — RESEARCH ONLY — NOTHING IS TRADED, ACTIVATED OR PROMOTED";
  const VK = { PASS: "ok", WARN: "warn", FAIL: "alert" };

  let cfg = null, runs = [], busy = "", notice = "", requests = 0, seq = 0, campaign = "";
  let detail = null, bench = null, attr = null, abl = null, wins = null, regimes = null, view = "scorecard", which = "";

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
    if (c.status === 200) { cfg = c.body; if (!campaign && cfg.campaigns.length) campaign = cfg.campaigns[0].campaign_id; }
    if (r.status === 200) runs = r.body.runs || [];
    draw();
  }

  async function runDiag() {
    if (!campaign || busy) return;
    busy = "run"; notice = ""; draw();
    const r = await api("POST", `${BASE}/run`, { campaign_id: campaign });
    busy = "";
    if (r.status !== 200) { notice = `${r.body.status || r.status}: ${r.body.message || "The diagnostics could not run."}`; draw(); return; }
    await open(r.body.run.diag_id);
    const l = await api("GET", `${BASE}/runs?limit=20`);
    if (l.status === 200) runs = l.body.runs || [];
    draw();
  }

  async function open(id) {
    busy = "open"; draw();
    const [d, b, a, x, w, g] = await Promise.all([api("GET", `${BASE}/runs/${id}`), api("GET", `${BASE}/runs/${id}/benchmarks`), api("GET", `${BASE}/runs/${id}/attribution`),
      api("GET", `${BASE}/runs/${id}/ablation`), api("GET", `${BASE}/runs/${id}/windows`), api("GET", `${BASE}/runs/${id}/regimes`)]);
    busy = "";
    if (d.status !== 200) { notice = "The run could not be loaded."; draw(); return; }
    detail = d.body; bench = b.status === 200 ? b.body : null; attr = a.status === 200 ? a.body : null; abl = x.status === 200 ? x.body : null;
    wins = w.status === 200 ? w.body : null; regimes = g.status === 200 ? g.body.regimes : null;
    which = detail.configs.length ? detail.configs[0].config_hash : "";
    draw();
  }

  function onClick(ev) {
    const b = ev.target.closest("[data-pdx-act]");
    if (!b || b.disabled) return;
    const a = b.dataset.pdxAct;
    if (a === "run") runDiag();
    else if (a === "open") open(b.dataset.id);
    else if (a === "view") { view = b.dataset.view; draw(); }
    else if (a === "which") { which = b.dataset.hash; draw(); }
  }

  function cell(label, value) { return `<div><span class="cc-label">${esc(label)}</span><b>${value}</b></div>`; }
  const strat = () => (bench && bench.strategies ? bench.strategies.find((s) => s.config_hash === which) : null);

  function controls() {
    const d = busy ? " disabled" : "";
    const opts = (cfg ? cfg.campaigns : []).map((c) => `<option value="${esc(c.campaign_id)}"${c.campaign_id === campaign ? " selected" : ""}>${esc(c.campaign_id.slice(0, 8))} · ${esc(c.status)} · ${esc(c.n_candidates)} candidates · ${esc(c.n_finalists)} finalists · ${esc(c.run_at)}</option>`).join("");
    return `<section class="cc-card"><div class="cc-head"><h2>DIAGNOSTICS / ATTRIBUTION</h2><span class="cc-small cc-dimtext">equal-weight and buy-and-hold universe benchmarks · attribution · ablations · research only</span></div>
      <div class="pdx-row"><label><span class="cc-label">STORED CAMPAIGN</span><select data-pdx-f="campaign"${d}>${opts || '<option value="">No completed campaign — run one in Model Campaign</option>'}</select></label>
        <button type="button" class="cc-btn cc-primary" data-pdx-act="run"${!campaign || busy ? " disabled" : ""}>${busy === "run" ? "Running…" : "Run Diagnostics"}</button>
        <span class="cc-small cc-dimtext">${esc(cfg ? cfg.universe_note : "")}</span></div></section>`;
  }

  function resultCard() {
    if (!detail) return `<section class="cc-card"><p class="cc-small cc-dimtext">Run diagnostics on a stored campaign or open a stored run.</p></section>`;
    const r = detail.run;
    const head = `<div class="cc-head"><h2>RUN ${esc((r.diag_id || "").slice(0, 8))}</h2>${tag(r.status, r.status === "COMPLETED" ? "ok" : "alert")}
      <span class="cc-small cc-dimtext">campaign ${esc((r.campaign_id || "").slice(0, 8))} · ${esc(r.start_date)} → ${esc(r.end_date)} · ${esc(r.n_windows)} windows · ${esc(r.n_configs)} configs · ${esc(r.n_evaluations)} replays · ${esc(r.runtime_s)} s · flags ${esc(r.flags_version)}</span></div>`;
    if (r.status !== "COMPLETED") return `<section class="cc-card">${head}<div class="cc-small prt-why">Failed closed: ${esc(r.failure_code)} — ${esc(r.failure_detail)}</div></section>`;
    const pick = detail.configs.map((c) => `<button type="button" class="cc-btn cc-mini${which === c.config_hash ? " cc-primary" : ""}" data-pdx-act="which" data-hash="${esc(c.config_hash)}">${esc(c.role)} · ${esc(c.label)}</button>`).join(" ");
    const views = ["scorecard", "benchmarks", "attribution", "ablation", "windows", "regimes"].map((v) => `<button type="button" class="cc-btn cc-mini${view === v ? " cc-primary" : ""}" data-pdx-act="view" data-view="${v}">${esc(v)}</button>`).join(" ");
    const c = detail.configs.find((x) => x.config_hash === which) || detail.configs[0];
    let body = "";
    if (view === "scorecard") {
      body = `<div class="pdx-row">${(c.flags || []).map((f) => tag(f, f === "ROTATION_VALUE_ADDED" ? "ok" : "warn")).join(" ") || tag("no flags", "dim")}</div>
        <div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Check</th><th>Verdict</th><th>Value</th></tr></thead><tbody>
        ${(c.scorecard || []).map((s) => `<tr><td>${esc(s.check)}</td><td>${tag(s.verdict, VK[s.verdict] || "dim")}</td><td class="cc-small">${esc(typeof s.value === "object" ? JSON.stringify(s.value) : s.value)}</td></tr>`).join("")}</tbody></table></div>
        <p class="cc-small cc-dimtext">${esc((cfg && cfg.flags && cfg.flags.note) || "")}</p>`;
    } else if (view === "benchmarks") {
      const s = strat();
      const rows = s ? [["strategy", s.stitched.strategy], ["EW_REBALANCED", s.stitched.EW_REBALANCED], ["BUY_HOLD", s.stitched.BUY_HOLD], ["SPY", s.stitched.SPY]] : [];
      body = `<div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Curve</th><th>Total return</th><th>CAGR</th><th>Sharpe</th><th>Sortino</th><th>Max DD</th><th>DD sessions</th><th>Worst month</th></tr></thead><tbody>
        ${rows.map(([k, m]) => `<tr><td>${esc(k)}</td><td>${pct(m.total_return)}</td><td>${pct(m.cagr)}</td><td>${num(m.sharpe)}</td><td>${num(m.sortino)}</td><td>${pct(m.max_drawdown)}</td><td>${esc(m.max_drawdown_sessions)}</td><td>${m.worst_month ? `${esc(m.worst_month.month)} ${pct(m.worst_month.return)}` : "—"}</td></tr>`).join("")}</tbody></table></div>
        <h3 class="cc-small">Cost-adjusted excess</h3><div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Cost / slip bps</th><th>Strategy CAGR</th><th>EW CAGR</th><th>BH CAGR</th><th>Excess vs SPY</th><th>Excess vs EW</th><th>Excess vs BH</th></tr></thead><tbody>
        ${s ? s.cost.rows.map((x) => `<tr><td>${esc(x.transaction_cost_bps)} / ${esc(x.slippage_bps)}</td><td>${pct(x.strategy.cagr)}</td><td>${pct(x.EW_REBALANCED.cagr)}</td><td>${pct(x.BUY_HOLD.cagr)}</td><td>${pct(x.excess_vs_spy)}</td><td>${pct(x.excess_vs_ew)}</td><td>${pct(x.excess_vs_bh)}</td></tr>`).join("") : ""}</tbody></table></div>
        <div class="cc-small cc-dimtext">first non-positive excess vs EW at ${esc(s && s.cost.first_nonpositive_vs_ew != null ? s.cost.first_nonpositive_vs_ew + " bps" : "none of the tested points")} · vs SPY at ${esc(s && s.cost.first_nonpositive_vs_spy != null ? s.cost.first_nonpositive_vs_spy + " bps" : "none")}</div>`;
    } else if (view === "attribution") {
      const syms = (attr ? attr.symbols : []).filter((x) => x.config_hash === which);
      const secs = (attr ? attr.sectors : []).filter((x) => x.config_hash === which);
      const h = attr && attr.holdings ? attr.holdings[which] : null;
      body = `${h ? `<div class="pdx-facts">${cell("Names held (mean)", num(h.names_held_mean, 1))}${cell("Overlap with previous", pct(h.overlap_mean, 0))}${cell("Added / removed per rebalance", `${num(h.added_mean, 1)} / ${num(h.removed_mean, 1)}`)}${cell("Churn", pct(h.churn_mean, 1))}${cell("Top-1 / 3 / 5 share", `${pct(h.concentration.top1, 0)} / ${pct(h.concentration.top3, 0)} / ${pct(h.concentration.top5, 0)}`)}${cell("Top sector share / max weight", `${pct(h.sector_concentration.top_sector_share, 0)} / ${pct(h.sector_concentration.max_sector_weight, 0)}`)}${cell("Best window share of log return", pct(h.window_concentration.best_window_share_of_log_return, 0))}</div>` : ""}
        <div class="pdx-two"><div class="sf-tablewrap pdx-scroll"><table class="sf-table"><thead><tr><th>Symbol</th><th>Sector</th><th>P&amp;L</th><th>Share of +P&amp;L</th><th>Windows held</th></tr></thead><tbody>
        ${syms.map((x) => `<tr><td>${esc(x.symbol)}</td><td>${esc(x.sector)}</td><td>${esc(x.pnl)}</td><td>${pct(x.share_of_positive)}</td><td>${esc(x.windows_held)}</td></tr>`).join("")}</tbody></table></div>
        <div class="sf-tablewrap pdx-scroll"><table class="sf-table"><thead><tr><th>Sector</th><th>P&amp;L</th><th>Share of +P&amp;L</th><th>Avg weight</th><th>Max weight</th></tr></thead><tbody>
        ${secs.map((x) => `<tr><td>${esc(x.sector)}</td><td>${esc(x.pnl)}</td><td>${pct(x.share_of_positive)}</td><td>${pct(x.avg_weight)}</td><td>${pct(x.max_weight)}</td></tr>`).join("")}</tbody></table></div></div>`;
    } else if (view === "ablation") {
      const loo = (abl ? abl.leave_one_out : []).filter((x) => x.config_hash === which);
      const lso = (abl ? abl.leave_sector_out : []).filter((x) => x.config_hash === which);
      const row = (x, k) => `<tr class="${x.dominant || x.dependent ? "prt-why" : ""}"><td>${esc(x[k])}</td><td>${esc(x.status)}</td><td>${pct(x.deltas.cagr)}</td><td>${num(x.deltas.sharpe)}</td><td>${pct(x.deltas.max_drawdown)}</td><td>${pct(x.deltas.excess_return)}</td><td>${x.dominant || x.dependent ? tag(k === "symbol" ? "DOMINANT_CONTRIBUTOR" : "SECTOR_DEPENDENCE", "warn") : ""}</td></tr>`;
      body = `<p class="cc-small cc-dimtext">${esc(abl ? abl.rule : "")}</p><div class="pdx-two">
        <div class="sf-tablewrap pdx-scroll"><table class="sf-table"><thead><tr><th>Without symbol</th><th>Status</th><th>Δ CAGR</th><th>Δ Sharpe</th><th>Δ max DD</th><th>Δ excess vs SPY</th><th></th></tr></thead><tbody>${loo.map((x) => row(x, "symbol")).join("")}</tbody></table></div>
        <div class="sf-tablewrap pdx-scroll"><table class="sf-table"><thead><tr><th>Without sector</th><th>Status</th><th>Δ CAGR</th><th>Δ Sharpe</th><th>Δ max DD</th><th>Δ excess vs SPY</th><th></th></tr></thead><tbody>${lso.map((x) => row(x, "sector")).join("")}</tbody></table></div></div>`;
    } else if (view === "windows") {
      const rows = (wins ? wins.windows : []).filter((x) => x.config_hash === which);
      const s = wins && wins.summary ? wins.summary[which] : null;
      body = `${s ? `<div class="pdx-facts">${cell("Beats SPY / EW / BH", `${pct(s.pct_beating_SPY, 0)} / ${pct(s.pct_beating_EW_REBALANCED, 0)} / ${pct(s.pct_beating_BUY_HOLD, 0)}`)}${cell("Median excess vs SPY / EW / BH", `${pct(s.median_excess_vs_spy)} / ${pct(s.median_excess_vs_ew)} / ${pct(s.median_excess_vs_bh)}`)}${cell("Worst window vs EW", esc(JSON.stringify(s.worst_relative_window_vs_ew)))}</div>` : ""}
        <div class="sf-tablewrap pdx-scroll"><table class="sf-table"><thead><tr><th>#</th><th>Test window</th><th>Strategy</th><th>SPY</th><th>EW</th><th>BH</th><th>Excess vs EW</th><th>Max DD</th><th>Turnover</th><th>Top contributors</th><th>Trend-up share</th></tr></thead><tbody>
        ${rows.map((x) => `<tr><td>${esc(x.window_index)}</td><td>${esc(x.test_first_session)} → ${esc(x.test_last_session)}</td><td>${pct(x.strategy_return)}</td><td>${pct(x.spy_return)}</td><td>${pct(x.ew_return)}</td><td>${pct(x.bh_return)}</td><td>${pct(x.excess_vs_ew)}</td><td>${pct(x.strategy_max_drawdown)}</td><td>${pct(x.mean_turnover)}</td><td class="cc-small">${esc((x.dominant_contributors || []).map((t) => t[0]).join(", "))}</td><td>${pct(x.regime_mix ? x.regime_mix.trend_up_share : null, 0)}</td></tr>`).join("")}</tbody></table></div>`;
    } else {
      const g = regimes ? regimes[which] : null;
      const rows = g && g.rows ? Object.entries(g.rows) : [];
      body = `${g && g.weak && g.weak.length ? `<div class="pdx-row">${tag("WEAK_REGIME_SAMPLE", "warn")} <span class="cc-small">${esc(g.weak.join(", "))}</span></div>` : ""}
        <div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Regime</th><th>Sessions</th><th>Share</th><th>Strategy</th><th>SPY</th><th>EW</th><th>Excess vs EW</th><th>Exposure</th><th>Turnover</th></tr></thead><tbody>
        ${rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v.sessions)}</td><td>${pct(v.share, 0)}</td><td>${pct(v.strategy_return)}</td><td>${pct(v.spy_return)}</td><td>${pct(v.ew_return)}</td><td>${pct(v.excess_vs_ew)}</td><td>${pct(v.mean_exposure, 0)}</td><td>${pct(v.mean_turnover)}</td></tr>`).join("")}</tbody></table></div>`;
    }
    return `<section class="cc-card">${head}<div class="pdx-row">${pick}</div><div class="pdx-row">${views}</div>${body}</section>`;
  }

  function runsCard() {
    if (!runs.length) return "";
    return `<section class="cc-card"><div class="cc-head"><h2>STORED DIAGNOSTIC RUNS</h2><span class="cc-small cc-dimtext">immutable · newest first</span></div>
      <div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Run</th><th>Campaign</th><th>Status</th><th>Configs</th><th>Windows</th><th></th></tr></thead><tbody>
      ${runs.map((r) => `<tr><td><code>${esc(r.diag_id.slice(0, 8))}</code></td><td><code>${esc((r.campaign_id || "").slice(0, 8))}</code></td><td>${tag(r.status, r.status === "COMPLETED" ? "ok" : "alert")}</td><td>${esc(r.n_configs)}</td><td>${esc(r.n_windows)}</td>
        <td><button type="button" class="cc-btn cc-mini" data-pdx-act="open" data-id="${esc(r.diag_id)}"${busy ? " disabled" : ""}>Open</button></td></tr>`).join("")}</tbody></table></div></section>`;
  }

  function draw() {
    root.innerHTML = `<div class="pdx"><div class="prt-banner" role="note">${esc(BANNER)}</div>
      ${notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}${controls()}${resultCard()}${runsCard()}
      <p class="cc-small cc-dimtext">${esc(cfg ? cfg.note : "")}</p></div>`;
  }
  function show() {
    if (!root.dataset.pdxBound) {
      root.dataset.pdxBound = "1";
      root.addEventListener("click", onClick);
      root.addEventListener("change", (ev) => { const el = ev.target.closest("[data-pdx-f]"); if (el && el.dataset.pdxF === "campaign") { campaign = el.value; draw(); } });
    }
    draw();
    load();
  }

  window.StrategyFit.addWorkspace({ id: "diagnostics51", label: "Diagnostics", cls: "pdx-mode", show });
  window.RotationDiagnostics = { get state() { return { busy, notice, requests, campaign, runs: runs.length, run: detail ? { id: detail.run.diag_id, status: detail.run.status } : null, view, which }; } };
})();
