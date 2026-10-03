// backtest_lab.js — Stage 3.2 HISTORICAL BACKTEST panel inside the Strategy Lab tab.
//
// Runs one SAVED, BACKTEST READY strategy version over past daily bars under explicit assumptions, and shows what it
// would have done. It never optimises, ranks, recommends, paper trades or places orders, and never calls Claude.
// Server calls: /api/backtests/config (once), the version's run history, explicit "Check data" (local preflight),
// explicit "Download market data" (read-only Alpaca market data), "Run backtest" (one-shot job, polled until done)
// and opening a stored run (run + ledger + audit events). Sorting, filtering, charts and details are client-side.
(function () {
  const root = document.getElementById("bt-body");
  if (!root) return;
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const getJSON = (u) => fetch(u).then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }));
  const send = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })
    .then(async (r) => ({ status: r.status, body: await r.json().catch(() => ({})) }));
  const tag = (t, k) => `<span class="cc-tag cc-${esc(k)}">${esc(t)}</span>`;
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const money = (v) => (isNum(v) ? `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "—");
  const money0 = (v) => (isNum(v) ? `${v < 0 ? "−" : ""}$${Math.abs(Math.round(v)).toLocaleString()}` : "—");
  const pct = (v, d = 2) => (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}%` : "—");
  const num = (v, d = 2) => (isNum(v) ? v.toLocaleString(undefined, { maximumFractionDigits: d }) : "—");
  const cls = (v) => (isNum(v) ? (v > 0 ? "pct-up" : v < 0 ? "pct-down" : "") : "");
  const short = (h) => (h ? String(h).slice(0, 12) : "—");
  const when = (v) => (v ? new Date(v).toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");
  const LABEL = { CONDITION: "Exit condition", INVALIDATION: "Invalidation", TARGET: "Target", MAX_HOLDING: "Max holding", MULTIPLE: "Multiple reasons" };
  const STATUS_TAG = { READY: ["READY TO RUN", "ok"], DATA_REQUIRED: ["DATA REQUIRED", "warn"], BLOCKED: ["BLOCKED", "bad"] };
  const RUN_TAG = { COMPLETED: "ok", FAILED: "bad", RUNNING: "info", PENDING: "dim" };
  const EVENT_TYPES = ["ENTRY_SIGNAL", "ENTRY_FILLED", "ENTRY_SKIPPED", "EXIT_SIGNAL", "EXIT_FILLED", "UNFILLED_ENTRY", "UNFILLED_EXIT"];

  let cfg = null, sel = null, form = null, pf = null, pfKey = "", busy = "", notice = "", history = [];
  let run = null, ledger = [], events = [], tab = "overview", detail = null, pollTimer = null, polling = null;
  let sortKey = "trade_no", sortDir = 1, fSym = "", fReason = "", fEvent = "", showAllCoverage = false;
  let showSpy = true, showUni = false;          // chart lines only; both benchmarks are always in the table

  const formKey = () => JSON.stringify([sel && sel.strategy_id, sel && sel.version_number, form]);
  function body() {
    return { strategy_id: sel.strategy_id, version_number: sel.version_number, start_date: form.start_date, end_date: form.end_date,
      initial_equity: Number(form.initial_equity), slippage_bps_per_side: Number(form.slippage_bps_per_side),
      commission_per_order: Number(form.commission_per_order), selection_policy: "ALPHABETICAL" };
  }

  // ---- charts (inline SVG, drawn from the stored equity rows — no extra requests) ---------------------------------
  function chart(dates, series, opts) {
    const W = 960, H = opts.h || 230, L = 72, R = 12, T = 10, B = 26, n = dates.length;
    const vals = series.flatMap((s) => s.values.filter(isNum));
    if (!n || !vals.length) return `<p class="cc-small cc-dimtext">No data to chart.</p>`;
    let lo = Math.min(...vals, opts.floor != null ? opts.floor : Infinity), hi = Math.max(...vals, opts.ceil != null ? opts.ceil : -Infinity);
    if (hi === lo) { hi += 1; lo -= 1; }
    const padv = (hi - lo) * 0.04; lo -= padv; if (opts.ceil !== 0) hi += padv;
    const x = (i) => L + (n === 1 ? 0 : (i * (W - L - R)) / (n - 1));
    const y = (v) => T + ((hi - v) / (hi - lo)) * (H - T - B);
    const grid = [0, 1, 2, 3, 4].map((k) => { const v = lo + ((hi - lo) * k) / 4; return `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="bt-grid"/><text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" class="bt-axis" text-anchor="end">${esc(opts.fmt(v))}</text>`; }).join("");
    const xl = [0, Math.floor((n - 1) / 2), n - 1].map((i, k) => `<text x="${x(i).toFixed(1)}" y="${H - 8}" class="bt-axis" text-anchor="${k === 0 ? "start" : k === 2 ? "end" : "middle"}">${esc(dates[i])}</text>`).join("");
    const paths = series.map((s) => {
      const pts = s.values.map((v, i) => (isNum(v) ? [x(i), y(v)] : null)).filter(Boolean);
      if (!pts.length) return "";
      const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
      if (s.area) return `<path d="${d}L${pts[pts.length - 1][0].toFixed(1)},${y(0).toFixed(1)}L${pts[0][0].toFixed(1)},${y(0).toFixed(1)}Z" class="${s.cls}"/>`;
      return `<path d="${d}" class="${s.cls}" fill="none"/>`;
    }).join("");
    return `<svg viewBox="0 0 ${W} ${H}" class="bt-chart" role="img" aria-label="${esc(opts.label)}">${grid}${xl}${paths}</svg>`;
  }

  // ---- preflight ------------------------------------------------------------------------------------------------
  function coverageTable(c) {
    const rows = c.rows.filter((r) => showAllCoverage || r.role !== "BREADTH_LIST");
    const breadth = c.rows.filter((r) => r.role === "BREADTH_LIST");
    const bSummary = breadth.length ? `<tr class="bt-group"><td><b>Breadth list</b><div class="cc-small cc-dimtext">${breadth.length} stocks</div></td><td>BREADTH LIST</td>
      <td colspan="5" class="cc-small">${breadth.filter((r) => r.status === "CACHED").length} with data in range · ${breadth.filter((r) => r.status === "NOT_CACHED").length} not downloaded · ${breadth.filter((r) => r.status === "NO_DATA_IN_RANGE").length} without data · ${breadth.filter((r) => r.starts_late).length} start late
      <button class="cc-btn cc-mini cc-link" data-bt="coverage">${showAllCoverage ? "hide list" : "show list"}</button></td></tr>` : "";
    const st = (r) => (r.status === "CACHED" ? (r.starts_late ? tag("STARTS LATE", "warn") : r.warmup_ok === false ? tag("PARTIAL WARM-UP", "warn") : tag("✓ CACHED", "ok"))
      : r.status === "NOT_CACHED" ? tag("NOT DOWNLOADED", "warn") : tag("NO DATA", "bad"));
    return `<table class="data-table bt-cov"><tr><th>Symbol</th><th>Role</th><th>Status</th><th>Available in period</th><th>Sessions</th><th>Warm-up</th><th>Missing sessions</th></tr>
      ${rows.map((r) => `<tr><td><b>${esc(r.symbol)}</b></td><td class="cc-small">${esc(r.role.replace(/_/g, " "))}</td><td>${st(r)}</td>
        <td class="cc-small">${r.first_session ? `${esc(r.first_session)} → ${esc(r.last_session)}` : "—"}</td><td>${r.sessions != null ? esc(r.sessions) : "—"}</td>
        <td class="cc-small">${r.warmup_sessions != null ? `${esc(r.warmup_sessions)} before start` : "—"}</td>
        <td class="cc-small">${r.missing_sessions ? `${esc(r.missing_sessions)} (e.g. ${esc((r.missing_examples || []).join(", "))})` : r.status === "CACHED" ? "0" : "—"}</td></tr>`).join("")}${bSummary}</table>`;
  }
  function preflightView() {
    if (!pf) return "";
    const [label, kind] = STATUS_TAG[pf.status] || [pf.status, "dim"];
    const checks = pf.checks.map((c) => `<li class="cc-small"><span class="sl-mark cc-${c.ok ? "ok" : "bad"}">${c.ok ? "✓" : "✕"}</span> ${esc(c.label)}${c.detail ? ` <span class="cc-dimtext">· ${esc(c.detail)}</span>` : ""}</li>`).join("");
    const errs = pf.errors.length ? `<ul class="sl-errors">${pf.errors.map((e) => `<li><b>${esc(e.code.replace(/_/g, " "))}</b><div class="cc-small">${esc(e.message)}</div></li>`).join("")}</ul>` : "";
    const dl = pf.download ? `<div class="cc-banner bt-download"><b>MARKET DATA NEEDED</b> — ${esc(pf.download.count)} symbol${pf.download.count === 1 ? "" : "s"}
        (${esc(pf.download.symbols.slice(0, 12).join(", "))}${pf.download.count > 12 ? ` and ${pf.download.count - 12} more` : ""})${pf.download.breadth_list ? `, including ${esc(pf.download.breadth_list)} from the breadth list` : ""},
        daily bars ${esc(pf.download.fetch_start)} → ${esc(pf.download.fetch_end)} · about ${esc(pf.download.estimated_bars.toLocaleString())} bars in ${esc(pf.download.request)}.
        <div class="cc-actions"><button class="cc-btn cc-primary" data-bt="download"${busy ? " disabled" : ""}>${busy === "downloading" ? "Downloading…" : "Download market data"}</button></div></div>` : "";
    const warns = pf.warnings && pf.warnings.length ? `<div class="cc-label">WARNINGS</div><ul class="bt-warns">${pf.warnings.map((w) => `<li class="cc-small"><span class="sl-mark cc-warn">⚠</span> ${esc(w.text)}</li>`).join("")}</ul>` : "";
    return `<div class="bt-pre"><div class="bt-pre-head">${tag(label, kind)} <span class="cc-small cc-dimtext">checked from the local cache — no network</span></div>
      <ul class="sl-why">${checks}</ul>${errs}${dl}
      ${pf.coverage ? `<div class="cc-label">DATA COVERAGE · ${esc(pf.coverage.calendar_sessions)} market sessions ${pf.coverage.calendar_first ? `(${esc(pf.coverage.calendar_first)} → ${esc(pf.coverage.calendar_last)})` : ""} · warm-up needed ${esc(pf.coverage.required_warmup_sessions)} sessions</div>${coverageTable(pf.coverage)}` : ""}
      ${warns}</div>`;
  }

  // ---- configuration panel --------------------------------------------------------------------------------------
  function setup() {
    const ready = sel.readiness === "BACKTEST_READY" && sel.integrity === "OK";
    const zero = Number(form.slippage_bps_per_side) === 0 && Number(form.commission_per_order) === 0;
    const running = polling && (polling.status === "PENDING" || polling.status === "RUNNING");
    const canRun = pf && pf.status === "READY" && pfKey === formKey() && !busy && !running;
    const lim = cfg.limits;
    return `<section class="cc-card bt-setup"><div class="cc-head"><h2>HISTORICAL BACKTEST</h2>
        <div class="cc-head-tools"><button class="cc-btn cc-mini cc-link" data-bt="close">Close</button></div></div>
      <div class="bt-meta"><div><span class="cc-label">STRATEGY</span><div><b>${esc(sel.name)}</b></div></div>
        <div><span class="cc-label">VERSION</span><div>v${esc(sel.version_number)} · <code>${esc(short(sel.spec_hash))}</code></div></div>
        <div><span class="cc-label">READINESS</span><div>${sel.readiness === "BACKTEST_READY" ? tag("BACKTEST READY", "ok") : sel.readiness === "FORWARD_TEST_ONLY" ? tag("FORWARD TEST ONLY", "warn") : tag(sel.readiness || "UNKNOWN", "dim")}</div></div></div>
      ${!ready ? `<div class="cc-banner cc-warn">${sel.integrity !== "OK" ? "This stored version failed its hash check, so it cannot be backtested." : sel.readiness === "FORWARD_TEST_ONLY"
        ? "Backtest unavailable — this version is FORWARD TEST ONLY: some rules use data (research, events) that cannot be rebuilt for past dates. Save a version without those rules to backtest it."
        : "Backtest unavailable — this version uses data no strategy may use yet."}</div>` : `
      <div class="bt-form">
        <label><span class="cc-label">DATE RANGE</span><span class="bt-dates"><input type="date" data-bf="start_date" value="${esc(form.start_date)}" min="${esc(lim.earliest_start)}" max="${esc(cfg.data.last_complete_session)}"> → <input type="date" data-bf="end_date" value="${esc(form.end_date)}" min="${esc(lim.earliest_start)}" max="${esc(cfg.data.last_complete_session)}"></span></label>
        <label><span class="cc-label">INITIAL EQUITY</span><span>$ <input type="number" step="1000" min="${lim.initial_equity[0]}" max="${lim.initial_equity[1]}" data-bf="initial_equity" value="${esc(form.initial_equity)}"></span></label>
        <label><span class="cc-label">SLIPPAGE</span><span><input type="number" step="0.5" min="0" max="${lim.slippage_bps_per_side[1]}" data-bf="slippage_bps_per_side" value="${esc(form.slippage_bps_per_side)}"> bps / side</span></label>
        <label><span class="cc-label">COMMISSION</span><span>$ <input type="number" step="0.01" min="0" max="${lim.commission_per_order[1]}" data-bf="commission_per_order" value="${esc(form.commission_per_order)}"> / order</span></label>
        <div><span class="cc-label">ENTRY SELECTION</span><div class="cc-small">Alphabetical when capacity is limited <span class="cc-dimtext">— a neutral tie-break, not a ranking</span></div></div>
      </div>
      <div class="cc-small cc-dimtext">These fields are configuration values, not recommended assumptions. ${zero ? `<span class="cc-warn-text">Zero slippage and commission is an optimistic, cost-free assumption.</span>` : ""}</div>
      <div class="cc-small cc-dimtext">${esc(cfg.execution_model.text)} Data: ${esc(cfg.data.source)} · feed ${esc(cfg.data.feed)} · adjustment ${esc(cfg.data.adjustment)} · last complete session ${esc(cfg.data.last_complete_session)}.</div>
      <div class="cc-actions"><button class="cc-btn" data-bt="check"${busy ? " disabled" : ""}>${busy === "checking" ? "Checking…" : "Check data"}</button>
        <button class="cc-btn cc-primary" data-bt="run"${canRun ? "" : " disabled"}>${busy === "starting" ? "Starting…" : "Run backtest"}</button>
        ${pf && pfKey !== formKey() ? `<span class="cc-small cc-dimtext">Settings changed — check data again.</span>` : ""}</div>
      ${notice ? `<div class="cc-banner cc-warn">${esc(notice)}</div>` : ""}
      ${running ? progressView() : ""}
      ${preflightView()}`}
      ${historyView()}</section>`;
  }
  function progressView() {
    const p = polling.progress || {};
    const frac = p.total ? Math.round((100 * p.done) / p.total) : 0;
    return `<div class="bt-progress"><div class="cc-small"><b>${esc(polling.status)}</b> · ${esc((p.phase || "QUEUED").replace(/_/g, " ").toLowerCase())}${p.total ? ` · ${esc(p.done)}/${esc(p.total)} sessions` : ""}</div>
      <div class="bt-bar"><span style="width:${frac}%"></span></div><div class="cc-small cc-dimtext">A one-shot local job for this run only — nothing is scheduled.</div></div>`;
  }
  function historyView() {
    return `<div class="bt-history"><div class="cc-label">BACKTEST HISTORY · v${esc(sel.version_number)}</div>
      ${history.length ? history.map((h) => { const s = h.summary || {}; const c = h.config || {};
        return `<div class="bt-run${run && run.run_id === h.run_id ? " sl-on" : ""}"><b>#${esc(h.run_id.slice(0, 6))}</b>
          <span class="cc-small">${esc(h.start_date)} → ${esc(h.end_date)} · ${money0(c.capital && c.capital.initial_equity)} · ${esc(num(c.costs && c.costs.slippage_bps_per_side, 2))} bps · ${money(c.costs && c.costs.commission_per_order)}/order</span>
          ${tag(h.status, RUN_TAG[h.status] || "dim")}
          <span class="cc-small">${h.status === "COMPLETED" ? `${esc(s.closed_trades)} closed trade${s.closed_trades === 1 ? "" : "s"} · ${pct(s.total_return_pct)}` : h.status === "FAILED" ? esc(h.error_code || "") : ""}</span>
          <span class="cc-small cc-dimtext">${esc(when(h.created_at))}</span>
          ${h.status === "COMPLETED" || h.status === "FAILED" ? `<button class="cc-btn cc-mini" data-bt-open="${esc(h.run_id)}">Open</button>` : ""}</div>`; }).join("")
        : `<p class="cc-small cc-dimtext">No backtests of this version yet.</p>`}</div>`;
  }

  // ---- result ---------------------------------------------------------------------------------------------------
  function resultHeader() {
    const c = run.config, r = run.result;
    const syms = (r && r.needs && r.needs.universe) || [];
    return `<div class="bt-rhead">
      <div><span class="cc-label">STRATEGY</span><div><b>${esc(sel && sel.name)}</b> · v${esc(run.version_number)}</div></div>
      <div><span class="cc-label">SPEC / RULES HASH</span><div><code>${esc(short(run.spec_hash))}</code> / <code>${esc(short(run.rules_hash))}</code></div></div>
      <div><span class="cc-label">REGISTRY</span><div>v${esc(run.feature_registry_version)} · <code>${esc(short(run.feature_registry_fingerprint))}</code></div></div>
      <div><span class="cc-label">PERIOD</span><div>${esc(run.start_date)} → ${esc(run.end_date)}${r ? ` · ${esc(r.metrics.sessions)} sessions` : ""}</div>
        ${run.equity && run.equity.length ? `<div class="cc-small cc-dimtext">sessions ${esc(run.equity[0].session_date)} → ${esc(run.equity[run.equity.length - 1].session_date)}</div>` : ""}</div>
      <div><span class="cc-label">SYMBOLS</span><div class="cc-small">${esc(syms.join(", "))}</div></div>
      <div><span class="cc-label">INITIAL EQUITY</span><div>${money0(c.capital.initial_equity)}</div></div>
      <div><span class="cc-label">COSTS</span><div>${esc(num(c.costs.slippage_bps_per_side))} bps / side · ${money(c.costs.commission_per_order)} / order${c.costs.slippage_bps_per_side === 0 && c.costs.commission_per_order === 0 ? ` ${tag("COST-FREE (OPTIMISTIC)", "warn")}` : ""}</div></div>
      <div><span class="cc-label">EXECUTION</span><div class="cc-small">decide at close · fill next open · long only · cash only · whole shares · ${esc(c.execution.selection_policy.toLowerCase())} contention</div></div>
      <div><span class="cc-label">DATA</span><div class="cc-small">${esc(c.data.source)} · ${esc(c.data.feed)} · adjustment ${esc(c.data.adjustment)}</div></div>
      <div><span class="cc-label">RUN</span><div class="cc-small">#${esc(run.run_id.slice(0, 6))} · config <code>${esc(short(run.config_hash))}</code> · data <code>${esc(short(run.data_hash))}</code> · ${esc(when(run.created_at))}</div></div>
      <div class="bt-badges">${run.status === "COMPLETED" && run.point_in_time_safe ? tag("POINT-IN-TIME SAFE", "ok") : ""} ${tag(run.status, RUN_TAG[run.status] || "dim")}</div></div>`;
  }
  function metricCard(label, value, formula, klass) {
    return `<div class="bt-metric"${formula ? ` title="${esc(formula)}"` : ""}><span class="cc-label">${esc(label)}</span><div class="bt-mval ${klass || ""}">${value}</div></div>`;
  }
  function overview() {
    const r = run.result, m = r.metrics, f = r.formulas, eq = run.equity;
    const dates = eq.map((e) => e.session_date);
    const pfv = m.profit_factor.text ? esc(m.profit_factor.text) : num(m.profit_factor.value, 2);
    const best = m.best_trade, worst = m.worst_trade;
    const sample = r.sample;
    const cards = [
      metricCard("Historical result", pct(m.total_return_pct), f.total_return_pct, cls(m.total_return_pct)),
      metricCard("Ending equity", money(m.ending_equity), f.total_return_pct),
      metricCard("Maximum drawdown", pct(m.max_drawdown_pct), f.max_drawdown_pct),
      metricCard("Annualized return", m.annualized_return_pct == null ? `<span class="cc-small">${esc(m.annualized_note)}</span>` : pct(m.annualized_return_pct), f.annualized_return_pct),
      metricCard("Closed trades", esc(m.closed_trades)),
      metricCard("Open positions at end", esc(m.open_positions_at_end)),
      metricCard("Winning / losing", `${esc(m.winning_trades)} / ${esc(m.losing_trades)}${m.breakeven_trades ? ` <span class="cc-small cc-dimtext">(${esc(m.breakeven_trades)} flat)</span>` : ""}`, f.win_rate_pct),
      metricCard("Win rate", m.win_rate_pct == null ? "—" : `${num(m.win_rate_pct, 1)}%`, f.win_rate_pct),
      metricCard("Average trade (expectancy)", pct(m.average_trade_return_pct), f.expectancy_pct, cls(m.average_trade_return_pct)),
      metricCard("Median trade", pct(m.median_trade_return_pct), f.median_trade_return_pct, cls(m.median_trade_return_pct)),
      metricCard("Average winner / loser", `${pct(m.average_winner_pct)} / ${pct(m.average_loser_pct)}`, `${f.average_winner_pct}; ${f.average_loser_pct}`),
      metricCard("Gross profit / loss", `${money0(m.gross_profit)} / ${money0(m.gross_loss)}`, `${f.gross_profit}; ${f.gross_loss}`),
      metricCard("Profit factor", pfv, f.profit_factor),
      metricCard("Holding days (avg / median)", `${num(m.average_holding_days, 1)} / ${num(m.median_holding_days, 1)}`, f.holding_days),
      metricCard("Average MFE / MAE", `${pct(m.average_mfe_pct)} / ${pct(m.average_mae_pct)}`, `${f.mfe_pct}; ${f.mae_pct}`),
      metricCard("Best / worst closed trade", `${best ? `${esc(best.symbol)} ${pct(best.return_pct)}` : "—"} / ${worst ? `${esc(worst.symbol)} ${pct(worst.return_pct)}` : "—"}`),
      metricCard("Time in market", `${num(m.time_in_market_pct, 1)}%`, f.time_in_market_pct),
      metricCard("Simultaneous positions (max / avg)", `${esc(m.max_simultaneous_positions)} / ${num(m.average_simultaneous_positions, 2)}`, f.simultaneous_positions),
      metricCard("Costs paid", `${money(m.total_commission)} commission · ${money(m.total_slippage_impact)} slippage`),
      metricCard("Realized / unrealized P&L", `${money(m.realized_pnl)} / ${money(m.unrealized_pnl)}`),
    ].join("");
    const b = r.benchmarks;
    const eqChart = chart(dates, [
      ...(showUni ? [{ values: eq.map((e) => e.universe_benchmark_equity), cls: "bt-l-uni" }] : []),
      ...(showSpy ? [{ values: eq.map((e) => e.spy_benchmark_equity), cls: "bt-l-spy" }] : []),
      { values: eq.map((e) => e.equity), cls: "bt-l-str" }], { fmt: (v) => money0(v), label: "Equity curve" });
    const ddChart = chart(dates, [{ values: eq.map((e) => e.drawdown * 100), cls: "bt-dd", area: true }],
      { fmt: (v) => `${v.toFixed(1)}%`, label: "Drawdown", h: 150, ceil: 0 });
    const openRows = ledger.filter((t) => t.status === "OPEN_AT_END");
    return `<div class="bt-sample bt-s-${esc(sample.code)}"><b>${esc(sample.text)}</b> <span class="cc-small cc-dimtext">${esc(sample.note)}</span></div>
      <div class="bt-metrics">${cards}</div>
      <div class="bt-chartbox"><div class="cc-label">EQUITY — STRATEGY VS BENCHMARK CONTEXT (same starting capital, marked at each close)</div>${eqChart}
        <div class="bt-legend cc-small"><span><span class="bt-k bt-k-str"></span> Strategy</span>
          <label><input type="checkbox" data-bt-line="spy"${showSpy ? " checked" : ""}> <span class="bt-k bt-k-spy"></span> ${esc(b.spy.symbol)} buy-and-hold</label>
          <label><input type="checkbox" data-bt-line="uni"${showUni ? " checked" : ""}> <span class="bt-k bt-k-uni"></span> Universe equal-weight buy-and-hold</label>
          <span class="cc-dimtext">(benchmark lines can be hidden when one dominates the scale; all values stay in the table below)</span></div></div>
      <div class="bt-chartbox"><div class="cc-label">DRAWDOWN — equity vs its running peak · maximum ${pct(m.max_drawdown_pct)}</div>${ddChart}</div>
      <div class="cc-label">BENCHMARK CONTEXT</div>
      <table class="data-table"><tr><th></th><th>Total return</th><th>Max drawdown</th><th>Annualized</th><th>Ending equity</th><th>Uninvested cash</th><th>Notes</th></tr>
        <tr><td><b>Strategy</b></td><td class="${cls(m.total_return_pct)}">${pct(m.total_return_pct)}</td><td>${pct(m.max_drawdown_pct)}</td><td>${m.annualized_return_pct == null ? "—" : pct(m.annualized_return_pct)}</td><td>${money(m.ending_equity)}</td><td>—</td><td class="cc-small">${esc(m.closed_trades)} closed · ${esc(m.open_positions_at_end)} open</td></tr>
        <tr><td><b>${esc(b.spy.symbol)} buy-and-hold</b></td><td class="${cls(b.spy.total_return_pct)}">${pct(b.spy.total_return_pct)}</td><td>${pct(b.spy.max_drawdown_pct)}</td><td>${b.spy.annualized_return_pct == null ? "—" : pct(b.spy.annualized_return_pct)}</td><td>${money(b.spy.ending_equity)}</td><td>${money(b.spy.uninvested_cash)}</td><td class="cc-small">bought ${esc(b.spy.first_session)} open</td></tr>
        <tr><td><b>Universe equal-weight</b></td><td class="${cls(b.universe_equal_weight.total_return_pct)}">${pct(b.universe_equal_weight.total_return_pct)}</td><td>${pct(b.universe_equal_weight.max_drawdown_pct)}</td><td>${b.universe_equal_weight.annualized_return_pct == null ? "—" : pct(b.universe_equal_weight.annualized_return_pct)}</td><td>${money(b.universe_equal_weight.ending_equity)}</td><td>${money(b.universe_equal_weight.uninvested_cash)}</td>
          <td class="cc-small">${esc(Object.keys(b.universe_equal_weight.holdings).length)} held${b.universe_equal_weight.excluded.length ? ` · not bought: ${esc(b.universe_equal_weight.excluded.map((x) => x.symbol).join(", "))}` : ""}</td></tr></table>
      <p class="cc-small cc-dimtext">${esc(b.note)}</p>
      <div class="cc-label">OPEN POSITIONS AT END · ${esc(openRows.length)}</div>
      ${openRows.length ? `<table class="data-table"><tr><th>Symbol</th><th>Entry fill</th><th>Entry price</th><th>Shares</th><th>Mark (${esc(openRows[0].mark_date)})</th><th>Unrealized P&L</th><th>Holding days</th></tr>
        ${openRows.map((t) => `<tr><td><b>${esc(t.symbol)}</b></td><td>${esc(t.entry_fill_date)}</td><td>${money(t.entry_fill_price)}</td><td>${esc(t.shares)}</td><td>${money(t.mark_price)}</td><td class="${cls(t.unrealized_pnl)}">${money(t.unrealized_pnl)}</td><td>${esc(t.holding_days)}</td></tr>`).join("")}</table>
        <p class="cc-small cc-dimtext">Still open at the end: marked to the last close (before exit costs), included in equity, not counted as completed trades.</p>` : `<p class="cc-small cc-dimtext">None.</p>`}`;
  }

  const COLS = [["symbol", "Symbol"], ["entry_signal_date", "Entry signal"], ["entry_fill_date", "Entry fill"], ["entry_fill_price", "Entry price"],
    ["exit_signal_date", "Exit signal"], ["exit_fill_date", "Exit fill"], ["exit_fill_price", "Exit price"], ["return_pct", "Return"],
    ["pnl_dollars", "P&L"], ["holding_days", "Days"], ["mfe_pct", "MFE"], ["mae_pct", "MAE"], ["primary_exit_reason", "Exit reason"]];
  function tradesView() {
    const closed = ledger.filter((t) => t.status === "CLOSED");
    const symbols = [...new Set(closed.map((t) => t.symbol))].sort();
    const reasonOf = (t) => (t.all_exit_reasons.length > 1 ? "MULTIPLE" : t.all_exit_reasons[0]);
    const rows = closed.filter((t) => (!fSym || t.symbol === fSym) && (!fReason || reasonOf(t) === fReason))
      .sort((a, b) => { const x = a[sortKey], y = b[sortKey]; return (x === y ? a.trade_no - b.trade_no : x == null ? 1 : y == null ? -1 : x < y ? -1 : 1) * (x === y ? 1 : sortDir); });
    const cell = (t, k) => (k === "return_pct" || k === "mfe_pct" || k === "mae_pct" ? `<td class="${cls(t[k])}">${pct(t[k])}</td>`
      : k === "pnl_dollars" ? `<td class="${cls(t[k])}">${money(t[k])}</td>` : k.endsWith("price") ? `<td>${money(t[k])}</td>`
        : k === "primary_exit_reason" ? `<td class="cc-small">${esc(LABEL[reasonOf(t)] || reasonOf(t))}${t.all_exit_reasons.length > 1 ? ` <span class="cc-dimtext">(${esc(t.all_exit_reasons.map((x) => LABEL[x] || x).join(" + "))})</span>` : ""}</td>`
          : k === "symbol" ? `<td><b>${esc(t[k])}</b></td>` : `<td>${esc(t[k])}</td>`);
    return `<div class="bt-filters"><label class="cc-small">Symbol <select data-bt-f="sym"><option value="">All</option>${symbols.map((s) => `<option${s === fSym ? " selected" : ""}>${esc(s)}</option>`).join("")}</select></label>
        <label class="cc-small">Exit reason <select data-bt-f="reason"><option value="">All</option>${["CONDITION", "INVALIDATION", "TARGET", "MAX_HOLDING", "MULTIPLE"].map((r) => `<option value="${r}"${r === fReason ? " selected" : ""}>${esc(LABEL[r])}</option>`).join("")}</select></label>
        <span class="cc-small cc-dimtext">${esc(rows.length)} of ${esc(closed.length)} closed trades · click a column to sort, a row for details</span></div>
      <div class="bt-tablewrap bt-scroll"><table class="data-table bt-trades"><tr>${COLS.map(([k, l]) => `<th data-bt-sort="${k}" class="bt-sortable">${esc(l)}${sortKey === k ? (sortDir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr>
        ${rows.map((t) => `<tr data-bt-trade="${esc(t.trade_no)}" class="${detail === t.trade_no ? "bt-selrow" : ""}">${COLS.map(([k]) => cell(t, k)).join("")}</tr>`).join("") || `<tr><td colspan="${COLS.length}" class="cc-small cc-dimtext">No closed trades.</td></tr>`}</table></div>
      ${detail != null ? tradeDetail(ledger.find((t) => t.trade_no === detail)) : ""}`;
  }
  function traceRows(trace) {
    return (trace || []).map((t) => (t.children ? `<tr><td colspan="4" class="cc-small">(${esc(t.group)} group: ${esc(t.result)})</td></tr>${traceRows(t.children)}`
      : `<tr><td class="cc-small"><b>${esc(t.feature)}</b></td><td class="cc-small">${esc(t.op)} ${esc(Array.isArray(t.expected) ? t.expected.join(", ") : t.expected)}</td>
        <td class="cc-small">${t.actual == null ? `<span class="cc-dimtext">unavailable</span>` : esc(typeof t.actual === "number" ? num(t.actual, 4) : t.actual)}</td>
        <td>${t.result === "MET" ? tag("MET", "ok") : t.result === "UNAVAILABLE" ? tag(t.availability === "INSUFFICIENT_HISTORY" ? "INSUFFICIENT HISTORY" : "UNAVAILABLE", "dim") : tag("NOT MET", "dim")}</td></tr>`)).join("");
  }
  function snapTable(snap) {
    if (!snap) return `<p class="cc-small cc-dimtext">No feature snapshot.</p>`;
    return `<div class="cc-small cc-dimtext">Evaluated at the close of ${esc(snap.session)} (${esc(snap.evaluated_at)}) from bars dated on or before that session.</div>
      <table class="data-table"><tr><th>Feature</th><th>Value</th><th>Availability</th><th>Source</th></tr>
      ${snap.features.map((f) => `<tr><td class="cc-small"><b>${esc(f.name)}</b><div class="cc-dimtext">${esc(f.feature_id)}</div></td>
        <td class="cc-small">${f.value == null ? "—" : esc(typeof f.value === "number" ? num(f.value, 4) : String(f.value))}</td>
        <td class="cc-small">${esc(f.availability.replace(/_/g, " "))}</td><td class="cc-small cc-dimtext">${esc(f.source)}</td></tr>`).join("")}</table>`;
  }
  function tradeDetail(t) {
    if (!t) return "";
    const x = t.exit_evaluation_trace || {}, c = run.config.costs;
    const rule = (k, lab) => (x[k] ? `<li class="cc-small">${x[k].hit ? tag("HIT", "warn") : tag("NOT HIT", "dim")} ${esc(lab)} — ${k === "max_holding" ? `holding day ${esc(x[k].holding_days)} of max ${esc(x[k].max_holding_days)}` : `close ${money(x[k].close)} vs level ${money(x[k].level)} (${esc(x[k].method.replace(/_/g, " ").toLowerCase())})`}</li>` : "");
    return `<section class="bt-detail"><div class="cc-head"><h3>TRADE #${esc(t.trade_no)} · ${esc(t.symbol)}</h3><button class="cc-btn cc-mini cc-link" data-bt="closedetail">Close</button></div>
      <div class="bt-detail-grid">
        <div><div class="cc-label">ENTRY DECISION · signal at close ${esc(t.entry_signal_date)}</div>
          <table class="data-table"><tr><th>Condition</th><th>Rule</th><th>Actual</th><th>Result</th></tr>${traceRows((t.entry_evaluation_trace || {}).trace)}</table>
          <div class="cc-label">ENTRY FEATURE SNAPSHOT</div>${snapTable(t.entry_feature_snapshot)}</div>
        <div><div class="cc-label">ENTRY LEVELS (frozen at the entry signal)</div>
          <ul class="sl-why"><li class="cc-small">Support at entry: <b>${money(t.entry_support)}</b></li><li class="cc-small">Resistance at entry: <b>${money(t.entry_resistance)}</b></li>
            <li class="cc-small">Invalidation level: <b>${money(t.invalidation_level)}</b></li><li class="cc-small">Target level: <b>${money(t.target_level)}</b></li>
            <li class="cc-small">Market trend at entry: <b>${esc(t.market_trend_at_entry || "—")}</b>${t.market_environment_at_entry ? ` · environment <b>${esc(t.market_environment_at_entry)}</b>` : ""}</li></ul>
          <div class="cc-label">EXIT DECISION ${t.exit_signal_date ? `· signal at close ${esc(t.exit_signal_date)}` : ""}</div>
          ${t.exit_signal_date ? `<div class="cc-small">Reasons true: <b>${esc(t.all_exit_reasons.map((r) => LABEL[r] || r).join(", "))}</b>${t.all_exit_reasons.length > 1 ? ` · shown as ${esc(LABEL[t.primary_exit_reason])} (fixed order: condition, invalidation, target, max holding)` : ""}</div>
            <ul class="sl-why">${rule("invalidation", "Invalidation")}${rule("target", "Target")}${rule("max_holding", "Maximum holding")}</ul>
            ${x.conditions ? `<table class="data-table"><tr><th>Exit condition</th><th>Rule</th><th>Actual</th><th>Result</th></tr>${traceRows(x.conditions.trace)}</table>` : ""}`
            : `<p class="cc-small cc-dimtext">No exit signal — still open at the end.</p>`}
          <div class="cc-label">EXECUTION + ASSUMPTIONS</div>
          <ul class="sl-why"><li class="cc-small">Entry: ${esc(t.shares)} shares at ${money(t.entry_fill_price)} on ${esc(t.entry_fill_date)} (open ${money(t.entry_open_price)} + ${esc(num(c.slippage_bps_per_side))} bps slippage)</li>
            ${t.exit_fill_date ? `<li class="cc-small">Exit: at ${money(t.exit_fill_price)} on ${esc(t.exit_fill_date)} (open ${money(t.exit_open_price)} − ${esc(num(c.slippage_bps_per_side))} bps)${t.exit_fill_delay_sessions ? ` · filled ${esc(t.exit_fill_delay_sessions)} session(s) late (no bar)` : ""}</li>` : ""}
            <li class="cc-small">Commission ${money(t.commission)} (${money(c.commission_per_order)} per order) · slippage impact ${money(t.slippage_impact)}</li>
            <li class="cc-small">${t.status === "CLOSED" ? `P&L ${money(t.pnl_dollars)} · return ${pct(t.return_pct)}` : `Unrealized ${money(t.unrealized_pnl)} at ${money(t.mark_price)}`} · MFE ${pct(t.mfe_pct)} · MAE ${pct(t.mae_pct)} · ${esc(t.holding_days)} holding day(s)</li></ul></div></div></section>`;
  }
  function groupTable(title, rows, first) {
    if (!rows) return "";
    return `<div class="cc-label">${esc(title)}</div><table class="data-table"><tr><th>${esc(first)}</th><th>Closed trades</th><th>Win rate</th><th>Avg return</th><th>Realized P&L</th><th>Avg days</th><th>Avg MFE</th><th>Avg MAE</th></tr>
      ${rows.length ? rows.map((g) => `<tr><td><b>${esc(LABEL[g.group] || g.group)}</b>${g.closed_trades < 10 ? ` <span class="cc-small cc-dimtext">n=${esc(g.closed_trades)} · very small</span>` : g.closed_trades < 30 ? ` <span class="cc-small cc-dimtext">n=${esc(g.closed_trades)} · small</span>` : ""}</td>
        <td>${esc(g.closed_trades)}</td><td>${g.win_rate_pct == null ? "—" : `${num(g.win_rate_pct, 1)}%`}</td><td class="${cls(g.average_return_pct)}">${pct(g.average_return_pct)}</td>
        <td class="${cls(g.realized_pnl)}">${money(g.realized_pnl)}</td><td>${num(g.average_holding_days, 1)}</td><td>${pct(g.average_mfe_pct)}</td><td>${pct(g.average_mae_pct)}</td></tr>`).join("") : `<tr><td colspan="8" class="cc-small cc-dimtext">No closed trades.</td></tr>`}</table>`;
  }
  function breakdownsView() {
    const b = run.result.breakdowns;
    return `<p class="cc-small cc-dimtext">${esc(b.note)}</p>
      ${groupTable("MARKET TREND AT ENTRY (SPY-based label: Improving / Mixed / Weakening)", b.market_trend_at_entry, "Market trend")}
      ${b.market_environment_at_entry ? groupTable("TRADING ENVIRONMENT AT ENTRY", b.market_environment_at_entry, "Environment") : ""}
      ${groupTable("BY SYMBOL", b.symbol, "Symbol")}
      ${groupTable("BY EXIT REASON (a trade with more than one true reason is grouped as Multiple)", b.exit_reason, "Exit reason")}
      <div class="cc-small cc-dimtext">Reason appearances across closed trades: ${Object.entries(b.exit_reason_appearances).map(([k, v]) => `${esc(LABEL[k])} ${esc(v)}`).join(" · ")}</div>`;
  }
  function auditView() {
    const a = run.result.audit, c = run.config, d = run.data || {}, pit = run.result.point_in_time_checks;
    const shown = events.filter((e) => !fEvent || e.event_type === fEvent);
    const counts = EVENT_TYPES.map((t) => `${esc(t.replace(/_/g, " ").toLowerCase())} <b>${esc(events.filter((e) => e.event_type === t).length)}</b>`).join(" · ");
    const reasons = (o) => Object.entries(o || {}).map(([k, v]) => `${esc(k.replace(/_/g, " ").toLowerCase())} ${esc(v)}`).join(" · ") || "none";
    return `<div class="bt-auditgrid"><div><div class="cc-label">SIGNALS</div><ul class="sl-why">
        <li class="cc-small">Entry evaluations ${esc(a.entry_evaluations)} · signals considered <b>${esc(a.signals_considered)}</b> · filled <b>${esc(a.signals_filled)}</b></li>
        <li class="cc-small">Skipped: ${reasons(a.skipped)}</li><li class="cc-small">Unfilled entries: ${reasons(a.unfilled_entries)} · unfilled exits: ${reasons(a.unfilled_exits)}</li>
        <li class="cc-small">Exit signals ${esc(a.exit_signals)} · exits filled ${esc(a.exits_filled)} · evaluations with insufficient history ${esc(a.evaluations_with_insufficient_history)}</li></ul>
        <div class="cc-label">ASSUMPTIONS</div><ul class="sl-why"><li class="cc-small">${esc(run.result.execution_model.text)}</li>
        <li class="cc-small">Slippage ${esc(num(c.costs.slippage_bps_per_side))} bps per side (entry = open × (1 + bps/10000), exit = open × (1 − bps/10000)); commission ${money(c.costs.commission_per_order)} per order; one exit commission is reserved per open position so cash never goes negative.</li>
        <li class="cc-small">${esc(run.result.selection_policy.text)}</li>
        <li class="cc-small">Position size: strategy equity at the fill-day open × ${esc(num(run.result.risk.max_position_pct))}% (at most ${esc(run.result.risk.max_open_positions)} open positions), whole shares, capped by available cash. No leverage or margin.</li></ul></div>
      <div><div class="cc-label">POINT-IN-TIME CHECKS</div><ul class="sl-why">${Object.entries(pit).map(([k, v]) => `<li class="cc-small">${esc(k.replace(/_/g, " "))}: <b>${esc(String(v))}</b></li>`).join("")}</ul>
        <div class="cc-label">DATA USED (immutable cached datasets)</div>
        <div class="bt-tablewrap bt-small-table"><table class="data-table"><tr><th>Symbol</th><th>Bars</th><th>Range</th><th>Fetched</th><th>Content hash</th></tr>
        ${Object.entries(d.datasets || {}).map(([s, x]) => `<tr><td>${esc(s)}</td><td>${esc(x.bar_count)}</td><td class="cc-small">${esc(x.first_session || "—")} → ${esc(x.last_session || "—")}</td><td class="cc-small">${esc(when(x.fetched_at))}</td><td><code>${esc(short(x.content_hash))}</code></td></tr>`).join("")}</table></div>
        <div class="cc-small cc-dimtext">${esc(d.source)} · feed ${esc(d.feed)} · adjustment ${esc(d.adjustment)} · compute ${esc(run.result.compute.mode.replace(/_/g, " "))}</div></div></div>
      <div class="cc-label">AUDIT EVENTS · ${counts}</div>
      <div class="bt-filters"><label class="cc-small">Type <select data-bt-f="event"><option value="">All</option>${EVENT_TYPES.map((t) => `<option${t === fEvent ? " selected" : ""}>${t}</option>`).join("")}</select></label>
        <span class="cc-small cc-dimtext">${esc(shown.length)} event(s)${shown.length > 400 ? " · showing the first 400" : ""}</span></div>
      <div class="bt-tablewrap bt-scroll"><table class="data-table bt-events"><tr><th>#</th><th>Session</th><th>Symbol</th><th>Event</th><th>Reason</th><th>Trade</th><th>Detail</th></tr>
        ${shown.slice(0, 400).map((e) => `<tr><td>${esc(e.seq)}</td><td>${esc(e.session_date)}</td><td>${esc(e.symbol)}</td><td class="cc-small">${esc(e.event_type.replace(/_/g, " "))}</td><td class="cc-small">${esc((e.reason_code || "").replace(/_/g, " "))}</td>
          <td>${e.trade_no ? `<button class="cc-btn cc-mini cc-link" data-bt-trade="${esc(e.trade_no)}">#${esc(e.trade_no)}</button>` : ""}</td>
          <td class="cc-small cc-dimtext">${esc(eventDetail(e))}</td></tr>`).join("")}</table></div>`;
  }
  function eventDetail(e) {
    const d = e.detail || {};
    if (e.event_type === "ENTRY_FILLED") return `${d.shares} sh at ${money(d.fill_price)} (signal ${d.signal_date})`;
    if (e.event_type === "EXIT_FILLED") return `at ${money(d.fill_price)} · P&L ${money(d.pnl_dollars)}`;
    if (e.event_type === "EXIT_SIGNAL") return (d.reasons || []).map((r) => LABEL[r] || r).join(", ");
    if (e.event_type === "ENTRY_SIGNAL") return `${(d.trace && d.trace.trace || []).length} condition(s) met`;
    if (e.event_type === "ENTRY_SKIPPED" && d.max_open_positions) return `${d.open_positions} of ${d.max_open_positions} slots in use`;
    if (e.event_type === "ENTRY_SKIPPED" && d.available_cash != null) return `needs ${money(d.fill_price)} per share · ${money(d.available_cash)} available`;
    return d.signal_date ? `signal ${d.signal_date}` : "";
  }
  function warningsView() {
    return `<ul class="bt-warns">${run.result.warnings.map((w) => `<li><b>${esc(w.code.replace(/_/g, " "))}</b><div class="cc-small">${esc(w.text)}</div></li>`).join("")}</ul>`;
  }
  function resultView() {
    if (!run) return "";
    if (run.status === "FAILED") {
      return `<section class="cc-card bt-result"><div class="cc-head"><h2>BACKTEST RESULT</h2></div>${resultHeader()}
        <div class="cc-banner cc-warn"><b>FAILED · ${esc(run.error_code)}</b> — ${esc(run.error_message)} No results were saved for this run.</div></section>`;
    }
    const tabs = [["overview", "Overview"], ["trades", "Trades"], ["breakdowns", "Breakdowns"], ["audit", "Audit"], ["warnings", `Warnings (${run.result.warnings.length})`]];
    const body = tab === "trades" ? tradesView() : tab === "breakdowns" ? breakdownsView() : tab === "audit" ? auditView() : tab === "warnings" ? warningsView() : overview();
    return `<section class="cc-card bt-result"><div class="cc-head"><h2>BACKTEST RESULT</h2>
        <div class="cc-head-tools"><button class="cc-btn cc-mini" data-bt="rerun"${busy || (polling && polling.status !== "COMPLETED" && polling.status !== "FAILED") ? " disabled" : ""}>Re-run with same configuration</button></div></div>
      ${resultHeader()}
      <div class="bt-tabs">${tabs.map(([k, l]) => `<button class="bt-tab${tab === k ? " active" : ""}" data-bt-tab="${k}">${esc(l)}</button>`).join("")}</div>
      <div class="bt-tabbody">${body}</div>
      <p class="cc-small cc-dimtext">${esc(run.note || "")} Stored result — opening it never recomputes anything.</p></section>`;
  }

  function render() {
    if (!sel || !cfg) { root.innerHTML = ""; return; }
    root.innerHTML = `<div class="bt">${setup()}${resultView()}</div>`;
    wire();
  }

  // ---- actions ------------------------------------------------------------------------------------------------------
  async function loadHistory() {
    const r = await getJSON(`/api/backtests?strategy_id=${encodeURIComponent(sel.strategy_id)}&version_number=${encodeURIComponent(sel.version_number)}`);
    history = (r.body && r.body.runs) || [];
  }
  async function check() {
    busy = "checking"; notice = ""; render();
    const r = await send("/api/backtests/preflight", body());
    busy = "";
    if (r.status !== 200) { notice = (r.body && (r.body.message || (r.body.detail && JSON.stringify(r.body.detail)))) || "Check failed."; pf = null; }
    else { pf = r.body; pfKey = formKey(); }
    render();
  }
  async function download() {
    busy = "downloading"; notice = ""; render();
    const r = await send("/api/backtests/data", body());
    busy = "";
    if (r.status !== 200) notice = (r.body && r.body.message) || "The download failed; nothing was cached.";
    else { pf = r.body; pfKey = formKey(); }
    render();
  }
  async function start(b) {
    busy = "starting"; notice = ""; render();
    const r = await send("/api/backtests", b);
    busy = "";
    if (r.status !== 202) { notice = (r.body && r.body.message) || "The backtest could not start."; render(); return; }
    polling = { run_id: r.body.run_id, status: "PENDING", progress: null };
    await loadHistory(); render(); poll();
  }
  async function poll() {
    if (!polling) return;
    const r = await getJSON(`/api/backtests/${encodeURIComponent(polling.run_id)}`);
    if (r.status !== 200) { notice = "Lost track of the run; reopen it from the history."; polling = null; render(); return; }
    polling.status = r.body.status; polling.progress = r.body.progress;
    if (r.body.status === "COMPLETED" || r.body.status === "FAILED") {
      const id = polling.run_id; polling = null; await loadHistory(); await openRun(id, r.body); return;
    }
    render();
    pollTimer = setTimeout(poll, 700);
  }
  async function openRun(id, pre) {
    const r = pre ? { status: 200, body: pre } : await getJSON(`/api/backtests/${encodeURIComponent(id)}`);
    if (r.status !== 200) { notice = "Could not open that run."; render(); return; }
    run = r.body; detail = null; tab = "overview"; fSym = ""; fReason = ""; fEvent = "";
    if (run.status === "COMPLETED") {
      const [l, s] = await Promise.all([getJSON(`/api/backtests/${encodeURIComponent(id)}/ledger`), getJSON(`/api/backtests/${encodeURIComponent(id)}/signals`)]);
      ledger = (l.body && l.body.trades) || []; events = (s.body && s.body.events) || [];
    } else { ledger = []; events = []; }
    render();
    const el = root.querySelector(".bt-result");
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "start" });
  }

  function wire() {
    root.querySelectorAll("[data-bf]").forEach((el) => el.addEventListener("change", () => { form[el.dataset.bf] = el.value; render(); }));
    root.querySelectorAll("[data-bt]").forEach((b) => b.addEventListener("click", () => {
      const a = b.dataset.bt;
      if (a === "check") check();
      else if (a === "download") download();
      else if (a === "run") start(body());
      else if (a === "rerun" && run) {
        const c = run.config;
        start({ strategy_id: run.strategy_id, version_number: run.version_number, start_date: c.period.start, end_date: c.period.end,
          initial_equity: c.capital.initial_equity, slippage_bps_per_side: c.costs.slippage_bps_per_side,
          commission_per_order: c.costs.commission_per_order, selection_policy: c.execution.selection_policy });
      } else if (a === "close") { sel = null; run = null; pf = null; render(); }
      else if (a === "closedetail") { detail = null; render(); }
      else if (a === "coverage") { showAllCoverage = !showAllCoverage; render(); }
    }));
    root.querySelectorAll("[data-bt-open]").forEach((b) => b.addEventListener("click", () => openRun(b.dataset.btOpen)));
    root.querySelectorAll("[data-bt-tab]").forEach((b) => b.addEventListener("click", () => { tab = b.dataset.btTab; render(); }));
    root.querySelectorAll("[data-bt-sort]").forEach((th) => th.addEventListener("click", () => {
      const k = th.dataset.btSort; sortDir = sortKey === k ? -sortDir : 1; sortKey = k; render();
    }));
    root.querySelectorAll("[data-bt-trade]").forEach((el) => el.addEventListener("click", () => {
      detail = Number(el.dataset.btTrade); tab = "trades"; render();
      const d = root.querySelector(".bt-detail");
      if (d && d.scrollIntoView) d.scrollIntoView({ block: "nearest" });
    }));
    root.querySelectorAll("[data-bt-line]").forEach((cb) => cb.addEventListener("change", () => {
      if (cb.dataset.btLine === "spy") showSpy = cb.checked; else showUni = cb.checked;
      render();
    }));
    root.querySelectorAll("[data-bt-f]").forEach((s) => s.addEventListener("change", () => {
      if (s.dataset.btF === "sym") fSym = s.value; else if (s.dataset.btF === "reason") fReason = s.value; else fEvent = s.value;
      render();
    }));
  }

  // ---- entry points used by the Strategy Lab (strategy_lab.js) -------------------------------------------------------
  async function select(v) {
    if (pollTimer && (!v || !sel || v.strategy_id !== sel.strategy_id || v.version_number !== sel.version_number)) { clearTimeout(pollTimer); pollTimer = null; polling = null; }
    if (!v) { sel = null; run = null; pf = null; render(); return; }
    if (!cfg) { const r = await getJSON("/api/backtests/config"); if (r.status !== 200) { root.innerHTML = `<div class="cc-banner cc-warn">Backtesting could not load. If the server was started before this update, restart it.</div>`; return; } cfg = r.body; }
    const same = sel && sel.strategy_id === v.strategy_id && sel.version_number === v.version_number;
    sel = v;
    if (!same) { pf = null; run = null; ledger = []; events = []; detail = null; notice = ""; }
    form = form || { start_date: cfg.defaults.start_date, end_date: cfg.defaults.end_date, initial_equity: cfg.defaults.initial_equity,
      slippage_bps_per_side: cfg.defaults.slippage_bps_per_side, commission_per_order: cfg.defaults.commission_per_order };
    await loadHistory();
    render();
  }
  async function open(v) {
    await select(v);
    const el = root.querySelector(".bt-setup");
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "start" });
  }
  window.BacktestLab = { select, open, get state() { return { sel, pf, run, history, polling, busy, tab, detail, ledger: ledger.length, events: events.length }; } };
})();
