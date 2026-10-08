// portfolio_signal_research.js — Stage 5.2 DETERMINISTIC SIGNAL RESEARCH (a Strategy Lab workspace). RESEARCH ONLY — NOTHING IS TRADED, ACTIVATED OR PROMOTED.
//
// Pick a stored campaign, run the fixed research set (factor ablation, sector-neutral ranking, sector caps, regime exposure
// overlay, two fixed combinations), read the comparisons against SPY / equal-weight / buy-and-hold, the factor verdicts, the
// research flags, the predeclared criteria and the scorecard. No broker button, no promotion button, nothing is written into
// any form. No timers, no polling, nothing on page load; every value is escaped.
(function () {
  const tabEl = document.getElementById("tab-strategy");
  const root = document.getElementById("psr-body");
  if (!tabEl || !root || !window.StrategyFit || !window.StrategyFit.addWorkspace) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const pct = (x, d = 2) => (x == null ? "—" : `${(Number(x) * 100).toFixed(d)}%`);
  const num = (x, d = 2) => (x == null ? "—" : Number(x).toFixed(d));
  const BASE = "/api/signal-research";
  const BANNER = "SIGNAL RESEARCH — RESEARCH ONLY — NOTHING IS TRADED, ACTIVATED OR PROMOTED";
  const VK = { PASS: "ok", WARN: "warn", FAIL: "alert", FACTOR_HELPFUL: "ok", FACTOR_NEUTRAL: "dim", FACTOR_HARMFUL: "alert" };
  const GOOD = { IMPROVEMENT_CRITERIA_MET: 1, CONCENTRATION_REDUCED: 1, COST_ROBUSTNESS_IMPROVED: 1, DRAWDOWN_IMPROVED: 1, SECTOR_NEUTRALITY_HELPFUL: 1, REGIME_OVERLAY_HELPFUL: 1 };

  let cfg = null, runs = [], busy = "", notice = "", requests = 0, seq = 0, campaign = "";
  let detail = null, abl = null, sector = null, regime = null, combos = null, bench = null, cards = null, view = "scorecard", which = "";

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

  async function runResearch() {
    if (!campaign || busy) return;
    busy = "run"; notice = ""; draw();
    const r = await api("POST", `${BASE}/run`, { campaign_id: campaign });
    busy = "";
    if (r.status !== 200) { notice = `${r.body.status || r.status}: ${r.body.message || "The research run could not start."}`; draw(); return; }
    await open(r.body.run.run_id);
    const l = await api("GET", `${BASE}/runs?limit=20`);
    if (l.status === 200) runs = l.body.runs || [];
    draw();
  }

  async function open(id) {
    busy = "open"; draw();
    const [d, a, s, g, k, b, c] = await Promise.all([api("GET", `${BASE}/runs/${id}`), api("GET", `${BASE}/runs/${id}/ablation`), api("GET", `${BASE}/runs/${id}/sector`),
      api("GET", `${BASE}/runs/${id}/regime`), api("GET", `${BASE}/runs/${id}/combinations`), api("GET", `${BASE}/runs/${id}/benchmarks`), api("GET", `${BASE}/runs/${id}/scorecard`)]);
    busy = "";
    if (d.status !== 200) { notice = "The run could not be loaded."; draw(); return; }
    detail = d.body; abl = a.status === 200 ? a.body : null; sector = s.status === 200 ? s.body : null; regime = g.status === 200 ? g.body : null;
    combos = k.status === 200 ? k.body : null; bench = b.status === 200 ? b.body : null; cards = c.status === 200 ? c.body : null;
    which = detail.variants.length ? detail.variants[0].config_hash : "";
    draw();
  }

  function onClick(ev) {
    const b = ev.target.closest("[data-psr-act]");
    if (!b || b.disabled) return;
    const a = b.dataset.psrAct;
    if (a === "run") runResearch();
    else if (a === "open") open(b.dataset.id);
    else if (a === "view") { view = b.dataset.view; draw(); }
    else if (a === "which") { which = b.dataset.hash; draw(); }
  }

  function cell(label, value) { return `<div><span class="cc-label">${esc(label)}</span><b>${value}</b></div>`; }
  const labelOf = (h) => { const v = detail ? detail.variants.find((x) => x.config_hash === h) : null; return v ? v.label : (h || "").slice(0, 8); };
  const flagTags = (fs) => (fs || []).map((f) => tag(f, GOOD[f] ? "ok" : (f === "NO_SIGNAL_IMPROVEMENT" || f === "EW_STILL_DOMINANT" || f === "SPY_STILL_DOMINANT" ? "alert" : "warn"))).join(" ") || tag("no flags", "dim");

  function controls() {
    const d = busy ? " disabled" : "";
    const opts = (cfg ? cfg.campaigns : []).map((c) => `<option value="${esc(c.campaign_id)}"${c.campaign_id === campaign ? " selected" : ""}>${esc(c.campaign_id.slice(0, 8))} · ${esc(c.status)} · ${esc(c.n_candidates)} candidates · ${esc(c.run_at)}</option>`).join("");
    return `<section class="cc-card"><div class="cc-head"><h2>SIGNAL RESEARCH</h2><span class="cc-small cc-dimtext">factor ablation · sector-neutral ranking · sector caps · regime exposure overlay · fixed set of at most ${esc(cfg ? cfg.max_variants : 20)} variants · research only</span></div>
      <div class="psr-row"><label><span class="cc-label">STORED CAMPAIGN</span><select data-psr-f="campaign"${d}>${opts || '<option value="">No completed campaign — run one in Model Campaign</option>'}</select></label>
        <button type="button" class="cc-btn cc-primary" data-psr-act="run"${!campaign || busy ? " disabled" : ""}>${busy === "run" ? "Running…" : "Run Signal Research"}</button>
        <span class="cc-small cc-dimtext">${esc(cfg ? cfg.universe_note : "")}</span></div></section>`;
  }

  function variantTable(rows, extra) {
    return `<div class="sf-tablewrap psr-scroll"><table class="sf-table"><thead><tr><th>Variant</th><th>Family</th><th>Total</th><th>CAGR</th><th>Sharpe</th><th>Max DD</th><th>Med. Sharpe</th><th>Beats EW</th><th>Med. excess vs EW</th><th>Excess vs EW @20</th><th>Top-3</th><th>Top sector</th><th>Churn</th>${extra ? "<th>Verdict</th>" : ""}<th>Criteria</th></tr></thead><tbody>
      ${rows.map((v) => { const st = v.stitched ? v.stitched.strategy : {}; const ws = v.windows_summary || {}; const sc = v.sector_concentration || {}; const h = v.holdings || {};
        return `<tr class="${which === v.config_hash ? "prt-why" : ""}"><td><button type="button" class="cc-btn cc-mini" data-psr-act="which" data-hash="${esc(v.config_hash)}">${esc(v.label)}</button></td><td>${esc(v.family)}</td><td>${pct(st.total_return)}</td><td>${pct(st.cagr)}</td><td>${num(st.sharpe)}</td><td>${pct(st.max_drawdown)}</td><td>${num(v.oos ? v.oos.median_sharpe : null)}</td><td>${pct(ws.pct_beating_EW_REBALANCED, 0)}</td><td>${pct(ws.median_excess_vs_ew)}</td><td>${pct(v.cost ? v.cost.excess_vs_ew_at_20 : null)}</td><td>${pct(v.concentration ? v.concentration.top3 : null, 0)}</td><td>${pct(sc.top_sector_share, 0)}</td><td>${pct(h.churn_mean, 0)}</td>${extra ? `<td>${extra(v)}</td>` : ""}<td>${v.criteria ? `${esc(v.criteria.passed)}/${esc(v.criteria.n)}${v.criteria.met ? " " + tag("MET", "ok") : ""}` : "—"}</td></tr>`; }).join("")}</tbody></table></div>`;
  }

  function resultCard() {
    if (!detail) return `<section class="cc-card"><p class="cc-small cc-dimtext">Run the research set on a stored campaign or open a stored run.</p></section>`;
    const r = detail.run;
    const head = `<div class="cc-head"><h2>RUN ${esc((r.run_id || "").slice(0, 8))}</h2>${tag(r.status, r.status === "COMPLETED" ? "ok" : "alert")} ${(r.run_flags || []).map((f) => tag(f, "alert")).join(" ")}
      <span class="cc-small cc-dimtext">campaign ${esc((r.campaign_id || "").slice(0, 8))} · ${esc(r.start_date)} → ${esc(r.end_date)} · ${esc(r.n_windows)} windows · ${esc(r.n_variants)} variants · ${esc(r.n_evaluations)} replays · ${esc(r.runtime_s)} s · rules ${esc(r.rules_version)} · flags ${esc(r.flags_version)} · criteria ${esc(r.criteria_version)}</span></div>`;
    if (r.status !== "COMPLETED") return `<section class="cc-card">${head}<div class="cc-small prt-why">Failed closed: ${esc(r.failure_code)} — ${esc(r.failure_detail)}</div></section>`;
    const views = ["scorecard", "ablation", "sector", "regime", "combined", "benchmarks"].map((v) => `<button type="button" class="cc-btn cc-mini${view === v ? " cc-primary" : ""}" data-psr-act="view" data-view="${v}">${esc(v)}</button>`).join(" ");
    const v = detail.variants.find((x) => x.config_hash === which) || detail.variants[0];
    const card = cards ? (cards.scorecards || []).find((c) => c.config_hash === which) : null;
    let body = "";
    if (view === "scorecard") {
      const pick = detail.variants.map((x) => `<button type="button" class="cc-btn cc-mini${which === x.config_hash ? " cc-primary" : ""}" data-psr-act="which" data-hash="${esc(x.config_hash)}">${esc(x.label)}</button>`).join(" ");
      body = `<div class="psr-row">${pick}</div><div class="psr-row">${flagTags(v.research_flags)}</div><div class="psr-row cc-small">${(v.flags || []).map((f) => tag(f, "dim")).join(" ")}</div>
        <div class="psr-facts">${cell("Ranking", esc(v.research.ranking))}${cell("Sector cap", esc(v.research.max_sector_weight || "none"))}${cell("Regime overlay", esc(v.research.regime_overlay))}${cell("Weights", esc(Object.entries(v.weights || {}).filter(([, w]) => Number(w) > 0).map(([k, w]) => `${k} ${w}`).join(" · ")))}</div>
        <h3 class="cc-small">Predeclared criteria (${esc(v.criteria ? v.criteria.version : "")})</h3><div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Criterion</th><th>Result</th><th>Value</th></tr></thead><tbody>
        ${((v.criteria || {}).checks || []).map((c) => `<tr><td>${esc(c.check)}</td><td>${tag(c.pass ? "PASS" : "FAIL", c.pass ? "ok" : "alert")}</td><td class="cc-small">${esc(typeof c.value === "object" ? JSON.stringify(c.value) : c.value)}</td></tr>`).join("")}</tbody></table></div>
        <h3 class="cc-small">Stage 5.1 scorecard</h3><div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Check</th><th>Verdict</th><th>Value</th></tr></thead><tbody>
        ${((card || {}).scorecard || []).map((s) => `<tr><td>${esc(s.check)}</td><td>${tag(s.verdict, VK[s.verdict] || "dim")}</td><td class="cc-small">${esc(typeof s.value === "object" ? JSON.stringify(s.value) : s.value)}</td></tr>`).join("")}</tbody></table></div>
        <p class="cc-small cc-dimtext">${esc(cfg && cfg.research ? cfg.research.criteria_rule : "")}</p>`;
    } else if (view === "ablation") {
      const fs = abl ? abl.factors : [];
      body = `<p class="cc-small cc-dimtext">${esc(abl ? abl.rule : "")}</p><div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Factor</th><th>Verdict</th><th>Worse</th><th>Better</th><th>Comparisons</th></tr></thead><tbody>
        ${fs.map((f) => `<tr><td>${esc(f.factor)}</td><td>${tag(f.verdict, VK[f.verdict] || "dim")}</td><td>${esc(f.n_worse)}</td><td>${esc(f.n_better)}</td><td class="cc-small">${esc((f.comparisons || []).map((c) => `${c.metric}: ${c.result}`).join(" · "))}</td></tr>`).join("")}</tbody></table></div>
        ${variantTable(abl ? abl.variants : [], null)}`;
    } else if (view === "sector") {
      const rows = sector ? sector.sector_tests : [];
      body = `<p class="cc-small cc-dimtext">${esc(sector ? sector.rule : "")}</p><div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Variant</th><th>Ranking</th><th>Cap</th><th>Top sector</th><th>Top-sector share</th><th>Max sector weight</th><th>Top-3</th><th>Cap changed</th><th>Cap infeasible</th><th>Δ CAGR</th><th>Δ Sharpe</th><th>Δ Max DD</th><th>Δ churn</th></tr></thead><tbody>
        ${rows.map((x) => `<tr><td>${esc(labelOf(x.config_hash))}</td><td>${esc(x.ranking)}</td><td>${esc(x.max_sector_weight || "—")}</td><td>${esc(x.top_sector)}</td><td>${pct(x.top_sector_share, 0)}</td><td>${pct(x.max_sector_weight_observed, 0)}</td><td>${pct(x.top3_share, 0)}</td><td>${esc(x.cap_changed_selection)}</td><td>${esc(x.cap_infeasible)}</td><td>${pct(x.delta.cagr)}</td><td>${num(x.delta.sharpe)}</td><td>${pct(x.delta.max_drawdown)}</td><td>${pct(x.delta.churn, 0)}</td></tr>`).join("")}</tbody></table></div>`;
    } else if (view === "regime") {
      const rows = regime ? regime.regime_tests : [];
      body = `<p class="cc-small cc-dimtext">${esc(regime ? regime.rule : "")}</p><div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Variant</th><th>Overlay</th><th>vs</th><th>Mean exposure</th><th>Exposure by regime</th><th>Δ CAGR</th><th>Δ Sharpe</th><th>Δ Max DD</th><th>Δ TREND_DOWN excess</th><th>Δ HIGH_VOL excess</th></tr></thead><tbody>
        ${rows.map((x) => `<tr><td>${esc(labelOf(x.config_hash))}</td><td>${esc(x.regime_overlay)}</td><td>${esc(labelOf(x.counterpart_hash))}</td><td>${pct(x.mean_exposure, 0)}</td><td class="cc-small">${esc(Object.entries(x.exposure_by_regime || {}).map(([k, e]) => `${k} ${(Number(e) * 100).toFixed(0)}%`).join(" · "))}</td><td>${pct(x.delta.cagr)}</td><td>${num(x.delta.sharpe)}</td><td>${pct(x.delta.max_drawdown)}</td><td>${pct(x.delta.trend_down_excess_vs_ew)}</td><td>${pct(x.delta.high_vol_excess_vs_ew)}</td></tr>`).join("")}</tbody></table></div>`;
    } else if (view === "combined") {
      const rows = combos ? combos.combinations : [];
      body = `<p class="cc-small cc-dimtext">${esc(combos ? combos.rule : "")}</p><div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Variant</th><th>Components</th><th>Criteria met</th><th>Δ CAGR</th><th>Δ Sharpe</th><th>Δ Max DD</th><th>Δ top-3</th><th>Δ top sector</th></tr></thead><tbody>
        ${rows.map((x) => `<tr><td>${esc(labelOf(x.config_hash))}</td><td class="cc-small">${esc((x.components || []).join(" + "))}</td><td>${tag(x.criteria_met ? "MET" : "NOT MET", x.criteria_met ? "ok" : "alert")}</td><td>${pct(x.delta.cagr)}</td><td>${num(x.delta.sharpe)}</td><td>${pct(x.delta.max_drawdown)}</td><td>${pct(x.delta.top3_share, 0)}</td><td>${pct(x.delta.top_sector_share, 0)}</td></tr>`).join("")}</tbody></table></div>`;
    } else {
      const bs = bench ? bench.benchmarks : [];
      const cmp = (bench ? bench.comparisons : []).filter((x) => x.config_hash === which);
      body = `<div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Benchmark</th><th>Cost / slip</th><th>Total</th><th>CAGR</th><th>Sharpe</th><th>Max DD</th></tr></thead><tbody>
        ${bs.map((b) => `<tr><td>${esc(b.benchmark)}</td><td>${esc(b.transaction_cost_bps)} / ${esc(b.slippage_bps)}</td><td>${pct(b.metrics.total_return)}</td><td>${pct(b.metrics.cagr)}</td><td>${num(b.metrics.sharpe)}</td><td>${pct(b.metrics.max_drawdown)}</td></tr>`).join("")}</tbody></table></div>
        <h3 class="cc-small">${esc(v.label)} vs benchmarks</h3><div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Benchmark</th><th>Cost / slip</th><th>Variant total</th><th>Benchmark total</th><th>Excess</th><th>Variant Sharpe</th><th>Benchmark Sharpe</th><th>Windows beating</th><th>Median excess</th></tr></thead><tbody>
        ${cmp.map((x) => `<tr><td>${esc(x.benchmark)}</td><td>${esc(x.transaction_cost_bps)} / ${esc(x.slippage_bps)}</td><td>${pct(x.strategy_total_return)}</td><td>${pct(x.benchmark_total_return)}</td><td>${pct(x.excess)}</td><td>${num(x.strategy_sharpe)}</td><td>${num(x.benchmark_sharpe)}</td><td>${pct(x.pct_windows_beating, 0)}</td><td>${pct(x.median_excess)}</td></tr>`).join("")}</tbody></table></div>`;
    }
    return `<section class="cc-card">${head}<div class="psr-row">${views}</div>${body}</section>`;
  }

  function runsCard() {
    if (!runs.length) return "";
    return `<section class="cc-card"><div class="cc-head"><h2>STORED RESEARCH RUNS</h2><span class="cc-small cc-dimtext">immutable · newest first</span></div>
      <div class="sf-tablewrap"><table class="sf-table"><thead><tr><th>Run</th><th>Campaign</th><th>Status</th><th>Variants</th><th>Windows</th><th>Flags</th><th></th></tr></thead><tbody>
      ${runs.map((r) => `<tr><td><code>${esc(r.run_id.slice(0, 8))}</code></td><td><code>${esc((r.campaign_id || "").slice(0, 8))}</code></td><td>${tag(r.status, r.status === "COMPLETED" ? "ok" : "alert")}</td><td>${esc(r.n_variants)}</td><td>${esc(r.n_windows)}</td><td class="cc-small">${esc((r.run_flags || []).join(", "))}</td>
        <td><button type="button" class="cc-btn cc-mini" data-psr-act="open" data-id="${esc(r.run_id)}"${busy ? " disabled" : ""}>Open</button></td></tr>`).join("")}</tbody></table></div></section>`;
  }

  function draw() {
    root.innerHTML = `<div class="psr"><div class="prt-banner" role="note">${esc(BANNER)}</div>
      ${notice ? `<div class="cc-banner cc-warn cc-small">${esc(notice)}</div>` : ""}${controls()}${resultCard()}${runsCard()}
      <p class="cc-small cc-dimtext">${esc(cfg ? cfg.note : "")}</p></div>`;
  }
  function show() {
    if (!root.dataset.psrBound) {
      root.dataset.psrBound = "1";
      root.addEventListener("click", onClick);
      root.addEventListener("change", (ev) => { const el = ev.target.closest("[data-psr-f]"); if (el && el.dataset.psrF === "campaign") { campaign = el.value; draw(); } });
    }
    draw();
    load();
  }

  window.StrategyFit.addWorkspace({ id: "signalresearch52", label: "Signal Research", cls: "psr-mode", show });
  window.SignalResearch = { get state() { return { busy, notice, requests, campaign, runs: runs.length, run: detail ? { id: detail.run.run_id, status: detail.run.status } : null, view, which }; } };
})();
