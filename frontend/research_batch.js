// research_batch.js — Stage 2.7G.2: the limit-aware research batch used by the 2.7F "Analyze my holdings" action.
//
// Planning is NOT done here: coverage comes from the Stage 2.7G report (POST /api/insights/daily-review ->
// portfolio.coverage) and limits/cache estimates from the Stage 2.7G planner (POST .../research-plan), which is
// called again right before anything runs. Research itself runs only through window.ResearchIds.analyze (the
// existing Stage 2 endpoint with its gating, cache and hourly/daily limits). No timers, no background queue:
// stocks that do not fit stay "still needed" until the user asks again. Presentation reuses the 2.7G classes.
(function () {
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const REASON = { "hourly AI call limit": "Hourly AI limit", "daily AI call limit": "Daily AI limit" };
  const list = (a) => (a && a.length ? a.map(esc).join(", ") : "none");
  const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then((r) => r.json());

  // Current coverage + plan from the 2.7G report (0 Claude calls). null when Robinhood is not connected.
  async function coverage(analysisIds) {
    const r = await post("/api/insights/daily-review", { analysis_ids: analysisIds || {} });
    return r && r.portfolio && r.portfolio.available ? r.portfolio.coverage : null;
  }

  function confirmHtml(cov) {
    const plan = cov.plan;
    if (!cov.need.length) {
      return `<div class="cc-confirm dr-confirm"><div class="cc-label">RESEARCH COVERAGE</div>
        <p>${esc(cov.current)} / ${esc(cov.total)} holdings current — nothing needs research, so no AI calls are needed.</p>
        <button class="cc-btn" data-no>Close</button></div>`;
    }
    const none = !plan.analyze_now.length;
    return `<div class="cc-confirm dr-confirm"><div class="cc-label">ANALYZE MY HOLDINGS</div>
      <div class="dr-cov-grid">
        <div><div class="cc-label">RESEARCH COVERAGE</div><div class="cc-vval">${esc(cov.current)} / ${esc(cov.total)} holdings current</div>
          <div class="cc-small">Need research: ${esc(cov.need.length)}</div></div>
        <div><div class="cc-label">AI CAPACITY</div>
          <div class="cc-small">Hourly limit ${esc(plan.hourly_limit)} · Used this hour ${esc(plan.calls_last_hour)}</div>
          <div class="cc-small">Daily limit ${esc(plan.daily_limit)} · Used today ${esc(plan.calls_today)}</div></div>
        <div><div class="cc-label">CAN ANALYZE NOW</div><div class="cc-small">${list(plan.analyze_now)}</div></div>
        <div><div class="cc-label">WAIT UNTIL LATER</div><div class="cc-small">${list(plan.remaining)}${plan.remaining.length ? ` (${esc(REASON[plan.limited_by] || plan.limited_by)})` : ""}</div></div></div>
      <table class="dr-kv"><tr><th>Estimated maximum new Claude calls</th><td>${esc(plan.calls_for_now)} (cached results count as zero)</td></tr></table>
      ${none ? `<p><strong>No AI capacity right now</strong> (${esc(REASON[plan.limited_by] || "limit reached")}). Nothing will run and no AI calls are made.</p>` : ""}
      ${none ? "" : `<button class="cc-btn cc-primary" data-yes>Analyze available stocks</button> `}<button class="cc-btn" data-no>${none ? "Close" : "Cancel"}</button></div>`;
  }

  // Runs only after confirmation. Limits are re-checked first; only what fits RIGHT NOW is analyzed.
  async function run(need, onProgress) {
    const plan = await post("/api/insights/daily-review/research-plan", { symbols: need });
    if (!plan.analyze_now.length) {
      return { updated: [], still_needed: need.slice(), reason: REASON[plan.limited_by] || "No AI capacity", calls_planned: 0 };
    }
    const updated = [];
    let reason = null;
    for (const [i, sym] of plan.analyze_now.entries()) {
      if (onProgress) onProgress(sym, i + 1, plan.analyze_now.length);
      try { await window.ResearchIds.analyze(sym); updated.push(sym); }
      catch (e) { reason = /503|unavailable/i.test(String(e.message)) ? `Unavailable (${e.message})` : `Error (${e.message})`; break; }
    }
    const still = need.filter((s) => !updated.includes(s));
    if (!reason && still.length) reason = REASON[plan.limited_by] || "Not run";
    return { updated, still_needed: still, reason, calls_planned: plan.calls_for_now };
  }

  function resultHtml(res) {
    return `<div class="dr-batch"><div><strong>Research updated:</strong> ${list(res.updated)}</div>
      <div><strong>Still needed:</strong> ${list(res.still_needed)}</div>
      ${res.still_needed.length && res.reason ? `<div><strong>Reason:</strong> ${esc(res.reason)}</div>` : ""}
      <div class="cc-small cc-dimtext">Nothing continues automatically.</div></div>`;
  }

  // 2.8E: once a run has finished, the result collapses into ONE status line. Details shows the same numbers the
  // confirmation showed (coverage and AI capacity at the time of the run) plus what ran — kept in memory, no request.
  function statusHtml(res) {
    const cov = res.coverage;
    if (!cov) return resultHtml(res);
    const still = res.still_needed.length, current = cov.total - still;
    const reason = res.reason ? `${res.reason}${/limit$/.test(res.reason) ? " reached" : ""}` : "";
    const summary = !still ? "Research up to date"
      : `${still} ${res.updated.length ? "still need research" : "waiting"}${reason ? ` · ${esc(reason)}` : ""}`;
    const plan = cov.plan || {};
    return `<div class="cc-batchbar"><span class="cc-label">RESEARCH COVERAGE</span><b>${esc(current)} / ${esc(cov.total)} current</b>
        <span class="cc-small">${summary}</span>
        <button class="cc-btn cc-mini" data-toggle="cc-batch-details" aria-expanded="false">Details</button></div>
      <div class="cc-panel" id="cc-batch-details" hidden><div class="dr-cov-grid">
        <div><div class="cc-label">BEFORE THIS RUN</div><div class="cc-small">${esc(cov.current)} / ${esc(cov.total)} current · ${esc(cov.need.length)} needed research</div></div>
        <div><div class="cc-label">AI CAPACITY (AT THE TIME OF THE RUN)</div>
          <div class="cc-small">Hourly limit ${esc(plan.hourly_limit)} · Used this hour ${esc(plan.calls_last_hour)}</div>
          <div class="cc-small">Daily limit ${esc(plan.daily_limit)} · Used today ${esc(plan.calls_today)}</div></div>
        <div><div class="cc-label">CAN ANALYZE NOW</div><div class="cc-small">${list(plan.analyze_now)}</div></div>
        <div><div class="cc-label">WAIT UNTIL LATER</div><div class="cc-small">${list(plan.remaining)}</div></div></div>
        <div class="cc-small"><strong>Research updated:</strong> ${list(res.updated)} · <strong>Still needed:</strong> ${list(res.still_needed)}${res.still_needed.length && res.reason ? ` · <strong>Reason:</strong> ${esc(res.reason)}` : ""}</div>
        <div class="cc-small cc-dimtext">Nothing continues automatically.</div></div>`;
  }

  window.ResearchBatch = { coverage, confirmHtml, run, resultHtml, statusHtml };
})();
